#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
BACKUP="/opt/appbox-manager-poc-backup-v0.3.3-$(date +%F-%H%M%S)"

echo "[1/6] Sauvegarde"
cp -a "$TARGET" "$BACKUP"

echo "[2/6] Vérification du réseau partagé"
docker network inspect appbox-shared >/dev/null 2>&1 || {
  echo "ERREUR : réseau appbox-shared absent."
  exit 1
}

echo "[3/6] Installation V0.3.3"
cp -a "$SOURCE/app" "$TARGET/"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"

echo "[4/6] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[5/6] Reconstruction"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[6/6] Contrôles"
for i in $(seq 1 45); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health.json 2>/dev/null && break
  sleep 1
done
python3 -m json.tool /tmp/appbox-health.json
docker logs appbox-manager-artemis --tail 20

echo
echo "V0.3.3 installée."
echo "Backup : $BACKUP"
