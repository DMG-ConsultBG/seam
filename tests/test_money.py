# -*- coding: utf-8 -*-
"""Getting paid, end to end.

An invoice used to be a page that was generated and forgotten. Now it is a
record, and this suite follows one all the way: recorded, sent, chased when it
falls due, settled from a bank statement, receipted - and then counted towards
what this company can prove about how fast it pays.

That last part is the reason any of it is worth building. Every accounting
package prints invoices and emails reminders. None can tell a stranger how fast
you settle, because none of them holds both sides of the trade. Here the same
row is the seller's receivable and the buyer's obligation, so the days between
issuing and paying are recorded rather than claimed.
"""
import os, re, sys, json, sqlite3, urllib.request, urllib.error, http.cookiejar
from datetime import datetime, timedelta

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
DB = os.environ.get("SEAM_DB", "")
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

# ------------------------------------------------------- where I get paid --
st, r = call("PUT", "/api/pay-settings", {"iban": "BG18RZBB91550123456789", "bic": "RZBBBGSF",
                                          "bank": "Test Bank", "terms": "14",
                                          "reminders": [-3, 1, 7]})
check("payment details save", st == 200 and r.get("iban") == "BG18RZBB91550123456789",
      "%s %s" % (st, r))
check("the reminder plan is kept in order", r.get("reminders") == [-3, 1, 7], str(r.get("reminders")))

# A mistyped IBAN is caught here rather than by the customer's bank a week on.
st, r = call("PUT", "/api/pay-settings", {"iban": "BG18RZBB91550123456780"})
check("a bad IBAN is refused", st == 400 and r.get("code") == "bad_iban", "%s %s" % (st, r.get("code")))
call("PUT", "/api/pay-settings", {"iban": "BG18RZBB91550123456789", "terms": "14",
                                  "reminders": [-3, 1, 7]})

# ------------------------------------------------------------ the record ---
TODAY = datetime.utcnow().date()
LINES = [{"desc": "Кабел NYM 3x2.5", "qty": "250", "unit": "м", "price": "4.20"},
         {"desc": "Монтаж", "qty": "16", "unit": "ч", "price": "35"}]
st, inv = call("POST", "/api/invoices", {
    "number": "INV-TEST-1", "customer": "Muster GmbH", "customer_email": "kunde@example.com",
    "customer_reg": "HRB 12345", "lines": LINES, "vat_rate": 20, "lang": "de",
    "issue_date": TODAY.strftime("%Y-%m-%d")})
check("the invoice is recorded", st == 200 and inv.get("id"), "%s %s" % (st, inv))
# 250 x 4.20 = 1050, 16 x 35 = 560, net 1610, VAT 322, gross 1932.
check("the totals are worked out once, on the server",
      inv.get("net") == 1610.0 and inv.get("vat_amount") == 322.0 and inv.get("gross") == 1932.0,
      json.dumps(inv))
check("the due date follows the company's terms",
      inv.get("due_date") == (TODAY + timedelta(days=14)).strftime("%Y-%m-%d"), str(inv.get("due_date")))
check("it starts as a draft", inv.get("status") == "draft", str(inv.get("status")))
check("and carries a payment reference", len(inv.get("pay_ref") or "") >= 6, str(inv.get("pay_ref")))
IID, REF = inv["id"], inv["pay_ref"]

st, r = call("POST", "/api/invoices", {"customer": "X", "lines": [], "vat_rate": 20})
check("a zero invoice is not recorded", st == 400 and r.get("code") == "invoice_empty",
      "%s %s" % (st, r.get("code")))

# Two invoices must never share a reference, or a payment settles the wrong one.
st, inv2 = call("POST", "/api/invoices", {
    "number": "INV-TEST-1", "customer": "Muster GmbH", "lines": LINES, "vat_rate": 20})
check("two invoices with the same number get different references",
      inv2.get("pay_ref") and inv2["pay_ref"] != REF, "%s vs %s" % (inv2.get("pay_ref"), REF))

# --------------------------------------------------------------- sending ---
st, r = call("POST", "/api/invoices/%d/send" % IID, {})
# No mail server on a test run: it says so rather than claiming to have sent.
check("sending without a mail server says so", st == 400 and r.get("code") == "smtp_unset",
      "%s %s" % (st, r.get("code")))
