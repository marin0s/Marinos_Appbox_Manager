import hashlib
import importlib.util
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


AGENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "marinos-appbox-agent.py"
spec = importlib.util.spec_from_file_location("marinos_appbox_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(agent)


class PlexSourceCaptureTests(unittest.TestCase):
    def test_claim_and_credentials_removed_but_template_preferences_preserved(self):
        self.preferences.write_text('<Preferences MachineIdentifier="source" PLEX_CLAIM="claim-abcdefgh" ClaimToken="secret" CustomAuthToken="token" Language="fr" AcceptedEULA="1"/>',encoding='utf-8')
        archive,_,_=self._build_archive()
        with tarfile.open(archive,'r:gz') as tar:
            prefs=tar.extractfile(agent.PLEX_REFERENCE_ROOT.as_posix()+'/Preferences.xml').read()
        for secret in (b'source',b'claim-',b'secret',b'token'):
            self.assertNotIn(secret,prefs)
        self.assertIn(b'Language="fr"',prefs)
        self.assertIn(b'AcceptedEULA="1"',prefs)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.plex = self.config / agent.PLEX_REFERENCE_ROOT
        self.db_dir = self.plex / "Plug-in Support/Databases"
        self.db_dir.mkdir(parents=True)

        files = {
            "Metadata/item/poster.jpg": b"poster",
            "Media/localhost/index.bin": b"media-index",
            "Plug-ins/example.bundle/Contents/plugin.py": b"plugin",
            "Scanners/Series/scanner.py": b"scanner",
            "Profiles/profile.xml": b"profile",
            "Resources/resource.dat": b"resource",
        }
        for relative, content in files.items():
            target = self.plex / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        for directory in ("Cache", "Logs", "Crash Reports", "Codecs", "Diagnostics", "Sessions", "Transcode"):
            target = self.plex / directory
            target.mkdir(parents=True)
            (target / "excluded.bin").write_bytes(b"excluded")

        for relative in (
            "Metadata/item/process.pid",
            "Metadata/item/work.tmp",
            "Media/localhost/.transcode-session",
        ):
            target = self.plex / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"transient")

        self.preferences = self.plex / "Preferences.xml"
        self.preferences.write_text(
            '<Preferences MachineIdentifier="machine" ProcessedMachineIdentifier="processed" '
            'AnonymousMachineIdentifier="anonymous" PlexOnlineToken="token" '
            'PlexOnlineUsername="user" PlexOnlineMail="mail@example.invalid" '
            'PlexOnlineHome="1" CertificateUUID="certificate" PubSubServer="server" '
            'PubSubServerRegion="region" FriendlyName="Reference" Language="fr" />',
            encoding="utf-8",
        )
        self._create_database("com.plexapp.plugins.library.db", rows=3)
        self._create_database("com.plexapp.plugins.library.blobs.db", rows=2)
        self._create_database("com.plexapp.plugins.library-2026-07-30.db", rows=1)
        self._create_database("library-backup.db", rows=1)
        (self.db_dir / "com.plexapp.plugins.library.db-wal").write_bytes(b"wal")
        (self.db_dir / "com.plexapp.plugins.library.db-shm").write_bytes(b"shm")

    def tearDown(self):
        self.temp.cleanup()

    def _create_database(self, name, rows):
        connection = sqlite3.connect(self.db_dir / name)
        try:
            connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT)")
            connection.executemany("INSERT INTO items(title) VALUES(?)", [(f"item-{index}",) for index in range(rows)])
            connection.commit()
        finally:
            connection.close()

    def _build_archive(self):
        workdir = self.root / "work"
        overlay, sanitization = agent._prepare_plex_reference_overlay(self.config, workdir)
        archive = self.root / "reference.tar.gz"
        report = agent._archive_plex_reference(self.config, overlay, archive)
        return archive, sanitization, report

    def test_canonical_archive_preserves_application_data_and_excludes_runtime_data(self):
        archive, sanitization, report = self._build_archive()
        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
            prefix = agent.PLEX_REFERENCE_ROOT.as_posix()
            self.assertIn(f"{prefix}/Metadata/item/poster.jpg", names)
            self.assertIn(f"{prefix}/Media/localhost/index.bin", names)
            self.assertIn(f"{prefix}/Plug-ins/example.bundle/Contents/plugin.py", names)
            self.assertIn(f"{prefix}/Scanners/Series/scanner.py", names)
            self.assertIn(f"{prefix}/Profiles/profile.xml", names)
            self.assertIn(f"{prefix}/Resources/resource.dat", names)
            canonical = f"{prefix}/Plug-in Support/Databases/com.plexapp.plugins.library.db"
            blobs = f"{prefix}/Plug-in Support/Databases/com.plexapp.plugins.library.blobs.db"
            self.assertIn(canonical, names)
            self.assertIn(blobs, names)
            self.assertFalse(any("2026-07-30" in name or "backup" in name.lower() for name in names))
            for excluded in ("Cache", "Logs", "Crash Reports", "Codecs", "Diagnostics", "Sessions", "Transcode"):
                self.assertFalse(any(name == f"{prefix}/{excluded}" or name.startswith(f"{prefix}/{excluded}/") for name in names))
            self.assertFalse(any(name.endswith((".db-wal", ".db-shm", ".pid", ".tmp")) for name in names))
            self.assertFalse(any(".transcode" in name.lower() for name in names))
            preferences = tar.extractfile(f"{prefix}/Preferences.xml").read().decode("utf-8")

        for attribute in agent.PLEX_REFERENCE_IDENTITY_ATTRIBUTES:
            self.assertNotIn(attribute, preferences)
        self.assertIn('FriendlyName="Reference"', preferences)
        self.assertIn('Language="fr"', preferences)
        self.assertTrue(set(sanitization["identity_attributes_removed"]) <= set(agent.PLEX_REFERENCE_IDENTITY_ATTRIBUTES))
        self.assertEqual(report["metadata"], {"size_bytes": len(b"poster"), "file_count": 1})
        self.assertEqual(report["media"], {"size_bytes": len(b"media-index"), "file_count": 1})
        self.assertEqual(report["databases"]["file_count"], 2)
        self.assertEqual(set(report["databases"]["names"]), {
            "com.plexapp.plugins.library.db", "com.plexapp.plugins.library.blobs.db",
        })
        with tarfile.open(archive, "r:gz") as tar:
            actual_uncompressed_size = sum(member.size for member in tar.getmembers() if member.isfile())
        self.assertEqual(report["uncompressed_size_bytes"], actual_uncompressed_size)
        for validation in sanitization["sqlite_snapshots"]:
            snapshot = self.root / "work/overlay" / agent.PLEX_REFERENCE_ROOT / "Plug-in Support/Databases" / validation["name"]
            self.assertEqual(validation["sha256"], hashlib.sha256(snapshot.read_bytes()).hexdigest())
            self.assertIn(validation["validation"], {"quick_check", "schema-readable-tokenizer-unavailable"})
        self.assertEqual(sanitization["sqlite_engine_selection"]["selected_engine"], "python-sqlite3")
        self.assertEqual(sanitization["sqlite_engine_selection"]["reason"], "source-container-frozen")
        self.assertFalse(sanitization["sqlite_engine_selection"]["container_exec_attempted"])

    def test_initially_running_plex_is_captured_without_stop_or_restart(self):
        workdir = self.root / "capture-running"
        overlay = workdir / "overlay"
        overlay.mkdir(parents=True, exist_ok=True)

        sanitization = {
            "sqlite_snapshots": [],
            "sqlite_engine_selection": {
                "selected_engine": "plex-sqlite-with-python-fallback",
                "reason": "container-engine-requested",
                "container_exec_attempted": True,
            },
        }

        archive_report = {
            "included_paths": [],
            "excluded_paths": [],
            "uncompressed_size_bytes": 7,
            "metadata": {"size_bytes": 0, "file_count": 0},
            "media": {"size_bytes": 0, "file_count": 0},
            "databases": {"size_bytes": 0, "file_count": 0, "names": []},
        }

        def fake_archive(_config_path, _overlay, archive):
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"archive")
            return archive_report

        with patch.object(
            agent,
            "_docker_container_state",
            side_effect=["running", "running"],
        ), patch.object(
            agent,
            "_prepare_plex_reference_overlay",
            return_value=(overlay, sanitization),
        ) as prepare, patch.object(
            agent,
            "_archive_plex_reference",
            side_effect=fake_archive,
        ), patch.object(
            agent,
            "_stop_plex_for_capture",
        ) as stop, patch.object(
            agent,
            "_restart_plex_after_capture",
        ) as restart:
            result = agent._capture_plex_reference(
                self.config,
                workdir,
                "plex-source",
            )

        stop.assert_not_called()
        restart.assert_not_called()

        prepare.assert_called_once_with(
            self.config,
            workdir,
            "plex-source",
        )

        self.assertFalse(result["builder_stopped_container"])
        self.assertFalse(result["restart_attempted"])
        self.assertFalse(result["stop_result"]["attempted"])
        self.assertFalse(result["restart_result"]["attempted"])
        self.assertEqual(result["final_container_state"], "running")

    def test_identity_uses_container_ip_before_container_tools(self):
        response = MagicMock()
        response.read.return_value = b'<MediaContainer claimed="1" version="1.2.3"/>'
        response.__enter__.return_value = response
        with patch.object(agent, "run", return_value=(0, "bridge|172.18.0.9\n", "")) as docker, \
             patch.object(agent.urllib.request, "urlopen", return_value=response) as urlopen:
            identity = agent._wait_for_plex_identity("plex-source", timeout=1)
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://172.18.0.9:32400/identity")
        self.assertEqual(identity["method"], "host-http:http://172.18.0.9:32400/identity")
        self.assertTrue(identity["claimed"])
        self.assertEqual(len(docker.call_args_list), 1)

    def test_archive_inspection_rejects_members_outside_plex_root_explicitly(self):
        archive = self.root / "outside-root.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../../escape.txt")
            info.size = 0
            tar.addfile(info)
        self.assertIsNone(agent._plex_archive_relative("../../escape.txt"))
        self.assertIsNone(agent._plex_archive_relative("unrelated/file.txt"))
        with self.assertRaisesRegex(RuntimeError, "chemins exclus"):
            agent._inspect_plex_reference_archive(archive)

    def test_development_agent_and_phase1_builder_versions_are_reported(self):
        metrics = {"docker_ok": True, "compose_version": "v2"}
        config = {"node_id": "test-node", "agent_id": "agent-test"}
        with patch.object(agent, "collect_metrics", return_value=metrics), \
             patch.object(agent, "api", side_effect=lambda _config, _method, _path, payload=None: payload):
            payload = agent.heartbeat(config)
        self.assertEqual(agent.VERSION, "1.6.0-alpha.5-dev")
        self.assertEqual(payload["agent_version"], "1.6.0-alpha.5-dev")
        self.assertEqual(payload["capabilities"]["reference_builder_versions"]["plex"], "1.6.0-alpha.5-phase1")
        self.assertEqual(payload["capabilities"]["reference_archive_schemas"]["plex"], 1)

    def test_plex_sqlite_snapshot_path_has_uuid_dependency(self):
        source = self.db_dir / "com.plexapp.plugins.library.db"
        destination = self.root / "snapshot" / source.name
        fallback_result = {"name": source.name, "engine": "python-sqlite3"}
        with patch.object(agent, "run", return_value=(1, "", "Plex SQLite unavailable")), \
             patch.object(agent, "_python_sqlite_hot_backup", return_value=fallback_result) as fallback:
            result = agent._plex_sqlite_hot_backup(
                "plex-source",
                Path("/config") / source.relative_to(self.config),
                source,
                destination,
            )
        self.assertEqual(result, fallback_result)
        fallback.assert_called_once_with(source, destination)

    def test_initially_stopped_plex_remains_stopped(self):
        with patch.object(agent, "_docker_container_state", side_effect=["exited", "exited"]), \
             patch.object(agent, "_stop_plex_for_capture") as stop, \
             patch.object(agent, "_restart_plex_after_capture") as restart:
            result = agent._capture_plex_reference(self.config, self.root / "capture-stopped", "plex-source")
        stop.assert_not_called()
        restart.assert_not_called()
        self.assertFalse(result["builder_stopped_container"])
        self.assertFalse(result["restart_attempted"])
        self.assertEqual(result["final_container_state"], "exited")


if __name__ == "__main__":
    unittest.main()
