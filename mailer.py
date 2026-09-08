# -*- coding: utf-8 -*-
"""
SMTP mailer for Seam.

Fire-and-forget email delivery in a daemon thread so a slow or broken SMTP
server can never block or fail a request. Gracefully a no-op when SMTP is not
configured (the self-host default).

Two ways to encrypt, and both are needed. Most providers use STARTTLS on 587:
connect in the clear, then upgrade. Others - abv.bg among them - listen on 465
and expect TLS from the first byte, with no plaintext phase at all. Connecting
to 465 with the STARTTLS flow does not fail cleanly; it sends a plaintext
greeting into a TLS socket and hangs until the timeout, which reads like a
network fault rather than a wrong setting.

Configuration, in priority order:
  1. Environment: SEAM_SMTP_HOST, SEAM_SMTP_PORT (default 587), SEAM_SMTP_USER,
     SEAM_SMTP_PASS, SEAM_SMTP_FROM, SEAM_SMTP_TLS (set "0" to disable STARTTLS),
     SEAM_SMTP_SSL ("1" for implicit TLS, the usual thing on port 465)
  2. JSON file ".smtp_config" next to the app:
     {"host": "...", "port": 587, "user": "...", "password": "...",
      "sender": "...", "tls": true, "ssl": false}
"""
import os
import ssl as ssl_mod
import json
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BASE = os.path.dirname(__file__)
CFG_FILE = os.path.join(BASE, ".smtp_config")


#: Ports that mean "TLS from the first byte". Used only to pick a sensible
#: default when nobody said; an explicit setting always wins.
IMPLICIT_TLS_PORTS = (465, 993, 995)


def implicit_tls(cfg):
    """Whether to open the connection already encrypted.

    Derived from the port when unset, because somebody who typed 465 meant
    SSL - and the alternative is a connection that hangs until it times out
    rather than saying what is wrong.
    """
    if cfg.get("ssl") is not None:
        return bool(cfg.get("ssl"))
    try:
        return int(cfg.get("port", 587)) in IMPLICIT_TLS_PORTS
    except (TypeError, ValueError):
        return False


def config():
    host = os.environ.get("SEAM_SMTP_HOST")
    if host:
        env_ssl = os.environ.get("SEAM_SMTP_SSL")
        return {
            "host": host,
            "port": int(os.environ.get("SEAM_SMTP_PORT", "587")),
            "user": os.environ.get("SEAM_SMTP_USER", ""),
            "password": os.environ.get("SEAM_SMTP_PASS", ""),
            "sender": os.environ.get("SEAM_SMTP_FROM", ""),
            "tls": os.environ.get("SEAM_SMTP_TLS", "1") != "0",
            "ssl": (env_ssl == "1") if env_ssl is not None else None,
        }
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE, "r", encoding="utf-8") as f:
                c = json.load(f)
            if c.get("host"):
                c.setdefault("port", 587)
                c.setdefault("tls", True)
                c.setdefault("ssl", None)
                return c
        except Exception:
            return None
    return None


def enabled():
    return config() is not None


def _send(cfg, to, subject, html):
    """Deliver one message. Returns (ok, detail); raises nothing.

    `detail` exists so the setup screen can say what actually went wrong.
    Swallowing the error was right for fire-and-forget notifications and wrong
    for a test button, which would otherwise report success for a server that
    refused the password.
    """
    sender = cfg.get("sender") or cfg.get("user") or "seam@localhost"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    s = None
    try:
        host, port = cfg["host"], int(cfg.get("port", 587))
        if implicit_tls(cfg):
            # Encrypted from the first byte. Providers on 465 - abv.bg among
            # them - never speak plaintext at all, so there is no STARTTLS
            # step to take afterwards.
            s = smtplib.SMTP_SSL(host, port, timeout=15,
                                 context=ssl_mod.create_default_context())
        else:
            s = smtplib.SMTP(host, port, timeout=15)
            if cfg.get("tls"):
                # If encryption was asked for and the server will not do it,
                # stop. Carrying on used to mean the mailbox password went over
                # the wire in the clear to a server that had just said it could
                # not protect it - the one case where failing is the safe
                # outcome.
                try:
                    s.starttls(context=ssl_mod.create_default_context())
                except (smtplib.SMTPException, OSError) as e:
                    return False, "STARTTLS refused: %s" % e
        if cfg.get("user") and cfg.get("password"):
            s.login(cfg["user"], cfg["password"])
        s.sendmail(sender, [to], msg.as_string())
        return True, "sent"
    except Exception as e:                                  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:                               # noqa: BLE001
                pass


def _deliver(cfg, to, subject, html):
    _send(cfg, to, subject, html)   # background: nothing to report to


def send_async(to, subject, html):
    cfg = config()
    if not cfg or not to:
        return False
    threading.Thread(target=_deliver, args=(cfg, to, subject, html), daemon=True).start()
    return True


def send_now(to, subject, html):
    """Synchronous, with the reason when it fails. For the setup screen."""
    cfg = config()
    if not cfg:
        return False, "not configured"
    if not to:
        return False, "no recipient"
    return _send(cfg, to, subject, html)
