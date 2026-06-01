#!/usr/bin/env bash
#
# Deploy the on-prem monitoring stack on poc-containers (192.168.164.184).
#
#   sudo ./deploy.sh                 # pull latest + bring up the whole stack
#   sudo ./deploy.sh genus-monitor   # pull latest + (re)create just one service
#
# Run from the repo checkout at /home/docker/engineering-plugin-monitoring/onprem.
# The compose project name is pinned to "monitoring" so the named volumes
# (grafana/prometheus/loki/pushgateway data) re-attach. Postgres data lives in
# the git-ignored ./data/postgres bind mount; .env (secrets) is git-ignored too.
#
# NOTE: the standalone bridge collectors (alloy, metrics-proxy) are NOT in this
# compose — manage them with ./collectors-compose.yml.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> git pull"
git pull --ff-only

echo "==> docker compose up -d ${*:-(whole stack)}"
docker compose -p monitoring up -d "$@"

echo
echo "==> running services"
docker compose -p monitoring ps

cat <<'NOTE'

Done.

If you added a NEW scraped collector (Prometheus pulls it over the VPN), it also
needs an AWS-side scrape job. On your workstation:

  1. add a job to images/prometheus/prometheus.yml -> '192.168.164.184:<host-port>'
  2. aws codebuild start-build --project-name crown-eng-mcp-monitoring-build
  3. confirm the target is 'up' in Prometheus

(Collectors that PUSH — remote_write / Loki push — need no scrape job; they just
use the metrics-proxy endpoints.)
NOTE
