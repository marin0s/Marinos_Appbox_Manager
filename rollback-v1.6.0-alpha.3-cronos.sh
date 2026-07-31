#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="${APPBOX_TARGET:-/opt/appbox-manager-poc}"
MARKER="/root/appbox-manager-v1.6.0-alpha.3-last-backup"
[ "$(id -u)" -eq 0 ] || { echo "Exécuter en root" >&2; exit 1; }
[ -s "$MARKER" ] || { echo "Backup V1.6 introuvable" >&2; exit 1; }
BACKUP="$(cat "$MARKER")"
[ -d "$BACKUP" ] || { echo "Répertoire backup absent: $BACKUP" >&2; exit 1; }
STAMP="$(date +%Y%m%d-%H%M%S)"
cd "$TARGET" && docker compose down || true
mv "$TARGET" "${TARGET}.failed-v1.6-${STAMP}"
cp -a "$BACKUP" "$TARGET"
cd "$TARGET"
docker compose up -d --build
curl -fsS http://127.0.0.1:8090/health
echo "Rollback terminé depuis $BACKUP"
