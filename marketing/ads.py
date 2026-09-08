# -*- coding: utf-8 -*-
"""Compose the screenshots into the sizes the ad networks actually accept.

A screenshot is not an advertisement. What runs on LinkedIn or Google Display
is a fixed-size canvas carrying one claim and one picture of the product, and
each network wants its own dimensions. This lays the shots out in those sizes
using the same headless browser that took them, so the type is the product's
own type and the claims are the ones already written and checked in i18n.js
rather than invented here.

    .venv\\Scripts\\python marketing\\ads.py            # Bulgarian
    .venv\\Scripts\\python marketing\\ads.py --lang en

Output: marketing/ads/<message>-<size>-<lang>.png, rendered at twice the
nominal pixel count. Every network accepts an image above its minimum at the
right aspect ratio, and the extra density is what keeps 11px type readable.
"""
import os
import re
import sys
import base64
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import cdp                                                   # noqa: E402

SHOTS = os.path.join(HERE, "shots")

#: name, width, height, layout. "split" puts the claim beside the screen,
#: "stack" above it, "strip" is a banner with no room for a screen at all.
SIZES = [
    ("feed",    1200, 628,  "split"),      # LinkedIn, Facebook, X
    ("square",  1080, 1080, "stack"),      # Instagram, Facebook
    ("story",   1080, 1920, "stack"),      # Stories, Reels
    ("wide",    1600, 900,  "split"),      # X card, presentations
    ("rect",    300,  250,  "strip"),      # Google Display
    ("leader",  728,  90,   "strip"),      # Google Display
    ("sky",     300,  600,  "stack"),      # Google Display
]

#: Which claim, and which photograph of the product backs it up.
#:
#: The screen is shown whole. Cropping a 16:10 desktop into a panel that is
#: nearly square was tried twice: framed by its corner it shows the navigation
#: rail and cuts the numbers off the right, and zoomed in to move it about it
#: slices the labels off the rows. A screenshot of software is not a landscape
#: photograph; there is no part of it that can be lost without losing the
#: point. It is set whole against the same dark the interface uses, so the
#: panel reads as one surface.
MESSAGES = [
    ("what",     "hero_kicker", "hero_title", "dashboard-desktop"),
    ("orders",   "pt1_t",       "pt1_s",      "order-desktop"),
    ("terms",    "pt2_t",       "pt2_s",      "workspace-desktop"),
    ("papers",   "pt3_t",       "pt3_s",      "documents-desktop"),
    ("invoices", "pt4_t",       "pt4_s",      "receivables-desktop"),
]

MARK = """<svg width="34" height="34" viewBox="0 0 32 32" fill="none"
 xmlns="http://www.w3.org/2000/svg"><rect x="1" y="1" width="30" height="30" rx="8"
 fill="#3A6FF0"/><path d="M12 9c-3 1.6-4.6 4-4.6 7s1.6 5.4 4.6 7M20 9c3 1.6 4.6 4 4.6 7
 s-1.6 5.4-4.6 7" stroke="#fff" stroke-width="2.1" stroke-linecap="round"/>
 <path d="M16 10v3M16 15v2M16 19v3" stroke="#fff" stroke-width="2.1"
 stroke-linecap="round"/></svg>"""


# --------------------------------------------------------------------------- #
def strings(lang):
    """Read the claims out of i18n.js.

    The base object spans hundreds of lines per language, so the block is taken
    by matching braces rather than by reading a line, which is the mistake that
    made an earlier sweep miss a fifth of the keys.
    """
    src = open(os.path.join(ROOT, "static", "js", "i18n.js"), encoding="utf-8").read()
    out = {}
    for m in re.finditer(r"(?:^|[\s,{])(%s):\s*\{" % lang, src, re.M):
        at = src.index("{", m.end() - 1)
        depth, j, instr = 0, at, None
        while j < len(src):
            ch = src[j]
            if instr:
                if ch == "\\":
                    j += 2
                    continue
                if ch == instr:
                    instr = None
            elif ch in "\"'`":
                instr = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        for km in re.finditer(r'([A-Za-z_]\w*): "((?:[^"\\]|\\.)*)"', src[at:j]):
            out[km.group(1)] = km.group(2)   # later blocks win, as Object.assign does
    return out


