from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import main

ROOT = Path(__file__).resolve().parents[1]


class ReferenceDeploymentTests(unittest.TestCase):
    def test_operator_uses_deployment_image_term(self):
        html = (ROOT / "app/templates/appboxes.html").read_text(encoding="utf-8")
        self.assertIn("Image de déploiement", html)
        self.assertIn('name="deployment_image_id"', html)
        self.assertNotIn("Profil de déploiement", html)
        self.assertNotIn('name="reference_version_id" data-reference-select', html)

    def test_catalog_contains_blank_and_published_references(self):
        published = {
            "version_id": "marinos-2026-07-30",
            "image_name": "Marinos Official",
            "media_type": "plex",
            "version": "2026-07-30-v001",
            "state": "published",
            "image_status": "published",
            "source_available": True,
        }
        with patch.object(main, "list_reference_versions", return_value=[published]):
            catalog = main.deployment_images("plex")
        self.assertEqual(catalog[0]["deployment_image_id"], "blank:plex")
        self.assertEqual(catalog[1]["deployment_image_id"], "reference:marinos-2026-07-30")
        self.assertTrue(catalog[1]["available"])

    def test_plex_clone_sanitization_removes_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefs = root / "Library/Application Support/Plex Media Server/Preferences.xml"
            prefs.parent.mkdir(parents=True)
            prefs.write_text('<Preferences MachineIdentifier="source" PlexOnlineToken="secret" FriendlyName="Reference"/>')
            cache = root / "Library/Application Support/Plex Media Server/Cache"
            cache.mkdir(parents=True)
            (cache / "temp").write_text("x")
            main.sanitize_plex_clone(root)
            content = prefs.read_text()
            self.assertNotIn("MachineIdentifier", content)
            self.assertNotIn("PlexOnlineToken", content)
            self.assertNotIn("FriendlyName", content)
            self.assertFalse(cache.exists())


if __name__ == "__main__":
    unittest.main()
