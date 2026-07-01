# Crown monitoring — Ansible

Config-management rollout for the Prometheus exporters on Linux hosts. This replaces
hand-running [`../scripts/install-node-exporter.sh`](../scripts/install-node-exporter.sh)
on each box — the role is idempotent and version-pinned, so re-running it is the way you
both install and upgrade.

> **Windows hosts are not managed here.** `windows_exporter` is rolled out via Group
> Policy (MSI Software Installation + a GPO firewall rule), which avoids needing WinRM
> enabled fleet-wide. See [`../scripts/install-windows-exporter.ps1`](../scripts/install-windows-exporter.ps1)
> for the equivalent install args (collector set, `:9182`, firewall scope) to mirror in the GPO.

## Layout

```
ansible.cfg                 # sudo become, yaml output, inventory path
inventory/hosts.ini             # Linux targets (seeded from metrics-proxy routing table)
inventory/group_vars/swarm.yml  # filesystem-collector fs-types-exclude for swarm nodes
playbooks/node-exporter.yml # the rollout play (serial waves)
roles/node_exporter/        # the role
```

## Prerequisites

- Ansible on the control node (run on-net so `crowngp.local` / `192.168.164.x` resolve).
- SSH key auth to each host as a **sudo-capable** user. Set the user with `-u <user>`,
  `ansible_user=` in the inventory, or `group_vars/all.yml`.

## Usage

```bash
cd onprem/ansible

# Dry run first — shows what would change, no writes
ansible-playbook playbooks/node-exporter.yml --check --diff

# Roll out to everything (in 50% waves)
ansible-playbook playbooks/node-exporter.yml

# One host only
ansible-playbook playbooks/node-exporter.yml --limit apps-prod-1
```

## What the role does

Mirrors `install-node-exporter.sh`, made idempotent:

1. Maps `ansible_architecture` → `amd64` / `arm64` / `armv7`.
2. Creates the `node_exporter` system user (in `systemd-journal` for the systemd collector).
3. Downloads + installs the pinned binary to `/usr/local/bin/node_exporter` — **skipped if
   the running binary already reports the target version**, so re-runs are cheap.
4. Templates `/etc/systemd/system/node_exporter.service`, enables + starts it.
5. Probes `http://127.0.0.1:9100/metrics` to confirm it's live.

## Upgrading the exporter

Bump `node_exporter_version` in [`roles/node_exporter/defaults/main.yml`](roles/node_exporter/defaults/main.yml)
(keep it in step with the shell script) and re-run the playbook. The version check triggers a
re-download, and the binary-install handler restarts the service.

## Adding a host

Add it under `[linux]` in `inventory/hosts.ini`. If it's a Docker Swarm node, add it under
`[swarm]` instead so it picks up the `fs-types-exclude` override (cifs/nfs4/overlay) — without
that the filesystem collector can hang and trip the scrape timeout.
