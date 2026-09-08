# -*- coding: utf-8 -*-
"""The shelf.

Two holes, the same shape. A document generated here was never kept: the URL
rebuilt it every time, so losing the link lost the document. And a document
that arrived from outside had nowhere to go, because `attachments` takes photos
and video - progress evidence, not paperwork. Most of what passes between two
companies is a PDF.

The guarded part is who can open what. A document sits at the company, the
relationship, or one order; the counterparty sees it only once it is shared,
and a stranger is told the same thing whether it exists or not.
"""
import os, re, sys, json, sqlite3, urllib.request, urllib.error, http.cookiejar

BASE = os.environ.get("SEAM_TEST_BASE", "http://127.0.0.1:5000")
DB = os.environ.get("SEAM_DB", "")
ok = fail = skipped = 0

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def session():
    cj = http.cookiejar.CookieJar()
    return {"op": urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)),
            "cj": cj, "csrf": ""}


S = session()


def call(method, path, data=None, raw=False, sess=None, headers=None, body_bytes=None):
    s = sess or S
    c = http.cookiejar.Cookie(0, "seam_lang", "bg", None, False, "127.0.0.1", False, False,
                              "/", True, False, None, True, None, None, {})
    s["cj"].set_cookie(c)
    b = body_bytes if body_bytes is not None else (
        json.dumps(data).encode("utf-8") if data is not None else None)
    h = {"accept": "*/*"}
    if b and body_bytes is None:
        h["content-type"] = "application/json"
    if s["csrf"]:
        h["X-CSRF"] = s["csrf"]
    h.update(headers or {})
    try:
        r = s["op"].open(urllib.request.Request(BASE + path, data=b, headers=h, method=method), timeout=60)
        t = r.read()
        return r.status, (t.decode("utf-8", "ignore") if raw else json.loads(t.decode("utf-8") or "{}")), dict(r.headers)
    except urllib.error.HTTPError as e:
        t = e.read().decode("utf-8", "ignore")
        return e.code, (t if raw else (json.loads(t) if t.startswith("{") else t)), dict(e.headers)


def multipart(fields, filename, content, field="files"):
    boundary = "----seamtest" + os.urandom(8).hex()
    out = []
    for k, v in (fields or {}).items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode("utf-8"))
    out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
                % (boundary, field, filename)).encode("utf-8"))
    out.append(content)
    out.append(("\r\n--%s--\r\n" % boundary).encode())
    return b"".join(out), {"content-type": "multipart/form-data; boundary=" + boundary}


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        fail += 1
        print("  FAIL  %s  %s" % (name, detail))


def skip(name, why):
    global skipped
    skipped += 1
    print("  SKIP  %s  (%s)" % (name, why))


st, r, _ = call("POST", "/api/auth/login", {"email": "ops@seam.demo", "password": "demo1234"})
S["csrf"] = r.get("csrf", "")
check("login", st == 200)

st, w, _ = call("GET", "/api/workspaces")
spaces = w if isinstance(w, list) else (w.get("workspaces") or [])

# The outsider is signed in first, so the workspace chosen below is one they are
# genuinely on the other side of. Picking any workspace with *a* partner is not
# the same thing, and the sharing checks would then fail for the wrong reason.
OUT = session()
st, r, _ = call("POST", "/api/auth/login", {"email": "supply@seam.demo", "password": "demo1234"},
                sess=OUT)
OUTSIDE = st == 200
if OUTSIDE:
    OUT["csrf"] = r.get("csrf", "")
    st, w2, _ = call("GET", "/api/workspaces", sess=OUT)
    theirs = {x["id"] for x in (w2 if isinstance(w2, list) else (w2.get("workspaces") or []))}
else:
    theirs = set()
shared_ws = next((x for x in spaces if x["id"] in theirs), None)
ws = shared_ws or next((x for x in spaces if (x.get("partner") or {}).get("id")), None) \
    or (spaces[0] if spaces else None)
WSID = ws["id"] if ws else None
BOTH_SIDES = bool(shared_ws)

# --------------------------------------------------------------- the shelf --
st, lst, _ = call("GET", "/api/files")
check("the shelf is readable", st == 200 and "files" in lst, str(st))
check("it says what it accepts", "pdf" in (lst.get("accepts") or []), str(lst.get("accepts"))[:120])
# The whole point: paperwork, not only photographs.
for want in ("pdf", "docx", "xlsx", "csv"):
    check("it takes .%s" % want, want in (lst.get("accepts") or []), "")

