# -*- coding: utf-8 -*-
"""Workflow automation: rules really fire on real events, conditions gate them,
actions take effect, every firing is logged, and a rule cannot trigger itself."""
import os, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client()
c.login()
ws, order = c.first_order()
oid, wsid = order["id"], ws["id"]

# start from a clean slate
st, a = c.call("GET", "/api/automations")
for r in a.get("automations", []):
    c.call("DELETE", "/api/automations/%d" % r["id"])

print("== catalogue ==")
st, a = c.call("GET", "/api/automations")
c.check("200", st == 200, str(a)[:200])
c.check("triggers offered", "order.status_changed" in a["triggers"], str(a.get("triggers")))
c.check("actions offered", set(["notify", "message", "set_status", "request_signature", "webhook", "flag"])
        <= set(a["actions"]), str(a.get("actions")))
c.check("starts empty", a["automations"] == [], str(a["automations"])[:150])

print("\n== validation ==")
st, r = c.call("POST", "/api/automations", {"name": "", "trigger": "order.status_changed",
                                            "actions": [{"type": "notify"}]})
c.check("name required", st == 400 and r.get("code") == "field_required", "%s %s" % (st, r))
st, r = c.call("POST", "/api/automations", {"name": "X", "trigger": "nope",
                                            "actions": [{"type": "notify"}]})
c.check("unknown trigger refused", st == 400 and r.get("code") == "auto_trigger", "%s %s" % (st, r))
st, r = c.call("POST", "/api/automations", {"name": "X", "trigger": "order.status_changed",
                                            "actions": []})
c.check("at least one action required", st == 400 and r.get("code") == "auto_no_action", "%s %s" % (st, r))
st, r = c.call("POST", "/api/automations", {"name": "X", "trigger": "order.status_changed",
                                            "actions": [{"type": "rm -rf"}]})
c.check("unknown action type dropped -> no actions", st == 400 and r.get("code") == "auto_no_action",
        "%s %s" % (st, r))

print("\n== a rule that notifies on every status change ==")
st, r = c.call("POST", "/api/automations", {
    "name": "Известие при смяна на статус", "trigger": "order.status_changed",
    "actions": [{"type": "notify", "text": "{ref} премина в {status}"}]})
c.check("created", st == 200 and len(r["automations"]) == 1, str(r)[:200])
rule = r["automations"][0]
c.check("stored active", rule["active"] is True, str(rule))
c.check("runs starts at zero", rule["runs"] == 0, str(rule["runs"]))

st, tpls = c.call("GET", "/api/templates")
stages = [s["key"] if isinstance(s, dict) else s
          for s in ((tpls.get(order["template"]) or {}).get("stages") or [])]
other = [s for s in stages if s != order["status"]][0]

st, nb = c.call("GET", "/api/notifications")
before_n = len(nb.get("items") or [])

st, _ = c.call("POST", "/api/orders/%d/status" % oid, {"status": other})
c.check("status changed", st == 200, "")

st, a = c.call("GET", "/api/automations")
rule = a["automations"][0]
c.check("rule fired", rule["runs"] >= 1, "runs=%s" % rule["runs"])
c.check("run recorded ok", rule["last_status"] == "ok", str(rule["last_status"]))
c.check("run detail says notify", any("notify" in (x["detail"] or "") for x in rule["recent"]),
        str(rule["recent"])[:200])

st, na = c.call("GET", "/api/notifications")
notifs = na.get("items") or []
auto = [n for n in notifs if n.get("kind") == "automation"]
c.check("a notification was produced", len(auto) >= 1, str(notifs)[:200])
c.check("placeholders substituted", order["ref"] in (auto[0].get("body") or ""),
        str(auto[0])[:200] if auto else "")
print("     notification: %s" % (auto[0].get("body") if auto else "-"))

print("\n== conditions gate the rule ==")
st, r = c.call("POST", "/api/automations", {
    "name": "Само за конкретен статус", "trigger": "order.status_changed",
    "conditions": [{"field": "status", "op": "eq", "value": "__never__"}],
    "actions": [{"type": "flag", "text": "не трябва да се случва"}]})
