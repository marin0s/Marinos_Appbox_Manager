#!/usr/bin/env python3
"""Release-owned upgrade supervisor. The previous controller owns activation and recovery."""
import argparse
import ast
import base64
import sys
import json
import os
import stat
import subprocess
import time
from pathlib import Path
try:
    from agent.upgrade_contract import (HELPER_FILES, TERMINAL, atomic_json, canonical,
        digest, fsync_directory, prepare_release, source_version, version_key, validate_package, atomic_file, LAUNCHER_ABI)
    from agent.upgrade_client import operation_path, request, install_scheduler, scheduler_units
except ModuleNotFoundError:
    from upgrade_contract import (HELPER_FILES, TERMINAL, atomic_json, canonical,
        digest, fsync_directory, prepare_release, source_version, version_key, validate_package, atomic_file, LAUNCHER_ABI)
    from upgrade_client import operation_path, request, install_scheduler, scheduler_units

ROOT = Path("/opt/marinos-appbox-agent")
STATE = Path("/var/lib/marinos-appbox-updater")
SPOOL = Path("/var/lib/marinos-appbox-agent/upgrades")
CONFIG = Path("/etc/marinos-appbox-agent/agent.json")
SERVICE = "marinos-appbox-agent.service"
UNITS = Path("/etc/systemd/system")
ENTRY = ("import runpy,sys; sys.path.insert(0,sys.argv[1]); "
         "sys.argv=[sys.argv[2],sys.argv[3]]; runpy.run_path(sys.argv[0],run_name='__main__')")


def probe_helper(release):
    """Exercise the candidate's fixed readiness entrypoint, never install hooks."""
    release = Path(release)
    def unprivileged():
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    try:
        result = subprocess.run([sys.executable, '-I', '-B', '-c', ENTRY,
            str(release), str(release / 'upgrade_helper.py'), 'probe'],
            capture_output=True, timeout=10, check=False,
            preexec_fn=unprivileged if os.name == 'posix' and os.geteuid() == 0 else None)
        proof = json.loads(result.stdout) if len(result.stdout) < 1024 else {}
        return (result.returncode == 0 and proof.get('launcher_abi') == LAUNCHER_ABI
                and proof.get('helper_sha256') == digest((release / 'upgrade_helper.py').read_bytes()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False



def systemctl(*args):
    result = subprocess.run(["systemctl", *args], capture_output=True, timeout=45, check=False)
    if result.returncode:
        raise RuntimeError("systemd_operation_failed")  # never log stdout/stderr/environment
    return result.stdout.decode().strip()


def switch_pointer(root, name, target):
    if name not in {"current", "previous", "controller", "rescue"}:
        raise ValueError("Unknown release pointer")
    root, target = Path(root).resolve(), Path(target).resolve(strict=True)
    if target.parent != root / "releases":
        raise ValueError("Release path outside managed root")
    temporary = root / ("." + name + "-next")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, root / name)
    fsync_directory(root)


def switch_current(root, target):
    switch_pointer(root, "current", target)


def read_legacy(legacy):
    """Read only installed runtime files; missing optional modules are not synthesized."""
    legacy = Path(legacy)
    contents = {}
    for name in ("marinos-appbox-agent.py", "reference_contract.py", "upgrade_client.py", "upgrade_contract.py"):
        path = legacy / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            if name == "marinos-appbox-agent.py":
                raise
            continue
        if not stat.S_ISREG(info.st_mode) or path.resolve().parent != legacy.resolve():
            raise ValueError("Invalid legacy file: " + name)
        data = path.read_bytes()  # permission/I/O errors, including disappearance, are real failures
        compile(data, name, "exec")  # syntax validation only; never import legacy code
        contents[name] = data
    source = contents["marinos-appbox-agent.py"]
    try:
        version = source_version(source)
    except ValueError:
        # alpha.4 monolithic agents used a literal VERSION, without PRODUCT_VERSION.
        declarations = [node.value for node in ast.parse(source).body
                        if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == 'VERSION'
                                for target in node.targets)]
        if (len(declarations) != 1 or not isinstance(declarations[0], ast.Constant)
                or not isinstance(declarations[0].value, str)
                or version_key(declarations[0].value) is None):
            raise ValueError("Unsupported legacy agent version declaration") from None
        version = declarations[0].value
    return contents, version


