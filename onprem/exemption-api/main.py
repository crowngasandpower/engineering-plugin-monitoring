import base64
import datetime
import time
import html
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import urllib.parse
import urllib.request
import psycopg2
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509 import ocsp as crypto_ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID

import logging as _logging
_log = _logging.getLogger(__name__)

@asynccontextmanager
async def _lifespan(app: FastAPI):
    _create_tables()
    _warn_if_multi_worker()
    yield

app = FastAPI(title="Exemption API", lifespan=_lifespan)

SD_DIR = os.environ.get("SD_DIR", "/sd")

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
    "high_memory_host": "High Memory Hosts (DB)",
}

# Allowlist for identifiers stored in platform_suppressions. These values are
# interpolated into PromQL regex via string_agg(..., '|') in dashboard template
# variables, so characters that are PromQL regex metacharacters must not appear.
# Dots are permitted (needed for IP addresses and FQDNs) and are the only remaining
# PromQL regex metacharacter. The specific risk: a dot in an instance label value
# (e.g. apps.prod-mysql:9100 in the selector instance!~"apps.prod-mysql:9100") would
# also match apps-prod-mysql:9100 or appsprod-mysql:9100 — potentially suppressing a
# different host. In this controlled internal environment with known hostnames this is
# accepted, but new identifiers with dots in hostname segments should be noted.
_SAFE_IDENTIFIER_RE = re.compile(r'^[a-zA-Z0-9._:\-]+$')

def _get_unifi_sites() -> list[str]:
    """Return all console names: parsed from local exporter metrics (includes offline) merged with Site Manager."""
    names: set[str] = set()
    # Primary: parse unifi_scrape_success from local exporter metrics — always emitted per controller
    # regardless of whether it's reachable, so offline consoles are included.
    try:
        with urllib.request.urlopen("http://unifi-local-exporter:3000/metrics", timeout=5) as r:
            body = r.read().decode()
        for line in body.splitlines():
            if not line.startswith('#') and line.startswith("unifi_scrape_success{"):
                # @ai-review-ignore: [^"]+ won't match escaped quotes — site names in practice
                # are plain strings; a full Prometheus text-format parser is not warranted here.
                m = re.search(r'site="([^"]+)"', line)
                if m:
                    names.add(m.group(1))
    except Exception as exc:
        _log.debug("_get_unifi_sites: local exporter unavailable: %s", exc)
    # Supplement: Site Manager exporter metrics — authority on all consoles, including offline ones
    try:
        with urllib.request.urlopen("http://unifi-exporter:3000/metrics", timeout=5) as r:
            body = r.read().decode()
        for line in body.splitlines():
            if not line.startswith('#') and line.startswith("unifi_host_up{"):
                # @ai-review-ignore: same as above — host names don't contain escaped quotes.
                m = re.search(r'host="([^"]+)"', line)
                if m:
                    names.add(m.group(1))
    except Exception as exc:
        _log.debug("_get_unifi_sites: site manager exporter unavailable: %s", exc)
    return sorted(names)


def _get_unifi_devices() -> list[str]:
    """Fetch all known UniFi device names directly from the unifi-local-exporter metrics."""
    try:
        with urllib.request.urlopen("http://unifi-local-exporter:3000/metrics", timeout=3) as r:
            body = r.read().decode()
        devices = set()
        for line in body.splitlines():
            if line.startswith("unifi_device_up{"):
                m = re.search(r'device="([^"]+)"', line)
                if m:
                    devices.add(m.group(1))
        return sorted(devices)
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

