#!/bin/sh
set -e
# Prometheus does not expand environment variables in its config file directly.
# This script writes the credentials file and substitutes EXEMPTION_API_HOST into
# a runtime copy of prometheus.yml before handing off to the real binary.
[ -n "${GRAFANA_ADMIN_PASSWORD}" ] || { echo 'GRAFANA_ADMIN_PASSWORD is not set'; exit 1; }
[ -n "${EXEMPTION_API_HOST}" ] || { echo 'EXEMPTION_API_HOST is not set'; exit 1; }

install -d -m 700 /run/prometheus
(umask 077 && printf '%s' "${GRAFANA_ADMIN_PASSWORD}" > /run/prometheus/grafana-credentials)
chmod 600 /run/prometheus/grafana-credentials

# Substitute the EXEMPTION_API_HOST placeholder throughout the config.
sed "s|\${EXEMPTION_API_HOST}|${EXEMPTION_API_HOST}|g" \
    /etc/prometheus/prometheus.yml > /run/prometheus/prometheus.yml

exec /bin/prometheus --config.file=/run/prometheus/prometheus.yml "$@"
