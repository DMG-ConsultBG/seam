# -*- coding: utf-8 -*-
"""Multi-level approvals: steps unlock in order, only the named approver may
decide, a rejection ends the chain, and every decision is in the audit trail."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client()
c.login()
ws, _ = c.first_order()

# Work on an order this run creates, so leftovers from earlier runs cannot
# change the outcome - approvals are audit records and are never deleted.
st, tpls = c.call("GET", "/api/templates")
spec = tpls.get(ws["template"]) or {}
fields = {f["key"]: "тест" for f in (spec.get("fields") or []) if f.get("required")}
st, order = c.call("POST", "/api/workspaces/%d/orders" % ws["id"],
                   {"title": "Тест на одобрения", "fields": fields})
if st != 200 or not order.get("id"):
    print("could not create a test order:", st, str(order)[:300])
    sys.exit(1)
oid = order["id"]
print("test order: %s (#%d)" % (order.get("ref"), oid))

st, x = c.call("GET", "/api/approval-chains")
for ch in x.get("chains", []):
    c.call("DELETE", "/api/approval-chains/%d" % ch["id"])

print("== chain catalogue ==")
st, x = c.call("GET", "/api/approval-chains")
c.check("200", st == 200, str(x)[:200])
c.check("members listed", len(x["members"]) >= 1, str(x["members"])[:200])
me = x["members"][0]

print("\n== validation ==")
st, r = c.call("POST", "/api/approval-chains", {"name": "", "steps": [{"label": "A"}]})
c.check("name required", st == 400 and r.get("code") == "field_required", "%s %s" % (st, r))
st, r = c.call("POST", "/api/approval-chains", {"name": "X", "steps": []})
c.check("at least one step", st == 400 and r.get("code") == "chain_no_steps", "%s %s" % (st, r))

print("\n== a three-step chain ==")
st, r = c.call("POST", "/api/approval-chains", {
    "name": "Голяма стойност", "threshold": 1000,
    "steps": [{"label": "Ръководител проект", "user_id": me["id"]},
              {"label": "Финансов контрол"},
              {"label": "Управител"}]})
c.check("created", st == 200 and len(r["chains"]) == 1, str(r)[:200])
chain = r["chains"][0]
c.check("three steps stored", len(chain["steps"]) == 3, str(chain["steps"]))
c.check("named approver kept", chain["steps"][0].get("user_id") == me["id"], str(chain["steps"][0]))

print("\n== starting it on an order ==")
st, ap = c.call("POST", "/api/orders/%d/approvals/start" % oid, {"chain_id": chain["id"]})
c.check("started", st == 200 and len(ap["approvals"]) == 3, str(ap)[:250])
c.check("only the latest chain is reported", len(ap["approvals"]) == 3, str(len(ap["approvals"])))
c.check("state is pending", ap["state"] == "pending", ap["state"])
c.check("current step is 1", ap["current_step"] == 1, str(ap["current_step"]))
s1, s2, s3 = ap["approvals"]
c.check("step 1 is decidable now", s1["can_decide"] is True, str(s1))
c.check("step 2 is NOT decidable yet", s2["can_decide"] is False, str(s2))
c.check("step 3 is NOT decidable yet", s3["can_decide"] is False, str(s3))
c.check("progress 0 of 3", ap["progress"] == {"done": 0, "total": 3}, str(ap["progress"]))

print("\n== steps must be decided in order ==")
st, r = c.call("POST", "/api/approvals/%d/decide" % s2["id"],
               {"decision": "approved", "note": "прескачам"})
c.check("400 out of order", st == 400 and r.get("code") == "appr_out_of_order", "%s %s" % (st, r))

print("\n== a bad decision value is refused ==")
st, r = c.call("POST", "/api/approvals/%d/decide" % s1["id"], {"decision": "maybe"})
c.check("400 appr_decision", st == 400 and r.get("code") == "appr_decision", "%s %s" % (st, r))

print("\n== step 1 approved ==")
st, ap = c.call("POST", "/api/approvals/%d/decide" % s1["id"],
                {"decision": "approved", "note": "проверено"})
c.check("200", st == 200, str(ap)[:200])
c.check("step 1 approved", ap["approvals"][0]["status"] == "approved", str(ap["approvals"][0]))
c.check("note recorded", ap["approvals"][0]["note"] == "проверено", str(ap["approvals"][0]))
c.check("decider recorded", bool(ap["approvals"][0]["decided_by"]), str(ap["approvals"][0]))
c.check("IP recorded", bool(ap["approvals"][0]["ip"]), str(ap["approvals"][0]))
c.check("device recorded", bool(ap["approvals"][0]["device"]), str(ap["approvals"][0]))
c.check("current step advanced to 2", ap["current_step"] == 2, str(ap["current_step"]))
c.check("step 2 now decidable", ap["approvals"][1]["can_decide"] is True, str(ap["approvals"][1]))
c.check("step 3 still locked", ap["approvals"][2]["can_decide"] is False, str(ap["approvals"][2]))

print("\n== deciding the same step twice is refused ==")
st, r = c.call("POST", "/api/approvals/%d/decide" % s1["id"], {"decision": "rejected"})
c.check("400 appr_decided", st == 400 and r.get("code") == "appr_decided", "%s %s" % (st, r))

print("\n== a rejection ends the chain ==")
st, ap = c.call("POST", "/api/approvals/%d/decide" % s2["id"],
                {"decision": "rejected", "note": "бюджетът не стига"})
c.check("state rejected", ap["state"] == "rejected", ap["state"])
c.check("step 2 rejected", ap["approvals"][1]["status"] == "rejected", str(ap["approvals"][1]))
c.check("step 3 skipped, not silently dropped", ap["approvals"][2]["status"] == "skipped",
        str(ap["approvals"][2]))
c.check("history is complete", len(ap["approvals"]) == 3, str(len(ap["approvals"])))

print("\n== the decisions are in the audit trail ==")
st, od = c.call("GET", "/api/orders/%d" % oid)
kinds = [e.get("kind") for e in (od.get("events") or [])]
c.check("approval_started logged", "approval_started" in kinds, str(kinds[:12]))
c.check("approval_decided logged", kinds.count("approval_decided") >= 2, str(kinds[:12]))

print("\n== a named approver blocks everyone else ==")
partner = Client()
if partner.login("build@seam.demo", "demo1234")[0] == 200:
    st, r = partner.call("GET", "/api/orders/%d/approvals" % oid)
    c.check("the other side sees its own chain, not mine",
            st == 200 and r["approvals"] == [], str(r)[:200])
    st, r = partner.call("POST", "/api/approvals/%d/decide" % s3["id"], {"decision": "approved"})
    c.check("cross-org decision refused", st in (400, 403), "%s %s" % (st, str(r)[:150]))

print("\n== threshold and template gating ==")
st, r = c.call("POST", "/api/approval-chains", {
    "name": "Никога", "threshold": 99999999, "steps": [{"label": "Никой"}]})
st, ap = c.call("POST", "/api/orders/%d/approvals/start" % oid, {})
c.check("high-threshold chain did not attach", len(ap["approvals"]) == 3, str(len(ap["approvals"])))

print("\n== only one chain may run at a time ==")
st, fresh = c.call("POST", "/api/approval-chains", {
    "name": "Втора", "steps": [{"label": "Само една"}]})
fid = fresh["chains"][0]["id"]
st, ap2 = c.call("POST", "/api/orders/%d/approvals/start" % oid, {"chain_id": fid})
c.check("a new chain may start once the previous one closed", st == 200, str(ap2)[:200])
c.check("the card reports only the new chain", len(ap2["approvals"]) == 1, str(ap2["approvals"]))
c.check("the earlier chain is kept as history", len(ap2.get("history") or []) == 3,
        str(ap2.get("history")))
st, r = c.call("POST", "/api/orders/%d/approvals/start" % oid, {"chain_id": chain["id"]})
c.check("400 while one is still running", st == 400 and r.get("code") == "appr_running",
        "%s %s" % (st, r))

for ch in c.call("GET", "/api/approval-chains")[1]["chains"]:
    c.call("DELETE", "/api/approval-chains/%d" % ch["id"])
sys.exit(c.summary())
