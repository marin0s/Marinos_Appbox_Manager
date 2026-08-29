"""Persistent Reference Image/version deletion and confined cache cleanup.

The catalogue target is locked before node purges start. Local and remote file
removal is idempotent; partial work remains observable and retryable.
"""
import hashlib
import json
import os
import re
import sqlite3
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
    existing = {row[1] for row in con.execute('PRAGMA table_info(reference_image_deletions)')}
    for column, definition in {
        'target_type': "TEXT NOT NULL DEFAULT 'image'", 'target_version_id': 'TEXT',
        'phase': "TEXT NOT NULL DEFAULT 'preflight'", 'progress': 'INTEGER NOT NULL DEFAULT 0',
        'detail': "TEXT NOT NULL DEFAULT ''", 'error_code': 'TEXT', 'started_at': 'TEXT',
        'operator': "TEXT NOT NULL DEFAULT 'admin'",
    }.items():
        if column not in existing:
            con.execute(f'ALTER TABLE reference_image_deletions ADD COLUMN {column} {definition}')
    con.execute("""CREATE TABLE IF NOT EXISTS reference_image_deletion_nodes (
        deletion_id TEXT NOT NULL, version_id TEXT NOT NULL, node_id TEXT NOT NULL,
        local_path TEXT, checksum TEXT, size_bytes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending', command_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY(deletion_id,version_id,node_id),
        FOREIGN KEY(deletion_id) REFERENCES reference_image_deletions(deletion_id) ON DELETE CASCADE)""")
    con.execute("CREATE INDEX IF NOT EXISTS ix_reference_deletion_nodes_status ON reference_image_deletion_nodes(node_id,status)")
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
    # A deletion first marks versions unavailable under the SQLite writer lock.
    # Late deploy/distribution writers cannot reattach them after preflight.
    for table, column in (
        ('appboxes','reference_version_id'), ('provisioning_profiles','reference_version_id'),
        ('control_plane_deployments','reference_version_id'),
        ('reference_image_distribution','version_id'), ('node_reference_cache','version_id'),
    ):
        for suffix, operation in (('insert', 'INSERT'), ('update', f'UPDATE OF {column}')):
            con.execute(f"""CREATE TRIGGER IF NOT EXISTS reference_deleting_guard_{table}_{suffix}
                BEFORE {operation} ON {table}
                WHEN NULLIF(NEW.{column},'') IS NOT NULL AND EXISTS (
                    SELECT 1 FROM reference_image_versions
                    WHERE version_id=NEW.{column} AND state='deleting')
                BEGIN SELECT RAISE(ABORT,'Reference version deletion in progress'); END""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS reference_deleting_guard_publish
        BEFORE UPDATE OF current_version_id ON reference_images
        WHEN NULLIF(NEW.current_version_id,'') IS NOT NULL AND EXISTS (
            SELECT 1 FROM reference_image_versions
            WHERE version_id=NEW.current_version_id AND state='deleting')
        BEGIN SELECT RAISE(ABORT,'Reference version deletion in progress'); END""")
    con.execute("""CREATE TRIGGER IF NOT EXISTS reference_deleting_guard_new_version
        BEFORE INSERT ON reference_image_versions WHEN EXISTS (
            SELECT 1 FROM reference_image_deletions d WHERE d.image_id=NEW.image_id
            AND d.target_type='image' AND d.state NOT IN ('deleted'))
        BEGIN SELECT RAISE(ABORT,'Reference image deletion in progress'); END""")
    for suffix, operation in (('insert','INSERT'), ('update','UPDATE OF image_id,version_id')):
        con.execute(f'DROP TRIGGER IF EXISTS reference_deleting_guard_build_{suffix}')
        con.execute(f"""CREATE TRIGGER IF NOT EXISTS reference_deleting_guard_build_{suffix}
            BEFORE {operation} ON reference_builds WHEN
            NULLIF(NEW.image_id,'') IS NOT NULL AND NULLIF(NEW.version_id,'') IS NOT NULL AND (
            (EXISTS (SELECT 1 FROM reference_image_versions
                WHERE version_id=NEW.version_id AND state='deleting')) OR
            (EXISTS (SELECT 1 FROM reference_image_deletions
                WHERE image_id=NEW.image_id AND target_type='image' AND state!='deleted')))
            BEGIN SELECT RAISE(ABORT,'Reference deletion in progress'); END""")


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


