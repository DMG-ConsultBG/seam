# -*- coding: utf-8 -*-
"""Per-country facts: registers, VAT rates, and which signature a country
actually recognises.

This is data a business will rely on when it puts a number on an invoice or
signs a contract, so it is pinned rather than merely present: the rates and
labels below are written out in full, and changing one has to be a deliberate
edit to this file rather than a slip in company.py.

Each identifier pattern is exercised with a number of the shape that country
really issues, because a pattern that quietly rejects every Scottish company or
every Polish REGON looks like nothing until a customer cannot register.
"""
import os, sys, re
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from seamclient import Client
import company, qtsp

c = Client(); c.login()

#: country -> (standard VAT %, id label, in the EU VAT area)
PINNED = {
    "BG": (20, "ЕИК", True),
    "RO": (21, "CUI", True),
    "GR": (24, "ΑΦΜ", True),
    "IT": (22, "Partita IVA", True),
    "DE": (19, "USt-IdNr", True),
    "ES": (21, "CIF / NIF", True),
    "FR": (20, "SIREN / SIRET", True),
    "PL": (23, "NIP / REGON", True),
    "PT": (23, "NIPC / NIF", True),
    "TR": (20, "Vergi No", False),
    "RU": (22, "ИНН", False),
    "RS": (20, "PIB / Матични број", False),
    "MK": (18, "ЕМБС / ЕДБ", False),
    "UA": (20, "ЄДРПОУ / ІПН", False),
    "GB": (20, "Company No. / VAT", False),
}

#: Numbers of the shape each country actually issues, and shapes it must refuse.
SAMPLES = {
    "BG": (["205643632", "BG205643632"], ["12345", "205643631"]),
    "RO": (["14837428", "RO14837428"], ["A14837428"]),
    "GR": (["094014201", "EL094014201"], ["9401420"]),
    "IT": (["00743110157", "IT00743110157"], ["7431101"]),
    "DE": (["123456789", "DE123456789"], ["12345"]),
    "ES": (["A08015497", "ESA08015497"], ["A0801549"]),
    "FR": (["552081317", "55208131700024", "FR552081317"], ["5520"]),
    "PL": (["5260250995", "012345678", "01234567891234"], ["12345"]),
    "PT": (["500100144", "PT500100144"], ["50010014"]),
    "TR": (["1234567890", "12345678901"], ["123456789"]),
    "RU": (["7707083893", "123456789012"], ["12345"]),
    "RS": (["100000000", "12345678"], ["1234"]),
    "MK": (["1234567", "4030000000000"], ["12345"]),
    "UA": (["12345678", "123456789012"], ["1234567"]),
    # A UK company number is eight characters and may start with letters.
    "GB": (["SC123456", "OC123456", "12345678", "123456789", "GB123456789"], ["1234"]),
}

print("== every supported country is described everywhere ==")
codes = [x for x in company.supported_countries() if x != "OTHER"]
c.check("15 countries", len(codes) == 15, str(codes))
c.check("META and COUNTRIES agree",
        set(codes) == set(k for k in company.META if k != "OTHER"), str(sorted(codes)))
c.check("every country has a legal framework",
        not [x for x in codes if x not in qtsp.LEGAL],
        str([x for x in codes if x not in qtsp.LEGAL]))
c.check("every country is pinned here", set(codes) == set(PINNED),
        str(set(codes) ^ set(PINNED)))

print("\n== registers and VAT ==")
for code in sorted(codes):
    rate, label, eu = PINNED[code]
    m = company.country_meta(code)
    c.check("%s VAT %d%%" % (code, rate), m["vat_rate"] == rate, str(m["vat_rate"]))
    c.check("%s identifier is %s" % (code, label), m["id_label"] == label, m["id_label"])
    c.check("%s EU VAT area = %s" % (code, eu), bool(m["eu"]) == eu, str(m["eu"]))
    c.check("%s names its authority" % code, bool(m["authority"]), "")
c.check("rates carry a verification date",
        re.match(r"^\d{4}-\d{2}-\d{2}$", company.VAT_RATES_VERIFIED or ""),
        company.VAT_RATES_VERIFIED)

print("\n== a real company number from each country is accepted ==")
for code in sorted(codes):
    good, bad = SAMPLES[code]
    for raw in good:
        ok, norm, got = company.validate(raw, code)
        c.check("%s accepts %s" % (code, raw), ok and got == code, "%s -> %s" % (ok, got))
    for raw in bad:
        ok, _n, _g = company.validate(raw, code)
        c.check("%s refuses %s" % (code, raw), not ok, "accepted")

print("\n== a VAT prefix identifies the country on its own ==")
for raw, want in (("BG205643632", "BG"), ("EL094014201", "GR"), ("GR094014201", "GR"),
                  ("DE123456789", "DE"), ("XI123456789", "GB"), ("PL5260250995", "PL")):
    ok, _n, got = company.validate(raw, "OTHER")
    c.check("%s -> %s" % (raw, want), ok and got == want, "%s %s" % (ok, got))

print("\n== which signature each country recognises ==")
for code in sorted(codes):
    L = qtsp.LEGAL[code]
    c.check("%s cites its law" % code, bool(L.get("law")), "")
    c.check("%s names its supervisor" % code, bool(L.get("supervisor")), "")
    c.check("%s links its trusted list" % code, str(L.get("list", "")).startswith("http"), "")
    provs = qtsp.providers_for(code)
    c.check("%s has providers" % code, len(provs) > 0, "")
    for p in provs:
        c.check("%s/%s can be signed up for" % (code, p.get("key") or p.get("name")),
                str(p.get("signup", "")).startswith(("http", "mailto:")), str(p.get("signup")))
# The UK has no EU-style qualified tier, so nothing may claim one there.
c.check("GB tops out at advanced", qtsp.max_level("GB") == "advanced", qtsp.max_level("GB"))
c.check("GB says so in its own note", "no EU-style qualified tier" in
        (qtsp.LEGAL["GB"].get("note") or ""), "")
c.check("GB does not equate QES with a handwritten signature",
        qtsp.LEGAL["GB"]["qes_equals_handwritten"] is False, "")
for code in sorted(set(codes) - {"GB"}):
    c.check("%s reaches qualified" % code, qtsp.max_level(code) == "qualified",
            qtsp.max_level(code))
c.check("registry carries a verification date",
        re.match(r"^\d{4}-\d{2}-\d{2}$", qtsp.REGISTRY_VERIFIED or ""), qtsp.REGISTRY_VERIFIED)

print("\n== an unknown country is handled, not guessed ==")
m = company.country_meta("ZZ")
c.check("falls back to a neutral label", m["id_label"] == "Reg. No.", m["id_label"])
c.check("claims no VAT rate", m["vat_rate"] == 0, str(m["vat_rate"]))
c.check("claims no EU membership", not m["eu"], "")
c.check("offers no qualified tier", qtsp.max_level("ZZ") != "qualified", qtsp.max_level("ZZ"))
c.check("offers no providers", qtsp.providers_for("ZZ") == [], str(qtsp.providers_for("ZZ")))

print("\n== the API refuses a company number that is not real ==")
st, r = c.call("GET", "/api/company/countries")
if st == 200:
    got = r.get("countries") or r
    c.check("the app is told the same list", len(got) >= 15, str(got)[:120])

sys.exit(c.summary())
