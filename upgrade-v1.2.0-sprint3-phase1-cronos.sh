#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="/opt/appbox-manager-poc"
BACKUP_DIR="/opt/appbox-manager-poc.backup-$(date +%Y%m%d-%H%M%S)"

[[ $EUID -eq 0 ]] || { echo "Exécuter en root." >&2; exit 1; }
[[ -d "$TARGET_DIR" ]] || { echo "$TARGET_DIR introuvable." >&2; exit 1; }

cp -a "$TARGET_DIR" "$BACKUP_DIR"
echo "Sauvegarde : $BACKUP_DIR"

install -m 0644 "$SOURCE_DIR/app/main.py" "$TARGET_DIR/app/main.py"
install -m 0755 "$SOURCE_DIR/agent/marinos-appbox-agent.py" "$TARGET_DIR/agent/marinos-appbox-agent.py"
install -m 0755 "$SOURCE_DIR/agent/install-agent.sh" "$TARGET_DIR/agent/install-agent.sh"
install -m 0644 "$SOURCE_DIR/agent/marinos-appbox-agent.service" "$TARGET_DIR/agent/marinos-appbox-agent.service"
install -m 0644 "$SOURCE_DIR/README-V1.2.0-SPRINT3-PHASE1.md" "$TARGET_DIR/README-V1.2.0-SPRINT3-PHASE1.md"

cd "$TARGET_DIR"
docker compose up -d --build
sleep 5
curl -fsS http://127.0.0.1:8090/api/health || curl -fsS http://127.0.0.1:8090/health
printf '
Mise à jour 1.2.0 Sprint 3 Phase 1 terminée.
'
