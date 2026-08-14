# 发布检查表

## 范围

- [ ] 两个 `skills/*/SKILL.md` 存在且 frontmatter 合法；
- [ ] Scholar-Slides runtime source、assets、schemas、references 和 launcher 齐全；
- [ ] Paper-Tutor 的四份 references 与 `agents/openai.yaml` 齐全；
- [ ] `.venv`、`node_modules`、`__pycache__`、`.pyc` 和 installed `manifest.json` 未被跟踪；
- [ ] 没有论文、项目、checkpoint、PPTX、PDF 或用户输出。

## 版本与依赖

- [ ] `skills/scholar-slides/VERSION` 为 `0.3.0`；
- [ ] `runtime/VERSION`、`pyproject.toml`、`package.json` 版本一致；
- [ ] `package-lock.json` 与 `package.json` 一致，`npm ci` 成功；
- [ ] `requirements-runtime.txt` 能在干净 Python 3.11+ 环境安装；
- [ ] CLI `--version` 返回 `0.3.0`；
- [ ] `doctor --json` 可解析且 `ok = true`，否则阻断发布。

## 文档与证据合同

- [ ] README 中英文入口、安装、组合、架构、证据、安全、开发和贡献文档齐全；
- [ ] 所有 Markdown 相对链接有效；
- [ ] Integrated/Standalone 状态块与 Paper-Tutor 合同一致；
- [ ] 证据优先级、论文身份匹配和四类 claim labels 未被弱化；
- [ ] CKPT-1/CKPT-2 仍要求用户显式批准；
- [ ] Paper-Tutor → Scholar-Slides 的反向写入仍被禁止；
- [ ] 代表性 validation scenarios 已按发布说明中的方式审查。

## 许可证与安全

- [ ] Scholar-Slides MIT License 与 `Copyright (c) 2026 louwill` 原样保留；
- [ ] `LICENSES.md` 没有把 MIT 错套到 Paper-Tutor；
- [ ] `NOTICE` 清楚区分原技能与仓库级整合；
- [ ] 没有本机绝对路径、访问 token、API key、邮箱或凭据；
- [ ] 没有达到 GitHub 100 MB 限制的文件；
- [ ] `SECURITY.md` 中的报告和论文数据处理原则仍有效。

## 验证与发布

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -s tests -v
pwsh -NoProfile -File scripts/verify-package.ps1
git diff --check
git status -sb
```

- [ ] 临时目标中的完整安装成功；
- [ ] 版本和 doctor 检查成功；
- [ ] 最终工作树干净；
- [ ] GitHub CLI 当前账户与批准 owner 完全一致；
- [ ] 远端 visibility 为 `PUBLIC`、default branch 为 `main`；
- [ ] `origin/main` SHA 与本地 HEAD 一致；
- [ ] GitHub Actions 最新 `validate` run 成功；
- [ ] GitHub 插件能读取仓库与关键文件。
