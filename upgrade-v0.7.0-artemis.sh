#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.7.0-${STAMP}"
HEALTH="/tmp/appbox-health-v070.json"

rollback() {
  echo
  echo "[ROLLBACK] Échec V0.7.0"
  docker logs appbox-manager-artemis --tail 160 || true
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

echo "[4/9] Installation"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.7.0.md" "$TARGET/"

echo "[5/9] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[6/9] Reconstruction sans cache"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

echo "[7/9] Healthcheck"
rm -f "$HEALTH"
for i in $(seq 1 90); do
  curl -fsS http://127.0.0.1:8090/health >"$HEALTH" 2>/dev/null && break
  docker inspect -f '{{.State.Running}}' appbox-manager-artemis 2>/dev/null | grep -qx true
  sleep 1
done
test -s "$HEALTH"
python3 -m json.tool "$HEALTH"
grep -q '"version":"0.7.0"' "$HEALTH"
grep -q '"reference_images":true' "$HEALTH"

echo "[8/9] Contrôles UI"
curl -fsS http://127.0.0.1:8090/reference-images | grep -q 'Images Plex et Jellyfin de référence'
curl -fsS http://127.0.0.1:8090/appboxes | grep -q 'Options avancées'
curl -fsS http://127.0.0.1:8090/appboxes | grep -q 'Profil de déploiement'
curl -fsS http://127.0.0.1:8090/storage | grep -q 'Profils de déploiement'
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.7.0.js'

echo "[9/9] Contrôles SQLite"
docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
required={
    "reference_images",
    "reference_image_versions",
    "node_reference_cache",
}
present={r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
)}
missing=sorted(required-present)
assert not missing, f"Tables absentes : {missing}"

appbox_cols={r[1] for r in con.execute("PRAGMA table_info(appboxes)")}
profile_cols={r[1] for r in con.execute("PRAGMA table_info(provisioning_profiles)")}
assert {"reference_image_id","reference_version_id","acceleration_mode"} <= appbox_cols
assert {"reference_image_id","reference_version_id","acceleration_mode"} <= profile_cols

print("Profils :", con.execute("SELECT COUNT(*) FROM provisioning_profiles").fetchone()[0])
print("Images de référence :", con.execute("SELECT COUNT(*) FROM reference_images").fetchone()[0])
print("Versions :", con.execute("SELECT COUNT(*) FROM reference_image_versions").fetchone()[0])
PY

trap - ERR
docker logs appbox-manager-artemis --tail 50
echo
echo "V0.7.0 installée. Backup : $BACKUP"
