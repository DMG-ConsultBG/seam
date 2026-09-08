# -*- coding: utf-8 -*-
"""No screen may mix languages.

Four things have to hold, and each one broke at least once while this was
being built:

  1. every server-side translation table carries all 13 languages with the
     same keys - a missing language silently falls back to English inside an
     otherwise Bulgarian document;
  2. every error the API can raise has a translation - `errMsg()` falls back to
     the server's own message, and those are written in Bulgarian;
  3. every key the UI asks for is defined somewhere - comparing languages
     against each other passes a key defined in none of them, and `t("days")`
     printed "days" into a Bulgarian screen;
  4. a rendered page carries no writing system its language does not use.

The fourth is the empirical one: it reads the actual HTML the server sends.
"""
import os, re, io, sys, json
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from seamclient import Client

LANGS = ["en", "bg", "de", "ro", "el", "tr", "it", "ru", "es", "fr", "pl", "uk", "pt"]
CYRILLIC = ["bg", "ru", "uk"]
GREEK = ["el"]

c = Client(); c.login()

# --------------------------------------------------------------------------- #
#  1. Server-side tables
# --------------------------------------------------------------------------- #
print("== translation tables in app.py ==")
import app as A

tables = bad = 0
for name in sorted(dir(A)):
    obj = getattr(A, name)
    if not isinstance(obj, dict) or not obj or "en" not in obj:
        continue
    if not (set(obj) & set(LANGS[1:])):
        continue
    tables += 1
    miss = [l for l in LANGS if l not in obj]
    if miss:
        bad += 1
        c.check("%s has all 13 languages" % name, False, "missing %s" % miss)
        continue
    if isinstance(obj["en"], dict):
        base = set(obj["en"])
        for l in LANGS:
            if set(obj[l]) != base:
                bad += 1
                c.check("%s/%s same keys as en" % (name, l), False,
                        "missing %s extra %s" % (sorted(base - set(obj[l]))[:5],
                                                 sorted(set(obj[l]) - base)[:5]))
c.check("every table complete (%d tables)" % tables, bad == 0, "%d problems" % bad)


# --------------------------------------------------------------------------- #
#  2 and 3. The browser bundle
# --------------------------------------------------------------------------- #
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _balanced(text, start):
    depth, j = 0, start
    while j < len(text):
        ch = text[j]
        if ch in "\"'`":
            q, j = ch, j + 1
            while j < len(text) and text[j] != q:
                j += 2 if text[j] == "\\" else 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        j += 1
    return text[start:]


def _keys(text):
    """Identifiers at brace depth 1 only: "HTTP: ..." inside a message is text,
    not a key."""
    out, depth, j = set(), 0, 0
    while j < len(text):
        ch = text[j]
        if ch in "\"'`":
            q, j = ch, j + 1
            while j < len(text) and text[j] != q:
                j += 2 if text[j] == "\\" else 1
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and (ch.isalpha() or ch == "_"):
            m = IDENT.match(text, j)
            k = m.end()
            while k < len(text) and text[k] in " \t\n":
                k += 1
            if k < len(text) and text[k] == ":":
                out.add(m.group(0))
            j = m.end() - 1
        j += 1
    return out


print("\n== the browser bundle ==")
JS = os.path.join(ROOT, "static", "js")
src = io.open(os.path.join(JS, "i18n.js"), encoding="utf-8").read()
defined = {}
for m in re.finditer(r"[\s,{]([a-z]{2}):\s*\{", src):
    lang = m.group(1)
    if lang not in LANGS:
        continue
    body = _balanced(src, src.index("{", m.end() - 1))
    ks = _keys(body)
    # The container block, whose own keys are the sections rather than
    # strings: descend into `ui`, which is where the flat keys live. `addon`
    # joined `ui` and `tpl` when add-ons arrived; leaving it out here made
    # every real key look undefined, because the descent silently stopped.
    if ks <= {"ui", "tpl", "addon"}:
        um = re.search(r"\bui:\s*\{", body)
        if um:
            ks = _keys(_balanced(body, body.index("{", um.end() - 1)))
    defined.setdefault(lang, set()).update(ks)

