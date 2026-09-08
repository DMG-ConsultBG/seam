# -*- coding: utf-8 -*-
"""What a company owes, and what is left, from the numbers already in Seam.

Revenue is here: the invoices this company issued. Costs are partly here too,
in the payouts it recorded. The rest - rent, leases, wages, fuel, software -
is not, so it is entered once and carried forward, and the arithmetic joins
the two.

    revenue
      less operating costs, leases, wages and the employer's contributions
    = profit before tax
      less profit tax
    = what is left

    VAT charged out, less VAT paid in, = VAT payable

**These are not tax advice and this file does not pretend otherwise.** The
rates below are the published headline rates, carried as a starting point so
nobody types fifteen numbers on their first day; every one of them is editable
per company and every country has reliefs, bands and sector rules that change
them. `RATES_REVIEWED` says when they were last looked at. A company that has
not confirmed its own rates is told so on screen, in words, rather than shown a
figure it might believe.

No Flask, no database: given numbers in, numbers out, tested by self_test().

    py -3 taxes.py
"""
import datetime

#: When the defaults below were last reviewed. Shown to the user, so a stale
#: file admits it rather than presenting last year's rate as this year's.
RATES_REVIEWED = "2026-01"

#: Per country: the headline profit tax, the employer's share of payroll, how
#: often VAT is filed, and whether an intra-EU business customer shifts the VAT.
#:
#: `payroll_employer` is the one most likely to be wrong for any given company:
#: it moves with sector, contract type, headcount and salary band almost
#: everywhere. It is a starting figure, not a rule.
COUNTRY = {
    "BG": {"profit_tax": 10.0, "payroll_employer": 18.92, "vat_period": "monthly", "eu": True},
    "RO": {"profit_tax": 16.0, "payroll_employer": 2.25, "vat_period": "monthly", "eu": True},
    "GR": {"profit_tax": 22.0, "payroll_employer": 22.29, "vat_period": "quarterly", "eu": True},
    "IT": {"profit_tax": 24.0, "payroll_employer": 30.0, "vat_period": "quarterly", "eu": True},
    "DE": {"profit_tax": 30.0, "payroll_employer": 20.0, "vat_period": "monthly", "eu": True},
    "ES": {"profit_tax": 25.0, "payroll_employer": 31.4, "vat_period": "quarterly", "eu": True},
    "FR": {"profit_tax": 25.0, "payroll_employer": 45.0, "vat_period": "monthly", "eu": True},
    "PL": {"profit_tax": 19.0, "payroll_employer": 20.48, "vat_period": "monthly", "eu": True},
    "PT": {"profit_tax": 21.0, "payroll_employer": 23.75, "vat_period": "quarterly", "eu": True},
    "TR": {"profit_tax": 25.0, "payroll_employer": 22.5, "vat_period": "monthly", "eu": False},
    "RU": {"profit_tax": 25.0, "payroll_employer": 30.0, "vat_period": "quarterly", "eu": False},
    "UA": {"profit_tax": 18.0, "payroll_employer": 22.0, "vat_period": "monthly", "eu": False},
    "RS": {"profit_tax": 15.0, "payroll_employer": 15.15, "vat_period": "monthly", "eu": False},
    "MK": {"profit_tax": 10.0, "payroll_employer": 0.0, "vat_period": "monthly", "eu": False},
    "GB": {"profit_tax": 25.0, "payroll_employer": 15.0, "vat_period": "quarterly", "eu": False},
    "OTHER": {"profit_tax": 0.0, "payroll_employer": 0.0, "vat_period": "quarterly", "eu": False},
}

