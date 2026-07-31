#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.9.5-${STAMP}"
HEALTH_FILE="/tmp/appbox-v095-health.json"

log(){ printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail(){ echo "ERREUR: $*" >&2; exit 1; }

rollback(){
  echo "[ROLLBACK] Échec de l'upgrade V0.9.5" >&2
  cd "$TARGET" 2>/dev/null && docker compose logs --tail 200 appbox-manager || true
  rm -rf "${TARGET}.failed"
  mv "$TARGET" "${TARGET}.failed" 2>/dev/null || true
  cp -a "$BACKUP" "$TARGET"
  cd "$TARGET"
  docker compose up -d --build --force-recreate appbox-manager
}
trap rollback ERR

[[ $EUID -eq 0 ]] || fail "Exécuter ce script en root."
[[ -d "$TARGET" ]] || fail "Installation absente: $TARGET"
[[ -f "$TARGET/docker-compose.yml" ]] || fail "docker-compose.yml absent."
[[ -f "$SOURCE/app/main.py" ]] || fail "Paquet V0.9.5 incomplet."
[[ -f "$SOURCE/agent/appbox-agent-latest.zip" ]] || fail "Archive agent absente."

log "1/8 - Sauvegarde intégrale"
cp -a "$TARGET" "$BACKUP"

log "2/8 - Arrêt du Control Plane"
cd "$TARGET"
docker compose stop appbox-manager || true

log "3/8 - Installation des fichiers V0.9.5"
rm -rf "$TARGET/app" "$TARGET/agent"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/agent" "$TARGET/agent"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$TARGET/"
cp -a "$SOURCE/README-V0.9.5.md" "$TARGET/"
# Le compose CRONOS existant est volontairement conservé.

log "4/8 - Validation statique"
python3 -m py_compile "$TARGET/app/main.py"
python3 -m py_compile "$TARGET/agent/marinos-appbox-agent.py"
bash -n "$TARGET/agent/install-agent.sh"
bash -n "$TARGET/agent/bootstrap-install-agent.sh"
unzip -t "$TARGET/agent/appbox-agent-latest.zip" >/dev/null

log "5/8 - Reconstruction"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

log "6/8 - Attente du healthcheck"
rm -f "$HEALTH_FILE"
for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8090/health >"$HEALTH_FILE" 2>/dev/null; then
    break
  fi
  sleep 1
done
[[ -s "$HEALTH_FILE" ]] || fail "Healthcheck indisponible."
grep -q '"version":"0.9.5"' "$HEALTH_FILE"
grep -q '"agent_self_service_installer":true' "$HEALTH_FILE"
grep -q '"agent_download_archive":true' "$HEALTH_FILE"

log "7/8 - Contrôle des artefacts HTTP"
curl -fsS http://127.0.0.1:8090/downloads/install-agent.sh -o /tmp/appbox-install-agent.sh
bash -n /tmp/appbox-install-agent.sh
curl -fsS http://127.0.0.1:8090/downloads/appbox-agent-latest.zip -o /tmp/appbox-agent-latest.zip
unzip -t /tmp/appbox-agent-latest.zip >/dev/null

log "8/8 - Contrôle UI et base"
curl -fsS http://127.0.0.1:8090/agents | grep -q "Télécharger le ZIP"
curl -fsS http://127.0.0.1:8090/nodes >/dev/null
python3 - <<PY
import sqlite3
con=sqlite3.connect("$TARGET/data/appbox-manager.db")
assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
print("SQLite: OK")
PY

trap - ERR
printf '\nV0.9.5 installée sur CRONOS.\nBackup : %s\n' "$BACKUP"
