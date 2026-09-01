#!/usr/bin/env python3
import json
import hashlib
import ipaddress
import re
import secrets
import os
import platform
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from threading import Event, Lock, Thread
from queue import Empty, Full, Queue
import uuid
import urllib.error
import urllib.request
import urllib.parse
import http.client
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
try:
    from agent.reference_contract import IDENTITY_ATTRIBUTES, sanitize_preferences, validate_archive, extract_archive, sha256_file, plex_runtime_preferences, apply_plex_runtime_preferences
except ModuleNotFoundError:
    from reference_contract import IDENTITY_ATTRIBUTES, sanitize_preferences, validate_archive, extract_archive, sha256_file, plex_runtime_preferences, apply_plex_runtime_preferences

try:
    from agent.upgrade_client import runtime_identity, stage_upgrade
except ModuleNotFoundError:
    from upgrade_client import runtime_identity, stage_upgrade

# Capture once: resolving current again after activation would misidentify this process.
RUNTIME_IDENTITY = runtime_identity(__file__)

PRODUCT_VERSION = "1.6.0-alpha.5"
VERSION = f"{PRODUCT_VERSION}-dev"

PLEX_REFERENCE_ARCHIVE_SCHEMA = 1
PLEX_REFERENCE_BUILDER_VERSION = f"{PRODUCT_VERSION}-phase1"
PLEX_REFERENCE_ROOT = Path("Library/Application Support/Plex Media Server")
PLEX_REFERENCE_INCLUDED_DIRECTORIES = (
    "Metadata",
    "Media",
    "Plug-in Support/Databases",
    "Plug-ins",
    "Scanners",
    "Profiles",
    "Resources",
)
PLEX_REFERENCE_EXCLUDED_DIRECTORIES = {
    "cache", "logs", "crash reports", "codecs", "diagnostics",
    "sessions", "session", "transcode", "transcodes", "tmp", "temp",
}
PLEX_REFERENCE_IDENTITY_ATTRIBUTES = IDENTITY_ATTRIBUTES
CONFIG = Path("/etc/marinos-appbox-agent/agent.json")
PLEX_SQLITE_EXECUTABLE = "/usr/lib/plexmediaserver/Plex SQLite"
SQLITE_DIAGNOSTIC_TEXT_LIMIT = 4096


class PlexSQLiteCaptureError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict):
        super().__init__(message)
        self.diagnostics = _sanitize_diagnostics(diagnostics)

    def add_diagnostics(self, key: str, value) -> None:
        self.diagnostics[key] = _sanitize_diagnostics(value)


def _sanitize_diagnostic_text(value: str) -> str:
    text = str(value)
    substitutions = (
        (r"(?i)(authorization\s*:\s*)\S+(?:\s+\S+)?", r"\1[REDACTED]"),
        (
            r"(?i)([\"']?(?:X-Plex-Token|PlexOnlineToken|PLEX_CLAIM|access_token|refresh_token|password|api[_-]?key|secret|token)[\"']?\s*[:=]\s*)[\"']?[^\"',;\s}&]+[\"']?",
            r"\1[REDACTED]",
        ),
        (r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@"),
        (r"(?i)([?&](?:X-Plex-Token|access_token|refresh_token|token|password|api[_-]?key|secret)=)[^&#\s]+", r"\1[REDACTED]"),
        (r"(?i)\bclaim-[A-Za-z0-9_-]{8,}\b", "claim-[REDACTED]"),
    )
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text)
    if len(text) > SQLITE_DIAGNOSTIC_TEXT_LIMIT:
        return text[:SQLITE_DIAGNOSTIC_TEXT_LIMIT] + "...[truncated]"
    return text


def _sanitize_diagnostics(value):
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if re.search(r"(?i)token|password|secret|authorization|claim_code", str(key)) else _sanitize_diagnostics(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_diagnostics(item) for item in value]
    if isinstance(value, str):
        return _sanitize_diagnostic_text(value)
    return value


def _path_diagnostics(path: Path) -> dict:
    path = Path(path)
    result = {
        "path": os.path.abspath(str(path)),
        "exists": False,
        "file_type": "missing",
        "permissions": None,
        "uid": None,
        "gid": None,
        "readable": False,
        "writable": False,
        "searchable": False,
    }
    try:
        details = path.lstat()
        result.update({
            "exists": True,
            "permissions": f"{stat.S_IMODE(details.st_mode):04o}",
            "uid": getattr(details, "st_uid", None),
            "gid": getattr(details, "st_gid", None),
            "readable": os.access(path, os.R_OK),
            "writable": os.access(path, os.W_OK),
            "searchable": os.access(path, os.X_OK),
            "size_bytes": details.st_size,
        })
        if stat.S_ISREG(details.st_mode):
            result["file_type"] = "file"
        elif stat.S_ISDIR(details.st_mode):
            result["file_type"] = "directory"
        elif stat.S_ISLNK(details.st_mode):
            result["file_type"] = "symlink"
        else:
            result["file_type"] = "other"
    except FileNotFoundError:
        pass
    except Exception as exc:
        result["diagnostic_error"] = _sanitize_diagnostic_text(f"{type(exc).__name__}: {exc}")
    return result


def _disk_space_diagnostics(path: Path) -> dict:
    candidate = Path(path)
    try:
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        usage = shutil.disk_usage(candidate)
        return {
            "checked_path": os.path.abspath(str(candidate)),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except Exception as exc:
        return {
            "checked_path": os.path.abspath(str(candidate)),
            "diagnostic_error": _sanitize_diagnostic_text(f"{type(exc).__name__}: {exc}"),
        }


def _sqlite_diagnostics(source: Path, destination: Path, engine: str, executable: str) -> dict:
    source = Path(source)
    destination = Path(destination)
    return {
        "engine": engine,
        "selected_sqlite_executable": executable,
        "sqlite_library_version": sqlite3.sqlite_version,
        "source": _path_diagnostics(source),
        "source_parent": _path_diagnostics(source.parent),
        "source_sidecars": {
            suffix: _path_diagnostics(source.with_name(source.name + suffix))
            for suffix in ("-wal", "-shm", "-journal")
        },
        "destination": _path_diagnostics(destination),
        "destination_parent": _path_diagnostics(destination.parent),
        "destination_free_disk": _disk_space_diagnostics(destination.parent),
        "cwd": os.path.abspath(os.getcwd()),
        "subprocesses": [],
    }


def _refresh_sqlite_paths(diagnostics: dict, source: Path, destination: Path) -> None:
    diagnostics["source"] = _path_diagnostics(source)
    diagnostics["source_parent"] = _path_diagnostics(source.parent)
    diagnostics["destination"] = _path_diagnostics(destination)
    diagnostics["destination_parent"] = _path_diagnostics(destination.parent)
    diagnostics["destination_free_disk"] = _disk_space_diagnostics(destination.parent)


def _sqlite_capture_failure(
    *, stage: str, role: str, failed_path: Path, source: Path,
    destination: Path, diagnostics: dict, error: Exception,
) -> PlexSQLiteCaptureError:
    _refresh_sqlite_paths(diagnostics, source, destination)
    failed = _path_diagnostics(failed_path)
    diagnostics["failure"] = {
        "stage": stage,
        "role": role,
        "failed_path": failed,
        "exception_type": type(error).__name__,
        "original_error": _sanitize_diagnostic_text(str(error)),
    }
    parent = _path_diagnostics(Path(failed_path).parent)
    message = (
        f"Plex SQLite capture failed at {stage}: role={role}, path={failed['path']}, "
        f"engine={diagnostics['engine']}, exists={failed['exists']}, type={failed['file_type']}, "
        f"permissions={failed['permissions']}, uid={failed['uid']}, gid={failed['gid']}, "
        f"readable={failed['readable']}, writable={failed['writable']}; "
        f"parent={parent['path']}, parent_exists={parent['exists']}, "
        f"parent_permissions={parent['permissions']}, parent_uid={parent['uid']}, "
        f"parent_gid={parent['gid']}, parent_writable={parent['writable']}; "
        f"original={_sanitize_diagnostic_text(str(error))}"
    )
    return PlexSQLiteCaptureError(message, diagnostics)


def _run_sqlite_subprocess(arguments: list[str], timeout: int, diagnostics: dict) -> tuple[int, str, str]:
    code, output, error = run(arguments, timeout=timeout)
    diagnostics["subprocesses"].append({
        "arguments": list(arguments),
        "cwd": os.path.abspath(os.getcwd()),
        "return_code": code,
        "stdout": _sanitize_diagnostic_text(output),
        "stderr": _sanitize_diagnostic_text(error),
    })
    return code, output, error


def _close_sqlite_connections(diagnostics: dict, **connections) -> None:
    errors = []
    for role, connection in connections.items():
        if connection is None:
            continue
        try:
            connection.close()
        except Exception as exc:
            errors.append({
                "role": role,
                "exception_type": type(exc).__name__,
                "error": _sanitize_diagnostic_text(str(exc)),
            })
    if errors:
        diagnostics.setdefault("connection_cleanup_errors", []).extend(errors)


def run(command, timeout=15, progress_callback=None):
    if progress_callback is not None:
        started_at = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            while True:
                elapsed = time.monotonic() - started_at
                remaining = timeout - elapsed
                if remaining <= 0:
                    proc.kill()
                    out, err = proc.communicate()
                    return 1, (out or "").strip(), (err or f"Command timed out after {timeout} seconds").strip()
                try:
                    out, err = proc.communicate(timeout=min(1.0, remaining))
                    return proc.returncode, (out or "").strip(), (err or "").strip()
                except subprocess.TimeoutExpired:
                    progress_callback(elapsed)
        except Exception as exc:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
            raise
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def read_meminfo():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0]) * 1024
    except Exception:
        pass
    return values


def cpu_model():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def temperature():
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            value = float(path.read_text().strip())
            if value > 1000:
                value /= 1000
            if -20 < value < 150:
                return round(value, 1)
        except Exception:
            continue
    return None


def command_version(command):
    code, out, _ = run(command)
    return out.splitlines()[0] if code == 0 and out else None



_PREVIOUS_SAMPLE = {"time": None, "disk": None, "net": None, "cpu": None}

def _read_cpu_times():
    try:
        fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        return sum(fields), fields[3] + (fields[4] if len(fields) > 4 else 0)
    except Exception:
        return None

def _read_disk_bytes():
    read_sectors = write_sectors = 0
    try:
        for line in Path("/proc/diskstats").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 14 and not parts[2].startswith(("loop", "ram")):
                read_sectors += int(parts[5]); write_sectors += int(parts[9])
    except Exception:
        pass
    return read_sectors * 512, write_sectors * 512

def _read_net_bytes():
    rx = tx = 0
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            _, data = line.split(":", 1); values = data.split(); rx += int(values[0]); tx += int(values[8])
    except Exception:
        pass
    return rx, tx

def rate_metrics():
    now = time.monotonic(); cpu = _read_cpu_times(); disk = _read_disk_bytes(); net = _read_net_bytes()
    elapsed = max(.1, now - _PREVIOUS_SAMPLE["time"]) if _PREVIOUS_SAMPLE["time"] else 0
    cpu_percent = 0.0
    if elapsed and cpu and _PREVIOUS_SAMPLE["cpu"]:
        total_delta = cpu[0] - _PREVIOUS_SAMPLE["cpu"][0]; idle_delta = cpu[1] - _PREVIOUS_SAMPLE["cpu"][1]
        cpu_percent = max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)) if total_delta else 0.0
    def rate(cur, prev, idx): return max(0.0, (cur[idx]-prev[idx])/elapsed) if elapsed and prev else 0.0
    result = {"cpu_percent": cpu_percent, "disk_read_bps": rate(disk,_PREVIOUS_SAMPLE["disk"],0), "disk_write_bps": rate(disk,_PREVIOUS_SAMPLE["disk"],1), "net_rx_bps": rate(net,_PREVIOUS_SAMPLE["net"],0), "net_tx_bps": rate(net,_PREVIOUS_SAMPLE["net"],1)}
    _PREVIOUS_SAMPLE.update({"time":now,"cpu":cpu,"disk":disk,"net":net})
    return result

