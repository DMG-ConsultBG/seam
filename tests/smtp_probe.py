# -*- coding: utf-8 -*-
"""Does each provider actually answer, on the port and in the mode we claim?

Not a test - it needs the open internet, so it is not in the suite. It is the
thing to run before telling somebody a preset will work: the mock proves our
side of the conversation, and this proves theirs.

It connects, completes the handshake, says EHLO and hangs up. No credentials,
no message, nothing sent. What it cannot tell you is whether *your* mailbox is
allowed to relay - only your own password can answer that.

    .venv\\Scripts\\python tests\\smtp_probe.py
    .venv\\Scripts\\python tests\\smtp_probe.py abv icloud
"""
import os
import ssl
import sys
import socket
import smtplib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mailer                                               # noqa: E402

#: Mirrors SMTP_PRESETS in static/js/app.js. Kept here rather than parsed out
#: of the JavaScript, and the suite checks the two agree.
PRESETS = [
    ("abv",         "smtp.abv.bg",          465, True),
    ("mailbg",      "smtp.mail.bg",         465, True),
    ("icloud",      "smtp.mail.me.com",     587, False),
    ("gmail",       "smtp.gmail.com",       587, False),
    ("outlook",     "smtp.office365.com",   587, False),
    ("zoho",        "smtp.zoho.eu",         587, False),
    ("brevo",       "smtp-relay.brevo.com", 587, False),
    ("mailgun",     "smtp.eu.mailgun.org",  587, False),
    ("sendgrid",    "smtp.sendgrid.net",    587, False),
    ("postmark",    "smtp.postmarkapp.com", 587, False),
    # live, not sandbox: the sandbox host accepts everything and delivers
    # none of it, so it is the wrong thing to be reachable from production.
    ("mailtrap",    "live.smtp.mailtrap.io", 587, False),
]
TIMEOUT = 12


def probe(host, port, implicit):
    """Returns (ok, note). Never raises; a provider being down is not a fault
    of ours and should read as one line, not a traceback."""
    s = None
    try:
        ctx = ssl.create_default_context()
        if implicit:
            s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
            mode = "SSL"
        else:
            s = smtplib.SMTP(host, port, timeout=TIMEOUT)
            s.ehlo()
            s.starttls(context=ctx)
            mode = "STARTTLS"
        code, caps = s.ehlo()
        text = caps.decode("utf-8", "replace") if isinstance(caps, bytes) else str(caps)
        auth = ""
        for line in text.splitlines():
            if line.upper().startswith("AUTH"):
                auth = line.strip()
        cert = s.sock.getpeercert() if hasattr(s.sock, "getpeercert") else {}
        names = [v for k, v in (cert.get("subject") or [(None, None)])[-1] if k == "commonName"]
        return True, "%s ok, cert %s, %s" % (mode, names[0] if names else "?", auth or "no AUTH line")
    except ssl.SSLCertVerificationError as e:
        return False, "certificate rejected: %s" % e
    except (socket.timeout, TimeoutError):
        # The exact failure a wrong encryption mode produces: a plaintext
        # greeting pushed into a TLS socket, and nobody answering.
        return False, "timed out - wrong port or encryption mode, or blocked outbound"
    except (smtplib.SMTPException, OSError) as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:                               # noqa: BLE001
                pass


def main():
    want = set(a.lower() for a in sys.argv[1:])
    rows = [p for p in PRESETS if not want or p[0] in want]
    print("Probing %d provider(s). Nothing is sent and no password is used.\n" % len(rows))
    good = 0
    for name, host, port, implicit in rows:
        ok, note = probe(host, port, implicit)
        good += 1 if ok else 0
        print("  %-4s %-10s %-22s :%-4d %s" % ("ok" if ok else "FAIL", name, host, port, note))
    print("\n%d of %d answered as expected." % (good, len(rows)))
    print("This proves the host, the port and the encryption mode. Whether your\n"
          "own mailbox may relay is a question only your password can answer.")
    return 0 if good == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
