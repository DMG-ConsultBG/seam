# -*- coding: utf-8 -*-
"""Operations: limits that survive a restart, failures that are visible, and a
backup that actually restores."""
import os, sys, os, io, json, time, zipfile, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the app package
from seamclient import Client
import db as seamdb

c = Client(); c.login()

print("== rate limits are in the database, not in memory ==")
conn = seamdb.get_db()
tables = {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
c.check("rate_events table exists", "rate_events" in tables, str(sorted(tables))[:200])
c.check("login_locks table exists", "login_locks" in tables, "")
c.check("error_log table exists", "error_log" in tables, "")

conn.execute("DELETE FROM rate_events WHERE bucket='unittest'")
conn.commit()
import app as seamapp
with seamapp.app.test_request_context("/"):
    hits = [seamapp.rate_limited("unittest", 3, 60) for _ in range(5)]
c.check("first three allowed", hits[:3] == [False, False, False], str(hits))
c.check("fourth and fifth blocked", hits[3:] == [True, True], str(hits))
rows = conn.execute("SELECT COUNT(*) c FROM rate_events WHERE bucket='unittest'").fetchone()["c"]
c.check("the attempts are persisted, so a restart does not reset them", rows == 5, str(rows))
conn.execute("DELETE FROM rate_events WHERE bucket='unittest'")

print("\n== the window really expires ==")
old = time.time() - 120
conn.execute("INSERT INTO rate_events (bucket, at) VALUES ('unittest2', ?)", (old,))
conn.commit()
with seamapp.app.test_request_context("/"):
    c.check("an attempt outside the window does not count",
            seamapp.rate_limited("unittest2", 1, 60) is False, "")
conn.execute("DELETE FROM rate_events WHERE bucket LIKE 'unittest%'")
conn.commit()

print("\n== login lockout survives a restart ==")
bad = Client()
for i in range(6):
    bad.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "wrong%d" % i})
row = conn.execute("SELECT fails, until FROM login_locks WHERE email='ops@seam.demo'").fetchone()
c.check("failures are recorded in the database", row and row["fails"] >= 5, str(dict(row) if row else None))
c.check("the account is locked", row and row["until"] > time.time(), str(row["until"] if row else 0))
st, r = bad.call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
c.check("even the right password is refused while locked",
        st in (400, 401, 403, 429) and r.get("code") in ("account_locked", "rate_limited"),
        "%s %s" % (st, str(r)[:120]))
conn.execute("DELETE FROM login_locks WHERE email='ops@seam.demo'")
# The IP rate limit is persistent too, and deliberately tripping it here would
# otherwise lock out every suite that runs after this one. Clean up what this
# test caused, exactly as it does for the lock itself.
conn.execute("DELETE FROM rate_events WHERE bucket LIKE 'login%'")
conn.commit()
st, r = bad.login()
c.check("clearing the lock lets them back in", st == 200, str(r)[:150])
conn.close()

print("\n== an unhandled failure is recorded, not lost ==")
c2 = Client(); c2.login()
st, before = c2.call("GET", "/api/admin/errors")
n_before = len(before["errors"])
import app as A
with A.app.test_request_context("/api/pretend"):
    try:
        raise ValueError("умишлена грешка за тест")
    except ValueError as e:
        A.log_error(e)
st, after = c2.call("GET", "/api/admin/errors")
c.check("a new entry appeared", len(after["errors"]) == n_before + 1,
        "%d -> %d" % (n_before, len(after["errors"])))
e0 = after["errors"][0]
c.check("the message is kept", "умишлена" in e0["message"], str(e0)[:200])
c.check("the traceback is kept", "ValueError" in (e0["traceback"] or ""), str(e0["traceback"])[:150])
c.check("the request path is kept", e0["path"] == "/api/pretend", str(e0["path"]))
c.check("it starts unseen", e0["seen"] is False, str(e0["seen"]))

st, h = c2.call("GET", "/api/admin/health")
c.check("health reports unseen errors", h["errors"]["unseen"] >= 1, str(h["errors"]))
c.check("health reports storage sizes", h["storage"]["database"] > 0, str(h["storage"]))
c.check("health reports which services are configured",
        set(h["services"]) == {"email", "sms", "cloud", "qtsp"}, str(h["services"]))
st, r = c2.call("POST", "/api/admin/errors/seen", {})
st, h2 = c2.call("GET", "/api/admin/health")
c.check("marking them seen clears the count", h2["errors"]["unseen"] == 0, str(h2["errors"]))

