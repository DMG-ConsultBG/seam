# -*- coding: utf-8 -*-
"""Just enough Chrome DevTools Protocol to drive a headless browser.

Why by hand: this project has no dependencies and is not going to grow one for
marketing screenshots. CDP is a JSON request/response protocol over a
WebSocket, and the WebSocket handshake plus client framing is about eighty
lines - the same trade already made for VAPID, SigV4 and OIDC elsewhere here.

Only what the capture script needs is implemented: navigate, evaluate, resize,
screenshot. No fragmentation, no compression extensions, no server-to-client
masking, because a local debugging endpoint sends none of those.
"""
import os
import ssl                                                  # noqa: F401
import json
import time
import base64
import socket
import struct
import secrets
import subprocess
import urllib.request

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pdfout                                               # noqa: E402


class Browser(object):
    """A headless browser with one page open."""

    def __init__(self, port=9222, width=1280, height=800, timeout=30):
        self.exe = pdfout.find_renderer()
        if not self.exe:
            raise RuntimeError("no Chrome or Edge on this machine")
        self.port = port
        self.timeout = timeout
        self.profile = os.path.join(os.environ.get("TEMP", "."), "seam-shoot-%d" % port)
        self.proc = subprocess.Popen(
            [self.exe, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--hide-scrollbars",
             "--remote-debugging-port=%d" % port,
             "--user-data-dir=" + self.profile,
             "--window-size=%d,%d" % (width, height),
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sock = None
        self._id = 0
        self._connect()
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.resize(width, height)

    # -- connection ---------------------------------------------------------
    def _target(self):
        deadline = time.time() + self.timeout
        last = None
        while time.time() < deadline:
            try:
                raw = urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % self.port, timeout=2).read()
                for t in json.loads(raw.decode("utf-8")):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception as e:                          # noqa: BLE001
                last = e
            time.sleep(0.25)
        raise RuntimeError("the browser never opened a debugging port (%s)" % last)

    def _connect(self):
        url = self._target()
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
                           "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n"
                           % (path, hostport, key)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise RuntimeError("the debugging endpoint closed during the handshake")
            head += chunk
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError("the debugging endpoint refused to upgrade: %r" % head[:120])
        self._buf = b""

    # -- framing ------------------------------------------------------------
    def _send_frame(self, payload):
        data = payload.encode("utf-8")
        head = bytearray([0x81])                            # FIN + text
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = secrets.token_bytes(4)                       # clients must mask
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("the browser closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        b0, b1 = self._recv(2)
        opcode = b0 & 0x0F
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv(8))[0]
        if b1 & 0x80:                                       # a server should not mask
            mask = self._recv(4)
            body = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv(n)))
        else:
            body = self._recv(n)
        if opcode == 0x8:                                   # close
            raise RuntimeError("the browser asked to close the connection")
        if opcode == 0x9:                                   # ping -> pong
            self.sock.sendall(b"\x8a\x80" + secrets.token_bytes(4))
            return None
        return body.decode("utf-8", "replace") if opcode == 0x1 else None

    # -- protocol -----------------------------------------------------------
    def send(self, method, **params):
        self._id += 1
        want = self._id
        self._send_frame(json.dumps({"id": want, "method": method, "params": params}))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            text = self._read_frame()
            if not text:
                continue
            msg = json.loads(text)
            if msg.get("id") != want:
                continue                                    # an event, not our answer
            if "error" in msg:
                raise RuntimeError("%s: %s" % (method, msg["error"].get("message")))
            return msg.get("result", {})
        raise RuntimeError("%s timed out" % method)

    # -- the four things a screenshot run needs -----------------------------
    def resize(self, width, height, mobile=False, scale=2):
        self.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                  deviceScaleFactor=scale, mobile=bool(mobile))

    def go(self, url, settle=1.2):
        """Navigate and wait for *this* document, not merely for a document.

        about:blank is already `complete`, so waiting on readyState alone hands
        back the blank page the browser started on - and then everything after
        it fails with an opaque-origin error that says nothing about the cause.

        Waiting only for "not about:blank" has the same fault one step later:
        from the second navigation onwards the previous page satisfies it, so
        the wait collapsed into a plain sleep and whatever ran next read the
        page before. An audit blamed three pages for missing a rule they had,
        because it was still looking at the one before them.
        """
        want = url.split("#")[0]
        self.send("Page.navigate", url=url)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            here = (self.js("location.href") or "").split("#")[0]
            if here == want and self.js("document.readyState") == "complete":
                break
            time.sleep(0.15)
        time.sleep(settle)

    def hash(self, route, settle=1.4):
        """Move inside the single-page app without reloading it, which is what
        clicking its own navigation does."""
        self.js("location.hash = %s" % json.dumps(route))
        time.sleep(settle)

    def js(self, expression, wait=False):
        r = self.send("Runtime.evaluate", expression=expression, returnByValue=True,
                      awaitPromise=bool(wait), userGesture=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            ex = (d.get("exception") or {})
            raise RuntimeError("script failed: %s %s"
                               % (d.get("text"), ex.get("description") or ex.get("value") or ""))
        return r.get("result", {}).get("value")

    def wait_ready(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js("document.readyState") == "complete":
                return True
            time.sleep(0.15)
        return False

    def wait_for(self, selector, timeout=15):
        """Wait for something to exist and have content - screenshots taken a
        beat too early are the main way a marketing shot ends up showing a
        spinner."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js("!!document.querySelector(%s)" % json.dumps(selector)):
                return True
            time.sleep(0.15)
        return False

    def shot(self, path, full=False):
        r = self.send("Page.captureScreenshot", format="png",
                      captureBeyondViewport=bool(full))
        data = base64.b64decode(r["data"])
        with open(path, "wb") as f:
            f.write(data)
        return len(data)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:                               # noqa: BLE001
                self.proc.kill()
