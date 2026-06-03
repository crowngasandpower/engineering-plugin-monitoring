FROM alpine:latest AS cert-builder
RUN apk add --no-cache ca-certificates
COPY certs/ /tmp/extra-certs/
RUN find /tmp/extra-certs -name "*.crt" -exec cp {} /usr/local/share/ca-certificates/ \; \
    && update-ca-certificates

FROM prom/blackbox-exporter:latest
COPY --from=cert-builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
