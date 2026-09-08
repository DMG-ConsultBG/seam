# -*- coding: utf-8 -*-
"""Cloud storage: the SigV4 signature must be independently verifiable, the
archive must open without Seam, and both provider shapes must work."""
import os, sys, os, io, json, zipfile, hashlib, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # the app package
from seamclient import Client
import cloudstore

GW = "http://127.0.0.1:5097"
gw = lambda p: json.loads(urllib.request.urlopen(GW + p, timeout=10).read().decode())

c = Client(); c.login()

print("== SigV4 against the AWS published test vector ==")
# AWS SigV4 test suite: the derived signing key for the documented inputs.
k = cloudstore.signing_key("wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                           "20150830", "us-east-1", "iam")
c.check("signing key matches the published vector",
        k.hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9",
        k.hex())

print("\n== configuration ==")
st, r = c.call("POST", "/api/cloud", {"provider": "nope"})
c.check("unknown provider refused", st == 400 and r.get("code") == "cloud_provider",
        "%s %s" % (st, r))
st, r = c.call("POST", "/api/cloud", {
    "provider": "s3", "endpoint": GW, "region": "eu-central-1", "bucket": "seam-backup",
    "prefix": "archive/", "access_key": "AKIATESTKEY", "secret_key": "testsecretkey"})
c.check("saved and ready", st == 200 and r["cloud"]["ready"] is True, str(r)[:250])
c.check("secret not echoed back", "secret_key" not in r["cloud"], str(r["cloud"]))

print("\n== a real upload, signature verified by the receiver ==")
gw("/__reset")
st, r = c.call("POST", "/api/cloud/test", {})
c.check("test upload accepted", st == 200 and r.get("ok") is True, str(r)[:250])
got = gw("/__received")["received"]
c.check("the target received exactly one object", len(got) == 1, str(got)[:200])
if got:
    c.check("signed as s3, signature verified independently", got[0]["mode"] == "s3", str(got[0]))
    c.check("path-style with bucket and prefix",
            got[0]["path"] == "/seam-backup/archive/seam-connection-test.txt", got[0]["path"])

print("\n== a wrong secret is caught by the signature, not waved through ==")
c.call("POST", "/api/cloud", {"provider": "s3", "endpoint": GW, "region": "eu-central-1",
                              "bucket": "seam-backup", "prefix": "archive/",
                              "access_key": "AKIATESTKEY", "secret_key": "wrong-secret"})
st, r = c.call("POST", "/api/cloud/test", {})
c.check("502 cloud_failed", st == 502 and r.get("code") == "cloud_failed",
        "%s %s" % (st, str(r)[:150]))
c.check("the target said the signature did not match",
        "SignatureDoesNotMatch" in json.dumps(r), str(r)[:200])
c.call("POST", "/api/cloud", {"provider": "s3", "endpoint": GW, "region": "eu-central-1",
                              "bucket": "seam-backup", "prefix": "archive/",
                              "access_key": "AKIATESTKEY", "secret_key": "testsecretkey"})

print("\n== the archive opens without Seam ==")
ws, order = c.first_order()
st, data = c.call("GET", "/api/workspaces/%d/archive" % ws["id"], binary=True)
c.check("200", st == 200, str(st))
z = zipfile.ZipFile(io.BytesIO(data))
names = z.namelist()
c.check("it is a valid zip", z.testzip() is None, "")
c.check("workspace.json present", "workspace.json" in names, str(names[:6]))
c.check("orders.csv present", "orders.csv" in names, str(names[:6]))
c.check("README present", "README.txt" in names, str(names[:6]))
doc = json.loads(z.read("workspace.json").decode("utf-8"))
c.check("workspace named", bool(doc["workspace"]["name"]), str(doc["workspace"]))
c.check("orders carried", len(doc["orders"]) >= 1, str(len(doc["orders"])))
o0 = doc["orders"][0]
c.check("terms carry labels, not raw keys",
        all(t["label"] and t["label"] != t["key"] for t in o0["terms"]) or not o0["terms"], "")
c.check("audit trail included with IP and device",
        any("ip" in e for e in o0["events"]), str(o0["events"][:1])[:200])
c.check("signature hashes included",
        all(len(s["sha256"]) == 64 for s in o0["signatures"]) or not o0["signatures"], "")
c.check("csv has a BOM so Excel opens it correctly",
        z.read("orders.csv").startswith("\ufeff".encode("utf-8")), "")
print("     archive: %d entries, %d bytes" % (len(names), len(data)))

print("\n== pushing the archive to storage ==")
gw("/__reset")
st, r = c.call("POST", "/api/workspaces/%d/archive/cloud" % ws["id"], {})
c.check("uploaded", st == 200 and r.get("uploaded") is True, str(r)[:200])
got = gw("/__received")["received"]
c.check("target got the zip", len(got) == 1 and got[0]["bytes"] > 200, str(got)[:200])
c.check("stored under the workspace", "/workspace-%d/" % ws["id"] in got[0]["path"],
        got[0]["path"])
c.check("content type is zip", got[0]["content_type"] == "application/zip", str(got[0]))

print("\n== WebDAV shape ==")
gw("/__reset")
st, r = c.call("POST", "/api/cloud", {"provider": "webdav", "endpoint": GW + "/dav",
                                      "prefix": "seam/", "username": "u", "password": "p"})
c.check("webdav ready", r["cloud"]["ready"] is True, str(r)[:200])
st, r = c.call("POST", "/api/cloud/test", {})
c.check("upload accepted", st == 200, str(r)[:200])
got = gw("/__received")["received"]
c.check("received over webdav with basic auth",
        got and got[0]["mode"] == "webdav" and got[0]["auth"].startswith("Basic "), str(got)[:200])
c.check("path built from endpoint and prefix",
        got[0]["path"] == "/dav/seam/seam-connection-test.txt", got[0]["path"])

print("\n== disconnect ==")
st, r = c.call("POST", "/api/cloud", {"provider": ""})
c.check("cleared", st == 200 and r.get("cloud") is None, str(r)[:150])
st, r = c.call("POST", "/api/workspaces/%d/archive/cloud" % ws["id"], {})
c.check("without storage it refuses cleanly", st == 400 and r.get("code") == "cloud_config",
        "%s %s" % (st, r))

sys.exit(c.summary())
