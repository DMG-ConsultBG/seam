# -*- coding: utf-8 -*-
"""The roadmap's own example: contract -> invoice. A rule that prepares an
invoice when a term is agreed must really produce it."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client(); c.login()
partner = Client()
if partner.login("build@seam.demo", "demo1234")[0] != 200:
    print("partner login failed"); sys.exit(1)

st, a = c.call("GET", "/api/automations")
for r in a.get("automations", []):
    c.call("DELETE", "/api/automations/%d" % r["id"])

print("== the action is offered ==")
st, a = c.call("GET", "/api/automations")
c.check("create_document in the catalogue", "create_document" in a["actions"], str(a["actions"]))
c.check("document kinds offered", "invoice" in (a.get("docs") or []), str(a.get("docs"))[:200])

print("\n== an unknown document kind is rejected at run time ==")
st, r = c.call("POST", "/api/automations", {
    "name": "Лош документ", "trigger": "term.agreed",
    "actions": [{"type": "create_document", "doc": "not-a-doc"}]})
bad = r["automations"][0]

print("\n== contract -> invoice ==")
st, r = c.call("POST", "/api/automations", {
    "name": "Договорено условие → фактура", "trigger": "term.agreed",
    "actions": [{"type": "create_document", "doc": "invoice"}]})
rule = [x for x in r["automations"] if x["name"].endswith("фактура")][0]
c.check("rule stored with its document kind", rule["actions"][0].get("doc") == "invoice",
        str(rule["actions"]))

ws, order = c.first_order()
oid = order["id"]
st, pod = partner.call("GET", "/api/orders/%d" % oid)
money = [x for x in (pod.get("terms") or []) if x.get("type") == "money"]
if not money:
    print("  SKIP  no money term on this order"); sys.exit(c.summary())
partner.call("POST", "/api/orders/%d/terms" % oid, {"key": money[0]["key"], "value": "EUR 7700"})

st, od = c.call("GET", "/api/orders/%d" % oid)
prop = [x for x in (od.get("terms") or []) if x.get("state") == "proposed" and x.get("awaiting_you")]
c.check("a term is awaiting me", bool(prop), str(od.get("terms"))[:200])
st, r = c.call("POST", "/api/orders/%d/terms/%s/accept" % (oid, prop[0]["key"]), {})
c.check("term agreed", st == 200, str(r)[:150])

st, a = c.call("GET", "/api/automations")
fired = [x for x in a["automations"] if x["id"] == rule["id"]][0]
failed = [x for x in a["automations"] if x["id"] == bad["id"]][0]
c.check("the rule fired", fired["runs"] >= 1, str(fired["recent"])[:200])
c.check("run says create_document: invoice",
        any("create_document: invoice" in (x["detail"] or "") for x in fired["recent"]),
        str(fired["recent"])[:250])
c.check("the bad kind was refused, not silently accepted",
        any("unknown kind" in (x["detail"] or "") for x in failed["recent"]),
        str(failed["recent"])[:250])

print("\n== the document is on the timeline and reachable ==")
st, od = c.call("GET", "/api/orders/%d" % oid)
ev = [e for e in (od.get("events") or []) if e.get("kind") == "document_ready"]
c.check("document_ready event logged", bool(ev), str([e.get("kind") for e in od.get("events", [])][:8]))
import json
meta = json.loads((ev[0].get("meta_json") or "{}")) if ev else {}
c.check("event carries the link", (meta.get("url") or "").startswith("/docs/invoice?"),
        str(meta)[:250])
c.check("link is prefilled with both parties",
        "a=" in meta.get("url", "") and "b=" in meta.get("url", ""), str(meta.get("url"))[:200])
c.check("link carries the agreed amount", "amount=" in meta.get("url", ""), str(meta.get("url"))[:200])

st, html = c.call("GET", meta["url"], raw=True)
c.check("the document actually renders", st in (200, 402) and len(html) > 500, str(st))

st, n = c.call("GET", "/api/notifications")
c.check("the team was notified",
        any(x.get("kind") == "document_ready" for x in (n.get("items") or [])),
        str((n.get("items") or [])[:2])[:200])

for x in c.call("GET", "/api/automations")[1]["automations"]:
    c.call("DELETE", "/api/automations/%d" % x["id"])
sys.exit(c.summary())
