"""Deterministic, bounded agent artifacts. No package code is executed here."""
import configparser
import hashlib
import io
import json
import os
import re
import stat
import uuid
import zipfile
from pathlib import Path

PROTOCOL = 1
LAUNCHER_ABI = 1
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
FILES = (
    "install-agent.sh", "marinos-appbox-agent.py", "marinos-appbox-agent.service",
    "reference_contract.py", "rdad_refresh.py", "upgrade_contract.py", "upgrade_client.py",
    "upgrade_helper.py", "marinos-appbox-updater.service", "marinos-appbox-updater.timer",
    "upgrade_launcher.py", "managed-agent.service",
)
HELPER_FILES = ("upgrade_helper.py", "upgrade_contract.py", "upgrade_client.py")
MANIFEST = "agent-manifest.json"
TERMINAL = {"success", "upgrade_failed", "rolled_back", "rollback_failed"}
PHASES = ("queued", "downloading", "verifying", "prepared", "installing",
          "restarting", "awaiting_heartbeat", "rolling_back", *sorted(TERMINAL))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def version_key(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?(-dev)?", str(value))
    if not match:
        return None
    major, minor, patch, stage, number, dev = match.groups()
    rank = {"alpha": 0, "beta": 1, "rc": 2}.get(stage, -1 if dev else 3)
    return (int(major), int(minor), int(patch), rank, int(number or 0), 0 if dev else 1)


def update_status(installed, available, installed_build=None, available_build=None):
    old, new = version_key(installed), version_key(available)
    if old is None or new is None:
        return "unknown"
    if old > new:
        return "up_to_date"  # never offer an automatic downgrade
    if old == new and installed_build and installed_build == available_build:
        return "up_to_date"
    return "update_available"


def source_version(source):
    text = source.decode("utf-8").replace("\r\n", "\n")
    product = re.search(r'^PRODUCT_VERSION = "([^"]+)"$', text, re.M)
    if not product or 'VERSION = f"{PRODUCT_VERSION}-dev"' not in text:
        raise ValueError("Unsupported agent version declaration")
    version = product[1] + "-dev"
    if version_key(version) is None:
        raise ValueError("Invalid agent version")
    return version


def manifest_for(contents):
    hashes = {name: digest(data) for name, data in contents.items()}
    return {"protocol": PROTOCOL, "launcher_abi": LAUNCHER_ABI, "version": source_version(contents["marinos-appbox-agent.py"]),
            "build_id": digest(canonical(hashes)), "files": hashes}


def package_bytes(source):
    contents = {}
    for name in FILES:
        data = (Path(source) / name).read_bytes().replace(b"\r\n", b"\n")
        if b"\r" in data:
            raise ValueError("Bare CR in package")
        contents[name] = data
    contents[MANIFEST] = canonical(manifest_for(contents))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in contents.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if name.endswith(".sh") or name == "marinos-appbox-agent.py" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, data)
    return output.getvalue()


def validate_package(data, expected_sha):
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha)) or digest(data) != expected_sha:
        raise ValueError("Agent package checksum mismatch")
    if not 0 < len(data) <= MAX_PACKAGE_BYTES:
        raise ValueError("Agent package size rejected")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        names = [m.filename for m in members]
        if len(names) != len(set(names)) or set(names) != set(FILES) | {MANIFEST}:
            raise ValueError("Agent package file allowlist mismatch")
        if sum(m.file_size for m in members) > MAX_PACKAGE_BYTES:
            raise ValueError("Expanded agent package too large")
        for member in members:
            if member.flag_bits & 1 or stat.S_IFMT(member.external_attr >> 16) != stat.S_IFREG:
                raise ValueError("Agent package file type rejected")
        contents = {m.filename: archive.read(m) for m in members}
    manifest = json.loads(contents.pop(MANIFEST))
    if manifest != manifest_for(contents):
        raise ValueError("Agent manifest inconsistent")
    validate_managed_unit(contents["managed-agent.service"])
    for name, data in contents.items():
        if b"\r" in data:
            raise ValueError("Noncanonical package")
        if name.endswith(".py"):
            compile(data, name, "exec")  # syntax only, never execute downloaded code
    return manifest, contents


def validate_managed_unit(data):
    """A versioned runtime unit, not an unrestricted root script delivery channel."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(data.decode('utf-8'))
    except configparser.Error as exc:
        raise ValueError('Invalid managed unit syntax') from exc
    allowed = {
        'Unit': {'Description', 'After', 'Wants', 'Requires', 'StartLimitIntervalSec', 'StartLimitBurst'},
        'Service': {'Type', 'ExecStart', 'Restart', 'RestartSec', 'User', 'Group',
                    'TimeoutStartSec', 'TimeoutStopSec', 'KillMode', 'WorkingDirectory',
                    'NoNewPrivileges', 'PrivateTmp', 'ProtectHome', 'ProtectSystem',
                    'ReadWritePaths', 'ProtectKernelTunables', 'ProtectKernelModules',
                    'ProtectControlGroups', 'CPUQuota', 'MemoryMax', 'TasksMax', 'UMask'},
        'Install': {'WantedBy'},
    }
    if parser.defaults() or set(parser.sections()) != set(allowed):
        raise ValueError('Invalid managed unit sections')
    for section in parser.sections():
        if set(parser[section]) - allowed[section]:
            raise ValueError('Unsupported managed unit directive')
    required = {'Type':'simple', 'User':'root', 'Restart':'always',
                'ExecStart':'/usr/bin/python3 /opt/marinos-appbox-agent/current/marinos-appbox-agent.py',
                'NoNewPrivileges':'true', 'ProtectSystem':'strict',
                'ReadWritePaths':'/var/lib/marinos-appbox-agent /run /srv/appboxes'}
    if any(parser['Service'].get(k) != v for k,v in required.items()):
        raise ValueError('Managed unit violates execution boundary')
    if parser['Install'].get('WantedBy') != 'multi-user.target':
        raise ValueError('Invalid managed unit target')
    return data


def atomic_file(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fsync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            if os.name != "nt":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_release(root, data, expected_sha):
    manifest, contents = validate_package(data, expected_sha)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / expected_sha
    if target.exists():
        if target.is_symlink() or any((target / name).is_symlink() or
                not (target / name).is_file() or digest((target / name).read_bytes()) != manifest["files"][name]
                for name in FILES):
            raise ValueError("Existing release differs from artifact")
        receipt = target / "release-receipt.json"
        metadata = target / MANIFEST
        if (receipt.is_symlink() or metadata.is_symlink()
                or json.loads(receipt.read_text()) != {"sha256":expected_sha, **manifest}
                or json.loads(metadata.read_text()) != manifest):
            raise ValueError("Existing release metadata differs from artifact")
        return target, manifest
    staging = root / ("." + expected_sha + "-" + uuid.uuid4().hex)
    staging.mkdir(mode=0o755)
    staging.chmod(0o755)
    try:
        for name, content in contents.items():
            path = staging / name
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o755 if name.endswith(".sh") or name == "marinos-appbox-agent.py" else 0o644)
        atomic_json(staging / MANIFEST, manifest)
        atomic_json(staging / "release-receipt.json", {"sha256": expected_sha, **manifest})
        # Runtime metadata is non-secret and readable by the agent's service.
        (staging / MANIFEST).chmod(0o644)
        (staging / "release-receipt.json").chmod(0o644)
        fsync_directory(staging)
        os.replace(staging, target)
        fsync_directory(root)
    finally:
        if staging.exists():
            import shutil
            shutil.rmtree(staging)
    return target, manifest
