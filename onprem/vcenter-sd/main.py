import json
import os
import re
import ssl
import time
from datetime import datetime, timezone

import requests
from pyVmomi import vim
from pyVim.connect import SmartConnect, Disconnect

VCENTER_HOST = os.environ['VCENTER_HOST']
VCENTER_USER = os.environ['VCENTER_USER']
VCENTER_PASSWORD = os.environ['VCENTER_PASSWORD']
VCENTER_IGNORE_SSL = os.environ.get('VCENTER_IGNORE_SSL', 'false').lower() == 'true'
NODE_EXPORTER_PORT = int(os.environ.get('NODE_EXPORTER_PORT', '9100'))
WINDOWS_EXPORTER_PORT = int(os.environ.get('WINDOWS_EXPORTER_PORT', '9182'))
SD_FILE = '/sd/linux-hosts.json'
WINDOWS_SD_FILE = '/sd/windows-hosts.json'
PUSHGATEWAY_URL = os.environ.get('PUSHGATEWAY_URL', 'http://pushgateway:9091')
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL_SECONDS', '60'))
CADVISOR_PORT = int(os.environ.get('CADVISOR_PORT', '8080'))
CADVISOR_SD_FILE = '/sd/cadvisor-hosts.json'


def connect():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if VCENTER_IGNORE_SSL:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return SmartConnect(
        host=VCENTER_HOST,
        user=VCENTER_USER,
        pwd=VCENTER_PASSWORD,
        sslContext=context,
    )


def collect_all_vms(content):
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
    vms = list(view.view)
    view.Destroy()
    return vms


def collect_host_datastores(content):
    """Returns list of (host_name, host_moid, ds_name, ds_moid) for every accessible datastore per host."""
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
    results = []
    for host in view.view:
        for ds in host.datastore:
            results.append((host.name, host._moId, ds.name, ds._moId))
    view.Destroy()
    return results


def _parse_rubrik_timestamp(value):
    """Parse 'Last Backup successfully completed at Tue May 19 23:01:17 UTC 2026' → Unix timestamp."""
    m = re.search(r'at\s+(.+)$', value.strip())
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1).strip(), '%a %b %d %H:%M:%S UTC %Y')
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def poll():
    si = None
    try:
        si = connect()
        content = si.RetrieveContent()

        # Build custom-field key → name map once per poll
        field_map = {f.key: f.name for f in content.customFieldsManager.field}
        backup_key = next((k for k, n in field_map.items() if n == 'Rubrik_LastBackup'), None)
        docker_key = next((k for k, n in field_map.items() if n == 'docker_host'), None)

        # Pass 1: backup times + powered-off for every VM in vCenter
        backup_times = {}
        powered_off = []
        for vm in collect_all_vms(content):
            if vm.runtime.powerState != 'poweredOn':
                powered_off.append(vm.name)
            if backup_key is not None:
                for cv in vm.customValue:
                    if cv.key == backup_key and cv.value:
                        ts = _parse_rubrik_timestamp(cv.value)
                        if ts is not None:
                            backup_times[vm.name] = ts
                        break

        # Pass 2: service-discovery targets for all powered-on VMs
        linux_targets, windows_targets, cadvisor_targets, no_ip = [], [], [], []
        for vm in collect_all_vms(content):
                if vm.runtime.powerState != 'poweredOn':
                    continue
                family = vm.guest and vm.guest.guestFamily
                ip = vm.guest and vm.guest.ipAddress
                hostname = vm.guest and vm.guest.hostName
                if family == 'windowsGuest':
                    port = WINDOWS_EXPORTER_PORT
                    target_list = windows_targets
                else:
                    port = NODE_EXPORTER_PORT
                    target_list = linux_targets
                if ip:
                    target_list.append({
                        'targets': [f'{hostname or ip}:{port}'],
                        'labels': {'host': vm.name},
                    })
                else:
                    no_ip.append(vm.name)

                if docker_key is not None and ip:
                    for cv in vm.customValue:
                        if cv.key == docker_key and cv.value.lower() == 'true':
                            cadvisor_targets.append({
                                'targets': [f'{hostname or ip}:{CADVISOR_PORT}'],
                                'labels': {'host': vm.name},
                            })
                            break

        with open(SD_FILE, 'w') as f:
            json.dump(linux_targets, f, indent=2)
        with open(WINDOWS_SD_FILE, 'w') as f:
            json.dump(windows_targets, f, indent=2)
        with open(CADVISOR_SD_FILE, 'w') as f:
            json.dump(cadvisor_targets, f, indent=2)

        host_datastores = collect_host_datastores(content)

        print(f'SD updated: {len(linux_targets)} Linux, {len(windows_targets)} Windows, {len(cadvisor_targets)} cAdvisor, {len(no_ip)} no IP, {len(powered_off)} powered off, {len(backup_times)} backup times, {len(host_datastores)} host-datastore pairs')
        _push_metrics(no_ip, powered_off, backup_times, host_datastores)

    except Exception as e:
        print(f'Poll error: {e}')
    finally:
        if si:
            Disconnect(si)


def _push_metrics(no_ip_vms, powered_off_vms, backup_times, host_datastores):
    lines = [
        '# HELP vcenter_vm_no_tools_ip VM in monitored folder is powered on but has no VMware Tools IP\n',
        '# TYPE vcenter_vm_no_tools_ip gauge\n',
    ]
    for name in no_ip_vms:
        safe = name.replace('"', '\\"')
        lines.append(f'vcenter_vm_no_tools_ip{{vm_name="{safe}"}} 1\n')

    lines += [
        '# HELP vcenter_vm_powered_off VM in vCenter inventory is powered off\n',
        '# TYPE vcenter_vm_powered_off gauge\n',
    ]
    for name in powered_off_vms:
        safe = name.replace('"', '\\"')
        lines.append(f'vcenter_vm_powered_off{{vm_name="{safe}"}} 1\n')

    lines += [
        '# HELP vcenter_vm_last_backup_timestamp Unix timestamp of last successful Rubrik backup\n',
        '# TYPE vcenter_vm_last_backup_timestamp gauge\n',
    ]
    for name, ts in backup_times.items():
        safe = name.replace('"', '\\"')
        lines.append(f'vcenter_vm_last_backup_timestamp{{vm_name="{safe}"}} {ts}\n')

    vcenter_label = f'{VCENTER_HOST}:443'
    lines += [
        '# HELP vmware_host_datastore_accessible Datastore is accessible from host\n',
        '# TYPE vmware_host_datastore_accessible gauge\n',
    ]
    for host_name, host_moid, ds_name, ds_moid in host_datastores:
        safe_host = host_name.replace('"', '\\"')
        safe_ds = ds_name.replace('"', '\\"')
        lines.append(
            f'vmware_host_datastore_accessible{{vcenter="{vcenter_label}", host="{safe_host}", hostmo="{host_moid}", ds="{safe_ds}", dsmo="{ds_moid}"}} 1\n'
        )

    try:
        requests.delete(f'{PUSHGATEWAY_URL}/metrics/job/vcenter-sd', timeout=5)
        requests.put(
            f'{PUSHGATEWAY_URL}/metrics/job/vcenter-sd',
            data=''.join(lines),
            headers={'Content-Type': 'text/plain'},
            timeout=5,
        )
    except Exception as e:
        print(f'Pushgateway error: {e}')


if __name__ == '__main__':
    while True:
        poll()
        time.sleep(POLL_INTERVAL)
