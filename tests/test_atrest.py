# -*- coding: utf-8 -*-
"""What a stolen copy of the database is worth.

Everything else in this suite asks whether the application behaves. This asks
the opposite question: if someone walks off with `seam.db`, or with a backup of
it, what can they actually do? The answer should be "read the business data" -
which is bad enough and is what disk encryption and file permissions are for -
and not "sign in as anyone, open every signing link, and generate second-factor
codes", which is what storing bearer tokens in the clear amounts to.

So these checks read the database file directly and assert the *absence* of
usable secrets, then confirm the application still works with them stored that
way.
"""
import os, re, sys, io, json, time, sqlite3, zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from seamclient import Client
import cryptobox
import totp
import db

c = Client(); c.login()
DB = os.environ.get("SEAM_DB") or os.path.join(ROOT, "seam.db")

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def raw(query, args=()):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


print("== the sealed box, before anything relies on it ==")
r = cryptobox.self_test()
for name in ("chacha20_block", "poly1305", "aead_ciphertext", "aead_tag"):
    c.check("RFC 8439 vector: %s" % name, r[name], str(r))
c.check("round trip", r["round_trip"], str(r))
c.check("refuses the wrong key", r["rejects_wrong_key"], str(r))
c.check("refuses a tampered box", r["rejects_tampered"], str(r))
c.check("refuses the wrong context", r["rejects_wrong_aad"], str(r))

print("\n== a session cookie cannot be recovered from the database ==")
st, me = c.call("GET", "/api/me")
c.check("this client is signed in", st == 200, str(st))
rows = raw("SELECT sid FROM user_sessions WHERE revoked_at IS NULL")
c.check("sessions exist", len(rows) > 0, str(len(rows)))
c.check("every stored session id is a hash",
        all(HEX64.match(x["sid"] or "") for x in rows),
        str([x["sid"][:20] for x in rows[:3]]))
# The decisive check: take what is in the database and try to use it.
stolen = rows[0]["sid"]
thief = Client()
thief.cj.set_cookie(__import__("http.cookiejar", fromlist=["Cookie"]).Cookie(
    0, "session", stolen, None, False, "127.0.0.1", False, False, "/", True,
    False, None, True, None, None, {}))
st, out = thief.call("GET", "/api/me")
c.check("the stored value does not sign anyone in", st == 401, "%s %s" % (st, str(out)[:100]))

print("\n== password reset and verification links ==")
c.call("POST", "/api/auth/forgot", {"email": "ops@seam.demo"})
rows = raw("SELECT reset_token FROM users WHERE reset_token IS NOT NULL")
c.check("a reset token was issued", len(rows) > 0, str(len(rows)))
c.check("and stored only as a hash",
        all(HEX64.match(x["reset_token"]) for x in rows), str(rows[0]["reset_token"])[:20])
rows = raw("SELECT verify_token FROM users WHERE verify_token IS NOT NULL")
c.check("verification tokens too",
        all(HEX64.match(x["verify_token"]) for x in rows) if rows else True,
        str([x["verify_token"][:16] for x in rows[:2]]))

print("\n== links that must keep working are encrypted, not readable ==")
st, cal = c.call("GET", "/api/calendar")
tok = cal["url"].rsplit("/", 1)[-1].replace(".ics", "")
row = raw("SELECT cal_token, cal_token_enc FROM users WHERE cal_token IS NOT NULL")[0]
c.check("the calendar token is stored as a hash", HEX64.match(row["cal_token"]), row["cal_token"][:20])
c.check("with an encrypted copy beside it", str(row["cal_token_enc"]).startswith("enc:"),
        str(row["cal_token_enc"])[:24])
c.check("the plaintext is nowhere in the row", tok not in json.dumps(dict(row)), tok[:12])
st, ics = c.call("GET", "/calendar/%s.ics" % tok, raw=True)
c.check("and the feed still works", st == 200 and "BEGIN:VCALENDAR" in ics, str(st))
st, ics = c.call("GET", "/calendar/%s.ics" % row["cal_token"], raw=True)
c.check("while the stored hash is not a working URL", st == 404, str(st))

print("\n== a signing link ==")
st, sig = c.call("POST", "/api/signatures", {
    "doc_kind": "protocol", "doc_title": "At rest", "doc_url": "/protocol/1",
    "level": "simple", "signer_name": "T", "signer_email": "t@example.com"})
stok = sig.get("token")
c.check("issued", bool(stok), str(sig)[:140])
srow = raw("SELECT token, token_enc FROM signatures ORDER BY id DESC LIMIT 1")[0]
c.check("stored as a hash", HEX64.match(srow["token"]), srow["token"][:20])
c.check("the link is not readable from the row", stok not in json.dumps(dict(srow)), stok[:12])
st, page = c.call("GET", "/sign/%s" % stok, raw=True)
c.check("the real link opens", st == 200, str(st))
st, page = c.call("GET", "/sign/%s" % srow["token"], raw=True)
c.check("the stored hash does not", st == 404, str(st))
st, again = c.call("GET", "/api/signatures?order_id=")
c.check("and the app can still show the link again",
        any(x.get("sign_url") for x in (again if isinstance(again, list) else [])), str(again)[:120])

print("\n== a published reference ==")
st, r = c.call("POST", "/api/reference", {"fields": {}})
rtok = r["url"].rsplit("/", 1)[-1]
rrow = raw("SELECT token, token_enc FROM org_references LIMIT 1")[0]
c.check("stored as a hash", HEX64.match(rrow["token"]), rrow["token"][:20])
c.check("not readable from the row", rtok not in json.dumps(dict(rrow)), rtok[:12])
st, html = c.call("GET", "/ref/%s" % rtok, raw=True)
c.check("the real link opens", st == 200, str(st))
st, html = c.call("GET", "/ref/%s" % rrow["token"], raw=True)
c.check("the stored hash does not", st == 404, str(st))
st, r2 = c.call("GET", "/api/reference")
c.check("and the same link is shown again", r2["url"].endswith(rtok), str(r2["url"])[-30:])

