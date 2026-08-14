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
            private_path = "C:" + "\\Users\\example-user\\secret.pdf"
            file.write_text(private_path, encoding="utf-8")
            errors = validate_repository(root)
            self.assertTrue(any("absolute path" in error for error in errors))

    def test_accepts_minimal_valid_skill_metadata(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "scholar-slides")
            write_skill(root, "paper-tutor")
            self.assertEqual(validate_repository(root, minimal=True), [])

    def test_rejects_broken_relative_markdown_links(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "scholar-slides")
            write_skill(root, "paper-tutor")
            root.joinpath("README.md").write_text(
                "[missing](docs/missing.md)\n", encoding="utf-8"
            )
            errors = validate_repository(root, minimal=True)
            self.assertTrue(any("broken Markdown link" in error for error in errors))

    def test_rejects_malformed_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "scholar-slides")
            write_skill(root, "paper-tutor")
            root.joinpath("bad.json").write_text("{not-json}", encoding="utf-8")
            errors = validate_repository(root, minimal=True)
            self.assertTrue(any("invalid JSON" in error for error in errors))

    def test_rejects_scholar_slides_version_drift(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "scholar-slides")
            write_skill(root, "paper-tutor")
            skill = root / "skills/scholar-slides"
            skill.joinpath("VERSION").write_text("0.3.0\n", encoding="utf-8")
            skill.joinpath("runtime").mkdir()
            skill.joinpath("runtime/VERSION").write_text("0.4.0\n", encoding="utf-8")
            errors = validate_repository(root, minimal=True)
            self.assertTrue(any("version mismatch" in error for error in errors))

    def test_rejects_paper_tutor_agent_metadata_drift(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "scholar-slides")
            write_skill(root, "paper-tutor")
            metadata = root / "skills/paper-tutor/agents/openai.yaml"
            metadata.parent.mkdir()
            metadata.write_text(
                'interface:\n  display_name: "Wrong"\n'
                '  default_prompt: "Use $wrong-skill."\n',
                encoding="utf-8",
            )
            errors = validate_repository(root, minimal=True)
            self.assertTrue(any("Paper-Tutor agent metadata" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
