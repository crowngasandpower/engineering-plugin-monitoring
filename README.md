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
images/                  AWS Fargate image build contexts (what CodeBuild builds):
  grafana/    Dockerfile + provisioning/ (AWS datasources→*.mcp.local + alerting) + dashboards/
  loki/       Dockerfile + loki-config.yml
  prometheus/ Dockerfile + prometheus.yml  (the VPN-pull scrape config — the lynchpin)
onprem/                  The poc-containers stack — a faithful copy of what runs on
                         192.168.164.184 (:/home/docker/monitoring). Deployed THERE, not on AWS:
  docker-compose.yml         the `monitoring` compose project (exporters, pushgateway,
                             legacy on-prem prometheus/loki/grafana, proxies)
  prometheus.yml, loki-config.yml, blackbox.yml, init.sql   on-prem configs
  grafana/                   on-prem Grafana provisioning + dashboards (legacy)
  genus-monitor/             custom exporter → room_temperature_celsius (server-room temps)
  powerstore-exporter/ weather-exporter/ idrac-monitor/   custom exporters
  error-diagnosis/ supervisor-monitor/ grafana-proxy/ jenkins-proxy/ file-gateway/
  alloy/alloy.config         cAdvisor docker metrics → remote_write (standalone container)
  metrics-proxy-nginx.conf   :9550 scrape router + :9551 loki push-proxy (standalone)
  collectors-compose.yml     run definitions for the two standalone bridge containers
                             (alloy + metrics-proxy; project: monitoring-collectors)
```

> **AWS vs on-prem configs deliberately differ.** `images/prometheus/prometheus.yml`
> is the AWS scrape config (pulls on-prem over the VPN); `onprem/prometheus.yml` is
> the legacy on-prem scrape config. Likewise the Grafana datasources/dashboards under
> `images/grafana` (AWS, prod) vs `onprem/grafana` (legacy). Dashboards are NOT yet
> reconciled between the two — `images/` is what prod serves.

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

## Deploy (on-prem stack + collectors)

The on-prem stack runs on **poc-containers** (`192.168.164.184`) out of
`/home/docker/monitoring`, which historically tracked the old
`crowngasandpower/monitoring` repo. `onprem/` here is the authoritative copy.

```bash
# main stack (compose project name MUST stay "monitoring" so named volumes re-attach)
cd onprem && docker compose -p monitoring up -d
# the two standalone bridge containers (alloy + metrics-proxy)
docker compose -f onprem/collectors-compose.yml -p monitoring-collectors up -d
```

> ⚠️ Re-pointing the live poc-containers checkout at this repo is a maintenance-window
> task (the directory layout moved under `onprem/`, and recreating containers briefly
> disrupts scraping). The running stack was left untouched during consolidation; do the
> re-clone deliberately, preserving the `monitoring` project name and the git-ignored
> `data/` and `.env` files.

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
