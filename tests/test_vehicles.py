# -*- coding: utf-8 -*-
"""Vehicles: a VIN is the constant an imported car's life hangs on, and its
passport must gather every record that touched it."""
import os, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seamclient import Client

c = Client(); c.login()
VIN = "WVWZZZ1KZAW123456"

st, v = c.call("GET", "/api/vehicles")
for x in v.get("vehicles", []):
    if x["vin"] == VIN:
        c.call("PATCH", "/api/vehicles/%d" % x["id"], {"status": "archived"})

print("== VIN validation ==")
for bad, why in (("SHORT", "too short"), ("WVWZZZ1KZAW12345I", "contains I"),
                 ("WVWZZZ1KZAW12345O", "contains O"), ("WVWZZZ1KZAW12345Q", "contains Q"),
                 ("", "empty")):
    st, r = c.call("POST", "/api/vehicles", {"vin": bad})
    c.check("rejected: %s" % why, st == 400 and r.get("code") == "bad_vin", "%s %s" % (st, r))

print("\n== creating one ==")
st, r = c.call("POST", "/api/vehicles", {
    "vin": VIN.lower(), "make": "Volkswagen", "model": "Golf VI", "year": "2010",
    "plate": "CB1234AB", "colour": "сребрист", "fuel": "дизел", "mileage": "184000"})
c.check("created", st == 200, str(r)[:200])
veh = [x for x in r["vehicles"] if x["vin"] == VIN]
c.check("VIN normalised to upper case", bool(veh), str(r["vehicles"])[:200])
v = veh[0]
c.check("year is a number", v["year"] == 2010, str(v["year"]))
c.check("mileage is a number", v["mileage"] == 184000, str(v["mileage"]))
c.check("label built from make/model/year", v["label"] == "Volkswagen Golf VI 2010", v["label"])
vid = v["id"]
base_links = v["orders"]        # a reactivated VIN keeps its history
print("     starting from %d linked records" % base_links)

print("\n== the same VIN cannot be entered twice while active ==")
st, r = c.call("POST", "/api/vehicles", {"vin": VIN})
c.check("400 vin_exists", st == 400 and r.get("code") == "vin_exists", "%s %s" % (st, r))

print("\n== an archived VIN reactivates instead of starting a second history ==")
c.call("PATCH", "/api/vehicles/%d" % vid, {"status": "archived"})
st, r = c.call("POST", "/api/vehicles", {"vin": VIN, "make": "Volkswagen", "model": "Golf VI",
                                         "year": "2010", "plate": "CB1234AB", "colour": "сребрист",
                                         "fuel": "дизел", "mileage": "184000"})
c.check("accepted", st == 200, str(r)[:200])
same = [x for x in r["vehicles"] if x["vin"] == VIN]
c.check("still one record for this VIN", len(same) == 1, str(len(same)))
c.check("it is the same record, reactivated", same[0]["id"] == vid and same[0]["status"] == "active",
        str(same[0]))

print("\n== search ==")
st, r = c.call("GET", "/api/vehicles?q=golf")
c.check("found by model", any(x["id"] == vid for x in r["vehicles"]), str(r)[:150])
st, r = c.call("GET", "/api/vehicles?q=CB1234")
c.check("found by plate", any(x["id"] == vid for x in r["vehicles"]), str(r)[:150])
st, r = c.call("GET", "/api/vehicles?q=zzzznothing")
c.check("no false positives", not any(x["id"] == vid for x in r["vehicles"]), str(r)[:150])

print("\n== a vehicle's life spans several records ==")
ws, _o = c.first_order()
st, ords = c.call("GET", "/api/workspaces/%d/orders" % ws["id"])
orders = (ords.get("orders") if isinstance(ords, dict) else ords)[:3]
c.check("at least two records to link", len(orders) >= 2, str(len(orders)))
for i, o in enumerate(orders):
    st, r = c.call("POST", "/api/vehicles/%d/link" % vid,
                   {"order_id": o["id"], "role": ["покупка", "транспорт", "продажба"][i % 3]})
    c.check("linked %s" % o["ref"], st == 200, str(r)[:150])
