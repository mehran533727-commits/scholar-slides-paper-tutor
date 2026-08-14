# scholar-slides 中文使用说明

> 当前正式版本：0.3.0 Final Stable

scholar-slides 是面向 Codex 的学术论文阅读与学术汇报 Skill。它把论文、证据、人工审核、
PPT 和最终交付串成可审计流程：事实来自源 PDF 和已批准 checkpoint，不从记忆补全缺失事实。

## 1. 什么时候使用

适用于：

- 阅读、解释、总结论文，或提取贡献、方法、公式、实验、图表和关键数字；
- 从本地 PDF、Zotero 导出的本地 PDF 或 arXiv 制作学术汇报；
- 继续已有 scholar-slides 项目；
- 修改已经批准的 deck，同时保留旧审批历史。

只有研究主题、没有论文或可信来源时，不足以制作 source-bound deck。

## 2. 支持的输入

- 本地 PDF；
- Zotero 导出的本地 PDF；
- arXiv ID 或 URL（下载后的 PDF 成为证据源）；
- 已有 scholar-slides 项目；
- 只有 `digest.json` 的项目可以做 extractive 阅读，但不能证明 PDF 身份或 evidence audit，
  也不能据此推断 source-bound deck。

## 3. Mode A：只读懂论文

Mode A 用于论文理解，不制作 PPT：

```text
论文 -> ingest/build -> extractive digest -> CKPT-1 candidate -> 人工明确批准 -> 停止
```

CKPT-1 前的内容是候选理解，同时生成面向用户的 `paper-analysis.md`。用户明确批准后，标题、贡献、方法、实验结果和证据绑定才成为
`confirmed semantic view`。推荐提示词：

```text
请使用 scholar-slides 阅读这篇论文，解释问题、方法、实验结论和术语，先做到 CKPT-1，不制作 PPT。
```

## 4. Mode B：制作学术汇报

Mode B 用于 journal club、conference、组会、论文汇报、thesis defense、可编辑 PPTX、PDF、
HTML 或 speaker notes；它包含 Mode A 的全部证据保护：

```text
论文 -> CKPT-1 -> 人工批准 -> deck + quantitative coverage
     -> semantic/visual QA + aesthetics + figure legibility
     -> CKPT-2 -> 人工批准 -> export
```

## 5. 选择 deck type 与可选 outline review

支持三种 deck type，默认是 `journal-club`：

| deck type | 适用场景 | 叙事与密度 |
| --- | --- | --- |
| `journal-club` | 论文精读、组会 | reading-first、高密度、深入方法和结果、critique/discussion |
| `conference` | 会议演讲 | speaker-led、低密度、每页一个主信息、figure-forward |
| `thesis-defense` | 学位答辩 | mixed density、多项贡献、答辩叙事、appendix-rich |

先选择类型，再给出页数、演讲时长、听众或 density；用户的明确约束在类型安全范围内优先。
简单请求使用 fast path：

```text
CKPT-1 -> plan -> deck -> CKPT-2
```

需要先讨论故事线、页数、听众或备选图时，在 CKPT-1 明确批准后请求 `outline review`。
它只是可选的规划暂停，不是 checkpoint，也不产生 approval state；接受 outline 后再生成
deck，公共人工门仍只有 CKPT-1 和 CKPT-2。

## 6. CKPT-1：论文理解审核门

CKPT-1 审核元数据、问题、贡献、方法、实验结果、关键指标、公式、图表、source locators、
candidate correction、marker resolution 和 evidence audit。保留 `[MISSING: ...]` 与
`[UNVERIFIED: ...]`；`prepared_by`、`ready_for_human_approval` 或用户沉默都不是批准。

批准格式：

```text
批准 CKPT-1（确认人：<name>）
```

确认后的 record 绑定源 PDF SHA-256、digest、resolved semantic view、marker ledger、audits
与 readiness evidence。不要手改 checkpoint JSON。

## 7. CKPT-2：PPT 内容审核门

CKPT-2 审核已经完成 semantic QA、visual QA、quantitative coverage、aesthetics 和 figure
legibility 的当前 deck。通过 QA 只表示 `ready_for_human_approval`，不表示批准：

```text
批准 CKPT-2（确认人：<name>）
```

未明确批准 CKPT-2 时，不能导出 HTML、PDF、PPTX 或 speaker notes。

## 8. quantitative coverage

`coverage-requirements.json` 先按 reviewed research question / contributions 判断科学相关性，
再要求已经确认的核心事实真实出现在可见页面，而不是只写在 notes：

- reviewed key metrics（标签和值）；
- 数值型 reviewed experimental results；
- 有效 audit-backed pairwise comparison 的端点或比例。

系统优先保留正文中直接回答研究问题的证据。Appendix 结果默认是 supporting evidence；只有
负向结果、trade-off、robustness 发现或正文无等价证据时，才可带着明确理由提升为核心要求。
priority report 会记录研究维度、正文/附录范围、tier 和选择理由，不能仅因 audit 可用就把
appendix 数字提升为核心叙事。

使用可编辑 native text、comparison card、native table 或 chart。notes-only 不算覆盖；缺失、
缺少上下文或只存在于 notes 时，semantic QA 以
`semantic-quantitative-coverage-missing` 阻断 CKPT-2。coverage 和 provenance hash 会绑定到
review manifest。