st, lst = call("GET", "/api/invoices")
mine = [i for i in lst["issued"] if i["id"] == IID][0]
check("but the invoice is out of draft, so it will still be chased",
      mine["status"] == "sent", mine["status"])

# ------------------------------------------------------------- reminders ---
if DB:
    con = sqlite3.connect(DB)
    # Fall due yesterday, so the first chase past the due date is owing.
    con.execute("UPDATE invoices SET due_date=? WHERE id=?",
                ((TODAY - timedelta(days=1)).strftime("%Y-%m-%d"), IID))
    con.commit(); con.close()

    st, r = call("POST", "/api/invoices/remind")
    check("the reminder pass runs", st == 200, "%s %s" % (st, r))
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    row = con.execute("SELECT reminders, last_reminder FROM invoices WHERE id=?", (IID,)).fetchone()
    con.close()
    check("an overdue invoice is chased once", row["reminders"] == 1, str(dict(row)))

    st, r = call("POST", "/api/invoices/remind")
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    row2 = con.execute("SELECT reminders FROM invoices WHERE id=?", (IID,)).fetchone()
    con.close()
    # Running the pass twice in a day must send nothing twice: a customer who
    # gets four identical letters stops reading any of them.
    check("running the pass again the same day chases nothing",
          row2["reminders"] == 1, str(row2["reminders"]))
else:
    skip("reminder pass", "needs SEAM_DB")

