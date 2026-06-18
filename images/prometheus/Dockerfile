FROM prom/prometheus:v3.12.0
COPY --chmod=755 images/prometheus/entrypoint.sh /entrypoint.sh
COPY images/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY onprem/alerts/ /etc/prometheus/alerts/
ENTRYPOINT ["/entrypoint.sh"]
# --config.file is injected by entrypoint.sh after EXEMPTION_API_HOST substitution.
CMD ["--storage.tsdb.path=/prometheus"]
