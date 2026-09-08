# -*- coding: utf-8 -*-
"""Clear the second factor for one account, from the server itself.

`POST /api/auth/2fa/disable` needs a signed-in session, and signing in needs
the second factor. So an owner who loses the authenticator and spends all
eight recovery codes is locked out of their own installation permanently, with
the database sitting on their own disk. Preflight recommends turning 2FA on for
exactly that account, which makes the trap worse the more carefully somebody
follows the advice.

This is the way back. It is a command-line tool rather than an endpoint on
purpose: an HTTP escape hatch, even one bound to loopback, is reachable by
anything that can make a request from that host, including a vulnerable
neighbour on the same box. A file on disk needs real access to the server.

    python unlock_2fa.py owner@example.com

The account password is still required - this is a recovery path, not a
bypass - and the removal is written to the audit log, because a second factor
disappearing is exactly the kind of event nobody should be able to arrange
quietly.

    python unlock_2fa.py --list      # who has a second factor, and codes left
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db                                                    # noqa: E402
from werkzeug.security import check_password_hash            # noqa: E402


def rows(conn):
    return conn.execute(
        "SELECT u.id, u.email, u.name, "
        "  (SELECT COUNT(*) FROM user_recovery_codes r "
        "     WHERE r.user_id = u.id AND r.used_at IS NULL) AS codes_left "
        "FROM users u JOIN user_totp t ON t.user_id = u.id "
        "ORDER BY u.email").fetchall()


def show(conn):
    found = rows(conn)
    if not found:
        print("No account on this instance has a second factor.")
        return 0
    print("%-34s %-22s %s" % ("email", "name", "recovery codes left"))
    for r in found:
        print("%-34s %-22s %d" % (r["email"], (r["name"] or "")[:22], r["codes_left"]))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Clear the second factor for one account.")
    ap.add_argument("email", nargs="?", help="the account to unlock")
    ap.add_argument("--list", action="store_true", help="show who has one, then stop")
    args = ap.parse_args()

    conn = db.get_db()
    try:
        if args.list or not args.email:
            return show(conn)

        email = args.email.strip().lower()
        row = conn.execute("SELECT id, email, password_hash FROM users WHERE email=?",
                           (email,)).fetchone()
        if not row:
            # Not a login form: whoever is running this already has the disk.
            # Being vague here would only waste the operator's time.
            sys.exit("No account with that address on this instance.")
        if not conn.execute("SELECT 1 FROM user_totp WHERE user_id=?", (row["id"],)).fetchone():
            sys.exit("That account has no second factor to remove.")

        # The variable is checked before the terminal, not after: `isatty()` is
        # true inside plenty of wrappers that cannot actually deliver typed
        # input, and there the prompt waits for a keystroke that never arrives.
        # An explicit setting should win over a guess about the terminal in any
        # case.
        pw = os.environ.get("SEAM_UNLOCK_PASSWORD", "")
        if not pw:
            if not sys.stdin.isatty():
                sys.exit("No password. Set SEAM_UNLOCK_PASSWORD, or run this "
                         "where a terminal can ask for one.")
            pw = getpass.getpass("Password for %s: " % row["email"])
        if not check_password_hash(row["password_hash"], pw):
            sys.exit("Wrong password. Nothing was changed.")

        conn.execute("DELETE FROM user_totp WHERE user_id=?", (row["id"],))
        conn.execute("DELETE FROM user_recovery_codes WHERE user_id=?", (row["id"],))
        # Every session that was opened behind the old factor is cut, so a
        # stolen laptop does not survive the reset that was meant to undo it.
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (row["id"],))
        conn.execute(
            "INSERT INTO error_log (path, method, user_id, kind, message, traceback, ip, device) "
            "VALUES ('unlock_2fa.py', 'CLI', ?, 'security', ?, '', 'local', 'console')",
            (row["id"], "Second factor cleared from the server console for %s" % row["email"]))
        conn.commit()
        print("Second factor removed for %s." % row["email"])
        print("Every session for that account was signed out.")
        print("Sign in with the password alone, then set it up again from Profile.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
