# -*- coding: utf-8 -*-
"""
SQLite data layer.

Chosen deliberately for the self-host path (Section 10): no cloud coupling, no
native build step, a single file you can back up by copying. Multi-tenant
isolation is enforced in the access layer — every query is scoped by workspace
membership, and every mutation is written to the immutable `events` audit log.
"""
import os
import re
import hashlib
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("SEAM_DB", os.path.join(os.path.dirname(__file__), "seam.db"))

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    verified      INTEGER NOT NULL DEFAULT 0,
    verify_token  TEXT,
    lang          TEXT,
    phone         TEXT,
    -- The person behind the account. In a network where two companies decide
    -- whether to work together, knowing who is on the other side is part of
    -- the product, not decoration.
    job_title     TEXT,
    bio           TEXT,
    avatar        TEXT,           -- file name under uploads/profile/
    links_json    TEXT,           -- [{label, url}]
    notify_json   TEXT,
    cal_token     TEXT,
    cal_token_enc TEXT,
    reset_token   TEXT,
    reset_expires TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orgs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('business','partner')),
    reg_number      TEXT,
    country         TEXT,
    verified_company INTEGER NOT NULL DEFAULT 0,
    plan            TEXT NOT NULL DEFAULT 'starter',
    plan_until      TEXT,
    -- How the processor identifies this company, so a webhook that arrives an
    -- hour later can be matched without trusting anything the browser says.
    pay_session     TEXT,
    pay_customer    TEXT,
    pay_method      TEXT,
    -- Where this company is paid. Entered once; carried onto every invoice it
    -- issues, so nobody retypes an IBAN into a legal document.
    pay_iban        TEXT,
    pay_bic         TEXT,
    pay_bank        TEXT,
    pay_terms       TEXT,          -- default payment days
    pay_link        TEXT,          -- a card or checkout link, when there is one
    remind_json     TEXT,          -- when to chase, per company
    -- Which trades this company is actually in. The whole platform is built for
    -- seven of them; a construction firm has no use for a harvest supply
    -- agreement, so it does not have to look at one. NULL means never asked,
    -- which is what triggers the question at sign-in.
    sectors_json    TEXT,
    -- What this company owes and at what rate. NULL means "use the published
    -- figure for the country"; a saved value is the company's own and wins.
    -- tax_reviewed_at is the day somebody confirmed them, and until it is set
    -- the screen says so instead of presenting a figure as settled.
    tax_profit_rate   REAL,
    tax_payroll_rate  REAL,
    vat_exempt        INTEGER NOT NULL DEFAULT 0,
    vat_exempt_reason TEXT,
    profit_tax_exempt INTEGER NOT NULL DEFAULT 0,
    tax_reviewed_at   TEXT,
    -- The company as its counterparties see it.
    about           TEXT,
    website         TEXT,
    logo            TEXT,           -- file name under uploads/profile/
    org_links_json  TEXT,           -- [{label, url}]
    contact_json    TEXT,
    -- Which side of the table this company sits on: it hands out work, it
    -- looks for work, or both. NULL means never stated, and profiles.py then
    -- reads it from `kind`, which is the closest honest guess.
    posture         TEXT,
    headline        TEXT,           -- one line, the first thing anyone reads
    size_band       TEXT,           -- solo | small | medium | large
    founded         TEXT,           -- year, as text: nobody has a full date
    areas_json      TEXT,           -- where it actually works, ["BG","RO"]
    contact_email   TEXT,           -- the office address, when it is not the owner's
    -- Whether this profile may be found by companies it has never worked
    -- with. Off until switched on: being on Seam is not consent to be listed.
    listed          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What a company has actually done. Typed in by its own people, so it proves
-- nothing on its own - which is why an entry can carry filed documents as
-- evidence, and why a Seam reference (reference.py), built from delivered
-- orders and impossible to type, is presented separately and never mixed in.
CREATE TABLE IF NOT EXISTS portfolio (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL DEFAULT 'project',
    title      TEXT NOT NULL,
    summary    TEXT,
    role       TEXT,            -- what this company did on it
    client     TEXT,            -- named only if the company chooses to
    place      TEXT,
    year       TEXT,
    value_band TEXT,            -- a band, never a figure: see reference.py
    url        TEXT,
    visible    INTEGER NOT NULL DEFAULT 1,
    sort       INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portfolio_org ON portfolio(org_id, sort, id);

-- Evidence attached to a portfolio entry. The file itself stays on the
-- document shelf, filed once and reused: a certificate hung on three projects
-- is one file, one checksum, one place to revoke it.
CREATE TABLE IF NOT EXISTS portfolio_docs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
    doc_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    UNIQUE (item_id, doc_id)
);

