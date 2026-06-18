#!/bin/sh
set -e
# Prometheus does not expand environment variables in its config file, so
# credentials_file is used for the grafana-alerts basic_auth. Write the
# password from the env var here before Prometheus starts.
[ -n "${GRAFANA_ADMIN_PASSWORD}" ] || { echo 'GRAFANA_ADMIN_PASSWORD is not set'; exit 1; }
install -d -m 700 /run/prometheus
(umask 077 && printf '%s' "${GRAFANA_ADMIN_PASSWORD}" > /run/prometheus/grafana-credentials)
exec /bin/prometheus "$@"
