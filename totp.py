# -*- coding: utf-8 -*-
"""Time-based one-time passwords, RFC 6238.

Written against the standard rather than pulled in as a dependency, for the
same reason as the rest of this codebase: a company that self-hosts Seam should
be able to read every line that touches its credentials.

The algorithm is small enough to state in full. A shared secret is combined
with the number of 30-second steps since the Unix epoch through HMAC-SHA1
(RFC 4226 fixes SHA-1 here, and every authenticator app implements that);
four bytes are taken from a position the last nibble of the digest points at,
and the low 31 bits of those are reduced modulo 10^6.

Two things this module refuses to do quietly:
  - it never accepts the same step twice (`last_step` in the caller), because
    a code read over someone's shoulder is otherwise good for 30 seconds;
  - it compares with `hmac.compare_digest`, so a wrong code takes the same time
    as a right one.
"""
import hmac
import time
import base64
import struct
import hashlib
import secrets
import urllib.parse

DIGITS = 6
PERIOD = 30
#: One step either side, so a phone whose clock is slightly off still works.
#: Wider than this stops being a time-based code.
DRIFT = 1


def new_secret():
    """160 bits, the length RFC 4226 recommends, in the base32 every
    authenticator app expects."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _steps(at=None):
    return int((at if at is not None else time.time()) // PERIOD)


def code_at(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret, code, last_step=0, at=None):
    """Return the step the code belongs to, or None.

    The caller must persist the returned step and pass it back as `last_step`:
    that is what stops the same code being replayed while it is still inside
    its window.
    """
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    now = _steps(at)
    for step in range(now - DRIFT, now + DRIFT + 1):
        if step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def provisioning_uri(secret, account, issuer="Seam"):
    """The otpauth:// URI an authenticator app reads. Kept to the documented
    parameters only - apps ignore extras, and some choke on them."""
    label = urllib.parse.quote("%s:%s" % (issuer, account))
    q = urllib.parse.urlencode({
        "secret": secret, "issuer": issuer,
        "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD,
    })
    return "otpauth://totp/%s?%s" % (label, q)


def grouped(secret):
    """The secret in blocks of four, for someone typing it in by hand."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def self_test():
    """RFC 6238's published test vectors, on the SHA-1 secret it specifies
    ("12345678901234567890" in ASCII). If this fails, nothing else here is
    worth trusting."""
    secret = base64.b32encode(b"12345678901234567890").decode("ascii")
    vectors = [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
               (1234567890, "005924"), (2000000000, "279037")]
    out = []
    for ts, expected in vectors:
        got = code_at(secret, _steps(ts))
        out.append({"at": ts, "expected": expected, "got": got, "ok": got == expected})
    return {"ok": all(x["ok"] for x in out), "vectors": out}


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=1))