base = defined["en"]
c.check("13 languages in the bundle", sorted(defined) == sorted(LANGS), str(sorted(defined)))
for l in LANGS:
    c.check("%s carries every key" % l, defined[l] == base,
            "missing %s" % sorted(base - defined[l])[:6])
print("     (%d keys)" % len(base))

# A duplicate top-level `const` is a SyntaxError for the whole file, and the
# whole file is the translation table: everything then renders as raw keys, or
# not at all. Nothing else here catches it, because every check above reads the
# file as text rather than running it - which is exactly how it got through.
for name in ("i18n.js", "app.js", "api.js", "money.js"):
    js = io.open(os.path.join(JS, name), encoding="utf-8").read()
    names = re.findall(r"^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", js, re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    c.check("%s declares each name once" % name, not dupes, str(dupes))

# The same failure by another route: a straight quote inside a double-quoted
# value ends the string early, and the rest of the line becomes broken syntax.
# One English sentence about “Download PDF” took the entire bundle down, and
# every textual check above still passed, because the file reads fine as text.
# A value must be followed by a comma, a closing brace, or the end of the line.
broken = []
for m in re.finditer(r'([A-Za-z_]\w*): "((?:[^"\\\n]|\\.)*)"([^,}\n]*)', src):
    tail = m.group(3).strip()
    if tail and not tail.startswith(("}", ",")):
        broken.append("%s -> %s" % (m.group(1), tail[:40]))
c.check("no value is cut short by a stray quote", not broken, str(broken[:4]))

# The translations are assembled from merge blocks that Object.assign on top of
# each other, so two blocks claiming the same key means the later one silently
# wins. Nothing breaks; the screen just says the wrong thing. The go-live
# checklist and the profile both reached for `pf_title`, and the profile ended
# up labelling a job title "Ready to go live".
#
# There used to be three named exceptions here, on the reasoning that a later
# block restating a string was sometimes deliberate. It was never worth it: the
# superseded copy of err_weak_password still promised "at least 6 characters"
# long after the server demanded eight with a letter and a digit, and nobody
# could have noticed by reading the screen, because the screen showed the other
# one. The dead statements were deleted instead, so no key is claimed twice and
# there is nothing left to allow.
DELIBERATE = set()
block_starts = [(m.start(), m.group(1)) for m in re.finditer(r"\nconst ([A-Z]\w*) = \{", src)]


def _block_at(pos):
    name = "I18N"
    for start, nm in block_starts:
        if start < pos:
            name = nm
        else:
            break
    return name


# This used to read `en: {` and take the rest of that line, which is the whole
# block for the forty merge blocks and almost nothing for the base object, whose
# `en: {` is followed by `ui: {` on the next line. So the base was invisible to
# the ownership map, and hero_title sat in the file three times per language
# while this check reported nothing. The advertisement composer read the file
# rather than the running app and printed copy from two rewrites ago.
owners = {}
for m in re.finditer(r"(?:^|[\s,{])en:\s*\{", src, re.M):
    at = src.index("{", m.end() - 1)
    body = _balanced(src, at)
    if re.match(r"\s*(ui|tpl):\s*\{", body):
        um = re.search(r"\bui:\s*\{", body)
        body = _balanced(src, src.index("{", at + um.end() - 1))
    blk = _block_at(m.start())
    for km in re.finditer(r'([A-Za-z_]\w*): "', body):
        owners.setdefault(km.group(1), set()).add(blk)
clashes = sorted("%s in %s" % (k, "+".join(sorted(v)))
                 for k, v in owners.items() if len(v) > 1 and k not in DELIBERATE)
c.check("no key is claimed by two blocks", not clashes, " | ".join(clashes[:5]))

# A count next to a noun is not a fill-in-the-blank. Interpolating blindly put
# "1 дни" on a Bulgarian screen and "1 дней" on a Russian one; a translation
# now carries its forms separated by a bar and t() picks one. These are the
# counts CLDR asks for, and a value with the wrong number silently falls back
# to its last form.
FORMS = {"ru": 3, "uk": 3, "pl": 3, "ro": 3}

# The forty merge blocks put one language on one line; the base object does not,
# it opens `bg: {` and runs for two hundred. Reading line by line therefore saw
# the merge blocks and skipped the base, which is where "{n} days ago" sat
# uninflected the whole time. Read brace-balanced bodies instead, so both are
# covered: 1452 keys rather than 1280.
_vals = {}
for m in re.finditer(r"(?:^|[\s,{])(%s):\s*\{" % "|".join(LANGS), src):
    at = src.index("{", m.end() - 1)
    body = _balanced(src, at)
    if re.match(r"\s*(ui|tpl):\s*\{", body):                 # base block: into ui
        um = re.search(r"\bui:\s*\{", body)
        body = _balanced(src, src.index("{", at + um.end() - 1))
    for km in re.finditer(r'([A-Za-z_]\w*): "((?:[^"\\]|\\.)*)"', body):
        _vals.setdefault(m.group(1), {})[km.group(1)] = km.group(2)
c.check("the sweep reads the base object too, not only the merge blocks",
        len(_vals.get("en", {})) > 1400, "en keys: %d" % len(_vals.get("en", {})))

# Nothing may pluralise by hand at the call site: `n === 1 ? t(x_one) : t(x_many)`
# is a two-form rule written in JavaScript, so it was right for English and
# wrong for Romanian at twenty-one. t() knows the rules; the branch must not
# come back.
appjs = io.open(os.path.join(JS, "app.js"), encoding="utf-8").read()
byhand = re.findall(r'\?\s*t\("(\w+_one)"\)', appjs)
c.check("no screen pluralises by hand", not byhand, str(byhand[:4]))

wrong, countless = [], []
for lang, table in _vals.items():
    want = FORMS.get(lang, 2)
    for key, val in table.items():
        if "|" not in val:
            continue
        got = len(val.split("|"))
        if got != want:
            wrong.append("%s %s: %d forms, needs %d" % (lang, key, got, want))
        # A form may spell the number as a word ("o zi", "a single code")
        # only where its bucket holds exactly one value. Russian and Ukrainian
        # put 21, 31 and 41 in the same bucket as 1, and French puts 0 there,
        # so in those three every form must print the figure: dropping it
        # announced twenty-one failures as "the last failure".
        forms = val.split("|")
        first_is_only_one = lang not in ("ru", "uk", "fr")
        for i, form in enumerate(forms):
            if "{n}" not in form and not (i == 0 and first_is_only_one):
                countless.append("%s %s" % (lang, key))
                break
c.check("every counted string has the forms its language needs", not wrong,
        " | ".join(wrong[:5]))
c.check("and no form of one has dropped its number", not countless,
        " | ".join(countless[:5]))
print("     (%d counted strings)" % sum(
    1 for t_ in _vals.values() for v in t_.values() if "|" in v))

# French and Italian glue a short word to the next with an apostrophe. Some
# strings kept it and 65 had lost it, so the same screen carried "jusqu'à" and
# "l instant". A bare l, qu, dell or dall standing alone before a vowel is
# never anything but an elision, which makes this decidable. Italian "un" is
# out on purpose: only the feminine "un'amica" takes the mark.
ELIDE = {
    "fr": re.compile(r"\b(l|d|j|n|m|t|s|c|qu|jusqu|lorsqu|puisqu|quelqu|aujourd)"
                     r" (?=[aeiouyéèêàâôîûh])", re.I),
    "it": re.compile(r"\b(l|dell|nell|all|dall|sull|quell|c|n|d|sant|bell|grand)"
                     r" (?=[aeiou])", re.I),
}
naked = []
for lang, rx in ELIDE.items():
    for key, val in _vals.get(lang, {}).items():
        if rx.search(val):
            naked.append("%s %s" % (lang, key))
for ln in io.open(os.path.join(ROOT, "app.py"), encoding="utf-8"):
    head = re.match(r'\s*"(fr|it)": \{', ln)
    if head:
        for km in re.finditer(r': "((?:[^"\\]|\\.)*)"', ln):
            if ELIDE[head.group(1)].search(km.group(1)):
                naked.append("app.py %s" % head.group(1))
c.check("no French or Italian elision has lost its apostrophe", not naked,
        " | ".join(sorted(set(naked))[:6]))

used, refs = {}, set()
for name in sorted(os.listdir(JS)):
    if not name.endswith(".js") or name == "i18n.js":
        continue
    js = io.open(os.path.join(JS, name), encoding="utf-8").read()
    # Whole keys only: t("nr_f_" + k) is a prefix, and every variant of those
    # is proven by the cross-language check above.
    for m in re.finditer(r"""\bt\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*[),]""", js):
        used.setdefault(m.group(1), set()).add(name)
    for m in re.finditer(r"""["']§([a-z][a-z0-9_]*)["']""", js):
        refs.add(m.group(1))

missing = sorted(k for k in used if k not in base)
c.check("every t(\"key\") is defined (%d used)" % len(used), not missing, str(missing[:8]))
missing = sorted(k for k in refs if k not in base)
c.check("every §key reference resolves (%d used)" % len(refs), not missing, str(missing[:8]))

# --------------------------------------------------------------------------- #
#  Grammar, as far as a machine can see it
#
#  A key can be present, in the right script, and still produce a broken
#  sentence: a placeholder dropped in translation leaves the text talking about
#  a number it never states, and two quotation styles inside one language is
#  the same kind of error as two spellings of a word.
# --------------------------------------------------------------------------- #
print("\n== the sentences themselves ==")
PLACE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _values(body):
    """key -> string value, for one language block."""
    out, depth, j = {}, 0, 0
    while j < len(body):
        ch = body[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and (ch.isalpha() or ch == "_"):
            m = IDENT.match(body, j)
            if not m:
                j += 1
                continue
            k = m.end()
            while k < len(body) and body[k] in " \t\n":
                k += 1
            if k < len(body) and body[k] == ":":
                k += 1
                while k < len(body) and body[k] in " \t\n":
                    k += 1
                if k < len(body) and body[k] in "\"'":
                    q, s = body[k], k + 1
                    e = s
                    while e < len(body) and body[e] != q:
                        e += 2 if body[e] == "\\" else 1
                    out[m.group(0)] = body[s:e]
                    j = e + 1
                    continue
            j = m.end() - 1
        elif ch in "\"'":
            q, j = ch, j + 1
            while j < len(body) and body[j] != q:
                j += 2 if body[j] == "\\" else 1
        j += 1
    return out


values = {l: {} for l in LANGS}
for m in re.finditer(r"[\s,{]([a-z]{2}):\s*\{", src):
    lang = m.group(1)
    if lang not in LANGS:
        continue
    body = _balanced(src, src.index("{", m.end() - 1))
    got = _values(body)
    if set(got) <= {"ui", "tpl"}:
        um = re.search(r"\bui:\s*\{", body)
        if um:
            got = _values(_balanced(body, body.index("{", um.end() - 1)))
    values[lang].update(got)

bad = []
for key, en_text in values["en"].items():
    want = set(PLACE.findall(en_text))
    for lang in LANGS[1:]:
        got = values[lang].get(key)
        if got is not None and set(PLACE.findall(got)) != want:
            bad.append("%s/%s wants %s" % (lang, key, sorted(want)))
c.check("every translation keeps its placeholders", not bad, str(bad[:6]))

#: The pair each language actually uses. A sentence that borrows another
#: language's quotation marks reads as a typo to anyone who speaks it.
QUOTE_STYLE = {
    "en": ("“", "”"), "tr": ("“", "”"),
    "bg": ("„", "“"), "de": ("„", "“"),
    "pl": ("„", "”"), "ro": ("„", "”"),
    "fr": ("«", "»"), "el": ("«", "»"),
    "it": ("«", "»"), "es": ("«", "»"),
    "pt": ("«", "»"), "ru": ("«", "»"),
    "uk": ("«", "»"),
}
for lang in LANGS:
    allowed = set(QUOTE_STYLE[lang])
    stray = sorted({ch for v in values[lang].values() for ch in v
                    if ch in "„“”«»"} - allowed)
    c.check("%s uses only its own quotation marks" % lang, not stray, str(stray))

# Quotation marks belong to the language, so the app may not carry any of its
# own. They were hardcoded German once, and mismatched: „…" opened one way and
# closed another, in every language.
loose_quotes = []
for name in sorted(os.listdir(JS)):
    if not name.endswith(".js") or name == "i18n.js":
        continue
    js = io.open(os.path.join(JS, name), encoding="utf-8").read()
    for n, line in enumerate(js.splitlines(), 1):
        if any(ch in line for ch in "„“”«»") and "quote_open" not in line:
            loose_quotes.append("%s:%d" % (name, n))
c.check("no quotation mark is hardcoded in the app", not loose_quotes, str(loose_quotes[:5]))
for lang in LANGS:
    pair = (values[lang].get("quote_open"), values[lang].get("quote_close"))
    c.check("%s exposes its quotation pair" % lang, pair == QUOTE_STYLE[lang], str(pair))

#: Words that are the same in every language: product and protocol names.
SAME_OK = ("WebDAV", "Nextcloud", "Seam", "CSV", "API", "IBAN", "SMS", "SEPA")
same = []
for lang in LANGS[1:]:
    for key, val in values[lang].items():
        en_text = values["en"].get(key) or ""
        if len(val.split()) < 4 or val.strip().lower() != en_text.strip().lower():
            continue
        if any(w in val for w in SAME_OK):
            continue
        same.append("%s/%s" % (lang, key))
c.check("no sentence is left in English", not same, str(same[:8]))

codes = {}
for name in os.listdir(ROOT):
    if name.endswith(".py"):
        py = io.open(os.path.join(ROOT, name), encoding="utf-8").read()
        for m in re.finditer(r"""code\s*=\s*["']([a-z0-9_]+)["']""", py):
            codes.setdefault(m.group(1), set()).add(name)
missing = sorted(x for x in codes if ("err_" + x) not in base)
c.check("every error code has a translation (%d codes)" % len(codes), not missing,
        str(missing[:8]))


# --------------------------------------------------------------------------- #
#  4. What the server actually sends
# --------------------------------------------------------------------------- #
CYR = re.compile(r"[Ѐ-ӿ]")
GRK = re.compile(r"[Ͱ-Ͽἀ-῿]")
#: Not writing in the reader's language, and not chrome either: base64 in a
#: signature, HTML entities, and `.loc` - the local designation of a foreign
#: company number, which is deliberately kept in its own script beside the
#: reader's word for it.
NOISE = re.compile(r"<code[^>]*>.*?</code>|<span class=\"loc\">.*?</span>|&#\d+;", re.S)


def script_check(label, html, lang):
    text = NOISE.sub(" ", html)
    if lang in CYRILLIC:
        c.check("%s/%s is Cyrillic" % (label, lang), bool(CYR.search(text)))
        c.check("%s/%s has no Greek" % (label, lang), not GRK.search(text),
                (GRK.search(text).group(0) if GRK.search(text) else ""))
    elif lang in GREEK:
        c.check("%s/%s is Greek" % (label, lang), bool(GRK.search(text)))
        c.check("%s/%s has no Cyrillic" % (label, lang), not CYR.search(text),
                (CYR.search(text).group(0) if CYR.search(text) else ""))
    else:
        m = CYR.search(text) or GRK.search(text)
        c.check("%s/%s is Latin only" % (label, lang), not m,
                repr(text[max(0, m.start() - 40):m.start() + 90]) if m else "")


print("\n== rendered pages, all 13 languages ==")
st, r = c.call("POST", "/api/reference", {"fields": {}})
token = r["url"].rsplit("/", 1)[-1]
for lang in LANGS:
    st, html = c.call("GET", "/ref/%s?lang=%s" % (token, lang), raw=True)
    script_check("reference", html, lang)

# A page that does not exist must also speak the reader's language.
for lang in ("de", "bg", "el"):
    st, html = c.call("GET", "/ref/nosuchtoken?lang=%s" % lang, raw=True)
    c.check("missing reference is localised (%s)" % lang, st == 404, str(st))
    script_check("reference-404", html, lang)

# No order_id on purpose: with one, the title is the record's own name, which
# is data. A document must say what was signed, not a translation of it, so a
# frozen title is correct and is checked separately below.
st, sig = c.call("POST", "/api/signatures", {
    "doc_kind": "protocol", "doc_title": "Language check", "doc_url": "/protocol/1",
    "level": "simple", "signer_name": "Test", "signer_email": "t@example.com"})
if c.check("signature request created", st in (200, 201), str(sig)[:200]):
    stoken = sig.get("token") or (sig.get("signature") or {}).get("token")
    for lang in LANGS:
        st, html = c.call("GET", "/sign/%s" % stoken, raw=True, lang=lang)
        script_check("sign page", html, lang)

print("\n== a signed document does not translate itself ==")
ws, order = c.first_order()
st, sig2 = c.call("POST", "/api/signatures", {
    "order_id": order["id"], "doc_kind": "protocol", "level": "simple",
    "signer_name": "Test", "signer_email": "t@example.com"}, lang="bg")
tok2 = sig2.get("token") or (sig2.get("signature") or {}).get("token")
st, a = c.call("GET", "/sign/%s" % tok2, raw=True, lang="bg")
st, b = c.call("GET", "/sign/%s" % tok2, raw=True, lang="de")
title = re.search(r"<b>([^<]*)</b>", a)
c.check("the title is frozen at request time",
        bool(title) and title.group(1) in b, title.group(1) if title else "")

print("\n== no sentence hardcoded outside a translation table ==")
import ast

tree = ast.parse(io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read())
safe = []                                          # line spans of language tables
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
        if "en" in keys or "bg" in keys:
            safe.append((node.lineno, node.end_lineno))
# An ApiError message never reaches the screen untranslated: the UI renders
# err_<code> and the suite above proves every code has one.
#
# The description written into `events.summary` and `notifications.body` is
# likewise internal. It is the record of what happened, written once at the
# moment it happened; both the app and the API render what the reader sees from
# kind + meta instead, which the "every kind is rendered" check below proves.
INTERNAL = {"ApiError": None, "log_event": 6, "log_system_event": 5,
            "notify_org": 5, "notify_user_row": 5}
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    fn = getattr(node.func, "id", "")
    if fn not in INTERNAL:
        continue
    idx = INTERNAL[fn]
    targets = node.args if idx is None else node.args[idx:idx + 1]
    for a in targets:
        safe.append((a.lineno, a.end_lineno))

# The list of passwords nobody may use. It contains "парола" and "пароль"
# because Bulgarian and Russian speakers pick those, and a blocklist that only
# knows the English ones protects nobody. These strings are compared against
# what a person types; not one of them is ever shown.
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and \
            any(getattr(t, "id", "") == "COMMON_PASSWORDS" for t in node.targets):
        safe.append((node.lineno, node.end_lineno))

# Column headings are matched against an uploaded file, not shown to anyone.
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) and \
            any(getattr(t, "id", "") in ("PARTNER_COLUMNS", "ORDER_COLUMNS") for t in node.targets):
        safe.append((node.lineno, node.end_lineno))

# Values sent to the Bulgarian invoicing API, which expects Bulgarian: they are
# request fields, not anything Seam puts on a screen.
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "").startswith("INVBG_") for t in node.targets):
        safe.append((node.lineno, node.end_lineno))

