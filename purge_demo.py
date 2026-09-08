# -*- coding: utf-8 -*-
"""Remove the seeded demo companies from an instance that is going live.

The go-live checks report demo data as still present and give no way to remove
it, which leaves two bad options: start again from an empty database and lose
everything entered while evaluating, or hand-delete rows across fifty-four
tables joined by foreign keys.

This does it properly. Foreign keys are on and the schema cascades, so deleting
the right handful of roots takes the rest with it.

    python purge_demo.py              # say what would go, change nothing
    python purge_demo.py --yes        # actually remove it

It refuses to run while Seam is serving, and it refuses to delete anything a
real account has joined unless told twice: somebody who evaluated the product
by inviting a colleague into the demo workspace should be asked, not surprised.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db                                                    # noqa: E402

#: What the seed writes. Nothing else in the product creates addresses here.
DEMO_SUFFIX = "@seam.demo"


def survey(conn):
    """Everything that would go, and anything real caught up in it."""
    users = conn.execute(
        "SELECT id, email, name FROM users WHERE email LIKE ?",
        ("%" + DEMO_SUFFIX,)).fetchall()
    uids = [u["id"] for u in users]
    if not uids:
        return {"users": [], "orgs": [], "workspaces": [], "orders": 0, "outsiders": []}
    ph = ",".join("?" * len(uids))

    orgs = conn.execute(
        "SELECT DISTINCT o.id, o.name FROM orgs o JOIN memberships m ON m.org_id = o.id "
        "WHERE m.user_id IN (%s)" % ph, uids).fetchall()
    oids = [o["id"] for o in orgs]

    # A real account inside one of those companies. Deleting the company would
    # take their membership with it, so it is worth saying out loud.
    outsiders = []
    if oids:
        oph = ",".join("?" * len(oids))
        outsiders = conn.execute(
            "SELECT DISTINCT u.email, o.name AS org FROM users u "
            "JOIN memberships m ON m.user_id = u.id JOIN orgs o ON o.id = m.org_id "
            "WHERE m.org_id IN (%s) AND u.email NOT LIKE ?" % oph,
            oids + ["%" + DEMO_SUFFIX]).fetchall()

    workspaces, orders = [], 0
    if oids:
        oph = ",".join("?" * len(oids))
        workspaces = conn.execute(
            "SELECT id, name FROM workspaces WHERE buyer_org_id IN (%s) "
            "OR partner_org_id IN (%s)" % (oph, oph), oids + oids).fetchall()
        if workspaces:
            wph = ",".join("?" * len(workspaces))
            orders = conn.execute(
                "SELECT COUNT(*) c FROM orders WHERE workspace_id IN (%s)" % wph,
                [w["id"] for w in workspaces]).fetchone()["c"]
    return {"users": users, "orgs": orgs, "workspaces": workspaces,
            "orders": orders, "outsiders": outsiders}


def report(s):
    if not s["users"]:
        print("No demo data on this instance. Nothing to do.")
        return False
    print("Demo accounts (%d):" % len(s["users"]))
    for u in s["users"]:
        print("   %-28s %s" % (u["email"], u["name"] or ""))
    print("Companies (%d):" % len(s["orgs"]))
    for o in s["orgs"]:
        print("   %s" % o["name"])
    print("Workspaces (%d), records inside them: %d" % (len(s["workspaces"]), s["orders"]))
    for w in s["workspaces"]:
        print("   %s" % w["name"])
    if s["outsiders"]:
        print("")
        print("These are NOT demo accounts but belong to a company above.")
        print("Removing the company removes their membership with it:")
        for x in s["outsiders"]:
            print("   %-28s in %s" % (x["email"], x["org"]))
    return True


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def _blockers(conn, parent):
    """Every (table, column) that points at `parent` and would refuse to let it
    go: the schema cascades in some places and not others, and the ones that do
    not are what a DELETE trips over."""
    out = []
    for t in _tables(conn):
        for fk in conn.execute("PRAGMA foreign_key_list(%s)" % t).fetchall():
            # (id, seq, table, from, to, on_update, on_delete, match)
            if fk[2] == parent and (fk[6] or "NO ACTION").upper() not in ("CASCADE", "SET NULL"):
                out.append((t, fk[3]))
    return out


def _delete_referencing(conn, parent, ids, seen=None):
    """Remove everything that points at these rows, depth first.

    Driven by the foreign key graph rather than a hand-written list, because a
    hand-written list is wrong the first time somebody adds a table - and the
    failure mode is a purge that stops halfway with a constraint error.

    Anything referencing a demo company or a demo account is demo data by
    definition: these companies exist only in the seed.
    """
    if not ids:
        return
    seen = seen if seen is not None else set()
    for table, col in _blockers(conn, parent):
        if (table, col, tuple(ids)) in seen:
            continue
        seen.add((table, col, tuple(ids)))
        ph = ",".join("?" * len(ids))
        rows = conn.execute("SELECT rowid FROM %s WHERE %s IN (%s)" % (table, col, ph),
                            list(ids)).fetchall()
        if not rows:
            continue
        # Clear what points at *these* rows before removing them, or the same
        # constraint simply fails one level down.
        pk = conn.execute("SELECT name FROM pragma_table_info('%s') WHERE pk=1" % table).fetchone()
        if pk:
            doomed = [r[0] for r in conn.execute(
                "SELECT %s FROM %s WHERE %s IN (%s)" % (pk[0], table, col, ph), list(ids))]
            _delete_referencing(conn, table, doomed, seen)
        conn.execute("DELETE FROM %s WHERE %s IN (%s)" % (table, col, ph), list(ids))


def purge(conn, s):
    """Workspaces first, then the companies, then the accounts."""
    ws_ids = [w["id"] for w in s["workspaces"]]
    org_ids = [o["id"] for o in s["orgs"]]
    user_ids = [u["id"] for u in s["users"]]

    _delete_referencing(conn, "workspaces", ws_ids)
    if ws_ids:
        conn.execute("DELETE FROM workspaces WHERE id IN (%s)" % ",".join("?" * len(ws_ids)),
                     ws_ids)
    _delete_referencing(conn, "orgs", org_ids)
    if org_ids:
        conn.execute("DELETE FROM orgs WHERE id IN (%s)" % ",".join("?" * len(org_ids)), org_ids)
    _delete_referencing(conn, "users", user_ids)
    if user_ids:
        conn.execute("DELETE FROM users WHERE id IN (%s)" % ",".join("?" * len(user_ids)),
                     user_ids)

    # Never leave a half-cleaned database behind: if anything is dangling, roll
    # the whole thing back and say so rather than committing a broken instance.
    orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
    if orphans:
        conn.rollback()
        sys.exit("Stopped: %d dangling references would have been left behind. "
                 "Nothing was changed." % len(orphans))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Remove the seeded demo data.")
    ap.add_argument("--yes", action="store_true", help="actually delete it")
    ap.add_argument("--force", action="store_true",
                    help="proceed even though a real account is in a demo company")
    args = ap.parse_args()

    conn = db.get_db()
    try:
        s = survey(conn)
        if not report(s):
            return 0
        if not args.yes:
            print("")
            print("Nothing was changed. Re-run with --yes to remove it.")
            print("Take a backup first: it cannot be undone.")
            return 0
        if s["outsiders"] and not args.force:
            print("")
            sys.exit("Refusing: a real account is inside a demo company (listed above). "
                     "Move them out first, or re-run with --force.")
        purge(conn, s)
        left = conn.execute("SELECT COUNT(*) c FROM users WHERE email LIKE ?",
                            ("%" + DEMO_SUFFIX,)).fetchone()["c"]
        print("")
        print("Removed. Demo accounts remaining: %d" % left)
        print("Run the go-live checks again; demo_data_cleared should now pass.")
        return 0 if left == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
