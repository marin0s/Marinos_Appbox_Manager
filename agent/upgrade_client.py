"""Agent preparation and release-owned systemd scheduling; no agent activation here."""
import base64
import configparser
import errno
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
try:
    from agent.upgrade_contract import MAX_PACKAGE_BYTES, TERMINAL, atomic_file, atomic_json, digest, fsync_directory, prepare_release, validate_package
except ModuleNotFoundError:
    from upgrade_contract import MAX_PACKAGE_BYTES, TERMINAL, atomic_file, atomic_json, digest, fsync_directory, prepare_release, validate_package

ROOT = Path("/opt/marinos-appbox-agent")
SPOOL = Path("/var/lib/marinos-appbox-agent/upgrades")
PROCESS_ID = uuid.uuid4().hex
UPDATER = 'marinos-appbox-updater'
UPDATER_STATE = Path('/var/lib/marinos-appbox-updater')
SYSTEM_UNITS = Path('/etc/systemd/system')


def scheduler_systemctl(*args):
    result = subprocess.run(['systemctl', *args], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('scheduler_systemd_failed')
    return result.stdout.decode().strip()


def scheduler_units(release, root=ROOT):
    """Only known unit directives; no new ZIP entries required by legacy validators."""
    release = Path(release).resolve(strict=True)
    if release.parent != Path(root).resolve() / 'releases':
        raise ValueError('Scheduler outside managed releases')
    script = release / 'upgrade_client.py'
    manifest = json.loads((release / 'agent-manifest.json').read_text())
    if script.is_symlink() or digest(script.read_bytes()) != manifest['files']['upgrade_client.py']:
        raise ValueError('Scheduler checksum mismatch')
    timer = (release / (UPDATER + '.timer')).read_bytes()
    if digest(timer) != manifest['files'][UPDATER + '.timer']:
        raise ValueError('Scheduler timer checksum mismatch')
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string(timer.decode('utf-8'))
    if (parser.defaults() or set(parser.sections()) != {'Unit', 'Timer', 'Install'}
            or set(parser['Unit']) != {'Description'}
            or dict(parser['Install']) != {'WantedBy': 'timers.target'}
            or dict(parser['Timer']) != {'OnBootSec':'15s', 'OnActiveSec':'5s',
                'OnUnitInactiveSec':'5s', 'AccuracySec':'1s', 'Unit':UPDATER + '.service'}):
        raise ValueError('Unsupported adaptive timer')
    command_path = script.as_posix()
    if any(c in command_path for c in ('"', '%', '\n', '\r')):
        raise ValueError('Invalid scheduler path')
    return {
        UPDATER + '.timer': timer,
        UPDATER + '.path': (f'[Unit]\nDescription=Wake Marinos updater on a durable request\n\n'
            f'[Path]\nPathChanged=/var/lib/marinos-appbox-agent/upgrades/request.json\n'
            f'Unit={UPDATER}.service\n\n[Install]\nWantedBy=paths.target\n').encode(),
        UPDATER + '.service.d/30-adaptive-scheduler.conf': (f'[Service]\n'
            f'ExecStopPost=/usr/bin/python3 "{command_path}" schedule\nTimeoutStopSec=90\n').encode(),
    }


def install_scheduler(release, root=ROOT, state=UPDATER_STATE, units=SYSTEM_UNITS,
                      ctl=scheduler_systemctl):
    """After confirmed handoff only. Atomic files + durable rollback/replay journal."""
    root, state, units, release = Path(root), Path(state), Path(units), Path(release).resolve()
    desired = scheduler_units(release, root)
    journal = state / 'scheduler.json'
    record = json.loads(journal.read_text()) if journal.exists() else None
    if record and record['release'] == str(release) and record['phase'] == 'complete':
        return True
    if not record or record['release'] != str(release):
        if record and record['phase'] != 'complete':
            raise RuntimeError('Previous scheduler migration must finish first')
        try:
            path_enabled = ctl('is-enabled', UPDATER + '.path') == 'enabled'
        except RuntimeError:
            path_enabled = False
        backups = {}
        for name in desired:
            path = units / name
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError('Symlink in scheduler units')
            backups[name] = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
        record = {'release':str(release), 'phase':'prepared', 'backups':backups,
                  'path_enabled':path_enabled, 'error_code':None}
        atomic_json(journal, record)
    try:
        if record['phase'] == 'rollback_pending':
            raise RuntimeError('Resume scheduler rollback')
        if record['phase'] != 'activating':
            for name, data in desired.items():
                atomic_file(units / name, data)
            ctl('daemon-reload')  # replay even after a crash immediately after rename
            ctl('enable', UPDATER + '.timer', UPDATER + '.path')
            record.update(phase='activating', activation_deadline=time.time() + 60, error_code=None)
            atomic_json(journal, record)
        # Never wait on units ordered Before the oneshot that is executing us.
        ctl('--no-block', 'start', UPDATER + '.path', UPDATER + '.timer')
        try:
            ready = ctl('is-active', UPDATER + '.path') == 'active'
        except RuntimeError:
            ready = False  # a queued asynchronous activation is not a failure
        if ready:
            record['phase'] = 'complete'
            atomic_json(journal, record)
            return True
        if time.time() >= record['activation_deadline']:
            raise RuntimeError('Scheduler watcher activation timed out')
        return False
    except Exception:
        record.update(phase='rollback_pending', error_code='scheduler_activation_failed')
        atomic_json(journal, record)
        # Do not stop/restart the agent or updater service while recovering units.
        if not record['path_enabled'] and (units / (UPDATER + '.path')).exists():
            ctl('--no-block', 'disable', '--now', UPDATER + '.path')
        for name, encoded in record['backups'].items():
            if name not in desired:
                raise ValueError('Unknown scheduler backup path')
            path = units / name
            if encoded is None:
                path.unlink(missing_ok=True)
                if path.parent.exists():
                    fsync_directory(path.parent)
            else:
                atomic_file(path, base64.b64decode(encoded, validate=True))
        ctl('daemon-reload')
        if record['path_enabled']:
            ctl('--no-block', 'start', UPDATER + '.path')
        ctl('enable', UPDATER + '.timer')
        ctl('--no-block', 'restart', UPDATER + '.timer')
        record['phase'] = 'retry'
        atomic_json(journal, record)
        return False


def scheduler_pending(root=ROOT, state=UPDATER_STATE, spool=SPOOL):
    """Read ABI-1 state without importing/executing the current controller."""
    try:
        if (Path(spool) / 'request.json').exists():
            return True
        journal = Path(state) / 'scheduler.json'
        migration = json.loads(journal.read_text()) if journal.exists() else {}
        if migration.get('phase') != 'complete':
            return True
        capsule = Path(state) / 'state.json'
        if not capsule.exists():
            return False
        current = json.loads(capsule.read_text())
        if current['phase'] not in TERMINAL or not current.get('reported') or current.get('events'):
            return True
        if current['phase'] == 'success':
            if not current.get('controller_handed_off'):
                return True
            controller = (Path(root) / 'controller').resolve()
            if controller == Path(current['candidate']) and controller != Path(migration['release']):
                return True  # one more tick lets the new controller adopt its scheduler
        return False
    except (OSError, ValueError, KeyError, TypeError):
        return True  # damaged/unreadable durable state must never silently disable recovery


def reconcile_scheduler(root=ROOT, state=UPDATER_STATE, spool=SPOOL, units=SYSTEM_UNITS,
                        ctl=scheduler_systemctl):
    journal = Path(state) / 'scheduler.json'
    if journal.exists():
        record = json.loads(journal.read_text())
        if record['phase'] != 'complete':
            install_scheduler(record['release'], root, state, units, ctl)
    active = scheduler_pending(root, state, spool)
    ctl('--no-block', 'start' if active else 'stop', UPDATER + '.timer')
    return active


def scheduler_main():
    if os.name != 'posix' or os.geteuid() != 0:
        raise SystemExit('Linux root required')
    import fcntl
    with (UPDATER_STATE / 'supervisor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            scheduler_systemctl('--no-block', 'start', UPDATER + '.timer')
            return
        try:
            reconcile_scheduler()
        except Exception:
            scheduler_systemctl('--no-block', 'start', UPDATER + '.timer')
            raise SystemExit('Scheduler reconciliation failed; retry timer retained') from None


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


if __name__ == '__main__':
    if sys.argv[1:] == ['schedule']:
        scheduler_main()
    else:
        raise SystemExit('Unsupported upgrade client entrypoint')
