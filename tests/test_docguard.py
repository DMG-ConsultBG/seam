# -*- coding: utf-8 -*-
"""The free-plan document guard was an inline <script>, which the pages' own
CSP (script-src 'self') blocked - so it never ran. Confirm it is now external
and actually reachable, and that no inline script is left on these pages."""
import os, json, sys, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
CSRF = {"v": ""}
ok = fail = 0


def call(method, path, data=None, raw=False):
    c = http.cookiejar.Cookie(0, "seam_lang", "bg", None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    cj.set_cookie(c)
    b = json.dumps(data).encode("utf-8") if data is not None else None
    h = {"accept": "application/json"}
    if b:
        h["content-type"] = "application/json"
    if CSRF["v"]:
        h["X-CSRF"] = CSRF["v"]
    try:
        r = op.open(urllib.request.Request(BASE + path, data=b,
                                           headers=h, method=method), timeout=20)
        t = r.read().decode("utf-8", "ignore")
        return r.status, (t if raw else json.loads(t or "{}"))
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "ignore")
        return e.code, (t if raw else (json.loads(t) if t.startswith("{") else t))


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        fail += 1
        print("  FAIL  %s  %s" % (name, detail))


st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
CSRF["v"] = r.get("csrf", "")
check("login", st == 200)

st, e = call("GET", "/api/docs/entitlement")
print("     plan=%s used=%s" % (e.get("plan"), e.get("used")))

st, h = call("GET", "/docs/nda", raw=True)
check("document renders", st in (200, 402), str(st))
check("no inline <script> left on document pages", "<script>" not in h, "")
if "wm" in h:
    check("free sheet loads the external guard", "/static/js/docguard.js" in h, "")

st, js = call("GET", "/static/js/docguard.js", raw=True)
check("docguard.js is served", st == 200 and "contextmenu" in js, str(st))
st, js = call("GET", "/static/js/sign.js", raw=True)
check("sign.js is served", st == 200 and "sign-cfg" in js, str(st))

st, h = call("GET", "/protocol/1", raw=True)
check("protocol page has no inline script", st in (200, 402, 403, 404) and "<script>" not in h, str(st))

print("\n---- %d passed, %d failed ----" % (ok, fail))
sys.exit(1 if fail else 0)
