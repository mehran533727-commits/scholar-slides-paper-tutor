# CLI reference

Use `scholar-slides --help` and the command-specific `--help` output as the contract. The installed launcher selects its version-local Python environment; it does not import a development checkout and does not require `PYTHONPATH` or `NODE_PATH`.

## Environment and source

```text
scholar-slides --version
scholar-slides doctor --json
scholar-slides build --input <paper.pdf-or-arxiv-id> --out <project>
scholar-slides ingest --input <paper.pdf-or-arxiv-id> --out <project>
```

`build --input` runs source preparation and the automatic extractive digest pass, then stops at CKPT-1 with `paper-analysis.md`. `ingest` prepares the source bundle for a lower-level workflow. Both accept the same `--input` forms supported by the facade: a local PDF path, an arXiv ID, or an arXiv URL. A Zotero-managed paper must be exported as a local PDF first.

## CKPT-1 preparation and approval

The Skill prepares the candidate input; the CLI canonicalizes and binds it:

```text
scholar-slides prepare-checkpoint --project <project> --checkpoint CKPT-1 --review-input <project>/ckpt1-review-input.json --prepared-by Codex --json
```

Supported preparation flags are `--project`, `--checkpoint CKPT-1`, `--review-input`, `--prepared-by`, `--dry-run`, `--resume`, `--json`, `--verbose`, `--config`, and the shared global flags shown by `--help`. This command ends at `pending_human_confirmation`; it never approves.

After the user explicitly confirms the prepared evidence, approve the existing record:

```text
scholar-slides approve 1 --out <project> --confirmed-by <name> --json
```

The positional checkpoint is `1` or `2`; there is no public third approval gate. `--out` names the project and `--confirmed-by` is required. Although `--attach` appears in the general approval help for compatibility, do not use it for CKPT-1: approval consumes the already prepared bundle. If evidence changes, rerun `prepare-checkpoint`.

## CKPT-2 review and export

```text
scholar-slides build --out <project> --resume
scholar-slides review --project <project>
scholar-slides approve 2 --out <project> --confirmed-by <name> --json
scholar-slides export --project <project> --formats html,pdf,pptx,notes
```

`review` requires a pending CKPT-2 and writes offline review evidence. Mode B also writes `presentation-script.md` and `presentation-summary.md` beside the deck. A passing review is not approval. `export` runs only after the explicit CKPT-2 approval and accepts `--formats html,pdf,pptx,notes`; formal delivery always includes those two preparation documents as companions. Output files stay beside the user project, not in the installed Skill directory.

## Reopen an approved CKPT-2 for a revised deck

```text
scholar-slides reopen --out <project> --checkpoint CKPT-2 --requested-by <actor> --reason <reason> [--dry-run] [--json]
```

`reopen` archives the approved checkpoint, its bound deck/review/delivery evidence, and a hash-bound history manifest under `checkpoint-history/CKPT-2/<revision-id>`, then retires the current approval slot so a revised deck can be regenerated and re-reviewed. It never rewrites approval fields. `--dry-run` reports the planned revision and writes nothing; repeated identical reopen with `--resume` is a safe no-op.

## Quantitative coverage artifact

`build`/deck generation writes `<project>/coverage-requirements.json` after a confirmed CKPT-1. It prioritizes confirmed evidence against the reviewed research question and contributions, then records deterministic core requirements derived from relevant `reviewed_key_metrics`, numeric `reviewed_experimental_results`, valid `audit_ref` comparisons, and source-bound scientific results. Main-text evidence is preferred; appendix evidence stays supporting unless it changes the interpretation through a negative result, trade-off, robustness finding, or no main-text equivalent. The scientific-priority summary records source scope, tier, dimension coverage, and promotion reason, while provenance hashes bind the digest, CKPT-1 record, and audits. `review` binds the artifact into the review manifest, and semantic QA blocks readiness when any required fact is missing from visible slide text.

## Resume and output rules

`--resume` means validate and continue the same project. For `prepare-checkpoint`, an unchanged candidate is a validated no-op, a changed candidate gets a new semantic identity, and a changed source or evidence audit is stale and must be regenerated. Do not edit checkpoint JSON by hand, pass a candidate's `confirmed_by` field as approval, or treat `ready_for_human_approval` as a decision.
