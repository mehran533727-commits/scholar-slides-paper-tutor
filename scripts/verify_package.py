#!/usr/bin/env python3
"""Validate the distributable Scholar-Slides + Paper-Tutor source tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


FORBIDDEN_DIRECTORY_NAMES = {".venv", "node_modules", "__pycache__"}
FORBIDDEN_FILE_SUFFIXES = {".pyc", ".pyo"}
TEXT_SUFFIXES = {
    ".cmd",
    ".css",
    ".example",
    ".gitignore",
    ".gitattributes",
    ".json",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

FULL_REQUIRED_PATHS = (
    ".gitattributes",
    ".gitignore",
    "README.md",
    "README.zh-CN.md",
    "LICENSES.md",
    "NOTICE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    ".github/workflows/validate.yml",
    "docs/INSTALLATION.md",
    "docs/INTEGRATION_GUIDE.md",
    "docs/ARCHITECTURE.md",
    "docs/EVIDENCE_SAFETY.md",
    "docs/DEVELOPMENT.md",
    "docs/RELEASE_CHECKLIST.md",
    "scripts/install.ps1",
    "scripts/uninstall.ps1",
    "scripts/verify-package.ps1",
    "skills/scholar-slides/SKILL.md",
    "skills/scholar-slides/LICENSE",
    "skills/scholar-slides/VERSION",
    "skills/scholar-slides/runtime/VERSION",
    "skills/scholar-slides/runtime/package.json",
    "skills/scholar-slides/runtime/package-lock.json",
    "skills/scholar-slides/runtime/requirements-runtime.txt",
    "skills/paper-tutor/SKILL.md",
    "skills/paper-tutor/agents/openai.yaml",
)

SENSITIVE_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+(?:[\\/]|$)", re.I),
    re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+(?:/|$)", re.I),
)
SECRET_PATTERNS = (
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---(?:\s*\n|\Z)", re.S)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_ignored(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    return ".git" in parts or any(part in FORBIDDEN_DIRECTORY_NAMES for part in parts)


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and not _is_ignored(path, root):
            yield path


def _parse_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    match = FRONTMATTER_RE.search(text)
    if not match:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def _validate_skill_metadata(root: Path, errors: list[str]) -> None:
    for name in ("scholar-slides", "paper-tutor"):
        skill_file = root / "skills" / name / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"missing skill definition: {_relative(root, skill_file)}")
            continue
        try:
            metadata = _parse_frontmatter(skill_file)
        except UnicodeError as exc:
            errors.append(f"invalid UTF-8 in {_relative(root, skill_file)}: {exc}")
            continue
        if metadata.get("name") != name:
            errors.append(
                f"skill name mismatch in {_relative(root, skill_file)}: "
                f"expected {name!r}, found {metadata.get('name')!r}"
            )
        description = metadata.get("description", "")
        if not description.startswith("Use when"):
            errors.append(
                f"skill description must start with 'Use when' in {_relative(root, skill_file)}"
            )


def _validate_forbidden_content(root: Path, errors: list[str]) -> None:
    tracked_paths: list[Path] | None = None
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            tracked_paths = [
                root / entry.decode("utf-8", errors="surrogateescape")
                for entry in result.stdout.split(b"\0")
                if entry
            ]

    if tracked_paths is not None:
        for path in tracked_paths:
            relative_parts = path.relative_to(root).parts
            forbidden_parts = [
                part for part in relative_parts if part in FORBIDDEN_DIRECTORY_NAMES
            ]
            if forbidden_parts:
                errors.append(
                    f"forbidden generated path is tracked: {_relative(root, path)}"
                )
            if path.suffix.lower() in FORBIDDEN_FILE_SUFFIXES:
                errors.append(f"forbidden generated file is tracked: {_relative(root, path)}")
        installed_manifest = root / "skills" / "scholar-slides" / "manifest.json"
        if installed_manifest in tracked_paths:
            errors.append(
                "forbidden installed production manifest: "
                f"{_relative(root, installed_manifest)}"
            )
        return

    reported_directories: set[str] = set()
    for path in root.rglob("*"):
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_dir() and path.name in FORBIDDEN_DIRECTORY_NAMES:
            relative = _relative(root, path)
            if relative not in reported_directories:
                errors.append(f"forbidden generated directory: {relative}")
                reported_directories.add(relative)
        if path.is_file() and path.suffix.lower() in FORBIDDEN_FILE_SUFFIXES:
            errors.append(f"forbidden generated file: {_relative(root, path)}")

    installed_manifest = root / "skills" / "scholar-slides" / "manifest.json"
    if installed_manifest.exists():
        errors.append(
            "forbidden installed production manifest: "
            f"{_relative(root, installed_manifest)}"
        )


def _validate_json(root: Path, errors: list[str]) -> None:
    for path in _iter_source_files(root):
        if path.suffix.lower() != ".json":
            continue
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"invalid JSON in {_relative(root, path)}: {exc}")


def _clean_link_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1 : target.index(">")]
    elif " " in target:
        target = target.split(" ", 1)[0]
    return target.split("#", 1)[0]


def _validate_markdown_links(root: Path, errors: list[str]) -> None:
    for path in _iter_source_files(root):
        if path.suffix.lower() != ".md":
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            errors.append(f"cannot read Markdown {_relative(root, path)}: {exc}")
            continue
        for raw_target in MARKDOWN_LINK_RE.findall(text):
            target = _clean_link_target(raw_target)
            if not target or target.startswith(("#", "/")):
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                errors.append(
                    f"Markdown link escapes repository in {_relative(root, path)}: {raw_target}"
                )
                continue
            if not resolved.exists():
                errors.append(
                    f"broken Markdown link in {_relative(root, path)}: {raw_target}"
                )


def _validate_sensitive_text(root: Path, errors: list[str]) -> None:
    for path in _iter_source_files(root):
        relative = _relative(root, path)
        if relative.startswith("docs/superpowers/"):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
            "LICENSE",
            "NOTICE",
            "VERSION",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if any(pattern.search(text) for pattern in SENSITIVE_PATH_PATTERNS):
            errors.append(f"private absolute path found in {relative}")
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            errors.append(f"possible secret found in {relative}")


def _validate_versions(root: Path, errors: list[str]) -> None:
    versions: dict[str, str] = {}
    scholar = root / "skills" / "scholar-slides"
    for label, path in (
        ("skill VERSION", scholar / "VERSION"),
        ("runtime VERSION", scholar / "runtime" / "VERSION"),
    ):
        if path.is_file():
            versions[label] = path.read_text(encoding="utf-8-sig").strip()

    package_json = scholar / "runtime" / "package.json"
    if package_json.is_file():
        try:
            versions["package.json"] = str(
                json.loads(package_json.read_text(encoding="utf-8-sig")).get("version", "")
            )
        except json.JSONDecodeError:
            pass

    pyproject = scholar / "runtime" / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(
            r'^version\s*=\s*["\']([^"\']+)["\']',
            pyproject.read_text(encoding="utf-8-sig"),
            re.M,
        )
        if match:
            versions["pyproject.toml"] = match.group(1)

    if versions and (set(versions.values()) != {"0.3.0"}):
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(versions.items()))
        errors.append(f"Scholar-Slides version mismatch: {rendered}")


def _validate_paper_tutor_agent(root: Path, errors: list[str]) -> None:
    metadata = root / "skills" / "paper-tutor" / "agents" / "openai.yaml"
    if not metadata.is_file():
        return
    text = metadata.read_text(encoding="utf-8-sig")
    if 'display_name: "Paper Tutor"' not in text or "$paper-tutor" not in text:
        errors.append(
            "Paper-Tutor agent metadata does not match skills/paper-tutor/SKILL.md"
        )


def _validate_license(root: Path, errors: list[str]) -> None:
    license_path = root / "skills" / "scholar-slides" / "LICENSE"
    if not license_path.is_file():
        return
    text = license_path.read_text(encoding="utf-8-sig")
    if "MIT License" not in text or "Copyright (c) 2026 louwill" not in text:
        errors.append("Scholar-Slides MIT license or louwill attribution changed")


def _validate_file_sizes(root: Path, errors: list[str]) -> None:
    github_limit = 100 * 1024 * 1024
    for path in _iter_source_files(root):
        if path.stat().st_size >= github_limit:
            errors.append(f"file reaches GitHub 100 MB limit: {_relative(root, path)}")


def validate_repository(root: Path, minimal: bool = False) -> list[str]:
    """Return deterministic validation errors for *root*; an empty list is valid."""

    root = root.resolve()
    errors: list[str] = []
    if not root.is_dir():
        return [f"repository root does not exist: {root}"]

    if not minimal:
        for relative in FULL_REQUIRED_PATHS:
            if not (root / relative).is_file():
                errors.append(f"missing required file: {relative}")

    _validate_skill_metadata(root, errors)
    _validate_forbidden_content(root, errors)
    _validate_json(root, errors)
    _validate_markdown_links(root, errors)
    _validate_sensitive_text(root, errors)
    _validate_versions(root, errors)
    _validate_paper_tutor_agent(root, errors)
    _validate_license(root, errors)
    _validate_file_sizes(root, errors)
    return sorted(set(errors))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("--minimal", action="store_true")
    args = parser.parse_args(argv)
    errors = validate_repository(args.root, minimal=args.minimal)
    if errors:
        print(f"Package validation failed with {len(errors)} error(s):", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Package validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
