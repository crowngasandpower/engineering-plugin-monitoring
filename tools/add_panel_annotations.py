"""
Add panelUrl annotation to alert rules in alert-rules.yml.
Removes any previously injected __dashboardUid__ / __panelId__ lines (reserved
Grafana names — stripped from notification payloads) and replaces with a plain
panelUrl annotation that DOES appear in the PD firing text.

Run once, then delete this script.
"""
import re
from pathlib import Path

# Maps rule uid → (dashboardUID, panelId)
PANEL_MAP = {
    # Temperature
    "room-temp-warning":                ("server-room-temperature", 4),
    "room-temp-critical":               ("server-room-temperature", 4),
    "server-intake-temp-warning":       ("server-room-temperature", 5),
    "server-intake-temp-critical":      ("server-room-temperature", 5),
    # iDRAC
    "idrac-cpu-health":                 ("idrac-crown", 4),
    "idrac-psu-health":                 ("idrac-crown", 12),
    "idrac-psu-no-input":               ("idrac-crown", 14),
    "idrac-fan-health":                 ("idrac-crown", 32),
    "idrac-fan-rpm-low":                ("idrac-crown", 31),
    "idrac-storage-drive-health":       ("idrac-crown", 41),
    "idrac-storage-controller-health":  ("idrac-crown", 42),
    "idrac-storage-volume-health":      ("idrac-crown", 43),
    "idrac-system-power-off":           ("idrac-crown", 2),
    "idrac-system-health":              ("idrac-crown", 3),
    "idrac-cpu-temp":                   ("idrac-crown", 25),
    "idrac-exhaust-temp":               ("idrac-crown", 25),
    "idrac-nic-down":                   ("idrac-crown", 51),
    "idrac-memory-health":              ("idrac-crown", 44),
    # PowerStore
    "powerstore-major-alert":           ("powerstore-crown", 49),
    "powerstore-capacity":              ("powerstore-crown", 11),
    "powerstore-drive-wear":            ("powerstore-crown", 42),
    "powerstore-eth-port-down":         ("powerstore-crown", 49),
    "powerstore-eth-port-errors":       ("powerstore-crown", 49),
    "powerstore-metro-status":          ("powerstore-crown", 49),
    "powerstore-metro-witness":         ("powerstore-crown", 49),
    # VMware
    "vcenter-host-cpu-high":            ("VMwExClV", 14),
    "vcenter-cluster-mem-high":         ("VMwExClV", 16),
    "vcenter-datastore-space-low":      ("VMwExDsV", 100),
    "vcenter-datastore-space-critical": ("VMwExDsV", 100),
    "vcenter-host-mem-balloon":         ("VMwExHoV", 16),
    "vcenter-host-mem-swap":            ("VMwExHoV", 16),
    "vcenter-host-cpu-ready":           ("VMwExHoV", 14),
    # Linux
    "linux-disk-warning":               ("vm-disk-capacity", 1),
    "linux-disk-critical":              ("vm-disk-capacity", 1),
    "linux-inode-warning":              ("vm-disk-capacity", 1),
    "linux-cpu-warning":                ("compute-cpu", 1),
    "linux-memory-warning":             ("compute-memory", 1),
    "linux-iowait-warning":             ("compute-iowait", 1),
    "linux-load-warning":               ("infra-detail", 3),
    # Windows
    "windows-disk-warning":             ("vm-disk-capacity", 1),
    "windows-disk-critical":            ("vm-disk-capacity", 1),
    "windows-cpu-warning":              ("compute-cpu", 1),
    "windows-memory-warning":           ("compute-memory", 1),
    # Networking
    "network-wan-latency":              ("crown-network-connectivity", 5),
    "network-wan-packetloss-warning":   ("crown-network-connectivity", 6),
    "network-wan-packetloss-critical":  ("crown-network-connectivity", 6),
    "network-wan-unreachable":          ("crown-network-connectivity", 5),
    "network-site-ping-down":           ("crown-network-connectivity", 5),
    "network-unifi-console-offline":    ("crown-unifi-network", 2),
    "network-unifi-offline":            ("crown-unifi-network", 10),
    "network-switch-down":              ("crown-dell-switches", 3),
    "network-switch-exporter-down":     ("crown-dell-switches", 1),
    "network-switch-unreachable":       ("crown-dell-switches", 1),
    "network-switch-errors":            ("crown-dell-switches", 20),
    # Certificates
    "cert-expiring-soon":               ("cert-mon", 30),
    "cert-expired":                     ("cert-mon", 30),
    "cert-expiring-critical":           ("cert-mon", 30),
    "cert-probe-failure":               ("cert-mon", 50),
    "cert-revoked":                     ("cert-mon", 40),
    "cert-weak-signature":              ("cert-mon", 41),
    # Containers
    "container-crash-loop":             ("docker-containers", 5),
    "container-memory-limit":           ("docker-containers", 8),
    "container-cpu-throttle":           ("docker-containers", 6),
    # Applications
    "http-app-down":                    ("infrastructure", 15),
    "http-app-slow":                    ("infrastructure", 15),
    "mysql-connections-high":           ("infrastructure", 5),
    "mysql-slow-queries":               ("infrastructure", 5),
    "laravel-error-rate-spike":         ("infrastructure", 2),
    "genus-db-down":                    ("genus-monitoring", 1),
    "hub-file-age":                     ("genus-monitoring", 40),
    "ewelcome-failures":                ("genus-monitoring", 64),
    "oldest-in-queue":                  ("genus-monitoring", 99),
    "replica-lag":                      ("genus-details", 22),
    "supervisor-worker-fatal":          ("infra-detail", 7),
    "supervisor-host-unreachable":      ("infra-detail", 7),
    "supervisor-check-stale":           ("infra-detail", 7),
}

_RESERVED = {"__dashboardUid__", "__panelId__"}

TARGET = Path(r"c:\Users\john.simmons\OneDrive - Crown Gas & Power\Documents\crown\monitoring\images\grafana\alert-rules.yml")

lines       = TARGET.read_text(encoding="utf-8").splitlines()
out         = []
i           = 0
current_uid = None
added       = 0

while i < len(lines):
    line = lines[i]

    # Strip any previously-injected reserved annotations (they don't appear in notifications)
    if any(f"{k}:" in line for k in _RESERVED):
        i += 1
        continue

    # Track current rule uid
    m = re.match(r'\s+- uid:\s+(\S+)', line)
    if m:
        current_uid = m.group(1)

    # Inject panelUrl right after the summary annotation
    if current_uid in PANEL_MAP:
        m2 = re.match(r'(\s+)summary:', line)
        if m2:
            indent = m2.group(1)
            out.append(line)
            i += 1
            # Skip any existing panelUrl line (idempotency)
            while i < len(lines) and 'panelUrl:' in lines[i]:
                i += 1
            dashboard_uid, panel_id = PANEL_MAP[current_uid]
            panel_path = f"/d/{dashboard_uid}?orgId=1&viewPanel={panel_id}"
            out.append(f'{indent}panelUrl: "{panel_path}"')
            added += 1
            current_uid = None
            continue

    out.append(line)
    i += 1

result = "\n".join(out) + "\n"
TARGET.write_text(result, encoding="utf-8")
print(f"Done — injected panelUrl into {added} rules")
