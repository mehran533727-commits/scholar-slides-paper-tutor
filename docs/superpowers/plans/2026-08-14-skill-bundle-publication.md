# Scholar-Slides + Paper-Tutor Skill Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, validate, and publish a reproducibly installable public monorepo containing the complete Scholar-Slides 0.3.0 source skill and the complete Paper-Tutor skill, with clear integration and maintenance documentation.

**Architecture:** Keep both skills as independent directories under `skills/`, add repository-level documentation and deterministic PowerShell/Python validation tooling, and reconstruct rather than vendor Python/Node dependency directories. Preserve the one-way evidence flow from matching Scholar-Slides artifacts into Paper-Tutor and keep human checkpoint approval outside all automation.

**Tech Stack:** Markdown, PowerShell 7/Windows PowerShell 5.1-compatible scripts, Python 3.11+ standard library, Node.js 18+, npm lockfile, GitHub Actions on `windows-latest`, GitHub CLI.

## Global Constraints

- Publish to the approved repository `mehran533727-commits/scholar-slides-paper-tutor` with visibility `public` and default branch `main`.
- Preserve Scholar-Slides version `0.3.0 Final Stable` and its existing MIT license with `Copyright (c) 2026 louwill` verbatim.
- Do not assign a new license to Paper-Tutor; document its absence of a separate license explicitly.
- Include maintained source, references, schemas, templates, launchers, lockfiles, and UI metadata.
- Exclude `.venv`, `node_modules`, `__pycache__`, `.pyc`, installed production `manifest.json`, papers, projects, checkpoints, and generated presentations.
- Keep Scholar-Slides and Paper-Tutor independently installable.
- Keep integration data flow one-way: matching paper/source → Scholar-Slides artifacts → read-only Paper-Tutor explanation.
- Never automate, infer, or record CKPT-1 or CKPT-2 human approval.
- Require Python 3.11+ and Node.js 18+ for Scholar-Slides runtime installation.
- Before remote writes, require `gh api user --jq .login` to equal `mehran533727-commits`; fail closed on account mismatch.

---

## File Map

- `skills/scholar-slides/`: exact maintained Scholar-Slides files copied from the installed 0.3.0 skill, excluding generated runtime dependencies and installed manifest.
- `skills/paper-tutor/`: exact Paper-Tutor skill, references, and `agents/openai.yaml` copied from the installed skill.
- `scripts/verify_package.py`: deterministic package validator used locally and in CI.
- `scripts/verify-package.ps1`: PowerShell entrypoint for the validator and source compilation checks.
- `scripts/install.ps1`: guarded Windows installer for both skills and Scholar-Slides dependencies.
- `scripts/uninstall.ps1`: narrow, confirmation-gated uninstaller for the two known skill directories.
- `tests/test_verify_package.py`: negative and positive tests for repository validation rules.
- `tests/test_install_scripts.py`: static and isolated-copy tests for installer safety behavior.
- `.github/workflows/validate.yml`: Windows CI for package validation and CLI smoke tests.
- `README.md`, `README.zh-CN.md`: English and Chinese entrypoints.
- `docs/*.md`: installation, integration, architecture, evidence safety, development, and release guidance.
- `LICENSES.md`, `NOTICE`, `SECURITY.md`, `CONTRIBUTING.md`: legal, attribution, security, and contribution policies.

---

### Task 1: Capture the packaging baseline and verifier tests

**Files:**
- Create: `tests/test_verify_package.py`
- Create: `tests/test_install_scripts.py`

**Interfaces:**
- Consumes: approved design at `docs/superpowers/specs/2026-08-14-skill-bundle-publication-design.md`.
- Produces: executable `unittest` contracts for `scripts.verify_package.validate_repository(root: Path) -> list[str]` and isolated installer safety checks.

- [ ] **Step 1: Write failing validator tests**

Create fixtures in a temporary directory and assert the required failures and a minimal success case:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.verify_package import validate_repository


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
            for name in ("scholar-slides", "paper-tutor"):
                skill = root / "skills" / name
                skill.mkdir(parents=True)
                skill.joinpath("SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: Use when testing {name}.\n---\n",
                    encoding="utf-8",
                )
            self.assertEqual(validate_repository(root, minimal=True), [])
```

- [ ] **Step 2: Write failing installer safety tests**

Assert that both scripts exist, use explicit skill names, and contain no recursive operation against the skills root:

```python
class InstallScriptSafetyTests(unittest.TestCase):
    def test_uninstaller_targets_only_known_skill_directories(self):
        text = Path("scripts/uninstall.ps1").read_text(encoding="utf-8")
        self.assertIn("scholar-slides", text)
        self.assertIn("paper-tutor", text)
        self.assertNotIn("Remove-Item -LiteralPath $DestinationRoot -Recurse", text)
