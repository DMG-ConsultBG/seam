# -*- coding: utf-8 -*-
"""Notification channels: per-user preferences, a real SMS gateway round-trip,
and SMS actually going out when a business event fires."""
import os, sys, os, time, json, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

GW = "http://127.0.0.1:5098"


def gw(path):
    return json.loads(urllib.request.urlopen(GW + path, timeout=10).read().decode("utf-8"))


c = Client()
c.login()

print("== defaults ==")
st, s = c.call("GET", "/api/notify/settings")
c.check("200", st == 200, str(s)[:200])
c.check("in-app is not a toggle", "inapp" not in s["channels"], str(s["channels"]))
c.check("email on by default", s["channels"]["email"] is True, str(s["channels"]))
c.check("sms off by default", s["channels"]["sms"] is False, str(s["channels"]))
c.check("push on by default", s["channels"]["push"] is True, str(s["channels"]))
c.check("availability reported", set(s["available"]) == {"email", "sms", "push"}, str(s["available"]))

print("\n== phone validation ==")
st, r = c.call("PUT", "/api/notify/settings", {"phone": "not a phone!!"})
c.check("400 bad_phone", st == 400 and r.get("code") == "bad_phone", "%s %s" % (st, r))

print("\n== preferences persist ==")
st, r = c.call("PUT", "/api/notify/settings",
               {"channels": {"email": False, "sms": True, "push": False}, "phone": "+359888123456"})
c.check("saved", st == 200, str(r)[:150])
st, s = c.call("GET", "/api/notify/settings")
c.check("email off", s["channels"]["email"] is False, str(s["channels"]))
c.check("sms on", s["channels"]["sms"] is True, str(s["channels"]))
c.check("phone kept", s["phone"] == "+359888123456", s["phone"])

print("\n== gateway configuration ==")
st, r = c.call("POST", "/api/notify/sms-config", {"provider": "nope"})
c.check("unknown gateway refused", st == 400 and r.get("code") == "sms_provider", "%s %s" % (st, r))

st, r = c.call("POST", "/api/notify/sms-config",
               {"provider": "bearer", "url": GW + "/send", "api_key": "test-key", "from": "SEAM"})
c.check("gateway saved", st == 200 and (r.get("sms") or {}).get("ready") is True, str(r)[:250])
c.check("api key not echoed back", "api_key" not in (r.get("sms") or {}), str(r.get("sms")))

st, s = c.call("GET", "/api/notify/settings")
c.check("sms now available", s["available"]["sms"] is True, str(s["available"]))

print("\n== a test message really reaches the gateway ==")
gw("/__reset")
st, r = c.call("POST", "/api/notify/sms-test", {"to": "+359888123456"})
c.check("sent", st == 200 and r.get("sent") is True, str(r)[:200])
got = gw("/__received")["received"]
c.check("gateway received exactly one message", len(got) == 1, str(got)[:250])
if got:
    c.check("recipient correct", got[0]["data"].get("to") == "+359888123456", str(got[0]))
    c.check("text present", "Seam" in (got[0]["data"].get("text") or ""), str(got[0]))
    c.check("sender carried", got[0]["data"].get("from") == "SEAM", str(got[0]))

print("\n== a wrong key is reported, not swallowed ==")
c.call("POST", "/api/notify/sms-config",
       {"provider": "bearer", "url": GW + "/send", "api_key": "wrong-key"})
st, r = c.call("POST", "/api/notify/sms-test", {"to": "+359888123456"})
c.check("502 sms_failed", st == 502 and r.get("code") == "sms_failed", "%s %s" % (st, str(r)[:150]))
c.call("POST", "/api/notify/sms-config",
       {"provider": "bearer", "url": GW + "/send", "api_key": "test-key", "from": "SEAM"})

print("\n== a real business event sends an SMS ==")
gw("/__reset")
partner = Client()
if partner.login("build@seam.demo", "demo1234")[0] == 200:
    ws, order = c.first_order()
    st, pod = partner.call("GET", "/api/orders/%d" % order["id"])
    money = [x for x in (pod.get("terms") or []) if x.get("type") == "money"]
    if money:
        partner.call("POST", "/api/orders/%d/terms" % order["id"],
                     {"key": money[0]["key"], "value": "EUR 4321"})
        time.sleep(1.2)                      # sending is off the request path
        got = gw("/__received")["received"]
        c.check("an SMS went out for the event", len(got) >= 1, str(got)[:250])
        if got:
            c.check("addressed to the stored number",
                    got[0]["data"].get("to") == "+359888123456", str(got[0]))

print("\n== turning SMS off stops them ==")
c.call("PUT", "/api/notify/settings", {"channels": {"email": True, "sms": False, "push": True}})
gw("/__reset")
if partner.csrf:
    ws, order = c.first_order()
    st, pod = partner.call("GET", "/api/orders/%d" % order["id"])
    money = [x for x in (pod.get("terms") or []) if x.get("type") == "money"]
    if money:
        partner.call("POST", "/api/orders/%d/terms" % order["id"],
                     {"key": money[0]["key"], "value": "EUR 4322"})
        time.sleep(1.2)
        got = gw("/__received")["received"]
        c.check("no SMS once the channel is off", len(got) == 0, str(got)[:200])

print("\n== the service worker is served from the root ==")
st, sw = c.call("GET", "/sw.js", raw=True)
c.check("/sw.js served (root scope)", st == 200 and "showNotification" in sw, str(st))
c.check("sw is honest about background push", "VAPID" in sw, "")

print("\n== disconnect the gateway ==")
st, r = c.call("POST", "/api/notify/sms-config", {"provider": ""})
c.check("cleared", st == 200 and r.get("sms") is None, str(r)[:150])
c.call("PUT", "/api/notify/settings",
       {"channels": {"email": True, "sms": False, "push": True}, "phone": ""})

sys.exit(c.summary())
