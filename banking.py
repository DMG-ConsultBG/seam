# -*- coding: utf-8 -*-
"""
Banking, through the formats banks already speak.

A live bank API means PSD2 registration, a QWAC certificate and a per-bank
contract - not something a self-hosted instance can assume. What every business
bank in Europe does offer, today, without asking anyone's permission:

  in   CAMT.053 (ISO 20022 XML) or MT940 (SWIFT text) account statements
  out  pain.001 (ISO 20022 XML) SEPA credit transfer instructions

So Seam reads a statement and matches it against what is outstanding, and writes
a transfer file the accountant uploads to the bank's own portal. That closes the
loop the roadmap asks for - "was this paid?" answered from the bank's own record
rather than from someone's memory - without pretending to hold banking licences.

Everything here is stdlib: ElementTree for XML, plain parsing for MT940.
"""
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation

MAX_STATEMENT_BYTES = 8 * 1024 * 1024


class StatementError(Exception):
    pass


def _dec(raw):
    try:
        return Decimal(str(raw).replace(",", ".").strip())
    except (InvalidOperation, AttributeError):
        return Decimal("0")


def _local(tag):
    """ElementTree keeps namespaces in the tag; CAMT versions differ, names do not."""
    return tag.rsplit("}", 1)[-1]


def _find(elem, *names):
    """Depth-first search by local name, so camt.053.001.02 and .08 both parse."""
    for child in elem:
        if _local(child.tag) in names:
            return child
    for child in elem:
        found = _find(child, *names)
        if found is not None:
            return found
    return None


def _findall(elem, name):
    out = []
    for child in elem:
        if _local(child.tag) == name:
            out.append(child)
        out.extend(_findall(child, name))
    return out


def _text(elem, *names):
    found = _find(elem, *names) if elem is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


# --------------------------------------------------------------------------- #
#  CAMT.053
# --------------------------------------------------------------------------- #
def parse_camt(raw):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise StatementError("Файлът не е валиден XML: %s" % str(e)[:120])
    entries = _findall(root, "Ntry")
    if not entries:
        raise StatementError("Не са намерени движения (Ntry) - това CAMT.053 файл ли е?")
    out = []
    for n in entries:
        amt_el = _find(n, "Amt")
        amount = _dec(amt_el.text if amt_el is not None else 0)
        currency = (amt_el.get("Ccy") if amt_el is not None else "") or ""
        credit = _text(n, "CdtDbtInd").upper() == "CRDT"
        booking = _find(n, "BookgDt") or _find(n, "ValDt")
        date = _text(booking, "Dt", "DtTm")[:10] if booking is not None else ""
        # Remittance information is where the payment reference actually lives.
        ref = " ".join(x.text.strip() for x in _findall(n, "Ustrd") if x.text)
        if not ref:
            ref = _text(n, "AddtlNtryInf") or _text(n, "EndToEndId") or ""
        party = _find(n, "Dbtr") if credit else _find(n, "Cdtr")
        out.append({
            "date": date, "amount": str(amount), "currency": currency,
            "credit": credit, "reference": ref[:300],
            "counterparty": _text(party, "Nm") if party is not None else "",
            "bank_ref": _text(n, "AcctSvcrRef") or _text(n, "EndToEndId"),
        })
    return out


# --------------------------------------------------------------------------- #
#  MT940
# --------------------------------------------------------------------------- #
_MT940_LINE = re.compile(
    r"^:61:(?P<value>\d{6})(?P<entry>\d{4})?(?P<dc>[CD])(?P<funds>[A-Z])?"
    r"(?P<amount>[\d,]+)(?P<code>[A-Z0-9]{4})(?P<ref>[^\r\n]*)")


