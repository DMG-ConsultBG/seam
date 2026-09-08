# -*- coding: utf-8 -*-
"""Signing in with a national electronic identity.

Why this exists
---------------
A password proves someone knows a secret. In most of the countries Seam serves,
a company can prove considerably more than that: a national electronic identity
scheme will state, with legal weight, who is at the keyboard. Where that is
available it should be usable here, and where it is not, nothing should pretend
otherwise.

How it works
------------
Every scheme below that Seam can talk to is an OpenID Connect provider, so this
module is an OIDC relying party: discovery, authorization code with PKCE, and
verification of the returned ID token. There is no shortcut here - the token is
verified against the provider's own published keys, and `iss`, `aud`, `exp` and
`nonce` are all checked, because an unverified ID token is a claim, not proof.

Two deliberate restrictions
---------------------------
1. **A provider is offered only when it is configured.** The registry says what
   exists in a country; it never produces a button that leads nowhere.
2. **Signing in this way links to an existing account; it never creates one.**
   Anyone holding a national identity could otherwise open an account here
   claiming to work at any company. The account holder attaches their identity
   from inside Seam, and only then can it be used to sign in.

Endpoints are not hard-coded. Every one of these schemes requires registering as
a relying party, and issues the discovery URL and credentials as part of that -
so the administrator supplies them, and this module reads the rest from the
provider's own `/.well-known/openid-configuration`.
"""
import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import urllib.parse
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, ".eid_config")

#: Checked against the national portals on this date. Shown in the UI so it is
#: never mistaken for a live feed.
REGISTRY_VERIFIED = "2026-08-04"

#: What exists, per country.
#:
#: `protocol` is the honest part. Seam speaks OpenID Connect; a scheme built on
#: SAML is listed so a company knows it exists, and marked so nobody is told
#: Seam can use it when it cannot.
PROVIDERS = {
    # ---- Bulgaria -------------------------------------------------------- #
    "bg_eavt": {"name": "еАвтентикация (eGov.bg)", "country": "BG", "protocol": "oidc",
                "portal": "https://egov.bg/", "authority": "Министерство на електронното управление"},
    "bg_evrotrust": {"name": "Evrotrust", "country": "BG", "protocol": "oidc",
                     "portal": "https://evrotrust.com/business/api-integration/",
                     "authority": "КРС (регистриран доставчик)"},
    "bg_btrust": {"name": "B-Trust (Борика)", "country": "BG", "protocol": "oidc",
                  "portal": "https://www.b-trust.bg/", "authority": "КРС (регистриран доставчик)"},

    # ---- Romania --------------------------------------------------------- #
    "ro_roeid": {"name": "ROeID", "country": "RO", "protocol": "oidc",
                 "portal": "https://roeid.ro/", "authority": "ADR"},

    # ---- Greece ---------------------------------------------------------- #
    "gr_govgr": {"name": "gov.gr / TAXISnet", "country": "GR", "protocol": "oidc",
                 "portal": "https://www.gov.gr/", "authority": "ΓΓΠΣΨΔ"},

    # ---- Italy ----------------------------------------------------------- #
    "it_spid": {"name": "SPID", "country": "IT", "protocol": "saml",
                "portal": "https://www.spid.gov.it/", "authority": "AgID"},
    "it_cieid": {"name": "CIE id", "country": "IT", "protocol": "oidc",
                 "portal": "https://www.cartaidentita.interno.gov.it/", "authority": "Ministero dell'Interno"},

    # ---- Germany --------------------------------------------------------- #
    "de_bundid": {"name": "BundID", "country": "DE", "protocol": "oidc",
                  "portal": "https://id.bund.de/", "authority": "Bundesministerium des Innern"},

    # ---- Spain ----------------------------------------------------------- #
    "es_clave": {"name": "Cl@ve", "country": "ES", "protocol": "saml",
                 "portal": "https://clave.gob.es/", "authority": "Ministerio para la Transformación Digital"},

    # ---- France ---------------------------------------------------------- #
    "fr_franceconnect": {"name": "FranceConnect", "country": "FR", "protocol": "oidc",
                         "portal": "https://franceconnect.gouv.fr/partenaires",
                         "authority": "DINUM"},

    # ---- Poland ---------------------------------------------------------- #
    "pl_login": {"name": "login.gov.pl / Profil Zaufany", "country": "PL", "protocol": "saml",
                 "portal": "https://www.gov.pl/web/login/", "authority": "Ministerstwo Cyfryzacji"},

    # ---- Portugal -------------------------------------------------------- #
    "pt_autenticacao": {"name": "Autenticação.gov / Chave Móvel Digital", "country": "PT",
                        "protocol": "oidc", "portal": "https://www.autenticacao.gov.pt/",
                        "authority": "AMA"},

    # ---- United Kingdom -------------------------------------------------- #
    "gb_onelogin": {"name": "GOV.UK One Login", "country": "GB", "protocol": "oidc",
                    "portal": "https://www.sign-in.service.gov.uk/", "authority": "GDS / DSIT"},

    # ---- Turkey ---------------------------------------------------------- #
    "tr_edevlet": {"name": "e-Devlet Kapısı", "country": "TR", "protocol": "oidc",
                   "portal": "https://www.turkiye.gov.tr/", "authority": "Cumhurbaşkanlığı Dijital Dönüşüm Ofisi"},

    # ---- Russia ---------------------------------------------------------- #
    "ru_esia": {"name": "ЕСИА / Госуслуги", "country": "RU", "protocol": "oidc",
                "portal": "https://partners.gosuslugi.ru/", "authority": "Минцифры России"},

    # ---- Ukraine --------------------------------------------------------- #
    "ua_idgovua": {"name": "id.gov.ua / Дія", "country": "UA", "protocol": "oidc",
                   "portal": "https://id.gov.ua/", "authority": "Мінцифри"},

    # ---- Serbia ---------------------------------------------------------- #
    "rs_eid": {"name": "eID.gov.rs", "country": "RS", "protocol": "saml",
               "portal": "https://eid.gov.rs/", "authority": "Канцеларија за ИТ и еУправу"},

    # ---- North Macedonia ------------------------------------------------- #
    "mk_portal": {"name": "Портал за е-услуги", "country": "MK", "protocol": "oidc",
                  "portal": "https://uslugi.gov.mk/", "authority": "МИОА"},
}


