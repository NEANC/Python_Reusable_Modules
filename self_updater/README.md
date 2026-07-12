# Python SelfUpdater

基于 M9A_Update_Assistant 项目实践的 Python 程序自我更新模块；
为 Nuitka / PyInstaller 打包的 Windows exe 可执行文件提供：

- **从 GitHub Release 获取新版 → 下载校验 → PowerShell 热替换 → 失败回滚**

---

## 特性

- **零侵入**：仅需复制 `self_updater/` 目录，不依赖任何项目内部模块
- **热替换**：通过 PowerShell 脚本在程序退出后完成文件替换，无需额外守护进程
- **多层安全**：SHA256 校验（下载缓存 + 替换前 + 替换后）、自身完整性校验、新版健康检查
- **自动回滚**：替换失败或验证不通过时自动恢复旧版，重试耗尽后禁用该版本
- **版本语义**：支持 `alpha` / `beta` / `rc` 预发布版本比较，构建版本自动跳过
- **可注入**：下载函数可通过构造函数注入（对接 tqdm、自定义协议等）

---

## 依赖

```
requests>=2.32
tqdm>=4.67
colorama>=0.4.6
```

---

## 文件结构

```
self_updater/
├── __init__.py        # 包入口，重导出公开 API
├── self_utils.py      # 工具函数（版本比较、SHA256、打包检测）
├── self_config.py     # UpdateState（INI 状态文件管理）
├── self_updater.py    # SelfUpdater 核心类与脚本生成流程
├── ps1_fragments.py   # PowerShell 脚本片段生成函数
├── self_progress.py   # tqdm 进度条 + colorama 颜色封装
└── README.md
```

---

## 快速开始

```python
import logging
import os
import sys
from self_updater import SelfUpdater, detect_package_type

# 1. 检测运行环境
is_bundled, package_type = detect_package_type()

# 2. 创建更新器
cache_root = os.getenv("LOCALAPPDATA")
temp_folder = os.path.join(cache_root, "MyApp", "SelfUpdate") if cache_root else None

logger = logging.getLogger("MyApp")
updater = SelfUpdater(
    github_repo="you/your-repo",                                      # GitHub 仓库
    asset_pattern=r"^MyApp-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",    # exe 文件名正则
    app_name="MyApp",                                                 # 安全应用标识
    current_version="v1.0.0",                                         # 当前版本号
    proxy="",                                                         # HTTP 代理（留空则不使用）
    temp_folder=temp_folder,                                          # 可选：基础运行时目录（不传则自动解析）
    logger=logger,
    self_update_channel="preview",                                    # "preview" 或 "stable"
    is_bundled=is_bundled,                                            # 可选：预检测结果
    package_type=package_type,                                        # 可选：打包方式
)

# 如需自定义进度条，可传入 download_func=(url, save_path) -> bool。

# 3. 检查并准备更新
need_exit = updater.check_self_update()       # 普通升级（仅当远端更新时升级）
# need_exit = updater.check_self_update(force=True)  # 强制升级（跳过版本比较）

if need_exit:
    sys.exit(0)  # 退出程序，由 PowerShell 接管完成替换
```

### 与 argparse 集成

