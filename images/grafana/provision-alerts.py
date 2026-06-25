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
              from /opt/alert-rules/*.yml (one file per group; legacy single
              /opt/alert-rules.yml still supported as a fallback) via the Grafana
              provisioning API with the X-Disable-Provenance header.  Rules are
              created without a provenance lock, keeping them editable in the UI.

Why API provisioning instead of file provisioning?
---------------------------------------------------
Grafana 13 introduced a hard lock: rules provisioned from a YAML file are
stamped provenance='file' and cannot be modified via the UI or any API —
including the provisioning API itself.  Provisioning via the HTTP provisioning
API with X-Disable-Provenance: true instead leaves rules with no provenance
lock, keeping them editable in the UI.

Why not the ruler API?
----------------------
Grafana 13.0.1 rejects creation of brand-new rules via the ruler API with
"field keep_firing_for cannot be negative", regardless of the value supplied.
Updates to existing rules work fine.  The provisioning API with
X-Disable-Provenance does not have this restriction.

Live-edit / persistence contract
---------------------------------
* UI edits take effect immediately and are stored in the Grafana database.
* On container restart this script re-runs and re-applies the alert-rules/ group
  files, overwriting any UI-only changes.
* To make a change permanent: edit the relevant alert-rules/<group>.yml and raise
  a PR.
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
RULES_DIR   = "/opt/alert-rules"        # one YAML file per group (preferred)
RULES_FILE  = "/opt/alert-rules.yml"    # legacy single-file fallback
DB_PATH     = "/var/lib/grafana/grafana.db"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _auth_header():
    user = os.environ.get("GF_SECURITY_ADMIN_USER", "admin")
    pwd  = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "admin")
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


def _request(method, path, data=None, extra_headers=None):
    url  = GRAFANA_URL + path
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"Authorization": _auth_header(), "Content-Type": "application/json"}
    if extra_headers:
        hdrs.update(extra_headers)
    req  = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {}
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


def _ensure_folder(folders, title):
    """Return the UID for a folder, creating it if it doesn't exist."""
    if title in folders:
        return folders[title]
    print(f"[provision-alerts]   Creating folder '{title}'...", flush=True)
    status, resp = _request("POST", "/api/folders", {"title": title})
    if status in (200, 201):
        uid = resp["uid"]
        folders[title] = uid
        return uid
    print(f"[provision-alerts]   ERROR creating folder '{title}': HTTP {status} — {resp}", flush=True)
    return None


def _existing_rule_uids():
    """Return the set of rule UIDs that already exist in Grafana."""
    _, rules = _request("GET", "/api/v1/provisioning/alert-rules")
    if isinstance(rules, list):
        return {r["uid"] for r in rules}
    return set()


def _convert_rule(rule, folder_uid, group_name):
    """Convert a YAML rule to the Grafana provisioning API format (flat structure)."""
    return {
        "uid":          rule["uid"],
        "title":        rule["title"],
        "ruleGroup":    group_name,
        "folderUID":    folder_uid,
        "condition":    rule["condition"],
        "data":         rule.get("data", []),
        "noDataState":  rule.get("noDataState", "NoData"),
        "execErrState": rule.get("execErrState", "Error"),
        "isPaused":     bool(rule.get("isPaused", False)),
        "for":          rule.get("for", "0s"),
        "labels":       rule.get("labels") or {},
        "annotations":  rule.get("annotations") or {},
    }


def _upsert_rule(rule_body, existing_uids):
    """Create or update a single rule via the provisioning API."""
    uid    = rule_body["uid"]
    hdrs   = {"X-Disable-Provenance": "true"}
    if uid in existing_uids:
        status, resp = _request("PUT", f"/api/v1/provisioning/alert-rules/{uid}", rule_body, extra_headers=hdrs)
    else:
        status, resp = _request("POST", "/api/v1/provisioning/alert-rules", rule_body, extra_headers=hdrs)
    return status, resp


# ---------------------------------------------------------------------------
# Provision mode
# ---------------------------------------------------------------------------

def _load_groups():
    """Load alert rule groups from the alert-rules directory (one YAML file per
    group), merging every file's `groups`. Falls back to the legacy single-file
    path for backward compatibility with images that predate the split.
    Returns (groups, source_description)."""
    if os.path.isdir(RULES_DIR):
        groups = []
        for fn in sorted(os.listdir(RULES_DIR)):
            if fn.endswith((".yml", ".yaml")):
                with open(os.path.join(RULES_DIR, fn)) as f:
                    cfg = yaml.safe_load(f) or {}
                groups.extend(cfg.get("groups", []) or [])
        return groups, RULES_DIR
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            cfg = yaml.safe_load(f) or {}
        return (cfg.get("groups", []) or []), RULES_FILE
    return [], None


def provision():
    _wait_for_grafana()

    groups, source = _load_groups()
    if source is None:
        print(f"[provision-alerts] No rules found at {RULES_DIR} or {RULES_FILE}, skipping.", flush=True)
        return
    if not groups:
        print(f"[provision-alerts] No groups in {source}, skipping.", flush=True)
        return

    folders      = _folder_map()
    existing     = _existing_rule_uids()
    total_rules  = sum(len(g.get("rules", [])) for g in groups)
    print(f"[provision-alerts] Provisioning {total_rules} rules across {len(groups)} groups...", flush=True)

    yaml_uids = set()
    for group in groups:
        name   = group["name"]
        folder = group["folder"]
        uid    = _ensure_folder(folders, folder)

        if not uid:
            print(f"[provision-alerts]   SKIP  '{name}': could not create folder '{folder}'.", flush=True)
            continue

        rules     = group.get("rules", [])
        n_ok      = 0
        n_err     = 0
        for rule in rules:
            yaml_uids.add(rule["uid"])
            body            = _convert_rule(rule, uid, name)
            status, resp    = _upsert_rule(body, existing)
            if status in (200, 201):
                n_ok += 1
                existing.add(rule["uid"])
            else:
                n_err += 1
                print(f"[provision-alerts]   ERROR rule '{rule['uid']}' in '{name}': HTTP {status} — {resp}", flush=True)

        if n_err == 0:
            print(f"[provision-alerts]   OK    '{name}' ({n_ok} rules)", flush=True)
        else:
            print(f"[provision-alerts]   PARTIAL '{name}' ({n_ok} ok, {n_err} errors)", flush=True)

    # Delete rules that exist in Grafana but are no longer in the YAML
    stale = existing - yaml_uids
    for uid in stale:
        status, _ = _request("DELETE", f"/api/v1/provisioning/alert-rules/{uid}")
        if status in (200, 204):
            print(f"[provision-alerts]   DELETED stale rule '{uid}'", flush=True)
        else:
            print(f"[provision-alerts]   WARN could not delete stale rule '{uid}': HTTP {status}", flush=True)

    print("[provision-alerts] Provisioning complete.", flush=True)


# ---------------------------------------------------------------------------
# Migrate mode
# ---------------------------------------------------------------------------

def migrate():
    """
    Remove file-provenance alert rule records from the Grafana database so the
    provisioning API can take ownership without hitting the 'cannot change
    provenance from file to api' error in Grafana 13.

    Handles both SQLite (local/on-prem) and PostgreSQL (AWS RDS).
    """
    db_type = os.environ.get("GF_DATABASE_TYPE", "sqlite3")

    if db_type == "sqlite3":
        _migrate_sqlite()
    else:
        _migrate_postgres()


def _migrate_sqlite():
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
            print(f"[provision-alerts] Migration: removed {n} file-provenance record(s) from SQLite.", flush=True)
        else:
            print("[provision-alerts] Migration: nothing to remove from SQLite.", flush=True)
    except Exception as e:
        print(f"[provision-alerts] Migration warning (SQLite): {e}", flush=True)


def _migrate_postgres():
    try:
        import psycopg2
    except ImportError:
        print("[provision-alerts] psycopg2 not available — cannot run RDS migration automatically.", flush=True)
        print("[provision-alerts] Run manually against your database:", flush=True)
        print("  DELETE FROM provenance_type WHERE record_type = 'alertRule';", flush=True)
        return

    host_port = os.environ.get("GF_DATABASE_HOST", "localhost:5432")
    if ":" in host_port:
        host, port = host_port.rsplit(":", 1)
        port = int(port)
    else:
        host, port = host_port, 5432

    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=os.environ.get("GF_DATABASE_NAME", "grafana"),
            user=os.environ.get("GF_DATABASE_USER", "grafana"),
            password=os.environ.get("GF_DATABASE_PASSWORD", ""),
            sslmode=os.environ.get("GF_DATABASE_SSL_MODE", "require"),
        )
        cur = conn.cursor()
        cur.execute("DELETE FROM provenance_type WHERE record_type = 'alertRule'")
        n = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        if n:
            print(f"[provision-alerts] Migration: removed {n} file-provenance record(s) from RDS.", flush=True)
        else:
            print("[provision-alerts] Migration: nothing to remove from RDS.", flush=True)
    except Exception as e:
        print(f"[provision-alerts] Migration warning (RDS): {e}", flush=True)


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
