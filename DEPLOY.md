# Putting Seam in front of real companies

Everything below was run and verified on this codebase, not written from
memory. Where a value is yours to supply it says so; nothing here has a
placeholder that quietly needs to stay a placeholder.

## 0. The quick way

**On a Linux machine you control** (a free Oracle Cloud VM, a €4 Hetzner box,
anything with Ubuntu or Debian and a domain pointing at it):

```sh
sudo bash deploy/install.sh seam.example.com you@example.com
```

That installs Python, Caddy, a `seam` service that restarts itself, obtains an
HTTPS certificate, writes a configuration file with a generated backup
passphrase, and checks the result answers. Roughly two minutes. It is safe to
run again; it never overwrites a database, a key, or your configuration.

**On Fly.io** (no server to look after):

```sh
fly launch --copy-config --config deploy/fly.toml --no-deploy
fly volumes create seam_data --size 1
fly deploy
```

The volume is the part that matters. SQLite lives on a disk and a container
filesystem is discarded on every deploy: without it, the database and every
uploaded document vanish at the first restart.

### Choosing where

The one hard requirement is **a real disk that survives a restart**. Anything
with an ephemeral filesystem — most free web tiers — will lose the database,
quietly, on the day it reboots. Check that before anything else.

Free options with a persistent disk do exist; terms change often enough that
you should read the current ones rather than trust a list written here. A small
paid VM is a few euros a month and avoids the question entirely.

The rest of this file is what those two commands do, for when something needs
changing by hand.

## 1. The shape of a deployment

Seam speaks plain HTTP and expects a reverse proxy in front of it to terminate
TLS. That is the only supported arrangement: it does not hold a certificate
itself.

    the internet ──HTTPS──▶ nginx / Caddy ──HTTP──▶ Seam on 127.0.0.1:5000

Bind Seam to `127.0.0.1` so the only way in is through the proxy.

## 2. Environment

Put this in a file the service manager reads, not in the shell history.

```sh
# --- required in production ------------------------------------------------
SEAM_HOST=127.0.0.1                 # never 0.0.0.0 with a proxy in front
PORT=5000
SEAM_TRUSTED_PROXY=*                # see the warning below

# --- mail: without it, nobody can recover a password -----------------------
SEAM_SMTP_HOST=smtp.your-provider.com
SEAM_SMTP_PORT=587                  # 465 means implicit TLS and is detected
SEAM_SMTP_USER=you@your-domain.com
SEAM_SMTP_PASS=...
SEAM_SMTP_FROM=noreply@your-domain.com
```

### Mailtrap

Mailtrap has two products and they behave very differently.

**Sending** actually delivers to real people. This is the one to use:

```sh
SEAM_SMTP_HOST=live.smtp.mailtrap.io
SEAM_SMTP_PORT=587
SEAM_SMTP_USER=api                  # literally the word "api"
SEAM_SMTP_PASS=<your API token>     # Sending Domains -> SMTP/API -> token
SEAM_SMTP_FROM=noreply@your-domain.com
```

**Sandbox** (`sandbox.smtp.mailtrap.io`, port 2525) catches mail and delivers
none of it. Useful while setting things up, and a trap in production: password
resets would look like they worked and nobody would ever receive one.

The sender address is the part that catches people out. Mailtrap will only
send **from a domain you have verified**, and its shared `demomailtrap.co`
demo domain delivers **only to your own account address**. That is fine for a
first test and useless for real users: a customer resetting their password
would get nothing. Verify your own domain before anyone else depends on this.

Sent mail is visible at <https://mailtrap.io/sending/email_logs>, which is the
quickest way to tell "Seam did not send it" from "the provider dropped it".

```sh

# --- backups ---------------------------------------------------------------
SEAM_BACKUP_DAILY=1
SEAM_BACKUP_PASSPHRASE=...          # required before any off-site copy
                                    # keep it somewhere other than the server

# --- being told when something breaks --------------------------------------
SEAM_ALERT_URL=https://hooks.example.com/...   # any endpoint taking a JSON POST
```

