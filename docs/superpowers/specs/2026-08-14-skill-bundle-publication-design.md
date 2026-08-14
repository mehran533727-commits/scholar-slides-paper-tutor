# Scholar-Slides + Paper-Tutor 技能包发布设计

日期：2026-08-14

## 1. 目标

在 GitHub 账户 `mehran533727-commits` 下创建公开仓库
`scholar-slides-paper-tutor`，发布一套可审阅、可安装、可验证的双技能组合：

- `scholar-slides` 负责从论文来源提取、约束和审核事实证据，并在需要时生成学术汇报；
- `paper-tutor` 只读消费匹配的 Scholar-Slides 产物，把证据约束的内容转化为教学型解释；
- 两者保持单向数据流，Paper-Tutor 的教学文本不得写回 Scholar-Slides、checkpoint 或演示文稿流程。

## 2. 发布形式

采用单一公开仓库，两个技能各自保持独立、可单独安装。仓库的第一版以当前安装版本为基线：

- Scholar-Slides：`0.3.0 Final Stable`；
- Paper-Tutor：当前本机技能版本，以其 `SKILL.md`、`agents/openai.yaml` 和四份 references 为完整定义。

公开仓库保存可重建的源代码，不保存机器生成的依赖目录或本机状态。`scholar-slides` 的
Python/Node 依赖由 requirements、`package.json` 和 `package-lock.json` 重建。

## 3. 仓库结构

```text
scholar-slides-paper-tutor/
├── README.md
├── README.zh-CN.md
├── LICENSES.md
├── NOTICE
├── SECURITY.md
├── CONTRIBUTING.md
├── .gitignore
├── .gitattributes
├── .github/workflows/validate.yml
├── docs/
│   ├── INSTALLATION.md
│   ├── INTEGRATION_GUIDE.md
│   ├── ARCHITECTURE.md
│   ├── EVIDENCE_SAFETY.md
│   ├── DEVELOPMENT.md
│   └── RELEASE_CHECKLIST.md
├── scripts/
│   ├── install.ps1
│   ├── uninstall.ps1
│   └── verify-package.ps1
└── skills/
    ├── scholar-slides/
    │   ├── SKILL.md
    │   ├── LICENSE
    │   ├── VERSION
    │   ├── bin/
    │   ├── references/
    │   ├── runtime/
    │   │   ├── assets/
    │   │   ├── scripts/
    │   │   ├── package.json
    │   │   ├── package-lock.json
    │   │   ├── pyproject.toml
    │   │   └── requirements*.txt
    │   └── schemas/
    └── paper-tutor/
        ├── SKILL.md
        ├── agents/openai.yaml
        └── references/
```

`docs/superpowers/specs/` 只保存本次设计记录，不作为运行时技能内容。

## 4. 收录与排除规则

### 收录

- 两个技能的全部人工维护指令、references、schemas、模板和运行源码；
- Scholar-Slides 的 Windows 启动器、Python 与 Node 依赖声明及 lockfile；
- Paper-Tutor 的 UI 元数据与全部验证场景；
- 安装、卸载、包验证、组合工作流、证据边界、维护和发布文档；
- 用于验证目录结构、YAML frontmatter、Markdown 链接、敏感路径和最小 CLI smoke test 的自动化检查。

### 排除

- `runtime/.venv/`、`runtime/node_modules/`、`__pycache__/`、`.pyc`；
- 本机论文、项目输出、checkpoint、deck、PDF、PPTX、截图和用户数据；
- 含本机绝对路径的已安装环境文件；
- 当前 `manifest.json`。该文件描述已安装的 200 MB 级 Windows 生产快照，并包含 `.venv` 与
  `node_modules` 的哈希，不适合作为源代码仓库 manifest；发布包改由 Git 和验证脚本界定源文件。

这里的“完整技能包”指能够从仓库独立安装并重建运行环境的完整源代码包，不指复制不可移植的
本机虚拟环境和依赖缓存。

## 5. 文档设计

`README.md` 提供英文主入口，`README.zh-CN.md` 提供完整中文入口，二者相互链接并覆盖：定位、
能力边界、快速安装、最小示例、双技能选择规则、兼容性和文档索引。

`docs/INTEGRATION_GUIDE.md` 是组合使用的核心文档，必须说明：

1. 只有阅读/制作用 Scholar-Slides，教学解释用 Paper-Tutor；
2. Integrated Mode 需要先匹配论文身份，再读取受信任的 Scholar-Slides 产物；
3. Standalone Mode 只基于可读 PDF，并明确披露未经 CKPT-1 验证；
4. 数据流只能是 `paper/source → Scholar-Slides artifacts → Paper-Tutor explanation`；
5. CKPT-1/CKPT-2 都必须由用户明确批准，安装或脚本不得代替批准；
6. 给出快速阅读、深入辅导、论文汇报三个可复制示例。