def collect_metrics(config):
    mem = read_meminfo()
    disk_path = config.get("disk_path", "/")
    usage = shutil.disk_usage(disk_path)
    docker_version = command_version(["docker", "version", "--format", "{{.Server.Version}}"])
    compose_version = command_version(["docker", "compose", "version", "--short"])
    rdad_path = Path(config.get("rdad_path", "/mnt/decypharr-poc/.mnt"))
    rates = rate_metrics()
    code, counts, _ = run(["docker", "ps", "-a", "--format", "{{.State}}"], timeout=10)
    states = counts.splitlines() if code == 0 and counts else []
    return {
        "hostname": socket.gethostname(),
        "os_name": platform.platform(),
        "kernel_version": platform.release(),
        "docker_version": docker_version,
        "compose_version": compose_version,
        "cpu_model": cpu_model(),
        "cpu_count": os.cpu_count(),
        "load_1": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
        "memory_total_bytes": mem.get("MemTotal"),
        "memory_available_bytes": mem.get("MemAvailable"),
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
        "temperature_c": temperature(),
        "gpu_present": Path("/dev/dri").exists(),
        "rdad_present": rdad_path.exists() and os.access(rdad_path, os.R_OK),
        "docker_ok": docker_version is not None,
        "docker_containers": len(states),
        "running_containers": sum(1 for state in states if state == "running"),
        **rates,
    }



def collect_container_inventory(config):
    code, output, error = run(["docker", "ps", "-aq"], timeout=30)
    if code != 0:
        raise RuntimeError(error or output or "Impossible de lister les conteneurs Docker.")
    ids = [line.strip() for line in output.splitlines() if line.strip()]
    if not ids:
        return []
    code, raw, error = run(["docker", "inspect", *ids], timeout=90)
    if code != 0:
        raise RuntimeError(error or raw or "Impossible d'inspecter les conteneurs Docker.")
    payload = json.loads(raw)
    containers = []
    for item in payload:
        cfg = item.get("Config") or {}
        state = item.get("State") or {}
        network = item.get("NetworkSettings") or {}
        labels = dict(cfg.get("Labels") or {})
        name = str(item.get("Name") or "").lstrip("/")
        ports = []
        for key, bindings in (network.get("Ports") or {}).items():
            container_port, _, protocol = key.partition("/")
            for binding in bindings or [None]:
                binding = binding or {}
                ports.append({
                    "container_port": container_port,
                    "protocol": protocol or "tcp",
                    "host_ip": binding.get("HostIp"),
                    "host_port": binding.get("HostPort"),
                })
        mounts = [{
            "type": mount.get("Type"),
            "source": mount.get("Source"),
            "destination": mount.get("Destination"),
            "mode": mount.get("Mode"),
            "rw": mount.get("RW"),
            "propagation": mount.get("Propagation"),
        } for mount in (item.get("Mounts") or [])]
        networks = [{
            "name": network_name,
            "network_id": details.get("NetworkID"),
            "ip_address": details.get("IPAddress"),
            "gateway": details.get("Gateway"),
            "aliases": details.get("Aliases") or [],
        } for network_name, details in (network.get("Networks") or {}).items()]

        service = {}
        appbox_type = labels.get("marinos.appbox.type")
        compose_service = labels.get("com.docker.compose.service")
        if not appbox_type:
            if compose_service in {"plex", "jellyfin", "tautulli"}:
                appbox_type = compose_service
            elif name.startswith("plex-"):
                appbox_type = "plex"
            elif name.startswith("jellyfin-"):
                appbox_type = "jellyfin"
            elif name.startswith("tautulli-"):
                appbox_type = "tautulli"
        if state.get("Status") == "running" and appbox_type == "plex":
            c, identity, _ = run(["docker", "exec", name, "sh", "-c",
                "curl -fsS http://127.0.0.1:32400/identity || wget -qO- http://127.0.0.1:32400/identity"], timeout=15)
            if c == 0 and "<MediaContainer" in identity:
                import xml.etree.ElementTree as ET
                try:
                    start = identity.find("<?xml") if "<?xml" in identity else identity.find("<MediaContainer")
                    parsed = ET.fromstring(identity[start:])
                    service = {
                        "kind": "plex", "reachable": True,
                        "claimed": parsed.attrib.get("claimed") == "1",
                        "machine_id": parsed.attrib.get("machineIdentifier"),
                        "version": parsed.attrib.get("version"),
                    }
                except Exception:
                    service = {"kind": "plex", "reachable": True, "claimed": None, "version": None}
            else:
                service = {"kind": "plex", "reachable": False, "claimed": None, "version": None}
        elif state.get("Status") == "running" and appbox_type == "jellyfin":
            c, identity, _ = run(["docker", "exec", name, "sh", "-c",
                "curl -fsS http://127.0.0.1:8096/System/Info/Public || wget -qO- http://127.0.0.1:8096/System/Info/Public"], timeout=15)
            if c == 0:
                try:
                    info = json.loads(identity)
                    service = {"kind": "jellyfin", "reachable": True, "version": info.get("Version"), "server_name": info.get("ServerName")}
                except Exception:
                    service = {"kind": "jellyfin", "reachable": True, "version": None, "server_name": None}
            else:
                service = {"kind": "jellyfin", "reachable": False, "version": None, "server_name": None}

        containers.append({
            "container_id": item.get("Id"),
            "name": name,
            "image": cfg.get("Image"),
            "image_id": item.get("Image"),
            "state": state.get("Status") or "unknown",
            "status": state.get("Status"),
            "health": (state.get("Health") or {}).get("Status"),
            "restart_count": int(state.get("RestartCount") or 0),
            "ports": ports,
            "labels": labels,
            "mounts": mounts,
            "networks": networks,
            "created_at": item.get("Created"),
            "service": service,
        })
    return containers



def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
        for name in files:
            try:
                item = Path(root, name)
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    return total


def _plex_library_report(database: Path) -> tuple[list[dict], dict]:
    libraries = []
    totals = {"movies": 0, "shows": 0, "seasons": 0, "episodes": 0, "other": 0}
    if not database.exists():
        return libraries, totals
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        sections = connection.execute(
            "SELECT id,name,section_type FROM library_sections ORDER BY id"
        ).fetchall()
        for section in sections:
            section_id = int(section["id"]); section_type = int(section["section_type"] or 0)
            counts = {"movies": 0, "shows": 0, "seasons": 0, "episodes": 0, "other": 0}
            rows = connection.execute(
                "SELECT metadata_type,COUNT(*) AS amount FROM metadata_items "
                "WHERE library_section_id=? AND deleted_at IS NULL GROUP BY metadata_type",
                (section_id,),
            ).fetchall()
            for row in rows:
                key = {1: "movies", 2: "shows", 3: "seasons", 4: "episodes"}.get(int(row["metadata_type"] or 0), "other")
                counts[key] += int(row["amount"] or 0); totals[key] += int(row["amount"] or 0)
            libraries.append({
                "id": section_id, "name": section["name"], "section_type": section_type,
                "kind": {1: "movie", 2: "show", 8: "music", 13: "photo"}.get(section_type, "other"),
                "counts": counts,
            })
    finally:
        connection.close()
    return libraries, totals


def _reference_storage_requirement(config: dict, estimated_payload: int, temp_parent: Path) -> dict:
    estimated_payload = max(0,int(estimated_payload or 0))
    free = shutil.disk_usage(temp_parent).free
    fixed = max(0,int(config.get('reference_build_reserve_bytes',5*1024**3)))
    ratio = max(0.0,float(config.get('reference_build_reserve_ratio',0.10)))
    margin=max(fixed,int(estimated_payload*ratio))
    required=estimated_payload+margin
    return {'estimated_payload_bytes':estimated_payload,'temporary_free_bytes':free,
            'safety_margin_bytes':margin,'required_free_bytes':required,
            'missing_free_bytes':max(0,required-free),'can_build':free>=required}


def discover_plex_instance(config, payload):
    requested = str((payload or {}).get("source_instance") or "").strip()
    code, raw, error = run(["docker", "ps", "-aq"], timeout=30)
    if code != 0:
        raise RuntimeError(error or "Docker indisponible.")
    ids = [line for line in raw.splitlines() if line.strip()]
    if not ids:
        raise RuntimeError("Aucun conteneur Docker détecté.")
    code, inspected, error = run(["docker", "inspect", *ids], timeout=90)
    if code != 0:
        raise RuntimeError(error or "Inspection Docker impossible.")
    candidates = []
    for item in json.loads(inspected):
        cfg = item.get("Config") or {}; labels = cfg.get("Labels") or {}
        name = str(item.get("Name") or "").lstrip("/")
        service = labels.get("com.docker.compose.service")
        appbox_type = labels.get("marinos.appbox.type")
        is_plex = appbox_type == "plex" or service == "plex" or name.startswith("plex-") or "plex" in str(cfg.get("Image") or "").lower()
        if is_plex and (not requested or requested == name):
            candidates.append(item)
    if not candidates:
        raise RuntimeError(f"Instance Plex introuvable{f' : {requested}' if requested else ''}.")
    if len(candidates) > 1 and not requested:
        raise RuntimeError("Plusieurs instances Plex détectées : " + ", ".join(str(x.get("Name") or "").lstrip("/") for x in candidates))
    item = candidates[0]; cfg = item.get("Config") or {}; state = item.get("State") or {}
    name = str(item.get("Name") or "").lstrip("/")
    mounts = item.get("Mounts") or []
    config_mount = next((m for m in mounts if m.get("Destination") == "/config"), None)
    if not config_mount or not config_mount.get("Source"):
        raise RuntimeError("Montage /config Plex introuvable.")
    config_path = Path(str(config_mount["Source"]))
    plex_root = config_path / "Library/Application Support/Plex Media Server"
    database = plex_root / "Plug-in Support/Databases/com.plexapp.plugins.library.db"
    preferences = plex_root / "Preferences.xml"
    identity = {}
    if state.get("Status") == "running":
        c, output, _ = run(["docker", "exec", name, "sh", "-c", "curl -fsS http://127.0.0.1:32400/identity || wget -qO- http://127.0.0.1:32400/identity"], timeout=15)
        if c == 0 and "<MediaContainer" in output:
            try:
                start = output.find("<?xml") if "<?xml" in output else output.find("<MediaContainer")
                identity = ET.fromstring(output[start:]).attrib
            except Exception:
                identity = {}
    libraries, totals = _plex_library_report(database)
    paths = []
    for mount in mounts:
        destination = str(mount.get("Destination") or "")
        if destination == "/config":
            continue
        source = str(mount.get("Source") or "")
        category = "rdad" if "decypharr" in source.lower() or destination.startswith("/data") else "nas" if source.startswith("/mnt/") else "other"
        paths.append({"source": source, "destination": destination, "type": mount.get("Type"), "read_only": not bool(mount.get("RW")), "category": category})
    sizes = {
        "config": _directory_size(config_path),
        "metadata": _directory_size(plex_root / "Metadata"),
        "media": _directory_size(plex_root / "Media"),
        "cache": _directory_size(plex_root / "Cache"),
        "logs": _directory_size(plex_root / "Logs"),
    }
    estimated_payload = sizes["metadata"] + sizes["media"] + (database.stat().st_size if database.exists() else 0)
    configured_temp = str(config.get("reference_build_temp_dir") or "").strip()
    temp_parent = Path(configured_temp) if configured_temp else (
        Path("/var/lib/marinos-appbox-agent/reference-builds") if os.name == "posix" else Path(tempfile.gettempdir()))
    temp_parent.mkdir(parents=True,exist_ok=True)
    storage = _reference_storage_requirement(config, estimated_payload, temp_parent)
    free_source = storage['temporary_free_bytes']
    safety_margin = storage['safety_margin_bytes']
    required_free = storage['required_free_bytes']
    missing_free = storage['missing_free_bytes']
    warnings = []
    if state.get("Status") != "running": warnings.append("Le conteneur Plex n'est pas en cours d'exécution.")
    if not database.exists(): warnings.append("La base Plex principale est introuvable ou inaccessible.")
    if not preferences.exists(): warnings.append("Preferences.xml est introuvable.")
    if free_source < required_free:
        warnings.append("Espace temporaire insuffisant pour la capture et sa marge de sécurité.")
    blockers = [w for w in warnings if "base Plex" in w or "montage" in w or "Espace temporaire insuffisant" in w]
    score = max(1, 5 - len(warnings) - len(blockers))
    return {
        "schema_version": 1, "read_only": True, "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node": socket.gethostname(),
        "instance": {"container_name": name, "container_id": item.get("Id"), "state": state.get("Status"), "image": cfg.get("Image"), "created_at": item.get("Created"), "plex_version": identity.get("version"), "claimed": identity.get("claimed") == "1" if identity else None},
        "configuration": {"config_path": str(config_path), "database_path": str(database), "preferences_path": str(preferences), "config_readable": os.access(config_path, os.R_OK), "uid_gid": cfg.get("User") or "image-default"},
        "libraries": libraries, "totals": totals, "mounts": paths, "sizes": sizes,
        "preflight": {"docker_ok": True, "config_accessible": config_path.exists() and os.access(config_path, os.R_OK), "database_accessible": database.exists() and os.access(database, os.R_OK), "source_free_bytes": free_source, "temporary_free_bytes": free_source, "estimated_payload_bytes": estimated_payload, "required_free_bytes": required_free, "safety_margin_bytes": safety_margin, "missing_free_bytes": missing_free, "warnings": warnings, "blockers": blockers, "compatibility_score": score, "can_build": not blockers},
        "inclusion_policy": {"included": ["Metadata", "Media", "Plug-in Support/Databases", "bibliothèques", "collections", "affiches", "chemins médias"], "excluded": ["MachineIdentifier", "claim token", "sessions", "cache", "logs", "transcode", "PID", "fichiers temporaires"]},
    }


