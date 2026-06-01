#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# check-workers.sh — supervisor worker health monitor
#
# Runs via cron on poc-containers every 5 minutes. For each configured
# host, SSHes in, parses `supervisorctl status`, restarts any FATAL or
# STOPPED workers, re-checks, and pushes per-worker Prometheus metrics
# to Pushgateway. Grafana alert rules fire if a worker stays unhealthy
# after the restart attempt.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-http://localhost:9501}"
SSH_OPTS="-o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes"
SSH_USER="${SSH_USER:-root}"
HOSTS="${SUPERVISOR_HOSTS:-apps-prod-1 apps-prod-2}"
LOG_TAG="supervisor-monitor"
RESTART_WAIT_SECONDS=5

# ── Helpers ──────────────────────────────────────────────────────────
log() { logger -t "$LOG_TAG" "$*" 2>/dev/null; echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

# Parse supervisorctl status output into tab-separated: name\tstate
# Handles both "RUNNING pid ..." and "FATAL Exited ..." formats.
parse_status() {
    awk '{
        name = $1
        state = $2
        if (state == "RUNNING" || state == "FATAL" || state == "STOPPED" ||
            state == "STARTING" || state == "BACKOFF" || state == "EXITED" ||
            state == "UNKNOWN")
            print name "\t" state
    }'
}

# Extract app name from supervisor process name.
# "ces-worker-ebilling:00" → "ces"
# "gateway-elec-polling-cycle-default:00" → "gateway-elec"
# Strategy: strip the known worker-name suffixes to find the app prefix.
extract_app() {
    local proc_name="$1"
    # Remove the :NN process number suffix
    proc_name="${proc_name%%:*}"
    # Known app prefixes (longest first to match gateway-elec before gateway)
    local -a apps=(
        ces-elec ces
        gateway-elec gateway
        reporting
        gps eps
        wolfy wolfy-elec
        synergy
        aerio
        settlements settlements-elec
        billy
        dds dds-elec
        pims pims-elec
        revenue-dollar revenue-dollar-elec
        jigsaw jigsaw-elec
        esema esema2
        meter-data-manager
        echo-server
        complaintstracker
        link
        doc-master doc-master-elec
    )
    for app in "${apps[@]}"; do
        if [[ "$proc_name" == "$app-"* || "$proc_name" == "$app" ]]; then
            echo "$app"
            return
        fi
    done
    # Fallback: first token before "-worker" or "-polling" or just the name
    echo "$proc_name" | sed -E 's/-(worker|polling|build|import|default|ceased).*$//'
}

# Extract worker name from supervisor process name.
# "ces-worker-ebilling:00" → "ebilling"
# "gateway-elec-polling-cycle-default:00" → "polling-cycle-default"
extract_worker() {
    local proc_name="$1"
    local app="$2"
    proc_name="${proc_name%%:*}"
    # Remove app prefix and leading dash
    local remainder="${proc_name#"$app"}"
    remainder="${remainder#-}"
    # Remove "worker-" prefix if present
    remainder="${remainder#worker-}"
    echo "${remainder:-default}"
}