-- "Call me back". A profile never shows a stranger a telephone number; it
-- takes the caller's own instead and mails it to the owner, who decides. The
-- row is kept so a company can see who asked and how often.
CREATE TABLE IF NOT EXISTS contact_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    from_org_id  INTEGER REFERENCES orgs(id) ON DELETE SET NULL,
    from_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    phone        TEXT,
    email        TEXT,
    note         TEXT,
    sent         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contact_req_org ON contact_requests(org_id, id);

-- Which depth a company has switched on, per relationship template. An add-on
-- adds fields, terms, stages and figures that only its own trade needs, so
-- this is what keeps a builder's form from carrying a shop's returns policy.
-- Rows are per (org, template, add-on); absence means off.
CREATE TABLE IF NOT EXISTS org_addons (
    org_id       INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    template_key TEXT NOT NULL,
    addon_key    TEXT NOT NULL,
    enabled_at   TEXT NOT NULL DEFAULT (datetime('now')),
    enabled_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (org_id, template_key, addon_key)
);

CREATE TABLE IF NOT EXISTS memberships (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id  INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    role    TEXT NOT NULL DEFAULT 'admin',
    UNIQUE (user_id, org_id)
);

CREATE TABLE IF NOT EXISTS custom_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    key         TEXT UNIQUE NOT NULL,
    label       TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    plan        TEXT,
    price       TEXT,
    status      TEXT NOT NULL DEFAULT 'draft',
    paid_until  TEXT,
    pay_method  TEXT,
    invoice_no  TEXT,
    stripe_session TEXT,
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workspaces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    template      TEXT NOT NULL,
    buyer_org_id  INTEGER NOT NULL REFERENCES orgs(id),
    partner_org_id INTEGER REFERENCES orgs(id),
    status        TEXT NOT NULL DEFAULT 'active',
    created_by    INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    token        TEXT UNIQUE NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ref          TEXT NOT NULL,
    title        TEXT NOT NULL,
    template     TEXT NOT NULL,
    status       TEXT NOT NULL,
    fields_json  TEXT NOT NULL DEFAULT '{}',
    created_by   INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS order_terms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    value         TEXT,
    state         TEXT NOT NULL DEFAULT 'proposed' CHECK (state IN ('proposed','agreed')),
    proposed_by_org INTEGER REFERENCES orgs(id),
    updated_by    INTEGER REFERENCES users(id),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- When the two sides settled on this, kept apart from updated_at, which
    -- moves on any edit. A review is counted from the agreement, not from the
    -- last time somebody touched the row.
    agreed_at     TEXT,
    -- How often this wants another look, in days. Null means never, which is
    -- the right default: most terms are settled once and stay settled.
    review_every_days INTEGER,
    UNIQUE (order_id, key)
);

-- What has already been said about an agreement, so it is not said again.
--
-- A deadline that slipped raises one alert for the date it missed and then
-- goes quiet; without this row it would be raised every morning until somebody
-- either fixed it or learned to ignore the whole system, and the second is
-- what actually happens.
CREATE TABLE IF NOT EXISTS agreement_alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key   TEXT NOT NULL UNIQUE,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    order_id    INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    term_key    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    on_date     TEXT NOT NULL,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agr_alert_order ON agreement_alerts(order_id);

-- Costs the platform cannot know about.
--
-- Revenue is in the invoices and part of the cost is in the payouts, but rent,
-- leases, wages, fuel and software are not, and without them a profit figure
-- is only a revenue figure wearing a different label. Entered once; a
-- recurring one is counted in every month of the period it covers.
CREATE TABLE IF NOT EXISTS expenses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- operating | lease | salary | other
    label       TEXT NOT NULL,
    amount      REAL NOT NULL DEFAULT 0,
    currency    TEXT NOT NULL DEFAULT 'EUR',
    -- The VAT inside `amount`, when the company can reclaim it. Kept apart
    -- rather than derived: a lease and a wage carry different treatment and
    -- guessing one rate for both is how a VAT return goes wrong.
    vat_amount  REAL NOT NULL DEFAULT 0,
    -- 0 for a one-off on `on_date`; 1 for something that repeats every month
    -- between starts_on and ends_on.
    recurring   INTEGER NOT NULL DEFAULT 0,
    on_date     TEXT,
    starts_on   TEXT,
    ends_on     TEXT,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL,
    note        TEXT,
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_expenses_org ON expenses(org_id, kind);

-- One part of the platform, bought on its own.
--
-- Rows here are written by exactly the same three proofs that grant a plan: a
-- signed webhook, an answer from the processor about a session this server
-- opened, or an instance administrator recording a transfer. Nothing a browser
-- says reaches this table, which is why `ref` is not null: every row can be
-- traced back to the thing that paid for it.
CREATE TABLE IF NOT EXISTS feature_purchases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    feature    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'once',   -- once | month | year
    until      TEXT,                            -- null for 'once'
    price      REAL,
    currency   TEXT NOT NULL DEFAULT 'EUR',
    method     TEXT,
    ref        TEXT NOT NULL,
    granted_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fpurch_org ON feature_purchases(org_id, feature);

