import asyncio
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import main
from reference_fixtures import reference_archive
from test_agent_deployment import agent


@pytest.fixture
def reliable_build(tmp_path, monkeypatch):
    old_db, old_root = main.DB_FILE, main.REFERENCE_ROOT
    main.DB_FILE, main.REFERENCE_ROOT = tmp_path / 'manager.db', tmp_path / 'references'
    main.init_database()
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('ouranos','OURANOS','remote','online',?,?)",(stamp,stamp))
    build_id=main.create_reference_build_draft(source_node_id='ouranos',display_name='Reliable Plex')
    job_id=main.create_job(None,'reference_build','Reliable Plex',node_id='ouranos')
    with main.db() as con:
        con.execute("UPDATE jobs SET status='running',started_at=? WHERE job_id=?",(stamp,job_id))
        con.execute("UPDATE reference_builds SET job_id=?,status='building',current_stage='capture',progress=25 WHERE build_id=?",(job_id,build_id))
        con.execute("UPDATE job_steps SET status='running',started_at=? WHERE job_id=? AND step_key='capture'",(stamp,job_id))
        payload=json.dumps({'build_id':build_id})
        con.execute("""INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,
            created_at,claimed_at,delivery_acknowledged_at,worker_activity_at,lease_expires_at)
            VALUES('capture','ouranos','reference_build',?,'claimed',?,?,?,?,?)""",
            (payload,stamp,stamp,stamp,stamp,(datetime.now(timezone.utc)+timedelta(seconds=180)).isoformat()))
    yield SimpleNamespace(build_id=build_id,job_id=job_id,tmp=tmp_path)
    main.DB_FILE, main.REFERENCE_ROOT = old_db, old_root


class JsonRequest:
    def __init__(self,payload): self.payload=payload
    async def json(self): return self.payload


