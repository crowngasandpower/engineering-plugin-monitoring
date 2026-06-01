"""Dell OS10 switch exporter for Crown monitoring.

Polls Dell OS10 switches via the RESTCONF API (HTTPS/JSON) and exposes
interface statistics and system info as Prometheus metrics on /metrics.

All configured switches are polled in parallel on each scrape. A switch that
is unreachable simply contributes os10_scrape_success=0 and no other samples —
the remaining switches continue normally.

Environment:
  OS10_USER               — switch username (default: readonly)
  OS10_PASSWORD           — switch password
  OS10_1_ADDRESS          — switch 1 IP/hostname (required)
  OS10_1_NAME             — switch 1 friendly name (default: sw1)
  OS10_2_ADDRESS          — switch 2 IP/hostname (optional, leave blank to skip)
  OS10_2_NAME             — switch 2 friendly name
  OS10_3_ADDRESS          — switch 3 IP/hostname (optional)
  OS10_3_NAME             — switch 3 friendly name
  OS10_4_ADDRESS          — switch 4 IP/hostname (optional)
  OS10_4_NAME             — switch 4 friendly name
  OS10_TIMEOUT            — per-switch HTTP timeout in seconds (default: 10)

Metrics:
  os10_interface_up                 1 if oper-status == "up", 0 otherwise
  os10_interface_in_bytes_total     ingress octets (counter)
  os10_interface_out_bytes_total    egress octets (counter)
  os10_interface_in_errors_total    ingress error frames (counter)
  os10_interface_out_errors_total   egress error frames (counter)
  os10_interface_in_discards_total  ingress discarded frames (counter)
  os10_interface_out_discards_total egress discarded frames (counter)
  os10_system_uptime_seconds        system uptime in seconds (gauge)
  os10_scrape_success               1 if last RESTCONF fetch succeeded (gauge)
"""

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Response

app = FastAPI(title="Crown Dell OS10 Exporter", version="0.1.0")

USER = os.environ.get("OS10_USER", "readonly")
PASSWORD = os.environ.get("OS10_PASSWORD", "")
TIMEOUT = float(os.environ.get("OS10_TIMEOUT", "10"))

RESTCONF_HEADERS = {"Accept": "application/yang-data+json"}


@dataclass
class Switch:
    address: str
    name: str


def _parse_switches() -> list[Switch]:
    out: list[Switch] = []
    for n in range(1, 5):
        addr = os.environ.get(f"OS10_{n}_ADDRESS", "").strip()
        if not addr:
            continue
        name = os.environ.get(f"OS10_{n}_NAME", f"sw{n}").strip()
        out.append(Switch(address=addr, name=name))
    return out


SWITCHES = _parse_switches()


def _int(val) -> int:
    try:
        return int(str(val))
    except (ValueError, TypeError):
        return 0


def _parse_sonic_uptime(s: str) -> float | None:
    """Parse SONiC/Dell uptime strings: '5 days, 3:45:00.000000' or '3:45:00'."""
    try:
        m = re.match(r"(?:(\d+)\s+days?,\s*)?(\d+):(\d+):(\d+)", s)
        if not m:
            return None
        days = int(m.group(1) or 0)
        return float(days * 86400 + int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4)))
    except Exception:
        return None


def _parse_uptime(system_state: dict) -> float | None:
    # Dell OS10 may namespace the key as "ietf-system:clock" or plain "clock"
    clock = system_state.get("ietf-system:clock") or system_state.get("clock", {})
    boot_str = clock.get("boot-datetime", "")
    current_str = clock.get("current-datetime", "")
    if not boot_str or not current_str:
        return None
    try:
        boot_dt = datetime.fromisoformat(boot_str.replace("Z", "+00:00"))
        current_dt = datetime.fromisoformat(current_str.replace("Z", "+00:00"))
        # Normalise to UTC if one side is naive and the other is aware
        if boot_dt.tzinfo is None and current_dt.tzinfo is not None:
            boot_dt = boot_dt.replace(tzinfo=timezone.utc)
        elif current_dt.tzinfo is None and boot_dt.tzinfo is not None:
            current_dt = current_dt.replace(tzinfo=timezone.utc)
        return max(0.0, (current_dt - boot_dt).total_seconds())
    except Exception:
        return None


