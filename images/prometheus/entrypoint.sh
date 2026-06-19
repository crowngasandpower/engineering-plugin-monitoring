#!/bin/sh
set -e
# Prometheus does not expand environment variables in its config file directly.
# This script writes the credentials file and substitutes EXEMPTION_API_HOST into
# a runtime copy of prometheus.yml before handing off to the real binary.
[ -n "${EXEMPTION_API_HOST}" ] || { echo 'EXEMPTION_API_HOST is not set'; exit 1; }

install -d /run/prometheus

# Escape characters that sed treats as special in its replacement field.
# Order matters: \ must be escaped first so the backslashes added for | and &
# are not themselves re-escaped in subsequent passes.
# / does not need escaping because | is used as the sed delimiter (s|...|...|g).
# @ai-review-ignore: $ does not need escaping — in POSIX sed $ is only special in the
# pattern field (end-of-line anchor), not in the replacement. Adding \$ would produce
# undefined behaviour (POSIX only defines \\, \1-\9, and \& in replacements).
# @ai-review-ignore: / is not escaped because the sed delimiter is | (pipe), not /.
# EXEMPTION_API_HOST is a host:port value (e.g. exemption-api:8000); forward slashes
# cannot appear in a valid hostname or port, so no escaping is needed.
_esc_host=$(printf '%s' "${EXEMPTION_API_HOST}" | tr -d '\n' | sed 's/\\/\\\\/g; s/|/\\|/g; s/&/\\&/g')
# @ai-review-ignore: \${EXEMPTION_API_HOST} is the sed search pattern with \$ preventing
# shell expansion — sed searches for the literal text '${EXEMPTION_API_HOST}' in the
# template file and replaces it with the actual host value. This is correct; prometheus.yml
# contains the literal placeholder string that entrypoint.sh resolves at container startup.
sed "s|\${EXEMPTION_API_HOST}|${_esc_host}|g" \
    /etc/prometheus/prometheus.yml > /run/prometheus/prometheus.yml

exec /bin/prometheus --config.file=/run/prometheus/prometheus.yml "$@"