-- Immutable audit trail. Section 10: "Audit trail / versioning on every agreed
-- term — this is what kills disputes and is a genuine moat." Append only.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    order_id     INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    actor_user_id INTEGER REFERENCES users(id),
    actor_side   TEXT,
    kind         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    meta_json    TEXT NOT NULL DEFAULT '{}',
    ip           TEXT,
    device       TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Electronic signatures with signing evidence (roadmap item 2).
-- A request is created for a document, then signed via a single-use token.
-- Nothing here is ever updated after signing - it is the evidence record.
CREATE TABLE IF NOT EXISTS signatures (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    order_id      INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    doc_kind      TEXT NOT NULL,
    doc_title     TEXT NOT NULL,
    doc_url       TEXT,
    doc_hash      TEXT NOT NULL,
    level         TEXT NOT NULL DEFAULT 'simple',
    token         TEXT UNIQUE NOT NULL,
    token_enc     TEXT,
    requested_by  INTEGER REFERENCES users(id),
    signer_name   TEXT,
    signer_email  TEXT,
    signer_role   TEXT,
    otp_code      TEXT,
    otp_verified  INTEGER NOT NULL DEFAULT 0,
    signature_img TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    ip            TEXT,
    device        TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    -- Qualified level: which provider carried the signature and what it returned
    qtsp_provider  TEXT,
    qtsp_ref       TEXT,
    qtsp_state     TEXT,
    qtsp_signature TEXT,
    qtsp_cert      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    signed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_order ON signatures(order_id);
CREATE INDEX IF NOT EXISTS idx_sig_token ON signatures(token);

-- The shelf.
--
-- Two things were missing and both are the same shape. A document generated
-- here was never kept: `/docs/invoice?...` rebuilt it from the query string
-- every time, so losing the link lost the document. And a document that
-- arrived from outside had nowhere to go at all - `attachments` accepts photos
-- and video, which is progress evidence, not paperwork. Most of what passes
-- between two companies is a PDF.
--
-- `workspace_id` is what makes it a shelf rather than a folder: a framework
-- contract, an insurance certificate or a licence belongs to the relationship,
-- not to one order inside it. `shared` decides whether the counterparty sees
-- it; a company's own certificates are its own business until it says
-- otherwise.
--
-- `sha256` is over the bytes as stored, so a file can be shown to be the same
-- one that was filed - which is the point of filing it.
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    workspace_id  INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    order_id      INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    invoice_id    INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    source        TEXT NOT NULL DEFAULT 'upload',   -- upload | generated
    doc_kind      TEXT,                             -- invoice, nda, ... when known
    title         TEXT NOT NULL,
    filename      TEXT NOT NULL,                    -- on disk, under uploads/docs/<org>/
    original_name TEXT NOT NULL,
    mime          TEXT NOT NULL DEFAULT 'application/octet-stream',
    size          INTEGER NOT NULL DEFAULT 0,
    sha256        TEXT,
    lang          TEXT,
    note          TEXT,
    shared        INTEGER NOT NULL DEFAULT 0,       -- visible to the counterparty
    uploaded_by   INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_docs_org ON documents(org_id, id);
CREATE INDEX IF NOT EXISTS idx_docs_ws ON documents(workspace_id, id);
CREATE INDEX IF NOT EXISTS idx_docs_order ON documents(order_id);

-- Getting paid.
--
-- Until now an invoice was a page that got generated and forgotten. Nothing
-- remembered it existed, so nothing could chase it, nothing could notice the
-- money arriving, and nothing could say whether this company pays on time.
-- That is the spine those three things hang from.
--
-- `pay_ref` is what the payer is asked to put in the transfer narrative, and
-- what the bank statement is matched against. Short and unmistakable on
-- purpose: it is typed by a human into a banking app.
CREATE TABLE IF NOT EXISTS invoices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,  -- who issued it
    customer_org_id INTEGER REFERENCES orgs(id),   -- set when the customer is here too
    workspace_id  INTEGER REFERENCES workspaces(id) ON DELETE SET NULL,
    order_id      INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    number        TEXT NOT NULL,
    pay_ref       TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'invoice',   -- invoice | proforma
    customer      TEXT NOT NULL,
    customer_reg  TEXT,
    customer_vat  TEXT,
    customer_email TEXT,
    customer_addr TEXT,
    lang          TEXT NOT NULL DEFAULT 'en',
    currency      TEXT NOT NULL DEFAULT 'EUR',
    net           REAL NOT NULL DEFAULT 0,
    vat_rate      REAL NOT NULL DEFAULT 0,
    vat_amount    REAL NOT NULL DEFAULT 0,
    gross         REAL NOT NULL DEFAULT 0,
    paid_amount   REAL NOT NULL DEFAULT 0,
    issue_date    TEXT NOT NULL,
    tax_date      TEXT,
    due_date      TEXT,
    status        TEXT NOT NULL DEFAULT 'draft',     -- draft|sent|paid|part|cancelled
    sent_at       TEXT,
    paid_at       TEXT,
    reminders     INTEGER NOT NULL DEFAULT 0,
    last_reminder TEXT,
    lines_json    TEXT NOT NULL DEFAULT '[]',
    query_json    TEXT,                              -- what regenerates the sheet
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_inv_ref ON invoices(pay_ref);
CREATE INDEX IF NOT EXISTS idx_inv_org ON invoices(org_id, status);
CREATE INDEX IF NOT EXISTS idx_inv_cust ON invoices(customer_org_id, status);
CREATE INDEX IF NOT EXISTS idx_inv_due ON invoices(status, due_date);

-- Every letter that went out about money, and every payment that came in. Kept
-- so a second run of the reminder pass does not send the same letter twice, and
-- so "we never chased this" can be answered with a date.
CREATE TABLE IF NOT EXISTS invoice_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id  INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,     -- sent | reminder | overdue | paid | note
    detail      TEXT,
    amount      REAL,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_invev ON invoice_events(invoice_id, id);

-- Money.
--
-- A plan becomes paid for exactly three reasons: a payment processor said so
-- over a signed webhook, the processor confirmed it when asked directly, or the
-- operator of this installation approved a bank transfer they can see in their
-- own statement. Never because the buyer clicked a button saying they paid -
-- that is what this table is for. A claim is a request, not an activation.
CREATE TABLE IF NOT EXISTS billing_claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_id     INTEGER REFERENCES users(id),
    kind        TEXT NOT NULL DEFAULT 'plan',      -- plan | template
    ref         TEXT,                              -- template id, when kind='template'
    plan        TEXT,
    amount      TEXT,
    note        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    decided_by  INTEGER REFERENCES users(id),
    decided_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON billing_claims(status, id);
CREATE INDEX IF NOT EXISTS idx_claims_org ON billing_claims(org_id);

-- What the processor said, kept so the same event arriving twice does not grant
-- two months. Webhooks are re-delivered on purpose and by accident.
CREATE TABLE IF NOT EXISTS billing_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    event_name  TEXT,
    org_id      INTEGER,
    result      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (provider, event_id)
);

