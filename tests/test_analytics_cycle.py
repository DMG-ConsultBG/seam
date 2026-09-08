# -*- coding: utf-8 -*-
"""Closing an order must move it out of `open`, into `closed`, and produce a
cycle time derived from the audit trail (not from updated_at)."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client()
c.login()

st, before = c.call("GET", "/api/analytics")
ws, order = c.first_order()
st, tpls = c.call("GET", "/api/templates")          # keyed by template key
spec = tpls.get(order["template"]) or {}
stages = [s["key"] if isinstance(s, dict) else s for s in (spec.get("stages") or [])]
if not stages:
    print("could not resolve stages for", order["template"], "->", str(spec)[:300])
    sys.exit(1)
terminal = stages[-1]
print("order %s: %s -> %s" % (order["ref"], order["status"], terminal))

st, r = c.call("POST", "/api/orders/%d/status" % order["id"], {"status": terminal})
c.check("order moved to the final stage", st == 200, str(r)[:200])

st, after = c.call("GET", "/api/analytics")
c.check("closed count went up",
        after["productivity"]["closed"] == before["productivity"]["closed"] + 1,
        "%s -> %s" % (before["productivity"]["closed"], after["productivity"]["closed"]))
c.check("open count went down",
        after["productivity"]["open"] == before["productivity"]["open"] - 1,
        "%s -> %s" % (before["productivity"]["open"], after["productivity"]["open"]))
c.check("cycle time is now computed", after["productivity"]["avg_cycle_days"] is not None,
        str(after["productivity"]["avg_cycle_days"]))
c.check("cycle time is not negative", (after["productivity"]["avg_cycle_days"] or 0) >= 0, "")
c.check("completion rate rose",
        after["productivity"]["completion_rate"] > before["productivity"]["completion_rate"],
        "%s -> %s" % (before["productivity"]["completion_rate"], after["productivity"]["completion_rate"]))
c.check("closed order no longer counted as open value",
        float(after["revenue"]["open"][4:]) <= float(before["revenue"]["open"][4:]) + 0.01,
        "%s -> %s" % (before["revenue"]["open"], after["revenue"]["open"]))
c.check("agreed revenue unchanged by closing",
        after["revenue"]["agreed"] == before["revenue"]["agreed"],
        "%s -> %s" % (before["revenue"]["agreed"], after["revenue"]["agreed"]))
print("     cycle=%s days, closed series=%s"
      % (after["productivity"]["avg_cycle_days"],
         [s["closed"] for s in after["productivity"]["series"]]))

# put it back so the demo data stays as it was
c.call("POST", "/api/orders/%d/status" % order["id"], {"status": order["status"]})
sys.exit(c.summary())
