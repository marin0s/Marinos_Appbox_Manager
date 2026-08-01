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
import uuid
import urllib.error
import urllib.request
import urllib.parse
import http.client
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

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
PLEX_REFERENCE_IDENTITY_ATTRIBUTES = (
    "MachineIdentifier", "ProcessedMachineIdentifier",
    "AnonymousMachineIdentifier", "PlexOnlineToken",
    "PlexOnlineUsername", "PlexOnlineMail", "PlexOnlineHome",
    "CertificateUUID", "PubSubServer", "PubSubServerRegion",
)
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
            r"(?i)([\"']?(?:PlexOnlineToken|PLEX_CLAIM|access_token|refresh_token|password|api[_-]?key|secret|token)[\"']?\s*[:=]\s*)[\"']?[^\"',;\s}&]+[\"']?",
            r"\1[REDACTED]",
        ),
        (r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@"),
        (r"(?i)([?&](?:access_token|refresh_token|token|password|api[_-]?key|secret)=)[^&#\s]+", r"\1[REDACTED]"),
        (r"(?i)\bclaim-[A-Za-z0-9_-]{8,}\b", "claim-[REDACTED]"),
    )
    for pattern, replacement in substitutions:
        text = re.sub(pattern, replacement, text)
    if len(text) > SQLITE_DIAGNOSTIC_TEXT_LIMIT:
        return text[:SQLITE_DIAGNOSTIC_TEXT_LIMIT] + "...[truncated]"
    return text


def _sanitize_diagnostics(value):
    if isinstance(value, dict):
        return {str(key): _sanitize_diagnostics(item) for key, item in value.items()}
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


def run(command, timeout=15):
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
    free_source = shutil.disk_usage(config_path).free
    estimated_payload = sizes["metadata"] + sizes["media"] + (database.stat().st_size if database.exists() else 0)
    warnings = []
    if state.get("Status") != "running": warnings.append("Le conteneur Plex n'est pas en cours d'exécution.")
    if not database.exists(): warnings.append("La base Plex principale est introuvable ou inaccessible.")
    if not preferences.exists(): warnings.append("Preferences.xml est introuvable.")
    if free_source < estimated_payload: warnings.append("Espace libre local inférieur à la taille estimée de la référence.")
    blockers = [w for w in warnings if "base Plex" in w or "montage" in w]
    score = max(1, 5 - len(warnings) - len(blockers))
    return {
        "schema_version": 1, "read_only": True, "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node": socket.gethostname(),
        "instance": {"container_name": name, "container_id": item.get("Id"), "state": state.get("Status"), "image": cfg.get("Image"), "created_at": item.get("Created"), "plex_version": identity.get("version"), "claimed": identity.get("claimed") == "1" if identity else None},
        "configuration": {"config_path": str(config_path), "database_path": str(database), "preferences_path": str(preferences), "config_readable": os.access(config_path, os.R_OK), "uid_gid": cfg.get("User") or "image-default"},
        "libraries": libraries, "totals": totals, "mounts": paths, "sizes": sizes,
        "preflight": {"docker_ok": True, "config_accessible": config_path.exists() and os.access(config_path, os.R_OK), "database_accessible": database.exists() and os.access(database, os.R_OK), "source_free_bytes": free_source, "estimated_payload_bytes": estimated_payload, "warnings": warnings, "blockers": blockers, "compatibility_score": score, "can_build": not blockers},
        "inclusion_policy": {"included": ["Metadata", "Media", "Plug-in Support/Databases", "bibliothèques", "collections", "affiches", "chemins médias"], "excluded": ["MachineIdentifier", "claim token", "sessions", "cache", "logs", "transcode", "PID", "fichiers temporaires"]},
    }


def _python_sqlite_hot_backup(source: Path, destination: Path) -> dict:
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
                source_connection.backup(destination_connection, pages=2048, sleep=0.05)
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
        tree = ET.parse(source_preferences)
        root = tree.getroot()
        for key in PLEX_REFERENCE_IDENTITY_ATTRIBUTES:
            if key in root.attrib:
                removed.append(key)
            root.attrib.pop(key, None)
        tree.write(target_preferences, encoding="utf-8", xml_declaration=True)

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
    if name.endswith((".pid", ".db-wal", ".db-shm", ".tmp", ".temp", ".partial", ".part", ".swp", ".lock", "~")):
        return True
    if name.startswith((".transcode", "transcode-", "transcode_")):
        return True
    return False