```

- [ ] **Step 3: Run the tests and verify RED**

Run:

```powershell
python -m unittest discover -s tests -v
```

Expected: import/file-not-found failures because the verifier and installer scripts do not exist.

- [ ] **Step 4: Record the baseline failure output**

Save the exact command and failure summary in the implementation notes section of the final verification report; do not commit generated logs.

- [ ] **Step 5: Commit the tests**

```powershell
git add tests/test_verify_package.py tests/test_install_scripts.py
git commit -m "test: define skill bundle packaging contracts"
```

---

### Task 2: Assemble the two clean skill source directories

**Files:**
- Create: `skills/scholar-slides/**`
- Create: `skills/paper-tutor/**`
- Create: `.gitignore`
- Create: `.gitattributes`

**Interfaces:**
- Consumes: installed sources at `C:/Users/16595/.agents/skills/scholar-slides` and `C:/Users/16595/.codex/skills/paper-tutor`.
- Produces: source-only directories consumed by installer, validator, docs, and CI.

- [ ] **Step 1: Copy Scholar-Slides maintained files**

Copy `SKILL.md`, `LICENSE`, `VERSION`, `bin/`, `references/`, `schemas/`, and the maintained `runtime/` files. Exclude `runtime/.venv`, `runtime/node_modules`, every `__pycache__`, `.pyc`, and root `manifest.json`.

- [ ] **Step 2: Copy Paper-Tutor maintained files**

Copy `SKILL.md`, `agents/openai.yaml`, and all four reference documents without rewriting their evidence contracts.

- [ ] **Step 3: Add repository ignore and line-ending rules**

Use these effective rules:

```gitignore
**/.venv/
**/node_modules/
**/__pycache__/
**/*.py[cod]
*.pdf
*.pptx
*.docx
*.zip
.DS_Store
Thumbs.db
```

```gitattributes
* text=auto
*.ps1 text eol=crlf
*.cmd text eol=crlf
*.py text eol=lf
*.mjs text eol=lf
*.md text eol=lf
*.json text eol=lf
*.css text eol=lf
```

- [ ] **Step 4: Verify the source inventory**

Run a file count and size summary excluding `.git`; confirm both `SKILL.md` files, 72 Scholar-Slides runtime scripts, 8 schemas, 6 template assets, 5 Scholar-Slides references, and 4 Paper-Tutor references are present.

- [ ] **Step 5: Commit the source mirror**

```powershell
git add .gitignore .gitattributes skills
git commit -m "feat: add scholar slides and paper tutor skills"
```

---

### Task 3: Implement deterministic package validation

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/verify_package.py`
- Create: `scripts/verify-package.ps1`
- Modify: `tests/test_verify_package.py`
- Modify: `tests/test_install_scripts.py`

**Interfaces:**
- Consumes: repository root and the file rules from Task 2.
- Produces: `validate_repository(root: Path, minimal: bool = False) -> list[str]`; command exits 0 on success and 1 with one error per line on failure.

- [ ] **Step 1: Implement the minimum Python validator**

Validate required files, skill frontmatter (`name`, `description`, `Use when`), Paper-Tutor UI metadata, forbidden directory/file names, JSON parseability, relative Markdown link targets, version equality, and sensitive absolute path patterns. Exclude `.git` and the historical design/plan documents from private-path scanning because they intentionally record local implementation locations.

- [ ] **Step 2: Add the PowerShell wrapper**

The wrapper resolves the repository root, runs the Python validator, then runs:

```powershell
python -m compileall -q "skills/scholar-slides/runtime/scripts"
Get-ChildItem -Recurse -Filter *.ps1 | ForEach-Object {
  [void][System.Management.Automation.Language.Parser]::ParseFile(
    $_.FullName, [ref]$null, [ref]$parseErrors
  )
}
```

It must propagate a nonzero exit code and print a concise summary.

- [ ] **Step 3: Extend tests for broken links, malformed JSON, version drift, and Paper-Tutor metadata drift**

Each test creates one bad fixture and asserts the specific diagnostic category.

- [ ] **Step 4: Run tests and verify GREEN**

```powershell
python -m unittest discover -s tests -v
pwsh -NoProfile -File scripts/verify-package.ps1
```

Expected: all unit tests pass; repository validation reports no errors after later documentation tasks are present. During this task, allow only expected missing-document diagnostics and keep them visible.

- [ ] **Step 5: Commit the validator**

```powershell
git add scripts tests
git commit -m "feat: add deterministic package validation"
```

---

### Task 4: Implement guarded installation and uninstallation

**Files:**
- Create: `scripts/install.ps1`
- Create: `scripts/uninstall.ps1`
- Modify: `tests/test_install_scripts.py`

**Interfaces:**
- Consumes: `skills/scholar-slides`, `skills/paper-tutor`, Python 3.11+, Node.js 18+, and npm.
- Produces: `install.ps1 -DestinationRoot C:/Users/example/.agents/skills [-Force] [-SkipDependencies] [-SkipBrowser] [-AddToPath]`; `uninstall.ps1 -DestinationRoot C:/Users/example/.agents/skills -ConfirmRemoval`.

- [ ] **Step 1: Expand RED tests for safe isolated install behavior**

Use a temporary destination and call:

```powershell
pwsh -NoProfile -File scripts/install.ps1 `
  -DestinationRoot $temporaryRoot -SkipDependencies
```

Assert both skill directories are copied, a second run fails without `-Force`, and a forced run creates a timestamped backup outside the active target.

- [ ] **Step 2: Implement `install.ps1`**

Use `[CmdletBinding(SupportsShouldProcess)]`, resolved absolute source/destination paths, explicit target names, version checks, backup-before-replace, and checked external process exit codes. Never remove or overwrite the whole destination root.

- [ ] **Step 3: Implement dependency provisioning**

Create `skills/scholar-slides/runtime/.venv`, install `requirements-runtime.txt`, run `npm ci` from the runtime directory, optionally run `npx playwright install chromium`, and verify the version-local PowerShell launcher. `-SkipDependencies` supports copy-only tests and advanced manual installation.

- [ ] **Step 4: Implement `uninstall.ps1`**

Require `-ConfirmRemoval`, resolve exactly `scholar-slides` and `paper-tutor`, reject targets that escape `DestinationRoot`, and remove only those two paths. Remove a PATH entry only when it exactly equals the installed Scholar-Slides `bin` directory and the user explicitly requests it.

- [ ] **Step 5: Run RED/GREEN safety tests**

```powershell
python -m unittest tests.test_install_scripts -v
```

Expected: isolated copy, refusal, backup, and narrow uninstall cases all pass without installing dependencies into the repository.

- [ ] **Step 6: Commit installer scripts**

```powershell
git add scripts/install.ps1 scripts/uninstall.ps1 tests/test_install_scripts.py
git commit -m "feat: add guarded Windows installation scripts"
```

---

### Task 5: Write repository and integration documentation

**Files:**
- Create: `README.md`
- Create: `README.zh-CN.md`
- Create: `docs/INSTALLATION.md`
- Create: `docs/INTEGRATION_GUIDE.md`
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/EVIDENCE_SAFETY.md`
- Create: `docs/DEVELOPMENT.md`
- Create: `docs/RELEASE_CHECKLIST.md`
- Create: `LICENSES.md`
- Create: `NOTICE`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: actual scripts and skill contracts from Tasks 2–4.
- Produces: public installation, usage, integration, evidence, maintenance, licensing, and contribution contracts.

- [ ] **Step 1: Run a documentation baseline scenario without the integration guide**

Use an independent fresh-context agent with only the two original skill directories and this request: “Explain exactly when to use Scholar-Slides alone, Paper-Tutor standalone, or both together; include data-flow direction and checkpoint rules.” Record omissions and source confusion without sharing the intended answer.

- [ ] **Step 2: Write the English and Chinese README entrypoints**

Both entrypoints must cover project purpose, capability comparison, source-only completeness, requirements, quick install, three minimal prompts, document index, license status, and restart/doctor verification.

- [ ] **Step 3: Write `docs/INTEGRATION_GUIDE.md`**

Define three routes:

```text
Evidence-grounded reading: paper → Scholar-Slides Mode A → explicit CKPT-1 → stop
Teaching: matching artifacts → Paper-Tutor Integrated Mode → explanation only
Presentation: paper → Scholar-Slides Mode A/CKPT-1 → Mode B/CKPT-2 → export
```

Include a decision table, paper-identity match rules, Integrated/Standalone status blocks, evidence precedence, one-way boundary, conflicts, and copyable quick/deep/research prompts.

- [ ] **Step 4: Write the remaining focused documents**

Keep each document responsible for the exact scope defined in the approved design. Commands must match the installed scripts and Scholar-Slides CLI help; do not invent unsupported platforms or flags.

- [ ] **Step 5: Forward-test the written guidance**

Run the same independent scenario with the repository skill/docs available, plus representative Paper-Tutor validation scenarios 1, 2, 8, 11, and 12. Require correct mode disclosure, evidence precedence, exact unavailable values, no reverse contamination, and the full-document appendix contract.

- [ ] **Step 6: Run link and sensitive-information validation**

```powershell
python scripts/verify_package.py .
```

Expected: no broken relative links, placeholders, machine paths, or license misclassification.

- [ ] **Step 7: Commit public documentation**

```powershell
git add README.md README.zh-CN.md docs LICENSES.md NOTICE SECURITY.md CONTRIBUTING.md
git commit -m "docs: explain installation and combined paper workflow"
```

---

### Task 6: Add continuous validation

**Files:**
- Create: `.github/workflows/validate.yml`

**Interfaces:**
- Consumes: validator, unit tests, dependency manifests, and Scholar-Slides launcher.
- Produces: Windows GitHub Actions job `validate` on pushes and pull requests.

- [ ] **Step 1: Write the workflow**

Use `actions/checkout`, `actions/setup-python` with Python 3.13, and `actions/setup-node` with Node 22 and npm cache bound to `skills/scholar-slides/runtime/package-lock.json`. Run unit tests, package validation, Python compile, `pip install -r requirements-runtime.txt`, `npm ci`, and `bin/scholar-slides.ps1 --version`.

- [ ] **Step 2: Validate the YAML and commands locally**

Parse the workflow as YAML using an available parser or a minimal safe parser check, then run every shell command locally from the same working directories. Do not install Playwright Chromium in CI solely for the version smoke test.

- [ ] **Step 3: Commit CI**

```powershell
git add .github/workflows/validate.yml
git commit -m "ci: validate skill package on Windows"
```

---

### Task 7: Run clean-install and release verification

**Files:**
- Modify only files required by observed verification failures.

**Interfaces:**
- Consumes: complete local repository.
- Produces: fresh evidence that repository structure, installation, runtime, docs, and security requirements pass.

- [ ] **Step 1: Run the full local unit and package suite**

```powershell
python -m unittest discover -s tests -v
pwsh -NoProfile -File scripts/verify-package.ps1
```

- [ ] **Step 2: Test a clean install in an explicit temporary directory**

Create a temporary directory, run `scripts/install.ps1` with dependencies and `-SkipBrowser`, call the installed launcher with `--version` and `doctor --json`, parse JSON, then delete only the verified temporary directory.

- [ ] **Step 3: Validate Node and Python dependency reconstruction**

Confirm `npm ci` succeeds from the clean copied runtime and the version-local Python environment imports every package in `requirements-runtime.txt`.

- [ ] **Step 4: Run final repository safety scans**

Check tracked files for forbidden directories, binaries, secrets, email addresses not intentionally present in Git metadata, absolute user paths, generated artifacts, and files over GitHub's 100 MB limit.

- [ ] **Step 5: Review the complete diff and commit any evidence-driven fixes**

Use `git diff --check`, `git status -sb`, file inventory, and commit history. Make no speculative cleanup.

---

### Task 8: Create and publish the GitHub repository

**Files:**
- No new repository files unless remote verification reveals a real documentation defect.

**Interfaces:**
- Consumes: verified local `main`, authenticated GitHub CLI account `mehran533727-commits`.
- Produces: public GitHub repository with matching `main` HEAD.

- [ ] **Step 1: Verify account, name availability, and clean scope**

```powershell
gh api user --jq .login
gh repo view mehran533727-commits/scholar-slides-paper-tutor
git status -sb
```

Expected: login exactly `mehran533727-commits`; repository absent before creation; clean local worktree.

- [ ] **Step 2: Create and push the public repository**

```powershell
gh repo create mehran533727-commits/scholar-slides-paper-tutor `
  --public --source . --remote origin --push `
  --description "Evidence-grounded academic paper analysis, tutoring, and slide workflows for Codex."
```

- [ ] **Step 3: Verify local/remote parity**

Fetch the remote, compare `git rev-parse HEAD` with `git rev-parse origin/main`, and confirm the default branch and public visibility through `gh repo view --json url,visibility,defaultBranchRef`.

- [ ] **Step 4: Verify through the GitHub connector**

Read repository metadata, `README.md`, `README.zh-CN.md`, both `SKILL.md` files, and workflow status through the connected GitHub plugin. Confirm the connector sees the same owner/repository and final commit.

- [ ] **Step 5: Deliver the release URL and verification evidence**

Report the repository URL, final commit SHA, source inventory, commands executed, test counts, CI state, and any remaining environment warning without implying unverified success.
