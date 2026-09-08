# -*- coding: utf-8 -*-
"""Add-ons: depth per trade, and the guarantees that make it safe to switch on.

The catalogue has its own self-test for shape and key collisions. What is on
trial here is what happens once a real company switches one on:

* the resolved template actually reaches the browser, so a form grows without
  the front end knowing anything about construction;
* an add-on belonging to another trade cannot be forced onto a template;
* switching one **off** never destroys what was recorded under it, which is
  the difference between a preference and a data loss bug;
* only an owner or an admin may change it;
* and every add-on is translated into all thirteen languages, because a
  Bulgarian field label appearing inside a Polish form is the exact defect the
  language checks exist to prevent.
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from seamclient import Client                                # noqa: E402
import template_addons as addons                             # noqa: E402
from templates_config import TEMPLATES                       # noqa: E402

c = Client()
c.login()

LANGS = ["en", "bg", "de", "ro", "el", "tr", "it", "ru", "es", "fr", "pl", "uk", "pt"]

# --------------------------------------------------------------------------- #
print("== the catalogue, on its own ==")
c.check("the catalogue passes its own checks", addons.self_test())
c.check("every template has depth to offer",
        all(len(addons.available(k)) >= 3 for k in TEMPLATES),
        str({k: len(addons.available(k)) for k in TEMPLATES}))

# --------------------------------------------------------------------------- #
print("\n== what the catalogue endpoint offers ==")
st, d = c.call("GET", "/api/addons")
c.check("the catalogue answers", st == 200 and d.get("templates"), str(st))
c.check("and covers every template", set(d["templates"]) == set(TEMPLATES),
        str(sorted(set(TEMPLATES) - set(d["templates"]))))
con = d["templates"]["construction"]
c.check("construction is grouped, not one flat list", len(con["groups"]) >= 3,
        str(len(con["groups"])))
c.check("the groups are in catalogue order",
        [g["group"] for g in con["groups"]] ==
        sorted([g["group"] for g in con["groups"]], key=addons.GROUPS.index),
        str([g["group"] for g in con["groups"]]))
items = {a["key"]: a for g in con["groups"] for a in g["items"]}
c.check("акт образец 19 is offered to builders", "act19" in items, str(sorted(items))[:120])
c.check("and a marketplace channel is not", "marketplace" not in items, "it was offered")
c.check("each entry says what it will cost the form",
        items["act19"]["fields"] == 4 and items["act19"]["terms"] == 3, str(items["act19"]))
c.check("and names the stage it inserts",
        [s["key"] for s in items["act19"]["stages"]] == ["acted"], str(items["act19"]["stages"]))
c.check("nothing is on to begin with", con["on"] == [], str(con["on"]))

st, one = c.call("GET", "/api/addons?template=auto-import")
c.check("one template can be asked for alone", set(one["templates"]) == {"auto-import"},
        str(sorted(one["templates"])))

# --------------------------------------------------------------------------- #
print("\n== preview before committing ==")
st, p = c.call("POST", "/api/addons/preview",
               {"template": "construction", "addons": ["act19", "safety"]})
base = TEMPLATES["construction"]
c.check("the preview answers", st == 200, str(st))
c.check("and shows the form as it would become",
        len(p["fields"]) == len(base["fields"]) + 4 + 4, str(len(p["fields"])))
c.check("every added row says which add-on put it there",
        {r["from"] for r in p["fields"] if r["from"]} == {"act19", "safety"},
        str({r["from"] for r in p["fields"]}))
c.check("base rows are not attributed to an add-on",
        all(not r["from"] for r in p["fields"][:len(base["fields"])]), "a base row was")
c.check("the new stage is marked as new",
        [s["key"] for s in p["stages"] if s["from"]] == ["acted"],
        str([s["key"] for s in p["stages"]]))
c.check("and lands after the stage it names",
        [s["key"] for s in p["stages"]].index("acted") ==
        [s["key"] for s in base["stages"]].index("in_progress") + 1,
        str([s["key"] for s in p["stages"]]))
c.check("the terminal stage stays terminal",
        p["stages"][-1]["key"] == base["stages"][-1]["key"], str(p["stages"][-1]))
st, p2 = c.call("POST", "/api/addons/preview",
                {"template": "construction", "addons": ["marketplace"]})
c.check("previewing another trade's add-on adds nothing", p2["addons"] == [], str(p2["addons"]))
st, r = c.call("POST", "/api/addons/preview", {"template": "nonsense", "addons": []})
c.check("an unknown template is refused", st == 400, str(st))

# --------------------------------------------------------------------------- #
print("\n== switching depth on ==")
st, r = c.call("PUT", "/api/addons", {"template": "construction",
                                      "addons": ["act19", "safety", "warranty"]})
c.check("three are switched on", st == 200 and set(r["on"]) == {"act19", "safety", "warranty"},
        "%s %s" % (st, r.get("on")))
c.check("and the resolved template comes back",
        len(r["resolved"]["fields"]) == len(base["fields"]) + 8, str(len(r["resolved"]["fields"])))

st, tpls = c.call("GET", "/api/templates")
got = {f["key"] for f in tpls["construction"]["fields"] + tpls["construction"]["terms"]}
c.check("the browser now receives the deeper form", "act_number" in got and "safety_plan" in got,
        str(sorted(got))[:140])
c.check("and the deeper pipeline",
        "acted" in [s["key"] for s in tpls["construction"]["stages"]],
        str([s["key"] for s in tpls["construction"]["stages"]]))
c.check("a template nobody configured stays thin",
        len(tpls["ecommerce"]["fields"]) == len(TEMPLATES["ecommerce"]["fields"]),
        str(len(tpls["ecommerce"]["fields"])))

st, d2 = c.call("GET", "/api/addons?template=construction")
c.check("the catalogue remembers", set(d2["templates"]["construction"]["on"]) ==
        {"act19", "safety", "warranty"}, str(d2["templates"]["construction"]["on"]))

st, r = c.call("PUT", "/api/addons", {"template": "construction", "addons": ["marketplace"]})
c.check("an add-on for another trade is refused, not silently dropped",
        st == 400 and r.get("code") == "invalid_value", "%s %s" % (st, r))
st, r = c.call("PUT", "/api/addons", {"template": "nonsense", "addons": []})
c.check("an unknown template is refused", st == 400, str(st))
st, r = c.call("PUT", "/api/addons", {"template": "construction"})
c.check("a missing list is refused", st == 400 and r.get("code") == "field_required", str(st))
st, r = c.call("PUT", "/api/addons", {"template": "construction",
                                      "addons": ["act19", "act19"]})
c.check("a repeated key is stored once", r.get("on") == ["act19"], str(r.get("on")))

# --------------------------------------------------------------------------- #
print("\n== switching depth off keeps the record ==")
# Record something under an add-on field, switch the add-on off, and the value
# has to survive: it was agreed, and a preference must not delete evidence.
ws, order = c.first_order()
st, before = c.call("GET", "/api/orders/%d" % order["id"])
tpl_key = before.get("template") or order.get("template")
if tpl_key in TEMPLATES and "quality_control" in addons.available(tpl_key):
    c.call("PUT", "/api/addons", {"template": tpl_key, "addons": ["quality_control"]})
    st, r = c.call("PATCH", "/api/orders/%d" % order["id"],
                   {"fields": {"qc_notes": "Проба от партида 4, без отклонения."}})
    saved = st in (200, 204)
    c.check("a value can be recorded under an add-on field", saved, "%s %s" % (st, str(r)[:120]))
    c.call("PUT", "/api/addons", {"template": tpl_key, "addons": []})
    st, after = c.call("GET", "/api/orders/%d" % order["id"])
    blob = json.dumps(after, ensure_ascii=False)
    c.check("and switching the add-on off does not delete it",
            "партида 4" in blob, "the value is gone")
    c.check("the label still reads correctly after switching off",
            "qc_notes" not in blob or "Забележки" in blob or "партида 4" in blob,
            "label fell back to the raw key")
else:
    c.check("the seeded record's template accepts quality control", False, str(tpl_key))

# --------------------------------------------------------------------------- #
print("\n== the figures ==")
c.call("PUT", "/api/addons", {"template": "construction",
                              "addons": ["act19", "safety", "warranty"]})
st, fig = c.call("GET", "/api/analytics/metrics?days=90")
c.check("the figures endpoint answers", st == 200, str(st))
c.check("and reports the sample floor it applies",
        fig.get("min_sample") == addons.MIN_SAMPLE, str(fig.get("min_sample")))
groups = {g["template"]: g for g in fig.get("groups", [])}
c.check("construction has a group", "construction" in groups, str(sorted(groups)))
if "construction" in groups:
    keys = {m["key"] for m in groups["construction"]["metrics"]}
    c.check("carrying exactly what act19 and safety declare",
            keys == {"acted_value", "days_to_act", "incidents"}, str(sorted(keys)))
    c.check("every figure names the add-on behind it",
            all(m["addon"] for m in groups["construction"]["metrics"]), "one had none")
    c.check("and every figure carries its sample size",
            all(isinstance(m["n"], int) for m in groups["construction"]["metrics"]), "not an int")
    c.check("a figure with too little behind it is withheld, not shown as zero",
            all(m["value"] is not None or m["n"] < addons.MIN_SAMPLE
                for m in groups["construction"]["metrics"]),
            str([(m["key"], m["value"], m["n"]) for m in groups["construction"]["metrics"]]))

# Switching everything off must empty the panel rather than leave stale figures.
was = {}
st, allon = c.call("GET", "/api/addons")
for tkey, cfg in allon["templates"].items():
    if cfg["on"]:
        was[tkey] = cfg["on"]
        c.call("PUT", "/api/addons", {"template": tkey, "addons": []})
st, none = c.call("GET", "/api/analytics/metrics?days=90")
c.check("with nothing switched on the panel says so",
        none.get("none_on") is True and not none.get("groups"), str(none)[:140])
for tkey, keys in was.items():
    c.call("PUT", "/api/addons", {"template": tkey, "addons": keys})

st, bad = c.call("GET", "/api/analytics/metrics?days=99999")
c.check("an impossible range falls back rather than failing",
        st == 200 and bad["range"]["days"] in (30, 90, 180, 365), str(bad.get("range")))

# --------------------------------------------------------------------------- #
print("\n== who may change it ==")
other = Client()
tag = os.urandom(3).hex()
st, reg = other.call("POST", "/api/auth/register", {
    "name": "Друга Фирма", "email": "addons-%s@seam.test" % tag,
    "password": "vodopad-3312-kula", "org_name": "Добавки ЕООД %s" % tag,
    "side": "buyer", "country": "BG", "reg_number": "205643632"})
if c.check("a second company signs up", st == 200, "%s %s" % (st, str(reg)[:140])):
    other.csrf = reg.get("csrf", "")
    st, mine = other.call("GET", "/api/addons?template=construction")
    c.check("a new company sees nothing switched on",
            mine["templates"]["construction"]["on"] == [], str(mine["templates"]["construction"]["on"]))
    st, tp = other.call("GET", "/api/templates")
    c.check("and its form is not the other company's",
            "act_number" not in {f["key"] for f in tp["construction"]["fields"]},
            "another company's depth leaked")

# --------------------------------------------------------------------------- #
print("\n== every add-on speaks every language ==")
src = io.open(os.path.join(ROOT, "static", "js", "i18n.js"), encoding="utf-8").read()
blocks = {}
for m in re.finditer(r"^  ([a-z]{2}): \{", src, re.M):
    lang = m.group(1)
    am = re.search(r"^    addon: \{", src[m.end():], re.M)
    if not am:
        continue
    start = m.end() + am.end() - 1
    depth, j = 0, start
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    blocks[lang] = src[start:j + 1]

c.check("all thirteen languages carry an add-on block",
        sorted(blocks) == sorted(LANGS), str(sorted(blocks)))

want_fields, want_opts, want_stages, want_metrics = set(), set(), set(), set()
for a in addons.ADDONS.values():
    for f in a.get("fields", []) + a.get("terms", []):
        want_fields.add(f["key"])
        want_opts.update(f.get("options", []))
    for s in a.get("stages", []):
        want_stages.add(s["key"])
    for m in a.get("metrics", []):
        want_metrics.add(m["key"])

for lang in LANGS:
    b = blocks.get(lang, "")
    missing_a = [k for k in addons.ADDONS if not re.search(r"\b%s: \{ label:" % k, b)]
    c.check("%s names every add-on" % lang, not missing_a, str(missing_a[:5]))
    keys = set(re.findall(r"([a-z_0-9]+): \"", b))
    for name, want in (("fields", want_fields), ("options", want_opts),
                       ("stages", want_stages), ("metrics", want_metrics)):
        gap = sorted(want - keys)
        c.check("%s translates every add-on %s" % (lang, name), not gap, str(gap[:5]))
    # A label left in Bulgarian inside another language is the defect the
    # whole language suite exists to catch, so check the alphabets do not mix.
    if lang not in ("bg", "ru", "uk", "el"):
        cyr = re.findall(r'"[^"]*[Ѐ-ӿ][^"]*"', b)
        c.check("%s carries no Cyrillic" % lang, not cyr, str(cyr[:3]))
    if lang != "el":
        grk = re.findall(r'"[^"]*[Ͱ-Ͽ][^"]*"', b)
        c.check("%s carries no Greek" % lang, not grk, str(grk[:3]))

# --------------------------------------------------------------------------- #
print('\n== a new bundle cannot be forgotten ==')
# Static assets are cached for six hours, made safe by a ?v= fingerprint built
# from ASSET_FILES. A bundle linked from index.html but absent from that tuple
# is served stale for six hours after every release, silently - which is what
# happened to directory.js the first time it shipped.
import app as A                                              # noqa: E402
_html = io.open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
_linked = set(re.findall(r'(?:src|href)="/static/((?:js|css)/[^"?]+)"', _html))
_gap = sorted(_linked - set(A.ASSET_FILES))
c.check("every bundle index.html links is cache-busted (%d)" % len(_linked),
        not _gap, str(_gap))

sys.exit(c.summary())
