# -*- coding: utf-8 -*-
"""The digital passport, one level below the counterparty: a record (project,
vehicle, shipment) and a product."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client(); c.login()
ws, order = c.first_order()
oid = order["id"]

print("== record passport ==")
st, p = c.call("GET", "/api/passport/order/%d" % oid)
c.check("200", st == 200, str(p)[:200])
c.check("kind is order", p.get("kind") == "order", str(p.get("kind")))
for k in ("subject", "fields", "terms", "stats", "signatures", "approvals",
          "payouts", "attachments", "events"):
    c.check("section %s" % k, k in p, str(list(p))[:150])

s = p["subject"]
c.check("ref carried", s["ref"] == order["ref"], str(s)[:150])
c.check("status label localized", bool(s["status_label"]), str(s))
c.check("workspace named", bool(s["workspace"]["name"]), str(s["workspace"]))
c.check("counterparty named", s["counterparty"] and bool(s["counterparty"]["name"]),
        str(s.get("counterparty")))
c.check("value is an amount", p["stats"]["value"].startswith("EUR "), str(p["stats"]))
c.check("terms have labels, not raw keys",
        all(t["label"] and t["label"] != t["key"] for t in p["terms"]) or not p["terms"],
        str(p["terms"][:2]))
c.check("events newest first",
        len(p["events"]) < 2 or p["events"][0]["id"] > p["events"][-1]["id"], "")
c.check("approvals progress reported", "approvals" in p["stats"], str(p["stats"]))
print("     %s · %s · %s · %d events, %d signatures, %d payouts"
      % (s["ref"], s["status_label"], p["stats"]["value"], len(p["events"]),
         len(p["signatures"]), len(p["payouts"])))

print("\n== everything is joined up ==")
st, sigs = c.call("GET", "/api/signatures?order_id=%d" % oid)
lst = (sigs.get("signatures") if isinstance(sigs, dict) else sigs) or []
c.check("signature count matches the order", len(p["signatures"]) == len(lst),
        "%d vs %d" % (len(p["signatures"]), len(lst)))
if p["signatures"]:
    c.check("signature carries its document hash", len(p["signatures"][0]["hash"]) == 64,
            str(p["signatures"][0]))

st, pay = c.call("POST", "/api/workspaces/%d/payouts" % ws["id"],
                 {"amount": "500", "description": "Passport test", "order_id": oid})
st, p2 = c.call("GET", "/api/passport/order/%d" % oid)
c.check("a new payout shows up on the passport", len(p2["payouts"]) == len(p["payouts"]) + 1,
        "%d -> %d" % (len(p["payouts"]), len(p2["payouts"])))
for x in (pay.get("payouts") or []):
    if x["description"] == "Passport test":
        c.call("DELETE", "/api/payouts/%d" % x["id"])

print("\n== access control ==")
out = Client()
st, r = out.call("GET", "/api/passport/order/%d" % oid)
c.check("anonymous refused", st == 401, str(st))
st, r = c.call("GET", "/api/passport/order/999999")
c.check("unknown record 404", st == 404, str(st))

stranger = Client()
if stranger.login("fulfil@seam.demo", "demo1234")[0] == 200:
    st, r = stranger.call("GET", "/api/passport/order/%d" % oid)
    c.check("a company outside the workspace is refused", st in (403, 404),
            "%s %s" % (st, str(r)[:120]))

print("\n== product passport ==")
st, prods = c.call("GET", "/api/store/products")
plist = (prods.get("products") if isinstance(prods, dict) else prods) or []
if plist:
    pid = plist[0]["id"]
    st, pp = c.call("GET", "/api/passport/product/%d" % pid)
    c.check("200", st == 200, str(pp)[:200])
    c.check("kind is product", pp.get("kind") == "product", str(pp.get("kind")))
    c.check("name carried", bool(pp["subject"]["name"]), str(pp["subject"])[:150])
    c.check("availability computed", "available" in pp["subject"], str(pp["subject"])[:150])
    c.check("revenue is an amount", pp["stats"]["revenue"].startswith("EUR "), str(pp["stats"]))
    c.check("order history present", "orders" in pp, str(list(pp)))
    c.check("fulfilled never exceeds orders", pp["stats"]["fulfilled"] <= pp["stats"]["orders"],
            str(pp["stats"]))
    print("     %s · %d orders · %s" % (pp["subject"]["name"], pp["stats"]["orders"],
                                        pp["stats"]["revenue"]))
    st, r = c.call("GET", "/api/passport/product/999999")
    c.check("unknown product 404", st == 404, str(st))
    other = Client()
    if other.login("build@seam.demo", "demo1234")[0] == 200:
        st, r = other.call("GET", "/api/passport/product/%d" % pid)
        c.check("another company cannot read my product", st in (400, 403, 404),
                "%s %s" % (st, str(r)[:120]))
else:
    print("  SKIP  no products seeded")

sys.exit(c.summary())
