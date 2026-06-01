"""Crown File Gateway — S3-compatible API over on-premise filesystems.

Each S3 "bucket" maps to a local mount point. Laravel apps use the native
S3 Flysystem driver pointed at this gateway. When apps move to real AWS,
swap the endpoint to S3 — zero code changes.

Implements the subset of S3 that Flysystem actually calls:
  HeadBucket, GetObject, PutObject, HeadObject, DeleteObject, ListObjectsV2

Security: full AWS Signature V4 verification on every request.
"""

import hashlib
import hmac
import logging
import mimetypes
import os
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from xml.etree.ElementTree import Element, SubElement, tostring

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("file-gateway")

ACCESS_KEY = os.environ.get("FILE_GATEWAY_ACCESS_KEY", "crown-gateway")
SECRET_KEY = os.environ.get("FILE_GATEWAY_SECRET_KEY", "")
REGION = os.environ.get("FILE_GATEWAY_REGION", "eu-west-2")

BUCKETS: dict[str, dict] = {
    # ── Genus (gas) ─────────────────────────────────────────────
    "genus-docstore": {
        "path": "/mnt/genus/production/DocStore",
        "mode": "rw",
    },
    "genus-gps-imports": {
        "path": "/mnt/genus/production/HubFiles/Shippers/NGSS/GPS Imports",
        "mode": "rw",
    },
    "genus-hub-files": {
        "path": "/mnt/genus/production/HubFiles",
        "mode": "rw",
    },
    "genus-auddis": {
        "path": "/mnt/genus/production/OpenAccounts/DD Various/E Contract DD Set ups",
        "mode": "rw",
    },

    # ── Genus (electricity) ─────────────────────────────────────
    "genus-elec-docstore": {
        "path": "/mnt/genus-elec/production/docstore",
        "mode": "rw",
    },

    # ── App local storage (NFS share from apps-prod-shares) ─────
    "ces-uploads": {
        "path": "/mnt/crown-storage/ces/storage/app/public/uploads",
        "mode": "ro",
    },
    "gps-quotes": {
        "path": "/mnt/crown-storage/gps/storage/app/public/uploads/quotes",
        "mode": "ro",
    },
    "eps-uploads": {
        "path": "/mnt/crown-storage/eps/storage/app/public/uploads",
        "mode": "ro",
    },
    "synergy-avatars": {
        "path": "/mnt/crown-storage/synergy/storage/app/public/uploads/avatars",
        "mode": "ro",
    },
    "doc-master": {
        "path": "/mnt/crown-storage/doc-master/storage/app/private",
        "mode": "rw",
    },
    "doc-master-elec": {
        "path": "/mnt/crown-storage/doc-master-elec/storage/app/private",
        "mode": "rw",
    },
    "postmaster": {
        "path": "/mnt/crown-storage/postmaster",
        "mode": "rw",
    },

    # ── File server shares ──────────────────────────────────────
    "gdrive": {
        "path": "/mnt/gdrive",
        "mode": "rw",
    },
    "pdrive": {
        "path": "/mnt/pdrive",
        "mode": "rw",
    },
}


# ---------------------------------------------------------------------------
# AWS Signature V4 verification
# ---------------------------------------------------------------------------

_AUTH_RE = re.compile(
    r"AWS4-HMAC-SHA256\s+"
    r"Credential=(?P<access_key>[^/]+)/(?P<date>\d{8})/(?P<region>[^/]+)/(?P<service>[^/]+)/aws4_request,\s*"
    r"SignedHeaders=(?P<signed_headers>[^,]+),\s*"
    r"Signature=(?P<signature>[0-9a-f]+)"
)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _derive_signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _canonical_query_string(query_string: str) -> str:
    if not query_string:
        return ""
    params = urllib.parse.parse_qsl(query_string, keep_blank_values=True)
    params.sort(key=lambda p: (p[0], p[1]))
    return "&".join(
        urllib.parse.quote(k, safe="-_.~") + "=" + urllib.parse.quote(v, safe="-_.~")
        for k, v in params
    )


def _canonical_uri(path: str) -> str:
    segments = path.split("/")
    return "/".join(
        urllib.parse.quote(urllib.parse.unquote(s), safe="-_.~")
        for s in segments
    )


