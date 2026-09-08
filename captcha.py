# -*- coding: utf-8 -*-
"""
Self-hosted SVG CAPTCHA. No external service, no keys, works offline - which is
what fits the self-host path.

The characters are drawn as **stroke geometry**, not as text. The first version
emitted `<text>K</text>` per character, which meant the answer travelled inside
the picture that was supposed to hide it: one regular expression over the
response and any script was through. Glyph outlines cannot be read that way
without actually recognising shapes.

None of this makes a CAPTCHA strong. It raises the cost of a scripted signup;
what actually limits abuse here is the per-address rate limit on registration
and the fact that an unverified account can do nothing. Where a deployment sits
behind a VPN and wants none of it, SEAM_CAPTCHA=off turns it off - and the
go-live checks then refuse to call the instance ready.
"""
import os
import time
import random
import secrets

_STORE = {}  # id -> (answer_upper, expiry_ts)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no easily-confused chars
_TTL = 300


def enabled():
    return (os.environ.get("SEAM_CAPTCHA", "on").strip().lower()
            not in ("0", "off", "no", "false"))


def _cleanup():
    now = time.time()
    for k in [k for k, v in _STORE.items() if v[1] < now]:
        _STORE.pop(k, None)


def new_challenge():
    _cleanup()
    text = "".join(random.choice(_ALPHABET) for _ in range(5))
    cid = secrets.token_urlsafe(12)
    _STORE[cid] = (text, time.time() + _TTL)
    return cid, render_svg(text)


def check(cid, answer):
    if not enabled():
        return True
    rec = _STORE.pop(cid, None)
    if not rec:
        return False
    text, exp = rec
    if exp < time.time():
        return False
    return (answer or "").strip().upper() == text


# --------------------------------------------------------------------------- #
#  A stroke font
#
#  Each glyph is a list of polylines on a 6 wide by 10 tall grid, y downwards.
#  Hand-drawn rather than taken from a font file, because a font file is a
#  dependency and this needs 32 shapes.
# --------------------------------------------------------------------------- #
GLYPHS = {
    "A": [[(0, 10), (3, 0), (6, 10)], [(1, 6), (5, 6)]],
    "B": [[(0, 0), (0, 10)], [(0, 0), (4, 0), (5, 1), (5, 4), (4, 5), (0, 5)],
          [(0, 5), (4, 5), (5, 6), (5, 9), (4, 10), (0, 10)]],
    "C": [[(6, 2), (4, 0), (2, 0), (0, 2), (0, 8), (2, 10), (4, 10), (6, 8)]],
    "D": [[(0, 0), (0, 10)], [(0, 0), (3, 0), (5, 2), (5, 8), (3, 10), (0, 10)]],
    "E": [[(6, 0), (0, 0), (0, 10), (6, 10)], [(0, 5), (4, 5)]],
    "F": [[(6, 0), (0, 0), (0, 10)], [(0, 5), (4, 5)]],
    "G": [[(6, 2), (4, 0), (2, 0), (0, 2), (0, 8), (2, 10), (4, 10), (6, 8), (6, 5), (3, 5)]],
    "H": [[(0, 0), (0, 10)], [(6, 0), (6, 10)], [(0, 5), (6, 5)]],
    "J": [[(5, 0), (5, 8), (3, 10), (1, 10), (0, 8)]],
    "K": [[(0, 0), (0, 10)], [(6, 0), (0, 5), (6, 10)]],
    "L": [[(0, 0), (0, 10), (6, 10)]],
    "M": [[(0, 10), (0, 0), (3, 5), (6, 0), (6, 10)]],
    "N": [[(0, 10), (0, 0), (6, 10), (6, 0)]],
    "P": [[(0, 10), (0, 0), (4, 0), (6, 2), (6, 4), (4, 6), (0, 6)]],
    "Q": [[(6, 2), (4, 0), (2, 0), (0, 2), (0, 8), (2, 10), (4, 10), (6, 8), (6, 2)],
          [(4, 7), (6, 10)]],
    "R": [[(0, 10), (0, 0), (4, 0), (6, 2), (6, 4), (4, 6), (0, 6)], [(3, 6), (6, 10)]],
    "S": [[(6, 2), (4, 0), (2, 0), (0, 2), (0, 4), (6, 6), (6, 8), (4, 10), (2, 10), (0, 8)]],
    "T": [[(0, 0), (6, 0)], [(3, 0), (3, 10)]],
    "U": [[(0, 0), (0, 8), (2, 10), (4, 10), (6, 8), (6, 0)]],
    "V": [[(0, 0), (3, 10), (6, 0)]],
    "W": [[(0, 0), (1, 10), (3, 4), (5, 10), (6, 0)]],
    "X": [[(0, 0), (6, 10)], [(6, 0), (0, 10)]],
    "Y": [[(0, 0), (3, 5), (6, 0)], [(3, 5), (3, 10)]],
    "Z": [[(0, 0), (6, 0), (0, 10), (6, 10)]],
    "2": [[(0, 2), (2, 0), (4, 0), (6, 2), (6, 4), (0, 10), (6, 10)]],
    "3": [[(0, 0), (6, 0), (3, 4), (6, 6), (6, 8), (4, 10), (2, 10), (0, 8)]],
    "4": [[(4, 10), (4, 0), (0, 6), (6, 6)]],
    "5": [[(6, 0), (0, 0), (0, 4), (4, 4), (6, 6), (6, 8), (4, 10), (1, 10)]],
    "6": [[(6, 0), (2, 0), (0, 3), (0, 8), (2, 10), (4, 10), (6, 8), (6, 6), (4, 4), (1, 4)]],
    "7": [[(0, 0), (6, 0), (2, 10)]],
    "8": [[(2, 0), (0, 2), (0, 3), (6, 7), (6, 8), (4, 10), (2, 10), (0, 8), (0, 7),
           (6, 3), (6, 2), (4, 0), (2, 0)]],
    "9": [[(0, 10), (4, 10), (6, 7), (6, 2), (4, 0), (2, 0), (0, 2), (0, 4), (2, 6), (5, 6)]],
}


