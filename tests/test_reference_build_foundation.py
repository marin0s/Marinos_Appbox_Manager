import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import main


class ReferenceBuildFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = main.DB_FILE
        main.DB_FILE = Path(self.tmp.name) / "reference-foundation.db"
        main.init_database()
        stamp = main.now_iso()
        with main.db() as con:
            con.execute(
                """INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at)
                   VALUES('ouranos','OURANOS','remote','online',?,?)""",
                (stamp, stamp),
            )

    def tearDown(self):
        main.DB_FILE = self.old_db
        self.tmp.cleanup()

    def test_schema_and_plex_builder_are_installed(self):
        with sqlite3.connect(main.DB_FILE) as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("reference_builds", tables)
            self.assertIn("reference_build_logs", tables)
            self.assertIn("reference_builder_registry", tables)
            builder = con.execute(
                "SELECT application,intrusive_actions_enabled FROM reference_builder_registry WHERE builder_key='plex'"
            ).fetchone()
            self.assertEqual(builder, ("plex", 0))
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_create_draft_is_non_intrusive_and_logged(self):
        build_id = main.create_reference_build_draft(
            source_node_id="ouranos",
            display_name="Plex Production OURANOS",
            description="Référence officielle",
        )
        with sqlite3.connect(main.DB_FILE) as con:
            build = con.execute(
                "SELECT status,current_stage,progress,job_id FROM reference_builds WHERE build_id=?",
                (build_id,),
            ).fetchone()
            self.assertEqual(build, ("draft", "foundation", 0, None))
            log = con.execute(
                "SELECT level,message FROM reference_build_logs WHERE build_id=?",
                (build_id,),
            ).fetchone()
            self.assertEqual(log[0], "info")
            self.assertIn("Aucune action intrusive", log[1])

    def test_reference_job_workflow_has_full_pipeline(self):
        keys = [key for key, _ in main.workflow_definition("reference_build")]
        self.assertEqual(
            keys,
            ["discover", "preflight", "capture", "sanitize", "package", "transfer", "validate_reference", "publish"],
        )


if __name__ == "__main__":
    unittest.main()
