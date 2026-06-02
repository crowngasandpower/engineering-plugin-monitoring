#!/usr/bin/env python3
"""
Crown site ping monitor — /data/site-ping.py
Fetches config and ping targets from the exemption-api and pushes results to Pushgateway.
Runs every minute via cron. Reinstalled automatically on boot by on_boot.d/99-site-ping.sh.

Per-device setup:
  1. Set the hostname on the firewall — this becomes the site ID and metrics label.
  2. Set POC_CONTAINERS to the IP of poc-containers if it differs from the default.
  3. Optionally set FETCH_SOURCE_IP if the default route can't reach poc-containers.

The script auto-registers itself in the API on first run. No manual API config needed.
"""

import http.client
import json
import re
import socket
import subprocess
import urllib.parse

POC_CONTAINERS  = "192.168.164.184"  # ← poc-containers static IP
FETCH_SOURCE_IP = ""                 # ← local IP of the interface that can reach poc-containers (blank = OS default)

SITE_ID     = socket.gethostname()
API_URL     = f"http://{POC_CONTAINERS}:9511/ping/targets/script?site_id={urllib.parse.quote(SITE_ID)}"
PUSHGATEWAY = f"http://{POC_CONTAINERS}:9501"


def _http_get(url):
    parsed = urllib.parse.urlparse(url)
    source = (FETCH_SOURCE_IP, 0) if FETCH_SOURCE_IP else None
    conn = http.client.HTTPConnection(
        parsed.hostname, parsed.port or 80,
        timeout=5,
        source_address=source,
    )
    conn.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""))
    r = conn.getresponse()
    body = r.read()
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}: {body.decode(errors='replace')}")
    return body


def _http_post(url, data):
    parsed = urllib.parse.urlparse(url)
    source = (FETCH_SOURCE_IP, 0) if FETCH_SOURCE_IP else None
    conn = http.client.HTTPConnection(
        parsed.hostname, parsed.port or 80,
        timeout=5,
        source_address=source,
    )
    conn.request("POST", parsed.path, body=data)
    r = conn.getresponse()
    if r.status not in (200, 202):
        raise RuntimeError(f"HTTP {r.status}: {r.read().decode(errors='replace')}")


def fetch_config():
    try:
        return json.loads(_http_get(API_URL))
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        print(f"fetch failed: {e}")
        return None
    except Exception as e:
        print(f"fetch failed: {e}")
        return None


def register():
    reg_url = (
        f"http://{POC_CONTAINERS}:9511/ping/sources/add"
        f"?site_id={urllib.parse.quote(SITE_ID)}"
        f"&source={urllib.parse.quote(SITE_ID)}"
    )
    try:
        _http_get(reg_url)
        print(f"registered as {SITE_ID!r} — set a source IP at /ping/sources if needed")
        return True
    except Exception as e:
        print(f"auto-register failed: {e}")
        return False


def ping_target(ip, name, source, source_ip=None):
    cmd = ["ping", "-c", "5", "-W", "2"]
    if source_ip:
        cmd += ["-I", source_ip]
    cmd.append(ip)
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        out = ""

    loss_m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*packet\s+loss", out)
    rtt_m  = re.search(r"min/avg/max[^\d]*([\d.]+)/([\d.]+)/", out)

    loss    = float(loss_m.group(1)) if loss_m else 100.0
    rtt     = float(rtt_m.group(2))  if rtt_m  else 0.0
    success = 1.0 if loss < 100 else 0.0

    lb   = f'source="{source}",target="{name}",dst="{ip}"'
    body = (
        f"# TYPE site_ping_success gauge\nsite_ping_success{{{lb}}} {success}\n"
        f"# TYPE site_ping_rtt_ms gauge\nsite_ping_rtt_ms{{{lb}}} {rtt}\n"
        f"# TYPE site_ping_loss_percent gauge\nsite_ping_loss_percent{{{lb}}} {loss}\n"
    ).encode()

    push_url = f"{PUSHGATEWAY}/metrics/job/site_ping/source/{urllib.parse.quote(source, safe='')}/target/{urllib.parse.quote(name, safe='')}"
    try:
        _http_post(push_url, body)
    except Exception as e:
        print(f"push failed for {name}: {e}")


config = fetch_config()

if config is None:
    if not register():
        raise SystemExit("could not register with API")
    config = fetch_config()

if not config:
    raise SystemExit("could not fetch config from API")

source    = config["source"]
source_ip = config.get("source_ip")

for t in config["targets"]:
    ping_target(t["ip"], t["name"], source, t.get("source_ip") or source_ip)
