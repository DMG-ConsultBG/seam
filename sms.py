# -*- coding: utf-8 -*-
"""
Outbound SMS.

Provider-agnostic on purpose: an operator in Bulgaria will not use the same
gateway as one in Portugal, and a self-hosted instance may sit behind a
corporate SMS relay. Three shapes cover essentially every gateway on the
market:

  twilio   POST /Accounts/<sid>/Messages.json, HTTP basic auth, form-encoded
  bearer   POST <url> with {"to","text"} and Authorization: Bearer <key>
  form     POST <url> form-encoded, field names configurable

Configure in ".sms_config" (JSON) or via SEAM_SMS_* environment variables:
    {"provider": "twilio", "account_sid": "...", "auth_token": "...",
     "from": "+3598..."}
    {"provider": "bearer", "url": "https://gw.example/send", "api_key": "...",
     "from": "SEAM"}

Sending is best-effort and always off the request path - a failed SMS must
never fail the business action that triggered it.
"""
import os
import json
import base64
import threading
import urllib.parse
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, ".sms_config")
PROVIDERS = ("twilio", "bearer", "form")
SECRET_FIELDS = ("auth_token", "api_key", "password")


def config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if os.environ.get("SEAM_SMS_PROVIDER"):
        cfg = {
            "provider": os.environ["SEAM_SMS_PROVIDER"],
            "url": os.environ.get("SEAM_SMS_URL", ""),
            "account_sid": os.environ.get("SEAM_SMS_ACCOUNT_SID", ""),
            "auth_token": os.environ.get("SEAM_SMS_AUTH_TOKEN", ""),
            "api_key": os.environ.get("SEAM_SMS_API_KEY", ""),
            "from": os.environ.get("SEAM_SMS_FROM", ""),
            "to_field": os.environ.get("SEAM_SMS_TO_FIELD", ""),
            "text_field": os.environ.get("SEAM_SMS_TEXT_FIELD", ""),
        }
    if cfg.get("provider") in PROVIDERS:
        return cfg
    return None


def enabled():
    return config() is not None


def missing_fields(cfg):
    if not cfg:
        return ["provider"]
    need = {"twilio": ["account_sid", "auth_token", "from"],
            "bearer": ["url", "api_key"],
            "form": ["url"]}[cfg["provider"]]
    return [f for f in need if not cfg.get(f)]


def public_config(cfg):
    if not cfg:
        return None
    out = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
    out["secrets_set"] = {f: bool(cfg.get(f)) for f in SECRET_FIELDS if cfg.get(f)}
    out["missing"] = missing_fields(cfg)
    out["ready"] = not out["missing"]
    return out


def _post(url, data, headers, form=True, timeout=12):
    body = (urllib.parse.urlencode(data) if form
            else json.dumps(data, ensure_ascii=False)).encode("utf-8")
    hdr = {"user-agent": "Seam/1.0",
           "content-type": ("application/x-www-form-urlencoded" if form else "application/json")}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")[:200]


def send_now(to, text, cfg=None):
    """Send synchronously. Returns (ok, detail) - used by the "send a test"
    button, where the operator needs to see the gateway's answer."""
    cfg = cfg or config()
    if not cfg:
        return False, "no provider configured"
    miss = missing_fields(cfg)
    if miss:
        return False, "missing: " + ", ".join(miss)
    to = (to or "").strip()
    if not to:
        return False, "no recipient"
    text = (text or "")[:480]
    try:
        p = cfg["provider"]
        if p == "twilio":
            url = ("https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json"
                   % urllib.parse.quote(cfg["account_sid"]))
            auth = base64.b64encode(("%s:%s" % (cfg["account_sid"], cfg["auth_token"]))
                                    .encode("utf-8")).decode("ascii")
            st, detail = _post(url, {"To": to, "From": cfg["from"], "Body": text},
                               {"Authorization": "Basic " + auth})
        elif p == "bearer":
            payload = {(cfg.get("to_field") or "to"): to,
                       (cfg.get("text_field") or "text"): text}
            if cfg.get("from"):
                payload["from"] = cfg["from"]
            st, detail = _post(cfg["url"], payload,
                               {"Authorization": "Bearer " + cfg["api_key"]}, form=False)
        else:                                    # generic form gateway
            payload = {(cfg.get("to_field") or "to"): to,
                       (cfg.get("text_field") or "text"): text}
            for k in ("from", "username", "password", "api_key"):
                if cfg.get(k):
                    payload[k] = cfg[k]
            st, detail = _post(cfg["url"], payload, {})
        return (200 <= st < 300), "%s %s" % (st, detail)
    except urllib.error.HTTPError as e:
        return False, "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:150])
    except Exception as e:
        return False, str(e)[:180]


def send(to, text):
    """Fire and forget, off the request path."""
    if not to or not enabled():
        return
    threading.Thread(target=send_now, args=(to, text), daemon=True).start()
