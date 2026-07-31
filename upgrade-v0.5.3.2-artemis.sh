#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.5.3.2-${STAMP}"

echo "[1/7] Sauvegarde"
cp -a "$TARGET" "$BACKUP"

echo "[2/7] Arrêt"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[3/7] Installation"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.5.3.2.md" "$TARGET/"

echo "[4/7] Validation"
python3 -m py_compile "$TARGET/app/main.py"

echo "[5/7] Reconstruction sans cache"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

echo "[6/7] Attente"
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v0532.json 2>/dev/null && break
  sleep 1
done

echo "[7/7] Contrôles"
python3 -m json.tool /tmp/appbox-health-v0532.json
grep -q 'jellyfin-logo.png' "$TARGET/app/templates/detail.html"
grep -q "Port {{'Jellyfin' if item.type=='jellyfin' else 'Plex'}}" "$TARGET/app/templates/detail.html"
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.5.3.2.js'

echo "V0.5.3.2 installée. Backup : $BACKUP"
