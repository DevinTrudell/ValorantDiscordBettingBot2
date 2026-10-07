#!/usr/bin/env bash
# Installs the bot as a service on Debian/Ubuntu (e.g. a Proxmox LXC). Run as root, from the unpacked bot folder:
#   bash deploy/setup.sh
# Safe to run again after copying in a newer version: it keeps .env and valbet.db.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/valbet

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync avahi-daemon >/dev/null
# avahi makes this container answer to <hostname>.local on the home network, which the Overwolf
# "HomeAssistant Game Events" app needs (it only sends to .local addresses).
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

id valbet >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin valbet
mkdir -p "$DEST"

if [ "$SRC" != "$DEST" ]; then
  # Copy the code; never overwrite the server's own .env or database if they already exist there.
  rsync -a --exclude .venv --exclude __pycache__ --exclude '*.tar.gz' \
        $( [ -f "$DEST/.env" ] && echo --exclude .env ) \
        $( [ -f "$DEST/valbet.db" ] && echo "--exclude valbet.db --exclude valbet.db-wal --exclude valbet.db-shm" ) \
        "$SRC"/ "$DEST"/
fi

[ -f "$DEST/.env" ] || { echo "Missing $DEST/.env (copy yours from the PC)"; exit 1; }

python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

chown -R valbet:valbet "$DEST"
chmod 600 "$DEST/.env"

install -m 644 "$DEST/deploy/valbet.service" /etc/systemd/system/valbet.service
systemctl daemon-reload
systemctl enable --now valbet
systemctl restart valbet
sleep 8

echo
systemctl --no-pager --lines=0 status valbet || true
echo
journalctl -u valbet --no-pager -n 6
echo
echo "Done. Live log: journalctl -u valbet -f    Restart: systemctl restart valbet"
echo "Overwolf webhook host: $(hostname).local  (on the gaming PC: python ha_link.py --host $(hostname))"
