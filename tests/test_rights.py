# -*- coding: utf-8 -*-
"""Taking your data out, and being erased from it.

Two rights that exist in most of the countries Seam serves, and one real
conflict between the second and the audit trail. The trail is append-only
because that is what makes an agreement provable; deleting a person's rows
would tear holes in the evidence a dispute turns on, and deleting nothing
would make the right a pretence.

So the checks below are about that middle ground: after erasure the human
being is gone from the database, and what two companies agreed is still there
and still provable.
"""
import os, sys, io, json, zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from seamclient import Client

c = Client(); c.login()

print("== the export opens without Seam ==")
st, blob = c.call("GET", "/api/account/export", binary=True)
c.check("served", st == 200 and blob[:2] == b"PK", "%s %s" % (st, blob[:8]))
z = zipfile.ZipFile(io.BytesIO(blob))
names = z.namelist()
c.check("valid archive", z.testzip() is None, "")
c.check("the company is in it", "company.json" in names, str(names[:6]))
c.check("so is the network", "network.json" in names, str(names[:6]))
c.check("and it explains itself", "README.txt" in names, str(names[:6]))
c.check("each relationship is a whole archive",
        any(n.startswith("relationships/") and n.endswith(".zip") for n in names),
        str([n for n in names if n.startswith("relationships/")][:3]))

company = json.loads(z.read("company.json").decode("utf-8"))
c.check("names the company", (company["company"] or {}).get("name"), str(company)[:120])
c.check("lists the people", len(company["people"]) >= 1, str(len(company["people"])))
c.check("lists the relationships", len(company["relationships"]) >= 1,
        str(len(company["relationships"])))

person = company["people"][0]
c.check("a person carries what is held about them",
        {"name", "email", "role", "sessions", "notifications"} <= set(person), str(sorted(person)))

print("\n== an export is not a way to lift credentials ==")
blob_text = json.dumps(company, ensure_ascii=False)
for bad, label in [("password_hash", "no password hashes"),
                   ("scrypt:", "no hashes by any other name"),
                   ("secret", "no second-factor secret"),
                   ("sid", "no session identifiers"),
                   ("recovery", "no recovery codes")]:
    c.check(label, bad not in blob_text, blob_text[:0] or bad)
readme = z.read("README.txt").decode("utf-8")
c.check("and the archive says so", "not a way to lift credentials" in readme, readme[:120])

print("\n== one relationship archive really is whole ==")
inner_name = [n for n in names if n.startswith("relationships/")][0]
inner = zipfile.ZipFile(io.BytesIO(z.read(inner_name)))
c.check("records and trail", "workspace.json" in inner.namelist(), str(inner.namelist()[:5]))
ws = json.loads(inner.read("workspace.json").decode("utf-8"))
c.check("with the audit trail in it",
        any(o.get("events") for o in ws["orders"]) or not ws["orders"], str(ws)[:140])

print("\n== closing says what it will do before it does it ==")
st, e = c.call("GET", "/api/account/close-effect")
c.check("served", st == 200 and "relationships" in e, str(e)[:140])
c.check("knows the role", e["role"] in ("owner", "admin", "member", "viewer"), str(e))
c.check("counts the relationships", e["relationships"] >= 1, str(e))

print("\n== the password is asked for again ==")
part = Client(); part.login("build@seam.demo", "demo1234")
st, r = part.call("POST", "/api/account/close", {"password": "not-the-password"})
c.check("a wrong password will not do it", st == 403 and r.get("code") == "bad_password",
        "%s %s" % (st, r))

print("\n== erasure removes the person and keeps the agreement ==")
# A fresh person inside an existing company, so the erasure is observable
# without destroying the demo data every other suite relies on. Inviting them
# also gives the owner a colleague, which is what the next check needs.
st, team = c.call("POST", "/api/team", {"email": "leaver@seam.demo", "name": "Ще напусне",
                                        "role": "member"})
c.check("colleague invited", st in (200, 201), str(team)[:160])
link = team.get("invite_link") or ""
token = link.split("reset=")[-1] if "reset=" in link else ""
c.check("with a way in", bool(token), str(team)[:160])

leaver = Client()
st, r = leaver.call("POST", "/api/auth/reset", {"token": token, "password": "leaving123"})
c.check("password set", st == 200, str(r)[:120])
st, r = leaver.call("POST", "/api/auth/login", {"email": "leaver@seam.demo",
                                                "password": "leaving123"})
leaver.csrf = r.get("csrf", "")
c.check("and they can sign in", st == 200, str(r)[:120])

print("\n== an owner with colleagues must hand over first ==")
# Now that the company has a second person, the owner is refused - which is
# also why this account is never actually closed by the suite.
st, r = c.call("POST", "/api/account/close", {"password": "demo1234"})
c.check("refused", st == 400 and r.get("code") == "owner_must_hand_over", "%s %s" % (st, r))
st, me = c.call("GET", "/api/me")
c.check("and the owner is still signed in", st == 200, str(st))

st, before = c.call("GET", "/api/team")
n_before = len(before.get("members") or before or [])

st, r = leaver.call("POST", "/api/account/close", {"password": "leaving123"})
c.check("erased", st == 200 and r.get("closed"), "%s %s" % (st, r))
st, me = leaver.call("GET", "/api/me")
c.check("the session is gone with them", st == 401, str(st))
st, r = leaver.call("POST", "/api/auth/login", {"email": "leaver@seam.demo",
                                                "password": "leaving123"})
c.check("and the old password no longer works", st == 401, "%s %s" % (st, str(r)[:100]))

st, after = c.call("GET", "/api/team")
n_after = len(after.get("members") or after or [])
c.check("they are out of the company", n_after == n_before - 1, "%s -> %s" % (n_before, n_after))
c.check("and their name is nowhere in it",
        "leaver@seam.demo" not in json.dumps(after, ensure_ascii=False), str(after)[:160])

print("\n== the audit trail still proves what was agreed ==")
ws, order = c.first_order()
st, o = c.call("GET", "/api/orders/%d" % order["id"])
c.check("records still open", st == 200, str(st))
c.check("their history is intact", isinstance(o.get("events"), list) and o["events"],
        str(len(o.get("events") or [])))
st, blob2 = c.call("GET", "/api/account/export", binary=True)
c.check("and a fresh export still works", st == 200 and blob2[:2] == b"PK", str(st))
z2 = zipfile.ZipFile(io.BytesIO(blob2))
c2 = json.loads(z2.read("company.json").decode("utf-8"))
c.check("with the erased person no longer listed",
        not any("leaver@seam.demo" == p["email"] for p in c2["people"]),
        str([p["email"] for p in c2["people"]])[:160])

sys.exit(c.summary())
