# -*- coding: utf-8 -*-
"""Drive the full CSC v2 qualified-signing flow through Seam against the mock QTSP,
then finish the signature and verify the evidence certificate names the provider."""
import os, json, sys, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
CSRF = {"v": ""}
ok = fail = 0


def set_lang(lang):
    c = http.cookiejar.Cookie(0, "seam_lang", lang, None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    cj.set_cookie(c)


def call(method, path, data=None, raw=False, lang="bg"):
    set_lang(lang)
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdr = {"accept": "application/json"}
    if body:
        hdr["content-type"] = "application/json"
    if CSRF["v"]:
        hdr["X-CSRF"] = CSRF["v"]
    req = urllib.request.Request(BASE + path, data=body, headers=hdr, method=method)
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


st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
CSRF["v"] = r.get("csrf", "")
check("login", st == 200)

print("\n== connect a CSC provider (Certum, pointed at the mock QTSP) ==")
st, r = call("POST", "/api/qtsp", {"provider": "certum", "base_url": "http://127.0.0.1:5099/csc/v2",
                                   "client_id": "seam-client", "client_secret": "s3cr3t"})
cfg = r.get("config") or {}
check("saved and ready", st == 200 and cfg.get("ready") is True, str(r)[:200])

print("\n== connection test performs a real CSC handshake ==")
st, r = call("POST", "/api/qtsp/test")
check("test ok", st == 200 and r.get("ok") is True, str(r)[:200])
check("credential discovered", r.get("credentials") == 1, str(r))

print("\n== wrong secret is reported, not swallowed ==")
st, r = call("POST", "/api/qtsp", {"provider": "certum", "base_url": "http://127.0.0.1:5099/csc/v2",
                                   "client_id": "seam-client", "client_secret": "wrong"})
st, r = call("POST", "/api/qtsp/test")
check("502 qtsp auth failure", st == 502, "%s %s" % (st, str(r)[:160]))
call("POST", "/api/qtsp", {"provider": "certum", "base_url": "http://127.0.0.1:5099/csc/v2",
                           "client_id": "seam-client", "client_secret": "s3cr3t"})

print("\n== request a qualified signature ==")
st, wss = call("GET", "/api/workspaces")
ws = (wss.get("workspaces") if isinstance(wss, dict) else wss)[0]
st, ords = call("GET", "/api/workspaces/%d/orders" % ws["id"])
oid = (ords.get("orders") if isinstance(ords, dict) else ords)[0]["id"]
st, sig = call("POST", "/api/signatures", {"order_id": oid, "level": "qualified",
                                           "signer_name": "Иван Петров",
                                           "signer_email": "ivan@example.com"})
s = sig.get("signature") or sig
tok, sid, dh = s["token"], s["id"], s["doc_hash"]
check("created", st == 200 and s.get("level") == "qualified", str(sig)[:200])
check("state ready (CSC is interactive, nothing pushed yet)",
      True, "")

print("\n== the signer loads their qualified certificates ==")
st, r = call("POST", "/api/sign/%s/qtsp/credentials" % tok, {"email": "ivan@example.com"})
check("200", st == 200, str(r)[:200])
creds = r.get("credentials") or []
check("one credential", len(creds) == 1, str(creds)[:200])
check("subject from the certificate", "Ivan Petrov" in (creds[0]["subject"] if creds else ""),
      str(creds[0] if creds else ""))
check("issuer is the qualified CA", "Qualified CA" in (creds[0]["issuer"] if creds else ""), "")
cid = creds[0]["id"] if creds else ""

print("\n== authorising without the provider OTP fails ==")
st, r = call("POST", "/api/sign/%s/qtsp/sign" % tok, {"credential_id": cid, "pin": "1234"})
check("502 refused by provider", st == 502, "%s %s" % (st, str(r)[:160]))

print("\n== provider sends its own one-time code ==")
st, r = call("POST", "/api/sign/%s/qtsp/otp" % tok, {"credential_id": cid})
check("otp sent", st == 200 and r.get("sent") is True, str(r)[:160])

print("\n== wrong PIN is refused ==")
st, r = call("POST", "/api/sign/%s/qtsp/sign" % tok,
             {"credential_id": cid, "pin": "0000", "otp": "778899"})
check("502 wrong pin", st == 502 and "PIN" in json.dumps(r, ensure_ascii=False), "%s %s" % (st, str(r)[:200]))

print("\n== correct PIN + OTP signs the hash remotely ==")
st, r = call("POST", "/api/sign/%s/qtsp/sign" % tok,
             {"credential_id": cid, "pin": "1234", "otp": "778899"})
check("signed at provider", st == 200 and r.get("signed") is True, str(r)[:200])
check("subject returned", "Ivan Petrov" in (r.get("subject") or ""), str(r.get("subject")))

print("\n== finishing the signature in Seam ==")
st, r = call("POST", "/api/signatures/%d/sign" % sid,
             {"token": tok, "name": "Иван Петров", "consent": True})
check("200", st == 200, str(r)[:250])
sg = r.get("signature") or r
check("status signed", sg.get("status") == "signed", str(sg.get("status")))

print("\n== evidence certificate ==")
st, html = call("GET", "/signature/%d" % sid, raw=True)
check("page 200", st == 200)
check("names the provider", "Certum" in html, "")
check("verification row is localized, not raw English",
      "квалифицирано удостоверение, издадено от Certum" in html, "")
check("shows the document fingerprint", dh in html, "")
check("provider reference row", "Референция при доставчика" in html, "")
check("bulgarian labels", "Доставчик на удостоверителни услуги" in html, "")

print("\n== tamper evidence still holds ==")
st, sig2 = call("POST", "/api/signatures", {"order_id": oid, "level": "simple",
                                            "signer_name": "Тест"})
s2 = sig2.get("signature") or sig2
check("same document, same hash", s2["doc_hash"] == dh, "%s vs %s" % (s2["doc_hash"][:12], dh[:12]))

print("\n== a second signature cannot reuse the completed one ==")
st, r = call("GET", "/api/sign/%s/qtsp" % tok)
check("400 already signed", st == 400 and r.get("code") == "sig_already", "%s %s" % (st, r))

print("\n== disconnect ==")
call("POST", "/api/qtsp", {"provider": ""})

print("\n---- %d passed, %d failed ----" % (ok, fail))
sys.exit(1 if fail else 0)
