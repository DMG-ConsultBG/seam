# -*- coding: utf-8 -*-
"""Who may open what.

Three ways a company gets a part of Seam:

  a plan          everything, for a month, a year, or once and for good
  a purchase      one part, bought on its own
  free            what every account has without paying anything

The distinction that matters is between *asking* and *deciding*. This file
decides, from stored facts: a plan name, the day it runs out, and the list of
things already bought. It never sees a request, so nothing a browser sends can
reach it, and a company cannot argue its way into a feature.

No Flask, no database. Tested by self_test().

    py -3 entitlements.py
"""
import datetime

#: Everything that can be locked, and what an unpaid account still gets.
#:
#: `free` means the part is open to everybody: the record itself, the agreed
#: terms and the audit trail are what the platform is for, and charging for
#: them would be charging for the thing two companies came here to do.
#: `free_kinds` is a count, not a flag: two document types are free, the third
#: is not.
FEATURES = {
    "orders":       {"free": True},
    "analytics":    {"free": True},
    "documents":    {"free_kinds": 2},
    "finance":      {},
    "assistant":    {},
    "automations":  {},
    "network":      {},
    "store":        {},
    "vehicles":     {},
    "integrations": {},
}

#: What this instance sells. Prices are this installation's list, not a law:
#: the operator sets them, and an instance that gives everything away sets them
#: all to zero.
#:
#: `days` None with `forever` means exactly that, and it is the reason
#: plan_until is not consulted for it: a lifetime plan that expired would be a
#: contradiction, and reading a date that was never written is how one appears.
#: `months` is what the period is worth in monthly payments, which is how the
#: saving is worked out rather than written down: a half year costs five
#: months, a year costs nine, and the screen says how many are free instead of
#: leaving somebody to divide.
PLANS = {
    "starter": {"days": None, "price": 0,   "months": 0,  "all": False, "forever": False},
    "pro":     {"days": 30,   "price": 29,  "months": 1,  "all": True,  "forever": False},
    "half":    {"days": 182,  "price": 145, "months": 6,  "all": True,  "forever": False},
    "year":    {"days": 365,  "price": 261, "months": 12, "all": True,  "forever": False},
    "life":    {"days": None, "price": 690, "months": 0,  "all": True,  "forever": True},
}

#: A part bought on its own, and for how long. "once" is forever.
PURCHASE_KINDS = ("once", "month", "half", "year")
PURCHASE_DAYS = {"once": None, "month": 30, "half": 182, "year": 365}

#: What one part costs for a month on its own. The ladder has to hold in both
#: directions or one rung is dead: a part must cost less than everything, or
#: nobody buys a part; and owning a part outright must cost more than renting
#: it for a year, or nobody rents. self_test() checks both for every feature.
FEATURE_PRICE = {
    "documents": 9, "finance": 9, "assistant": 7, "automations": 6,
    "network": 6, "store": 7, "vehicles": 6, "integrations": 6,
}

#: The same ladder for a single part as for the whole platform: half a year is
#: five months of it, a year is nine, and owning it outright is twenty.
PRICE_HALF_MONTHS = 5
PRICE_YEAR_MONTHS = 9
PRICE_ONCE_MONTHS = 20


def _date(value):
    text = (value or "")[:10]
    if len(text) != 10:
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except (ValueError, TypeError):
        return None


def plan_active(plan, plan_until, today=None):
    """Whether this plan is running today.

    A lifetime plan does not consult a date. Everything else does, and a plan
    whose date has passed is not a plan: `plan_until` was once written and
    never read, and companies kept access for months after they stopped paying.
    """
    spec = PLANS.get(plan or "")
    if not spec or not spec["all"]:
        return False
    if spec["forever"]:
        return True
    end = _date(plan_until)
    return bool(end and end >= (today or datetime.date.today()))


def purchase_active(purchase, today=None):
    """One bought part: {feature, kind, until}. "once" never expires."""
    if (purchase.get("kind") or "") == "once":
        return True
    end = _date(purchase.get("until"))
    return bool(end and end >= (today or datetime.date.today()))