def _python_sqlite_hot_backup(source: Path, destination: Path, progress_callback=None) -> dict:
    """Snapshot a frozen database through private writable staging."""
    source = Path(source)
    destination = Path(destination)
    diagnostics = _sqlite_diagnostics(source, destination, "python-sqlite3", sys.executable)
    diagnostics["source_staging_strategy"] = "private-writable-copy"

    if not source.is_file():
        error = FileNotFoundError(f"SQLite source database is missing: {source}")
        raise _sqlite_capture_failure(
            stage="source_preflight", role="source", failed_path=source,
            source=source, destination=destination, diagnostics=diagnostics, error=error,
        ) from error

    parent_existed = destination.parent.is_dir()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise _sqlite_capture_failure(
            stage="destination_parent_prepare", role="destination_parent",
            failed_path=destination.parent, source=source, destination=destination,
            diagnostics=diagnostics, error=exc,
        ) from exc
    diagnostics["destination_parent_created"] = not parent_existed

    try:
        destination.unlink(missing_ok=True)
    except Exception as exc:
        raise _sqlite_capture_failure(
            stage="destination_prepare", role="destination", failed_path=destination,
            source=source, destination=destination, diagnostics=diagnostics, error=exc,
        ) from exc

    staging_path = None
    staging_lifecycle = {
        "created": False,
        "cleanup_policy": "TemporaryDirectory",
        "cleanup_completed": False,
    }
    validation = "schema-only"
    try:
        staging_context = tempfile.TemporaryDirectory(prefix="sqlite-source-", dir=destination.parent)
    except Exception as exc:
        raise _sqlite_capture_failure(
            stage="source_staging_prepare", role="destination_parent",
            failed_path=destination.parent, source=source, destination=destination,
            diagnostics=diagnostics, error=exc,
        ) from exc
    try:
        with staging_context as staging_directory:
            staging_path = Path(staging_directory)
            staging_lifecycle.update({
                "path": os.path.abspath(str(staging_path)),
                "created": True,
                "exists_during_snapshot": staging_path.is_dir(),
            })
            staged_source = staging_path / source.name
            copied_sidecars = []
            copy_pairs = [(source, staged_source)]
            copy_pairs.extend(
                (source.with_name(source.name + suffix), staged_source.with_name(staged_source.name + suffix))
                for suffix in ("-wal", "-shm", "-journal")
                if source.with_name(source.name + suffix).is_file()
            )
            for copy_source, copy_destination in copy_pairs:
                try:
                    if progress_callback:
                        with copy_source.open('rb') as source_stream, copy_destination.open('xb') as destination_stream:
                            for block in iter(lambda: source_stream.read(1024 * 1024), b''):
                                destination_stream.write(block)
                                progress_callback()
                        shutil.copystat(copy_source, copy_destination)
                    else:
                        shutil.copy2(copy_source, copy_destination)
                    copy_destination.chmod(0o600)
                except Exception as exc:
                    reported_path = getattr(exc, "filename", None)
                    failed_path = Path(reported_path) if reported_path else copy_destination
                    diagnostics["copy_operation"] = {
                        "source": _path_diagnostics(copy_source),
                        "destination": _path_diagnostics(copy_destination),
                    }
                    raise _sqlite_capture_failure(
                        stage="source_stage_copy", role="source_staging",
                        failed_path=failed_path, source=source, destination=destination,
                        diagnostics=diagnostics, error=exc,
                    ) from exc
                if copy_source != source:
                    copied_sidecars.append(copy_source.name[len(source.name):])

            diagnostics["staged_source"] = _path_diagnostics(staged_source)
            diagnostics["staged_source_parent"] = _path_diagnostics(staging_path)
            diagnostics["staged_source_sidecars"] = copied_sidecars

            try:
                source_connection = sqlite3.connect(staged_source, timeout=60)
            except Exception as exc:
                raise _sqlite_capture_failure(
                    stage="source_open", role="staged_source", failed_path=staged_source,
                    source=source, destination=destination, diagnostics=diagnostics, error=exc,
                ) from exc

            try:
                destination_connection = sqlite3.connect(destination, timeout=60)
            except Exception as exc:
                _close_sqlite_connections(diagnostics, source=source_connection)
                raise _sqlite_capture_failure(
                    stage="destination_open", role="destination", failed_path=destination,
                    source=source, destination=destination, diagnostics=diagnostics, error=exc,
                ) from exc

            stage = "backup"
            try:
                source_connection.execute("PRAGMA busy_timeout=60000")
                destination_connection.execute("PRAGMA journal_mode=DELETE")
                backup_progress = ((lambda status, remaining, total: progress_callback())
                                   if progress_callback else None)
                source_connection.backup(destination_connection, pages=2048, sleep=0.05,
                                         progress=backup_progress)
                destination_connection.execute("PRAGMA journal_mode=DELETE")
                stage = "quick_check"
                try:
                    check = destination_connection.execute("PRAGMA quick_check").fetchone()
                    if not check or str(check[0]).lower() != "ok":
                        raise RuntimeError(f"Sauvegarde SQLite incohérente pour {source.name}: {check}")
                    validation = "quick_check"
                except sqlite3.OperationalError as exc:
                    # Plex databases may reference proprietary FTS tokenizers. The
                    # Python SQLite library cannot load them; validate basic readability
                    # without interpreting those virtual tables.
                    if "unknown tokenizer" not in str(exc).lower():
                        raise
                    destination_connection.execute("SELECT schema_version FROM pragma_schema_version").fetchone()
                    destination_connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                    validation = "schema-readable-tokenizer-unavailable"
                destination_connection.commit()
            except Exception as exc:
                raise _sqlite_capture_failure(
                    stage=stage, role="destination", failed_path=destination,
                    source=source, destination=destination, diagnostics=diagnostics, error=exc,
                ) from exc
            finally:
                _close_sqlite_connections(
                    diagnostics,
                    destination=destination_connection,
                    source=source_connection,
                )
            if diagnostics.get("connection_cleanup_errors"):
                error = RuntimeError("SQLite connection cleanup failed")
                raise _sqlite_capture_failure(
                    stage="connection_cleanup", role="destination", failed_path=destination,
                    source=source, destination=destination, diagnostics=diagnostics, error=error,
                ) from error
    except PlexSQLiteCaptureError as exc:
        staging_lifecycle["cleanup_completed"] = bool(staging_path) and not staging_path.exists()
        exc.add_diagnostics("source_staging_lifecycle", staging_lifecycle)
        try:
            destination.unlink(missing_ok=True)
        except Exception as cleanup_exc:
            exc.add_diagnostics("destination_cleanup_error", f"{type(cleanup_exc).__name__}: {cleanup_exc}")
        raise
    except Exception as exc:
        staging_lifecycle["cleanup_completed"] = bool(staging_path) and not staging_path.exists()
        diagnostics["source_staging_lifecycle"] = staging_lifecycle
        try:
            destination.unlink(missing_ok=True)
        except Exception as cleanup_exc:
            diagnostics["destination_cleanup_error"] = str(cleanup_exc)
        raise _sqlite_capture_failure(
            stage="source_staging_cleanup", role="destination", failed_path=destination,
            source=source, destination=destination, diagnostics=diagnostics, error=exc,
        ) from exc
    staging_lifecycle["cleanup_completed"] = bool(staging_path) and not staging_path.exists()
    diagnostics["source_staging_lifecycle"] = staging_lifecycle

    destination.with_name(destination.name + "-wal").unlink(missing_ok=True)
    destination.with_name(destination.name + "-shm").unlink(missing_ok=True)
    _refresh_sqlite_paths(diagnostics, source, destination)
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "name": source.name,
        "source_size_bytes": source.stat().st_size,
        "snapshot_size_bytes": destination.stat().st_size,
        "sha256": digest.hexdigest(),
        "engine": "python-sqlite3",
        "engine_path": sys.executable,
        "validation": validation,
        "quick_check": "ok" if validation == "quick_check" else validation,
        "diagnostics": _sanitize_diagnostics(diagnostics),
    }


def _plex_sqlite_hot_backup(container_name: str, container_source: Path, host_source: Path, destination: Path) -> dict:
    """Use Plex SQLite inside the running container for backup and validation."""
    plex_sqlite = PLEX_SQLITE_EXECUTABLE
    token = uuid.uuid4().hex
    container_snapshot = f"/tmp/appbox-reference-{token}.db"
    diagnostics = _sqlite_diagnostics(host_source, destination, "plex-sqlite", plex_sqlite)
    diagnostics.update({
        "container_name": container_name,
        "container_source_path": str(container_source),
        "container_snapshot_path": container_snapshot,
    })
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
    except Exception as exc:
        raise _sqlite_capture_failure(
            stage="destination_prepare", role="destination", failed_path=destination,
            source=host_source, destination=destination, diagnostics=diagnostics, error=exc,
        ) from exc

    probe_args = ["docker", "exec", container_name, "test", "-x", plex_sqlite]
    probe_code, _, _ = _run_sqlite_subprocess(probe_args, 15, diagnostics)
    if probe_code != 0:
        try:
            result = _python_sqlite_hot_backup(host_source, destination)
        except PlexSQLiteCaptureError as exc:
            exc.add_diagnostics("engine_selection", {
                "requested_engine": "plex-sqlite",
                "selected_engine": "python-sqlite3",
                "plex_probe": diagnostics["subprocesses"][-1],
            })
            raise
        result.setdefault("diagnostics", {})["engine_selection"] = {
            "requested_engine": "plex-sqlite",
            "selected_engine": "python-sqlite3",
            "plex_probe": diagnostics["subprocesses"][-1],
        }
        return result

    failure = None
    try:
        backup_args = [
            "docker", "exec", container_name, plex_sqlite,
            str(container_source), ".timeout 60000", f".backup '{container_snapshot}'",
        ]
        backup_code, backup_out, backup_err = _run_sqlite_subprocess(backup_args, 900, diagnostics)
        if backup_code != 0:
            failure = (
                "plex_backup_subprocess", "source", container_source,
                RuntimeError(backup_err or backup_out or "Plex SQLite backup failed"),
            )
        else:
            check_args = [
                "docker", "exec", container_name, plex_sqlite,
                container_snapshot, "PRAGMA quick_check;",
            ]
            check_code, check_out, check_err = _run_sqlite_subprocess(check_args, 900, diagnostics)
            checks = [line.strip().lower() for line in check_out.splitlines() if line.strip()]
            if check_code != 0 or checks != ["ok"]:
                failure = (
                    "plex_quick_check_subprocess", "container_snapshot", Path(container_snapshot),
                    RuntimeError(check_err or check_out or "Plex SQLite quick_check failed"),
                )
            else:
                copy_args = ["docker", "cp", f"{container_name}:{container_snapshot}", str(destination)]
                copy_code, copy_out, copy_err = _run_sqlite_subprocess(copy_args, 900, diagnostics)
                if copy_code != 0 or not destination.exists():
                    failure = (
                        "plex_snapshot_copy", "destination", destination,
                        RuntimeError(copy_err or copy_out or "Docker copy did not create destination"),
                    )
    finally:
        cleanup_args = [
            "docker", "exec", container_name, "rm", "-f",
            container_snapshot, container_snapshot + "-wal", container_snapshot + "-shm",
        ]
        cleanup_code, cleanup_out, cleanup_err = _run_sqlite_subprocess(cleanup_args, 30, diagnostics)
        diagnostics["container_snapshot_cleanup"] = {
            "attempted": True,
            "success": cleanup_code == 0,
            "return_code": cleanup_code,
        }
        if cleanup_code != 0:
            cleanup_failure = RuntimeError(cleanup_err or cleanup_out or "Container snapshot cleanup failed")
            if failure is None:
                failure = ("plex_snapshot_cleanup", "container_snapshot", Path(container_snapshot), cleanup_failure)
            else:
                diagnostics["container_snapshot_cleanup"]["error"] = _sanitize_diagnostic_text(str(cleanup_failure))

    if failure is not None:
        stage, role, failed_path, error = failure
        raise _sqlite_capture_failure(
            stage=stage, role=role, failed_path=Path(failed_path),
            source=host_source, destination=destination, diagnostics=diagnostics, error=error,
        ) from error

    destination.with_name(destination.name + "-wal").unlink(missing_ok=True)
    destination.with_name(destination.name + "-shm").unlink(missing_ok=True)
    _refresh_sqlite_paths(diagnostics, host_source, destination)
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "name": host_source.name,
        "source_size_bytes": host_source.stat().st_size,
        "snapshot_size_bytes": destination.stat().st_size,
        "sha256": digest.hexdigest(),
        "engine": "plex-sqlite",
        "engine_path": plex_sqlite,
        "validation": "quick_check",
        "quick_check": "ok",
        "diagnostics": _sanitize_diagnostics(diagnostics),
    }


