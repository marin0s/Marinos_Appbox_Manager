#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.4.1-${STAMP}"

echo "[1/7] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/7] Arrêt contrôlé"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[3/7] Installation UI/UX V0.4.1"
cp -a "$SOURCE/app" "$TARGET/"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.4.1.md" "$TARGET/"

echo "[4/7] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[5/7] Reconstruction"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[6/7] Attente du healthcheck"
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v041.json 2>/dev/null && break
  sleep 1
done

echo "[7/7] Contrôles"
python3 -m json.tool /tmp/appbox-health-v041.json
curl -fsS http://127.0.0.1:8090/ >/dev/null
curl -fsS http://127.0.0.1:8090/appboxes >/dev/null
curl -fsS http://127.0.0.1:8090/nodes >/dev/null
curl -fsS http://127.0.0.1:8090/jobs >/dev/null
docker logs appbox-manager-artemis --tail 30
echo
echo "V0.4.1 installée. Backup : $BACKUP"
