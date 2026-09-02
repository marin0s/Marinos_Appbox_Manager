import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, reference_deletion


@pytest.fixture
def admin_catalogue(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_FILE', tmp_path / 'admin.db')
    monkeypatch.setattr(main, 'REFERENCE_ROOT', tmp_path / 'references')
    monkeypatch.setattr(main, 'DEPLOYMENT_STALE_SECONDS', 300)
    main.REFERENCE_ROOT.mkdir()
    main.init_database()
    stamp = main.now_iso()
    source = main.REFERENCE_ROOT / 'source'; source.mkdir()
    with main.db() as con:
        con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('node-a','NODE A','remote','online',?,?)", (stamp, stamp))
        con.execute("""INSERT INTO reference_images(image_id,name,media_type,status,current_version_id,created_at,updated_at)
            VALUES('keep-ref','Golden Keep','plex','published',NULL,?,?)""", (stamp, stamp))
        con.execute("INSERT INTO catalog_snapshots(snapshot_id,name,media_type,source_path,status,created_at,updated_at) VALUES('snap','Snapshot','plex',?,'ready',?,?)", (str(source), stamp, stamp))
        con.execute("""INSERT INTO reference_image_versions(version_id,image_id,version,snapshot_id,state,created_at,published_at)
            VALUES('keep-v1','keep-ref','1','snap','published',?,?)""", (stamp, stamp))
        con.execute("UPDATE reference_images SET current_version_id='keep-v1' WHERE image_id='keep-ref'")
    return TestClient(main.app)


def _deployment(status, *, deployment_id='dep', completed_at=None):
    with main.db() as con:
        con.execute("""INSERT INTO control_plane_deployments(deployment_id,node_id,reference_version_id,status,
            current_step,progress,created_at,updated_at,completed_at)
            VALUES(?,'node-a','keep-v1',?,'compose_ready',25,'2000-01-01T00:00:00+00:00','2000-01-01T00:00:00+00:00',?)""",
            (deployment_id, status, completed_at))


@pytest.mark.parametrize('status,completed_at', [
    ('prepared', None),
    ('deploying', '2026-01-01T00:00:00+00:00'),
])
def test_abandoned_deployment_can_be_cancelled_and_unblocks_deletion(admin_catalogue, status, completed_at):
    _deployment(status, completed_at=completed_at)
    response = admin_catalogue.post('/deployments/dep/cancel', follow_redirects=False)
    assert response.status_code == 303
    with main.db() as con:
        row = con.execute("SELECT status,completed_at,detail FROM control_plane_deployments WHERE deployment_id='dep'").fetchone()
    assert row['status'] == 'cancelled' and row['completed_at'] and 'manuellement' in row['detail']
    assert not any('Déploiement actif' in blocker for blocker in reference_deletion.preview('keep-ref')['blockers'])
    assert 'dep' in admin_catalogue.get('/deployments').text


def test_active_deployment_activity_prevents_manual_close(admin_catalogue):
    _deployment('prepared')
    with main.db() as con:
        con.execute("""INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at)
            VALUES('active-command','node-a','appbox_action',?,'claimed',?)""",
            (json.dumps({'deployment_id':'dep','action':'deploy'}), main.now_iso()))
    response = admin_catalogue.post('/deployments/dep/cancel')
    assert response.status_code == 409
    with main.db() as con:
        assert con.execute("SELECT status FROM control_plane_deployments WHERE deployment_id='dep'").fetchone()[0] == 'prepared'
    html = admin_catalogue.get('/deployments').text
    assert 'ACTIVE-COMMAND' in html.upper() and 'STALE' not in html


def test_active_job_referencing_deployment_prevents_manual_close(admin_catalogue):
    _deployment('prepared')
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO jobs(job_id,node_id,action,title,status,created_at,updated_at,options_json)
            VALUES('active-job','node-a','deploy','Deployment','running',?,?,?)""",
            (stamp, stamp, json.dumps({'deployment_id':'dep'})))
    assert admin_catalogue.post('/deployments/dep/cancel').status_code == 409


def test_unrelated_recent_activity_on_same_appbox_does_not_block_old_deployment(admin_catalogue):
    _deployment('prepared')
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO appboxes(client_id,node_id,path,status,containers_json,created_at,updated_at)
            VALUES('shared-client','node-a','/tmp/shared','running','[]',?,?)""", (stamp, stamp))
        con.execute("UPDATE control_plane_deployments SET client_id='shared-client' WHERE deployment_id='dep'")
        con.execute("""INSERT INTO jobs(job_id,client_id,node_id,action,title,status,created_at,updated_at,options_json)
            VALUES('restart-job','shared-client','node-a','restart','Restart','running',?,?,'{}')""", (stamp, stamp))
        con.execute("""INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at)
            VALUES('restart-command','node-a','appbox_action',?,'claimed',?)""",
            (json.dumps({'client_id':'shared-client','action':'restart'}), stamp))
    assert admin_catalogue.post('/deployments/dep/cancel', follow_redirects=False).status_code == 303


