# Crown Monitoring

Observability stack for Crown Gas & Power infrastructure and applications. Lives on `poc-containers` (192.168.173.140).

## Contents

| Service | Port | Container | Purpose |
| --- | --- | --- | --- |
| Prometheus | 9500 | `monitoring-prometheus` | Metrics scraping + storage |
| Pushgateway | 9501 | `monitoring-pushgateway` | Push-based metrics from short-lived jobs (Jenkins runs, etc.) |
| Grafana | 9502 | `monitoring-grafana` | Dashboards, alerting, LDAP login |
| Loki | 9503 | `monitoring-loki` | Log aggregation |
| Genus Monitor | 9507 | `monitoring-genus-monitor` | Custom Prometheus exporter — Genus health, file freshness, EPS/GPS imports, PRTG, PowerStore, Ubibot, etc. |
| PostgreSQL (monitoring) | 9508 | `monitoring-postgres` | Stores genus/ewelcome/pricing-failure tables written by the Genus monitor |
| PostgREST (monitoring) | 9509 | `monitoring-postgrest` | REST view onto the monitoring postgres |

## Cross-stack reference

The Grafana **Code Review** dashboard reads its data from a different stack — the [`crown-engineering-mcp`](https://github.com/crowngasandpower/crown-engineering-mcp) repo, which owns the `reviews` table in its own postgres on port 9504. The provisioning in [`grafana/provisioning/datasources/datasources.yml`](grafana/provisioning/datasources/datasources.yml) defines two postgres datasources:

- `PostgreSQL` (UID `review-api-postgres`) — points cross-stack at `poc-containers:9504`. Referenced **by name** by the Code Review dashboard.
- `PostgreSQL Monitoring` (UID `PCC52D03280B7034C`) — points in-stack at `monitoring-postgres:5432`. Referenced **by UID** by the Genus dashboards.

Don't rename either, and don't change the UIDs — the dashboards rely on this exact pairing.

## Running locally

```bash
cp .env.example .env   # populate GENUS, MYSQL, EPS_REDIS, PRTG, POWERSTORE, UBIBOT, LDAP creds
docker compose up -d
```

The compose project name is pinned to `monitoring` so the existing on-host named volumes (`monitoring_grafana_data`, `monitoring_prometheus_data`, `monitoring_pushgateway_data`, `monitoring_loki_data`) re-attach transparently.

## History

This repo split out of [`crowngasandpower/ai-code-review`](https://github.com/crowngasandpower/ai-code-review), which had grown to host both the AI code review service and the entire monitoring stack. Code Review went to [`crown-engineering-mcp`](https://github.com/crowngasandpower/crown-engineering-mcp); monitoring lives here.