def has(feature, plan=None, plan_until=None, purchases=(), today=None):
    """Whether this company may open this part, today."""
    spec = FEATURES.get(feature)
    if spec is None:
        return False                      # an unknown part is not open by default
    if spec.get("free"):
        return True
    if plan_active(plan, plan_until, today):
        return True
    for p in purchases or ():
        if p.get("feature") == feature and purchase_active(p, today):
            return True
    return False


def free_kinds(feature):
    """How many of this part an unpaid account gets before it is asked to pay."""
    return int((FEATURES.get(feature) or {}).get("free_kinds") or 0)


def summary(plan=None, plan_until=None, purchases=(), today=None):
    """{feature: bool} for every feature, plus how the answer was reached.

    The reason is part of the answer on purpose: a screen that greys something
    out without saying why is a screen people complain about rather than buy
    from.
    """
    on_plan = plan_active(plan, plan_until, today)
    out = {}
    for key, spec in FEATURES.items():
        if spec.get("free"):
            out[key] = {"allowed": True, "why": "free"}
        elif on_plan:
            out[key] = {"allowed": True, "why": "plan"}
        elif any(p.get("feature") == key and purchase_active(p, today)
                 for p in purchases or ()):
            out[key] = {"allowed": True, "why": "purchase"}
        else:
            out[key] = {"allowed": False, "why": "locked",
                        "price": FEATURE_PRICE.get(key)}
    return out


def price_of(plan=None, feature=None, kind="once"):
    """What to charge, or None when there is nothing to charge for."""
    if plan:
        spec = PLANS.get(plan)
        return spec["price"] if spec else None
    base = FEATURE_PRICE.get(feature)
    if base is None:
        return None
    if kind == "once":
        return base * PRICE_ONCE_MONTHS
    if kind == "year":
        return base * PRICE_YEAR_MONTHS
    if kind == "half":
        return base * PRICE_HALF_MONTHS
    return base


def free_months(plan=None, feature=None, kind=None):
    """How many months of a period are not paid for.

    Worked out from the price rather than stated beside it, so a price that
    changes cannot leave a stale promise next to it.
    """
    if plan:
        spec = PLANS.get(plan)
        if not spec or not spec["months"]:
            return 0
        monthly = PLANS["pro"]["price"]
        return max(0, spec["months"] - round(spec["price"] / float(monthly)))
    months = {"half": 6, "year": 12}.get(kind or "")
    if not months or feature not in FEATURE_PRICE:
        return 0
    paid = price_of(feature=feature, kind=kind) / float(FEATURE_PRICE[feature])
    return max(0, months - int(round(paid)))


