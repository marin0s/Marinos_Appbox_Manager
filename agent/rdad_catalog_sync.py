"""Bounded, persistent RDAD catalog replication. This component never contacts Plex."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path, PurePosixPath


LIBRARIES = ("radarr", "radarr-4k", "sonarr", "sonarr-4k")
SAFE_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SAFE_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
SAFE_REMOTE_ROOT = re.compile(r"^/[A-Za-z0-9._/-]+$")
DISABLED = {"false", "0", "disabled", "off", "none"}


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run(command: list[str], timeout: int) -> int:
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def _valid_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(SAFE_HOST.fullmatch(value)) and ".." not in value


def _valid_remote_root(value: str) -> bool:
    path = PurePosixPath(value)
    return (bool(SAFE_REMOTE_ROOT.fullmatch(value)) and path.is_absolute()
            and ".." not in path.parts and ".mnt" not in path.parts and str(path) != "/")


def _valid_local_root(path: Path) -> bool:
    return path.is_absolute() and path != path.parent and ".." not in path.parts and ".mnt" not in path.parts


class CatalogSyncEngine:
    def __init__(self, config: dict, *, runner=None, logger=None, clock=None, legacy_probe=None):
        self.config = config
        self.node_id = str(config.get("node_id") or "unknown")
        self.runner = runner or _run
        self.logger = logger or self._log
        self.clock = clock or time.time
        self.legacy_probe = legacy_probe or self.legacy_runtime_active
        self.interval = max(60, float(config.get("rdad_catalog_sync_interval", 300)))
        self.timeout = max(10, min(3600, int(config.get("rdad_catalog_sync_timeout", 180))))
        self.state_file = Path(config.get(
            "rdad_catalog_sync_state_file",
            "/var/lib/marinos-appbox-agent/rdad-catalog-sync/state.json",
        ))

    def _log(self, event: str, **fields) -> None:
        safe = {key: value for key, value in fields.items()
                if key in {"library", "result", "return_code", "attempted", "succeeded", "failed"}}
        print(json.dumps({"component":"rdad_catalog_sync", "event":event,
                          "node":self.node_id, **safe}, sort_keys=True), flush=True)

    def legacy_runtime_active(self) -> bool:
        for unit in ("sync-decypharr-catalogs.timer", "sync-decypharr-catalogs.service"):
            try:
                result = subprocess.run(["systemctl", "is-active", unit], text=True,
                                        capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                return True  # unknown legacy state fails closed; never risk a competing --delete
            if result.returncode == 0 and result.stdout.strip() == "active":
                return True
        return False

    def _settings(self):
        setting = self.config.get("rdad_catalog_sync_enabled", "auto")
        if setting is False or str(setting).lower() in DISABLED:
            return None, "disabled"
        if self.legacy_probe():
            return None, "legacy_timer_active"
        host = str(self.config.get("rdad_catalog_sync_host") or "").strip()
        user = str(self.config.get("rdad_catalog_sync_user") or "root").strip()
        identity = Path(str(self.config.get(
            "rdad_catalog_sync_identity_file", "/root/.ssh/id_ed25519_decypharr_sync")))
        source = str(self.config.get("rdad_catalog_sync_source_root") or "/mnt/media/decypharr")
        destination = Path(str(self.config.get(
            "rdad_catalog_sync_destination_root", "/mnt/decypharr-poc")))
        if (not host or not _valid_host(host) or not SAFE_USER.fullmatch(user)
                or not _valid_remote_root(source) or not _valid_local_root(destination)
                or not identity.is_absolute() or ".." in identity.parts
                or any(char not in "/\\:._-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
                       for char in str(identity))):
            return None, "configuration_invalid"
        if not identity.is_file() or identity.is_symlink():
            return None, "identity_unavailable"
        if not destination.is_dir() or destination.is_symlink():
            return None, "destination_root_unavailable"
        try:
            if destination.resolve() != destination.absolute():
                return None, "destination_root_unsafe"
        except OSError:
            return None, "destination_root_unavailable"
        return {"host":host, "user":user, "identity":identity,
                "source":PurePosixPath(source), "destination":destination}, "enabled"

    def _load(self) -> dict:
        if not self.state_file.exists():
            return {"schema":1, "libraries":{}}
        if self.state_file.is_symlink() or not self.state_file.is_file():
            raise RuntimeError("sync_state_unsafe")
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if state.get("schema") != 1 or not isinstance(state.get("libraries"), dict):
                raise ValueError
            return state
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("sync_state_invalid") from exc

    def _command(self, settings: dict, library: str) -> list[str]:
        host = f"[{settings['host']}]" if ":" in settings["host"] else settings["host"]
        source = f"{settings['user']}@{host}:{settings['source'].as_posix()}/{library}/"
        destination = str(settings["destination"] / library) + os.sep
        ssh = (f"ssh -i {settings['identity']} -oBatchMode=yes -oIdentitiesOnly=yes "
               f"-oStrictHostKeyChecking=yes -oConnectTimeout={min(self.timeout, 60)}")
        return ["rsync", "-a", "--delete", "--links", f"--timeout={self.timeout}",
                "-e", ssh, "--", source, destination]

    def run_cycle(self) -> dict:
        settings, reason = self._settings()
        if settings is None:
            self.logger("cycle_skipped", result=reason)
            return {"enabled":False, "reason":reason, "attempted":0, "succeeded":0, "failed":0}
        try:
            state = self._load()
        except RuntimeError as exc:
            self.logger("cycle_failed", result=str(exc))
            return {"enabled":True, "error":str(exc), "attempted":0, "succeeded":0, "failed":0}
        now = float(self.clock())
        due = []
        for library in LIBRARIES:
            previous = state["libraries"].get(library, {})
            attempted = previous.get("last_attempt_epoch")
            if not isinstance(attempted, (int, float)) or now < attempted or now - attempted >= self.interval:
                due.append(library)
        if not due:
            return {"enabled":True, "reason":"not_due", "attempted":0, "succeeded":0, "failed":0}
        succeeded = failed = 0
        for library in due:
            record = state["libraries"].setdefault(library, {})
            record.update(last_attempt_epoch=now, last_attempt_at=utc_stamp())
            destination = settings["destination"] / library
            result = "failed"
            code = 1
            try:
                if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
                    result, code = "destination_unsafe", 126
                else:
                    destination.mkdir(mode=0o755, exist_ok=True)
                    if destination.resolve().parent != settings["destination"].resolve():
                        result, code = "destination_unsafe", 126
                    else:
                        code = int(self.runner(self._command(settings, library), self.timeout))
                        result = "success" if code == 0 else ("timeout" if code == 124 else "rsync_failed")
            except (OSError, ValueError):
                result, code = "local_io_failed", 125
            record.update(last_result=result, return_code=code)
            if code == 0:
                succeeded += 1
                record.update(last_success_epoch=now, last_success_at=utc_stamp())
                self.logger("catalog_sync_success", library=library, result=result, return_code=code)
            else:
                failed += 1
                self.logger("catalog_sync_failed", library=library, result=result, return_code=code)
            _atomic_json(self.state_file, state)
        summary = {"enabled":True, "attempted":len(due), "succeeded":succeeded, "failed":failed}
        self.logger("cycle_complete", **summary)
        return summary