def test_legacy_deploy_activity_belongs_only_to_latest_matching_deployment(admin_catalogue):
    _deployment('prepared', deployment_id='old-deployment')
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO appboxes(client_id,node_id,path,status,containers_json,created_at,updated_at)
            VALUES('legacy-client','node-a','/tmp/legacy','running','[]',?,?)""", (stamp, stamp))
        con.execute("UPDATE control_plane_deployments SET client_id='legacy-client' WHERE deployment_id='old-deployment'")
        con.execute("""INSERT INTO control_plane_deployments(deployment_id,client_id,node_id,reference_version_id,status,
            created_at,updated_at) VALUES('new-deployment','legacy-client','node-a','keep-v1','prepared',
            '2025-01-01T00:00:00+00:00','2025-01-01T00:00:00+00:00')""")
        con.execute("""INSERT INTO jobs(job_id,client_id,node_id,action,title,status,created_at,updated_at,options_json)
            VALUES('legacy-deploy','legacy-client','node-a','deploy','Deploy','running',?,?,'{}')""", (stamp, stamp))
    assert admin_catalogue.post('/deployments/old-deployment/cancel', follow_redirects=False).status_code == 303
    assert admin_catalogue.post('/deployments/new-deployment/cancel').status_code == 409


def test_recent_coherent_deployment_is_not_closable(admin_catalogue):
    stamp = datetime.now(timezone.utc).isoformat()
    with main.db() as con:
        con.execute("""INSERT INTO control_plane_deployments(deployment_id,node_id,status,created_at,updated_at)
            VALUES('recent','node-a','prepared',?,?)""", (stamp, stamp))
    assert admin_catalogue.post('/deployments/recent/cancel').status_code == 409


def test_cancelled_history_is_terminal_and_idempotent(admin_catalogue):
    _deployment('prepared')
    assert admin_catalogue.post('/deployments/dep/cancel', follow_redirects=False).status_code == 303
    assert admin_catalogue.post('/deployments/dep/cancel', follow_redirects=False).status_code == 303
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM control_plane_deployments WHERE deployment_id='dep'").fetchone()[0] == 1


def test_reference_cache_page_does_not_change_unrelated_catalogue(admin_catalogue, monkeypatch):
    monkeypatch.setattr(main, 'list_control_nodes', lambda: [{'node_id':'node-a','name':'NODE A','status':'offline'}])
    assert 'Aucun cache de Reference Image connu' in admin_catalogue.get('/reference-caches').text
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO node_reference_cache(node_id,version_id,local_path,checksum,status,size_bytes,updated_at)
            VALUES('node-a','keep-v1','/safe/cache.tar.gz',?,'ready',42,?)""", ('a' * 64, stamp))
    html = admin_catalogue.get('/reference-caches').text
    assert 'Golden Keep' in html and 'NODE A' in html and '/safe/cache.tar.gz' in html
    response = admin_catalogue.post('/reference-caches/node-a/keep-v1/purge', data={'confirmed':'true'}, follow_redirects=False)
    assert response.status_code == 303
    with main.db() as con:
        assert tuple(con.execute("SELECT status,current_version_id FROM reference_images WHERE image_id='keep-ref'").fetchone()) == ('published', 'keep-v1')
        assert con.execute("SELECT COUNT(*) FROM agent_commands WHERE command_type='reference_cache_delete'").fetchone()[0] == 0
    assert 'PURGE EN ATTENTE' in admin_catalogue.get('/reference-caches').text