def render_svg(text):
    w, h = 190, 60
    colors = ["#2f6df0", "#7a4fd0", "#1f9d6b", "#c9851a", "#0e8aa3", "#b8782a"]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
             'height="%d" role="img" aria-label="captcha">' % (w, h, w, h),
             '<rect width="%d" height="%d" rx="8" fill="#f2f4f8"/>' % (w, h)]

    for _ in range(6):
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1" '
                     'opacity="0.28"/>' % (random.randint(0, w), random.randint(0, h),
                                           random.randint(0, w), random.randint(0, h),
                                           random.choice(colors)))
    for _ in range(22):
        parts.append('<circle cx="%d" cy="%d" r="1.3" fill="%s" opacity="0.3"/>'
                     % (random.randint(0, w), random.randint(0, h), random.choice(colors)))

    for i, ch in enumerate(text):
        strokes = GLYPHS.get(ch)
        if not strokes:
            continue
        scale = 3.0 + random.uniform(-0.25, 0.25)
        ox = 14 + i * 34 + random.randint(-3, 3)
        oy = 12 + random.randint(-3, 3)
        rot = random.randint(-24, 24)
        cx, cy = ox + 3 * scale, oy + 5 * scale
        col = random.choice(colors)
        for line in strokes:
            d = " ".join(("M" if n == 0 else "L") + "%.1f %.1f"
                         % (ox + x * scale, oy + y * scale)
                         for n, (x, y) in enumerate(line))
            parts.append('<path d="%s" fill="none" stroke="%s" stroke-width="2.6" '
                         'stroke-linecap="round" stroke-linejoin="round" '
                         'transform="rotate(%d %.1f %.1f)"/>' % (d, col, rot, cx, cy))

    parts.append('</svg>')
    return "".join(parts)


def self_test():
    """Two things worth proving: every character in the alphabet can be drawn,
    and the answer cannot be read back out of the drawing."""
    import re
    missing = [ch for ch in _ALPHABET if ch not in GLYPHS]
    cid, svg = new_challenge()
    answer = _STORE.get(cid, ("", 0))[0] if cid in _STORE else ""
    leaked = bool(re.search(r">[A-Z0-9]<", svg)) or (answer and answer in svg)
    return {"alphabet_complete": not missing, "missing": missing,
            "answer_not_in_picture": not leaked, "ok": not missing and not leaked}


if __name__ == "__main__":
    import json
    print(json.dumps(self_test(), indent=1))