# An error body carrying a "code" follows the same contract as ApiError.
for node in ast.walk(tree):
    if isinstance(node, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "code" for k in node.keys):
        safe.append((node.lineno, node.end_lineno))

# Docstrings and comments describe the code, not the product.
docstrings = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if node.body and isinstance(node.body[0], ast.Expr) and \
                isinstance(node.body[0].value, ast.Constant) and \
                isinstance(node.body[0].value.value, str):
            docstrings.add(node.body[0].value.lineno)

#: "лв" is a currency symbol, like ₽ or €: it stays in its own script whoever
#: is reading. Two Cyrillic letters or more otherwise means a word.
CYR_WORD = re.compile(r"[Ѐ-ӿ]{2,}")
loose = []
for node in ast.walk(tree):
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        continue
    text = node.value.replace("лв", "")
    if not CYR_WORD.search(text):
        continue
    if node.lineno in docstrings or any(a <= node.lineno <= b for a, b in safe):
        continue
    loose.append("%d: %s" % (node.lineno, node.value[:60]))
c.check("every user-visible sentence lives in a table", not loose, " | ".join(loose[:6]))

# The internal descriptions above are only safe while nothing displays them, so
# every kind the server can write must be rendered by the app from kind + meta.
KIND_ARG = {"log_event": 5, "log_system_event": 4, "notify_org": 4, "notify_user_row": 4}
kinds = {"log_event": set(), "notify_org": set()}
for node in ast.walk(tree):
    fn = getattr(getattr(node, "func", None), "id", "")
    if not isinstance(node, ast.Call) or fn not in KIND_ARG:
        continue
    idx = KIND_ARG[fn]
    # The add belonged inside this check and was one level out, so a call that
    # passes a variable as the kind - which the agreement scanner does, because
    # it raises four kinds from one loop - reached for .value on a Name and
    # crashed the whole suite file.
    if len(node.args) > idx and isinstance(node.args[idx], ast.Constant):
        bucket = "notify_org" if fn in ("notify_org", "notify_user_row") else "log_event"
        kinds[bucket].add(node.args[idx].value)

