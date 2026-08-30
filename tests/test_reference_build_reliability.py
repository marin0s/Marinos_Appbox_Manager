import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import main
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
        con.execute("INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at,claimed_at,lease_expires_at) VALUES('capture','ouranos','reference_build',?,'claimed',?,?,?)",
                    (payload,stamp,stamp,(datetime.now(timezone.utc)+timedelta(seconds=180)).isoformat()))
    yield SimpleNamespace(build_id=build_id,job_id=job_id,tmp=tmp_path)
    main.DB_FILE, main.REFERENCE_ROOT = old_db, old_root


class JsonRequest:
    def __init__(self,payload): self.payload=payload
    async def json(self): return self.payload


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
