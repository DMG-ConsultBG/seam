# -*- coding: utf-8 -*-
"""Record a walkthrough of the running product as an animated GIF.

The story is the one the platform exists for, in order: a stranger reads what
Seam is, signs in, opens the order that is waiting on them, sees the price the
subcontractor proposed, accepts it, and the subcontractor's money screen shows
what is owed and what has gone past its date. Nothing is staged; every frame is
the real interface against the seeded demo.

    .venv\\Scripts\\python marketing\\film.py             # Bulgarian
    .venv\\Scripts\\python marketing\\film.py --lang en

Output: marketing/shots/walkthrough-<lang>.gif
"""
import os
import sys
import json
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import cdp                                                   # noqa: E402
import gif                                                   # noqa: E402

BUYER = {"email": "ops@seam.demo", "password": "demo1234"}
SUPPLIER = {"email": "build@seam.demo", "password": "demo1234"}


def sign_in(b, base, who):
    return b.js("""
      (async () => {
        const me = await (await fetch('/api/me')).json().catch(() => ({}));
        if (me && me.csrf) await fetch('/api/auth/logout', {method:'POST',
          headers:{'content-type':'application/json','X-CSRF':me.csrf},
          credentials:'same-origin', body:'{}'});
        const r = await fetch('/api/auth/login', {method:'POST',
          headers:{'content-type':'application/json'}, credentials:'same-origin',
          body: JSON.stringify(%s)});
        return r.status;
      })()""" % json.dumps(who), wait=True)


def awaiting_order(b):
    """The order that is waiting on the buyer, asked of the instance."""
    return b.js("""
      (async () => {
        const ws = await (await fetch('/api/workspaces')).json();
        for (const w of (ws.workspaces || ws)) {
          const r = await (await fetch('/api/workspaces/' + w.id + '/orders')).json();
          for (const o of (r.orders || r)) {
            const d = await (await fetch('/api/orders/' + o.id)).json();
            const terms = d.terms || [];
            if (terms.some(t => t.state === 'proposed')) return o.id;
          }
        }
        return null;
      })()""", wait=True)


def record(base, lang, out, seconds_per_beat=1.6, fps=3):
    """Beats, not frames: each step is held long enough to be read."""
    b = cdp.Browser(port=9333, width=1280, height=800)
    shots = []
    try:
        b.resize(1280, 800, mobile=False, scale=2)

        def hold(beats=1):
            for _ in range(max(1, int(beats * seconds_per_beat * fps))):
                shots.append(b.send("Page.captureScreenshot", format="png")["data"])
                time.sleep(1.0 / fps)

        b.go(base + "/", settle=0.5)
        b.js("document.cookie = 'seam_lang=%s; path=/'" % lang)

        b.go(base + "/#/login", settle=1.2)                  # 1. what Seam is
        b.wait_for(".auth-aside")
        hold(2.2)

        if sign_in(b, base, BUYER) != 200:
            raise SystemExit("could not sign in as the demo buyer")
        b.go(base + "/#/", settle=1.6)                       # 2. the buyer's day
        b.wait_for("main")
        hold(1.6)

        oid = awaiting_order(b)                              # 3. the record itself
        if oid:
            b.go(base + "/#/o/%s" % oid, settle=1.6)
            b.wait_for("main")
            hold(2.2)
            # 4. accept what the subcontractor proposed, for real
            b.js("""(() => {
              const btns = [...document.querySelectorAll('button')];
              const yes = btns.find(x => /\\u2713/.test(x.textContent));
              if (yes) yes.click();
              return !!yes;
            })()""")
            time.sleep(1.4)
            hold(1.6)

        sign_in(b, base, SUPPLIER)                           # 5. the other side
        b.go(base + "/#/money", settle=1.8)
        b.wait_for("main")
        hold(2.4)

        print("  %d frames captured" % len(shots))
        import base64
        frames, w, h = [], 0, 0
        for i, data in enumerate(shots):
            fw, fh, rgb = gif.png_rgb(base64.b64decode(data))
            w, h, rgb = gif.box_shrink(fw, fh, rgb, 4)        # 2560x1600 -> 640x400
            frames.append(rgb)
        print("  %dx%d, building palette" % (w, h))
        palette = gif.build_palette(frames[::max(1, len(frames) // 8)])
        mapper = gif.Mapper(palette)
        indexed = [mapper.frame(f) for f in frames]
        path = os.path.join(out, "walkthrough-%s.gif" % lang)
        size = gif.write(path, indexed, w, h, palette, delay_cs=int(100 / fps))
        print("  %s  %.1f MB" % (os.path.basename(path), size / 1048576.0))
        return path
    finally:
        b.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SEAM_BASE", "http://127.0.0.1:5000"))
    ap.add_argument("--lang", default="bg")
    ap.add_argument("--out", default=os.path.join(HERE, "shots"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    print("filming %s in %s" % (args.base, args.lang))
    record(args.base, args.lang, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
