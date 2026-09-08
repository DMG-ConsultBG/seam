# -*- coding: utf-8 -*-
"""Minimal Cloud Signature Consortium API v2 server, used to prove Seam's CSC
adapter really drives the standard flow: oauth2/token -> credentials/list ->
credentials/info -> credentials/sendOTP -> credentials/authorize -> signHash."""
import json
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

STATE = {"otp_sent": False, "sad": None, "calls": []}
TOKEN = "mock-access-token"
CRED = "cred-ivan-petrov-qes"
OTP = "778899"
PIN = "1234"


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

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n).decode("utf-8")
        path = self.path
        STATE["calls"].append(path)

        if path.endswith("/oauth2/token"):
            q = parse_qs(raw)
            if q.get("client_id", [""])[0] != "seam-client" or q.get("client_secret", [""])[0] != "s3cr3t":
                return self._send(401, {"error": "invalid_client"})
            return self._send(200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600})

        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, {"error": "invalid_token"})
        d = json.loads(raw or "{}")

        if path.endswith("/credentials/list"):
            return self._send(200, {"credentialIDs": [CRED]})

        if path.endswith("/credentials/info"):
            return self._send(200, {
                "credentialID": d.get("credentialID"),
                "key": {"status": "enabled", "algo": ["1.2.840.113549.1.1.11"], "len": 2048},
                "cert": {"status": "valid",
                         "certificates": ["MIIC-mock-qualified-certificate"],
                         "issuerDN": "CN=Mock QTSP Qualified CA, C=BG",
                         "subjectDN": "CN=Ivan Petrov, serialNumber=PNOBG-8001010101, C=BG",
                         "validFrom": "20260101000000Z", "validTo": "20280101000000Z"},
                "OTP": {"presence": "true", "type": "online"},
                "multisign": 1, "authMode": "explicit"})

        if path.endswith("/credentials/sendOTP"):
            STATE["otp_sent"] = True
            return self._send(200, {})

        if path.endswith("/credentials/authorize"):
            if not STATE["otp_sent"]:
                return self._send(400, {"error": "otp_not_sent"})
            if d.get("PIN") != PIN:
                return self._send(400, {"error": "invalid_pin", "error_description": "Wrong PIN"})
            if d.get("OTP") != OTP:
                return self._send(400, {"error": "invalid_otp", "error_description": "Wrong OTP"})
            if not d.get("hash"):
                return self._send(400, {"error": "missing_hash"})
            STATE["sad"] = "mock-SAD-" + d["hash"][0][:12]
            return self._send(200, {"SAD": STATE["sad"], "expiresIn": 300})

        if path.endswith("/signatures/signHash"):
            if d.get("SAD") != STATE["sad"]:
                return self._send(400, {"error": "invalid_sad"})
            if d.get("hashAlgo") != "2.16.840.1.101.3.4.2.1":
                return self._send(400, {"error": "bad_hash_algo", "got": d.get("hashAlgo")})
            sig = base64.b64encode(("MOCK-SIGNATURE-OVER-" + d["hash"][0]).encode()).decode()
            return self._send(200, {"signatures": [sig]})

        return self._send(404, {"error": "not_found", "path": path})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 5099), H).serve_forever()
