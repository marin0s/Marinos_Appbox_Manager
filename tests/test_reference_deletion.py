import json
import sqlite3
import stat
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main, reference_deletion as deletion


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
    return deletion.delete('image', deletion.preview('image')['confirmation'])


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
        response = client.request('DELETE', '/api/reference-images/image', json={'confirmation': preview['confirmation']})
        assert response.status_code == 200 and response.json()['state'] == 'deleted'
    assert client.get('/api/reference-images/missing/deletion').status_code == 404
    with main.db() as con:
        assert con.execute('SELECT COUNT(*) FROM audit_log WHERE action="reference_image_delete"').fetchone()[0] == 1


def test_multiple_versions_artifacts_cache_and_audit(catalogue, monkeypatch):
    files = [*catalogue(1), *catalogue(2)]
    # Availability of nodes is irrelevant; no remote command is ever queued.
    remote = Mock(side_effect=AssertionError('No remote deletion allowed'))
    monkeypatch.setattr(main, 'queue_agent_command', remote)
    stamp = main.now_iso()
    build = main.create_reference_build_draft(source_node_id=main.HOSTNAME, display_name='Plex Test')
    with main.db() as con:
        con.execute("UPDATE reference_builds SET image_id='image',version_id='v1',status='published' WHERE build_id=?", (build,))
        con.execute("INSERT INTO node_reference_cache(node_id,version_id,local_path,status,updated_at) VALUES(?,'v1','/remote/cache','ready',?)", (main.HOSTNAME, stamp))
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
        assert manifest['orphaned_node_cache'][0]['local_path'] == '/remote/cache'
        assert manifest['distributions'][0]['status'] == 'ready'
    remote.assert_not_called()


@pytest.mark.parametrize('fields', [{'reference_image_id': 'image'}, {'reference_version_id': 'v1'}, {'snapshot_id': 's1'}])
def test_appbox_blocker_and_ui(catalogue, fields):
    files = catalogue(1)
    with main.db() as con:
        appbox(con, **fields)
    preview = deletion.preview('image')
    assert 'AppBox : ab-test' in preview['blockers']
    response = TestClient(main.app).get('/reference-images/image/delete')
    assert response.status_code == 200
    assert 'Plex Test' in response.text and '1 version(s)' in response.text
    assert 'data-deletion-blockers' in response.text and 'AppBox : ab-test' in response.text
    assert 'type="submit"' not in response.text
    with pytest.raises(HTTPException) as error:
        deletion.delete('image', preview['confirmation'])
    assert error.value.status_code == 409
    assert all(file.exists() for file in files)


@pytest.mark.parametrize('status', ['planned', 'success', 'failed'])
def test_all_deployment_references_block(catalogue, status):
    catalogue(1)
    with main.db() as con:
        con.execute("INSERT INTO control_plane_deployments(deployment_id,reference_version_id,status,created_at,updated_at) VALUES('dep','v1',?,?,?)", (status, main.now_iso(), main.now_iso()))
    assert f'Déploiement : dep ({status})' in deletion.preview('image')['blockers']
    with pytest.raises(HTTPException):
        erase()


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
    with pytest.raises(sqlite3.IntegrityError, match='injected failure'):
        erase()
    assert all(path.exists() for path in files)
    with main.db() as con:
        assert con.execute('SELECT COUNT(*) FROM reference_image_versions').fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM reference_image_deletions').fetchone()[0] == 0
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
    response = client.request('DELETE', '/api/reference-images/image', json={'confirmation': token})
    assert response.status_code == 202 and response.json()['state'] == 'cleanup_pending'
    assert archive.exists() and not cache.exists() and not marker.exists()
    assert 'Reprendre le nettoyage' in client.get('/reference-images/image/delete').text
    assert 'Nettoyage en attente : Plex Test' in client.get('/reference-images').text
    with main.db() as con:
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []
        assert con.execute('SELECT COUNT(*) FROM reference_images').fetchone()[0] == 0
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
    assert deletion.delete('image', token)['state'] == 'cleanup_pending'
    archive.write_bytes(b'a different archive')
    monkeypatch.setattr(deletion, 'remove_file', original)
    result = deletion.delete('image', token)
    assert result['state'] == 'cleanup_pending'
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
        deletion.delete('image', token)
    monkeypatch.setattr(deletion, 'remove_file', original)
    assert deletion.pending()[0]['state'] == 'cleanup_pending'
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
    deletion.delete('image', token)
    with main.db() as con:
        con.execute("INSERT INTO reference_images(image_id,name,media_type,created_at,updated_at) VALUES('image','Recreated','plex',?,?)", (main.now_iso(), main.now_iso()))
    assert deletion.delete('image', token)['state'] == 'deleted'
    assert deletion.preview('image')['name'] == 'Recreated'


def test_ui_confirmation_form_and_delete_button(catalogue):
    catalogue(1)
    client = TestClient(main.app)
    assert 'href="/reference-images/image/delete">Supprimer' in client.get('/reference-images').text
    preview = deletion.preview('image')
    html = client.get('/reference-images/image/delete').text
    assert 'Supprimer définitivement : Plex Test (1 version(s))' in html
    response = client.post('/reference-images/image/delete', data={'confirmation': preview['confirmation']})
    assert response.status_code == 200 and 'Suppression terminée' in response.text
