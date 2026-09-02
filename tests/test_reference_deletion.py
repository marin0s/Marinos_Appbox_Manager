import json
import importlib.util
import sqlite3
import stat
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main, reference_deletion as deletion

AGENT_PATH=Path(__file__).parents[1]/'agent'/'marinos-appbox-agent.py'
spec=importlib.util.spec_from_file_location('reference_deletion_agent',AGENT_PATH)
agent=importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_FILE', tmp_path / 'catalogue.db')
    monkeypatch.setattr(main, 'REFERENCE_ROOT', tmp_path / 'references')
    main.REFERENCE_ROOT.mkdir()
    main.init_database()
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO reference_images(image_id,name,media_type,created_at,updated_at) VALUES('image','Plex Test','plex',?,?)", (stamp, stamp))
    def version(number):
        archive = main.REFERENCE_ROOT / 'builds' / str(number) / 'reference.tar.gz'
        source = archive.parent / 'source'
        source.mkdir(parents=True)
        archive.write_bytes(b'archive')
        (source / 'REFERENCE-ARCHIVE.txt').write_text(str(archive), encoding='utf-8')
        cache = main.REFERENCE_ROOT / 'deployment-cache' / f'v{number}.tar.gz'
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(b'cached')
        with main.db() as con:
            con.execute("INSERT INTO catalog_snapshots(snapshot_id,name,media_type,source_path,status,created_at,updated_at) VALUES(?,'Test','plex',?,'ready',?,?)", (f's{number}', str(source), stamp, stamp))
            con.execute("INSERT INTO reference_image_versions(version_id,image_id,version,snapshot_id,archive_path,state,created_at) VALUES(?,'image',?,?,?,'published',?)", (f'v{number}', str(number), f's{number}', str(archive), stamp))
        return archive, cache, source / 'REFERENCE-ARCHIVE.txt'
    return version


def erase():
    return deletion.delete('image', deletion.preview('image')['confirmation'], confirmed_name='Plex Test')


def appbox(con, **fields):
    stamp = main.now_iso()
    columns = ','.join(fields)
    placeholders = ','.join('?' for _ in fields)
    con.execute(f"INSERT INTO appboxes(client_id,node_id,path,containers_json,created_at,updated_at,{columns}) VALUES('ab-test',?,'/unused','[]',?,?,{placeholders})", (main.HOSTNAME, stamp, stamp, *fields.values()))


def test_empty_image_and_idempotent_delete(catalogue):
    client = TestClient(main.app)
    preview = client.get('/api/reference-images/image/deletion').json()
    assert preview['name'] == 'Plex Test' and preview['version_count'] == 0
    assert client.delete('/api/reference-images/image').status_code == 422
    for _ in range(2):
        response = client.request('DELETE', '/api/reference-images/image', json={'confirmation': preview['confirmation'], 'confirmed_name':'Plex Test'})
        assert response.status_code == 200 and response.json()['state'] == 'deleted'
    assert client.get('/api/reference-images/missing/deletion').status_code == 404
    with main.db() as con:
        assert con.execute('SELECT COUNT(*) FROM audit_log WHERE action="reference_image_delete"').fetchone()[0] == 1


def test_multiple_versions_artifacts_cache_and_audit(catalogue, monkeypatch):
    files = [*catalogue(1), *catalogue(2)]
    stamp = main.now_iso()
    build = main.create_reference_build_draft(source_node_id=main.HOSTNAME, display_name='Plex Test')
    with main.db() as con:
        con.execute("UPDATE reference_builds SET image_id='image',version_id='v1',status='published' WHERE build_id=?", (build,))
        con.execute("INSERT INTO reference_image_distribution(version_id,node_id,status,updated_at) VALUES('v1',?,'ready',?)", (main.HOSTNAME, stamp))
    assert erase()['state'] == 'deleted'
    assert not any(file.exists() for file in files)
    with main.db() as con:
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
        assert con.execute('SELECT COUNT(*) FROM reference_image_versions').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM node_reference_cache').fetchone()[0] == 0
        row = con.execute('SELECT image_id,version_id,status FROM reference_builds WHERE build_id=?', (build,)).fetchone()
        assert tuple(row) == (None, None, 'published')
        assert con.execute('SELECT COUNT(*) FROM reference_build_logs WHERE build_id=?', (build,)).fetchone()[0] > 0
        manifest = json.loads(con.execute('SELECT manifest_json FROM reference_image_deletions').fetchone()[0])
        assert len(manifest['versions']) == 2


