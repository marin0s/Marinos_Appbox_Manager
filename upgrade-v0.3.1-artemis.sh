#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
BACKUP="/opt/appbox-manager-poc-backup-v0.3.1-$(date +%F-%H%M%S)"
cd "$(dirname "$0")"

echo "[1/5] Sauvegarde"
cp -a "$TARGET" "$BACKUP"

echo "[2/5] Mise à jour applicative"
cp -a app "$TARGET/"
cp -a Dockerfile requirements.txt docker-compose.yml "$TARGET/"

echo "[3/5] Reconstruction"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[4/5] Attente API"
for i in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8090/health >/dev/null; then
    break
  fi
  sleep 1
done

echo "[5/5] Validation"
docker exec appbox-manager-artemis docker --version
docker exec appbox-manager-artemis docker compose version
curl -fsS http://127.0.0.1:8090/api/appboxes/ab35ah/status
echo
echo "Mise à jour terminée. Backup: $BACKUP"
