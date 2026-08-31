import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from unittest.mock import patch
import pytest
from pathlib import Path

os.environ.setdefault("APPBOX_MODE", "mock")
from app import main


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE appboxes (client_id TEXT PRIMARY KEY);
CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, client_id TEXT, node_id TEXT, action TEXT,
  status TEXT, progress INTEGER, detail TEXT, created_at TEXT, updated_at TEXT,
  started_at TEXT, finished_at TEXT,
  FOREIGN KEY(client_id) REFERENCES appboxes(client_id)
);
CREATE TABLE events (event_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE notifications_queue (notification_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE port_reservations (
  reservation_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id),
  status TEXT, released_at TEXT
);
CREATE TABLE appbox_mounts (client_id TEXT REFERENCES appboxes(client_id) ON DELETE CASCADE);
CREATE TABLE snapshot_deployments (client_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE reconciliation_events (client_id TEXT REFERENCES appboxes(client_id) ON DELETE CASCADE);
CREATE TABLE containers (container_id TEXT PRIMARY KEY, appbox_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE networks (network_id TEXT PRIMARY KEY, appbox_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE volumes (volume_id TEXT PRIMARY KEY, appbox_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE placement_decisions (
  decision_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id), reason TEXT
);
CREATE TABLE control_plane_deployments (
  deployment_id TEXT PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id), detail TEXT
);
"""


class TransactionEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = main.DB_FILE
        main.DB_FILE = Path(self.tmp.name) / "test.db"
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO appboxes(client_id) VALUES ('test141')")
            con.execute("INSERT INTO jobs VALUES ('job1','test141','artemis','delete','running',50,'',datetime('now'),datetime('now'),NULL,NULL)")
            con.execute("INSERT INTO events(client_id) VALUES ('test141')")
            con.execute("INSERT INTO notifications_queue(client_id) VALUES ('test141')")
            con.execute("INSERT INTO port_reservations(client_id,status) VALUES ('test141','reserved')")
            con.execute("INSERT INTO appbox_mounts VALUES ('test141')")
            con.execute("INSERT INTO snapshot_deployments VALUES ('test141')")
            con.execute("INSERT INTO reconciliation_events VALUES ('test141')")
            con.execute("INSERT INTO containers VALUES ('c1','test141')")
            con.execute("INSERT INTO networks VALUES ('n1','test141')")
            con.execute("INSERT INTO volumes VALUES ('v1','test141')")
            con.execute("INSERT INTO placement_decisions VALUES (1,'test141','historique conservé')")
            con.execute("INSERT INTO control_plane_deployments VALUES ('dep1','test141','historique conservé')")

    def tearDown(self):
        main.DB_FILE = self.old_db
        self.tmp.cleanup()

    def test_test141_regression_detaches_history_and_deletes_inventory(self):
        self.assertTrue(main.finalize_appbox_deletion('test141', 'job1'))
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM appboxes").fetchone()[0], 0)
            self.assertIsNone(con.execute("SELECT client_id FROM jobs WHERE job_id='job1'").fetchone()[0])
            self.assertIsNone(con.execute("SELECT client_id FROM placement_decisions WHERE decision_id=1").fetchone()[0])
            self.assertIsNone(con.execute("SELECT client_id FROM control_plane_deployments WHERE deployment_id='dep1'").fetchone()[0])
            self.assertEqual(con.execute("SELECT status FROM port_reservations").fetchone()[0], 'released')
            for table in ('appbox_mounts','snapshot_deployments','reconciliation_events','containers','networks','volumes'):
                self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_finalization_is_idempotent(self):
        self.assertTrue(main.finalize_appbox_deletion('test141', 'job1'))
        self.assertFalse(main.finalize_appbox_deletion('test141', 'job1'))

    def test_foreign_key_failure_rolls_back_everything(self):
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("CREATE TABLE unknown_child(client_id TEXT NOT NULL REFERENCES appboxes(client_id))")
            con.execute("INSERT INTO unknown_child VALUES ('test141')")
        with self.assertRaises(sqlite3.IntegrityError):
            main.finalize_appbox_deletion('test141', 'job1')
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM appboxes WHERE client_id='test141'").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT client_id FROM jobs WHERE job_id='job1'").fetchone()[0], 'test141')
            self.assertEqual(con.execute("SELECT client_id FROM placement_decisions WHERE decision_id=1").fetchone()[0], 'test141')
            self.assertEqual(con.execute("SELECT client_id FROM control_plane_deployments WHERE deployment_id='dep1'").fetchone()[0], 'test141')
            self.assertEqual(con.execute("SELECT status FROM port_reservations").fetchone()[0], 'reserved')


if __name__ == '__main__':
    unittest.main()


@pytest.fixture
def job_database(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_FILE',tmp_path/'jobs.db')
    monkeypatch.setattr(main,'BASE_DIR',tmp_path)
    monkeypatch.setattr(main,'HOSTNAME','cronos')
    monkeypatch.setattr(main,'worker_stop',Event())
    monkeypatch.setattr(main,'worker_wakeup',Event())
    main.init_database()
    stamp=main.now_iso()
    with main.db() as con:
        for node in ('artemis','orion'):
            con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES(?,?,'remote','online',?,?)",(node,node,stamp,stamp))
            con.execute("INSERT INTO node_agents(node_id,status,last_heartbeat,capabilities_json,updated_at) VALUES(?,'online',?,?,?)",
                        (node,stamp,json.dumps({'deployment_executor':True}),stamp))
        for client,node in [('ab40ah','artemis'),('ab37ah','artemis'),('image-jelly','orion')]:
            con.execute("INSERT INTO appboxes(client_id,node_id,path,containers_json,status,desired_state,observed_state,created_at,updated_at) VALUES(?,?,?,'[]','error','deleted','missing',?,?)",
                        (client,node,str(tmp_path/client),stamp,stamp))
    yield tmp_path
    main.worker_stop.set()
    main.worker_wakeup.set()


class AgentResultRequest:
    headers={}
    def __init__(self,result): self.result=result
    async def json(self): return self.result


def complete_local_agent(command_id,base):
    from test_agent_deployment import agent, DeletionDocker
    with main.db() as con:
        row=con.execute('SELECT * FROM agent_commands WHERE command_id=?',(command_id,)).fetchone()
        con.execute("UPDATE agent_commands SET status='claimed',claimed_at=? WHERE command_id=?",(main.now_iso(),command_id))
    with patch.object(agent,'run',DeletionDocker()):
        result=agent.execute_command({'appbox_base_dir':str(base)},
            {'command_type':'appbox_action','payload':json.loads(row['payload_json'])})
    with patch.object(main,'authenticate_agent'):
        asyncio.run(main.agent_command_result(row['node_id'],command_id,AgentResultRequest({'status':'success','result':result})))


@pytest.mark.parametrize('mode',['delete','purge'])
def test_legacy_missing_appbox_finishes_all_deletion_steps(job_database,monkeypatch,mode):
    job_id=main.create_job('ab40ah','delete','repair',node_id='artemis',options={'deletion_mode':mode})
    wait=main.wait_agent_command
    def deliver(command_id,**kwargs):
        complete_local_agent(command_id,job_database)
        return wait(command_id,**kwargs)
    monkeypatch.setattr(main,'wait_agent_command',deliver)
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
    main.execute_job(job)
    with main.db() as con:
        result=con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        assert result['status']=='success' and result['progress']==100 and result['client_id'] is None
        assert not con.execute("SELECT 1 FROM appboxes WHERE client_id='ab40ah'").fetchone()
        assert con.execute("SELECT result FROM audit_log WHERE client_id='ab40ah' ORDER BY audit_id DESC LIMIT 1").fetchone()[0]=='SUCCESS'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
    steps={s['step_key']:s['status'] for s in main.step_rows(job_id)}
    for name in ('docker_remove','cleanup_files','inventory','audit','notification'): assert steps[name]=='success'
    assert not (job_database/'ab40ah').exists()
    assert main.finalize_appbox_deletion('ab40ah',job_id,purge=mode=='purge') is False


@pytest.mark.parametrize('age,expired',[(59.999,False),(60,True),(60.001,True)])
def test_command_claim_deadline_boundaries_legacy_and_new(job_database,age,expired):
    now=datetime.now(timezone.utc)
    with patch.object(main,'AGENT_CLAIM_TIMEOUT_SECONDS',60):
        command={'command_type':'appbox_action','status':'queued','created_at':(now-timedelta(seconds=age)).isoformat(),'payload_json':'{}'}
        assert main.appbox_claim_expired(command,now)==expired
        command['payload_json']=json.dumps({'_claim_deadline':(now+timedelta(seconds=60-age)).isoformat()})
        assert main.appbox_claim_expired(command,now)==expired


def test_unclaimed_timeout_fails_job_and_rejects_late_claim_result(job_database,monkeypatch):
    monkeypatch.setattr(main,'AGENT_CLAIM_TIMEOUT_SECONDS',0)
    job_id=main.create_job('image-jelly','delete','timeout',node_id='orion')
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
    main.execute_job(job)
    with main.db() as con:
        result=con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        command=con.execute("SELECT * FROM agent_commands WHERE node_id='orion'").fetchone()
        assert result['status']=='failed' and result['progress']==100
        assert main.CLAIM_TIMEOUT_MESSAGE in result['detail']
        assert command['status']=='failed' and command['claimed_at'] is None
    assert all(s['status'] in {'success','failed','skipped'} for s in main.step_rows(job_id))
    with patch.object(main,'authenticate_agent'):
        assert json.loads(main.agent_poll_commands('orion',AgentResultRequest({})).body)['command'] is None
        response=asyncio.run(main.agent_command_result('orion',command['command_id'],AgentResultRequest({'status':'success','result':{}})))
        assert json.loads(response.body)['ignored']=='terminal_command'
    with main.db() as con:
        assert con.execute('SELECT status FROM agent_commands WHERE command_id=?',(command['command_id'],)).fetchone()[0]=='failed'
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='failed'


def test_poll_expires_old_unclaimed_without_waiter(job_database):
    command_id=main.queue_agent_command('orion','appbox_action',{'client_id':'image-jelly','action':'delete'})
    with main.db() as con:
        con.execute("UPDATE agent_commands SET payload_json=?,created_at='2000-01-01T00:00:00' WHERE command_id=?",
                    (json.dumps({'client_id':'image-jelly','action':'delete'}),command_id))
    with patch.object(main,'authenticate_agent'):
        assert json.loads(main.agent_poll_commands('orion',AgentResultRequest({})).body)['command'] is None
    with main.db() as con:
        assert con.execute('SELECT status FROM agent_commands WHERE command_id=?',(command_id,)).fetchone()[0]=='failed'


def test_restart_finalizes_legacy_running_and_cancels_command_preserving_queued_jobs(job_database):
    interrupted=main.create_job('image-jelly','delete','old running',node_id='orion')
    queued=main.create_job('ab37ah','delete','next',node_id='artemis')
    command_id=main.queue_agent_command('orion','appbox_action',{'client_id':'image-jelly','action':'delete'})
    with main.db() as con:
        con.execute("UPDATE jobs SET status='running' WHERE job_id=?",(interrupted,))
        # Legacy payload: no persistent job linkage/deadline.
        con.execute('UPDATE agent_commands SET payload_json=? WHERE command_id=?',
                    (json.dumps({'client_id':'image-jelly','action':'delete'}),command_id))
    assert main.recover_interrupted_jobs()==1
    assert main.recover_interrupted_jobs()==0
    with main.db() as con:
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(interrupted,)).fetchone()[0]=='failed'
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(queued,)).fetchone()[0]=='queued'
        assert con.execute('SELECT status FROM agent_commands WHERE command_id=?',(command_id,)).fetchone()[0]=='failed'


def test_blocked_orion_does_not_block_artemis_and_each_node_stays_sequential(job_database,monkeypatch):
    first=main.create_job('image-jelly','delete','blocked orion',node_id='orion')
    second=main.create_job('ab40ah','delete','artemis',node_id='artemis')
    third=main.create_job('ab37ah','delete','artemis second',node_id='artemis')
    orion_waiting,artemis_started,release_artemis,second_artemis,orion_done=Event(),Event(),Event(),Event(),Event()
    wait=main.wait_agent_command
    def deliver(command_id,**kwargs):
        with main.db() as con: node=con.execute('SELECT node_id FROM agent_commands WHERE command_id=?',(command_id,)).fetchone()[0]
        if node=='orion':
            orion_waiting.set()
            result=wait(command_id,**kwargs)
            orion_done.set()
            return result
        if kwargs['job_id']==second:
            artemis_started.set()
            assert release_artemis.wait(4)
        else:
            second_artemis.set()
        complete_local_agent(command_id,job_database)
        return wait(command_id,**kwargs)
    monkeypatch.setattr(main,'wait_agent_command',deliver)
    dispatcher=Thread(target=main.queue_worker,daemon=True)
    dispatcher.start()
    try:
        assert orion_waiting.wait(3) and artemis_started.wait(3)
        assert not second_artemis.is_set()
        release_artemis.set()
        assert second_artemis.wait(3) and not orion_done.is_set()
        with main.db() as con:
            row=con.execute("SELECT command_id,payload_json FROM agent_commands WHERE node_id='orion'").fetchone()
            payload=json.loads(row['payload_json']); payload['_claim_deadline']='2000-01-01T00:00:00+00:00'
            con.execute('UPDATE agent_commands SET payload_json=? WHERE command_id=?',(json.dumps(payload),row['command_id']))
        assert orion_done.wait(3)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with main.db() as con: statuses=dict(con.execute('SELECT job_id,status FROM jobs').fetchall())
            if all(statuses[j] in {'success','failed'} for j in (first,second,third)): break
            time.sleep(.01)
        assert statuses[first]=='failed' and statuses[second]==statuses[third]=='success'
    finally:
        release_artemis.set(); main.worker_stop.set(); main.worker_wakeup.set(); dispatcher.join(3)
        assert not dispatcher.is_alive()


def test_offline_node_refused_before_queueing(job_database,monkeypatch):
    with main.db() as con: con.execute("UPDATE node_agents SET last_heartbeat='2000-01-01T00:00:00+00:00' WHERE node_id='orion'")
    job_id=main.create_job('image-jelly','delete','offline',node_id='orion')
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
    main.execute_job(job)
    with main.db() as con:
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='failed'
        assert con.execute('SELECT COUNT(*) FROM agent_commands').fetchone()[0]==0


def test_claimed_execution_timeout_is_failure_not_success(job_database,monkeypatch):
    command_id=main.queue_agent_command('orion','appbox_action',{'client_id':'image-jelly','action':'delete'})
    with main.db() as con: con.execute("UPDATE agent_commands SET status='claimed',claimed_at=? WHERE command_id=?",(main.now_iso(),command_id))
    ticks=iter([0,0,2])
    monkeypatch.setattr(main.time,'monotonic',lambda:next(ticks))
    monkeypatch.setattr(main.worker_stop,'wait',lambda seconds:False)
    ok,_,error=main.wait_agent_command(command_id,timeout=1)
    assert not ok and 'non confirmée' in error
    with main.db() as con: assert con.execute('SELECT status FROM agent_commands WHERE command_id=?',(command_id,)).fetchone()[0]=='failed'


def test_embedded_cleanup_reuses_agent_primitive_and_finishes_missing_path(job_database,monkeypatch):
    monkeypatch.setattr(main,'HOSTNAME','artemis')
    monkeypatch.setattr(main,'APPBOX_MODE','real')
    monkeypatch.setattr(main,'BASE_DIR',job_database)
    main.embedded_deletion_executor.cache_clear()
    cleanup=main.embedded_deletion_executor()
    from test_agent_deployment import DeletionDocker
    monkeypatch.setitem(cleanup.__globals__,'run',DeletionDocker())
    job_id=main.create_job('ab40ah','delete','local repair',node_id='artemis')
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
    main.execute_job(job)
    with main.db() as con: assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='success'
    assert all(s['status']=='success' for s in main.step_rows(job_id))
    main.embedded_deletion_executor.cache_clear()


def test_mock_embedded_cleanup_never_runs_docker(job_database,monkeypatch):
    monkeypatch.setattr(main,'HOSTNAME','artemis')
    monkeypatch.setattr(main,'APPBOX_MODE','mock')
    job_id=main.create_job('ab40ah','delete','mock',node_id='artemis')
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
    with patch.object(main,'embedded_deletion_executor') as cleanup:
        main.execute_job(job)
        cleanup.assert_not_called()
    with main.db() as con: assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='failed'


def test_queue_continues_on_same_node_after_worker_exception(job_database,monkeypatch):
    first=main.create_job('ab40ah','delete','failure',node_id='artemis')
    second=main.create_job('ab37ah','delete','next',node_id='artemis')
    done=Event()
    def execute(job):
        if job['job_id']==first: raise RuntimeError('injected executor failure')
        main.update_job(job['job_id'],'success',100,'continued')
        done.set()
    monkeypatch.setattr(main,'execute_job',execute)
    dispatcher=Thread(target=main.queue_worker,daemon=True); dispatcher.start()
    try:
        assert done.wait(3)
        with main.db() as con:
            assert con.execute('SELECT status FROM jobs WHERE job_id=?',(first,)).fetchone()[0]=='failed'
            assert con.execute('SELECT status FROM jobs WHERE job_id=?',(second,)).fetchone()[0]=='success'
    finally:
        main.worker_stop.set(); main.worker_wakeup.set(); dispatcher.join(3)


def test_timeout_claim_race_does_not_expire_claimed_execution(job_database,monkeypatch):
    monkeypatch.setattr(main,'AGENT_CLAIM_TIMEOUT_SECONDS',0)
    command_id=main.queue_agent_command('orion','appbox_action',{'client_id':'image-jelly','action':'delete'})
    fail=main.fail_pending_command
    def race(cid,message,queued_only=False):
        with main.db() as con: con.execute("UPDATE agent_commands SET status='claimed',claimed_at=? WHERE command_id=?",(main.now_iso(),cid))
        return fail(cid,message,queued_only)
    monkeypatch.setattr(main,'fail_pending_command',race)
    def finish(seconds):
        with main.db() as con: con.execute("UPDATE agent_commands SET status='success',result_json='{}' WHERE command_id=?",(command_id,))
    monkeypatch.setattr(main.worker_stop,'wait',finish)
    assert main.wait_agent_command(command_id,timeout=5)[0]


def test_restart_expires_malformed_command_without_blocking_startup(job_database):
    command_id=main.queue_agent_command('orion','appbox_action',{})
    with main.db() as con: con.execute("UPDATE agent_commands SET payload_json='invalid' WHERE command_id=?",(command_id,))
    assert main.recover_interrupted_jobs()==0
    with main.db() as con: assert con.execute('SELECT status FROM agent_commands WHERE command_id=?',(command_id,)).fetchone()[0]=='failed'


@pytest.mark.parametrize('proof',[{'path_exists':False}, {'path_exists':None,'containers_remaining':[]},
                                {'path_exists':False,'containers_remaining':['remaining']}])
def test_delete_missing_runtime_proof_never_commits_inventory(job_database,monkeypatch,proof):
    job_id=main.create_job('ab40ah','delete','proof',node_id='artemis')
    monkeypatch.setattr(main,'wait_agent_command',lambda *a,**kw:(True,proof,''))
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
    main.execute_job(job)
    with main.db() as con:
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='failed'
        assert con.execute("SELECT 1 FROM appboxes WHERE client_id='ab40ah'").fetchone()


@pytest.mark.parametrize('mode',['delete','purge'])
def test_repeated_delete_api_returns_verified_job_without_new_execution(job_database,monkeypatch,mode):
    request=AgentResultRequest({}); request.headers={'X-Requested-With':'XMLHttpRequest'}
    first=json.loads(main.delete_appbox(request,'ab40ah',mode,'SUPPRIMER').body)
    wait=main.wait_agent_command
    def deliver(command_id,**kwargs):
        complete_local_agent(command_id,job_database)
        return wait(command_id,**kwargs)
    monkeypatch.setattr(main,'wait_agent_command',deliver)
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(first['job_id'],)).fetchone())
    main.execute_job(job)
    second=json.loads(main.delete_appbox(request,'ab40ah',mode,'SUPPRIMER').body)
    assert second['already_deleted'] is True and second['job_id']==first['job_id']
    with main.db() as con:
        assert con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
        assert con.execute('SELECT COUNT(*) FROM agent_commands').fetchone()[0]==1
    with pytest.raises(main.HTTPException) as error:
        main.delete_appbox(request,'unknown',mode,'SUPPRIMER')
    assert error.value.status_code==404


def _create_remote_appbox(base, *, client_id, node='artemis', placement='manual',
                          port='32448', tautulli=False):
    return main.create_appbox(
        client_id=client_id, media_type='plex', profile_id='', deployment_image_id='',
        mount_group_id='', snapshot_id='', reference_version_id='', port_mode='manual',
        media_port_requested=port, acceleration_mode='disabled', placement_mode=placement,
        target_node_id=node, bare_metal_override=False, with_tautulli=tautulli,
        deploy_now=False,
    )


def test_manual_create_reserves_plex_and_tautulli_on_selected_node(job_database,monkeypatch):
    monkeypatch.setattr(main, 'BASE_DIR', job_database/'appboxes')
    response = _create_remote_appbox(job_database, client_id='jdmry', tautulli=True)
    assert response.status_code == 303
    with main.db() as con:
        item = con.execute("SELECT node_id,selected_node_id,status,desired_state,observed_state FROM appboxes WHERE client_id='jdmry'").fetchone()
        reservations = con.execute("SELECT node_id,service,status FROM port_reservations WHERE client_id='jdmry' ORDER BY service").fetchall()
    assert tuple(item) == ('artemis','artemis','generated','not_deployed','not_deployed')
    assert [(row['node_id'],row['service'],row['status']) for row in reservations] == [
        ('artemis','plex','reserved'), ('artemis','tautulli','reserved')]
    with main.db() as con:
        row=con.execute("SELECT * FROM appboxes WHERE client_id='jdmry'").fetchone()
    generated=main.row_to_appbox(row)
    assert generated['ux_state']=='ready_to_deploy'
    rendered=main.templates.env.get_template('command_center.html').render(
        appboxes=[{**generated,'runtime':{'status':'absent'}}],recent_jobs=[],
        running=0,claimed=0,stopped=0,hostname='cronos',
        node={'running_jobs':0,'queued_jobs':0,'rdad_ok':False,
              'metrics':{'cpu_percent':0,'ram_percent':0,'disk_percent':0,
                         'running_containers':0,'docker_containers':0}})
    assert 'CONFIGURATION CRÉÉE — NON DÉPLOYÉE' in rendered


def test_failed_remote_delete_cleans_verified_cp_workspace_and_allows_recreate(job_database,monkeypatch):
    root=job_database/'control-plane-runtime'; monkeypatch.setattr(main,'BASE_DIR',root)
    _create_remote_appbox(job_database,client_id='retry01',port='32461')
    workspace=root/'retry01'
    assert (workspace/main.CONTROL_PLANE_WORKSPACE_MARKER).is_file()
    with main.db() as con:
        con.execute("UPDATE appboxes SET status='error',last_message='failed deploy' WHERE client_id='retry01'")
    # The remote node proves its own path and containers are absent. It never
    # touches this central workspace; execute_remote_job must verify and remove it.
    monkeypatch.setattr(main,'wait_agent_command',lambda *args,**kwargs:
                        (True,{'output':'remote cleanup complete','path_exists':False,'containers_remaining':[]},''))
    deletion_job=main.create_job('retry01','delete','cleanup',node_id='artemis')
    with main.db() as con:
        job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(deletion_job,)).fetchone())
    main.execute_job(job)
    with main.db() as con:
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(deletion_job,)).fetchone()[0]=='success'
        assert not con.execute("SELECT 1 FROM appboxes WHERE client_id='retry01'").fetchone()
    assert not workspace.exists()
    assert _create_remote_appbox(job_database,client_id='retry01',port='32461').status_code==303

    orphan=root/'unsafe01'; orphan.mkdir(); (orphan/'operator-data').write_text('keep')
    with pytest.raises(main.HTTPException) as conflict:
        _create_remote_appbox(job_database,client_id='unsafe01',port='32462')
    assert conflict.value.status_code==409 and orphan.exists()


def test_automatic_create_uses_eligible_appbox_node_for_port(job_database,monkeypatch):
    monkeypatch.setattr(main, 'BASE_DIR', job_database/'automatic')
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("UPDATE nodes SET rdad_ok=1 WHERE node_id='artemis'")
        con.execute("INSERT INTO node_tag_assignments(node_id,tag_id,assigned_at) VALUES('artemis','appbox-node',?)",(stamp,))
        main.store_agent_metrics(con,'artemis',{'rdad_present':True,'docker_ok':True},'test',stamp)
    _create_remote_appbox(job_database,client_id='auto01',placement='automatic',node='orion',port='32449')
    with main.db() as con:
        appbox=con.execute("SELECT selected_node_id FROM appboxes WHERE client_id='auto01'").fetchone()[0]
        reservation=con.execute("SELECT node_id FROM port_reservations WHERE client_id='auto01'").fetchone()[0]
    assert appbox == reservation == 'artemis'


def test_port_reservation_reconciliation_handles_move_retry_delete_and_node_scope(job_database):
    stamp=main.now_iso()
    with main.db() as con:
        for client,node,port in (('same01','artemis',32455),('same02','orion',32455),('move01','artemis',32456)):
            con.execute("""INSERT INTO appboxes(client_id,node_id,selected_node_id,path,containers_json,status,plex_port,created_at,updated_at)
                VALUES(?,?,?,?,'[]','generated',?, ?,?)""",(client,node,node,str(job_database/client),port,stamp,stamp))
    assert main.sync_port_reservations() >= 3
    assert main.sync_port_reservations() >= 3
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM port_reservations WHERE port=32455 AND status='reserved'").fetchone()[0] == 2
        con.execute("UPDATE appboxes SET node_id='orion',selected_node_id='orion' WHERE client_id='move01'")
        con.execute("UPDATE appboxes SET status='deleted' WHERE client_id='same01'")
    main.sync_port_reservations()
    with main.db() as con:
        assert con.execute("SELECT status FROM port_reservations WHERE client_id='same01' ORDER BY reservation_id DESC LIMIT 1").fetchone()[0]=='released'
        assert tuple(con.execute("SELECT node_id,status FROM port_reservations WHERE client_id='move01' ORDER BY reservation_id DESC LIMIT 1").fetchone())==('orion','reserved')
        assert con.execute("SELECT COUNT(*) FROM port_reservations WHERE node_id='artemis' AND port=32456 AND status='reserved'").fetchone()[0]==0


def test_same_node_port_collision_and_creation_rollback(job_database,monkeypatch):
    monkeypatch.setattr(main,'BASE_DIR',job_database/'collisions')
    _create_remote_appbox(job_database,client_id='first01',port='32460')
    with pytest.raises(main.HTTPException) as collision:
        _create_remote_appbox(job_database,client_id='second01',port='32460')
    assert collision.value.status_code==409
    with main.db() as con:
        assert not con.execute("SELECT 1 FROM appboxes WHERE client_id='second01'").fetchone()
        assert not con.execute("SELECT 1 FROM port_reservations WHERE client_id='second01'").fetchone()
    assert not (job_database/'collisions'/'second01').exists()


def _insert_historical_appbox(con, client_id, status, port, base):
    stamp = main.now_iso()
    con.execute("""INSERT INTO appboxes(
        client_id,node_id,path,containers_json,status,plex_port,created_at,updated_at,
        desired_state,observed_state
    ) VALUES(?,'artemis',?,'[]',?,?,?,?,'deleted','missing')""",
        (client_id, str(base/client_id), status, port, stamp, stamp))


def _assert_creation_absent(base, client_id):
    with main.db() as con:
        for table in ('appboxes','port_reservations','appbox_mounts','jobs'):
            assert con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE client_id=?", (client_id,)
            ).fetchone()[0] == 0, table
        assert con.execute(
            "SELECT COUNT(*) FROM agent_commands WHERE payload_json LIKE ?",
            (f'%{client_id}%',),
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM placement_decisions WHERE client_id=?", (client_id,)
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM control_plane_deployments WHERE client_id=?", (client_id,)
        ).fetchone()[0] == 0
    assert not (base/client_id).exists()


def test_deleted_appbox_with_released_reservation_allows_automatic_port_reuse(job_database,monkeypatch):
    root = job_database/'deleted-port-reuse'
    monkeypatch.setattr(main, 'BASE_DIR', root)
    stamp = main.now_iso()
    with main.db() as con:
        _insert_historical_appbox(con, 'testlab01', 'deleted', 32436, root)
        con.execute("""INSERT INTO port_reservations(
            node_id,client_id,service,port,protocol,status,reserved_at,released_at
        ) VALUES('artemis','testlab01','plex',32436,'tcp','released',?,?)""", (stamp, stamp))
        _insert_historical_appbox(con, 'portguard', 'stopped', 32435, root)

    response = main.create_appbox(
        client_id='p0e2e01', media_type='plex', profile_id='', deployment_image_id='',
        mount_group_id='', snapshot_id='', reference_version_id='', port_mode='automatic',
        media_port_requested='', acceleration_mode='disabled', placement_mode='manual',
        target_node_id='artemis', bare_metal_override=False, with_tautulli=False,
        deploy_now=False,
    )
    assert response.status_code == 303
    with main.db() as con:
        created = con.execute(
            "SELECT plex_port,status FROM appboxes WHERE client_id='p0e2e01'"
        ).fetchone()
        reservation = con.execute("""SELECT port,status FROM port_reservations
            WHERE client_id='p0e2e01' AND service='plex'""").fetchone()
    assert tuple(created) == (32436, 'generated')
    assert tuple(reservation) == (32436, 'reserved')


@pytest.mark.parametrize('status', ['stopped', 'error', 'missing', 'not_deployed'])
def test_non_terminal_appbox_status_keeps_port_reserved_for_allocator(job_database,monkeypatch,status):
    root = job_database/f'active-port-{status}'
    monkeypatch.setattr(main, 'BASE_DIR', root)
    with main.db() as con:
        _insert_historical_appbox(con, 'portguard', 'running', 32435, root)
        _insert_historical_appbox(con, 'existing01', status, 32436, root)

    response = main.create_appbox(
        client_id='nextport', media_type='plex', profile_id='', deployment_image_id='',
        mount_group_id='', snapshot_id='', reference_version_id='', port_mode='automatic',
        media_port_requested='', acceleration_mode='disabled', placement_mode='manual',
        target_node_id='artemis', bare_metal_override=False, with_tautulli=False,
        deploy_now=False,
    )
    assert response.status_code == 303
    with main.db() as con:
        assert con.execute(
            "SELECT plex_port FROM appboxes WHERE client_id='nextport'"
        ).fetchone()[0] == 32437


def test_concurrent_port_collision_after_allocation_returns_409_and_rolls_back(job_database,monkeypatch):
    root = job_database/'concurrent-collision'
    monkeypatch.setattr(main, 'BASE_DIR', root)
    with main.db() as con:
        con.execute("""CREATE TRIGGER concurrent_port_owner BEFORE INSERT ON appboxes
            WHEN NEW.client_id='race02'
            BEGIN
                INSERT INTO appboxes(
                    client_id,node_id,media_type,with_tautulli,plex_port,status,
                    path,containers_json,created_at,updated_at
                ) VALUES(
                    'race-owner',NEW.node_id,'plex',0,NEW.plex_port,'generated',
                    'trigger-owner','[]',datetime('now'),datetime('now')
                );
            END""")
    with pytest.raises(main.HTTPException) as conflict:
        _create_remote_appbox(job_database, client_id='race02', port='32470')
    assert conflict.value.status_code == 409
    assert 'Conflit de création' in conflict.value.detail
    _assert_creation_absent(root, 'race02')
    with main.db() as con:
        assert not con.execute("SELECT 1 FROM appboxes WHERE client_id='race-owner'").fetchone()


@pytest.mark.parametrize('table', ['appboxes', 'port_reservations', 'appbox_mounts'])
def test_integrity_failure_after_workspace_generation_rolls_back_db_and_files(job_database,monkeypatch,table):
    root = job_database/f'forced-{table}'
    monkeypatch.setattr(main, 'BASE_DIR', root)
    client_id = {'appboxes':'failapp', 'port_reservations':'failport', 'appbox_mounts':'failmount'}[table]
    with main.db() as con:
        con.execute(f"""CREATE TRIGGER force_{table}_failure BEFORE INSERT ON {table}
            WHEN NEW.client_id='{client_id}'
            BEGIN SELECT RAISE(ABORT,'forced {table} integrity failure'); END""")
    with pytest.raises(main.HTTPException) as conflict:
        _create_remote_appbox(job_database, client_id=client_id, port='32471')
    assert conflict.value.status_code == 409
    _assert_creation_absent(root, client_id)


def test_marker_write_failure_removes_only_empty_workspace_without_recursive_delete(job_database,monkeypatch):
    root = job_database/'marker-failure'
    monkeypatch.setattr(main, 'BASE_DIR', root)
    original_write_text = Path.write_text

    def fail_marker_staging(path, *args, **kwargs):
        if (
            path.parent == root/'markfail'
            and path.name.startswith(f'.{main.CONTROL_PLANE_WORKSPACE_MARKER}.')
        ):
            raise OSError('forced marker write failure')
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', fail_marker_staging)
    with pytest.raises(OSError, match='forced marker write failure'):
        _create_remote_appbox(job_database, client_id='markfail', port='32473')
    _assert_creation_absent(root, 'markfail')


@pytest.mark.parametrize('marker', [
    {'schema_version':1, 'client_id':'existing01', 'node_id':'artemis'},
    {'schema_version':1, 'client_id':'wrong-client', 'node_id':'artemis'},
])
def test_preexisting_workspace_is_never_removed_by_creation_rollback(job_database,monkeypatch,marker):
    root = job_database/'preexisting-workspace'
    monkeypatch.setattr(main, 'BASE_DIR', root)
    workspace = root/'existing01'
    workspace.mkdir(parents=True)
    (workspace/main.CONTROL_PLANE_WORKSPACE_MARKER).write_text(json.dumps(marker), encoding='utf-8')
    sentinel = workspace/'operator-data'
    sentinel.write_text('keep', encoding='utf-8')
    with pytest.raises(main.HTTPException) as conflict:
        _create_remote_appbox(job_database, client_id='existing01', port='32472')
    assert conflict.value.status_code == 409
    assert sentinel.read_text(encoding='utf-8') == 'keep'
    with main.db() as con:
        assert not con.execute("SELECT 1 FROM appboxes WHERE client_id='existing01'").fetchone()


def test_port_index_migration_preserves_deleted_history_and_frees_plex_and_tautulli(job_database):
    with main.db() as con:
        con.executescript("""
            DROP INDEX ux_appbox_node_plex_port;
            CREATE UNIQUE INDEX ux_appbox_node_plex_port
                ON appboxes(node_id,plex_port) WHERE plex_port IS NOT NULL;
            DROP INDEX ux_appbox_node_tautulli_port;
            CREATE UNIQUE INDEX ux_appbox_node_tautulli_port
                ON appboxes(node_id,tautulli_port) WHERE tautulli_port IS NOT NULL;
        """)
        stamp = main.now_iso()
        con.execute("""INSERT INTO appboxes(
            client_id,node_id,path,containers_json,status,plex_port,tautulli_port,created_at,updated_at
        ) VALUES('legacy-deleted','artemis','legacy','[]','deleted',32480,8190,?,?)""", (stamp,stamp))

    main.init_database()
    with main.db() as con:
        index_sql = {
            row['name']: row['sql'] for row in con.execute("""SELECT name,sql FROM sqlite_master
                WHERE type='index' AND name IN (
                    'ux_appbox_node_plex_port','ux_appbox_node_tautulli_port'
                )""").fetchall()
        }
        assert all("status != 'deleted'" in sql for sql in index_sql.values())
        stamp = main.now_iso()
        con.execute("""INSERT INTO appboxes(
            client_id,node_id,path,containers_json,status,plex_port,tautulli_port,created_at,updated_at
        ) VALUES('new-active','artemis','active','[]','generated',32480,8190,?,?)""", (stamp,stamp))
        assert tuple(con.execute(
            "SELECT plex_port,tautulli_port FROM appboxes WHERE client_id='legacy-deleted'"
        ).fetchone()) == (32480,8190)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("""INSERT INTO appboxes(
                client_id,node_id,path,containers_json,status,plex_port,created_at,updated_at
            ) VALUES('second-active','artemis','second','[]','error',32480,?,?)""", (stamp,stamp))


def test_appbox_lease_progress_expiry_late_result_and_restart_are_terminal(job_database):
    job_id=main.create_job('ab40ah','deploy','lease',node_id='artemis')
    main.update_job(job_id,'running',10,'started')
    with main.db() as con:
        con.execute("UPDATE node_agents SET capabilities_json=? WHERE node_id='artemis'",(json.dumps({'deployment_executor':True,'appbox_command_lease':True}),))
        con.execute("""INSERT INTO control_plane_deployments(deployment_id,client_id,node_id,status,progress,created_at,updated_at)
            VALUES('lease-deployment','ab40ah','artemis','prepared',0,?,?)""",(main.now_iso(),main.now_iso()))
    command_id=main.queue_agent_command('artemis','appbox_action',{'_job_id':job_id,'client_id':'ab40ah','action':'deploy'})
    with patch.object(main,'authenticate_agent'):
        claimed=json.loads(main.agent_poll_commands('artemis',AgentResultRequest({})).body)['command']
        assert claimed['command_id']==command_id
        assert claimed['command_deadline_at']
        asyncio.run(main.agent_command_progress('artemis',command_id,AgentResultRequest({'stage':'preparing','percent':5,'detail':'preparing'})))
        asyncio.run(main.agent_command_progress('artemis',command_id,AgentResultRequest({'stage':'checksum_reference','percent':63,'detail':'checksum'})))
        asyncio.run(main.agent_command_progress('artemis',command_id,AgentResultRequest({'stage':'checksum_reference','percent':20,'detail':'old'})))
    with main.db() as con:
        command=con.execute("SELECT lease_expires_at,worker_activity_at,progress_json,command_deadline_at FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()
        claimed_lease=command['lease_expires_at']
        step=con.execute("SELECT progress FROM job_steps WHERE job_id=? AND step_key='checksum_reference'",(job_id,)).fetchone()[0]
        docker=con.execute("SELECT status FROM job_steps WHERE job_id=? AND step_key='docker_deploy'",(job_id,)).fetchone()[0]
        assert command['lease_expires_at'] and command['worker_activity_at'] is None and command['command_deadline_at'] and step==63
        assert docker == 'pending'
    # UX progress cannot renew ownership. A heartbeat from the runtime that owns
    # this exact command does, without changing functional progress.
    with patch.object(main,'authenticate_agent'):
        heartbeat_response=asyncio.run(main.agent_heartbeat('artemis',AgentResultRequest({
            'agent_version':'test','active_command_id':command_id,
            'capabilities':{'deployment_executor':True,'appbox_command_lease':True},
        })))
        heartbeat=json.loads(heartbeat_response.body)
        assert heartbeat['status']=='ok'
    with main.db() as con:
        renewed=con.execute("SELECT lease_expires_at,worker_activity_at,progress_json FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()
        assert renewed['lease_expires_at'] >= claimed_lease
        assert renewed['worker_activity_at'] is not None
        assert json.loads(renewed['progress_json'])['percent']==20
    with patch.object(main,'authenticate_agent'):
        asyncio.run(main.agent_command_progress('artemis',command_id,AgentResultRequest({'stage':'compose_deployment','percent':10,'detail':'compose'})))
    with main.db() as con:
        assert con.execute("SELECT status FROM job_steps WHERE job_id=? AND step_key='docker_deploy'",(job_id,)).fetchone()[0]=='running'
        con.execute("UPDATE agent_commands SET lease_expires_at='2000-01-01T00:00:00+00:00',command_deadline_at='2000-01-01T00:00:00+00:00' WHERE command_id=?",(command_id,))
    with patch.object(main,'authenticate_agent'):
        asyncio.run(main.agent_heartbeat('artemis',AgentResultRequest({
            'agent_version':'test','active_command_id':command_id,
            'capabilities':{'deployment_executor':True,'appbox_command_lease':True},
        })))
    with main.db() as con:
        assert con.execute("SELECT lease_expires_at FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()[0]=='2000-01-01T00:00:00+00:00'
    assert main.expire_appbox_command_leases()==1
    stalled=next(item for item in main.list_control_nodes() if item['node_id']=='artemis')
    assert stalled['status']=='online' and stalled['executor_health']=='stalled' and not stalled['execution_capable']
    with main.db() as con:
        before=tuple(con.execute("SELECT status,last_message FROM appboxes WHERE client_id='ab40ah'").fetchone())
    with patch.object(main,'authenticate_agent'):
        response=asyncio.run(main.agent_command_result('artemis',command_id,AgentResultRequest({'status':'success','result':{'state':'running'}})))
        assert json.loads(response.body)['ignored']=='terminal_command'
        assert json.loads(main.agent_poll_commands('artemis',AgentResultRequest({})).body)['command'] is None
    recovered=next(item for item in main.list_control_nodes() if item['node_id']=='artemis')
    assert recovered['executor_health']=='healthy' and recovered['execution_capable']
    with main.db() as con:
        assert con.execute("SELECT status FROM jobs WHERE job_id=?",(job_id,)).fetchone()[0]=='failed'
        assert con.execute("SELECT status FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()[0]=='failed'
        assert con.execute("SELECT status FROM control_plane_deployments WHERE deployment_id='lease-deployment'").fetchone()[0]=='failed'
        assert tuple(con.execute("SELECT status,last_message FROM appboxes WHERE client_id='ab40ah'").fetchone())==before
        assert con.execute("SELECT COUNT(*) FROM events WHERE event_type='late_agent_result_ignored'").fetchone()[0]==1


def test_executor_health_is_distinct_from_agent_liveness(job_database):
    with main.db() as con:
        con.execute("UPDATE node_agents SET capabilities_json=? WHERE node_id='artemis'",(json.dumps({'deployment_executor':True,'appbox_command_lease':True}),))
    command=main.queue_agent_command('artemis','appbox_action',{'action':'deploy'})
    with main.db() as con:
        con.execute("UPDATE agent_commands SET status='claimed',claimed_at=?,lease_expires_at='2000-01-01T00:00:00+00:00' WHERE command_id=?",(main.now_iso(),command))
    node=next(item for item in main.list_control_nodes() if item['node_id']=='artemis')
    assert node['status']=='online' and node['agent_online']
    assert node['executor_health']=='stalled' and node['worker_lease_status']=='stalled'
    assert not node['execution_capable'] and not node['actionable']


def test_delivery_offer_lost_before_client_receives_response_is_reoffered(job_database):
    with main.db() as con:
        con.execute("UPDATE node_agents SET capabilities_json=? WHERE node_id='artemis'",
                    (json.dumps({'deployment_executor':True,'appbox_command_lease':True,'appbox_delivery_ack':True}),))
    command_id=main.queue_agent_command('artemis','appbox_action',{'action':'deploy'})
    with patch.object(main,'authenticate_agent'):
        first=json.loads(main.agent_poll_commands('artemis',AgentResultRequest({})).body)['command']
    assert first['status']=='offered' and first['delivery_token']
    with main.db() as con:
        row=con.execute("SELECT status,claimed_at,worker_activity_at,lease_expires_at FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()
        assert tuple(row)==('offered',None,None,None)
        con.execute("UPDATE agent_commands SET delivery_ack_deadline='2000-01-01T00:00:00+00:00' WHERE command_id=?",(command_id,))
    with patch.object(main,'authenticate_agent'):
        second=json.loads(main.agent_poll_commands('artemis',AgentResultRequest({})).body)['command']
        with pytest.raises(main.HTTPException) as old_offer:
            asyncio.run(main.agent_ack_command_delivery('artemis',command_id,AgentResultRequest({'delivery_token':first['delivery_token']})))
        acknowledged=json.loads(asyncio.run(main.agent_ack_command_delivery(
            'artemis',command_id,AgentResultRequest({'delivery_token':second['delivery_token']}))).body)
        repeated=json.loads(asyncio.run(main.agent_ack_command_delivery(
            'artemis',command_id,AgentResultRequest({'delivery_token':second['delivery_token']}))).body)
    assert old_offer.value.status_code==409
    assert second['delivery_token']!=first['delivery_token']
    assert acknowledged['status']=='claimed' and not acknowledged['idempotent']
    assert repeated['status']=='claimed' and repeated['idempotent']
    with main.db() as con:
        row=con.execute("SELECT status,delivery_attempts,delivery_acknowledged_at,worker_activity_at,command_deadline_at FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()
        assert row['status']=='claimed' and row['delivery_attempts']==2
        assert row['delivery_acknowledged_at'] and row['worker_activity_at'] and row['command_deadline_at']


def test_delivery_columns_migrate_additively_without_changing_legacy_commands(job_database):
    stamp=main.now_iso()
    with main.db() as con:
        con.execute('DROP TABLE agent_commands')
        con.execute('''CREATE TABLE agent_commands (
            command_id TEXT PRIMARY KEY,node_id TEXT NOT NULL,command_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,claimed_at TEXT,completed_at TEXT,result_json TEXT,error_text TEXT,
            FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE)''')
        con.execute("INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at) VALUES('legacy-delivery','artemis','appbox_action','{\"action\":\"start\"}','queued',?)",(stamp,))
    main.init_database()
    with main.db() as con:
        columns={row['name'] for row in con.execute('PRAGMA table_info(agent_commands)')}
        command=con.execute("SELECT * FROM agent_commands WHERE command_id='legacy-delivery'").fetchone()
    assert {'delivery_token_hash','delivery_offered_at','delivery_ack_deadline',
            'delivery_acknowledged_at','delivery_attempts'} <= columns
    assert command['status']=='queued' and json.loads(command['payload_json'])=={'action':'start'}
    assert command['delivery_token_hash'] is None and command['delivery_attempts']==0


def test_ack_then_silent_worker_heartbeat_renews_lease_and_dead_worker_expires(job_database):
    job_id=main.create_job('ab40ah','deploy','delivery',node_id='artemis')
    main.update_job(job_id,'running',7,'Préparation de la référence')
    with main.db() as con:
        con.execute("UPDATE node_agents SET capabilities_json=? WHERE node_id='artemis'",
                    (json.dumps({'deployment_executor':True,'appbox_command_lease':True,'appbox_delivery_ack':True}),))
    command_id=main.queue_agent_command('artemis','appbox_action',{'_job_id':job_id,'client_id':'ab40ah','action':'deploy'})
    with patch.object(main,'authenticate_agent'):
        offer=json.loads(main.agent_poll_commands('artemis',AgentResultRequest({})).body)['command']
        asyncio.run(main.agent_ack_command_delivery('artemis',command_id,AgentResultRequest({'delivery_token':offer['delivery_token']})))
    with main.db() as con:
        before=con.execute("SELECT lease_expires_at,worker_activity_at,progress_json,command_deadline_at FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()
    with patch.object(main,'authenticate_agent'):
        heartbeat=json.loads(asyncio.run(main.agent_heartbeat('artemis',AgentResultRequest({
            'agent_version':'test','active_command_id':command_id,
            'capabilities':{'deployment_executor':True,'appbox_command_lease':True,'appbox_delivery_ack':True},
        }))).body)
    assert heartbeat['active_command_state']=='lease_renewed' and heartbeat['worker_activity_at']
    with main.db() as con:
        after=con.execute("SELECT lease_expires_at,worker_activity_at,progress_json,command_deadline_at FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()
        assert after['lease_expires_at']>=before['lease_expires_at']
        assert after['worker_activity_at']>=before['worker_activity_at']
        assert after['progress_json']=='{}' and after['command_deadline_at']==before['command_deadline_at']
        con.execute("UPDATE agent_commands SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE command_id=?",(command_id,))
    assert main.expire_appbox_command_leases()==1
    with main.db() as con:
        assert con.execute("SELECT status FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()[0]=='failed'
        job=con.execute("SELECT * FROM jobs WHERE job_id=?",(job_id,)).fetchone()
    delivery=main.job_dict(job,include_steps=True)['command_delivery']
    assert delivery['delivery_acknowledged_at'] and delivery['worker_activity_at']


def test_generated_deleted_capacity_remote_job_counts_and_legacy_error_migration(job_database):
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO appboxes(client_id,node_id,path,containers_json,status,desired_state,observed_state,created_at,updated_at) VALUES('generated01','artemis','x','[]','generated','not_deployed','not_deployed',?,?)",(stamp,stamp))
        con.execute("INSERT INTO appboxes(client_id,node_id,path,containers_json,status,desired_state,observed_state,created_at,updated_at) VALUES('deleted01','artemis','x','[]','deleted','deleted','missing',?,?)",(stamp,stamp))
        for index,status in enumerate(('running','queued','success','failed','error','cancelled')):
            con.execute("INSERT INTO jobs(job_id,node_id,action,title,status,progress,detail,created_at,updated_at) VALUES(?,?,'x','x',?,0,'',?,?)",(f'count-{index}','artemis',status,stamp,stamp))
    node=next(item for item in main.list_control_nodes() if item['node_id']=='artemis')
    assert node['not_deployed_appboxes']==1 and node['deleted_appboxes']==1
    assert node['appbox_count']==2  # two legacy active fixture rows; history/generated excluded
    status=json.loads(main.api_node_status('artemis').body)
    assert status['running_jobs']==1 and status['queued_jobs']==1
    main.init_database()
    with main.db() as con:
        assert con.execute("SELECT status FROM jobs WHERE job_id='count-4'").fetchone()[0]=='failed'
    assert main.active_job_for('generated01') is None


def test_reconciliation_stopped_has_no_port_drift_and_deleted_is_non_destructive(job_database):
    stamp=main.now_iso(); deleted_path=job_database/'deleted-present'; deleted_path.mkdir()
    with main.db() as con:
        con.execute("UPDATE appboxes SET status='stopped',desired_state='stopped',observed_state='stopped',plex_port=32448,containers_json='[\"plex-appb-40ah\"]' WHERE client_id='ab40ah'")
        con.execute("""INSERT INTO containers(container_id,node_id,appbox_id,name,state,ports_json,labels_json,mounts_json,networks_json,last_seen,updated_at)
            VALUES('c-stopped','artemis','ab40ah','plex-appb-40ah','exited','[]','{}','[]','[]',?,?)""",(stamp,stamp))
        con.execute("UPDATE appboxes SET status='deleted',desired_state='running',observed_state='unknown',path=? WHERE client_id='ab37ah'",(str(deleted_path),))
    main.reconcile_node('artemis')
    with main.db() as con:
        stopped=con.execute("SELECT reconciliation_status,drift_json FROM appboxes WHERE client_id='ab40ah'").fetchone()
        deleted=con.execute("SELECT desired_state,observed_state,reconciliation_status FROM appboxes WHERE client_id='ab37ah'").fetchone()
    assert stopped['reconciliation_status']=='in_sync' and 'port_drift' not in stopped['drift_json']
    assert tuple(deleted)==('deleted','present','cleanup_required') and deleted_path.exists()