def _create_tables():
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cert_targets (
                url      TEXT PRIMARY KEY,
                name     TEXT NOT NULL,
                team     TEXT NOT NULL DEFAULT 'general',
                added_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS unifi_exempt (
                device   TEXT PRIMARY KEY,
                reason   TEXT,
                added_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS unifi_console_exempt (
                site     TEXT PRIMARY KEY,
                reason   TEXT,
                added_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

def _warn_if_multi_worker():
    workers = os.environ.get("WEB_CONCURRENCY", "1")
    if workers != "1":
        _log.warning(
            "exemption-api: WEB_CONCURRENCY=%s but node_memory_exempt uses an "
            "in-process TTL cache that is not shared across workers — results may "
            "be stale. Pin --workers 1 or switch to a shared cache backend.",
            workers,
        )
    else:
        _log.info("exemption-api: single-worker mode confirmed; in-process cache active")

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


def _js(v: str) -> str:
    """Escape a value for safe embedding in a JS single-quoted string inside an HTML double-quoted attribute.
    Handles JS-level escaping (backslash, single quote, newlines) then HTML-level escaping (", <, >, &)."""
    s = str(v).replace("\\", "\\\\").replace("'", "\\'").replace("\r", "").replace("\n", " ")
    return html.escape(s)

@app.get("/exempt/add")
def add_exempt(host: str = Query(...), reason: str = Query(default="")):
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO exporter_exempt(hostname, reason) VALUES (%s, %s) ON CONFLICT(hostname) DO NOTHING",
            (host, reason),
        )
    return _page(f"✓ <strong>{html.escape(host)}</strong> exempted from exporter count.")

@app.get("/exempt/remove")
def remove_exempt(host: str = Query(...)):
    if not _SAFE_IDENTIFIER_RE.match(host):
        raise HTTPException(status_code=400, detail="Host contains invalid characters. Use only letters, digits, dots, hyphens, underscores, and colons.")
    with get_conn() as conn:
        conn.cursor().execute(
            "DELETE FROM exporter_exempt WHERE hostname = %s", (host,)
        )
    return _page(f"✓ <strong>{html.escape(host)}</strong> removed from exemption list.")

# In-process caches — valid only because the Dockerfile pins --workers 1.
# Multi-worker deployments would need a shared backend (Redis/Postgres).
# Locks guard against concurrent sync-handler threads causing a double DB query
# on cache expiry (no data corruption, but avoids redundant work under load).
_node_memory_exempt_cache: tuple[float, str] | None = None
_NODE_MEMORY_EXEMPT_TTL = 60.0
_node_memory_exempt_lock = threading.Lock()
_vm_powered_off_exempt_cache: tuple[float, str] | None = None
_VM_POWERED_OFF_EXEMPT_TTL = 60.0
_vm_powered_off_exempt_lock = threading.Lock()

@app.get("/node-memory-exempt/metrics")
def node_memory_exempt_metrics():
    """Prometheus text format — one gauge per host suppressed from memory % alerts.
    Scraped by the node_memory_exempt job so alert rules can filter DB hosts via
    `unless on(instance) node_memory_exempt`. Identifiers must match the instance
    label used by node_exporter / windows_exporter (short hostname, no domain).

    This endpoint is intentionally unauthenticated. It is not routed through the
    Traefik/nginx proxy and is only reachable from within the Docker compose network
    (the Prometheus container is the sole intended consumer)."""
    global _node_memory_exempt_cache
    now = time.monotonic()
    with _node_memory_exempt_lock:
        if _node_memory_exempt_cache is not None:
            cached_at, body = _node_memory_exempt_cache
            if now - cached_at < _NODE_MEMORY_EXEMPT_TTL:
                return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT identifier FROM platform_suppressions WHERE category = 'high_memory_host' ORDER BY identifier"
                )
                rows = cur.fetchall()
        except Exception as exc:
            # Return stale cache rather than a 500 — a scrape failure causes node_memory_exempt
            # to disappear from TSDB, which breaks the `unless on(instance) node_memory_exempt`
            # clause and immediately un-suppresses all DB hosts in the memory alert.
            _log.warning("node_memory_exempt_metrics: DB unavailable (%s); returning %s",
                         exc, "stale cache" if _node_memory_exempt_cache is not None else "empty metrics")
            if _node_memory_exempt_cache is not None:
                _, body = _node_memory_exempt_cache
                return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
            # @ai-review-ignore: empty-metrics path deliberately returns PlainTextResponse (HTTP 200)
            # not an exception, so Prometheus marks the scrape as succeeded. With no series the
            # `unless` clause becomes a no-op (all hosts fire), but that is unavoidable when the DB
            # has never been reachable and there is no prior cache to fall back on.
            return PlainTextResponse(
                "# HELP node_memory_exempt 1 if this host is suppressed from memory utilisation alerts (expected high memory, e.g. DB servers)\n"
                "# TYPE node_memory_exempt gauge\n",
                media_type="text/plain; version=0.0.4",
            )

        lines = [
            "# HELP node_memory_exempt 1 if this host is suppressed from memory utilisation alerts (expected high memory, e.g. DB servers)",
            "# TYPE node_memory_exempt gauge",
        ]
        for (identifier,) in rows:
            # _escape_label is mandatory here — identifier is user-supplied via /suppress/add
            # and a raw value would allow Prometheus text-format label injection.
            lines.append(f'node_memory_exempt{{instance="{_escape_label(identifier)}"}} 1')
        body = "\n".join(lines) + "\n"
        _node_memory_exempt_cache = (now, body)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


# In-process cache — valid only because the Dockerfile pins --workers 1.
@app.get("/vm-powered-off-exempt/metrics")
def vm_powered_off_exempt_metrics():
    """Prometheus text format — one gauge per VM suppressed from the powered-off alert.
    Scraped by the vcenter_vm_powered_off_exempt job. The vm_name label must match
    the vm_name label on vcenter_vm_powered_off emitted by vcenter-sd.

    Intentionally unauthenticated; only reachable within the Docker compose network."""
    global _vm_powered_off_exempt_cache
    now = time.monotonic()
    with _vm_powered_off_exempt_lock:
        if _vm_powered_off_exempt_cache is not None:
            cached_at, body = _vm_powered_off_exempt_cache
            if now - cached_at < _VM_POWERED_OFF_EXEMPT_TTL:
                return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

        try:
            with get_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT identifier FROM platform_suppressions WHERE category = 'powered_off' ORDER BY identifier"
                )
                rows = cur.fetchall()
        except Exception as exc:
            # Same rationale as node_memory_exempt_metrics: a scrape failure would cause the
            # vcenter_vm_powered_off_exempt series to disappear, breaking the `unless on(vm_name)`
            # suppression and firing alerts for all suppressed VMs.
            _log.warning("vm_powered_off_exempt_metrics: DB unavailable (%s); returning %s",
                         exc, "stale cache" if _vm_powered_off_exempt_cache is not None else "empty metrics")
            if _vm_powered_off_exempt_cache is not None:
                _, body = _vm_powered_off_exempt_cache
                return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
            # @ai-review-ignore: same reasoning as node_memory_exempt_metrics — HTTP 200 with no
            # series is unavoidable on first-startup DB failure; stale cache preferred when available.
            return PlainTextResponse(
                "# HELP vcenter_vm_powered_off_exempt 1 if this VM is suppressed from the powered-off alert\n"
                "# TYPE vcenter_vm_powered_off_exempt gauge\n",
                media_type="text/plain; version=0.0.4",
            )

        lines = [
            "# HELP vcenter_vm_powered_off_exempt 1 if this VM is suppressed from the powered-off alert",
            "# TYPE vcenter_vm_powered_off_exempt gauge",
        ]
        for (identifier,) in rows:
            # _escape_label is mandatory here — identifier is user-supplied via /suppress/add
            # and a raw value would allow Prometheus text-format label injection.
            lines.append(f'vcenter_vm_powered_off_exempt{{vm_name="{_escape_label(identifier)}"}} 1')
        body = "\n".join(lines) + "\n"
        _vm_powered_off_exempt_cache = (now, body)
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


