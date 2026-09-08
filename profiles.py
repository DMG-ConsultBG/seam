# -*- coding: utf-8 -*-
"""What a company shows about itself, and how much of it a stranger may see.

Seam has two kinds of participant: one that gives out work and one that takes
it on. Until now the difference lived only in a column nobody looked at. A
company deciding whether to answer an enquiry needs to see, in one card, who
the other side is, what they have actually done, and what happens if it goes
wrong. That card is what this module describes.

Three things are kept apart on purpose:

* **Posture** - whether this company is offering work, looking for it, or both.
  It is stated by the company itself and it is the first thing on the card,
  because reading a profile with the wrong assumption wastes everybody's time.

* **Tier** - how far a profile has been filled in. Not a badge to collect: each
  tier unlocks the thing it is evidence for. A profile with no logo, no line of
  description and no trade is not shown in the directory at all, because a
  directory of blanks is worse than no directory.

* **Reach** - what a viewer may see of the contact details. Someone who already
  shares a workspace sees the phone number. Someone who does not sees a button
  that asks the owner to make the call. The number is never handed to a
  stranger, and the owner is never unreachable.

No Flask, no database, no translation. Everything here is a decision that can
be tested on its own, and it is: run this file.
"""

import re

# --------------------------------------------------------------------------- #
#  Vocabulary
# --------------------------------------------------------------------------- #
#: What this company is on the platform for. Stored on the org; when it has
#: never been stated, POSTURE_DEFAULT reads it from the account kind, which is
#: the closest honest guess.
POSTURES = ("hiring", "seeking", "both")
POSTURE_DEFAULT = {"business": "hiring", "partner": "seeking"}

#: A one-person trade and a fifty-person firm answer an enquiry very
#: differently. Bands, not a number, because nobody keeps a headcount current.
SIZES = ("solo", "small", "medium", "large")

#: Evidence of past work. "reference" is a letter from a client; a Seam
#: reference built from delivered orders is a different thing and lives in
#: reference.py, where it cannot be typed in by hand.
PORTFOLIO_KINDS = ("project", "certificate", "licence", "reference",
                   "award", "equipment", "other")

#: Ordered, weakest first. Each is a superset of the one before it.
TIERS = ("draft", "listed", "established", "verified")

#: Every requirement, in the order a person would sensibly fill them in. The
#: keys are the field names the caller must report on; the UI turns them into
#: a to-do list, so adding one here adds it to the screen.
REQUIREMENTS = {
    "listed": ("name", "country", "posture", "headline", "sectors"),
    "established": ("logo", "about", "contact", "portfolio"),
    "verified": ("reg_number", "verified"),
}

#: How many entries count as a portfolio. One project is an anecdote.
PORTFOLIO_FOR_TIER = 2
HEADLINE_MIN = 12
HEADLINE_MAX = 120
ABOUT_MIN = 80


def tier_requirements(tier):
    """Everything a profile must have to stand at `tier`, lower tiers included."""
    out = []
    for name in TIERS[1:]:
        out.extend(REQUIREMENTS[name])
        if name == tier:
            break
    return tuple(out)


ALL_REQUIREMENTS = tier_requirements(TIERS[-1])


# --------------------------------------------------------------------------- #
#  Measuring a profile
# --------------------------------------------------------------------------- #
def posture(org_kind, stated):
    """The stated posture, or the one implied by the account kind."""
    if stated in POSTURES:
        return stated
    return POSTURE_DEFAULT.get(org_kind, "seeking")


def _has(profile, field):
    """Is this one requirement met? The rules are deliberately unkind: a
    headline of three characters and an "about" of one sentence are how a
    directory fills up with profiles nobody can act on."""
    v = profile.get(field)
    if field == "sectors":
        return bool(v)
    if field == "headline":
        return bool(v) and HEADLINE_MIN <= len(v.strip()) <= HEADLINE_MAX
    if field == "about":
        return bool(v) and len(v.strip()) >= ABOUT_MIN
    if field == "portfolio":
        return int(v or 0) >= PORTFOLIO_FOR_TIER
    if field == "contact":
        return bool(v)
    if field in ("verified",):
        return bool(v)
    if field == "posture":
        return v in POSTURES
    return bool(v) and bool(str(v).strip())


def checklist(profile):
    """Every requirement with met/not met, in filling-in order.

    Returned even for a finished profile: the screen shows the ticks, which is
    the only way somebody can tell a complete profile from a lucky one.
    """
    out = []
    for tier in TIERS[1:]:
        for field in REQUIREMENTS[tier]:
            out.append({"field": field, "tier": tier, "ok": _has(profile, field)})
    return out


def missing(profile, tier="verified"):
    """The fields still standing between this profile and `tier`."""
    want = tier_requirements(tier)
    return tuple(f for f in want if not _has(profile, f))


def tier(profile):
    """The highest tier every requirement of which is met."""
    reached = TIERS[0]
    for name in TIERS[1:]:
        if any(not _has(profile, f) for f in REQUIREMENTS[name]):
            break
        reached = name
    return reached


