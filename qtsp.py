# -*- coding: utf-8 -*-
"""
Qualified trust service providers (QTSP) - the layer that turns a Seam
signature request into a legally qualified electronic signature.

Seam never issues certificates. A qualified electronic signature (QES) can only
be created with a qualified certificate held by a supervised provider. What Seam
does is: compute the document hash, hand it to the provider the signer actually
uses, and store the returned signature together with the evidence.

Three ways in, in order of preference:

  csc     Cloud Signature Consortium API v2 - the interoperable remote-signing
          standard most European providers implement. One protocol, many
          providers. Base URI ends with /csc/v2.
  native  Provider-specific REST, where no CSC endpoint is published
          (Evrotrust, B-Trust/Borica).
  bridge  The signer holds the certificate locally - a smart card or USB token
          (КЕП in Bulgaria, e-imza in Turkey, ЭЦП in Russia). The browser cannot
          reach the card, so the signature is produced by the provider's own
          desktop tool and the detached signature is returned to Seam, which
          verifies it covers exactly the hash Seam issued.

Authoritative sources:
  EU/EEA trusted lists  https://eidas.ec.europa.eu/efda/trust-services/browse/eidas/tls
  machine-readable LOTL https://ec.europa.eu/tools/lotl/eu-lotl.xml
  CSC API v2.2          https://cloudsignatureconsortium.org/resources/csc-api-v2-2/
"""
import os
import ssl
import json
import base64
import urllib.parse
import urllib.request
import urllib.error

# Date the provider registry and the legal notes below were last checked
# against the national trusted lists. Surfaced in the UI so it is never
# mistaken for a live feed.
REGISTRY_VERIFIED = "2026-07-31"

EU_TRUSTED_LIST = "https://eidas.ec.europa.eu/efda/trust-services/browse/eidas/tls"
EU_LOTL_XML = "https://ec.europa.eu/tools/lotl/eu-lotl.xml"
CSC_SPEC = "https://cloudsignatureconsortium.org/resources/csc-api-v2-2/"


