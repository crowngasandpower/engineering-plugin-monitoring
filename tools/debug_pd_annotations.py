"""Check PD incident body structure for dashboard annotation data."""
import sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "tray-monitor"))
import settings; settings.load()

import requests

cfg     = settings.get()
api_key = cfg.get("pd_api_key", "").strip()

r = requests.get(
    "https://api.pagerduty.com/incidents",
    headers={
        "Authorization": f"Token token={api_key}",
        "Accept": "application/vnd.pagerduty+json;version=2",
    },
    params={"statuses[]": ["triggered", "acknowledged", "resolved"], "limit": 5},
    timeout=10,
)
incidents = r.json().get("incidents", [])

# Find the first Grafana-sourced incident (has [FIRING or [CRITICAL/WARNING in title)
target = None
for inc in incidents:
    t = inc.get("title", "")
    if "[FIRING" in t or "[CRITICAL" in t or "[WARNING" in t:
        target = inc
        break

if not target:
    print("No Grafana incident found in recent 5. Titles seen:")
    for inc in incidents:
        print(" -", inc.get("title"))
    sys.exit(0)

print(f"Incident: {target.get('title')}")
print(f"Status:   {target.get('status')}")
print()

details = (target.get("body") or {}).get("details") or {}
print("=== Top-level details keys ===")
print(list(details.keys()))

cef = details.get("__pd_cef_payload") or {}
print("\n=== __pd_cef_payload keys ===")
print(list(cef.keys()))

cef_det = cef.get("details") or {}
print("\n=== __pd_cef_payload.details keys ===")
print(list(cef_det.keys()))

firing = cef_det.get("firing", "")
if firing:
    print("\n=== firing text (first 800 chars) ===")
    print(firing[:800])
else:
    print("\n(no firing text found at __pd_cef_payload.details.firing)")
    print("\n=== Full cef_det ===")
    print(json.dumps(cef_det, indent=2)[:1000])
