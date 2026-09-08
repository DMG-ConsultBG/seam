# -*- coding: utf-8 -*-
"""Mail, actually delivered.

Every other suite runs with no mail server, because that is a fresh
installation and the interesting behaviour there is what Seam does when it
cannot send. This one turns mail on against a local server that accepts
everything and delivers nothing, and follows the letters that matter: the
invoice, the reminder, the receipt, the invitation, the password reset.

It configures mail through the product's own screen and takes it away again at
the end, so the suites either side still see an installation with none.
"""
import os, re, sys, json, time, sqlite3, urllib.request, urllib.error, http.cookiejar
from datetime import datetime, timedelta

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
DB = os.environ.get("SEAM_DB", "")
SMTP_PORT = int(os.environ.get("SEAM_MOCK_SMTP", "5095"))
PEEK = "http://127.0.0.1:%d" % (SMTP_PORT - 1)
ok = fail = skipped = 0


def session():
    cj = http.cookiejar.CookieJar()
    return {"op": urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)),
            "cj": cj, "csrf": ""}


S = session()


def call(method, path, data=None, raw=False, sess=None):
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
    try:
        r = s["op"].open(urllib.request.Request(BASE + path, data=b, headers=h, method=method), timeout=40)
        t = r.read().decode("utf-8", "ignore")
        return r.status, (t if raw else json.loads(t or "{}"))
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "ignore")
        return e.code, (t if raw else (json.loads(t) if t.startswith("{") else t))


def inbox():
    try:
        return json.loads(urllib.request.urlopen(PEEK + "/messages", timeout=5).read().decode("utf-8"))
    except Exception:                                       # noqa: BLE001
        return None


def clear():
    try:
        urllib.request.urlopen(urllib.request.Request(PEEK + "/reset", data=b"", method="POST"),
                               timeout=5).read()
    except Exception:                                       # noqa: BLE001
        pass


def wait_for(pred, seconds=6):
    """Delivery is fire-and-forget in a thread for most letters."""
    end = time.time() + seconds
    while time.time() < end:
        box = inbox()
        if box and pred(box):
            return box
        time.sleep(0.25)
    return inbox()


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


if inbox() is None:
    print("  SKIP  the whole suite (the mock mail server is not listening on %d)" % SMTP_PORT)
    sys.exit(0)

st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
S["csrf"] = r.get("csrf", "")
check("login", st == 200)

# ------------------------------------------------------------- setting up --
st, before = call("GET", "/api/admin/smtp")
check("mail starts switched off", before.get("enabled") is False, json.dumps(before))
st, r = call("POST", "/api/admin/smtp/test", {})
check("testing it says so rather than claiming success",
      st == 400 and r.get("code") == "smtp_unset", "%s %s" % (st, r.get("code")))

# The mock refuses STARTTLS on purpose, so encryption is off here. On a real
# provider it stays on, and mailer aborts rather than sending the password in
# the clear if the server will not do it - proved further down.
st, saved = call("PUT", "/api/admin/smtp", {
    "host": "127.0.0.1", "port": SMTP_PORT, "user": "seam", "password": "secret",
    "sender": "seam@test.invalid", "tls": False})
check("mail can be set up from the screen", st == 200 and saved.get("enabled") is True,
      "%s %s" % (st, json.dumps(saved)))
check("the password is never handed back", "password" not in saved, json.dumps(saved))
check("but the form knows one is set", saved.get("has_password") is True, json.dumps(saved))

clear()
st, r = call("POST", "/api/admin/smtp/test", {})
check("the test message is sent", st == 200 and r.get("sent") is True, "%s %s" % (st, r))
box = wait_for(lambda b: b["count"] >= 1)
check("and it arrives", box["count"] >= 1, str(box["count"]))
if box["count"]:
    m = box["messages"][-1]
    check("from the address that was configured", m["envelope_from"] == "seam@test.invalid",
          m["envelope_from"])
    check("having authenticated", m["authenticated"] is True, str(m["authenticated"]))
    check("in the reader's language", "работи" in m["subject"] or "работи" in m["body"],
          m["subject"])

# --------------------------------------------------------- the money letters --
st, ps = call("PUT", "/api/pay-settings", {"iban": "BG18RZBB91550123456789",
                                           "terms": "14", "reminders": [1]})
TODAY = datetime.utcnow().date()
st, inv = call("POST", "/api/invoices", {
    "number": "INV-MAIL-1", "customer": "Muster GmbH", "customer_email": "kunde@example.com",
    "lines": [{"desc": "Кабел", "qty": "10", "unit": "м", "price": "4.20"}],
    "vat_rate": 20, "lang": "de", "issue_date": TODAY.strftime("%Y-%m-%d")})