```python
import argparse
import logging
import os
import sys
from self_updater import SelfUpdater, detect_package_type

APP_VERSION = "v1.0.0"
cache_root = os.getenv("LOCALAPPDATA")
temp_folder = os.path.join(cache_root, "YourApp", "SelfUpdate") if cache_root else None
logger = logging.getLogger("YourApp")

# ── 命令行参数 ──
parser = argparse.ArgumentParser()
parser.add_argument("--update", "--Update", action="store_true",
                    help="仅检查自身更新")
parser.add_argument("--update-force", "--Update-force", "--Update-Force",
                    action="store_true", dest="update_force",
                    help="强制更新自身到最新版本")

# ── 内部参数（必须添加，否则 PS1 脚本启动新版时参数不被识别） ──
parser.add_argument("--self-update-verify", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--expected-sha256", type=str, default="", help=argparse.SUPPRESS)
parser.add_argument("--expected-version", type=str, default="", help=argparse.SUPPRESS)
parser.add_argument("--retry-update", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--update-failed", action="store_true", help=argparse.SUPPRESS)

args = parser.parse_args()

# ── 新版验证模式（PS1 替换后触发） ──
if args.self_update_verify:
    exit_code = SelfUpdater.self_update_verify(
        expected_sha256=args.expected_sha256,
        expected_version=args.expected_version,
        version_func=lambda: APP_VERSION,  # 返回应用内部版本号；不传则仅校验 SHA256
    )
    sys.exit(exit_code)

# ── 重试更新模式（PS1 回滚后触发） ──
if args.retry_update:
    logger.info("正在重试自更新...")
    is_bundled, package_type = detect_package_type()
    updater = SelfUpdater(
        github_repo="you/your-repo",
        asset_pattern=r'^YourApp-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$',
        app_name="YourApp",
        current_version=APP_VERSION,
        proxy="",
        # 使用环境变量解析出的基础运行时目录；传 None 时由 SelfUpdater 自动解析
        temp_folder=temp_folder,
        logger=logger,
        is_bundled=is_bundled,
        package_type=package_type,
    )
    need_exit = updater.check_self_update()
    if need_exit:
        sys.exit(0)
    logger.error("重试更新失败，无法获取新版本")
    sys.exit(1)

# ── 更新失败模式（PS1 回滚耗尽后触发） ──
if args.update_failed:
    from self_updater import UpdateState
    state = UpdateState.load()
    if state:
        failed_ver = state["new_version"]
        logger.critical(f"自更新失败：版本 {failed_ver} 多次验证不通过")
        print(f"\n软件自动更新失败，版本 {failed_ver} 已被标记为不可用。")
        print("已回退到旧版本，后续将跳过该版本的自动更新。")
    else:
        logger.critical("自更新失败，但无法读取状态信息")
    input("\n按任意键退出...")
    sys.exit(1)

# ── 仅检查自身更新 / 强制更新模式 ──
if args.update or args.update_force:
    is_bundled, package_type = detect_package_type()
    updater = SelfUpdater(
        github_repo="you/your-repo",
        asset_pattern=r'^YourApp-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$',
        app_name="YourApp",
        current_version=APP_VERSION,
        proxy="",
        # 使用环境变量解析出的基础运行时目录；传 None 时由 SelfUpdater 自动解析
        temp_folder=temp_folder,
        logger=logger,
        is_bundled=is_bundled,
        package_type=package_type,
    )
    if updater.check_self_update(force=args.update_force):
        logger.info("已将新版本下载到运行时目录，即将退出以完成更新...")
        sys.exit(0)
    input("\n按任意键退出...")
    sys.exit(0)

# ── 正常启动：清理上次更新残留 ──
SelfUpdater._cleanup_update_residue(logger)
```

---

## API 参考

### `SelfUpdater.__init__(...)`

| 参数                  | 类型                 | 必填 | 说明                                                    |
| --------------------- | -------------------- | ---- | ------------------------------------------------------- |
| `github_repo`         | `str`                | 是   | GitHub 仓库，格式 `"owner/repo"`                        |
| `asset_pattern`       | `str`                | 是   | exe asset 文件名正则，编译为 `re.compile()`             |
| `app_name`            | `str`                | 是   | 安全应用标识，用于 PS1 脚本名、缓存目录名和 User-Agent |
| `current_version`     | `str`                | 是   | 当前版本号（如 `"v1.0.0"`）                             |
| `proxy`               | `str`                | 是   | HTTP/HTTPS 代理地址，留空 `""` 则不使用                 |
| `temp_folder`         | `str`                | 否   | 基础运行时目录；不传则默认 `%LOCALAPPDATA%\\{app}\\SelfUpdate`，不可用时回退到 `program_dir\\SelfUpdate` |
| `logger`              | `logging.Logger`     | 是   | 日志记录器                                              |
| `download_func`       | `(str, str) -> bool` | 否   | 自定义下载函数 `(url, save_path) -> bool`               |
| `self_update_channel` | `str`                | 否   | 更新通道：`"preview"`（默认，兼容旧值 `"release"`）或 `"stable"`（兼容旧值 `"latest"`） |
| `is_bundled`          | `bool`               | 否   | 预检测的打包标记，避免重复调用 `detect_package_type()`  |
| `package_type`        | `str`                | 否   | 预检测的打包方式：`"Nuitka"` 或 `"PyInstaller"`         |

### `SelfUpdater.check_self_update(force=False) -> bool`

检查并准备自身更新。返回 `True` 表示已准备好，调用方应立即 `sys.exit(0)`。

| 参数    | 类型   | 说明                                    |
| ------- | ------ | --------------------------------------- |
| `force` | `bool` | `True` 时跳过版本比较，强制升级到最新版 |

### `SelfUpdater.self_update_verify(...) -> int`

新版程序启动后的健康检查（通常由 `--self-update-verify` 参数触发）。

默认校验 SHA256。未传 version_func 时仅校验 SHA256；如需校验应用内部版本号，调用方需要传入 `version_func`。

返回值：`0` 通过，`1` 缺少 SHA256，`2` SHA256 不匹配，`3` 版本号不匹配。

### `SelfUpdater.clean_update_cache(...)`

清理自更新下载缓存 `UpdateCache/` 目录。

### `SelfUpdater._cleanup_update_residue(logger)`

