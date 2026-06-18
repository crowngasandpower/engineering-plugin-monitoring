# Monitoring

**Grafana dashboards** for infrastructure, application and pricing monitoring,
reached through the engineering-tools SSO (no separate Grafana login). Open the
**Monitoring** tile (`/grafana/`).

## What's there

- Infrastructure dashboards (hosts, CPU/memory, MySQL, Genus replica lag).
- Application dashboards (per-app health via `probe_success`, EPS pricing queue
  depth, etc.).
- Pricing monitoring.

Your platform role maps to a Grafana role: `admin` → Admin, `lead-engineer` /
`engineer` → Editor, everyone else → Viewer.

## Underlying data sources

The same metrics/logs are queryable directly (and via the `/metrics` and
`/logs` slash commands):

- **Prometheus** (metrics): `http://poc-containers:9500` — PromQL.
- **Loki** (logs): `http://poc-containers:9503` — LogQL, labels `app`,
  `log_type`, `host`.

## Notes

- No MCP tools — this is a proxied **web UI** opened via the tile.
- Public dashboards under `/grafana/public-dashboards/*` are served without
  auth.
