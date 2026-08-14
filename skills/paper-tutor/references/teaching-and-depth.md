# Teaching and Depth Rules

## Depth selection

Default to `deep + auto-adaptive`. Honor an explicit user request for `quick`, `deep`, or `research`; otherwise use `deep` and adapt each concept to the user's stated or demonstrated expertise. Apply claim labels, evidence limits, mode selection, and verification disclosure from [integration-and-evidence.md](integration-and-evidence.md); do not reinterpret them here.

| Depth | Deliver |
| --- | --- |
| `quick` | Build an overall understanding in 10–20 minutes: problem, importance, core insight, overall method, decisive experiments, contribution, and limitations. Avoid unnecessary derivations. |
| `deep` | Explain background, gap, intuition, modules, key formulas, figures/tables, experimental design, results, ablations, and limitations. This is the default. |
| `research` | Include everything in `deep`, then examine assumptions, design alternatives, claim–evidence sufficiency, missing baselines or ablations, reproducibility, generalization, scalability, computational cost, and future research. |

## Adaptive concept depth

For every concept, ask these three questions in order:

1. Is the concept necessary to understand the requested scope?
2. Is the user likely to know it, based on stated expertise and the ongoing conversation?
3. Would omitting it block later understanding?

Select one teaching level below. Favor the smallest level that leaves the requested reasoning unblocked; make genuinely central, difficult concepts more detailed than obvious supporting material.

| Level | Use when | Deliver |
| --- | --- | --- |
| `skip` | It is unnecessary, already known, and omission blocks nothing. | Omit it. |
| `brief` | It is useful context but not a reasoning bottleneck. | Give a concise definition and local relevance. |
| `detailed` | It is needed to follow a result, design choice, or transition. | Explain components, mechanism, and connection to the paper. |
| `deep` | It is central, unfamiliar, or omission would block later understanding. | Build from prerequisites through intuition, worked reasoning, and exact role in the paper. |

Do not use one fixed ladder for the entire paper. Re-evaluate concept depth as the user reveals expertise or narrows scope.

## Local depth overrides

Treat a local override as scoped only to the named content.

| User request | Required effect |
| --- | --- |
| “skip derivation” | Omit derivation for the named formula or scope; retain the formula's role if needed. |
| “research depth for methods” | Apply `research` only to methods; preserve current depth elsewhere. |
| “quick experiments” | Apply `quick` only to experiments; do not reduce method or limitation coverage. |

If scope is ambiguous, use the narrowest reasonable named scope and state the interpretation briefly. Do not let a local override silently change global depth.

## Confusion recovery and follow-ups

When the user remains confused, advance through this sequence without merely repeating the same answer:

```text
formal explanation → intuition → simple example → analogy → exact role in this paper
```

Start at the next useful stage rather than restarting when it was already given. Reuse the current paper identity, verified model, available evidence, and previous explanation. Re-read the PDF only when paper identity changes, evidence conflicts, or required context is missing.

Answer a focused question only for the requested part. Update `paper-tutor.md` only when the user explicitly asks to update that file; otherwise answer in conversation and retain the document unchanged.
