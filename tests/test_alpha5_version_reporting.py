import importlib.util
import unittest
import zipfile
import tempfile
import subprocess
import sys
from pathlib import Path
from scripts.package_agent import FILES, package_bytes

from app import main


AGENT_PATH = Path(__file__).resolve().parents[1] / "agent" / "marinos-appbox-agent.py"
AGENT_ARCHIVE_PATH = AGENT_PATH.with_name("appbox-agent-latest.zip")
spec = importlib.util.spec_from_file_location("alpha5_embedded_agent", AGENT_PATH)
embedded_agent = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(embedded_agent)


class Alpha5VersionReportingTests(unittest.TestCase):
    def test_control_plane_health_and_footer_report_development_version(self):
        self.assertEqual(main.PRODUCT_VERSION, "1.6.0-alpha.5")
        self.assertEqual(main.VERSION, "1.6.0-alpha.5-dev")
        self.assertEqual(main.health()["version"], main.VERSION)
        self.assertTrue(main.health()["reference_build_intrusive_actions"])
        footer = main.templates.env.get_template("base.html").render(active_page="")
        self.assertIn("v1.6.0-alpha.5-dev", footer)

    def test_embedded_agent_and_builder_versions_match_alpha5_strategy(self):
        self.assertEqual(embedded_agent.PRODUCT_VERSION, main.PRODUCT_VERSION)
        self.assertEqual(embedded_agent.VERSION, main.VERSION)
        self.assertEqual(embedded_agent.PLEX_REFERENCE_BUILDER_VERSION, "1.6.0-alpha.5-phase1")

    def test_downloadable_agent_archive_contains_the_current_agent(self):
        with zipfile.ZipFile(AGENT_ARCHIVE_PATH) as archive:
            archived_bytes = archive.read("marinos-appbox-agent.py")
        self.assertEqual(AGENT_ARCHIVE_PATH.read_bytes(), package_bytes(AGENT_PATH.parent))
        self.assertNotIn(b"\r", archived_bytes)
        self.assertEqual(archived_bytes, AGENT_PATH.read_bytes().replace(b"\r\n", b"\n"))
        archived_source = archived_bytes.decode("utf-8")
        namespace = {"__name__": "downloadable_agent", "__file__": "marinos-appbox-agent.py"}
        exec(compile(archived_source, "marinos-appbox-agent.py", "exec"), namespace)
        self.assertEqual(namespace["VERSION"], "1.6.0-alpha.5-dev")
        self.assertEqual(namespace["PLEX_REFERENCE_BUILDER_VERSION"], "1.6.0-alpha.5-phase1")
        self.assertEqual(namespace["uuid"].__name__, "uuid")

    def test_package_is_reproducible_across_checkout_line_endings(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for name in FILES:
                data = (AGENT_PATH.parent / name).read_bytes().replace(b"\r\n", b"\n")
                (source / name).write_bytes(data.replace(b"\n", b"\r\n"))
            self.assertEqual(package_bytes(source), package_bytes(AGENT_PATH.parent))
            with zipfile.ZipFile(AGENT_ARCHIVE_PATH) as archive:
                self.assertEqual(sorted(archive.namelist()), sorted(FILES))

    def test_packaged_agent_imports_without_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(AGENT_ARCHIVE_PATH) as archive:
                archive.extractall(directory)
            result=subprocess.run([sys.executable,'-I','-c',
                'import sys,runpy; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[1]+"/marinos-appbox-agent.py",run_name="package-test")',directory],
                capture_output=True,text=True,cwd=directory)
            self.assertEqual(result.returncode,0,result.stderr)


if __name__ == "__main__":
    unittest.main()