def _mentions(value, targets):
    if isinstance(value, dict):
        return any(_mentions(item, targets) for item in value.values())
    if isinstance(value, list):
        return any(_mentions(item, targets) for item in value)
    return str(value) in targets


def _plan(con, image_id, version_id=None):
    main = host()
    active = con.execute('''SELECT * FROM reference_image_deletions WHERE image_id=?
        AND target_version_id IS ? AND state!='deleted' ORDER BY created_at DESC LIMIT 1''',
        (image_id,version_id)).fetchone()
    if active:
        return _result(active,con)
    competing = con.execute('''SELECT deletion_id,target_version_id,state
        FROM reference_image_deletions WHERE image_id=? AND state!='deleted'
        ORDER BY created_at LIMIT 1''', (image_id,)).fetchone()
    image = con.execute('SELECT * FROM reference_images WHERE image_id=?', (image_id,)).fetchone()
    if not image:
        previous = con.execute('''SELECT * FROM reference_image_deletions WHERE image_id=?
            AND target_version_id IS ? ORDER BY created_at DESC LIMIT 1''', (image_id, version_id)).fetchone()
        if not previous:
            raise HTTPException(404, 'Image de référence introuvable.')
        return _result(previous)
    versions = [dict(row) for row in con.execute('''SELECT * FROM reference_image_versions
        WHERE image_id=? AND (? IS NULL OR version_id=?) ORDER BY version_id''', (image_id, version_id, version_id))]
    if version_id and not versions:
        previous = con.execute('''SELECT * FROM reference_image_deletions WHERE image_id=?
            AND target_version_id=? ORDER BY created_at DESC LIMIT 1''', (image_id, version_id)).fetchone()
        if previous:
            return _result(previous)
        raise HTTPException(404, 'Version de référence introuvable.')
    snapshots = sorted({v['snapshot_id'] for v in versions})
    version_ids = {v['version_id'] for v in versions}
    blockers = []
    if competing:
        target = competing['target_version_id'] or image_id
        blockers.append(f"Suppression déjà active : {target} ({competing['state']}, {competing['deletion_id']})")
    preserved = []
    if version_id and image['current_version_id'] == version_id:
        blockers.append(f'Version active/default de l’image : {version_id}')
    if not version_id and (image['current_version_id'] or image['status'] == 'published'):
        blockers.append('Image active/publiée : transition explicite requise avant suppression.')
    placeholders = ','.join('?' for _ in version_ids) or "''"
    snapshot_placeholders = ','.join('?' for _ in snapshots) or "''"
    # Existing AppBoxes no longer read the archive: deploy restored it once and
    # recreate uses the existing configuration. They are detached at finalization.
    rows = con.execute(f'''SELECT client_id FROM appboxes WHERE reference_image_id=?
        OR reference_version_id IN ({placeholders}) OR snapshot_id IN ({snapshot_placeholders})''',
        (image_id, *version_ids, *snapshots))
    preserved.extend(f'AppBox autonome (lien catalogue détaché) : {row[0]}' for row in rows)
    rows = con.execute(f'''SELECT profile_id FROM provisioning_profiles WHERE reference_image_id=?
        OR reference_version_id IN ({placeholders}) OR snapshot_id IN ({snapshot_placeholders})''',
        (image_id, *version_ids, *snapshots))
    blockers.extend(f'Profil de provisioning : {row[0]}' for row in rows)
    for table, key, clause, label in (
        ('control_plane_deployments', 'deployment_id', f"reference_version_id IN ({placeholders}) AND status IN ('planned','queued','preparing','prepared','running','restoring','awaiting_claim')", 'Déploiement actif'),
        ('snapshot_deployments', 'deployment_id', f"snapshot_id IN ({snapshot_placeholders}) AND status IN ('planned','queued','running','restoring','restored_unclaimed')", 'Déploiement snapshot actif'),
        ('reference_builds', 'build_id', f"(image_id=? OR version_id IN ({placeholders})) AND status NOT IN ('published','completed','failed','build_failed','discovery_failed')", 'Build actif'),
        ('reference_image_distribution', 'distribution_id', f"version_id IN ({placeholders}) AND status='transferring'", 'Distribution active'),
    ):
        params = ((image_id, *version_ids) if table == 'reference_builds' else
                  tuple(snapshots) if table == 'snapshot_deployments' else tuple(version_ids))
        rows = con.execute(f'SELECT {key},status FROM {table} WHERE {clause}', params)
        blockers.extend(f'{label} : {row[0]} ({row[1]})' for row in rows)
    targets = version_ids | set(snapshots) | {image_id}
    for row in con.execute("SELECT job_id,action,status,options_json FROM jobs WHERE status IN ('queued','running')"):
        try:
            value = json.loads(row['options_json'] or '{}')
        except (ValueError, TypeError):
            value = {}
        if _mentions(value, targets):
            blockers.append(f"Job actif : {row['job_id']} ({row['action']}/{row['status']})")
    for row in con.execute("SELECT command_id,command_type,status,payload_json FROM agent_commands WHERE status IN ('queued','claimed')"):
        try:
            value = json.loads(row['payload_json'] or '{}')
        except (ValueError, TypeError):
            value = {}
        if _mentions(value, targets):
            blockers.append(f"Commande agent active : {row['command_id']} ({row['command_type']}/{row['status']})")
    shared = con.execute(f'''SELECT version_id FROM reference_image_versions WHERE version_id NOT IN ({placeholders})
        AND snapshot_id IN ({snapshot_placeholders})''', (*version_ids, *snapshots))
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
                for other in con.execute(f"SELECT version_id,archive_path FROM reference_image_versions WHERE version_id NOT IN ({placeholders})", tuple(version_ids)):
                    other_paths = [root / 'deployment-cache' / f"{main.slugify_identifier(other['version_id'])}.tar.gz"]
                    if other['archive_path']:
                        other_paths.append(Path(other['archive_path']))
                    if any(other_path.resolve() == path.resolve() for other_path in other_paths):
                        raise ValueError(f"Artefact partagé avec la version {other['version_id']}.")
                metadata = path.stat() if path.exists() else None
                files[str(path)] = {
                    'path': str(path),
                    'identity': identity(metadata) if metadata else None,
                    'size_bytes': metadata.st_size if metadata else 0,
                }
            except (ValueError, OSError) as exc:
                blockers.append(f"Version {version['version_id']} : {exc}")
    snapshot_records = [dict(row) for row in con.execute(
        f'''SELECT * FROM catalog_snapshots WHERE snapshot_id IN ({snapshot_placeholders}) ORDER BY snapshot_id''',
        tuple(snapshots))]
    nodes = [dict(row) for row in con.execute(f'''SELECT c.*,
        COALESCE(d.status,'missing') AS distribution_status FROM node_reference_cache c
        LEFT JOIN reference_image_distribution d ON d.version_id=c.version_id AND d.node_id=c.node_id
        WHERE c.version_id IN ({placeholders}) ORDER BY c.node_id,c.version_id''', tuple(version_ids))]
    payload = {'target_type':'version' if version_id else 'image', 'target_version_id':version_id,
               'image': dict(image), 'versions': versions, 'snapshots': snapshots,
               'snapshot_records': snapshot_records, 'files': list(files.values()),
               'nodes': nodes, 'preserved': preserved}
    token = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return {'image_id': image_id, 'version_id':version_id, 'target_type':payload['target_type'],
            'target_label':f"Version {versions[0]['version']}" if version_id else f"Image {image['name']}",
            'name': image['name'], 'version_count': len(versions),
            'size_bytes':sum(version.get('size_bytes') or 0 for version in versions),
            'node_count':len({item['node_id'] for item in nodes}), 'nodes':nodes,
            'confirmation': token, 'blockers': blockers, 'state': 'available',
            'phase':'preview', 'progress':0, 'detail':'Plan calculé.', 'preserved':preserved,
            'storage_root': str(root), 'manifest': payload, 'files':list(files.values()), 'errors': []}


