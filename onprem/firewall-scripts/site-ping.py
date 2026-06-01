#!/usr/bin/env python3
"""
Crown site ping monitor — /data/site-ping.py
Fetches ping targets from the exemption-api and pushes results to Pushgateway.
Runs every minute via cron. Reinstalled automatically on boot by on_boot.d/99-site-ping.sh.

CONFIGURE per firewall:
  SOURCE        — must match the UNIFI_N_NAME value in monitoring/.env
  POC_CONTAINERS — hostname or IP of poc-containers, reachable from this site
"""

import json
import re
import subprocess
import urllib.parse
import urllib.request

SOURCE        = "BH-FIREWALL"       # ← change per firewall (UNIFI_N_NAME value)
POC_CONTAINERS = "192.168.164.184"  # ← poc-containers static IP

API_URL     = f"http://{POC_CONTAINERS}:9511/ping/targets/script?source={SOURCE}"
PUSHGATEWAY = f"http://{POC_CONTAINERS}:9501"


def fetch_targets():
    try:
        with urllib.request.urlopen(API_URL, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"fetch failed: {e}")
        return []


def ping_target(ip, name):
    try:
        out = subprocess.run(
            ["ping", "-c", "5", "-W", "2", ip],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        out = ""

    loss_m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*packet\s+loss", out)
    rtt_m  = re.search(r"min/avg/max[^\d]*([\d.]+)/([\d.]+)/", out)

    loss    = float(loss_m.group(1)) if loss_m else 100.0
    rtt     = float(rtt_m.group(2))  if rtt_m  else 0.0
    success = 1.0 if loss < 100 else 0.0

    lb   = f'source="{SOURCE}",target="{name}",dst="{ip}"'
    body = (
        f"# TYPE site_ping_success gauge\nsite_ping_success{{{lb}}} {success}\n"
        f"# TYPE site_ping_rtt_ms gauge\nsite_ping_rtt_ms{{{lb}}} {rtt}\n"
        f"# TYPE site_ping_loss_percent gauge\nsite_ping_loss_percent{{{lb}}} {loss}\n"
    ).encode()

    url = f"{PUSHGATEWAY}/metrics/job/site_ping/source/{urllib.parse.quote(SOURCE, safe='')}/target/{urllib.parse.quote(name, safe='')}"
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, data=body, method="POST"), timeout=5
        )
    except Exception as e:
        print(f"push failed for {name}: {e}")


for t in fetch_targets():
    ping_target(t["ip"], t["name"])
