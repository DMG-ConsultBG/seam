# -*- coding: utf-8 -*-
"""The network: a published reference, introductions through a mutual partner,
and needs that travel two hops.

The checks that matter here are the ones about what is *not* visible. A
reference must not name a counterparty; an asker must not be able to tell
"my partner forwarded it" from "my partner refused"; a need must not reveal
which company sits between the poster and the reader.

Seeded topology: Nordic Retail Group works with all three partners, and the
three do not work with each other. So from Global Supply, Nordic is one hop
away and BuildPro is two.
"""
import os, sys, json, hashlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from seamclient import Client
import webpush

nordic = Client(); nordic.login()                       # ops@seam.demo
supply = Client(); supply.login("supply@seam.demo", "demo1234")
build = Client(); build.login("build@seam.demo", "demo1234")
fulfil = Client(); fulfil.login("fulfil@seam.demo", "demo1234")
c = nordic                                              # counters live on one client

PARTNER_NAMES = ("Global Supply", "SpeedLogic", "BuildPro")

print("== reference: what it computes ==")
st, r = nordic.call("GET", "/api/reference")
c.check("200", st == 200, str(r)[:200])
c.check("starts unpublished", r.get("published") is False, str(r)[:120])
p = r.get("preview") or {}
c.check("counts records", p.get("records", 0) > 0, str(p))
c.check("counts counterparties", p.get("counterparties", 0) >= 3, str(p))
c.check("no counterparty names in preview",
        not any(n in json.dumps(p, ensure_ascii=False) for n in PARTNER_NAMES), str(p))

print("\n== publishing ==")
st, r = nordic.call("POST", "/api/reference", {"fields": {"value_band": True, "documents": True}})
c.check("published", st == 200 and r.get("url"), "%s %s" % (st, r))
url = r.get("url", "")
token = url.rsplit("/", 1)[-1]
st, r = build.call("POST", "/api/reference", {"fields": {}})
c.check("second company can publish too", st == 200 and r.get("url"), "%s %s" % (st, r))
build_ref = r.get("url", "")

print("\n== the public page ==")
st, html = nordic.call("GET", "/ref/" + token, raw=True)
c.check("page renders", st == 200 and "Nordic Retail Group" in html, "%s %s" % (st, html[:160]))
leaked = [n for n in PARTNER_NAMES if n in html]
c.check("names no counterparty", not leaked, "leaked: %s" % leaked)
c.check("carries a date", "as-of" in html.lower() or "20" in html, html[:120])
st, html_bg = nordic.call("GET", "/ref/%s?lang=bg" % token, raw=True)
c.check("reader picks the language", "Търговска референция" in html_bg, html_bg[:200])
st, html_en = nordic.call("GET", "/ref/%s?lang=en" % token, raw=True)
c.check("and English is whole", "Trade reference" in html_en and "Търговска" not in html_en,
        html_en[:200])

