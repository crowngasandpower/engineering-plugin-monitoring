#!/bin/sh
set -e
# Prometheus does not expand environment variables in its config file, so
# credentials_file is used for the grafana-alerts basic_auth. Write the
# password from the env var here before Prometheus starts.
install -d -m 700 /run/prometheus
(umask 077 && printf '%s' "${GRAFANA_ADMIN_PASSWORD}" > /run/prometheus/grafana-credentials)
exec /bin/prometheus "$@"
