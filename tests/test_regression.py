# -*- coding: utf-8 -*-
"""Broad regression across the app, with the CSRF boundary checked explicitly:
the signing endpoints are exempt (they authenticate by signing token), and
everything else must still be rejected without the header."""
import os, json, sys, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
CSRF = {"v": ""}
ok = fail = 0


def call(method, path, data=None, raw=False, lang="bg", csrf=True):
    c = http.cookiejar.Cookie(0, "seam_lang", lang, None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    cj.set_cookie(c)
    b = json.dumps(data).encode("utf-8") if data is not None else None
    h = {"accept": "application/json"}
    if b:
        h["content-type"] = "application/json"
    if csrf and CSRF["v"]:
        h["X-CSRF"] = CSRF["v"]
    try:
        r = op.open(urllib.request.Request(BASE + path, data=b, headers=h, method=method), timeout=25)
        t = r.read().decode("utf-8", "ignore")
        return r.status, (t if raw else json.loads(t or "{}"))
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "ignore")
        try:
            return e.code, (t if raw else json.loads(t or "{}"))
        except ValueError:
            return e.code, t


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        fail += 1
        print("  FAIL  %s  %s" % (name, detail))


print("== auth ==")
st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
CSRF["v"] = r.get("csrf", "")
check("login", st == 200 and bool(CSRF["v"]), str(r)[:150])
st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "wrong"})
check("bad password rejected", st in (400, 401, 429), str(st))

print("\n== CSRF boundary ==")
st, r = call("POST", "/api/qtsp", {"provider": ""}, csrf=False)
check("write without X-CSRF is refused", st == 403 and r.get("code") == "csrf", "%s %s" % (st, r))
st, r = call("POST", "/api/qtsp", {"provider": ""}, csrf=True)
check("write with X-CSRF succeeds", st == 200, str(r)[:150])

print("\n== core reads ==")
for path, key in (("/api/me", "user"), ("/api/workspaces", None), ("/api/pending", None),
                  ("/api/notifications", None), ("/api/templates", None),
                  ("/api/compliance", None), ("/api/pricing", None),
                  ("/api/store/products", None), ("/api/integration/webhooks", None),
                  ("/api/qtsp", "providers")):
    st, r = call("GET", path)
    check("GET %s" % path, st == 200 and (key is None or key in r), "%s %s" % (st, str(r)[:90]))

print("\n== workspace and orders ==")
st, wss = call("GET", "/api/workspaces")
ws = (wss.get("workspaces") if isinstance(wss, dict) else wss)[0]
st, ords = call("GET", "/api/workspaces/%d/orders" % ws["id"])
orders = ords.get("orders") if isinstance(ords, dict) else ords
check("orders listed", st == 200 and len(orders) > 0, str(st))
oid = orders[0]["id"]
st, o = call("GET", "/api/orders/%d" % oid)
check("order detail", st == 200, str(st))
st, f = call("GET", "/api/orders/%d/feed" % oid)
check("order feed", st == 200, str(st))
st, m = call("GET", "/api/workspaces/%d/messages" % ws["id"])
check("workspace messages", st == 200, str(st))
st, ct = call("GET", "/api/workspaces/%d/contacts" % ws["id"])
check("emergency contacts", st == 200, str(st))

print("\n== documents render in every language ==")
for lg in ("bg", "en", "de", "ro", "el", "tr", "it", "ru", "es", "fr", "pl", "uk", "pt"):
    st, h = call("GET", "/docs/nda", raw=True, lang=lg)
    check("docs/nda %s" % lg, st in (200, 402) and len(h) > 500, str(st))

print("\n== printable pages ==")
st, h = call("GET", "/protocol/%d" % oid, raw=True)
check("acceptance protocol", st == 200, str(st))
st, h = call("GET", "/api/passport/%d" % ws["buyer_org_id"] if "buyer_org_id" in ws else "/api/me")
check("passport reachable", st in (200, 403), str(st))

print("\n== signatures still work at every level ==")
for lvl in ("simple", "advanced"):
    st, sig = call("POST", "/api/signatures", {"order_id": oid, "level": lvl,
                                               "signer_name": "Регресионен тест"})
    s = sig.get("signature") or sig
    check("%s request created" % lvl, st == 200 and s.get("level") == lvl, str(sig)[:150])
    st, h = call("GET", "/sign/%s" % s["token"], raw=True)
    check("%s page renders" % lvl, st == 200 and "sign.js" in h, str(st))
    check("%s page has no inline script" % lvl, "<script>" not in h, "")

print("\n== qualified without a provider stays honest ==")
call("POST", "/api/qtsp", {"provider": ""})
st, sig = call("POST", "/api/signatures", {"order_id": oid, "level": "qualified",
                                           "signer_name": "Без доставчик"})
s = sig.get("signature") or sig
st, h = call("GET", "/sign/%s" % s["token"], raw=True)
check("warns no provider is connected", "Няма свързан доставчик" in h, "")
check("falls back to Seam's own e-mail code", 'id="otp_send"' in h, "")

print("\n== i18n assets ==")
for f in ("/static/js/i18n.js", "/static/js/app.js", "/static/js/api.js",
          "/static/js/sign.js", "/static/js/docguard.js", "/static/css/styles.css"):
    st, t = call("GET", f, raw=True)
    check("asset %s" % f, st == 200 and len(t) > 100, str(st))

print("\n---- %d passed, %d failed ----" % (ok, fail))
sys.exit(1 if fail else 0)
