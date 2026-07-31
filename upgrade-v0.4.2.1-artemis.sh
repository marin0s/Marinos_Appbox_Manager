#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.4.2.1-${STAMP}"

echo "[1/6] Sauvegarde"
cp -a "$TARGET" "$BACKUP"

echo "[2/6] Arrêt du manager"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[3/6] Installation du correctif"
cp -a "$SOURCE/app" "$TARGET/"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.4.2.1.md" "$TARGET/"

echo "[4/6] Validation"
python3 -m py_compile "$TARGET/app/main.py"

echo "[5/6] Reconstruction"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[6/6] Contrôles"
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v0421.json 2>/dev/null && break
  sleep 1
done
python3 -m json.tool /tmp/appbox-health-v0421.json
docker logs appbox-manager-artemis --tail 25
echo
echo "Correctif V0.4.2.1 installé. Backup : $BACKUP"
echo "Recharge forcée du navigateur recommandée : Ctrl+F5."
