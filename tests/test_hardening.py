# -*- coding: utf-8 -*-
"""Production hardening: the debugger is off, SQLite waits instead of failing,
roles are actually enforced, and a company can finally have a team."""
import os, sys, os, re, io, threading, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the app package
from seamclient import Client
import db as seamdb

c = Client(); c.login()

print("== the Werkzeug debugger is not the default ==")
src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
c.check("debug is opt-in via SEAM_DEBUG", 'os.environ.get("SEAM_DEBUG") == "1"' in src, "")
c.check("debug=True is no longer hard-coded", "debug=True" not in src, "")
c.check("it refuses to debug on a non-local host", "Refusing to run the debugger" in src, "")
c.check("a real WSGI server is used when available", "from waitress import serve" in src, "")
from waitress import serve as _serve
c.check("waitress is installed and importable", callable(_serve), "")

print("\n== an error page does not leak a console ==")
st, html = c.call("GET", "/api/passport/vehicle/abc", raw=True)
c.check("no debugger markup in the response",
        "Werkzeug Debugger" not in str(html) and "__debugger__" not in str(html), str(html)[:150])

print("\n== SQLite waits for a writer instead of giving up ==")
conn = seamdb.get_db()
c.check("busy_timeout is set",
        conn.execute("PRAGMA busy_timeout").fetchone()[0] == seamdb.BUSY_TIMEOUT_MS,
        str(conn.execute("PRAGMA busy_timeout").fetchone()[0]))
c.check("journal mode is WAL",
        conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal",
        str(conn.execute("PRAGMA journal_mode").fetchone()[0]))
conn.close()

errors = []
def hammer(n):
    try:
        cx = seamdb.get_db()
        for i in range(12):
            cx.execute("INSERT INTO events (workspace_id, order_id, actor_side, kind, summary) "
                       "VALUES (1, NULL, 'system', 'loadtest', ?)", ("t%d-%d" % (n, i),))
            cx.commit()
        cx.close()
    except Exception as e:
        errors.append(str(e))

threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
c.check("8 threads writing at once produced no 'database is locked'",
        not errors, str(errors[:2]))
cx = seamdb.get_db()
cx.execute("DELETE FROM events WHERE kind='loadtest'")
cx.commit(); cx.close()

print("\n== the team ==")
st, team = c.call("GET", "/api/team")
c.check("200", st == 200, str(team)[:200])
c.check("I am in it", any(m["is_me"] for m in team["members"]), str(team)[:200])
c.check("the founder owns the company", team["my_role"] == "owner", str(team["my_role"]))
c.check("roles offered", set(team["roles"]) == {"owner", "admin", "member", "viewer"},
        str(team["roles"]))

for m in team["members"]:
    if not m["is_me"]:
        c.call("DELETE", "/api/team/%d" % m["id"])

print("\n== inviting a colleague ==")
st, r = c.call("POST", "/api/team", {"email": "not-an-email", "name": "X"})
c.check("bad email refused", st == 400 and r.get("code") == "bad_email", "%s %s" % (st, r))
st, r = c.call("POST", "/api/team", {"email": "clerk@nordic-retail.example", "name": ""})
c.check("name required", st == 400 and r.get("code") == "field_required", "%s %s" % (st, r))

st, r = c.call("POST", "/api/team", {"email": "clerk@nordic-retail.example",
                                     "name": "Ива Колева", "role": "member"})
c.check("invited", st == 200, str(r)[:250])
mate = [m for m in r["members"] if m["email"] == "clerk@nordic-retail.example"]
c.check("appears in the team", bool(mate), str(r["members"])[:250])
c.check("with the role given", mate[0]["role"] == "member", str(mate[0]))
c.check("a set-password link is issued rather than a password",
        bool(r.get("invite_link")) and "?reset=" in r["invite_link"], str(r.get("invite_link")))
mate_id = mate[0]["id"]
link = r["invite_link"]

st, r = c.call("POST", "/api/team", {"email": "clerk@nordic-retail.example", "name": "Ива"})
c.check("cannot invite the same person twice", st == 400 and r.get("code") == "already_member",
        "%s %s" % (st, r))
st, r = c.call("POST", "/api/team", {"email": "build@seam.demo", "name": "David"})
c.check("cannot poach someone from another company", st == 400 and r.get("code") == "email_taken",
        "%s %s" % (st, r))

