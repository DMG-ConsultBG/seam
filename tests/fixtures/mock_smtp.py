# -*- coding: utf-8 -*-
"""A mail server that keeps everything and delivers nothing.

Enough SMTP to accept a message from Seam and hold it, so the whole mail path
can be proved without a provider account and without a single letter reaching a
real mailbox. `smtpd` was removed from the standard library in 3.12 and this
project ships no dependencies, so the protocol is spoken directly - it is nine
verbs and a full stop on its own line.

    python tests/fixtures/mock_smtp.py [port]

What it took is readable over HTTP on the port *below* the SMTP one - 5096 is
already the OIDC mock, and two fixtures fighting over a port fails in a way
that looks like a product bug:
    GET /messages   -> everything received, newest last
    POST /reset     -> forget it all
"""
import re
import sys
import json
import base64
import socket
import threading
import email
from email import policy
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5095
INBOX = []
LOCK = threading.Lock()
#: Whatever the client sends. It is checked for presence, never for value: this
#: exists to prove Seam authenticates, not to be a real authority.
ACCEPT_ANY_LOGIN = True


def _decode(payload):
    msg = email.message_from_string(payload, policy=policy.default)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/html", "text/plain"):
                body = part.get_content()
                break
    else:
        body = msg.get_content()
    return {"from": str(msg.get("From") or ""), "to": str(msg.get("To") or ""),
            "subject": str(msg.get("Subject") or ""), "body": body}


def _session(conn, addr):
    """One SMTP conversation. Deliberately literal: every reply is the code the
    client is waiting for, and nothing else is claimed."""
    f = conn.makefile("rwb", buffering=0)

    def say(line):
        f.write((line + "\r\n").encode("utf-8", "replace"))

    def read():
        raw = f.readline()
        return raw.decode("utf-8", "replace").rstrip("\r\n") if raw else ""

    say("220 seam-mock ESMTP")
    state = {"from": "", "rcpt": [], "auth": ""}
    try:
        while True:
            line = read()
            if not line:
                break
            verb = line.split(" ", 1)[0].upper()

            if verb in ("HELO",):
                say("250 seam-mock")
            elif verb == "EHLO":
                # STARTTLS is deliberately NOT advertised. Seam asks for it and
                # aborts when refused rather than sending the password in the
                # clear, so a mock that offered it would be testing a path that
                # never runs here.
                say("250-seam-mock")
                say("250-AUTH PLAIN LOGIN")
                say("250 SIZE 33554432")
            elif verb == "STARTTLS":
                say("454 TLS not available")
            elif verb == "AUTH":
                # Both spellings; Seam uses whatever smtplib picks.
                if "LOGIN" in line.upper():
                    say("334 " + base64.b64encode(b"Username:").decode())
                    state["auth"] = read()
                    say("334 " + base64.b64encode(b"Password:").decode())
                    read()
                else:
                    parts = line.split(" ")
                    state["auth"] = parts[2] if len(parts) > 2 else read()
                say("235 authenticated" if ACCEPT_ANY_LOGIN else "535 no")
            elif verb == "MAIL":
                m = re.search(r"<([^>]*)>", line)
                state["from"] = m.group(1) if m else ""
                say("250 sender ok")
            elif verb == "RCPT":
                m = re.search(r"<([^>]*)>", line)
                if m:
                    state["rcpt"].append(m.group(1))
                say("250 recipient ok")
            elif verb == "DATA":
                say("354 go ahead")
                buf = []
                while True:
                    ln = read()
                    if ln == ".":
                        break
                    # Dot-stuffing, per the protocol.
                    buf.append(ln[1:] if ln.startswith("..") else ln)
                item = _decode("\r\n".join(buf))
                item.update(envelope_from=state["from"], envelope_to=state["rcpt"],
                            authenticated=bool(state["auth"]))
                with LOCK:
                    INBOX.append(item)
                state = {"from": "", "rcpt": [], "auth": state["auth"]}
                say("250 queued")
            elif verb == "RSET":
                state = {"from": "", "rcpt": [], "auth": ""}
                say("250 reset")
            elif verb == "NOOP":
                say("250 ok")
            elif verb == "QUIT":
                say("221 bye")
                break
            else:
                say("502 not implemented")
    except (OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def smtp_loop(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(20)
    while True:
        conn, addr = s.accept()
        threading.Thread(target=_session, args=(conn, addr), daemon=True).start()


class Peek(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/messages"):
            with LOCK:
                self._send({"count": len(INBOX), "messages": list(INBOX)})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path.startswith("/reset"):
            with LOCK:
                del INBOX[:]
            self._send({"ok": True})
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=smtp_loop, args=(PORT,), daemon=True).start()
    print("mock SMTP on 127.0.0.1:%d, inbox on http://127.0.0.1:%d/messages"
          % (PORT, PORT - 1))
    HTTPServer(("127.0.0.1", PORT - 1), Peek).serve_forever()
