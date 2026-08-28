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


@pytest.mark.parametrize('modules', [(), ('reference_contract.py',),
    ('reference_contract.py', 'upgrade_client.py', 'upgrade_contract.py')])
def test_legacy_bootstrap_preserves_config_and_can_resume(tmp_path, monkeypatch, artifact, modules):
    data, _, _ = artifact
    legacy, root, state, spool, units = (tmp_path/name for name in ('legacy','root','state','spool','units'))
    legacy.mkdir(); units.mkdir()
    (units/helper.SERVICE).write_bytes((SOURCE/helper.SERVICE).read_bytes())
    for name in ('marinos-appbox-agent.py', *modules):
        (legacy/name).write_bytes((SOURCE/name).read_bytes().replace(b'alpha.5',b'alpha.4'))
    if not modules:
        (legacy/'marinos-appbox-agent.py').write_bytes(b'VERSION = "1.6.0-alpha.4"\nprint(VERSION)\n')
    original_files = {path.name: path.read_bytes() for path in legacy.iterdir()}
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
    snapshots = list((root/'releases').glob('legacy-*'))
    assert len(snapshots) == 1
    assert {path.name: path.read_bytes() for path in snapshots[0].glob('*.py')} == original_files
    assert {path.name: path.read_bytes() for path in legacy.iterdir()} == original_files
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


@pytest.mark.parametrize('modules', [(), ('reference_contract.py',), ('upgrade_client.py',),
    ('upgrade_contract.py',), ('upgrade_client.py', 'upgrade_contract.py'),
    ('reference_contract.py', 'upgrade_client.py', 'upgrade_contract.py')])
def test_legacy_snapshot_identity_uses_only_installed_files(tmp_path, modules):
    legacy = tmp_path/'legacy'; legacy.mkdir()
    original = {'marinos-appbox-agent.py': b'VERSION = "1.6.0-alpha.4"\r\nprint(VERSION)\r\n',
                **{name: b'# original legacy module\n' for name in modules}}
    for name, data in original.items():
        (legacy/name).write_bytes(data)
    first = helper.snapshot_legacy(tmp_path/'root', legacy)
    expected = 'legacy-' + contract.digest(contract.canonical(
        {name: contract.digest(data) for name, data in original.items()}))
    assert first.name == expected
    assert json.loads((first/'release-receipt.json').read_text()) == {
        'version':'1.6.0-alpha.4', 'build_id':expected, 'sha256':None}
    assert {p.name: p.read_bytes() for p in first.glob('*.py')} == original
    assert helper.snapshot_legacy(tmp_path/'root', legacy) == first
    # Neither an unrelated source file nor the destination location affects identity.
    (legacy/'unrelated.py').write_text('not part of the agent')
    assert helper.snapshot_legacy(tmp_path/'other-root', legacy).name == expected
    if modules:
        (legacy/modules[0]).write_bytes(b'# changed installed module\n')
        assert helper.snapshot_legacy(tmp_path/'root', legacy).name != expected
        assert {p.name: p.read_bytes() for p in first.glob('*.py')} == original
    else:
        # Refuse an artificially populated backup instead of accepting a mixed release.
        (first/'reference_contract.py').write_bytes(b'# not a legacy file\n')
        with pytest.raises(ValueError, match='Unexpected legacy backup file'):
            helper.snapshot_legacy(tmp_path/'root', legacy)


@pytest.mark.parametrize('name', ['marinos-appbox-agent.py', 'reference_contract.py',
                                 'upgrade_client.py', 'upgrade_contract.py'])