def _prepare_plex_reference_overlay(config_path: Path, workdir: Path, container_name: str = "") -> tuple[Path, dict]:
    plex_rel = PLEX_REFERENCE_ROOT
    source_plex = config_path / plex_rel
    overlay = workdir / "overlay"
    overlay.mkdir(parents=True, exist_ok=True)

    removed = []
    source_preferences = source_plex / "Preferences.xml"
    target_preferences = overlay / plex_rel / "Preferences.xml"
    if source_preferences.exists():
        target_preferences.parent.mkdir(parents=True, exist_ok=True)
        if source_preferences.is_symlink():
            raise RuntimeError("Preferences.xml ne doit pas être un lien.")
        shutil.copyfile(source_preferences, target_preferences)
        removed = sanitize_preferences(target_preferences)

    source_databases = source_plex / "Plug-in Support/Databases"
    target_databases = overlay / plex_rel / "Plug-in Support/Databases"
    target_databases.mkdir(parents=True, exist_ok=True)
    snapshots = []
    copied_auxiliary = []
    if source_databases.exists():
        for item in sorted(source_databases.iterdir(), key=lambda value: value.name):
            if item.is_symlink():
                continue
            if item.is_file() and _is_canonical_plex_database(item.name):
                container_database = Path("/config") / item.relative_to(config_path)
                snapshots.append(_plex_sqlite_hot_backup(container_name, container_database, item, target_databases / item.name) if container_name else _python_sqlite_hot_backup(item, target_databases / item.name))

    engines = sorted({snapshot.get("engine", "unknown") for snapshot in snapshots})
    engine_selection = {
        "selected_engine": "plex-sqlite-with-python-fallback" if container_name else "python-sqlite3",
        "reason": "container-engine-requested" if container_name else "source-container-frozen",
        "plex_executable_candidate": PLEX_SQLITE_EXECUTABLE,
        "container_exec_attempted": bool(container_name),
    }
    return overlay, {
        "identity_attributes_removed": removed,
        "sqlite_snapshots": snapshots,
        "database_auxiliary_files": copied_auxiliary,
        "sqlite_strategy": "+".join(engines) if engines else "no-database",
        "sqlite_engine_selection": engine_selection,
    }


def _is_canonical_plex_database(name: str) -> bool:
    lower = name.lower()
    if not lower.endswith(".db"):
        return False
    stem = lower[:-3]
    return not (
        "backup" in stem
        or stem.endswith(("-copy", "_copy"))
        or re.search(r"(?:^|[-_.])20\d{2}[-_.]\d{2}[-_.]\d{2}(?:[-_.]\d{4,6})?$", stem)
    )


