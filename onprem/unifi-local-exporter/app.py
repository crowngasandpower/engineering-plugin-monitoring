"""UniFi local controller exporter for Crown monitoring.

Connects directly to each UniFi OS controller (EFG, UDM Pro Max) via the
local controller API using per-controller API keys. All controllers are polled
in parallel on each Prometheus scrape.

Complements the unifi-exporter (Site Manager cloud API), which provides fleet
overview and VPN-failure detection. This exporter provides the per-device
operational detail the cloud API cannot access.

If a controller is unreachable (e.g. SDWAN link down), it contributes
unifi_scrape_success=0 and no other samples — the remaining controllers
continue normally.

Environment (N = 1..4):
  UNIFI_{N}_URL        — controller base URL, e.g. https://192.168.164.215
  UNIFI_{N}_NAME       — friendly site name used as the "site" label
  UNIFI_{N}_API_KEY    — API key generated on that controller
  UNIFI_{N}_SITE       — UniFi site slug (default: "default")
  UNIFI_TIMEOUT        — per-controller HTTP timeout in seconds (default: 15)

Metrics:
  unifi_device_up                  1 if device is connected to its controller
  unifi_device_uptime_seconds      device uptime in seconds
  unifi_device_clients             total clients associated to this device
  unifi_device_cpu_percent         device CPU utilisation (0-100)
  unifi_device_memory_percent      device memory utilisation (0-100)
  unifi_port_up                    1 if switch port link is up
  unifi_port_rx_bytes_total        switch port received bytes (counter)
  unifi_port_tx_bytes_total        switch port transmitted bytes (counter)
  unifi_port_rx_errors_total       switch port receive errors (counter)
  unifi_port_tx_errors_total       switch port transmit errors (counter)
  unifi_port_poe_power_watts       switch port PoE power draw in watts
  unifi_radio_channel              AP radio current channel number
  unifi_radio_channel_utilization  AP radio channel utilization percent (0-100)
  unifi_radio_clients              AP radio connected client count
  unifi_wan_latency_ms             WAN latency in milliseconds
  unifi_wan_uptime_seconds         WAN link uptime in seconds
  unifi_wan_rx_bytes_rate          WAN current receive rate in bytes/s
  unifi_wan_tx_bytes_rate          WAN current transmit rate in bytes/s
  unifi_wan_packet_loss_percent    WAN packet loss percentage
  unifi_site_clients               total connected clients by subsystem (wlan/lan)
  unifi_vpn_tunnels_active         count of active VPN tunnels at this site
  unifi_scrape_success             1 if last fetch from this controller succeeded
"""

import asyncio
import os
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Response

app = FastAPI(title="Crown UniFi Local Exporter", version="0.4.1")

TIMEOUT = float(os.environ.get("UNIFI_TIMEOUT", "15"))

_GATEWAY_TYPES = frozenset({"ugw", "udm", "uxg", "efg", "udmpro", "udmpromax", "udmprose"})


@dataclass
class Controller:
    url: str
    name: str
    api_key: str
    site: str


def _parse_controllers() -> list[Controller]:
    out: list[Controller] = []
    for n in range(1, 5):
        url = os.environ.get(f"UNIFI_{n}_URL", "").strip().rstrip("/")
        if not url:
            continue
        out.append(Controller(
            url=url,
            name=os.environ.get(f"UNIFI_{n}_NAME", f"site{n}").strip(),
            api_key=os.environ.get(f"UNIFI_{n}_API_KEY", "").strip(),
            site=os.environ.get(f"UNIFI_{n}_SITE", "default").strip(),
        ))
    return out


CONTROLLERS = _parse_controllers()


def _num(val, default: float = 0.0) -> float:
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return default