def enable_reference_delivery(command_id='capture'):
    capabilities={
        'reference_builder_foundation':True,
        'reference_build_command_lease':True,
        'reference_build_delivery_ack':True,
        'independent_heartbeat':True,
    }
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO node_agents(node_id,status,last_heartbeat,capabilities_json,updated_at)
            VALUES('ouranos','online',?,?,?) ON CONFLICT(node_id) DO UPDATE SET
            status='online',last_heartbeat=excluded.last_heartbeat,
            capabilities_json=excluded.capabilities_json,updated_at=excluded.updated_at""",
            (stamp,json.dumps(capabilities),stamp))
        con.execute("""UPDATE agent_commands SET status='queued',claimed_at=NULL,completed_at=NULL,
            lease_expires_at=NULL,worker_activity_at=NULL,command_deadline_at=NULL,
            delivery_token_hash=NULL,delivery_offered_at=NULL,delivery_ack_deadline=NULL,
            delivery_acknowledged_at=NULL,delivery_attempts=0,error_text=NULL
            WHERE command_id=?""",(command_id,))
    return capabilities


def offer_and_ack_reference(capabilities, command_id='capture'):
    with patch.object(main,'authenticate_agent'):
        first=json.loads(main.agent_poll_commands('ouranos',JsonRequest({})).body)['command']
    assert first['status']=='offered' and first['delivery_token']
    with main.db() as con:
        offered=con.execute("SELECT claimed_at,worker_activity_at,lease_expires_at FROM agent_commands WHERE command_id=?",(command_id,)).fetchone()
        assert tuple(offered)==(None,None,None)
        con.execute("UPDATE agent_commands SET delivery_ack_deadline='2000-01-01T00:00:00+00:00' WHERE command_id=?",(command_id,))
    with patch.object(main,'authenticate_agent'):
        second=json.loads(main.agent_poll_commands('ouranos',JsonRequest({})).body)['command']
        with pytest.raises(main.HTTPException) as old:
            asyncio.run(main.agent_ack_command_delivery('ouranos',command_id,JsonRequest({'delivery_token':first['delivery_token']})))
        claimed=json.loads(asyncio.run(main.agent_ack_command_delivery(
            'ouranos',command_id,JsonRequest({'delivery_token':second['delivery_token']}))).body)
        repeated=json.loads(asyncio.run(main.agent_ack_command_delivery(
            'ouranos',command_id,JsonRequest({'delivery_token':second['delivery_token']}))).body)
    assert old.value.status_code==409 and first['delivery_token']!=second['delivery_token']
    assert claimed['status']=='claimed' and not claimed['idempotent']
    assert repeated['status']=='claimed' and repeated['idempotent']
    return second


def test_capture_progress_is_real_and_globally_monotone(reliable_build):
    with patch.object(main,'authenticate_agent'):
        asyncio.run(main.agent_command_progress('ouranos','capture',JsonRequest({'stage':'capture','bytes_written':500,'estimated_payload_bytes':1000})))
        asyncio.run(main.agent_command_progress('ouranos','capture',JsonRequest({'stage':'capture','bytes_written':100,'estimated_payload_bytes':1000})))
    with main.db() as con:
        build=con.execute("SELECT progress,status FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()
        job=con.execute("SELECT progress,status FROM jobs WHERE job_id=?",(reliable_build.job_id,)).fetchone()
        step=con.execute("SELECT progress,status FROM job_steps WHERE job_id=? AND step_key='capture'",(reliable_build.job_id,)).fetchone()
    assert tuple(build)==(50,'building') and tuple(job)==(50,'running')
    assert tuple(step)==(50,'running')


def test_heartbeat_renews_lease_and_returns_cancellation(reliable_build):
    payload={'agent_version':'test','capabilities':{},'active_command_id':'capture'}
    with patch.object(main,'authenticate_agent'):
        response=asyncio.run(main.agent_heartbeat('ouranos',JsonRequest(payload)))
    assert json.loads(response.body)['cancel_active_command'] is False
    with main.db() as con:
        assert con.execute("SELECT lease_expires_at FROM agent_commands WHERE command_id='capture'").fetchone()[0]
        con.execute("UPDATE agent_commands SET cancel_requested_at=? WHERE command_id='capture'",(main.now_iso(),))
    with patch.object(main,'authenticate_agent'):
        response=asyncio.run(main.agent_heartbeat('ouranos',JsonRequest(payload)))
    assert json.loads(response.body)['cancel_active_command'] is True


def test_reference_capture_can_run_for_virtual_hours_with_fixed_progress(reliable_build):
    capabilities=enable_reference_delivery()
    base=datetime.now(timezone.utc); clock=[base]
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return clock[0]
    with patch.object(main,'datetime',Clock):
        offer_and_ack_reference(capabilities)
        with main.db() as con:
            acknowledged=con.execute("SELECT worker_activity_at,lease_expires_at,command_deadline_at FROM agent_commands WHERE command_id='capture'").fetchone()
        assert acknowledged['worker_activity_at'] and acknowledged['lease_expires_at']
        assert acknowledged['command_deadline_at'] is None
        first_activity=acknowledged['worker_activity_at']
        for _ in range(181):
            clock[0]+=timedelta(seconds=60)
            with patch.object(main,'authenticate_agent'):
                heartbeat=json.loads(asyncio.run(main.agent_heartbeat('ouranos',JsonRequest({
                    'agent_version':'test','active_command_id':'capture','capabilities':capabilities}))).body)
            assert heartbeat['active_command_state']=='lease_renewed'
            assert main.expire_reference_command_leases(now=clock[0])==0
        with main.db() as con:
            command=con.execute("SELECT worker_activity_at,lease_expires_at,progress_json FROM agent_commands WHERE command_id='capture'").fetchone()
            build=con.execute("SELECT status,progress,updated_at FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()
            job=con.execute("SELECT status,progress,updated_at FROM jobs WHERE job_id=?",(reliable_build.job_id,)).fetchone()
        assert command['worker_activity_at']>first_activity
        assert datetime.fromisoformat(command['lease_expires_at'])>clock[0]
        assert command['progress_json']=='{}' and tuple(build[:2])==('building',25)
        assert tuple(job[:2])==('running',0)
        assert build['updated_at']==job['updated_at']==clock[0].isoformat()

        data=reference_archive(reliable_build.tmp).read_bytes(); checksum=hashlib.sha256(data).hexdigest()
        class UploadRequest:
            headers={'X-Reference-SHA256':checksum}
            async def stream(self):
                for offset in range(0,len(data),97): yield data[offset:offset+97]
        with patch.object(main,'authenticate_agent'):
            uploaded=asyncio.run(main.upload_reference_build_archive('ouranos',reliable_build.build_id,UploadRequest()))
            assert uploaded.status_code==200
            result={'sha256':checksum,'uncompressed_size_bytes':len(data),'sanitization':{'source_unchanged':True},
                    'builder_version':'test','manifest':{'metadata':{}}}
            completed=asyncio.run(main.agent_command_result('ouranos','capture',JsonRequest({'status':'success','result':result})))
            retry=asyncio.run(main.agent_command_result('ouranos','capture',JsonRequest({'status':'success','result':result})))
        assert completed.status_code==200 and json.loads(retry.body)['ignored']=='terminal_command'
    with main.db() as con:
        assert con.execute("SELECT status FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()[0]=='published'
        assert con.execute('SELECT COUNT(*) FROM reference_image_versions').fetchone()[0]==1


def test_reference_legacy_agent_is_not_given_an_unrenewable_lease(reliable_build):
    stamp=main.now_iso()
    with main.db() as con:
        con.execute("""INSERT INTO node_agents(node_id,status,last_heartbeat,capabilities_json,updated_at)
            VALUES('ouranos','online',?,'{}',?) ON CONFLICT(node_id) DO UPDATE SET
            last_heartbeat=excluded.last_heartbeat,capabilities_json='{}',updated_at=excluded.updated_at""",(stamp,stamp))
        con.execute("""UPDATE agent_commands SET status='queued',claimed_at=NULL,
            lease_expires_at=NULL,worker_activity_at=NULL,delivery_acknowledged_at=NULL
            WHERE command_id='capture'""")
    with patch.object(main,'authenticate_agent'):
        command=json.loads(main.agent_poll_commands('ouranos',JsonRequest({})).body)['command']
    # Legacy poll responses keep their historical pre-update status field; the
    # persisted command is claimed, but without an unrenewable lease.
    assert command['status']=='queued' and command['lease_timeout_seconds'] is None
    with main.db() as con:
        row=con.execute("SELECT status,lease_expires_at,worker_activity_at FROM agent_commands WHERE command_id='capture'").fetchone()
    assert tuple(row)==('claimed',None,None)
    # A lease left by the pre-fix Control Plane has no delivery ACK proof and
    # must not terminalize an in-flight legacy capture during rolling upgrade.
    with main.db() as con:
        con.execute("UPDATE agent_commands SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE command_id='capture'")
    assert main.expire_reference_command_leases()==0


def test_dead_reference_worker_expires_requests_cancel_and_rejects_late_upload(reliable_build):
    capabilities=enable_reference_delivery(); offer_and_ack_reference(capabilities)
    with main.db() as con:
        lease=con.execute("SELECT lease_expires_at FROM agent_commands WHERE command_id='capture'").fetchone()[0]
    assert main.expire_reference_command_leases(datetime.fromisoformat(lease)+timedelta(microseconds=1))==1
    with main.db() as con:
        command=con.execute("SELECT status,cancel_requested_at,error_text FROM agent_commands WHERE command_id='capture'").fetchone()
        build=con.execute("SELECT status,error_text FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()
    assert command['status']=='failed' and command['cancel_requested_at']
    assert 'Lease de capture expirée' in command['error_text']
    assert build['status']=='build_failed' and 'Lease de capture expirée' in build['error_text']
    with patch.object(main,'authenticate_agent'):
        heartbeat=json.loads(asyncio.run(main.agent_heartbeat('ouranos',JsonRequest({
            'agent_version':'test','active_command_id':'capture','capabilities':capabilities}))).body)
    assert heartbeat['cancel_active_command'] and heartbeat['active_command_state']=='terminal'
    data=reference_archive(reliable_build.tmp).read_bytes()
    class LateUpload:
        headers={'X-Reference-SHA256':hashlib.sha256(data).hexdigest()}
        async def stream(self): yield data
    with patch.object(main,'authenticate_agent'),pytest.raises(main.HTTPException) as rejected:
        asyncio.run(main.upload_reference_build_archive('ouranos',reliable_build.build_id,LateUpload()))
    assert rejected.value.status_code==409
    with main.db() as con:
        assert con.execute('SELECT COUNT(*) FROM reference_image_versions').fetchone()[0]==0


def test_expired_lease_fails_orphan_and_late_success_is_ignored(reliable_build):
    with main.db() as con:
        con.execute("UPDATE agent_commands SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE command_id='capture'")
    assert main.expire_reference_command_leases()==1
    with patch.object(main,'authenticate_agent'):
        response=asyncio.run(main.agent_command_result('ouranos','capture',JsonRequest({'status':'success','result':{'sha256':'0'*64}})))
    assert json.loads(response.body)['ignored']=='terminal_command'
    with main.db() as con:
        assert con.execute("SELECT status FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()[0]=='build_failed'
        assert con.execute("SELECT status FROM jobs WHERE job_id=?",(reliable_build.job_id,)).fetchone()[0]=='failed'


def test_cancel_claimed_build_requests_cooperative_stop(reliable_build):
    response=main.cancel_reference_build(reliable_build.build_id)
    assert response.status_code==303
    with main.db() as con:
        assert con.execute("SELECT status FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()[0]=='cancelling'
        assert con.execute("SELECT cancel_requested_at FROM agent_commands WHERE command_id='capture'").fetchone()[0]


def test_cancel_unacknowledged_reference_offer_is_immediate(reliable_build):
    enable_reference_delivery()
    with patch.object(main,'authenticate_agent'):
        offer=json.loads(main.agent_poll_commands('ouranos',JsonRequest({})).body)['command']
    assert offer['status']=='offered'
    response=main.cancel_reference_build(reliable_build.build_id)
    assert response.status_code==303
    with main.db() as con:
        command=con.execute("SELECT status,cancel_requested_at FROM agent_commands WHERE command_id='capture'").fetchone()
        build=con.execute("SELECT status FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()
    assert command['status']=='cancelled' and command['cancel_requested_at']
    assert build['status']=='cancelled'


def test_cancelled_agent_result_is_terminal_and_cleans_job(reliable_build):
    with patch.object(main,'authenticate_agent'):
        asyncio.run(main.agent_command_result('ouranos','capture',JsonRequest({'status':'cancelled','error':'operator','result':{'temporary_cleanup':'completed'}})))
    with main.db() as con:
        assert con.execute("SELECT status FROM reference_builds WHERE build_id=?",(reliable_build.build_id,)).fetchone()[0]=='cancelled'
        assert con.execute("SELECT status FROM jobs WHERE job_id=?",(reliable_build.job_id,)).fetchone()[0]=='cancelled'


def test_disk_requirement_blocks_and_margin_is_configurable(tmp_path):
    usage=SimpleNamespace(free=12_000)
    with patch.object(agent.shutil,'disk_usage',return_value=usage):
        blocked=agent._reference_storage_requirement({'reference_build_reserve_bytes':5_000,'reference_build_reserve_ratio':.10},10_000,tmp_path)
        allowed=agent._reference_storage_requirement({'reference_build_reserve_bytes':1_000,'reference_build_reserve_ratio':.05},10_000,tmp_path)
    assert blocked['required_free_bytes']==15_000 and blocked['missing_free_bytes']==3_000 and not blocked['can_build']
    assert allowed['required_free_bytes']==11_000 and allowed['can_build']


def test_space_change_is_rechecked_before_capture(tmp_path):
    config={'reference_build_temp_dir':str(tmp_path),'control_plane_url':'http://invalid','token':'x'}
    discovery={'preflight':{'can_build':True,'estimated_payload_bytes':100},'configuration':{'config_path':str(tmp_path)},'instance':{'container_name':'plex'}}
    with patch.object(agent,'discover_plex_instance',return_value=discovery), \
         patch.object(agent,'_reference_storage_requirement',return_value={'can_build':False,'required_free_bytes':200,'temporary_free_bytes':50,'missing_free_bytes':150}), \
         patch.object(agent,'_capture_plex_reference') as capture:
        with pytest.raises(RuntimeError,match='devenu insuffisant'):
            agent.build_and_upload_plex_reference(config,{'upload_path':'/api/agent/v1/n/reference-builds/b/archive'})
    capture.assert_not_called()
    assert not list(tmp_path.glob('appbox-reference-build-*'))


def test_cancelled_capture_cleans_temporary_directory(tmp_path):
    config={'reference_build_temp_dir':str(tmp_path),'control_plane_url':'http://invalid','token':'x'}
    discovery={'preflight':{'can_build':True,'estimated_payload_bytes':100},'configuration':{'config_path':str(tmp_path)},'instance':{'container_name':'plex'}}
    with patch.object(agent,'discover_plex_instance',return_value=discovery), \
         patch.object(agent,'_reference_storage_requirement',return_value={'can_build':True,'required_free_bytes':100,'temporary_free_bytes':200,'missing_free_bytes':0}), \
         patch.object(agent,'_capture_plex_reference',side_effect=agent.CommandCancelled('cancelled')):
        with pytest.raises(agent.CommandCancelled):
            agent.build_and_upload_plex_reference(config,{'upload_path':'/api/agent/v1/n/reference-builds/b/archive'})
    assert not list(tmp_path.glob('appbox-reference-build-*'))


def test_long_capture_observes_cancel_event_and_cleans_staging(tmp_path):
    config={'reference_build_temp_dir':str(tmp_path),'control_plane_url':'http://invalid','token':'x'}
    discovery={'preflight':{'can_build':True,'estimated_payload_bytes':100},
               'configuration':{'config_path':str(tmp_path)},'instance':{'container_name':'plex'}}
    entered=threading.Event(); cancel=threading.Event(); errors=[]
    def capture(_config_path,workdir,_container_name,**kwargs):
        (workdir/'partial-capture').write_bytes(b'partial')
        entered.set()
        assert kwargs['cancel_event'].wait(3)
        raise agent.CommandCancelled('cancelled during long capture')
    def run_capture():
        try:
            agent.build_and_upload_plex_reference(config,{
                'upload_path':'/api/agent/v1/n/reference-builds/b/archive'},cancel_event=cancel)
        except Exception as exc:
            errors.append(exc)
    with patch.object(agent,'discover_plex_instance',return_value=discovery), \
         patch.object(agent,'_reference_storage_requirement',return_value={
             'can_build':True,'required_free_bytes':100,'temporary_free_bytes':200,'missing_free_bytes':0}), \
         patch.object(agent,'_capture_plex_reference',side_effect=capture):
        worker=threading.Thread(target=run_capture); worker.start()
        assert entered.wait(1); cancel.set(); worker.join(4)
    assert not worker.is_alive()
    assert len(errors)==1 and isinstance(errors[0],agent.CommandCancelled)
    assert not list(tmp_path.glob('appbox-reference-build-*'))


def test_success_step_without_timestamps_is_never_labelled_not_started(reliable_build):
    with main.db() as con:
        con.execute("UPDATE job_steps SET status='success',started_at=NULL,finished_at=NULL WHERE job_id=? AND step_key='discover'",(reliable_build.job_id,))
    page=TestClient(main.app).get(f'/jobs/{reliable_build.job_id}')
    assert page.status_code==200 and 'Terminée' in page.text
    assert '0.00 s' not in page.text


def test_capture_writer_reports_growth_and_obeys_cancel(tmp_path):
    reports=[]
    event=agent.Event()
    writer=agent._CaptureWriter(tmp_path/'archive',100,lambda written,total: reports.append((written,total)),event)
    writer.last_reported_at=-10
    assert writer.write(b'x'*40)==40 and reports[-1]==(40,100)
    event.set()
    with pytest.raises(agent.CommandCancelled): writer.write(b'y')
    writer.close()