print("\n== the colleague sets their own password and gets in ==")
token = re.search(r"\?reset=(\S+)", link).group(1)
mate_c = Client()
st, r = mate_c.call("POST", "/api/auth/reset", {"token": token, "password": "Kolegata2026"})
c.check("password set", st == 200, str(r)[:200])
st, r = mate_c.login("clerk@nordic-retail.example", "Kolegata2026")
c.check("they can log in", st == 200, str(r)[:200])
st, me = mate_c.call("GET", "/api/me")
c.check("they see the same company", (me.get("org") or {}).get("name") ==
        (c.call("GET", "/api/me")[1].get("org") or {}).get("name"), str(me.get("org")))

print("\n== a member does the work but not the settings ==")
ws, order = mate_c.first_order()
st, r = mate_c.call("POST", "/api/orders/%d/comments" % order["id"], {"body": "проверих доставката"})
c.check("member can work on records", st == 200, str(r)[:150])
for path, payload in (("/api/qtsp", {"provider": ""}),
                      ("/api/automations", {"name": "x", "trigger": "order.created",
                                            "actions": [{"type": "notify"}]}),
                      ("/api/approval-chains", {"name": "x", "steps": [{"label": "y"}]}),
                      ("/api/cloud", {"provider": ""}),
                      ("/api/notify/sms-config", {"provider": ""}),
                      ("/api/team", {"email": "x@y.zz", "name": "Z"})):
    st, r = mate_c.call("POST", path, payload)
    c.check("member blocked from %s" % path, st == 403 and r.get("code") == "role_admin_only",
            "%s %s" % (st, str(r)[:120]))

print("\n== a viewer reads and nothing more ==")
st, r = c.call("PATCH", "/api/team/%d" % mate_id, {"role": "viewer"})
c.check("role changed", [m for m in r["members"] if m["id"] == mate_id][0]["role"] == "viewer",
        str(r["members"])[:200])
st, r = mate_c.call("POST", "/api/orders/%d/comments" % order["id"], {"body": "не бива"})
c.check("viewer cannot write", st == 403 and r.get("code") == "role_viewer", "%s %s" % (st, r))
st, r = mate_c.call("GET", "/api/orders/%d" % order["id"])
c.check("viewer can still read", st == 200, str(st))
st, r = mate_c.call("PUT", "/api/notify/settings", {"channels": {"email": False}})
c.check("viewer can still manage their own account", st == 200, "%s %s" % (st, str(r)[:120]))

print("\n== the owner is protected ==")
me_id = [m for m in team["members"] if m["is_me"]][0]["id"]
st, r = c.call("PATCH", "/api/team/%d" % me_id, {"role": "viewer"})
c.check("the owner cannot be demoted, not even by themselves",
        st == 400 and r.get("code") in ("owner_protected", "self_role"), "%s %s" % (st, r))
st, r = mate_c.call("PATCH", "/api/team/%d" % me_id, {"role": "viewer"})
c.check("a viewer certainly cannot demote the owner", st == 403, "%s %s" % (st, str(r)[:120]))
st, r = c.call("DELETE", "/api/team/%d" % me_id)
c.check("the owner cannot be removed", st == 400 and
        r.get("code") in ("owner_protected", "self_remove"), "%s %s" % (st, r))
# An admin who is not the owner is stopped by the self-checks instead.
st, adm = c.call("POST", "/api/team", {"email": "admin2@nordic-retail.example",
                                       "name": "Втори админ", "role": "admin"})
adm_id = [m for m in adm["members"] if m["email"] == "admin2@nordic-retail.example"][0]["id"]
adm_c = Client()
tok2 = re.search(r"\?reset=(\S+)", adm["invite_link"]).group(1)
adm_c.call("POST", "/api/auth/reset", {"token": tok2, "password": "Vtori2026x"})
adm_c.login("admin2@nordic-retail.example", "Vtori2026x")
st, r = adm_c.call("PATCH", "/api/team/%d" % adm_id, {"role": "member"})
c.check("an admin cannot change their own role", st == 400 and r.get("code") == "self_role",
        "%s %s" % (st, r))
st, r = adm_c.call("DELETE", "/api/team/%d" % adm_id)
c.check("an admin cannot remove themselves", st == 400 and r.get("code") == "self_remove",
        "%s %s" % (st, r))
st, r = adm_c.call("DELETE", "/api/team/%d" % me_id)
c.check("an admin cannot remove the owner", st == 400 and r.get("code") == "owner_protected",
        "%s %s" % (st, r))
