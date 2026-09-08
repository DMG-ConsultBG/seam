# -*- coding: utf-8 -*-
"""Signing in: the second factor, and control over open sessions.

The point of these checks is what must *not* work. A password on its own must
not sign anyone in once a second factor is on; a code must not work twice; a
recovery code must not work twice; a revoked session must stop being accepted
on the very next request, not at some later refresh.
"""
import os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from seamclient import Client
import totp
import db as seamdb

c = Client(); c.login()


def _unlock(email="ops@seam.demo"):
    """Run the console tool the way an operator would, and return its exit
    code. It calls sys.exit, so the SystemExit is what carries the answer."""
    import unlock_2fa
    argv = sys.argv
    sys.argv = ["unlock_2fa.py", email]
    try:
        return unlock_2fa.main() or 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = argv

print("== the algorithm before anything is built on it ==")
r = totp.self_test()
c.check("RFC 6238 test vectors", r["ok"], str(r["vectors"]))
c.check("a secret is 160 bits of base32", len(totp.new_secret()) == 32, "")
s = totp.new_secret()
c.check("otpauth URI names the issuer", "issuer=Seam" in totp.provisioning_uri(s, "a@b.c"), "")
step = totp._steps()
c.check("the current code verifies", totp.verify(s, totp.code_at(s, step)) == step, "")
c.check("and is refused a second time", totp.verify(s, totp.code_at(s, step), step) is None, "")
c.check("a code from long ago is refused",
        totp.verify(s, totp.code_at(s, step - 50)) is None, "")
c.check("a wrong code is refused", totp.verify(s, "000000") is None or
        totp.code_at(s, step) == "000000", "")

print("\n== turning the second factor on ==")
st, r = c.call("GET", "/api/auth/2fa")
c.check("starts off", st == 200 and r["enabled"] is False, str(r))
st, setup = c.call("POST", "/api/auth/2fa/setup", {})
c.check("a secret is issued", st == 200 and len(setup.get("secret", "")) == 32, str(setup)[:120])
c.check("and grouped for typing", " " in setup["grouped"], setup["grouped"])
st, r = c.call("GET", "/api/auth/2fa")
c.check("but nothing is on until a code is proved", r["enabled"] is False, str(r))

st, r = c.call("POST", "/api/auth/2fa/enable", {"code": "000000"})
c.check("a wrong code does not enable it", st == 400 and r.get("code") == "totp_bad",
        "%s %s" % (st, r))
now = totp._steps()
st, r = c.call("POST", "/api/auth/2fa/enable", {"code": totp.code_at(setup["secret"], now)})
c.check("the right code does", st == 200 and r.get("enabled"), str(r)[:160])
codes = r.get("recovery_codes") or []
c.check("recovery codes are handed over once", len(codes) == 8, str(len(codes)))
st, r = c.call("GET", "/api/auth/2fa")
c.check("status reports them", r["enabled"] and r["recovery_left"] == 8, str(r))

print("\n== the password alone is no longer enough ==")
two = Client()
st, r = two.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
c.check("login asks for the second factor", st == 200 and r.get("totp_required") is True, str(r))
two.csrf = r.get("csrf", "")
st, me = two.call("GET", "/api/me")
c.check("and nobody is signed in yet", st == 401, str(me)[:120])

st, r = two.call("POST", "/api/auth/2fa/verify", {"code": "111111"})
c.check("a wrong code is refused", st == 401 and r.get("code") == "totp_bad", "%s %s" % (st, r))

# A fresh step, so the code is not the one already spent enabling it.
while totp._steps() == now:
    time.sleep(1)
code = totp.code_at(setup["secret"], totp._steps())
st, r = two.call("POST", "/api/auth/2fa/verify", {"code": code})
c.check("the right code signs in", st == 200 and r.get("email") == "ops@seam.demo", str(r)[:160])
two.csrf = r.get("csrf", "")
st, me = two.call("GET", "/api/me")
c.check("and the session works", st == 200, str(me)[:120])

print("\n== a code cannot be replayed ==")
three = Client()
st, r = three.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
three.csrf = r.get("csrf", "")
st, r = three.call("POST", "/api/auth/2fa/verify", {"code": code})
c.check("the same code is refused the second time", st == 401, "%s %s" % (st, str(r)[:120]))

print("\n== a recovery code works once ==")
st, r = three.call("POST", "/api/auth/2fa/verify", {"recovery_code": codes[0]})
c.check("recovery code signs in", st == 200 and r.get("email") == "ops@seam.demo", str(r)[:140])
three.csrf = r.get("csrf", "")
four = Client()
st, r = four.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
four.csrf = r.get("csrf", "")
st, r = four.call("POST", "/api/auth/2fa/verify", {"recovery_code": codes[0]})
c.check("and not a second time", st == 401, "%s %s" % (st, str(r)[:120]))
st, r = c.call("GET", "/api/auth/2fa")
c.check("one code is spent", r["recovery_left"] == 7, str(r))

print("\n== where this account is signed in ==")
st, s1 = c.call("GET", "/api/auth/sessions")
c.check("sessions are listed", st == 200 and len(s1["sessions"]) >= 3, str(len(s1.get("sessions", []))))
c.check("this one is marked", any(x["current"] for x in s1["sessions"]), "")
c.check("each carries where and when",
        all(x["last_seen"] and x["created_at"] for x in s1["sessions"]), "")
