# Seam: what is being sold

A shared operational layer for two companies working together: one hands out
work, the other does it, and both see the same record. Orders and their
statuses, terms that need both signatures, documents and e-signatures,
invoices and payment matching, per-country tax and filing.

Written for the Bulgarian and wider south-east European market first, in
thirteen languages.

This document is for somebody deciding whether to buy it. It is written to be
checked, not to persuade: every figure below can be reproduced from the
repository in a few minutes, and the section on what is missing is longer than
the section on what is good.

---

## What exists

| | |
|---|---|
| Server | 28 Python modules, ~20 300 lines |
| Browser | 9 files, ~10 000 lines, no framework |
| Tests | 44 suites, ~7 500 lines, **2 204 checks, 0 failing** |
| API | 218 routes |
| Languages | 13, complete, with tests that prevent mixing |
| Relationship templates | 7 base, plus 28 opt-in trade packs |
| Countries with tax and filing data | 15 |

**Dependencies: Flask and waitress.** Everything else is the Python standard
library: the PDF pipeline, the e-signature layer, the AWS-signature and
banking-format parsers, the GIF encoder, the CDP browser driver. No database
server, no compiled extension, no build step for the front end.

That is the single most commercially relevant fact in this document. It means
a buyer can audit the whole thing, deploy it on anything, and is not exposed to
a supply chain they did not choose.

## Verifying it in ten minutes

```sh
python tests/run_tests.py     # 2 204 checks against a real server
python tests/audit_web.py     # accessibility, links, weight, both themes
.\demo.ps1                    # runs it, prints demo logins
```

The test suite starts its own server and database; it does not need
configuration. The audit drives a real browser and checks contrast against
each element's actual background.

To see what the product is for, open two browsers: `ops@seam.demo` and
`build@seam.demo`, both `demo1234`. One record, two sides, every change dated
and attributed.

## What is genuinely good

**The test suite is not decoration.** It found, during development: a column
that never existed sitting in a live code path; an account-enumeration oracle
in password reset; session cookies shipping without `Secure` behind a TLS
proxy; an encrypted backup format with no way to decrypt it. Each is in the
history with the fix.

**The thirteen languages are real.** Not machine output: CLDR plural forms,
French and Italian elisions, per-country form names left untranslated because
a *справка-декларация* is called that on the form. Four separate tests keep
the languages from mixing.

**Deployment is a solved problem.** One script for a Linux box with automatic
HTTPS; a Dockerfile and `fly.toml` for a container platform. Both were run.

**Operational tooling exists** for the things that bite later: an unauthenticated
health endpoint, an outbound alert hook, a backup decryptor, a second-factor
recovery tool, a demo-data purge.

## What a buyer must know

These are the reasons this is worth less than the line count suggests. A buyer
will find them anyway; better they come from the seller.

**No users. No revenue. No validation.** Nobody has run a business on this. The
product may solve a problem nobody pays for, and nothing here proves otherwise.
This is the dominant fact and it should dominate the price.

**One writer.** SQLite with WAL. Measured on a laptop: 16 concurrent writes,
p95 223 ms, no failures. Fine for tens of companies, and a rewrite of the
storage layer before hundreds. The schema is ordinary SQL and would port to
Postgres, but that is real work.

**Never operated.** Deployment is verified; production is not the same thing.
Nothing has run for a month, been restored from a backup in anger, or had a
customer waiting while it was down.

**The legal templates have not been reviewed by a lawyer.** Seam generates
contracts, protocols, powers of attorney and GDPR declarations across several
jurisdictions. They were written to be plausible and are not legal advice. A
buyer taking this to market needs them reviewed, and that is a real cost and a
real liability.

**Solo-authored.** No second pair of eyes has read this code. The tests are the
only external check, and they were written by the same person.

**Narrow first market.** Bulgarian-first, south-east Europe. That is a
deliberate wedge and it is also a smaller pool of buyers.

## Who this is actually for

Not a fund, and not a general acquirer. The realistic buyer is a company that
**already has the customers** and would rather buy than build:

- accounting or ERP software with an installed base of small firms
- a construction-sector or logistics SaaS wanting the counterparty side
- an integrator who already sells to these companies and wants something of
  their own to sell alongside
- a company that needs the per-country tax, filing and e-signature work and
  would spend a year building it

The value is the **domain work and the time**, not the code as such: fifteen
countries of tax and filing rules, twenty-eight trade packs, thirteen
languages, an e-signature path, banking format parsers. Someone starting from
zero spends a long time on exactly that and gets it wrong twice.

## What comes with it

- The repository, including history
- The test suite and the browser audit
- `DEPLOY.md`, the deployment kit, the operational tools
- The thirteen-language string tables
- The per-country data in `taxes.py` and `company.py`

Written entirely by one author with no third-party code beyond Flask and
waitress, so the copyright position is simple. **No licence has been chosen
yet.** That is the seller's decision and should be settled before any
conversation, because a buyer will ask on the first call.

## Honest framing for a conversation

Do not open with the line count. Open with:

> Two companies working together keep the same job in two places and argue
> about which is right. This is one record both of them see, with every change
> dated and attributed, and it already knows about VAT, actuation protocols
> and customs in fifteen countries.

Then show the two-browser demo. Then hand them this document and invite them to
run the test suite themselves.

The strongest thing you have is that everything claimed here can be checked in
ten minutes by someone who does not trust you. Lead with that.