print("\n== the response to the user leaks nothing ==")
st, r = c2.call("GET", "/api/admin/errors")
c.check("only an admin may read the log", st == 200, str(st))

print("\n== a backup that opens without Seam ==")
st, r = c2.call("POST", "/api/admin/backup", {})
c.check("created", st == 200 and r.get("bytes", 0) > 1000, str(r)[:200])
name = r["name"]
st, blob = c2.call("GET", "/api/admin/backup/%s" % name, binary=True)
c.check("downloadable", st == 200 and len(blob) == r["bytes"], "%s %s" % (st, len(blob)))

# An instance that followed the go-live advice has SEAM_BACKUP_PASSPHRASE set,
# and then the archive is sealed rather than a zip. This block used to assume
# it never was and died on the first ZipFile() - so the whole ops suite crashed
# on exactly the configuration it is meant to certify. Open it the way the
# operator would, then run the same checks on what comes out.
import restore_backup as RB
if name.endswith(".enc"):
    c.check("an encrypted archive carries the header", blob[:8] == RB.MAGIC, str(blob[:8]))
    _pw = os.environ.get("SEAM_BACKUP_PASSPHRASE", "")
    c.check("the passphrase is available to open it", bool(_pw),
            "SEAM_BACKUP_PASSPHRASE is unset, so this archive cannot be opened")
    opened = RB.decrypt(blob, _pw) if _pw else None
    c.check("and the restore tool opens it", opened is not None, "decrypt returned nothing")
    c.check("a wrong passphrase does not", RB.decrypt(blob, _pw + "x") is None, "")
    blob = opened or b""
else:
    c.check("an unencrypted archive is a plain zip", blob[:2] == b"PK", str(blob[:8]))
z = zipfile.ZipFile(io.BytesIO(blob))
names = z.namelist()
c.check("valid zip", z.testzip() is None, "")
c.check("the database is in it", "seam.db" in names, str(names[:5]))
c.check("restore instructions included", "RESTORE.txt" in names, str(names[:5]))
# The archive used to carry config/.secret next to the database it signs
# sessions for, which made one stolen backup a complete compromise. Keys are
# the operator's to keep elsewhere.
c.check("no key material travels with the data",
        not any(n.startswith("config/") for n in names),
        str([n for n in names if "config" in n]))
c.check("and the restore note says where the keys went",
        "NOT in this archive" in z.read("RESTORE.txt").decode("utf-8"),
        z.read("RESTORE.txt").decode("utf-8")[:200])

print("\n== the copied database is consistent and complete ==")
tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_restored.db")
with open(tmp, "wb") as f:
    f.write(z.read("seam.db"))
rc = sqlite3.connect(tmp)
c.check("integrity check passes", rc.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "")
live = seamdb.get_db()
for table in ("users", "orgs", "orders", "events", "signatures"):
    a = live.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    b = rc.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    c.check("%s carried over (%d rows)" % (table, b), a == b, "%d vs %d" % (a, b))
live.close(); rc.close(); os.remove(tmp)

print("\n== only an owner or admin may touch operations ==")
st, team = c2.call("GET", "/api/team")
for m in team["members"]:
    if not m["is_me"]:
        c2.call("DELETE", "/api/team/%d" % m["id"])
st, inv = c2.call("POST", "/api/team", {"email": "ops-test@nordic-retail.example",
                                        "name": "Тест Оператор", "role": "member"})
import re as _re
tok = _re.search(r"\?reset=(\S+)", inv["invite_link"]).group(1)
mem = Client()
mem.call("POST", "/api/auth/reset", {"token": tok, "password": "Operator2026"})
mem.login("ops-test@nordic-retail.example", "Operator2026")
for path in ("/api/admin/errors", "/api/admin/health"):
    st, r = mem.call("GET", path)
    c.check("member cannot read %s" % path, st == 403, "%s %s" % (st, str(r)[:100]))
st, r = mem.call("POST", "/api/admin/backup", {})
c.check("member cannot take a backup", st == 403, "%s %s" % (st, str(r)[:100]))
mid = [m for m in inv["members"] if m["email"] == "ops-test@nordic-retail.example"][0]["id"]
c2.call("DELETE", "/api/team/%d" % mid)

print("\n== path traversal on the backup download ==")
for evil in ("..%2f..%2fseam.db", "seam-backup-x.zip", "....//seam.db"):
    st, r = c2.call("GET", "/api/admin/backup/%s" % evil, raw=True)
    c.check("refused: %s" % evil, st in (400, 404), str(st))