@pytest.mark.parametrize('failure', ['permission', 'syntax', 'directory', 'symlink'])
def test_legacy_bootstrap_rejects_invalid_present_files_before_changes(tmp_path, monkeypatch, artifact, name, failure):
    data, _, _ = artifact
    legacy = tmp_path/'legacy'; legacy.mkdir()
    (legacy/'marinos-appbox-agent.py').write_bytes(b'VERSION = "1.6.0-alpha.4"\n')
    bad = legacy/name
    if name != 'marinos-appbox-agent.py':
        bad.write_bytes(b'# installed module\n')
    expected = ValueError
    if failure == 'permission':
        original_read = Path.read_bytes
        def read(path):
            if path == bad:
                raise PermissionError('unreadable installed legacy file')
            return original_read(path)
        monkeypatch.setattr(Path, 'read_bytes', read)
        expected = PermissionError
    elif failure == 'syntax':
        bad.write_bytes(b'def broken(\n')
        expected = SyntaxError
    elif failure == 'directory':
        bad.unlink(); bad.mkdir()
    else:
        original_stat = Path.lstat
        monkeypatch.setattr(Path, 'lstat', lambda path: Mock(st_mode=stat.S_IFLNK)
                            if path == bad else original_stat(path))
    archive = tmp_path/'agent.zip'; archive.write_bytes(data)
    root, state, spool, units = (tmp_path/n for n in ('root','state','spool','units'))
    config_file = tmp_path/'agent.json'; config_file.write_bytes(b'{"node_id":"testnode","token":"unchanged"}')
    ctl, transport = Mock(), Mock()
    with pytest.raises(expected):
        helper.bootstrap(archive, contract.digest(data), json.loads(config_file.read_text()),
                         root, state, spool, legacy, units, ctl, transport)
    ctl.assert_not_called(); transport.assert_not_called()
    assert not any(p.exists() for p in (root, state, spool, units))
    assert config_file.read_bytes() == b'{"node_id":"testnode","token":"unchanged"}'


@pytest.mark.parametrize('source,error', [(None, FileNotFoundError),
    (b'VERSION = "invalid"\n', ValueError),
    (b'VERSION = str("1.6.0-alpha.4")\n', ValueError)])
def test_legacy_bootstrap_requires_main_agent_and_static_version(tmp_path, artifact, source, error):
    data, _, _ = artifact
    legacy = tmp_path/'legacy'; legacy.mkdir()
    (legacy/'reference_contract.py').write_bytes(b'# cannot replace the main agent\n')
    if source is not None:
        (legacy/'marinos-appbox-agent.py').write_bytes(source)
    archive = tmp_path/'agent.zip'; archive.write_bytes(data)
    root, state = tmp_path/'root', tmp_path/'state'
    ctl, transport = Mock(), Mock()
    with pytest.raises(error):
        helper.bootstrap(archive, contract.digest(data), {'node_id':'testnode'},
                         root, state, tmp_path/'spool', legacy, tmp_path/'units', ctl, transport)
    ctl.assert_not_called(); transport.assert_not_called()
    assert not root.exists() and not state.exists()


