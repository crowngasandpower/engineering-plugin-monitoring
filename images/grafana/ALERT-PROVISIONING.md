# Grafana Alert Rule Provisioning

## Why this exists

Grafana 13 introduced a hard lock on YAML file-provisioned alert rules: once a
rule is stamped `provenance=file` it cannot be modified via the UI or any API.
This made live hotfixing impossible — every threshold change required a code
change, PR, and redeploy.

This PR switches alert rule provisioning from Grafana's built-in file
provisioner to a Python startup script (`provision-alerts.py`) that pushes
rules via the Grafana ruler API. Rules provisioned this way carry no provenance
lock and are fully editable in the Grafana UI.

---

## Live-edit contract

| Action | Result |
|---|---|
| Edit a rule threshold/query in the Grafana UI | Takes effect immediately |
| Container restart (local or ECS task replacement) | `alert-rules.yml` wins — UI edits overwritten |
| Raise a PR updating `alert-rules.yml` and deploy | Change is permanent |

This matches the agreed workflow: make hotfixes live in the UI, then raise a
PR to make them permanent before the next deploy overwrites them.

---

## Files changed

| File | Change |
|---|---|
| `Dockerfile` | Adds `python3` + `py3-yaml` (Alpine apk); copies `entrypoint.sh`, `provision-alerts.py`, `alert-rules.yml`; overrides `ENTRYPOINT` |
| `entrypoint.sh` | New. Runs DB migration, starts provisioner in background, hands off to `/run.sh` |
| `provision-alerts.py` | New. Reads `alert-rules.yml` and pushes groups via the ruler API |
| `alert-rules.yml` | New. Contains all alert rule groups (previously inside `alerting.yml`) |
| `provisioning/alerting/alerting.yml` | Groups section removed; now contains only contact points, notification policies, and templates |

Onprem-specific:

| File | Change |
|---|---|
| `onprem/grafana/alert-rules.yml` | New. Same rules as images version but with flat folder names and dashboard runbook annotations |
| `onprem/grafana/provisioning/alerting/alerting.yml` | Groups section removed |
| `onprem/docker-compose.override.yml` | Mounts `./grafana/alert-rules.yml` into the container at `/opt/alert-rules.yml`; adds `GF_UNIFIED_ALERTING_PROVISIONING_ALLOW_UI_UPDATES=true` |

---

## One-time database migration (AWS RDS)

The existing rules in the live Grafana database carry `provenance=file` from
the old file provisioner. The startup script automatically handles this for the
local SQLite database (deletes the rows from `provenance_type` before Grafana
starts). For AWS (RDS/PostgreSQL), run this **once** against the Grafana
database before or immediately after deploying this image:

```sql
DELETE FROM provenance_type WHERE record_type = 'alertRule';
```

After the new container starts, `provision-alerts.py` will re-provision all
rules via the ruler API and they will be editable in the UI.

> **Note**: This only affects alert rule provenance. Contact points, notification
> policies, and templates remain file-provisioned and retain their lock
> (intentionally — those do not need live editing).

---

## Adding or editing alert rules

**Permanent change (the normal path):**
1. Edit `monitoring/images/grafana/alert-rules.yml`
2. Also edit `monitoring/onprem/grafana/alert-rules.yml` if the folder names differ
3. Raise a PR
4. On deploy the new rules are applied automatically

**Live hotfix (emergency path):**
1. Edit the rule in the Grafana UI — change takes effect immediately
2. Raise a PR updating `alert-rules.yml` to match before the next deploy

**Exporting a UI-edited rule back to YAML:**
```bash
# Get a single rule (provisioning API format)
curl -s -u admin:PASSWORD http://localhost:9510/api/v1/provisioning/alert-rules/{uid}

# Export all rules in provisioning YAML format
curl -s -u admin:PASSWORD "http://localhost:9510/api/v1/provisioning/alert-rules/export?format=yaml"
```

---

## How `provision-alerts.py` works

```
Container starts
  entrypoint.sh:
    1. python3 provision-alerts.py --migrate
         Deletes provenance_type rows for alertRule (SQLite only).
         For Postgres/RDS: logs a reminder to run the SQL manually.

    2. python3 provision-alerts.py --provision &  (background)
         Polls /api/health until Grafana is ready.
         Reads /opt/alert-rules.yml.
         For each group: POST to /api/ruler/grafana/api/v1/rules/{folderUID}
         Rules have no provenance lock — editable in UI.

    3. exec /run.sh  (Grafana starts normally)
```

The ruler API POST is idempotent: it creates the group on first run and
replaces it (same UIDs) on every subsequent restart.
