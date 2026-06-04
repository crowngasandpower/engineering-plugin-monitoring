#!/usr/bin/env python3
"""
provision-alerts.py — API-based alert rule provisioner for Grafana.

Two modes
---------
--migrate     Deletes file-provenance alert rule records from the local SQLite
              database before Grafana starts.  Run synchronously in the
              container entrypoint so the file provisioner cannot re-stamp
              rules as read-only before we take ownership via the API.

--provision   Waits for Grafana to be healthy, then creates/updates every rule
              group in /opt/alert-rules.yml via the Grafana ruler API.  Rules
              are created without provenance, making them fully editable in the
              Grafana UI.

Why API provisioning instead of file provisioning?
---------------------------------------------------
Grafana 13 introduced a hard lock: rules provisioned from a YAML file are
stamped provenance='file' and cannot be modified via the UI or any API —
including the provisioning API itself.  Provisioning via the ruler API instead
leaves rules with no provenance lock, keeping them editable in the UI.

Live-edit / persistence contract
---------------------------------
* UI edits take effect immediately and are stored in the Grafana database.
* On container restart this script re-runs and re-applies alert-rules.yml,
  overwriting any UI-only changes.
* To make a change permanent: edit alert-rules.yml and raise a PR.
  The next deploy will bake the change into the image and it will survive
  all future restarts.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:
    print("[provision-alerts] ERROR: PyYAML not available.", flush=True)
    sys.exit(1)

GRAFANA_URL = "http://localhost:3000"
RULES_FILE  = "/opt/alert-rules.yml"
DB_PATH     = "/var/lib/grafana/grafana.db"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_header():
    user = os.environ.get("GF_SECURITY_ADMIN_USER", "admin")
    pwd  = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "admin")
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


def _request(method, path, data=None):
    url  = GRAFANA_URL + path
    body = json.dumps(data).encode() if data is not None else None
    req  = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": _auth_header(),
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except Exception as e:
        return 0, {"error": str(e)}


def _wait_for_grafana(retries=90, delay=3):
    print("[provision-alerts] Waiting for Grafana to be ready...", flush=True)
    for _ in range(retries):
        try:
            req = urllib.request.Request(f"{GRAFANA_URL}/api/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if json.loads(r.read()).get("database") == "ok":
                    print("[provision-alerts] Grafana is ready.", flush=True)
                    return
        except Exception:
            pass
        time.sleep(delay)
    print("[provision-alerts] ERROR: Grafana did not become ready in time.", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _folder_map():
    _, folders = _request("GET", "/api/folders?limit=200")
    return {f["title"]: f["uid"] for f in (folders if isinstance(folders, list) else [])}


def _convert_rule(rule):
    """
    Convert a YAML rule to the Grafana ruler API format.
    The ruler API wraps Grafana-specific fields inside a 'grafana_alert' object,
    unlike the provisioning API which uses a flat structure.
    """
    grafana_alert = {
        "uid":            rule["uid"],
        "title":          rule["title"],
        "condition":      rule["condition"],
        "data":           rule.get("data", []),
        "no_data_state":  rule.get("noDataState", "NoData"),
        "exec_err_state": rule.get("execErrState", "Error"),
        "is_paused":      bool(rule.get("isPaused", False)),
    }
    if rule.get("keep_firing_for"):
        grafana_alert["keep_firing_for"] = rule["keep_firing_for"]

    return {
        "expr":          "",
        "for":           rule.get("for", "0s"),
        "labels":        rule.get("labels") or {},
        "annotations":   rule.get("annotations") or {},
        "grafana_alert": grafana_alert,
    }


# ---------------------------------------------------------------------------
# Provision mode
# ---------------------------------------------------------------------------

def provision():
    _wait_for_grafana()

    if not os.path.exists(RULES_FILE):
        print(f"[provision-alerts] {RULES_FILE} not found, skipping.", flush=True)
        return

    with open(RULES_FILE) as f:
        config = yaml.safe_load(f)

    groups = config.get("groups", [])
    if not groups:
        print("[provision-alerts] No groups in rules file, skipping.", flush=True)
        return

    folders     = _folder_map()
    total_rules = sum(len(g.get("rules", [])) for g in groups)
    print(f"[provision-alerts] Provisioning {total_rules} rules across {len(groups)} groups...", flush=True)

    for group in groups:
        name   = group["name"]
        folder = group["folder"]
        uid    = folders.get(folder)

        if not uid:
            print(f"[provision-alerts]   SKIP  '{name}': folder '{folder}' not found.", flush=True)
            continue

        rules   = [_convert_rule(r) for r in group.get("rules", [])]
        payload = {
            "name":     name,
            "interval": group.get("interval", "1m"),
            "rules":    rules,
        }
        status, resp = _request("POST", f"/api/ruler/grafana/api/v1/rules/{uid}", payload)

        if status in (200, 201, 202):
            print(f"[provision-alerts]   OK    '{name}' ({len(rules)} rules)", flush=True)
        else:
            print(f"[provision-alerts]   ERROR '{name}': HTTP {status} — {resp}", flush=True)

    print("[provision-alerts] Provisioning complete.", flush=True)


# ---------------------------------------------------------------------------
# Migrate mode
# ---------------------------------------------------------------------------

def migrate():
    """
    Remove file-provenance alert rule records from the local SQLite database
    so the ruler API can take ownership without hitting the
    'cannot change provenance from file to api' error in Grafana 13.

    For non-SQLite databases (AWS RDS) this step must be done manually — see
    the PR description for the one-time SQL command.
    """
    db_type = os.environ.get("GF_DATABASE_TYPE", "sqlite3")

    if db_type != "sqlite3":
        print(f"[provision-alerts] DB type is '{db_type}' — skipping automatic migration.", flush=True)
        print("[provision-alerts] Run the following against your database once before deploying:", flush=True)
        print("  DELETE FROM provenance_type WHERE record_type = 'alertRule';", flush=True)
        return

    if not os.path.exists(DB_PATH):
        print("[provision-alerts] No SQLite DB found — first run, no migration needed.", flush=True)
        return

    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH)
        c    = conn.cursor()
        c.execute("DELETE FROM provenance_type WHERE record_type = 'alertRule'")
        n = c.rowcount
        conn.commit()
        conn.close()
        if n:
            print(f"[provision-alerts] Migration: removed {n} file-provenance record(s).", flush=True)
        else:
            print("[provision-alerts] Migration: nothing to remove.", flush=True)
    except Exception as e:
        print(f"[provision-alerts] Migration warning: {e}", flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--provision"
    if mode == "--migrate":
        migrate()
    elif mode == "--provision":
        provision()
    else:
        print(f"[provision-alerts] Unknown mode: {mode}", flush=True)
        sys.exit(1)