@pytest.mark.parametrize('fields', [{'reference_image_id': 'image'}, {'reference_version_id': 'v1'}, {'snapshot_id': 's1'}])
def test_appbox_dependency_blocks_catalogue_deletion(catalogue, fields):
    catalogue(1)
    with main.db() as con:
        appbox(con, **fields)
    preview = deletion.preview('image')
    assert any('AppBox dépendante : ab-test' in item for item in preview['blockers'])
    response = TestClient(main.app).get('/reference-images/image/delete')
    assert response.status_code == 200
    assert 'Plex Test' in response.text and '1 version(s)' in response.text
    assert 'AppBox dépendante' in response.text
    assert 'type="submit"' not in response.text


@pytest.mark.parametrize('status,blocked', [('planned',True),('running',True),('success',False),('failed',False)])
def test_only_active_deployment_references_block(catalogue, status, blocked):
    catalogue(1)
    with main.db() as con:
        con.execute("INSERT INTO control_plane_deployments(deployment_id,reference_version_id,status,created_at,updated_at) VALUES('dep','v1',?,?,?)", (status, main.now_iso(), main.now_iso()))
    preview=deletion.preview('image')
    assert any('Déploiement actif' in item for item in preview['blockers']) is blocked
    if blocked:
        with pytest.raises(HTTPException): erase()
    else:
        assert erase()['state']=='deleted'


def test_profile_and_active_distribution_block(catalogue):
    catalogue(1)
    with main.db() as con:
        con.execute("UPDATE provisioning_profiles SET reference_version_id='v1' WHERE profile_id='plex-blank'")
        con.execute("INSERT INTO reference_image_distribution(version_id,node_id,status,updated_at) VALUES('v1',?,'transferring',?)", (main.HOSTNAME, main.now_iso()))
    blockers = deletion.preview('image')['blockers']
    assert any('Profil de provisioning' in item for item in blockers)
    assert any('Distribution active' in item for item in blockers)


def test_missing_archive_is_success(catalogue):
    archive, _, _ = catalogue(1)
    archive.unlink()
    assert erase()['state'] == 'deleted'


@pytest.mark.parametrize('kind', ['outside', 'traversal', 'directory', 'relative'])
def test_bad_path_refused_before_any_change(catalogue, kind, tmp_path):
    archive, _, _ = catalogue(1)
    bad = {'outside': tmp_path / 'outside.tar.gz', 'traversal': main.REFERENCE_ROOT / 'builds' / '..' / 'outside.tar.gz',
           'directory': archive.parent, 'relative': Path('builds/reference.tar.gz')}[kind]
    with main.db() as con:
        con.execute("UPDATE reference_image_versions SET archive_path=? WHERE version_id='v1'", (str(bad),))
    assert deletion.preview('image')['blockers']
    with pytest.raises(HTTPException):
        erase()
    with main.db() as con:
        assert con.execute('SELECT COUNT(*) FROM reference_images').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM reference_image_deletions').fetchone()[0] == 0
    assert archive.exists()


def test_database_failure_rolls_back_before_unlink(catalogue):
    files = catalogue(1)
    with main.db() as con:
        con.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON reference_images BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    assert erase()['state']=='partial'
    assert all(path.exists() for path in files)
    with main.db() as con:
        assert con.execute('SELECT COUNT(*) FROM reference_image_versions').fetchone()[0] == 1
        assert con.execute('SELECT state FROM reference_image_deletions').fetchone()[0] == 'partial'
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
        assert con.execute('SELECT status FROM catalog_snapshots').fetchone()[0] == 'ready'