c.check("the lifetimes are stated", s1["idle_hours"] > 0 and s1["max_days"] > 0, str(s1)[:120])

# Ask the other client which row is its own rather than guessing: by the time
# the whole suite has run, this account has several sessions open.
st, s2 = two.call("GET", "/api/auth/sessions")
other = [x for x in s2["sessions"] if x["current"]][0]
c.check("the other client sees its own row", other["id"] != [x for x in s1["sessions"] if x["current"]][0]["id"], "")
st, r = c.call("DELETE", "/api/auth/sessions/%d" % other["id"])
c.check("one can be cut off", st == 200, str(r))
st, me = two.call("GET", "/api/me")
c.check("the cut-off session stops working at once",
        st == 401 and me.get("code") == "session_revoked", "%s %s" % (st, str(me)[:120]))

st, r = c.call("POST", "/api/auth/sessions/revoke-others", {})
c.check("or all the others at once", st == 200, str(r))
st, me = three.call("GET", "/api/me")
c.check("they are all out", st == 401, "%s %s" % (st, str(me)[:120]))
st, me = c.call("GET", "/api/me")
c.check("but this one stays in", st == 200, str(me)[:120])

print("\n== signing out really signs out ==")
five = Client()
st, r = five.call("POST", "/api/auth/login", {"email": "build@seam.demo", "password": "demo1234"})
five.csrf = r.get("csrf", "")
st, _m = five.call("GET", "/api/me")
c.check("partner signs in without a second factor", st == 200, str(st))
five.call("POST", "/api/auth/logout", {})
st, me = five.call("GET", "/api/me")
c.check("and is out afterwards", st == 401, str(st))

print("\n== turning it off asks for the password ==")
st, r = c.call("POST", "/api/auth/2fa/disable", {"password": "wrong-one"})
c.check("a wrong password will not do it", st == 403 and r.get("code") == "bad_password",
        "%s %s" % (st, r))
st, r = c.call("POST", "/api/auth/2fa/disable", {"password": "demo1234"})
c.check("the right one does", st == 200 and r.get("enabled") is False, str(r))
st, r = c.call("GET", "/api/auth/2fa")
c.check("recovery codes go with it", r["enabled"] is False and r["recovery_left"] == 0, str(r))

st, r = c.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
c.check("the password signs in again", st == 200 and not r.get("totp_required"), str(r)[:120])
c.csrf = r.get("csrf", "")

print("\n== the state an owner can actually reach ==")
# Lose the authenticator, spend all eight codes, and `2fa/disable` is useless:
# it needs a session, and getting one needs the factor that is gone. Preflight
# recommends 2FA on the owner - the one account nobody else can remove - so the
# more carefully somebody follows the advice, the worse the trap. unlock_2fa.py
# is the way back, and it has to keep working.
st, setup = c.call("POST", "/api/auth/2fa/setup", {})
st, r = c.call("POST", "/api/auth/2fa/enable",
               {"code": totp.code_at(setup["secret"], totp._steps())})
spent = r.get("recovery_codes") or []
used = 0
for code in spent:
    s = Client()
    st, _ = s.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
    st, _ = s.call("POST", "/api/auth/2fa/verify", {"recovery_code": code})
    if st == 200:
        used += 1
st, r = c.call("GET", "/api/auth/2fa")
# Not all eight: burning them one after another trips the login rate limit,
# which is the limiter doing its job. What matters is that each one that was
# accepted is gone for good, so the supply really does run out.
c.check("each recovery code used is gone for good",
        used and r["recovery_left"] == len(spent) - used,
        "used %d, %d left of %d" % (used, r["recovery_left"], len(spent)))

locked = Client()
st, r = locked.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
locked.csrf = r.get("csrf", "")
c.check("the right password alone no longer gets in", r.get("totp_required") is True, str(r)[:120])
st, r = locked.call("POST", "/api/auth/2fa/disable", {"password": "demo1234"})
c.check("and disabling needs the session it cannot have",
        st == 401 and r.get("code") == "auth_required", "%s %s" % (st, str(r)[:100]))

import unlock_2fa as UN
_conn = seamdb.get_db()
c.check("the tool lists who is locked behind a factor",
        any(x["email"] == "ops@seam.demo" for x in UN.rows(_conn)), "")
_conn.close()

os.environ["SEAM_UNLOCK_PASSWORD"] = "not-the-password"
c.check("a wrong password changes nothing", _unlock() != 0, "it exited zero")
st, r = c.call("GET", "/api/auth/2fa")
c.check("and the factor is still on", r["enabled"] is True, str(r))

os.environ["SEAM_UNLOCK_PASSWORD"] = "demo1234"
c.check("the right one clears it", _unlock() == 0, "it did not exit zero")
back = Client()
st, r = back.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
c.check("the owner is back in", st == 200 and not r.get("totp_required"), str(r)[:120])
back.csrf = r.get("csrf", "")
st, e = back.call("GET", "/api/admin/errors")
c.check("and the removal is on the record",
        any(x.get("kind") == "security" and "ops@seam.demo" in (x.get("message") or "")
            for x in e.get("errors", [])), "no audit entry")
os.environ.pop("SEAM_UNLOCK_PASSWORD", None)

sys.exit(c.summary())
