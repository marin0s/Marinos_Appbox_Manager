#!/usr/bin/env bash
set -Eeuo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="/opt/appbox-manager-poc"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${TARGET}.backup-${STAMP}"
[ -d "$TARGET" ] || { echo "Installation absente : $TARGET" >&2; exit 1; }
cp -a "$TARGET" "$BACKUP"
echo "Sauvegarde : $BACKUP"
rsync -a --delete --exclude data --exclude generated --exclude docker-compose.yml --exclude .env --exclude control-plane-runtime "$SRC_DIR/" "$TARGET/"
cd "$TARGET"
python3 -m zipfile -c agent/appbox-agent-latest.zip agent/marinos-appbox-agent.py agent/marinos-appbox-agent.service agent/install-agent.sh
docker compose up -d --build --remove-orphans
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8090/health >/dev/null && break; sleep 1; done
curl -fsS http://127.0.0.1:8090/health | grep -q '"version":"1.4.0"' || { echo "Validation healthcheck échouée. Rollback : $BACKUP" >&2; exit 1; }
if docker ps -a --format '{{.Names}}' | grep -qx 'appbox-manager-artemis'; then echo "Conteneur parasite appbox-manager-artemis détecté" >&2; exit 1; fi
echo "Mise à jour 1.4.0 terminée. Réinstallez l’agent sur le node laboratoire avant les tests de suppression."
