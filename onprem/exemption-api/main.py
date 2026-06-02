import json
import os
import urllib.parse
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
                ip          TEXT NOT NULL,
                name        TEXT NOT NULL,
                category    TEXT NOT NULL DEFAULT 'general',
                site_filter TEXT NOT NULL DEFAULT 'all',
                source_ip   TEXT,
                added_at    TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (ip, site_filter)
            )
        """)
        cur.execute("ALTER TABLE ping_targets ADD COLUMN IF NOT EXISTS source_ip TEXT")
        # Migrate old single-column PK to composite if needed
        cur.execute("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'ping_targets_pkey'
                    AND contype = 'p'
                    AND array_length(conkey, 1) = 1
                ) THEN
                    ALTER TABLE ping_targets DROP CONSTRAINT ping_targets_pkey;
                    ALTER TABLE ping_targets ADD PRIMARY KEY (ip, site_filter);
                END IF;
            END $$
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ping_sources (
                site_id     TEXT PRIMARY KEY,
                source      TEXT NOT NULL,
                source_ip   TEXT,
                added_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS exporter_exempt (
                hostname    TEXT PRIMARY KEY,
                reason      TEXT,
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

@app.get("/ping/metrics")
def ping_metrics():
    """Prometheus text format — one gauge per configured ping target.
    Scrape this from Prometheus so the dashboard can show configured targets
    as DOWN even before a firewall script has pushed any data."""
    from fastapi.responses import PlainTextResponse
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ip, name, category, site_filter FROM ping_targets ORDER BY site_filter, name")
        target_rows = cur.fetchall()
        cur.execute("SELECT source FROM ping_sources ORDER BY source")
        source_rows = cur.fetchall()
    lines = [
        "# HELP ping_source_registered Probe source registered in the API",
        "# TYPE ping_source_registered gauge",
    ]
    for (source,) in source_rows:
        lines.append(f'ping_source_registered{{source="{source}"}} 1')
    lines += [
        "",
        "# HELP ping_target_configured Ping target configured in the API",
        "# TYPE ping_target_configured gauge",
    ]
    for ip, name, category, site_filter in target_rows:
        for source in [s.strip() for s in site_filter.split(",")]:
            lines.append(
                f'ping_target_configured{{source="{source}",target="{name}",dst="{ip}",category="{category}"}} 1'
            )
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


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
def ping_targets_script(site_id: str = Query(..., description="Short site identifier registered in ping_sources, e.g. BH")):
    """Returns config and ping targets for a site firewall script.

    Response: {source, source_ip, targets: [{ip, name, category, source_ip?}]}
    The script uses source as the metrics label and source_ip as the default ping interface.
    Per-target source_ip overrides the firewall default for that destination.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT source, source_ip FROM ping_sources WHERE site_id = %s", (site_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"No source configured for site_id={site_id!r} — add it at /ping/sources")
        source, default_source_ip = row

        cur.execute("SELECT ip, name, category, site_filter, source_ip FROM ping_targets ORDER BY category, name")
        target_rows = cur.fetchall()

    targets = []
    for ip, name, category, site_filter, source_ip in target_rows:
        if site_filter == "all" or source in [s.strip() for s in site_filter.split(",")]:
            t = {"ip": ip, "name": name, "category": category}
            if source_ip:
                t["source_ip"] = source_ip
            targets.append(t)

    return JSONResponse({"source": source, "source_ip": default_source_ip, "targets": targets})


