# -*- coding: utf-8 -*-
"""
Per-country company-number validation.

EIK/Bulstat is Bulgaria-only; every country has its own company / VAT number
format (CUI in Romania, ΑΦΜ in Greece, Partita IVA in Italy, USt-IdNr in
Germany, VKN in Turkey, ИНН in Russia, PIB in Serbia ...). So validation is
driven by a per-country registry: each entry has the accepted format and, where
we have a reliable algorithm, a checksum. Unknown countries fall back to a
generic format check so legitimate firms are never wrongly rejected.

`validate(raw, country)` returns (ok, normalized, country_code).
"""
import re


# ---- checksums (only where the algorithm is well established) -------------- #
def _bg_eik9(num):
    d = [int(x) for x in num]
    w1 = [1, 2, 3, 4, 5, 6, 7, 8]
    s = sum(d[i] * w1[i] for i in range(8)) % 11
    if s < 10:
        return s == d[8]
    w2 = [3, 4, 5, 6, 7, 8, 9, 10]
    s2 = sum(d[i] * w2[i] for i in range(8)) % 11
    c = 0 if s2 == 10 else s2
    return c == d[8]


def _bg_eik(num):
    if len(num) == 9:
        return _bg_eik9(num)
    if len(num) == 13:
        if not _bg_eik9(num[:9]):
            return False
        d = [int(x) for x in num]
        w1 = [2, 7, 3, 5]
        s = sum(d[8 + i] * w1[i] for i in range(4)) % 11
        if s == 10:
            w2 = [4, 9, 5, 7]
            s = sum(d[8 + i] * w2[i] for i in range(4)) % 11
            if s == 10:
                s = 0
        return s == d[12]
    return len(num) == 10  # EGN-based VAT: accept by length


# ---- registry ------------------------------------------------------------- #
# code: {re: numeric/core format, vat: VAT prefix (or None), check: fn or None}
COUNTRIES = {
    "BG": {"re": r"^\d{9,13}$", "vat": "BG", "check": _bg_eik},
    "RO": {"re": r"^\d{2,10}$", "vat": "RO", "check": None},
    "GR": {"re": r"^\d{9}$", "vat": "EL", "check": None},
    "TR": {"re": r"^\d{10,11}$", "vat": None, "check": None},
    "IT": {"re": r"^\d{11}$", "vat": "IT", "check": None},
    "DE": {"re": r"^\d{9}$", "vat": "DE", "check": None},
    "RU": {"re": r"^(\d{10}|\d{12})$", "vat": None, "check": None},
    "RS": {"re": r"^\d{8,9}$", "vat": "RS", "check": None},
    "MK": {"re": r"^\d{7,13}$", "vat": "MK", "check": None},
    # ЄДРПОУ is 8 digits, the ІПН of a legal person is 12.
    "UA": {"re": r"^\d{8,12}$", "vat": "UA", "check": None},
    # A UK company number is eight characters and often starts with letters
    # (SC, NI, OC, FC); only the VAT number is all digits. Accepting just
    # digits rejected every Scottish and Northern Irish company outright.
    "GB": {"re": r"^([A-Z]{2}\d{6}|\d{8}|\d{9}|\d{12})$", "vat": "GB", "check": None},
    "ES": {"re": r"^[A-Z0-9]\d{7}[A-Z0-9]$", "vat": "ES", "check": None},
    "FR": {"re": r"^\d{9,14}$", "vat": "FR", "check": None},
    # NIP is 10 digits, REGON is 9 or 14. The label offered both; the pattern
    # accepted only NIP.
    "PL": {"re": r"^(\d{9}|\d{10}|\d{14})$", "vat": "PL", "check": None},
    "PT": {"re": r"^\d{9}$", "vat": "PT", "check": None},
    "OTHER": {"re": r"^[A-Z0-9]{4,20}$", "vat": None, "check": None},
}

# Map an EU-style VAT prefix to a country code (EL -> Greece).
VAT_PREFIX = {}
for _code, _spec in COUNTRIES.items():
    if _spec["vat"]:
        VAT_PREFIX[_spec["vat"]] = _code
VAT_PREFIX["GR"] = "GR"  # accept GR as well as EL
VAT_PREFIX["XI"] = "GB"  # Northern Ireland VAT numbers carry the XI prefix


