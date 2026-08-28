#!/usr/bin/env python3
"""Stable dispatch ABI 1. No package, network, unit-install or upgrade policy here."""
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path('/opt/marinos-appbox-agent')
STATE = Path('/var/lib/marinos-appbox-updater')
ENTRY = ("import runpy,sys; sys.path.insert(0,sys.argv[1]); "
         "sys.argv=[sys.argv[2],sys.argv[3]]; runpy.run_path(sys.argv[0],run_name='__main__')")


def resolve_helper(root, pointer):
    release = (root / pointer).resolve(strict=True)
    if release.parent != root.resolve() / 'releases':
        raise ValueError('Unmanaged controller')
    script = release / 'upgrade_helper.py'
    if script.is_symlink() or not script.is_file():
        raise ValueError('Missing controller')
    return script


def invoke(script, action):
    # Bounded child, fixed entrypoints only; never disclose a helper's output.
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', ENTRY,
                                str(script.parent), str(script), action],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=os.name == 'posix')
    try:
        return process.wait(timeout=150) == 0
    except subprocess.TimeoutExpired:
        # Stop the entire helper process group before invoking recovery.
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()
        return False



def dispatch(root=ROOT, run=invoke):
    root = Path(root)
    try:
        if run(resolve_helper(root, 'controller'), 'tick'):
            return True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    # rescue always points to a previously usable controller, never an untested candidate.
    try:
        return run(resolve_helper(root, 'rescue'), 'recover')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def main():
    if os.name != 'posix' or os.geteuid() != 0:
        raise SystemExit('Linux root required')
    import fcntl
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # FD lock belongs to the launcher, surviving controller crashes/timeouts.
        if not dispatch():
            raise SystemExit('Upgrade dispatch failed; inspect durable state')


if __name__ == '__main__':
    main()
