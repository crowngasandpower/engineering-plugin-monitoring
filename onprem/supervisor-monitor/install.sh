#!/usr/bin/env bash
# Install the supervisor monitor cron job on poc-containers.
# Run once after deploying the script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/check-workers.sh"
CRON_SCHEDULE="*/5 * * * *"
LOG_FILE="/var/log/supervisor-monitor.log"

if [[ ! -f "$SCRIPT_PATH" ]]; then
    echo "ERROR: $SCRIPT_PATH not found"
    exit 1
fi

chmod +x "$SCRIPT_PATH"

# Build the cron line — log to file, keep last 10k lines
CRON_LINE="${CRON_SCHEDULE} ${SCRIPT_PATH} >> ${LOG_FILE} 2>&1 && tail -10000 ${LOG_FILE} > ${LOG_FILE}.tmp && mv ${LOG_FILE}.tmp ${LOG_FILE}"

# Check if already installed
if crontab -l 2>/dev/null | grep -qF "check-workers.sh"; then
    echo "Cron entry already exists — updating..."
    crontab -l 2>/dev/null | grep -vF "check-workers.sh" | { cat; echo "$CRON_LINE"; } | crontab -
else
    echo "Installing cron entry..."
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
fi

echo "Installed. Cron entry:"
crontab -l | grep "check-workers"
echo ""
echo "Log file: ${LOG_FILE}"
echo "Test with: ${SCRIPT_PATH}"