def snapshot_legacy(root, legacy):
    root = Path(root)
    contents, version = read_legacy(legacy)
    key = digest(canonical({name: digest(data) for name, data in contents.items()}))
    target = root / "releases" / ("legacy-" + key)
    if target.is_symlink():
        raise ValueError("Invalid legacy backup directory")
    target.mkdir(parents=True, exist_ok=True)
    if any(path.name not in {*contents, "release-receipt.json", "managed-agent.service"}
           or path.is_symlink() or not path.is_file() for path in target.iterdir()):
        raise ValueError("Unexpected legacy backup file")
    for name, data in contents.items():
        if (target / name).exists() and (target / name).read_bytes() != data:
            raise ValueError("Legacy backup mismatch")
        with (target / name).open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        (target / name).chmod(0o755 if name.endswith("agent.py") else 0o644)
    atomic_json(target / "release-receipt.json", {"version":version, "build_id":"legacy-" + key, "sha256":None})
    fsync_directory(target)
    return target


class PreparationPending(Exception):
    """The serial agent is still preparing; timer retries without changing current."""


class Supervisor:
    def __init__(self, config, root=ROOT, state=STATE, spool=SPOOL,
                 ctl=systemctl, transport=request, clock=time.time, units=UNITS, controller=None, probe=probe_helper):
        self.config, self.root, self.state_dir, self.spool = config, Path(root), Path(state), Path(spool)
        self.ctl, self.transport, self.clock = ctl, transport, clock
        self.units = Path(units)
        self.controller = Path(controller) if controller else Path(__file__).resolve().parent
        self.probe = probe
        self.state_file = self.state_dir / "state.json"
        self.recovery_file = self.state_dir / "recovery.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def load(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else None

    def save(self, state):
        # Durable ordered outbox: an outage must not lose intermediate phases.
        if state.get("last_enqueued") != state["phase"]:
            state.setdefault("events", []).append({"phase": state["phase"],
                "error_code": state.get("error_code"), "runtime": state.get("confirmed_runtime")})
            state["last_enqueued"] = state["phase"]
        atomic_json(self.state_file, state)
        # Stable recovery envelope, independent of the candidate's state implementation.
        # Its owner remains the old release until this operation is confirmed.
        atomic_json(self.recovery_file, {'abi': 1, 'armed': state['phase'] not in TERMINAL, 'state': state})

    def remote(self, state, suffix="", payload=None):
        return self.transport(self.config, operation_path(self.config, state["operation_id"]) + suffix, payload)

    def publish(self, state):
        try:
            for event in list(state.get("events", [])):
                if event["phase"] == "success":
                    # A reboot after local confirmation may have changed MainPID.
                    # Refresh proof when possible before retrying the CP notification.
                    runtime = self.active_runtime(state)
                    if runtime:
                        event["runtime"] = runtime
                        state["confirmed_runtime"] = runtime
                        self.save(state)
                self.remote(state, "/events", event)
                state["events"].pop(0)
                self.save(state)
            if state["phase"] in TERMINAL:
                state["reported"] = True
                self.save(state)
        except Exception:
            pass  # local rollback never depends on CP availability

    def begin(self, operation_id, archive):
        initial = {"operation_id":operation_id}
        info = self.remote(initial)
        op, runtime = info["operation"], info.get("runtime") or {}
        if (op["node_id"] == self.config["node_id"] and op["phase"] in {"queued", "downloading", "verifying"}
                and self.clock() < op["deadline_epoch"]):
            raise PreparationPending()
        if op["node_id"] != self.config["node_id"] or op["phase"] != "prepared":
            raise RuntimeError("Operation not authorized for activation")
        if self.clock() >= op["deadline_epoch"]:
            raise RuntimeError("Operation expired before activation")
        data = Path(archive).read_bytes()
        manifest, contents = validate_package(data, op["package_sha256"])
        if manifest['launcher_abi'] != LAUNCHER_ABI:
            raise RuntimeError("Unsupported launcher ABI")
        if self.controller.resolve().parent != self.root.resolve() / 'releases':
            raise RuntimeError("Controller must belong to a managed release")
        candidate, _ = prepare_release(self.root / "releases", data, op["package_sha256"])
        previous = (self.root / "current").resolve(strict=True)
        if previous.parent != self.root / "releases":
            raise RuntimeError("Previous release is not managed")
        previous_receipt = json.loads((previous / "release-receipt.json").read_text())
        state = {"operation_id":operation_id, "phase":"installing", "candidate":str(candidate),
                 "previous":str(previous), "version":manifest["version"], "build_id":manifest["build_id"],
                 "package_sha256":op["package_sha256"], "previous_version":previous_receipt["version"],
                 "previous_build":previous_receipt["build_id"], "previous_sha256":previous_receipt.get("sha256"),
                 "previous_unit":base64.b64encode((self.units / SERVICE).read_bytes()).decode('ascii'),
                 "controller":str(self.controller), "before_process":runtime.get("process_id"),
                 "baseline_heartbeat":runtime.get("received_at"), "activation_started":False,
                 "deadline":min(self.clock()+300, op["deadline_epoch"]), "reported":False}
        self.save(state)  # durable recovery record BEFORE stopping or changing anything
        switch_pointer(self.root, 'rescue', self.controller)
        switch_pointer(self.root, 'controller', self.controller)
        switch_pointer(self.root, 'previous', previous)
        return state

    def active_runtime(self, state, rollback=False):
        if self.ctl("is-active", SERVICE) != "active":
            return None
        pid = int(self.ctl("show", SERVICE, "--property=MainPID", "--value"))
        if pid <= 0:
            return None
        runtime = self.remote(state).get("runtime") or {}
        if runtime.get("version") != (state["previous_version"] if rollback else state["version"]):
            return None
        if rollback and state["previous_build"].startswith("legacy-"):
            return runtime if runtime.get("received_at", "") > (state.get("rollback_baseline") or "") else None
        if runtime.get("package_sha256") != (state.get("previous_sha256") if rollback else state["package_sha256"]):
            return None
        expected = state["previous_build"] if rollback else state["build_id"]
        if (runtime.get("build_id") != expected or runtime.get("pid") != pid
                or not runtime.get("process_id") or runtime["process_id"] == state.get("before_process")
                or runtime.get("received_at", "") <= (state.get("rollback_baseline" if rollback else "baseline_heartbeat") or "")):
            return None
        return runtime

    def install_unit(self, data):
        # Repeat reload even if bytes already match: a crash can occur after replace.
        target = self.units / SERVICE
        if not target.is_file() or target.is_symlink() or target.read_bytes() != data:
            atomic_file(target, data)
        self.ctl('daemon-reload')

    def handoff(self, state):
        if state['phase'] == 'success' and state.get('reported') and not state.get('controller_handed_off'):
            switch_pointer(self.root, 'controller', state['candidate'])
            state['controller_handed_off'] = True
            self.save(state)

    def recover(self):
        """Invoked only by the stable launcher using the last known usable controller."""
        envelope = json.loads(self.recovery_file.read_text()) if self.recovery_file.exists() else None
        switch_pointer(self.root, 'controller', self.controller)
        if envelope:
            if envelope['abi'] != 1:
                raise RuntimeError('Unsupported recovery ABI')
            state = envelope['state']
            if envelope['armed'] and state.get('activation_started'):
                if state['phase'] != 'rolling_back':
                    self.rollback(state, 'controller_failed')
                self.step(state)
                return
            # A dispatcher failure after confirmation must not kill a business job.
            # Keep serving future operations using the rescue controller instead.
            state['controller_handed_off'] = True
            self.save(state)
        self.tick()

    def rollback(self, state, error):
        state.update(phase="rolling_back", error_code=error, rollback_deadline=self.clock()+300)
        # Save recovery state before any rollback mutation. A reboot retries this phase.
        try:
            state["rollback_baseline"] = (self.remote(state).get("runtime") or {}).get("received_at")
        except Exception:
            state["rollback_baseline"] = state.get("baseline_heartbeat")
        state["rollback_started"] = False
        self.save(state)
        self.publish(state)

    def step(self, state):
        if state["phase"] in TERMINAL:
            self.publish(state)
            self.handoff(state)
            return
        try:
            if state["phase"] in {"installing", "restarting"}:
                if self.clock() > state["deadline"]:
                    if state.get("activation_started"):
                        self.rollback(state, "confirmation_timeout")
                    else:
                        state.update(phase="upgrade_failed", error_code="installation_failed")
                        self.save(state)
                        self.publish(state)
                    return
                self.publish(state)
                state["activation_started"] = True
                state["phase"] = "restarting"
                self.save(state)
                self.ctl("stop", SERVICE)
                self.install_unit((Path(state["candidate"]) / "managed-agent.service").read_bytes())
                switch_current(self.root, state["candidate"])
                self.publish(state)
                self.ctl("start", SERVICE)
                state["phase"] = "awaiting_heartbeat"
                self.save(state)
                self.publish(state)
            elif state["phase"] == "awaiting_heartbeat":
                runtime = self.active_runtime(state)
                if runtime and self.clock() <= state["deadline"]:
                    if not self.probe(Path(state['candidate'])):
                        self.rollback(state, 'candidate_controller_failed')
                        return
                    if self.clock() > state['deadline']:
                        self.rollback(state, 'confirmation_timeout')
                        return
                    state.update(phase="success", confirmed_runtime=runtime)
                    self.save(state)
                    self.publish(state)
                    self.handoff(state)
                elif self.clock() > state["deadline"]:
                    self.rollback(state, "confirmation_timeout")
            elif state["phase"] == "rolling_back":
                if not state.get("rollback_started"):
                    self.ctl("stop", SERVICE)
                    self.install_unit(base64.b64decode(state["previous_unit"], validate=True))
                    switch_current(self.root, state["previous"])
                    self.ctl("start", SERVICE)
                    state["rollback_started"] = True
                    self.save(state)
                runtime = self.active_runtime(state, rollback=True)
                if runtime:
                    state.update(phase="rolled_back", confirmed_runtime=runtime)
                    self.save(state)
                    self.publish(state)
                elif self.clock() > state["rollback_deadline"]:
                    state.update(phase="rollback_failed", error_code="previous_agent_return_unconfirmed")
                    self.save(state)
                    self.publish(state)
        except Exception:
            if state["phase"] == "rolling_back":
                if self.clock() > state["rollback_deadline"]:
                    state.update(phase="rollback_failed", error_code="rollback_unconfirmed")
                    self.save(state)
                    self.publish(state)
            elif state.get("activation_started"):
                if state["phase"] != "awaiting_heartbeat" or self.clock() > state["deadline"]:
                    self.rollback(state, "activation_or_confirmation_failed")
            else:
                state.update(phase="upgrade_failed", error_code="installation_failed")
                self.save(state)
                self.publish(state)

    def tick(self):
        state = self.load()
        if (state and state['phase'] == 'success' and state.get('reported')
                and state.get('controller_handed_off')
                and Path(state['candidate']) == self.controller
                and (self.root / 'current').resolve() == self.controller):
            try:
                ready = install_scheduler(self.controller, self.root, self.state_dir, self.units, self.ctl)
            except Exception:
                # Unit recovery failure is not a broken controller: retain this
                # confirmed release to replay migration on the next fast tick.
                atomic_json(self.state_dir / 'scheduler-error.json', {'error_code':'scheduler_setup_failed'})
                self.ctl('--no-block', 'start', 'marinos-appbox-updater.timer')
                return
            if not ready:
                return  # finish/recover unit migration before accepting another candidate
            (self.state_dir / 'scheduler-error.json').unlink(missing_ok=True)
        if state and (state["phase"] not in TERMINAL or not state.get("reported")):
            self.step(state)
            return
        if state:
            self.handoff(state)
        pending = self.spool / "request.json"
        if not pending.exists():
            return
        operation_id = json.loads(pending.read_text())["operation_id"]
        operation_path(self.config, operation_id)  # reject traversal before filesystem use
        if state and state["operation_id"] == operation_id:
            pending.unlink()
            return
        try:
            state = self.begin(operation_id, self.spool / operation_id / "agent.zip")
        except PreparationPending:
            return
        except Exception:
            state = {"operation_id":operation_id, "phase":"upgrade_failed", "error_code":"candidate_rejected", "reported":False}
            self.save(state)
            self.publish(state)
            return
        pending.unlink()
        self.step(state)