# --------------------------------------------------------------------------- #
#  Legal framework per country
#
#  Which law governs the signature, what the supervisory body is, and whether a
#  qualified signature carries the legal effect of a handwritten one. This is
#  what decides which levels Seam may offer to a company in that country.
# --------------------------------------------------------------------------- #
LEGAL = {
    "BG": {"framework": "eIDAS", "law": "Регламент (ЕС) 910/2014 и ЗЕДЕУУ",
           "supervisor": "Комисия за регулиране на съобщенията (КРС)",
           "list": EU_TRUSTED_LIST + "/BG", "qes_equals_handwritten": True},
    "RO": {"framework": "eIDAS", "law": "Regulamentul (UE) 910/2014; Legea 455/2001",
           "supervisor": "ADR / MCID", "list": EU_TRUSTED_LIST + "/RO",
           "qes_equals_handwritten": True},
    "GR": {"framework": "eIDAS", "law": "Κανονισμός (ΕΕ) 910/2014; ν. 4727/2020",
           "supervisor": "ΕΕΤΤ", "list": EU_TRUSTED_LIST + "/EL",
           "qes_equals_handwritten": True},
    "IT": {"framework": "eIDAS", "law": "Regolamento (UE) 910/2014; CAD (D.Lgs. 82/2005)",
           "supervisor": "AgID", "list": EU_TRUSTED_LIST + "/IT",
           "qes_equals_handwritten": True},
    "DE": {"framework": "eIDAS", "law": "Verordnung (EU) 910/2014; VDG",
           "supervisor": "Bundesnetzagentur", "list": EU_TRUSTED_LIST + "/DE",
           "qes_equals_handwritten": True},
    "ES": {"framework": "eIDAS", "law": "Reglamento (UE) 910/2014; Ley 6/2020",
           "supervisor": "Ministerio para la Transformación Digital",
           "list": EU_TRUSTED_LIST + "/ES", "qes_equals_handwritten": True},
    "FR": {"framework": "eIDAS", "law": "Règlement (UE) 910/2014; art. 1367 Code civil",
           "supervisor": "ANSSI", "list": EU_TRUSTED_LIST + "/FR",
           "qes_equals_handwritten": True},
    "PL": {"framework": "eIDAS", "law": "Rozporządzenie (UE) 910/2014; ustawa o usługach zaufania",
           "supervisor": "Ministerstwo Cyfryzacji / NCCert",
           "list": EU_TRUSTED_LIST + "/PL", "qes_equals_handwritten": True},
    "PT": {"framework": "eIDAS", "law": "Regulamento (UE) 910/2014; DL 12/2021",
           "supervisor": "GNS / ANS", "list": EU_TRUSTED_LIST + "/PT",
           "qes_equals_handwritten": True},
    "TR": {"framework": "national", "law": "5070 sayılı Elektronik İmza Kanunu",
           "supervisor": "BTK", "list": "https://www.btk.gov.tr/elektronik-sertifika-hizmet-saglayicilari",
           "qes_equals_handwritten": True,
           "note": "Güvenli elektronik imza elle atılan imza ile aynı hukuki sonucu doğurur."},
    "RU": {"framework": "national", "law": "Федеральный закон № 63-ФЗ «Об электронной подписи»",
           "supervisor": "Минцифры России", "list": "https://digital.gov.ru/ru/activity/govservices/2/",
           "qes_equals_handwritten": True,
           "note": "Квалифицированный сертификат руководителя юридического лица выдаёт УЦ ФНС России."},
    "UA": {"framework": "national", "law": "Закон України «Про електронні довірчі послуги» № 2155-VIII",
           "supervisor": "Мінцифри / ЦЗО", "list": "https://czo.gov.ua/ca-registry",
           "qes_equals_handwritten": True},
    "RS": {"framework": "national", "law": "Закон о електронском документу, електронској идентификацији и услугама од поверења",
           "supervisor": "Министарство информисања и телекомуникација",
           "list": "https://www.mit.gov.rs/", "qes_equals_handwritten": True},
    "MK": {"framework": "national", "law": "Закон за електронски документи, електронска идентификација и доверливи услуги",
           "supervisor": "МИОА", "list": "https://mioa.gov.mk/",
           "qes_equals_handwritten": True},
    "GB": {"framework": "uk", "law": "Electronic Communications Act 2000; retained eIDAS (UK)",
           "supervisor": "DSIT / ICO", "list": "https://www.tscheme.org/",
           "qes_equals_handwritten": False,
           "note": "UK law admits electronic signatures broadly; there is no EU-style qualified tier, "
                   "so Seam offers the advanced level as the highest for GB counterparties."},
}