# --------------------------------------------------------------------------- #
def self_test():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS  %s" % name)
        else:
            fail += 1
            print("  FAIL  %s  %s" % (name, detail))

    d = datetime.date
    today = d(2026, 6, 15)

    print("== what everybody has without paying ==")
    check("the records themselves", has("orders", today=today))
    check("and what they add up to", has("analytics", today=today))
    check("but not the tax calculation", not has("finance", today=today))
    check("and not the assistant", not has("assistant", today=today))
    check("two document types are free", free_kinds("documents") == 2)

    print("\n== a plan while it runs ==")
    check("a monthly plan opens everything",
          has("finance", "pro", "2026-07-01", today=today))
    check("an annual plan does the same",
          has("assistant", "year", "2027-01-31", today=today))
    check("the day it ends still counts",
          has("finance", "pro", "2026-06-15", today=today))
    check("the day after does not",
          not has("finance", "pro", "2026-06-14", today=today))
    check("a plan with no end date is not a plan",
          not has("finance", "pro", None, today=today))
    check("and neither is a name nobody sells",
          not has("finance", "gold", "2030-01-01", today=today))

    print("\n== bought once and for good ==")
    check("a lifetime plan needs no date",
          has("finance", "life", None, today=today))
    check("and is not expired by an old one",
          has("assistant", "life", "2019-01-01", today=today))

    print("\n== one part, bought on its own ==")
    bought = [{"feature": "finance", "kind": "once"}]
    check("it opens that part", has("finance", purchases=bought, today=today))
    check("and only that part", not has("assistant", purchases=bought, today=today))
    monthly = [{"feature": "assistant", "kind": "month", "until": "2026-06-30"}]
    check("a monthly purchase runs to its date",
          has("assistant", purchases=monthly, today=today))
    check("and stops after it",
          not has("assistant", purchases=monthly, today=d(2026, 7, 1)))
    check("a purchase with no date and no kind opens nothing",
          not has("assistant", purchases=[{"feature": "assistant"}], today=today))

    print("\n== an unknown part is closed, not open ==")
    check("nothing is granted by accident", not has("nuclear_launch", "life", today=today))

    print("\n== the screen is told why, not only whether ==")
    s = summary("starter", None, [{"feature": "documents", "kind": "once"}], today)
    check("free is called free", s["orders"]["why"] == "free")
    check("a purchase is called a purchase", s["documents"]["why"] == "purchase")
    check("locked says what it costs", s["finance"]["why"] == "locked" and
          s["finance"]["price"] == FEATURE_PRICE["finance"], str(s["finance"]))
    s = summary("year", "2027-01-01", (), today)
    check("on a plan, everything is on the plan",
          all(v["allowed"] for v in s.values()) and s["finance"]["why"] == "plan")

    print("\n== the price list holds together ==")
    check("every priced feature is a real feature",
          set(FEATURE_PRICE) <= set(FEATURES), str(set(FEATURE_PRICE) - set(FEATURES)))
    check("every feature that is not free has a price",
          all(k in FEATURE_PRICE for k, v in FEATURES.items() if not v.get("free")),
          str([k for k, v in FEATURES.items() if not v.get("free") and k not in FEATURE_PRICE]))
    check("the annual plan beats twelve months of the monthly one",
          PLANS["year"]["price"] < PLANS["pro"]["price"] * 12)
    check("the half year beats six of them",
          PLANS["half"]["price"] < PLANS["pro"]["price"] * 6)
    check("and the longer period is the better one per month",
          PLANS["year"]["price"] / 12.0 < PLANS["half"]["price"] / 6.0
          < PLANS["pro"]["price"],
          "%.2f %.2f %.2f" % (PLANS["year"]["price"] / 12.0,
                              PLANS["half"]["price"] / 6.0, PLANS["pro"]["price"]))
    check("a year is three months free", free_months(plan="year") == 3,
          str(free_months(plan="year")))
    check("half a year is one", free_months(plan="half") == 1,
          str(free_months(plan="half")))
    check("a monthly plan promises nothing free", free_months(plan="pro") == 0)
    check("and neither does a lifetime one, which has no months to give",
          free_months(plan="life") == 0)
    check("the same holds for a single part",
          free_months(feature="documents", kind="year") == 3 and
          free_months(feature="documents", kind="half") == 1)
    check("owning a part promises no free months",
          free_months(feature="documents", kind="once") == 0)
    dear = [k for k in FEATURE_PRICE
            if price_of(feature=k, kind="month") >= PLANS["pro"]["price"]]
    check("no single part costs as much as everything", not dear, str(dear))
    cheap = [k for k in FEATURE_PRICE
             if price_of(feature=k, kind="once") <= price_of(feature=k, kind="year")]
    check("and owning a part costs more than renting it for a year", not cheap, str(cheap))
    over = [k for k in FEATURE_PRICE
            if price_of(feature=k, kind="year") >= PLANS["year"]["price"]]
    check("a year of one part is cheaper than a year of everything", not over, str(over))
    check("nothing is priced for a part that does not exist",
          price_of(feature="nonsense") is None)

    print("\n---- %d passed, %d failed ----" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