def _result(row, con=None):
    nodes = [] if con is None else [dict(item) for item in con.execute(
        'SELECT * FROM reference_image_deletion_nodes WHERE deletion_id=? ORDER BY node_id,version_id',
        (row['deletion_id'],))]
    manifest = json.loads(row['manifest_json'])
    return {'operation_id':row['deletion_id'], 'image_id': row['image_id'],
            'version_id':row['target_version_id'], 'target_type':row['target_type'],
            'target_label':manifest.get('target_label') or row['name'],
            'name': row['name'], 'version_count': row['version_count'],
            'size_bytes':sum(version.get('size_bytes') or 0 for version in manifest.get('versions',[])),
            'node_count':len({item['node_id'] for item in nodes}), 'nodes':nodes,
            'files':manifest.get('files',[]), 'preserved':manifest.get('preserved',[]),
            'confirmation': row['deletion_id'], 'state': row['state'], 'blockers': [],
            'phase':row['phase'], 'progress':row['progress'], 'detail':row['detail'],
            'error_code':row['error_code'], 'created_at':row['created_at'],
            'started_at':row['started_at'], 'completed_at':row['completed_at'],
            'errors': json.loads(row['errors_json'])}


def preview(image_id, version_id=None):
    with host().db() as con:
        result = _plan(con, image_id, version_id)
    return {k: v for k, v in result.items() if k not in ('manifest', 'storage_root')}


