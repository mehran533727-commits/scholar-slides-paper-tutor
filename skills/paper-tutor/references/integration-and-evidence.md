# Integration and Evidence Safety

## Contents

1. [Input discovery and paper identity](#input-discovery-and-paper-identity)
2. [Mode selection and status disclosure](#mode-selection-and-status-disclosure)
3. [Source priority and conflicts](#source-priority-and-conflicts)
4. [Claim types and evidence handling](#claim-types-and-evidence-handling)
5. [Isolation and uncertainty](#isolation-and-uncertainty)

## Input discovery and paper identity

Discover inputs in this observable order. Use this order only to locate inputs
and set the user's requested discovery scope; do not use it as factual-claim
evidence precedence. Do not use a filename-only shortcut.

```text
explicitly supplied artifact or project directory
> matching verified context already in the conversation
> human-reviewed structured semantics
> approved evidence, quantitative, figure/table, and CKPT-1 artifacts
> structured extraction
> raw extraction
> raw PDF
```

Before combining sources, require a matching paper identity. Match by title,
authors, document hash/path, or mutually consistent metadata. If identity does
not match or cannot be established, do not combine the sources.

Record the selected `paper_identity`, the `analysis_source` pathway, and the
`evidence_source` that supports factual claims. Treat a project directory as an
explicit artifact source only after identifying the matching paper within it.
An explicitly supplied path selects where to discover matching inputs; it does
not make a raw artifact more authoritative than reviewed or verified semantics.

## Mode selection and status disclosure

Select `mode` using this contract:

```text
explicit supported override
else trusted matching Scholar-Slides artifacts → Integrated
else readable PDF → Standalone
else report missing input
```

Use a visible status block in either mode. `Analysis source` identifies the
analysis pathway. `Evidence source` identifies the highest-priority matching
factual evidence class from the precedence list below. Never put an
input-discovery channel such as `explicitly supplied artifact or project
directory` or `matching verified context already in the conversation` in
either source field. In Integrated Mode, state the `verification_status`
supported by the artifacts.

```text
Mode: Integrated
Paper identity: <matched identity>
Analysis source: Scholar-Slides-backed Paper-Tutor analysis
Evidence source: <highest-priority matching factual evidence class used>
Verification: <status supported by the matching artifacts>
```

In Standalone Mode, include this status block exactly:

```text
Mode: Standalone
Paper identity: <matched identity>
Analysis source: Standalone Paper-Tutor analysis
Evidence source: <highest-priority matching factual evidence class used>
Verification: Not verified through Scholar-Slides CKPT-1
```

Replace `<matched identity>` only with a verified title, authors, hash, or path.
If the identity is unavailable, render exactly
`Paper identity: Not verifiable from available evidence`; never emit a template
placeholder. Immediately after either mode block, render exactly one plain-text
depth line: `Depth: quick`, `Depth: deep`, or `Depth: research`. Do not wrap the
depth value in backticks.

If the user explicitly requests Integrated Mode but trusted matching artifacts
do not exist, explain that the required matching artifacts are missing. Fall
back to Standalone Mode only when a readable PDF exists and the request permits
a best-effort explanation. Never silently claim Integrated Mode.

If no readable PDF or trusted matching Scholar-Slides artifact is available,
report missing input and do not invent an analysis source, evidence source, or
verification status.

## Source priority and conflicts

For factual claims, use this evidence precedence, independently of the
input-discovery order:

```text
matching human-reviewed or verified semantics
> approved evidence, quantitative, figure/table, and CKPT-1 artifacts
> structured extraction
> raw extraction
> raw PDF
```

These five entries are the only allowed factual evidence-class values for
`Evidence source`. Use the exact class name that matches the selected evidence;
for example, a Standalone response based only on a readable PDF uses
`Evidence source: raw PDF`.

An explicitly supplied artifact or project directory defines discovery scope,
not factual precedence. Use the highest-ranked matching evidence available for
each factual claim. Keep sources separate when their facts are incompatible.

| Situation | Required behavior |
| --- | --- |
| Reviewed and raw sources agree | Use the reviewed material and retain available supporting evidence. |
| Reviewed and raw sources conflict | Prefer reviewed material; disclose a material conflict. |
| Sources are incompatible | Never silently merge their facts. |
| Identity is uncertain | Do not combine sources; request clarification or constrain the answer. |

When a conflict is material to the answer, state the competing evidence and
which reviewed source was preferred. Do not convert a raw extraction into a
reviewed claim merely by restating it.

## Claim types and evidence handling

Assign every substantive statement one of these labels and follow its allowed
behavior:

| Label | Allowed behavior |
| --- | --- |
| Paper Fact | Evidence-constrained. State it only when matching evidence supports it. |
| Tutor Explanation | May simplify; never attribute it to authors without evidence. |
| Tutor Analysis | Inference or critique; label explicitly. |
| Unsupported | Do not present it as fact. |

Preserve upstream Evidence IDs exactly; do not rename, regenerate, or merge
them. For each cited Paper Fact, retain the available page, section, figure, or
table location. State explicitly when a page, section, figure, or table
location is unavailable.

Apply this RED rule: a caption containing only "overview" supports only that
stated purpose. Any claimed system role, flow, module relationship, or intent
derived from the figure must be Tutor Analysis unless independently evidenced.

Do not promote a Tutor Explanation or Tutor Analysis into a Paper Fact because
it seems plausible, fits surrounding context, or appears in an unlabeled raw
extraction.

## Isolation and uncertainty

Keep the Scholar-Slides boundary strictly one-way. Paper-Tutor never writes to
Scholar-Slides artifacts, and Paper-Tutor prose never enters Mode B. Do not
modify Scholar-Slides outputs, feed teaching prose back into Scholar-Slides, or
make a presentation a required input.

When evidence is insufficient, use concrete language such as:

```text
The paper does not provide enough information here.
The following is a context-based explanation, not an explicit author statement.
```

Use `Unsupported` for material that lacks support. Label context-based
inference as Tutor Analysis, preserve the known uncertainty, and distinguish
what the paper states from what the tutor explains.
