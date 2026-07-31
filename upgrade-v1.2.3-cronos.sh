#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/opt/appbox-manager-poc"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${TARGET_DIR}.backup-${STAMP}"
EXPECTED_VERSION="1.2.3"
[[ $EUID -eq 0 ]] || { echo "Exécuter en root." >&2; exit 1; }
[[ -d "$TARGET_DIR" && -f "$TARGET_DIR/docker-compose.yml" ]] || { echo "Installation CRONOS introuvable." >&2; exit 1; }
[[ -f "$SOURCE_DIR/app/main.py" && -f "$SOURCE_DIR/app/static/app-v0.9.7.js" ]] || { echo "Archive source incomplète." >&2; exit 1; }
grep -q "VERSION = \"${EXPECTED_VERSION}\"" "$SOURCE_DIR/app/main.py" || { echo "Version source inattendue." >&2; exit 1; }
grep -q "delete-confirm-modal" "$SOURCE_DIR/app/templates/base.html" || { echo "Correctif modal absent." >&2; exit 1; }
grep -q "'deploy','start','stop','restart','recreate'" "$SOURCE_DIR/app/static/app-v0.9.7.js" || { echo "Correctif refresh Deploy absent." >&2; exit 1; }
cp -a "$TARGET_DIR" "$BACKUP"
echo "Sauvegarde : $BACKUP"
install -m 0644 "$SOURCE_DIR/app/main.py" "$TARGET_DIR/app/main.py"
install -m 0644 "$SOURCE_DIR/app/templates/base.html" "$TARGET_DIR/app/templates/base.html"
install -m 0644 "$SOURCE_DIR/app/templates/detail.html" "$TARGET_DIR/app/templates/detail.html"
install -m 0644 "$SOURCE_DIR/app/static/app.css" "$TARGET_DIR/app/static/app.css"
install -m 0644 "$SOURCE_DIR/app/static/app.js" "$TARGET_DIR/app/static/app.js"
install -m 0644 "$SOURCE_DIR/app/static/app-v0.9.7.js" "$TARGET_DIR/app/static/app-v0.9.7.js"
install -m 0644 "$SOURCE_DIR/README-V1.2.3-SPRINT3-FINAL-POLISH.md" "$TARGET_DIR/README-V1.2.3-SPRINT3-FINAL-POLISH.md"
install -m 0644 "$SOURCE_DIR/CHANGELOG.md" "$TARGET_DIR/CHANGELOG.md"
install -m 0644 "$SOURCE_DIR/MODIFICATION-HISTORY.md" "$TARGET_DIR/MODIFICATION-HISTORY.md"
cd "$TARGET_DIR"
docker compose up -d --build
sleep 5
HEALTH="$(curl -fsS http://127.0.0.1:8090/health)"
grep -q '"version":"1.2.3"' <<<"$HEALTH" || { echo "Version HTTP inattendue : $HEALTH" >&2; exit 1; }
[[ "$(docker inspect -f '{{.Name}}' appbox-manager-cronos 2>/dev/null || true)" == "/appbox-manager-cronos" ]] || { echo "Conteneur CRONOS absent." >&2; exit 1; }
if docker ps -a --format '{{.Names}}' | grep -qx 'appbox-manager-artemis'; then echo "ERREUR : conteneur parasite appbox-manager-artemis détecté." >&2; exit 1; fi
echo "Mise à jour ${EXPECTED_VERSION} terminée. Agent de node inchangé."