check("an invoice is recorded", st == 200 and inv.get("id"), "%s %s" % (st, str(inv)[:160]))

clear()
st, r = call("POST", "/api/invoices/%d/send" % inv["id"], {})
check("the invoice is sent", st == 200 and r.get("status") == "sent", "%s %s" % (st, str(r)[:160]))
box = wait_for(lambda b: b["count"] >= 1)
check("the customer receives it", box["count"] >= 1, str(box["count"]))
if box["count"]:
    m = box["messages"][-1]
    check("addressed to the customer", "kunde@example.com" in m["envelope_to"], str(m["envelope_to"]))
    # The invoice was written in German, so the letter about it is German. The
    # sender's own interface language has nothing to do with it.
    check("written in the language of the invoice, not of the sender",
          "Rechnung" in m["subject"], m["subject"])
    check("it carries the invoice number", inv["number"] in m["subject"] or inv["number"] in m["body"],
          m["subject"])
    check("and says where to pay", "BG18RZBB91550123456789" in m["body"], m["body"][:200])
    check("with the reference the statement is matched on",
          inv["pay_ref"] in m["body"], inv["pay_ref"])

# ------------------------------------------------------------- the chasing --
if DB:
    con = sqlite3.connect(DB)
    con.execute("UPDATE invoices SET due_date=? WHERE id=?",
                ((TODAY - timedelta(days=1)).strftime("%Y-%m-%d"), inv["id"]))
    con.commit(); con.close()
    clear()
    st, r = call("POST", "/api/invoices/remind")
    check("the reminder pass sends", st == 200 and r.get("sent") >= 1, "%s %s" % (st, r))
    box = wait_for(lambda b: b["count"] >= 1)
    check("the overdue notice arrives", box["count"] >= 1, str(box["count"]))
    if box["count"]:
        m = box["messages"][-1]
        check("and says how late it is", re.search(r"\b\d+\b", m["subject"]) is not None, m["subject"])
        # Firm about the fact, never accusing: the letter states what is owed
        # and stops. Threats belong to lawyers.
        low = (m["subject"] + " " + m["body"]).lower()
        for word in ("court", "legal action", "съд", "адвокат", "inkasso"):
            check("it does not threaten (%s)" % word, word not in low, word)

    clear()
    st, r = call("POST", "/api/invoices/remind")
    box = inbox()
    check("running the pass again the same day sends nothing", box["count"] == 0, str(box["count"]))
else:
    skip("reminder letters", "needs SEAM_DB")

# ------------------------------------------------------------- the receipt --
clear()
st, r = call("POST", "/api/invoices/%d/paid" % inv["id"], {})
check("the invoice is settled", st == 200 and r.get("status") == "paid", str(r.get("status")))
box = wait_for(lambda b: b["count"] >= 1)
check("the customer is told the money arrived", box["count"] >= 1, str(box["count"]))
if box["count"]:
    m = box["messages"][-1]
    check("and it reads as a receipt, not another demand",
          "erhalten" in m["body"].lower() or "danke" in m["body"].lower(), m["body"][:160])

# ---------------------------------------------------- the password reset ----
# With mail on, the link goes to the mailbox and is NOT handed back over the
# API - that fallback exists only for a machine with nowhere to send.
clear()
st, r = call("POST", "/api/auth/forgot", {"email": "ops@seam.demo"})
check("the reset is accepted", st == 200, str(st))
check("the link is not returned over the API once mail works",
      "reset_link" not in r, json.dumps(r))
box = wait_for(lambda b: b["count"] >= 1)
check("it goes to the mailbox instead", box["count"] >= 1, str(box["count"]))
if box["count"]:
    m = box["messages"][-1]
    check("addressed to the account that asked", "ops@seam.demo" in m["envelope_to"],
          str(m["envelope_to"]))
    check("carrying a single-use link", "/?reset=" in m["body"], m["body"][:200])

# ------------------------------------------------------------- encryption --
# Asked for and refused by this server: the send must stop rather than put the
# mailbox password on the wire in the clear.
st, r = call("PUT", "/api/admin/smtp", {
    "host": "127.0.0.1", "port": SMTP_PORT, "user": "seam", "password": "secret",
    "sender": "seam@test.invalid", "tls": True})
clear()
st, r = call("POST", "/api/admin/smtp/test", {})
check("a refused STARTTLS stops the send", st == 502 and r.get("code") == "smtp_failed",
      "%s %s" % (st, r.get("code")))
