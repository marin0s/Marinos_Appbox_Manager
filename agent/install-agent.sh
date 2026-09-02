#!/usr/bin/env bash
set -Eeuo pipefail

NODE_ID="${1:-}"
CONTROL_PLANE_URL="${2:-}"
TOKEN="${3:-}"

if [[ -z "$NODE_ID" || -z "$CONTROL_PLANE_URL" || -z "$TOKEN" ]]; then
  echo "Usage: $0 <node-id> <control-plane-url> <token>"
  echo "Exemple: $0 demeter http://10.55.1.81:8090 TOKEN"
  exit 1
fi

[[ "${EUID}" -eq 0 ]] || { echo "Exécuter en root ou avec sudo." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 est requis." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd est requis." >&2; exit 1; }

SOURCE="$(cd "$(dirname "$0")" && pwd)"
install -d -m 700 /etc/marinos-appbox-agent
install -d -m 755 /var/lib/marinos-appbox-agent
install -d -m 755 /srv/appboxes
install -m 755 "$SOURCE/marinos-appbox-agent.py" /usr/local/sbin/marinos-appbox-agent.py
install -m 644 "$SOURCE/reference_contract.py" /usr/local/sbin/reference_contract.py
install -m 644 "$SOURCE/rdad_refresh.py" /usr/local/sbin/rdad_refresh.py
install -m 644 "$SOURCE/upgrade_contract.py" /usr/local/sbin/upgrade_contract.py
install -m 644 "$SOURCE/upgrade_client.py" /usr/local/sbin/upgrade_client.py
install -m 644 "$SOURCE/marinos-appbox-agent.service" /etc/systemd/system/marinos-appbox-agent.service

python3 - "$NODE_ID" "$CONTROL_PLANE_URL" "$TOKEN" <<'PY'
import json, pathlib, sys
node_id, url, token = sys.argv[1:]
config = {
    "node_id": node_id,
    "agent_id": f"agent-{node_id}",
    "control_plane_url": url.rstrip("/"),
    "token": token,
    "heartbeat_interval": 60,
    "inventory_interval": 30,
    "command_poll_interval": 2,
    "rdad_path": "/mnt/decypharr-poc/.mnt",
    "rdad_refresh_enabled": "auto",
    "rdad_refresh_interval": 60,
    "rdad_refresh_catalog_interval": 300,
    "rdad_refresh_mode": "readable",
    "disk_path": "/",
    "appbox_base_dir": "/srv/appboxes",
}
path = pathlib.Path("/etc/marinos-appbox-agent/agent.json")
path.write_text(json.dumps(config, indent=2) + "\n")
path.chmod(0o600)
PY

rm -f /etc/systemd/system/marinos-appbox-agent.service.d/10-appboxes-write.conf
systemctl daemon-reload
systemctl enable marinos-appbox-agent.service
systemctl restart marinos-appbox-agent.service
sleep 3
systemctl is-active --quiet marinos-appbox-agent.service || {
  journalctl -u marinos-appbox-agent.service --since "5 minutes ago" --no-pager >&2 || true
  exit 1
}
systemctl status marinos-appbox-agent.service --no-pager -l
