#!/usr/bin/env bash
set -euo pipefail

VERSION="1.8.2"
USER="node_exporter"
BIN="/usr/local/bin/node_exporter"
SERVICE="/etc/systemd/system/node_exporter.service"
HOSTNAME=$(hostname)

# Detect architecture
case "$(uname -m)" in
  x86_64)  ARCH="amd64" ;;
  aarch64) ARCH="arm64" ;;
  armv7l)  ARCH="armv7" ;;
  *)        echo "Unsupported architecture: $(uname -m)"; exit 1 ;;
esac

TARBALL="node_exporter-${VERSION}.linux-${ARCH}.tar.gz"
URL="https://github.com/prometheus/node_exporter/releases/download/v${VERSION}/${TARBALL}"

echo "==> Installing node_exporter ${VERSION} (${ARCH})"

# Download and install binary
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "--> Downloading ${URL}"
curl -fsSL "$URL" -o "${TMP}/${TARBALL}"
tar -xzf "${TMP}/${TARBALL}" -C "$TMP"

# Stop running service before replacing binary to avoid "Text file busy"
if systemctl is-active --quiet node_exporter 2>/dev/null; then
  echo "--> Stopping existing node_exporter service"
  sudo systemctl stop node_exporter
fi

sudo cp "${TMP}/node_exporter-${VERSION}.linux-${ARCH}/node_exporter" "$BIN"
sudo chmod 755 "$BIN"

echo "--> Binary installed at ${BIN}"

# Create system user if it doesn't exist
if ! id "$USER" &>/dev/null; then
  sudo useradd -rs /bin/false "$USER"
  echo "--> Created user: ${USER}"
fi

# systemd collector needs access to the systemd D-Bus socket
sudo usermod -aG systemd-journal "$USER" 2>/dev/null || true

# Write systemd service
sudo tee "$SERVICE" > /dev/null <<EOF
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
User=${USER}
ExecStart=${BIN} \
  --collector.systemd \
  --collector.systemd.unit-include=".+\\.service"
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

echo "--> Systemd service written to ${SERVICE}"

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now node_exporter

echo "--> Service enabled and started"

# Verify
sleep 2
if systemctl is-active --quiet node_exporter; then
  echo ""
  echo "==> node_exporter is running. Test with:"
  echo "    curl -s http://${HOSTNAME}:9100/metrics | head -5"
else
  echo "ERROR: node_exporter failed to start"
  systemctl status node_exporter
  exit 1
fi
