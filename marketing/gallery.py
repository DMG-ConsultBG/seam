# -*- coding: utf-8 -*-
"""Build the one-page gallery a buyer can be sent a link to.

The fifteen listing screenshots are inlined as data URIs, so the page is a
single file that works from anywhere: a marketplace listing, an e-mail, a USB
stick. Nothing is fetched at view time, which also means nothing can rot.

    .venv\\Scripts\\python marketing\\gallery.py

Writes it twice: marketing/gallery.html, to open or send as a file, and
docs/index.html, which is the directory GitHub Pages serves a site from. One
generator rather than two copies to keep in step. Re-run it after re-shooting;
the captions live here, next to the order they are read in.
"""
import base64
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOTS = os.path.join(HERE, "listing")
OUT = os.path.join(HERE, "gallery.html")
PAGES = os.path.join(ROOT, "docs", "index.html")

#: Number, file, name, and what the screen proves. The order is the argument:
#: a record is opened, worked, measured, papered, signed, invoiced, chased and
#: accounted for. The numbering is that sequence, not decoration.
SCREENS = [
    ("01-dashboard.png", "Dashboard",
     "Every partnership on one screen, with whatever is waiting on you at the top."),
    ("02-order.png", "The shared record",
     "One record, not two. Both companies read and change the same fields, and every "
     "change is written into the log with who made it and when."),
    ("03-workspace.png", "Workspace",
     "One relationship and everything inside it: records, messages, documents and the "
     "audit trail the two sides share."),
    ("04-analytics.png", "Analytics",
     "Cycle time, completion rate and agreed value, computed from the audit trail "
     "rather than typed in again by somebody."),
    ("05-profiles.png", "Company directory",
     "Companies looking for work and companies offering it, each showing only what it "
     "chose to publish."),
    ("06-assistant.png", "Assistant",
     "Answers about your own orders, statuses and agreed terms. It sees your data and "
     "no one else's, and the provider key belongs to the company."),
    ("07-documents.png", "Documents",
     "Offers, delivery notes, protocols and invoices, generated from the record "
     "instead of retyped into a word processor."),
    ("08-signing.png", "Electronic signature",
     "The signing page a counterparty opens without an account, carrying a SHA-256 "
     "fingerprint of the exact document being signed."),
    ("09-invoice.png", "Invoice",
     "What the customer actually receives. A real PDF, in thirteen languages, with the "
     "registration and VAT fields each country expects."),
    ("10-receivables.png", "Receivables",
     "The invoice chases itself, reconciles against the bank statement, and turns how "
     "fast a company pays into a measurement rather than a claim."),
    ("11-finance.png", "Taxes and profit",
     "Revenue, costs, VAT and profit before tax, on the rates published for the "
     "company's own country."),
    ("12-templates.png", "Templates",
     "The relationship types: the fields, terms and stages a given trade actually "
     "works with."),
    ("13-addons.png", "Add-ons",
     "Twenty-eight opt-in packs that add the depth one trade needs without imposing it "
     "on every other."),
    ("14-plans.png", "Plans",
     "Six tiers, and any single part of the platform buyable on its own."),
    ("15-language.png", "Thirteen languages",
     "Every language complete, with none of the half-translated screens that make "
     "software feel foreign in its own market."),
]

FACTS = [
    ("13", "languages"),
    ("28", "trade packs"),
    ("2", "dependencies"),
    ("2,204", "tests passing"),
]

# The logo, drawn here rather than linked: two sides facing each other and the
# stitched line down the middle where they meet.
MARK = """<svg class="mark" viewBox="0 0 256 256" aria-hidden="true">
  <rect width="256" height="256" rx="58" fill="var(--brand)"/>
  <g stroke="var(--brand-pale)" stroke-width="15" fill="none"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M96 68 H74 a22 22 0 0 0 -22 22 V166 a22 22 0 0 0 22 22 H96"/>
    <path d="M160 68 H182 a22 22 0 0 1 22 22 V166 a22 22 0 0 1 -22 22 H160"/>
  </g>
  <path d="M128 44 V212" stroke="#ffffff" stroke-width="16" stroke-linecap="round"
        stroke-dasharray="22 24"/>
</svg>"""

