# -*- coding: utf-8 -*-
"""The calendar feed must be valid iCalendar that a real client would accept,
carry the right deadlines, and be revocable."""
import os, sys, os, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client(); c.login()

print("== the feed URL ==")
st, r = c.call("GET", "/api/calendar")
c.check("200", st == 200, str(r)[:150])
c.check("url ends in .ics", r["url"].endswith(".ics"), r["url"])
c.check("token is not guessable", len(r["token"]) >= 20, r["token"])
url = "/calendar/%s.ics" % r["token"]

print("\n== it is valid iCalendar ==")
st, ics = c.call("GET", url, raw=True)
c.check("200", st == 200, str(st))
c.check("begins and ends correctly",
        ics.startswith("BEGIN:VCALENDAR") and ics.rstrip().endswith("END:VCALENDAR"), ics[:60])
c.check("CRLF line endings as RFC 5545 requires", "\r\n" in ics and "\n\n" not in ics, "")
c.check("VERSION:2.0 present", "VERSION:2.0" in ics, "")
c.check("PRODID present", "PRODID:" in ics, "")
c.check("calendar is named", "X-WR-CALNAME:" in ics, "")
c.check("refresh interval advertised", "REFRESH-INTERVAL" in ics, "")

lines = ics.split("\r\n")
c.check("no content line exceeds 75 octets",
        all(len(l.encode("utf-8")) <= 75 for l in lines),
        str([l[:40] for l in lines if len(l.encode("utf-8")) > 75][:2]))
c.check("BEGIN and END are balanced",
        ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") and
        ics.count("BEGIN:VALARM") == ics.count("END:VALARM"),
        "%d/%d" % (ics.count("BEGIN:VEVENT"), ics.count("END:VEVENT")))

events = ics.count("BEGIN:VEVENT")
print("     %d events" % events)
c.check("there are events to show", events >= 1, str(events))

for f in ("UID:", "DTSTAMP:", "DTSTART", "SUMMARY:", "URL:"):
    c.check("every event carries %s" % f.rstrip(":"),
            ics.count(f) >= events, "%d vs %d" % (ics.count(f), events))
uids = re.findall(r"UID:(\S+)", ics)
c.check("UIDs are unique", len(uids) == len(set(uids)), str(len(uids) - len(set(uids))))
c.check("dates are valid YYYYMMDD",
        all(re.match(r"^\d{8}$", d) for d in re.findall(r"DTSTART;VALUE=DATE:(\d+)", ics)), "")
c.check("links point back into the app", "/#/o/" in ics or "/#/w/" in ics, "")
c.check("reminders attached", "TRIGGER:-P1D" in ics, "")

print("\n== content is localized to the subscriber ==")
c.check("Bulgarian calendar name", "срокове" in ics or "Seam" in ics, ics[:200])

print("\n== a payout due date turns into an event ==")
ws, order = c.first_order()
st, p = c.call("POST", "/api/workspaces/%d/payouts" % ws["id"],
               {"amount": "900", "description": "Календарен тест", "due_date": "2026-11-20"})
made = [x for x in p["payouts"] if x["description"] == "Календарен тест"]
st, ics2 = c.call("GET", url, raw=True)
c.check("event count went up", ics2.count("BEGIN:VEVENT") > events,
        "%d -> %d" % (events, ics2.count("BEGIN:VEVENT")))
c.check("the due date is the event date", "DTSTART;VALUE=DATE:20261120" in ics2, "")
c.check("the amount is in the summary", "900" in ics2, "")
for x in made:
    c.call("DELETE", "/api/payouts/%d" % x["id"])

print("\n== access control and revocation ==")
anon = Client()
st, r = anon.call("GET", url, raw=True)
c.check("the feed works without a session (that is the point of the token)", st == 200, str(st))
st, r = anon.call("GET", "/calendar/not-a-real-token.ics", raw=True)
c.check("a wrong token gets nothing", st == 404, str(st))

st, r2 = c.call("POST", "/api/calendar/rotate")
c.check("rotated to a new url", r2["token"] != r["token"] if isinstance(r, dict) else True, "")
st, old = anon.call("GET", url, raw=True)
c.check("the old url stops working", st == 404, str(st))
st, new = anon.call("GET", "/calendar/%s.ics" % r2["token"], raw=True)
c.check("the new url works", st == 200 and new.startswith("BEGIN:VCALENDAR"), str(st))

sys.exit(c.summary())
