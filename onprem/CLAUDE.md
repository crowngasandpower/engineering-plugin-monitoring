# Crown Monitoring — Claude guide

## What this repo is

The observability stack for Crown Gas & Power: Prometheus, Grafana, Loki, Pushgateway, plus a custom Prometheus exporter (`genus-monitor`) and a small Postgres + PostgREST pair holding the data the exporter writes.

Deployed to `poc-containers` (192.168.173.140), single host, single compose project named `monitoring`.

## Engineering standards

Follow the central standards in `../engineering-standards/CLAUDE.md`.

## Critical: don't break the existing volumes

Four named docker volumes hold all historical state:

- `monitoring_grafana_data` — user-edited dashboards, the UI-created Loki datasource
- `monitoring_prometheus_data` — historical metrics
- `monitoring_loki_data` — historical logs
- `monitoring_pushgateway_data` — pushed metric state

These re-attach automatically as long as the compose project name stays `monitoring` (set explicitly at the top of `docker-compose.yml`). **Do not change `name: monitoring`.** Don't rename the volumes in the compose file either.

## Critical: don't break the cross-stack datasource pairing

[`grafana/provisioning/datasources/datasources.yml`](grafana/provisioning/datasources/datasources.yml) defines two postgres datasources with very specific names and UIDs:

- `PostgreSQL` / UID `review-api-postgres` → cross-stack to `crown-engineering-mcp` postgres on port 9504, holds the `reviews` table. Referenced **by name** by the Code Review dashboard.
- `PostgreSQL Monitoring` / UID `PCC52D03280B7034C` → in-stack `monitoring-postgres:5432`, holds the genus tables. Referenced **by UID** by the Genus dashboards.

If you change either name or either UID, dashboards break silently (panels show "Datasource not found"). Add new datasources rather than mutating these.

## When adding a new dashboard

Drop the JSON under `grafana/dashboards/<Folder>/`. The provisioning at `grafana/provisioning/dashboards/` will pick it up on next Grafana restart (or `docker compose restart grafana`).

For dashboards that need the cross-stack review-api postgres, reference the datasource by **name** (`"datasource": "PostgreSQL"`).
For dashboards that need the in-stack monitoring postgres, reference the datasource by **UID** (`"PCC52D03280B7034C"`).

## When adding a new exporter / scrape target

Edit `prometheus.yml` and add the new scrape job. Restart prometheus: `docker compose restart prometheus`.

If the target lives outside this stack (i.e. on another host), use the host's hostname/IP (e.g. `apps-prod-1:9100`) — the existing node_exporter scrapes are the pattern.

## Pre-push review

Before pushing changes, run the review API:

```bash
DIFF=$(git diff origin/main...HEAD)
curl -s -X POST http://poc-containers:9506/review \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg diff "$DIFF" '{diff: $diff}')" | jq .
```

Fix HIGH/MEDIUM issues before pushing.
