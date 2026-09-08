# -*- coding: utf-8 -*-
"""Measure the three things on the launch list that cannot be asserted in code.

Accessibility, broken links and page weight are properties of the rendered
page, so they are measured in a real browser against a running instance rather
than guessed at from the source.

    .venv\\Scripts\\python tests\\audit_web.py
    .venv\\Scripts\\python tests\\audit_web.py --base http://127.0.0.1:5000

Findings are printed as FAIL (must be fixed before this is public), WARN (worth
a decision) and a measurement line. Exit code is non-zero when anything failed,
so this can gate a deploy.
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "marketing"))
sys.path.insert(0, ROOT)

import cdp                                                   # noqa: E402

DEMO = {"email": "ops@seam.demo", "password": "demo1234"}

#: Public first, then the screens behind a session. A launch audit that only
#: looks at the sign-in page measures the smallest page in the product.
#: One translated page too: hreflang and the picker are part of the
#: public surface, and a page that only passes in English is not done.
PUBLIC = ["/", "/terms", "/privacy-policy", "/faq", "/bg/terms", "/de/faq"]
PRIVATE = ["/#/", "/#/money", "/#/docs", "/#/analytics", "/#/network", "/#/plans",
           "/#/directory", "/#/messages", "/#/addons"]


# --------------------------------------------------------------------------- #
#  17. Accessibility
# --------------------------------------------------------------------------- #
A11Y_JS = r"""
(() => {
  const out = {fail: [], warn: [], counts: {}};
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const name = (el) =>
    (el.getAttribute("aria-label") || el.getAttribute("title") ||
     el.textContent || el.value || "").trim();

  // An image with no alt is invisible to a screen reader and to a search
  // engine; an empty alt is a deliberate "decorative" and is fine.
  document.querySelectorAll("img").forEach(el => {
    if (!el.hasAttribute("alt")) out.fail.push("img without alt: " + (el.src || "").slice(-60));
  });

  // A control with no accessible name is a control nobody can be told about.
  document.querySelectorAll("button, a, [role=button]").forEach(el => {
    if (!vis(el)) return;
    if (!name(el)) out.fail.push(el.tagName.toLowerCase() + " with no accessible name: " +
      (el.className || el.id || el.outerHTML.slice(0, 60)));
  });

  // Every field needs a label, whether by <label for>, wrapping, or aria.
  document.querySelectorAll("input, select, textarea").forEach(el => {
    if (!vis(el) || el.type === "hidden") return;
    const has = el.labels && el.labels.length ||
      el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") ||
      el.closest("label") || el.getAttribute("title");
    if (!has) out.fail.push("field with no label: " + (el.name || el.id || el.type));
  });

  // Contrast. WCAG AA is 4.5:1 for body text and 3:1 for large text.
  const lum = (c) => {
    const [r, g, b] = c.map(v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); });
    return .2126 * r + .7152 * g + .0722 * b;
  };
  const rgb = (s) => (s.match(/\d+(\.\d+)?/g) || []).slice(0, 3).map(Number);
  const bgOf = (el) => {
    for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      const p = rgb(c);
      if (p.length === 3 && !/rgba\(.*,\s*0\)/.test(c)) return p;
    }
    return rgb(getComputedStyle(document.body).backgroundColor) || [255, 255, 255];
  };
  let checked = 0, worst = 99;
  document.querySelectorAll("p, span, a, li, td, th, h1, h2, h3, h4, b, small, label, div").forEach(el => {
    if (!vis(el)) return;
    if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) return;
    const s = getComputedStyle(el);
    const fg = rgb(s.color), bg = bgOf(el);
    if (fg.length !== 3 || bg.length !== 3) return;
    const L1 = lum(fg), L2 = lum(bg);
    const ratio = (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05);
    const px = parseFloat(s.fontSize), bold = parseInt(s.fontWeight, 10) >= 700;
    const large = px >= 24 || (px >= 18.66 && bold);
    const need = large ? 3 : 4.5;
    checked++;
    if (ratio < worst) worst = ratio;
    if (ratio < need) out.fail.push("contrast " + ratio.toFixed(2) + ":1 (needs " +
      need + ") " + Math.round(px) + "px " + s.color + " on rgb(" + bg.join(",") + ") — \"" +
      el.textContent.trim().slice(0, 42) + "\"");
  });
  out.counts.contrast_checked = checked;
  out.counts.worst_contrast = Number(worst.toFixed(2));

  // Headings should descend without gaps: a jump from h1 to h4 tells a screen
  // reader the page has a level it does not.
  let last = 0;
  document.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach(h => {
    if (!vis(h)) return;
    const lvl = Number(h.tagName[1]);
    if (last && lvl > last + 1) out.warn.push("heading jumps h" + last + " to h" + lvl +
      ": " + h.textContent.trim().slice(0, 40));
    last = lvl;
  });
  out.counts.h1 = document.querySelectorAll("h1").length;
  if (!document.documentElement.lang) out.fail.push("<html> has no lang");

  // A keyboard user has to be able to see where they are. Probing with
  // element.focus() does not answer this: :focus-visible deliberately does not
  // match programmatic focus, so the probe reported "no ring" on a page that
  // has one. Ask the stylesheet instead.
  let ring = false;
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch (e) { continue; }   // cross-origin
    for (const r of rules || []) {
      if (r.selectorText && r.selectorText.includes(":focus-visible") &&
          (r.style.outlineWidth || r.style.outline || r.style.boxShadow)) ring = true;
    }
  }
  if (!ring) out.fail.push("no :focus-visible rule: a keyboard user cannot see where they are");
  return out;
})()
"""


# --------------------------------------------------------------------------- #
#  19. Links, 20. Weight
# --------------------------------------------------------------------------- #
LINKS_JS = r"""
(() => {
  const seen = new Set();
  document.querySelectorAll("a[href]").forEach(a => {
    const h = a.getAttribute("href");
    if (!h || h.startsWith("#") || h.startsWith("mailto:") || h.startsWith("tel:")) return;
    try { seen.add(new URL(h, location.href).href); } catch (e) {}
  });
  document.querySelectorAll("img[src], script[src], link[href]").forEach(el => {
    const h = el.getAttribute("src") || el.getAttribute("href");
    if (!h || h.startsWith("data:")) return;
    try { seen.add(new URL(h, location.href).href); } catch (e) {}
  });
  return [...seen];
})()
"""

WEIGHT_JS = r"""
(() => {
  const nav = performance.getEntriesByType("navigation")[0] || {};
  const res = performance.getEntriesByType("resource");
  const bytes = res.reduce((n, r) => n + (r.encodedBodySize || 0), 0) +
    (nav.encodedBodySize || 0);
  const uncompressed = res.filter(r =>
    r.encodedBodySize && r.decodedBodySize &&
    r.encodedBodySize >= r.decodedBodySize && r.decodedBodySize > 24000)
    .map(r => r.name.split("/").pop().split("?")[0] + " " +
      Math.round(r.decodedBodySize / 1024) + " kB");
  const paint = performance.getEntriesByType("paint")
    .find(p => p.name === "first-contentful-paint");
  return {
    requests: res.length + 1,
    bytes: bytes,
    dom_nodes: document.getElementsByTagName("*").length,
    fcp_ms: paint ? Math.round(paint.startTime) : null,
    dom_ready_ms: nav.domContentLoadedEventEnd ? Math.round(nav.domContentLoadedEventEnd) : null,
    uncompressed: uncompressed,
  };
})()
"""


def audit(base, sign_in=True, theme="dark"):
    fails, warns, notes = [], [], []
    b = cdp.Browser(port=9466, width=1366, height=900)
    links = set()
    try:
        b.resize(1366, 900, mobile=False, scale=1)
        # Both themes are shipped, so both are audited. A palette that passes
        # in the dark and fails in the light is half a palette.
        b.go(base + "/", settle=0.4)
        b.js("localStorage.setItem('seam_theme', %r)" % theme)
        pages = list(PUBLIC)
        if sign_in:
            b.go(base + "/", settle=0.6)
            ok = b.js("""
              (async () => {
                const r = await fetch('/api/auth/login', {method:'POST',
                  headers:{'content-type':'application/json'}, credentials:'same-origin',
                  body: JSON.stringify(%s)});
                return r.status;
              })()""" % json.dumps(DEMO), wait=True)
            if ok == 200:
                pages += PRIVATE
            else:
                warns.append("could not sign in (HTTP %s): private screens not audited" % ok)

        for path in pages:
            b.go(base + path, settle=1.6)
            b.wait_for("main, .auth-wrap, body")
            a11y = b.js(A11Y_JS)
            for f in a11y["fail"]:
                fails.append("%s  %s" % (path, f))
            for w in a11y["warn"]:
                warns.append("%s  %s" % (path, w))
            weight = b.js(WEIGHT_JS)
            notes.append((path, a11y["counts"], weight))
            for u in weight["uncompressed"]:
                warns.append("%s  served uncompressed: %s" % (path, u))
            links.update(b.js(LINKS_JS))

        # 19: every link and asset the pages point at, actually fetched.
        broken = b.js("""
          (async () => {
            const out = [];
            for (const u of %s) {
              if (!u.startsWith(location.origin)) continue;   // outside: not ours to fix
              try {
                const r = await fetch(u, {method: 'GET', credentials: 'same-origin'});
                if (!r.ok) out.push(r.status + '  ' + u);
              } catch (e) { out.push('ERR ' + u); }
            }
            return out;
          })()""" % json.dumps(sorted(links)), wait=True)
        for row in broken:
            fails.append("broken link  %s" % row)
    finally:
        b.close()
    return fails, warns, notes, len(links)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("SEAM_BASE", "http://127.0.0.1:5000"))
    ap.add_argument("--no-login", action="store_true")
    ap.add_argument("--theme", default="dark,light",
                    help="which themes to audit; both by default")
    args = ap.parse_args()

    total_f = total_w = 0
    for theme in [t.strip() for t in args.theme.split(",") if t.strip()]:
        print("auditing %s in the %s theme\n" % (args.base, theme))
        fails, warns, notes, nlinks = audit(args.base, not args.no_login, theme)
        report(theme, fails, warns, notes, nlinks)
        total_f, total_w = total_f + len(fails), total_w + len(warns)
    print("%d failed, %d warnings across every theme" % (total_f, total_w))
    return 1 if total_f else 0


def report(theme, fails, warns, notes, nlinks):
    print("== 20. weight and speed (%s) ==" % theme)
    print("   %-16s %8s %10s %8s %8s %8s" %
          ("page", "requests", "bytes", "DOM", "FCP ms", "worst ratio"))
    for path, counts, w in notes:
        print("   %-16s %8d %10d %8d %8s %8s"
              % (path, w["requests"], w["bytes"], w["dom_nodes"],
                 w["fcp_ms"] if w["fcp_ms"] is not None else "-",
                 counts.get("worst_contrast", "-")))
    print("\n== 19. links ==\n   %d distinct URLs followed" % nlinks)

    print("\n== findings ==")
    for f in fails:
        print("   FAIL  %s" % f)
    for w in warns:
        print("   WARN  %s" % w)
    if not fails and not warns:
        print("   nothing to report")
    print("")


if __name__ == "__main__":
    sys.exit(main())