# ---- per-country tax / registration metadata ------------------------------ #
# id_label: local name of the company number · vat_rate: standard VAT %
# · authority: registration authority · eu: in the EU VAT area (drives the
# intra-community reverse-charge option on invoices).
#
# Standard VAT rates verified against public sources on 2026-07-21. Rates change
# by legislation - the UI labels these as indicative and tells the user to
# confirm with an accountant. Recent changes reflected here:
#   RO 19 -> 21 % (from 2025-08-01) · RU 20 -> 22 % (from 2026-01-01)
# BG remains 20 % in 2026 (the 2026 ZDDS changes concern registration
# thresholds/regimes, not the standard rate).
VAT_RATES_VERIFIED = "2026-07-21"

META = {
    "BG": {"id_label": "ЕИК", "vat_prefix": "BG", "vat_rate": 20, "authority": "Търговски регистър (АВ)", "eu": True},
    "RO": {"id_label": "CUI", "vat_prefix": "RO", "vat_rate": 21, "authority": "ONRC", "eu": True},
    "GR": {"id_label": "ΑΦΜ", "vat_prefix": "EL", "vat_rate": 24, "authority": "ΓΕΜΗ", "eu": True},
    "IT": {"id_label": "Partita IVA", "vat_prefix": "IT", "vat_rate": 22, "authority": "Registro Imprese", "eu": True},
    # The number captured is the VAT identification number, so the authority
    # named is the one that issues it - not the commercial register, which
    # issues the unrelated HRB/HRA number.
    "DE": {"id_label": "USt-IdNr", "vat_prefix": "DE", "vat_rate": 19, "authority": "Bundeszentralamt für Steuern", "eu": True},
    "TR": {"id_label": "Vergi No", "vat_prefix": None, "vat_rate": 20, "authority": "Ticaret Sicili", "eu": False},
    # КПП is a separate nine-digit code and is not what is validated here.
    "RU": {"id_label": "ИНН", "vat_prefix": None, "vat_rate": 22, "authority": "ЕГРЮЛ (ФНС)", "eu": False},
    "RS": {"id_label": "PIB / Матични број", "vat_prefix": "RS", "vat_rate": 20, "authority": "APR", "eu": False},
    "MK": {"id_label": "ЕМБС / ЕДБ", "vat_prefix": "MK", "vat_rate": 18, "authority": "Централен регистар", "eu": False},
    "UA": {"id_label": "ЄДРПОУ / ІПН", "vat_prefix": "UA", "vat_rate": 20, "authority": "ЄДР (Мін'юст)", "eu": False},
    "GB": {"id_label": "Company No. / VAT", "vat_prefix": "GB", "vat_rate": 20, "authority": "Companies House", "eu": False},
    "ES": {"id_label": "CIF / NIF", "vat_prefix": "ES", "vat_rate": 21, "authority": "Registro Mercantil", "eu": True},
    "FR": {"id_label": "SIREN / SIRET", "vat_prefix": "FR", "vat_rate": 20, "authority": "RCS / Infogreffe", "eu": True},
    "PL": {"id_label": "NIP / REGON", "vat_prefix": "PL", "vat_rate": 23, "authority": "KRS", "eu": True},
    "PT": {"id_label": "NIPC / NIF", "vat_prefix": "PT", "vat_rate": 23, "authority": "Registo Comercial (IRN)", "eu": True},
    "OTHER": {"id_label": "Reg. No.", "vat_prefix": None, "vat_rate": 0, "authority": "", "eu": False},
}


def country_meta(code):
    return META.get((code or "OTHER").upper(), META["OTHER"])


def supported_countries():
    return [c for c in COUNTRIES.keys() if c != "OTHER"] + ["OTHER"]


def validate(raw, country):
    if not raw:
        return False, "", country
    s = re.sub(r"[\s.\-/]", "", str(raw).strip().upper())
    m = re.match(r"^([A-Z]{2})(.+)$", s)
    if m and m.group(1) in VAT_PREFIX:
        country = VAT_PREFIX[m.group(1)]
        num = m.group(2)
    else:
        num = s
    spec = COUNTRIES.get((country or "").upper()) or COUNTRIES["OTHER"]
    code = (country or "OTHER").upper()
    if code not in COUNTRIES:
        code = "OTHER"
    if not re.match(spec["re"], num):
        return False, s, code
    chk = spec.get("check")
    if chk and num.isdigit() and not chk(num):
        return False, s, code
    return True, s, code
