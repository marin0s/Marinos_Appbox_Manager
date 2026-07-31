#!/usr/bin/env bash
set -Eeuo pipefail
TARGET="${APPBOX_TARGET:-/opt/appbox-manager-poc}"
DB="${TARGET}/data/appbox-manager.db"
HEALTH_URL="${APPBOX_HEALTH_URL:-http://127.0.0.1:8090/health}"
fail(){ echo "[ERREUR] $*" >&2; exit 1; }
curl -fsS "$HEALTH_URL" | grep -q '"version":"1.6.0-alpha.4"' || fail "Version healthcheck incorrecte"
docker inspect appbox-manager-cronos --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}' | grep -q -- '/srv/appbox-manager/reference-images -> /srv/appbox-manager/reference-images' || fail "Référentiel non monté persistamment"
sqlite3 "$DB" 'PRAGMA quick_check;' | grep -qx ok || fail "quick_check invalide"
[ -z "$(sqlite3 "$DB" 'PRAGMA foreign_key_check;')" ] || fail "foreign_key_check invalide"
for column in archive_path manifest_json source_report_json sanitization_report_json compressed_size_bytes; do
 sqlite3 "$DB" 'PRAGMA table_info(reference_image_versions);' | cut -d'|' -f2 | grep -qx "$column" || fail "Colonne absente: $column"
done
python3 - <<PY
import json, urllib.request
for path in ('/api/reference-images','/api/deployment-images/plex'):
 data=json.load(urllib.request.urlopen('$HEALTH_URL'.rsplit('/health',1)[0]+path, timeout=10))
 assert isinstance(data,dict), path
print('API Reference Images OK')
PY
echo "Vérifications V1.6.0-alpha.4 OK"