# @ai-review-ignore: suppress/add, suppress/remove, and exempt/remove are unauthenticated
# by design. They are only reachable from within the Docker compose network (no external
# routing) or via the nginx proxy on poc-containers (LAN-only). Adding bearer-token auth
# would require reworking the HTML forms that submit via browser GET, which is out of scope
# for this PR. The risk is accepted: a compromised compose-network container could alter
# suppressions, but cannot exfiltrate data or escalate beyond monitoring configuration.
@app.get("/suppress/add")
def suppress_add(
    category: str = Query(...),
    identifier: str = Query(...),
    reason: str = Query(default=""),
):
    if category not in CATEGORY_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown category: {category}")
    if not _SAFE_IDENTIFIER_RE.match(identifier):
        raise HTTPException(status_code=400, detail="Identifier contains invalid characters. Use only letters, digits, dots, hyphens, underscores, and colons.")
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO platform_suppressions(category, identifier, reason)
               VALUES (%s, %s, %s)
               ON CONFLICT(category, identifier) DO UPDATE SET reason = EXCLUDED.reason""",
            (category, identifier, reason),
        )
    label = CATEGORY_LABELS[category]
    return _page(f"✓ <strong>{html.escape(identifier)}</strong> suppressed from <strong>{html.escape(label)}</strong>.")

@app.get("/suppress/remove")
def suppress_remove(category: str = Query(...), identifier: str = Query(...)):
    if category not in CATEGORY_LABELS:
        raise HTTPException(status_code=400, detail=f"Unknown category: {category}")
    if not _SAFE_IDENTIFIER_RE.match(identifier):
        raise HTTPException(status_code=400, detail="Identifier contains invalid characters. Use only letters, digits, dots, hyphens, underscores, and colons.")
    with get_conn() as conn:
        conn.cursor().execute(
            "DELETE FROM platform_suppressions WHERE category = %s AND identifier = %s",
            (category, identifier),
        )
    label = CATEGORY_LABELS[category]
    return _page(f"✓ <strong>{html.escape(identifier)}</strong> removed from <strong>{html.escape(label)}</strong> suppressions.")

@app.get("/suppress", response_class=HTMLResponse)
def suppress_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, identifier, reason, added_at FROM platform_suppressions ORDER BY category, identifier"
        )
        rows = cur.fetchall()
        cur.execute("SELECT device, reason, added_at FROM unifi_exempt ORDER BY device")
        unifi_rows = cur.fetchall()
        cur.execute("SELECT site, reason, added_at FROM unifi_console_exempt ORDER BY site")
        console_rows = cur.fetchall()

    rows_html = "".join(
        f"<tr>"
        f"<td>{CATEGORY_LABELS.get(r[0], html.escape(r[0]))}</td>"
        f"<td>{html.escape(r[1])}</td>"
        f"<td>{html.escape(r[2] or '')}</td>"
        f"<td>{r[3].strftime('%Y-%m-%d %H:%M') if r[3] else ''}</td>"
        f"<td><a href='/suppress/remove?category={urllib.parse.quote(r[0])}&identifier={urllib.parse.quote(r[1])}' "
        f"onclick=\"return confirm('Remove {_js(r[1])}?')\">Remove</a></td>"
        f"</tr>"
        for r in rows
    ) or "<tr><td colspan='5' style='color:#888'>No suppressions configured</td></tr>"

    unifi_rows_html = "".join(
        f"<tr>"
        f"<td>{html.escape(r[0])}</td>"
        f"<td>{html.escape(r[1] or '')}</td>"
        f"<td>{r[2].strftime('%Y-%m-%d %H:%M') if r[2] else ''}</td>"
        f"<td><a href='/unifi/remove?device={urllib.parse.quote(r[0])}' "
        f"onclick=\"return confirm('Remove {_js(r[0])}?')\">Remove</a></td>"
        f"</tr>"
        for r in unifi_rows
    ) or "<tr><td colspan='4' style='color:#888'>No exemptions configured</td></tr>"

    exempted_devices = {r[0] for r in unifi_rows}
    all_devices = _get_unifi_devices()
    device_options = "".join(
        f'<option value="{html.escape(d)}">{html.escape(d)}</option>'
        for d in all_devices
        if d not in exempted_devices
    ) or '<option value="" disabled>No devices found — check Prometheus</option>'

    console_rows_html = "".join(
        f"<tr>"
        f"<td>{html.escape(r[0])}</td>"
        f"<td>{html.escape(r[1] or '')}</td>"
        f"<td>{r[2].strftime('%Y-%m-%d %H:%M') if r[2] else ''}</td>"
        f"<td><a href='/unifi/console/remove?site={urllib.parse.quote(r[0])}' "
        f"onclick=\"return confirm('Remove {_js(r[0])}?')\">Remove</a></td>"
        f"</tr>"
        for r in console_rows
    ) or "<tr><td colspan='4' style='color:#888'>No exemptions configured</td></tr>"

    exempted_consoles = {r[0] for r in console_rows}
    all_sites = _get_unifi_sites()
    console_options = "".join(
        f'<option value="{html.escape(s)}">{html.escape(s)}</option>'
        for s in all_sites
        if s not in exempted_consoles
    ) or '<option value="" disabled>No consoles found — check Prometheus</option>'

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Platform Overview Suppressions</title>
<style>
  body {{ font-family: sans-serif; padding: 2rem; max-width: 900px }}
  h1 {{ font-size: 1.4rem; margin-bottom: 1rem }}
  h2 {{ font-size: 1.1rem; margin: 2rem 0 0.5rem }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 2.5rem 0 }}
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
      <option value="high_memory_host">High Memory Hosts (DB)</option>
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

<div style="background:#fff8e1;border:1px solid #ffe082;border-radius:4px;padding:0.8rem 1rem;margin-top:1rem;font-size:0.9rem">
  <strong>High Memory Hosts (DB)</strong> — use this category to suppress false positives from the
  <em>Memory &gt; 90%</em> alert and dashboard panel for database servers (MySQL, SQL Server, etc.)
  that legitimately claim all available RAM for their buffer pool.<br><br>
  The identifier must match the Prometheus <code>instance</code> label exactly — include the port suffix
  (e.g. <code>apps-prod-mysql:9100</code> for Linux hosts, <code>apps-prod-win:9182</code> for Windows).
  Check Prometheus targets or the Infrastructure Detail dashboard to find the exact label value.<br><br>
  ⚠ Suppressing a host here only silences the memory <em>utilisation</em> alert. Genuine memory pressure
  on these hosts will still be caught by the <strong>Active Swapping</strong> alert, which fires when the
  host is actively reading pages back from swap. Check the <em>Active Swap-In</em> panels in the
  Infrastructure Detail dashboard for more detail.
</div>

<hr>
<h2>UniFi Device Exemptions</h2>
<p>Devices listed here are excluded from the <strong>network-unifi-offline</strong> alert
and hidden from the Device Status table in the UniFi Site Detail dashboard when offline.
Changes take effect within 60 seconds.</p>

<table>
  <thead><tr><th>Device</th><th>Reason</th><th>Added</th><th></th></tr></thead>
  <tbody>{unifi_rows_html}</tbody>
</table>

<h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Add exemption</h3>
<form method="get" action="/unifi/add">
  <label>Device
    <select name="device" required>
      {device_options}
    </select>
  </label>
  <label>Reason (optional)
    <input name="reason" placeholder="e.g. decommissioned" size="25">
  </label>
  <button type="submit">Add</button>
</form>

<hr>
<h2>UniFi Console Exemptions</h2>
<p>Consoles listed here are excluded from the <strong>network-unifi-console-offline</strong> alert
and shown as <strong>Offline (Suppressed)</strong> in the Console Status table in the UniFi Site Detail dashboard.
Changes take effect within 60 seconds.</p>

<table>
  <thead><tr><th>Console</th><th>Reason</th><th>Added</th><th></th></tr></thead>
  <tbody>{console_rows_html}</tbody>
</table>

<h3 style="font-size:1rem;margin:1.5rem 0 0.5rem">Add exemption</h3>
<form method="get" action="/unifi/console/add">
  <label>Console
    <select name="site" required>
      {console_options}
    </select>
  </label>
  <label>Reason (optional)
    <input name="reason" placeholder="e.g. temporary maintenance" size="25">
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
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail="ip must be a valid IP address (IPv4 or IPv6)")
    if addr.is_loopback or addr.is_link_local:
        raise HTTPException(status_code=400, detail="Loopback and link-local addresses are not permitted")
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO connectivity_checks(ip, name, category)
               VALUES (%s, %s, %s)
               ON CONFLICT(ip) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category""",
            (ip, name, category),
        )
    return _page(f"✓ <strong>{html.escape(name)}</strong> ({html.escape(ip)}) added to connectivity checks.")


