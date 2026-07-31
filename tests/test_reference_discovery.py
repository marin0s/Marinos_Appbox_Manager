import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import main


class ReferenceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = main.DB_FILE
        main.DB_FILE = Path(self.tmp.name) / "reference-discovery.db"
        main.init_database()
        stamp = main.now_iso()
        with main.db() as con:
            con.execute(
                """INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at)
                   VALUES('ouranos','OURANOS','remote','online',?,?)""",
                (stamp, stamp),
            )
        self.build_id = main.create_reference_build_draft(
            source_node_id="ouranos",
            display_name="Plex Production OURANOS",
        )

    def tearDown(self):
        main.DB_FILE = self.old_db
        self.tmp.cleanup()

    def _seed_command_and_job(self):
        stamp = main.now_iso()
        job_id = "job-discovery-1"
        command_id = "command-discovery-1"
        with main.db() as con:
            con.execute(
                """INSERT INTO jobs(job_id,client_id,node_id,action,title,status,progress,detail,created_at,updated_at,started_at,options_json)
                   VALUES(?,NULL,'ouranos','reference_discovery','Discovery','running',10,'',?,?,?,'{}')""",
                (job_id, stamp, stamp, stamp),
            )
            for key, title in main.workflow_definition("reference_discovery"):
                con.execute(
                    """INSERT INTO job_steps(job_id,step_key,title,status,progress,detail,executor,resources_json)
                       VALUES(?,?,?,'pending',0,'','control-plane','{}')""",
                    (job_id, key, title),
                )
            payload = {"build_id": self.build_id, "job_id": job_id, "application": "plex"}
            con.execute(
                """INSERT INTO agent_commands(command_id,node_id,command_type,payload_json,status,created_at)
                   VALUES(?,'ouranos','reference_discovery',?,'claimed',?)""",
                (command_id, json.dumps(payload), stamp),
            )
            con.execute("UPDATE reference_builds SET job_id=?,status='analyzing' WHERE build_id=?", (job_id, self.build_id))
            command = con.execute("SELECT * FROM agent_commands WHERE command_id=?", (command_id,)).fetchone()
        return job_id, command

    def test_discovery_workflow_is_read_only_pipeline(self):
        self.assertEqual(
            [key for key, _ in main.workflow_definition("reference_discovery")],
            ["connecting", "discovering", "collecting_metadata", "compatibility_check", "completed"],
        )

    def test_successful_result_updates_build_and_existing_job(self):
        job_id, command = self._seed_command_and_job()
        result = {
            "read_only": True,
            "libraries": [{"name": "Films"}, {"name": "Séries"}],
            "totals": {"movies": 120, "shows": 15, "seasons": 60, "episodes": 900},
            "preflight": {"compatibility_score": 5, "can_build": True, "warnings": [], "blockers": []},
        }
        main.finalize_reference_discovery_command(command, "success", result, None)
        with sqlite3.connect(main.DB_FILE) as con:
            build = con.execute(
                "SELECT status,current_stage,progress,source_report_json FROM reference_builds WHERE build_id=?",
                (self.build_id,),
            ).fetchone()
            job = con.execute("SELECT status,progress FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            steps = con.execute("SELECT status FROM job_steps WHERE job_id=?", (job_id,)).fetchall()
            capture = con.execute("SELECT command_type,status FROM agent_commands WHERE command_type='reference_build' AND node_id='ouranos'").fetchone()
        self.assertEqual(build[:3], ("building", "capture", 55))
        self.assertTrue(json.loads(build[3])["read_only"])
        self.assertEqual(job, ("success", 100))
        self.assertTrue(all(row[0] == "success" for row in steps))
        self.assertEqual(capture, ("reference_build", "queued"))

    def test_failed_result_is_persisted(self):
        job_id, command = self._seed_command_and_job()
        main.finalize_reference_discovery_command(command, "failed", {}, "Instance Plex introuvable")
        with sqlite3.connect(main.DB_FILE) as con:
            build = con.execute("SELECT status,error_text FROM reference_builds WHERE build_id=?", (self.build_id,)).fetchone()
            job = con.execute("SELECT status,detail FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        self.assertEqual(build, ("discovery_failed", "Instance Plex introuvable"))
        self.assertEqual(job[0], "error")
        self.assertIn("introuvable", job[1])


if __name__ == "__main__":
    unittest.main()
