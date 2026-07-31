#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.8.0-${STAMP}"
HEALTH="/tmp/appbox-health-v080.json"

rollback() {
  echo
  echo "[ROLLBACK] Échec V0.8.0"
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

echo "[4/9] Installation V0.8.0"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.8.0.md" "$TARGET/"

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
grep -q '"version":"0.8.0"' "$HEALTH"
grep -q '"bare_metal_exclusion":true' "$HEALTH"

echo "[8/9] Contrôles HTTP"
curl -fsS http://127.0.0.1:8090/nodes | grep -q 'Registre des nodes'
curl -fsS http://127.0.0.1:8090/agents | grep -q 'Agents des nodes'
curl -fsS http://127.0.0.1:8090/distribution | grep -q 'Matrice de distribution'
curl -fsS http://127.0.0.1:8090/deployments | grep -q 'Décisions de placement'
curl -fsS http://127.0.0.1:8090/appboxes | grep -q 'Placement'
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.8.0.js'

echo "[9/9] Contrôles SQLite et politique"
docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
con.row_factory=sqlite3.Row
required={
 "node_tags","node_tag_assignments","placement_settings",
 "placement_decisions","node_agents",
 "reference_image_distribution","control_plane_deployments",
}
present={r[0] for r in con.execute(
 "SELECT name FROM sqlite_master WHERE type='table'"
)}
missing=sorted(required-present)
assert not missing, f"Tables absentes : {missing}"

tags={
 r["tag_id"] for r in con.execute(
  "SELECT tag_id FROM node_tag_assignments WHERE node_id='artemis'"
 )
}
assert "appbox-node" in tags
assert "control-plane" in tags
policy=con.execute(
 "SELECT * FROM placement_settings WHERE setting_id='global'"
).fetchone()
assert policy["default_mode"] == "manual"
assert policy["automatic_required_tag"] == "appbox-node"
assert policy["automatic_excluded_tag"] == "bare-metal"
print("Tags ARTEMIS :", sorted(tags))
print("Placement :", dict(policy))
print("Agent :", dict(con.execute(
 "SELECT * FROM node_agents WHERE node_id='artemis'"
).fetchone()))
PY

trap - ERR
docker logs appbox-manager-artemis --tail 60
echo
echo "V0.8.0 installée. Backup : $BACKUP"
