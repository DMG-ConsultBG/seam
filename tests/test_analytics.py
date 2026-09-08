# -*- coding: utf-8 -*-
"""Analytics endpoint: shape, arithmetic and access control."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client()
st, _ = c.login()
c.check("login", st == 200)

print("\n== shape ==")
st, a = c.call("GET", "/api/analytics")
c.check("200", st == 200, str(a)[:200])
for k in ("range", "revenue", "productivity", "status", "verticals", "customers", "activity", "attention"):
    c.check("section %s" % k, k in a, str(list(a))[:120])

print("\n== revenue ==")
r = a["revenue"]
for k in ("agreed", "open", "store", "total", "series"):
    c.check("revenue.%s" % k, k in r, str(r)[:150])
c.check("amounts are EUR strings", all(r[k].startswith("EUR ") for k in ("agreed", "open", "store", "total")),
        str(r)[:150])
agreed = float(r["agreed"][4:]); store = float(r["store"][4:]); total = float(r["total"][4:])
c.check("total = agreed + store", abs(total - (agreed + store)) < 0.01, "%s vs %s+%s" % (total, agreed, store))
c.check("open <= agreed", float(r["open"][4:]) <= agreed + 0.01, str(r))
c.check("series has periods", all("period" in p and "value" in p for p in r["series"]), str(r["series"])[:150])

print("\n== productivity ==")
p = a["productivity"]
c.check("orders_total = open + closed", p["orders_total"] == p["open"] + p["closed"],
        "%s != %s + %s" % (p["orders_total"], p["open"], p["closed"]))
c.check("completion rate in range", 0 <= p["completion_rate"] <= 100, str(p["completion_rate"]))
c.check("cycle days is a number or null", p["avg_cycle_days"] is None or p["avg_cycle_days"] >= 0,
        str(p["avg_cycle_days"]))
c.check("approval days is a number or null", p["avg_approval_days"] is None or p["avg_approval_days"] >= 0,
        str(p["avg_approval_days"]))
c.check("series aligned with revenue series", len(p["series"]) == len(r["series"]), "")
print("     orders=%s open=%s closed=%s rate=%s%% cycle=%s approval=%s"
      % (p["orders_total"], p["open"], p["closed"], p["completion_rate"],
         p["avg_cycle_days"], p["avg_approval_days"]))

print("\n== breakdowns ==")
c.check("status counts sum to orders", sum(s["count"] for s in a["status"]) == p["orders_total"],
        str(a["status"])[:200])
c.check("vertical orders sum to orders", sum(v["orders"] for v in a["verticals"]) == p["orders_total"],
        str(a["verticals"])[:200])
c.check("verticals carry a template key", all("key" in v for v in a["verticals"]), "")
c.check("customers named", all(cu["name"] for cu in a["customers"]), str(a["customers"])[:200])
c.check("customer open <= orders", all(cu["open"] <= cu["orders"] for cu in a["customers"]), "")
print("     statuses=%d verticals=%d customers=%d attention=%d"
      % (len(a["status"]), len(a["verticals"]), len(a["customers"]), len(a["attention"])))

print("\n== ranges ==")
for d in (30, 90, 180, 365):
    st, x = c.call("GET", "/api/analytics?days=%d" % d)
    c.check("days=%d" % d, st == 200 and x["range"]["days"] == d, str(x.get("range")))
st, x = c.call("GET", "/api/analytics?days=7")
c.check("unknown range falls back to 90", x["range"]["days"] == 90, str(x.get("range")))
st, x = c.call("GET", "/api/analytics?days=abc")
c.check("garbage range falls back to 90", st == 200 and x["range"]["days"] == 90, str(x.get("range")))

print("\n== access control ==")
c2 = Client()
st, x = c2.call("GET", "/api/analytics")
c.check("anonymous is refused", st == 401, str(st))

print("\n== a partner sees only their own relationship ==")
c3 = Client()
st, _ = c3.login("build@seam.demo", "demo1234")
if st == 200:
    st, pa = c3.call("GET", "/api/analytics")
    c.check("partner gets analytics", st == 200, str(pa)[:150])
    if st == 200 and not pa.get("empty"):
        c.check("partner sees fewer or equal orders",
                pa["productivity"]["orders_total"] <= p["orders_total"],
                "%s vs %s" % (pa["productivity"]["orders_total"], p["orders_total"]))

sys.exit(c.summary())