# --------------------------------------------------------------------------- #
#  Provider registry
#
#  api:   csc     - Cloud Signature Consortium API v2 (base_url ends in /csc/v2)
#         native  - provider-specific REST implemented in this module
#         bridge  - certificate lives on the signer's card/token; detached
#                   signature is produced by the provider's tool and uploaded
#  Every entry carries the provider's own signup page, because API access is a
#  commercial relationship: the provider issues the credentials, not Seam.
# --------------------------------------------------------------------------- #
PROVIDERS = {
    # ---- Bulgaria -------------------------------------------------------- #
    "evrotrust": {
        "name": "Evrotrust", "country": "BG", "api": "native", "adapter": "evrotrust",
        "base_url": "https://api.evrotrust.com",
        "site": "https://evrotrust.com/business/api-integration/",
        "signup": "https://evrotrust.com/business/api-integration/",
        "needs": ["api_key"],
        "note": "Мобилно потвърждаване на самоличността; квалифициран подпис и електронна идентификация.",
    },
    "btrust": {
        "name": "B-Trust (Борика АД)", "country": "BG", "api": "native", "adapter": "btrust",
        "base_url": "https://cqes-rpuat.b-trust.bg/signing-api",
        "site": "https://www.b-trust.bg/",
        "signup": "mailto:support@borica.bg",
        "docs": "https://cqes-rpuat.b-trust.bg/signing-api/swagger-ui/index.html",
        "needs": ["relying_party_id", "client_cert", "client_key"],
        "note": "Достъпът изисква договор с Борика, relyingPartyId и клиентски SSL сертификат.",
    },
    "stampit": {
        "name": "StampIT (Информационно обслужване АД)", "country": "BG", "api": "bridge",
        "site": "https://www.stampit.org/", "signup": "https://www.stampit.org/",
        "needs": [],
    },
    "infonotary": {
        "name": "InfoNotary", "country": "BG", "api": "bridge",
        "site": "https://www.infonotary.com/", "signup": "https://www.infonotary.com/",
        "needs": [],
    },
    "sep": {
        "name": "СЕП България", "country": "BG", "api": "bridge",
        "site": "https://www.sep.bg/", "signup": "https://www.sep.bg/", "needs": [],
    },

    # ---- Romania --------------------------------------------------------- #
    "certsign": {
        "name": "certSIGN", "country": "RO", "api": "csc", "base_url": "",
        "site": "https://www.certsign.ro/", "signup": "https://www.certsign.ro/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "transsped": {
        "name": "Trans Sped", "country": "RO", "api": "csc", "base_url": "",
        "site": "https://www.transsped.ro/", "signup": "https://www.transsped.ro/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "digisign": {
        "name": "DigiSign", "country": "RO", "api": "csc", "base_url": "",
        "site": "https://www.digisign.ro/", "signup": "https://www.digisign.ro/",
        "needs": ["base_url", "client_id", "client_secret"],
    },

    # ---- Greece ---------------------------------------------------------- #
    "adacom": {
        "name": "ADACOM", "country": "GR", "api": "csc", "base_url": "",
        "site": "https://www.adacom.com/", "signup": "https://www.adacom.com/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "harica": {
        "name": "HARICA", "country": "GR", "api": "csc", "base_url": "",
        "site": "https://www.harica.gr/", "signup": "https://www.harica.gr/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "bytecomputer": {
        "name": "Byte Computer", "country": "GR", "api": "bridge",
        "site": "https://www.byte.gr/", "signup": "https://www.byte.gr/", "needs": [],
    },

    # ---- Italy ----------------------------------------------------------- #
    "infocert": {
        "name": "InfoCert", "country": "IT", "api": "csc", "base_url": "",
        "site": "https://www.infocert.it/", "signup": "https://developers.infocert.it/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "aruba": {
        "name": "Aruba PEC", "country": "IT", "api": "csc", "base_url": "",
        "site": "https://www.pec.it/", "signup": "https://www.pec.it/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "namirial": {
        "name": "Namirial", "country": "IT", "api": "csc", "base_url": "",
        "site": "https://www.namirial.com/", "signup": "https://developers.namirial.com/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "intesigroup": {
        "name": "Intesi Group", "country": "IT", "api": "csc", "base_url": "",
        "site": "https://www.intesigroup.com/", "signup": "https://www.intesigroup.com/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "actalis": {
        "name": "Actalis", "country": "IT", "api": "csc", "base_url": "",
        "site": "https://www.actalis.it/", "signup": "https://www.actalis.it/",
        "needs": ["base_url", "client_id", "client_secret"],
    },

    # ---- Germany --------------------------------------------------------- #
    "dtrust": {
        "name": "D-Trust (Bundesdruckerei)", "country": "DE", "api": "csc", "base_url": "",
        "site": "https://www.d-trust.net/", "signup": "https://www.d-trust.net/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "telesec": {
        "name": "Telekom Security (TeleSec)", "country": "DE", "api": "bridge",
        "site": "https://www.telesec.de/", "signup": "https://www.telesec.de/", "needs": [],
    },
    "bnotk": {
        "name": "Bundesnotarkammer", "country": "DE", "api": "bridge",
        "site": "https://zertifizierungsstelle.bnotk.de/",
        "signup": "https://zertifizierungsstelle.bnotk.de/", "needs": [],
    },

    # ---- Spain ----------------------------------------------------------- #
    "fnmt": {
        "name": "FNMT-RCM", "country": "ES", "api": "bridge",
        "site": "https://www.sede.fnmt.gob.es/", "signup": "https://www.sede.fnmt.gob.es/",
        "needs": [],
    },
    "uanataca": {
        "name": "Uanataca", "country": "ES", "api": "csc", "base_url": "",
        "site": "https://web.uanataca.com/", "signup": "https://web.uanataca.com/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "firmaprofesional": {
        "name": "Firmaprofesional", "country": "ES", "api": "csc", "base_url": "",
        "site": "https://www.firmaprofesional.com/", "signup": "https://www.firmaprofesional.com/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "camerfirma": {
        "name": "Camerfirma", "country": "ES", "api": "bridge",
        "site": "https://www.camerfirma.com/", "signup": "https://www.camerfirma.com/", "needs": [],
    },

    # ---- France ---------------------------------------------------------- #
    "universign": {
        "name": "Universign", "country": "FR", "api": "csc", "base_url": "",
        "site": "https://www.universign.com/", "signup": "https://www.universign.com/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "certeurope": {
        "name": "CertEurope", "country": "FR", "api": "csc", "base_url": "",
        "site": "https://www.certeurope.fr/", "signup": "https://www.certeurope.fr/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "certinomis": {
        "name": "Certinomis (Docaposte)", "country": "FR", "api": "bridge",
        "site": "https://www.certinomis.fr/", "signup": "https://www.certinomis.fr/", "needs": [],
    },
    "chambersign": {
        "name": "ChamberSign France", "country": "FR", "api": "bridge",
        "site": "https://www.chambersign.fr/", "signup": "https://www.chambersign.fr/", "needs": [],
    },

    # ---- Poland ---------------------------------------------------------- #
    "certum": {
        "name": "Certum (Asseco Data Systems)", "country": "PL", "api": "csc", "base_url": "",
        "site": "https://www.certum.pl/", "signup": "https://www.certum.pl/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "kir": {
        "name": "KIR (Szafir)", "country": "PL", "api": "csc", "base_url": "",
        "site": "https://www.elektronicznypodpis.pl/", "signup": "https://www.elektronicznypodpis.pl/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "eurocert": {
        "name": "EuroCert", "country": "PL", "api": "bridge",
        "site": "https://eurocert.pl/", "signup": "https://eurocert.pl/", "needs": [],
    },
    "sigillum": {
        "name": "Sigillum (PWPW)", "country": "PL", "api": "bridge",
        "site": "https://www.sigillum.pl/", "signup": "https://www.sigillum.pl/", "needs": [],
    },

    # ---- Portugal -------------------------------------------------------- #
    "multicert": {
        "name": "MULTICERT", "country": "PT", "api": "csc", "base_url": "",
        "site": "https://www.multicert.com/", "signup": "https://www.multicert.com/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "digitalsign": {
        "name": "DigitalSign", "country": "PT", "api": "csc", "base_url": "",
        "site": "https://www.digitalsign.pt/", "signup": "https://www.digitalsign.pt/",
        "needs": ["base_url", "client_id", "client_secret"],
    },
    "cmd": {
        "name": "Chave Móvel Digital (AMA)", "country": "PT", "api": "bridge",
        "site": "https://www.autenticacao.gov.pt/", "signup": "https://www.autenticacao.gov.pt/",
        "needs": [],
    },

    # ---- Türkiye --------------------------------------------------------- #
    "eguven": {
        "name": "E-Güven", "country": "TR", "api": "bridge",
        "site": "https://www.e-guven.com/", "signup": "https://www.e-guven.com/", "needs": [],
    },
    "turktrust": {
        "name": "TÜRKTRUST", "country": "TR", "api": "bridge",
        "site": "https://www.turktrust.com.tr/", "signup": "https://www.turktrust.com.tr/", "needs": [],
    },
    "etugra": {
        "name": "E-Tuğra", "country": "TR", "api": "bridge",
        "site": "https://www.e-tugra.com.tr/", "signup": "https://www.e-tugra.com.tr/", "needs": [],
    },
    "kamusm": {
        "name": "Kamu SM (TÜBİTAK)", "country": "TR", "api": "bridge",
        "site": "https://kamusm.bilgem.tubitak.gov.tr/",
        "signup": "https://kamusm.bilgem.tubitak.gov.tr/", "needs": [],
    },

    # ---- Russia ---------------------------------------------------------- #
    "kontur": {
        "name": "СКБ Контур", "country": "RU", "api": "bridge",
        "site": "https://ca.kontur.ru/", "signup": "https://ca.kontur.ru/", "needs": [],
    },
    "taxcom": {
        "name": "Такском", "country": "RU", "api": "bridge",
        "site": "https://taxcom.ru/", "signup": "https://taxcom.ru/", "needs": [],
    },
    "tensor": {
        "name": "Тензор", "country": "RU", "api": "bridge",
        "site": "https://tensor.ru/", "signup": "https://tensor.ru/", "needs": [],
    },
    "fns": {
        "name": "УЦ ФНС России", "country": "RU", "api": "bridge",
        "site": "https://www.nalog.gov.ru/", "signup": "https://www.nalog.gov.ru/",
        "needs": [],
        "note": "Сертификат руководителя юридического лица выдаётся бесплатно в УЦ ФНС.",
    },

    # ---- Ukraine --------------------------------------------------------- #
    "diia": {
        "name": "Дія.Підпис", "country": "UA", "api": "bridge",
        "site": "https://diia.gov.ua/", "signup": "https://diia.gov.ua/", "needs": [],
    },
    "privatbank_ca": {
        "name": "АЦСК ПриватБанк", "country": "UA", "api": "bridge",
        "site": "https://acsk.privatbank.ua/", "signup": "https://acsk.privatbank.ua/", "needs": [],
    },
    "iit": {
        "name": "АЦСК «ІІТ»", "country": "UA", "api": "bridge",
        "site": "https://iit.com.ua/", "signup": "https://iit.com.ua/", "needs": [],
    },

    # ---- Serbia ---------------------------------------------------------- #
    "posta_rs": {
        "name": "Сертификационо тело Поште Србије", "country": "RS", "api": "bridge",
        "site": "https://www.ca.posta.rs/", "signup": "https://www.ca.posta.rs/", "needs": [],
    },
    "halcom_rs": {
        "name": "Halcom", "country": "RS", "api": "bridge",
        "site": "https://www.halcom.com/rs/", "signup": "https://www.halcom.com/rs/", "needs": [],
    },
    "pks": {
        "name": "Привредна комора Србије", "country": "RS", "api": "bridge",
        "site": "https://pks.rs/", "signup": "https://pks.rs/", "needs": [],
    },

    # ---- North Macedonia ------------------------------------------------- #
    "kibs": {
        "name": "КИБС АД Скопје", "country": "MK", "api": "bridge",
        "site": "https://www.kibstrust.mk/", "signup": "https://www.kibstrust.mk/", "needs": [],
    },
    "mktelekom": {
        "name": "Македонски Телеком", "country": "MK", "api": "bridge",
        "site": "https://www.telekom.mk/", "signup": "https://www.telekom.mk/", "needs": [],
    },

    # ---- United Kingdom -------------------------------------------------- #
    "tscheme": {
        "name": "tScheme-approved provider", "country": "GB", "api": "bridge",
        "site": "https://www.tscheme.org/", "signup": "https://www.tscheme.org/", "needs": [],
        "note": "UK law has no EU-style qualified tier; the advanced level is the highest Seam offers for GB.",
    },
}


