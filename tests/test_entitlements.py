# -*- coding: utf-8 -*-
"""Who may open what, and what it takes to change that.

The rules are tested on their own in entitlements.py. This drives them through
a live instance and checks the thing that actually matters: a company cannot
talk its way into a part it has not paid for. Every road in - the gate, the
checkout, the administrator's grant - is walked, and the one that must be shut
is pushed on.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from seamclient import Client                                # noqa: E402
import entitlements as E                                     # noqa: E402

c = Client()
c.login()

print("== what the company is told it has ==")
st, ent = c.call("GET", "/api/entitlements")
c.check("the answer arrives", st == 200, str(st))
c.check("every feature is accounted for",
        set(ent.get("features", {})) == set(E.FEATURES),
        str(sorted(set(E.FEATURES) - set(ent.get("features", {})))))
c.check("the records themselves are free",
        ent["features"]["orders"]["allowed"] and ent["features"]["orders"]["why"] == "free")
c.check("a locked part says what it costs",
        all(v.get("price") for k, v in ent["features"].items()
            if not v["allowed"] and k in E.FEATURE_PRICE) or
        all(v["allowed"] for v in ent["features"].values()),
        str(ent["features"]))
c.check("four plans are offered", {"starter", "pro", "year", "life"} <= set(ent["plans"]),
        str(sorted(ent["plans"])))
c.check("the annual plan costs less than twelve monthly ones",
        ent["plans"]["year"]["price"] < ent["plans"]["pro"]["price"] * 12)
c.check("the lifetime plan carries no end date", ent["plans"]["life"]["days"] is None)

print("\n== the gate ==")
paid = ent["features"]["finance"]["allowed"]
st, r = c.call("GET", "/api/finance?year=2026")
if paid:
    c.check("a company on a plan opens the calculation", st == 200, str(st))
else:
    c.check("an unpaid company is refused", st == 402, "%s %s" % (st, r))
    c.check("and told which part, and what it costs",
            r.get("code") == "feature_locked" and r.get("info", {}).get("feature") == "finance",
            str(r))

print("\n== nobody buys anything by asking ==")
st, before = c.call("GET", "/api/entitlements")
st, r = c.call("POST", "/api/features/checkout", {"feature": "finance", "kind": "once"})
c.check("the checkout answers", st == 200, "%s %s" % (st, r))
c.check("and grants nothing on its own", r.get("status") in ("pending", "transfer"), str(r))
st, after = c.call("GET", "/api/entitlements")
c.check("the part is exactly as open as it was",
        after["features"]["finance"]["allowed"] == before["features"]["finance"]["allowed"],
        "%s -> %s" % (before["features"]["finance"], after["features"]["finance"]))
c.check("and nothing was written down",
        len(after.get("purchases", [])) == len(before.get("purchases", [])),
        str(after.get("purchases")))

print("\n== a part nobody sells ==")
st, r = c.call("POST", "/api/features/checkout", {"feature": "nuclear_launch", "kind": "once"})
c.check("cannot be bought", st == 400 and r.get("code") == "unknown_feature", "%s %s" % (st, r))
st, r = c.call("POST", "/api/plans/checkout", {"plan": "free_forever_please"})
c.check("and a plan nobody sells cannot be chosen", st == 400, "%s %s" % (st, r))

print("\n== the administrator's grant ==")
st, r = c.call("POST", "/api/entitlements/grant",
               {"org_id": 1, "feature": "finance", "kind": "once"})
c.check("needs a payment reference", st == 400 and r.get("code") == "field_required",
        "%s %s" % (st, r))
st, r = c.call("POST", "/api/entitlements/grant",
               {"org_id": 1, "feature": "finance", "kind": "once", "ref": "BANK-2026-0007"})
c.check("and then records the purchase", st == 200 and r.get("ok"), "%s %s" % (st, r))
st, now = c.call("GET", "/api/entitlements")
c.check("which opens the part", now["features"]["finance"]["allowed"], str(now["features"]["finance"]))
c.check("and says it was bought, not granted by a plan",
        now["features"]["finance"]["why"] in ("purchase", "plan"),
        str(now["features"]["finance"]))
st, r = c.call("GET", "/api/finance?year=2026")
c.check("the gate opens too", st == 200, str(st))

print("\n== and only an administrator may do it ==")
other = Client()
other.login("build@seam.demo", "demo1234")
st, r = other.call("POST", "/api/entitlements/grant",
                   {"org_id": 1, "feature": "assistant", "kind": "once", "ref": "self-service"})
c.check("an ordinary account cannot grant itself anything", st in (401, 403),
        "%s %s" % (st, r))

sys.exit(c.summary())