def pending():
    with host().db() as con:
        return [_result(row, con) for row in con.execute("SELECT * FROM reference_image_deletions WHERE state!='deleted' ORDER BY created_at")]


def operation(deletion_id):
    with host().db() as con:
        row=con.execute('SELECT * FROM reference_image_deletions WHERE deletion_id=?',(deletion_id,)).fetchone()
        if not row: raise HTTPException(404,'Opération de suppression introuvable.')
        return _result(row,con)


def _audit(con,action,result,detail):
    con.execute("INSERT INTO audit_log(actor,action,result,detail,created_at) VALUES('admin',?,?,?,?)",
                (action,result,json.dumps(detail,ensure_ascii=False),host().now_iso()))


def _update(con,deletion_id,state,phase,progress,detail,errors=(),error_code=None,completed_at=None):
    con.execute('''UPDATE reference_image_deletions SET state=?,phase=?,progress=?,detail=?,errors_json=?,
        error_code=?,completed_at=? WHERE deletion_id=?''',(state,phase,progress,str(detail)[:4000],
        json.dumps(list(errors),ensure_ascii=False),error_code,completed_at,deletion_id))


def _schedule(deletion_id,only_node=None):
    main=host(); nodes={item['node_id']:item for item in main.list_control_nodes()}
    with main.db() as con:
        sql="SELECT * FROM reference_image_deletion_nodes WHERE deletion_id=? AND status='pending'"
        params=(deletion_id,)
        if only_node: sql+=' AND node_id=?'; params+=(only_node,)
        tasks=[dict(row) for row in con.execute(sql,params)]
    for task in tasks:
        node=nodes.get(task['node_id'])
        if not node or node.get('status')!='online' or not node.get('agent_online'): continue
        error=None
        if not (node.get('capabilities') or {}).get('reference_cache_delete'):
            error='Agent à mettre à jour : purge de cache non supportée.'
        elif not task.get('local_path') or not re.fullmatch(r'[0-9a-f]{64}',str(task.get('checksum') or '')):
            error='Métadonnées cache insuffisantes pour une purge sûre.'
        if error:
            with main.db_lock,main.db() as con:
                con.execute("""UPDATE reference_image_deletion_nodes SET status='failed',detail=?,updated_at=?
                    WHERE deletion_id=? AND version_id=? AND node_id=? AND status='pending'""",
                    (error,main.now_iso(),deletion_id,task['version_id'],task['node_id']))
            continue
        command_id=main.queue_agent_command(task['node_id'],'reference_cache_delete',{
            'operation_id':deletion_id,'version_id':task['version_id'],'local_path':task['local_path'],
            'checksum':task['checksum'],'size_bytes':task['size_bytes']})
        with main.db_lock,main.db() as con:
            changed=con.execute("""UPDATE reference_image_deletion_nodes SET status='queued',command_id=?,
                attempts=attempts+1,detail='Commande de purge envoyée.',updated_at=? WHERE deletion_id=?
                AND version_id=? AND node_id=? AND status='pending'""",
                (command_id,main.now_iso(),deletion_id,task['version_id'],task['node_id'])).rowcount
            if not changed:
                con.execute("UPDATE agent_commands SET status='failed',completed_at=?,error_text='Purge déjà prise en charge.' WHERE command_id=? AND status='queued'",
                            (main.now_iso(),command_id))


