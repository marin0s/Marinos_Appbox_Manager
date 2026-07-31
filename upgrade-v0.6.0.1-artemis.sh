#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.6.0.1-${STAMP}"
HEALTH="/tmp/appbox-health-v0601.json"

rollback() {
  echo
  echo "[ROLLBACK] La V0.6.0.1 n'a pas démarré correctement."
  docker logs appbox-manager-artemis --tail 120 || true
  cd /opt
  rm -rf "${TARGET}.failed"
  mv "$TARGET" "${TARGET}.failed" 2>/dev/null || true
  cp -a "$BACKUP" "$TARGET"
  cd "$TARGET"
  docker compose up -d --build --force-recreate
  echo "[ROLLBACK] Version précédente restaurée depuis : $BACKUP"
}
trap rollback ERR

echo "[1/9] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/9] Sauvegarde SQLite"
mkdir -p "$BACKUP/database"
cp -a "$TARGET/data/appbox-manager.db"* "$BACKUP/database/" 2>/dev/null || true

echo "[3/9] Arrêt contrôlé"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/9] Installation V0.6.0.1"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.6.0.1.md" "$TARGET/"

echo "[5/9] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[6/9] Reconstruction sans cache"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

echo "[7/9] Attente du healthcheck"
rm -f "$HEALTH"
for i in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8090/health >"$HEALTH" 2>/dev/null; then
    break
  fi
  if ! docker inspect -f '{{.State.Running}}' appbox-manager-artemis 2>/dev/null | grep -qx true; then
    echo "Le conteneur s'est arrêté pendant le démarrage."
    false
  fi
  sleep 1
done
test -s "$HEALTH"

echo "[8/9] Contrôles HTTP"
python3 -m json.tool "$HEALTH"
grep -q '"version":"0.6.0.1"' "$HEALTH"
curl -fsS http://127.0.0.1:8090/storage | grep -q 'Volume Mounts'
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.6.0.1.js'

echo "[9/9] Contrôles SQLite"
docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3
con = sqlite3.connect("/data/appbox-manager.db")
con.row_factory = sqlite3.Row

required = {
    "storage_mounts", "mount_groups", "mount_group_members",
    "catalog_snapshots", "provisioning_profiles",
    "appbox_mounts", "snapshot_deployments",
}
present = {r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
)}
missing = sorted(required - present)
assert not missing, f"Tables absentes : {missing}"

rows = con.execute("""
    SELECT mount_id,node_id,host_path,container_path
    FROM storage_mounts ORDER BY mount_id
""").fetchall()
assert rows, "Aucun Volume Mount initialisé"
for row in rows:
    print(dict(row))
PY

trap - ERR
docker logs appbox-manager-artemis --tail 40
echo
echo "V0.6.0.1 installée. Backup : $BACKUP"
