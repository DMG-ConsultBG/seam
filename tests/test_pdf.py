# -*- coding: utf-8 -*-
"""The Save-as-PDF button used to open the print dialogue, which is not a file
and cannot be attached to an email. Now `/docs/<kind>.pdf` returns the document
itself.

What is worth guarding here is not that a PDF appears - it is that the right
one does: the same language the sheet was generated in, with no browser
furniture printed onto it, under a file name a mail client will accept, and
never for someone who is not entitled to the document.
"""
import os, re, sys, json, zlib, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
CSRF = {"v": ""}
ok = fail = skipped = 0


def call(method, path, data=None, raw=False, binary=False):
    c = http.cookiejar.Cookie(0, "seam_lang", "bg", None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    cj.set_cookie(c)
    b = json.dumps(data).encode("utf-8") if data is not None else None
    h = {"accept": "*/*"}
    if b:
        h["content-type"] = "application/json"
    if CSRF["v"]:
        h["X-CSRF"] = CSRF["v"]
    try:
        r = op.open(urllib.request.Request(BASE + path, data=b, headers=h, method=method), timeout=90)
        body = r.read()
        if binary:
            return r.status, body, dict(r.headers)
        t = body.decode("utf-8", "ignore")
        return r.status, (t if raw else json.loads(t or "{}")), dict(r.headers)
    except urllib.error.HTTPError as e:
        body = e.read()
        if binary:
            return e.code, body, dict(e.headers)
        t = body.decode("utf-8", "ignore")
        return e.code, (t if raw else (json.loads(t) if t.startswith("{") else t)), dict(e.headers)


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        fail += 1
        print("  FAIL  %s  %s" % (name, detail))


def skip(name, why):
    global skipped
    skipped += 1
    print("  SKIP  %s  (%s)" % (name, why))


def pdf_text(data):
    """Enough of a PDF reader to see what words ended up on the page: follow
    each font's ToUnicode CMap and decode the show-text operators. Boxes
    instead of Cyrillic would show up here as unmapped codes."""
    objs = {}
    for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj(.*?)endobj", data, re.S):
        objs[int(m.group(1))] = m.group(3)

    def stream(body):
        m = re.search(rb"stream\r?\n(.*?)endstream", body, re.S)
        if not m:
            return None
        raw = m.group(1)
        if b"/FlateDecode" not in body:
            return raw
        try:
            return zlib.decompressobj().decompress(raw)
        except zlib.error:
            return None

    cmaps = {}
    for num, body in objs.items():
        s = stream(body)
        if not s or b"beginbf" not in s:
            continue
        table = {}
        for blk in re.findall(rb"beginbfchar(.*?)endbfchar", s, re.S):
            for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                table[int(src, 16)] = bytes.fromhex(dst.decode()).decode("utf-16-be", "replace")
        for blk in re.findall(rb"beginbfrange(.*?)endbfrange", s, re.S):
            for lo, hi, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                start = int(dst, 16)
                for i, code in enumerate(range(int(lo, 16), int(hi, 16) + 1)):
                    table[code] = chr(start + i)
        if table:
            cmaps[num] = table

    font_cmap = {}
    for num, body in objs.items():
        m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
        if m and int(m.group(1)) in cmaps:
            font_cmap[num] = cmaps[int(m.group(1))]
    res = {}
    for body in objs.values():
        for name, num in re.findall(rb"/(F\d+|C\d+_\d+|TT\d+)\s+(\d+)\s+0\s+R", body):
            if int(num) in font_cmap:
                res[name.decode()] = font_cmap[int(num)]

    out, cur = [], None
    for num, body in objs.items():
        if b"/Font" in body or b"beginbf" in body:
            continue
        s = stream(body)
        if not s or (b"Tj" not in s and b"TJ" not in s):
            continue
        for tok in re.finditer(rb"/(F\d+|C\d+_\d+|TT\d+)\s+[\d.]+\s+Tf|<([0-9A-Fa-f]+)>\s*Tj|\[(.*?)\]\s*TJ", s, re.S):
            if tok.group(1):
                cur = res.get(tok.group(1).decode())
                continue
            hexes = [tok.group(2)] if tok.group(2) else re.findall(rb"<([0-9A-Fa-f]+)>", tok.group(3) or b"")
            for h in hexes:
                h = h.decode()
                for i in range(0, len(h), 4):
                    out.append((cur or {}).get(int(h[i:i + 4].ljust(4, "0"), 16), "�"))
    # Kerning splits words across show operators, so compare on a squeezed copy.
    return re.sub(r"\s+", "", "".join(out))


INV = ("?a=%D0%A1%D0%B8%D0%B9%D0%BC%20%D0%95%D0%9E%D0%9E%D0%94&b=Muster%20GmbH"
       "&rega=203456789&regb=HRB%2012345&vata=BG203456789&vatb=DE811234567"
       "&number=INV-20260808&desc=%D0%9A%D0%B0%D0%B1%D0%B5%D0%BB%D0%B8&amount=1240.50")

st, r, _ = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
CSRF["v"] = r.get("csrf", "")
check("login", st == 200)

st, ent, _ = call("GET", "/api/docs/entitlement")
paid = bool(ent.get("paid"))
if not paid and os.environ.get("SEAM_DB"):
    # The seeded org is on the free plan, where printing and PDF are locked -
    # so the interesting half of this suite would be skipped. Put it on a paid
    # plan by writing the row, not by calling the product's activation
    # endpoint: a test should not depend on how billing happens to work today.
    import sqlite3, datetime as _dt
    con = sqlite3.connect(os.environ["SEAM_DB"])
    con.execute("UPDATE orgs SET plan='pro', plan_until=? WHERE id IN "
                "(SELECT org_id FROM memberships WHERE user_id IN "
                "(SELECT id FROM users WHERE email='ops@seam.demo'))",
                ((_dt.date.today() + _dt.timedelta(days=30)).isoformat(),))
    con.commit()
    con.close()
    st, ent, _ = call("GET", "/api/docs/entitlement")
    paid = bool(ent.get("paid"))
print("     plan paid=%s" % paid)

# ---------------------------------------------------------------- the page --
st, page, _ = call("GET", "/docs/invoice" + INV + "&lang=de", raw=True)
check("invoice page renders", st == 200, str(st))
has_dl = 'id="pdfBtn"' in page and "/docs/invoice.pdf" in page
if paid:
    check("page offers the file, not only the printer", has_dl or "pbar" in page)
    check("the download is a plain link that works without script",
          ('<a class="dl"' in page) or ("/docs/invoice.pdf" not in page))
check("no inline script on the sheet", "<script>" not in page)

# ----------------------------------------------------------------- the file --
st, body, hdr = call("GET", "/docs/invoice.pdf" + INV + "&lang=de", binary=True)
if st == 503:
    skip("pdf export", "no renderer installed on this machine")
elif st == 402:
    skip("pdf export", "free plan: printing and PDF are paid")
else:
    check("pdf route answers 200", st == 200, str(st))
    check("it really is a PDF", body[:5] == b"%PDF-", repr(body[:8]))
    ct = (hdr.get("Content-Type") or "").lower()
    check("served as application/pdf", ct.startswith("application/pdf"), ct)
    cd = hdr.get("Content-Disposition") or ""
    check("delivered as an attachment, not a page", cd.lower().startswith("attachment"), cd)
    check("carries an ASCII file name a mail client will accept",
          re.search(r'filename="[A-Za-z0-9._-]+\.pdf"', cd) is not None, cd)
    check("and the real name in UTF-8 alongside it", "filename*=UTF-8''" in cd, cd)
    check("not cached by anything in the path",
          "no-store" in (hdr.get("Cache-Control") or ""), hdr.get("Cache-Control"))

    text = pdf_text(body)
    check("the buyer's name is in the file", "MusterGmbH" in text, text[:200])
    # The point of doing this with a browser rather than by hand: Cyrillic on a
    # German sheet has to come out as Cyrillic.
    check("Cyrillic survives into the PDF", "СиймЕООД" in text, text[:200])
    check("the document is in the language it was generated in",
          "Rechnung" in text and "Фактура" not in text, text[:200])
    check("no unmapped glyphs", "�" not in text, text[:200])
    # A print header would put the temp file path and a locale date on the
    # sheet - the second of which arrives in the operator's language, not the
    # document's.
    check("no browser header or footer printed onto it",
          "file:///" not in text and "seam-pdf-" not in text, text[:200])

    st2, body2, hdr2 = call("GET", "/docs/invoice.pdf" + INV + "&lang=bg", binary=True)
    if st2 == 200:
        t2 = pdf_text(body2)
        check("the same document in Bulgarian is Bulgarian",
              "Фактура" in t2 and "Rechnung" not in t2, t2[:200])

# --------------------------------------------------------------- the guards --
st, _b, _h = call("GET", "/docs/not_a_real_kind.pdf", binary=True)
check("unknown document is 404, same as without .pdf", st == 404, str(st))

anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
try:
    r = anon.open(BASE + "/docs/invoice.pdf" + INV, timeout=30)
    code = r.status
except urllib.error.HTTPError as e:
    code = e.code
check("a stranger cannot download somebody's invoice", code == 401, str(code))

# ---------------------------------------------- the invoice as a document --
# A description and one number is a note. An invoice has to say how much of
# what at what price, when the supply happened, by when it is due and where to
# pay - Bulgarian accounting law (ЗСч чл. 6) and the EU VAT directive
# (art. 226) ask for broadly the same list.
FULL = ("?a=%D0%A1%D0%B8%D0%B9%D0%BC&b=Muster%20GmbH&rega=203456789&vata=BG203456789"
        "&adra=%D0%A1%D0%BE%D1%84%D0%B8%D1%8F&adrb=Berlin&number=INV-77"
        "&date=2026-08-01&taxdate=2026-07-28&terms=14"
        "&iban=BG80BNBG96611020345678&bic=BNBGBGSF&bank=Test%20Bank&by=Maria%20L"
        "&desc=%D0%9A%D0%B0%D0%B1%D0%B5%D0%BB%D0%B8&qty=12.5&unit=%D0%BC&price=4.20"
        "&amount=52.5&vatrate=20&lang=bg")
st, page, _h = call("GET", "/docs/invoice" + FULL, raw=True)
check("the detailed invoice renders", st == 200, str(st))
for label, needle in (("quantity column", "Кол."), ("unit column", "Мярка"),
                      ("unit price column", "Ед. цена"),
                      ("date of supply", "2026-07-28"),
                      ("payment details", "BG80BNBG96611020345678"),
                      ("who drew it up", "Maria L"),
                      ("the buyer's address", "Berlin")):
    check("the invoice carries the %s" % label, needle in page, needle)
# 12.5 m at 4.20 is 52.50, VAT 10.50, gross 63.00. A rounding slip here is an
# accounting error, not a display one.
check("the line total is quantity times unit price", "52.50" in page, "")
check("VAT is worked out on it", "10.50" in page, "")
check("and the gross follows", "63.00" in page, "")
# Fourteen days from the first of August.
check("the due date follows the payment terms", "2026-08-15" in page, "")

# The older shape - description and a bare amount - still has to produce a
# document: links generated before this change are already out in the world.
st, old, _h = call("GET", "/docs/invoice" + INV + "&lang=bg", raw=True)
check("an invoice given only totals still works", st == 200 and "1 240.50" in old, str(st))
check("and does not invent empty columns", "Ед. цена" not in old, "")

st, js, _ = call("GET", "/static/js/docprint.js", raw=True)
check("docprint.js is served", st == 200, str(st))
# It is served with ?v=<build>, so its own changes have to move that number -
# otherwise a fix to it never reaches a browser that has the old one.
st, appjs, _ = call("GET", "/api/pricing")
st, idx, _ = call("GET", "/", raw=True)
import re as _re
_m = _re.search(r"app\.js\?v=(\d+)", idx)
check("the asset fingerprint is published", bool(_m), idx[:120])
if _m:
    import os as _os
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    _dp = _os.path.join(_root, "static", "js", "docprint.js")
    check("and docprint.js counts towards it",
          int(_os.path.getmtime(_dp)) <= int(_m.group(1)), "%s vs %s" % (
              int(_os.path.getmtime(_dp)), _m.group(1)))
check("it enhances the link rather than replacing it",
      "createObjectURL" in js and "preventDefault" in js)
check("the print fallback is still there", "window.print()" in js)

print("\n---- %d passed, %d failed, %d skipped ----" % (ok, fail, skipped))
sys.exit(1 if fail else 0)