def test_monolithic_bootstrap_retry_after_old_preflight_with_existing_free_lock(tmp_path, monkeypatch, artifact):
    from types import SimpleNamespace
    data, _, _ = artifact
    legacy, root, state, spool, units = (tmp_path/n for n in ('legacy','root','state','spool','units'))
    legacy.mkdir(); state.mkdir(); units.mkdir()
    source = b'VERSION = "1.6.0-alpha.4"\nprint(VERSION)\n'
    (legacy/'marinos-appbox-agent.py').write_bytes(source)
    (units/helper.SERVICE).write_bytes((SOURCE/helper.SERVICE).read_bytes())
    (state/'supervisor.lock').touch()
    # Reproduce the old preflight: only the lock exists, no operation or managed root.
    with pytest.raises(FileNotFoundError):
        for name in ('marinos-appbox-agent.py', 'reference_contract.py'):
            (legacy/name).read_bytes()
    archive = tmp_path/'agent.zip'; archive.write_bytes(data)
    config_file = tmp_path/'agent.json'; config_file.write_bytes(b'{"node_id":"testnode","token":"unchanged"}')
    original = config_file.read_bytes()
    opid = '00000000-0000-0000-0000-000000000002'
    remote = {'operation_id':opid, 'phase':'queued'}
    def transport(config, path, payload=None):
        if path.endswith('/events'):
            remote['phase'] = payload['phase']
        return {'operation':remote.copy()}
    ctl, locks = Mock(), []
    def flock(lock, flags):
        assert flags == 6 and not lock.closed and Path(lock.name).stat().st_size == 0
        locks.append(lock)
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock))
    # Simulate only the Linux primitives; execute real main(), preflight and bootstrap.
    monkeypatch.setattr(helper, 'os', SimpleNamespace(name='posix', geteuid=lambda:0, fsync=helper.os.fsync))
    monkeypatch.setattr(helper, 'STATE', state)
    monkeypatch.setattr(helper, 'CONFIG', config_file)
    monkeypatch.setattr(helper, 'switch_pointer', Mock())
    bootstrap = helper.bootstrap
    monkeypatch.setattr(helper, 'bootstrap', lambda archive, checksum, config: bootstrap(
        archive, checksum, config, root, state, spool, legacy, units, ctl, transport))
    monkeypatch.setattr(sys, 'argv', ['upgrade_helper.py', 'bootstrap', '--archive', str(archive),
                                    '--sha256', contract.digest(data)])
    helper.main()
    assert len(locks) == 1 and locks[0].closed
    assert config_file.read_bytes() == original
    assert not any(call.args[0] in {'stop','start','restart'} for call in ctl.call_args_list)
    assert remote['phase'] == 'prepared' and (state/'bootstrap.json').is_file()
    assert json.loads((spool/'request.json').read_text())['operation_id'] == opid
    assert {p.name: p.read_bytes() for p in legacy.iterdir()} == {'marinos-appbox-agent.py':source}
    snapshot = next((root/'releases').glob('legacy-*'))
    assert not (snapshot/'reference_contract.py').exists()


