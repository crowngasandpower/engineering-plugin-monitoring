"""Automated error diagnosis service for Crown monitoring.

Triggered by a Grafana alert when the Laravel error rate exceeds a
threshold. Gathers context from Loki, Prometheus, MySQL, and GitHub,
sends it to Claude for diagnosis, then creates or updates a Jira
ticket and notifies Teams.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import zoneinfo
from datetime import datetime, timezone, timedelta

import httpx
import pymysql
from fastapi import FastAPI, BackgroundTasks, Request, Response
from fastapi.responses import HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("error-diagnosis")

app = FastAPI(title="Crown Error Diagnosis", version="0.1.0")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY_DIAGNOSIS", "")
ANTHROPIC_API_KEY_INVESTIGATE = os.environ.get("ANTHROPIC_API_KEY_INVESTIGATE", "")
CLAUDE_MODEL = os.environ.get("DIAGNOSIS_CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = 4096

JIRA_CLOUD_ID = os.environ.get("JIRA_CLOUD_ID", "d8ec9f9f-df71-41ba-b57d-e417bcf37617")
JIRA_BASE_URL = f"https://api.atlassian.com/ex/jira/{JIRA_CLOUD_ID}"
JIRA_SITE_URL = os.environ.get(
    "JIRA_SITE_URL",
    "https://crowngasandpower-team-delivery.atlassian.net",
)
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
JIRA_PROJECT = os.environ.get("JIRA_PROJECT", "CT")

MYSQL_HOST = os.environ.get("MYSQL_HOST", "")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER", "")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_ORG = os.environ.get("GITHUB_ORG", "crowngasandpower")

TEAMS_WEBHOOK_URL = os.environ.get("DIAGNOSIS_TEAMS_WEBHOOK_URL", "")

COOLDOWN_HOURS = int(os.environ.get("DIAGNOSIS_COOLDOWN_HOURS", "4"))

BUSINESS_HOURS_START = int(os.environ.get("DIAGNOSIS_BUSINESS_HOURS_START", "7"))
BUSINESS_HOURS_END = int(os.environ.get("DIAGNOSIS_BUSINESS_HOURS_END", "19"))

def _is_business_hours() -> bool:
    """True if current UK local time is Mon-Fri between configured hours."""
    try:
        now = datetime.now(zoneinfo.ZoneInfo("Europe/London"))
    except Exception:
        now = datetime.now(timezone.utc)
    return now.weekday() < 5 and BUSINESS_HOURS_START <= now.hour < BUSINESS_HOURS_END

# Teams channel polling (Graph API)
TEAMS_TENANT_ID = os.environ.get("TEAMS_TENANT_ID", "")
TEAMS_CLIENT_ID = os.environ.get("TEAMS_CLIENT_ID", "")
TEAMS_CLIENT_SECRET = os.environ.get("TEAMS_CLIENT_SECRET", "")
TEAMS_TEAM_ID = os.environ.get("TEAMS_TEAM_ID", "")
TEAMS_CHANNEL_ID = os.environ.get("TEAMS_CHANNEL_ID", "")
TEAMS_POLL_INTERVAL = int(os.environ.get("TEAMS_POLL_INTERVAL", "15"))
TEAMS_REPLY_WEBHOOK_URL = os.environ.get("TEAMS_REPLY_WEBHOOK_URL", "")

MAX_LOG_LINES_PER_APP = 50
MAX_PROMPT_CHARS = 100_000

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

cooldown_map: dict[str, float] = {}
history: list[dict] = []

# ---------------------------------------------------------------------------
# Loki helpers
# ---------------------------------------------------------------------------

LOKI_EXCLUDE = '!= "Serialization failure: 1213 Deadlock"'
LOKI_ERROR_FILTER = '|~ "(?i)error|exception|fatal"'


async def query_loki_error_counts(client: httpx.AsyncClient) -> dict[str, int]:
    now = int(time.time())
    params = {
        "query": f'sum by (app) (count_over_time({{log_type="laravel"}} {LOKI_ERROR_FILTER} {LOKI_EXCLUDE} [5m]))',
        "start": str(now - 300),
        "end": str(now),
        "step": "300",
    }
    resp = await client.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=15)
    resp.raise_for_status()
    counts: dict[str, int] = {}
    for result in resp.json().get("data", {}).get("result", []):
        app_name = result.get("metric", {}).get("app", "unknown")
        for _, val in result.get("values", []):
            counts[app_name] = max(counts.get(app_name, 0), int(float(val)))
    return counts


async def query_loki_error_lines(
    client: httpx.AsyncClient, app_name: str, limit: int = 200,
    lookback_seconds: int = 900,
) -> list[str]:
    now = int(time.time())
    params = {
        "query": f'{{app="{app_name}", log_type="laravel"}} {LOKI_ERROR_FILTER} {LOKI_EXCLUDE}',
        "start": str((now - lookback_seconds)) + "000000000",
        "end": str(now) + "000000000",
        "limit": str(limit),
        "direction": "backward",
    }
    resp = await client.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=15)
    resp.raise_for_status()
    lines: list[str] = []
    for stream in resp.json().get("data", {}).get("result", []):
        for _, line in stream.get("values", []):
            lines.append(line[:500])
    return _deduplicate_lines(lines, MAX_LOG_LINES_PER_APP)


def _deduplicate_lines(lines: list[str], max_lines: int) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        normalised = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "TIMESTAMP", line)
        normalised = re.sub(r"\b\d{5,}\b", "ID", normalised)
        if normalised not in seen:
            seen.add(normalised)
            unique.append(line)
        if len(unique) >= max_lines:
            break
    return unique


# ---------------------------------------------------------------------------
# Prometheus helpers
# ---------------------------------------------------------------------------

async def query_prometheus(client: httpx.AsyncClient, expr: str) -> list[dict]:
    resp = await client.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": expr},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("data", {}).get("result", [])


async def gather_prometheus_metrics(client: httpx.AsyncClient) -> str:
    queries = {
        "App health (probe_success == 0)": "probe_success == 0",
        "MySQL connections": "mysql_global_status_threads_connected",
        "MySQL max connections": "mysql_global_variables_max_connections",
        "InnoDB deadlock rate (5m)": "rate(mysql_info_schema_innodb_metrics_lock_lock_deadlocks_total[5m])",
        "Genus replica lag (seconds)": "max(genus_replica_lag_seconds)",
        "EPS pricing queue depth": "eps_pricing_queue_depth",
        "EPS oldest in queue (seconds)": "eps_pricing_oldest_in_queue_seconds",
    }
    sections: list[str] = []
    for label, expr in queries.items():
        try:
            results = await query_prometheus(client, expr)
            if results:
                values = ", ".join(
                    f"{r.get('metric', {}).get('app', r.get('metric', {}).get('instance', '?'))}={r['value'][1]}"
                    for r in results
                )
                sections.append(f"- {label}: {values}")
            else:
                sections.append(f"- {label}: (no data)")
        except Exception:
            sections.append(f"- {label}: (query failed)")
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# MySQL helpers
# ---------------------------------------------------------------------------

def query_mysql_processlist() -> str:
    if not MYSQL_HOST or not MYSQL_USER:
        return "(MySQL not configured)"
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            connect_timeout=5,
            read_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, user, host, db, command, time, state, LEFT(info, 200) "
                "FROM information_schema.processlist "
                "WHERE command != 'Sleep' ORDER BY time DESC LIMIT 20"
            )
            rows = cur.fetchall()
        conn.close()
        if not rows:
            return "(no active queries)"
        lines = ["id | user | db | command | time | state | query"]
        for r in rows:
            lines.append(f"{r[0]} | {r[1]} | {r[3]} | {r[4]} | {r[5]}s | {r[6]} | {(r[7] or '')[:120]}")
        return "\n".join(lines)
    except Exception as e:
        return f"(MySQL query failed: {e})"


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

async def fetch_recent_commits(client: httpx.AsyncClient, app_names: list[str]) -> str:
    if not GITHUB_TOKEN:
        return "(GitHub token not configured)"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    sections: list[str] = []
    for app_name in app_names[:5]:
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{GITHUB_ORG}/{app_name}/commits",
                params={"since": since, "sha": "main", "per_page": 10},
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            commits = resp.json()
            if not commits:
                continue
            lines = [f"### {app_name}"]
            for c in commits:
                sha = c["sha"][:7]
                msg = c["commit"]["message"].split("\n")[0][:80]
                author = c["commit"]["author"]["name"]
                lines.append(f"  {sha} {msg} ({author})")
            sections.append("\n".join(lines))
        except Exception:
            continue
    return "\n".join(sections) if sections else "(no recent commits found)"


# ---------------------------------------------------------------------------
# Claude helpers
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an error diagnosis assistant for Crown Gas & Power's application estate.
You are given application error logs, infrastructure metrics, MySQL process state,
and optionally recent code changes. Your job is to identify the most likely root
cause and recommend immediate actions.

Crown's estate is ~40 Laravel applications running on two production servers
(apps-prod-1, apps-prod-2) with a shared MySQL database on apps-prod-mysql.
Common failure modes include:
- MySQL deadlocks (InnoDB row-level locking contention, especially in batch jobs)
- MySQL connection exhaustion (max_connections reached, often from queue workers)
- Genus SQL Server replica lag causing stale reads
- EPS pricing queue backup (Redis queue depth > 1000)
- Filesystem mount failures (NFS docstore or HubFiles becoming unavailable)
- Deploy-related regressions (errors starting within minutes of a deployment)
- Memory exhaustion in long-running Horizon queue workers
- Missing or misconfigured Composer dependencies
- Missing mix-manifest entries causing cascading view errors
- Scheduled command failures from schema or data changes

When multiple apps error simultaneously, look for a shared root cause (database,
filesystem, network) rather than treating each app independently.

Respond with ONLY a JSON object — no markdown, no extra text:
{
  "summary_one_liner": "Short overall description for parent ticket title",
  "root_cause": "One paragraph: is this a shared infrastructure issue or independent per-app failures?",
  "confidence": "high|medium|low",
  "severity": "critical|high|medium|low",
  "per_app": {
    "app_name": {
      "summary": "Short one-line description of this app's specific issue",
      "root_cause": "What is wrong in this specific app",
      "immediate_actions": ["Action 1 specific to this app"],
      "investigation_steps": ["Step 1 if actions don't resolve it"]
    }
  }
}

The "per_app" object MUST have one entry per affected application. Each entry
should be independently actionable — a developer assigned to that app's ticket
should be able to act on it without reading the other apps' entries."""


