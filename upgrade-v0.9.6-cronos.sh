#!/usr/bin/env bash
set -Eeuo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/appbox-manager-poc"
BACKUP_DIR="/opt/appbox-manager-backups/v0.9.6-$(date +%F-%H%M%S)"
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
for i in $(seq 1 45); do curl -fsS http://127.0.0.1:8090/health >/tmp/appbox-health.json && break; sleep 2; done
python3 -c 'import json; p=json.load(open("/tmp/appbox-health.json")); assert p["version"]=="0.9.6"; assert p.get("remote_deployment_executor") is True; print(json.dumps(p,indent=2,ensure_ascii=False))'
curl -fsS http://127.0.0.1:8090/downloads/install-agent.sh >/dev/null
wget -q http://127.0.0.1:8090/downloads/appbox-agent-latest.zip -O /tmp/appbox-agent-latest.zip
unzip -t /tmp/appbox-agent-latest.zip
echo "Upgrade V0.9.6 terminé. Rechargez l'interface avec Ctrl+F5."
