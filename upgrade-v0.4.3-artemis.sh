#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.4.3-${STAMP}"

echo "[1/7] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/7] Arrêt contrôlé"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[3/7] Installation V0.4.3"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.4.3.md" "$TARGET/"

echo "[4/7] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[5/7] Reconstruction sans cache"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate

echo "[6/7] Attente du healthcheck"
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v043.json 2>/dev/null && break
  sleep 1
done

echo "[7/7] Contrôles"
python3 -m json.tool /tmp/appbox-health-v043.json
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.4.3.js'
curl -fsS http://127.0.0.1:8090/changelog >/dev/null
curl -fsS 'http://127.0.0.1:8090/api/nodes/artemis/logs?source=provisioner&lines=20' >/dev/null
curl -fsS http://127.0.0.1:8090/static/app-v0.4.3.js | grep -Fq "\$\$('.deploy-step',\$('#job-steps'))"
docker logs appbox-manager-artemis --tail 25

echo
echo "V0.4.3 installée."
echo "Backup : $BACKUP"