def _plex_archive_member_excluded(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if any(part in PLEX_REFERENCE_EXCLUDED_DIRECTORIES for part in parts[:-1]):
        return True
    name = relative.name.lower()
    if name in PLEX_REFERENCE_EXCLUDED_DIRECTORIES:
        return True
    if name in {".env", "credentials.json"} or name.endswith((".pem", ".key", ".p12", ".pfx", ".log", ".dmp", "-journal", ".pid", "-wal", "-shm", ".tmp", ".temp", ".partial", ".part", ".swp", ".lock", "~")):
        return True
    if name.startswith((".transcode", "transcode-", "transcode_")):
        return True
    return False


def _tar_tree(tar: tarfile.TarFile, source: Path, arcname: str) -> None:
    if not source.exists():
        return

    def archive_filter(info: tarfile.TarInfo):
        relative = _plex_archive_relative(info.name)
        if relative is None or not (info.isfile() or info.isdir()) or _plex_archive_member_excluded(relative):
            return None
        return info

    tar.add(source, arcname=arcname, recursive=True, filter=archive_filter)


def _plex_archive_relative(member_name: str) -> Path | None:
    member = PurePosixPath(member_name)
    root = PurePosixPath(PLEX_REFERENCE_ROOT.as_posix())
    if member.is_absolute() or ".." in member.parts:
        return None
    try:
        relative = member.relative_to(root)
    except ValueError:
        return None
    return Path(*relative.parts)


def _inspect_plex_reference_archive(archive: Path) -> dict:
    root = PLEX_REFERENCE_ROOT.as_posix()
    required = {
        "metadata": f"{root}/Metadata",
        "media": f"{root}/Media",
        "databases": f"{root}/Plug-in Support/Databases",
    }
    metrics = {key: {"size_bytes": 0, "file_count": 0} for key in required}
    included_paths = []
    excluded_violations = []
    uncompressed_size = 0
    database_names = []
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = {member.name.rstrip("/") for member in members}
        for label in PLEX_REFERENCE_INCLUDED_DIRECTORIES:
            prefix = f"{root}/{label}"
            if prefix in names or any(name.startswith(prefix + "/") for name in names):
                included_paths.append(prefix + "/")
        preferences = f"{root}/Preferences.xml"
        if preferences in names:
            included_paths.append(preferences)
        for member in members:
            normalized = member.name.rstrip("/")
            relative = _plex_archive_relative(normalized)
            if relative is None:
                excluded_violations.append(normalized)
                continue
            if relative.parts and _plex_archive_member_excluded(relative):
                excluded_violations.append(normalized)
            if member.isfile():
                uncompressed_size += member.size
                for key, prefix in required.items():
                    if normalized.startswith(prefix + "/"):
                        metrics[key]["size_bytes"] += member.size
                        metrics[key]["file_count"] += 1
                if normalized.startswith(required["databases"] + "/"):
                    database_names.append(Path(normalized).name)
    if excluded_violations:
        raise RuntimeError("L’archive Plex contient des chemins exclus : " + ", ".join(excluded_violations[:10]))
    missing = [prefix for prefix in required.values() if not any(path.startswith(prefix) for path in included_paths)]
    if preferences not in included_paths:
        missing.append(preferences)
    if missing:
        raise RuntimeError("L’archive Plex ne contient pas les chemins requis : " + ", ".join(missing))
    return {
        "included_paths": included_paths,
        "excluded_paths": [
            f"{root}/{name}/" for name in ("Cache", "Logs", "Crash Reports", "Codecs", "Diagnostics", "Sessions", "Transcode")
        ] + ["*.pid", "*.db-wal", "*.db-shm", "*.tmp", "*.temp", "*.partial", "*.part", "*.swp", "*.lock"],
        "uncompressed_size_bytes": uncompressed_size,
        "metadata": metrics["metadata"],
        "media": metrics["media"],
        "databases": {**metrics["databases"], "names": sorted(database_names)},
    }


class CommandCancelled(RuntimeError):
    pass


class _CaptureWriter:
    def __init__(self, path, estimated, progress_callback=None, cancel_event=None):
        self.handle = Path(path).open('wb')
        self.estimated = max(1, int(estimated or 1))
        self.progress_callback = progress_callback
        self.cancel_event = cancel_event
        self.last_reported_at = 0.0
        self.last_reported_bytes = 0

    def write(self, data):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CommandCancelled('Capture annulée par le Control Plane.')
        written = self.handle.write(data)
        current = self.handle.tell()
        now = time.monotonic()
        if self.progress_callback and (now-self.last_reported_at >= 2 or current-self.last_reported_bytes >= 64*1024*1024):
            self.progress_callback(current,self.estimated)
            self.last_reported_at,self.last_reported_bytes=now,current
        return written

    def tell(self): return self.handle.tell()
    def flush(self): return self.handle.flush()
    def close(self): return self.handle.close()


def _archive_plex_reference(config_path: Path, overlay: Path, archive: Path,
                            estimated_payload_bytes=0, progress_callback=None, cancel_event=None) -> dict:
    source_plex = config_path / PLEX_REFERENCE_ROOT
    overlay_plex = overlay / PLEX_REFERENCE_ROOT
    writer = _CaptureWriter(archive,estimated_payload_bytes,progress_callback,cancel_event)
    try:
        with tarfile.open(fileobj=writer, mode="w:gz", compresslevel=3, dereference=False) as tar:
            for directory in PLEX_REFERENCE_INCLUDED_DIRECTORIES:
                source = overlay_plex / directory if directory == "Plug-in Support/Databases" else source_plex / directory
                _tar_tree(tar, source, f"{PLEX_REFERENCE_ROOT.as_posix()}/{directory}")
            preferences = overlay_plex / "Preferences.xml"
            if preferences.exists():
                tar.add(preferences, arcname=f"{PLEX_REFERENCE_ROOT.as_posix()}/Preferences.xml", recursive=False)
        if progress_callback:
            progress_callback(writer.tell(), max(1,int(estimated_payload_bytes or 1)))
    finally:
        writer.close()
    validate_archive(archive, plex=True)
    return _inspect_plex_reference_archive(archive)


def _docker_container_state(container_name: str) -> str:
    code, output, error = run(
        ["docker", "inspect", "-f", "{{.State.Status}}", container_name],
        timeout=30,
    )
    if code != 0:
        raise RuntimeError(f"État Docker de {container_name} indisponible : {(error or output).strip()}")
    state = output.strip().lower()
    if not state:
        raise RuntimeError(f"Docker n’a retourné aucun état pour {container_name}.")
    return state


def _wait_for_container_state(container_name: str, expected: str, timeout: int = 90) -> str:
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        last_state = _docker_container_state(container_name)
        if last_state == expected:
            return last_state
        time.sleep(1)
    raise RuntimeError(
        f"Docker n’a pas confirmé l’état {expected} pour {container_name} "
        f"dans le délai imparti (dernier état : {last_state})."
    )


def _plex_identity_host_urls(container_name: str) -> list[str]:
    code, output, error = run([
        "docker", "inspect", "-f",
        "{{.HostConfig.NetworkMode}}|{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
        container_name,
    ], timeout=30)
    if code != 0:
        raise RuntimeError(f"Réseau Docker de {container_name} indisponible : {(error or output).strip()}")
    network_mode, _, raw_addresses = output.strip().partition("|")
    if network_mode.strip().lower() == "host":
        return ["http://127.0.0.1:32400/identity"]
    urls = []
    for value in raw_addresses.split():
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        host = f"[{address}]" if address.version == 6 else str(address)
        urls.append(f"http://{host}:32400/identity")
    return urls


def _parse_plex_identity(output: str, method: str) -> dict:
    if "<MediaContainer" not in output:
        raise RuntimeError("réponse sans MediaContainer")
    start = output.find("<?xml") if "<?xml" in output else output.find("<MediaContainer")
    identity = ET.fromstring(output[start:]).attrib
    return {
        "reachable": True,
        "version": identity.get("version"),
        "claimed": identity.get("claimed") == "1",
        "identity_generated": bool(identity.get("machineIdentifier")),
        "identity_fingerprint": hashlib.sha256(identity.get("machineIdentifier", "").encode()).hexdigest(),
        "method": method,
    }


def _wait_for_plex_identity(container_name: str, timeout: int = 120) -> dict:
    deadline = time.monotonic() + timeout
    last_error = "endpoint indisponible"
    try:
        host_urls = _plex_identity_host_urls(container_name)
    except Exception as exc:
        host_urls = []
        last_error = str(exc)
    while time.monotonic() < deadline:
        for url in host_urls:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": f"marinos-appbox-agent/{VERSION}"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    output = response.read().decode("utf-8", errors="replace")
                return _parse_plex_identity(output, f"host-http:{url}")
            except Exception as exc:
                last_error = f"{url}: {exc}"
        code, output, error = run([
            "docker", "exec", container_name, "sh", "-c",
            "curl -fsS http://127.0.0.1:32400/identity || wget -qO- http://127.0.0.1:32400/identity",
        ], timeout=15)
        if code == 0:
            try:
                return _parse_plex_identity(output, "container-curl-or-wget")
            except Exception as exc:
                last_error = f"conteneur : {exc}"
        else:
            tool_error = (error or output or "curl/wget indisponible").strip()
            last_error = f"{last_error}; conteneur : {tool_error}"
        time.sleep(2)
    raise RuntimeError(f"Plex /identity ne répond pas après le redémarrage : {last_error}")


def _stop_plex_for_capture(container_name: str) -> dict:
    code, output, error = run(["docker", "stop", "--time", "60", container_name], timeout=90)
    if code != 0:
        raise RuntimeError(f"Arrêt propre de Plex impossible : {(error or output).strip()}")
    final_state = _wait_for_container_state(container_name, "exited")
    return {"success": True, "output": output.strip(), "confirmed_state": final_state}


def _restart_plex_after_capture(container_name: str) -> tuple[dict, dict]:
    code, output, error = run(["docker", "start", container_name], timeout=90)
    if code != 0:
        raise RuntimeError(f"Redémarrage de Plex impossible : {(error or output).strip()}")
    final_state = _wait_for_container_state(container_name, "running")
    identity = _wait_for_plex_identity(container_name)
    return ({"success": True, "output": output.strip(), "confirmed_state": final_state}, identity)


def _capture_plex_reference(config_path: Path, workdir: Path, container_name: str, *,
                            estimated_payload_bytes=0, progress_callback=None, cancel_event=None) -> dict:
    archive = workdir / "reference.tar.gz"
    initial_state = _docker_container_state(container_name)
    if initial_state not in {"running", "exited", "created"}:
        raise RuntimeError(f"État initial Plex non pris en charge pour une capture sûre : {initial_state}.")
    lifecycle = {
        "initial_container_state": initial_state,
        "builder_stopped_container": False,
        "stop_result": {"attempted": False, "success": None},
        "restart_attempted": False,
        "restart_result": {"attempted": False, "success": None},
        "final_container_state": initial_state,
        "plex_identity_health_after_restart": {"checked": False, "reachable": None},
    }
    capture_error = None
    sanitization = {}
    archive_report = {}
    checksum = ""
    try:
        # A running Plex stays online during reference capture. Its databases
        # are snapshotted through Plex SQLite inside the live container.
        # Stopped/created sources use the Python SQLite snapshot path because
        # docker exec is not available in that state.
        snapshot_container = container_name if initial_state == "running" else ""
        overlay, sanitization = _prepare_plex_reference_overlay(
            config_path,
            workdir,
            snapshot_container,
        )
        archive_report = _archive_plex_reference(config_path, overlay, archive,
            estimated_payload_bytes, progress_callback, cancel_event)
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        checksum = digest.hexdigest()
    except Exception as exc:
        capture_error = exc
    finally:
        try:
            lifecycle["final_container_state"] = _docker_container_state(container_name)
        except Exception:
            lifecycle["final_container_state"] = "unknown"

    capture_diagnostics = {
        "container_lifecycle": lifecycle,
        "capture_work_directory": _path_diagnostics(workdir),
        "archive_staging_path": _path_diagnostics(archive),
    }
    if isinstance(capture_error, PlexSQLiteCaptureError):
        for key, value in capture_diagnostics.items():
            capture_error.add_diagnostics(key, value)

    if capture_error is not None:
        raise capture_error
    return {
        "archive": archive,
        "sha256": checksum,
        "archive_report": archive_report,
        "sanitization": sanitization,
        **lifecycle,
    }


def _reference_build_temp_parent(config: dict) -> Path:
    configured = str(config.get("reference_build_temp_dir") or "").strip()
    parent = Path(configured) if configured else (
        Path("/var/lib/marinos-appbox-agent/reference-builds")
        if os.name == "posix" else Path(tempfile.gettempdir())
    )
    stage = "temporary_parent_prepare"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage = "temporary_parent_write_preflight"
        with tempfile.NamedTemporaryFile(prefix=".appbox-write-probe-", dir=parent):
            pass
    except Exception as exc:
        diagnostics = {
            "stage": stage,
            "temporary_directory_parent": _path_diagnostics(parent),
            "free_disk": _disk_space_diagnostics(parent),
            "cwd": os.path.abspath(os.getcwd()),
        }
        raise PlexSQLiteCaptureError(
            f"Plex reference temporary parent is not writable: {os.path.abspath(str(parent))}; "
            f"original={_sanitize_diagnostic_text(str(exc))}",
            diagnostics,
        ) from exc
    return parent


def build_and_upload_plex_reference(config: dict, payload: dict, *, progress_callback=None, cancel_event=None) -> dict:
    discovery = discover_plex_instance(config, payload)
    preflight = discovery.get("preflight") or {}
    if not preflight.get("can_build", False):
        raise RuntimeError("La pré-validation Plex interdit la construction de la référence.")
    config_path = Path((discovery.get("configuration") or {}).get("config_path") or "")
    if not config_path.is_dir():
        raise RuntimeError("Répertoire /config Plex source introuvable.")
    upload_path = str(payload.get("upload_path") or "")
    if not upload_path.startswith("/api/agent/v1/"):
        raise RuntimeError("Destination de téléversement invalide.")

    # The allowlisted archive is streamed from a frozen /config tree plus a small
    # sanitized overlay. Metadata and Media are Plex application data, not RDAD media.
    temp_parent = _reference_build_temp_parent(config)
    try:
        workdir = Path(tempfile.mkdtemp(prefix="appbox-reference-build-", dir=temp_parent))
    except Exception as exc:
        raise PlexSQLiteCaptureError(
            f"Plex reference temporary directory creation failed under {temp_parent}; "
            f"original={_sanitize_diagnostic_text(str(exc))}",
            {
                "stage": "temporary_directory_create",
                "temporary_directory_parent": _path_diagnostics(temp_parent),
                "free_disk": _disk_space_diagnostics(temp_parent),
                "cwd": os.path.abspath(os.getcwd()),
            },
        ) from exc
    temp_lifecycle = {
        "path": os.path.abspath(str(workdir)),
        "parent": _path_diagnostics(temp_parent),
        "created": workdir.is_dir(),
        "exists_during_capture": workdir.is_dir(),
        "cleanup_attempted": False,
        "cleanup_completed": False,
    }
    result = None
    failure = None
    cleanup_error = None
    try:
        container_name = str((discovery.get("instance") or {}).get("container_name") or payload.get("source_instance") or "")
        if not container_name:
            raise RuntimeError("Nom du conteneur Plex source introuvable.")
        # Discovery data may be minutes old; repeat the storage safety check on
        # the actual temporary filesystem immediately before archive creation.
        expected = int(preflight.get('estimated_payload_bytes') or 0)
        current_storage = _reference_storage_requirement(config, expected, temp_parent)
        required = current_storage['required_free_bytes']
        free_now = current_storage['temporary_free_bytes']
        if not current_storage['can_build']:
            raise RuntimeError(f"Espace temporaire devenu insuffisant avant capture : requis={required}, disponible={free_now}, manquant={current_storage['missing_free_bytes']}.")
        if cancel_event is not None and cancel_event.is_set():
            raise CommandCancelled('Capture annulée avant création de l’archive.')
        capture = _capture_plex_reference(config_path, workdir, container_name,
            estimated_payload_bytes=expected, progress_callback=progress_callback,
            cancel_event=cancel_event)
        archive = capture.pop("archive")
        checksum = capture.pop("sha256")
        archive_report = capture.pop("archive_report")
        sanitization = capture.pop("sanitization")

        target = urllib.parse.urlsplit(config["control_plane_url"].rstrip("/") + upload_path)
        connection_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(target.hostname, target.port, timeout=7200)
        try:
            connection.putrequest("PUT", target.path + (("?" + target.query) if target.query else ""))
            connection.putheader("Authorization", f"Bearer {config['token']}")
            connection.putheader("Content-Type", "application/gzip")
            connection.putheader("Content-Length", str(archive.stat().st_size))
            connection.putheader("X-Reference-SHA256", checksum)
            connection.putheader("User-Agent", f"marinos-appbox-agent/{VERSION}")
            connection.endheaders()
            with archive.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    if cancel_event is not None and cancel_event.is_set():
                        raise CommandCancelled('Capture annulée pendant le transfert.')
                    connection.send(block)
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Téléversement refusé HTTP {response.status}: {body[:1000]}")
            stored = json.loads(body)
        finally:
            connection.close()

        sanitization.update({
            "source_unchanged": True,
            "full_staging_copy_created": False,
        })
        manifest = {
            "archive_schema_version": PLEX_REFERENCE_ARCHIVE_SCHEMA,
            "builder_version": PLEX_REFERENCE_BUILDER_VERSION,
            "sha256": checksum,
            "compressed_size_bytes": archive.stat().st_size,
            **archive_report,
            "database_validation_results": sanitization.get("sqlite_snapshots", []),
            "removed_identity_attributes": sanitization.get("identity_attributes_removed", []),
            "source_lifecycle": {
                key: capture[key] for key in (
                    "initial_container_state", "builder_stopped_container", "stop_result",
                    "restart_attempted", "restart_result", "final_container_state",
                    "plex_identity_health_after_restart",
                )
            },
        }
        result = {
            "archive_path": stored.get("archive_path"),
            "sha256": checksum,
            "compressed_size_bytes": archive.stat().st_size,
            "uncompressed_size_bytes": archive_report["uncompressed_size_bytes"],
            "included_paths": archive_report["included_paths"],
            "excluded_paths": archive_report["excluded_paths"],
            "metadata": archive_report["metadata"],
            "media": archive_report["media"],
            "databases": archive_report["databases"],
            "database_validation_results": sanitization.get("sqlite_snapshots", []),
            "removed_identity_attributes": sanitization.get("identity_attributes_removed", []),
            "builder_version": PLEX_REFERENCE_BUILDER_VERSION,
            "archive_schema_version": PLEX_REFERENCE_ARCHIVE_SCHEMA,
            "sanitization": sanitization,
            "discovery": discovery,
            "manifest": manifest,
            **capture,
        }
    except Exception as exc:
        failure = exc
    finally:
        temp_lifecycle["cleanup_attempted"] = True
        try:
            shutil.rmtree(workdir)
        except FileNotFoundError:
            pass
        except Exception as exc:
            cleanup_error = exc
        temp_lifecycle["exists_after_cleanup"] = workdir.exists()
        temp_lifecycle["cleanup_completed"] = not workdir.exists()

    if failure is not None:
        if isinstance(failure, PlexSQLiteCaptureError):
            failure.add_diagnostics("temporary_directory_lifecycle", temp_lifecycle)
            if cleanup_error is not None:
                failure.add_diagnostics("temporary_directory_cleanup_error", f"{type(cleanup_error).__name__}: {cleanup_error}")
        raise failure.with_traceback(failure.__traceback__)
    if cleanup_error is not None:
        raise PlexSQLiteCaptureError(
            f"Plex reference temporary directory cleanup failed: {workdir}; "
            f"original={_sanitize_diagnostic_text(str(cleanup_error))}",
            {"temporary_directory_lifecycle": temp_lifecycle},
        ) from cleanup_error
    if result is None:
        raise RuntimeError("La construction Plex n’a produit aucun résultat.")
    result["temporary_directory_lifecycle"] = temp_lifecycle
    return result


def send_inventory(config):
    payload = {
        "agent_version": VERSION,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "containers": collect_container_inventory(config),
    }
    return api(config, "POST", f"/api/agent/v1/{config['node_id']}/inventory", payload)


def api(config, method, path, payload=None, *, timeout=None):
    base = config["control_plane_url"].rstrip("/")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Content-Type": "application/json",
            "User-Agent": f"marinos-appbox-agent/{VERSION}",
        },
    )
    with urllib.request.urlopen(request, timeout=20 if timeout is None else timeout) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def heartbeat(config, metrics=None, active_command_id=""):
    metrics = metrics or {}
    payload = {
        "agent_id": config.get("agent_id", f"agent-{config['node_id']}"),
        "agent_version": VERSION,
        "runtime": RUNTIME_IDENTITY,
        "endpoint": config.get("endpoint", ""),
        "active_command_id": active_command_id or None,
        "capabilities": {
            "docker": bool(metrics.get("docker_ok")),
            "compose": metrics.get("compose_version") is not None,
            "filesystem": True,
            "inventory": True,
            "reference_distribution": True,
            "reference_deployment": True,
            "reference_cache_delete": True,
            "reference_builder_foundation": True,
            "reference_discovery": True,
            "reference_builders": ["plex"],
            "reference_builder_versions": {"plex": PLEX_REFERENCE_BUILDER_VERSION},
            "reference_archive_schemas": {"plex": PLEX_REFERENCE_ARCHIVE_SCHEMA},
            "reference_builder_intrusive_actions": False,
            "deployment_executor": True,
            "plex_runtime_preferences": True,
            "independent_heartbeat": True,
            "appbox_command_lease": True,
            "appbox_progress": True,
            "appbox_delivery_ack": True,
            "reference_build_command_lease": True,
            "reference_build_delivery_ack": True,
            "remote_upgrade": RUNTIME_IDENTITY["managed"],
        },
    }
    return api(
        config,
        "POST",
        f"/api/agent/v1/{config['node_id']}/heartbeat",
        payload,
    )


