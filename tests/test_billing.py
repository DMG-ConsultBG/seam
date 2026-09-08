# -*- coding: utf-8 -*-
"""Money.

`/api/plans/confirm` used to set the plan to Pro for whoever asked. Any account
could hand its own company a paid plan with one request, and the Merchant of
Record path could take a payment that activated nothing, because the webhook
only ever looked at templates.

What is guarded here is the rule the fix is built on: a plan becomes paid for
three reasons only - a signed webhook, an answer from the processor about a
session this server itself opened, or an administrator of this installation
approving a transfer. Never because the buyer said so.
"""
import os, re, sys, json, time, hmac, hashlib, sqlite3
import urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
DB = os.environ.get("SEAM_DB", "")
ok = fail = skipped = 0


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


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def plan_of(email="ops@seam.demo"):
    con = db()
    try:
        r = con.execute("SELECT o.plan, o.plan_until FROM orgs o JOIN memberships m ON m.org_id=o.id "
                        "JOIN users u ON u.id=m.user_id WHERE u.email=?", (email,)).fetchone()
        return (r["plan"], r["plan_until"]) if r else (None, None)
    finally:
        con.close()


def org_of(email="ops@seam.demo"):
    con = db()
    try:
        r = con.execute("SELECT o.id FROM orgs o JOIN memberships m ON m.org_id=o.id "
                        "JOIN users u ON u.id=m.user_id WHERE u.email=?", (email,)).fetchone()
        return r["id"] if r else 0
    finally:
        con.close()


def set_plan(email, plan, until=None):
    con = db()
    try:
        con.execute("UPDATE orgs SET plan=?, plan_until=? WHERE id IN "
                    "(SELECT org_id FROM memberships WHERE user_id IN "
                    "(SELECT id FROM users WHERE email=?))", (plan, until, email))
        con.commit()
    finally:
        con.close()


if not DB:
    print("  SKIP  the whole suite (needs SEAM_DB; run through run_tests.py)")
    sys.exit(0)

st, r = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
S["csrf"] = r.get("csrf", "")
check("login", st == 200)

# ------------------------------------------------- the hole that was there --
set_plan("ops@seam.demo", "starter", None)
st, r = call("POST", "/api/plans/confirm", {})
plan, until = plan_of()
check("asking to confirm does not grant a paid plan", plan != "pro",
      "plan is now %r" % plan)
check("it is recorded as a claim instead", r.get("status") == "awaiting_review", json.dumps(r))
check("and the company is told it is pending", plan == "pro_pending", str(plan))

st, r2 = call("POST", "/api/plans/confirm", {})
check("pressing it twice does not stack up claims", r2.get("status") == "awaiting_review")
con = db()
n = con.execute("SELECT COUNT(*) c FROM billing_claims WHERE status='pending' AND kind='plan'").fetchone()["c"]
con.close()
check("exactly one claim waiting", n == 1, str(n))

st, p = call("GET", "/api/pricing")
check("the plans screen knows a claim is waiting", p.get("claim_pending") is True, json.dumps(p))
check("and does not report the company as paid", p.get("paid") is False, json.dumps(p))

# A forged session id is not a payment either.
st, r = call("POST", "/api/plans/confirm", {"session_id": "cs_test_forged_12345"})
check("a made-up checkout id changes nothing",
      plan_of()[0] != "pro", "plan is %r" % plan_of()[0])

# ------------------------------------------------------- the operator's say --
st, d = call("GET", "/api/admin/billing")
check("the operator sees the claim", st == 200 and len(d.get("claims") or []) >= 1, str(st))
claim_id = (d.get("claims") or [{}])[0].get("id")
check("it names the company that asked", bool((d["claims"][0] or {}).get("org")),
      json.dumps(d["claims"][0]))

st, r = call("POST", "/api/admin/billing/%d/approve" % claim_id, {"note": "IBAN, 08.08"})
check("approving works", st == 200 and r.get("status") == "approved", str(st))
plan, until = plan_of()
check("and only then is the plan paid", plan == "pro" and bool(until), "%r %r" % (plan, until))
check("with a real end date", bool(re.match(r"^\d{4}-\d{2}-\d{2}$", until or "")), str(until))

st, r = call("POST", "/api/admin/billing/%d/approve" % claim_id, {})
check("a claim cannot be approved twice", st == 400, str(st))

st, e = call("GET", "/api/docs/entitlement")
check("paid unlocks the document library", e.get("paid") is True, json.dumps(e))

# ------------------------------------------------------------- expiry --------
set_plan("ops@seam.demo", "pro", "2020-01-01")
st, e = call("GET", "/api/docs/entitlement")
check("a plan that ran out stops being paid", e.get("paid") is False, json.dumps(e))
st, p = call("GET", "/api/pricing")
check("and the plans screen agrees", p.get("paid") is False, json.dumps(p))

set_plan("ops@seam.demo", "pro", (time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400 * 20))))
st, e = call("GET", "/api/docs/entitlement")
check("a plan still running is paid", e.get("paid") is True, json.dumps(e))