async def call_claude(client: httpx.AsyncClient, user_message: str) -> dict | None:
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY_DIAGNOSIS not set, skipping Claude call")
        return None
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message[:MAX_PROMPT_CHARS]}],
    }
    try:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json().get("content", [{}])[0].get("text", "")
        # Claude sometimes wraps JSON in markdown code fences
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        return json.loads(content)
    except (httpx.HTTPError, json.JSONDecodeError, IndexError, KeyError) as e:
        log.error("Claude API call failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Jira helpers
# ---------------------------------------------------------------------------

def _jira_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {JIRA_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _extract_error_signature(app_name: str, log_lines: list[str]) -> str:
    for line in log_lines[:10]:
        match = re.search(r"([\w\\]+(?:Exception|Error))", line)
        if match:
            return f"{app_name}::{match.group(1)}"
    if log_lines:
        first = re.sub(r"\[[\d\-: ]+\]\s*\w+\.ERROR:\s*", "", log_lines[0])
        first = re.sub(r"\d+", "N", first)[:80]
        return f"{app_name}::{first}"
    return app_name


async def search_existing_ticket(
    client: httpx.AsyncClient, app_name: str, error_keyword: str
) -> dict | None:
    if not JIRA_API_TOKEN:
        return None
    keyword = re.sub(r'["\[\]\\]', "", error_keyword)[:60]
    jql = (
        f'project = {JIRA_PROJECT} AND statusCategory != Done '
        f'AND summary ~ "{app_name}" AND text ~ "{keyword}"'
    )
    try:
        resp = await client.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=_jira_headers(),
            json={"jql": jql, "maxResults": 5, "fields": ["summary", "status", "created"]},
            timeout=15,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        return issues[0] if issues else None
    except Exception as e:
        log.error("Jira search failed: %s", e)
        return None


async def create_jira_ticket(
    client: httpx.AsyncClient,
    summary: str,
    description: str,
    labels: list[str],
    parent_key: str | None = None,
) -> str | None:
    if not JIRA_API_TOKEN:
        return None
    issue_type = "Subtask" if parent_key else "Bug"
    fields: dict = {
        "project": {"key": JIRA_PROJECT},
        "issuetype": {"name": issue_type},
        "summary": summary[:255],
        "description": {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description[:30000]}],
                }
            ],
        },
        "labels": labels,
    }
    if parent_key:
        fields["parent"] = {"key": parent_key}
    payload = {"fields": fields}
    try:
        resp = await client.post(
            f"{JIRA_BASE_URL}/rest/api/3/issue",
            headers=_jira_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        key = resp.json().get("key", "")
        log.info("Created Jira ticket: %s", key)
        return key
    except Exception as e:
        log.error("Jira ticket creation failed: %s", e)
        return None


async def add_jira_comment(client: httpx.AsyncClient, key: str, body: str) -> None:
    try:
        await client.post(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/comment",
            headers=_jira_headers(),
            json={
                "body": {
                    "version": 1,
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": body[:30000]}],
                        }
                    ],
                }
            },
            timeout=15,
        )
        log.info("Added comment to %s", key)
    except Exception as e:
        log.error("Jira comment failed: %s", e)