CSS = """
:root {
  /* Neutrals carry the logo's blue, so the greys read as chosen rather than
     inherited from a stylesheet default. */
  --ground:      #eef0f5;
  --surface:     #ffffff;
  --surface-sunk:#e4e7ef;
  --ink:         #12161f;
  --ink-soft:    #4a5265;
  --ink-faint:   #757e93;
  --rule:        #d2d7e3;
  --brand:       #2552c8;
  --brand-lift:  #3f7bff;
  --brand-pale:  #cfe0ff;
  --shot-edge:   #c3cad9;
  --shot-shadow: 0 1px 2px rgba(18,22,31,.10), 0 12px 34px rgba(18,22,31,.14);
  --seam:        #c3cad9;

  --display: "Archivo", "Helvetica Neue", Arial, sans-serif;
  --body: "IBM Plex Sans", "Segoe UI", system-ui, sans-serif;
  --data: "IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ground:      #0c0f16;
    --surface:     #151a24;
    --surface-sunk:#101520;
    --ink:         #eef1f7;
    --ink-soft:    #a8b1c4;
    --ink-faint:   #7a8398;
    --rule:        #262d3b;
    --brand:       #3f7bff;
    --brand-lift:  #6f9bff;
    --brand-pale:  #cfe0ff;
    --shot-edge:   #2b3346;
    --shot-shadow: 0 1px 2px rgba(0,0,0,.5), 0 14px 40px rgba(0,0,0,.45);
    --seam:        #2b3346;
  }
}

:root[data-theme="dark"] {
  --ground:      #0c0f16;
  --surface:     #151a24;
  --surface-sunk:#101520;
  --ink:         #eef1f7;
  --ink-soft:    #a8b1c4;
  --ink-faint:   #7a8398;
  --rule:        #262d3b;
  --brand:       #3f7bff;
  --brand-lift:  #6f9bff;
  --brand-pale:  #cfe0ff;
  --shot-edge:   #2b3346;
  --shot-shadow: 0 1px 2px rgba(0,0,0,.5), 0 14px 40px rgba(0,0,0,.45);
  --seam:        #2b3346;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--ground);
  color: var(--ink);
  font-family: var(--body);
  font-size: 16px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

.wrap { max-width: 1180px; margin: 0 auto; padding: 0 28px 96px; }

/* ---- masthead --------------------------------------------------------- */
.masthead {
  display: flex; flex-direction: column; gap: 30px;
  padding: 60px 0 40px;
  border-bottom: 1px solid var(--rule);
}
.brandline { display: flex; align-items: center; gap: 16px; }
.mark { width: 46px; height: 46px; border-radius: 11px; flex: none; display: block; }
.wordmark {
  font-family: var(--display);
  font-weight: 800;
  font-size: 30px;
  letter-spacing: -.02em;
  line-height: 1;
  margin: 0;
}
.wordmark span { display: block; font-family: var(--body); font-weight: 400;
  font-size: 12.5px; letter-spacing: .13em; text-transform: uppercase;
  color: var(--ink-faint); margin-top: 6px; }

.lede {
  font-family: var(--display);
  font-weight: 700;
  font-size: clamp(30px, 4.4vw, 50px);
  line-height: 1.1;
  letter-spacing: -.025em;
  text-wrap: balance;
  margin: 0;
  max-width: 20ch;
}
.lede em { font-style: normal; color: var(--brand); }
.standfirst { margin: 0; max-width: 62ch; color: var(--ink-soft); font-size: 17.5px; }

.facts {
  display: flex; flex-wrap: wrap; gap: 12px;
  list-style: none; margin: 0; padding: 0;
}
.facts li {
  display: flex; align-items: baseline; gap: 8px;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 3px;
  padding: 9px 14px;
}
.facts b {
  font-family: var(--data); font-weight: 600; font-size: 16px;
  font-variant-numeric: tabular-nums; color: var(--ink);
}
.facts span { font-size: 13px; color: var(--ink-faint); letter-spacing: .01em; }

.note {
  margin: 0; font-size: 13.5px; color: var(--ink-faint); max-width: 68ch;
}

/* ---- the seam --------------------------------------------------------- */
/* A dashed rule down the left of the gallery, and every screen a stitch on
   it. The mark is a running stitch holding two sides together; this is the
   same line, at page length. */
.gallery { position: relative; padding: 56px 0 0 84px; }
.gallery::before {
  content: ""; position: absolute; left: 25px; top: 74px; bottom: 40px;
  width: 2px;
  background: repeating-linear-gradient(
    to bottom, var(--seam) 0 11px, transparent 11px 23px);
}

.shot { position: relative; padding-bottom: 76px; }
.stitch {
  position: absolute; left: -84px; top: 2px;
  width: 52px; height: 52px; border-radius: 50%;
  display: grid; place-items: center;
  background: var(--surface);
  border: 1px solid var(--rule);
  font-family: var(--data); font-weight: 600; font-size: 14px;
  font-variant-numeric: tabular-nums;
  color: var(--brand);
}
.shot h2 {
  font-family: var(--display); font-weight: 700;
  font-size: 25px; letter-spacing: -.015em; line-height: 1.2;
  margin: 8px 0 8px; text-wrap: balance;
}
.shot p { margin: 0 0 20px; color: var(--ink-soft); max-width: 66ch; }

.frame {
  display: block; width: 100%; padding: 0; border: 0; cursor: zoom-in;
  background: var(--surface-sunk);
  border: 1px solid var(--shot-edge);
  border-radius: 5px;
  overflow: hidden;
  box-shadow: var(--shot-shadow);
  transition: transform .18s ease, box-shadow .18s ease;
}
.frame img { display: block; width: 100%; height: auto; }
.frame:hover { transform: translateY(-2px); }
.frame:focus-visible { outline: 3px solid var(--brand-lift); outline-offset: 3px; }

/* ---- full size -------------------------------------------------------- */
dialog.viewer {
  padding: 0; border: 0; background: transparent;
  max-width: 96vw; max-height: 94vh;
}
dialog.viewer::backdrop { background: rgba(8, 11, 17, .88); }
dialog.viewer img {
  display: block; max-width: 96vw; max-height: 88vh;
  width: auto; height: auto; border-radius: 4px;
}
.viewer-bar {
  display: flex; justify-content: space-between; align-items: center; gap: 20px;
  padding: 10px 2px 12px; color: #e8ecf5;
  font-family: var(--data); font-size: 12.5px; letter-spacing: .04em;
}
.viewer-bar button {
  font: inherit; color: #e8ecf5; background: transparent;
  border: 1px solid rgba(232,236,245,.4); border-radius: 3px;
  padding: 5px 12px; cursor: pointer;
}
.viewer-bar button:hover { border-color: #e8ecf5; }

/* ---- close ------------------------------------------------------------ */
.colophon {
  margin-top: 28px; padding-top: 26px; border-top: 1px solid var(--rule);
  display: flex; flex-wrap: wrap; gap: 10px 28px;
  font-size: 13.5px; color: var(--ink-faint);
}
.colophon b { font-weight: 500; color: var(--ink-soft); }

@media (max-width: 760px) {
  .wrap { padding: 0 18px 64px; }
  .gallery { padding-left: 0; }
  .gallery::before { display: none; }
  .stitch { position: static; margin-bottom: 10px; }
  .masthead { padding-top: 40px; }
}

@media (prefers-reduced-motion: reduce) {
  .frame { transition: none; }
  .frame:hover { transform: none; }
}
"""

