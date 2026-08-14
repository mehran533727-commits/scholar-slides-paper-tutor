# Scholar-Slides + Paper-Tutor

[中文说明](README.zh-CN.md)

An evidence-grounded Codex skill bundle for reading academic papers, teaching them clearly, and producing reviewed academic presentations.

The bundle keeps two responsibilities separate:

- **Scholar-Slides 0.3.0** reads the source, builds evidence-bound semantics, enforces explicit human checkpoints, and can produce HTML, PDF, PPTX, and speaker notes.
- **Paper-Tutor** turns matching evidence into adaptive explanations while keeping Paper Facts, Tutor Explanation, and Tutor Analysis distinct.

The integration is deliberately one-way:

```text
paper or matching project
        ↓
Scholar-Slides evidence and reviewed semantics
        ↓ read-only
Paper-Tutor teaching explanation
```

Paper-Tutor prose never flows back into Scholar-Slides, a checkpoint, or a presentation.

## Choose the right route

| Goal | Use | Stop or gate |
| --- | --- | --- |
| Understand or audit a paper | Scholar-Slides Mode A | Stop at explicit CKPT-1 approval |
| Ask for a quick, deep, or research-level explanation | Paper-Tutor | Prefer Integrated Mode when matching trusted Scholar-Slides artifacts exist |
| Explain directly from a PDF without reviewed artifacts | Paper-Tutor Standalone Mode | Disclose that it is not CKPT-1 verified |
| Make a journal-club, conference, or thesis deck | Scholar-Slides Mode B | Explicit CKPT-1, then explicit CKPT-2, then export |

See the [complete integration guide](docs/INTEGRATION_GUIDE.md) before combining the skills.

## What “complete source package” means

This repository contains both complete maintained skill trees:

- Skill instructions, references, schemas, launchers, UI metadata, templates, and runtime source;
- Python and Node dependency declarations, including the npm lockfile;
- Installation, uninstallation, validation, CI, and maintenance tooling.

Generated and machine-specific directories are intentionally absent: `.venv`, `node_modules`, `__pycache__`, installed production manifests, paper files, checkpoints, and generated decks. The installer rebuilds dependencies locally. This makes the repository reproducible and reviewable instead of copying a non-portable 200 MB installed snapshot.

## Requirements

- Windows 10 or 11;
- PowerShell 5.1 or PowerShell 7;
- Python 3.11 or newer;
- Node.js 18 or newer with npm;
- Git and Codex.

Scholar-Slides rendering also needs a supported Chrome/Edge/Chromium environment and suitable fonts. `doctor --json` reports the exact environment state.

## Quick install

```powershell
git clone https://github.com/mehran533727-commits/scholar-slides-paper-tutor.git
Set-Location scholar-slides-paper-tutor
.\scripts\install.ps1 -AddToPath
```

The installer refuses to overwrite an existing skill. Use `-Force` only when you want a timestamped backup followed by replacement. See [Installation](docs/INSTALLATION.md) for copy-only, browser-skipping, upgrade, and uninstall options.

Restart Codex and open a new terminal, then verify:

```powershell
scholar-slides --version
scholar-slides doctor --json
```

Expected version: `0.3.0`. Start a long workflow only when `doctor.ok` is `true`.

## Minimal prompts

Evidence-grounded reading only:

```text
Use scholar-slides to read this paper. Explain the problem, method, experiments, and limitations. Stop at CKPT-1; do not create slides.
```

Deep teaching from matching reviewed artifacts:

```text
Use paper-tutor in Integrated Mode with this Scholar-Slides project. Teach the method and decisive experiments at deep depth, keeping Paper Facts, Tutor Explanation, and Tutor Analysis separate.
```

Reviewed presentation:

```text
Use scholar-slides to create a 12-slide journal-club presentation. Keep required quantitative results visible and complete both explicit CKPT-1 and CKPT-2 review gates before export.
```

## Repository layout

```text
skills/scholar-slides/   Full Scholar-Slides skill and runtime source
skills/paper-tutor/      Full Paper-Tutor skill, metadata, and references
scripts/                 Guarded installer, uninstaller, and package validator
tests/                   Repository packaging and installation safety tests
docs/                    User, integration, evidence, architecture, and release docs
```

## Documentation

- [Installation and upgrade](docs/INSTALLATION.md)
- [Using the two skills together](docs/INTEGRATION_GUIDE.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Evidence and checkpoint safety](docs/EVIDENCE_SAFETY.md)
- [Development and verification](docs/DEVELOPMENT.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [Security policy](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Licenses and attribution](LICENSES.md)

The original skill contracts remain available at [Scholar-Slides SKILL.md](skills/scholar-slides/SKILL.md) and [Paper-Tutor SKILL.md](skills/paper-tutor/SKILL.md).

## License status

Scholar-Slides is distributed under its existing MIT license, with `Copyright (c) 2026 louwill` preserved verbatim. Paper-Tutor and the repository-level integration material do not currently carry a separate open-source license. See [LICENSES.md](LICENSES.md) and [NOTICE](NOTICE); do not assume the Scholar-Slides license applies to every file.
