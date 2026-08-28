import io
import json
import stat
import tempfile
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from agent import upgrade_contract as contract, upgrade_helper as helper, upgrade_client as client
from agent import upgrade_launcher as launcher
from app import main, agent_upgrades as upgrades

SOURCE = Path(__file__).resolve().parents[1] / 'agent'


@pytest.fixture
def artifact():
    data = contract.package_bytes(SOURCE)
    manifest, contents = contract.validate_package(data, contract.digest(data))
    return data, manifest, contents


@pytest.mark.parametrize('old,new,expected', [
    ('1.6.0-alpha.5-dev', '1.6.0-alpha.5', 'update_available'),
    ('1.6.0-alpha.9', '1.6.0-alpha.10-dev', 'update_available'),
    ('1.6.0-beta.1', '1.6.0-alpha.10', 'up_to_date'),
    ('1.6.0-rc.9', '1.6.0', 'update_available'),
    ('1.6.0-dev', '1.6.0-alpha.1', 'update_available'),
    ('1.10.0', '1.9.0', 'up_to_date'),
    ('bad', '1.6.0', 'unknown'), ('1.6.0', 'invalid', 'unknown'),
])
def test_versions(old, new, expected):
    assert contract.update_status(old, new) == expected


def test_same_version_build_identity():
    assert contract.update_status('1.6.0-alpha.5-dev', '1.6.0-alpha.5-dev', 'same', 'same') == 'up_to_date'
    assert contract.update_status('1.6.0-alpha.5-dev', '1.6.0-alpha.5-dev', 'old', 'new') == 'update_available'


def test_checksum_candidate_and_immutable_release(tmp_path, artifact):
    data, manifest, _ = artifact
    target, actual = contract.prepare_release(tmp_path, data, contract.digest(data))
    assert actual == manifest
    assert target.name == contract.digest(data)
    assert json.loads((target/'release-receipt.json').read_text())['sha256'] == target.name
    before = (target/'marinos-appbox-agent.py').read_bytes()
    with pytest.raises(ValueError, match='checksum'):
        contract.prepare_release(tmp_path, data, '0'*64)
    assert (target/'marinos-appbox-agent.py').read_bytes() == before
    assert contract.prepare_release(tmp_path, data, target.name)[0] == target
    (target/'reference_contract.py').write_text('damaged')
    with pytest.raises(ValueError, match='differs'):
        contract.prepare_release(tmp_path, data, target.name)


@pytest.mark.parametrize('attack', ['traversal','absolute','backslash','duplicate','symlink','fifo','unexpected','manifest'])
def test_malicious_zip_rejected(artifact, attack):
    data, _, _ = artifact
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as original, zipfile.ZipFile(output, 'w') as modified:
        for member in original.infolist():
            content = original.read(member)
            if member.filename == 'install-agent.sh':
                member.filename = {'traversal':'../install-agent.sh','absolute':'/install-agent.sh',
                                   'backslash':'..\\install-agent.sh'}.get(attack, member.filename)
                if attack in {'symlink','fifo'}:
                    member.external_attr = ((stat.S_IFLNK if attack == 'symlink' else stat.S_IFIFO) | 0o644) << 16
                if attack == 'duplicate':
                    with pytest.warns(UserWarning):
                        modified.writestr(member, content)
                        modified.writestr(member, content)
                    continue
            if attack == 'manifest' and member.filename == contract.MANIFEST:
                content = b'{}'
            modified.writestr(member, content)
        if attack == 'unexpected':
            modified.writestr('run-me.sh', b'not allowed')
    data = output.getvalue()
    with pytest.raises(ValueError):
        contract.validate_package(data, contract.digest(data))


