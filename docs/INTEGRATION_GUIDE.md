# Scholar-Slides 与 Paper-Tutor 搭配指南

## 核心分工

把 Scholar-Slides 当作准确、可审计的论文阅读器，把 Paper-Tutor 当作清晰、可适配的教师。

```text
论文/PDF/arXiv/匹配项目
        ↓
Scholar-Slides：提取、证据绑定、候选语义、人工确认
        ↓ 只读且论文身份必须匹配
Paper-Tutor：教学解释、类比、研究分析
```

数据流不能反向。Paper-Tutor 不修改 Scholar-Slides 产物，不把教学文本写入 CKPT-1/CKPT-2，不把教学推断变成演示文稿事实。

## 决策表

| 用户目标 | 路由 | 必要输入 | 输出/停止点 |
| --- | --- | --- | --- |
| 只想可靠读懂论文 | Scholar-Slides Mode A | 本地 PDF、Zotero 导出 PDF、arXiv 或已有项目 | `paper-analysis.md`、候选语义；用户明确批准 CKPT-1 后停止 |
| 基于已审核结果继续学习 | Scholar-Slides → Paper-Tutor Integrated | 与同一论文匹配的可信 Scholar-Slides 产物 | 教学回答或一个 `paper-tutor.md`；不写回上游 |
| 手头只有 PDF，希望先解释 | Paper-Tutor Standalone | 可读 PDF | 明确标注未经 CKPT-1 验证的教学回答 |
| 制作学术汇报 | Scholar-Slides Mode B | 论文来源与已明确批准的 CKPT-1 | deck/review → 明确批准 CKPT-2 → export |
| 只有研究主题，没有论文 | 暂不进入 source-bound 工作流 | 需要补充论文或可信来源 | 说明输入不足，不编造论文事实 |

## 三条标准路径

### 1. 证据约束的论文阅读

```text
paper → Scholar-Slides build → extractive digest → CKPT-1 candidate
      → 用户检查 → 用户明确批准 CKPT-1 → stop
```

命令入口：

```powershell
scholar-slides doctor --json
scholar-slides build --input "C:\work\paper.pdf" --project "C:\work\paper-project"
```

`paper-analysis.md` 是候选理解的可读投影，不是新的事实来源。`prepared_by=Codex`、readiness 状态或用户沉默都不构成批准。

### 2. 基于匹配产物的教学

Paper-Tutor 先核对论文身份，再选择模式。可以用标题、作者、文档 hash/path 或相互一致的元数据确认同一篇论文。身份不匹配或无法确认时，不得合并来源。

可信匹配产物存在时使用：

```text
Mode: Integrated
Paper identity: <已核实的论文身份>
Analysis source: Scholar-Slides-backed Paper-Tutor analysis
Evidence source: <最高优先级的匹配事实证据类别>
Verification: <匹配产物实际支持的状态>
Depth: quick | deep | research
```

状态字段中的尖括号表示输出时必须替换为实际值；不能原样保留占位符。

### 3. 经过审核的学术汇报

```text
paper → CKPT-1 candidate → 用户明确批准 CKPT-1
      → deck + 可见定量覆盖 + semantic/visual/aesthetics QA
      → 用户审核 → 用户明确批准 CKPT-2 → export
```

```powershell
scholar-slides build --project "C:\work\paper-project" --resume
scholar-slides review --project "C:\work\paper-project"
scholar-slides approve 2 --out "C:\work\paper-project" --confirmed-by "Reviewer Name"
scholar-slides export --project "C:\work\paper-project" --formats html,pdf,pptx,notes
```

执行前以 `scholar-slides --help` 和子命令 `--help` 为最终 CLI 合同。审核通过只表示 ready，不等于用户批准。

## Integrated 与 Standalone

选择规则：

```text
用户明确支持的模式覆盖
否则，有可信且论文身份匹配的 Scholar-Slides 产物 → Integrated
否则，有可读 PDF → Standalone
否则 → 报告缺少输入
```