# ---------------------------------------------------------------------------
# Teams helpers
# ---------------------------------------------------------------------------

async def send_teams_notification(
    client: httpx.AsyncClient,
    summary: str,
    diagnosis: dict,
    jira_key: str | None,
    child_keys: dict[str, str],
    error_count: int,
) -> None:
    if not TEAMS_WEBHOOK_URL:
        return
    jira_link = f"{JIRA_SITE_URL}/browse/{jira_key}" if jira_key else "N/A"
    severity = diagnosis.get("severity", "unknown")
    root_cause = diagnosis.get("root_cause", "Could not determine")
    per_app = diagnosis.get("per_app", {})

    tickets_section = ""
    for app_name, key in child_keys.items():
        app_diag = per_app.get(app_name, {})
        app_summary = app_diag.get("summary", app_name)
        tickets_section += f"- [{key}]({JIRA_SITE_URL}/browse/{key}) {app_name}: {app_summary}\n"

    text = (
        f"**Error Spike Diagnosed** ({error_count} errors in 5 min)\n\n"
        f"**Severity:** {severity}\n"
        f"**Overall:** {root_cause}\n\n"
        f"**Tickets:**\n{tickets_section}\n"
        f"**Parent:** {jira_link}"
    )
    payload = {"type": "message", "attachments": [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": f"Error Spike: {summary}", "weight": "Bolder", "size": "Medium"},
                    {"type": "TextBlock", "text": text, "wrap": True},
                ],
            },
        }
    ]}
    try:
        await client.post(TEAMS_WEBHOOK_URL, json=payload, timeout=15)
    except Exception as e:
        log.error("Teams notification failed: %s", e)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_diagnosis_pipeline(force: bool = False) -> dict:
    result: dict = {"timestamp": datetime.now(timezone.utc).isoformat(), "status": "started"}

    async with httpx.AsyncClient() as client:
        # 1. Query Loki for error counts by app
        try:
            error_counts = await query_loki_error_counts(client)
        except Exception as e:
            log.error("Loki query failed, aborting: %s", e)
            result["status"] = "failed"
            result["error"] = f"Loki unavailable: {e}"
            _store_history(result)
            return result

        total_errors = sum(error_counts.values())
        affected_apps = sorted(error_counts.keys(), key=lambda k: error_counts[k], reverse=True)
        significant_apps = [a for a in affected_apps if error_counts[a] > 5]

        if not significant_apps:
            result["status"] = "no_significant_errors"
            _store_history(result)
            return result

        # Cooldown check
        cooldown_key = "|".join(significant_apps[:3])
        if not force and cooldown_key in cooldown_map:
            elapsed = time.time() - cooldown_map[cooldown_key]
            if elapsed < COOLDOWN_HOURS * 3600:
                result["status"] = "cooldown"
                result["cooldown_remaining_minutes"] = round((COOLDOWN_HOURS * 3600 - elapsed) / 60)
                _store_history(result)
                return result
        cooldown_map[cooldown_key] = time.time()

        log.info("Diagnosing error spike: %d total errors, apps: %s", total_errors, significant_apps)

        # 2. Fetch log lines per app
        app_logs: dict[str, list[str]] = {}
        for app_name in significant_apps[:8]:
            try:
                app_logs[app_name] = await query_loki_error_lines(client, app_name)
            except Exception:
                app_logs[app_name] = ["(failed to fetch logs)"]

        # 3. Prometheus metrics
        try:
            metrics_text = await gather_prometheus_metrics(client)
        except Exception:
            metrics_text = "(Prometheus unavailable)"

        # 4. MySQL processlist
        processlist = await asyncio.to_thread(query_mysql_processlist)

        # 5. GitHub recent commits
        try:
            commits_text = await fetch_recent_commits(client, significant_apps)
        except Exception:
            commits_text = "(GitHub unavailable)"

        # 6. Build Claude prompt
        logs_section = ""
        for app_name in significant_apps[:8]:
            lines = app_logs.get(app_name, [])
            logs_section += f"\n### {app_name} ({error_counts[app_name]} errors)\n"
            logs_section += "\n".join(lines[:MAX_LOG_LINES_PER_APP]) + "\n"

        user_message = (
            f"## Error Spike Detected\n"
            f"Triggered at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"Total errors in 5-minute window: {total_errors}\n\n"
            f"## Error Logs by Application\n{logs_section}\n"
            f"## Infrastructure Metrics\n{metrics_text}\n\n"
            f"## MySQL Process List\n{processlist}\n\n"
            f"## Recent Commits (last 24h)\n{commits_text}\n"
        )

        # 7. Call Claude
        diagnosis = await call_claude(client, user_message)
        if not diagnosis:
            diagnosis = {
                "root_cause": "Automated diagnosis unavailable — Claude API call failed. Manual investigation required.",
                "confidence": "low",
                "severity": "unknown",
                "summary_one_liner": f"Error spike across {', '.join(significant_apps[:3])}",
                "per_app": {
                    app: {
                        "summary": f"Errors in {app} — manual investigation required",
                        "root_cause": "Claude API unavailable",
                        "immediate_actions": ["Check Loki logs manually"],
                        "investigation_steps": [],
                    }
                    for app in significant_apps
                },
            }

        result["diagnosis"] = diagnosis
        result["total_errors"] = total_errors
        result["affected_apps"] = significant_apps

        # 8. Jira — parent ticket + per-app child tickets
        per_app = diagnosis.get("per_app", {})
        parent_key = None
        child_keys: dict[str, str] = {}

        parent_description = (
            f"Automated error diagnosis triggered at {result['timestamp']}.\n\n"
            f"Total errors: {total_errors}\n"
            f"Affected apps: {', '.join(significant_apps)}\n"
            f"Confidence: {diagnosis.get('confidence', 'unknown')}\n\n"
            f"Overall assessment:\n{diagnosis.get('root_cause', 'Unknown')}\n\n"
            f"Per-app child tickets are linked below."
        )

        # Check if a parent ticket already exists for this spike
        existing = await search_existing_ticket(
            client, "Auto-Diagnosis", diagnosis.get("summary_one_liner", "error spike")
        )
        if existing:
            parent_key = existing.get("key")
            await add_jira_comment(
                client, parent_key,
                f"Error spike re-occurred at {result['timestamp']}. "
                f"{total_errors} errors across {', '.join(significant_apps)}.\n\n"
                f"Assessment: {diagnosis.get('root_cause', 'N/A')}"
            )
            result["jira_action"] = "commented"
        else:
            parent_summary = f"[Auto-Diagnosis] {diagnosis.get('summary_one_liner', 'Error spike')}"
            parent_key = await create_jira_ticket(
                client, parent_summary, parent_description, ["auto-diagnosis"]
            )
            result["jira_action"] = "created"

        # Create a child ticket per affected app
        if parent_key:
            for app_name in significant_apps:
                app_diag = per_app.get(app_name, {})
                app_lines = app_logs.get(app_name, [])
                error_sig = _extract_error_signature(app_name, app_lines)
                error_keyword = error_sig.split("::")[-1] if "::" in error_sig else app_name

                # Check for existing ticket for this app's error
                existing_child = await search_existing_ticket(client, app_name, error_keyword)
                if existing_child:
                    child_keys[app_name] = existing_child["key"]
                    await add_jira_comment(
                        client, existing_child["key"],
                        f"Re-occurred at {result['timestamp']} (parent: {parent_key}).\n\n"
                        f"Diagnosis: {app_diag.get('root_cause', 'N/A')}"
                    )
                    continue

                child_desc = (
                    f"Parent ticket: {parent_key}\n\n"
                    f"App: {app_name}\n"
                    f"Errors in window: {error_counts.get(app_name, 0)}\n\n"
                    f"Root cause:\n{app_diag.get('root_cause', 'Unknown')}\n\n"
                    f"Immediate actions:\n" +
                    "\n".join(f"- {a}" for a in app_diag.get("immediate_actions", [])) +
                    "\n\nInvestigation steps:\n" +
                    "\n".join(f"- {s}" for s in app_diag.get("investigation_steps", [])) +
                    "\n\nSample errors:\n" +
                    "\n".join(app_lines[:10])
                )
                child_summary = f"[Auto-Diagnosis] {app_name}: {app_diag.get('summary', error_sig)}"
                child_key = await create_jira_ticket(
                    client, child_summary, child_desc, ["auto-diagnosis"],
                    parent_key=parent_key,
                )
                if child_key:
                    child_keys[app_name] = child_key

        result["jira_key"] = parent_key
        result["jira_children"] = child_keys

        # 9. Teams notification
        await send_teams_notification(
            client,
            diagnosis.get("summary_one_liner", "Error spike detected"),
            diagnosis,
            parent_key,
            child_keys,
            total_errors,
        )

        result["status"] = "completed"
        log.info("Diagnosis complete: %s (Jira: %s, children: %s)", result["status"], parent_key, child_keys)

    _store_history(result)
    return result


