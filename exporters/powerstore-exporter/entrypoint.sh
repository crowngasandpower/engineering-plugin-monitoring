#!/bin/sh
set -e

mkdir -p /sd

# Generate config.yml from environment variables.
# Credentials are shared across all arrays; only ADDRESS and NAME vary per SAN.
{
  printf 'exporter:\n'
  printf '  port: 9010\n'
  printf '  reqLimit: 10\n'
  printf 'log:\n'
  printf '  type: logfmt\n'
  printf '  path: ./powerstore.out.log\n'
  printf '  level: info\n'
  printf 'storageList:\n'
  printf '  - ip: "%s"\n'         "$POWERSTORE_1_ADDRESS"
  printf '    name: "%s"\n'       "${POWERSTORE_1_NAME:-$POWERSTORE_1_ADDRESS}"
  printf '    user: "%s"\n'       "$POWERSTORE_USER"
  printf '    password: "%s"\n'   "$POWERSTORE_PASSWORD"
  printf '    apiVersion: "%s"\n' "${POWERSTORE_API_VERSION:-1.0}"
  if [ -n "${POWERSTORE_2_ADDRESS:-}" ]; then
    printf '  - ip: "%s"\n'         "$POWERSTORE_2_ADDRESS"
    printf '    name: "%s"\n'       "${POWERSTORE_2_NAME:-$POWERSTORE_2_ADDRESS}"
    printf '    user: "%s"\n'       "$POWERSTORE_USER"
    printf '    password: "%s"\n'   "$POWERSTORE_PASSWORD"
    printf '    apiVersion: "%s"\n' "${POWERSTORE_API_VERSION:-1.0}"
  fi
  if [ -n "${POWERSTORE_3_ADDRESS:-}" ]; then
    printf '  - ip: "%s"\n'         "$POWERSTORE_3_ADDRESS"
    printf '    name: "%s"\n'       "${POWERSTORE_3_NAME:-$POWERSTORE_3_ADDRESS}"
    printf '    user: "%s"\n'       "$POWERSTORE_USER"
    printf '    password: "%s"\n'   "$POWERSTORE_PASSWORD"
    printf '    apiVersion: "%s"\n' "${POWERSTORE_API_VERSION:-1.0}"
  fi
  if [ -n "${POWERSTORE_4_ADDRESS:-}" ]; then
    printf '  - ip: "%s"\n'         "$POWERSTORE_4_ADDRESS"
    printf '    name: "%s"\n'       "${POWERSTORE_4_NAME:-$POWERSTORE_4_ADDRESS}"
    printf '    user: "%s"\n'       "$POWERSTORE_USER"
    printf '    password: "%s"\n'   "$POWERSTORE_PASSWORD"
    printf '    apiVersion: "%s"\n' "${POWERSTORE_API_VERSION:-1.0}"
  fi
} > /powerstore_exporter/config.yml

# Generate Prometheus file service discovery targets.
# Prometheus watches /sd/powerstore.json and picks up changes within 30s.
{
  printf '[\n'
  printf '  {\n'
  printf '    "targets": ["powerstore-exporter:9010"],\n'
  printf '    "labels": {\n'
  printf '      "array_ip": "%s",\n'  "$POWERSTORE_1_ADDRESS"
  printf '      "array_name": "%s"\n' "${POWERSTORE_1_NAME:-$POWERSTORE_1_ADDRESS}"
  printf '    }\n'
  printf '  }'
  if [ -n "${POWERSTORE_2_ADDRESS:-}" ]; then
    printf ',\n'
    printf '  {\n'
    printf '    "targets": ["powerstore-exporter:9010"],\n'
    printf '    "labels": {\n'
    printf '      "array_ip": "%s",\n'  "$POWERSTORE_2_ADDRESS"
    printf '      "array_name": "%s"\n' "${POWERSTORE_2_NAME:-$POWERSTORE_2_ADDRESS}"
    printf '    }\n'
    printf '  }'
  fi
  if [ -n "${POWERSTORE_3_ADDRESS:-}" ]; then
    printf ',\n'
    printf '  {\n'
    printf '    "targets": ["powerstore-exporter:9010"],\n'
    printf '    "labels": {\n'
    printf '      "array_ip": "%s",\n'  "$POWERSTORE_3_ADDRESS"
    printf '      "array_name": "%s"\n' "${POWERSTORE_3_NAME:-$POWERSTORE_3_ADDRESS}"
    printf '    }\n'
    printf '  }'
  fi
  if [ -n "${POWERSTORE_4_ADDRESS:-}" ]; then
    printf ',\n'
    printf '  {\n'
    printf '    "targets": ["powerstore-exporter:9010"],\n'
    printf '    "labels": {\n'
    printf '      "array_ip": "%s",\n'  "$POWERSTORE_4_ADDRESS"
    printf '      "array_name": "%s"\n' "${POWERSTORE_4_NAME:-$POWERSTORE_4_ADDRESS}"
    printf '    }\n'
    printf '  }'
  fi
  printf '\n]\n'
} > /sd/powerstore.json

exec ./powerstore_exporter -c /powerstore_exporter/config.yml
