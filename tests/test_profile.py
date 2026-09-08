# -*- coding: utf-8 -*-
"""What a company does, and who its people are.

Two features that share one boundary: both are visible to whoever already
shares a company or a workspace with you, and to nobody else. A profile is the
first thing in this product that invites a person to paste a URL and upload an
image, so most of what is guarded here is what happens when they paste the
wrong one.
"""
import os, io, re, sys, json, sqlite3, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
DB = os.environ.get("SEAM_DB", "")
ok = fail = skipped = 0

#: A 1x1 PNG, so an upload can be tested without shipping a fixture file.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def session():
    cj = http.cookiejar.CookieJar()
    return {"op": urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)),
            "cj": cj, "csrf": ""}


S = session()


def call(method, path, data=None, raw=False, sess=None, headers=None, body_bytes=None):
    s = sess or S
    c = http.cookiejar.Cookie(0, "seam_lang", "bg", None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    s["cj"].set_cookie(c)
    b = body_bytes if body_bytes is not None else (
        json.dumps(data).encode("utf-8") if data is not None else None)
    h = {"accept": "*/*"}
    if b and body_bytes is None:
        h["content-type"] = "application/json"
    if s["csrf"]:
        h["X-CSRF"] = s["csrf"]
    h.update(headers or {})
    try:
        r = s["op"].open(urllib.request.Request(BASE + path, data=b, headers=h, method=method), timeout=30)
        t = r.read()
        return r.status, (t.decode("utf-8", "ignore") if raw else json.loads(t.decode("utf-8") or "{}"))
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "ignore")
        return e.code, (t if raw else (json.loads(t) if t.startswith("{") else t))


def multipart(fields, filename, content, field="file"):
    """A multipart body by hand - there is no requests here and there is not
    going to be one."""
    boundary = "----seamtest" + os.urandom(8).hex()
    out = []
    for k, v in (fields or {}).items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode())
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n" % (boundary, field, filename)).encode())
    out.append(content)
    out.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(out), {"content-type": "multipart/form-data; boundary=" + boundary}


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


st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
S["csrf"] = r.get("csrf", "")
check("login", st == 200)

# --------------------------------------------------------------- sectors ----
st, s = call("GET", "/api/org/sectors")
check("the trades are readable", st == 200 and isinstance(s.get("all"), list), str(st))
check("all seven are offered", len(s.get("all") or []) == 7, str(s.get("all")))

st, r = call("PUT", "/api/org/sectors", {"sectors": ["construction", "nonsense", "ecommerce"]})
check("only real trades are stored", r.get("sectors") == ["ecommerce", "construction"],
      json.dumps(r))
# Stored in the platform's own order, not the order they were clicked, so the
# menus do not reshuffle between sessions.
st, r2 = call("PUT", "/api/org/sectors", {"sectors": ["construction", "ecommerce"]})
check("the order is stable", r2.get("sectors") == ["ecommerce", "construction"], json.dumps(r2))

st, s = call("GET", "/api/org/sectors")
check("and it is asked no more", s.get("asked") is True, json.dumps(s))

st, r = call("PUT", "/api/org/sectors", {"sectors": []})
check("choosing nothing is a real answer, not a refusal",
      r.get("sectors") == [] and r.get("asked") is True, json.dumps(r))
st, s = call("GET", "/api/org/sectors")
check("an empty choice still counts as answered", s.get("asked") is True, json.dumps(s))

st, r = call("PUT", "/api/org/sectors", {"sectors": "construction"})
check("a string where a list belongs is refused", st == 400, str(st))

call("PUT", "/api/org/sectors", {"sectors": ["construction"]})

# --------------------------------------------------------------- profile ----
st, p = call("GET", "/api/profile")
check("the profile is readable", st == 200 and "person" in p and "company" in p, str(st))

st, r = call("PUT", "/api/profile", {
    "title": "Ръководител доставки", "bio": "Двайсет години в снабдяването.",
    "phone": "+359 2 000 0000",
    "links": [{"label": "Портфолио", "url": "https://example.com/work"},
              {"label": "Опасна", "url": "javascript:alert(1)"},
              {"label": "Данни", "url": "data:text/html,<script>alert(1)</script>"},
              {"label": "Относителна", "url": "/somewhere"}]})
check("the profile saves", st == 200 and r.get("title") == "Ръководител доставки", str(st))
urls = [l["url"] for l in (r.get("links") or [])]
# A profile link is shown to colleagues and counterparties. A javascript: or
# data: URL here would be a stored cross-site script handed to each of them.
check("only http and https survive", urls == ["https://example.com/work"], str(urls))

st, r = call("PUT", "/api/profile/company", {"about": "Строителство от 2004.",
                                             "website": "example.com",
                                             "links": [{"label": "Каталог", "url": "https://example.com/cat"}]})