def _finish_catalogue(deletion_id):
    main=host()
    with main.db_lock,main.immediate_transaction() as con:
        row=con.execute('SELECT * FROM reference_image_deletions WHERE deletion_id=?',(deletion_id,)).fetchone()
        if not row or row['phase']=='done': return
        if con.execute("SELECT 1 FROM reference_image_deletion_nodes WHERE deletion_id=? AND status='queued'",(deletion_id,)).fetchone(): return
        manifest=json.loads(row['manifest_json']); root=Path(row['storage_root']); errors=[]
        targets=[item['version_id'] for item in manifest['versions']]; marks=','.join('?' for _ in targets)
        snapshots=manifest.get('snapshots',[]); snapshot_marks=','.join('?' for _ in snapshots)
        # Exercise all FK/delete triggers before touching storage. The savepoint
        # rolls the catalogue mutation back while retaining the durable operation.
        con.execute('SAVEPOINT reference_delete_probe'); probe_error=None
        try:
            con.execute(f'''UPDATE appboxes SET reference_image_id=NULL,reference_version_id=NULL,snapshot_id=NULL,updated_at=?
                WHERE reference_version_id IN ({marks}) OR reference_image_id=? OR snapshot_id IN ({snapshot_marks})''',
                (main.now_iso(),*targets,row['image_id'],*snapshots))
            con.execute(f'''UPDATE control_plane_deployments SET reference_version_id=NULL,updated_at=?
                WHERE reference_version_id IN ({marks})''',(main.now_iso(),*targets))
            con.execute(f'DELETE FROM node_reference_cache WHERE version_id IN ({marks})',tuple(targets))
            if row['target_type']=='image': con.execute('DELETE FROM reference_images WHERE image_id=?',(row['image_id'],))
            else: con.execute('DELETE FROM reference_image_versions WHERE version_id=?',(row['target_version_id'],))
        except sqlite3.Error as exc:
            probe_error=exc
        finally:
            con.execute('ROLLBACK TO reference_delete_probe')
            con.execute('RELEASE reference_delete_probe')
        if probe_error is not None:
            message=f'Finalisation DB refusée : {probe_error}'
            _update(con,deletion_id,'partial','database_finalize_failed',55,
                    'Catalogue conservé ; aucun fichier supprimé.',[message],'database_finalize_failed')
            _audit(con,'reference_deletion_partial','partial',{'operation_id':deletion_id,'errors':[message]})
            return
        if root!=Path(os.path.abspath(main.REFERENCE_ROOT)):
            errors.append('Storage Reference Images modifié : nettoyage suspendu.')
        else:
            for entry in manifest.get('files',[]):
                try:
                    for owner in con.execute("SELECT version_id,archive_path FROM reference_image_versions WHERE state!='deleting'"):
                        paths=[root/'deployment-cache'/f"{main.slugify_identifier(owner['version_id'])}.tar.gz"]
                        if owner['archive_path']: paths.append(Path(owner['archive_path']))
                        if any(path.resolve()==Path(entry['path']).resolve() for path in paths):
                            raise ValueError('Artefact désormais utilisé par une autre référence.')
                    remove_file(root,entry)
                except (OSError,ValueError) as exc: errors.append(f"{entry['path']} : {exc}")
        if errors:
            _update(con,deletion_id,'partial','central_cleanup_failed',65,
                    'Suppression centrale incomplète ; retry requis.',errors,'central_cleanup_failed')
            _audit(con,'reference_deletion_partial','partial',{'operation_id':deletion_id,'errors':errors}); return
        con.execute(f'''UPDATE appboxes SET reference_image_id=NULL,reference_version_id=NULL,snapshot_id=NULL,updated_at=?
            WHERE reference_version_id IN ({marks}) OR reference_image_id=? OR snapshot_id IN ({snapshot_marks})''',
            (main.now_iso(),*targets,row['image_id'],*snapshots))
        con.execute(f'''UPDATE control_plane_deployments SET reference_version_id=NULL,updated_at=?
            WHERE reference_version_id IN ({marks})''',(main.now_iso(),*targets))
        con.execute(f'DELETE FROM node_reference_cache WHERE version_id IN ({marks})',tuple(targets))
        for snapshot in snapshots:
            if not con.execute(f'''SELECT 1 FROM reference_image_versions WHERE snapshot_id=?
                    AND version_id NOT IN ({marks})''',(snapshot,*targets)).fetchone():
                con.execute("UPDATE catalog_snapshots SET status='deleted',source_path=NULL,updated_at=? WHERE snapshot_id=?",
                            (main.now_iso(),snapshot))
        if row['target_type']=='image': con.execute('DELETE FROM reference_images WHERE image_id=?',(row['image_id'],))
        else: con.execute('DELETE FROM reference_image_versions WHERE version_id=?',(row['target_version_id'],))
        tasks=[dict(item) for item in con.execute('SELECT * FROM reference_image_deletion_nodes WHERE deletion_id=?',(deletion_id,))]
        states={item['status'] for item in tasks}
        state='partial' if 'failed' in states else 'purge_pending' if 'pending' in states else 'deleted'
        detail={'deleted':'Catalogue, archive centrale et caches connus supprimés.',
                'purge_pending':'Catalogue et archive supprimés ; cache de node offline en attente.',
                'partial':'Catalogue et archive supprimés ; au moins une purge node a échoué.'}[state]
        _update(con,deletion_id,state,'done' if state=='deleted' else 'remote_cleanup_pending',
                100 if state=='deleted' else 90,detail,[item['detail'] for item in tasks if item['status']=='failed'],
                'remote_cleanup_failed' if state=='partial' else None,main.now_iso() if state=='deleted' else None)
        _audit(con,'reference_image_delete' if row['target_type']=='image' else 'reference_version_delete',state,
               {'operation_id':deletion_id,'image_id':row['image_id'],'version_id':row['target_version_id'],
                'size_bytes':sum(v.get('size_bytes') or 0 for v in manifest['versions']),
                'nodes':sorted({item['node_id'] for item in tasks})})


