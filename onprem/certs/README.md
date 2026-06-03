# certs/

Place your corporate CA certificate here as `corporate-ca.crt` in **PEM format**.

This directory is mounted read-only into the blackbox-exporter container at
`/etc/blackbox_exporter/certs/` and referenced by the `https_cert` module in
`blackbox.yml` so it can validate internal TLS certificates signed by the corporate CA.

## Exporting the CA certificate

**From Windows Certificate Manager (certmgr.msc):**
1. Find the root CA under Trusted Root Certification Authorities
2. Right-click → All Tasks → Export
3. Choose "Base-64 encoded X.509 (.CER)" format
4. Save as `corporate-ca.crt` in this directory

**If you have it in DER format (.cer) and need to convert:**
```
openssl x509 -inform der -in corporate-ca.cer -out corporate-ca.crt
```

**Verify the file is PEM (should start with `-----BEGIN CERTIFICATE-----`):**
```
head -1 corporate-ca.crt
```

## After placing the file

Restart the blackbox exporter:
```
docker compose restart blackbox-exporter
```

Then verify with the debug endpoint:
```
curl "http://poc-containers:9514/probe?target=https://your-internal-url&module=https_cert"
```
