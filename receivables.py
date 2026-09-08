# -*- coding: utf-8 -*-
"""Getting paid.

Three things a generated page cannot do: chase itself, notice the money
arriving, and prove afterwards that this company pays on time. All three need
the invoice to be a record, and all three are here.

The last one is the reason this is worth building rather than buying. Every
accounting product can print an invoice and email a reminder. None of them can
tell a new counterparty **how fast you actually pay**, because none of them
holds both sides of the trade. Seam does: when the customer is also here, the
same invoice row is the seller's receivable and the buyer's obligation, and the
days between issuing and settling are a fact rather than a claim.

Nothing in here reveals a counterparty. A punctuality figure is a company's own
aggregate over its own behaviour, which is the same rule the trade reference
already follows.
"""
import re
import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

#: When to chase, in days relative to the due date. Negative is before.
DEFAULT_REMINDERS = (-3, 1, 7, 14)
#: After this many chases, stop. A letter a week forever is not a dunning
#: process, it is a spam folder.
MAX_REMINDERS = 5
#: Reference shown to the payer and matched against the bank narrative. Short,
#: unambiguous in print, and free of characters a banking form will strip.
REF_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"


def make_ref(number, salt):
    """A reference the payer types and the statement is matched on.

    Built from the invoice number where that is usable, because a customer who
    sees their own invoice number in the narrative knows what they are paying.
    A random tail keeps two companies numbering INV-1 apart.
    """
    core = re.sub(r"[^A-Za-z0-9]", "", str(number or "")).upper()[:12]
    tail = "".join(REF_ALPHABET[b % len(REF_ALPHABET)] for b in salt[:4])
    return ("%s-%s" % (core, tail)) if core else ("SEAM-%s" % tail)


