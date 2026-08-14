# 证据与 Checkpoint 安全

## 不可协商的不变量

1. 科学事实必须来自与当前论文身份匹配的证据；
2. 缺失事实不从模型记忆、相似论文或教学解释补齐；
3. CKPT-1 和 CKPT-2 只能由用户显式批准；
4. readiness、QA 通过、`prepared_by` 或用户沉默都不是批准；
5. Paper-Tutor 只读消费上游，不修改上游；
6. 演示页面必须真实显示所需定量事实，speaker notes 不能代替可见覆盖。

## 论文身份

合并来源前，至少用标题、作者、文档 hash/path 或相互一致元数据确认同一篇论文。文件名、目录名或主题相似不足以建立身份。

身份不确定时：

- 不合并来源；
- 把范围限制在已确认输入；
- 无法建立身份时写 `Paper identity: Not verifiable from available evidence`。

## 证据优先级

事实优先级从高到低：

```text
matching human-reviewed or verified semantics
> approved evidence, quantitative, figure/table, and CKPT-1 artifacts
> structured extraction
> raw extraction
> raw PDF
```

显式提供的目录只决定发现范围，不自动提高目录内材料的事实等级。

如果 reviewed 与 raw 冲突，使用 reviewed 内容，并披露影响结论的冲突。不同论文的材料不得静默拼接。

## Source hash 链

Scholar-Slides 的 source-bound audit 需要满足：

```text
SHA-256(actual source PDF)
= digest source SHA-256
= checkpoint source identity SHA-256
= audit source SHA-256
```

来源、digest、crop、audit、candidate 或 marker ledger 发生变化后，旧绑定过期。重新生成对应证据，不手改 JSON 或 hash。

## CKPT-1

`prepare-checkpoint` 生成候选审核包并停在 `pending_human_confirmation`。候选可以包含 metadata correction、claims、metrics、marker decision 和 evidence audit，但仍不是确认事实。

只有用户检查精确候选并明确授权后才能执行：

```powershell
scholar-slides approve 1 --out "C:\work\paper-project" --confirmed-by "Reviewer Name"
```

批准记录不可手工伪造或覆盖。

## CKPT-2

CKPT-2 绑定具体 deck、可见定量覆盖和 review bundle。QA 通过只表示 ready。只有用户明确批准当前未变化的 deck 后才允许 export。

已批准 deck 需要修改时，使用 reopen/supersede 生命周期，保留旧 approval/review/delivery 历史；新 deck 必须重新 review 和批准。

## Paper-Tutor 声明标签

- `Paper Fact`：匹配证据支持的事实；
- `Tutor Explanation`：教学简化或类比，不归因给作者；
- `Tutor Analysis`：明确标注的推断或批评；
- `Unsupported`：不作为事实，写明不可用或不可验证。

Paper-Tutor 必须保留可用的 Evidence ID、页码、章节和 Figure/Table 定位。不能重新命名或合并上游 Evidence ID。

## Standalone 披露

直接基于 PDF 且没有匹配可信产物时，必须写：

```text
Mode: Standalone
Analysis source: Standalone Paper-Tutor analysis
Evidence source: raw PDF
Verification: Not verified through Scholar-Slides CKPT-1
```

这不是低质量模式，而是明确说明验证边界。

## 安全失败方式

遇到以下情况应停止或降级声明，而不是猜测：

- 来源不可读或身份无法确认；
- required fact 没有可见 deck 覆盖；
- audit/checkpoint/review hash 过期；
- Figure/Table 只有模糊图注；
- 公式符号、实验设置或 ablation 信息在证据中缺失；
- 用户没有明确批准 checkpoint。

使用 `Not present in the available source.`、`Not verifiable from available evidence` 或明确的 Tutor Analysis 标签。
