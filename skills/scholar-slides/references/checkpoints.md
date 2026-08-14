# Human checkpoints

`prepare-checkpoint` creates a candidate checkpoint. `approve` records an explicit human decision. They are separate commands and separate state transitions.

## CKPT-1: prepare, inspect, confirm

The automatic `digest.json` is extractive source evidence. It can report `EXTRACTED_WITH_WARNINGS`, `[MISSING: ...]`, or `[UNVERIFIED: ...]`; none of these is a human confirmation. Codex writes `ckpt1-review-input.json` as a candidate overlay. It may carry `metadata_corrections`, proposed claims and metrics, marker-ledger decisions, and generic evidence audits. A correction must retain its extracted original, proposed value, reason, and source evidence.

Automatic title/authors reported as `NEEDS_REVIEW` (or `MISSING` with a schema-supported replacement) is still eligible for CKPT-1 preparation when the candidate carries a valid correction overlay for that exact field: the correction preserves the automatic original, proposes a nonblank value, carries a page/evidence locator and reason, identifies the agent preparer, and is bound to a current resolved evidence audit for the corresponding digest marker. The extractive digest remains unchanged and keeps reporting `NEEDS_REVIEW` until explicit human approval; only the resolved view after approval exposes corrected metadata.

Prepare the bundle with the current public CLI:

```text
scholar-slides prepare-checkpoint --project <project> --checkpoint CKPT-1 --review-input <project>/ckpt1-review-input.json --prepared-by Codex --json
```

The command validates the candidate, writes `ckpt1-review.json`, `ckpt1-review.md`, `ckpt1-markers.json`, `ckpt1-readiness.json`, and `checkpoint-1.json`, and binds the evidence bundle atomically. The expected state is:

```text
checkpoint = CKPT-1
status = pending_human_confirmation
approval_status = not_approved
ready_for_human_confirmation = true
human_review_required = true
errors = 0
```

`prepared_by=Codex` is not `confirmed_by`. A candidate is not approved content. Do not call `approve` or edit a checkpoint file while the state is pending. The user must inspect the digest, candidate overlays, flags, marker ledger, and audits and explicitly confirm CKPT-1. Only then is this command valid:

```text
scholar-slides approve 1 --out <project> --confirmed-by <name> --json
```

The resolved CKPT-1 view can expose corrected metadata and approved candidate content only after that confirmation. `prepare-checkpoint --resume` validates the existing bundle and is a no-op only when the candidate, source, and bound evidence are unchanged. A changed candidate gets a new identity; a stale source or audit must be regenerated. Interruption-safe temporary writes must not be mistaken for approval.

## Evidence audits and source identity

An audit is generic, source-bound evidence, not a paper-specific exception. It identifies a source page or locator, crop region when applicable, reviewed values, preparer, and current status. If an asset is marked `excluded_from_deck`, it is forbidden from deck selection but remains visibly unresolved; exclusion is not verification.

Every source-bound audit must agree with the actual source PDF bytes:

```text
SHA-256(actual source PDF) = digest source SHA-256 = checkpoint source identity SHA-256 = audit source SHA-256
```

If any source, digest, review candidate, marker ledger, crop, audit, or readiness bytes change, the old binding is stale. Rerun the affected extraction or review step and prepare a new bundle. Never hand-edit JSON to repair a hash.

## CKPT-2: review, then confirm

After CKPT-1 is explicitly confirmed, resume the same project to create the outline and CKPT-2 record. `scholar-slides review --project <project>` creates the offline visual review artifacts and QA report. Show the outline, action titles, selected assets, montage, and QA report. A passing report means `ready_for_human_approval`; it does not approve CKPT-2.

Only after the user explicitly approves the unchanged CKPT-2 evidence may the operator run:

```text
scholar-slides approve 2 --out <project> --confirmed-by <name> --json
scholar-slides export --project <project> --formats html,pdf,pptx,notes
```

Never infer either approval from silence, `prepared_by`, `confirmed_by` text embedded in a candidate, or a readiness report. Never create a deck, review montage, CKPT-2, or delivery artifact before its preceding explicit gate.

## Revising an approved CKPT-2

An approved CKPT-2 is immutable evidence for its bound deck. When the deck changes after approval, use the official reopen lifecycle:

```text
scholar-slides reopen --out <project> --checkpoint CKPT-2 --requested-by <actor> --reason <reason>
```

Reopen atomically archives the approved checkpoint bytes, the bound deck/outline/asset graph, the sealed review bundle, and the old delivery (when present) under `checkpoint-history/CKPT-2/<revision-id>` with a hash-bound `history-manifest.json`, then retires the current approval slot. The historical approval is preserved read-only; the project is reopened and ready for a revised deck. The new CKPT-2 links to the superseded revision through its `supersedes` lineage block, and export remains blocked until a new explicit human approval. `reopen` never writes approval fields and never rewrites CKPT-1.

## Quantitative coverage gate

The reviewed deck must show every confirmed quantitative fact visibly. `reviewed_key_metrics` are first-class visible requirements, numeric `reviewed_experimental_results` are coverage requirements, and a valid `audit_ref` may support native pairwise text/cards/charts. Forbidden table images remain forbidden; audited numbers are shown only as native content. Notes-only facts do not satisfy coverage. `coverage-requirements.json` is generated review evidence and is bound into the review manifest; tampering it makes the review stale and blocks readiness. Semantic QA reports `semantic-quantitative-coverage-missing` until every requirement is visible with its context label, so a sealed CKPT-2 review requires `quantitative coverage errors = 0`.