def _tar_tree(tar: tarfile.TarFile, source: Path, arcname: str) -> None:
    if not source.exists():
        return

    def archive_filter(info: tarfile.TarInfo):
        relative = _plex_archive_relative(info.name)
        if relative is None or info.issym() or info.islnk() or _plex_archive_member_excluded(relative):
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


def _archive_plex_reference(config_path: Path, overlay: Path, archive: Path) -> dict:
    source_plex = config_path / PLEX_REFERENCE_ROOT
    overlay_plex = overlay / PLEX_REFERENCE_ROOT
    with tarfile.open(archive, "w:gz", compresslevel=3, dereference=False) as tar:
        for directory in PLEX_REFERENCE_INCLUDED_DIRECTORIES:
            source = overlay_plex / directory if directory == "Plug-in Support/Databases" else source_plex / directory
            _tar_tree(tar, source, f"{PLEX_REFERENCE_ROOT.as_posix()}/{directory}")
        preferences = overlay_plex / "Preferences.xml"
        if preferences.exists():
            tar.add(preferences, arcname=f"{PLEX_REFERENCE_ROOT.as_posix()}/Preferences.xml", recursive=False)
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


def _capture_plex_reference(config_path: Path, workdir: Path, container_name: str) -> dict:
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
    restoration_error = None
    sanitization = {}
    archive_report = {}
    checksum = ""
    try:
        if initial_state == "running":
            lifecycle["stop_result"] = {"attempted": True, **_stop_plex_for_capture(container_name)}
            lifecycle["builder_stopped_container"] = True
        overlay, sanitization = _prepare_plex_reference_overlay(config_path, workdir)
        archive_report = _archive_plex_reference(config_path, overlay, archive)
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        checksum = digest.hexdigest()
    except Exception as exc:
        capture_error = exc
    finally:
        if initial_state == "running":
            lifecycle["restart_attempted"] = True
            lifecycle["restart_result"] = {"attempted": True, "success": False}
            lifecycle["plex_identity_health_after_restart"] = {"checked": True, "reachable": False}
            try:
                restart_result, identity_health = _restart_plex_after_capture(container_name)
                lifecycle["restart_result"] = {"attempted": True, **restart_result}
                lifecycle["plex_identity_health_after_restart"] = {"checked": True, **identity_health}
            except Exception as exc:
                restoration_error = exc
            try:
                lifecycle["final_container_state"] = _docker_container_state(container_name)
            except Exception as exc:
                if restoration_error is None:
                    restoration_error = exc
                lifecycle["final_container_state"] = "unknown"
        else:
            lifecycle["final_container_state"] = _docker_container_state(container_name)

    capture_diagnostics = {
        "container_lifecycle": lifecycle,
        "capture_work_directory": _path_diagnostics(workdir),
        "archive_staging_path": _path_diagnostics(archive),
    }
    if isinstance(capture_error, PlexSQLiteCaptureError):
        for key, value in capture_diagnostics.items():
            capture_error.add_diagnostics(key, value)

    if restoration_error is not None:
        capture_detail = f" Capture également échouée : {capture_error}" if capture_error else ""
        message = f"Restauration explicite du Plex source échouée : {restoration_error}.{capture_detail}"
        if isinstance(capture_error, PlexSQLiteCaptureError):
            error = PlexSQLiteCaptureError(message, capture_error.diagnostics)
            error.add_diagnostics("restoration_error", f"{type(restoration_error).__name__}: {restoration_error}")
            raise error from restoration_error
        raise RuntimeError(message) from restoration_error
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


def build_and_upload_plex_reference(config: dict, payload: dict) -> dict:
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
        capture = _capture_plex_reference(config_path, workdir, container_name)
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


def api(config, method, path, payload=None):
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
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}