def _store_history(result: dict) -> None:
    history.insert(0, result)
    if len(history) > 20:
        history.pop()


# ---------------------------------------------------------------------------
# Investigation helpers (user-reported issues)
# ---------------------------------------------------------------------------

KNOWN_APPS = [
    "eps", "gps", "ces", "ces-elec", "billy", "dds", "dds-elec",
    "econtracts", "econtracts-power", "ewelcome", "ewelcome-power",
    "gateway", "gateway-elec", "synergy", "esema", "esema2",
    "meter-data-manager", "doc-master", "doc-master-elec",
    "complaintstracker", "portal-gas", "portal-power",
    "portal-gas-admin", "portal-power-admin", "jigsaw", "jigsaw-elec",
    "pims", "pims-elec", "sanctions", "reporting", "link",
    "multisite-manager", "cva-dataflow-validation",
]

# Map quote ref prefixes and keywords to apps
APP_HINTS = {
    "eps": ["eps", "mpan", "electricity", "elec pricing", "hh data", "half hourly", "kva", "duos", "measurement_class"],
    "gps": ["gps", "mprn", "gas pricing", "gas quote"],
    "ces": ["ces", "customer experience", "nomination"],
    "ces-elec": ["ces-elec", "ces elec"],
    "synergy": ["synergy", "crm"],
}


def detect_app(message: str) -> str | None:
    msg_lower = message.lower()
    for app_name in KNOWN_APPS:
        if app_name in msg_lower:
            return app_name
    for app_name, keywords in APP_HINTS.items():
        for kw in keywords:
            if kw in msg_lower:
                return app_name
    if re.search(r"Q\d{8}", message):
        return "eps"
    return None


def extract_identifiers(message: str) -> dict:
    ids: dict[str, list[str]] = {}
    quote_refs = re.findall(r"Q\d{8}", message)
    if quote_refs:
        ids["quote_refs"] = quote_refs
    mpans = re.findall(r"\b\d{13}\b", message)
    if mpans:
        ids["mpans"] = mpans
    mprns = re.findall(r"\b\d{10}\b", message)
    if mprns:
        ids["mprns"] = mprns
    return ids