@app.get("/connectivity/remove")
def connectivity_remove(ip: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM connectivity_checks WHERE ip = %s", (ip,))
    return _page(f"✓ {html.escape(ip)} removed from connectivity checks.")


@app.get("/connectivity", response_class=HTMLResponse)
def connectivity_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ip, name, category, added_at FROM connectivity_checks ORDER BY category, name")
        rows = cur.fetchall()

    rows_html = "".join(
        f"<tr>"
        f"<td>{html.escape(r[1])}</td>"
        f"<td>{html.escape(r[2])}</td>"
        f"<td>{html.escape(r[0])}</td>"
        f"<td>{r[3].strftime('%Y-%m-%d %H:%M') if r[3] else ''}</td>"
        f"<td><a href='/connectivity/remove?ip={urllib.parse.quote(r[0])}' "
        f"onclick=\"return confirm('Remove {_js(r[1])}?')\">Remove</a></td>"
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
        lines.append(f'ping_source_registered{{source="{_escape_label(source)}"}} 1')
    lines += [
        "",
        "# HELP ping_target_configured Ping target configured in the API",
        "# TYPE ping_target_configured gauge",
    ]
    for ip, name, category, site_filter in target_rows:
        for source in [s.strip() for s in site_filter.split(",")]:
            lines.append(
                f'ping_target_configured{{source="{_escape_label(source)}",target="{_escape_label(name)}",dst="{_escape_label(ip)}",category="{_escape_label(category)}"}} 1'
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
    if source_ip and not re.match(r'^[a-zA-Z0-9._:/-]{1,64}$', source_ip):
        raise HTTPException(status_code=400, detail="source_ip must be an IP address or interface name (e.g. 192.168.1.1 or eth1)")
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO ping_sources(site_id, source, source_ip)
               VALUES (%s, %s, %s)
               ON CONFLICT(site_id) DO UPDATE SET
                 source = EXCLUDED.source,
                 source_ip = EXCLUDED.source_ip""",
            (site_id, source, source_ip or None),
        )
    return _page(f"✓ Firewall <strong>{html.escape(site_id)}</strong> → <strong>{html.escape(source)}</strong> saved.")


@app.get("/ping/sources/remove")
def ping_sources_remove(site_id: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM ping_sources WHERE site_id = %s", (site_id,))
    return _page(f"✓ Source <strong>{html.escape(site_id)}</strong> removed.")


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
        return _page(f"✓ Imported {len(added)} UniFi source(s): <strong>{html.escape(', '.join(added))}</strong>. "
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
        f"<td>{html.escape(r[0])}</td>"
        f"<td>{html.escape(r[1])}</td>"
        f"<td>{html.escape(r[2]) if r[2] else '<span style=\"color:#aaa\">not set</span>'}</td>"
        f"<td>{r[3].strftime('%Y-%m-%d %H:%M') if r[3] else ''}</td>"
        f"<td>"
        f"<a href='#add-form' onclick=\"prefill('{_js(r[0])}','{_js(r[1])}','{_js(r[2] or '')}')\">Edit</a>"
        f" &nbsp; "
        f"<a href='/ping/sources/remove?site_id={urllib.parse.quote(r[0])}' "
        f"onclick=\"return confirm('Remove {_js(r[0])}?')\">Remove</a>"
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
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail="ip must be a valid IP address (IPv4 or IPv6)")
    if addr.is_loopback or addr.is_link_local:
        raise HTTPException(status_code=400, detail="Loopback and link-local addresses are not permitted")
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
    return _page(f"✓ <strong>{html.escape(name)}</strong> ({html.escape(ip)}) added to ping targets.")


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
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=400, detail="ip must be a valid IP address (IPv4 or IPv6)")
    if addr.is_loopback or addr.is_link_local:
        raise HTTPException(status_code=400, detail="Loopback and link-local addresses are not permitted")
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
    return _page(f"✓ <strong>{html.escape(name)}</strong> ({html.escape(ip)}) saved.")


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
    return _page(f"✓ {html.escape(name)} ({html.escape(ip)}) removed from ping targets.")


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
        f"<td>{html.escape(r[3])}</td>"
        f"<td>{html.escape(r[4] or '')}</td>"
        f"<td>{html.escape(r[1])}</td>"
        f"<td>{html.escape(r[0])}</td>"
        f"<td>{r[5].strftime('%Y-%m-%d %H:%M') if r[5] else ''}</td>"
        f"<td>"
        f"<a href='#target-form' onclick=\"prefill('{_js(r[0])}','{_js(r[1])}','{_js(r[2])}','{_js(r[3])}','{_js(r[4] or '')}','{_js(r[3])}')\">Edit</a>"
        f" &nbsp; "
        f"<a href='/ping/remove?ip={urllib.parse.quote(r[0])}&site_filter={urllib.parse.quote(r[3])}' onclick=\"return confirm('Remove {_js(r[1])}?')\">Remove</a>"
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
      {"".join(f'<option value="{html.escape(s[0])}">{html.escape(s[0])} ({html.escape(s[1])}) — probe script</option>' for s in probe_sources)}
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


# ── Certificate Monitoring ────────────────────────────────────────────────────
# Tracks HTTPS URLs using corporate certificates. Prometheus scrapes:
#   - /cert/targets  → HTTP SD for blackbox cert_probe job (TLS expiry via probe_ssl_earliest_cert_expiry)
#   - /cert/metrics  → custom Prometheus text: OCSP revocation + SHA-1 chain + cert metadata

def _cert_get(url: str) -> tuple:
    """Open TLS connection and return (x509.Certificate, success:bool). Skips chain
    verification so we can inspect expired or self-signed certs."""
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or url
    port = parsed.port or 443
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((hostname, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                der = ssock.getpeercert(binary_form=True)
        if not der:
            return None, False
        return x509.load_der_x509_certificate(der, default_backend()), True
    except Exception:
        return None, False


def _get_issuer_cert(cert: x509.Certificate):
    """Download the issuer certificate from the AIA caIssuers URL."""
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
        for desc in aia.value:
            if desc.access_method == AuthorityInformationAccessOID.CA_ISSUERS:
                req = urllib.request.Request(
                    desc.access_location.value,
                    headers={"User-Agent": "crown-cert-monitor/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read()
                try:
                    return x509.load_der_x509_certificate(data, default_backend())
                except Exception:
                    return x509.load_pem_x509_certificate(data, default_backend())
    except Exception:
        return None


def _ocsp_status(cert: x509.Certificate, issuer: x509.Certificate) -> int:
    """Return 0=good  1=revoked  2=unknown  -1=error/unreachable."""
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
        ocsp_url = None
        for desc in aia.value:
            if desc.access_method == AuthorityInformationAccessOID.OCSP:
                ocsp_url = desc.access_location.value
                break
        if not ocsp_url:
            return 2

        builder = crypto_ocsp.OCSPRequestBuilder().add_certificate(cert, issuer, hashes.SHA1())
        req_bytes = builder.build().public_bytes(serialization.Encoding.DER)

        post = urllib.request.Request(
            ocsp_url,
            data=req_bytes,
            headers={
                "Content-Type": "application/ocsp-request",
                "User-Agent": "crown-cert-monitor/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(post, timeout=10) as r:
            resp = crypto_ocsp.load_der_ocsp_response(r.read())

        if resp.response_status == crypto_ocsp.OCSPResponseStatus.SUCCESSFUL:
            if resp.certificate_status == crypto_ocsp.OCSPCertStatus.GOOD:
                return 0
            if resp.certificate_status == crypto_ocsp.OCSPCertStatus.REVOKED:
                return 1
        return 2
    except Exception:
        return -1


def _crl_status(cert: x509.Certificate) -> int:
    """Check CRL revocation. Return 0=good 1=revoked 2=unknown -1=error."""
    try:
        cdp_ext = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints)
        for dp in cdp_ext.value:
            for gn in dp.full_name:
                url = gn.value
                if not url.startswith("http"):
                    continue
                req = urllib.request.Request(
                    url, headers={"User-Agent": "crown-cert-monitor/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    crl = x509.load_der_x509_crl(r.read())
                return 1 if crl.get_revoked_certificate_by_serial_number(cert.serial_number) else 0
        return 2
    except Exception:
        return -1


def _chain_has_sha1(cert: x509.Certificate) -> bool:
    try:
        alg = cert.signature_hash_algorithm
        return alg is not None and alg.name.lower() in ("sha1", "md5", "md2")
    except Exception:
        return False


def _cn_from_name(name: x509.Name) -> str:
    try:
        return name.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    except Exception:
        return str(name)[:64]


def _check_one_cert(target: dict) -> dict:
    url, name, team = target["url"], target["name"], target["team"]
    result = {
        "url": url, "name": name, "team": team,
        "check_success": 0,
        "ocsp_status": -1,
        "has_sha1": 0,
        "subject_cn": "",
        "issuer_cn": "",
        "not_after": "",
        "expiry_ts": 0.0,
    }
    cert, ok = _cert_get(url)
    if not ok or cert is None:
        return result

    result["check_success"] = 1
    result["has_sha1"] = 1 if _chain_has_sha1(cert) else 0
    result["subject_cn"] = _cn_from_name(cert.subject)
    result["issuer_cn"] = _cn_from_name(cert.issuer)
    result["not_after"] = cert.not_valid_after_utc.strftime("%Y-%m-%d")
    result["expiry_ts"] = cert.not_valid_after_utc.timestamp()

    issuer = _get_issuer_cert(cert)
    ocsp = _ocsp_status(cert, issuer) if issuer else 2
    if ocsp in (2, -1):
        ocsp = _crl_status(cert)
    result["ocsp_status"] = ocsp
    return result


def _escape_label(v: str) -> str:
    """Escape a Prometheus label value per the text exposition format spec.
    Order is mandatory: backslash first, then double-quote, then newline.
    https://prometheus.io/docs/instrumenting/exposition_formats/#text-format-details
    """
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@app.get("/cert/targets")
def cert_targets_sd():
    """Prometheus HTTP SD — feeds the cert_probe blackbox job."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT url, name, team FROM cert_targets ORDER BY name")
        rows = cur.fetchall()
    return JSONResponse([
        {"targets": [r[0]], "labels": {"name": r[1], "team": r[2]}}
        for r in rows
    ])


@app.get("/cert/metrics")
def cert_metrics():
    """Prometheus text format — OCSP status, chain validation, and cert metadata.
    Scraped by the cert_custom job every 5 minutes. Checks run in parallel (up to 10
    concurrent connections) so scrape time scales with the slowest host, not total count."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT url, name, team FROM cert_targets ORDER BY name")
        targets = [{"url": r[0], "name": r[1], "team": r[2]} for r in cur.fetchall()]

    if not targets:
        return PlainTextResponse(
            "# No cert targets configured — add URLs at /cert\n",
            media_type="text/plain; version=0.0.4",
        )

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(_check_one_cert, targets))

    def lbl(r: dict, extra: str = "") -> str:
        base = f'url="{_escape_label(r["url"])}",name="{_escape_label(r["name"])}",team="{_escape_label(r["team"])}"'
        return base + ("," + extra if extra else "")

    lines = [
        "# HELP cert_check_success 1 if TLS connection to the target succeeded",
        "# TYPE cert_check_success gauge",
        *[f'cert_check_success{{{lbl(r)}}} {r["check_success"]}' for r in results],
        "",
        "# HELP cert_ocsp_status OCSP revocation status: 0=good 1=revoked 2=unknown -1=error",
        "# TYPE cert_ocsp_status gauge",
        *[f'cert_ocsp_status{{{lbl(r)}}} {r["ocsp_status"]}' for r in results if r["check_success"]],
        "",
        "# HELP cert_chain_has_sha1 1 if the leaf certificate uses a weak SHA-1 or MD5 signature",
        "# TYPE cert_chain_has_sha1 gauge",
        *[f'cert_chain_has_sha1{{{lbl(r)}}} {r["has_sha1"]}' for r in results if r["check_success"]],
        "",
        "# HELP cert_info Certificate metadata; value is the Unix timestamp of the expiry date",
        "# TYPE cert_info gauge",
        *[
            f'cert_info{{{lbl(r, "subject_cn=\"" + _escape_label(r["subject_cn"]) + "\",issuer_cn=\"" + _escape_label(r["issuer_cn"]) + "\",not_after=\"" + r["not_after"] + "\"")}}}'
            f' {r["expiry_ts"]}'
            for r in results if r["check_success"]
        ],
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/cert/add")
def cert_add(
    url: str = Query(..., description="Full HTTPS URL, e.g. https://portal.crowngasandpower.co.uk"),
    name: str = Query(..., description="Human-readable name for dashboards and alerts"),
    team: str = Query(default="general", description="Owning team — used in alert routing labels"),
):
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="URL must start with https://")
    hostname = urllib.parse.urlparse(url).hostname or ""
    try:
        ipaddress.ip_address(hostname)
        raise HTTPException(status_code=400, detail="IP addresses are not permitted — use a fully-qualified hostname")
    except ValueError:
        pass
    if "." not in hostname:
        raise HTTPException(status_code=400, detail="URL must use a fully-qualified hostname (e.g. portal.crowngasandpower.co.uk)")
    with get_conn() as conn:
        conn.cursor().execute(
            """INSERT INTO cert_targets(url, name, team)
               VALUES (%s, %s, %s)
               ON CONFLICT(url) DO UPDATE SET name = EXCLUDED.name, team = EXCLUDED.team""",
            (url, name, team),
        )
    return _page(f"✓ <strong>{html.escape(name)}</strong> (<code>{html.escape(url)}</code>) added to certificate monitoring.")


@app.get("/cert/remove")
def cert_remove(url: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM cert_targets WHERE url = %s", (url,))
    return _page(f"✓ <code>{html.escape(url)}</code> removed from certificate monitoring.")


@app.get("/cert/scan", response_class=HTMLResponse)
def cert_scan():
    """Run all certificate checks immediately and return an HTML results page."""
    import datetime as _dt
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT url, name, team FROM cert_targets ORDER BY team, name")
        targets = [{"url": r[0], "name": r[1], "team": r[2]} for r in cur.fetchall()]

    if not targets:
        return HTMLResponse("<p>No certificate targets configured. <a href='/cert'>Add some</a>.</p>")

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(_check_one_cert, targets))

    now = _dt.datetime.now(_dt.timezone.utc)

    def _ocsp_label(v):
        return {0: ("Good", "#2e7d32"), 1: ("Revoked", "#c62828"), 2: ("Unknown", "#e65100")}.get(v, ("Error", "#f9a825"))

    def _days_color(days):
        if days < 7:   return "#c62828"
        if days < 30:  return "#e65100"
        return "#2e7d32"

    rows_html = ""
    for r in results:
        if not r["check_success"]:
            rows_html += (
                f"<tr style='background:#fff3e0'>"
                f"<td>{html.escape(r['name'])}</td><td>{html.escape(r['team'])}</td>"
                f"<td><code>{html.escape(r['url'])}</code></td>"
                f"<td colspan='5' style='color:#c62828'>&#x2715; TLS connection failed</td>"
                f"</tr>"
            )
            continue

        days = (r["expiry_ts"] - now.timestamp()) / 86400
        days_str = f"{days:.1f}d"
        days_color = _days_color(days)
        sha1_cell = ("<span style='color:#e65100'>&#x26A0; SHA-1</span>" if r["has_sha1"]
                     else "<span style='color:#2e7d32'>&#x2713; OK</span>")
        ocsp_text, ocsp_color = _ocsp_label(r["ocsp_status"])

        rows_html += (
            f"<tr>"
            f"<td>{html.escape(r['name'])}</td>"
            f"<td>{html.escape(r['team'])}</td>"
            f"<td><code>{html.escape(r['url'])}</code></td>"
            f"<td>{html.escape(r['subject_cn'])}</td>"
            f"<td>{html.escape(r['issuer_cn'])}</td>"
            f"<td>{html.escape(r['not_after'])}</td>"
            f"<td style='color:{days_color};font-weight:bold'>{html.escape(days_str)}</td>"
            f"<td style='color:{ocsp_color}'>{ocsp_text}</td>"
            f"<td>{sha1_cell}</td>"
            f"</tr>"
        )

    scanned_at = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Certificate Scan Results</title>
<style>
  body {{ font-family: sans-serif; padding: 2rem; max-width: 1200px }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.3rem }}
  .meta {{ font-size: 0.85rem; color: #666; margin-bottom: 1.5rem }}
  table {{ border-collapse: collapse; width: 100% }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left }}
  th {{ background: #f4f4f4 }}
  code {{ font-size: 0.85em; background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 2px }}
  a {{ color: #1a73e8 }}
</style>
</head>
<body>
<p><a href="/cert">← Back to Certificate Targets</a></p>
<h1>Certificate Scan Results</h1>
<p class="meta">Scanned {len(results)} target(s) at {scanned_at} &nbsp;|&nbsp;
<a href="/cert/scan">Scan again</a></p>
<table>
  <thead>
    <tr>
      <th>Name</th><th>Team</th><th>URL</th><th>Subject</th><th>Issuer</th>
      <th>Expires On</th><th>Days Left</th><th>OCSP</th><th>Signature</th>
    </tr>
  </thead>
  <tbody>{rows_html}</tbody>
</table>
</body>
</html>""")


@app.get("/cert", response_class=HTMLResponse)
def cert_list():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT url, name, team, added_at FROM cert_targets ORDER BY team, name")
        rows = cur.fetchall()

    rows_html = "".join(
        f"<tr>"
        f"<td>{html.escape(r[1])}</td>"
        f"<td>{html.escape(r[2])}</td>"
        f"<td><code>{html.escape(r[0])}</code></td>"
        f"<td>{r[3].strftime('%Y-%m-%d %H:%M') if r[3] else ''}</td>"
        f"<td>"
        f"<a href='#add-form' onclick=\"prefill('{_js(r[0])}','{_js(r[1])}','{_js(r[2])}')\">Edit</a>"
        f" &nbsp; "
        f"<a href='/cert/remove?url={urllib.parse.quote(r[0])}' "
        f"onclick=\"return confirm('Remove {_js(r[1])}?')\">Remove</a>"
        f"</td>"
        f"</tr>"
        for r in rows
    ) or "<tr><td colspan='5' style='color:#888'>No certificate targets configured</td></tr>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
<title>Certificate Monitoring</title>
<style>
  body {{ font-family: sans-serif; padding: 2rem; max-width: 1000px }}
  h1 {{ font-size: 1.4rem; margin-bottom: 1rem }}
  h2 {{ font-size: 1.1rem; margin: 2rem 0 0.5rem }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left }}
  th {{ background: #f4f4f4 }}
  code {{ font-size: 0.85em; background: #f0f0f0; padding: 0.1rem 0.3rem; border-radius: 2px }}
  a {{ color: #c00 }}
  form {{ display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: flex-end; margin-bottom: 1rem }}
  label {{ display: flex; flex-direction: column; font-size: 0.85rem; gap: 0.2rem }}
  input {{ padding: 0.3rem 0.5rem; border: 1px solid #ccc; border-radius: 3px }}
  button {{ padding: 0.35rem 0.9rem; background: #1a73e8; color: #fff; border: none; border-radius: 3px; cursor: pointer }}
  button:hover {{ background: #1558b0 }}
  .hint {{ font-size: 0.8rem; color: #666; margin-top: 0.2rem }}
  .legend {{ background: #f8f8f8; border: 1px solid #ddd; padding: 0.8rem 1rem; margin-bottom: 1.5rem; font-size: 0.9rem; border-radius: 3px }}
</style>
</head>
<body>
<p><a href="javascript:history.back()">← Back</a></p>
<h1>Certificate Monitoring</h1>
<div class="legend">
  HTTPS endpoints added here are probed every 5 minutes by the blackbox exporter (TLS expiry)
  and the exemption API (OCSP revocation, chain signature). Changes take effect within 1 minute.
  Alerts fire at &lt;30 days (warning) and &lt;3 days (PagerDuty critical).<br><br>
  <a href="/cert/scan" style="display:inline-block;padding:0.35rem 0.9rem;background:#1a73e8;color:#fff;border-radius:3px;text-decoration:none;font-size:0.9rem">&#9654; Scan Now</a>
  <span style="font-size:0.8rem;color:#666;margin-left:0.6rem">Runs all checks immediately and shows results — takes up to ~30s</span>
</div>

<table>
  <thead><tr><th>Name</th><th>Team</th><th>URL</th><th>Added</th><th></th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<h2 id="add-form">Add / update target</h2>
<form method="get" action="/cert/add">
  <label>HTTPS URL
    <input id="f-url" name="url" required placeholder="https://portal.crowngasandpower.co.uk" size="45" type="url">
    <span class="hint">Full URL including https://</span>
  </label>
  <label>Name
    <input id="f-name" name="name" required placeholder="e.g. Customer Portal" size="28">
  </label>
  <label>Team
    <input id="f-team" name="team" value="general" size="15">
    <span class="hint">Used in alert routing</span>
  </label>
  <button type="submit">Save</button>
</form>
<script>
function prefill(url, name, team) {{
  document.getElementById('f-url').value = url;
  document.getElementById('f-name').value = name;
  document.getElementById('f-team').value = team;
}}
</script>
</body>
</html>""")


# ── vCenter Service Discovery ────────────────────────────────────────────────
# Reads the SD files written by the vcenter-sd container and exposes them as
# Prometheus HTTP SD endpoints. AWS Prometheus polls these so it can scrape
# dynamically-discovered hosts through the metrics proxy without hardcoding
# any static targets.

def _vcenter_sd_response(sd_file: str):
    try:
        with open(sd_file) as f:
            entries = json.load(f)
    except FileNotFoundError:
        return JSONResponse([])
    result = []
    for entry in entries:
        for raw_target in entry.get("targets", []):
            # Strip the port — the proxy path encodes the host only; nginx appends the exporter port
            host = raw_target.rsplit(":", 1)[0]
            result.append({"targets": [host], "labels": entry.get("labels", {})})
    return JSONResponse(result)


@app.get("/vcenter/linux-targets")
def vcenter_linux_targets():
    """Prometheus HTTP SD — Linux node exporter targets discovered by vcenter-sd."""
    return _vcenter_sd_response(os.path.join(SD_DIR, "linux-hosts.json"))


@app.get("/vcenter/windows-targets")
def vcenter_windows_targets():
    """Prometheus HTTP SD — Windows exporter targets discovered by vcenter-sd."""
    return _vcenter_sd_response(os.path.join(SD_DIR, "windows-hosts.json"))


# ── UniFi Device Exemptions ───────────────────────────────────────────────────
# Devices added here are excluded from the network-unifi-offline alert and
# hidden from the Device Status table in the UniFi Site Detail dashboard.
# Prometheus scrapes /unifi/metrics to build the unifi_device_exempt series.

@app.get("/unifi/metrics")
def unifi_metrics():
    """Prometheus text format — one gauge per exempted device and per exempted console."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT device FROM unifi_exempt ORDER BY device")
        device_rows = cur.fetchall()
        cur.execute("SELECT site FROM unifi_console_exempt ORDER BY site")
        console_rows = cur.fetchall()
    lines = [
        "# HELP unifi_device_exempt 1 if this UniFi device is suppressed from offline alerts and status",
        "# TYPE unifi_device_exempt gauge",
    ]
    for (device,) in device_rows:
        lines.append(f'unifi_device_exempt{{device="{_escape_label(device)}"}} 1')
    lines += [
        "# HELP unifi_console_exempt 1 if this UniFi console is suppressed from the console-offline alert",
        "# TYPE unifi_console_exempt gauge",
    ]
    for (site,) in console_rows:
        lines.append(f'unifi_console_exempt{{site="{_escape_label(site)}",host="{_escape_label(site)}"}} 1')
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/unifi/add")
def unifi_add(device: str = Query(...), reason: str = Query(default="")):
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO unifi_exempt(device, reason) VALUES (%s, %s) ON CONFLICT(device) DO NOTHING",
            (device, reason),
        )
    return _page(f"✓ <strong>{html.escape(device)}</strong> exempted from UniFi offline alerts.")


@app.get("/unifi/remove")
def unifi_remove(device: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM unifi_exempt WHERE device = %s", (device,))
    return _page(f"✓ <strong>{html.escape(device)}</strong> removed from UniFi exemptions.")


@app.get("/unifi/console/add")
def unifi_console_add(site: str = Query(...), reason: str = Query(default="")):
    with get_conn() as conn:
        conn.cursor().execute(
            "INSERT INTO unifi_console_exempt(site, reason) VALUES (%s, %s) ON CONFLICT(site) DO NOTHING",
            (site, reason),
        )
    return _page(f"✓ Console <strong>{html.escape(site)}</strong> exempted from UniFi console offline alerts.")


@app.get("/unifi/console/remove")
def unifi_console_remove(site: str = Query(...)):
    with get_conn() as conn:
        conn.cursor().execute("DELETE FROM unifi_console_exempt WHERE site = %s", (site,))
    return _page(f"✓ Console <strong>{html.escape(site)}</strong> removed from UniFi console exemptions.")


@app.get("/unifi", response_class=HTMLResponse)
def unifi_list():
    return RedirectResponse(url="/suppress", status_code=301)