def heartbeat(config):
    metrics = collect_metrics(config)
    payload = {
        "agent_id": config.get("agent_id", f"agent-{config['node_id']}"),
        "agent_version": VERSION,
        "endpoint": config.get("endpoint", ""),
        "capabilities": {
            "docker": metrics["docker_ok"],
            "compose": metrics["compose_version"] is not None,
            "filesystem": True,
            "inventory": True,
            "reference_distribution": True,
            "reference_deployment": True,
            "reference_builder_foundation": True,
            "reference_discovery": True,
            "reference_builders": ["plex"],
            "reference_builder_versions": {"plex": PLEX_REFERENCE_BUILDER_VERSION},
            "reference_archive_schemas": {"plex": PLEX_REFERENCE_ARCHIVE_SCHEMA},
            "reference_builder_intrusive_actions": False,
            "deployment_executor": True,
        },
        "metrics": metrics,
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
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)



def safe_extract_tar(archive: Path, destination: Path):
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if destination != target and destination not in target.parents:
                raise RuntimeError("Archive de référence invalide : chemin hors destination.")
            if member.issym() or member.islnk():
                raise RuntimeError("Archive de référence invalide : liens symboliques refusés.")
        tar.extractall(destination)


def install_reference_archive(config: dict, descriptor: dict, app_dir: Path):
    path = str(descriptor.get("download_path") or "")
    expected = str(descriptor.get("sha256") or "").lower()
    target_name = str(descriptor.get("target_directory") or "")
    if target_name not in {"plex-config", "jellyfin-config"}:
        raise RuntimeError("Destination de l’image de déploiement invalide.")
    if not path or not expected:
        raise RuntimeError("Descripteur d’image de déploiement incomplet.")
    request = urllib.request.Request(
        config["control_plane_url"].rstrip("/") + path,
        headers={
            "Authorization": f"Bearer {config['token']}",
            "User-Agent": f"marinos-appbox-agent/{VERSION}",
        },
    )
    app_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="appbox-reference-", suffix=".tar.gz", delete=False) as temp:
        temp_path = Path(temp.name)
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=3600) as response:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                temp.write(block)
                digest.update(block)
    try:
        if digest.hexdigest().lower() != expected:
            raise RuntimeError("Checksum de l’image de déploiement invalide.")
        target = app_dir / target_name
        staging = app_dir / f".{target_name}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        safe_extract_tar(temp_path, staging)
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
        if target_name == "plex-config":
            preferences = target / "Library" / "Application Support" / "Plex Media Server" / "Preferences.xml"
            if preferences.exists():
                tree = ET.parse(preferences)
                root = tree.getroot()
                for key in (
                    "MachineIdentifier", "ProcessedMachineIdentifier", "AnonymousMachineIdentifier",
                    "PlexOnlineToken", "PlexOnlineUsername", "PlexOnlineMail", "PlexOnlineHome",
                    "CertificateUUID", "PubSubServer", "PubSubServerRegion",
                ):
                    root.attrib.pop(key, None)
                tree.write(preferences, encoding="utf-8", xml_declaration=True)
            for relative in (
                "Library/Application Support/Plex Media Server/Cache",
                "Library/Application Support/Plex Media Server/Logs",
                "Library/Application Support/Plex Media Server/Crash Reports",
            ):
                shutil.rmtree(target / relative, ignore_errors=True)
            for pid in target.rglob("*.pid"):
                pid.unlink(missing_ok=True)
    finally:
        temp_path.unlink(missing_ok=True)


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


