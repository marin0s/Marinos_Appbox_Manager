#!/usr/bin/env bash
set -Eeuo pipefail
TARGET=/opt/appbox-manager-poc
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.5.3.1-${STAMP}"
echo "[1/8] Sauvegarde"; cp -a "$TARGET" "$BACKUP"
echo "[2/8] Arrêt"; cd "$TARGET"; docker compose stop appbox-manager || true
echo "[3/8] Installation"; rm -rf "$TARGET/app"; cp -a "$SOURCE/app" "$TARGET/app"; cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
echo "[4/8] Validation"; python3 -m py_compile "$TARGET/app/main.py"
echo "[5/8] Reconstruction sans cache"; cd "$TARGET"; docker compose build --no-cache appbox-manager; docker compose up -d --force-recreate appbox-manager
echo "[6/8] Attente"; for i in $(seq 1 60); do curl -fsS http://127.0.0.1:8090/health >/tmp/health-v0531.json 2>/dev/null && break; sleep 1; done
echo "[7/8] Contrôles"; python3 -m json.tool /tmp/health-v0531.json; grep -q 'VERSION = "0.5.3.1"' "$TARGET/app/main.py"; curl -fsS http://127.0.0.1:8090/appboxes | grep -q 'name="media_type"'
echo "[8/8] Test fonctionnel du formulaire"; docker exec -i appbox-manager-artemis python - <<'PY2'
from app.main import compose_for
j=compose_for('abtest', 'jellyfin', 8100, None)
assert 'jellyfin-abtest' in j and '32400' not in j and '8096' in j
print('Générateur Jellyfin : OK')
PY2
echo "V0.5.3.1 installée. Backup : $BACKUP"
