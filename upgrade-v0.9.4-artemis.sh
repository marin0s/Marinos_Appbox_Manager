#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.9.4-${STAMP}"
rollback() {
  echo "[ROLLBACK] Échec V0.9.4"
  docker logs appbox-manager-artemis --tail 200 || true
  rm -rf "${TARGET}.failed"
  mv "$TARGET" "${TARGET}.failed" 2>/dev/null || true
  cp -a "$BACKUP" "$TARGET"
  cd "$TARGET" && docker compose up -d --build --force-recreate
}
trap rollback ERR
echo "[1/7] Sauvegarde"; cp -a "$TARGET" "$BACKUP"
echo "[2/7] Arrêt"; cd "$TARGET"; docker compose stop appbox-manager || true
echo "[3/7] Installation"; rm -rf "$TARGET/app" "$TARGET/agent"; cp -a "$SOURCE/app" "$TARGET/app"; cp -a "$SOURCE/agent" "$TARGET/agent"; cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"; cp -a "$SOURCE/README-V0.9.4.md" "$TARGET/"
echo "[4/7] Validation"; python3 -m py_compile "$TARGET/app/main.py"; python3 -m py_compile "$TARGET/agent/marinos-appbox-agent.py"
echo "[5/7] Reconstruction"; cd "$TARGET"; docker compose build --no-cache appbox-manager; docker compose up -d --force-recreate appbox-manager
echo "[6/7] Healthcheck"; for i in $(seq 1 90); do curl -fsS http://127.0.0.1:8090/health >/tmp/v094-health.json 2>/dev/null && break; sleep 1; done; grep -q '"version":"0.9.4"' /tmp/v094-health.json; grep -q '"remote_node_detail":true' /tmp/v094-health.json
echo "[7/7] Contrôles"; curl -fsS http://127.0.0.1:8090/settings | grep -q 'Politique de placement'; test "$(curl -fsS http://127.0.0.1:8090/settings | grep -o 'Enregistrer la politique' | wc -l)" -eq 1; curl -fsS http://127.0.0.1:8090/nodes/demeter >/dev/null; curl -fsS http://127.0.0.1:8090/agents >/dev/null
trap - ERR
echo "V0.9.4 installée. Backup : $BACKUP"
