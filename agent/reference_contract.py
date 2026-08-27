"""Portable reference archive primitives shared by the agent and Control Plane."""
import gzip
import hashlib
import re
import shutil
import sqlite3
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

PLEX_ROOT = 'Library/Application Support/Plex Media Server'
IDENTITY_ATTRIBUTES = (
    'MachineIdentifier', 'ProcessedMachineIdentifier', 'AnonymousMachineIdentifier',
    'PlexOnlineToken', 'PlexOnlineUsername', 'PlexOnlineMail', 'PlexOnlineHome',
    'CertificateUUID', 'PubSubServer', 'PubSubServerRegion',
    'PlexClaim', 'PLEX_CLAIM', 'ClaimToken', 'PlexOnlineAuthToken',
    'LastAutomaticMappedPort',
)


def identity_attribute(name):
    lower = name.lower()
    return lower in {key.lower() for key in IDENTITY_ATTRIBUTES} or any(
        word in lower for word in ('token', 'password', 'secret', 'claim')
    )


def sanitize_preferences(path):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != 'Preferences':
        raise RuntimeError('Preferences.xml invalide.')
    removed = []
    for key in list(root.attrib):
        if identity_attribute(key):
            root.attrib.pop(key)
            removed.append(key)
    tree.write(path, encoding='utf-8', xml_declaration=True)
    return removed


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def redact_result(value):
    """Boundary sanitization before persisting remote results or event messages."""
    if isinstance(value, dict):
        return {str(key): '[REDACTED]' if re.search(r'token|password|secret|authorization|claim_code', str(key), re.I)
                else redact_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_result(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)\bclaim-[A-Za-z0-9_-]{8,}\b', 'claim-[REDACTED]', value)
        value = re.sub(r'(?i)((?:X-Plex-Token|PlexOnlineToken|PLEX_CLAIM|token|password|secret|api[_-]?key)["\x27]?\s*[:=]\s*)[^\s,;&<>]+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(authorization\s*:\s*)\S+(?:\s+\S+)?', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)(https?://)[^/@\s:]+:[^/@\s]+@', r'\1[REDACTED]@', value)
    return value


def archive_member_path(member):
    name = member.name
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or '..' in path.parts or not path.parts
            or not (member.isfile() or member.isdir())):
        raise RuntimeError('Archive invalide : chemin ou type de fichier interdit.')
    return path


def validate_archive(path, plex=False):
    # Consume gzip to EOF as tar readers may otherwise ignore a truncated trailer.
    expanded = 0
    with gzip.open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            expanded += len(block)
            if expanded > 501 * 1024**3:
                raise RuntimeError('Archive de référence trop volumineuse.')
    names = set()
    files = set()
    total = 0
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            relative = archive_member_path(member)
            name = relative.as_posix()
            if name in names or any(parent.as_posix() in files for parent in relative.parents):
                raise RuntimeError('Archive invalide : chemins dupliqués ou conflictuels.')
            names.add(name)
            if member.isfile():
                files.add(name)
                total += member.size
            if len(names) > 2000000 or total > 500 * 1024**3:
                raise RuntimeError('Archive de référence trop volumineuse.')
            if plex:
                root = PurePosixPath(PLEX_ROOT)
                if relative in root.parents or relative == root:
                    if not member.isdir():
                        raise RuntimeError('Archive Plex : racine invalide.')
                    continue
                try:
                    local = relative.relative_to(root)
                except ValueError:
                    raise RuntimeError('Archive Plex : chemin hors contrat.') from None
                allowed = {'Metadata', 'Media', 'Plug-in Support', 'Plug-ins', 'Scanners', 'Profiles', 'Resources', 'Preferences.xml'}
                if local.parts[0] not in allowed:
                    raise RuntimeError('Archive Plex : chemin hors contrat.')
                lower = [part.lower() for part in local.parts]
                if (set(lower) & {'cache','logs','crash reports','codecs','diagnostics','sessions','session','transcode','transcodes','tmp','temp'}
                        or lower[-1].endswith(('.pid','-wal','-shm','-journal','.tmp','.temp','.log','.dmp','.partial','.part','.swp','.lock','~'))
                        or lower[-1].startswith(('.transcode','transcode-','transcode_'))
                        or lower[-1] in {'.env', 'credentials.json'}
                        or lower[-1].endswith(('.pem', '.key', '.p12', '.pfx'))):
                    raise RuntimeError('Archive Plex : contenu transitoire ou secret interdit.')
                if local.parts[0] == 'Plug-in Support' and member.isfile():
                    if (len(local.parts) != 3 or local.parts[1] != 'Databases'
                            or not local.name.endswith('.db') or 'backup' in local.name.lower()
                            or local.name[:-3].lower().endswith(('-copy', '_copy'))
                            or re.search(r'20\d{2}[-_.]\d{2}[-_.]\d{2}', local.name)):
                        raise RuntimeError('Archive Plex : base hors contrat.')
                    # Validate database contents, not just a .db filename in a tar.
                    with tempfile.TemporaryDirectory(prefix='archive-sqlite-', dir=Path(path).parent) as directory:
                        database = Path(directory) / 'validation.db'
                        with archive.extractfile(member) as source, database.open('wb') as output:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
                        try:
                            try:
                                check = connection.execute('PRAGMA quick_check').fetchone()
                                if not check or check[0] != 'ok':
                                    raise RuntimeError('Archive Plex : base SQLite incohérente.')
                            except sqlite3.OperationalError as exc:
                                if 'unknown tokenizer' not in str(exc).lower():
                                    raise
                                connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
                        finally:
                            connection.close()
                if name == PLEX_ROOT + '/Preferences.xml':
                    if not member.isfile() or member.size > 1024 * 1024:
                        raise RuntimeError('Preferences.xml invalide.')
                    prefs = ET.fromstring(archive.extractfile(member).read())
                    if prefs.tag != 'Preferences' or any(identity_attribute(key) for key in prefs.attrib):
                        raise RuntimeError('Archive Plex : identité source présente.')
    if not files:
        raise RuntimeError('Archive vide.')
    if plex:
        required = [PLEX_ROOT + '/' + value for value in ('Metadata', 'Media')]
        if any(not any(n == prefix or n.startswith(prefix + '/') for n in names) for prefix in required):
            raise RuntimeError('Archive Plex : Metadata/Media absents.')
        if not {PLEX_ROOT+'/Preferences.xml', PLEX_ROOT+'/Plug-in Support/Databases/com.plexapp.plugins.library.db'} <= files:
            raise RuntimeError('Archive Plex : préférences ou base canonique absentes.')
    return {'file_count': len(files), 'uncompressed_size_bytes': total}


def extract_archive(path, destination):
    validate_archive(path)
    destination = Path(destination).resolve()
    # Preflight every path before writing even into staging.
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            target = destination.joinpath(*archive_member_path(member).parts).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError('Archive invalide : chemin hors staging.')
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            target = destination.joinpath(*archive_member_path(member).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                target.chmod((member.mode & 0o755) | 0o600)
