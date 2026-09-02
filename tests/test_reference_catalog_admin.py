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


def _operational_appbox(*, client_id='client-ok', node_id='node-a', reference_version_id='keep-v1',
                        desired_state='running', observed_state='running',
                        reconciliation_status='in_sync', status='running'):
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO appboxes(client_id,node_id,path,status,containers_json,created_at,updated_at,
            reference_image_id,reference_version_id,desired_state,observed_state,reconciliation_status)
            VALUES(?,?,?,?, '[]',?,?,?,?,?,?,?)""",
            (client_id, node_id, f'/tmp/{client_id}', status, stamp, stamp, 'keep-ref',
             reference_version_id, desired_state, observed_state, reconciliation_status))


def _attach_deployment(client_id='client-ok', *, deployment_id='dep', status='awaiting_claim', node_id='node-a'):
    with main.db() as con:
        con.execute("""UPDATE control_plane_deployments SET client_id=?,node_id=?,status=?,
            current_step='health',progress=95 WHERE deployment_id=?""",
            (client_id, node_id, status, deployment_id))


def test_incomplete_deployment_with_strong_runtime_evidence_is_reconcilable(admin_catalogue):
    _deployment('awaiting_claim')
    _operational_appbox()
    _attach_deployment()
    with main.db() as con:
        deployment = dict(con.execute("SELECT * FROM control_plane_deployments WHERE deployment_id='dep'").fetchone())
        evidence = main.deployment_success_evidence(con, deployment)
        main._decorate_deployment(con, deployment)
    assert evidence['eligible'] is True
    assert deployment['lifecycle_state'] == 'reconcilable'
    html = admin_catalogue.get('/deployments').text
    assert 'À RÉGULARISER · AWAITING_CLAIM' in html
    assert 'Régulariser comme terminé' in html
    assert 'action="/deployments/dep/cancel"' not in html


def test_reconcile_success_is_local_audited_and_idempotent(admin_catalogue):
    _deployment('awaiting_claim')
    _operational_appbox()
    _attach_deployment()
    with main.db() as con:
        before_box = tuple(con.execute("SELECT * FROM appboxes WHERE client_id='client-ok'").fetchone())
        before_commands = con.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0]
        before_jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert admin_catalogue.post('/deployments/dep/reconcile-success', follow_redirects=False).status_code == 303
    assert admin_catalogue.post('/deployments/dep/reconcile-success', follow_redirects=False).status_code == 303
    with main.db() as con:
        row = con.execute("""SELECT status,current_step,progress,completed_at,detail,created_at,
            reference_version_id,node_id,client_id FROM control_plane_deployments WHERE deployment_id='dep'""").fetchone()
        assert tuple(row[:3]) == ('success', 'reconciled', 100)
        assert row['completed_at'] and 'régularisé' in row['detail']
        assert (row['created_at'], row['reference_version_id'], row['node_id'], row['client_id']) == (
            '2000-01-01T00:00:00+00:00', 'keep-v1', 'node-a', 'client-ok')
        assert tuple(con.execute("SELECT * FROM appboxes WHERE client_id='client-ok'").fetchone()) == before_box
        assert con.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0] == before_commands
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before_jobs
        assert con.execute("SELECT COUNT(*) FROM audit_log WHERE action='control_plane_deployment_reconciled'").fetchone()[0] == 1


@pytest.mark.parametrize('mutation,expected_reason', [
    ({'reference_version_id': 'keep-v2'}, 'reference_version_differente'),
    ({'node_id': 'node-b'}, 'node_different'),
    ({'desired_state': 'stopped'}, 'etat_runtime_non_running'),
    ({'observed_state': 'stopped'}, 'etat_runtime_non_running'),
    ({'reconciliation_status': 'drift'}, 'reconciliation_non_conforme'),
])
def test_success_evidence_rejects_mismatched_runtime(admin_catalogue, mutation, expected_reason):
    _deployment('awaiting_claim')
    if mutation.get('reference_version_id') == 'keep-v2':
        with main.db() as con:
            con.execute("""INSERT INTO reference_image_versions(version_id,image_id,version,snapshot_id,state,created_at,published_at)
                VALUES('keep-v2','keep-ref','2','snap','published',?,?)""", (main.now_iso(), main.now_iso()))
    if mutation.get('node_id') == 'node-b':
        stamp = main.now_iso()
        with main.db() as con:
            con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('node-b','NODE B','remote','online',?,?)", (stamp, stamp))
    _operational_appbox(
        node_id=mutation.get('node_id', 'node-a'),
        reference_version_id=mutation.get('reference_version_id', 'keep-v1'),
        desired_state=mutation.get('desired_state', 'running'),
        observed_state=mutation.get('observed_state', 'running'),
        reconciliation_status=mutation.get('reconciliation_status', 'in_sync'),
    )
    _attach_deployment()
    with main.db() as con:
        deployment = dict(con.execute("SELECT * FROM control_plane_deployments WHERE deployment_id='dep'").fetchone())
        evidence = main.deployment_success_evidence(con, deployment)
    assert evidence['eligible'] is False and evidence['reason'] == expected_reason
    assert admin_catalogue.post('/deployments/dep/reconcile-success').status_code == 409


@pytest.mark.parametrize('activity_kind', ['job', 'command'])
def test_exact_active_activity_blocks_success_reconciliation(admin_catalogue, activity_kind):
    _deployment('awaiting_claim')
    _operational_appbox()
    _attach_deployment()
    stamp = main.now_iso()
    with main.db() as con:
        if activity_kind == 'job':
            con.execute("""INSERT INTO jobs(job_id,client_id,node_id,action,title,status,created_at,updated_at,options_json)
                VALUES('active','client-ok','node-a','deploy','Deploy','running',?,?,?)""",
                (stamp, stamp, json.dumps({'deployment_id': 'dep'})))
        else:
            con.execute("""INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at)
                VALUES('active','node-a','appbox_action',?,'claimed',?)""",
                (json.dumps({'deployment_id': 'dep', 'client_id': 'client-ok', 'action': 'deploy'}), stamp))
    assert admin_catalogue.post('/deployments/dep/reconcile-success').status_code == 409


def test_unrelated_activity_does_not_block_success_reconciliation(admin_catalogue):
    _deployment('awaiting_claim')
    _operational_appbox()
    _attach_deployment()
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO jobs(job_id,client_id,node_id,action,title,status,created_at,updated_at,options_json)
            VALUES('other-job','client-ok','node-a','deploy','Other deploy','running',?,?,?)""",
            (stamp, stamp, json.dumps({'deployment_id': 'another-deployment'})))
        con.execute("""INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at)
            VALUES('restart','node-a','appbox_action',?,'claimed',?)""",
            (json.dumps({'client_id': 'client-ok', 'action': 'restart'}), stamp))
    assert admin_catalogue.post('/deployments/dep/reconcile-success', follow_redirects=False).status_code == 303