linked = r["orders"]                 # links are a set, not a running total
c.check("all of them are attached", linked >= len(orders), "%d vs %d" % (linked, len(orders)))
st, r = c.call("POST", "/api/vehicles/%d/link" % vid, {"order_id": orders[0]["id"]})
c.check("linking twice does not duplicate", r["orders"] == linked,
        "%s vs %s" % (r["orders"], linked))

print("\n== the passport ==")
st, p = c.call("GET", "/api/passport/vehicle/%d" % vid)
c.check("200", st == 200, str(p)[:200])
c.check("kind is vehicle", p["kind"] == "vehicle", str(p.get("kind")))
c.check("subject carries the VIN", p["subject"]["vin"] == VIN, str(p["subject"])[:150])
c.check("every linked record appears", len(p["orders"]) == linked,
        "%d vs %d" % (len(p["orders"]), linked))
c.check("each of the ones just linked is there",
        all(any(x["id"] == o["id"] for x in p["orders"]) for o in orders), str(p["orders"])[:200])
c.check("roles kept", any(o["role"] == "покупка" for o in p["orders"]), str(p["orders"])[:250])
c.check("status labels localized", all(o["status_label"] for o in p["orders"]), "")
c.check("value aggregated across records", p["stats"]["value"].startswith("EUR "),
        str(p["stats"]))
c.check("signatures gathered from all records", isinstance(p["signatures"], list), "")
c.check("audit trail merged newest first",
        len(p["events"]) < 2 or p["events"][0]["id"] > p["events"][-1]["id"], "")
c.check("stats agree with the lists",
        p["stats"]["records"] == len(p["orders"]) and
        p["stats"]["signatures"] == len(p["signatures"]), str(p["stats"]))
print("     %s · %d records · %s · %d signatures · %d events"
      % (p["subject"]["label"], p["stats"]["records"], p["stats"]["value"],
         p["stats"]["signatures"], p["stats"]["events"]))

print("\n== unlinking ==")
st, r = c.call("DELETE", "/api/vehicles/%d/link/%d" % (vid, orders[0]["id"]))
st, p2 = c.call("GET", "/api/passport/vehicle/%d" % vid)
c.check("record removed from the passport", len(p2["orders"]) == len(p["orders"]) - 1,
        "%d -> %d" % (len(p["orders"]), len(p2["orders"])))

print("\n== editing ==")
st, r = c.call("PATCH", "/api/vehicles/%d" % vid, {"mileage": "191500", "note": "нова верига"})
c.check("mileage updated", r["mileage"] == 191500, str(r))
c.check("note updated", r["note"] == "нова верига", str(r))
st, r = c.call("PATCH", "/api/vehicles/%d" % vid, {"mileage": "not a number"})
c.check("garbage mileage ignored, not stored", r["mileage"] == 191500, str(r))

print("\n== another company cannot see it ==")
other = Client()
if other.login("build@seam.demo", "demo1234")[0] == 200:
    st, r = other.call("GET", "/api/passport/vehicle/%d" % vid)
    c.check("passport refused", st in (400, 403, 404), "%s %s" % (st, str(r)[:120]))
    st, r = other.call("GET", "/api/vehicles")
    c.check("not in their list",
            not any(x.get("vin") == VIN for x in (r.get("vehicles") or [])), str(r)[:150])
anon = Client()
st, r = anon.call("GET", "/api/passport/vehicle/%d" % vid)
c.check("anonymous refused", st == 401, str(st))

st, r = c.call("GET", "/api/passport/vehicle/999999")
c.check("unknown vehicle 404", st == 404, str(st))

c.call("PATCH", "/api/vehicles/%d" % vid, {"status": "archived"})
sys.exit(c.summary())
