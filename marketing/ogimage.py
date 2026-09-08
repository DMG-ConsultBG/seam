# -*- coding: utf-8 -*-
"""The picture that appears when somebody pastes the link.

og:image pointed at an SVG. LinkedIn, Facebook, Slack and X all decline SVG and
fall back to no image at all, so every share of this product looked like a bare
URL. This renders the same layout the advertisements use at the 1200x630 those
networks ask for, in PNG, straight into static/.

    .venv\\Scripts\\python marketing\\ogimage.py
"""
import os
import sys
import base64

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import ads                                                   # noqa: E402
import cdp                                                   # noqa: E402


def main():
    t = ads.strings("en")
    shot = ads.data_uri("dashboard-desktop-en") or ads.data_uri("dashboard-desktop-bg")
    if not shot:
        raise SystemExit("no dashboard screenshot; run marketing/shoot.py first")
    html = ads.page(1200, 630, "split", t["hero_kicker"], t["hero_title"], shot)
    out = os.path.join(ROOT, "static", "og-image.png")
    b = cdp.Browser(port=9455, width=1200, height=630)
    try:
        # Exactly 1200x630, not twice it: the networks resample anything larger
        # and several cap the file at 1 MB.
        b.resize(1200, 630, mobile=False, scale=1)
        b.go("data:text/html;charset=utf-8;base64," +
             base64.b64encode(html.encode("utf-8")).decode(), settle=0.8)
        size = b.shot(out)
    finally:
        b.close()
    print("static/og-image.png  1200 x 630  %.1f kB" % (size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
