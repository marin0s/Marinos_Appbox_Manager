import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app import main
from test_agent_deployment import agent
from test_node_liveness import Request


@pytest.fixture
def storage_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_FILE", tmp_path / "storage.db")
    monkeypatch.setattr(main, "HOSTNAME", "cronos")
    monkeypatch.setattr(main, "BASE_DIR", tmp_path / "appboxes")
    main.init_database()
    stamp = main.now_iso()
    with main.db() as con:
        for node_id in ("orion", "artemis"):
            con.execute(
                "INSERT INTO nodes(node_id,name,mode,status,rdad_ok,created_at,updated_at) VALUES(?,?,'remote','online',1,?,?)",
                (node_id, node_id.upper(), stamp, stamp),
            )
            con.execute(
                "INSERT INTO node_agents(node_id,status,last_heartbeat,capabilities_json,updated_at) VALUES(?,'online',?,?,?)",
                (node_id, stamp, json.dumps({"deployment_executor": True, "storage_observations": True}), stamp),
            )
            main.store_agent_metrics(con, node_id, {
                "docker_ok": True, "memory_total_bytes": 16_000_000_000,
                "memory_available_bytes": 12_000_000_000, "disk_total_bytes": 1_000_000_000_000,
                "disk_free_bytes": 900_000_000_000, "cpu_count": 8, "rdad_present": True,
            }, "test", stamp)
            con.execute("INSERT OR IGNORE INTO node_tag_assignments(node_id,tag_id,assigned_at) VALUES(?,'appbox-node',?)", (node_id, stamp))
    return tmp_path


def mounts():
    return main.mounts_for_group("rdad-standard", "plex")


def observe(node_id, values, *, received_at=None):
    stamp = received_at or main.now_iso()
    with main.db() as con:
        return main.persist_storage_observations(con, node_id, values, stamp, stamp)


def all_available(node_id):
    observe(node_id, [
        {"path": mount["host_path"], "exists": True, "mounted": True}
        for mount in mounts()
    ])


def test_same_logical_mount_is_available_on_orion_and_absent_on_cronos(storage_db):
    target = mounts()[0]
    observe("orion", [{"path": target["host_path"], "exists": True, "mounted": True}])
    states = {item["node_id"]: item for item in main.storage_topology([target])[target["mount_id"]]}
    assert states["orion"]["state"] == "available"
    assert states["cronos"]["state"] == "unknown"


def test_existing_directory_that_is_not_mountpoint_is_absent(storage_db):
    target = next(mount for mount in mounts() if mount["requires_mountpoint"])
    observe("orion", [{"path": target["host_path"], "exists": True, "mounted": False}])
    result = main.resolve_mounts_for_node([target], "orion")
    assert result["states"][0]["state"] == "absent"
    assert "non monté" in result["states"][0]["reason"]


def test_existing_path_without_mountpoint_requirement_is_available_for_rdad(storage_db):
    target = next(mount for mount in mounts() if mount["mount_id"] == "rdad-media")
    assert target["requires_mountpoint"] == 0
    observe("orion", [{"path": target["host_path"], "exists": True, "mounted": False}])
    assert main.resolve_mounts_for_node([target], "orion")["states"][0]["state"] == "available"


def test_mounted_nfs_is_available_when_mountpoint_is_required(storage_db):
    target = next(mount for mount in mounts() if mount["mount_id"] == "nas-athena")
    observe("orion", [{
        "path": target["host_path"], "exists": True, "mounted": True,
        "filesystem": "nfs4", "source": "nas:/athena",
    }])
    state = main.resolve_mounts_for_node([target], "orion")["states"][0]
    assert state["state"] == "available" and state["filesystem"] == "nfs4"


