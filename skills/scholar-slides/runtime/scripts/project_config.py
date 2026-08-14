"""Versioned, portable project configuration and environment diagnostics."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from deck_types import DeckTypeError, get_deck_contract, resolve_deck_options


ROOT = Path(__file__).resolve().parents[1]
CONFIG_VERSION = 1
SUPPORTED_EXPORT_FORMATS = ("html", "pdf", "pptx", "notes")
DEFAULT_OPTIONS: dict[str, Any] = {
    "language": "zh-CN",
    "deck_type": "journal-club",
    "theme": "academic",
    "checkpoint": {"require_human_confirmation": True},
    "export": {"formats": list(SUPPORTED_EXPORT_FORMATS)},
}
_TOP_LEVEL_KEYS = frozenset({"version", *DEFAULT_OPTIONS, "audience", "slide_count", "density", "talk_time_minutes", "checkpoints", "exports"})
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class ConfigError(ValueError):
    """A project configuration cannot be safely applied."""


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    next_step: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def _scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith(("'", '"')):
        if len(value) < 2 or value[-1] not in {"'", '"'}:
            raise ConfigError("Malformed configuration: unterminated quote.")
        if value[-1] != value[0]:
            raise ConfigError("Malformed configuration: malformed quote.")
    elif value.endswith(("'", '"')):
        raise ConfigError("Malformed configuration: malformed quote.")
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(part) for part in value[1:-1].split(",") if part.strip()]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _parse_yaml_like(text: str) -> dict[str, Any]:
    """Parse the deliberately small mapping/list subset used by the example file."""
    result: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, result)]
    previous_indent: int | None = None
    previous_was_container = False
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line or ":" not in raw_line:
            if "\t" in raw_line:
                raise ConfigError(f"Malformed configuration at line {line_number}: tab indentation is not allowed.")
            raise ConfigError(f"Malformed configuration at line {line_number}.")
        if (previous_indent is None and indent != 0) or (previous_indent is not None and indent > previous_indent and not previous_was_container):
            raise ConfigError(f"Invalid orphan indentation at line {line_number}.")
        key, raw_value = raw_line.strip().split(":", 1)
        if not key or key.strip() != key:
            raise ConfigError(f"Malformed configuration key at line {line_number}.")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ConfigError(f"Invalid indentation at line {line_number}.")
        parent = stack[-1][1]
        value = raw_value.strip()
        if not value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
            previous_was_container = True
        else:
            parent[key] = _scalar(value)
            previous_was_container = False
        previous_indent = indent
    return result


def _contains_absolute_path(value: Any) -> bool:
    if isinstance(value, str):
        return Path(value).is_absolute() or value.startswith("\\\\") or bool(_WINDOWS_ABSOLUTE.match(value))
    if isinstance(value, Mapping):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    return False


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    for alias, canonical in (("checkpoints", "checkpoint"), ("exports", "export")):
        if alias in config:
            if canonical in config:
                raise ConfigError(f"Use only one of {canonical} and {alias}.")
            config[canonical] = config.pop(alias)
    unexpected = set(config) - _TOP_LEVEL_KEYS
    if unexpected:
        raise ConfigError(f"Unsupported configuration key(s): {', '.join(sorted(unexpected))}.")
    if config.get("version") != CONFIG_VERSION:
        raise ConfigError(f"Configuration version must be {CONFIG_VERSION}.")
    if _contains_absolute_path(config):
        raise ConfigError("Configuration must not contain an absolute local path.")
    for name in ("language", "audience", "theme", "deck_type", "density"):
        if name in config and not isinstance(config[name], str):
            raise ConfigError(f"{name} must be a string.")
    try:
        deck_contract = get_deck_contract(config.get("deck_type"))
    except DeckTypeError as exc:
        raise ConfigError(str(exc)) from exc
    for name in ("density", "talk_time_minutes"):
        if name in config:
            try:
                resolve_deck_options({name: config[name], "deck_type": config.get("deck_type")})
            except DeckTypeError as exc:
                raise ConfigError(str(exc)) from exc
    if "slide_count" in config:
        budget = deck_contract.time_to_slide_budget["slide_count"]
        if (
            not isinstance(config["slide_count"], int)
            or isinstance(config["slide_count"], bool)
            or not budget["min"] <= config["slide_count"] <= budget["max"]
        ):
            raise ConfigError(
                f"slide_count must be an integer from {budget['min']} through {budget['max']} for {deck_contract.deck_type.value}."
            )
    if "checkpoint" in config and not isinstance(config["checkpoint"], dict):
        raise ConfigError("checkpoint must be a mapping.")
    if "export" in config and not isinstance(config["export"], dict):
        raise ConfigError("export must be a mapping.")
    if "checkpoint" in config:
        unknown_checkpoint = set(config["checkpoint"]) - {"require_human_confirmation"}
        if unknown_checkpoint:
            raise ConfigError(f"Unsupported checkpoint setting(s): {', '.join(sorted(unknown_checkpoint))}.")
        if "require_human_confirmation" in config["checkpoint"] and config["checkpoint"]["require_human_confirmation"] is not True:
            raise ConfigError("checkpoint.require_human_confirmation cannot disable human confirmation.")
    if "export" in config:
        unknown_export = set(config["export"]) - {"formats"}
        if unknown_export:
            raise ConfigError(f"Unsupported export setting(s): {', '.join(sorted(unknown_export))}.")
    if "export" in config and "formats" in config["export"]:
        formats = config["export"]["formats"]
        if not isinstance(formats, list) or not all(isinstance(item, str) for item in formats):
            raise ConfigError("export.formats must be a list of format names.")
        normalized: list[str] = []
        for item in formats:
            value = item.strip().lower()
            if value not in SUPPORTED_EXPORT_FORMATS:
                raise ConfigError(
                    "export.formats supports only html, pdf, pptx, and notes."
                )
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ConfigError("export.formats must include at least one supported format.")
        config["export"]["formats"] = normalized
    return config


def load_project_config(path: Path | None) -> dict[str, Any]:
    """Load an optional versioned project configuration without changing global state."""
    candidate = Path.cwd() / "scholar-slides.yaml" if path is None else Path(path)
    if not candidate.is_file():
        if path is None:
            return {}
        raise ConfigError(f"Configuration file does not exist: {candidate}")
    try:
        parsed = _parse_yaml_like(candidate.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read configuration: {candidate}: {exc}") from exc
    return _validate_config(parsed)


def resolve_options(cli: Mapping[str, Any], project: Mapping[str, Any], defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Merge option layers with CLI values taking precedence over project and defaults."""
    resolved: dict[str, Any] = {}
    for key in set(defaults) | set(project) | set(cli):
        if cli.get(key) is not None:
            resolved[key] = cli[key]
        elif project.get(key) is not None:
            resolved[key] = project[key]
        elif key in defaults:
            resolved[key] = defaults[key]
    return resolved


