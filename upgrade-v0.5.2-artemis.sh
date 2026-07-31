#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.5.2-${STAMP}"

echo "[1/9] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/9] Sauvegarde SQLite indépendante"
mkdir -p "$BACKUP/database"
cp -a "$TARGET/data/appbox-manager.db"* "$BACKUP/database/" 2>/dev/null || true

echo "[3/9] Arrêt contrôlé"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/9] Installation V0.5.2"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.5.2.md" "$TARGET/"

echo "[5/9] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[6/9] Reconstruction"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[7/9] Attente du healthcheck"
for i in $(seq 1 60); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v052.json 2>/dev/null && break
  sleep 1
done

echo "[8/9] Synchronisation de l’inventaire"
curl -fsS -X POST http://127.0.0.1:8090/api/inventory/sync \
  >/tmp/appbox-inventory-v052.json

echo "[9/9] Contrôles"
python3 -m json.tool /tmp/appbox-health-v052.json
python3 -m json.tool /tmp/appbox-inventory-v052.json
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.5.2.js'
curl -fsS http://127.0.0.1:8090/inventory | grep -q 'Inventaire central'

docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3

con = sqlite3.connect("/data/appbox-manager.db")
required = {
    "nodes", "appboxes", "containers", "networks", "volumes",
    "jobs", "job_steps", "events", "notifications_queue",
    "settings_store", "templates", "port_reservations",
}
present = {
    row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
}
missing = sorted(required - present)
assert not missing, f"Tables absentes : {missing}"

for table in ("containers", "networks", "volumes", "templates", "port_reservations"):
    print(f"{table:20s}", con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
PY

docker logs appbox-manager-artemis --tail 30

echo
echo "V0.5.2 installée."
echo "Backup : $BACKUP"
