# -*- coding: utf-8 -*-
"""Mock S3 + WebDAV target that verifies the SigV4 signature independently,
so the test proves the signing is correct rather than merely well-formed."""
import hmac, hashlib, json, urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

ACCESS, SECRET, REGION = "AKIATESTKEY", "testsecretkey", "eu-central-1"
RECEIVED = []


def signing_key(secret, date, region, service):
    def s(k, m):
        return hmac.new(k, m.encode(), hashlib.sha256).digest()
    return s(s(s(s(("AWS4" + secret).encode(), date), region), service), "aws4_request")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/__received":
            return self._send(200, json.dumps({"received": RECEIVED}).encode())
        if self.path == "/__reset":
            del RECEIVED[:]
            return self._send(200, b"{}")
        return self._send(404)

    def do_PUT(self):
        n = int(self.headers.get("content-length") or 0)
        data = self.rfile.read(n)
        auth = self.headers.get("Authorization") or ""

        if auth.startswith("Basic "):                       # WebDAV path
            RECEIVED.append({"mode": "webdav", "path": self.path, "bytes": len(data),
                             "auth": auth[:20]})
            return self._send(201)

        if not auth.startswith("AWS4-HMAC-SHA256 "):
            return self._send(403, b"no sigv4")
        try:
            fields = dict(p.strip().split("=", 1) for p in auth[17:].split(","))
            cred = fields["Credential"]
            signed = fields["SignedHeaders"]
            given = fields["Signature"]
            key_id, date, region, service, _ = cred.split("/")
            if key_id != ACCESS:
                return self._send(403, b"unknown key")

            amz_date = self.headers.get("x-amz-date")
            payload_hash = self.headers.get("x-amz-content-sha256")
            if payload_hash != hashlib.sha256(data).hexdigest():
                return self._send(400, b"payload hash mismatch")

            canon_headers = ""
            for h in signed.split(";"):
                canon_headers += "%s:%s\n" % (h, (self.headers.get(h) or "").strip())
            canonical = "\n".join(["PUT", urllib.parse.urlsplit(self.path).path, "",
                                   canon_headers, signed, payload_hash])
            sts = "\n".join(["AWS4-HMAC-SHA256", amz_date, "%s/%s/%s/aws4_request" %
                             (date, region, service),
                             hashlib.sha256(canonical.encode()).hexdigest()])
            expect = hmac.new(signing_key(SECRET, date, region, service),
                              sts.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expect, given):
                return self._send(403, b"SignatureDoesNotMatch")
        except Exception as e:
            return self._send(400, str(e).encode()[:100])

        RECEIVED.append({"mode": "s3", "path": self.path, "bytes": len(data),
                         "content_type": self.headers.get("content-type")})
        return self._send(200)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 5097), H).serve_forever()
