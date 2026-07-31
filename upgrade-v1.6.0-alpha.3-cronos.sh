#!/usr/bin/env bash
set -Eeuo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${APPBOX_TARGET:-/opt/appbox-manager-poc}"
HEALTH_URL="${APPBOX_HEALTH_URL:-http://127.0.0.1:8090/health}"
REFERENCE_DIR="${APPBOX_REFERENCE_HOST_DIR:-/srv/appbox-manager/reference-images}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${TARGET}.backup-v1.6.0-alpha.3-${STAMP}"
DB="${TARGET}/data/appbox-manager.db"
SERVICE="appbox-manager"
fail(){ echo "[ERREUR] $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail "Exécuter en root"
for command in rsync docker sqlite3 curl; do command -v "$command" >/dev/null || fail "$command absent"; done
docker compose version >/dev/null 2>&1 || fail "plugin Docker Compose absent"
[ -d "$TARGET" ] || fail "Installation absente : $TARGET"
if [ -f "$DB" ]; then
 sqlite3 "$DB" 'PRAGMA quick_check;' | grep -qx ok || fail "quick_check invalide avant upgrade"
 [ -z "$(sqlite3 "$DB" 'PRAGMA foreign_key_check;')" ] || fail "Violations de clés étrangères avant upgrade"
fi
cp -a "$TARGET" "$BACKUP"
echo "$BACKUP" > /root/appbox-manager-v1.6.0-alpha.3-last-backup
rsync -a --delete --exclude data --exclude generated --exclude .env --exclude control-plane-runtime --exclude docker-compose.yml "$SRC_DIR/" "$TARGET/"
mkdir -p "$REFERENCE_DIR/builds" "$REFERENCE_DIR/deployment-cache"
chmod 750 "$REFERENCE_DIR" "$REFERENCE_DIR/builds" "$REFERENCE_DIR/deployment-cache"
# Preserve the local compose while guaranteeing persistent Reference Image storage.
if ! grep -q '/srv/appbox-manager/reference-images:/srv/appbox-manager/reference-images' "$TARGET/docker-compose.yml"; then
 sed -i '/control-plane-runtime:\/srv\/appboxes/a\      - /srv/appbox-manager/reference-images:/srv/appbox-manager/reference-images' "$TARGET/docker-compose.yml"
fi
cd "$TARGET"
python3 -m zipfile -c agent/appbox-agent-latest.zip agent/marinos-appbox-agent.py agent/marinos-appbox-agent.service agent/install-agent.sh
docker compose build "$SERVICE"
IMAGE_ID="$(docker compose images -q "$SERVICE" | head -n1)"
[ -n "$IMAGE_ID" ] || fail "Image Docker introuvable"
docker run --rm -v "$TARGET:/src" -w /src "$IMAGE_ID" python -m unittest discover -s tests -v
docker run --rm -v "$TARGET:/src" -w /src "$IMAGE_ID" python -m py_compile app/main.py agent/marinos-appbox-agent.py
docker compose up -d --remove-orphans
for _ in $(seq 1 60); do
 curl -fsS "$HEALTH_URL" 2>/dev/null | grep -q '"version":"1.6.0-alpha.3"' && healthy=1 && break
 sleep 2
done
[ "${healthy:-0}" -eq 1 ] || fail "Healthcheck V1.6 échoué. Rollback: $SRC_DIR/rollback-v1.6.0-alpha.3-cronos.sh"
"$SRC_DIR/verify-v1.6.0-alpha.3-cronos.sh"
echo "V1.6.0-alpha.3 installée. Backup : $BACKUP"
echo "IMPORTANT : mettre à jour l’agent du node source avant de lancer une nouvelle capture."