def test_missing_and_stale_telemetry_are_not_available(storage_db, monkeypatch):
    target = mounts()[0]
    assert main.resolve_mounts_for_node([target], "orion")["states"][0]["state"] == "unknown"
    old = (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()
    observe("orion", [{"path": target["host_path"], "exists": True, "mounted": True}], received_at=old)
    monkeypatch.setattr(main, "STORAGE_OBSERVATION_SECONDS", 180)
    assert main.resolve_mounts_for_node([target], "orion")["states"][0]["state"] == "stale"


def test_required_absent_refuses_manual_deploy_and_available_allows_it(storage_db):
    required = [mounts()[0]]
    observe("orion", [{"path": required[0]["host_path"], "exists": False, "mounted": False}])
    with pytest.raises(main.HTTPException) as error:
        main.evaluate_placement("manual", "orion", mounts=required)
    assert error.value.status_code == 409 and "obligatoire absent" in error.value.detail
    observe("orion", [{"path": required[0]["host_path"], "exists": True, "mounted": True}])
    assert main.evaluate_placement("manual", "orion", mounts=required)["selected"]["node_id"] == "orion"


@pytest.mark.parametrize("action", ["deploy", "recreate"])
def test_runtime_storage_loss_refuses_existing_compose_before_agent_command(storage_db, action):
    all_available("orion")
    main.create_appbox(
        client_id="latebind", media_type="plex", profile_id="", deployment_image_id="",
        mount_group_id="rdad-standard", snapshot_id="", reference_version_id="",
        port_mode="manual", media_port_requested="32491", acceleration_mode="disabled",
        placement_mode="manual", target_node_id="orion", bare_metal_override=False,
        with_tautulli=False, deploy_now=False,
    )
    required = mounts()[0]
    observe("orion", [{"path": required["host_path"], "exists": False, "mounted": False}])
    job_id = main.create_job("latebind", action, "storage validation", node_id="orion")
    with main.db() as con:
        job = dict(con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
    main.execute_remote_job(job, main.get_appbox("latebind"))
    with main.db() as con:
        status = con.execute("SELECT status,detail FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        commands = con.execute("SELECT COUNT(*) FROM agent_commands WHERE node_id='orion'").fetchone()[0]
    assert status["status"] == "failed" and "obligatoire absent" in status["detail"]
    assert commands == 0


def test_optional_absent_is_omitted_without_fake_bind(storage_db):
    optional = [mount for mount in mounts() if not mount["required"]][0]
    observe("orion", [{"path": optional["host_path"], "exists": False, "mounted": False}])
    resolved = main.resolve_mounts_for_node([optional], "orion")
    compose = main.compose_for("safeopt", "plex", 32490, None, resolved["mounts"], acceleration_mode="disabled", target_node="orion")
    assert not resolved["blockers"] and resolved["omitted"]
    assert optional["host_path"] not in compose


@pytest.mark.parametrize("state", ["unknown", "stale"])
def test_optional_unknown_or_stale_blocks_ambiguous_provisioning(storage_db, monkeypatch, state):
    optional = next(mount for mount in mounts() if not mount["required"])
    if state == "stale":
        old = (datetime.now(timezone.utc) - timedelta(seconds=181)).isoformat()
        observe("orion", [{"path": optional["host_path"], "exists": True, "mounted": True}], received_at=old)
        monkeypatch.setattr(main, "STORAGE_OBSERVATION_SECONDS", 180)
    resolved = main.resolve_mounts_for_node([optional], "orion")
    assert resolved["states"][0]["state"] == state
    assert resolved["blockers"] and not resolved["omitted"]
    assert "impossible de décider une omission sûre" in resolved["blockers"][0]


def test_automatic_placement_excludes_node_with_missing_required_storage(storage_db):
    required = [mounts()[0]]
    observe("orion", [{"path": required[0]["host_path"], "exists": True, "mounted": True}])
    observe("artemis", [{"path": required[0]["host_path"], "exists": False, "mounted": False}])
    result = main.evaluate_placement("automatic", None, mounts=required)
    assert result["selected"]["node_id"] == "orion"
    assert any(item["node_id"] == "artemis" and "obligatoire absent" in item["reason"] for item in result["rejected"])


def test_legacy_mount_node_id_does_not_scope_logical_definition(storage_db):
    target = mounts()[0]
    with main.db() as con:
        con.execute("UPDATE storage_mounts SET node_id='artemis' WHERE mount_id=?", (target["mount_id"],))
    target = mounts()[0]
    observe("orion", [{"path": target["host_path"], "exists": True, "mounted": True}])
    assert target["legacy_node_id"] == "artemis"
    assert main.resolve_mounts_for_node([target], "orion")["states"][0]["state"] == "available"


def test_old_agent_and_old_inventory_payload_remain_compatible_and_unknown(storage_db):
    with main.db() as con:
        con.execute("UPDATE node_agents SET capabilities_json='{}' WHERE node_id='orion'")
    with patch.object(main, "authenticate_agent"):
        response = asyncio.run(main.agent_inventory("orion", Request({"containers": [], "collected_at": main.now_iso()})))
    assert json.loads(response.body)["status"] == "ok"
    assert main.resolve_mounts_for_node([mounts()[0]], "orion")["states"][0]["state"] == "unknown"


def test_inventory_persists_only_configured_safe_paths(storage_db):
    target = mounts()[0]
    payload = {"containers": [], "storage_paths": [
        {"path": target["host_path"], "exists": True, "mounted": True, "filesystem": "nfs4"},
        {"path": "/mnt/../etc", "exists": True, "mounted": True},
        {"path": "/not-configured", "exists": True, "mounted": True},
    ]}
    with patch.object(main, "authenticate_agent"):
        asyncio.run(main.agent_inventory("orion", Request(payload)))
    with main.db() as con:
        rows = con.execute("SELECT host_path,filesystem FROM node_storage_paths WHERE node_id='orion'").fetchall()
    assert [tuple(row) for row in rows] == [(target["host_path"], "nfs4")]


def test_storage_page_shows_per_node_states_and_reason(storage_db):
    target = mounts()[0]
    observe("orion", [{"path": target["host_path"], "exists": False, "mounted": False}])
    rendered = main.templates.env.get_template("storage.html").render(
        mounts=main.list_storage_mounts(), groups=[], hostname="cronos", active_page="storage"
    )
    assert "ORION" in rendered and "ABSENT" in rendered
    assert "Chemin absent sur ce node" in rendered


def test_existing_rdad_standard_is_usable_after_fresh_observation(storage_db):
    assert {item["mount_id"]: item["requires_mountpoint"] for item in mounts()} == {
        "rdad-media": 0, "nas-athena": 1, "nas-nemesis": 1,
    }
    all_available("orion")
    resolved = main.resolve_mounts_for_node(mounts(), "orion")
    assert not resolved["blockers"]
    assert {item["mount_id"] for item in resolved["mounts"]} == {"rdad-media", "nas-athena", "nas-nemesis"}


def test_storage_schema_migration_is_additive_and_idempotent(tmp_path, monkeypatch):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as con:
        con.execute("""CREATE TABLE storage_mounts (
            mount_id TEXT PRIMARY KEY,name TEXT NOT NULL,node_id TEXT NOT NULL,
            host_path TEXT NOT NULL,container_path TEXT NOT NULL,read_only INTEGER NOT NULL DEFAULT 1,
            propagation TEXT NOT NULL DEFAULT 'rprivate',required INTEGER NOT NULL DEFAULT 0,
            media_types_json TEXT NOT NULL DEFAULT '[\"plex\"]',enabled INTEGER NOT NULL DEFAULT 1,
            description TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
        con.executemany("INSERT INTO storage_mounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ('rdad-media','RDAD Media','artemis','/mnt/decypharr-poc','/data',1,'rshared',1,'["plex"]',1,'','now','now'),
            ('nas-athena','NAS ATHENA','artemis','/mnt/ATHENA','/ATHENA',1,'rprivate',0,'["plex"]',1,'','now','now'),
            ('nas-nemesis','NAS NEMESIS','artemis','/mnt/NEMESIS','/NEMESIS',1,'rprivate',0,'["plex"]',1,'','now','now'),
        ])
    monkeypatch.setattr(main, "DB_FILE", database)
    monkeypatch.setattr(main, "HOSTNAME", "cronos")
    main.init_database()
    main.init_database()
    stamp = main.now_iso()
    with main.db() as con:
        con.execute("INSERT INTO nodes(node_id,name,mode,status,created_at,updated_at) VALUES('orion','ORION','remote','online',?,?)", (stamp,stamp))
        con.execute("INSERT INTO node_agents(node_id,status,last_heartbeat,capabilities_json,updated_at) VALUES('orion','online',?,?,?)",
                    (stamp,json.dumps({'deployment_executor':True,'storage_observations':True}),stamp))
        main.persist_storage_observations(con,'orion',[{
            'path':'/mnt/decypharr-poc','exists':True,'mounted':False,
        }],stamp,stamp)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(storage_mounts)")}
        rows = con.execute("SELECT mount_id,node_id,requires_mountpoint FROM storage_mounts ORDER BY mount_id").fetchall()
        storage_table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_storage_paths'").fetchone()
    assert "requires_mountpoint" in columns and storage_table
    assert [tuple(row) for row in rows] == [
        ('nas-athena','artemis',0), ('nas-nemesis','artemis',0), ('rdad-media','artemis',0),
    ]
    legacy_rdad = next(item for item in main.mounts_for_group('rdad-standard','plex') if item['mount_id']=='rdad-media')
    resolved = main.resolve_mounts_for_node([legacy_rdad], 'orion')
    assert resolved['states'][0]['state'] == 'available' and not resolved['blockers']


def test_migration_does_not_modify_existing_compose(tmp_path, monkeypatch):
    database = tmp_path / "existing.db"
    monkeypatch.setattr(main, "DB_FILE", database)
    monkeypatch.setattr(main, "HOSTNAME", "cronos")
    main.init_database()
    compose = tmp_path / "compose.yml"
    original = b"services:\n  plex:\n    volumes:\n      - /mnt/decypharr-poc:/data\n"
    compose.write_bytes(original)
    with main.db() as con:
        con.execute("UPDATE storage_mounts SET requires_mountpoint=0 WHERE mount_id='rdad-media'")
    main.init_database()
    assert compose.read_bytes() == original


def test_agent_collects_storage_outside_heartbeat(storage_db):
    usage = type("Usage", (), {"total": 10, "free": 4, "used": 6})()
    with patch.object(agent.Path, "exists", return_value=True), \
            patch.object(agent.os.path, "ismount", return_value=False), \
            patch.object(agent.shutil, "disk_usage", return_value=usage):
        observed = agent.collect_storage_paths(["/mnt/plain", "/mnt/../etc"])
    assert len(observed) == 1 and observed[0]["exists"] is True and observed[0]["mounted"] is False
    with patch.object(agent, "collect_storage_paths", side_effect=AssertionError("heavy storage stat")), patch.object(agent, "api", return_value={}) as api:
        agent.heartbeat({"node_id": "orion"}, {"docker_ok": True})
    assert api.call_count == 1


def test_blocked_storage_collection_does_not_block_heartbeat_loop(storage_db):
    loops = agent.AgentLoops({"node_id": "orion", "heartbeat_interval": 0.01, "inventory_interval": 0.01})
    loops.heartbeat_interval = 0.01
    entered = threading.Event()
    release = threading.Event()
    beats = threading.Event()
    calls = {"beats": 0}

    def blocked_inventory(*_args):
        entered.set()
        release.wait(3)
        return {}

    def heartbeat(*_args):
        calls["beats"] += 1
        if entered.is_set() and calls["beats"] >= 3:
            beats.set()
        return {"storage_paths": ["/mnt/decypharr-poc"]}

    with patch.object(agent, "collect_metrics", return_value={"docker_ok": True}), \
            patch.object(agent, "api", return_value={}), \
            patch.object(agent, "send_inventory", side_effect=blocked_inventory), \
            patch.object(agent, "heartbeat", side_effect=heartbeat):
        heartbeat_thread = threading.Thread(target=loops.heartbeat_loop)
        telemetry_thread = threading.Thread(target=loops.telemetry_loop)
        heartbeat_thread.start(); telemetry_thread.start()
        try:
            assert beats.wait(2)
        finally:
            loops.stop.set(); loops.inventory_request.set(); release.set()
            heartbeat_thread.join(2); telemetry_thread.join(2)
    assert calls["beats"] >= 3
