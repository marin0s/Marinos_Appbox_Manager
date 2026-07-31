#!/usr/bin/env python3
import json
import hashlib
import re
import secrets
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import http.client
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

VERSION = "1.6.0-alpha.4"
CONFIG = Path("/etc/marinos-appbox-agent/agent.json")


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
    """Fallback transactionally consistent snapshot for non-Plex SQLite databases."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    source_uri = f"file:{urllib.parse.quote(str(source))}?mode=ro"
    source_connection = sqlite3.connect(source_uri, uri=True, timeout=60)
    destination_connection = sqlite3.connect(destination, timeout=60)
    validation = "schema-only"
    try:
        source_connection.execute("PRAGMA busy_timeout=60000")
        destination_connection.execute("PRAGMA journal_mode=DELETE")
        source_connection.backup(destination_connection, pages=2048, sleep=0.05)
        destination_connection.execute("PRAGMA journal_mode=DELETE")
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
    finally:
        destination_connection.close()
        source_connection.close()
    destination.with_name(destination.name + "-wal").unlink(missing_ok=True)
    destination.with_name(destination.name + "-shm").unlink(missing_ok=True)
    return {
        "name": source.name,
        "source_size_bytes": source.stat().st_size,
        "snapshot_size_bytes": destination.stat().st_size,
        "engine": "python-sqlite3",
        "validation": validation,
        "quick_check": "ok" if validation == "quick_check" else validation,
    }


def _plex_sqlite_hot_backup(container_name: str, container_source: Path, host_source: Path, destination: Path) -> dict:
    """Use Plex SQLite inside the running container for backup and validation."""
    plex_sqlite = "/usr/lib/plexmediaserver/Plex SQLite"
    token = uuid.uuid4().hex
    container_snapshot = f"/tmp/appbox-reference-{token}.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    probe_code, _, _ = run(["docker", "exec", container_name, "test", "-x", plex_sqlite], timeout=15)
    if probe_code != 0:
        return _python_sqlite_hot_backup(host_source, destination)

    try:
        backup_code, backup_out, backup_err = run([
            "docker", "exec", container_name, plex_sqlite,
            str(container_source), ".timeout 60000", f".backup '{container_snapshot}'",
        ], timeout=900)
        if backup_code != 0:
            raise RuntimeError(f"Plex SQLite .backup a échoué pour {host_source.name}: {(backup_err or backup_out).strip()}")

        check_code, check_out, check_err = run([
            "docker", "exec", container_name, plex_sqlite,
            container_snapshot, "PRAGMA quick_check;",
        ], timeout=900)
        checks = [line.strip().lower() for line in check_out.splitlines() if line.strip()]
        if check_code != 0 or checks != ["ok"]:
            raise RuntimeError(f"Plex SQLite quick_check a échoué pour {host_source.name}: {(check_err or check_out).strip()}")

        copy_code, copy_out, copy_err = run([
            "docker", "cp", f"{container_name}:{container_snapshot}", str(destination),
        ], timeout=900)
        if copy_code != 0 or not destination.exists():
            raise RuntimeError(f"Impossible de récupérer le snapshot SQLite {host_source.name}: {(copy_err or copy_out).strip()}")
    finally:
        run(["docker", "exec", container_name, "rm", "-f", container_snapshot, container_snapshot + "-wal", container_snapshot + "-shm"], timeout=30)

    destination.with_name(destination.name + "-wal").unlink(missing_ok=True)
    destination.with_name(destination.name + "-shm").unlink(missing_ok=True)
    return {
        "name": host_source.name,
        "source_size_bytes": host_source.stat().st_size,
        "snapshot_size_bytes": destination.stat().st_size,
        "engine": "plex-sqlite",
        "engine_path": plex_sqlite,
        "validation": "quick_check",
        "quick_check": "ok",
    }


def _prepare_plex_reference_overlay(config_path: Path, workdir: Path, container_name: str = "") -> tuple[Path, dict]:
    plex_rel = Path("Library/Application Support/Plex Media Server")
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
        for key in (
            "MachineIdentifier", "ProcessedMachineIdentifier",
            "AnonymousMachineIdentifier", "PlexOnlineToken",
            "PlexOnlineUsername", "PlexOnlineMail", "PlexOnlineHome",
            "CertificateUUID", "PubSubServer", "PubSubServerRegion",
        ):
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
            if item.is_file() and item.name.endswith(".db"):
                container_database = Path("/config") / item.relative_to(config_path)
                snapshots.append(_plex_sqlite_hot_backup(container_name, container_database, item, target_databases / item.name) if container_name else _python_sqlite_hot_backup(item, target_databases / item.name))
            elif item.is_file() and not item.name.endswith((".db-wal", ".db-shm")):
                shutil.copy2(item, target_databases / item.name)
                copied_auxiliary.append(item.name)

    engines = sorted({snapshot.get("engine", "unknown") for snapshot in snapshots})
    return overlay, {
        "identity_attributes_removed": removed,
        "sqlite_snapshots": snapshots,
        "database_auxiliary_files": copied_auxiliary,
        "sqlite_strategy": "+".join(engines) if engines else "no-database",
    }


def _archive_plex_reference(config_path: Path, overlay: Path, archive: Path) -> dict:
    plex_prefix = "Library/Application Support/Plex Media Server"
    excluded_prefixes = {
        f"{plex_prefix}/Cache",
        f"{plex_prefix}/Logs",
        f"{plex_prefix}/Crash Reports",
        f"{plex_prefix}/Codecs",
        f"{plex_prefix}/Metadata",
        f"{plex_prefix}/Media",
        f"{plex_prefix}/Plug-in Support/Databases",
    }
    replaced_paths = {
        f"{plex_prefix}/Preferences.xml",
    }
    excluded_count = 0

    def archive_filter(info: tarfile.TarInfo):
        nonlocal excluded_count
        normalized = info.name.strip("./")
        if normalized in replaced_paths:
            excluded_count += 1
            return None
        if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in excluded_prefixes):
            excluded_count += 1
            return None
        basename = Path(normalized).name
        if basename.endswith(".pid") or basename.startswith(".transcode"):
            excluded_count += 1
            return None
        return info

    with tarfile.open(archive, "w:gz", compresslevel=3, dereference=False) as tar:
        for item in sorted(config_path.iterdir(), key=lambda value: value.name):
            tar.add(item, arcname=item.name, recursive=True, filter=archive_filter)
        for item in sorted(overlay.iterdir(), key=lambda value: value.name):
            tar.add(item, arcname=item.name, recursive=True)
    return {"excluded_archive_entries": excluded_count}


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

    # The archive is built directly from the source plus a small sanitized overlay.
    # This avoids duplicating a 35+ GiB Plex configuration on the source node.
    with tempfile.TemporaryDirectory(prefix="appbox-reference-build-") as tempdir:
        workdir = Path(tempdir)
        container_name = str((discovery.get("instance") or {}).get("container_name") or payload.get("source_instance") or "")
        if not container_name:
            raise RuntimeError("Nom du conteneur Plex source introuvable.")
        overlay, sanitization = _prepare_plex_reference_overlay(config_path, workdir, container_name)
        archive = workdir / "reference.tar.gz"
        archive_report = _archive_plex_reference(config_path, overlay, archive)

        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        checksum = digest.hexdigest()

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

        sanitization.update(archive_report)
        sanitization.update({
            "excluded_directories": ["Cache", "Logs", "Crash Reports", "Codecs"],
            "source_unchanged": True,
            "plex_was_stopped": False,
            "full_staging_copy_created": False,
        })
        return {
            "archive_path": stored.get("archive_path"),
            "sha256": checksum,
            "compressed_size_bytes": archive.stat().st_size,
            "uncompressed_size_bytes": int((preflight.get("estimated_payload_bytes") or 0)),
            "sanitization": sanitization,
            "discovery": discovery,
        }


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
        payload = {"status": "failed", "error": str(exc), "result": {}}
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