async def _fetch_uptime(client: httpx.AsyncClient, base: str, auth: tuple) -> float | None:
    # Attempt 1: Dell OS10 — dell-system:system-state/system-status/uptime (seconds)
    try:
        resp = await client.get(
            f"{base}/dell-system:system-state",
            headers=RESTCONF_HEADERS, auth=auth, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        status = data.get("dell-system:system-state", data).get("system-status", {})
        uptime = status.get("uptime")
        if uptime is not None:
            return float(uptime)
    except Exception:
        pass

    # Attempt 2: IETF system state (clock.boot-datetime / current-datetime)
    try:
        resp = await client.get(
            f"{base}/ietf-system:system-state",
            headers=RESTCONF_HEADERS, auth=auth, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        sys_state = resp.json().get("ietf-system:system-state", resp.json())
        uptime = _parse_uptime(sys_state)
        if uptime is not None:
            return uptime
    except Exception:
        pass

    # Attempt 3: OpenConfig system state (boot-time = nanoseconds since Unix epoch)
    try:
        resp = await client.get(
            f"{base}/openconfig-system:system/state",
            headers=RESTCONF_HEADERS, auth=auth, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        state = data.get("openconfig-system:state", data.get("state", data))
        boot_ns = state.get("boot-time")
        if boot_ns is not None:
            now_s = datetime.now(timezone.utc).timestamp()
            return max(0.0, now_s - float(boot_ns) / 1e9)
    except Exception:
        pass

    return None


async def _fetch_switch(client: httpx.AsyncClient, sw: Switch) -> dict:
    base = f"https://{sw.address}/restconf/data"
    auth = (USER, PASSWORD)
    result: dict = {"interfaces": [], "uptime": None, "ok": False}
    try:
        iface_resp = await client.get(
            f"{base}/ietf-interfaces:interfaces-state",
            headers=RESTCONF_HEADERS,
            auth=auth,
            timeout=TIMEOUT,
        )
        iface_resp.raise_for_status()
        iface_data = iface_resp.json()
        # RESTCONF may wrap the root key or not, depending on firmware version
        iface_root = iface_data.get("ietf-interfaces:interfaces-state", iface_data)
        result["interfaces"] = iface_root.get("interface", [])
        result["ok"] = True
    except Exception:
        return result

    result["uptime"] = await _fetch_uptime(client, base, auth)
    return result


def fmt_block(name: str, help_text: str, type_: str, samples: list[tuple[str, float]]) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {type_}"]
    for labels, value in samples:
        if labels:
            lines.append(f"{name}{{{labels}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return "\n".join(lines)


@app.get("/metrics")
async def metrics() -> Response:
    up_s: list[tuple[str, float]] = []
    in_bytes_s: list[tuple[str, float]] = []
    out_bytes_s: list[tuple[str, float]] = []
    in_err_s: list[tuple[str, float]] = []
    out_err_s: list[tuple[str, float]] = []
    in_disc_s: list[tuple[str, float]] = []
    out_disc_s: list[tuple[str, float]] = []
    uptime_s: list[tuple[str, float]] = []
    scrape_s: list[tuple[str, float]] = []

    # OS10 switches use self-signed certificates — disable verification
    async with httpx.AsyncClient(verify=False) as client:
        results = await asyncio.gather(*[_fetch_switch(client, sw) for sw in SWITCHES])

    for sw, res in zip(SWITCHES, results):
        sw_lb = f'switch="{sw.name}"'
        scrape_s.append((sw_lb, 1.0 if res["ok"] else 0.0))
        if res["uptime"] is not None:
            uptime_s.append((sw_lb, res["uptime"]))
        for iface in res["interfaces"]:
            iname = iface.get("name", "")
            lb = f'switch="{sw.name}",interface="{iname}"'
            oper = iface.get("oper-status", "down")
            up_s.append((lb, 1.0 if oper == "up" else 0.0))
            stats = iface.get("statistics", {})
            in_bytes_s.append((lb, float(_int(stats.get("in-octets", 0)))))
            out_bytes_s.append((lb, float(_int(stats.get("out-octets", 0)))))
            in_err_s.append((lb, float(_int(stats.get("in-errors", 0)))))
            out_err_s.append((lb, float(_int(stats.get("out-errors", 0)))))
            in_disc_s.append((lb, float(_int(stats.get("in-discards", 0)))))
            out_disc_s.append((lb, float(_int(stats.get("out-discards", 0)))))

    blocks = [
        fmt_block("os10_interface_up", "1 if interface oper-status is up.", "gauge", up_s),
        fmt_block("os10_interface_in_bytes_total", "Total ingress octets.", "counter", in_bytes_s),
        fmt_block("os10_interface_out_bytes_total", "Total egress octets.", "counter", out_bytes_s),
        fmt_block("os10_interface_in_errors_total", "Total ingress error frames.", "counter", in_err_s),
        fmt_block("os10_interface_out_errors_total", "Total egress error frames.", "counter", out_err_s),
        fmt_block("os10_interface_in_discards_total", "Total ingress discarded frames.", "counter", in_disc_s),
        fmt_block("os10_interface_out_discards_total", "Total egress discarded frames.", "counter", out_disc_s),
        fmt_block("os10_system_uptime_seconds", "Switch system uptime in seconds.", "gauge", uptime_s),
        fmt_block("os10_scrape_success", "1 if the last RESTCONF fetch from this switch succeeded.", "gauge", scrape_s),
    ]
    return Response(content="\n".join(blocks) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "switches": [sw.name for sw in SWITCHES]}


_UPTIME_DATA_PATHS = [
    "ietf-system:system-state",
    "ietf-system:system-state/clock",
    "openconfig-system:system/state",
    "openconfig-system:system/state/boot-time",
    "dell-system:system",
    "dell-system:system-state",
    "sonic-system-uptime:system-uptime",
    "ietf-yang-library:modules-state",
]

_RESTCONF_TOP_PATHS = [
    "yang-library-version",
]


async def _probe_switch(client: httpx.AsyncClient, sw: Switch) -> dict:
    restconf = f"https://{sw.address}/restconf"
    auth = (USER, PASSWORD)
    out: dict = {}
    for path in _RESTCONF_TOP_PATHS:
        try:
            resp = await client.get(
                f"{restconf}/{path}",
                headers=RESTCONF_HEADERS, auth=auth, timeout=TIMEOUT,
            )
            out[path] = {"status": resp.status_code, "body": resp.text[:500]}
        except Exception as exc:
            out[path] = {"error": str(exc)}
    for path in _UPTIME_DATA_PATHS:
        try:
            resp = await client.get(
                f"{restconf}/data/{path}",
                headers=RESTCONF_HEADERS, auth=auth, timeout=TIMEOUT,
            )
            out[path] = {"status": resp.status_code, "body": resp.text[:1000]}
        except Exception as exc:
            out[path] = {"error": str(exc)}
    return out


@app.get("/debug/uptime")
async def debug_uptime() -> dict:
    """Probe each switch for uptime-related RESTCONF paths.
    Call once to diagnose why os10_system_uptime_seconds returns no data."""
    async with httpx.AsyncClient(verify=False) as client:
        results = await asyncio.gather(*[_probe_switch(client, sw) for sw in SWITCHES])
    return {sw.name: r for sw, r in zip(SWITCHES, results)}