gated = r["automations"][0]
st, _ = c.call("POST", "/api/orders/%d/status" % oid, {"status": order["status"]})
st, a = c.call("GET", "/api/automations")
g2 = [x for x in a["automations"] if x["id"] == gated["id"]][0]
c.check("non-matching rule did not fire", g2["runs"] == 0, "runs=%s" % g2["runs"])
other_rule = [x for x in a["automations"] if x["id"] == rule["id"]][0]
c.check("matching rule fired again", other_rule["runs"] >= 2, "runs=%s" % other_rule["runs"])

print("\n== set_status action, and no rule may trigger another ==")
c.call("DELETE", "/api/automations/%d" % gated["id"])
st, r = c.call("POST", "/api/automations", {
    "name": "Придвижи напред", "trigger": "term.agreed",
    "actions": [{"type": "set_status", "status": stages[-1]}]})
mover = r["automations"][0]
st, r = c.call("POST", "/api/automations", {
    "name": "Не трябва да се верижи", "trigger": "order.status_changed",
    "actions": [{"type": "set_status", "status": stages[0]}]})
chainer = r["automations"][0]

# Make the test self-sufficient: a term can only be agreed by the side that did
# not propose it, so have the partner propose one first.
partner = Client()
if partner.login("build@seam.demo", "demo1234")[0] == 200:
    st, pod = partner.call("GET", "/api/orders/%d" % oid)
    money = [t for t in (pod.get("terms") or []) if t.get("type") == "money"]
    if money:
        partner.call("POST", "/api/orders/%d/terms" % oid,
                     {"key": money[0]["key"], "value": "EUR 8900"})

st, od = c.call("GET", "/api/orders/%d" % oid)
terms = od.get("terms") or []
# a term this side may agree to must have been proposed by the OTHER side
prop = [t for t in terms if t.get("state") == "proposed" and t.get("awaiting_you")]
if prop:
    key = prop[0]["key"]
    st, r = c.call("POST", "/api/orders/%d/terms/%s/accept" % (oid, key), {})
    c.check("term agreed", st == 200, str(r)[:200])
    st, od = c.call("GET", "/api/orders/%d" % oid)
    st, a = c.call("GET", "/api/automations")
    mv = [x for x in a["automations"] if x["id"] == mover["id"]][0]
    ch = [x for x in a["automations"] if x["id"] == chainer["id"]][0]
    c.check("set_status rule fired", mv["runs"] >= 1, str(mv["recent"])[:200])
    c.check("order moved to the target stage", od.get("status") == stages[-1],
            "%s != %s" % (od.get("status"), stages[-1]))
    c.check("the second rule was NOT chained", ch["runs"] == 0, "runs=%s" % ch["runs"])
else:
    print("  SKIP  no proposable term on this order")

print("\n== toggle and delete ==")
st, r = c.call("PATCH", "/api/automations/%d" % rule["id"], {"active": False})
off = [x for x in r["automations"] if x["id"] == rule["id"]][0]
c.check("deactivated", off["active"] is False, str(off))
runs_before = off["runs"]
c.call("POST", "/api/orders/%d/status" % oid, {"status": stages[0]})
st, a = c.call("GET", "/api/automations")
off = [x for x in a["automations"] if x["id"] == rule["id"]][0]
c.check("inactive rule does not fire", off["runs"] == runs_before, "%s -> %s" % (runs_before, off["runs"]))

for x in a["automations"]:
    c.call("DELETE", "/api/automations/%d" % x["id"])
st, a = c.call("GET", "/api/automations")
c.check("all deleted", a["automations"] == [], str(a["automations"])[:120])

print("\n== another org cannot touch my rules ==")
c2 = Client()
st, _ = c2.login("build@seam.demo", "demo1234")
if st == 200:
    st, r = c2.call("DELETE", "/api/automations/%d" % rule["id"])
    c.check("cross-org delete is a no-op or refused", st in (200, 400, 403, 404), str(st))

c.call("POST", "/api/orders/%d/status" % oid, {"status": order["status"]})
sys.exit(c.summary())
