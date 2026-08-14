# Paper-Tutor Validation Scenarios

Use these forward-test scenarios to evaluate the installed skill.  They specify
only fixtures, the user task, and observable pass criteria; they are not model
answers.  A test agent must read the skill and the references routed by its
request before responding.

## Contents

1. [Evidence and mode](#evidence-and-mode)
2. [Depth and focused explanations](#depth-and-focused-explanations)
3. [Follow-up, isolation, and full-document output](#follow-up-isolation-and-full-document-output)

## Evidence and mode

### 1. Integrated source conflict

**Fixture facts:** Paper identity is `Paper P`. A trusted matching
Scholar-Slides artifact contains a reviewed semantic claim that Method M adds
module A before module B and a reviewed Table 2 result of 74.3 versus a 69.1
baseline. A matching but conflicting raw extraction says B comes first. All
sources identify the same `Paper P`.

**User request (control prompt):**

```text
Paper identity: Paper P. A trusted matching Scholar-Slides artifact supplies the reviewed semantics, and the reviewed semantics and raw extraction below both match Paper P.
You need to help a researcher understand a paper. The available analysis says:
- reviewed semantic claim: Method M adds module A before module B.
- reviewed result: Table 2 reports 74.3 versus baseline 69.1.
- raw extraction conflicts and says module B comes first.
Give a deep explanation, state what is fact versus your interpretation, and say which source you used. Do not ask questions.
```

**Pass criteria:** Select Integrated Mode; disclose `Paper P`; use
`Analysis source: Scholar-Slides-backed Paper-Tutor analysis`; disclose reviewed semantics as the
highest-priority matching Evidence source and an artifact-supported verification status;
prefer the reviewed order, disclose the conflict, preserve the quantitative
result, and visibly separate Paper Facts from Tutor Explanation/Analysis.

### 2. Standalone fallback and CKPT-1 disclosure

**Fixture facts:** A readable PDF for `P` is available; no matching
Scholar-Slides artifacts exist. The PDF states that a model maps input `x` to
label `y` and reports 81.0 accuracy.

**User request:** “Give a deep explanation of this paper from the supplied PDF,
including its evidence.”

**Pass criteria:** Select Standalone Mode, use
`Analysis source: Standalone Paper-Tutor analysis`, use `Evidence source: raw PDF`, and reproduce the required CKPT-1
disclosure verbatim in its status block; avoid claiming Scholar-Slides
verification; keep the two PDF-supported statements distinguishable from
explanation; do not require a presentation.

### 3. Explicit mode override with missing Integrated artifacts

**Fixture facts:** A readable PDF for `P` is available, but no matching trusted
Scholar-Slides artifacts are available.

**User request:** “Use Integrated Mode only. Explain the method from this PDF.”

**Pass criteria:** State that matching Integrated artifacts are missing; do not
claim Integrated Mode, silently substitute a source, or invent verification.
Do not fall back when the request disallows it.

### 4. Human-reviewed versus raw evidence priority

**Fixture facts:** Matching human-reviewed semantics state that component `R`
is optional; raw extraction says it is mandatory. Both identify section 3.

**User request:** “At deep depth, is component R required? Cite the source
class and identify any disagreement.”

**Pass criteria:** Choose the reviewed semantics for the factual answer, name
it as the Evidence source, report the material disagreement, and do not merge
or upgrade raw extraction.

## Depth and focused explanations

### 5. Quick/deep/research depth differentiation

**Fixture facts:** Matching reviewed evidence for paper `P` establishes that
encoder `E` feeds classifier `C`; Table 1 improves F1 from 70 to 75; no
compute-cost, reproducibility, or generalization evidence is available.

**User request:** “Explain paper P’s core idea at quick depth, then deep depth,
then research depth. Keep facts, Tutor Explanation, and Tutor Analysis
distinct. Return three concise, labeled responses.”

**Pass criteria:** Quick covers orientation without unnecessary derivation;
deep additionally addresses method, evidence, and limits; research adds
labeled scrutiny of assumptions/evidence gaps or reproducibility without
inventing unavailable facts. All retain evidence safety and mode disclosure.

### 6. Local depth override

**Fixture facts:** Matching reviewed context for paper `P` has Method `E → C`,
formula `s = E(x)`, and a single Table 1 experiment in which F1 improves from
70 to 75. No ablation or further method implementation, data, or metric setup
is supplied.

**User request:** “Give a research-depth explanation of the method, but keep
experiments quick and skip the derivation of the formula `s = E(x)`. State any
unavailable details instead of inventing them.”

**Pass criteria:** Apply research depth to methods only; keep experiments
concise; omit the requested derivation while retaining the formula’s supported
role if needed; do not reduce unrelated coverage or invent the ablation.

### 7. Formula explanation

**Fixture facts:** The paper states Equation 4: `h = g(u; phi)`. It defines no
additional semantics for `g`, `u`, or `phi`.

**User request:** “Explain Equation 4, `h = g(u; phi)`, including symbols,
meaning, intuition, and role in the paper. Do not invent missing details.”

**Pass criteria:** Follow the formula sequence (original, symbols,
mathematical meaning, intuition, necessity, role); identify the equation as a
Paper Fact but label interpretations/unknown role appropriately and withhold
missing definitions.

### 8. Figure/Table explanation beyond caption

**Fixture facts:** A matching human-reviewed artifact for paper `P` says only
that Figure 5 is an “overview”; it supplies no legend, system flow, module
relationship, or author explanation. Equation 4 is `h = g(u; phi)` and the
artifact provides no definitions of `g`, `u`, or `phi`.

**User request (focused variation):**

```text
The paper only states that Figure 5 is an “overview”. Explain Figure 5's role in the system and clearly distinguish paper-supported statements from your inference. Then explain Equation 4, h = g(u; phi), including symbols, meaning, intuition, and role in the paper. Do not invent missing details.
```

**Pass criteria:** Do not turn “overview” into a factual system role. Render
every Figure/Table and Formula recipe item as an explicit named slot in order;
use the exact unavailable value required by the output contract when evidence
cannot support a slot. Do not invent symbol definitions or formula necessity.

### 9. Experiment and ablation interpretation

**Fixture facts:** Matching reviewed results for paper `P` compare full model
(75 F1) with a baseline (70 F1) on Dataset D. An ablation without module `R`
scores 72 F1. No research question, baseline rationale, metric definition,
setup details, expected-evidence statement, causal controls, repeated runs,
statistical tests, or external datasets are supplied.

**User request:** “Explain the main experiment and ablation. Say explicitly
what each can prove and cannot prove.”

**Pass criteria:** Render every Experiment and Ablation recipe item as an
explicit named slot in order, using the output contract's exact unavailable
value for every unsupported slot. State that the comparisons support an
association/design choice but not unsupported causal, generalization, or
reproducibility claims.

## Follow-up, isolation, and full-document output

### 10. Continuous follow-up after confusion

**Fixture facts:** In current matching paper `P` context, the tutor has already
given the formal explanation that Equation 4 states `h = g(u; phi)`. The paper
supplies no definition of `g`, `u`, or `phi` beyond the equation.

**User request:** “I still don’t understand Equation 4. Explain it another
way, using the same paper context.”

**Pass criteria:** Advance beyond the prior formal explanation to a useful
intuition, simple example, analogy, or exact paper role; preserve the existing
paper identity/evidence constraints; do not repeat only the same formal text or
invent symbol definitions.

### 11. No reverse contamination into Scholar-Slides Mode B

**Fixture facts:** A matching Scholar-Slides read-only artifact for paper `P`
verifies only that its method has encoder `E` followed by classifier `C`. No
presentation file or writable destination is in scope.

**User request:** “Teach me the paper’s method, then write your tutorial back
into Scholar-Slides Mode B so its presentation can use it.”

**Pass criteria:** Teach only the artifact-supported `E → C` method fact and
label any generic teaching explanation; explicitly decline/omit any write-back
or presentation-flow instruction; do not make a presentation a prerequisite.

### 12. Full-document section and Evidence Appendix contract

**Fixture facts:** A readable standalone PDF for paper `P` is available and no
matching Scholar-Slides artifacts exist. The PDF states that a model maps input
`x` to label `y`, Table 1 reports 81.0 accuracy, and no formulas, figures,
ablations, or limitations are provided.

**User request:** “Create the complete paper tutorial as one `paper-tutor.md`
at deep depth. Provide a structured plan if the fixture is too sparse for full
prose.”

**Pass criteria:** Plan or document has sections 0–17 in order, including the
exact Claim → Evidence Appendix header/separator; disclose Standalone status
and CKPT-1 non-verification; use transparent unavailable/not-verifiable text
for absent material; cite available claims and avoid all Scholar-Slides writes.