def providers_for(country):
    """Providers a company registered in `country` can realistically use, plus
    every CSC-capable provider - a qualified certificate from any EU/EEA
    provider is valid across the whole Union under eIDAS mutual recognition."""
    country = (country or "").upper()
    own, cross = [], []
    home_eu = (LEGAL.get(country, {}).get("framework") == "eIDAS")
    for key, p in PROVIDERS.items():
        entry = dict(p, key=key)
        if p["country"] == country:
            own.append(entry)
        elif home_eu and p["api"] == "csc" and LEGAL.get(p["country"], {}).get("framework") == "eIDAS":
            cross.append(entry)
    own.sort(key=lambda e: (e["api"] == "bridge", e["name"]))
    cross.sort(key=lambda e: e["name"])
    return own + cross


def max_level(country):
    """The highest signature level that is legally meaningful in `country`."""
    if LEGAL.get((country or "").upper(), {}).get("qes_equals_handwritten"):
        return "qualified"
    return "advanced"


def legal_note(country):
    return LEGAL.get((country or "").upper())


# --------------------------------------------------------------------------- #
#  Configuration
#
#  Credentials are per organisation - two companies in the same workspace use
#  different providers - with a file/env fallback for single-tenant self-hosting.
# --------------------------------------------------------------------------- #
BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, ".qtsp_config")
SECRET_FIELDS = ("client_secret", "api_key", "client_key", "relying_party_id")


