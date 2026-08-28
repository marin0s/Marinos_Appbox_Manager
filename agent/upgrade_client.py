"""Agent-side preparation only; activation is exclusively the external helper's job."""
import json
import os
import time
import urllib.request
import uuid
from pathlib import Path
try:
    from agent.upgrade_contract import MAX_PACKAGE_BYTES, atomic_json, prepare_release, validate_package
except ModuleNotFoundError:
    from upgrade_contract import MAX_PACKAGE_BYTES, atomic_json, prepare_release, validate_package

ROOT = Path("/opt/marinos-appbox-agent")
SPOOL = Path("/var/lib/marinos-appbox-agent/upgrades")
PROCESS_ID = uuid.uuid4().hex


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Upgrade redirects are forbidden")


def operation_path(config, operation_id):
    if str(uuid.UUID(operation_id)) != operation_id:
        raise ValueError("Invalid operation identifier")
    return f"/api/agent/v1/{config['node_id']}/upgrades/{operation_id}"


def request(config, path, payload=None, binary=False):
    prefix = f"/api/agent/v1/{config['node_id']}/"
    if not path.startswith(prefix) or ".." in path or "\\" in path or "?" in path:
        raise ValueError("Invalid upgrade endpoint")
    req = urllib.request.Request(config["control_plane_url"].rstrip("/") + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json"})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
        if binary:
            deadline, chunks, size = time.monotonic() + 120, [], 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("Upgrade download deadline exceeded")
                chunk = response.read1(min(65536, MAX_PACKAGE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_PACKAGE_BYTES:
                    raise ValueError("Upgrade package too large")
            data = b"".join(chunks)
        else:
            data = response.read(128 * 1024)
    if binary:
        if len(data) > MAX_PACKAGE_BYTES:
            raise ValueError("Upgrade package too large")
        return data
    return json.loads(data)


def runtime_identity(script):
    path = Path(script).resolve()
    try:
        receipt = json.loads(path.with_name("release-receipt.json").read_text())
        managed = path.parent.parent == ROOT / "releases" and (ROOT / "upgrade_launcher.py").is_file()
        return {"build_id": receipt["build_id"], "package_sha256": receipt["sha256"],
                "process_id": PROCESS_ID, "pid": os.getpid(), "managed": managed}
    except (OSError, ValueError, KeyError):
        return {"build_id": None, "package_sha256": None,
                "process_id": PROCESS_ID, "pid": os.getpid(), "managed": False}


def stage_upgrade(config, payload):
    operation_id = str(payload.get("operation_id") or "")
    base = operation_path(config, operation_id)
    info = request(config, base)
    op = info["operation"]
    if op["phase"] != "queued" or op["node_id"] != config["node_id"]:
        raise RuntimeError("Upgrade operation not queued for this node")
    SPOOL.mkdir(parents=True, exist_ok=True)
    # Never overwrite another pending handoff.
    if (SPOOL / "request.json").exists():
        raise RuntimeError("Another upgrade handoff needs operator inspection")
    work = SPOOL / operation_id
    work.mkdir(mode=0o700, exist_ok=True)
    # Reserve durable supervision before network/download work; a reboot cannot lose it.
    atomic_json(SPOOL / "request.json", {"operation_id": operation_id})
    handoff_started = False
    try:
        request(config, base + "/events", {"phase": "downloading"})
        data = request(config, base + "/archive", binary=True)
        request(config, base + "/events", {"phase": "verifying"})
        validate_package(data, op["package_sha256"])
        prepare_release(work / "candidate", data, op["package_sha256"])
        archive = work / "agent.zip"
        with archive.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        handoff_started = True
        request(config, base + "/events", {"phase": "prepared"})
        return {"operation_id": operation_id, "handoff": "prepared"}
    except Exception:
        try:
            if not handoff_started:
                request(config, base + "/events", {"phase": "upgrade_failed", "error_code": "preparation_failed"})
        except Exception:
            pass
        raise RuntimeError("Agent upgrade preparation interrupted; inspect external supervisor state") from None
