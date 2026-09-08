#!/usr/bin/env bash
#
# Put Seam on a fresh Ubuntu or Debian machine, with HTTPS, as a service that
# restarts itself.
#
#   sudo bash deploy/install.sh seam.example.com you@example.com
#
# The first argument is the domain that already points at this machine. The
# second is an address for the certificate authority to warn about expiry.
#
# Safe to run twice: every step checks before it acts. It never overwrites an
# existing database, an existing key, or an existing environment file.
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
APP_USER="seam"
APP_DIR="/opt/seam"
PORT="5000"

if [[ -z "$DOMAIN" || -z "$EMAIL" ]]; then
    echo "Usage: sudo bash deploy/install.sh <domain> <email-for-certificate>"
    echo "  e.g. sudo bash deploy/install.sh seam.example.com you@example.com"
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo."
    exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "==> Installing Seam from $SRC"
echo "    domain : $DOMAIN"
echo "    into   : $APP_DIR"

# --------------------------------------------------------------------------- #
echo "==> System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl \
                       rsync gnupg debian-keyring debian-archive-keyring \
                       apt-transport-https

# Caddy rather than nginx: it obtains and renews the certificate on its own,
# and it sets the forwarded headers Seam needs without being asked. One less
# thing to get wrong, and getting it wrong here means session cookies without
# the Secure flag.
if ! command -v caddy >/dev/null 2>&1; then
    echo "==> Caddy (for HTTPS)"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy
fi

# --------------------------------------------------------------------------- #
echo "==> Account and files"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"

# Never clobber a live instance: the database, the uploads and the keys stay.
rsync -a --delete \
      --exclude 'seam.db*' --exclude 'uploads/' --exclude 'backups/' \
      --exclude '.venv/' --exclude '.secret' --exclude '.data_key' \
      --exclude '.vapid_keys' --exclude '.ref_keys' --exclude '.smtp_config' \
      --exclude '.stripe_key' --exclude '.qtsp_config' --exclude '.eid_config' \
      --exclude '.ai_config' --exclude 'seam.env' --exclude '__pycache__/' \
      "$SRC/" "$APP_DIR/"

python3 -m venv "$APP_DIR/.venv" 2>/dev/null || true
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# --------------------------------------------------------------------------- #
echo "==> Configuration"
ENV_FILE="$APP_DIR/seam.env"
if [[ ! -f "$ENV_FILE" ]]; then
    # A backup passphrase is generated rather than left blank: an unencrypted
    # archive is fine on a disk you control and wrong the moment it is copied
    # anywhere else, and nobody comes back to add it later.
    GENERATED="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    cat > "$ENV_FILE" <<EOF
# Seam configuration. Edit, then: sudo systemctl restart seam
#
# Written by install.sh. Everything below the mail block is optional; without
# mail, a user who forgets their password has no way back in, so fill that in
# before anyone else uses this.

SEAM_HOST=127.0.0.1
PORT=$PORT

# Caddy is the only thing that can reach Seam, so it may be believed about
# whether the connection outside was encrypted. Without this the session
# cookie is not marked Secure.
SEAM_TRUSTED_PROXY=*

# --- mail: fill this in ----------------------------------------------------
#SEAM_SMTP_HOST=smtp.your-provider.com
#SEAM_SMTP_PORT=587
#SEAM_SMTP_USER=you@your-domain.com
#SEAM_SMTP_PASS=
#SEAM_SMTP_FROM=noreply@your-domain.com
#
# Mailtrap, for example. Note "api" is the literal username, and the password
# is the API token. Use live.smtp.mailtrap.io, not sandbox.smtp.mailtrap.io -
# the sandbox catches mail and delivers none, so password resets would look
# like they worked and nobody would receive one.
#SEAM_SMTP_HOST=live.smtp.mailtrap.io
#SEAM_SMTP_PORT=587
#SEAM_SMTP_USER=api
#SEAM_SMTP_PASS=
#SEAM_SMTP_FROM=noreply@your-verified-domain.com

# --- backups ---------------------------------------------------------------
SEAM_BACKUP_DAILY=1
# Keep a copy of this somewhere other than this machine. Without it an
# encrypted archive cannot be opened, and there is no way around that.
SEAM_BACKUP_PASSPHRASE=$GENERATED

# --- being told when something breaks --------------------------------------
#SEAM_ALERT_URL=https://hooks.example.com/...
EOF
    chmod 600 "$ENV_FILE"
    echo "    wrote $ENV_FILE (a backup passphrase was generated - save it)"
else
    echo "    $ENV_FILE exists, left alone"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --------------------------------------------------------------------------- #
echo "==> Service"
cp "$SRC/deploy/seam.service" /etc/systemd/system/seam.service
systemctl daemon-reload
systemctl enable --now seam

echo "==> HTTPS"
cat > /etc/caddy/Caddyfile <<EOF
# Caddy obtains and renews the certificate itself, and passes through the
# X-Forwarded-* headers Seam needs to know the outside connection was HTTPS.
{
    email $EMAIL
}

$DOMAIN {
    reverse_proxy 127.0.0.1:$PORT
    request_body {
        max_size 64MB          # progress evidence can be video
    }
    encode gzip
}
EOF
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# --------------------------------------------------------------------------- #
echo "==> Checking it answers"
sleep 3
if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null; then
    echo "    healthz: ok"
else
    echo "    healthz did NOT answer. Look at: journalctl -u seam -n 50"
    exit 1
fi

cat <<EOF

Done. Seam should be at https://$DOMAIN

Next, in this order:

  1. Open the site and register. The first account is the owner.
  2. Put your mail settings in $ENV_FILE, then:
         sudo systemctl restart seam
     Until then nobody can recover a forgotten password.
  3. Remove the demo companies if you seeded any:
         cd $APP_DIR && sudo -u $APP_USER .venv/bin/python purge_demo.py
         cd $APP_DIR && sudo -u $APP_USER .venv/bin/python purge_demo.py --yes
  4. Turn on the second factor for the owner, from Profile.
  5. Open Health and run the go-live checks. Blockers must be zero.

Copy these off the machine and keep them somewhere else:
     $APP_DIR/.secret        signs session cookies
     $APP_DIR/.data_key      without it the encrypted columns cannot be read
     the SEAM_BACKUP_PASSPHRASE line in $ENV_FILE

Useful:
     sudo systemctl status seam
     sudo journalctl -u seam -f
     sudo systemctl restart seam
EOF
