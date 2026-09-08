# -*- coding: utf-8 -*-
"""Profiles, portfolio, the directory and reaching somebody through it.

Two things are on trial here.

The first is the rule that decides what a stranger sees. A published card is
an invitation to be contacted, not a licence to harvest: the telephone number,
the registration number and the staff list stay behind the line, and the only
way through is a request the owner has to act on. Half this file tries to get
past that line - as a signed-in stranger, through the portfolio, through the
document shelf, through another company's entries - and expects to fail.

The second is that a profile cannot lie about how complete it is. The tier is
computed from the same rules that gate publication, so a company cannot appear
in the directory as a blank card, and cannot be talked into publishing one by
flipping the switch through the API.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from seamclient import Client                                # noqa: E402
import profiles                                              # noqa: E402

c = Client()
c.login()

# --------------------------------------------------------------------------- #
print("== the rules, on their own ==")
# The module has its own self-test; these are the two decisions the endpoints
# depend on, checked here so a failure points at the right file.
c.check("a blank profile is not listable", not profiles.may_list({}))
c.check("posture falls back to the account kind",
        profiles.posture("business", None) == "hiring" and
        profiles.posture("partner", None) == "seeking")
c.check("a number with brackets still dials",
        profiles.phone_href("(02) 981-1234") == "tel:029811234")
c.check("a stranger is offered a request, not a number",
        profiles.reach({"phone": "+359888123456", "name": "x", "country": "BG",
                        "posture": "hiring", "headline": "x" * 20,
                        "sectors": ("construction",)}, False)["phone"] == "")

# --------------------------------------------------------------------------- #
print("\n== my own profile ==")
st, me = c.call("GET", "/api/profile")
mine = me.get("company") or {}
c.check("the company profile carries a tier", st == 200 and mine.get("tier") in profiles.TIERS,
        "%s %s" % (st, mine.get("tier")))
c.check("and a percentage", isinstance(mine.get("completeness"), int), str(mine.get("completeness")))
c.check("and a checklist of every requirement",
        len(mine.get("checklist") or []) == len(profiles.ALL_REQUIREMENTS),
        str(len(mine.get("checklist") or [])))
c.check("the posture is stated, never absent", mine.get("posture") in profiles.POSTURES,
        str(mine.get("posture")))
my_org = mine.get("id")

st, r = c.call("PUT", "/api/profile/company", {"posture": "nonsense"})
c.check("an invented posture is refused", st == 400 and r.get("code") == "invalid_value",
        "%s %s" % (st, r))
st, r = c.call("PUT", "/api/profile/company", {"founded": "1500"})
c.check("a founding year before the industrial age is refused", st == 400, str(st))
st, r = c.call("PUT", "/api/profile/company", {"founded": "2199"})
c.check("and one in the future too", st == 400, str(st))
st, r = c.call("PUT", "/api/profile/company", {"contact_email": "not an address"})
c.check("an unusable office address is refused",
        st == 400 and r.get("code") == "invalid_email", "%s %s" % (st, r))

st, r = c.call("PUT", "/api/profile/company", {
    "posture": "hiring", "headline": "Търговия на дребно в цялата страна",
    "size_band": "medium", "founded": "2014", "contact_email": "office@nordic.demo",
    "areas": ["София", "Пловдив", "", "Варна"]})
c.check("what was sent is stored", st == 200 and r.get("headline") and
        r.get("size_band") == "medium" and r.get("founded") == "2014", str(r)[:160])
c.check("empty areas are dropped, not stored as blanks", r.get("areas") == ["София", "Пловдив", "Варна"],
        str(r.get("areas")))
c.check("a short headline does not count as one",
        not profiles._has({"headline": "Ремонти"}, "headline"))

# One field at a time must not wipe the rest: a profile is exactly the kind of
# record people edit a line at a time.
st, r = c.call("PUT", "/api/profile/company", {"headline": "Търговия на дребно в цялата страна"})
c.check("a partial save keeps the other fields", r.get("founded") == "2014" and
        r.get("size_band") == "medium", str(r)[:120])

# --------------------------------------------------------------------------- #
print("\n== portfolio ==")
st, r = c.call("POST", "/api/portfolio", {"title": ""})
c.check("an entry without a title is refused", st == 400 and r.get("code") == "field_required",
        "%s %s" % (st, r))
st, r = c.call("POST", "/api/portfolio", {"title": "Тест", "kind": "invention"})
c.check("an invented kind is refused", st == 400 and r.get("code") == "invalid_value", str(st))
st, r = c.call("POST", "/api/portfolio", {"title": "Тест", "year": "1200"})
c.check("an impossible year is refused", st == 400, str(st))
st, r = c.call("POST", "/api/portfolio", {"title": "Тест", "url": "javascript:alert(1)"})
c.check("a script URL never becomes a link", st == 400 and r.get("code") == "invalid_value",
        "%s %s" % (st, r))

st, one = c.call("POST", "/api/portfolio", {
    "title": "Логистичен център, Божурище", "kind": "project",
    "summary": "Приемане, съхранение и експедиция за търговска верига.",
    "role": "Главен изпълнител", "place": "Божурище", "year": "2024",
    "value_band": "до 500 000", "client": "", "url": "example.com"})
c.check("an entry is created", st == 201 and one.get("id"), "%s %s" % (st, one))
c.check("a bare host is stored as a real link", one.get("url") == "https://example.com",
        str(one.get("url")))
st, two = c.call("POST", "/api/portfolio", {
    "title": "ISO 9001", "kind": "certificate", "year": "2023",
    "summary": "Система за управление на качеството, преиздаден през 2023 г."})
c.check("a second entry is created", st == 201 and two.get("id"), str(st))

st, lst = c.call("GET", "/api/portfolio")
c.check("both entries are listed", st == 200 and len(lst.get("items") or []) >= 2, str(st))
c.check("the list says whose it is", lst.get("mine") is True, str(lst.get("mine")))
c.check("and offers the kinds it will accept",
        set(lst.get("kinds") or []) == set(profiles.PORTFOLIO_KINDS), str(lst.get("kinds")))

st, r = c.call("PATCH", "/api/portfolio/%d" % one["id"], {"place": "Елин Пелин"})
c.check("an entry can be corrected", st == 200 and r.get("place") == "Елин Пелин", str(st))
c.check("and correcting one field keeps the others", r.get("year") == "2024", str(r.get("year")))

st, r = c.call("PATCH", "/api/portfolio/%d" % one["id"], {"visible": False})
c.check("an entry can be taken out of the shop window", r.get("visible") is False, str(r))
st, r = c.call("PATCH", "/api/portfolio/%d" % one["id"], {"visible": True})
c.check("and put back", r.get("visible") is True, str(r))

# Evidence: only this company's own filed documents.
def upload(client, filename, content, title):
    """File one document through the real upload route.

    The seed carries no document for this company, and a suite that skips
    itself when the fixture is missing is a suite that silently stops testing
    the thing it was written for. Multipart by hand: there is no requests here.
    """
    boundary = "----seamtest" + os.urandom(8).hex()
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\n%s\r\n"
              % (boundary, title)).encode("utf-8"),
             ("--%s\r\nContent-Disposition: form-data; name=\"files\"; filename=\"%s\"\r\n"
              "Content-Type: application/pdf\r\n\r\n" % (boundary, filename)).encode("utf-8"),
             content, ("\r\n--%s--\r\n" % boundary).encode("utf-8")]
    req = urllib.request.Request(
        client.base + "/api/files", data=b"".join(parts), method="POST",
        headers={"content-type": "multipart/form-data; boundary=" + boundary,
                 "X-CSRF": client.csrf, "accept": "application/json"})
    try:
        with client.op.open(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")


st, up = upload(c, "sertifikat.pdf", b"%PDF-1.4 seam test evidence\n%%EOF\n", "ISO 9001, обхват")
c.check("a document can be filed to hang on an entry", st == 200 and up.get("files"),
        "%s %s" % (st, str(up)[:140]))
st, files = c.call("GET", "/api/files")
own = [f for f in (files.get("files") or []) if f.get("mine")]
if own:
    st, r = c.call("POST", "/api/portfolio/%d/docs" % one["id"], {"doc_ids": [own[0]["id"]]})
    c.check("a filed document can be hung on an entry",
            st == 200 and len(r.get("item", {}).get("docs") or []) == 1, "%s %s" % (st, str(r)[:120]))
    st, r = c.call("POST", "/api/portfolio/%d/docs" % one["id"], {"doc_ids": [own[0]["id"]]})
    c.check("hanging the same one twice does not duplicate it",
            len(r.get("item", {}).get("docs") or []) == 1, str(r.get("item", {}).get("docs")))
    st, r = c.call("DELETE", "/api/portfolio/%d/docs/%d" % (one["id"], own[0]["id"]))
    c.check("and it can be taken down again", st == 200, str(st))
    st, still = c.call("GET", "/api/files")
    c.check("taking it down does not destroy the document",
            any(f["id"] == own[0]["id"] for f in (still.get("files") or [])), "gone")
    st, r = c.call("POST", "/api/portfolio/%d/docs" % one["id"], {"doc_ids": [own[0]["id"]]})
else:
    c.check("the filed document is readable back", False, "upload did not appear")

st, r = c.call("POST", "/api/portfolio/%d/docs" % one["id"], {"doc_ids": [999999]})
c.check("a document that does not exist cannot be attached", st == 404, str(st))
st, r = c.call("POST", "/api/portfolio/%d/docs" % one["id"], {"doc_ids": []})
c.check("attaching nothing is a mistake, not a silent success", st == 400, str(st))

# --------------------------------------------------------------------------- #
print("\n== publishing ==")
st, r = c.call("PUT", "/api/profile/company", {"listed": True})
listed_now = st == 200 and r.get("listed")
if not listed_now:
    c.check("an incomplete profile cannot be published",
            st == 400 and r.get("code") == "profile_incomplete", "%s %s" % (st, r))
    c.check("and the refusal says what is missing", bool((r.get("info") or {}).get("missing")),
            str(r.get("info")))
    # Fill in what it asked for, then try again.
    c.call("PUT", "/api/profile/company", {
        "about": "Търговска верига с обекти в цялата страна. Работим с доставчици "
                 "по дългосрочни рамкови договори и възлагаме транспорт и монтаж."})
    st, r = c.call("PUT", "/api/profile/company", {"listed": True})
c.check("a profile that meets the bar can be published", r.get("listed") is True, str(r)[:160])
c.check("publishing does not invent a tier",
        profiles.tier_index(r.get("tier")) >= profiles.tier_index("listed"), str(r.get("tier")))

st, d = c.call("GET", "/api/directory")
c.check("the directory answers", st == 200 and isinstance(d.get("items"), list), str(st))
c.check("and the published profile is in it",
        any(x["id"] == my_org for x in d["items"]), str([x["id"] for x in d["items"]]))
c.check("every card carries a posture",
        all(x.get("posture") in profiles.POSTURES for x in d["items"]), "missing")
c.check("no card leaks a registration number",
        not any(x.get("reg_number") for x in d["items"]), "a card carried one")
c.check("and none leaks contact details",
        not any(x.get("contacts") or x.get("reach") for x in d["items"]), "a card carried them")

st, d2 = c.call("GET", "/api/directory?posture=seeking")
c.check("filtering by posture excludes a company that only offers work",
        not any(x["id"] == my_org for x in d2["items"]), "still listed")
st, d3 = c.call("GET", "/api/directory?country=ZZ")
c.check("an unknown country matches nothing", d3.get("total") == 0, str(d3.get("total")))
# The query travels in a URL, so it must be encoded exactly as the browser
# would encode it: a Cyrillic search that only works from Python is no test.
def q(text):
    return "/api/directory?" + urllib.parse.urlencode({"q": text})

st, d4 = c.call("GET", q("цялата страна"))
c.check("the headline is searchable", any(x["id"] == my_org for x in d4["items"]), "not found")
st, d5 = c.call("GET", q("кораборемонт"))
c.check("a word nobody wrote matches nothing", d5.get("total") == 0, str(d5.get("total")))

st, r = c.call("PUT", "/api/profile/company", {"listed": False})
c.check("publication can be withdrawn", r.get("listed") is False, str(r.get("listed")))
st, d6 = c.call("GET", "/api/directory")
c.check("and the card leaves the directory",
        not any(x["id"] == my_org for x in d6["items"]), "still there")
c.call("PUT", "/api/profile/company", {"listed": True})

# --------------------------------------------------------------------------- #
print("\n== what a stranger sees ==")
# A real stranger has to be registered, not borrowed from the seed: every demo
# partner already shares a workspace with the demo business, so any of them
# would pass these checks by being entitled to the details rather than by the
# boundary holding. That would be a test that can never fail.
other = Client()
tag = os.urandom(3).hex()
st, reg = other.call("POST", "/api/auth/register", {
    "name": "Странична Фирма", "email": "stranger-%s@seam.test" % tag,
    "password": "kvartal-7719-most", "org_name": "Непозната ЕООД %s" % tag,
    "side": "buyer", "country": "BG", "reg_number": "205643632"})
if not c.check("a company with no shared history signs up", st == 200, "%s %s" % (st, str(reg)[:160])):
    sys.exit(c.summary())
other.csrf = reg.get("csrf", "")
st, om = other.call("GET", "/api/profile")
other_org = (om.get("company") or {}).get("id")
c.check("and it really is a different company", other_org and other_org != my_org,
        "%s vs %s" % (other_org, my_org))

st, card = other.call("GET", "/api/directory/%d" % my_org)
c.check("a stranger may open the published card", st == 200 and card.get("name"), str(st))
c.check("and does not get the telephone number", not (card.get("contacts") or {}), str(card.get("contacts")))
c.check("nor the registration number", not card.get("reg_number"), str(card.get("reg_number")))
c.check("nor the staff list", card.get("people") == [], str(card.get("people")))
c.check("but is told a way through exists",
        (card.get("reach") or {}).get("can_request") is True, str(card.get("reach")))
c.check("the portfolio is part of the card", isinstance(card.get("portfolio"), list),
        str(type(card.get("portfolio"))))

# The hidden entry must not appear to anyone else.
c.call("PATCH", "/api/portfolio/%d" % two["id"], {"visible": False})
st, card2 = other.call("GET", "/api/directory/%d" % my_org)
c.check("a hidden entry is hidden from the other side",
        not any(w["id"] == two["id"] for w in card2.get("portfolio") or []), "it showed")
st, mine_list = c.call("GET", "/api/portfolio")
c.check("but its owner still sees it",
        any(w["id"] == two["id"] for w in mine_list.get("items") or []), "gone from my own list")
c.call("PATCH", "/api/portfolio/%d" % two["id"], {"visible": True})

st, r = other.call("GET", "/api/portfolio?org_id=%d" % my_org)
c.check("a stranger may read the published portfolio", st == 200, str(st))
c.check("and is told it is not theirs", r.get("mine") is False, str(r.get("mine")))
st, r = other.call("PATCH", "/api/portfolio/%d" % one["id"], {"title": "Взето"})
c.check("but may not rewrite it", st in (403, 404), str(st))
st, r = other.call("DELETE", "/api/portfolio/%d" % one["id"])
c.check("nor delete it", st in (403, 404), str(st))
st, r = other.call("POST", "/api/portfolio/%d/docs" % one["id"], {"doc_ids": [1]})
c.check("nor hang anything on it", st in (403, 404), str(st))

st, r = c.call("PUT", "/api/profile/company", {"listed": False})
st, r = other.call("GET", "/api/directory/%d" % my_org)
c.check("an unpublished profile is not there at all for a stranger", st == 404, str(st))
st, r = other.call("GET", "/api/portfolio?org_id=%d" % my_org)
c.check("and neither is its portfolio", st == 404, str(st))
c.call("PUT", "/api/profile/company", {"listed": True})

# --------------------------------------------------------------------------- #
print("\n== asking to be contacted ==")
st, r = other.call("POST", "/api/directory/%d/contact" % my_org, {"phone": "не е телефон"})
c.check("an unusable number is refused", st == 400 and r.get("code") == "invalid_value",
        "%s %s" % (st, r))
st, r = other.call("POST", "/api/directory/%d/contact" % my_org, {"email": "nonsense"})
c.check("an unusable address is refused", st == 400 and r.get("code") == "invalid_email", str(st))
st, r = other.call("POST", "/api/directory/%d/contact" % my_org, {})
c.check("a request with neither a number nor a message is refused", st == 400, str(st))

st, r = other.call("POST", "/api/directory/%d/contact" % my_org,
                   {"phone": "+359 888 123 456", "note": "Търсим доставчик за Q4."})
c.check("a request goes through", st == 201 and r.get("ok"), "%s %s" % (st, r))
c.check("and says honestly whether it was emailed", "delivered" in r, str(r))

if other_org:
    st, r = other.call("POST", "/api/directory/%d/contact" % other_org, {"note": "x"})
    c.check("a company cannot file a request against itself",
            st == 400 and r.get("code") == "own_org", "%s %s" % (st, r))

st, box = c.call("GET", "/api/contact-requests")
c.check("the request arrives in the owner's list", st == 200 and box.get("items"), str(st))
top = (box.get("items") or [{}])[0]
c.check("with the caller's number as a link that dials",
        top.get("phone_href", "").startswith("tel:+359888123456"), str(top.get("phone_href")))
c.check("and their address as a link that writes",
        top.get("email_href", "").startswith("mailto:"), str(top.get("email_href")))
c.check("and their message", "Q4" in (top.get("note") or ""), str(top.get("note")))
st, r = other.call("GET", "/api/contact-requests")
c.check("nobody else sees what came in",
        not any("Q4" in (x.get("note") or "") for x in r.get("items") or []), "leaked")

# Hard rate limit: this is the only route to a company nobody has worked with.
blocked = False
for _ in range(8):
    st, r = other.call("POST", "/api/directory/%d/contact" % my_org, {"note": "again"})
    if st == 429:
        blocked = True
        break
c.check("a stranger cannot send request after request", blocked, "no limit hit in 8 tries")

# --------------------------------------------------------------------------- #
print("\n== the message inbox ==")
st, inbox = c.call("GET", "/api/messages")
c.check("the inbox answers", st == 200 and isinstance(inbox.get("items"), list), str(st))
c.check("every row is identifiable",
        all(r.get("org") for r in inbox["items"]),
        str([r for r in inbox["items"] if not r.get("org")][:2]))
c.check("a workspace nobody has joined is marked, not left blank",
        all(r.get("org") for r in inbox["items"] if r.get("pending")),
        str([r for r in inbox["items"] if r.get("pending")][:2]))
c.check("and counts what has not been read",
        all(isinstance(r.get("unread"), int) for r in inbox["items"]), "not a number")
if inbox["items"]:
    ws = inbox["items"][0]["workspace_id"]
    c.call("GET", "/api/workspaces/%d/messages" % ws)
    st, again = c.call("GET", "/api/messages")
    row = [r for r in again["items"] if r["workspace_id"] == ws][0]
    c.check("opening a conversation clears its unread count", row["unread"] == 0, str(row))

sys.exit(c.summary())
