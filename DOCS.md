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

Everything runs on **AWS** behind `https://tools.cgp3.co.uk` (the old on-prem
`poc-containers:9500/9502/9503` ports were **decommissioned 2026-06-01** and no
longer respond — only the collectors on `:9550-9552` still run there, feeding AWS).
The same metrics/logs are queryable directly (and via the `/metrics` and `/logs`
slash commands):

- **Loki** (logs): `https://tools.cgp3.co.uk/loki/api/v1/query_range` — LogQL,
  labels `app`, `log_type`, `host`. Currently unauthenticated.
- **Prometheus** (metrics): no dedicated public route — Prometheus is consumed
  internally by Grafana. To run raw PromQL, go through Grafana's datasource proxy:
  `https://tools.cgp3.co.uk/grafana/api/datasources/proxy/uid/PBFA97CFB590B2093/api/v1/query`
  with `Authorization: Bearer <cgp_live_ key>` (mint one via the `mint_api_key` MCP tool).

## Notes

- No MCP tools — this is a proxied **web UI** opened via the tile.
- Public dashboards under `/grafana/public-dashboards/*` are served without
  auth.