def test_partial_io_failure_durable_resume_and_ui(catalogue, monkeypatch):
    archive, cache, marker = catalogue(1)
    token = deletion.preview('image')['confirmation']
    original = deletion.remove_file
    def fail(root, entry):
        if entry['path'] == str(archive):
            raise PermissionError('injected disk error')
        return original(root, entry)
    monkeypatch.setattr(deletion, 'remove_file', fail)
    client = TestClient(main.app)
    response = client.request('DELETE', '/api/reference-images/image', json={'confirmation': token, 'confirmed_name':'Plex Test'})
    assert response.status_code == 202 and response.json()['state'] == 'partial'
    assert archive.exists() and not cache.exists() and not marker.exists()
    assert 'Reprendre la purge' in client.get('/reference-images/image/delete').text
    listing=client.get('/reference-images').text
    assert 'Suppression à reprendre' in listing and 'Plex Test' in listing
    with main.db() as con:
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
        assert con.execute('SELECT COUNT(*) FROM reference_images').fetchone()[0] == 1
    # Simulate a CP restart: only the SQLite journal is needed for recovery.
    main.init_database()
    monkeypatch.setattr(deletion, 'remove_file', original)
    assert deletion.delete('image', token)['state'] == 'deleted'
    assert not archive.exists()


def test_confirmation_change_and_late_writers(catalogue):
    catalogue(1)
    old = deletion.preview('image')['confirmation']
    catalogue(2)
    with pytest.raises(HTTPException, match='409'):
        deletion.delete('image', old)
    erase()
    for fields in ({'reference_version_id': 'v1'}, {'reference_image_id': 'image'}, {'snapshot_id': 's1'}):
        with pytest.raises(sqlite3.IntegrityError), main.db() as con:
            appbox(con, **fields)


def test_external_source_preserved(catalogue, tmp_path):
    archive, _, _ = catalogue(1)
    external = tmp_path / 'source-library'
    external.mkdir()
    data = external / 'metadata.db'
    data.write_bytes(b'not ours')
    with main.db() as con:
        con.execute("UPDATE reference_image_versions SET archive_path=NULL WHERE version_id='v1'")
        con.execute('UPDATE catalog_snapshots SET source_path=?', (str(external),))
    assert erase()['state'] == 'deleted'
    assert data.read_bytes() == b'not ours'
    assert archive.exists()  # unowned orphan, not part of the catalogue manifest


def test_replaced_artifact_is_not_removed_on_retry(catalogue, monkeypatch):
    archive, _, _ = catalogue(1)
    token = deletion.preview('image')['confirmation']
    original = deletion.remove_file
    monkeypatch.setattr(deletion, 'remove_file', Mock(side_effect=OSError('disk unavailable')))
    assert deletion.delete('image', token, confirmed_name='Plex Test')['state'] == 'partial'
    archive.write_bytes(b'a different archive')
    monkeypatch.setattr(deletion, 'remove_file', original)
    result = deletion.delete('image', token)
    assert result['state'] == 'partial'
    assert any('remplacé' in message for message in result['errors'])
    assert archive.read_bytes() == b'a different archive'


def test_parent_link_and_windows_junction_refused(catalogue, monkeypatch):
    archive, _, _ = catalogue(1)
    original = Path.lstat
    for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFDIR, 0x400)):
        def fake_lstat(path, *args, **kwargs):
            if path == archive.parent:
                return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
            return original(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'lstat', fake_lstat)
            with pytest.raises(ValueError, match='Lien symbolique/jonction'):
                deletion.confined(main.REFERENCE_ROOT, archive)


def test_crash_during_cleanup_replays_durable_manifest(catalogue, monkeypatch):
    files = catalogue(1)
    token = deletion.preview('image')['confirmation']
    original = deletion.remove_file
    def crash(root, entry):
        original(root, entry)
        raise SystemExit('simulated process loss')
    monkeypatch.setattr(deletion, 'remove_file', crash)
    with pytest.raises(SystemExit):
        deletion.delete('image', token, confirmed_name='Plex Test')
    monkeypatch.setattr(deletion, 'remove_file', original)
    assert deletion.pending()[0]['state'] == 'running'
    assert deletion.delete('image', token)['state'] == 'deleted'
    assert not any(file.exists() for file in files)


def test_active_build_and_shared_archive_block(catalogue):
    archive, _, _ = catalogue(1)
    build = main.create_reference_build_draft(source_node_id=main.HOSTNAME, display_name='Building')
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("UPDATE reference_builds SET image_id='image',status='building' WHERE build_id=?", (build,))
        con.execute("INSERT INTO reference_images(image_id,name,media_type,created_at,updated_at) VALUES('other','Other','plex',?,?)", (stamp, stamp))
        con.execute("INSERT INTO reference_image_versions(version_id,image_id,version,snapshot_id,archive_path,created_at) VALUES('other-v1','other','1','s1',?,?)", (str(archive), stamp))
    blockers = deletion.preview('image')['blockers']
    assert any('Build actif' in item for item in blockers)
    assert any('Snapshot partagé' in item for item in blockers)
    assert any('Artefact partagé' in item for item in blockers)
    with pytest.raises(HTTPException):
        erase()