#: What each country actually asks a company to file, and roughly when.
#:
#: A profit figure is arithmetic; an obligation is administration, and the two
#: are not the same thing. This is the second: the name of the return in its
#: own language, because a Bulgarian VAT return is a справка-декларация in
#: every interface language, the usual deadline, and the electronic obligation
#: that catches companies out - clearance platforms, e-invoicing mandates and
#: standard audit files, which are now the part people are unprepared for.
#:
#: Deadlines move, and a filing calendar is not a substitute for an accountant.
#: `RATES_REVIEWED` covers these too, and the screen says so.
FILING = {
    "BG": {"vat_form": "Справка-декларация по ЗДДС", "vat_due": {"day": 14, "rule": "next_month"},
           "profit_form": "Годишна данъчна декларация по чл. 92 ЗКПО",
           "profit_due": {"month": 6, "day": 30}, "eu_sales": "VIES декларация",
           "digital": ["СУПТО, за търговски обекти"]},
    "RO": {"vat_form": "Decontul de TVA (D300)", "vat_due": {"day": 25, "rule": "next_month"},
           "profit_form": "Declarația 101", "profit_due": {"month": 3, "day": 25},
           "eu_sales": "Declarația 390 (VIES)",
           "digital": ["e-Factura (RO e-Factura)", "SAF-T (D406)", "e-Transport"]},
    "GR": {"vat_form": "Δήλωση ΦΠΑ (Φ2)", "vat_due": {"rule": "month_end"},
           "profit_form": "Δήλωση φορολογίας εισοδήματος (Ν)", "profit_due": {"month": 6, "day": 30},
           "eu_sales": "Ανακεφαλαιωτικός πίνακας",
           "digital": ["myDATA ηλεκτρονικά βιβλία"]},
    "IT": {"vat_form": "Liquidazione periodica IVA (LIPE)", "vat_due": {"day": 16, "rule": "after_period"},
           "profit_form": "Modello Redditi SC", "profit_due": {"month": 9, "day": 30},
           "eu_sales": "Elenchi INTRASTAT",
           "digital": ["Fattura elettronica via SdI", "Esterometro"]},
    "DE": {"vat_form": "Umsatzsteuer-Voranmeldung", "vat_due": {"day": 10, "rule": "next_month"},
           "profit_form": "Körperschaftsteuererklärung", "profit_due": {"month": 7, "day": 31},
           "eu_sales": "Zusammenfassende Meldung",
           "digital": ["ELSTER", "E-Rechnung (B2B, phased from 2025)"]},
    "ES": {"vat_form": "Modelo 303", "vat_due": {"day": 20, "rule": "after_period"},
           "profit_form": "Modelo 200", "profit_due": {"month": 7, "day": 25},
           "eu_sales": "Modelo 349",
           "digital": ["SII, for large filers", "Verifactu / TicketBAI"]},
    "FR": {"vat_form": "Déclaration CA3", "vat_due": {"rule": "varies"},
           "profit_form": "Liasse fiscale (2065)", "profit_due": {"month": 5, "day": 3},
           "eu_sales": "État récapitulatif TVA (DEB/DES)",
           "digital": ["Facturation électronique, phased from 2026"]},
    "PL": {"vat_form": "JPK_V7M / JPK_V7K", "vat_due": {"day": 25, "rule": "next_month"},
           "profit_form": "CIT-8", "profit_due": {"month": 3, "day": 31},
           "eu_sales": "Informacja podsumowująca VAT-UE",
           "digital": ["KSeF, phased from 2026"]},
    "PT": {"vat_form": "Declaração periódica de IVA", "vat_due": {"day": 20, "rule": "second_month"},
           "profit_form": "Modelo 22", "profit_due": {"month": 5, "day": 31},
           "eu_sales": "Declaração recapitulativa",
           "digital": ["SAF-T (PT)", "ATCUD e código QR na fatura"]},
    "TR": {"vat_form": "KDV Beyannamesi (KDV1)", "vat_due": {"day": 28, "rule": "next_month"},
           "profit_form": "Kurumlar Vergisi Beyannamesi", "profit_due": {"month": 4, "day": 30},
           "eu_sales": None, "digital": ["e-Fatura", "e-Arşiv", "e-Defter"]},
    "RU": {"vat_form": "Налоговая декларация по НДС", "vat_due": {"day": 25, "rule": "after_period"},
           "profit_form": "Декларация по налогу на прибыль", "profit_due": {"month": 3, "day": 25},
           "eu_sales": None, "digital": ["ЭДО, электронные счета-фактуры"]},
    "UA": {"vat_form": "Податкова декларація з ПДВ", "vat_due": {"day": 20, "rule": "next_month"},
           "profit_form": "Декларація з податку на прибуток", "profit_due": {"months_after": 2},
           "eu_sales": None, "digital": ["Реєстрація податкових накладних у ЄРПН"]},
    "RS": {"vat_form": "Пореска пријава ПДВ (ПППДВ)", "vat_due": {"day": 15, "rule": "after_period"},
           "profit_form": "Пореска пријава ПДП", "profit_due": {"months_after": 6},
           "eu_sales": None, "digital": ["Систем е-фактура (SEF)"]},
    "MK": {"vat_form": "Пријава ДДВ-04", "vat_due": {"day": 25, "rule": "after_period"},
           "profit_form": "Даночен биланс (ДБ)", "profit_due": {"month": 2, "day": 28},
           "eu_sales": None, "digital": []},
    "GB": {"vat_form": "VAT Return", "vat_due": {"rule": "one_month_seven"},
           "profit_form": "Company Tax Return (CT600)", "profit_due": {"months_after": 12},
           "eu_sales": None, "digital": ["Making Tax Digital for VAT"]},
    "OTHER": {"vat_form": None, "vat_due": None, "profit_form": None,
              "profit_due": None, "eu_sales": None, "digital": []},
}