def catalogue(country):
    """What exists in a country, whether or not Seam can use it."""
    cc = (country or "").upper()
    out = []
    for key, p in PROVIDERS.items():
        if p["country"] != cc:
            continue
        out.append(dict(p, key=key, usable=p["protocol"] == "oidc"))
    out.sort(key=lambda e: (not e["usable"], e["name"]))
    return out


# --------------------------------------------------------------------------- #
#  Configuration
#
#  Nothing here is guessed. Registering as a relying party is how these schemes
#  work, and registration is what produces the discovery URL and the client
#  credentials; the administrator pastes them in.
# --------------------------------------------------------------------------- #
def _load():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    import cryptobox
    cryptobox.secure_file(CONFIG_FILE)


def config(key=None):
    cfg = _load()
    if os.environ.get("SEAM_EID_PROVIDER"):
        cfg = dict(cfg)
        cfg[os.environ["SEAM_EID_PROVIDER"]] = {
            "discovery": os.environ.get("SEAM_EID_DISCOVERY", ""),
            "client_id": os.environ.get("SEAM_EID_CLIENT_ID", ""),
            "client_secret": os.environ.get("SEAM_EID_CLIENT_SECRET", ""),
            "scope": os.environ.get("SEAM_EID_SCOPE", "openid"),
        }
    return cfg.get(key) if key else cfg


def set_config(key, values):
    if key not in PROVIDERS:
        raise ValueError("unknown provider")
    cfg = _load()
    if values is None:
        cfg.pop(key, None)
    else:
        cfg[key] = {k: str(values.get(k) or "").strip()
                    for k in ("discovery", "client_id", "client_secret", "scope")}
    _save(cfg)
    return cfg.get(key)


def is_ready(key):
    c = config(key) or {}
    return bool(PROVIDERS.get(key) and PROVIDERS[key]["protocol"] == "oidc"
                and c.get("discovery") and c.get("client_id"))