# ----------------------------------------------------- the bank statement --
# A minimal CAMT.053 carrying one credit that quotes the reference.
CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Ntry><Amt Ccy="EUR">1932.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>%s</Dt></BookgDt>
<NtryDtls><TxDtls><RmtInf><Ustrd>Plateno po faktura %s</Ustrd></RmtInf>
<RltdPties><Dbtr><Nm>Muster GmbH</Nm></Dbtr></RltdPties></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>""" % (TODAY.strftime("%Y-%m-%d"), REF)

st, prev = call("POST", "/api/invoices/reconcile", {"content": CAMT})
check("the statement is read", st == 200 and prev.get("transactions") == 1,
      "%s %s" % (st, json.dumps(prev)[:200]))
m = (prev.get("matches") or [{}])[0]
check("the payment is matched on the reference",
      m.get("invoice_id") == IID and m.get("reason") == "reference", json.dumps(m)[:200])
check("and it settles the invoice in full", m.get("settles") == "full", str(m.get("settles")))
# Preview changes nothing: a machine that moves money on a guess is worse than
# one that asks.
check("a preview applies nothing", prev.get("applied") == 0, str(prev.get("applied")))
st, lst = call("GET", "/api/invoices")
check("the invoice is still open after a preview",
      [i for i in lst["issued"] if i["id"] == IID][0]["status"] == "sent", "")

st, done = call("POST", "/api/invoices/reconcile", {"content": CAMT, "commit": True})
check("committing settles it", done.get("applied") == 1, json.dumps(done)[:200])
st, lst = call("GET", "/api/invoices")
paid = [i for i in lst["issued"] if i["id"] == IID][0]
check("the invoice is paid", paid["status"] == "paid", paid["status"])
check("nothing is left outstanding", paid["outstanding"] == 0, str(paid["outstanding"]))

st, ev = call("GET", "/api/invoices/%d/events" % IID)
kinds = [e["kind"] for e in ev.get("events") or []]
check("the payment is on the record", "paid" in kinds, str(kinds))

# An ambiguous amount is never applied: two customers owing the same figure
# must not be settled against each other by a machine.
st, a1 = call("POST", "/api/invoices", {"number": "AMB-1", "customer": "A",
                                        "lines": [{"amount": "500"}], "vat_rate": 0})
st, a2 = call("POST", "/api/invoices", {"number": "AMB-2", "customer": "B",
                                        "lines": [{"amount": "500"}], "vat_rate": 0})
if DB:
    con = sqlite3.connect(DB)
    con.execute("UPDATE invoices SET status='sent' WHERE id IN (?,?)", (a1["id"], a2["id"]))
    con.commit(); con.close()
AMB = CAMT.replace("1932.00", "500.00").replace("Plateno po faktura %s" % REF, "transfer")
st, r = call("POST", "/api/invoices/reconcile", {"content": AMB, "commit": True})
hit = [m for m in r.get("matches") or []]
check("two invoices for the same amount are left alone",
      hit and hit[0]["invoice_id"] is None and hit[0]["reason"] == "ambiguous",
      json.dumps(hit)[:200])
check("and nothing was applied", r.get("applied") == 0, str(r.get("applied")))

# ------------------------------------------------- what it can be proved of --
# The part no accounting package can do: the counterparty issued this, we paid
# it, and the days between are a record rather than a claim.
if DB:
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    me = con.execute("SELECT m.org_id AS id FROM memberships m JOIN users u ON u.id=m.user_id "
                     "WHERE u.email='ops@seam.demo'").fetchone()["id"]
    other = con.execute("SELECT id FROM orgs WHERE id<>? ORDER BY id LIMIT 1", (me,)).fetchone()["id"]
    for n in range(3):
        issued = (TODAY - timedelta(days=40 - n)).strftime("%Y-%m-%d")
        due = (TODAY - timedelta(days=26 - n)).strftime("%Y-%m-%d")
        paid_at = (TODAY - timedelta(days=30 - n)).strftime("%Y-%m-%d")
        con.execute(
            "INSERT INTO invoices (org_id, customer_org_id, number, pay_ref, customer, "
            "lang, currency, net, vat_rate, vat_amount, gross, issue_date, due_date, "
            "status, paid_at, paid_amount) "
            "VALUES (?,?,?,?,'Nordic','bg','EUR',100,0,0,100,?,?,'paid',?,100)",
            (other, me, "SUP-%d" % n, "SUPREF%d" % n, issued, due, paid_at))
    con.commit(); con.close()

    st, ref = call("GET", "/api/reference")
    facts = ref.get("preview") or {}
    check("the reference knows how fast we pay",
          facts.get("pays_in_days") is not None, json.dumps(facts)[:260])
    check("and how often we pay on time",
          facts.get("pays_on_time") is not None, json.dumps(facts)[:260])
    # It names nobody. Publishing which supplier was paid late is publishing
    # their business, not ours.
    blob = json.dumps(facts, ensure_ascii=False)
    check("without naming a single counterparty", "SUP-" not in blob and "SUPREF" not in blob, blob[:200])
    check("it is off by default, because a slow payer should decide for itself",
          (ref.get("fields") or {}).get("pays_in_days") is False, json.dumps(ref.get("fields")))
else:
    skip("payment record on the reference", "needs SEAM_DB")

# ------------------------------------------------------------- who may see --
outsider = session()
st, r = call("POST", "/api/auth/login", {"email": "supply@seam.demo", "password": "demo1234"},
             sess=outsider)
if st == 200:
    outsider["csrf"] = r.get("csrf", "")
    st, lst2 = call("GET", "/api/invoices", sess=outsider)
    ids = [i["id"] for i in (lst2.get("issued") or [])]
    check("another company does not see our receivables", IID not in ids, str(ids[:6]))
    st, r = call("POST", "/api/invoices/%d/paid" % IID, {"amount": 1}, sess=outsider)
    check("nor can it mark our invoice paid", st == 404, str(st))
else:
    skip("outsider checks", "the second demo account did not sign in")

# --------------------------------------------------------------- letters ---
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
block = src[src.index("MONEY_MAIL = {"):src.index("\n}\n", src.index("MONEY_MAIL = {"))]
for lang in ("en", "bg", "de", "ro", "el", "tr", "it", "ru", "es", "fr", "pl", "uk", "pt"):
    check("the money letters exist in %s" % lang, ('"%s": {' % lang) in block, "")
for key in ("sent_s", "rem_t", "late_t", "paid_t", "ref_note"):
    check("every letter has %s" % key, block.count('"%s"' % key) == 13,
          str(block.count('"%s"' % key)))

print("\n---- %d passed, %d failed, %d skipped ----" % (ok, fail, skipped))
sys.exit(1 if fail else 0)