#: The official electronic services, per country.
#:
#: Each entry is (category, the service's own name, address). The category is
#: translated by the interface; the name never is. "НАП" is not "the National
#: Revenue Agency" on the screen somebody has to log into, and sending a person
#: to look for a translated name is sending them nowhere.
#:
#: Only the institution's own domain is listed - no aggregators, no
#: intermediaries. A tax portal reached through somebody else's site is how
#: credentials end up somewhere they should not.
PORTALS = {
    "BG": [("authority", "НАП, електронни услуги", "https://portal.nra.bg/"),
           ("register", "Търговски регистър", "https://portal.registryagency.bg/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "RO": [("authority", "ANAF, Spațiul Privat Virtual", "https://www.anaf.ro/"),
           ("einvoice", "RO e-Factura", "https://mfinante.gov.ro/ro/web/efactura"),
           ("register", "ONRC", "https://portal.onrc.ro/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "GR": [("authority", "myAADE", "https://www.aade.gr/"),
           ("einvoice", "myDATA", "https://mydata.aade.gr/"),
           ("register", "ΓΕΜΗ", "https://www.businessportal.gr/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "IT": [("authority", "Agenzia delle Entrate", "https://www.agenziaentrate.gov.it/"),
           ("einvoice", "Fatture e Corrispettivi",
            "https://ivaservizi.agenziaentrate.gov.it/"),
           ("register", "Registro Imprese", "https://www.registroimprese.it/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "DE": [("authority", "ELSTER", "https://www.elster.de/"),
           ("authority2", "Bundeszentralamt für Steuern", "https://www.bzst.de/"),
           ("register", "Handelsregister", "https://www.handelsregister.de/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "ES": [("authority", "Sede electrónica de la AEAT",
            "https://sede.agenciatributaria.gob.es/"),
           ("register", "Registradores de España", "https://www.registradores.org/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "FR": [("authority", "impots.gouv.fr, espace professionnel",
            "https://www.impots.gouv.fr/professionnel"),
           ("register", "Infogreffe", "https://www.infogreffe.fr/"),
           ("register2", "INPI, guichet des formalités",
            "https://procedures.inpi.fr/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "PL": [("authority", "e-Urząd Skarbowy", "https://www.podatki.gov.pl/"),
           ("einvoice", "KSeF", "https://www.podatki.gov.pl/ksef/"),
           ("register", "KRS", "https://ekrs.ms.gov.pl/"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "PT": [("authority", "Portal das Finanças",
            "https://www.portaldasfinancas.gov.pt/"),
           ("register", "ePortugal, empresas", "https://eportugal.gov.pt/empresas"),
           ("vat_check", "VIES", "https://ec.europa.eu/taxation_customs/vies/")],
    "TR": [("authority", "İnteraktif Vergi Dairesi", "https://ivd.gib.gov.tr/"),
           ("einvoice", "e-Fatura", "https://ebelge.gib.gov.tr/"),
           ("register", "MERSİS", "https://mersis.ticaret.gov.tr/")],
    "RU": [("authority", "ФНС, личный кабинет", "https://lkul.nalog.ru/"),
           ("register", "ЕГРЮЛ", "https://egrul.nalog.ru/")],
    "UA": [("authority", "Електронний кабінет ДПС", "https://cabinet.tax.gov.ua/"),
           ("register", "ЄДР", "https://usr.minjust.gov.ua/")],
    "RS": [("authority", "ePorezi", "https://eporezi.purs.gov.rs/"),
           ("einvoice", "Систем е-фактура", "https://efaktura.gov.rs/"),
           ("register", "APR", "https://www.apr.gov.rs/")],
    "MK": [("authority", "е-Даноци (УЈП)", "https://etax.ujp.gov.mk/"),
           ("register", "Централен регистар", "https://www.crm.com.mk/")],
    "GB": [("authority", "HMRC online services",
            "https://www.gov.uk/log-in-register-hmrc-online-services"),
           ("register", "Companies House",
            "https://find-and-update.company-information.service.gov.uk/")],
    "OTHER": [],
}


def portals(country):
    """[{category, name, url}, ...] for a country, or an empty list."""
    return [{"category": c, "name": n, "url": u}
            for c, n, u in PORTALS.get((country or "").upper()) or []]


def filing(country):
    """What this country asks to be filed, or the empty answer."""
    out = dict(FILING.get((country or "").upper()) or FILING["OTHER"])
    out["digital"] = list(out.get("digital") or [])
    out["reviewed"] = RATES_REVIEWED
    return out


#: The kinds of cost a company enters itself, because they are not in Seam.
#: Wages are apart from the rest: the employer's contribution is a percentage
#: of them and of nothing else.
COST_KINDS = ("operating", "lease", "salary", "other")

#: Why a company might charge no VAT. Kept as reasons rather than a single
#: flag, because they are not the same thing and an accountant will ask which.
VAT_EXEMPT_REASONS = ("not_registered", "npo", "small_business", "other")


def rules(country):
    """The starting figures for a country, or the neutral ones."""
    return dict(COUNTRY.get((country or "").upper()) or COUNTRY["OTHER"])


def _money(x):
    try:
        return round(float(x or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def period_bounds(year, month=None, quarter=None):
    """(first day, last day) of a year, a month or a quarter."""
    year = int(year)
    if month:
        month = int(month)
        first = datetime.date(year, month, 1)
        last = (datetime.date(year + (month == 12), (month % 12) + 1, 1)
                - datetime.timedelta(days=1))
    elif quarter:
        q = int(quarter)
        first = datetime.date(year, 3 * (q - 1) + 1, 1)
        end_m = 3 * q
        last = (datetime.date(year + (end_m == 12), (end_m % 12) + 1, 1)
                - datetime.timedelta(days=1))
    else:
        first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    return first, last


def compute(revenue_net, vat_charged, vat_paid, costs, country,
            profit_tax=None, payroll_employer=None,
            vat_exempt=False, profit_tax_exempt=False):
    """The whole calculation, from figures a caller has already gathered.

    `costs` is {kind: amount} over COST_KINDS. Wages are the gross the company
    pays its people; the employer's contribution is worked out from them and
    added, because it is a cost of employing somebody and leaving it out
    overstates what is left by roughly a fifth in most of these countries.
    """
    r = rules(country)
    rate_profit = r["profit_tax"] if profit_tax is None else float(profit_tax)
    rate_payroll = r["payroll_employer"] if payroll_employer is None else float(payroll_employer)

    salaries = _money(costs.get("salary"))
    employer = round(salaries * rate_payroll / 100.0, 2)
    operating = _money(costs.get("operating"))
    lease = _money(costs.get("lease"))
    other = _money(costs.get("other"))
    total_costs = round(operating + lease + salaries + employer + other, 2)

    revenue = _money(revenue_net)
    before_tax = round(revenue - total_costs, 2)
    # No tax on a loss, and none for a body that does not pay it. Carrying a
    # loss forward is a real thing and a decision for an accountant, so this
    # says nothing about it rather than guessing.
    taxable = max(0.0, before_tax)
    tax = 0.0 if profit_tax_exempt else round(taxable * rate_profit / 100.0, 2)
    net = round(before_tax - tax, 2)

    # A company that charges no VAT does not reclaim it either. Netting the
    # input VAT of an exempt company would hand it a refund it cannot claim.
    out_vat = 0.0 if vat_exempt else _money(vat_charged)
    in_vat = 0.0 if vat_exempt else _money(vat_paid)
    vat_due = round(out_vat - in_vat, 2)

    return {
        "revenue": revenue,
        "costs": {"operating": operating, "lease": lease, "salary": salaries,
                  "employer_contributions": employer, "other": other,
                  "total": total_costs},
        "profit_before_tax": before_tax,
        "profit_tax_rate": rate_profit,
        "profit_tax": tax,
        "net_profit": net,
        "payroll_employer_rate": rate_payroll,
        "vat": {"charged": out_vat, "paid": in_vat, "due": vat_due,
                "exempt": bool(vat_exempt), "period": r["vat_period"]},
        "profit_tax_exempt": bool(profit_tax_exempt),
        "country": (country or "").upper() or "OTHER",
        "eu": r["eu"],
        "rates_reviewed": RATES_REVIEWED,
    }


def reverse_charge(seller_country, buyer_country, buyer_has_vat_id):
    """Whether the seller charges no VAT and the buyer accounts for it.

    Inside the EU, between two VAT-registered businesses in different member
    states, the supply is reported without VAT and the customer self-accounts.
    Outside that, this says no and lets the normal rate apply, because the
    exceptions are a matter for the company's own advisers.
    """
    a, b = (seller_country or "").upper(), (buyer_country or "").upper()
    if not a or not b or a == b:
        return False
    return bool(rules(a)["eu"] and rules(b)["eu"] and buyer_has_vat_id)


# --------------------------------------------------------------------------- #
def self_test():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS  %s" % name)
        else:
            fail += 1
            print("  FAIL  %s  %s" % (name, detail))

    print("== a period is a period ==")
    check("a whole year", period_bounds(2026) ==
          (datetime.date(2026, 1, 1), datetime.date(2026, 12, 31)))
    check("a month in the middle", period_bounds(2026, month=6) ==
          (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30)))
    check("December does not run into next year",
          period_bounds(2026, month=12) ==
          (datetime.date(2026, 12, 1), datetime.date(2026, 12, 31)))
    check("February in a leap year", period_bounds(2028, month=2) ==
          (datetime.date(2028, 2, 1), datetime.date(2028, 2, 29)))
    check("the fourth quarter", period_bounds(2026, quarter=4) ==
          (datetime.date(2026, 10, 1), datetime.date(2026, 12, 31)))
    check("the first quarter", period_bounds(2026, quarter=1) ==
          (datetime.date(2026, 1, 1), datetime.date(2026, 3, 31)))

    print("\n== the arithmetic ==")
    r = compute(100000, 20000, 6000,
                {"operating": 20000, "lease": 12000, "salary": 30000}, "BG")
    check("the employer's share is added to wages",
          r["costs"]["employer_contributions"] == 5676.0,
          str(r["costs"]["employer_contributions"]))
    check("costs are the sum of the parts", r["costs"]["total"] == 67676.0,
          str(r["costs"]["total"]))
    check("profit before tax", r["profit_before_tax"] == 32324.0,
          str(r["profit_before_tax"]))
    check("profit tax at the country's rate", r["profit_tax"] == 3232.4,
          str(r["profit_tax"]))
    check("what is left", r["net_profit"] == 29091.6, str(r["net_profit"]))
    check("VAT is what was charged less what was paid", r["vat"]["due"] == 14000.0,
          str(r["vat"]["due"]))

    print("\n== a loss is not taxed ==")
    r = compute(10000, 2000, 500, {"operating": 40000}, "BG")
    check("the loss is reported", r["profit_before_tax"] == -30000.0,
          str(r["profit_before_tax"]))
    check("and no tax is charged on it", r["profit_tax"] == 0.0, str(r["profit_tax"]))
    check("what is left is the loss itself", r["net_profit"] == -30000.0,
          str(r["net_profit"]))

    print("\n== exempt is not the same as zero ==")
    r = compute(50000, 10000, 3000, {"operating": 10000}, "BG", vat_exempt=True)
    check("an exempt company owes no VAT", r["vat"]["due"] == 0.0, str(r["vat"]))
    check("and does not reclaim what it paid", r["vat"]["paid"] == 0.0, str(r["vat"]))
    check("but still pays profit tax", r["profit_tax"] == 4000.0, str(r["profit_tax"]))

    r = compute(50000, 0, 0, {"operating": 10000}, "BG",
                vat_exempt=True, profit_tax_exempt=True)
    check("a body exempt from profit tax pays none", r["profit_tax"] == 0.0)
    check("and keeps the whole surplus", r["net_profit"] == 40000.0, str(r["net_profit"]))

    print("\n== the company's own rates win over the defaults ==")
    r = compute(100000, 0, 0, {"salary": 10000}, "BG",
                profit_tax=5, payroll_employer=0)
    check("its own profit tax rate is used", r["profit_tax_rate"] == 5)
    check("its own payroll rate is used", r["costs"]["employer_contributions"] == 0.0)
    check("a rate of zero is honoured, not treated as unset",
          r["profit_tax"] == 4500.0, str(r["profit_tax"]))

    print("\n== every country the platform knows has figures ==")
    import company                                            # noqa: E402
    missing = [c for c in company.META if c not in COUNTRY]
    check("no country is left without them", not missing, str(missing))
    bad = [c for c, v in COUNTRY.items()
           if not (0 <= v["profit_tax"] <= 60) or not (0 <= v["payroll_employer"] <= 60)
           or v["vat_period"] not in ("monthly", "quarterly")]
    check("and none of them is nonsense", not bad, str(bad))

    print("\n== what each country asks to be filed ==")
    gaps = [c for c in company.META if c not in FILING]
    check("every country the platform knows has a filing calendar", not gaps, str(gaps))
    thin = [c for c, v in FILING.items()
            if c != "OTHER" and not (v["vat_form"] and v["vat_due"] and v["profit_form"])]
    check("and none of them is half-filled", not thin, str(thin))
    # A deadline is data, not an English sentence: the screen renders it in the
    # reader's language. A string here would put "14th of the following month"
    # next to a Bulgarian form name.
    prose = [c for c, v in FILING.items()
             if c != "OTHER" and (isinstance(v["vat_due"], str) or isinstance(v["profit_due"], str))]
    check("no deadline is left as English prose", not prose, str(prose))
    VAT_RULES = ("next_month", "after_period", "month_end", "second_month",
                 "one_month_seven", "varies")
    odd = [c for c, v in FILING.items()
           if c != "OTHER" and v["vat_due"].get("rule") not in VAT_RULES]
    check("and every VAT rule is one the screen can render", not odd, str(odd))
    eu_no_recap = [c for c, v in FILING.items()
                   if c != "OTHER" and rules(c)["eu"] and not v["eu_sales"]]
    check("every EU country has its recapitulative statement", not eu_no_recap,
          str(eu_no_recap))
    non_eu_recap = [c for c, v in FILING.items()
                    if c != "OTHER" and not rules(c)["eu"] and v["eu_sales"]]
    check("and no country outside the EU pretends to have one", not non_eu_recap,
          str(non_eu_recap))
    check("an unknown country answers with nothing rather than raising",
          filing("ZZ")["vat_form"] is None and filing("ZZ")["digital"] == [])
    check("the calendar carries the date it was reviewed",
          filing("BG")["reviewed"] == RATES_REVIEWED)

    print("\n== the official portals ==")
    missing = [c for c in company.META if c != "OTHER" and not portals(c)]
    check("every country the platform knows has at least one", not missing, str(missing))
    no_auth = [c for c in PORTALS
               if c != "OTHER" and not any(p[0].startswith("authority")
                                           for p in PORTALS[c])]
    check("and each of them names its tax authority", not no_auth, str(no_auth))
    # A tax portal reached over plain HTTP is a tax portal somebody can sit in
    # the middle of.
    plain = [(c, u) for c, rows in PORTALS.items() for _k, _n, u in rows
             if not u.startswith("https://")]
    check("every address is https", not plain, str(plain))
    eu_no_vies = [c for c, rows in PORTALS.items()
                  if c != "OTHER" and rules(c)["eu"]
                  and not any(k == "vat_check" for k, _n, _u in rows)]
    check("every EU country can check a VAT number", not eu_no_vies, str(eu_no_vies))
    non_eu_vies = [c for c, rows in PORTALS.items()
                   if c != "OTHER" and not rules(c)["eu"]
                   and any(k == "vat_check" for k, _n, _u in rows)]
    check("and no country outside it is sent to VIES", not non_eu_vies, str(non_eu_vies))
    check("an unknown country is sent nowhere", portals("ZZ") == [])

    print("\n== who charges the VAT on a cross-border sale ==")
    check("two EU businesses, the customer accounts for it",
          reverse_charge("BG", "DE", True))
    check("without a VAT number, the seller charges it",
          not reverse_charge("BG", "DE", False))
    check("inside one country, the seller charges it",
          not reverse_charge("BG", "BG", True))
    check("outside the EU, this says nothing",
          not reverse_charge("BG", "TR", True))

    print("\n== unusable input does not raise ==")
    r = compute("", None, "x", {"salary": "abc"}, "ZZ")
    check("nothing blows up", r["net_profit"] == 0.0, str(r["net_profit"]))
    check("an unknown country falls back to neutral figures",
          r["profit_tax_rate"] == 0.0 and r["country"] == "ZZ", str(r["country"]))

    print("\n---- %d passed, %d failed ----" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
