#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.9.0-${STAMP}"
HEALTH="/tmp/appbox-health-v090.json"

rollback() {
  echo
  echo "[ROLLBACK] Échec V0.9.0"
  docker logs appbox-manager-artemis --tail 180 || true
  rm -rf "${TARGET}.failed"
  mv "$TARGET" "${TARGET}.failed" 2>/dev/null || true
  cp -a "$BACKUP" "$TARGET"
  cd "$TARGET"
  docker compose up -d --build --force-recreate
  echo "Version précédente restaurée : $BACKUP"
}
trap rollback ERR

echo "[1/9] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/9] Sauvegarde SQLite"
mkdir -p "$BACKUP/database"
cp -a "$TARGET/data/appbox-manager.db"* "$BACKUP/database/" 2>/dev/null || true

echo "[3/9] Arrêt"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/9] Installation V0.9.0"
rm -rf "$TARGET/app" "$TARGET/agent"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/agent" "$TARGET/agent"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.9.0.md" "$TARGET/"

echo "[5/9] Validations"
python3 -m py_compile "$TARGET/app/main.py"
python3 -m py_compile "$TARGET/agent/marinos-appbox-agent.py"
bash -n "$TARGET/agent/install-agent.sh"

echo "[6/9] Reconstruction sans cache"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

echo "[7/9] Healthcheck"
rm -f "$HEALTH"
for i in $(seq 1 90); do
  curl -fsS http://127.0.0.1:8090/health >"$HEALTH" 2>/dev/null && break
  sleep 1
done
test -s "$HEALTH"
python3 -m json.tool "$HEALTH"
grep -q '"version":"0.9.0"' "$HEALTH"
grep -q '"agent_api_v1":true' "$HEALTH"

echo "[8/9] Contrôles HTTP"
curl -fsS http://127.0.0.1:8090/agents | grep -q 'Control Plane Agent V1'
curl -fsS http://127.0.0.1:8090/nodes | grep -q 'Registre des nodes'
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.9.0.js'

echo "[9/9] Contrôles SQLite"
docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
required={
 "agent_enrollment_tokens",
 "agent_commands",
 "node_metrics",
}
present={r[0] for r in con.execute(
 "SELECT name FROM sqlite_master WHERE type='table'"
)}
missing=sorted(required-present)
assert not missing, f"Tables absentes : {missing}"
print("Tables agent :", sorted(required))
print("Nodes :", con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
print("Agents :", con.execute("SELECT COUNT(*) FROM node_agents").fetchone()[0])
PY

trap - ERR
docker logs appbox-manager-artemis --tail 60
echo
echo "V0.9.0 installée. Backup : $BACKUP"