CLIENT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,20}$")


def safe_appbox_dir(base: Path, client_id: str) -> Path:
    if not CLIENT_RE.fullmatch(client_id):
        raise RuntimeError("Identifiant AppBox invalide.")
    resolved_base = base.resolve()
    target = (resolved_base / client_id).resolve()
    if target.parent != resolved_base:
        raise RuntimeError("Chemin AppBox hors du répertoire autorisé.")
    return target


def deletion_target(base, client_id, supplied_path=None):
    """Never resolve a client symlink into another client's directory."""
    base = Path(base)
    if not CLIENT_RE.fullmatch(client_id) or not base.is_absolute() or base == base.parent:
        raise RuntimeError("Racine ou identifiant AppBox invalide pour suppression.")
    if base.resolve() != base:
        raise RuntimeError("Racine AppBox via symlink refusée.")
    target = base / client_id
    if supplied_path is not None and Path(supplied_path) != target:
        raise RuntimeError("Chemin AppBox hors racine ou appartenant à un autre client.")
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target, False
    if stat.S_ISLNK(info.st_mode) or target.resolve() != target or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("Chemin AppBox non sûr : symlink ou dossier invalide.")
    return target, True


def reject_appbox_mounts(target, mountinfo=None):
    # A bind mount can expose unrelated data inside an otherwise safe directory.
    if mountinfo is None:
        if not sys.platform.startswith('linux'):
            return
        mountinfo = Path('/proc/self/mountinfo').read_text()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6:
            raise RuntimeError('Table des montages illisible ; suppression refusée.')
        point = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
        if point == target or target in point.parents:
            raise RuntimeError(f'Montage actif dans le dossier AppBox : {point}.')


def delete_appbox_resources(base, client_id, containers, mode, supplied_path=None):
    """Idempotent absence only; Docker/filesystem/security failures remain failures."""
    target, present = deletion_target(base, client_id, supplied_path)
    if mode not in {'archive', 'delete', 'purge'}:
        raise RuntimeError('Mode de suppression invalide.')
    # Historical ab40ah / ab-40ah naming is retained.
    short = client_id[2:].lstrip('-') if client_id.startswith('ab') else client_id
    expected = {f'plex-appb-{short}', f'plex-{client_id}', f'jellyfin-{client_id}', f'tautulli-{client_id}'}
    requested = set(containers or [])
    if any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', n) for n in requested):
        raise RuntimeError('Nom de conteneur invalide.')
    messages = [] if present else ['Répertoire déjà absent ; nettoyage Docker poursuivi.']

    def project_containers():
        code, out, err = run(['docker', 'ps', '-a', '--filter',
            f'label=com.docker.compose.project={client_id}', '--format', '{{.Names}}'], timeout=30)
        if code:
            raise RuntimeError(err or out or 'Docker indisponible.')
        names = set(out.splitlines())
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', n) for n in names):
            raise RuntimeError('Inventaire Docker invalide.')
        return names

    def absent_error(output, name):
        return bool(re.fullmatch(r'(?:Error(?: response from daemon)?: )?No such (?:container|object): '
                                 + re.escape(name), output.strip(), re.I))

    def inspect(name):
        code, out, err = run(['docker', 'inspect', '--type', 'container', name], timeout=20)
        if code:
            if absent_error(err or out, name):
                return None
            raise RuntimeError(err or out or 'Vérification Docker impossible.')
        info = json.loads(out)[0]
        project = (info.get('Config', {}).get('Labels') or {}).get('com.docker.compose.project')
        legacy_mount = any(target == source or target in source.parents for source in
                           (Path(m.get('Source') or '').resolve() for m in info.get('Mounts') or []))
        if (project and project != client_id) or (not project and (name not in expected or not legacy_mount)):
            raise RuntimeError(f'Conteneur hors AppBox : {name}.')
        return info

    names = requested | expected | project_containers()
    existing = {name for name in names if inspect(name) is not None}
    if names - existing:
        messages.append('Conteneur déjà absent.')
    compose = target / 'compose.yml'
    try:
        compose_info = compose.lstat()
    except FileNotFoundError:
        compose_info = None
    if compose_info:
        deletion_target(base, client_id, supplied_path)
        if not stat.S_ISREG(compose_info.st_mode):
            raise RuntimeError('Compose non sûr : fichier régulier requis.')
        code, out, err = run(['docker', 'compose', '-p', client_id, '-f', str(compose),
                              'down', '--remove-orphans'], timeout=300)
        output = '\n'.join(x for x in (out, err) if x)
        if code and not re.fullmatch(r'(?:Warning: )?No resource found to remove\.?', output.strip(), re.I):
            raise RuntimeError(output or f'Docker Compose a retourné {code}.')
        if output:
            messages.append(output)
    else:
        messages.append('Compose déjà absent ; nettoyage Docker direct.')
    for name in sorted(existing):
        code, out, err = run(['docker', 'rm', '-f', name], timeout=300)
        if code and not absent_error(err or out, name):
            raise RuntimeError(err or out or f'Suppression Docker impossible : {name}.')
    remaining = project_containers() | {name for name in names if inspect(name) is not None}
    if remaining:
        raise RuntimeError(f'Suppression incomplète : conteneurs restants {sorted(remaining)}.')
    target, present = deletion_target(base, client_id, supplied_path)
    if mode != 'archive' and present:
        reject_appbox_mounts(target)
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            # Only the absent final directory is success, not arbitrary inner errors.
            if deletion_target(base, client_id, supplied_path)[1]:
                raise
    _, present = deletion_target(base, client_id, supplied_path)
    if mode != 'archive' and present:
        raise RuntimeError('Suppression incomplète : dossier AppBox restant.')
    remaining = project_containers() | {name for name in names if inspect(name) is not None}
    if remaining:
        raise RuntimeError(f'Suppression incomplète : conteneurs recréés {sorted(remaining)}.')
    messages.append('AppBox archivée, configuration conservée.' if mode == 'archive'
                    else 'Suppression idempotente terminée.')
    return {'output':'\n'.join(messages)[-16000:], 'state':'archived' if mode == 'archive' else 'deleted',
            'deletion_mode':mode, 'path':str(target), 'path_exists':present,
            'containers_remaining':[], 'data_preserved':mode == 'archive', 'executor':'docker-verified'}


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)



def safe_extract_tar(archive: Path, destination: Path, progress_callback=None):
    extract_archive(archive, destination, progress_callback=progress_callback)


def install_reference_archive(config: dict, descriptor: dict, app_dir: Path, progress_callback=None):
    path = str(descriptor.get("download_path") or "")
    expected = str(descriptor.get("sha256") or "").lower()
    target_name = str(descriptor.get("target_directory") or "")
    if target_name not in {"plex-config", "jellyfin-config"}:
        raise RuntimeError("Destination de référence invalide.")
    if not path.startswith("/api/agent/v1/") or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("Descripteur de référence invalide.")
    target = app_dir / target_name
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise RuntimeError("Restore refusé : configuration existante, sauvegarde opérateur requise.")
    cache = Path(config.get("reference_cache_dir", "/var/lib/marinos-appbox-agent/reference-cache"))
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / (expected + ".tar.gz")
    temporary = cache / (expected + "." + uuid.uuid4().hex + ".partial")
    staging = app_dir / ("." + target_name + ".staging-" + uuid.uuid4().hex)
    def report(stage,completed=0,total=0,detail=''):
        if progress_callback:
            percent=round(min(1,completed/total)*100) if total else 25
            progress_callback(stage=stage,percent=percent,detail=detail,completed=completed,total=total)
    try:
        report('cache_reference',0,1,'Vérification du cache local de référence.')
        cached_valid=cached.is_file() and sha256_file(cached,progress_callback=report)==expected
        report('cache_reference',1,1,'Cache local valide.' if cached_valid else 'Téléchargement du cache requis.')
        if not cached_valid:
            request = urllib.request.Request(
                config["control_plane_url"].rstrip("/") + path,
                headers={"Authorization": f"Bearer {config['token']}", "User-Agent": f"marinos-appbox-agent/{VERSION}"},
            )
            digest = hashlib.sha256()
            with temporary.open("xb") as output, urllib.request.urlopen(request, timeout=3600) as response:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    digest.update(block)
                    report('checksum_reference',output.tell(),int(descriptor.get('size_bytes') or 0),'Téléchargement et checksum de la référence.')
                output.flush()
                os.fsync(output.fileno())
            if not secrets.compare_digest(digest.hexdigest(), expected):
                raise RuntimeError("Checksum de l’image de déploiement invalide.")
            validate_archive(temporary, plex=target_name == "plex-config",progress_callback=report)
            os.replace(temporary, cached)
        validate_archive(cached, plex=target_name == "plex-config",progress_callback=report)
        staging.mkdir(parents=True)
        safe_extract_tar(cached, staging,progress_callback=report)
        if target_name == "plex-config":
            report('runtime_customization',25,100,'Assainissement des préférences Plex.')
            sanitize_preferences(staging / PLEX_REFERENCE_ROOT / "Preferences.xml")
            # Reuse private-copy SQLite validation; never modify cached/source DBs.
            with tempfile.TemporaryDirectory(prefix="validate-sqlite-", dir=app_dir) as checks:
                databases=list((staging / PLEX_REFERENCE_ROOT / "Plug-in Support/Databases").glob("*.db"))
                for index,database in enumerate(databases,1):
                    report('sqlite_validation',index-1,max(1,len(databases)),f'Validation SQLite {database.name}.')
                    _python_sqlite_hot_backup(
                        database,
                        Path(checks) / database.name,
                        progress_callback=lambda: report(
                            'sqlite_validation', index - 1, max(1, len(databases)),
                            f'Validation SQLite {database.name} en cours.'
                        ),
                    )
                    report('sqlite_validation',index,max(1,len(databases)),f'Validation SQLite {database.name} terminée.')
        if target.exists():
            target.rmdir()  # only an empty directory can be replaced
        os.replace(staging, target)
        report('extraction',1,1,'Référence extraite et activée dans le staging AppBox.')
        return {"status": "ready", "version_id": descriptor.get("version_id"),
                "checksum": expected, "local_path": str(cached), "size_bytes": cached.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)


