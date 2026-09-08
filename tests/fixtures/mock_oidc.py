# -*- coding: utf-8 -*-
"""A minimal OpenID Connect provider, so the relying party in eid.py is tested
against a real protocol exchange rather than a stub.

It publishes a discovery document and a JWKS, issues authorization codes, and
signs RS256 ID tokens with a key it generates at start-up. It can also be told
to misbehave - wrong nonce, wrong audience, expired, bad signature - because
those are the cases the verification exists for.
"""
import os
import sys
import json
import time
import base64
import hashlib
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(HERE))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "seam"))
import eid                                            # noqa: E402  (path set above)

PORT = int(os.environ.get("MOCK_OIDC_PORT", "5096"))
BASE = "http://127.0.0.1:%d" % PORT
CLIENT_ID = "seam-test-client"
CLIENT_SECRET = "seam-test-secret"

_e = 65537
_p = eid._probable_prime(512)
_q = eid._probable_prime(512)
while _p == _q or (_p - 1) % _e == 0 or (_q - 1) % _e == 0:
    _q = eid._probable_prime(512)
_n = _p * _q
_d = pow(_e, -1, (_p - 1) * (_q - 1))
_K = (_n.bit_length() + 7) // 8
KID = "mock-1"

#: code -> {nonce, sub, name, quirk}
CODES = {}
LOCK = threading.Lock()


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign(payload, alg="RS256"):
    head = b64u(json.dumps({"alg": alg, "typ": "JWT", "kid": KID}).encode())
    body = b64u(json.dumps(payload).encode())
    signed = (head + "." + body).encode()
    digest = hashlib.sha256(signed).digest()
    em = b"\x00\x01" + b"\xff" * (_K - 3 - len(eid._ASN1["SHA-256"]) - len(digest)) \
        + b"\x00" + eid._ASN1["SHA-256"] + digest
    sig = pow(int.from_bytes(em, "big"), _d, _n).to_bytes(_K, "big")
    return head + "." + body + "." + b64u(sig)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/.well-known/openid-configuration":
            return self._json({
                "issuer": BASE,
                "authorization_endpoint": BASE + "/authorize",
                "token_endpoint": BASE + "/token",
                "jwks_uri": BASE + "/jwks",
            })
        if u.path == "/jwks":
            return self._json({"keys": [{
                "kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
                "n": b64u(_n.to_bytes(_K, "big")), "e": b64u(b"\x01\x00\x01")}]})
        if u.path == "/authorize":
            code = secrets.token_urlsafe(12)
            with LOCK:
                # `sub` and `quirk` are extras the test appends to the URL; a
                # real provider ignores parameters it does not know, and so
                # does this one for everything else.
                CODES[code] = {"nonce": q.get("nonce", ""), "sub": q.get("sub") or "citizen-1",
                               "challenge": q.get("code_challenge", ""),
                               "quirk": q.get("quirk", "")}
            # The test drives the browser itself, so hand the code back as JSON
            # instead of redirecting.
            return self._json({"code": code, "state": q.get("state", "")})
        if u.path == "/reset":
            with LOCK:
                CODES.clear()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("content-length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        if u.path != "/token":
            return self._json({"error": "not found"}, 404)
        with LOCK:
            rec = CODES.pop(form.get("code", ""), None)
        if not rec:
            return self._json({"error": "invalid_grant"}, 400)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return self._json({"error": "invalid_client"}, 401)
        # PKCE: the verifier must hash to the challenge sent at /authorize.
        want = b64u(hashlib.sha256((form.get("code_verifier") or "").encode()).digest())
        if rec["challenge"] and want != rec["challenge"]:
            return self._json({"error": "invalid_grant", "detail": "pkce"}, 400)

        quirk = rec["quirk"]
        now = int(time.time())
        claims = {"iss": BASE, "aud": CLIENT_ID, "sub": rec["sub"], "iat": now,
                  "exp": now + 300, "nonce": rec["nonce"], "name": "Test Citizen"}
        if quirk == "nonce":
            claims["nonce"] = "wrong"
        elif quirk == "aud":
            claims["aud"] = "somebody-else"
        elif quirk == "exp":
            claims["exp"] = now - 3600
        elif quirk == "iss":
            claims["iss"] = "https://evil.example"
        token = sign(claims)
        if quirk == "sig":
            token = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        return self._json({"access_token": "at", "token_type": "Bearer", "id_token": token})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