async def _verify_sig_v4(request: Request, body: bytes | None = None) -> Response | None:
    if not SECRET_KEY:
        return _s3_error(500, "InternalError", "FILE_GATEWAY_SECRET_KEY not configured")

    auth_header = request.headers.get("authorization", "")
    m = _AUTH_RE.match(auth_header)
    if not m:
        return _s3_error(403, "AccessDenied", "Missing or malformed AWS4-HMAC-SHA256 authorization")

    access_key = m.group("access_key")
    date_stamp = m.group("date")
    region = m.group("region")
    service = m.group("service")
    signed_headers_str = m.group("signed_headers")
    provided_sig = m.group("signature")

    if not hmac.compare_digest(access_key, ACCESS_KEY):
        return _s3_error(403, "InvalidAccessKeyId", "The access key does not exist")

    signed_header_names = signed_headers_str.split(";")
    canonical_headers = ""
    for name in signed_header_names:
        if name == "host":
            val = request.headers.get("host", "")
        else:
            val = request.headers.get(name, "")
        canonical_headers += f"{name}:{val.strip()}\n"

    payload_hash = request.headers.get("x-amz-content-sha256", "")
    if not payload_hash:
        if body is not None:
            payload_hash = hashlib.sha256(body).hexdigest()
        else:
            payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_request = "\n".join([
        request.method,
        _canonical_uri(request.url.path),
        _canonical_query_string(request.url.query or ""),
        canonical_headers,
        signed_headers_str,
        payload_hash,
    ])

    amz_date = request.headers.get("x-amz-date", "")
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"

    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _derive_signing_key(SECRET_KEY, date_stamp, region, service)
    computed_sig = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_sig, provided_sig):
        log.warning("Sig V4 mismatch for %s %s (key=%s)", request.method, request.url.path, access_key)
        return _s3_error(403, "SignatureDoesNotMatch", "The request signature does not match")

    return None


# ---------------------------------------------------------------------------
# Path resolution with traversal protection
# ---------------------------------------------------------------------------

def _resolve(bucket_name: str, key: str, *, require_write: bool = False):
    cfg = BUCKETS.get(bucket_name)
    if cfg is None:
        return None, _s3_error(404, "NoSuchBucket", f"Bucket '{bucket_name}' not found")
    if require_write and cfg["mode"] == "ro":
        return None, _s3_error(403, "AccessDenied", f"Bucket '{bucket_name}' is read-only")

    base = Path(cfg["path"])
    clean = PurePosixPath(key)
    if ".." in clean.parts:
        return None, _s3_error(400, "InvalidArgument", "Path traversal not allowed")

    resolved = base / clean
    try:
        resolved.resolve().relative_to(base.resolve())
    except ValueError:
        return None, _s3_error(400, "InvalidArgument", "Path traversal not allowed")

    return resolved, None


# ---------------------------------------------------------------------------
# S3 XML helpers
# ---------------------------------------------------------------------------

def _s3_error(status: int, code: str, message: str) -> Response:
    root = Element("Error")
    SubElement(root, "Code").text = code
    SubElement(root, "Message").text = message
    return Response(
        tostring(root, xml_declaration=True, encoding="unicode"),
        status_code=status,
        media_type="application/xml",
    )


def _s3_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _etag(data: bytes) -> str:
    return f'"{hashlib.md5(data).hexdigest()}"'


def _file_etag(path: Path) -> str:
    if path.stat().st_size > 50_000_000:
        return f'"{hashlib.md5(str(path.stat().st_mtime).encode()).hexdigest()}"'
    return _etag(path.read_bytes())


# ---------------------------------------------------------------------------
# S3 operations
# ---------------------------------------------------------------------------

async def handle_health(request: Request) -> Response:
    available = {}
    for name, cfg in BUCKETS.items():
        available[name] = Path(cfg["path"]).is_dir()
    import json
    return Response(json.dumps({"status": "ok", "buckets": available}), media_type="application/json")


async def handle_bucket(request: Request) -> Response:
    bucket = request.path_params["bucket"]
    key = request.path_params.get("key", "")

    body = None
    if request.method == "PUT":
        body = await request.body()

    err = await _verify_sig_v4(request, body)
    if err:
        return err

    if request.method == "HEAD" and not key:
        return _head_bucket(bucket)
    if request.method == "GET" and not key:
        return _list_objects_v2(request, bucket)
    if request.method == "HEAD" and key:
        return _head_object(bucket, key)
    if request.method == "GET" and key:
        return _get_object(bucket, key)
    if request.method == "PUT" and key:
        return _put_object(bucket, key, body)
    if request.method == "DELETE" and key:
        return _delete_object(bucket, key)

    return _s3_error(405, "MethodNotAllowed", f"{request.method} not supported")


def _head_bucket(bucket: str) -> Response:
    cfg = BUCKETS.get(bucket)
    if not cfg or not Path(cfg["path"]).is_dir():
        return _s3_error(404, "NoSuchBucket", f"Bucket '{bucket}' not found")
    return Response(status_code=200, headers={"x-amz-bucket-region": REGION})