body, hdr = multipart({"title": "Рамков договор 2026", "note": "подписан"}, "dogovor.pdf", PDF)
st, up, _ = call("POST", "/api/files", headers=hdr, body_bytes=body)
check("a PDF is filed", st == 200 and (up.get("files") or []), "%s %s" % (st, str(up)[:200]))
DOC = (up.get("files") or [{}])[0]
check("with the title it was given", DOC.get("title") == "Рамков договор 2026", str(DOC.get("title")))
check("and a fingerprint of the bytes", len(DOC.get("sha256") or "") >= 16, str(DOC.get("sha256")))
check("it is private until shared", DOC.get("shared") is False, str(DOC.get("shared")))

st, blob, hh = call("GET", DOC["url"], raw=True)
check("the file comes back", st == 200, str(st))
check("as a download, not a page", "attachment" in (hh.get("Content-Disposition") or ""),
      hh.get("Content-Disposition"))
check("with sniffing off", (hh.get("X-Content-Type-Options") or "") == "nosniff", "")
check("and no privileges of its own", "sandbox" in (hh.get("Content-Security-Policy") or ""),
      hh.get("Content-Security-Policy"))

# An allow-list, so a format nobody thought about fails closed.
body, hdr = multipart(None, "payload.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
st, r, _ = call("POST", "/api/files", headers=hdr, body_bytes=body)
check("an SVG is refused", st == 400 and r.get("code") == "doc_type", "%s %s" % (st, r.get("code")))
body, hdr = multipart(None, "tool.exe", b"MZ")
st, r, _ = call("POST", "/api/files", headers=hdr, body_bytes=body)
check("an executable is refused", st == 400 and r.get("code") == "doc_type", str(st))

# The same class of hole was open on order attachments, which are served from
# our own origin to everyone in the workspace.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
img = src[src.index("IMAGE_EXT = {"):src.index("VIDEO_EXT = {")]
check("order attachments no longer take SVG either", '"svg"' not in img, img[:120])

st, r, _ = call("POST", "/api/files?shared=1", headers=multipart(None, "x.pdf", PDF)[1],
                body_bytes=multipart(None, "x.pdf", PDF)[0])
check("sharing without a relationship is refused",
      st == 400 and r.get("code") == "share_no_ws", "%s %s" % (st, r.get("code")))

# ------------------------------------------------------- the relationship --
if WSID:
    body, hdr = multipart({"title": "NDA"}, "nda.pdf", PDF)
    st, up2, _ = call("POST", "/api/files?workspace_id=%d&shared=1" % WSID, headers=hdr, body_bytes=body)
    SHARED = (up2.get("files") or [{}])[0]
    check("a document can belong to the relationship", st == 200 and SHARED.get("workspace_id") == WSID,
          "%s %s" % (st, str(up2)[:160]))
    check("and be shared with the other side", SHARED.get("shared") is True, str(SHARED.get("shared")))

    st, only, _ = call("GET", "/api/files?workspace_id=%d" % WSID)
    ids = [f["id"] for f in only.get("files") or []]
    check("the shelf can be narrowed to one relationship", SHARED["id"] in ids and DOC["id"] not in ids,
          str(ids[:8]))
else:
    SHARED = None
    skip("relationship shelf", "no workspace with a partner in the seed")

# ------------------------------------------------------------- who may see --
if OUTSIDE:
    st, _b, _h = call("GET", DOC["url"], raw=True, sess=OUT)
    # Not "forbidden": whether a document exists is itself worth not telling.
    check("another company cannot open an unshared document", st == 404, str(st))
    st, theirlist, _ = call("GET", "/api/files", sess=OUT)
    tids = [f["id"] for f in theirlist.get("files") or []]
    check("nor see it on their shelf", DOC["id"] not in tids, str(tids[:8]))
    if SHARED and BOTH_SIDES:
        check("but a shared one is on it", SHARED["id"] in tids, str(tids[:8]))
        st, _b, _h = call("GET", SHARED["url"], raw=True, sess=OUT)
        check("and can be opened", st == 200, str(st))
    else:
        skip("shared document reaches the other side",
             "the two demo accounts share no workspace")
    st, r, _ = call("DELETE", "/api/files/%d" % DOC["id"], sess=OUT)
    check("and cannot delete ours", st == 404, str(st))
else:
    skip("outsider checks", "the second demo account did not sign in")

anon = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
try:
    anon.open(BASE + DOC["url"], timeout=20)
    code = 200
except urllib.error.HTTPError as e:
    code = e.code
check("a stranger is turned away", code == 401, str(code))

# ------------------------------------------------- keeping what we generated --
INV = ("/docs/invoice?a=%D0%A1%D0%B8%D0%B9%D0%BC&b=Muster%20GmbH&number=INV-ARCH-1"
       "&desc=%D0%9A%D0%B0%D0%B1%D0%B5%D0%BB%D0%B8&qty=10&unit=%D0%BC&price=4.20"
       "&amount=42&vatrate=20&lang=bg")
st, arch, _ = call("POST", "/api/files/archive", {"path": INV, "title": "Фактура INV-ARCH-1"})
if st == 400 and arch.get("code") == "pdf_unavailable":
    skip("archiving a generated document", "no PDF renderer on this machine")
elif st == 402:
    skip("archiving a generated document", "free plan")
else:
    check("a generated document can be kept", st == 200 and arch.get("id"), "%s %s" % (st, str(arch)[:200]))
    check("it is filed as a PDF", (arch.get("mime") or "") == "application/pdf", str(arch.get("mime")))
    # A title that is all Cyrillic leaves nothing once non-ASCII is stripped,
    # and the saved file was called "-INV-ARCH-1.pdf".
    check("the saved name does not start with a dash",
          not (arch.get("name") or "").startswith("-"), str(arch.get("name")))
    check("and marked as generated, not uploaded", arch.get("source") == "generated", str(arch.get("source")))
    st, blob, hh = call("GET", arch["url"], raw=True)
    check("the kept copy opens", st == 200, str(st))
    # Kept as bytes rather than as a link that rebuilds it: a template or a
    # price list changes, and last March's invoice has to stay last March's.
    check("it really is a PDF", blob[:5] == "%PDF-", repr(blob[:8]))

st, r, _ = call("POST", "/api/files/archive", {"path": "/etc/passwd"})
check("only a document path can be archived", st == 400, str(st))
st, r, _ = call("POST", "/api/files/archive", {"path": "/docs/../../secret"})
check("and the path cannot be climbed", st == 400, str(st))

# ---------------------------------------------------------------- editing --
st, r, _ = call("PATCH", "/api/files/%d" % DOC["id"], {"title": "Рамков договор 2026 (подновен)"})
check("a filed document can be retitled", st == 200 and "подновен" in (r.get("title") or ""),
      str(r.get("title")))
st, r, _ = call("PATCH", "/api/files/%d" % DOC["id"], {"shared": True})
check("but not shared when it belongs to no relationship",
      st == 400 and r.get("code") == "share_no_ws", "%s %s" % (st, r.get("code")))

st, r, _ = call("DELETE", "/api/files/%d" % DOC["id"])
check("and removed", st == 200, str(st))
st, _b, _h = call("GET", DOC["url"], raw=True)
check("after which it is gone", st == 404, str(st))

if DB:
    con = sqlite3.connect(DB)
    n = con.execute("SELECT COUNT(*) FROM documents WHERE id=?", (DOC["id"],)).fetchone()[0]
    con.close()
    check("with no row left behind", n == 0, str(n))

# ------------------------------------------------------- the correspondence --
# It already existed and is untouched; this only confirms it is still there,
# because a shelf without the conversation around it is half a record.
if WSID:
    st, r, _ = call("POST", "/api/workspaces/%d/messages" % WSID, {"body": "Изпращам договора."})
    check("the relationship channel takes a message", st in (200, 201), str(st))
    st, msgs, _ = call("GET", "/api/workspaces/%d/messages" % WSID)
    items = msgs if isinstance(msgs, list) else (msgs.get("messages") or [])
    check("and keeps it", any("договора" in (m.get("body") or "") for m in items), str(len(items)))
else:
    skip("relationship channel", "no workspace with a partner")

print("\n---- %d passed, %d failed, %d skipped ----" % (ok, fail, skipped))
sys.exit(1 if fail else 0)