-- Finding partners, built on what the audit trail already proves rather than on
-- a directory of self-descriptions.
--
-- A reference publishes only a company's OWN aggregate. Anything involving a
-- counterparty is either absent or reduced to a count, because publishing a
-- partner's performance without their consent would be selling their data.
CREATE TABLE IF NOT EXISTS org_references (
    org_id       INTEGER PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    token        TEXT UNIQUE NOT NULL,
    token_enc    TEXT,
    fields_json  TEXT NOT NULL DEFAULT '{}',
    active       INTEGER NOT NULL DEFAULT 1,
    views        INTEGER NOT NULL DEFAULT 0,
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    published_by INTEGER REFERENCES users(id)
);

-- An introduction is a request to a partner, never a lookup of a stranger.
-- The company being introduced to is not revealed until the partner agrees.
CREATE TABLE IF NOT EXISTS introductions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asker_org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    via_org_id   INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    target_org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    message     TEXT,
    status      TEXT NOT NULL DEFAULT 'requested',  -- requested|approved|declined|connected
    note        TEXT,
    created_by  INTEGER REFERENCES users(id),
    decided_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT,
    UNIQUE (asker_org_id, via_org_id, target_org_id)
);
CREATE INDEX IF NOT EXISTS idx_intro_via ON introductions(via_org_id, status);

-- What a company is looking for, seen by its network rather than the open web.
CREATE TABLE IF NOT EXISTS needs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    detail      TEXT,
    template    TEXT,
    country     TEXT,
    status      TEXT NOT NULL DEFAULT 'open',       -- open|closed
    created_by  INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_needs_open ON needs(status, id DESC);

CREATE TABLE IF NOT EXISTS need_replies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    need_id    INTEGER NOT NULL REFERENCES needs(id) ON DELETE CASCADE,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    message    TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (need_id, org_id)
);

-- Sign-in security.
--
-- A signed cookie on its own can only be revoked by rotating the instance key,
-- which signs everybody out at once. Each sign-in therefore gets a row here,
-- and the cookie carries only its id: a lost laptop is one row to revoke, a
-- password change revokes every row but the one in use, and a company can see
-- where its account is actually signed in.
CREATE TABLE IF NOT EXISTS user_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sid        TEXT UNIQUE NOT NULL,
    ip         TEXT,
    device     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id, revoked_at);