@app.get("/ping/sources/add")
def ping_sources_add(
    site_id: str = Query(...),
    source: str = Query(...),
    source_ip: str = Query(default=""),
):
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO ping_sources(site_id, source, source_ip)
               VALUES (%s, %s, %s)
               ON CONFLICT(site_id) DO UPDATE SET
                 source = EXCLUDED.source,
                 source_ip = EXCLUDED.source_ip""",
            (site_id, source, source_ip or None),
        )
    return _page(f"✓ Firewall <strong>{site_id}</strong> → <strong>{source}</strong> saved.")


@app.get("/ping/sources/remove")
def ping_sources_remove(site_id: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM ping_sources WHERE site_id = %s", (site_id,))
    return _page(f"✓ Source <strong>{site_id}</strong> removed.")


@app.get("/ping/sources/import-unifi")
def ping_sources_import_unifi():
    sites = _get_unifi_sites()
    if not sites:
        return _page("⚠ No UniFi controllers found — check that unifi-local-exporter is running.")
    added = []
    with get_conn() as conn:
        cur = conn.cursor()
        for site in sites:
            cur.execute(
                """INSERT INTO ping_sources(site_id, source)
                   VALUES (%s, %s)
                   ON CONFLICT(site_id) DO NOTHING""",
                (site, site),
            )
            if cur.rowcount:
                added.append(site)
    if added:
        return _page(f"✓ Imported {len(added)} UniFi source(s): <strong>{', '.join(added)}</strong>. "
                     f"Set a source IP for each if needed.")
    return _page("No new sources to import — all UniFi sites already registered.")


@app.get("/ping/sources", response_class=HTMLResponse)
def ping_sources_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT site_id, source, source_ip, added_at FROM ping_sources ORDER BY site_id")
        rows = cur.fetchall()

    rows_html = "".join(
        f"<tr>"
        f"<td>{r[0]}</td>"
        f"<td>{r[1]}</td>"
        f"<td>{r[2] or '<span style=\"color:#aaa\">not set</span>'}</td>"
        f"<td>{r[3].strftime('%Y-%m-%d %H:%M') if r[3] else ''}</td>"
        f"<td>"
        f"<a href='#add-form' onclick=\"prefill('{r[0]}','{r[1]}','{r[2] or ''}')\">Edit</a>"
        f" &nbsp; "
        f"<a href='/ping/sources/remove?site_id={r[0]}' "
        f"onclick=\"return confirm('Remove {r[0]}?')\">Remove</a>"
        f"</td>"
        f"</tr>"
        for r in rows
    ) or "<tr><td colspan='5' style='color:#888'>No sources configured</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Ping Probe Sources</title>
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
  .hint {{ font-size: 0.8rem; color: #666; margin-top: 0.2rem }}
  .legend {{ background: #f8f8f8; border: 1px solid #ddd; padding: 0.8rem 1rem; margin-bottom: 1.5rem; font-size: 0.9rem; border-radius: 3px }}
  .legend code {{ background: #eee; padding: 0.1rem 0.3rem; border-radius: 2px }}
</style>
</head>
<body>
<p><a href="/ping">← Back to Ping Targets</a></p>
<h1>Ping Probe Sources</h1>
<div class="legend">
  Each script-based probe identifies itself with a short <strong>Site ID</strong> (e.g. <code>BH</code>).
  The API returns the full <strong>Source</strong> name used in metrics labels and the optional default
  <strong>Source IP / Interface</strong> for pinging. Works with any Linux host running the site-ping script —
  not just firewalls. Per-target IP overrides are configured on the <a href="/ping">Ping Targets</a> page.
</div>

<table>
  <thead><tr><th>Site ID</th><th>Source (metrics label)</th><th>Default Source IP / Interface</th><th>Added</th><th></th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2>Import from UniFi</h2>
<p style="font-size:0.9rem;margin-bottom:0.5rem">Adds all UniFi controllers configured in the unifi-local-exporter as probe sources. Skips any already registered. You can set source IPs afterwards.</p>
<a href="/ping/sources/import-unifi" style="display:inline-block;padding:0.35rem 0.9rem;background:#1a73e8;color:#fff;border-radius:3px;text-decoration:none;font-size:0.9rem">Import from UniFi</a>

<h2 id="add-form">Add / update source</h2>
<form method="get" action="/ping/sources/add">
  <label>Site ID
    <input id="f-site_id" name="site_id" required placeholder="e.g. BH" size="10">
    <span class="hint">Short stable key set in the site-ping script</span>
  </label>
  <label>Source name
    <input id="f-source" name="source" required placeholder="e.g. BH-FIREWALL" size="22">
    <span class="hint">Used as the source= label in Prometheus metrics — any Linux host</span>
  </label>
  <label>Default source IP / interface
    <input id="f-source_ip" name="source_ip" placeholder="e.g. 192.168.1.1 or eth1" size="22">
    <span class="hint">Optional — leave blank to use OS default</span>
  </label>
  <button type="submit">Save</button>
</form>
<script>
function prefill(site_id, source, source_ip) {{
  document.getElementById('f-site_id').value = site_id;
  document.getElementById('f-source').value = source;
  document.getElementById('f-source_ip').value = source_ip;
}}
</script>
</body>
</html>""")