清理上次成功更新后的残留文件。清理逻辑会读取程序目录中的 `update_state.ini`，按其中记录的绝对路径删除 `runtime_dir` 内的 PS1 脚本、lock、新版暂存文件和旧版备份文件；`update.log` 会保留，最后删除 `update_state.ini`。
在程序正常启动时调用，确保 exe 目录保持整洁。

### `SelfUpdater.rollback(logger=None) -> bool`

从 `update_state.ini` 读取备份路径，手动恢复旧版。

---

## 工具函数 (`self_updater.self_utils`)

| 函数                                  | 说明                                        |
| ------------------------------------- | ------------------------------------------- |
| `calculate_sha256(file_path)`         | 计算文件 SHA256                             |
| `detect_package_type()`               | 检测运行环境（Nuitka / PyInstaller / 源码） |
| `version_to_tuple(v)`                 | `"v1.2.3"` → `(1, 2, 3)`                    |
| `version_newer_than(current, latest)` | `latest` 是否比 `current` 新                |
| `is_prerelease(v)`                    | 是否为 alpha/beta/rc 版本                   |
| `is_build_tag(v)`                     | 是否为构建版本                              |
| `get_exe_path()`                      | 获取当前 exe/脚本路径                       |

---

## 进度条 (`self_updater.self_progress`)

| 常量/函数                                | 说明                         |
| ---------------------------------------- | ---------------------------- |
| `BAR_FG`                                 | 进度条主体颜色（白色加粗）   |
| `BAR_AUX`                                | 辅助信息颜色（浅灰色）       |
| `BAR_OK`                                 | 完成状态颜色（亮绿色）       |
| `BAR_WARN`                               | 警告状态颜色（亮黄色加粗）   |
| `BAR_ERR`                                | 错误/失败颜色（亮红色加粗）  |
| `BAR_RST`                                | 颜色重置                     |
| `BAR_FORMAT`                             | 统一进度条格式字符串         |
| `create_progress_bar(total, desc)`       | 创建统一风格的 tqdm 进度条   |
| `format_ok(action, source, dest, total)` | 格式化完成消息（亮绿色）     |
| `format_error(desc, reason)`             | 格式化错误消息（亮红色加粗） |
| `format_warn(msg)`                       | 格式化警告消息（亮黄色加粗） |

## PowerShell 片段 (`self_updater.ps1_fragments`)

`ps1_fragments.py` 负责生成 Helper.ps1 和 Update.ps1 复用的 PowerShell 函数片段。该模块是内部实现细节，不属于公开 API。

| 函数 | 说明 |
| ---- | ---- |
| `generate_common_base_functions_ps1()` | 生成 `Normalize-IniValue`、`Assert-NotEmpty`、`Write-Log`。 |
| `generate_common_state_functions_ps1()` | 生成 `Read-IniValue`、`Write-IniValue`、`Set-UpdateStatus`。 |
| `generate_move_with_retry_ps1()` | 生成 `Move-WithRetry`。 |
| `generate_sha256_function_ps1()` | 生成 `Get-SHA256`，支持 SHA256 多路径回退。 |
| `generate_helper_argument_functions_ps1()` | 生成 Helper 专用的 `Quote-Arg`。 |
| `generate_helper_retry_functions_ps1()` | 生成 Helper 专用的 `Get-RetryOrDefault`。 |
| `generate_helper_file_cleanup_functions_ps1()` | 生成 Helper 专用的 `Remove-WithRetry`。 |
| `generate_helper_lifecycle_functions_ps1()` | 生成 `Commit-Update`、`Restore-Backup`、`Start-ProcWait`、`Start-NormalAppVisible`。 |

生成脚本时，`SelfUpdater` 只负责脚本头、变量初始化、片段拼接、主流程和写文件逻辑。共享函数会同时进入 Helper.ps1 和 Update.ps1；Helper 专用函数只进入 Helper.ps1。

---

## 工作流程

```
check_self_update()
├── 检测运行环境（打包/源码）
├── 从 GitHub API 获取最新 release
├── 版本比较（支持 pre-release 语义）
├── 检查失败禁用状态（跳过已标记的坏版本）
├── 自身完整性校验（当前 exe SHA256 对比 GitHub 记录）
├── 下载新版本 exe → SHA256 校验 → 下载缓存
└── _replace_executable()
    ├── 解析程序目录文件：update_state.ini、update.log
    ├── 解析 runtime_dir：temp_folder\\{version}
    ├── 暂存新版本 exe 到 runtime_dir
    ├── 生成 {app}_Update_Helper.ps1 到 runtime_dir
    ├── 生成 {app}_Update.ps1 到 runtime_dir
    ├── 写入 update_state.ini 状态文件（包含绝对运行时路径）
    └── 启动 PowerShell → 等待 runtime_dir 中的 lock 文件确认 → 返回 True
```

PowerShell 接管后的流程：