def _command_output(command: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> str | None:
    try:
        result = subprocess.run(command, cwd=cwd or ROOT, env=env, shell=False, check=False, capture_output=True, text=True)
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def _command_works(command: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None) -> bool:
    return _command_output(command, cwd=cwd, env=env) is not None


def _installed_runtime_checks() -> list[DoctorCheck]:
    """Checks that only apply to the version-local release runtime.

    The development checkout intentionally keeps the older portable diagnostics;
    an installed app is recognizable by its ``versions/<version>/app`` layout.
    """
    if ROOT.name != "app" or ROOT.parent.parent.name != "versions":
        return []
    version_dir = ROOT.parent
    runtime_root = version_dir.parent.parent
    checks: list[DoctorCheck] = []
    version_file = ROOT / "VERSION"
    installed_version = version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
    checks.append(
        DoctorCheck("installed version", "PASS", f"Runtime version {installed_version or 'unknown'} is present.", "")
        if installed_version
        else DoctorCheck("installed version", "FAIL", "The version-local VERSION file is missing.", "Reinstall from a verified release archive.")
    )
    manifest_path = version_dir / "install-manifest.json"
    manifest = {}
    try:
        manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    checks.append(
        DoctorCheck("install manifest", "PASS", "The version-local install manifest is readable.", "")
        if manifest.get("kind") == "scholar-slides-install-manifest"
        else DoctorCheck("install manifest", "FAIL", "The version-local install manifest is missing or malformed.", "Reinstall from a verified release archive.")
    )
    release_manifest = {}
    try:
        release_manifest = __import__("json").loads((version_dir / "release-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    checks.append(
        DoctorCheck("release manifest", "PASS", "The installed release manifest is readable and versioned.", "")
        if release_manifest.get("kind") == "scholar-slides-release-manifest" and release_manifest.get("version") == installed_version
        else DoctorCheck("release manifest", "FAIL", "The installed release manifest is missing or does not match the runtime version.", "Reinstall from a verified release archive.")
    )
    current = runtime_root / "current"
    current_ok = current.is_symlink() and current.resolve() == version_dir.resolve()
    checks.append(
        DoctorCheck("runtime current", "PASS", "The current runtime pointer selects this version.", "")
        if current_ok
        else DoctorCheck("runtime current", "FAIL", "The current runtime pointer does not select this version.", "Repair the installation or run the rollback workflow.")
    )
    skills_root = os.environ.get("SCHOLAR_SLIDES_CODEX_SKILLS_DIR")
    candidates = [Path(skills_root)] if skills_root else [Path.home() / ".codex" / "skills", Path.home() / ".agents" / "skills"]
    skill = next((candidate / "scholar-slides" for candidate in candidates if (candidate / "scholar-slides").is_dir()), None)
    if skill is None:
        checks.append(DoctorCheck("Codex Skill", "FAIL", "The installed scholar-slides Skill was not found.", "Install the Skill into the discovered Codex Skills root and restart the Codex session."))
    else:
        skill_text = (skill / "SKILL.md").read_text(encoding="utf-8", errors="replace") if (skill / "SKILL.md").is_file() else ""
        frontmatter = skill_text.startswith("---\n") and "name: scholar-slides" in skill_text[:1000] and "description:" in skill_text[:1000]
        links = re.findall(r"`(references/[^`]+)`", skill_text)
        references_ok = bool(links) and all((skill / link).is_file() for link in links)
        forbidden = any(token in skill_text.casefold() for token in ("/home/", ".superpowers/", "node_modules/", ".venv/"))
        skill_version = (skill / "VERSION").read_text(encoding="utf-8").strip() if (skill / "VERSION").is_file() else ""
        skill_ok = frontmatter and references_ok and skill_version == installed_version and not forbidden
        checks.append(
            DoctorCheck("Codex Skill", "PASS", f"Skill {skill} is installed and discoverable.", "")
            if skill_ok
            else DoctorCheck("Codex Skill", "FAIL", "Skill frontmatter, references, version, or path-safety checks failed.", "Reinstall the Skill atomically and restart the Codex session.")
        )
    node_modules = ROOT / "node_modules"
    checks.append(
        DoctorCheck("Node modules", "PASS", "Version-local Node modules are present.", "")
        if node_modules.is_dir()
        else DoctorCheck("Node modules", "FAIL", "Version-local node_modules is missing.", "Rerun the release installer so npm ci can complete.")
    )
    launcher = shutil.which("scholar-slides")
    if launcher:
        checks.append(DoctorCheck("launcher", "PASS", f"Launcher resolved to {launcher}.", ""))
    else:
        checks.append(DoctorCheck("launcher", "WARN", "The launcher directory is not on PATH.", f"Add {manifest.get('bin_dir', runtime_root)} to PATH manually, then restart the Codex session."))
    return checks


def _check_write_access() -> DoctorCheck:
    try:
        with tempfile.NamedTemporaryFile(dir=ROOT, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return DoctorCheck("write access", "FAIL", f"Cannot write to skill directory: {exc}", "Fix directory permissions or install into a writable location.")
    return DoctorCheck("write access", "PASS", "Skill directory is writable.", "")


def doctor() -> list[DoctorCheck]:
    """Return portable diagnostics. This function only observes the environment."""
    checks: list[DoctorCheck] = []
    checks.append(DoctorCheck("Python", "PASS", f"Python {sys.version_info.major}.{sys.version_info.minor} is available.", "") if sys.version_info >= (3, 11) else DoctorCheck("Python", "FAIL", "Python 3.11+ is required.", "Install Python 3.11+ and rerun the platform installer."))
    node = shutil.which("node")
    npm = shutil.which("npm")
    node_version = _command_output([node, "--version"]) if node else None
    node_match = re.match(r"v?(\d+)(?:\.\d+){1,2}$", node_version.strip()) if node_version else None
    if node and npm and node_match and int(node_match.group(1)) >= 18 and _command_works([npm, "--version"]):
        checks.append(DoctorCheck("Node/npm", "PASS", "Node and npm are available.", ""))
    else:
        checks.append(DoctorCheck("Node/npm", "FAIL", "Node and/or npm is missing or unusable.", "Install Node.js 18+ and npm, then rerun the installer."))
    browser_env = os.environ.copy()
    installed_runtime = ROOT.name == "app" and ROOT.parent.parent.name == "versions"
    if installed_runtime:
        installed_manifest = {}
        try:
            installed_manifest = __import__("json").loads((ROOT.parent / "install-manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        browser_env["PLAYWRIGHT_BROWSERS_PATH"] = str(installed_manifest.get("browser_cache") or (ROOT.parent.parent.parent / "cache" / "ms-playwright"))
    if os.name == "nt" and not browser_env.get("SCHOLAR_SLIDES_CHROMIUM_EXECUTABLE"):
        candidates = [
            Path(browser_env[key]) / vendor / product / "Application" / executable
            for key, vendor, product, executable in (
                ("ProgramFiles", "Google", "Chrome", "chrome.exe"),
                ("ProgramFiles(x86)", "Google", "Chrome", "chrome.exe"),
                ("LOCALAPPDATA", "Google", "Chrome", "chrome.exe"),
                ("ProgramFiles", "Microsoft", "Edge", "msedge.exe"),
                ("ProgramFiles(x86)", "Microsoft", "Edge", "msedge.exe"),
            )
            if browser_env.get(key)
        ]
        discovered = next((candidate for candidate in candidates if candidate.is_file()), None)
        if discovered:
            browser_env["SCHOLAR_SLIDES_CHROMIUM_EXECUTABLE"] = str(discovered)
    browser_script = "const {chromium}=require('playwright');const p=process.env.SCHOLAR_SLIDES_CHROMIUM_EXECUTABLE;const o={headless:true,...(p?{executablePath:p}:{})};chromium.launch(o).then(b=>b.close()).then(()=>process.exit(0)).catch(()=>process.exit(1))"
    chromium_ready = bool(node) and _command_works([node, "-e", browser_script], cwd=ROOT, env=browser_env)
    checks.append(DoctorCheck("Playwright Chromium", "PASS", "Playwright Chromium is installed.", "") if chromium_ready else DoctorCheck("Playwright Chromium", "FAIL", "Playwright Chromium is unavailable.", "Run: npx playwright install chromium. On Linux/WSL, install required system libraries too."))
    fc_list = shutil.which("fc-list")
    cjk_output = _command_output([fc_list, ":lang=zh", "family"]) if fc_list else None
    cjk_ready = bool(cjk_output and cjk_output.strip())
    checks.append(DoctorCheck("CJK font", "PASS", "A CJK-capable font is available.", "") if cjk_ready else DoctorCheck("CJK font", "WARN", "No CJK-capable font was detected.", "Install a CJK font before exporting Chinese PDF/PPTX output."))
    modules = ("fitz", "PIL", "pptx")
    missing = [module for module in modules if importlib.util.find_spec(module) is None]
    checks.append(DoctorCheck("PDF/PPTX dependencies", "PASS", "PDF and PPTX Python dependencies are importable.", "") if not missing else DoctorCheck("PDF/PPTX dependencies", "FAIL", f"Missing Python dependencies: {', '.join(missing)}.", "Run the platform installer to install requirements.txt."))
    checks.append(_check_write_access())
    provider_keys = ("ZOTERO_API_KEY", "CROSSREF_MAILTO")
    if any(os.environ.get(key) for key in provider_keys):
        checks.append(DoctorCheck("optional providers", "PASS", "An optional citation-provider setting is available.", ""))
    else:
        checks.append(DoctorCheck("optional providers", "WARN", "No optional citation provider is configured.", "Configure Zotero or another provider only if you want citation enrichment."))
    if os.environ.get("SCHOLAR_SLIDES_CONTACT_EMAIL"):
        checks.append(DoctorCheck("SCHOLAR_SLIDES_CONTACT_EMAIL", "PASS", "Contact email is configured.", ""))
    else:
        checks.append(DoctorCheck("SCHOLAR_SLIDES_CONTACT_EMAIL", "WARN", "Contact email is not configured.", "Set SCHOLAR_SLIDES_CONTACT_EMAIL for external metadata-provider etiquette."))
    checks.extend(_installed_runtime_checks())
    return checks
