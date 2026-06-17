# Build the Dell PowerStore metrics exporter (Apache-2.0,
# github.com/dell/powerstore-metrics-exporter). The upstream source is cloned
# at build time from a PINNED commit, so the image builds from a clean checkout
# with no manually-cloned powerstore-monitor/ prerequisite. Crown patches in
# powerstore-exporter/ overwrite/extend upstream files before the build.
FROM golang:alpine AS builder
RUN apk add --no-cache git
# Pinned to tag powerstore-metrics-exporter_v1.0.3. Bump deliberately (and
# re-check the COPY targets below still exist) when taking a newer upstream.
ARG POWERSTORE_MONITOR_REF=564df4d66274f318092529986288baeca12ec798
WORKDIR /build
RUN git clone https://github.com/dell/powerstore-metrics-exporter . \
 && git checkout "${POWERSTORE_MONITOR_REF}"
COPY powerstore-exporter/api.go   collector/client/api.go
COPY powerstore-exporter/alert.go collector/generalCollector/alert.go
COPY powerstore-exporter/route.go route/route.go
RUN go build -o powerstore_exporter .

FROM alpine:latest
RUN apk add --no-cache ca-certificates
WORKDIR /powerstore_exporter
COPY --from=builder /build/powerstore_exporter .
COPY powerstore-exporter/entrypoint.sh .
RUN sed -i 's/\r//' entrypoint.sh && chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
