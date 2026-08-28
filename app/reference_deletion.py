"""Central catalogue deletion with atomic DB changes and resumable local cleanup.

No remote command is issued. Files are removed only AFTER the durable deletion
record commits; an I/O failure leaves an explicit pending cleanup, not broken FKs.
"""
import hashlib
import json
import os
import stat
from pathlib import Path

from fastapi import Form, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


def host():
    from app import main
    return main


def init_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS reference_image_deletions (
        deletion_id TEXT PRIMARY KEY, image_id TEXT NOT NULL, name TEXT NOT NULL,
        version_count INTEGER NOT NULL, storage_root TEXT NOT NULL,
        manifest_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'cleanup_pending',
        errors_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
        completed_at TEXT)""")
    # These legacy columns have no foreign keys. Protect late writers which read
    # a version before the deletion transaction acquired its SQLite write lock.
    for table in ('appboxes', 'provisioning_profiles'):
        for operation in ('INSERT', 'UPDATE OF reference_image_id,reference_version_id,snapshot_id'):
            suffix = 'insert' if operation == 'INSERT' else 'update'
            con.execute(f"""CREATE TRIGGER IF NOT EXISTS reference_guard_{table}_{suffix}
                BEFORE {operation} ON {table} BEGIN
                SELECT CASE WHEN NULLIF(NEW.reference_image_id,'') IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM reference_images WHERE image_id=NEW.reference_image_id)
                    THEN RAISE(ABORT,'Reference image no longer exists') END;
                SELECT CASE WHEN NULLIF(NEW.reference_version_id,'') IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM reference_image_versions WHERE version_id=NEW.reference_version_id)
                    THEN RAISE(ABORT,'Reference version no longer exists') END;
                SELECT CASE WHEN EXISTS (SELECT 1 FROM catalog_snapshots
                    WHERE snapshot_id=NEW.snapshot_id AND status='deleted')
                    THEN RAISE(ABORT,'Reference snapshot deleted') END;
                END""")
    # Reusing a human-readable image ID is allowed once cleanup completes. Until
    # then a new publication must not overwrite files scheduled for deletion.
    con.execute("""CREATE TRIGGER IF NOT EXISTS reference_pending_reuse
        BEFORE INSERT ON reference_images WHEN EXISTS (
        SELECT 1 FROM reference_image_deletions WHERE image_id=NEW.image_id AND state!='deleted')
        BEGIN SELECT RAISE(ABORT,'Reference cleanup pending'); END""")


def confined(root, value):
    """Reject traversal, links/junctions, directories and anything outside root."""
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Chemin d’artefact invalide (absolu, sans traversée requis).')
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError('Artefact hors du storage Reference Images autorisé.') from None
    if not relative.parts:
        raise ValueError('Le répertoire de stockage ne peut pas être supprimé.')
    current = root
    for part in ('', *relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Lien symbolique/jonction interdit dans un chemin d’artefact.')
    if path.exists() and not path.is_file():
        raise ValueError('Un artefact doit être un fichier régulier, jamais un répertoire.')
    return path


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def remove_file(root, entry):
    path = confined(root, entry['path'])
    # Linux: hold directory descriptors with O_NOFOLLOW all the way to the file;
    # a concurrent parent rename/symlink cannot redirect unlink outside storage.
    if os.name == 'posix':
        descriptors = []
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            parts = path.relative_to(root).parts
            for part in parts[:-1]:
                descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(descriptor)
            info = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or identity(info) != entry['identity']:
                raise ValueError('Artefact remplacé depuis la confirmation : nettoyage suspendu.')
            os.unlink(parts[-1], dir_fd=descriptor)
            os.fsync(descriptor)
        except FileNotFoundError:
            pass
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    else:
        try:
            if identity(path.stat()) != entry['identity']:
                raise ValueError('Artefact remplacé depuis la confirmation : nettoyage suspendu.')
            path.unlink()
        except FileNotFoundError:
            pass


def _plan(con, image_id):
    main = host()
    image = con.execute('SELECT * FROM reference_images WHERE image_id=?', (image_id,)).fetchone()
    if not image:
        previous = con.execute('SELECT * FROM reference_image_deletions WHERE image_id=? ORDER BY created_at DESC LIMIT 1', (image_id,)).fetchone()
        if not previous:
            raise HTTPException(404, 'Image de référence introuvable.')
        return _result(previous)
    versions = [dict(row) for row in con.execute('SELECT * FROM reference_image_versions WHERE image_id=? ORDER BY version_id', (image_id,))]
    snapshots = sorted({v['snapshot_id'] for v in versions})
    blockers = []
    for table, key, label in (('appboxes', 'client_id', 'AppBox'), ('provisioning_profiles', 'profile_id', 'Profil de provisioning')):
        rows = con.execute(f'''SELECT {key} FROM {table} WHERE reference_image_id=?
            OR reference_version_id IN (SELECT version_id FROM reference_image_versions WHERE image_id=?)
            OR snapshot_id IN (SELECT snapshot_id FROM reference_image_versions WHERE image_id=?)''', (image_id,)*3)
        blockers.extend(f'{label} : {row[0]}' for row in rows)
    for table, key, clause, label in (
        ('control_plane_deployments', 'deployment_id', 'reference_version_id IN (SELECT version_id FROM reference_image_versions WHERE image_id=?)', 'Déploiement'),
        ('snapshot_deployments', 'deployment_id', 'snapshot_id IN (SELECT snapshot_id FROM reference_image_versions WHERE image_id=?)', 'Déploiement snapshot'),
        ('reference_builds', 'build_id', "(image_id=? OR version_id IN (SELECT version_id FROM reference_image_versions WHERE image_id=?)) AND status NOT IN ('published','completed','failed','build_failed','discovery_failed')", 'Build actif'),
        ('reference_image_distribution', 'distribution_id', "version_id IN (SELECT version_id FROM reference_image_versions WHERE image_id=?) AND status='transferring'", 'Distribution active'),
    ):
        rows = con.execute(f'SELECT {key},status FROM {table} WHERE {clause}', (image_id,)*clause.count('?'))
        blockers.extend(f'{label} : {row[0]} ({row[1]})' for row in rows)
    shared = con.execute('''SELECT version_id FROM reference_image_versions WHERE image_id!=?
        AND snapshot_id IN (SELECT snapshot_id FROM reference_image_versions WHERE image_id=?)''', (image_id, image_id))
    blockers.extend(f'Snapshot partagé avec la version : {row[0]}' for row in shared)
    # current_version_id is another historical non-FK column.
    shared = con.execute('''SELECT image_id FROM reference_images WHERE image_id!=? AND current_version_id IN
        (SELECT version_id FROM reference_image_versions WHERE image_id=?)''', (image_id, image_id))
    blockers.extend(f'Version courante d’une autre image : {row[0]}' for row in shared)
    root = Path(os.path.abspath(main.REFERENCE_ROOT))
    files = {}
    for version in versions:
        candidates = [root / 'deployment-cache' / f"{main.slugify_identifier(version['version_id'])}.tar.gz"]
        if version.get('archive_path'):
            archive = Path(version['archive_path'])
            candidates.append(archive)
            snapshot = con.execute('SELECT source_path FROM catalog_snapshots WHERE snapshot_id=?', (version['snapshot_id'],)).fetchone()
            if snapshot and snapshot[0] and Path(snapshot[0]) == archive.parent / 'source':
                candidates.append(archive.parent / 'source' / 'REFERENCE-ARCHIVE.txt')
        for candidate in candidates:
            try:
                path = confined(root, candidate)
                # Preserve other images even if a legacy row shares an archive.
                for other in con.execute("SELECT version_id,archive_path FROM reference_image_versions WHERE image_id!=?", (image_id,)):
                    other_paths = [root / 'deployment-cache' / f"{main.slugify_identifier(other['version_id'])}.tar.gz"]
                    if other['archive_path']:
                        other_paths.append(Path(other['archive_path']))
                    if any(other_path.resolve() == path.resolve() for other_path in other_paths):
                        raise ValueError(f"Artefact partagé avec la version {other['version_id']}.")
                files[str(path)] = {'path': str(path), 'identity': identity(path.stat()) if path.exists() else None}
            except (ValueError, OSError) as exc:
                blockers.append(f"Version {version['version_id']} : {exc}")
    snapshot_records = [dict(row) for row in con.execute('''SELECT * FROM catalog_snapshots WHERE snapshot_id IN
        (SELECT snapshot_id FROM reference_image_versions WHERE image_id=?) ORDER BY snapshot_id''', (image_id,))]
    payload = {'image': dict(image), 'versions': versions, 'snapshots': snapshots,
               'snapshot_records': snapshot_records, 'files': list(files.values())}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {'image_id': image_id, 'name': image['name'], 'version_count': len(versions),
            'confirmation': token, 'blockers': blockers, 'state': 'available',
            'storage_root': str(root), 'manifest': payload, 'errors': []}


def _result(row):
    return {'image_id': row['image_id'], 'name': row['name'], 'version_count': row['version_count'],
            'confirmation': row['deletion_id'], 'state': row['state'], 'blockers': [],
            'errors': json.loads(row['errors_json'])}


def preview(image_id):
    with host().db() as con:
        result = _plan(con, image_id)
    return {k: v for k, v in result.items() if k not in ('manifest', 'storage_root')}


def pending():
    with host().db() as con:
        return [_result(row) for row in con.execute("SELECT * FROM reference_image_deletions WHERE state!='deleted' ORDER BY created_at")]


def delete(image_id, confirmation):
    main = host()
    with main.db_lock, main.immediate_transaction() as con:
        previous = con.execute('SELECT * FROM reference_image_deletions WHERE deletion_id=? AND image_id=?', (confirmation, image_id)).fetchone()
        if not previous:
            plan = _plan(con, image_id)
            if plan['state'] != 'available' or plan['confirmation'] != confirmation:
                raise HTTPException(409, 'La référence a changé : rechargez la confirmation.')
            if plan['blockers']:
                raise HTTPException(409, {'message': 'Suppression refusée.', 'blockers': plan['blockers']})
            manifest = plan['manifest']
            manifest['orphaned_node_cache'] = [dict(row) for row in con.execute('''SELECT * FROM node_reference_cache
                WHERE version_id IN (SELECT version_id FROM reference_image_versions WHERE image_id=?)''', (image_id,))]
            manifest['distributions'] = [dict(row) for row in con.execute('''SELECT * FROM reference_image_distribution
                WHERE version_id IN (SELECT version_id FROM reference_image_versions WHERE image_id=?)''', (image_id,))]
            manifest['build_links'] = [dict(row) for row in con.execute('''SELECT build_id,image_id,version_id FROM reference_builds
                WHERE image_id=? OR version_id IN (SELECT version_id FROM reference_image_versions WHERE image_id=?)''', (image_id, image_id))]
            con.execute('''INSERT INTO reference_image_deletions(deletion_id,image_id,name,version_count,storage_root,manifest_json,created_at)
                VALUES(?,?,?,?,?,?,?)''', (confirmation, image_id, plan['name'], plan['version_count'], plan['storage_root'], json.dumps(manifest), main.now_iso()))
            con.execute('''DELETE FROM node_reference_cache WHERE version_id IN
                (SELECT version_id FROM reference_image_versions WHERE image_id=?)''', (image_id,))
            # Snapshots and build logs remain as audit; no stale source path is
            # advertised as a usable catalogue. External source data is untouched.
            con.execute('''UPDATE catalog_snapshots SET status='deleted',source_path=NULL,updated_at=? WHERE snapshot_id IN
                (SELECT snapshot_id FROM reference_image_versions WHERE image_id=?)''', (main.now_iso(), image_id))
            con.execute('DELETE FROM reference_images WHERE image_id=?', (image_id,))
            con.execute('''INSERT INTO audit_log(actor,action,result,detail,created_at)
                VALUES('admin','reference_image_delete','cleanup_pending',?,?)''', (json.dumps({'image_id': image_id, 'deletion_id': confirmation}), main.now_iso()))
    return cleanup(image_id, confirmation)


def cleanup(image_id, confirmation):
    main = host()
    # Same writer lock as publication: no new DB owner can appear mid-unlink.
    with main.db_lock, main.immediate_transaction() as con:
        row = con.execute('SELECT * FROM reference_image_deletions WHERE deletion_id=? AND image_id=?', (confirmation, image_id)).fetchone()
        if row['state'] == 'deleted':
            return _result(row)
        errors = []
        manifest = json.loads(row['manifest_json'])
        root = Path(row['storage_root'])
        if root != Path(os.path.abspath(main.REFERENCE_ROOT)):
            errors.append('Storage Reference Images modifié : nettoyage suspendu.')
        else:
            for entry in manifest['files']:
                try:
                    for owner in con.execute('SELECT version_id,archive_path FROM reference_image_versions'):
                        paths = [root / 'deployment-cache' / f"{main.slugify_identifier(owner['version_id'])}.tar.gz"]
                        if owner['archive_path']:
                            paths.append(Path(owner['archive_path']))
                        if any(path.resolve() == Path(entry['path']).resolve() for path in paths):
                            raise ValueError('Artefact désormais utilisé par une autre référence.')
                    remove_file(root, entry)
                except (OSError, ValueError) as exc:
                    errors.append(f"{entry['path']} : {exc}")
        state = 'cleanup_pending' if errors else 'deleted'
        con.execute('UPDATE reference_image_deletions SET state=?,errors_json=?,completed_at=? WHERE deletion_id=?',
                    (state, json.dumps(errors), None if errors else main.now_iso(), confirmation))
        result = con.execute('SELECT * FROM reference_image_deletions WHERE deletion_id=?', (confirmation,)).fetchone()
        return _result(result)


class Confirmation(BaseModel):
    confirmation: str


def install_routes(app):
    @app.get('/api/reference-images/{image_id}/deletion')
    def deletion_preview(image_id: str):
        return preview(image_id)

    @app.delete('/api/reference-images/{image_id}')
    def deletion_api(image_id: str, body: Confirmation):
        result = delete(image_id, body.confirmation)
        return JSONResponse(result, status_code=202 if result['state'] == 'cleanup_pending' else 200)

    def page(request, result, status=200, error=None):
        main = host()
        return main.templates.TemplateResponse(request, 'reference_delete.html', {
            'mode': main.APPBOX_MODE, 'hostname': main.HOSTNAME, 'active_page': 'reference_images',
            'deletion': result, 'error': error}, status_code=status)

    @app.get('/reference-images/{image_id}/delete')
    def deletion_page(request: Request, image_id: str):
        return page(request, preview(image_id))

    @app.post('/reference-images/{image_id}/delete')
    def deletion_form(request: Request, image_id: str, confirmation: str = Form(...)):
        try:
            result = delete(image_id, confirmation)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return page(request, preview(image_id), 409, str(exc.detail))
        return page(request, result, 202 if result['state'] == 'cleanup_pending' else 200)
