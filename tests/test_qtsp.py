# -*- coding: utf-8 -*-
"""End-to-end check of the qualified-signature layer against the running app."""
import os, json, sys, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
CSRF = {"v": ""}
ok = fail = 0


def set_lang(lang):
    """Set seam_lang in the jar rather than as a raw header - a manual Cookie
    header would replace the session cookie the jar manages."""
    c = http.cookiejar.Cookie(0, "seam_lang", lang, None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    cj.set_cookie(c)


def call(method, path, data=None, raw=False, lang="bg"):
    set_lang(lang)
    url = BASE + path
    body = None
    hdr = {"accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        hdr["content-type"] = "application/json"
    if CSRF["v"]:
        hdr["X-CSRF"] = CSRF["v"]
    req = urllib.request.Request(url, data=body, headers=hdr, method=method)
    try:
        with op.open(req, timeout=30) as r:
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


print("== login ==")
st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
check("login 200", st == 200, str(r)[:200])
CSRF["v"] = r.get("csrf", "")

call("POST", "/api/qtsp", {"provider": ""})   # start from a clean slate

print("\n== GET /api/qtsp (provider registry for the org's country) ==")
st, q = call("GET", "/api/qtsp")
check("status 200", st == 200, str(q)[:200])
check("country present", bool(q.get("country")), str(q.get("country")))
keys = [p["key"] for p in q.get("providers", [])]
check("evrotrust offered", "evrotrust" in keys)
check("btrust offered", "btrust" in keys)
check("stampit offered", "stampit" in keys)
check("infonotary offered", "infonotary" in keys)
check("cross-border EU providers offered", any(not p["home"] for p in q["providers"]),
      "home-only: %d" % len(q["providers"]))
check("legal framework present", (q.get("legal") or {}).get("framework") == "eIDAS",
      str(q.get("legal"))[:150])
check("max level qualified for BG", q.get("max_level") == "qualified", str(q.get("max_level")))
check("no provider connected yet", q.get("config") is None, str(q.get("config"))[:120])
print("     country=%s providers=%d law=%s" % (q["country"], len(q["providers"]),
                                               (q.get("legal") or {}).get("law", "")[:60]))

print("\n== POST /api/qtsp: connect B-Trust with incomplete credentials ==")
st, r = call("POST", "/api/qtsp", {"provider": "btrust", "relying_party_id": "RP-TEST-001"})
check("saved 200", st == 200, str(r)[:200])
cfg = r.get("config") or {}
check("not ready (cert missing)", cfg.get("ready") is False, str(cfg)[:200])
check("missing lists the cert", "client_cert" in (cfg.get("missing") or []), str(cfg.get("missing")))
check("secret not echoed back", "relying_party_id" not in cfg, str(cfg.keys()))
check("secrets_set flags rp id", (cfg.get("secrets_set") or {}).get("relying_party_id") is True,
      str(cfg.get("secrets_set")))

print("\n== unknown provider is rejected ==")
st, r = call("POST", "/api/qtsp", {"provider": "definitely-not-a-qtsp"})
check("400 qtsp_unknown", st == 400 and r.get("code") == "qtsp_unknown", "%s %s" % (st, r))

print("\n== blank secret keeps the stored value ==")
st, r = call("POST", "/api/qtsp", {"provider": "btrust", "relying_party_id": ""})
check("still stored", ((r.get("config") or {}).get("secrets_set") or {}).get("relying_party_id") is True,
      str(r.get("config"))[:200])

print("\n== connect Evrotrust (api_key only) ==")
st, r = call("POST", "/api/qtsp", {"provider": "evrotrust", "api_key": "test-key-not-real"})
cfg = r.get("config") or {}
check("ready", cfg.get("ready") is True, str(cfg)[:200])
check("name resolved", cfg.get("name") == "Evrotrust", str(cfg.get("name")))

print("\n== qualified signature request routes to the provider ==")
st, wss = call("GET", "/api/workspaces")
ws_list = wss.get("workspaces") if isinstance(wss, dict) else wss
st, ords = call("GET", "/api/workspaces/%d/orders" % ws_list[0]["id"])
o_list = ords.get("orders") if isinstance(ords, dict) else ords
oid = o_list[0]["id"] if o_list else None
check("have an order to sign", oid is not None, str(ords)[:150])
st, sig = call("POST", "/api/signatures", {"order_id": oid, "level": "qualified",
                                           "signer_name": "Иван Петров",
                                           "signer_email": "ivan@example.com"})
check("signature created", st == 200, str(sig)[:250])
s = sig.get("signature") or sig
tok = s.get("token") or ""
check("level is qualified", s.get("level") == "qualified", str(s.get("level")))
check("has document hash", len(s.get("doc_hash") or "") == 64, str(s.get("doc_hash")))

print("\n== /api/sign/<token>/qtsp reports the provider to the signer ==")
if tok:
    st, qs = call("GET", "/api/sign/%s/qtsp" % tok)
    check("status 200", st == 200, str(qs)[:200])
    check("provider is evrotrust", qs.get("provider") == "evrotrust", str(qs.get("provider")))
    check("api mode native", qs.get("api") == "native", str(qs.get("api")))
    check("hash matches", qs.get("hash") == s.get("doc_hash"), "")
    print("     state=%s name=%s" % (qs.get("state"), qs.get("name")))

    print("\n== signing page renders the qualified panel ==")
    st, html = call("GET", "/sign/%s" % tok, raw=True)
    check("page 200", st == 200, str(st))
    check("panel present", 'class="qbox"' in html, "")
    check("provider named", "Evrotrust" in html, "")
    check("governing law shown", "910/2014" in html or "ЗЕДЕУУ" in html, "")
    check("no e-mail OTP row for provider-backed qualified", 'id="otp_send"' not in html, "")
    check("bulgarian copy", "Квалифициран подпис" in html, "")

    print("\n== hash-mismatch is refused on the detached path ==")
    st, r = call("POST", "/api/sign/%s/qtsp/detached" % tok,
                 {"signature": "A" * 200, "signed_hash": "deadbeef" * 8})
    check("400 sig_hash_mismatch", st == 400 and r.get("code") == "sig_hash_mismatch", "%s %s" % (st, r))

    print("\n== signing is blocked until the provider finishes ==")
    st, r = call("POST", "/api/signatures/%d/sign" % s["id"],
                 {"token": tok, "name": "Иван Петров", "consent": True})
    check("400 sig_qtsp_pending", st == 400 and r.get("code") == "sig_qtsp_pending", "%s %s" % (st, r))

print("\n== signing page in other languages ==")
for lg, needle in (("en", "Qualified signature"), ("de", "Qualifizierte Signatur"),
                   ("pl", "Podpis kwalifikowany"), ("el", "Εγκεκριμένη υπογραφή"),
                   ("pt", "Assinatura qualificada")):
    st, html = call("GET", "/sign/%s" % tok, raw=True, lang=lg)
    check("sign page %s" % lg, st == 200 and needle in html, needle)

print("\n== disconnect ==")
st, r = call("POST", "/api/qtsp", {"provider": ""})
check("cleared", st == 200 and r.get("config") is None, str(r)[:150])

print("\n---- %d passed, %d failed ----" % (ok, fail))
sys.exit(1 if fail else 0)
