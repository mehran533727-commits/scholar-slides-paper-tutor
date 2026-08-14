# Scholar-Slides + Paper-Tutor

[English README](README.md)

这是一套面向 Codex 的学术论文技能组合，用于证据约束的论文阅读、清晰教学和经过人工审核的学术汇报。

两个技能职责严格分离：

- **Scholar-Slides 0.3.0** 读取来源、构建与证据绑定的语义、执行显式人工审核门，并可生成 HTML、PDF、可编辑 PPTX 和讲稿；
- **Paper-Tutor** 基于匹配证据做自适应讲解，始终区分 Paper Facts、Tutor Explanation 和 Tutor Analysis。

二者只能单向组合：

```text
论文或匹配项目
      ↓
Scholar-Slides 证据与已审核语义
      ↓ 只读
Paper-Tutor 教学解释
```

Paper-Tutor 的教学文本不能写回 Scholar-Slides、checkpoint 或演示文稿。

## 如何选择

| 目标 | 使用技能 | 停止点或审核门 |
| --- | --- | --- |
| 阅读、理解或审计论文 | Scholar-Slides Mode A | 用户明确批准 CKPT-1 后停止 |
| 快速、深入或研究级教学解释 | Paper-Tutor | 有匹配可信产物时优先 Integrated Mode |
| 没有已审核产物，直接从 PDF 解释 | Paper-Tutor Standalone Mode | 必须披露“未经 Scholar-Slides CKPT-1 验证” |
| 制作组会、会议或答辩 PPT | Scholar-Slides Mode B | 显式 CKPT-1 → 显式 CKPT-2 → export |

组合使用前请阅读[完整搭配指南](docs/INTEGRATION_GUIDE.md)。

## “完整技能包”的含义

仓库包含两个技能的全部人工维护内容：

- SKILL 指令、references、schemas、启动器、UI 元数据、模板和运行源码；
- Python/Node 依赖声明与 npm lockfile；
- 安装、卸载、校验、CI 和维护工具。

仓库不会提交 `.venv`、`node_modules`、`__pycache__`、本机安装 manifest、论文、checkpoint 或生成的 deck。这些是不可移植的生成物；安装脚本会在目标电脑上重建依赖。因此这里发布的是可独立重建的完整源码包，而不是约 200 MB 的本机环境快照。

## 环境要求

- Windows 10/11；
- Windows PowerShell 5.1 或 PowerShell 7；
- Python 3.11+；
- Node.js 18+ 与 npm；
- Git 和 Codex。

Scholar-Slides 渲染还需要受支持的 Chrome、Edge 或 Chromium 环境及合适字体；以 `doctor --json` 的实际结果为准。

## 快速安装

```powershell
git clone https://github.com/mehran533727-commits/scholar-slides-paper-tutor.git
Set-Location scholar-slides-paper-tutor
.\scripts\install.ps1 -AddToPath
```

安装器默认拒绝覆盖已有技能。只有明确希望先生成时间戳备份再替换时，才使用 `-Force`。复制安装、跳过浏览器、升级和卸载说明见[安装文档](docs/INSTALLATION.md)。

安装后重启 Codex 并打开新终端：

```powershell
scholar-slides --version
scholar-slides doctor --json
```

预期版本为 `0.3.0`。长任务开始前必须确认 `doctor.ok = true`。

## 可直接复制的提示词

只做证据约束的论文阅读：

```text
请使用 scholar-slides 阅读这篇论文，解释问题、方法、实验和局限，先做到 CKPT-1，不制作 PPT。
```

使用已审核产物做深入教学：

```text
请使用 paper-tutor 的 Integrated Mode 读取这个匹配的 Scholar-Slides 项目，以 deep 深度讲解方法和关键实验，严格区分 Paper Facts、Tutor Explanation 和 Tutor Analysis。
```

制作经过审核的汇报：

```text
请使用 scholar-slides 制作约 12 页 journal-club 汇报，让关键定量结果在页面中可见，并在导出前完整走显式 CKPT-1 和 CKPT-2。
```

## 仓库结构

```text
skills/scholar-slides/   Scholar-Slides 完整技能与运行源码
skills/paper-tutor/      Paper-Tutor 完整技能、UI 元数据和 references
scripts/                 受保护的安装、卸载与包校验工具
tests/                   包结构与安装安全测试
docs/                    安装、组合、证据、架构与发布文档
```

## 文档索引

- [安装、升级和卸载](docs/INSTALLATION.md)
- [两个技能如何搭配](docs/INTEGRATION_GUIDE.md)
- [架构说明](docs/ARCHITECTURE.md)
- [证据与 checkpoint 安全](docs/EVIDENCE_SAFETY.md)
- [开发和验证](docs/DEVELOPMENT.md)
- [发布检查表](docs/RELEASE_CHECKLIST.md)
- [安全策略](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)
- [许可证与署名](LICENSES.md)

原始技能合同见 [Scholar-Slides SKILL.md](skills/scholar-slides/SKILL.md) 和 [Paper-Tutor SKILL.md](skills/paper-tutor/SKILL.md)。

## 许可证状态

Scholar-Slides 保留现有 MIT License 和 `Copyright (c) 2026 louwill`。Paper-Tutor 与仓库级整合文档当前没有单独声明开源许可证；不能把 Scholar-Slides 的 MIT 条款自动套到所有文件。详见 [LICENSES.md](LICENSES.md) 与 [NOTICE](NOTICE)。
