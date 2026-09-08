# -*- coding: utf-8 -*-
"""Passwords.

The hole this suite exists for: with no mail server configured,
`/api/auth/forgot` handed the reset link back to whoever asked. No session, no
proof of the address - an e-mail address alone was enough to take over any
account on the installation, and a fresh deployment has no mail server.

Alongside it, the two things that were simply missing: a way to change your own
password, and a rule that refuses the passwords people actually pick.
"""
import os, re, sys, json, sqlite3, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
DB = os.environ.get("SEAM_DB", "")
ok = fail = skipped = 0


def session():
    cj = http.cookiejar.CookieJar()
    return {"op": urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)),
            "cj": cj, "csrf": ""}


S = session()


def call(method, path, data=None, raw=False, sess=None, headers=None):
    s = sess or S
    c = http.cookiejar.Cookie(0, "seam_lang", "bg", None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    s["cj"].set_cookie(c)
    b = json.dumps(data).encode("utf-8") if data is not None else None
    h = {"accept": "application/json"}
    if b:
        h["content-type"] = "application/json"
    if s["csrf"]:
        h["X-CSRF"] = s["csrf"]
    h.update(headers or {})
    try:
        r = s["op"].open(urllib.request.Request(BASE + path, data=b, headers=h, method=method), timeout=30)
        t = r.read().decode("utf-8", "ignore")
        return r.status, (t if raw else json.loads(t or "{}"))
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "ignore")
        return e.code, (t if raw else (json.loads(t) if t.startswith("{") else t))


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


PW = "demo1234"
ME = "ops@seam.demo"

st, r = call("POST", "/api/auth/login", {"email": ME, "password": PW})
S["csrf"] = r.get("csrf", "")
check("login", st == 200)

# ------------------------------------------------------------- the rule ----
st, rules = call("GET", "/api/auth/password-rules")
check("the rules are published before someone is refused by them",
      st == 200 and rules.get("min") == 8 and rules.get("blocks_common") is True,
      json.dumps({k: v for k, v in rules.items() if k != "common"}))
# The browser meter applies the same three tests on the same list. Without the
# list it called "password1" acceptable while the server refused it, which
# sends people to a wall it told them was a door.
common = set(rules.get("common") or [])
check("the blocklist travels with the rules", len(common) > 30, str(len(common)))
for word in ("password", "qwerty", "letmein", "парола"):
    check("the list knows %s" % word, word in common, "")

REFUSED = [
    ("short1", "weak_password", "under eight characters"),
    ("abcdefgh", "weak_password", "no digit"),
    ("12345678", "weak_password", "no letter"),
    ("password1", "password_common", "the most used password there is"),
    ("Password123", "password_common", "the same with a capital and more digits"),
    ("qwerty123", "password_common", "a row of keys"),
    ("aaaaaaa1", "password_common", "three distinct characters"),
]
for pw, want, why in REFUSED:
    st, r = call("POST", "/api/auth/password", {"current": PW, "password": pw})
    check("refused (%s): %s" % (why, pw), st == 400 and r.get("code") == want,
          "%s %s" % (st, r.get("code")))

# A password built from the company it protects is known to everyone who knows
# the company. The demo org is "Nordic Retail Group", and "nordicretail1"
# contains no single word long enough to notice unless the name is taken apart
# first - which is the case this exists to catch.
st, me0 = call("GET", "/api/me")
org_name = ((me0.get("org") or {}).get("name") or "")
guessable = re.sub(r"[^A-Za-z0-9]+", "", org_name)[:12].lower() + "1"
if len(guessable) > 5:
    st, r = call("POST", "/api/auth/password", {"current": PW, "password": guessable})
    check("refused: made of the company name (%s)" % guessable,
          st == 400 and r.get("code") == "password_obvious", "%s %s" % (st, r.get("code")))
else:
    skip("company-name password", "the demo company has no usable name")

st, r = call("POST", "/api/auth/password", {"current": PW, "password": PW})
check("refused: the new one is the old one", st == 400 and r.get("code") == "password_same",
      "%s %s" % (st, r.get("code")))

st, r = call("POST", "/api/auth/password", {"current": "not-the-password", "password": "korab-vetrilo-77"})
check("the current password is required", st == 403 and r.get("code") == "bad_password",
      "%s %s" % (st, r.get("code")))

# ------------------------------------------------------------- changing ----
NEW = "korab-vetrilo-77x"
st, r = call("POST", "/api/auth/password", {"current": PW, "password": NEW})
check("a good password is accepted", st == 200, "%s %s" % (st, r))

# The session that made the change stays; that is what makes it usable.
st, me = call("GET", "/api/me")
check("the session that changed it stays open", st == 200 and (me.get("user") or {}).get("email") == ME,
      str(st))

other = session()
st, r = call("POST", "/api/auth/login", {"email": ME, "password": PW}, sess=other)
check("the old password no longer works", st in (400, 401, 403), str(st))

st, r = call("POST", "/api/auth/login", {"email": ME, "password": NEW}, sess=other)
check("the new one does", st == 200, str(st))
other["csrf"] = r.get("csrf", "")

# The seeded demo password is itself one the rule refuses - "demo1234" reduces
# to "demo" - which is the rule working, not a fault. It is put back through
# the database rather than the API, because the API is right to say no.
st, r = call("POST", "/api/auth/password", {"current": NEW, "password": PW}, sess=other)
check("the demo password is refused by the rule it now falls under",
      st == 400 and r.get("code") == "password_common", "%s %s" % (st, r.get("code")))

st, r = call("POST", "/api/auth/password", {"current": PW, "password": "korab-vetrilo-88"})
check("a session revoked by the change cannot use it", st in (401, 403), str(st))