### About `SEAM_TRUSTED_PROXY`

This is the switch that most needs to be right, and it was the last real bug
found in this codebase.

Waitress discards `X-Forwarded-*` unless told a proxy is in front. That default
is correct — a header anyone can send must not be allowed to claim the
connection was encrypted — but with no way to say "there *is* a proxy", two
things went wrong silently on an HTTPS site:

* the session cookie shipped **without `Secure`**, so anyone on the network
  path could take a session;
* the go-live check reported "no https" on a correctly terminated site, sending
  whoever read it hunting for a fault that was not there.

`*` means "believe whoever connects", which is right only when the only thing
that can connect is the proxy. Seam **refuses to start** with `*` on a
non-local bind, and tells you the three ways out:

```sh
SEAM_HOST=127.0.0.1                 # let the proxy reach it there  (a VM)
SEAM_TRUSTED_PROXY=10.0.0.7         # or name the proxy
SEAM_TRUSTED_PROXY=* SEAM_PROXY_ONLY=1   # or state that the port is private
```

The last is what a container needs: it has to bind `0.0.0.0` to be reachable at
all, and only the platform's proxy can open that port. `SEAM_PROXY_ONLY` is a
separate switch because it is a claim about the network that the process has no
way to verify for itself, so it should be made deliberately.

## 3. Proxy configuration

The two headers Seam needs:

```nginx
location / {
    proxy_pass         http://127.0.0.1:5000;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   X-Forwarded-For   $remote_addr;
    client_max_body_size 64m;          # progress evidence can be video
}
```

Caddy sets both by default; `reverse_proxy 127.0.0.1:5000` is enough.

## 4. Before opening the doors

```sh
python purge_demo.py            # what would go; --yes to remove it
python purge_demo.py --yes
```

Then sign in and open **Health → go-live checks**. Run it *on the deployed
host*: over localhost the transport checks pass automatically and the screen
says which answers it could not really give.

Blockers must be zero. The three "important" ones worth clearing before real
companies arrive:

* **email** — until this is configured, a locked-out user has no way back in
* **owner_2fa** — the one account nobody else can remove
* **backup_offsite** — a backup on the same disk is not a backup

## 5. Monitoring

Point whatever you use at `/healthz`. No session needed; it answers `200
{"ok":true}`, or `503` when the database cannot be read *and written*. It says
nothing else, deliberately.

`SEAM_ALERT_URL` receives a JSON POST on an unhandled error — kind, path,
message, time, and how many were suppressed since the last one. At most one per
five minutes. No traceback and nothing identifying a person: it leaves the
building.

## 6. When something goes wrong

```sh
python restore_backup.py backups/seam-backup-....zip.enc --check   # verify
python restore_backup.py backups/seam-backup-....zip.enc           # extract
```

An encrypted archive is not a zip, and the restore notes are inside it, so use
this rather than trying to open it directly.

```sh
python unlock_2fa.py --list                # who is behind a second factor
python unlock_2fa.py owner@your-domain.com # lost authenticator, no codes left
```

Asks for the account password, cuts every session for that account, and records
the removal in the error log. There is deliberately no HTTP equivalent.

## 7. Keys to keep, separately from the server

Backups do **not** contain these, on purpose — an archive carrying the key that
signs sessions is a complete compromise if it is ever lost. Copy them somewhere
else and keep them:

    .secret        signs session cookies
    .data_key      without it, second-factor secrets, calendar, signing and
                   reference links in the database cannot be read back
    .vapid_keys    browser push

## 8. Checking a build before you ship it

```sh
python tests/run_tests.py     # 2203 checks
python tests/audit_web.py     # accessibility, links and weight, both themes
```

Both must come back clean. The suite runs with a backup passphrase set, so the
configuration it certifies is the one you are meant to deploy.