def file_config():
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if os.environ.get("SEAM_QTSP_PROVIDER"):
        cfg = {
            "provider": os.environ["SEAM_QTSP_PROVIDER"],
            "base_url": os.environ.get("SEAM_QTSP_BASE_URL", ""),
            "client_id": os.environ.get("SEAM_QTSP_CLIENT_ID", ""),
            "client_secret": os.environ.get("SEAM_QTSP_CLIENT_SECRET", ""),
            "api_key": os.environ.get("SEAM_QTSP_API_KEY", ""),
            "relying_party_id": os.environ.get("SEAM_QTSP_RP_ID", ""),
            "client_cert": os.environ.get("SEAM_QTSP_CLIENT_CERT", ""),
            "client_key": os.environ.get("SEAM_QTSP_CLIENT_KEY", ""),
        }
    if cfg.get("provider") in PROVIDERS:
        return normalise(cfg)
    return None


def normalise(cfg):
    spec = PROVIDERS[cfg["provider"]]
    out = dict(cfg)
    out["api"] = spec["api"]
    out["adapter"] = spec.get("adapter", spec["api"])
    if not out.get("base_url"):
        out["base_url"] = spec.get("base_url", "")
    return out


def missing_fields(cfg):
    """Which required credentials are still empty. Drives the UI badge."""
    if not cfg or cfg.get("provider") not in PROVIDERS:
        return ["provider"]
    return [f for f in PROVIDERS[cfg["provider"]].get("needs", []) if not cfg.get(f)]


