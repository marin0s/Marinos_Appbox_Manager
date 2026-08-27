from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ReferenceImagesUxTests(unittest.TestCase):
    def test_resource_menu_is_simplified(self):
        html = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        self.assertIn(">Images de référence</a>", html)
        self.assertIn(">Déploiements</a>", html)
        self.assertIn(">Agents</a>", html)
        self.assertIn(">Stockage</a>", html)
        self.assertNotIn(">Distribution</a>", html)
        self.assertNotIn("Stockage & Références", html)

    def test_reference_page_uses_operator_workflow_without_score(self):
        html = (ROOT / "app/templates/reference_images.html").read_text(encoding="utf-8")
        self.assertIn("Bibliothèque", html)
        self.assertIn("Depuis un serveur", html)
        self.assertIn("Depuis un fichier", html)
        self.assertIn("Instance compatible", html)
        self.assertIn("Création impossible", html)
        self.assertNotIn("compatibility_score", html)
        self.assertNotIn("Builder", html)
        self.assertNotIn("Manifest", html)
        self.assertNotIn("tar.zst", html)

    def test_storage_page_only_exposes_mounts_and_groups(self):
        html = (ROOT / "app/templates/storage.html").read_text(encoding="utf-8")
        self.assertIn("Volume Mounts", html)
        self.assertIn("Groupes de montages", html)
        self.assertNotIn("Images de référence", html)
        self.assertNotIn("Sources techniques", html)
        self.assertNotIn("Profils disponibles", html)
        self.assertNotIn("snapshot-create", html)

if __name__ == "__main__":
    unittest.main()