def query_mysql_investigation(app_name: str | None, identifiers: dict) -> str:
    if not MYSQL_HOST or not MYSQL_USER:
        return "(MySQL not configured)"
    sections: list[str] = []
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            connect_timeout=5,
            read_timeout=10,
        )
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            for qref in identifiers.get("quote_refs", []):
                db = "eps" if app_name in ("eps", None) else "gps"
                try:
                    cur.execute(f"SELECT id, quote_ref, quote_status_id, created_at FROM {db}.quotes WHERE quote_ref = %s AND deleted_at IS NULL", (qref,))
                    quote = cur.fetchone()
                    if quote:
                        sections.append(f"Quote {qref} (db={db}): id={quote['id']}, status_id={quote['quote_status_id']}, created={quote['created_at']}")
                        cur.execute(
                            f"SELECT id, mpan, measurement_class, half_hourly, profile, kva, "
                            f"half_hourly_data_imported, standard_settlement_code, site_ref "
                            f"FROM {db}.sites WHERE quote_id = %s AND deleted_at IS NULL",
                            (quote["id"],),
                        )
                        sites = cur.fetchall()
                        for s in sites:
                            sections.append(f"  Site {s['id']}: mpan={s.get('mpan','?')}, mprn={s.get('mprn','?')}, "
                                            f"measurement_class={s.get('measurement_class','?')}, half_hourly={s.get('half_hourly','?')}, "
                                            f"profile={s.get('profile','?')}, kva={s.get('kva','?')}, "
                                            f"hh_imported={s.get('half_hourly_data_imported','?')}, ssc={s.get('standard_settlement_code','?')}")
                except Exception as e:
                    sections.append(f"Query for {qref} in {db} failed: {e}")

            for mpan in identifiers.get("mpans", []):
                try:
                    cur.execute(
                        "SELECT s.id, s.quote_id, q.quote_ref, s.measurement_class, s.half_hourly, s.profile, s.kva "
                        "FROM eps.sites s JOIN eps.quotes q ON q.id = s.quote_id "
                        "WHERE s.mpan = %s AND s.deleted_at IS NULL ORDER BY s.id DESC LIMIT 5",
                        (mpan,),
                    )
                    for row in cur.fetchall():
                        sections.append(f"  MPAN {mpan}: site={row['id']}, quote={row['quote_ref']}, "
                                        f"mc={row['measurement_class']}, hh={row['half_hourly']}, "
                                        f"profile={row['profile']}, kva={row['kva']}")
                except Exception:
                    pass

        conn.close()
    except Exception as e:
        return f"(MySQL query failed: {e})"
    return "\n".join(sections) if sections else "(no matching records found)"


async def search_jira_for_issue(client: httpx.AsyncClient, keywords: str) -> str:
    if not JIRA_API_TOKEN:
        return "(Jira not configured)"
    clean = re.sub(r'["\[\]\\]', "", keywords)[:100]
    jql = f'project = {JIRA_PROJECT} AND text ~ "{clean}" ORDER BY created DESC'
    try:
        resp = await client.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=_jira_headers(),
            json={"jql": jql, "maxResults": 10, "fields": ["summary", "status", "created"]},
            timeout=15,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        if not issues:
            return "(no matching Jira tickets found)"
        lines: list[str] = []
        for i in issues:
            key = i["key"]
            summary = i["fields"]["summary"]
            status = i["fields"]["status"]["name"]
            lines.append(f"- {key} [{status}]: {summary} ({JIRA_SITE_URL}/browse/{key})")
        return "\n".join(lines)
    except Exception as e:
        return f"(Jira search failed: {e})"