-- A national electronic identity attached to an account.
--
-- The link is made from inside Seam by the account holder, never by the identity
-- provider: anyone holding a national identity could otherwise open an account
-- here claiming to work at any company. `sub` is the provider's own stable
-- subject identifier; the display name is kept only so a person recognises
-- which identity they attached.
CREATE TABLE IF NOT EXISTS user_eid (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider   TEXT NOT NULL,
    sub        TEXT NOT NULL,
    label      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used  TEXT,
    UNIQUE (provider, sub)
);
CREATE INDEX IF NOT EXISTS idx_eid_user ON user_eid(user_id);

-- Second factor. The secret is a TOTP shared secret (RFC 6238); recovery codes
-- are stored hashed, exactly like passwords, because a recovery code that can
-- be read out of a backup is a password in a costume.
CREATE TABLE IF NOT EXISTS user_totp (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    secret      TEXT NOT NULL,
    confirmed   INTEGER NOT NULL DEFAULT 0,
    last_step   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_recovery_codes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    used_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON user_recovery_codes(user_id, used_at);

-- Abuse controls live in the database, not in one process's memory: a restart
-- must not hand an attacker a clean slate, and a second worker must not have
-- its own private idea of how many attempts have been made.
CREATE TABLE IF NOT EXISTS rate_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket TEXT NOT NULL,
    at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_bucket ON rate_events(bucket, at);

CREATE TABLE IF NOT EXISTS login_locks (
    email TEXT PRIMARY KEY,
    fails INTEGER NOT NULL DEFAULT 0,
    until REAL NOT NULL DEFAULT 0,
    last  REAL NOT NULL DEFAULT 0
);

-- Unhandled failures, so a problem is noticed here rather than reported by the
-- customer it happened to.
CREATE TABLE IF NOT EXISTS error_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL DEFAULT (datetime('now')),
    path       TEXT,
    method     TEXT,
    user_id    INTEGER,
    org_id     INTEGER,
    kind       TEXT,
    message    TEXT,
    traceback  TEXT,
    ip         TEXT,
    device     TEXT,
    seen       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_errlog ON error_log(id DESC);

-- Vehicles as first-class objects, not just a field on an order. An imported
-- car is bought, transported, cleared through customs, registered and sold -
-- several records over months - and the thing that stays constant is the VIN.
CREATE TABLE IF NOT EXISTS vehicles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    vin        TEXT NOT NULL,
    make       TEXT,
    model      TEXT,
    year       INTEGER,
    plate      TEXT,
    colour     TEXT,
    fuel       TEXT,
    mileage    INTEGER,
    note       TEXT,
    status     TEXT NOT NULL DEFAULT 'active',
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, vin)
);
CREATE INDEX IF NOT EXISTS idx_vehicle_org ON vehicles(org_id, id DESC);

-- Which records touched which vehicle. One vehicle, many orders over its life.
CREATE TABLE IF NOT EXISTS vehicle_orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    role       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vehicle_id, order_id)
);

-- Web Push subscriptions. One row per browser/device a user has allowed.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint   TEXT UNIQUE NOT NULL,
    p256dh     TEXT,
    auth       TEXT,
    device     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_sent  TEXT,
    last_status TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_user ON push_subscriptions(user_id);

-- Payouts to the other side of a workspace (roadmap item 5: the freelancer /
-- subcontractor portal). The payer records what is owed and marks it paid; the
-- payee sees only their own. Seam does not move money - it records the fact and
-- keeps it next to the work it belongs to.
CREATE TABLE IF NOT EXISTS payouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    order_id     INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    payer_org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    payee_org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    amount       TEXT NOT NULL,
    description  TEXT,
    method       TEXT NOT NULL DEFAULT 'bank',
    reference    TEXT,
    status       TEXT NOT NULL DEFAULT 'due',   -- due | paid | cancelled
    due_date     TEXT,
    note         TEXT,
    created_by   INTEGER REFERENCES users(id),
    paid_by      INTEGER REFERENCES users(id),
    paid_at      TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_payout_ws ON payouts(workspace_id, id DESC);

-- Multi-level approvals (roadmap item 9). A chain is an ordered list of steps;
-- each step names who may decide it. Steps are stored as JSON on the chain and
-- materialised into `approvals` rows when a chain is started on an order.
CREATE TABLE IF NOT EXISTS approval_chains (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    template   TEXT,                       -- NULL = applies to every vertical
    threshold  REAL NOT NULL DEFAULT 0,    -- only required above this agreed value
    steps_json TEXT NOT NULL DEFAULT '[]',
    active     INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per step per order: the decision record. Never updated after a
-- decision is taken except to carry that decision.
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    chain_id    INTEGER REFERENCES approval_chains(id) ON DELETE SET NULL,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    step        INTEGER NOT NULL,
    label       TEXT NOT NULL,
    approver_user_id INTEGER REFERENCES users(id),
    approver_role    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected | skipped
    note        TEXT,
    decided_by  INTEGER REFERENCES users(id),
    decided_at  TEXT,
    ip          TEXT,
    device      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (order_id, chain_id, step)
);
CREATE INDEX IF NOT EXISTS idx_appr_order ON approvals(order_id, step);