def _d(x):
    try:
        return Decimal(str(x or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def totals(lines, vat_rate, reverse_charge=False):
    """Net, VAT and gross from the lines. One place, so the sheet, the record
    and the reminder can never disagree about what is owed."""
    net = Decimal(0)
    for ln in lines or []:
        qty, price = ln.get("qty"), ln.get("price")
        if qty not in (None, "") and price not in (None, ""):
            net += (_d(qty) * _d(price)).quantize(Decimal("0.01"))
        else:
            net += _d(ln.get("amount"))
    rate = Decimal(0) if reverse_charge else _d(vat_rate)
    vat = (net * rate / Decimal(100)).quantize(Decimal("0.01"))
    return float(net), float(vat), float(net + vat)


def due_date(issue, terms_days):
    d = _date(issue)
    if not d or not terms_days:
        return ""
    try:
        return (d + timedelta(days=max(0, min(365, int(terms_days))))).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def reminder_plan(raw):
    """The days a company chases on. A bad or missing setting falls back to the
    default rather than to silence: an invoice nobody chases is the failure
    this whole module exists to prevent."""
    try:
        got = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return list(DEFAULT_REMINDERS)
    days = []
    for x in got if isinstance(got, list) else []:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if -60 <= n <= 180 and n not in days:
            days.append(n)
    return sorted(days) or list(DEFAULT_REMINDERS)


def next_reminder(invoice, plan, today=None):
    """Which chase is due for this invoice right now, or None.

    Returns the offset from the plan that has come round and has not been sent,
    so a pass that runs twice in a day sends nothing the second time.
    """
    if invoice["status"] not in ("sent", "part"):
        return None
    due = _date(invoice["due_date"])
    if not due:
        return None
    today = today or datetime.utcnow().date()
    sent = int(invoice["reminders"] or 0)
    if sent >= MAX_REMINDERS:
        return None
    # One a day at most, whatever the plan says.
    last = _date(invoice["last_reminder"])
    if last and last >= today:
        return None
    passed = [d for d in plan if due + timedelta(days=d) <= today]
    if len(passed) <= sent:
        return None
    return passed[sent]


def match_receivables(transactions, invoices):
    """Pair incoming credits with unpaid invoices.

    The reference is the strong signal: it was printed on the invoice for
    exactly this purpose. Amount alone is offered only when a single invoice
    has that amount outstanding - two customers owing 1 200 € must never be
    settled against each other by a machine.

    Partial payments are reported as such rather than swallowed: an invoice
    half paid is a different conversation from one paid.
    """
    out, used = [], set()
    for tx in transactions:
        if not tx.get("credit"):
            continue
        amount = _d(tx.get("amount"))
        narrative = _norm("%s %s" % (tx.get("reference", ""), tx.get("counterparty", "")))
        hit, why = None, ""

        for inv in invoices:
            if inv["id"] in used or inv["status"] not in ("sent", "part"):
                continue
            ref = _norm(inv["pay_ref"])
            if ref and len(ref) >= 5 and ref in narrative:
                hit, why = inv, "reference"
                break
        if hit is None:
            # The invoice number itself, because customers type that instead.
            for inv in invoices:
                if inv["id"] in used or inv["status"] not in ("sent", "part"):
                    continue
                num = _norm(inv["number"])
                if num and len(num) >= 5 and num in narrative:
                    hit, why = inv, "number"
                    break
        if hit is None:
            same = [i for i in invoices
                    if i["id"] not in used and i["status"] in ("sent", "part")
                    and abs(_d(i["gross"]) - _d(i["paid_amount"]) - amount) < Decimal("0.01")]
            if len(same) == 1:
                hit, why = same[0], "amount"
            elif len(same) > 1:
                why = "ambiguous"

        settles = ""
        if hit is not None:
            used.add(hit["id"])
            outstanding = _d(hit["gross"]) - _d(hit["paid_amount"])
            if abs(outstanding - amount) < Decimal("0.01"):
                settles = "full"
            elif amount < outstanding:
                settles = "part"
            else:
                settles = "over"
        out.append({"transaction": tx, "invoice_id": hit["id"] if hit else None,
                    "number": hit["number"] if hit else "", "reason": why,
                    "settles": settles, "amount": str(amount)})
    return out


def punctuality(rows, today=None):
    """How this company behaves about money, as its own aggregate.

    `rows` are invoices where this company is the payer. The output names
    nobody: a count, an average, and a share settled by the due date. That is
    the most that can be published about a trading relationship without
    publishing the other party's business along with it.

    Returned as None below three invoices - an average of one is not a record,
    it is an anecdote, and presenting it as a record would be the dishonest
    part of every reputation system.
    """
    today = today or datetime.utcnow().date()
    days, on_time, counted = [], 0, 0
    for r in rows:
        issued = _date(r["issue_date"])
        due = _date(r["due_date"])
        paid = _date(r["paid_at"])
        if not issued or not paid:
            continue
        counted += 1
        days.append(max(0, (paid - issued).days))
        if due and paid <= due:
            on_time += 1
    if counted < 3:
        return None
    return {"invoices": counted,
            "avg_days": round(sum(days) / float(len(days)), 1) if days else None,
            "on_time_pct": int(round(100.0 * on_time / counted))}


def band(total):
    """Money as a band, never as a figure. Publishing the exact number a
    company turns over hands its competitors a spreadsheet."""
    t = float(total or 0)
    for edge, label in ((10000, "<10k"), (50000, "10-50k"), (250000, "50-250k"),
                        (1000000, "250k-1M")):
        if t < edge:
            return label
    return "1M+"


def self_test():
    lines = [{"qty": "250", "price": "4.20"}, {"qty": "16", "price": "35"}]
    net, vat, gross = totals(lines, 20)
    assert (net, vat, gross) == (1610.0, 322.0, 1932.0), (net, vat, gross)
    assert totals(lines, 20, reverse_charge=True) == (1610.0, 0.0, 1610.0)
    assert due_date("2026-08-01", 14) == "2026-08-15"

    inv = {"id": 1, "number": "INV-77", "pay_ref": "INV77-ACDE", "status": "sent",
           "gross": 1932.0, "paid_amount": 0.0}
    tx = [{"credit": True, "amount": "1932.00", "reference": "plateno INV77-ACDE",
           "counterparty": "Muster GmbH"}]
    m = match_receivables(tx, [inv])
    assert m[0]["invoice_id"] == 1 and m[0]["reason"] == "reference", m
    assert m[0]["settles"] == "full", m

    # Two invoices for the same amount must not be guessed at.
    a = dict(inv, id=1, pay_ref="AAA-1111", number="INV-1")
    b = dict(inv, id=2, pay_ref="BBB-2222", number="INV-2")
    m = match_receivables([{"credit": True, "amount": "1932.00",
                            "reference": "transfer", "counterparty": "x"}], [a, b])
    assert m[0]["invoice_id"] is None and m[0]["reason"] == "ambiguous", m

    rows = [{"issue_date": "2026-01-01", "due_date": "2026-01-15", "paid_at": "2026-01-10"},
            {"issue_date": "2026-02-01", "due_date": "2026-02-15", "paid_at": "2026-02-20"},
            {"issue_date": "2026-03-01", "due_date": "2026-03-15", "paid_at": "2026-03-11"}]
    p = punctuality(rows)
    assert p["invoices"] == 3 and p["on_time_pct"] == 67, p
    assert punctuality(rows[:2]) is None
    assert band(9999) == "<10k" and band(1200000) == "1M+"
    return {"ok": True}


if __name__ == "__main__":
    print(self_test())
