---
name: scholar-slides
description: Use when a user asks to read, explain, digest, review, summarize, or create a presentation from an academic paper, local PDF, Zotero-exported PDF, arXiv paper, or existing scholar-slides project.
---

# scholar-slides

Version: 0.3.0 Final Stable

Use this Skill for source-grounded paper analysis and academic presentations. Never fill a
scientific gap from memory. Keep every project in the user's chosen output directory, never
inside the installed Skill.

## Mode A — 论文分析

Mode A reads the paper, extracts metadata and evidence, builds the reviewed semantic
candidate, discovers quantitative evidence, and stops for CKPT-1 human confirmation.

```powershell
scholar-slides build --input "C:\path\paper.pdf" --project "C:\path\paper-project"
```

The pending project includes `paper-analysis.md`, a user-readable analysis with natural page,
Figure, and Table locators. It may be read before approval, but it is a projection of the
candidate rather than a new fact source.

When a formal review input is required, prepare it without recording approval:

```powershell
scholar-slides prepare-checkpoint --project "C:\path\paper-project" `
  --checkpoint CKPT-1 --review-input "C:\path\review-input.json" --prepared-by Codex
```

Only an explicit user instruction may approve CKPT-1:

```powershell
scholar-slides approve 1 --project "C:\path\paper-project" --confirmed-by "Reviewer Name"
```

Approval freezes the source-bound reviewed semantic view. Never edit checkpoint JSON, infer
approval from silence, or regenerate a confirmed CKPT-1.

## Mode B — 汇报生成

Mode B starts from a confirmed CKPT-1 and generates the narrative plan, visible quantitative
coverage, deck, speaker notes, user preparation documents, and QA evidence:

```powershell
scholar-slides build --project "C:\path\paper-project" --resume
```

It stops at pending CKPT-2. The project contains:

- `deck.json` and `deck-outline.md`;
- `speaker_notes.md` or the project notes artifact;
- `presentation-script.md`, a complete per-slide preparation script;
- `presentation-summary.md`, a five-minute pre-talk summary;
- the review montage and semantic, quantitative, audience, visual, figure-legibility, and
  aesthetics reports.

Generate or refresh only the pending review preview with:

```powershell
scholar-slides review --project "C:\path\paper-project"
```

Only an explicit user instruction may approve the unchanged reviewed deck:

```powershell
scholar-slides approve 2 --project "C:\path\paper-project" --confirmed-by "Reviewer Name"
```

Do not export before CKPT-2 is confirmed. If an approved deck must change, use the documented
reopen lifecycle; never overwrite its approval record.

## Export

After CKPT-2 approval, run the single formal delivery command:

Canonical project placeholder: `scholar-slides export --project <project> --formats html,pdf,pptx,notes`.

```powershell
scholar-slides export --project "C:\path\paper-project" --formats html,pdf,pptx,notes
```

Delivery contains `slides.html`, `slides.pdf`, editable `slides.pptx`, `speaker_notes.md`,
`presentation-script.md`, `presentation-summary.md`, and machine-verifiable manifest,
validation, consistency, and parity evidence.

## Environment check

```powershell
scholar-slides --version
scholar-slides doctor --json
```

Require version `0.3.0` and `doctor.ok = true` before starting a long run. A missing or stale
source, checkpoint binding, required quantitative fact, or blocking QA finding must fail
closed. Do not invent content, bypass a gate, or add a paper-specific exception.

Read only the reference needed for the current stage:

- `references/workflow.md`: sources, Mode A, Mode B, and planning.
- `references/checkpoints.md`: approvals, reopen, and immutable history.
- `references/cli.md`: supported commands and output paths.
- `references/troubleshooting.md`: environment, stale evidence, and resume.
- `references/USAGE_ZH.md`: Chinese guide and copyable commands.