## 9. Semantic QA、Visual QA 与 aesthetics gate

Semantic QA 检查 claim、数字、方法图和贡献的 evidence/provenance，以及 checkpoint、digest、
audit、review、coverage binding 是否过期或被篡改。Visual QA 检查空白页、裁切、遮挡、标题换行、
密度、图像缺失、乱码、中文字体和布局。

Visual QA 之后、CKPT-2 之前运行 deterministic aesthetics gate。每页按
`hierarchy_focus`、`typography`、`space_grid`、`figures_data_ink`、`color_contrast`、
`consistency_finish` 六维打 0--4 分；有 figure/data 时满分 24，无 figure/data 时满分 20。
任一有效维度不超过 2，或总分低于等价 18/24 阈值，就进入有限 `rework`，修改后重新
render/rescore。报告要列 weakest 3、缺陷、维度、严重度、建议，以及 emoji、无意义渐变/blob、
重复 dashboard card 等 AI 视觉陈词滥调。报告缺失或仍有 rework 时，CKPT-2 readiness = false。

## 10. figure legibility

图表使用集中阈值做 deterministic 检查：

- `figure-text-illegible`：投影后的图内文字低于 12 px 下限；
- `figure-compressed`：图被过度压缩；
- `vertical-void` / `horizontal-void`：布局留下异常大空白。

每条 finding 都带 slide、asset、source locator、measurement、threshold 和 action。
图中文字不可读或图被压缩会阻断 CKPT-2；允许的空白 warning 仍要保留并说明。

## 11. citation fallback

引用补全使用可选 provider chain：

```text
Zotero（可用时） -> DOI/Crossref -> arXiv -> unresolved
```

统一字段至少包括 `title`、`authors`、`year`、`venue`、`doi`、`arxiv_id`、`url_or_locator`、
`provider`、`verified`、`confidence`。不能验证时必须 `verified = false` 并显示
`[UNVERIFIED]`，禁止猜 DOI、venue、作者或年份。外部 related-work citation 和 source paper
metadata 分开；provider 不能绕过 CKPT-1 correction flow 覆盖源论文元数据。没有 provider 时，
本地 PDF 的 Mode A 继续离线阅读。

## 12. 修改已经批准的 deck：reopen/supersede

不能直接覆盖 approved CKPT-2，必须使用：

```text
approved CKPT-2 -> reopen -> checkpoint-history 保留旧 approval/review/delivery
                 -> 修改 -> 新 review -> 新 CKPT-2 -> 明确批准 -> export
```

`supersedes` lineage 记录新旧 revision。reopen 保留旧 hash、review 与 delivery，不重写历史，
也不是 approve；被中断时使用 documented resume，不手改 checkpoint/history JSON。

## 13. 导出最终交付

当前 CKPT-2 被明确批准后，使用唯一 public delivery contract：

```bash
scholar-slides export --project <project> --formats html,pdf,pptx,notes
```

最终支持 HTML、PDF、可编辑 PPTX 和 speaker notes，并始终附带逐页完整讲稿
`presentation-script.md` 与汇报速记 `presentation-summary.md`；facade 始终使用同一 CKPT-2 gate、delivery.mjs、delivery manifest、validation 和跨格式一致性检查。

## 14. 常用 CLI

```bash
scholar-slides --version
scholar-slides doctor --json
scholar-slides build --help
scholar-slides ingest --help
scholar-slides review --help
scholar-slides approve --help
scholar-slides reopen --help
scholar-slides export --help
```

先读 `--help`，不要猜参数。

## 15. 可直接复制的提示词

```text
请使用 scholar-slides 阅读这篇论文，先做到 CKPT-1，不制作 PPT。
```

```text
请使用 scholar-slides 将这篇论文制作成 12 页左右的组会汇报，选择 journal-club，关键定量结果必须可见，完整走 CKPT-1 / CKPT-2。
```

```text
请使用 scholar-slides 继续这个已有项目：<project path>。先读取 checkpoint 状态，不要重复批准已经确认的 checkpoint。
```

```text
请使用 scholar-slides 正式 reopen 当前 CKPT-2，保留旧 approval、review 和 delivery 历史，然后按我的新要求重新审核。
```

```text
CKPT-2 已批准。请使用 scholar-slides 正式导出 HTML、PDF、PPTX 和 speaker notes，并完成 delivery validation。
```

## 16. 安装与发现验证

安装或升级后重新启动 Codex，再检查：

```bash
scholar-slides --version
scholar-slides doctor --json
```

预期：

```text
版本 = 0.3.0
doctor.ok = true
```

## 17. 常见错误与处理原则

- 缺少来源、source binding 或可见关键数字：STOP，补齐通用证据，不绕过 gate；
- checkpoint、review 或 delivery 被篡改或过期：重新生成并重新审核；
- `figure-text-illegible` 或 `figure-compressed`：调整布局/尺寸后重新 render 和 QA；
- generic bug：修通用产品行为并添加回归测试，禁止按单篇论文写特判；
- 不能确认的人类批准：等待明确批准，绝不伪造。

一句话：

```text
只读论文：请使用 scholar-slides 阅读这篇论文，先做到 CKPT-1。
做 PPT：请使用 scholar-slides 将这篇论文制作成学术汇报，关键定量结果必须可见，完整走 CKPT-1 / CKPT-2。
```
