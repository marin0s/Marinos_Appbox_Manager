from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ReferenceImagesUxTests(unittest.TestCase):
    def test_resource_menu_is_simplified(self):
        html = (ROOT / "app/templates/base.html").read_text(encoding="utf-8")
        self.assertIn(">Références</a>", html)
        self.assertIn(">Déploiements</a>", html)
        self.assertIn(">Agents</a>", html)
        self.assertIn(">Stockage</a>", html)
        self.assertNotIn(">Distribution</a>", html)
        self.assertNotIn("Stockage & Références", html)

    def test_reference_library_is_simple_and_non_destructive(self):
        html = (ROOT / "app/templates/reference_images.html").read_text(encoding="utf-8")
        self.assertIn("Bibliothèque", html)
        self.assertIn("Créer une référence", html)
        self.assertIn(">Gérer</a>", html)
        self.assertIn(">Déployer</a>", html)
        self.assertNotIn("Supprimer l’image", html)
        self.assertNotIn("version-list", html)

    def test_wizard_explains_available_and_future_sources(self):
        html = (ROOT / "app/templates/reference_wizard.html").read_text(encoding="utf-8")
        self.assertIn("Serveur ou node existant", html)
        self.assertIn("AppBox existante", html)
        self.assertIn("Fichier ou archive", html)
        self.assertIn("non disponible dans alpha.5", html)
        self.assertNotIn("version_id", html)

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