# ---------------------------------------------------------- who may decide --
other = session()
st, r = call("POST", "/api/auth/login", {"email": "supply@seam.demo", "password": "demo1234"}, sess=other)
if st == 200:
    other["csrf"] = r.get("csrf", "")
    st, r = call("GET", "/api/admin/billing", sess=other)
    check("a partner cannot see other companies' payments", st == 403, str(st))
    st, r = call("POST", "/api/admin/billing/%d/approve" % claim_id, {}, sess=other)
    check("nor approve one", st in (400, 403), str(st))
else:
    skip("partner cannot decide payments", "second demo account did not sign in")

# ------------------------------------------------------------- webhooks -----
st, r = call("POST", "/api/webhooks/stripe", {"type": "checkout.session.completed"})
check("the Stripe hook is closed when nothing is configured", st in (401, 404), str(st))

secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
if not secret:
    skip("signed Stripe webhook", "STRIPE_WEBHOOK_SECRET not set for this run")
else:
    oid = org_of()
    set_plan("ops@seam.demo", "starter", None)

    def signed(payload, ts=None, secret_used=None):
        raw = json.dumps(payload).encode("utf-8")
        ts = str(int(ts if ts is not None else time.time()))
        mac = hmac.new((secret_used or secret).encode(), (ts + ".").encode() + raw,
                       hashlib.sha256).hexdigest()
        return raw, {"Stripe-Signature": "t=%s,v1=%s" % (ts, mac),
                     "content-type": "application/json"}

    ev = {"id": "evt_test_1", "type": "checkout.session.completed",
          "data": {"object": {"id": "cs_test_1", "payment_status": "paid",
                              "metadata": {"org": str(oid)}}}}
    raw, hdr = signed(ev)
    st, r = call("POST", "/api/webhooks/stripe", raw=False, headers=hdr, body_bytes=raw)
    check("a correctly signed webhook activates the plan",
          st == 200 and r.get("result") == "activated", "%s %s" % (st, r))
    check("the plan really moved", plan_of()[0] == "pro", str(plan_of()))

    first_until = plan_of()[1]
    st, r = call("POST", "/api/webhooks/stripe", raw=False, headers=hdr, body_bytes=raw)
    check("the same event delivered twice grants nothing extra",
          r.get("ignored") == "already handled" and plan_of()[1] == first_until,
          "%s -> %s" % (r, plan_of()))

    ev2 = dict(ev, id="evt_test_2")
    raw2, _h = signed(ev2)
    _r2, bad = signed(ev2, secret_used="wrong-secret")
    st, r = call("POST", "/api/webhooks/stripe", raw=False, headers=bad, body_bytes=raw2)
    check("a wrong signature is refused", st == 401, str(st))

    # The timestamp is inside what is signed, so an old capture cannot be
    # replayed even with a signature that was once genuine.
    raw3, oldhdr = signed(dict(ev, id="evt_test_3"), ts=time.time() - 4000)
    st, r = call("POST", "/api/webhooks/stripe", raw=False, headers=oldhdr, body_bytes=raw3)
    check("an old capture cannot be replayed", st == 401, str(st))

    ev4 = {"id": "evt_test_4", "type": "customer.subscription.deleted",
           "data": {"object": {"id": "sub_1", "metadata": {"org": str(oid)}}}}
    raw4, hdr4 = signed(ev4)
    st, r = call("POST", "/api/webhooks/stripe", raw=False, headers=hdr4, body_bytes=raw4)
    check("a cancelled subscription ends the plan",
          r.get("result") == "ended" and plan_of()[0] == "starter", "%s %s" % (r, plan_of()))

lemon = os.environ.get("SEAM_LEMON_WEBHOOK_SECRET", "")
if not lemon:
    skip("Merchant-of-Record plan orders", "SEAM_LEMON_WEBHOOK_SECRET not set for this run")
else:
    oid = org_of()
    set_plan("ops@seam.demo", "starter", None)
    payload = {"data": {"id": "ls_1"},
               "meta": {"event_name": "order_created",
                        "custom_data": {"plan": "pro", "org": str(oid)}}}
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(lemon.encode(), raw, hashlib.sha256).hexdigest()
    st, r = call("POST", "/api/webhooks/lemonsqueezy", headers={"X-Signature": sig,
                 "content-type": "application/json"}, body_bytes=raw)
    # This is the gap that made a paid Merchant-of-Record order do nothing: the
    # handler read custom_data.tpl and ignored custom_data.plan entirely.
    check("a paid plan order through the Merchant of Record activates it",
          st == 200 and r.get("result") == "activated" and plan_of()[0] == "pro",
          "%s %s %s" % (st, r, plan_of()))

# Leave the org paid so the suites after this one are not surprised.
set_plan("ops@seam.demo", "pro", time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400 * 30)))

print("\n---- %d passed, %d failed, %d skipped ----" % (ok, fail, skipped))
sys.exit(1 if fail else 0)