def enabled_providers(country=None):
    """Configured and usable. This is what a sign-in screen may offer, and
    nothing else: a button that cannot complete is worse than no button."""
    out = []
    for key, p in PROVIDERS.items():
        if country and p["country"] != (country or "").upper():
            continue
        if is_ready(key):
            out.append(dict(p, key=key))
    out.sort(key=lambda e: e["name"])
    return out


def public_config(key):
    c = config(key) or {}
    return {"key": key, "ready": is_ready(key), "discovery": c.get("discovery", ""),
            "client_id": c.get("client_id", ""), "scope": c.get("scope") or "openid",
            "has_secret": bool(c.get("secret") or c.get("client_secret"))}


# --------------------------------------------------------------------------- #
#  OpenID Connect
# --------------------------------------------------------------------------- #
_DISCOVERY_CACHE = {}
_JWKS_CACHE = {}
DISCOVERY_TTL = 3600
HTTP_TIMEOUT = 12


def _get_json(url, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def discover(key, force=False):
    c = config(key) or {}
    url = c.get("discovery") or ""
    if not url:
        raise ValueError("no discovery url")
    if not url.endswith("openid-configuration"):
        url = url.rstrip("/") + "/.well-known/openid-configuration"
    hit = _DISCOVERY_CACHE.get(url)
    if hit and not force and time.time() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    doc = _get_json(url)
    _DISCOVERY_CACHE[url] = (time.time(), doc)
    return doc


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_d(txt):
    txt = txt.strip()
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def start(key, redirect_uri):
    """Build the URL the browser is sent to, plus the state that must be kept
    for the callback. PKCE is used even with a client secret: it costs nothing
    and it removes a whole class of code-interception attack."""
    if not is_ready(key):
        raise ValueError("provider not configured")
    c = config(key)
    doc = discover(key)
    verifier = _b64u(secrets.token_bytes(48))
    challenge = _b64u(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64u(secrets.token_bytes(24))
    nonce = _b64u(secrets.token_bytes(24))
    q = {
        "response_type": "code",
        "client_id": c["client_id"],
        "redirect_uri": redirect_uri,
        "scope": c.get("scope") or "openid",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = doc["authorization_endpoint"]
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode(q)
    return {"url": url, "state": state, "nonce": nonce, "verifier": verifier}


def exchange(key, code, redirect_uri, verifier):
    c = config(key)
    doc = discover(key)
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": c["client_id"],
        "client_secret": c.get("client_secret") or "",
        "code_verifier": verifier,
    }).encode("ascii")
    req = urllib.request.Request(doc["token_endpoint"], data=data, method="POST", headers={
        "content-type": "application/x-www-form-urlencoded", "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise ValueError("token endpoint refused: %s %s" % (e.code, detail))


def jwks(key, force=False):
    doc = discover(key)
    url = doc["jwks_uri"]
    hit = _JWKS_CACHE.get(url)
    if hit and not force and time.time() - hit[0] < DISCOVERY_TTL:
        return hit[1]
    out = _get_json(url)
    _JWKS_CACHE[url] = (time.time(), out)
    return out


# ---- signature verification ----------------------------------------------- #
#: DigestInfo prefixes for RSASSA-PKCS1-v1_5, RFC 8017 section 9.2.
_ASN1 = {
    "SHA-256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "SHA-384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "SHA-512": bytes.fromhex("3051300d060960864801650304020305000440"),
}
_HASH = {"RS256": ("SHA-256", hashlib.sha256), "RS384": ("SHA-384", hashlib.sha384),
         "RS512": ("SHA-512", hashlib.sha512)}


def _int(raw):
    return int.from_bytes(raw, "big")


def _verify_rsa(alg, signed, sig, jwk):
    name, hfn = _HASH[alg]
    n, e = _int(_b64u_d(jwk["n"])), _int(_b64u_d(jwk["e"]))
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    m = pow(_int(sig), e, n)
    em = m.to_bytes(k, "big")
    digest = hfn(signed).digest()
    # EM = 0x00 || 0x01 || PS || 0x00 || DigestInfo
    expect = b"\x00\x01" + b"\xff" * (k - 3 - len(_ASN1[name]) - len(digest)) + b"\x00" \
        + _ASN1[name] + digest
    return hmac.compare_digest(em, expect)


def _verify_ec(alg, signed, sig, jwk):
    import webpush                                   # the P-256 maths already here
    if alg != "ES256" or jwk.get("crv") != "P-256" or len(sig) != 64:
        return False
    pub = b"\x04" + _b64u_d(jwk["x"]) + _b64u_d(jwk["y"])
    return webpush.verify(sig, hashlib.sha256(signed).digest(), webpush._b64(pub))


def verify_id_token(key, token, nonce, leeway=120, now=None):
    """Return the claims, or raise. Everything below is checked; an ID token
    that has not been is a claim about who someone is, not proof of it."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed id_token")
    head = json.loads(_b64u_d(parts[0]))
    claims = json.loads(_b64u_d(parts[1]))
    sig = _b64u_d(parts[2])
    signed = (parts[0] + "." + parts[1]).encode("ascii")

    alg = head.get("alg", "")
    if alg not in _HASH and alg != "ES256":
        raise ValueError("unsupported alg %r" % alg)
    keys = [k for k in jwks(key).get("keys", [])
            if not head.get("kid") or k.get("kid") == head.get("kid")]
    if not keys:                                     # rotated key: fetch again
        keys = [k for k in jwks(key, force=True).get("keys", [])
                if not head.get("kid") or k.get("kid") == head.get("kid")]
    ok = False
    for jwk in keys:
        if jwk.get("kty") == "RSA" and alg in _HASH:
            ok = _verify_rsa(alg, signed, sig, jwk)
        elif jwk.get("kty") == "EC":
            ok = _verify_ec(alg, signed, sig, jwk)
        if ok:
            break
    if not ok:
        raise ValueError("signature does not verify")

    doc = discover(key)
    c = config(key)
    t = int(now if now is not None else time.time())
    if claims.get("iss") != doc.get("issuer"):
        raise ValueError("wrong issuer")
    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    if c["client_id"] not in aud:
        raise ValueError("wrong audience")
    if int(claims.get("exp", 0)) + leeway < t:
        raise ValueError("expired")
    if int(claims.get("iat", t)) - leeway > t:
        raise ValueError("issued in the future")
    if nonce and claims.get("nonce") != nonce:
        raise ValueError("nonce does not match")
    if not claims.get("sub"):
        raise ValueError("no subject")
    return claims


def _probable_prime(bits):
    """Miller-Rabin. Only used by the self-test below - Seam never generates an
    RSA key in normal operation, it only ever verifies with someone else's."""
    small = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37]
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if any(cand % s == 0 for s in small):
            continue
        d, r = cand - 1, 0
        while d % 2 == 0:
            d //= 2
            r += 1
        for a in small:
            x = pow(a, d, cand)
            if x in (1, cand - 1):
                continue
            for _ in range(r - 1):
                x = x * x % cand
                if x == cand - 1:
                    break
            else:
                break
        else:
            return cand


def self_test():
    """Verify an RS256 token this module builds itself, against a key it
    generates itself, without touching the network. Proves the PKCS#1 v1.5
    padding and the big-integer arithmetic, which is the part worth doubting."""
    e = 65537
    while True:
        p, q = _probable_prime(512), _probable_prime(512)
        if p != q and (p - 1) % e and (q - 1) % e:
            break
    n = p * q
    d = pow(e, -1, (p - 1) * (q - 1))
    head = _b64u(json.dumps({"alg": "RS256", "kid": "t"}).encode())
    body = _b64u(json.dumps({"sub": "1", "iss": "x", "aud": "y",
                             "exp": int(time.time()) + 60}).encode())
    signed = (head + "." + body).encode()
    k = (n.bit_length() + 7) // 8
    digest = hashlib.sha256(signed).digest()
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(_ASN1["SHA-256"]) - len(digest)) + b"\x00" \
        + _ASN1["SHA-256"] + digest
    sig = pow(_int(em), d, n).to_bytes(k, "big")
    jwk = {"kty": "RSA", "n": _b64u(n.to_bytes(k, "big")), "e": _b64u(b"\x01\x00\x01")}
    good = _verify_rsa("RS256", signed, sig, jwk)
    bad = _verify_rsa("RS256", signed + b"x", sig, jwk)
    return {"ok": bool(good) and not bad, "accepts_valid": bool(good),
            "rejects_tampered": not bad, "modulus_bits": n.bit_length()}


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=1))
