#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
BACKUP="/opt/appbox-manager-poc-backup-$(date +%F-%H%M%S)"

cd "$(dirname "$0")"
echo "[1/5] Sauvegarde de l'installation actuelle vers $BACKUP"
cp -a "$TARGET" "$BACKUP"

echo "[2/5] Conservation des données persistantes"
cp -a "$TARGET/data" /tmp/appbox-data-preserve
cp -a "$TARGET/generated" /tmp/appbox-generated-preserve
[ -f "$TARGET/.env" ] && cp -a "$TARGET/.env" /tmp/appbox-env-preserve

echo "[3/5] Mise à jour des fichiers applicatifs"
cp -a Dockerfile requirements.txt docker-compose.yml app "$TARGET/"
rm -rf "$TARGET/data" "$TARGET/generated"
mv /tmp/appbox-data-preserve "$TARGET/data"
mv /tmp/appbox-generated-preserve "$TARGET/generated"
[ -f /tmp/appbox-env-preserve ] && mv /tmp/appbox-env-preserve "$TARGET/.env"

echo "[4/5] Reconstruction"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[5/5] Validation"
curl -fsS http://127.0.0.1:8090/health
echo
echo "Mise à jour terminée. Backup: $BACKUP"
