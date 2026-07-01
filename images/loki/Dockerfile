FROM grafana/loki:3.5.0
# The base image's default CMD is `-config.file=/etc/loki/local-config.yaml`.
# Copying to any other path (the old /etc/loki/config.yml) left our config inert
# — Loki silently ran on its stock defaults, so retention + query guardrails here
# never took effect. Overwrite the path Loki actually loads.
COPY loki-config.yml /etc/loki/local-config.yaml
