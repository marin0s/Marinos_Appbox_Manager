"""Control Plane upgrade orchestration; no remote shell or operator-supplied URL."""
import json
import os
import time
import uuid
import zipfile
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from agent.upgrade_contract import (BRIDGE_FILES, FILES, MAX_PACKAGE_BYTES, PHASES, TERMINAL, digest,
    fsync_directory, package_bytes, update_status, validate_package)


def host():
    from app import main
    return main


def init_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS agent_upgrade_runtime (
          node_id TEXT PRIMARY KEY REFERENCES nodes(node_id) ON DELETE CASCADE,
          version TEXT NOT NULL, build_id TEXT, package_sha256 TEXT, process_id TEXT,
          pid INTEGER, received_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_upgrades (
          operation_id TEXT PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
          phase TEXT NOT NULL, version TEXT NOT NULL, build_id TEXT NOT NULL,
          package_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
          before_process TEXT, deadline_epoch REAL NOT NULL, error_code TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS agent_upgrades_node ON agent_upgrades(node_id, created_at);
    """)
    columns = {row[1] for row in con.execute("PRAGMA table_info(agent_upgrades)")}
    additions = {
        "artifact_kind": "TEXT NOT NULL DEFAULT 'full'",
        "parent_operation_id": "TEXT",
        "followup_version": "TEXT",
        "followup_build_id": "TEXT",
        "followup_package_sha256": "TEXT",
        "followup_size_bytes": "INTEGER",
    }
    for name, definition in additions.items():
        if name not in columns:
            con.execute(f"ALTER TABLE agent_upgrades ADD COLUMN {name} {definition}")


def active(con, node_id):
    # Safe only before prepared: activation requires an authoritative prepared
    # operation. Expiry and the prepared transition serialize in SQLite.
    expired = [r["operation_id"] for r in con.execute("""SELECT operation_id FROM agent_upgrades
        WHERE node_id=? AND phase IN ('queued','downloading','verifying') AND deadline_epoch<?""", (node_id,time.time()))]
    for operation_id in expired:
        changed = con.execute("""UPDATE agent_upgrades SET phase='upgrade_failed',error_code='preparation_timeout',updated_at=?
            WHERE operation_id=? AND phase IN ('queued','downloading','verifying') AND deadline_epoch<?""",
                    (host().now_iso(),operation_id,time.time())).rowcount
        if changed:
            con.execute("UPDATE agent_commands SET status='failed',completed_at=? WHERE command_id=?",(host().now_iso(),operation_id))
    return con.execute("""SELECT * FROM agent_upgrades WHERE node_id=?
        AND phase NOT IN ('success','upgrade_failed','rolled_back','rollback_failed')
        ORDER BY created_at DESC LIMIT 1""", (node_id,)).fetchone()


def require_idle(con, node_id):
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    if active(con, node_id):
        raise HTTPException(409, "Mise à jour agent en cours : nouvelle commande interdite.")


def official_artifact(pin=False, kind="full"):
    main = host()
    source = main.AGENT_ASSET_DIR
    if kind not in {"full", "bridge"}:
        raise HTTPException(503, "Type de package agent invalide.")
    path = source / ("appbox-agent-latest.zip" if kind == "full" else "appbox-agent-bridge.zip")
    expected_files = FILES if kind == "full" else BRIDGE_FILES
    try:
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("Oversize package")
        data = path.read_bytes()
        if data != package_bytes(source, expected_files):
            raise ValueError("Package differs from shipped sources")
        sha = digest(data)
        manifest, _ = validate_package(data, sha)
        if pin:
            path = main.DATA_DIR / "agent-upgrades" / "artifacts" / (sha + ".zip")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.is_symlink() or digest(path.read_bytes()) != sha:
                    raise ValueError("Immutable artifact damaged")
            else:
                temporary = path.with_suffix("." + uuid.uuid4().hex + ".partial")
                try:
                    with temporary.open("xb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                    fsync_directory(path.parent)
                finally:
                    temporary.unlink(missing_ok=True)
        return {"version":manifest["version"], "build_id":manifest["build_id"],
                "sha256":sha, "size_bytes":len(data), "path":path, "kind":kind}
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, SyntaxError):
        raise HTTPException(503, "Package agent absent, incohérent ou invalide.") from None


def decorate_nodes(nodes):
    main = host()
    try:
        artifact = official_artifact()
    except HTTPException:
        artifact = None
    with main.db_lock, main.db() as con:
        for node in nodes:
            active(con, node["node_id"])
        runtimes = {r["node_id"]:dict(r) for r in con.execute("SELECT * FROM agent_upgrade_runtime")}
        operations = {}
        for row in con.execute("SELECT * FROM agent_upgrades ORDER BY created_at, rowid"):
            operations[row["node_id"]] = dict(row)
    for node in nodes:
        runtime = runtimes.get(node["node_id"], {})
        op = operations.get(node["node_id"])
        installed = runtime.get("version") or node.get("registered_agent_version")
        state = update_status(installed, artifact["version"], runtime.get("build_id"), artifact["build_id"]) if artifact else "unknown"
        pending = bool(op and op["phase"] not in TERMINAL)
        if pending:
            state = "upgrading" if time.time() <= op["deadline_epoch"] else "upgrade_failed"
        elif op and op["phase"] in {"upgrade_failed", "rolled_back", "rollback_failed"}:
            state = "upgrade_failed"
        node["upgrade_in_progress"] = pending
        node["upgrade"] = {"status":state, "installed_version":installed, "installed_build_id":runtime.get("build_id"),
            "available_version":artifact["version"] if artifact else None,
            "available_sha256":artifact["sha256"] if artifact else None,
            "available_size_bytes":artifact["size_bytes"] if artifact else None,
            "phase":op["phase"] if op else None, "operation_id":op["operation_id"] if op else None,
            "error_code":("supervisor_confirmation_overdue" if pending and state == "upgrade_failed" else op.get("error_code")) if op else None,
            "restart_expected":bool(pending and time.time() <= op["deadline_epoch"] and op["phase"] in {"restarting","awaiting_heartbeat"}),
            "bootstrap_required":not node.get("capabilities", {}).get("remote_upgrade"),
            "can_upgrade":bool(artifact and not pending and not node["is_local"] and node["agent_online"]
                and node["status"] == "online" and node.get("capabilities", {}).get("remote_upgrade")
                and update_status(installed, artifact["version"], runtime.get("build_id"), artifact["build_id"]) == "update_available")}
    return nodes


def start(node_id, bootstrap=False, expected_sha=None):
    main = host()
    full_artifact = official_artifact(pin=True)
    if (bootstrap or expected_sha is not None) and expected_sha != full_artifact["sha256"]:
        raise HTTPException(409, "Le bootstrap ne correspond pas au package officiel.")
    node = next((n for n in main.list_control_nodes() if n["node_id"] == node_id), None)
    if not node or not node["agent_online"] or node["status"] != "online" or node["is_local"] or node_id.lower() == "cronos":
        raise HTTPException(409, "Agent indisponible, en maintenance ou Control Plane.")
    if not bootstrap and not node.get("capabilities", {}).get("remote_upgrade"):
        raise HTTPException(409, "Bootstrap opérateur requis : cet agent ne sait pas se mettre à jour.")
    if update_status(node["upgrade"]["installed_version"], full_artifact["version"]) == "unknown":
        raise HTTPException(409, "Version installée inconnue : vérification opérateur requise.")
    if update_status(node["upgrade"]["installed_version"], full_artifact["version"],
                     node["upgrade"].get("installed_build_id"), full_artifact["build_id"]) == "up_to_date":
        raise HTTPException(409, "Agent déjà à jour ; aucun downgrade automatique.")
    needs_bridge = not bootstrap and not node.get("capabilities", {}).get("upgrade_manifest_files")
    artifact = official_artifact(pin=True, kind="bridge") if needs_bridge else full_artifact
    operation_id = str(uuid.uuid4())
    stamp = main.now_iso()
    with main.db_lock, main.db() as con:
        con.execute("BEGIN IMMEDIATE")
        if active(con, node_id) or con.execute("SELECT 1 FROM agent_commands WHERE node_id=? AND status IN ('queued','offered','claimed')", (node_id,)).fetchone() or con.execute("SELECT 1 FROM jobs WHERE node_id=? AND status IN ('queued','running')", (node_id,)).fetchone():
            raise HTTPException(409, "Une opération incompatible est en cours ou en attente.")
        runtime = con.execute("SELECT process_id FROM agent_upgrade_runtime WHERE node_id=?", (node_id,)).fetchone()
        con.execute("""INSERT INTO agent_upgrades(operation_id,node_id,phase,version,build_id,package_sha256,
                    size_bytes,before_process,deadline_epoch,created_at,updated_at,artifact_kind,
                    followup_version,followup_build_id,followup_package_sha256,followup_size_bytes)
                    VALUES(?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (operation_id,node_id,artifact["version"],artifact["build_id"],artifact["sha256"],
                     artifact["size_bytes"],runtime["process_id"] if runtime else None,time.time()+900,stamp,stamp,
                     artifact["kind"],full_artifact["version"] if needs_bridge else None,
                     full_artifact["build_id"] if needs_bridge else None,
                     full_artifact["sha256"] if needs_bridge else None,
                     full_artifact["size_bytes"] if needs_bridge else None))
        if not bootstrap:
            con.execute("""INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at)
                         VALUES(?,?,'agent_upgrade',?,'queued',?)""",
                         (operation_id,node_id,json.dumps({"operation_id":operation_id}),stamp))
    main.record_event(None, "agent_upgrade_requested", f"Upgrade manuel {operation_id} sur {node_id}.")
    return operation(node_id, operation_id)


def operation(node_id, operation_id):
    main = host()
    with main.db() as con:
        row = con.execute("SELECT * FROM agent_upgrades WHERE operation_id=? AND node_id=?", (operation_id,node_id)).fetchone()
        runtime = con.execute("SELECT * FROM agent_upgrade_runtime WHERE node_id=?", (node_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Upgrade introuvable pour ce node.")
    return {"operation":dict(row), "runtime":dict(runtime) if runtime else None}


def observe_heartbeat(con, node_id, payload, stamp):
    runtime = payload.get("runtime") or {}
    if not isinstance(runtime, dict):
        runtime = {}
    # Only bounded, known fields; never persist arbitrary agent result payloads.
    import re
    def safe_id(key):
        value = runtime.get(key)
        return value if isinstance(value,str) and re.fullmatch(r"[a-zA-Z0-9-]{1,80}",value) else None
    pid = runtime.get("pid")
    con.execute("""INSERT INTO agent_upgrade_runtime(node_id,version,build_id,package_sha256,process_id,pid,received_at)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(node_id) DO UPDATE SET version=excluded.version,
                   build_id=excluded.build_id,package_sha256=excluded.package_sha256,process_id=excluded.process_id,
                   pid=excluded.pid,received_at=excluded.received_at""",
                (node_id,str(payload.get("agent_version") or "unknown")[:80],safe_id("build_id"),
                 safe_id("package_sha256"),safe_id("process_id"),pid if type(pid) is int and 0 < pid < 2**31 else None,stamp))


def event(node_id, operation_id, payload):
    main = host()
    phase = payload.get("phase")
    if phase not in PHASES:
        raise HTTPException(400, "Phase inconnue.")
    allowed = {"queued":{"downloading","upgrade_failed"}, "downloading":{"verifying","upgrade_failed"},
        "verifying":{"prepared","upgrade_failed"}, "prepared":{"installing","upgrade_failed"},
        "installing":{"restarting","rolling_back","upgrade_failed"},
        "restarting":{"awaiting_heartbeat","rolling_back"},
        "awaiting_heartbeat":{"success","rolling_back"}, "rolling_back":{"rolled_back","rollback_failed"}}
    with main.db_lock, main.db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM agent_upgrades WHERE operation_id=? AND node_id=?", (operation_id,node_id)).fetchone()
        if not row:
            raise HTTPException(404, "Upgrade introuvable pour ce node.")
        if phase == row["phase"] and phase in TERMINAL:
            return {"status":"ok"}  # retry after a lost terminal acknowledgement
        if phase != row["phase"] and phase not in allowed.get(row["phase"],set()):
            raise HTTPException(409, "Transition upgrade interdite.")
        if phase == "success":
            runtime = con.execute("SELECT * FROM agent_upgrade_runtime WHERE node_id=?", (node_id,)).fetchone()
            proof = payload.get("runtime") or {}
            if (not runtime or runtime["version"] != row["version"] or runtime["build_id"] != row["build_id"]
                    or runtime["package_sha256"] != row["package_sha256"]
                    or runtime["received_at"] <= row["created_at"]
                    or not runtime["process_id"] or runtime["process_id"] == row["before_process"]
                    or proof.get("process_id") != runtime["process_id"] or proof.get("pid") != runtime["pid"]):
                raise HTTPException(409, "Nouveau heartbeat non confirmé.")
        error = payload.get("error_code")
        codes = {"preparation_failed","package_download_failed","package_too_large","package_sha256_mismatch",
                 "manifest_invalid","package_file_set_invalid","package_file_checksum_mismatch",
                 "protocol_incompatible","launcher_abi_incompatible","package_path_unsafe",
                 "package_preparation_failed","candidate_rejected","installation_failed","confirmation_timeout",
                 "activation_or_confirmation_failed","previous_agent_return_unconfirmed","rollback_unconfirmed",
                 "controller_failed","candidate_controller_failed"}
        error = error if error in codes else None
        con.execute("UPDATE agent_upgrades SET phase=?,error_code=COALESCE(?,error_code),updated_at=? WHERE operation_id=?",
                    (phase,error,main.now_iso(),operation_id))
        if phase in TERMINAL:
            con.execute("UPDATE agent_commands SET status=?,completed_at=? WHERE command_id=? AND node_id=?",
                        ("success" if phase == "success" else "failed",main.now_iso(),operation_id,node_id))
        followup_operation_id = None
        if phase == "success" and row["followup_package_sha256"]:
            existing = con.execute("SELECT operation_id FROM agent_upgrades WHERE parent_operation_id=?", (operation_id,)).fetchone()
            if existing:
                followup_operation_id = existing["operation_id"]
            else:
                followup_operation_id = str(uuid.uuid4())
                stamp = main.now_iso()
                runtime = con.execute("SELECT process_id FROM agent_upgrade_runtime WHERE node_id=?", (node_id,)).fetchone()
                con.execute("""INSERT INTO agent_upgrades(operation_id,node_id,phase,version,build_id,
                    package_sha256,size_bytes,before_process,deadline_epoch,created_at,updated_at,
                    artifact_kind,parent_operation_id) VALUES(?,?,'queued',?,?,?,?,?,?,?,?, 'full',?)""",
                    (followup_operation_id,node_id,row["followup_version"],row["followup_build_id"],
                     row["followup_package_sha256"],row["followup_size_bytes"],
                     runtime["process_id"] if runtime else None,time.time()+900,stamp,stamp,operation_id))
                con.execute("""INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at)
                    VALUES(?,?,'agent_upgrade',?,'queued',?)""",
                    (followup_operation_id,node_id,json.dumps({"operation_id":followup_operation_id}),stamp))
    return {"status":"ok", "followup_operation_id":followup_operation_id}


def install_routes(app):
    @app.post("/nodes/{node_id}/upgrade-agent")
    def trigger(node_id: str):
        start(node_id)
        return RedirectResponse("/agents", status_code=303)

    @app.get("/api/nodes/{node_id}/agent-upgrade")
    def status(node_id: str):
        node = next((n for n in host().list_control_nodes() if n["node_id"] == node_id), None)
        if not node:
            raise HTTPException(404, "Node introuvable.")
        return JSONResponse({"node_status":node["status"], "heartbeat_age_seconds":node["heartbeat_age_seconds"], **node["upgrade"]})

    @app.post("/api/agent/v1/{node_id}/upgrades/bootstrap")
    async def bootstrap(node_id: str, request: Request):
        host().authenticate_agent(request,node_id)
        payload = await request.json()
        return JSONResponse(start(node_id,bootstrap=True,expected_sha=payload.get("package_sha256")))

    @app.get("/api/agent/v1/{node_id}/upgrades/{operation_id}")
    def get_operation(node_id: str, operation_id: str, request: Request):
        host().authenticate_agent(request,node_id)
        return JSONResponse(operation(node_id,operation_id))

    @app.post("/api/agent/v1/{node_id}/upgrades/{operation_id}/events")
    async def events(node_id: str, operation_id: str, request: Request):
        host().authenticate_agent(request,node_id)
        return JSONResponse(event(node_id,operation_id,await request.json()))

    @app.get("/api/agent/v1/{node_id}/upgrades/{operation_id}/archive")
    def download(node_id: str, operation_id: str, request: Request):
        host().authenticate_agent(request,node_id)
        op = operation(node_id,operation_id)["operation"]
        path = host().DATA_DIR / "agent-upgrades" / "artifacts" / (op["package_sha256"]+".zip")
        if not path.is_file() or path.is_symlink() or digest(path.read_bytes()) != op["package_sha256"]:
            raise HTTPException(503, "Artefact immuable absent ou corrompu.")
        return FileResponse(path,media_type="application/zip",headers={"ETag":op["package_sha256"]})
