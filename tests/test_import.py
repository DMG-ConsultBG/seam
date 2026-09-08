# -*- coding: utf-8 -*-
"""CSV import: a real spreadsheet works unedited, the preview writes nothing,
and a row that cannot be imported says why."""
import os, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client(); c.login()
STAMP = str(int(time.time()))[-6:]

# A European export: BOM, semicolons, Bulgarian headers, Windows line endings.
PARTNERS = ("﻿Фирма;Имейл;ЕИК;Държава;Шаблон\r\n"
            "Алфа Логистика %s;alfa%s@example.com;131234567;BG;general\r\n"
            "Бета Строй %s;beta%s@example.com;204567890;BG;construction\r\n"
            "Гама Трейд %s;;;BG;general\r\n" % (STAMP, STAMP, STAMP, STAMP, STAMP))

print("== a real spreadsheet is read as it arrives ==")
st, p = c.call("POST", "/api/import/preview", {"kind": "partners", "content": PARTNERS})
c.check("200", st == 200, str(p)[:250])
c.check("semicolons and BOM handled", p["rows"] == 3, str(p["rows"]))
c.check("Bulgarian headers recognised",
        all(x["name"] for x in p["plan"]), str(p["plan"])[:250])
c.check("three rows planned as create", p["summary"]["create"] == 3, str(p["summary"]))
c.check("a partner with no e-mail is still importable",
        [x for x in p["plan"] if x["name"].startswith("Гама")][0]["action"] == "create",
        str(p["plan"])[:250])

print("\n== the preview writes nothing ==")
st, ws_before = c.call("GET", "/api/workspaces")
n_before = len(ws_before.get("workspaces") if isinstance(ws_before, dict) else ws_before)
st, p2 = c.call("POST", "/api/import/preview", {"kind": "partners", "content": PARTNERS})
st, ws_mid = c.call("GET", "/api/workspaces")
c.check("no workspace was created by previewing",
        len(ws_mid.get("workspaces") if isinstance(ws_mid, dict) else ws_mid) == n_before,
        "%d" % n_before)

print("\n== bad rows explain themselves instead of vanishing ==")
BAD = ("Фирма;Имейл;Шаблон\n"
       ";nobody@example.com;general\n"                     # no name
       "Делта;not-an-email;general\n"                      # bad e-mail
       "Епсилон;eps@example.com;no-such-template\n"        # unknown vertical
       "Зета;dup@example.com;general\n"
       "Зета Второ;dup@example.com;general\n")             # duplicate in file
st, p = c.call("POST", "/api/import/preview", {"kind": "partners", "content": BAD})
probs = {x["problem"] for x in p["plan"] if x["action"] == "skip"}
c.check("missing name reported", "no_name" in probs, str(p["plan"])[:300])
c.check("bad e-mail reported", "bad_email" in probs, str(probs))
c.check("unknown vertical reported", "unknown_template" in probs, str(probs))
c.check("duplicate inside the file reported", "duplicate_in_file" in probs, str(probs))
c.check("nothing is silently dropped", len(p["plan"]) == 5, str(len(p["plan"])))

print("\n== committing creates the relationships ==")
st, r = c.call("POST", "/api/import/commit", {"kind": "partners", "content": PARTNERS})
c.check("three created", st == 200 and r["created"] == 3, str(r)[:200])
c.check("two invited (the third had no e-mail)", r["invited"] == 2, str(r))
c.check("invite links handed back when e-mail is not configured",
        len(r.get("invite_links") or []) == 2, str(r.get("invite_links"))[:200])
st, ws_after = c.call("GET", "/api/workspaces")
after = ws_after.get("workspaces") if isinstance(ws_after, dict) else ws_after
c.check("workspaces really exist now", len(after) == n_before + 3,
        "%d -> %d" % (n_before, len(after)))
made = [w for w in after if w["name"].endswith(STAMP)]
c.check("named from the file", len(made) == 3, str([w["name"] for w in made]))
c.check("the vertical from the file was used",
        any(w["template"] == "construction" for w in made), str([w["template"] for w in made]))

print("\n== importing the same file twice does not duplicate ==")
st, p = c.call("POST", "/api/import/preview", {"kind": "partners", "content": PARTNERS})
c.check("all three now report as existing", p["summary"]["exists"] == 3, str(p["summary"]))
st, r = c.call("POST", "/api/import/commit", {"kind": "partners", "content": PARTNERS})
c.check("nothing created the second time", r["created"] == 0 and r["skipped"] == 3, str(r))

