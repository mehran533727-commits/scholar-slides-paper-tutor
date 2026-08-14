# Paper-Tutor Output Contract

## Contents

1. [Output boundary](#output-boundary)
2. [Full-document contract](#full-document-contract)
3. [Explanation recipes](#explanation-recipes)
4. [Evidence appendix](#evidence-appendix)

## Output boundary

Produce a focused answer for a focused request. Produce exactly one `paper-tutor.md` for a full-paper request. Apply paper identity, mode, verification, claim labels, source priority, conflict handling, and the caption-only `overview` safety rule from [integration-and-evidence.md](integration-and-evidence.md); do not duplicate or weaken them here.

Use transparent `Not present in the available source.` or `Not verifiable from the available evidence.` text whenever required material is absent. Never fill a section with plausible but invented material. Keep simple or obvious sections concise and explain complex core concepts in detail.

## Full-document contract

Create the following numbered sections, in this order, in one `paper-tutor.md`. For each section, answer its central question and include at least the stated minimum content. Use lightweight body citations for key methods, numbers, results, contributions, limitations, and important figures/tables. Preserve upstream Evidence IDs exactly.

### 0. 阅读导航

**Question:** What is this paper and how should the reader approach it?

**Minimum:** Disclose paper identity, one-sentence thread, domain, reading difficulty, 3–5 key questions, prerequisite knowledge, current depth, current mode, analysis source, and verification status.

### 1. 先用最简单的话讲清整篇论文

**Question:** What does the paper do, in plain language?

**Minimum:** State the problem, main idea, method at a high level, and what the evidence says it achieves.

### 2. 阅读这篇论文需要哪些前置知识

**Question:** What must the reader know before proceeding?

**Minimum:** Name only prerequisites needed for this paper; define each at adaptive concept depth or say why no special prerequisite is needed.

### 3. 为什么会有这篇论文

**Question:** What gap or failure motivates the work?

**Minimum:** Connect background, prior limitation, and the paper's target problem without overstating unstated motivation.

### 4. 作者最核心的 Insight

**Question:** What central insight makes the approach plausible?

**Minimum:** Separate authors' supported claim from Tutor Explanation or Tutor Analysis, then connect the insight to the gap.

### 5. 方法整体框架

**Question:** How does the complete method flow from input to output?

**Minimum:** Give pipeline, key data/control flow, major components, and where each component supports the insight.

### 6. 方法逐模块拆解

**Question:** What does each module do and why is it present?

**Minimum:** Explain every material module using the module recipe; mark unavailable implementation detail as not present or not verifiable.

### 7. 公式与数学

**Question:** Which mathematics is necessary to understand the method?

**Minimum:** Explain each key formula with the formula recipe. Omit a nonessential derivation when selected depth or local override calls for it, while preserving the formula's role.

### 8. Figures/Tables 怎么看

**Question:** How should the reader extract intended evidence from each important figure or table?

**Minimum:** Explain important figures/tables with the figure/table recipe and body citations. Do not copy captions as the explanation.

### 9. 实验设计

**Question:** What question is each experiment designed to answer?

**Minimum:** Explain datasets, baselines, metrics, setup, and expected evidence with the experiment recipe.

### 10. 实验结果

**Question:** What did the reported results show?

**Minimum:** Report key supported outcomes, comparisons, and uncertainty; distinguish what results can and cannot establish.

### 11. Ablation 怎么理解

**Question:** What does removing or changing a component reveal?

**Minimum:** Explain each available ablation with the ablation recipe; state when no ablation is present or verifiable.

### 12. 论文真正的贡献

**Question:** What did the authors claim to contribute, and what is the tutor's assessment?

**Minimum:** Use two labeled subsections: `Authors' Claimed Contributions` for supported author claims and `Tutor Interpretation` for tutor synthesis.

### 13. Limitations

**Question:** What limits does the paper state, and what additional limits are inferred?

**Minimum:** Use two labeled subsections: `Explicit Limitations` for supported author statements and `Inferred Limitations` for clearly labeled Tutor Analysis.

### 14. 研究者视角重新审视

**Question:** How should a researcher assess the work beyond its headline result?

**Minimum:** At research depth, assess assumptions, alternatives, evidence sufficiency, missing baselines/ablations, reproducibility, generalization, scalability, cost, and future research. At lower depths, provide only the most decision-relevant, clearly labeled analysis.

### 15. 把全文重新串起来

**Question:** How do the paper's parts form one argument?

**Minimum:** Explicitly reconnect `Background → Gap → Insight → Method → Evidence → Contribution` in one coherent thread.

### 16. 你真正应该记住什么

**Question:** What high-value knowledge should remain after reading?

**Minimum:** Provide 5–10 high-density takeaways, calibrated to selected depth and separated from unsupported speculation.

### 17. Claim → Evidence Appendix

**Question:** What evidence supports the document's substantive claims?

**Minimum:** Include the exact appendix schema below and entries for body claims requiring support.

## Explanation recipes

Use these ordered recipes whenever their content type is requested. Render
**every recipe item as an explicit named slot**, in the listed order; do not
collapse, rename, or omit a slot. When the available evidence cannot support a
slot, render that slot with the exact value
`Not verifiable from available evidence` rather than filling it with a plausible inference. A separately
labeled Tutor Explanation or Tutor Analysis may follow a slot, but it does not
replace the slot.

| Content type | Required explanation sequence |
| --- | --- |
| Formula | original → symbols → mathematical meaning → intuition → necessity → role |
| Figure/Table | question → reading order → encodings/parts → intended observation → paper connection → limits |
| Experiment | research question → dataset → baseline rationale → metric → setup → expected evidence → result → can/cannot prove |
| Ablation | removed component → change → supported design choice → alternative explanation |
| Module | purpose → necessity → input → output → implementation → design rationale → relationship to neighbors |

When matching evidence contains the original formula, place `Paper Fact:` on a
separate line immediately before its `original` slot so the equation itself is
explicitly labeled. This label does not replace or rename the `original` slot.
Keep unsupported symbol definitions, meaning, intuition, necessity, and role at
the required unavailable value.

Do not copy a figure or table caption in place of explanation. The caption-only `overview` safety rule remains governed by [integration-and-evidence.md](integration-and-evidence.md).

## Evidence appendix

Use exactly this header and separator row:

| Claim | Source | Page | Section | Figure/Table | Evidence ID |
| --- | --- | --- | --- | --- | --- |

Keep body citations lightweight while making supporting appendix rows traceable. Retain available page, section, and figure/table locations; state when a location is unavailable. Preserve upstream Evidence IDs without renaming, regenerating, or merging them.

In Standalone Mode, label every appendix evidence entry `Standalone evidence` and `Not CKPT-1 verified` in the appropriate Source and/or verification text, while preserving required mode disclosure from [integration-and-evidence.md](integration-and-evidence.md).
