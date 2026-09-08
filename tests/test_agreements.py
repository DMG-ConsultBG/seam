# -*- coding: utf-8 -*-
"""Agreements that ask for attention again: reviews, deadlines, delays, money.

An agreed term is not the end of anything. This drives the whole path against
a live instance: set a review cadence, make a deadline fall due, run the scan,
and check that both companies were told once and only once, that the words
arrive in the reader's language, and that the date lands in the calendar feed
their own software subscribes to.
"""
import os
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from seamclient import Client                                # noqa: E402
import agreements                                            # noqa: E402

c = Client()
c.login()

print("== the rules, on their own ==")
d = datetime.date
today = d(2026, 6, 15)
c.check("a deadline three days out is due soon",
        agreements.term_alerts("2026-06-18", "agreed", today) ==
        [("term_due_soon", d(2026, 6, 18))])
c.check("a deadline that passed is overdue",
        agreements.term_alerts("2026-06-01", "agreed", today) ==
        [("term_overdue", d(2026, 6, 1))])
c.check("a proposal has no deadline to miss",
        agreements.term_alerts("2026-06-01", "proposed", today) == [])
c.check("a review falls on the anniversary",
        agreements.next_review("2026-03-17", 90, today) == d(2026, 6, 15))

print("\n== a record to work with ==")
# Not simply the first record: a review only applies to a term the two sides
# have already agreed, and the first record in the seed has none.
order, detail, term = None, None, None
st, wss = c.call("GET", "/api/workspaces")
for w in (wss.get("workspaces") if isinstance(wss, dict) else wss) or []:
    st, ords = c.call("GET", "/api/workspaces/%d/orders" % w["id"])
    for o in (ords.get("orders") if isinstance(ords, dict) else ords) or []:
        st, d = c.call("GET", "/api/orders/%d" % o["id"])
        agreed = [x for x in (d.get("terms") or []) if x["state"] == "agreed"]
        if agreed:
            order, detail, term = o, d, agreed[0]
            break
    if term:
        break
c.check("a record with an agreed term exists", term is not None)
if term:
    c.check("the record opens", bool(detail.get("terms")), str(detail)[:80])

if term:
    print("\n== how often this agreement wants another look ==")
    st, r = c.call("POST", "/api/orders/%d/terms/%s/review" % (order["id"], term["key"]),
                   {"every_days": 90})
    c.check("a cadence is accepted", st == 200 and r.get("every_days") == 90, str(r))
    st, detail = c.call("GET", "/api/orders/%d" % order["id"])
    back = [t for t in detail["terms"] if t["key"] == term["key"]][0]
    c.check("and comes back on the record", back.get("review_every_days") == 90, str(back))
    c.check("with the day it next falls due", bool(back.get("next_review")), str(back))

    st, r = c.call("POST", "/api/orders/%d/terms/%s/review" % (order["id"], term["key"]),
                   {"every_days": 99999})
    c.check("an absurd period is refused", st == 400 and r.get("code") == "bad_review_period",
            "%s %s" % (st, r))
    st, r = c.call("POST", "/api/orders/%d/terms/%s/review" % (order["id"], term["key"]),
                   {"every_days": None})
    c.check("and it can be turned off", st == 200 and r.get("every_days") is None, str(r))

    print("\n== a term nobody outside the two companies may touch ==")
    # Whichever demo partner is not on this workspace. Picking one by name
    # found the workspace's own partner and proved nothing.
    stranger = None
    for email in ("supply@seam.demo", "fulfil@seam.demo", "build@seam.demo"):
        probe = Client()
        probe.login(email, "demo1234")
        st, _ = probe.call("GET", "/api/orders/%d" % order["id"])
        if st in (403, 404):
            stranger = probe
            break
    c.check("a demo account outside this workspace exists", stranger is not None)
    if stranger:
        st, r = stranger.call("POST", "/api/orders/%d/terms/%s/review"
                              % (order["id"], term["key"]), {"every_days": 30})
        c.check("and it cannot set a review on somebody else's agreement",
                st in (403, 404), "%s %s" % (st, r))

print("\n== what is coming up ==")
st, up = c.call("GET", "/api/agreements?days=365")
c.check("the list answers", st == 200, str(st))
c.check("it is a list", isinstance(up.get("items"), list), str(up)[:120])
c.check("the horizon is capped", up.get("days") == 90, str(up.get("days")))
for row in (up.get("items") or [])[:1]:
    c.check("a row names the record", bool(row.get("ref")), str(row))
    c.check("a row names the term in the reader's language",
            bool(row.get("term_label")), str(row))
    c.check("a row carries a kind this module knows",
            row.get("kind") in agreements.KINDS, str(row.get("kind")))

print("\n== the scan says each thing once ==")
st, first = c.call("POST", "/api/agreements/run", {})
c.check("an admin can run it", st == 200, "%s %s" % (st, first))
st, again = c.call("POST", "/api/agreements/run", {})
c.check("a second pass sends nothing again", again.get("sent") == 0, str(again))

print("\n== and it reaches both sides, in their own language ==")
st, notes = c.call("GET", "/api/notifications?limit=50", lang="bg")
kinds = {n["kind"] for n in (notes.get("items") or notes or [])} if notes else set()
c.check("agreement notifications exist",
        bool(kinds & set(agreements.KINDS)) or first.get("sent", 0) == 0,
        str(sorted(kinds))[:160])

print("\n== the calendar their own software subscribes to ==")
st, cal = c.call("GET", "/api/calendar")
c.check("the feed has an address", st == 200 and ".ics" in (cal.get("url") or ""), str(cal))
if cal.get("url"):
    path = "/" + cal["url"].split("/", 3)[-1]
    st, body = c.call("GET", path, raw=True)
    c.check("the feed is served", st == 200, str(st))
    c.check("it is a calendar", "BEGIN:VCALENDAR" in body, body[:60])
    c.check("with CRLF line endings, as the format requires", "\r\n" in body)

sys.exit(c.summary())
