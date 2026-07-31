#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
BACKUP="/opt/appbox-manager-poc-backup-v0.3-step1-$(date +%F-%H%M%S)"
cd "$(dirname "$0")"
cp -a "$TARGET" "$BACKUP"
cp -a app "$TARGET/"
cp -a Dockerfile requirements.txt docker-compose.yml "$TARGET/"
cd "$TARGET"
docker compose up -d --build --force-recreate
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8090/health >/dev/null && break; sleep 1; done
curl -fsS http://127.0.0.1:8090/api/appboxes/ab35ah/status
echo
echo "Mise à jour terminée. Backup: $BACKUP"