print("\n== orders, with the spreadsheet's own columns kept ==")
target = [w for w in made if w["template"] == "general"][0]["name"]
ORDERS = ("Пространство,Описание,Статус,price,delivery_date\n"
          "%s,Доставка на рафтове,requested,EUR 4500,2026-09-30\n"
          "%s,Ремонт на рампа,,EUR 1200,\n"
          "Несъществуващо,Няма как,,,\n"
          "%s,,,,\n" % (target, target, target))          # a row with no title
st, p = c.call("POST", "/api/import/preview", {"kind": "orders", "content": ORDERS})
c.check("two importable", p["summary"]["create"] == 2, str(p["summary"]))
probs = {x["problem"] for x in p["plan"] if x["action"] == "skip"}
c.check("unknown workspace reported", "unknown_workspace" in probs, str(probs))
c.check("missing title reported", "no_title" in probs, str(probs))
first = [x for x in p["plan"] if x["action"] == "create"][0]
c.check("matching columns become terms", first["terms"].get("price") == "EUR 4500",
        str(first["terms"]))
c.check("a blank status falls back to the first stage",
        [x for x in p["plan"] if x["title"].startswith("Ремонт")][0]["status"], str(p["plan"])[:300])

st, r = c.call("POST", "/api/import/commit", {"kind": "orders", "content": ORDERS})
c.check("two orders created", st == 200 and r.get("created") == 2, "%s %s" % (st, str(r)[:250]))
ws_id = [w for w in after if w["name"] == target][0]["id"]
st, ords = c.call("GET", "/api/workspaces/%d/orders" % ws_id)
lst = ords.get("orders") if isinstance(ords, dict) else ords
c.check("they are really there", len(lst) == 2, str(len(lst)))
c.check("references generated in the vertical's format",
        all(o["ref"].startswith(("REQ", "ORD")) for o in lst), str([o["ref"] for o in lst]))
details = [c.call("GET", "/api/orders/%d" % o["id"])[1] for o in lst]
c.check("the term from the spreadsheet is on the right order",
        any(any(t.get("value") == "EUR 4500" for t in (d.get("terms") or [])) for d in details),
        str([[t.get("value") for t in (d.get("terms") or [])] for d in details])[:250])
c.check("imported terms start as proposed, not agreed",
        all(t["state"] == "proposed" for d in details for t in (d.get("terms") or [])
            if t.get("value")),
        str(details[0].get("terms"))[:200])
od = details[0]
kinds = [e.get("kind") for e in (od.get("events") or [])]
c.check("the import is in the audit trail", "order_created" in kinds, str(kinds[:5]))

print("\n== guards ==")
st, r = c.call("POST", "/api/import/preview", {"kind": "nonsense", "content": "a,b\n1,2"})
c.check("unknown kind refused", st == 400 and r.get("code") == "import_kind", "%s %s" % (st, r))
st, r = c.call("POST", "/api/import/preview", {"kind": "partners", "content": ""})
c.check("empty file refused", st == 400 and r.get("code") == "field_required", "%s %s" % (st, r))
st, r = c.call("POST", "/api/import/preview", {"kind": "partners", "content": "Фирма;Имейл"})
c.check("header without rows refused", st == 400 and r.get("code") == "import_empty",
        "%s %s" % (st, r))

st, team = c.call("GET", "/api/team")
for m in team["members"]:
    if not m["is_me"]:
        c.call("DELETE", "/api/team/%d" % m["id"])
st, inv = c.call("POST", "/api/team", {"email": "imp%s@nordic-retail.example" % STAMP,
                                       "name": "Вносител", "role": "member"})
import re as _re
tok = _re.search(r"\?reset=(\S+)", inv["invite_link"]).group(1)
mem = Client()
mem.call("POST", "/api/auth/reset", {"token": tok, "password": "Vnositel2026"})
mem.login("imp%s@nordic-retail.example" % STAMP, "Vnositel2026")
st, r = mem.call("POST", "/api/import/commit", {"kind": "partners", "content": PARTNERS})
c.check("a member cannot import", st == 403, "%s %s" % (st, str(r)[:120]))
c.call("DELETE", "/api/team/%d" % [m for m in inv["members"]
                                   if m["email"].startswith("imp")][0]["id"])

sys.exit(c.summary())
