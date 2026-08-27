import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


AGENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "marinos-appbox-agent.py"
spec = importlib.util.spec_from_file_location("sqlite_diagnostic_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(agent)


class PlexSQLiteDiagnosticsTests(unittest.TestCase):
    def test_corrupt_database_fails_with_cleanup_and_no_source_mutation(self):
        self.source.write_bytes(b'not sqlite')
        destination=self.root/'output/library.db'
        with self.assertRaises(agent.PlexSQLiteCaptureError) as error:
            agent._python_sqlite_hot_backup(self.source,destination)
        self.assertEqual(error.exception.diagnostics['failure']['stage'],'backup')
        self.assertEqual(self.source.read_bytes(),b'not sqlite')
        self.assertFalse(destination.exists())
        self.assertEqual(list(destination.parent.glob('sqlite-source-*')),[])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source" / "com.plexapp.plugins.library.db"
        self.source.parent.mkdir()
        connection = sqlite3.connect(self.source)
        try:
            connection.execute("CREATE TABLE metadata(id INTEGER PRIMARY KEY, title TEXT)")
            connection.execute("INSERT INTO metadata(title) VALUES('Plex')")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_source_database_is_identified(self):
        missing = self.root / "missing" / "library.db"
        destination = self.root / "output" / "library.db"
        with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
            agent._python_sqlite_hot_backup(missing, destination)
        failure = raised.exception.diagnostics["failure"]
        self.assertEqual(failure["stage"], "source_preflight")
        self.assertEqual(failure["role"], "source")
        self.assertFalse(failure["failed_path"]["exists"])
        self.assertEqual(Path(failure["failed_path"]["path"]), missing.absolute())
        self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def test_missing_destination_parent_is_created_and_snapshot_succeeds(self):
        destination = self.root / "missing" / "nested" / "snapshot.db"
        result = agent._python_sqlite_hot_backup(self.source, destination)
        self.assertTrue(destination.is_file())
        self.assertTrue(result["diagnostics"]["destination_parent_created"])
        self.assertEqual(result["quick_check"], "ok")

    def test_inaccessible_destination_parent_is_actionable(self):
        blocked_parent = self.root / "not-a-directory"
        blocked_parent.write_text("blocked", encoding="utf-8")
        destination = blocked_parent / "snapshot.db"
        with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
            agent._python_sqlite_hot_backup(self.source, destination)
        failure = raised.exception.diagnostics["failure"]
        self.assertEqual(failure["stage"], "destination_parent_prepare")
        self.assertEqual(failure["role"], "destination_parent")
        self.assertEqual(failure["failed_path"]["file_type"], "file")
        self.assertIsNotNone(raised.exception.__cause__)

    def test_permission_failure_preserves_path_ownership_and_context(self):
        destination = self.root / "output" / "snapshot.db"
        permission_error = PermissionError(13, "Permission denied")
        with patch.object(agent.shutil, "copy2", side_effect=permission_error):
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent._python_sqlite_hot_backup(self.source, destination)
        diagnostics = raised.exception.diagnostics
        failure = diagnostics["failure"]
        self.assertEqual(failure["stage"], "source_stage_copy")
        self.assertEqual(failure["exception_type"], "PermissionError")
        self.assertIn("permissions", diagnostics["source"])
        self.assertIn("uid", diagnostics["source"])
        self.assertIn("gid", diagnostics["source_parent"])
        self.assertIn("writable", diagnostics["destination_parent"])
        self.assertIn("free_bytes", diagnostics["destination_free_disk"])
        self.assertIs(raised.exception.__cause__, permission_error)

    def test_destination_open_failure_names_destination_and_engine(self):
        destination = self.root / "output" / "snapshot.db"
        real_connect = sqlite3.connect

        def fail_destination(path, *args, **kwargs):
            if Path(path) == destination:
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(path, *args, **kwargs)

        with patch.object(agent.sqlite3, "connect", side_effect=fail_destination):
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent._python_sqlite_hot_backup(self.source, destination)
        failure = raised.exception.diagnostics["failure"]
        self.assertEqual(failure["stage"], "destination_open")
        self.assertEqual(failure["role"], "destination")
        self.assertEqual(raised.exception.diagnostics["engine"], "python-sqlite3")
        self.assertIn(str(destination.absolute()), str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)

    def test_plex_sqlite_subprocess_failure_is_diagnostic_and_redacted(self):
        destination = self.root / "output" / "snapshot.db"
        secret_stderr = (
            "unable to open database file Authorization: Bearer agent-secret "
            "PlexOnlineToken=plex-secret PLEX_CLAIM=claim-ABCDEFGH password=hunter2 "
            "Authorization: Basic basic-secret {\"access_token\":\"access-secret\"} "
            "refresh_token=refresh-secret https://user:pass-secret@example.invalid/db?token=query-secret"
        )
        results = [
            (0, "", ""),
            (1, "", secret_stderr),
            (0, "", ""),
        ]
        with patch.object(agent, "run", side_effect=results) as docker:
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent._plex_sqlite_hot_backup(
                    "plex-ouranos", Path("/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases") / self.source.name,
                    self.source, destination,
                )
        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["failure"]["stage"], "plex_backup_subprocess")
        self.assertEqual(diagnostics["selected_sqlite_executable"], agent.PLEX_SQLITE_EXECUTABLE)
        self.assertEqual(diagnostics["subprocesses"][1]["return_code"], 1)
        self.assertEqual(diagnostics["subprocesses"][1]["cwd"], os.path.abspath(os.getcwd()))
        self.assertEqual(diagnostics["subprocesses"][1]["arguments"][:4], [
            "docker", "exec", "plex-ouranos", agent.PLEX_SQLITE_EXECUTABLE,
        ])
        serialized = json.dumps(diagnostics)
        for secret in (
            "agent-secret", "plex-secret", "claim-ABCDEFGH", "hunter2", "basic-secret",
            "access-secret", "refresh-secret", "pass-secret", "query-secret",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)
        cleanup = docker.call_args_list[-1].args[0]
        self.assertEqual(cleanup[:5], ["docker", "exec", "plex-ouranos", "rm", "-f"])

    def test_successful_python_snapshot_is_coherent_and_diagnostic(self):
        destination = self.root / "output" / "snapshot.db"
        result = agent._python_sqlite_hot_backup(self.source, destination)
        self.assertEqual(result["engine"], "python-sqlite3")
        self.assertEqual(result["quick_check"], "ok")
        self.assertEqual(result["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest())
        self.assertEqual(result["diagnostics"]["source_staging_strategy"], "private-writable-copy")
        self.assertTrue(result["diagnostics"]["source_staging_lifecycle"]["cleanup_completed"])
        self.assertFalse(destination.with_name(destination.name + "-wal").exists())
        self.assertFalse(destination.with_name(destination.name + "-shm").exists())

    def test_wal_sidecars_are_consolidated_from_private_staging_only(self):
        source = self.root / "wal-source" / "library.db"
        source.parent.mkdir()
        connection = sqlite3.connect(source)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT)")
            connection.execute("INSERT INTO items(title) VALUES('from-wal')")
            connection.commit()
            sidecars = [
                source.with_name(source.name + suffix)
                for suffix in ("-wal", "-shm")
                if source.with_name(source.name + suffix).is_file()
            ]
            self.assertTrue(any(path.name.endswith("-wal") for path in sidecars))
            for path in [source, *sidecars]:
                path.chmod(0o444)
            try:
                destination = self.root / "output" / "wal-snapshot.db"
                result = agent._python_sqlite_hot_backup(source, destination)
            finally:
                for path in [source, *sidecars]:
                    if path.exists():
                        path.chmod(0o600)
        finally:
            connection.close()
        snapshot = sqlite3.connect(destination)
        try:
            self.assertEqual(snapshot.execute("SELECT title FROM items").fetchone()[0], "from-wal")
            self.assertEqual(snapshot.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            snapshot.close()
        self.assertIn("-wal", result["diagnostics"]["staged_source_sidecars"])
        self.assertFalse(destination.with_name(destination.name + "-wal").exists())
        self.assertFalse(destination.with_name(destination.name + "-shm").exists())

    def test_successful_plex_sqlite_snapshot_records_all_subprocesses(self):
        destination = self.root / "output" / "snapshot.db"

        def docker_run(arguments, timeout=15):
            if arguments[:2] == ["docker", "cp"]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.source, destination)
            if arguments[-1:] == ["PRAGMA quick_check;"]:
                return 0, "ok", ""
            return 0, "", ""

        with patch.object(agent, "run", side_effect=docker_run):
            result = agent._plex_sqlite_hot_backup(
                "plex-ouranos", Path("/config/database.db"), self.source, destination,
            )
        self.assertEqual(result["engine"], "plex-sqlite")
        self.assertEqual(result["quick_check"], "ok")
        self.assertEqual(len(result["diagnostics"]["subprocesses"]), 5)
        self.assertEqual(result["diagnostics"]["subprocesses"][2]["stdout"], "ok")

    def test_plex_sqlite_cleanup_failure_is_explicit(self):
        destination = self.root / "output" / "snapshot.db"

        def docker_run(arguments, timeout=15):
            if arguments[:2] == ["docker", "cp"]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.source, destination)
            if arguments[-1:] == ["PRAGMA quick_check;"]:
                return 0, "ok", ""
            if arguments[3:5] == ["rm", "-f"]:
                return 1, "", "permission denied"
            return 0, "", ""

        with patch.object(agent, "run", side_effect=docker_run):
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent._plex_sqlite_hot_backup(
                    "plex-ouranos", Path("/config/database.db"), self.source, destination,
                )
        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["failure"]["stage"], "plex_snapshot_cleanup")
        self.assertFalse(diagnostics["container_snapshot_cleanup"]["success"])
        self.assertEqual(diagnostics["container_snapshot_cleanup"]["return_code"], 1)

    def test_sqlite_failure_still_restarts_initially_running_plex(self):
        diagnostic_error = agent.PlexSQLiteCaptureError("snapshot failed", {"engine": "python-sqlite3"})
        with patch.object(agent, "_docker_container_state", side_effect=["running", "running"]), \
             patch.object(agent, "_stop_plex_for_capture", return_value={"success": True, "confirmed_state": "exited"}), \
             patch.object(agent, "_prepare_plex_reference_overlay", side_effect=diagnostic_error), \
             patch.object(agent, "_restart_plex_after_capture", return_value=(
                 {"success": True, "confirmed_state": "running"}, {"reachable": True},
             )) as restart:
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent._capture_plex_reference(self.root, self.root / "work-running", "plex-ouranos")
        restart.assert_called_once_with("plex-ouranos")
        lifecycle = raised.exception.diagnostics["container_lifecycle"]
        self.assertEqual(lifecycle["initial_container_state"], "running")
        self.assertTrue(lifecycle["restart_attempted"])
        self.assertTrue(lifecycle["restart_result"]["success"])
        self.assertEqual(lifecycle["final_container_state"], "running")

    def test_sqlite_failure_leaves_initially_stopped_plex_stopped(self):
        diagnostic_error = agent.PlexSQLiteCaptureError("snapshot failed", {"engine": "python-sqlite3"})
        with patch.object(agent, "_docker_container_state", side_effect=["exited", "exited"]), \
             patch.object(agent, "_prepare_plex_reference_overlay", side_effect=diagnostic_error), \
             patch.object(agent, "_stop_plex_for_capture") as stop, \
             patch.object(agent, "_restart_plex_after_capture") as restart:
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent._capture_plex_reference(self.root, self.root / "work-stopped", "plex-ouranos")
        stop.assert_not_called()
        restart.assert_not_called()
        lifecycle = raised.exception.diagnostics["container_lifecycle"]
        self.assertEqual(lifecycle["initial_container_state"], "exited")
        self.assertFalse(lifecycle["restart_attempted"])
        self.assertEqual(lifecycle["final_container_state"], "exited")

    def test_build_failure_reports_cleaned_temporary_directory_and_version(self):
        discovery = {
            "preflight": {"can_build": True},
            "configuration": {"config_path": str(self.root)},
            "instance": {"container_name": "plex-ouranos"},
        }
        error = agent.PlexSQLiteCaptureError("snapshot failed", {"engine": "python-sqlite3"})
        config = {
            "reference_build_temp_dir": str(self.root / "agent-work"),
            "control_plane_url": "http://control-plane.invalid",
            "token": "agent-secret",
        }
        payload = {"upload_path": "/api/agent/v1/ouranos/reference-builds/test/archive"}
        with patch.object(agent, "discover_plex_instance", return_value=discovery), \
             patch.object(agent, "_capture_plex_reference", side_effect=error):
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent.build_and_upload_plex_reference(config, payload)
        lifecycle = raised.exception.diagnostics["temporary_directory_lifecycle"]
        self.assertTrue(lifecycle["created"])
        self.assertTrue(lifecycle["cleanup_attempted"])
        self.assertTrue(lifecycle["cleanup_completed"])
        self.assertFalse(Path(lifecycle["path"]).exists())
        self.assertNotIn(config["token"], json.dumps(raised.exception.diagnostics))
        self.assertEqual(agent.PLEX_REFERENCE_BUILDER_VERSION, "1.6.0-alpha.5-phase1")

    def test_build_temporary_directory_creation_failure_is_actionable(self):
        discovery = {
            "preflight": {"can_build": True},
            "configuration": {"config_path": str(self.root)},
            "instance": {"container_name": "plex-ouranos"},
        }
        config = {
            "reference_build_temp_dir": str(self.root / "agent-work"),
            "control_plane_url": "http://control-plane.invalid",
            "token": "agent-secret",
        }
        payload = {"upload_path": "/api/agent/v1/ouranos/reference-builds/test/archive"}
        permission_error = PermissionError(13, "Permission denied")
        with patch.object(agent, "discover_plex_instance", return_value=discovery), \
             patch.object(agent.tempfile, "mkdtemp", side_effect=permission_error):
            with self.assertRaises(agent.PlexSQLiteCaptureError) as raised:
                agent.build_and_upload_plex_reference(config, payload)
        self.assertEqual(raised.exception.diagnostics["stage"], "temporary_directory_create")
        self.assertIn("free_disk", raised.exception.diagnostics)
        self.assertIs(raised.exception.__cause__, permission_error)

    def test_command_failure_returns_redacted_structured_diagnostics(self):
        error = agent.PlexSQLiteCaptureError(
            "Authorization: Bearer agent-secret unable to open database file",
            {"stderr": "PlexOnlineToken=plex-secret", "stage": "source_open"},
        )
        submitted = []

        def fake_api(_config, method, _path, payload=None):
            if method == "GET":
                return {"command": {
                    "command_id": "diagnostic-command",
                    "command_type": "reference_build",
                    "payload": {},
                }}
            submitted.append(payload)
            return {"status": "ok"}

        with patch.object(agent, "api", side_effect=fake_api), \
             patch.object(agent, "execute_command", side_effect=error):
            agent.command_cycle({"node_id": "ouranos"})
        payload = submitted[0]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["result"]["diagnostics"]["stage"], "source_open")
        serialized = json.dumps(payload)
        self.assertNotIn("agent-secret", serialized)
        self.assertNotIn("plex-secret", serialized)
        self.assertIn("[REDACTED]", serialized)


if __name__ == "__main__":
    unittest.main()
