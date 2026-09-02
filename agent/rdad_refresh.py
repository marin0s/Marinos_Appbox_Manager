"""Local, persistent and target-isolated RDAD refresh for managed Plex AppBoxes."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


LIBRARY_PATHS = {
    "radarr": "/data/radarr",
    "radarr-4k": "/data/radarr-4k",
    "sonarr": "/data/sonarr",
    "sonarr-4k": "/data/sonarr-4k",
}
LEGACY_CONTAINER_NAMES = frozenset({"plex-appb-34ah"})
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SAFE_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_HTTP_BYTES = 2 * 1024 * 1024


def utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_text(value) -> str:
    text = str(value)[:500]
    text = re.sub(
        r"(?i)((?:X-Plex-Token|PlexOnlineToken|token|password|secret|authorization)\s*[:=]\s*)[^\s,;&]+",
        r"\1[REDACTED]", text,
    )
    return text


def _error_reason(exc: Exception) -> str:
    return _safe_text(exc)


def _run(command: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


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


def _normal_posix_path(value: str) -> str | None:
    path = PurePosixPath(str(value))
    if not path.is_absolute() or ".." in path.parts:
        return None
    return str(path)


@dataclass(frozen=True)
class RefreshTarget:
    node_id: str
    client_id: str
    container_id: str
    container: str
    state: str
    endpoint: str | None
    config_root: Path | None
    legacy: bool = False

    @property
    def identity(self) -> str:
        raw = f"{self.node_id}\0{self.client_id}\0{self.container_id}".encode()
        return hashlib.sha256(raw).hexdigest()


class DockerRuntime:
    def __init__(self, runner=_run):
        self.runner = runner

    def inspect_all(self) -> list[dict]:
        code, output, error = self.runner(["docker", "ps", "-aq"], timeout=30)
        if code:
            raise RuntimeError(error or output or "docker_list_failed")
        identifiers = [line.strip() for line in output.splitlines() if line.strip()]
        if not identifiers:
            return []
        code, output, error = self.runner(["docker", "inspect", *identifiers], timeout=90)
        if code:
            raise RuntimeError(error or output or "docker_inspect_failed")
        value = json.loads(output)
        if not isinstance(value, list):
            raise RuntimeError("docker_inspect_invalid")
        return value

    def media_probe(self, target: RefreshTarget, path: str, mode: str = "readable") -> tuple[bool, str]:
        if not SAFE_CONTAINER.fullmatch(target.container) or _normal_posix_path(path) is None:
            return False, "unsafe_target"
        type_script = 'test -e "$1" || test -L "$1"'
        code, _, _ = self.runner(
            ["docker", "exec", target.container, "sh", "-c", type_script, "sh", path], timeout=15,
        )
        if code:
            return False, "media_absent"
        if mode == "force":
            return True, "force"
        read_script = (
            'item=$(find -L "$1" -type f -print -quit 2>/dev/null); '
            'test -n "$item" && dd if="$item" of=/dev/null bs=1048576 count=1 status=none'
        )
        code, _, _ = self.runner(
            ["docker", "exec", target.container, "sh", "-c", read_script, "sh", path], timeout=45,
        )
        return (True, "readable") if code == 0 else (False, "media_unreadable")


class PlexHTTP:
    def request(self, target: RefreshTarget, path: str, token: str) -> bytes:
        if not target.endpoint or not target.endpoint.startswith("http://127.0.0.1:"):
            raise RuntimeError("plex_endpoint_unavailable")
        request = urllib.request.Request(
            target.endpoint + path,
            headers={"X-Plex-Token": token, "Accept": "application/xml"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(MAX_HTTP_BYTES + 1)
        if len(data) > MAX_HTTP_BYTES:
            raise RuntimeError("plex_response_too_large")
        return data

    def refresh(self, target: RefreshTarget, section: str, path: str, token: str) -> None:
        query = urllib.parse.urlencode({"path": path})
        self.request(target, f"/library/sections/{urllib.parse.quote(section, safe='')}/refresh?{query}", token)


def _published_port(item: dict) -> str | None:
    bindings = ((item.get("NetworkSettings") or {}).get("Ports") or {}).get("32400/tcp") or []
    for binding in bindings:
        port = str((binding or {}).get("HostPort") or "")
        if port.isdigit() and 1 <= int(port) <= 65535:
            return port
    return None


def _config_root(item: dict) -> Path | None:
    for mount in item.get("Mounts") or []:
        if mount.get("Destination") == "/config" and mount.get("Type") in {"bind", "volume"}:
            source = str(mount.get("Source") or "")
            if source and Path(source).is_absolute():
                return Path(source)
    return None


def discover_targets(items: list[dict], node_id: str, *, allow_legacy: bool = True) -> list[RefreshTarget]:
    targets = []
    for item in items:
        config = item.get("Config") or {}
        labels = dict(config.get("Labels") or {})
        container = str(item.get("Name") or "").lstrip("/")
        container_id = str(item.get("Id") or "")
        modern = labels.get("marinos.appbox.type") == "plex"
        client_id = str(labels.get("marinos.appbox.id") or "").strip().lower()
        label_node = str(labels.get("marinos.appbox.node") or "").strip().lower()
        legacy = False
        if modern:
            if not SAFE_ID.fullmatch(client_id) or label_node != str(node_id).lower():
                continue
        elif allow_legacy and container in LEGACY_CONTAINER_NAMES:
            client_id = "legacy-" + container.removeprefix("plex-appb-").lower()
            legacy = True
        else:
            continue
        if not container_id or not SAFE_CONTAINER.fullmatch(container):
            continue
        port = _published_port(item)
        targets.append(RefreshTarget(
            node_id=str(node_id), client_id=client_id, container_id=container_id,
            container=container, state=str((item.get("State") or {}).get("Status") or "unknown"),
            endpoint=f"http://127.0.0.1:{port}" if port else None,
            config_root=_config_root(item), legacy=legacy,
        ))
    return targets


def read_plex_token(target: RefreshTarget) -> tuple[str | None, str]:
    if target.config_root is None:
        return None, "config_mount_missing"
    preferences = target.config_root / "Library/Application Support/Plex Media Server/Preferences.xml"
    try:
        config_root = target.config_root.resolve(strict=True)
        info = preferences.lstat()
        resolved = preferences.resolve(strict=True)
        if preferences.is_symlink() or config_root not in resolved.parents or not resolved.is_file():
            return None, "preferences_invalid"
        if info.st_size > MAX_HTTP_BYTES:
            return None, "preferences_invalid"
        root = ET.fromstring(resolved.read_bytes())
    except FileNotFoundError:
        return None, "preferences_missing"
    except (OSError, ET.ParseError):
        return None, "preferences_invalid"
    token = str(root.attrib.get("PlexOnlineToken") or "")
    return (token, "available") if token else (None, "token_missing")


def plex_section_map(payload: bytes) -> dict[str, str]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("plex_sections_invalid") from exc
    expected = {value: key for key, value in LIBRARY_PATHS.items()}
    result = {}
    for directory in root.findall(".//Directory"):
        section = str(directory.attrib.get("key") or "")
        if not section:
            continue
        for location in directory.findall("./Location"):
            path = _normal_posix_path(str(location.attrib.get("path") or ""))
            if path in expected:
                result[expected[path]] = section
    return result


def plex_is_busy(payload: bytes) -> bool:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise RuntimeError("plex_activities_invalid") from exc
    try:
        return int(root.attrib.get("size") or 0) > 0 or bool(list(root))
    except ValueError as exc:
        raise RuntimeError("plex_activities_invalid") from exc


def scan_catalog_state(root: Path) -> dict[str, str]:
    fingerprints = {}
    root = Path(root)
    for library in LIBRARY_PATHS:
        base = root / library
        if not base.exists():
            continue
        for current, directories, files in os.walk(base, followlinks=False):
            directories[:] = sorted(directories)
            for name in sorted(files):
                path = Path(current, name)
                try:
                    info = path.lstat()
                except OSError:
                    continue
                relative = path.relative_to(root).as_posix()
                parts = PurePosixPath(relative).parts
                top = "/".join(parts[:2]) if len(parts) > 1 else library
                digest = fingerprints.setdefault(top, hashlib.sha256())
                digest.update(relative.encode("utf-8", errors="surrogateescape"))
                digest.update(f"\0{info.st_mtime_ns}:{info.st_size}:{info.st_mode}\n".encode())
    return {top: digest.hexdigest() for top, digest in sorted(fingerprints.items())}


def extract_changed_top_paths(previous: dict[str, str], current: dict[str, str]) -> list[dict[str, str]]:
    changed = []
    seen = set()
    for relative in sorted(set(previous) | set(current)):
        if previous.get(relative) == current.get(relative):
            continue
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] not in LIBRARY_PATHS:
            continue
        library = parts[0]
        path = LIBRARY_PATHS[library]
        if len(parts) > 1:
            path += "/" + parts[1]
        key = (library, path)
        if key not in seen:
            changed.append({"library": library, "path": path})
            seen.add(key)
    return changed


class QueueStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, identity: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("unsafe_target_identity")
        return self.root / "targets" / identity / "queue.json"

    @property
    def catalog_scan_path(self) -> Path:
        return self.root / "catalog-scan.json"

    def last_catalog_scan(self) -> float | None:
        path = self.catalog_scan_path
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("catalog_scan_state_unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema") != 1:
                raise ValueError
            return float(value["scanned_at_epoch"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("catalog_scan_state_invalid") from exc

    def save_catalog_scan(self, scanned_at: float) -> None:
        _atomic_json(self.catalog_scan_path, {
            "schema": 1, "scanned_at_epoch": float(scanned_at), "scanned_at": utc_stamp(),
        })

    def load(self, target: RefreshTarget) -> tuple[dict, bool]:
        path = self.path(target.identity)
        if not path.exists():
            return {
                "schema": 1, "identity": target.identity, "node_id": target.node_id,
                "client_id": target.client_id, "container_id": target.container_id,
                "container": target.container, "legacy": target.legacy, "entries": [],
                "catalog_timestamps": {}, "created_at": utc_stamp(), "last_seen_at": None,
                "orphaned_at": None, "baseline_complete": False,
            }, True
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise RuntimeError("refresh_queue_unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("refresh_queue_invalid") from exc
        if value.get("identity") != target.identity or not isinstance(value.get("entries"), list):
            raise RuntimeError("refresh_queue_identity_mismatch")
        value.setdefault("baseline_complete", bool(value.get("catalog_timestamps")))
        return value, False

    def save(self, target: RefreshTarget, value: dict) -> None:
        _atomic_json(self.path(target.identity), value)

    def mark_orphans(self, active: set[str]) -> None:
        base = self.root / "targets"
        if not base.exists():
            return
        for path in base.glob("*/queue.json"):
            try:
                if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                    continue
                value = json.loads(path.read_text(encoding="utf-8"))
                identity = str(value.get("identity") or "")
                if identity not in active and not value.get("orphaned_at"):
                    value["orphaned_at"] = utc_stamp()
                    _atomic_json(path, value)
            except (OSError, ValueError):
                continue


class TargetedRefreshEngine:
    def __init__(self, config: dict, *, docker=None, plex=None, store=None, catalog_scanner=None,
                 logger=None, clock=None):
        self.config = config
        self.node_id = str(config.get("node_id") or "unknown")
        self.docker = docker or DockerRuntime()
        self.plex = plex or PlexHTTP()
        root = Path(config.get("rdad_refresh_state_dir", "/var/lib/marinos-appbox-agent/rdad-refresh"))
        self.store = store or QueueStore(root)
        self.catalog_root = Path(config.get("rdad_refresh_catalog_root") or self._default_catalog_root())
        self.catalog_scanner = catalog_scanner or scan_catalog_state
        self.logger = logger or self._log
        self.clock = clock or time.time
        self.mode = str(config.get("rdad_refresh_mode") or "readable").lower()
        self.catalog_interval = max(0, float(config.get("rdad_refresh_catalog_interval", 300)))

    def _default_catalog_root(self) -> str:
        configured = Path(self.config.get("rdad_path", "/mnt/decypharr-poc/.mnt"))
        return str(configured.parent if configured.name == ".mnt" else configured)

    def _log(self, event: str, **fields) -> None:
        safe = {
            key: (_safe_text(value) if isinstance(value, str) else value)
            for key, value in fields.items() if "token" not in key.lower()
        }
        print(json.dumps({"component": "rdad_refresh", "event": event, "node": self.node_id, **safe},
                         sort_keys=True, ensure_ascii=True), flush=True)

    def legacy_runtime_active(self) -> bool:
        for unit in ("sync-decypharr-catalogs.timer", "sync-decypharr-catalogs.service"):
            code, output, _ = _run(["systemctl", "is-active", unit], timeout=10)
            if code == 124:
                return True
            if code == 0 and output.strip() == "active":
                return True
        return False

    def enabled(self) -> tuple[bool, str]:
        setting = self.config.get("rdad_refresh_enabled", "auto")
        if setting is False or str(setting).lower() in {"false", "0", "disabled", "off"}:
            return False, "disabled"
        if self.legacy_runtime_active():
            return False, "legacy_timer_active"
        return True, "enabled"

    def _enqueue(self, target: RefreshTarget, state: dict, changes: list[dict[str, str]]) -> None:
        existing = {(entry.get("library"), entry.get("path")) for entry in state["entries"]}
        for change in changes:
            key = (change["library"], change["path"])
            if key in existing:
                continue
            state["entries"].append({**change, "detected_at": utc_stamp(), "last_attempt_at": None,
                                     "last_result": "queued"})
            existing.add(key)
            self.logger("queue_add", client_id=target.client_id, container=target.container, **change)

    def _process(self, target: RefreshTarget, state: dict) -> None:
        if not state["entries"]:
            return
        if target.state != "running":
            self.logger("queue_defer", client_id=target.client_id, container=target.container,
                        result="container_not_running")
            return
        token, reason = read_plex_token(target)
        if not token:
            self.logger("refresh_target_unavailable", client_id=target.client_id,
                        container=target.container, result=reason)
            return
        if not target.endpoint:
            self.logger("refresh_target_unavailable", client_id=target.client_id,
                        container=target.container, result="plex_endpoint_unavailable")
            return
        try:
            sections = plex_section_map(self.plex.request(target, "/library/sections", token))
            if plex_is_busy(self.plex.request(target, "/activities", token)):
                self.logger("queue_defer", client_id=target.client_id, container=target.container,
                            result="plex_busy")
                return
        except Exception as exc:
            self.logger("queue_defer", client_id=target.client_id, container=target.container,
                        result=_error_reason(exc))
            return
        remaining = []
        for entry in state["entries"]:
            library, path = entry.get("library"), entry.get("path")
            entry["last_attempt_at"] = utc_stamp()
            section = sections.get(library)
            if not section:
                entry["last_result"] = "section_not_found"
                remaining.append(entry)
                self.logger("queue_defer", client_id=target.client_id, container=target.container,
                            library=library, path=path, result="section_not_found")
                continue
            readable, probe_reason = self.docker.media_probe(target, path, self.mode)
            if not readable:
                entry["last_result"] = probe_reason
                remaining.append(entry)
                self.logger("queue_defer", client_id=target.client_id, container=target.container,
                            library=library, section=section, path=path, result=probe_reason)
                continue
            try:
                self.plex.refresh(target, section, path, token)
                self.logger("refresh_success", client_id=target.client_id, container=target.container,
                            library=library, section=section, path=path, result="success")
            except Exception as exc:
                entry["last_result"] = _error_reason(exc)
                remaining.append(entry)
                self.logger("refresh_failed", client_id=target.client_id, container=target.container,
                            library=library, section=section, path=path, result=_error_reason(exc))
        state["entries"] = remaining

    def run_cycle(self, force_scan: bool = False) -> dict:
        enabled, reason = self.enabled()
        if not enabled:
            self.logger("cycle_skipped", result=reason)
            return {"enabled": False, "reason": reason, "targets": 0}
        try:
            targets = discover_targets(
                self.docker.inspect_all(), self.node_id,
                allow_legacy=bool(self.config.get("rdad_refresh_legacy_34ah", True)),
            )
        except Exception as exc:
            self.logger("discovery_failed", result=_error_reason(exc))
            return {"enabled": True, "targets": 0, "error": "discovery_failed"}
        active = {target.identity for target in targets}
        self.store.mark_orphans(active)
        if not targets:
            self.logger("cycle_idle", result="no_local_target")
            return {"enabled": True, "targets": 0}

        target_states = []
        for target in targets:
            self.logger("refresh_target_discovered", client_id=target.client_id,
                        container=target.container, result="legacy" if target.legacy else "managed")
            try:
                state, created = self.store.load(target)
                state["last_seen_at"] = utc_stamp()
                state["orphaned_at"] = None
                target_states.append((target, state, created))
            except Exception as exc:
                self.logger("target_failed", client_id=target.client_id,
                            container=target.container, result=_error_reason(exc))

        now = float(self.clock())
        scan_required = bool(force_scan) or any(created or not state.get("baseline_complete")
                            for _, state, created in target_states)
        if not scan_required:
            try:
                last_scan = self.store.last_catalog_scan()
                scan_required = (
                    last_scan is None or now < last_scan
                    or now - last_scan >= self.catalog_interval
                )
            except Exception as exc:
                self.logger("catalog_scan_state_invalid", result=_error_reason(exc))
                scan_required = True

        catalog = None
        if scan_required:
            try:
                catalog = self.catalog_scanner(self.catalog_root)
            except Exception as exc:
                self.logger("catalog_scan_failed", result=_error_reason(exc))

        all_persisted = True
        for target, state, created in target_states:
            try:
                if catalog is not None:
                    previous = state.get("catalog_timestamps") or {}
                    changes = [] if created or not state.get("baseline_complete") else extract_changed_top_paths(previous, catalog)
                    self._enqueue(target, state, changes)
                    state["catalog_timestamps"] = catalog
                    state["baseline_complete"] = True
                self._process(target, state)
                self.store.save(target, state)
            except Exception as exc:
                all_persisted = False
                self.logger("target_failed", client_id=target.client_id,
                            container=target.container, result=_error_reason(exc))
        if catalog is not None and all_persisted and len(target_states) == len(targets):
            self.store.save_catalog_scan(now)
        result = {"enabled": True, "targets": len(targets), "catalog_scanned": catalog is not None}
        if scan_required and catalog is None:
            result["error"] = "catalog_scan_failed"
        return result