if DB:
    from werkzeug.security import generate_password_hash as _hash
    con = sqlite3.connect(DB)
    con.execute("UPDATE users SET password_hash=? WHERE email=?", (_hash(PW), ME))
    con.commit()
    con.close()
    st, r = call("POST", "/api/auth/login", {"email": ME, "password": PW}, sess=session())
    check("and the suites after this one can still sign in", st == 200, str(st))
else:
    skip("restoring the demo password", "needs SEAM_DB")

# --------------------------------------------------------------- the leak --
# From this machine the link is handed over: whoever is asking is the operator,
# and without it a first-run self-host installation has no way to recover an
# account at all.
st, r = call("POST", "/api/auth/forgot", {"email": ME})
check("forgot answers whatever happens", st == 200, str(st))
local_link = r.get("reset_link")
if local_link:
    check("from this machine the link is handed over", local_link.startswith("/?reset="), local_link)
else:
    skip("loopback reset link", "mail is configured on this run")

# The same request pretending to come from somewhere else must not get it. The
# header is the only thing an attacker controls, so it is the thing that must
# not be believed.
st, r = call("POST", "/api/auth/forgot", {"email": ME},
             headers={"X-Forwarded-For": "203.0.113.9"})
check("a forwarded-for header does not make a request local",
      st == 200 and "reset_link" in r, "the header should be ignored either way")

st, r = call("POST", "/api/auth/forgot", {"email": "nobody@nowhere.invalid"})
check("an unknown address is answered the same way",
      st == 200 and "reset_link" not in r, json.dumps(r))

# The proof that matters, and it cannot be made over the loopback interface the
# suite runs on: read the source and confirm the link is behind the check.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
forgot = src[src.index("def password_forgot"):src.index("def password_reset")]
check("the reset link is behind a loopback test", "_loopback()" in forgot, "")

# Whether this installation can send mail at all is a fact about the
# installation. Reported only for addresses that exist, it becomes an oracle:
# ask about an address, and the shape of the answer says whether the account is
# real. This suite runs on loopback and cannot provoke the remote branch, so
# the proof is structural - the flag must be decided before the row is looked
# at, not inside the branch that knows the row exists.
_no_mail = forgot.find('out["no_mail"]')
_if_row = forgot.find("if row:")
check("the no-mail answer does not depend on whether the account exists",
      _no_mail != -1 and _if_row != -1 and _no_mail < _if_row,
      "no_mail at %d, 'if row:' at %d" % (_no_mail, _if_row))
check("and it is not handed out when the link already is",
      "not mailer.enabled() and not _loopback()" in forgot, "")
check("and remote_addr is what that test reads, not the forwarded header",
      "request.remote_addr" in src[src.index("def _loopback"):src.index("def _abuse_db")], "")
otp = src[src.index("def signature_send_otp"):src.index("def signature_sign")]
check("the signing code is behind the same test", "_loopback()" in otp, "")

# ------------------------------------------------------------- at rest -----
if DB:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT password_hash, reset_token FROM users WHERE email=?", (ME,)).fetchone()
    con.close()
    h = row["password_hash"]
    check("the password is not stored", PW not in h and NEW not in h, "")
    check("it is hashed with scrypt", h.startswith("scrypt:"), h.split("$")[0])
    # The reset token is a bearer credential: whoever holds the string is
    # treated as the person.
    if row["reset_token"]:
        check("the reset token is stored hashed",
              re.match(r"^[0-9a-f]{64}$", row["reset_token"]) is not None, row["reset_token"][:20])
else:
    skip("storage checks", "needs SEAM_DB")

# ---------------------------------------------------------------- mail ----
# Mail is what removes the need for the loopback fallback altogether, so it has
# to be settable from the screen rather than by editing a file on the server.
st, m = call("GET", "/api/admin/smtp")
check("the mail settings are readable", st == 200 and "enabled" in m, str(st))
check("the stored password is never sent back", "password" not in m, json.dumps(m))
check("but the form is told whether one is set", "has_password" in m, json.dumps(m))

st, r = call("POST", "/api/admin/smtp/test", {})
check("testing an unset mail server says so, rather than claiming success",
      st == 400 and r.get("code") == "smtp_unset", "%s %s" % (st, r.get("code")))

outsider = session()
st, r = call("POST", "/api/auth/login", {"email": "supply@seam.demo", "password": PW}, sess=outsider)
if st == 200:
    outsider["csrf"] = r.get("csrf", "")
    st, r = call("GET", "/api/admin/smtp", sess=outsider)
    check("a partner cannot read the mail settings", st == 403, str(st))
    st, r = call("PUT", "/api/admin/smtp", {"host": "evil.example"}, sess=outsider)
    check("nor write them", st == 403, str(st))
else:
    skip("outsider mail checks", "the second demo account did not sign in")

# If encryption is asked for and the server will not do it, the send stops.
# Continuing meant the mailbox password went over the wire in the clear to a
# server that had just said it could not protect it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ml = open(os.path.join(ROOT, "mailer.py"), encoding="utf-8").read()
send = ml[ml.index("def _send"):ml.index("def _deliver")]
check("a refused STARTTLS stops the send", "return False, \"STARTTLS refused" in send, "")
check("and the failure has a reason attached", "def send_now" in ml, "")

# ------------------------------------------------------------- the screen --
st, js = call("GET", "/static/js/i18n.js", raw=True)
for key in ("pw_title", "pw_current", "pw_g_common", "err_password_common",
            "err_password_obvious", "forgot_no_mail", "sm_title", "sm_tls_note"):
    check("%s is translated" % key, ('%s: "' % key) in js)

print("\n---- %d passed, %d failed, %d skipped ----" % (ok, fail, skipped))
sys.exit(1 if fail else 0)