def test_unknown_cache_cannot_supply_an_arbitrary_path(admin_catalogue):
    response = admin_catalogue.post('/reference-caches/node-a/missing/purge', data={'confirmed':'true'})
    assert response.status_code == 404


def test_schema_initialization_is_idempotent_and_storage_topology_remains_available(admin_catalogue):
    main.init_database()
    main.init_database()
    with main.db() as con:
        deletion_columns = {row[1] for row in con.execute('PRAGMA table_info(reference_image_deletions)')}
        storage_columns = {row[1] for row in con.execute('PRAGMA table_info(node_storage_paths)')}
        assert {'target_type', 'target_version_id', 'phase', 'progress'} <= deletion_columns
        assert {'node_id', 'host_path'} <= storage_columns
        assert con.execute('PRAGMA foreign_key_check').fetchall() == []


def test_pending_reuse_trigger_distinguishes_cache_and_catalogue_operations(admin_catalogue):
    stamp = main.now_iso()
    manifest = json.dumps({'versions': [], 'files': []})
    with main.db() as con:
        con.execute("""INSERT INTO reference_image_deletions(deletion_id,image_id,name,version_count,
            storage_root,manifest_json,state,errors_json,created_at,target_type)
            VALUES('cache-op','cache-reuse','Cache',0,?,?,'purge_pending','[]',?,'cache')""",
            (str(main.REFERENCE_ROOT), manifest, stamp))
        con.execute("INSERT INTO reference_images(image_id,name,media_type,created_at,updated_at) VALUES('cache-reuse','Allowed','plex',?,?)", (stamp, stamp))
        con.execute("""INSERT INTO reference_image_deletions(deletion_id,image_id,name,version_count,
            storage_root,manifest_json,state,errors_json,created_at,target_type)
            VALUES('catalogue-op','catalogue-reuse','Catalogue',0,?,?,'purge_pending','[]',?,'image')""",
            (str(main.REFERENCE_ROOT), manifest, stamp))
        with pytest.raises(sqlite3.IntegrityError, match='Reference cleanup pending'):
            con.execute("INSERT INTO reference_images(image_id,name,media_type,created_at,updated_at) VALUES('catalogue-reuse','Blocked','plex',?,?)", (stamp, stamp))
        trigger = con.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='reference_pending_reuse'").fetchone()[0]
    assert "target_type!='cache'" in trigger


def test_representative_golden_references_are_unchanged_by_other_admin_operations(admin_catalogue, monkeypatch):
    stamp = main.now_iso()
    protected_ids = ('plex-golden-ouranos', 'golden-test-orion', 'plex-jdmry')
    with main.db() as con:
        for image_id in protected_ids:
            con.execute("""INSERT INTO reference_images(image_id,name,media_type,status,created_at,updated_at)
                VALUES(?,?,'plex','published',?,?)""", (image_id, image_id, stamp, stamp))
        before = [tuple(row) for row in con.execute("""SELECT image_id,status,current_version_id,updated_at
            FROM reference_images WHERE image_id IN (?,?,?) ORDER BY image_id""", protected_ids)]
        con.execute("""INSERT INTO node_reference_cache(node_id,version_id,local_path,checksum,status,size_bytes,updated_at)
            VALUES('node-a','keep-v1','/safe/other.tar.gz',?,'ready',42,?)""", ('a'*64, stamp))
    monkeypatch.setattr(main, 'list_control_nodes', lambda: [{'node_id':'node-a','name':'NODE A','status':'offline'}])
    assert admin_catalogue.get('/reference-images').status_code == 200
    assert admin_catalogue.get('/reference-caches').status_code == 200
    admin_catalogue.post('/reference-caches/node-a/keep-v1/purge', data={'confirmed':'true'})
    _deployment('prepared', deployment_id='old-other')
    admin_catalogue.post('/deployments/old-other/cancel')
    admin_catalogue.post('/reference-images/keep-ref/retire', data={'confirmed':'true'})
    with main.db() as con:
        after = [tuple(row) for row in con.execute("""SELECT image_id,status,current_version_id,updated_at
            FROM reference_images WHERE image_id IN (?,?,?) ORDER BY image_id""", protected_ids)]
    assert after == before