def tier_index(name):
    return TIERS.index(name) if name in TIERS else 0


def completeness(profile):
    """A percentage, for the progress bar. Rounded down, so 99% never lies
    about being finished."""
    done = sum(1 for f in ALL_REQUIREMENTS if _has(profile, f))
    return int(done * 100 // len(ALL_REQUIREMENTS))


def may_list(profile):
    """A profile appears in the directory once it says who it is, where it is,
    what it does and what it is looking for. Not before."""
    return tier_index(tier(profile)) >= tier_index("listed")


# --------------------------------------------------------------------------- #
#  Contact details
# --------------------------------------------------------------------------- #
_TEL_KEEP = re.compile(r"[^0-9+]")
_MAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def phone_href(value):
    """A dialable link, or "" when the input is not a phone number.

    Everything but digits and a leading plus is dropped: people write numbers
    with brackets, dots and spaces, and a `tel:` with those in it is a link
    that fails silently on half the phones that follow it.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    plus = raw.startswith("+") or raw.startswith("00")
    digits = _TEL_KEEP.sub("", raw).lstrip("+")
    if raw.startswith("00"):
        digits = digits[2:]
    if not 6 <= len(digits) <= 15:
        return ""
    return "tel:" + ("+" if plus else "") + digits


def mail_href(value):
    """A mailto: link, or "" when it is not an address. No subject, no body:
    those end up in the recipient's spam heuristics, and the sender can type."""
    raw = (value or "").strip()
    if not raw or not _MAIL.match(raw) or len(raw) > 254:
        return ""
    return "mailto:" + raw


def reach(profile, shares_ground):
    """What a viewer may do with this profile's contact details.

    `shares_ground` is true when the two already share a company or a
    workspace. Then the phone and the address are theirs to use. When it is
    false the details are withheld and a request is offered instead: the owner
    gets an email with the caller's own number and decides whether to ring
    back. A directory that publishes phone numbers is a list for sale.
    """
    if shares_ground:
        return {"phone": phone_href(profile.get("phone")),
                "email": mail_href(profile.get("email")),
                "can_request": False, "withheld": False}
    return {"phone": "", "email": "",
            "can_request": may_list(profile), "withheld": bool(
                phone_href(profile.get("phone")) or mail_href(profile.get("email")))}


def forward_to(profile):
    """Where a contact request is delivered.

    The company's own stated address if it has one, otherwise the account it
    was opened with. A request that reaches nobody is worse than a button that
    is not there, so a caller is told up front which of the two applies.
    """
    for key in ("contact_email", "email"):
        got = mail_href(profile.get(key))
        if got:
            return got[len("mailto:"):]
    return ""


# --------------------------------------------------------------------------- #
#  The directory
# --------------------------------------------------------------------------- #
def matches(profile, want_posture=None, country=None, sector=None, query=None):
    """Whether a listed profile answers a search. Every filter is optional and
    they are combined with "and", which is what a person filling in two boxes
    expects."""
    if not may_list(profile):
        return False
    if want_posture in POSTURES:
        # Somebody who is both shows up in either list. That is the point of
        # saying "both": one profile, two audiences.
        mine = profile.get("posture")
        if mine != "both" and mine != want_posture:
            return False
    if country and (profile.get("country") or "").upper() != country.upper():
        return False
    if sector and sector not in (profile.get("sectors") or ()):
        return False
    if query:
        q = query.strip().lower()
        hay = " ".join(str(profile.get(k) or "") for k in ("name", "headline", "about"))
        if q and q not in hay.lower():
            return False
    return True


def rank(profile):
    """Sort key for the directory, strongest profile first.

    Deliberately not "most recently active": that rewards whoever opens the
    app most, not whoever is worth contacting. It ranks what the company has
    actually put its name to, and breaks ties by name so the order is stable.
    """
    return (-tier_index(tier(profile)), -completeness(profile),
            (profile.get("name") or "").lower())


# --------------------------------------------------------------------------- #
#  Self-test
# --------------------------------------------------------------------------- #
def self_test():
    ok, bad = 0, []

    def eq(got, want, what):
        nonlocal ok
        if got == want:
            ok += 1
        else:
            bad.append("%s: %r != %r" % (what, got, want))

    full = {"name": "Алфа", "country": "BG", "posture": "hiring",
            "headline": "Строителни работи по покриви", "sectors": ("construction",),
            "logo": "a.png", "about": "x" * ABOUT_MIN, "contact": "+359888123456",
            "portfolio": 4, "reg_number": "203344556", "verified": 1,
            "phone": "+359 888 123 456", "email": "a@b.bg"}

    eq(tier(full), "verified", "a filled profile reaches the top tier")
    eq(completeness(full), 100, "and reads as complete")
    eq(missing(full), (), "with nothing outstanding")

    empty = {}
    eq(tier(empty), "draft", "an empty profile is a draft")
    eq(may_list(empty), False, "and is not in the directory")
    eq(completeness(empty), 0, "and is 0 per cent")

    # Each tier must actually gate on its own fields.
    for field in REQUIREMENTS["listed"]:
        p = dict(full)
        p[field] = "" if field != "sectors" else ()
        eq(tier(p), "draft", "missing %s drops to draft" % field)
        eq(may_list(p), False, "missing %s is not listed" % field)
    for field in REQUIREMENTS["established"]:
        p = dict(full)
        p[field] = "" if field != "portfolio" else 0
        eq(tier(p), "listed", "missing %s stops at listed" % field)
        eq(may_list(p), True, "missing %s is still listed" % field)
    for field in REQUIREMENTS["verified"]:
        p = dict(full)
        p[field] = 0
        eq(tier(p), "established", "missing %s stops at established" % field)

    # Quality thresholds, not mere presence.
    eq(_has({"headline": "roofs"}, "headline"), False, "a three word headline is not one")
    eq(_has({"headline": "x" * (HEADLINE_MAX + 1)}, "headline"), False, "nor is an essay")
    eq(_has({"about": "We do roofs."}, "about"), False, "one sentence is not an about")
    eq(_has({"portfolio": 1}, "portfolio"), False, "one entry is not a portfolio")
    eq(_has({"portfolio": PORTFOLIO_FOR_TIER}, "portfolio"), True, "two entries are")
    eq(_has({"headline": "   " + "x" * 20}, "headline"), True, "leading space is trimmed")

    # Posture.
    eq(posture("business", None), "hiring", "a business gives work by default")
    eq(posture("partner", None), "seeking", "a partner looks for it")
    eq(posture("partner", "hiring"), "hiring", "a stated posture wins")
    eq(posture("business", "nonsense"), "hiring", "a bad value falls back")

    # Telephone links.
    eq(phone_href("+359 888 123 456"), "tel:+359888123456", "spaces are dropped")
    eq(phone_href("(02) 981-1234"), "tel:029811234", "brackets and dashes too")
    eq(phone_href("00359888123456"), "tel:+359888123456", "00 becomes +")
    eq(phone_href("12345"), "", "too short is not a number")
    eq(phone_href("1234567890123456"), "", "too long is not a number")
    eq(phone_href("нула осем осем"), "", "words are not a number")
    eq(phone_href(""), "", "empty is empty")
    eq(phone_href(None), "", "missing is empty")
    eq(mail_href("a@b.bg"), "mailto:a@b.bg", "an address links")
    eq(mail_href("a@b"), "", "a bare host is not an address")
    eq(mail_href("a b@c.bg"), "", "a space is not an address")
    eq(mail_href("x" * 250 + "@b.bg"), "", "an overlong address is refused")

    # Reach: the whole point of the feature.
    near = reach(full, True)
    eq(near["phone"], "tel:+359888123456", "a colleague may dial")
    eq(near["email"], "mailto:a@b.bg", "and may write")
    far = reach(full, False)
    eq(far["phone"], "", "a stranger gets no number")
    eq(far["email"], "", "and no address")
    eq(far["can_request"], True, "but may ask to be called")
    eq(far["withheld"], True, "and is told something is being withheld")
    eq(reach(empty, False)["can_request"], False, "an unlisted profile takes no requests")
    eq(forward_to({"email": "a@b.bg"}), "a@b.bg", "requests go to the account")
    eq(forward_to({"email": "a@b.bg", "contact_email": "office@b.bg"}), "office@b.bg",
       "the stated address wins")
    eq(forward_to({"contact_email": "bad"}), "", "an unusable address forwards nowhere")

    # Directory filtering.
    eq(matches(full, want_posture="hiring"), True, "a hiring company is in the hiring list")
    eq(matches(full, want_posture="seeking"), False, "and not in the other one")
    both = dict(full, posture="both")
    eq(matches(both, want_posture="hiring"), True, "both appears under hiring")
    eq(matches(both, want_posture="seeking"), True, "and under seeking")
    eq(matches(full, country="bg"), True, "country matching ignores case")
    eq(matches(full, country="DE"), False, "another country does not match")
    eq(matches(full, sector="construction"), True, "sector matches")
    eq(matches(full, sector="farming"), False, "another sector does not")
    eq(matches(full, query="покриви"), True, "the headline is searched")
    eq(matches(full, query="ПОКРИВИ"), True, "search ignores case")
    eq(matches(full, query="кораби"), False, "an absent word does not match")
    eq(matches(empty, query=""), False, "a draft never appears")
    eq(matches(full, want_posture="hiring", country="DE"), False, "filters are combined with and")

    # Ranking.
    weaker = dict(full, verified=0, reg_number="")
    eq(rank(full) < rank(weaker), True, "a verified profile ranks above an established one")
    a, b = dict(full, name="Бета"), dict(full, name="Алфа")
    eq(rank(b) < rank(a), True, "ties break by name")

    print("profiles.py: %d checks, %d failed" % (ok + len(bad), len(bad)))
    for line in bad:
        print("  FAIL " + line)
    return not bad


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if self_test() else 1)
