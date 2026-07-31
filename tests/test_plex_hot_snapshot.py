import importlib.util
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path


AGENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "marinos-appbox-agent.py"
spec = importlib.util.spec_from_file_location("marinos_appbox_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(agent)


class PlexHotSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.plex = self.config / "Library/Application Support/Plex Media Server"
        self.db_dir = self.plex / "Plug-in Support/Databases"
        self.db_dir.mkdir(parents=True)
        self.plex.joinpath("Metadata/item").mkdir(parents=True)
        self.plex.joinpath("Metadata/item/poster.jpg").write_bytes(b"poster")
        self.plex.joinpath("Cache").mkdir()
        self.plex.joinpath("Cache/temporary.bin").write_bytes(b"cache")
        self.plex.joinpath("Logs").mkdir()
        self.plex.joinpath("Logs/Plex Media Server.log").write_text("secret log")
        self.plex.joinpath("Preferences.xml").write_text(
            '<Preferences MachineIdentifier="machine" PlexOnlineToken="token" FriendlyName="Reference" />'
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_hot_backup_is_consistent_and_drops_wal_files(self):
        source = self.db_dir / "com.plexapp.plugins.library.db"
        writer = sqlite3.connect(source)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT)")
        writer.executemany("INSERT INTO items(title) VALUES(?)", [(f"item-{i}",) for i in range(100)])
        writer.commit()
        writer.execute("INSERT INTO items(title) VALUES('committed-in-wal')")
        writer.commit()

        overlay, report = agent._prepare_plex_reference_overlay(self.config, self.root / "work")
        snapshot = overlay / "Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 101)
        self.assertFalse(snapshot.with_name(snapshot.name + "-wal").exists())
        self.assertEqual(report["sqlite_strategy"], "python-sqlite3")
        self.assertEqual(report["sqlite_snapshots"][0]["quick_check"], "ok")
        writer.close()

    def test_archive_uses_sanitized_overlay_and_excludes_runtime_data(self):
        source = self.db_dir / "com.plexapp.plugins.library.db"
        with sqlite3.connect(source) as db:
            db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, title TEXT)")
            db.execute("INSERT INTO items(title) VALUES('ok')")

        overlay, _ = agent._prepare_plex_reference_overlay(self.config, self.root / "work")
        archive = self.root / "reference.tar.gz"
        agent._archive_plex_reference(self.config, overlay, archive)

        with tarfile.open(archive, "r:gz") as tar:
            names = set(tar.getnames())
            prefs_name = "Library/Application Support/Plex Media Server/Preferences.xml"
            self.assertIn(prefs_name, names)
            self.assertNotIn("Library/Application Support/Plex Media Server/Metadata/item/poster.jpg", names)
            self.assertIn("Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db", names)
            self.assertFalse(any("/Cache/" in f"/{name}/" for name in names))
            self.assertFalse(any("/Logs/" in f"/{name}/" for name in names))
            prefs = tar.extractfile(prefs_name).read().decode()
            self.assertNotIn("MachineIdentifier", prefs)
            self.assertNotIn("PlexOnlineToken", prefs)
            self.assertIn("FriendlyName", prefs)


if __name__ == "__main__":
    unittest.main()
