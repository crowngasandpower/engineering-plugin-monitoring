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
# Verify the credentials file is non-empty before Prometheus starts — a missing or
# empty file causes the grafana-alerts scrape to fail silently with no obvious error.
[ -s /run/prometheus/grafana-credentials ] || { echo 'ERROR: grafana-credentials is empty; Prometheus would silently fail to scrape Grafana'; exit 1; }

# Escape characters that sed treats as special in its replacement field.
# Order matters: \ must be escaped first so the backslashes added for | and &
# are not themselves re-escaped in subsequent passes.
# / does not need escaping because | is used as the sed delimiter (s|...|...|g).
# @ai-review-ignore: $ does not need escaping — in POSIX sed $ is only special in the
# pattern field (end-of-line anchor), not in the replacement. Adding \$ would produce
# undefined behaviour (POSIX only defines \\, \1-\9, and \& in replacements).
_esc_host=$(printf '%s' "${EXEMPTION_API_HOST}" | tr -d '\n' | sed 's/\\/\\\\/g; s/|/\\|/g; s/&/\\&/g')
sed "s|\${EXEMPTION_API_HOST}|${_esc_host}|g" \
    /etc/prometheus/prometheus.yml > /run/prometheus/prometheus.yml

exec /bin/prometheus --config.file=/run/prometheus/prometheus.yml "$@"