def test_cancel_refuses_success_evidence_and_terminal_failure_cannot_be_reconciled(admin_catalogue):
    _deployment('awaiting_claim')
    _operational_appbox()
    _attach_deployment()
    assert admin_catalogue.post('/deployments/dep/cancel').status_code == 409
    with main.db() as con:
        con.execute("UPDATE control_plane_deployments SET status='failed' WHERE deployment_id='dep'")
    assert admin_catalogue.post('/deployments/dep/reconcile-success').status_code == 409


def test_planned_deployment_is_never_inferred_success(admin_catalogue):
    _deployment('planned')
    _operational_appbox()
    _attach_deployment(status='planned')
    assert admin_catalogue.post('/deployments/dep/reconcile-success').status_code == 409


def test_bulk_preview_and_cancel_only_true_obsolete_zombies(admin_catalogue):
    _deployment('prepared', deployment_id='obsolete')
    _deployment('deploying', deployment_id='golden-evidence')
    _deployment('awaiting_claim', deployment_id='p0-evidence')
    _operational_appbox(client_id='golden-test-orion')
    _operational_appbox(client_id='p0e2e01')
    _attach_deployment('golden-test-orion', deployment_id='golden-evidence', status='deploying')
    _attach_deployment('p0e2e01', deployment_id='p0-evidence', status='awaiting_claim')
    _deployment('prepared', deployment_id='active-job')
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO jobs(job_id,node_id,action,title,status,created_at,updated_at,options_json)
            VALUES('busy','node-a','deploy','Busy','running',?,?,?)""",
            (stamp, stamp, json.dumps({'deployment_id': 'active-job'})))
        before_boxes = [tuple(row) for row in con.execute(
            "SELECT * FROM appboxes WHERE client_id IN ('golden-test-orion','p0e2e01') ORDER BY client_id"
        )]
        before_reference = tuple(con.execute("SELECT * FROM reference_images WHERE image_id='keep-ref'").fetchone())
    html = admin_catalogue.get('/deployments').text
    assert 'Historique obsolète (1)' in html and 'obsolete' in html
    assert 'Prévisualiser la clôture en masse' in html
    assert admin_catalogue.post('/deployments/actions/cancel-obsolete').status_code == 400
    assert admin_catalogue.post('/deployments/actions/cancel-obsolete', data={'confirmed': 'true'}, follow_redirects=False).status_code == 303
    with main.db() as con:
        states = dict(con.execute("SELECT deployment_id,status FROM control_plane_deployments"))
        assert states == {
            'obsolete': 'cancelled', 'golden-evidence': 'deploying',
            'p0-evidence': 'awaiting_claim', 'active-job': 'prepared',
        }
        assert [tuple(row) for row in con.execute(
            "SELECT * FROM appboxes WHERE client_id IN ('golden-test-orion','p0e2e01') ORDER BY client_id"
        )] == before_boxes
        assert tuple(con.execute("SELECT * FROM reference_images WHERE image_id='keep-ref'").fetchone()) == before_reference
        assert con.execute("SELECT COUNT(*) FROM agent_commands").fetchone()[0] == 0
        audit = con.execute("SELECT detail FROM audit_log WHERE action='control_plane_deployments_bulk_cancelled'").fetchone()
        assert json.loads(audit['detail'])['deployment_ids'] == ['obsolete']


def test_deployments_ui_sections_and_long_detail_disclosure(admin_catalogue):
    _deployment('prepared', deployment_id='old')
    _deployment('success', deployment_id='done')
    long_detail = 'Downloading...\n' * 40
    with main.db() as con:
        con.execute("UPDATE control_plane_deployments SET detail=? WHERE deployment_id='old'", (long_detail,))
    html = admin_catalogue.get('/deployments').text
    assert 'À traiter' in html and 'Terminés (1)' in html and 'Historique obsolète (1)' in html
    assert '<details class="card section-block"><summary><strong>Terminés (1)' in html
    assert '<details class="card section-block"><summary><strong>Historique obsolète (1)' in html
    assert 'Afficher les détails' in html
    assert long_detail.strip() in html


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
