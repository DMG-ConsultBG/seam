# -*- coding: utf-8 -*-
"""Banking: real CAMT.053 and MT940 parsing, careful matching, valid pain.001."""
import os, sys, os, json, re
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the app package
from seamclient import Client
import banking

c = Client(); c.login()

CAMT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
 <BkToCstmrStmt><Stmt><Id>ST-1</Id>
  <Ntry><Amt Ccy="EUR">1250.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
    <BookgDt><Dt>2026-08-01</Dt></BookgDt><AcctSvcrRef>BK-1</AcctSvcrRef>
    <NtryDtls><TxDtls><RmtInf><Ustrd>Plateja po INV-2026-014</Ustrd></RmtInf>
      <RltdPties><Dbtr><Nm>BuildPro Contractors</Nm></Dbtr></RltdPties>
    </TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="EUR">77.50</Amt><CdtDbtInd>DBIT</CdtDbtInd>
    <BookgDt><Dt>2026-08-01</Dt></BookgDt>
    <NtryDtls><TxDtls><RmtInf><Ustrd>Bank charges</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
  <Ntry><Amt Ccy="EUR">640.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>
    <BookgDt><Dt>2026-08-02</Dt></BookgDt>
    <NtryDtls><TxDtls><RmtInf><Ustrd>bez osnovanie</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
 </Stmt></BkToCstmrStmt></Document>"""

MT940 = """:20:STMT
:25:BG80BNBG96611020345678
:28C:1/1
:60F:C260801EUR1000,00
:61:2608010801C1250,00NTRFINV-2026-014//BK-1
:86:Plateja po INV-2026-014 ot BuildPro
:61:2608020802D77,50NCHGCHARGES//BK-2
:86:Bank charges
:62F:C260802EUR2172,50
"""

print("== CAMT.053 ==")
fmt, txs = banking.parse_statement(CAMT)
c.check("detected as camt.053", fmt == "camt.053", fmt)
c.check("three movements", len(txs) == 3, str(len(txs)))
c.check("credits and debits distinguished", [t["credit"] for t in txs] == [True, False, True],
        str([t["credit"] for t in txs]))
c.check("amount parsed", txs[0]["amount"] == "1250.00", txs[0]["amount"])
c.check("currency from the Ccy attribute", txs[0]["currency"] == "EUR", txs[0]["currency"])
c.check("booking date", txs[0]["date"] == "2026-08-01", txs[0]["date"])
c.check("remittance info kept", "INV-2026-014" in txs[0]["reference"], txs[0]["reference"])
c.check("counterparty read", txs[0]["counterparty"] == "BuildPro Contractors",
        txs[0]["counterparty"])

print("\n== MT940 ==")
fmt2, txs2 = banking.parse_statement(MT940)
c.check("detected as mt940", fmt2 == "mt940", fmt2)
c.check("two movements", len(txs2) == 2, str(len(txs2)))
c.check("credit flag", txs2[0]["credit"] is True and txs2[1]["credit"] is False,
        str([t["credit"] for t in txs2]))
c.check("comma decimal handled", txs2[0]["amount"] == "1250.00", txs2[0]["amount"])
c.check("two-digit year expanded", txs2[0]["date"] == "2026-08-01", txs2[0]["date"])
c.check(":86: narrative folded in", "BuildPro" in txs2[0]["reference"], txs2[0]["reference"])

print("\n== a file that is neither is rejected clearly ==")
try:
    banking.parse_statement("just some text")
    c.check("rejected", False, "no error raised")
except banking.StatementError as e:
    c.check("rejected with a readable message", "MT940" in str(e) or ":61:" in str(e), str(e))

print("\n== matching is careful ==")
from decimal import Decimal
payouts = [
    {"id": 1, "status": "due", "reference": "INV-2026-014", "amount": "EUR 1250"},
    {"id": 2, "status": "due", "reference": "", "amount": "EUR 640"},
    {"id": 3, "status": "due", "reference": "", "amount": "EUR 999"},
]
m = banking.match_payouts(txs, payouts, lambda p: Decimal(p["amount"].split()[1]))
c.check("debits are ignored", len(m) == 2, str(len(m)))
c.check("matched by reference", m[0]["payout_id"] == 1 and m[0]["reason"] == "reference", str(m[0]))
c.check("matched by unique amount", m[1]["payout_id"] == 2 and m[1]["reason"] == "amount", str(m[1]))

ambiguous = [{"id": 1, "status": "due", "reference": "", "amount": "EUR 640"},
             {"id": 2, "status": "due", "reference": "", "amount": "EUR 640"}]
m2 = banking.match_payouts([txs[2]], ambiguous, lambda p: Decimal(p["amount"].split()[1]))
c.check("two identical amounts are NOT silently matched",
        m2[0]["payout_id"] is None and m2[0]["reason"] == "ambiguous", str(m2[0]))

print("\n== IBAN check digits ==")
c.check("valid BG IBAN accepted", banking.valid_iban("BG80 BNBG 9661 1020 3456 78"), "")
c.check("valid DE IBAN accepted", banking.valid_iban("DE89370400440532013000"), "")
c.check("one digit wrong is rejected", not banking.valid_iban("DE89370400440532013001"), "")
c.check("nonsense rejected", not banking.valid_iban("not-an-iban"), "")
st, r = c.call("POST", "/api/banking/check-iban", {"iban": "DE89370400440532013000"})
c.check("endpoint agrees", st == 200 and r["valid"] is True, str(r))

print("\n== pain.001 is schema-shaped ==")
xml, msg = banking.build_pain001(
    "Nordic Retail Group", "DE89370400440532013000", "COBADEFFXXX",
    [{"name": "BuildPro", "iban": "BG80BNBG96611020345678", "bic": "BNBGBGSF",
      "amount": "1250.00", "currency": "EUR", "reference": "INV-2026-014"}])
root = ET.fromstring(xml)
c.check("namespace is pain.001.001.03",
        root.tag == "{urn:iso:std:iso:20022:tech:xsd:pain.001.001.03}Document", root.tag)
find = lambda n: [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == n]
c.check("one transaction", len(find("CdtTrfTxInf")) == 1, str(len(find("CdtTrfTxInf"))))
c.check("NbOfTxs correct", all(e.text == "1" for e in find("NbOfTxs")), "")
c.check("CtrlSum matches", all(e.text == "1250.00" for e in find("CtrlSum")), "")
c.check("amount carries its currency", find("InstdAmt")[0].get("Ccy") == "EUR", "")
c.check("SEPA service level", find("Cd")[0].text == "SEPA", "")
c.check("creditor IBAN present", any(e.text == "BG80BNBG96611020345678" for e in find("IBAN")), "")
c.check("remittance carried", find("Ustrd")[0].text == "INV-2026-014", "")
c.check("end-to-end id present", find("EndToEndId")[0].text == "INV-2026-014", "")

print("\n== a bad IBAN stops the file being built ==")
try:
    banking.build_pain001("X", "DE00000000000000000000", "",
                          [{"name": "Y", "iban": "BG80BNBG96611020345678", "amount": "1"}])
    c.check("refused", False, "no error")
except banking.StatementError as e:
    c.check("refused with a reason", "IBAN" in str(e), str(e))

print("\n== end to end through the API ==")
ws, order = c.first_order()
st, p = c.call("POST", "/api/workspaces/%d/payouts" % ws["id"],
               {"amount": "1250", "description": "Банков тест", "reference": "INV-2026-014"})
mine = [x for x in p["payouts"] if x["reference"] == "INV-2026-014"]
c.check("payout created", bool(mine), str(p)[:200])
pid = mine[0]["id"]

st, r = c.call("POST", "/api/banking/statement", {"content": CAMT})
c.check("statement parsed", st == 200 and r["format"] == "camt.053", str(r)[:200])
c.check("credits counted", r["credits"] == 2, str(r)[:200])
c.check("our payout was matched", any(m["payout_id"] == pid for m in r["matches"]),
        str(r["matches"])[:300])
c.check("nothing is paid yet", c.call("GET", "/api/workspaces/%d/payouts" % ws["id"])[1]
        ["payouts"][0]["status"] == "due", "reading a statement must not settle anything")

st, r = c.call("POST", "/api/banking/reconcile", {"payout_ids": [pid], "note": "CAMT.053"})
c.check("reconciled", st == 200 and r["reconciled"] == 1, str(r))
st, after = c.call("GET", "/api/workspaces/%d/payouts" % ws["id"])
now = [x for x in after["payouts"] if x["id"] == pid][0]
c.check("now paid", now["status"] == "paid", str(now))
c.check("the note says where it came from", now["note"] == "CAMT.053", str(now))

print("\n== SEPA export from outstanding payouts ==")
st, p2 = c.call("POST", "/api/workspaces/%d/payouts" % ws["id"],
                {"amount": "480", "description": "SEPA тест", "reference": "INV-2026-015"})
pid2 = [x for x in p2["payouts"] if x["reference"] == "INV-2026-015"][0]["id"]
st, xml2 = c.call("POST", "/api/banking/sepa", {
    "debtor_name": "Nordic Retail Group", "debtor_iban": "DE89370400440532013000",
    "payout_ids": [pid2], "accounts": {str(pid2): {"iban": "BG80BNBG96611020345678"}}}, raw=True)
c.check("file produced", st == 200 and xml2.startswith("<?xml"), str(st) + " " + str(xml2)[:120])
r2 = ET.fromstring(xml2)
c.check("valid XML with one transaction",
        len([e for e in r2.iter() if e.tag.endswith("CdtTrfTxInf")]) == 1, "")
st, r = c.call("POST", "/api/banking/sepa", {"payout_ids": [pid2],
                                             "debtor_iban": "DE00000000000000000000"})
c.check("bad debtor IBAN refused", st == 400 and r.get("code") == "sepa_invalid",
        "%s %s" % (st, r))

c.call("PATCH", "/api/payouts/%d" % pid2, {"status": "cancelled"})
c.call("DELETE", "/api/payouts/%d" % pid2)
sys.exit(c.summary())
