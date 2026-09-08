# -*- coding: utf-8 -*-
"""
Run the whole suite against a live Seam.

Usage:
    .venv\\Scripts\\python tests\\run_tests.py            # everything
    .venv\\Scripts\\python tests\\run_tests.py banking     # only matching suites

These are integration tests on purpose: they drive the real HTTP API against a
real SQLite database, because that is where the bugs in this system have
actually been - a SQL function that does not lowercase Cyrillic, a service
worker scope, a CSRF guard that locked out the very people it was meant to let
through. Unit tests would have missed all three.

The runner starts its own server on a spare port with its own database, so it
never touches the one you are working in, and starts the mock providers the
connector suites need (a push service, an SMS gateway, an S3 target, a CSC
signing service).
"""
import os
import re
import sys
import time
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

#: Suites that need one of the mock providers listening.
FIXTURES = [("mock_sms.py", 5098), ("mock_s3.py", 5097), ("mock_csc.py", 5099),
            ("mock_oidc.py", 5096),
            # Accepts mail and delivers none, so the letters can be read back.
            # It listens on 5095 and answers questions about what it took on
            # 5094 - the port below, because 5096 is the OIDC mock.
            ("mock_smtp.py", 5095)]

ORDER = [
    "test_schema", "test_tools", "test_regression", "test_hardening", "test_ops", "test_import",
    "test_analytics", "test_analytics_cycle", "test_automations", "test_auto_doc",
    "test_approvals", "test_payouts", "test_passport", "test_vehicles",
    "test_calendar", "test_agreements", "test_banking", "test_cloud", "test_webpush", "test_notify",
    "test_qtsp", "test_csc", "test_bridge", "test_docguard", "test_network",
    "test_i18n", "test_countries", "test_auth", "test_isolation", "test_eid",
    "test_atrest", "test_rights", "test_profile", "test_profiles", "test_addons",
    "test_passwords", "test_firstday",
    # These two move the demo org between plans, and the change outlives the
    # suite, so they run after everything that assumes the seeded state.
    "test_billing", "test_entitlements", "test_pdf", "test_money", "test_files",
    # Last: it is the only suite that turns mail on, and it turns it off again
    # at the end so everything before it sees a fresh installation with none.
    "test_mail",
]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(url, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    suites = [s for s in ORDER
              if os.path.exists(os.path.join(HERE, s + ".py")) and (not only or only in s)]
    if not suites:
        print("no suites match %r" % only)
        return 1

    workdir = tempfile.mkdtemp(prefix="seam-tests-")
    db_path = os.path.join(workdir, "seam.db")
    port = free_port()
    procs = []

    env = dict(os.environ)
    env.update(SEAM_DB=db_path, PORT=str(port), PYTHONIOENCODING="utf-8",
               SEAM_TEST_BASE="http://127.0.0.1:%d" % port,
               # The picture code cannot be solved by a script, which is the
               # point of it; the suite that walks a real signup turns it off
               # here rather than weakening it in the product. The go-live
               # checks treat "off" as a blocker, and test_firstday proves it.
               SEAM_CAPTCHA="off",
               # Webhook signing secrets, so the billing suite can exercise the
               # real signature check rather than skipping it. These only make
               # the endpoints exist; nothing here reaches a payment processor.
               STRIPE_WEBHOOK_SECRET="whsec_test_only_do_not_use_anywhere",
               SEAM_LEMON_WEBHOOK_SECRET="ls_test_only_do_not_use_anywhere",
               # An instance that followed the go-live advice has this set, and
               # then every backup is sealed rather than a zip. Running without
               # it meant the suite only ever certified the configuration
               # nobody is supposed to deploy - and test_ops crashed outright
               # the first time it met a real one.
               SEAM_BACKUP_PASSPHRASE="test-only-passphrase-not-a-secret")

    print("Seam test run")
    print("  database : %s" % db_path)
    print("  port     : %d" % port)

    try:
        subprocess.check_call([PY, os.path.join(ROOT, "seed.py")], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL)
        for name, fport in FIXTURES:
            procs.append(subprocess.Popen([PY, os.path.join(HERE, "fixtures", name)],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        app = subprocess.Popen([PY, os.path.join(ROOT, "app.py")], cwd=ROOT, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        procs.append(app)
        if not wait_for("http://127.0.0.1:%d/" % port):
            err = app.stderr.read().decode("utf-8", "ignore")[-1500:] if app.stderr else ""
            print("the server did not come up:\n" + err)
            return 1

        total = failed = 0
        broken = []
        for suite in suites:
            out = subprocess.run([PY, os.path.join(HERE, suite + ".py")], cwd=HERE, env=env,
                                 capture_output=True)
            text = (out.stdout + out.stderr).decode("utf-8", "ignore")
            m = re.search(r"(\d+) passed, (\d+) failed", text)
            if m:
                p, f = int(m.group(1)), int(m.group(2))
                total += p + f
                failed += f
                mark = "ok  " if f == 0 else "FAIL"
                print("  %s %-24s %3d passed, %d failed" % (mark, suite, p, f))
                if f:
                    broken.append((suite, [l for l in text.splitlines() if "FAIL" in l][:6]))
            else:
                failed += 1
                print("  FAIL %-24s did not report - crashed?" % suite)
                broken.append((suite, text.strip().splitlines()[-8:]))

        print("\n%d checks, %d failed" % (total, failed))
        for suite, lines in broken:
            print("\n--- %s" % suite)
            for l in lines:
                print("    " + l.strip())
        return 1 if failed else 0
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(0.4)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
