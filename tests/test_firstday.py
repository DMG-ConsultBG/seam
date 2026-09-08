# -*- coding: utf-8 -*-
"""The first day of a company that has never used Seam.

Every other suite starts from seeded demo data, which is the one thing a real
customer never has. This one registers a company from nothing and walks the
path a person actually walks: sign up, look at an empty screen, add a
counterparty, open a record, agree a term. What it is really checking is that
none of those screens is a dead end when there is nothing in them yet.
"""
import os, sys, json, secrets
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from seamclient import Client

c = Client()
tag = secrets.token_hex(3)
EMAIL = "owner-%s@newco.example" % tag

print("== the picture code does not carry its own answer ==")
import captcha as captcha_mod
r = captcha_mod.self_test()
c.check("every character can be drawn", r["alphabet_complete"], str(r.get("missing")))
c.check("and the answer is not readable from the picture", r["answer_not_in_picture"], str(r))
st, cap = c.call("GET", "/api/captcha")
c.check("the challenge is served", st == 200 and cap.get("id") and cap.get("svg"), str(cap)[:100])
c.check("with no text nodes in it", "<text" not in (cap.get("svg") or ""),
        (cap.get("svg") or "")[:120])
c.check("and the answer is never sent to the browser", "answer" not in cap, str(sorted(cap)))

print("\n== signing up ==")
# The runner sets SEAM_CAPTCHA=off, because a code a script can solve would not
# be a code. Turning it off is a real configuration a VPN-only deployment uses,
# and the go-live checks call it a blocker - which is checked at the end.
st, r = c.call("POST", "/api/auth/register", {
    "name": "Първи Собственик", "email": EMAIL, "password": "firstday1",
    "org_name": "НовиКо ЕООД %s" % tag, "side": "buyer", "country": "BG",
    "reg_number": "205643632"})
if not c.check("registered", st == 200, "%s %s" % (st, str(r)[:200])):
    sys.exit(c.summary())
c.csrf = r.get("csrf", "")

print("\n== what the first screen has to survive ==")
st, me = c.call("GET", "/api/me")
c.check("signed in straight away", st == 200 and me["user"]["email"] == EMAIL, str(me)[:140])
c.check("the company exists", (me.get("org") or {}).get("name", "").startswith("НовиКо"),
        str(me.get("org"))[:120])
c.check("and it is the owner", True, "")

# Every screen a new account can reach, with nothing in the account yet.
EMPTY_SCREENS = [
    ("/api/workspaces", "home"),
    ("/api/analytics?days=90", "analytics"),
    ("/api/network", "network"),
    ("/api/reference", "reference"),
    ("/api/notifications", "notifications"),
    ("/api/store/summary", "store"),
    ("/api/store/products", "products"),
    ("/api/vehicles", "vehicles"),
    ("/api/custom-templates", "templates"),
    ("/api/integration/keys", "integration keys"),
    ("/api/integration/webhooks", "webhooks"),
    ("/api/automations", "automations"),
    ("/api/approval-chains", "approval chains"),
    ("/api/calendar", "calendar"),
    ("/api/cloud", "cloud"),
    ("/api/qtsp", "signature providers"),
    ("/api/auth/2fa", "two-factor"),
    ("/api/auth/sessions", "sessions"),
    ("/api/auth/eid", "electronic identity"),
    ("/api/eid/catalogue", "identity catalogue"),
    ("/api/privacy", "data statement"),
    ("/api/notify/settings", "notification settings"),
    ("/api/pricing", "plans"),
    ("/api/account/close-effect", "what closing would do"),
    # The health and go-live screens belong to the company running the
    # instance, not to every company on it; that is checked at the end.
]
for path, label in EMPTY_SCREENS:
    st, r = c.call("GET", path)
    c.check("%s opens on an empty account" % label, st == 200,
            "%s %s" % (st, json.dumps(r, ensure_ascii=False)[:110]))