def test_old_confirmation_never_deletes_recreated_image(catalogue):
    token = deletion.preview('image')['confirmation']
    deletion.delete('image', token, confirmed_name='Plex Test')
    with main.db() as con:
        con.execute("INSERT INTO reference_images(image_id,name,media_type,created_at,updated_at) VALUES('image','Recreated','plex',?,?)", (main.now_iso(), main.now_iso()))
    assert deletion.delete('image', token)['state'] == 'deleted'
    assert deletion.preview('image')['name'] == 'Recreated'


def test_ui_confirmation_form_and_delete_button(catalogue):
    catalogue(1)
    client = TestClient(main.app)
    listing=client.get('/reference-images').text
    assert 'href="/reference-images/image">Gérer' in listing
    assert 'Supprimer l’image' not in listing
    detail=client.get('/reference-images/image').text
    assert 'Zone de danger' in detail and 'Supprimer la référence complète' in detail
    assert 'href="/reference-images/image/versions/v1/delete">Supprimer cette version' in detail
    preview = deletion.preview('image')
    html = client.get('/reference-images/image/delete').text
    assert 'Supprimer définitivement' in html and 'Plex Test' in html
    response = client.post('/reference-images/image/delete', data={'confirmation': preview['confirmation'], 'confirmed_name':'Plex Test'})
    assert response.status_code == 200 and 'Suppression terminée' in response.text


def test_delete_old_version_only_and_active_default_refused(catalogue):
    first=catalogue(1); second=catalogue(2)
    with main.db() as con:
        con.execute("UPDATE reference_images SET status='published',current_version_id='v2' WHERE image_id='image'")
    detail=TestClient(main.app).get('/reference-images/image').text
    assert 'Créez ou activez une autre version avant de supprimer celle-ci.' in detail
    assert 'Créer une nouvelle version' in detail
    old=deletion.preview('image','v1')
    assert not old['blockers'] and old['version_count']==1
    result=deletion.delete('image',old['confirmation'],'v1')
    assert result['state']=='deleted' and not any(path.exists() for path in first)
    assert all(path.exists() for path in second)
    with main.db() as con:
        assert [row[0] for row in con.execute("SELECT version_id FROM reference_image_versions").fetchall()]==['v2']
    assert any('active/default' in item for item in deletion.preview('image','v2')['blockers'])
    assert any('active/publiée' in item for item in deletion.preview('image')['blockers'])