JS = """
const viewer = document.getElementById('viewer');
const big = document.getElementById('big');
const cap = document.getElementById('cap');
document.querySelectorAll('.frame').forEach((btn) => {
  btn.addEventListener('click', () => {
    const img = btn.querySelector('img');
    big.src = img.src;
    big.alt = img.alt;
    cap.textContent = btn.dataset.cap;
    viewer.showModal();
  });
});
document.getElementById('shut').addEventListener('click', () => viewer.close());
viewer.addEventListener('click', (e) => { if (e.target === viewer) viewer.close(); });
viewer.addEventListener('close', () => { big.removeAttribute('src'); });
"""


def data_uri(path):
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def build():
    facts = "".join('<li><b>%s</b><span>%s</span></li>' % (n, w) for n, w in FACTS)

    shots = []
    for i, (fname, title, blurb) in enumerate(SCREENS, 1):
        path = os.path.join(SHOTS, fname)
        if not os.path.exists(path):
            raise SystemExit("missing screenshot: %s" % path)
        alt = "Seam, %s screen" % title.lower()
        shots.append(
            '<article class="shot">'
            '<div class="stitch">%02d</div>'
            '<h2>%s</h2><p>%s</p>'
            '<button class="frame" data-cap="%02d &middot; %s" '
            'aria-label="Open %s at full size">'
            '<img src="%s" alt="%s" width="2560" height="1600" loading="lazy" />'
            '</button></article>'
            % (i, title, blurb, i, title, title, data_uri(path), alt))

    return """<title>Seam Screens</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?\
family=Archivo:wght@700;800&\
family=IBM+Plex+Mono:wght@500;600&\
family=IBM+Plex+Sans:wght@400;500&display=swap" />
<style>%s</style>

<div class="wrap">
  <header class="masthead">
    <div class="brandline">
      %s
      <h1 class="wordmark">Seam<span>In sync with every partner</span></h1>
    </div>
    <p class="lede">Both companies are looking at <em>the same record</em>.</p>
    <p class="standfirst">Seam is a self-hosted operational layer for two companies that
      work together. One shared record instead of an e-mail thread on each side: the same
      fields, the same status, the same agreed terms, and one audit trail neither party can
      quietly edit. Fifteen screens, photographed from the running application.</p>
    <ul class="facts">%s</ul>
    <p class="note">No mockups. Every image below is the software running against its demo
      data, at 2560 &times; 1600. Click any screen to open it full size.</p>
  </header>

  <main class="gallery">%s</main>

  <footer class="colophon">
    <span><b>Stack</b> Python and SQLite, two dependencies, no build step</span>
    <span><b>Runs on</b> one machine you control</span>
    <span><b>Interface</b> 13 languages, light and dark</span>
  </footer>
</div>

<dialog class="viewer" id="viewer">
  <div class="viewer-bar">
    <span id="cap"></span>
    <button type="button" id="shut">Close</button>
  </div>
  <img id="big" alt="" />
</dialog>

<script>%s</script>
""" % (CSS, MARK, facts, "".join(shots), JS)


if __name__ == "__main__":
    html = build()
    for path in (OUT, PAGES):
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print("%s  %.1f MB" % (path, os.path.getsize(path) / (1024.0 * 1024.0)))