def bootstrap(archive, checksum, config, root=ROOT, state=STATE, spool=SPOOL,
              legacy=Path("/usr/local/sbin"), units=Path("/etc/systemd/system"),
              ctl=systemctl, transport=request):
    """Operator-only migration. Existing config is read, never rewritten."""
    data = Path(archive).read_bytes()
    _, contents = validate_package(data, checksum)
    root, units = Path(root), Path(units)
    journal = Path(state) / "bootstrap.json"
    override = units / (SERVICE + ".d") / "20-managed-releases.conf"
    if journal.exists():
        saved = json.loads(journal.read_text())
        if saved["node_id"] != config["node_id"]:
            raise RuntimeError("Bootstrap identity mismatch")
        operation_id = saved["operation_id"]
        op = transport(config, operation_path(config, operation_id))["operation"]
        if op["phase"] in {"upgrade_failed", "rolled_back"}:
            current = root / "current"
            if current.exists() and not current.resolve().name.startswith("legacy-"):
                raise RuntimeError("Retry bootstrap only after a verified return to legacy")
            local = Supervisor(config, root, state, spool, ctl, transport).load()
            if local and (local["phase"] not in {"upgrade_failed", "rolled_back"} or not local.get("reported")):
                raise RuntimeError("Wait for the previous helper result before retrying")
            op = transport(config, f"/api/agent/v1/{config['node_id']}/upgrades/bootstrap",
                           {"package_sha256":checksum})["operation"]
            operation_id = op["operation_id"]
            atomic_json(journal, {"operation_id":operation_id, "checksum":checksum,
                                 "node_id":config["node_id"]})
        elif op["phase"] not in {"queued", "downloading", "verifying", "prepared"}:
            raise RuntimeError("Bootstrap already handed off; inspect helper state")
        elif saved["checksum"] != checksum:
            raise RuntimeError("Resume bootstrap with the original artifact")
    else:
        if (root / "current").exists() or (root / "current").is_symlink() or override.exists():
            raise RuntimeError("Managed installation exists; inspect before migration")
        # Preflight before any change to the service or installation.
        read_legacy(legacy)
        op = transport(config, f"/api/agent/v1/{config['node_id']}/upgrades/bootstrap",
                       {"package_sha256":checksum})["operation"]
        operation_id = op["operation_id"]
        atomic_json(journal, {"operation_id":operation_id, "checksum":checksum,
                             "node_id":config["node_id"]})
    for path in (root / "releases", Path(state), Path(spool)):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700 if path in (Path(state), Path(spool)) else 0o755)
    root.chmod(0o755)
    previous = snapshot_legacy(root, legacy)
    if (root / "current").exists() and (root / "current").resolve() != previous.resolve():
        raise RuntimeError("Current changed; bootstrap must not replace it")
    controller, _ = prepare_release(root / 'releases', data, checksum)
    # Preserve the original unit once, then make the legacy snapshot bootable via current.
    original_unit = Path(state) / 'legacy-agent.service'
    if not original_unit.exists():
        atomic_file(original_unit, (units / SERVICE).read_bytes())
    legacy_unit = original_unit.read_text().replace(
        'ExecStart=/usr/local/sbin/marinos-appbox-agent.py',
        'ExecStart=/usr/bin/python3 /opt/marinos-appbox-agent/current/marinos-appbox-agent.py')
    if 'ExecStart=/usr/bin/python3 /opt/marinos-appbox-agent/current/marinos-appbox-agent.py' not in legacy_unit:
        raise RuntimeError('Unsupported legacy service entrypoint')
    # Custom ExecStart drop-ins would override the versioned unit. Fail before activation.
    for dropin in (units / (SERVICE + '.d')).glob('*.conf'):
        if 'ExecStart' in dropin.read_text():
            raise RuntimeError('Resolve custom ExecStart drop-ins during initial bootstrap')
    atomic_file(previous / 'managed-agent.service', legacy_unit.encode('utf-8'))
    switch_current(root, previous)
    switch_pointer(root, 'previous', previous)
    switch_pointer(root, 'controller', controller)
    switch_pointer(root, 'rescue', controller)
    launcher = root / 'upgrade_launcher.py'
    if launcher.exists() and launcher.read_bytes() != contents['upgrade_launcher.py']:
        raise RuntimeError('Stable launcher already installed with different contents')
    if not launcher.exists():
        atomic_file(launcher, contents['upgrade_launcher.py'])
    for name in ('marinos-appbox-updater.service', 'marinos-appbox-updater.timer'):
        atomic_file(units / name, contents[name])
    atomic_file(units / SERVICE, legacy_unit.encode('utf-8'))
    # No service restart until the supervisor has a durable previous/candidate record.
    ctl("daemon-reload")
    ctl("enable", "--now", "marinos-appbox-updater.timer")
    base = operation_path(config, operation_id)
    phases = ["queued", "downloading", "verifying", "prepared"]
    if phases.index(op["phase"]) < 1:
        transport(config, base + "/events", {"phase":"downloading"})
    if phases.index(op["phase"]) < 2:
        transport(config, base + "/events", {"phase":"verifying"})
    work = Path(spool) / operation_id
    work.mkdir(exist_ok=True)
    with (work / "agent.zip").open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(work)
    if op["phase"] != "prepared":
        transport(config, base + "/events", {"phase":"prepared"})
    atomic_json(Path(spool) / "request.json", {"operation_id":operation_id})
    return operation_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("tick", "bootstrap", "recover", "probe"))
    parser.add_argument("--archive")
    parser.add_argument("--sha256")
    args = parser.parse_args()
    if args.action == 'probe':
        release = Path(__file__).resolve().parent
        manifest = json.loads((release / 'agent-manifest.json').read_text())
        if manifest['launcher_abi'] != LAUNCHER_ABI:
            raise RuntimeError('Incompatible dispatcher ABI')
        for name, expected in manifest['files'].items():
            if digest((release / name).read_bytes()) != expected:
                raise RuntimeError('Candidate file mismatch')
        scheduler_units(release, release.parent.parent)
        print(json.dumps({'launcher_abi':LAUNCHER_ABI, 'helper_sha256':digest(Path(__file__).read_bytes())}))
        return
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("Linux root required")
    config = json.loads(CONFIG.read_text())
    if args.action == 'bootstrap':
        import fcntl
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / 'supervisor.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(bootstrap(args.archive, args.sha256, config))
    elif args.action == 'recover':
        Supervisor(config).recover()
    else:
        Supervisor(config).tick()



if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Upgrade supervisor failed; inspect operation state (credentials omitted)") from None
