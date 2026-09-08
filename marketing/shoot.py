# -*- coding: utf-8 -*-
"""Photograph the real product.

Every image this produces is the running application against the seeded demo
data - no mockups, no retouching, nothing that promises a screen the customer
will not find. If a shot looks wrong, the fix belongs in the product.

    .venv\\Scripts\\python marketing\\shoot.py            # everything
    .venv\\Scripts\\python marketing\\shoot.py --lang en  # in English
    .venv\\Scripts\\python marketing\\shoot.py --base http://127.0.0.1:5000

Output lands in marketing/shots/ as PNG, at twice the pixel density so the
images stay sharp on a phone and in print.
"""
import os
import sys
import json
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import cdp                                                  # noqa: E402

DEMO = {"email": "ops@seam.demo", "password": "demo1234"}

#: Chasing an invoice is the supplier's screen, not the buyer's: the buyer sees
#: what it owes, the supplier sees what it is owed and what has gone late. Shot
#: from the subcontractor's account so the feature is photographed from the side
#: that uses it.
SUPPLIER = {"email": "build@seam.demo", "password": "demo1234"}
SUPPLIER_SCREENS = [("receivables", "#/money", "main")]

#: Shot before signing in. These were missing, which meant the one screen a
#: stranger actually meets first was the only one never photographed.
PUBLIC = [
    ("signin",   "#/login",    ".auth-aside, form"),
    ("register", "#/register", ".auth-aside, form"),
]

#: Screen, route, and what has to be on the page before the shutter opens.
#: A shot of a spinner is worse than no shot.
SCREENS = [
    ("dashboard",    "#/",            ".card, .kpi, main"),
    ("money",        "#/money",       "main"),
    ("network",      "#/network",     "main"),
    ("analytics",    "#/analytics",   "main"),
    ("documents",    "#/docs",        "main"),
    ("templates",    "#/templates",   "main"),
    ("vehicles",     "#/vehicles",    "main"),
    ("integrations", "#/integrations", "main"),
    ("store",        "#/store",       "main"),
    ("plans",        "#/plans",       "main"),
]

#: The document shots are the ones a buyer actually cares about, because that
#: is what their customer receives.
DOCS = [
    ("invoice", "/docs/invoice?a=%(a)s&b=%(b)s&rega=203456789&vata=BG203456789"
                "&regb=HRB%%2012345&vatb=DE811234567&number=INV-20260808"
                "&desc=%(d1)s&amount=1240.50&desc=%(d2)s&amount=380&lang=%(lang)s"),
]

SIZES = [("desktop", 1280, 800, False), ("mobile", 390, 844, True)]


def login(b, base, lang, who=DEMO):
    """Sign in inside the page, so the session cookie belongs to the browser
    rather than to a script holding it at arm's length."""
    b.go(base + "/", settle=0.6)
    b.js("document.cookie = 'seam_lang=%s; path=/'" % lang)
    out = b.js("""
      (async () => {
        const me = await (await fetch('/api/me')).json().catch(() => ({}));
        if (me && me.csrf) await fetch('/api/auth/logout', {method: 'POST',
          headers: {'content-type': 'application/json', 'X-CSRF': me.csrf},
          credentials: 'same-origin', body: '{}'});
        const r = await fetch('/api/auth/login', {
          method: 'POST', headers: {'content-type': 'application/json'},
          credentials: 'same-origin',
          body: JSON.stringify(%s)});
        return r.status;
      })()""" % json.dumps(who), wait=True)
    if out != 200:
        raise SystemExit("could not sign in as the demo account (HTTP %s). "
                         "Is the server running with the seeded database?" % out)
    b.go(base + "/", settle=2.0)


