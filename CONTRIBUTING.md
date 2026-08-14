# Contributing

Contributions are welcome when they preserve the evidence and checkpoint safety model.

## Before changing files

1. Read [Architecture](docs/ARCHITECTURE.md), [Evidence Safety](docs/EVIDENCE_SAFETY.md), and [Development](docs/DEVELOPMENT.md).
2. Keep Scholar-Slides and Paper-Tutor independent.
3. Search for an existing helper, schema, reference rule, or validation scenario before adding a new abstraction.
4. For behavior changes, add a failing test or a failing skill scenario before implementation.

## Required invariants

- Never invent scientific facts or silently merge mismatched papers.
- Never infer CKPT-1/CKPT-2 approval.
- Never write Paper-Tutor prose back into Scholar-Slides or a deck.
- Preserve upstream Evidence IDs, uncertainty, and material conflicts.
- Preserve the Scholar-Slides MIT license and attribution.
- Do not add generated dependencies, papers, or user projects.

## Verification

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -s tests -v
pwsh -NoProfile -File scripts/verify-package.ps1
```

For skill behavior changes, also run the relevant cases in [Paper-Tutor validation scenarios](skills/paper-tutor/references/validation-scenarios.md) or the corresponding Scholar-Slides checkpoint/QA workflow.

## Pull requests

Explain what changed, why it is needed, the exact evidence or failing scenario that motivated it, tests run, and remaining limitations. Keep unrelated formatting, dependency upgrades, and refactors out of the same change.