def parse_mt940(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    lines = raw.replace("\r\n", "\n").split("\n")
    out, current = [], None
    for line in lines:
        m = _MT940_LINE.match(line)
        if m:
            if current:
                out.append(current)
            yy = int(m.group("value")[:2])
            year = 2000 + yy if yy < 80 else 1900 + yy
            current = {
                "date": "%04d-%s-%s" % (year, m.group("value")[2:4], m.group("value")[4:6]),
                "amount": str(_dec(m.group("amount"))),
                "currency": "", "credit": m.group("dc") == "C",
                "reference": (m.group("ref") or "").strip()[:300],
                "counterparty": "", "bank_ref": "",
            }
        elif line.startswith(":86:") and current is not None:
            current["reference"] = (current["reference"] + " " + line[4:].strip()).strip()[:300]
        elif current is not None and line.startswith("    ") and not line.startswith(":"):
            current["reference"] = (current["reference"] + " " + line.strip()).strip()[:300]
    if current:
        out.append(current)
    if not out:
        raise StatementError("Не са намерени движения (:61:) - това MT940 файл ли е?")
    return out


def parse_statement(raw):
    """Detect the format rather than making the user declare it."""
    if isinstance(raw, bytes):
        head = raw[:400].decode("utf-8", "replace")
    else:
        head, raw = raw[:400], raw
    if "<" in head and ("Document" in head or "camt" in head.lower() or "?xml" in head):
        return "camt.053", parse_camt(raw)
    return "mt940", parse_mt940(raw)


# --------------------------------------------------------------------------- #
#  Matching
# --------------------------------------------------------------------------- #
def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def match_payouts(transactions, payouts, amount_of):
    """Pair incoming credits with outstanding payouts.

    A reference that appears in the bank narrative is a strong match; amount
    alone is only offered when exactly one outstanding item has that amount, so
    two identical invoices are never silently matched to the wrong one."""
    results = []
    used = set()
    for tx in transactions:
        if not tx["credit"]:
            continue
        tx_amount = _dec(tx["amount"])
        narrative = _norm(tx["reference"] + " " + tx["counterparty"])
        best, why = None, ""

        for p in payouts:
            if p["id"] in used or p["status"] != "due":
                continue
            ref = _norm(p.get("reference"))
            if ref and len(ref) >= 4 and ref in narrative:
                best, why = p, "reference"
                break
        if best is None:
            same = [p for p in payouts
                    if p["id"] not in used and p["status"] == "due"
                    and abs(amount_of(p) - tx_amount) < Decimal("0.01")]
            if len(same) == 1:
                best, why = same[0], "amount"
            elif len(same) > 1:
                why = "ambiguous"

        if best is not None:
            used.add(best["id"])
        results.append({"transaction": tx, "payout_id": best["id"] if best else None,
                        "payout_amount": str(amount_of(best)) if best else "",
                        "reason": why})
    return results


# --------------------------------------------------------------------------- #
#  pain.001 (SEPA credit transfer)
# --------------------------------------------------------------------------- #
IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")


def valid_iban(iban):
    """ISO 13616 mod-97 check - catches a mistyped account before the bank does."""
    s = re.sub(r"\s+", "", str(iban or "")).upper()
    if not IBAN_RE.match(s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) if ch.isalpha() else ch for ch in rearranged)
    return int(digits) % 97 == 1


def _el(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = str(text)
    return e


def build_pain001(debtor_name, debtor_iban, debtor_bic, payments, msg_id=None):
    """payments: [{"name","iban","bic","amount","currency","reference","end_to_end"}]"""
    if not valid_iban(debtor_iban):
        raise StatementError("Невалиден IBAN на наредителя")
    for p in payments:
        if not valid_iban(p.get("iban")):
            raise StatementError("Невалиден IBAN за %s" % (p.get("name") or "получател"))
    ns = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"
    ET.register_namespace("", ns)
    root = ET.Element("{%s}Document" % ns)
    init = _el(root, "CstmrCdtTrfInitn")
    now = datetime.utcnow()
    msg_id = msg_id or ("SEAM-%s" % uuid.uuid4().hex[:20].upper())
    total = sum(_dec(p["amount"]) for p in payments)

    hdr = _el(init, "GrpHdr")
    _el(hdr, "MsgId", msg_id)
    _el(hdr, "CreDtTm", now.strftime("%Y-%m-%dT%H:%M:%S"))
    _el(hdr, "NbOfTxs", len(payments))
    _el(hdr, "CtrlSum", "%.2f" % total)
    _el(_el(hdr, "InitgPty"), "Nm", debtor_name)

    inf = _el(init, "PmtInf")
    _el(inf, "PmtInfId", msg_id)
    _el(inf, "PmtMtd", "TRF")
    _el(inf, "BtchBookg", "false")
    _el(inf, "NbOfTxs", len(payments))
    _el(inf, "CtrlSum", "%.2f" % total)
    tp = _el(inf, "PmtTpInf")
    _el(_el(tp, "SvcLvl"), "Cd", "SEPA")
    _el(inf, "ReqdExctnDt", now.strftime("%Y-%m-%d"))
    dbtr = _el(inf, "Dbtr")
    _el(dbtr, "Nm", debtor_name)
    _el(_el(_el(inf, "DbtrAcct"), "Id"), "IBAN", re.sub(r"\s+", "", debtor_iban).upper())
    if debtor_bic:
        _el(_el(_el(inf, "DbtrAgt"), "FinInstnId"), "BIC", debtor_bic.upper())
    _el(inf, "ChrgBr", "SLEV")

    for p in payments:
        tx = _el(inf, "CdtTrfTxInf")
        pid = _el(tx, "PmtId")
        _el(pid, "EndToEndId", (p.get("end_to_end") or p.get("reference") or "NOTPROVIDED")[:35])
        amt = _el(_el(tx, "Amt"), "InstdAmt", "%.2f" % _dec(p["amount"]))
        amt.set("Ccy", (p.get("currency") or "EUR")[:3].upper())
        if p.get("bic"):
            _el(_el(_el(tx, "CdtrAgt"), "FinInstnId"), "BIC", p["bic"].upper())
        _el(_el(tx, "Cdtr"), "Nm", (p.get("name") or "")[:70])
        _el(_el(_el(tx, "CdtrAcct"), "Id"), "IBAN", re.sub(r"\s+", "", p["iban"]).upper())
        if p.get("reference"):
            _el(_el(tx, "RmtInf"), "Ustrd", str(p["reference"])[:140])

    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(root, encoding="unicode")), msg_id