def delete(image_id,confirmation,version_id=None,confirmed_name=None):
    main=host(); refusal=None
    with main.db_lock,main.immediate_transaction() as con:
        previous=con.execute('SELECT * FROM reference_image_deletions WHERE deletion_id=?',(confirmation,)).fetchone()
        if previous:
            if previous['image_id']!=image_id or previous['target_version_id']!=version_id:
                raise HTTPException(409,'Confirmation liée à une autre cible.')
            if previous['state'] in {'partial','purge_pending'}:
                con.execute("""UPDATE reference_image_deletion_nodes SET status='pending',command_id=NULL,
                    detail='Retry demandé.',updated_at=? WHERE deletion_id=? AND status='failed'""",(main.now_iso(),confirmation))
                for task in con.execute("SELECT command_id FROM reference_image_deletion_nodes WHERE deletion_id=? AND status='queued'",(confirmation,)):
                    command=con.execute('SELECT status FROM agent_commands WHERE command_id=?',(task[0],)).fetchone()
                    if not command or command[0]=='failed':
                        con.execute("UPDATE reference_image_deletion_nodes SET status='pending',command_id=NULL,updated_at=? WHERE deletion_id=? AND command_id=?",
                                    (main.now_iso(),confirmation,task[0]))
        else:
            plan=_plan(con,image_id,version_id)
            if plan['state']!='available' or plan['confirmation']!=confirmation:
                raise HTTPException(409,'La référence a changé : rechargez la confirmation.')
            if not version_id and confirmed_name!=plan['name']:
                raise HTTPException(400,'Confirmation renforcée requise : saisissez exactement le nom de l’image.')
            if plan['blockers']: refusal=plan
            else:
                manifest=plan['manifest']; manifest['target_label']=plan['target_label']; stamp=main.now_iso()
                con.execute('''INSERT INTO reference_image_deletions(
                    deletion_id,image_id,name,version_count,storage_root,manifest_json,state,errors_json,created_at,
                    target_type,target_version_id,phase,progress,detail,started_at,operator)
                    VALUES(?,?,?,?,?,?,'running','[]',?,?,?,?,30,?,?,'admin')''',
                    (confirmation,image_id,plan['name'],plan['version_count'],plan['storage_root'],json.dumps(manifest),
                     stamp,plan['target_type'],version_id,'purge_nodes','Suppression verrouillée ; purge des caches en préparation.',stamp))
                for version in manifest['versions']:
                    con.execute("UPDATE reference_image_versions SET state='deleting' WHERE version_id=?",(version['version_id'],))
                for task in manifest['nodes']:
                    con.execute('''INSERT INTO reference_image_deletion_nodes(
                        deletion_id,version_id,node_id,local_path,checksum,size_bytes,status,detail,updated_at)
                        VALUES(?,?,?,?,?,?,'pending','Node à contacter.',?)''',
                        (confirmation,task['version_id'],task['node_id'],task['local_path'],task['checksum'],task['size_bytes'],stamp))
                _audit(con,'reference_deletion_started','running',{'operation_id':confirmation,'target':plan['target_label']})
    if refusal:
        with main.db_lock,main.db() as con:
            _audit(con,'reference_deletion_refused','refused',{'image_id':image_id,'version_id':version_id,'blockers':refusal['blockers']})
        raise HTTPException(409,{'message':'Suppression refusée.','blockers':refusal['blockers']})
    _schedule(confirmation); _finish_catalogue(confirmation)
    return operation(confirmation)