@app.get("/ping/add")
def ping_add(
    ip: str = Query(...),
    name: str = Query(...),
    category: str = Query(default="general"),
    site_filter: str = Query(default="all"),
    source_ip: str = Query(default=""),
):
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO ping_targets(ip, name, category, site_filter, source_ip)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT(ip, site_filter) DO UPDATE SET
                 name = EXCLUDED.name,
                 category = EXCLUDED.category,
                 source_ip = EXCLUDED.source_ip""",
            (ip, name, category, site_filter, source_ip or None),
        )
    return _page(f"✓ <strong>{name}</strong> ({ip}) added to ping targets.")


@app.get("/ping/save")
def ping_save(
    ip: str = Query(...),
    name: str = Query(...),
    category: str = Query(default="general"),
    site_filter: str = Query(default="all"),
    source_ip: str = Query(default=""),
    old_ip: str = Query(default=""),
    old_site_filter: str = Query(default=""),
):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT source FROM ping_sources")
        sources = [r[0] for r in cur.fetchall()]
        if old_ip and (old_ip != ip or old_site_filter != site_filter):
            cur.execute("SELECT name FROM ping_targets WHERE ip = %s AND site_filter = %s", (old_ip, old_site_filter))
            old_row = cur.fetchone()
            old_name = old_row[0] if old_row else old_ip
            cur.execute("DELETE FROM ping_targets WHERE ip = %s AND site_filter = %s", (old_ip, old_site_filter))
            for source in sources:
                _pushgateway_delete(source, old_name)
        cur.execute(
            """INSERT INTO ping_targets(ip, name, category, site_filter, source_ip)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT(ip, site_filter) DO UPDATE SET
                 name = EXCLUDED.name,
                 category = EXCLUDED.category,
                 source_ip = EXCLUDED.source_ip""",
            (ip, name, category, site_filter, source_ip or None),
        )
    return _page(f"✓ <strong>{name}</strong> ({ip}) saved.")


def _pushgateway_delete(source: str, target: str):
    url = (
        "http://pushgateway:9091/metrics/job/site_ping"
        f"/source/{urllib.parse.quote(source, safe='')}"
        f"/target/{urllib.parse.quote(target, safe='')}"
    )
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, method="DELETE"), timeout=3
        )
    except Exception:
        pass


@app.get("/ping/remove")
def ping_remove(ip: str = Query(...), site_filter: str = Query(...)):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM ping_targets WHERE ip = %s AND site_filter = %s", (ip, site_filter))
        row = cur.fetchone()
        if row:
            name = row[0]
            cur.execute("SELECT source FROM ping_sources")
            sources = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM ping_targets WHERE ip = %s AND site_filter = %s", (ip, site_filter))
        else:
            name = ip
            sources = []
    for source in sources:
        _pushgateway_delete(source, name)
    return _page(f"✓ {name} ({ip}) removed from ping targets.")


@app.get("/ping", response_class=HTMLResponse)
def ping_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT ip, name, category, site_filter, source_ip, added_at FROM ping_targets ORDER BY site_filter, name"
        )
        rows = cur.fetchall()
        cur.execute("SELECT source, site_id FROM ping_sources ORDER BY source")
        probe_sources = cur.fetchall()
    sites = _get_unifi_sites()

    rows_html = "".join(
        f"<tr>"
        f"<td>{r[3]}</td>"
        f"<td>{r[4] or ''}</td>"
        f"<td>{r[1]}</td>"
        f"<td>{r[0]}</td>"
        f"<td>{r[5].strftime('%Y-%m-%d %H:%M') if r[5] else ''}</td>"
        f"<td>"
        f"<a href='#target-form' onclick=\"prefill('{r[0]}','{r[1]}','{r[2]}','{r[3]}','{r[4] or ''}','{r[3]}')\">Edit</a>"
        f" &nbsp; "
        f"<a href='/ping/remove?ip={r[0]}&site_filter={urllib.parse.quote(r[3])}' onclick=\"return confirm('Remove {r[1]}?')\">Remove</a>"
        f"</td>"
        f"</tr>"
        for r in rows
    ) or "<tr><td colspan='7' style='color:#888'>No ping targets configured</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Ping Targets</title>
<style>
  body {{ font-family: sans-serif; padding: 2rem; max-width: 1100px }}
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
<p><a href="javascript:history.back()">← Back</a> &nbsp;|&nbsp; <a href="/ping/sources">Manage probe sources →</a></p>
<h1>Ping Targets</h1>
<div class="legend">
  <strong>Source filter</strong> controls which probe runs the ping:<br>
  <code>all</code> — probed by the blackbox exporter on poc-containers (HQ perspective)<br>
  Any registered source name (e.g. <code>BH-FIREWALL</code>, <code>poc-containers</code>) — probed by that host's script<br>
  Comma-separate to probe from multiple sources.<br><br>
  <strong>Source IP / interface</strong> (optional) — overrides which interface the source host uses for this specific target.
  Useful for VPN destinations where the default route won't reach. Per-source defaults are set in <a href="/ping/sources">Probe Sources</a>.
</div>

<table>
  <thead><tr><th>Source</th><th>Source IP override</th><th>Destination Name</th><th>Destination IP</th><th>Added</th><th></th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2 id="target-form">Add / edit target</h2>
<form method="get" action="/ping/save">
  <input type="hidden" id="f-old_ip" name="old_ip" value="">
  <input type="hidden" id="f-old_site_filter" name="old_site_filter" value="">
  <label>IP Address
    <input id="f-ip" name="ip" required placeholder="e.g. 10.168.161.1" size="18">
  </label>
  <label>Name
    <input id="f-name" name="name" required placeholder="e.g. ZEN Firewall" size="25">
  </label>
  <label>Category
    <input id="f-category" name="category" placeholder="e.g. site" value="general" size="12">
  </label>
  <label>Source filter
    <select id="f-site_filter" name="site_filter">
      <option value="all">all — probed from HQ blackbox</option>
      {"".join(f'<option value="{s[0]}">{s[0]} ({s[1]}) — probe script</option>' for s in probe_sources)}
    </select>
    <span class="hint">Controls which probe checks this target — <a href="/ping/sources">manage sources</a></span>
  </label>
  <label>Source IP / interface override
    <input id="f-source_ip" name="source_ip" placeholder="e.g. 10.0.0.1 or tun0" size="22">
    <span class="hint">Optional — for VPN targets that need a specific interface</span>
  </label>
  <button type="submit">Save</button>
</form>
<script>
function prefill(ip, name, category, site_filter, source_ip, old_site_filter) {{
  document.getElementById('f-old_ip').value = ip;
  document.getElementById('f-old_site_filter').value = old_site_filter || site_filter;
  document.getElementById('f-ip').value = ip;
  document.getElementById('f-name').value = name;
  document.getElementById('f-category').value = category;
  document.getElementById('f-source_ip').value = source_ip;
  var sel = document.getElementById('f-site_filter');
  for (var i = 0; i < sel.options.length; i++) {{
    if (sel.options[i].value === site_filter) {{ sel.selectedIndex = i; break; }}
  }}
}}
</script>
</body>
</html>""")
