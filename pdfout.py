# -*- coding: utf-8 -*-
"""Turning a generated document into a PDF file.

Why not a PDF library: writing PDF is easy, writing *text* in PDF is not. The
thirteen languages here need Latin, Cyrillic and Greek, which means embedding a
TrueType subset - a font file to ship, a licence to honour, and a shaping
problem to get wrong quietly. A hand-rolled PDF that renders Bulgarian as boxes
is worse than no PDF.

Why a headless browser: the machine already has one, it already renders this
exact HTML correctly, and it produces a proper PDF with the right glyphs in one
call. Chrome and Edge both expose `--print-to-pdf`, which is the same engine
behind Ctrl+P.

The page is handed over as a temporary file rather than as a URL, so the
renderer needs no session, no credentials and no network access at all. Nothing
of the document leaves the machine.
"""
import os
import sys
import time
import shutil
import tempfile
import subprocess

#: Where a browser usually is, per platform. First one that exists wins.
CANDIDATES = {
    "nt": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ],
    "posix": [
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable", "/usr/bin/microsoft-edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
}
TIMEOUT = 40
_FOUND = {"at": 0, "path": None}
CACHE_TTL = 300


def find_renderer():
    """The browser this machine will use, or None. Cached briefly: a company
    that installs Chrome should not have to restart Seam to get PDFs."""
    override = (os.environ.get("SEAM_PDF_BROWSER") or "").strip()
    if override:
        return override if os.path.exists(override) else None
    if _FOUND["path"] and time.time() - _FOUND["at"] < CACHE_TTL:
        return _FOUND["path"]
    found = None
    for raw in CANDIDATES.get("nt" if os.name == "nt" else "posix", []):
        path = os.path.expandvars(raw)
        if "%" not in path and os.path.exists(path):
            found = path
            break
    if not found:
        for name in ("chromium", "chromium-browser", "google-chrome", "msedge"):
            found = shutil.which(name)
            if found:
                break
    _FOUND.update(at=time.time(), path=found)
    return found


def available():
    return bool(find_renderer())


def render(html, timeout=TIMEOUT):
    """HTML in, PDF bytes out. Returns None when no renderer is installed, so
    the caller can fall back to the browser's own print dialogue rather than
    show an error for something that is a deployment choice."""
    browser = find_renderer()
    if not browser:
        return None
    work = tempfile.mkdtemp(prefix="seam-pdf-")
    src = os.path.join(work, "doc.html")
    out = os.path.join(work, "doc.pdf")
    try:
        with open(src, "w", encoding="utf-8") as f:
            f.write(html)
        # A separate profile directory keeps this out of the operator's own
        # browser profile, and stops a second run colliding with the first.
        args = [
            browser, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-extensions",
            "--user-data-dir=" + os.path.join(work, "profile"),
            # Both spellings on purpose. The flag was renamed part-way through
            # Chromium's headless rewrite, and the old one is silently ignored
            # by newer builds - which prints the page URL and a locale-formatted
            # date onto the sheet. On a German invoice that date arrives in the
            # operator's own language, which is exactly the mixing this product
            # does not allow.
            "--print-to-pdf-no-header",
            "--no-pdf-header-footer",
            "--print-to-pdf=" + out,
            "file:///" + src.replace("\\", "/"),
        ]
        try:
            subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        # Older builds do not accept --headless=new; try the plain flag once.
        if not os.path.exists(out):
            args[1] = "--headless"
            try:
                subprocess.run(args, capture_output=True, timeout=timeout, check=False)
            except (OSError, subprocess.SubprocessError):
                return None
        if not os.path.exists(out):
            return None
        with open(out, "rb") as f:
            data = f.read()
        return data if data[:4] == b"%PDF" else None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def self_test():
    """Render a page carrying every script the product needs, and check the
    result is a PDF that is not suspiciously small and carries no browser
    furniture. The temp path is the tell: if the footer is still on, the file
    name appears inside the file."""
    html = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Seam</title></head><body>"
            "<h1>Seam</h1><p>Фактура · Παράδειγμα · Beispiel · Örnek · Przykład · "
            "Рахунок · Fatura</p></body></html>")
    data = render(html)
    clean = bool(data) and b"seam-pdf-" not in data
    return {"renderer": find_renderer(), "ok": bool(data) and len(data) > 800 and clean,
            "clean": clean, "bytes": len(data) if data else 0}


if __name__ == "__main__":
    import json
    r = self_test()
    print(json.dumps(r, indent=1, ensure_ascii=False))
    sys.exit(0 if r["ok"] else 1)