Standalone 必须使用以下披露：

```text
Mode: Standalone
Paper identity: <已核实身份；无法核实时写 Not verifiable from available evidence>
Analysis source: Standalone Paper-Tutor analysis
Evidence source: raw PDF
Verification: Not verified through Scholar-Slides CKPT-1
Depth: quick | deep | research
```

如果用户只允许 Integrated，而匹配产物不存在，就停止并说明缺失，不能静默退化为 Standalone。

## 事实证据优先级

发现输入的顺序不等于事实优先级。事实声明按以下顺序取最高等级的匹配证据：

```text
matching human-reviewed or verified semantics
> approved evidence, quantitative, figure/table, and CKPT-1 artifacts
> structured extraction
> raw extraction
> raw PDF
```

原始来源与已审核语义冲突时，优先已审核内容，并披露对答案有影响的冲突。不同论文或身份不确定的来源不得静默合并。

## 四种声明类型

| 标签 | 允许行为 |
| --- | --- |
| Paper Fact | 只能在匹配证据支持时陈述，并保留可用的页码、章节、图表或 Evidence ID |
| Tutor Explanation | 可以简化和类比，但不能冒充作者原话或作者结论 |
| Tutor Analysis | 推断、批评或研究建议，必须明确标注 |
| Unsupported | 不作为事实输出；写明 unavailable 或 not verifiable |

只有 “overview” 的图注只能支持“这是 overview”。由此推断系统角色、模块关系或数据流属于 Tutor Analysis，除非另有证据。

## 深度选择

- `quick`：10–20 分钟建立问题、核心思想、整体方法、关键证据、贡献和局限；
- `deep`：默认；增加背景、模块、公式、图表、实验设计、ablation 和局限；
- `research`：在 deep 基础上审视假设、替代设计、证据充分性、可复现性、泛化、成本和未来研究。

局部要求只影响命名范围，例如“方法 research，实验 quick，跳过公式推导”不应改变其他部分的深度。

## 可复制提示词

快速阅读：

```text
请先使用 scholar-slides 对这篇论文做到 CKPT-1；我明确批准后，再使用 paper-tutor 以 quick 深度解释问题、核心 insight、方法和决定性实验。不要制作 PPT。
```

深入教学：

```text
请使用 paper-tutor 的 Integrated Mode 读取这个匹配的 Scholar-Slides 项目，以 deep 深度讲解方法、关键公式、图表、实验和 ablation。每一处都区分 Paper Fact、Tutor Explanation 和 Tutor Analysis，并保留 Evidence ID 与定位。
```

研究级审视：

```text
请使用 paper-tutor 以 research 深度审视这篇论文：先按匹配证据解释方法和结果，再讨论假设、证据充分性、缺失 baseline/ablation、复现、泛化、扩展性和成本。没有证据的内容必须标为 Tutor Analysis 或不可验证。
```

学术汇报：

```text
请使用 scholar-slides 将这篇论文制作成 12 页左右的 journal-club 汇报。关键定量结果必须在页面中可见，完整走 CKPT-1 和 CKPT-2；不要把 Paper-Tutor 教学文本写回 deck。
```

## 禁止的组合方式

- 先让 Paper-Tutor 猜出论文事实，再写回 Scholar-Slides；
- 用 Paper-Tutor 的解释替代 source locator、Evidence ID 或 evidence audit；
- 把 CKPT readiness、`prepared_by` 或静默当作人工批准；
- 仅凭文件名把两篇论文的产物合并；
- 让 speaker notes 代替演示页面中的定量证据；
- 修改已批准 deck 后继续沿用旧 CKPT-2。需要修改时必须使用 Scholar-Slides 的 reopen/supersede 生命周期。

更精确的合同见 [Scholar-Slides 工作流](../skills/scholar-slides/references/workflow.md)、[checkpoint 说明](../skills/scholar-slides/references/checkpoints.md)和 [Paper-Tutor 证据规则](../skills/paper-tutor/references/integration-and-evidence.md)。