def _lv(val: str) -> str:
    """Escape a string for use as a Prometheus label value."""
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt_block(name: str, help_text: str, type_: str, samples: list[tuple[str, float]]) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {type_}"]
    for labels, value in samples:
        if labels:
            lines.append(f"{name}{{{labels}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return "\n".join(lines)


def _find_gateway_mac(devices: list) -> str | None:
    for dev in devices:
        t = (dev.get("type") or "").lower()
        if t in _GATEWAY_TYPES or "gw" in t or t.startswith("udm"):
            return dev.get("mac")
    for dev in devices:
        if dev.get("wan_ip") or dev.get("uplink", {}).get("type") == "wire":
            return dev.get("mac")
    return None


async def _fetch_controller(client: httpx.AsyncClient, ctrl: Controller) -> dict:
    base = f"{ctrl.url}/proxy/network/api/s/{ctrl.site}"
    v2_base = f"{ctrl.url}/proxy/network/v2/api/site/{ctrl.site}"
    headers = {"X-API-KEY": ctrl.api_key}
    result: dict = {"devices": [], "health": [], "wg_active": 0, "wg_peers_raw": [], "wg_status": "", "ok": False}
    try:
        dr = await client.get(f"{base}/stat/device", headers=headers, timeout=TIMEOUT)
        dr.raise_for_status()
        result["devices"] = dr.json().get("data", [])

        hr = await client.get(f"{base}/stat/health", headers=headers, timeout=TIMEOUT)
        hr.raise_for_status()
        result["health"] = hr.json().get("data", [])

        # WireGuard VPN peers — UniFi Network v2 API; fails gracefully on older firmware
        try:
            wg_r = await client.get(
                f"{v2_base}/vpn-server/peers", headers=headers, timeout=TIMEOUT
            )
            result["wg_status"] = str(wg_r.status_code)
            if wg_r.status_code == 200:
                body = wg_r.json()
                peers = body if isinstance(body, list) else body.get("data", body.get("peers", []))
                result["wg_peers_raw"] = peers
                result["wg_active"] = sum(
                    1 for p in peers
                    if p.get("is_connected") or p.get("connected") or p.get("status") == "active"
                )
        except Exception as exc:
            result["wg_status"] = f"error: {exc}"

        result["ok"] = True
    except Exception:
        pass
    return result


def _mem_percent(dev: dict) -> float | None:
    ss = dev.get("sys_stats", {})
    used = ss.get("mem_used")
    total = ss.get("mem_total")
    if used is not None and total and float(total) > 0:
        return round(float(used) / float(total) * 100, 1)
    alt = dev.get("system-stats", {}).get("mem")
    if alt is not None:
        return _num(alt)
    return None


def _cpu_percent(dev: dict) -> float | None:
    val = dev.get("sys_stats", {}).get("cpu_load")
    if val is None:
        val = dev.get("system-stats", {}).get("cpu")
    if val is not None:
        return _num(val)
    return None


def _process(ctrl: Controller, res: dict, acc: dict) -> None:
    site_lb = f'site="{_lv(ctrl.name)}"'
    acc["scrape"].append((site_lb, 1.0 if res["ok"] else 0.0))

    for dev in res["devices"]:
        dname = _lv(dev.get("name") or dev.get("mac", "unknown"))
        dtype = _lv(dev.get("type", "unknown"))
        dmodel = _lv(dev.get("model", ""))

        dev_lb = f'{site_lb},device="{dname}",type="{dtype}",model="{dmodel}"'

        acc["device_up"].append((dev_lb, 1.0 if dev.get("state") == 1 else 0.0))
        if dev.get("uptime") is not None:
            acc["device_uptime"].append((dev_lb, _num(dev["uptime"])))
        if dev.get("num_sta") is not None:
            acc["device_clients"].append((dev_lb, _num(dev["num_sta"])))

        cpu = _cpu_percent(dev)
        if cpu is not None:
            acc["device_cpu"].append((dev_lb, cpu))

        mem = _mem_percent(dev)
        if mem is not None:
            acc["device_mem"].append((dev_lb, mem))

        for port in dev.get("port_table", []):
            pidx = str(port.get("port_idx", ""))
            pname = _lv(port.get("name", pidx))
            p_lb = f'{site_lb},device="{dname}",port="{pidx}",port_name="{pname}"'
            acc["port_up"].append((p_lb, 1.0 if port.get("up") else 0.0))
            acc["port_rx"].append((p_lb, _num(port.get("rx_bytes", 0))))
            acc["port_tx"].append((p_lb, _num(port.get("tx_bytes", 0))))
            acc["port_rx_err"].append((p_lb, _num(port.get("rx_errors", 0))))
            acc["port_tx_err"].append((p_lb, _num(port.get("tx_errors", 0))))
            if port.get("poe_enable") and port.get("poe_power") is not None:
                acc["port_poe"].append((p_lb, _num(port["poe_power"])))

        for radio in dev.get("radio_table_stats", []):
            rname = _lv(radio.get("name", ""))
            r_lb = f'{site_lb},device="{dname}",radio="{rname}"'
            if radio.get("channel") is not None:
                acc["radio_ch"].append((r_lb, _num(radio["channel"])))
            if radio.get("cu_total") is not None:
                acc["radio_cu"].append((r_lb, _num(radio["cu_total"])))
            if radio.get("num_sta") is not None:
                acc["radio_clients"].append((r_lb, _num(radio["num_sta"])))

    vpn_health: float | None = None
    wan_data: dict = {}
    www_data: dict = {}
    for sub in res["health"]:
        sub_name = sub.get("subsystem", "")
        if sub_name == "wan":
            wan_data = sub
        elif sub_name == "www":
            www_data = sub
        elif sub_name == "vpn":
            vpn_health = (
                _num(sub.get("site_to_site_num_active", 0)) +
                _num(sub.get("remote_user_num_active", 0)) +
                _num(sub.get("num_active", 0))
            )
        if sub_name in ("wlan", "lan") and sub.get("num_user") is not None:
            sub_lb = f'{site_lb},subsystem="{sub_name}"'
            acc["site_clients"].append((sub_lb, _num(sub["num_user"])))

    if wan_data or www_data:
        wan_uptime_stats = wan_data.get("uptime_stats", {}).get("WAN", {})

        for src, key in [(www_data, "latency"), (wan_uptime_stats, "latency_average")]:
            if src.get(key) is not None:
                acc["wan_latency"].append((site_lb, _num(src[key])))
                break

        for src, key in [(www_data, "uptime"), (wan_uptime_stats, "uptime")]:
            if src.get(key) is not None:
                acc["wan_uptime"].append((site_lb, _num(src[key])))
                break

        for key in ("rx_bytes-r", "rx_bytes_r"):
            if wan_data.get(key) is not None:
                acc["wan_rx_rate"].append((site_lb, _num(wan_data[key])))
                break
        for key in ("tx_bytes-r", "tx_bytes_r"):
            if wan_data.get(key) is not None:
                acc["wan_tx_rate"].append((site_lb, _num(wan_data[key])))
                break

        availability = wan_uptime_stats.get("availability")
        if availability is not None:
            acc["wan_loss"].append((site_lb, round(100.0 - float(availability), 2)))
        else:
            for key in ("drops", "packet_loss", "loss"):
                if www_data.get(key) is not None:
                    acc["wan_loss"].append((site_lb, _num(www_data[key])))
                    break

    wg_active = res.get("wg_active", 0)
    if vpn_health is not None or wg_active > 0:
        acc["vpn_active"].append((site_lb, (vpn_health or 0) + wg_active))


@app.get("/metrics")
async def metrics() -> Response:
    acc: dict[str, list[tuple[str, float]]] = {
        "device_up": [], "device_uptime": [], "device_clients": [],
        "device_cpu": [], "device_mem": [],
        "port_up": [], "port_rx": [], "port_tx": [],
        "port_rx_err": [], "port_tx_err": [], "port_poe": [],
        "radio_ch": [], "radio_cu": [], "radio_clients": [],
        "wan_latency": [], "wan_uptime": [], "wan_rx_rate": [], "wan_tx_rate": [],
        "wan_loss": [], "vpn_active": [],
        "site_clients": [], "scrape": [],
    }

    async with httpx.AsyncClient(verify=False) as client:
        results = await asyncio.gather(*[_fetch_controller(client, c) for c in CONTROLLERS])

    for ctrl, res in zip(CONTROLLERS, results):
        _process(ctrl, res, acc)

    blocks = [
        fmt_block("unifi_device_up", "1 if device is connected to its controller.", "gauge", acc["device_up"]),
        fmt_block("unifi_device_uptime_seconds", "Device uptime in seconds.", "gauge", acc["device_uptime"]),
        fmt_block("unifi_device_clients", "Total clients associated to this device.", "gauge", acc["device_clients"]),
        fmt_block("unifi_device_cpu_percent", "Device CPU utilisation (0-100).", "gauge", acc["device_cpu"]),
        fmt_block("unifi_device_memory_percent", "Device memory utilisation (0-100).", "gauge", acc["device_mem"]),
        fmt_block("unifi_port_up", "1 if switch port link is up.", "gauge", acc["port_up"]),
        fmt_block("unifi_port_rx_bytes_total", "Switch port cumulative received bytes.", "counter", acc["port_rx"]),
        fmt_block("unifi_port_tx_bytes_total", "Switch port cumulative transmitted bytes.", "counter", acc["port_tx"]),
        fmt_block("unifi_port_rx_errors_total", "Switch port cumulative receive errors.", "counter", acc["port_rx_err"]),
        fmt_block("unifi_port_tx_errors_total", "Switch port cumulative transmit errors.", "counter", acc["port_tx_err"]),
        fmt_block("unifi_port_poe_power_watts", "Switch port PoE power draw in watts.", "gauge", acc["port_poe"]),
        fmt_block("unifi_radio_channel", "AP radio current channel number.", "gauge", acc["radio_ch"]),
        fmt_block("unifi_radio_channel_utilization", "AP radio channel utilization percent (0-100).", "gauge", acc["radio_cu"]),
        fmt_block("unifi_radio_clients", "AP radio connected client count.", "gauge", acc["radio_clients"]),
        fmt_block("unifi_wan_latency_ms", "WAN latency in milliseconds.", "gauge", acc["wan_latency"]),
        fmt_block("unifi_wan_uptime_seconds", "WAN link uptime in seconds.", "gauge", acc["wan_uptime"]),
        fmt_block("unifi_wan_rx_bytes_rate", "WAN current receive rate in bytes/s.", "gauge", acc["wan_rx_rate"]),
        fmt_block("unifi_wan_tx_bytes_rate", "WAN current transmit rate in bytes/s.", "gauge", acc["wan_tx_rate"]),
        fmt_block("unifi_wan_packet_loss_percent", "WAN packet loss percentage reported by gateway.", "gauge", acc["wan_loss"]),
        fmt_block("unifi_vpn_tunnels_active", "Count of established VPN tunnels at this site.", "gauge", acc["vpn_active"]),
        fmt_block("unifi_site_clients", "Total connected clients per subsystem.", "gauge", acc["site_clients"]),
        fmt_block("unifi_scrape_success", "1 if last fetch from this controller succeeded.", "gauge", acc["scrape"]),
    ]
    return Response(content="\n".join(blocks) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "controllers": [c.name for c in CONTROLLERS]}


@app.get("/debug")
async def debug() -> dict:
    """Return raw health and WireGuard peer data from each controller for field-name diagnosis."""
    out = {}
    async with httpx.AsyncClient(verify=False) as client:
        for ctrl in CONTROLLERS:
            res = await _fetch_controller(client, ctrl)
            vpn_health_sub = next((s for s in res["health"] if s.get("subsystem") == "vpn"), None)
            out[ctrl.name] = {
                "ok": res["ok"],
                "vpn_health_subsystem": vpn_health_sub,
                "wg_status": res.get("wg_status", ""),
                "wg_active_computed": res.get("wg_active", 0),
                "wg_peers_raw": res.get("wg_peers_raw", []),
                "gateway_mac": _find_gateway_mac(res.get("devices", [])),
            }
    return out
