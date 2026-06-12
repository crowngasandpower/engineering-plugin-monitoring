"""UniFi Site Manager exporter for Crown monitoring.

Polls the UniFi Site Manager API (api.ui.com) with a single API key to collect
metrics across all managed consoles. Exposes as Prometheus gauges on /metrics.

The Site Manager API provides fleet-level health only — per-device metrics
(clients, traffic, port stats) are collected by the unifi-local-exporter.
Primary use: fleet overview and SDWAN failure detection. If a site shows
connected here but unifi_local metrics go dark, the SDWAN link is down.

Environment:
  UNIFI_API_KEY     — API key from ui.com account → API Keys → Generate API Key
  UNIFI_TIMEOUT     — HTTP timeout in seconds (default: 15)

Metrics (labelled host="<name>", model="<hardware model>"):
  unifi_host_up                  1 if host state == "connected" in Site Manager
  unifi_host_info                Info metric: host up with firmware version label
  unifi_host_pending_updates     count of installed controllers with a pending update
  unifi_controller_running       1 if a controller application is running (label: app)
  unifi_controller_update_available  1 if a controller application has an update (label: app)
  unifi_scrape_success           1 if the last api.ui.com call succeeded
"""

import os

import httpx
from fastapi import FastAPI, Response

app = FastAPI(title="Crown UniFi Exporter", version="0.3.0")

API_BASE = "https://api.ui.com"
API_KEY = os.environ.get("UNIFI_API_KEY", "")
TIMEOUT = float(os.environ.get("UNIFI_TIMEOUT", "15"))


def _lv(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def fmt_block(name: str, help_text: str, type_: str, samples: list[tuple[str, float]]) -> str:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {type_}"]
    for labels, value in samples:
        if labels:
            lines.append(f"{name}{{{labels}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return "\n".join(lines)


def _host_labels(host: dict) -> str:
    state = host.get("reportedState", {})
    name = state.get("name") or state.get("hostname") or host.get("id", "unknown")
    model = state.get("hardware", {}).get("name", "")
    return f'host="{_lv(name)}",model="{_lv(model)}"'


def _pending_updates(state: dict) -> int:
    """Count installed controllers that have a firmware/software update available."""
    return sum(
        1 for c in state.get("controllers", [])
        if c.get("isInstalled") and c.get("updateAvailable")
    )


@app.get("/metrics")
async def metrics() -> Response:
    up_s: list[tuple[str, float]] = []
    info_s: list[tuple[str, float]] = []
    update_s: list[tuple[str, float]] = []
    ctrl_running_s: list[tuple[str, float]] = []
    ctrl_update_s: list[tuple[str, float]] = []
    scrape_ok = 1.0

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{API_BASE}/v1/hosts",
                headers={"X-API-KEY": API_KEY},
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            hosts = resp.json().get("data", [])

        for host in hosts:
            lb = _host_labels(host)
            state = host.get("reportedState", {})
            is_up = not host.get("isBlocked", False) and state.get("state") == "connected"
            up_s.append((lb, 1.0 if is_up else 0.0))
            update_s.append((lb, float(_pending_updates(state))))

            # Info metric: carries firmware version as a label so dashboards can
            # display it without a separate lookup.
            fw = _lv(state.get("hardware", {}).get("firmwareVersion", ""))
            info_s.append((f"{lb},firmware=\"{fw}\"", 1.0))

            # Per-controller-application metrics (network, protect, access, talk…)
            for ctrl in state.get("controllers", []):
                if not ctrl.get("isInstalled"):
                    continue
                app_name = _lv(ctrl.get("name", "unknown"))
                c_lb = f'{lb},app="{app_name}"'
                ctrl_running_s.append((c_lb, 1.0 if ctrl.get("isRunning") else 0.0))
                ctrl_update_s.append((c_lb, 1.0 if ctrl.get("updateAvailable") else 0.0))

    except Exception:
        scrape_ok = 0.0

    blocks = [
        fmt_block("unifi_host_up", "1 if host state is connected in UniFi Site Manager.", "gauge", up_s),
        fmt_block("unifi_host_info", "Host info metric — value always 1, firmware version carried as label.", "gauge", info_s),
        fmt_block("unifi_host_pending_updates", "Installed controllers with a pending firmware or software update.", "gauge", update_s),
        fmt_block("unifi_controller_running", "1 if the controller application is running on the host.", "gauge", ctrl_running_s),
        fmt_block("unifi_controller_update_available", "1 if an update is available for this controller application.", "gauge", ctrl_update_s),
        fmt_block("unifi_scrape_success", "1 if the last api.ui.com fetch succeeded.", "gauge", [("", scrape_ok)]),
    ]
    return Response(content="\n".join(blocks) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
