from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class InstallScriptSafetyTests(unittest.TestCase):
    def test_installer_names_only_the_two_known_skills(self):
        text = (REPOSITORY_ROOT / "scripts/install.ps1").read_text(encoding="utf-8")
        self.assertIn("scholar-slides", text)
        self.assertIn("paper-tutor", text)

    def test_uninstaller_targets_only_known_skill_directories(self):
        text = (REPOSITORY_ROOT / "scripts/uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("scholar-slides", text)
        self.assertIn("paper-tutor", text)
        self.assertNotIn("Remove-Item -LiteralPath $DestinationRoot -Recurse", text)


if __name__ == "__main__":
    unittest.main()
