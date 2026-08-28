import hashlib
import importlib.util
import json
import tempfile
import unittest
import io
import tarfile
from pathlib import Path
from unittest.mock import patch
from reference_fixtures import reference_archive
from app import main

AGENT_PATH = Path(__file__).parents[1] / "agent" / "marinos-appbox-agent.py"
spec = importlib.util.spec_from_file_location("marinos_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


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
