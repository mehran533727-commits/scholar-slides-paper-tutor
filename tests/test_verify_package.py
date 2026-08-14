from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.verify_package import validate_repository


def write_skill(root: Path, name: str) -> None:
    skill = root / "skills" / name
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Use when testing {name}.\n---\n",
        encoding="utf-8",
    )


class PackageValidationTests(unittest.TestCase):
    def test_rejects_generated_dependency_directories(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills/scholar-slides/runtime/.venv").mkdir(parents=True)
            errors = validate_repository(root)
            self.assertTrue(any(".venv" in error for error in errors))

    def test_rejects_private_absolute_paths(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "README.md"
            file.write_text(r"C:\Users\16595\secret.pdf", encoding="utf-8")
            errors = validate_repository(root)
            self.assertTrue(any("absolute path" in error for error in errors))

    def test_accepts_minimal_valid_skill_metadata(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "scholar-slides")
            write_skill(root, "paper-tutor")
            self.assertEqual(validate_repository(root, minimal=True), [])


if __name__ == "__main__":
    unittest.main()
