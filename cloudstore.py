# -*- coding: utf-8 -*-
"""
Cloud storage: push documents and exports out of Seam and into storage the
company already owns.

Two shapes cover the field without an SDK:

  s3      AWS Signature Version 4 over plain HTTPS. Works with Amazon S3 and
          every S3-compatible service - MinIO, Backblaze B2, Wasabi, Hetzner,
          DigitalOcean Spaces, Cloudflare R2 - because they all implement the
          same signing scheme.
  webdav  HTTP PUT with basic auth: Nextcloud, ownCloud, a NAS, any WebDAV
          share.

SigV4 is an HMAC-SHA256 chain, so `hmac` and `hashlib` are the whole dependency
list. Configure in ".cloud_config" or SEAM_CLOUD_* environment variables.
"""
import os
import json
import hmac
import base64
import hashlib
import threading
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, ".cloud_config")
PROVIDERS = ("s3", "webdav")
SECRET_FIELDS = ("secret_key", "password")


def config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if os.environ.get("SEAM_CLOUD_PROVIDER"):
        cfg = {"provider": os.environ["SEAM_CLOUD_PROVIDER"],
               "endpoint": os.environ.get("SEAM_CLOUD_ENDPOINT", ""),
               "region": os.environ.get("SEAM_CLOUD_REGION", "us-east-1"),
               "bucket": os.environ.get("SEAM_CLOUD_BUCKET", ""),
               "prefix": os.environ.get("SEAM_CLOUD_PREFIX", "seam/"),
               "access_key": os.environ.get("SEAM_CLOUD_ACCESS_KEY", ""),
               "secret_key": os.environ.get("SEAM_CLOUD_SECRET_KEY", ""),
               "username": os.environ.get("SEAM_CLOUD_USERNAME", ""),
               "password": os.environ.get("SEAM_CLOUD_PASSWORD", "")}
    if cfg.get("provider") in PROVIDERS:
        return cfg
    return None


def enabled():
    return config() is not None


def missing_fields(cfg):
    if not cfg:
        return ["provider"]
    need = {"s3": ["endpoint", "bucket", "access_key", "secret_key"],
            "webdav": ["endpoint"]}[cfg["provider"]]
    return [f for f in need if not cfg.get(f)]


def public_config(cfg):
    if not cfg:
        return None
    out = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
    out["secrets_set"] = {f: bool(cfg.get(f)) for f in SECRET_FIELDS if cfg.get(f)}
    out["missing"] = missing_fields(cfg)
    out["ready"] = not out["missing"]
    return out


class CloudError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  AWS Signature Version 4
# --------------------------------------------------------------------------- #
UNRESERVED = "-_.~"


def _quote(path):
    """S3 wants each path segment percent-encoded, but the separators kept."""
    return "/".join(urllib.parse.quote(seg, safe=UNRESERVED) for seg in path.split("/"))


def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret, date, region, service):
    k = _sign(("AWS4" + secret).encode("utf-8"), date)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def sigv4_headers(cfg, method, key, payload, content_type="application/octet-stream", now=None):
    """The Authorization header S3 expects, computed from scratch."""
    now = now or datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    region = cfg.get("region") or "us-east-1"
    parts = urllib.parse.urlsplit(cfg["endpoint"])
    host = parts.netloc
    # Path-style addressing: works on MinIO and every S3 clone, not only AWS.
    canonical_uri = _quote("/%s/%s" % (cfg["bucket"].strip("/"), key.lstrip("/")))
    payload_hash = hashlib.sha256(payload or b"").hexdigest()

    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    if content_type:
        headers["content-type"] = content_type
    signed = ";".join(sorted(headers))
    canonical_headers = "".join("%s:%s\n" % (k, headers[k].strip()) for k in sorted(headers))
    canonical_request = "\n".join([method, canonical_uri, "", canonical_headers, signed,
                                   payload_hash])
    scope = "%s/%s/s3/aws4_request" % (date, region)
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(cfg["secret_key"], date, region, "s3"),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = dict(headers)
    out["Authorization"] = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                           % (cfg["access_key"], scope, signed, signature))
    return out, "%s://%s%s" % (parts.scheme, host, canonical_uri)


# --------------------------------------------------------------------------- #
#  Upload
# --------------------------------------------------------------------------- #
def put_now(name, data, content_type="application/octet-stream", cfg=None):
    """Upload synchronously; returns (ok, detail). Used by the test button so
    the operator sees exactly what the storage answered."""
    cfg = cfg or config()
    if not cfg:
        return False, "no provider configured"
    miss = missing_fields(cfg)
    if miss:
        return False, "missing: " + ", ".join(miss)
    if isinstance(data, str):
        data = data.encode("utf-8")
    key = (cfg.get("prefix") or "").strip("/")
    key = ("%s/%s" % (key, name.lstrip("/"))) if key else name.lstrip("/")

    try:
        if cfg["provider"] == "s3":
            headers, url = sigv4_headers(cfg, "PUT", key, data, content_type)
            req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
        else:
            base = cfg["endpoint"].rstrip("/")
            url = "%s/%s" % (base, _quote(key))
            headers = {"content-type": content_type}
            if cfg.get("username"):
                token = base64.b64encode(
                    ("%s:%s" % (cfg["username"], cfg.get("password") or "")).encode()).decode()
                headers["Authorization"] = "Basic " + token
            req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, "%s %s" % (r.status, url)
    except urllib.error.HTTPError as e:
        return False, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:200])
    except Exception as e:
        return False, str(e)[:200]


def put(name, data, content_type="application/octet-stream"):
    """Fire and forget, off the request path."""
    if not enabled():
        return
    threading.Thread(target=put_now, args=(name, data, content_type), daemon=True).start()
