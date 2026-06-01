import json
import os
import urllib.request
import psycopg2
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

app = FastAPI(title="Exemption API")

DB_CONFIG = dict(
    host=os.environ.get("DB_HOST", "monitoring-postgres"),
    port=int(os.environ.get("DB_PORT", 5432)),
    dbname=os.environ.get("DB_NAME", "monitoring"),
    user=os.environ.get("DB_USER", "monitoring"),
    password=os.environ.get("DB_PASSWORD", "monitoring"),
)

CATEGORY_LABELS = {
    "powered_off": "Powered Off VMs",
    "backup_overdue": "Backups Overdue",
    "active_alert": "Active Alerts",
}

def _get_unifi_sites() -> list[str]:
    """Fetch controller names from the unifi-local-exporter health endpoint."""
    try:
        with urllib.request.urlopen("http://unifi-local-exporter:3000/health", timeout=3) as r:
            return json.loads(r.read()).get("controllers", [])
    except Exception:
        return []


@contextmanager
def get_conn():
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

@app.on_event("startup")
def create_tables():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS platform_suppressions (
                category    TEXT NOT NULL,
                identifier  TEXT NOT NULL,
                reason      TEXT,
                added_at    TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (category, identifier)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS connectivity_checks (
                ip          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'general',
                added_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ping_targets (
                ip          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'general',
                site_filter TEXT NOT NULL DEFAULT 'all',
                added_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)

def _page(msg: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head><title>Exporter Exemption</title>
<style>body{{font-family:sans-serif;padding:2rem;max-width:500px}}</style>
</head>
<body>
<p>{msg}</p>
<p><a href="javascript:history.back()">← Back to dashboard</a></p>
<script>setTimeout(() => location.href = document.referrer || '/', 1500)</script>
</body>
</html>""")

@app.get("/exempt/add")
def add_exempt(host: str = Query(...), reason: str = Query(default="")):
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO exporter_exempt(hostname, reason) VALUES (%s, %s) ON CONFLICT(hostname) DO NOTHING",
            (host, reason),
        )
    return _page(f"✓ <strong>{host}</strong> exempted from exporter count.")

@app.get("/exempt/remove")
def remove_exempt(host: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute(
            "DELETE FROM exporter_exempt WHERE hostname = %s", (host,)
        )
    return _page(f"✓ <strong>{host}</strong> removed from exemption list.")

@app.get("/suppress/add")
def suppress_add(
    category: str = Query(...),
    identifier: str = Query(...),
    reason: str = Query(default=""),
):
    if category not in CATEGORY_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown category: {category}")
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO platform_suppressions(category, identifier, reason)
               VALUES (%s, %s, %s)
               ON CONFLICT(category, identifier) DO UPDATE SET reason = EXCLUDED.reason""",
            (category, identifier, reason),
        )
    label = CATEGORY_LABELS[category]
    return _page(f"✓ <strong>{identifier}</strong> suppressed from <strong>{label}</strong>.")

@app.get("/suppress/remove")
def suppress_remove(category: str = Query(...), identifier: str = Query(...)):
    if category not in CATEGORY_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown category: {category}")
    with get_conn() as conn:
        conn.cursor().execute(
            "DELETE FROM platform_suppressions WHERE category = %s AND identifier = %s",
            (category, identifier),
        )
    label = CATEGORY_LABELS[category]
    return _page(f"✓ <strong>{identifier}</strong> removed from <strong>{label}</strong> suppressions.")

@app.get("/suppress", response_class=HTMLResponse)
def suppress_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, identifier, reason, added_at FROM platform_suppressions ORDER BY category, identifier"
        )
        rows = cur.fetchall()

    rows_html = "".join(
        f"<tr>"
        f"<td>{CATEGORY_LABELS.get(r[0], r[0])}</td>"
        f"<td>{r[1]}</td>"
        f"<td>{r[2] or ''}</td>"
        f"<td>{r[3].strftime('%Y-%m-%d %H:%M') if r[3] else ''}</td>"
        f"<td><a href='/suppress/remove?category={r[0]}&identifier={r[1]}' "
        f"onclick=\"return confirm('Remove {r[1]}?')\">Remove</a></td>"
        f"</tr>"
        for r in rows
    ) or "<tr><td colspan='5' style='color:#888'>No suppressions configured</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Platform Overview Suppressions</title>
<style>
  body {{ font-family: sans-serif; padding: 2rem; max-width: 900px }}
  h1 {{ font-size: 1.4rem; margin-bottom: 1rem }}
  h2 {{ font-size: 1.1rem; margin: 2rem 0 0.5rem }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left }}
  th {{ background: #f4f4f4 }}
  a {{ color: #c00 }}
  form {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1rem }}
  label {{ display: flex; flex-direction: column; font-size: 0.85rem; gap: 0.2rem }}
  input, select {{ padding: 0.3rem 0.5rem; border: 1px solid #ccc; border-radius: 3px }}
  button {{ padding: 0.35rem 0.9rem; background: #1a73e8; color: #fff; border: none; border-radius: 3px; cursor: pointer }}
  button:hover {{ background: #1558b0 }}
  .back {{ font-size: 0.9rem }}
</style>
</head>
<body>
<p class="back"><a href="javascript:history.back()">← Back</a></p>
<h1>Platform Overview Suppressions</h1>
<p>Items suppressed here are excluded from the stat counts on the Platform Overview dashboard.</p>

<table>
  <thead><tr><th>Category</th><th>Identifier</th><th>Reason</th><th>Added</th><th></th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2>Add suppression</h2>
<form method="get" action="/suppress/add">
  <label>Category
    <select name="category">
      <option value="powered_off">Powered Off VMs</option>
      <option value="backup_overdue">Backups Overdue</option>
      <option value="active_alert">Active Alerts</option>
    </select>
  </label>
  <label>Identifier (VM name or alert name)
    <input name="identifier" required placeholder="e.g. my-vm or AlertName" size="30">
  </label>
  <label>Reason (optional)
    <input name="reason" placeholder="e.g. decommissioned" size="25">
  </label>
  <button type="submit">Add</button>
</form>
</body>
</html>""")


@app.get("/connectivity/targets")
def connectivity_targets():
    """Prometheus HTTP SD endpoint — returns all checks in service-discovery format."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ip, name, category FROM connectivity_checks ORDER BY category, name")
        rows = cur.fetchall()
    return JSONResponse([
        {"targets": [r[0]], "labels": {"name": r[1], "category": r[2]}}
        for r in rows
    ])


@app.get("/connectivity/add")
def connectivity_add(
    ip: str = Query(...),
    name: str = Query(...),
    category: str = Query(default="general"),
):
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO connectivity_checks(ip, name, category)
               VALUES (%s, %s, %s)
               ON CONFLICT(ip) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category""",
            (ip, name, category),
        )
    return _page(f"✓ <strong>{name}</strong> ({ip}) added to connectivity checks.")


@app.get("/connectivity/remove")
def connectivity_remove(ip: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM connectivity_checks WHERE ip = %s", (ip,))
    return _page(f"✓ {ip} removed from connectivity checks.")


@app.get("/connectivity", response_class=HTMLResponse)
def connectivity_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ip, name, category, added_at FROM connectivity_checks ORDER BY category, name")
        rows = cur.fetchall()

    rows_html = "".join(
        f"<tr>"
        f"<td>{r[1]}</td>"
        f"<td>{r[2]}</td>"
        f"<td>{r[0]}</td>"
        f"<td>{r[3].strftime('%Y-%m-%d %H:%M') if r[3] else ''}</td>"
        f"<td><a href='/connectivity/remove?ip={r[0]}' "
        f"onclick=\"return confirm('Remove {r[1]}?')\">Remove</a></td>"
        f"</tr>"
        for r in rows
    ) or "<tr><td colspan='5' style='color:#888'>No checks configured</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Connectivity Checks</title>
<style>
  body {{ font-family: sans-serif; padding: 2rem; max-width: 900px }}
  h1 {{ font-size: 1.4rem; margin-bottom: 1rem }}
  h2 {{ font-size: 1.1rem; margin: 2rem 0 0.5rem }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left }}
  th {{ background: #f4f4f4 }}
  a {{ color: #c00 }}
  form {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1rem }}
  label {{ display: flex; flex-direction: column; font-size: 0.85rem; gap: 0.2rem }}
  input {{ padding: 0.3rem 0.5rem; border: 1px solid #ccc; border-radius: 3px }}
  button {{ padding: 0.35rem 0.9rem; background: #1a73e8; color: #fff; border: none; border-radius: 3px; cursor: pointer }}
  button:hover {{ background: #1558b0 }}
</style>
</head>
<body>
<p><a href="javascript:history.back()">← Back</a></p>
<h1>Connectivity Checks</h1>
<p>ICMP ping checks scraped by Prometheus every 60s. Changes take effect within 30s.</p>

<table>
  <thead><tr><th>Name</th><th>Category</th><th>IP</th><th>Added</th><th></th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2>Add check</h2>
<form method="get" action="/connectivity/add">
  <label>IP Address
    <input name="ip" required placeholder="e.g. 10.0.0.1" size="18">
  </label>
  <label>Name
    <input name="name" required placeholder="e.g. Phone Provider VPN" size="28">
  </label>
  <label>Category
    <input name="category" placeholder="e.g. telephony" value="general" size="15">
  </label>
  <button type="submit">Add</button>
</form>
</body>
</html>""")


# ── UniFi API Ping Targets ────────────────────────────────────────────────────
# Targets are pinged from each site's own firewall via the UniFi controller API,
# so results reflect actual site-to-site reachability rather than HQ-only pings.

@app.get("/ping/targets")
def ping_targets_sd():
    """Prometheus HTTP SD endpoint — returns targets with source='all' for blackbox probing from HQ."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ip, name, category FROM ping_targets WHERE site_filter = 'all' ORDER BY category, name"
        )
        rows = cur.fetchall()
    return JSONResponse([
        {"targets": [r[0]], "labels": {"name": r[1], "category": r[2]}}
        for r in rows
    ])


@app.get("/ping/targets/script")
def ping_targets_script(source: str = Query(..., description="Site name, e.g. BH-FIREWALL")):
    """Returns ping targets for a specific site firewall script.

    Includes targets where site_filter is 'all' or contains this source name.
    Format: simple JSON list consumed by the on-firewall Python script.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ip, name, category, site_filter FROM ping_targets ORDER BY category, name")
        rows = cur.fetchall()
    result = []
    for ip, name, category, site_filter in rows:
        if site_filter == "all" or source in [s.strip() for s in site_filter.split(",")]:
            result.append({"ip": ip, "name": name, "category": category})
    return JSONResponse(result)


@app.get("/ping/add")
def ping_add(
    ip: str = Query(...),
    name: str = Query(...),
    category: str = Query(default="general"),
    site_filter: str = Query(default="all"),
):
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO ping_targets(ip, name, category, site_filter)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT(ip) DO UPDATE SET
                 name = EXCLUDED.name,
                 category = EXCLUDED.category,
                 site_filter = EXCLUDED.site_filter""",
            (ip, name, category, site_filter),
        )
    return _page(f"✓ <strong>{name}</strong> ({ip}) added to ping targets.")


@app.get("/ping/remove")
def ping_remove(ip: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM ping_targets WHERE ip = %s", (ip,))
    return _page(f"✓ {ip} removed from ping targets.")


@app.get("/ping", response_class=HTMLResponse)
def ping_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ip, name, category, site_filter, added_at FROM ping_targets ORDER BY category, name"
        )
        rows = cur.fetchall()
    sites = _get_unifi_sites()

    rows_html = "".join(
        f"<tr>"
        f"<td>{r[1]}</td>"
        f"<td>{r[2]}</td>"
        f"<td>{r[3]}</td>"
        f"<td>{r[0]}</td>"
        f"<td>{r[4].strftime('%Y-%m-%d %H:%M') if r[4] else ''}</td>"
        f"<td><a href='/ping/remove?ip={r[0]}' "
        f"onclick=\"return confirm('Remove {r[1]}?')\">Remove</a></td>"
        f"</tr>"
        for r in rows
    ) or "<tr><td colspan='6' style='color:#888'>No ping targets configured</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Ping Targets</title>
<style>
  body {{ font-family: sans-serif; padding: 2rem; max-width: 960px }}
  h1 {{ font-size: 1.4rem; margin-bottom: 1rem }}
  h2 {{ font-size: 1.1rem; margin: 2rem 0 0.5rem }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left }}
  th {{ background: #f4f4f4 }}
  a {{ color: #c00 }}
  form {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1rem }}
  label {{ display: flex; flex-direction: column; font-size: 0.85rem; gap: 0.2rem }}
  input {{ padding: 0.3rem 0.5rem; border: 1px solid #ccc; border-radius: 3px }}
  button {{ padding: 0.35rem 0.9rem; background: #1a73e8; color: #fff; border: none; border-radius: 3px; cursor: pointer }}
  button:hover {{ background: #1558b0 }}
  .hint {{ font-size: 0.8rem; color: #666; margin-top: 0.2rem }}
  .legend {{ background: #f8f8f8; border: 1px solid #ddd; padding: 0.8rem 1rem; margin-bottom: 1.5rem; font-size: 0.9rem; border-radius: 3px }}
  .legend code {{ background: #eee; padding: 0.1rem 0.3rem; border-radius: 2px }}
</style>
</head>
<body>
<p><a href="javascript:history.back()">← Back</a></p>
<h1>Ping Targets</h1>
<div class="legend">
  <strong>Source</strong> controls which probe runs the ping:<br>
  <code>all</code> — probed by the blackbox exporter on poc-containers (HQ perspective, appears as <code>probe_success{{job="ping_targets"}}</code>)<br>
  <code>BH-FIREWALL</code> / <code>ZEN-Firewall-EFG</code> etc. — probed by that site's firewall script pushing to Pushgateway (appears as <code>site_ping_success{{source="..."}}</code>)<br>
  Comma-separate to probe from multiple sources, e.g. <code>BH-FIREWALL,ZEN-Firewall-EFG</code>
</div>

<table>
  <thead><tr><th>Name</th><th>Category</th><th>Source</th><th>IP</th><th>Added</th><th></th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2>Add target</h2>
<form method="get" action="/ping/add">
  <label>IP Address
    <input name="ip" required placeholder="e.g. 10.168.161.1" size="18">
  </label>
  <label>Name
    <input name="name" required placeholder="e.g. ZEN Firewall" size="25">
  </label>
  <label>Category
    <input name="category" placeholder="e.g. site" value="general" size="12">
  </label>
  <label>Source
    <select name="site_filter">
      <option value="all">all — probed from HQ blackbox</option>
      {"".join(f'<option value="{s}">{s} — firewall script</option>' for s in sites)}
    </select>
    <span class="hint">Controls which probe checks this target</span>
  </label>
  <button type="submit">Add</button>
</form>
</body>
</html>""")