def data_uri(name):
    path = os.path.join(SHOTS, name + ".png")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def page(w, h, layout, eyebrow, claim, shot):
    """One ad, as a page the browser can photograph."""
    if layout == "strip":
        body = """
          <div class="strip">
            <div class="mark">%(mark)s</div>
            <div class="copy"><b>%(claim)s</b><span>%(eyebrow)s</span></div>
          </div>""" % {"mark": MARK, "claim": claim, "eyebrow": eyebrow}
    else:
        shot_html = ('<div class="shot" style="background-image:url(%s)"></div>'
                     % shot) if shot else ""
        body = """
          <div class="%(layout)s">
            <div class="copy">
              <div class="mark">%(mark)s</div>
              <div class="eyebrow">%(eyebrow)s</div>
              <h1>%(claim)s</h1>
              <div class="rule"></div>
              <div class="wordmark">Seam</div>
            </div>
            %(shot)s
          </div>""" % {"layout": layout, "mark": MARK, "eyebrow": eyebrow,
                       "claim": claim, "shot": shot_html}

    # Type scales with the canvas, so one stylesheet serves a banner and a story.
    unit = min(w, h)
    return """<!doctype html><html><head><meta charset="utf-8"><style>
      * { margin:0; padding:0; box-sizing:border-box; }
      html, body { width:%(w)dpx; height:%(h)dpx; overflow:hidden;
        background:#0B0E14; color:#fff;
        font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
        font-feature-settings: "kern" 1; -webkit-font-smoothing: antialiased; }
      .split, .stack { display:flex; width:100%%; height:100%%; }
      .split { flex-direction:row; align-items:center; }
      .stack { flex-direction:column; }
      .copy { padding:%(pad)dpx; display:flex; flex-direction:column;
        justify-content:center; gap:%(gap)dpx; }
      .split .copy { width:42%%; height:100%%; }
      .stack .copy { width:100%%; }
      .eyebrow { font-size:%(eb)dpx; font-weight:700; letter-spacing:.19em;
        text-transform:uppercase; color:#8FB0FF; }
      h1 { font-size:%(h1)dpx; line-height:1.16; letter-spacing:-.02em;
        font-weight:700; }
      .rule { width:%(rule)dpx; height:3px; border-radius:2px; background:#3A6FF0; }
      .wordmark { font-size:%(eb)dpx; font-weight:700; letter-spacing:.06em;
        color:#7C8698; }
      .shot { flex:1; height:100%%; overflow:hidden; position:relative; }
      .stack .shot { width:100%%; }
      .shot { background-repeat:no-repeat; background-size:contain;
        background-position:center; background-origin:content-box;
        padding:%(inset)dpx; }
      /* An edge, so the screen sits on something instead of floating in the
         dark it happens to share with the page. */
      .shot::after { content:""; position:absolute; inset:%(inset)dpx;
        border:1px solid rgba(255,255,255,.09); border-radius:12px; }
      /* The seam itself, the one mark this brand owns. */
      .split .shot::before { content:""; position:absolute; left:0; top:0; bottom:0;
        width:4px; z-index:2; border-radius:2px;
        background:repeating-linear-gradient(180deg,#3A6FF0 0 10px,transparent 10px 21px); }
      .stack .shot::before { content:""; position:absolute; left:0; right:0; top:0;
        height:4px; z-index:2; border-radius:2px;
        background:repeating-linear-gradient(90deg,#3A6FF0 0 10px,transparent 10px 21px); }
      .strip { display:flex; align-items:center; gap:%(gap)dpx; height:100%%;
        padding:0 %(pad)dpx; }
      .strip .copy { padding:0; gap:2px; }
      .strip b { font-size:%(h1)dpx; font-weight:700; display:block;
        letter-spacing:-.015em; }
      .strip span { font-size:%(eb)dpx; font-weight:700; letter-spacing:.16em;
        text-transform:uppercase; color:#8FB0FF; }
      .mark svg { display:block; width:%(mk)dpx; height:%(mk)dpx; }
    </style></head><body>%(body)s</body></html>""" % {
        "w": w, "h": h, "body": body, "inset": max(10, int(unit * 0.05)),
        "pad": max(14, int(unit * 0.075)),
        "gap": max(6, int(unit * 0.028)),
        "eb": max(8, int(unit * 0.0165)),
        "h1": max(13, int(unit * (0.052 if layout != "strip" else 0.09))),
        "rule": max(24, int(unit * 0.09)),
        "mk": max(18, int(unit * 0.05)),
    }


def build(lang, outdir):
    t = strings(lang)
    missing = [k for _, a, b, _ in MESSAGES for k in (a, b) if k not in t]
    if missing:
        raise SystemExit("i18n.js has no %s in %s" % (", ".join(missing), lang))

    made = []
    b = cdp.Browser(port=9444, width=1200, height=628)
    try:
        for name, ek, ck, shot_name in MESSAGES:
            shot = data_uri(shot_name + "-" + lang)
            if shot is None:
                print("  no screenshot %s-%s, skipping %s" % (shot_name, lang, name))
                continue
            for size, w, h, layout in SIZES:
                if layout == "strip" and name != "what":
                    continue            # a banner fits one claim, and it is the first
                claim, eyebrow = t[ck], t[ek]
                html = page(w, h, layout, eyebrow, claim,
                            shot if layout != "strip" else None)
                b.resize(w, h, mobile=False, scale=2)
                b.go("data:text/html;charset=utf-8;base64," +
                     base64.b64encode(html.encode("utf-8")).decode(), settle=0.7)
                path = os.path.join(outdir, "%s-%s-%s.png" % (name, size, lang))
                made.append((os.path.basename(path), b.shot(path)))
                print("  %-30s %5d x %-5d %6.1f kB"
                      % (os.path.basename(path), w * 2, h * 2, made[-1][1] / 1024.0))
    finally:
        b.close()
    return made


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="bg")
    ap.add_argument("--out", default=os.path.join(HERE, "ads"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    print("composing %s" % args.lang)
    made = build(args.lang, args.out)
    print("\n%d files in %s" % (len(made), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
