#!/usr/bin/env bash
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${APPBOX_TARGET:-/opt/appbox-manager-poc}"
HEALTH_URL="${APPBOX_HEALTH_URL:-http://127.0.0.1:8090/health}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${TARGET}.backup-v1.4.1-${STAMP}"
DB="${TARGET}/data/appbox-manager.db"

fail() { echo "[ERREUR] $*" >&2; exit 1; }
[ -d "$TARGET" ] || fail "Installation absente : $TARGET"
command -v rsync >/dev/null || fail "rsync absent"
command -v docker >/dev/null || fail "docker absent"

if [ -f "$DB" ]; then
  sqlite3 "$DB" 'PRAGMA quick_check;' | grep -qx 'ok' || fail "La base SQLite actuelle ne passe pas quick_check"
fi

cp -a "$TARGET" "$BACKUP"
echo "Sauvegarde complète : $BACKUP"

rsync -a --delete \
  --exclude data \
  --exclude generated \
  --exclude docker-compose.yml \
  --exclude .env \
  --exclude control-plane-runtime \
  "$SRC_DIR/" "$TARGET/"

cd "$TARGET"
python3 -m zipfile -c agent/appbox-agent-latest.zip \
  agent/marinos-appbox-agent.py \
  agent/marinos-appbox-agent.service \
  agent/install-agent.sh

docker compose up -d --build --remove-orphans

healthy=0
for _ in $(seq 1 60); do
  if curl -fsS "$HEALTH_URL" 2>/dev/null | grep -q '"version":"1.4.1"'; then
    healthy=1
    break
  fi
  sleep 2
done

if [ "$healthy" -ne 1 ]; then
  echo "Healthcheck 1.4.1 échoué." >&2
  echo "Rollback disponible :" >&2
  echo "  cd /opt && mv '$TARGET' '${TARGET}.failed-${STAMP}' && cp -a '$BACKUP' '$TARGET' && cd '$TARGET' && docker compose up -d --build" >&2
  exit 1
fi

if [ -f "$DB" ]; then
  sqlite3 "$DB" 'PRAGMA foreign_key_check;' | grep -q . && fail "Violations de clés étrangères après mise à niveau"
  sqlite3 "$DB" 'PRAGMA quick_check;' | grep -qx 'ok' || fail "quick_check échoué après mise à niveau"
fi

echo "AppBox Manager 1.4.1 Transaction Engine déployé avec succès."
echo "Sauvegarde conservée : $BACKUP"
echo "Réinstallez ensuite l'agent 1.4.1 sur chaque node depuis l'interface Agents."
