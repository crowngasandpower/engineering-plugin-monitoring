FROM grafana/grafana:13.0.2

# Python is needed by the alert rule provisioner (provision-alerts.py).
# We use API-based provisioning instead of Grafana's built-in file provisioner
# so that alert rules are editable in the UI (Grafana 13 locks file-provisioned
# rules — see provision-alerts.py for the full explanation).
USER root
RUN apk add --no-cache python3 py3-yaml py3-psycopg2
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER grafana

COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /opt/grafana-dashboards/
COPY provision-alerts.py /opt/provision-alerts.py
COPY alert-rules.yml /opt/alert-rules.yml

ENTRYPOINT ["/entrypoint.sh"]
