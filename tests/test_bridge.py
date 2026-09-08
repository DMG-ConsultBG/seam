# -*- coding: utf-8 -*-
"""The card/token path (КЕП, e-imza, ЭЦП) and the per-country legal rules."""
import os, json, sys, urllib.request, urllib.error, http.cookiejar, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the app package
import qtsp

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
CSRF = {"v": ""}
ok = fail = 0


def call(method, path, data=None, raw=False, lang="bg"):
    c = http.cookiejar.Cookie(0, "seam_lang", lang, None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    cj.set_cookie(c)
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


print("== per-country coverage in the registry (no app needed) ==")
COUNTRIES = ["BG", "RO", "GR", "TR", "IT", "DE", "RU", "RS", "MK", "UA", "GB", "ES", "FR", "PL", "PT"]
for c in COUNTRIES:
    own = [p for p in qtsp.providers_for(c) if p["country"] == c]
    legal = qtsp.legal_note(c)
    check("%s: %d provider(s), law present, max=%s"
          % (c, len(own), qtsp.max_level(c)),
          len(own) >= 1 and bool(legal and legal.get("law")) and bool(legal.get("supervisor")),
          str(legal)[:120])
check("GB tops out at advanced (no EU-style qualified tier)",
      qtsp.max_level("GB") == "advanced", qtsp.max_level("GB"))
check("every other country reaches qualified",
      all(qtsp.max_level(c) == "qualified" for c in COUNTRIES if c != "GB"))
check("EU company also sees cross-border CSC providers",
      len(qtsp.providers_for("BG")) > len([p for p in qtsp.providers_for("BG") if p["country"] == "BG"]))
check("non-EU company sees only its own country's providers",
      all(p["country"] == "RU" for p in qtsp.providers_for("RU")))
check("registry carries a signup link for every provider",
      all(p.get("signup") for p in qtsp.PROVIDERS.values()))
check("every provider has a known protocol",
      all(p["api"] in ("csc", "native", "bridge") for p in qtsp.PROVIDERS.values()))
print("     %d providers across %d countries"
      % (len(qtsp.PROVIDERS), len(set(p["country"] for p in qtsp.PROVIDERS.values()))))

print("\n== card/token signing inside the platform ==")
st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
CSRF["v"] = r.get("csrf", "")
st, r = call("POST", "/api/qtsp", {"provider": "stampit"})
check("bridge provider connects with no credentials",
      st == 200 and (r.get("config") or {}).get("ready") is True, str(r)[:200])

st, wss = call("GET", "/api/workspaces")
ws = (wss.get("workspaces") if isinstance(wss, dict) else wss)[0]
st, ords = call("GET", "/api/workspaces/%d/orders" % ws["id"])
oid = (ords.get("orders") if isinstance(ords, dict) else ords)[1]["id"]
st, sig = call("POST", "/api/signatures", {"order_id": oid, "level": "qualified",
                                           "signer_name": "Мария Георгиева"})
s = sig.get("signature") or sig
tok, sid, dh = s["token"], s["id"], s["doc_hash"]
check("qualified request created", st == 200, str(sig)[:200])

st, html = call("GET", "/sign/%s" % tok, raw=True)
check("page offers the fingerprint to sign locally", dh in html and 'id="q_hash"' in html, "")
check("upload field present", 'id="q_file"' in html, "")
check("StampIT named", "StampIT" in html, "")

print("\n== a signature over a different document is refused ==")
st, r = call("POST", "/api/sign/%s/qtsp/detached" % tok,
             {"signature": "B" * 300, "signed_hash": "0" * 64})
check("400 sig_hash_mismatch", st == 400 and r.get("code") == "sig_hash_mismatch", "%s %s" % (st, r))

print("\n== the real detached signature is accepted ==")
blob = base64.b64encode(b"PKCS7-DETACHED-SIGNATURE-FROM-SMART-CARD").decode()
st, r = call("POST", "/api/sign/%s/qtsp/detached" % tok,
             {"signature": "data:application/pkcs7-signature;base64," + blob, "signed_hash": dh})
check("accepted", st == 200 and r.get("signed") is True, str(r)[:200])

st, r = call("POST", "/api/signatures/%d/sign" % sid,
             {"token": tok, "name": "Мария Георгиева", "consent": True})
sg = r.get("signature") or r
check("signature completes", st == 200 and sg.get("status") == "signed", str(r)[:250])

st, html = call("GET", "/signature/%d" % sid, raw=True)
check("certificate names StampIT", "StampIT" in html, "")
check("certificate shows the fingerprint", dh in html, "")

print("\n== consent is still mandatory ==")
st, sig = call("POST", "/api/signatures", {"order_id": oid, "level": "qualified",
                                           "signer_name": "Тест"})
s3 = sig.get("signature") or sig
call("POST", "/api/sign/%s/qtsp/detached" % s3["token"],
     {"signature": blob, "signed_hash": s3["doc_hash"]})
st, r = call("POST", "/api/signatures/%d/sign" % s3["id"],
             {"token": s3["token"], "name": "Тест", "consent": False})
check("400 sig_consent", st == 400 and r.get("code") == "sig_consent", "%s %s" % (st, r))

call("POST", "/api/qtsp", {"provider": ""})
print("\n---- %d passed, %d failed ----" % (ok, fail))
sys.exit(1 if fail else 0)
