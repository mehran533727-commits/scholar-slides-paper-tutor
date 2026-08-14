#!/usr/bin/env python3
"""Fail-closed command-line facade for the scholar-slides workflow."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Mapping, Sequence

from project_config import CONFIG_VERSION, DEFAULT_OPTIONS, ConfigError, doctor as run_doctor, load_project_config, resolve_options
from deck_types import DeckTypeError, resolve_deck_options
from paper_metadata import validate_metadata_for_ckpt1_preparation


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_version() -> str:
    """Resolve the distribution version in both source and installed layouts."""
    try:
        return package_version("codex-scholar-slides")
    except PackageNotFoundError:
        try:
            return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            return "0.0.0"


VERSION = _load_version()
PENDING = 2
STAGES = ("ingest", "digest", "deck", "review", "export")


class CliError(RuntimeError):
    """An error that can tell the caller how to proceed safely."""

    def __init__(self, message: str, next_step: str) -> None:
        super().__init__(message)
        self.next_step = next_step


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError(message, "Run scholar-slides --help to see supported commands and flags.")


def _checkpoint_status(record: Path) -> str | None:
    if not record.is_file():
        return None
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"Cannot read checkpoint record: {record}: {exc}", "Repair or recreate the checkpoint record.") from exc
    return payload.get("status") if isinstance(payload, dict) else None


def _run(script: str, args: Sequence[str | Path], *, verbose: bool, json_mode: bool = False) -> None:
    command = [sys.executable, str(SCRIPTS / script), *[str(arg) for arg in args]]
    if verbose:
        print("+", " ".join(command), file=sys.stderr if json_mode else sys.stdout)
    result = subprocess.run(command, cwd=ROOT, shell=False, check=False, capture_output=json_mode, text=json_mode)
    if result.returncode:
        raise CliError(
            f"Stage command failed ({script}) with exit code {result.returncode}.",
            f"Fix the reported {script} error, then rerun with --resume.",
        )


def _run_node(script: str, args: Sequence[str | Path], *, verbose: bool) -> None:
    command = ["node", str(SCRIPTS / script), *[str(arg) for arg in args]]
    if verbose:
        print("+", " ".join(command))
    result = subprocess.run(command, cwd=ROOT, shell=False, check=False)
    if result.returncode:
        raise CliError(
            f"Stage command failed ({script}) with exit code {result.returncode}.",
            f"Fix the reported {script} error, then rerun with --resume.",
        )


def _bundle(args: argparse.Namespace) -> Path:
    if args.out:
        return Path(args.out)
    if args.input:
        return ROOT / "out" / Path(args.input).stem
    raise CliError("--out is required when resuming without --input.", "Provide --out <paper-bundle>.")


def _require_input(args: argparse.Namespace) -> Path:
    if not args.input:
        raise CliError("--input is required for this operation.", "Provide --input <local.pdf|arXiv-id|arXiv-url>.")
    return Path(args.input)


def _project_options_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {"config_version": CONFIG_VERSION, "options": args.options}


def _canonical_option_value(value: Any) -> Any:
    """Compare JSON-bound options independently of tuple/list implementation details."""
    if isinstance(value, Mapping):
        return {str(key): _canonical_option_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_option_value(child) for child in value]
    return value


def _options_match_bound_project(bound: Mapping[str, Any], expected: Mapping[str, Any], args: argparse.Namespace) -> bool:
    """Accept the 0.1.9 two-format default when no new export override was requested.

    Export gained HTML and notes in 0.2.0, but an existing CKPT-bound project may still
    intentionally select a supported subset such as ``[pdf, pptx]``.  That migration
    compatibility must not relax any other bound presentation option or an explicit
    ``export`` setting supplied through ``--config``.
    """
    if _canonical_option_value(bound) == _canonical_option_value(expected):
        return True
    if args.project_config.get("export") is not None:
        return False
    expected_export = expected.get("export")
    bound_export = bound.get("export")
    if not isinstance(expected_export, Mapping) or not isinstance(bound_export, Mapping):
        return False
    if _canonical_option_value(expected_export.get("formats")) != _canonical_option_value(DEFAULT_OPTIONS["export"]["formats"]):
        return False
    bound_formats = bound_export.get("formats")
    supported = set(DEFAULT_OPTIONS["export"]["formats"])
    if not isinstance(bound_formats, list) or not bound_formats or not set(bound_formats) <= supported:
        return False
    return all(
        _canonical_option_value(bound.get(key)) == _canonical_option_value(expected.get(key))
        for key in set(bound) | set(expected)
        if key != "export"
    )


def _bind_project_options(args: argparse.Namespace, bundle: Path) -> Path:
    """Persist effective options before stages create evidence that depends on them."""
    path = bundle / "project-options.json"
    payload = _project_options_payload(args)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") != serialized:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None
        if not isinstance(existing, Mapping) or existing.get("config_version") != payload["config_version"] or not _options_match_bound_project(existing.get("options", {}), payload["options"], args):
            raise CliError("Resolved project options differ from the options already bound to this bundle.", "Use the original configuration, or create a new bundle before changing presentation settings.")
    bundle.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(serialized, encoding="utf-8")
    return path


def _revalidate_bound_options(args: argparse.Namespace, bundle: Path) -> None:
    path = bundle / "project-options.json"
    if path.is_file():
        expected = json.dumps(_project_options_payload(args), indent=2, sort_keys=True) + "\n"
        if path.read_text(encoding="utf-8") != expected:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if not isinstance(existing, Mapping) or existing.get("config_version") != CONFIG_VERSION or not _options_match_bound_project(existing.get("options", {}), json.loads(expected)["options"], args):
                raise CliError("Resolved project options differ from the options bound to this bundle.", "Use the original configuration; presentation settings cannot change after checkpoint evidence exists.")
    elif args.project_config:
        raise CliError("This command cannot apply project presentation settings to an existing unbound bundle.", "Run build or ingest with this configuration to create a new, bound project-options.json artifact.")


def _ingest(args: argparse.Namespace) -> int:
    _bind_project_options(args, _bundle(args))
    _run("prepare_source.py", [_require_input(args), "--out-dir", _bundle(args)], verbose=args.verbose)
    print(f"Source bundle created at {_bundle(args)}.")
    return 0


def _require_reliable_ckpt1_metadata(bundle: Path) -> None:
    """Fail before checkpoint creation when the paper identity is not auditable."""
    try:
        digest = json.loads((bundle / "digest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError("Cannot read digest metadata before CKPT-1 creation.", "Rebuild the digest and retain source-grounded paper_metadata.") from exc
    metadata = digest.get("paper_metadata") if isinstance(digest, dict) else None
    if not isinstance(metadata, dict):
        raise CliError("CKPT-1 metadata blocker: [MISSING: paper_metadata]", "Provide source-grounded title and authors in digest.json before CKPT-1.")
    blockers = validate_metadata_for_ckpt1_preparation(metadata)
    if blockers:
        raise CliError("; ".join(blockers), "Resolve the [MISSING]/[UNVERIFIED] title or authors evidence before CKPT-1.")


def _build(args: argparse.Namespace) -> int:
    bundle = _bundle(args)
    project_options = _bind_project_options(args, bundle)
    ckpt1 = bundle / "checkpoint-1.json"
    ckpt2 = bundle / "checkpoint-2.json"
    c1_status = _checkpoint_status(ckpt1)
    if c1_status is None:
        _run("prepare_source.py", [_require_input(args), "--out-dir", bundle], verbose=args.verbose)
        _run("build_digest.py", [bundle], verbose=args.verbose)
        _require_reliable_ckpt1_metadata(bundle)
        _run("checkpoint.py", ["create", "CKPT-1", bundle / "digest.json", ckpt1, "--attach", project_options], verbose=args.verbose)
        _run("user_documents.py", ["paper-analysis", bundle], verbose=args.verbose)
        raise CliError(
            "CKPT-1 is pending human confirmation; build stopped before deck generation.",
            f"Review {bundle / 'digest.md'}, explicitly approve CKPT-1, then rerun build --resume.",
        )
    if c1_status not in {"confirmed", "approved"}:
        raise CliError(
            f"CKPT-1 is {c1_status!r}, not explicitly confirmed.",
            "Review the digest and explicitly approve CKPT-1 before resuming.",
        )
    c2_status = _checkpoint_status(ckpt2)
    if c2_status == "approved":
        print("CKPT-2 is approved. The pending-CKPT-2 review preview is no longer available; continue with the approved checkpoint workflow.")
        return 0
    if c2_status is None:
        _run("generate_deck.py", [bundle, "--options", project_options], verbose=args.verbose)
        _run("checkpoint.py", ["create", "CKPT-2", bundle / "deck.json", ckpt2, "--requires", ckpt1, "--asset-graph", bundle / "asset-graph.json", "--attach", bundle / "deck-outline.md", "--attach", bundle / "presentation-script.md", "--attach", bundle / "presentation-summary.md", "--attach", project_options], verbose=args.verbose)
    preview_command: list[str | Path] = [bundle, "--checkpoint", ckpt2]
    if args.json:
        preview_command.append("--json")
    if args.force_rebuild_stage == "review":
        preview_command.append("--force-rebuild-review")
    _run("review_preview.py", preview_command, verbose=args.verbose)
    raise CliError(
        "CKPT-2 is pending; review preview is ready and build stopped before approval or export.",
        f"Review {bundle / 'review' / 'montage.png'} and explicitly approve CKPT-2 only after examining the visual QA evidence.",
    )


def _review(args: argparse.Namespace) -> int:
    bundle = _bundle(args)
    _revalidate_bound_options(args, bundle)
    ckpt2 = bundle / "checkpoint-2.json"
    if _checkpoint_status(ckpt2) != "pending":
        raise CliError(
            "Review preview requires a pending CKPT-2.",
            "Create or restore a pending CKPT-2 review record before generating a new preview.",
        )
    command: list[str | Path] = [bundle, "--checkpoint", ckpt2]
    if args.json:
        command.append("--json")
    if args.force_rebuild_stage == "review":
        command.append("--force-rebuild-review")
    _run("review_preview.py", command, verbose=args.verbose)
    return 0


def _export(args: argparse.Namespace) -> int:
    bundle = _bundle(args)
    _revalidate_bound_options(args, bundle)
    formats = getattr(args, "formats", None)
    command: list[str | Path] = [bundle]
    if formats is not None:
        command += ["--formats", formats]
    if args.resume or args.force_rebuild_stage == "export":
        command.append("--resume")
    if getattr(args, "keep_validation_artifacts", False):
        command.append("--keep-validation-artifacts")
    if args.json:
        command.append("--json")
    if args.verbose:
        command.append("--verbose")
    _run_node("delivery.mjs", command, verbose=args.verbose)
    return 0


def _approve(args: argparse.Namespace) -> int:
    _revalidate_bound_options(args, _bundle(args))
    record = _bundle(args) / f"checkpoint-{args.checkpoint}.json"
    if not args.confirmed_by:
        raise CliError("approve requires --confirmed-by.", "Re-run approve with the explicit confirmer's name.")
    if args.checkpoint == 1:
        bundle = _bundle(args)
        readiness = bundle / "ckpt1-readiness.json"
        if args.attach:
            raise CliError("CKPT-1 approval consumes the existing prepared evidence bundle; --attach cannot rebuild it.", "Run prepare-checkpoint again if review evidence changed, then approve the resulting pending_human_confirmation record.")
        _run("ckpt1_readiness.py", [bundle, "--verify-existing"], verbose=args.verbose, json_mode=args.json)
        _run("checkpoint.py", ["approve", record, "--confirmed-by", args.confirmed_by, "--readiness-artifact", readiness], verbose=args.verbose, json_mode=args.json)
        _run("user_documents.py", ["paper-analysis", bundle], verbose=args.verbose, json_mode=args.json)
        if args.json:
            print(json.dumps({"ok": True, "checkpoint": "CKPT-1", "status": "confirmed", "project": str(bundle)}, ensure_ascii=False, sort_keys=True))
        return 0
    command: list[str | Path] = ["approve", record, "--confirmed-by", args.confirmed_by]
    _run("checkpoint.py", command, verbose=args.verbose)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    checks = run_doctor()
    payload = {"ok": not any(check.status == "FAIL" for check in checks), "version": VERSION, "config_version": CONFIG_VERSION, "options": args.options, "checks": [check.as_dict() for check in checks]}
    if args.json:
        print(json.dumps(payload))
    else:
        print(f"CONFIG: effective options {json.dumps(args.options, sort_keys=True)}")
        for check in checks:
            suffix = f" Next: {check.next_step}" if check.next_step else ""
            print(f"{check.status}: {check.name}: {check.message}{suffix}")
    return 0 if payload["ok"] else 1


def _prepare_checkpoint(args: argparse.Namespace) -> int:
    # Keep --version and basic help usable in the minimal no-dependency console install.
    # The CKPT-1 preparation stack is loaded only when the public command is invoked.
    from prepare_checkpoint import PrepareCheckpointError, prepare_checkpoint

    try:
        result = prepare_checkpoint(
            args.project,
            args.review_input,
            checkpoint_name=args.checkpoint,
            prepared_by=args.prepared_by,
            dry_run=args.dry_run,
            project_options=_project_options_payload(args),
        )
    except (PrepareCheckpointError, OSError) as exc:
        raise CliError(str(exc), "Fix the CKPT-1 candidate or source evidence, then rerun prepare-checkpoint --resume.") from exc
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif result.get("changed"):
        print(f"CKPT-1 prepared for human confirmation at {args.project}.")
    else:
        print("CKPT-1 preparation is already current; no files changed.")
    return 0


def _reuse_confirmed_ckpt1(args: argparse.Namespace) -> int:
    command: list[str | Path] = [
        "reuse-confirmed-ckpt1",
        args.source_record,
        args.destination_project,
    ]
    if args.destination_record:
        command.extend(["--record", args.destination_record])
    process = subprocess.run(
        [sys.executable, str(SCRIPTS / "checkpoint.py"), *[str(item) for item in command]],
        cwd=ROOT,
        shell=False,
        check=False,
        capture_output=args.json,
        text=args.json,
    )
    if process.returncode:
        detail = (process.stderr or process.stdout or "").strip() if args.json else "checkpoint reuse failed"
        raise CliError(
            f"CKPT-1 reuse failed: {detail}",
            "Keep the original confirmed CKPT-1 unchanged and fix the destination project before retrying.",
        )
    if args.json:
        print((process.stdout or "").strip())
    return 0


def _reopen(args: argparse.Namespace) -> int:
    from checkpoint import CheckpointError, reopen_ckpt2

    try:
        result = reopen_ckpt2(
            Path(args.out),
            requested_by=args.requested_by,
            reason=args.reason,
            dry_run=args.dry_run,
            resume=args.resume,
        )
    except CheckpointError as exc:
        raise CliError(str(exc), "Fix the reported lifecycle precondition, then rerun reopen.") from exc
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    elif result.get("dry_run"):
        print(f"CKPT-2 reopen plan: archive revision {result['revision_id']} to {result['history_path']}; no changes written.")
    elif result.get("changed"):
        print(f"CKPT-2 reopened: approved revision {result['revision_id']} archived; a new review is required before export.")
    else:
        print(f"CKPT-2 already reopened: revision {result['revision_id']} remains archived.")
    return 0


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument("--json", action="store_true", default=default, help="emit errors as JSON")
    parser.add_argument("--verbose", action="store_true", default=default)
    parser.add_argument("--resume", action="store_true", default=default)
    parser.add_argument("--keep-validation-artifacts", action="store_true", default=default)
    parser.add_argument("--force-rebuild-stage", choices=STAGES, default=default)
    parser.add_argument("--config", default=default, help="path to a versioned scholar-slides.yaml project configuration")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="scholar-slides", description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    _add_global_flags(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "ingest", "review", "export", "doctor"):
        child = sub.add_parser(name)
        _add_global_flags(child, suppress_defaults=True)
        if name != "doctor":
            child.add_argument("--input")
            child.add_argument("--out", "--project", dest="out", metavar="PROJECT")
        if name == "export":
            child.add_argument("--formats", help="comma-separated formats: html,pdf,pptx,notes")
        if name == "build":
            child.add_argument("--language")
            child.add_argument("--audience")
            child.add_argument("--slides", type=int)
            child.add_argument("--deck-type")
            child.add_argument("--talk-time", dest="talk_time_minutes", type=int)
            child.add_argument("--density")
            child.add_argument("--theme")
    approve = sub.add_parser("approve")
    _add_global_flags(approve, suppress_defaults=True)
    approve.add_argument("checkpoint", type=int, choices=(1, 2))
    approve.add_argument("--out", required=True)
    approve.add_argument("--confirmed-by")
    approve.add_argument("--attach", action="append", default=[])
    prepare = sub.add_parser("prepare-checkpoint", help="prepare CKPT-1 evidence for later human confirmation")
    _add_global_flags(prepare, suppress_defaults=True)
    prepare.add_argument("--project", required=True)
    prepare.add_argument("--checkpoint", required=True, choices=("CKPT-1",))
    prepare.add_argument("--review-input", required=True)
    prepare.add_argument("--prepared-by", required=True)
    prepare.add_argument("--dry-run", action="store_true", default=False)
    reuse = sub.add_parser(
        "reuse-confirmed-ckpt1",
        help="reuse a confirmed CKPT-1 in a new project without modifying the source record",
    )
    _add_global_flags(reuse, suppress_defaults=True)
    reuse.add_argument("--source-record", required=True)
    reuse.add_argument("--destination-project", required=True)
    reuse.add_argument("--destination-record")
    reopen = sub.add_parser("reopen", help="archive an approved CKPT-2 and reopen the project for a revised deck review")
    _add_global_flags(reopen, suppress_defaults=True)
    reopen.add_argument("--out", "--project", dest="out", required=True, metavar="PROJECT")
    reopen.add_argument("--checkpoint", required=True, choices=("CKPT-2",))
    reopen.add_argument("--requested-by", required=True)
    reopen.add_argument("--reason", required=True)
    reopen.add_argument("--dry-run", action="store_true", default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        try:
            args.project_config = load_project_config(Path(args.config) if args.config is not None else None)
        except ConfigError as exc:
            raise CliError(str(exc), "Fix the project configuration or pass --config <portable-config-path>.") from exc
        try:
            args.options = resolve_deck_options(resolve_options(
                {
                    "language": getattr(args, "language", None),
                    "audience": getattr(args, "audience", None),
                    "theme": getattr(args, "theme", None),
                    "slide_count": getattr(args, "slides", None),
                    "deck_type": getattr(args, "deck_type", None),
                    "talk_time_minutes": getattr(args, "talk_time_minutes", None),
                    "density": getattr(args, "density", None),
                },
                {key: value for key, value in args.project_config.items() if key != "version"},
                DEFAULT_OPTIONS,
            ))
        except DeckTypeError as exc:
            raise CliError(str(exc), "Use a supported deck type and valid density/time constraints.") from exc
        if args.force_rebuild_stage is not None and args.force_rebuild_stage not in {"review", "export"}:
            raise CliError(
                f"--force-rebuild-stage {args.force_rebuild_stage!r} is not supported by the fail-closed facade.",
                "Use the relevant documented stage command after explicitly invalidating and reviewing its checkpoint evidence.",
            )
        handlers = {"build": _build, "ingest": _ingest, "review": _review, "approve": _approve, "export": _export, "doctor": _doctor, "prepare-checkpoint": _prepare_checkpoint, "reuse-confirmed-ckpt1": _reuse_confirmed_ckpt1, "reopen": _reopen}
        return handlers[args.command](args)
    except CliError as exc:
        json_mode = "--json" in (argv if argv is not None else sys.argv[1:])
        if json_mode:
            print(json.dumps({"ok": False, "error": str(exc), "next_step": exc.next_step}))
        else:
            print(f"Error: {exc}\nNext: {exc.next_step}", file=sys.stderr)
        return 1 if "pending" not in str(exc).lower() else PENDING
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
