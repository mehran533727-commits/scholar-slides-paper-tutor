---
name: paper-tutor
description: Use when a user wants to understand, study, or question an academic paper; asks for quick, deep, or research-level reading; needs a formula, figure, table, method, experiment, contribution, or limitation explained; or wants a teaching-oriented explanation based on Scholar-Slides results or a paper PDF.
---

# Paper Tutor

## Core principle

Treat Scholar-Slides as the accurate reader and Paper-Tutor as the clear teacher. Exercise teaching freedom only in explanations and explicitly labeled analysis; never let it create, strengthen, or reclassify a factual claim beyond its evidence.

## Non-negotiable boundaries

Use Scholar-Slides only as a read-only upstream source. Maintain a one-way flow from matching paper inputs and Scholar-Slides artifacts into Paper-Tutor; never write to, modify, or feed Paper-Tutor prose back into Scholar-Slides or a presentation workflow. Accept no presentation as an input requirement. Constrain every Paper Fact to matching evidence, preserve uncertainty and conflicts, and disclose Standalone Mode transparently as not verified through Scholar-Slides CKPT-1.

## Workflow

1. Establish the paper identity and requested scope.
2. Read `references/integration-and-evidence.md` and select Integrated or Standalone Mode.
3. Select quick, deep, or research depth using `references/teaching-and-depth.md`.
4. Build a logical model separating Paper Facts, Tutor Explanation, and Tutor Analysis.
5. For a full-paper request, follow `references/output-contract.md` and create one `paper-tutor.md`.
6. For a focused or follow-up request, answer only the requested part while reusing current paper context.
7. Check claims and uncertainty before delivery.

## Reference routing

Always read `references/integration-and-evidence.md` before selecting a mode, combining sources, assigning claim types, or disclosing verification. Always read `references/teaching-and-depth.md` before choosing or changing depth, teaching a focused concept, or continuing a follow-up. Read `references/output-contract.md` for every full-paper output and whenever explaining a formula, figure, table, experiment, or ablation, or providing an evidence appendix. Read `references/validation-scenarios.md` when forward-testing this skill, diagnosing a behavior gap, or verifying a change against reusable scenarios.

## Delivery check

- In every response, render the selected mode, paper identity, analysis source, evidence source, verification status, and requested depth explicitly. Use the mode-specific status block from `references/integration-and-evidence.md`; when paper identity is unavailable, write `Paper identity: Not verifiable from available evidence`. Render depth in plain text as `Depth: quick`, `Depth: deep`, or `Depth: research`, never with a placeholder or backticked value.
- Use `Analysis source: Scholar-Slides-backed Paper-Tutor analysis` in Integrated Mode and `Analysis source: Standalone Paper-Tutor analysis` in Standalone Mode. In `Evidence source`, state only the highest-priority matching factual evidence class from `references/integration-and-evidence.md`; never use an input-discovery channel. Apply source priority without silently merging conflicts.
- Keep Paper Facts, Tutor Explanation, and Tutor Analysis visibly distinct; treat a role inferred from a caption such as "overview" as Tutor Analysis, not a Paper Fact.
- Apply the required formula, figure/table, experiment, or ablation contract whenever that content is requested.
- Include the required claim-to-evidence appendix for every full-paper output and preserve available evidence identifiers and locations.
- Mark unsupported material as unavailable, not verifiable, or Tutor Analysis rather than presenting it as fact.
- Confirm that no Scholar-Slides artifact, presentation input, or reverse-contaminating output was written or instructed.
