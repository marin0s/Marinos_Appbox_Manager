import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

AGENT_PATH = Path(__file__).parents[1] / "agent" / "marinos-appbox-agent.py"
spec = importlib.util.spec_from_file_location("marinos_agent", AGENT_PATH)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class AgentDeploymentTests(unittest.TestCase):
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
