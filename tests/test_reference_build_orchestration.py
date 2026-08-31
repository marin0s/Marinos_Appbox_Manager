import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from app import main
from reference_fixtures import reference_archive
from unittest.mock import patch
import asyncio
import threading
from fastapi import HTTPException

class ReferenceBuildOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.old_db=main.DB_FILE; self.old_root=main.REFERENCE_ROOT
        main.DB_FILE=Path(self.tmp.name)/'db.sqlite'; main.REFERENCE_ROOT=Path(self.tmp.name)/'refs'; main.init_database(); stamp=main.now_iso()
        with main.db() as con:
            con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('ouranos','OURANOS','remote','online',?,?)",(stamp,stamp))
        self.build_id=main.create_reference_build_draft(source_node_id='ouranos',display_name='Plex OURANOS')
        self.discovery={'instance':{'plex_version':'1.0'},'libraries':[{'name':'Films'}],'sizes':{'metadata':42},'preflight':{'can_build':True}}
        with main.db() as con:
            con.execute("UPDATE reference_builds SET source_report_json=?,preflight_report_json=?,source_instance='plex-ouranos' WHERE build_id=?",(json.dumps(self.discovery),json.dumps(self.discovery['preflight']),self.build_id))
            con.execute("INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at) VALUES('capture','ouranos','reference_build',?,'claimed',?)",(json.dumps({'build_id':self.build_id}),stamp))
            self.command=con.execute("SELECT * FROM agent_commands WHERE command_id='capture'").fetchone()
    def tearDown(self):
        main.DB_FILE=self.old_db; main.REFERENCE_ROOT=self.old_root; self.tmp.cleanup()
    def test_success_creates_published_catalogue_entry(self):
        archive=main._reference_build_storage(self.build_id)/'reference.tar.gz'; archive.write_bytes(reference_archive(Path(self.tmp.name)).read_bytes())
        sha=main.sha256_file(archive)
        result={'archive_path':str(archive),'sha256':sha,'uncompressed_size_bytes':100,'sanitization':{'source_unchanged':True},'builder_version':'1.6.0-alpha.5-phase1','manifest':{'metadata':{'file_count':1},'source_lifecycle':{'final_container_state':'running'}}}
        main.finalize_reference_build_command(self.command,'success',result,None)
        main.finalize_reference_build_command(self.command,'success',result,None)
        with main.db() as con:
            build=con.execute("SELECT status,image_id,version_id FROM reference_builds WHERE build_id=?",(self.build_id,)).fetchone()
            version=con.execute("SELECT * FROM reference_image_versions WHERE version_id=?",(build['version_id'],)).fetchone()
            self.assertEqual(con.execute('SELECT COUNT(*) FROM reference_image_versions').fetchone()[0], 1)
        self.assertEqual(build['status'],'published'); self.assertEqual(version['state'],'published'); self.assertEqual(version['checksum'],sha)
        catalog=main.deployment_images('plex')
        self.assertTrue(any(item['kind']=='reference' and item['available'] for item in catalog))
        self.assertEqual(version['builder_version'], '1.6.0-alpha.5-phase1')
        self.assertEqual(json.loads(version['manifest_json'])['metadata'], {'file_count':1})

    def test_slow_legacy_hash_runs_outside_db_lock_and_published_checksum_skips_hash(self):
        archive=main._reference_build_storage(self.build_id)/'reference.tar.gz'
        archive.write_bytes(reference_archive(Path(self.tmp.name)).read_bytes())
        sha=main.sha256_file(archive)
        main.finalize_reference_build_command(self.command,'success',{
            'archive_path':str(archive),'sha256':sha,'uncompressed_size_bytes':100,
            'sanitization':{'source_unchanged':True},'builder_version':'test','manifest':{'metadata':{}}},None)
        with main.db() as con:
            version=con.execute('SELECT version_id FROM reference_image_versions').fetchone()[0]
        with patch.object(main,'sha256_file',side_effect=AssertionError('published immutable checksum must be reused')):
            path,actual=main.reference_deployment_archive(version)
        self.assertEqual((path,actual),(archive,sha))

        with main.db() as con:
            con.execute('UPDATE reference_image_versions SET checksum=NULL WHERE version_id=?',(version,))
        entered=threading.Event(); release=threading.Event(); result=[]
        def slow_hash(path):
            entered.set(); self.assertTrue(release.wait(3)); return sha
        def prepare(): result.append(main.reference_deployment_archive(version))
        with patch.object(main,'sha256_file',side_effect=slow_hash),patch.object(main,'authenticate_agent'):
            worker=threading.Thread(target=prepare); worker.start(); self.assertTrue(entered.wait(1))
            completed=threading.Event()
            class Request:
                async def json(self): return {'agent_version':'test','capabilities':{}}
            def heartbeat(): asyncio.run(main.agent_heartbeat('ouranos',Request())); completed.set()
            concurrent=threading.Thread(target=heartbeat); concurrent.start()
            self.assertTrue(completed.wait(1),'heartbeat blocked by reference hashing')
            with main.db_lock,main.db() as con:
                self.assertEqual(con.execute("SELECT status FROM nodes WHERE node_id='ouranos'").fetchone()[0],'online')
            release.set(); worker.join(3); concurrent.join(3)
        self.assertEqual(result,[(archive,sha)])

        # A delete/republication transition that wins while the archive is
        # being inspected must invalidate the prepared result, without making
        # the filesystem work hold the global database lock.
        entered=threading.Event(); release=threading.Event(); errors=[]
        def racing_hash(path):
            entered.set(); self.assertTrue(release.wait(3)); return sha
        def prepare_during_delete():
            try:
                main.reference_deployment_archive(version)
            except Exception as exc:
                errors.append(exc)
        with patch.object(main,'sha256_file',side_effect=racing_hash):
            worker=threading.Thread(target=prepare_during_delete); worker.start(); self.assertTrue(entered.wait(1))
            with main.db_lock,main.db() as con:
                con.execute("UPDATE reference_image_versions SET state='deleting' WHERE version_id=?",(version,))
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],HTTPException)
        self.assertEqual(errors[0].status_code,409)

        # The same post-I/O revalidation also catches republication/identity
        # changes, not only deletion.
        with main.db() as con:
            con.execute("UPDATE reference_image_versions SET state='published',checksum=NULL WHERE version_id=?",(version,))
        entered=threading.Event(); release=threading.Event(); errors=[]
        with patch.object(main,'sha256_file',side_effect=racing_hash):
            worker=threading.Thread(target=prepare_during_delete); worker.start(); self.assertTrue(entered.wait(1))
            with main.db_lock,main.db() as con:
                con.execute("UPDATE reference_image_versions SET checksum=? WHERE version_id=?",('f'*64,version))
            release.set(); worker.join(3)
        self.assertEqual(len(errors),1)
        self.assertIsInstance(errors[0],HTTPException)
        self.assertEqual(errors[0].status_code,409)

        # If deletion starts just after archive resolution, the transactional
        # enqueue check closes the remaining TOCTOU window.
        with main.db() as con:
            con.execute("UPDATE reference_image_versions SET state='deleting',checksum=? WHERE version_id=?",(sha,version))
        with self.assertRaises(HTTPException) as rejected:
            main.queue_agent_command('ouranos','appbox_action',{
                'action':'deploy','reference_archive':{'version_id':version,'sha256':sha}})
        self.assertEqual(rejected.exception.status_code,409)
        with main.db() as con:
            self.assertFalse(con.execute("SELECT 1 FROM agent_commands WHERE command_type='appbox_action'").fetchone())

    def test_new_version_build_keeps_image_identity_and_existing_appbox(self):
        def publish(build_id, command):
            archive=main._reference_build_storage(build_id)/'reference.tar.gz'
            archive.write_bytes(reference_archive(Path(self.tmp.name)).read_bytes())
            sha=main.sha256_file(archive)
            main.finalize_reference_build_command(command,'success',{
                'sha256':sha,'uncompressed_size_bytes':100,'sanitization':{'source_unchanged':True},
                'builder_version':'1.6.0-alpha.5-phase1','manifest':{'metadata':{'file_count':1}}},None)

        publish(self.build_id,self.command)
        stamp=main.now_iso()
        with main.db() as con:
            image=con.execute("SELECT * FROM reference_images").fetchone()
            first_version=image['current_version_id']
            con.execute("INSERT INTO appboxes(client_id,node_id,path,containers_json,status,created_at,updated_at,reference_image_id,reference_version_id) VALUES('ab-keep','ouranos','/unchanged','[]','running',?,?,?,?)",
                        (stamp,stamp,image['image_id'],first_version))
            before=dict(con.execute("SELECT * FROM appboxes WHERE client_id='ab-keep'").fetchone())
        second=main.create_reference_build_draft(source_node_id='ouranos',display_name='A different name',
                                                 target_image_id=image['image_id'])
        with main.db() as con:
            con.execute("UPDATE reference_builds SET source_report_json=?,preflight_report_json=?,source_instance='plex-appb-34ah' WHERE build_id=?",
                        (json.dumps(self.discovery),json.dumps(self.discovery['preflight']),second))
            con.execute("INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at) VALUES('capture-2','ouranos','reference_build',?,'claimed',?)",
                        (json.dumps({'build_id':second}),stamp))
            command=con.execute("SELECT * FROM agent_commands WHERE command_id='capture-2'").fetchone()
        publish(second,command)
        with main.db() as con:
            images=con.execute("SELECT * FROM reference_images").fetchall()
            versions=con.execute("SELECT * FROM reference_image_versions WHERE image_id=? ORDER BY created_at",(image['image_id'],)).fetchall()
            after=dict(con.execute("SELECT * FROM appboxes WHERE client_id='ab-keep'").fetchone())
        self.assertEqual(len(images),1)
        self.assertEqual(len(versions),2)
        self.assertEqual(images[0]['image_id'],image['image_id'])
        self.assertNotEqual(images[0]['current_version_id'],first_version)
        self.assertIn(first_version,{version['version_id'] for version in versions})
        self.assertEqual(after,before)

    def test_invalid_result_never_publishes(self):
        archive=main._reference_build_storage(self.build_id)/'reference.tar.gz'; archive.write_bytes(b'bad')
        main.finalize_reference_build_command(self.command,'success',{'sha256':main.sha256_file(archive)},None)
        with main.db() as con:
            self.assertEqual(con.execute('SELECT status FROM reference_builds WHERE build_id=?',(self.build_id,)).fetchone()[0], 'build_failed')
            self.assertEqual(con.execute('SELECT COUNT(*) FROM reference_image_versions').fetchone()[0],0)

    def upload(self, data, checksum=None, interrupt=False):
        class Request:
            headers={'X-Reference-SHA256': checksum or hashlib.sha256(data).hexdigest()}
            async def stream(self):
                for start in range(0,len(data),31):
                    yield data[start:start+31]
                    if interrupt:
                        raise RuntimeError('disconnected')
        with patch.object(main,'authenticate_agent'):
            return asyncio.run(main.upload_reference_build_archive('ouranos',self.build_id,Request()))

    def test_streamed_upload_is_atomic_and_verified(self):
        data=reference_archive(Path(self.tmp.name)).read_bytes()
        response=self.upload(data)
        self.assertEqual(response.status_code,200)
        target=main._reference_build_storage(self.build_id)/'reference.tar.gz'
        self.assertEqual(target.read_bytes(),data)
        self.assertEqual(list(target.parent.glob('*.uploading')),[])

    def test_failed_upload_preserves_previous_archive(self):
        target=main._reference_build_storage(self.build_id)/'reference.tar.gz'; target.write_bytes(b'previous')
        data=reference_archive(Path(self.tmp.name)).read_bytes()
        for payload, checksum, interrupt in [(data,'0'*64,False),(data[:-8],None,False),(data,None,True),(b'bad',None,False)]:
            with self.subTest(checksum=checksum,interrupt=interrupt,size=len(payload)):
                with self.assertRaises((HTTPException,RuntimeError)):
                    self.upload(payload,checksum,interrupt)
                self.assertEqual(target.read_bytes(),b'previous')
                self.assertEqual(list(target.parent.glob('*.uploading')),[])

    def test_published_archive_cannot_be_overwritten(self):
        with main.db() as con:
            con.execute("UPDATE reference_builds SET status='published' WHERE build_id=?",(self.build_id,))
        with self.assertRaises(HTTPException) as error:
            self.upload(b'anything')
        self.assertEqual(error.exception.status_code,409)

    def test_upload_lock_rejects_concurrent_transfer(self):
        lock=main._reference_build_storage(self.build_id)/'.upload.lock'; lock.touch()
        with self.assertRaises(HTTPException) as error:
            self.upload(b'anything')
        self.assertEqual(error.exception.status_code,409)
        self.assertTrue(lock.exists())

    def test_remote_result_secrets_are_redacted_before_persistence(self):
        class Request:
            async def json(self):
                return {'status':'failed','error':'X-Plex-Token=sensitive claim-abcdefgh','result':{'token':'sensitive'}}
        with patch.object(main,'authenticate_agent'):
            asyncio.run(main.agent_command_result('ouranos','capture',Request()))
        with main.db() as con:
            row=con.execute("SELECT result_json,error_text FROM agent_commands WHERE command_id='capture'").fetchone()
        self.assertNotIn('sensitive',' '.join(row))
        self.assertNotIn('claim-abcdefgh',' '.join(row))

    def test_control_plane_claim_erases_queued_token_on_success_and_failure(self):
        item={'type':'plex','node_id':'ouranos','containers':['plex-appb-test']}
        nodes=[{'node_id':'ouranos','agent_online':True,'status':'online','actionable':True,'rdad_ok':True,'capabilities':{'deployment_executor':True,'plex_runtime_preferences':True}}]
        for ok,result,error in [(True,{'claimed':True},''),(True,{'claimed':False},'claim refused'),(False,{},'timeout')]:
            with self.subTest(ok=ok,result=result), patch.object(main,'get_appbox',return_value=item), patch.object(main,'list_control_nodes',return_value=nodes), patch.object(main,'wait_agent_command',return_value=(ok,result,error)), patch.object(main,'record_event') as event:
                if ok and result.get('claimed'):
                    self.assertEqual(main.claim_appbox('abtest','claim-abcdefgh').status_code,303)
                else:
                    with self.assertRaises(HTTPException):
                        main.claim_appbox('abtest','claim-abcdefgh')
                    self.assertEqual(event.call_args.args[-1],'error')
                with main.db() as con:
                    commands=con.execute("SELECT payload_json FROM agent_commands WHERE command_type='appbox_action'").fetchall()
                self.assertTrue(commands)
                self.assertTrue(all('claim-abcdefgh' not in row[0] for row in commands))

    def test_cache_failure_clears_ready_metadata(self):
        self.test_success_creates_published_catalogue_entry()
        with main.db() as con:
            version=con.execute('SELECT version_id FROM reference_image_versions').fetchone()[0]
        main.set_reference_distribution('ouranos',version,'ready',{'local_path':'cache','checksum':'f'*64,'size_bytes':100})
        main.set_reference_distribution('ouranos',version,'failed')
        with main.db() as con:
            row=con.execute('SELECT * FROM node_reference_cache').fetchone()
        self.assertEqual(row['status'],'failed')
        self.assertIsNone(row['local_path'])
        self.assertIsNone(row['checksum'])

    def test_restore_job_rejects_unverified_agent_success(self):
        item={'node_id':'ouranos','path':self.tmp.name,'type':'plex','reference_version_id':'v1','containers':['plex-appb-test']}
        job={'job_id':'job-test','client_id':'abtest','action':'deploy'}
        nodes=[{'node_id':'ouranos','agent_online':True,'status':'online','actionable':True,'rdad_ok':True,'capabilities':{'deployment_executor':True,'plex_runtime_preferences':True}}]
        from contextlib import ExitStack
        for result in ({'output':'docker success'}, {'health_verified':True,'reference_cache':{'status':'ready','checksum':'wrong'}}):
            with self.subTest(result=result), ExitStack() as stack:
                for name in ('update_job','record_event','deployment_env_for','build_deployment_manifest'):
                    stack.enter_context(patch.object(main,name))
                steps=stack.enter_context(patch.object(main,'update_step'))
                stack.enter_context(patch.object(main,'list_control_nodes',return_value=nodes))
                stack.enter_context(patch.object(main,'reference_deployment_archive',return_value=(Path(__file__),'a'*64)))
                stack.enter_context(patch.object(main,'queue_agent_command',return_value='command'))
                stack.enter_context(patch.object(main,'wait_agent_command',return_value=(True,result,'')))
                distribution=stack.enter_context(patch.object(main,'set_reference_distribution'))
                failed=stack.enter_context(patch.object(main,'fail_workflow'))
                status=stack.enter_context(patch.object(main,'save_appbox_status'))
                main.execute_remote_job(job,item)
                failed.assert_called_once()
                self.assertEqual(status.call_args.args[1],'error')
                self.assertEqual(distribution.call_args.args[2],'failed')
                self.assertFalse(any(call.args[1]=='docker_deploy' and call.args[2]=='running'
                                     for call in steps.call_args_list))

    def test_interrupted_job_does_not_accept_late_agent_result(self):
        job_id=main.create_job(None,'reference_build','test',node_id='ouranos')
        with main.db() as con:
            con.execute("UPDATE jobs SET status='failed' WHERE job_id=?",(job_id,))
            con.execute("UPDATE agent_commands SET status='success',result_json='{}' WHERE command_id='capture'")
        ok,_,error=main.wait_agent_command('capture',timeout=1,job_id=job_id)
        self.assertFalse(ok)
        self.assertIn('interrompu',error)

    def test_synthetic_capture_upload_publish_restore_and_claim(self):
        import io
        from test_agent_deployment import agent
        root=Path(self.tmp.name)
        reference_archive(root)
        source=root/'fixture-config'
        (source/agent.PLEX_REFERENCE_ROOT/'Preferences.xml').write_text('<Preferences MachineIdentifier="source-id" PlexOnlineToken="source-token" Language="fr"/>',encoding='utf-8')
        with patch.object(agent,'_docker_container_state',return_value='exited'):
            captured=agent._capture_plex_reference(source,root/'capture','plex-source-test')
        uploaded=self.upload(captured['archive'].read_bytes())
        stored=json.loads(uploaded.body)
        result={**captured,**stored,'builder_version':agent.PLEX_REFERENCE_BUILDER_VERSION,
                'manifest':{'metadata':captured['archive_report']['metadata']}}
        result.pop('archive')
        main.finalize_reference_build_command(self.command,'success',result,None)
        with main.db() as con:
            version=con.execute('SELECT version_id FROM reference_image_versions').fetchone()[0]
        archive,checksum=main.reference_deployment_archive(version)
        identifier='ab-e2e16'; name='plex-appb-e2e16'
        compose=main.compose_for(identifier,'plex',32499,None,acceleration_mode='disabled',target_node='ouranos')
        manifest=main.build_deployment_manifest({'client_id':identifier,'node_id':'ouranos','type':'plex','plex_port':32499},compose,'')
        descriptor={'version_id':version,'download_path':'/api/agent/v1/ouranos/archive','sha256':checksum,'target_directory':'plex-config'}
        config={'appbox_base_dir':str(root/'node'),'reference_cache_dir':str(root/'cache'),'control_plane_url':'http://test.invalid','token':'synthetic'}
        payload={'action':'deploy','client_id':identifier,'compose':compose,'env':'','manifest':manifest,'reference_archive':descriptor,'containers':[name]}
        with patch.object(agent.urllib.request,'urlopen',return_value=io.BytesIO(archive.read_bytes())), patch.object(agent,'run',return_value=(0,'','')), patch.object(agent,'_wait_for_container_state'), patch.object(agent,'_wait_plex_ready',return_value={'identity_generated':True}):
            deployed=agent.execute_command(config,{'command_type':'appbox_action','payload':payload})
        self.assertTrue(deployed['health_verified'])
        self.assertEqual(deployed['reference_cache']['status'],'ready')
        target=root/'node'/identifier
        prefs=(target/'plex-config'/agent.PLEX_REFERENCE_ROOT/'Preferences.xml').read_text()
        self.assertNotIn('source-id',prefs); self.assertNotIn('source-token',prefs)
        self.assertIn('Language="fr"',prefs)
        self.assertIn('FriendlyName="AB-E2E16"',prefs)
        self.assertIn('ManualPortMappingPort="32499"',prefs)
        self.assertIn('container_name: '+name,(target/'compose.yml').read_text())
        before={'identity_fingerprint':'new-identity','claimed':False}
        after={**before,'claimed':True}
        with patch.object(agent,'run',return_value=(0,'','')), patch.object(agent,'_wait_plex_ready',side_effect=[before,after,after]):
            claimed=agent.claim_plex(target,identifier,[name],'claim-abcdefgh')
        self.assertTrue(claimed['claimed'])
        self.assertNotIn('claim-abcdefgh',(target/'.env').read_text())
if __name__ == '__main__': unittest.main()
