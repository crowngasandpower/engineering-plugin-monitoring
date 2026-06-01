# Build the Dell PowerStore metrics exporter from the cloned source tree.
# Prerequisite: git clone https://github.com/dell/powerstore-metrics-exporter powerstore-monitor
# Crown patches are in powerstore-exporter/ and overwrite upstream files before build.
FROM golang:alpine AS builder
WORKDIR /build
COPY powerstore-monitor/ .
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
