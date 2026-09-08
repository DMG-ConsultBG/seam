# -*- coding: utf-8 -*-
"""
Seam - a shared operational layer between two businesses in a recurring
B2B relationship. Owned by neither, used by both.

This module is the engine: auth, multi-tenant access control, the structured
order/term model, the immutable audit trail, and notifications. Verticals are
configuration (see templates_config.py), not code.
"""
import io
import os
import re
import csv
import gzip
import json
import zipfile
import time
import hmac
import hashlib
import secrets
import sqlite3
import shutil
import tempfile
import traceback
import functools
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, session, send_from_directory, redirect, g, make_response
from werkzeug.exceptions import HTTPException
from markupsafe import escape as html_escape
from werkzeug.security import generate_password_hash, check_password_hash

import db
import eid
import sms
import totp
import pdfout
import receivables
import agreements
import taxes
import entitlements
import profiles
import template_addons as addons
import cryptobox
import qtsp
import banking
import webpush
import cloudstore
import demo_i18n
import public_text
import company
import mailer
import threading
import captcha as captcha_mod
from templates_config import TEMPLATES, get_template, stage_keys, stage_label, field_label, FIELD_TYPES

# Pricing for custom (paid) work templates. Modest monthly fee or one-time price.
CUSTOM_TPL_PRICING = {"monthly": "EUR 12", "onetime": "EUR 120"}

SUPPORTED_LANGS = {"en", "bg", "de", "ro", "el", "tr", "it", "ru", "es", "fr", "pl", "uk", "pt"}

BASE = os.path.dirname(__file__)
app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")


#: Everything on disk that would undo the rest of the security if it were
#: readable. Closed down at start-up, and reported by the go-live checks.
CREDENTIAL_FILES = (".secret", ".data_key", ".vapid_keys", ".ref_keys",
                    ".qtsp_config", ".eid_config", ".ai_config", ".stripe_key",
                    ".invbg_token")


def _load_secret():
    path = os.path.join(BASE, ".secret")
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    s = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(s)
    cryptobox.secure_file(path)
    return s


def _lock_down_credentials():
    """Applied on every start, not only on creation: a file restored from a
    backup or copied by hand arrives with the directory's permissions."""
    for name in CREDENTIAL_FILES:
        cryptobox.secure_file(os.path.join(BASE, name))


# The Secure flag makes a browser refuse to send the cookie over plain HTTP.
# That is exactly right in production and fatal on a local http://127.0.0.1,
# so it follows the same switch as the rest of the go-live checks: on unless
# this instance is explicitly being run for development.
SECURE_COOKIES = os.environ.get("SEAM_SECURE_COOKIES", "").strip() or (
    "0" if os.environ.get("SEAM_DEBUG") == "1" else "auto")

#: The address of the reverse proxy that terminates TLS, or "*" when the only
#: way to reach this process is through it. Until this is set, forwarded
#: headers are discarded before the app ever sees them - which is the safe
#: default, because "this arrived over https" is not something a client may be
#: allowed to assert about itself.
TRUSTED_PROXY = os.environ.get("SEAM_TRUSTED_PROXY", "").strip()

#: Set when nothing but the proxy can open this port - inside a container, or
#: behind a firewall that allows only the proxy through. It is what makes
#: SEAM_TRUSTED_PROXY=* safe on a bind that is not loopback, and it is a
#: separate switch because it is a claim about the network that this process
#: has no way to check for itself.
PROXY_ONLY = os.environ.get("SEAM_PROXY_ONLY", "").strip() == "1"

app.config.update(
    SECRET_KEY=_load_secret(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=(SECURE_COOKIES == "1"),
    JSON_AS_ASCII=False,
    MAX_CONTENT_LENGTH=64 * 1024 * 1024,  # allows photo/video progress evidence
)


@app.before_request
def _cookie_security():
    """With SEAM_SECURE_COOKIES=auto (the default), the flag is decided per
    request from how the request actually arrived, so the same build works
    behind a TLS proxy and on a developer's laptop without being told."""
    if SECURE_COOKIES != "auto":
        return
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    app.config["SESSION_COOKIE_SECURE"] = (proto or request.scheme) == "https"
# Preserve our curated template ordering (general/flagship first) in JSON output.
app.json.sort_keys = False

db.init_db()
_lock_down_credentials()


# --------------------------------------------------------------------------- #
#  Security
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  Roles inside a company
#
#  owner   registered the company; cannot be demoted or removed by anyone else
#  admin   everything, including settings and integrations
#  member  the day-to-day work: records, terms, documents, messages, signatures
#  viewer  reads and nothing else
#
#  Enforcement is one guard rather than a check scattered over ninety endpoints,
#  so a new endpoint is covered the day it is written instead of the day someone
#  remembers. Paths are matched by prefix; anything not listed is member-level.
# --------------------------------------------------------------------------- #
ROLES = ("owner", "admin", "member", "viewer")
ROLE_RANK = {"owner": 4, "admin": 3, "member": 2, "viewer": 1}

#: Settings-grade surface: configuring how the company itself operates.
ADMIN_PATHS = (
    "/api/qtsp", "/api/automations", "/api/approval-chains", "/api/integration/",
    "/api/notify/sms-config", "/api/notify/sms-test", "/api/cloud", "/api/team",
    "/api/plans/", "/api/org/contacts", "/api/custom-templates", "/api/calendar/rotate",
    "/api/banking/", "/api/invbg/", "/api/reference",
)

#: Writes a viewer is still allowed: their own account, nothing about the work.
VIEWER_WRITES = (
    "/api/auth/", "/api/notifications", "/api/notify/settings", "/api/notify/push-",
    "/api/calendar", "/api/translate",
)


def org_role(conn, user_id, org_id):
    row = conn.execute("SELECT role FROM memberships WHERE user_id=? AND org_id=?",
                       (user_id, org_id)).fetchone()
    return (row["role"] if row and row["role"] in ROLES else "member") if row else None


def my_best_role(conn, user_id):
    """A person belongs to one company in practice; take the strongest role if
    that ever stops being true."""
    rows = conn.execute("SELECT role FROM memberships WHERE user_id=?", (user_id,)).fetchall()
    best = None
    for r in rows:
        role = r["role"] if r["role"] in ROLES else "member"
        if best is None or ROLE_RANK[role] > ROLE_RANK[best]:
            best = role
    return best


def host_org_id(conn):
    """The company this instance belongs to.

    Seam is self-hosted: one company runs it and invites its counterparties in.
    That company is the first business account created here, and an explicit
    SEAM_HOST_ORG overrides it if an instance is ever set up the other way
    round."""
    forced = (os.environ.get("SEAM_HOST_ORG") or "").strip()
    if forced.isdigit():
        return int(forced)
    r = conn.execute("SELECT id FROM orgs WHERE kind='business' ORDER BY id LIMIT 1").fetchone()
    return r["id"] if r else 0


def require_instance_admin(conn, user_id):
    """For things that are about the installation rather than about a company:
    the error log, backups, the health screen.

    Checking "is an admin" is not enough here and was the bug: every partner is
    the owner of its own company, so every partner passed. What is required is
    being an admin of the company that runs this instance.
    """
    host = host_org_id(conn)
    if not host or org_role(conn, user_id, host) not in ("owner", "admin"):
        raise ApiError("Нямате достъп", 403, code="forbidden")
    return host


def _instance_admin_ids(conn):
    """Who runs this installation. They are the ones who see a bank transfer on
    a statement, so they are the ones a payment claim goes to."""
    host = host_org_id(conn)
    if not host:
        return []
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM memberships WHERE org_id=? AND role IN ('owner','admin')",
        (host,)).fetchall()]


def require_role(conn, user_id, org_id, *allowed):
    role = org_role(conn, user_id, org_id)
    if role is None:
        raise ApiError("Нямате достъп", 403, code="forbidden")
    if role not in allowed:
        raise ApiError("Нямате право за това действие", 403, code="role_denied")
    return role


@app.before_request
def _role_guard():
    if request.method not in ("POST", "PATCH", "PUT", "DELETE"):
        return
    path = request.path
    if not path.startswith("/api/") or "uid" not in session:
        return
    if path.startswith("/api/sign/") or _SIGN_ACT_RE.match(path):
        return                                   # signing authenticates by token
    conn = db.get_db()
    try:
        role = my_best_role(conn, session["uid"])
    finally:
        conn.close()
    if role is None:
        return                                   # no company yet: registration flow
    if role == "viewer" and not path.startswith(VIEWER_WRITES):
        raise ApiError("Профилът ви е само за четене", 403, code="role_viewer")
    if ROLE_RANK[role] < ROLE_RANK["admin"] and path.startswith(ADMIN_PATHS):
        raise ApiError("Само администратор може да променя настройките", 403,
                       code="role_admin_only")


_SIGN_ACT_RE = re.compile(r"^/api/signatures/\d+/(sign|otp)$")


@app.before_request
def _csrf_protect():
    """Double-submit CSRF: state-changing API calls from an authenticated
    session must carry the X-CSRF header (OWASP ASVS). Webhooks and inbound
    integration authenticate by key, not session, and are exempt."""
    if request.method not in ("POST", "PATCH", "DELETE"):
        return
    if not request.path.startswith("/api/"):
        return
    if request.path.startswith(("/api/webhooks/", "/api/inbound/")):
        return
    # Signing endpoints authenticate with the single-use signing token, not the
    # session - the signer is often not a Seam user, and when they happen to be
    # logged in the public signing page has no CSRF token to send.
    if request.path.startswith("/api/sign/") or _SIGN_ACT_RE.match(request.path):
        return
    if "uid" not in session:
        return
    tok = session.get("csrf") or ""
    hdr = request.headers.get("X-CSRF") or ""
    if not tok or not hdr or not hmac.compare_digest(tok, hdr):
        raise ApiError("Невалидна сесия, презаредете страницата", 403, code="csrf")


def issue_csrf():
    if not session.get("csrf"):
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


COMPRESSIBLE = ("text/", "application/javascript", "application/json",
                "application/xml", "image/svg+xml")


@app.after_request
def _compress(resp):
    """gzip text on the way out.

    Nothing in front of this process compresses: waitress does not, and there
    is no proxy in the self-hosted case. The bundle is 400 kB of JavaScript,
    CSS and JSON, which is a second of someone's morning on a slow connection
    and about a fifth of that once compressed.
    """
    if resp.direct_passthrough or resp.status_code < 200 or resp.status_code >= 300:
        return resp
    if "Content-Encoding" in resp.headers:
        return resp
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if not ctype.startswith(COMPRESSIBLE):
        return resp
    accept = request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept.lower():
        return resp
    data = resp.get_data()
    if len(data) < 900:                      # below this the header costs more
        return resp
    resp.set_data(gzip.compress(data, 6))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(resp.get_data()))
    # Caches must key on the encoding, or a proxy hands a gzipped body to a
    # client that said it could not read one.
    vary = resp.headers.get("Vary")
    resp.headers["Vary"] = (vary + ", Accept-Encoding") if vary else "Accept-Encoding"
    return resp


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), interest-cohort=()"
    # Set, not overwritten. A view that serves somebody else's file asks for
    # `default-src 'none'; sandbox`, which is far stricter than this; replacing
    # it here would quietly hand that file back its privileges.
    if "Content-Security-Policy" not in resp.headers:
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
    return _compress(resp)


# --------------------------------------------------------------------------- #
#  Transport optimisation
#
#  The UI ships ~600 KB of JS/CSS (the i18n bundle alone carries 13 fully
#  translated languages). Gzip cuts that by roughly 80 %, and immutable-ish
#  caching means returning visitors re-download nothing. Both are pure wins and
#  keep the zero-dependency, single-process deployment model.
# --------------------------------------------------------------------------- #
COMPRESSIBLE = ("text/", "application/json", "application/javascript",
                "application/xml", "image/svg+xml")
COMPRESS_MIN = 1024          # don't bother below 1 KB
STATIC_MAX_AGE = 60 * 60 * 6  # 6 h; ETag still revalidates on change


COMPRESS_MAX = 4 * 1024 * 1024   # never buffer more than this to compress


def _compress(resp):
    try:
        if resp.status_code >= 300:
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or ""):
            return resp
        if resp.headers.get("Content-Encoding"):
            return resp
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        if not any(ctype.startswith(p) for p in COMPRESSIBLE):
            return resp
        if resp.direct_passthrough:
            # Flask streams static files; buffer small text assets so they can
            # be gzipped, but leave big/binary streams (PDF, media) untouched.
            try:
                size = int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                size = 0
            if size == 0 or size > COMPRESS_MAX:
                return resp
            resp.direct_passthrough = False
        data = resp.get_data()
        if len(data) < COMPRESS_MIN:
            return resp
        packed = gzip.compress(data, 6)
        if len(packed) >= len(data):
            return resp
        resp.set_data(packed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(packed))
        vary = resp.headers.get("Vary")
        resp.headers["Vary"] = (vary + ", Accept-Encoding") if vary and "Accept-Encoding" not in vary else (vary or "Accept-Encoding")
    except Exception:
        return resp
    return resp


@app.after_request
def _static_cache(resp):
    if request.path.startswith("/static/") and resp.status_code == 200:
        resp.headers["Cache-Control"] = "public, max-age=%d, must-revalidate" % STATIC_MAX_AGE
    return resp


# --------------------------------------------------------------------------- #
#  Abuse controls
#
#  These live in the database rather than in one process's memory. In memory a
#  restart hands an attacker a clean slate, and a second worker keeps its own
#  private count - both of which make the limit decorative. SQLite is fast
#  enough for this: one indexed insert and one count per guarded request.
# --------------------------------------------------------------------------- #
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 900
_RL_PRUNE = {"at": 0.0}          # last time old rate rows were swept


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def _loopback():
    """Is this request coming from the machine Seam is running on?

    Used for the one thing that is safe to hand to the operator and to nobody
    else: a password-reset link when there is no mail server to send it to.
    `remote_addr` deliberately, not `_client_ip()` - a forwarded header is
    written by whoever is talking to the proxy, so trusting it here would let
    anyone claim to be local by sending `X-Forwarded-For: 127.0.0.1`.
    """
    return (request.remote_addr or "") in ("127.0.0.1", "::1", "localhost")


def _abuse_db():
    """The request's connection when there is one, otherwise a short-lived one -
    these helpers are called from before_request, before g.conn exists."""
    conn = getattr(g, "conn", None)
    return (conn, False) if conn is not None else (db.get_db(), True)


def rate_limited(key, limit, window):
    now = time.time()
    conn, owned = _abuse_db()
    try:
        conn.execute("DELETE FROM rate_events WHERE bucket=? AND at < ?", (key, now - window))
        conn.execute("INSERT INTO rate_events (bucket, at) VALUES (?,?)", (key, now))
        n = conn.execute("SELECT COUNT(*) c FROM rate_events WHERE bucket=? AND at >= ?",
                         (key, now - window)).fetchone()["c"]
        # Sweep buckets nobody is asking about any more, at most once a minute.
        if now - _RL_PRUNE["at"] > 60:
            _RL_PRUNE["at"] = now
            conn.execute("DELETE FROM rate_events WHERE at < ?", (now - 86400,))
        conn.commit()
        return n > limit
    except sqlite3.Error:
        return False              # never let the limiter itself break a request
    finally:
        if owned:
            conn.close()


def login_locked(email):
    conn, owned = _abuse_db()
    try:
        row = conn.execute("SELECT until FROM login_locks WHERE email=?", (email,)).fetchone()
        return bool(row and row["until"] > time.time())
    except sqlite3.Error:
        return False
    finally:
        if owned:
            conn.close()


def note_login_fail(email):
    now = time.time()
    conn, owned = _abuse_db()
    try:
        row = conn.execute("SELECT fails FROM login_locks WHERE email=?", (email,)).fetchone()
        fails = (row["fails"] if row else 0) + 1
        until = now + LOGIN_LOCK_SECONDS if fails >= LOGIN_MAX_FAILS else 0
        conn.execute(
            "INSERT INTO login_locks (email, fails, until, last) VALUES (?,?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET fails=excluded.fails, until=excluded.until, "
            "last=excluded.last", (email, fails, until, now))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        if owned:
            conn.close()


def clear_login_fails(email):
    conn, owned = _abuse_db()
    try:
        conn.execute("DELETE FROM login_locks WHERE email=?", (email,))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        if owned:
            conn.close()


# --------------------------------------------------------------------------- #
#  Passwords
#
#  Eight characters with a letter and a digit is the floor, and on its own it
#  admits "password1", "qwerty123" and the company's own name with a 1 after
#  it - which is what people actually choose and what a list attack tries
#  first. The rule below adds the two checks that catch those: a password must
#  not be one of the handful that everybody uses, and it must not be built out
#  of the address or the company it protects.
#
#  Deliberately not here: forced rotation and character-class quotas. Both push
#  people towards Passw0rd!1, Passw0rd!2, and NIST stopped recommending them
#  years ago. Length, a blocklist, rate limiting and a second factor are what
#  work, and the other three already exist.
# --------------------------------------------------------------------------- #
PASSWORD_MIN = 8
PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$")

#: The ones that appear at the top of every breach corpus, plus the shapes this
#: product invites (its own name, the word for invoice). Compared after
#: lowercasing and stripping trailing digits, so "Password123" is caught too.
COMMON_PASSWORDS = frozenset("""
password passwort parola parola contrasena motdepasse haslo пароль парола
qwerty qwertyui azerty qwertz asdfgh zxcvbn
letmein welcome admin administrator login user guest test testing demo
iloveyou monkey dragon sunshine princess football baseball master shadow
abc abcd abcdef abcdefg abcdefgh
seam seampass invoice faktura factura rechnung
123 1234 12345 123456 1234567 12345678 123456789 1234567890 111111 000000
""".split())


def password_problem(password, email="", org_name=""):
    """Returns an error code for a password that must not be used, or None.

    The codes are deliberately specific: "too short" and "everybody uses this
    one" are different problems and a person can only fix the one they have.
    """
    pw = password or ""
    if len(pw) < PASSWORD_MIN or not PASSWORD_RE.match(pw):
        return "weak_password"
    low = pw.lower()
    # Trailing digits are the usual way a blocked word is smuggled past a
    # blocklist: password -> password1 -> password2024.
    stem = re.sub(r"[0-9]+$", "", low) or low
    if low in COMMON_PASSWORDS or stem in COMMON_PASSWORDS:
        return "password_common"
    # A password made of the thing it protects is known to everyone who knows
    # the company. Each word on its own, not only the whole name: a company
    # called "Nordic Retail Group" invites "nordicretail1", which contains no
    # word long enough to notice unless the name is taken apart first.
    flat = re.sub(r"[^a-z0-9]+", "", low)
    parts = re.split(r"[^a-z0-9]+", (email or "").split("@")[0].lower())
    parts += re.split(r"[^a-z0-9]+", (org_name or "").lower())
    parts.append(re.sub(r"[^a-z0-9]+", "", (org_name or "").lower()))
    for word in parts:
        if len(word) >= 4 and word in flat:
            return "password_obvious"
    if len(set(low)) <= 3:
        return "password_common"
    return None


def password_error(code):
    """One place that turns those codes into the sentence a person reads."""
    return {
        "weak_password": ApiError(
            "Паролата трябва да е поне 8 символа и да съдържа буква и цифра",
            400, code="weak_password"),
        "password_common": ApiError(
            "Тази парола е сред най-често използваните. Изберете друга",
            400, code="password_common"),
        "password_obvious": ApiError(
            "Паролата не бива да съдържа имейла или името на фирмата",
            400, code="password_obvious"),
    }[code]


def is_verified(conn, uid):
    r = conn.execute("SELECT verified FROM users WHERE id=?", (uid,)).fetchone()
    return bool(r and r["verified"])


def require_verified(conn, uid):
    if not is_verified(conn, uid):
        raise ApiError("Профилът ви не е потвърден", 403, code="verify_required")


# --------------------------------------------------------------------------- #
#  Real-company checks
# --------------------------------------------------------------------------- #
# Free / personal mailbox providers are not accepted for business accounts -
# real companies sign up from a company domain.
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com", "yandex.ru",
    "mail.ru", "abv.bg", "mail.bg", "dir.bg", "gbg.bg", "web.de",
}


def email_domain(email):
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def is_free_email(email):
    return email_domain(email) in FREE_EMAIL_DOMAINS


# Per-country company-number validation lives in company.py (EIK/CUI/AFM/VAT...).


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, message, status=400, code="error", info=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.info = info or {}


@app.errorhandler(ApiError)
def _handle_api_error(e):
    return jsonify({"error": e.message, "code": e.code, "info": e.info}), e.status


@app.errorhandler(413)
def _too_large(e):
    return jsonify({"error": "Файлът е твърде голям", "code": "att_too_large", "info": {}}), 413


# --------------------------------------------------------------------------- #
#  Error visibility
#
#  An unhandled failure used to vanish into a console nobody reads. It is now a
#  row someone can see, with enough context to reproduce it: which request, which
#  user, and the traceback. The response to the user stays deliberately blank of
#  detail - the diagnosis belongs in the log, not on their screen.
# --------------------------------------------------------------------------- #
ERROR_LOG_KEEP = 500


#: Where to shout when something breaks. A URL, because mail is the thing most
#: likely to be unconfigured on a fresh install and an alert that depends on
#: the broken subsystem is not an alert. Any endpoint that takes a JSON POST
#: does: a chat webhook, a pager, a two-line script.
ALERT_URL = os.environ.get("SEAM_ALERT_URL", "").strip()
#: At most one call per window. A loop that fails on every request would
#: otherwise turn one fault into a denial of service against the operator's
#: own pager, and the second thousand alerts say nothing the first did not.
ALERT_WINDOW = int(os.environ.get("SEAM_ALERT_WINDOW", "300"))
_ALERT_SENT = {"at": 0.0, "suppressed": 0}


def _alert_operator(kind, message, path):
    """Tell somebody a fault happened. Best effort, never raises, never blocks
    the request that failed."""
    if not ALERT_URL.startswith(("http://", "https://")):
        return
    now = time.time()
    if now - _ALERT_SENT["at"] < ALERT_WINDOW:
        _ALERT_SENT["suppressed"] += 1
        return
    held = _ALERT_SENT["suppressed"]
    _ALERT_SENT["at"], _ALERT_SENT["suppressed"] = now, 0

    def send():
        # No traceback and no user data: this leaves the building, and an
        # alerting endpoint is not a place to put somebody's records.
        body_json = json.dumps({
            "source": "seam", "kind": kind, "path": path,
            "message": message, "suppressed_since_last": held,
            "at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                ALERT_URL, data=body_json, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "seam-alert"})
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass                   # an alert that fails must not become a fault

    threading.Thread(target=send, daemon=True).start()


def log_error(exc, kind=None):
    try:
        conn = db.get_db()
    except Exception:
        return
    try:
        uid = session.get("uid") if request else None
        org_id = None
        if uid:
            row = conn.execute("SELECT org_id FROM memberships WHERE user_id=? LIMIT 1",
                               (uid,)).fetchone()
            org_id = row["org_id"] if row else None
        conn.execute(
            "INSERT INTO error_log (path, method, user_id, org_id, kind, message, traceback, "
            "ip, device) VALUES (?,?,?,?,?,?,?,?,?)",
            (request.path if request else "", request.method if request else "", uid, org_id,
             kind or type(exc).__name__, str(exc)[:500],
             "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:],
             _client_ip() if request else "", _device_label() if request else ""))
        # Keep the log bounded: this is a signal, not an archive.
        conn.execute("DELETE FROM error_log WHERE id NOT IN "
                     "(SELECT id FROM error_log ORDER BY id DESC LIMIT ?)", (ERROR_LOG_KEEP,))
        conn.commit()
        _alert_operator(kind or type(exc).__name__, str(exc)[:200],
                        request.path if request else "")
    except Exception:
        pass                       # logging a failure must never cause one
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.errorhandler(Exception)
def _handle_unexpected(e):
    # Werkzeug's own HTTP errors (404, 405, ...) are not faults; let them pass.
    if isinstance(e, HTTPException):
        return e
    log_error(e)
    if app.debug:
        raise e                    # local work: keep the interactive traceback
    return jsonify({"error": "Възникна неочаквана грешка. Записана е и ще бъде прегледана.",
                    "code": "server_error", "info": {}}), 500


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def require(data, *keys):
    out = []
    for k in keys:
        v = data.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ApiError("Липсва задължително поле: %s" % k, code="field_required", info={"field": k})
        out.append(v.strip() if isinstance(v, str) else v)
    return out if len(out) > 1 else out[0]


# --------------------------------------------------------------------------- #
#  Sessions
#
#  A signed cookie proves who you are but cannot be taken back: the only way to
#  invalidate one is to rotate the instance key, which signs out every customer
#  at once. So each sign-in also writes a row, and the cookie carries its id.
#  That row is what makes "sign out on my stolen laptop" and "sign out
#  everywhere after a password change" possible at all, and it is what lets a
#  company see where its account is signed in.
#
#  Two clocks, because they answer different questions. Idle: a browser left
#  open in a warehouse should not stay signed in for a fortnight. Absolute: a
#  session that has been alive for a month should be re-proved regardless of
#  how busy it has been.
# --------------------------------------------------------------------------- #
SESSION_IDLE = int(os.environ.get("SEAM_SESSION_IDLE_HOURS", "12")) * 3600
SESSION_MAX = int(os.environ.get("SEAM_SESSION_MAX_DAYS", "30")) * 86400
#: last_seen is written at most this often, so reading a page is not a write.
SESSION_TOUCH = 300


def start_session(conn, uid):
    """Replace whatever was in the cookie with a fresh session. Called on every
    successful sign-in, so a fixed session id cannot be planted beforehand."""
    sid = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO user_sessions (user_id, sid, ip, device) VALUES (?,?,?,?)",
                 (uid, db.token_hash(sid), _client_ip(), _device_label()))
    conn.commit()
    session.clear()
    session["uid"] = uid
    session["sid"] = sid
    return sid


def revoke_sessions(conn, uid, keep_sid=None):
    conn.execute("UPDATE user_sessions SET revoked_at=datetime('now') "
                 "WHERE user_id=? AND revoked_at IS NULL AND sid IS NOT ?",
                 (uid, db.token_hash(keep_sid)))


def _session_ok(conn, uid, sid):
    """None when the session is still good, otherwise why it is not."""
    row = conn.execute("SELECT * FROM user_sessions WHERE sid=? AND user_id=?",
                       (db.token_hash(sid), uid)).fetchone()
    if not row:
        return "session_unknown"
    if row["revoked_at"]:
        return "session_revoked"
    started, seen = _parse_ts(row["created_at"]), _parse_ts(row["last_seen"])
    now = datetime.utcnow()
    if seen and (now - seen).total_seconds() > SESSION_IDLE:
        return "session_idle"
    if started and (now - started).total_seconds() > SESSION_MAX:
        return "session_expired"
    if seen and (now - seen).total_seconds() > SESSION_TOUCH:
        conn.execute("UPDATE user_sessions SET last_seen=datetime('now') WHERE id=?",
                     (row["id"],))
        conn.commit()
    return None


def current_user(conn):
    uid = session.get("uid")
    if not uid:
        return None
    sid = session.get("sid")
    if not sid:
        # A cookie from before sessions were recorded. Treat it as expired
        # rather than trusting it: an unrevocable cookie is the thing this
        # whole mechanism exists to remove.
        session.clear()
        return None
    bad = _session_ok(conn, uid, sid)
    if bad:
        session.clear()
        raise ApiError("Сесията е приключила", 401, code=bad)
    row = conn.execute("SELECT id, email, name, lang FROM users WHERE id=?", (uid,)).fetchone()
    return db.row_to_dict(row)


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        conn = db.get_db()
        try:
            user = current_user(conn)
            if not user:
                raise ApiError("Изисква се вписване", 401, code="auth_required")
            g.user = user
            g.conn = conn
            result = fn(*a, **kw)
            conn.commit()
            return result
        except ApiError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return wrapper


def user_org_ids(conn, user_id):
    rows = conn.execute("SELECT org_id FROM memberships WHERE user_id=?", (user_id,)).fetchall()
    return [r["org_id"] for r in rows]


def user_primary_org(conn, user_id):
    row = conn.execute(
        "SELECT o.* FROM orgs o JOIN memberships m ON m.org_id=o.id WHERE m.user_id=? ORDER BY o.id LIMIT 1",
        (user_id,),
    ).fetchone()
    return db.row_to_dict(row)


def workspace_access(conn, ws_id, user_id):
    """Return (workspace_dict, side) or raise. Enforces multi-tenant isolation.

    "Not found" and "not yours" are the same answer on purpose. Telling them
    apart lets an outsider walk the ids and learn how many relationships exist
    here and roughly when each began - which is a fact about other companies,
    not about the person asking.
    """
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    orgs = set(user_org_ids(conn, user_id)) if ws else set()
    if ws and ws["buyer_org_id"] in orgs:
        side = "buyer"
    elif ws and ws["partner_org_id"] in orgs:
        side = "partner"
    else:
        raise ApiError("Работното пространство не е намерено", 404, code="ws_not_found")
    return dict(ws), side


def other_org_id(ws, side):
    return ws["partner_org_id"] if side == "buyer" else ws["buyer_org_id"]


def org_user_ids(conn, org_id):
    if not org_id:
        return []
    rows = conn.execute("SELECT user_id FROM memberships WHERE org_id=?", (org_id,)).fetchall()
    return [r["user_id"] for r in rows]


def _device_label():
    """Short, human-readable device summary from the User-Agent - enough for an
    audit trail ("who did it, from where, on what") without storing the full
    fingerprint."""
    ua = (request.headers.get("User-Agent") or "")[:400] if request else ""
    if not ua:
        return None
    if "Mobi" in ua or "Android" in ua or "iPhone" in ua:
        plat = "Mobile"
    elif "Macintosh" in ua:
        plat = "macOS"
    elif "Windows" in ua:
        plat = "Windows"
    elif "Linux" in ua:
        plat = "Linux"
    else:
        plat = "Other"
    for name in ("Edg", "OPR", "Chrome", "Firefox", "Safari"):
        if name in ua:
            browser = {"Edg": "Edge", "OPR": "Opera"}.get(name, name)
            return "%s · %s" % (browser, plat)
    return plat


def notify_user_row(conn, uid, ws_id, order_id, kind, body_text, meta=None):
    """One notification to one person, where notify_org's whole-company fan-out
    is not what is wanted. `body_text` is internal, as everywhere else."""
    conn.execute(
        "INSERT INTO notifications (user_id, workspace_id, order_id, kind, body, meta_json) "
        "VALUES (?,?,?,?,?,?)",
        (uid, ws_id, order_id, kind, body_text, json.dumps(meta or {}, ensure_ascii=False)))


def log_system_event(conn, ws_id, order_id, uid, kind, summary, meta=None):
    """An event with no human behind it: an automation rule firing. Same
    contract as log_event - `summary` is the internal record of what happened,
    and what a reader sees is rendered from kind + meta."""
    conn.execute(
        "INSERT INTO events (workspace_id, order_id, actor_user_id, actor_side, kind, summary, meta_json) "
        "VALUES (?,?,?, 'system', ?, ?, ?)",
        (ws_id, order_id, uid, kind, summary, json.dumps(meta or {}, ensure_ascii=False)))


def log_event(conn, ws_id, order_id, actor, side, kind, summary, meta=None):
    """Append-only audit record: who, what, when, from where, on what device."""
    try:
        ip, device = _client_ip(), _device_label()
    except Exception:
        ip, device = None, None
    conn.execute(
        "INSERT INTO events (workspace_id, order_id, actor_user_id, actor_side, kind, summary, meta_json, ip, device) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ws_id, order_id, actor["id"], side, kind, summary,
         json.dumps(meta or {}, ensure_ascii=False), ip, device),
    )


# --------------------------------------------------------------------------- #
#  Email notifications (localized per recipient; best-effort, never blocking)
# --------------------------------------------------------------------------- #
EMAIL_I18N = {
    "en": {"n_subject": "New activity", "n_line": "You have new activity in Seam.", "cta": "Open Seam", "inv_subject": "Partnership invitation on Seam", "inv_line": "{org} invites you as a partner on Seam. Partner access is free.", "v_subject": "Confirm your email for Seam", "v_line": "Confirm your email to activate your account:", "cr_subject": "contact request from {name}", "cr_line": "A contact request came in through your profile on Seam.", "cr_from": "From", "cr_person": "Person", "cr_phone": "Telephone", "cr_email": "Email"},
    "es": {"n_subject": "Nueva actividad", "n_line": "Tiene nueva actividad en Seam.", "cta": "Abrir Seam", "inv_subject": "Invitación de asociación en Seam", "inv_line": "{org} le invita como socio en Seam. El acceso de socio es gratuito.", "v_subject": "Confirme su correo para Seam", "v_line": "Confirme su correo para activar su cuenta:", "cr_subject": "solicitud de contacto de {name}", "cr_line": "A través de su perfil en Seam ha llegado una solicitud de contacto.", "cr_from": "De", "cr_person": "Persona", "cr_phone": "Teléfono", "cr_email": "Correo"},
    "fr": {"n_subject": "Nouvelle activité", "n_line": "Vous avez une nouvelle activité sur Seam.", "cta": "Ouvrir Seam", "inv_subject": "Invitation de partenariat sur Seam", "inv_line": "{org} vous invite comme partenaire sur Seam. L'accès partenaire est gratuit.", "v_subject": "Confirmez votre e-mail pour Seam", "v_line": "Confirmez votre e-mail pour activer votre compte :", "cr_subject": "demande de contact de {name}", "cr_line": "Une demande de contact est arrivée par votre profil sur Seam.", "cr_from": "De", "cr_person": "Personne", "cr_phone": "Téléphone", "cr_email": "Courriel"},
    "pl": {"n_subject": "Nowa aktywność", "n_line": "Masz nową aktywność w Seam.", "cta": "Otwórz Seam", "inv_subject": "Zaproszenie do współpracy w Seam", "inv_line": "{org} zaprasza Cię jako partnera w Seam. Dostęp partnera jest darmowy.", "v_subject": "Potwierdź swój e-mail dla Seam", "v_line": "Potwierdź e-mail, aby aktywować konto:", "cr_subject": "prośba o kontakt od {name}", "cr_line": "Przez Państwa profil w Seam wpłynęła prośba o kontakt.", "cr_from": "Od", "cr_person": "Osoba", "cr_phone": "Telefon", "cr_email": "Poczta"},
    "uk": {"n_subject": "Нова активність", "n_line": "У вас нова активність у Seam.", "cta": "Відкрити Seam", "inv_subject": "Запрошення до партнерства в Seam", "inv_line": "{org} запрошує вас як партнера в Seam. Партнерський доступ безкоштовний.", "v_subject": "Підтвердьте пошту для Seam", "v_line": "Підтвердьте пошту, щоб активувати акаунт:", "cr_subject": "запит на звʼязок від {name}", "cr_line": "Через ваш профіль у Seam надійшов запит на звʼязок.", "cr_from": "Від", "cr_person": "Особа", "cr_phone": "Телефон", "cr_email": "Пошта"},
    "pt": {"n_subject": "Nova atividade", "n_line": "Tem nova atividade no Seam.", "cta": "Abrir o Seam", "inv_subject": "Convite de parceria no Seam", "inv_line": "{org} convida-o como parceiro no Seam. O acesso de parceiro é gratuito.", "v_subject": "Confirme o seu e-mail para o Seam", "v_line": "Confirme o seu e-mail para ativar a sua conta:", "cr_subject": "pedido de contacto de {name}", "cr_line": "Chegou um pedido de contacto através do seu perfil no Seam.", "cr_from": "De", "cr_person": "Pessoa", "cr_phone": "Telefone", "cr_email": "Correio"},
    "bg": {"n_subject": "Нова активност", "n_line": "Имате нова активност в Seam.", "cta": "Отворете Seam", "inv_subject": "Покана за партньорство в Seam", "inv_line": "{org} ви кани като партньор в Seam. Партньорският достъп е безплатен.", "v_subject": "Потвърдете имейла си за Seam", "v_line": "Потвърдете имейла си, за да активирате акаунта:", "cr_subject": "заявка за контакт от {name}", "cr_line": "През профила ви в Seam постъпи заявка за контакт.", "cr_from": "От", "cr_person": "Лице", "cr_phone": "Телефон", "cr_email": "Имейл"},
    "de": {"n_subject": "Neue Aktivität", "n_line": "Sie haben neue Aktivität in Seam.", "cta": "Seam öffnen", "inv_subject": "Partnerschaftseinladung auf Seam", "inv_line": "{org} lädt Sie als Partner auf Seam ein. Der Partnerzugang ist kostenlos.", "v_subject": "Bestätigen Sie Ihre E-Mail für Seam", "v_line": "Bestätigen Sie Ihre E-Mail, um Ihr Konto zu aktivieren:", "cr_subject": "Kontaktanfrage von {name}", "cr_line": "Über Ihr Profil bei Seam ist eine Kontaktanfrage eingegangen.", "cr_from": "Von", "cr_person": "Person", "cr_phone": "Telefon", "cr_email": "E-Mail"},
    "ro": {"n_subject": "Activitate nouă", "n_line": "Aveți activitate nouă în Seam.", "cta": "Deschideți Seam", "inv_subject": "Invitație de parteneriat pe Seam", "inv_line": "{org} vă invită ca partener pe Seam. Accesul de partener este gratuit.", "v_subject": "Confirmați e-mailul pentru Seam", "v_line": "Confirmați e-mailul pentru a vă activa contul:", "cr_subject": "solicitare de contact de la {name}", "cr_line": "Prin profilul dumneavoastră de pe Seam a venit o solicitare de contact.", "cr_from": "De la", "cr_person": "Persoană", "cr_phone": "Telefon", "cr_email": "E-mail"},
    "el": {"n_subject": "Νέα δραστηριότητα", "n_line": "Έχετε νέα δραστηριότητα στο Seam.", "cta": "Άνοιγμα Seam", "inv_subject": "Πρόσκληση συνεργασίας στο Seam", "inv_line": "Η {org} σας προσκαλεί ως συνεργάτη στο Seam. Η πρόσβαση συνεργάτη είναι δωρεάν.", "v_subject": "Επιβεβαιώστε το email σας για το Seam", "v_line": "Επιβεβαιώστε το email σας για να ενεργοποιήσετε τον λογαριασμό:", "cr_subject": "αίτημα επικοινωνίας από {name}", "cr_line": "Μέσω του προφίλ σας στο Seam ήρθε αίτημα επικοινωνίας.", "cr_from": "Από", "cr_person": "Πρόσωπο", "cr_phone": "Τηλέφωνο", "cr_email": "Email"},
    "tr": {"n_subject": "Yeni etkinlik", "n_line": "Seam'de yeni etkinliğiniz var.", "cta": "Seam'i aç", "inv_subject": "Seam'de iş ortaklığı daveti", "inv_line": "{org} sizi Seam'de iş ortağı olarak davet ediyor. İş ortağı erişimi ücretsizdir.", "v_subject": "Seam için e-postanızı doğrulayın", "v_line": "Hesabınızı etkinleştirmek için e-postanızı doğrulayın:", "cr_subject": "{name} firmasından iletişim talebi", "cr_line": "Seam'deki profiliniz üzerinden bir iletişim talebi geldi.", "cr_from": "Gönderen", "cr_person": "Kişi", "cr_phone": "Telefon", "cr_email": "E-posta"},
    "it": {"n_subject": "Nuova attività", "n_line": "Hai nuova attività in Seam.", "cta": "Apri Seam", "inv_subject": "Invito di partnership su Seam", "inv_line": "{org} ti invita come partner su Seam. L'accesso partner è gratuito.", "v_subject": "Conferma la tua email per Seam", "v_line": "Conferma la tua email per attivare l'account:", "cr_subject": "richiesta di contatto da {name}", "cr_line": "Attraverso il vostro profilo su Seam è arrivata una richiesta di contatto.", "cr_from": "Da", "cr_person": "Persona", "cr_phone": "Telefono", "cr_email": "Email"},
    "ru": {"n_subject": "Новая активность", "n_line": "У вас новая активность в Seam.", "cta": "Открыть Seam", "inv_subject": "Приглашение к партнёрству в Seam", "inv_line": "{org} приглашает вас как партнёра в Seam. Партнёрский доступ бесплатный.", "v_subject": "Подтвердите почту для Seam", "v_line": "Подтвердите почту, чтобы активировать аккаунт:", "cr_subject": "запрос на связь от {name}", "cr_line": "Через ваш профиль в Seam поступил запрос на связь.", "cr_from": "От", "cr_person": "Лицо", "cr_phone": "Телефон", "cr_email": "Почта"},
}


def _user_email_lang(conn, uid):
    r = conn.execute("SELECT email, COALESCE(lang,'en') AS l FROM users WHERE id=?", (uid,)).fetchone()
    if not r:
        return None, "en"
    return r["email"], (r["l"] if r["l"] in EMAIL_I18N else "en")


# The four letters about money. Written to be read by somebody who owes
# something: firm about the fact, never accusing. The overdue one states what
# is outstanding and by how long, and stops - threats belong to lawyers, and a
# platform that writes them on a company's behalf is putting words in its mouth.
MONEY_MAIL = {
    "en": {"sent_s": "Invoice {number} from {seller}", "sent_t": "Please find invoice {number} for {amount}, payable by {due}.", "rem_s": "Invoice {number} falls due on {due}", "rem_t": "A reminder that invoice {number} for {amount} falls due on {due}. If it has already been paid, please disregard this.", "due_s": "Invoice {number} is due today", "due_t": "Invoice {number} for {amount} is due today. If it has already been paid, please disregard this.", "late_s": "Invoice {number} is {days} days overdue", "late_t": "Invoice {number} for {amount} was due on {due} and is still outstanding, {days} days later. If payment has crossed with this message, please disregard it.", "paid_s": "Payment received, invoice {number}", "paid_t": "We have received {amount} for invoice {number}. Thank you. Nothing further is needed.", "pay_h": "How to pay", "ref_l": "Reference", "iban_l": "IBAN", "amount_l": "Amount", "due_l": "Payable by", "open_l": "Open the invoice", "card_l": "Pay by card", "ref_note": "Please quote the reference, so the payment is matched automatically."},
    "bg": {"sent_s": "Фактура {number} от {seller}", "sent_t": "Прилагаме фактура {number} за {amount}, платима до {due}.", "rem_s": "Фактура {number} е платима на {due}", "rem_t": "Напомняме, че фактура {number} за {amount} е платима на {due}. Ако вече е платена, моля не се съобразявайте с това съобщение.", "due_s": "Фактура {number} е платима днес", "due_t": "Фактура {number} за {amount} е платима днес. Ако вече е платена, моля не се съобразявайте с това съобщение.", "late_s": "Фактура {number} е просрочена с {days} дни", "late_t": "Фактура {number} за {amount} беше платима на {due} и все още не е погасена, {days} дни по-късно. Ако плащането се е разминало с това съобщение, моля не се съобразявайте с него.", "paid_s": "Постъпило плащане по фактура {number}", "paid_t": "Получихме {amount} по фактура {number}. Благодарим. Не се изисква нищо повече.", "pay_h": "Как да платите", "ref_l": "Основание", "iban_l": "IBAN", "amount_l": "Сума", "due_l": "Платимо до", "open_l": "Отвори фактурата", "card_l": "Плащане с карта", "ref_note": "Моля, посочете основанието, за да бъде плащането разпознато автоматично."},
    "de": {"sent_s": "Rechnung {number} von {seller}", "sent_t": "Anbei Rechnung {number} über {amount}, zahlbar bis {due}.", "rem_s": "Rechnung {number} wird am {due} fällig", "rem_t": "Eine Erinnerung, dass Rechnung {number} über {amount} am {due} fällig wird. Sollte sie bereits bezahlt sein, betrachten Sie diese Nachricht als gegenstandslos.", "due_s": "Rechnung {number} ist heute fällig", "due_t": "Rechnung {number} über {amount} ist heute fällig. Sollte sie bereits bezahlt sein, betrachten Sie diese Nachricht als gegenstandslos.", "late_s": "Rechnung {number} ist seit {days} Tagen überfällig", "late_t": "Rechnung {number} über {amount} war am {due} fällig und ist {days} Tage später weiterhin offen. Sollte sich die Zahlung mit dieser Nachricht überschnitten haben, betrachten Sie sie als gegenstandslos.", "paid_s": "Zahlungseingang zu Rechnung {number}", "paid_t": "Wir haben {amount} zu Rechnung {number} erhalten. Vielen Dank. Es ist nichts weiter zu tun.", "pay_h": "Zahlung", "ref_l": "Verwendungszweck", "iban_l": "IBAN", "amount_l": "Betrag", "due_l": "Zahlbar bis", "open_l": "Rechnung öffnen", "card_l": "Mit Karte zahlen", "ref_note": "Bitte geben Sie den Verwendungszweck an, damit die Zahlung automatisch zugeordnet wird."},
    "ro": {"sent_s": "Factura {number} de la {seller}", "sent_t": "Vă transmitem factura {number} în valoare de {amount}, scadentă la {due}.", "rem_s": "Factura {number} este scadentă la {due}", "rem_t": "Vă reamintim că factura {number} în valoare de {amount} este scadentă la {due}. Dacă a fost deja achitată, vă rugăm să ignorați acest mesaj.", "due_s": "Factura {number} este scadentă astăzi", "due_t": "Factura {number} în valoare de {amount} este scadentă astăzi. Dacă a fost deja achitată, vă rugăm să ignorați acest mesaj.", "late_s": "Factura {number} este restantă de {days} zile", "late_t": "Factura {number} în valoare de {amount} a fost scadentă la {due} și este încă neachitată, {days} zile mai târziu. Dacă plata s-a intersectat cu acest mesaj, vă rugăm să îl ignorați.", "paid_s": "Plată încasată, factura {number}", "paid_t": "Am încasat {amount} pentru factura {number}. Vă mulțumim. Nu mai este nimic de făcut.", "pay_h": "Cum se plătește", "ref_l": "Explicație", "iban_l": "IBAN", "amount_l": "Sumă", "due_l": "Scadent la", "open_l": "Deschide factura", "card_l": "Plată cu cardul", "ref_note": "Vă rugăm să indicați explicația, pentru ca plata să fie identificată automat."},
    "el": {"sent_s": "Τιμολόγιο {number} από {seller}", "sent_t": "Επισυνάπτεται το τιμολόγιο {number} ποσού {amount}, πληρωτέο έως {due}.", "rem_s": "Το τιμολόγιο {number} λήγει στις {due}", "rem_t": "Υπενθύμιση ότι το τιμολόγιο {number} ποσού {amount} λήγει στις {due}. Αν έχει ήδη εξοφληθεί, παρακαλούμε αγνοήστε το μήνυμα.", "due_s": "Το τιμολόγιο {number} λήγει σήμερα", "due_t": "Το τιμολόγιο {number} ποσού {amount} λήγει σήμερα. Αν έχει ήδη εξοφληθεί, παρακαλούμε αγνοήστε το μήνυμα.", "late_s": "Το τιμολόγιο {number} είναι ληξιπρόθεσμο {days} ημέρες", "late_t": "Το τιμολόγιο {number} ποσού {amount} έληξε στις {due} και παραμένει ανεξόφλητο, {days} ημέρες αργότερα. Αν η πληρωμή διασταυρώθηκε με αυτό το μήνυμα, παρακαλούμε αγνοήστε το.", "paid_s": "Εισπράχθηκε πληρωμή, τιμολόγιο {number}", "paid_t": "Λάβαμε {amount} για το τιμολόγιο {number}. Σας ευχαριστούμε. Δεν απαιτείται τίποτε άλλο.", "pay_h": "Τρόπος πληρωμής", "ref_l": "Αιτιολογία", "iban_l": "IBAN", "amount_l": "Ποσό", "due_l": "Πληρωτέο έως", "open_l": "Άνοιγμα τιμολογίου", "card_l": "Πληρωμή με κάρτα", "ref_note": "Παρακαλούμε αναγράψτε την αιτιολογία, ώστε η πληρωμή να αντιστοιχιστεί αυτόματα."},
    "tr": {"sent_s": "{seller} tarafından {number} numaralı fatura", "sent_t": "{number} numaralı, {amount} tutarındaki fatura ilişiktedir; son ödeme {due}.", "rem_s": "{number} numaralı faturanın vadesi {due}", "rem_t": "{number} numaralı, {amount} tutarındaki faturanın vadesi {due} tarihindedir. Ödeme yapıldıysa bu mesajı dikkate almayın.", "due_s": "{number} numaralı faturanın vadesi bugün", "due_t": "{number} numaralı, {amount} tutarındaki faturanın vadesi bugündür. Ödeme yapıldıysa bu mesajı dikkate almayın.", "late_s": "{number} numaralı fatura {days} gün gecikti", "late_t": "{number} numaralı, {amount} tutarındaki faturanın vadesi {due} idi ve {days} gün sonra hâlâ ödenmemiştir. Ödeme bu mesajla kesiştiyse dikkate almayın.", "paid_s": "Ödeme alındı, fatura {number}", "paid_t": "{number} numaralı fatura için {amount} tahsil edilmiştir. Teşekkür ederiz. Başka bir işlem gerekmiyor.", "pay_h": "Ödeme", "ref_l": "Açıklama", "iban_l": "IBAN", "amount_l": "Tutar", "due_l": "Son ödeme", "open_l": "Faturayı aç", "card_l": "Kartla öde", "ref_note": "Ödemenin otomatik eşleşmesi için lütfen açıklamayı yazın."},
    "it": {"sent_s": "Fattura {number} da {seller}", "sent_t": "In allegato la fattura {number} di {amount}, pagabile entro il {due}.", "rem_s": "La fattura {number} scade il {due}", "rem_t": "Le ricordiamo che la fattura {number} di {amount} scade il {due}. Se è già stata pagata, ignori questo messaggio.", "due_s": "La fattura {number} scade oggi", "due_t": "La fattura {number} di {amount} scade oggi. Se è già stata pagata, ignori questo messaggio.", "late_s": "La fattura {number} è scaduta da {days} giorni", "late_t": "La fattura {number} di {amount} scadeva il {due} ed è ancora aperta, {days} giorni dopo. Se il pagamento si è incrociato con questo messaggio, lo ignori.", "paid_s": "Pagamento ricevuto, fattura {number}", "paid_t": "Abbiamo ricevuto {amount} per la fattura {number}. Grazie. Non occorre altro.", "pay_h": "Come pagare", "ref_l": "Causale", "iban_l": "IBAN", "amount_l": "Importo", "due_l": "Pagabile entro", "open_l": "Apri la fattura", "card_l": "Paga con carta", "ref_note": "Indichi la causale, così il pagamento viene abbinato automaticamente."},
    "ru": {"sent_s": "Счёт {number} от {seller}", "sent_t": "Направляем счёт {number} на сумму {amount}, к оплате до {due}.", "rem_s": "Счёт {number} подлежит оплате {due}", "rem_t": "Напоминаем, что счёт {number} на сумму {amount} подлежит оплате {due}. Если он уже оплачен, просим не принимать это сообщение во внимание.", "due_s": "Счёт {number} подлежит оплате сегодня", "due_t": "Счёт {number} на сумму {amount} подлежит оплате сегодня. Если он уже оплачен, просим не принимать это сообщение во внимание.", "late_s": "Счёт {number} просрочен на {days} дней", "late_t": "Счёт {number} на сумму {amount} подлежал оплате {due} и остаётся непогашенным, {days} дней спустя. Если платёж разминулся с этим сообщением, просим его не учитывать.", "paid_s": "Оплата получена, счёт {number}", "paid_t": "Мы получили {amount} по счёту {number}. Благодарим. Больше ничего не требуется.", "pay_h": "Как оплатить", "ref_l": "Назначение", "iban_l": "IBAN", "amount_l": "Сумма", "due_l": "Оплатить до", "open_l": "Открыть счёт", "card_l": "Оплатить картой", "ref_note": "Просим указать назначение, чтобы платёж был сопоставлен автоматически."},
    "es": {"sent_s": "Factura {number} de {seller}", "sent_t": "Adjuntamos la factura {number} por {amount}, pagadera hasta el {due}.", "rem_s": "La factura {number} vence el {due}", "rem_t": "Le recordamos que la factura {number} por {amount} vence el {due}. Si ya ha sido abonada, haga caso omiso de este mensaje.", "due_s": "La factura {number} vence hoy", "due_t": "La factura {number} por {amount} vence hoy. Si ya ha sido abonada, haga caso omiso de este mensaje.", "late_s": "La factura {number} lleva {days} días vencida", "late_t": "La factura {number} por {amount} venció el {due} y sigue pendiente, {days} días después. Si el pago se ha cruzado con este mensaje, haga caso omiso.", "paid_s": "Pago recibido, factura {number}", "paid_t": "Hemos recibido {amount} de la factura {number}. Gracias. No hace falta nada más.", "pay_h": "Cómo pagar", "ref_l": "Concepto", "iban_l": "IBAN", "amount_l": "Importe", "due_l": "Pagadero hasta", "open_l": "Abrir la factura", "card_l": "Pagar con tarjeta", "ref_note": "Indique el concepto para que el pago se identifique automáticamente."},
    "fr": {"sent_s": "Facture {number} de {seller}", "sent_t": "Veuillez trouver la facture {number} d'un montant de {amount}, payable avant le {due}.", "rem_s": "La facture {number} arrive à échéance le {due}", "rem_t": "Nous vous rappelons que la facture {number} d'un montant de {amount} arrive à échéance le {due}. Si elle a déjà été réglée, ne tenez pas compte de ce message.", "due_s": "La facture {number} est due aujourd'hui", "due_t": "La facture {number} d'un montant de {amount} est due aujourd'hui. Si elle a déjà été réglée, ne tenez pas compte de ce message.", "late_s": "La facture {number} est en retard de {days} jours", "late_t": "La facture {number} d'un montant de {amount} était due le {due} et reste impayée, {days} jours plus tard. Si le règlement a croisé ce message, n'en tenez pas compte.", "paid_s": "Règlement reçu, facture {number}", "paid_t": "Nous avons reçu {amount} pour la facture {number}. Merci. Rien d'autre n'est nécessaire.", "pay_h": "Comment payer", "ref_l": "Référence", "iban_l": "IBAN", "amount_l": "Montant", "due_l": "Payable avant le", "open_l": "Ouvrir la facture", "card_l": "Payer par carte", "ref_note": "Merci d'indiquer la référence, pour que le règlement soit rapproché automatiquement."},
    "pl": {"sent_s": "Faktura {number} od {seller}", "sent_t": "W załączeniu faktura {number} na kwotę {amount}, płatna do {due}.", "rem_s": "Faktura {number} jest płatna do {due}", "rem_t": "Przypominamy, że faktura {number} na kwotę {amount} jest płatna do {due}. Jeśli została już opłacona, prosimy zignorować tę wiadomość.", "due_s": "Faktura {number} jest płatna dzisiaj", "due_t": "Faktura {number} na kwotę {amount} jest płatna dzisiaj. Jeśli została już opłacona, prosimy zignorować tę wiadomość.", "late_s": "Faktura {number} jest przeterminowana o {days} dni", "late_t": "Faktura {number} na kwotę {amount} była płatna do {due} i pozostaje nieuregulowana, {days} dni później. Jeśli płatność minęła się z tą wiadomością, prosimy ją zignorować.", "paid_s": "Otrzymano płatność, faktura {number}", "paid_t": "Otrzymaliśmy {amount} do faktury {number}. Dziękujemy. Nic więcej nie jest potrzebne.", "pay_h": "Jak zapłacić", "ref_l": "Tytułem", "iban_l": "IBAN", "amount_l": "Kwota", "due_l": "Termin płatności", "open_l": "Otwórz fakturę", "card_l": "Zapłać kartą", "ref_note": "Prosimy podać tytuł przelewu, aby płatność została dopasowana automatycznie."},
    "uk": {"sent_s": "Рахунок {number} від {seller}", "sent_t": "Надсилаємо рахунок {number} на суму {amount}, до сплати до {due}.", "rem_s": "Рахунок {number} підлягає сплаті {due}", "rem_t": "Нагадуємо, що рахунок {number} на суму {amount} підлягає сплаті {due}. Якщо його вже сплачено, просимо не зважати на це повідомлення.", "due_s": "Рахунок {number} підлягає сплаті сьогодні", "due_t": "Рахунок {number} на суму {amount} підлягає сплаті сьогодні. Якщо його вже сплачено, просимо не зважати на це повідомлення.", "late_s": "Рахунок {number} прострочено на {days} днів", "late_t": "Рахунок {number} на суму {amount} підлягав сплаті {due} і залишається непогашеним, {days} днів потому. Якщо платіж розминувся з цим повідомленням, просимо його не враховувати.", "paid_s": "Оплату отримано, рахунок {number}", "paid_t": "Ми отримали {amount} за рахунком {number}. Дякуємо. Більше нічого не потрібно.", "pay_h": "Як сплатити", "ref_l": "Призначення", "iban_l": "IBAN", "amount_l": "Сума", "due_l": "Сплатити до", "open_l": "Відкрити рахунок", "card_l": "Сплатити карткою", "ref_note": "Просимо вказати призначення, щоб платіж зіставився автоматично."},
    "pt": {"sent_s": "Fatura {number} de {seller}", "sent_t": "Junto se envia a fatura {number} no valor de {amount}, pagável até {due}.", "rem_s": "A fatura {number} vence a {due}", "rem_t": "Lembramos que a fatura {number} no valor de {amount} vence a {due}. Se já foi paga, ignore esta mensagem.", "due_s": "A fatura {number} vence hoje", "due_t": "A fatura {number} no valor de {amount} vence hoje. Se já foi paga, ignore esta mensagem.", "late_s": "A fatura {number} está {days} dias em atraso", "late_t": "A fatura {number} no valor de {amount} venceu a {due} e continua por liquidar, {days} dias depois. Se o pagamento se cruzou com esta mensagem, ignore-a.", "paid_s": "Pagamento recebido, fatura {number}", "paid_t": "Recebemos {amount} referente à fatura {number}. Obrigado. Nada mais é necessário.", "pay_h": "Como pagar", "ref_l": "Descritivo", "iban_l": "IBAN", "amount_l": "Valor", "due_l": "Pagável até", "open_l": "Abrir a fatura", "card_l": "Pagar com cartão", "ref_note": "Indique o descritivo para que o pagamento seja associado automaticamente."},
}


def _email_button(url, label):
    return ('<p style="margin:22px 0"><a href="%s" style="background:#2f6df0;color:#fff;text-decoration:none;'
            'padding:11px 22px;border-radius:8px;font-weight:600">%s</a></p>' % (url, html_escape(label)))


# --------------------------------------------------------------------------- #
#  Notification channels (roadmap item 10)
#
#  In-app is always on - it is the record. Everything else is per user: e-mail,
#  SMS, and a browser notification the service worker raises while the app is
#  open. Collaboration platforms are already covered by outbound webhooks.
# --------------------------------------------------------------------------- #
NOTIFY_CHANNELS = ("email", "sms", "push")
NOTIFY_DEFAULT = {"email": True, "sms": False, "push": True}


def user_channels(row):
    """A user's channel preferences, defaulted for accounts that never set them."""
    out = dict(NOTIFY_DEFAULT)
    try:
        saved = json.loads((row["notify_json"] if row else None) or "{}")
    except Exception:
        saved = {}
    for k in NOTIFY_CHANNELS:
        if k in saved:
            out[k] = bool(saved[k])
    return out


# --------------------------------------------------------------------------- #
#  Notification text, rendered by the server
#
#  The app renders notifications itself from kind + meta, so it follows the
#  language switcher. The service worker cannot: it raises an OS notification
#  with no page and no bundle. It used to show `notifications.body`, which is
#  written in Bulgarian at the point of the event - so every phone in every
#  country buzzed in Bulgarian. The API now hands over a `text` rendered in the
#  recipient's own stored language.
# --------------------------------------------------------------------------- #
NOTIF_I18N = {
    "en": {"invite": "You have been invited to {ws}", "document_ready": "{ref} · a document is ready", "order_created": "New record: {title} ({ref})", "status_changed": "{ref} · status changed", "term_proposed": "{ref} · new proposal on a term", "term_agreed": "{ref} · a term was agreed", "comment": "{ref} · new comment from {by}", "field_updated": "{ref} · details updated", "partner_joined": "{name} accepted the invitation", "attachment_added": "{ref} · new material from the work", "ws_message": "{ws} · message from {by}", "payout_recorded": "Payment {amount}", "approval_decided": "{ref} · approval: {decision}", "intro_request": "Introduction request from {name}", "intro_decided": "A decision was taken on your introduction request", "need_reply": "{name} replied to: {title}", "term_due_soon": "{ref} · a term falls due on {date}", "term_due_today": "{ref} · a term is due today", "term_overdue": "{ref} · a term was due on {date}", "term_review": "{ref} · an agreement is due a review", "payment_received": "{ref} · payment received", "contact_request": "{name} asks you to get in touch", "generic": "New activity in Seam"},
    "bg": {"invite": "Поканени сте в {ws}", "document_ready": "{ref} · готов документ", "order_created": "Нова преписка: {title} ({ref})", "status_changed": "{ref} · нов статус", "term_proposed": "{ref} · ново предложение по условие", "term_agreed": "{ref} · договорено условие", "comment": "{ref} · нов коментар от {by}", "field_updated": "{ref} · обновени данни", "partner_joined": "{name} прие поканата", "attachment_added": "{ref} · нови материали от изпълнението", "ws_message": "{ws} · съобщение от {by}", "payout_recorded": "Плащане {amount}", "approval_decided": "{ref} · одобрение: {decision}", "intro_request": "Заявка за препоръка от {name}", "intro_decided": "Взето е решение по вашата заявка за препоръка", "need_reply": "{name} отговори на: {title}", "term_due_soon": "{ref} · срок настъпва на {date}", "term_due_today": "{ref} · срок с падеж днес", "term_overdue": "{ref} · срок беше на {date}", "term_review": "{ref} · договорка подлежи на преразглеждане", "payment_received": "{ref} · постъпило плащане", "contact_request": "{name} иска да се свържете", "generic": "Ново събитие в Seam"},
    "de": {"invite": "Sie wurden zu {ws} eingeladen", "document_ready": "{ref} · ein Dokument ist fertig", "order_created": "Neuer Vorgang: {title} ({ref})", "status_changed": "{ref} · Status geändert", "term_proposed": "{ref} · neuer Vorschlag zu einer Bedingung", "term_agreed": "{ref} · eine Bedingung wurde vereinbart", "comment": "{ref} · neuer Kommentar von {by}", "field_updated": "{ref} · Angaben aktualisiert", "partner_joined": "{name} hat die Einladung angenommen", "attachment_added": "{ref} · neue Unterlagen aus der Ausführung", "ws_message": "{ws} · Nachricht von {by}", "payout_recorded": "Zahlung {amount}", "approval_decided": "{ref} · Freigabe: {decision}", "intro_request": "Empfehlungsanfrage von {name}", "intro_decided": "Über Ihre Empfehlungsanfrage wurde entschieden", "need_reply": "{name} hat geantwortet auf: {title}", "term_due_soon": "{ref} · eine Bedingung wird am {date} fällig", "term_due_today": "{ref} · eine Bedingung ist heute fällig", "term_overdue": "{ref} · eine Bedingung war am {date} fällig", "term_review": "{ref} · eine Vereinbarung steht zur Überprüfung an", "payment_received": "{ref} · Zahlung eingegangen", "contact_request": "{name} bittet um Kontaktaufnahme", "generic": "Neue Aktivität in Seam"},
    "ro": {"invite": "Ați fost invitat în {ws}", "document_ready": "{ref} · un document este gata", "order_created": "Dosar nou: {title} ({ref})", "status_changed": "{ref} · stare modificată", "term_proposed": "{ref} · propunere nouă la o condiție", "term_agreed": "{ref} · o condiție a fost convenită", "comment": "{ref} · comentariu nou de la {by}", "field_updated": "{ref} · date actualizate", "partner_joined": "{name} a acceptat invitația", "attachment_added": "{ref} · materiale noi din execuție", "ws_message": "{ws} · mesaj de la {by}", "payout_recorded": "Plată {amount}", "approval_decided": "{ref} · aprobare: {decision}", "intro_request": "Cerere de recomandare de la {name}", "intro_decided": "S-a luat o decizie privind cererea dvs. de recomandare", "need_reply": "{name} a răspuns la: {title}", "term_due_soon": "{ref} · o condiție ajunge la termen pe {date}", "term_due_today": "{ref} · o condiție are termen astăzi", "term_overdue": "{ref} · o condiție avea termen pe {date}", "term_review": "{ref} · o înțelegere trebuie reanalizată", "payment_received": "{ref} · încasare înregistrată", "contact_request": "{name} vă cere să luați legătura", "generic": "Activitate nouă în Seam"},
    "el": {"invite": "Προσκληθήκατε στο {ws}", "document_ready": "{ref} · ένα έγγραφο είναι έτοιμο", "order_created": "Νέος φάκελος: {title} ({ref})", "status_changed": "{ref} · αλλαγή κατάστασης", "term_proposed": "{ref} · νέα πρόταση σε όρο", "term_agreed": "{ref} · όρος συμφωνήθηκε", "comment": "{ref} · νέο σχόλιο από {by}", "field_updated": "{ref} · ενημερωμένα στοιχεία", "partner_joined": "Ο/Η {name} αποδέχτηκε την πρόσκληση", "attachment_added": "{ref} · νέο υλικό από την εκτέλεση", "ws_message": "{ws} · μήνυμα από {by}", "payout_recorded": "Πληρωμή {amount}", "approval_decided": "{ref} · έγκριση: {decision}", "intro_request": "Αίτημα σύστασης από {name}", "intro_decided": "Λήφθηκε απόφαση για το αίτημα σύστασής σας", "need_reply": "Ο/Η {name} απάντησε σε: {title}", "term_due_soon": "{ref} · όρος λήγει στις {date}", "term_due_today": "{ref} · όρος λήγει σήμερα", "term_overdue": "{ref} · όρος έληξε στις {date}", "term_review": "{ref} · συμφωνία χρειάζεται επανεξέταση", "payment_received": "{ref} · εισπράχθηκε πληρωμή", "contact_request": "Η {name} ζητά να επικοινωνήσετε", "generic": "Νέα δραστηριότητα στο Seam"},
    "tr": {"invite": "{ws} alanına davet edildiniz", "document_ready": "{ref} · bir belge hazır", "order_created": "Yeni kayıt: {title} ({ref})", "status_changed": "{ref} · durum değişti", "term_proposed": "{ref} · bir koşul için yeni öneri", "term_agreed": "{ref} · bir koşulda anlaşıldı", "comment": "{ref} · {by} yeni yorum yaptı", "field_updated": "{ref} · bilgiler güncellendi", "partner_joined": "{name} daveti kabul etti", "attachment_added": "{ref} · işten yeni materyal", "ws_message": "{ws} · {by} mesaj gönderdi", "payout_recorded": "Ödeme {amount}", "approval_decided": "{ref} · onay: {decision}", "intro_request": "{name} tavsiye talebinde bulundu", "intro_decided": "Tavsiye talebiniz hakkında karar verildi", "need_reply": "{name} şunu yanıtladı: {title}", "term_due_soon": "{ref} · bir koşulun süresi {date} tarihinde doluyor", "term_due_today": "{ref} · bir koşulun süresi bugün doluyor", "term_overdue": "{ref} · bir koşulun süresi {date} tarihinde doldu", "term_review": "{ref} · bir mutabakat gözden geçirilmeli", "payment_received": "{ref} · tahsilat kaydedildi", "contact_request": "{name} iletişime geçmenizi istiyor", "generic": "Seam'de yeni hareket"},
    "it": {"invite": "È stato invitato in {ws}", "document_ready": "{ref} · un documento è pronto", "order_created": "Nuova pratica: {title} ({ref})", "status_changed": "{ref} · stato cambiato", "term_proposed": "{ref} · nuova proposta su una condizione", "term_agreed": "{ref} · una condizione è stata concordata", "comment": "{ref} · nuovo commento da {by}", "field_updated": "{ref} · dati aggiornati", "partner_joined": "{name} ha accettato l'invito", "attachment_added": "{ref} · nuovo materiale dall'esecuzione", "ws_message": "{ws} · messaggio da {by}", "payout_recorded": "Pagamento {amount}", "approval_decided": "{ref} · approvazione: {decision}", "intro_request": "Richiesta di presentazione da {name}", "intro_decided": "È stata presa una decisione sulla sua richiesta di presentazione", "need_reply": "{name} ha risposto a: {title}", "term_due_soon": "{ref} · una condizione scade il {date}", "term_due_today": "{ref} · una condizione scade oggi", "term_overdue": "{ref} · una condizione scadeva il {date}", "term_review": "{ref} · un accordo va rivisto", "payment_received": "{ref} · incasso registrato", "contact_request": "{name} chiede di essere contattata", "generic": "Nuova attività in Seam"},
    "ru": {"invite": "Вас пригласили в {ws}", "document_ready": "{ref} · документ готов", "order_created": "Новая запись: {title} ({ref})", "status_changed": "{ref} · статус изменён", "term_proposed": "{ref} · новое предложение по условию", "term_agreed": "{ref} · условие согласовано", "comment": "{ref} · новый комментарий от {by}", "field_updated": "{ref} · данные обновлены", "partner_joined": "{name} принял приглашение", "attachment_added": "{ref} · новые материалы по исполнению", "ws_message": "{ws} · сообщение от {by}", "payout_recorded": "Платёж {amount}", "approval_decided": "{ref} · согласование: {decision}", "intro_request": "Запрос рекомендации от {name}", "intro_decided": "По вашему запросу рекомендации принято решение", "need_reply": "{name} ответил на: {title}", "term_due_soon": "{ref} · срок наступает {date}", "term_due_today": "{ref} · срок наступает сегодня", "term_overdue": "{ref} · срок был {date}", "term_review": "{ref} · договорённость пора пересмотреть", "payment_received": "{ref} · поступил платёж", "contact_request": "{name} просит связаться", "generic": "Новое событие в Seam"},
    "es": {"invite": "Le han invitado a {ws}", "document_ready": "{ref} · un documento está listo", "order_created": "Nuevo expediente: {title} ({ref})", "status_changed": "{ref} · estado cambiado", "term_proposed": "{ref} · nueva propuesta sobre una condición", "term_agreed": "{ref} · se acordó una condición", "comment": "{ref} · nuevo comentario de {by}", "field_updated": "{ref} · datos actualizados", "partner_joined": "{name} aceptó la invitación", "attachment_added": "{ref} · nuevo material de la ejecución", "ws_message": "{ws} · mensaje de {by}", "payout_recorded": "Pago {amount}", "approval_decided": "{ref} · aprobación: {decision}", "intro_request": "Solicitud de presentación de {name}", "intro_decided": "Se ha tomado una decisión sobre su solicitud de presentación", "need_reply": "{name} respondió a: {title}", "term_due_soon": "{ref} · una condición vence el {date}", "term_due_today": "{ref} · una condición vence hoy", "term_overdue": "{ref} · una condición vencía el {date}", "term_review": "{ref} · un acuerdo debe revisarse", "payment_received": "{ref} · cobro registrado", "contact_request": "{name} pide que se pongan en contacto", "generic": "Nueva actividad en Seam"},
    "fr": {"invite": "Vous avez été invité dans {ws}", "document_ready": "{ref} · un document est prêt", "order_created": "Nouveau dossier : {title} ({ref})", "status_changed": "{ref} · statut modifié", "term_proposed": "{ref} · nouvelle proposition sur une condition", "term_agreed": "{ref} · une condition a été convenue", "comment": "{ref} · nouveau commentaire de {by}", "field_updated": "{ref} · données mises à jour", "partner_joined": "{name} a accepté l'invitation", "attachment_added": "{ref} · nouveaux éléments d'exécution", "ws_message": "{ws} · message de {by}", "payout_recorded": "Paiement {amount}", "approval_decided": "{ref} · validation : {decision}", "intro_request": "Demande de mise en relation de {name}", "intro_decided": "Une décision a été prise sur votre demande de mise en relation", "need_reply": "{name} a répondu à : {title}", "term_due_soon": "{ref} · une condition échoit le {date}", "term_due_today": "{ref} · une condition échoit aujourd'hui", "term_overdue": "{ref} · une condition échoyait le {date}", "term_review": "{ref} · un accord demande un réexamen", "payment_received": "{ref} · encaissement enregistré", "contact_request": "{name} demande à être contactée", "generic": "Nouvelle activité dans Seam"},
    "pl": {"invite": "Zostali Państwo zaproszeni do {ws}", "document_ready": "{ref} · dokument jest gotowy", "order_created": "Nowa sprawa: {title} ({ref})", "status_changed": "{ref} · status zmieniony", "term_proposed": "{ref} · nowa propozycja do warunku", "term_agreed": "{ref} · warunek został uzgodniony", "comment": "{ref} · nowy komentarz od {by}", "field_updated": "{ref} · zaktualizowane dane", "partner_joined": "{name} przyjął zaproszenie", "attachment_added": "{ref} · nowe materiały z realizacji", "ws_message": "{ws} · wiadomość od {by}", "payout_recorded": "Płatność {amount}", "approval_decided": "{ref} · zatwierdzenie: {decision}", "intro_request": "Prośba o rekomendację od {name}", "intro_decided": "Podjęto decyzję w sprawie Państwa prośby o rekomendację", "need_reply": "{name} odpowiedział na: {title}", "term_due_soon": "{ref} · termin przypada {date}", "term_due_today": "{ref} · termin przypada dzisiaj", "term_overdue": "{ref} · termin minął {date}", "term_review": "{ref} · ustalenie wymaga przeglądu", "payment_received": "{ref} · wpłata zaksięgowana", "contact_request": "{name} prosi o kontakt", "generic": "Nowa aktywność w Seam"},
    "uk": {"invite": "Вас запросили до {ws}", "document_ready": "{ref} · документ готовий", "order_created": "Новий запис: {title} ({ref})", "status_changed": "{ref} · статус змінено", "term_proposed": "{ref} · нова пропозиція щодо умови", "term_agreed": "{ref} · умову погоджено", "comment": "{ref} · новий коментар від {by}", "field_updated": "{ref} · дані оновлено", "partner_joined": "{name} прийняв запрошення", "attachment_added": "{ref} · нові матеріали з виконання", "ws_message": "{ws} · повідомлення від {by}", "payout_recorded": "Платіж {amount}", "approval_decided": "{ref} · погодження: {decision}", "intro_request": "Запит рекомендації від {name}", "intro_decided": "За вашим запитом рекомендації ухвалено рішення", "need_reply": "{name} відповів на: {title}", "term_due_soon": "{ref} · строк настає {date}", "term_due_today": "{ref} · строк настає сьогодні", "term_overdue": "{ref} · строк був {date}", "term_review": "{ref} · домовленість час переглянути", "payment_received": "{ref} · надійшов платіж", "contact_request": "{name} просить звʼязатися", "generic": "Нова подія в Seam"},
    "pt": {"invite": "Foi convidado para {ws}", "document_ready": "{ref} · um documento está pronto", "order_created": "Novo processo: {title} ({ref})", "status_changed": "{ref} · estado alterado", "term_proposed": "{ref} · nova proposta sobre uma condição", "term_agreed": "{ref} · uma condição foi acordada", "comment": "{ref} · novo comentário de {by}", "field_updated": "{ref} · dados atualizados", "partner_joined": "{name} aceitou o convite", "attachment_added": "{ref} · novo material da execução", "ws_message": "{ws} · mensagem de {by}", "payout_recorded": "Pagamento {amount}", "approval_decided": "{ref} · aprovação: {decision}", "intro_request": "Pedido de apresentação de {name}", "intro_decided": "Foi tomada uma decisão sobre o seu pedido de apresentação", "need_reply": "{name} respondeu a: {title}", "term_due_soon": "{ref} · uma condição vence a {date}", "term_due_today": "{ref} · uma condição vence hoje", "term_overdue": "{ref} · uma condição vencia a {date}", "term_review": "{ref} · um acordo carece de revisão", "payment_received": "{ref} · recebimento registado", "contact_request": "A {name} pede que entre em contacto", "generic": "Nova atividade no Seam"},
}


def notif_text(conn, row, lang):
    """One notification, in the reader's language, built from kind + meta."""
    L = NOTIF_I18N.get(lang) or NOTIF_I18N["en"]
    tpl = L.get(row["kind"])
    if not tpl:
        return L["generic"]
    try:
        meta = json.loads(row["meta_json"] or "{}")
    except Exception:
        meta = {}
    # Deliberately no stage or term name: those labels live in the browser
    # bundle, and keeping a second copy here would drift. The buzz says which
    # record and what happened; the name is one tap away.
    vals = {
        "ref": meta.get("ref") or (row["order_ref"] if "order_ref" in row.keys() else "") or "",
        "title": dl(meta.get("title") or ""), "by": dl(meta.get("by") or ""),
        "name": dl(meta.get("name") or ""), "ws": dl(meta.get("ws") or ""),
        "amount": meta.get("amount") or "", "decision": meta.get("decision") or "",
        "date": meta.get("date") or "",
    }
    try:
        return tpl.format(**vals).strip(" ·")
    except (KeyError, IndexError):
        return L["generic"]


def push_notify_user(conn, uid, detail=""):
    """A data-less Web Push tickle. The notification text never passes through
    the push service - the worker fetches it over the user's own session."""
    row = conn.execute("SELECT notify_json FROM users WHERE id=?", (uid,)).fetchone()
    if not user_channels(row)["push"]:
        return
    subject = "mailto:" + (os.environ.get("SEAM_PUSH_CONTACT") or "admin@localhost")

    def drop(endpoint):
        c = db.get_db()
        try:
            c.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
            c.commit()
        finally:
            c.close()

    for s in conn.execute("SELECT endpoint FROM push_subscriptions WHERE user_id=?",
                          (uid,)).fetchall():
        webpush.send(s["endpoint"], subject, on_gone=drop)


def sms_notify_user(conn, uid, detail=""):
    """One short SMS per in-app notification, for users who asked for them."""
    if not sms.enabled():
        return
    row = conn.execute("SELECT phone, notify_json FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not (row["phone"] or "").strip():
        return
    if not user_channels(row)["sms"]:
        return
    sms.send(row["phone"], ("Seam · " + detail)[:300] if detail else "Seam")


def email_notify_user(conn, uid, detail=""):
    """One localized email per in-app notification. Best-effort."""
    if not mailer.enabled():
        return
    row = conn.execute("SELECT notify_json FROM users WHERE id=?", (uid,)).fetchone()
    if not user_channels(row)["email"]:
        return
    email, lang = _user_email_lang(conn, uid)
    if not email:
        return
    L = EMAIL_I18N[lang]
    base = request.url_root.rstrip("/") if request else "http://localhost:5000"
    subject = "Seam · " + (detail or L["n_subject"])
    html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b">'
            "<p>%s</p>%s%s</div>"
            % (html_escape(L["n_line"]),
               ("<p><b>%s</b></p>" % html_escape(detail)) if detail else "",
               _email_button(base, L["cta"])))
    mailer.send_async(email, subject, html)


# --------------------------------------------------------------------------- #
#  Outbound webhooks: push events to the tools a company already runs
# --------------------------------------------------------------------------- #
WEBHOOK_EVENTS = ("order.created", "order.status_changed", "term.proposed", "term.agreed",
                  "comment.added", "attachment.added", "store.incoming", "message.posted",
                  "signature.signed", "approval.decided", "payout.recorded",
                  # What an agreement does after it is made. These are the ones
                  # a company wants inside its own planning software, because
                  # they are the ones that move somebody's week.
                  "term.due_soon", "term.due_today", "term.overdue", "term.review_due",
                  "payment.received")

_NOTIF_TO_EVENT = {
    "order_created": "order.created", "status_changed": "order.status_changed",
    "term_proposed": "term.proposed", "term_agreed": "term.agreed",
    "term_due_soon": "term.due_soon", "term_due_today": "term.due_today",
    "term_overdue": "term.overdue", "term_review": "term.review_due",
    "payment_received": "payment.received",
    "comment": "comment.added", "attachment_added": "attachment.added",
    "store_incoming": "store.incoming", "ws_message": "message.posted",
    "signature_signed": "signature.signed", "approval_decided": "approval.decided",
    "payout_recorded": "payout.recorded",
}


def _webhook_send(hook_id, url, secret, payload):
    """Best-effort delivery in a daemon thread; never blocks or fails a request."""
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    status = "error"
    try:
        req = urllib.request.Request(url, data=body_bytes, method="POST", headers={
            "content-type": "application/json", "user-agent": "Seam-Webhook/1.0",
            "x-seam-event": payload.get("event", ""), "x-seam-signature": sig})
        with urllib.request.urlopen(req, timeout=12) as r:
            status = str(r.status)
    except urllib.error.HTTPError as e:
        status = str(e.code)
    except Exception as e:
        status = ("error: " + str(e))[:60]
    try:
        c = db.get_db()
        c.execute("UPDATE webhooks SET last_status=?, last_at=datetime('now') WHERE id=?", (status, hook_id))
        c.commit()
        c.close()
    except Exception:
        pass


def dispatch_webhooks(conn, org_id, event, data):
    if not org_id or event not in WEBHOOK_EVENTS:
        return
    try:
        rows = conn.execute("SELECT * FROM webhooks WHERE org_id=? AND active=1", (org_id,)).fetchall()
    except Exception:
        return
    for h in rows:
        try:
            evs = json.loads(h["events_json"] or '["*"]')
        except Exception:
            evs = ["*"]
        if "*" not in evs and event not in evs:
            continue
        payload = {"event": event, "sent_at": datetime.utcnow().isoformat() + "Z", "data": data}
        threading.Thread(target=_webhook_send, args=(h["id"], h["url"], h["secret"], payload), daemon=True).start()


# --------------------------------------------------------------------------- #
#  Workflow automation (roadmap item 3)
#
#  "When this happens, do that." Rules are data, not code: a trigger, a list of
#  conditions that must all hold, and a list of actions. They run inside the
#  caller's transaction, on the same events that already drive notifications and
#  webhooks, and every firing is written to automation_runs - an automated
#  action has to be as auditable as a manual one.
# --------------------------------------------------------------------------- #
AUTOMATION_TRIGGERS = WEBHOOK_EVENTS
AUTOMATION_ACTIONS = ("notify", "message", "set_status", "request_signature",
                      "create_document", "webhook", "flag")
AUTOMATION_OPS = ("eq", "ne", "contains", "gt", "lt", "any")
MAX_AUTOMATIONS = 40


def _auto_field(conn, data, field):
    """Resolve a condition field from the event payload, with a few conveniences
    that spare the user from knowing the payload shape."""
    if field in data:
        return data[field]
    oid = data.get("order_id")
    if not oid:
        return None
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return None
    if field in ("status", "template", "ref", "title"):
        return o[field]
    if field.startswith("term."):
        row = conn.execute("SELECT value FROM order_terms WHERE order_id=? AND key=?",
                           (oid, field[5:])).fetchone()
        return row["value"] if row else None
    if field.startswith("field."):
        try:
            return json.loads(o["fields_json"] or "{}").get(field[6:])
        except Exception:
            return None
    if field == "value":
        spec = {s["key"]: s for s in (tpl_get(conn, o["template"]) or {}).get("terms", [])}
        total = 0.0
        for t_ in conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=?",
                               (oid,)).fetchall():
            if t_["state"] == "agreed" and (spec.get(t_["key"]) or {}).get("type") == "money":
                total += _price_eur(t_["value"])
        return total
    return None


def _auto_match(conn, data, conditions):
    for c in conditions or []:
        got = _auto_field(conn, data, c.get("field") or "")
        want = c.get("value")
        op = c.get("op") or "eq"
        try:
            if op == "eq" and str(got or "") != str(want or ""):
                return False
            if op == "ne" and str(got or "") == str(want or ""):
                return False
            if op == "contains" and str(want or "").lower() not in str(got or "").lower():
                return False
            if op == "gt" and not (_price_eur(got) > _price_eur(want)):
                return False
            if op == "lt" and not (_price_eur(got) < _price_eur(want)):
                return False
            if op == "any" and str(got or "") not in [x.strip() for x in str(want or "").split(",")]:
                return False
        except Exception:
            return False
    return True


def _auto_text(conn, data, raw):
    """Substitute {ref}, {status}, {title}, {term.x}, {field.x} into a message."""
    def sub(m):
        v = _auto_field(conn, data, m.group(1))
        return "" if v is None else str(v)
    return re.sub(r"\{([a-zA-Z0-9_.]+)\}", sub, raw or "")[:2000]


def _auto_do(conn, org_id, rule, action, data):
    """Perform one action. Returns a short description for the run log."""
    kind = action.get("type")
    ws_id, order_id = data.get("workspace_id"), data.get("order_id")

    if kind == "notify":
        text = _auto_text(conn, data, action.get("text") or rule["name"])
        for uid in org_user_ids(conn, org_id):
            conn.execute(
                "INSERT INTO notifications (user_id, workspace_id, order_id, kind, body, meta_json) "
                "VALUES (?,?,?, 'automation', ?, ?)",
                (uid, ws_id, order_id, text,
                 json.dumps({"rule": rule["name"], "ref": data.get("ref", "")}, ensure_ascii=False)))
        return "notify"

    if kind == "message" and ws_id:
        text = _auto_text(conn, data, action.get("text") or "")
        if not text:
            return "message: empty"
        conn.execute("INSERT INTO ws_messages (workspace_id, user_id, body) VALUES (?,?,?)",
                     (ws_id, rule["created_by"], text))
        return "message"

    if kind == "set_status" and order_id:
        want = (action.get("status") or "").strip()
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o or not want or want == o["status"]:
            return "set_status: skipped"
        if want not in (tpl_stage_keys(conn, o["template"]) or []):
            return "set_status: unknown stage"
        conn.execute("UPDATE orders SET status=?, updated_at=datetime('now') WHERE id=?",
                     (want, order_id))
        log_system_event(conn, o["workspace_id"], order_id, rule["created_by"], "status_changed",
                         "Автоматизация „%s“: %s → %s" % (rule["name"], o["status"], want),
                         {"tpl": o["template"], "from": o["status"], "to": want,
                          "automation": rule["name"]})
        return "set_status: " + want

    if kind == "request_signature" and order_id:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            return "request_signature: no order"
        level = action.get("level") if action.get("level") in SIG_LEVELS else "simple"
        terms = conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=? ORDER BY key",
                             (order_id,)).fetchall()
        payload = "|".join([o["ref"], o["title"] or "", o["status"] or ""] +
                           ["%s=%s:%s" % (t_["key"], t_["value"] or "", t_["state"]) for t_ in terms])
        token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO signatures (org_id, order_id, doc_kind, doc_title, doc_url, doc_hash, level, "
            "token, token_enc, requested_by, signer_email, evidence_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, order_id, "protocol", "%s · %s" % (o["ref"], o["title"] or ""),
             "/protocol/%d" % order_id, _doc_hash(payload), level,
             db.token_hash(token), cryptobox.encrypt_field(token), rule["created_by"],
             (action.get("email") or "").strip()[:160],
             json.dumps({"automation": rule["name"], "hash_alg": "SHA-256"}, ensure_ascii=False)))
        return "request_signature: " + level

    if kind == "create_document" and order_id:
        # The roadmap's own example: contract -> invoice. Documents in Seam are
        # generated pages rather than stored files, so what an automation can do
        # is produce the prefilled link and put it where both sides see it.
        doc = (action.get("doc") or "").strip()
        if doc not in DOC_KINDS:
            return "create_document: unknown kind"
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            return "create_document: no order"
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (o["workspace_id"],)).fetchone()
        buyer = conn.execute("SELECT * FROM orgs WHERE id=?", (ws["buyer_org_id"],)).fetchone()
        partner = conn.execute("SELECT * FROM orgs WHERE id=?", (ws["partner_org_id"],)).fetchone() \
            if ws["partner_org_id"] else None
        spec = {s["key"]: s for s in (tpl_get(conn, o["template"]) or {}).get("terms", [])}
        amount = 0.0
        for t_ in conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=?",
                               (order_id,)).fetchall():
            if t_["state"] == "agreed" and (spec.get(t_["key"]) or {}).get("type") == "money":
                amount += _price_eur(t_["value"])
        params = {"a": buyer["name"] if buyer else "", "rega": (buyer["reg_number"] or "") if buyer else "",
                  "b": partner["name"] if partner else "", "regb": (partner["reg_number"] or "") if partner else "",
                  "subject": "%s · %s" % (o["ref"], dl(o["title"]) or "")}
        if amount:
            params["amount"] = "%.2f" % amount
        url = "/docs/%s?%s" % (doc, urllib.parse.urlencode(params))
        log_system_event(conn, o["workspace_id"], order_id, rule["created_by"], "document_ready",
                         "Автоматизация „%s“: подготвен документ" % rule["name"],
                         {"rule": rule["name"], "doc": doc, "url": url})
        for uid in org_user_ids(conn, org_id):
            notify_user_row(conn, uid, o["workspace_id"], order_id, "document_ready",
                            "%s · %s" % (o["ref"], doc),
                            {"rule": rule["name"], "doc": doc, "url": url, "ref": o["ref"]})
        return "create_document: " + doc

    if kind == "webhook":
        url = (action.get("url") or "").strip()
        if not url.startswith("https://") and not url.startswith("http://"):
            return "webhook: bad url"
        payload = {"event": "automation." + (rule["trigger"] or ""), "rule": rule["name"],
                   "sent_at": datetime.utcnow().isoformat() + "Z", "data": data}
        threading.Thread(target=_webhook_send,
                         args=(0, url, action.get("secret") or "seam", payload), daemon=True).start()
        return "webhook"

    if kind == "flag" and order_id:
        conn.execute(
            "INSERT INTO events (workspace_id, order_id, actor_user_id, actor_side, kind, summary, meta_json) "
            "VALUES (?,?,?, 'system', 'automation_flag', ?, ?)",
            (ws_id, order_id, rule["created_by"],
             _auto_text(conn, data, action.get("text") or rule["name"]),
             json.dumps({"rule": rule["name"]}, ensure_ascii=False)))
        return "flag"

    return "unknown action"


def run_automations(conn, org_id, trigger, data):
    """Fire every active rule of `org_id` that matches. Never raises: a broken
    rule must not roll back the business action that triggered it."""
    if not org_id or trigger not in AUTOMATION_TRIGGERS:
        return
    try:
        if getattr(g, "_in_automation", False):  # no rule may trigger another
            return
    except RuntimeError:                         # outside a request context
        return
    try:
        rules = conn.execute(
            "SELECT * FROM automations WHERE org_id=? AND trigger=? AND active=1 ORDER BY id",
            (org_id, trigger)).fetchall()
    except Exception:
        return
    if not rules:
        return
    g._in_automation = True
    try:
        for rule in rules:
            try:
                conds = json.loads(rule["conditions_json"] or "[]")
                acts = json.loads(rule["actions_json"] or "[]")
            except Exception:
                continue
            if not _auto_match(conn, data, conds):
                continue
            done, failed = [], None
            for a in acts[:6]:
                try:
                    done.append(_auto_do(conn, org_id, rule, a, data))
                except Exception as e:
                    failed = str(e)[:120]
                    break
            status = "error" if failed else "ok"
            conn.execute(
                "INSERT INTO automation_runs (automation_id, workspace_id, order_id, trigger, status, detail) "
                "VALUES (?,?,?,?,?,?)",
                (rule["id"], data.get("workspace_id"), data.get("order_id"), trigger, status,
                 (failed or ", ".join(done))[:300]))
            conn.execute(
                "UPDATE automations SET runs=runs+1, last_run=datetime('now'), last_status=? WHERE id=?",
                (status, rule["id"]))
    finally:
        g._in_automation = False


def notify_org(conn, org_id, ws_id, order_id, kind, body_text, exclude_user=None,
               meta=None, channels=("app", "mail", "sms", "push")):
    """Tell a company something happened.

    `channels` exists because not everything belongs in an inbox. A payment
    receipt has already gone to the customer by e-mail; sending the seller a
    second letter saying the same thing in worse words is how a product teaches
    people to filter its mail. That event still belongs in the app and in the
    webhook their own systems listen to.
    """
    mj = json.dumps(meta or {}, ensure_ascii=False)
    detail = (meta or {}).get("ref") or ""
    ev = _NOTIF_TO_EVENT.get(kind)
    if ev:
        data = dict(meta or {}, workspace_id=ws_id, order_id=order_id)
        targets = set()
        if ws_id:
            w = conn.execute("SELECT buyer_org_id, partner_org_id FROM workspaces WHERE id=?", (ws_id,)).fetchone()
            if w:
                targets.add(w["buyer_org_id"])
                if w["partner_org_id"]:
                    targets.add(w["partner_org_id"])
        else:
            targets.add(org_id)
        for oid in targets:
            dispatch_webhooks(conn, oid, ev, data)
            run_automations(conn, oid, ev, data)
    for uid in org_user_ids(conn, org_id):
        if uid == exclude_user:
            continue
        if "app" in channels:
            conn.execute(
                "INSERT INTO notifications (user_id, workspace_id, order_id, kind, body, meta_json) VALUES (?,?,?,?,?,?)",
                (uid, ws_id, order_id, kind, body_text, mj),
            )
        if "mail" in channels:
            email_notify_user(conn, uid, detail)
        if "sms" in channels:
            sms_notify_user(conn, uid, detail)
        if "push" in channels:
            push_notify_user(conn, uid, detail)


# --------------------------------------------------------------------------- #
#  Demo-content localization (resolve "§token" values to the viewer's language)
# --------------------------------------------------------------------------- #
def req_lang():
    """The reader's language: their own choice first, then what they asked for.

    Reading only the cookie meant a first visit was always English, and a
    crawler, which never has a cookie, could not reach twelve of the thirteen
    translations at all. Accept-Language is what a browser sends precisely to
    answer this, so it is honoured when nothing has been chosen yet.
    """
    lang = request.cookies.get("seam_lang")
    if lang in SUPPORTED_LANGS:
        return lang
    for part in (request.headers.get("Accept-Language") or "").split(","):
        tag = part.split(";")[0].strip().lower()
        if tag[:2] in SUPPORTED_LANGS:
            return tag[:2]
    return "en"


def dl(val):
    """Translate a demo token like '§o_packaging'; pass real data through."""
    if isinstance(val, str) and val.startswith("§"):
        return demo_i18n.translate(val[1:], req_lang())
    return val


def dl_fields(fields):
    return {k: dl(v) for k, v in fields.items()}


def dl_meta(meta_json):
    try:
        m = json.loads(meta_json or "{}")
    except Exception:
        return meta_json
    for k in ("title", "old", "new", "value", "product", "name"):
        if k in m:
            m[k] = dl(m[k])
    return json.dumps(m, ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  Template resolution (built-in + custom)
# --------------------------------------------------------------------------- #
def tpl_get(conn, key, org_id=None):
    """A template as it is actually configured.

    Every label, every allowed status and every form on both sides comes
    through here, so this is the one place add-ons have to be applied. It
    Two modes, and the difference matters:

    * With `org_id`, it resolves against **that company's** switches. This is
      what a pipeline and a form are drawn from, so a builder never sees a
      customs stage belonging to somebody else's trade.
    * Without it, it resolves against the **whole catalogue**. Labels and
      status validation use this, because the other side of a workspace has
      its own add-ons on: a stage recorded by the company that uses актуване
      must still read as "Актувано" for the company looking at it, and must
      not become invalid the day either side switches the add-on off.
    """
    t = get_template(key)
    if t:
        on = (org_addons(conn, org_id, key).get(key, []) if org_id
              else addons.available(key))
        return addons.resolve(key, on) or t
    row = conn.execute("SELECT config_json FROM custom_templates WHERE key=?", (key,)).fetchone()
    if row:
        try:
            return json.loads(row["config_json"])
        except Exception:
            return None
    return None


def tpl_stage_keys(conn, key):
    return [s["key"] for s in (tpl_get(conn, key) or {}).get("stages", [])]


def tpl_stage_label(conn, key, sk):
    for s in (tpl_get(conn, key) or {}).get("stages", []):
        if s["key"] == sk:
            return s["label"]
    return sk


def tpl_field_label(conn, key, fk):
    t = tpl_get(conn, key) or {}
    for f in t.get("fields", []) + t.get("terms", []):
        if f["key"] == fk:
            return f["label"]
    return fk


# --------------------------------------------------------------------------- #
#  Serialization
# --------------------------------------------------------------------------- #
def serialize_workspace(conn, ws, user_id, side):
    buyer = conn.execute("SELECT id,name,kind,verified_company FROM orgs WHERE id=?", (ws["buyer_org_id"],)).fetchone()
    partner = None
    if ws["partner_org_id"]:
        partner = conn.execute("SELECT id,name,kind,verified_company FROM orgs WHERE id=?", (ws["partner_org_id"],)).fetchone()
    tpl = tpl_get(conn, ws["template"], ws["buyer_org_id"]) or {}

    counts = {"total": 0, "open": 0, "awaiting_you": 0, "awaiting_partner": 0, "delivered": 0}
    orders = conn.execute("SELECT id,status,template FROM orders WHERE workspace_id=?", (ws["id"],)).fetchall()
    for o in orders:
        counts["total"] += 1
        skeys = tpl_stage_keys(conn, o["template"])
        last = skeys[-1] if skeys else None
        is_closed = (o["status"] == last) or (o["status"] == "closed")
        if not is_closed:
            counts["open"] += 1
        if o["status"] in ("delivered", "approved"):
            counts["delivered"] += 1
        # awaiting acceptance from a given side
        pend = conn.execute(
            "SELECT proposed_by_org FROM order_terms WHERE order_id=? AND state='proposed' AND value IS NOT NULL AND value!=''",
            (o["id"],),
        ).fetchall()
        for p in pend:
            if p["proposed_by_org"] is None:
                continue
            # if the OTHER side proposed it, it's awaiting YOU
            my_org = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
            if p["proposed_by_org"] != my_org:
                counts["awaiting_you"] += 1
            else:
                counts["awaiting_partner"] += 1

    pending_invite = None
    if not ws["partner_org_id"]:
        inv = conn.execute(
            "SELECT email FROM invites WHERE workspace_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (ws["id"],),
        ).fetchone()
        if inv:
            pending_invite = inv["email"]

    return {
        "id": ws["id"],
        "name": dl(ws["name"]),
        "template": ws["template"],
        "template_label": tpl.get("label", ws["template"]),
        "tagline": tpl.get("tagline", ""),
        # Empty rather than a word: the browser has the translated noun, and a
        # default in one language would surface in every other.
        "item_noun": tpl.get("item_noun", ""),
        "status": ws["status"],
        "side": side,
        "buyer": db.row_to_dict(buyer),
        "partner": db.row_to_dict(partner),
        "pending_invite": pending_invite,
        "counts": counts,
        "created_at": ws["created_at"],
    }


def serialize_order_summary(conn, o, ws, side):
    tpl = tpl_get(conn, o["template"]) or {}
    fields = json.loads(o["fields_json"] or "{}")
    awaiting_you = False
    pend = conn.execute(
        "SELECT proposed_by_org FROM order_terms WHERE order_id=? AND state='proposed' AND value IS NOT NULL AND value!=''",
        (o["id"],),
    ).fetchall()
    my_org = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    for p in pend:
        if p["proposed_by_org"] and p["proposed_by_org"] != my_org:
            awaiting_you = True
    return {
        "id": o["id"],
        "ref": o["ref"],
        "title": dl(o["title"]),
        "status": o["status"],
        "status_label": tpl_stage_label(conn, o["template"], o["status"]),
        "template": o["template"],
        "fields": dl_fields(fields),
        "awaiting_you": awaiting_you,
        "created_at": o["created_at"],
        "updated_at": o["updated_at"],
    }


def _next_review_iso(row):
    """The day this term next wants a look, or None.

    agreed_at is the truth when it is there; the row's last edit is the only
    honest answer when it is not, which is the case for anything agreed before
    the column existed or written straight into the database by the seed.
    """
    if not row or not row["review_every_days"]:
        return None
    due = agreements.next_review(row["agreed_at"] or row["updated_at"],
                                 row["review_every_days"], datetime.utcnow().date())
    return due.isoformat() if due else None


def serialize_order_detail(conn, o, ws, side):
    tpl = tpl_get(conn, o["template"]) or {}
    fields = json.loads(o["fields_json"] or "{}")
    my_org = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]

    term_rows = conn.execute("SELECT * FROM order_terms WHERE order_id=?", (o["id"],)).fetchall()
    by_key = {r["key"]: r for r in term_rows}
    terms = []
    for spec in tpl.get("terms", []):
        r = by_key.get(spec["key"])
        proposed_by_org = r["proposed_by_org"] if r else None
        terms.append({
            "key": spec["key"],
            "label": spec["label"],
            "type": spec["type"],
            "value": dl(r["value"]) if r else "",
            "state": r["state"] if r else "unset",
            "proposed_by_org": proposed_by_org,
            "proposed_by_me": proposed_by_org == my_org if proposed_by_org else False,
            "awaiting_you": bool(r and r["state"] == "proposed" and r["value"] and proposed_by_org and proposed_by_org != my_org),
            "updated_at": r["updated_at"] if r else None,
            "agreed_at": r["agreed_at"] if r else None,
            "review_every_days": r["review_every_days"] if r else None,
            "next_review": _next_review_iso(r),
            "placeholder": spec.get("placeholder", ""),
            "options": spec.get("options"),
        })

    events = conn.execute(
        "SELECT e.*, u.name AS actor_name FROM events e LEFT JOIN users u ON u.id=e.actor_user_id "
        "WHERE e.order_id=? ORDER BY e.id ASC",
        (o["id"],),
    ).fetchall()
    comments = conn.execute(
        "SELECT c.*, u.name AS author FROM comments c JOIN users u ON u.id=c.user_id "
        "WHERE c.order_id=? ORDER BY c.id ASC",
        (o["id"],),
    ).fetchall()

    def ev_dict(e):
        d = dict(e); d["meta_json"] = dl_meta(d.get("meta_json")); return d

    def cm_dict(c):
        d = dict(c); d["body"] = dl(d["body"]); return d

    atts = conn.execute(
        "SELECT a.*, u.name AS uploader FROM attachments a JOIN users u ON u.id=a.user_id "
        "WHERE a.order_id=? ORDER BY a.id DESC",
        (o["id"],),
    ).fetchall()
    attachments = [{
        "id": a["id"], "url": "/uploads/%d/%s" % (o["id"], a["filename"]),
        "name": a["original_name"], "kind": a["kind"], "size": a["size"],
        "uploader": a["uploader"], "uploader_id": a["user_id"], "created_at": a["created_at"],
    } for a in atts]

    return {
        "id": o["id"],
        "ref": o["ref"],
        "title": dl(o["title"]),
        "status": o["status"],
        "template": o["template"],
        "workspace_id": o["workspace_id"],
        "side": side,
        "stages": tpl.get("stages", []),
        "field_specs": tpl.get("fields", []),
        "fields": dl_fields(fields),
        "terms": terms,
        "events": [ev_dict(e) for e in events],
        "comments": [cm_dict(c) for c in comments],
        "attachments": attachments,
        "created_at": o["created_at"],
        "updated_at": o["updated_at"],
    }


# --------------------------------------------------------------------------- #
#  Auth
# --------------------------------------------------------------------------- #
@app.post("/api/auth/register")
def register():
    conn = db.get_db()
    try:
        if rate_limited("reg:" + _client_ip(), 8, 600):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        d = body()
        name, email, password, org_name = require(d, "name", "email", "password", "org_name")
        side = d.get("side", "buyer")
        if side not in ("buyer", "partner"):
            side = "buyer"
        email = email.lower()
        # CAPTCHA (anti-bot) - required for every signup
        if not captcha_mod.check(d.get("captcha_id"), d.get("captcha_text")):
            raise ApiError("Грешен код от картинката", code="captcha_wrong")
        bad = password_problem(password, email, org_name)
        if bad:
            raise password_error(bad)
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise ApiError("Този имейл вече е регистриран", code="email_taken")
        # Real-company checks for the paying (business) side.
        reg_norm, reg_country = None, None
        if side == "buyer":
            if is_free_email(email):
                raise ApiError("Използвайте фирмен имейл, не личен", code="free_email")
            ok, reg_norm, reg_country = company.validate(d.get("reg_number"), d.get("country"))
            if not ok:
                raise ApiError("Невалиден фирмен номер", code="invalid_reg_number")
        vtoken = secrets.token_urlsafe(24)
        cur = conn.execute(
            "INSERT INTO users (email, name, password_hash, verified, verify_token, lang) VALUES (?,?,?,0,?,?)",
            (email, name, generate_password_hash(password), db.token_hash(vtoken), req_lang()),
        )
        uid = cur.lastrowid
        kind = "business" if side == "buyer" else "partner"
        cur = conn.execute(
            "INSERT INTO orgs (name, kind, reg_number, country, verified_company) VALUES (?,?,?,?,?)",
            (org_name, kind, reg_norm, reg_country, 1 if (side == "buyer" and reg_norm) else 0),
        )
        oid = cur.lastrowid
        # Whoever registers the company owns it: the one role that cannot be
        # taken away by another member.
        conn.execute("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?, 'owner')",
                     (uid, oid))
        conn.commit()
        start_session(conn, uid)
        # With SMTP configured the verification link goes out by email;
        # without SMTP it is handed to the UI (in-app banner) instead.
        if mailer.enabled():
            lang = req_lang() if req_lang() in EMAIL_I18N else "en"
            L = EMAIL_I18N[lang]
            link = request.url_root.rstrip("/") + "/?verify=" + vtoken
            html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b"><p>%s</p>%s</div>'
                    % (html_escape(L["v_line"]), _email_button(link, L["v_subject"])))
            mailer.send_async(email, L["v_subject"], html)
        return jsonify({"id": uid, "name": name, "email": email, "verified": False,
                        "verify_token": vtoken, "csrf": issue_csrf()})
    except ApiError:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/auth/login")
def login():
    conn = db.get_db()
    try:
        # Generous per address, strict per account. A whole office shares one
        # public address, and 09:00 on Monday is not an attack; what actually
        # stops brute force is the five-failure lock on the account below.
        if rate_limited("login:" + _client_ip(), 120, 300):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        d = body()
        email, password = require(d, "email", "password")
        email = email.lower()
        if login_locked(email):
            raise ApiError("Профилът е временно заключен след много опити", 403, code="account_locked")
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not row or not check_password_hash(row["password_hash"], password):
            note_login_fail(email)
            raise ApiError("Грешен имейл или парола", 401, code="bad_credentials")
        clear_login_fails(email)
        # Remember the user's UI language for localized emails.
        conn.execute("UPDATE users SET lang=? WHERE id=?", (req_lang(), row["id"]))
        conn.commit()

        # With a second factor enabled the password alone signs nobody in. The
        # half-finished state is held in the cookie and expires on its own, so
        # a stolen password cannot be parked here and used later.
        if totp_enabled(conn, row["id"]):
            session.clear()
            session["pending_uid"] = row["id"]
            session["pending_at"] = int(time.time())
            return jsonify({"totp_required": True, "csrf": issue_csrf()})

        start_session(conn, row["id"])
        return jsonify({"id": row["id"], "name": row["name"], "email": row["email"],
                        "verified": bool(row["verified"]), "csrf": issue_csrf()})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Second factor
#
#  TOTP, because it is the one method that works identically in every country
#  Seam serves: no national scheme to integrate, no SMS gateway to pay, no
#  phone number to hand over. See totp.py for the algorithm.
#
#  Recovery codes are stored hashed with the same function as passwords. A
#  recovery code readable from a database backup is a password in a costume.
# --------------------------------------------------------------------------- #
TOTP_PENDING_TTL = 300
RECOVERY_CODES = 8


def totp_enabled(conn, uid):
    r = conn.execute("SELECT confirmed FROM user_totp WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r["confirmed"])


def _new_recovery_codes(conn, uid):
    conn.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (uid,))
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "-".join(secrets.token_hex(2) for _ in range(3))
        codes.append(raw)
        conn.execute("INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?,?)",
                     (uid, generate_password_hash(raw)))
    return codes


def _use_recovery_code(conn, uid, given):
    given = (given or "").strip().lower()
    if not given:
        return False
    for r in conn.execute("SELECT id, code_hash FROM user_recovery_codes "
                          "WHERE user_id=? AND used_at IS NULL", (uid,)).fetchall():
        if check_password_hash(r["code_hash"], given):
            conn.execute("UPDATE user_recovery_codes SET used_at=datetime('now') WHERE id=?",
                         (r["id"],))
            return True
    return False


@app.get("/api/auth/2fa")
@login_required
def totp_status():
    conn, user = g.conn, g.user
    left = conn.execute("SELECT COUNT(*) c FROM user_recovery_codes "
                        "WHERE user_id=? AND used_at IS NULL", (user["id"],)).fetchone()["c"]
    return jsonify({"enabled": totp_enabled(conn, user["id"]), "recovery_left": left})


@app.post("/api/auth/2fa/setup")
@login_required
def totp_setup():
    """Hand out a secret, but do not turn anything on: the code has to be
    proved once first, or a mistyped secret locks the person out of their own
    company."""
    conn, user = g.conn, g.user
    if totp_enabled(conn, user["id"]):
        raise ApiError("Двуфакторното удостоверяване вече е включено", 400, code="totp_on")
    secret = totp.new_secret()
    # Encrypted with a key held outside the database, so a copy of the database
    # - or a backup of it - does not let anyone generate valid codes.
    conn.execute("INSERT INTO user_totp (user_id, secret, confirmed) VALUES (?,?,0) "
                 "ON CONFLICT(user_id) DO UPDATE SET secret=excluded.secret, confirmed=0, "
                 "last_step=0", (user["id"], cryptobox.encrypt_field(secret)))
    conn.commit()
    return jsonify({"secret": secret, "grouped": totp.grouped(secret),
                    "uri": totp.provisioning_uri(secret, user["email"]),
                    "digits": totp.DIGITS, "period": totp.PERIOD})


@app.post("/api/auth/2fa/enable")
@login_required
def totp_enable():
    conn, user = g.conn, g.user
    row = conn.execute("SELECT * FROM user_totp WHERE user_id=?", (user["id"],)).fetchone()
    if not row:
        raise ApiError("Първо започнете настройката", 400, code="totp_no_setup")
    if rate_limited("totp:%d" % user["id"], 10, 300):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    step = totp.verify(cryptobox.decrypt_field(row["secret"]), body().get("code"),
                       row["last_step"])
    if step is None:
        raise ApiError("Кодът не е верен", 400, code="totp_bad")
    conn.execute("UPDATE user_totp SET confirmed=1, last_step=? WHERE user_id=?",
                 (step, user["id"]))
    codes = _new_recovery_codes(conn, user["id"])
    conn.commit()
    # Shown once. Seam keeps only the hashes, so it cannot show them again.
    return jsonify({"enabled": True, "recovery_codes": codes})


@app.post("/api/auth/2fa/disable")
@login_required
def totp_disable():
    """Turning the second factor off asks for the password again: whoever is at
    the keyboard is not necessarily whoever signed in."""
    conn, user = g.conn, g.user
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not check_password_hash(row["password_hash"], body().get("password") or ""):
        raise ApiError("Грешна парола", 403, code="bad_password")
    conn.execute("DELETE FROM user_totp WHERE user_id=?", (user["id"],))
    conn.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (user["id"],))
    conn.commit()
    return jsonify({"enabled": False})


@app.post("/api/auth/2fa/verify")
def totp_login_verify():
    """The second half of signing in. Deliberately not @login_required: at this
    point nobody is signed in yet."""
    conn = db.get_db()
    try:
        uid = session.get("pending_uid")
        started = session.get("pending_at") or 0
        if not uid or time.time() - started > TOTP_PENDING_TTL:
            session.clear()
            raise ApiError("Влизането изтече, започнете отново", 401, code="totp_expired")
        if rate_limited("totp2:%d" % uid, 10, 300):
            session.clear()
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        row = conn.execute("SELECT * FROM user_totp WHERE user_id=?", (uid,)).fetchone()
        d = body()
        step = totp.verify(cryptobox.decrypt_field(row["secret"]), d.get("code"),
                           row["last_step"]) if row else None
        if step is not None:
            conn.execute("UPDATE user_totp SET last_step=? WHERE user_id=?", (step, uid))
        elif not _use_recovery_code(conn, uid, d.get("recovery_code")):
            raise ApiError("Кодът не е верен", 401, code="totp_bad")
        u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        start_session(conn, uid)
        conn.commit()
        return jsonify({"id": u["id"], "name": u["name"], "email": u["email"],
                        "verified": bool(u["verified"]), "csrf": issue_csrf()})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Signing in with a national electronic identity
#
#  See eid.py for the protocol. Two rules are enforced here rather than there,
#  because they are about accounts rather than about OpenID Connect:
#
#    - an identity is attached from inside Seam by whoever already holds the
#      account. Signing in with an unattached identity does not create an
#      account, it says so and stops;
#    - the state that survives the redirect lives in the session cookie and
#      expires with it, so a callback cannot be replayed later or from
#      somewhere else.
# --------------------------------------------------------------------------- #
EID_FLOW_TTL = 600

#: The page the identity provider sends the browser back to. It is read by a
#: person, often on a phone, and it is the only screen in Seam that a stranger
#: to the account may see - so it says what happened and nothing more.
EID_I18N = {
    "en": {"failed": "Sign-in did not complete", "expired": "This attempt took too long. Start again from the sign-in screen.", "refused": "The identity provider did not authorise this. Nothing has changed here.", "mismatch": "The reply did not match the request. Start again from the sign-in screen.", "unverified": "The identity could not be verified against the provider. Nothing has changed here.", "taken": "That identity is already attached to a different account here.", "linked": "Identity attached", "linked_p": "You can now sign in with it.", "not_linked": "No account here is attached to this identity. Sign in with your password first, then attach it under Security.", "signed_in": "Signed in", "signed_in_p": "Welcome back, %s.", "back": "Continue to Seam"},
    "bg": {"failed": "Вписването не беше завършено", "expired": "Опитът отне твърде дълго. Започнете отново от екрана за вписване.", "refused": "Доставчикът на идентичност не разреши това. Тук нищо не е променено.", "mismatch": "Отговорът не съответства на заявката. Започнете отново от екрана за вписване.", "unverified": "Идентичността не можа да бъде потвърдена при доставчика. Тук нищо не е променено.", "taken": "Тази идентичност вече е свързана с друг профил тук.", "linked": "Идентичността е свързана", "linked_p": "Вече можете да се вписвате с нея.", "not_linked": "Няма профил тук, свързан с тази идентичност. Впишете се с парола и я свържете от раздел Сигурност.", "signed_in": "Вписахте се", "signed_in_p": "Добре дошли отново, %s.", "back": "Към Seam"},
    "de": {"failed": "Anmeldung nicht abgeschlossen", "expired": "Dieser Versuch hat zu lange gedauert. Beginnen Sie erneut auf der Anmeldeseite.", "refused": "Der Identitätsanbieter hat dies nicht freigegeben. Hier wurde nichts geändert.", "mismatch": "Die Antwort passt nicht zur Anfrage. Beginnen Sie erneut auf der Anmeldeseite.", "unverified": "Die Identität konnte beim Anbieter nicht bestätigt werden. Hier wurde nichts geändert.", "taken": "Diese Identität ist hier bereits mit einem anderen Konto verknüpft.", "linked": "Identität verknüpft", "linked_p": "Sie können sich damit nun anmelden.", "not_linked": "Kein Konto hier ist mit dieser Identität verknüpft. Melden Sie sich zuerst mit Ihrem Passwort an und verknüpfen Sie sie unter Sicherheit.", "signed_in": "Angemeldet", "signed_in_p": "Willkommen zurück, %s.", "back": "Weiter zu Seam"},
    "ro": {"failed": "Autentificarea nu s-a finalizat", "expired": "Încercarea a durat prea mult. Reluați din ecranul de autentificare.", "refused": "Furnizorul de identitate nu a autorizat această operațiune. Aici nu s-a schimbat nimic.", "mismatch": "Răspunsul nu corespunde cererii. Reluați din ecranul de autentificare.", "unverified": "Identitatea nu a putut fi verificată la furnizor. Aici nu s-a schimbat nimic.", "taken": "Această identitate este deja asociată altui cont de aici.", "linked": "Identitate asociată", "linked_p": "Acum vă puteți autentifica cu ea.", "not_linked": "Niciun cont de aici nu este asociat acestei identități. Autentificați-vă întâi cu parola și asociați-o din secțiunea Securitate.", "signed_in": "Autentificat", "signed_in_p": "Bine ați revenit, %s.", "back": "Continuă spre Seam"},
    "el": {"failed": "Η σύνδεση δεν ολοκληρώθηκε", "expired": "Η προσπάθεια άργησε πολύ. Ξεκινήστε ξανά από την οθόνη σύνδεσης.", "refused": "Ο πάροχος ταυτότητας δεν το ενέκρινε. Εδώ δεν άλλαξε τίποτα.", "mismatch": "Η απάντηση δεν ταιριάζει με το αίτημα. Ξεκινήστε ξανά από την οθόνη σύνδεσης.", "unverified": "Η ταυτότητα δεν επαληθεύτηκε στον πάροχο. Εδώ δεν άλλαξε τίποτα.", "taken": "Αυτή η ταυτότητα είναι ήδη συνδεδεμένη με άλλον λογαριασμό εδώ.", "linked": "Η ταυτότητα συνδέθηκε", "linked_p": "Μπορείτε πλέον να συνδέεστε με αυτήν.", "not_linked": "Κανένας λογαριασμός εδώ δεν είναι συνδεδεμένος με αυτή την ταυτότητα. Συνδεθείτε πρώτα με τον κωδικό σας και συνδέστε την από την Ασφάλεια.", "signed_in": "Συνδεθήκατε", "signed_in_p": "Καλώς ορίσατε ξανά, %s.", "back": "Συνέχεια στο Seam"},
    "tr": {"failed": "Oturum açma tamamlanmadı", "expired": "Bu deneme çok uzun sürdü. Oturum açma ekranından yeniden başlayın.", "refused": "Kimlik sağlayıcı buna izin vermedi. Burada hiçbir şey değişmedi.", "mismatch": "Yanıt istekle eşleşmedi. Oturum açma ekranından yeniden başlayın.", "unverified": "Kimlik sağlayıcıda doğrulanamadı. Burada hiçbir şey değişmedi.", "taken": "Bu kimlik burada başka bir hesaba bağlı.", "linked": "Kimlik bağlandı", "linked_p": "Artık onunla oturum açabilirsiniz.", "not_linked": "Burada bu kimliğe bağlı bir hesap yok. Önce parolanızla girin, sonra Güvenlik bölümünden bağlayın.", "signed_in": "Oturum açıldı", "signed_in_p": "Tekrar hoş geldiniz, %s.", "back": "Seam'e devam"},
    "it": {"failed": "L'accesso non è stato completato", "expired": "Il tentativo ha richiesto troppo tempo. Ricominci dalla schermata di accesso.", "refused": "Il fornitore di identità non lo ha autorizzato. Qui non è cambiato nulla.", "mismatch": "La risposta non corrisponde alla richiesta. Ricominci dalla schermata di accesso.", "unverified": "L'identità non è stata verificata presso il fornitore. Qui non è cambiato nulla.", "taken": "Quell'identità è già collegata a un altro account qui.", "linked": "Identità collegata", "linked_p": "Ora può accedere con essa.", "not_linked": "Nessun account qui è collegato a questa identità. Acceda prima con la password e la colleghi da Sicurezza.", "signed_in": "Accesso effettuato", "signed_in_p": "Bentornato, %s.", "back": "Continua su Seam"},
    "ru": {"failed": "Вход не завершён", "expired": "Попытка заняла слишком много времени. Начните заново с экрана входа.", "refused": "Поставщик идентичности не разрешил это. Здесь ничего не изменилось.", "mismatch": "Ответ не соответствует запросу. Начните заново с экрана входа.", "unverified": "Идентичность не удалось подтвердить у поставщика. Здесь ничего не изменилось.", "taken": "Эта идентичность уже связана с другим аккаунтом здесь.", "linked": "Идентичность привязана", "linked_p": "Теперь вы можете входить с её помощью.", "not_linked": "Ни один аккаунт здесь не связан с этой идентичностью. Войдите с паролем и привяжите её в разделе «Безопасность».", "signed_in": "Вы вошли", "signed_in_p": "С возвращением, %s.", "back": "Перейти в Seam"},
    "es": {"failed": "El acceso no se completó", "expired": "El intento tardó demasiado. Empiece de nuevo desde la pantalla de acceso.", "refused": "El proveedor de identidad no lo autorizó. Aquí no ha cambiado nada.", "mismatch": "La respuesta no coincide con la solicitud. Empiece de nuevo desde la pantalla de acceso.", "unverified": "No se pudo verificar la identidad ante el proveedor. Aquí no ha cambiado nada.", "taken": "Esa identidad ya está vinculada a otra cuenta aquí.", "linked": "Identidad vinculada", "linked_p": "Ya puede acceder con ella.", "not_linked": "Ninguna cuenta de aquí está vinculada a esta identidad. Acceda primero con su contraseña y vincúlela en Seguridad.", "signed_in": "Sesión iniciada", "signed_in_p": "Bienvenido de nuevo, %s.", "back": "Continuar a Seam"},
    "fr": {"failed": "La connexion n'a pas abouti", "expired": "Cette tentative a pris trop de temps. Recommencez depuis l'écran de connexion.", "refused": "Le fournisseur d'identité ne l'a pas autorisée. Rien n'a changé ici.", "mismatch": "La réponse ne correspond pas à la demande. Recommencez depuis l'écran de connexion.", "unverified": "L'identité n'a pas pu être vérifiée auprès du fournisseur. Rien n'a changé ici.", "taken": "Cette identité est déjà rattachée à un autre compte ici.", "linked": "Identité rattachée", "linked_p": "Vous pouvez désormais vous connecter avec elle.", "not_linked": "Aucun compte ici n'est rattaché à cette identité. Connectez-vous d'abord avec votre mot de passe, puis rattachez-la dans Sécurité.", "signed_in": "Connecté", "signed_in_p": "Bon retour, %s.", "back": "Continuer vers Seam"},
    "pl": {"failed": "Logowanie nie zostało ukończone", "expired": "Próba trwała zbyt długo. Proszę zacząć od nowa na ekranie logowania.", "refused": "Dostawca tożsamości tego nie autoryzował. Tutaj nic się nie zmieniło.", "mismatch": "Odpowiedź nie pasuje do żądania. Proszę zacząć od nowa na ekranie logowania.", "unverified": "Nie udało się zweryfikować tożsamości u dostawcy. Tutaj nic się nie zmieniło.", "taken": "Ta tożsamość jest już powiązana z innym kontem tutaj.", "linked": "Tożsamość powiązana", "linked_p": "Można się już nią logować.", "not_linked": "Żadne konto tutaj nie jest powiązane z tą tożsamością. Proszę zalogować się hasłem i powiązać ją w sekcji Bezpieczeństwo.", "signed_in": "Zalogowano", "signed_in_p": "Witamy ponownie, %s.", "back": "Przejdź do Seam"},
    "uk": {"failed": "Вхід не завершено", "expired": "Спроба тривала занадто довго. Почніть знову з екрана входу.", "refused": "Постачальник ідентичності цього не дозволив. Тут нічого не змінилося.", "mismatch": "Відповідь не збігається із запитом. Почніть знову з екрана входу.", "unverified": "Ідентичність не вдалося підтвердити в постачальника. Тут нічого не змінилося.", "taken": "Ця ідентичність уже прив'язана до іншого облікового запису тут.", "linked": "Ідентичність прив'язано", "linked_p": "Тепер ви можете входити з її допомогою.", "not_linked": "Жоден обліковий запис тут не прив'язаний до цієї ідентичності. Увійдіть паролем і прив'яжіть її в розділі «Безпека».", "signed_in": "Ви увійшли", "signed_in_p": "З поверненням, %s.", "back": "Перейти до Seam"},
    "pt": {"failed": "O início de sessão não foi concluído", "expired": "Esta tentativa demorou demasiado. Comece de novo no ecrã de início de sessão.", "refused": "O fornecedor de identidade não o autorizou. Aqui nada mudou.", "mismatch": "A resposta não corresponde ao pedido. Comece de novo no ecrã de início de sessão.", "unverified": "Não foi possível verificar a identidade junto do fornecedor. Aqui nada mudou.", "taken": "Essa identidade já está associada a outra conta aqui.", "linked": "Identidade associada", "linked_p": "Já pode iniciar sessão com ela.", "not_linked": "Nenhuma conta aqui está associada a esta identidade. Inicie sessão com a sua palavra-passe e associe-a em Segurança.", "signed_in": "Sessão iniciada", "signed_in_p": "Bem-vindo de volta, %s.", "back": "Continuar para o Seam"},
}


def _eid_redirect_uri():
    return request.url_root.rstrip("/") + "/auth/eid/callback"


@app.get("/api/auth/eid/providers")
def eid_providers():
    """What this instance can actually sign someone in with. Deliberately
    open: the sign-in screen has to draw it before anyone is signed in."""
    ready = eid.enabled_providers()
    return jsonify({"providers": [{"key": p["key"], "name": p["name"],
                                   "country": p["country"]} for p in ready]})


@app.get("/api/eid/catalogue")
@login_required
def eid_catalogue():
    """Everything that exists in a country, including schemes Seam cannot speak
    to. A company should be told what exists, not only what is convenient."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    country = (request.args.get("country") or (org or {}).get("country") or "").upper()
    items = []
    for p in eid.catalogue(country):
        items.append(dict(p, configured=eid.is_ready(p["key"])))
    return jsonify({"country": country, "verified": eid.REGISTRY_VERIFIED,
                    "providers": items})


@app.get("/api/eid/config")
@login_required
def eid_config_get():
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    return jsonify({"providers": [eid.public_config(k) for k in sorted(eid.PROVIDERS)
                                  if eid.config(k)]})


@app.post("/api/eid/config")
@login_required
def eid_config_set():
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    d = body()
    key = (d.get("key") or "").strip()
    if key not in eid.PROVIDERS:
        raise ApiError("Непознат доставчик", 400, code="eid_unknown")
    if eid.PROVIDERS[key]["protocol"] != "oidc":
        raise ApiError("Тази схема не се поддържа тук", 400, code="eid_protocol")
    try:
        eid.set_config(key, None if d.get("remove") else d)
    except ValueError:
        raise ApiError("Непознат доставчик", 400, code="eid_unknown")
    return jsonify(eid.public_config(key))


@app.get("/api/auth/eid")
@login_required
def eid_links():
    conn, user = g.conn, g.user
    rows = conn.execute("SELECT * FROM user_eid WHERE user_id=? ORDER BY id",
                        (user["id"],)).fetchall()
    return jsonify({"links": [
        {"id": r["id"], "provider": r["provider"],
         "name": (eid.PROVIDERS.get(r["provider"]) or {}).get("name", r["provider"]),
         "label": r["label"] or "", "created_at": r["created_at"],
         "last_used": r["last_used"]} for r in rows]})


@app.delete("/api/auth/eid/<int:lid>")
@login_required
def eid_unlink(lid):
    conn, user = g.conn, g.user
    conn.execute("DELETE FROM user_eid WHERE id=? AND user_id=?", (lid, user["id"]))
    conn.commit()
    return jsonify({"removed": True})


@app.post("/api/auth/eid/<key>/start")
def eid_start(key):
    """Works signed in (to attach an identity) and signed out (to use one)."""
    if not eid.is_ready(key):
        raise ApiError("Доставчикът не е настроен", 400, code="eid_unset")
    if rate_limited("eid:" + _client_ip(), 30, 600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    try:
        flow = eid.start(key, _eid_redirect_uri())
    except (ValueError, KeyError, urllib.error.URLError, OSError) as exc:
        log_error(exc, kind="eid_start")
        raise ApiError("Доставчикът не отговаря", 502, code="eid_unreachable")
    session["eid"] = {"key": key, "state": flow["state"], "nonce": flow["nonce"],
                      "verifier": flow["verifier"], "at": int(time.time()),
                      "link_uid": session.get("uid")}
    return jsonify({"url": flow["url"]})


@app.get("/auth/eid/callback")
def eid_callback():
    """Where the identity provider sends the browser back. Renders a page
    rather than JSON, because a person is looking at it."""
    lang = req_lang() if req_lang() in EID_I18N else "en"
    L = EID_I18N[lang]

    def page(title, message, ok=False):
        inner = ('%s<h1>%s</h1><div class="doc-sub">%s</div>'
                 '<p style="margin-top:22px"><a href="/">%s</a></p>'
                 % (_doc_brandmark(lang), html_escape(title), html_escape(message),
                    html_escape(L["back"])))
        # A message about what just happened, not a document.
        return _doc_shell(lang, title, inner, printable=False), (200 if ok else 400)

    flow = session.pop("eid", None)
    if not flow or time.time() - flow.get("at", 0) > EID_FLOW_TTL:
        return page(L["failed"], L["expired"])
    if request.args.get("error"):
        return page(L["failed"], L["refused"])
    # The state is compared in constant time and is single-use: it was popped
    # from the session above, so a callback cannot be replayed.
    if not hmac.compare_digest(request.args.get("state") or "", flow["state"]):
        return page(L["failed"], L["mismatch"])
    code = request.args.get("code") or ""
    if not code:
        return page(L["failed"], L["mismatch"])

    conn = db.get_db()
    try:
        try:
            tok = eid.exchange(flow["key"], code, _eid_redirect_uri(), flow["verifier"])
            claims = eid.verify_id_token(flow["key"], tok.get("id_token") or "",
                                         flow["nonce"])
        except Exception as exc:                       # network, protocol, signature
            log_error(exc, kind="eid_callback")
            return page(L["failed"], L["unverified"])

        sub = claims["sub"]
        label = (claims.get("name") or claims.get("preferred_username")
                 or claims.get("email") or "")[:120]
        row = conn.execute("SELECT * FROM user_eid WHERE provider=? AND sub=?",
                           (flow["key"], sub)).fetchone()

        if flow.get("link_uid"):
            # Attaching an identity to the account that started the flow.
            if row and row["user_id"] != flow["link_uid"]:
                return page(L["failed"], L["taken"])
            if not row:
                conn.execute("INSERT INTO user_eid (user_id, provider, sub, label) "
                             "VALUES (?,?,?,?)", (flow["link_uid"], flow["key"], sub, label))
                conn.commit()
            return page(L["linked"], L["linked_p"], ok=True)

        if not row:
            # No account here belongs to this identity, and one is not created:
            # holding a national identity says nothing about which company a
            # person works for.
            return page(L["failed"], L["not_linked"])

        u = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if not u:
            return page(L["failed"], L["not_linked"])
        # An electronic identity is at least as strong as a password plus a
        # code, so it stands in for both; the second factor is not asked again.
        conn.execute("UPDATE user_eid SET last_used=datetime('now') WHERE id=?", (row["id"],))
        start_session(conn, u["id"])
        conn.commit()
        return page(L["signed_in"], L["signed_in_p"] % u["name"], ok=True)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Where this account is signed in
# --------------------------------------------------------------------------- #
@app.get("/api/auth/sessions")
@login_required
def sessions_list():
    conn, user = g.conn, g.user
    rows = conn.execute(
        "SELECT * FROM user_sessions WHERE user_id=? AND revoked_at IS NULL "
        "ORDER BY last_seen DESC LIMIT 50", (user["id"],)).fetchall()
    here = session.get("sid")
    return jsonify({"sessions": [
        {"id": r["id"], "ip": r["ip"] or "", "device": r["device"] or "",
         "created_at": r["created_at"], "last_seen": r["last_seen"],
         "current": r["sid"] == db.token_hash(here)} for r in rows],
        "idle_hours": SESSION_IDLE // 3600, "max_days": SESSION_MAX // 86400})


@app.delete("/api/auth/sessions/<int:sid_row>")
@login_required
def session_revoke(sid_row):
    conn, user = g.conn, g.user
    conn.execute("UPDATE user_sessions SET revoked_at=datetime('now') "
                 "WHERE id=? AND user_id=? AND revoked_at IS NULL", (sid_row, user["id"]))
    conn.commit()
    return jsonify({"revoked": True})


@app.post("/api/auth/sessions/revoke-others")
@login_required
def sessions_revoke_others():
    conn, user = g.conn, g.user
    revoke_sessions(conn, user["id"], keep_sid=session.get("sid"))
    conn.commit()
    return jsonify({"revoked": True})


@app.post("/api/auth/logout")
def logout():
    sid = session.get("sid")
    if sid:
        conn = db.get_db()
        try:
            conn.execute("UPDATE user_sessions SET revoked_at=datetime('now') WHERE sid=?",
                         (db.token_hash(sid),))
            conn.commit()
        finally:
            conn.close()
    session.clear()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  Password reset
#
#  Request a link -> single-use token valid for RESET_TTL -> set a new password.
#  The request endpoint always answers the same way, so it cannot be used to
#  discover which e-mails are registered. With SMTP configured the link is
#  e-mailed; on a self-hosted demo without SMTP it is handed back to the UI.
# --------------------------------------------------------------------------- #
RESET_TTL = 60 * 60  # 1 hour

RESET_EMAIL_I18N = {
    "en": {"s": "Reset your Seam password", "l": "Use the button below to set a new password. The link is valid for 1 hour and can be used once. If you did not request this, ignore this e-mail.", "b": "Set a new password"},
    "bg": {"s": "Възстановяване на паролата за Seam", "l": "Използвайте бутона по-долу, за да зададете нова парола. Линкът е валиден 1 час и е за еднократна употреба. Ако не сте заявявали това, игнорирайте писмото.", "b": "Задай нова парола"},
    "de": {"s": "Seam-Passwort zurücksetzen", "l": "Setzen Sie über die Schaltfläche unten ein neues Passwort. Der Link ist 1 Stunde gültig und einmal verwendbar. Falls Sie dies nicht angefordert haben, ignorieren Sie diese E-Mail.", "b": "Neues Passwort setzen"},
    "ro": {"s": "Resetarea parolei Seam", "l": "Folosiți butonul de mai jos pentru a seta o parolă nouă. Linkul este valabil 1 oră și poate fi folosit o singură dată. Dacă nu ați solicitat acest lucru, ignorați e-mailul.", "b": "Setează o parolă nouă"},
    "el": {"s": "Επαναφορά κωδικού Seam", "l": "Χρησιμοποιήστε το κουμπί παρακάτω για να ορίσετε νέο κωδικό. Ο σύνδεσμος ισχύει για 1 ώρα και για μία χρήση. Αν δεν το ζητήσατε, αγνοήστε το μήνυμα.", "b": "Ορισμός νέου κωδικού"},
    "tr": {"s": "Seam parolanızı sıfırlayın", "l": "Yeni bir parola belirlemek için aşağıdaki düğmeyi kullanın. Bağlantı 1 saat geçerlidir ve tek kullanımlıktır. Bunu siz talep etmediyseniz e-postayı yok sayın.", "b": "Yeni parola belirle"},
    "it": {"s": "Reimposta la password di Seam", "l": "Usa il pulsante qui sotto per impostare una nuova password. Il link è valido 1 ora ed è monouso. Se non hai richiesto questo, ignora l'e-mail.", "b": "Imposta una nuova password"},
    "ru": {"s": "Сброс пароля Seam", "l": "Нажмите кнопку ниже, чтобы задать новый пароль. Ссылка действует 1 час и одноразовая. Если вы этого не запрашивали, проигнорируйте письмо.", "b": "Задать новый пароль"},
    "es": {"s": "Restablecer su contraseña de Seam", "l": "Use el botón de abajo para establecer una nueva contraseña. El enlace es válido 1 hora y de un solo uso. Si no lo solicitó, ignore este correo.", "b": "Establecer nueva contraseña"},
    "fr": {"s": "Réinitialiser votre mot de passe Seam", "l": "Utilisez le bouton ci-dessous pour définir un nouveau mot de passe. Le lien est valable 1 heure et à usage unique. Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail.", "b": "Définir un nouveau mot de passe"},
    "pl": {"s": "Reset hasła Seam", "l": "Użyj przycisku poniżej, aby ustawić nowe hasło. Link jest ważny 1 godzinę i jednorazowy. Jeśli nie prosiłeś o to, zignoruj wiadomość.", "b": "Ustaw nowe hasło"},
    "uk": {"s": "Скидання пароля Seam", "l": "Скористайтеся кнопкою нижче, щоб задати новий пароль. Посилання дійсне 1 годину й одноразове. Якщо ви цього не запитували, проігноруйте лист.", "b": "Задати новий пароль"},
    "pt": {"s": "Repor a sua palavra-passe Seam", "l": "Use o botão abaixo para definir uma nova palavra-passe. A ligação é válida 1 hora e de utilização única. Se não solicitou isto, ignore este e-mail.", "b": "Definir nova palavra-passe"},
}


@app.post("/api/auth/forgot")
def password_forgot():
    conn = db.get_db()
    try:
        email = (body().get("email") or "").strip().lower()
        ip_key = "fgt:" + _client_ip()
        if rate_limited(ip_key, 5, 900):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        row = conn.execute("SELECT id, name, email FROM users WHERE email=?", (email,)).fetchone()
        # Whether this installation can send mail at all is a property of the
        # installation, not of the address being asked about. Reporting it only
        # for addresses that exist turned this endpoint into an account
        # enumeration oracle the moment SMTP was left unset: ask, and the shape
        # of the answer tells you whether the account is real.
        out = {"ok": True}
        if not mailer.enabled() and not _loopback():
            out["no_mail"] = True
        if row:
            token = secrets.token_urlsafe(32)
            expires = (datetime.utcnow() + timedelta(seconds=RESET_TTL)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE users SET reset_token=?, reset_expires=? WHERE id=?",
                     (db.token_hash(token), expires, row["id"]))
            conn.commit()
            link = request.url_root.rstrip("/") + "/?reset=" + token
            lang = req_lang() if req_lang() in RESET_EMAIL_I18N else "en"
            L = RESET_EMAIL_I18N[lang]
            if mailer.enabled():
                html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b">'
                        "<p>%s</p>%s</div>" % (html_escape(L["l"]), _email_button(link, L["b"])))
                mailer.send_async(row["email"], L["s"], html)
            elif _loopback():
                # No SMTP and nowhere to send it. The link is handed back only
                # to a request from this machine, where whoever is asking is
                # already the operator.
                #
                # Returning it to anyone was the original behaviour and it is
                # an account takeover from an e-mail address alone: no session,
                # no proof of the address, just ask and receive. On the open
                # internet with SMTP unset that is every account on the
                # installation.
                out["reset_link"] = "/?reset=" + token
        return jsonify(out)
    finally:
        conn.close()


@app.post("/api/auth/reset")
def password_reset():
    conn = db.get_db()
    try:
        d = body()
        token = (require(d, "token") or "").strip()
        password = d.get("password") or ""
        row = conn.execute(
            "SELECT id, email, reset_expires FROM users WHERE reset_token=? AND reset_token IS NOT NULL",
            (db.token_hash(token),)).fetchone()
        if not row:
            raise ApiError("Невалиден или изтекъл линк за възстановяване", 400, code="reset_invalid")
        # Checked after the token, so a bad password cannot be used to find out
        # whether a reset link is live.
        bad = password_problem(password, row["email"])
        if bad:
            raise password_error(bad)
        try:
            expired = datetime.utcnow() > datetime.strptime(row["reset_expires"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            expired = True
        if expired:
            conn.execute("UPDATE users SET reset_token=NULL, reset_expires=NULL WHERE id=?", (row["id"],))
            conn.commit()
            raise ApiError("Невалиден или изтекъл линк за възстановяване", 400, code="reset_invalid")
        conn.execute("UPDATE users SET password_hash=?, reset_token=NULL, reset_expires=NULL WHERE id=?",
                     (generate_password_hash(password), row["id"]))
        # A password is reset because it may be in someone else's hands. Every
        # session opened with the old one goes, on every device, not just the
        # cookie in front of us.
        revoke_sessions(conn, row["id"])
        conn.commit()
        clear_login_fails(row["email"])
        session.clear()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/auth/password")
@login_required
def password_change():
    """Change your own password.

    There was no way to do this at all: an account whose password had been
    seen over a shoulder could only be rescued by the reset flow, which needs a
    mailbox. The current password is required, because a session left open on a
    borrowed machine must not be enough to lock its owner out.
    """
    conn, user = g.conn, g.user
    d = body()
    current = d.get("current") or ""
    new = d.get("password") or ""
    row = conn.execute("SELECT password_hash, email FROM users WHERE id=?", (user["id"],)).fetchone()
    if not check_password_hash(row["password_hash"], current):
        # Counted here and nowhere else. What is worth limiting is guessing at
        # the current password; a person being told four times that their new
        # one is too weak is doing the right thing, and must not be locked out
        # of the form for it.
        if rate_limited("pwch:%d" % user["id"], 10, 900):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        raise ApiError("Грешна парола", 403, code="bad_password")
    if check_password_hash(row["password_hash"], new):
        raise ApiError("Новата парола съвпада със старата", 400, code="password_same")
    org = user_primary_org(conn, user["id"])
    bad = password_problem(new, row["email"], (org or {})["name"] if org else "")
    if bad:
        raise password_error(bad)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(new), user["id"]))
    # Everywhere else is signed out, here is not. Changing a password is how
    # somebody removes a session they cannot see; keeping their own open is
    # what makes the action usable.
    revoke_sessions(conn, user["id"], keep_sid=session.get("sid"))
    conn.commit()
    clear_login_fails(row["email"])
    return jsonify({"ok": True})


@app.get("/api/auth/password-rules")
def password_rules():
    """What the server will accept, so the form can say it before the person
    finds out by being refused.

    The blocklist goes with it. It is not a secret - it is the list of
    passwords everybody already knows - and without it the meter in the browser
    calls "password1" acceptable while the server refuses it, which is worse
    than having no meter at all.
    """
    return jsonify({"min": PASSWORD_MIN, "needs_letter": True, "needs_digit": True,
                    "blocks_common": True, "blocks_obvious": True,
                    "common": sorted(COMMON_PASSWORDS)})


@app.post("/api/auth/verify/<token>")
def verify_email(token):
    conn = db.get_db()
    try:
        row = conn.execute("SELECT id FROM users WHERE verify_token=?",
                           (db.token_hash(token),)).fetchone()
        if not row:
            raise ApiError("Невалиден или изтекъл линк за потвърждение", 400, code="verify_invalid")
        conn.execute("UPDATE users SET verified=1, verify_token=NULL WHERE id=?", (row["id"],))
        conn.commit()
        return jsonify({"ok": True})
    except ApiError:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/api/me")
@login_required
def me():
    conn = g.conn
    org = user_primary_org(conn, g.user["id"])
    row = conn.execute("SELECT verified FROM users WHERE id=?", (g.user["id"],)).fetchone()
    user = dict(g.user)
    user["verified"] = bool(row["verified"])

    # Without SMTP the verification link has nowhere to go, so the app shows it
    # in a banner. The stored column holds a hash and cannot be handed back, so
    # a fresh token is issued for the banner and the old one stops working -
    # which is the right behaviour for a single-use link anyway.
    verify_token = None
    if not row["verified"] and not mailer.enabled():
        verify_token = secrets.token_urlsafe(24)
        conn.execute("UPDATE users SET verify_token=? WHERE id=?",
                     (db.token_hash(verify_token), g.user["id"]))
    return jsonify({"user": user, "org": org, "verify_token": verify_token, "csrf": issue_csrf()})


# --------------------------------------------------------------------------- #
#  Templates
# --------------------------------------------------------------------------- #
def org_addons(conn, org_id, template_key=None):
    """Which add-ons a company has switched on, as {template: [keys]}."""
    sql = "SELECT template_key, addon_key FROM org_addons WHERE org_id=?"
    args = [org_id]
    if template_key:
        sql += " AND template_key=?"
        args.append(template_key)
    out = {}
    for r in conn.execute(sql + " ORDER BY enabled_at, addon_key", args).fetchall():
        out.setdefault(r["template_key"], []).append(r["addon_key"])
    return out


def resolved_templates(conn, org_id):
    """Every template as this company has configured it.

    The browser renders forms and pipelines straight from this, so a company
    that switched on актуване sees the act fields appear in the form without
    the front end knowing anything about construction.
    """
    on = org_addons(conn, org_id) if org_id else {}
    return {k: (addons.resolve(k, on.get(k, [])) or v) for k, v in TEMPLATES.items()}


@app.get("/api/templates")
def templates():
    """Anonymous callers get the plain catalogue; a signed-in one gets their
    own company's configuration, which is what the forms are drawn from."""
    if "uid" not in session:
        return jsonify(TEMPLATES)
    conn = db.get_db()
    org = user_primary_org(conn, session["uid"])
    return jsonify(resolved_templates(conn, org["id"] if org else 0))


@app.get("/api/addons")
@login_required
def addons_list():
    """The catalogue of depth available to this company, grouped, with what is
    already switched on."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    want = (request.args.get("template") or "").strip()
    keys = [want] if want in TEMPLATES else list(TEMPLATES)
    on = org_addons(conn, org["id"])
    sectors, _asked = org_sectors(org)
    out = {}
    for tkey in keys:
        groups = []
        for gname, members in addons.by_group(tkey).items():
            groups.append({"group": gname, "items": [{
                "key": k,
                "label": addons.ADDONS[k]["label"],
                "tagline": addons.ADDONS[k]["tagline"],
                "fields": len(addons.ADDONS[k].get("fields", [])),
                "terms": len(addons.ADDONS[k].get("terms", [])),
                "stages": [{"key": s["key"], "label": s["label"]}
                           for s in addons.ADDONS[k].get("stages", [])],
                "metrics": len(addons.ADDONS[k].get("metrics", [])),
                # Highlighted rather than filtered: a company that also does
                # something outside its stated trades must still find it.
                "mine": bool(set(addons.ADDONS[k].get("sectors", ())) & set(sectors)),
                "on": k in on.get(tkey, []),
            } for k in members]})
        groups.sort(key=lambda g: addons.GROUPS.index(g["group"]))
        out[tkey] = {"label": TEMPLATES[tkey]["label"], "groups": groups,
                     "on": on.get(tkey, [])}
    return jsonify({"templates": out, "groups": list(addons.GROUPS)})


@app.post("/api/addons/preview")
@login_required
def addons_preview():
    """The form and the pipeline as this selection would leave them.

    Switching on five add-ons blind and only then discovering a form of forty
    fields is how a useful feature becomes one nobody trusts. Every row says
    which add-on put it there, so the cost of each is visible before saving.
    """
    conn = g.conn
    d = body()
    tkey = (d.get("template") or "").strip()
    if tkey not in TEMPLATES:
        raise ApiError("Невалиден шаблон", 400, code="invalid_value", info={"field": "template"})
    want = [str(x) for x in (d.get("addons") or []) if str(x) in set(addons.available(tkey))]
    r = addons.resolve(tkey, want)
    base = TEMPLATES[tkey]
    origin = {}
    for akey in want:
        for f in addons.ADDONS[akey].get("fields", []) + addons.ADDONS[akey].get("terms", []):
            origin[f["key"]] = akey
        for s in addons.ADDONS[akey].get("stages", []):
            origin[s["key"]] = akey
    base_stages = {s["key"] for s in base.get("stages", [])}

    def rows(items):
        return [{"key": f["key"], "label": f["label"], "type": f["type"],
                 "options": f.get("options", []), "from": origin.get(f["key"], "")}
                for f in items]
    return jsonify({
        "template": tkey, "addons": r["addons"],
        "fields": rows(r["fields"]), "terms": rows(r["terms"]),
        "stages": [{"key": s["key"], "label": s["label"],
                    "from": "" if s["key"] in base_stages else origin.get(s["key"], "")}
                   for s in r["stages"]],
        "metrics": [{"key": m["key"], "label": m["label"], "unit": m.get("unit", ""),
                     "from": m.get("addon", "")} for m in r["metrics"]],
    })


@app.put("/api/addons")
@login_required
def addons_set():
    """Switch depth on or off for one template.

    Switching an add-on off leaves everything already recorded under it alone.
    The fields stop being offered on new records; they are not deleted from
    the ones that carry them, because that would quietly destroy an agreed
    term somebody signed.
    """
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = body()
    tkey = (d.get("template") or "").strip()
    if tkey not in TEMPLATES:
        raise ApiError("Невалиден шаблон", 400, code="invalid_value", info={"field": "template"})
    want = d.get("addons")
    if not isinstance(want, list):
        raise ApiError("Липсва списък", 400, code="field_required", info={"field": "addons"})
    fits = set(addons.available(tkey))
    asked = list(dict.fromkeys(str(x) for x in want))
    keep = [k for k in asked if k in fits]
    if len(keep) != len(asked):
        raise ApiError("Добавка, която не е за този шаблон", 400,
                       code="invalid_value", info={"field": "addons"})
    have = set(org_addons(conn, org["id"], tkey).get(tkey, []))
    for k in have - set(keep):
        conn.execute("DELETE FROM org_addons WHERE org_id=? AND template_key=? AND addon_key=?",
                     (org["id"], tkey, k))
    for k in keep:
        conn.execute("INSERT OR IGNORE INTO org_addons "
                     "(org_id, template_key, addon_key, enabled_by) VALUES (?,?,?,?)",
                     (org["id"], tkey, k, user["id"]))
    conn.commit()
    return jsonify({"template": tkey, "on": keep,
                    "resolved": addons.resolve(tkey, keep)})


@app.get("/api/captcha")
def captcha():
    cid, svg = captcha_mod.new_challenge()
    return jsonify({"id": cid, "svg": svg})


# --------------------------------------------------------------------------- #
#  Custom (paid) work templates
# --------------------------------------------------------------------------- #
def _ct_row(r):
    cfg = json.loads(r["config_json"] or "{}")
    return {
        "id": r["id"], "key": r["key"], "label": r["label"], "config": cfg,
        "plan": r["plan"], "price": r["price"], "status": r["status"],
        "paid_until": r["paid_until"], "pay_method": r["pay_method"], "invoice_no": r["invoice_no"],
    }


def _build_template_config(d):
    label = (d.get("label") or "").strip()
    if not label:
        raise ApiError("Липсва име на шаблона", code="field_required", info={"field": "label"})
    item_noun = (d.get("item_noun") or label).strip()

    def mk(items, prefix, with_required):
        out = []
        for i, it in enumerate(items or []):
            lbl = (it.get("label") or "").strip()
            if not lbl:
                continue
            typ = it.get("type") if it.get("type") in FIELD_TYPES else "text"
            spec = {"key": "%s%d" % (prefix, len(out) + 1), "label": lbl, "type": typ}
            if typ == "select":
                opts = [o.strip() for o in (it.get("options") or []) if o and o.strip()]
                if opts:
                    spec["options"] = opts
            if with_required and it.get("required"):
                spec["required"] = True
            out.append(spec)
        return out

    fields = mk(d.get("fields"), "f", True)
    terms = mk(d.get("terms"), "t", False)
    stages = mk(d.get("stages"), "s", False)
    if len(stages) < 2:
        raise ApiError("Нужни са поне 2 етапа", code="too_few_stages")
    ascii_prefix = re.sub(r"[^A-Za-z0-9]", "", item_noun).upper()[:3] or "ORD"
    return {
        "label": label, "tagline": (d.get("tagline") or "").strip(), "item_noun": item_noun,
        "ref_prefix": ascii_prefix, "custom": True,
        "fields": fields, "terms": terms, "stages": stages, "filters": [],
    }


@app.get("/api/custom-templates")
@login_required
def list_custom_templates():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        return jsonify([])
    rows = conn.execute("SELECT * FROM custom_templates WHERE org_id=? ORDER BY id DESC", (org["id"],)).fetchall()
    return jsonify([_ct_row(r) for r in rows])


@app.post("/api/custom-templates")
@login_required
def create_custom_template():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org or org["kind"] != "business":
        raise ApiError("Само бизнес акаунт може да създава шаблони", 403, code="only_business_creates")
    require_verified(conn, user["id"])
    cfg = _build_template_config(body())
    key = "cust_" + secrets.token_hex(5)
    cur = conn.execute(
        "INSERT INTO custom_templates (org_id, key, label, config_json, status, created_by) VALUES (?,?,?,?, 'draft', ?)",
        (org["id"], key, cfg["label"], json.dumps(cfg, ensure_ascii=False), user["id"]),
    )
    row = conn.execute("SELECT * FROM custom_templates WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(_ct_row(row))


# ---- payments: Stripe Checkout (card) + proforma invoice (bank transfer) ---
STRIPE_KEY_FILE = os.path.join(BASE, ".stripe_key")
STRIPE_AMOUNTS = {"monthly": 1200, "onetime": 12000}  # cents, EUR


def stripe_key():
    k = os.environ.get("STRIPE_SECRET_KEY")
    if k:
        return k.strip()
    if os.path.exists(STRIPE_KEY_FILE):
        with open(STRIPE_KEY_FILE, "r") as f:
            return f.read().strip()
    return ""


# Merchant-of-Record / payment-link path: works WITHOUT a registered company.
# The operator pastes checkout links from Lemon Squeezy, Gumroad, Paddle,
# PayPal.me, Revolut etc. (MoR providers onboard individuals and handle VAT).
PAY_LINKS_FILE = os.path.join(BASE, ".payment_links")


def payment_link(plan):
    env = os.environ.get("SEAM_PAY_LINK_" + plan.upper()) or os.environ.get("SEAM_PAY_LINK")
    if env:
        return env.strip()
    if os.path.exists(PAY_LINKS_FILE):
        try:
            with open(PAY_LINKS_FILE, "r", encoding="utf-8") as f:
                j = json.load(f)
            return (j.get(plan) or j.get("link") or "").strip() or None
        except Exception:
            return None
    return None


def payment_provider():
    if stripe_key():
        return "stripe"
    if payment_link("monthly") or payment_link("onetime"):
        return "link"
    return "demo"


# Lemon Squeezy webhook: auto-activate templates when the MoR reports a paid
# order. Signing secret comes from the LS dashboard (Settings -> Webhooks).
LEMON_SECRET_FILE = os.path.join(BASE, ".lemon_webhook_secret")


def lemon_secret():
    s = os.environ.get("SEAM_LEMON_WEBHOOK_SECRET")
    if s:
        return s.strip()
    if os.path.exists(LEMON_SECRET_FILE):
        with open(LEMON_SECRET_FILE, "r") as f:
            return f.read().strip()
    return ""


STRIPE_WH_FILE = os.path.join(BASE, ".stripe_webhook_secret")


def stripe_webhook_secret():
    s = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if s:
        return s.strip()
    if os.path.exists(STRIPE_WH_FILE):
        with open(STRIPE_WH_FILE, "r") as f:
            return f.read().strip()
    return ""


def stripe_api(method, path, params=None):
    import urllib.parse as up
    data = up.urlencode(params or {}).encode("utf-8") if params else None
    req = urllib.request.Request(
        "https://api.stripe.com" + path, data=data,
        headers={"Authorization": "Bearer " + stripe_key(),
                 "content-type": "application/x-www-form-urlencoded",
                 "user-agent": "Seam/1.0"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
#  Granting a paid plan
#
#  Everything that makes a company paid goes through activate_plan(), and
#  activate_plan() is never reachable from a request the buyer controls. There
#  are exactly three ways in:
#
#    1. a signed webhook from the payment processor,
#    2. asking the processor directly about a checkout session it issued,
#    3. an administrator of this installation approving a bank transfer that
#       they can see in their own statement.
#
#  The button a customer presses after paying by transfer records a claim. A
#  claim is a request for (3); on its own it grants nothing. Getting this wrong
#  once meant any account could hand itself a paid plan with a single request.
# --------------------------------------------------------------------------- #
PLAN_DAYS = 30
#: A subscription that renews on the 30th and a webhook that lands a few hours
#: late should not leave a customer locked out in between.
PLAN_GRACE_DAYS = 3


def org_is_paid(org):
    """Paid means a paid plan that has not run out.

    `plan_until` used to be written and never read, so a plan that stopped being
    paid for stayed paid forever."""
    if not org:
        return False
    plan = (org["plan"] if not isinstance(org, dict) else org.get("plan")) or "starter"
    if plan not in PAID_PLANS:
        return False
    until = (org["plan_until"] if not isinstance(org, dict) else org.get("plan_until")) or ""
    if not until:
        # Enterprise is a contract, not a subscription: no end date means no end.
        return True
    try:
        end = datetime.strptime(until[:10], "%Y-%m-%d") + timedelta(days=PLAN_GRACE_DAYS)
    except ValueError:
        return False
    return end >= datetime.utcnow()


def activate_plan(conn, org_id, days=PLAN_DAYS, method="card", plan="pro", ref=""):
    """Extend, do not reset. A renewal arriving before the current period ends
    adds to it, so paying early never costs the customer days."""
    row = conn.execute("SELECT plan, plan_until FROM orgs WHERE id=?", (org_id,)).fetchone()
    if not row:
        return None
    now = datetime.utcnow()
    start = now
    if (row["plan"] or "") in PAID_PLANS and row["plan_until"]:
        try:
            have = datetime.strptime(row["plan_until"][:10], "%Y-%m-%d")
            if have > now:
                start = have
        except ValueError:
            pass
    until = (start + timedelta(days=days)).strftime("%Y-%m-%d")
    conn.execute("UPDATE orgs SET plan=?, plan_until=?, pay_method=? WHERE id=?",
                 (plan, until, method, org_id))
    billing_log(conn, "local", "grant:%s" % secrets.token_hex(8), "plan_activated", org_id,
                "%s until %s via %s %s" % (plan, until, method, ref))
    for uid in org_user_ids(conn, org_id):
        conn.execute("INSERT INTO notifications (user_id, kind, body, meta_json) "
                     "VALUES (?, 'plan_active', 'plan_active', ?)",
                     (uid, json.dumps({"plan": plan, "until": until}, ensure_ascii=False)))
    return until


def downgrade_plan(conn, org_id, reason="cancelled"):
    """A cancelled or refunded subscription stops being paid. The company keeps
    everything it made - only the paid features close."""
    conn.execute("UPDATE orgs SET plan='starter', plan_until=NULL WHERE id=?", (org_id,))
    billing_log(conn, "local", "end:%s" % secrets.token_hex(8), "plan_ended", org_id, reason)


def billing_log(conn, provider, event_id, name="", org_id=None, result=""):
    """The money ledger. Everything that moved a plan leaves a line here."""
    try:
        conn.execute("INSERT INTO billing_events (provider, event_id, event_name, org_id, result) "
                     "VALUES (?,?,?,?,?)", (provider, str(event_id), name, org_id, result))
        return True
    except sqlite3.IntegrityError:
        return False


def billing_event_seen(conn, provider, event_id, name="", org_id=None, result=""):
    """True when this exact event was already acted on. Processors re-deliver
    webhooks by design and on failure; without this, one payment could grant
    three months."""
    if not event_id:
        return False
    return not billing_log(conn, provider, event_id, name, org_id, result)


def _activate_template(conn, tid, plan, price, pay_method):
    paid_until = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d") if plan == "monthly" else None
    conn.execute(
        "UPDATE custom_templates SET plan=?, price=?, status='active', paid_until=?, pay_method=? WHERE id=?",
        (plan, price, paid_until, pay_method, tid),
    )


def _own_template(conn, user, tid):
    org = user_primary_org(conn, user["id"])
    row = conn.execute("SELECT * FROM custom_templates WHERE id=? AND org_id=?", (tid, org["id"] if org else 0)).fetchone()
    if not row:
        raise ApiError("Шаблонът не е намерен", 404, code="not_found")
    return row


@app.post("/api/custom-templates/<int:tid>/checkout")
@login_required
def checkout_custom_template(tid):
    """Card via Stripe Checkout (real when a Stripe key is configured, instant
    demo activation otherwise) or bank transfer via a proforma invoice."""
    conn, user = g.conn, g.user
    row = _own_template(conn, user, tid)
    d = body()
    plan = require(d, "plan")
    if plan not in CUSTOM_TPL_PRICING:
        raise ApiError("Невалиден план", code="invalid_plan")
    method = d.get("method") if d.get("method") in ("card", "invoice") else "card"
    price = CUSTOM_TPL_PRICING[plan]

    if method == "invoice":
        invoice_no = "PF-%s-%04d" % (datetime.utcnow().strftime("%Y%m"), tid)
        conn.execute(
            "UPDATE custom_templates SET plan=?, price=?, status='pending_invoice', pay_method='invoice', invoice_no=? WHERE id=?",
            (plan, price, invoice_no, tid),
        )
        row = conn.execute("SELECT * FROM custom_templates WHERE id=?", (tid,)).fetchone()
        return jsonify(_ct_row(row))

    if not stripe_key():
        link = payment_link(plan)
        if link:
            # Merchant-of-Record / payment link: open the external checkout,
            # template waits for confirmation (same flow as the proforma).
            conn.execute(
                "UPDATE custom_templates SET plan=?, price=?, status='pending_invoice', pay_method='link' WHERE id=?",
                (plan, price, tid),
            )
            row = conn.execute("SELECT * FROM custom_templates WHERE id=?", (tid,)).fetchone()
            out = _ct_row(row)
            # Pass the template id as Lemon Squeezy custom data so the webhook
            # can auto-activate the right template after payment.
            sep = "&" if "?" in link else "?"
            out["payment_url"] = link + sep + "checkout[custom][tpl]=" + str(tid)
            return jsonify(out)
        # Nothing configured (self-host demo): activate instantly, clearly demo.
        _activate_template(conn, tid, plan, price, "card_demo")
        row = conn.execute("SELECT * FROM custom_templates WHERE id=?", (tid,)).fetchone()
        return jsonify(_ct_row(row))

    base = request.url_root.rstrip("/")
    try:
        sess = stripe_api("POST", "/v1/checkout/sessions", {
            "mode": "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "eur",
            "line_items[0][price_data][unit_amount]": str(STRIPE_AMOUNTS[plan]),
            "line_items[0][price_data][product_data][name]": "Seam template: %s (%s)" % (row["label"], plan),
            "success_url": base + "/?stripe_tpl=%d&session_id={CHECKOUT_SESSION_ID}" % tid,
            "cancel_url": base + "/",
        })
    except urllib.error.HTTPError as e:
        raise ApiError("Stripe грешка: %s" % e.read().decode("utf-8", "ignore")[:200], 502, code="stripe_error")
    except Exception as e:
        raise ApiError("Stripe недостъпен: %s" % str(e)[:200], 502, code="stripe_error")
    conn.execute("UPDATE custom_templates SET plan=?, price=?, pay_method='card', stripe_session=? WHERE id=?",
                 (plan, price, sess.get("id"), tid))
    return jsonify({"checkout_url": sess.get("url"), "id": tid})


@app.post("/api/custom-templates/<int:tid>/confirm-stripe")
@login_required
def confirm_stripe(tid):
    conn, user = g.conn, g.user
    row = _own_template(conn, user, tid)
    sid = require(body(), "session_id")
    if not row["stripe_session"] or row["stripe_session"] != sid:
        raise ApiError("Невалидна сесия за плащане", 400, code="stripe_error")
    try:
        sess = stripe_api("GET", "/v1/checkout/sessions/" + sid)
    except Exception as e:
        raise ApiError("Stripe недостъпен: %s" % str(e)[:200], 502, code="stripe_error")
    if sess.get("payment_status") != "paid":
        raise ApiError("Плащането не е завършено", 402, code="stripe_unpaid")
    _activate_template(conn, tid, row["plan"], row["price"], "card")
    r2 = conn.execute("SELECT * FROM custom_templates WHERE id=?", (tid,)).fetchone()
    return jsonify(_ct_row(r2))


@app.post("/api/custom-templates/<int:tid>/confirm-invoice")
@login_required
def confirm_invoice(tid):
    """The buyer says the transfer has gone out.

    That is a claim, not a payment. It used to activate the template on the
    spot, which made the proforma path a free-of-charge button. The operator of
    this installation is the one who can see the money arrive, so it goes to
    them.
    """
    conn, user = g.conn, g.user
    row = _own_template(conn, user, tid)
    if row["status"] != "pending_invoice":
        raise ApiError("Няма чакаща проформа", 400, code="not_found")
    org = user_primary_org(conn, user["id"])
    if conn.execute("SELECT 1 FROM billing_claims WHERE kind='template' AND ref=? AND status='pending'",
                    (str(tid),)).fetchone():
        return jsonify(dict(_ct_row(row), status="awaiting_review"))
    conn.execute("INSERT INTO billing_claims (org_id, user_id, kind, ref, plan, amount, note) "
                 "VALUES (?,?,'template',?,?,?,?)",
                 (org["id"] if org else 0, user["id"], str(tid), row["plan"] or "",
                  row["price"] or "", (body().get("note") or "").strip()[:400]))
    for uid in _instance_admin_ids(conn):
        conn.execute("INSERT INTO notifications (user_id, kind, body, meta_json) "
                     "VALUES (?, 'billing_claim', 'billing_claim', ?)",
                     (uid, json.dumps({"org": org["name"] if org else "", "template": row["label"]},
                                      ensure_ascii=False)))
    r2 = conn.execute("SELECT * FROM custom_templates WHERE id=?", (tid,)).fetchone()
    return jsonify(dict(_ct_row(r2), status="awaiting_review"))


PRO_I18N = {
    "en": {"reg_label": "Company number", "title": "Proforma invoice", "seller": "Provider", "buyer": "Customer", "item": "Service", "monthly": "Monthly subscription", "onetime": "One-time price", "amount": "Amount due", "bank": "Bank details", "note": "A proforma is not a tax document. Activation upon receipt of payment.", "date": "Date", "reg": "Company no."},
    "es": {"reg_label": "Número de registro", "title": "Factura proforma", "seller": "Proveedor", "buyer": "Cliente", "item": "Servicio", "monthly": "Suscripción mensual", "onetime": "Precio único", "amount": "Importe a pagar", "bank": "Datos bancarios", "note": "Una proforma no es un documento fiscal. Activación al recibir el pago.", "date": "Fecha", "reg": "N.º de empresa"},
    "fr": {"reg_label": "Numéro d'immatriculation", "title": "Facture proforma", "seller": "Fournisseur", "buyer": "Client", "item": "Service", "monthly": "Abonnement mensuel", "onetime": "Prix unique", "amount": "Montant dû", "bank": "Coordonnées bancaires", "note": "Une proforma n'est pas un document fiscal. Activation à réception du paiement.", "date": "Date", "reg": "N° d'entreprise"},
    "pl": {"reg_label": "Numer rejestrowy", "title": "Faktura proforma", "seller": "Dostawca", "buyer": "Klient", "item": "Usługa", "monthly": "Abonament miesięczny", "onetime": "Cena jednorazowa", "amount": "Kwota do zapłaty", "bank": "Dane bankowe", "note": "Proforma nie jest dokumentem podatkowym. Aktywacja po otrzymaniu płatności.", "date": "Data", "reg": "Nr firmy"},
    "uk": {"reg_label": "Реєстраційний номер", "title": "Проформа-рахунок", "seller": "Постачальник", "buyer": "Клієнт", "item": "Послуга", "monthly": "Місячна підписка", "onetime": "Одноразова ціна", "amount": "Сума до сплати", "bank": "Банківські реквізити", "note": "Проформа не є податковим документом. Активація після надходження оплати.", "date": "Дата", "reg": "Код компанії"},
    "pt": {"reg_label": "Número de registo", "title": "Fatura proforma", "seller": "Fornecedor", "buyer": "Cliente", "item": "Serviço", "monthly": "Subscrição mensal", "onetime": "Preço único", "amount": "Montante a pagar", "bank": "Dados bancários", "note": "Uma proforma não é um documento fiscal. Ativação após receção do pagamento.", "date": "Data", "reg": "N.º de empresa"},
    "bg": {"reg_label": "Фирмен номер", "title": "Проформа фактура", "seller": "Доставчик", "buyer": "Клиент", "item": "Услуга", "monthly": "Месечен абонамент", "onetime": "Еднократна цена", "amount": "Дължима сума", "bank": "Банкови данни", "note": "Проформата не е данъчен документ. Активиране след постъпване на плащането.", "date": "Дата", "reg": "ЕИК / рег. №"},
    "de": {"reg_label": "Firmennummer", "title": "Proforma-Rechnung", "seller": "Anbieter", "buyer": "Kunde", "item": "Leistung", "monthly": "Monatsabonnement", "onetime": "Einmalpreis", "amount": "Fälliger Betrag", "bank": "Bankverbindung", "note": "Eine Proforma ist kein Steuerdokument. Aktivierung nach Zahlungseingang.", "date": "Datum", "reg": "Firmen-Nr."},
    "ro": {"reg_label": "Număr de înregistrare", "title": "Factură proformă", "seller": "Furnizor", "buyer": "Client", "item": "Serviciu", "monthly": "Abonament lunar", "onetime": "Preț unic", "amount": "Suma datorată", "bank": "Date bancare", "note": "Proforma nu este document fiscal. Activare după primirea plății.", "date": "Data", "reg": "Nr. firmă"},
    "el": {"reg_label": "Αριθμός μητρώου", "title": "Προτιμολόγιο", "seller": "Πάροχος", "buyer": "Πελάτης", "item": "Υπηρεσία", "monthly": "Μηνιαία συνδρομή", "onetime": "Εφάπαξ τιμή", "amount": "Οφειλόμενο ποσό", "bank": "Τραπεζικά στοιχεία", "note": "Το προτιμολόγιο δεν είναι φορολογικό παραστατικό. Ενεργοποίηση μετά την πληρωμή.", "date": "Ημερομηνία", "reg": "Αρ. εταιρείας"},
    "tr": {"reg_label": "Sicil numarası", "title": "Proforma fatura", "seller": "Sağlayıcı", "buyer": "Müşteri", "item": "Hizmet", "monthly": "Aylık abonelik", "onetime": "Tek seferlik fiyat", "amount": "Ödenecek tutar", "bank": "Banka bilgileri", "note": "Proforma vergi belgesi değildir. Ödeme alındıktan sonra etkinleştirilir.", "date": "Tarih", "reg": "Şirket no"},
    "it": {"reg_label": "Numero di registro", "title": "Fattura proforma", "seller": "Fornitore", "buyer": "Cliente", "item": "Servizio", "monthly": "Abbonamento mensile", "onetime": "Prezzo una tantum", "amount": "Importo dovuto", "bank": "Coordinate bancarie", "note": "La proforma non è un documento fiscale. Attivazione al ricevimento del pagamento.", "date": "Data", "reg": "N. azienda"},
    "ru": {"reg_label": "Регистрационный номер", "title": "Проформа-счёт", "seller": "Поставщик", "buyer": "Клиент", "item": "Услуга", "monthly": "Ежемесячная подписка", "onetime": "Разовая цена", "amount": "Сумма к оплате", "bank": "Банковские реквизиты", "note": "Проформа не является налоговым документом. Активация после поступления оплаты.", "date": "Дата", "reg": "Рег. №"},
}


@app.get("/proforma/<int:tid>")
def proforma_page(tid):
    conn = db.get_db()
    try:
        uid = session.get("uid")
        if not uid:
            return "", 401
        user = conn.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
        org = user_primary_org(conn, uid)
        row = conn.execute("SELECT * FROM custom_templates WHERE id=? AND org_id=?", (tid, org["id"] if org else 0)).fetchone()
        if not user or not row or not row["invoice_no"]:
            return "", 404
        lang = req_lang() if req_lang() in PRO_I18N else "en"
        L = PRO_I18N[lang]
        amount_eur = STRIPE_AMOUNTS.get(row["plan"], 0) / 100.0
        amount_line = "%.2f €" % amount_eur
        bank = os.environ.get("SEAM_BANK_DETAILS", "IBAN: BGxx XXXX XXXX XXXX XXXX (SEAM_BANK_DETAILS)")
        plan_label = L["monthly"] if row["plan"] == "monthly" else L["onetime"]
        html_doc = ("""<!DOCTYPE html><html lang="%(lang)s"><head><meta charset="utf-8"><meta name="color-scheme" content="light"><title>%(inv)s</title>
<style>html{background:#fff}body{font-family:'Segoe UI',Arial,sans-serif;color:#1a1f2b;background:#fff;max-width:640px;margin:40px auto;padding:0 20px}
h1{font-size:22px}table{width:100%%;border-collapse:collapse;margin:18px 0}
td{padding:9px 6px;border-bottom:1px solid #e6e8ec;font-size:14px}td:first-child{color:#5b6472;width:200px}
.total{font-size:18px;font-weight:700}.note{color:#8a93a3;font-size:12.5px;margin-top:22px}
@media print{.noprint{display:none}}</style></head><body>
<h1>%(title)s <span style="color:#2f6df0">%(inv)s</span></h1>
<table>
<tr><td>%(date)s</td><td>%(today)s</td></tr>
<tr><td>%(seller)s</td><td>Seam Platform</td></tr>
<tr><td>%(buyer)s</td><td>%(org)s%(regline)s</td></tr>
<tr><td>%(item)s</td><td>%(label)s · %(plan)s</td></tr>
<tr><td>%(amount)s</td><td class="total">%(amountv)s</td></tr>
<tr><td>%(bankl)s</td><td>%(bank)s</td></tr>
</table>
<p class="note">%(note)s</p>
</body></html>""") % {
            "lang": lang, "title": L["title"], "inv": html_escape(row["invoice_no"]),
            "date": L["date"], "today": datetime.utcnow().strftime("%Y-%m-%d"),
            "seller": L["seller"], "buyer": L["buyer"],
            "org": html_escape(org["name"]),
            "regline": (" · %s %s" % (L["reg"], html_escape(org["reg_number"]))) if org.get("reg_number") else "",
            "item": L["item"], "label": html_escape(row["label"]), "plan": plan_label,
            "amount": L["amount"], "amountv": amount_line,
            "bankl": L["bank"], "bank": html_escape(bank), "note": L["note"],
        }
        return html_doc
    finally:
        conn.close()


@app.delete("/api/custom-templates/<int:tid>")
@login_required
def delete_custom_template(tid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    row = conn.execute("SELECT key FROM custom_templates WHERE id=? AND org_id=?", (tid, org["id"] if org else 0)).fetchone()
    if not row:
        raise ApiError("Шаблонът не е намерен", 404, code="not_found")
    used = conn.execute("SELECT 1 FROM workspaces WHERE template=? LIMIT 1", (row["key"],)).fetchone()
    if used:
        raise ApiError("Шаблонът се използва от пространство", code="template_in_use")
    conn.execute("DELETE FROM custom_templates WHERE id=?", (tid,))
    return jsonify({"ok": True})


#: Display prices, taken from entitlements so the screen, the checkout and
#: the gate cannot disagree. Enterprise is a conversation, not a price.
PLAN_PRICES = {"starter": "", "ent": "",
               "pro": "EUR %d" % entitlements.PLANS["pro"]["price"],
               "half": "EUR %d" % entitlements.PLANS["half"]["price"],
               "year": "EUR %d" % entitlements.PLANS["year"]["price"],
               "life": "EUR %d" % entitlements.PLANS["life"]["price"]}


@app.get("/api/pricing")
def pricing():
    out = dict(CUSTOM_TPL_PRICING)
    out["stripe"] = bool(stripe_key())
    out["provider"] = payment_provider()
    out["webhook"] = bool(lemon_secret())
    out["plan"] = "starter"
    out["paid"] = False
    if "uid" in session:
        conn = db.get_db()
        try:
            org = user_primary_org(conn, session["uid"])
            if org:
                out["plan"] = org["plan"] or "starter"
                out["plan_until"] = org["plan_until"]
                out["paid"] = org_is_paid(org)
                # So the plans screen can say "waiting for the operator to
                # confirm your transfer" instead of offering the button again.
                claim = conn.execute(
                    "SELECT id, created_at FROM billing_claims WHERE org_id=? AND kind='plan' "
                    "AND status='pending' ORDER BY id DESC LIMIT 1", (org["id"],)).fetchone()
                out["claim_pending"] = bool(claim)
                if claim:
                    out["claim_at"] = claim["created_at"]
        finally:
            conn.close()
    return jsonify(out)


@app.post("/api/plans/checkout")
@login_required
def plan_checkout():
    """Finalized plan billing: Starter is free, Pro goes through the configured
    payment path (Stripe / Merchant-of-Record link / proforma), Enterprise is
    a sales conversation."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    plan = (require(body(), "plan") or "").strip()
    if plan not in PLAN_PRICES:
        raise ApiError("Невалиден план", code="invalid_plan")

    if plan == "starter":
        conn.execute("UPDATE orgs SET plan='starter', plan_until=NULL WHERE id=?", (org["id"],))
        return jsonify({"plan": "starter", "status": "active"})
    if plan == "ent":
        return jsonify({"plan": "ent", "status": "contact", "email": "sales@seam.example"})

    _spec = entitlements.PLANS.get(plan) or entitlements.PLANS["pro"]
    link = payment_link("plan_" + plan) or payment_link("plan_pro") or payment_link("monthly")
    if link:
        sep = "&" if "?" in link else "?"
        conn.execute("UPDATE orgs SET plan='pro_pending' WHERE id=?", (org["id"],))
        return jsonify({"plan": "pro", "status": "pending",
                        "payment_url": link + sep + "checkout[custom][plan]=pro&checkout[custom][org]=%d" % org["id"]})
    if stripe_key():
        base = request.url_root.rstrip("/")
        try:
            fields = {
                # A recurring price is only valid in subscription mode. In
                # payment mode Stripe rejects the whole call, which meant this
                # path could never have taken a single euro.
                # A lifetime plan is a payment, not a subscription; billing it
                # as one would charge the customer again next month.
                "mode": "payment" if _spec["forever"] else "subscription",
                "line_items[0][quantity]": "1",
                "line_items[0][price_data][currency]": "eur",
                "line_items[0][price_data][unit_amount]": str(_spec["price"] * 100),
                "line_items[0][price_data][product_data][name]": "Seam %s" % plan,
                # Both, on purpose: the webhook reads metadata, and
                # client_reference_id survives into the subscription's invoices,
                # which is what renewals arrive as.
                "client_reference_id": str(org["id"]),
                "metadata[org]": str(org["id"]),
                # Which plan was bought, on the session only this server built.
                # The webhook reads it from there and nowhere else.
                "metadata[plan]": plan,
                "success_url": base + "/?plan=" + plan + "&session_id={CHECKOUT_SESSION_ID}",
                "cancel_url": base + "/#/plans",
            }
            if not _spec["forever"]:
                # Stripe bills by month or by year. A six-month period is
                # neither, so it is taken once and the plan runs its 182 days;
                # a recurring interval it does not have would either overcharge
                # or silently become monthly.
                if _spec["days"] == 182:
                    fields["mode"] = "payment"
                else:
                    fields["line_items[0][price_data][recurring][interval]"] = (
                        "year" if _spec["days"] == 365 else "month")
                fields["subscription_data[metadata][org]"] = str(org["id"])
                fields["subscription_data[metadata][plan]"] = plan
            sess = stripe_api("POST", "/v1/checkout/sessions", fields)
        except urllib.error.HTTPError as e:
            raise ApiError("Stripe грешка: %s" % e.read().decode("utf-8", "ignore")[:200],
                           502, code="stripe_error")
        except Exception as e:
            raise ApiError("Платежната услуга е недостъпна: %s" % str(e)[:120], 502, code="stripe_error")
        conn.execute("UPDATE orgs SET plan='pro_pending', pay_session=? WHERE id=?",
                     (sess.get("id"), org["id"]))
        return jsonify({"plan": "pro", "status": "pending", "payment_url": sess.get("url")})

    # Nothing configured: issue a proforma so the deal can still close by bank transfer.
    conn.execute("UPDATE orgs SET plan='pro_pending' WHERE id=?", (org["id"],))
    return jsonify({"plan": "pro", "status": "pending", "proforma": True})



@app.post("/api/features/checkout")
@login_required
def feature_checkout():
    """Buy one part of the platform on its own.

    The same road as a plan and for the same reason: this opens a session with
    the processor and records nothing. What was bought travels on the session's
    metadata, which only this server writes, and the part is granted when the
    signed webhook comes back. A browser that posts here and then closes has
    bought nothing.
    """
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = body()
    feature = (d.get("feature") or "").strip()
    kind = (d.get("kind") or "once").strip()
    if feature not in entitlements.FEATURE_PRICE:
        raise ApiError("Непозната услуга", 400, code="unknown_feature")
    if kind not in entitlements.PURCHASE_KINDS:
        kind = "once"
    price = entitlements.price_of(feature=feature, kind=kind)

    if stripe_key():
        base = request.url_root.rstrip("/")
        fields = {
            "mode": "payment" if kind == "once" else "subscription",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "eur",
            "line_items[0][price_data][unit_amount]": str(int(price) * 100),
            "line_items[0][price_data][product_data][name]": "Seam %s (%s)" % (feature, kind),
            "client_reference_id": str(org["id"]),
            "metadata[org]": str(org["id"]),
            "metadata[feature]": feature,
            "metadata[kind]": kind,
            "success_url": base + "/#/plans",
            "cancel_url": base + "/#/plans",
        }
        if kind != "once":
            fields["line_items[0][price_data][recurring][interval]"] = (
                "year" if kind == "year" else "month")
            fields["subscription_data[metadata][org]"] = str(org["id"])
            fields["subscription_data[metadata][feature]"] = feature
            fields["subscription_data[metadata][kind]"] = kind
        try:
            sess = stripe_api("POST", "/v1/checkout/sessions", fields)
        except urllib.error.HTTPError as e:
            raise ApiError("Stripe грешка: %s" % e.read().decode("utf-8", "ignore")[:200],
                           502, code="stripe_error")
        except Exception as e:
            raise ApiError("Платежната услуга е недостъпна: %s" % str(e)[:120],
                           502, code="stripe_error")
        return jsonify({"feature": feature, "kind": kind, "price": price,
                        "url": sess.get("url"), "status": "pending"})

    # No processor configured: the operator records the transfer, which is the
    # third proof and the only one left.
    return jsonify({"feature": feature, "kind": kind, "price": price,
                    "status": "transfer"})


@app.post("/api/plans/confirm")
@login_required
def plan_confirm():
    """Coming back from checkout.

    This used to switch the plan to Pro on the spot, for anybody who asked. It
    now proves the payment with Stripe before granting anything, and where
    there is nothing to ask - a bank transfer - it records a claim for the
    operator of this installation to approve against their own statement.
    """
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    if rate_limited("plan:%d" % org["id"], 20, 600):
        raise ApiError("Твърде много заявки", 429, code="rate_limited")
    sid = (body().get("session_id") or "").strip()[:200]

    # 1. The processor is asked directly. Only the session this server itself
    #    opened for this company counts, so a session id copied from somewhere
    #    else is worth nothing.
    if sid and stripe_key():
        if not org["pay_session"] or org["pay_session"] != sid:
            raise ApiError("Невалидна сесия за плащане", 400, code="stripe_error")
        try:
            sess = stripe_api("GET", "/v1/checkout/sessions/" + urllib.parse.quote(sid, safe=""))
        except Exception as e:
            raise ApiError("Stripe недостъпен: %s" % str(e)[:200], 502, code="stripe_error")
        if sess.get("payment_status") != "paid" and sess.get("status") != "complete":
            raise ApiError("Плащането не е завършено", 402, code="stripe_unpaid")
        if sess.get("customer"):
            conn.execute("UPDATE orgs SET pay_customer=? WHERE id=?", (sess["customer"], org["id"]))
        # The webhook may have arrived first; the shared id keeps it to one grant.
        if billing_event_seen(conn, "stripe", "cs:" + sid, "checkout.confirmed", org["id"], "return"):
            return jsonify({"plan": "pro", "status": "active",
                            "plan_until": org["plan_until"]})
        until = activate_plan(conn, org["id"], method="card", ref=sid)
        return jsonify({"plan": "pro", "status": "active", "plan_until": until})

    # 2. Nothing to ask: this is a claim, and it activates nothing by itself.
    if conn.execute("SELECT 1 FROM billing_claims WHERE org_id=? AND kind='plan' AND status='pending'",
                    (org["id"],)).fetchone():
        return jsonify({"plan": "pro", "status": "awaiting_review", "claimed": True})
    note = (body().get("note") or "").strip()[:400]
    conn.execute("INSERT INTO billing_claims (org_id, user_id, kind, plan, amount, note) "
                 "VALUES (?,?,'plan','pro',?,?)",
                 (org["id"], user["id"], PLAN_PRICES.get("pro", ""), note))
    conn.execute("UPDATE orgs SET plan='pro_pending' WHERE id=?", (org["id"],))
    for uid in _instance_admin_ids(conn):
        conn.execute("INSERT INTO notifications (user_id, kind, body, meta_json) "
                     "VALUES (?, 'billing_claim', 'billing_claim', ?)",
                     (uid, json.dumps({"org": org["name"], "plan": "pro"}, ensure_ascii=False)))
    return jsonify({"plan": "pro", "status": "awaiting_review", "claimed": True})


def _claim_row(r):
    return {"id": r["id"], "org_id": r["org_id"], "org": r["org_name"] if "org_name" in r.keys() else "",
            "kind": r["kind"], "ref": r["ref"] or "", "plan": r["plan"] or "",
            "amount": r["amount"] or "", "note": r["note"] or "", "status": r["status"],
            "created_at": r["created_at"], "decided_at": r["decided_at"],
            "decided_note": r["decided_note"] or ""}


@app.get("/api/admin/billing")
@login_required
def admin_billing():
    """Payments waiting on a human. Whoever runs this installation reconciles
    them against a bank statement - Seam can already import one."""
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    status = (request.args.get("status") or "pending").strip()
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    sql = ("SELECT c.*, o.name AS org_name FROM billing_claims c "
           "LEFT JOIN orgs o ON o.id = c.org_id ")
    args = ()
    if status != "all":
        sql += "WHERE c.status=? "
        args = (status,)
    sql += "ORDER BY c.id DESC LIMIT 200"
    rows = [_claim_row(r) for r in conn.execute(sql, args).fetchall()]
    ledger = [{"provider": r["provider"], "event": r["event_name"] or "",
               "org_id": r["org_id"], "result": r["result"] or "", "at": r["created_at"]}
              for r in conn.execute(
                  "SELECT * FROM billing_events ORDER BY id DESC LIMIT 50").fetchall()]
    return jsonify({"claims": rows, "ledger": ledger,
                    "provider": payment_provider(),
                    "stripe_webhook": bool(stripe_webhook_secret()),
                    "lemon_webhook": bool(lemon_secret())})


@app.post("/api/admin/billing/<int:cid>/<decision>")
@login_required
def admin_billing_decide(cid, decision):
    if decision not in ("approve", "reject"):
        raise ApiError("Невалидно действие", 400, code="invalid_action")
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    row = conn.execute("SELECT * FROM billing_claims WHERE id=?", (cid,)).fetchone()
    if not row:
        raise ApiError("Не е намерено", 404, code="not_found")
    if row["status"] != "pending":
        raise ApiError("Вече е решено", 400, code="already_decided")
    note = (body().get("note") or "").strip()[:400]
    if decision == "approve":
        if row["kind"] == "template" and (row["ref"] or "").isdigit():
            t = conn.execute("SELECT * FROM custom_templates WHERE id=?", (int(row["ref"]),)).fetchone()
            if t:
                _activate_template(conn, t["id"], t["plan"], t["price"], "invoice")
        else:
            activate_plan(conn, row["org_id"], method="invoice", ref="claim:%d" % cid)
        conn.execute("INSERT INTO notifications (user_id, kind, body, meta_json) "
                     "VALUES (?, 'plan_active', 'plan_active', '{}')", (row["user_id"],))
    else:
        # A rejected plan claim must not leave the company sitting in
        # "pending" forever, waiting for something that will not come.
        if row["kind"] != "template":
            conn.execute("UPDATE orgs SET plan='starter' WHERE id=? AND plan='pro_pending'",
                         (row["org_id"],))
    conn.execute("UPDATE billing_claims SET status=?, decided_at=datetime('now'), "
                 "decided_by=?, decided_note=? WHERE id=?",
                 ("approved" if decision == "approve" else "rejected", user["id"], note, cid))
    r2 = conn.execute("SELECT c.*, o.name AS org_name FROM billing_claims c "
                      "LEFT JOIN orgs o ON o.id=c.org_id WHERE c.id=?", (cid,)).fetchone()
    return jsonify(_claim_row(r2))


def _stripe_signature_ok(raw, header, secret, tolerance=300):
    """Stripe signs `timestamp.payload` with HMAC-SHA256 and sends it as
    `t=<unix>,v1=<hex>[,v1=<hex>]`.

    The timestamp is part of what is signed, and it is checked: without that,
    a captured webhook could be replayed forever by anyone who once saw it.
    """
    if not header or not secret:
        return False
    parts = {}
    sigs = []
    for chunk in header.split(","):
        k, _, v = chunk.strip().partition("=")
        if k == "v1":
            sigs.append(v)
        else:
            parts[k] = v
    ts = parts.get("t") or ""
    if not ts.isdigit() or not sigs:
        return False
    if abs(time.time() - int(ts)) > tolerance:
        return False
    signed = ts.encode("ascii") + b"." + raw
    want = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, s) for s in sigs)


def _org_from_stripe(conn, obj):
    """Find the company a Stripe object belongs to, without believing anything
    the browser said. Metadata first, then the ids this server recorded when it
    opened the checkout."""
    meta = obj.get("metadata") or {}
    for key in ("org", "org_id"):
        if str(meta.get(key) or "").isdigit():
            return int(meta[key])
    if str(obj.get("client_reference_id") or "").isdigit():
        return int(obj["client_reference_id"])
    for col, val in (("pay_session", obj.get("id")), ("pay_customer", obj.get("customer")),
                     ("pay_customer", obj.get("customer_id"))):
        if val:
            r = conn.execute("SELECT id FROM orgs WHERE %s=?" % col, (val,)).fetchone()
            if r:
                return r["id"]
    return None


@app.post("/api/webhooks/stripe")
def stripe_webhook():
    """The only thing that turns a card payment into a paid plan.

    Not @login_required on purpose: Stripe is the caller, and it authenticates
    with a signature rather than a session."""
    secret = stripe_webhook_secret()
    if not secret:
        # Nothing configured means this endpoint does not exist, rather than
        # existing and accepting anything.
        raise ApiError("Не е намерено", 404, code="not_found")
    if rate_limited("stwh:" + _client_ip(), 240, 60):
        raise ApiError("Твърде много заявки", 429, code="rate_limited")
    raw = request.get_data() or b""
    if not _stripe_signature_ok(raw, request.headers.get("Stripe-Signature", ""), secret):
        raise ApiError("Невалиден подпис", 401, code="webhook_signature")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError("Невалидни данни", 400, code="field_required", info={"field": "body"})

    event = payload.get("type") or ""
    event_id = str(payload.get("id") or "")
    if not event_id:
        # Every genuine Stripe event carries one, and without it there is no
        # way to tell a re-delivery from a second payment.
        raise ApiError("Невалидни данни", 400, code="field_required", info={"field": "id"})
    obj = ((payload.get("data") or {}).get("object")) or {}
    conn = db.get_db()
    try:
        org_id = _org_from_stripe(conn, obj)
        if not org_id:
            billing_log(conn, "stripe", event_id, event, None, "no company on the event")
            conn.commit()
            return jsonify({"ok": True, "ignored": "no company reference"})
        if billing_event_seen(conn, "stripe", event_id, event, org_id, "seen"):
            conn.commit()
            return jsonify({"ok": True, "ignored": "already handled"})

        result = "ignored"
        if event == "checkout.session.completed":
            if obj.get("payment_status") == "paid" or obj.get("status") == "complete":
                if obj.get("customer"):
                    conn.execute("UPDATE orgs SET pay_customer=? WHERE id=?",
                                 (obj["customer"], org_id))
                # The checkout session and the return both carry the same id, so
                # whichever lands second grants nothing extra.
                if not billing_event_seen(conn, "stripe", "cs:" + str(obj.get("id") or ""),
                                          event, org_id, "webhook"):
                    # What was bought is on the session's own metadata, which
                    # only this server ever set. A checkout for one part grants
                    # that part; anything else is a plan, as before.
                    meta = obj.get("metadata") or {}
                    feature = (meta.get("feature") or "").strip()
                    if feature in entitlements.FEATURES:
                        grant_feature(conn, org_id, feature,
                                      (meta.get("kind") or "once").strip(),
                                      str(obj.get("id") or ""), method="card")
                        result = "feature granted"
                    else:
                        plan = (meta.get("plan") or "pro").strip()
                        spec = entitlements.PLANS.get(plan)
                        if spec and spec["all"] and spec["forever"]:
                            conn.execute("UPDATE orgs SET plan=?, plan_until=NULL, "
                                         "pay_method='card' WHERE id=?", (plan, org_id))
                            billing_log(conn, "stripe", "cs:" + str(obj.get("id") or ""),
                                        "plan_activated", org_id, "%s for good" % plan)
                        else:
                            activate_plan(conn, org_id, method="card",
                                          days=(spec or {}).get("days") or PLAN_DAYS,
                                          plan=plan if spec and spec["all"] else "pro",
                                          ref=str(obj.get("id") or ""))
                        result = "activated"
                else:
                    result = "activated"
        elif event in ("invoice.paid", "invoice.payment_succeeded"):
            activate_plan(conn, org_id, method="card", ref=str(obj.get("id") or ""))
            result = "renewed"
        elif event in ("customer.subscription.deleted", "charge.refunded"):
            downgrade_plan(conn, org_id, reason=event)
            result = "ended"
        elif event == "invoice.payment_failed":
            # Not a cancellation. Stripe retries a failed charge for days and
            # sends customer.subscription.deleted only when it gives up; cutting
            # the plan on the first failure would lock out a customer whose card
            # simply needed a second attempt.
            result = "payment failed, waiting for Stripe to retry"
        conn.execute("UPDATE billing_events SET result=? WHERE provider='stripe' AND event_id=?",
                     (result, event_id))
        conn.commit()
        return jsonify({"ok": True, "result": result})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/api/webhooks/lemonsqueezy")
def lemon_webhook():
    """Auto-activation from the Merchant of Record. Lemon Squeezy signs the raw
    body with HMAC-SHA256 (X-Signature header, hex)."""
    secret = lemon_secret()
    if not secret:
        raise ApiError("Не е намерено", 404, code="not_found")
    if rate_limited("lswh:" + _client_ip(), 120, 60):
        raise ApiError("Твърде много заявки", 429, code="rate_limited")
    raw = request.get_data() or b""
    sig = request.headers.get("X-Signature", "")
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not sig or not hmac.compare_digest(digest, sig):
        raise ApiError("Невалиден подпис", 401, code="webhook_signature")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError("Невалидни данни", 400, code="field_required", info={"field": "body"})
    meta = payload.get("meta") or {}
    event = meta.get("event_name") or ""
    custom = meta.get("custom_data") or {}
    event_id = (payload.get("data") or {}).get("id") or meta.get("webhook_id") or ""

    # A plan order. `plan_checkout` puts these two values into the checkout URL,
    # and until now nothing read them back: a customer could pay for Pro
    # through the Merchant of Record and never receive it.
    org_ref = custom.get("org") or custom.get("org_id")
    if custom.get("plan") and str(org_ref or "").isdigit():
        if not event_id:
            # Without an id from the provider there is nothing to deduplicate
            # on, and a re-delivered order would buy a second month.
            return jsonify({"ok": True, "ignored": "no event id"})
        conn = db.get_db()
        try:
            org_id = int(org_ref)
            if not conn.execute("SELECT 1 FROM orgs WHERE id=?", (org_id,)).fetchone():
                return jsonify({"ok": True, "ignored": "unknown company"})
            if billing_event_seen(conn, "lemon", "%s:%s" % (event, event_id), event, org_id, "seen"):
                conn.commit()
                return jsonify({"ok": True, "ignored": "already handled"})
            result = "ignored"
            if event in ("order_created", "subscription_created", "subscription_payment_success"):
                activate_plan(conn, org_id, method="link", ref=str(event_id))
                result = "activated" if event == "order_created" else "renewed"
            elif event in ("subscription_expired", "subscription_cancelled",
                           "subscription_payment_failed", "order_refunded"):
                downgrade_plan(conn, org_id, reason=event)
                result = "ended"
            conn.execute("UPDATE billing_events SET result=? WHERE provider='lemon' AND event_id=?",
                         (result, "%s:%s" % (event, event_id)))
            conn.commit()
            return jsonify({"ok": True, "result": result})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    tpl_id = custom.get("tpl") or custom.get("template_id")
    if not tpl_id or not str(tpl_id).isdigit():
        return jsonify({"ok": True, "ignored": "no template reference"})

    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM custom_templates WHERE id=?", (int(tpl_id),)).fetchone()
        if not row:
            return jsonify({"ok": True, "ignored": "unknown template"})
        acted = None
        if event == "order_created" and row["status"] in ("draft", "pending_invoice"):
            plan = row["plan"] or "onetime"
            price = row["price"] or CUSTOM_TPL_PRICING.get(plan, "")
            _activate_template(conn, row["id"], plan, price, "link")
            acted = "activated"
        elif event == "subscription_payment_success":
            new_until = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
            conn.execute("UPDATE custom_templates SET status='active', paid_until=? WHERE id=?",
                         (new_until, row["id"]))
            acted = "renewed"
        if acted:
            meta_json = json.dumps({"label": row["label"]}, ensure_ascii=False)
            for uid in org_user_ids(conn, row["org_id"]):
                conn.execute(
                    "INSERT INTO notifications (user_id, kind, body, meta_json) VALUES (?, 'template_paid', 'template_paid', ?)",
                    (uid, meta_json))
                email_notify_user(conn, uid, row["label"])
        conn.commit()
        return jsonify({"ok": True, "result": acted or "ignored"})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  AI assistant (Seam Assist) - real Claude
# --------------------------------------------------------------------------- #
AI_CFG_FILE = os.path.join(BASE, ".ai_config")

# Supported providers. Several have a genuinely free tier so the assistant can be
# used at no cost. "openai" is the generic OpenAI-compatible path (Groq,
# OpenRouter, Together, local Ollama, ...).
AI_PROVIDERS = {
    "anthropic": {"model": "claude-sonnet-4-6", "base": ""},
    "groq":      {"model": "llama-3.3-70b-versatile", "base": "https://api.groq.com/openai/v1"},
    "gemini":    {"model": "gemini-2.0-flash", "base": ""},
    "openai":    {"model": "llama3.1", "base": "http://localhost:11434/v1"},  # default = local Ollama
}


def ai_config():
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return {"provider": "anthropic", "key": env.strip(),
                "model": os.environ.get("SEAM_AI_MODEL", "claude-sonnet-4-6"), "base_url": ""}
    if os.path.exists(AI_CFG_FILE):
        try:
            with open(AI_CFG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def ai_enabled():
    cfg = ai_config()
    if not cfg or cfg.get("provider") not in AI_PROVIDERS:
        return False
    # local Ollama needs no key; everything else does
    return bool(cfg.get("key") or cfg.get("provider") == "openai")


def build_user_context(conn, user):
    orgs = user_org_ids(conn, user["id"])
    if not orgs:
        return {"workspaces": [], "orders": [], "unread_notifications": 0}
    ph = ",".join("?" * len(orgs))
    wss = conn.execute(
        "SELECT * FROM workspaces WHERE buyer_org_id IN (%s) OR partner_org_id IN (%s) ORDER BY id DESC" % (ph, ph),
        orgs + orgs,
    ).fetchall()
    workspaces, orders = [], []
    for ws in wss:
        ws = dict(ws)
        side = "buyer" if ws["buyer_org_id"] in orgs else "partner"
        sw = serialize_workspace(conn, ws, user["id"], side)
        workspaces.append({"id": ws["id"], "name": sw["name"], "type": sw["template_label"],
                           "side": side, "counts": sw["counts"]})
        for o in conn.execute("SELECT * FROM orders WHERE workspace_id=? ORDER BY id DESC", (ws["id"],)).fetchall():
            if len(orders) >= 80:
                break
            os_ = serialize_order_summary(conn, o, ws, side)
            terms = conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=? AND value!=''", (o["id"],)).fetchall()
            orders.append({
                "ref": os_["ref"], "title": os_["title"], "status": os_["status_label"],
                "workspace": sw["name"], "awaiting_you": os_["awaiting_you"],
                "terms": {tt["key"]: {"value": dl(tt["value"]), "state": tt["state"]} for tt in terms},
                "updated": o["updated_at"],
            })
    unread = conn.execute("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND read=0", (user["id"],)).fetchone()["c"]
    return {"me": user["name"], "workspaces": workspaces, "orders": orders, "unread_notifications": unread}


def _http_json(url, payload, headers):
    hdrs = dict(headers, **{"content-type": "application/json",
                            "user-agent": "Mozilla/5.0 (compatible; SeamAssist/1.0)",
                            "accept": "application/json"})
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def ai_reply(cfg, system, messages):
    provider = cfg.get("provider")
    model = cfg.get("model") or AI_PROVIDERS.get(provider, {}).get("model")
    key = (cfg.get("key") or "").strip()

    if provider == "anthropic":
        data = _http_json("https://api.anthropic.com/v1/messages",
                          {"model": model, "max_tokens": 800, "system": system, "messages": messages},
                          {"x-api-key": key, "anthropic-version": "2023-06-01"})
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return "".join(parts).strip()

    if provider == "gemini":
        contents = [{"role": ("model" if m["role"] == "assistant" else "user"), "parts": [{"text": m["content"]}]} for m in messages]
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model, key)
        data = _http_json(url, {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                                "generationConfig": {"maxOutputTokens": 800}}, {})
        cand = (data.get("candidates") or [{}])[0]
        return "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])).strip()

    # OpenAI-compatible: Groq, OpenRouter, Together, local Ollama, ...
    base = (cfg.get("base_url") or AI_PROVIDERS.get(provider, {}).get("base") or "").rstrip("/")
    headers = {"Authorization": "Bearer " + key} if key else {}
    data = _http_json(base + "/chat/completions",
                      {"model": model, "max_tokens": 800,
                       "messages": [{"role": "system", "content": system}] + messages}, headers)
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()


@app.get("/api/assistant/status")
@login_required
def assistant_status():
    cfg = ai_config() or {}
    return jsonify({"enabled": ai_enabled(), "provider": cfg.get("provider"), "model": cfg.get("model")})


@app.post("/api/assistant/key")
@login_required
def assistant_set_key():
    d = body()
    provider = (d.get("provider") or "anthropic").strip()
    if provider not in AI_PROVIDERS:
        raise ApiError("Непознат доставчик", code="invalid_plan")
    key = (d.get("key") or "").strip()
    if not key and provider != "openai":
        raise ApiError("Липсва API ключ", code="field_required", info={"field": "key"})
    defaults = AI_PROVIDERS[provider]
    cfg = {"provider": provider, "key": key,
           "model": (d.get("model") or "").strip() or defaults["model"],
           "base_url": (d.get("base_url") or "").strip() or defaults["base"]}
    with open(AI_CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    return jsonify({"enabled": ai_enabled(), "provider": provider})


@app.post("/api/assistant")
@login_required
def assistant():
    cfg = ai_config()
    require_feature(g.conn, user_primary_org(g.conn, g.user["id"]), "assistant")
    if not ai_enabled():
        raise ApiError("AI асистентът не е настроен", 400, code="assistant_no_key")
    d = body()
    msg = require(d, "message")
    history = d.get("history") or []
    ctx = build_user_context(g.conn, g.user)
    lang = req_lang()
    system = (
        "You are Seam Assist, a concise, helpful assistant embedded in the Seam B2B operations platform "
        "(shared workspaces between a business and its partners: structured orders, shared status, agreed terms, audit trail). "
        "Always answer in the user's language (code: %s). Use the JSON snapshot of THIS user's live data to answer operational "
        "questions: where an order stands, what is awaiting their acceptance, totals, what changed, finding orders. Be brief and "
        "practical; reference orders by their ref (e.g. REQ-0001). If something is outside the snapshot, help generally and say you "
        "only see their Seam data. Do not invent orders.\n\nUSER DATA (JSON):\n%s"
        % (lang, json.dumps(ctx, ensure_ascii=False))
    )
    messages = [{"role": m.get("role"), "content": m.get("content")}
                for m in history if m.get("role") in ("user", "assistant") and m.get("content")][-6:]
    messages.append({"role": "user", "content": msg})
    try:
        reply = ai_reply(cfg, system, messages)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise ApiError("AI грешка (%s): %s" % (e.code, detail), 502, code="assistant_error")
    except Exception as e:
        raise ApiError("AI недостъпен: %s" % (str(e)[:200]), 502, code="assistant_error")
    # An empty reply is reported as a flag, not as a sentence: the wording
    # belongs to the reader's language, and only the browser knows that.
    return jsonify({"reply": reply or "", "empty": not reply})


# --------------------------------------------------------------------------- #
#  Cross-language correspondence: translate any message into the reader's
#  language, so companies from different language groups work in their own.
# --------------------------------------------------------------------------- #
LANG_NAMES = {
    "en": "English", "bg": "Bulgarian", "de": "German", "ro": "Romanian",
    "el": "Greek", "tr": "Turkish", "it": "Italian", "ru": "Russian",
    "fr": "French", "es": "Spanish", "pl": "Polish", "uk": "Ukrainian",
    "pt": "Portuguese", "nl": "Dutch",
}


@app.post("/api/translate")
@login_required
def translate():
    cfg = ai_config()
    if not ai_enabled():
        raise ApiError("Преводът не е наличен (свържете AI)", 400, code="translate_unavailable")
    d = body()
    text = (require(d, "text") or "").strip()[:4000]
    if not text:
        raise ApiError("Липсва текст", 400, code="field_required", info={"field": "text"})
    if rate_limited("tr:%d" % g.user["id"], 40, 60):
        raise ApiError("Твърде много заявки", 429, code="rate_limited")
    target = (d.get("to") or req_lang() or "en").lower()
    tname = LANG_NAMES.get(target, "English")
    system = (
        "You are a translation engine for the Seam B2B platform. Detect the source "
        "language of the user's message automatically and translate it into %s. "
        "Preserve meaning, professional tone, names, order references (e.g. REQ-0001), "
        "numbers, dates and currency exactly. If it is already in %s, output it unchanged. "
        "CRITICAL: output ONLY the translated text itself. Do NOT add any preamble, "
        "notes, language names, quotation marks, or phrases such as 'here is the translation'. "
        "Your entire response must be just the translation." % (tname, tname)
    )
    try:
        out = ai_reply(cfg, system, [{"role": "user", "content": text}])
    except urllib.error.HTTPError as e:
        raise ApiError("Грешка при превод (%s)" % e.code, 502, code="translate_error")
    except Exception as e:
        raise ApiError("Преводът е недостъпен: %s" % (str(e)[:160]), 502, code="translate_error")
    return jsonify({"text": (out or text).strip(), "to": target})


# --------------------------------------------------------------------------- #
#  Workspaces
# --------------------------------------------------------------------------- #
@app.get("/api/workspaces")
@login_required
def list_workspaces():
    conn, user = g.conn, g.user
    orgs = user_org_ids(conn, user["id"])
    if not orgs:
        return jsonify([])
    placeholders = ",".join("?" * len(orgs))
    rows = conn.execute(
        "SELECT * FROM workspaces WHERE buyer_org_id IN (%s) OR partner_org_id IN (%s) ORDER BY id DESC"
        % (placeholders, placeholders),
        orgs + orgs,
    ).fetchall()
    out = []
    for ws in rows:
        ws = dict(ws)
        side = "buyer" if ws["buyer_org_id"] in orgs else "partner"
        out.append(serialize_workspace(conn, ws, user["id"], side))
    return jsonify(out)


@app.post("/api/workspaces")
@login_required
def create_workspace():
    conn, user = g.conn, g.user
    d = body()
    name, template = require(d, "name", "template")
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", code="no_org")
    if org["kind"] != "business":
        raise ApiError("Само бизнес акаунт може да създава работно пространство (партньорите се присъединяват безплатно)", code="only_business_creates")
    require_verified(conn, user["id"])
    if template not in TEMPLATES:
        ct = conn.execute("SELECT status FROM custom_templates WHERE key=? AND org_id=?", (template, org["id"])).fetchone()
        if not ct:
            raise ApiError("Непознат шаблон", code="unknown_template")
        if ct["status"] != "active":
            raise ApiError("Шаблонът не е активиран (плащане)", 402, code="template_unpaid")
    cur = conn.execute(
        "INSERT INTO workspaces (name, template, buyer_org_id, created_by) VALUES (?,?,?,?)",
        (name, template, org["id"], user["id"]),
    )
    ws_id = cur.lastrowid
    log_event(conn, ws_id, None, user, "buyer", "workspace_created",
              "Създадено работно пространство „%s“" % name, {"name": name})

    partner_email = (d.get("partner_email") or "").strip().lower()
    if partner_email:
        _create_invite(conn, ws_id, partner_email)

    ws = dict(conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone())
    return jsonify(serialize_workspace(conn, ws, user["id"], "buyer"))


@app.get("/api/workspaces/<int:ws_id>")
@login_required
def get_workspace(ws_id):
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    return jsonify(serialize_workspace(conn, ws, user["id"], side))


def _create_invite(conn, ws_id, email):
    token = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO invites (workspace_id, email, token) VALUES (?,?,?)",
        (ws_id, email, db.token_hash(token)),
    )
    # If the invitee already has an account, drop them a notification too.
    u = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    ws = conn.execute("SELECT name, buyer_org_id FROM workspaces WHERE id=?", (ws_id,)).fetchone()
    if u:
        notify_user_row(conn, u["id"], ws_id, None, "invite",
                        "Поканени сте в „%s“ като партньор" % ws["name"], {"ws": ws["name"]})
    # Email the invitee (works even before they have an account - key for the
    # two-sided model). Language: the inviter's UI language as best guess.
    if mailer.enabled():
        borg = conn.execute("SELECT name FROM orgs WHERE id=?", (ws["buyer_org_id"],)).fetchone()
        lang = req_lang() if req_lang() in EMAIL_I18N else "en"
        L = EMAIL_I18N[lang]
        base = request.url_root.rstrip("/")
        html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b"><p>%s</p>%s</div>'
                % (html_escape(L["inv_line"].format(org=borg["name"] if borg else "Seam")),
                   _email_button(base + "/#/register", L["cta"])))
        mailer.send_async(email, L["inv_subject"], html)
    return token


@app.post("/api/workspaces/<int:ws_id>/invite")
@login_required
def invite_partner(ws_id):
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    if side != "buyer":
        raise ApiError("Само бизнес страната може да кани партньор", 403, code="only_business_invites")
    require_verified(conn, user["id"])
    if ws["partner_org_id"]:
        raise ApiError("Вече има присъединен партньор", code="partner_exists")
    email = require(body(), "email").lower()
    token = _create_invite(conn, ws_id, email)
    return jsonify({"ok": True, "email": email, "token": token})


# --------------------------------------------------------------------------- #
#  Invites
# --------------------------------------------------------------------------- #
@app.get("/api/invites")
@login_required
def my_invites():
    conn, user = g.conn, g.user
    rows = conn.execute(
        "SELECT i.id, i.token, i.workspace_id, w.name AS workspace_name, w.template "
        "FROM invites i JOIN workspaces w ON w.id=i.workspace_id "
        "WHERE i.email=? AND i.status='pending' AND w.partner_org_id IS NULL",
        (user["email"],),
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        tpl = tpl_get(conn, r["template"]) or {}
        r["template_label"] = tpl.get("label", r["template"])
        out.append(r)
    return jsonify(out)


@app.post("/api/invites/<token>/accept")
@login_required
def accept_invite(token):
    conn, user = g.conn, g.user
    inv = conn.execute("SELECT * FROM invites WHERE token=? AND status='pending'",
                       (db.token_hash(token),)).fetchone()
    if not inv:
        raise ApiError("Невалидна или вече използвана покана", 404, code="invite_invalid")
    if inv["email"].lower() != user["email"].lower():
        raise ApiError("Тази покана е за друг имейл", 403, code="invite_other_email")
    ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (inv["workspace_id"],)).fetchone()
    if ws["partner_org_id"]:
        raise ApiError("Вече има присъединен партньор", code="partner_exists")
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", code="no_org")
    conn.execute("UPDATE workspaces SET partner_org_id=? WHERE id=?", (org["id"], ws["id"]))
    conn.execute("UPDATE invites SET status='accepted' WHERE id=?", (inv["id"],))
    log_event(conn, ws["id"], None, user, "partner", "partner_joined",
              "Партньорът „%s“ се присъедини" % org["name"], {"name": org["name"]})
    notify_org(conn, ws["buyer_org_id"], ws["id"], None, "partner_joined",
               "Партньорът „%s“ прие поканата" % org["name"], meta={"name": org["name"]})
    return jsonify({"ok": True, "workspace_id": ws["id"]})


# --------------------------------------------------------------------------- #
#  Orders
# --------------------------------------------------------------------------- #
@app.get("/api/workspaces/<int:ws_id>/orders")
@login_required
def list_orders(ws_id):
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    status = request.args.get("status")
    q = (request.args.get("q") or "").strip().lower()
    rows = conn.execute(
        "SELECT * FROM orders WHERE workspace_id=? ORDER BY id DESC", (ws_id,)
    ).fetchall()
    out = []
    for o in rows:
        if status and o["status"] != status:
            continue
        item = serialize_order_summary(conn, o, ws, side)
        if q:
            hay = (item["title"] + " " + item["ref"] + " " + json.dumps(item["fields"], ensure_ascii=False)).lower()
            if q not in hay:
                continue
        out.append(item)
    return jsonify(out)


@app.post("/api/workspaces/<int:ws_id>/orders")
@login_required
def create_order(ws_id):
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    tpl = tpl_get(conn, ws["template"], ws["buyer_org_id"])
    d = body()
    title = require(d, "title")
    fields = d.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    # validate required descriptive fields
    for spec in tpl.get("fields", []):
        if spec.get("required") and not str(fields.get(spec["key"], "")).strip():
            raise ApiError("Липсва задължително поле: %s" % spec["label"], code="field_required", info={"tpl": ws["template"], "key": spec["key"]})

    count = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE workspace_id=?", (ws_id,)).fetchone()["c"]
    ref = "%s-%04d" % (tpl.get("ref_prefix", "ORD"), count + 1)
    first_stage = tpl["stages"][0]["key"]
    cur = conn.execute(
        "INSERT INTO orders (workspace_id, ref, title, template, status, fields_json, created_by) "
        "VALUES (?,?,?,?,?,?,?)",
        (ws_id, ref, title, ws["template"], first_stage, json.dumps(fields, ensure_ascii=False), user["id"]),
    )
    oid = cur.lastrowid
    # seed empty term rows so both sides see the slots to fill
    for spec in tpl.get("terms", []):
        conn.execute(
            "INSERT INTO order_terms (order_id, key, value, state) VALUES (?,?,?, 'proposed')",
            (oid, spec["key"], ""),
        )
    log_event(conn, ws_id, oid, user, side, "order_created",
              "Създаде %s „%s“ (%s)" % (tpl.get("item_noun", "поръчка"), title, ref),
              {"ref": ref, "title": title})
    notify_org(conn, other_org_id(ws, side), ws_id, oid, "order_created",
               "Нова %s: %s (%s)" % (tpl.get("item_noun", "поръчка").lower(), title, ref),
               meta={"ref": ref, "title": title})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.get("/api/orders/<int:oid>")
@login_required
def get_order(oid):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.patch("/api/orders/<int:oid>")
@login_required
def update_order(oid):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    tpl = tpl_get(conn, o["template"])
    d = body()
    fields = json.loads(o["fields_json"] or "{}")
    changes = []
    new_fields = d.get("fields")
    if isinstance(new_fields, dict):
        specs = {s["key"]: s for s in tpl.get("fields", [])}
        for k, v in new_fields.items():
            if k not in specs:
                continue
            old = fields.get(k, "")
            if str(old) != str(v):
                changes.append((k, specs[k]["label"], old, v))
                fields[k] = v
    title = d.get("title")
    title_changed = False
    if title and title.strip() and title.strip() != o["title"]:
        title_changed = True

    if not changes and not title_changed:
        return jsonify(serialize_order_detail(conn, o, ws, side))

    conn.execute(
        "UPDATE orders SET fields_json=?, title=?, updated_at=datetime('now') WHERE id=?",
        (json.dumps(fields, ensure_ascii=False), title.strip() if title_changed else o["title"], oid),
    )
    if title_changed:
        log_event(conn, ws["id"], oid, user, side, "title_changed",
                  "Преименува на „%s“" % title.strip(),
                  {"old": o["title"], "new": title.strip()})
    for key, label, old, new in changes:
        log_event(conn, ws["id"], oid, user, side, "field_updated",
                  "Промени „%s“: %s → %s" % (label, old or "-", new or "-"),
                  {"tpl": o["template"], "field": key, "old": old, "new": new})
    notify_org(conn, other_org_id(ws, side), ws["id"], oid, "field_updated",
               "Обновени данни по %s" % o["ref"], exclude_user=user["id"],
               meta={"ref": o["ref"]})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.post("/api/orders/<int:oid>/status")
@login_required
def change_status(oid):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    new_status = require(body(), "status")
    if new_status not in tpl_stage_keys(conn, o["template"]):
        raise ApiError("Невалиден етап", code="invalid_stage")
    if new_status == o["status"]:
        return jsonify(serialize_order_detail(conn, o, ws, side))
    old_label = tpl_stage_label(conn, o["template"], o["status"])
    new_label = tpl_stage_label(conn, o["template"], new_status)
    conn.execute("UPDATE orders SET status=?, updated_at=datetime('now') WHERE id=?", (new_status, oid))
    log_event(conn, ws["id"], oid, user, side, "status_changed",
              "Промени статус: %s → %s" % (old_label, new_label),
              {"tpl": o["template"], "from": o["status"], "to": new_status})
    notify_org(conn, other_org_id(ws, side), ws["id"], oid, "status_changed",
               "%s · нов статус: %s" % (o["ref"], new_label), exclude_user=user["id"],
               meta={"ref": o["ref"], "tpl": o["template"], "to": new_status})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.post("/api/orders/<int:oid>/terms")
@login_required
def propose_term(oid):
    """Propose / change an agreed term. Sets state=proposed by my side.
    The OTHER side must accept for it to become 'agreed'."""
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    d = body()
    key = require(d, "key")
    value = d.get("value", "")
    if value is None:
        value = ""
    value = str(value).strip()
    tpl = tpl_get(conn, o["template"])
    if key not in [t["key"] for t in tpl.get("terms", [])]:
        raise ApiError("Непознато условие", code="unknown_term")
    my_org = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    label = tpl_field_label(conn, o["template"], key)
    existing = conn.execute("SELECT * FROM order_terms WHERE order_id=? AND key=?", (oid, key)).fetchone()
    old_value = existing["value"] if existing else ""
    if existing:
        conn.execute(
            "UPDATE order_terms SET value=?, state='proposed', proposed_by_org=?, updated_by=?, updated_at=datetime('now') "
            "WHERE id=?",
            (value, my_org, user["id"], existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO order_terms (order_id, key, value, state, proposed_by_org, updated_by) "
            "VALUES (?,?,?, 'proposed', ?, ?)",
            (oid, key, value, my_org, user["id"]),
        )
    log_event(conn, ws["id"], oid, user, side, "term_proposed",
              "Предложи „%s“: %s" % (label, value or "-"),
              {"tpl": o["template"], "term": key, "old": old_value, "new": value})
    notify_org(conn, other_org_id(ws, side), ws["id"], oid, "term_proposed",
               "%s · ново предложение по „%s“ - изисква се приемане" % (o["ref"], label),
               exclude_user=user["id"], meta={"ref": o["ref"], "tpl": o["template"], "term": key})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.post("/api/orders/<int:oid>/terms/<key>/accept")
@login_required
def accept_term(oid, key):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    row = conn.execute("SELECT * FROM order_terms WHERE order_id=? AND key=?", (oid, key)).fetchone()
    if not row or not (row["value"] or "").strip():
        raise ApiError("Няма какво да се приеме", code="nothing_to_accept")
    if row["state"] == "agreed":
        raise ApiError("Вече е договорено", code="already_agreed")
    my_org = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    if row["proposed_by_org"] == my_org:
        raise ApiError("Не можете да приемете собственото си предложение - изчаква другата страна", 403, code="cannot_accept_own")
    label = tpl_field_label(conn, o["template"], key)
    # agreed_at is set here and nowhere else. A review cadence counts from the
    # moment the two sides settled, not from the last edit to the row, or every
    # later correction would silently postpone the next review.
    conn.execute("UPDATE order_terms SET state='agreed', updated_at=datetime('now'), "
                 "agreed_at=COALESCE(agreed_at, datetime('now')) WHERE id=?", (row["id"],))
    log_event(conn, ws["id"], oid, user, side, "term_agreed",
              "Прие „%s“: %s - договорено" % (label, row["value"]),
              {"tpl": o["template"], "term": key, "value": row["value"]})
    notify_org(conn, other_org_id(ws, side), ws["id"], oid, "term_agreed",
               "%s · „%s“ е договорено" % (o["ref"], label), exclude_user=user["id"],
               meta={"ref": o["ref"], "tpl": o["template"], "term": key})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.post("/api/orders/<int:oid>/comments")
@login_required
def add_comment(oid):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    text = require(body(), "body")
    conn.execute("INSERT INTO comments (order_id, user_id, body) VALUES (?,?,?)", (oid, user["id"], text))
    notify_org(conn, other_org_id(ws, side), ws["id"], oid, "comment",
               "%s · нов коментар от %s" % (o["ref"], user["name"]), exclude_user=user["id"],
               meta={"ref": o["ref"], "by": user["name"]})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


# --------------------------------------------------------------------------- #
#  Progress evidence: photo/video attachments shared between the two sides
# --------------------------------------------------------------------------- #
UPLOAD_DIR = os.path.join(BASE, "uploads")
# No SVG. It is a document that can carry script, and it is served back from
# our own origin to everyone in the workspace - the same reason a profile
# picture cannot be one.
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif"}
VIDEO_EXT = {"mp4", "webm", "mov", "m4v", "mkv", "avi"}


def _media_kind(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in IMAGE_EXT:
        return "image", ext
    if ext in VIDEO_EXT:
        return "video", ext
    return None, ext


@app.post("/api/orders/<int:oid>/attachments")
@login_required
def add_attachments(oid):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    files = request.files.getlist("files")
    if not files:
        raise ApiError("Липсва файл", 400, code="field_required", info={"field": "files"})
    dirp = os.path.join(UPLOAD_DIR, str(oid))
    os.makedirs(dirp, exist_ok=True)
    names = []
    for f in files[:10]:
        orig = (f.filename or "file")[:180]
        kind, ext = _media_kind(orig)
        if not kind:
            raise ApiError("Неподдържан формат (само снимки и видео)", 400, code="att_type")
        fname = secrets.token_hex(10) + "." + ext
        path = os.path.join(dirp, fname)
        f.save(path)
        size = os.path.getsize(path)
        conn.execute(
            "INSERT INTO attachments (order_id, user_id, filename, original_name, kind, size) VALUES (?,?,?,?,?,?)",
            (oid, user["id"], fname, orig, kind, size),
        )
        names.append(orig)
    log_event(conn, ws["id"], oid, user, side, "attachment_added",
              "Прикачи материали: %s" % ", ".join(names),
              {"name": ", ".join(names[:3]) + ("…" if len(names) > 3 else ""), "count": len(names)})
    notify_org(conn, other_org_id(ws, side), ws["id"], oid, "attachment_added",
               "%s · нови материали от изпълнението" % o["ref"], exclude_user=user["id"],
               meta={"ref": o["ref"]})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.delete("/api/attachments/<int:aid>")
@login_required
def delete_attachment(aid):
    conn, user = g.conn, g.user
    a = conn.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    if not a:
        raise ApiError("Не е намерено", 404, code="not_found")
    o = conn.execute("SELECT * FROM orders WHERE id=?", (a["order_id"],)).fetchone()
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    if a["user_id"] != user["id"]:
        raise ApiError("Само качилият може да премахне файла", 403, code="no_access")
    try:
        os.remove(os.path.join(UPLOAD_DIR, str(a["order_id"]), a["filename"]))
    except OSError:
        pass
    conn.execute("DELETE FROM attachments WHERE id=?", (aid,))
    log_event(conn, ws["id"], o["id"], user, side, "attachment_removed",
              "Премахна файл: %s" % a["original_name"], {"name": a["original_name"]})
    o = conn.execute("SELECT * FROM orders WHERE id=?", (o["id"],)).fetchone()
    return jsonify(serialize_order_detail(conn, o, ws, side))


@app.get("/uploads/<int:oid>/<path:fname>")
def serve_upload(oid, fname):
    """Media is private to the workspace: both sides can view, nobody else."""
    conn = db.get_db()
    try:
        uid = session.get("uid")
        if not uid:
            return "", 401
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return "", 404
        workspace_access(conn, o["workspace_id"], uid)  # raises 403 if outside
        row = conn.execute("SELECT 1 FROM attachments WHERE order_id=? AND filename=?", (oid, fname)).fetchone()
        if not row:
            return "", 404
        resp = make_response(send_from_directory(os.path.join(UPLOAD_DIR, str(oid)), fname))
        # Belt and braces behind the extension check: never let the browser
        # guess a type, and give the file no privileges of its own.
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return resp
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Activity (workspace-level audit) + live feed
# --------------------------------------------------------------------------- #
@app.get("/api/workspaces/<int:ws_id>/activity")
@login_required
def workspace_activity(ws_id):
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    since = request.args.get("since", type=int)
    params = [ws_id]
    sql = ("SELECT e.*, u.name AS actor_name, o.ref AS order_ref, o.title AS order_title "
           "FROM events e LEFT JOIN users u ON u.id=e.actor_user_id "
           "LEFT JOIN orders o ON o.id=e.order_id WHERE e.workspace_id=?")
    if since:
        sql += " AND e.id > ?"
        params.append(since)
    sql += " ORDER BY e.id DESC LIMIT 100"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta_json"] = dl_meta(d.get("meta_json"))
        if d.get("order_title"):
            d["order_title"] = dl(d["order_title"])
        out.append(d)
    return jsonify(out)


@app.get("/api/orders/<int:oid>/feed")
@login_required
def order_feed(oid):
    """Lightweight polling endpoint for near-real-time updates on one order."""
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    last_event = conn.execute("SELECT MAX(id) AS m FROM events WHERE order_id=?", (oid,)).fetchone()["m"] or 0
    last_comment = conn.execute("SELECT MAX(id) AS m FROM comments WHERE order_id=?", (oid,)).fetchone()["m"] or 0
    return jsonify({"last_event": last_event, "last_comment": last_comment,
                    "status": o["status"], "updated_at": o["updated_at"]})


# --------------------------------------------------------------------------- #
#  Notifications
# --------------------------------------------------------------------------- #
@app.get("/api/notifications")
@login_required
def list_notifications():
    conn, user = g.conn, g.user
    rows = conn.execute(
        "SELECT n.*, o.ref AS order_ref FROM notifications n LEFT JOIN orders o ON o.id=n.order_id "
        "WHERE n.user_id=? ORDER BY n.id DESC LIMIT 50",
        (user["id"],),
    ).fetchall()
    unread = conn.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id=? AND read=0", (user["id"],)
    ).fetchone()["c"]
    lang = (user["lang"] if "lang" in user.keys() and user["lang"] in NOTIF_I18N
            else req_lang())
    items = []
    for r in rows:
        d = dict(r)
        d["meta_json"] = dl_meta(d.get("meta_json"))
        # For the service worker, which has no bundle to translate with.
        d["text"] = notif_text(conn, r, lang)
        items.append(d)
    return jsonify({"items": items, "unread": unread})


@app.post("/api/notifications/read")
@login_required
def mark_read():
    conn, user = g.conn, g.user
    d = body()
    ids = d.get("ids")
    if isinstance(ids, list) and ids:
        ph = ",".join("?" * len(ids))
        conn.execute("UPDATE notifications SET read=1 WHERE user_id=? AND id IN (%s)" % ph, [user["id"]] + ids)
    else:
        conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (user["id"],))
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  E-commerce fulfillment (physical stock + digital key vault)
# --------------------------------------------------------------------------- #
def store_org(conn, user):
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", code="no_org")
    return org


def product_available(conn, p):
    if p["type"] == "physical":
        return p["stock"]
    return conn.execute("SELECT COUNT(*) c FROM product_keys WHERE product_id=? AND status='available'", (p["id"],)).fetchone()["c"]


def serialize_product(conn, p):
    d = {"id": p["id"], "name": dl(p["name"]), "type": p["type"], "sku": p["sku"] or "",
         "price": p["price"] or "", "stock": p["stock"], "available": product_available(conn, p),
         "image": (p["image"] if "image" in p.keys() else None) or ""}
    if p["type"] == "digital":
        d["keys_total"] = conn.execute("SELECT COUNT(*) c FROM product_keys WHERE product_id=?", (p["id"],)).fetchone()["c"]
    return d


def _price_eur(raw):
    m = re.match(r"^\s*([A-Z]{3})?\s*([0-9]+(?:[.,][0-9]+)?)", str(raw or ""))
    if not m:
        return 0.0
    try:
        return float(m.group(2).replace(",", "."))
    except ValueError:
        return 0.0


def serialize_store_order(conn, o):
    p = conn.execute("SELECT * FROM products WHERE id=?", (o["product_id"],)).fetchone()
    keys = [r["code"] for r in conn.execute("SELECT code FROM product_keys WHERE order_id=?", (o["id"],)).fetchall()]
    return {"id": o["id"], "ref": o["ref"], "customer": dl(o["customer"] or ""), "qty": o["qty"],
            "status": o["status"], "note": o["note"] or "", "created_at": o["created_at"],
            "fulfilled_at": o["fulfilled_at"], "keys": keys, "source": o["source"],
            "product": {"id": p["id"], "name": dl(p["name"]), "type": p["type"], "price": p["price"] or ""} if p else None}


@app.get("/api/store/summary")
@login_required
def store_summary():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    incoming = conn.execute("SELECT COUNT(*) c FROM store_orders WHERE org_id=? AND status='new'", (org["id"],)).fetchone()["c"]
    completed = conn.execute("SELECT COUNT(*) c FROM store_orders WHERE org_id=? AND status='fulfilled'", (org["id"],)).fetchone()["c"]
    prods = conn.execute("SELECT * FROM products WHERE org_id=?", (org["id"],)).fetchall()
    low = []
    for p in prods:
        av = product_available(conn, p)
        if av <= 3:
            low.append({"name": dl(p["name"]), "type": p["type"], "available": av})
    # Shopify-style revenue: fulfilled orders valued at their product price.
    revenue = 0.0
    for o in conn.execute("SELECT qty, product_id FROM store_orders WHERE org_id=? AND status='fulfilled'", (org["id"],)).fetchall():
        pr = conn.execute("SELECT price FROM products WHERE id=?", (o["product_id"],)).fetchone()
        if pr:
            revenue += _price_eur(pr["price"]) * (o["qty"] or 1)
    total_orders = conn.execute("SELECT COUNT(*) c FROM store_orders WHERE org_id=?", (org["id"],)).fetchone()["c"]
    rate = int(round(100.0 * completed / total_orders)) if total_orders else 0
    return jsonify({"incoming": incoming, "completed": completed, "products": len(prods), "low": low,
                    "revenue": "EUR %.2f" % revenue, "orders_total": total_orders, "fulfil_rate": rate})


@app.get("/api/store/products")
@login_required
def store_products():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    rows = conn.execute("SELECT * FROM products WHERE org_id=? ORDER BY id DESC", (org["id"],)).fetchall()
    return jsonify([serialize_product(conn, p) for p in rows])


@app.post("/api/store/products")
@login_required
def store_add_product():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    d = body()
    name = require(d, "name")
    ptype = d.get("type") if d.get("type") in ("physical", "digital") else "physical"
    stock = int(d.get("stock") or 0) if ptype == "physical" else 0
    image = (d.get("image") or "").strip()[:2000]
    if image and not re.match(r"^(https?://|data:image/)", image):
        image = ""
    cur = conn.execute(
        "INSERT INTO products (org_id, name, type, sku, price, stock, image) VALUES (?,?,?,?,?,?,?)",
        (org["id"], name, ptype, (d.get("sku") or "").strip(), (d.get("price") or "").strip(), stock, image),
    )
    p = conn.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(serialize_product(conn, p))


@app.post("/api/store/products/<int:pid>/keys")
@login_required
def store_add_keys(pid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    p = conn.execute("SELECT * FROM products WHERE id=? AND org_id=?", (pid, org["id"])).fetchone()
    if not p:
        raise ApiError("Продуктът не е намерен", 404, code="not_found")
    if p["type"] != "digital":
        raise ApiError("Само дигитални продукти имат ключове", code="not_digital")
    codes = [c.strip() for c in (body().get("codes") or "").splitlines() if c.strip()]
    for c in codes:
        conn.execute("INSERT INTO product_keys (product_id, code) VALUES (?,?)", (pid, c))
    p = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    return jsonify({"added": len(codes), "product": serialize_product(conn, p)})


@app.delete("/api/store/products/<int:pid>")
@login_required
def store_del_product(pid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    p = conn.execute("SELECT id FROM products WHERE id=? AND org_id=?", (pid, org["id"])).fetchone()
    if not p:
        raise ApiError("Продуктът не е намерен", 404, code="not_found")
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    return jsonify({"ok": True})


@app.get("/api/store/orders.csv")
@login_required
def store_orders_csv():
    """Native export: download all store orders as CSV for accounting / ERP /
    couriers - the universal format every daily-ops tool imports."""
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    rows = conn.execute("SELECT * FROM store_orders WHERE org_id=? ORDER BY id", (org["id"],)).fetchall()
    out = ["ref,date,status,source,customer,product,sku,qty,price,keys"]

    def esc_csv(v):
        s = str(v if v is not None else "")
        if any(ch in s for ch in [",", '"', "\n"]):
            s = '"' + s.replace('"', '""') + '"'
        return s

    for o in rows:
        p = conn.execute("SELECT name, sku, price FROM products WHERE id=?", (o["product_id"],)).fetchone()
        keys = ";".join(r["code"] for r in conn.execute("SELECT code FROM product_keys WHERE order_id=?", (o["id"],)).fetchall())
        out.append(",".join(esc_csv(x) for x in [
            o["ref"], o["created_at"], o["status"], o["source"], dl(o["customer"] or ""),
            dl(p["name"]) if p else "", (p["sku"] if p else "") or "", o["qty"],
            (p["price"] if p else "") or "", keys]))
    csv_data = "﻿" + "\n".join(out)  # BOM so Excel opens UTF-8 correctly
    return app.response_class(csv_data, mimetype="text/csv", headers={
        "Content-Disposition": "attachment; filename=seam-orders.csv"})


# Native courier connector: printable shipping label with a scannable Code 39
# barcode of the order reference - no external service required.
_CODE39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
    "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "$": "nwnwnwnnn",
    "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn", "*": "nwnnwnwnn",
}


def _code39_svg(data, h=52):
    data = "*" + re.sub(r"[^0-9A-Z\-. $/+%]", "", str(data).upper()) + "*"
    n, w, gap = 2, 5, 2  # narrow, wide, inter-char gap (px)
    x, bars = 0, []
    for ch in data:
        pat = _CODE39.get(ch)
        if not pat:
            continue
        for i, el in enumerate(pat):
            width = w if el == "w" else n
            if i % 2 == 0:
                bars.append('<rect x="%d" y="0" width="%d" height="%d"/>' % (x, width, h))
            x += width
        x += gap
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" fill="#111">%s</svg>'
            % (x, h, "".join(bars)))


LABEL_I18N = {
    "en": {"t": "Shipping label", "from": "Sender", "to": "Recipient", "order": "Order", "item": "Item", "qty": "Qty", "courier": "Courier", "weight": "Weight", "cod": "Cash on delivery", "note": "Note"},
    "es": {"t": "Etiqueta de envío", "from": "Remitente", "to": "Destinatario", "order": "Pedido", "item": "Artículo", "qty": "Cant.", "courier": "Mensajería", "weight": "Peso", "cod": "Contra reembolso", "note": "Nota"},
    "fr": {"t": "Étiquette d'expédition", "from": "Expéditeur", "to": "Destinataire", "order": "Commande", "item": "Article", "qty": "Qté", "courier": "Transporteur", "weight": "Poids", "cod": "Contre-remboursement", "note": "Note"},
    "pl": {"t": "Etykieta wysyłkowa", "from": "Nadawca", "to": "Odbiorca", "order": "Zamówienie", "item": "Artykuł", "qty": "Ilość", "courier": "Kurier", "weight": "Waga", "cod": "Pobranie", "note": "Uwaga"},
    "uk": {"t": "Транспортна етикетка", "from": "Відправник", "to": "Отримувач", "order": "Замовлення", "item": "Товар", "qty": "К-сть", "courier": "Кур'єр", "weight": "Вага", "cod": "Накладений платіж", "note": "Нотатка"},
    "pt": {"t": "Etiqueta de expedição", "from": "Remetente", "to": "Destinatário", "order": "Encomenda", "item": "Artigo", "qty": "Qtd.", "courier": "Transportadora", "weight": "Peso", "cod": "Pagamento à cobrança", "note": "Nota"},
    "bg": {"t": "Товарителен етикет", "from": "Изпращач", "to": "Получател", "order": "Поръчка", "item": "Артикул", "qty": "Бр.", "courier": "Куриер", "weight": "Тегло", "cod": "Наложен платеж", "note": "Бележка"},
    "de": {"t": "Versandetikett", "from": "Absender", "to": "Empfänger", "order": "Auftrag", "item": "Artikel", "qty": "Menge", "courier": "Kurier", "weight": "Gewicht", "cod": "Nachnahme", "note": "Notiz"},
    "ro": {"t": "Etichetă de expediere", "from": "Expeditor", "to": "Destinatar", "order": "Comandă", "item": "Articol", "qty": "Cant.", "courier": "Curier", "weight": "Greutate", "cod": "Ramburs", "note": "Notă"},
    "el": {"t": "Ετικέτα αποστολής", "from": "Αποστολέας", "to": "Παραλήπτης", "order": "Παραγγελία", "item": "Είδος", "qty": "Ποσ.", "courier": "Κούριερ", "weight": "Βάρος", "cod": "Αντικαταβολή", "note": "Σημείωση"},
    "tr": {"t": "Gönderi etiketi", "from": "Gönderen", "to": "Alıcı", "order": "Sipariş", "item": "Ürün", "qty": "Adet", "courier": "Kurye", "weight": "Ağırlık", "cod": "Kapıda ödeme", "note": "Not"},
    "it": {"t": "Etichetta di spedizione", "from": "Mittente", "to": "Destinatario", "order": "Ordine", "item": "Articolo", "qty": "Q.tà", "courier": "Corriere", "weight": "Peso", "cod": "Contrassegno", "note": "Nota"},
    "ru": {"t": "Транспортная этикетка", "from": "Отправитель", "to": "Получатель", "order": "Заказ", "item": "Товар", "qty": "Кол.", "courier": "Курьер", "weight": "Вес", "cod": "Наложенный платёж", "note": "Заметка"},
}


@app.get("/label/<int:oid>")
def shipping_label(oid):
    conn = db.get_db()
    try:
        uid = session.get("uid")
        if not uid:
            return "", 401
        org = user_primary_org(conn, uid)
        o = conn.execute("SELECT * FROM store_orders WHERE id=? AND org_id=?", (oid, org["id"] if org else 0)).fetchone()
        if not o:
            return "", 404
        lang = _doc_lang(LABEL_I18N)
        L = LABEL_I18N[lang]
        p = conn.execute("SELECT name FROM products WHERE id=?", (o["product_id"],)).fetchone()
        q = request.args
        courier = (q.get("courier") or "").strip()[:40]
        weight = (q.get("weight") or "").strip()[:20]
        cod = (q.get("cod") or "").strip()[:30]
        sender = html_escape(org["name"]) + (
            (" · %s %s" % (company.country_meta(org["country"])["id_label"], html_escape(org["reg_number"])))
            if org["reg_number"] else "")
        line = lambda k, v: ('<tr><td class="k">%s</td><td>%s</td></tr>' % (L[k], v)) if v else ""
        page = ("""<!DOCTYPE html><html lang="%(lang)s"><head><meta charset="utf-8"><meta name="color-scheme" content="light">
<title>%(t)s %(ref)s</title><style>
html{background:#fff}body{font-family:'Segoe UI',Arial,sans-serif;color:#111;background:#fff;margin:0;padding:18px}
.lbl{max-width:420px;margin:0 auto;border:2px solid #111;border-radius:8px;padding:18px 20px}
.hd{display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #111;padding-bottom:8px;margin-bottom:10px}
.hd .b{font-weight:800;font-size:16px}.hd .c{font-weight:700}
.blk{margin:10px 0}.blk .l{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#555}
.blk .v{font-size:15px;font-weight:600}
table{width:100%%;border-collapse:collapse;margin-top:8px}td{padding:4px 2px;font-size:13px;vertical-align:top}
td.k{color:#555;width:120px}
.bc{text-align:center;margin-top:14px;border-top:2px dashed #111;padding-top:12px}
.bc svg{max-width:100%%}.bc .ref{font-family:ui-monospace,monospace;font-weight:700;letter-spacing:2px;margin-top:4px}
@media print{body{padding:0}.lbl{border-width:1px}}
</style></head><body><div class="lbl">
<div class="hd"><span class="b">Seam</span><span class="c">%(courier)s</span></div>
<div class="blk"><div class="l">%(from)s</div><div class="v">%(sender)s</div></div>
<div class="blk"><div class="l">%(to)s</div><div class="v">%(recipient)s</div></div>
<table>
%(orderline)s%(itemline)s%(qtyline)s%(weightline)s%(codline)s%(noteline)s
</table>
<div class="bc">%(barcode)s<div class="ref">%(ref)s</div></div>
</div></body></html>""") % {
            "lang": lang, "t": L["t"], "ref": html_escape(o["ref"]),
            "courier": html_escape(courier), "from": L["from"], "sender": sender,
            "to": L["to"], "recipient": html_escape(dl(o["customer"] or "-")),
            "orderline": line("order", html_escape(o["ref"])),
            "itemline": line("item", html_escape(dl(p["name"])) if p else ""),
            "qtyline": line("qty", str(o["qty"])),
            "weightline": line("weight", html_escape(weight)),
            "codline": line("cod", html_escape(cod)),
            "noteline": line("note", html_escape(o["note"] or "")),
            "barcode": _code39_svg(o["ref"]),
        }
        return page
    finally:
        conn.close()


@app.get("/api/store/orders")
@login_required
def store_list_orders():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    status = request.args.get("status")
    rows = conn.execute("SELECT * FROM store_orders WHERE org_id=? ORDER BY id DESC", (org["id"],)).fetchall()
    out = [serialize_store_order(conn, o) for o in rows if not status or o["status"] == status]
    return jsonify(out)


def _safe_qty(v):
    try:
        q = int(v or 1)
    except (TypeError, ValueError):
        q = 1
    return max(1, min(999, q))


def _new_store_order(conn, org_id, product, qty, customer, note, source, notify_users=False):
    count = conn.execute("SELECT COUNT(*) c FROM store_orders WHERE org_id=?", (org_id,)).fetchone()["c"]
    ref = "SO-%04d" % (count + 1)
    cur = conn.execute(
        "INSERT INTO store_orders (org_id, ref, customer, product_id, qty, note, source) VALUES (?,?,?,?,?,?,?)",
        (org_id, ref, (customer or "").strip()[:120], product["id"], qty, (note or "").strip()[:500], source),
    )
    if notify_users:
        meta = json.dumps({"ref": ref, "product": product["name"]}, ensure_ascii=False)
        for uid in org_user_ids(conn, org_id):
            conn.execute("INSERT INTO notifications (user_id, kind, body, meta_json) VALUES (?, 'store_incoming', 'store_incoming', ?)",
                         (uid, meta))
            email_notify_user(conn, uid, ref)
        dispatch_webhooks(conn, org_id, "store.incoming", {
            "ref": ref, "product": product["name"], "qty": qty,
            "customer": (customer or "").strip(), "source": source})
    return conn.execute("SELECT * FROM store_orders WHERE id=?", (cur.lastrowid,)).fetchone()


@app.post("/api/store/orders")
@login_required
def store_create_order():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    d = body()
    pid = require(d, "product_id")
    p = conn.execute("SELECT * FROM products WHERE id=? AND org_id=?", (pid, org["id"])).fetchone()
    if not p:
        raise ApiError("Продуктът не е намерен", 404, code="not_found")
    o = _new_store_order(conn, org["id"], p, _safe_qty(d.get("qty")), d.get("customer"), d.get("note"), "manual")
    return jsonify(serialize_store_order(conn, o))


@app.post("/api/store/orders/<int:oid>/fulfill")
@login_required
def store_fulfill(oid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    o = conn.execute("SELECT * FROM store_orders WHERE id=? AND org_id=?", (oid, org["id"])).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    if o["status"] == "fulfilled":
        raise ApiError("Вече е изпълнена", code="store_already")
    p = conn.execute("SELECT * FROM products WHERE id=?", (o["product_id"],)).fetchone()
    if not p:
        raise ApiError("Продуктът не е намерен", 404, code="not_found")
    qty = o["qty"]
    if p["type"] == "physical":
        if p["stock"] < qty:
            raise ApiError("Недостатъчна наличност", code="out_of_stock")
        conn.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, p["id"]))
    else:
        avail = conn.execute("SELECT id FROM product_keys WHERE product_id=? AND status='available' ORDER BY id LIMIT ?", (p["id"], qty)).fetchall()
        if len(avail) < qty:
            raise ApiError("Няма достатъчно ключове", code="out_of_keys")
        for r in avail:
            conn.execute("UPDATE product_keys SET status='delivered', order_id=? WHERE id=?", (oid, r["id"]))
    conn.execute("UPDATE store_orders SET status='fulfilled', fulfilled_at=datetime('now') WHERE id=?", (oid,))
    o = conn.execute("SELECT * FROM store_orders WHERE id=?", (oid,)).fetchone()
    return jsonify(serialize_store_order(conn, o))


# --------------------------------------------------------------------------- #
#  Productivity: everything awaiting MY approval, with age in days
#  ("a PO should never sit open without acknowledgment" - supplier-portal
#   best practice)
# --------------------------------------------------------------------------- #
@app.get("/api/pending")
@login_required
def pending_approvals():
    conn, user = g.conn, g.user
    orgs = set(user_org_ids(conn, user["id"]))
    if not orgs:
        return jsonify([])
    rows = conn.execute(
        "SELECT o.id, o.ref, o.title, w.id AS wsid, w.name AS wsname, w.buyer_org_id, w.partner_org_id, "
        "MIN(t.updated_at) AS oldest, COUNT(*) AS cnt "
        "FROM order_terms t JOIN orders o ON o.id=t.order_id JOIN workspaces w ON w.id=o.workspace_id "
        "WHERE t.state='proposed' AND t.value!='' AND t.proposed_by_org IS NOT NULL "
        "GROUP BY o.id ORDER BY oldest ASC"
    ).fetchall()
    out = []
    now = datetime.utcnow()
    for r in rows:
        my_org = r["buyer_org_id"] if r["buyer_org_id"] in orgs else (r["partner_org_id"] if r["partner_org_id"] in orgs else None)
        if my_org is None:
            continue
        pend = conn.execute(
            "SELECT COUNT(*) c FROM order_terms WHERE order_id=? AND state='proposed' AND value!='' "
            "AND proposed_by_org IS NOT NULL AND proposed_by_org!=?",
            (r["id"], my_org),
        ).fetchone()["c"]
        if not pend:
            continue
        days = 0
        try:
            days = max(0, (now - datetime.strptime(r["oldest"], "%Y-%m-%d %H:%M:%S")).days)
        except Exception:
            pass
        out.append({"id": r["id"], "ref": r["ref"], "title": dl(r["title"]),
                    "workspace": dl(r["wsname"]), "wsid": r["wsid"], "terms": pend, "days": days})
    return jsonify(out)


# --------------------------------------------------------------------------- #
#  Analytics (roadmap item 11)
#
#  Every number here is derived from records that already exist - agreed terms,
#  the audit trail, store orders - rather than from a separate reporting store.
#  That is the point: the audit trail is what makes the figures defensible.
# --------------------------------------------------------------------------- #
ANALYTICS_RANGES = (30, 90, 180, 365)


def _parse_ts(raw):
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _month_key(dt):
    return dt.strftime("%Y-%m")


def _months_back(n):
    """The last n calendar months, oldest first, as YYYY-MM."""
    out, cur = [], datetime.utcnow().replace(day=1)
    for _ in range(n):
        out.append(_month_key(cur))
        cur = (cur - timedelta(days=1)).replace(day=1)
    return list(reversed(out))


def _order_close_time(conn, oid, terminal):
    """When the order actually reached its final stage, taken from the audit
    trail rather than from updated_at, which later edits would move."""
    row = conn.execute(
        "SELECT created_at, meta_json FROM events WHERE order_id=? AND kind='status_changed' "
        "ORDER BY id DESC", (oid,)).fetchall()
    for e in row:
        try:
            if (json.loads(e["meta_json"] or "{}")).get("to") == terminal:
                return _parse_ts(e["created_at"])
        except Exception:
            continue
    return None


def _stage_history(conn, order_ids):
    """When each record first reached each stage.

    Read from the event log rather than from a column, because a record only
    carries its *current* status. "First reached", not last: a job pushed back
    into progress and certified a second time took as long as it took the
    first time, and taking the later timestamp would quietly flatter the
    figure every time something went wrong.
    """
    if not order_ids:
        return {}
    out = {}
    ph = ",".join("?" * len(order_ids))
    rows = conn.execute(
        "SELECT order_id, meta_json, created_at FROM events "
        "WHERE kind='status_changed' AND order_id IN (%s) ORDER BY id" % ph,
        list(order_ids)).fetchall()
    for r in rows:
        try:
            to = (json.loads(r["meta_json"] or "{}") or {}).get("to")
        except (TypeError, ValueError):
            continue
        when = parse_dt(r["created_at"])
        if not to or not when:
            continue
        out.setdefault(r["order_id"], {}).setdefault(to, when)
    return out


def parse_dt(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


@app.get("/api/analytics/metrics")
@login_required
def analytics_metrics():
    """The figures this company's add-ons make measurable.

    Separate from /api/analytics on purpose: these depend on which add-ons are
    switched on, and a company with none should see the rest of the screen
    rather than an error. Grouped by template, because a return rate and a
    certification cycle are not comparable and should not sit in one row.
    """
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    try:
        days = int(request.args.get("days") or 90)
    except ValueError:
        days = 90
    if days not in ANALYTICS_RANGES:
        days = 90
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    on = org_addons(conn, org["id"])
    if not any(on.values()):
        return jsonify({"range": {"days": days}, "groups": [], "none_on": True,
                        "min_sample": addons.MIN_SAMPLE})

    my = set(user_org_ids(conn, user["id"]))
    ph = ",".join("?" * len(my))
    ws_rows = conn.execute(
        "SELECT id, template FROM workspaces WHERE buyer_org_id IN (%s) OR partner_org_id IN (%s)"
        % (ph, ph), list(my) + list(my)).fetchall()
    if not ws_rows:
        return jsonify({"range": {"days": days}, "groups": [], "empty": True,
                        "min_sample": addons.MIN_SAMPLE})

    wph = ",".join("?" * len(ws_rows))
    orders = conn.execute(
        "SELECT * FROM orders WHERE workspace_id IN (%s) AND created_at >= ?" % wph,
        [w["id"] for w in ws_rows] + [since]).fetchall()
    history = _stage_history(conn, [o["id"] for o in orders])

    terms_by_order = {}
    if orders:
        oph = ",".join("?" * len(orders))
        for t in conn.execute(
                "SELECT order_id, key, value FROM order_terms "
                "WHERE order_id IN (%s) AND state='agreed'" % oph,
                [o["id"] for o in orders]).fetchall():
            terms_by_order.setdefault(t["order_id"], {})[t["key"]] = t["value"]

    by_template = {}
    for o in orders:
        try:
            fields = json.loads(o["fields_json"] or "{}")
        except (TypeError, ValueError):
            fields = {}
        reached = dict(history.get(o["id"], {}))
        # A record starts at the template's first stage the moment it is
        # opened; nothing logs that as a change, and without it every span
        # measured from the first stage would be empty.
        first = (tpl_get(conn, o["template"], org["id"]) or {}).get("stages") or []
        opened = parse_dt(o["created_at"])
        if first and opened:
            reached.setdefault(first[0]["key"], opened)
        by_template.setdefault(o["template"], []).append(
            {"fields": fields, "terms": terms_by_order.get(o["id"], {}), "reached": reached})

    groups = []
    for tkey, enabled in on.items():
        if not enabled or tkey not in TEMPLATES:
            continue
        figures = addons.report(tkey, enabled, by_template.get(tkey, []))
        if figures:
            groups.append({"template": tkey, "label": TEMPLATES[tkey]["label"],
                           "records": len(by_template.get(tkey, [])), "metrics": figures})
    return jsonify({"range": {"days": days}, "groups": groups,
                    "min_sample": addons.MIN_SAMPLE})


@app.get("/api/analytics")
@login_required
def analytics():
    conn, user = g.conn, g.user
    my_orgs = set(user_org_ids(conn, user["id"]))
    if not my_orgs:
        raise ApiError("Нямате организация", 400, code="no_org")
    try:
        days = int(request.args.get("days") or 90)
    except ValueError:
        days = 90
    if days not in ANALYTICS_RANGES:
        days = 90
    since = datetime.utcnow() - timedelta(days=days)
    months = _months_back(min(12, max(3, days // 30)))

    ws_rows = conn.execute(
        "SELECT * FROM workspaces WHERE buyer_org_id IN (%s) OR partner_org_id IN (%s)"
        % (",".join("?" * len(my_orgs)), ",".join("?" * len(my_orgs))),
        list(my_orgs) + list(my_orgs)).fetchall()
    ws_by_id = {w["id"]: w for w in ws_rows}
    if not ws_by_id:
        return jsonify({"range": {"days": days}, "empty": True})
    ph = ",".join("?" * len(ws_by_id))
    orders = conn.execute(
        "SELECT * FROM orders WHERE workspace_id IN (%s) ORDER BY id DESC" % ph,
        list(ws_by_id)).fetchall()

    value_total = value_open = 0.0
    created_by_month = dict((m, 0) for m in months)
    closed_by_month = dict((m, 0) for m in months)
    value_by_month = dict((m, 0.0) for m in months)
    by_status, by_template, partners = {}, {}, {}
    cycle_days, approval_days = [], []
    open_orders = closed_orders = 0
    overdue = []

    now = datetime.utcnow()
    for o in orders:
        stages = tpl_stage_keys(conn, o["template"]) or []
        terminal = stages[-1] if stages else None
        spec = {s["key"]: s for s in tpl_get(conn, o["template"]).get("terms", [])}
        is_closed = bool(terminal) and o["status"] == terminal

        o_value = 0.0
        for t_ in conn.execute("SELECT key,value,state,updated_at FROM order_terms "
                               "WHERE order_id=? AND value!=''", (o["id"],)).fetchall():
            if t_["state"] == "agreed" and (spec.get(t_["key"]) or {}).get("type") == "money":
                o_value += _price_eur(t_["value"])
        value_total += o_value
        if not is_closed:
            value_open += o_value

        created = _parse_ts(o["created_at"])
        if created:
            mk = _month_key(created)
            if mk in created_by_month:
                created_by_month[mk] += 1
                value_by_month[mk] += o_value
        if is_closed:
            closed_orders += 1
            closed_at = _order_close_time(conn, o["id"], terminal)
            if closed_at:
                mk = _month_key(closed_at)
                if mk in closed_by_month:
                    closed_by_month[mk] += 1
                if created and closed_at >= created:
                    cycle_days.append((closed_at - created).total_seconds() / 86400.0)
        else:
            open_orders += 1
            # An order sitting in the same stage for a long time is the signal
            # a shared operational layer exists to surface.
            age = (now - _parse_ts(o["updated_at"])).days if _parse_ts(o["updated_at"]) else 0
            if age >= 14:
                overdue.append({"id": o["id"], "ref": o["ref"], "title": dl(o["title"]),
                                "status_label": tpl_stage_label(conn, o["template"], o["status"]),
                                "days": age})

        label = tpl_stage_label(conn, o["template"], o["status"])
        st = by_status.setdefault(o["status"], {"key": o["status"], "label": label, "count": 0})
        st["count"] += 1
        # The key travels; the frontend localizes it through tplLabel().
        tp = by_template.setdefault(o["template"], {
            "key": o["template"], "label": (tpl_get(conn, o["template"]) or {}).get("label", o["template"]),
            "orders": 0, "value": 0.0})
        tp["orders"] += 1
        tp["value"] += o_value

        ws = ws_by_id[o["workspace_id"]]
        other = ws["partner_org_id"] if ws["buyer_org_id"] in my_orgs else ws["buyer_org_id"]
        if other:
            p = partners.setdefault(other, {"org_id": other, "name": "", "orders": 0,
                                            "open": 0, "value": 0.0, "last": ""})
            p["orders"] += 1
            p["value"] += o_value
            if not is_closed:
                p["open"] += 1
            if (o["updated_at"] or "") > p["last"]:
                p["last"] = o["updated_at"] or ""

    for pid, p in partners.items():
        row = conn.execute("SELECT name FROM orgs WHERE id=?", (pid,)).fetchone()
        p["name"] = dl(row["name"]) if row else ""
        p["value"] = "EUR %.2f" % p["value"]

    # How long a proposed term waits before the other side agrees to it.
    for e in conn.execute(
            "SELECT o.id AS oid, t.key, t.updated_at FROM order_terms t JOIN orders o ON o.id=t.order_id "
            "WHERE o.workspace_id IN (%s) AND t.state='agreed'" % ph, list(ws_by_id)).fetchall():
        prop = conn.execute(
            "SELECT created_at FROM events WHERE order_id=? AND kind='term_proposed' "
            "AND meta_json LIKE ? ORDER BY id DESC LIMIT 1",
            (e["oid"], '%%"term": "%s"%%' % e["key"])).fetchone()
        a, b = _parse_ts(prop["created_at"]) if prop else None, _parse_ts(e["updated_at"])
        if a and b and b >= a:
            approval_days.append((b - a).total_seconds() / 86400.0)

    store_revenue = 0.0
    biz = [oid for oid in my_orgs]
    if biz:
        sph = ",".join("?" * len(biz))
        for so in conn.execute(
                "SELECT qty, product_id FROM store_orders WHERE org_id IN (%s) AND status='fulfilled'"
                % sph, biz).fetchall():
            pr = conn.execute("SELECT price FROM products WHERE id=?", (so["product_id"],)).fetchone()
            if pr:
                store_revenue += _price_eur(pr["price"]) * (so["qty"] or 1)

    ev_since = since.strftime("%Y-%m-%d %H:%M:%S")
    counts = {}
    for kind, sql, args in (
            ("events", "SELECT COUNT(*) c FROM events WHERE workspace_id IN (%s) AND created_at>=?" % ph,
             list(ws_by_id) + [ev_since]),
            ("documents", "SELECT COUNT(*) c FROM attachments WHERE order_id IN "
                          "(SELECT id FROM orders WHERE workspace_id IN (%s))" % ph, list(ws_by_id)),
            ("messages", "SELECT COUNT(*) c FROM ws_messages WHERE workspace_id IN (%s)" % ph,
             list(ws_by_id)),
            ("signatures", "SELECT COUNT(*) c FROM signatures WHERE org_id IN (%s) AND status='signed'"
                           % ",".join("?" * len(my_orgs)), list(my_orgs))):
        try:
            counts[kind] = conn.execute(sql, args).fetchone()["c"]
        except Exception:
            counts[kind] = 0

    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    return jsonify({
        "range": {"days": days, "options": list(ANALYTICS_RANGES)},
        "revenue": {
            "agreed": "EUR %.2f" % value_total,
            "open": "EUR %.2f" % value_open,
            "store": "EUR %.2f" % store_revenue,
            "total": "EUR %.2f" % (value_total + store_revenue),
            "series": [{"period": m, "value": round(value_by_month[m], 2)} for m in months],
        },
        "productivity": {
            "orders_total": len(orders), "open": open_orders, "closed": closed_orders,
            "completion_rate": int(round(100.0 * closed_orders / len(orders))) if orders else 0,
            "avg_cycle_days": avg(cycle_days),
            "avg_approval_days": avg(approval_days),
            "series": [{"period": m, "created": created_by_month[m], "closed": closed_by_month[m]}
                       for m in months],
        },
        "status": sorted(by_status.values(), key=lambda s: -s["count"]),
        "verticals": sorted(
            [dict(v, value="EUR %.2f" % v["value"]) for v in by_template.values()],
            key=lambda v: -v["orders"]),
        "customers": sorted(partners.values(), key=lambda p: -p["orders"])[:12],
        "attention": sorted(overdue, key=lambda x: -x["days"])[:8],
        "activity": counts,
    })


# --------------------------------------------------------------------------- #
#  What this company actually does
#
#  Seam covers seven trades. A construction firm has no use for a harvest supply
#  agreement and an online shop has none for a site protocol, so the library is
#  narrowed to the trades a company says it works in. It is a filter, never a
#  wall: everything is one button away, and the choice can be changed.
#
#  NULL means the question has not been asked yet, which is what puts the
#  chooser in front of someone signing in for the first time. An empty list is a
#  deliberate "show me everything" and is not the same thing.
# --------------------------------------------------------------------------- #
SECTORS = ("general", "ecommerce", "agency", "construction", "restaurant",
           "agriculture", "auto-import")


def org_sectors(org):
    """Returns (sectors, asked). `asked` is False only when nobody has chosen."""
    if not org:
        return [], False
    raw = org["sectors_json"] if "sectors_json" in org.keys() else None
    if raw is None:
        return [], False
    try:
        got = json.loads(raw)
    except (TypeError, ValueError):
        return [], False
    if not isinstance(got, list):
        return [], False
    return [s for s in got if s in SECTORS], True


@app.get("/api/org/sectors")
@login_required
def org_sectors_get():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    sectors, asked = org_sectors(org)
    return jsonify({"sectors": sectors, "asked": asked, "all": list(SECTORS),
                    "can_set": bool(org) and org_role(conn, user["id"], org["id"])
                    in ("owner", "admin")})


@app.put("/api/org/sectors")
@login_required
def org_sectors_set():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    want = body().get("sectors")
    if not isinstance(want, list):
        raise ApiError("Изберете поне един отрасъл", 400, code="field_required",
                       info={"field": "sectors"})
    # Order follows SECTORS rather than the order they were clicked, so the
    # menus are stable between sessions.
    chosen = [s for s in SECTORS if s in want]
    conn.execute("UPDATE orgs SET sectors_json=? WHERE id=?",
                 (json.dumps(chosen, ensure_ascii=False), org["id"]))
    return jsonify({"sectors": chosen, "asked": True})


# --------------------------------------------------------------------------- #
#  Profiles
#
#  Two of them, and they answer different questions. The person's profile says
#  who is on the other end of a message; the company's says who the contract is
#  with. Both are visible to people who already share a company or a workspace -
#  the same boundary everything else here uses. Nothing is public.
# --------------------------------------------------------------------------- #
PROFILE_DIR = os.path.join(UPLOAD_DIR, "profile")
#: No SVG. An SVG is a document that can carry script, and this one is served
#: back to signed-in people from our own origin.
AVATAR_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
AVATAR_MAX = 4 * 1024 * 1024
LINK_MAX = 8


#: Anything of the shape "scheme:", which is what decides whether a string is
#: already a URL or a bare host somebody typed.
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _public_url(value, field, limit=300):
    """A link safe to put in an href, or "" when there was nothing.

    People type "example.com", so a bare host has to gain a scheme. The order
    matters and is the whole point of this function: **check the scheme first,
    then add one**. Prepending first turns "javascript:alert(1)" into
    "https://javascript:alert(1)", which passes a naive http-or-https test and
    is then stored as a link. Anything carrying a scheme that is not http or
    https is refused outright rather than repaired.
    """
    url = str(value or "").strip()[:limit]
    if not url:
        return ""
    if _SCHEME.match(url):
        if not url.lower().startswith(("http://", "https://")):
            raise ApiError("Невалиден адрес", 400, code="invalid_value", info={"field": field})
    else:
        url = "https://" + url
    if not re.match(r"^https?://[^\s<>\"'\\]+\.[^\s<>\"'\\]+$", url):
        raise ApiError("Невалиден адрес", 400, code="invalid_value", info={"field": field})
    return url


def _links(raw):
    try:
        got = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for item in got if isinstance(got, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()[:400]
        # Only the two schemes a browser should follow from a profile. A
        # javascript: or data: link here would be a stored cross-site script
        # handed to every colleague and counterparty who opens the page.
        if not url.lower().startswith(("http://", "https://")):
            continue
        out.append({"label": str(item.get("label") or "").strip()[:60], "url": url})
        if len(out) >= LINK_MAX:
            break
    return out


def _person_row(conn, uid):
    """The whole user row.

    `g.user` deliberately carries only id, email, name and lang, so a handler
    that wants the profile columns has to ask for them. Reading them off
    `g.user` silently produced an empty profile and, worse, skipped deleting
    the picture a new upload replaced."""
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def _person_profile(row, full=False):
    k = row.keys()
    out = {"id": row["id"], "name": row["name"],
           "title": (row["job_title"] if "job_title" in k else "") or "",
           "bio": (row["bio"] if "bio" in k else "") or "",
           "avatar": ("/uploads/profile/%s" % row["avatar"])
                     if ("avatar" in k and row["avatar"]) else "",
           "links": _links(row["links_json"] if "links_json" in k else "")}
    if full:
        out["email"] = row["email"] if "email" in k else ""
        out["phone"] = (row["phone"] if "phone" in k else "") or ""
    return out


def _col(row, name, default=""):
    """Read a column that older databases may not have yet."""
    return (row[name] if name in row.keys() else None) or default


def _company_profile(org, conn=None):
    if not org:
        return {}
    k = org.keys()
    sectors, _asked = org_sectors(org)
    contacts = _org_contacts(org)
    out = {"id": org["id"], "name": org["name"], "country": org["country"] or "",
           "reg_number": org["reg_number"] or "",
           "about": (org["about"] if "about" in k else "") or "",
           "website": (org["website"] if "website" in k else "") or "",
           "logo": ("/uploads/profile/%s" % org["logo"]) if ("logo" in k and org["logo"]) else "",
           "links": _links(org["org_links_json"] if "org_links_json" in k else ""),
           "sectors": sectors, "verified": bool(org["verified_company"]),
           "contacts": contacts,
           "kind": org["kind"],
           "posture": profiles.posture(org["kind"], _col(org, "posture") or None),
           "headline": _col(org, "headline"),
           "size_band": _col(org, "size_band"),
           "founded": _col(org, "founded"),
           "contact_email": _col(org, "contact_email"),
           "areas": _str_list(_col(org, "areas_json")),
           "listed": bool(_col(org, "listed", 0))}
    if conn is not None:
        out["portfolio_count"] = conn.execute(
            "SELECT COUNT(*) c FROM portfolio WHERE org_id=? AND visible=1",
            (org["id"],)).fetchone()["c"]
        out.update(_profile_standing(out, contacts))
    return out


def _profile_standing(prof, contacts):
    """Tier, percentage and the outstanding list, measured by profiles.py."""
    measured = dict(prof)
    measured["contact"] = any(contacts.get(f) for f in CONTACT_FIELDS if f != "note") \
        or prof.get("contact_email") or prof.get("website")
    measured["portfolio"] = prof.get("portfolio_count", 0)
    return {"tier": profiles.tier(measured),
            "completeness": profiles.completeness(measured),
            "checklist": profiles.checklist(measured),
            "missing": list(profiles.missing(measured)),
            "may_list": profiles.may_list(measured)}


def _str_list(raw, limit=12, each=40):
    try:
        got = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x).strip()[:each] for x in got if str(x or "").strip()][:limit]


@app.get("/api/profile")
@login_required
def profile_get():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    return jsonify({
        "person": _person_profile(_person_row(conn, user["id"]), full=True),
        "company": _company_profile(org, conn),
        "can_edit_company": bool(org) and org_role(conn, user["id"], org["id"]) in ("owner", "admin"),
    })


def _patch(conn, table, row_id, d, fields):
    """Only what was sent is written.

    A PUT that replaces the whole record means anyone calling it with one field
    silently wipes the rest - and a profile is exactly the kind of thing people
    update one field at a time. Clearing is still possible: send the field with
    an empty value."""
    sets, args = [], []
    for key, col, limit in fields:
        if key not in d:
            continue
        sets.append("%s=?" % col)
        args.append(str(d.get(key) or "").strip()[:limit])
    if "links" in d:
        sets.append("links_json=?" if table == "users" else "org_links_json=?")
        args.append(json.dumps(_links(json.dumps(d.get("links") or [])), ensure_ascii=False))
    if not sets:
        return
    conn.execute("UPDATE %s SET %s WHERE id=?" % (table, ", ".join(sets)), args + [row_id])


@app.put("/api/profile")
@login_required
def profile_set():
    conn, user = g.conn, g.user
    _patch(conn, "users", user["id"], body(),
           [("title", "job_title", 80), ("bio", "bio", 1200), ("phone", "phone", 40)])
    return jsonify(_person_profile(_person_row(conn, user["id"]), full=True))


@app.put("/api/profile/company")
@login_required
def profile_company_set():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = dict(body())
    if "website" in d:
        # People type "example.com". Storing that unchanged makes a link the
        # browser reads as relative to Seam itself; repairing it without
        # checking the scheme first would store "https://javascript:..." as a
        # perfectly valid looking link. _public_url does both in the right order.
        d["website"] = _public_url(d.get("website"), "website")
    if "posture" in d and d.get("posture") not in profiles.POSTURES:
        raise ApiError("Невалидна стойност", 400, code="invalid_value",
                       info={"field": "posture"})
    if "size_band" in d and d.get("size_band") and d.get("size_band") not in profiles.SIZES:
        raise ApiError("Невалидна стойност", 400, code="invalid_value",
                       info={"field": "size_band"})
    if "founded" in d and str(d.get("founded") or "").strip():
        year = str(d.get("founded")).strip()
        if not re.match(r"^(1[89]|20)\d{2}$", year) or int(year) > datetime.utcnow().year:
            raise ApiError("Невалидна година", 400, code="invalid_value",
                           info={"field": "founded"})
    if "contact_email" in d and str(d.get("contact_email") or "").strip():
        if not profiles.mail_href(str(d.get("contact_email"))):
            raise ApiError("Невалиден адрес", 400, code="invalid_email",
                           info={"field": "contact_email"})
    _patch(conn, "orgs", org["id"], d,
           [("about", "about", 2000), ("website", "website", 300),
            ("posture", "posture", 12), ("headline", "headline", profiles.HEADLINE_MAX),
            ("size_band", "size_band", 12), ("founded", "founded", 4),
            ("contact_email", "contact_email", 254)])
    if "areas" in d:
        conn.execute("UPDATE orgs SET areas_json=? WHERE id=?",
                     (json.dumps(_str_list(json.dumps(d.get("areas") or [])),
                                 ensure_ascii=False), org["id"]))
    if "listed" in d:
        # A profile cannot be published while it is still a draft: the switch
        # would put an empty card in front of strangers, and the owner would
        # never know why nobody wrote.
        want = 1 if d.get("listed") else 0
        if want:
            fresh = _company_profile(
                conn.execute("SELECT * FROM orgs WHERE id=?", (org["id"],)).fetchone(), conn)
            if not fresh["may_list"]:
                raise ApiError("Профилът още не е достатъчно попълнен", 400,
                               code="profile_incomplete", info={"missing": fresh["missing"]})
        conn.execute("UPDATE orgs SET listed=? WHERE id=?", (want, org["id"]))
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (org["id"],)).fetchone()
    return jsonify(_company_profile(org, conn))


@app.post("/api/profile/photo")
@login_required
def profile_photo():
    """One picture for the person, one for the company. Replacing one deletes
    the file it replaced, so a profile changed weekly does not fill the disk."""
    conn, user = g.conn, g.user
    which = (request.args.get("of") or "person").strip()
    if which not in ("person", "company"):
        raise ApiError("Невалидно действие", 400, code="invalid_action")
    org = user_primary_org(conn, user["id"])
    if which == "company":
        if not org:
            raise ApiError("Няма организация", 400, code="not_found")
        require_role(conn, user["id"], org["id"], "owner", "admin")
    f = (request.files.getlist("file") or [None])[0]
    if not f:
        raise ApiError("Липсва файл", 400, code="field_required", info={"field": "file"})
    ext = (f.filename or "").rsplit(".", 1)[-1].lower() if "." in (f.filename or "") else ""
    if ext not in AVATAR_EXT:
        raise ApiError("Неподдържан формат (само снимки)", 400, code="att_type")
    # The whole-request ceiling is 64 MB, because progress evidence can be
    # video. A profile picture is not, so refuse before anything is written -
    # otherwise every oversized attempt costs a 64 MB write and a delete.
    if (request.content_length or 0) > AVATAR_MAX + 8192:
        raise ApiError("Файлът е твърде голям", 400, code="att_type")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    fname = secrets.token_hex(12) + "." + ext
    path = os.path.join(PROFILE_DIR, fname)
    f.save(path)
    if os.path.getsize(path) > AVATAR_MAX:
        os.remove(path)
        raise ApiError("Файлът е твърде голям", 400, code="att_type")
    if which == "company":
        old = org["logo"] if "logo" in org.keys() else None
        conn.execute("UPDATE orgs SET logo=? WHERE id=?", (fname, org["id"]))
    else:
        old = (_person_row(conn, user["id"]) or {})["avatar"]
        conn.execute("UPDATE users SET avatar=? WHERE id=?", (fname, user["id"]))
    if old and old != fname:
        try:
            os.remove(os.path.join(PROFILE_DIR, old))
        except OSError:
            pass
    return jsonify({"url": "/uploads/profile/" + fname})


@app.delete("/api/profile/photo")
@login_required
def profile_photo_clear():
    conn, user = g.conn, g.user
    which = (request.args.get("of") or "person").strip()
    org = user_primary_org(conn, user["id"])
    if which == "company":
        if not org:
            raise ApiError("Няма организация", 400, code="not_found")
        require_role(conn, user["id"], org["id"], "owner", "admin")
        old = org["logo"] if "logo" in org.keys() else None
        conn.execute("UPDATE orgs SET logo=NULL WHERE id=?", (org["id"],))
    else:
        old = (_person_row(conn, user["id"]) or {})["avatar"]
        conn.execute("UPDATE users SET avatar=NULL WHERE id=?", (user["id"],))
    if old:
        try:
            os.remove(os.path.join(PROFILE_DIR, old))
        except OSError:
            pass
    return jsonify({"url": ""})


def _shares_ground(conn, me, other_uid):
    """True when two people already have a reason to see each other: the same
    company, or two companies with a workspace between them."""
    if me == other_uid:
        return True
    row = conn.execute(
        "SELECT 1 FROM memberships a JOIN memberships b ON a.org_id = b.org_id "
        "WHERE a.user_id=? AND b.user_id=? LIMIT 1", (me, other_uid)).fetchone()
    if row:
        return True
    row = conn.execute(
        "SELECT 1 FROM workspaces w "
        "JOIN memberships ma ON ma.org_id IN (w.buyer_org_id, w.partner_org_id) "
        "JOIN memberships mb ON mb.org_id IN (w.buyer_org_id, w.partner_org_id) "
        "WHERE ma.user_id=? AND mb.user_id=? LIMIT 1", (me, other_uid)).fetchone()
    return bool(row)


@app.get("/api/profile/<int:uid>")
@login_required
def profile_of(uid):
    """Somebody else's card. "Not allowed" and "no such person" read the same
    from outside, so this cannot be used to find out who has an account."""
    conn, user = g.conn, g.user
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not _shares_ground(conn, user["id"], uid):
        raise ApiError("Не е намерено", 404, code="not_found")
    out = _person_profile(row, full=True)
    org = user_primary_org(conn, uid)
    out["company"] = _company_profile(org, conn) if org else {}
    return jsonify(out)


@app.get("/uploads/profile/<path:fname>")
def serve_profile_photo(fname):
    """Profile pictures are for people inside the installation, not the web."""
    if "uid" not in session:
        return "", 401
    if not re.match(r"^[0-9a-f]{24}\.[a-z0-9]{2,5}$", fname or ""):
        return "", 404
    path = os.path.join(PROFILE_DIR, fname)
    if not os.path.exists(path):
        return "", 404
    resp = make_response(send_from_directory(PROFILE_DIR, fname))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp


# --------------------------------------------------------------------------- #
#  Portfolio: what a company has actually done
#
#  Typed in by its own people, so on its own it proves nothing. What makes it
#  worth reading is the evidence hung on it - documents already filed on the
#  shelf, with their checksum and their date - and the fact that the Seam
#  reference next to it is built from delivered orders and cannot be typed.
# --------------------------------------------------------------------------- #
PF_LIMITS = (("title", 160), ("summary", 1200), ("role", 120),
             ("client", 120), ("place", 120), ("year", 4), ("value_band", 24))
PF_MAX = 60


def _pf_row(conn, r, docs=True):
    out = {"id": r["id"], "kind": r["kind"], "title": r["title"],
           "summary": r["summary"] or "", "role": r["role"] or "",
           "client": r["client"] or "", "place": r["place"] or "",
           "year": r["year"] or "", "value_band": r["value_band"] or "",
           "url": r["url"] or "", "visible": bool(r["visible"]),
           "sort": r["sort"], "at": r["created_at"], "docs": []}
    if docs:
        rows = conn.execute(
            "SELECT d.* FROM portfolio_docs pd JOIN documents d ON d.id = pd.doc_id "
            "WHERE pd.item_id=? ORDER BY d.id", (r["id"],)).fetchall()
        out["docs"] = [{"id": d["id"], "title": d["title"], "name": d["original_name"],
                        "mime": d["mime"], "size": d["size"],
                        "sha256": (d["sha256"] or "")[:16],
                        "url": "/files/%d/%s" % (d["id"], d["original_name"])} for d in rows]
    return out


def _pf_fields(d, creating):
    out = {}
    for key, limit in PF_LIMITS:
        if key in d or creating:
            out[key] = str(d.get(key) or "").strip()[:limit]
    if "kind" in d or creating:
        kind = str(d.get("kind") or "project").strip()
        if kind not in profiles.PORTFOLIO_KINDS:
            raise ApiError("Невалиден вид", 400, code="invalid_value", info={"field": "kind"})
        out["kind"] = kind
    if out.get("year"):
        if not re.match(r"^(19|20)\d{2}$", out["year"]) or int(out["year"]) > datetime.utcnow().year:
            raise ApiError("Невалидна година", 400, code="invalid_value", info={"field": "year"})
    if "url" in d or creating:
        out["url"] = _public_url(d.get("url"), "url")
    if "visible" in d:
        out["visible"] = 1 if d.get("visible") else 0
    if "sort" in d:
        try:
            out["sort"] = max(-999, min(999, int(d.get("sort") or 0)))
        except (TypeError, ValueError):
            out["sort"] = 0
    if creating and not out.get("title"):
        raise ApiError("Липсва заглавие", 400, code="field_required", info={"field": "title"})
    return out


def _pf_item(conn, uid, item_id, write=False):
    """One entry, and the right to read or change it.

    Reading: your own company always; another company's only when the entry is
    visible and the profile is one you are allowed to see at all. Writing:
    owner or admin of the company that owns it, never the other side.
    """
    r = conn.execute("SELECT * FROM portfolio WHERE id=?", (item_id,)).fetchone()
    if not r:
        raise ApiError("Не е намерено", 404, code="not_found")
    role = org_role(conn, uid, r["org_id"])
    if role is None:
        if write or not r["visible"] or not _profile_readable(conn, uid, r["org_id"]):
            raise ApiError("Не е намерено", 404, code="not_found")
    elif write and role not in ("owner", "admin"):
        raise ApiError("Няма права", 403, code="forbidden")
    return r


@app.get("/api/portfolio")
@login_required
def portfolio_list():
    conn, user = g.conn, g.user
    want = request.args.get("org_id")
    if want and str(want).isdigit():
        org_id = int(want)
        mine = org_role(conn, user["id"], org_id) is not None
        if not mine and not _profile_readable(conn, user["id"], org_id):
            raise ApiError("Не е намерено", 404, code="not_found")
    else:
        org = user_primary_org(conn, user["id"])
        if not org:
            raise ApiError("Няма организация", 400, code="not_found")
        org_id, mine = org["id"], True
    sql = "SELECT * FROM portfolio WHERE org_id=?"
    if not mine:
        sql += " AND visible=1"
    rows = conn.execute(sql + " ORDER BY sort, id DESC", (org_id,)).fetchall()
    return jsonify({"items": [_pf_row(conn, r) for r in rows], "mine": mine,
                    "kinds": list(profiles.PORTFOLIO_KINDS), "max": PF_MAX})


@app.post("/api/portfolio")
@login_required
def portfolio_add():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    n = conn.execute("SELECT COUNT(*) c FROM portfolio WHERE org_id=?", (org["id"],)).fetchone()["c"]
    if n >= PF_MAX:
        raise ApiError("Достигнат е максимумът от %d записа" % PF_MAX, 400, code="limit_reached")
    f = _pf_fields(body(), creating=True)
    cols = list(f) + ["org_id", "created_by"]
    cur = conn.execute(
        "INSERT INTO portfolio (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
        list(f.values()) + [org["id"], user["id"]])
    conn.commit()
    return jsonify(_pf_row(conn, conn.execute("SELECT * FROM portfolio WHERE id=?",
                                              (cur.lastrowid,)).fetchone())), 201


@app.patch("/api/portfolio/<int:item_id>")
@login_required
def portfolio_edit(item_id):
    conn, user = g.conn, g.user
    _pf_item(conn, user["id"], item_id, write=True)
    f = _pf_fields(body(), creating=False)
    if f:
        conn.execute("UPDATE portfolio SET %s, updated_at=datetime('now') WHERE id=?"
                     % ", ".join("%s=?" % c for c in f), list(f.values()) + [item_id])
        conn.commit()
    return jsonify(_pf_row(conn, conn.execute("SELECT * FROM portfolio WHERE id=?",
                                              (item_id,)).fetchone()))


@app.delete("/api/portfolio/<int:item_id>")
@login_required
def portfolio_delete(item_id):
    conn, user = g.conn, g.user
    _pf_item(conn, user["id"], item_id, write=True)
    # The link rows go; the documents themselves stay on the shelf. Deleting a
    # project should not quietly destroy a certificate filed against it.
    conn.execute("DELETE FROM portfolio WHERE id=?", (item_id,))
    conn.commit()
    return jsonify({"ok": True})


@app.post("/api/portfolio/<int:item_id>/docs")
@login_required
def portfolio_attach(item_id):
    """Hang already filed documents on an entry.

    Only documents this company filed itself. A file received from the other
    side of a workspace belongs to them, and publishing it as evidence of your
    own work would be both a lie and a leak.
    """
    conn, user = g.conn, g.user
    item = _pf_item(conn, user["id"], item_id, write=True)
    ids = body().get("doc_ids") or []
    if not isinstance(ids, list) or not ids:
        raise ApiError("Липсва документ", 400, code="field_required", info={"field": "doc_ids"})
    added = 0
    for raw in ids[:20]:
        try:
            doc_id = int(raw)
        except (TypeError, ValueError):
            continue
        d = conn.execute("SELECT id, org_id FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not d or d["org_id"] != item["org_id"]:
            raise ApiError("Не е намерено", 404, code="not_found")
        conn.execute("INSERT OR IGNORE INTO portfolio_docs (item_id, doc_id) VALUES (?,?)",
                     (item_id, doc_id))
        added += 1
    conn.commit()
    return jsonify({"added": added,
                    "item": _pf_row(conn, conn.execute("SELECT * FROM portfolio WHERE id=?",
                                                       (item_id,)).fetchone())})


@app.delete("/api/portfolio/<int:item_id>/docs/<int:doc_id>")
@login_required
def portfolio_detach(item_id, doc_id):
    conn, user = g.conn, g.user
    _pf_item(conn, user["id"], item_id, write=True)
    conn.execute("DELETE FROM portfolio_docs WHERE item_id=? AND doc_id=?", (item_id, doc_id))
    conn.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  The directory: profiles a company may be found by
#
#  Off by default. Being on Seam is not consent to be listed, and a listing is
#  refused until the profile says who it is, where it is, what trades it is in
#  and whether it is offering work or looking for it - the four things without
#  which the card wastes the reader's time.
# --------------------------------------------------------------------------- #
DIRECTORY_PAGE = 24


def _profile_readable(conn, uid, org_id):
    """May this person see that company's profile at all?

    Their own, one they already share a workspace with, or one that has
    published itself. Nothing else - the directory is not a way to enumerate
    every company on the installation.
    """
    if org_role(conn, uid, org_id) is not None:
        return True
    row = conn.execute("SELECT listed FROM orgs WHERE id=?", (org_id,)).fetchone()
    if row and row["listed"]:
        return True
    return bool(conn.execute(
        "SELECT 1 FROM workspaces w JOIN memberships m "
        "ON m.org_id IN (w.buyer_org_id, w.partner_org_id) "
        "WHERE m.user_id=? AND ? IN (w.buyer_org_id, w.partner_org_id) LIMIT 1",
        (uid, org_id)).fetchone())


def _shares_org_ground(conn, uid, org_id):
    """True when the viewer is inside that company or already works with it.
    Contact details are shown to those people and to nobody else."""
    if org_role(conn, uid, org_id) is not None:
        return True
    return bool(conn.execute(
        "SELECT 1 FROM workspaces w JOIN memberships m "
        "ON m.org_id IN (w.buyer_org_id, w.partner_org_id) "
        "WHERE m.user_id=? AND ? IN (w.buyer_org_id, w.partner_org_id) LIMIT 1",
        (uid, org_id)).fetchone())


def _directory_card(conn, org, uid, full=False):
    """A profile as a stranger sees it: everything the company chose to publish
    and nothing it did not. No counterparties, no order history, no figures."""
    prof = _company_profile(org, conn)
    near = _shares_org_ground(conn, uid, org["id"])
    card = {k: prof[k] for k in ("id", "name", "country", "logo", "posture", "headline",
                                 "sectors", "verified", "size_band", "tier",
                                 "completeness", "portfolio_count")}
    card["areas"] = prof["areas"]
    card["known"] = near
    if full:
        card["about"] = prof["about"]
        card["website"] = prof["website"]
        card["links"] = prof["links"]
        card["founded"] = prof["founded"]
        card["reg_number"] = prof["reg_number"] if near else ""
        contacts = prof["contacts"]
        card["reach"] = profiles.reach(
            {"phone": contacts.get("phone") or "",
             "email": prof["contact_email"] or "",
             "name": prof["name"], "country": prof["country"], "posture": prof["posture"],
             "headline": prof["headline"], "sectors": prof["sectors"]}, near)
        if near:
            card["contacts"] = contacts
    return card


@app.get("/api/directory")
@login_required
def directory_list():
    """Search the published profiles. Filters are applied in SQL where they are
    cheap and by profiles.py where the rule is a judgement, so one definition
    of "listed" governs both this and the switch that publishes a profile."""
    conn, user = g.conn, g.user
    want = (request.args.get("posture") or "").strip()
    country = (request.args.get("country") or "").strip()[:2].upper()
    sector = (request.args.get("sector") or "").strip()
    query = (request.args.get("q") or "").strip()[:80]
    try:
        page = max(0, int(request.args.get("page") or 0))
    except (TypeError, ValueError):
        page = 0
    mine = user_primary_org(conn, user["id"])
    sql, args = "SELECT * FROM orgs WHERE listed=1", []
    if country:
        sql += " AND UPPER(COALESCE(country,''))=?"
        args.append(country)
    rows = conn.execute(sql, args).fetchall()
    out = []
    for org in rows:
        prof = _company_profile(org, conn)
        if not profiles.matches(prof, want_posture=want or None,
                                country=None, sector=sector or None, query=query or None):
            continue
        out.append(prof)
    out.sort(key=profiles.rank)
    total = len(out)
    page_rows = out[page * DIRECTORY_PAGE:(page + 1) * DIRECTORY_PAGE]
    cards = [_directory_card(conn, conn.execute("SELECT * FROM orgs WHERE id=?", (p["id"],)).fetchone(),
                             user["id"]) for p in page_rows]
    return jsonify({"items": cards, "total": total, "page": page, "per_page": DIRECTORY_PAGE,
                    "postures": list(profiles.POSTURES), "sectors": list(SECTORS),
                    "me": mine["id"] if mine else 0})


@app.get("/api/directory/<int:org_id>")
@login_required
def directory_card(org_id):
    conn, user = g.conn, g.user
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
    if not org or not _profile_readable(conn, user["id"], org_id):
        raise ApiError("Не е намерено", 404, code="not_found")
    card = _directory_card(conn, org, user["id"], full=True)
    rows = conn.execute("SELECT * FROM portfolio WHERE org_id=? AND visible=1 "
                        "ORDER BY sort, id DESC LIMIT 30", (org_id,)).fetchall()
    card["portfolio"] = [_pf_row(conn, r) for r in rows]
    card["people"] = []
    if card["known"]:
        # Who to ask for. Only once the two sides already work together: a
        # published card is a company, not a staff list for anyone to harvest.
        for p in conn.execute(
                "SELECT u.* FROM users u JOIN memberships m ON m.user_id = u.id "
                "WHERE m.org_id=? ORDER BY u.id LIMIT 12", (org_id,)).fetchall():
            card["people"].append(_person_profile(p))
    return jsonify(card)


@app.post("/api/directory/<int:org_id>/contact")
@login_required
def directory_contact(org_id):
    """Ask a company to make contact.

    The telephone number on a published profile is never handed out. This puts
    the caller's own number in front of the owner instead, by email and in the
    app, and lets them decide. It is the only way through to a company you have
    never worked with, which is why it is rate limited hard.
    """
    conn, user = g.conn, g.user
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
    if not org or not _profile_readable(conn, user["id"], org_id):
        raise ApiError("Не е намерено", 404, code="not_found")
    if org_role(conn, user["id"], org_id) is not None:
        raise ApiError("Това е вашата организация", 400, code="own_org")
    if rate_limited("contactreq:%d" % user["id"], 5, 3600) or \
       rate_limited("contactreq:to:%d" % org_id, 30, 3600):
        raise ApiError("Твърде много заявки, опитайте по-късно", 429, code="rate_limited")
    d = body()
    phone = str(d.get("phone") or "").strip()[:40]
    email = str(d.get("email") or user["email"] or "").strip()[:254]
    note = str(d.get("note") or "").strip()[:600]
    if phone and not profiles.phone_href(phone):
        raise ApiError("Невалиден телефон", 400, code="invalid_value", info={"field": "phone"})
    if not profiles.mail_href(email):
        raise ApiError("Невалиден адрес", 400, code="invalid_email", info={"field": "email"})
    if not phone and not note:
        raise ApiError("Оставете телефон или съобщение", 400, code="field_required",
                       info={"field": "note"})
    from_org = user_primary_org(conn, user["id"])
    cur = conn.execute(
        "INSERT INTO contact_requests (org_id, from_org_id, from_user_id, phone, email, note) "
        "VALUES (?,?,?,?,?,?)",
        (org_id, from_org["id"] if from_org else None, user["id"], phone, email, note))
    conn.commit()
    who = (from_org["name"] if from_org else user["name"])
    delivered = _forward_contact_request(conn, org, user, who, phone, email, note)
    conn.execute("UPDATE contact_requests SET sent=? WHERE id=?", (1 if delivered else 0, cur.lastrowid))
    notify_org(conn, org_id, None, None, "contact_request",
               "contact_request", exclude_user=user["id"], meta={"name": who})
    conn.commit()
    return jsonify({"ok": True, "delivered": bool(delivered)}), 201


def _forward_contact_request(conn, org, sender, who, phone, email, note):
    """Deliver the request by email.

    To the company's stated office address if it has one, otherwise to every
    owner and admin: a request nobody reads is the same as no button at all.
    Written in the recipient company's language, not the caller's, because the
    person opening it is the one who has to act on it.
    """
    if not mailer.enabled():
        return False
    owners = conn.execute(
        "SELECT u.id, u.email FROM users u JOIN memberships m ON m.user_id = u.id "
        "WHERE m.org_id=? AND m.role IN ('owner','admin') ORDER BY u.id", (org["id"],)).fetchall()
    stated = profiles.forward_to({"contact_email": _col(org, "contact_email")})
    to = [stated] if stated else [r["email"] for r in owners if r["email"]]
    if not to:
        return False
    lang = "en"
    if owners:
        lang = _user_email_lang(conn, owners[0]["id"])[1] or "en"
    L = EMAIL_I18N.get(lang) or EMAIL_I18N["en"]
    base = request.url_root.rstrip("/") if request else ""
    rows = [(L["cr_from"], who), (L["cr_person"], sender["name"]),
            (L["cr_phone"], phone), (L["cr_email"], email)]
    body_html = "".join(
        '<p style="margin:4px 0"><b>%s:</b> %s</p>' % (html_escape(k), html_escape(v))
        for k, v in rows if v)
    if note:
        body_html += '<p style="margin:12px 0;white-space:pre-wrap">%s</p>' % html_escape(note)
    html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b">'
            "<p>%s</p>%s%s</div>"
            % (html_escape(L["cr_line"]), body_html,
               _email_button(base + "/#/directory", L["cta"])))
    subject = "Seam · " + L["cr_subject"].replace("{name}", who)
    for addr in to[:5]:
        mailer.send_async(addr, subject, html)
    return True


@app.get("/api/contact-requests")
@login_required
def contact_requests_list():
    """What came in through the published profile."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    rows = conn.execute(
        "SELECT c.*, o.name org_name FROM contact_requests c "
        "LEFT JOIN orgs o ON o.id = c.from_org_id "
        "WHERE c.org_id=? ORDER BY c.id DESC LIMIT 100", (org["id"],)).fetchall()
    return jsonify({"items": [
        {"id": r["id"], "from": r["org_name"] or "", "phone": r["phone"] or "",
         "phone_href": profiles.phone_href(r["phone"]),
         "email": r["email"] or "", "email_href": profiles.mail_href(r["email"]),
         "note": r["note"] or "", "sent": bool(r["sent"]), "at": r["created_at"]}
        for r in rows]})


# --------------------------------------------------------------------------- #
#  Workspace communication channel (relationship-level, not per order)
# --------------------------------------------------------------------------- #
CONTACT_FIELDS = ("phone", "whatsapp", "viber", "telegram", "alt_email", "note")


def _org_contacts(org_row):
    try:
        c = json.loads(org_row["contact_json"] or "{}") if ("contact_json" in org_row.keys()) else {}
    except Exception:
        c = {}
    return {k: dl(c.get(k) or "") for k in CONTACT_FIELDS}


@app.get("/api/org/contacts")
@login_required
def org_contacts_get():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    return jsonify(_org_contacts(org))


@app.put("/api/org/contacts")
@login_required
def org_contacts_set():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    d = body()
    data = {k: (str(d.get(k) or "").strip()[:120]) for k in CONTACT_FIELDS}
    conn.execute("UPDATE orgs SET contact_json=? WHERE id=?", (json.dumps(data, ensure_ascii=False), org["id"]))
    return jsonify(data)


# --------------------------------------------------------------------------- #
#  Electronic signatures (roadmap item 2)
#
#  Levels, in the sense of eIDAS:
#    simple   (SES)  - typed/drawn name + explicit consent, full evidence trail
#    advanced (AdES) - additionally a one-time code sent to the signer's e-mail,
#                      uniquely linking the signature to that address
#    qualified(QES)  - requires a qualified trust service provider and a
#                      qualified certificate; Seam records the request and hands
#                      off to the QTSP, it does not issue qualified certificates
#                      itself. Marked accordingly so nobody is misled.
#
#  Evidence captured for every signature: signer identity, timestamp (UTC),
#  IP, device, verification method and the SHA-256 hash of the exact document
#  content that was signed - so any later change is provable.
# --------------------------------------------------------------------------- #
SIG_LEVELS = ("simple", "advanced", "qualified")

# --------------------------------------------------------------------------- #
#  Qualified electronic signatures
#
#  Seam issues no certificates. A qualified signature is created by the trust
#  service provider the signer actually holds a certificate with; Seam computes
#  the document hash, routes it to that provider and records what comes back.
#  The provider registry, the per-country legal framework and the protocol
#  adapters (CSC API v2, Evrotrust, B-Trust) live in qtsp.py.
# --------------------------------------------------------------------------- #
QTSP_FIELDS = ("provider", "base_url", "client_id", "client_secret", "api_key",
               "relying_party_id", "client_cert", "client_key")


def org_qtsp_config(conn, org_id):
    """The provider connection for one organisation, falling back to the
    instance-wide file/env config when the org has not set up its own."""
    row = conn.execute("SELECT * FROM org_qtsp WHERE org_id=?", (org_id,)).fetchone()
    if row and row["provider"] in qtsp.PROVIDERS:
        return qtsp.normalise({f: (row[f] or "") for f in QTSP_FIELDS})
    return qtsp.file_config()


def _org_country(conn, org_id):
    row = conn.execute("SELECT country FROM orgs WHERE id=?", (org_id,)).fetchone()
    return (row["country"] if row else "") or ""


@app.get("/api/qtsp")
@login_required
def qtsp_get():
    """What this organisation can sign with: its own country's providers, the
    governing law, and whether a connection is already configured."""
    conn, user = g.conn, g.user
    org_id = (user_org_ids(conn, user["id"]) or [None])[0]
    if not org_id:
        raise ApiError("Нямате организация", 400, code="no_org")
    country = _org_country(conn, org_id)
    cfg = org_qtsp_config(conn, org_id)
    return jsonify({
        "country": country,
        "legal": qtsp.legal_note(country),
        "max_level": qtsp.max_level(country),
        "providers": [
            {"key": p["key"], "name": p["name"], "country": p["country"], "api": p["api"],
             "site": p.get("site"), "signup": p.get("signup"), "docs": p.get("docs"),
             "needs": p.get("needs", []), "note": p.get("note"),
             "home": p["country"] == (country or "").upper()}
            for p in qtsp.providers_for(country)
        ],
        "config": qtsp.public_config(cfg),
        "verified": qtsp.REGISTRY_VERIFIED,
        "trusted_list": qtsp.EU_TRUSTED_LIST,
        "csc_spec": qtsp.CSC_SPEC,
    })


@app.post("/api/qtsp")
@login_required
def qtsp_save():
    """Store this organisation's provider credentials. Secrets left blank keep
    their previous value, so the UI never has to echo them back."""
    conn, user = g.conn, g.user
    org_id = (user_org_ids(conn, user["id"]) or [None])[0]
    if not org_id:
        raise ApiError("Нямате организация", 400, code="no_org")
    d = body()
    provider = (d.get("provider") or "").strip()
    if provider and provider not in qtsp.PROVIDERS:
        raise ApiError("Непознат доставчик", 400, code="qtsp_unknown")
    if not provider:
        conn.execute("DELETE FROM org_qtsp WHERE org_id=?", (org_id,))
        conn.commit()
        return jsonify({"config": None})

    old = conn.execute("SELECT * FROM org_qtsp WHERE org_id=?", (org_id,)).fetchone()
    vals = {}
    for f in QTSP_FIELDS:
        v = (d.get(f) or "").strip()
        if not v and old and f in qtsp.SECRET_FIELDS:
            v = old[f] or ""          # blank secret means "keep what is stored"
        vals[f] = v[:500]
    vals["provider"] = provider
    if not vals["base_url"]:
        vals["base_url"] = qtsp.PROVIDERS[provider].get("base_url", "")

    conn.execute(
        "INSERT INTO org_qtsp (org_id, provider, base_url, client_id, client_secret, api_key, "
        "relying_party_id, client_cert, client_key, updated_by, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now')) "
        "ON CONFLICT(org_id) DO UPDATE SET provider=excluded.provider, base_url=excluded.base_url, "
        "client_id=excluded.client_id, client_secret=excluded.client_secret, api_key=excluded.api_key, "
        "relying_party_id=excluded.relying_party_id, client_cert=excluded.client_cert, "
        "client_key=excluded.client_key, updated_by=excluded.updated_by, updated_at=datetime('now')",
        (org_id, vals["provider"], vals["base_url"], vals["client_id"], vals["client_secret"],
         vals["api_key"], vals["relying_party_id"], vals["client_cert"], vals["client_key"],
         user["id"]))
    # Who changed this and when is kept on org_qtsp itself; the events table is
    # per-workspace and an org-level setting has no workspace to belong to.
    conn.commit()
    return jsonify({"config": qtsp.public_config(org_qtsp_config(conn, org_id))})


@app.post("/api/qtsp/test")
@login_required
def qtsp_test():
    """Prove the credentials actually work before anyone relies on them."""
    conn, user = g.conn, g.user
    org_id = (user_org_ids(conn, user["id"]) or [None])[0]
    cfg = org_qtsp_config(conn, org_id) if org_id else None
    if not cfg:
        raise ApiError("Няма конфигуриран доставчик", 400, code="qtsp_config")
    miss = qtsp.missing_fields(cfg)
    if miss:
        raise ApiError("Липсват данни: %s" % ", ".join(miss), 400, code="qtsp_config")
    try:
        if cfg["adapter"] == "csc":
            token = qtsp.csc_token(cfg)
            creds = qtsp.csc_credentials(cfg, token)
            return jsonify({"ok": True, "credentials": len(creds)})
        # Native providers have no cheap probe; a reachable host is the signal.
        qtsp.submit(cfg, "Seam connection test", _doc_hash("seam-connection-test"),
                    user["email"], user["name"])
        return jsonify({"ok": True})
    except qtsp.QtspError as e:
        raise ApiError(e.message, 502, code=e.code)


SIG_EMAIL_I18N = {
    "en": {"s": "Signature request: %s", "l": "You have been asked to sign the document below. Opening the link shows the document and the signing evidence that will be recorded.", "b": "Review and sign", "os": "Your signing code: %s", "ol": "Use this one-time code to confirm your signature. It is valid for 15 minutes."},
    "bg": {"s": "Заявка за подпис: %s", "l": "Поканени сте да подпишете документа по-долу. Линкът показва документа и доказателствата, които ще бъдат записани.", "b": "Прегледай и подпиши", "os": "Вашият код за подпис: %s", "ol": "Използвайте този еднократен код, за да потвърдите подписа. Валиден е 15 минути."},
    "de": {"s": "Signaturanfrage: %s", "l": "Sie wurden gebeten, das folgende Dokument zu unterschreiben. Der Link zeigt das Dokument und die aufgezeichneten Nachweise.", "b": "Prüfen und unterschreiben", "os": "Ihr Signaturcode: %s", "ol": "Verwenden Sie diesen Einmalcode, um Ihre Signatur zu bestätigen. Gültig für 15 Minuten."},
    "ro": {"s": "Cerere de semnare: %s", "l": "Vi s-a cerut să semnați documentul de mai jos. Linkul arată documentul și dovezile care vor fi înregistrate.", "b": "Verifică și semnează", "os": "Codul dvs. de semnare: %s", "ol": "Folosiți acest cod unic pentru a confirma semnătura. Valabil 15 minute."},
    "el": {"s": "Αίτημα υπογραφής: %s", "l": "Σας ζητήθηκε να υπογράψετε το παρακάτω έγγραφο. Ο σύνδεσμος εμφανίζει το έγγραφο και τα στοιχεία που θα καταγραφούν.", "b": "Έλεγχος και υπογραφή", "os": "Ο κωδικός υπογραφής σας: %s", "ol": "Χρησιμοποιήστε αυτόν τον κωδικό μίας χρήσης. Ισχύει για 15 λεπτά."},
    "tr": {"s": "İmza talebi: %s", "l": "Aşağıdaki belgeyi imzalamanız istendi. Bağlantı, belgeyi ve kaydedilecek delilleri gösterir.", "b": "İncele ve imzala", "os": "İmza kodunuz: %s", "ol": "İmzanızı onaylamak için bu tek kullanımlık kodu kullanın. 15 dakika geçerlidir."},
    "it": {"s": "Richiesta di firma: %s", "l": "Ti è stato chiesto di firmare il documento qui sotto. Il link mostra il documento e le prove che verranno registrate.", "b": "Esamina e firma", "os": "Il tuo codice di firma: %s", "ol": "Usa questo codice monouso per confermare la firma. Valido 15 minuti."},
    "ru": {"s": "Запрос на подпись: %s", "l": "Вас попросили подписать документ ниже. По ссылке видны документ и записываемые доказательства.", "b": "Просмотреть и подписать", "os": "Ваш код подписи: %s", "ol": "Используйте этот одноразовый код для подтверждения подписи. Действует 15 минут."},
    "es": {"s": "Solicitud de firma: %s", "l": "Se le ha pedido que firme el documento siguiente. El enlace muestra el documento y las pruebas que se registrarán.", "b": "Revisar y firmar", "os": "Su código de firma: %s", "ol": "Use este código de un solo uso para confirmar la firma. Válido 15 minutos."},
    "fr": {"s": "Demande de signature : %s", "l": "Il vous est demandé de signer le document ci-dessous. Le lien affiche le document et les preuves qui seront enregistrées.", "b": "Examiner et signer", "os": "Votre code de signature : %s", "ol": "Utilisez ce code à usage unique pour confirmer votre signature. Valable 15 minutes."},
    "pl": {"s": "Prośba o podpis: %s", "l": "Poproszono Cię o podpisanie poniższego dokumentu. Link pokazuje dokument oraz zapisywane dowody.", "b": "Sprawdź i podpisz", "os": "Twój kod podpisu: %s", "ol": "Użyj tego jednorazowego kodu, aby potwierdzić podpis. Ważny 15 minut."},
    "uk": {"s": "Запит на підпис: %s", "l": "Вас попросили підписати документ нижче. За посиланням видно документ і докази, які буде записано.", "b": "Переглянути та підписати", "os": "Ваш код підпису: %s", "ol": "Скористайтеся цим одноразовим кодом для підтвердження підпису. Дійсний 15 хвилин."},
    "pt": {"s": "Pedido de assinatura: %s", "l": "Foi-lhe pedido que assine o documento abaixo. A ligação mostra o documento e as provas que serão registadas.", "b": "Rever e assinar", "os": "O seu código de assinatura: %s", "ol": "Use este código de utilização única para confirmar a assinatura. Válido 15 minutos."},
}


def _doc_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sig_row(r, include_token=False):
    try:
        ev = json.loads(r["evidence_json"] or "{}")
    except Exception:
        ev = {}
    d = {"id": r["id"], "order_id": r["order_id"], "doc_kind": r["doc_kind"],
         "doc_title": r["doc_title"], "doc_url": r["doc_url"] or "", "doc_hash": r["doc_hash"],
         "level": r["level"], "status": r["status"], "signer_name": r["signer_name"] or "",
         "signer_email": r["signer_email"] or "", "signer_role": r["signer_role"] or "",
         "ip": r["ip"] or "", "device": r["device"] or "",
         "created_at": r["created_at"], "signed_at": r["signed_at"], "evidence": ev}
    if include_token:
        # `token` holds the hash; the link is rebuilt from the encrypted copy,
        # so a signing request can be re-sent without the database ever holding
        # a usable link.
        tok = cryptobox.decrypt_field(r["token_enc"] if "token_enc" in r.keys() else None)
        if tok:
            d["token"] = tok
            d["sign_url"] = "/sign/" + tok
    return d


@app.get("/api/signatures")
@login_required
def signatures_list():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    oid = request.args.get("order_id")
    if oid:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
        workspace_access(conn, o["workspace_id"], user["id"])
        rows = conn.execute("SELECT * FROM signatures WHERE order_id=? ORDER BY id DESC", (oid,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM signatures WHERE org_id=? ORDER BY id DESC LIMIT 100",
                            (org["id"],)).fetchall()
    return jsonify([_sig_row(r, include_token=True) for r in rows])


@app.post("/api/signatures")
@login_required
def signature_request():
    """Create a signing request for a document. The exact content that will be
    signed is hashed now, so the signature is bound to this version."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    d = body()
    level = d.get("level") if d.get("level") in SIG_LEVELS else "simple"
    order_id = d.get("order_id")
    doc_kind = (d.get("doc_kind") or "protocol").strip()[:40]

    # A level the signer's jurisdiction does not recognise is not offered. In the
    # UK there is no EU-style qualified tier, so "advanced" is the ceiling there.
    country = org["country"] or ""
    if level == "qualified" and qtsp.max_level(country) != "qualified":
        level = "advanced"

    if order_id:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o:
            raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
        ws, side = workspace_access(conn, o["workspace_id"], user["id"])
        title = "%s · %s" % (o["ref"], dl(o["title"]))
        doc_url = "/protocol/%d" % o["id"]
        # Hash the material terms + status, i.e. what the parties are agreeing to.
        terms = conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=? ORDER BY key",
                             (o["id"],)).fetchall()
        payload = "|".join([o["ref"], o["title"] or "", o["status"] or ""] +
                           ["%s=%s:%s" % (t_["key"], t_["value"] or "", t_["state"]) for t_ in terms])
    else:
        title = (d.get("doc_title") or "").strip()[:200]
        doc_url = (d.get("doc_url") or "").strip()[:500]
        if not title:
            raise ApiError("Липсва документ", 400, code="field_required", info={"field": "doc_title"})
        payload = title + "|" + doc_url

    token = secrets.token_urlsafe(24)
    dh = _doc_hash(payload)
    conn.execute(
        "INSERT INTO signatures (org_id, order_id, doc_kind, doc_title, doc_url, doc_hash, level, token, "
        "token_enc, requested_by, signer_name, signer_email, signer_role, evidence_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (org["id"], order_id, doc_kind, title, doc_url, dh, level,
         db.token_hash(token), cryptobox.encrypt_field(token), user["id"],
         (d.get("signer_name") or "").strip()[:120], (d.get("signer_email") or "").strip()[:160],
         (d.get("signer_role") or "").strip()[:80],
         json.dumps({"requested_by": user["name"], "requested_at": datetime.utcnow().isoformat() + "Z",
                     "hash_alg": "SHA-256"}, ensure_ascii=False)))
    row = conn.execute("SELECT * FROM signatures WHERE token=?",
                           (db.token_hash(token),)).fetchone()

    # Qualified level: route the hash to the provider the organisation is
    # connected to. Async providers (Evrotrust, B-Trust) push a confirmation to
    # the signer's phone; CSC providers are driven from the signing page itself.
    if level == "qualified":
        cfg = org_qtsp_config(conn, org["id"])
        ev = json.loads(row["evidence_json"] or "{}")
        ev["legal"] = qtsp.legal_note(country)
        if qtsp.is_ready(cfg):
            spec = qtsp.PROVIDERS[cfg["provider"]]
            ev.update({"qtsp": cfg["provider"], "qtsp_name": spec["name"],
                       "qtsp_api": spec["api"], "qtsp_country": spec["country"]})
            state, ref = "ready", ""
            if spec["api"] in ("native",):
                try:
                    ref, mode = qtsp.submit(cfg, title, dh,
                                            (d.get("signer_email") or "").strip(),
                                            (d.get("signer_name") or "").strip())
                    state = "pending"
                    ev["qtsp_reference"] = ref
                    ev["qtsp_submitted_at"] = datetime.utcnow().isoformat() + "Z"
                except qtsp.QtspError as e:
                    state = "error"
                    ev["qtsp_error"] = e.message
            conn.execute("UPDATE signatures SET qtsp_provider=?, qtsp_ref=?, qtsp_state=?, evidence_json=? "
                         "WHERE id=?",
                         (cfg["provider"], ref, state, json.dumps(ev, ensure_ascii=False), row["id"]))
        else:
            # Honest fallback: the request stands, the evidence is complete, but
            # nothing qualified backs it until a provider is connected.
            ev["qtsp"] = None
            ev["qtsp_note"] = ("No qualified trust service provider is connected for this organisation; "
                               "the signature is recorded with full evidence but is not backed by a "
                               "qualified certificate.")
            conn.execute("UPDATE signatures SET qtsp_state='unconfigured', evidence_json=? WHERE id=?",
                         (json.dumps(ev, ensure_ascii=False), row["id"]))
        row = conn.execute("SELECT * FROM signatures WHERE id=?", (row["id"],)).fetchone()

    email = (d.get("signer_email") or "").strip()
    if email and mailer.enabled():
        lang = req_lang() if req_lang() in SIG_EMAIL_I18N else "en"
        L = SIG_EMAIL_I18N[lang]
        link = request.url_root.rstrip("/") + "/sign/" + token
        html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b">'
                "<p>%s</p><p><b>%s</b></p>%s</div>"
                % (html_escape(L["l"]), html_escape(title), _email_button(link, L["b"])))
        mailer.send_async(email, L["s"] % title, html)

    if order_id:
        o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        ws, side = workspace_access(conn, o["workspace_id"], user["id"])
        log_event(conn, o["workspace_id"], o["id"], user, side, "signature_requested",
                  "signature_requested", {"level": level, "signer": d.get("signer_name") or email or ""})
    return jsonify(_sig_row(row, include_token=True))


@app.post("/api/signatures/<int:sid>/otp")
def signature_send_otp(sid):
    """Advanced level: e-mail a one-time code that links the signature to the
    signer's mailbox. Public because the signer may not have a Seam account."""
    conn = db.get_db()
    try:
        token = (body().get("token") or "").strip()
        r = conn.execute("SELECT * FROM signatures WHERE id=? AND token=?",
                         (sid, db.token_hash(token))).fetchone()
        if not r or r["status"] != "pending":
            raise ApiError("Невалидна заявка за подпис", 404, code="sig_invalid")
        if rate_limited("otp:%d" % sid, 5, 900):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        code = "%06d" % secrets.randbelow(1000000)
        conn.execute("UPDATE signatures SET otp_code=? WHERE id=?", (code, sid))
        conn.commit()
        out = {"ok": True}
        email = r["signer_email"]
        lang = req_lang() if req_lang() in SIG_EMAIL_I18N else "en"
        L = SIG_EMAIL_I18N[lang]
        if email and mailer.enabled():
            html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px">'
                    '<p>%s</p><p style="font-size:26px;font-weight:800;letter-spacing:4px">%s</p></div>'
                    % (html_escape(L["ol"]), html_escape(code)))
            mailer.send_async(email, L["os"] % code, html)
        elif _loopback():
            # Only on this machine, where whoever is asking is the operator.
            #
            # The whole point of the advanced level is that the code goes to
            # the signer's mailbox and coming back with it proves they hold it.
            # Handing the code to whoever already has the signing link proves
            # nothing, and the evidence record would still say the mailbox was
            # verified. That is a false claim on a document meant to stand up.
            out["code"] = code
        else:
            out["no_mail"] = True
        return jsonify(out)
    finally:
        conn.close()


@app.post("/api/signatures/<int:sid>/sign")
def signature_sign(sid):
    conn = db.get_db()
    try:
        d = body()
        token = (d.get("token") or "").strip()
        r = conn.execute("SELECT * FROM signatures WHERE id=? AND token=?",
                         (sid, db.token_hash(token))).fetchone()
        if not r:
            raise ApiError("Невалидна заявка за подпис", 404, code="sig_invalid")
        if r["status"] == "signed":
            raise ApiError("Документът вече е подписан", 400, code="sig_already")
        if rate_limited("sign:" + _client_ip(), 20, 600):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")

        name = (d.get("name") or "").strip()[:120]
        if len(name) < 3:
            raise ApiError("Въведете име на подписващия", 400, code="field_required", info={"field": "name"})
        if not d.get("consent"):
            raise ApiError("Необходимо е съгласие за подписване", 400, code="sig_consent")

        # Qualified with a connected provider: the provider's signature is the
        # proof, so Seam does not ask for its own e-mail code on top of it.
        qtsp_done = (r["level"] == "qualified" and r["qtsp_state"] == "signed")
        otp_ok = 0
        if r["level"] == "qualified" and (r["qtsp_provider"] or r["qtsp_state"] in ("ready", "pending")):
            if not qtsp_done:
                raise ApiError("Подписването при доставчика не е завършено", 400, code="sig_qtsp_pending")
        elif r["level"] in ("advanced", "qualified"):
            code = (d.get("code") or "").strip()
            if not r["otp_code"] or code != r["otp_code"]:
                raise ApiError("Грешен код за потвърждение", 400, code="sig_otp")
            otp_ok = 1

        img = (d.get("signature_img") or "")
        if img and not img.startswith("data:image/"):
            img = ""
        img = img[:200000]

        ip, device = _client_ip(), _device_label()
        now = datetime.utcnow().isoformat() + "Z"
        try:
            ev = json.loads(r["evidence_json"] or "{}")
        except Exception:
            ev = {}
        # `verification` is the evidence record and stays in one canonical
        # language; `verification_kind` is what the certificate renders, so the
        # reader sees it in their own language.
        if qtsp_done:
            pname = ev.get("qtsp_name") or qtsp.PROVIDERS.get(r["qtsp_provider"], {}).get("name") \
                or r["qtsp_provider"] or ""
            verification = "qualified certificate via %s" % pname
            ev["verification_kind"] = "qtsp"
            ev["qtsp_name"] = pname
            ev.setdefault("qtsp", r["qtsp_provider"])
            ev.setdefault("qtsp_reference", r["qtsp_ref"] or "")
            ev["qtsp_signature_present"] = bool(r["qtsp_signature"])
            ev["qtsp_certificate_present"] = bool(r["qtsp_cert"])
        elif otp_ok:
            verification = "email one-time code"
            ev["verification_kind"] = "otp"
        else:
            verification = "declared identity + consent"
            ev["verification_kind"] = "consent"
        ev.update({
            "signed_at_utc": now, "ip": ip, "device": device,
            "user_agent": (request.headers.get("User-Agent") or "")[:300],
            "method": r["level"],
            "verification": verification,
            "document_hash": r["doc_hash"], "hash_alg": "SHA-256",
            "drawn_signature": bool(img),
        })
        conn.execute(
            "UPDATE signatures SET status='signed', signer_name=?, signer_email=COALESCE(NULLIF(?,''), signer_email), "
            "signature_img=?, otp_verified=?, ip=?, device=?, evidence_json=?, signed_at=datetime('now'), otp_code=NULL "
            "WHERE id=?",
            (name, (d.get("email") or "").strip()[:160], img, otp_ok, ip, device,
             json.dumps(ev, ensure_ascii=False), sid))

        if r["order_id"]:
            o = conn.execute("SELECT * FROM orders WHERE id=?", (r["order_id"],)).fetchone()
            if o:
                ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (o["workspace_id"],)).fetchone()
                conn.execute(
                    "INSERT INTO events (workspace_id, order_id, actor_user_id, actor_side, kind, summary, meta_json, ip, device) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (o["workspace_id"], o["id"], r["requested_by"], "system", "signature_signed",
                     "signature_signed", json.dumps({"by": name, "level": r["level"]}, ensure_ascii=False),
                     ip, device))
                for uid in org_user_ids(conn, ws["buyer_org_id"]):
                    conn.execute("INSERT INTO notifications (user_id, workspace_id, order_id, kind, body, meta_json) "
                                 "VALUES (?,?,?, 'signature_signed', 'signature_signed', ?)",
                                 (uid, o["workspace_id"], o["id"],
                                  json.dumps({"ref": o["ref"], "by": name}, ensure_ascii=False)))
        conn.commit()
        row = conn.execute("SELECT * FROM signatures WHERE id=?", (sid,)).fetchone()
        return jsonify(_sig_row(row))
    except ApiError:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Qualified signing from inside the signing page
#
#  Three routes, decided by the provider the organisation is connected to:
#    csc     the signer picks the credential, gets an OTP from the provider and
#            the hash is signed remotely - all without leaving Seam
#    native  Evrotrust / B-Trust push a confirmation to the signer's phone;
#            Seam polls until the provider returns the signature
#    bridge  the certificate is on a card or token, so the detached signature is
#            produced by the provider's own tool and returned here, where Seam
#            checks it covers exactly the hash it issued
#  These endpoints authenticate with the single-use signing token, not a session.
# --------------------------------------------------------------------------- #
def _sig_by_token(conn, token, sid=None):
    q, a = "SELECT * FROM signatures WHERE token=?", [db.token_hash(token)]
    if sid is not None:
        q += " AND id=?"
        a.append(sid)
    r = conn.execute(q, a).fetchone()
    if not r:
        raise ApiError("Невалидна заявка за подпис", 404, code="sig_invalid")
    if r["status"] == "signed":
        raise ApiError("Документът вече е подписан", 400, code="sig_already")
    if r["level"] != "qualified":
        raise ApiError("Заявката не е за квалифициран подпис", 400, code="sig_level")
    return r


def _sig_qtsp_cfg(conn, r):
    cfg = org_qtsp_config(conn, r["org_id"])
    if not qtsp.is_ready(cfg):
        raise ApiError("Не е свързан доставчик на удостоверителни услуги", 400, code="qtsp_config")
    return cfg


@app.get("/api/sign/<token>/qtsp")
def sign_qtsp_state(token):
    """What the signer has to do next, and with whom."""
    conn = db.get_db()
    try:
        r = _sig_by_token(conn, token)
        cfg = org_qtsp_config(conn, r["org_id"])
        spec = qtsp.PROVIDERS.get((cfg or {}).get("provider")) or {}
        return jsonify({
            "ready": qtsp.is_ready(cfg),
            "provider": (cfg or {}).get("provider"),
            "name": spec.get("name"), "api": spec.get("api"),
            "site": spec.get("site"), "note": spec.get("note"),
            "state": r["qtsp_state"] or "unconfigured",
            "hash": r["doc_hash"], "hash_alg": "SHA-256",
            "trusted_list": qtsp.EU_TRUSTED_LIST,
        })
    finally:
        conn.close()


@app.post("/api/sign/<token>/qtsp/credentials")
def sign_qtsp_credentials(token):
    """CSC: the qualified certificates this signer holds with the provider."""
    conn = db.get_db()
    try:
        r = _sig_by_token(conn, token)
        cfg = _sig_qtsp_cfg(conn, r)
        if cfg["adapter"] != "csc":
            raise ApiError("Този доставчик не използва CSC", 400, code="qtsp_bridge")
        if rate_limited("qtsp:" + _client_ip(), 20, 600):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        try:
            tok = qtsp.csc_token(cfg)
            ids = qtsp.csc_credentials(cfg, tok, (body().get("email") or r["signer_email"] or "").strip() or None)
            out = []
            for cid in ids[:10]:
                try:
                    info = qtsp.csc_credential_info(cfg, tok, cid)
                except qtsp.QtspError:
                    info = {}
                cert = (info.get("cert") or {})
                out.append({"id": cid, "subject": cert.get("subjectDN") or "",
                            "issuer": cert.get("issuerDN") or "",
                            "valid_to": cert.get("validTo") or "",
                            "otp": bool((info.get("OTP") or {}).get("presence") not in (None, "false"))})
            return jsonify({"credentials": out})
        except qtsp.QtspError as e:
            raise ApiError(e.message, 502, code=e.code)
    finally:
        conn.close()


@app.post("/api/sign/<token>/qtsp/otp")
def sign_qtsp_otp(token):
    """CSC: ask the provider to send its own one-time code to the signer."""
    conn = db.get_db()
    try:
        r = _sig_by_token(conn, token)
        cfg = _sig_qtsp_cfg(conn, r)
        cid = (body().get("credential_id") or "").strip()
        if not cid:
            raise ApiError("Изберете удостоверение", 400, code="field_required",
                           info={"field": "credential_id"})
        if rate_limited("qtspotp:" + _client_ip(), 10, 600):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        try:
            qtsp.csc_send_otp(cfg, qtsp.csc_token(cfg), cid)
        except qtsp.QtspError as e:
            raise ApiError(e.message, 502, code=e.code)
        return jsonify({"sent": True})
    finally:
        conn.close()


@app.post("/api/sign/<token>/qtsp/sign")
def sign_qtsp_sign(token):
    """CSC: authorise the credential and sign the hash. The document itself
    never leaves Seam - only its SHA-256 fingerprint goes to the provider."""
    conn = db.get_db()
    try:
        r = _sig_by_token(conn, token)
        cfg = _sig_qtsp_cfg(conn, r)
        d = body()
        cid = (d.get("credential_id") or "").strip()
        if not cid:
            raise ApiError("Изберете удостоверение", 400, code="field_required",
                           info={"field": "credential_id"})
        if rate_limited("qtspsign:" + _client_ip(), 10, 600):
            raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
        hashes = [qtsp._hex_to_b64(r["doc_hash"])]
        try:
            tok = qtsp.csc_token(cfg)
            sad = qtsp.csc_authorize(cfg, tok, cid, hashes,
                                     pin=(d.get("pin") or "").strip() or None,
                                     otp=(d.get("otp") or "").strip() or None)
            sig = qtsp.csc_sign_hash(cfg, tok, cid, sad, hashes)
            info = qtsp.csc_credential_info(cfg, tok, cid)
        except qtsp.QtspError as e:
            raise ApiError(e.message, 502, code=e.code)
        cert = (info.get("cert") or {}).get("certificates") or []
        conn.execute("UPDATE signatures SET qtsp_state='signed', qtsp_ref=?, qtsp_signature=?, qtsp_cert=? "
                     "WHERE id=?", (cid, sig[:20000], (cert[0] if cert else "")[:20000], r["id"]))
        conn.commit()
        return jsonify({"signed": True, "subject": (info.get("cert") or {}).get("subjectDN") or ""})
    finally:
        conn.close()


@app.post("/api/sign/<token>/qtsp/poll")
def sign_qtsp_poll(token):
    """Native async providers: has the signer confirmed on their phone yet?"""
    conn = db.get_db()
    try:
        r = _sig_by_token(conn, token)
        cfg = _sig_qtsp_cfg(conn, r)
        if not r["qtsp_ref"]:
            raise ApiError("Няма изпратена заявка към доставчика", 400, code="qtsp_noref")
        try:
            state, sig = qtsp.poll(cfg, r["qtsp_ref"])
        except qtsp.QtspError as e:
            raise ApiError(e.message, 502, code=e.code)
        if state == "signed":
            conn.execute("UPDATE signatures SET qtsp_state='signed', qtsp_signature=? WHERE id=?",
                         ((sig or "")[:20000], r["id"]))
            conn.commit()
        elif state == "rejected":
            conn.execute("UPDATE signatures SET qtsp_state='rejected' WHERE id=?", (r["id"],))
            conn.commit()
        return jsonify({"state": state})
    finally:
        conn.close()


@app.post("/api/sign/<token>/qtsp/detached")
def sign_qtsp_detached(token):
    """Bridge: the certificate is on a card or token (КЕП, e-imza, ЭЦП). The
    signer produces a detached signature over the fingerprint Seam issued with
    the provider's own tool and returns it here."""
    conn = db.get_db()
    try:
        r = _sig_by_token(conn, token)
        d = body()
        blob = (d.get("signature") or "").strip()
        if len(blob) < 64:
            raise ApiError("Липсва файл с подпис", 400, code="field_required",
                           info={"field": "signature"})
        if "," in blob[:120] and blob.startswith("data:"):
            blob = blob.split(",", 1)[1]
        signed_hash = (d.get("signed_hash") or "").strip().lower()
        if signed_hash and signed_hash != (r["doc_hash"] or "").lower():
            raise ApiError("Подписът покрива друг документ", 400, code="sig_hash_mismatch")
        conn.execute("UPDATE signatures SET qtsp_state='signed', qtsp_provider=COALESCE(qtsp_provider,?), "
                     "qtsp_signature=? WHERE id=?",
                     ((d.get("provider") or "").strip()[:40] or None, blob[:200000], r["id"]))
        conn.commit()
        return jsonify({"signed": True})
    finally:
        conn.close()


SIGN_PAGE_I18N = {
 "en": {"t": "Sign document", "doc": "Document", "hash": "Document fingerprint (SHA-256)", "lvl": "Signature level",
  "l_simple": "Simple electronic signature", "l_advanced": "Advanced (e-mail verified)", "l_qualified": "Qualified (via trust service provider)",
  "name": "Full name of the signer", "role": "Role / position", "email": "E-mail",
  "draw": "Draw your signature (optional)", "clear": "Clear", "code": "Verification code",
  "send_code": "Send code", "code_sent": "Code sent to your e-mail.",
  "consent": "I have read the document and I sign it electronically. I accept that my name, time, IP address and device are recorded as evidence.",
  "btn": "Sign", "done": "Signed", "done_p": "The signature and its evidence have been recorded.", "cert": "Signature certificate",
  "already": "This document has already been signed.", "invalid": "This signing link is not valid.",
  "ev": "Recorded evidence", "ev_when": "Signed at (UTC)", "ev_ip": "IP address", "ev_dev": "Device",
  "ev_method": "Verification",
  "qnote": "A qualified signature requires a qualified certificate from a trust service provider. Seam records the request and the evidence; the qualified certificate itself is issued by the provider.",
  "qvia": "Qualified signing via %s. The provider verifies your identity and issues the qualified certificate; Seam sends only the document fingerprint and records the evidence.",
  "ev_qtsp": "Trust service provider", "ev_qref": "Provider reference",
  "v_qtsp": "qualified certificate issued by %s", "v_otp": "one-time code sent by e-mail", "v_consent": "declared identity and consent",
  "q_title": "Qualified signature", "q_pick": "Your qualified certificate", "q_load": "Load my certificates",
  "q_pin": "Certificate PIN", "q_otp": "One-time code from the provider", "q_send": "Request code",
  "q_sign": "Sign with certificate", "q_done": "Signed with a qualified certificate.",
  "q_wait": "Waiting for your confirmation in %s.", "q_check": "Check status",
  "q_bridge": "Your certificate is on a card or token. Sign the fingerprint below with your %s software, then upload the signature file.",
  "q_upload": "Signature file", "q_copy": "Copy fingerprint", "q_copied": "Copied",
  "q_law": "Governing law", "q_reject": "The signing request was declined at the provider.",
  "q_none": "No trust service provider is connected. The signature will be recorded with full evidence, but without a qualified certificate."},

 "bg": {"t": "Подписване на документ", "doc": "Документ", "hash": "Отпечатък на документа (SHA-256)", "lvl": "Ниво на подписа",
  "l_simple": "Обикновен електронен подпис", "l_advanced": "Усъвършенстван (проверен по имейл)", "l_qualified": "Квалифициран (чрез доставчик на удостоверителни услуги)",
  "name": "Име и фамилия на подписващия", "role": "Длъжност / роля", "email": "Имейл",
  "draw": "Нарисувайте подписа си (по избор)", "clear": "Изчисти", "code": "Код за потвърждение",
  "send_code": "Изпрати код", "code_sent": "Кодът е изпратен на имейла ви.",
  "consent": "Запознах се с документа и го подписвам електронно. Приемам, че името ми, часът, IP адресът и устройството се записват като доказателство.",
  "btn": "Подпиши", "done": "Подписано", "done_p": "Подписът и доказателствата към него са записани.", "cert": "Сертификат за подпис",
  "already": "Този документ вече е подписан.", "invalid": "Този линк за подписване не е валиден.",
  "ev": "Записани доказателства", "ev_when": "Подписано на (UTC)", "ev_ip": "IP адрес", "ev_dev": "Устройство",
  "ev_method": "Проверка",
  "qnote": "Квалифицираният подпис изисква квалифицирано удостоверение от доставчик на удостоверителни услуги. Seam записва заявката и доказателствата; самото квалифицирано удостоверение се издава от доставчика.",
  "qvia": "Квалифицирано подписване чрез %s. Доставчикът проверява самоличността ви и издава квалифицираното удостоверение; Seam изпраща само отпечатъка на документа и записва доказателствата.",
  "ev_qtsp": "Доставчик на удостоверителни услуги", "ev_qref": "Референция при доставчика",
  "v_qtsp": "квалифицирано удостоверение, издадено от %s", "v_otp": "еднократен код, изпратен по имейл", "v_consent": "декларирана самоличност и съгласие",
  "q_title": "Квалифициран подпис", "q_pick": "Вашето квалифицирано удостоверение", "q_load": "Зареди моите удостоверения",
  "q_pin": "ПИН на удостоверението", "q_otp": "Еднократен код от доставчика", "q_send": "Заяви код",
  "q_sign": "Подпиши с удостоверение", "q_done": "Подписано с квалифицирано удостоверение.",
  "q_wait": "Изчаква се потвърждение от ваша страна в %s.", "q_check": "Провери статуса",
  "q_bridge": "Удостоверението ви е на карта или токен. Подпишете отпечатъка по-долу със софтуера на %s и прикачете файла с подписа.",
  "q_upload": "Файл с подпис", "q_copy": "Копирай отпечатъка", "q_copied": "Копирано",
  "q_law": "Приложимо право", "q_reject": "Заявката за подпис е отказана при доставчика.",
  "q_none": "Няма свързан доставчик на удостоверителни услуги. Подписът ще бъде записан с пълни доказателства, но без квалифицирано удостоверение."},

 "de": {"t": "Dokument unterschreiben", "doc": "Dokument", "hash": "Dokument-Fingerabdruck (SHA-256)", "lvl": "Signaturniveau",
  "l_simple": "Einfache elektronische Signatur", "l_advanced": "Fortgeschritten (per E-Mail bestätigt)", "l_qualified": "Qualifiziert (über Vertrauensdiensteanbieter)",
  "name": "Vor- und Nachname der unterzeichnenden Person", "role": "Funktion / Position", "email": "E-Mail",
  "draw": "Unterschrift zeichnen (optional)", "clear": "Löschen", "code": "Bestätigungscode",
  "send_code": "Code senden", "code_sent": "Der Code wurde an Ihre E-Mail-Adresse gesendet.",
  "consent": "Ich habe das Dokument gelesen und unterschreibe es elektronisch. Ich akzeptiere, dass mein Name, die Uhrzeit, die IP-Adresse und das Gerät als Nachweis erfasst werden.",
  "btn": "Unterschreiben", "done": "Unterschrieben", "done_p": "Die Signatur und ihre Nachweise wurden erfasst.", "cert": "Signaturzertifikat",
  "already": "Dieses Dokument wurde bereits unterschrieben.", "invalid": "Dieser Signaturlink ist ungültig.",
  "ev": "Erfasste Nachweise", "ev_when": "Unterschrieben am (UTC)", "ev_ip": "IP-Adresse", "ev_dev": "Gerät",
  "ev_method": "Überprüfung",
  "qnote": "Eine qualifizierte Signatur erfordert ein qualifiziertes Zertifikat eines Vertrauensdiensteanbieters. Seam erfasst die Anfrage und die Nachweise; das qualifizierte Zertifikat selbst wird vom Anbieter ausgestellt.",
  "qvia": "Qualifizierte Signatur über %s. Der Anbieter prüft Ihre Identität und stellt das qualifizierte Zertifikat aus; Seam übermittelt nur den Fingerabdruck des Dokuments und erfasst die Nachweise.",
  "ev_qtsp": "Vertrauensdiensteanbieter", "ev_qref": "Referenz beim Anbieter",
  "v_qtsp": "qualifiziertes Zertifikat, ausgestellt von %s", "v_otp": "Einmalcode per E-Mail", "v_consent": "erklärte Identität und Zustimmung",
  "q_title": "Qualifizierte Signatur", "q_pick": "Ihr qualifiziertes Zertifikat", "q_load": "Meine Zertifikate laden",
  "q_pin": "PIN des Zertifikats", "q_otp": "Einmalcode des Anbieters", "q_send": "Code anfordern",
  "q_sign": "Mit Zertifikat unterschreiben", "q_done": "Mit einem qualifizierten Zertifikat unterschrieben.",
  "q_wait": "Warten auf Ihre Bestätigung in %s.", "q_check": "Status prüfen",
  "q_bridge": "Ihr Zertifikat befindet sich auf einer Karte oder einem Token. Unterschreiben Sie den unten stehenden Fingerabdruck mit Ihrer %s-Software und laden Sie die Signaturdatei hoch.",
  "q_upload": "Signaturdatei", "q_copy": "Fingerabdruck kopieren", "q_copied": "Kopiert",
  "q_law": "Anwendbares Recht", "q_reject": "Die Signaturanfrage wurde beim Anbieter abgelehnt.",
  "q_none": "Es ist kein Vertrauensdiensteanbieter verbunden. Die Signatur wird mit vollständigen Nachweisen erfasst, jedoch ohne qualifiziertes Zertifikat."},

 "ro": {"t": "Semnarea documentului", "doc": "Document", "hash": "Amprenta documentului (SHA-256)", "lvl": "Nivelul semnăturii",
  "l_simple": "Semnătură electronică simplă", "l_advanced": "Avansată (verificată prin e-mail)", "l_qualified": "Calificată (prin prestator de servicii de încredere)",
  "name": "Numele și prenumele semnatarului", "role": "Funcția / rolul", "email": "E-mail",
  "draw": "Desenați semnătura (opțional)", "clear": "Șterge", "code": "Cod de verificare",
  "send_code": "Trimite codul", "code_sent": "Codul a fost trimis pe e-mailul dumneavoastră.",
  "consent": "Am citit documentul și îl semnez electronic. Accept ca numele meu, ora, adresa IP și dispozitivul să fie înregistrate ca dovadă.",
  "btn": "Semnează", "done": "Semnat", "done_p": "Semnătura și dovezile aferente au fost înregistrate.", "cert": "Certificat de semnătură",
  "already": "Acest document a fost deja semnat.", "invalid": "Acest link de semnare nu este valid.",
  "ev": "Dovezi înregistrate", "ev_when": "Semnat la (UTC)", "ev_ip": "Adresă IP", "ev_dev": "Dispozitiv",
  "ev_method": "Verificare",
  "qnote": "O semnătură calificată necesită un certificat calificat de la un prestator de servicii de încredere. Seam înregistrează cererea și dovezile; certificatul calificat este emis de prestator.",
  "qvia": "Semnare calificată prin %s. Prestatorul vă verifică identitatea și emite certificatul calificat; Seam transmite doar amprenta documentului și înregistrează dovezile.",
  "ev_qtsp": "Prestator de servicii de încredere", "ev_qref": "Referință la prestator",
  "v_qtsp": "certificat calificat emis de %s", "v_otp": "cod unic trimis prin e-mail", "v_consent": "identitate declarată și consimțământ",
  "q_title": "Semnătură calificată", "q_pick": "Certificatul dumneavoastră calificat", "q_load": "Încarcă certificatele mele",
  "q_pin": "PIN-ul certificatului", "q_otp": "Cod unic de la prestator", "q_send": "Solicită cod",
  "q_sign": "Semnează cu certificatul", "q_done": "Semnat cu un certificat calificat.",
  "q_wait": "Se așteaptă confirmarea dumneavoastră în %s.", "q_check": "Verifică starea",
  "q_bridge": "Certificatul se află pe un card sau token. Semnați amprenta de mai jos cu software-ul %s și încărcați fișierul cu semnătura.",
  "q_upload": "Fișierul semnăturii", "q_copy": "Copiază amprenta", "q_copied": "Copiat",
  "q_law": "Legea aplicabilă", "q_reject": "Cererea de semnare a fost respinsă la prestator.",
  "q_none": "Niciun prestator de servicii de încredere nu este conectat. Semnătura va fi înregistrată cu dovezi complete, dar fără certificat calificat."},

 "el": {"t": "Υπογραφή εγγράφου", "doc": "Έγγραφο", "hash": "Αποτύπωμα εγγράφου (SHA-256)", "lvl": "Επίπεδο υπογραφής",
  "l_simple": "Απλή ηλεκτρονική υπογραφή", "l_advanced": "Προηγμένη (επαληθευμένη με e-mail)", "l_qualified": "Εγκεκριμένη (μέσω παρόχου υπηρεσιών εμπιστοσύνης)",
  "name": "Ονοματεπώνυμο υπογράφοντος", "role": "Θέση / ρόλος", "email": "E-mail",
  "draw": "Σχεδιάστε την υπογραφή σας (προαιρετικό)", "clear": "Καθαρισμός", "code": "Κωδικός επαλήθευσης",
  "send_code": "Αποστολή κωδικού", "code_sent": "Ο κωδικός εστάλη στο e-mail σας.",
  "consent": "Έλαβα γνώση του εγγράφου και το υπογράφω ηλεκτρονικά. Αποδέχομαι ότι το όνομά μου, η ώρα, η διεύθυνση IP και η συσκευή καταγράφονται ως αποδεικτικά στοιχεία.",
  "btn": "Υπογραφή", "done": "Υπογράφηκε", "done_p": "Η υπογραφή και τα αποδεικτικά στοιχεία καταγράφηκαν.", "cert": "Πιστοποιητικό υπογραφής",
  "already": "Το έγγραφο έχει ήδη υπογραφεί.", "invalid": "Ο σύνδεσμος υπογραφής δεν είναι έγκυρος.",
  "ev": "Καταγεγραμμένα στοιχεία", "ev_when": "Υπογραφή στις (UTC)", "ev_ip": "Διεύθυνση IP", "ev_dev": "Συσκευή",
  "ev_method": "Επαλήθευση",
  "qnote": "Η εγκεκριμένη υπογραφή απαιτεί εγκεκριμένο πιστοποιητικό από πάροχο υπηρεσιών εμπιστοσύνης. Το Seam καταγράφει το αίτημα και τα αποδεικτικά στοιχεία· το εγκεκριμένο πιστοποιητικό εκδίδεται από τον πάροχο.",
  "qvia": "Εγκεκριμένη υπογραφή μέσω %s. Ο πάροχος επαληθεύει την ταυτότητά σας και εκδίδει το εγκεκριμένο πιστοποιητικό· το Seam αποστέλλει μόνο το αποτύπωμα του εγγράφου και καταγράφει τα αποδεικτικά στοιχεία.",
  "ev_qtsp": "Πάροχος υπηρεσιών εμπιστοσύνης", "ev_qref": "Αναφορά παρόχου",
  "v_qtsp": "εγκεκριμένο πιστοποιητικό που εκδόθηκε από %s", "v_otp": "κωδικός μίας χρήσης μέσω e-mail", "v_consent": "δηλωθείσα ταυτότητα και συγκατάθεση",
  "q_title": "Εγκεκριμένη υπογραφή", "q_pick": "Το εγκεκριμένο πιστοποιητικό σας", "q_load": "Φόρτωση των πιστοποιητικών μου",
  "q_pin": "PIN πιστοποιητικού", "q_otp": "Κωδικός μίας χρήσης από τον πάροχο", "q_send": "Αίτημα κωδικού",
  "q_sign": "Υπογραφή με πιστοποιητικό", "q_done": "Υπογράφηκε με εγκεκριμένο πιστοποιητικό.",
  "q_wait": "Αναμονή για την επιβεβαίωσή σας στο %s.", "q_check": "Έλεγχος κατάστασης",
  "q_bridge": "Το πιστοποιητικό σας βρίσκεται σε κάρτα ή token. Υπογράψτε το παρακάτω αποτύπωμα με το λογισμικό %s και ανεβάστε το αρχείο υπογραφής.",
  "q_upload": "Αρχείο υπογραφής", "q_copy": "Αντιγραφή αποτυπώματος", "q_copied": "Αντιγράφηκε",
  "q_law": "Εφαρμοστέο δίκαιο", "q_reject": "Το αίτημα υπογραφής απορρίφθηκε στον πάροχο.",
  "q_none": "Δεν έχει συνδεθεί πάροχος υπηρεσιών εμπιστοσύνης. Η υπογραφή θα καταγραφεί με πλήρη αποδεικτικά στοιχεία, αλλά χωρίς εγκεκριμένο πιστοποιητικό."},

 "tr": {"t": "Belgeyi imzala", "doc": "Belge", "hash": "Belge parmak izi (SHA-256)", "lvl": "İmza düzeyi",
  "l_simple": "Basit elektronik imza", "l_advanced": "Gelişmiş (e-posta ile doğrulanmış)", "l_qualified": "Nitelikli (güven hizmeti sağlayıcısı aracılığıyla)",
  "name": "İmzalayanın adı ve soyadı", "role": "Görev / unvan", "email": "E-posta",
  "draw": "İmzanızı çizin (isteğe bağlı)", "clear": "Temizle", "code": "Doğrulama kodu",
  "send_code": "Kod gönder", "code_sent": "Kod e-posta adresinize gönderildi.",
  "consent": "Belgeyi okudum ve elektronik olarak imzalıyorum. Adımın, saatin, IP adresimin ve cihazımın delil olarak kaydedilmesini kabul ediyorum.",
  "btn": "İmzala", "done": "İmzalandı", "done_p": "İmza ve delilleri kaydedildi.", "cert": "İmza sertifikası",
  "already": "Bu belge zaten imzalanmış.", "invalid": "Bu imzalama bağlantısı geçerli değil.",
  "ev": "Kaydedilen deliller", "ev_when": "İmzalanma zamanı (UTC)", "ev_ip": "IP adresi", "ev_dev": "Cihaz",
  "ev_method": "Doğrulama",
  "qnote": "Nitelikli imza, bir güven hizmeti sağlayıcısından alınan nitelikli sertifika gerektirir. Seam talebi ve delilleri kaydeder; nitelikli sertifikayı sağlayıcı düzenler.",
  "qvia": "%s aracılığıyla nitelikli imzalama. Sağlayıcı kimliğinizi doğrular ve nitelikli sertifikayı düzenler; Seam yalnızca belgenin parmak izini iletir ve delilleri kaydeder.",
  "ev_qtsp": "Güven hizmeti sağlayıcısı", "ev_qref": "Sağlayıcı referansı",
  "v_qtsp": "%s tarafından düzenlenen nitelikli sertifika", "v_otp": "e-posta ile gönderilen tek kullanımlık kod", "v_consent": "beyan edilen kimlik ve onay",
  "q_title": "Nitelikli imza", "q_pick": "Nitelikli sertifikanız", "q_load": "Sertifikalarımı yükle",
  "q_pin": "Sertifika PIN kodu", "q_otp": "Sağlayıcıdan gelen tek kullanımlık kod", "q_send": "Kod iste",
  "q_sign": "Sertifika ile imzala", "q_done": "Nitelikli sertifika ile imzalandı.",
  "q_wait": "%s uygulamasında onayınız bekleniyor.", "q_check": "Durumu kontrol et",
  "q_bridge": "Sertifikanız bir kart veya token üzerindedir. Aşağıdaki parmak izini %s yazılımıyla imzalayın ve imza dosyasını yükleyin.",
  "q_upload": "İmza dosyası", "q_copy": "Parmak izini kopyala", "q_copied": "Kopyalandı",
  "q_law": "Uygulanacak hukuk", "q_reject": "İmza talebi sağlayıcı tarafında reddedildi.",
  "q_none": "Bağlı bir güven hizmeti sağlayıcısı yok. İmza eksiksiz delillerle kaydedilecek, ancak nitelikli sertifika olmadan."},

 "it": {"t": "Firma del documento", "doc": "Documento", "hash": "Impronta del documento (SHA-256)", "lvl": "Livello della firma",
  "l_simple": "Firma elettronica semplice", "l_advanced": "Avanzata (verificata via e-mail)", "l_qualified": "Qualificata (tramite prestatore di servizi fiduciari)",
  "name": "Nome e cognome del firmatario", "role": "Ruolo / posizione", "email": "E-mail",
  "draw": "Disegni la sua firma (facoltativo)", "clear": "Cancella", "code": "Codice di verifica",
  "send_code": "Invia codice", "code_sent": "Il codice è stato inviato al suo indirizzo e-mail.",
  "consent": "Ho letto il documento e lo firmo elettronicamente. Accetto che il mio nome, l'ora, l'indirizzo IP e il dispositivo siano registrati come prova.",
  "btn": "Firma", "done": "Firmato", "done_p": "La firma e le relative prove sono state registrate.", "cert": "Certificato di firma",
  "already": "Questo documento è già stato firmato.", "invalid": "Questo link di firma non è valido.",
  "ev": "Prove registrate", "ev_when": "Firmato il (UTC)", "ev_ip": "Indirizzo IP", "ev_dev": "Dispositivo",
  "ev_method": "Verifica",
  "qnote": "Una firma qualificata richiede un certificato qualificato rilasciato da un prestatore di servizi fiduciari. Seam registra la richiesta e le prove; il certificato qualificato è rilasciato dal prestatore.",
  "qvia": "Firma qualificata tramite %s. Il prestatore verifica la sua identità e rilascia il certificato qualificato; Seam trasmette solo l'impronta del documento e registra le prove.",
  "ev_qtsp": "Prestatore di servizi fiduciari", "ev_qref": "Riferimento del prestatore",
  "v_qtsp": "certificato qualificato rilasciato da %s", "v_otp": "codice monouso inviato via e-mail", "v_consent": "identità dichiarata e consenso",
  "q_title": "Firma qualificata", "q_pick": "Il suo certificato qualificato", "q_load": "Carica i miei certificati",
  "q_pin": "PIN del certificato", "q_otp": "Codice monouso del prestatore", "q_send": "Richiedi codice",
  "q_sign": "Firma con certificato", "q_done": "Firmato con un certificato qualificato.",
  "q_wait": "In attesa della sua conferma in %s.", "q_check": "Verifica stato",
  "q_bridge": "Il suo certificato si trova su una smart card o un token. Firmi l'impronta qui sotto con il software %s e carichi il file della firma.",
  "q_upload": "File della firma", "q_copy": "Copia impronta", "q_copied": "Copiato",
  "q_law": "Legge applicabile", "q_reject": "La richiesta di firma è stata rifiutata presso il prestatore.",
  "q_none": "Nessun prestatore di servizi fiduciari è collegato. La firma sarà registrata con prove complete, ma senza certificato qualificato."},

 "ru": {"t": "Подписание документа", "doc": "Документ", "hash": "Отпечаток документа (SHA-256)", "lvl": "Вид подписи",
  "l_simple": "Простая электронная подпись", "l_advanced": "Усиленная (подтверждённая по электронной почте)", "l_qualified": "Квалифицированная (через удостоверяющий центр)",
  "name": "Фамилия и имя подписанта", "role": "Должность / роль", "email": "Электронная почта",
  "draw": "Нарисуйте подпись (по желанию)", "clear": "Очистить", "code": "Код подтверждения",
  "send_code": "Отправить код", "code_sent": "Код отправлен на вашу электронную почту.",
  "consent": "Я ознакомился с документом и подписываю его электронно. Я согласен, что моё имя, время, IP-адрес и устройство фиксируются в качестве доказательства.",
  "btn": "Подписать", "done": "Подписано", "done_p": "Подпись и доказательства зафиксированы.", "cert": "Сертификат подписи",
  "already": "Этот документ уже подписан.", "invalid": "Эта ссылка для подписания недействительна.",
  "ev": "Зафиксированные доказательства", "ev_when": "Подписано (UTC)", "ev_ip": "IP-адрес", "ev_dev": "Устройство",
  "ev_method": "Проверка",
  "qnote": "Квалифицированная подпись требует квалифицированного сертификата удостоверяющего центра. Seam фиксирует заявку и доказательства; сам квалифицированный сертификат выдаёт удостоверяющий центр.",
  "qvia": "Квалифицированное подписание через %s. Удостоверяющий центр проверяет вашу личность и выдаёт квалифицированный сертификат; Seam передаёт только отпечаток документа и фиксирует доказательства.",
  "ev_qtsp": "Удостоверяющий центр", "ev_qref": "Ссылка удостоверяющего центра",
  "v_qtsp": "квалифицированный сертификат, выданный %s", "v_otp": "одноразовый код, отправленный по электронной почте", "v_consent": "заявленная личность и согласие",
  "q_title": "Квалифицированная подпись", "q_pick": "Ваш квалифицированный сертификат", "q_load": "Загрузить мои сертификаты",
  "q_pin": "ПИН-код сертификата", "q_otp": "Одноразовый код удостоверяющего центра", "q_send": "Запросить код",
  "q_sign": "Подписать сертификатом", "q_done": "Подписано квалифицированным сертификатом.",
  "q_wait": "Ожидается ваше подтверждение в %s.", "q_check": "Проверить статус",
  "q_bridge": "Ваш сертификат находится на карте или токене. Подпишите отпечаток ниже с помощью программы %s и загрузите файл подписи.",
  "q_upload": "Файл подписи", "q_copy": "Копировать отпечаток", "q_copied": "Скопировано",
  "q_law": "Применимое право", "q_reject": "Заявка на подписание отклонена удостоверяющим центром.",
  "q_none": "Удостоверяющий центр не подключён. Подпись будет зафиксирована с полными доказательствами, но без квалифицированного сертификата."},

 "es": {"t": "Firma del documento", "doc": "Documento", "hash": "Huella del documento (SHA-256)", "lvl": "Nivel de la firma",
  "l_simple": "Firma electrónica simple", "l_advanced": "Avanzada (verificada por correo electrónico)", "l_qualified": "Cualificada (a través de un prestador de servicios de confianza)",
  "name": "Nombre y apellidos del firmante", "role": "Cargo / función", "email": "Correo electrónico",
  "draw": "Dibuje su firma (opcional)", "clear": "Borrar", "code": "Código de verificación",
  "send_code": "Enviar código", "code_sent": "El código se ha enviado a su correo electrónico.",
  "consent": "He leído el documento y lo firmo electrónicamente. Acepto que mi nombre, la hora, la dirección IP y el dispositivo queden registrados como prueba.",
  "btn": "Firmar", "done": "Firmado", "done_p": "La firma y sus pruebas han quedado registradas.", "cert": "Certificado de firma",
  "already": "Este documento ya ha sido firmado.", "invalid": "Este enlace de firma no es válido.",
  "ev": "Pruebas registradas", "ev_when": "Firmado el (UTC)", "ev_ip": "Dirección IP", "ev_dev": "Dispositivo",
  "ev_method": "Verificación",
  "qnote": "Una firma cualificada requiere un certificado cualificado de un prestador de servicios de confianza. Seam registra la solicitud y las pruebas; el certificado cualificado lo emite el prestador.",
  "qvia": "Firma cualificada a través de %s. El prestador verifica su identidad y emite el certificado cualificado; Seam solo transmite la huella del documento y registra las pruebas.",
  "ev_qtsp": "Prestador de servicios de confianza", "ev_qref": "Referencia del prestador",
  "v_qtsp": "certificado cualificado emitido por %s", "v_otp": "código de un solo uso enviado por correo electrónico", "v_consent": "identidad declarada y consentimiento",
  "q_title": "Firma cualificada", "q_pick": "Su certificado cualificado", "q_load": "Cargar mis certificados",
  "q_pin": "PIN del certificado", "q_otp": "Código de un solo uso del prestador", "q_send": "Solicitar código",
  "q_sign": "Firmar con certificado", "q_done": "Firmado con un certificado cualificado.",
  "q_wait": "A la espera de su confirmación en %s.", "q_check": "Comprobar estado",
  "q_bridge": "Su certificado está en una tarjeta o token. Firme la huella siguiente con el software de %s y suba el archivo de firma.",
  "q_upload": "Archivo de firma", "q_copy": "Copiar huella", "q_copied": "Copiado",
  "q_law": "Legislación aplicable", "q_reject": "La solicitud de firma ha sido rechazada en el prestador.",
  "q_none": "No hay ningún prestador de servicios de confianza conectado. La firma se registrará con pruebas completas, pero sin certificado cualificado."},

 "fr": {"t": "Signature du document", "doc": "Document", "hash": "Empreinte du document (SHA-256)", "lvl": "Niveau de signature",
  "l_simple": "Signature électronique simple", "l_advanced": "Avancée (vérifiée par e-mail)", "l_qualified": "Qualifiée (via un prestataire de services de confiance)",
  "name": "Nom et prénom du signataire", "role": "Fonction / rôle", "email": "E-mail",
  "draw": "Dessinez votre signature (facultatif)", "clear": "Effacer", "code": "Code de vérification",
  "send_code": "Envoyer le code", "code_sent": "Le code a été envoyé à votre adresse e-mail.",
  "consent": "J'ai pris connaissance du document et je le signe électroniquement. J'accepte que mon nom, l'heure, l'adresse IP et l'appareil soient enregistrés à titre de preuve.",
  "btn": "Signer", "done": "Signé", "done_p": "La signature et ses preuves ont été enregistrées.", "cert": "Certificat de signature",
  "already": "Ce document a déjà été signé.", "invalid": "Ce lien de signature n'est pas valide.",
  "ev": "Preuves enregistrées", "ev_when": "Signé le (UTC)", "ev_ip": "Adresse IP", "ev_dev": "Appareil",
  "ev_method": "Vérification",
  "qnote": "Une signature qualifiée nécessite un certificat qualifié délivré par un prestataire de services de confiance. Seam enregistre la demande et les preuves ; le certificat qualifié est délivré par le prestataire.",
  "qvia": "Signature qualifiée via %s. Le prestataire vérifie votre identité et délivre le certificat qualifié ; Seam ne transmet que l'empreinte du document et enregistre les preuves.",
  "ev_qtsp": "Prestataire de services de confiance", "ev_qref": "Référence du prestataire",
  "v_qtsp": "certificat qualifié délivré par %s", "v_otp": "code à usage unique envoyé par e-mail", "v_consent": "identité déclarée et consentement",
  "q_title": "Signature qualifiée", "q_pick": "Votre certificat qualifié", "q_load": "Charger mes certificats",
  "q_pin": "Code PIN du certificat", "q_otp": "Code à usage unique du prestataire", "q_send": "Demander un code",
  "q_sign": "Signer avec le certificat", "q_done": "Signé avec un certificat qualifié.",
  "q_wait": "En attente de votre confirmation dans %s.", "q_check": "Vérifier le statut",
  "q_bridge": "Votre certificat se trouve sur une carte ou un token. Signez l'empreinte ci-dessous avec le logiciel %s, puis téléversez le fichier de signature.",
  "q_upload": "Fichier de signature", "q_copy": "Copier l'empreinte", "q_copied": "Copié",
  "q_law": "Droit applicable", "q_reject": "La demande de signature a été refusée chez le prestataire.",
  "q_none": "Aucun prestataire de services de confiance n'est connecté. La signature sera enregistrée avec des preuves complètes, mais sans certificat qualifié."},

 "pl": {"t": "Podpisanie dokumentu", "doc": "Dokument", "hash": "Odcisk dokumentu (SHA-256)", "lvl": "Poziom podpisu",
  "l_simple": "Zwykły podpis elektroniczny", "l_advanced": "Zaawansowany (zweryfikowany e-mailem)", "l_qualified": "Kwalifikowany (przez dostawcę usług zaufania)",
  "name": "Imię i nazwisko podpisującego", "role": "Stanowisko / rola", "email": "E-mail",
  "draw": "Narysuj swój podpis (opcjonalnie)", "clear": "Wyczyść", "code": "Kod weryfikacyjny",
  "send_code": "Wyślij kod", "code_sent": "Kod został wysłany na Państwa adres e-mail.",
  "consent": "Zapoznałem się z dokumentem i podpisuję go elektronicznie. Akceptuję, że moje imię i nazwisko, czas, adres IP oraz urządzenie zostaną zapisane jako dowód.",
  "btn": "Podpisz", "done": "Podpisano", "done_p": "Podpis i dowody zostały zapisane.", "cert": "Certyfikat podpisu",
  "already": "Ten dokument został już podpisany.", "invalid": "Ten link do podpisu jest nieprawidłowy.",
  "ev": "Zapisane dowody", "ev_when": "Podpisano (UTC)", "ev_ip": "Adres IP", "ev_dev": "Urządzenie",
  "ev_method": "Weryfikacja",
  "qnote": "Podpis kwalifikowany wymaga kwalifikowanego certyfikatu od dostawcy usług zaufania. Seam zapisuje wniosek i dowody; sam certyfikat kwalifikowany wydaje dostawca.",
  "qvia": "Podpis kwalifikowany przez %s. Dostawca weryfikuje Państwa tożsamość i wydaje certyfikat kwalifikowany; Seam przekazuje wyłącznie odcisk dokumentu i zapisuje dowody.",
  "ev_qtsp": "Dostawca usług zaufania", "ev_qref": "Referencja u dostawcy",
  "v_qtsp": "certyfikat kwalifikowany wydany przez %s", "v_otp": "kod jednorazowy wysłany e-mailem", "v_consent": "zadeklarowana tożsamość i zgoda",
  "q_title": "Podpis kwalifikowany", "q_pick": "Państwa certyfikat kwalifikowany", "q_load": "Wczytaj moje certyfikaty",
  "q_pin": "PIN certyfikatu", "q_otp": "Kod jednorazowy od dostawcy", "q_send": "Poproś o kod",
  "q_sign": "Podpisz certyfikatem", "q_done": "Podpisano certyfikatem kwalifikowanym.",
  "q_wait": "Oczekiwanie na Państwa potwierdzenie w %s.", "q_check": "Sprawdź status",
  "q_bridge": "Państwa certyfikat znajduje się na karcie lub tokenie. Proszę podpisać poniższy odcisk oprogramowaniem %s i przesłać plik podpisu.",
  "q_upload": "Plik podpisu", "q_copy": "Kopiuj odcisk", "q_copied": "Skopiowano",
  "q_law": "Prawo właściwe", "q_reject": "Wniosek o podpis został odrzucony u dostawcy.",
  "q_none": "Nie podłączono dostawcy usług zaufania. Podpis zostanie zapisany z pełnymi dowodami, ale bez certyfikatu kwalifikowanego."},

 "uk": {"t": "Підписання документа", "doc": "Документ", "hash": "Відбиток документа (SHA-256)", "lvl": "Рівень підпису",
  "l_simple": "Простий електронний підпис", "l_advanced": "Удосконалений (підтверджений електронною поштою)", "l_qualified": "Кваліфікований (через надавача електронних довірчих послуг)",
  "name": "Прізвище та ім'я підписувача", "role": "Посада / роль", "email": "Електронна пошта",
  "draw": "Намалюйте підпис (за бажанням)", "clear": "Очистити", "code": "Код підтвердження",
  "send_code": "Надіслати код", "code_sent": "Код надіслано на вашу електронну пошту.",
  "consent": "Я ознайомився з документом і підписую його електронно. Погоджуюсь, що моє ім'я, час, IP-адреса та пристрій фіксуються як доказ.",
  "btn": "Підписати", "done": "Підписано", "done_p": "Підпис і докази зафіксовано.", "cert": "Сертифікат підпису",
  "already": "Цей документ уже підписано.", "invalid": "Це посилання для підписання недійсне.",
  "ev": "Зафіксовані докази", "ev_when": "Підписано (UTC)", "ev_ip": "IP-адреса", "ev_dev": "Пристрій",
  "ev_method": "Перевірка",
  "qnote": "Кваліфікований підпис потребує кваліфікованого сертифіката від надавача електронних довірчих послуг. Seam фіксує заявку та докази; сам кваліфікований сертифікат видає надавач.",
  "qvia": "Кваліфіковане підписання через %s. Надавач перевіряє вашу особу та видає кваліфікований сертифікат; Seam передає лише відбиток документа і фіксує докази.",
  "ev_qtsp": "Надавач електронних довірчих послуг", "ev_qref": "Референс надавача",
  "v_qtsp": "кваліфікований сертифікат, виданий %s", "v_otp": "одноразовий код, надісланий електронною поштою", "v_consent": "заявлена особа та згода",
  "q_title": "Кваліфікований підпис", "q_pick": "Ваш кваліфікований сертифікат", "q_load": "Завантажити мої сертифікати",
  "q_pin": "ПІН сертифіката", "q_otp": "Одноразовий код від надавача", "q_send": "Запросити код",
  "q_sign": "Підписати сертифікатом", "q_done": "Підписано кваліфікованим сертифікатом.",
  "q_wait": "Очікується ваше підтвердження в %s.", "q_check": "Перевірити статус",
  "q_bridge": "Ваш сертифікат розміщено на картці або токені. Підпишіть відбиток нижче програмою %s і завантажте файл підпису.",
  "q_upload": "Файл підпису", "q_copy": "Копіювати відбиток", "q_copied": "Скопійовано",
  "q_law": "Застосовне право", "q_reject": "Заявку на підписання відхилено надавачем.",
  "q_none": "Надавача електронних довірчих послуг не підключено. Підпис буде зафіксовано з повними доказами, але без кваліфікованого сертифіката."},

 "pt": {"t": "Assinatura do documento", "doc": "Documento", "hash": "Impressão digital do documento (SHA-256)", "lvl": "Nível da assinatura",
  "l_simple": "Assinatura eletrónica simples", "l_advanced": "Avançada (verificada por e-mail)", "l_qualified": "Qualificada (através de prestador de serviços de confiança)",
  "name": "Nome completo do signatário", "role": "Cargo / função", "email": "E-mail",
  "draw": "Desenhe a sua assinatura (opcional)", "clear": "Limpar", "code": "Código de verificação",
  "send_code": "Enviar código", "code_sent": "O código foi enviado para o seu e-mail.",
  "consent": "Li o documento e assino-o eletronicamente. Aceito que o meu nome, a hora, o endereço IP e o dispositivo sejam registados como prova.",
  "btn": "Assinar", "done": "Assinado", "done_p": "A assinatura e as respetivas provas foram registadas.", "cert": "Certificado de assinatura",
  "already": "Este documento já foi assinado.", "invalid": "Esta ligação de assinatura não é válida.",
  "ev": "Provas registadas", "ev_when": "Assinado em (UTC)", "ev_ip": "Endereço IP", "ev_dev": "Dispositivo",
  "ev_method": "Verificação",
  "qnote": "Uma assinatura qualificada exige um certificado qualificado de um prestador de serviços de confiança. O Seam regista o pedido e as provas; o certificado qualificado é emitido pelo prestador.",
  "qvia": "Assinatura qualificada através de %s. O prestador verifica a sua identidade e emite o certificado qualificado; o Seam transmite apenas a impressão digital do documento e regista as provas.",
  "ev_qtsp": "Prestador de serviços de confiança", "ev_qref": "Referência do prestador",
  "v_qtsp": "certificado qualificado emitido por %s", "v_otp": "código de utilização única enviado por e-mail", "v_consent": "identidade declarada e consentimento",
  "q_title": "Assinatura qualificada", "q_pick": "O seu certificado qualificado", "q_load": "Carregar os meus certificados",
  "q_pin": "PIN do certificado", "q_otp": "Código de utilização única do prestador", "q_send": "Pedir código",
  "q_sign": "Assinar com certificado", "q_done": "Assinado com um certificado qualificado.",
  "q_wait": "A aguardar a sua confirmação em %s.", "q_check": "Verificar estado",
  "q_bridge": "O seu certificado está num cartão ou token. Assine a impressão digital abaixo com o software %s e carregue o ficheiro da assinatura.",
  "q_upload": "Ficheiro da assinatura", "q_copy": "Copiar impressão digital", "q_copied": "Copiado",
  "q_law": "Lei aplicável", "q_reject": "O pedido de assinatura foi recusado no prestador.",
  "q_none": "Não existe qualquer prestador de serviços de confiança ligado. A assinatura será registada com provas completas, mas sem certificado qualificado."},
}


def _sign_qnote(r, L):
    """Qualified level: name the actual provider when one is wired in, otherwise
    stay honest that no qualified certificate stands behind the signature."""
    if r["level"] != "qualified":
        return ""
    try:
        ev = json.loads(r["evidence_json"] or "{}")
    except Exception:
        ev = {}
    out = ""
    if ev.get("qtsp"):
        out = '<div class="qn ok">%s</div>' % html_escape(L["qvia"] % ev.get("qtsp_name", ""))
    else:
        out = '<div class="qn">%s</div>' % html_escape(L["q_none"])
    legal = ev.get("legal") or {}
    if legal.get("law"):
        out += ('<div class="law"><b>%s:</b> %s · %s</div>'
                % (html_escape(L["q_law"]), html_escape(legal["law"]),
                   html_escape(legal.get("supervisor") or "")))
    return out


def _sign_qpanel(conn, r, L):
    """The in-platform qualified signing panel. What it offers depends on how
    the signer's certificate is actually held: remotely at a CSC provider, in
    the provider's mobile app, or on a card the signer carries."""
    if r["level"] != "qualified":
        return ""
    cfg = org_qtsp_config(conn, r["org_id"])
    if not qtsp.is_ready(cfg):
        return ""
    spec = qtsp.PROVIDERS[cfg["provider"]]
    api, name = spec["api"], spec["name"]
    head = ('<div class="qbox"><div class="qh">%s · %s</div>'
            % (html_escape(L["q_title"]), html_escape(name)))

    if api == "csc":
        return head + ("""
 <button type="button" class="qbtn" id="q_load">%(load)s</button>
 <div id="q_creds" class="qhide">
   <label>%(pick)s</label><select id="q_cid"></select>
   <label>%(pin)s</label><input id="q_pin" type="password" autocomplete="off" maxlength="40">
   <label>%(otp)s</label>
   <div class="otprow"><input id="q_otp" maxlength="12" inputmode="numeric" autocomplete="off">
     <button type="button" id="q_send">%(send)s</button></div>
   <button type="button" class="qbtn go" id="q_sign">%(sign)s</button>
 </div>
 <div id="q_state" class="qstate"></div></div>""" % {
            "load": html_escape(L["q_load"]), "pick": html_escape(L["q_pick"]),
            "pin": html_escape(L["q_pin"]), "otp": html_escape(L["q_otp"]),
            "send": html_escape(L["q_send"]), "sign": html_escape(L["q_sign"])})

    if api == "native":
        return head + ('<div class="qstate" id="q_state">%s</div>'
                       '<button type="button" class="qbtn" id="q_poll">%s</button></div>'
                       % (html_escape(L["q_wait"] % name), html_escape(L["q_check"])))

    # bridge - the certificate is on a card or token
    return head + ("""
 <p class="qp">%(txt)s</p>
 <div class="hashrow"><code id="q_hash">%(hash)s</code>
   <button type="button" id="q_copy">%(copy)s</button></div>
 <label>%(upl)s</label><input type="file" id="q_file" accept=".p7s,.p7m,.sig,.pkcs7,.der,.asice,.sce,.bin">
 <button type="button" class="qbtn go" id="q_send_file">%(sign)s</button>
 <div id="q_state" class="qstate"></div></div>""" % {
        "txt": html_escape(L["q_bridge"] % name), "hash": r["doc_hash"],
        "copy": html_escape(L["q_copy"]), "upl": html_escape(L["q_upload"]),
        "sign": html_escape(L["q_sign"])})


@app.get("/sign/<token>")
def sign_page(token):
    conn = db.get_db()
    try:
        lang = _doc_lang(SIGN_PAGE_I18N)
        L = SIGN_PAGE_I18N[lang]
        r = conn.execute("SELECT * FROM signatures WHERE token=?",
                         (db.token_hash(token),)).fetchone()
        if not r:
            return _doc_shell(lang, L["invalid"], "%s<h1>%s</h1>" % (_doc_brandmark(lang), html_escape(L["invalid"]))), 404
        if r["status"] == "signed":
            return redirect("/signature/%d" % r["id"])

        lvl_label = {"simple": L["l_simple"], "advanced": L["l_advanced"], "qualified": L["l_qualified"]}[r["level"]]
        qpanel = _sign_qpanel(conn, r, L)
        # With a real provider carrying the signature, Seam's own e-mail code
        # would be ceremony on top of a stronger check - so it is not asked for.
        adv = r["level"] == "advanced" or (r["level"] == "qualified" and not qpanel)
        inner = ("""%(brand)s
<h1>%(t)s</h1>
<table>
 <tr><td>%(docl)s</td><td><b>%(doc)s</b>%(link)s</td></tr>
 <tr><td>%(lvll)s</td><td>%(lvl)s</td></tr>
 <tr><td>%(hashl)s</td><td><code style="font-size:11.5px;word-break:break-all">%(hash)s</code></td></tr>
</table>
%(qnote)s
%(qpanel)s
<form id="sf" style="margin-top:18px">
 <label>%(namel)s *</label><input id="s_name" required maxlength="120" value="%(pname)s">
 <label>%(rolel)s</label><input id="s_role" maxlength="80" value="%(prole)s">
 <label>%(emaill)s</label><input id="s_email" type="email" maxlength="160" value="%(pemail)s">
 <label>%(drawl)s</label>
 <div class="padwrap"><canvas id="pad" width="560" height="150"></canvas>
   <button type="button" class="mini" id="clr">%(clear)s</button></div>
 %(otp)s
 <label class="chk"><input type="checkbox" id="s_consent"> <span>%(consent)s</span></label>
 <button type="submit" id="go">%(btn)s</button>
 <div id="err" class="err"></div>
</form>
<style>
label{display:block;font-size:12.5px;font-weight:600;color:#5b6472;margin:14px 0 5px}
input[type=text],input,select{width:100%%;box-sizing:border-box;padding:10px 12px;border:1px solid #d3d7de;border-radius:8px;font-size:14px;font-family:inherit}
.padwrap{position:relative;border:1px dashed #c8cdd6;border-radius:10px;background:#fbfcfd;padding:6px}
#pad{width:100%%;height:150px;touch-action:none;cursor:crosshair;display:block}
.mini{position:absolute;top:8px;right:8px;font-size:11.5px;padding:4px 10px;border:1px solid #d3d7de;background:#fff;border-radius:6px;cursor:pointer}
.chk{display:flex;gap:9px;align-items:flex-start;font-weight:400;font-size:13px;color:#1a1f2b;margin-top:16px}
.chk input{width:17px;height:17px;flex:none;margin-top:1px}
#go{width:100%%;margin-top:18px;padding:13px;background:#2f6df0;color:#fff;border:0;border-radius:8px;font-size:15px;font-weight:700;cursor:pointer}
#go:disabled{opacity:.6;cursor:default}
.err{color:#d24b4b;font-size:13px;margin-top:10px}
.otprow{display:flex;gap:8px;align-items:center}.otprow input{flex:1}
.otprow button{padding:10px 14px;border:1px solid #d3d7de;background:#fff;border-radius:8px;cursor:pointer;font-size:13px;white-space:nowrap}
.qn{background:#fdf3e0;border:1px solid #e8d5a8;border-radius:8px;padding:10px 13px;font-size:12.5px;color:#6b5620;margin-top:14px}
.qn.ok{background:#eef6ee;border-color:#c3ddc3;color:#3c6340}
.law{font-size:11.5px;color:#5b6472;margin-top:7px;line-height:1.5}
.qbox{border:1px solid #d3d7de;border-radius:10px;padding:14px 16px;margin-top:16px;background:#fbfcfd}
.qh{font-size:13px;font-weight:700;color:#1a1f2b;margin-bottom:4px}
.qp{font-size:12.5px;color:#5b6472;line-height:1.55;margin:8px 0 4px}
.qbtn{margin-top:12px;padding:10px 14px;border:1px solid #d3d7de;background:#fff;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit}
.qbtn.go{width:100%%;background:#1a1f2b;color:#fff;border-color:#1a1f2b;font-weight:600}
.qbtn:disabled{opacity:.55;cursor:default}
.qhide{display:none}
.qstate{font-size:12.5px;color:#5b6472;margin-top:10px;line-height:1.5}
.qstate.ok{color:#3c6340;font-weight:600}.qstate.bad{color:#d24b4b}
.hashrow{display:flex;gap:8px;align-items:center;margin-top:8px}
.hashrow code{flex:1;font-size:11px;word-break:break-all;background:#fff;border:1px solid #e3e6ea;border-radius:6px;padding:7px 9px}
.hashrow button{padding:7px 11px;border:1px solid #d3d7de;background:#fff;border-radius:6px;cursor:pointer;font-size:12px;white-space:nowrap}
</style>
<div id="sign-cfg" data-sid="%(sid)d" data-token="%(tok)s" data-i18n='%(i18n)s'></div>
<script src="/static/js/sign.js?v=%(build)s"></script>""") % {
            "brand": _doc_brandmark(lang), "t": L["t"], "docl": L["doc"], "doc": html_escape(r["doc_title"]),
            "link": (' <a href="%s" target="_blank">↗</a>' % html_escape(r["doc_url"])) if r["doc_url"] else "",
            "lvll": L["lvl"], "lvl": html_escape(lvl_label), "hashl": L["hash"], "hash": r["doc_hash"],
            "qnote": _sign_qnote(r, L),
            "namel": L["name"], "rolel": L["role"], "emaill": L["email"], "drawl": L["draw"], "clear": L["clear"],
            "pname": html_escape(r["signer_name"] or ""), "prole": html_escape(r["signer_role"] or ""),
            "pemail": html_escape(r["signer_email"] or ""),
            "otp": ('<label>%s</label><div class="otprow"><input id="s_code" maxlength="6" inputmode="numeric">'
                    '<button type="button" id="otp_send">%s</button></div>'
                    '<div id="otp_note" class="dim" style="font-size:12px;margin-top:6px"></div>'
                    % (L["code"], L["send_code"])) if adv else "",
            "consent": html_escape(L["consent"]), "btn": L["btn"],
            # The token from the URL, not the stored hash: the page posts it
            # back and the server hashes it again to find this row.
            "sid": r["id"], "tok": html_escape(token), "qpanel": qpanel,
            "build": _asset_build(),
            # Strings the external script needs, as data - never as code.
            "i18n": html_escape(json.dumps(
                {k: L[k] for k in ("code_sent", "q_done", "q_reject", "q_copied", "q_upload")},
                ensure_ascii=False)),
        }
        # No print bar: this page is a form to fill in, not a sheet to keep.
        # The certificate it produces after signing is the thing worth saving.
        return _doc_shell(lang, L["t"], inner, printable=False)
    finally:
        conn.close()


def _verification_label(ev, L):
    """How the signer was verified, in the reader's language. Older records
    that predate verification_kind fall back to the stored evidence string."""
    kind = ev.get("verification_kind")
    if kind == "qtsp":
        return L["v_qtsp"] % (ev.get("qtsp_name") or "")
    if kind == "otp":
        return L["v_otp"]
    if kind == "consent":
        return L["v_consent"]
    return ev.get("verification") or ""


@app.get("/signature/<int:sid>.pdf")
def signature_certificate_pdf(sid):
    g._doc_pdf = True
    return signature_certificate(sid)


@app.get("/signature/<int:sid>")
def signature_certificate(sid):
    """Printable evidence record for a completed signature."""
    conn = db.get_db()
    try:
        if not getattr(g, "_doc_pdf", False):
            g._doc_pdf_base = "/signature/%d.pdf" % sid
        lang = _doc_lang(SIGN_PAGE_I18N)
        L = SIGN_PAGE_I18N[lang]
        r = conn.execute("SELECT * FROM signatures WHERE id=?", (sid,)).fetchone()
        if not r or r["status"] != "signed":
            return _doc_shell(lang, L["invalid"], "%s<h1>%s</h1>" % (_doc_brandmark(lang), html_escape(L["invalid"]))), 404
        try:
            ev = json.loads(r["evidence_json"] or "{}")
        except Exception:
            ev = {}
        lvl_label = {"simple": L["l_simple"], "advanced": L["l_advanced"], "qualified": L["l_qualified"]}[r["level"]]
        img = ('<div style="margin:14px 0"><img src="%s" alt="" style="max-width:300px;border-bottom:1px solid #1a1f2b"></div>'
               % html_escape(r["signature_img"])) if r["signature_img"] else ""
        inner = ("""%(brand)s
<h1>✓ %(done)s</h1><div class="doc-sub">%(donep)s</div>
<table>
 <tr><td>%(docl)s</td><td><b>%(doc)s</b></td></tr>
 <tr><td>%(namel)s</td><td><b>%(name)s</b>%(role)s</td></tr>
 <tr><td>%(emaill)s</td><td>%(email)s</td></tr>
 <tr><td>%(lvll)s</td><td>%(lvl)s</td></tr>
 <tr><td>%(whenl)s</td><td>%(when)s</td></tr>
 <tr><td>%(ipl)s</td><td>%(ip)s</td></tr>
 <tr><td>%(devl)s</td><td>%(dev)s</td></tr>
 <tr><td>%(methl)s</td><td>%(meth)s</td></tr>
%(qtsp)s
 <tr><td>%(hashl)s</td><td><code style="font-size:11.5px;word-break:break-all">%(hash)s</code></td></tr>
</table>
%(img)s
<p class="disc">%(ev)s · Seam</p>""") % {
            "brand": _doc_brandmark(lang), "done": L["done"], "donep": L["done_p"],
            "docl": L["doc"], "doc": html_escape(r["doc_title"]),
            "namel": L["name"], "name": html_escape(r["signer_name"] or ""),
            "role": (" · " + html_escape(r["signer_role"])) if r["signer_role"] else "",
            "emaill": L["email"], "email": html_escape(r["signer_email"] or "—"),
            "lvll": L["lvl"], "lvl": html_escape(lvl_label),
            "whenl": L["ev_when"], "when": html_escape(ev.get("signed_at_utc") or r["signed_at"] or ""),
            "ipl": L["ev_ip"], "ip": html_escape(r["ip"] or "—"),
            "devl": L["ev_dev"], "dev": html_escape(r["device"] or "—"),
            "methl": L["ev_method"], "meth": html_escape(_verification_label(ev, L)),
            "qtsp": ("<tr><td>%s</td><td><b>%s</b></td></tr>\n<tr><td>%s</td><td><code>%s</code></td></tr>" % (
                html_escape(L["ev_qtsp"]), html_escape(ev.get("qtsp_name") or ""),
                html_escape(L["ev_qref"]), html_escape(ev.get("qtsp_reference") or "—"))
            ) if ev.get("qtsp") else "",
            "hashl": L["hash"], "hash": r["doc_hash"], "img": img, "ev": L["ev"],
        }
        return _doc_out(lang, L["cert"], inner, number=str(sid))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Business Digital Passport
#
#  One permanent record per counterparty: identity, every workspace and order,
#  agreed terms, documents/evidence, communication and the full audit trail -
#  assembled from data that already exists, so nothing has to be re-entered.
# --------------------------------------------------------------------------- #
@app.get("/api/passport/<int:org_id>")
@login_required
def passport(org_id):
    conn, user = g.conn, g.user
    my_orgs = set(user_org_ids(conn, user["id"]))
    if not my_orgs:
        raise ApiError("Нямате организация", 400, code="no_org")

    # Only counterparties you actually share a workspace with (or yourself).
    shared = conn.execute(
        "SELECT * FROM workspaces WHERE (buyer_org_id=? AND partner_org_id IN (%s)) "
        "   OR (partner_org_id=? AND buyer_org_id IN (%s))"
        % (",".join("?" * len(my_orgs)), ",".join("?" * len(my_orgs))),
        (org_id,) + tuple(my_orgs) + (org_id,) + tuple(my_orgs)).fetchall()
    # Same answer whether the company does not exist or is simply none of your
    # business: otherwise walking the ids maps out who is registered here.
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
    if not org or (not shared and org_id not in my_orgs):
        raise ApiError("Не е намерено", 404, code="not_found")
    meta = company.country_meta(org["country"])

    ws_ids = [w["id"] for w in shared] or [
        w["id"] for w in conn.execute(
            "SELECT id FROM workspaces WHERE buyer_org_id=? OR partner_org_id=?", (org_id, org_id)).fetchall()]
    ph = ",".join("?" * len(ws_ids)) if ws_ids else "NULL"

    orders, docs, terms_open, terms_agreed, value = [], 0, 0, 0, 0.0
    if ws_ids:
        for o in conn.execute(
                "SELECT o.*, w.name AS ws_name FROM orders o JOIN workspaces w ON w.id=o.workspace_id "
                "WHERE o.workspace_id IN (%s) ORDER BY o.id DESC LIMIT 100" % ph, ws_ids).fetchall():
            tr = conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=? AND value!=''",
                              (o["id"],)).fetchall()
            spec = {s["key"]: s for s in tpl_get(conn, o["template"]).get("terms", [])}
            for t_ in tr:
                if t_["state"] == "agreed":
                    terms_agreed += 1
                    if (spec.get(t_["key"]) or {}).get("type") == "money":
                        value += _price_eur(t_["value"])
                else:
                    terms_open += 1
            docs += conn.execute("SELECT COUNT(*) c FROM attachments WHERE order_id=?", (o["id"],)).fetchone()["c"]
            orders.append({
                "id": o["id"], "ref": o["ref"], "title": dl(o["title"]), "status": o["status"],
                "status_label": tpl_stage_label(conn, o["template"], o["status"]),
                "workspace": dl(o["ws_name"]), "template": o["template"],
                "created_at": o["created_at"], "updated_at": o["updated_at"],
                "terms": [{"label": tpl_field_label(conn, o["template"], x["key"]),
                           "value": dl(x["value"]), "state": x["state"]} for x in tr],
            })

    events = []
    if ws_ids:
        for e in conn.execute(
                "SELECT e.*, u.name AS actor_name, o.ref AS order_ref FROM events e "
                "LEFT JOIN users u ON u.id=e.actor_user_id LEFT JOIN orders o ON o.id=e.order_id "
                "WHERE e.workspace_id IN (%s) ORDER BY e.id DESC LIMIT 200" % ph, ws_ids).fetchall():
            d = dict(e)
            d["meta_json"] = dl_meta(d.get("meta_json"))
            events.append(d)

    messages = 0
    if ws_ids:
        messages = conn.execute("SELECT COUNT(*) c FROM ws_messages WHERE workspace_id IN (%s)" % ph,
                                ws_ids).fetchone()["c"]

    first = min([o["created_at"] for o in orders], default=org["created_at"])
    return jsonify({
        "org": {"id": org["id"], "name": org["name"], "kind": org["kind"],
                "reg_number": org["reg_number"] or "", "country": org["country"] or "",
                "id_label": meta["id_label"], "vat_prefix": meta["vat_prefix"] or "",
                "verified": bool(org["verified_company"]), "since": first},
        "contacts": _org_contacts(org),
        "workspaces": [{"id": w["id"], "name": dl(w["name"]), "template": w["template"]} for w in shared],
        "stats": {"orders": len(orders), "open": sum(1 for o in orders if o["status"] not in ("closed", "completed")),
                  "terms_agreed": terms_agreed, "terms_open": terms_open,
                  "documents": docs, "messages": messages, "value": "EUR %.2f" % value},
        "orders": orders,
        "events": events,
    })


# --------------------------------------------------------------------------- #
#  Passport for a record: a project, a vehicle, a shipment - whatever the
#  vertical calls it. Same idea as the counterparty passport, one level down:
#  everything that ever happened to this one thing, in one permanent record.
# --------------------------------------------------------------------------- #
@app.get("/api/passport/order/<int:oid>")
@login_required
def passport_order(oid):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Не е намерено", 404, code="not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    my_org_id = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    other = conn.execute("SELECT * FROM orgs WHERE id=?", (other_org_id(ws, side),)).fetchone()
    tpl = tpl_get(conn, o["template"]) or {}
    spec = {s["key"]: s for s in tpl.get("terms", [])}

    terms, value = [], 0.0
    for t_ in conn.execute("SELECT * FROM order_terms WHERE order_id=? ORDER BY key",
                           (oid,)).fetchall():
        if t_["state"] == "agreed" and (spec.get(t_["key"]) or {}).get("type") == "money":
            value += _price_eur(t_["value"])
        terms.append({"key": t_["key"], "label": tpl_field_label(conn, o["template"], t_["key"]),
                      "value": dl(t_["value"] or ""), "state": t_["state"],
                      "updated_at": t_["updated_at"]})

    try:
        fields = json.loads(o["fields_json"] or "{}")
    except Exception:
        fields = {}

    sigs = [{"id": s["id"], "title": s["doc_title"], "level": s["level"], "status": s["status"],
             "signer": s["signer_name"] or "", "signed_at": s["signed_at"],
             "hash": s["doc_hash"], "provider": s["qtsp_provider"] or ""}
            for s in conn.execute("SELECT * FROM signatures WHERE order_id=? ORDER BY id",
                                  (oid,)).fetchall()]
    appr = _approvals_payload(conn, oid, my_org_id, user["id"])
    pays = [_payout_row(conn, r, {my_org_id}) for r in conn.execute(
        "SELECT * FROM payouts WHERE order_id=? ORDER BY id DESC", (oid,)).fetchall()]
    atts = [{"id": a["id"], "name": a["original_name"], "file": a["filename"],
             "kind": a["kind"], "at": a["created_at"]}
            for a in conn.execute("SELECT * FROM attachments WHERE order_id=? ORDER BY id",
                                  (oid,)).fetchall()]
    events = []
    for e in conn.execute(
            "SELECT e.*, u.name AS actor_name FROM events e LEFT JOIN users u ON u.id=e.actor_user_id "
            "WHERE e.order_id=? ORDER BY e.id DESC LIMIT 200", (oid,)).fetchall():
        d = dict(e)
        d["meta_json"] = dl_meta(d.get("meta_json"))
        events.append(d)
    comments = conn.execute("SELECT COUNT(*) c FROM comments WHERE order_id=?", (oid,)).fetchone()["c"]

    return jsonify({
        "kind": "order",
        "subject": {"id": o["id"], "ref": o["ref"], "title": dl(o["title"]),
                    "template": o["template"], "status": o["status"],
                    "status_label": tpl_stage_label(conn, o["template"], o["status"]),
                    "created_at": o["created_at"], "updated_at": o["updated_at"],
                    "workspace": {"id": ws["id"], "name": dl(ws["name"])},
                    "counterparty": {"id": other["id"], "name": dl(other["name"])} if other else None},
        "fields": [{"key": f["key"], "label": tpl_field_label(conn, o["template"], f["key"]),
                    "value": dl(str(fields.get(f["key"], "")))}
                   for f in tpl.get("fields", []) if fields.get(f["key"])],
        "terms": terms,
        "stats": {"value": "EUR %.2f" % value, "documents": len(atts), "signatures": len(sigs),
                  "comments": comments, "events": len(events),
                  "approvals": appr["progress"], "payouts": len(pays)},
        "signatures": sigs,
        "approvals": appr["approvals"],
        "payouts": pays,
        "attachments": atts,
        "events": events,
    })


# --------------------------------------------------------------------------- #
#  Vehicles
#
#  An imported car is bought, shipped, cleared, registered and sold - several
#  records over months. What stays constant is the VIN, so that is the object
#  the passport hangs on.
# --------------------------------------------------------------------------- #
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{11,17}$")     # I, O and Q are never used in a VIN


def _vehicle_row(conn, v):
    n = conn.execute("SELECT COUNT(*) c FROM vehicle_orders WHERE vehicle_id=?",
                     (v["id"],)).fetchone()["c"]
    return {"id": v["id"], "vin": v["vin"], "make": v["make"] or "", "model": v["model"] or "",
            "year": v["year"], "plate": v["plate"] or "", "colour": v["colour"] or "",
            "fuel": v["fuel"] or "", "mileage": v["mileage"], "note": v["note"] or "",
            "status": v["status"], "created_at": v["created_at"], "orders": n,
            "label": " ".join(x for x in [v["make"], v["model"],
                                          str(v["year"]) if v["year"] else ""] if x) or v["vin"]}


@app.get("/api/vehicles")
@login_required
def vehicles_list():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    q = (request.args.get("q") or "").strip().upper()
    rows = conn.execute("SELECT * FROM vehicles WHERE org_id=? ORDER BY id DESC",
                        (org["id"],)).fetchall()
    out = [_vehicle_row(conn, v) for v in rows]
    if q:
        out = [v for v in out if q in (v["vin"] + v["make"] + v["model"] + v["plate"]).upper()]
    return jsonify({"vehicles": out})


@app.post("/api/vehicles")
@login_required
def vehicles_create():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    d = body()
    vin = re.sub(r"\s+", "", (d.get("vin") or "")).upper()
    if not VIN_RE.match(vin):
        raise ApiError("Невалиден VIN (11-17 знака, без I, O и Q)", 400, code="bad_vin")
    year = d.get("year")
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    mileage = d.get("mileage")
    try:
        mileage = int(mileage) if mileage else None
    except (TypeError, ValueError):
        mileage = None
    # A VIN is never re-issued, so it stays on file for good. If this one was
    # archived, the car has come back - reactivate the record it already has
    # rather than starting a second history for the same vehicle.
    existing = conn.execute("SELECT * FROM vehicles WHERE org_id=? AND vin=?",
                            (org["id"], vin)).fetchone()
    if existing and existing["status"] != "archived":
        raise ApiError("Този VIN вече е заведен", 400, code="vin_exists")
    args = ((d.get("make") or "").strip()[:40], (d.get("model") or "").strip()[:60], year,
            (d.get("plate") or "").strip()[:20], (d.get("colour") or "").strip()[:30],
            (d.get("fuel") or "").strip()[:20], mileage, (d.get("note") or "").strip()[:300])
    if existing:
        conn.execute("UPDATE vehicles SET make=?, model=?, year=?, plate=?, colour=?, fuel=?, "
                     "mileage=?, note=?, status='active' WHERE id=?", args + (existing["id"],))
    else:
        conn.execute(
            "INSERT INTO vehicles (org_id, vin, make, model, year, plate, colour, fuel, mileage, "
            "note, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (org["id"], vin) + args + (user["id"],))
    conn.commit()
    return jsonify({"vehicles": [_vehicle_row(conn, v) for v in conn.execute(
        "SELECT * FROM vehicles WHERE org_id=? ORDER BY id DESC", (org["id"],)).fetchall()]})


@app.patch("/api/vehicles/<int:vid>")
@login_required
def vehicles_update(vid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    v = conn.execute("SELECT * FROM vehicles WHERE id=? AND org_id=?", (vid, org["id"])).fetchone()
    if not v:
        raise ApiError("Не е намерено", 404, code="not_found")
    d = body()
    fields, args = [], []
    for k, cap in (("make", 40), ("model", 60), ("plate", 20), ("colour", 30),
                   ("fuel", 20), ("note", 300), ("status", 20)):
        if k in d:
            fields.append("%s=?" % k)
            args.append(str(d[k] or "")[:cap])
    for k in ("year", "mileage"):
        if k in d:
            try:
                args.append(int(d[k]) if d[k] not in (None, "") else None)
                fields.append("%s=?" % k)
            except (TypeError, ValueError):
                pass
    if fields:
        conn.execute("UPDATE vehicles SET %s WHERE id=?" % ",".join(fields), args + [vid])
        conn.commit()
    v = conn.execute("SELECT * FROM vehicles WHERE id=?", (vid,)).fetchone()
    return jsonify(_vehicle_row(conn, v))


@app.post("/api/vehicles/<int:vid>/link")
@login_required
def vehicles_link(vid):
    """Attach a record to this vehicle - purchase, transport, customs, sale."""
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    v = conn.execute("SELECT * FROM vehicles WHERE id=? AND org_id=?", (vid, org["id"])).fetchone()
    if not v:
        raise ApiError("Не е намерено", 404, code="not_found")
    oid = body().get("order_id")
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone() if oid else None
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    workspace_access(conn, o["workspace_id"], user["id"])
    try:
        conn.execute("INSERT INTO vehicle_orders (vehicle_id, order_id, role) VALUES (?,?,?)",
                     (vid, o["id"], (body().get("role") or "").strip()[:40]))
    except sqlite3.IntegrityError:
        pass                                     # already linked
    conn.commit()
    return jsonify(_vehicle_row(conn, v))


@app.delete("/api/vehicles/<int:vid>/link/<int:oid>")
@login_required
def vehicles_unlink(vid, oid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    conn.execute("DELETE FROM vehicle_orders WHERE vehicle_id=? AND order_id=? AND vehicle_id IN "
                 "(SELECT id FROM vehicles WHERE org_id=?)", (vid, oid, org["id"]))
    conn.commit()
    return jsonify({"unlinked": True})


@app.get("/api/passport/vehicle/<int:vid>")
@login_required
def passport_vehicle(vid):
    """Everything that ever touched this VIN: every record, every document,
    every signature, every payment, in one permanent history."""
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    v = conn.execute("SELECT * FROM vehicles WHERE id=? AND org_id=?", (vid, org["id"])).fetchone()
    if not v:
        raise ApiError("Не е намерено", 404, code="not_found")

    links = conn.execute(
        "SELECT vo.role, o.* FROM vehicle_orders vo JOIN orders o ON o.id=vo.order_id "
        "WHERE vo.vehicle_id=? ORDER BY o.id", (vid,)).fetchall()
    orders, value, docs, sigs, events = [], 0.0, 0, [], []
    for o in links:
        spec = {s["key"]: s for s in (tpl_get(conn, o["template"]) or {}).get("terms", [])}
        for t_ in conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=?",
                               (o["id"],)).fetchall():
            if t_["state"] == "agreed" and (spec.get(t_["key"]) or {}).get("type") == "money":
                value += _price_eur(t_["value"])
        docs += conn.execute("SELECT COUNT(*) c FROM attachments WHERE order_id=?",
                             (o["id"],)).fetchone()["c"]
        for s in conn.execute("SELECT * FROM signatures WHERE order_id=? ORDER BY id",
                              (o["id"],)).fetchall():
            sigs.append({"id": s["id"], "title": s["doc_title"], "level": s["level"],
                         "status": s["status"], "signer": s["signer_name"] or "",
                         "signed_at": s["signed_at"], "hash": s["doc_hash"]})
        for e in conn.execute(
                "SELECT e.*, u.name AS actor_name FROM events e LEFT JOIN users u "
                "ON u.id=e.actor_user_id WHERE e.order_id=? ORDER BY e.id DESC LIMIT 60",
                (o["id"],)).fetchall():
            d_ = dict(e)
            d_["meta_json"] = dl_meta(d_.get("meta_json"))
            events.append(d_)
        ws = conn.execute("SELECT name FROM workspaces WHERE id=?", (o["workspace_id"],)).fetchone()
        orders.append({"id": o["id"], "ref": o["ref"], "title": dl(o["title"]),
                       "status": o["status"], "template": o["template"],
                       "status_label": tpl_stage_label(conn, o["template"], o["status"]),
                       "workspace": dl(ws["name"]) if ws else "",
                       "role": o["role"] or "", "created_at": o["created_at"]})
    events.sort(key=lambda e: e["id"], reverse=True)

    payouts = []
    if links:
        ids = [o["id"] for o in links]
        ph = ",".join("?" * len(ids))
        payouts = [_payout_row(conn, r, {org["id"]}) for r in conn.execute(
            "SELECT * FROM payouts WHERE order_id IN (%s) ORDER BY id DESC" % ph, ids).fetchall()]

    return jsonify({
        "kind": "vehicle",
        "subject": _vehicle_row(conn, v),
        "stats": {"records": len(orders), "value": "EUR %.2f" % value, "documents": docs,
                  "signatures": len(sigs), "payouts": len(payouts), "events": len(events)},
        "orders": orders, "signatures": sigs, "payouts": payouts, "events": events[:200],
    })


@app.get("/api/passport/product/<int:pid>")
@login_required
def passport_product(pid):
    """Everything that ever happened to one product: stock, keys, every store
    order it appeared in."""
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    p = conn.execute("SELECT * FROM products WHERE id=? AND org_id=?", (pid, org["id"])).fetchone()
    if not p:
        raise ApiError("Не е намерено", 404, code="not_found")
    rows = conn.execute(
        "SELECT * FROM store_orders WHERE product_id=? ORDER BY id DESC", (pid,)).fetchall()
    keys = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN order_id IS NULL THEN 1 ELSE 0 END) free "
        "FROM product_keys WHERE product_id=?", (pid,)).fetchone()
    revenue = sum(_price_eur(p["price"]) * (r["qty"] or 1) for r in rows if r["status"] == "fulfilled")
    return jsonify({
        "kind": "product",
        "subject": {"id": p["id"], "name": dl(p["name"]), "type": p["type"],
                    "price": p["price"] or "", "sku": p["sku"] or "", "image": p["image"] or "",
                    "stock": p["stock"], "available": product_available(conn, p),
                    "created_at": p["created_at"]},
        "stats": {"orders": len(rows),
                  "fulfilled": sum(1 for r in rows if r["status"] == "fulfilled"),
                  "revenue": "EUR %.2f" % revenue,
                  "keys_total": keys["c"] or 0, "keys_free": keys["free"] or 0},
        "orders": [serialize_store_order(conn, r) for r in rows[:100]],
    })


@app.get("/api/workspaces/<int:ws_id>/contacts")
@login_required
def ws_contacts(ws_id):
    """Both sides' off-platform emergency contacts, for the comms channel."""
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    buyer = conn.execute("SELECT * FROM orgs WHERE id=?", (ws["buyer_org_id"],)).fetchone()
    partner = conn.execute("SELECT * FROM orgs WHERE id=?", (ws["partner_org_id"],)).fetchone() if ws["partner_org_id"] else None
    my_org_id = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    other = partner if side == "buyer" else buyer
    return jsonify({
        "mine": _org_contacts(buyer if side == "buyer" else partner) if (buyer if side == "buyer" else partner) else {},
        "other": _org_contacts(other) if other else {},
        "other_name": (other["name"] if other else ""),
        "my_org_id": my_org_id,
    })


def _ws_messages_payload(conn, ws_id, uid):
    """Plain helper so both the GET route and the POST route can serialise the
    list on the SAME connection. (Calling a @login_required view directly would
    open a second connection that cannot see the current transaction.)"""
    rows = conn.execute(
        "SELECT m.*, u.name AS author FROM ws_messages m JOIN users u ON u.id=m.user_id "
        "WHERE m.workspace_id=? ORDER BY m.id ASC LIMIT 200", (ws_id,)).fetchall()
    return [{"id": r["id"], "author": r["author"], "body": dl(r["body"]),
             "mine": r["user_id"] == uid, "created_at": r["created_at"]} for r in rows]


@app.get("/api/workspaces/<int:ws_id>/messages")
@login_required
def ws_messages(ws_id):
    conn, user = g.conn, g.user
    workspace_access(conn, ws_id, user["id"])
    out = _ws_messages_payload(conn, ws_id, user["id"])
    _mark_read(conn, user["id"], ws_id, out[-1]["id"] if out else 0)
    return jsonify(out)


def _mark_read(conn, uid, ws_id, last_id):
    """Never move the marker backwards: opening an old conversation on a phone
    must not resurrect messages already read on a desktop."""
    conn.execute(
        "INSERT INTO ws_reads (user_id, workspace_id, last_id) VALUES (?,?,?) "
        "ON CONFLICT(user_id, workspace_id) DO UPDATE SET "
        "last_id=MAX(last_id, excluded.last_id), at=datetime('now')",
        (uid, ws_id, int(last_id or 0)))
    conn.commit()


@app.get("/api/messages")
@login_required
def messages_inbox():
    """Every conversation this person is part of, newest first.

    One list across all workspaces, because "where did we agree that" is a
    question about a company, not about a workspace somebody has to remember
    the name of. Each row carries the other side, so the inbox reads as people
    rather than as project codes.
    """
    conn, user = g.conn, g.user
    rows = conn.execute(
        "SELECT DISTINCT w.id, w.name, w.buyer_org_id, w.partner_org_id "
        "FROM workspaces w JOIN memberships m "
        "ON m.org_id IN (w.buyer_org_id, w.partner_org_id) WHERE m.user_id=?",
        (user["id"],)).fetchall()
    mine = user_primary_org(conn, user["id"])
    my_id = mine["id"] if mine else 0
    out = []
    for w in rows:
        last = conn.execute(
            "SELECT m.id, m.body, m.created_at, m.user_id, u.name author "
            "FROM ws_messages m JOIN users u ON u.id = m.user_id "
            "WHERE m.workspace_id=? ORDER BY m.id DESC LIMIT 1", (w["id"],)).fetchone()
        read = conn.execute("SELECT last_id FROM ws_reads WHERE user_id=? AND workspace_id=?",
                            (user["id"], w["id"])).fetchone()
        seen = read["last_id"] if read else 0
        unread = conn.execute(
            "SELECT COUNT(*) c FROM ws_messages WHERE workspace_id=? AND id>? AND user_id<>?",
            (w["id"], seen, user["id"])).fetchone()["c"]
        other_id = w["partner_org_id"] if w["buyer_org_id"] == my_id else w["buyer_org_id"]
        other = conn.execute("SELECT id, name, logo FROM orgs WHERE id=?",
                             (other_id,)).fetchone() if other_id else None
        out.append({
            "workspace_id": w["id"], "workspace": w["name"],
            "org_id": other["id"] if other else 0,
            # A workspace whose invitation nobody has accepted yet has no other
            # side to name. It still belongs in the list - our own side may have
            # written in it - so it is labelled by the workspace and marked,
            # rather than rendered as a row with a blank where a company goes.
            "org": other["name"] if other else dl(w["name"]),
            "pending": not other,
            "logo": ("/uploads/profile/%s" % other["logo"]) if (other and other["logo"]) else "",
            "unread": unread,
            "last": ({"body": dl(last["body"])[:160], "at": last["created_at"],
                      "author": last["author"], "mine": last["user_id"] == user["id"]}
                     if last else None),
            "at": last["created_at"] if last else "",
        })
    # Conversations with something in them first, most recent at the top; the
    # rest below in name order, so a new relationship is still findable.
    out.sort(key=lambda r: (0 if r["at"] else 1, r["at"] and _neg_at(r["at"]), r["org"].lower()))
    return jsonify({"items": out, "unread": sum(r["unread"] for r in out)})


def _neg_at(at):
    """Descending order for an ISO timestamp without parsing it: invert each
    character against the fixed alphabet the format uses."""
    return "".join(chr(255 - ord(c)) for c in at)


@app.post("/api/workspaces/<int:ws_id>/messages")
@login_required
def ws_message_post(ws_id):
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    text = require(body(), "body")[:2000]
    conn.execute("INSERT INTO ws_messages (workspace_id, user_id, body) VALUES (?,?,?)", (ws_id, user["id"], text))
    notify_org(conn, other_org_id(ws, side), ws_id, None, "ws_message",
               "ws_message", exclude_user=user["id"], meta={"ws": ws["name"], "by": user["name"]})
    return jsonify(_ws_messages_payload(conn, ws_id, user["id"]))


# --------------------------------------------------------------------------- #
#  Acceptance protocol (printable PDF via browser print) - order + parties +
#  agreed terms + status history + photo evidence + signature blocks
# --------------------------------------------------------------------------- #
PROT_I18N = {
    "en": {"reg_label": "Company number", "title": "Acceptance protocol", "order": "Order", "date": "Date", "parties": "Parties", "business": "Assigning business", "partner": "Executing partner", "reg": "Company no.", "details": "Details", "terms": "Agreed terms", "state_agreed": "agreed", "state_proposed": "proposed", "history": "Status history", "media": "Photo evidence", "sig_a": "For the business", "sig_b": "For the partner", "sig": "Name, signature, date", "footer": "Generated by Seam. The full версия of the record, including the complete audit trail, is available in the platform."},
    "es": {"reg_label": "Número de registro", "title": "Acta de aceptación", "order": "Pedido", "date": "Fecha", "parties": "Partes", "business": "Empresa que encarga", "partner": "Socio ejecutor", "reg": "N.º de empresa", "details": "Detalles", "terms": "Condiciones acordadas", "state_agreed": "acordado", "state_proposed": "propuesto", "history": "Historial de estado", "media": "Evidencia fotográfica", "sig_a": "Por la empresa", "sig_b": "Por el socio", "sig": "Nombre, firma, fecha", "footer": "Generado por Seam. El registro completo, incluido todo el rastro de auditoría, está disponible en la plataforma."},
    "fr": {"reg_label": "Numéro d'immatriculation", "title": "Procès-verbal de réception", "order": "Commande", "date": "Date", "parties": "Parties", "business": "Entreprise donneuse d'ordre", "partner": "Partenaire exécutant", "reg": "N° d'entreprise", "details": "Détails", "terms": "Conditions convenues", "state_agreed": "convenu", "state_proposed": "proposé", "history": "Historique du statut", "media": "Preuves photo", "sig_a": "Pour l'entreprise", "sig_b": "Pour le partenaire", "sig": "Nom, signature, date", "footer": "Généré par Seam. L'enregistrement complet, y compris toute la piste d'audit, est disponible sur la plateforme."},
    "pl": {"reg_label": "Numer rejestrowy", "title": "Protokół odbioru", "order": "Zamówienie", "date": "Data", "parties": "Strony", "business": "Firma zlecająca", "partner": "Partner wykonujący", "reg": "Nr firmy", "details": "Szczegóły", "terms": "Uzgodnione warunki", "state_agreed": "uzgodnione", "state_proposed": "zaproponowane", "history": "Historia statusu", "media": "Dowody zdjęciowe", "sig_a": "Za firmę", "sig_b": "Za partnera", "sig": "Imię i nazwisko, podpis, data", "footer": "Wygenerowano przez Seam. Pełny zapis, w tym cała ścieżka audytu, jest dostępny na platformie."},
    "uk": {"reg_label": "Реєстраційний номер", "title": "Акт приймання", "order": "Замовлення", "date": "Дата", "parties": "Сторони", "business": "Компанія-замовник", "partner": "Партнер-виконавець", "reg": "Код компанії", "details": "Деталі", "terms": "Узгоджені умови", "state_agreed": "узгоджено", "state_proposed": "запропоновано", "history": "Історія статусу", "media": "Фотодокази", "sig_a": "За компанію", "sig_b": "За партнера", "sig": "Ім'я, підпис, дата", "footer": "Створено в Seam. Повний запис, включно з усім аудиторським слідом, доступний на платформі."},
    "pt": {"reg_label": "Número de registo", "title": "Auto de receção", "order": "Encomenda", "date": "Data", "parties": "Partes", "business": "Empresa adjudicante", "partner": "Parceiro executante", "reg": "N.º de empresa", "details": "Detalhes", "terms": "Condições acordadas", "state_agreed": "acordado", "state_proposed": "proposto", "history": "Histórico do estado", "media": "Provas fotográficas", "sig_a": "Pela empresa", "sig_b": "Pelo parceiro", "sig": "Nome, assinatura, data", "footer": "Gerado pelo Seam. O registo completo, incluindo todo o rasto de auditoria, está disponível na plataforma."},
    "bg": {"reg_label": "Фирмен номер", "title": "Приемо-предавателен протокол", "order": "Поръчка", "date": "Дата", "parties": "Страни", "business": "Възложител", "partner": "Изпълнител", "reg": "ЕИК / рег. №", "details": "Детайли", "terms": "Договорени условия", "state_agreed": "договорено", "state_proposed": "предложено", "history": "История на статуса", "media": "Снимков материал", "sig_a": "За възложителя", "sig_b": "За изпълнителя", "sig": "Име, подпис, дата", "footer": "Генерирано от Seam. Пълният запис, включително цялата одитна следа, е достъпен в платформата."},
    "de": {"reg_label": "Firmennummer", "title": "Abnahmeprotokoll", "order": "Auftrag", "date": "Datum", "parties": "Parteien", "business": "Auftraggeber", "partner": "Auftragnehmer", "reg": "Firmen-Nr.", "details": "Details", "terms": "Vereinbarte Konditionen", "state_agreed": "vereinbart", "state_proposed": "vorgeschlagen", "history": "Statusverlauf", "media": "Fotonachweise", "sig_a": "Für den Auftraggeber", "sig_b": "Für den Auftragnehmer", "sig": "Name, Unterschrift, Datum", "footer": "Erstellt mit Seam. Der vollständige Datensatz inkl. Audit-Trail ist in der Plattform verfügbar."},
    "ro": {"reg_label": "Număr de înregistrare", "title": "Proces-verbal de recepție", "order": "Comandă", "date": "Data", "parties": "Părți", "business": "Beneficiar", "partner": "Executant", "reg": "Nr. firmă", "details": "Detalii", "terms": "Condiții agreate", "state_agreed": "agreat", "state_proposed": "propus", "history": "Istoric status", "media": "Dovezi foto", "sig_a": "Pentru beneficiar", "sig_b": "Pentru executant", "sig": "Nume, semnătură, data", "footer": "Generat de Seam. Înregistrarea completă, inclusiv întregul audit, este disponibilă în platformă."},
    "el": {"reg_label": "Αριθμός μητρώου", "title": "Πρωτόκολλο παραλαβής", "order": "Παραγγελία", "date": "Ημερομηνία", "parties": "Μέρη", "business": "Αναθέτων", "partner": "Ανάδοχος", "reg": "Αρ. εταιρείας", "details": "Λεπτομέρειες", "terms": "Συμφωνημένοι όροι", "state_agreed": "συμφωνημένο", "state_proposed": "προταθέν", "history": "Ιστορικό κατάστασης", "media": "Φωτογραφικό υλικό", "sig_a": "Για τον αναθέτοντα", "sig_b": "Για τον ανάδοχο", "sig": "Όνομα, υπογραφή, ημερομηνία", "footer": "Δημιουργήθηκε από το Seam. Η πλήρης εγγραφή με το πλήρες ίχνος ελέγχου είναι διαθέσιμη στην πλατφόρμα."},
    "tr": {"reg_label": "Sicil numarası", "title": "Kabul tutanağı", "order": "Sipariş", "date": "Tarih", "parties": "Taraflar", "business": "İşveren", "partner": "Yüklenici", "reg": "Şirket no", "details": "Ayrıntılar", "terms": "Anlaşılan koşullar", "state_agreed": "anlaşıldı", "state_proposed": "önerildi", "history": "Durum geçmişi", "media": "Fotoğraf kanıtları", "sig_a": "İşveren adına", "sig_b": "Yüklenici adına", "sig": "Ad, imza, tarih", "footer": "Seam tarafından oluşturuldu. Tam kayıt ve denetim izi platformda mevcuttur."},
    "it": {"reg_label": "Numero di registro", "title": "Verbale di accettazione", "order": "Ordine", "date": "Data", "parties": "Parti", "business": "Committente", "partner": "Esecutore", "reg": "N. azienda", "details": "Dettagli", "terms": "Condizioni concordate", "state_agreed": "concordato", "state_proposed": "proposto", "history": "Cronologia stato", "media": "Prove fotografiche", "sig_a": "Per il committente", "sig_b": "Per l'esecutore", "sig": "Nome, firma, data", "footer": "Generato da Seam. Il record completo con l'intero audit trail è disponibile nella piattaforma."},
    "ru": {"reg_label": "Регистрационный номер", "title": "Акт приёма-передачи", "order": "Заказ", "date": "Дата", "parties": "Стороны", "business": "Заказчик", "partner": "Исполнитель", "reg": "Рег. №", "details": "Детали", "terms": "Согласованные условия", "state_agreed": "согласовано", "state_proposed": "предложено", "history": "История статуса", "media": "Фотоподтверждения", "sig_a": "От заказчика", "sig_b": "От исполнителя", "sig": "ФИО, подпись, дата", "footer": "Создано в Seam. Полная запись со всем аудитом доступна на платформе."},
}


@app.get("/protocol/<int:oid>.pdf")
def protocol_pdf(oid):
    g._doc_pdf = True
    return protocol_page(oid)


@app.get("/protocol/<int:oid>")
def protocol_page(oid):
    conn = db.get_db()
    try:
        uid = session.get("uid")
        if not uid:
            return "", 401
        if not getattr(g, "_doc_pdf", False):
            g._doc_pdf_base = "/protocol/%d.pdf" % oid
        o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return "", 404
        ws, side = workspace_access(conn, o["workspace_id"], uid)
        lang = _doc_lang(PROT_I18N)
        L = PROT_I18N[lang]
        d = serialize_order_detail(conn, o, ws, side)
        buyer = conn.execute("SELECT name, reg_number, country FROM orgs WHERE id=?", (ws["buyer_org_id"],)).fetchone()
        partner = conn.execute("SELECT name, reg_number, country FROM orgs WHERE id=?", (ws["partner_org_id"],)).fetchone() if ws["partner_org_id"] else None

        def party(p):
            if not p:
                return "-"
            s = html_escape(p["name"])
            if p["reg_number"]:
                s += " · %s %s" % (L["reg"], html_escape(p["reg_number"]))
            return s

        fields_rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (
            html_escape(tpl_field_label(conn, o["template"], f["key"])), html_escape(dl(d["fields"].get(f["key"], "")) or "-"))
            for f in d["field_specs"])
        def fmt_term_value(tm):
            v = tm["value"] or "-"
            if tm.get("type") == "money":
                m = re.match(r"^([A-Z]{3})\s+([0-9]+(?:\.[0-9]+)?)$", v.strip())
                if m:
                    cur, amt = m.group(1), float(m.group(2))
                    pretty = "{:,.2f}".format(amt).replace(",", " ")
                    v = "%s %s" % (pretty, "€" if cur == "EUR" else cur)
            return v

        terms_rows = "".join("<tr><td>%s</td><td>%s</td><td class='st %s'>%s</td></tr>" % (
            html_escape(tm["label"]), html_escape(fmt_term_value(tm)),
            tm["state"], L["state_agreed"] if tm["state"] == "agreed" else L["state_proposed"])
            for tm in d["terms"] if tm["value"])
        hist_rows = ""
        for e in d["events"]:
            if e["kind"] != "status_changed":
                continue
            try:
                m = json.loads(e["meta_json"] or "{}")
            except Exception:
                m = {}
            hist_rows += "<tr><td>%s</td><td>%s → %s</td><td>%s</td></tr>" % (
                html_escape(e["created_at"][:16]),
                html_escape(tpl_stage_label(conn, o["template"], m.get("from", ""))),
                html_escape(tpl_stage_label(conn, o["template"], m.get("to", ""))),
                html_escape(e.get("actor_name") or ""))
        photos = "".join("<div class='ph'><img src='%s'><div class='pc'>%s · %s</div></div>"
                         % (a["url"], html_escape(a["uploader"]), html_escape(a["created_at"][:16]))
                         for a in d["attachments"] if a["kind"] == "image")
        page = ("""<!DOCTYPE html><html lang="%(lang)s"><head><meta charset="utf-8"><meta name="color-scheme" content="light">
<title>%(title)s · %(ref)s</title><style>
html{background:#fff}body{font-family:'Segoe UI',Arial,sans-serif;color:#1a1f2b;background:#fff;max-width:760px;margin:36px auto;padding:0 22px}
h1{font-size:21px;margin:0 0 2px}h2{font-size:14px;margin:26px 0 8px;text-transform:uppercase;letter-spacing:.05em;color:#5b6472}
.ref{color:#2f6df0;font-weight:700}.sub{color:#8a93a3;font-size:13px}
table{width:100%%;border-collapse:collapse}td,th{padding:7px 8px;border-bottom:1px solid #e6e8ec;font-size:13.5px;text-align:left;vertical-align:top}
td:first-child{color:#5b6472;width:210px}.st.agreed{color:#1f9d6b;font-weight:600}.st.proposed{color:#c9851a}
.phs{display:flex;flex-wrap:wrap;gap:12px}.ph{width:47%%}.ph img{width:100%%;border-radius:8px;border:1px solid #e6e8ec}
.pc{font-size:11.5px;color:#8a93a3;margin-top:3px}
.sigs{display:flex;gap:40px;margin-top:44px}.sig{flex:1;border-top:1.5px solid #1a1f2b;padding-top:8px;font-size:13px}
.sig b{display:block;margin-bottom:26px}.foot{margin-top:36px;color:#8a93a3;font-size:11.5px}
@media print{body{margin:10mm auto}}
</style></head><body>
<h1>%(title)s · <span class="ref">%(ref)s</span></h1>
<div class="sub">%(ordl)s: %(otitle)s · %(datel)s: %(today)s</div>
<h2>%(partiesl)s</h2><table>
<tr><td>%(bl)s</td><td>%(bp)s</td></tr>
<tr><td>%(pl)s</td><td>%(pp)s</td></tr></table>
<h2>%(detl)s</h2><table>%(fields)s</table>
<h2>%(terml)s</h2><table>%(terms)s</table>
%(hist_block)s
%(media_block)s
<div class="sigs"><div class="sig"><b>%(siga)s</b>%(sigl)s</div><div class="sig"><b>%(sigb)s</b>%(sigl)s</div></div>
<p class="foot">%(footer)s</p>
</body></html>""") % {
            "lang": lang, "title": L["title"], "ref": html_escape(o["ref"]),
            "ordl": L["order"], "otitle": html_escape(d["title"]), "datel": L["date"],
            "today": datetime.utcnow().strftime("%Y-%m-%d"),
            "partiesl": L["parties"], "bl": L["business"], "bp": party(buyer),
            "pl": L["partner"], "pp": party(partner),
            "detl": L["details"], "fields": fields_rows or "<tr><td>-</td><td>-</td></tr>",
            "terml": L["terms"], "terms": terms_rows or "<tr><td>-</td><td>-</td><td>-</td></tr>",
            "hist_block": ("<h2>%s</h2><table>%s</table>" % (L["history"], hist_rows)) if hist_rows else "",
            "media_block": ("<h2>%s</h2><div class='phs'>%s</div>" % (L["media"], photos)) if photos else "",
            "siga": L["sig_a"], "sigb": L["sig_b"], "sigl": L["sig"], "footer": L["footer"],
        }
        return _page_out(page, lang, L["title"], o["ref"])
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Document library: region-aware fill-in templates (handover protocol, NDA,
#  framework service agreement). Printable; NOT legal advice - a convenience
#  starting point localized per UI language.
# --------------------------------------------------------------------------- #
DOC_I18N = {
    "en": {"generated": "Date", "between": "between", "and": "and", "reg": "Company no.", "city": "City", "sig": "Name, signature, date", "disclaimer": "Sample template for convenience. Not legal advice - review with a qualified professional before use.",
           "handover": {"t": "Handover / acceptance protocol", "c": ["The parties confirm that the work / goods described below were handed over by the Contractor and accepted by the Assignor:", "Subject: {subject}", "The Assignor has inspected the subject and has no objections at the time of acceptance, unless noted otherwise below.", "Notes: {notes}", "This protocol is drawn up in two identical copies, one for each party."]},
           "nda": {"t": "Confidentiality declaration (NDA)", "c": ["Each party may share business, technical and commercial information with the other in connection with: {subject}.", "The receiving party shall use the information solely for the purpose above and protect it with at least the care applied to its own confidential information.", "Confidentiality obligations remain in force for 3 years after the last exchange of information.", "Public information and information required by law to be disclosed is not subject to this declaration."]},
           "service": {"t": "Framework service agreement", "c": ["The Contractor undertakes to perform for the Assignor the following services: {subject}.", "Specific orders, volumes, prices and deadlines are agreed per order in the shared Seam workspace and each two-sided approval there forms an integral part of this agreement.", "Payment is due per the terms agreed in each order, unless otherwise agreed in writing.", "The parties will document progress with the tools of the platform (statuses, agreed terms, photo evidence, audit trail).", "The agreement may be terminated by either party with 30 days' written notice; obligations already agreed per orders remain due."]}},
    "es": {"generated": "Fecha", "between": "entre", "and": "y", "reg": "N.º de empresa", "city": "Ciudad", "sig": "Nombre, firma, fecha", "disclaimer": "Modelo de muestra para comodidad. No constituye asesoramiento legal - revíselo con un profesional cualificado antes de usarlo.",
           "handover": {"t": "Acta de entrega / aceptación", "c": ["Las partes confirman que los trabajos / bienes descritos a continuación fueron entregados por el Contratista y aceptados por el Comitente:", "Objeto: {subject}", "El Comitente ha inspeccionado el objeto y no tiene objeciones en el momento de la aceptación, salvo lo indicado a continuación.", "Notas: {notes}", "La presente acta se redacta en dos ejemplares idénticos, uno para cada parte."]},
           "nda": {"t": "Declaración de confidencialidad (NDA)", "c": ["Cada parte puede compartir con la otra información comercial, técnica y de negocio en relación con: {subject}.", "La parte receptora usará la información únicamente para el fin anterior y la protegerá al menos con el cuidado aplicado a su propia información confidencial.", "Las obligaciones de confidencialidad permanecen en vigor durante 3 años tras el último intercambio de información.", "La información pública y la que la ley obligue a divulgar no están sujetas a esta declaración."]},
           "service": {"t": "Contrato marco de servicios", "c": ["El Contratista se compromete a prestar al Comitente los siguientes servicios: {subject}.", "Los pedidos concretos, volúmenes, precios y plazos se acuerdan por pedido en el espacio compartido de Seam, y cada aprobación bilateral allí forma parte integrante de este contrato.", "El pago se debe según las condiciones acordadas en cada pedido, salvo acuerdo escrito en contrario.", "Las partes documentarán el progreso con las herramientas de la plataforma (estados, condiciones acordadas, evidencia fotográfica, rastro de auditoría).", "El contrato puede ser rescindido por cualquiera de las partes con 30 días de preaviso por escrito; las obligaciones ya acordadas por pedidos siguen siendo exigibles."]}},
    "fr": {"generated": "Date", "between": "entre", "and": "et", "reg": "N° d'entreprise", "city": "Ville", "sig": "Nom, signature, date", "disclaimer": "Modèle d'exemple pour votre commodité. Ne constitue pas un conseil juridique - faites-le vérifier par un professionnel qualifié avant utilisation.",
           "handover": {"t": "Procès-verbal de remise / réception", "c": ["Les parties confirment que les travaux / biens décrits ci-dessous ont été remis par le Prestataire et acceptés par le Donneur d'ordre :", "Objet : {subject}", "Le Donneur d'ordre a inspecté l'objet et n'a aucune objection au moment de la réception, sauf indication contraire ci-dessous.", "Notes : {notes}", "Le présent procès-verbal est établi en deux exemplaires identiques, un pour chaque partie."]},
           "nda": {"t": "Déclaration de confidentialité (NDA)", "c": ["Chaque partie peut communiquer à l'autre des informations commerciales, techniques et d'affaires en lien avec : {subject}.", "La partie réceptrice utilise les informations uniquement aux fins ci-dessus et les protège au moins avec le soin appliqué à ses propres informations confidentielles.", "Les obligations de confidentialité restent en vigueur pendant 3 ans après le dernier échange d'informations.", "Les informations publiques et celles dont la loi exige la divulgation ne sont pas soumises à la présente déclaration."]},
           "service": {"t": "Contrat-cadre de services", "c": ["Le Prestataire s'engage à fournir au Donneur d'ordre les services suivants : {subject}.", "Les commandes concrètes, volumes, prix et délais sont convenus par commande dans l'espace partagé Seam, et chaque approbation bilatérale y fait partie intégrante du présent contrat.", "Le paiement est dû selon les conditions convenues pour chaque commande, sauf accord écrit contraire.", "Les parties documentent l'avancement avec les outils de la plateforme (statuts, conditions convenues, preuves photo, piste d'audit).", "Le contrat peut être résilié par chaque partie moyennant un préavis écrit de 30 jours ; les obligations déjà convenues par commandes restent dues."]}},
    "pl": {"generated": "Data", "between": "między", "and": "a", "reg": "Nr firmy", "city": "Miasto", "sig": "Imię i nazwisko, podpis, data", "disclaimer": "Przykładowy wzór dla wygody. Nie stanowi porady prawnej - przed użyciem skonsultuj z wykwalifikowanym specjalistą.",
           "handover": {"t": "Protokół przekazania / odbioru", "c": ["Strony potwierdzają, że opisane poniżej prace / towary zostały przekazane przez Wykonawcę i odebrane przez Zleceniodawcę:", "Przedmiot: {subject}", "Zleceniodawca dokonał oględzin przedmiotu i nie wnosi zastrzeżeń w chwili odbioru, o ile poniżej nie zaznaczono inaczej.", "Uwagi: {notes}", "Niniejszy protokół sporządzono w dwóch jednobrzmiących egzemplarzach, po jednym dla każdej strony."]},
           "nda": {"t": "Deklaracja poufności (NDA)", "c": ["Każda ze stron może przekazywać drugiej informacje biznesowe, techniczne i handlowe w związku z: {subject}.", "Strona otrzymująca wykorzystuje informacje wyłącznie w powyższym celu i chroni je co najmniej z taką starannością, jak własne informacje poufne.", "Obowiązki poufności obowiązują przez 3 lata po ostatniej wymianie informacji.", "Informacje publiczne oraz te, których ujawnienia wymaga prawo, nie podlegają niniejszej deklaracji."]},
           "service": {"t": "Umowa ramowa o świadczenie usług", "c": ["Wykonawca zobowiązuje się świadczyć na rzecz Zleceniodawcy następujące usługi: {subject}.", "Konkretne zamówienia, wolumeny, ceny i terminy są uzgadniane per zamówienie we wspólnej przestrzeni Seam, a każda dwustronna akceptacja stanowi integralną część niniejszej umowy.", "Płatność jest należna zgodnie z warunkami uzgodnionymi dla każdego zamówienia, chyba że uzgodniono inaczej na piśmie.", "Strony dokumentują postęp za pomocą narzędzi platformy (statusy, uzgodnione warunki, dowody zdjęciowe, ścieżka audytu).", "Umowa może zostać rozwiązana przez każdą ze stron z 30-dniowym pisemnym wypowiedzeniem; już uzgodnione zobowiązania per zamówienia pozostają należne."]}},
    "uk": {"generated": "Дата", "between": "між", "and": "та", "reg": "Код компанії", "city": "Місто", "sig": "Ім'я, підпис, дата", "disclaimer": "Зразок для зручності. Не є юридичною консультацією - перевірте з кваліфікованим фахівцем перед використанням.",
           "handover": {"t": "Акт приймання-передачі", "c": ["Сторони підтверджують, що описані нижче роботи / товари передані Виконавцем і прийняті Замовником:", "Предмет: {subject}", "Замовник оглянув предмет і не має заперечень на момент приймання, якщо нижче не зазначено інше.", "Примітки: {notes}", "Цей акт складено у двох однакових примірниках - по одному для кожної сторони."]},
           "nda": {"t": "Декларація про конфіденційність (NDA)", "c": ["Кожна сторона може передавати іншій ділову, технічну та комерційну інформацію у зв'язку з: {subject}.", "Сторона, яка отримує інформацію, використовує її виключно для зазначеної мети та захищає щонайменше з тією ж дбайливістю, що й власну конфіденційну інформацію.", "Зобов'язання щодо конфіденційності діють 3 роки після останнього обміну інформацією.", "Публічна інформація та інформація, розкриття якої вимагає закон, не є предметом цієї декларації."]},
           "service": {"t": "Рамковий договір про надання послуг", "c": ["Виконавець зобов'язується надавати Замовнику такі послуги: {subject}.", "Конкретні замовлення, обсяги, ціни та терміни узгоджуються за замовленнями у спільному просторі Seam, і кожне двостороннє схвалення там є невід'ємною частиною цього договору.", "Оплата здійснюється згідно з умовами, узгодженими за кожним замовленням, якщо письмово не погоджено інше.", "Сторони документують прогрес інструментами платформи (статуси, узгоджені умови, фотодокази, аудиторський слід).", "Договір може бути розірваний будь-якою стороною з письмовим повідомленням за 30 днів; уже узгоджені за замовленнями зобов'язання залишаються чинними."]}},
    "pt": {"generated": "Data", "between": "entre", "and": "e", "reg": "N.º de empresa", "city": "Cidade", "sig": "Nome, assinatura, data", "disclaimer": "Modelo de exemplo para sua comodidade. Não constitui aconselhamento jurídico - reveja com um profissional qualificado antes de utilizar.",
           "handover": {"t": "Auto de entrega / receção", "c": ["As partes confirmam que os trabalhos / bens abaixo descritos foram entregues pelo Prestador e aceites pelo Adjudicante:", "Objeto: {subject}", "O Adjudicante inspecionou o objeto e não tem objeções no momento da receção, salvo indicação em contrário abaixo.", "Notas: {notes}", "O presente auto é elaborado em dois exemplares idênticos, um para cada parte."]},
           "nda": {"t": "Declaração de confidencialidade (NDA)", "c": ["Cada parte pode partilhar com a outra informações empresariais, técnicas e comerciais relacionadas com: {subject}.", "A parte recetora utiliza as informações exclusivamente para o fim acima e protege-as com, pelo menos, o cuidado aplicado às suas próprias informações confidenciais.", "As obrigações de confidencialidade mantêm-se em vigor durante 3 anos após a última troca de informações.", "As informações públicas e aquelas cuja divulgação seja exigida por lei não são objeto da presente declaração."]},
           "service": {"t": "Contrato-quadro de serviços", "c": ["O Prestador obriga-se a prestar ao Adjudicante os seguintes serviços: {subject}.", "As encomendas concretas, volumes, preços e prazos são acordados por encomenda no espaço partilhado do Seam, e cada aprovação bilateral aí constitui parte integrante do presente contrato.", "O pagamento é devido nos termos acordados em cada encomenda, salvo acordo escrito em contrário.", "As partes documentam o progresso com as ferramentas da plataforma (estados, condições acordadas, provas fotográficas, rasto de auditoria).", "O contrato pode ser denunciado por qualquer das partes com aviso prévio escrito de 30 dias; as obrigações já acordadas por encomendas mantêm-se devidas."]}},
    "bg": {"generated": "Дата", "between": "между", "and": "и", "reg": "ЕИК", "city": "Град", "sig": "Име, подпис, дата", "disclaimer": "Примерен образец за улеснение. Не представлява правен съвет - прегледайте с квалифициран специалист преди употреба.",
           "handover": {"t": "Приемо-предавателен протокол", "c": ["Страните удостоверяват, че описаните по-долу работи / стоки бяха предадени от Изпълнителя и приети от Възложителя:", "Предмет: {subject}", "Възложителят е прегледал предмета и няма възражения към момента на приемането, освен ако по-долу не е отбелязано друго.", "Забележки: {notes}", "Протоколът се състави в два еднообразни екземпляра - по един за всяка страна."]},
           "nda": {"t": "Декларация за конфиденциалност (NDA)", "c": ["Всяка от страните може да споделя с другата бизнес, техническа и търговска информация във връзка с: {subject}.", "Получаващата страна използва информацията единствено за горната цел и я защитава поне с грижата, полагана за собствената ѝ поверителна информация.", "Задълженията за поверителност остават в сила 3 години след последния обмен на информация.", "Публична информация и информация, чието разкриване се изисква по закон, не е предмет на тази декларация."]},
           "service": {"t": "Рамков договор за услуги", "c": ["Изпълнителят се задължава да извършва за Възложителя следните услуги: {subject}.", "Конкретните поръчки, обеми, цени и срокове се договарят по поръчки в общото пространство в Seam, като всяко двустранно одобрение там е неразделна част от този договор.", "Плащането се дължи съгласно условията, договорени по всяка поръчка, освен ако писмено не е уговорено друго.", "Страните документират напредъка с инструментите на платформата (статуси, договорени условия, снимков материал, одитна следа).", "Договорът може да бъде прекратен от всяка страна с 30-дневно писмено предизвестие; вече договорените по поръчки задължения остават дължими."]}},
    "de": {"generated": "Datum", "between": "zwischen", "and": "und", "reg": "Firmen-Nr.", "city": "Stadt", "sig": "Name, Unterschrift, Datum", "disclaimer": "Mustervorlage zur Erleichterung. Keine Rechtsberatung - vor Verwendung fachlich prüfen lassen.",
           "handover": {"t": "Übergabe-/Abnahmeprotokoll", "c": ["Die Parteien bestätigen, dass die unten beschriebenen Arbeiten / Waren vom Auftragnehmer übergeben und vom Auftraggeber abgenommen wurden:", "Gegenstand: {subject}", "Der Auftraggeber hat den Gegenstand geprüft und erhebt zum Zeitpunkt der Abnahme keine Einwände, sofern unten nichts anderes vermerkt ist.", "Anmerkungen: {notes}", "Dieses Protokoll wurde in zwei gleichlautenden Exemplaren erstellt - eines je Partei."]},
           "nda": {"t": "Vertraulichkeitserklärung (NDA)", "c": ["Jede Partei kann der anderen geschäftliche, technische und kommerzielle Informationen im Zusammenhang mit: {subject} übermitteln.", "Die empfangende Partei nutzt die Informationen ausschließlich für den obigen Zweck und schützt sie mindestens mit der Sorgfalt eigener vertraulicher Informationen.", "Die Vertraulichkeitspflichten gelten 3 Jahre nach dem letzten Informationsaustausch fort.", "Öffentliche Informationen und gesetzlich offenzulegende Informationen sind nicht Gegenstand dieser Erklärung."]},
           "service": {"t": "Rahmenvertrag über Dienstleistungen", "c": ["Der Auftragnehmer verpflichtet sich, für den Auftraggeber folgende Leistungen zu erbringen: {subject}.", "Konkrete Aufträge, Mengen, Preise und Fristen werden je Auftrag im gemeinsamen Seam-Arbeitsbereich vereinbart; jede beidseitige Freigabe dort ist Bestandteil dieses Vertrags.", "Die Zahlung erfolgt gemäß den je Auftrag vereinbarten Konditionen, sofern nicht schriftlich anders vereinbart.", "Die Parteien dokumentieren den Fortschritt mit den Werkzeugen der Plattform (Status, Konditionen, Fotonachweise, Audit-Trail).", "Der Vertrag kann von jeder Partei mit 30-tägiger schriftlicher Frist gekündigt werden; bereits vereinbarte Auftragspflichten bleiben bestehen."]}},
    "ro": {"generated": "Data", "between": "între", "and": "și", "reg": "CUI", "city": "Oraș", "sig": "Nume, semnătură, data", "disclaimer": "Model orientativ pentru comoditate. Nu constituie consultanță juridică - verificați cu un specialist înainte de utilizare.",
           "handover": {"t": "Proces-verbal de predare-primire", "c": ["Părțile confirmă că lucrările / bunurile descrise mai jos au fost predate de Executant și recepționate de Beneficiar:", "Obiect: {subject}", "Beneficiarul a inspectat obiectul și nu are obiecții la momentul recepției, dacă nu se notează altfel mai jos.", "Observații: {notes}", "Prezentul proces-verbal a fost întocmit în două exemplare identice, câte unul pentru fiecare parte."]},
           "nda": {"t": "Declarație de confidențialitate (NDA)", "c": ["Fiecare parte poate transmite celeilalte informații de afaceri, tehnice și comerciale în legătură cu: {subject}.", "Partea care primește informațiile le folosește exclusiv în scopul de mai sus și le protejează cel puțin cu grija aplicată propriilor informații confidențiale.", "Obligațiile de confidențialitate rămân în vigoare 3 ani după ultimul schimb de informații.", "Informațiile publice și cele a căror divulgare este cerută de lege nu fac obiectul acestei declarații."]},
           "service": {"t": "Contract-cadru de servicii", "c": ["Executantul se obligă să presteze pentru Beneficiar următoarele servicii: {subject}.", "Comenzile concrete, volumele, prețurile și termenele se agreează per comandă în spațiul comun Seam; fiecare aprobare bilaterală de acolo face parte integrantă din contract.", "Plata se datorează conform condițiilor agreate per comandă, dacă nu se convine altfel în scris.", "Părțile documentează progresul cu instrumentele platformei (statusuri, condiții agreate, dovezi foto, audit).", "Contractul poate fi denunțat de oricare parte cu preaviz scris de 30 de zile; obligațiile deja agreate per comenzi rămân datorate."]}},
    "el": {"generated": "Ημερομηνία", "between": "μεταξύ", "and": "και", "reg": "ΑΦΜ", "city": "Πόλη", "sig": "Όνομα, υπογραφή, ημερομηνία", "disclaimer": "Ενδεικτικό υπόδειγμα για διευκόλυνση. Δεν αποτελεί νομική συμβουλή - ελέγξτε με ειδικό πριν τη χρήση.",
           "handover": {"t": "Πρωτόκολλο παράδοσης-παραλαβής", "c": ["Τα μέρη βεβαιώνουν ότι οι εργασίες / τα αγαθά που περιγράφονται παρακάτω παραδόθηκαν από τον Ανάδοχο και παραλήφθηκαν από τον Αναθέτοντα:", "Αντικείμενο: {subject}", "Ο Αναθέτων επιθεώρησε το αντικείμενο και δεν έχει αντιρρήσεις κατά την παραλαβή, εκτός αν σημειώνεται διαφορετικά παρακάτω.", "Παρατηρήσεις: {notes}", "Το παρόν συντάχθηκε σε δύο όμοια αντίτυπα, ένα για κάθε μέρος."]},
           "nda": {"t": "Δήλωση εμπιστευτικότητας (NDA)", "c": ["Κάθε μέρος μπορεί να μοιράζεται με το άλλο επιχειρηματικές, τεχνικές και εμπορικές πληροφορίες σχετικά με: {subject}.", "Το λαμβάνον μέρος χρησιμοποιεί τις πληροφορίες αποκλειστικά για τον παραπάνω σκοπό και τις προστατεύει τουλάχιστον με τη φροντίδα των δικών του εμπιστευτικών πληροφοριών.", "Οι υποχρεώσεις εμπιστευτικότητας ισχύουν για 3 έτη μετά την τελευταία ανταλλαγή πληροφοριών.", "Δημόσιες πληροφορίες και πληροφορίες που απαιτείται εκ του νόμου να γνωστοποιηθούν δεν υπόκεινται στην παρούσα."]},
           "service": {"t": "Σύμβαση-πλαίσιο υπηρεσιών", "c": ["Ο Ανάδοχος αναλαμβάνει να παρέχει στον Αναθέτοντα τις εξής υπηρεσίες: {subject}.", "Οι συγκεκριμένες παραγγελίες, όγκοι, τιμές και προθεσμίες συμφωνούνται ανά παραγγελία στον κοινό χώρο Seam· κάθε αμφίπλευρη έγκριση εκεί αποτελεί αναπόσπαστο μέρος της σύμβασης.", "Η πληρωμή οφείλεται σύμφωνα με τους όρους κάθε παραγγελίας, εκτός αν συμφωνηθεί άλλως εγγράφως.", "Τα μέρη τεκμηριώνουν την πρόοδο με τα εργαλεία της πλατφόρμας (καταστάσεις, όροι, φωτογραφίες, ίχνος ελέγχου).", "Η σύμβαση μπορεί να καταγγελθεί από κάθε μέρος με έγγραφη προειδοποίηση 30 ημερών· οι ήδη συμφωνημένες υποχρεώσεις παραμένουν."]}},
    "tr": {"generated": "Tarih", "between": "taraflar", "and": "ve", "reg": "Şirket no", "city": "Şehir", "sig": "Ad, imza, tarih", "disclaimer": "Kolaylık için örnek şablon. Hukuki tavsiye değildir - kullanmadan önce uzmana danışın.",
           "handover": {"t": "Teslim-tesellüm tutanağı", "c": ["Taraflar, aşağıda tanımlanan iş / malların Yüklenici tarafından teslim edildiğini ve İşveren tarafından kabul edildiğini onaylar:", "Konu: {subject}", "İşveren konuyu incelemiş olup, aşağıda aksi belirtilmedikçe kabul anında itirazı yoktur.", "Notlar: {notes}", "Bu tutanak her taraf için birer adet olmak üzere iki nüsha düzenlenmiştir."]},
           "nda": {"t": "Gizlilik beyanı (NDA)", "c": ["Taraflardan her biri, {subject} ile bağlantılı ticari, teknik ve iş bilgilerini diğeriyle paylaşabilir.", "Bilgiyi alan taraf, bilgiyi yalnızca yukarıdaki amaç için kullanır ve en az kendi gizli bilgilerine gösterdiği özenle korur.", "Gizlilik yükümlülükleri son bilgi alışverişinden itibaren 3 yıl geçerlidir.", "Kamuya açık bilgiler ve yasal olarak açıklanması gereken bilgiler bu beyanın kapsamı dışındadır."]},
           "service": {"t": "Çerçeve hizmet sözleşmesi", "c": ["Yüklenici, İşveren için şu hizmetleri sunmayı taahhüt eder: {subject}.", "Somut siparişler, hacimler, fiyatlar ve süreler ortak Seam alanında sipariş bazında kararlaştırılır; oradaki her çift taraflı onay bu sözleşmenin ayrılmaz parçasıdır.", "Ödeme, yazılı olarak aksi kararlaştırılmadıkça her siparişte anlaşılan koşullara göre yapılır.", "Taraflar ilerlemeyi platform araçlarıyla belgeler (durumlar, koşullar, fotoğraflar, denetim izi).", "Sözleşme, 30 gün önceden yazılı bildirimle her tarafça feshedilebilir; sipariş bazında kararlaştırılmış yükümlülükler geçerli kalır."]}},
    "it": {"generated": "Data", "between": "tra", "and": "e", "reg": "P.IVA", "city": "Città", "sig": "Nome, firma, data", "disclaimer": "Modello esemplificativo per comodità. Non costituisce consulenza legale - verificare con un professionista prima dell'uso.",
           "handover": {"t": "Verbale di consegna e accettazione", "c": ["Le parti confermano che i lavori / beni descritti di seguito sono stati consegnati dall'Esecutore e accettati dal Committente:", "Oggetto: {subject}", "Il Committente ha ispezionato l'oggetto e non ha obiezioni al momento dell'accettazione, salvo quanto annotato di seguito.", "Note: {notes}", "Il presente verbale è redatto in due esemplari identici, uno per ciascuna parte."]},
           "nda": {"t": "Dichiarazione di riservatezza (NDA)", "c": ["Ciascuna parte può condividere con l'altra informazioni aziendali, tecniche e commerciali relative a: {subject}.", "La parte ricevente utilizza le informazioni esclusivamente per lo scopo di cui sopra e le protegge almeno con la cura riservata alle proprie informazioni riservate.", "Gli obblighi di riservatezza restano in vigore per 3 anni dopo l'ultimo scambio di informazioni.", "Le informazioni pubbliche e quelle da divulgare per legge non sono oggetto della presente dichiarazione."]},
           "service": {"t": "Contratto quadro di servizi", "c": ["L'Esecutore si impegna a svolgere per il Committente i seguenti servizi: {subject}.", "Ordini concreti, volumi, prezzi e scadenze si concordano per ordine nello spazio condiviso Seam; ogni approvazione bilaterale ne costituisce parte integrante.", "Il pagamento è dovuto secondo le condizioni concordate per ciascun ordine, salvo diverso accordo scritto.", "Le parti documentano l'avanzamento con gli strumenti della piattaforma (stati, condizioni, prove fotografiche, audit trail).", "Il contratto può essere risolto da ciascuna parte con preavviso scritto di 30 giorni; restano dovuti gli obblighi già concordati."]}},
    "ru": {"generated": "Дата", "between": "между", "and": "и", "reg": "Рег. №", "city": "Город", "sig": "ФИО, подпись, дата", "disclaimer": "Примерный шаблон для удобства. Не является юридической консультацией - проверьте со специалистом перед использованием.",
           "handover": {"t": "Акт приёма-передачи", "c": ["Стороны подтверждают, что описанные ниже работы / товары переданы Исполнителем и приняты Заказчиком:", "Предмет: {subject}", "Заказчик осмотрел предмет и на момент приёмки возражений не имеет, если ниже не указано иное.", "Примечания: {notes}", "Акт составлен в двух идентичных экземплярах - по одному для каждой стороны."]},
           "nda": {"t": "Декларация о конфиденциальности (NDA)", "c": ["Каждая сторона может передавать другой деловую, техническую и коммерческую информацию в связи с: {subject}.", "Получающая сторона использует информацию исключительно для указанной цели и защищает её не менее тщательно, чем собственную конфиденциальную информацию.", "Обязательства о конфиденциальности действуют 3 года после последнего обмена информацией.", "Публичная информация и информация, подлежащая раскрытию по закону, не является предметом настоящей декларации."]},
           "service": {"t": "Рамочный договор оказания услуг", "c": ["Исполнитель обязуется оказывать Заказчику следующие услуги: {subject}.", "Конкретные заказы, объёмы, цены и сроки согласуются по каждому заказу в общем пространстве Seam; каждое двустороннее согласование там является неотъемлемой частью договора.", "Оплата производится на условиях, согласованных по каждому заказу, если письменно не согласовано иное.", "Стороны документируют прогресс инструментами платформы (статусы, условия, фотоподтверждения, аудит).", "Договор может быть расторгнут любой стороной с письменным уведомлением за 30 дней; уже согласованные по заказам обязательства сохраняются."]}},
}


# Extended document kinds (concise, per-language skeletons).
DOC_EXTRA = {
    "en": {
        "poa": {"t": "Power of attorney", "c": ["{a} authorises {b} to represent it in connection with: {subject}.", "The attorney may sign, submit and receive documents within the scope above.", "This authorisation is valid until: {notes}, or until revoked in writing.", "Sub-delegation is not permitted without express written consent."]},
        "subcontract": {"t": "Subcontract agreement", "c": ["Party A assigns and Party B accepts to perform as subcontractor: {subject}.", "Party B performs with professional care, following Party A's instructions and applicable regulations.", "Stage acceptance is documented in the shared Seam workspace (statuses, photo evidence, protocols), which form an integral part of this agreement.", "Prices and deadlines follow the per-order terms approved in the platform.", "Party B may not re-assign the work without written consent."]},
        "claim": {"t": "Defect / claim protocol", "c": ["Upon acceptance or use the following non-conformities were established: {subject}.", "Evidence (photos/video) is attached to the record in Seam.", "The parties agree the defects shall be remedied within: {notes}.", "This protocol does not deprive the parties of other contractual or statutory rights."]},
        "gdpr": {"t": "Personal data processing declaration (GDPR)", "c": ["The parties process personal data only for the purposes of: {subject}.", "Each party acts as an independent controller and ensures a lawful basis plus technical and organisational measures.", "Data is not disclosed to third parties beyond what is necessary or legally required.", "In case of a data incident the affected party notifies the other without undue delay.", "Data subjects may exercise their GDPR rights before either party."]},
        "site": {"t": "Site opening / access protocol", "c": ["Party A grants Party B access to the site: {subject}.", "The site has been inspected; its condition is documented with photos in Seam.", "Party B complies with safety requirements and house rules; access: {notes}.", "Deadlines under the assignment run from the date of this protocol."]},
    },
    "es": {
        "poa": {"t": "Poder notarial", "c": ["{a} autoriza a {b} a representarle en relación con: {subject}.", "El apoderado puede firmar, presentar y recibir documentos dentro del alcance anterior.", "Esta autorización es válida hasta: {notes}, o hasta su revocación por escrito.", "No se permite la subdelegación sin consentimiento expreso por escrito."]},
        "subcontract": {"t": "Contrato de subcontratación", "c": ["La Parte A encarga y la Parte B acepta ejecutar como subcontratista: {subject}.", "La Parte B ejecuta con diligencia profesional, siguiendo las instrucciones de la Parte A y la normativa aplicable.", "La aceptación de las etapas se documenta en el espacio compartido de Seam (estados, evidencia fotográfica, actas), que forman parte integrante de este contrato.", "Los precios y plazos siguen las condiciones aprobadas por pedido en la plataforma.", "La Parte B no puede reasignar el trabajo sin consentimiento por escrito."]},
        "claim": {"t": "Acta de defectos / reclamación", "c": ["En la aceptación o el uso se establecieron las siguientes no conformidades: {subject}.", "Las evidencias (fotos/vídeo) están adjuntas al registro en Seam.", "Las partes acuerdan que los defectos se subsanarán en un plazo de: {notes}.", "La presente acta no priva a las partes de otros derechos contractuales o legales."]},
        "gdpr": {"t": "Declaración de tratamiento de datos personales (RGPD)", "c": ["Las partes tratan datos personales únicamente para los fines de: {subject}.", "Cada parte actúa como responsable independiente y garantiza una base jurídica más medidas técnicas y organizativas.", "Los datos no se comunican a terceros más allá de lo necesario o legalmente exigido.", "En caso de incidente de datos, la parte afectada notifica a la otra sin dilación indebida.", "Los interesados pueden ejercer sus derechos RGPD ante cualquiera de las partes."]},
        "site": {"t": "Acta de apertura de obra / acceso", "c": ["La Parte A concede a la Parte B acceso a la obra: {subject}.", "La obra ha sido inspeccionada; su estado está documentado con fotos en Seam.", "La Parte B cumple los requisitos de seguridad y el reglamento interno; acceso: {notes}.", "Los plazos del encargo corren desde la fecha de la presente acta."]}},
    "fr": {
        "poa": {"t": "Procuration", "c": ["{a} autorise {b} à le représenter en lien avec : {subject}.", "Le mandataire peut signer, soumettre et recevoir des documents dans le cadre ci-dessus.", "Cette autorisation est valable jusqu'au : {notes}, ou jusqu'à révocation écrite.", "La subdélégation n'est pas autorisée sans consentement écrit exprès."]},
        "subcontract": {"t": "Contrat de sous-traitance", "c": ["La Partie A confie et la Partie B accepte d'exécuter en tant que sous-traitant : {subject}.", "La Partie B exécute avec diligence professionnelle, selon les instructions de la Partie A et la réglementation applicable.", "La réception des étapes est documentée dans l'espace partagé Seam (statuts, preuves photo, procès-verbaux), qui font partie intégrante du contrat.", "Les prix et délais suivent les conditions approuvées par commande sur la plateforme.", "La Partie B ne peut pas réattribuer les travaux sans consentement écrit."]},
        "claim": {"t": "Procès-verbal de défauts / réclamation", "c": ["Lors de la réception ou de l'utilisation, les non-conformités suivantes ont été constatées : {subject}.", "Les preuves (photos/vidéo) sont jointes à l'enregistrement dans Seam.", "Les parties conviennent que les défauts seront corrigés dans un délai de : {notes}.", "Le présent procès-verbal ne prive pas les parties d'autres droits contractuels ou légaux."]},
        "gdpr": {"t": "Déclaration de traitement des données personnelles (RGPD)", "c": ["Les parties traitent des données personnelles uniquement aux fins de : {subject}.", "Chaque partie agit en tant que responsable indépendant et garantit une base légale ainsi que des mesures techniques et organisationnelles.", "Les données ne sont pas communiquées à des tiers au-delà du nécessaire ou de l'exigence légale.", "En cas d'incident de données, la partie concernée informe l'autre sans retard injustifié.", "Les personnes concernées peuvent exercer leurs droits RGPD auprès de chaque partie."]},
        "site": {"t": "Procès-verbal d'ouverture de chantier / accès", "c": ["La Partie A accorde à la Partie B l'accès au site : {subject}.", "Le site a été inspecté ; son état est documenté par des photos dans Seam.", "La Partie B respecte les exigences de sécurité et le règlement intérieur ; accès : {notes}.", "Les délais de la mission courent à compter de la date du présent procès-verbal."]}},
    "pl": {
        "poa": {"t": "Pełnomocnictwo", "c": ["{a} upoważnia {b} do reprezentowania go w związku z: {subject}.", "Pełnomocnik może podpisywać, składać i odbierać dokumenty w powyższym zakresie.", "Niniejsze pełnomocnictwo jest ważne do: {notes}, lub do pisemnego odwołania.", "Substytucja jest niedozwolona bez wyraźnej pisemnej zgody."]},
        "subcontract": {"t": "Umowa podwykonawstwa", "c": ["Strona A zleca, a Strona B przyjmuje do wykonania jako podwykonawca: {subject}.", "Strona B wykonuje z profesjonalną starannością, zgodnie z instrukcjami Strony A i obowiązującymi przepisami.", "Odbiór etapów dokumentuje się we wspólnej przestrzeni Seam (statusy, dowody zdjęciowe, protokoły), które stanowią integralną część umowy.", "Ceny i terminy wynikają z warunków zatwierdzonych per zamówienie na platformie.", "Strona B nie może przekazywać prac dalej bez pisemnej zgody."]},
        "claim": {"t": "Protokół usterek / reklamacja", "c": ["Przy odbiorze lub w użytkowaniu stwierdzono następujące niezgodności: {subject}.", "Dowody (zdjęcia/wideo) są dołączone do zapisu w Seam.", "Strony uzgadniają usunięcie usterek w terminie: {notes}.", "Niniejszy protokół nie pozbawia stron innych praw umownych ani ustawowych."]},
        "gdpr": {"t": "Deklaracja przetwarzania danych osobowych (RODO)", "c": ["Strony przetwarzają dane osobowe wyłącznie w celach: {subject}.", "Każda strona działa jako niezależny administrator i zapewnia podstawę prawną oraz środki techniczne i organizacyjne.", "Dane nie są ujawniane osobom trzecim poza tym, co niezbędne lub wymagane prawem.", "W razie incydentu z danymi strona dotknięta niezwłocznie powiadamia drugą.", "Osoby, których dane dotyczą, mogą realizować swoje prawa RODO wobec każdej ze stron."]},
        "site": {"t": "Protokół otwarcia budowy / dostępu", "c": ["Strona A udziela Stronie B dostępu do obiektu: {subject}.", "Obiekt został skontrolowany; jego stan udokumentowano zdjęciami w Seam.", "Strona B przestrzega wymogów bezpieczeństwa i regulaminu; dostęp: {notes}.", "Terminy zlecenia biegną od daty niniejszego protokołu."]}},
    "uk": {
        "poa": {"t": "Довіреність", "c": ["{a} уповноважує {b} представляти його у зв'язку з: {subject}.", "Повірений може підписувати, подавати та отримувати документи в межах зазначеного обсягу.", "Ця довіреність дійсна до: {notes}, або до письмового відкликання.", "Передоручення не допускається без прямої письмової згоди."]},
        "subcontract": {"t": "Договір субпідряду", "c": ["Сторона А доручає, а Сторона Б приймає до виконання як субпідрядник: {subject}.", "Сторона Б виконує з професійною дбайливістю, за вказівками Сторони А та згідно з чинними нормами.", "Приймання етапів документується у спільному просторі Seam (статуси, фотодокази, акти), які є невід'ємною частиною договору.", "Ціни та терміни відповідають умовам, схваленим за замовленнями на платформі.", "Сторона Б не має права передоручати роботи без письмової згоди."]},
        "claim": {"t": "Акт про дефекти / рекламація", "c": ["Під час приймання або експлуатації виявлено такі невідповідності: {subject}.", "Докази (фото/відео) додані до запису в Seam.", "Сторони погоджують усунення дефектів у строк: {notes}.", "Цей акт не позбавляє сторони інших договірних або законних прав."]},
        "gdpr": {"t": "Декларація про обробку персональних даних (GDPR)", "c": ["Сторони обробляють персональні дані виключно для цілей: {subject}.", "Кожна сторона діє як самостійний контролер і забезпечує правову підставу та технічні й організаційні заходи.", "Дані не передаються третім особам понад необхідне або вимагане законом.", "У разі інциденту з даними постраждала сторона повідомляє іншу без невиправданої затримки.", "Суб'єкти даних можуть реалізувати свої права за GDPR перед будь-якою зі сторін."]},
        "site": {"t": "Акт відкриття об'єкта / доступу", "c": ["Сторона А надає Стороні Б доступ до об'єкта: {subject}.", "Об'єкт оглянуто; його стан задокументовано фотографіями в Seam.", "Сторона Б дотримується вимог безпеки та внутрішнього розпорядку; доступ: {notes}.", "Строки за дорученням обчислюються від дати цього акта."]}},
    "pt": {
        "poa": {"t": "Procuração", "c": ["{a} autoriza {b} a representá-lo em relação a: {subject}.", "O procurador pode assinar, submeter e receber documentos no âmbito acima.", "Esta autorização é válida até: {notes}, ou até revogação por escrito.", "A subdelegação não é permitida sem consentimento expresso por escrito."]},
        "subcontract": {"t": "Contrato de subcontratação", "c": ["A Parte A adjudica e a Parte B aceita executar como subcontratada: {subject}.", "A Parte B executa com diligência profissional, seguindo as instruções da Parte A e a regulamentação aplicável.", "A receção das etapas é documentada no espaço partilhado do Seam (estados, provas fotográficas, autos), que fazem parte integrante do presente contrato.", "Os preços e prazos seguem as condições aprovadas por encomenda na plataforma.", "A Parte B não pode subcontratar os trabalhos sem consentimento por escrito."]},
        "claim": {"t": "Auto de defeitos / reclamação", "c": ["Na receção ou utilização foram constatadas as seguintes não conformidades: {subject}.", "As provas (fotos/vídeo) estão anexadas ao registo no Seam.", "As partes acordam que os defeitos serão corrigidos no prazo de: {notes}.", "O presente auto não priva as partes de outros direitos contratuais ou legais."]},
        "gdpr": {"t": "Declaração de tratamento de dados pessoais (RGPD)", "c": ["As partes tratam dados pessoais apenas para as finalidades de: {subject}.", "Cada parte atua como responsável independente e assegura um fundamento jurídico, bem como medidas técnicas e organizativas.", "Os dados não são divulgados a terceiros para além do necessário ou legalmente exigido.", "Em caso de incidente de dados, a parte afetada notifica a outra sem demora injustificada.", "Os titulares dos dados podem exercer os seus direitos ao abrigo do RGPD perante qualquer das partes."]},
        "site": {"t": "Auto de abertura de obra / acesso", "c": ["A Parte A concede à Parte B acesso à obra: {subject}.", "A obra foi inspecionada; o seu estado está documentado com fotografias no Seam.", "A Parte B cumpre os requisitos de segurança e o regulamento interno; acesso: {notes}.", "Os prazos da adjudicação contam-se a partir da data do presente auto."]}},
    "bg": {
        "poa": {"t": "Пълномощно", "c": ["{a} упълномощава {b} да го представлява във връзка с: {subject}.", "Пълномощникът може да подписва, подава и получава документи в рамките на горния обхват.", "Пълномощното е валидно до: {notes}, или до писмено оттегляне.", "Преупълномощаване не се допуска без изрично писмено съгласие."]},
        "subcontract": {"t": "Договор за подизпълнение", "c": ["Страна А възлага, а Страна Б приема да изпълни като подизпълнител: {subject}.", "Страна Б изпълнява с грижата на добър търговец, при указанията на Страна А и приложимите нормативни изисквания.", "Приемането на етапите се документира в общото пространство в Seam (статуси, снимков материал, протоколи), които са неразделна част от договора.", "Цените и сроковете следват одобрените в платформата условия по поръчки.", "Страна Б няма право да превъзлага работата без писмено съгласие."]},
        "claim": {"t": "Протокол за дефекти / рекламация", "c": ["При приемане или експлоатация се установиха следните несъответствия: {subject}.", "Доказателствата (снимки/видео) са прикачени към записа в Seam.", "Страните се споразумяват несъответствията да бъдат отстранени в срок: {notes}.", "Настоящият протокол не лишава страните от други договорни или законови права."]},
        "gdpr": {"t": "Декларация за обработване на лични данни (GDPR)", "c": ["Страните обработват лични данни единствено за целите на: {subject}.", "Всяка страна действа като самостоятелен администратор и осигурява законово основание и технически и организационни мерки.", "Данни не се предоставят на трети лица извън необходимото или законово изискуемото.", "При инцидент с данни засегнатата страна уведомява другата без излишно забавяне.", "Субектите на данни могат да упражняват правата си по GDPR пред всяка от страните."]},
        "site": {"t": "Протокол за откриване на обект / достъп", "c": ["Страна А предоставя на Страна Б достъп до обект: {subject}.", "Обектът е огледан; състоянието му е документирано със снимки в Seam.", "Страна Б спазва изискванията за безопасност и вътрешния ред; достъп: {notes}.", "Сроковете по възлагането текат от датата на настоящия протокол."]},
    },
    "de": {
        "poa": {"t": "Vollmacht", "c": ["{a} bevollmächtigt {b}, es zu vertreten im Zusammenhang mit: {subject}.", "Der Bevollmächtigte darf im obigen Rahmen Dokumente unterzeichnen, einreichen und entgegennehmen.", "Die Vollmacht gilt bis: {notes}, oder bis zum schriftlichen Widerruf.", "Untervollmachten sind ohne ausdrückliche schriftliche Zustimmung nicht zulässig."]},
        "subcontract": {"t": "Nachunternehmervertrag", "c": ["Partei A beauftragt und Partei B übernimmt als Nachunternehmer: {subject}.", "Partei B leistet mit professioneller Sorgfalt nach Weisungen von Partei A und geltenden Vorschriften.", "Die Abnahme der Phasen wird im gemeinsamen Seam-Bereich dokumentiert (Status, Fotonachweise, Protokolle) und ist Vertragsbestandteil.", "Preise und Fristen folgen den in der Plattform freigegebenen Auftragskonditionen.", "Partei B darf ohne schriftliche Zustimmung nicht weitervergeben."]},
        "claim": {"t": "Mängel-/Reklamationsprotokoll", "c": ["Bei Abnahme oder Nutzung wurden folgende Abweichungen festgestellt: {subject}.", "Nachweise (Fotos/Videos) sind dem Datensatz in Seam beigefügt.", "Die Parteien vereinbaren die Behebung innerhalb von: {notes}.", "Dieses Protokoll lässt sonstige vertragliche oder gesetzliche Rechte unberührt."]},
        "gdpr": {"t": "Erklärung zur Verarbeitung personenbezogener Daten (DSGVO)", "c": ["Die Parteien verarbeiten personenbezogene Daten nur zu den Zwecken von: {subject}.", "Jede Partei handelt als eigenständiger Verantwortlicher und gewährleistet Rechtsgrundlage sowie technische und organisatorische Maßnahmen.", "Daten werden nicht über das Erforderliche oder gesetzlich Gebotene hinaus an Dritte weitergegeben.", "Bei einem Datenvorfall informiert die betroffene Partei die andere unverzüglich.", "Betroffene können ihre DSGVO-Rechte gegenüber jeder Partei ausüben."]},
        "site": {"t": "Baustelleneröffnungs-/Zugangsprotokoll", "c": ["Partei A gewährt Partei B Zugang zum Objekt: {subject}.", "Das Objekt wurde besichtigt; der Zustand ist mit Fotos in Seam dokumentiert.", "Partei B beachtet Sicherheitsanforderungen und Hausordnung; Zugang: {notes}.", "Fristen aus der Beauftragung laufen ab dem Datum dieses Protokolls."]},
    },
    "ro": {
        "poa": {"t": "Procură", "c": ["{a} împuternicește {b} să îl reprezinte în legătură cu: {subject}.", "Împuternicitul poate semna, depune și primi documente în limitele de mai sus.", "Procura este valabilă până la: {notes}, sau până la revocarea scrisă.", "Substituirea nu este permisă fără acord scris expres."]},
        "subcontract": {"t": "Contract de subantrepriză", "c": ["Partea A atribuie, iar Partea B acceptă să execute ca subantreprenor: {subject}.", "Partea B execută cu diligență profesională, după instrucțiunile Părții A și normele aplicabile.", "Recepția etapelor se documentează în spațiul comun Seam (statusuri, dovezi foto, procese-verbale), parte integrantă a contractului.", "Prețurile și termenele urmează condițiile aprobate per comandă în platformă.", "Partea B nu poate subcontracta mai departe fără acord scris."]},
        "claim": {"t": "Proces-verbal de defecte / reclamație", "c": ["La recepție sau în exploatare s-au constatat următoarele neconformități: {subject}.", "Dovezile (foto/video) sunt atașate înregistrării din Seam.", "Părțile convin remedierea în termen de: {notes}.", "Prezentul proces-verbal nu privează părțile de alte drepturi contractuale sau legale."]},
        "gdpr": {"t": "Declarație privind prelucrarea datelor personale (GDPR)", "c": ["Părțile prelucrează date personale doar în scopurile: {subject}.", "Fiecare parte acționează ca operator independent și asigură temei legal și măsuri tehnice și organizatorice.", "Datele nu se divulgă terților dincolo de necesar sau de cerințele legale.", "În caz de incident, partea afectată notifică cealaltă parte fără întârziere.", "Persoanele vizate își pot exercita drepturile GDPR față de oricare parte."]},
        "site": {"t": "Proces-verbal de deschidere șantier / acces", "c": ["Partea A acordă Părții B acces la obiectivul: {subject}.", "Obiectivul a fost inspectat; starea este documentată cu fotografii în Seam.", "Partea B respectă cerințele de siguranță și regulamentul intern; acces: {notes}.", "Termenele din atribuire curg de la data prezentului proces-verbal."]},
    },
    "el": {
        "poa": {"t": "Πληρεξούσιο", "c": ["Ο/Η {a} εξουσιοδοτεί τον/την {b} να τον/την εκπροσωπεί σχετικά με: {subject}.", "Ο πληρεξούσιος μπορεί να υπογράφει, να υποβάλλει και να παραλαμβάνει έγγραφα στο παραπάνω πλαίσιο.", "Το πληρεξούσιο ισχύει έως: {notes}, ή έως γραπτή ανάκληση.", "Δεν επιτρέπεται περαιτέρω εξουσιοδότηση χωρίς ρητή γραπτή συναίνεση."]},
        "subcontract": {"t": "Σύμβαση υπεργολαβίας", "c": ["Το Μέρος Α αναθέτει και το Μέρος Β αναλαμβάνει ως υπεργολάβος: {subject}.", "Το Μέρος Β εκτελεί με επαγγελματική επιμέλεια, κατά τις οδηγίες του Μέρους Α και τους ισχύοντες κανόνες.", "Η παραλαβή σταδίων τεκμηριώνεται στον κοινό χώρο Seam (καταστάσεις, φωτογραφίες, πρωτόκολλα) και αποτελεί αναπόσπαστο μέρος.", "Τιμές και προθεσμίες ακολουθούν τους εγκεκριμένους όρους ανά παραγγελία.", "Το Μέρος Β δεν αναθέτει περαιτέρω χωρίς γραπτή συναίνεση."]},
        "claim": {"t": "Πρωτόκολλο ελαττωμάτων / διαμαρτυρίας", "c": ["Κατά την παραλαβή ή χρήση διαπιστώθηκαν οι εξής μη συμμορφώσεις: {subject}.", "Τα αποδεικτικά (φωτο/βίντεο) επισυνάπτονται στην εγγραφή στο Seam.", "Τα μέρη συμφωνούν αποκατάσταση εντός: {notes}.", "Το παρόν δεν στερεί από τα μέρη άλλα συμβατικά ή νόμιμα δικαιώματα."]},
        "gdpr": {"t": "Δήλωση επεξεργασίας προσωπικών δεδομένων (GDPR)", "c": ["Τα μέρη επεξεργάζονται προσωπικά δεδομένα μόνο για τους σκοπούς: {subject}.", "Κάθε μέρος ενεργεί ως ανεξάρτητος υπεύθυνος και διασφαλίζει νόμιμη βάση και τεχνικά/οργανωτικά μέτρα.", "Δεδομένα δεν γνωστοποιούνται σε τρίτους πέραν του αναγκαίου ή του νόμιμου.", "Σε περιστατικό, το θιγόμενο μέρος ειδοποιεί το άλλο χωρίς καθυστέρηση.", "Τα υποκείμενα ασκούν τα δικαιώματά τους GDPR έναντι οποιουδήποτε μέρους."]},
        "site": {"t": "Πρωτόκολλο έναρξης εργοταξίου / πρόσβασης", "c": ["Το Μέρος Α παρέχει στο Μέρος Β πρόσβαση στο έργο: {subject}.", "Το έργο επιθεωρήθηκε· η κατάσταση τεκμηριώνεται με φωτογραφίες στο Seam.", "Το Μέρος Β τηρεί τους κανόνες ασφαλείας και τον εσωτερικό κανονισμό· πρόσβαση: {notes}.", "Οι προθεσμίες της ανάθεσης τρέχουν από την ημερομηνία του παρόντος."]},
    },
    "tr": {
        "poa": {"t": "Vekaletname", "c": ["{a}, {b} tarafını şu konuda kendisini temsil etmek üzere yetkilendirir: {subject}.", "Vekil, yukarıdaki kapsamda belge imzalayabilir, sunabilir ve teslim alabilir.", "Vekaletname şu tarihe kadar geçerlidir: {notes}, veya yazılı iptal edilene dek.", "Yazılı açık onay olmadan alt yetkilendirme yapılamaz."]},
        "subcontract": {"t": "Alt yüklenici sözleşmesi", "c": ["Taraf A görevlendirir, Taraf B alt yüklenici olarak üstlenir: {subject}.", "Taraf B, Taraf A'nın talimatlarına ve geçerli mevzuata uygun, mesleki özenle çalışır.", "Aşama kabulleri ortak Seam alanında belgelenir (durumlar, fotoğraflar, tutanaklar) ve sözleşmenin ayrılmaz parçasıdır.", "Fiyat ve süreler platformda onaylanan sipariş koşullarına göredir.", "Taraf B yazılı onay olmadan işi devredemez."]},
        "claim": {"t": "Kusur / şikâyet tutanağı", "c": ["Kabul veya kullanım sırasında şu uygunsuzluklar tespit edildi: {subject}.", "Kanıtlar (foto/video) Seam kaydına eklidir.", "Taraflar giderme süresi olarak şunu kararlaştırır: {notes}.", "Bu tutanak tarafların diğer sözleşmesel veya yasal haklarını ortadan kaldırmaz."]},
        "gdpr": {"t": "Kişisel verilerin işlenmesi beyanı (GDPR/KVKK)", "c": ["Taraflar kişisel verileri yalnızca şu amaçlarla işler: {subject}.", "Her taraf bağımsız veri sorumlusudur; hukuki dayanak ile teknik ve idari tedbirleri sağlar.", "Veriler gerekli veya yasal olanın ötesinde üçüncü kişilere aktarılmaz.", "Veri ihlalinde etkilenen taraf diğerini gecikmeden bilgilendirir.", "İlgili kişiler haklarını her iki tarafa karşı kullanabilir."]},
        "site": {"t": "Şantiye açılış / erişim tutanağı", "c": ["Taraf A, Taraf B'ye şu sahaya erişim sağlar: {subject}.", "Saha incelendi; durumu Seam'de fotoğraflarla belgelendi.", "Taraf B güvenlik gereklerine ve iç düzene uyar; erişim: {notes}.", "Görevlendirmedeki süreler bu tutanak tarihinden itibaren işler."]},
    },
    "it": {
        "poa": {"t": "Procura", "c": ["{a} autorizza {b} a rappresentarlo in relazione a: {subject}.", "Il procuratore può firmare, presentare e ricevere documenti nei limiti di cui sopra.", "La procura è valida fino a: {notes}, o fino a revoca scritta.", "La sub-delega non è consentita senza espresso consenso scritto."]},
        "subcontract": {"t": "Contratto di subappalto", "c": ["La Parte A affida e la Parte B accetta di eseguire come subappaltatore: {subject}.", "La Parte B esegue con diligenza professionale, secondo le istruzioni della Parte A e le norme applicabili.", "L'accettazione delle fasi è documentata nello spazio Seam (stati, prove foto, verbali), parte integrante del contratto.", "Prezzi e termini seguono le condizioni approvate per ordine in piattaforma.", "La Parte B non può subappaltare ulteriormente senza consenso scritto."]},
        "claim": {"t": "Verbale di difetti / reclamo", "c": ["In sede di accettazione o uso sono emerse le seguenti non conformità: {subject}.", "Le prove (foto/video) sono allegate al record in Seam.", "Le parti concordano la rimozione entro: {notes}.", "Il presente verbale non priva le parti di altri diritti contrattuali o di legge."]},
        "gdpr": {"t": "Dichiarazione sul trattamento dei dati personali (GDPR)", "c": ["Le parti trattano dati personali solo per le finalità di: {subject}.", "Ciascuna parte agisce come titolare autonomo e garantisce base giuridica e misure tecniche e organizzative.", "I dati non sono comunicati a terzi oltre il necessario o il dovuto per legge.", "In caso di incidente, la parte interessata avvisa l'altra senza ritardo.", "Gli interessati possono esercitare i diritti GDPR verso ciascuna parte."]},
        "site": {"t": "Verbale di apertura cantiere / accesso", "c": ["La Parte A concede alla Parte B l'accesso al sito: {subject}.", "Il sito è stato ispezionato; lo stato è documentato con foto in Seam.", "La Parte B rispetta i requisiti di sicurezza e il regolamento interno; accesso: {notes}.", "I termini dell'incarico decorrono dalla data del presente verbale."]},
    },
    "ru": {
        "poa": {"t": "Доверенность", "c": ["{a} уполномочивает {b} представлять его в связи с: {subject}.", "Поверенный может подписывать, подавать и получать документы в указанных пределах.", "Доверенность действует до: {notes}, либо до письменного отзыва.", "Передоверие не допускается без прямого письменного согласия."]},
        "subcontract": {"t": "Договор субподряда", "c": ["Сторона А поручает, а Сторона Б принимает выполнение как субподрядчик: {subject}.", "Сторона Б выполняет с профессиональной тщательностью, по указаниям Стороны А и применимым нормам.", "Приёмка этапов документируется в общем пространстве Seam (статусы, фото, акты) и является неотъемлемой частью договора.", "Цены и сроки следуют условиям, согласованным по заказам в платформе.", "Сторона Б не вправе перепоручать работу без письменного согласия."]},
        "claim": {"t": "Акт о дефектах / рекламация", "c": ["При приёмке или эксплуатации установлены следующие несоответствия: {subject}.", "Доказательства (фото/видео) приложены к записи в Seam.", "Стороны согласуют устранение в срок: {notes}.", "Настоящий акт не лишает стороны иных договорных или законных прав."]},
        "gdpr": {"t": "Декларация об обработке персональных данных (GDPR)", "c": ["Стороны обрабатывают персональные данные только в целях: {subject}.", "Каждая сторона выступает самостоятельным оператором и обеспечивает правовое основание и технические и организационные меры.", "Данные не передаются третьим лицам сверх необходимого или требуемого законом.", "При инциденте затронутая сторона уведомляет другую без промедления.", "Субъекты данных могут осуществлять свои права GDPR перед любой из сторон."]},
        "site": {"t": "Акт открытия объекта / доступа", "c": ["Сторона А предоставляет Стороне Б доступ к объекту: {subject}.", "Объект осмотрен; состояние задокументировано фотографиями в Seam.", "Сторона Б соблюдает требования безопасности и внутренний распорядок; доступ: {notes}.", "Сроки по поручению исчисляются с даты настоящего акта."]},
    },
}
for _l, _docs in DOC_EXTRA.items():
    if _l in DOC_I18N:
        DOC_I18N[_l].update(_docs)

# Invoicing documents (table layout) - labels per language.
INV_I18N = {
    "en": {"sig_line": "Name, signature", "inv": "Invoice", "pro": "Proforma invoice", "num": "No.", "date": "Date", "seller": "Supplier", "buyer": "Customer", "desc": "Description", "amount": "Amount", "total": "Total due", "vat": "VAT is applied per the legislation applicable to the supplier.", "issued": "Issued by", "recv": "Received by", "pro_note": "A proforma is not a tax document."},
    "es": {"sig_line": "Nombre, firma", "inv": "Factura", "pro": "Factura proforma", "num": "N.º", "date": "Fecha", "seller": "Proveedor", "buyer": "Destinatario", "desc": "Descripción", "amount": "Importe", "total": "Total a pagar", "vat": "El IVA se aplica según la legislación aplicable al proveedor.", "issued": "Emitida por", "recv": "Recibida por", "pro_note": "Una proforma no es un documento fiscal."},
    "fr": {"sig_line": "Nom, signature", "inv": "Facture", "pro": "Facture proforma", "num": "N°", "date": "Date", "seller": "Fournisseur", "buyer": "Destinataire", "desc": "Description", "amount": "Montant", "total": "Total à payer", "vat": "La TVA s'applique selon la législation applicable au fournisseur.", "issued": "Émise par", "recv": "Reçue par", "pro_note": "Une proforma n'est pas un document fiscal."},
    "pl": {"sig_line": "Imię i nazwisko, podpis", "inv": "Faktura", "pro": "Faktura proforma", "num": "Nr", "date": "Data", "seller": "Dostawca", "buyer": "Odbiorca", "desc": "Opis", "amount": "Kwota", "total": "Do zapłaty", "vat": "VAT stosuje się zgodnie z przepisami właściwymi dla dostawcy.", "issued": "Wystawił", "recv": "Odebrał", "pro_note": "Proforma nie jest dokumentem podatkowym."},
    "uk": {"sig_line": "Ім'я, підпис", "inv": "Рахунок-фактура", "pro": "Проформа-рахунок", "num": "№", "date": "Дата", "seller": "Постачальник", "buyer": "Отримувач", "desc": "Опис", "amount": "Сума", "total": "До сплати", "vat": "ПДВ застосовується згідно із законодавством, чинним для постачальника.", "issued": "Склав", "recv": "Отримав", "pro_note": "Проформа не є податковим документом."},
    "pt": {"sig_line": "Nome, assinatura", "inv": "Fatura", "pro": "Fatura proforma", "num": "N.º", "date": "Data", "seller": "Fornecedor", "buyer": "Destinatário", "desc": "Descrição", "amount": "Montante", "total": "Total a pagar", "vat": "O IVA é aplicado nos termos da legislação aplicável ao fornecedor.", "issued": "Emitida por", "recv": "Recebida por", "pro_note": "Uma proforma não é um documento fiscal."},
    "bg": {"sig_line": "Име, подпис", "inv": "Фактура", "pro": "Проформа фактура", "num": "№", "date": "Дата", "seller": "Доставчик", "buyer": "Получател", "desc": "Описание", "amount": "Сума", "total": "Дължима сума", "vat": "ДДС се прилага съгласно законодателството, приложимо за доставчика.", "issued": "Съставил", "recv": "Получил", "pro_note": "Проформата не е данъчен документ."},
    "de": {"sig_line": "Name, Unterschrift", "inv": "Rechnung", "pro": "Proforma-Rechnung", "num": "Nr.", "date": "Datum", "seller": "Lieferant", "buyer": "Empfänger", "desc": "Beschreibung", "amount": "Betrag", "total": "Fälliger Betrag", "vat": "USt. gemäß dem für den Lieferanten geltenden Recht.", "issued": "Erstellt von", "recv": "Erhalten von", "pro_note": "Eine Proforma ist kein Steuerdokument."},
    "ro": {"sig_line": "Nume, semnătură", "inv": "Factură", "pro": "Factură proformă", "num": "Nr.", "date": "Data", "seller": "Furnizor", "buyer": "Beneficiar", "desc": "Descriere", "amount": "Suma", "total": "Total de plată", "vat": "TVA se aplică potrivit legislației aplicabile furnizorului.", "issued": "Întocmit de", "recv": "Primit de", "pro_note": "Proforma nu este document fiscal."},
    "el": {"sig_line": "Ονοματεπώνυμο, υπογραφή", "inv": "Τιμολόγιο", "pro": "Προτιμολόγιο", "num": "Αρ.", "date": "Ημερομηνία", "seller": "Προμηθευτής", "buyer": "Λήπτης", "desc": "Περιγραφή", "amount": "Ποσό", "total": "Συνολικό οφειλόμενο", "vat": "Ο ΦΠΑ εφαρμόζεται κατά τη νομοθεσία που ισχύει για τον προμηθευτή.", "issued": "Συντάχθηκε από", "recv": "Παρελήφθη από", "pro_note": "Το προτιμολόγιο δεν είναι φορολογικό παραστατικό."},
    "tr": {"sig_line": "Ad, imza", "inv": "Fatura", "pro": "Proforma fatura", "num": "No", "date": "Tarih", "seller": "Tedarikçi", "buyer": "Alıcı", "desc": "Açıklama", "amount": "Tutar", "total": "Ödenecek toplam", "vat": "KDV, tedarikçi için geçerli mevzuata göre uygulanır.", "issued": "Düzenleyen", "recv": "Teslim alan", "pro_note": "Proforma vergi belgesi değildir."},
    "it": {"sig_line": "Nome, firma", "inv": "Fattura", "pro": "Fattura proforma", "num": "N.", "date": "Data", "seller": "Fornitore", "buyer": "Destinatario", "desc": "Descrizione", "amount": "Importo", "total": "Totale dovuto", "vat": "L'IVA si applica secondo la normativa applicabile al fornitore.", "issued": "Emessa da", "recv": "Ricevuta da", "pro_note": "La proforma non è un documento fiscale."},
    "ru": {"sig_line": "Имя, подпись", "inv": "Счёт-фактура", "pro": "Проформа-счёт", "num": "№", "date": "Дата", "seller": "Поставщик", "buyer": "Получатель", "desc": "Описание", "amount": "Сумма", "total": "Итого к оплате", "vat": "НДС применяется согласно законодательству, применимому к поставщику.", "issued": "Составил", "recv": "Получил", "pro_note": "Проформа не является налоговым документом."},
}

# Auto-import sector documents (clause layout).
DOC_AUTO = {
    "en": {"car_sale": {"t": "Vehicle sale-purchase agreement", "c": ["The Seller {a} sells to the Buyer {b} the following vehicle: {subject}.", "The agreed price is {amount}, payable as arranged between the parties.", "The Seller declares the vehicle is their property, free of liens, pledges and third-party claims, and the documents are authentic.", "Ownership and risk pass upon handover and full payment; handover is certified with a protocol including mileage and condition.", "Special terms: {notes}."]},
              "car_handover": {"t": "Vehicle handover protocol", "c": ["The Seller hands over and the Buyer accepts the vehicle: {subject}.", "Mileage, condition and equipment at handover: {notes}.", "The Buyer has inspected the vehicle and accepts its condition; keys and documents are handed over with it.", "From the moment of handover the risk passes to the Buyer."]}},
    "es": {"car_sale": {"t": "Contrato de compraventa de vehículo", "c": ["El Vendedor {a} vende al Comprador {b} el siguiente vehículo: {subject}.", "El precio acordado es {amount}, pagadero según lo acordado entre las partes.", "El Vendedor declara que el vehículo es de su propiedad, libre de cargas, prendas y reclamaciones de terceros, y que los documentos son auténticos.", "La propiedad y el riesgo se transmiten con la entrega y el pago íntegro; la entrega se certifica con un acta que incluye kilometraje y estado.", "Condiciones especiales: {notes}."]},
              "car_handover": {"t": "Acta de entrega de vehículo", "c": ["El Vendedor entrega y el Comprador recibe el vehículo: {subject}.", "Kilometraje, estado y equipamiento en la entrega: {notes}.", "El Comprador ha inspeccionado el vehículo y acepta su estado; se entregan las llaves y los documentos.", "Desde el momento de la entrega, el riesgo pasa al Comprador."]}},
    "fr": {"car_sale": {"t": "Contrat de vente de véhicule", "c": ["Le Vendeur {a} vend à l'Acheteur {b} le véhicule suivant : {subject}.", "Le prix convenu est {amount}, payable selon l'accord entre les parties.", "Le Vendeur déclare que le véhicule est sa propriété, libre de charges, gages et réclamations de tiers, et que les documents sont authentiques.", "La propriété et le risque sont transférés à la remise et au paiement intégral ; la remise est attestée par un procès-verbal indiquant le kilométrage et l'état.", "Conditions particulières : {notes}."]},
              "car_handover": {"t": "Procès-verbal de remise de véhicule", "c": ["Le Vendeur remet et l'Acheteur reçoit le véhicule : {subject}.", "Kilométrage, état et équipement à la remise : {notes}.", "L'Acheteur a inspecté le véhicule et en accepte l'état ; les clés et les documents sont remis avec.", "À partir de la remise, le risque passe à l'Acheteur."]}},
    "pl": {"car_sale": {"t": "Umowa sprzedaży pojazdu", "c": ["Sprzedający {a} sprzedaje Kupującemu {b} następujący pojazd: {subject}.", "Uzgodniona cena to {amount}, płatna w sposób ustalony między stronami.", "Sprzedający oświadcza, że pojazd jest jego własnością, wolny od obciążeń, zastawów i roszczeń osób trzecich, a dokumenty są autentyczne.", "Własność i ryzyko przechodzą z chwilą wydania i pełnej zapłaty; wydanie potwierdza protokół z przebiegiem i stanem.", "Warunki szczególne: {notes}."]},
              "car_handover": {"t": "Protokół przekazania pojazdu", "c": ["Sprzedający wydaje, a Kupujący odbiera pojazd: {subject}.", "Przebieg, stan i wyposażenie przy wydaniu: {notes}.", "Kupujący dokonał oględzin pojazdu i akceptuje jego stan; wraz z pojazdem wydawane są kluczyki i dokumenty.", "Od chwili wydania ryzyko przechodzi na Kupującego."]}},
    "uk": {"car_sale": {"t": "Договір купівлі-продажу автомобіля", "c": ["Продавець {a} продає Покупцеві {b} такий автомобіль: {subject}.", "Узгоджена ціна становить {amount} і сплачується у погоджений сторонами спосіб.", "Продавець заявляє, що автомобіль є його власністю, вільний від обтяжень, застав і претензій третіх осіб, а документи є справжніми.", "Право власності та ризик переходять при передачі та повній оплаті; передача засвідчується актом із пробігом і станом.", "Особливі умови: {notes}."]},
              "car_handover": {"t": "Акт передачі автомобіля", "c": ["Продавець передає, а Покупець приймає автомобіль: {subject}.", "Пробіг, стан і комплектація при передачі: {notes}.", "Покупець оглянув автомобіль і приймає його стан; разом передаються ключі та документи.", "З моменту передачі ризик переходить до Покупця."]}},
    "pt": {"car_sale": {"t": "Contrato de compra e venda de veículo", "c": ["O Vendedor {a} vende ao Comprador {b} o seguinte veículo: {subject}.", "O preço acordado é {amount}, pagável conforme acordado entre as partes.", "O Vendedor declara que o veículo é sua propriedade, livre de ónus, penhores e reclamações de terceiros, e que os documentos são autênticos.", "A propriedade e o risco transferem-se com a entrega e o pagamento integral; a entrega é certificada por auto com quilometragem e estado.", "Condições especiais: {notes}."]},
              "car_handover": {"t": "Auto de entrega de veículo", "c": ["O Vendedor entrega e o Comprador recebe o veículo: {subject}.", "Quilometragem, estado e equipamento na entrega: {notes}.", "O Comprador inspecionou o veículo e aceita o seu estado; as chaves e os documentos são entregues com o mesmo.", "A partir do momento da entrega, o risco transfere-se para o Comprador."]}},
    "bg": {"car_sale": {"t": "Договор за покупко-продажба на МПС", "c": ["Продавачът {a} продава на Купувача {b} следното МПС: {subject}.", "Договорената цена е {amount}, платима по уговорения между страните начин.", "Продавачът декларира, че МПС е негова собственост, без тежести, залози и претенции на трети лица, и че документите са автентични.", "Собствеността и рискът преминават при предаване и пълно плащане; предаването се удостоверява с протокол с километраж и състояние.", "Особени условия: {notes}."]},
              "car_handover": {"t": "Протокол за предаване на МПС", "c": ["Продавачът предава, а Купувачът приема МПС: {subject}.", "Километраж, състояние и окомплектовка при предаването: {notes}.", "Купувачът е извършил оглед и приема състоянието; заедно с МПС се предават ключове и документи.", "От момента на предаването рискът преминава върху Купувача."]}},
    "de": {"car_sale": {"t": "Kfz-Kaufvertrag", "c": ["Der Verkäufer {a} verkauft dem Käufer {b} folgendes Fahrzeug: {subject}.", "Der vereinbarte Preis beträgt {amount}, zahlbar wie zwischen den Parteien vereinbart.", "Der Verkäufer erklärt, dass das Fahrzeug sein Eigentum, frei von Lasten, Pfandrechten und Ansprüchen Dritter ist und die Dokumente echt sind.", "Eigentum und Risiko gehen mit Übergabe und vollständiger Zahlung über; die Übergabe wird per Protokoll mit Kilometerstand und Zustand bestätigt.", "Besondere Bedingungen: {notes}."]},
              "car_handover": {"t": "Fahrzeug-Übergabeprotokoll", "c": ["Der Verkäufer übergibt und der Käufer übernimmt das Fahrzeug: {subject}.", "Kilometerstand, Zustand und Ausstattung bei Übergabe: {notes}.", "Der Käufer hat das Fahrzeug besichtigt und akzeptiert den Zustand; Schlüssel und Dokumente werden mit übergeben.", "Ab der Übergabe geht das Risiko auf den Käufer über."]}},
    "ro": {"car_sale": {"t": "Contract de vânzare-cumpărare auto", "c": ["Vânzătorul {a} vinde Cumpărătorului {b} următorul autovehicul: {subject}.", "Prețul convenit este {amount}, plătibil conform înțelegerii părților.", "Vânzătorul declară că autovehiculul este proprietatea sa, liber de sarcini, gajuri și pretenții ale terților, iar documentele sunt autentice.", "Proprietatea și riscul trec la predare și plata integrală; predarea se atestă prin proces-verbal cu kilometraj și stare.", "Condiții speciale: {notes}."]},
              "car_handover": {"t": "Proces-verbal de predare auto", "c": ["Vânzătorul predă, iar Cumpărătorul primește autovehiculul: {subject}.", "Kilometraj, stare și dotări la predare: {notes}.", "Cumpărătorul a inspectat autovehiculul și acceptă starea; se predau cheile și documentele.", "Din momentul predării riscul trece asupra Cumpărătorului."]}},
    "el": {"car_sale": {"t": "Σύμβαση αγοραπωλησίας οχήματος", "c": ["Ο Πωλητής {a} πωλεί στον Αγοραστή {b} το εξής όχημα: {subject}.", "Το συμφωνημένο τίμημα είναι {amount}, πληρωτέο όπως συμφωνήσουν τα μέρη.", "Ο Πωλητής δηλώνει ότι το όχημα είναι ιδιοκτησία του, ελεύθερο βαρών, ενεχύρων και αξιώσεων τρίτων, και τα έγγραφα γνήσια.", "Κυριότητα και κίνδυνος μεταβαίνουν με την παράδοση και την πλήρη πληρωμή· η παράδοση βεβαιώνεται με πρωτόκολλο με χιλιόμετρα και κατάσταση.", "Ειδικοί όροι: {notes}."]},
              "car_handover": {"t": "Πρωτόκολλο παράδοσης οχήματος", "c": ["Ο Πωλητής παραδίδει και ο Αγοραστής παραλαμβάνει το όχημα: {subject}.", "Χιλιόμετρα, κατάσταση και εξοπλισμός κατά την παράδοση: {notes}.", "Ο Αγοραστής επιθεώρησε το όχημα και αποδέχεται την κατάσταση· παραδίδονται κλειδιά και έγγραφα.", "Από την παράδοση ο κίνδυνος μεταβαίνει στον Αγοραστή."]}},
    "tr": {"car_sale": {"t": "Araç satış sözleşmesi", "c": ["Satıcı {a}, Alıcı {b} tarafına şu aracı satar: {subject}.", "Kararlaştırılan bedel {amount} olup taraflarca kararlaştırılan şekilde ödenir.", "Satıcı, aracın kendi mülkü olduğunu, üzerinde takyidat, rehin ve üçüncü kişi taleplerinin bulunmadığını ve belgelerin gerçek olduğunu beyan eder.", "Mülkiyet ve risk, teslim ve tam ödeme ile geçer; teslim, kilometre ve durumu içeren tutanakla belgelenir.", "Özel koşullar: {notes}."]},
              "car_handover": {"t": "Araç teslim tutanağı", "c": ["Satıcı teslim eder, Alıcı şu aracı teslim alır: {subject}.", "Teslimde kilometre, durum ve donanım: {notes}.", "Alıcı aracı incelemiş olup durumunu kabul eder; anahtarlar ve belgeler birlikte teslim edilir.", "Teslim anından itibaren risk Alıcıya geçer."]}},
    "it": {"car_sale": {"t": "Contratto di compravendita veicolo", "c": ["Il Venditore {a} vende all'Acquirente {b} il seguente veicolo: {subject}.", "Il prezzo concordato è {amount}, pagabile come concordato tra le parti.", "Il Venditore dichiara che il veicolo è di sua proprietà, libero da gravami, pegni e pretese di terzi, e che i documenti sono autentici.", "Proprietà e rischio passano con la consegna e il pagamento integrale; la consegna è attestata da verbale con chilometraggio e stato.", "Condizioni particolari: {notes}."]},
              "car_handover": {"t": "Verbale di consegna veicolo", "c": ["Il Venditore consegna e l'Acquirente riceve il veicolo: {subject}.", "Chilometraggio, stato e dotazioni alla consegna: {notes}.", "L'Acquirente ha ispezionato il veicolo e ne accetta lo stato; si consegnano chiavi e documenti.", "Dal momento della consegna il rischio passa all'Acquirente."]}},
    "ru": {"car_sale": {"t": "Договор купли-продажи автомобиля", "c": ["Продавец {a} продаёт Покупателю {b} следующий автомобиль: {subject}.", "Согласованная цена составляет {amount} и оплачивается в согласованном сторонами порядке.", "Продавец заявляет, что автомобиль является его собственностью, свободен от обременений, залогов и требований третьих лиц, а документы подлинны.", "Право собственности и риск переходят при передаче и полной оплате; передача удостоверяется актом с пробегом и состоянием.", "Особые условия: {notes}."]},
              "car_handover": {"t": "Акт передачи автомобиля", "c": ["Продавец передаёт, а Покупатель принимает автомобиль: {subject}.", "Пробег, состояние и комплектация при передаче: {notes}.", "Покупатель осмотрел автомобиль и принимает его состояние; вместе передаются ключи и документы.", "С момента передачи риск переходит к Покупателю."]}},
}
for _l, _docs in DOC_AUTO.items():
    if _l in DOC_I18N:
        DOC_I18N[_l].update(_docs)

# Extra table-document labels: commercial offer + delivery note.
INV_EXTRA = {
    "en": {"offer": "Commercial offer", "offer_note": "This offer is valid for 30 days from the date above, unless stated otherwise.", "delivery": "Delivery note", "delivery_note_txt": "Goods received in the quantities listed above.", "qty": "Quantity", "total_items": "Total items", "total_offer": "Total"},
    "es": {"offer": "Oferta comercial", "offer_note": "Esta oferta es válida 30 días desde la fecha indicada, salvo indicación en contrario.", "delivery": "Albarán de entrega", "delivery_note_txt": "Mercancía recibida en las cantidades indicadas arriba.", "qty": "Cantidad", "total_items": "Total de artículos", "total_offer": "Valor total"},
    "fr": {"offer": "Offre commerciale", "offer_note": "Cette offre est valable 30 jours à compter de la date indiquée, sauf mention contraire.", "delivery": "Bon de livraison", "delivery_note_txt": "Marchandises reçues dans les quantités indiquées ci-dessus.", "qty": "Quantité", "total_items": "Total des articles", "total_offer": "Valeur totale"},
    "pl": {"offer": "Oferta handlowa", "offer_note": "Oferta ważna 30 dni od podanej daty, o ile nie wskazano inaczej.", "delivery": "Dowód dostawy (WZ)", "delivery_note_txt": "Towar odebrany w ilościach podanych powyżej.", "qty": "Ilość", "total_items": "Razem pozycji", "total_offer": "Wartość całkowita"},
    "uk": {"offer": "Комерційна пропозиція", "offer_note": "Пропозиція дійсна 30 днів від зазначеної дати, якщо не вказано інше.", "delivery": "Видаткова накладна", "delivery_note_txt": "Товар отримано у зазначених вище кількостях.", "qty": "Кількість", "total_items": "Усього позицій", "total_offer": "Загальна вартість"},
    "pt": {"offer": "Proposta comercial", "offer_note": "Esta proposta é válida por 30 dias a contar da data indicada, salvo indicação em contrário.", "delivery": "Guia de remessa", "delivery_note_txt": "Mercadoria recebida nas quantidades indicadas acima.", "qty": "Quantidade", "total_items": "Total de artigos", "total_offer": "Valor total"},
    "bg": {"offer": "Търговска оферта", "offer_note": "Офертата е валидна 30 дни от посочената дата, освен ако не е указано друго.", "delivery": "Стокова разписка", "delivery_note_txt": "Стоките са получени в посочените по-горе количества.", "qty": "Количество", "total_items": "Общо бройки", "total_offer": "Обща стойност"},
    "de": {"offer": "Angebot", "offer_note": "Dieses Angebot ist ab dem oben genannten Datum 30 Tage gültig, sofern nicht anders angegeben.", "delivery": "Lieferschein", "delivery_note_txt": "Ware in den oben aufgeführten Mengen erhalten.", "qty": "Menge", "total_items": "Positionen gesamt", "total_offer": "Gesamtwert"},
    "ro": {"offer": "Ofertă comercială", "offer_note": "Oferta este valabilă 30 de zile de la data de mai sus, dacă nu se specifică altfel.", "delivery": "Aviz de însoțire a mărfii", "delivery_note_txt": "Mărfurile au fost primite în cantitățile de mai sus.", "qty": "Cantitate", "total_items": "Total articole", "total_offer": "Valoare totală"},
    "el": {"offer": "Εμπορική προσφορά", "offer_note": "Η προσφορά ισχύει 30 ημέρες από την παραπάνω ημερομηνία, εκτός αν ορίζεται διαφορετικά.", "delivery": "Δελτίο αποστολής", "delivery_note_txt": "Τα εμπορεύματα παρελήφθησαν στις παραπάνω ποσότητες.", "qty": "Ποσότητα", "total_items": "Σύνολο ειδών", "total_offer": "Συνολική αξία"},
    "tr": {"offer": "Ticari teklif", "offer_note": "Bu teklif, aksi belirtilmedikçe yukarıdaki tarihten itibaren 30 gün geçerlidir.", "delivery": "İrsaliye", "delivery_note_txt": "Mallar yukarıdaki miktarlarda teslim alınmıştır.", "qty": "Miktar", "total_items": "Toplam kalem", "total_offer": "Toplam tutar"},
    "it": {"offer": "Offerta commerciale", "offer_note": "L'offerta è valida 30 giorni dalla data sopra indicata, salvo diversa indicazione.", "delivery": "Documento di trasporto", "delivery_note_txt": "Merce ricevuta nelle quantità sopra indicate.", "qty": "Quantità", "total_items": "Totale articoli", "total_offer": "Valore totale"},
    "ru": {"offer": "Коммерческое предложение", "offer_note": "Предложение действительно 30 дней с указанной даты, если не указано иное.", "delivery": "Товарная накладная", "delivery_note_txt": "Товары получены в указанных выше количествах.", "qty": "Количество", "total_items": "Всего позиций", "total_offer": "Общая стоимость"},
}
for _l, _v in INV_EXTRA.items():
    if _l in INV_I18N:
        INV_I18N[_l].update(_v)

# VAT breakdown + intra-community reverse-charge labels.
INV_VAT = {
    "en": {"vat_no": "VAT No.", "net": "Net (taxable)", "vat_amount": "VAT", "gross": "Total incl. VAT", "reverse_charge": "Reverse charge. VAT to be accounted for by the recipient (intra-Community supply)."},
    "es": {"vat_no": "N.º IVA", "net": "Base imponible", "vat_amount": "IVA", "gross": "Total con IVA", "reverse_charge": "Inversión del sujeto pasivo. El IVA lo declara el destinatario (entrega intracomunitaria)."},
    "fr": {"vat_no": "N° TVA", "net": "Base imposable", "vat_amount": "TVA", "gross": "Total TTC", "reverse_charge": "Autoliquidation. La TVA est due par le preneur (livraison intracommunautaire)."},
    "pl": {"vat_no": "Nr VAT", "net": "Podstawa opodatkowania", "vat_amount": "VAT", "gross": "Razem z VAT", "reverse_charge": "Odwrotne obciążenie. VAT rozlicza nabywca (dostawa wewnątrzwspólnotowa)."},
    "uk": {"vat_no": "Номер ПДВ", "net": "База оподаткування", "vat_amount": "ПДВ", "gross": "Разом із ПДВ", "reverse_charge": "Зворотне нарахування. ПДВ нараховує отримувач (внутрішньосоюзне постачання)."},
    "pt": {"vat_no": "N.º IVA", "net": "Base tributável", "vat_amount": "IVA", "gross": "Total com IVA", "reverse_charge": "Autoliquidação. O IVA é devido pelo adquirente (transmissão intracomunitária)."},
    "bg": {"vat_no": "ДДС №", "net": "Данъчна основа", "vat_amount": "ДДС", "gross": "Общо с ДДС", "reverse_charge": "Обратно начисляване на ДДС. Данъкът се начислява от получателя (вътреобщностна доставка)."},
    "de": {"vat_no": "USt-IdNr", "net": "Nettobetrag", "vat_amount": "USt.", "gross": "Gesamt inkl. USt.", "reverse_charge": "Reverse-Charge. Die Steuer schuldet der Leistungsempfänger (innergemeinschaftliche Lieferung)."},
    "ro": {"vat_no": "Cod TVA", "net": "Bază impozabilă", "vat_amount": "TVA", "gross": "Total cu TVA", "reverse_charge": "Taxare inversă. TVA se datorează de către beneficiar (livrare intracomunitară)."},
    "el": {"vat_no": "ΑΦΜ/ΦΠΑ", "net": "Καθαρή αξία", "vat_amount": "ΦΠΑ", "gross": "Σύνολο με ΦΠΑ", "reverse_charge": "Αντίστροφη χρέωση. Ο ΦΠΑ αποδίδεται από τον λήπτη (ενδοκοινοτική παράδοση)."},
    "tr": {"vat_no": "Vergi No", "net": "Matrah", "vat_amount": "KDV", "gross": "Toplam (KDV dahil)", "reverse_charge": "Ters ibare. KDV alıcı tarafından hesaplanır."},
    "it": {"vat_no": "Partita IVA", "net": "Imponibile", "vat_amount": "IVA", "gross": "Totale IVA incl.", "reverse_charge": "Inversione contabile. L'IVA è assolta dal destinatario (cessione intracomunitaria)."},
    "ru": {"vat_no": "ИНН/НДС", "net": "Налоговая база", "vat_amount": "НДС", "gross": "Итого с НДС", "reverse_charge": "Обратное начисление. НДС исчисляется получателем."},
}
for _l, _v in INV_VAT.items():
    if _l in INV_I18N:
        INV_I18N[_l].update(_v)


# The elements a document has to carry to be an invoice rather than a note:
# quantity and unit price per line, when the supply happened, when it is due,
# where to pay, and who drew it up. Bulgarian accounting law (ЗСч чл. 6) and
# the EU VAT directive (art. 226) ask for broadly the same list.
INV_LEGAL = {
    "en": {"qty_col": "Qty", "unit": "Unit", "unit_price": "Unit price", "line_total": "Amount", "due": "Due by", "tax_date": "Date of supply", "pay_title": "Payment", "iban": "IBAN", "bic": "BIC", "bank": "Bank", "pay_ref": "Reference", "pay_terms": "Payment terms", "days": "days", "addr": "Address", "compiled": "Drawn up by", "of_page": "Page 1 of 1"},
    "bg": {"qty_col": "Кол.", "unit": "Мярка", "unit_price": "Ед. цена", "line_total": "Стойност", "due": "Платимо до", "tax_date": "Дата на данъчното събитие", "pay_title": "Плащане", "iban": "IBAN", "bic": "BIC", "bank": "Банка", "pay_ref": "Основание", "pay_terms": "Срок за плащане", "days": "дни", "addr": "Адрес", "compiled": "Съставил", "of_page": "Страница 1 от 1"},
    "de": {"qty_col": "Menge", "unit": "Einheit", "unit_price": "Einzelpreis", "line_total": "Betrag", "due": "Zahlbar bis", "tax_date": "Leistungsdatum", "pay_title": "Zahlung", "iban": "IBAN", "bic": "BIC", "bank": "Bank", "pay_ref": "Verwendungszweck", "pay_terms": "Zahlungsziel", "days": "Tage", "addr": "Anschrift", "compiled": "Erstellt von", "of_page": "Seite 1 von 1"},
    "ro": {"qty_col": "Cant.", "unit": "U.M.", "unit_price": "Preț unitar", "line_total": "Valoare", "due": "Scadent la", "tax_date": "Data livrării", "pay_title": "Plată", "iban": "IBAN", "bic": "BIC", "bank": "Bancă", "pay_ref": "Explicație", "pay_terms": "Termen de plată", "days": "zile", "addr": "Adresă", "compiled": "Întocmit de", "of_page": "Pagina 1 din 1"},
    "el": {"qty_col": "Ποσ.", "unit": "Μονάδα", "unit_price": "Τιμή μονάδας", "line_total": "Αξία", "due": "Πληρωτέο έως", "tax_date": "Ημερομηνία παράδοσης", "pay_title": "Πληρωμή", "iban": "IBAN", "bic": "BIC", "bank": "Τράπεζα", "pay_ref": "Αιτιολογία", "pay_terms": "Προθεσμία πληρωμής", "days": "ημέρες", "addr": "Διεύθυνση", "compiled": "Συντάχθηκε από", "of_page": "Σελίδα 1 από 1"},
    "tr": {"qty_col": "Miktar", "unit": "Birim", "unit_price": "Birim fiyat", "line_total": "Tutar", "due": "Son ödeme", "tax_date": "Teslim tarihi", "pay_title": "Ödeme", "iban": "IBAN", "bic": "BIC", "bank": "Banka", "pay_ref": "Açıklama", "pay_terms": "Ödeme vadesi", "days": "gün", "addr": "Adres", "compiled": "Düzenleyen", "of_page": "Sayfa 1 / 1"},
    "it": {"qty_col": "Q.tà", "unit": "U.M.", "unit_price": "Prezzo unitario", "line_total": "Importo", "due": "Pagabile entro", "tax_date": "Data della prestazione", "pay_title": "Pagamento", "iban": "IBAN", "bic": "BIC", "bank": "Banca", "pay_ref": "Causale", "pay_terms": "Termine di pagamento", "days": "giorni", "addr": "Indirizzo", "compiled": "Redatto da", "of_page": "Pagina 1 di 1"},
    "ru": {"qty_col": "Кол-во", "unit": "Ед.", "unit_price": "Цена за ед.", "line_total": "Сумма", "due": "Оплатить до", "tax_date": "Дата поставки", "pay_title": "Оплата", "iban": "IBAN", "bic": "BIC", "bank": "Банк", "pay_ref": "Назначение", "pay_terms": "Срок оплаты", "days": "дней", "addr": "Адрес", "compiled": "Составил", "of_page": "Страница 1 из 1"},
    "es": {"qty_col": "Cant.", "unit": "Unidad", "unit_price": "Precio unitario", "line_total": "Importe", "due": "Pagadero hasta", "tax_date": "Fecha de la operación", "pay_title": "Pago", "iban": "IBAN", "bic": "BIC", "bank": "Banco", "pay_ref": "Concepto", "pay_terms": "Plazo de pago", "days": "días", "addr": "Dirección", "compiled": "Emitida por", "of_page": "Página 1 de 1"},
    "fr": {"qty_col": "Qté", "unit": "Unité", "unit_price": "Prix unitaire", "line_total": "Montant", "due": "Payable avant le", "tax_date": "Date de la prestation", "pay_title": "Paiement", "iban": "IBAN", "bic": "BIC", "bank": "Banque", "pay_ref": "Référence", "pay_terms": "Délai de paiement", "days": "jours", "addr": "Adresse", "compiled": "Établie par", "of_page": "Page 1 sur 1"},
    "pl": {"qty_col": "Ilość", "unit": "J.m.", "unit_price": "Cena jedn.", "line_total": "Wartość", "due": "Termin płatności", "tax_date": "Data dostawy", "pay_title": "Płatność", "iban": "IBAN", "bic": "BIC", "bank": "Bank", "pay_ref": "Tytułem", "pay_terms": "Termin zapłaty", "days": "dni", "addr": "Adres", "compiled": "Wystawił", "of_page": "Strona 1 z 1"},
    "uk": {"qty_col": "К-сть", "unit": "Од.", "unit_price": "Ціна за од.", "line_total": "Сума", "due": "Сплатити до", "tax_date": "Дата постачання", "pay_title": "Оплата", "iban": "IBAN", "bic": "BIC", "bank": "Банк", "pay_ref": "Призначення", "pay_terms": "Строк оплати", "days": "днів", "addr": "Адреса", "compiled": "Склав", "of_page": "Сторінка 1 з 1"},
    "pt": {"qty_col": "Qtd.", "unit": "Unidade", "unit_price": "Preço unitário", "line_total": "Valor", "due": "Pagável até", "tax_date": "Data da operação", "pay_title": "Pagamento", "iban": "IBAN", "bic": "BIC", "bank": "Banco", "pay_ref": "Descritivo", "pay_terms": "Prazo de pagamento", "days": "dias", "addr": "Morada", "compiled": "Emitida por", "of_page": "Página 1 de 1"},
}
for _l, _v in INV_LEGAL.items():
    if _l in INV_I18N:
        INV_I18N[_l].update(_v)


@app.get("/api/compliance")
@login_required
def compliance_ref():
    """Per-country tax / registration reference for the docs UI."""
    rows = []
    for code in company.supported_countries():
        m = company.country_meta(code)
        rows.append({"code": code, "id_label": m["id_label"], "vat_prefix": m["vat_prefix"] or "",
                     "vat_rate": m["vat_rate"], "authority": m["authority"], "eu": m["eu"]})
    org = user_primary_org(g.conn, g.user["id"])
    return jsonify({"countries": rows, "org_country": (org["country"] if org else None) or "",
                    "verified": company.VAT_RATES_VERIFIED})

# Niche operational documents: logistics + commercial settlement.
DOC_NICHE = {
    "en": {
        "transport_order": {"t": "Transport order", "c": ["The Principal {a} orders and the Carrier {b} accepts the transport of: {subject}.", "Agreed freight: {amount}, payable per the terms agreed in this order.", "Loading, route and delivery conditions: {notes}.", "The Carrier confirms valid licences and insurance and is liable for the cargo from loading until delivery."]},
        "cmr": {"t": "CMR consignment note (summary)", "c": ["Sender: {a}. Consignee: {b}.", "Goods, packaging and weight: {subject}.", "Vehicle, route and accompanying documents: {notes}.", "The carriage is subject to the CMR Convention. The consignee confirms receipt of the goods in the described condition."]},
        "reconciliation": {"t": "Offset / reconciliation protocol", "c": ["The parties {a} and {b} establish mutual claims arising from: {subject}.", "The parties offset the mutual claims up to the smaller amount; the resulting net balance is {amount}.", "After the offset the remaining balance is payable per the agreed terms.", "This protocol evidences the settlement of accounts between the parties."]},
        "consignment": {"t": "Consignment (commission) agreement", "c": ["The Consignor {a} entrusts the Commission agent {b} with the sale of: {subject}.", "The agent's commission is {amount}, due upon a completed sale.", "The goods remain the property of the Consignor until sold; the agent stores them with due care.", "Settlement and reporting terms: {notes}."]},
    },
    "es": {
        "transport_order": {"t": "Orden de transporte", "c": ["El Cargador {a} ordena y el Transportista {b} acepta el transporte de: {subject}.", "Flete acordado: {amount}, pagadero según las condiciones acordadas en esta orden.", "Condiciones de carga, ruta y entrega: {notes}.", "El Transportista confirma licencias y seguro válidos y es responsable de la carga desde la carga hasta la entrega."]},
        "cmr": {"t": "Carta de porte CMR (resumen)", "c": ["Remitente: {a}. Destinatario: {b}.", "Mercancías, embalaje y peso: {subject}.", "Vehículo, ruta y documentos de acompañamiento: {notes}.", "El transporte se rige por el Convenio CMR. El destinatario confirma la recepción de la mercancía en el estado descrito."]},
        "reconciliation": {"t": "Acta de compensación", "c": ["Las partes {a} y {b} establecen créditos recíprocos derivados de: {subject}.", "Las partes compensan los créditos recíprocos hasta el importe menor; el saldo neto resultante es {amount}.", "Tras la compensación, el saldo restante es exigible según las condiciones acordadas.", "La presente acta acredita la liquidación de cuentas entre las partes."]},
        "consignment": {"t": "Contrato de consignación (comisión)", "c": ["El Consignante {a} encomienda al Comisionista {b} la venta de: {subject}.", "La comisión del agente es {amount}, exigible una vez realizada la venta.", "La mercancía sigue siendo propiedad del Consignante hasta su venta; el comisionista la custodia con la debida diligencia.", "Condiciones de liquidación e informe: {notes}."]}},
    "fr": {
        "transport_order": {"t": "Ordre de transport", "c": ["Le Donneur d'ordre {a} commande et le Transporteur {b} accepte le transport de : {subject}.", "Fret convenu : {amount}, payable selon les conditions convenues dans le présent ordre.", "Conditions de chargement, itinéraire et livraison : {notes}.", "Le Transporteur confirme disposer de licences et d'une assurance valides et est responsable de la marchandise du chargement à la livraison."]},
        "cmr": {"t": "Lettre de voiture CMR (résumé)", "c": ["Expéditeur : {a}. Destinataire : {b}.", "Marchandises, emballage et poids : {subject}.", "Véhicule, itinéraire et documents d'accompagnement : {notes}.", "Le transport est régi par la Convention CMR. Le destinataire confirme la réception de la marchandise dans l'état décrit."]},
        "reconciliation": {"t": "Procès-verbal de compensation", "c": ["Les parties {a} et {b} constatent des créances réciproques résultant de : {subject}.", "Les parties compensent les créances réciproques à hauteur du montant le plus faible ; le solde net résultant est {amount}.", "Après compensation, le solde restant est dû selon les conditions convenues.", "Le présent procès-verbal atteste le règlement des comptes entre les parties."]},
        "consignment": {"t": "Contrat de consignation (commission)", "c": ["Le Consignateur {a} confie au Commissionnaire {b} la vente de : {subject}.", "La commission de l'agent est {amount}, due une fois la vente réalisée.", "Les marchandises restent la propriété du Consignateur jusqu'à leur vente ; le commissionnaire les conserve avec la diligence requise.", "Conditions de règlement et de reporting : {notes}."]}},
    "pl": {
        "transport_order": {"t": "Zlecenie transportowe", "c": ["Zleceniodawca {a} zleca, a Przewoźnik {b} przyjmuje przewóz: {subject}.", "Uzgodniony fracht: {amount}, płatny zgodnie z warunkami niniejszego zlecenia.", "Warunki załadunku, trasa i dostawa: {notes}.", "Przewoźnik potwierdza posiadanie ważnych licencji i ubezpieczenia oraz odpowiada za ładunek od załadunku do dostawy."]},
        "cmr": {"t": "List przewozowy CMR (skrót)", "c": ["Nadawca: {a}. Odbiorca: {b}.", "Towar, opakowanie i waga: {subject}.", "Pojazd, trasa i dokumenty towarzyszące: {notes}.", "Przewóz podlega Konwencji CMR. Odbiorca potwierdza odbiór towaru w opisanym stanie."]},
        "reconciliation": {"t": "Protokół kompensaty", "c": ["Strony {a} i {b} ustalają wzajemne wierzytelności wynikające z: {subject}.", "Strony kompensują wzajemne wierzytelności do wysokości niższej kwoty; saldo netto wynosi {amount}.", "Po kompensacie pozostałe saldo jest należne zgodnie z uzgodnionymi warunkami.", "Niniejszy protokół potwierdza rozliczenie rachunków między stronami."]},
        "consignment": {"t": "Umowa komisu", "c": ["Komitent {a} powierza Komisantowi {b} sprzedaż: {subject}.", "Prowizja agenta wynosi {amount}, należna po zrealizowanej sprzedaży.", "Towary pozostają własnością Komitenta do czasu ich sprzedaży; komisant przechowuje je z należytą starannością.", "Warunki rozliczeń i raportowania: {notes}."]}},
    "uk": {
        "transport_order": {"t": "Заявка на перевезення", "c": ["Замовник {a} доручає, а Перевізник {b} приймає перевезення: {subject}.", "Узгоджений фрахт: {amount}, сплачується на умовах цієї заявки.", "Умови завантаження, маршрут і доставка: {notes}.", "Перевізник підтверджує наявність чинних ліцензій і страхування та відповідає за вантаж від завантаження до доставки."]},
        "cmr": {"t": "Накладна CMR (зведена)", "c": ["Відправник: {a}. Отримувач: {b}.", "Вантаж, упаковка та вага: {subject}.", "Транспортний засіб, маршрут і супровідні документи: {notes}.", "Перевезення регулюється Конвенцією CMR. Отримувач підтверджує одержання вантажу в описаному стані."]},
        "reconciliation": {"t": "Акт зарахування зустрічних вимог", "c": ["Сторони {a} та {b} встановлюють зустрічні вимоги, що виникли з: {subject}.", "Сторони зараховують зустрічні вимоги до розміру меншої суми; отримане чисте сальдо становить {amount}.", "Після зарахування залишок підлягає сплаті на узгоджених умовах.", "Цей акт засвідчує врегулювання розрахунків між сторонами."]},
        "consignment": {"t": "Договір комісії", "c": ["Комітент {a} доручає Комісіонеру {b} продаж: {subject}.", "Комісійна винагорода становить {amount} і належить після здійсненого продажу.", "Товари залишаються власністю Комітента до їх продажу; комісіонер зберігає їх з належною дбайливістю.", "Умови розрахунків і звітності: {notes}."]}},
    "pt": {
        "transport_order": {"t": "Ordem de transporte", "c": ["O Expedidor {a} ordena e o Transportador {b} aceita o transporte de: {subject}.", "Frete acordado: {amount}, pagável nos termos acordados na presente ordem.", "Condições de carga, itinerário e entrega: {notes}.", "O Transportador confirma licenças e seguro válidos e é responsável pela carga desde o carregamento até à entrega."]},
        "cmr": {"t": "Declaração de expedição CMR (resumo)", "c": ["Remetente: {a}. Destinatário: {b}.", "Mercadorias, embalagem e peso: {subject}.", "Veículo, itinerário e documentos de acompanhamento: {notes}.", "O transporte rege-se pela Convenção CMR. O destinatário confirma a receção da mercadoria no estado descrito."]},
        "reconciliation": {"t": "Auto de compensação", "c": ["As partes {a} e {b} estabelecem créditos recíprocos decorrentes de: {subject}.", "As partes compensam os créditos recíprocos até ao montante menor; o saldo líquido resultante é {amount}.", "Após a compensação, o saldo remanescente é devido nos termos acordados.", "O presente auto comprova o acerto de contas entre as partes."]},
        "consignment": {"t": "Contrato de consignação (comissão)", "c": ["O Consignante {a} confia ao Comissário {b} a venda de: {subject}.", "A comissão do agente é {amount}, devida após a venda concretizada.", "As mercadorias permanecem propriedade do Consignante até serem vendidas; o comissário guarda-as com a devida diligência.", "Condições de liquidação e reporte: {notes}."]}},
    "bg": {
        "transport_order": {"t": "Заявка за транспорт", "c": ["Възложителят {a} възлага, а Превозвачът {b} приема превоза на: {subject}.", "Договорено навло: {amount}, платимо съгласно уговорените в заявката условия.", "Условия за товарене, маршрут и доставка: {notes}.", "Превозвачът потвърждава наличие на валидни лицензи и застраховка и носи отговорност за товара от натоварването до доставката."]},
        "cmr": {"t": "ЧМР товарителница (обобщена)", "c": ["Изпращач: {a}. Получател: {b}.", "Стоки, опаковка и тегло: {subject}.", "Превозно средство, маршрут и придружаващи документи: {notes}.", "Превозът се подчинява на Конвенцията CMR. Получателят потвърждава получаването на стоката в описаното състояние."]},
        "reconciliation": {"t": "Протокол за прихващане на насрещни вземания", "c": ["Страните {a} и {b} установяват насрещни вземания, произтичащи от: {subject}.", "Страните прихващат насрещните вземания до размера на по-малкото; полученото нетно салдо е {amount}.", "След прихващането остатъкът се дължи съгласно уговорените условия.", "Настоящият протокол удостоверява уреждането на сметките между страните."]},
        "consignment": {"t": "Комисионен договор", "c": ["Доверителят {a} възлага на Комисионера {b} продажбата на: {subject}.", "Комисионното възнаграждение е {amount}, дължимо при осъществена продажба.", "Стоките остават собственост на Доверителя до продажбата им; комисионерът ги съхранява с грижата на добър търговец.", "Условия за разплащане и отчитане: {notes}."]},
    },
    "de": {
        "transport_order": {"t": "Transportauftrag", "c": ["Der Auftraggeber {a} beauftragt und der Frachtführer {b} übernimmt den Transport von: {subject}.", "Vereinbarte Fracht: {amount}, zahlbar gemäß den in diesem Auftrag vereinbarten Bedingungen.", "Verlade-, Routen- und Lieferbedingungen: {notes}.", "Der Frachtführer bestätigt gültige Lizenzen und Versicherung und haftet für die Ladung von der Verladung bis zur Ablieferung."]},
        "cmr": {"t": "CMR-Frachtbrief (Zusammenfassung)", "c": ["Absender: {a}. Empfänger: {b}.", "Ware, Verpackung und Gewicht: {subject}.", "Fahrzeug, Route und Begleitdokumente: {notes}.", "Die Beförderung unterliegt dem CMR-Übereinkommen. Der Empfänger bestätigt den Erhalt der Ware im beschriebenen Zustand."]},
        "reconciliation": {"t": "Aufrechnungs-/Abstimmungsprotokoll", "c": ["Die Parteien {a} und {b} stellen gegenseitige Forderungen fest aus: {subject}.", "Die Parteien rechnen die gegenseitigen Forderungen bis zum niedrigeren Betrag auf; der Nettosaldo beträgt {amount}.", "Nach der Aufrechnung ist der verbleibende Saldo gemäß den vereinbarten Bedingungen fällig.", "Dieses Protokoll belegt den Ausgleich der Konten zwischen den Parteien."]},
        "consignment": {"t": "Kommissionsvertrag", "c": ["Der Kommittent {a} überträgt dem Kommissionär {b} den Verkauf von: {subject}.", "Die Kommission beträgt {amount} und ist bei erfolgtem Verkauf fällig.", "Die Ware bleibt bis zum Verkauf Eigentum des Kommittenten; der Kommissionär lagert sie mit der gebotenen Sorgfalt.", "Abrechnungs- und Berichtsbedingungen: {notes}."]},
    },
    "ro": {
        "transport_order": {"t": "Comandă de transport", "c": ["Beneficiarul {a} comandă, iar Transportatorul {b} acceptă transportul: {subject}.", "Navlu convenit: {amount}, plătibil conform condițiilor din prezenta comandă.", "Condiții de încărcare, rută și livrare: {notes}.", "Transportatorul confirmă licențe și asigurare valabile și răspunde pentru marfă de la încărcare până la livrare."]},
        "cmr": {"t": "Scrisoare de trăsură CMR (sinteză)", "c": ["Expeditor: {a}. Destinatar: {b}.", "Mărfuri, ambalaj și greutate: {subject}.", "Vehicul, rută și documente însoțitoare: {notes}.", "Transportul este supus Convenției CMR. Destinatarul confirmă primirea mărfii în starea descrisă."]},
        "reconciliation": {"t": "Proces-verbal de compensare", "c": ["Părțile {a} și {b} constată creanțe reciproce rezultate din: {subject}.", "Părțile compensează creanțele reciproce până la suma mai mică; soldul net rezultat este {amount}.", "După compensare, soldul rămas este datorat conform condițiilor agreate.", "Prezentul proces-verbal atestă regularizarea conturilor între părți."]},
        "consignment": {"t": "Contract de comision", "c": ["Comitentul {a} încredințează Comisionarului {b} vânzarea: {subject}.", "Comisionul este {amount}, datorat la realizarea vânzării.", "Mărfurile rămân proprietatea Comitentului până la vânzare; comisionarul le păstrează cu diligența cuvenită.", "Condiții de decontare și raportare: {notes}."]},
    },
    "el": {
        "transport_order": {"t": "Εντολή μεταφοράς", "c": ["Ο Εντολέας {a} αναθέτει και ο Μεταφορέας {b} αναλαμβάνει τη μεταφορά: {subject}.", "Συμφωνημένος ναύλος: {amount}, πληρωτέος βάσει των όρων της παρούσας εντολής.", "Όροι φόρτωσης, διαδρομής και παράδοσης: {notes}.", "Ο Μεταφορέας βεβαιώνει ότι διαθέτει ισχύουσες άδειες και ασφάλιση και ευθύνεται για το φορτίο από τη φόρτωση έως την παράδοση."]},
        "cmr": {"t": "Φορτωτική CMR (σύνοψη)", "c": ["Αποστολέας: {a}. Παραλήπτης: {b}.", "Εμπορεύματα, συσκευασία και βάρος: {subject}.", "Όχημα, διαδρομή και συνοδευτικά έγγραφα: {notes}.", "Η μεταφορά διέπεται από τη Σύμβαση CMR. Ο παραλήπτης βεβαιώνει την παραλαβή στην περιγραφόμενη κατάσταση."]},
        "reconciliation": {"t": "Πρωτόκολλο συμψηφισμού", "c": ["Τα μέρη {a} και {b} διαπιστώνουν αμοιβαίες απαιτήσεις από: {subject}.", "Τα μέρη συμψηφίζουν τις αμοιβαίες απαιτήσεις έως το μικρότερο ποσό· το καθαρό υπόλοιπο είναι {amount}.", "Μετά τον συμψηφισμό το υπόλοιπο οφείλεται σύμφωνα με τους συμφωνημένους όρους.", "Το παρόν πιστοποιεί την τακτοποίηση των λογαριασμών μεταξύ των μερών."]},
        "consignment": {"t": "Σύμβαση παρακαταθήκης (προμήθειας)", "c": ["Ο Παραγγελέας {a} αναθέτει στον Παραγγελιοδόχο {b} την πώληση: {subject}.", "Η προμήθεια ανέρχεται σε {amount}, οφειλόμενη με την ολοκλήρωση της πώλησης.", "Τα εμπορεύματα παραμένουν ιδιοκτησία του Παραγγελέα μέχρι την πώληση· ο παραγγελιοδόχος τα φυλάσσει με τη δέουσα επιμέλεια.", "Όροι εκκαθάρισης και αναφοράς: {notes}."]},
    },
    "tr": {
        "transport_order": {"t": "Nakliye siparişi", "c": ["Sipariş veren {a}, Taşıyıcı {b} tarafına şu taşımayı verir: {subject}.", "Kararlaştırılan navlun: {amount}, bu siparişteki koşullara göre ödenir.", "Yükleme, güzergâh ve teslim koşulları: {notes}.", "Taşıyıcı geçerli lisans ve sigortaya sahip olduğunu beyan eder ve yükten yüklemeden teslime kadar sorumludur."]},
        "cmr": {"t": "CMR taşıma belgesi (özet)", "c": ["Gönderici: {a}. Alıcı: {b}.", "Mal, ambalaj ve ağırlık: {subject}.", "Araç, güzergâh ve ekli belgeler: {notes}.", "Taşıma CMR Sözleşmesine tabidir. Alıcı, malı tarif edilen durumda teslim aldığını onaylar."]},
        "reconciliation": {"t": "Mahsuplaşma tutanağı", "c": ["Taraflar {a} ve {b}, şu işlemden doğan karşılıklı alacakları tespit eder: {subject}.", "Taraflar karşılıklı alacakları küçük tutara kadar mahsup eder; net bakiye {amount}.", "Mahsuptan sonra kalan bakiye kararlaştırılan koşullara göre ödenir.", "Bu tutanak taraflar arasındaki hesapların kapatıldığını belgeler."]},
        "consignment": {"t": "Konsinye (komisyon) sözleşmesi", "c": ["Konsinyeci {a}, Komisyoncu {b} tarafına şunun satışını verir: {subject}.", "Komisyon bedeli {amount} olup satış gerçekleştiğinde doğar.", "Mallar satılana kadar Konsinyecinin mülkiyetinde kalır; komisyoncu gerekli özenle saklar.", "Ödeme ve raporlama koşulları: {notes}."]},
    },
    "it": {
        "transport_order": {"t": "Ordine di trasporto", "c": ["Il Committente {a} affida e il Vettore {b} accetta il trasporto di: {subject}.", "Nolo concordato: {amount}, pagabile secondo le condizioni del presente ordine.", "Condizioni di carico, itinerario e consegna: {notes}.", "Il Vettore conferma licenze e assicurazione valide ed è responsabile della merce dal carico fino alla consegna."]},
        "cmr": {"t": "Lettera di vettura CMR (sintesi)", "c": ["Mittente: {a}. Destinatario: {b}.", "Merce, imballaggio e peso: {subject}.", "Veicolo, itinerario e documenti di accompagnamento: {notes}.", "Il trasporto è soggetto alla Convenzione CMR. Il destinatario conferma la ricezione della merce nello stato descritto."]},
        "reconciliation": {"t": "Verbale di compensazione", "c": ["Le parti {a} e {b} accertano crediti reciproci derivanti da: {subject}.", "Le parti compensano i crediti reciproci fino all'importo minore; il saldo netto risultante è {amount}.", "Dopo la compensazione il saldo residuo è dovuto secondo le condizioni concordate.", "Il presente verbale attesta la sistemazione dei conti tra le parti."]},
        "consignment": {"t": "Contratto di conto vendita (commissione)", "c": ["Il Committente {a} affida al Commissionario {b} la vendita di: {subject}.", "La provvigione è {amount}, dovuta a vendita conclusa.", "La merce resta di proprietà del Committente fino alla vendita; il commissionario la custodisce con la dovuta diligenza.", "Condizioni di regolamento e rendicontazione: {notes}."]},
    },
    "ru": {
        "transport_order": {"t": "Заявка на перевозку", "c": ["Заказчик {a} поручает, а Перевозчик {b} принимает перевозку: {subject}.", "Согласованный фрахт: {amount}, оплачивается на условиях настоящей заявки.", "Условия погрузки, маршрута и доставки: {notes}.", "Перевозчик подтверждает наличие действующих лицензий и страхования и отвечает за груз с момента погрузки до доставки."]},
        "cmr": {"t": "Накладная CMR (сводная)", "c": ["Отправитель: {a}. Получатель: {b}.", "Груз, упаковка и вес: {subject}.", "Транспортное средство, маршрут и сопроводительные документы: {notes}.", "Перевозка регулируется Конвенцией CMR. Получатель подтверждает получение груза в описанном состоянии."]},
        "reconciliation": {"t": "Акт зачёта встречных требований", "c": ["Стороны {a} и {b} устанавливают встречные требования, возникшие из: {subject}.", "Стороны производят зачёт встречных требований до меньшей суммы; итоговое сальдо составляет {amount}.", "После зачёта остаток подлежит оплате на согласованных условиях.", "Настоящий акт подтверждает урегулирование расчётов между сторонами."]},
        "consignment": {"t": "Договор комиссии", "c": ["Комитент {a} поручает Комиссионеру {b} продажу: {subject}.", "Комиссионное вознаграждение составляет {amount} и причитается при состоявшейся продаже.", "Товары остаются собственностью Комитента до их продажи; комиссионер хранит их с должной заботливостью.", "Условия расчётов и отчётности: {notes}."]},
    },
}
for _l, _docs in DOC_NICHE.items():
    if _l in DOC_I18N:
        DOC_I18N[_l].update(_docs)

# Auto-import: customs / import declaration summary.
DOC_CUSTOMS = {
    "en": {"car_customs": {"t": "Import / customs declaration (summary)", "c": ["Importer {a} declares for customs clearance the following vehicle: {subject}.", "Declared customs value: {amount}.", "Origin, EORI, HS code and accompanying documents: {notes}.", "The importer confirms the accuracy of the data and assumes liability for duties and taxes due upon import."]}},
    "es": {"car_customs": {"t": "Declaración de importación / aduana (resumen)", "c": ["El Importador {a} declara para despacho aduanero el siguiente vehículo: {subject}.", "Valor en aduana declarado: {amount}.", "Origen, EORI, código arancelario y documentos de acompañamiento: {notes}.", "El importador confirma la exactitud de los datos y asume la responsabilidad de los derechos e impuestos debidos en la importación."]}},
    "fr": {"car_customs": {"t": "Déclaration d'importation / douane (résumé)", "c": ["L'Importateur {a} déclare pour dédouanement le véhicule suivant : {subject}.", "Valeur en douane déclarée : {amount}.", "Origine, EORI, code tarifaire et documents d'accompagnement : {notes}.", "L'importateur confirme l'exactitude des données et assume la responsabilité des droits et taxes dus à l'importation."]}},
    "pl": {"car_customs": {"t": "Zgłoszenie importowe / celne (skrót)", "c": ["Importer {a} zgłasza do odprawy celnej następujący pojazd: {subject}.", "Zadeklarowana wartość celna: {amount}.", "Pochodzenie, EORI, kod taryfowy i dokumenty towarzyszące: {notes}.", "Importer potwierdza prawidłowość danych i przyjmuje odpowiedzialność za cła i podatki należne przy imporcie."]}},
    "uk": {"car_customs": {"t": "Митна декларація на імпорт (зведена)", "c": ["Імпортер {a} декларує для митного оформлення такий автомобіль: {subject}.", "Заявлена митна вартість: {amount}.", "Походження, EORI, код УКТЗЕД і супровідні документи: {notes}.", "Імпортер підтверджує достовірність даних і бере на себе відповідальність за мита та податки, що належать до сплати при імпорті."]}},
    "pt": {"car_customs": {"t": "Declaração de importação / aduaneira (resumo)", "c": ["O Importador {a} declara para desalfandegamento o seguinte veículo: {subject}.", "Valor aduaneiro declarado: {amount}.", "Origem, EORI, código pautal e documentos de acompanhamento: {notes}.", "O importador confirma a exatidão dos dados e assume a responsabilidade pelos direitos e impostos devidos na importação."]}},
    "bg": {"car_customs": {"t": "Митническа декларация за внос (обобщена)", "c": ["Вносителят {a} декларира за митническо оформяне следното МПС: {subject}.", "Декларирана митническа стойност: {amount}.", "Произход, EORI, тарифен код и придружаващи документи: {notes}.", "Вносителят потвърждава верността на данните и поема отговорност за дължимите при вноса мита и данъци."]}},
    "de": {"car_customs": {"t": "Einfuhr-/Zollanmeldung (Zusammenfassung)", "c": ["Der Importeur {a} meldet zur Zollabfertigung folgendes Fahrzeug an: {subject}.", "Angemeldeter Zollwert: {amount}.", "Ursprung, EORI, Zolltarifnummer und Begleitdokumente: {notes}.", "Der Importeur bestätigt die Richtigkeit der Angaben und haftet für die bei der Einfuhr fälligen Abgaben und Steuern."]}},
    "ro": {"car_customs": {"t": "Declarație vamală de import (sinteză)", "c": ["Importatorul {a} declară pentru vămuire următorul autovehicul: {subject}.", "Valoare vamală declarată: {amount}.", "Origine, EORI, cod tarifar și documente însoțitoare: {notes}.", "Importatorul confirmă exactitatea datelor și își asumă răspunderea pentru taxele și impozitele datorate la import."]}},
    "el": {"car_customs": {"t": "Τελωνειακή διασάφηση εισαγωγής (σύνοψη)", "c": ["Ο εισαγωγέας {a} διασαφεί προς τελωνισμό το εξής όχημα: {subject}.", "Δηλωθείσα δασμολογητέα αξία: {amount}.", "Προέλευση, EORI, δασμολογική κλάση και συνοδευτικά έγγραφα: {notes}.", "Ο εισαγωγέας βεβαιώνει την ακρίβεια των στοιχείων και αναλαμβάνει ευθύνη για δασμούς και φόρους κατά την εισαγωγή."]}},
    "tr": {"car_customs": {"t": "İthalat / gümrük beyannamesi (özet)", "c": ["İthalatçı {a}, gümrükleme için şu aracı beyan eder: {subject}.", "Beyan edilen gümrük kıymeti: {amount}.", "Menşe, EORI, GTİP ve ekli belgeler: {notes}.", "İthalatçı bilgilerin doğruluğunu teyit eder ve ithalatta doğan vergi ve resimlerden sorumludur."]}},
    "it": {"car_customs": {"t": "Dichiarazione doganale di importazione (sintesi)", "c": ["L'importatore {a} dichiara per lo sdoganamento il seguente veicolo: {subject}.", "Valore doganale dichiarato: {amount}.", "Origine, EORI, codice tariffario e documenti di accompagnamento: {notes}.", "L'importatore conferma l'esattezza dei dati e si assume la responsabilità per dazi e imposte dovuti all'importazione."]}},
    "ru": {"car_customs": {"t": "Таможенная декларация на импорт (сводная)", "c": ["Импортёр {a} декларирует для таможенного оформления следующий автомобиль: {subject}.", "Заявленная таможенная стоимость: {amount}.", "Происхождение, EORI, код ТН ВЭД и сопроводительные документы: {notes}.", "Импортёр подтверждает достоверность данных и несёт ответственность за пошлины и налоги при импорте."]}},
}
for _l, _docs in DOC_CUSTOMS.items():
    if _l in DOC_I18N:
        DOC_I18N[_l].update(_docs)

# Sector-specific documents, one signature template per vertical:
#   fulfillment      -> e-commerce <-> 3PL
#   ip_transfer      -> agency <-> client
#   act19            -> construction <-> subcontractors
#   supply_recurring -> restaurant <-> distributor
DOC_SECTOR = {
    "en": {
        "fulfillment": {"t": "Warehousing & fulfillment agreement", "c": ["The Client {a} entrusts the Operator {b} with the storage, picking, packing and dispatch of goods: {subject}.", "The agreed handling fee is {amount}; storage, packaging and returns are charged per the price list agreed per order in Seam.", "The Operator dispatches within the agreed SLA and records each shipment status in the shared workspace.", "The goods remain the property of the Client; the Operator keeps them separated, insured and inventoried.", "Discrepancies, damages and returns are documented with photo evidence in Seam. Special terms: {notes}."]},
        "ip_transfer": {"t": "Copyright / IP transfer agreement", "c": ["The Author {b} creates for the Client {a} the following works: {subject}.", "Upon full payment of {amount}, the Author transfers to the Client the exclusive economic rights to use the works, without territorial or time limitation.", "The transfer covers reproduction, distribution, public display, adaptation and use in any medium, including digital.", "The Author warrants the works are original and do not infringe third-party rights; third-party assets (fonts, stock, licences) are listed separately.", "Moral rights and portfolio use: {notes}."]},
        "act19": {"t": "Protocol for completed construction works", "c": ["The parties establish that the Contractor {b} completed for the Assignor {a} the following works: {subject}.", "The value of the completed works accepted with this protocol is {amount}.", "Quantities, unit prices and measurements are attached; deviations from the design are noted below.", "The works were inspected on site and comply with the agreed specification and applicable technical requirements.", "Notes and remaining items: {notes}. This protocol is a basis for invoicing the accepted works."]},
        "supply_recurring": {"t": "Framework agreement for recurring supplies", "c": ["The Supplier {b} undertakes to deliver to the Buyer {a} on a recurring basis: {subject}.", "Prices are fixed at {amount} per the agreed price list and are updated only in writing, with notice before the next order.", "Orders, quantities, delivery windows and substitutions are agreed per order in the shared Seam workspace.", "The Supplier guarantees origin, quality, storage conditions and cold chain where applicable, and provides the accompanying documents required by law.", "Delivery, acceptance and complaints procedure: {notes}."]},
    },
    "bg": {
        "fulfillment": {"t": "Договор за складиране и фулфилмънт", "c": ["Възложителят {a} възлага на Оператора {b} съхранението, комплектоването, опаковането и експедицията на стоки: {subject}.", "Договореното възнаграждение за обработка е {amount}; складирането, опаковките и връщанията се таксуват по ценоразпис, договорен по поръчки в Seam.", "Операторът експедира в договорения срок (SLA) и отразява статуса на всяка пратка в общото работно пространство.", "Стоките остават собственост на Възложителя; Операторът ги съхранява отделно, застраховани и заведени по количества.", "Разлики, повреди и връщания се документират със снимков материал в Seam. Особени условия: {notes}."]},
        "ip_transfer": {"t": "Договор за прехвърляне на авторски права", "c": ["Авторът {b} създава за Възложителя {a} следните произведения: {subject}.", "При пълно плащане на {amount} Авторът прехвърля на Възложителя изключителните имуществени права за използване на произведенията, без териториално и времево ограничение.", "Прехвърлянето обхваща възпроизвеждане, разпространение, публично показване, преработка и използване във всякакъв носител, включително цифров.", "Авторът гарантира, че произведенията са оригинални и не нарушават права на трети лица; чужди елементи (шрифтове, стокови изображения, лицензи) се посочват отделно.", "Неимуществени права и използване в портфолио: {notes}."]},
        "act19": {"t": "Протокол за извършени строително-монтажни работи", "c": ["Страните установяват, че Изпълнителят {b} извърши за Възложителя {a} следните работи: {subject}.", "Стойността на приетите с настоящия протокол извършени работи е {amount}.", "Количества, единични цени и замервания се прилагат; отклоненията от проекта са отбелязани по-долу.", "Работите са огледани на място и съответстват на договорената спецификация и приложимите технически изисквания.", "Забележки и оставащи позиции: {notes}. Протоколът е основание за фактуриране на приетите работи."]},
        "supply_recurring": {"t": "Рамков договор за периодични доставки", "c": ["Доставчикът {b} се задължава да доставя периодично на Купувача {a}: {subject}.", "Цените са фиксирани на {amount} съгласно договорения ценоразпис и се променят само писмено, с предизвестие преди следващата поръчка.", "Поръчките, количествата, часовите прозорци за доставка и заместващите артикули се договарят по поръчки в общото пространство в Seam.", "Доставчикът гарантира произход, качество, условия на съхранение и хладилна верига където е приложимо, и предоставя изискуемите по закон придружаващи документи.", "Ред за доставка, приемане и рекламации: {notes}."]},
    },
    "de": {
        "fulfillment": {"t": "Lager- und Fulfillment-Vertrag", "c": ["Der Auftraggeber {a} überträgt dem Betreiber {b} die Lagerung, Kommissionierung, Verpackung und den Versand von Waren: {subject}.", "Das vereinbarte Bearbeitungsentgelt beträgt {amount}; Lagerung, Verpackung und Retouren werden nach der je Auftrag in Seam vereinbarten Preisliste berechnet.", "Der Betreiber versendet innerhalb des vereinbarten SLA und dokumentiert jeden Sendungsstatus im gemeinsamen Arbeitsbereich.", "Die Ware bleibt Eigentum des Auftraggebers; der Betreiber lagert sie getrennt, versichert und bestandsgeführt.", "Abweichungen, Schäden und Retouren werden mit Fotonachweisen in Seam dokumentiert. Besondere Bedingungen: {notes}."]},
        "ip_transfer": {"t": "Vertrag über die Übertragung von Urheberrechten", "c": ["Der Urheber {b} erstellt für den Auftraggeber {a} folgende Werke: {subject}.", "Mit vollständiger Zahlung von {amount} überträgt der Urheber dem Auftraggeber die ausschließlichen Nutzungsrechte an den Werken, ohne räumliche und zeitliche Beschränkung.", "Die Übertragung umfasst Vervielfältigung, Verbreitung, öffentliche Wiedergabe, Bearbeitung und Nutzung in jedem Medium, einschließlich digital.", "Der Urheber sichert zu, dass die Werke original sind und keine Rechte Dritter verletzen; fremde Bestandteile (Schriften, Stockmaterial, Lizenzen) werden gesondert aufgeführt.", "Urheberpersönlichkeitsrechte und Portfolio-Nutzung: {notes}."]},
        "act19": {"t": "Protokoll über erbrachte Bauleistungen", "c": ["Die Parteien stellen fest, dass der Auftragnehmer {b} für den Auftraggeber {a} folgende Leistungen erbracht hat: {subject}.", "Der Wert der mit diesem Protokoll abgenommenen Leistungen beträgt {amount}.", "Mengen, Einheitspreise und Aufmaße sind beigefügt; Abweichungen von der Planung sind unten vermerkt.", "Die Leistungen wurden vor Ort geprüft und entsprechen der vereinbarten Spezifikation und den geltenden technischen Anforderungen.", "Anmerkungen und Restleistungen: {notes}. Dieses Protokoll ist Grundlage für die Abrechnung der abgenommenen Leistungen."]},
        "supply_recurring": {"t": "Rahmenvertrag über wiederkehrende Lieferungen", "c": ["Der Lieferant {b} verpflichtet sich, dem Käufer {a} wiederkehrend zu liefern: {subject}.", "Die Preise sind mit {amount} gemäß der vereinbarten Preisliste festgelegt und ändern sich nur schriftlich, mit Ankündigung vor der nächsten Bestellung.", "Bestellungen, Mengen, Lieferfenster und Ersatzartikel werden je Auftrag im gemeinsamen Seam-Arbeitsbereich vereinbart.", "Der Lieferant garantiert Herkunft, Qualität, Lagerbedingungen und - soweit einschlägig - die Kühlkette und stellt die gesetzlich erforderlichen Begleitdokumente bereit.", "Liefer-, Abnahme- und Reklamationsverfahren: {notes}."]},
    },
    "ro": {
        "fulfillment": {"t": "Contract de depozitare și fulfillment", "c": ["Beneficiarul {a} încredințează Operatorului {b} depozitarea, pregătirea, ambalarea și expedierea mărfurilor: {subject}.", "Tariful de procesare convenit este {amount}; depozitarea, ambalajele și retururile se taxează conform listei de prețuri agreate per comandă în Seam.", "Operatorul expediază în cadrul SLA convenit și înregistrează statusul fiecărei expedieri în spațiul comun de lucru.", "Mărfurile rămân proprietatea Beneficiarului; Operatorul le păstrează separat, asigurate și inventariate.", "Diferențele, deteriorările și retururile se documentează cu dovezi foto în Seam. Condiții speciale: {notes}."]},
        "ip_transfer": {"t": "Contract de cesiune a drepturilor de autor", "c": ["Autorul {b} creează pentru Beneficiarul {a} următoarele opere: {subject}.", "La plata integrală a {amount}, Autorul cedează Beneficiarului drepturile patrimoniale exclusive de utilizare a operelor, fără limitare teritorială sau temporală.", "Cesiunea acoperă reproducerea, distribuirea, comunicarea publică, adaptarea și utilizarea pe orice suport, inclusiv digital.", "Autorul garantează că operele sunt originale și nu încalcă drepturi ale terților; elementele terțe (fonturi, stock, licențe) se enumeră separat.", "Drepturi morale și utilizare în portofoliu: {notes}."]},
        "act19": {"t": "Proces-verbal de lucrări de construcții executate", "c": ["Părțile constată că Executantul {b} a realizat pentru Beneficiarul {a} următoarele lucrări: {subject}.", "Valoarea lucrărilor executate și recepționate prin prezentul proces-verbal este {amount}.", "Cantitățile, prețurile unitare și măsurătorile sunt anexate; abaterile de la proiect sunt notate mai jos.", "Lucrările au fost verificate la fața locului și corespund specificației agreate și cerințelor tehnice aplicabile.", "Observații și poziții rămase: {notes}. Prezentul proces-verbal stă la baza facturării lucrărilor recepționate."]},
        "supply_recurring": {"t": "Contract-cadru de livrări periodice", "c": ["Furnizorul {b} se obligă să livreze periodic Cumpărătorului {a}: {subject}.", "Prețurile sunt fixate la {amount} conform listei agreate și se modifică doar în scris, cu preaviz înainte de următoarea comandă.", "Comenzile, cantitățile, ferestrele de livrare și înlocuirile se agreează per comandă în spațiul comun Seam.", "Furnizorul garantează originea, calitatea, condițiile de depozitare și lanțul frigorific acolo unde este cazul și pune la dispoziție documentele însoțitoare cerute de lege.", "Procedura de livrare, recepție și reclamații: {notes}."]},
    },
    "el": {
        "fulfillment": {"t": "Σύμβαση αποθήκευσης και fulfillment", "c": ["Ο Εντολέας {a} αναθέτει στον Πάροχο {b} την αποθήκευση, συλλογή, συσκευασία και αποστολή εμπορευμάτων: {subject}.", "Η συμφωνημένη αμοιβή διαχείρισης είναι {amount}· η αποθήκευση, οι συσκευασίες και οι επιστροφές χρεώνονται βάσει του τιμοκαταλόγου που συμφωνείται ανά παραγγελία στο Seam.", "Ο Πάροχος αποστέλλει εντός του συμφωνημένου SLA και καταγράφει την κατάσταση κάθε αποστολής στον κοινό χώρο εργασίας.", "Τα εμπορεύματα παραμένουν ιδιοκτησία του Εντολέα· ο Πάροχος τα φυλάσσει χωριστά, ασφαλισμένα και απογεγραμμένα.", "Αποκλίσεις, ζημιές και επιστροφές τεκμηριώνονται με φωτογραφικό υλικό στο Seam. Ειδικοί όροι: {notes}."]},
        "ip_transfer": {"t": "Σύμβαση μεταβίβασης πνευματικών δικαιωμάτων", "c": ["Ο Δημιουργός {b} δημιουργεί για τον Εντολέα {a} τα εξής έργα: {subject}.", "Με την πλήρη καταβολή {amount}, ο Δημιουργός μεταβιβάζει στον Εντολέα τα αποκλειστικά περιουσιακά δικαιώματα χρήσης των έργων, χωρίς εδαφικό ή χρονικό περιορισμό.", "Η μεταβίβαση καλύπτει αναπαραγωγή, διανομή, παρουσίαση στο κοινό, διασκευή και χρήση σε κάθε μέσο, συμπεριλαμβανομένου του ψηφιακού.", "Ο Δημιουργός εγγυάται ότι τα έργα είναι πρωτότυπα και δεν προσβάλλουν δικαιώματα τρίτων· στοιχεία τρίτων (γραμματοσειρές, stock, άδειες) αναφέρονται χωριστά.", "Ηθικά δικαιώματα και χρήση σε portfolio: {notes}."]},
        "act19": {"t": "Πρωτόκολλο εκτελεσθεισών οικοδομικών εργασιών", "c": ["Τα μέρη διαπιστώνουν ότι ο Ανάδοχος {b} εκτέλεσε για τον Αναθέτοντα {a} τις εξής εργασίες: {subject}.", "Η αξία των εργασιών που παραλαμβάνονται με το παρόν πρωτόκολλο είναι {amount}.", "Ποσότητες, τιμές μονάδας και επιμετρήσεις επισυνάπτονται· οι αποκλίσεις από τη μελέτη σημειώνονται παρακάτω.", "Οι εργασίες ελέγχθηκαν επιτόπου και συμμορφώνονται με τη συμφωνημένη προδιαγραφή και τις ισχύουσες τεχνικές απαιτήσεις.", "Παρατηρήσεις και υπολειπόμενες εργασίες: {notes}. Το παρόν αποτελεί βάση τιμολόγησης των παραληφθεισών εργασιών."]},
        "supply_recurring": {"t": "Σύμβαση-πλαίσιο περιοδικών προμηθειών", "c": ["Ο Προμηθευτής {b} αναλαμβάνει να παραδίδει περιοδικά στον Αγοραστή {a}: {subject}.", "Οι τιμές καθορίζονται σε {amount} βάσει του συμφωνημένου τιμοκαταλόγου και μεταβάλλονται μόνο εγγράφως, με προειδοποίηση πριν την επόμενη παραγγελία.", "Οι παραγγελίες, ποσότητες, χρονικά παράθυρα παράδοσης και αντικαταστάσεις συμφωνούνται ανά παραγγελία στον κοινό χώρο Seam.", "Ο Προμηθευτής εγγυάται προέλευση, ποιότητα, συνθήκες αποθήκευσης και ψυκτική αλυσίδα όπου απαιτείται, και παρέχει τα νομίμως απαιτούμενα συνοδευτικά έγγραφα.", "Διαδικασία παράδοσης, παραλαβής και παραπόνων: {notes}."]},
    },
    "tr": {
        "fulfillment": {"t": "Depolama ve fulfillment sözleşmesi", "c": ["İşveren {a}, İşletmeciye {b} malların depolanmasını, toplanmasını, paketlenmesini ve sevkiyatını verir: {subject}.", "Kararlaştırılan işlem ücreti {amount}'dır; depolama, ambalaj ve iadeler Seam'de sipariş bazında kararlaştırılan fiyat listesine göre faturalanır.", "İşletmeci kararlaştırılan SLA içinde sevk eder ve her gönderinin durumunu ortak çalışma alanında kaydeder.", "Mallar İşverenin mülkiyetinde kalır; İşletmeci bunları ayrı, sigortalı ve sayımlı olarak saklar.", "Farklar, hasarlar ve iadeler Seam'de fotoğraflı olarak belgelenir. Özel koşullar: {notes}."]},
        "ip_transfer": {"t": "Telif hakkı devri sözleşmesi", "c": ["Eser sahibi {b}, İşveren {a} için şu eserleri yaratır: {subject}.", "{amount} tutarının tam ödenmesiyle Eser sahibi, eserlerin kullanımına ilişkin münhasır mali hakları, yer ve süre sınırlaması olmaksızın İşverene devreder.", "Devir; çoğaltma, yayma, umuma iletim, işleme ve dijital dahil her mecrada kullanımı kapsar.", "Eser sahibi, eserlerin özgün olduğunu ve üçüncü kişilerin haklarını ihlal etmediğini taahhüt eder; üçüncü taraf unsurlar (yazı tipleri, stok, lisanslar) ayrıca listelenir.", "Manevi haklar ve portfolyoda kullanım: {notes}."]},
        "act19": {"t": "Yapılan inşaat işleri tutanağı", "c": ["Taraflar, Yüklenicinin {b} İşveren {a} için şu işleri yaptığını tespit eder: {subject}.", "Bu tutanakla kabul edilen yapılan işlerin bedeli {amount}'dır.", "Metrajlar, birim fiyatlar ve ölçümler ektedir; projeden sapmalar aşağıda belirtilmiştir.", "İşler sahada incelenmiş olup kararlaştırılan şartnameye ve geçerli teknik gerekliliklere uygundur.", "Notlar ve kalan kalemler: {notes}. Bu tutanak, kabul edilen işlerin faturalanmasına esastır."]},
        "supply_recurring": {"t": "Periyodik tedarik çerçeve sözleşmesi", "c": ["Tedarikçi {b}, Alıcı {a} tarafına periyodik olarak teslim etmeyi taahhüt eder: {subject}.", "Fiyatlar kararlaştırılan liste uyarınca {amount} olarak sabittir ve yalnızca yazılı olarak, bir sonraki siparişten önce bildirimle değişir.", "Siparişler, miktarlar, teslim zaman aralıkları ve muadil ürünler ortak Seam alanında sipariş bazında kararlaştırılır.", "Tedarikçi menşei, kaliteyi, saklama koşullarını ve gerektiğinde soğuk zinciri garanti eder ve yasal olarak gereken ekli belgeleri sağlar.", "Teslim, kabul ve şikâyet prosedürü: {notes}."]},
    },
    "it": {
        "fulfillment": {"t": "Contratto di stoccaggio e fulfillment", "c": ["Il Committente {a} affida all'Operatore {b} lo stoccaggio, il prelievo, l'imballaggio e la spedizione delle merci: {subject}.", "Il corrispettivo di gestione concordato è {amount}; stoccaggio, imballaggi e resi sono fatturati secondo il listino concordato per ordine in Seam.", "L'Operatore spedisce entro lo SLA concordato e registra lo stato di ogni spedizione nello spazio di lavoro condiviso.", "Le merci restano di proprietà del Committente; l'Operatore le custodisce separate, assicurate e inventariate.", "Differenze, danni e resi sono documentati con prove fotografiche in Seam. Condizioni particolari: {notes}."]},
        "ip_transfer": {"t": "Contratto di cessione dei diritti d'autore", "c": ["L'Autore {b} realizza per il Committente {a} le seguenti opere: {subject}.", "Con il pagamento integrale di {amount}, l'Autore cede al Committente i diritti patrimoniali esclusivi di utilizzazione delle opere, senza limiti territoriali o temporali.", "La cessione comprende riproduzione, distribuzione, comunicazione al pubblico, elaborazione e uso su qualsiasi supporto, incluso quello digitale.", "L'Autore garantisce che le opere sono originali e non violano diritti di terzi; gli elementi di terzi (font, stock, licenze) sono elencati separatamente.", "Diritti morali e uso nel portfolio: {notes}."]},
        "act19": {"t": "Verbale dei lavori edili eseguiti", "c": ["Le parti accertano che l'Esecutore {b} ha eseguito per il Committente {a} i seguenti lavori: {subject}.", "Il valore dei lavori eseguiti e accettati con il presente verbale è {amount}.", "Quantità, prezzi unitari e misurazioni sono allegati; gli scostamenti dal progetto sono annotati di seguito.", "I lavori sono stati verificati in loco e sono conformi alla specifica concordata e ai requisiti tecnici applicabili.", "Note e voci residue: {notes}. Il presente verbale costituisce base per la fatturazione dei lavori accettati."]},
        "supply_recurring": {"t": "Contratto quadro per forniture ricorrenti", "c": ["Il Fornitore {b} si impegna a consegnare periodicamente all'Acquirente {a}: {subject}.", "I prezzi sono fissati a {amount} secondo il listino concordato e si modificano solo per iscritto, con preavviso prima dell'ordine successivo.", "Ordini, quantità, finestre di consegna e sostituzioni si concordano per ordine nello spazio condiviso Seam.", "Il Fornitore garantisce origine, qualità, condizioni di conservazione e catena del freddo ove applicabile, e fornisce i documenti di accompagnamento richiesti dalla legge.", "Procedura di consegna, accettazione e reclami: {notes}."]},
    },
    "ru": {
        "fulfillment": {"t": "Договор складирования и фулфилмента", "c": ["Заказчик {a} поручает Оператору {b} хранение, комплектацию, упаковку и отправку товаров: {subject}.", "Согласованное вознаграждение за обработку составляет {amount}; хранение, упаковка и возвраты тарифицируются по прайс-листу, согласованному по заказам в Seam.", "Оператор отправляет в рамках согласованного SLA и фиксирует статус каждой отправки в общем рабочем пространстве.", "Товары остаются собственностью Заказчика; Оператор хранит их отдельно, застрахованными и с учётом количества.", "Расхождения, повреждения и возвраты документируются фотоматериалами в Seam. Особые условия: {notes}."]},
        "ip_transfer": {"t": "Договор о передаче авторских прав", "c": ["Автор {b} создаёт для Заказчика {a} следующие произведения: {subject}.", "При полной оплате {amount} Автор передаёт Заказчику исключительные имущественные права на использование произведений, без территориальных и временных ограничений.", "Передача охватывает воспроизведение, распространение, публичный показ, переработку и использование на любом носителе, включая цифровой.", "Автор гарантирует, что произведения оригинальны и не нарушают прав третьих лиц; сторонние элементы (шрифты, стоковые материалы, лицензии) указываются отдельно.", "Личные неимущественные права и использование в портфолио: {notes}."]},
        "act19": {"t": "Акт выполненных строительно-монтажных работ", "c": ["Стороны устанавливают, что Подрядчик {b} выполнил для Заказчика {a} следующие работы: {subject}.", "Стоимость принятых настоящим актом выполненных работ составляет {amount}.", "Объёмы, единичные расценки и обмеры прилагаются; отклонения от проекта отмечены ниже.", "Работы осмотрены на объекте и соответствуют согласованной спецификации и применимым техническим требованиям.", "Примечания и оставшиеся позиции: {notes}. Акт является основанием для выставления счёта за принятые работы."]},
        "supply_recurring": {"t": "Рамочный договор периодических поставок", "c": ["Поставщик {b} обязуется периодически поставлять Покупателю {a}: {subject}.", "Цены зафиксированы на уровне {amount} согласно согласованному прайс-листу и меняются только письменно, с уведомлением до следующего заказа.", "Заказы, количества, окна доставки и замены согласуются по заказам в общем пространстве Seam.", "Поставщик гарантирует происхождение, качество, условия хранения и холодовую цепь там, где это применимо, и предоставляет требуемые законом сопроводительные документы.", "Порядок поставки, приёмки и рекламаций: {notes}."]},
    },
    "es": {
        "fulfillment": {"t": "Contrato de almacenaje y fulfillment", "c": ["El Cliente {a} encomienda al Operador {b} el almacenaje, la preparación, el embalaje y la expedición de mercancías: {subject}.", "La tarifa de gestión acordada es {amount}; el almacenaje, los embalajes y las devoluciones se facturan según la lista de precios acordada por pedido en Seam.", "El Operador expide dentro del SLA acordado y registra el estado de cada envío en el espacio de trabajo compartido.", "Las mercancías siguen siendo propiedad del Cliente; el Operador las conserva separadas, aseguradas e inventariadas.", "Discrepancias, daños y devoluciones se documentan con evidencia fotográfica en Seam. Condiciones especiales: {notes}."]},
        "ip_transfer": {"t": "Contrato de cesión de derechos de autor", "c": ["El Autor {b} crea para el Cliente {a} las siguientes obras: {subject}.", "Con el pago íntegro de {amount}, el Autor cede al Cliente los derechos patrimoniales exclusivos de explotación de las obras, sin limitación territorial ni temporal.", "La cesión comprende reproducción, distribución, comunicación pública, transformación y uso en cualquier soporte, incluido el digital.", "El Autor garantiza que las obras son originales y no infringen derechos de terceros; los elementos de terceros (tipografías, stock, licencias) se relacionan por separado.", "Derechos morales y uso en portfolio: {notes}."]},
        "act19": {"t": "Acta de obras de construcción ejecutadas", "c": ["Las partes constatan que el Contratista {b} ejecutó para el Comitente {a} las siguientes obras: {subject}.", "El valor de las obras ejecutadas y aceptadas mediante la presente acta es {amount}.", "Mediciones, precios unitarios y cubicaciones se adjuntan; las desviaciones respecto al proyecto se anotan a continuación.", "Las obras fueron inspeccionadas in situ y cumplen la especificación acordada y los requisitos técnicos aplicables.", "Observaciones y partidas pendientes: {notes}. La presente acta sirve de base para facturar las obras aceptadas."]},
        "supply_recurring": {"t": "Contrato marco de suministros periódicos", "c": ["El Proveedor {b} se obliga a entregar periódicamente al Comprador {a}: {subject}.", "Los precios quedan fijados en {amount} según la lista acordada y solo se modifican por escrito, con preaviso antes del siguiente pedido.", "Pedidos, cantidades, franjas de entrega y sustituciones se acuerdan por pedido en el espacio compartido de Seam.", "El Proveedor garantiza origen, calidad, condiciones de conservación y cadena de frío cuando proceda, y aporta los documentos de acompañamiento exigidos por ley.", "Procedimiento de entrega, recepción y reclamaciones: {notes}."]},
    },
    "fr": {
        "fulfillment": {"t": "Contrat d'entreposage et de logistique (fulfillment)", "c": ["Le Client {a} confie à l'Opérateur {b} l'entreposage, la préparation, l'emballage et l'expédition des marchandises : {subject}.", "Les frais de traitement convenus s'élèvent à {amount} ; l'entreposage, les emballages et les retours sont facturés selon le tarif convenu par commande dans Seam.", "L'Opérateur expédie dans le SLA convenu et enregistre le statut de chaque expédition dans l'espace de travail partagé.", "Les marchandises restent la propriété du Client ; l'Opérateur les conserve séparées, assurées et inventoriées.", "Écarts, dommages et retours sont documentés avec des preuves photo dans Seam. Conditions particulières : {notes}."]},
        "ip_transfer": {"t": "Contrat de cession de droits d'auteur", "c": ["L'Auteur {b} réalise pour le Client {a} les œuvres suivantes : {subject}.", "Au paiement intégral de {amount}, l'Auteur cède au Client les droits patrimoniaux exclusifs d'exploitation des œuvres, sans limitation territoriale ni temporelle.", "La cession couvre la reproduction, la distribution, la communication au public, l'adaptation et l'utilisation sur tout support, y compris numérique.", "L'Auteur garantit que les œuvres sont originales et ne portent pas atteinte aux droits de tiers ; les éléments de tiers (polices, banques d'images, licences) sont listés séparément.", "Droits moraux et utilisation en portfolio : {notes}."]},
        "act19": {"t": "Procès-verbal de travaux de construction exécutés", "c": ["Les parties constatent que le Prestataire {b} a exécuté pour le Donneur d'ordre {a} les travaux suivants : {subject}.", "La valeur des travaux exécutés et réceptionnés par le présent procès-verbal est de {amount}.", "Les quantités, prix unitaires et métrés sont joints ; les écarts par rapport au projet sont notés ci-dessous.", "Les travaux ont été inspectés sur place et sont conformes à la spécification convenue et aux exigences techniques applicables.", "Remarques et postes restants : {notes}. Le présent procès-verbal sert de base à la facturation des travaux réceptionnés."]},
        "supply_recurring": {"t": "Contrat-cadre de livraisons récurrentes", "c": ["Le Fournisseur {b} s'engage à livrer périodiquement à l'Acheteur {a} : {subject}.", "Les prix sont fixés à {amount} selon le tarif convenu et ne sont modifiés que par écrit, avec préavis avant la commande suivante.", "Les commandes, quantités, créneaux de livraison et substitutions sont convenus par commande dans l'espace partagé Seam.", "Le Fournisseur garantit l'origine, la qualité, les conditions de conservation et la chaîne du froid le cas échéant, et fournit les documents d'accompagnement exigés par la loi.", "Procédure de livraison, de réception et de réclamation : {notes}."]},
    },
    "pl": {
        "fulfillment": {"t": "Umowa magazynowania i fulfillmentu", "c": ["Zleceniodawca {a} powierza Operatorowi {b} magazynowanie, kompletację, pakowanie i wysyłkę towarów: {subject}.", "Uzgodnione wynagrodzenie za obsługę wynosi {amount}; magazynowanie, opakowania i zwroty rozliczane są według cennika uzgodnionego per zamówienie w Seam.", "Operator wysyła w ramach uzgodnionego SLA i odnotowuje status każdej przesyłki we wspólnej przestrzeni roboczej.", "Towary pozostają własnością Zleceniodawcy; Operator przechowuje je oddzielnie, ubezpieczone i zinwentaryzowane.", "Różnice, uszkodzenia i zwroty dokumentuje się dowodami zdjęciowymi w Seam. Warunki szczególne: {notes}."]},
        "ip_transfer": {"t": "Umowa przeniesienia praw autorskich", "c": ["Twórca {b} tworzy dla Zleceniodawcy {a} następujące utwory: {subject}.", "Z chwilą pełnej zapłaty {amount} Twórca przenosi na Zleceniodawcę wyłączne autorskie prawa majątkowe do korzystania z utworów, bez ograniczeń terytorialnych i czasowych.", "Przeniesienie obejmuje zwielokrotnianie, rozpowszechnianie, publiczne udostępnianie, opracowanie i wykorzystanie na każdym nośniku, w tym cyfrowym.", "Twórca zapewnia, że utwory są oryginalne i nie naruszają praw osób trzecich; elementy osób trzecich (czcionki, materiały stockowe, licencje) wymienia się oddzielnie.", "Autorskie prawa osobiste i wykorzystanie w portfolio: {notes}."]},
        "act19": {"t": "Protokół wykonanych robót budowlanych", "c": ["Strony stwierdzają, że Wykonawca {b} wykonał dla Zamawiającego {a} następujące roboty: {subject}.", "Wartość robót wykonanych i odebranych niniejszym protokołem wynosi {amount}.", "Obmiary, ceny jednostkowe i pomiary stanowią załącznik; odstępstwa od projektu odnotowano poniżej.", "Roboty zostały sprawdzone na miejscu i są zgodne z uzgodnioną specyfikacją oraz obowiązującymi wymaganiami technicznymi.", "Uwagi i pozycje pozostałe: {notes}. Niniejszy protokół stanowi podstawę do zafakturowania odebranych robót."]},
        "supply_recurring": {"t": "Umowa ramowa dostaw cyklicznych", "c": ["Dostawca {b} zobowiązuje się cyklicznie dostarczać Kupującemu {a}: {subject}.", "Ceny są ustalone na {amount} zgodnie z uzgodnionym cennikiem i zmieniają się wyłącznie pisemnie, z wyprzedzeniem przed kolejnym zamówieniem.", "Zamówienia, ilości, okna dostaw i zamienniki uzgadnia się per zamówienie we wspólnej przestrzeni Seam.", "Dostawca gwarantuje pochodzenie, jakość, warunki przechowywania i łańcuch chłodniczy tam, gdzie ma to zastosowanie, oraz dostarcza dokumenty wymagane prawem.", "Tryb dostawy, odbioru i reklamacji: {notes}."]},
    },
    "uk": {
        "fulfillment": {"t": "Договір зберігання та фулфілменту", "c": ["Замовник {a} доручає Оператору {b} зберігання, комплектацію, пакування та відправлення товарів: {subject}.", "Погоджена винагорода за обробку становить {amount}; зберігання, пакування та повернення тарифікуються за прайс-листом, погодженим за замовленнями в Seam.", "Оператор відправляє в межах погодженого SLA та фіксує статус кожного відправлення у спільному робочому просторі.", "Товари залишаються власністю Замовника; Оператор зберігає їх окремо, застрахованими та з обліком кількості.", "Розбіжності, пошкодження та повернення документуються фотоматеріалами в Seam. Особливі умови: {notes}."]},
        "ip_transfer": {"t": "Договір про передання авторських прав", "c": ["Автор {b} створює для Замовника {a} такі твори: {subject}.", "За умови повної оплати {amount} Автор передає Замовнику виключні майнові права на використання творів, без територіальних і часових обмежень.", "Передання охоплює відтворення, розповсюдження, публічний показ, перероблення та використання на будь-якому носії, включно з цифровим.", "Автор гарантує, що твори є оригінальними і не порушують прав третіх осіб; сторонні елементи (шрифти, стокові матеріали, ліцензії) зазначаються окремо.", "Особисті немайнові права та використання у портфоліо: {notes}."]},
        "act19": {"t": "Акт виконаних будівельно-монтажних робіт", "c": ["Сторони встановлюють, що Підрядник {b} виконав для Замовника {a} такі роботи: {subject}.", "Вартість прийнятих цим актом виконаних робіт становить {amount}.", "Обсяги, одиничні розцінки та обміри додаються; відхилення від проєкту зазначено нижче.", "Роботи оглянуто на об'єкті, і вони відповідають погодженій специфікації та застосовним технічним вимогам.", "Примітки та залишкові позиції: {notes}. Акт є підставою для виставлення рахунку за прийняті роботи."]},
        "supply_recurring": {"t": "Рамковий договір періодичних поставок", "c": ["Постачальник {b} зобов'язується періодично постачати Покупцю {a}: {subject}.", "Ціни зафіксовані на рівні {amount} згідно з погодженим прайс-листом і змінюються лише письмово, з повідомленням до наступного замовлення.", "Замовлення, кількості, вікна доставки та заміни погоджуються за замовленнями у спільному просторі Seam.", "Постачальник гарантує походження, якість, умови зберігання та холодовий ланцюг там, де це застосовно, і надає передбачені законом супровідні документи.", "Порядок постачання, приймання та рекламацій: {notes}."]},
    },
    "pt": {
        "fulfillment": {"t": "Contrato de armazenagem e fulfillment", "c": ["O Cliente {a} confia ao Operador {b} a armazenagem, preparação, embalagem e expedição de mercadorias: {subject}.", "A taxa de processamento acordada é {amount}; armazenagem, embalagens e devoluções são faturadas segundo a tabela acordada por encomenda no Seam.", "O Operador expede dentro do SLA acordado e regista o estado de cada expedição no espaço de trabalho partilhado.", "As mercadorias mantêm-se propriedade do Cliente; o Operador guarda-as separadas, seguradas e inventariadas.", "Divergências, danos e devoluções são documentados com provas fotográficas no Seam. Condições especiais: {notes}."]},
        "ip_transfer": {"t": "Contrato de cessão de direitos de autor", "c": ["O Autor {b} cria para o Cliente {a} as seguintes obras: {subject}.", "Com o pagamento integral de {amount}, o Autor cede ao Cliente os direitos patrimoniais exclusivos de exploração das obras, sem limitação territorial ou temporal.", "A cessão abrange reprodução, distribuição, comunicação ao público, transformação e utilização em qualquer suporte, incluindo digital.", "O Autor garante que as obras são originais e não violam direitos de terceiros; os elementos de terceiros (tipos de letra, stock, licenças) são listados separadamente.", "Direitos morais e utilização em portfólio: {notes}."]},
        "act19": {"t": "Auto de trabalhos de construção executados", "c": ["As partes constatam que o Empreiteiro {b} executou para o Dono da obra {a} os seguintes trabalhos: {subject}.", "O valor dos trabalhos executados e aceites pelo presente auto é {amount}.", "Medições, preços unitários e levantamentos seguem em anexo; os desvios face ao projeto são anotados abaixo.", "Os trabalhos foram verificados no local e cumprem a especificação acordada e os requisitos técnicos aplicáveis.", "Observações e trabalhos remanescentes: {notes}. O presente auto serve de base à faturação dos trabalhos aceites."]},
        "supply_recurring": {"t": "Contrato-quadro de fornecimentos periódicos", "c": ["O Fornecedor {b} obriga-se a entregar periodicamente ao Comprador {a}: {subject}.", "Os preços ficam fixados em {amount} conforme a tabela acordada e só se alteram por escrito, com aviso prévio antes da encomenda seguinte.", "Encomendas, quantidades, janelas de entrega e substituições são acordadas por encomenda no espaço partilhado Seam.", "O Fornecedor garante origem, qualidade, condições de conservação e cadeia de frio quando aplicável, e fornece os documentos de acompanhamento exigidos por lei.", "Procedimento de entrega, receção e reclamações: {notes}."]},
    },
}
for _l, _docs in DOC_SECTOR.items():
    if _l in DOC_I18N:
        DOC_I18N[_l].update(_docs)


# Agriculture sector document: harvest purchase contract.
DOC_AGRI = {
    "en": {"harvest_supply": {"t": "Harvest purchase agreement", "c": ["The Producer {b} sells and the Buyer {a} purchases the following agricultural produce: {subject}.", "The agreed price is {amount} per tonne, on the delivery term stated in the order in Seam.", "Quantity, quality parameters (moisture, impurities, hectolitre weight, grain damage) and the acceptance tolerances are agreed per lot; sampling and analysis are performed on acceptance.", "The Producer warrants that the produce comes from its own or lawfully used land, meets food and feed safety requirements, and is accompanied by the documents required by law.", "Delivery, weighing, storage and payment terms: {notes}."]}},
    "bg": {"harvest_supply": {"t": "Договор за изкупуване на реколта", "c": ["Производителят {b} продава, а Изкупвачът {a} купува следната земеделска продукция: {subject}.", "Договорената цена е {amount} за тон, при условие на доставка, посочено в поръчката в Seam.", "Количеството, качествените показатели (влага, примеси, хектолитрово тегло, зърнени повреди) и допустимите отклонения се договарят по партиди; пробовземането и анализът се извършват при приемане.", "Производителят гарантира, че продукцията е от собствени или законно ползвани площи, отговаря на изискванията за безопасност на храните и фуражите и се придружава от изискуемите по закон документи.", "Условия за доставка, претегляне, съхранение и плащане: {notes}."]}},
    "de": {"harvest_supply": {"t": "Kaufvertrag über Ernteerzeugnisse", "c": ["Der Erzeuger {b} verkauft und der Käufer {a} kauft folgende landwirtschaftliche Erzeugnisse: {subject}.", "Der vereinbarte Preis beträgt {amount} je Tonne, zu der im Auftrag in Seam genannten Lieferbedingung.", "Menge, Qualitätsparameter (Feuchte, Besatz, Hektolitergewicht, Kornbeschädigung) und Annahmetoleranzen werden je Partie vereinbart; Probenahme und Analyse erfolgen bei der Annahme.", "Der Erzeuger sichert zu, dass die Erzeugnisse von eigenen oder rechtmäßig genutzten Flächen stammen, den Lebens- und Futtermittelsicherheitsanforderungen entsprechen und von den gesetzlich vorgeschriebenen Dokumenten begleitet werden.", "Liefer-, Verwiege-, Lager- und Zahlungsbedingungen: {notes}."]}},
    "ro": {"harvest_supply": {"t": "Contract de achiziție a recoltei", "c": ["Producătorul {b} vinde, iar Cumpărătorul {a} achiziționează următoarea producție agricolă: {subject}.", "Prețul convenit este {amount} pe tonă, la condiția de livrare indicată în comanda din Seam.", "Cantitatea, parametrii de calitate (umiditate, impurități, masa hectolitrică, boabe deteriorate) și toleranțele de recepție se convin pe loturi; prelevarea probelor și analiza se fac la recepție.", "Producătorul garantează că producția provine de pe terenuri proprii sau folosite legal, îndeplinește cerințele de siguranță alimentară și a furajelor și este însoțită de documentele cerute de lege.", "Condiții de livrare, cântărire, depozitare și plată: {notes}."]}},
    "el": {"harvest_supply": {"t": "Σύμβαση αγοράς σοδειάς", "c": ["Ο Παραγωγός {b} πωλεί και ο Αγοραστής {a} αγοράζει την εξής γεωργική παραγωγή: {subject}.", "Η συμφωνημένη τιμή είναι {amount} ανά τόνο, με τον όρο παράδοσης που αναφέρεται στην παραγγελία στο Seam.", "Η ποσότητα, τα ποιοτικά χαρακτηριστικά (υγρασία, προσμείξεις, εκατολιτρικό βάρος, φθορά κόκκων) και οι ανοχές παραλαβής συμφωνούνται ανά παρτίδα· η δειγματοληψία και η ανάλυση γίνονται κατά την παραλαβή.", "Ο Παραγωγός εγγυάται ότι η παραγωγή προέρχεται από ιδιόκτητες ή νομίμως χρησιμοποιούμενες εκτάσεις, πληροί τις απαιτήσεις ασφάλειας τροφίμων και ζωοτροφών και συνοδεύεται από τα νομίμως απαιτούμενα έγγραφα.", "Όροι παράδοσης, ζύγισης, αποθήκευσης και πληρωμής: {notes}."]}},
    "tr": {"harvest_supply": {"t": "Hasat alım sözleşmesi", "c": ["Üretici {b} satar ve Alıcı {a} şu tarımsal ürünü satın alır: {subject}.", "Kararlaştırılan fiyat ton başına {amount} olup, Seam'deki siparişte belirtilen teslim şartıyla geçerlidir.", "Miktar, kalite parametreleri (nem, yabancı madde, hektolitre ağırlığı, tane hasarı) ve kabul toleransları parti bazında kararlaştırılır; numune alma ve analiz kabulde yapılır.", "Üretici, ürünün kendi veya hukuka uygun kullanılan arazilerden geldiğini, gıda ve yem güvenliği gerekliliklerini karşıladığını ve yasal olarak gerekli belgelerle birlikte geldiğini taahhüt eder.", "Teslim, tartım, depolama ve ödeme koşulları: {notes}."]}},
    "it": {"harvest_supply": {"t": "Contratto di acquisto del raccolto", "c": ["Il Produttore {b} vende e l'Acquirente {a} acquista i seguenti prodotti agricoli: {subject}.", "Il prezzo concordato è {amount} a tonnellata, alla condizione di consegna indicata nell'ordine in Seam.", "Quantità, parametri di qualità (umidità, impurità, peso ettolitrico, danneggiamento della granella) e tolleranze di accettazione si concordano per lotto; campionamento e analisi si effettuano all'accettazione.", "Il Produttore garantisce che la produzione proviene da terreni propri o legittimamente utilizzati, soddisfa i requisiti di sicurezza alimentare e dei mangimi ed è accompagnata dai documenti richiesti dalla legge.", "Condizioni di consegna, pesatura, stoccaggio e pagamento: {notes}."]}},
    "ru": {"harvest_supply": {"t": "Договор закупки урожая", "c": ["Производитель {b} продаёт, а Покупатель {a} приобретает следующую сельскохозяйственную продукцию: {subject}.", "Согласованная цена составляет {amount} за тонну, на условии поставки, указанном в заказе в Seam.", "Количество, качественные показатели (влажность, сорная примесь, натура, повреждение зерна) и приёмные допуски согласуются по партиям; отбор проб и анализ производятся при приёмке.", "Производитель гарантирует, что продукция получена с собственных или законно используемых площадей, соответствует требованиям безопасности пищевых продуктов и кормов и сопровождается требуемыми законом документами.", "Условия поставки, взвешивания, хранения и оплаты: {notes}."]}},
    "es": {"harvest_supply": {"t": "Contrato de compra de cosecha", "c": ["El Productor {b} vende y el Comprador {a} adquiere la siguiente producción agrícola: {subject}.", "El precio acordado es {amount} por tonelada, en la condición de entrega indicada en el pedido en Seam.", "La cantidad, los parámetros de calidad (humedad, impurezas, peso hectolítrico, grano dañado) y las tolerancias de recepción se acuerdan por lote; el muestreo y el análisis se realizan en la recepción.", "El Productor garantiza que la producción procede de terrenos propios o legalmente utilizados, cumple los requisitos de seguridad alimentaria y de piensos y va acompañada de los documentos exigidos por ley.", "Condiciones de entrega, pesaje, almacenamiento y pago: {notes}."]}},
    "fr": {"harvest_supply": {"t": "Contrat d'achat de récolte", "c": ["Le Producteur {b} vend et l'Acheteur {a} achète la production agricole suivante : {subject}.", "Le prix convenu est de {amount} la tonne, à la condition de livraison indiquée dans la commande dans Seam.", "La quantité, les paramètres de qualité (humidité, impuretés, poids spécifique, grains endommagés) et les tolérances de réception sont convenus par lot ; l'échantillonnage et l'analyse sont effectués à la réception.", "Le Producteur garantit que la production provient de terres lui appartenant ou légalement exploitées, satisfait aux exigences de sécurité alimentaire et des aliments pour animaux et est accompagnée des documents exigés par la loi.", "Conditions de livraison, de pesée, de stockage et de paiement : {notes}."]}},
    "pl": {"harvest_supply": {"t": "Umowa skupu plonów", "c": ["Producent {b} sprzedaje, a Kupujący {a} nabywa następującą produkcję rolną: {subject}.", "Uzgodniona cena wynosi {amount} za tonę, na warunku dostawy wskazanym w zamówieniu w Seam.", "Ilość, parametry jakościowe (wilgotność, zanieczyszczenia, gęstość w stanie zsypnym, uszkodzenia ziarna) oraz tolerancje odbioru uzgadnia się dla każdej partii; pobór próbek i analiza następują przy odbiorze.", "Producent gwarantuje, że produkcja pochodzi z gruntów własnych lub legalnie użytkowanych, spełnia wymagania bezpieczeństwa żywności i pasz oraz jest zaopatrzona w dokumenty wymagane prawem.", "Warunki dostawy, ważenia, przechowywania i płatności: {notes}."]}},
    "uk": {"harvest_supply": {"t": "Договір закупівлі врожаю", "c": ["Виробник {b} продає, а Покупець {a} придбаває таку сільськогосподарську продукцію: {subject}.", "Погоджена ціна становить {amount} за тонну, на умові постачання, зазначеній у замовленні в Seam.", "Кількість, показники якості (вологість, домішки, натура, пошкодження зерна) та приймальні допуски погоджуються за партіями; відбір проб і аналіз виконуються під час приймання.", "Виробник гарантує, що продукція одержана з власних або законно використовуваних площ, відповідає вимогам безпечності харчових продуктів і кормів та супроводжується документами, передбаченими законом.", "Умови постачання, зважування, зберігання та оплати: {notes}."]}},
    "pt": {"harvest_supply": {"t": "Contrato de compra de colheita", "c": ["O Produtor {b} vende e o Comprador {a} adquire a seguinte produção agrícola: {subject}.", "O preço acordado é {amount} por tonelada, na condição de entrega indicada na encomenda no Seam.", "A quantidade, os parâmetros de qualidade (humidade, impurezas, peso hectolítrico, grão danificado) e as tolerâncias de receção são acordados por lote; a amostragem e a análise realizam-se na receção.", "O Produtor garante que a produção provém de terrenos próprios ou legalmente utilizados, cumpre os requisitos de segurança alimentar e de alimentos para animais e é acompanhada dos documentos exigidos por lei.", "Condições de entrega, pesagem, armazenamento e pagamento: {notes}."]}},
}
for _l, _docs in DOC_AGRI.items():
    if _l in DOC_I18N:
        DOC_I18N[_l].update(_docs)


DOC_KINDS = ("handover", "nda", "service", "poa", "subcontract", "claim", "gdpr", "site",
             "car_sale", "car_handover", "invoice", "proforma_doc",
             "transport_order", "cmr", "reconciliation", "consignment",
             "commercial_offer", "delivery_note", "car_customs",
             "fulfillment", "ip_transfer", "act19", "supply_recurring", "harvest_supply")


def _fmt_amount(raw):
    """One currency on a document, and it is the euro.

    Bulgaria's statutory dual display ran from 8 August 2025 to 8 August 2026.
    That window has closed, so a lev figure alongside the euro is no longer a
    courtesy owed to anyone - it is a second number on a legal document with
    nothing requiring it to be there.
    """
    try:
        amt = float(str(raw).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return "{:,.2f}".format(amt).replace(",", " ") + " €"


DOC_STYLE = """
html{background:#eef1f5}
*{box-sizing:border-box}
body{font-family:'Segoe UI',Arial,sans-serif;color:#1a1f2b;background:#eef1f5;margin:0;padding:28px 16px}
.sheet{max-width:760px;margin:0 auto;background:#fff;border-radius:14px;box-shadow:0 10px 40px rgba(20,30,60,.14);
  padding:44px 46px;position:relative;overflow:hidden}
.sheet::before{content:"";position:absolute;top:0;left:0;right:0;height:6px;
  background:linear-gradient(90deg,#2f6df0,#7a4fd0)}
.brandmark{display:flex;align-items:center;gap:9px;margin-bottom:26px}
.brandmark .logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#2f6df0,#7a4fd0);
  display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;font-size:16px}
.brandmark .bt{font-weight:750;font-size:15px}.brandmark .bs{color:#8a93a3;font-size:11.5px;font-style:italic}
h1{font-size:21px;margin:0 0 3px}.doc-sub{color:#5b6472;font-size:13px;margin-bottom:24px}
.meta{color:#5b6472;font-size:13.5px;margin-bottom:22px;line-height:1.7}
ol{padding-left:20px;margin:0}li{margin-bottom:12px;font-size:14px;line-height:1.65}
table{width:100%;border-collapse:collapse;margin:20px 0}
th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#8a93a3;
  border-bottom:2px solid #e6e8ec;padding:8px 6px}
td{padding:10px 6px;border-bottom:1px solid #eef0f3;font-size:13.5px;vertical-align:top}
td.r,th.r{text-align:right}
.tot{font-size:17px;font-weight:800}.tot td{border-top:2px solid #1a1f2b;border-bottom:none;padding-top:12px}
.parties{display:flex;gap:24px;flex-wrap:wrap;margin-bottom:20px}
.party{flex:1;min-width:200px;background:#f7f8fa;border:1px solid #eceef1;border-radius:10px;padding:14px 16px}
.party .pl{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8a93a3;margin-bottom:5px}
.party .pn{font-weight:650;font-size:14px}.party .pr{color:#5b6472;font-size:12.5px;margin-top:2px}
.sigs{display:flex;gap:40px;margin-top:46px}.sig{flex:1;border-top:1.5px solid #1a1f2b;padding-top:8px;font-size:13px}
.sig b{display:block;margin-bottom:26px}.disc{margin-top:30px;color:#9aa3b2;font-size:11.5px;border-top:1px solid #eef0f3;padding-top:14px}
.paybox{background:#f7f8fa;border:1px solid #eceef1;border-radius:10px;padding:13px 16px;margin-top:18px}
.paybox .pbt{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8a93a3;margin-bottom:7px}
.paybox div{font-size:13px;margin-top:3px}.paybox span{color:#5b6472;display:inline-block;min-width:78px}
.sig .cmp{display:block;font-weight:600;font-size:13px;margin:-20px 0 20px}
@media print{html,body{background:#fff}.sheet{box-shadow:none;border-radius:0;padding:6mm 4mm;max-width:100%}
  table{page-break-inside:auto}tr{page-break-inside:avoid}thead{display:table-header-group}}
"""


DOC_MOTTO = {
    "en": "In sync with every partner.", "bg": "Синхрон с всеки партньор.",
    "de": "Synchron mit jedem Partner.", "ro": "În sincron cu fiecare partener.",
    "el": "Σε συγχρονισμό με κάθε συνεργάτη.", "tr": "Her iş ortağıyla senkron.",
    "it": "In sincronia con ogni partner.", "ru": "В синхроне с каждым партнёром.",
    "es": "en sintonía con cada socio", "fr": "en phase avec chaque partenaire",
    "pl": "w zgraniu z każdym partnerem", "uk": "у синхроні з кожним партнером",
    "pt": "em sintonia com cada parceiro",
}


def _doc_lang(table):
    """The language a generated document should be written in.

    `?lang=` wins, because the person generating it is choosing on behalf of
    whoever will read it. Without it, their own interface language."""
    want = (request.args.get("lang") or "").strip().lower()
    if want in table:
        return want
    return req_lang() if req_lang() in table else "en"


def _doc_brandmark(lang=None):
    """The language must be passed in wherever the page does not come from the
    reader's own cookie. A public reference is read by someone who has no
    account here and chose their language in the URL; taking it from the cookie
    put a Bulgarian motto on top of a German page."""
    sub = DOC_MOTTO.get(lang or req_lang(), DOC_MOTTO["en"])
    return ('<div class="brandmark"><div class="logo">S</div>'
            '<div><div class="bt">Seam</div><div class="bs">%s</div></div></div>' % sub)


#: The one bar on a generated document. A document is a web page here, not a
#: file: there is no PDF generator, because that means a compiled dependency and
#: this application has none. The browser's own print engine produces a proper
#: PDF with the right fonts for all thirteen languages - it just has to be
#: offered, instead of leaving people to guess at Ctrl+P.
PRINT_I18N = {
    "en": {"dl": "Download PDF", "dl_hint": "A finished file, ready to attach to an email.",
           "working": "Preparing the file…", "print": "Print",
           "save": "Save as PDF", "hint": "Opens your browser's print dialogue - choose “Save as PDF”.",
           "paid": "Printing and saving as PDF are part of a paid plan. This sheet carries a demo watermark.",
           "failed": "The PDF could not be produced. Please use Print and choose “Save as PDF”."},
    "bg": {"dl": "Изтегляне на PDF", "dl_hint": "Готов файл за прикачване към имейл.",
           "working": "Файлът се подготвя…", "print": "Печат",
           "save": "Запазване като PDF", "hint": "Отваря диалога за печат на браузъра - изберете „Запази като PDF“.",
           "paid": "Печатът и запазването като PDF са част от платен план. Този лист носи демонстрационен воден знак.",
           "failed": "PDF файлът не можа да бъде създаден. Използвайте „Печат“ и изберете „Запази като PDF“."},
    "de": {"dl": "PDF herunterladen", "dl_hint": "Eine fertige Datei, bereit zum Anhängen an eine E-Mail.",
           "working": "Die Datei wird vorbereitet…", "print": "Drucken",
           "save": "Als PDF speichern", "hint": "Öffnet den Druckdialog Ihres Browsers - wählen Sie „Als PDF speichern“.",
           "paid": "Drucken und Speichern als PDF gehören zu einem kostenpflichtigen Tarif. Dieses Blatt trägt ein Demo-Wasserzeichen.",
           "failed": "Das PDF konnte nicht erstellt werden. Bitte nutzen Sie „Drucken“ und wählen Sie „Als PDF speichern“."},
    "ro": {"dl": "Descărcați PDF", "dl_hint": "Un fișier gata de atașat la un e-mail.",
           "working": "Se pregătește fișierul…", "print": "Tipărire",
           "save": "Salvați ca PDF", "hint": "Deschide dialogul de tipărire al browserului - alegeți „Salvare ca PDF”.",
           "paid": "Tipărirea și salvarea ca PDF fac parte dintr-un plan plătit. Această filă poartă un filigran demonstrativ.",
           "failed": "Fișierul PDF nu a putut fi creat. Folosiți „Tipărire” și alegeți „Salvare ca PDF”."},
    "el": {"dl": "Λήψη PDF", "dl_hint": "Έτοιμο αρχείο για επισύναψη σε μήνυμα.",
           "working": "Το αρχείο ετοιμάζεται…", "print": "Εκτύπωση",
           "save": "Αποθήκευση ως PDF", "hint": "Ανοίγει τον διάλογο εκτύπωσης του προγράμματος περιήγησης - επιλέξτε «Αποθήκευση ως PDF».",
           "paid": "Η εκτύπωση και η αποθήκευση ως PDF ανήκουν σε πληρωμένο πλάνο. Το φύλλο φέρει δοκιμαστικό υδατογράφημα.",
           "failed": "Το PDF δεν ήταν δυνατό να δημιουργηθεί. Χρησιμοποιήστε «Εκτύπωση» και επιλέξτε «Αποθήκευση ως PDF»."},
    "tr": {"dl": "PDF indir", "dl_hint": "E-postaya eklemeye hazır bir dosya.",
           "working": "Dosya hazırlanıyor…", "print": "Yazdır",
           "save": "PDF olarak kaydet", "hint": "Tarayıcınızın yazdırma penceresini açar - “PDF olarak kaydet” seçin.",
           "paid": "Yazdırma ve PDF olarak kaydetme ücretli planın parçasıdır. Bu sayfa deneme filigranı taşır.",
           "failed": "PDF oluşturulamadı. Lütfen “Yazdır” ile “PDF olarak kaydet” seçeneğini kullanın."},
    "it": {"dl": "Scarica il PDF", "dl_hint": "Un file pronto da allegare a un'e-mail.",
           "working": "Preparazione del file…", "print": "Stampa",
           "save": "Salva come PDF", "hint": "Apre la finestra di stampa del browser: scelga «Salva come PDF».",
           "paid": "La stampa e il salvataggio in PDF fanno parte di un piano a pagamento. Questo foglio porta una filigrana dimostrativa.",
           "failed": "Non è stato possibile creare il PDF. Usi «Stampa» e scelga «Salva come PDF»."},
    "ru": {"dl": "Скачать PDF", "dl_hint": "Готовый файл для вложения в письмо.",
           "working": "Файл готовится…", "print": "Печать",
           "save": "Сохранить как PDF", "hint": "Открывает диалог печати браузера - выберите «Сохранить как PDF».",
           "paid": "Печать и сохранение в PDF входят в платный тариф. На этом листе демонстрационный водяной знак.",
           "failed": "PDF не удалось создать. Воспользуйтесь печатью и выберите «Сохранить как PDF»."},
    "es": {"dl": "Descargar PDF", "dl_hint": "Un archivo listo para adjuntar a un correo.",
           "working": "Preparando el archivo…", "print": "Imprimir",
           "save": "Guardar como PDF", "hint": "Abre el diálogo de impresión del navegador: elija «Guardar como PDF».",
           "paid": "Imprimir y guardar como PDF forman parte de un plan de pago. Esta hoja lleva una marca de agua de demostración.",
           "failed": "No se ha podido crear el PDF. Use «Imprimir» y elija «Guardar como PDF»."},
    "fr": {"dl": "Télécharger le PDF", "dl_hint": "Un fichier prêt à joindre à un courriel.",
           "working": "Préparation du fichier…", "print": "Imprimer",
           "save": "Enregistrer en PDF", "hint": "Ouvre la fenêtre d'impression du navigateur : choisissez « Enregistrer au format PDF ».",
           "paid": "L'impression et l'enregistrement en PDF font partie d'une formule payante. Cette feuille porte un filigrane de démonstration.",
           "failed": "Le PDF n'a pas pu être créé. Utilisez « Imprimer » et choisissez « Enregistrer au format PDF »."},
    "pl": {"dl": "Pobierz PDF", "dl_hint": "Gotowy plik do załączenia do wiadomości.",
           "working": "Trwa przygotowywanie pliku…", "print": "Drukuj",
           "save": "Zapisz jako PDF", "hint": "Otwiera okno drukowania przeglądarki - proszę wybrać „Zapisz jako PDF”.",
           "paid": "Drukowanie i zapis do PDF należą do planu płatnego. Ten arkusz nosi znak wodny wersji demonstracyjnej.",
           "failed": "Nie udało się utworzyć pliku PDF. Proszę użyć drukowania i wybrać „Zapisz jako PDF”."},
    "uk": {"dl": "Завантажити PDF", "dl_hint": "Готовий файл для вкладення в лист.",
           "working": "Файл готується…", "print": "Друк",
           "save": "Зберегти як PDF", "hint": "Відкриває діалог друку браузера - оберіть «Зберегти як PDF».",
           "paid": "Друк і збереження в PDF входять до платного тарифу. На цьому аркуші демонстраційний водяний знак.",
           "failed": "PDF не вдалося створити. Скористайтеся друком і оберіть «Зберегти як PDF»."},
    "pt": {"dl": "Descarregar PDF", "dl_hint": "Um ficheiro pronto a anexar a uma mensagem.",
           "working": "A preparar o ficheiro…", "print": "Imprimir",
           "save": "Guardar como PDF", "hint": "Abre a caixa de impressão do navegador: escolha «Guardar como PDF».",
           "paid": "Imprimir e guardar como PDF fazem parte de um plano pago. Esta folha tem uma marca de água de demonstração.",
           "failed": "Não foi possível criar o PDF. Utilize «Imprimir» e escolha «Guardar como PDF»."},
}

PRINT_STYLE = """
.pbar{max-width:760px;margin:0 auto 14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.pbar button,.pbar a.dl{padding:10px 18px;border:0;border-radius:8px;background:#2f6df0;color:#fff;
  font-size:14px;font-weight:650;font-family:inherit;cursor:pointer;text-decoration:none;
  display:inline-block;line-height:1.2}
.pbar button:hover,.pbar a.dl:hover{background:#2a5fd0}
.pbar a.dl.busy{background:#8aa5e0;pointer-events:none}
.pbar .alt{background:none;color:#2f6df0;padding:10px 4px;font-weight:600;text-decoration:underline}
.pbar .alt:hover{background:none;color:#2a5fd0}
.pbar .ph{color:#5b6472;font-size:12.5px}
.pbar .ph.bad{color:#a3341f}
.pbar.locked{background:#fdf3e0;border:1px solid #e8d5a8;border-radius:10px;padding:11px 14px;
  color:#6b5620;font-size:12.5px;display:block}
@media print{.noprint{display:none!important}}
"""


def _pdf_href():
    """The same document, as a file. Only offered where a `.pdf` sibling route
    exists - set by whichever route is answering."""
    base = getattr(g, "_doc_pdf_base", None)
    if not base:
        return ""
    qs = request.query_string.decode("ascii", "ignore")
    return base + ("?" + qs if qs else "")


def _pdf_bar(lang, printable=True, free=False):
    """The strip above a sheet that turns it into a file."""
    P = PRINT_I18N.get(lang, PRINT_I18N["en"])
    if not printable:
        return ""
    if free:
        # Printing really is blocked on the free plan; saying so is better than
        # a print dialogue that yields a blank page.
        return '<div class="pbar locked noprint">%s</div>' % html_escape(P["paid"])
    href = _pdf_href()
    if href and pdfout.available():
        # A real file. The link works on its own, so the download survives a
        # blocked script; docprint.js only adds the "preparing" state, because
        # rendering takes a couple of seconds and silence looks like a dud
        # button.
        return ('<div class="pbar noprint">'
                '<a class="dl" id="pdfBtn" href="%s" download rel="nofollow">%s</a>'
                '<button class="alt" id="printBtn" type="button">%s</button>'
                '<span class="ph" id="pdfMsg" data-working="%s" data-failed="%s">%s</span></div>'
                % (html_escape(href), html_escape(P["dl"]), html_escape(P["print"]),
                   html_escape(P["working"]), html_escape(P["failed"]),
                   html_escape(P["dl_hint"])))
    # No renderer installed on this machine: the browser's own dialogue is the
    # honest fallback, and it is labelled as what it actually does.
    return ('<div class="pbar noprint"><button id="printBtn" type="button">%s</button>'
            '<span class="ph">%s</span></div>'
            % (html_escape(P["save"]), html_escape(P["hint"])))


def _doc_shell(lang, title, inner, printable=True):
    """Wraps a generated document. On the free plan the sheet carries a demo
    watermark and casual copying is discouraged (selection/context menu off,
    printing blocked). Note: no web page can truly prevent a screenshot - the
    watermark is what makes a leaked copy identifiable.

    `printable` adds the bar that turns the page into a file. It is off for the
    signing page, which is a form to fill in rather than a sheet to keep.
    """
    free = bool(getattr(g, "_doc_free", False))
    bar = _pdf_bar(lang, printable=printable, free=free)
    extra, overlay = PRINT_STYLE, ""
    if free:
        mark = DOC_WATERMARK.get(lang, DOC_WATERMARK["en"])
        extra += ("""
.sheet{user-select:none;-webkit-user-select:none}
.wm{position:fixed;inset:0;pointer-events:none;z-index:9;overflow:hidden}
.wm span{position:absolute;top:50%%;left:50%%;transform:translate(-50%%,-50%%) rotate(-30deg);
  font-size:44px;font-weight:800;color:rgba(47,109,240,.10);white-space:nowrap;letter-spacing:.06em}
.wm span:nth-child(2){top:20%%}.wm span:nth-child(3){top:80%%}
@media print{body{display:none!important}}
""")
        overlay = ('<div class="wm"><span>%s</span><span>%s</span><span>%s</span></div>'
                   % (html_escape(mark), html_escape(mark), html_escape(mark)))
    # Scripts must be external: these pages run under script-src 'self', which
    # blocks inline <script> outright.
    scripts = ""
    if free:
        scripts += '<script src="/static/js/docguard.js?v=%s"></script>' % _asset_build()
    elif printable:
        scripts += '<script src="/static/js/docprint.js?v=%s"></script>' % _asset_build()
    return ("""<!DOCTYPE html><html lang="%(lang)s"><head><meta charset="utf-8">
<meta name="color-scheme" content="light"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title><style>%(style)s%(extra)s</style></head>
<body>%(overlay)s%(bar)s<div class="sheet">%(inner)s</div>%(scripts)s</body></html>""") % {
        "lang": lang, "title": html_escape(title), "style": DOC_STYLE,
        "extra": extra, "overlay": overlay, "bar": bar, "inner": inner,
        "scripts": scripts}


# Anything outside this set is replaced in the ASCII fallback file name. The
# UTF-8 name goes in `filename*`, which every current browser prefers; the
# fallback only matters to old clients and to mail programs that re-read it.
_FN_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _pdf_filename(lang, title, number=""):
    base = ("%s %s" % (title, number)).strip() or "document"
    ascii_name = _FN_SAFE.sub("-", base).strip("-.")
    if not ascii_name or ascii_name.strip("-.") == "":
        ascii_name = "document"
    return base[:80] + ".pdf", ascii_name[:80] + ".pdf"


def _doc_out(lang, title, inner, printable=True, number=""):
    """One exit for a generated sheet: the page to look at, or the file to send.

    Which one is decided by the route (`.pdf` or not), so the two can never
    drift apart - the PDF is this exact HTML, rendered."""
    if not getattr(g, "_doc_pdf", False):
        return _doc_shell(lang, title, inner, printable=printable)
    P = PRINT_I18N.get(lang, PRINT_I18N["en"])
    if getattr(g, "_doc_free", False):
        return _doc_shell(lang, title, inner, printable=printable), 402
    # The bar is not part of the document, so it is not rendered into it.
    html = _doc_shell(lang, title, inner, printable=False)
    data = pdfout.render(html)
    if not data:
        app.logger.warning("pdf render unavailable or failed (renderer=%r)", pdfout.find_renderer())
        return _doc_shell(lang, title, inner, printable=printable), 503
    utf8_name, ascii_name = _pdf_filename(lang, title, number)
    resp = make_response(data)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = (
        "attachment; filename=\"%s\"; filename*=UTF-8''%s"
        % (ascii_name, urllib.parse.quote(utf8_name, safe="")))
    resp.headers["Content-Length"] = str(len(data))
    # A generated invoice is not a public asset; it must not sit in a proxy.
    resp.headers["Cache-Control"] = "no-store, private"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _page_out(page, lang, title, number=""):
    """The same two exits, for a page that builds its own HTML instead of going
    through `_doc_shell` - the order protocol. The bar is injected rather than
    written into the template, so there is one bar in the product, not two."""
    if getattr(g, "_doc_pdf", False):
        data = pdfout.render(page)
        if not data:
            app.logger.warning("pdf render unavailable or failed (renderer=%r)", pdfout.find_renderer())
        else:
            utf8_name, ascii_name = _pdf_filename(lang, title, number)
            resp = make_response(data)
            resp.headers["Content-Type"] = "application/pdf"
            resp.headers["Content-Disposition"] = (
                "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                % (ascii_name, urllib.parse.quote(utf8_name, safe="")))
            resp.headers["Content-Length"] = str(len(data))
            resp.headers["Cache-Control"] = "no-store, private"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp
    bar = _pdf_bar(lang)
    if not bar:
        return page
    page = page.replace("</style>", PRINT_STYLE + "</style>", 1)
    page = page.replace("<body>", "<body>" + bar, 1)
    page = page.replace("</body>", '<script src="/static/js/docprint.js?v=%s"></script></body>'
                        % _asset_build(), 1)
    return page


# --------------------------------------------------------------------------- #
#  Document library entitlements
#
#  The ready-made templates are part of the paid plan. A free (Starter) account
#  may use FREE_DOC_LIMIT different templates so it can try the library; the
#  documents it produces carry a demo watermark and are protected against
#  casual copying. Paid plans get the full library, clean.
# --------------------------------------------------------------------------- #
FREE_DOC_LIMIT = 2
#: An annual plan and a one-off lifetime plan sit beside the monthly one
#: and the enterprise contract. "life" carries no end date, which the
#: no-end branch in org_is_paid already reads as no end.
PAID_PLANS = ("pro", "ent", "year", "life")

DOC_LOCK_I18N = {
    "en": {"t": "Template limit reached", "p": "Your free plan includes %d document templates. Upgrade to unlock the full library of %d ready-made documents.", "cta": "See plans", "used": "Already used"},
    "bg": {"t": "Достигнат лимит на шаблоните", "p": "Безплатният план включва %d образеца. Преминете към платен план, за да отключите цялата библиотека от %d готови документа.", "cta": "Виж плановете", "used": "Вече използвани"},
    "de": {"t": "Vorlagenlimit erreicht", "p": "Ihr kostenloser Tarif enthält %d Dokumentvorlagen. Upgraden Sie, um die vollständige Bibliothek mit %d Dokumenten freizuschalten.", "cta": "Tarife ansehen", "used": "Bereits genutzt"},
    "ro": {"t": "Limita de șabloane atinsă", "p": "Planul gratuit include %d modele. Treceți la un plan plătit pentru a debloca biblioteca completă de %d documente.", "cta": "Vezi planurile", "used": "Deja folosite"},
    "el": {"t": "Συμπληρώθηκε το όριο προτύπων", "p": "Το δωρεάν πλάνο περιλαμβάνει %d υποδείγματα. Αναβαθμίστε για να ξεκλειδώσετε τη βιβλιοθήκη με %d έγγραφα.", "cta": "Δείτε τα πλάνα", "used": "Ήδη σε χρήση"},
    "tr": {"t": "Şablon sınırına ulaşıldı", "p": "Ücretsiz planınız %d belge şablonu içerir. Tam kütüphaneyi (%d belge) açmak için yükseltin.", "cta": "Planlara bak", "used": "Zaten kullanılan"},
    "it": {"t": "Limite di modelli raggiunto", "p": "Il piano gratuito include %d modelli. Passa a un piano a pagamento per sbloccare la libreria completa di %d documenti.", "cta": "Vedi i piani", "used": "Già usati"},
    "ru": {"t": "Достигнут лимит шаблонов", "p": "Бесплатный тариф включает %d шаблона. Перейдите на платный тариф, чтобы открыть всю библиотеку из %d документов.", "cta": "Смотреть тарифы", "used": "Уже использованы"},
    "es": {"t": "Límite de plantillas alcanzado", "p": "Su plan gratuito incluye %d plantillas. Actualice para desbloquear la biblioteca completa de %d documentos.", "cta": "Ver planes", "used": "Ya utilizadas"},
    "fr": {"t": "Limite de modèles atteinte", "p": "Votre formule gratuite inclut %d modèles. Passez à une formule payante pour débloquer la bibliothèque complète de %d documents.", "cta": "Voir les formules", "used": "Déjà utilisés"},
    "pl": {"t": "Osiągnięto limit szablonów", "p": "Plan bezpłatny obejmuje %d szablony. Przejdź na plan płatny, aby odblokować pełną bibliotekę %d dokumentów.", "cta": "Zobacz plany", "used": "Już użyte"},
    "uk": {"t": "Досягнуто ліміт шаблонів", "p": "Безкоштовний тариф включає %d шаблони. Перейдіть на платний тариф, щоб відкрити всю бібліотеку з %d документів.", "cta": "Переглянути тарифи", "used": "Вже використані"},
    "pt": {"t": "Limite de modelos atingido", "p": "O seu plano gratuito inclui %d modelos. Faça upgrade para desbloquear a biblioteca completa de %d documentos.", "cta": "Ver planos", "used": "Já utilizados"},
}

DOC_WATERMARK = {
    "en": "DEMO · free plan", "bg": "ДЕМО · безплатен план", "de": "DEMO · Gratis-Tarif",
    "ro": "DEMO · plan gratuit", "el": "DEMO · δωρεάν πλάνο", "tr": "DEMO · ücretsiz plan",
    "it": "DEMO · piano gratuito", "ru": "ДЕМО · бесплатный тариф", "es": "DEMO · plan gratuito",
    "fr": "DÉMO · formule gratuite", "pl": "DEMO · plan bezpłatny", "uk": "ДЕМО · безкоштовний тариф",
    "pt": "DEMO · plano gratuito",
}


def doc_entitlement(conn, uid, kind=None):
    """Returns (allowed, is_paid, used_kinds). `allowed` only meaningful with kind."""
    org = user_primary_org(conn, uid)
    if not org:
        return (False, False, [])
    paid = org_is_paid(org)
    used = [r["kind"] for r in conn.execute(
        "SELECT kind FROM doc_usage WHERE org_id=? ORDER BY id", (org["id"],)).fetchall()]
    if paid:
        return (True, True, used)
    if kind is None:
        return (False, False, used)
    if kind in used:
        return (True, False, used)
    return (len(used) < FREE_DOC_LIMIT, False, used)


@app.get("/api/docs/entitlement")
@login_required
def docs_entitlement():
    allowed, paid, used = doc_entitlement(g.conn, g.user["id"])
    return jsonify({"paid": paid, "limit": FREE_DOC_LIMIT, "used": used,
                    "remaining": max(0, FREE_DOC_LIMIT - len(used)) if not paid else None,
                    "total": len(DOC_KINDS)})


def _doc_locked_page(lang):
    L = DOC_LOCK_I18N.get(lang, DOC_LOCK_I18N["en"])
    inner = ('%s<h1>🔒 %s</h1><p style="font-size:14px;color:#5b6472">%s</p>'
             '<p style="margin-top:22px"><a href="/#/plans" style="background:#2f6df0;color:#fff;'
             'text-decoration:none;padding:11px 22px;border-radius:8px;font-weight:600">%s</a></p>'
             % (_doc_brandmark(lang), html_escape(L["t"]),
                html_escape(L["p"] % (FREE_DOC_LIMIT, len(DOC_KINDS))), html_escape(L["cta"])))
    return _doc_shell(lang, L["t"], inner), 402


@app.get("/docs/<kind>")
def doc_page(kind):
    # `/docs/invoice.pdf` is the same document as `/docs/invoice`, delivered as
    # a file. Handled here rather than as its own rule, because `<kind>` would
    # swallow the suffix anyway and two rules would only invite them to drift.
    want_pdf = kind.endswith(".pdf")
    if want_pdf:
        kind = kind[:-4]
    if kind not in DOC_KINDS:
        return "", 404
    if "uid" not in session:
        return "", 401
    if want_pdf:
        g._doc_pdf = True
    else:
        g._doc_pdf_base = "/docs/%s.pdf" % kind
    # The document's language is chosen when it is generated, not taken from
    # whoever is generating it: a Bulgarian company invoicing a German customer
    # sends a German invoice while working in Bulgarian itself.
    lang = _doc_lang(DOC_I18N)
    _conn = db.get_db()
    try:
        allowed, is_paid, _used = doc_entitlement(_conn, session["uid"], kind)
        if not allowed:
            return _doc_locked_page(lang)
        org = user_primary_org(_conn, session["uid"]) or {}
        if not is_paid:
            _conn.execute("INSERT OR IGNORE INTO doc_usage (org_id, kind) VALUES (?,?)", (org["id"], kind))
            _conn.commit()
        g._doc_free = not is_paid
        g._doc_country = org.get("country") or ""
    finally:
        _conn.close()
    q = request.args
    a, b = (q.get("a") or "-")[:120], (q.get("b") or "-")[:120]
    rega, regb = (q.get("rega") or "")[:40], (q.get("regb") or "")[:40]
    today = datetime.utcnow().strftime("%Y-%m-%d")

    if kind in ("invoice", "proforma_doc", "commercial_offer", "delivery_note"):
        IL = INV_I18N.get(lang, INV_I18N["en"])
        cfg = {
            "invoice": {"title": IL["inv"], "note": IL["vat"], "col": "money", "prefix": "INV-"},
            "proforma_doc": {"title": IL["pro"], "note": IL["vat"] + " " + IL["pro_note"], "col": "money", "prefix": "PF-"},
            "commercial_offer": {"title": IL["offer"], "note": IL["offer_note"], "col": "money", "prefix": "OFR-", "total": IL.get("total_offer", IL["total"])},
            "delivery_note": {"title": IL["delivery"], "note": IL["delivery_note_txt"], "col": "qty", "prefix": "DN-"},
        }[kind]
        is_qty = cfg["col"] == "qty"
        title = cfg["title"]
        number = (q.get("number") or "").strip()[:40] or (cfg["prefix"] + today.replace("-", ""))
        descs = [d[:200] for d in q.getlist("desc")][:30]
        amts = q.getlist("amount")[:30]
        qtys = q.getlist("qty")[:30]
        units = [u[:16] for u in q.getlist("unit")][:30]
        prices = q.getlist("price")[:30]

        def _num(seq, i, default=None):
            raw = seq[i] if i < len(seq) else ""
            if str(raw).strip() == "":
                return default
            try:
                return float(str(raw).replace(",", "."))
            except (TypeError, ValueError):
                return default

        # A line can be given two ways: quantity and unit price, or a bare
        # amount. The first is what an invoice needs; the second is what the
        # older form sent, and links already out in the world still send it.
        detailed = any(str(x).strip() for x in qtys) or any(str(x).strip() for x in prices)
        cols = 5 if (detailed and not is_qty) else (3 if detailed else 2)
        rows, total = "", 0.0
        for i, dsc in enumerate(descs):
            qty = _num(qtys, i)
            price = _num(prices, i)
            unit = units[i] if i < len(units) else ""
            if qty is not None and price is not None:
                val = round(qty * price, 2)
            else:
                val = _num(amts, i, 0.0)
            total += (qty if (is_qty and qty is not None) else val)
            cells = [html_escape(dsc or "-")]
            if cols >= 3:
                cells.append("<td class='r'>%s</td>" % (("%g" % qty) if qty is not None else "-"))
                cells.append("<td>%s</td>" % html_escape(unit or "-"))
            if cols == 5:
                cells.append("<td class='r'>%s</td>" % html_escape(_fmt_amount(price) or "-")
                             if price is not None else "<td class='r'>-</td>")
            if not (is_qty and cols >= 3):
                cells.append("<td class='r'>%s</td>" % (("%g" % (qty if qty is not None else val))
                                                        if is_qty else html_escape(_fmt_amount(val) or "-")))
            rows += "<tr><td>%s</td>%s</tr>" % (cells[0], "".join(cells[1:]))
        if not rows:
            rows = "<tr><td>-</td>%s</tr>" % ("<td class='r'>-</td>" * (cols - 1))
        # The header follows whichever shape the lines took, and each column
        # carries its own alignment: numbers right, words left. A right-aligned
        # "Unit" over left-aligned "m" and "h" reads as a mistake.
        heads = [(IL["desc"], False)]
        if cols >= 3:
            heads += [(IL["qty_col"], True), (IL["unit"], False)]
        if cols == 5:
            heads.append((IL["unit_price"], True))
        if not (is_qty and cols >= 3):
            heads.append((IL["qty"] if is_qty else IL["line_total"], True))
        colhead = IL["qty"] if is_qty else IL["amount"]
        vat_doc = kind in ("invoice", "proforma_doc")
        vata, vatb = (q.get("vata") or "").strip()[:24], (q.get("vatb") or "").strip()[:24]
        rc = (q.get("rc") or "").lower() in ("1", "true", "on", "yes")
        try:
            vatrate = max(0.0, min(30.0, float(str(q.get("vatrate") or 20).replace(",", "."))))
        except (TypeError, ValueError):
            vatrate = 20.0

        def tot_row(label, val, cls=""):
            # The label spans everything but the last column, so the totals line
            # up under the amounts however many columns the lines needed.
            return ("<tr class='%s'><td class='r' colspan='%d'>%s</td><td class='r'>%s</td></tr>"
                    % (cls, max(1, len(heads) - 1), html_escape(label), val))

        if is_qty:
            totals = tot_row(IL["total_items"], "%g" % total, "tot")
            note = cfg["note"]
        elif not vat_doc:
            totals = tot_row(cfg.get("total") or IL["total"], html_escape(_fmt_amount(total) or "-"), "tot")
            note = cfg["note"]
        else:
            vat_amt = 0.0 if rc else round(total * vatrate / 100.0, 2)
            gross = total + vat_amt
            vlabel = "%s (%g%%)" % (IL["vat_amount"], (0 if rc else vatrate))
            totals = (tot_row(IL["net"], html_escape(_fmt_amount(total) or "-"))
                      + tot_row(vlabel, html_escape(_fmt_amount(vat_amt) or "-"))
                      + tot_row(IL["gross"], html_escape(_fmt_amount(gross) or "-"), "tot"))
            base_note = IL["vat"] + ((" " + IL["pro_note"]) if kind == "proforma_doc" else "")
            note = IL["reverse_charge"] if rc else base_note

        def party_extra(reg, vat, addr):
            out = ('<div class="pr">%s %s</div>' % (IL["num"], html_escape(reg))) if reg else ""
            if vat:
                out += '<div class="pr">%s %s</div>' % (IL["vat_no"], html_escape(vat))
            if addr:
                out += '<div class="pr">%s</div>' % html_escape(addr)
            return out

        # Dates. The date of supply is a separate legal fact from the date the
        # invoice was written, and the two differ often enough that guessing is
        # wrong. Absent, it is simply not claimed.
        issue = (q.get("date") or "").strip()[:10] or today
        taxdate = (q.get("taxdate") or "").strip()[:10]
        due = (q.get("due") or "").strip()[:10]
        try:
            terms_days = int(str(q.get("terms") or "").strip() or 0)
        except ValueError:
            terms_days = 0
        if not due and terms_days > 0:
            try:
                due = (datetime.strptime(issue, "%Y-%m-%d")
                       + timedelta(days=min(terms_days, 365))).strftime("%Y-%m-%d")
            except ValueError:
                due = ""

        sub = "%s: %s" % (IL["date"], issue)
        if taxdate:
            sub += " · %s: %s" % (IL["tax_date"], taxdate)
        if due and vat_doc:
            sub += ' · <b>%s: %s</b>' % (html_escape(IL["due"]), html_escape(due))
        elif terms_days > 0:
            sub += " · %s: %d %s" % (IL["pay_terms"], terms_days, IL["days"])

        # Where to pay. An invoice without it is a request the customer cannot
        # act on without a phone call.
        pay_bits = []
        for label, key, limit in ((IL["iban"], "iban", 40), (IL["bic"], "bic", 16),
                                  (IL["bank"], "bank", 80), (IL["pay_ref"], "payref", 80)):
            val = (q.get(key) or "").strip()[:limit]
            if val:
                pay_bits.append("<div><span>%s</span> <b>%s</b></div>"
                                % (html_escape(label), html_escape(val)))
        pay_block = ('<div class="paybox"><div class="pbt">%s</div>%s</div>'
                     % (html_escape(IL["pay_title"]), "".join(pay_bits))) if pay_bits else ""

        # Who drew it up belongs on the signature line, printed above where
        # they sign - not repeated underneath, which is how it read at first.
        compiled = (q.get("by") or "").strip()[:80]
        compiled_name = ('<span class="cmp">%s</span>' % html_escape(compiled)) if compiled else ""

        sig = IL["sig_line"]
        inner = ("""%(brand)s
<h1>%(title)s <span style="color:#2f6df0">%(number)s</span></h1>
<div class="doc-sub">%(sub)s</div>
<div class="parties">
  <div class="party"><div class="pl">%(seller)s</div><div class="pn">%(pa)s</div>%(regA)s</div>
  <div class="party"><div class="pl">%(buyer)s</div><div class="pn">%(pb)s</div>%(regB)s</div>
</div>
<table><thead><tr>%(heads)s</tr></thead><tbody>%(rows)s
%(totals)s</tbody></table>
%(pay)s
<p class="disc">%(note)s</p>
<div class="sigs"><div class="sig"><b>%(issued)s</b>%(compiled)s%(sig)s</div><div class="sig"><b>%(recv)s</b>%(sig)s</div></div>""") % {
            "brand": _doc_brandmark(lang), "title": title, "number": html_escape(number),
            "sub": sub, "seller": IL["seller"], "buyer": IL["buyer"],
            "pa": html_escape(a), "pb": html_escape(b),
            "regA": party_extra(rega, vata, (q.get("adra") or "").strip()[:160]),
            "regB": party_extra(regb, vatb, (q.get("adrb") or "").strip()[:160]),
            "heads": "".join("<th%s>%s</th>" % (' class="r"' if right else "", html_escape(h))
                             for h, right in heads),
            "rows": rows, "totals": totals, "note": note, "pay": pay_block,
            "issued": IL["issued"], "recv": IL["recv"], "sig": sig,
            "compiled": compiled_name,
        }
        return _doc_out(lang, title, inner, number=number)

    L = DOC_I18N[lang]
    D = L[kind]
    subject = (q.get("subject") or "-")[:500]
    notes = (q.get("notes") or "-")[:500]
    city = (q.get("city") or "")[:80]
    amount_raw = (q.get("amount") or "").strip()
    amount = (_fmt_amount(amount_raw) or html_escape(amount_raw)) if amount_raw else "-"
    pa = html_escape(a) + ((" · %s %s" % (L["reg"], html_escape(rega))) if rega else "")
    pb = html_escape(b) + ((" · %s %s" % (L["reg"], html_escape(regb))) if regb else "")
    clauses = "".join("<li>%s</li>" % (str(html_escape(c))
                      .replace("{subject}", "<b>%s</b>" % html_escape(subject))
                      .replace("{notes}", str(html_escape(notes)))
                      .replace("{amount}", "<b>%s</b>" % amount))
                      for c in D["c"])
    inner = ("""%(brand)s
<h1>%(t)s</h1>
<div class="meta">%(between)s <b>%(pa)s</b> %(and)s <b>%(pb)s</b><br>%(datel)s: %(today)s%(cityline)s</div>
<ol>%(clauses)s</ol>
<div class="sigs"><div class="sig"><b>%(pas)s</b>%(sig)s</div><div class="sig"><b>%(pbs)s</b>%(sig)s</div></div>
<p class="disc">%(disc)s</p>""") % {
        "brand": _doc_brandmark(lang), "t": D["t"], "between": L["between"], "and": L["and"],
        "pa": pa, "pb": pb, "pas": html_escape(a), "pbs": html_escape(b),
        "datel": L["generated"], "today": today,
        "cityline": (" · %s: %s" % (L["city"], html_escape(city))) if city else "",
        "clauses": clauses, "sig": L["sig"], "disc": L["disclaimer"],
    }
    return _doc_out(lang, D["t"], inner)


# --------------------------------------------------------------------------- #
#  Inbound integration: API keys, webhook endpoint, hosted order form
# --------------------------------------------------------------------------- #
@app.get("/api/integration/keys")
@login_required
def integration_keys():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    rows = conn.execute("SELECT * FROM api_keys WHERE org_id=? ORDER BY id DESC", (org["id"],)).fetchall()
    # A secret key is not here to be returned - only its hash is stored, so one
    # that was not written down is replaced rather than recovered. A public key
    # is returned, because it belongs in a URL.
    return jsonify([{"id": r["id"], "label": r["label"] or "", "kind": r["kind"],
                     "key": r["key"] if r["kind"] == "public" else None,
                     "revoked": bool(r["revoked"]), "last_used_at": r["last_used_at"],
                     "created_at": r["created_at"]}
                    for r in rows])


@app.post("/api/integration/keys")
@login_required
def integration_create_key():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    if org["kind"] != "business":
        raise ApiError("Само бизнес акаунт може да създава API ключове", 403, code="only_business_creates")
    require_verified(conn, user["id"])
    d = body()
    kind = d.get("kind") if d.get("kind") in ("secret", "public") else "secret"
    key = ("sk_seam_" if kind == "secret" else "pk_seam_") + secrets.token_urlsafe(18)
    stored = _api_key_stored(key, kind)
    conn.execute("INSERT INTO api_keys (org_id, key, label, kind) VALUES (?,?,?,?)",
                 (org["id"], stored, (d.get("label") or "").strip()[:60], kind))
    r = conn.execute("SELECT * FROM api_keys WHERE key=?", (stored,)).fetchone()
    return jsonify({"id": r["id"], "key": key, "label": r["label"] or "",
                    "kind": r["kind"], "revoked": False})


def _api_key_stored(key, kind):
    """A secret key is handed over once and only its hash is kept, so it cannot
    be read back out of the database or a backup of it.

    A public key is the opposite: it is meant to sit in a URL on the company's
    own website, where it authorises nothing but placing an inbound order.
    Hashing it would break the hosted form for no gain, so it is stored as it
    is - and it is not a secret, which is why it is named `pk_`.
    """
    return db.token_hash(key) if kind == "secret" else key


@app.delete("/api/integration/keys/<int:kid>")
@login_required
def integration_revoke_key(kid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    r = conn.execute("SELECT id FROM api_keys WHERE id=? AND org_id=?", (kid, org["id"])).fetchone()
    if not r:
        raise ApiError("Не е намерено", 404, code="not_found")
    conn.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (kid,))
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
#  inv.bg - Bulgarian invoicing service (real REST API v3)
#
#  Auth: long-lived company token from inv.bg → Настройки → За Фирмата →
#  API достъп, sent as "Authorization: Bearer <token>".
#  Docs: https://api.inv.bg/v3/docs/  ·  Base: https://api.inv.bg/v3
# --------------------------------------------------------------------------- #
INVBG_BASE = "https://api.inv.bg/v3"
INVBG_TOKEN_FILE = os.path.join(BASE, ".invbg_token")
# Document types accepted by inv.bg: dan = tax invoice, prof = proforma,
# deb = debit note, cred = credit note.
INVBG_TYPES = ("dan", "prof", "deb", "cred")
#: A value sent to a Bulgarian API, not a label Seam shows anyone: inv.bg
#: expects the unit of measure in Bulgarian, and "бр." is its "pcs".
INVBG_DEFAULT_UNIT = "бр."


def invbg_token():
    t = os.environ.get("SEAM_INVBG_TOKEN")
    if t:
        return t.strip()
    if os.path.exists(INVBG_TOKEN_FILE):
        with open(INVBG_TOKEN_FILE, "r") as f:
            return f.read().strip()
    return ""


def invbg_request(method, path, payload=None, raw=False):
    token = invbg_token()
    if not token:
        raise ApiError("inv.bg не е свързан", 400, code="invbg_no_token")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(INVBG_BASE + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept-Language": "bg",
        "User-Agent": "Seam/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return body if raw else (json.loads(body.decode("utf-8")) if body else {})


@app.get("/api/invbg/status")
@login_required
def invbg_status():
    return jsonify({"enabled": bool(invbg_token()), "base": INVBG_BASE, "types": list(INVBG_TYPES)})


@app.post("/api/invbg/invoice")
@login_required
def invbg_create_invoice():
    """Push a Seam store order to inv.bg as a real invoice / proforma."""
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    d = body()
    oid = require(d, "order_id")
    o = conn.execute("SELECT * FROM store_orders WHERE id=? AND org_id=?", (oid, org["id"])).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="not_found")
    p = conn.execute("SELECT * FROM products WHERE id=?", (o["product_id"],)).fetchone()
    dtype = d.get("type") if d.get("type") in INVBG_TYPES else "dan"

    unit_price = _price_eur(p["price"] if p else "")
    payload = {
        "type": dtype,
        "to_name": (d.get("to_name") or dl(o["customer"] or "") or "-")[:200],
        "to_address": (d.get("to_address") or "-")[:200],
        "to_mol": (d.get("to_mol") or "-")[:100],
        "to_bulstat": (d.get("to_bulstat") or "")[:20],
        "to_egn": (d.get("to_egn") or "")[:20],
        "to_is_reg_vat": bool(d.get("to_is_reg_vat")),
        "to_vat_number": (d.get("to_vat_number") or "")[:24],
        "notes": ("Seam %s" % o["ref"])[:200],
        "items": [{
            "name": (dl(p["name"]) if p else "-")[:200],
            "price": unit_price,
            "quantity": o["qty"] or 1,
            "quantity_unit": (d.get("quantity_unit") or INVBG_DEFAULT_UNIT),
        }],
    }
    if d.get("vat_percent") is not None:
        try:
            payload["items"][0]["vat_percent"] = float(d.get("vat_percent"))
        except (TypeError, ValueError):
            pass

    try:
        res = invbg_request("POST", "/invoices", payload)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise ApiError("inv.bg отказа заявката (%s): %s" % (e.code, detail), 502, code="invbg_error")
    except Exception as e:
        raise ApiError("inv.bg е недостъпен: %s" % str(e)[:160], 502, code="invbg_error")

    inv_id, number = res.get("id"), res.get("number")
    log_ref = "%s → inv.bg %s" % (o["ref"], number or inv_id)
    for uid in org_user_ids(conn, org["id"]):
        conn.execute("INSERT INTO notifications (user_id, kind, body, meta_json) VALUES (?, 'invbg_invoice', 'invbg_invoice', ?)",
                     (uid, json.dumps({"ref": o["ref"], "number": number or str(inv_id)}, ensure_ascii=False)))
    dispatch_webhooks(conn, org["id"], "store.incoming", {"ref": o["ref"], "invbg": log_ref})
    return jsonify({"id": inv_id, "number": number, "pdf": "/api/invbg/invoice/%s/pdf" % inv_id})


@app.get("/api/invbg/invoice/<int:inv_id>/pdf")
@login_required
def invbg_invoice_pdf(inv_id):
    try:
        pdf = invbg_request("GET", "/invoices/%d/pdf" % inv_id, raw=True)
    except urllib.error.HTTPError as e:
        raise ApiError("inv.bg: не може да върне PDF (%s)" % e.code, 502, code="invbg_error")
    except Exception as e:
        raise ApiError("inv.bg е недостъпен: %s" % str(e)[:160], 502, code="invbg_error")
    return app.response_class(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": 'inline; filename="invbg-%d.pdf"' % inv_id})


def _webhooks_payload(conn, org_id):
    """Shared helper - see _ws_messages_payload for why this is not a view call."""
    rows = conn.execute("SELECT * FROM webhooks WHERE org_id=? ORDER BY id DESC", (org_id,)).fetchall()
    return {"events": list(WEBHOOK_EVENTS), "hooks": [{
        "id": r["id"], "url": r["url"], "label": r["label"] or "", "secret": r["secret"],
        "events": json.loads(r["events_json"] or '["*"]'), "active": bool(r["active"]),
        "last_status": r["last_status"], "last_at": r["last_at"]} for r in rows]}


# --------------------------------------------------------------------------- #
#  Payouts (roadmap item 5: the freelancer / subcontractor side)
#
#  Seam moves no money. It records what is owed for which work, who marked it
#  paid and when, and keeps that next to the order it belongs to - so the
#  freelancer stops having to ask "was that one paid?" in a chat thread.
# --------------------------------------------------------------------------- #
PAYOUT_METHODS = ("bank", "card", "cash", "paypal", "revolut", "wise", "other")
PAYOUT_STATUSES = ("due", "paid", "cancelled")


def _payout_row(conn, r, my_orgs):
    payer = conn.execute("SELECT name FROM orgs WHERE id=?", (r["payer_org_id"],)).fetchone()
    payee = conn.execute("SELECT name FROM orgs WHERE id=?", (r["payee_org_id"],)).fetchone()
    order = conn.execute("SELECT ref, title FROM orders WHERE id=?", (r["order_id"],)).fetchone() \
        if r["order_id"] else None
    return {
        "id": r["id"], "amount": r["amount"], "description": dl(r["description"] or ""),
        "method": r["method"], "reference": r["reference"] or "", "status": r["status"],
        "due_date": r["due_date"] or "", "note": r["note"] or "",
        "paid_at": r["paid_at"], "created_at": r["created_at"],
        "order_id": r["order_id"], "order_ref": order["ref"] if order else "",
        "payer": dl(payer["name"]) if payer else "", "payee": dl(payee["name"]) if payee else "",
        "i_pay": r["payer_org_id"] in my_orgs,
    }


def _payouts_payload(conn, ws_id, my_orgs):
    rows = conn.execute("SELECT * FROM payouts WHERE workspace_id=? ORDER BY id DESC",
                        (ws_id,)).fetchall()
    out = [_payout_row(conn, r, my_orgs) for r in rows]
    due = sum(_price_eur(p["amount"]) for p in out if p["status"] == "due")
    paid = sum(_price_eur(p["amount"]) for p in out if p["status"] == "paid")
    return {"payouts": out, "methods": list(PAYOUT_METHODS),
            "totals": {"due": "EUR %.2f" % due, "paid": "EUR %.2f" % paid}}


@app.get("/api/workspaces/<int:ws_id>/payouts")
@login_required
def payouts_list(ws_id):
    conn, user = g.conn, g.user
    workspace_access(conn, ws_id, user["id"])
    return jsonify(_payouts_payload(conn, ws_id, set(user_org_ids(conn, user["id"]))))


@app.post("/api/workspaces/<int:ws_id>/payouts")
@login_required
def payouts_create(ws_id):
    """Only the paying side records a payout - the payee cannot invent one."""
    conn, user = g.conn, g.user
    ws, side = workspace_access(conn, ws_id, user["id"])
    my_org = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    other = other_org_id(ws, side)
    if not other:
        raise ApiError("Няма партньор в това пространство", 400, code="no_partner")
    d = body()
    amount = (d.get("amount") or "").strip()[:40]
    if _price_eur(amount) <= 0:
        raise ApiError("Въведете сума", 400, code="field_required", info={"field": "amount"})
    # A prefix already typed is respected, including BGN on a figure carried
    # over from before the euro. Nothing here offers the lev; it is only read.
    if not amount.upper().startswith(("EUR", "BGN", "USD", "GBP")):
        amount = "EUR " + amount
    method = d.get("method") if d.get("method") in PAYOUT_METHODS else "bank"
    order_id = d.get("order_id")
    if order_id:
        o = conn.execute("SELECT id FROM orders WHERE id=? AND workspace_id=?",
                         (order_id, ws_id)).fetchone()
        if not o:
            order_id = None
    conn.execute(
        "INSERT INTO payouts (workspace_id, order_id, payer_org_id, payee_org_id, amount, "
        "description, method, reference, due_date, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ws_id, order_id, my_org, other, amount,
         (d.get("description") or "").strip()[:200], method,
         (d.get("reference") or "").strip()[:80], (d.get("due_date") or "").strip()[:20],
         user["id"]))
    log_event(conn, ws_id, order_id, user, side, "payout_recorded",
              "Записа плащане %s" % amount, {"amount": amount, "method": method})
    notify_org(conn, other, ws_id, order_id, "payout_recorded",
               "Ново плащане: %s" % amount, meta={"amount": amount})
    conn.commit()
    return jsonify(_payouts_payload(conn, ws_id, set(user_org_ids(conn, user["id"]))))


@app.patch("/api/payouts/<int:pid>")
@login_required
def payouts_update(pid):
    conn, user = g.conn, g.user
    r = conn.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
    if not r:
        raise ApiError("Не е намерено", 404, code="not_found")
    ws, side = workspace_access(conn, r["workspace_id"], user["id"])
    my_orgs = set(user_org_ids(conn, user["id"]))
    if r["payer_org_id"] not in my_orgs:
        raise ApiError("Само платецът може да променя това", 403, code="payout_not_payer")
    status = (body().get("status") or "").strip()
    if status not in PAYOUT_STATUSES:
        raise ApiError("Непознат статус", 400, code="payout_status")
    if r["status"] == status:
        return jsonify(_payouts_payload(conn, r["workspace_id"], my_orgs))
    if status == "paid":
        conn.execute("UPDATE payouts SET status='paid', paid_by=?, paid_at=datetime('now'), "
                     "note=? WHERE id=?",
                     (user["id"], (body().get("note") or "").strip()[:300], pid))
    else:
        conn.execute("UPDATE payouts SET status=?, paid_by=NULL, paid_at=NULL WHERE id=?",
                     (status, pid))
    log_event(conn, r["workspace_id"], r["order_id"], user, side, "payout_recorded",
              "Плащане %s: %s" % (r["amount"], status), {"amount": r["amount"], "status": status})
    notify_org(conn, r["payee_org_id"], r["workspace_id"], r["order_id"], "payout_recorded",
               "Плащане %s · %s" % (r["amount"], status),
               exclude_user=user["id"], meta={"amount": r["amount"], "status": status})
    conn.commit()
    return jsonify(_payouts_payload(conn, r["workspace_id"], my_orgs))


@app.delete("/api/payouts/<int:pid>")
@login_required
def payouts_delete(pid):
    conn, user = g.conn, g.user
    r = conn.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
    if not r:
        raise ApiError("Не е намерено", 404, code="not_found")
    workspace_access(conn, r["workspace_id"], user["id"])
    my_orgs = set(user_org_ids(conn, user["id"]))
    if r["payer_org_id"] not in my_orgs:
        raise ApiError("Само платецът може да променя това", 403, code="payout_not_payer")
    if r["status"] == "paid":
        raise ApiError("Платена сума не се изтрива - отбележете я като отменена", 400,
                       code="payout_paid")
    conn.execute("DELETE FROM payouts WHERE id=?", (pid,))
    conn.commit()
    return jsonify(_payouts_payload(conn, r["workspace_id"], my_orgs))


# --------------------------------------------------------------------------- #
#  Multi-level approvals (roadmap item 9)
#
#  A chain is an ordered list of steps, each naming who may decide it. Steps
#  unlock one at a time: step 2 cannot be decided while step 1 is pending, so
#  the record shows not just who approved but in what order they were able to.
#  A rejection stops the chain; the remaining steps are marked skipped rather
#  than deleted, so the history stays complete.
# --------------------------------------------------------------------------- #
MAX_CHAIN_STEPS = 6


def _chain_steps(raw):
    try:
        steps = json.loads(raw or "[]")
    except Exception:
        steps = []
    return [s for s in steps if isinstance(s, dict) and s.get("label")][:MAX_CHAIN_STEPS]


def _order_agreed_value(conn, o):
    spec = {s["key"]: s for s in (tpl_get(conn, o["template"]) or {}).get("terms", [])}
    total = 0.0
    for t_ in conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=?",
                           (o["id"],)).fetchall():
        if t_["state"] == "agreed" and (spec.get(t_["key"]) or {}).get("type") == "money":
            total += _price_eur(t_["value"])
    return total


def _approvals_payload(conn, order_id, org_id, user_id):
    # Ordered by chain first: two chains would otherwise interleave their steps
    # and "which step is next" would be meaningless.
    rows = conn.execute(
        "SELECT a.*, u.name AS approver_name, d.name AS decided_name FROM approvals a "
        "LEFT JOIN users u ON u.id=a.approver_user_id LEFT JOIN users d ON d.id=a.decided_by "
        "WHERE a.order_id=? AND a.org_id=? ORDER BY a.chain_id, a.step",
        (order_id, org_id)).fetchall()
    # A record can carry more than one chain over its life. The card reports the
    # latest one; earlier ones stay available as history rather than being mixed
    # into it, so "which step is next" always has a single answer.
    latest_chain = rows[-1]["chain_id"] if rows else None
    history = [r for r in rows if r["chain_id"] != latest_chain]
    rows = [r for r in rows if r["chain_id"] == latest_chain]

    out, current = [], None
    for r in rows:
        if current is None and r["status"] == "pending":
            current = r["step"]
        out.append({
            "id": r["id"], "step": r["step"], "label": r["label"], "status": r["status"],
            "approver_user_id": r["approver_user_id"], "approver_name": r["approver_name"] or "",
            "approver_role": r["approver_role"] or "", "note": r["note"] or "",
            "decided_by": r["decided_name"] or "", "decided_at": r["decided_at"],
            "ip": r["ip"] or "", "device": r["device"] or "",
        })
    for a in out:
        # Only the step that is next in line, and only its named approver
        # (or anyone, if the step names no one), may decide.
        a["is_current"] = (a["step"] == current)
        a["can_decide"] = bool(a["is_current"] and a["status"] == "pending" and
                               (not a["approver_user_id"] or a["approver_user_id"] == user_id))
    done = [a for a in out if a["status"] == "approved"]
    rejected = [a for a in out if a["status"] == "rejected"]
    return {
        "approvals": out,
        "state": ("rejected" if rejected else ("approved" if out and len(done) == len(out) else
                  ("pending" if out else "none"))),
        "current_step": current,
        "progress": {"done": len(done), "total": len(out)},
        "history": [{"label": r["label"], "status": r["status"], "decided_at": r["decided_at"]}
                    for r in history],
    }


def _has_open_chain(conn, order_id, org_id):
    return bool(conn.execute(
        "SELECT 1 FROM approvals WHERE order_id=? AND org_id=? AND status='pending' LIMIT 1",
        (order_id, org_id)).fetchone())


def _start_chain(conn, o, org_id, chain):
    """Materialise a chain's steps as approval rows for this order. Only one
    chain runs at a time - otherwise "which step is next" has no answer."""
    steps = _chain_steps(chain["steps_json"])
    if not steps:
        return 0
    if _has_open_chain(conn, o["id"], org_id):
        raise ApiError("Върху този запис вече тече одобрение", 400, code="appr_running")
    n = 0
    for i, s in enumerate(steps, start=1):
        uid = s.get("user_id")
        if uid:
            member = conn.execute("SELECT 1 FROM memberships WHERE user_id=? AND org_id=?",
                                  (uid, org_id)).fetchone()
            if not member:
                uid = None      # a named approver who left the org is not enforced
        try:
            conn.execute(
                "INSERT INTO approvals (order_id, chain_id, org_id, step, label, approver_user_id, "
                "approver_role) VALUES (?,?,?,?,?,?,?)",
                (o["id"], chain["id"], org_id, i, str(s["label"])[:80], uid,
                 str(s.get("role") or "")[:60]))
            n += 1
        except sqlite3.IntegrityError:
            pass                # chain already started on this order
    return n


def _auto_start_chains(conn, o, org_id):
    """Apply every active chain of `org_id` that matches this order."""
    if _has_open_chain(conn, o["id"], org_id):
        return 0
    rows = conn.execute("SELECT * FROM approval_chains WHERE org_id=? AND active=1 ORDER BY id",
                        (org_id,)).fetchall()
    value = _order_agreed_value(conn, o)
    for ch in rows:
        if ch["template"] and ch["template"] != o["template"]:
            continue
        if value < (ch["threshold"] or 0):
            continue
        return _start_chain(conn, o, org_id, ch)   # the first match wins
    return 0


def _chains_payload(conn, org_id):
    """Takes the caller's connection - never call a decorated view directly, or
    the second connection cannot see this transaction's writes."""
    rows = conn.execute("SELECT * FROM approval_chains WHERE org_id=? ORDER BY id DESC",
                        (org_id,)).fetchall()
    members = conn.execute(
        "SELECT u.id, u.name, u.email FROM users u JOIN memberships m ON m.user_id=u.id "
        "WHERE m.org_id=? ORDER BY u.name", (org_id,)).fetchall()
    return {
        "chains": [{"id": r["id"], "name": r["name"], "template": r["template"],
                    "threshold": r["threshold"], "active": bool(r["active"]),
                    "steps": _chain_steps(r["steps_json"])} for r in rows],
        "members": [{"id": m["id"], "name": m["name"], "email": m["email"]} for m in members],
        "max_steps": MAX_CHAIN_STEPS,
    }


@app.get("/api/approval-chains")
@login_required
def chains_list():
    conn, user = g.conn, g.user
    return jsonify(_chains_payload(conn, store_org(conn, user)["id"]))


@app.post("/api/approval-chains")
@login_required
def chains_create():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    d = body()
    name = (d.get("name") or "").strip()[:80]
    if not name:
        raise ApiError("Дайте име на веригата", 400, code="field_required", info={"field": "name"})
    steps = []
    member_ids = {m["user_id"] for m in conn.execute(
        "SELECT user_id FROM memberships WHERE org_id=?", (org["id"],)).fetchall()}
    for s in (d.get("steps") or [])[:MAX_CHAIN_STEPS]:
        label = (s.get("label") or "").strip()[:80]
        if not label:
            continue
        uid = s.get("user_id")
        steps.append({"label": label, "role": (s.get("role") or "").strip()[:60],
                      "user_id": uid if uid in member_ids else None})
    if not steps:
        raise ApiError("Добавете поне една стъпка", 400, code="chain_no_steps")
    try:
        threshold = float(d.get("threshold") or 0)
    except (TypeError, ValueError):
        threshold = 0.0
    conn.execute(
        "INSERT INTO approval_chains (org_id, name, template, threshold, steps_json, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (org["id"], name, (d.get("template") or "").strip() or None, max(0.0, threshold),
         json.dumps(steps, ensure_ascii=False), user["id"]))
    conn.commit()
    return jsonify(_chains_payload(conn, org["id"]))


@app.patch("/api/approval-chains/<int:cid>")
@login_required
def chains_toggle(cid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    conn.execute("UPDATE approval_chains SET active=? WHERE id=? AND org_id=?",
                 (1 if body().get("active") else 0, cid, org["id"]))
    conn.commit()
    return jsonify(_chains_payload(conn, org["id"]))


@app.delete("/api/approval-chains/<int:cid>")
@login_required
def chains_delete(cid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    conn.execute("DELETE FROM approval_chains WHERE id=? AND org_id=?", (cid, org["id"]))
    conn.commit()
    return jsonify(_chains_payload(conn, org["id"]))


@app.get("/api/orders/<int:oid>/approvals")
@login_required
def approvals_get(oid):
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    org_id = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    return jsonify(_approvals_payload(conn, oid, org_id, user["id"]))


@app.post("/api/orders/<int:oid>/approvals/start")
@login_required
def approvals_start(oid):
    """Attach an approval chain to this order - a specific one, or every chain
    whose template and threshold match."""
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    org_id = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    cid = body().get("chain_id")
    if cid:
        ch = conn.execute("SELECT * FROM approval_chains WHERE id=? AND org_id=?",
                          (cid, org_id)).fetchone()
        if not ch:
            raise ApiError("Веригата не е намерена", 404, code="not_found")
        started = _start_chain(conn, o, org_id, ch)
    else:
        started = _auto_start_chains(conn, o, org_id)
    if started:
        log_event(conn, ws["id"], oid, user, side, "approval_started",
                  "Стартира одобрение (%d стъпки)" % started, {"steps": started})
    conn.commit()
    return jsonify(_approvals_payload(conn, oid, org_id, user["id"]))


@app.post("/api/approvals/<int:aid>/decide")
@login_required
def approvals_decide(aid):
    conn, user = g.conn, g.user
    row = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    if not row:
        raise ApiError("Не е намерено", 404, code="not_found")
    o = conn.execute("SELECT * FROM orders WHERE id=?", (row["order_id"],)).fetchone()
    ws, side = workspace_access(conn, o["workspace_id"], user["id"])
    org_id = ws["buyer_org_id"] if side == "buyer" else ws["partner_org_id"]
    if row["org_id"] != org_id:
        raise ApiError("Нямате достъп", 403, code="forbidden")
    if row["status"] != "pending":
        raise ApiError("Стъпката вече е решена", 400, code="appr_decided")

    state = _approvals_payload(conn, row["order_id"], org_id, user["id"])
    if state["current_step"] != row["step"]:
        raise ApiError("Предходна стъпка още не е решена", 400, code="appr_out_of_order")
    if row["approver_user_id"] and row["approver_user_id"] != user["id"]:
        raise ApiError("Тази стъпка се решава от друг", 403, code="appr_not_yours")

    decision = (body().get("decision") or "").strip()
    if decision not in ("approved", "rejected"):
        raise ApiError("Изберете одобрение или отказ", 400, code="appr_decision")
    note = (body().get("note") or "").strip()[:500]
    ip, device = _client_ip(), _device_label()
    conn.execute(
        "UPDATE approvals SET status=?, note=?, decided_by=?, decided_at=datetime('now'), "
        "ip=?, device=? WHERE id=?", (decision, note, user["id"], ip, device, aid))
    if decision == "rejected":
        # A rejection ends the chain; later steps never became decidable.
        conn.execute("UPDATE approvals SET status='skipped' WHERE order_id=? AND chain_id=? "
                     "AND step>? AND status='pending'",
                     (row["order_id"], row["chain_id"], row["step"]))
    log_event(conn, ws["id"], row["order_id"], user, side, "approval_decided",
              "%s стъпка „%s“" % ("Одобри" if decision == "approved" else "Отказа", row["label"]),
              {"step": row["step"], "label": row["label"], "decision": decision, "note": note})
    notify_org(conn, org_id, ws["id"], row["order_id"], "approval_decided",
               "%s · %s: %s" % (o["ref"], row["label"], decision),
               exclude_user=user["id"],
               meta={"ref": o["ref"], "step": row["step"], "decision": decision})
    conn.commit()
    return jsonify(_approvals_payload(conn, row["order_id"], org_id, user["id"]))


def _automations_payload(conn, org_id):
    rows = conn.execute("SELECT * FROM automations WHERE org_id=? ORDER BY id DESC", (org_id,)).fetchall()
    out = []
    for r in rows:
        try:
            conds, acts = json.loads(r["conditions_json"] or "[]"), json.loads(r["actions_json"] or "[]")
        except Exception:
            conds, acts = [], []
        last = conn.execute(
            "SELECT status, detail, created_at FROM automation_runs WHERE automation_id=? "
            "ORDER BY id DESC LIMIT 5", (r["id"],)).fetchall()
        out.append({
            "id": r["id"], "name": r["name"], "trigger": r["trigger"], "active": bool(r["active"]),
            "conditions": conds, "actions": acts, "runs": r["runs"],
            "last_run": r["last_run"], "last_status": r["last_status"],
            "recent": [{"status": x["status"], "detail": x["detail"], "at": x["created_at"]} for x in last],
        })
    return {"automations": out, "triggers": list(AUTOMATION_TRIGGERS),
            "actions": list(AUTOMATION_ACTIONS), "ops": list(AUTOMATION_OPS),
            "docs": list(DOC_KINDS), "limit": MAX_AUTOMATIONS}


def _clean_rule(d):
    name = (d.get("name") or "").strip()[:80]
    trigger = (d.get("trigger") or "").strip()
    if not name:
        raise ApiError("Дайте име на правилото", 400, code="field_required", info={"field": "name"})
    if trigger not in AUTOMATION_TRIGGERS:
        raise ApiError("Непознат тригер", 400, code="auto_trigger")
    conds = []
    for c in (d.get("conditions") or [])[:5]:
        f = (c.get("field") or "").strip()[:60]
        if not f:
            continue
        conds.append({"field": f, "op": c.get("op") if c.get("op") in AUTOMATION_OPS else "eq",
                      "value": str(c.get("value") or "")[:200]})
    acts = []
    for a in (d.get("actions") or [])[:6]:
        if a.get("type") not in AUTOMATION_ACTIONS:
            continue
        act = {"type": a["type"]}
        for k in ("text", "status", "level", "url", "email", "secret", "doc"):
            if a.get(k):
                act[k] = str(a[k])[:500]
        acts.append(act)
    if not acts:
        raise ApiError("Добавете поне едно действие", 400, code="auto_no_action")
    return name, trigger, conds, acts


# --------------------------------------------------------------------------- #
#  The team inside one company
#
#  Until now a company was one login, which no real company is. Colleagues are
#  added here with a role; they set their own password through the same
#  single-use link the password reset uses, so no one ever types a password for
#  someone else.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  Verifiable trade reference
#
#  Every B2B directory shows what a company wrote about itself. Seam can show
#  what a company actually did, because the audit trail already recorded it: how
#  many records were carried to completion, how quickly terms were agreed, how
#  often a dated commitment was met.
#
#  Three rules hold this honest:
#    - only the company's OWN aggregate is published; nothing that would expose
#      a counterparty's identity, prices or performance;
#    - the figures are signed with this instance's key, so a reader can tell
#      they were not retyped by hand;
#    - the link is revocable and carries the date it was produced, because a
#      reference from two years ago is not evidence about today.
# --------------------------------------------------------------------------- #
REF_KEY_FILE = os.path.join(BASE, ".ref_keys")

#: What a company may publish. Everything is off unless it opts in, except the
#: plain facts of activity - and money is banded rather than exact, because an
#: exact figure is competitive information the reader has no need for.
REF_FIELDS = ("since", "counterparties", "records", "completion", "agreement_speed",
              "on_time", "signatures", "documents", "value_band",
              # How fast this company settles what it is invoiced. Off by
              # default like every other figure that could embarrass: publishing
              # it is a decision, and a company that pays slowly should not have
              # that decided for it by a default.
              "pays_in_days", "pays_on_time")
REF_DEFAULT = {"since": True, "counterparties": True, "records": True, "completion": True,
               "agreement_speed": True, "on_time": True, "signatures": True,
               "documents": False, "value_band": False,
               "pays_in_days": False, "pays_on_time": False}

REF_I18N = {
    "en": {"reg_label": "Company number", "title": "Trade reference", "sub": "Computed from this company's own operating record in Seam.",
           "gone": "Reference unavailable",
           "gone_p": "The link has been revoked by the company or is no longer current. Ask the company for a new link.",
           "country": "Country", "as_of": "As of", "verified": "verified", "days": "days",
           "explain": "These figures are computed from the platform's audit trail, not typed in by hand. No counterparty, agreed price or term is disclosed.",
           "verify_t": "Verification", "verify_p": "The figures are signed with this Seam instance's key. The signed record and the public key are available at",
           "privacy": "Only the company's own aggregate is published. No counterparty data is shared.",
           "f_since": "Active on the platform since", "f_counterparties": "Counterparties",
           "f_records": "Records opened", "f_completion": "Records carried to completion",
           "f_speed": "Average time to agreement", "f_on_time": "Dated commitments met",
           "f_signatures": "Documents signed", "f_documents": "Files attached",
           "f_value": "Volume through the platform", "f_pays_in_days": "Pays within", "f_pays_on_time": "Pays on time"},
    "bg": {"reg_label": "Фирмен номер", "title": "Търговска референция", "sub": "Изчислена от собствения операционен запис на дружеството в Seam.",
           "gone": "Референцията не е достъпна",
           "gone_p": "Връзката е оттеглена от дружеството или вече не е актуална. Поискайте нова връзка от самото дружество.",
           "country": "Държава", "as_of": "Към дата", "verified": "проверено", "days": "дни",
           "explain": "Стойностите са изчислени от одитната следа на платформата, а не са въведени на ръка. Не се разкриват контрагенти, договорени цени или условия.",
           "verify_t": "Проверка", "verify_p": "Стойностите са подписани с ключа на тази инсталация на Seam. Подписаният запис и публичният ключ са достъпни на",
           "privacy": "Публикува се само собственият сборен запис на дружеството. Данни за контрагенти не се споделят.",
           "f_since": "Активно в платформата от", "f_counterparties": "Брой контрагенти",
           "f_records": "Заведени преписки", "f_completion": "Дял на завършените преписки",
           "f_speed": "Средно време до договореност", "f_on_time": "Спазени срокове по договорености",
           "f_signatures": "Подписани документи", "f_documents": "Приложени файлове",
           "f_value": "Оборот през платформата", "f_pays_in_days": "Плаща в рамките на", "f_pays_on_time": "Плаща в срок"},
    "de": {"reg_label": "Firmennummer", "title": "Handelsreferenz", "sub": "Errechnet aus dem eigenen Betriebsverlauf des Unternehmens in Seam.",
           "gone": "Referenz nicht verfügbar",
           "gone_p": "Der Link wurde vom Unternehmen widerrufen oder ist nicht mehr aktuell. Fordern Sie einen neuen Link an.",
           "country": "Land", "as_of": "Stand", "verified": "geprüft", "days": "Tage",
           "explain": "Die Werte stammen aus dem Prüfpfad der Plattform und wurden nicht von Hand eingetragen. Es werden weder Geschäftspartner noch vereinbarte Preise oder Konditionen offengelegt.",
           "verify_t": "Überprüfung", "verify_p": "Die Werte sind mit dem Schlüssel dieser Seam-Installation signiert. Der signierte Datensatz und der öffentliche Schlüssel stehen bereit unter",
           "privacy": "Veröffentlicht wird ausschließlich die eigene Gesamtzahl des Unternehmens. Daten von Geschäftspartnern werden nicht geteilt.",
           "f_since": "Auf der Plattform aktiv seit", "f_counterparties": "Geschäftspartner",
           "f_records": "Eröffnete Vorgänge", "f_completion": "Abgeschlossene Vorgänge",
           "f_speed": "Durchschnittliche Zeit bis zur Einigung", "f_on_time": "Eingehaltene Termine",
           "f_signatures": "Unterzeichnete Dokumente", "f_documents": "Angehängte Dateien",
           "f_value": "Volumen über die Plattform", "f_pays_in_days": "Zahlt innerhalb von", "f_pays_on_time": "Zahlt fristgerecht"},
    "ro": {"reg_label": "Număr de înregistrare", "title": "Referință comercială", "sub": "Calculată din propriul istoric operațional al companiei în Seam.",
           "gone": "Referința nu este disponibilă",
           "gone_p": "Linkul a fost retras de companie sau nu mai este actual. Solicitați companiei un link nou.",
           "country": "Țara", "as_of": "La data de", "verified": "verificat", "days": "zile",
           "explain": "Valorile sunt calculate din pista de audit a platformei, nu sunt introduse manual. Nu se dezvăluie parteneri, prețuri convenite sau condiții.",
           "verify_t": "Verificare", "verify_p": "Valorile sunt semnate cu cheia acestei instalări Seam. Înregistrarea semnată și cheia publică sunt disponibile la",
           "privacy": "Se publică doar totalul propriu al companiei. Nu se partajează date despre parteneri.",
           "f_since": "Activ pe platformă din", "f_counterparties": "Parteneri comerciali",
           "f_records": "Dosare deschise", "f_completion": "Dosare finalizate",
           "f_speed": "Timp mediu până la acord", "f_on_time": "Termene respectate",
           "f_signatures": "Documente semnate", "f_documents": "Fișiere atașate",
           "f_value": "Volum prin platformă", "f_pays_in_days": "Plătește în", "f_pays_on_time": "Plătește la termen"},
    "el": {"reg_label": "Αριθμός μητρώου", "title": "Εμπορική σύσταση", "sub": "Υπολογισμένη από το ίδιο το λειτουργικό ιστορικό της εταιρείας στο Seam.",
           "gone": "Η σύσταση δεν είναι διαθέσιμη",
           "gone_p": "Ο σύνδεσμος ανακλήθηκε από την εταιρεία ή δεν είναι πλέον έγκυρος. Ζητήστε νέο σύνδεσμο από την εταιρεία.",
           "country": "Χώρα", "as_of": "Με ημερομηνία", "verified": "επαληθευμένο", "days": "ημέρες",
           "explain": "Τα μεγέθη υπολογίζονται από το ιστορικό ελέγχου της πλατφόρμας και δεν καταχωρούνται χειροκίνητα. Δεν αποκαλύπτονται αντισυμβαλλόμενοι, συμφωνημένες τιμές ή όροι.",
           "verify_t": "Επαλήθευση", "verify_p": "Τα μεγέθη υπογράφονται με το κλειδί αυτής της εγκατάστασης Seam. Η υπογεγραμμένη εγγραφή και το δημόσιο κλειδί διατίθενται στο",
           "privacy": "Δημοσιεύεται μόνο το συγκεντρωτικό μέγεθος της ίδιας της εταιρείας. Δεν κοινοποιούνται δεδομένα αντισυμβαλλομένων.",
           "f_since": "Ενεργή στην πλατφόρμα από", "f_counterparties": "Αντισυμβαλλόμενοι",
           "f_records": "Φάκελοι που άνοιξαν", "f_completion": "Φάκελοι που ολοκληρώθηκαν",
           "f_speed": "Μέσος χρόνος έως τη συμφωνία", "f_on_time": "Τηρημένες προθεσμίες",
           "f_signatures": "Υπογεγραμμένα έγγραφα", "f_documents": "Συνημμένα αρχεία",
           "f_value": "Όγκος μέσω της πλατφόρμας", "f_pays_in_days": "Πληρώνει εντός", "f_pays_on_time": "Πληρώνει εμπρόθεσμα"},
    "tr": {"reg_label": "Sicil numarası", "title": "Ticari referans", "sub": "Şirketin Seam üzerindeki kendi işlem kaydından hesaplanmıştır.",
           "gone": "Referans kullanılamıyor",
           "gone_p": "Bağlantı şirket tarafından geri alındı veya artık geçerli değil. Şirketten yeni bir bağlantı isteyin.",
           "country": "Ülke", "as_of": "Tarih itibarıyla", "verified": "doğrulandı", "days": "gün",
           "explain": "Değerler platformun denetim kaydından hesaplanır, elle girilmez. Hiçbir karşı taraf, anlaşılan fiyat veya koşul açıklanmaz.",
           "verify_t": "Doğrulama", "verify_p": "Değerler bu Seam kurulumunun anahtarıyla imzalanmıştır. İmzalı kayıt ve açık anahtar şu adreste bulunur:",
           "privacy": "Yalnızca şirketin kendi toplamı yayımlanır. Karşı taraf verileri paylaşılmaz.",
           "f_since": "Platformda faal olduğu tarih", "f_counterparties": "Karşı taraf sayısı",
           "f_records": "Açılan kayıtlar", "f_completion": "Tamamlanan kayıtlar",
           "f_speed": "Anlaşmaya varma süresi (ortalama)", "f_on_time": "Zamanında karşılanan taahhütler",
           "f_signatures": "İmzalanan belgeler", "f_documents": "Eklenen dosyalar",
           "f_value": "Platform üzerinden hacim", "f_pays_in_days": "Ödeme süresi", "f_pays_on_time": "Vadesinde ödeme"},
    "it": {"reg_label": "Numero di registro", "title": "Referenza commerciale", "sub": "Calcolata dal registro operativo dell'azienda stessa in Seam.",
           "gone": "Referenza non disponibile",
           "gone_p": "Il collegamento è stato revocato dall'azienda o non è più attuale. Richiedete un nuovo collegamento all'azienda.",
           "country": "Paese", "as_of": "Alla data", "verified": "verificato", "days": "giorni",
           "explain": "I valori sono calcolati dal registro di controllo della piattaforma, non inseriti a mano. Non vengono divulgati controparti, prezzi concordati o condizioni.",
           "verify_t": "Verifica", "verify_p": "I valori sono firmati con la chiave di questa installazione di Seam. Il record firmato e la chiave pubblica sono disponibili a",
           "privacy": "Viene pubblicato solo il dato aggregato dell'azienda stessa. Nessun dato delle controparti viene condiviso.",
           "f_since": "Attiva sulla piattaforma da", "f_counterparties": "Controparti",
           "f_records": "Pratiche aperte", "f_completion": "Pratiche portate a termine",
           "f_speed": "Tempo medio fino all'accordo", "f_on_time": "Scadenze rispettate",
           "f_signatures": "Documenti firmati", "f_documents": "File allegati",
           "f_value": "Volume tramite la piattaforma", "f_pays_in_days": "Paga entro", "f_pays_on_time": "Paga puntualmente"},
    "ru": {"reg_label": "Регистрационный номер", "title": "Торговая референция", "sub": "Рассчитана по собственной операционной истории компании в Seam.",
           "gone": "Референция недоступна",
           "gone_p": "Ссылка отозвана компанией или более неактуальна. Запросите у компании новую ссылку.",
           "country": "Страна", "as_of": "По состоянию на", "verified": "проверено", "days": "дн.",
           "explain": "Показатели рассчитаны по журналу аудита платформы, а не введены вручную. Контрагенты, согласованные цены и условия не раскрываются.",
           "verify_t": "Проверка", "verify_p": "Показатели подписаны ключом этой установки Seam. Подписанная запись и открытый ключ доступны по адресу",
           "privacy": "Публикуется только собственный сводный показатель компании. Данные контрагентов не передаются.",
           "f_since": "Работает на платформе с", "f_counterparties": "Контрагенты",
           "f_records": "Открытые дела", "f_completion": "Доведено до завершения",
           "f_speed": "Среднее время до договорённости", "f_on_time": "Соблюдённые сроки",
           "f_signatures": "Подписанные документы", "f_documents": "Приложенные файлы",
           "f_value": "Объём через платформу", "f_pays_in_days": "Платит в течение", "f_pays_on_time": "Платит в срок"},
    "es": {"reg_label": "Número de registro", "title": "Referencia comercial", "sub": "Calculada a partir del propio registro operativo de la empresa en Seam.",
           "gone": "Referencia no disponible",
           "gone_p": "La empresa ha revocado el enlace o este ya no está vigente. Solicite un enlace nuevo a la empresa.",
           "country": "País", "as_of": "A fecha de", "verified": "verificado", "days": "días",
           "explain": "Las cifras se calculan a partir del registro de auditoría de la plataforma, no se introducen a mano. No se revela ninguna contraparte, precio acordado ni condición.",
           "verify_t": "Verificación", "verify_p": "Las cifras están firmadas con la clave de esta instalación de Seam. El registro firmado y la clave pública están disponibles en",
           "privacy": "Solo se publica el agregado propio de la empresa. No se comparten datos de contrapartes.",
           "f_since": "Activa en la plataforma desde", "f_counterparties": "Contrapartes",
           "f_records": "Expedientes abiertos", "f_completion": "Expedientes completados",
           "f_speed": "Tiempo medio hasta el acuerdo", "f_on_time": "Plazos cumplidos",
           "f_signatures": "Documentos firmados", "f_documents": "Archivos adjuntos",
           "f_value": "Volumen a través de la plataforma", "f_pays_in_days": "Paga en", "f_pays_on_time": "Paga a tiempo"},
    "fr": {"reg_label": "Numéro d'immatriculation", "title": "Référence commerciale", "sub": "Calculée à partir du propre historique opérationnel de l'entreprise dans Seam.",
           "gone": "Référence indisponible",
           "gone_p": "Le lien a été révoqué par l'entreprise ou n'est plus à jour. Demandez un nouveau lien à l'entreprise.",
           "country": "Pays", "as_of": "À la date du", "verified": "vérifié", "days": "jours",
           "explain": "Les chiffres sont calculés à partir de la piste d'audit de la plateforme, ils ne sont pas saisis à la main. Aucune contrepartie, aucun prix convenu ni aucune condition n'est divulgué.",
           "verify_t": "Vérification", "verify_p": "Les chiffres sont signés avec la clé de cette installation Seam. L'enregistrement signé et la clé publique sont disponibles à",
           "privacy": "Seul l'agrégat propre de l'entreprise est publié. Aucune donnée de contrepartie n'est partagée.",
           "f_since": "Active sur la plateforme depuis", "f_counterparties": "Contreparties",
           "f_records": "Dossiers ouverts", "f_completion": "Dossiers menés à terme",
           "f_speed": "Délai moyen jusqu'à l'accord", "f_on_time": "Échéances respectées",
           "f_signatures": "Documents signés", "f_documents": "Fichiers joints",
           "f_value": "Volume via la plateforme", "f_pays_in_days": "Règle sous", "f_pays_on_time": "Règle dans les délais"},
    "pl": {"reg_label": "Numer rejestrowy", "title": "Referencja handlowa", "sub": "Wyliczona z własnej historii operacyjnej firmy w Seam.",
           "gone": "Referencja niedostępna",
           "gone_p": "Link został wycofany przez firmę lub jest już nieaktualny. Poproś firmę o nowy link.",
           "country": "Kraj", "as_of": "Stan na", "verified": "zweryfikowano", "days": "dni",
           "explain": "Wartości są wyliczane ze ścieżki audytu platformy, a nie wpisywane ręcznie. Nie ujawnia się kontrahentów, uzgodnionych cen ani warunków.",
           "verify_t": "Weryfikacja", "verify_p": "Wartości są podpisane kluczem tej instalacji Seam. Podpisany zapis i klucz publiczny są dostępne pod adresem",
           "privacy": "Publikowane jest wyłącznie własne zestawienie firmy. Dane kontrahentów nie są udostępniane.",
           "f_since": "Aktywna na platformie od", "f_counterparties": "Kontrahenci",
           "f_records": "Otwarte sprawy", "f_completion": "Sprawy doprowadzone do końca",
           "f_speed": "Średni czas do uzgodnienia", "f_on_time": "Dotrzymane terminy",
           "f_signatures": "Podpisane dokumenty", "f_documents": "Załączone pliki",
           "f_value": "Wolumen przez platformę", "f_pays_in_days": "Płaci w ciągu", "f_pays_on_time": "Płaci terminowo"},
    "uk": {"reg_label": "Реєстраційний номер", "title": "Торгова референція", "sub": "Обчислена з власного операційного запису компанії в Seam.",
           "gone": "Референція недоступна",
           "gone_p": "Посилання відкликане компанією або більше не є актуальним. Попросіть у компанії нове посилання.",
           "country": "Країна", "as_of": "Станом на", "verified": "перевірено", "days": "дн.",
           "explain": "Показники обчислені з журналу аудиту платформи, а не введені вручну. Контрагентів, погоджені ціни та умови не розкрито.",
           "verify_t": "Перевірка", "verify_p": "Показники підписані ключем цього встановлення Seam. Підписаний запис і відкритий ключ доступні за адресою",
           "privacy": "Публікується лише власний зведений показник компанії. Дані контрагентів не передаються.",
           "f_since": "Працює на платформі з", "f_counterparties": "Контрагенти",
           "f_records": "Відкриті справи", "f_completion": "Доведено до завершення",
           "f_speed": "Середній час до домовленості", "f_on_time": "Дотримані строки",
           "f_signatures": "Підписані документи", "f_documents": "Долучені файли",
           "f_value": "Обсяг через платформу", "f_pays_in_days": "Сплачує протягом", "f_pays_on_time": "Сплачує вчасно"},
    "pt": {"reg_label": "Número de registo", "title": "Referência comercial", "sub": "Calculada a partir do próprio registo operacional da empresa no Seam.",
           "gone": "Referência indisponível",
           "gone_p": "A ligação foi revogada pela empresa ou já não está atual. Peça uma nova ligação à empresa.",
           "country": "País", "as_of": "À data de", "verified": "verificado", "days": "dias",
           "explain": "Os valores são calculados a partir do registo de auditoria da plataforma e não são introduzidos à mão. Não é divulgada qualquer contraparte, preço acordado ou condição.",
           "verify_t": "Verificação", "verify_p": "Os valores são assinados com a chave desta instalação do Seam. O registo assinado e a chave pública estão disponíveis em",
           "privacy": "Apenas o agregado da própria empresa é publicado. Não são partilhados dados de contrapartes.",
           "f_since": "Ativa na plataforma desde", "f_counterparties": "Contrapartes",
           "f_records": "Processos abertos", "f_completion": "Processos concluídos",
           "f_speed": "Tempo médio até ao acordo", "f_on_time": "Prazos cumpridos",
           "f_signatures": "Documentos assinados", "f_documents": "Ficheiros anexados",
           "f_value": "Volume através da plataforma", "f_pays_in_days": "Paga em", "f_pays_on_time": "Paga a tempo"},
}

REF_STYLE = """
.rgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(190px,100%),1fr));gap:12px;margin:22px 0 4px}
.rstat{background:#f7f8fa;border:1px solid #eceef1;border-radius:10px;padding:14px 16px}
.rstat .rv{font-size:23px;font-weight:800;letter-spacing:-.01em}
.rstat .rv small{font-size:12.5px;font-weight:600;color:#5b6472;margin-left:3px}
.rstat .rl{font-size:11.5px;color:#5b6472;margin-top:3px;line-height:1.4}
.rhead{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
.rid{font-size:12.5px;color:#5b6472;line-height:1.7}
.rver{background:#eef6ee;border:1px solid #c3ddc3;color:#3c6340;border-radius:999px;
  padding:4px 11px;font-size:11.5px;font-weight:700;white-space:nowrap}
.qn{background:#eef6ee;border:1px solid #c3ddc3;color:#3c6340;border-radius:8px;
  padding:11px 14px;font-size:12.5px;line-height:1.55;margin-top:20px}
.rsig{margin-top:16px;font-size:11.5px;color:#5b6472;line-height:1.6}
.rsig code{display:block;margin-top:5px;background:#f7f8fa;border:1px solid #e3e6ea;border-radius:6px;
  padding:7px 9px;font-size:10.5px;word-break:break-all;overflow-wrap:anywhere}
.loc{color:#8a93a3}
"""


def ref_keys():
    """A signing key for references, kept separate from the push key: one key,
    one purpose."""
    if os.path.exists(REF_KEY_FILE):
        try:
            with open(REF_KEY_FILE, "r", encoding="utf-8") as f:
                k = json.load(f)
            if k.get("private") and k.get("public"):
                return k
        except Exception:
            pass
    k = webpush.generate_keys()
    with open(REF_KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(k, f)
    cryptobox.secure_file(REF_KEY_FILE)
    return k


def _ref_token(row):
    """The published link, recovered from its encrypted copy. `token` itself
    holds only a hash, so the database never contains a working URL."""
    if not row:
        return None
    return cryptobox.decrypt_field(row["token_enc"] if "token_enc" in row.keys() else None)


def _value_band(total):
    for edge, label in ((10000, "< 10k"), (50000, "10k - 50k"), (100000, "50k - 100k"),
                        (500000, "100k - 500k"), (1000000, "500k - 1M")):
        if total < edge:
            return label
    return "> 1M"


def reference_facts(conn, org_id):
    """Compute the aggregate from records that already exist. Nothing here is
    entered by hand, which is the whole point."""
    ws_rows = conn.execute(
        "SELECT * FROM workspaces WHERE buyer_org_id=? OR partner_org_id=?",
        (org_id, org_id)).fetchall()
    ws_ids = [w["id"] for w in ws_rows]
    counterparties = len({(w["partner_org_id"] if w["buyer_org_id"] == org_id
                           else w["buyer_org_id"]) for w in ws_rows} - {None})
    if not ws_ids:
        return {"since": None, "counterparties": 0, "records": 0, "closed": 0,
                "completion": 0, "agreement_speed": None, "on_time": None,
                "signatures": 0, "documents": 0, "value_band": None}

    ph = ",".join("?" * len(ws_ids))
    orders = conn.execute("SELECT * FROM orders WHERE workspace_id IN (%s)" % ph, ws_ids).fetchall()
    closed = 0
    value = 0.0
    on_time_hits = on_time_total = 0
    first = None
    for o in orders:
        created = _parse_ts(o["created_at"])
        if created and (first is None or created < first):
            first = created
        stages = tpl_stage_keys(conn, o["template"]) or []
        terminal = stages[-1] if stages else None
        spec = {s["key"]: s for s in (tpl_get(conn, o["template"]) or {}).get("terms", [])}
        is_closed = bool(terminal) and o["status"] == terminal
        if is_closed:
            closed += 1
            closed_at = _order_close_time(conn, o["id"], terminal)
            # A dated term that was agreed is a commitment; meeting it is the
            # only defensible definition of "on time" this data supports.
            for t_ in conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=?",
                                   (o["id"],)).fetchall():
                if t_["state"] != "agreed":
                    continue
                if (spec.get(t_["key"]) or {}).get("type") == "date":
                    due = _ics_date(t_["value"])
                    if due and closed_at:
                        on_time_total += 1
                        if closed_at.date() <= due.date():
                            on_time_hits += 1
        for t_ in conn.execute("SELECT key,value,state FROM order_terms WHERE order_id=?",
                               (o["id"],)).fetchall():
            if t_["state"] == "agreed" and (spec.get(t_["key"]) or {}).get("type") == "money":
                value += _price_eur(t_["value"])

    speeds = []
    for e in conn.execute(
            "SELECT o.id AS oid, t.key, t.updated_at FROM order_terms t "
            "JOIN orders o ON o.id=t.order_id WHERE o.workspace_id IN (%s) AND t.state='agreed'"
            % ph, ws_ids).fetchall():
        prop = conn.execute(
            "SELECT created_at FROM events WHERE order_id=? AND kind='term_proposed' "
            "AND meta_json LIKE ? ORDER BY id DESC LIMIT 1",
            (e["oid"], '%%"term": "%s"%%' % e["key"])).fetchone()
        a = _parse_ts(prop["created_at"]) if prop else None
        b = _parse_ts(e["updated_at"])
        if a and b and b >= a:
            speeds.append((b - a).total_seconds() / 86400.0)

    sigs = conn.execute("SELECT COUNT(*) c FROM signatures WHERE org_id=? AND status='signed'",
                        (org_id,)).fetchone()["c"]
    docs = conn.execute("SELECT COUNT(*) c FROM attachments WHERE order_id IN "
                        "(SELECT id FROM orders WHERE workspace_id IN (%s))" % ph, ws_ids).fetchone()["c"]

    # How this company behaves about money, from invoices its counterparties
    # issued to it here. This is the thing an accounting package cannot know,
    # because it only ever holds one side: here the same row is the seller's
    # receivable and the buyer's obligation, so the days between issuing and
    # settling are recorded rather than claimed.
    #
    # It names nobody. A count, an average and a share settled on time is the
    # most that can be published about a trading relationship without
    # publishing the counterparty's business along with it - the same rule
    # every other field here follows.
    pays = receivables.punctuality([dict(r) for r in conn.execute(
        "SELECT issue_date, due_date, paid_at FROM invoices "
        "WHERE customer_org_id=? AND status='paid'", (org_id,)).fetchall()])

    return {
        "since": first.strftime("%Y-%m") if first else None,
        "counterparties": counterparties,
        "records": len(orders),
        "closed": closed,
        "completion": int(round(100.0 * closed / len(orders))) if orders else 0,
        "agreement_speed": round(sum(speeds) / len(speeds), 1) if speeds else None,
        "on_time": int(round(100.0 * on_time_hits / on_time_total)) if on_time_total else None,
        "signatures": sigs,
        "documents": docs,
        "value_band": _value_band(value) if value else None,
        "pays_in_days": pays["avg_days"] if pays else None,
        "pays_on_time": pays["on_time_pct"] if pays else None,
        "paid_invoices": pays["invoices"] if pays else 0,
    }


def reference_payload(conn, row):
    """The exact object that gets signed. Field order is fixed so the signature
    is reproducible by whoever verifies it."""
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (row["org_id"],)).fetchone()
    try:
        chosen = json.loads(row["fields_json"] or "{}")
    except Exception:
        chosen = {}
    facts = reference_facts(conn, org["id"])
    meta = company.country_meta(org["country"])
    shown = {k: facts.get(k) for k in REF_FIELDS if chosen.get(k, REF_DEFAULT[k])}
    body = {
        "issuer": "Seam",
        "company": dl(org["name"]),
        "country": org["country"] or "",
        "id_label": meta["id_label"],
        "reg_number": org["reg_number"] or "",
        "verified_company": bool(org["verified_company"]),
        "as_of": datetime.utcnow().strftime("%Y-%m-%d"),
        "facts": shown,
    }
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    k = ref_keys()
    d = int.from_bytes(webpush._b64d(k["private"]), "big")
    sig = webpush._sign(hashlib.sha256(canonical.encode("utf-8")).digest(), d)
    return {"reference": body, "signature": webpush._b64(sig), "key": k["public"],
            "algorithm": "ES256 over SHA-256 of the canonical JSON"}


@app.get("/api/reference")
@login_required
def reference_get():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    row = conn.execute("SELECT * FROM org_references WHERE org_id=?", (org["id"],)).fetchone()
    try:
        chosen = json.loads(row["fields_json"]) if row else {}
    except Exception:
        chosen = {}
    return jsonify({
        "published": bool(row and row["active"]),
        "url": ((request.url_root.rstrip("/") + "/ref/" + _ref_token(row))
                if row and _ref_token(row) else None),
        "views": row["views"] if row else 0,
        "published_at": row["published_at"] if row else None,
        "fields": {k: bool(chosen.get(k, REF_DEFAULT[k])) for k in REF_FIELDS},
        "preview": reference_facts(conn, org["id"]),
    })


@app.post("/api/reference")
@login_required
def reference_publish():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = body()
    chosen = {k: bool((d.get("fields") or {}).get(k, REF_DEFAULT[k])) for k in REF_FIELDS}
    row = conn.execute("SELECT * FROM org_references WHERE org_id=?", (org["id"],)).fetchone()
    # Republishing keeps the same link, so a reference already handed out does
    # not silently stop working; only revoking changes it.
    token = (_ref_token(row) if row else None) or secrets.token_urlsafe(18)
    conn.execute(
        "INSERT INTO org_references (org_id, token, token_enc, fields_json, active, published_by) "
        "VALUES (?,?,?,?,1,?) ON CONFLICT(org_id) DO UPDATE SET fields_json=excluded.fields_json, "
        "active=1, published_at=datetime('now'), published_by=excluded.published_by",
        (org["id"], db.token_hash(token), cryptobox.encrypt_field(token),
         json.dumps(chosen, ensure_ascii=False), user["id"]))
    conn.commit()
    return jsonify({"published": True,
                    "url": request.url_root.rstrip("/") + "/ref/" + token})


@app.post("/api/reference/revoke")
@login_required
def reference_revoke():
    """Revoking issues a new token as well, so a link already handed out cannot
    be brought back to life by republishing."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    _fresh = secrets.token_urlsafe(18)
    conn.execute("UPDATE org_references SET active=0, token=?, token_enc=? WHERE org_id=?",
                 (db.token_hash(_fresh), cryptobox.encrypt_field(_fresh), org["id"]))
    conn.commit()
    return jsonify({"published": False})


@app.get("/ref/<token>.json")
def reference_json(token):
    """Everything a third party needs to check the signature offline."""
    conn = db.get_db()
    try:
        row = conn.execute("SELECT * FROM org_references WHERE token=? AND active=1",
                           (db.token_hash(token),)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(reference_payload(conn, row))
    finally:
        conn.close()


@app.get("/ref/<token>")
def reference_page(token):
    """The reader of a reference is usually not a Seam user, so ?lang= decides
    the language: they have no cookie of ours to carry a preference."""
    conn = db.get_db()
    try:
        lang = request.args.get("lang") or req_lang()
        if lang not in REF_I18N:
            lang = "en"
        L = REF_I18N[lang]
        row = conn.execute("SELECT * FROM org_references WHERE token=? AND active=1",
                           (db.token_hash(token),)).fetchone()
        if not row:
            return _doc_shell(lang, L["gone"],
                              '%s<h1>%s</h1><div class="doc-sub">%s</div>'
                              % (_doc_brandmark(lang), html_escape(L["gone"]),
                                 html_escape(L["gone_p"])), printable=False), 404
        if not rate_limited("refview:" + _client_ip(), 240, 3600):
            conn.execute("UPDATE org_references SET views=views+1 WHERE org_id=?",
                         (row["org_id"],))
            conn.commit()
        payload = reference_payload(conn, row)
        ref, facts = payload["reference"], payload["reference"]["facts"]

        label = {"since": L["f_since"], "counterparties": L["f_counterparties"],
                 "records": L["f_records"], "completion": L["f_completion"],
                 "agreement_speed": L["f_speed"], "on_time": L["f_on_time"],
                 "signatures": L["f_signatures"], "documents": L["f_documents"],
                 "value_band": L["f_value"]}
        # A percentage sits against its number; a word does not.
        unit = {"completion": "%", "on_time": "%", "agreement_speed": " " + L["days"]}
        cards = ""
        for k in REF_FIELDS:
            if k not in facts or facts[k] in (None, ""):
                continue
            cards += ('<div class="rstat"><div class="rv">%s%s</div><div class="rl">%s</div></div>'
                      % (html_escape(str(facts[k])),
                         ("<small>%s</small>" % html_escape(unit[k])) if k in unit else "",
                         html_escape(label[k])))

        # An empty registration number or country is left out rather than shown
        # as a dash: a blank row reads as missing data on a page whose whole
        # purpose is to be believed.
        #
        # The number carries two labels on purpose. The reader's own word for
        # it, so a German reading about a Bulgarian company is not staring at
        # Cyrillic with no idea what it means; and the local designation next to
        # it, because a Bulgarian company number *is* called ЕИК and that is the
        # word they will need when they look it up in the register.
        ident = ""
        if ref["reg_number"]:
            local = ('&nbsp;<span class="loc">(%s)</span>' % html_escape(ref["id_label"])) \
                    if ref["country"] else ""
            ident += "%s%s <b>%s</b>" % (html_escape(L["reg_label"]), local,
                                         html_escape(ref["reg_number"]))
        if ref["country"]:
            ident += ("<br>" if ident else "") + "%s %s" % (html_escape(L["country"]) + ":",
                                                           html_escape(ref["country"]))

        inner = ("""<style>%(style)s</style>%(brand)s
<div class="rhead">
 <div><h1>%(company)s</h1>
  %(ident)s</div>
 %(ver)s
</div>
<div class="doc-sub" style="margin-top:16px">%(sub)s</div>
<div class="rgrid">%(cards)s</div>
<div class="qn">%(explain)s</div>
<div class="rsig"><b>%(verifyl)s</b><br>%(verify)s <a href="%(jsonurl)s">%(jsonurl)s</a>
 <code>%(fp)s</code></div>
<p class="disc">%(asofl)s: <b>%(asof)s</b>. %(privacy)s</p>""") % {
            "style": REF_STYLE, "brand": _doc_brandmark(lang),
            "company": html_escape(ref["company"]), "sub": html_escape(L["sub"]),
            "ident": ('<div class="rid">%s</div>' % ident) if ident else "",
            "ver": ('<div class="rver">&#10003; %s</div>' % html_escape(L["verified"]))
                   if ref["verified_company"] else "",
            "cards": cards, "asofl": html_escape(L["as_of"]),
            "asof": html_escape(ref["as_of"]),
            "explain": html_escape(L["explain"]),
            "verifyl": html_escape(L["verify_t"]), "verify": html_escape(L["verify_p"]),
            "jsonurl": html_escape(request.path + ".json"),
            "fp": html_escape(payload["signature"]),
            "privacy": html_escape(L["privacy"]),
        }
        return _doc_shell(lang, "%s - %s" % (ref["company"], L["title"]), inner)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  What this installation actually does with data
#
#  Not a policy document. A policy is a promise about software; this is a report
#  from the software. Everything under "leaves" is read from the running
#  configuration, so a company sees what its own instance is doing today rather
#  than what the vendor intended in general.
# --------------------------------------------------------------------------- #
def data_statement(conn, org):
    # Each of these returns None when nothing is configured, which is the
    # normal state on a fresh instance and the one this page exists to report.
    q_cfg = (org_qtsp_config(conn, org["id"]) if org else None) or {}
    ai_cfg = ai_config() or {}
    cloud_cfg = cloudstore.config() or {}
    sms_cfg = sms.config() or {}
    try:
        backups = len([f for f in os.listdir(BACKUP_DIR) if ".zip" in f])
    except OSError:
        backups = 0
    leaves = [
        {"key": "email", "on": mailer.enabled(),
         "where": os.environ.get("SEAM_SMTP_HOST", "")},
        {"key": "sms", "on": sms.enabled(), "where": sms_cfg.get("provider", "")},
        {"key": "push", "on": bool(conn.execute(
            "SELECT 1 FROM push_subscriptions LIMIT 1").fetchone()), "where": ""},
        {"key": "cloud", "on": cloudstore.enabled(),
         "where": cloud_cfg.get("endpoint", "")},
        {"key": "qtsp", "on": qtsp.is_ready(q_cfg),
         "where": (qtsp.PROVIDERS.get(q_cfg.get("provider") or "") or {}).get("name", "")},
        # Statements are uploaded by hand. Seam holds no bank credentials and
        # never reaches out to a bank, which is why this one cannot be turned on.
        {"key": "bank", "on": False, "where": ""},
        {"key": "ai", "on": ai_enabled(), "where": ai_cfg.get("provider", "")},
        {"key": "invbg", "on": bool(invbg_token()), "where": INVBG_BASE},
        {"key": "stripe", "on": bool(stripe_key()), "where": "api.stripe.com"},
    ]
    return {
        "stored": ["account", "company", "relationships", "documents", "files",
                   "audit", "notifications", "network"],
        "leaves": leaves,
        "never": ["prices", "partner_lists", "cross_tenant", "selling"],
        "retention": {
            "error_log": ERROR_LOG_KEEP,
            "session_idle_hours": SESSION_IDLE // 3600,
            "session_max_days": SESSION_MAX // 86400,
            "backups": backups,
        },
    }


@app.get("/api/privacy")
@login_required
def privacy_statement():
    conn, user = g.conn, g.user
    return jsonify(data_statement(conn, user_primary_org(conn, user["id"])))


# --------------------------------------------------------------------------- #
#  The network: introductions and needs
#
#  A company's partner list is its own asset, so Seam never shows it to anyone.
#  Two mechanisms give the same benefit without handing that list over:
#
#  Introductions. A asks its own partner B to introduce it to C. A already knows
#  C exists - from a published reference or from the world. B either passes the
#  request on or does not, and C decides for itself. A is told only pending,
#  accepted or declined: if A could tell "B forwarded it" from "B refused", A
#  would learn whether B works with C, which is exactly the fact B has not
#  agreed to share.
#
#  Needs. A posts what it is looking for. The post reaches partners and their
#  partners, never the open web, and never names the company in between.
# --------------------------------------------------------------------------- #
NETWORK_HOPS = 2
NEED_FEED_LIMIT = 60


def _partner_ids(conn, org_id):
    rows = conn.execute(
        "SELECT buyer_org_id AS a, partner_org_id AS b FROM workspaces "
        "WHERE buyer_org_id=? OR partner_org_id=?", (org_id, org_id)).fetchall()
    out = set()
    for r in rows:
        out.add(r["b"] if r["a"] == org_id else r["a"])
    out.discard(None)
    out.discard(org_id)
    return out


def network_reach(conn, org_id, hops=NETWORK_HOPS):
    """{org_id: distance} for everyone within `hops`. Used to decide what may be
    shown; the map itself is never returned to a client."""
    seen, frontier = {}, {org_id}
    for d in range(1, hops + 1):
        nxt = set()
        for oid in frontier:
            for p in _partner_ids(conn, oid):
                if p != org_id and p not in seen:
                    seen[p] = d
                    nxt.add(p)
        frontier = nxt
        if not frontier:
            break
    return seen


def _org_card(conn, org_id, with_contacts=False):
    o = conn.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
    if not o:
        return None
    ref = conn.execute("SELECT token, token_enc FROM org_references WHERE org_id=? AND active=1",
                       (org_id,)).fetchone()
    card = {"id": o["id"], "name": dl(o["name"]), "country": o["country"] or "",
            "reg_number": o["reg_number"] or "", "verified": bool(o["verified_company"]),
            "reference_url": ("/ref/" + _ref_token(ref)) if _ref_token(ref) else None}
    if with_contacts:
        card["contacts"] = _org_contacts(o)
        card["emails"] = [r["email"] for r in conn.execute(
            "SELECT u.email FROM users u JOIN memberships m ON m.user_id=u.id "
            "WHERE m.org_id=? ORDER BY u.id LIMIT 3", (org_id,)).fetchall()]
    return card


def _intro_row(conn, r, me):
    """The asker never learns which of the two sides said no, nor that the
    request reached the third company at all."""
    mine = r["asker_org_id"] == me
    status = r["status"]
    if mine and status == "forwarded":
        status = "requested"
    out = {"id": r["id"], "status": status, "message": r["message"] or "",
           "note": r["note"] or "", "created_at": r["created_at"],
           "decided_at": r["decided_at"], "role": "asker" if mine else
           ("via" if r["via_org_id"] == me else "target")}
    done = r["status"] == "accepted"
    if out["role"] == "asker":
        out["target"] = _org_card(conn, r["target_org_id"], done)
        out["via"] = _org_card(conn, r["via_org_id"])
    elif out["role"] == "via":
        out["asker"] = _org_card(conn, r["asker_org_id"])
        out["target"] = _org_card(conn, r["target_org_id"])
    else:
        out["asker"] = _org_card(conn, r["asker_org_id"], done)
        out["via"] = _org_card(conn, r["via_org_id"])
    return out


def _need_row(conn, r, me, dist):
    replies = conn.execute("SELECT * FROM need_replies WHERE need_id=? ORDER BY id",
                           (r["id"],)).fetchall()
    mine = r["org_id"] == me
    out = {"id": r["id"], "title": dl(r["title"]), "detail": dl(r["detail"] or ""),
           "template": r["template"] or "", "country": r["country"] or "",
           "status": r["status"], "created_at": r["created_at"], "mine": mine,
           "distance": 0 if mine else dist, "reply_count": len(replies),
           "replied": any(x["org_id"] == me for x in replies)}
    if mine:
        # Only the company that posted sees who answered: replying is how a
        # company chooses to identify itself, and only to the asker.
        out["replies"] = [{"id": x["id"], "message": x["message"] or "",
                           "created_at": x["created_at"],
                           "org": _org_card(conn, x["org_id"], True)} for x in replies]
    else:
        out["org"] = _org_card(conn, r["org_id"])
    return out


@app.get("/api/network")
@login_required
def network_get():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    me = org["id"]
    reach = network_reach(conn, me)
    partners = [_org_card(conn, p) for p in sorted(_partner_ids(conn, me))]

    intros = conn.execute(
        "SELECT * FROM introductions WHERE asker_org_id=? OR via_org_id=? OR "
        "(target_org_id=? AND status IN ('forwarded','accepted','declined')) "
        "ORDER BY id DESC LIMIT 80", (me, me, me)).fetchall()
    sent, inbox = [], []
    for r in intros:
        row = _intro_row(conn, r, me)
        (sent if row["role"] == "asker" else inbox).append(row)

    visible = [o for o, d in reach.items() if d <= NETWORK_HOPS]
    feed = []
    if visible:
        ph = ",".join("?" * len(visible))
        for r in conn.execute(
                "SELECT * FROM needs WHERE status='open' AND org_id IN (%s) "
                "ORDER BY id DESC LIMIT ?" % ph, visible + [NEED_FEED_LIMIT]).fetchall():
            feed.append(_need_row(conn, r, me, reach.get(r["org_id"], NETWORK_HOPS)))
    mine = [_need_row(conn, r, me, 0) for r in conn.execute(
        "SELECT * FROM needs WHERE org_id=? ORDER BY id DESC LIMIT 40", (me,)).fetchall()]

    return jsonify({"org": _org_card(conn, me), "partners": [p for p in partners if p],
                    "reach": len(reach), "introductions": {"sent": sent, "inbox": inbox},
                    "needs": {"feed": feed, "mine": mine}})


@app.get("/api/network/search")
@login_required
def network_search():
    """Only companies that have published a reference are findable. Publishing
    is the act of consenting to be found."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"results": []})
    # Matching happens in Python: SQLite's lower() is ASCII-only, so "СОФИЯ"
    # would never match "софия" if the comparison were left to SQL.
    needle = q.casefold()
    me = (org or {}).get("id") or 0
    rows = conn.execute(
        "SELECT o.id, o.name, o.reg_number FROM orgs o "
        "JOIN org_references r ON r.org_id=o.id AND r.active=1 WHERE o.id<>? ORDER BY o.name",
        (me,)).fetchall()
    hits = [r["id"] for r in rows
            if needle in dl(r["name"]).casefold()
            or needle in (r["reg_number"] or "").casefold()][:20]
    return jsonify({"results": [_org_card(conn, i) for i in hits]})


@app.post("/api/network/introductions")
@login_required
def introduction_create():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_role(conn, user["id"], org["id"], "owner", "admin", "member")
    d = body()
    via, target = int(d.get("via_org_id") or 0), int(d.get("target_org_id") or 0)
    if via not in _partner_ids(conn, org["id"]):
        raise ApiError("Може да поискате препоръка само чрез свой партньор", 400,
                       code="not_partner")
    if target in (org["id"], via) or not conn.execute(
            "SELECT 1 FROM orgs WHERE id=?", (target,)).fetchone():
        raise ApiError("Невалидно дружество", 400, code="bad_target")
    if target in _partner_ids(conn, org["id"]):
        raise ApiError("Вече работите с това дружество", 400, code="already_partner")
    if rate_limited("intro:%d" % org["id"], 20, 86400):
        raise ApiError("Достигнат дневен лимит на заявките", 429, code="intro_rate")
    try:
        cur = conn.execute(
            "INSERT INTO introductions (asker_org_id, via_org_id, target_org_id, message, created_by) "
            "VALUES (?,?,?,?,?)",
            (org["id"], via, target, (str(d.get("message") or "").strip())[:600], user["id"]))
    except sqlite3.IntegrityError:
        raise ApiError("Заявката вече е подадена", 400, code="intro_duplicate")
    conn.commit()
    notify_org(conn, via, None, None, "intro_request",
               "Заявка за препоръка", meta={"name": dl(org["name"])})
    conn.commit()
    row = conn.execute("SELECT * FROM introductions WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(_intro_row(conn, row, org["id"])), 201


@app.post("/api/network/introductions/<int:iid>/decide")
@login_required
def introduction_decide(iid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    r = conn.execute("SELECT * FROM introductions WHERE id=?", (iid,)).fetchone()
    if not r or not org:
        raise ApiError("Заявката не е намерена", 404, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin", "member")
    d = body()
    ok = str(d.get("decision") or "").lower() in ("approve", "accept", "yes")
    note = (str(d.get("note") or "").strip())[:400]

    if org["id"] == r["via_org_id"] and r["status"] == "requested":
        status = "forwarded" if ok else "declined"
        if ok:
            notify_org(conn, r["target_org_id"], None, None, "intro_request",
                       "Препоръка от партньор", meta={"name": dl(_org_card(conn, r["via_org_id"])["name"])})
    elif org["id"] == r["target_org_id"] and r["status"] == "forwarded":
        status = "accepted" if ok else "declined"
    else:
        raise ApiError("Не можете да решавате по тази заявка", 403, code="intro_not_yours")

    conn.execute("UPDATE introductions SET status=?, note=?, decided_by=?, "
                 "decided_at=datetime('now') WHERE id=?",
                 (status, note or r["note"], user["id"], iid))
    conn.commit()
    if status in ("accepted", "declined"):
        notify_org(conn, r["asker_org_id"], None, None, "intro_decided",
                   "Решение по заявка за препоръка", meta={"status": status})
        if status == "accepted":
            notify_org(conn, r["via_org_id"], None, None, "intro_decided",
                       "Препоръката е приета", meta={"status": status})
        conn.commit()
    row = conn.execute("SELECT * FROM introductions WHERE id=?", (iid,)).fetchone()
    return jsonify(_intro_row(conn, row, org["id"]))


@app.delete("/api/network/introductions/<int:iid>")
@login_required
def introduction_withdraw(iid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    r = conn.execute("SELECT * FROM introductions WHERE id=?", (iid,)).fetchone()
    if not r or not org or r["asker_org_id"] != org["id"]:
        raise ApiError("Заявката не е намерена", 404, code="not_found")
    conn.execute("DELETE FROM introductions WHERE id=?", (iid,))
    conn.commit()
    return jsonify({"deleted": True})


@app.post("/api/network/needs")
@login_required
def need_create():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_role(conn, user["id"], org["id"], "owner", "admin", "member")
    d = body()
    title = (str(d.get("title") or "").strip())[:160]
    if not title:
        raise ApiError("Опишете какво търсите", 400, code="no_title")
    if rate_limited("need:%d" % org["id"], 10, 86400):
        raise ApiError("Достигнат дневен лимит на обявите", 429, code="need_rate")
    cur = conn.execute(
        "INSERT INTO needs (org_id, title, detail, template, country, created_by) VALUES (?,?,?,?,?,?)",
        (org["id"], title, (str(d.get("detail") or "").strip())[:1200],
         (str(d.get("template") or "").strip())[:60],
         (str(d.get("country") or "").strip().upper())[:2] or None, user["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM needs WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(_need_row(conn, row, org["id"], 0)), 201


@app.post("/api/network/needs/<int:nid>/close")
@login_required
def need_close(nid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    r = conn.execute("SELECT * FROM needs WHERE id=?", (nid,)).fetchone()
    if not r or not org or r["org_id"] != org["id"]:
        raise ApiError("Обявата не е намерена", 404, code="not_found")
    conn.execute("UPDATE needs SET status='closed', closed_at=datetime('now') WHERE id=?", (nid,))
    conn.commit()
    return jsonify({"status": "closed"})


@app.post("/api/network/needs/<int:nid>/reply")
@login_required
def need_reply(nid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    r = conn.execute("SELECT * FROM needs WHERE id=? AND status='open'", (nid,)).fetchone()
    if not r or not org:
        raise ApiError("Обявата не е намерена", 404, code="not_found")
    if r["org_id"] == org["id"]:
        raise ApiError("Това е ваша обява", 400, code="own_need")
    if network_reach(conn, org["id"]).get(r["org_id"], 99) > NETWORK_HOPS:
        raise ApiError("Обявата не е достъпна за вас", 403, code="out_of_reach")
    require_role(conn, user["id"], org["id"], "owner", "admin", "member")
    try:
        conn.execute(
            "INSERT INTO need_replies (need_id, org_id, message, created_by) VALUES (?,?,?,?)",
            (nid, org["id"], (str(body().get("message") or "").strip())[:800], user["id"]))
    except sqlite3.IntegrityError:
        raise ApiError("Вече сте отговорили", 400, code="need_duplicate")
    conn.commit()
    notify_org(conn, r["org_id"], None, None, "need_reply", "Отговор на вашата обява",
               meta={"name": dl(org["name"]), "title": dl(r["title"])})
    conn.commit()
    return jsonify({"ok": True}), 201


# --------------------------------------------------------------------------- #
#  Bringing existing work in (CSV)
#
#  A company arrives with counterparties and open jobs already in a spreadsheet.
#  If day one is retyping them, the platform does not survive the week. So:
#  upload, see exactly what will happen, then commit. Nothing is written during
#  the preview, and a row that cannot be imported says why instead of being
#  dropped in silence.
# --------------------------------------------------------------------------- #
#: The one SMS Seam ever writes itself, so it is written in the reader's
#: language rather than in two at once.
SMS_TEST_I18N = {
    "en": "test message", "bg": "тестово съобщение", "de": "Testnachricht",
    "ro": "mesaj de test", "el": "δοκιμαστικό μήνυμα", "tr": "test mesajı",
    "it": "messaggio di prova", "ru": "тестовое сообщение", "es": "mensaje de prueba",
    "fr": "message de test", "pl": "wiadomość testowa", "uk": "тестове повідомлення",
    "pt": "mensagem de teste",
}

IMPORT_KINDS = ("partners", "orders")
IMPORT_MAX_ROWS = 2000

#: Column names accepted for each field, so a real spreadsheet works unedited.
#: A company exports from its own accounting software, which writes headers in
#: the language that software runs in - so every language the platform serves is
#: listed here, not only the two it started with. Matching is case-folded, which
#: is why these are all lower case.
PARTNER_COLUMNS = {
    "name": ("name", "partner", "company", "supplier", "customer", "client",
             "име", "фирма", "партньор", "контрагент",
             "firma", "kunde", "lieferant", "nume", "furnizor", "client",
             "όνομα", "εταιρεία", "προμηθευτής",
             "ad", "unvan", "firma adı", "tedarikçi",
             "azienda", "fornitore", "cliente", "ragione sociale",
             "имя", "компания", "поставщик", "клиент", "контрагент",
             "назва", "постачальник",
             "nombre", "empresa", "proveedor", "razón social",
             "nom", "société", "entreprise", "fournisseur",
             "nazwa", "kontrahent", "dostawca", "firma",
             "nome", "empresa", "fornecedor"),
    "email": ("email", "e-mail", "mail", "имейл", "поща", "эл. почта", "пошта",
              "correo", "correo electrónico", "courriel", "posta", "e-posta",
              "ηλεκτρονικό ταχυδρομείο", "poczta"),
    "reg_number": ("reg_number", "reg", "vat", "vat number", "tax id", "eik", "bulstat",
                   "еик", "булстат", "номер", " инн", "инн", "єдрпоу", "едрпоу",
                   "ust-idnr", "steuernummer", "handelsregister", "cui", "cif",
                   "αφμ", "vergi no", "partita iva", "codice fiscale",
                   "nif", "cif/nif", "siren", "siret", "nip", "regon",
                   "nipc", "company no", "company number"),
    "country": ("country", "държава", "страна", "land", "țara", "tara", "χώρα",
                "ülke", "paese", "país", "pays", "kraj", "країна", "país"),
    "template": ("template", "vertical", "шаблон", "направление", "vorlage", "branche",
                 "șablon", "domeniu", "πρότυπο", "κλάδος", "şablon", "sektör",
                 "modello", "settore", "шаблон", "отрасль", "галузь",
                 "plantilla", "sector", "modèle", "secteur", "wzorzec", "branża",
                 "modelo", "setor"),
    "workspace": ("workspace", "relation", "пространство", "релация",
                  "arbeitsbereich", "beziehung", "spațiu", "relație",
                  "χώρος", "σχέση", "çalışma alanı", "ilişki",
                  "spazio", "relazione", "пространство", "отношение",
                  "espacio", "relación", "espace", "relation",
                  "przestrzeń", "relacja", "простір", "espaço", "relação"),
}
ORDER_COLUMNS = {
    "workspace": ("workspace", "relation", "partner", "пространство", "релация", "партньор",
                  "arbeitsbereich", "beziehung", "geschäftspartner", "spațiu", "relație",
                  "partener", "χώρος", "σχέση", "συνεργάτης", "çalışma alanı", "iş ortağı",
                  "spazio", "relazione", "partner", "контрагент", "партнёр", "партнер",
                  "espacio", "socio", "espace", "partenaire", "przestrzeń", "kontrahent",
                  "espaço", "parceiro"),
    "title": ("title", "subject", "description", "описание", "заглавие", "предмет",
              "titel", "betreff", "beschreibung", "titlu", "subiect", "descriere",
              "τίτλος", "θέμα", "περιγραφή", "başlık", "konu", "açıklama",
              "titolo", "oggetto", "descrizione", "название", "тема", "описание",
              "назва", "опис", "título", "asunto", "descripción",
              "titre", "objet", "description", "tytuł", "temat", "opis",
              "título", "assunto", "descrição"),
    "ref": ("ref", "reference", "no", "number", "номер", "референция",
            "referenz", "nummer", "referință", "număr", "αναφορά", "αριθμός",
            "referans", "numara", "riferimento", "numero", "ссылка", "номер",
            "посилання", "referencia", "número", "référence", "numéro",
            "referencja", "numer", "referência"),
    "status": ("status", "stage", "статус", "етап", "phase", "stadium", "etapă", "stadiu",
               "κατάσταση", "στάδιο", "durum", "aşama", "stato", "fase",
               "статус", "этап", "стан", "етап", "estado", "etapa",
               "statut", "étape", "faza", "estado", "fase"),
}


def _csv_rows(raw):
    """Read a spreadsheet export as it actually arrives: BOM, semicolons in
    European locales, and Windows line endings."""
    if isinstance(raw, bytes):
        for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
            try:
                raw = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    raw = raw.lstrip("﻿")
    sample = raw[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
    rows = []
    for r in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip()
                     for k, v in r.items() if k})
        if len(rows) >= IMPORT_MAX_ROWS:
            break
    return rows, (reader.fieldnames or [])


def _pick(row, aliases):
    for a in aliases:
        if a in row and row[a]:
            return row[a]
    return ""


def _plan_partners(conn, org, rows):
    """Work out what each row would do, without doing any of it."""
    seen_emails = set()
    plan = []
    for i, row in enumerate(rows, start=2):          # row 1 is the header
        name = _pick(row, PARTNER_COLUMNS["name"])
        email = _pick(row, PARTNER_COLUMNS["email"]).lower()
        tpl = _pick(row, PARTNER_COLUMNS["template"]) or "general"
        ws_name = _pick(row, PARTNER_COLUMNS["workspace"]) or name
        item = {"row": i, "name": name, "email": email, "workspace": ws_name,
                "template": tpl, "action": "create", "problem": None}
        if not name:
            item.update(action="skip", problem="no_name")
        elif email and not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
            item.update(action="skip", problem="bad_email")
        elif email and email in seen_emails:
            item.update(action="skip", problem="duplicate_in_file")
        elif tpl not in TEMPLATES and not conn.execute(
                "SELECT 1 FROM custom_templates WHERE key=? AND org_id=? AND status='active'",
                (tpl, org["id"])).fetchone():
            item.update(action="skip", problem="unknown_template")
        elif conn.execute("SELECT 1 FROM workspaces WHERE buyer_org_id=? AND name=?",
                          (org["id"], ws_name)).fetchone():
            item.update(action="exists", problem=None)
        if email:
            seen_emails.add(email)
        plan.append(item)
    return plan


def _plan_orders(conn, org, rows):
    ws_rows = conn.execute("SELECT id, name, template FROM workspaces WHERE buyer_org_id=?",
                           (org["id"],)).fetchall()
    by_name = {w["name"].strip().lower(): w for w in ws_rows}
    plan = []
    for i, row in enumerate(rows, start=2):
        ws_name = _pick(row, ORDER_COLUMNS["workspace"])
        title = _pick(row, ORDER_COLUMNS["title"])
        status = _pick(row, ORDER_COLUMNS["status"])
        ws = by_name.get(ws_name.strip().lower())
        # The resolved id travels with the plan: SQLite's lower() is ASCII-only
        # and would not match a Cyrillic name that Python already lowercased, so
        # preview and commit must not each do their own lookup.
        item = {"row": i, "workspace": ws_name, "workspace_id": ws["id"] if ws else None,
                "title": title, "status": status,
                "action": "create", "problem": None, "terms": {}}
        if not title:
            item.update(action="skip", problem="no_title")
        elif not ws:
            item.update(action="skip", problem="unknown_workspace")
        else:
            stages = tpl_stage_keys(conn, ws["template"]) or []
            if status and status not in stages:
                item["status"] = stages[0] if stages else status
                item["problem"] = "status_defaulted"
            elif not status:
                item["status"] = stages[0] if stages else ""
            # Any remaining column that matches a term of this vertical is
            # carried across, so a spreadsheet's own columns are not thrown away.
            spec = {s["key"]: s for s in (tpl_get(conn, ws["template"]) or {}).get("terms", [])}
            used = set()
            for group in ORDER_COLUMNS.values():
                used.update(group)
            for key, value in row.items():
                if key in used or not value:
                    continue
                if key in spec:
                    item["terms"][key] = value
        plan.append(item)
    return plan


@app.post("/api/import/preview")
@login_required
def import_preview():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = body()
    kind = d.get("kind")
    if kind not in IMPORT_KINDS:
        raise ApiError("Изберете какво внасяте", 400, code="import_kind")
    raw = d.get("content") or ""
    if not raw.strip():
        raise ApiError("Прикачете CSV файл", 400, code="field_required", info={"field": "file"})
    rows, headers = _csv_rows(raw)
    if not rows:
        raise ApiError("Файлът няма редове с данни", 400, code="import_empty")
    plan = _plan_partners(conn, org, rows) if kind == "partners" else _plan_orders(conn, org, rows)
    return jsonify({
        "kind": kind, "headers": headers, "rows": len(rows), "plan": plan,
        "summary": {a: sum(1 for p in plan if p["action"] == a)
                    for a in ("create", "exists", "skip")},
    })


@app.post("/api/import/commit")
@login_required
def import_commit():
    """Writes only the rows the preview marked as creatable. One transaction: an
    import either lands or it does not."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_role(conn, user["id"], org["id"], "owner", "admin")
    if org["kind"] != "business":
        raise ApiError("Само бизнес акаунт може да внася", 400, code="only_business_creates")
    require_verified(conn, user["id"])
    d = body()
    kind = d.get("kind")
    if kind not in IMPORT_KINDS:
        raise ApiError("Изберете какво внасяте", 400, code="import_kind")
    rows, _headers = _csv_rows(d.get("content") or "")
    if not rows:
        raise ApiError("Файлът няма редове с данни", 400, code="import_empty")

    created, invited, skipped, links = 0, 0, 0, []
    try:
        if kind == "partners":
            for item in _plan_partners(conn, org, rows):
                if item["action"] != "create":
                    skipped += 1
                    continue
                ws_id = conn.execute(
                    "INSERT INTO workspaces (name, template, buyer_org_id, created_by) "
                    "VALUES (?,?,?,?)",
                    (item["workspace"], item["template"], org["id"], user["id"])).lastrowid
                log_event(conn, ws_id, None, user, "buyer", "workspace_created",
                          "Внесено от CSV: „%s“" % item["workspace"], {"name": item["workspace"],
                                                                       "source": "import"})
                created += 1
                if item["email"]:
                    token = _create_invite(conn, ws_id, item["email"])
                    invited += 1
                    if not mailer.enabled() and token:
                        links.append({"email": item["email"],
                                      "link": request.url_root.rstrip("/") + "/?invite=" + token})
        else:
            for item in _plan_orders(conn, org, rows):
                if item["action"] != "create":
                    skipped += 1
                    continue
                ws = conn.execute("SELECT * FROM workspaces WHERE id=? AND buyer_org_id=?",
                                  (item["workspace_id"], org["id"])).fetchone()
                if not ws:
                    skipped += 1
                    continue
                n = conn.execute("SELECT COUNT(*) c FROM orders WHERE workspace_id=?",
                                 (ws["id"],)).fetchone()["c"]
                tpl = tpl_get(conn, ws["template"]) or {}
                ref = "%s-%04d" % (tpl.get("ref_prefix", "ORD"), n + 1)
                oid = conn.execute(
                    "INSERT INTO orders (workspace_id, ref, title, template, status, created_by) "
                    "VALUES (?,?,?,?,?,?)",
                    (ws["id"], ref, item["title"], ws["template"], item["status"],
                     user["id"])).lastrowid
                for key, value in (item["terms"] or {}).items():
                    conn.execute(
                        "INSERT INTO order_terms (order_id, key, value, state, proposed_by_org, "
                        "updated_by) VALUES (?,?,?,'proposed',?,?)",
                        (oid, key, value, org["id"], user["id"]))
                log_event(conn, ws["id"], oid, user, "buyer", "order_created",
                          "Внесено от CSV: %s" % ref, {"ref": ref, "source": "import"})
                created += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"created": created, "invited": invited, "skipped": skipped,
                    "invite_links": links})


# --------------------------------------------------------------------------- #
#  Operations: what broke, and a backup that can actually be restored
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz():
    """Liveness, for whatever watches this from outside.

    Deliberately unauthenticated, because a monitor that needs a session is a
    monitor nobody sets up, and this installation currently has nothing at all
    watching it. Equally deliberately, it says almost nothing: the response is
    the same fixed shape whoever asks, so it cannot be used to learn the
    version, the size, who is signed in, or what has been failing. The one bit
    it does carry is the one a pager needs - whether the database can still be
    read and written.

    503 rather than 200 on failure, so a monitor alerts without being taught
    to parse anything.
    """
    ok = True
    try:
        conn = db.get_db()
        try:
            # A read proves the file is there; the write proves the disk is not
            # full and the lock is not wedged, which is how SQLite actually
            # fails in production. Rolled back, so it leaves nothing behind.
            conn.execute("SELECT 1").fetchone()
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except Exception:
        ok = False
    resp = jsonify({"ok": ok})
    resp.status_code = 200 if ok else 503
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/admin/health")
@login_required
def admin_health():
    """One screen answering: is anything failing, is anything configured, and
    when was the last backup taken."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_instance_admin(conn, user["id"])
    unseen = conn.execute("SELECT COUNT(*) c FROM error_log WHERE seen=0").fetchone()["c"]
    last_err = conn.execute("SELECT at FROM error_log ORDER BY id DESC LIMIT 1").fetchone()
    db_bytes = os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else 0
    up_bytes = 0
    for root, _dirs, files in os.walk(UPLOAD_DIR):
        for f in files:
            try:
                up_bytes += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return jsonify({
        "errors": {"unseen": unseen, "last": last_err["at"] if last_err else None},
        "storage": {"database": db_bytes, "uploads": up_bytes},
        "services": {"email": mailer.enabled(), "sms": sms.enabled(),
                     "cloud": cloudstore.enabled(), "qtsp": qtsp.is_ready(
                         org_qtsp_config(conn, org["id"]))},
        "backup": _last_backup(),
        "counts": {
            "users": conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
            "orders": conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
            "events": conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"],
        },
    })


#: What has to be true before real people use this, and how badly it matters.
#: Ordered by consequence, not by how easy each one is to fix.
def preflight(conn, org):
    """Answer one question honestly: is this safe to put in front of customers?"""
    checks = []

    def add(key, ok, level, detail=""):
        checks.append({"key": key, "ok": bool(ok), "level": level, "detail": detail})

    add("debug_off", not app.debug, "blocker",
        "SEAM_DEBUG" if app.debug else "")
    add("secret_persisted", os.path.exists(os.path.join(BASE, ".secret")), "blocker")
    # Behind a proxy Flask sees http; the forwarded header is what tells the truth.
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme).lower()
    is_local = request.host.split(":")[0] in ("127.0.0.1", "localhost")
    # Two checks below cannot be answered on a laptop, so they are exempted
    # there. That exemption is itself reported: a green board read over
    # localhost says nothing about the host this will actually run on, and
    # somebody will otherwise take it for a clearance it never gave.
    # When this fails behind a proxy, the usual cause is not a missing
    # certificate but a missing SEAM_TRUSTED_PROXY: waitress discards the
    # forwarded header, so a correctly terminated HTTPS site looks like plain
    # http from in here. Say which of the two it is rather than leaving the
    # operator to check the certificate they already installed.
    add("https", proto == "https" or is_local, "blocker",
        "localhost" if is_local and proto != "https"
        else "" if proto == "https"
        else ("SEAM_TRUSTED_PROXY" if not TRUSTED_PROXY else proto))
    try:
        import waitress                                     # noqa: F401
        add("wsgi_server", True, "blocker")
    except ImportError:
        add("wsgi_server", False, "blocker", "waitress")

    # A session cookie sent over plain HTTP is a session anyone on the path can
    # take. The flag is decided per request when SEAM_SECURE_COOKIES is "auto",
    # so this reports what the running instance is actually doing.
    secure_on = bool(app.config.get("SESSION_COOKIE_SECURE"))
    add("secure_cookies", secure_on or is_local, "blocker",
        "localhost" if is_local and not secure_on else ("" if secure_on else "SEAM_SECURE_COOKIES"))
    # Turning the picture code off is a legitimate choice behind a VPN and a
    # bad one on the open internet, so it is reported rather than assumed.
    add("captcha_on", captcha_mod.enabled(), "blocker",
        "" if captcha_mod.enabled() else "SEAM_CAPTCHA")

    owner = conn.execute(
        "SELECT user_id FROM memberships WHERE org_id=? AND role='owner'", (org["id"],)).fetchone()
    add("owner_set", bool(owner), "important")
    # The owner is the one account that cannot be removed by anyone else, which
    # makes it the one worth a second factor.
    add("owner_2fa", bool(owner) and totp_enabled(conn, owner["user_id"]), "important")
    add("backup_taken", _last_backup() is not None, "important")
    add("backup_offsite", cloudstore.enabled(), "important")
    # A plain archive is fine on a disk you control. Pushed to somebody else's
    # object storage it is the whole database in a place you do not own, so the
    # passphrase stops being optional the moment off-site upload is switched on.
    add("backup_encrypted", backup_is_encrypted() or not cloudstore.enabled(),
        "important" if cloudstore.enabled() else "optional",
        "" if backup_is_encrypted() else "SEAM_BACKUP_PASSPHRASE")
    # The files that would undo everything above if they were readable.
    creds = [f for f in (".secret", ".data_key", ".vapid_keys", ".ref_keys",
                         ".qtsp_config", ".eid_config", ".ai_config", ".stripe_key")
             if os.path.exists(os.path.join(BASE, f))]
    loose = [f for f in creds if cryptobox.is_secured(os.path.join(BASE, f)) is False]
    add("keys_locked_down", not loose, "important", ", ".join(loose))
    add("email", mailer.enabled(), "important")
    add("company_verified", bool(org["verified_company"]), "important")

    # Taking money without a way to confirm it is worse than not taking it: the
    # customer pays, nothing activates, and the only trace is a claim waiting
    # for someone to notice. If a processor is configured, its webhook must be
    # too.
    needs_hook = payment_provider() in ("stripe", "link")
    has_hook = bool(stripe_webhook_secret()) if stripe_key() else bool(lemon_secret())
    add("payment_webhook", has_hook or not needs_hook, "important",
        "" if has_hook or not needs_hook else
        ("STRIPE_WEBHOOK_SECRET" if stripe_key() else "SEAM_LEMON_WEBHOOK_SECRET"))

    add("sms", sms.enabled(), "optional")
    add("qtsp", qtsp.is_ready(org_qtsp_config(conn, org["id"])), "optional")
    # Without a renderer on the server, "Download PDF" falls back to the
    # browser's print dialogue. Everything still works; the file just is not
    # handed over ready to attach, so this is worth knowing before go-live.
    add("pdf_export", pdfout.available(), "optional",
        "" if pdfout.available() else "chromium")
    add("demo_data_cleared", not conn.execute(
        "SELECT 1 FROM users WHERE email LIKE '%@seam.demo'").fetchone(), "optional")

    blockers = [c for c in checks if c["level"] == "blocker" and not c["ok"]]
    important = [c for c in checks if c["level"] == "important" and not c["ok"]]
    # Checks that only passed because this is a laptop. `ready` stays true so
    # the board is usable during development, but it is never true *and*
    # unqualified: a run over localhost has not tested the transport, and the
    # screen has to say which answers were not really given.
    exempt = [c["key"] for c in checks if c["detail"] == "localhost"]
    return {"checks": checks, "ready": not blockers,
            "blockers": len(blockers), "important": len(important),
            "untested_here": exempt, "host": request.host}


@app.get("/api/admin/preflight")
@login_required
def admin_preflight():
    conn, user = g.conn, g.user
    host = require_instance_admin(conn, user["id"])
    # The checks are about the installation, so they are read against the
    # company that runs it, not against whichever company is asking.
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (host,)).fetchone()
    return jsonify(preflight(conn, db.row_to_dict(org)))


@app.get("/api/admin/errors")
@login_required
def admin_errors():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_instance_admin(conn, user["id"])
    rows = conn.execute(
        "SELECT e.*, u.email AS user_email FROM error_log e LEFT JOIN users u ON u.id=e.user_id "
        "ORDER BY e.id DESC LIMIT 100").fetchall()
    return jsonify({"errors": [{
        "id": r["id"], "at": r["at"], "path": r["path"], "method": r["method"],
        "kind": r["kind"], "message": r["message"], "traceback": r["traceback"],
        "user": r["user_email"] or "", "ip": r["ip"], "device": r["device"],
        "seen": bool(r["seen"])} for r in rows]})


@app.post("/api/admin/errors/seen")
@login_required
def admin_errors_seen():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_instance_admin(conn, user["id"])
    conn.execute("UPDATE error_log SET seen=1 WHERE seen=0")
    conn.commit()
    return jsonify({"ok": True})


BACKUP_DIR = os.path.join(BASE, "backups")
BACKUP_KEEP = 14


def _last_backup():
    try:
        files = sorted(f for f in os.listdir(BACKUP_DIR) if ".zip" in f)
    except OSError:
        return None
    if not files:
        return None
    path = os.path.join(BACKUP_DIR, files[-1])
    return {"name": files[-1], "bytes": os.path.getsize(path),
            # Read off the file itself rather than off the current setting: what
            # matters is whether *this* archive is sealed, and the passphrase
            # may have been added or removed since it was written.
            "encrypted": files[-1].endswith(".enc"),
            "at": datetime.utcfromtimestamp(os.path.getmtime(path)).isoformat() + "Z"}


#: Set SEAM_BACKUP_PASSPHRASE to encrypt the archive. Without it the archive is
#: a plain ZIP, which is fine on a disk you control and not fine in someone
#: else's object storage - so the go-live checks say so when cloud upload is on.
BACKUP_PASSPHRASE = os.environ.get("SEAM_BACKUP_PASSPHRASE", "").strip()
BACKUP_MAGIC = b"SEAMBK01"


def backup_is_encrypted():
    return bool(BACKUP_PASSPHRASE)


def make_backup():
    """A consistent copy of the whole instance.

    The database is copied through SQLite's own online backup API rather than by
    reading the file: with WAL, copying the file while a write is in flight
    yields an archive that restores to a torn state.

    What is deliberately *not* in here is the key material - `.secret`,
    `.vapid_keys`, `.data_key` and the provider credentials. It used to be, for
    the convenience of a restore that kept sessions alive, and that made one
    stolen archive a complete compromise: the session-signing key travelling
    next to the database it signs sessions for. Keys are the operator's to keep
    somewhere else; a backup is for the data.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    encrypted = backup_is_encrypted()
    name = "seam-backup-%s.zip%s" % (stamp, ".enc" if encrypted else "")
    path = os.path.join(BACKUP_DIR, name)

    tmpdir = tempfile.mkdtemp(prefix="seam-bk-")
    snapshot = os.path.join(tmpdir, "seam.db")
    zip_path = os.path.join(tmpdir, "archive.zip") if encrypted else path
    try:
        src = sqlite3.connect(db.DB_PATH)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)                  # consistent even under concurrent writes
        finally:
            dst.close()
            src.close()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot, "seam.db")
            for root, _dirs, files in os.walk(UPLOAD_DIR):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, os.path.join("uploads",
                                               os.path.relpath(full, UPLOAD_DIR)).replace("\\", "/"))
            z.writestr("RESTORE.txt",
                       "Seam backup %s\n\n"
                       "Contents: seam.db and uploads/. No keys.\n\n"
                       "Restore: stop Seam, put seam.db back next to app.py, restore\n"
                       "uploads/, then start Seam again.\n\n"
                       "The key files (.secret, .vapid_keys, .data_key and any provider\n"
                       "credentials) are NOT in this archive, on purpose: an archive that\n"
                       "carries the key that signs sessions is a complete compromise if it\n"
                       "is ever lost. Keep them somewhere else, and keep them - without\n"
                       ".data_key the second-factor secrets and the calendar, signing and\n"
                       "reference links in this database cannot be read back.\n" % stamp)

        if encrypted:
            with open(zip_path, "rb") as f:
                blob = f.read()
            salt = secrets.token_bytes(16)
            key = cryptobox.derive(BACKUP_PASSPHRASE, salt)
            with open(path, "wb") as f:
                f.write(BACKUP_MAGIC + salt + cryptobox.seal(key, blob, BACKUP_MAGIC))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    cryptobox.secure_file(path)

    # Keep a rolling window; an unbounded backup folder is its own outage.
    files = sorted(f for f in os.listdir(BACKUP_DIR)
                   if f.endswith(".zip") or f.endswith(".zip.enc"))
    for old in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass
    return path, name


@app.post("/api/admin/backup")
@login_required
def admin_backup():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_instance_admin(conn, user["id"])
    if rate_limited("backup:" + str(org["id"]), 6, 3600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    path, name = make_backup()
    size = os.path.getsize(path)
    out = {"name": name, "bytes": size, "cloud": None,
           "encrypted": backup_is_encrypted()}
    if cloudstore.enabled() and body().get("to_cloud"):
        with open(path, "rb") as f:
            ok, detail = cloudstore.put_now("backups/" + name, f.read(), "application/zip")
        out["cloud"] = detail if ok else ("error: " + detail)
    return jsonify(out)


@app.get("/api/admin/backup/<name>")
@login_required
def admin_backup_download(name):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_instance_admin(conn, user["id"])
    if not re.match(r"^seam-backup-[0-9TZ]+\.zip(\.enc)?$", name):
        raise ApiError("Не е намерено", 404, code="not_found")
    return send_from_directory(BACKUP_DIR, name, as_attachment=True)


def _backup_daily():
    """Optional unattended backup. Off unless SEAM_BACKUP_DAILY=1, because a
    surprise write to disk is not a good default."""
    while True:
        try:
            time.sleep(24 * 3600)
            path, name = make_backup()
            if cloudstore.enabled():
                with open(path, "rb") as f:
                    cloudstore.put_now("backups/" + name, f.read(), "application/zip")
        except Exception as e:
            log_error(e, kind="backup")


def _agreements_daily():
    """Once a day, look at every open agreement and say what changed.

    On by default, unlike the backup: this sends nothing to anyone outside the
    instance and writes nothing to disk, and an installation that quietly never
    tells anybody about a missed deadline is worse than one that does. Set
    SEAM_AGREEMENT_SCAN=off to stop it.

    The pass is idempotent, so a restart in the middle of the day costs at most
    a delay, never a duplicate.
    """
    while True:
        try:
            time.sleep(6 * 3600)          # four looks a day; each says nothing twice
            with db.db_cursor() as conn:
                run_agreement_alerts(conn)
                run_reminders(conn)
        except Exception as e:
            log_error(e, kind="agreements")


@app.get("/api/team")
@login_required
def team_list():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    rows = conn.execute(
        "SELECT u.id, u.name, u.email, u.verified, u.created_at, u.job_title, "
        "u.avatar, m.role FROM users u "
        "JOIN memberships m ON m.user_id=u.id WHERE m.org_id=? ORDER BY u.id",
        (org["id"],)).fetchall()
    return jsonify({
        "members": [{"id": r["id"], "name": r["name"], "email": r["email"],
                     "role": r["role"] if r["role"] in ROLES else "member",
                     "verified": bool(r["verified"]), "since": r["created_at"],
                     "title": r["job_title"] or "",
                     "avatar": ("/uploads/profile/%s" % r["avatar"]) if r["avatar"] else "",
                     "is_me": r["id"] == user["id"]} for r in rows],
        "roles": list(ROLES),
        "my_role": org_role(conn, user["id"], org["id"]),
    })


@app.post("/api/team")
@login_required
def team_invite():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = body()
    email = (d.get("email") or "").strip().lower()[:160]
    name = (d.get("name") or "").strip()[:120]
    role = d.get("role") if d.get("role") in ("admin", "member", "viewer") else "member"
    if not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
        raise ApiError("Невалиден имейл", 400, code="bad_email")
    if len(name) < 2:
        raise ApiError("Въведете име", 400, code="field_required", info={"field": "name"})

    existing = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        already = conn.execute("SELECT 1 FROM memberships WHERE user_id=? AND org_id=?",
                               (existing["id"], org["id"])).fetchone()
        if already:
            raise ApiError("Този човек вече е в екипа", 400, code="already_member")
        other = conn.execute("SELECT 1 FROM memberships WHERE user_id=?",
                             (existing["id"],)).fetchone()
        if other:
            raise ApiError("Този имейл вече принадлежи на друга организация", 400,
                           code="email_taken")
        uid = existing["id"]
    else:
        # No password is set here: the colleague chooses their own through the
        # link, so it is never known to the person who invited them.
        uid = conn.execute(
            "INSERT INTO users (email, name, password_hash, verified, lang) VALUES (?,?,?,1,?)",
            (email, name, generate_password_hash(secrets.token_urlsafe(32)), req_lang())
        ).lastrowid

    conn.execute("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,?)",
                 (uid, org["id"], role))
    token = secrets.token_urlsafe(32)
    # Same storage format as the password-reset flow, which parses it strictly.
    expires = (datetime.utcnow() + timedelta(seconds=RESET_TTL)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE users SET reset_token=?, reset_expires=? WHERE id=?",
                 (db.token_hash(token), expires, uid))
    conn.commit()

    link = request.url_root.rstrip("/") + "/?reset=" + token
    if mailer.enabled():
        lang = req_lang() if req_lang() in RESET_EMAIL_I18N else "en"
        L = RESET_EMAIL_I18N[lang]
        html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b">'
                "<p>%s</p><p>%s</p>%s</div>"
                % (html_escape("%s · %s" % (dl(org["name"]), user["name"])),
                   html_escape(L["l"]), _email_button(link, L["b"])))
        mailer.send_async(email, "Seam · " + dl(org["name"]), html)
    return jsonify(dict(_team_payload(conn, org["id"], user["id"]),
                        invite_link=None if mailer.enabled() else link))


def _team_payload(conn, org_id, me):
    rows = conn.execute(
        "SELECT u.id, u.name, u.email, u.verified, u.created_at, m.role FROM users u "
        "JOIN memberships m ON m.user_id=u.id WHERE m.org_id=? ORDER BY u.id", (org_id,)).fetchall()
    return {"members": [{"id": r["id"], "name": r["name"], "email": r["email"],
                         "role": r["role"] if r["role"] in ROLES else "member",
                         "verified": bool(r["verified"]), "since": r["created_at"],
                         "is_me": r["id"] == me} for r in rows],
            "roles": list(ROLES), "my_role": org_role(conn, me, org_id)}


@app.patch("/api/team/<int:uid>")
@login_required
def team_role(uid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_role(conn, user["id"], org["id"], "owner", "admin")
    role = body().get("role")
    if role not in ("admin", "member", "viewer"):
        raise ApiError("Непозната роля", 400, code="bad_role")
    target = org_role(conn, uid, org["id"])
    if target is None:
        raise ApiError("Не е намерено", 404, code="not_found")
    if target == "owner":
        raise ApiError("Собственикът не може да бъде понижен", 400, code="owner_protected")
    if uid == user["id"]:
        raise ApiError("Не можете да променяте собствената си роля", 400, code="self_role")
    conn.execute("UPDATE memberships SET role=? WHERE user_id=? AND org_id=?",
                 (role, uid, org["id"]))
    conn.commit()
    return jsonify(_team_payload(conn, org["id"], user["id"]))


@app.delete("/api/team/<int:uid>")
@login_required
def team_remove(uid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_role(conn, user["id"], org["id"], "owner", "admin")
    target = org_role(conn, uid, org["id"])
    if target is None:
        raise ApiError("Не е намерено", 404, code="not_found")
    if target == "owner":
        raise ApiError("Собственикът не може да бъде премахнат", 400, code="owner_protected")
    if uid == user["id"]:
        raise ApiError("Не можете да премахнете себе си", 400, code="self_remove")
    # The membership goes; the person and everything they did stays, because the
    # audit trail must not develop holes when someone leaves.
    conn.execute("DELETE FROM memberships WHERE user_id=? AND org_id=?", (uid, org["id"]))
    conn.commit()
    return jsonify(_team_payload(conn, org["id"], user["id"]))


@app.get("/api/notify/settings")
@login_required
def notify_settings_get():
    """What this user gets, on which channel, and what the instance can send."""
    conn, user = g.conn, g.user
    row = conn.execute("SELECT phone, notify_json FROM users WHERE id=?", (user["id"],)).fetchone()
    cfg = sms.config()
    devices = conn.execute("SELECT COUNT(*) c FROM push_subscriptions WHERE user_id=?",
                           (user["id"],)).fetchone()["c"]
    return jsonify({
        "channels": user_channels(row),
        "phone": (row["phone"] if row else "") or "",
        "available": {"email": mailer.enabled(), "sms": sms.enabled(), "push": True},
        "sms": sms.public_config(cfg),
        "sms_providers": list(sms.PROVIDERS),
        "push": {"key": webpush.public_key(), "devices": devices},
    })


@app.put("/api/notify/settings")
@login_required
def notify_settings_put():
    conn, user = g.conn, g.user
    d = body()
    row = conn.execute("SELECT phone, notify_json FROM users WHERE id=?", (user["id"],)).fetchone()
    ch = user_channels(row)
    for k in NOTIFY_CHANNELS:
        if k in (d.get("channels") or {}):
            ch[k] = bool(d["channels"][k])
    phone = (d.get("phone") or "").strip()[:32]
    if phone and not re.match(r"^\+?[0-9 ()\-]{6,32}$", phone):
        raise ApiError("Невалиден телефонен номер", 400, code="bad_phone")
    conn.execute("UPDATE users SET notify_json=?, phone=? WHERE id=?",
                 (json.dumps(ch, ensure_ascii=False), phone, user["id"]))
    conn.commit()
    return jsonify({"channels": ch, "phone": phone})


# --------------------------------------------------------------------------- #
#  Calendar (roadmap item 12: calendars)
#
#  A subscribable iCalendar feed rather than an OAuth integration: Google
#  Calendar, Outlook, Apple Calendar and Thunderbird all take a URL, refresh it
#  themselves, and need no app review or client secret. The URL carries a
#  per-user token, so it can be revoked without touching the password.
# --------------------------------------------------------------------------- #
CAL_I18N = {
    "en": {"review": "agreement review", "name": "Seam · deadlines", "agreed": "agreed", "proposed": "proposed",
           "approval": "approval outstanding", "approval_d": "%d step(s) still open",
           "payout": "Payment due"},
    "bg": {"review": "преразглеждане на договорка", "name": "Seam · срокове", "agreed": "договорено", "proposed": "предложено",
           "approval": "чакащо одобрение", "approval_d": "%d отворени стъпки",
           "payout": "Падеж на плащане"},
    "de": {"review": "Überprüfung der Vereinbarung", "name": "Seam · Fristen", "agreed": "vereinbart", "proposed": "vorgeschlagen",
           "approval": "Freigabe offen", "approval_d": "%d Stufe(n) offen",
           "payout": "Zahlung fällig"},
    "ro": {"review": "reanalizarea înțelegerii", "name": "Seam · termene", "agreed": "convenit", "proposed": "propus",
           "approval": "aprobare în așteptare", "approval_d": "%d etapă(e) deschise",
           "payout": "Plată scadentă"},
    "el": {"review": "επανεξέταση συμφωνίας", "name": "Seam · προθεσμίες", "agreed": "συμφωνημένο", "proposed": "προτεινόμενο",
           "approval": "εκκρεμεί έγκριση", "approval_d": "%d βήμα(τα) ανοικτά",
           "payout": "Πληρωμή προς εξόφληση"},
    "tr": {"review": "mutabakat gözden geçirmesi", "name": "Seam · son tarihler", "agreed": "mutabık", "proposed": "önerilen",
           "approval": "onay bekliyor", "approval_d": "%d adım açık",
           "payout": "Ödeme vadesi"},
    "it": {"review": "revisione dell'accordo", "name": "Seam · scadenze", "agreed": "concordato", "proposed": "proposto",
           "approval": "approvazione in sospeso", "approval_d": "%d fase/i aperte",
           "payout": "Pagamento in scadenza"},
    "ru": {"review": "пересмотр договорённости", "name": "Seam · сроки", "agreed": "согласовано", "proposed": "предложено",
           "approval": "ожидает согласования", "approval_d": "%d этап(ов) открыто",
           "payout": "Срок платежа"},
    "es": {"review": "revisión del acuerdo", "name": "Seam · vencimientos", "agreed": "acordado", "proposed": "propuesto",
           "approval": "aprobación pendiente", "approval_d": "%d paso(s) abiertos",
           "payout": "Pago pendiente"},
    "fr": {"review": "réexamen de l'accord", "name": "Seam · échéances", "agreed": "convenu", "proposed": "proposé",
           "approval": "validation en attente", "approval_d": "%d étape(s) ouvertes",
           "payout": "Paiement à échéance"},
    "pl": {"review": "przegląd ustalenia", "name": "Seam · terminy", "agreed": "uzgodnione", "proposed": "zaproponowane",
           "approval": "oczekuje zatwierdzenia", "approval_d": "%d krok(ów) otwartych",
           "payout": "Termin płatności"},
    "uk": {"review": "перегляд домовленості", "name": "Seam · терміни", "agreed": "погоджено", "proposed": "запропоновано",
           "approval": "очікує погодження", "approval_d": "%d крок(ів) відкрито",
           "payout": "Термін оплати"},
    "pt": {"review": "revisão do acordo", "name": "Seam · prazos", "agreed": "acordado", "proposed": "proposto",
           "approval": "aprovação pendente", "approval_d": "%d etapa(s) em aberto",
           "payout": "Pagamento a vencer"},
}


def _ics_escape(text):
    return (str(text or "").replace("\\", "\\\\").replace(";", r"\;")
            .replace(",", r"\,").replace("\n", r"\n"))


def _ics_fold(line):
    """RFC 5545 caps a content line at 75 octets; continuations start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 73:
        return line
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 73:
            out.append(cur.decode("utf-8"))
            cur = b" " + b
        else:
            cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def _ics_date(value):
    """Accept the date shapes the app actually stores; None if unusable."""
    s = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if " " in s else s, fmt)
        except ValueError:
            continue
    return None


def calendar_token(conn, user_id, create=False):
    """A calendar feed URL has to stay the same, or every subscription breaks -
    so this one is kept encrypted as well as hashed. The hash is what the feed
    request is matched against; the encrypted copy is what lets the screen show
    the URL again, and it is useless to anyone holding only the database."""
    row = conn.execute("SELECT cal_token, cal_token_enc FROM users WHERE id=?",
                       (user_id,)).fetchone()
    tok = cryptobox.decrypt_field(row["cal_token_enc"]) if row else None
    if not tok and create:
        tok = secrets.token_urlsafe(24)
        conn.execute("UPDATE users SET cal_token=?, cal_token_enc=? WHERE id=?",
                     (db.token_hash(tok), cryptobox.encrypt_field(tok), user_id))
        conn.commit()
    return tok


# --------------------------------------------------------------------------- #
#  Cloud storage (roadmap item 12: cloud storage)
#
#  Push a workspace's record out to storage the company already owns, so the
#  audit trail survives Seam itself. S3-compatible or WebDAV; the archive is a
#  plain ZIP a person can open without this app.
# --------------------------------------------------------------------------- #
@app.get("/api/cloud")
@login_required
def cloud_get():
    conn, user = g.conn, g.user
    store_org(conn, user)
    return jsonify({"cloud": cloudstore.public_config(cloudstore.config()),
                    "providers": list(cloudstore.PROVIDERS)})


@app.post("/api/cloud")
@login_required
def cloud_save():
    conn, user = g.conn, g.user
    store_org(conn, user)
    d = body()
    provider = (d.get("provider") or "").strip()
    if not provider:
        try:
            os.remove(cloudstore.CONFIG_FILE)
        except OSError:
            pass
        return jsonify({"cloud": None})
    if provider not in cloudstore.PROVIDERS:
        raise ApiError("Непознат доставчик", 400, code="cloud_provider")
    old = cloudstore.config() or {}
    cfg = {"provider": provider}
    for k in ("endpoint", "region", "bucket", "prefix", "access_key", "secret_key",
              "username", "password"):
        v = (d.get(k) or "").strip()
        if not v and k in cloudstore.SECRET_FIELDS:
            v = old.get(k, "")
        if v:
            cfg[k] = v[:300]
    with open(cloudstore.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    return jsonify({"cloud": cloudstore.public_config(cloudstore.config())})


def _workspace_archive(conn, ws_id, user_id):
    """A self-contained ZIP: the orders, terms, audit trail, signatures and
    payouts as JSON and CSV, plus every uploaded file."""
    ws, side = workspace_access(conn, ws_id, user_id)
    orders = conn.execute("SELECT * FROM orders WHERE workspace_id=? ORDER BY id",
                          (ws_id,)).fetchall()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        payload = {"workspace": {"id": ws["id"], "name": dl(ws["name"]),
                                 "template": ws["template"], "created_at": ws["created_at"]},
                   "exported_at": datetime.utcnow().isoformat() + "Z", "orders": []}
        for o in orders:
            terms = [{"key": t_["key"],
                      "label": tpl_field_label(conn, o["template"], t_["key"]),
                      "value": dl(t_["value"] or ""), "state": t_["state"],
                      "updated_at": t_["updated_at"]}
                     for t_ in conn.execute(
                         "SELECT * FROM order_terms WHERE order_id=? ORDER BY key",
                         (o["id"],)).fetchall()]
            events = [{"at": e["created_at"], "kind": e["kind"], "summary": e["summary"],
                       "ip": e["ip"], "device": e["device"]}
                      for e in conn.execute(
                          "SELECT * FROM events WHERE order_id=? ORDER BY id", (o["id"],)).fetchall()]
            sigs = [{"title": s["doc_title"], "level": s["level"], "status": s["status"],
                     "signer": s["signer_name"], "signed_at": s["signed_at"],
                     "sha256": s["doc_hash"]}
                    for s in conn.execute("SELECT * FROM signatures WHERE order_id=?",
                                          (o["id"],)).fetchall()]
            payload["orders"].append({
                "ref": o["ref"], "title": dl(o["title"]), "status": o["status"],
                "created_at": o["created_at"], "terms": terms,
                "events": events, "signatures": sigs})
            for a in conn.execute("SELECT * FROM attachments WHERE order_id=?",
                                  (o["id"],)).fetchall():
                path = os.path.join(UPLOAD_DIR, str(o["id"]), a["filename"])
                if os.path.exists(path):
                    z.write(path, "files/%s/%s" % (o["ref"], a["original_name"]))
        z.writestr("workspace.json", json.dumps(payload, ensure_ascii=False, indent=1))
        rows = ["ref;title;status;created_at"]
        for o in payload["orders"]:
            rows.append(";".join(str(o[k]).replace(";", ",") for k in
                                 ("ref", "title", "status", "created_at")))
        z.writestr("orders.csv", "﻿" + "\r\n".join(rows))
        z.writestr("README.txt",
                   "Seam export of workspace '%s'.\nOpens without Seam: workspace.json holds the "
                   "records and the audit trail, files/ holds the uploads.\n"
                   % dl(ws["name"]))
    return buf.getvalue(), dl(ws["name"])


# --------------------------------------------------------------------------- #
#  Taking your data out, and being erased from it
#
#  Two rights that exist in most of the countries Seam serves, and one real
#  conflict between them and the rest of this system.
#
#  The conflict: the audit trail is append-only, and that is the whole point of
#  it - a record of who agreed to what, when, that neither side can quietly
#  revise. Deleting a person's rows would tear holes in exactly the evidence a
#  dispute turns on. Deleting nothing would make the right to erasure a
#  pretence.
#
#  The answer is pseudonymisation. The person goes: name, e-mail, phone,
#  signature image, devices and addresses. What they did stays, attributed to a
#  number. A contract remains provable; the human being is no longer in the
#  database.
# --------------------------------------------------------------------------- #
def _account_export(conn, user, org):
    """Everything held about one company and the people in it, in a form that
    opens without Seam."""
    uid, org_id = user["id"], (org or {}).get("id")
    ws_rows = conn.execute(
        "SELECT * FROM workspaces WHERE buyer_org_id=? OR partner_org_id=?",
        (org_id, org_id)).fetchall() if org_id else []

    people = []
    for m in conn.execute(
            "SELECT u.*, m.role FROM users u JOIN memberships m ON m.user_id=u.id "
            "WHERE m.org_id=?", (org_id,)).fetchall() if org_id else []:
        people.append({
            "id": m["id"], "name": m["name"], "email": m["email"], "role": m["role"],
            "language": m["lang"] or "", "phone": m["phone"] or "",
            "verified": bool(m["verified"]), "created_at": m["created_at"],
            # Deliberately absent: password hash, second-factor secret, session
            # ids, recovery codes. An export is not a way to lift credentials.
            "two_factor": totp_enabled(conn, m["id"]),
            "electronic_identities": [
                {"provider": r["provider"], "label": r["label"] or "",
                 "attached_at": r["created_at"]}
                for r in conn.execute("SELECT * FROM user_eid WHERE user_id=?",
                                      (m["id"],)).fetchall()],
            "sessions": [
                {"ip": r["ip"], "device": r["device"], "started": r["created_at"],
                 "last_seen": r["last_seen"]}
                for r in conn.execute(
                    "SELECT * FROM user_sessions WHERE user_id=? AND revoked_at IS NULL",
                    (m["id"],)).fetchall()],
            "notifications": [
                {"at": r["created_at"], "kind": r["kind"], "read": bool(r["read"])}
                for r in conn.execute(
                    "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 500",
                    (m["id"],)).fetchall()],
        })

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        company = {
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "company": {
                "name": dl(org["name"]), "kind": org["kind"],
                "registration_number": org["reg_number"] or "",
                "country": org["country"] or "", "plan": org["plan"],
                "verified": bool(org["verified_company"]),
                "created_at": org["created_at"],
                "contacts": _org_contacts(org),
            } if org else None,
            "people": people,
            "relationships": [{"id": w["id"], "name": dl(w["name"]),
                               "template": w["template"], "status": w["status"],
                               "created_at": w["created_at"]} for w in ws_rows],
        }
        z.writestr("company.json", json.dumps(company, ensure_ascii=False, indent=1))

        # Each relationship exported whole, reusing the archive a company can
        # already take for one of them.
        for w in ws_rows:
            try:
                data, name = _workspace_archive(conn, w["id"], uid)
            except ApiError:
                continue
            z.writestr("relationships/%d-%s.zip" % (w["id"], _slug(name)), data)

        if org_id:
            z.writestr("network.json", json.dumps({
                "reference": reference_facts(conn, org_id),
                "needs": [{"title": dl(r["title"]), "detail": dl(r["detail"] or ""),
                           "status": r["status"], "created_at": r["created_at"]}
                          for r in conn.execute("SELECT * FROM needs WHERE org_id=?",
                                                (org_id,)).fetchall()],
            }, ensure_ascii=False, indent=1))

        z.writestr("README.txt",
                   "Seam export for %s, taken %s.\n\n"
                   "company.json  - the company, the people in it and what is held about them\n"
                   "relationships/ - one archive per counterparty: records, agreed terms,\n"
                   "                 the audit trail, signatures and every uploaded file\n"
                   "network.json  - your published reference and your own posts\n\n"
                   "Not included, on purpose: password hashes, second-factor secrets,\n"
                   "session identifiers and recovery codes. An export is a copy of your\n"
                   "data, not a way to lift credentials out of the system.\n"
                   % ((dl(org["name"]) if org else "-"),
                      datetime.utcnow().strftime("%Y-%m-%d")))
    return buf.getvalue()


def _slug(text):
    out = re.sub(r"[^A-Za-z0-9]+", "-", str(text or "")).strip("-")
    return (out or "relationship")[:40]


@app.get("/api/account/export")
@login_required
def account_export():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_role(conn, user["id"], org["id"], "owner", "admin") if org else None
    if rate_limited("export:%d" % user["id"], 5, 3600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    data = _account_export(conn, user, org)
    stamp = datetime.utcnow().strftime("%Y%m%d")
    return app.response_class(data, mimetype="application/zip", headers={
        "Content-Disposition": 'attachment; filename="seam-export-%s.zip"' % stamp})


def pseudonymise_user(conn, uid):
    """Remove the person, keep what they did.

    Everything that identifies a human being goes. The events they wrote stay,
    attributed to a number, because the other company's ability to prove what
    was agreed does not end when one person leaves.
    """
    tag = "deleted-user-%d" % uid
    conn.execute(
        "UPDATE users SET name=?, email=?, phone=NULL, notify_json=NULL, lang=NULL, "
        "password_hash=?, verified=0, verify_token=NULL, reset_token=NULL, "
        "reset_expires=NULL, cal_token=NULL, cal_token_enc=NULL WHERE id=?",
        (tag, "%s@deleted.invalid" % tag, generate_password_hash(secrets.token_urlsafe(32)), uid))
    for table in ("user_sessions", "user_totp", "user_recovery_codes", "user_eid",
                  "push_subscriptions", "notifications"):
        try:
            conn.execute("DELETE FROM %s WHERE user_id=?" % table, (uid,))
        except sqlite3.Error:
            pass
    # The audit trail keeps the act and loses the actor's traces.
    conn.execute("UPDATE events SET ip=NULL, device=NULL WHERE actor_user_id=?", (uid,))
    conn.execute("UPDATE signatures SET signer_name=?, signer_email=NULL, "
                 "signature_img=NULL, ip=NULL, device=NULL WHERE requested_by=?",
                 (tag, uid))
    conn.execute("DELETE FROM memberships WHERE user_id=?", (uid,))


@app.post("/api/account/close")
@login_required
def account_close():
    """Erase the person. The password is asked for again because this cannot be
    undone and whoever is at the keyboard is not necessarily whoever signed in."""
    conn, user = g.conn, g.user
    d = body()
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not check_password_hash(row["password_hash"], d.get("password") or ""):
        raise ApiError("Грешна парола", 403, code="bad_password")
    org = user_primary_org(conn, user["id"])
    if org:
        role = org_role(conn, user["id"], org["id"])
        others = conn.execute(
            "SELECT COUNT(*) c FROM memberships WHERE org_id=? AND user_id<>?",
            (org["id"], user["id"])).fetchone()["c"]
        # An owner leaving a company with nobody else in it would strand the
        # company and its counterparties. Hand it over first.
        if role == "owner" and others:
            raise ApiError("Първо предайте собствеността на друг член", 400,
                           code="owner_must_hand_over")
    pseudonymise_user(conn, user["id"])
    conn.commit()
    session.clear()
    return jsonify({"closed": True})


@app.get("/api/account/close-effect")
@login_required
def account_close_effect():
    """What closing would actually do, said before it is done."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    others = conn.execute(
        "SELECT COUNT(*) c FROM memberships WHERE org_id=? AND user_id<>?",
        (org["id"], user["id"])).fetchone()["c"] if org else 0
    role = org_role(conn, user["id"], org["id"]) if org else None
    ws = conn.execute(
        "SELECT COUNT(*) c FROM workspaces WHERE buyer_org_id=? OR partner_org_id=?",
        (org["id"], org["id"])).fetchone()["c"] if org else 0
    return jsonify({"role": role, "colleagues": others, "relationships": ws,
                    "last_owner": role == "owner" and others == 0,
                    "must_hand_over": role == "owner" and others > 0})


@app.get("/api/workspaces/<int:ws_id>/archive")
@login_required
def workspace_archive(ws_id):
    conn, user = g.conn, g.user
    data, name = _workspace_archive(conn, ws_id, user["id"])
    stamp = datetime.utcnow().strftime("%Y%m%d")
    return app.response_class(data, mimetype="application/zip", headers={
        "Content-Disposition": 'attachment; filename="seam-%d-%s.zip"' % (ws_id, stamp)})


@app.post("/api/workspaces/<int:ws_id>/archive/cloud")
@login_required
def workspace_archive_cloud(ws_id):
    """Same archive, pushed to the configured storage."""
    conn, user = g.conn, g.user
    if not cloudstore.enabled():
        raise ApiError("Няма конфигурирано хранилище", 400, code="cloud_config")
    if rate_limited("cloud:" + str(user["id"]), 10, 600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    data, _name = _workspace_archive(conn, ws_id, user["id"])
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    ok, detail = cloudstore.put_now("workspace-%d/%s.zip" % (ws_id, stamp), data,
                                    "application/zip")
    if not ok:
        raise ApiError("Качването не успя: %s" % detail, 502, code="cloud_failed")
    return jsonify({"uploaded": True, "detail": detail, "bytes": len(data)})


@app.post("/api/cloud/test")
@login_required
def cloud_test():
    conn, user = g.conn, g.user
    store_org(conn, user)
    if rate_limited("cloudtest:" + str(user["id"]), 5, 600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    ok, detail = cloudstore.put_now(
        "seam-connection-test.txt",
        "Seam connection test %s\n" % datetime.utcnow().isoformat(), "text/plain")
    if not ok:
        raise ApiError("Връзката не успя: %s" % detail, 502, code="cloud_failed")
    return jsonify({"ok": True, "detail": detail})


# --------------------------------------------------------------------------- #
#  Banking (roadmap item 12: banking)
#
#  Read a statement the bank already produces, match it against what is
#  outstanding, and write the transfer file the bank already accepts. No PSD2
#  registration, no per-bank contract, and no claim to hold either.
# --------------------------------------------------------------------------- #
@app.post("/api/banking/statement")
@login_required
def banking_statement():
    """Upload CAMT.053 or MT940 and see what it settles. Read-only: nothing is
    marked paid until the user confirms the matches."""
    conn, user = g.conn, g.user
    my_orgs = set(user_org_ids(conn, user["id"]))
    if not my_orgs:
        raise ApiError("Нямате организация", 400, code="no_org")
    f = request.files.get("file")
    raw = f.read() if f else (body().get("content") or "").encode("utf-8")
    if not raw:
        raise ApiError("Прикачете файл с извлечение", 400, code="field_required",
                       info={"field": "file"})
    if len(raw) > banking.MAX_STATEMENT_BYTES:
        raise ApiError("Файлът е твърде голям", 400, code="file_too_large")
    try:
        fmt, txs = banking.parse_statement(raw)
    except banking.StatementError as e:
        raise ApiError(str(e), 400, code="statement_parse")

    ph = ",".join("?" * len(my_orgs))
    rows = conn.execute(
        "SELECT * FROM payouts WHERE payer_org_id IN (%s) AND status='due' ORDER BY id" % ph,
        list(my_orgs)).fetchall()
    payouts = [{"id": r["id"], "status": r["status"], "reference": r["reference"] or "",
                "amount": r["amount"], "description": dl(r["description"] or ""),
                "workspace_id": r["workspace_id"]} for r in rows]
    matches = banking.match_payouts(
        txs, payouts, amount_of=lambda p: banking._dec(_price_eur(p["amount"])))
    by_id = {p["id"]: p for p in payouts}
    for m in matches:
        m["payout"] = by_id.get(m["payout_id"])
    return jsonify({
        "format": fmt, "transactions": len(txs),
        "credits": sum(1 for t in txs if t["credit"]),
        "matched": sum(1 for m in matches if m["payout_id"]),
        "matches": matches,
        "outstanding": len(payouts),
    })


@app.post("/api/banking/reconcile")
@login_required
def banking_reconcile():
    """Confirm the matches - this is what actually marks payouts as settled."""
    conn, user = g.conn, g.user
    my_orgs = set(user_org_ids(conn, user["id"]))
    ids = [int(x) for x in (body().get("payout_ids") or []) if str(x).isdigit()][:200]
    # No default sentence: a note written here in one language would be read by
    # a counterparty in another. The reconcile screen already says what it did.
    note = (body().get("note") or "").strip()[:200]
    done = 0
    for pid in ids:
        r = conn.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
        if not r or r["payer_org_id"] not in my_orgs or r["status"] != "due":
            continue
        conn.execute("UPDATE payouts SET status='paid', paid_by=?, paid_at=datetime('now'), "
                     "note=? WHERE id=?", (user["id"], note, pid))
        ws = conn.execute("SELECT * FROM workspaces WHERE id=?", (r["workspace_id"],)).fetchone()
        side = "buyer" if ws["buyer_org_id"] in my_orgs else "partner"
        log_event(conn, r["workspace_id"], r["order_id"], user, side, "payout_recorded",
                  "Разплатено по банково извлечение: %s" % r["amount"],
                  {"amount": r["amount"], "status": "paid", "source": "bank"})
        notify_org(conn, r["payee_org_id"], r["workspace_id"], r["order_id"], "payout_recorded",
                   "Плащане %s · paid" % r["amount"], exclude_user=user["id"],
                   meta={"amount": r["amount"], "status": "paid"})
        done += 1
    conn.commit()
    return jsonify({"reconciled": done})


@app.post("/api/banking/sepa")
@login_required
def banking_sepa():
    """Build a pain.001 credit transfer file from the selected outstanding
    payouts, ready to upload to the bank's own portal."""
    conn, user = g.conn, g.user
    my_orgs = set(user_org_ids(conn, user["id"]))
    d = body()
    debtor = (d.get("debtor_name") or "").strip()[:70]
    iban = (d.get("debtor_iban") or "").strip()
    if not debtor:
        org = user_primary_org(conn, user["id"])
        debtor = org["name"] if org else "Seam"
    ids = [int(x) for x in (d.get("payout_ids") or []) if str(x).isdigit()][:200]
    if not ids:
        raise ApiError("Изберете поне едно плащане", 400, code="sepa_no_items")

    accounts = d.get("accounts") or {}
    payments = []
    for pid in ids:
        r = conn.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
        if not r or r["payer_org_id"] not in my_orgs or r["status"] != "due":
            continue
        payee = conn.execute("SELECT name FROM orgs WHERE id=?", (r["payee_org_id"],)).fetchone()
        acct = accounts.get(str(pid)) or accounts.get(str(r["payee_org_id"])) or {}
        payments.append({
            "name": dl(payee["name"]) if payee else "",
            "iban": (acct.get("iban") or "").strip(),
            "bic": (acct.get("bic") or "").strip(),
            "amount": "%.2f" % _price_eur(r["amount"]),
            "currency": (r["amount"].split()[0] if " " in r["amount"] else "EUR"),
            "reference": r["reference"] or dl(r["description"] or ""),
            "end_to_end": "SEAM-%d" % r["id"],
        })
    if not payments:
        raise ApiError("Няма подходящи плащания", 400, code="sepa_no_items")
    try:
        xml, msg_id = banking.build_pain001(debtor, iban, (d.get("debtor_bic") or "").strip(),
                                            payments)
    except banking.StatementError as e:
        raise ApiError(str(e), 400, code="sepa_invalid")
    return app.response_class(xml, mimetype="application/xml", headers={
        "Content-Disposition": 'attachment; filename="%s.xml"' % msg_id})


@app.post("/api/banking/check-iban")
@login_required
def banking_check_iban():
    return jsonify({"valid": banking.valid_iban(body().get("iban"))})


@app.get("/api/calendar")
@login_required
def calendar_info():
    conn, user = g.conn, g.user
    tok = calendar_token(conn, user["id"], create=True)
    return jsonify({"url": request.url_root.rstrip("/") + "/calendar/%s.ics" % tok,
                    "token": tok})


@app.post("/api/calendar/rotate")
@login_required
def calendar_rotate():
    """Revoke the old feed URL - the only way to un-share a subscribed link."""
    conn, user = g.conn, g.user
    tok = secrets.token_urlsafe(24)
    conn.execute("UPDATE users SET cal_token=?, cal_token_enc=? WHERE id=?",
                 (db.token_hash(tok), cryptobox.encrypt_field(tok), user["id"]))
    conn.commit()
    return jsonify({"url": request.url_root.rstrip("/") + "/calendar/%s.ics" % tok,
                    "token": tok})


@app.get("/calendar/<token>.ics")
def calendar_feed(token):
    """Deadlines from every workspace this user belongs to: dated order terms,
    payout due dates and outstanding approvals."""
    conn = db.get_db()
    try:
        u = conn.execute("SELECT * FROM users WHERE cal_token=?",
                         (db.token_hash(token),)).fetchone()
        if not u:
            return "", 404
        lang = u["lang"] if u["lang"] in SUPPORTED_LANGS else "en"
        g._cal_lang = lang
        my_orgs = set(user_org_ids(conn, u["id"]))
        if not my_orgs:
            my_orgs = {0}
        ph = ",".join("?" * len(my_orgs))
        ws_rows = conn.execute(
            "SELECT * FROM workspaces WHERE buyer_org_id IN (%s) OR partner_org_id IN (%s)"
            % (ph, ph), list(my_orgs) + list(my_orgs)).fetchall()
        ws_ids = [w["id"] for w in ws_rows]
        L = CAL_I18N.get(lang, CAL_I18N["en"])

        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Seam//Operational layer//EN",
                 "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                 "X-WR-CALNAME:" + _ics_escape(L["name"]),
                 "X-WR-TIMEZONE:UTC", "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
                 "X-PUBLISHED-TTL:PT1H"]
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        base = request.url_root.rstrip("/")

        def event(uid, day, summary, description, url):
            lines.extend([
                "BEGIN:VEVENT",
                "UID:%s@seam" % uid,
                "DTSTAMP:" + stamp,
                "DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"),
                "DTEND;VALUE=DATE:" + (day + timedelta(days=1)).strftime("%Y%m%d"),
                _ics_fold("SUMMARY:" + _ics_escape(summary)),
                _ics_fold("DESCRIPTION:" + _ics_escape(description)),
                "URL:" + url,
                "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY",
                _ics_fold("DESCRIPTION:" + _ics_escape(summary)), "END:VALARM",
                "END:VEVENT"])

        if ws_ids:
            wph = ",".join("?" * len(ws_ids))
            for o in conn.execute(
                    "SELECT o.*, w.name AS ws_name FROM orders o JOIN workspaces w ON w.id=o.workspace_id "
                    "WHERE o.workspace_id IN (%s)" % wph, ws_ids).fetchall():
                spec = {s["key"]: s for s in (tpl_get(conn, o["template"]) or {}).get("terms", [])}
                # Every column: the review date is computed from agreed_at and
                # review_every_days, and selecting three columns by name meant
                # reaching for the other two raised inside the feed.
                for t_ in conn.execute("SELECT * FROM order_terms WHERE order_id=?",
                                       (o["id"],)).fetchall():
                    label = tpl_field_label(conn, o["template"], t_["key"])

                    if (spec.get(t_["key"]) or {}).get("type") == "date":
                        day = _ics_date(t_["value"])
                        if day:
                            state = L["agreed"] if t_["state"] == "agreed" else L["proposed"]
                            event("term-%d-%s" % (o["id"], t_["key"]), day,
                                  "%s · %s" % (o["ref"], label),
                                  "%s\n%s · %s" % (dl(o["title"]), dl(o["ws_name"]), state),
                                  "%s/#/o/%d" % (base, o["id"]))

                    # A review is not a date term. A price agreed a year ago is
                    # exactly the kind of thing that wants another look, so this
                    # sits outside the date check rather than under it.
                    if t_["state"] == "agreed" and t_["review_every_days"]:
                        due = agreements.next_review(
                            t_["agreed_at"] or t_["updated_at"],
                            t_["review_every_days"], datetime.utcnow().date())
                        if due:
                            event("review-%d-%s" % (o["id"], t_["key"]),
                                  datetime(due.year, due.month, due.day),
                                  "%s · %s" % (o["ref"], L["review"]),
                                  "%s\n%s · %s" % (dl(o["title"]), dl(o["ws_name"]), label),
                                  "%s/#/o/%d" % (base, o["id"]))

                pend = conn.execute(
                    "SELECT COUNT(*) c FROM approvals WHERE order_id=? AND status='pending'",
                    (o["id"],)).fetchone()["c"]
                if pend:
                    day = _ics_date(o["updated_at"]) or datetime.utcnow()
                    event("appr-%d" % o["id"], day + timedelta(days=3),
                          "%s · %s" % (o["ref"], L["approval"]),
                          "%s\n%s" % (dl(o["title"]), L["approval_d"] % pend),
                          "%s/#/o/%d" % (base, o["id"]))

            for p in conn.execute(
                    "SELECT * FROM payouts WHERE workspace_id IN (%s) AND status='due'" % wph,
                    ws_ids).fetchall():
                day = _ics_date(p["due_date"])
                if not day:
                    continue
                payee = conn.execute("SELECT name FROM orgs WHERE id=?",
                                     (p["payee_org_id"],)).fetchone()
                event("payout-%d" % p["id"], day,
                      "%s · %s" % (L["payout"], p["amount"]),
                      "%s\n%s" % (dl(p["description"] or ""), dl(payee["name"]) if payee else ""),
                      "%s/#/w/%d" % (base, p["workspace_id"]))

        lines.append("END:VCALENDAR")
        body_txt = "\r\n".join(lines) + "\r\n"
        return app.response_class(body_txt, mimetype="text/calendar; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="seam.ics"',
            "Cache-Control": "no-cache"})
    finally:
        conn.close()


@app.get("/api/notify/push-key")
@login_required
def push_key():
    """The instance's VAPID public key, which the browser needs to subscribe."""
    return jsonify({"key": webpush.public_key()})


@app.post("/api/notify/push-subscribe")
@login_required
def push_subscribe():
    conn, user = g.conn, g.user
    d = body()
    endpoint = (d.get("endpoint") or "").strip()
    if not endpoint.startswith("https://"):
        raise ApiError("Невалиден адрес за push", 400, code="push_endpoint")
    keys_ = d.get("keys") or {}
    conn.execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, device) VALUES (?,?,?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, "
        "auth=excluded.auth, device=excluded.device",
        (user["id"], endpoint[:500], str(keys_.get("p256dh") or "")[:200],
         str(keys_.get("auth") or "")[:100], _device_label()))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) c FROM push_subscriptions WHERE user_id=?",
                     (user["id"],)).fetchone()["c"]
    return jsonify({"subscribed": True, "devices": n})


@app.post("/api/notify/push-unsubscribe")
@login_required
def push_unsubscribe():
    conn, user = g.conn, g.user
    endpoint = (body().get("endpoint") or "").strip()
    if endpoint:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint=? AND user_id=?",
                     (endpoint, user["id"]))
    else:
        conn.execute("DELETE FROM push_subscriptions WHERE user_id=?", (user["id"],))
    conn.commit()
    return jsonify({"subscribed": False})


@app.post("/api/notify/push-test")
@login_required
def push_test():
    conn, user = g.conn, g.user
    subs = conn.execute("SELECT * FROM push_subscriptions WHERE user_id=?",
                        (user["id"],)).fetchall()
    if not subs:
        raise ApiError("Няма регистрирано устройство за push", 400, code="push_none")
    if rate_limited("pushtest:" + str(user["id"]), 5, 600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    subject = "mailto:" + (os.environ.get("SEAM_PUSH_CONTACT") or "admin@localhost")
    sent, detail = 0, ""
    for s in subs:
        ok, d_ = webpush.send_now(s["endpoint"], subject)
        conn.execute("UPDATE push_subscriptions SET last_sent=datetime('now'), last_status=? "
                     "WHERE id=?", (("ok " + d_) if ok else ("error " + d_), s["id"]))
        if ok:
            sent += 1
        else:
            detail = d_
            if d_ in ("404", "410"):
                conn.execute("DELETE FROM push_subscriptions WHERE id=?", (s["id"],))
    conn.commit()
    if not sent:
        raise ApiError("Push не беше доставен: %s" % detail, 502, code="push_failed")
    return jsonify({"sent": sent})


SMTP_TEST_I18N = {
    "en": {"s": "Seam: mail is working", "b": "If you are reading this, Seam can send mail from this installation. Invitations, password resets and signing links will reach people."},
    "bg": {"s": "Seam: пощата работи", "b": "Ако четете това, Seam може да изпраща поща от тази инсталация. Поканите, възстановяването на парола и линковете за подпис ще стигат до хората."},
    "de": {"s": "Seam: E-Mail funktioniert", "b": "Wenn Sie das lesen, kann Seam von dieser Installation aus E-Mails versenden. Einladungen, Passwort-Zurücksetzungen und Signaturlinks erreichen die Empfänger."},
    "ro": {"s": "Seam: e-mailul funcționează", "b": "Dacă citiți acest mesaj, Seam poate trimite e-mailuri din această instalare. Invitațiile, resetările de parolă și linkurile de semnare vor ajunge la destinatari."},
    "el": {"s": "Seam: η αλληλογραφία λειτουργεί", "b": "Αν το διαβάζετε αυτό, το Seam μπορεί να στέλνει μηνύματα από αυτή την εγκατάσταση. Οι προσκλήσεις, οι επαναφορές κωδικού και οι σύνδεσμοι υπογραφής θα φτάνουν στους παραλήπτες."},
    "tr": {"s": "Seam: e-posta çalışıyor", "b": "Bunu okuyorsanız Seam bu kurulumdan e-posta gönderebiliyor. Davetler, parola sıfırlamaları ve imza bağlantıları kişilere ulaşacak."},
    "it": {"s": "Seam: la posta funziona", "b": "Se sta leggendo questo, Seam può inviare posta da questa installazione. Inviti, reimpostazioni della password e link di firma arriveranno ai destinatari."},
    "ru": {"s": "Seam: почта работает", "b": "Если вы это читаете, Seam может отправлять почту с этой установки. Приглашения, восстановление пароля и ссылки для подписи будут доходить до людей."},
    "es": {"s": "Seam: el correo funciona", "b": "Si está leyendo esto, Seam puede enviar correo desde esta instalación. Las invitaciones, los restablecimientos de contraseña y los enlaces de firma llegarán a sus destinatarios."},
    "fr": {"s": "Seam : la messagerie fonctionne", "b": "Si vous lisez ceci, Seam peut envoyer des messages depuis cette installation. Les invitations, les réinitialisations de mot de passe et les liens de signature parviendront à leurs destinataires."},
    "pl": {"s": "Seam: poczta działa", "b": "Jeśli to czytasz, Seam może wysyłać pocztę z tej instalacji. Zaproszenia, resetowanie hasła i linki do podpisu dotrą do odbiorców."},
    "uk": {"s": "Seam: пошта працює", "b": "Якщо ви це читаєте, Seam може надсилати листи з цієї інсталяції. Запрошення, відновлення пароля та посилання для підпису доходитимуть до людей."},
    "pt": {"s": "Seam: o correio funciona", "b": "Se está a ler isto, o Seam consegue enviar correio a partir desta instalação. Os convites, as reposições de palavra-passe e as ligações de assinatura chegarão aos destinatários."},
}


# --------------------------------------------------------------------------- #
#  The shelf
#
#  Paperwork, kept. Two holes closed at once: a document generated here was
#  never stored (the URL rebuilt it, so losing the link lost the document), and
#  one that arrived from outside had nowhere to go, because `attachments` takes
#  photos and video - progress evidence, not paperwork. Most of what passes
#  between two companies is a PDF.
#
#  A document can sit at three levels: the company's own (a licence, an
#  insurance certificate), the relationship (a framework contract, an NDA), or
#  one order. `shared` is what decides whether the counterparty sees it, and it
#  is off unless asked for.
# --------------------------------------------------------------------------- #
DOC_DIR = os.path.join(UPLOAD_DIR, "docs")
#: What a company actually exchanges. Deliberately a list rather than "anything
#: that is not executable": an allow-list fails closed when a new dangerous
#: format appears, a deny-list fails open.
DOC_MIME = {
    "pdf": "application/pdf", "txt": "text/plain", "csv": "text/csv",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "rtf": "application/rtf", "xml": "application/xml", "zip": "application/zip",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "heic": "image/heic",
}
DOC_MAX = 25 * 1024 * 1024
DOC_TITLE_MAX = 160


def _doc_row(r, mine=True):
    return {"id": r["id"], "title": r["title"], "name": r["original_name"],
            "kind": r["doc_kind"] or "", "source": r["source"],
            "size": r["size"], "mime": r["mime"], "sha256": (r["sha256"] or "")[:16],
            "shared": bool(r["shared"]), "note": r["note"] or "",
            "workspace_id": r["workspace_id"], "order_id": r["order_id"],
            "at": r["created_at"], "mine": mine,
            "url": "/files/%d/%s" % (r["id"], r["original_name"])}


def _doc_visible(conn, uid, doc):
    """Who may open a filed document.

    Anyone in the company that filed it. Anyone on the other side of the
    workspace it belongs to, but only once it has been shared - a company's own
    certificates are its own business until it says otherwise. And anyone who
    may read the profile it was hung on as evidence: publishing a licence under
    a project is the act of showing it.
    """
    if org_role(conn, uid, doc["org_id"]) is not None:
        return True
    if conn.execute(
            "SELECT 1 FROM portfolio_docs pd JOIN portfolio p ON p.id = pd.item_id "
            "WHERE pd.doc_id=? AND p.visible=1 LIMIT 1", (doc["id"],)).fetchone():
        if _profile_readable(conn, uid, doc["org_id"]):
            return True
    if not doc["workspace_id"] or not doc["shared"]:
        return False
    try:
        workspace_access(conn, doc["workspace_id"], uid)
        return True
    except ApiError:
        return False


@app.get("/api/files")
@login_required
def files_list():
    """The shelf. Narrowed by workspace or order when asked for, otherwise
    everything this company can see."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        return jsonify({"files": []})
    ws = request.args.get("workspace_id")
    order = request.args.get("order_id")
    rows, seen = [], set()
    own_sql = "SELECT * FROM documents WHERE org_id=?"
    args = [org["id"]]
    if ws and str(ws).isdigit():
        own_sql += " AND workspace_id=?"
        args.append(int(ws))
    if order and str(order).isdigit():
        own_sql += " AND order_id=?"
        args.append(int(order))
    for r in conn.execute(own_sql + " ORDER BY id DESC LIMIT 400", args).fetchall():
        rows.append(_doc_row(r))
        seen.add(r["id"])
    # What the other side has shared into a workspace this company is in.
    theirs = ("SELECT d.* FROM documents d JOIN workspaces w ON w.id=d.workspace_id "
              "WHERE d.shared=1 AND d.org_id<>? AND (w.buyer_org_id=? OR w.partner_org_id=?)")
    targs = [org["id"], org["id"], org["id"]]
    if ws and str(ws).isdigit():
        theirs += " AND d.workspace_id=?"
        targs.append(int(ws))
    if order and str(order).isdigit():
        theirs += " AND d.order_id=?"
        targs.append(int(order))
    for r in conn.execute(theirs + " ORDER BY d.id DESC LIMIT 400", targs).fetchall():
        if r["id"] not in seen:
            rows.append(_doc_row(r, mine=False))
    rows.sort(key=lambda x: x["id"], reverse=True)
    return jsonify({"files": rows, "accepts": sorted(DOC_MIME), "max_mb": DOC_MAX // (1024 * 1024)})


@app.post("/api/files")
@login_required
def files_upload():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    ws_id = request.args.get("workspace_id")
    if ws_id and str(ws_id).isdigit():
        workspace_access(conn, int(ws_id), user["id"])   # 403 from outside
        ws_id = int(ws_id)
    else:
        ws_id = None
    order_id = request.args.get("order_id")
    order_id = int(order_id) if order_id and str(order_id).isdigit() else None
    shared = (request.args.get("shared") or "").lower() in ("1", "true", "on", "yes")
    if shared and not ws_id:
        # Shared with whom? Without a workspace there is no other side, and a
        # flag that means nothing is a flag somebody will trust.
        raise ApiError("Споделянето изисква работно пространство", 400, code="share_no_ws")

    files = request.files.getlist("files")
    if not files:
        raise ApiError("Липсва файл", 400, code="field_required", info={"field": "files"})
    if (request.content_length or 0) > DOC_MAX * 4:
        raise ApiError("Файлът е твърде голям", 400, code="doc_too_big")
    os.makedirs(os.path.join(DOC_DIR, str(org["id"])), exist_ok=True)
    out = []
    for f in files[:10]:
        orig = (f.filename or "file")[:180]
        ext = orig.rsplit(".", 1)[-1].lower() if "." in orig else ""
        if ext not in DOC_MIME:
            raise ApiError("Неподдържан формат: .%s" % (ext or "?"), 400, code="doc_type")
        fname = secrets.token_hex(12) + "." + ext
        path = os.path.join(DOC_DIR, str(org["id"]), fname)
        f.save(path)
        size = os.path.getsize(path)
        if size > DOC_MAX:
            os.remove(path)
            raise ApiError("Файлът е твърде голям", 400, code="doc_too_big")
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        cur = conn.execute(
            "INSERT INTO documents (org_id, workspace_id, order_id, source, doc_kind, title, "
            "filename, original_name, mime, size, sha256, note, shared, uploaded_by) "
            "VALUES (?,?,?,'upload',?,?,?,?,?,?,?,?,?,?)",
            (org["id"], ws_id, order_id, (request.args.get("kind") or "")[:40],
             (request.form.get("title") or orig)[:DOC_TITLE_MAX], fname, orig,
             DOC_MIME[ext], size, h.hexdigest(),
             (request.form.get("note") or "")[:400], 1 if shared else 0, user["id"]))
        out.append(_doc_row(conn.execute("SELECT * FROM documents WHERE id=?",
                                         (cur.lastrowid,)).fetchone()))
    if ws_id:
        for r in out:
            log_system_event(conn, ws_id, r["order_id"], user["id"], "document_filed",
                             "Документ в архива: %s" % r["title"],
                             {"title": r["title"], "shared": r["shared"]})
    return jsonify({"files": out})


@app.post("/api/files/archive")
@login_required
def files_archive_generated():
    """Keep a document that was just generated.

    Rendered to PDF once and filed, rather than kept as a link that rebuilds
    it: a template changes, a price list changes, and the invoice sent last
    March has to stay the invoice sent last March.
    """
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    d = body()
    path = (d.get("path") or "").strip()
    if not path.startswith("/docs/") or "\n" in path or "\r" in path:
        raise ApiError("Невалиден документ", 400, code="invalid_action")
    if not pdfout.available():
        raise ApiError("Няма визуализатор за PDF на този сървър", 400, code="pdf_unavailable")
    kind = path.split("/docs/", 1)[1].split("?", 1)[0]
    if kind not in DOC_KINDS:
        raise ApiError("Невалиден документ", 400, code="invalid_action")
    html = _archive_render(conn, user, org, path)
    if html is None:
        raise ApiError("Документът не можа да се създаде", 400, code="invalid_action")
    data = pdfout.render(html)
    if not data:
        raise ApiError("Документът не можа да се създаде", 502, code="pdf_unavailable")
    os.makedirs(os.path.join(DOC_DIR, str(org["id"])), exist_ok=True)
    fname = secrets.token_hex(12) + ".pdf"
    with open(os.path.join(DOC_DIR, str(org["id"]), fname), "wb") as fh:
        fh.write(data)
    title = (d.get("title") or kind)[:DOC_TITLE_MAX]
    # The file name a person saves. A title that is all Cyrillic leaves nothing
    # once non-ASCII is stripped, so the document kind stands in rather than a
    # name that begins with a dash.
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-.")[:80] or kind
    ws_id = d.get("workspace_id") if str(d.get("workspace_id") or "").isdigit() else None
    if ws_id:
        workspace_access(conn, int(ws_id), user["id"])
    cur = conn.execute(
        "INSERT INTO documents (org_id, workspace_id, order_id, invoice_id, source, doc_kind, "
        "title, filename, original_name, mime, size, sha256, lang, note, shared, uploaded_by) "
        "VALUES (?,?,?,?,'generated',?,?,?,?,'application/pdf',?,?,?,?,?,?)",
        (org["id"], ws_id, d.get("order_id"), d.get("invoice_id"), kind, title, fname,
         ascii_name + ".pdf", len(data),
         hashlib.sha256(data).hexdigest(), (d.get("lang") or req_lang())[:5],
         (d.get("note") or "")[:400], 1 if (d.get("shared") and ws_id) else 0, user["id"]))
    return jsonify(_doc_row(conn.execute("SELECT * FROM documents WHERE id=?",
                                         (cur.lastrowid,)).fetchone()))


def _archive_render(conn, user, org, path):
    """Render a /docs/... page to HTML in this person's own context.

    Done by calling the view rather than fetching over the network: no second
    round trip, no credentials on the wire, and no way to archive a document
    the person could not have opened, because the same guards run.
    """
    from urllib.parse import urlsplit
    parts = urlsplit(path)
    kind = parts.path.split("/docs/", 1)[1]
    with app.test_request_context(parts.path + ("?" + parts.query if parts.query else "")):
        session["uid"] = user["id"]
        session["sid"] = "archive"
        g.conn = conn
        g.user = user
        g._archiving = True
        try:
            out = doc_page(kind)
        except Exception:
            return None
    if isinstance(out, tuple):
        out = out[0]
    return out if isinstance(out, str) else None


@app.patch("/api/files/<int:fid>")
@login_required
def files_update(fid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    doc = conn.execute("SELECT * FROM documents WHERE id=? AND org_id=?",
                       (fid, org["id"] if org else 0)).fetchone()
    if not doc:
        raise ApiError("Не е намерено", 404, code="not_found")
    d = body()
    shared = doc["shared"]
    if "shared" in d:
        if d["shared"] and not doc["workspace_id"]:
            raise ApiError("Споделянето изисква работно пространство", 400, code="share_no_ws")
        shared = 1 if d["shared"] else 0
    conn.execute("UPDATE documents SET title=?, note=?, shared=? WHERE id=?",
                 ((d.get("title") or doc["title"])[:DOC_TITLE_MAX],
                  (d.get("note") if "note" in d else doc["note"] or "")[:400],
                  shared, fid))
    return jsonify(_doc_row(conn.execute("SELECT * FROM documents WHERE id=?", (fid,)).fetchone()))


@app.delete("/api/files/<int:fid>")
@login_required
def files_delete(fid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    doc = conn.execute("SELECT * FROM documents WHERE id=? AND org_id=?",
                       (fid, org["id"] if org else 0)).fetchone()
    if not doc:
        raise ApiError("Не е намерено", 404, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin", "member")
    try:
        os.remove(os.path.join(DOC_DIR, str(doc["org_id"]), doc["filename"]))
    except OSError:
        pass
    conn.execute("DELETE FROM documents WHERE id=?", (fid,))
    return jsonify({"ok": True})


@app.get("/files/<int:fid>/<path:name>")
def serve_file(fid, name):
    """The file itself.

    The name in the URL is decoration for whoever saves it; what is served is
    decided by the id and the guard. A path is never built from anything the
    caller typed.
    """
    conn = db.get_db()
    try:
        uid = session.get("uid")
        if not uid:
            return "", 401
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (fid,)).fetchone()
        if not doc or not _doc_visible(conn, uid, doc):
            # The same answer either way: whether a document exists is itself
            # something worth not telling a stranger.
            return "", 404
        resp = make_response(send_from_directory(
            os.path.join(DOC_DIR, str(doc["org_id"])), doc["filename"]))
        resp.headers["Content-Type"] = doc["mime"]
        resp.headers["Content-Disposition"] = (
            "attachment; filename=\"%s\"; filename*=UTF-8''%s"
            % (re.sub(r"[^A-Za-z0-9._-]+", "-", doc["original_name"])[:80],
               urllib.parse.quote(doc["original_name"], safe="")))
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        resp.headers["Cache-Control"] = "no-store, private"
        return resp
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Getting paid
#
#  An invoice used to be a page that was generated and forgotten. Now it is a
#  record, which is what lets three things happen: it can chase itself, it can
#  notice the money arriving on the bank statement, and - because when the
#  customer is also here the same row is the seller's receivable and the
#  buyer's obligation - it can say afterwards how fast this company actually
#  pays. That last one is the part no accounting package can do, because none
#  of them holds both sides of the trade.
# --------------------------------------------------------------------------- #
def _inv_row(r, mine=True):
    outstanding = round(float(r["gross"] or 0) - float(r["paid_amount"] or 0), 2)
    overdue = 0
    if r["status"] in ("sent", "part") and r["due_date"]:
        try:
            overdue = max(0, (datetime.utcnow().date()
                              - datetime.strptime(r["due_date"], "%Y-%m-%d").date()).days)
        except ValueError:
            overdue = 0
    return {"id": r["id"], "number": r["number"], "pay_ref": r["pay_ref"],
            "kind": r["kind"], "customer": r["customer"],
            "customer_email": r["customer_email"] or "",
            "currency": r["currency"], "net": r["net"], "vat_rate": r["vat_rate"],
            "vat_amount": r["vat_amount"], "gross": r["gross"],
            "paid_amount": r["paid_amount"], "outstanding": outstanding,
            "issue_date": r["issue_date"], "due_date": r["due_date"] or "",
            "status": r["status"], "overdue_days": overdue,
            "reminders": r["reminders"], "last_reminder": r["last_reminder"] or "",
            "sent_at": r["sent_at"] or "", "paid_at": r["paid_at"] or "",
            "lang": r["lang"], "mine": mine,
            "money": "%s %.2f" % (r["currency"], r["gross"] or 0)}


def _pay_settings(org):
    k = org.keys() if org else []
    get = lambda c: ((org[c] if c in k else "") or "")            # noqa: E731
    return {"iban": get("pay_iban"), "bic": get("pay_bic"), "bank": get("pay_bank"),
            "terms": get("pay_terms") or "14", "link": get("pay_link"),
            "reminders": receivables.reminder_plan(get("remind_json"))}


@app.get("/api/pay-settings")
@login_required
def pay_settings_get():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    out = _pay_settings(org)
    out["can_edit"] = bool(org) and org_role(conn, user["id"], org["id"]) in ("owner", "admin")
    out["mail_on"] = mailer.enabled()
    return jsonify(out)


@app.put("/api/pay-settings")
@login_required
def pay_settings_set():
    """Where this company is paid, entered once. Every invoice it issues then
    carries it, so nobody retypes an IBAN onto a legal document."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = body()
    iban = re.sub(r"\s+", "", str(d.get("iban") or "")).upper()[:40]
    if iban and not banking.valid_iban(iban):
        # Caught here rather than by the customer's bank a week later.
        raise ApiError("Невалиден IBAN", 400, code="bad_iban")
    plan = d.get("reminders")
    conn.execute(
        "UPDATE orgs SET pay_iban=?, pay_bic=?, pay_bank=?, pay_terms=?, pay_link=?, "
        "remind_json=? WHERE id=?",
        (iban, str(d.get("bic") or "").strip().upper()[:16],
         str(d.get("bank") or "").strip()[:80],
         str(d.get("terms") or "14").strip()[:4],
         str(d.get("link") or "").strip()[:300],
         json.dumps(receivables.reminder_plan(json.dumps(plan)) if plan is not None
                    else receivables.reminder_plan(None)),
         org["id"]))
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (org["id"],)).fetchone()
    return jsonify(_pay_settings(org))


@app.get("/api/invoices")
@login_required
def invoices_list():
    """Both directions. What this company is owed, and what it owes - the
    second only exists because the counterparty issued it here."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        return jsonify({"issued": [], "received": [], "totals": {}})
    issued = [_inv_row(r) for r in conn.execute(
        "SELECT * FROM invoices WHERE org_id=? ORDER BY id DESC LIMIT 300",
        (org["id"],)).fetchall()]
    received = [_inv_row(r, mine=False) for r in conn.execute(
        "SELECT * FROM invoices WHERE customer_org_id=? AND org_id<>? AND status<>'draft' "
        "ORDER BY id DESC LIMIT 300", (org["id"], org["id"])).fetchall()]
    owed = sum(i["outstanding"] for i in issued if i["status"] in ("sent", "part"))
    late = sum(i["outstanding"] for i in issued if i["overdue_days"] > 0)
    owing = sum(i["outstanding"] for i in received if i["status"] in ("sent", "part"))
    return jsonify({"issued": issued, "received": received,
                    "totals": {"owed": round(owed, 2), "overdue": round(late, 2),
                               "owing": round(owing, 2)},
                    "settings": _pay_settings(org)})


@app.post("/api/invoices")
@login_required
def invoice_create():
    """Record what was just generated, so it can be chased and reconciled."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    d = body()
    lines = d.get("lines") if isinstance(d.get("lines"), list) else []
    rc = bool(d.get("reverse_charge"))
    try:
        rate = 0.0 if rc else max(0.0, min(30.0, float(d.get("vat_rate") or 0)))
    except (TypeError, ValueError):
        rate = 0.0
    net, vat, gross = receivables.totals(lines, rate, rc)
    if gross <= 0:
        raise ApiError("Фактура на нулева стойност не се записва", 400, code="invoice_empty")
    ps = _pay_settings(org)
    issue = (d.get("issue_date") or "").strip()[:10] or datetime.utcnow().strftime("%Y-%m-%d")
    due = (d.get("due_date") or "").strip()[:10] or receivables.due_date(issue, ps["terms"])
    number = (d.get("number") or "").strip()[:40] or ("INV-" + issue.replace("-", ""))

    # The customer, matched to a company here when the registration number or
    # the address says so. That link is what makes the punctuality figure
    # possible later; without it the invoice is still perfectly usable.
    cust_email = (d.get("customer_email") or "").strip()[:160]
    cust_reg = (d.get("customer_reg") or "").strip()[:40]
    cust_org = None
    if cust_reg:
        row = conn.execute("SELECT id FROM orgs WHERE reg_number=? AND id<>?",
                           (cust_reg, org["id"])).fetchone()
        cust_org = row["id"] if row else None
    if cust_org is None and cust_email:
        row = conn.execute(
            "SELECT m.org_id AS id FROM users u JOIN memberships m ON m.user_id=u.id "
            "WHERE u.email=? AND m.org_id<>? LIMIT 1", (cust_email.lower(), org["id"])).fetchone()
        cust_org = row["id"] if row else None

    for _ in range(6):
        ref = receivables.make_ref(number, secrets.token_bytes(8))
        try:
            cur = conn.execute(
                "INSERT INTO invoices (org_id, customer_org_id, workspace_id, order_id, "
                "number, pay_ref, kind, customer, customer_reg, customer_vat, customer_email, "
                "customer_addr, lang, currency, net, vat_rate, vat_amount, gross, issue_date, "
                "tax_date, due_date, status, lines_json, query_json, created_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)",
                (org["id"], cust_org, d.get("workspace_id"), d.get("order_id"),
                 number, ref, (d.get("kind") or "invoice")[:20],
                 (d.get("customer") or "-")[:160], cust_reg,
                 (d.get("customer_vat") or "").strip()[:24], cust_email,
                 (d.get("customer_addr") or "").strip()[:200],
                 (d.get("lang") or req_lang())[:5], (d.get("currency") or "EUR")[:3],
                 net, rate, vat, gross, issue, (d.get("tax_date") or "").strip()[:10] or None,
                 due or None, json.dumps(lines, ensure_ascii=False),
                 json.dumps(d.get("query") or {}, ensure_ascii=False), user["id"]))
            break
        except sqlite3.IntegrityError:
            continue                       # the reference collided; draw another
    else:
        raise ApiError("Не можа да се създаде основание за плащане", 500, code="ref_collision")
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(_inv_row(row))


def _money(amount, currency="EUR"):
    return "%s %s" % ("{:,.2f}".format(float(amount or 0)).replace(",", " "),
                      "€" if currency == "EUR" else currency)


def _invoice_letter(conn, inv, kind, days=0):
    """Compose one of the four letters. Returns (subject, html) in the language
    the invoice was written in - the reader's, not the sender's."""
    org = conn.execute("SELECT * FROM orgs WHERE id=?", (inv["org_id"],)).fetchone()
    lang = inv["lang"] if inv["lang"] in MONEY_MAIL else "en"
    L = MONEY_MAIL[lang]
    ps = _pay_settings(org)
    fields = {"seller": org["name"], "customer": inv["customer"], "number": inv["number"],
              "amount": _money(inv["gross"], inv["currency"]),
              "due": inv["due_date"] or "-", "days": days, "ref": inv["pay_ref"]}
    subject = L["%s_s" % kind].format(**fields)
    body_text = L["%s_t" % kind].format(**fields)

    rows = [(L["amount_l"], _money(inv["gross"], inv["currency"]))]
    if inv["due_date"]:
        rows.append((L["due_l"], inv["due_date"]))
    if ps["iban"]:
        rows.append((L["iban_l"], ps["iban"]))
    rows.append((L["ref_l"], inv["pay_ref"]))
    table = "".join(
        '<tr><td style="padding:4px 14px 4px 0;color:#5b6472">%s</td>'
        '<td style="padding:4px 0"><b>%s</b></td></tr>' % (html_escape(a), html_escape(b))
        for a, b in rows)

    pay_block = ""
    if kind != "paid":
        pay_block = ('<h3 style="font-size:14px;margin:22px 0 6px">%s</h3>'
                     '<table style="font-size:14px">%s</table>'
                     '<p style="font-size:12.5px;color:#5b6472">%s</p>'
                     % (html_escape(L["pay_h"]), table, html_escape(L["ref_note"])))
        if ps["link"]:
            pay_block += _email_button(ps["link"], L["card_l"])
    html = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1f2b">'
            "<p>%s</p>%s</div>" % (html_escape(body_text), pay_block))
    return subject, html


def _invoice_send(conn, inv, kind, days=0):
    """Send one letter and write down that it went. Returns True when it left
    the building, so a reminder is never counted for a message that could not
    be delivered."""
    if not inv["customer_email"]:
        return False, "no address"
    if not mailer.enabled():
        return False, "no mail server"
    subject, html = _invoice_letter(conn, inv, kind, days)
    ok, detail = mailer.send_now(inv["customer_email"], subject, html)
    if ok:
        conn.execute("INSERT INTO invoice_events (invoice_id, kind, detail) VALUES (?,?,?)",
                     (inv["id"], kind, inv["customer_email"]))
    return ok, detail


@app.post("/api/invoices/<int:iid>/send")
@login_required
def invoice_send(iid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    inv = conn.execute("SELECT * FROM invoices WHERE id=? AND org_id=?",
                       (iid, org["id"] if org else 0)).fetchone()
    if not inv:
        raise ApiError("Не е намерено", 404, code="not_found")
    to = (body().get("to") or inv["customer_email"] or "").strip()[:160]
    if not to:
        raise ApiError("Няма адрес на получателя", 400, code="field_required",
                       info={"field": "customer_email"})
    if to != inv["customer_email"]:
        conn.execute("UPDATE invoices SET customer_email=? WHERE id=?", (to, iid))
        inv = conn.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
    ok, detail = _invoice_send(conn, inv, "sent")
    if not ok and detail == "no mail server":
        # Marked sent regardless: the operator may have handed it over by other
        # means, and an invoice stuck in draft would never be chased.
        conn.execute("UPDATE invoices SET status='sent', sent_at=datetime('now') "
                     "WHERE id=? AND status='draft'", (iid,))
        conn.commit()
        raise ApiError("Пощата не е настроена", 400, code="smtp_unset")
    if not ok:
        raise ApiError("Писмото не беше изпратено: %s" % str(detail)[:160], 502,
                       code="smtp_failed")
    conn.execute("UPDATE invoices SET status=CASE WHEN status='draft' THEN 'sent' ELSE status END, "
                 "sent_at=COALESCE(sent_at, datetime('now')) WHERE id=?", (iid,))
    conn.commit()
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (iid,)).fetchone()
    return jsonify(_inv_row(row))


def _settle(conn, inv, amount, source):
    """Record money against an invoice, and tell the customer it arrived.

    The receipt is not a courtesy: a customer who is not told stops trusting
    the reminders, and the next one gets ignored."""
    paid = round(float(inv["paid_amount"] or 0) + float(amount or 0), 2)
    full = paid + 0.01 >= float(inv["gross"] or 0)
    conn.execute(
        "UPDATE invoices SET paid_amount=?, status=?, paid_at=CASE WHEN ? THEN "
        "COALESCE(paid_at, datetime('now')) ELSE paid_at END WHERE id=?",
        (paid, "paid" if full else "part", 1 if full else 0, inv["id"]))
    conn.execute("INSERT INTO invoice_events (invoice_id, kind, detail, amount) "
                 "VALUES (?,'paid',?,?)", (inv["id"], source, float(amount or 0)))
    row = conn.execute("SELECT * FROM invoices WHERE id=?", (inv["id"],)).fetchone()
    if full:
        _invoice_send(conn, row, "paid")
        # And inside the platform, both sides and their own software. The
        # customer already gets a receipt by e-mail; the people who were
        # chasing this need to stop chasing it, and their planning system
        # needs to know the money landed.
        # Only the company that issued it. The customer has just been sent a
        # receipt; a second letter saying the same thing in worse words is how
        # a product teaches people to filter its mail. The seller's side is
        # told in the app, and their own systems hear it through the webhook,
        # which is the point of the event.
        meta = {"ref": row["number"], "amount": "%.2f %s" % (float(row["gross"] or 0),
                                                            row["currency"] or "EUR"),
                "source": source}
        if row["org_id"]:
            notify_org(conn, row["org_id"], row["workspace_id"], row["order_id"],
                       "payment_received", "payment_received", meta=meta,
                       channels=("app", "push"))
    return row


@app.post("/api/invoices/<int:iid>/paid")
@login_required
def invoice_mark_paid(iid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    inv = conn.execute("SELECT * FROM invoices WHERE id=? AND org_id=?",
                       (iid, org["id"] if org else 0)).fetchone()
    if not inv:
        raise ApiError("Не е намерено", 404, code="not_found")
    try:
        amount = float(body().get("amount") or (float(inv["gross"]) - float(inv["paid_amount"])))
    except (TypeError, ValueError):
        raise ApiError("Невалидна сума", 400, code="field_required", info={"field": "amount"})
    row = _settle(conn, inv, amount, "manual")
    conn.commit()
    return jsonify(_inv_row(row))


@app.post("/api/invoices/reconcile")
@login_required
def invoices_reconcile():
    """A bank statement in, settled invoices out.

    `preview` first, always: a machine that moves money between accounts on a
    guess is worse than one that asks. Only a reference or an unambiguous
    amount is ever applied, and the ambiguous ones are handed back by name.
    """
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Няма организация", 400, code="not_found")
    require_role(conn, user["id"], org["id"], "owner", "admin", "member")
    d = body()
    raw = d.get("content") or ""
    try:
        # It returns (format, transactions) - the format is what the operator
        # is told the file was read as.
        fmt, txs = banking.parse_statement(raw)
    except Exception as e:
        raise ApiError("Извлечението не можа да се разчете: %s" % str(e)[:120],
                       400, code="statement_parse")
    open_rows = conn.execute(
        "SELECT * FROM invoices WHERE org_id=? AND status IN ('sent','part') ORDER BY id",
        (org["id"],)).fetchall()
    matches = receivables.match_receivables(txs, [dict(r) for r in open_rows])
    by_id = {r["id"]: r for r in open_rows}
    if not d.get("commit"):
        return jsonify({"format": fmt, "transactions": len(txs),
                        "matches": matches, "applied": 0})
    applied = 0
    for m in matches:
        if not m["invoice_id"] or m["settles"] == "over":
            continue
        _settle(conn, by_id[m["invoice_id"]], float(m["amount"]), "statement")
        applied += 1
    conn.commit()
    return jsonify({"format": fmt, "transactions": len(txs),
                    "matches": matches, "applied": applied})


# --------------------------------------------------------------------------- #
#  Agreements that need attention again
#
#  An agreed term is not the end of anything. The date on it comes closer, then
#  arrives, then passes; and a term agreed a year ago may no longer describe
#  the work. None of that announces itself, so this walks the agreements once a
#  day and says so, once, to both companies.
# --------------------------------------------------------------------------- #
def _order_is_open(conn, order):
    """A finished record raises nothing. Chasing a deadline on delivered work
    is how people learn to ignore a system."""
    keys = tpl_stage_keys(conn, order["template"])
    last = keys[-1] if keys else None
    return order["status"] not in ("closed", "cancelled") and order["status"] != last


def scan_agreements(conn, now=None, lead_days=None):
    """[(order, workspace, term, kind, on_date), ...] for today.

    Reads only. Deciding and telling are separate so the decision can be shown
    in the interface without anything being sent.
    """
    today = (now or datetime.utcnow()).date()
    lead = agreements.DEFAULT_LEAD_DAYS if lead_days is None else lead_days
    out = []
    rows = conn.execute(
        "SELECT o.*, w.name AS ws_name, w.buyer_org_id, w.partner_org_id "
        "FROM orders o JOIN workspaces w ON w.id = o.workspace_id "
        "WHERE w.status = 'active'").fetchall()
    for o in rows:
        if not _order_is_open(conn, o):
            continue
        spec = {t["key"]: t for t in (tpl_get(conn, o["template"]) or {}).get("terms", [])}
        for t_ in conn.execute(
                "SELECT * FROM order_terms WHERE order_id=?", (o["id"],)).fetchall():
            is_date = (spec.get(t_["key"]) or {}).get("type") == "date"
            for kind, on_date in agreements.term_alerts(
                    t_["value"] if is_date else None, t_["state"], today, lead,
                    review_every=t_["review_every_days"],
                    agreed_on=t_["agreed_at"] or t_["updated_at"]):
                out.append((o, t_, kind, on_date))
    return out


def run_agreement_alerts(conn, now=None, lead_days=None):
    """Say each of those once. Idempotent: running twice sends nothing twice."""
    sent = 0
    for order, term, kind, on_date in scan_agreements(conn, now, lead_days):
        key = agreements.alert_key(kind, order["id"], term["key"], on_date)
        try:
            conn.execute(
                "INSERT INTO agreement_alerts (alert_key, workspace_id, order_id, "
                "term_key, kind, on_date) VALUES (?,?,?,?,?,?)",
                (key, order["workspace_id"], order["id"], term["key"], kind,
                 on_date.isoformat()))
        except sqlite3.IntegrityError:
            continue                       # already said, and once is enough
        meta = {"ref": order["ref"], "term": term["key"], "date": on_date.isoformat(),
                "tpl": order["template"], "value": term["value"] or ""}
        # Both sides. A deadline belongs to the two companies, not to whoever
        # happened to type it in.
        for org_id in (order["buyer_org_id"], order["partner_org_id"]):
            if org_id:
                notify_org(conn, org_id, order["workspace_id"], order["id"],
                           kind, kind, meta=meta)
        sent += 1
    conn.commit()
    return sent


@app.post("/api/agreements/run")
@login_required
def agreements_run_now():
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    return jsonify({"sent": run_agreement_alerts(conn)})


@app.get("/api/agreements")
@login_required
def agreements_upcoming():
    """What is coming up and what is late, for the reader's own records.

    The horizon is wider than the alert lead time on purpose: an alert is an
    interruption and should be rare, while a list somebody chose to open can
    afford to show the month ahead.
    """
    conn, user = g.conn, g.user
    mine = set(user_org_ids(conn, user["id"]))
    horizon = min(90, max(1, int(request.args.get("days") or 30)))
    out = []
    for order, term, kind, on_date in scan_agreements(conn, lead_days=horizon):
        if order["buyer_org_id"] not in mine and order["partner_org_id"] not in mine:
            continue
        out.append({
            "order_id": order["id"], "ref": order["ref"], "title": dl(order["title"]),
            "workspace_id": order["workspace_id"], "workspace": dl(order["ws_name"]),
            "term": term["key"],
            "term_label": tpl_field_label(conn, order["template"], term["key"]),
            "value": dl(term["value"] or ""), "kind": kind,
            "date": on_date.isoformat(),
            "days": (on_date - datetime.utcnow().date()).days,
        })
    order_of = {k: i for i, k in enumerate(agreements.KINDS)}
    out.sort(key=lambda r: (order_of.get(r["kind"], 9), r["date"]))
    return jsonify({"items": out, "days": horizon})


@app.post("/api/orders/<int:oid>/terms/<key>/review")
@login_required
def term_set_review(oid, key):
    """How often this agreement wants another look. 0 or null turns it off."""
    conn, user = g.conn, g.user
    o = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        raise ApiError("Поръчката не е намерена", 404, code="order_not_found")
    # The same guard the rest of the term endpoints use: membership of one of
    # the two companies on this workspace, and nothing weaker.
    workspace_access(conn, o["workspace_id"], user["id"])
    d = body()
    raw = d.get("every_days")
    every = None if raw in (None, "", 0, "0") else int(raw)
    if every is not None and not (1 <= every <= 3650):
        raise ApiError("Периодът е между 1 и 3650 дни", 400, code="bad_review_period")
    row = conn.execute("SELECT * FROM order_terms WHERE order_id=? AND key=?",
                       (oid, key)).fetchone()
    if not row:
        raise ApiError("Не е намерено", 404, code="not_found")
    conn.execute("UPDATE order_terms SET review_every_days=? WHERE id=?", (every, row["id"]))
    conn.commit()
    return jsonify({"ok": True, "every_days": every})


# --------------------------------------------------------------------------- #
#  What is owed, and what is left
#
#  Revenue is already here, in the invoices this company issued. The costs that
#  decide whether there is a profit are not: rent, leases, wages, fuel,
#  software. Those are entered once and joined to the invoices; taxes.py does
#  the arithmetic and knows nothing about the database.
# --------------------------------------------------------------------------- #
def _months_in(first, last):
    """How many calendar months a period touches, for spreading a recurring cost."""
    return (last.year - first.year) * 12 + (last.month - first.month) + 1


def _expense_total(row, first, last):
    """What one expense contributes to this period.

    A recurring cost is monthly, so it is counted once for each month of the
    period it overlaps: a lease of 400 a month is 1200 in a quarter and not
    400, which is the mistake that makes a quarterly profit look generous.
    """
    amount, vat = float(row["amount"] or 0), float(row["vat_amount"] or 0)
    if not row["recurring"]:
        day = taxes_parse(row["on_date"])
        return (amount, vat) if day and first <= day <= last else (0.0, 0.0)
    starts = taxes_parse(row["starts_on"]) or first
    ends = taxes_parse(row["ends_on"]) or last
    lo, hi = max(first, starts), min(last, ends)
    if lo > hi:
        return (0.0, 0.0)
    n = _months_in(lo, hi)
    return (round(amount * n, 2), round(vat * n, 2))


def taxes_parse(value):
    return agreements.parse_date(value)


def _period_from_request():
    """(first, last, label) from year/month/quarter, defaulting to this year."""
    now = datetime.utcnow().date()
    try:
        year = int(request.args.get("year") or now.year)
    except ValueError:
        year = now.year
    month = request.args.get("month")
    quarter = request.args.get("quarter")
    try:
        month = int(month) if month else None
        quarter = int(quarter) if quarter else None
    except ValueError:
        month = quarter = None
    if month and not 1 <= month <= 12:
        month = None
    if quarter and not 1 <= quarter <= 4:
        quarter = None
    first, last = taxes.period_bounds(year, month, quarter)
    label = ("%04d-%02d" % (year, month) if month else
             ("%04d-Q%d" % (year, quarter) if quarter else str(year)))
    return first, last, label


def finance_figures(conn, org, first, last, basis="accrual"):
    """Everything taxes.compute needs, gathered from this company's own records.

    `basis` decides which day an invoice belongs to: the day it was issued, or
    the day the money arrived. Both are real ways to keep books and the answer
    differs, so the caller says which rather than the code assuming.
    """
    date_col = "issue_date" if basis == "accrual" else "paid_at"
    where = "org_id=? AND status<>'draft' AND %s IS NOT NULL AND date(%s) BETWEEN ? AND ?" % (
        date_col, date_col)
    if basis != "accrual":
        where += " AND status='paid'"
    rows = conn.execute("SELECT net, vat_amount FROM invoices WHERE " + where,
                        (org["id"], first.isoformat(), last.isoformat())).fetchall()
    revenue = round(sum(float(r["net"] or 0) for r in rows), 2)
    vat_out = round(sum(float(r["vat_amount"] or 0) for r in rows), 2)

    costs = {k: 0.0 for k in taxes.COST_KINDS}
    vat_in = 0.0
    for e in conn.execute("SELECT * FROM expenses WHERE org_id=?", (org["id"],)).fetchall():
        amount, vat = _expense_total(e, first, last)
        if not amount and not vat:
            continue
        kind = e["kind"] if e["kind"] in costs else "other"
        costs[kind] = round(costs[kind] + amount, 2)
        vat_in = round(vat_in + vat, 2)

    # Money this company recorded as paid out to its partners is a cost it
    # already told Seam about, so it is not asked for twice.
    payouts = conn.execute(
        "SELECT amount FROM payouts WHERE payer_org_id=? AND status='paid' "
        "AND date(COALESCE(paid_at, due_date)) BETWEEN ? AND ?",
        (org["id"], first.isoformat(), last.isoformat())).fetchall()
    for p in payouts:
        costs["operating"] = round(costs["operating"] + _amount_number(p["amount"]), 2)

    return revenue, vat_out, vat_in, costs, len(rows), len(payouts)


def _amount_number(text):
    """The number out of a stored amount like "EUR 1950" or "1950.00"."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(text or "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else 0.0


@app.get("/api/finance")
@login_required
def finance_report():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_feature(conn, org, "finance")
    first, last, label = _period_from_request()
    basis = "cash" if request.args.get("basis") == "cash" else "accrual"
    revenue, vat_out, vat_in, costs, n_inv, n_pay = finance_figures(
        conn, org, first, last, basis)

    keys = org.keys()
    out = taxes.compute(
        revenue, vat_out, vat_in, costs, org["country"],
        profit_tax=org["tax_profit_rate"] if "tax_profit_rate" in keys else None,
        payroll_employer=org["tax_payroll_rate"] if "tax_payroll_rate" in keys else None,
        vat_exempt=bool(org["vat_exempt"]) if "vat_exempt" in keys else False,
        profit_tax_exempt=bool(org["profit_tax_exempt"]) if "profit_tax_exempt" in keys else False)
    out.update({
        "period": {"from": first.isoformat(), "to": last.isoformat(), "label": label},
        "basis": basis,
        "sources": {"invoices": n_inv, "payouts": n_pay},
        # Until somebody has confirmed the rates, the screen says so rather
        # than presenting a published headline figure as this company's own.
        "rates_confirmed": bool(org["tax_reviewed_at"]) if "tax_reviewed_at" in keys else False,
        "vat_exempt_reason": (org["vat_exempt_reason"] if "vat_exempt_reason" in keys else None) or "",
        "currency": org["pay_iban"] and "EUR" or "EUR",
    })
    return jsonify(out)


@app.get("/api/finance/settings")
@login_required
def finance_settings_get():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    r = taxes.rules(org["country"])
    keys = org.keys()
    return jsonify({
        "country": org["country"] or "",
        "defaults": r,
        "vat_rate": company.META.get((org["country"] or "").upper(), {}).get("vat_rate", 0),
        "profit_rate": org["tax_profit_rate"] if "tax_profit_rate" in keys else None,
        "payroll_rate": org["tax_payroll_rate"] if "tax_payroll_rate" in keys else None,
        "vat_exempt": bool(org["vat_exempt"]) if "vat_exempt" in keys else False,
        "vat_exempt_reason": (org["vat_exempt_reason"] if "vat_exempt_reason" in keys else "") or "",
        "profit_tax_exempt": bool(org["profit_tax_exempt"]) if "profit_tax_exempt" in keys else False,
        "reviewed_at": (org["tax_reviewed_at"] if "tax_reviewed_at" in keys else "") or "",
        "reasons": list(taxes.VAT_EXEMPT_REASONS),
        "rates_reviewed": taxes.RATES_REVIEWED,
        # What this country asks to be filed. The form names stay in their own
        # language whatever the interface language is: a справка-декларация is
        # called that on the form, and translating it would send somebody
        # looking for a document that does not exist under that name.
        "filing": taxes.filing(org["country"]),
        # Where to actually go and do it. The institution's own domain, never
        # an intermediary: a tax portal reached through somebody else's site is
        # how credentials end up somewhere they should not.
        "portals": taxes.portals(org["country"]),
    })


@app.post("/api/finance/settings")
@login_required
def finance_settings_save():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_role(conn, user["id"], org["id"], "owner", "admin")
    d = body()

    def rate(name):
        v = d.get(name)
        if v in (None, ""):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ApiError("Ставката е число между 0 и 100", 400, code="bad_rate")
        if not 0 <= v <= 100:
            raise ApiError("Ставката е число между 0 и 100", 400, code="bad_rate")
        return v

    reason = (d.get("vat_exempt_reason") or "").strip()
    if reason and reason not in taxes.VAT_EXEMPT_REASONS:
        raise ApiError("Непознато основание", 400, code="bad_exempt_reason")
    conn.execute(
        "UPDATE orgs SET tax_profit_rate=?, tax_payroll_rate=?, vat_exempt=?, "
        "vat_exempt_reason=?, profit_tax_exempt=?, tax_reviewed_at=date('now') WHERE id=?",
        (rate("profit_rate"), rate("payroll_rate"), 1 if d.get("vat_exempt") else 0,
         reason or None, 1 if d.get("profit_tax_exempt") else 0, org["id"]))
    conn.commit()
    return finance_settings_get()


@app.get("/api/expenses")
@login_required
def expenses_list():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        return jsonify({"items": []})
    rows = conn.execute(
        "SELECT * FROM expenses WHERE org_id=? ORDER BY recurring DESC, id DESC",
        (org["id"],)).fetchall()
    return jsonify({"items": [dict(r) for r in rows], "kinds": list(taxes.COST_KINDS)})


@app.post("/api/expenses")
@login_required
def expense_add():
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_role(conn, user["id"], org["id"], "owner", "admin", "member")
    d = body()
    label = (d.get("label") or "").strip()[:160]
    if not label:
        raise ApiError("Липсва наименование", 400, code="field_required",
                       info={"field": "label"})
    kind = d.get("kind") if d.get("kind") in taxes.COST_KINDS else "operating"
    try:
        amount = float(d.get("amount") or 0)
        vat = float(d.get("vat_amount") or 0)
    except (TypeError, ValueError):
        raise ApiError("Невалидна сума", 400, code="bad_amount")
    if amount < 0 or vat < 0:
        raise ApiError("Невалидна сума", 400, code="bad_amount")
    recurring = 1 if d.get("recurring") else 0
    cur = conn.execute(
        "INSERT INTO expenses (org_id, kind, label, amount, currency, vat_amount, "
        "recurring, on_date, starts_on, ends_on, note, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (org["id"], kind, label, round(amount, 2), (d.get("currency") or "EUR")[:3],
         round(vat, 2), recurring,
         (d.get("on_date") or "")[:10] or None,
         (d.get("starts_on") or "")[:10] or None,
         (d.get("ends_on") or "")[:10] or None,
         (d.get("note") or "").strip()[:400] or None, user["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM expenses WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row))


@app.delete("/api/expenses/<int:eid>")
@login_required
def expense_delete(eid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    require_role(conn, user["id"], org["id"] if org else 0, "owner", "admin")
    row = conn.execute("SELECT * FROM expenses WHERE id=? AND org_id=?",
                       (eid, org["id"] if org else 0)).fetchone()
    if not row:
        raise ApiError("Не е намерено", 404, code="not_found")
    conn.execute("DELETE FROM expenses WHERE id=?", (eid,))
    conn.commit()
    return jsonify({"ok": True})


@app.get("/api/finance/export")
@login_required
def finance_export():
    """The period as a CSV an accountant, an ERP or a spreadsheet can read.

    Not a bespoke integration with one vendor: every accounting package on
    these markets imports a CSV, and the webhook already carries the events for
    the ones that would rather listen than import.
    """
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        raise ApiError("Нямате организация", 400, code="no_org")
    require_feature(conn, org, "finance")
    first, last, label = _period_from_request()
    basis = "cash" if request.args.get("basis") == "cash" else "accrual"
    revenue, vat_out, vat_in, costs, _n, _p = finance_figures(conn, org, first, last, basis)
    keys = org.keys()
    r = taxes.compute(revenue, vat_out, vat_in, costs, org["country"],
                      profit_tax=org["tax_profit_rate"] if "tax_profit_rate" in keys else None,
                      payroll_employer=org["tax_payroll_rate"] if "tax_payroll_rate" in keys else None,
                      vat_exempt=bool(org["vat_exempt"]) if "vat_exempt" in keys else False,
                      profit_tax_exempt=bool(org["profit_tax_exempt"]) if "profit_tax_exempt" in keys else False)
    rows = [
        ("period", label), ("from", first.isoformat()), ("to", last.isoformat()),
        ("basis", basis), ("country", r["country"]),
        ("revenue_net", r["revenue"]),
        ("cost_operating", r["costs"]["operating"]),
        ("cost_lease", r["costs"]["lease"]),
        ("cost_salary", r["costs"]["salary"]),
        ("cost_employer_contributions", r["costs"]["employer_contributions"]),
        ("cost_other", r["costs"]["other"]),
        ("cost_total", r["costs"]["total"]),
        ("profit_before_tax", r["profit_before_tax"]),
        ("profit_tax_rate", r["profit_tax_rate"]),
        ("profit_tax", r["profit_tax"]),
        ("net_profit", r["net_profit"]),
        ("vat_charged", r["vat"]["charged"]), ("vat_paid", r["vat"]["paid"]),
        ("vat_due", r["vat"]["due"]), ("vat_exempt", int(r["vat"]["exempt"])),
        ("rates_reviewed", r["rates_reviewed"]),
    ]
    csv_text = "key,value\r\n" + "".join("%s,%s\r\n" % (k, v) for k, v in rows)
    resp = app.response_class(csv_text, mimetype="text/csv")
    resp.headers["Content-Disposition"] = (
        'attachment; filename="seam-finance-%s.csv"' % label)
    return resp


# --------------------------------------------------------------------------- #
#  What this company may open
#
#  entitlements.py decides; this reads the stored facts and hands them over,
#  and refuses when the answer is no. A part is granted by exactly the three
#  proofs that grant a plan - a signed webhook, an answer from the processor
#  about a session this server opened, or an administrator recording a
#  transfer - and by nothing a browser says.
# --------------------------------------------------------------------------- #
def org_purchases(conn, org_id):
    rows = conn.execute(
        "SELECT feature, kind, until FROM feature_purchases WHERE org_id=?",
        (org_id,)).fetchall()
    return [dict(r) for r in rows]


def org_can(conn, org, feature):
    """Whether this company may open this part, today."""
    if not org:
        return False
    return entitlements.has(feature, org["plan"], org["plan_until"],
                            org_purchases(conn, org["id"]))


def require_feature(conn, org, feature):
    """The gate. Raises rather than returning, so a caller cannot forget to look.

    402 rather than 403: this is not "you may not", it is "this is not paid
    for", and the browser shows a price instead of an apology.
    """
    if org_can(conn, org, feature):
        return True
    raise ApiError("Тази част не е включена в плана", 402, code="feature_locked",
                   info={"feature": feature,
                         "price": entitlements.FEATURE_PRICE.get(feature)})


def grant_feature(conn, org_id, feature, kind, ref, method="card",
                  price=None, granted_by=None):
    """Record a bought part. Called only from a path that has proof of payment."""
    if feature not in entitlements.FEATURES:
        raise ApiError("Непозната услуга", 400, code="unknown_feature")
    if kind not in entitlements.PURCHASE_KINDS:
        kind = "once"
    days = entitlements.PURCHASE_DAYS.get(kind)
    until = None
    if days:
        # Extend rather than reset, for the same reason a plan does: paying
        # early must never cost somebody days.
        have = conn.execute(
            "SELECT until FROM feature_purchases WHERE org_id=? AND feature=? "
            "AND until IS NOT NULL ORDER BY until DESC LIMIT 1",
            (org_id, feature)).fetchone()
        start = datetime.utcnow()
        if have and have["until"]:
            try:
                got = datetime.strptime(have["until"][:10], "%Y-%m-%d")
                start = max(start, got)
            except ValueError:
                pass
        until = (start + timedelta(days=days)).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO feature_purchases (org_id, feature, kind, until, price, "
        "method, ref, granted_by) VALUES (?,?,?,?,?,?,?,?)",
        (org_id, feature, kind, until,
         price if price is not None else entitlements.price_of(feature=feature, kind=kind),
         method, ref, granted_by))
    billing_log(conn, "local", "feature:%s" % secrets.token_hex(8), "feature_granted",
                org_id, "%s %s%s via %s" % (feature, kind,
                                            (" until " + until) if until else "", method))
    return until


@app.get("/api/entitlements")
@login_required
def entitlements_get():
    """What is open, what is not, and what the closed ones cost."""
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    if not org:
        return jsonify({"features": {}, "plan": "starter", "plans": {}})
    purchases = org_purchases(conn, org["id"])
    return jsonify({
        "features": entitlements.summary(org["plan"], org["plan_until"], purchases),
        "plan": org["plan"] or "starter",
        "plan_until": org["plan_until"] or "",
        "lifetime": bool(entitlements.PLANS.get(org["plan"] or "", {}).get("forever")),
        "purchases": purchases,
        "plans": {k: dict(v, free_months=entitlements.free_months(plan=k))
                  for k, v in entitlements.PLANS.items()},
        "feature_prices": {
            k: {"month": entitlements.price_of(feature=k, kind="month"),
                "half": entitlements.price_of(feature=k, kind="half"),
                "year": entitlements.price_of(feature=k, kind="year"),
                "once": entitlements.price_of(feature=k, kind="once"),
                "free_half": entitlements.free_months(feature=k, kind="half"),
                "free_year": entitlements.free_months(feature=k, kind="year")}
            for k in entitlements.FEATURE_PRICE},
    })


@app.post("/api/entitlements/grant")
@login_required
def entitlements_grant():
    """An administrator recording a payment that arrived outside the processor.

    The third of the three proofs. A bank transfer is a real way to buy
    software on these markets, and refusing to record one would push companies
    onto a card they do not want to use; requiring an administrator is what
    keeps it from being a self-service upgrade.
    """
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    d = body()
    org_id = int(d.get("org_id") or 0)
    if not conn.execute("SELECT 1 FROM orgs WHERE id=?", (org_id,)).fetchone():
        raise ApiError("Не е намерено", 404, code="not_found")
    ref = (d.get("ref") or "").strip()[:120]
    if not ref:
        raise ApiError("Липсва основание за плащането", 400, code="field_required",
                       info={"field": "ref"})
    if d.get("plan"):
        plan = d["plan"]
        spec = entitlements.PLANS.get(plan)
        if not spec or not spec["all"]:
            raise ApiError("Непознат план", 400, code="unknown_plan")
        if spec["forever"]:
            conn.execute("UPDATE orgs SET plan=?, plan_until=NULL, pay_method='transfer' "
                         "WHERE id=?", (plan, org_id))
            billing_log(conn, "local", "grant:%s" % secrets.token_hex(8),
                        "plan_activated", org_id, "%s for good via transfer %s" % (plan, ref))
            until = None
        else:
            until = activate_plan(conn, org_id, days=spec["days"], method="transfer",
                                  plan=plan, ref=ref)
        conn.commit()
        return jsonify({"ok": True, "plan": plan, "until": until})

    until = grant_feature(conn, org_id, d.get("feature"), d.get("kind") or "once",
                          ref, method="transfer", granted_by=user["id"])
    conn.commit()
    return jsonify({"ok": True, "feature": d.get("feature"), "until": until})


def run_reminders(conn, now=None):
    """One pass over everything unpaid. Idempotent: an invoice already chased
    today is left alone, so running this twice sends nothing twice."""
    today = (now or datetime.utcnow()).date()
    sent = 0
    rows = conn.execute(
        "SELECT * FROM invoices WHERE status IN ('sent','part') AND due_date IS NOT NULL"
    ).fetchall()
    plans = {}
    for inv in rows:
        if inv["org_id"] not in plans:
            org = conn.execute("SELECT * FROM orgs WHERE id=?", (inv["org_id"],)).fetchone()
            plans[inv["org_id"]] = _pay_settings(org)["reminders"]
        offset = receivables.next_reminder(inv, plans[inv["org_id"]], today)
        if offset is None:
            continue
        kind = "rem" if offset < 0 else ("due" if offset == 0 else "late")
        ok, _detail = _invoice_send(conn, inv, kind, days=abs(offset))
        conn.execute(
            "UPDATE invoices SET reminders=reminders+1, last_reminder=? WHERE id=?",
            (today.strftime("%Y-%m-%d"), inv["id"]))
        # Counted whether or not it left: otherwise an invoice with no address
        # is retried every single day for ever.
        sent += 1 if ok else 0
    conn.commit()
    return sent


@app.post("/api/invoices/remind")
@login_required
def invoices_remind_now():
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    return jsonify({"sent": run_reminders(conn)})


@app.get("/api/invoices/<int:iid>/events")
@login_required
def invoice_events(iid):
    conn, user = g.conn, g.user
    org = user_primary_org(conn, user["id"])
    inv = conn.execute(
        "SELECT * FROM invoices WHERE id=? AND (org_id=? OR customer_org_id=?)",
        (iid, org["id"] if org else 0, org["id"] if org else 0)).fetchone()
    if not inv:
        raise ApiError("Не е намерено", 404, code="not_found")
    rows = conn.execute("SELECT * FROM invoice_events WHERE invoice_id=? ORDER BY id",
                        (iid,)).fetchall()
    return jsonify({"invoice": _inv_row(inv, mine=inv["org_id"] == (org or {})["id"]),
                    "events": [{"kind": r["kind"], "detail": r["detail"] or "",
                                "amount": r["amount"], "at": r["at"]} for r in rows]})


@app.get("/api/admin/smtp")
@login_required
def smtp_config_get():
    """Mail, set up from the screen rather than by editing a file.

    This is the thing that unblocks the rest: without it invitations go
    nowhere, a password cannot be reset by anyone who has lost it, and the
    advanced signature level cannot verify a mailbox."""
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    cfg = mailer.config() or {}
    return jsonify({
        "enabled": mailer.enabled(),
        "host": cfg.get("host", ""), "port": cfg.get("port", 587),
        "user": cfg.get("user", ""), "sender": cfg.get("sender", ""),
        "tls": bool(cfg.get("tls", True)),
        # Two different things: upgrade an open connection, or open it already
        # encrypted. Port 465 means the second and nothing else works there.
        "ssl": mailer.implicit_tls(cfg) if cfg else False,
        # Never sent back. Present only so the form can say whether one is set.
        "has_password": bool(cfg.get("password")),
        "from_env": bool(os.environ.get("SEAM_SMTP_HOST")),
    })


@app.put("/api/admin/smtp")
@login_required
def smtp_config_save():
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    if os.environ.get("SEAM_SMTP_HOST"):
        # The environment wins in mailer.config(), so writing the file would
        # have no effect and the screen would lie about what is in use.
        raise ApiError("Пощата е зададена през средата (SEAM_SMTP_HOST)", 400,
                       code="smtp_from_env")
    d = body()
    host = (d.get("host") or "").strip()[:200]
    if not host:
        try:
            os.remove(mailer.CFG_FILE)
        except OSError:
            pass
        return jsonify({"enabled": False})
    old = mailer.config() or {}
    try:
        port = max(1, min(65535, int(d.get("port") or 587)))
    except (TypeError, ValueError):
        port = 587
    password = (d.get("password") or "")
    cfg = {"host": host, "port": port,
           "user": (d.get("user") or "").strip()[:200],
           # A blank field keeps what is stored, so the form never has to hold
           # the password to save a change to the port.
           "password": password if password else old.get("password", ""),
           "sender": (d.get("sender") or "").strip()[:200],
           "tls": bool(d.get("tls", True)),
           # Left unset when the screen does not say, so the port decides -
           # somebody who typed 465 meant SSL.
           "ssl": bool(d["ssl"]) if "ssl" in d else None}
    with open(mailer.CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    # It holds a mailbox password, so it gets the same treatment as the other
    # credential files rather than whatever the umask happened to be.
    cryptobox.secure_file(mailer.CFG_FILE)
    return jsonify({"enabled": mailer.enabled(), "host": host, "port": port,
                    "user": cfg["user"], "sender": cfg["sender"], "tls": cfg["tls"],
                    "ssl": mailer.implicit_tls(cfg),
                    "has_password": bool(cfg["password"])})


@app.post("/api/admin/smtp/test")
@login_required
def smtp_test():
    """Send one to yourself. A mail setup that has never delivered anything is
    not a mail setup, and finding out at the moment somebody needs a reset is
    the worst time."""
    conn, user = g.conn, g.user
    require_instance_admin(conn, user["id"])
    if not mailer.enabled():
        raise ApiError("Пощата не е настроена", 400, code="smtp_unset")
    if rate_limited("smtptest:%d" % user["id"], 5, 600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    to = (body().get("to") or user["email"] or "").strip()
    lang = req_lang() if req_lang() in SMTP_TEST_I18N else "en"
    L = SMTP_TEST_I18N[lang]
    ok, detail = mailer.send_now(to, L["s"],
                                 '<div style="font-family:Segoe UI,Arial,sans-serif">%s</div>'
                                 % html_escape(L["b"]))
    if not ok:
        raise ApiError("Писмото не беше изпратено: %s" % str(detail)[:200], 502, code="smtp_failed")
    return jsonify({"sent": True, "to": to})


@app.post("/api/notify/sms-config")
@login_required
def sms_config_save():
    """Instance-wide SMS gateway. Written to .sms_config so a self-hosted
    deployment can be set up from the interface rather than by hand."""
    conn, user = g.conn, g.user
    store_org(conn, user)                       # business accounts only
    d = body()
    provider = (d.get("provider") or "").strip()
    if not provider:
        try:
            os.remove(sms.CONFIG_FILE)
        except OSError:
            pass
        return jsonify({"sms": None})
    if provider not in sms.PROVIDERS:
        raise ApiError("Непознат доставчик", 400, code="sms_provider")
    old = sms.config() or {}
    cfg = {"provider": provider}
    for k in ("url", "account_sid", "auth_token", "api_key", "from", "to_field",
              "text_field", "username", "password"):
        v = (d.get(k) or "").strip()
        if not v and k in sms.SECRET_FIELDS:
            v = old.get(k, "")                  # blank secret keeps what is stored
        if v:
            cfg[k] = v[:300]
    with open(sms.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    return jsonify({"sms": sms.public_config(sms.config())})


@app.post("/api/notify/sms-test")
@login_required
def sms_test():
    conn, user = g.conn, g.user
    row = conn.execute("SELECT phone FROM users WHERE id=?", (user["id"],)).fetchone()
    to = (body().get("to") or (row["phone"] if row else "") or "").strip()
    if not to:
        raise ApiError("Въведете телефонен номер", 400, code="field_required",
                       info={"field": "phone"})
    if rate_limited("smstest:" + str(user["id"]), 5, 600):
        raise ApiError("Твърде много опити, опитайте по-късно", 429, code="rate_limited")
    ok, detail = sms.send_now(to, "Seam: %s" % SMS_TEST_I18N.get(
        req_lang(), SMS_TEST_I18N["en"]))
    if not ok:
        raise ApiError("SMS не беше изпратен: %s" % detail, 502, code="sms_failed")
    return jsonify({"sent": True, "detail": detail})


@app.get("/api/automations")
@login_required
def automations_list():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    return jsonify(_automations_payload(conn, org["id"]))


@app.post("/api/automations")
@login_required
def automations_create():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    n = conn.execute("SELECT COUNT(*) c FROM automations WHERE org_id=?", (org["id"],)).fetchone()["c"]
    if n >= MAX_AUTOMATIONS:
        raise ApiError("Достигнат е лимитът от %d правила" % MAX_AUTOMATIONS, 400, code="auto_limit")
    name, trigger, conds, acts = _clean_rule(body())
    conn.execute(
        "INSERT INTO automations (org_id, name, trigger, conditions_json, actions_json, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (org["id"], name, trigger, json.dumps(conds, ensure_ascii=False),
         json.dumps(acts, ensure_ascii=False), user["id"]))
    conn.commit()
    return jsonify(_automations_payload(conn, org["id"]))


@app.patch("/api/automations/<int:aid>")
@login_required
def automations_update(aid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    row = conn.execute("SELECT * FROM automations WHERE id=? AND org_id=?", (aid, org["id"])).fetchone()
    if not row:
        raise ApiError("Не е намерено", 404, code="not_found")
    d = body()
    if "active" in d and len(d) == 1:            # the on/off toggle
        conn.execute("UPDATE automations SET active=? WHERE id=?", (1 if d["active"] else 0, aid))
    else:
        name, trigger, conds, acts = _clean_rule(d)
        conn.execute(
            "UPDATE automations SET name=?, trigger=?, conditions_json=?, actions_json=?, active=? WHERE id=?",
            (name, trigger, json.dumps(conds, ensure_ascii=False),
             json.dumps(acts, ensure_ascii=False), 1 if d.get("active", True) else 0, aid))
    conn.commit()
    return jsonify(_automations_payload(conn, org["id"]))


@app.delete("/api/automations/<int:aid>")
@login_required
def automations_delete(aid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    conn.execute("DELETE FROM automations WHERE id=? AND org_id=?", (aid, org["id"]))
    conn.commit()
    return jsonify(_automations_payload(conn, org["id"]))


@app.get("/api/integration/webhooks")
@login_required
def webhooks_list():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    return jsonify(_webhooks_payload(conn, org["id"]))


@app.post("/api/integration/webhooks")
@login_required
def webhooks_create():
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    require_verified(conn, user["id"])
    d = body()
    url = (require(d, "url") or "").strip()[:400]
    if not re.match(r"^https?://", url):
        raise ApiError("Адресът трябва да започва с http:// или https://", 400, code="bad_url")
    evs = d.get("events") or ["*"]
    evs = [e for e in evs if e == "*" or e in WEBHOOK_EVENTS] or ["*"]
    secret = "whsec_" + secrets.token_urlsafe(18)
    conn.execute("INSERT INTO webhooks (org_id, url, secret, label, events_json) VALUES (?,?,?,?,?)",
                 (org["id"], url, secret, (d.get("label") or "").strip()[:60],
                  json.dumps(evs, ensure_ascii=False)))
    return jsonify(_webhooks_payload(conn, org['id']))


@app.delete("/api/integration/webhooks/<int:hid>")
@login_required
def webhooks_delete(hid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    if not conn.execute("SELECT 1 FROM webhooks WHERE id=? AND org_id=?", (hid, org["id"])).fetchone():
        raise ApiError("Не е намерено", 404, code="not_found")
    conn.execute("DELETE FROM webhooks WHERE id=?", (hid,))
    return jsonify(_webhooks_payload(conn, org['id']))


@app.post("/api/integration/webhooks/<int:hid>/test")
@login_required
def webhooks_test(hid):
    conn, user = g.conn, g.user
    org = store_org(conn, user)
    h = conn.execute("SELECT * FROM webhooks WHERE id=? AND org_id=?", (hid, org["id"])).fetchone()
    if not h:
        raise ApiError("Не е намерено", 404, code="not_found")
    payload = {"event": "test.ping", "sent_at": datetime.utcnow().isoformat() + "Z",
               "data": {"org": org["name"], "message": "Seam webhook test"}}
    threading.Thread(target=_webhook_send, args=(h["id"], h["url"], h["secret"], payload), daemon=True).start()
    return jsonify({"ok": True})


def _find_product(conn, org_id, ident):
    s = str(ident).strip()
    if s.isdigit():
        p = conn.execute("SELECT * FROM products WHERE id=? AND org_id=?", (int(s), org_id)).fetchone()
        if p:
            return p
    p = conn.execute("SELECT * FROM products WHERE org_id=? AND UPPER(COALESCE(sku,''))=? LIMIT 1",
                     (org_id, s.upper())).fetchone()
    if p:
        return p
    return conn.execute("SELECT * FROM products WHERE org_id=? AND name=? LIMIT 1", (org_id, s)).fetchone()


@app.post("/api/inbound/orders")
def inbound_order():
    """External systems (webshop backend, Zapier, chatbot...) push orders here.
    Auth: X-Seam-Key header with a secret integration key."""
    conn = db.get_db()
    try:
        if rate_limited("inb:" + _client_ip(), 60, 60):
            raise ApiError("Твърде много заявки", 429, code="rate_limited")
        d = body()
        key = request.headers.get("X-Seam-Key") or d.get("api_key") or ""
        r = conn.execute("SELECT * FROM api_keys WHERE key=? AND revoked=0 AND kind='secret'",
                     (db.token_hash(key),)).fetchone()
        if not r:
            raise ApiError("Невалиден API ключ", 401, code="invalid_api_key")
        conn.execute("UPDATE api_keys SET last_used_at=datetime('now') WHERE id=?", (r["id"],))
        ident = d.get("product") or d.get("product_id") or d.get("sku")
        if not ident:
            raise ApiError("Липсва продукт", 400, code="field_required", info={"field": "product"})
        p = _find_product(conn, r["org_id"], ident)
        if not p:
            raise ApiError("Непознат продукт", 404, code="unknown_product")
        o = _new_store_order(conn, r["org_id"], p, _safe_qty(d.get("qty")),
                             d.get("customer"), d.get("note"), "api", notify_users=True)
        conn.commit()
        return jsonify({"ok": True, "ref": o["ref"], "id": o["id"]})
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Hosted order form (no-code path): share or embed /order/<public-key>.
FORM_I18N = {
    "en": {"reg_label": "Company number", "title": "Place an order", "product": "Product", "qty": "Quantity", "name": "Your name", "note": "Note", "submit": "Send order", "success_t": "Thank you!", "success_p": "Your order was received.", "ref": "Reference", "unavailable": "This order form is not available.", "powered": "Powered by Seam"},
    "es": {"reg_label": "Número de registro", "title": "Realizar un pedido", "product": "Producto", "qty": "Cantidad", "name": "Su nombre", "note": "Nota", "submit": "Enviar pedido", "success_t": "¡Gracias!", "success_p": "Su pedido ha sido recibido.", "ref": "Referencia", "unavailable": "Este formulario de pedido no está disponible.", "powered": "Con la tecnología de Seam"},
    "fr": {"reg_label": "Numéro d'immatriculation", "title": "Passer une commande", "product": "Produit", "qty": "Quantité", "name": "Votre nom", "note": "Note", "submit": "Envoyer la commande", "success_t": "Merci !", "success_p": "Votre commande a été reçue.", "ref": "Référence", "unavailable": "Ce formulaire de commande n'est pas disponible.", "powered": "Propulsé par Seam"},
    "pl": {"reg_label": "Numer rejestrowy", "title": "Złóż zamówienie", "product": "Produkt", "qty": "Ilość", "name": "Twoje imię i nazwisko", "note": "Uwaga", "submit": "Wyślij zamówienie", "success_t": "Dziękujemy!", "success_p": "Twoje zamówienie zostało przyjęte.", "ref": "Numer", "unavailable": "Ten formularz zamówienia jest niedostępny.", "powered": "Napędzane przez Seam"},
    "uk": {"reg_label": "Реєстраційний номер", "title": "Оформити замовлення", "product": "Товар", "qty": "Кількість", "name": "Ваше ім'я", "note": "Нотатка", "submit": "Надіслати замовлення", "success_t": "Дякуємо!", "success_p": "Ваше замовлення прийнято.", "ref": "Номер", "unavailable": "Ця форма замовлення недоступна.", "powered": "Працює на Seam"},
    "pt": {"reg_label": "Número de registo", "title": "Fazer uma encomenda", "product": "Produto", "qty": "Quantidade", "name": "O seu nome", "note": "Nota", "submit": "Enviar encomenda", "success_t": "Obrigado!", "success_p": "A sua encomenda foi recebida.", "ref": "Referência", "unavailable": "Este formulário de encomenda não está disponível.", "powered": "Com tecnologia Seam"},
    "bg": {"reg_label": "Фирмен номер", "title": "Направете поръчка", "product": "Продукт", "qty": "Количество", "name": "Вашето име", "note": "Бележка", "submit": "Изпрати поръчката", "success_t": "Благодарим ви!", "success_p": "Поръчката ви е приета.", "ref": "Референтен номер", "unavailable": "Тази форма за поръчки не е достъпна.", "powered": "Задвижвано от Seam"},
    "de": {"reg_label": "Firmennummer", "title": "Bestellung aufgeben", "product": "Produkt", "qty": "Menge", "name": "Ihr Name", "note": "Notiz", "submit": "Bestellung senden", "success_t": "Vielen Dank!", "success_p": "Ihre Bestellung ist eingegangen.", "ref": "Referenz", "unavailable": "Dieses Bestellformular ist nicht verfügbar.", "powered": "Bereitgestellt von Seam"},
    "ro": {"reg_label": "Număr de înregistrare", "title": "Plasați o comandă", "product": "Produs", "qty": "Cantitate", "name": "Numele dvs.", "note": "Notă", "submit": "Trimite comanda", "success_t": "Vă mulțumim!", "success_p": "Comanda dvs. a fost primită.", "ref": "Referință", "unavailable": "Acest formular de comandă nu este disponibil.", "powered": "Susținut de Seam"},
    "el": {"reg_label": "Αριθμός μητρώου", "title": "Κάντε παραγγελία", "product": "Προϊόν", "qty": "Ποσότητα", "name": "Το όνομά σας", "note": "Σημείωση", "submit": "Αποστολή παραγγελίας", "success_t": "Ευχαριστούμε!", "success_p": "Η παραγγελία σας παραλήφθηκε.", "ref": "Αναφορά", "unavailable": "Αυτή η φόρμα παραγγελίας δεν είναι διαθέσιμη.", "powered": "Με την υποστήριξη του Seam"},
    "tr": {"reg_label": "Sicil numarası", "title": "Sipariş verin", "product": "Ürün", "qty": "Adet", "name": "Adınız", "note": "Not", "submit": "Siparişi gönder", "success_t": "Teşekkürler!", "success_p": "Siparişiniz alındı.", "ref": "Referans", "unavailable": "Bu sipariş formu kullanılamıyor.", "powered": "Seam altyapısıyla"},
    "it": {"reg_label": "Numero di registro", "title": "Effettua un ordine", "product": "Prodotto", "qty": "Quantità", "name": "Il tuo nome", "note": "Nota", "submit": "Invia ordine", "success_t": "Grazie!", "success_p": "Il tuo ordine è stato ricevuto.", "ref": "Riferimento", "unavailable": "Questo modulo d'ordine non è disponibile.", "powered": "Fornito da Seam"},
    "ru": {"reg_label": "Регистрационный номер", "title": "Оформить заказ", "product": "Товар", "qty": "Количество", "name": "Ваше имя", "note": "Заметка", "submit": "Отправить заказ", "success_t": "Спасибо!", "success_p": "Ваш заказ принят.", "ref": "Номер", "unavailable": "Эта форма заказа недоступна.", "powered": "Работает на Seam"},
}


def _form_lang():
    lang = request.cookies.get("seam_lang")
    if lang in FORM_I18N:
        return lang
    al = (request.headers.get("Accept-Language") or "")[:2].lower()
    return al if al in FORM_I18N else "en"


def _form_page(lang, org_name, inner):
    L = FORM_I18N[lang]
    return ("""<!DOCTYPE html><html lang="%(lang)s"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light"><title>%(org)s · %(title)s</title>
<style>
 body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f6f7f9;color:#1a1f2b}
 .top{background:linear-gradient(135deg,#1b2336,#2d3f64);color:#fff;padding:26px 20px;text-align:center}
 .top h1{margin:0;font-size:20px}.top .org{opacity:.75;font-size:13px;margin-top:4px}
 .card{max-width:460px;margin:24px auto;background:#fff;border:1px solid #e6e8ec;border-radius:14px;padding:24px;box-shadow:0 4px 16px rgba(20,27,43,.08)}
 label{display:block;font-size:12.5px;font-weight:600;color:#5b6472;margin:14px 0 5px}
 input,select,textarea{width:100%%;box-sizing:border-box;padding:10px 12px;border:1px solid #d3d7de;border-radius:8px;font-size:14px;font-family:inherit}
 button{width:100%%;margin-top:20px;padding:12px;background:#2f6df0;color:#fff;border:0;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}
 button:hover{background:#2a5fd0}
 .ok{text-align:center;padding:14px 0}.ok .big{font-size:44px}.ok .ref{font-family:ui-monospace,monospace;background:#eaf1fe;color:#2f6df0;padding:6px 14px;border-radius:8px;display:inline-block;margin-top:10px;font-weight:700}
 .foot{text-align:center;color:#8a93a3;font-size:12px;margin:18px 0}
</style></head><body>
<div class="top"><h1>%(title)s</h1><div class="org">%(org)s</div></div>
<div class="card">%(inner)s</div>
<div class="foot">%(powered)s</div>
</body></html>""" % {"lang": lang, "org": html_escape(org_name or ""), "title": L["title"],
                     "inner": inner, "powered": L["powered"]})


@app.route("/order/<key>", methods=["GET", "POST"])
def public_order_form(key):
    conn = db.get_db()
    try:
        lang = _form_lang()
        L = FORM_I18N[lang]
        row = conn.execute("SELECT * FROM api_keys WHERE key=? AND revoked=0 AND kind='public'",
                       (key,)).fetchone()
        if not row:
            return _form_page(lang, "Seam", "<p>%s</p>" % L["unavailable"]), 404
        org = conn.execute("SELECT name FROM orgs WHERE id=?", (row["org_id"],)).fetchone()
        prods = conn.execute("SELECT * FROM products WHERE org_id=? ORDER BY name", (row["org_id"],)).fetchall()

        if request.method == "POST":
            if rate_limited("form:" + _client_ip(), 20, 300):
                return _form_page(lang, org["name"], "<p>%s</p>" % L["unavailable"]), 429
            p = conn.execute("SELECT * FROM products WHERE id=? AND org_id=?",
                             (request.form.get("product", "0"), row["org_id"])).fetchone()
            if p:
                conn.execute("UPDATE api_keys SET last_used_at=datetime('now') WHERE id=?", (row["id"],))
                o = _new_store_order(conn, row["org_id"], p, _safe_qty(request.form.get("qty")),
                                     request.form.get("customer"), request.form.get("note"), "form",
                                     notify_users=True)
                conn.commit()
                inner = ('<div class="ok"><div class="big">✓</div><h2>%s</h2><p>%s</p>'
                         '<div>%s: <span class="ref">%s</span></div></div>'
                         % (L["success_t"], L["success_p"], L["ref"], html_escape(o["ref"])))
                return _form_page(lang, org["name"], inner)

        opts = "".join('<option value="%d">%s%s</option>'
                       % (p["id"], html_escape(demo_i18n.translate(p["name"][1:], lang) if str(p["name"]).startswith("§") else p["name"]),
                          (" · " + html_escape(p["price"])) if p["price"] else "")
                       for p in prods)
        inner = ('<form method="post">'
                 '<label>%(product)s</label><select name="product" required>%(opts)s</select>'
                 '<label>%(qty)s</label><input name="qty" type="number" min="1" max="999" value="1" required>'
                 '<label>%(name)s</label><input name="customer" maxlength="120" required>'
                 '<label>%(note)s</label><textarea name="note" rows="3" maxlength="500"></textarea>'
                 '<button type="submit">%(submit)s</button></form>'
                 % {"product": L["product"], "opts": opts, "qty": L["qty"],
                    "name": L["name"], "note": L["note"], "submit": L["submit"]})
        return _form_page(lang, org["name"], inner)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
#  Static / SPA
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return _index_html()


#: The paths a crawler is welcome on. Everything else is either an API, a file
#: belonging to one relationship, or a rendered document with a one-time link
#: in it, and none of those should ever appear in a search result.
PUBLIC_PATHS = ("/", "/terms", "/privacy-policy", "/faq")


def _public_page(name, lang=None):
    """A page a visitor and a crawler can both read, without the app loading.

    Server-rendered on purpose: /terms answered by the single-page shell is a
    spinner to a crawler and an empty screen to anyone with JavaScript off.
    """
    lang = lang if lang in SUPPORTED_LANGS else req_lang()
    title, lead, items = public_text.page(name, lang)
    root = request.url_root.rstrip("/")
    path = PUBLIC_PATH[name]
    body = "".join(
        "<section><h2>%s</h2><p>%s</p></section>"
        % (html_escape(h), html_escape(b)) for h, b in items)
    nav = " · ".join(
        '<a href="%s">%s</a>' % (public_url(key, lang), html_escape(public_text.page(key, lang)[0]))
        for key in ("terms", "privacy", "faq") if key != name)

    # Every translation gets its own address, and each address names the others.
    # Without this a search engine finds one language of thirteen, and a reader
    # who lands on the wrong one has no way across.
    alts = "".join(
        '<link rel="alternate" hreflang="%s" href="%s%s" />' % (code, root, public_url(name, code))
        for code in PUBLIC_LANGS)
    alts += '<link rel="alternate" hreflang="x-default" href="%s%s" />' % (root, path)
    picker = " ".join(
        '<a hreflang="%s" href="%s"%s>%s</a>'
        % (code, public_url(name, code), ' aria-current="true"' if code == lang else "",
           html_escape(LANG_NAMES[code]))
        for code in PUBLIC_LANGS)

    doc = PUBLIC_PAGE_HTML % {
        "lang": lang, "title": html_escape(title), "lead": html_escape(lead),
        "body": body, "nav": nav, "alts": alts, "picker": picker,
        "canonical": root + public_url(name, lang), "root": root,
        "home": html_escape(public_text.HOME.get(lang, public_text.HOME["en"])),
    }
    resp = app.response_class(doc, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Content-Language"] = lang
    return resp


#: In the order a language picker should read, which is the order the interface
#: already uses, not alphabetical by English name.
PUBLIC_LANGS = ("en", "bg", "de", "ro", "el", "tr", "it", "ru", "es", "fr", "pl", "uk", "pt")
LANG_NAMES = {"en": "English", "bg": "Български", "de": "Deutsch", "ro": "Română",
              "el": "Ελληνικά", "tr": "Türkçe", "it": "Italiano", "ru": "Русский",
              "es": "Español", "fr": "Français", "pl": "Polski", "uk": "Українська",
              "pt": "Português"}
PUBLIC_PATH = {"terms": "/terms", "privacy": "/privacy-policy", "faq": "/faq"}


def public_url(name, lang):
    """/terms for the default, /bg/terms for a translation.

    A path segment rather than a query parameter: it is the address a person
    can read, copy and send, and the one a search engine treats as a page in
    its own right.
    """
    path = PUBLIC_PATH[name]
    return path if lang == "en" else "/%s%s" % (lang, path)


PUBLIC_PAGE_HTML = """<!DOCTYPE html>
<html lang="%(lang)s"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>%(title)s | Seam</title>
<meta name="description" content="%(lead)s" />
<meta name="robots" content="index, follow" />
<link rel="canonical" href="%(canonical)s" />
<meta property="og:type" content="article" />
<meta property="og:site_name" content="Seam" />
<meta property="og:title" content="%(title)s" />
<meta property="og:description" content="%(lead)s" />
<meta property="og:image" content="%(root)s/static/og-image.png" />
<meta property="og:url" content="%(canonical)s" />
<meta property="og:locale" content="%(lang)s" />
<meta name="twitter:card" content="summary_large_image" />
<meta name="twitter:title" content="%(title)s" />
<meta name="twitter:description" content="%(lead)s" />
<meta name="twitter:image" content="%(root)s/static/og-image.png" />
%(alts)s
<link rel="icon" type="image/svg+xml" href="/static/icon.svg" />
<meta name="theme-color" content="#2f6df0" />
<style>
 :root { color-scheme: light dark; }
 * { box-sizing: border-box; }
 body { margin:0; background:#0B0E14; color:#E7EAF0; line-height:1.62;
   font-family:"Segoe UI",system-ui,-apple-system,sans-serif;
   font-size:16px; }
 main { max-width:44rem; margin:0 auto; padding:64px 22px 96px; }
 a { color:#8FB0FF; }
 .home { display:inline-block; font-weight:700; letter-spacing:.04em;
   text-decoration:none; margin-bottom:36px; }
 h1 { font-size:clamp(26px,4vw,34px); line-height:1.18; margin:0 0 10px;
   letter-spacing:-.015em; }
 .lead { color:#A8B2C4; margin:0 0 12px; }
 section { border-top:1px solid rgba(255,255,255,.12); padding:22px 0 4px; }
 h2 { font-size:17px; margin:0 0 8px; }
 section p { margin:0; color:#B9C2D2; }
 footer { border-top:1px solid rgba(255,255,255,.12); margin-top:34px;
   padding-top:18px; color:#8A93A6; font-size:14px; }
 .langs { margin-top:14px; display:flex; flex-wrap:wrap; gap:6px 14px; }
 .langs a { color:#8A93A6; text-decoration:none; font-size:13px; }
 .langs a[aria-current] { color:#E7EAF0; font-weight:700; }
 :focus-visible { outline:2px solid #8FB0FF; outline-offset:3px; border-radius:4px; }
</style></head>
<body><main>
 <a class="home" href="/">&larr; Seam</a>
 <h1>%(title)s</h1>
 <p class="lead">%(lead)s</p>
 %(body)s
 <footer>%(nav)s
  <nav class="langs" aria-label="Language">%(picker)s</nav>
 </footer>
</main></body></html>
"""


@app.get("/terms")
def terms_page():
    return _public_page("terms")


@app.get("/privacy-policy")
def privacy_page():
    return _public_page("privacy")


@app.get("/faq")
def faq_page():
    return _public_page("faq")


# A translation is a page of its own, at an address a person can send.
# `any(...)` keeps the segment to the thirteen codes, so /w/terms is still a
# workspace and not a language.
_LANGS_RULE = ",".join(sorted(SUPPORTED_LANGS))


@app.get("/<any(%s):lang>/terms" % _LANGS_RULE)
def terms_page_i18n(lang):
    return _public_page("terms", lang)


@app.get("/<any(%s):lang>/privacy-policy" % _LANGS_RULE)
def privacy_page_i18n(lang):
    return _public_page("privacy", lang)


@app.get("/<any(%s):lang>/faq" % _LANGS_RULE)
def faq_page_i18n(lang):
    return _public_page("faq", lang)


@app.get("/robots.txt")
def robots():
    body = "\n".join([
        "User-agent: *",
        # Not secrecy, which is what the session check is for: a crawler that
        # walks these produces nothing but soft 404s and burns the crawl budget
        # that should go on the four pages worth indexing.
        "Disallow: /api/",
        "Disallow: /files/",
        "Disallow: /docs/",
        "Disallow: /d/",
        "Disallow: /sign/",
        "Disallow: /pay/",
        "Disallow: /invite/",
        "Disallow: /backups/",
        "Allow: /",
        "",
        "Sitemap: %ssitemap.xml" % request.url_root,
        "",
    ])
    return app.response_class(body, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    """Every page in every language, each one naming its translations.

    Listing four English URLs would have offered a search engine one thirteenth
    of what is written, and no way to learn the rest existed.
    """
    root = request.url_root.rstrip("/")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    XHTML = ' xmlns:xhtml="http://www.w3.org/1999/xhtml"'
    entries = []
    for name in ("home",) + tuple(PUBLIC_PATH):
        for code in PUBLIC_LANGS:
            here = "/" if name == "home" else public_url(name, code)
            if name == "home" and code != "en":
                continue                       # the app itself has one address
            alts = "".join(
                '<xhtml:link rel="alternate" hreflang="%s" href="%s%s"/>'
                % (c, root, "/" if name == "home" else public_url(name, c))
                for c in PUBLIC_LANGS) if name != "home" else ""
            entries.append(
                "<url><loc>%s%s</loc><lastmod>%s</lastmod>%s"
                "<changefreq>weekly</changefreq><priority>%s</priority></url>"
                % (root, "" if here == "/" else here, today, alts,
                   "1.0" if name == "home" else "0.6"))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"%s>%s</urlset>'
           % (XHTML, "".join(entries)))
    return app.response_class(xml, mimetype="application/xml")


@app.get("/favicon.ico")
def favicon():
    """A browser asks for this by name whatever the markup says, and a 404 in
    the console on every page load is the kind of thing that makes a product
    look unfinished."""
    return send_from_directory(app.static_folder, "icon.svg", mimetype="image/svg+xml")


@app.get("/sw.js")
def service_worker():
    """Served from the root on purpose: a service worker's scope defaults to the
    directory it is served from, and under /static/ it would not cover the app."""
    resp = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


#: Everything served with ?v=<build> has to be in here, or its own changes do
#: not move the fingerprint and browsers keep the copy they already have.
#: docprint.js was served with a version it did not contribute to.
ASSET_FILES = ("js/theme-init.js", "js/i18n.js", "js/money.js", "js/api.js",
               "js/directory.js", "js/app.js", "css/styles.css", "js/sign.js",
               "js/docguard.js", "js/docprint.js")


def _asset_build():
    """Fingerprint of the shipped assets. Appended as ?v=... so long-lived
    caching is safe: a new build changes the URL and browsers refetch."""
    stamp = 0
    for rel in ASSET_FILES:
        try:
            stamp = max(stamp, int(os.path.getmtime(os.path.join(app.static_folder, rel))))
        except OSError:
            pass
    return str(stamp)


def _index_html(status=200):
    """index.html with cache-busting versions on the asset URLs."""
    with open(os.path.join(app.static_folder, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()
    v = _asset_build()
    for rel in ASSET_FILES:
        html = html.replace('"/static/%s"' % rel, '"/static/%s?v=%s"' % (rel, v))
    # A relative canonical is ambiguous the moment the same app answers on two
    # hosts, which it does the day a staging copy goes up. The absolute one is
    # built from the request, so it is right wherever this is deployed.
    root = request.url_root
    lang = req_lang()
    html = html.replace('<html lang="en">', '<html lang="%s">' % lang, 1)
    html = html.replace('<link rel="canonical" href="/" />',
                        '<link rel="canonical" href="%s" />' % root)
    html = html.replace('content="/static/og-image', 'content="%sstatic/og-image' % root)
    # The shell told every reader its locale was English and never gave an
    # og:url at all, so a shared link carried no address of its own.
    html = html.replace(
        '<meta property="og:locale" content="en" />',
        '<meta property="og:locale" content="%s" />\n'
        '  <meta property="og:url" content="%s" />' % (lang, root))
    resp = app.response_class(html, mimetype="text/html", status=status)
    resp.headers["Cache-Control"] = "no-cache"   # the shell must always be fresh
    resp.headers["Content-Language"] = lang
    return resp


#: Paths the single-page app knows how to render. Anything else is a mistake,
#: and answering it with 200 and the shell is a soft 404: a crawler files the
#: page as real, a monitor never sees the break, and a mistyped link looks like
#: an empty product rather than a wrong address.
SPA_PREFIXES = ("w/", "o/", "d/", "p/", "r/", "invite/", "reset/", "verify/",
                "sign/", "pay/", "passport/", "vehicles", "analytics", "assistant",
                "store", "templates", "docs", "money", "network", "integrations",
                "plans", "profile", "settings", "privacy", "admin", "login",
                "register", "terms", "faq")


@app.get("/<path:path>")
def spa(path):
    if path.startswith("api/"):
        raise ApiError("Не е намерено", 404, code="not_found")
    full = os.path.join(app.static_folder, path)
    if os.path.isfile(full):
        return send_from_directory(app.static_folder, path)
    known = any(path == p.rstrip("/") or path.startswith(p) for p in SPA_PREFIXES)
    return _index_html(200 if known else 404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("SEAM_HOST", "127.0.0.1")

    # The Werkzeug debugger is an interactive Python console on any unhandled
    # exception. On a reachable host that is remote code execution, so it is
    # opt-in and never the default: set SEAM_DEBUG=1 deliberately, on a machine
    # only you can reach.
    debug = os.environ.get("SEAM_DEBUG") == "1"
    if debug and host != "127.0.0.1":
        raise SystemExit("Refusing to run the debugger on a non-local host (%s). "
                         "Unset SEAM_DEBUG or bind to 127.0.0.1." % host)

    if os.environ.get("SEAM_AGREEMENT_SCAN", "on").lower() not in ("off", "0", "false"):
        threading.Thread(target=_agreements_daily, daemon=True).start()
        print("Seam: agreement and invoice reminders on (SEAM_AGREEMENT_SCAN=off to stop)")

    if os.environ.get("SEAM_BACKUP_DAILY") == "1":
        threading.Thread(target=_backup_daily, daemon=True).start()
        print("Seam: daily backup enabled -> %s" % BACKUP_DIR)

    try:
        from waitress import serve
    except ImportError:
        serve = None

    if serve and not debug:
        # A real WSGI server: the Flask one is single-purpose and says so itself.
        #
        # Waitress *strips* X-Forwarded-* unless it is told a proxy is in front,
        # and that default is the right one: a header anybody can send must not
        # be allowed to claim the connection was encrypted. But nothing here
        # ever let the operator say "there is a proxy", so behind nginx or
        # Caddy the app saw plain http and two things went wrong quietly: the
        # session cookie shipped without Secure on an HTTPS site - a session
        # anyone on the path can take - and the go-live check reported "no
        # https" on a correctly terminated one, sending whoever read it hunting
        # for a fault that was not there.
        #
        # SEAM_TRUSTED_PROXY is the missing switch. Set it to the proxy's
        # address, or to * when the only route in is through it.
        kw = {}
        # "*" means "believe whoever connects". That is correct when the only
        # thing that can connect is the proxy, and a hole when the port is open
        # to the world: anyone could then send X-Forwarded-Proto: https, be
        # believed, and get a session cookie marked Secure over a connection
        # that is not. Refuse the combination rather than document it.
        local_bind = host in ("127.0.0.1", "localhost", "::1")
        if TRUSTED_PROXY == "*" and not local_bind and not PROXY_ONLY:
            raise SystemExit(
                "SEAM_TRUSTED_PROXY=* trusts forwarded headers from any caller, "
                "but this is bound to %s. Pick one:\n"
                "  - bind to 127.0.0.1 and let the proxy reach it there; or\n"
                "  - name the proxy's address instead of *; or\n"
                "  - set SEAM_PROXY_ONLY=1 if nothing but the proxy can open "
                "this port, which is the case inside a container." % host)
        if TRUSTED_PROXY:
            kw = {"trusted_proxy": TRUSTED_PROXY,
                  "trusted_proxy_headers": {"x-forwarded-proto", "x-forwarded-for",
                                            "x-forwarded-host"},
                  # Anything not listed above is removed, so a header forged by
                  # a client cannot ride in behind the ones we do trust.
                  "clear_untrusted_proxy_headers": True}
            print("Seam: trusting forwarded headers from %s" % TRUSTED_PROXY)
        else:
            print("Seam: no SEAM_TRUSTED_PROXY set - forwarded headers are "
                  "ignored. Behind a TLS proxy, set it or cookies will not be "
                  "marked Secure.")
        print("Seam on http://%s:%d (waitress, %d threads)" % (host, port, 8))
        serve(app, host=host, port=port, threads=8, ident="Seam", **kw)
    else:
        if not serve:
            print("waitress is not installed - falling back to the development "
                  "server. Install it before putting this in front of anyone: "
                  "pip install waitress")
        app.run(host=host, port=port, debug=debug, threaded=True,
                use_reloader=os.environ.get("SEAM_RELOAD") == "1")