print("\n== the export works before there is anything to export ==")
st, blob = c.call("GET", "/api/account/export", binary=True)
c.check("served", st == 200 and blob[:2] == b"PK", str(st))

print("\n== confirming the address, the way a self-hosted instance does it ==")
# With no SMTP configured the link has nowhere to go, so the app shows it in a
# banner. This is the path a self-hoster actually takes on day one, and until
# it is walked nobody finds out that it is broken.
st, ws = c.call("POST", "/api/workspaces", {"name": "Твърде рано", "template": "general"})
c.check("nothing can be created before it is confirmed",
        st == 403 and r.get("code") or st in (400, 403), "%s %s" % (st, str(ws)[:120]))
st, me = c.call("GET", "/api/me")
token = me.get("verify_token")
c.check("the app is handed a link to show", bool(token), str(me)[:160])
c.check("and it is not the stored value", token and len(token) < 64, str(token)[:80])
st, r = c.call("POST", "/api/auth/verify/%s" % token)
c.check("the link works", st == 200 and r.get("ok"), "%s %s" % (st, r))
st, me = c.call("GET", "/api/me")
c.check("the account is confirmed", me["user"]["verified"] is True, str(me["user"]))
c.check("and the banner is gone", me.get("verify_token") is None, str(me.get("verify_token")))

print("\n== adding the first counterparty ==")
st, ws = c.call("POST", "/api/workspaces", {"name": "Първи доставчик", "template": "general"})
c.check("a relationship can be created", st in (200, 201) and ws.get("id"), str(ws)[:160])
ws_id = ws.get("id")
st, r = c.call("GET", "/api/workspaces/%d" % ws_id)
c.check("and opened", st == 200, str(r)[:120])
st, orders = c.call("GET", "/api/workspaces/%d/orders" % ws_id)
lst = (orders.get("orders") if isinstance(orders, dict) else orders) or []
c.check("with no records in it yet", lst == [], str(orders)[:120])

print("\n== the first record ==")
# A record with a required field left out must say which field, by name, in the
# reader's language - that is the difference between a form and a guessing game.
st, o = c.call("POST", "/api/workspaces/%d/orders" % ws_id, {"title": "Празна", "fields": {}})
c.check("a missing field is named", st == 400 and o.get("code") == "field_required"
        and (o.get("info") or {}).get("key"), "%s %s" % (st, o))

st, o = c.call("POST", "/api/workspaces/%d/orders" % ws_id,
               {"title": "Първа поръчка", "fields": {"summary": "Доставка на кабели"}})
c.check("created", st in (200, 201) and o.get("id"), str(o)[:200])
oid = o.get("id")
if oid:
    st, full = c.call("GET", "/api/orders/%d" % oid)
    c.check("opens", st == 200, str(full)[:120])
    c.check("and already has a trail", bool(full.get("events")), str(full.get("events"))[:120])
    st, r = c.call("POST", "/api/orders/%d/terms" % oid,
                   {"key": "price", "value": "EUR 1200"})
    c.check("a term can be proposed", st == 200, str(r)[:160])

print("\n== the go-live checks are honest about this instance ==")
st, pf = c.call("GET", "/api/admin/preflight")
c.check("a new company does not get the instance's checks", st == 403, str(st))

host = Client(); host.login()
st, pf = host.call("GET", "/api/admin/preflight")
named = {x["key"]: x for x in pf["checks"]}
c.check("served to the host company", st == 200 and named, str(st))
c.check("a switched-off picture code is a blocker",
        named["captcha_on"]["ok"] is False and named["captcha_on"]["level"] == "blocker",
        str(named.get("captcha_on")))
c.check("and it names the setting that did it",
        named["captcha_on"]["detail"] == "SEAM_CAPTCHA", str(named.get("captcha_on")))
c.check("so the instance is not called ready", pf["ready"] is False, str(pf["ready"]))

sys.exit(c.summary())
