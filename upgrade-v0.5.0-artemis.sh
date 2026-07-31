#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.5.0-${STAMP}"

echo "[1/8] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/8] Sauvegarde SQLite indépendante"
mkdir -p "$BACKUP/database"
cp -a "$TARGET/data/appbox-manager.db"* "$BACKUP/database/" 2>/dev/null || true

echo "[3/8] Arrêt contrôlé"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/8] Installation V0.5.0"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.5.0.md" "$TARGET/"

echo "[5/8] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[6/8] Reconstruction"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[7/8] Attente du healthcheck"
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v050.json 2>/dev/null && break
  sleep 1
done

echo "[8/8] Contrôles"
python3 -m json.tool /tmp/appbox-health-v050.json
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.5.0.js'
curl -fsS http://127.0.0.1:8090/changelog | grep -q 'Workflow Engine persistant'
docker exec appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
print("job_steps:", con.execute("SELECT COUNT(*) FROM job_steps").fetchone()[0])
print("index:", con.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ux_job_steps_job_key'").fetchone())
PY
docker logs appbox-manager-artemis --tail 30

echo
echo "V0.5.0 installée."
echo "Backup : $BACKUP"
