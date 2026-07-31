#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
BACKUP="/opt/appbox-manager-poc-backup-v0.3.2-$(date +%F-%H%M%S)"
SOURCE="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] Sauvegarde de la version actuelle"
cp -a "$TARGET" "$BACKUP"

echo "[2/6] Vérification du réseau partagé"
docker network inspect appbox-shared >/dev/null 2>&1 || {
  echo "ERREUR : le réseau Docker externe appbox-shared est absent."
  exit 1
}

echo "[3/6] Copie des fichiers V0.3.2"
cp -a "$SOURCE/app" "$TARGET/"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"

echo "[4/6] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[5/6] Reconstruction du manager"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[6/6] Validation"
for i in $(seq 1 45); do
  if curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health.json 2>/dev/null; then
    break
  fi
  sleep 1
done

cat /tmp/appbox-health.json | python3 -m json.tool
docker exec appbox-manager-artemis docker --version
docker exec appbox-manager-artemis docker compose version

echo
echo "V0.3.2 installée. Backup : $BACKUP"
