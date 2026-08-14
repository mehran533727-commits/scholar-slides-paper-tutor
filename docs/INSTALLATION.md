# 安装、升级与卸载

## 支持范围

本仓库提供经过验证的 Windows 安装流程。安装器支持 Windows PowerShell 5.1 和 PowerShell 7，并要求：

- Python 3.11+；
- Node.js 18+ 与 npm；
- Git；
- Chrome、Edge 或由 Playwright 安装的 Chromium；
- 用于中文输出时具备 CJK 字体。

安装脚本不使用管理员权限，不调用 `sudo`，不修改 Codex 配置，不自动批准任何 checkpoint。

## 标准安装

```powershell
git clone https://github.com/mehran533727-commits/scholar-slides-paper-tutor.git
Set-Location scholar-slides-paper-tutor
.\scripts\install.ps1 -AddToPath
```

默认目标是当前用户的 `.agents\skills` 目录。安装器按以下顺序工作：

1. 检查两个技能源目录；
2. 默认拒绝覆盖已有目标；
3. 在目标根目录的唯一 staging 目录中复制技能；
4. 创建 Scholar-Slides 技能本地 `.venv`；
5. 安装 `requirements-runtime.txt` 与 `package-lock.json` 锁定的 Node 依赖；
6. 安装 Playwright Chromium；
7. 运行版本检查和 `doctor --json`；
8. 验证成功后再激活两个技能目录。

`-AddToPath` 把 Scholar-Slides 的 `bin` 目录加入用户 PATH。PATH 变化只对新终端生效。

## 安装到其他技能根目录

```powershell
.\scripts\install.ps1 -DestinationRoot "D:\Codex\skills" -AddToPath
```

`DestinationRoot` 只表示两个技能目录的父目录；安装器只创建或替换其直接子目录 `scholar-slides` 和 `paper-tutor`。

## 复制安装，不重建依赖

```powershell
.\scripts\install.ps1 -SkipDependencies
```

该模式用于检查文件、离线复制或由高级用户手动提供运行环境。此时 Scholar-Slides 启动器会在 `.venv` 不存在时明确失败；不能把 copy-only 状态描述为可运行安装。

## 跳过 Playwright 浏览器下载

```powershell
.\scripts\install.ps1 -SkipBrowser
```

Python 和 Node 依赖仍会安装。仅当系统已有受支持的浏览器或你计划随后运行 Playwright 安装时使用。最终状态以 `doctor --json` 为准。

## 升级或替换已有技能

安装器默认 fail closed：

```text
Target already exists ... Re-run with -Force to back it up and replace it.
```

明确希望替换时：

```powershell
.\scripts\install.ps1 -Force -AddToPath
```

旧目录先移动到：

```text
<DestinationRoot>\.skill-backups\<timestamp-id>\scholar-slides
<DestinationRoot>\.skill-backups\<timestamp-id>\paper-tutor
```

备份不会由安装器或卸载器自动删除。

## 安装后验证

重启 Codex 并打开新终端：

```powershell
scholar-slides --version
scholar-slides doctor --json
```

预期：

```text
version = 0.3.0
doctor.ok = true
```

如果没有使用 `-AddToPath`，可以直接调用：

```powershell
& "$HOME\.agents\skills\scholar-slides\bin\scholar-slides.ps1" --version
& "$HOME\.agents\skills\scholar-slides\bin\scholar-slides.ps1" doctor --json
```

`doctor` 的 `FAIL` 必须解决后再运行长任务；`WARN` 要作为环境限制报告。安装发现与当前 Codex 会话不是热重载，因此必须重启 Codex。

## 手动安装依赖

如果先使用了 `-SkipDependencies`：

```powershell
$runtime = "$HOME\.agents\skills\scholar-slides\runtime"
python -m venv "$runtime\.venv"
& "$runtime\.venv\Scripts\python.exe" -m pip install -r "$runtime\requirements-runtime.txt"
Push-Location $runtime
npm ci
npx playwright install chromium
Pop-Location
```

随后运行版本和 doctor 检查。

## 卸载

卸载必须显式确认：

```powershell
.\scripts\uninstall.ps1 -ConfirmRemoval
```

如果标准安装时加入了 PATH：

```powershell
.\scripts\uninstall.ps1 -ConfirmRemoval -RemoveFromPath
```

指定过其他目标时必须传回同一个根目录：

```powershell
.\scripts\uninstall.ps1 -DestinationRoot "D:\Codex\skills" -ConfirmRemoval
```

卸载器只删除两个经过路径边界检查的直接子目录，不删除目标根目录、其他技能或 `.skill-backups`。

## 常见问题

### 命令找不到

打开新终端，或用完整的 `bin\scholar-slides.ps1` 路径调用。确认安装时使用了 `-AddToPath`。

### Python 或 Node 版本不满足

安装器会在 staging 阶段停止，现有技能不会被覆盖。升级到 Python 3.11+ 与 Node 18+ 后重试。

### `doctor.ok = false`

按报告修复浏览器、字体、依赖、联系方式或写权限。不能通过编辑 JSON 或跳过 doctor 来伪造就绪状态。更多信息见 [Scholar-Slides troubleshooting](../skills/scholar-slides/references/troubleshooting.md)。