def execute_command(config, command):
    command_type = command["command_type"]
    if command_type == "ping":
        return {"pong": True, "hostname": socket.gethostname(), "agent_version": VERSION}
    if command_type == "inventory":
        metrics = collect_metrics(config)
        inventory = send_inventory(config)
        return {"metrics": metrics, "inventory": inventory}
    if command_type == "reference_discovery":
        return discover_plex_instance(config, command.get("payload") or {})
    if command_type == "reference_build":
        return build_and_upload_plex_reference(config, command.get("payload") or {})
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

        base = Path(config.get("appbox_base_dir", "/srv/appboxes"))
        base.mkdir(parents=True, exist_ok=True)
        app_dir = safe_appbox_dir(base, client_id)
        compose_path = app_dir / "compose.yml"
        env_path = app_dir / ".env"
        manifest_path = app_dir / "deployment-manifest.json"

        compose = str(payload.get("compose") or "")
        env_content = str(payload.get("env") or "")
        manifest = None
        if action in {"deploy", "recreate"}:
            if not compose:
                raise RuntimeError("Compose absent du manifeste de déploiement.")
            manifest = verify_manifest(payload, client_id, compose, env_content)
            app_dir.mkdir(parents=True, exist_ok=True)
            atomic_write(compose_path, compose, 0o600)
            atomic_write(env_path, env_content, 0o600)
            atomic_write(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                0o600,
            )
            reference_archive = payload.get("reference_archive") or None
            if reference_archive:
                install_reference_archive(config, reference_archive, app_dir)
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
            claim_code = str(payload.get("claim_code") or "").strip()
            if not re.fullmatch(r"claim-[A-Za-z0-9_-]{8,128}", claim_code):
                raise RuntimeError("Code Claim Plex invalide.")
            if not compose_path.exists():
                raise RuntimeError(f"Compose absent pour le Claim Plex : {compose_path}")
            original_compose = compose_path.read_text(encoding="utf-8")
            original_env = env_path.read_text(encoding="utf-8") if env_path.exists() else None
            claim_compose = original_compose
            if "PLEX_CLAIM:" not in claim_compose:
                marker = '      VERSION: docker\n'
                if marker not in claim_compose:
                    raise RuntimeError("Impossible d’injecter PLEX_CLAIM : variable VERSION introuvable dans le service Plex.")
                claim_compose = claim_compose.replace(marker, marker + '      PLEX_CLAIM: "${PLEX_CLAIM:-}"\n', 1)
            cleaned_env = "\n".join(
                line for line in (original_env or "").splitlines()
                if not line.startswith("PLEX_CLAIM=")
            ).rstrip("\n")
            claim_env = (cleaned_env + "\n" if cleaned_env else "") + f"PLEX_CLAIM={claim_code}\n"
            compose_cmd = ["docker", "compose", "-p", client_id, "-f", str(compose_path)]
            try:
                atomic_write(compose_path, claim_compose, 0o600)
                atomic_write(env_path, claim_env, 0o600)
                code, out, err = run(compose_cmd + ["up", "-d", "--force-recreate", "plex"], timeout=300)
                if code != 0:
                    raise RuntimeError((err or out or "Échec de la recréation Plex avec le Claim.")[-16000:])
                container = containers[0] if containers else ""
                deadline = time.monotonic() + 120
                claimed = False
                while container and time.monotonic() < deadline:
                    c, identity, _ = run(["docker", "exec", container, "sh", "-c",
                        "curl -fsS http://127.0.0.1:32400/identity || wget -qO- http://127.0.0.1:32400/identity"], timeout=15)
                    if c == 0 and ('claimed=\"1\"' in identity or "claimed='1'" in identity):
                        claimed = True
                        break
                    time.sleep(3)
                if not claimed:
                    raise RuntimeError("Plex n’a pas confirmé le Claim dans le délai imparti.")
            finally:
                atomic_write(compose_path, original_compose, 0o600)
                if original_env is None:
                    env_path.unlink(missing_ok=True)
                else:
                    atomic_write(env_path, original_env, 0o600)
            code, clean_out, clean_err = run(compose_cmd + ["up", "-d", "--force-recreate", "plex"], timeout=300)
            if code != 0:
                raise RuntimeError((clean_err or clean_out or "Claim réussi mais nettoyage du token impossible.")[-16000:])
            return {
                "output": "Claim Plex confirmé et conteneur recréé sans jeton persistant.",
                "state": "running",
                "claimed": True,
                "container": containers[0] if containers else None,
                "executor": "docker-compose-agent",
                "secret_redacted": True,
            }

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
            code, out, err = run(compose_cmd + args, timeout=300)
            executor = "docker-compose"
        else:
            if action in {"deploy", "start"}:
                code, out, err, executor = docker_direct("start")
            elif action == "restart":
                code, out, err, executor = docker_direct("restart")
            elif action == "stop":
                code, out, err, executor = docker_direct("stop")
            elif action == "delete":
                code, out, err, executor = docker_direct("rm")
                if code != 0:
                    code, out, err = run(["docker", "rm", "-f", *containers], timeout=300)
            else:
                raise RuntimeError("Recréation impossible : aucun Compose disponible sur le node ni transmis par le Control Plane.")

        output = "\n".join(x for x in (out, err) if x)[-16000:]
        if code != 0:
            raise RuntimeError(output or f"Docker a retourné {code}")

        if action == "delete":
            if deletion_mode != "archive":
                shutil.rmtree(app_dir, ignore_errors=False)
            remaining = []
            for name in containers:
                code_check, out_check, _ = run(["docker", "inspect", "-f", "{{.Name}}", name], timeout=15)
                if code_check == 0 and out_check.strip():
                    remaining.append(name)
            path_exists = app_dir.exists()
            if deletion_mode != "archive" and (path_exists or remaining):
                raise RuntimeError(f"Suppression incomplète : path_exists={path_exists}, containers_remaining={remaining}")
            return {
                "output": output or ("AppBox archivée, configuration conservée." if deletion_mode == "archive" else "AppBox supprimée et vérifiée."),
                "state": "archived" if deletion_mode == "archive" else "deleted",
                "deletion_mode": deletion_mode,
                "path": str(app_dir),
                "path_exists": path_exists,
                "containers_remaining": remaining,
                "data_preserved": deletion_mode == "archive",
                "executor": executor,
            }

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
            "output": output or state or "Commande exécutée.",
            "state": state,
            "path": str(app_dir),
            "containers": containers,
            "executor": executor,
            "compose_present": compose_path.exists(),
            "manifest_checksum": (manifest or {}).get("checksum"),
            "files_written": ["compose.yml", ".env", "deployment-manifest.json"] if manifest else [],
        }
    raise RuntimeError(f"Commande non supportée : {command_type}")