# ── Main loop per host ───────────────────────────────────────────────
for host in $HOSTS; do
    log "Checking $host..."

    # Collect supervisorctl status via SSH
    raw_status=$(ssh $SSH_OPTS "${SSH_USER}@${host}" "supervisorctl status 2>/dev/null" 2>/dev/null) || {
        log "ERROR: SSH to $host failed"
        # Push a host-level unreachable metric
        cat <<METRIC | curl -sf --data-binary @- "${PUSHGATEWAY_URL}/metrics/job/supervisor/host/${host}" >/dev/null 2>&1 || true
# TYPE supervisor_host_reachable gauge
# HELP supervisor_host_reachable Whether SSH to the host succeeded (1=yes, 0=no)
supervisor_host_reachable 0
# TYPE supervisor_check_timestamp_seconds gauge
# HELP supervisor_check_timestamp_seconds Unix timestamp of last check
supervisor_check_timestamp_seconds $(date +%s)
METRIC
        continue
    }

    # Parse into structured data
    parsed=$(echo "$raw_status" | parse_status)
    if [[ -z "$parsed" ]]; then
        log "WARNING: No supervisor processes found on $host"
        continue
    fi

    # Find unhealthy workers
    unhealthy=$(echo "$parsed" | awk -F'\t' '$2 == "FATAL" || $2 == "STOPPED" || $2 == "BACKOFF" { print $1 }')

    if [[ -n "$unhealthy" ]]; then
        log "Found unhealthy workers on ${host}: $(echo "$unhealthy" | tr '\n' ' ')"

        # Attempt restart
        restart_list=$(echo "$unhealthy" | tr '\n' ' ')
        log "Restarting: $restart_list"
        ssh $SSH_OPTS "${SSH_USER}@${host}" "supervisorctl start $restart_list 2>&1" 2>/dev/null || true

        # Wait for processes to stabilise
        sleep "$RESTART_WAIT_SECONDS"

        # Re-check status
        raw_status=$(ssh $SSH_OPTS "${SSH_USER}@${host}" "supervisorctl status 2>/dev/null" 2>/dev/null) || {
            log "ERROR: SSH to $host failed on re-check"
            continue
        }
        parsed=$(echo "$raw_status" | parse_status)

        # Check what's still unhealthy after restart
        still_unhealthy=$(echo "$parsed" | awk -F'\t' '$2 == "FATAL" || $2 == "STOPPED" || $2 == "BACKOFF" { print $1 }')
        if [[ -n "$still_unhealthy" ]]; then
            log "ALERT: Workers still unhealthy on ${host} after restart: $(echo "$still_unhealthy" | tr '\n' ' ')"
        else
            log "All workers recovered on $host after restart"
        fi
    else
        log "All workers healthy on $host"
    fi

    # ── Push metrics to Pushgateway ──────────────────────────────────
    metrics=""
    metrics+="# TYPE supervisor_host_reachable gauge\n"
    metrics+="# HELP supervisor_host_reachable Whether SSH to the host succeeded\n"
    metrics+="supervisor_host_reachable 1\n"
    metrics+="# TYPE supervisor_worker_up gauge\n"
    metrics+="# HELP supervisor_worker_up 1 if the supervisor worker is RUNNING, 0 otherwise\n"

    total=0
    running=0
    fatal=0

    while IFS=$'\t' read -r proc_name state; do
        app=$(extract_app "$proc_name")
        worker=$(extract_worker "$proc_name" "$app")
        total=$((total + 1))

        case "$state" in
            RUNNING|STARTING) up=1; running=$((running + 1)) ;;
            *)                up=0; fatal=$((fatal + 1)) ;;
        esac

        metrics+="supervisor_worker_up{app=\"${app}\",worker=\"${worker}\"} ${up}\n"
    done <<< "$parsed"

    metrics+="# TYPE supervisor_workers_total gauge\n"
    metrics+="# HELP supervisor_workers_total Total number of supervisor workers\n"
    metrics+="supervisor_workers_total ${total}\n"
    metrics+="# TYPE supervisor_workers_running gauge\n"
    metrics+="# HELP supervisor_workers_running Number of RUNNING supervisor workers\n"
    metrics+="supervisor_workers_running ${running}\n"
    metrics+="# TYPE supervisor_workers_fatal gauge\n"
    metrics+="# HELP supervisor_workers_fatal Number of FATAL/STOPPED/BACKOFF workers\n"
    metrics+="supervisor_workers_fatal ${fatal}\n"
    metrics+="# TYPE supervisor_check_timestamp_seconds gauge\n"
    metrics+="# HELP supervisor_check_timestamp_seconds Unix timestamp of last successful check\n"
    metrics+="supervisor_check_timestamp_seconds $(date +%s)\n"

    echo -e "$metrics" | curl -sf --data-binary @- \
        "${PUSHGATEWAY_URL}/metrics/job/supervisor/host/${host}" >/dev/null 2>&1 || {
        log "ERROR: Failed to push metrics to Pushgateway for $host"
    }

    log "Pushed metrics for $host: ${running}/${total} running, ${fatal} fatal"
done

log "Check complete"
