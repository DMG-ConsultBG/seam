# -*- coding: utf-8 -*-
"""
Web Push with VAPID, without leaving the standard library.

Two decisions make this possible in a dependency-free backend:

1. **VAPID needs one signature, not a crypto suite.** The application server
   proves who it is with an ES256 (ECDSA over NIST P-256) signature on a small
   JWT. That is modular arithmetic on a fixed curve - implemented below - not a
   TLS stack.

2. **Pushes carry no payload.** RFC 8030 allows a "tickle": a push with an empty
   body. The service worker wakes, then fetches the notification list over the
   session it already has. That removes the need for ECDH + HKDF + AES-GCM
   payload encryption, which is the part no one should hand-roll. It also means
   no notification content ever passes through the push service.

The private key identifies this Seam instance to the push service. It signs
nothing that belongs to a user and encrypts nothing; the failure mode of a bad
signature is "the push is rejected", not disclosure. Keys live in ".vapid_keys"
(mode 0600 where the platform supports it) and are generated on first use.
"""
import os
import json
import time
import hashlib
import secrets
import base64
import threading
import urllib.parse
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(BASE, ".vapid_keys")

# ---------------------------------------------------------------------------
# NIST P-256 (secp256r1)
# ---------------------------------------------------------------------------
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551


def _inv(x, m):
    return pow(x, -1, m)


def _add(p1, p2):
    """Point addition in affine coordinates; None is the point at infinity."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1 + A) * _inv(2 * y1, P) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _mul(k, point):
    """Double-and-add. Not constant time: the only secret multiplied here is the
    per-signature nonce and the instance key, on a server that is not a shared
    host. Stated plainly rather than left implied."""
    result, addend = None, point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(txt):
    txt = txt.strip()
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
def generate_keys():
    d = secrets.randbelow(N - 1) + 1
    x, y = _mul(d, (GX, GY))
    public = b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")
    return {"private": _b64(d.to_bytes(32, "big")), "public": _b64(public)}


def keys():
    """Load the instance key pair, creating it on first use."""
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, "r", encoding="utf-8") as f:
                k = json.load(f)
            if k.get("private") and k.get("public"):
                return k
        except Exception:
            pass
    k = generate_keys()
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump(k, f)
    import cryptobox
    cryptobox.secure_file(KEY_FILE)
    return k


def public_key():
    return keys()["public"]


# ---------------------------------------------------------------------------
# ES256 / VAPID
# ---------------------------------------------------------------------------
def _sign(digest, d):
    e = int.from_bytes(digest, "big")
    while True:
        k = secrets.randbelow(N - 1) + 1        # never reuse: k reuse leaks d
        pt = _mul(k, (GX, GY))
        if pt is None:
            continue
        r = pt[0] % N
        if r == 0:
            continue
        s = _inv(k, N) * (e + r * d) % N
        if s == 0:
            continue
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def vapid_header(endpoint, subject):
    """The Authorization header a push service expects (RFC 8292)."""
    k = keys()
    d = int.from_bytes(_b64d(k["private"]), "big")
    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": "%s://%s" % (parts.scheme, parts.netloc),
              "exp": int(time.time()) + 12 * 3600,
              "sub": subject or "mailto:admin@localhost"}
    signing_input = "%s.%s" % (
        _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()),
        _b64(json.dumps(claims, separators=(",", ":")).encode()))
    sig = _sign(hashlib.sha256(signing_input.encode("ascii")).digest(), d)
    return "vapid t=%s.%s, k=%s" % (signing_input, _b64(sig), k["public"])


def verify(signature, digest, public):
    """Self-check used by the test button - the maths must round-trip before a
    hand-written curve implementation is trusted with anything."""
    raw = _b64d(public)
    if len(raw) != 65 or raw[0] != 4:
        return False
    qx = int.from_bytes(raw[1:33], "big")
    qy = int.from_bytes(raw[33:], "big")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    e = int.from_bytes(digest, "big")
    w = _inv(s, N)
    pt = _add(_mul(e * w % N, (GX, GY)), _mul(r * w % N, (qx, qy)))
    return pt is not None and pt[0] % N == r


def self_test():
    k = generate_keys()
    d = int.from_bytes(_b64d(k["private"]), "big")
    digest = hashlib.sha256(b"seam-webpush-self-test").digest()
    sig = _sign(digest, d)
    ok = verify(sig, digest, k["public"])
    tampered = verify(sig, hashlib.sha256(b"different").digest(), k["public"])
    return ok and not tampered


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def send_now(endpoint, subject="mailto:admin@localhost", ttl=3600):
    """Data-less push. Returns (ok, detail); 404/410 means the subscription is
    gone and the caller should drop it."""
    try:
        req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
            "TTL": str(ttl),
            "Content-Length": "0",
            "Authorization": vapid_header(endpoint, subject),
            "user-agent": "Seam/1.0",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            return True, str(r.status)
    except urllib.error.HTTPError as e:
        return False, str(e.code)
    except Exception as e:
        return False, str(e)[:150]


def send(endpoint, subject="mailto:admin@localhost", on_gone=None):
    """Fire and forget, off the request path."""
    def run():
        ok, detail = send_now(endpoint, subject)
        if not ok and detail in ("404", "410") and on_gone:
            try:
                on_gone(endpoint)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()