check("and nothing was delivered", (inbox() or {}).get("count") == 0, str(inbox()))
check("the reason is reported, not swallowed", "STARTTLS" in json.dumps(r), json.dumps(r)[:160])

# ------------------------------------------------------- the two encryptions --
# STARTTLS opens in the clear and upgrades; SSL is encrypted from the first
# byte. abv.bg and Mail.bg speak only the second, on 465. Connecting there with
# the STARTTLS flow does not fail cleanly - it pushes a plaintext greeting into
# a TLS socket and hangs until the timeout, which reads like a network fault
# rather than a wrong setting.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mailer as _mailer                                    # noqa: E402

for cfg, want, why in (
        ({"port": 465}, True, "465 means encrypted from the first byte"),
        ({"port": 587}, False, "587 means open, then upgrade"),
        ({"port": 465, "ssl": False}, False, "an explicit setting beats the port"),
        ({"port": 587, "ssl": True}, True, "and the other way round"),
        ({"port": "nonsense"}, False, "a bad port falls back rather than crashing")):
    check("%s" % why, _mailer.implicit_tls(cfg) is want, str(cfg))

# The screen has to be able to say it, and the server has to store it.
st, r = call("PUT", "/api/admin/smtp", {
    "host": "smtp.abv.bg", "port": 465, "user": "x@abv.bg", "password": "app-password",
    "sender": "x@abv.bg", "tls": False, "ssl": True})
check("an SSL-only mailbox can be configured", st == 200 and r.get("ssl") is True,
      "%s %s" % (st, json.dumps(r)))
st, back = call("GET", "/api/admin/smtp")
check("and it is still SSL when read back", back.get("ssl") is True, json.dumps(back))
check("on the port that means it", back.get("port") == 465, str(back.get("port")))

# Left unsaid, the port decides - somebody who typed 465 meant SSL.
st, r = call("PUT", "/api/admin/smtp", {"host": "smtp.abv.bg", "port": 465,
                                        "user": "x@abv.bg", "sender": "x@abv.bg"})
check("with nothing said, the port decides", r.get("ssl") is True, json.dumps(r))

# The presets the screen offers must agree with all of that.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
appjs = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
presets = appjs[appjs.index("const SMTP_PRESETS = ["):]
presets = presets[:presets.index("\n];")]
for name, host in (("ABV.bg", "smtp.abv.bg"), ("Mail.bg", "smtp.mail.bg"),
                   ("iCloud Mail (Apple)", "smtp.mail.me.com")):
    check("%s is offered" % name, host in presets, "")
check("the Bulgarian mailboxes are set to SSL on 465",
      'host: "smtp.abv.bg", port: 465, ssl: true' in presets, "")
check("and iCloud to STARTTLS on 587",
      'host: "smtp.mail.me.com", port: 587,' in presets and "tls: true" in presets, "")
i18n = open(os.path.join(ROOT, "static", "js", "i18n.js"), encoding="utf-8").read()
for key in ("sm_p_abv", "sm_p_icloud", "sm_enc_ssl", "sm_enc_starttls", "sm_from_warn"):
    check("%s is translated" % key, i18n.count('%s: "' % key) == 13,
          str(i18n.count('%s: "' % key)))

# The presets here and the ones the screen offers are two lists of the same
# thing, and a probe that checks hosts nobody is offered proves nothing.
probe = open(os.path.join(ROOT, "tests", "smtp_probe.py"), encoding="utf-8").read()
for host in re.findall(r'host: "([^"]+)"', presets):
    if host == "mail.example.com":
        continue                     # the cPanel row is a placeholder to edit
    check("the probe covers %s" % host, '"%s"' % host in probe, "")

# ------------------------------------------------------------- who may set --
outsider = session()
st, r = call("POST", "/api/auth/login", {"email": "supply@seam.demo", "password": "demo1234"},
             sess=outsider)
if st == 200:
    outsider["csrf"] = r.get("csrf", "")
    st, r = call("PUT", "/api/admin/smtp", {"host": "evil.example"}, sess=outsider)
    check("another company cannot repoint the mail server", st == 403, str(st))
else:
    skip("outsider check", "the second demo account did not sign in")

# ---------------------------------------------------------------- clean up --
st, r = call("PUT", "/api/admin/smtp", {"host": ""})
check("mail can be switched off again", st == 200 and r.get("enabled") is False, str(r))
st, after = call("GET", "/api/admin/smtp")
check("and the installation is back as it was", after.get("enabled") is False, json.dumps(after))
clear()

print("\n---- %d passed, %d failed, %d skipped ----" % (ok, fail, skipped))
sys.exit(1 if fail else 0)
