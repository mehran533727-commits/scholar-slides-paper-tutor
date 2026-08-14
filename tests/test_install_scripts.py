from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class InstallScriptSafetyTests(unittest.TestCase):
    def run_script(self, name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        if not POWERSHELL:
            self.skipTest("PowerShell is unavailable")
        return subprocess.run(
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-File",
                str(REPOSITORY_ROOT / "scripts" / name),
                *arguments,
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
        )

    def test_installer_names_only_the_two_known_skills(self):
        text = (REPOSITORY_ROOT / "scripts/install.ps1").read_text(encoding="utf-8")
        self.assertIn("scholar-slides", text)
        self.assertIn("paper-tutor", text)

    def test_uninstaller_targets_only_known_skill_directories(self):
        text = (REPOSITORY_ROOT / "scripts/uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("scholar-slides", text)
        self.assertIn("paper-tutor", text)
        self.assertNotIn("Remove-Item -LiteralPath $DestinationRoot -Recurse", text)

    def test_copy_only_install_refuses_overwrite_and_force_creates_backup(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "skills"
            first = self.run_script(
                "install.ps1",
                "-DestinationRoot",
                str(destination),
                "-SkipDependencies",
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertTrue((destination / "scholar-slides/SKILL.md").is_file())
            self.assertTrue((destination / "paper-tutor/SKILL.md").is_file())

            second = self.run_script(
                "install.ps1",
                "-DestinationRoot",
                str(destination),
                "-SkipDependencies",
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("already exists", second.stdout + second.stderr)

            forced = self.run_script(
                "install.ps1",
                "-DestinationRoot",
                str(destination),
                "-SkipDependencies",
                "-Force",
            )
            self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
            backups = list((destination / ".skill-backups").glob("*/scholar-slides/SKILL.md"))
            self.assertEqual(len(backups), 1)

    def test_uninstall_removes_only_known_skills(self):
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "skills"
            installed = self.run_script(
                "install.ps1",
                "-DestinationRoot",
                str(destination),
                "-SkipDependencies",
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            sentinel = destination / "keep-me.txt"
            sentinel.write_text("keep", encoding="utf-8")

            removed = self.run_script(
                "uninstall.ps1",
                "-DestinationRoot",
                str(destination),
                "-ConfirmRemoval",
            )
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            self.assertFalse((destination / "scholar-slides").exists())
            self.assertFalse((destination / "paper-tutor").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
