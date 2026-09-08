# -*- coding: utf-8 -*-
"""Signing in with a national electronic identity.

The relying party is driven against a real OpenID Connect provider (the mock in
fixtures/mock_oidc.py, which signs proper RS256 tokens with a key it generates),
because the whole value of this feature is that the token is verified rather
than believed. So the checks that matter are the refusals: a wrong nonce, a
wrong audience, an expired token, a token whose signature has been touched, a
replayed callback, and an identity nobody here has attached.
"""
import os, sys, json, time
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from seamclient import Client
import eid

OIDC = "http://127.0.0.1:5096"
c = Client(); c.login()

print("== the maths before anything is built on it ==")
r = eid.self_test()
c.check("RS256 verification accepts a valid signature", r["accepts_valid"], str(r))
c.check("and refuses a tampered one", r["rejects_tampered"], str(r))

print("\n== the registry is honest about each country ==")
st, cat = c.call("GET", "/api/eid/catalogue?country=BG")
c.check("Bulgaria has schemes listed", st == 200 and cat["providers"], str(cat)[:160])
c.check("each names the authority behind it",
        all(p.get("authority") for p in cat["providers"]), str(cat["providers"])[:200])
c.check("each links where to register",
        all(str(p.get("portal", "")).startswith("http") for p in cat["providers"]),
        str(cat["providers"])[:200])
c.check("the registry carries a date", bool(cat.get("verified")), str(cat.get("verified")))

st, it = c.call("GET", "/api/eid/catalogue?country=IT")
spid = [p for p in it["providers"] if p["name"] == "SPID"]
c.check("a SAML scheme is listed but not claimed as usable",
        spid and spid[0]["usable"] is False, str(spid))
st, zz = c.call("GET", "/api/eid/catalogue?country=ZZ")
c.check("a country with nothing says nothing", zz["providers"] == [], str(zz))

print("\n== nothing is offered until it is configured ==")
st, p = c.call("GET", "/api/auth/eid/providers")
c.check("the sign-in screen is offered no providers yet", st == 200 and p["providers"] == [],
        str(p))
st, r = c.call("POST", "/api/auth/eid/bg_eavt/start", {})
c.check("and starting one is refused", st == 400 and r.get("code") == "eid_unset",
        "%s %s" % (st, r))
st, r = c.call("POST", "/api/eid/config", {"key": "it_spid", "discovery": OIDC,
                                           "client_id": "x", "client_secret": "y"})
c.check("a SAML scheme cannot be configured", st == 400 and r.get("code") == "eid_protocol",
        "%s %s" % (st, r))
st, r = c.call("POST", "/api/eid/config", {"key": "nope", "discovery": OIDC})
c.check("an unknown provider is refused", st == 400 and r.get("code") == "eid_unknown",
        "%s %s" % (st, r))

print("\n== configuring one ==")
st, r = c.call("POST", "/api/eid/config", {
    "key": "bg_eavt", "discovery": OIDC, "client_id": "seam-test-client",
    "client_secret": "seam-test-secret", "scope": "openid"})
c.check("saved", st == 200 and r.get("ready"), str(r))
c.check("the secret is never handed back", "client_secret" not in json.dumps(r), str(r))
st, p = c.call("GET", "/api/auth/eid/providers")
c.check("now the sign-in screen may offer it",
        [x for x in p["providers"] if x["key"] == "bg_eavt"], str(p))

partner = Client(); partner.login("build@seam.demo", "demo1234")
st, r = partner.call("POST", "/api/eid/config", {"key": "bg_eavt", "discovery": OIDC,
                                                 "client_id": "x"})
c.check("only the host company may configure it", st == 403, "%s %s" % (st, str(r)[:120]))


import urllib.request
import urllib.parse as up


def run_flow(client, quirk="", sub="citizen-1"):
    """Drive the whole redirect dance the way a browser would."""
    st, r = client.call("POST", "/api/auth/eid/bg_eavt/start", {})
    if st != 200:
        return st, json.dumps(r), {}
    q = up.parse_qs(up.urlparse(r["url"]).query)
    # This provider hands the code straight back instead of redirecting, so the
    # test does not need a browser to follow it.
    auth = r["url"] + "&sub=" + up.quote(sub) + ("&quirk=" + quirk if quirk else "")
    got = json.loads(urllib.request.urlopen(auth, timeout=5).read().decode())
    st, html = client.call("GET", "/auth/eid/callback?code=%s&state=%s"
                           % (got["code"], up.quote(got["state"])), raw=True)
    return st, html, q