async def search_github_prs(client: httpx.AsyncClient, app_name: str, keywords: str) -> str:
    if not GITHUB_TOKEN:
        return "(GitHub token not configured)"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    query = f"repo:{GITHUB_ORG}/{app_name} is:pr {keywords}"
    try:
        resp = await client.get(
            "https://api.github.com/search/issues",
            params={"q": query, "sort": "updated", "order": "desc", "per_page": 10},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return f"(no matching PRs found in {app_name})"
        lines: list[str] = []
        for pr in items:
            number = pr["number"]
            title = pr["title"]
            state = pr["state"]
            url = pr["html_url"]
            lines.append(f"- #{number} [{state}]: {title} ({url})")
        return "\n".join(lines)
    except Exception as e:
        return f"(GitHub PR search failed: {e})"


INVESTIGATE_SYSTEM_PROMPT = """\
You are a support investigation assistant for Crown Gas & Power's application estate.
A user has reported an issue. You are given their message along with any relevant data
pulled from the application databases, recent logs, and infrastructure metrics.

Your job is to explain what is happening and why. Not every report is a bug — sometimes
the system is working as designed and the user needs an explanation.

IMPORTANT — scope restriction:
You are a DIAGNOSTIC tool, not a data retrieval tool. You may use commercial data
(prices, margins, commissions, customer details, contract values, broker information)
internally to diagnose a reported problem, but you must NOT:
- Return commercial data in your response unless it is directly necessary to explain
  the specific bug or misconfiguration being diagnosed
- Fulfil requests that are purely about retrieving data (e.g. "show me all renewals
  for broker X", "what prices did customer Y get", "list all quotes above £X")
- Summarise, list, or export commercial records

If a request is not about diagnosing a technical problem or explaining unexpected
system behaviour, respond with:
{"explanation": "I can only help diagnose technical issues and unexpected system behaviour. For data queries, please use the appropriate application directly.", "is_bug": false, "root_cause": "Not a diagnostic request", "affected_data": null, "fix": null, "severity": "info", "existing_ticket": null, "existing_pr": null}

When you DO reference commercial fields to explain a bug (e.g. "the margin is null
which causes the division-by-zero"), include only the specific field and value relevant
to the diagnosis — not surrounding commercial context.

Crown's estate is ~40 Laravel applications. Key apps:
- EPS (Electricity Pricing System): quotes, sites, MPANs, HH/NHH pricing, KVA/DUoS charges
- GPS (Gas Pricing System): quotes, sites, MPRNs
- CES/CES-Elec: customer experience, nominations, contracts

For EPS specifically:
- Sites have both a `half_hourly` boolean and a `measurement_class` field (A/B = NHH, C/D/E/F/G = HH)
- Profile 00 = HH, profiles 1-8 = NHH profile classes
- KVA charges depend on profile, GSP, line loss factor, and distributor
- Sites are routed to pricing-hh or pricing-nhh queues based on the half_hourly boolean
- The `halfHourlySites()` relationship filters on measurement_class, not the boolean

You will also be given any existing Jira tickets and GitHub PRs that may be related.
If an existing ticket or PR already covers this issue, say so — don't recommend creating
a duplicate.

Respond with a JSON object:
{
  "explanation": "Clear explanation of what is happening and why, written for the user who reported it",
  "is_bug": true/false,
  "root_cause": "Technical root cause if it's a bug, or 'Working as designed' with explanation",
  "affected_data": "What specific records/fields are wrong, if applicable",
  "fix": "What needs to be done — could be a data fix, code fix, or just user guidance",
  "severity": "critical|high|medium|low|info",
  "existing_ticket": "Jira ticket key if one already covers this, or null",
  "existing_pr": "PR number/URL if one addresses this, or null"
}"""


async def run_investigation(message: str) -> dict:
    result: dict = {"timestamp": datetime.now(timezone.utc).isoformat(), "message": message}

    app_name = detect_app(message)
    identifiers = extract_identifiers(message)
    result["detected_app"] = app_name
    result["identifiers"] = identifiers

    async with httpx.AsyncClient() as client:
        # Gather context
        db_context = await asyncio.to_thread(query_mysql_investigation, app_name, identifiers)

        log_context = ""
        if app_name:
            try:
                lines = await query_loki_error_lines(client, app_name, limit=50, lookback_seconds=14400)
                if lines:
                    log_context = f"Recent {app_name} errors:\n" + "\n".join(lines[:20])
            except Exception:
                log_context = "(log query failed)"

        metrics_text = ""
        try:
            metrics_text = await gather_prometheus_metrics(client)
        except Exception:
            pass

        # Search Jira for existing tickets
        search_terms = message[:80]
        jira_context = await search_jira_for_issue(client, search_terms)

        # Search GitHub for related PRs
        pr_context = ""
        if app_name and GITHUB_TOKEN:
            pr_keywords = " ".join(identifiers.get("quote_refs", []))
            if not pr_keywords:
                pr_keywords = re.sub(r"[^\w\s]", "", message)[:60]
            pr_context = await search_github_prs(client, app_name, pr_keywords)

        user_message = (
            f"## User Report\n{message}\n\n"
            f"## Database Records\n{db_context}\n\n"
        )
        if log_context:
            user_message += f"## Recent Application Logs ({app_name})\n{log_context}\n\n"
        if metrics_text:
            user_message += f"## Infrastructure Metrics\n{metrics_text}\n\n"
        user_message += f"## Existing Jira Tickets\n{jira_context}\n\n"
        if pr_context:
            user_message += f"## Related GitHub PRs ({app_name})\n{pr_context}\n"

        # Call Claude
        api_key = ANTHROPIC_API_KEY_INVESTIGATE or ANTHROPIC_API_KEY
        if not api_key:
            result["status"] = "failed"
            result["error"] = "No Anthropic API key configured for investigation"
            return result

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": CLAUDE_MAX_TOKENS,
            "system": INVESTIGATE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message[:MAX_PROMPT_CHARS]}],
        }
        try:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=120,
            )
            resp.raise_for_status()
            content = resp.json().get("content", [{}])[0].get("text", "")
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)
            result["diagnosis"] = json.loads(content)
            result["status"] = "completed"
        except Exception as e:
            log.error("Investigation Claude call failed: %s", e)
            result["status"] = "failed"
            result["error"] = str(e)

    _store_history(result)
    return result


# ---------------------------------------------------------------------------
# Teams channel polling (Microsoft Graph API)
# ---------------------------------------------------------------------------

_teams_token: dict = {"access_token": "", "expires_at": 0.0}
_teams_last_seen: str = ""


async def _get_graph_token(client: httpx.AsyncClient) -> str:
    now = time.time()
    if _teams_token["access_token"] and _teams_token["expires_at"] > now + 60:
        return _teams_token["access_token"]
    resp = await client.post(
        f"https://login.microsoftonline.com/{TEAMS_TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": TEAMS_CLIENT_ID,
            "client_secret": TEAMS_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _teams_token["access_token"] = data["access_token"]
    _teams_token["expires_at"] = now + data.get("expires_in", 3600)
    return _teams_token["access_token"]


def _graph_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _extract_plain_text(body: dict) -> str:
    content = body.get("content", "")
    if body.get("contentType") == "html":
        content = re.sub(r"<[^>]+>", "", content)
        content = re.sub(r"&nbsp;", " ", content)
        content = re.sub(r"&amp;", "&", content)
        content = re.sub(r"&lt;", "<", content)
        content = re.sub(r"&gt;", ">", content)
    return content.strip()


def _format_diagnosis_html(data: dict) -> str:
    diag = data.get("diagnosis", {})
    sev = diag.get("severity", "info")
    parts = [f"<b>[{sev.upper()}]</b>"]
    if diag.get("is_bug"):
        parts[0] += " - Bug"
    parts.append(f"<br/><br/>{diag.get('explanation', diag.get('root_cause', 'No diagnosis available'))}")
    if diag.get("affected_data"):
        parts.append(f"<br/><br/><b>Affected Data:</b><br/>{diag['affected_data']}")
    if diag.get("fix"):
        parts.append(f"<br/><br/><b>Fix:</b><br/>{diag['fix']}")
    if diag.get("existing_ticket"):
        url = f"{JIRA_SITE_URL}/browse/{diag['existing_ticket']}"
        parts.append(f'<br/><br/><b>Existing Ticket:</b> <a href="{url}">{diag["existing_ticket"]}</a>')
    if diag.get("existing_pr"):
        parts.append(f'<br/><br/><b>Existing PR:</b> <a href="{diag["existing_pr"]}">{diag["existing_pr"]}</a>')
    if data.get("detected_app"):
        parts.append(f"<br/><br/><i>App: {data['detected_app']}</i>")
    return "".join(parts)


async def _post_to_teams_webhook(client: httpx.AsyncClient, text: str) -> None:
    if not TEAMS_REPLY_WEBHOOK_URL:
        return
    payload = {"type": "message", "attachments": [
        {
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [{"type": "TextBlock", "text": text, "wrap": True}],
            },
        }
    ]}
    try:
        await client.post(TEAMS_REPLY_WEBHOOK_URL, json=payload, timeout=15)
    except Exception as e:
        log.error("Teams webhook post failed: %s", e)