def live_routes(b):
    """The workspace and order to photograph, asked of the running instance.

    Hard-coding /w/1 and /o/1 works until somebody reseeds in a different order
    and the shot silently becomes a 'not found'. This picks the first workspace
    that actually has both a partner and a record, which is the one worth
    showing anyway.
    """
    found = b.js("""
      (async () => {
        const ws = await (await fetch('/api/workspaces')).json();
        for (const w of (ws.workspaces || ws)) {
          if (!w.partner) continue;
          const r = await (await fetch('/api/workspaces/' + w.id + '/orders')).json();
          const os = r.orders || r;
          if (os && os.length) return {w: w.id, o: os[0].id};
        }
        return null;
      })()""", wait=True)
    if not found:
        return []
    return [("workspace", "#/w/%s" % found["w"], "main"),
            ("order",     "#/o/%s" % found["o"], "main")]


def capture(base, lang, outdir, only=None):
    made = []
    for label, width, height, mobile in SIZES:
        b = cdp.Browser(port=9222 + len(made), width=width, height=height)
        try:
            b.resize(width, height, mobile=mobile, scale=2)
            # Signed out first: the same browser, before it has a session.
            for name, route, needs in PUBLIC:
                if only and name not in only:
                    continue
                b.go(base + "/" + route, settle=0.5)
                b.wait_for(needs)
                b.js("document.cookie = 'seam_lang=%s; path=/'" % lang)
                b.go(base + "/" + route, settle=0.4)
                b.wait_for(needs)
                time.sleep(1.0)
                path = os.path.join(outdir, "%s-%s-%s.png" % (name, label, lang))
                size = b.shot(path, full=(label == "desktop"))
                made.append((os.path.basename(path), size))
                print("  %-34s %6.1f kB" % (os.path.basename(path), size / 1024.0))

            login(b, base, lang)
            for name, route, needs in SCREENS + live_routes(b):
                if only and name not in only:
                    continue
                b.go(base + "/" + route, settle=0.4)
                b.wait_for(needs)
                # The interface settles after its first data arrives; a short
                # extra beat is the difference between a chart and an empty box.
                time.sleep(1.4)
                path = os.path.join(outdir, "%s-%s-%s.png" % (name, label, lang))
                size = b.shot(path)
                made.append((os.path.basename(path), size))
                print("  %-34s %6.1f kB" % (os.path.basename(path), size / 1024.0))
            for name, tmpl in DOCS:
                if only and name not in only:
                    continue
                url = base + tmpl % {
                    "a": "%D0%A1%D0%B8%D0%B9%D0%BC%20%D0%95%D0%9E%D0%9E%D0%94",
                    "b": "Muster%20GmbH",
                    "d1": "%D0%9A%D0%B0%D0%B1%D0%B5%D0%BB%D0%B8%20NYM%203x2.5",
                    "d2": "%D0%9C%D0%BE%D0%BD%D1%82%D0%B0%D0%B6", "lang": lang}
                b.go(url, settle=1.0)
                path = os.path.join(outdir, "%s-%s-%s.png" % (name, label, lang))
                size = b.shot(path, full=(label == "desktop"))
                made.append((os.path.basename(path), size))
                print("  %-34s %6.1f kB" % (os.path.basename(path), size / 1024.0))

            wanted = [s for s in SUPPLIER_SCREENS if not only or s[0] in only]
            if wanted:
                login(b, base, lang, SUPPLIER)
                for name, route, needs in wanted:
                    b.go(base + "/" + route, settle=0.4)
                    b.wait_for(needs)
                    time.sleep(1.4)
                    path = os.path.join(outdir, "%s-%s-%s.png" % (name, label, lang))
                    size = b.shot(path)
                    made.append((os.path.basename(path), size))
                    print("  %-34s %6.1f kB" % (os.path.basename(path), size / 1024.0))
        finally:
            b.close()
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SEAM_BASE", "http://127.0.0.1:5000"))
    ap.add_argument("--lang", default="bg")
    ap.add_argument("--out", default=os.path.join(HERE, "shots"))
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    only = set(x.strip() for x in args.only.split(",") if x.strip())
    print("shooting %s in %s" % (args.base, args.lang))
    made = capture(args.base, args.lang, args.out, only or None)
    print("\n%d images in %s" % (len(made), args.out))
    thin = [n for n, s in made if s < 12000]
    if thin:
        # A near-empty PNG almost always means the shutter beat the data.
        print("suspiciously small, check these by eye: " + ", ".join(thin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