print("\n== what watches this from outside ==")
# Nothing was watching this installation at all: the only health screen needs a
# session, so a monitor could not reach it and a 2 a.m. fault would be found by
# a customer. /healthz answers without one, and says almost nothing else.
import urllib.request as _u, urllib.error as _e, json as _j

def _anon(path):
    """No cookies, no CSRF: exactly what a monitor sends."""
    try:
        with _u.urlopen(c.base + path, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except _e.HTTPError as ex:
        return ex.code, ex.read().decode("utf-8", "ignore")

st, txt = _anon("/healthz")
c.check("healthz answers without a session", st == 200, str(st))
try:
    payload = _j.loads(txt)
except ValueError:
    payload = {}
c.check("and says the instance is alive", payload.get("ok") is True, txt[:120])
c.check("and says nothing else at all", set(payload) == {"ok"}, str(sorted(payload)))
# Anything below is a detail an attacker would like and a monitor does not need.
for leak in ("version", "database", "uploads", "errors", "users", "host", "path"):
    c.check("healthz does not leak %s" % leak, leak not in txt.lower(), txt[:120])
c.check("and is never cached", "no-store" in _u.urlopen(c.base + "/healthz").headers
        .get("Cache-Control", ""), "cacheable")

# The alert hook: off unless a URL is set, rate limited when it is, and it must
# never carry a traceback or anyone's data out of the building.
import app as _A
c.check("alerting is off until a URL is given", not _A.ALERT_URL or
        _A.ALERT_URL.startswith(("http://", "https://")), _A.ALERT_URL)
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = io.open(os.path.join(_root, "app.py"), encoding="utf-8").read()
_fn = _src[_src.index("def _alert_operator"):_src.index("def log_error")]
# Search the code, not the prose: the comment above the payload says the word
# "traceback" precisely because it is explaining that none is sent.
_code = "\n".join(l.split("#", 1)[0] for l in _fn.splitlines())
c.check("an alert carries no traceback", "traceback" not in _code, "")
c.check("nor anything identifying a person",
        not any(w in _code for w in ("user_id", "email", "session", "_client_ip")), "")
c.check("an alert is rate limited", "ALERT_WINDOW" in _fn, "")
c.check("and never raises out of the failing request", "except Exception" in _fn, "")
c.check("nor blocks it", "Thread" in _fn, "")


print("\n== an encrypted backup can be opened again ==")
# SEAM_BACKUP_PASSPHRASE turns the archive into SEAMBK01 + salt + sealed bytes.
# There was no way back: the format lived in app.py and the restore notes lived
# inside the archive nobody could open yet. An encryption with no supported way
# out is a second failure waiting behind the first, so the tool is part of the
# product and is checked like one.
# Built here rather than taken from the server, so these hold whether or not
# this particular instance has a passphrase set. The live round trip is checked
# further up, where the real archive is opened the way an operator would.
import cryptobox as _cb
import secrets as _sec

_tpw = "test-fraza-9931"
_plain = io.BytesIO()
with zipfile.ZipFile(_plain, "w") as _archive:
    _archive.writestr("seam.db", b"SQLite format 3" + bytes([0]) + b"x" * 200)
    _archive.writestr("RESTORE.txt", "notes")
_body = _plain.getvalue()
_salt = _sec.token_bytes(16)
_sealed = RB.MAGIC + _salt + _cb.seal(_cb.derive(_tpw, _salt), _body, RB.MAGIC)

c.check("the right passphrase opens it", RB.decrypt(_sealed, _tpw) == _body, "")
c.check("a wrong one does not", RB.decrypt(_sealed, "not-it") is None, "")
c.check("and neither does a file altered by one bit",
        RB.decrypt(_sealed[:-1] + bytes([_sealed[-1] ^ 1]), _tpw) is None, "")
c.check("a plain archive is recognised rather than mangled",
        RB.decrypt(_body, _tpw) is None, "")
# A passphrase given as an argument ends up in shell history and in the process
# list, where every other user on the box can read it.
_tool = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "restore_backup.py"), encoding="utf-8").read()
c.check("the tool never takes the passphrase as an argument",
        '"--passphrase"' not in _tool and "add_argument(\"--passphrase\"" not in _tool, "")
c.check("and offers a file and a prompt instead",
        "--passphrase-file" in _tool and "getpass" in _tool, "")

sys.exit(c.summary())
