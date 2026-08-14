# 架构说明

## 仓库边界

仓库由四个独立层组成：

1. `skills/scholar-slides`：证据约束的论文处理与演示运行时；
2. `skills/paper-tutor`：只读教学层；
3. `scripts` 与 `tests`：安装和源代码包验证；
4. `docs`：面向用户和维护者的仓库级合同。

两个技能不共享可写运行目录。每个 Scholar-Slides 项目都位于用户选择的项目目录，任何论文、digest、checkpoint、deck 或 export 都不能写进已安装技能目录。

## Scholar-Slides

主要组件：

- `SKILL.md` 与 `references/`：Codex 的工作流和人工审核合同；
- `bin/`：选择技能本地 Python 环境的 Windows 启动器；
- `runtime/scripts/`：来源准备、语义、定量覆盖、deck、QA、review 和 delivery 实现；
- `runtime/assets/`：deck stage 与主题模板；
- `schemas/`：checkpoint、digest、review、audit 和 quantitative coverage 的结构约束；
- `requirements-runtime.txt`、`package.json`、`package-lock.json`：可重建依赖。

高层状态流：

```text
doctor
  → source preparation / extractive digest
  → CKPT-1 candidate
  → explicit human CKPT-1 approval
  → narrative + deck + visible quantitative coverage
  → semantic/visual/aesthetics review
  → explicit human CKPT-2 approval
  → export + validation + parity evidence
```

系统 fail closed：来源、hash、audit、checkpoint 或 review 过期时停止，而不是尝试用记忆补齐。

## Paper-Tutor

Paper-Tutor 没有独立运行时脚本。它由：

- `SKILL.md`：工作流、边界和 reference routing；
- `references/integration-and-evidence.md`：身份、模式、证据优先级和 claim labels；
- `references/teaching-and-depth.md`：quick/deep/research 与自适应教学；
- `references/output-contract.md`：完整教程和公式/图表/实验/ablation 合同；
- `references/validation-scenarios.md`：可复用的行为场景；
- `agents/openai.yaml`：Codex UI 元数据。

它消费匹配证据，但没有写回接口。

## 双技能数据流

```text
                 ┌──────────────────────────┐
paper/PDF/arXiv →│ Scholar-Slides project   │
                 │ digest, reviews, evidence│
                 └────────────┬─────────────┘
                              │ matching identity, read-only
                              ▼
                 ┌──────────────────────────┐
                 │ Paper-Tutor              │
                 │ explanation and analysis │
                 └──────────────────────────┘
```

不存在从 Paper-Tutor 指向 Scholar-Slides 或 deck 的箭头。这是事实安全边界，不是实现细节。

## 安装架构

安装器在目标技能根目录内建立唯一 staging 目录，在 staging 中复制并重建依赖。验证成功后才把两个技能移动到最终名称。已有目标只有在 `-Force` 时才移动到 `.skill-backups`。

仓库本身永远不保存重建后的 `.venv` 或 `node_modules`。这些目录属于安装状态，不属于源代码。

## 验证架构

`scripts/verify_package.py` 检查：

- 必需文件与技能 metadata；
- JSON、Markdown 相对链接和版本一致性；
- Paper-Tutor UI metadata；
- 原 Scholar-Slides MIT 署名；
- 禁止提交的生成目录、旧 manifest、敏感本机路径、疑似 token 和超大文件。

`scripts/verify-package.ps1` 在此基础上增加 Python 编译与 PowerShell AST 解析。GitHub Actions 在 Windows 上重建依赖并运行 CLI 版本 smoke test。
