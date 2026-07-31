#!/usr/bin/env bash
set -Eeuo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/appbox-manager-poc"
BACKUP_DIR="/opt/appbox-manager-backups/v1.0.0-sprint1-$(date +%F-%H%M%S)"
[[ $EUID -eq 0 ]] || { echo "Lancez ce script en root."; exit 1; }
[[ -d "$APP_DIR" ]] || { echo "Installation absente : $APP_DIR"; exit 1; }
mkdir -p "$BACKUP_DIR"
cp -a "$APP_DIR"/. "$BACKUP_DIR"/
echo "Sauvegarde : $BACKUP_DIR"
cd "$APP_DIR"
docker compose down
cp -a "$SRC_DIR/app" "$APP_DIR/"
cp -a "$SRC_DIR/agent" "$APP_DIR/"
cp -a "$SRC_DIR/requirements.txt" "$SRC_DIR/Dockerfile" "$APP_DIR/"
docker compose build --no-cache
docker compose up -d
for i in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health.json; then break; fi
  sleep 2
done
python3 - <<'PY'
import json
p=json.load(open('/tmp/appbox-health.json'))
assert p['version']=='1.0.0-sprint1', p
assert p.get('distributed_runtime_inventory') is True, p
assert p.get('remote_runtime_source_of_truth') is True, p
print(json.dumps(p, indent=2, ensure_ascii=False))
PY
curl -fsS http://127.0.0.1:8090/downloads/install-agent.sh >/dev/null
wget -q http://127.0.0.1:8090/downloads/appbox-agent-latest.zip -O /tmp/appbox-agent-latest.zip
unzip -t /tmp/appbox-agent-latest.zip
printf '\nUpgrade V1.0 Sprint 1 terminé.\n'
printf 'Réinstallez ensuite les agents depuis Nodes > Installer l’agent.\n'
printf 'Rechargez l’interface avec Ctrl+F5.\n'