def command_cycle(config):
    response = api(
        config,
        "GET",
        f"/api/agent/v1/{config['node_id']}/commands",
    )
    command = response.get("command")
    if not command:
        return
    try:
        result = execute_command(config, command)
        if command.get("command_type") == "appbox_action":
            try:
                result["inventory_sync"] = send_inventory(config)
            except Exception as inventory_exc:
                result["inventory_warning"] = str(inventory_exc)
        payload = {"status": "success", "result": result}
    except Exception as exc:
        diagnostics = getattr(exc, "diagnostics", None)
        result = {"diagnostics": _sanitize_diagnostics(diagnostics)} if isinstance(diagnostics, dict) else {}
        payload = {"status": "failed", "error": _sanitize_diagnostic_text(str(exc)), "result": result}
    api(
        config,
        "POST",
        f"/api/agent/v1/{config['node_id']}/commands/{command['command_id']}/result",
        payload,
    )


def main():
    config = json.loads(CONFIG.read_text())
    heartbeat_interval = max(15, int(config.get("heartbeat_interval", 60)))
    inventory_interval = max(10, int(config.get("inventory_interval", 30)))
    command_poll_interval = max(1, int(config.get("command_poll_interval", 2)))

    next_heartbeat = 0.0
    next_inventory = 0.0

    while True:
        now = time.monotonic()
        try:
            # Les commandes sont interrogées indépendamment du heartbeat afin que
            # start/stop/restart/delete soient pris en charge en quelques secondes.
            command_cycle(config)

            now = time.monotonic()
            if now >= next_heartbeat:
                heartbeat(config)
                next_heartbeat = time.monotonic() + heartbeat_interval

            now = time.monotonic()
            if now >= next_inventory:
                send_inventory(config)
                next_inventory = time.monotonic() + inventory_interval
        except urllib.error.HTTPError as exc:
            print(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}", flush=True)
        except Exception as exc:
            print(f"Agent error: {exc}", flush=True)

        time.sleep(command_poll_interval)


if __name__ == "__main__":
    main()
