# Workflow

`scholar-slides` separates source understanding, a Codex-prepared CKPT-1 candidate, explicit human confirmation, visual review, and final export.

```text
doctor -> build/ingest -> automatic extractive digest -> Codex prepares candidate -> prepare-checkpoint -> pending_human_confirmation -> explicit human approve -> deck/review -> explicit CKPT-2 approval -> export
```

## Mode A - Paper Understanding Only

Use Mode A for paper explanation, structured reading, contribution/method/result extraction, or a reviewed digest without slides. Stop after the user explicitly approves CKPT-1.

Local PDFs, Zotero-exported local PDFs, arXiv IDs/URLs, and existing projects are supported. Only-digest mode is extractive input, not proof of a PDF or source-bound evidence; stop before any deck path.

## Mode B - Academic Presentation

Use Mode B for a presentation, journal club, lab meeting, thesis talk, editable PPTX, PDF, HTML, or speaker notes. It includes Mode A, then generates a deck with visible quantitative coverage, semantic/visual QA, CKPT-2 review, explicit CKPT-2 approval, and export.

Required facts cannot be silently dropped to meet a slide budget. Speaker notes never count as visible coverage.

## 1. Environment and source

Run `scholar-slides doctor --json` first. It checks the installed runtime, browser, fonts, optional providers, contact configuration, and writable paths. Resolve `FAIL`; report `WARN` as a limitation.

The source modes are deliberately narrow:

- A local PDF is read from the path passed to `build --input` or `ingest --input`.
- An arXiv ID or URL is a locator; the downloaded PDF becomes the evidence source.
- A Zotero workflow may export a managed local PDF. Zotero remains responsible for collection, tagging, and citation; the Skill does not edit the database.
- In only-digest mode, an existing `digest.json` is inspectable extractive input, not proof of a PDF, source identity, or evidence audit. Keep claims unverified and stop before any source-bound deck path.

## 2. Extractive digest

Extractive metadata is automatic. EXTRACTED_WITH_WARNINGS is a review status, not verified or human-confirmed metadata. Human-confirmed metadata exists only after explicit CKPT-1 approval and records confirmed_by.

A metadata correction overlay retains its extracted original, proposed value, source evidence, and reason; the candidate is not approved content. Every evidence audit needs source-bound page/locator evidence and the canonical source SHA-256. An excluded_from_deck ledger decision removes an asset from selection without verifying it. prepare-checkpoint is not approve, and prepared_by=Codex is only a preparer identity.

`build --input <source> --out <project>` performs source preparation and the automatic extractive digest pass. `ingest --input <source> --out <project>` prepares the source bundle when that lower-level step is needed; run the digest stage before CKPT-1. The digest and its Markdown projection preserve page text, metadata provenance, and visible `[MISSING: ...]` or `[UNVERIFIED: ...]` markers. `EXTRACTED_WITH_WARNINGS` is a review status, not `VERIFIED` and not human confirmation.

## Revising an approved deck (CKPT-2 reopen)

An approved CKPT-2 binds a specific deck. If that deck must change, the approved checkpoint cannot be reused. The official revision flow is:

```text
approved CKPT-2
-> user/agent decides the deck must change
-> scholar-slides reopen --out <project> --checkpoint CKPT-2 --requested-by <actor> --reason <reason>
-> old approval/review/delivery archived immutable under checkpoint-history/CKPT-2/<revision-id>
-> revise/regenerate the deck through the normal content workflow
-> build/review (new semantic + visual QA)
-> new CKPT-2 pending_human_confirmation
-> explicit human approve
-> export
```

`reopen` is not `approve`. It does not revoke or rewrite the historical approval; the old delivery becomes historical evidence. The new deck requires new semantic/visual QA and a new human CKPT-2 approval before export.

## Quantitative coverage in reviewed decks

Confirmed quantitative evidence is mandatory visible content, not optional commentary:

- Every non-empty reviewed key metric (label plus value) is a required visible fact.
- Every numeric reviewed experimental result is a required visible fact.
- An audited two-row comparison can add a native pairwise requirement; endpoints and ratios come only from the bound audit.
- Forbidden table images stay forbidden; audited numbers appear only as native text/cards/charts.
- Speaker notes never satisfy coverage.
- semantic QA blocks CKPT-2 readiness when a required fact is missing or notes-only (`semantic-quantitative-coverage-missing`).
- `coverage-requirements.json` is generated review evidence and is hash-bound into the review manifest.

The digest is the extractive layer. Codex then reads it and source evidence to write `ckpt1-review-input.json`. A candidate can propose metadata corrections, claims, metrics, marker resolutions, and audits, but it must preserve the extracted original and evidence for each correction.

## 3. Prepare CKPT-1

Run the public preparation command with `--checkpoint CKPT-1` and `--prepared-by Codex`. It canonicalizes the candidate, derives the marker ledger and readiness report, validates every audit, and binds the complete artifact bundle. `prepared_by=Codex` is an agent identity only. The resulting checkpoint is:

```text
checkpoint = CKPT-1
status = pending_human_confirmation
approval_status = not_approved
ready_for_human_confirmation = true
human_review_required = true
```

Preparation is not approval. Do not call `approve` until the user explicitly confirms the exact CKPT-1 evidence. A metadata correction remains a candidate overlay until that operation; it must not be silently written into the extractive digest.

## 4. Source-bound evidence

Each generic evidence audit must bind its page or locator, crop region when used, reviewed values, and the actual source PDF SHA-256. The canonical chain is:

```text
SHA-256(actual source PDF) = digest source SHA-256 = checkpoint source identity SHA-256 = audit source SHA-256
```

If a marker is not supported, preserve it. `excluded_from_deck` removes an asset from deck selection without claiming that its value is correct. If a source, digest, crop, audit, or review input changes, the old binding is stale and must not be reused.

## 5. Resume and CKPT-2

After explicit CKPT-1 approval, `scholar-slides build --out <project> --resume` creates the outline and CKPT-2 review bundle. Run `scholar-slides review --project <project>` to regenerate the offline HTML/PNG/montage/QA evidence. A passing review means `ready_for_human_approval`; it is not CKPT-2 approval. The user must explicitly approve CKPT-2 before `export`.

When `prepare-checkpoint --resume` sees the same candidate and unchanged evidence, it performs a validated no-op. A changed candidate receives a new identity and atomically refreshes the pending bundle. A stale source or audit is a hard error: regenerate the affected evidence and rerun preparation. Never hand-edit checkpoint JSON or use resume to bypass a human gate.

After installing or upgrading the Skill, restart Codex for Skill discovery. This Skill restart is required before relying on a newly installed version. Do not assume that a development checkout is the installed Skill.

The public delivery contract uses delivery.mjs for approved CKPT-2 projects. --formats selects output formats only; it must not switch the checkpoint gate or exporter.
