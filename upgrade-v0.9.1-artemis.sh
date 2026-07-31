#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.9.1-${STAMP}"
HEALTH="/tmp/appbox-health-v091.json"

rollback() {
  echo
  echo "[ROLLBACK] Échec V0.9.1"
  docker logs appbox-manager-artemis --tail 180 || true
  rm -rf "${TARGET}.failed"
  mv "$TARGET" "${TARGET}.failed" 2>/dev/null || true
  cp -a "$BACKUP" "$TARGET"
  cd "$TARGET"
  docker compose up -d --build --force-recreate
  echo "Version précédente restaurée : $BACKUP"
}
trap rollback ERR

echo "[1/8] Sauvegarde"
cp -a "$TARGET" "$BACKUP"

echo "[2/8] Sauvegarde SQLite"
mkdir -p "$BACKUP/database"
cp -a "$TARGET/data/appbox-manager.db"* "$BACKUP/database/" 2>/dev/null || true

echo "[3/8] Arrêt"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/8] Installation"
rm -rf "$TARGET/app" "$TARGET/agent"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/agent" "$TARGET/agent"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.9.1.md" "$TARGET/"

echo "[5/8] Validation"
python3 -m py_compile "$TARGET/app/main.py"
python3 -m py_compile "$TARGET/agent/marinos-appbox-agent.py"
bash -n "$TARGET/agent/install-agent.sh"

echo "[6/8] Reconstruction"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

echo "[7/8] Healthcheck"
for i in $(seq 1 90); do
  curl -fsS http://127.0.0.1:8090/health >"$HEALTH" 2>/dev/null && break
  sleep 1
done
test -s "$HEALTH"
python3 -m json.tool "$HEALTH"
grep -q '"version":"0.9.1"' "$HEALTH"
grep -q '"node_editing":true' "$HEALTH"

echo "[8/8] Contrôles UI"
curl -fsS http://127.0.0.1:8090/nodes | grep -q 'Modifier'
curl -fsS http://127.0.0.1:8090/agents | grep -q 'Générer le jeton d’installation'
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.9.1.js'

trap - ERR
docker logs appbox-manager-artemis --tail 50
echo
echo "V0.9.1 installée. Backup : $BACKUP"
