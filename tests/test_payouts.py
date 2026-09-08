# -*- coding: utf-8 -*-
"""Payouts: only the payer records and settles them, the payee sees their own,
and the whole thing lands in the audit trail."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

biz = Client(); biz.login()
part = Client()
if part.login("build@seam.demo", "demo1234")[0] != 200:
    print("partner login failed"); sys.exit(1)

ws, order = biz.first_order()
wsid, oid = ws["id"], order["id"]

st, p = biz.call("GET", "/api/workspaces/%d/payouts" % wsid)
for x in p.get("payouts", []):
    if x["status"] != "paid":
        biz.call("DELETE", "/api/payouts/%d" % x["id"])

print("== empty state ==")
st, p = biz.call("GET", "/api/workspaces/%d/payouts" % wsid)
biz.check("200", st == 200, str(p)[:200])
biz.check("methods offered", "bank" in p["methods"], str(p["methods"]))
biz.check("totals present", set(p["totals"]) == {"due", "paid"}, str(p["totals"]))

print("\n== validation ==")
st, r = biz.call("POST", "/api/workspaces/%d/payouts" % wsid, {"amount": ""})
biz.check("amount required", st == 400 and r.get("code") == "field_required", "%s %s" % (st, r))
st, r = biz.call("POST", "/api/workspaces/%d/payouts" % wsid, {"amount": "0"})
biz.check("zero refused", st == 400, "%s %s" % (st, str(r)[:120]))

st, base = biz.call("GET", "/api/workspaces/%d/payouts" % wsid)
base_due = float(base["totals"]["due"][4:])
base_paid = float(base["totals"]["paid"][4:])
base_n = len(base["payouts"])

print("\n== recording one ==")
st, p = biz.call("POST", "/api/workspaces/%d/payouts" % wsid,
                 {"amount": "1250", "description": "Втори етап", "method": "bank",
                  "reference": "INV-2026-014", "order_id": oid, "due_date": "2026-09-15"})
biz.check("created", st == 200 and len(p["payouts"]) == base_n + 1, str(p)[:250])
po = [x for x in p["payouts"] if x["reference"] == "INV-2026-014"][0]
biz.check("currency defaulted to EUR", po["amount"].startswith("EUR"), po["amount"])
biz.check("status due", po["status"] == "due", po["status"])
biz.check("linked to the order", po["order_ref"] == order["ref"], str(po))
biz.check("payer is me", po["i_pay"] is True, str(po))
biz.check("due total rose by the amount",
          abs(float(p["totals"]["due"][4:]) - (base_due + 1250)) < 0.01, str(p["totals"]))

print("\n== the payee sees it, as money owed to them ==")
st, pp = part.call("GET", "/api/workspaces/%d/payouts" % wsid)
mine_p = [x for x in pp["payouts"] if x["id"] == po["id"]]
biz.check("partner sees the payout", st == 200 and bool(mine_p), str(pp)[:200])
biz.check("partner is not the payer", mine_p[0]["i_pay"] is False, str(mine_p[0]))

print("\n== only the payer may settle it ==")
st, r = part.call("PATCH", "/api/payouts/%d" % po["id"], {"status": "paid"})
biz.check("403 payout_not_payer", st == 403 and r.get("code") == "payout_not_payer",
          "%s %s" % (st, r))
st, r = part.call("DELETE", "/api/payouts/%d" % po["id"])
biz.check("payee cannot delete either", st == 403, "%s %s" % (st, str(r)[:120]))

print("\n== bad status refused ==")
st, r = biz.call("PATCH", "/api/payouts/%d" % po["id"], {"status": "whatever"})
biz.check("400 payout_status", st == 400 and r.get("code") == "payout_status", "%s %s" % (st, r))

print("\n== marking it paid ==")
st, p = biz.call("PATCH", "/api/payouts/%d" % po["id"], {"status": "paid", "note": "по банков път"})
paid = [x for x in p["payouts"] if x["id"] == po["id"]][0]
biz.check("status paid", paid["status"] == "paid", paid["status"])
biz.check("paid_at recorded", bool(paid["paid_at"]), str(paid))
biz.check("note kept", paid["note"] == "по банков път", str(paid))
biz.check("totals moved by exactly this amount",
          abs(float(p["totals"]["due"][4:]) - base_due) < 0.01 and
          abs(float(p["totals"]["paid"][4:]) - (base_paid + 1250)) < 0.01, str(p["totals"]))

print("\n== a paid amount is not deletable ==")
st, r = biz.call("DELETE", "/api/payouts/%d" % po["id"])
biz.check("400 payout_paid", st == 400 and r.get("code") == "payout_paid", "%s %s" % (st, r))

print("\n== it is in the audit trail and the partner was notified ==")
st, od = biz.call("GET", "/api/orders/%d" % oid)
kinds = [e.get("kind") for e in (od.get("events") or [])]
biz.check("payout_recorded logged", "payout_recorded" in kinds, str(kinds[:10]))
st, n = part.call("GET", "/api/notifications")
biz.check("partner notified", any(x.get("kind") == "payout_recorded" for x in (n.get("items") or [])),
          str((n.get("items") or [])[:2])[:250])

print("\n== cancelling puts it back ==")
st, p = biz.call("PATCH", "/api/payouts/%d" % po["id"], {"status": "cancelled"})
cur = [x for x in p["payouts"] if x["id"] == po["id"]][0]
biz.check("cancelled", cur["status"] == "cancelled", str(cur))
biz.check("no longer counted as paid",
          abs(float(p["totals"]["paid"][4:]) - base_paid) < 0.01, str(p["totals"]))
biz.call("DELETE", "/api/payouts/%d" % po["id"])

print("\n== an outsider gets nothing ==")
out = Client()
st, r = out.call("GET", "/api/workspaces/%d/payouts" % wsid)
biz.check("anonymous refused", st == 401, str(st))

sys.exit(biz.summary())
