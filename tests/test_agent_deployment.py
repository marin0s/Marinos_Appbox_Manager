import hashlib
import importlib.util
import json
import tempfile
import unittest
import io
import tarfile
import errno
import os
import stat
import pytest
from pathlib import Path
from unittest.mock import patch
from reference_fixtures import reference_archive
from app import main

AGENT_PATH = Path(__file__).parents[1] / "agent" / "marinos-appbox-agent.py"
spec = importlib.util.spec_from_file_location("marinos_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class DeletionDocker:
    def __init__(self, names=()):
        self.names = set(names)
        self.calls = []
        self.compose_result = (0, '', '')
        self.rm_absent = False

    def __call__(self, args, timeout=15):
        self.calls.append(args)
        if args[1] == 'ps':
            return 0, '\n'.join(sorted(self.names)), ''
        if args[1] == 'inspect':
            name = args[-1]
            if name not in self.names:
                return 1, '', f'Error: No such object: {name}'
            return 0, json.dumps([{'Config':{'Labels':{'com.docker.compose.project':'ab40ah'}}}]), ''
        if args[1] == 'compose':
            return self.compose_result
        if args[1] == 'rm':
            self.names.discard(args[-1])
            return (1, '', f'Error response from daemon: No such container: {args[-1]}') if self.rm_absent else (0,'removed','')
        raise AssertionError(args)


@pytest.mark.parametrize('mode', ['delete','purge','archive'])
@pytest.mark.parametrize('has_path,has_container,has_compose', [
    (True,True,True), (True,False,True), (False,True,False), (False,False,False), (True,True,False)])
def test_appbox_deletion_absence_matrix_and_repeat(tmp_path,monkeypatch,mode,has_path,has_container,has_compose):
    directory=tmp_path/'ab40ah'
    if has_path:
        directory.mkdir(); (directory/'data').write_text('persistent config')
    if has_compose: (directory/'compose.yml').write_text('services: {}')
    docker=DeletionDocker(['plex-appb-40ah'] if has_container else [])
    monkeypatch.setattr(agent,'run',docker)
    payload={'client_id':'ab40ah','action':'delete','deletion_mode':mode,'containers':['plex-appb-40ah']}
    for _ in range(2):
        result=agent.execute_command({'appbox_base_dir':str(tmp_path)}, {'command_type':'appbox_action','payload':payload})
        assert result['containers_remaining']==[] and not docker.names
        assert result['path_exists']==(has_path and mode=='archive')
        assert directory.exists()==result['path_exists']
        if mode!='archive': assert 'idempotente terminée' in result['output']
    assert not any(c[1]=='network' for c in docker.calls)


def test_reference_cache_delete_present_absent_and_dispatch(tmp_path):
    root=tmp_path/'reference-cache'; root.mkdir()
    content=b'immutable reference archive'; checksum=hashlib.sha256(content).hexdigest()
    cached=root/f'{checksum}.tar.gz'; cached.write_bytes(content)
    payload={'version_id':'v1','checksum':checksum,'local_path':str(cached)}
    result=agent.execute_command({'reference_cache_dir':str(root)},
        {'command_type':'reference_cache_delete','payload':payload})
    assert result['cache_absent'] and result['bytes_freed']==len(content) and not cached.exists()
    repeated=agent.delete_reference_cache({'reference_cache_dir':str(root)},payload)
    assert repeated['cache_absent'] and repeated['bytes_freed']==0
    root.rmdir()
    assert agent.delete_reference_cache({'reference_cache_dir':str(root)},payload)['cache_absent']


@pytest.mark.parametrize('kind',['outside','root','relative','wrong_checksum','wrong_version','directory','symlink'])
def test_reference_cache_delete_rejects_unsafe_or_wrong_target(tmp_path,monkeypatch,kind):
    root=tmp_path/'cache'; root.mkdir(); data=b'cache'; checksum=hashlib.sha256(data).hexdigest()
    target=root/f'{checksum}.tar.gz'; target.write_bytes(data)
    supplied={'outside':tmp_path/'outside.tar.gz','root':root,'relative':Path('cache')/target.name}.get(kind,target)
    payload={'version_id':'../wrong' if kind=='wrong_version' else 'v1',
             'checksum':'0'*64 if kind=='wrong_checksum' else checksum,'local_path':str(supplied)}
    if kind=='directory': target.unlink(); target.mkdir()
    if kind=='symlink':
        original=Path.lstat
        monkeypatch.setattr(Path,'lstat',lambda path: type('Info',(),{'st_mode':stat.S_IFLNK})()
                            if path==target else original(path))
    with pytest.raises(RuntimeError): agent.delete_reference_cache({'reference_cache_dir':str(root)},payload)
    if kind not in {'directory'}: assert target.exists()


@pytest.mark.parametrize('failure',[PermissionError('denied'),OSError(errno.EROFS,'read only'),OSError(errno.EIO,'I/O')])
def test_reference_cache_delete_preserves_real_filesystem_errors(tmp_path,monkeypatch,failure):
    root=tmp_path/'cache'; root.mkdir(); data=b'cache'; checksum=hashlib.sha256(data).hexdigest()
    target=root/f'{checksum}.tar.gz'; target.write_bytes(data)
    monkeypatch.setattr(Path,'unlink',lambda path: (_ for _ in ()).throw(failure) if path==target else None)
    with pytest.raises(type(failure)):
        agent.delete_reference_cache({'reference_cache_dir':str(root)},
            {'version_id':'v1','checksum':checksum,'local_path':str(target)})
    assert target.exists()


@pytest.mark.parametrize('code', [0,1])
def test_appbox_delete_compose_no_resource_and_rm_no_such_container(tmp_path,monkeypatch,code):
    directory=tmp_path/'ab40ah'; directory.mkdir(); (directory/'compose.yml').write_text('services: {}')
    docker=DeletionDocker(['plex-appb-40ah']); docker.rm_absent=True
    docker.compose_result=(code,'','Warning: No resource found to remove')
    monkeypatch.setattr(agent,'run',docker)
    assert not agent.delete_appbox_resources(tmp_path,'ab40ah',['plex-appb-40ah'],'purge')['path_exists']


@pytest.mark.parametrize('error', [PermissionError(errno.EACCES,'denied'), OSError(errno.EROFS,'read only'), OSError(errno.EIO,'I/O')])
def test_appbox_delete_real_filesystem_errors_propagate(tmp_path,monkeypatch,error):
    directory=tmp_path/'ab40ah'; directory.mkdir()
    monkeypatch.setattr(agent,'run',DeletionDocker())
    def fail(*args,**kwargs): raise error
    monkeypatch.setattr(agent.shutil,'rmtree',fail)
    with pytest.raises(type(error)) as caught:
        agent.delete_appbox_resources(tmp_path,'ab40ah',[],'purge')
    assert caught.value.errno==error.errno and directory.exists()


@pytest.mark.parametrize('stage', ['ps','inspect','compose','rm','final_ps','after_cleanup'])
def test_appbox_delete_docker_failure_is_not_absence(tmp_path,monkeypatch,stage):
    directory=tmp_path/'ab40ah'; directory.mkdir(); (directory/'compose.yml').write_text('services: {}')
    docker=DeletionDocker(['plex-appb-40ah']); ps_calls=0
    def fail(args,timeout=15):
        nonlocal ps_calls
        if args[1]=='ps': ps_calls+=1
        if args[1]==stage or (stage=='final_ps' and args[1]=='ps' and ps_calls==2) or (stage=='after_cleanup' and args[1]=='ps' and ps_calls==3):
            return 1,'','Cannot connect to the Docker daemon'
        return docker(args,timeout)
    monkeypatch.setattr(agent,'run',fail)
    with pytest.raises(RuntimeError,match='Docker daemon'):
        agent.delete_appbox_resources(tmp_path,'ab40ah',['plex-appb-40ah'],'purge')
    assert directory.exists() == (stage!='after_cleanup')


@pytest.mark.parametrize('identifier,path', [('../other',None),('/srv/appboxes',None),('ab40ah','/etc'),
                                          ('ab40ah','root'),('ab40ah','sibling')])
def test_appbox_delete_rejects_traversal_root_and_other_client(tmp_path,monkeypatch,identifier,path):
    supplied=str(tmp_path) if path=='root' else str(tmp_path/'other') if path=='sibling' else path
    with patch.object(agent,'run') as docker:
        with pytest.raises(RuntimeError):
            agent.delete_appbox_resources(tmp_path,identifier,[],'purge',supplied)
        docker.assert_not_called()


def test_appbox_delete_symlink_and_sibling_resolution_rejected(tmp_path,monkeypatch):
    directory=tmp_path/'ab40ah'; directory.mkdir()
    original=Path.lstat
    from types import SimpleNamespace
    monkeypatch.setattr(Path,'lstat',lambda self: SimpleNamespace(st_mode=stat.S_IFLNK) if self==directory else original(self))
    with pytest.raises(RuntimeError,match='symlink'):
        agent.deletion_target(tmp_path,'ab40ah')
    monkeypatch.setattr(Path,'lstat',original)
    resolved=Path.resolve
    monkeypatch.setattr(Path,'resolve',lambda self,*a,**kw: tmp_path/'other' if self==directory else resolved(self,*a,**kw))
    with pytest.raises(RuntimeError,match='non sûr'):
        agent.deletion_target(tmp_path,'ab40ah')


def test_appbox_delete_refuses_container_owned_by_other_client(tmp_path,monkeypatch):
    docker=DeletionDocker(['plex-appb-40ah'])
    def wrong_owner(args,timeout=15):
        if args[1]=='inspect' and args[-1]=='plex-appb-40ah':
            return 0,json.dumps([{'Config':{'Labels':{'com.docker.compose.project':'someone-else'}}}]),''
        return docker(args,timeout)
    monkeypatch.setattr(agent,'run',wrong_owner)
    with pytest.raises(RuntimeError,match='hors AppBox'):
        agent.delete_appbox_resources(tmp_path,'ab40ah',['plex-appb-40ah'],'purge')
    assert not any(c[1] in {'rm','compose'} for c in docker.calls)


@pytest.mark.parametrize('owned',[True,False])
def test_appbox_delete_unlabelled_legacy_requires_matching_mount(tmp_path,monkeypatch,owned):
    docker=DeletionDocker(['plex-appb-40ah'])
    def legacy(args,timeout=15):
        if args[1]=='inspect' and args[-1] in docker.names:
            source=tmp_path/('ab40ah' if owned else 'other')/'plex-config'
            return 0,json.dumps([{'Config':{'Labels':{}},'Mounts':[{'Source':str(source)}]}]),''
        return docker(args,timeout)
    monkeypatch.setattr(agent,'run',legacy)
    if owned:
        assert agent.delete_appbox_resources(tmp_path,'ab40ah',[],'purge')['state']=='deleted'
    else:
        with pytest.raises(RuntimeError,match='hors AppBox'):
            agent.delete_appbox_resources(tmp_path,'ab40ah',[],'purge')


def test_appbox_delete_refuses_nested_bind_mounts(tmp_path):
    target=tmp_path/'ab40ah'
    safe=f'1 0 0:1 / {tmp_path.as_posix()} rw - ext4 device rw'
    agent.reject_appbox_mounts(target,safe)
    for point in (target,target/'shared-media'):
        with pytest.raises(RuntimeError,match='Montage actif'):
            agent.reject_appbox_mounts(target,f'1 0 0:1 / {point.as_posix()} rw - none device rw')


def test_appbox_delete_recreated_container_prevents_false_success(tmp_path,monkeypatch):
    directory=tmp_path/'ab40ah'; directory.mkdir()
    docker=DeletionDocker(); calls=0
    def recreated(args,timeout=15):
        nonlocal calls
        if args[1]=='ps':
            calls+=1
            if calls==3: docker.names.add('plex-appb-40ah')
        return docker(args,timeout)
    monkeypatch.setattr(agent,'run',recreated)
    with pytest.raises(RuntimeError,match='recréés'):
        agent.delete_appbox_resources(tmp_path,'ab40ah',[],'purge')


class AgentDeploymentTests(unittest.TestCase):
    def test_runtime_preferences_only_change_preferences_and_accept_old_reference(self):
        from agent.reference_contract import apply_plex_runtime_preferences, PLEX_ROOT
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.install(root, reference_archive(root).read_bytes())
            config = root / 'appbox/plex-config'
            prefs = config / PLEX_ROOT / 'Preferences.xml'
            prefs.write_text('<Preferences FriendlyName="SOURCE" ManualPortMappingPort="32434" customConnections="http://source" MachineIdentifier="source-id" PlexOnlineToken="secret" Language="fr"/>')
            before = {p.relative_to(config): p.read_bytes() for p in config.rglob('*') if p.is_file() and p != prefs}
            for identifier, port in [('newbox01', 32448), ('ab-other', 32500)]:
                apply_plex_runtime_preferences(config, identifier, port)
                attrs = ET.parse(prefs).getroot().attrib
                self.assertEqual(attrs, {'Language':'fr', 'FriendlyName':identifier.upper(), 'ManualPortMappingMode':'1', 'ManualPortMappingPort':str(port)})
            self.assertEqual(before, {p.relative_to(config): p.read_bytes() for p in config.rglob('*') if p.is_file() and p != prefs})
            for port in (0, 65536, True, '32448'):
                with self.assertRaises(RuntimeError):
                    apply_plex_runtime_preferences(config, 'newbox01', port)

    def test_blank_plex_preferences_and_manifest(self):
        from agent.reference_contract import apply_plex_runtime_preferences, PLEX_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = main.compose_for('newbox01', 'plex', 32448, None)
            manifest = main.build_deployment_manifest({'client_id':'newbox01', 'type':'plex', 'plex_port':32448}, compose, '')
            self.assertEqual(manifest['plex_runtime']['FriendlyName'], 'NEWBOX01')
            apply_plex_runtime_preferences(root, 'newbox01', 32448)
            self.assertIn('ManualPortMappingPort="32448"', (root / PLEX_ROOT / 'Preferences.xml').read_text())

    def test_blank_deploy_and_recreate_preserve_claimed_identity(self):
        from agent.reference_contract import PLEX_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            compose = main.compose_for('newbox01', 'plex', 32448, None)
            manifest = main.build_deployment_manifest({'client_id':'newbox01','type':'plex','plex_port':32448}, compose, '')
            payload = {'client_id':'newbox01','action':'deploy','compose':compose,'env':'','manifest':manifest,'containers':['plex-appb-newbox01']}
            config = {'appbox_base_dir':tmp}
            with patch.object(agent, 'run', return_value=(0,'','')), patch.object(agent, '_wait_for_container_state'), patch.object(agent, '_wait_plex_ready'):
                agent.execute_command(config, {'command_type':'appbox_action','payload':payload})
                prefs = Path(tmp) / 'newbox01/plex-config' / PLEX_ROOT / 'Preferences.xml'
                self.assertIn('FriendlyName="NEWBOX01"', prefs.read_text())
                prefs.write_text('<Preferences MachineIdentifier="new-identity" PlexOnlineToken="claimed-token"/>')
                before = prefs.read_bytes()
                payload['action'] = 'recreate'
                agent.execute_command(config, {'command_type':'appbox_action','payload':payload})
                self.assertEqual(prefs.read_bytes(), before)

    def test_naming_consistent_for_ab_prefix_separator(self):
        for identifier in ('ab34ah','ab-34ah','34ah'):
            compose=main.compose_for(identifier,'plex',32499,None,acceleration_mode='disabled')
            name='plex-appb-'+main.plex_short_id(identifier)
            self.assertIn('container_name: '+name,compose)
            self.assertNotIn('--',name)
            with patch.object(main,'list_appboxes',return_value=[{'client_id':identifier}]):
                self.assertEqual(main.appbox_id_for_resource(name),identifier)
        for identifier in ('-bad','ab--bad','abc-'):
            with self.assertRaises(ValueError):
                main.compose_for(identifier,'plex',32499,None)

    def test_unsafe_archive_rejected_before_any_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); archive=root/'bad.tar.gz'; target=root/'staging'; target.mkdir()
            for name,kind in [('../escape',tarfile.REGTYPE),('/absolute',tarfile.REGTYPE),('C:/escape',tarfile.REGTYPE),('..\\escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE),('hardlink',tarfile.LNKTYPE),('device',tarfile.CHRTYPE)]:
                with self.subTest(name=name):
                    with tarfile.open(archive,'w:gz') as tar:
                        good=tarfile.TarInfo('good'); good.size=2; tar.addfile(good,io.BytesIO(b'ok'))
                        bad=tarfile.TarInfo(name); bad.type=kind; bad.linkname='../escape'; tar.addfile(bad)
                    with self.assertRaises(RuntimeError):
                        agent.safe_extract_tar(archive,target)
                    self.assertEqual(list(target.iterdir()),[])

    def install(self, root, data, checksum=None, interrupted=False):
        class Response(io.BytesIO):
            def read(self,size=-1):
                if size < 0:
                    raise AssertionError('unbounded read')
                if interrupted:
                    raise OSError('download interrupted')
                return super().read(size)
        descriptor={'version_id':'v1','download_path':'/api/agent/v1/test/reference-deployments/v1/archive','sha256':checksum or hashlib.sha256(data).hexdigest(),'target_directory':'plex-config'}
        config={'control_plane_url':'http://test.invalid','token':'test-only','reference_cache_dir':str(root/'cache')}
        with patch.object(agent.urllib.request,'urlopen',return_value=Response(data)):
            return agent.install_reference_archive(config,descriptor,root/'appbox')

    def test_reference_restore_validates_and_uses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); data=reference_archive(root).read_bytes()
            result=self.install(root,data)
            self.assertEqual(result['status'],'ready')
            self.assertEqual(result['checksum'],hashlib.sha256(data).hexdigest())
            self.assertTrue((root/'appbox/plex-config'/agent.PLEX_REFERENCE_ROOT/'Metadata/poster').is_file())
            with self.assertRaisesRegex(RuntimeError,'existante'):
                self.install(root,data)
            self.assertEqual(list((root/'cache').glob('*.partial')),[])

    def test_restore_reuses_verified_cache_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); data=reference_archive(root).read_bytes()
            self.install(root,data)
            descriptor={'version_id':'v1','download_path':'/api/agent/v1/test/archive','sha256':hashlib.sha256(data).hexdigest(),'target_directory':'plex-config'}
            with patch.object(agent.urllib.request,'urlopen',side_effect=AssertionError('network not expected')):
                result=agent.install_reference_archive({'reference_cache_dir':str(root/'cache')},descriptor,root/'second-appbox')
            self.assertEqual(result['status'],'ready')

    def test_cached_reference_reports_checksum_validation_extraction_and_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); archive=reference_archive(root)
            # Realistic multi-block cached reference: progress must start before
            # checksum completion and preserve the provisioning stage order.
            (root/'fixture-config'/agent.PLEX_REFERENCE_ROOT/'Metadata/large.bin').write_bytes(os.urandom(3*1024*1024+17))
            with tarfile.open(archive,'w:gz') as tar:
                tar.add(root/'fixture-config'/'Library',arcname='Library')
            data=archive.read_bytes()
            self.install(root,data)
            descriptor={'version_id':'v1','download_path':'/api/agent/v1/test/archive',
                        'sha256':hashlib.sha256(data).hexdigest(),
                        'size_bytes':len(data),'target_directory':'plex-config'}
            progress=[]
            with patch.object(agent.urllib.request,'urlopen',side_effect=AssertionError('cache must be reused')):
                result=agent.install_reference_archive(
                    {'reference_cache_dir':str(root/'cache')},descriptor,root/'progress-appbox',
                    progress_callback=lambda **payload: progress.append(payload),
                )
            stages={item['stage'] for item in progress}
            self.assertEqual(result['status'],'ready')
            self.assertTrue({'cache_reference','checksum_reference','archive_validation',
                             'extraction','sqlite_validation','runtime_customization'} <= stages)
            for stage in stages:
                values=[item['percent'] for item in progress if item['stage']==stage]
                self.assertTrue(all(0 <= value <= 100 for value in values),stage)
            first={stage:next(index for index,item in enumerate(progress) if item['stage']==stage)
                   for stage in ('cache_reference','checksum_reference','archive_validation','extraction')}
            self.assertLess(first['cache_reference'],first['checksum_reference'])
            self.assertLess(first['checksum_reference'],first['archive_validation'])
            self.assertLess(first['archive_validation'],first['extraction'])

    def test_cancellation_interrupts_cached_checksum_and_extraction(self):
        for cancelled_stage in ('checksum_reference','extraction'):
            with self.subTest(stage=cancelled_stage), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); data=reference_archive(root).read_bytes()
                checksum=hashlib.sha256(data).hexdigest()
                cache=root/'cache'; cache.mkdir(); (cache/f'{checksum}.tar.gz').write_bytes(data)
                descriptor={'version_id':'v1','download_path':'/api/agent/v1/test/archive',
                            'sha256':checksum,'size_bytes':len(data),'target_directory':'plex-config'}
                def cancel(**payload):
                    if payload['stage']==cancelled_stage:
                        raise agent.CommandCancelled(f'cancelled during {cancelled_stage}')
                with self.assertRaises(agent.CommandCancelled):
                    agent.install_reference_archive(
                        {'reference_cache_dir':str(cache)},descriptor,root/'cancelled-appbox',
                        progress_callback=cancel,
                    )
                self.assertFalse((root/'cancelled-appbox'/'plex-config').exists())
                self.assertEqual(list((root/'cancelled-appbox').glob('.plex-config.staging-*')),[])

    def test_large_checksum_reports_incremental_worker_activity(self):
        from agent.reference_contract import sha256_file
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'large-reference.tar.gz'
            target.write_bytes(b'a'*(3*1024*1024+17))
            reports=[]
            checksum=sha256_file(target,progress_callback=lambda *args: reports.append(args))
        self.assertEqual(checksum,hashlib.sha256(b'a'*(3*1024*1024+17)).hexdigest())
        self.assertGreaterEqual(len(reports),4)
        self.assertEqual(reports[-1][0],'checksum_reference')
        self.assertEqual(reports[-1][1],reports[-1][2])
        self.assertEqual(reports[-1][2],3*1024*1024+17)

    def test_deploy_never_operates_an_unrelated_existing_plex(self):
        with tempfile.TemporaryDirectory() as tmp:
            commands=[]
            def docker(command,timeout=15,progress_callback=None):
                commands.append(list(command))
                if progress_callback:
                    progress_callback(1)
                return 0,'',''
            compose=main.compose_for('jdmry','plex',32448,None)
            manifest=main.build_deployment_manifest({'client_id':'jdmry','type':'plex','plex_port':32448},compose,'')
            payload={'client_id':'jdmry','action':'deploy','compose':compose,'env':'',
                     'manifest':manifest,'containers':['plex-appb-jdmry']}
            with patch.object(agent,'run',side_effect=docker), \
                 patch.object(agent,'_wait_for_container_state'), \
                 patch.object(agent,'_wait_plex_ready',return_value={'claimed':False}):
                agent.execute_command({'appbox_base_dir':tmp},{'command_type':'appbox_action','payload':payload},
                                      progress_callback=lambda **kwargs: None)
            flattened=[' '.join(command) for command in commands]
            self.assertTrue(any('-p jdmry' in command for command in flattened))
            self.assertFalse(any('plex-appb-34ah' in command for command in flattened))
            self.assertFalse(any((' stop ' in f' {command} ' or ' restart ' in f' {command} ')
                                 for command in flattened))

    def test_archive_with_corrupt_sqlite_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); reference_archive(root)
            source=root/'fixture-config'
            (source/agent.PLEX_REFERENCE_ROOT/'Plug-in Support/Databases/com.plexapp.plugins.library.db').write_bytes(b'corrupt')
            archive=root/'corrupt.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                tar.add(source/'Library',arcname='Library')
            import sqlite3
            with self.assertRaises(sqlite3.DatabaseError):
                self.install(root,archive.read_bytes())
            self.assertFalse((root/'appbox/plex-config').exists())
            self.assertEqual(list((root/'cache').glob('*.tar.gz')),[])
            self.assertEqual(list((root/'cache').glob('archive-sqlite-*')),[])

    def test_failed_restore_does_not_write_compose_or_touch_existing_appbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); appbox=root/'abtest'; appbox.mkdir()
            (appbox/'compose.yml').write_text('original',encoding='utf-8')
            payload={'client_id':'abtest','action':'deploy','compose':'new','reference_archive':{'version_id':'v1'}}
            with patch.object(agent,'verify_manifest',return_value={}), patch.object(agent,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'existante'):
                    agent.execute_command({'appbox_base_dir':str(root)},{'command_type':'appbox_action','payload':payload})
            self.assertEqual((appbox/'compose.yml').read_text(),'original')
            run.assert_not_called()

    def test_download_failures_never_publish_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); data=reference_archive(root).read_bytes()
            for payload,checksum,interrupt in [(data,'0'*64,False),(data[:-8],None,False),(data,None,True)]:
                with self.subTest(size=len(payload),interrupt=interrupt):
                    with self.assertRaises((RuntimeError,EOFError,OSError)):
                        self.install(root,payload,checksum,interrupt)
                    self.assertFalse((root/'appbox/plex-config').exists())
                    self.assertEqual(list((root/'cache').glob('*.partial')),[])

    def test_claim_success_cleans_token_and_checks_final_association(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'compose.yml').write_text('services:\n  plex:\n    environment:\n      VERSION: docker\n',encoding='utf-8')
            (root/'.env').write_text('APPBOX_CLIENT_ID=abtest\n',encoding='utf-8')
            before={'claimed':False,'identity_fingerprint':'new-id'}
            after={'claimed':True,'identity_fingerprint':'new-id'}
            with patch.object(agent,'_wait_plex_ready',side_effect=[before,after,after]) as ready, patch.object(agent,'run',return_value=(0,'','')) as run:
                result=agent.claim_plex(root,'abtest',['plex-appb-test'],'claim-abcdefgh')
            self.assertTrue(result['claimed']); self.assertEqual(ready.call_count,3); self.assertEqual(run.call_count,2)
            self.assertNotIn('claim-abcdefgh',(root/'.env').read_text())
            self.assertNotIn('PLEX_CLAIM',(root/'compose.yml').read_text())

    def test_claim_failure_also_recreates_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'compose.yml').write_text('      VERSION: docker\n',encoding='utf-8')
            for error in ('Timeout : claim refusé','Timeout : HTTP Plex indisponible'):
                with self.subTest(error=error), patch.object(agent,'_wait_plex_ready',side_effect=[{'claimed':False},RuntimeError(error)]), patch.object(agent,'run',return_value=(0,'','')) as run:
                    with self.assertRaisesRegex(RuntimeError,'Timeout'):
                        agent.claim_plex(root,'abtest',['plex-appb-test'],'claim-abcdefgh')
                    self.assertEqual(run.call_count,2)
                    self.assertNotIn('claim-abcdefgh',(root/'.env').read_text())

    def test_claim_absent_token_fails_before_docker(self):
        with patch.object(agent,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'absent'):
                agent.claim_plex(Path('unused'),'abtest',[], '')
        run.assert_not_called()

    def test_claim_cleanup_failure_is_explicit_and_does_not_leak_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'compose.yml').write_text('      VERSION: docker\n',encoding='utf-8')
            with patch.object(agent,'_wait_plex_ready',side_effect=[{'claimed':False},RuntimeError('claim-abcdefgh')]), patch.object(agent,'run',side_effect=[(0,'',''),(1,'PLEX_CLAIM=claim-abcdefgh','')]):
                with self.assertRaisesRegex(RuntimeError,'intervention opérateur') as error:
                    agent.claim_plex(root,'abtest',['plex-appb-test'],'claim-abcdefgh')
            self.assertNotIn('claim-abcdefgh',str(error.exception))
            self.assertNotIn('claim-abcdefgh',(root/'.env').read_text())

    def test_command_error_redacts_plex_header_and_structured_secrets(self):
        value=agent._sanitize_diagnostics({'token':'sensitive','details':'GET /?X-Plex-Token=sensitive PLEX_CLAIM=claim-abcdefgh'})
        self.assertNotIn('sensitive',json.dumps(value))
        self.assertNotIn('claim-abcdefgh',json.dumps(value))

    def test_claim_wait_distinguishes_http_identity_and_refusal(self):
        for identity,message in [({'identity_generated':False},'identité'),({'identity_generated':True,'claimed':False},'claim refusé')]:
            with self.subTest(message=message), patch.object(agent,'_wait_for_container_state'), patch.object(agent,'_wait_for_plex_identity',return_value=identity), patch.object(agent.time,'monotonic',side_effect=[0,0,0,2]), patch.object(agent.time,'sleep'):
                with self.assertRaisesRegex(RuntimeError,message):
                    agent._wait_plex_ready('plex-test',claimed=True,timeout=1)
        with patch.object(agent,'_wait_for_container_state'), patch.object(agent,'_wait_for_plex_identity',side_effect=RuntimeError('http unavailable')):
            with self.assertRaisesRegex(RuntimeError,'HTTP Plex'):
                agent._wait_plex_ready('plex-test')

    def test_safe_appbox_dir_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                agent.safe_appbox_dir(Path(tmp), "../../etc")

    def test_atomic_write_replaces_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "compose.yml"
            agent.atomic_write(target, "first\n")
            agent.atomic_write(target, "second\n")
            self.assertEqual(target.read_text(), "second\n")

    def test_manifest_verification(self):
        compose = "services: {}\n"
        env = "APPBOX_CLIENT_ID=ab36ah\n"
        manifest = {
            "schema_version": 1,
            "operation": "deploy",
            "client_id": "ab36ah",
            "node_id": "artemis",
            "application_version": "1.2.0-sprint3-phase1",
            "generated_at": "2026-07-29T17:30:00+00:00",
            "files": {
                "compose.yml": hashlib.sha256(compose.encode()).hexdigest(),
                ".env": hashlib.sha256(env.encode()).hexdigest(),
            },
        }
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest["checksum"] = hashlib.sha256(canonical.encode()).hexdigest()
        verified = agent.verify_manifest({"manifest": manifest}, "ab36ah", compose, env)
        self.assertEqual(verified["checksum"], manifest["checksum"])


if __name__ == "__main__":
    unittest.main()
