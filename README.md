# engineering-plugin-monitoring

The Crown engineering **monitoring stack**, end to end, in one repo: the AWS
images (Grafana / Loki / Prometheus), the on-prem collectors that feed them,
the custom exporters, and the MCP plugin manifest that surfaces it all.

> This repo was consolidated from three places that had drifted apart: the old
> `crowngasandpower/monitoring` repo (dashboards + exporters + on-prem compose),
> an **untracked** `engineering-tools-framework/monitoring/` copy (the AWS build
> context), and uncommitted collector configs living only on poc-containers.
> Everything that defines the monitoring stack now lives here.

---

## Architecture

```
APP HOSTS                          POC-CONTAINERS (192.168.164.184)        AWS (ECS: crown-eng-mcp-cluster)
apps-prod-1  .252                  ┌─────────────────────────────┐
apps-prod-2  .207                  │ metrics-proxy (nginx)        │        ┌───────────────────────────┐
apps-prod-mysql .206               │  :9550 scrape path-router ───┼──pull──┤ Prometheus                │
eps-worker-1 .40                   │  :9551 loki push-proxy ◄─────┼──push──┐│  (scrapes on-prem by IP   │
                                   │                              │        ││   over the S2S VPN)       │
 node/nginx/phpfpm/mysqld  ──pull──┤ (via :9550)                  │        │└───────────────────────────┘
 exporters                         │                              │        │
 log shippers (apache/laravel) ──push─► :9551 ──► tools.cgp3.co.uk ─ALB─► Loki │
                                   │                              │        │┌───────────────────────────┐
                                   │ custom exporters:            │        ││ Loki  (log store)         │
                                   │  genus-monitor :9507 ────────┼──pull──┤│                           │
                                   │   → room_temperature_celsius │        │└───────────────────────────┘
                                   │  powerstore :9521            │        │
                                   │  idrac      :9516            │        │┌───────────────────────────┐
                                   │  vmware     :9515            │        ││ Grafana                   │
                                   │  weather    :9520 (outside°) │        ││  dashboards + alerting    │
                                   │  blackbox, pushgateway :9501 │        │└───────────┬───────────────┘
                                   │  alloy → cAdvisor metrics ───┼─remote_write──────────┘
                                   └─────────────────────────────┘                     │
                                                                          surfaced via the "monitoring"
                                                                          MCP plugin → /grafana, /loki, /tv
```

**Key principle:** AWS Prometheus **pulls** every on-prem producer over the
site-to-site VPN, addressed by IP (`192.168.164.184:<port>` or, for per-host
exporters, through the metrics-proxy on `:9550`). Loki is **push** — app-host
log shippers send through the poc-containers loki-proxy (`:9551`) which forwards
to the ALB. This is why the ECS security group must allow the ALB to reach Loki
on 3100 (see `engineering-tools-framework/terraform/ecs/security_groups.tf`).

### Producer map (what AWS Prometheus scrapes)

| Producer | Host:port | Job | Feeds |
| --- | --- | --- | --- |
| pushgateway | .184:9501 | `pushgateway` | batch/script-pushed metrics |
| **genus-monitor** | .184:9507 | `genus` | **server room temps**, genus replica lag |
| weather-exporter | .184:9520 | `weather` | outside temperature (Bury) |
| vmware-exporter | .184:9515 | `vmware` | vCenter |
| idrac-exporter | .184:9516 | `idrac` | iDRAC hardware health/temps |
| powerstore-exporter | .184:9521 | `powerstore_*` | SAN |
| node/nginx/phpfpm/mysqld | via .184:9550 | `node`/`nginx`/`phpfpm`/`mysql` | per-app-host OS/web/PHP/DB |
| alloy (cAdvisor) | remote_write → :9500 | — | docker container metrics |

---

## Repo layout

```
plugin.json              MCP manifest — surfaces Grafana (/grafana, authed +
                         role-mapped), Loki (/loki, unauth for push), /tv redirect.
buildspec.yml            CodeBuild: build+push the 3 images, force-redeploy ECS.
images/                  AWS Fargate image build contexts:
  grafana/    Dockerfile + provisioning/ (AWS datasources→*.mcp.local + alerting) + dashboards/
  loki/       Dockerfile + loki-config.yml
  prometheus/ Dockerfile + prometheus.yml  (the VPN-pull scrape config — the lynchpin)
collectors/              On-prem bridge containers (run on poc-containers):
  docker-compose.yml     run definitions (project: monitoring-collectors)
  metrics-proxy/nginx.conf   :9550 scrape router + :9551 loki push-proxy
  alloy/alloy.config         cAdvisor docker metrics → remote_write
exporters/               Custom Prometheus exporter source (built/run on poc-containers):
  genus-monitor/  powerstore-exporter/  weather-exporter/  idrac-monitor/
```

---

## Build & deploy (AWS images)

CodeBuild project **`crown-eng-mcp-monitoring-build`** (source = this repo) runs
`buildspec.yml`: builds `crown-eng-mcp/{grafana,loki,prometheus}:latest`, pushes
to ECR, and — when `DEPLOY_CLUSTER` is set — force-redeploys the matching ECS
services so they pull the new image.

```bash
# Trigger a build + deploy
aws codebuild start-build --project-name crown-eng-mcp-monitoring-build
```

The ECS services (`grafana`, `loki`, `prometheus`) themselves are defined in
`engineering-tools-framework/terraform/ecs/monitoring.tf` (task defs, Cloud Map,
EFS for Loki/Prometheus data, the RDS-backed Grafana DB). Terraform owns the
*infrastructure*; this repo owns the *image contents*.

Dashboards are file-provisioned (baked into the grafana image). To change a
dashboard or the incident counter, edit the JSON here and rebuild — there is no
live UI editing of provisioned dashboards.

## Deploy (on-prem collectors)

On poc-containers, from a checkout of this repo:

```bash
cd collectors && docker compose -p monitoring-collectors up -d
```

## MCP plugin

`plugin.json` (v2.0.0) is mounted by the MCP server, which proxies:
- `/grafana` → grafana service, authenticated, role-mapped (admin→Admin,
  lead-engineer/engineer→Editor, others→Viewer) via `X-WEBAUTH-*` headers.
- `/loki`    → loki service, unauthenticated (so on-prem log shippers can push).
- `/tv`      → redirect to the public Infrastructure dashboard.

---

## App-host agents (documented, not deployed from here)

The per-machine exporters and log shippers on the app hosts are host-level
config (managed per-host, not from this repo). Captured here for reference:

| Host | IP | Agents |
| --- | --- | --- |
| apps-prod-1 | 192.168.164.252 | node_exporter :9100, nginx :9113, phpfpm :9253, log shipper → poc-containers :9551 |
| apps-prod-2 | 192.168.164.207 | node_exporter :9100, nginx :9113, phpfpm :9253, log shipper → poc-containers :9551 |
| apps-prod-mysql | 192.168.164.206 | node_exporter :9100, **mysqld_exporter :9104** (systemd `mysqld_exporter.service`) |
| eps-worker-1 | 192.168.164.40 | node_exporter :9100 |

> Moving these into a config-management repo (ansible) is a sensible follow-up.