def _head_object(bucket: str, key: str) -> Response:
    resolved, err = _resolve(bucket, key)
    if err:
        return err
    if not resolved.is_file():
        return _s3_error(404, "NoSuchKey", f"Key '{key}' not found")

    stat = resolved.stat()
    content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"

    return Response(
        status_code=200,
        headers={
            "Content-Length": str(stat.st_size),
            "Content-Type": content_type,
            "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(stat.st_mtime)),
            "ETag": _file_etag(resolved),
        },
    )


def _get_object(bucket: str, key: str) -> Response:
    resolved, err = _resolve(bucket, key)
    if err:
        return err
    if not resolved.is_file():
        return _s3_error(404, "NoSuchKey", f"Key '{key}' not found")

    stat = resolved.stat()
    content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"

    def stream():
        with open(resolved, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        stream(),
        media_type=content_type,
        headers={
            "Content-Length": str(stat.st_size),
            "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(stat.st_mtime)),
            "ETag": _file_etag(resolved),
        },
    )


def _put_object(bucket: str, key: str, body: bytes) -> Response:
    resolved, err = _resolve(bucket, key, require_write=True)
    if err:
        return err

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(body)

    log.info("PUT %s/%s (%d bytes)", bucket, key, len(body))
    return Response(
        status_code=200,
        headers={"ETag": _etag(body)},
    )


def _delete_object(bucket: str, key: str) -> Response:
    resolved, err = _resolve(bucket, key, require_write=True)
    if err:
        return err

    if resolved.is_file():
        resolved.unlink()
        log.info("DELETE %s/%s", bucket, key)

    return Response(status_code=204)


def _list_objects_v2(request: Request, bucket: str) -> Response:
    cfg = BUCKETS.get(bucket)
    if not cfg:
        return _s3_error(404, "NoSuchBucket", f"Bucket '{bucket}' not found")

    base = Path(cfg["path"])
    if not base.is_dir():
        return _s3_error(404, "NoSuchBucket", f"Bucket '{bucket}' path unavailable")

    prefix = request.query_params.get("prefix", "")
    delimiter = request.query_params.get("delimiter", "")
    max_keys = int(request.query_params.get("max-keys", "1000"))

    search_dir = base / prefix if prefix.endswith("/") else base / PurePosixPath(prefix).parent
    prefix_str = prefix

    root = Element("ListBucketResult")
    root.set("xmlns", "http://s3.amazonaws.com/doc/2006-03-01/")
    SubElement(root, "Name").text = bucket
    SubElement(root, "Prefix").text = prefix
    SubElement(root, "MaxKeys").text = str(max_keys)
    SubElement(root, "IsTruncated").text = "false"
    SubElement(root, "KeyCount").text = "0"

    if delimiter:
        SubElement(root, "Delimiter").text = delimiter

    if not search_dir.is_dir():
        return Response(
            tostring(root, xml_declaration=True, encoding="unicode"),
            media_type="application/xml",
        )

    contents = []
    common_prefixes = set()

    try:
        for child in sorted(search_dir.rglob("*") if not delimiter else search_dir.iterdir()):
            try:
                rel = child.relative_to(base).as_posix()
            except ValueError:
                continue

            if not rel.startswith(prefix_str):
                continue

            if delimiter and child.is_dir():
                common_prefixes.add(rel + "/")
                continue

            if child.is_file():
                contents.append((rel, child))
                if len(contents) >= max_keys:
                    break
    except PermissionError:
        pass

    for rel, child in contents:
        stat = child.stat()
        entry = SubElement(root, "Contents")
        SubElement(entry, "Key").text = rel
        SubElement(entry, "LastModified").text = _s3_timestamp(stat.st_mtime)
        SubElement(entry, "Size").text = str(stat.st_size)
        SubElement(entry, "ETag").text = f'"{hashlib.md5(str(stat.st_mtime).encode()).hexdigest()}"'
        SubElement(entry, "StorageClass").text = "STANDARD"

    for cp in sorted(common_prefixes):
        entry = SubElement(root, "CommonPrefixes")
        SubElement(entry, "Prefix").text = cp

    root.find("KeyCount").text = str(len(contents))

    return Response(
        tostring(root, xml_declaration=True, encoding="unicode"),
        media_type="application/xml",
    )


# ---------------------------------------------------------------------------
# Routing — path-style S3: /{bucket} and /{bucket}/{key}
# ---------------------------------------------------------------------------

app = Starlette(
    routes=[
        Route("/health", handle_health, methods=["GET"]),
        Route("/{bucket}/{key:path}", handle_bucket, methods=["GET", "PUT", "HEAD", "DELETE"]),
        Route("/{bucket}", handle_bucket, methods=["GET", "HEAD"]),
    ],
)
