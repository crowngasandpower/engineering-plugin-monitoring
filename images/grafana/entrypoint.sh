#!/bin/sh
set -e

# Step 1: clear any file-provenance alert rule records from the database so
# the API provisioner can take ownership (Grafana 13 hard-blocks changes to
# file-provisioned rules via the UI or any API).
python3 /opt/provision-alerts.py --migrate

# Step 2: start alert rule provisioner in the background — waits for Grafana
# to be ready, then creates/updates all rule groups from the alert-rules/ files.
python3 /opt/provision-alerts.py --provision &

# Step 3: hand off to the normal Grafana entrypoint.
exec /run.sh "$@"