def test_rollback_restarts_exact_monolithic_snapshot(supervisor, tmp_path):
    sup, archive, pointer, clock, runtime, events, calls, _, _ = supervisor
    legacy = tmp_path/'legacy'; legacy.mkdir()
    source = b'VERSION = "1.6.0-alpha.4"\nprint(VERSION)\n'
    (legacy/'marinos-appbox-agent.py').write_bytes(source)
    snapshot = helper.snapshot_legacy(sup.root, legacy)
    receipt = json.loads((snapshot/'release-receipt.json').read_text())
    pointer.update(current=snapshot, target=snapshot, previous=snapshot)
    legacy_unit = (SOURCE/helper.SERVICE).read_bytes().replace(
        b'ExecStart=/usr/local/sbin/marinos-appbox-agent.py',
        b'ExecStart=/usr/bin/python3 /opt/marinos-appbox-agent/current/marinos-appbox-agent.py')
    (sup.units/helper.SERVICE).write_bytes(legacy_unit)
    state = sup.begin('00000000-0000-0000-0000-000000000001', archive)
    assert state['previous_version'] == '1.6.0-alpha.4'
    assert state['previous_build'] == receipt['build_id']
    sup.step(state)
    assert state['phase'] == 'awaiting_heartbeat'
    clock[0] = state['deadline'] + 1
    sup.step(state)
    assert state['phase'] == 'rolling_back'
    original_ctl, processes = sup.ctl, []
    def ctl(*args):
        if args == ('start', helper.SERVICE):
            assert pointer['current'] == snapshot
            assert (sup.units/helper.SERVICE).read_bytes() == legacy_unit
            # Real isolated Python execution proves no candidate module is required.
            result = subprocess.run([sys.executable, '-I', '-B', str(snapshot/'marinos-appbox-agent.py')],
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0 and result.stderr == ''
            processes.append(result)
            runtime.clear()
            runtime.update(version=result.stdout.strip(), received_at='03')
        return original_ctl(*args)
    sup.ctl = ctl
    sup.step(state)
    assert sup.load()['phase'] == 'rolled_back' and len(processes) == 1
    assert pointer['current'] == pointer['previous'] == snapshot
    assert pointer['controller'] == pointer['rescue'] == sup.controller
    assert {p.name: p.read_bytes() for p in snapshot.glob('*.py')} == {'marinos-appbox-agent.py':source}
    assert (sup.root/'releases'/state['package_sha256']).is_dir()
    assert calls.count(('start', helper.SERVICE)) == 2  # candidate, then monolithic rollback
    assert events[-2:] == ['rolling_back', 'rolled_back']


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
elif action=='schedule':
    c.reconcile_scheduler(root,state,spool,units,ctl)
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
        assert dispatch()  # confirmed controller adopts its versioned scheduler
        run(candidate, 'schedule')
        scheduler=json.loads((area/'state'/'scheduler.json').read_text())
        assert scheduler['release']==str(candidate) and scheduler['phase']=='complete'
        dropin=units/'marinos-appbox-updater.service.d'/'30-adaptive-scheduler.conf'
        assert (candidate/'upgrade_client.py').as_posix() in dropin.read_text()
        assert cp()['systemd'][-1]==['--no-block','stop','marinos-appbox-updater.timer']
        previous=candidate
    assert first.is_dir()
    assert (root/'upgrade_launcher.py').read_bytes()==fixed
    assert (units/'marinos-appbox-updater.service').read_bytes()==(SOURCE/'marinos-appbox-updater.service').read_bytes()
    assert (units/'marinos-appbox-updater.timer').read_bytes()==(SOURCE/'marinos-appbox-updater.timer').read_bytes().replace(b'\r\n',b'\n')
    assert (area/'agent.json').read_bytes()==b'{"token":"unchanged","node_id":"testnode"}'
    assert not (root/'helper').exists()


def test_broken_candidate_helper_rolls_back_complete_previous_release(chain):
    area,root,units,first,package,request,announce,mapping,cp,run,dispatch,fixed=chain
    previous_unit=(units/helper.SERVICE).read_bytes()
    previous_files={p.name:p.read_bytes() for p in first.iterdir() if p.is_file()}
    request(package(1,broken='runtime'),1)
    assert client.install_scheduler(first,root,area/'state',units,Mock(return_value='active'))
    run(first,'schedule')
    assert cp()['systemd'][-1]==['--no-block','start','marinos-appbox-updater.timer']
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
    run(first,'schedule')
    assert cp()['systemd'][-1]==['--no-block','stop','marinos-appbox-updater.timer']


def test_unimportable_candidate_cannot_disable_launcher_rescue(chain):
    area,root,units,first,package,request,announce,mapping,cp,run,dispatch,fixed=chain
    request(package(1,broken='runtime'),1)
    assert client.install_scheduler(first,root,area/'state',units,Mock(return_value='active'))
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
    run(first,'schedule')  # post hook still works despite broken candidate/controller
    assert cp()['systemd'][-1]==['--no-block','stop','marinos-appbox-updater.timer']


def test_package_includes_dispatcher_abi_and_managed_components(artifact):
    _,manifest,contents=artifact
    assert manifest['launcher_abi']==1
    for name in ('upgrade_launcher.py','upgrade_helper.py','upgrade_client.py',
                 'upgrade_contract.py','managed-agent.service'):
        assert name in contents and name in manifest['files']


@pytest.mark.parametrize('mode,output,success', [
    ('free', 'controller:tick\n', True),
    ('busy', '', True),
    ('controller_error', 'controller:tick\nrescue:recover\n', True),
    ('lock_io_error', '', False),
    ('lock_permission_error', '', False),
    ('other_blocking_error', '', False),
])
def test_launcher_lock_exit_status_and_rescue(tmp_path, mode, output, success):
    # Run the real main/dispatch in a process; only POSIX flock/root and controller
    # execution are simulated so Windows also verifies exit code and traceback.
    code = r'''
import errno, importlib.util, sys
from pathlib import Path
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('launcher_test', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.STATE = Path(sys.argv[2])
module.os = SimpleNamespace(name='posix', geteuid=lambda: 0)
mode = sys.argv[3]
locks = []
def flock(lock, flags):
    assert flags == 6 and not lock.closed
    locks.append(lock)
    if mode == 'busy':
        raise BlockingIOError(errno.EAGAIN, 'already locked')
    if mode == 'lock_io_error':
        raise OSError(errno.EIO, 'real lock I/O error')
    if mode == 'lock_permission_error':
        raise PermissionError(errno.EACCES, 'real lock permission error')
    if mode == 'other_blocking_error':
        raise BlockingIOError(errno.EINPROGRESS, 'not lock contention')
sys.modules['fcntl'] = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock)
module.resolve_helper = lambda root, pointer: pointer
def run(controller, action):
    print(controller + ':' + action)
    if mode == 'controller_error' and controller == 'controller':
        raise OSError(errno.EIO, 'controller failed')
    return True
dispatch = module.dispatch
module.dispatch = lambda: dispatch(root=module.STATE, run=run)
for _ in range(2 if mode == 'busy' else 1):
    module.main()
assert all(lock.closed for lock in locks)
'''
    result = subprocess.run([sys.executable, '-I', '-c', code,
        str(SOURCE/'upgrade_launcher.py'), str(tmp_path), mode],
        capture_output=True, text=True, timeout=15)
    assert result.stdout == output
    if success:
        assert result.returncode == 0, result.stderr
        assert result.stderr == ''
    else:
        assert result.returncode != 0
        assert 'Traceback' in result.stderr


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


@pytest.fixture
def scheduler(tmp_path, artifact):
    root=tmp_path/'root'; state=tmp_path/'state'; spool=tmp_path/'spool'; units=tmp_path/'units'
    for folder in (state,spool,units): folder.mkdir()
    release,_=contract.prepare_release(root/'releases',artifact[0],contract.digest(artifact[0]))
    timer=client.UPDATER+'.timer'; watcher=client.UPDATER+'.path'
    old_timer=(SOURCE/timer).read_bytes().replace(b'OnActiveSec=5s\n',b'').replace(b'OnActiveSec=5s\r\n',b'')
    (units/timer).write_bytes(old_timer)
    (units/(client.UPDATER+'.service')).write_bytes((SOURCE/(client.UPDATER+'.service')).read_bytes())
    config=tmp_path/'agent.json'; config.write_bytes(b'{"token":"unchanged"}')
    systemd={'enabled':{timer}, 'active':{timer}, 'calls':[], 'ready':True}
    def ctl(*args):
        systemd['calls'].append(args)
        command=list(args)
        if command[0]=='--no-block': command.pop(0)
        action,*names=command
        if action=='is-enabled':
            return 'enabled' if names[0] in systemd['enabled'] else 'disabled'
        if action=='is-active':
            if names[0] in systemd['active']: return 'active'
            raise RuntimeError('inactive')
        if action=='enable': systemd['enabled'].update(names)
        if action=='disable':
            names=[n for n in names if n!='--now']
            systemd['enabled'].difference_update(names); systemd['active'].difference_update(names)
        if action in {'start','restart'}:
            systemd['active'].update(n for n in names if n!=watcher or systemd['ready'])
        if action=='stop': systemd['active'].difference_update(names)
        return ''
    def install(): return client.install_scheduler(release,root,state,units,ctl)
    def reconcile(): return client.reconcile_scheduler(root,state,spool,units,ctl)
    return root,state,spool,units,release,systemd,ctl,install,reconcile,old_timer,config


def test_scheduler_idle_boot_check_then_no_fast_timer(scheduler):
    root,state,spool,units,release,systemd,ctl,install,reconcile,old_timer,config=scheduler
    assert install()
    assert not reconcile()
    assert systemd['active']=={client.UPDATER+'.path'}
    assert client.UPDATER+'.timer' in systemd['enabled']  # boot recovery remains enabled
    # Model systemd starting enabled units on reboot, then executing its boot tick.
    systemd['active']=systemd['enabled'].copy()
    assert client.UPDATER+'.timer' in systemd['active']
    assert not reconcile()
    assert systemd['active']=={client.UPDATER+'.path'}
    assert config.read_bytes()==b'{"token":"unchanged"}'


@pytest.mark.parametrize('phase', ['installing','restarting','awaiting_heartbeat','rolling_back'])
def test_scheduler_active_and_reboot_keep_reconciliation_independent_of_agent(scheduler,phase):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    assert install()
    contract.atomic_json(state/'state.json',{'phase':phase,'reported':True})
    assert reconcile()
    systemd['active']=systemd['enabled'].copy()  # no running agent required
    assert reconcile()
    assert client.UPDATER+'.timer' in systemd['active']
    assert not any(helper.SERVICE in call for call in systemd['calls'])


@pytest.mark.parametrize('phase', sorted(contract.TERMINAL))
def test_scheduler_terminal_only_sleeps_after_durable_cp_ack(scheduler,monkeypatch,phase):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    assert install()
    resolve=Path.resolve
    monkeypatch.setattr(Path,'resolve',lambda self,*a,**kw: release if self==root/'controller' else resolve(self,*a,**kw))
    capsule={'phase':phase,'reported':False,'candidate':str(release),'controller_handed_off':True}
    contract.atomic_json(state/'state.json',capsule)
    assert reconcile()
    capsule.update(reported=True,events=[{'phase':phase}])
    contract.atomic_json(state/'state.json',capsule)
    assert reconcile()  # never discard a pending notification
    capsule['events']=[]
    contract.atomic_json(state/'state.json',capsule)
    assert not reconcile()
    assert client.UPDATER+'.timer' not in systemd['active']


def test_scheduler_request_during_idle_transition_remains_recoverable(scheduler,monkeypatch):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    assert install()
    pending=client.scheduler_pending
    def raced(*args):
        result=pending(*args)
        contract.atomic_json(spool/'request.json',{'operation_id':'new-durable-request'})
        return result
    monkeypatch.setattr(client,'scheduler_pending',raced)
    assert not reconcile()
    monkeypatch.setattr(client,'scheduler_pending',pending)
    # PathChanged records this write independently of timer state. Its next service
    # activation re-reads durable state; Linux inotify/job ordering remains E2E.
    assert reconcile()
    assert client.UPDATER+'.timer' in systemd['active']
    watcher=(units/(client.UPDATER+'.path')).read_text()
    assert 'PathChanged=/var/lib/marinos-appbox-agent/upgrades/request.json' in watcher
    assert 'PathExists=' not in watcher  # a still-present download must not busy-loop


def test_scheduler_migrates_old_timer_and_preserves_fixed_service_and_config(scheduler):
    root,state,spool,units,release,systemd,ctl,install,reconcile,old_timer,config=scheduler
    service=units/(client.UPDATER+'.service'); fixed=service.read_bytes()
    assert b'OnActiveSec=' not in old_timer
    assert install()
    assert (units/(client.UPDATER+'.timer')).read_bytes()==(release/(client.UPDATER+'.timer')).read_bytes()
    assert service.read_bytes()==fixed and config.read_bytes()==b'{"token":"unchanged"}'
    assert ('daemon-reload',) in systemd['calls']
    assert not reconcile()
    before=list(systemd['calls'])
    assert install() and systemd['calls']==before  # no repeated unit writes/reloads


def test_scheduler_waits_for_async_watcher_activation(scheduler):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    systemd['ready']=False
    assert not install()
    record=json.loads((state/'scheduler.json').read_text())
    assert record['phase']=='activating' and record['error_code'] is None
    assert reconcile()  # remains fast until watcher ready, no premature idle
    assert systemd['calls'].count(('daemon-reload',))==1
    systemd['ready']=True
    assert not reconcile()
    assert json.loads((state/'scheduler.json').read_text())['phase']=='complete'


@pytest.mark.parametrize('failure', ['daemon-reload','enable','watcher_timeout'])
def test_scheduler_migration_failure_restores_units_and_retries(scheduler,monkeypatch,failure):
    root,state,spool,units,release,systemd,ctl,install,reconcile,old_timer,_=scheduler
    if failure=='watcher_timeout':
        systemd['ready']=False
        assert not install()
        deadline=json.loads((state/'scheduler.json').read_text())['activation_deadline']
        monkeypatch.setattr(client.time,'time',lambda:deadline)
        assert not install()
    else:
        failed=False
        def failing(*args):
            nonlocal failed
            if args[0]==failure and not failed:
                failed=True
                raise RuntimeError('injected systemd error')
            return ctl(*args)
        assert not client.install_scheduler(release,root,state,units,failing)
    assert (units/(client.UPDATER+'.timer')).read_bytes()==old_timer
    assert not (units/(client.UPDATER+'.path')).exists()
    assert not (units/(client.UPDATER+'.service.d')/'30-adaptive-scheduler.conf').exists()
    assert client.UPDATER+'.timer' in systemd['active']
    assert json.loads((state/'scheduler.json').read_text())['phase']=='retry'
    systemd['ready']=True
    assert install() and not reconcile()


def test_scheduler_migration_replays_after_interrupted_reload(scheduler):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    def power_loss(*args):
        if args==('daemon-reload',): raise SystemExit('simulated power loss')
        return ctl(*args)
    with pytest.raises(SystemExit):
        client.install_scheduler(release,root,state,units,power_loss)
    assert json.loads((state/'scheduler.json').read_text())['phase']=='prepared'
    systemd['active']=systemd['enabled'].copy()
    assert not reconcile()  # replay transaction and return idle
    assert json.loads((state/'scheduler.json').read_text())['phase']=='complete'
    assert ('daemon-reload',) in systemd['calls']


def test_scheduler_failed_replacement_restores_previous_dropin(scheduler):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    assert install()
    previous={name:(units/name).read_bytes() for name in client.scheduler_units(release,root)}
    record=json.loads((state/'scheduler.json').read_text())
    record['release']='previous-scheduler-release'  # model a later managed migration
    contract.atomic_json(state/'scheduler.json',record)
    failed=False
    def failing(*args):
        nonlocal failed
        if args[0]=='enable' and not failed:
            failed=True
            raise RuntimeError('enable failure')
        return ctl(*args)
    assert not client.install_scheduler(release,root,state,units,failing)
    assert {name:(units/name).read_bytes() for name in previous}==previous
    assert client.UPDATER+'.path' in systemd['active']
    assert client.UPDATER+'.timer' in systemd['active']
    assert install()


def test_scheduler_replays_interrupted_unit_rollback(scheduler,monkeypatch):
    root,state,spool,units,release,systemd,ctl,install,reconcile,old_timer,_=scheduler
    atomic=client.atomic_file
    crashed=False
    def interrupted(path,data):
        nonlocal crashed
        if data==old_timer and not crashed:
            crashed=True
            raise SystemExit('power loss during rollback')
        return atomic(path,data)
    monkeypatch.setattr(client,'atomic_file',interrupted)
    def failing(*args):
        if args[0]=='enable': raise RuntimeError('enable failed')
        return ctl(*args)
    with pytest.raises(SystemExit):
        client.install_scheduler(release,root,state,units,failing)
    assert json.loads((state/'scheduler.json').read_text())['phase']=='rollback_pending'
    assert not install()
    assert (units/(client.UPDATER+'.timer')).read_bytes()==old_timer
    assert json.loads((state/'scheduler.json').read_text())['phase']=='retry'
    assert install() and not reconcile()


def test_scheduler_corrupt_state_keeps_recovery_timer(scheduler):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    assert install()
    (state/'state.json').write_text('{damaged')
    assert reconcile()
    assert client.UPDATER+'.timer' in systemd['active']


def test_scheduler_old_rescue_terminal_does_not_require_candidate_code(scheduler,monkeypatch):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    assert install()
    # The service post hook is pinned to a confirmed release, not current/controller.
    contract.atomic_json(state/'state.json',{'phase':'rolled_back','reported':True})
    assert not reconcile()
    hook=(units/(client.UPDATER+'.service.d')/'30-adaptive-scheduler.conf').read_text()
    assert (release/'upgrade_client.py').as_posix() in hook
    assert '/current/' not in hook and '/controller/' not in hook


@pytest.mark.parametrize('mode', ['free','busy'])
def test_scheduler_lock_serializes_with_launcher_without_traceback(tmp_path,mode):
    code=r'''
import errno, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
import upgrade_client as c
c.UPDATER_STATE=Path(sys.argv[2])
c.os=SimpleNamespace(name='posix',geteuid=lambda:0)
def flock(lock,flags):
    assert flags==6
    if sys.argv[3]=='busy': raise BlockingIOError(errno.EAGAIN,'busy')
sys.modules['fcntl']=SimpleNamespace(LOCK_EX=2,LOCK_NB=4,flock=flock)
c.reconcile_scheduler=lambda:print('reconcile')
c.scheduler_systemctl=lambda *args:None
c.scheduler_main()
'''
    result=subprocess.run([sys.executable,'-I','-B','-c',code,str(SOURCE),str(tmp_path),mode],
                          capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and result.stderr==''
    assert result.stdout==('reconcile\n' if mode=='free' else '')


def test_scheduler_rejects_unvalidated_or_arbitrary_units(scheduler):
    root,state,spool,units,release,systemd,ctl,install,reconcile,_,_=scheduler
    timer=release/(client.UPDATER+'.timer')
    timer.write_bytes(timer.read_bytes()+b'\n[Service]\nExecStart=/bin/sh bad\n')
    with pytest.raises(ValueError,match='checksum'): install()
    manifest=json.loads((release/'agent-manifest.json').read_text())
    manifest['files'][timer.name]=contract.digest(timer.read_bytes())
    contract.atomic_json(release/'agent-manifest.json',manifest)
    with pytest.raises(ValueError,match='Unsupported adaptive timer'): install()
    assert not (state/'scheduler.json').exists()
    assert systemd['calls']==[]


def test_scheduler_setup_failure_does_not_discard_confirmed_controller(chain,monkeypatch):
    area,root,units,first,package,request,announce,mapping,cp,run,dispatch,fixed=chain
    request(package(1),1)
    dispatch()
    candidate=Path(mapping()['current'])
    announce(candidate,'11-new')
    dispatch()
    assert cp()['operation']['phase']=='success'
    ctl=Mock(return_value='active')
    sup=helper.Supervisor({'node_id':'testnode'},root,area/'state',area/'spool',ctl,
                          units=units,controller=candidate)
    monkeypatch.setattr(helper,'install_scheduler',Mock(side_effect=OSError('unit recovery I/O failure')))
    sup.tick()
    assert Path(mapping()['controller'])==candidate
    assert ctl.call_args_list==[(('--no-block','start','marinos-appbox-updater.timer'),)]
    error=area/'state/scheduler-error.json'
    assert json.loads(error.read_text())=={'error_code':'scheduler_setup_failed'}
    monkeypatch.setattr(helper,'install_scheduler',Mock(return_value=True))
    sup.tick()
    assert not error.exists()
