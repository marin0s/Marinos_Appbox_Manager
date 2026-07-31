#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.6.0-${STAMP}"

echo "[1/9] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/9] Sauvegarde SQLite"
mkdir -p "$BACKUP/database"
cp -a "$TARGET/data/appbox-manager.db"* "$BACKUP/database/" 2>/dev/null || true

echo "[3/9] Arrêt contrôlé"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/9] Installation V0.6.0"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.6.0.md" "$TARGET/"

echo "[5/9] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[6/9] Reconstruction sans cache"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

echo "[7/9] Attente"
for i in $(seq 1 90); do
  curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v060.json 2>/dev/null && break
  sleep 1
done

echo "[8/9] Contrôles HTTP"
python3 -m json.tool /tmp/appbox-health-v060.json
curl -fsS http://127.0.0.1:8090/storage | grep -q 'Volume Mounts'
curl -fsS http://127.0.0.1:8090/appboxes | grep -q 'Catalogue de référence'
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.6.0.js'

echo "[9/9] Contrôles SQLite"
docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
required={
 "storage_mounts","mount_groups","mount_group_members",
 "catalog_snapshots","provisioning_profiles",
 "appbox_mounts","snapshot_deployments"
}
present={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
missing=sorted(required-present)
assert not missing, f"Tables absentes : {missing}"
for table in sorted(required):
    print(f"{table:26s}", con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
PY

docker logs appbox-manager-artemis --tail 40
echo
echo "V0.6.0 installée. Backup : $BACKUP"