-- Workflow automation (roadmap item 3): "when this happens, do that".
-- Rules belong to an organisation and only ever fire on that org's own events.
CREATE TABLE IF NOT EXISTS automations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    conditions_json TEXT NOT NULL DEFAULT '[]',
    actions_json TEXT NOT NULL DEFAULT '[]',
    active       INTEGER NOT NULL DEFAULT 1,
    runs         INTEGER NOT NULL DEFAULT 0,
    last_run     TEXT,
    last_status  TEXT,
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_auto_org ON automations(org_id, trigger, active);

-- Every firing is recorded, so an automated action is as auditable as a manual one.
CREATE TABLE IF NOT EXISTS automation_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    automation_id INTEGER NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    workspace_id  INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    order_id      INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    trigger       TEXT NOT NULL,
    status        TEXT NOT NULL,
    detail        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_autorun ON automation_runs(automation_id, id DESC);

-- Per-organisation connection to a qualified trust service provider.
-- Credentials come from a contract with the provider; Seam only stores them.
CREATE TABLE IF NOT EXISTS org_qtsp (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id           INTEGER NOT NULL UNIQUE REFERENCES orgs(id) ON DELETE CASCADE,
    provider         TEXT NOT NULL,
    base_url         TEXT,
    client_id        TEXT,
    client_secret    TEXT,
    api_key          TEXT,
    relying_party_id TEXT,
    client_cert      TEXT,
    client_key       TEXT,
    updated_by       INTEGER REFERENCES users(id),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which document templates an org has already used (free plan is capped)
CREATE TABLE IF NOT EXISTS doc_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (org_id, kind)
);

-- Outbound webhooks: Seam pushes events to the software a company already uses
CREATE TABLE IF NOT EXISTS webhooks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    secret      TEXT NOT NULL,
    label       TEXT,
    events_json TEXT NOT NULL DEFAULT '["*"]',
    active      INTEGER NOT NULL DEFAULT 1,
    last_status TEXT,
    last_at     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_webhooks_org ON webhooks(org_id);

-- Workspace-level communication channel (not tied to a single order)
CREATE TABLE IF NOT EXISTS ws_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_wsmsg ON ws_messages(workspace_id, id);

-- How far each person has read each conversation. Kept apart from
-- notifications: a notification is dismissed once, from any screen, while
-- "read up to here" is what makes an inbox usable a week later.
CREATE TABLE IF NOT EXISTS ws_reads (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id INTEGER NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    last_id      INTEGER NOT NULL DEFAULT 0,
    at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, workspace_id)
);

-- Progress evidence: photos/videos attached to an order, visible to both sides
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    filename      TEXT NOT NULL,
    original_name TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('image','video')),
    size          INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_att_order ON attachments(order_id, id);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id INTEGER REFERENCES workspaces(id) ON DELETE CASCADE,
    order_id     INTEGER REFERENCES orders(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    body         TEXT NOT NULL,
    meta_json    TEXT NOT NULL DEFAULT '{}',
    read         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- E-commerce fulfillment: physical products (stock) and digital goods (key vault)
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL CHECK (type IN ('physical','digital')),
    sku        TEXT,
    price      TEXT,
    stock      INTEGER NOT NULL DEFAULT 0,
    image      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    code       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'available',
    order_id   INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS store_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    ref          TEXT NOT NULL,
    customer     TEXT,
    product_id   INTEGER REFERENCES products(id),
    qty          INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'new',
    note         TEXT,
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    fulfilled_at TEXT
);

-- Inbound integration keys: secret keys for webhooks/API, public tokens for the hosted order form
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    key          TEXT UNIQUE NOT NULL,
    label        TEXT,
    kind         TEXT NOT NULL DEFAULT 'secret',
    revoked      INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_apikeys_key ON api_keys(key);

