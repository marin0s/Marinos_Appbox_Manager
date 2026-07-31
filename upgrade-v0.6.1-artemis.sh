#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.6.1-${STAMP}"
HEALTH="/tmp/appbox-health-v061.json"

rollback() {
  echo "[ROLLBACK] Échec V0.6.1"
  docker logs appbox-manager-artemis --tail 120 || true
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
echo "[2/8] Arrêt"
cd "$TARGET"; docker compose stop appbox-manager || true
echo "[3/8] Installation"
rm -rf "$TARGET/app"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.6.1.md" "$TARGET/"
echo "[4/8] Validation"
python3 -m py_compile "$TARGET/app/main.py"
echo "[5/8] Reconstruction"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager
echo "[6/8] Healthcheck"
rm -f "$HEALTH"
for i in $(seq 1 90); do
  curl -fsS http://127.0.0.1:8090/health >"$HEALTH" 2>/dev/null && break
  docker inspect -f '{{.State.Running}}' appbox-manager-artemis 2>/dev/null | grep -qx true
  sleep 1
done
test -s "$HEALTH"
python3 -m json.tool "$HEALTH"
grep -q '"version":"0.6.1"' "$HEALTH"
echo "[7/8] Contrôles UI"
curl -fsS http://127.0.0.1:8090/storage | grep -q 'Profils de déploiement'
curl -fsS http://127.0.0.1:8090/storage | grep -q 'Ouvrir / modifier'
curl -fsS http://127.0.0.1:8090/appboxes | grep -q 'RDAD Standard'
curl -fsS http://127.0.0.1:8090/ | grep -q 'app-v0.6.1.js'
echo "[8/8] Contrôles métier"
docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
active=con.execute("SELECT COUNT(*) FROM appboxes WHERE node_id='artemis' AND status!='deleted'").fetchone()[0]
groups=con.execute("SELECT COUNT(*) FROM mount_groups WHERE enabled=1").fetchone()[0]
profiles=con.execute("SELECT COUNT(*) FROM provisioning_profiles WHERE enabled=1").fetchone()[0]
print("AppBox actives ARTEMIS :", active)
print("Groupes actifs :", groups)
print("Profils actifs :", profiles)
assert groups > 0
assert profiles > 0
PY
trap - ERR
echo "V0.6.1 installée. Backup : $BACKUP"