def delete_reference_cache(config: dict, payload: dict):
    """Delete exactly one checksum-addressed cached archive, idempotently."""
    checksum = str(payload.get('checksum') or '').lower()
    version_id = str(payload.get('version_id') or '')
    supplied = Path(str(payload.get('local_path') or ''))
    if (not re.fullmatch(r'[0-9a-f]{64}', checksum)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', version_id)):
        raise RuntimeError('Descripteur de purge Reference Image invalide.')
    root = Path(config.get('reference_cache_dir', '/var/lib/marinos-appbox-agent/reference-cache'))
    if not root.is_absolute() or root == root.parent or root.resolve() != root:
        raise RuntimeError('Racine de cache Reference Images non sûre.')
    expected = root / (checksum + '.tar.gz')
    if supplied != expected or supplied == root or '..' in supplied.parts:
        raise RuntimeError('Purge hors du cache Reference Images autorisé.')
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return {'cache_absent':True, 'version_id':version_id, 'checksum':checksum,
                'local_path':str(expected), 'bytes_freed':0,
                'output':'Cache déjà absent ; purge idempotente terminée.'}
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError('Racine de cache invalide ou liée symboliquement.')
    try:
        info = expected.lstat()
    except FileNotFoundError:
        return {'cache_absent':True, 'version_id':version_id, 'checksum':checksum,
                'local_path':str(expected), 'bytes_freed':0,
                'output':'Cache déjà absent ; purge idempotente terminée.'}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or expected.resolve() != expected:
        raise RuntimeError('Artefact de cache non sûr : fichier régulier requis.')
    if not secrets.compare_digest(sha256_file(expected), checksum):
        raise RuntimeError('Checksum du cache différent : suppression refusée.')
    size = info.st_size
    expected.unlink()
    if hasattr(os, 'O_DIRECTORY'):
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    if expected.exists() or expected.is_symlink():
        raise RuntimeError('Purge incomplète : cache toujours présent.')
    return {'cache_absent':True, 'version_id':version_id, 'checksum':checksum,
            'local_path':str(expected), 'bytes_freed':size,
            'output':'Cache Reference Image supprimé et absence vérifiée.'}


def verify_manifest(payload: dict, client_id: str, compose: str, env_content: str) -> dict:
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise RuntimeError("Manifeste de déploiement absent ou invalide.")
    if manifest.get("schema_version") != 1:
        raise RuntimeError("Version de manifeste non supportée.")
    if manifest.get("client_id") != client_id:
        raise RuntimeError("Le manifeste ne correspond pas à l’AppBox demandée.")
    expected = manifest.get("files") or {}
    actual = {
        "compose.yml": hashlib.sha256(compose.encode("utf-8")).hexdigest(),
        ".env": hashlib.sha256(env_content.encode("utf-8")).hexdigest(),
    }
    if expected != actual:
        raise RuntimeError("Checksum des fichiers de déploiement invalide.")
    unsigned = dict(manifest)
    supplied_checksum = str(unsigned.pop("checksum", ""))
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not supplied_checksum or not secrets.compare_digest(supplied_checksum, calculated):
        raise RuntimeError("Checksum du manifeste invalide.")
    return manifest


def _wait_plex_ready(container, *, claimed=False, timeout=120):
    _wait_for_container_state(container, "running", timeout=timeout)
    deadline = time.monotonic() + timeout
    last = "HTTP Plex indisponible"
    while time.monotonic() < deadline:
        try:
            identity = _wait_for_plex_identity(container, timeout=max(1, int(deadline - time.monotonic())))
        except Exception:
            raise RuntimeError("Timeout : HTTP Plex /identity indisponible.") from None
        if not identity.get("identity_generated"):
            last = "identité Plex non générée"
        elif not claimed or identity.get("claimed"):
            return identity
        else:
            last = "claim refusé ou non confirmé par Plex"
        time.sleep(2)
    raise RuntimeError(f"Timeout : {last}.")


def claim_plex(app_dir, client_id, containers, claim_code):
    if not re.fullmatch(r"claim-[A-Za-z0-9_-]{8,128}", claim_code):
        raise RuntimeError("Code Claim Plex absent ou invalide.")
    container = next((name for name in containers if name.startswith("plex-")), None)
    if not container:
        raise RuntimeError("Conteneur Plex absent de l'inventaire.")
    compose_path, env_path = app_dir / "compose.yml", app_dir / ".env"
    if not compose_path.is_file():
        raise RuntimeError("Compose absent pour le Claim Plex.")
    before = _wait_plex_ready(container)
    if before.get("claimed"):
        return {"state": "running", "claimed": True, "output": "Plex déjà associé ; aucun jeton injecté."}
    original = compose_path.read_text(encoding="utf-8")
    clean_compose = "\n".join(line for line in original.splitlines() if not re.match(r"\s*PLEX_CLAIM\s*:", line)) + "\n"
    marker = '      VERSION: docker\n'
    if marker not in clean_compose:
        raise RuntimeError("Variable VERSION du service Plex introuvable.")
    claim_compose = clean_compose.replace(marker, marker + '      PLEX_CLAIM: "${PLEX_CLAIM:-}"\n', 1)
    original_env = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    clean_env = "\n".join(line for line in original_env.splitlines() if not re.match(r"\s*(?:export\s+)?PLEX_CLAIM\s*=", line)).rstrip() + "\n"
    command = ["docker", "compose", "-p", client_id, "-f", str(compose_path), "up", "-d", "--force-recreate", "plex"]
    failure = None
    try:
        atomic_write(compose_path, claim_compose)
        atomic_write(env_path, clean_env + f"PLEX_CLAIM={claim_code}\n")
        code, _, _ = run(command, timeout=300)
        if code:
            raise RuntimeError("Échec de recréation Plex pendant le claim.")
        after = _wait_plex_ready(container, claimed=True)
        if after["identity_fingerprint"] != before["identity_fingerprint"]:
            raise RuntimeError("Identité Plex modifiée pendant le claim.")
    except Exception as exc:
        failure = exc
    finally:
        # Both disk and Docker environment must be cleaned on every exit path.
        cleanup_failed = False
        for path, content in ((compose_path, clean_compose), (env_path, clean_env)):
            try:
                atomic_write(path, content)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise RuntimeError("Nettoyage des fichiers de claim impossible ; intervention opérateur requise.") from None
        code, _, _ = run(command, timeout=300)
        if code:
            raise RuntimeError("Nettoyage du conteneur après claim impossible ; intervention opérateur requise, ne pas relancer le claim.") from None
    if failure:
        raise RuntimeError(_sanitize_diagnostic_text(str(failure))) from None
    final = _wait_plex_ready(container, claimed=True)
    if final["identity_fingerprint"] != before["identity_fingerprint"]:
        raise RuntimeError("Identité Plex modifiée après nettoyage.")
    return {"output": "Claim confirmé après nettoyage et redémarrage.", "state": "running",
            "claimed": True, "container": container, "secret_redacted": True,
            "lifecycle": ["container_running", "http_available", "identity_generated", "claimed", "association_confirmed"]}


def execute_command(config, command, *, progress_callback=None, cancel_event=None):
    command_type = command["command_type"]
    if command_type == "agent_upgrade":
        if not RUNTIME_IDENTITY["managed"]:
            raise RuntimeError("Operator bootstrap required")
        return stage_upgrade(config, command.get("payload") or {})
    if command_type == "ping":
        return {"pong": True, "hostname": socket.gethostname(), "agent_version": VERSION}
    if command_type == "inventory":
        metrics = collect_metrics(config)
        inventory = send_inventory(config)
        return {"metrics": metrics, "inventory": inventory}
    if command_type == "reference_discovery":
        return discover_plex_instance(config, command.get("payload") or {})
    if command_type == "reference_build":
        return build_and_upload_plex_reference(config, command.get("payload") or {},
            progress_callback=progress_callback,cancel_event=cancel_event)
    if command_type == "reference_cache_delete":
        return delete_reference_cache(config, command.get('payload') or {})
    if command_type == "appbox_action":
        payload = command.get("payload") or {}
        client_id = str(payload.get("client_id") or "").strip().lower()
        action = str(payload.get("action") or "").strip().lower()
        deletion_mode = str(payload.get("deletion_mode") or "delete").strip().lower()
        if deletion_mode not in {"archive", "delete", "purge"}:
            raise RuntimeError("Mode de suppression invalide.")
        if not CLIENT_RE.fullmatch(client_id):
            raise RuntimeError("Identifiant AppBox invalide.")
        if action not in {"deploy", "start", "restart", "recreate", "stop", "delete", "claim"}:
            raise RuntimeError("Action AppBox non autorisée.")
        def report(stage,percent,detail,**extra):
            if progress_callback:
                progress_callback(stage=stage,percent=percent,detail=detail,**extra)
        report('preparing',5,'Préparation de la commande AppBox.')

        base = Path(config.get("appbox_base_dir", "/srv/appboxes"))
        if action == 'delete':
            return delete_appbox_resources(base, client_id, payload.get('containers') or [],
                                           deletion_mode, payload.get('path'))
        base.mkdir(parents=True, exist_ok=True)
        app_dir = safe_appbox_dir(base, client_id)
        compose_path = app_dir / "compose.yml"
        env_path = app_dir / ".env"
        manifest_path = app_dir / "deployment-manifest.json"

        compose = str(payload.get("compose") or "")
        env_content = str(payload.get("env") or "")
        manifest = None
        reference_result = None
        if action in {"deploy", "recreate"}:
            if not compose:
                raise RuntimeError("Compose absent du manifeste de déploiement.")
            manifest = verify_manifest(payload, client_id, compose, env_content)
            runtime = manifest.get("plex_runtime")
            if runtime is not None:
                try:
                    port = int(runtime["ManualPortMappingPort"])
                except (ValueError, TypeError, KeyError):
                    raise RuntimeError("Préférences Plex du manifeste invalides.") from None
                if runtime != plex_runtime_preferences(client_id, port) or f'"{port}:32400"' not in compose:
                    raise RuntimeError("Préférences Plex incompatibles avec le Compose.")
            reference_archive = payload.get("reference_archive") or None
            reference_result = None
            if reference_archive:
                if app_dir.exists() and any(app_dir.iterdir()):
                    raise RuntimeError("Restore refusé sur une AppBox existante. Utiliser une nouvelle AppBox de test.")
                staging_app = base / (".restore-" + client_id + "-" + uuid.uuid4().hex)
                staging_app.mkdir()
                try:
                    reference_result = install_reference_archive(config, reference_archive, staging_app,progress_callback=progress_callback)
                    if runtime is not None:
                        report('runtime_customization',50,'Application du nom et du port Plex alloués.')
                        apply_plex_runtime_preferences(staging_app / "plex-config", client_id, port)
                    report('write_configuration',25,'Écriture atomique du Compose, de .env et du manifeste.')
                    atomic_write(staging_app / "compose.yml", compose)
                    atomic_write(staging_app / ".env", env_content)
                    atomic_write(staging_app / "deployment-manifest.json", json.dumps(manifest))
                    if app_dir.exists():
                        app_dir.rmdir()
                    os.replace(staging_app, app_dir)
                    report('write_configuration',100,'Configuration AppBox activée atomiquement.')
                finally:
                    if staging_app.exists():
                        shutil.rmtree(staging_app)
            else:
                new_plex = not (app_dir / "plex-config" / PLEX_REFERENCE_ROOT / "Preferences.xml").exists()
                app_dir.mkdir(parents=True, exist_ok=True)
                if action == "deploy" and runtime is not None and new_plex:
                    report('runtime_customization',50,'Application du nom et du port Plex alloués.')
                    apply_plex_runtime_preferences(app_dir / "plex-config", client_id, port)
                report('write_configuration',25,'Écriture atomique du Compose, de .env et du manifeste.')
                atomic_write(compose_path, compose)
                atomic_write(env_path, env_content)
                atomic_write(manifest_path, json.dumps(manifest))
                report('write_configuration',100,'Configuration AppBox écrite.')
            for directory in payload.get("directories") or []:
                if directory in {"plex-config", "jellyfin-config", "jellyfin-cache", "tautulli-config"}:
                    (app_dir / directory).mkdir(exist_ok=True)

        containers = [
            str(name).strip() for name in (payload.get("containers") or [])
            if str(name).strip() and all(c.isalnum() or c in "-_." for c in str(name).strip())
        ]

        run(["docker", "network", "inspect", "appbox-shared"], timeout=10)[0] == 0 or run(
            ["docker", "network", "create", "appbox-shared"], timeout=20
        )

        if action == "claim":
            return claim_plex(app_dir, client_id, containers, str(payload.get("claim_code") or "").strip())

        def docker_direct(verb):
            if not containers:
                raise RuntimeError(f"Compose absent et aucun conteneur enregistré pour {client_id}.")
            code, out, err = run(["docker", verb, *containers], timeout=300)
            return code, out, err, "docker-direct"

        if compose_path.exists():
            compose_cmd = ["docker", "compose", "-p", client_id, "-f", str(compose_path)]
            if action in {"deploy", "start"}:
                args = ["up", "-d"]
            elif action == "restart":
                args = ["restart"]
            elif action == "recreate":
                args = ["up", "-d", "--force-recreate"]
            elif action == "stop":
                args = ["stop"]
            else:
                args = ["down", "--remove-orphans"]
            report('compose_deployment',10,f"Docker Compose {action} lancé pour {client_id}.")
            code, out, err = run(compose_cmd + args, timeout=300,progress_callback=lambda elapsed: report('compose_deployment',min(90,10+int(elapsed)),f'Docker Compose actif depuis {int(elapsed)} s.'))
            executor = "docker-compose"
        else:
            if action in {"deploy", "start"}:
                code, out, err, executor = docker_direct("start")
            elif action == "restart":
                code, out, err, executor = docker_direct("restart")
            elif action == "stop":
                code, out, err, executor = docker_direct("stop")
            else:
                raise RuntimeError("Recréation impossible : aucun Compose disponible sur le node ni transmis par le Control Plane.")

        output = "\n".join(x for x in (out, err) if x)[-16000:]
        if code != 0:
            raise RuntimeError(output or f"Docker a retourné {code}")
        report('compose_deployment',100,'Commande Docker terminée avec succès.')

        if action in {"deploy", "start", "restart", "recreate"}:
            if not containers:
                raise RuntimeError("Aucun conteneur déclaré : validation running impossible.")
            for container in containers:
                report('runtime_wait',25,f'Attente du conteneur {container}.')
                _wait_for_container_state(container, "running", timeout=90)
            plex = next((name for name in containers if name.startswith("plex-")), None)
            if plex:
                _wait_plex_ready(plex)
            report('runtime_wait',100,'Runtime et santé applicative confirmés.')

        if compose_path.exists():
            code, psout, pserr = run(
                ["docker", "compose", "-p", client_id, "-f", str(compose_path), "ps"], timeout=30
            )
            state = psout or pserr
        else:
            code, psout, pserr = run(
                ["docker", "ps", "-a", "--filter", f"name={client_id}", "--format", "{{.Names}} {{.Status}}"],
                timeout=30,
            )
            state = psout or pserr

        return {
            "output": _sanitize_diagnostic_text(output or state or "Commande exécutée."),
            "health_verified": action in {"deploy", "start", "restart", "recreate"},
            "state": state,
            "path": str(app_dir),
            "containers": containers,
            "executor": executor,
            "compose_present": compose_path.exists(),
            "manifest_checksum": (manifest or {}).get("checksum"),
            "reference_cache": reference_result,
            "files_written": ["compose.yml", ".env", "deployment-manifest.json"] if manifest else [],
        }
    raise RuntimeError(f"Commande non supportée : {command_type}")


class CommandProgressReporter:
    """Best-effort UX telemetry that never owns or renews a command lease."""

    def __init__(self, config, command_id, cancel_event):
        self.config = config
        self.command_id = str(command_id)
        self.cancel_event = cancel_event
        self.timeout = min(30.0, max(1.0, float(config.get('command_progress_timeout_seconds', 5))))
        self.pending = Queue(maxsize=16)
        self.last_attempt_at = 0.0
        self.last_stage = ''
        self.last_percent = -1
        self.thread = Thread(target=self._send_loop, name='agent-command-progress', daemon=True)
        self.thread.start()

    def __call__(self, *args, **kwargs):
        if self.cancel_event.is_set():
            raise CommandCancelled('Commande annulée pendant une opération longue.')
        now = time.monotonic()
        stage = str(kwargs.get('stage') or 'capture')
        percent = kwargs.get('percent')
        if len(args) >= 2:
            written, estimated = args[:2]
            total = max(0, int(estimated or 0))
            completed = max(0, int(written or 0))
            payload = {
                'stage': stage,
                'percent': max(0, min(100, round(completed * 100 / total))) if total else 0,
                'bytes_written': completed,
                'estimated_payload_bytes': total,
                'detail': str(kwargs.get('detail') or '')[:500],
            }
        else:
            payload = {
                'stage': stage,
                'percent': max(0, min(100, int(percent or 0))),
                'detail': str(kwargs.get('detail') or '')[:500],
            }
            if kwargs.get('completed') is not None:
                payload['bytes_written'] = int(kwargs['completed'])
            if kwargs.get('total') is not None:
                payload['estimated_payload_bytes'] = int(kwargs['total'])
        current_percent = int(payload.get('percent', -1))
        stage_changed = stage != self.last_stage
        if stage_changed:
            self.last_percent = -1
        significant = (
            stage_changed
            or current_percent >= 100
            or current_percent >= self.last_percent + 5
            or now - self.last_attempt_at >= 2.0
        )
        if not significant:
            return
        self.last_attempt_at = now
        self.last_stage = stage
        self.last_percent = max(self.last_percent, current_percent)
        try:
            self.pending.put_nowait(payload)
        except Full:
            # Coalesce old UX samples; ownership continues through heartbeat.
            try:
                self.pending.get_nowait()
                self.pending.task_done()
            except Empty:
                pass
            self.pending.put_nowait(payload)

    def _send_loop(self):
        while True:
            payload = self.pending.get()
            if payload is None:
                self.pending.task_done()
                return
            stage = payload.get('stage', '')
            percent = payload.get('percent', 0)
            started = time.monotonic()
            print(f"Agent progress: event=attempt command={self.command_id[:12]} stage={stage} percent={percent}", flush=True)
            outcome = 'success'
            try:
                response = api(
                    self.config, 'POST',
                    f"/api/agent/v1/{self.config['node_id']}/commands/{self.command_id}/progress",
                    payload, timeout=self.timeout,
                )
                if response.get('cancel_requested'):
                    self.cancel_event.set()
                    outcome = 'cancel_requested'
            except urllib.error.HTTPError as exc:
                outcome = f'http_{exc.code}'
                if exc.code in {404, 409, 410}:
                    self.cancel_event.set()
            except (urllib.error.URLError, TimeoutError, OSError):
                outcome = 'timeout_or_network_error'
            except Exception:
                outcome = 'unexpected_error'
            finally:
                duration_ms = round((time.monotonic() - started) * 1000, 1)
                print(f"Agent progress: event=completed command={self.command_id[:12]} stage={stage} percent={percent} result={outcome} duration_ms={duration_ms}", flush=True)
                self.pending.task_done()

    def close(self):
        # Give a healthy endpoint a short opportunity to flush stage transitions,
        # but never make the business result wait for an unavailable UX channel.
        drain_until = time.monotonic() + 0.5
        while self.pending.unfinished_tasks and time.monotonic() < drain_until:
            time.sleep(0.01)
        while True:
            try:
                self.pending.get_nowait()
                self.pending.task_done()
            except Empty:
                break
        try:
            self.pending.put_nowait(None)
        except Full:
            return
        self.thread.join(0.1)


def acknowledge_command_delivery(config, command):
    token = str(command.get('delivery_token') or '')
    if not token:
        return None
    attempts = min(10, max(1, int(config.get('command_delivery_ack_attempts', 3))))
    last_error = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            response = api(
                config, 'POST',
                f"/api/agent/v1/{config['node_id']}/commands/{command['command_id']}/ack",
                {'delivery_token': token}, timeout=min(20, max(1, float(config.get('command_delivery_ack_timeout_seconds', 5)))),
            )
            if response.get('status') != 'claimed':
                raise RuntimeError('ACK de livraison non confirmé par le Control Plane.')
            print(f"Agent delivery: event=acknowledged command={str(command['command_id'])[:12]} attempt={attempt} duration_ms={round((time.monotonic()-started)*1000,1)}",flush=True)
            return response
        except urllib.error.HTTPError as exc:
            if exc.code in {404,409,410}:
                raise CommandCancelled('Offre de commande expirée ou remplacée avant exécution.') from None
            last_error = exc
        except (urllib.error.URLError,TimeoutError,OSError) as exc:
            last_error = exc
        print(f"Agent delivery: event=ack_retry command={str(command['command_id'])[:12]} attempt={attempt} duration_ms={round((time.monotonic()-started)*1000,1)}",flush=True)
        if attempt < attempts:
            time.sleep(min(2.0, 0.25 * attempt))
    raise RuntimeError(f"ACK de livraison non confirmé après {attempts} tentative(s).") from last_error


def command_cycle(config, inventory_request=None, runtime=None):
    response = api(
        config,
        "GET",
        f"/api/agent/v1/{config['node_id']}/commands",
    )
    command = response.get("command")
    if not command:
        return
    progress_callback = None
    progress_reporter = None
    cancel_event = None
    if runtime is not None:
        runtime.begin_command(command['command_id'])
        cancel_event = runtime.cancel_event
        try:
            acknowledge_command_delivery(config,command)
        except Exception:
            runtime.finish_command(command['command_id'])
            raise
        progress_reporter = CommandProgressReporter(config, command['command_id'], cancel_event)
        progress_callback = progress_reporter
    try:
        if command.get('command_type') == 'inventory' and inventory_request is not None:
            inventory_request.set()
            result = {'inventory_refresh': 'scheduled'}
        else:
            result = execute_command(config, command,progress_callback=progress_callback,cancel_event=cancel_event)
        if command.get('command_type') == 'appbox_action' and inventory_request is not None:
            inventory_request.set()
            result['inventory_sync'] = 'scheduled'
        elif command.get("command_type") == "appbox_action":
            try:
                result["inventory_sync"] = send_inventory(config)
            except Exception as inventory_exc:
                result["inventory_warning"] = str(inventory_exc)
        payload = {"status": "success", "result": _sanitize_diagnostics(result)}
    except CommandCancelled as exc:
        payload = {"status":"cancelled","error":str(exc),"result":{"temporary_cleanup":"completed"}}
    except Exception as exc:
        diagnostics = getattr(exc, "diagnostics", None)
        result = {"diagnostics": _sanitize_diagnostics(diagnostics)} if isinstance(diagnostics, dict) else {}
        payload = {"status": "failed", "error": _sanitize_diagnostic_text(str(exc)), "result": result}
    if progress_reporter is not None:
        progress_reporter.close()
    try:
        api(config,"POST",f"/api/agent/v1/{config['node_id']}/commands/{command['command_id']}/result",payload)
    finally:
        if runtime is not None:
            runtime.finish_command(command['command_id'])


class AgentLoops:
    """One serial business worker, independent lightweight heartbeat and telemetry."""
    def __init__(self, config):
        self.config = config
        self.stop = Event()
        self.inventory_request = Event()
        self.lock = Lock()
        self.metrics = {}
        self.active_command_id = ""
        self.cancel_event = Event()
        self.heartbeat_interval = min(60, max(1, float(config.get('heartbeat_interval', 60))))
        self.inventory_interval = max(1, float(config.get('inventory_interval', 30)))
        self.command_interval = max(0.1, float(config.get('command_poll_interval', 2)))

    def report_error(self, loop, exc):
        # Never print HTTP bodies (which may contain credentials).
        print(f"Agent {loop}: {_sanitize_diagnostic_text(str(exc))}", flush=True)

    def heartbeat_loop(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    metrics = dict(self.metrics)
                    active_command_id = self.active_command_id
                response = heartbeat(self.config, metrics, active_command_id)
                if active_command_id and response.get('cancel_active_command'):
                    self.cancel_event.set()
                recommended = response.get('heartbeat_interval')
                if recommended is not None:
                    self.heartbeat_interval = min(self.heartbeat_interval, max(1, float(recommended)))
            except Exception as exc:
                self.report_error('heartbeat', exc)
            self.stop.wait(self.heartbeat_interval)

    def telemetry_loop(self):
        while not self.stop.is_set():
            self.inventory_request.clear()
            try:
                # Timestamp the beginning of collection, not a delayed upload.
                stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                metrics = collect_metrics(self.config)
                with self.lock:
                    self.metrics = dict(metrics)
                api(self.config, 'POST', f"/api/agent/v1/{self.config['node_id']}/metrics",
                    {'agent_version': VERSION, 'collected_at': stamp, 'metrics': metrics})
            except Exception as exc:
                self.report_error('metrics', exc)
            try:
                send_inventory(self.config)
            except Exception as exc:
                self.report_error('inventory', exc)
            self.inventory_request.wait(self.inventory_interval)

    def command_loop(self):
        while not self.stop.is_set():
            try:
                command_cycle(self.config, self.inventory_request, self)
            except Exception as exc:
                self.report_error('command', exc)
            self.stop.wait(self.command_interval)

    def begin_command(self, command_id):
        with self.lock:
            self.active_command_id = str(command_id)
            self.cancel_event = Event()

    def finish_command(self, command_id):
        with self.lock:
            if self.active_command_id == str(command_id):
                self.active_command_id = ""
                self.cancel_event = Event()

    def run(self):
        workers = [Thread(target=target, name=name, daemon=True) for name, target in (
            ('agent-heartbeat', self.heartbeat_loop), ('agent-telemetry', self.telemetry_loop))]
        for worker in workers:
            worker.start()
        try:
            self.command_loop()
        finally:
            self.stop.set()
            self.inventory_request.set()
            for worker in workers:
                worker.join(timeout=2)


def main():
    AgentLoops(json.loads(CONFIG.read_text(encoding='utf-8'))).run()


if __name__ == "__main__":
    main()
