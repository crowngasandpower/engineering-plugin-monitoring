FROM prom/prometheus:v3.12.0
COPY --chmod=755 images/prometheus/entrypoint.sh /entrypoint.sh
COPY images/prometheus/prometheus.yml /etc/prometheus/prometheus.yml
COPY onprem/alerts/ /etc/prometheus/alerts/
ENTRYPOINT ["/entrypoint.sh"]
CMD ["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus"]
