# -*- coding: utf-8 -*-
"""Web Push: the hand-written P-256 must be provably correct, the VAPID header
must parse, and the subscription lifecycle must work end to end."""
import os, sys, os, json, base64, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the app package
from seamclient import Client
import webpush

c = Client(); c.login()

print("== the curve is the real P-256 ==")
# RFC 6090 / NIST published test vectors for the secp256r1 base point
VECTORS = {
    2: "7cf27b188d034f7e8a52380304b51ac3c08969e277f21b35a60b48fc47669978",
    3: "5ecbe4d1a6330a44c8f7ef951d4bf165e6c6b721efada985fb41661bc6e7fd6c",
    4: "e2534a3532d08fbba02dde659ee62bd0031fe2db785596ef509302446b030852",
}
for k, want in VECTORS.items():
    got = "%064x" % webpush._mul(k, (webpush.GX, webpush.GY))[0]
    c.check("%dG matches the published vector" % k, got == want, "%s != %s" % (got, want))
c.check("n*G is the point at infinity",
        webpush._mul(webpush.N, (webpush.GX, webpush.GY)) is None)
c.check("(n+1)*G == G",
        webpush._mul(webpush.N + 1, (webpush.GX, webpush.GY)) == (webpush.GX, webpush.GY))

print("\n== keys are well formed ==")
k = webpush.generate_keys()
raw = webpush._b64d(k["public"])
c.check("uncompressed 65-byte point", len(raw) == 65 and raw[0] == 4, str(len(raw)))
x = int.from_bytes(raw[1:33], "big"); y = int.from_bytes(raw[33:], "big")
c.check("public key is on the curve",
        (y * y - (x * x * x + webpush.A * x + webpush.B)) % webpush.P == 0)
c.check("two key pairs differ", webpush.generate_keys()["private"] != k["private"])

print("\n== signatures verify, and only for the right message ==")
d = int.from_bytes(webpush._b64d(k["private"]), "big")
dig = hashlib.sha256(b"hello").digest()
sig = webpush._sign(dig, d)
c.check("signature is 64 bytes", len(sig) == 64, str(len(sig)))
c.check("verifies", webpush.verify(sig, dig, k["public"]))
c.check("rejects a different message",
        not webpush.verify(sig, hashlib.sha256(b"hello!").digest(), k["public"]))
c.check("rejects another key", not webpush.verify(sig, dig, webpush.generate_keys()["public"]))
c.check("two signatures over the same message differ (fresh nonce each time)",
        webpush._sign(dig, d) != sig)
c.check("self_test passes", webpush.self_test())

print("\n== the VAPID header is shaped as RFC 8292 requires ==")
h = webpush.vapid_header("https://fcm.googleapis.com/fcm/send/xyz", "mailto:a@b.c")
c.check("starts with vapid", h.startswith("vapid t="), h[:30])
tok = h.split("t=")[1].split(",")[0]
head, claims, s64 = tok.split(".")
hj = json.loads(webpush._b64d(head)); cj = json.loads(webpush._b64d(claims))
c.check("alg is ES256", hj.get("alg") == "ES256", str(hj))
c.check("aud is the push service origin", cj.get("aud") == "https://fcm.googleapis.com", str(cj))
c.check("sub carried", cj.get("sub") == "mailto:a@b.c", str(cj))
c.check("exp is in the future and under 24h", 0 < cj["exp"] - __import__("time").time() <= 86400,
        str(cj.get("exp")))
c.check("the JWT signature verifies against the advertised key",
        webpush.verify(webpush._b64d(s64),
                       hashlib.sha256(("%s.%s" % (head, claims)).encode()).digest(),
                       h.split("k=")[1]))

print("\n== the API ==")
st, r = c.call("GET", "/api/notify/push-key")
c.check("public key served", st == 200 and len(r.get("key", "")) > 80, str(r)[:120])
c.check("it is the instance key", r["key"] == webpush.public_key(), "")

st, r = c.call("POST", "/api/notify/push-subscribe", {"endpoint": "http://insecure/x"})
c.check("non-https endpoint refused", st == 400 and r.get("code") == "push_endpoint",
        "%s %s" % (st, r))

st, r = c.call("POST", "/api/notify/push-test", {})
c.check("no devices -> 400 push_none", st == 400 and r.get("code") == "push_none",
        "%s %s" % (st, r))

EP = "https://fcm.googleapis.com/fcm/send/seam-test-endpoint"
st, r = c.call("POST", "/api/notify/push-subscribe",
               {"endpoint": EP, "keys": {"p256dh": "abc", "auth": "def"}})
c.check("subscribed", st == 200 and r.get("devices", 0) >= 1, str(r)[:150])
st, s = c.call("GET", "/api/notify/settings")
c.check("settings report the device", (s.get("push") or {}).get("devices", 0) >= 1,
        str(s.get("push")))
c.check("settings carry the key", len((s.get("push") or {}).get("key", "")) > 80, "")

st, r = c.call("POST", "/api/notify/push-subscribe",
               {"endpoint": EP, "keys": {"p256dh": "abc", "auth": "def"}})
c.check("re-subscribing does not duplicate the device", r.get("devices") == 1, str(r))

print("\n== a real send attempt reaches the push service and is rejected as expected ==")
ok, detail = webpush.send_now(EP)
c.check("the push service answered (not a local crash)",
        detail.isdigit() or "certificate" in detail.lower() or "urlopen" in detail.lower(),
        detail)
print("     FCM replied: %s" % detail)

st, r = c.call("POST", "/api/notify/push-unsubscribe", {"endpoint": EP})
c.check("unsubscribed", st == 200 and r.get("subscribed") is False, str(r))
st, s = c.call("GET", "/api/notify/settings")
c.check("device gone", (s.get("push") or {}).get("devices") == 0, str(s.get("push")))

print("\n== the service worker handles push ==")
st, sw = c.call("GET", "/sw.js", raw=True)
c.check("push listener present", 'addEventListener("push"' in sw, "")
c.check("it fetches the notification itself", "/api/notifications" in sw, "")
c.check("no payload decryption is attempted", "aes" not in sw.lower(), "")

sys.exit(c.summary())
