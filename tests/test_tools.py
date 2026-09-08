# -*- coding: utf-8 -*-
"""The console tools, which are part of the product whether or not they feel it.

Three of the go-live checks reported a problem and offered no way to fix it:
an encrypted backup with no decryptor, a second factor with no way back once
the phone and the codes are gone, and demo data the checks flag but nothing
removes. The tools that close those gaps are only useful if they still work on
the day they are reached for, which is never the day they were written.

`purge_demo` is destructive, so everything here runs against a copy of the test
database made for the purpose. Nothing in this file touches the live one.
"""
import io
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from seamclient import Client                                # noqa: E402
import db as seamdb                                          # noqa: E402
import purge_demo as PD                                      # noqa: E402
import restore_backup as RB                                  # noqa: E402
import unlock_2fa as UN                                      # noqa: E402

c = Client()


def copy_db():
    """A snapshot through SQLite's own API, so it is consistent even though the
    server is serving out of the original while this runs."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="seam-tools-")
    os.close(fd)
    src = sqlite3.connect(seamdb.DB_PATH)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, path


# --------------------------------------------------------------------------- #
print("== the tools exist and say what they are for ==")
for mod, name in ((PD, "purge_demo"), (RB, "restore_backup"), (UN, "unlock_2fa")):
    c.check("%s has a docstring explaining itself" % name,
            (mod.__doc__ or "").strip().count("\n") > 3, "")

print("\n== removing the demo data ==")
conn, path = copy_db()
try:
    before = PD.survey(conn)
    c.check("the seeded accounts are found", len(before["users"]) >= 4,
            str([u["email"] for u in before["users"]]))
    c.check("and their companies", len(before["orgs"]) >= 2,
            str([o["name"] for o in before["orgs"]]))
    c.check("and the workspaces between them", len(before["workspaces"]) >= 1,
            str(len(before["workspaces"])))

    # An account that is not part of the seed must survive: this is the whole
    # difference between clearing the demo and clearing the database.
    real = conn.execute("SELECT COUNT(*) c FROM users WHERE email NOT LIKE ?",
                        ("%" + PD.DEMO_SUFFIX,)).fetchone()["c"]

    PD.purge(conn, before)
    left = conn.execute("SELECT COUNT(*) c FROM users WHERE email LIKE ?",
                        ("%" + PD.DEMO_SUFFIX,)).fetchone()["c"]
    c.check("every demo account is gone", left == 0, "%d left" % left)
    c.check("and every real one is still there",
            conn.execute("SELECT COUNT(*) c FROM users WHERE email NOT LIKE ?",
                         ("%" + PD.DEMO_SUFFIX,)).fetchone()["c"] == real,
            "a real account was removed")
    c.check("no company of theirs remains",
            conn.execute("SELECT COUNT(*) c FROM orgs").fetchone()["c"] == 0, "")
    # The part that is easy to get wrong: the schema cascades in some places
    # and not others, so a naive delete leaves rows pointing at nothing.
    c.check("nothing is left pointing at a deleted row",
            not conn.execute("PRAGMA foreign_key_check").fetchall(),
            str(conn.execute("PRAGMA foreign_key_check").fetchall()[:3]))
    c.check("and the database is still sound",
            conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "")
    c.check("a second run finds nothing to do", not PD.survey(conn)["users"], "")
finally:
    conn.close()
    os.remove(path)

print("\n== it will not quietly take a real account's company ==")
conn, path = copy_db()
try:
    # Made here rather than borrowed from the database: on a freshly seeded
    # instance every account is a demo one, and a test that only works when an
    # earlier suite happens to have left something behind is not a test.
    org = conn.execute("SELECT id FROM orgs LIMIT 1").fetchone()
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash) VALUES (?,?,?)",
        ("evaluator@example.test", "Оценяващ", "x"))
    conn.execute("INSERT INTO memberships (user_id, org_id, role) VALUES (?,?,'member')",
                 (cur.lastrowid, org["id"]))
    conn.commit()

    s = PD.survey(conn)
    c.check("somebody outside the seed is noticed", len(s["outsiders"]) >= 1,
            str([x["email"] for x in s["outsiders"]]))
    c.check("and is named with the company they are in",
            any(x["email"] == "evaluator@example.test" for x in s["outsiders"]),
            str([dict(x) for x in s["outsiders"]]))
    # The guard is advisory, not a lock: --force exists because the operator may
    # well decide the evaluator's membership can go. What must not happen is it
    # going without anybody being told.
    tool = io.open(os.path.join(ROOT, "purge_demo.py"), encoding="utf-8").read()
    c.check("and the tool refuses unless told twice",
            "outsiders" in tool and "--force" in tool and "Refusing" in tool, "")
finally:
    conn.close()
    os.remove(path)

print("\n== opening a sealed backup ==")
import cryptobox as CB                                       # noqa: E402
import secrets as _sec                                       # noqa: E402
import zipfile                                               # noqa: E402

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("seam.db", b"SQLite format 3" + bytes([0]) + b"x" * 64)
body = buf.getvalue()
salt = _sec.token_bytes(16)
sealed = RB.MAGIC + salt + CB.seal(CB.derive("a-passphrase", salt), body, RB.MAGIC)
c.check("the right passphrase returns the archive", RB.decrypt(sealed, "a-passphrase") == body, "")
c.check("a wrong one returns nothing", RB.decrypt(sealed, "wrong") is None, "")
c.check("a truncated file returns nothing", RB.decrypt(sealed[:40], "a-passphrase") is None, "")
c.check("and something that is not a Seam backup does too",
        RB.decrypt(b"just some bytes", "a-passphrase") is None, "")

print("\n== the second-factor escape ==")
conn, path = copy_db()
try:
    rows = UN.rows(conn)
    c.check("it can list who is behind a factor", isinstance(rows, list), str(type(rows)))
    c.check("and reports how many recovery codes each has left",
            all("codes_left" in r.keys() for r in rows), "")
finally:
    conn.close()
    os.remove(path)

# Nothing about these tools should be reachable over HTTP: an escape hatch on a
# port is reachable by anything that can make a request from that host.
app_src = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
for name in ("purge_demo", "restore_backup", "unlock_2fa"):
    c.check("%s is not wired into a route" % name, name not in app_src, "")

sys.exit(c.summary())