print("\n== the signature a third party can check ==")
st, doc = nordic.call("GET", "/ref/%s.json" % token)
c.check("json served", st == 200 and "signature" in doc, "%s %s" % (st, str(doc)[:160]))
canonical = json.dumps(doc["reference"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
digest = hashlib.sha256(canonical.encode("utf-8")).digest()
sig = webpush._b64d(doc["signature"])
c.check("signature verifies against the payload", webpush.verify(sig, digest, doc["key"]),
        doc["signature"][:40])
bad = hashlib.sha256((canonical + " ").encode("utf-8")).digest()
c.check("and fails on a tampered payload", not webpush.verify(sig, bad, doc["key"]))
c.check("key is not the push key", doc["key"] != os.environ.get("SEAM_VAPID_PUBLIC", ""), doc["key"][:20])
facts = doc["reference"]["facts"]
c.check("hidden field stays out", "on_time" in facts or "records" in facts, str(facts))

print("\n== finding a company ==")
st, s = supply.call("GET", "/api/network/search?q=nordic")
hits = [x["name"] for x in s.get("results", [])]
c.check("lowercase query finds it", "Nordic Retail Group" in hits, str(hits))
st, s = supply.call("GET", "/api/network/search?q=" + "SpeedLogic")
c.check("a company without a reference is not findable",
        "SpeedLogic 3PL" not in [x["name"] for x in s.get("results", [])], str(s))
st, s = supply.call("GET", "/api/network/search?q=n")
c.check("one letter is not a search", s.get("results") == [], str(s))

st, net = supply.call("GET", "/api/network")
c.check("network payload", st == 200 and "introductions" in net, str(net)[:160])
c.check("own partners listed", any("Nordic" in x["name"] for x in net["partners"]), str(net["partners"]))
c.check("partners of partners are counted, not listed", net["reach"] >= 3
        and not any("BuildPro" in x["name"] for x in net["partners"]), str(net["partners"]))

st, s = supply.call("GET", "/api/network/search?q=buildpro")
build_id = (s.get("results") or [{}])[0].get("id")
nordic_id = [x["id"] for x in net["partners"] if "Nordic" in x["name"]][0]
c.check("target resolved", bool(build_id), str(s))

print("\n== asking for an introduction ==")
st, r = supply.call("POST", "/api/network/introductions",
                    {"via_org_id": build_id, "target_org_id": nordic_id, "message": "x"})
c.check("cannot ask through a stranger", st == 400 and r.get("code") == "not_partner",
        "%s %s" % (st, r))
st, r = supply.call("POST", "/api/network/introductions",
                    {"via_org_id": nordic_id, "target_org_id": nordic_id})
c.check("cannot be introduced to an existing partner", st == 400
        and r.get("code") in ("bad_target", "already_partner"), "%s %s" % (st, r))
st, r = supply.call("POST", "/api/network/introductions",
                    {"via_org_id": nordic_id, "target_org_id": build_id,
                     "message": "Търсим изпълнител за обект в София."})
c.check("request created", st == 201 and r.get("status") == "requested", "%s %s" % (st, r))
intro_id = r.get("id")
st, r = supply.call("POST", "/api/network/introductions",
                    {"via_org_id": nordic_id, "target_org_id": build_id})
c.check("no duplicate", st == 400 and r.get("code") == "intro_duplicate", "%s %s" % (st, r))

print("\n== the target learns nothing yet ==")
st, bn = build.call("GET", "/api/network")
c.check("target sees no pending request", not any(x["id"] == intro_id for x in bn["introductions"]["inbox"]),
        str(bn["introductions"]["inbox"])[:200])
st, fn = fulfil.call("GET", "/api/network")
c.check("an unrelated company sees nothing",
        not any(x["id"] == intro_id for x in fn["introductions"]["inbox"] + fn["introductions"]["sent"]),
        str(fn["introductions"])[:200])

print("\n== the partner in the middle decides ==")
st, nn = nordic.call("GET", "/api/network")
mine = [x for x in nn["introductions"]["inbox"] if x["id"] == intro_id]
c.check("the partner sees it", len(mine) == 1 and mine[0]["role"] == "via", str(nn["introductions"])[:200])
c.check("and sees both sides", mine and mine[0].get("asker") and mine[0].get("target"), str(mine)[:200])
st, r = build.call("POST", "/api/network/introductions/%d/decide" % intro_id, {"decision": "approve"})
c.check("the target cannot decide before the partner does", st == 403, "%s %s" % (st, r))
st, r = nordic.call("POST", "/api/network/introductions/%d/decide" % intro_id, {"decision": "approve"})
c.check("partner passes it on", st == 200 and r.get("status") == "forwarded", "%s %s" % (st, r))

print("\n== the asker cannot tell forwarded from refused ==")
st, sn = supply.call("GET", "/api/network")
sent = [x for x in sn["introductions"]["sent"] if x["id"] == intro_id]
c.check("still reads as pending", sent and sent[0]["status"] == "requested", str(sent)[:200])
c.check("no contact details yet", sent and not (sent[0].get("target") or {}).get("emails"),
        str(sent)[:200])

print("\n== the target decides for itself ==")
st, bn = build.call("GET", "/api/network")
mine = [x for x in bn["introductions"]["inbox"] if x["id"] == intro_id]
c.check("now it appears", len(mine) == 1 and mine[0]["role"] == "target", str(bn["introductions"])[:200])
c.check("with the message it was sent", mine and "София" in mine[0]["message"], str(mine)[:200])
st, r = build.call("POST", "/api/network/introductions/%d/decide" % intro_id, {"decision": "approve"})
c.check("accepted", st == 200 and r.get("status") == "accepted", "%s %s" % (st, r))

st, sn = supply.call("GET", "/api/network")
sent = [x for x in sn["introductions"]["sent"] if x["id"] == intro_id][0]
c.check("asker sees acceptance", sent["status"] == "accepted", str(sent)[:160])
c.check("and gets contact details", (sent["target"] or {}).get("emails"), str(sent)[:250])
st, bn = build.call("GET", "/api/network")
got = [x for x in bn["introductions"]["inbox"] if x["id"] == intro_id][0]
c.check("both sides get them", (got["asker"] or {}).get("emails"), str(got)[:250])

st, n = supply.call("GET", "/api/notifications")
kinds = [x["kind"] for x in (n.get("items") or n.get("notifications") or [])]
c.check("asker was told", "intro_decided" in kinds, str(kinds[:8]))

print("\n== needs travel two hops and stop ==")
st, r = supply.call("POST", "/api/network/needs",
                    {"title": "Търсим склад в Пловдив", "detail": "400 кв.м, за 6 месеца.",
                     "country": "BG"})
c.check("posted", st == 201 and r.get("id"), "%s %s" % (st, r))
need_id = r.get("id")
st, r = supply.call("POST", "/api/network/needs", {"title": ""})
c.check("a need needs a subject", st == 400 and r.get("code") == "no_title", "%s %s" % (st, r))

st, nn = nordic.call("GET", "/api/network")
seen = [x for x in nn["needs"]["feed"] if x["id"] == need_id]
c.check("a partner sees it at one hop", seen and seen[0]["distance"] == 1, str(seen)[:200])
st, bn = build.call("GET", "/api/network")
seen = [x for x in bn["needs"]["feed"] if x["id"] == need_id]
c.check("a partner of a partner sees it at two", seen and seen[0]["distance"] == 2, str(seen)[:200])
c.check("but not through whom", seen and "Nordic" not in json.dumps(seen[0], ensure_ascii=False),
        str(seen)[:250])

print("\n== replies belong to the poster ==")
st, r = build.call("POST", "/api/network/needs/%d/reply" % need_id, {"message": "Имаме свободен склад."})
c.check("reply accepted", st == 201, "%s %s" % (st, r))
st, r = build.call("POST", "/api/network/needs/%d/reply" % need_id, {"message": "пак"})
c.check("only once", st == 400 and r.get("code") == "need_duplicate", "%s %s" % (st, r))
st, r = supply.call("POST", "/api/network/needs/%d/reply" % need_id, {"message": "x"})
c.check("not to your own", st == 400 and r.get("code") == "own_need", "%s %s" % (st, r))

st, sn = supply.call("GET", "/api/network")
own = [x for x in sn["needs"]["mine"] if x["id"] == need_id][0]
c.check("poster sees who replied", own["replies"] and "BuildPro" in own["replies"][0]["org"]["name"],
        str(own)[:250])
c.check("and how to reach them", own["replies"][0]["org"].get("emails"), str(own["replies"][0])[:250])
st, nn = nordic.call("GET", "/api/network")
other = [x for x in nn["needs"]["feed"] if x["id"] == need_id][0]
c.check("a bystander sees the count only", other.get("replies") is None and other["reply_count"] == 1,
        str(other)[:200])

st, n = supply.call("GET", "/api/notifications")
kinds = [x["kind"] for x in (n.get("items") or n.get("notifications") or [])]
c.check("poster was told", "need_reply" in kinds, str(kinds[:8]))

print("\n== closing ==")
st, r = supply.call("POST", "/api/network/needs/%d/close" % need_id)
c.check("closed", st == 200 and r.get("status") == "closed", "%s %s" % (st, r))
st, bn = build.call("GET", "/api/network")
c.check("gone from the feed", not any(x["id"] == need_id for x in bn["needs"]["feed"]),
        str(bn["needs"]["feed"])[:160])
st, r = build.call("POST", "/api/network/needs/%d/reply" % need_id, {"message": "късно"})
c.check("and cannot be answered", st == 404, "%s %s" % (st, r))

print("\n== revoking the reference ==")
st, r = nordic.call("POST", "/api/reference/revoke")
c.check("revoked", st == 200 and r.get("published") is False, "%s %s" % (st, r))
st, html = nordic.call("GET", "/ref/" + token, raw=True)
c.check("the old link is dead", st == 404, str(st))
st, r = nordic.call("POST", "/api/reference", {"fields": {}})
c.check("republishing issues a new link", st == 200 and r["url"].rsplit("/", 1)[-1] != token,
        str(r)[:160])

sys.exit(c.summary())
