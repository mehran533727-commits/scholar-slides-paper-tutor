#!/usr/bin/env python3
"""Safely inspect or remove generated development artifacts.

Only explicit allowlisted names below the repository root are considered. Git
tracked files always win over cleanup: they are reported as protected and never
deleted, even when nested under an otherwise generated directory.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


class CleanupError(RuntimeError):
    pass


ALLOW_DIRS = frozenset({"build", "out", "dist", ".superpowers", ".pytest_cache", "coverage"})
ALLOW_SUFFIXES = (".egg-info",)
ALLOW_NAMES = frozenset({"__pycache__"})


def _root(path: str | Path) -> Path:
    root = Path(path).resolve()
    if not (root / ".git").exists() and not (root / "scripts").is_dir():
        raise CleanupError(f"not a scholar-slides repository root: {root}")
    return root


def tracked_files(root: str | Path) -> set[str]:
    root = _root(root)
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False, shell=False)
    if result.returncode != 0:
        raise CleanupError(result.stderr.decode("utf-8", "replace").strip() or "git ls-files failed")
    return {item for item in result.stdout.decode("utf-8").split("\0") if item}


def _is_candidate(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if relative.parts and relative.parts[0] in ALLOW_DIRS:
        return True
    return path.name in ALLOW_NAMES or any(path.name.endswith(suffix) for suffix in ALLOW_SUFFIXES)


def _descendant_tracked(relative: str, tracked: set[str]) -> bool:
    prefix = relative.rstrip("/") + "/"
    return any(item == relative or item.startswith(prefix) for item in tracked)


def inspect_cleanup(root: str | Path, *, tracked: set[str] | None = None) -> dict:
    root = _root(root)
    tracked = tracked if tracked is not None else globals()["tracked_files"](root)
    files: list[str] = []
    protected: list[str] = []
    candidates: list[str] = []
    total = 0
    for item in sorted(root.rglob("*"), key=lambda p: (len(p.relative_to(root).parts), p.as_posix())):
        if not _is_candidate(item, root):
            continue
        if item.is_symlink():
            raise CleanupError(f"refusing to clean symlink candidate: {item.relative_to(root).as_posix()}")
        relative = item.relative_to(root).as_posix()
        if item.is_file():
            if relative in tracked:
                protected.append(relative)
                continue
            files.append(relative)
            total += item.stat().st_size
        elif item.is_dir() and not _descendant_tracked(relative, tracked):
            candidates.append(relative)
    return {"dry_run": True, "root": str(root), "files": files, "directories": candidates, "protected": protected, "bytes": total}


def apply_cleanup(root: str | Path) -> dict:
    root = _root(root)
    result = inspect_cleanup(root)
    # Remove files first, then empty generated directories. Never call a broad
    # recursive delete on the repository root or on a directory containing a
    # tracked file.
    for relative in result["files"]:
        target = (root / relative).resolve()
        if root not in target.parents:
            raise CleanupError(f"cleanup target escaped repository root: {relative}")
        target.unlink(missing_ok=True)
    for relative in sorted(result["directories"], key=lambda value: len(Path(value).parts), reverse=True):
        target = root / relative
        if target.is_dir() and not target.is_symlink():
            try:
                target.rmdir()
            except OSError:
                pass
    result["dry_run"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dry-run", action="store_true", help="inspect only (the default)")
    parser.add_argument("--apply", action="store_true", help="remove only allowlisted untracked artifacts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = apply_cleanup(args.root) if args.apply else inspect_cleanup(args.root)
    except CleanupError as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    result["ok"] = True
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        action = "removed" if not result["dry_run"] else "would remove"
        print(f"Cleanup {action} {len(result['files'])} files and {len(result['directories'])} directories ({result['bytes']} bytes).")
        if result["protected"]:
            print(f"Protected tracked/symlink entries: {len(result['protected'])}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