CREATE INDEX IF NOT EXISTS idx_products_org    ON products(org_id);
CREATE INDEX IF NOT EXISTS idx_pkeys_product   ON product_keys(product_id, status);
CREATE INDEX IF NOT EXISTS idx_sorders_org     ON store_orders(org_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_ws       ON orders(workspace_id);
CREATE INDEX IF NOT EXISTS idx_terms_order     ON order_terms(order_id);
CREATE INDEX IF NOT EXISTS idx_events_ws       ON events(workspace_id, id);
CREATE INDEX IF NOT EXISTS idx_events_order    ON events(order_id, id);
CREATE INDEX IF NOT EXISTS idx_comments_order  ON comments(order_id, id);
CREATE INDEX IF NOT EXISTS idx_notif_user      ON notifications(user_id, read);
CREATE INDEX IF NOT EXISTS idx_memberships_u   ON memberships(user_id);
"""


#: How long a connection waits for a writer to finish before giving up. Without
#: it SQLite raises "database is locked" the instant two requests write at once,
#: which under a threaded server is a matter of when, not if.
BUSY_TIMEOUT_MS = 8000


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = %d;" % BUSY_TIMEOUT_MS)
    # WAL lets readers work while one writer holds the lock; it is set on the
    # database file itself at init, and re-asserted here so a database restored
    # from a copy is not silently left in rollback-journal mode.
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


@contextmanager
def db_cursor():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Lightweight, idempotent migrations for older databases."""
    for col, decl in (("tax_profit_rate", "REAL"), ("tax_payroll_rate", "REAL"),
                      ("vat_exempt", "INTEGER NOT NULL DEFAULT 0"),
                      ("vat_exempt_reason", "TEXT"),
                      ("profit_tax_exempt", "INTEGER NOT NULL DEFAULT 0"),
                      ("tax_reviewed_at", "TEXT"),
                      ("posture", "TEXT"), ("headline", "TEXT"),
                      ("size_band", "TEXT"), ("founded", "TEXT"),
                      ("areas_json", "TEXT"), ("contact_email", "TEXT"),
                      ("listed", "INTEGER NOT NULL DEFAULT 0")):
        if col not in [r["name"] for r in conn.execute("PRAGMA table_info(orgs)").fetchall()]:
            conn.execute("ALTER TABLE orgs ADD COLUMN %s %s" % (col, decl))

    tcols = [r["name"] for r in conn.execute("PRAGMA table_info(order_terms)").fetchall()]
    if "agreed_at" not in tcols:
        conn.execute("ALTER TABLE order_terms ADD COLUMN agreed_at TEXT")
        # Everything already agreed is dated from its last edit. Imperfect, and
        # the only honest answer available: the moment of agreement was not
        # recorded before this column existed.
        conn.execute("UPDATE order_terms SET agreed_at=updated_at WHERE state='agreed'")
    if "review_every_days" not in tcols:
        conn.execute("ALTER TABLE order_terms ADD COLUMN review_every_days INTEGER")
    ncols = [r["name"] for r in conn.execute("PRAGMA table_info(notifications)").fetchall()]
    if "meta_json" not in ncols:
        conn.execute("ALTER TABLE notifications ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
    ucols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "verified" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    if "verify_token" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN verify_token TEXT")
    ocols = [r["name"] for r in conn.execute("PRAGMA table_info(orgs)").fetchall()]
    if "reg_number" not in ocols:
        conn.execute("ALTER TABLE orgs ADD COLUMN reg_number TEXT")
    if "country" not in ocols:
        conn.execute("ALTER TABLE orgs ADD COLUMN country TEXT")
    if "verified_company" not in ocols:
        conn.execute("ALTER TABLE orgs ADD COLUMN verified_company INTEGER NOT NULL DEFAULT 0")
    socols = [r["name"] for r in conn.execute("PRAGMA table_info(store_orders)").fetchall()]
    if socols and "source" not in socols:
        conn.execute("ALTER TABLE store_orders ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    pcols = [r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
    if pcols and "image" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN image TEXT")
    if "lang" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN lang TEXT")
    ecols = [r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
    for col in ("ip", "device"):
        if ecols and col not in ecols:
            conn.execute("ALTER TABLE events ADD COLUMN %s TEXT" % col)
    if "cal_token" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN cal_token TEXT")
    if "phone" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if "notify_json" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN notify_json TEXT")
    if "reset_token" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN reset_token TEXT")
    if "reset_expires" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN reset_expires TEXT")
    if "plan" not in ocols:
        conn.execute("ALTER TABLE orgs ADD COLUMN plan TEXT NOT NULL DEFAULT 'starter'")
    if "plan_until" not in ocols:
        conn.execute("ALTER TABLE orgs ADD COLUMN plan_until TEXT")
    if "contact_json" not in ocols:
        conn.execute("ALTER TABLE orgs ADD COLUMN contact_json TEXT")
    # What the processor knows this company as, so a webhook arriving later can
    # be tied back to it without trusting anything the browser sends.
    for col in ("pay_session", "pay_customer", "pay_method", "sectors_json",
                "about", "website", "logo", "org_links_json",
                # Where this company is paid, filled in once and carried onto
                # every invoice it issues.
                "pay_iban", "pay_bic", "pay_bank", "pay_terms", "pay_link",
                "remind_json"):
        if col not in ocols:
            conn.execute("ALTER TABLE orgs ADD COLUMN %s TEXT" % col)
    for col in ("job_title", "bio", "avatar", "links_json"):
        if col not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN %s TEXT" % col)
    # Roles used to be cosmetic - every membership was written as 'admin'. Make
    # the first member of each company its owner so the role that cannot be
    # taken away actually belongs to someone.
    if not conn.execute("SELECT 1 FROM memberships WHERE role='owner' LIMIT 1").fetchone():
        for row in conn.execute(
                "SELECT org_id, MIN(id) AS first_id FROM memberships GROUP BY org_id").fetchall():
            conn.execute("UPDATE memberships SET role='owner' WHERE id=?", (row["first_id"],))
    sgcols = [r["name"] for r in conn.execute("PRAGMA table_info(signatures)").fetchall()]
    for col in ("qtsp_provider", "qtsp_ref", "qtsp_state", "qtsp_signature", "qtsp_cert"):
        if sgcols and col not in sgcols:
            conn.execute("ALTER TABLE signatures ADD COLUMN %s TEXT" % col)
    ctcols = [r["name"] for r in conn.execute("PRAGMA table_info(custom_templates)").fetchall()]
    for col in ("pay_method", "invoice_no", "stripe_session"):
        if ctcols and col not in ctcols:
            conn.execute("ALTER TABLE custom_templates ADD COLUMN %s TEXT" % col)
    _migrate_tokens(conn)


# --------------------------------------------------------------------------- #
#  Bearer tokens at rest
#
#  A session id, a password-reset link, a calendar feed URL, a signing link: all
#  of them are bearer tokens - whoever holds the string is treated as the
#  person. Stored in the clear, a copy of this file hands over every live
#  session and every outstanding link.
#
#  They are all looked up *by value*, so the stored form can be a SHA-256 of the
#  token and the lookup still works: the URL a customer already has keeps
#  functioning, and the database no longer contains anything usable. SHA-256
#  rather than a slow hash on purpose - these are 128 bits or more of random,
#  so there is nothing to brute-force and nothing to gain from making every
#  request expensive.
#
#  Three of them have to be shown again after they are issued (a calendar feed
#  must keep the same URL, a signing link gets re-sent, a reference link is
#  copied from the screen). Those keep an encrypted second copy alongside the
#  hash, so display works and a stolen database without the data key does not.
# --------------------------------------------------------------------------- #
#: table -> (lookup column, encrypted copy or None, primary key)
TOKEN_COLUMNS = {
    "user_sessions": ("sid", None, "id"),
    "invites": ("token", None, "id"),
    # api_keys is handled separately: a `pk_` key belongs in a public URL and
    # is stored as it is, only `sk_` keys are hashed.
    "signatures": ("token", "token_enc", "id"),
    "org_references": ("token", "token_enc", "org_id"),
}
#: users has three of them and needs naming separately.
USER_TOKEN_COLUMNS = {"reset_token": None, "verify_token": None,
                      "cal_token": "cal_token_enc"}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _migrate_tokens(conn):
    import cryptobox                                  # stdlib only, no cycle

    def add_col(table, col):
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
        if cols and col not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s TEXT" % (table, col))
        return bool(cols)

    def convert(table, col, enc_col, pk="id"):
        if enc_col and not add_col(table, enc_col):
            return
        try:
            rows = conn.execute("SELECT %s AS pk, %s AS t FROM %s WHERE %s IS NOT NULL"
                                % (pk, col, table, col)).fetchall()
        except sqlite3.Error:
            return                                    # table not present yet
        for r in rows:
            raw = r["t"]
            # Already converted: a stored hash is 64 lowercase hex characters,
            # which token_urlsafe cannot produce.
            if not raw or _HEX64.match(raw):
                continue
            if enc_col:
                conn.execute("UPDATE %s SET %s=?, %s=? WHERE %s=?"
                             % (table, col, enc_col, pk),
                             (token_hash(raw), cryptobox.encrypt_field(raw), r["pk"]))
            else:
                conn.execute("UPDATE %s SET %s=? WHERE %s=?" % (table, col, pk),
                             (token_hash(raw), r["pk"]))

    for table, (col, enc_col, pk) in TOKEN_COLUMNS.items():
        convert(table, col, enc_col, pk)
    for col, enc_col in USER_TOKEN_COLUMNS.items():
        convert("users", col, enc_col)

    try:
        rows = conn.execute("SELECT id, key FROM api_keys WHERE kind='secret'").fetchall()
    except sqlite3.Error:
        return
    for r in rows:
        if r["key"] and not _HEX64.match(r["key"]):
            conn.execute("UPDATE api_keys SET key=? WHERE id=?",
                         (token_hash(r["key"]), r["id"]))


def token_hash(token):
    """The stored form of a bearer token."""
    if not token:
        return None
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def row_to_dict(row):
    return dict(row) if row is not None else None


def rows_to_list(rows):
    return [dict(r) for r in rows]
