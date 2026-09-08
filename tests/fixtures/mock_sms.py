# -*- coding: utf-8 -*-
"""Mock SMS gateway: accepts both the bearer-JSON and the form shape, and
records what it received so the test can assert on the real payload."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

RECEIVED = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/__received":
            return self._send(200, {"received": RECEIVED})
        if self.path == "/__reset":
            del RECEIVED[:]
            return self._send(200, {"ok": True})
        return self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        ctype = self.headers.get("content-type") or ""
        if "json" in ctype:
            if self.headers.get("Authorization") != "Bearer test-key":
                return self._send(401, {"error": "bad token"})
            data = json.loads(raw or "{}")
        else:
            data = {k: v[0] for k, v in parse_qs(raw).items()}
        RECEIVED.append({"path": self.path, "data": data})
        return self._send(200, {"id": "mock-%d" % len(RECEIVED), "status": "queued"})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 5098), H).serve_forever()