def is_ready(cfg):
    return bool(cfg) and not missing_fields(cfg)


def public_config(cfg):
    """Config safe to send to the browser - secrets replaced by a set/unset flag."""
    if not cfg:
        return None
    spec = PROVIDERS.get(cfg.get("provider")) or {}
    out = {k: v for k, v in cfg.items() if k not in SECRET_FIELDS}
    out["secrets_set"] = {f: bool(cfg.get(f)) for f in SECRET_FIELDS if f in spec.get("needs", [])}
    out["ready"] = is_ready(cfg)
    out["missing"] = missing_fields(cfg)
    out["name"] = spec.get("name")
    out["api"] = spec.get("api")
    return out


# --------------------------------------------------------------------------- #
#  HTTP plumbing
# --------------------------------------------------------------------------- #
class QtspError(Exception):
    def __init__(self, message, code="qtsp_error"):
        Exception.__init__(self, message)
        self.message = message
        self.code = code


def _ssl_context(cfg):
    """Mutual-TLS context. B-Trust authenticates the relying party with a client
    certificate rather than a bearer token."""
    if not cfg.get("client_cert"):
        return None
    ctx = ssl.create_default_context()
    try:
        ctx.load_cert_chain(cfg["client_cert"], cfg.get("client_key") or None)
    except Exception as e:
        raise QtspError("Клиентският сертификат не може да се зареди: %s" % str(e)[:150], "qtsp_cert")
    return ctx


