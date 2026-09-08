# -*- coding: utf-8 -*-
"""What one company must not be able to see about another.

Every id below is a real one, harvested from the company that owns it: a
refusal for an id that does not exist proves nothing at all. The outsider here
is BuildPro, which does share a workspace with Nordic - so this is not the easy
case of a total stranger, it is the realistic one where two companies have a
relationship and still must not see each other's other relationships.

A second rule is checked throughout: "not found" and "not yours" must read the
same from outside. Telling them apart lets anyone walk the ids and count how
many companies and relationships exist here, which is a fact about other
people's businesses.
"""
import os, sys, json
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from seamclient import Client

nordic = Client(); nordic.login()                        # buyer, works with all three
fulfil = Client(); fulfil.login("fulfil@seam.demo", "demo1234")
build = Client(); build.login("build@seam.demo", "demo1234")
c = nordic

REFUSED = (401, 403, 404)


def denied(label, client, method, path, data=None):
    st, r = client.call(method, path, data)
    body = json.dumps(r, ensure_ascii=False) if isinstance(r, (dict, list)) else str(r)
    return c.check(label, st in REFUSED, "%s %s" % (st, body[:140]))


print("== find the workspace BuildPro has nothing to do with ==")
st, wss = nordic.call("GET", "/api/workspaces")
all_ws = (wss.get("workspaces") if isinstance(wss, dict) else wss) or []
st, bws = build.call("GET", "/api/workspaces")
mine = {w["id"] for w in ((bws.get("workspaces") if isinstance(bws, dict) else bws) or [])}
foreign = [w for w in all_ws if w["id"] not in mine and w.get("partner")]
c.check("there is a relationship BuildPro is not part of", bool(foreign),
        "ws seen by nordic=%d, by build=%d" % (len(all_ws), len(mine)))
ws = foreign[0]
wsid = ws["id"]

st, ords = nordic.call("GET", "/api/workspaces/%d/orders" % wsid)
orders = (ords.get("orders") if isinstance(ords, dict) else ords) or []
c.check("and it has records in it", bool(orders), str(ords)[:120])
oid = orders[0]["id"]

print("\n== the relationship itself ==")
denied("workspace", build, "GET", "/api/workspaces/%d" % wsid)
denied("records in it", build, "GET", "/api/workspaces/%d/orders" % wsid)
denied("its activity", build, "GET", "/api/workspaces/%d/activity" % wsid)
denied("its contacts", build, "GET", "/api/workspaces/%d/contacts" % wsid)
denied("its messages", build, "GET", "/api/workspaces/%d/messages" % wsid)
denied("its payouts", build, "GET", "/api/workspaces/%d/payouts" % wsid)
denied("its archive", build, "GET", "/api/workspaces/%d/archive" % wsid)

print("\n== a single record in it ==")
denied("the record", build, "GET", "/api/orders/%d" % oid)
denied("its live feed", build, "GET", "/api/orders/%d/feed" % oid)
denied("its approvals", build, "GET", "/api/orders/%d/approvals" % oid)
denied("its passport", build, "GET", "/api/passport/order/%d" % oid)
denied("its protocol document", build, "GET", "/protocol/%d" % oid)

print("\n== writing into it is refused too ==")
denied("posting a comment", build, "POST", "/api/orders/%d/comments" % oid, {"body": "x"})
denied("moving its status", build, "POST", "/api/orders/%d/status" % oid, {"status": "delivered"})
denied("proposing a term", build, "POST", "/api/orders/%d/terms" % oid,
       {"key": "price", "value": "EUR 1"})
denied("recording a payout", build, "POST", "/api/workspaces/%d/payouts" % wsid,
       {"amount": "10", "method": "bank"})
denied("inviting itself in", build, "POST", "/api/workspaces/%d/invite" % wsid,
       {"email": "x@example.com"})

print("\n== the counterparty's own company record ==")
st, me = fulfil.call("GET", "/api/me")
fulfil_org = me["org"]["id"]
st, r = nordic.call("GET", "/api/passport/%d" % fulfil_org)
c.check("Nordic may see the partner it works with", st == 200, "%s %s" % (st, str(r)[:120]))
denied("BuildPro may not", build, "GET", "/api/passport/%d" % fulfil_org)

print("\n== a company that does not exist answers the same way ==")
st_missing, _r = build.call("GET", "/api/workspaces/999999")
st_foreign, _r = build.call("GET", "/api/workspaces/%d" % wsid)
c.check("workspace: unknown and forbidden are indistinguishable",
        st_missing == st_foreign == 404, "%s vs %s" % (st_missing, st_foreign))
