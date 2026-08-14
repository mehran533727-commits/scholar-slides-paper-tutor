# Troubleshooting

## Doctor and environment

- If `scholar-slides doctor --json` reports `FAIL`, fix the named runtime, package, browser, font, contact, or writable-path issue and rerun it.
- A missing CJK font may be a warning for an English-only deck but blocks a faithful Chinese PDF/PPTX export. Install a CJK-capable font through the WSL distribution's normal package workflow; the installer never invokes `sudo`.
- If Chromium cannot launch, run `npx playwright install-deps chromium` yourself and rerun the installer. Keep the WSL and Windows runtimes, browsers, fonts, and Node dependencies separate.

## CKPT-1 preparation

- `prepare-checkpoint` rejects a missing or malformed review input, an input that does not point to the project's canonical digest, or a candidate containing approval fields. `prepared_by=Codex` is an agent identity, not a human decision.
- If preparation reports a stale source digest, canonical source PDF, marker ledger, crop, evidence audit, project options file, or readiness artifact, treat it as a stale audit/evidence bundle and do not hand-edit the hash. Recreate the changed evidence from the actual source PDF and rerun preparation.
- If the candidate changed, rerun `prepare-checkpoint` with the new input. The new candidate receives a new identity and replaces the pending bundle atomically. `--resume` is a validated continuation; it is a no-op only for an unchanged candidate and unchanged evidence.
- If the process was interrupted, inspect the project for a complete pending bundle and rerun with `--resume`. Temporary files must not be promoted into an approval record.
- A passing readiness report means ready for human confirmation. It does not call `approve`; only an explicit user instruction can authorize `scholar-slides approve 1 --out <project> --confirmed-by <name>`.

## CKPT-2 and export

- If `build --resume` says CKPT-1 is not explicitly confirmed, stop and show the digest and candidate to the user.
- If `review --project <project>` reports stale or missing assets, regenerate the review from the same project and inspect the new offline evidence. Do not reuse a montage or QA report bound to another deck.
- A passing CKPT-2 review is `ready_for_human_approval`, not approval. Export remains blocked until the user explicitly approves CKPT-2.
- If an approved CKPT-2 no longer matches the deck because the deck changed, `export` refuses to run. Do not hand-edit the checkpoint or reuse the old approval binding. Run `scholar-slides reopen --out <project> --checkpoint CKPT-2 --requested-by <actor> --reason <reason>`, regenerate and re-review the deck, obtain a new explicit human CKPT-2 approval, then export. If reopen reports `CKPT-2 supersedes history is missing or tampered`, the archived revision was altered or removed; restore it from a trusted backup before continuing.
- If semantic QA reports `semantic-quantitative-coverage-missing`, a confirmed quantitative fact (a key metric label/value, a numeric result, or an audited pairwise endpoint/ratio) is absent from visible slide text or exists only in speaker notes. Regenerate the deck from the unchanged confirmed CKPT-1 so the planner assigns every required fact, then re-review. Do not hand-edit `coverage-requirements.json`; it is generated from confirmed evidence and must match the confirmed semantic digest at review time.

## Source limitations and Skill discovery

- In only-digest mode, preserve `[MISSING: ...]` and `[UNVERIFIED: ...]`; a digest without the actual source PDF cannot satisfy canonical source SHA or evidence-audit requirements.
- If a source or figure cannot be verified, keep its marker visible rather than filling the gap from memory. `excluded_from_deck` prevents selection; it is not verification.
- After installing or upgrading, restart Codex so Skill discovery reloads the installed Skill. A development checkout and the installed Skill may be different versions; verify with `scholar-slides doctor --json` after the restart.