```
Helper.ps1:
├── 等待主程序退出 (Wait-Process)
├── 调用 Update.ps1 替换文件 (app.exe → backup.exe, new.exe → app.exe)
├── 替换后 SHA256 校验
├── 启动新版程序 --self-update-verify
│   ├── SHA256 自检
│   └── 版本号自检（需调用方传入 version_func）
├── 验证通过 → 提交 (Commit-Update) → 启动新版
└── 验证失败 → 回滚 (Restore-Backup) → 重试 / 禁用
```

Update.ps1 负责文件替换：

```
Update.ps1:
├── 读取程序目录 update_state.ini 中的 target、new_file、backup_file、new_sha256
├── 校验 runtime_dir 中的 new_file 存在且路径互不相同
├── 校验新文件 SHA256
├── 删除 runtime_dir 中的旧 backup 文件
├── 移动 target → backup_file
└── 移动 new_file → target
```

### PowerShell SHA256 回退

Helper.ps1 和 Update.ps1 都通过 `Get-SHA256($filePath)` 计算文件哈希。该函数按以下顺序尝试：

1. `.NET`：`[System.Security.Cryptography.SHA256]::Create()` + `[System.IO.File]::OpenRead()`。
2. `Get-FileHash`：先用 `Get-Command Get-FileHash` 探测可用性，再用 `-LiteralPath` 计算。
3. `certutil.exe -hashfile`：解析输出中的 64 位 SHA256 十六进制字符串。
4. 全部失败：抛出 `Get-SHA256 failed: ...`，不会跳过校验。

这几种路径输出统一为小写、无分隔符的 64 位 SHA256 字符串。

---

## 状态文件与运行时目录

更新过程中会使用程序目录和 `runtime_dir` 两类位置：

- **程序目录（`program_dir`）：** 当前 exe 所在目录，只保留 `update_state.ini` 和 `update.log`。
- **基础运行时目录（`temp_folder`）：** 不传 `temp_folder` 时，默认使用 `%LOCALAPPDATA%\\{app}\\SelfUpdate`；当 `%LOCALAPPDATA%` 不可用或目录无法创建时，回退到 `program_dir\\SelfUpdate`。不会使用 `%TEMP%` 作为回退目录。
- **版本运行时目录（`runtime_dir`）：** `runtime_dir = temp_folder\\{version}`，PS1 脚本、lock、新版暂存文件和旧版备份文件均写入此目录。

| 文件 | 位置 | 说明 |
| ---- | ---- | ---- |
| `update_state.ini` | 程序目录 | 状态机状态、版本、重试次数、目标文件路径以及 `runtime_dir`、PS1、lock、新版暂存文件、旧版备份文件等绝对运行时路径。 |
| `update.log` | 程序目录 | 更新过程日志，清理时保留。 |
| `{app}_Update_Helper.ps1` | `runtime_dir` | 协调脚本。 |
| `{app}_Update.ps1` | `runtime_dir` | 文件替换脚本。 |
| `update_started.lock` | `runtime_dir` | Helper 就绪信号。 |
| `{exe_stem}.new.exe` | `runtime_dir` | 新版 exe 暂存文件。 |
| `{exe_stem}.backup.exe` | `runtime_dir` | 旧版 exe 备份文件，用于校验失败或手动回滚。 |

正常情况下，更新成功后的下一次正常启动会按 `update_state.ini` 记录的绝对路径清理 `runtime_dir` 中的运行时文件；`update.log` 会保留，最后删除 `update_state.ini`。

---

## 注意事项

1. **仅支持 Windows**：PS1 脚本依赖 PowerShell 5.1 或更高版本。
2. **`app_name` 安全约束**：`app_name` 是安全应用标识，用于 PS1 脚本名、缓存目录和 User-Agent。仅允许 `A-Za-z0-9_.-`，且拒绝空字符串、路径分隔符、PowerShell/文件名危险字符、纯点号/首尾点号、Windows 保留设备名及其带扩展名形式（如 `CON`、`CON.txt`）。建议使用 `MyApp`、`my-app` 这类稳定标识，不要使用产品显示名。
3. **Asset 命名规范**：exe asset 文件名必须匹配给定的正则，且包含 `Nuitka` 或 `PyInstaller` 关键字。
4. **Release 要求**：Release 需提供对应 exe 的 SHA256；优先读取 asset `digest` 中的 `sha256:...`，否则从 release body 中匹配包含文件名的 64 位 SHA256。
5. **缓存机制**：下载的 exe 缓存到 `{temp_folder}/UpdateCache/installs/{version}/`，下次启动直接复用。
6. **运行时目录清理**：更新完成前不要手动清理 `runtime_dir`，否则可能导致替换、校验或回滚失败。
7. **失败禁用**：同一版本连续失败 3 次后标记为 `failed_disabled`，后续自动跳过该版本。