print("\n== an integration key ==")
st, k = c.call("POST", "/api/integration/keys", {"kind": "secret", "label": "at-rest"})
plain = k["key"]
c.check("handed over once", plain.startswith("sk_seam_"), plain[:12])
krow = raw("SELECT key FROM api_keys WHERE kind='secret' ORDER BY id DESC LIMIT 1")[0]
c.check("stored as a hash", HEX64.match(krow["key"]), krow["key"][:20])
st, keys = c.call("GET", "/api/integration/keys")
sk = [x for x in keys if x["kind"] == "secret"][0]
c.check("and never handed out again", not sk.get("key"), str(sk))
# A public key is the opposite: it belongs in a URL on the company's own site.
st, pk = c.call("POST", "/api/integration/keys", {"kind": "public", "label": "form"})
prow = raw("SELECT key FROM api_keys WHERE kind='public' ORDER BY id DESC LIMIT 1")[0]
c.check("a public key stays readable, because it is not a secret",
        prow["key"] == pk["key"], "%s %s" % (prow["key"][:14], pk["key"][:14]))

print("\n== the second factor ==")
st, s = c.call("POST", "/api/auth/2fa/setup", {})
secret = s["secret"]
trow = raw("SELECT secret FROM user_totp LIMIT 1")[0]
c.check("the shared secret is encrypted at rest", str(trow["secret"]).startswith("enc:"),
        str(trow["secret"])[:24])
c.check("the base32 secret is not in the row", secret not in str(trow["secret"]), secret[:10])
st, r = c.call("POST", "/api/auth/2fa/enable", {"code": totp.code_at(secret, totp._steps())})
c.check("and it still verifies a real code", st == 200 and r.get("enabled"), str(r)[:120])
c.call("POST", "/api/auth/2fa/disable", {"password": "demo1234"})

print("\n== nothing usable is left lying in the file ==")
blob = io.open(DB, "rb").read()
# The signed cookie itself: the session id inside it is what the stored hash is
# a hash *of*, so the cookie is the thing that must not appear in the file.
cookie = [ck.value for ck in c.cj if ck.name == "session"][0]
for label, needle in (("the session cookie", cookie), ("the calendar link", tok),
                      ("the signing link", stok), ("the reference link", rtok),
                      ("the integration key", plain), ("the TOTP secret", secret)):
    c.check("%s is not in the database file" % label,
            needle.encode("utf-8") not in blob, needle[:14])
c.check("but the business data is (it is what backups are for)",
        b"Nordic" in blob or b"seam.demo" in blob, "")

print("\n== a backup carries data, not keys ==")
st, b = c.call("POST", "/api/admin/backup", {})
c.check("taken", st == 200 and b.get("name"), str(b)[:120])
st, blob = c.call("GET", "/api/admin/backup/%s" % b["name"], binary=True)
# On an instance with SEAM_BACKUP_PASSPHRASE set - which is what the go-live
# checks ask for before any off-site copy - the archive is sealed, not a zip.
# Open it the way an operator would rather than assuming the weaker setup.
import restore_backup as RB
if b["name"].endswith(".enc"):
    c.check("the sealed archive carries its header", blob[:8] == RB.MAGIC, str(blob[:8]))
    blob = RB.decrypt(blob, os.environ.get("SEAM_BACKUP_PASSPHRASE", "")) or b""
    c.check("and the restore tool opens it", bool(blob), "decrypt returned nothing")
z = zipfile.ZipFile(io.BytesIO(blob))
names = z.namelist()
c.check("the database is in it", "seam.db" in names, str(names[:4]))
c.check("no .secret", not any(".secret" in n for n in names), str(names))
c.check("no .data_key", not any("data_key" in n for n in names), str(names))
c.check("no push key", not any("vapid" in n for n in names), str(names))
c.check("and it says so", "NOT in this archive" in z.read("RESTORE.txt").decode("utf-8"), "")
# Without .data_key the encrypted columns in that database are unreadable, which
# is the point: a backup is worth the business data and nothing more.
inner = z.read("seam.db")
c.check("the backup's TOTP secrets are encrypted too", b"enc:" in inner, "")

print("\n== the passphrase option ==")
salt = b"0123456789abcdef"
key = cryptobox.derive("a passphrase", salt)
c.check("scrypt returns a 32-byte key", len(key) == 32, str(len(key)))
c.check("and the same passphrase gives the same key",
        cryptobox.derive("a passphrase", salt) == key, "")
box = cryptobox.seal(key, b"backup bytes", b"SEAMBK01")
c.check("an encrypted archive round-trips",
        cryptobox.open_box(key, box, b"SEAMBK01") == b"backup bytes", "")
c.check("a different passphrase does not open it",
        cryptobox.open_box(cryptobox.derive("wrong", salt), box, b"SEAMBK01") is None, "")

print("\n== the go-live checks report all of this ==")
st, pf = c.call("GET", "/api/admin/preflight")
keys_ = {x["key"]: x for x in pf["checks"]}
c.check("key files are checked", "keys_locked_down" in keys_, str(sorted(keys_))[:200])
c.check("backup encryption is checked", "backup_encrypted" in keys_, str(sorted(keys_))[:200])

sys.exit(c.summary())