def _online_cache(catalogue,tmp_path,monkeypatch,online=True):
    catalogue(1); cache_root=tmp_path/'node-cache'; cache_root.mkdir()
    data=b'node archive'; checksum=__import__('hashlib').sha256(data).hexdigest()
    cached=cache_root/f'{checksum}.tar.gz'; cached.write_bytes(data)
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO node_agents(node_id,status,agent_version,last_heartbeat,capabilities_json,updated_at)
            VALUES(?, 'online','test',?,?,?) ON CONFLICT(node_id) DO UPDATE SET
            last_heartbeat=excluded.last_heartbeat,capabilities_json=excluded.capabilities_json,updated_at=excluded.updated_at""",
            (main.HOSTNAME,stamp,json.dumps({'reference_cache_delete':True}),stamp))
        con.execute("INSERT INTO node_reference_cache(node_id,version_id,local_path,checksum,status,size_bytes,updated_at) VALUES(?,'v1',?,?,'ready',?,?)",
                    (main.HOSTNAME,str(cached),checksum,len(data),stamp))
        con.execute("INSERT INTO reference_image_distribution(version_id,node_id,status,local_path,actual_checksum,updated_at) VALUES('v1',?,'ready',?,?,?)",
                    (main.HOSTNAME,str(cached),checksum,stamp))
        if not online:
            con.execute("UPDATE node_agents SET last_heartbeat='2000-01-01T00:00:00+00:00' WHERE node_id=?",(main.HOSTNAME,))
    availability={'online':online}
    monkeypatch.setattr(main,'list_control_nodes',lambda: [{
        'node_id':main.HOSTNAME,
        'status':'online' if availability['online'] else 'offline',
        'agent_online':availability['online'],
        'capabilities':{'reference_cache_delete':True},
    }])
    return cache_root,cached,checksum,availability


def test_online_node_purge_ack_then_catalogue_cleanup(catalogue,tmp_path,monkeypatch):
    root,cached,checksum,_=_online_cache(catalogue,tmp_path,monkeypatch)
    plan=deletion.preview('image','v1'); result=deletion.delete('image',plan['confirmation'],'v1')
    assert result['state']=='running' and result['nodes'][0]['status']=='queued'
    with main.db() as con:
        command=con.execute("SELECT * FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()
    payload=json.loads(command['payload_json'])
    remote=agent.delete_reference_cache({'reference_cache_dir':str(root)},payload)
    deletion.finalize_remote_command(command,'success',remote,None)
    final=deletion.operation(plan['confirmation'])
    assert final['state']=='deleted' and final['nodes'][0]['status']=='success' and not cached.exists()
    with main.db() as con:
        assert not con.execute("SELECT 1 FROM reference_image_versions WHERE version_id='v1'").fetchone()
        assert con.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_offline_node_is_purge_pending_then_resumes(catalogue,tmp_path,monkeypatch):
    root,cached,_,availability=_online_cache(catalogue,tmp_path,monkeypatch,online=False)
    plan=deletion.preview('image','v1'); result=deletion.delete('image',plan['confirmation'],'v1')
    assert result['state']=='purge_pending' and result['nodes'][0]['status']=='pending'
    assert cached.exists() and not (main.REFERENCE_ROOT/'builds/1/reference.tar.gz').exists()
    stamp=main.now_iso()
    with main.db() as con: con.execute("UPDATE node_agents SET last_heartbeat=? WHERE node_id=?",(stamp,main.HOSTNAME))
    availability['online']=True
    deletion.reconcile_node(main.HOSTNAME)
    with main.db() as con:
        command=con.execute("SELECT * FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()
    assert command and deletion.operation(plan['confirmation'])['nodes'][0]['status']=='queued'


def test_remote_failure_keeps_retry_information(catalogue,tmp_path,monkeypatch):
    _online_cache(catalogue,tmp_path,monkeypatch)
    plan=deletion.preview('image','v1'); deletion.delete('image',plan['confirmation'],'v1')
    with main.db() as con: command=con.execute("SELECT * FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()
    deletion.finalize_remote_command(command,'failed',{},'read-only filesystem')
    result=deletion.operation(plan['confirmation'])
    assert result['state']=='partial' and result['nodes'][0]['status']=='failed'
    assert result['nodes'][0]['local_path'] and result['nodes'][0]['checksum'] and result['nodes'][0]['attempts']==1
    deletion.delete('image',plan['confirmation'],'v1')
    assert deletion.operation(plan['confirmation'])['nodes'][0]['attempts']==2


@pytest.mark.parametrize('kind',['job','command'])
def test_active_job_or_agent_command_blocks_and_refusal_is_audited(catalogue,kind):
    catalogue(1); stamp=main.now_iso()
    with main.db() as con:
        if kind=='job':
            con.execute("INSERT INTO jobs(job_id,node_id,action,title,status,progress,detail,created_at,updated_at,options_json) VALUES('j',?,'deploy','x','running',1,'',?,?,?)",
                        (main.HOSTNAME,stamp,stamp,json.dumps({'reference_version_id':'v1'})))
        else:
            con.execute("INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at) VALUES('c',?,'appbox_action',?,'queued',?)",
                        (main.HOSTNAME,json.dumps({'reference_version_id':'v1'}),stamp))
    plan=deletion.preview('image','v1'); assert any(('Job actif' if kind=='job' else 'Commande agent active') in item for item in plan['blockers'])
    with pytest.raises(HTTPException): deletion.delete('image',plan['confirmation'],'v1')
    with main.db() as con: assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='reference_deletion_refused'").fetchone()[0]==1


def test_deletion_lock_blocks_concurrent_publish_and_double_request_is_idempotent(catalogue,tmp_path,monkeypatch):
    _online_cache(catalogue,tmp_path,monkeypatch)
    plan=deletion.preview('image','v1')
    first=deletion.delete('image',plan['confirmation'],'v1'); second=deletion.delete('image',plan['confirmation'],'v1')
    assert first['operation_id']==second['operation_id']
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM reference_image_deletions").fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()[0]==1
        with pytest.raises(sqlite3.IntegrityError,match='deletion'):
            con.execute("UPDATE reference_images SET current_version_id='v1' WHERE image_id='image'")


def test_version_deletion_serializes_other_deletions_for_same_image(catalogue,tmp_path,monkeypatch):
    _online_cache(catalogue,tmp_path,monkeypatch)
    version_plan=deletion.preview('image','v1')
    running=deletion.delete('image',version_plan['confirmation'],'v1')
    assert running['state']=='running'
    image_plan=deletion.preview('image')
    assert any('Suppression déjà active' in item for item in image_plan['blockers'])
    with pytest.raises(HTTPException):
        deletion.delete('image',image_plan['confirmation'],confirmed_name='Plex Test')
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM reference_image_deletions").fetchone()[0]==1


def test_manual_cache_purge_online_is_scoped_idempotent_and_preserves_catalogue(catalogue,tmp_path,monkeypatch):
    root,cached,_,_ = _online_cache(catalogue,tmp_path,monkeypatch)
    stamp=main.now_iso()
    with main.db() as con:
        appbox(con, reference_version_id='v1')
        con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('node-b','NODE B','remote','online',?,?)", (stamp,stamp))
        con.execute("""INSERT INTO node_reference_cache(node_id,version_id,local_path,checksum,status,size_bytes,updated_at)
            VALUES('node-b','v1','/node-b/cache.tar.gz',?,'ready',1,?)""", ('b'*64,stamp))
    first=deletion.purge_cache(main.HOSTNAME,'v1')
    assert first['state']=='running' and first['nodes'][0]['status']=='queued'
    with main.db() as con:
        command=con.execute("SELECT * FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()
    payload=json.loads(command['payload_json'])
    assert payload['local_path']==str(cached)
    remote=agent.delete_reference_cache({'reference_cache_dir':str(root)},payload)
    deletion.finalize_remote_command(command,'success',remote,None)
    second=deletion.purge_cache(main.HOSTNAME,'v1')
    assert second['state']=='deleted' and second['operation_id']==first['operation_id']
    assert 'PURGE TERMINÉE' in TestClient(main.app).get('/reference-caches').text
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()[0]==1
        assert con.execute("SELECT status,current_version_id FROM reference_images WHERE image_id='image'").fetchone()[1] is None
        assert con.execute("SELECT 1 FROM reference_image_versions WHERE version_id='v1'").fetchone()
        assert con.execute("SELECT 1 FROM appboxes WHERE client_id='ab-test'").fetchone()
        assert con.execute("SELECT 1 FROM node_reference_cache WHERE node_id='node-b' AND version_id='v1'").fetchone()


def test_manual_cache_purge_failure_is_visible_and_blocks_catalogue_delete(catalogue,tmp_path,monkeypatch):
    _online_cache(catalogue,tmp_path,monkeypatch)
    operation=deletion.purge_cache(main.HOSTNAME,'v1')
    with main.db() as con:
        command=con.execute("SELECT * FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()
    deletion.finalize_remote_command(command,'failed',{},'permission denied')
    assert deletion.operation(operation['operation_id'])['state']=='partial'
    assert any('Purge cache non finalisée' in item for item in deletion.preview('image')['blockers'])
    assert 'PURGE ÉCHOUÉE' in TestClient(main.app).get('/reference-caches').text


def test_manual_cache_purge_resumes_from_real_agent_poll_after_restart(catalogue,tmp_path,monkeypatch):
    _,cached,_,availability=_online_cache(catalogue,tmp_path,monkeypatch,online=False)
    pending=deletion.purge_cache(main.HOSTNAME,'v1')
    assert pending['state']=='purge_pending' and cached.exists()
    main.init_database()  # Simulated Manager restart/migration on the same durable DB.
    availability['online']=True
    monkeypatch.setattr(main,'authenticate_agent',lambda *_: None)
    response=TestClient(main.app).get(f'/api/agent/v1/{main.HOSTNAME}/commands')
    assert response.status_code==200
    command=response.json()['command']
    assert command['command_type']=='reference_cache_delete'
    assert command['payload']['operation_id']==pending['operation_id']
    assert cached.exists()


def test_cache_identity_replacement_is_preserved_and_operation_fails(catalogue,tmp_path,monkeypatch):
    _,cached,_,_=_online_cache(catalogue,tmp_path,monkeypatch)
    operation=deletion.purge_cache(main.HOSTNAME,'v1')
    with main.db() as con:
        command=con.execute("SELECT * FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()
        con.execute("""UPDATE node_reference_cache SET local_path=?,checksum=?,size_bytes=99,updated_at=?
            WHERE node_id=? AND version_id='v1'""",('/replacement/B.tar.gz','b'*64,main.now_iso(),main.HOSTNAME))
    deletion.finalize_remote_command(command,'success',{'cache_absent':True,'output':'A absent'},None)
    result=deletion.operation(operation['operation_id'])
    assert result['state']=='partial' and result['phase']=='failed'
    assert result['error_code']=='cache_identity_changed'
    with main.db() as con:
        replacement=con.execute("SELECT local_path,checksum FROM node_reference_cache WHERE node_id=? AND version_id='v1'",(main.HOSTNAME,)).fetchone()
        assert tuple(replacement)==('/replacement/B.tar.gz','b'*64)
        assert con.execute("SELECT state FROM reference_image_versions WHERE version_id='v1'").fetchone()[0]=='published'
    assert cached.exists()


@pytest.mark.parametrize('checksum',[None,'not-a-sha'])
def test_manual_cache_purge_rejects_missing_or_invalid_registered_checksum(catalogue,tmp_path,monkeypatch,checksum):
    _online_cache(catalogue,tmp_path,monkeypatch)
    with main.db() as con:
        con.execute("UPDATE node_reference_cache SET checksum=? WHERE node_id=? AND version_id='v1'",(checksum,main.HOSTNAME))
    result=deletion.purge_cache(main.HOSTNAME,'v1')
    assert result['state']=='partial' and result['nodes'][0]['status']=='failed'
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()[0]==0


def test_distinct_cache_purges_do_not_collide(catalogue,tmp_path,monkeypatch):
    _online_cache(catalogue,tmp_path,monkeypatch)
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('node-b','NODE B','remote','online',?,?)",(stamp,stamp))
        con.execute("""INSERT INTO node_reference_cache(node_id,version_id,local_path,checksum,status,size_bytes,updated_at)
            VALUES('node-b','v1','/node-b/cache.tar.gz',?,'ready',1,?)""",('b'*64,stamp))
    first=deletion.purge_cache(main.HOSTNAME,'v1')
    second=deletion.purge_cache('node-b','v1')
    assert first['operation_id']!=second['operation_id']
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM reference_image_deletions WHERE target_type='cache'").fetchone()[0]==2


def test_catalogue_finalizer_is_a_noop_for_cache_operation(catalogue,tmp_path,monkeypatch):
    _online_cache(catalogue,tmp_path,monkeypatch,online=False)
    operation=deletion.purge_cache(main.HOSTNAME,'v1')
    deletion._finish_catalogue(operation['operation_id'])
    with main.db() as con:
        assert con.execute("SELECT state FROM reference_image_versions WHERE version_id='v1'").fetchone()[0]=='published'
        assert con.execute("SELECT 1 FROM reference_images WHERE image_id='image'").fetchone()
        assert con.execute("SELECT 1 FROM node_reference_cache WHERE node_id=? AND version_id='v1'",(main.HOSTNAME,)).fetchone()


def test_manual_purge_refuses_cache_owned_by_catalogue_deletion(catalogue,tmp_path,monkeypatch):
    _online_cache(catalogue,tmp_path,monkeypatch,online=False)
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO reference_image_deletions(deletion_id,image_id,name,version_count,storage_root,
            manifest_json,state,errors_json,created_at,target_type,target_version_id)
            VALUES('catalogue-running','image','Plex Test',1,?,'{}','running','[]',?,'version','v1')""",
            (str(main.REFERENCE_ROOT),stamp))
    with pytest.raises(HTTPException) as exc:
        deletion.purge_cache(main.HOSTNAME,'v1')
    assert exc.value.status_code==409