@pytest.fixture
def cp(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_FILE', tmp_path/'cp.db')
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr(main, 'HOSTNAME', 'cronos')
    monkeypatch.setattr(main, 'AGENT_ASSET_DIR', SOURCE)
    main.init_database()
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('testnode','TEST','remote','online',?,?)", (stamp,stamp))
        con.execute("INSERT INTO node_agents(node_id,status,agent_version,last_heartbeat,capabilities_json,updated_at) VALUES('testnode','online','1.6.0-alpha.4-dev',?,?,?)", (stamp,json.dumps({'deployment_executor':True,'remote_upgrade':True}),stamp))
    return TestClient(main.app)


def advance(op, *phases):
    for phase in phases:
        upgrades.event('testnode', op['operation_id'], {'phase':phase})


def test_official_artifact_pin_and_download_authorization(cp):
    info = upgrades.official_artifact(pin=True)
    assert info['path'].name == info['sha256']+'.zip'
    assert info['size_bytes'] == info['path'].stat().st_size
    op = upgrades.start('testnode')['operation']
    url = f"/api/agent/v1/testnode/upgrades/{op['operation_id']}/archive"
    assert cp.get(url).status_code in {401,403}
    _, token = main.create_agent_token('testnode')
    response = cp.get(url, headers={'Authorization':'Bearer '+token})
    assert response.status_code == 200
    assert contract.digest(response.content) == op['package_sha256']
    assert cp.get(url.replace('/testnode/','/cronos/'), headers={'Authorization':'Bearer '+token}).status_code in {401,403}
    info['path'].write_bytes(b'corrupted')
    assert cp.get(url, headers={'Authorization':'Bearer '+token}).status_code == 503


def test_busy_and_reverse_exclusion(cp):
    command = main.queue_agent_command('testnode','reference_build')
    for status in ('queued','claimed'):
        with main.db() as con:
            con.execute('UPDATE agent_commands SET status=? WHERE command_id=?',(status,command))
        with pytest.raises(HTTPException) as rejected:
            upgrades.start('testnode')
        assert rejected.value.status_code == 409
    with main.db() as con:
        con.execute("UPDATE agent_commands SET status='success' WHERE command_id=?", (command,))
    job = main.create_job(None,'start','test',node_id='testnode')
    with pytest.raises(HTTPException):
        upgrades.start('testnode')
    with main.db() as con:
        con.execute("UPDATE jobs SET status='success' WHERE job_id=?", (job,))
    upgrades.start('testnode')
    with pytest.raises(HTTPException):
        main.queue_agent_command('testnode','appbox_action', {'action':'start'})
    with pytest.raises(HTTPException):
        main.create_job('appb-test','restart','test',node_id='testnode')
    node = next(n for n in main.list_control_nodes() if n['node_id']=='testnode')
    assert node['status'] == 'online' and not node['provisioning_allowed']
    assert 'mise à jour' in main.execution_block_reason(node, 'stop').lower()


def test_success_requires_new_heartbeat_and_late_result_cannot_override(cp, monkeypatch):
    op = upgrades.start('testnode')['operation']
    advance(op,'downloading','verifying','prepared','installing','restarting','awaiting_heartbeat')
    with pytest.raises(HTTPException):
        upgrades.event('testnode',op['operation_id'],{'phase':'success'})
    runtime = {'build_id':op['build_id'],'package_sha256':op['package_sha256'],'process_id':'new-process','pid':101}
    with main.db() as con:
        upgrades.observe_heartbeat(con,'testnode',{'agent_version':op['version'],'runtime':runtime},main.now_iso())
    upgrades.event('testnode',op['operation_id'],{'phase':'success','runtime':runtime})
    monkeypatch.setattr(main, 'authenticate_agent', lambda *args: None)
    url = f"/api/agent/v1/testnode/commands/{op['operation_id']}/result"
    assert cp.post(url,json={'status':'failed','error':'late'}).status_code == 200
    with main.db() as con:
        assert con.execute('SELECT status FROM agent_commands WHERE command_id=?',(op['operation_id'],)).fetchone()['status'] == 'success'
        con.execute("UPDATE agent_upgrade_runtime SET process_id='after-reboot' WHERE node_id='testnode'")
    # Lost terminal acknowledgement remains idempotent after another restart.
    upgrades.event('testnode',op['operation_id'],{'phase':'success','runtime':runtime})


def test_failure_api_ui_retry_and_heartbeat_not_masked(cp):
    op = upgrades.start('testnode')['operation']
    advance(op,'downloading','verifying','prepared','installing','restarting','awaiting_heartbeat')
    status = cp.get('/api/nodes/testnode/agent-upgrade').json()
    assert status['status']=='upgrading' and status['node_status']=='online' and status['restart_expected']
    with main.db() as con:
        con.execute("UPDATE node_agents SET last_heartbeat=? WHERE node_id='testnode'", ((datetime.now(timezone.utc)-timedelta(seconds=181)).isoformat(),))
        con.execute('UPDATE agent_upgrades SET deadline_epoch=0 WHERE operation_id=?',(op['operation_id'],))
    status = cp.get('/api/nodes/testnode/agent-upgrade').json()
    assert status['node_status']=='offline' and status['status']=='upgrade_failed' and not status['restart_expected']
    advance(op,'rolling_back','rolled_back')
    assert 'rolled_back' in cp.get('/agents').text
    assert 'Mettre à jour l’agent' in cp.get('/nodes').text
    with main.db() as con:
        con.execute("UPDATE node_agents SET last_heartbeat=? WHERE node_id='testnode'",(main.now_iso(),))
    assert upgrades.start('testnode')['operation']['operation_id'] != op['operation_id']


def test_legacy_requires_manual_bootstrap_and_same_sha(cp):
    with main.db() as con:
        con.execute("UPDATE node_agents SET capabilities_json='{}' WHERE node_id='testnode'")
    with pytest.raises(HTTPException, match='Bootstrap'):
        upgrades.start('testnode')
    with pytest.raises(HTTPException):
        upgrades.start('testnode',bootstrap=True)
    op = upgrades.start('testnode',bootstrap=True,expected_sha=upgrades.official_artifact()['sha256'])['operation']
    with main.db() as con:
        assert not con.execute('SELECT 1 FROM agent_commands WHERE command_id=?',(op['operation_id'],)).fetchone()


@pytest.fixture
def supervisor(tmp_path, monkeypatch, artifact):
    data, manifest, contents = artifact
    root, state, spool = (tmp_path/name for name in ('root','state','spool'))
    previous = root/'releases'/'previous'
    previous.mkdir(parents=True)
    contract.atomic_json(previous/'release-receipt.json', {'version':'1.6.0-alpha.4-dev','build_id':'oldbuild','sha256':'oldsha'})
    for name in contract.HELPER_FILES:
        (previous/name).write_bytes(contents[name])
    units=tmp_path/'units'; units.mkdir()
    (units/helper.SERVICE).write_bytes(contents['managed-agent.service'].replace(b'RestartSec=10',b'RestartSec=7'))
    spool.mkdir()
    archive = spool/'agent.zip'; archive.write_bytes(data)
    pointer = {'target':previous, 'current':previous, 'previous':previous, 'controller':previous, 'rescue':previous}
    resolve = Path.resolve
    # Windows lacks symlink privilege: model only the current pointer and systemd.
    monkeypatch.setattr(Path,'resolve',lambda self,*a,**kw: pointer[self.name] if self.parent == root and self.name in {'current','previous','controller','rescue'} else resolve(self,*a,**kw))
    def switch(root_arg, name, target):
        assert Path(target).parent == root/'releases' and Path(target).is_dir()
        pointer[name] = Path(target)
        if name=='current': pointer['target']=Path(target)
    monkeypatch.setattr(helper,'switch_pointer',switch)
    clock = [1000.0]
    op = {'node_id':'testnode','phase':'prepared','package_sha256':contract.digest(data),'deadline_epoch':1900}
    runtime = {'version':'1.6.0-alpha.4-dev','build_id':'oldbuild','package_sha256':'oldsha','process_id':'old','pid':10,'received_at':'01'}
    events, calls, online = [], [], [True]
    def transport(config,path,payload=None):
        if not online[0]: raise OSError('network unavailable')
        if path.endswith('/events'):
            events.append(payload['phase'])
            return {}
        return {'operation':op.copy(),'runtime':runtime.copy()}
    def ctl(*args):
        calls.append(args)
        return 'active' if args[0]=='is-active' else '20' if args[0]=='show' else ''
    sup = helper.Supervisor({'node_id':'testnode'},root,state,spool,ctl,transport,lambda:clock[0],units=units,controller=previous,probe=lambda release:True)
    return sup, archive, pointer, clock, runtime, events, calls, online, manifest


def test_activation_confirmation_and_durable_reboot(supervisor):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    state = sup.begin('00000000-0000-0000-0000-000000000001',archive)
    assert pointer['target'].name == 'previous'
    assert sup.load()['previous'] == str(pointer['target'])
    online[0] = False
    sup.step(state)
    assert pointer['target'].name == state['package_sha256']
    assert sup.load()['phase']=='awaiting_heartbeat'
    assert calls[:3] == [('stop',helper.SERVICE),('daemon-reload',),('start',helper.SERVICE)]
    online[0] = True
    runtime.update(version=manifest['version'],build_id=manifest['build_id'],package_sha256=state['package_sha256'],process_id='new',pid=20,received_at='02')
    rebooted = helper.Supervisor(sup.config,sup.root,sup.state_dir,sup.spool,sup.ctl,sup.transport,sup.clock,units=sup.units,controller=sup.controller,probe=sup.probe)
    rebooted.tick()
    assert rebooted.load()['phase']=='success'
    assert events == ['installing','restarting','awaiting_heartbeat','success']
    assert (sup.root/'releases'/'previous').is_dir()


@pytest.mark.parametrize('failure',['missing','wrong_version','wrong_build','wrong_pid','same_process','checksum'])
def test_missing_or_wrong_confirmation_rolls_back(supervisor,failure):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    state = sup.begin('00000000-0000-0000-0000-000000000001',archive)
    sup.step(state)
    if failure!='missing':
        runtime.update(version=manifest['version'],build_id=manifest['build_id'],package_sha256=state['package_sha256'],process_id='new',pid=20,received_at='02')
        key,value = {'wrong_version':('version','9.0.0'),'wrong_build':('build_id','bad'),
                     'wrong_pid':('pid',99),'same_process':('process_id','old'),'checksum':('package_sha256','bad')}[failure]
        runtime[key]=value
    sup.step(state)
    assert state['phase']=='awaiting_heartbeat'
    clock[0] = state['deadline']+1
    sup.step(state)
    assert state['phase']=='rolling_back'
    sup.step(state)
    assert pointer['target'].name=='previous'
    runtime.update(version='1.6.0-alpha.4-dev',build_id='oldbuild',package_sha256='oldsha',process_id='old-restarted',pid=20,received_at='03')
    sup.step(state)
    assert sup.load()['phase']=='rolled_back'
    assert events[-2:] == ['rolling_back','rolled_back']


def test_helper_checksum_rejection_does_not_stop_agent(supervisor):
    sup, archive, pointer, _, _, _, calls, _, _ = supervisor
    archive.write_bytes(b'invalid')
    with pytest.raises(ValueError):
        sup.begin('00000000-0000-0000-0000-000000000001',archive)
    assert pointer['target'].name=='previous' and calls==[]


def test_atomic_pointer_uses_replace(tmp_path, monkeypatch):
    target = tmp_path/'releases'/'sha'; target.mkdir(parents=True)
    create, replace = Mock(), Mock()
    monkeypatch.setattr(Path,'symlink_to',create)
    monkeypatch.setattr(helper.os,'replace',replace)
    helper.switch_current(tmp_path,target)
    create.assert_called_once_with(target.resolve(),target_is_directory=True)
    replace.assert_called_once_with(tmp_path/'.current-next',tmp_path/'current')
    with pytest.raises(ValueError): helper.switch_current(tmp_path,tmp_path)


def test_legacy_bootstrap_preserves_config_and_can_resume(tmp_path, monkeypatch, artifact):
    data, _, _ = artifact
    legacy, root, state, spool, units = (tmp_path/name for name in ('legacy','root','state','spool','units'))
    legacy.mkdir(); units.mkdir()
    (units/helper.SERVICE).write_bytes((SOURCE/helper.SERVICE).read_bytes())
    for name in ('marinos-appbox-agent.py','reference_contract.py'):
        (legacy/name).write_bytes((SOURCE/name).read_bytes().replace(b'alpha.5',b'alpha.4'))
    config_file = tmp_path/'agent.json'
    original = b'{"node_id":"testnode","token":"DO-NOT-CHANGE","control_plane_url":"http://cp"}'
    config_file.write_bytes(original)
    archive = tmp_path/'agent.zip'; archive.write_bytes(data)
    opid = '00000000-0000-0000-0000-000000000002'
    remote = {'operation_id':opid,'phase':'queued'}
    def transport(config,path,payload=None):
        if path.endswith('/events'): remote['phase']=payload['phase']
        return {'operation':remote.copy()}
    monkeypatch.setattr(helper,'switch_current',Mock())
    monkeypatch.setattr(helper,'switch_pointer',Mock())
    ctl = Mock(side_effect=RuntimeError('simulated reboot before handoff'))
    args = (archive,contract.digest(data),json.loads(original),root,state,spool,legacy,units)
    with pytest.raises(RuntimeError): helper.bootstrap(*args,ctl=ctl,transport=transport)
    ctl = Mock()
    assert helper.bootstrap(*args,ctl=ctl,transport=transport)==opid
    assert config_file.read_bytes()==original
    assert json.loads((spool/'request.json').read_text())['operation_id']==opid
    assert list((root/'releases').glob('legacy-*'))
    assert not any(call.args[0] in {'stop','start','restart'} for call in ctl.call_args_list)
    assert '/current/marinos-appbox-agent.py' in (units/helper.SERVICE).read_text()
    # A first migration rolled back to legacy must remain retryable without
    # requiring the old agent to understand agent_upgrade.
    remote['phase']='rolled_back'
    helper.Supervisor(json.loads(original),root,state,spool).save(
        {'operation_id':opid,'phase':'rolled_back','reported':True})
    attempts=[]
    def retry_transport(config,path,payload=None):
        if path.endswith('/bootstrap'):
            attempts.append(path)
            remote.update(operation_id='00000000-0000-0000-0000-000000000004',phase='queued')
        return transport(config,path,payload)
    assert helper.bootstrap(*args,ctl=ctl,transport=retry_transport).endswith('004')
    assert len(attempts)==1 and config_file.read_bytes()==original



@pytest.fixture
def short_spool():
    # Keep Windows paths below MAX_PATH when staging a checksum-addressed release.
    with tempfile.TemporaryDirectory(prefix="upg-") as directory:
        yield Path(directory)


def test_agent_prepares_only_then_external_helper_handoff(short_spool, monkeypatch, artifact):
    tmp_path = short_spool
    data, _, _ = artifact
    monkeypatch.setattr(client,'SPOOL',tmp_path)
    opid = '00000000-0000-0000-0000-000000000003'
    events = []
    def transport(config,path,payload=None,binary=False):
        assert path.startswith('/api/agent/v1/testnode/upgrades/'+opid)
        if binary: return data
        if payload:
            events.append(payload['phase'])
            return {}
        return {'operation':{'phase':'queued','node_id':'testnode','package_sha256':contract.digest(data)}}
    monkeypatch.setattr(client,'request',transport)
    result = client.stage_upgrade({'node_id':'testnode'},{'operation_id':opid,'url':'https://ignored.invalid'})
    assert result['handoff']=='prepared'
    assert events==['downloading','verifying','prepared']
    assert json.loads((tmp_path/'request.json').read_text())=={'operation_id':opid}
    assert (tmp_path/opid/'agent.zip').read_bytes()==data
    with pytest.raises(RuntimeError,match='handoff'):
        client.stage_upgrade({'node_id':'testnode'},{'operation_id':opid})


def test_agent_checksum_failure_does_not_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(client,'SPOOL',tmp_path)
    phases = []
    def transport(config,path,payload=None,binary=False):
        if binary: return b'corrupt'
        if payload:
            phases.append(payload['phase'])
            return {}
        return {'operation':{'phase':'queued','node_id':'testnode','package_sha256':'0'*64}}
    monkeypatch.setattr(client,'request',transport)
    with pytest.raises(RuntimeError,match='preparation interrupted'):
        client.stage_upgrade({'node_id':'testnode'},{'operation_id':'00000000-0000-0000-0000-000000000003'})
    assert (tmp_path/'request.json').exists()  # helper owns durable cleanup; no prepared candidate
    assert not list(tmp_path.glob('*/agent.zip'))
    assert phases[-1]=='upgrade_failed'
    with pytest.raises(ValueError): client.operation_path({'node_id':'testnode'},'../../etc')
    with pytest.raises(RuntimeError): client.NoRedirect().redirect_request(None,None,302,'',{},'https://other')


def test_offline_rollback_replays_events_and_retry(supervisor):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    state = sup.begin('00000000-0000-0000-0000-000000000001',archive)
    sup.step(state)
    online[0]=False
    clock[0]=state['deadline']+1
    sup.tick()
    assert sup.load()['phase']=='rolling_back'
    sup.tick()
    assert pointer['target'].name=='previous'  # local restore despite CP outage
    online[0]=True
    runtime.update(process_id='old-restarted',pid=20,received_at='03')
    sup.tick()
    assert sup.load()['phase']=='rolled_back'
    assert events[-2:]==['rolling_back','rolled_back']
    next_state = sup.begin('00000000-0000-0000-0000-000000000002',archive)
    assert next_state['previous']==str(pointer['target'])
    sup.step(next_state)
    assert next_state['phase']=='awaiting_heartbeat'


def test_no_false_rollback_success_when_previous_missing(supervisor):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    state = sup.begin('00000000-0000-0000-0000-000000000001',archive)
    sup.step(state)
    clock[0]=state['deadline']+1
    sup.step(state)
    clock[0]=state['rollback_deadline']+1
    sup.step(state)
    assert state['phase']=='rollback_failed'
    assert state['error_code']=='previous_agent_return_unconfirmed'


def test_expired_activation_never_stops_current(supervisor):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    state = sup.begin('00000000-0000-0000-0000-000000000001',archive)
    clock[0]=state['deadline']+1
    sup.step(state)
    assert state['phase']=='upgrade_failed'
    assert pointer['target'].name=='previous' and not calls


def test_helper_changes_are_release_owned(supervisor):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    (sup.controller/'upgrade_helper.py').write_text('older helper bytes')
    state=sup.begin('00000000-0000-0000-0000-000000000001',archive)
    assert state['phase']=='installing'
    assert pointer['target'].name=='previous' and not calls


def test_poll_and_late_business_commands_are_blocked(cp, monkeypatch):
    op = upgrades.start('testnode')['operation']
    monkeypatch.setattr(main,'authenticate_agent',lambda *args:None)
    response = cp.get('/api/agent/v1/testnode/commands').json()
    assert response['command']['command_type']=='agent_upgrade'
    with main.db() as con:
        con.execute("INSERT INTO agent_commands(command_id,node_id,command_type,status,created_at) VALUES('late','testnode','reference_build','queued',?)",(main.now_iso(),))
    response = cp.get('/api/agent/v1/testnode/commands').json()
    assert response['command'] is None and response['reason']=='agent_upgrading'


def test_invalid_official_package_fails_closed(cp, tmp_path, monkeypatch):
    source = tmp_path/'source'; source.mkdir()
    for name in contract.FILES: (source/name).write_bytes((SOURCE/name).read_bytes())
    (source/'appbox-agent-latest.zip').write_bytes(b'invalid')
    monkeypatch.setattr(main,'AGENT_ASSET_DIR',source)
    assert cp.get('/api/nodes/testnode/agent-upgrade').json()['status']=='unknown'
    assert cp.post('/nodes/testnode/upgrade-agent').status_code==503


def test_full_helper_protocol_against_control_plane(cp, supervisor):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    with main.db() as con:
        upgrades.observe_heartbeat(con,'testnode',{'agent_version':'1.6.0-alpha.4-dev','runtime':runtime},main.now_iso())
    op = upgrades.start('testnode')['operation']
    advance(op,'downloading','verifying','prepared')
    def transport(config,path,payload=None):
        if not online[0]: raise OSError('CP unavailable')
        if path.endswith('/events'):
            return upgrades.event('testnode',op['operation_id'],payload)
        return upgrades.operation('testnode',op['operation_id'])
    sup.transport = transport
    state = sup.begin(op['operation_id'],archive)
    online[0]=False
    sup.step(state)
    assert upgrades.operation('testnode',op['operation_id'])['operation']['phase']=='prepared'
    online[0]=True
    runtime.update(build_id=op['build_id'],package_sha256=op['package_sha256'],process_id='new',pid=20)
    with main.db() as con:
        upgrades.observe_heartbeat(con,'testnode',{'agent_version':op['version'],'runtime':runtime},main.now_iso())
    sup.tick()
    assert sup.load()['reported']
    assert upgrades.operation('testnode',op['operation_id'])['operation']['phase']=='success'
    assert cp.get('/api/nodes/testnode/agent-upgrade').json()['status']=='up_to_date'


def test_preparation_reboot_waits_then_expires(supervisor):
    sup, archive, pointer, clock, runtime, events, calls, online, manifest = supervisor
    opid='00000000-0000-0000-0000-000000000001'
    def transport(config,path,payload=None):
        if payload:
            events.append(payload['phase'])
            return {}
        return {'operation':{'node_id':'testnode','phase':'downloading','deadline_epoch':1100}}
    sup.transport=transport
    contract.atomic_json(sup.spool/'request.json',{'operation_id':opid})
    sup.tick()
    assert sup.load() is None and pointer['target'].name=='previous'
    clock[0]=1101
    sup.tick()
    assert sup.load()['phase']=='upgrade_failed'
    assert calls==[]


def test_preparation_timeout_can_retry_without_liveness_change(cp):
    op=upgrades.start('testnode')['operation']
    advance(op,'downloading')
    with main.db() as con:
        con.execute('UPDATE agent_upgrades SET deadline_epoch=0 WHERE operation_id=?',(op['operation_id'],))
    status=cp.get('/api/nodes/testnode/agent-upgrade').json()
    assert status['status']=='upgrade_failed' and status['node_status']=='online'
    assert status['error_code']=='preparation_timeout'
    assert upgrades.start('testnode')['operation']['operation_id']!=op['operation_id']


def test_node_deletion_refused_while_upgrading(cp):
    op=upgrades.start('testnode')['operation']
    assert cp.post('/nodes/testnode/delete').status_code==409
    advance(op,'upgrade_failed')
    assert cp.post('/nodes/testnode/delete',follow_redirects=False).status_code==303


# Real per-release Python imports and methods; only Linux pointers/systemd and CP
# transport are replaced. No server is contacted and no system service is invoked.
RELEASE_HARNESS = r'''
import sys, json
from pathlib import Path
release, area, action = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0,str(release))
import upgrade_helper as h
import upgrade_client as c
import upgrade_contract as k
root=area/'root'; units=area/'units'; spool=area/'spool'; state=area/'state'
pointers=area/'pointers.json'; cpfile=area/'cp.json'
resolve=Path.resolve
def resolved(self,*args,**kwargs):
    if self.parent==root and self.name in {'current','previous','controller','rescue'}:
        return Path(json.loads(pointers.read_text())[self.name])
    return resolve(self,*args,**kwargs)
Path.resolve=resolved
def switch(root_arg,name,target):
    target=Path(target).resolve(strict=True)
    assert target.parent==root/'releases'
    mapping=json.loads(pointers.read_text()); mapping[name]=str(target)
    k.atomic_json(pointers,mapping)
h.switch_pointer=switch
def transport(config,path,payload=None,binary=False):
    cp=json.loads(cpfile.read_text())
    if binary: return Path(cp['archive']).read_bytes()
    if path.endswith('/events'):
        cp.setdefault('events',[]).append(payload['phase'])
        cp['operation']['phase']=payload['phase']
        k.atomic_json(cpfile,cp)
        return {}
    return cp
def ctl(*args):
    cp=json.loads(cpfile.read_text())
    cp.setdefault('systemd',[]).append(list(args)); k.atomic_json(cpfile,cp)
    return 'active' if args[0]=='is-active' else '20' if args[0]=='show' else ''
config={'node_id':'testnode'}
if action=='stage':
    c.SPOOL=spool; c.request=transport
    c.stage_upgrade(config,{'operation_id':json.loads(cpfile.read_text())['operation']['operation_id']})
else:
    supervisor=h.Supervisor(config,root,state,spool,ctl,transport,lambda:1000.0,units=units)
    getattr(supervisor,action)()
'''


@pytest.fixture
def chain(short_spool, monkeypatch):
    area=short_spool
    root=area/'root'; (root/'releases').mkdir(parents=True)
    units=area/'units'; units.mkdir()
    (area/'state').mkdir(); (area/'spool').mkdir()
    config=area/'agent.json'; config.write_bytes(b'{"token":"unchanged","node_id":"testnode"}')
    (root/'upgrade_launcher.py').write_bytes((SOURCE/'upgrade_launcher.py').read_bytes())
    fixed=(root/'upgrade_launcher.py').read_bytes()
    for name in ('marinos-appbox-updater.service','marinos-appbox-updater.timer'):
        (units/name).write_bytes((SOURCE/name).read_bytes())
    def package(generation, broken=None):
        source=area/('src'+str(generation)); source.mkdir()
        for name in contract.FILES:
            data=(SOURCE/name).read_bytes().replace(b'\r\n',b'\n')
            if name=='marinos-appbox-agent.py':
                data=data.replace(b'1.6.0-alpha.5',f'1.6.0-alpha.{5+generation}'.encode())
            if name=='upgrade_contract.py':
                data+=f'\nTEST_CONTRACT_GENERATION={generation}\n'.encode()
            if name=='upgrade_client.py':
                data+=f'\nTEST_CLIENT_GENERATION={generation}\n'.encode()
            if name=='upgrade_helper.py':
                needle=b'        initial = {"operation_id":operation_id}'
                data=data.replace(needle, (f'        from upgrade_contract import TEST_CONTRACT_GENERATION\n'
                    f'        from upgrade_client import TEST_CLIENT_GENERATION\n'
                    f'        (self.state_dir / "implementation.txt").write_text("{generation}:" + str(TEST_CONTRACT_GENERATION) + ":" + str(TEST_CLIENT_GENERATION))\n').encode()+needle)
                if broken=='runtime': data=b'raise RuntimeError("broken candidate helper")\n'+data
                if broken=='syntax': data=b'def broken(:\n'+data
            if name=='managed-agent.service':
                data=data.replace(b'RestartSec=10',f'RestartSec={10+generation}'.encode())
            (source/name).write_bytes(data)
        data=contract.package_bytes(source)
        archive=area/f'package{generation}.zip'; archive.write_bytes(data)
        return archive
    def run(release,action):
        completed=subprocess.run([sys.executable,'-I','-B','-c',RELEASE_HARNESS,str(release),str(area),action],
                                 capture_output=True,text=True,timeout=25)
        assert completed.returncode==0, completed.stderr
        return True
    def mapping(): return json.loads((area/'pointers.json').read_text())
    def cp(): return json.loads((area/'cp.json').read_text())
    def announce(release,nonce):
        receipt=json.loads((release/'release-receipt.json').read_text())
        content=cp()
        content['runtime']={'version':receipt['version'],'build_id':receipt['build_id'],
            'package_sha256':receipt['sha256'],'process_id':nonce,'pid':20,'received_at':nonce}
        contract.atomic_json(area/'cp.json',content)
    initial=package(0)
    first,_=contract.prepare_release(root/'releases',initial.read_bytes(),contract.digest(initial.read_bytes()))
    contract.atomic_json(area/'pointers.json',{key:str(first) for key in ('current','previous','controller','rescue')})
    (units/helper.SERVICE).write_bytes((first/'managed-agent.service').read_bytes())
    def request(archive,index):
        contract.atomic_json(area/'cp.json',{'archive':str(archive),'operation':{
            'operation_id':f'00000000-0000-0000-0000-{index:012d}',
            'node_id':'testnode','phase':'queued','package_sha256':contract.digest(archive.read_bytes()),
            'deadline_epoch':1900},'runtime':{},'events':[],'systemd':[]})
        announce(Path(mapping()['current']),f'{index}0-before')
        run(Path(mapping()['current']),'stage')
    resolve=Path.resolve
    monkeypatch.setattr(Path,'resolve',lambda self,*a,**kw: Path(mapping()[self.name])
        if self.parent==root and self.name in {'current','previous','controller','rescue'} else resolve(self,*a,**kw))
    def dispatch():
        return launcher.dispatch(root,lambda script,action: run(script.parent,action))
    return area,root,units,first,package,request,announce,mapping,cp,run,dispatch,fixed


def test_managed_chain_uses_new_helper_client_contract_without_bootstrap(chain):
    area,root,units,first,package,request,announce,mapping,cp,run,dispatch,fixed=chain
    previous=first
    for generation in (1,2):
        request(package(generation),generation)
        assert dispatch()
        candidate=Path(mapping()['current'])
        assert candidate!=previous and Path(mapping()['previous'])==previous
        assert Path(mapping()['controller'])==previous  # old code supervises new agent
        assert (area/'state'/'implementation.txt').read_text()==f'{generation-1}:{generation-1}:{generation-1}'
        assert (units/helper.SERVICE).read_bytes()==(candidate/'managed-agent.service').read_bytes()
        assert ['daemon-reload'] in cp()['systemd']
        announce(candidate,f'{generation}1-new')
        assert dispatch()  # real subprocess probe imports the candidate modules
        assert cp()['operation']['phase']=='success'
        assert Path(mapping()['controller'])==candidate
        assert previous.is_dir()
        previous=candidate
    assert first.is_dir()
    assert (root/'upgrade_launcher.py').read_bytes()==fixed
    for name in ('marinos-appbox-updater.service','marinos-appbox-updater.timer'):
        assert (units/name).read_bytes()==(SOURCE/name).read_bytes()
    assert (area/'agent.json').read_bytes()==b'{"token":"unchanged","node_id":"testnode"}'
    assert not (root/'helper').exists()


def test_broken_candidate_helper_rolls_back_complete_previous_release(chain):
    area,root,units,first,package,request,announce,mapping,cp,run,dispatch,fixed=chain
    previous_unit=(units/helper.SERVICE).read_bytes()
    previous_files={p.name:p.read_bytes() for p in first.iterdir() if p.is_file()}
    request(package(1,broken='runtime'),1)
    dispatch()
    candidate=Path(mapping()['current'])
    announce(candidate,'11-new')  # agent is healthy; helper itself is broken
    dispatch()
    assert cp()['operation']['phase']=='rolling_back'
    assert Path(mapping()['controller'])==first
    dispatch()
    assert Path(mapping()['current'])==first
    assert (units/helper.SERVICE).read_bytes()==previous_unit
    announce(first,'12-returned')
    dispatch()
    assert cp()['operation']['phase']=='rolled_back'
    assert {p.name:p.read_bytes() for p in first.iterdir() if p.is_file()}==previous_files
    assert candidate.is_dir()  # retained for diagnosis; never used as rollback controller
    assert cp()['systemd'].count(['daemon-reload'])==2


def test_unimportable_candidate_cannot_disable_launcher_rescue(chain):
    area,root,units,first,package,request,announce,mapping,cp,run,dispatch,fixed=chain
    request(package(1,broken='runtime'),1)
    dispatch()
    candidate=Path(mapping()['current'])
    # Simulate a dispatcher/controller failure with the active capsule still armed.
    pointers=mapping(); pointers['controller']=str(candidate)
    contract.atomic_json(area/'pointers.json',pointers)
    def invoke(script,action):
        if script.parent==candidate:
            return launcher.invoke(script,action)  # actual broken Python process
        return run(script.parent,action)
    assert launcher.dispatch(root,invoke)
    assert Path(mapping()['current'])==first
    assert Path(mapping()['controller'])==first
    assert (units/helper.SERVICE).read_bytes()==(first/'managed-agent.service').read_bytes()
    announce(first,'12-restored')
    dispatch()
    assert cp()['operation']['phase']=='rolled_back'


def test_package_includes_dispatcher_abi_and_managed_components(artifact):
    _,manifest,contents=artifact
    assert manifest['launcher_abi']==1
    for name in ('upgrade_launcher.py','upgrade_helper.py','upgrade_client.py',
                 'upgrade_contract.py','managed-agent.service'):
        assert name in contents and name in manifest['files']


@pytest.mark.parametrize('directive',['ExecStartPre=/bin/sh -c bad','EnvironmentFile=/etc/secrets',
                                     'ExecStopPost=/bin/sh -c bad'])
def test_versioned_unit_rejects_arbitrary_execution(directive):
    data=(SOURCE/'managed-agent.service').read_bytes().replace(b'[Service]',b'[Service]\n'+directive.encode())
    with pytest.raises(ValueError): contract.validate_managed_unit(data)


def test_unit_reload_failure_restores_previous_unit(supervisor):
    sup,archive,pointer,clock,runtime,events,calls,online,manifest=supervisor
    old_unit=(sup.units/helper.SERVICE).read_bytes()
    original=sup.ctl
    failed=[False]
    def ctl(*args):
        if args==('daemon-reload',) and not failed[0]:
            failed[0]=True
            raise RuntimeError('reload failure')
        return original(*args)
    sup.ctl=ctl
    state=sup.begin('00000000-0000-0000-0000-000000000001',archive)
    sup.step(state)
    assert state['phase']=='rolling_back'
    sup.step(state)
    assert (sup.units/helper.SERVICE).read_bytes()==old_unit
    assert pointer['current']==Path(state['previous'])


def test_recovery_does_not_reset_rollback_deadline(supervisor):
    sup,archive,pointer,clock,runtime,events,calls,online,manifest=supervisor
    state=sup.begin('00000000-0000-0000-0000-000000000001',archive)
    sup.step(state)
    sup.recover()
    deadline=sup.load()['rollback_deadline']
    clock[0]+=50
    sup.recover()
    assert sup.load()['rollback_deadline']==deadline
