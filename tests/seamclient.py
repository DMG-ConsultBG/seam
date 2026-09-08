# -*- coding: utf-8 -*-
"""Shared test client for the Seam API.

The runner starts a throwaway server on a spare port with its own database and
passes the address here. There used to be a fallback to http://127.0.0.1:5000
for running one suite by hand, and it was a trap: the suites create fixtures,
so every hand-run wrote them into the demo database. Twelve workspaces called
"Алфа Логистика 685751" ended up on the first screen after sign-in, three per
run of test_import. The runner takes a filter argument, so running one suite by
hand never needed the fallback in the first place."""
import os, sys, json, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE")
if not BASE:
    sys.exit(
        "Suites run through the runner, which starts a throwaway server on a spare\n"
        "port with its own database:\n"
        "    .venv\\Scripts\\python tests\\run_tests.py            # everything\n"
        "    .venv\\Scripts\\python tests\\run_tests.py i18n       # one suite\n\n"
        "Set SEAM_TEST_BASE yourself only if you mean to write fixtures into the\n"
        "database at that address."
    )


class Client(object):
    def __init__(self, base=BASE):
        self.base = base
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.csrf = ""
        self.ok = 0
        self.fail = 0

    def _lang(self, lang):
        c = http.cookiejar.Cookie(0, "seam_lang", lang, None, False, "127.0.0.1", False, False,
                                  "/", True, False, None, True, None, None, {})
        self.cj.set_cookie(c)

    def call(self, method, path, data=None, raw=False, lang="bg", csrf=True, binary=False):
        self._lang(lang)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        hdr = {"accept": "application/json"}
        if body:
            hdr["content-type"] = "application/json"
        if csrf and self.csrf:
            hdr["X-CSRF"] = self.csrf
        req = urllib.request.Request(self.base + path, data=body, headers=hdr, method=method)
        try:
            with self.op.open(req, timeout=30) as r:
                blob = r.read()
                if binary:                       # zip/pdf: never decode as text
                    return r.status, blob
                t = blob.decode("utf-8", "ignore")
                return r.status, (t if raw else json.loads(t or "{}"))
        except urllib.error.HTTPError as e:
            t = e.read().decode("utf-8", "ignore")
            try:
                return e.code, (t if raw else json.loads(t or "{}"))
            except ValueError:
                return e.code, t

    def login(self, email="ops@seam.demo", password="demo1234"):
        st, r = self.call("POST", "/api/auth/login", {"email": email, "password": password})
        self.csrf = r.get("csrf", "")
        return st, r

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            print("  PASS  %s" % name)
        else:
            self.fail += 1
            print("  FAIL  %s  %s" % (name, detail))
        return bool(cond)

    def summary(self):
        print("\n---- %d passed, %d failed ----" % (self.ok, self.fail))
        return 1 if self.fail else 0

    def first_order(self):
        """A workspace that has both sides and at least one record.

        Most suites need a real two-sided relationship - payouts, approvals and
        the passport all involve a counterparty. Picking simply the first
        workspace finds one that nobody has joined yet on a freshly seeded
        database, which fails in a way that looks like a product bug and is not.
        """
        st, wss = self.call("GET", "/api/workspaces")
        all_ws = (wss.get("workspaces") if isinstance(wss, dict) else wss) or []
        candidates = [w for w in all_ws if w.get("partner")] or all_ws
        for ws in candidates:
            st, ords = self.call("GET", "/api/workspaces/%d/orders" % ws["id"])
            found = ords.get("orders") if isinstance(ords, dict) else ords
            if found:
                return ws, found[0]
        ws = candidates[0]
        st, ords = self.call("GET", "/api/workspaces/%d/orders" % ws["id"])
        orders = ords.get("orders") if isinstance(ords, dict) else ords
        return ws, orders[0]
