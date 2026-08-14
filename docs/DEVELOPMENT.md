# 开发与验证

## 原则

- 保持两个技能目录独立；
- 不修改 checkpoint 人工审批语义；
- 不把 Paper-Tutor 教学内容加入 Scholar-Slides；
- 不提交生成依赖、论文或用户项目；
- 对安装/验证行为先写失败测试，再做最小实现；
- 对技能规则变更使用 `references/validation-scenarios.md` 做前向场景测试。

## 本地检查

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -s tests -v
pwsh -NoProfile -File scripts/verify-package.ps1
```

验证器会检查结构、metadata、JSON、Markdown 链接、版本、许可证署名、禁止文件、敏感路径和疑似 token；PowerShell wrapper 还检查 Python 语法与 PowerShell AST。

## 依赖重建

不要在仓库中提交 `.venv` 或 `node_modules`。需要测试 Scholar-Slides 时使用临时安装：

```powershell
$temporarySkills = Join-Path ([IO.Path]::GetTempPath()) ("skill-test-" + [Guid]::NewGuid())
.\scripts\install.ps1 -DestinationRoot $temporarySkills -SkipBrowser
& "$temporarySkills\scholar-slides\bin\scholar-slides.ps1" --version
& "$temporarySkills\scholar-slides\bin\scholar-slides.ps1" doctor --json
```

验证后只删除这个已确认位于系统临时目录中的唯一测试目录。

## 更新 Scholar-Slides 源

1. 从可信安装或正式源获取新版本；
2. 复制人工维护文件，排除 `.venv`、`node_modules`、cache 和 installed manifest；
3. 同步 `skills/scholar-slides/VERSION`、`runtime/VERSION`、`pyproject.toml`、`package.json` 与 lockfile；
4. 保留原 `LICENSE`；
5. 运行全套验证和干净安装；
6. 更新 README、安装文档和 release checklist 中的版本要求。

## 更新 Paper-Tutor

1. 先运行或记录没有新规则时的行为基线；
2. 只修改解决已观察问题的最小 instruction/reference；
3. 保持 `SKILL.md` 的 `name` 和 `agents/openai.yaml` 一致；
4. 运行相关 validation scenarios，特别是 evidence conflict、Standalone disclosure、caption-only overview、reverse contamination 和 full-document contract；
5. 不以测试为理由削弱证据或单向边界。

## 场景验证

Paper-Tutor 的场景文件不是标准答案，而是 fixture、用户请求和可观察 pass criteria。至少覆盖：

- Integrated source conflict；
- Standalone CKPT-1 disclosure；
- Formula/Figure/Table 明确槽位；
- 禁止反向污染；
- 完整 `paper-tutor.md` 的 0–17 章节与 Claim → Evidence Appendix。

选择内联执行时，主代理可以逐条审查这些合同，但这不等价于独立 fresh-context agent 的前向测试；发布说明应如实标注验证方式。

## 提交规范

每个提交保持一个可审阅主题，例如：

```text
test: define skill bundle packaging contracts
feat: add guarded Windows installation scripts
docs: explain installation and combined paper workflow
ci: validate skill package on Windows
```

提交前运行 `git diff --check`，确认没有无关文件或生成目录。