print("\n== an identity nobody has attached does not create an account ==")
out = Client()
st, html, _q = run_flow(out)
c.check("refused", st == 400, str(st))
c.check("and says why, in the reader's language",
        "Няма профил" in html, html[-400:][:200])
st, me = out.call("GET", "/api/me")
c.check("nobody was signed in", st == 401, str(st))

print("\n== attaching one to an account that already exists ==")
st, html, _q = run_flow(c)
c.check("attached", st == 200 and "свързана" in html, html[-400:][:200])
st, links = c.call("GET", "/api/auth/eid")
c.check("it is listed", len(links["links"]) == 1 and links["links"][0]["provider"] == "bg_eavt",
        str(links))
c.check("with a name a person recognises", links["links"][0]["label"] == "Test Citizen",
        str(links["links"][0]))

print("\n== and then it signs that account in ==")
fresh = Client()
st, html, _q = run_flow(fresh)
c.check("signed in", st == 200 and "Maria" in html, html[-400:][:200])
st, me = fresh.call("GET", "/api/me")
c.check("the session is real", st == 200 and me["user"]["email"] == "ops@seam.demo",
        str(me)[:140])
st, links = c.call("GET", "/api/auth/eid")
c.check("and the identity records when it was used", links["links"][0]["last_used"],
        str(links["links"][0]))

print("\n== somebody else's identity is not accepted for this account ==")
other = Client()
st, html, _q = run_flow(other, sub="citizen-2")
c.check("an unattached subject is still refused", st == 400 and "Няма профил" in html,
        html[-300:][:160])

print("\n== a token that does not verify is refused ==")
for quirk, label in (("nonce", "a replayed nonce"), ("aud", "another application's token"),
                     ("exp", "an expired token"), ("iss", "a different issuer"),
                     ("sig", "a touched signature")):
    st, html, _q = run_flow(Client(), quirk=quirk)
    c.check("%s is refused" % label, st == 400, "%s %s" % (st, html[-160:]))

print("\n== a callback cannot be replayed ==")
rep = Client()
st, r = rep.call("POST", "/api/auth/eid/bg_eavt/start", {})
got = json.loads(urllib.request.urlopen(r["url"] + "&sub=citizen-1", timeout=5).read().decode())
st1, _h = rep.call("GET", "/auth/eid/callback?code=%s&state=%s"
                   % (got["code"], up.quote(got["state"])), raw=True)
st2, h2 = rep.call("GET", "/auth/eid/callback?code=%s&state=%s"
                   % (got["code"], up.quote(got["state"])), raw=True)
c.check("the first callback works", st1 == 200, str(st1))
c.check("the second is refused", st2 == 400, str(st2))

print("\n== a forged state is refused ==")
forge = Client()
st, r = forge.call("POST", "/api/auth/eid/bg_eavt/start", {})
got = json.loads(urllib.request.urlopen(r["url"] + "&sub=citizen-1", timeout=5).read().decode())
st, h = forge.call("GET", "/auth/eid/callback?code=%s&state=%s" % (got["code"], "not-the-state"),
                   raw=True)
c.check("refused", st == 400, str(st))

print("\n== detaching ==")
st, links = c.call("GET", "/api/auth/eid")
st, r = c.call("DELETE", "/api/auth/eid/%d" % links["links"][0]["id"])
c.check("removed", st == 200, str(r))
gone = Client()
st, html, _q = run_flow(gone)
c.check("and the identity no longer signs anyone in", st == 400, str(st))

# Leave the instance as it was found.
c.call("POST", "/api/eid/config", {"key": "bg_eavt", "remove": True})
st, p = c.call("GET", "/api/auth/eid/providers")
c.check("configuration can be removed again", p["providers"] == [], str(p))

sys.exit(c.summary())
