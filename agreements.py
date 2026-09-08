# -*- coding: utf-8 -*-
"""When an agreement needs somebody's attention again.

Two companies agree a price, a date and a scope, and then the agreement sits
there. Three things can happen to it and none of them announce themselves: the
date comes closer, the date passes, or the terms quietly stop matching what the
work has become. This works out which of those has happened, on what day, and
for which term.

No Flask, no database. The rules are arithmetic on dates, so they are testable
on their own and are tested by self_test() at the bottom.

    py -3 agreements.py       # run the self-test
"""
import datetime

#: How long before an agreed date somebody wants to hear about it. Three days
#: is short enough to still be actionable and long enough to move a lorry.
DEFAULT_LEAD_DAYS = 3

#: A review cadence is a number of days, and these are the ones offered in the
#: interface. Anything is accepted; these are what a person actually picks.
REVIEW_CADENCES = (30, 90, 180, 365)

#: Every alert this module can raise, in the order of how much they interrupt.
KINDS = ("term_review", "term_due_soon", "term_due_today", "term_overdue")


def parse_date(value):
    """A date out of whatever the term holds, or None.

    Term values are free text: a date term usually holds an ISO date, but a
    person can type into it, so anything unparseable is simply not a date and
    raises nothing.
    """
    if isinstance(value, datetime.date):
        return value
    text = (value or "").strip()[:10]
    if len(text) != 10:
        return None
    try:
        return datetime.date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except (ValueError, TypeError):
        return None


def next_review(agreed_on, every_days, today):
    """The next day this agreement is due a look, at or after `today`.

    Counted from when it was agreed, not from the last reminder: an agreement
    reviewed on time and one reviewed late should both come round again on the
    same anniversary, or a slipped review moves every later one with it.
    """
    start = parse_date(agreed_on)
    if not start or not every_days or every_days < 1:
        return None
    if today <= start:
        return start + datetime.timedelta(days=every_days)
    gone = (today - start).days
    periods = gone // every_days
    due = start + datetime.timedelta(days=periods * every_days)
    return due if due >= today else due + datetime.timedelta(days=every_days)


def term_alerts(value, state, today, lead_days=DEFAULT_LEAD_DAYS,
                review_every=None, agreed_on=None, active=True):
    """[(kind, on_date), ...] for one term, today.

    A proposed term is not an agreement yet, so only an agreed one can fall
    due or go late; a review, on the other hand, only makes sense for something
    already agreed. A closed record raises nothing at all: chasing a deadline
    on finished work is how people learn to ignore a system.
    """
    out = []
    if not active or state != "agreed":
        return out

    day = parse_date(value)
    if day:
        delta = (day - today).days
        if delta < 0:
            out.append(("term_overdue", day))
        elif delta == 0:
            out.append(("term_due_today", day))
        elif delta <= max(0, lead_days):
            out.append(("term_due_soon", day))

    due = next_review(agreed_on, review_every, today)
    if due and due == today:
        out.append(("term_review", due))
    return out


def alert_key(kind, order_id, term_key, on_date):
    """What makes an alert the same alert.

    The date is part of it on purpose. An overdue term raises one alert for the
    date it missed and never again, so somebody who has decided to let it slide
    is not told about it every morning for a month.
    """
    return "%s:%s:%s:%s" % (kind, order_id, term_key, on_date.isoformat())


def summarise(alerts):
    """{kind: count} for a set of alerts, in KINDS order."""
    counts = dict((k, 0) for k in KINDS)
    for kind, _day in alerts:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


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

    print("== a date is read out of whatever the term holds ==")
    check("an ISO date", parse_date("2026-07-28") == d(2026, 7, 28))
    check("a timestamp keeps its date", parse_date("2026-07-28 14:03:01") == d(2026, 7, 28))
    check("prose is not a date", parse_date("as soon as possible") is None)
    check("empty is not a date", parse_date("") is None and parse_date(None) is None)
    check("the 31st of February is not a date", parse_date("2026-02-31") is None)

    print("\n== a deadline approaching, arriving and passing ==")
    soon = term_alerts("2026-06-17", "agreed", today)
    check("three days out is due soon", soon == [("term_due_soon", d(2026, 6, 17))], str(soon))
    check("today is due today",
          term_alerts("2026-06-15", "agreed", today) == [("term_due_today", d(2026, 6, 15))])
    check("yesterday is overdue",
          term_alerts("2026-06-14", "agreed", today) == [("term_overdue", d(2026, 6, 14))])
    check("far off says nothing", term_alerts("2026-09-01", "agreed", today) == [])
    check("the lead time is the caller's",
          term_alerts("2026-06-25", "agreed", today, lead_days=14) ==
          [("term_due_soon", d(2026, 6, 25))])

    print("\n== what is not an agreement raises nothing ==")
    check("a proposal is not agreed", term_alerts("2026-06-14", "proposed", today) == [])
    check("a closed record is left alone",
          term_alerts("2026-06-14", "agreed", today, active=False) == [])

    print("\n== when an agreement is due another look ==")
    # Two whole periods from the 15th of January is the 14th of July, not the
    # 15th: ninety days is ninety days, not three months.
    check("counted from the day it was agreed",
          next_review("2026-01-15", 90, today) == d(2026, 7, 14),
          str(next_review("2026-01-15", 90, today)))
    check("the anniversary itself counts as due",
          next_review("2026-03-15", 92, d(2026, 6, 15)) == d(2026, 6, 15))
    check("a slipped review does not move the next one",
          next_review("2026-01-15", 30, d(2026, 6, 15)) == d(2026, 7, 14),
          str(next_review("2026-01-15", 30, d(2026, 6, 15))))
    check("no cadence, no review", next_review("2026-01-15", None, today) is None)
    check("no agreed date, no review", next_review(None, 90, today) is None)
    check("a cadence of zero is not a cadence", next_review("2026-01-15", 0, today) is None)

    print("\n== a review and a deadline can fall on the same day ==")
    both = term_alerts("2026-06-15", "agreed", today, review_every=30, agreed_on="2026-05-16")
    check("both are raised", sorted(k for k, _ in both) == ["term_due_today", "term_review"],
          str(both))

    print("\n== the same alert twice is the same alert ==")
    k1 = alert_key("term_overdue", 7, "deadline", d(2026, 6, 14))
    k2 = alert_key("term_overdue", 7, "deadline", d(2026, 6, 14))
    check("same day, same key", k1 == k2)
    check("a different missed date is a different alert",
          k1 != alert_key("term_overdue", 7, "deadline", d(2026, 6, 13)))
    check("a different term is a different alert",
          k1 != alert_key("term_overdue", 7, "milestone_date", d(2026, 6, 14)))

    print("\n== counting ==")
    counts = summarise([("term_overdue", today), ("term_overdue", today),
                        ("term_review", today)])
    check("counted by kind", counts["term_overdue"] == 2 and counts["term_review"] == 1)
    check("every kind is present", set(counts) == set(KINDS), str(sorted(counts)))

    print("\n---- %d passed, %d failed ----" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
