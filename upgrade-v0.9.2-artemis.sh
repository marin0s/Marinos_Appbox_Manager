#!/usr/bin/env bash
set -Eeuo pipefail

TARGET="/opt/appbox-manager-poc"
SOURCE="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%F-%H%M%S)"
BACKUP="/opt/appbox-manager-poc-backup-v0.9.2-${STAMP}"
HEALTH="/tmp/appbox-health-v092.json"

rollback() {
  echo
  echo "[ROLLBACK] Échec V0.9.2"
  docker logs appbox-manager-artemis --tail 200 || true
  rm -rf "${TARGET}.failed"
  mv "$TARGET" "${TARGET}.failed" 2>/dev/null || true
  cp -a "$BACKUP" "$TARGET"
  cd "$TARGET"
  docker compose up -d --build --force-recreate
  echo "Version précédente restaurée : $BACKUP"
}
trap rollback ERR

echo "[1/9] Sauvegarde"
cp -a "$TARGET" "$BACKUP"

echo "[2/9] Sauvegarde SQLite"
mkdir -p "$BACKUP/database"
cp -a "$TARGET/data/appbox-manager.db"* "$BACKUP/database/" 2>/dev/null || true

echo "[3/9] Arrêt"
cd "$TARGET"
docker compose stop appbox-manager || true

echo "[4/9] Installation V0.9.2"
rm -rf "$TARGET/app" "$TARGET/agent"
cp -a "$SOURCE/app" "$TARGET/app"
cp -a "$SOURCE/agent" "$TARGET/agent"
cp -a "$SOURCE/Dockerfile" "$SOURCE/requirements.txt" "$SOURCE/docker-compose.yml" "$TARGET/"
cp -a "$SOURCE/README-V0.9.2.md" "$TARGET/"

echo "[5/9] Validations statiques"
python3 -m py_compile "$TARGET/app/main.py"
python3 -m py_compile "$TARGET/agent/marinos-appbox-agent.py"
bash -n "$TARGET/agent/install-agent.sh"

echo "[6/9] Reconstruction"
cd "$TARGET"
docker compose build --no-cache appbox-manager
docker compose up -d --force-recreate appbox-manager

echo "[7/9] Healthcheck"
rm -f "$HEALTH"
for i in $(seq 1 90); do
  curl -fsS http://127.0.0.1:8090/health >"$HEALTH" 2>/dev/null && break
  sleep 1
done
test -s "$HEALTH"
python3 -m json.tool "$HEALTH"
grep -q '"version":"0.9.2"' "$HEALTH"
grep -q '"agent_metrics_isolated":true' "$HEALTH"

echo "[8/9] Contrôles des pages et du bouton"
curl -fsS http://127.0.0.1:8090/nodes >/tmp/appbox-nodes-v092.html
curl -fsS http://127.0.0.1:8090/agents >/tmp/appbox-agents-v092.html
grep -q 'Modifier' /tmp/appbox-nodes-v092.html
grep -q 'Générer le jeton d’installation' /tmp/appbox-agents-v092.html
grep -q 'app-v0.9.2.js' <(curl -fsS http://127.0.0.1:8090/)

echo "[9/9] Contrôles SQLite"
docker exec -i appbox-manager-artemis python - <<'PY'
import sqlite3
con=sqlite3.connect("/data/appbox-manager.db")
con.row_factory=sqlite3.Row
tables={r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
)}
required={
    "agent_enrollment_tokens",
    "agent_commands",
    "agent_node_metrics",
}
missing=required-tables
assert not missing, f"Tables absentes : {sorted(missing)}"
assert "node_metrics" in tables, "La table historique node_metrics doit être conservée"
print("Tables agent OK :", sorted(required))
print("Table historique node_metrics conservée")
print("Nodes enregistrés :")
for row in con.execute("""
    SELECT n.node_id,n.name,n.status,
           group_concat(t.name, ', ') AS tags
    FROM nodes n
    LEFT JOIN node_tag_assignments a ON a.node_id=n.node_id
    LEFT JOIN node_tags t ON t.tag_id=a.tag_id
    GROUP BY n.node_id,n.name,n.status
    ORDER BY n.name
"""):
    print(dict(row))
PY

trap - ERR
docker logs appbox-manager-artemis --tail 60
echo
echo "V0.9.2 installée. Backup : $BACKUP"