# Kinds raised from a loop rather than written as a literal. They are still
# kinds the server writes, so they still have to be renderable on both sides;
# they are named here because the source cannot name them.
kinds["notify_org"].update(A.agreements.KINDS)
kinds["notify_org"].add("payment_received")
appjs = io.open(os.path.join(JS, "app.js"), encoding="utf-8").read()


def _cases(fn):
    i = appjs.index("function %s(" % fn)
    return set(re.findall(r"case \"([a-z_]+)\":", appjs[i:i + 4500]))


gap = kinds["log_event"] - _cases("eventText")
c.check("the app renders every event kind (%d)" % len(kinds["log_event"]), not gap, str(sorted(gap)))
gap = kinds["notify_org"] - _cases("notifText")
c.check("the app renders every notification kind (%d)" % len(kinds["notify_org"]), not gap, str(sorted(gap)))
gap = sorted(k for k in kinds["notify_org"] if k not in A.NOTIF_I18N["en"])
c.check("the server renders every notification kind", not gap, str(gap))

print("\n== errors are translatable in every language ==")
for lang in LANGS:
    st, r = c.call("POST", "/api/network/needs", {"title": ""}, lang=lang)
    c.check("error carries a code (%s)" % lang, st == 400 and r.get("code") == "no_title",
            "%s %s" % (st, r))

sys.exit(c.summary())
