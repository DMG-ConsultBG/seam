# Marketplace listing, ready to paste

Two versions, English first because that is the larger pool of buyers. Nothing
here claims users or revenue: marketplaces require the disclosure, and a buyer
who discovers it themselves stops the conversation.

---

## Title

**Use this. 79 characters.**

> Seam. Self-hosted subcontracting platform: one record, both sides, 13 languages

The category first, so it is found in a search, then the differentiator. The
colon does the work a dash would: it ties "what it is" to "what is different
about it" without a second sentence.

Alternatives, all within 80:

| | |
|---|---|
| 79 | Seam. Self-hosted subcontracting platform. One record, both sides, 13 languages |
| 74 | Seam. Self-hosted B2B subcontracting: one record, both sides, 13 languages |

Longer, if the field allows 100:

| | |
|---|---|
| 87 | Seam: self-hosted B2B platform where a company and its contractors work from one record |
| 85 | Seam: self-hosted B2B operations platform for companies and the contractors they hire |

Bulgarian, 79 characters:

> Seam: обща работна среда за фирма и нейните изпълнители. Един запис, две страни

## Tagline

For a second short field, if there is one. Within 80:

| | |
|---|---|
| 80 | Both sides see the same order, terms and invoice. Every change dated and signed. |
| 79 | Both sides see the same order, terms, docs and invoice. Every change is signed. |

The first drops "documents" from the list and keeps "dated and signed" whole,
which is the part that matters: the audit trail is the difference, and signing
implies the documents anyway. The second keeps all four items and pays for it
by shortening the second clause.

Longer version, if the field is generous:

> One record both companies can see, with every change dated and signed.

---

## Short description (150–200 words)

Seam is a finished, self-hosted B2B platform for two companies that work
together: one hands out the work, the other does it, and both look at the same
record instead of two spreadsheets and a WhatsApp thread.

Each order carries its own status pipeline, the terms both sides accepted,
the documents and e-signatures, the invoice and its payment. Every change is
dated and attributed, so "when did we agree that" has an answer.

Built for small and mid-sized firms in construction, logistics, e-commerce
fulfilment, vehicle import, agriculture and general subcontracting. Ships with
28 opt-in trade packs, per-country VAT, tax and filing data for 15 countries,
and a complete interface in 13 languages.

Python and vanilla JavaScript. **Flask and waitress are the only two
dependencies.** No database server, no front-end build, no third-party
services required. 2,204 automated checks pass against a real server.

Sold as-is: a complete codebase with no users and no revenue. Priced
accordingly.

---

## Full description

### The problem

When one company subcontracts to another, the job lives in two places. The
buyer has it in their spreadsheet, the contractor has it in theirs, and the
agreement itself is somewhere in a mail thread. Nobody can say what was agreed,
when the status changed, or who approved the extra work, so every dispute
becomes an argument about whose version is right.

Existing tools solve half of it. Project software is single-company: the other
side gets read-only access or an email. Accounting software knows the invoice
but not the job. Neither treats the two companies as equals looking at one
record.

### What Seam does

One shared record per job, visible to both companies, with every change dated
and attributed.

- **Orders and statuses.** Own reference, own pipeline, both sides watch it
- **Agreed terms.** Price, deadline, scope; each accepted by the other side,
  with the name and date on it
- **Documents and e-signatures.** Country-specific contracts and protocols,
  signed, exported as real PDFs
- **Invoices and payments.** Issued, chased on schedule, matched against a
  bank statement (CAMT.053 / MT940)
- **Tax and filing.** Per-country VAT, profit tax, filing deadlines and links
  to the tax authority's own e-services, for 15 countries
- **Profiles and directory.** Companies publish what they do and what they
  have delivered; contact details are never handed to strangers
- **Reminders.** Terms up for review, deadlines approaching, invoices overdue

### Trade depth

The base templates stay thin so the first record is easy to open. Depth is
opt-in: 28 packs, each a real practice with its own fields, agreed terms,
pipeline stage and figures.

Construction: interim works certificates with retention, concealed works,
health and safety, warranties, plant hours, materials with declarations of
performance. Vehicle import: customs clearance with MRN, registration,
environmental tax, transport, inspection, damage assessment, finance.
E-commerce: returns, cash on delivery, batches, marketplace channels, service
levels. Plus quality control, confidentiality, insurance and cold chain across
all of them.

### Technical

- Python 3.9+, Flask, waitress. Vanilla JavaScript, no framework, no build step
- SQLite (WAL). No database server to run
- **Only two dependencies.** PDF export, e-signatures, AWS request signing,
  banking format parsing and image encoding are all standard library
- 218 API routes, ~30,000 lines, 44 test suites, **2,204 checks passing**
- Accessibility audit passes with zero findings in light and dark themes
- Deployment kit: one-command install with automatic HTTPS, or Docker

Anyone can verify all of it in ten minutes:

```
python tests/run_tests.py
python tests/audit_web.py
```

### Who should buy this

A company that **already has the customers**: accounting or ERP software with
a base of small firms, a construction or logistics SaaS missing the
counterparty side, or an integrator who wants something of their own to sell.

The value is the domain work and the time it represents: 15 countries of tax
and filing rules, 28 trade packs, 13 languages, e-signatures, banking formats.
Starting from zero, that is a year, and it gets built wrong twice on the way.

### Honest disclosure

- **No users, no revenue, never operated in production.** This is a finished
  codebase, not a running business
- SQLite has a single writer, sized for tens of companies per instance;
  hundreds would need the storage layer reworked
- The generated legal documents have not been reviewed by a lawyer in any
  jurisdiction. A buyer taking this to market needs that done
- Written by one author; the test suite is the only external check

### Included

Full repository with history, 44 test suites, browser audit, deployment kit,
operational tools (backup decryption, second-factor recovery, demo-data purge),
all 13 language tables, and the per-country data. Complete transfer of rights;
no third-party code beyond Flask and waitress.

---

## Български вариант

### Заглавие

> Seam: обща работна среда за фирма и нейните изпълнители. Един запис, две страни

### Кратко описание

Seam е завършена платформа за две фирми, които работят заедно: едната възлага,
другата изпълнява, и двете гледат един и същи запис вместо две таблици и
кореспонденция.

Всяка поръчка носи своя статус, условията, приети и от двете страни,
документите с електронен подпис, фактурата и плащането по нея. Всяка промяна
остава с дата и име.

За малки и средни фирми в строителството, логистиката, онлайн търговията,
вноса на автомобили и земеделието. 28 браншови пакета, ДДС и данъчни данни за
15 държави, интерфейс на 13 езика.

Python и чист JavaScript. **Единствените зависимости са Flask и waitress.**
Без сървър за база данни, без външни услуги. 2204 автоматични проверки минават
срещу истински сървър.

Продава се в сегашния вид: завършен код без потребители и без приход.

---

## Notes for filling the form

**Category:** SaaS / B2B software, or "code and scripts", whichever the
marketplace uses for an asset sale rather than a running business.

**Asking price:** put a number. Listings without one get filtered out of
searches and attract tyre-kickers. If you would take a given figure, ask a little above and
say the price is negotiable.

**Screenshots:** the two-browser view is the one that explains the product:
the same record open as the buyer and as the contractor. Then the add-ons
screen, the analytics with the trade figures, and a generated document.

**First question you will get:** "why are you selling?" Answer it in the
listing before it is asked. "Built it, it is finished, I do not want to run a
company around it" is a perfectly good answer and buyers hear it often.

**Second question:** the licence. Decide before you list whether you are
selling exclusive rights or a licence. Buyers ask on the first call.