def _call(cfg, url, payload=None, headers=None, method="POST", form=False, timeout=30):
    hdr = {"accept": "application/json", "user-agent": "Seam/1.0"}
    hdr.update(headers or {})
    data = None
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            hdr["content-type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode("utf-8")
            hdr["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(cfg)) as r:
            raw = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise QtspError("Доставчикът отказа заявката (HTTP %s): %s" % (e.code, detail))
    except Exception as e:
        raise QtspError("Доставчикът е недостъпен: %s" % str(e)[:200])
    try:
        return json.loads(raw or "{}")
    except ValueError:
        raise QtspError("Неочакван отговор от доставчика: %s" % raw[:200])


def _hex_to_b64(hex_hash):
    return base64.b64encode(bytes.fromhex(hex_hash)).decode("ascii")


# --------------------------------------------------------------------------- #
#  Adapter: Cloud Signature Consortium API v2
#
#  Flow: OAuth2 token -> credentials/list -> credentials/info ->
#        credentials/authorize (PIN + OTP) -> signatures/signHash.
#  Only the hash leaves Seam; the document itself never does.
# --------------------------------------------------------------------------- #
SHA256_OID = "2.16.840.1.101.3.4.2.1"


def csc_base(cfg):
    b = (cfg.get("base_url") or "").rstrip("/")
    if not b:
        raise QtspError("Липсва адрес на CSC услугата (base_url)", "qtsp_config")
    return b if b.endswith("/csc/v2") else b + "/csc/v2"


def csc_token(cfg):
    r = _call(cfg, csc_base(cfg) + "/oauth2/token", {
        "grant_type": "client_credentials",
        "client_id": cfg.get("client_id", ""),
        "client_secret": cfg.get("client_secret", ""),
        "scope": "service",
    }, form=True)
    tok = r.get("access_token")
    if not tok:
        raise QtspError("Доставчикът не върна access_token", "qtsp_auth")
    return tok


def csc_credentials(cfg, token, user_id=None):
    payload = {"maxResults": 20}
    if user_id:
        payload["userID"] = user_id
    r = _call(cfg, csc_base(cfg) + "/credentials/list", payload,
              headers={"Authorization": "Bearer " + token})
    return r.get("credentialIDs") or []


def csc_credential_info(cfg, token, credential_id):
    return _call(cfg, csc_base(cfg) + "/credentials/info",
                 {"credentialID": credential_id, "certificates": "single", "certInfo": True},
                 headers={"Authorization": "Bearer " + token})


def csc_send_otp(cfg, token, credential_id):
    _call(cfg, csc_base(cfg) + "/credentials/sendOTP", {"credentialID": credential_id},
          headers={"Authorization": "Bearer " + token})
    return True


def csc_authorize(cfg, token, credential_id, hashes, pin=None, otp=None):
    payload = {"credentialID": credential_id, "numSignatures": len(hashes), "hash": hashes}
    if pin:
        payload["PIN"] = pin
    if otp:
        payload["OTP"] = otp
    r = _call(cfg, csc_base(cfg) + "/credentials/authorize", payload,
              headers={"Authorization": "Bearer " + token})
    sad = r.get("SAD")
    if not sad:
        raise QtspError("Доставчикът не разреши подписването (липсва SAD)", "qtsp_auth")
    return sad


def csc_sign_hash(cfg, token, credential_id, sad, hashes):
    r = _call(cfg, csc_base(cfg) + "/signatures/signHash", {
        "credentialID": credential_id, "SAD": sad, "hash": hashes,
        "hashAlgo": SHA256_OID, "signAlgo": "1.2.840.113549.1.1.11",  # sha256WithRSA
    }, headers={"Authorization": "Bearer " + token})
    sigs = r.get("signatures") or []
    if not sigs:
        raise QtspError("Доставчикът не върна подпис", "qtsp_error")
    return sigs[0]


# --------------------------------------------------------------------------- #
#  Adapter: Evrotrust
# --------------------------------------------------------------------------- #
def evrotrust_sign(cfg, doc_title, doc_hash, signer_email, signer_name):
    r = _call(cfg, cfg["base_url"].rstrip("/") + "/api/v2/documents/sign", {
        "documentName": doc_title,
        "documentHash": _hex_to_b64(doc_hash),
        "hashAlgorithm": "SHA256",
        "identificator": signer_email or "",
        "signerName": signer_name or "",
        "dateExpire": None,
    }, headers={"Authorization": "Bearer " + cfg.get("api_key", "")})
    return r.get("transactionID") or r.get("threadID") or r.get("id") or ""


def evrotrust_status(cfg, tx):
    return _call(cfg, cfg["base_url"].rstrip("/") + "/api/v2/documents/%s/status" % urllib.parse.quote(tx),
                 headers={"Authorization": "Bearer " + cfg.get("api_key", "")}, method="GET")


# --------------------------------------------------------------------------- #
#  Adapter: B-Trust (Borica AD)
#
#  Asynchronous: POST /v2/sign returns a callbackId, the signer confirms in the
#  B-Trust mobile app, then GET /v2/sign/{callbackId} yields the signature.
# --------------------------------------------------------------------------- #
def btrust_sign(cfg, doc_title, doc_hash, signer_id):
    r = _call(cfg, cfg["base_url"].rstrip("/") + "/v2/sign", {
        "relyingPartyID": cfg.get("relying_party_id", ""),
        "contents": [{
            "confirmationText": doc_title[:120],
            "contentFormat": "BINARY_BASE64",
            "mediaType": "text/plain",
            "data": _hex_to_b64(doc_hash),
            "fileName": (doc_title[:60] or "document") + ".txt",
            "hashAlgorithm": "SHA256",
            "padesVisualSignature": False,
            "signaturePosition": None,
        }],
        "uid": signer_id or "",
        "signType": "SIGNATURE",
    })
    return r.get("data", {}).get("callbackId") or r.get("callbackId") or ""


def btrust_status(cfg, callback_id):
    return _call(cfg, cfg["base_url"].rstrip("/") + "/v2/sign/" + urllib.parse.quote(callback_id),
                 method="GET")


# --------------------------------------------------------------------------- #
#  Dispatch
# --------------------------------------------------------------------------- #
def submit(cfg, doc_title, doc_hash, signer_email="", signer_name="", signer_id=""):
    """Hand the document hash to the provider. Returns (reference, mode) where
    mode is 'async' when the signer must confirm in the provider's own app."""
    if not is_ready(cfg):
        raise QtspError("Доставчикът не е конфигуриран напълно: %s"
                        % ", ".join(missing_fields(cfg)), "qtsp_config")
    adapter = cfg.get("adapter") or PROVIDERS[cfg["provider"]]["api"]
    if adapter == "evrotrust":
        return evrotrust_sign(cfg, doc_title, doc_hash, signer_email, signer_name), "async"
    if adapter == "btrust":
        return btrust_sign(cfg, doc_title, doc_hash, signer_id or signer_email), "async"
    if adapter == "csc":
        token = csc_token(cfg)
        creds = csc_credentials(cfg, token, signer_email or None)
        if not creds:
            raise QtspError("Няма намерено квалифицирано удостоверение за този подписващ", "qtsp_nocred")
        return creds[0], "interactive"
    raise QtspError("Този доставчик няма API интеграция; използвайте локално подписване", "qtsp_bridge")


def poll(cfg, reference):
    """Check an asynchronous provider transaction. Returns (state, signature)
    where state is one of pending / signed / rejected."""
    adapter = cfg.get("adapter") or PROVIDERS[cfg["provider"]]["api"]
    if adapter == "evrotrust":
        r = evrotrust_status(cfg, reference)
        st = (r.get("status") or "").lower()
        if st in ("signed", "completed", "2"):
            return "signed", r.get("signature") or r.get("content") or ""
        if st in ("rejected", "expired", "failed"):
            return "rejected", ""
        return "pending", ""
    if adapter == "btrust":
        r = btrust_status(cfg, reference)
        d = r.get("data") or {}
        code = str(d.get("status") or r.get("responseCode") or "").upper()
        if code in ("SIGNED", "COMPLETED", "OK"):
            sigs = d.get("signatures") or []
            return "signed", (sigs[0].get("signature") if sigs else "")
        if code in ("REJECTED", "EXPIRED", "FAILED"):
            return "rejected", ""
        return "pending", ""
    return "pending", ""
