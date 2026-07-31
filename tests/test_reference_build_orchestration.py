import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from app import main

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
        archive=main._reference_build_storage(self.build_id)/'reference.tar.gz'; archive.write_bytes(b'reference')
        sha=hashlib.sha256(b'reference').hexdigest()
        main.finalize_reference_build_command(self.command,'success',{'archive_path':str(archive),'sha256':sha,'uncompressed_size_bytes':100,'sanitization':{'source_unchanged':True}},None)
        with main.db() as con:
            build=con.execute("SELECT status,image_id,version_id FROM reference_builds WHERE build_id=?",(self.build_id,)).fetchone()
            version=con.execute("SELECT state,archive_path,checksum FROM reference_image_versions WHERE version_id=?",(build['version_id'],)).fetchone()
        self.assertEqual(build['status'],'published'); self.assertEqual(version['state'],'published'); self.assertEqual(version['checksum'],sha)
        catalog=main.deployment_images('plex')
        self.assertTrue(any(item['kind']=='reference' and item['available'] for item in catalog))
if __name__ == '__main__': unittest.main()
