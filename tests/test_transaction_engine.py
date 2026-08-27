import os
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("APPBOX_MODE", "mock")
from app import main


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE appboxes (client_id TEXT PRIMARY KEY);
CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY, client_id TEXT, node_id TEXT, action TEXT,
  status TEXT, progress INTEGER, detail TEXT, created_at TEXT, updated_at TEXT,
  started_at TEXT, finished_at TEXT,
  FOREIGN KEY(client_id) REFERENCES appboxes(client_id)
);
CREATE TABLE events (event_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE notifications_queue (notification_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE port_reservations (
  reservation_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id),
  status TEXT, released_at TEXT
);
CREATE TABLE appbox_mounts (client_id TEXT REFERENCES appboxes(client_id) ON DELETE CASCADE);
CREATE TABLE snapshot_deployments (client_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE reconciliation_events (client_id TEXT REFERENCES appboxes(client_id) ON DELETE CASCADE);
CREATE TABLE containers (container_id TEXT PRIMARY KEY, appbox_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE networks (network_id TEXT PRIMARY KEY, appbox_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE volumes (volume_id TEXT PRIMARY KEY, appbox_id TEXT REFERENCES appboxes(client_id));
CREATE TABLE placement_decisions (
  decision_id INTEGER PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id), reason TEXT
);
CREATE TABLE control_plane_deployments (
  deployment_id TEXT PRIMARY KEY, client_id TEXT REFERENCES appboxes(client_id), detail TEXT
);
"""


class TransactionEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = main.DB_FILE
        main.DB_FILE = Path(self.tmp.name) / "test.db"
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO appboxes(client_id) VALUES ('test141')")
            con.execute("INSERT INTO jobs VALUES ('job1','test141','artemis','delete','running',50,'',datetime('now'),datetime('now'),NULL,NULL)")
            con.execute("INSERT INTO events(client_id) VALUES ('test141')")
            con.execute("INSERT INTO notifications_queue(client_id) VALUES ('test141')")
            con.execute("INSERT INTO port_reservations(client_id,status) VALUES ('test141','reserved')")
            con.execute("INSERT INTO appbox_mounts VALUES ('test141')")
            con.execute("INSERT INTO snapshot_deployments VALUES ('test141')")
            con.execute("INSERT INTO reconciliation_events VALUES ('test141')")
            con.execute("INSERT INTO containers VALUES ('c1','test141')")
            con.execute("INSERT INTO networks VALUES ('n1','test141')")
            con.execute("INSERT INTO volumes VALUES ('v1','test141')")
            con.execute("INSERT INTO placement_decisions VALUES (1,'test141','historique conservé')")
            con.execute("INSERT INTO control_plane_deployments VALUES ('dep1','test141','historique conservé')")

    def tearDown(self):
        main.DB_FILE = self.old_db
        self.tmp.cleanup()

    def test_test141_regression_detaches_history_and_deletes_inventory(self):
        self.assertTrue(main.finalize_appbox_deletion('test141', 'job1'))
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM appboxes").fetchone()[0], 0)
            self.assertIsNone(con.execute("SELECT client_id FROM jobs WHERE job_id='job1'").fetchone()[0])
            self.assertIsNone(con.execute("SELECT client_id FROM placement_decisions WHERE decision_id=1").fetchone()[0])
            self.assertIsNone(con.execute("SELECT client_id FROM control_plane_deployments WHERE deployment_id='dep1'").fetchone()[0])
            self.assertEqual(con.execute("SELECT status FROM port_reservations").fetchone()[0], 'released')
            for table in ('appbox_mounts','snapshot_deployments','reconciliation_events','containers','networks','volumes'):
                self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_finalization_is_idempotent(self):
        self.assertTrue(main.finalize_appbox_deletion('test141', 'job1'))
        self.assertFalse(main.finalize_appbox_deletion('test141', 'job1'))

    def test_foreign_key_failure_rolls_back_everything(self):
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("CREATE TABLE unknown_child(client_id TEXT NOT NULL REFERENCES appboxes(client_id))")
            con.execute("INSERT INTO unknown_child VALUES ('test141')")
        with self.assertRaises(sqlite3.IntegrityError):
            main.finalize_appbox_deletion('test141', 'job1')
        with closing(sqlite3.connect(main.DB_FILE)) as con, con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM appboxes WHERE client_id='test141'").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT client_id FROM jobs WHERE job_id='job1'").fetchone()[0], 'test141')
            self.assertEqual(con.execute("SELECT client_id FROM placement_decisions WHERE decision_id=1").fetchone()[0], 'test141')
            self.assertEqual(con.execute("SELECT client_id FROM control_plane_deployments WHERE deployment_id='dep1'").fetchone()[0], 'test141')
            self.assertEqual(con.execute("SELECT status FROM port_reservations").fetchone()[0], 'reserved')


if __name__ == '__main__':
    unittest.main()