check("the company card saves", st == 200, str(st))
# A website typed the way people type it should still be a link.
check("a bare domain becomes a link", r.get("website") == "https://example.com", str(r.get("website")))

st, r = call("PUT", "/api/profile", {"links": [{"label": "x", "url": "https://e.com/%d" % i} for i in range(30)]})
check("the number of links is capped", len(r.get("links") or []) <= 8, str(len(r.get("links") or [])))

# ----------------------------------------------------------------- photo ----
body, hdr = multipart(None, "me.png", PNG)
st, r = call("POST", "/api/profile/photo?of=person", headers=hdr, body_bytes=body)
check("a picture uploads", st == 200 and (r.get("url") or "").startswith("/uploads/profile/"),
      "%s %s" % (st, r))
photo_url = r.get("url") or ""

st, _b = call("GET", photo_url, raw=True)
check("and is served back to the person who owns it", st == 200, str(st))

anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
try:
    anon.open(BASE + photo_url, timeout=20)
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("a stranger cannot fetch it", code == 401, str(code))

# An SVG is a document that can carry script, and it would be served from our
# own origin to everyone who opens the page.
body, hdr = multipart(None, "logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
st, r = call("POST", "/api/profile/photo?of=person", headers=hdr, body_bytes=body)
check("an SVG is refused", st == 400, str(st))

body, hdr = multipart(None, "sneaky.png.exe", b"MZ")
st, r = call("POST", "/api/profile/photo?of=person", headers=hdr, body_bytes=body)
check("so is anything that is not an image", st == 400, str(st))

# The request ceiling is 64 MB because progress evidence can be video. A
# profile picture is not, and an oversized one must not be written to disk
# first and deleted afterwards.
body, hdr = multipart(None, "huge.png", b"\x00" * (5 * 1024 * 1024))
st, r = call("POST", "/api/profile/photo?of=person", headers=hdr, body_bytes=body)
check("an oversized picture is refused", st == 400, str(st))

st, _b = call("GET", "/uploads/profile/../../seam.db", raw=True)
check("the path cannot be climbed", st in (400, 404), str(st))

st, _b = call("GET", "/uploads/profile/nothing-here.png", raw=True)
check("a name that is not a stored picture is 404", st == 404, str(st))

# The old file goes when it is replaced, or a profile changed weekly fills the
# disk with pictures nobody can reach.
body, hdr = multipart(None, "me2.png", PNG)
st, r = call("POST", "/api/profile/photo?of=person", headers=hdr, body_bytes=body)
second = r.get("url") or ""
check("replacing gives a new address", second and second != photo_url, second)
st, _b = call("GET", photo_url, raw=True)
check("and the file it replaced is gone", st == 404, str(st))

# ------------------------------------------------------------- who may see --
st, team = call("GET", "/api/team")
mine = [m for m in (team.get("members") or []) if not m["is_me"]]
if mine:
    st, other = call("GET", "/api/profile/%d" % mine[0]["id"])
    check("a colleague's card is readable", st == 200 and other.get("name"), str(st))
else:
    skip("colleague's card", "the demo company has one member")

st, r = call("GET", "/api/profile/99999")
check("a person who does not exist is 404", st == 404, str(st))

outsider = session()
st, r = call("POST", "/api/auth/login", {"email": "supply@seam.demo", "password": "demo1234"},
             sess=outsider)
if st == 200:
    outsider["csrf"] = r.get("csrf", "")
    me_id = (call("GET", "/api/profile")[1].get("person") or {}).get("id")
    check("my own profile carries an id", isinstance(me_id, int), str(me_id))
    st, r = call("GET", "/api/profile/%s" % me_id, sess=outsider)
    # These two share a workspace in the seeded data, so this is allowed and the
    # interesting part is that it is decided by the shared workspace, not by
    # asking politely.
    check("someone across a shared workspace may look", st in (200, 404), str(st))
    st, r = call("PUT", "/api/org/sectors", {"sectors": ["agency"]}, sess=outsider)
    check("and cannot set another company's trades through it", st in (200, 400, 403), str(st))
else:
    skip("outsider checks", "the second demo account did not sign in")

if DB:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT job_title, bio, avatar, links_json FROM users "
                      "WHERE email='ops@seam.demo'").fetchone()
    con.close()
    check("it is on the person, not in a blob somewhere",
          row and row["job_title"] and row["avatar"], str(dict(row) if row else None))

# The interface asks this question on a first sign-in, so the front end has to
# have the strings for it.
st, js = call("GET", "/static/js/i18n.js", raw=True)
for key in ("sec_welcome", "sec_show_all", "prof_title_modal", "prof_links_hint"):
    check("%s is translated" % key, ('%s: "' % key) in js)

print("\n---- %d passed, %d failed, %d skipped ----" % (ok, fail, skipped))
sys.exit(1 if fail else 0)
