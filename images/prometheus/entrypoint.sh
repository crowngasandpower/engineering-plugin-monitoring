#!/bin/sh
set -e
# Prometheus does not expand environment variables in its config file, so
# credentials_file is used for the grafana-alerts basic_auth. Write the
# password from the env var here before Prometheus starts.
(umask 077 && printf '%s' "${GRAFANA_ADMIN_PASSWORD}" > /tmp/grafana-credentials)
exec /bin/prometheus "$@"