def cleanup(image_id,confirmation):
    return delete(image_id,confirmation)


def finalize_remote_command(command,status,result,error):
    if command['command_type']!='reference_cache_delete': return
    try: payload=json.loads(command['payload_json'] or '{}')
    except (ValueError,TypeError): return
    deletion_id,version_id=payload.get('operation_id'),payload.get('version_id')
    if not deletion_id or not version_id: return
    final='success' if status=='success' and result.get('cache_absent') is True else 'failed'
    detail=result.get('output') or error or 'Purge distante non confirmée.'; main=host()
    with main.db_lock,main.db() as con:
        con.execute('''UPDATE reference_image_deletion_nodes SET status=?,detail=?,updated_at=? WHERE deletion_id=?
            AND version_id=? AND node_id=? AND command_id=? AND status='queued' ''',(final,str(detail)[:2000],main.now_iso(),
            deletion_id,version_id,command['node_id'],command['command_id']))
    _finish_catalogue(deletion_id)


def reconcile_node(node_id):
    main=host()
    with main.db_lock,main.db() as con:
        deletions=[row[0] for row in con.execute("SELECT DISTINCT deletion_id FROM reference_image_deletion_nodes WHERE node_id=? AND status='pending'",(node_id,))]
    for deletion_id in deletions: _schedule(deletion_id,node_id); _finish_catalogue(deletion_id)


class Confirmation(BaseModel):
    confirmation: str
    confirmed_name: str | None = None


def install_routes(app):
    @app.get('/api/reference-images/{image_id}/deletion')
    def deletion_preview(image_id: str):
        return preview(image_id)

    @app.get('/api/reference-images/{image_id}/versions/{version_id}/deletion')
    def version_deletion_preview(image_id: str, version_id: str):
        return preview(image_id, version_id)

    @app.get('/api/reference-deletions/{deletion_id}')
    def deletion_operation(deletion_id: str):
        return operation(deletion_id)

    @app.delete('/api/reference-images/{image_id}')
    def deletion_api(image_id: str, body: Confirmation):
        result = delete(image_id, body.confirmation, confirmed_name=body.confirmed_name)
        return JSONResponse(result, status_code=200 if result['state'] == 'deleted' else 202)

    @app.delete('/api/reference-images/{image_id}/versions/{version_id}')
    def version_deletion_api(image_id: str, version_id: str, body: Confirmation):
        result = delete(image_id, body.confirmation, version_id)
        return JSONResponse(result, status_code=200 if result['state'] == 'deleted' else 202)

    def page(request, result, status=200, error=None):
        main = host()
        return main.templates.TemplateResponse(request, 'reference_delete.html', {
            'mode': main.APPBOX_MODE, 'hostname': main.HOSTNAME, 'active_page': 'reference_images',
            'deletion': result, 'error': error}, status_code=status)

    @app.get('/reference-images/{image_id}/delete')
    def deletion_page(request: Request, image_id: str):
        return page(request, preview(image_id))

    @app.get('/reference-images/{image_id}/versions/{version_id}/delete')
    def version_deletion_page(request: Request, image_id: str, version_id: str):
        return page(request, preview(image_id, version_id))

    @app.post('/reference-images/{image_id}/delete')
    def deletion_form(request: Request, image_id: str, confirmation: str = Form(...), confirmed_name: str = Form('')):
        try:
            result = delete(image_id, confirmation, confirmed_name=confirmed_name)
        except HTTPException as exc:
            if exc.status_code not in {400, 409}:
                raise
            return page(request, preview(image_id), exc.status_code, str(exc.detail))
        return page(request, result, 200 if result['state'] == 'deleted' else 202)

    @app.post('/reference-images/{image_id}/versions/{version_id}/delete')
    def version_deletion_form(request: Request, image_id: str, version_id: str, confirmation: str = Form(...)):
        try:
            result = delete(image_id, confirmation, version_id)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            return page(request, preview(image_id, version_id), 409, str(exc.detail))
        return page(request, result, 200 if result['state'] == 'deleted' else 202)