st_missing, _r = build.call("GET", "/api/passport/999999")
st_foreign, _r = build.call("GET", "/api/passport/%d" % fulfil_org)
c.check("passport: same", st_missing == st_foreign == 404, "%s vs %s" % (st_missing, st_foreign))
st_missing, _r = build.call("GET", "/api/orders/999999")
st_foreign, _r = build.call("GET", "/api/orders/%d" % oid)
c.check("record: same", st_missing == st_foreign == 404, "%s vs %s" % (st_missing, st_foreign))

print("\n== the store, vehicles and templates belong to one company ==")
st, prods = nordic.call("GET", "/api/store/products")
plist = (prods.get("products") if isinstance(prods, dict) else prods) or []
if plist:
    pid = plist[0]["id"]
    denied("product passport", build, "GET", "/api/passport/product/%d" % pid)
    denied("deleting a product", build, "DELETE", "/api/store/products/%d" % pid)
    denied("adding keys to a product", build, "POST", "/api/store/products/%d/keys" % pid,
           {"codes": "AAA"})
st, veh = nordic.call("GET", "/api/vehicles")
vlist = (veh.get("vehicles") if isinstance(veh, dict) else veh) or []
if vlist:
    vid = vlist[0]["id"]
    denied("vehicle passport", build, "GET", "/api/passport/vehicle/%d" % vid)
    denied("changing a vehicle", build, "PATCH", "/api/vehicles/%d" % vid, {"note": "x"})

print("\n== administration belongs to the company that runs the instance ==")
# Every partner is the owner of its own company, so "is an admin" lets everyone
# through. What these need is being an admin of the host company.
denied("the error log", build, "GET", "/api/admin/errors")
denied("the health screen", build, "GET", "/api/admin/health")
denied("the go-live checks", build, "GET", "/api/admin/preflight")
denied("marking errors seen", build, "POST", "/api/admin/errors/seen", {})
denied("taking a backup", build, "POST", "/api/admin/backup", {})
denied("downloading a backup", build, "GET", "/api/admin/backup/seam-backup-x.zip")
st, r = nordic.call("GET", "/api/admin/errors")
c.check("the host company still can", st == 200, "%s %s" % (st, str(r)[:120]))

print("\n== the network names who posted, never through whom ==")
st, net = build.call("GET", "/api/network")
c.check("the partner list is only its own",
        all("Nordic" in p["name"] for p in net["partners"]), str(net["partners"])[:200])
mine_names = [p["name"] for p in net["partners"]]
far = [n for n in net["needs"]["feed"] if n.get("distance", 0) > 1]
for n in far:
    blob = json.dumps(n, ensure_ascii=False)
    c.check("a two-hop post does not name the company in between",
            not any(name in blob for name in mine_names), blob[:200])
c.check("a two-hop post still names its author",
        all((x.get("org") or {}).get("name") for x in far), str(far)[:160])
intros = net["introductions"]["sent"] + net["introductions"]["inbox"]
c.check("only introductions BuildPro is part of are listed",
        all(x["role"] in ("asker", "via", "target") for x in intros), str(intros)[:160])

print("\n== the data statement reports this instance, not a policy ==")
st, p = nordic.call("GET", "/api/privacy")
c.check("served", st == 200 and "leaves" in p, str(p)[:140])
keys = {x["key"] for x in p["leaves"]}
c.check("every outbound path is listed",
        {"email", "sms", "push", "cloud", "qtsp", "bank", "ai", "invbg", "stripe"} <= keys,
        str(sorted(keys)))
c.check("a path that is not configured says so",
        any(not x["on"] for x in p["leaves"]), str(p["leaves"])[:160])
c.check("the bank never reaches out",
        [x for x in p["leaves"] if x["key"] == "bank"][0]["on"] is False, "")
c.check("what is never shared is stated",
        {"prices", "partner_lists", "cross_tenant", "selling"} == set(p["never"]),
        str(p["never"]))
c.check("retention is a number, not a promise",
        p["retention"]["session_idle_hours"] > 0 and p["retention"]["error_log"] > 0,
        str(p["retention"]))
st, p2 = build.call("GET", "/api/privacy")
c.check("every company can read it for itself", st == 200, str(p2)[:120])

print("\n== a signed-out client sees nothing at all ==")
out = Client()
for path in ("/api/me", "/api/workspaces", "/api/network", "/api/reference",
             "/api/auth/sessions", "/api/admin/errors", "/api/privacy",
             "/api/workspaces/%d" % wsid, "/api/orders/%d" % oid,
             "/api/passport/%d" % fulfil_org):
    denied("signed out: %s" % path, out, "GET", path)

sys.exit(c.summary())
