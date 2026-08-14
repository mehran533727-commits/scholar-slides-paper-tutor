#!/usr/bin/env python3
"""Safely remove or roll back the user-local scholar-slides runtime."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


class UninstallError(RuntimeError):
    pass


def _manifest(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _inside(root: Path, path: Path) -> None:
    root = root.resolve()
    candidate = path.resolve(strict=False)
    if candidate != root and root not in candidate.parents:
        raise UninstallError(f"refusing to operate outside the selected root: {path}")


def _versions(runtime_root: Path) -> list[Path]:
    versions = runtime_root / "versions"
    if not versions.is_dir():
        return []
    return sorted((item for item in versions.iterdir() if item.is_dir() and not item.is_symlink()), key=lambda p: p.name)


def _current(runtime_root: Path) -> Path | None:
    link = runtime_root / "current"
    if not link.is_symlink():
        return None
    try:
        target = link.resolve(strict=True)
    except OSError:
        return None
    return target if target.parent == (runtime_root / "versions").resolve() else None


def _size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _skill_copy(source: Path, destination: Path, release_hash: str, version: str) -> None:
    temporary = destination.parent / f".scholar-slides-rollback-{os.getpid()}"
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(source, temporary)
    (temporary / ".install-manifest.json").write_text(json.dumps({"release_sha256": release_hash, "version": version}) + "\n", encoding="utf-8")
    old = destination.parent / f".scholar-slides-old-{os.getpid()}"
    if old.exists():
        shutil.rmtree(old)
    if destination.exists():
        os.replace(destination, old)
    os.replace(temporary, destination)
    if old.exists():
        shutil.rmtree(old)


def execute(args: argparse.Namespace) -> dict:
    runtime_root = Path(args.runtime_root).resolve()
    skills_dir = Path(args.skills_dir).resolve()
    bin_dir = Path(args.bin_dir).resolve()
    versions = _versions(runtime_root)
    current = _current(runtime_root)
    selected = None
    if args.version:
        selected = runtime_root / "versions" / args.version
        if not selected.is_dir():
            raise UninstallError(f"installed version not found: {args.version}")
    elif args.all_versions:
        selected = None
    elif current is not None:
        selected = current
    elif versions:
        selected = versions[-1]
    skill = skills_dir / "scholar-slides"
    launcher = bin_dir / "scholar-slides"
    launcher_configs = [bin_dir / ".scholar-slides-runtime", bin_dir / ".scholar-slides-skills", bin_dir / ".scholar-slides-browser"]
    removable: list[Path] = []
    if args.rollback:
        if current is None:
            raise UninstallError("rollback requires a current runtime")
        previous = [item for item in versions if item != current]
        if not previous:
            raise UninstallError("rollback requires at least one retained previous version")
        target = previous[-1]
        removable = []
    else:
        if args.all_versions:
            removable.extend(versions)
        elif selected is not None:
            removable.append(selected)
    if args.remove_cache:
        removable.append(runtime_root / "cache")
    # A dry-run reports only exact targets. User projects, PDFs, deliveries,
    # Zotero data, and shared caches are not derived targets unless --remove-cache.
    paths = [item for item in removable if item.exists() or item.is_symlink()]
    if args.rollback:
        paths = [runtime_root / "current", skill]
    elif current is not None and selected == current and (args.all_versions or len(versions) <= 1):
        paths.extend([runtime_root / "current", skill, launcher, *launcher_configs])
    elif args.all_versions:
        paths.extend([runtime_root / "current", skill, launcher, *launcher_configs])
    elif not versions:
        paths.extend([skill, launcher, *launcher_configs])
    paths = list(dict.fromkeys(paths))
    for path in paths:
        _inside(runtime_root, path) if runtime_root in path.parents or path == runtime_root else None
    payload = {
        "ok": True,
        "dry_run": args.dry_run,
        "operation": "rollback" if args.rollback else "uninstall",
        "runtime_root": str(runtime_root),
        "skills_dir": str(skills_dir),
        "bin_dir": str(bin_dir),
        "targets": [str(path) for path in paths],
        "bytes": sum(_size(path) for path in paths if path.exists() or path.is_symlink()),
        "preserved": ["user projects", "PDF files", "deliveries", "Zotero data"] + ([] if args.remove_cache else ["shared browser cache"]),
    }
    if args.dry_run:
        return payload
    if args.rollback:
        old_current = os.readlink(runtime_root / "current") if (runtime_root / "current").is_symlink() else None
        target = previous[-1]
        target_manifest = _manifest(target / "install-manifest.json")
        try:
            _atomic = runtime_root / f".current-rollback-{os.getpid()}"
            _atomic.unlink(missing_ok=True)
            os.symlink(f"versions/{target.name}", _atomic)
            os.replace(_atomic, runtime_root / "current")
            source_skill = target / "skill"
            if not source_skill.is_dir():
                raise UninstallError(f"retained version has no Skill payload: {target}")
            _skill_copy(source_skill, skill, str(target_manifest.get("release_sha256", "")), target.name)
        except Exception:
            if old_current is not None:
                rollback_link = runtime_root / f".current-restore-{os.getpid()}"
                rollback_link.unlink(missing_ok=True)
                os.symlink(old_current, rollback_link)
                os.replace(rollback_link, runtime_root / "current")
            raise
        return payload | {"target_version": target.name}
    for path in removable:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    remaining = _versions(runtime_root)
    if current is not None and selected == current and remaining:
        target = remaining[-1]
        link = runtime_root / f".current-uninstall-{os.getpid()}"
        link.unlink(missing_ok=True)
        os.symlink(f"versions/{target.name}", link)
        os.replace(link, runtime_root / "current")
        target_manifest = _manifest(target / "install-manifest.json")
        if (target / "skill").is_dir():
            _skill_copy(target / "skill", skill, str(target_manifest.get("release_sha256", "")), target.name)
    else:
        (runtime_root / "current").unlink(missing_ok=True)
        if not remaining:
            skill.unlink(missing_ok=True) if skill.is_symlink() else shutil.rmtree(skill, ignore_errors=True)
            launcher.unlink(missing_ok=True)
            for config in launcher_configs:
                config.unlink(missing_ok=True)
    return payload | {"remaining_versions": [item.name for item in remaining]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", default=os.environ.get("SCHOLAR_SLIDES_RUNTIME_ROOT", str(Path.home() / ".local" / "share" / "scholar-slides")))
    parser.add_argument("--skills-dir", default=os.environ.get("SCHOLAR_SLIDES_CODEX_SKILLS_DIR", str(Path.home() / ".codex" / "skills")))
    parser.add_argument("--bin-dir", default=os.environ.get("XDG_BIN_HOME", str(Path.home() / ".local" / "bin")))
    parser.add_argument("--version")
    parser.add_argument("--all-versions", action="store_true")
    parser.add_argument("--remove-cache", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = execute(args)
    except (UninstallError, OSError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True) if args.json else f"{payload['operation']} {'would be' if args.dry_run else 'completed'}; predicted/removed {payload['bytes']} bytes.\nPreserved: {', '.join(payload['preserved'])}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
