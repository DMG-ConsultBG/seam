# -*- coding: utf-8 -*-
"""The rule built through the UI (value > 5000 -> request an advanced signature)
must actually create a signature request when a term is agreed."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client()
c.login()
st, a = c.call("GET", "/api/automations")
rules = a["automations"]
c.check("the UI-built rule is stored", len(rules) >= 1, str(rules)[:200])
rule = rules[0]
c.check("trigger is term.agreed", rule["trigger"] == "term.agreed", rule["trigger"])
c.check("condition kept", rule["conditions"] and rule["conditions"][0]["op"] == "gt",
        str(rule["conditions"]))
c.check("action is a signature request",
        rule["actions"] and rule["actions"][0]["type"] == "request_signature",
        str(rule["actions"]))
c.check("level carried through", rule["actions"][0].get("level") == "advanced",
        str(rule["actions"][0]))

ws, order = c.first_order()
oid = order["id"]
st, sigs_before = c.call("GET", "/api/signatures?order_id=%d" % oid)
siglist = lambda r: (r.get("signatures") if isinstance(r, dict) else r) or []
n_before = len(siglist(sigs_before))

st, od = c.call("GET", "/api/orders/%d" % oid)
prop = [t for t in (od.get("terms") or []) if t.get("state") == "proposed" and t.get("awaiting_you")]
if not prop:
    print("  SKIP  nothing awaiting this side on %s" % order["ref"])
    sys.exit(c.summary())

st, r = c.call("POST", "/api/orders/%d/terms/%s/accept" % (oid, prop[0]["key"]), {})
c.check("term agreed", st == 200, str(r)[:150])

st, a2 = c.call("GET", "/api/automations")
fired = [x for x in a2["automations"] if x["id"] == rule["id"]][0]
st, od2 = c.call("GET", "/api/orders/%d" % oid)
value = 0.0
for t_ in od2.get("terms") or []:
    if t_.get("state") == "agreed" and t_.get("type") == "money":
        try:
            value += float(str(t_.get("value") or "").replace("EUR", "").replace(",", ".").strip() or 0)
        except ValueError:
            pass
print("     agreed money value on the order: %.2f (threshold 5000)" % value)

if value > 5000:
    c.check("rule fired", fired["runs"] >= 1, str(fired["recent"])[:200])
    c.check("run says request_signature",
            any("request_signature" in (x["detail"] or "") for x in fired["recent"]),
            str(fired["recent"])[:250])
    st, sigs_after = c.call("GET", "/api/signatures?order_id=%d" % oid)
    lst = siglist(sigs_after)
    c.check("a signature request was created", len(lst) > n_before,
            "%d -> %d" % (n_before, len(lst)))
    auto_sig = [s for s in lst if s.get("level") == "advanced"]
    c.check("created at the advanced level", len(auto_sig) >= 1, str(lst)[:250])
else:
    c.check("condition correctly blocked the rule", fired["runs"] == 0, str(fired["runs"]))

sys.exit(c.summary())
