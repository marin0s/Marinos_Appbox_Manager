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
        assert result['status']=='error' and result['progress']==100
        assert main.CLAIM_TIMEOUT_MESSAGE in result['detail']
        assert command['status']=='failed' and command['claimed_at'] is None
    assert all(s['status'] in {'success','failed','skipped'} for s in main.step_rows(job_id))
    with patch.object(main,'authenticate_agent'):
        assert json.loads(main.agent_poll_commands('orion',AgentResultRequest({})).body)['command'] is None
        response=asyncio.run(main.agent_command_result('orion',command['command_id'],AgentResultRequest({'status':'success','result':{}})))
        assert json.loads(response.body)['ignored']=='terminal_command'
    with main.db() as con:
        assert con.execute('SELECT status FROM agent_commands WHERE command_id=?',(command['command_id'],)).fetchone()[0]=='failed'
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='error'


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
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(interrupted,)).fetchone()[0]=='error'
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
            if all(statuses[j] in {'success','error'} for j in (first,second,third)): break
            time.sleep(.01)
        assert statuses[first]=='error' and statuses[second]==statuses[third]=='success'
    finally:
        release_artemis.set(); main.worker_stop.set(); main.worker_wakeup.set(); dispatcher.join(3)
        assert not dispatcher.is_alive()


def test_offline_node_refused_before_queueing(job_database,monkeypatch):
    with main.db() as con: con.execute("UPDATE node_agents SET last_heartbeat='2000-01-01T00:00:00+00:00' WHERE node_id='orion'")
    job_id=main.create_job('image-jelly','delete','offline',node_id='orion')
    with main.db() as con: job=dict(con.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone())
    main.execute_job(job)
    with main.db() as con:
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='error'
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
    with main.db() as con: assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='error'


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
            assert con.execute('SELECT status FROM jobs WHERE job_id=?',(first,)).fetchone()[0]=='error'
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
        assert con.execute('SELECT status FROM jobs WHERE job_id=?',(job_id,)).fetchone()[0]=='error'
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
