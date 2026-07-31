#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/opt/appbox-manager-poc"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${TARGET_DIR}.backup-${STAMP}"
EXPECTED='VERSION = "1.2.1-sprint3-phase2-hotfix"'
[[ $EUID -eq 0 ]] || { echo "Exécuter en root." >&2; exit 1; }
[[ -d "$TARGET_DIR" && -f "$TARGET_DIR/docker-compose.yml" ]] || { echo "Installation CRONOS introuvable." >&2; exit 1; }
[[ -f "$SOURCE_DIR/app/main.py" && -f "$SOURCE_DIR/agent/marinos-appbox-agent.py" ]] || { echo "Archive source incomplète." >&2; exit 1; }
cp -a "$TARGET_DIR" "$BACKUP"
echo "Sauvegarde : $BACKUP"
# Ne jamais remplacer docker-compose.yml, .env, data/ ou les runtimes existants.
install -m 0644 "$SOURCE_DIR/app/main.py" "$TARGET_DIR/app/main.py"
install -m 0644 "$SOURCE_DIR/app/templates/detail.html" "$TARGET_DIR/app/templates/detail.html"
install -m 0644 "$SOURCE_DIR/app/static/app.js" "$TARGET_DIR/app/static/app.js"
install -m 0644 "$SOURCE_DIR/app/static/app-v0.9.7.js" "$TARGET_DIR/app/static/app-v0.9.7.js"
install -m 0755 "$SOURCE_DIR/agent/marinos-appbox-agent.py" "$TARGET_DIR/agent/marinos-appbox-agent.py"
install -m 0755 "$SOURCE_DIR/agent/install-agent.sh" "$TARGET_DIR/agent/install-agent.sh"
install -m 0644 "$SOURCE_DIR/agent/marinos-appbox-agent.service" "$TARGET_DIR/agent/marinos-appbox-agent.service"
install -m 0644 "$SOURCE_DIR/README-V1.2.1-SPRINT3-PHASE2-HOTFIX.md" "$TARGET_DIR/README-V1.2.1-SPRINT3-PHASE2-HOTFIX.md"
cd "$TARGET_DIR/agent"
rm -f appbox-agent-latest.zip
zip -q -j appbox-agent-latest.zip marinos-appbox-agent.py marinos-appbox-agent.service install-agent.sh
ACTUAL="$(unzip -p appbox-agent-latest.zip marinos-appbox-agent.py | grep '^VERSION' || true)"
[[ "$ACTUAL" == "$EXPECTED" ]] || { echo "Archive agent incohérente : $ACTUAL" >&2; exit 1; }
cd "$TARGET_DIR"
docker compose up -d --build
sleep 5
HEALTH="$(curl -fsS http://127.0.0.1:8090/health)"
grep -q '1.2.1-sprint3-phase2-hotfix' <<<"$HEALTH" || { echo "Version HTTP inattendue : $HEALTH" >&2; exit 1; }
[[ "$(docker inspect -f '{{.Name}}' appbox-manager-cronos 2>/dev/null || true)" == "/appbox-manager-cronos" ]] || { echo "Conteneur CRONOS absent." >&2; exit 1; }
if docker ps -a --format '{{.Names}}' | grep -qx 'appbox-manager-artemis'; then
  echo "ERREUR : conteneur parasite appbox-manager-artemis détecté." >&2
  exit 1
fi
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
curl -fsS http://127.0.0.1:8090/downloads/appbox-agent-latest.zip -o "$TMP"
REMOTE="$(unzip -p "$TMP" marinos-appbox-agent.py | grep '^VERSION' || true)"
[[ "$REMOTE" == "$EXPECTED" ]] || { echo "Archive distribuée incohérente : $REMOTE" >&2; exit 1; }
echo "Mise à jour 1.2.1 Sprint 3 Phase 2 Hotfix terminée."