c.call("DELETE", "/api/team/%d" % adm_id)

print("\n== removing a colleague leaves the audit trail intact ==")
st, od = c.call("GET", "/api/orders/%d" % order["id"])
before = len(od.get("comments") or [])
st, r = c.call("DELETE", "/api/team/%d" % mate_id)
c.check("removed", st == 200 and not any(m["id"] == mate_id for m in r["members"]),
        str(r["members"])[:200])
st, od2 = c.call("GET", "/api/orders/%d" % order["id"])
c.check("their comment is still there", len(od2.get("comments") or []) == before,
        "%d -> %d" % (before, len(od2.get("comments") or [])))
st, r = mate_c.call("GET", "/api/orders/%d" % order["id"])
c.check("they lose access immediately", st in (401, 403, 404), str(st))

print("\n== a client cannot claim its own connection was encrypted ==")
# Waitress discards X-Forwarded-* unless SEAM_TRUSTED_PROXY names a proxy, and
# the runner does not set one. So a caller sending the header must NOT get a
# cookie marked Secure: "this arrived over https" is not something the far end
# may assert about itself. (The same stripping is why the app saw plain http
# behind a real TLS proxy and shipped session cookies without Secure - the bug
# this pair of checks exists to keep closed from both directions.)
import http.client as _hc, json as _j

_host, _port = c.base.split("//", 1)[1].split(":")
_conn = _hc.HTTPConnection(_host, int(_port), timeout=10)
_conn.request("POST", "/api/auth/login",
              _j.dumps({"email": "ops@seam.demo", "password": "demo1234"}),
              {"Content-Type": "application/json", "X-Forwarded-Proto": "https"})
_r = _conn.getresponse()
_r.read()
_ck = [v for k, v in _r.getheaders() if k.lower() == "set-cookie"]
_conn.close()
c.check("a forged forwarded header does not make the cookie Secure",
        _ck and not any("Secure" in v for v in _ck), str(_ck)[:120])
c.check("the session cookie is still HttpOnly and SameSite",
        all("HttpOnly" in v and "SameSite" in v for v in _ck), str(_ck)[:120])

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = io.open(os.path.join(_root, "app.py"), encoding="utf-8").read()
c.check("there is a switch for a real proxy", "SEAM_TRUSTED_PROXY" in _src, "")
c.check("and it names which headers it will trust",
        "trusted_proxy_headers" in _src and "clear_untrusted_proxy_headers" in _src, "")
# "*" plus a public bind means anybody can forge the header and be believed.
c.check("trusting everyone on a public bind is refused",
        'TRUSTED_PROXY == "*"' in _src and "Refusing" in _src or
        ('TRUSTED_PROXY == "*"' in _src and "SystemExit" in _src), "")
c.check("and the go-live check blames the missing switch, not the certificate",
        'SEAM_TRUSTED_PROXY" if not TRUSTED_PROXY' in _src, "")
# A container has to bind 0.0.0.0 and is still only reachable through the
# platform's proxy, so the refusal above must have a way through that states
# the claim rather than hiding it.
c.check("a container can say the port is private", "SEAM_PROXY_ONLY" in _src, "")
c.check("and the refusal names that way out",
        "SEAM_PROXY_ONLY=1 if nothing but the proxy" in _src, "")

print("\n== the shipped deployment files agree with the code ==")
_deploy = os.path.join(_root, "deploy")
c.check("there is an install script", os.path.exists(os.path.join(_deploy, "install.sh")), "")
_unit = io.open(os.path.join(_deploy, "seam.service"), encoding="utf-8").read()
c.check("the service reads its configuration from a file",
        "EnvironmentFile=" in _unit, "")
c.check("and restarts itself", "Restart=always" in _unit, "")
_docker = io.open(os.path.join(_deploy, "Dockerfile"), encoding="utf-8").read()
# The two mistakes that lose a database: no volume, and a bind the platform
# cannot reach.
c.check("the container keeps the database off the image",
        "SEAM_DB=/data/" in _docker, "")
c.check("and states that only the proxy can reach it",
        "SEAM_PROXY_ONLY=1" in _docker, "")
_fly = io.open(os.path.join(_deploy, "fly.toml"), encoding="utf-8").read()
c.check("the fly configuration mounts a volume at that path",
        "destination = \"/data\"" in _fly, "")
c.check("and keeps one machine, because SQLite has one writer",
        "min_machines_running = 1" in _fly, "")

sys.exit(c.summary())
