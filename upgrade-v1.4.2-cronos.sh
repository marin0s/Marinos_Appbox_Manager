#!/usr/bin/env bash
set -Eeuo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${APPBOX_TARGET:-/opt/appbox-manager-poc}"
HEALTH_URL="${APPBOX_HEALTH_URL:-http://127.0.0.1:8090/health}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${TARGET}.backup-v1.4.2-${STAMP}"
DB="${TARGET}/data/appbox-manager.db"
SERVICE="appbox-manager"

fail() { echo "[ERREUR] $*" >&2; exit 1; }
[ -d "$TARGET" ] || fail "Installation absente : $TARGET"
command -v rsync >/dev/null || fail "rsync absent"
command -v docker >/dev/null || fail "docker absent"
command -v sqlite3 >/dev/null || fail "sqlite3 absent"
command -v curl >/dev/null || fail "curl absent"

docker compose version >/dev/null 2>&1 || fail "plugin Docker Compose absent"

if [ -f "$DB" ]; then
  sqlite3 "$DB" 'PRAGMA quick_check;' | grep -qx 'ok' || fail "La base SQLite actuelle ne passe pas quick_check"
  if sqlite3 "$DB" 'PRAGMA foreign_key_check;' | grep -q .; then
    fail "La base SQLite contient déjà des violations de clés étrangères"
  fi
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

echo "Construction de l'image applicative 1.4.2..."
docker compose build "$SERVICE"
IMAGE_ID="$(docker compose images -q "$SERVICE" | head -n1)"
[ -n "$IMAGE_ID" ] || fail "Impossible de déterminer l'image Docker construite"

echo "Exécution des tests dans l'image applicative (aucune dépendance Python requise sur CRONOS)..."
docker run --rm \
  -v "$TARGET:/src" \
  -w /src \
  "$IMAGE_ID" \
  python -m unittest discover -s tests -v

docker run --rm \
  -v "$TARGET:/src" \
  -w /src \
  "$IMAGE_ID" \
  python -m py_compile app/main.py agent/marinos-appbox-agent.py

docker run --rm \
  -v "$TARGET:/src" \
  -w /src \
  "$IMAGE_ID" \
  python -m zipfile -c agent/appbox-agent-latest.zip \
    agent/marinos-appbox-agent.py \
    agent/marinos-appbox-agent.service \
    agent/install-agent.sh

echo "Démarrage de la version 1.4.2..."
docker compose up -d --remove-orphans

healthy=0
for _ in $(seq 1 60); do
  if curl -fsS "$HEALTH_URL" 2>/dev/null | grep -q '"version":"1.4.2"'; then
    healthy=1
    break
  fi
  sleep 2
done

if [ "$healthy" -ne 1 ]; then
  echo "Healthcheck 1.4.2 échoué." >&2
  echo "Rollback disponible :" >&2
  echo "  cd /opt && mv '$TARGET' '${TARGET}.failed-${STAMP}' && cp -a '$BACKUP' '$TARGET' && cd '$TARGET' && docker compose up -d --build" >&2
  exit 1
fi

if [ -f "$DB" ]; then
  if sqlite3 "$DB" 'PRAGMA foreign_key_check;' | grep -q .; then
    fail "Violations de clés étrangères après mise à niveau"
  fi
  sqlite3 "$DB" 'PRAGMA quick_check;' | grep -qx 'ok' || fail "quick_check échoué après mise à niveau"
fi

echo "AppBox Manager 1.4.2 Deletion Integrity Hotfix déployé avec succès."
echo "Sauvegarde conservée : $BACKUP"