其余文档职责如下：

- `INSTALLATION.md`：系统要求、Windows 安装、手动安装、升级、验证和卸载；
- `ARCHITECTURE.md`：目录、运行时组件、checkpoint 与双技能关系；
- `EVIDENCE_SAFETY.md`：论文身份、证据优先级、冲突处理、claim labels 和禁止反向污染；
- `DEVELOPMENT.md`：依赖重建、代码/技能验证、版本同步和本地开发；
- `RELEASE_CHECKLIST.md`：发布前的版本、许可证、敏感信息、测试和 tag 检查；
- `SECURITY.md`：漏洞报告、外部 PDF/URL 风险和不得提交用户论文数据；
- `CONTRIBUTING.md`：贡献范围、测试要求和 checkpoint 安全不变量。

## 6. 安装和卸载

`scripts/install.ps1` 默认把两个技能安装到 `~/.agents/skills/`，也允许显式指定目标目录。安装流程：

1. 检查 Windows PowerShell、Git、Python 3.11+、Node.js 18+ 和 npm；
2. 复制两个干净技能目录；
3. 在 Scholar-Slides 目录创建技能本地 `.venv`；
4. 安装 `requirements-runtime.txt`；
5. 使用 `npm ci` 安装锁定的 Node 依赖；
6. 安装 Playwright Chromium；
7. 运行 `scholar-slides --version` 和 `doctor --json`；
8. 提示用户重启 Codex 以刷新技能发现。

安装脚本不得自动覆盖已有技能。发现目标已存在时，默认失败并给出备份/升级说明；只有显式
`-Force` 才允许替换，替换前创建带时间戳的备份。

`scripts/uninstall.ps1` 只删除本仓库安装的两个明确目标目录，并要求显式确认；不递归操作技能根目录。

## 7. 许可证与署名

- 原样保留 `skills/scholar-slides/LICENSE`：MIT License，Copyright (c) 2026 louwill；
- `NOTICE` 明确 Scholar-Slides 的来源署名和本仓库新增的整合/文档范围；
- `LICENSES.md` 按目录说明许可证状态；
- 不擅自为没有现成许可证文件的 Paper-Tutor 重新授权。第一版将其标记为“未单独声明许可证”，
  避免把 Scholar-Slides 的 MIT 条款误套到其他文件。后续只有版权人明确授权时才增加仓库级许可证。

## 8. 验证策略

发布前必须取得以下新鲜证据：

- 两个 `SKILL.md` 的 YAML frontmatter、名称和描述合法；
- Paper-Tutor 的 `agents/openai.yaml` 与技能名称一致；
- 所有 Markdown 相对链接有效；
- 仓库不存在 `.venv`、`node_modules`、缓存、论文产物和本机绝对路径；
- Python 文件通过 `compileall`，JSON 文件可解析，PowerShell 脚本可解析；
- Node 依赖可由 `npm ci` 重建，Python 依赖声明可安装；
- Scholar-Slides CLI 在干净安装副本中返回版本 `0.3.0`，`doctor --json` 输出可解析；
- Paper-Tutor 的现有 validation scenarios 保持完整，并用代表性场景验证组合文档没有改变单向边界；
- GitHub Actions 在 Windows 上运行包验证和 CLI smoke test。

如果完整 `doctor` 因浏览器、字体或可选 provider 只产生环境警告，文档和交付报告必须逐项披露；
任何运行时 `FAIL` 在发布前修复。

## 9. GitHub 发布

使用 GitHub CLI 在 `mehran533727-commits` 下创建公开仓库，默认分支为 `main`。本地提交顺序：

1. 设计说明；
2. 完整技能与文档实现；
3. 验证修正（仅在确有修正时）。

在正式 push 前复核 staged diff 和文件清单。首次发布直接推送 `main`，不创建没有评审价值的空 PR；
推送后通过 GitHub 插件读取仓库元数据和关键文件，确认远端可访问且 HEAD 与本地提交一致。

## 10. 验收条件

任务完成需同时满足：

- 新公开仓库存在，URL 可访问，默认分支为 `main`；
- 两个技能包独立完整，且 Scholar-Slides 可由源代码重建运行环境；
- README、组合指南、安装、架构、证据安全、开发、发布、安全和贡献文档齐全；
- 原许可证与署名得到保留，Paper-Tutor 的许可证状态没有被错误推断；
- 本地与 GitHub Actions 验证通过，或所有非阻断环境限制被准确记录；
- 远端提交 SHA 与本地最终 HEAD 一致。
