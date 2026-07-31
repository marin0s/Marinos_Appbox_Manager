#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.4.0-${STAMP}"

echo "[1/8] Sauvegarde complète"
cp -a "$TARGET" "$BACKUP"

echo "[2/8] Vérification des prérequis"
docker version >/dev/null
docker compose version >/dev/null
docker network inspect appbox-shared >/dev/null

echo "[3/8] Arrêt contrôlé du manager"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/8] Installation des fichiers V0.4.0"
cp -a "$SOURCE/app" "$TARGET/"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.4.0.md" "$TARGET/"

echo "[5/8] Préservation des données existantes"
mkdir -p "$TARGET/data" "$TARGET/generated"
test -f "$TARGET/data/appboxes.json" && cp -a "$TARGET/data/appboxes.json" "$TARGET/data/appboxes.json.pre-v0.4.0-${STAMP}"
test -f "$TARGET/data/jobs.json" && cp -a "$TARGET/data/jobs.json" "$TARGET/data/jobs.json.pre-v0.4.0-${STAMP}"

echo "[6/8] Validation Python"
python3 -m py_compile "$TARGET/app/main.py"

echo "[7/8] Reconstruction et démarrage"
cd "$TARGET"
docker compose up -d --build --force-recreate

echo "[8/8] Contrôles"
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health-v040.json 2>/dev/null; then
    break
  fi
  sleep 1
done
python3 -m json.tool /tmp/appbox-health-v040.json
docker exec appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
for table in ("nodes","appboxes","jobs","job_steps","events","node_metrics"):
    print(f"{table}: {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]}")
PY
docker logs appbox-manager-artemis --tail 30

echo
echo "V0.4.0 installée."
echo "Backup : $BACKUP"