def _format_diagnosis_card(sender: str, question: str, data: dict) -> str:
    diag = data.get("diagnosis", {})
    sev = diag.get("severity", "info")
    bug_tag = " - Bug" if diag.get("is_bug") else ""
    parts = [f"**[{sev.upper()}{bug_tag}]** Re: *{sender}*\n\n"]
    parts.append(f"> {question[:200]}\n\n")
    parts.append(diag.get("explanation", diag.get("root_cause", "No diagnosis available")))
    if diag.get("affected_data"):
        parts.append(f"\n\n**Affected Data:** {diag['affected_data']}")
    if diag.get("fix"):
        parts.append(f"\n\n**Fix:** {diag['fix']}")
    if diag.get("existing_ticket"):
        url = f"{JIRA_SITE_URL}/browse/{diag['existing_ticket']}"
        parts.append(f"\n\n**Existing Ticket:** [{diag['existing_ticket']}]({url})")
    if diag.get("existing_pr"):
        parts.append(f"\n\n**Existing PR:** [{diag['existing_pr']}]({diag['existing_pr']})")
    if data.get("detected_app"):
        parts.append(f"\n\n*App: {data['detected_app']}*")
    return "".join(parts)


async def _poll_teams_channel() -> None:
    global _teams_last_seen
    log.info("Teams channel polling started (interval: %ds)", TEAMS_POLL_INTERVAL)

    async with httpx.AsyncClient() as client:
        while True:
            try:
                token = await _get_graph_token(client)
                headers = _graph_headers(token)

                url = (
                    f"https://graph.microsoft.com/v1.0/teams/{TEAMS_TEAM_ID}"
                    f"/channels/{TEAMS_CHANNEL_ID}/messages"
                    f"?$top=10"
                )
                resp = await client.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                messages = resp.json().get("value", [])

                # Process new messages (newest first, stop at last seen)
                new_messages = []
                for msg in messages:
                    msg_id = msg.get("id", "")
                    if msg_id == _teams_last_seen:
                        break
                    msg_from = msg.get("from") or {}
                    if msg_from.get("application"):
                        continue
                    if msg.get("messageType") != "message":
                        continue
                    if not msg_from.get("user"):
                        continue
                    new_messages.append(msg)

                if messages:
                    _teams_last_seen = messages[0].get("id", _teams_last_seen)

                for msg in reversed(new_messages):
                    text = _extract_plain_text(msg.get("body", {}))
                    if not text:
                        continue

                    sender = (msg.get("from") or {}).get("user", {}).get("displayName", "Someone")
                    log.info("Teams message from %s: %s", sender, text[:100])

                    # Acknowledge
                    await _post_to_teams_webhook(
                        client,
                        f"Hi **{sender}**, I'm investigating this now...",
                    )

                    # Run investigation
                    try:
                        result = await run_investigation(text)
                        if result.get("status") == "completed":
                            reply_text = _format_diagnosis_card(sender, text, result)
                        else:
                            reply_text = f"**Investigation failed:** {result.get('error', 'Unknown error')}"
                    except Exception as e:
                        reply_text = f"**Investigation error:** {e}"

                    await _post_to_teams_webhook(client, reply_text)
                    log.info("Posted diagnosis for %s", sender)

            except Exception as e:
                log.error("Teams polling error: %s", e)

            await asyncio.sleep(TEAMS_POLL_INTERVAL)


@app.on_event("startup")
async def startup():
    if all([TEAMS_TENANT_ID, TEAMS_CLIENT_ID, TEAMS_CLIENT_SECRET, TEAMS_TEAM_ID, TEAMS_CHANNEL_ID]):
        asyncio.create_task(_poll_teams_channel())
        log.info("Teams channel polling enabled")
    else:
        log.info("Teams channel polling disabled (missing config)")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

JIRA_BROWSE_URL = JIRA_SITE_URL + "/browse"

CHAT_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crown Diagnosis Bot</title>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --surface2: #232733;
    --border: #2a2e3b; --text: #e1e4eb; --muted: #8b8fa3;
    --accent: #6366f1; --accent-hover: #818cf8;
    --green: #22c55e; --red: #ef4444; --amber: #f59e0b; --blue: #3b82f6;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--bg); color: var(--text); height: 100vh; display: flex; flex-direction: column; }
  header { padding: 12px 20px; border-bottom: 1px solid var(--border); display: flex;
           align-items: center; gap: 10px; background: var(--surface); flex-shrink: 0; }
  header h1 { font-size: 16px; font-weight: 600; }
  header .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); }
  #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
  .msg { max-width: 85%; padding: 12px 16px; border-radius: 12px; line-height: 1.5; font-size: 14px; }
  .msg.user { align-self: flex-end; background: var(--accent); color: white; border-bottom-right-radius: 4px; }
  .msg.bot { align-self: flex-start; background: var(--surface2); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
  .msg.bot .severity { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px;
                       font-weight: 600; text-transform: uppercase; margin-bottom: 8px; }
  .severity.critical, .severity.high { background: rgba(239,68,68,0.2); color: var(--red); }
  .severity.medium { background: rgba(245,158,11,0.2); color: var(--amber); }
  .severity.low, .severity.info { background: rgba(59,130,246,0.2); color: var(--blue); }
  .msg.bot h3 { font-size: 13px; color: var(--muted); margin-top: 10px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
  .msg.bot p { margin-bottom: 6px; }
  .msg.bot a { color: var(--accent-hover); text-decoration: none; }
  .msg.bot a:hover { text-decoration: underline; }
  .msg.bot .tag { display: inline-block; background: var(--surface); border: 1px solid var(--border);
                  padding: 1px 8px; border-radius: 4px; font-size: 12px; margin-right: 4px; }
  .msg.bot .actions { margin-top: 10px; display: flex; gap: 8px; flex-wrap: wrap; }
  .msg.bot .actions button { background: var(--surface); border: 1px solid var(--border); color: var(--text);
                             padding: 6px 14px; border-radius: 6px; font-size: 13px; cursor: pointer; }
  .msg.bot .actions button:hover { background: var(--accent); border-color: var(--accent); }
  .thinking { align-self: flex-start; color: var(--muted); font-size: 13px; display: flex; align-items: center; gap: 8px; }
  .thinking .dots span { animation: blink 1.4s infinite both; }
  .thinking .dots span:nth-child(2) { animation-delay: 0.2s; }
  .thinking .dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes blink { 0%, 80%, 100% { opacity: 0.2; } 40% { opacity: 1; } }
  #input-bar { padding: 12px 20px; border-top: 1px solid var(--border); background: var(--surface); flex-shrink: 0; }
  #input-bar form { display: flex; gap: 8px; }
  #input-bar input { flex: 1; background: var(--bg); border: 1px solid var(--border); color: var(--text);
                     padding: 10px 14px; border-radius: 8px; font-size: 14px; outline: none; }
  #input-bar input:focus { border-color: var(--accent); }
  #input-bar button { background: var(--accent); color: white; border: none; padding: 10px 20px;
                      border-radius: 8px; font-size: 14px; cursor: pointer; font-weight: 500; }
  #input-bar button:hover { background: var(--accent-hover); }
  #input-bar button:disabled { opacity: 0.5; cursor: not-allowed; }
  .welcome { text-align: center; color: var(--muted); margin: auto; max-width: 400px; }
  .welcome h2 { font-size: 18px; color: var(--text); margin-bottom: 8px; }
  .welcome p { font-size: 14px; line-height: 1.6; }
