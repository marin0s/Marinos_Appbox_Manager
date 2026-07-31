#!/usr/bin/env bash
set -Eeuo pipefail

NODE_ID="${1:-}"
CONTROL_PLANE_URL="${2:-}"
TOKEN="${3:-}"

fail() { printf 'ERREUR: %s\n' "$*" >&2; exit 1; }
log() { printf '[AppBox Agent] %s\n' "$*"; }

[[ "${EUID}" -eq 0 ]] || fail "L'installation doit être lancée avec sudo ou en root."
[[ -n "$NODE_ID" && -n "$CONTROL_PLANE_URL" && -n "$TOKEN" ]] || \
  fail "Paramètres manquants: node_id, URL du Control Plane et jeton."
[[ "$NODE_ID" =~ ^[a-z0-9][a-z0-9-]{1,62}$ ]] || fail "Identifiant de node invalide."

CONTROL_PLANE_URL="${CONTROL_PLANE_URL%/}"
ARCHIVE_URL="${CONTROL_PLANE_URL}/downloads/appbox-agent-latest.zip"
WORKDIR="$(mktemp -d /tmp/appbox-agent-install.XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

for command in curl unzip python3 systemctl; do
  command -v "$command" >/dev/null 2>&1 || fail "Commande requise absente: $command"
done

log "Téléchargement de l'agent depuis ${CONTROL_PLANE_URL}"
curl --fail --silent --show-error --location \
  --connect-timeout 10 --max-time 120 \
  "$ARCHIVE_URL" -o "$WORKDIR/agent.zip"

[[ -s "$WORKDIR/agent.zip" ]] || fail "Archive vide."
unzip -q "$WORKDIR/agent.zip" -d "$WORKDIR/agent"

INSTALLER="$WORKDIR/agent/install-agent.sh"
[[ -x "$INSTALLER" ]] || chmod 755 "$INSTALLER" 2>/dev/null || true
[[ -f "$INSTALLER" ]] || fail "install-agent.sh absent de l'archive."
[[ -f "$WORKDIR/agent/marinos-appbox-agent.py" ]] || fail "Agent Python absent de l'archive."
[[ -f "$WORKDIR/agent/marinos-appbox-agent.service" ]] || fail "Service systemd absent de l'archive."

log "Installation pour le node ${NODE_ID}"
"$INSTALLER" "$NODE_ID" "$CONTROL_PLANE_URL" "$TOKEN"

log "Installation terminée."