</style>
</head>
<body>
<header>
  <div class="dot"></div>
  <h1>Crown Diagnosis Bot</h1>
</header>
<div id="messages">
  <div class="welcome">
    <h2>Describe the issue</h2>
    <p>Paste a URL, quote reference, MPAN, or describe what's going wrong.
       The bot will check logs, databases, and metrics to diagnose the problem.</p>
  </div>
</div>
<div id="input-bar">
  <form id="chat-form">
    <input type="text" id="chat-input" placeholder="e.g. Q00123599 HH meter but pricing says NHH..." autocomplete="off" />
    <button type="submit" id="send-btn">Send</button>
  </form>
</div>
<script>
const JIRA_URL = '""" + JIRA_BROWSE_URL + """';
const messages = document.getElementById('messages');
const form = document.getElementById('chat-form');
const input = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
let firstMsg = true;

function addUser(text) {
  if (firstMsg) { messages.querySelector('.welcome')?.remove(); firstMsg = false; }
  const d = document.createElement('div');
  d.className = 'msg user';
  d.textContent = text;
  messages.appendChild(d);
  messages.scrollTop = messages.scrollHeight;
}

function addThinking() {
  const d = document.createElement('div');
  d.className = 'thinking';
  d.id = 'thinking';
  d.innerHTML = 'Investigating <span class="dots"><span>.</span><span>.</span><span>.</span></span>';
  messages.appendChild(d);
  messages.scrollTop = messages.scrollHeight;
}

function removeThinking() { document.getElementById('thinking')?.remove(); }

function addBot(data) {
  const d = document.createElement('div');
  d.className = 'msg bot';
  const diag = data.diagnosis || {};
  const sev = diag.severity || 'info';
  const isBug = diag.is_bug;
  let html = '<span class="severity ' + sev + '">' + sev + (isBug ? ' &mdash; Bug' : '') + '</span>';
  html += '<p>' + esc(diag.explanation || diag.root_cause || 'No diagnosis available') + '</p>';
  if (diag.affected_data) { html += '<h3>Affected Data</h3><p>' + esc(diag.affected_data) + '</p>'; }
  if (diag.fix) { html += '<h3>Fix</h3><p>' + esc(diag.fix) + '</p>'; }
  if (diag.existing_ticket) {
    html += '<h3>Existing Ticket</h3><p><a href="' + JIRA_URL + '/' + esc(diag.existing_ticket) + '" target="_blank">'
          + esc(diag.existing_ticket) + '</a></p>';
  }
  if (diag.existing_pr) {
    html += '<h3>Existing PR</h3><p><a href="' + esc(diag.existing_pr) + '" target="_blank">'
          + esc(diag.existing_pr) + '</a></p>';
  }
  if (data.detected_app) { html += '<p><span class="tag">' + esc(data.detected_app) + '</span></p>'; }
  d.innerHTML = html;
  messages.appendChild(d);
  messages.scrollTop = messages.scrollHeight;
}

function addError(text) {
  const d = document.createElement('div');
  d.className = 'msg bot';
  d.innerHTML = '<span class="severity high">Error</span><p>' + esc(text) + '</p>';
  messages.appendChild(d);
  messages.scrollTop = messages.scrollHeight;
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addUser(text);
  sendBtn.disabled = true;
  addThinking();
  try {
    const resp = await fetch('/investigate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text})
    });
    removeThinking();
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (data.status === 'completed') { addBot(data); }
    else { addError(data.error || 'Investigation failed — check service logs'); }
  } catch (err) {
    removeThinking();
    addError('Failed to reach diagnosis service: ' + err.message);
  }
  sendBtn.disabled = false;
  input.focus();
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def chat_ui() -> str:
    return CHAT_HTML


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model": CLAUDE_MODEL, "cooldown_hours": COOLDOWN_HOURS}


@app.post("/webhook/grafana")
async def webhook_grafana(request: Request, background_tasks: BackgroundTasks) -> dict:
    body = await request.json()
    status = body.get("status", "")
    if status != "firing":
        return {"status": "ignored", "reason": f"alert status is '{status}'"}
    if not _is_business_hours():
        log.info("Alert outside business hours (Mon-Fri %d:00-%d:00) — skipping diagnosis", BUSINESS_HOURS_START, BUSINESS_HOURS_END)
        return {"status": "ignored", "reason": "outside business hours"}
    background_tasks.add_task(run_diagnosis_pipeline)
    return {"status": "accepted"}


@app.post("/diagnose")
async def diagnose_manual(request: Request) -> dict:
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    force = body.get("force", False)
    return await run_diagnosis_pipeline(force=force)


@app.post("/investigate")
async def investigate(request: Request) -> dict:
    body = await request.json()
    message = body.get("message", "")
    if not message:
        return {"status": "error", "error": "message field is required"}
    return await run_investigation(message)


@app.get("/history")
def get_history() -> list[dict]:
    return history
