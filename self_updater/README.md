# Python SelfUpdater

基于 M9A_Update_Assistant 项目实践的 Python 程序自我更新模块；
为 Nuitka / PyInstaller 打包的 Windows exe 可执行文件提供：

- **从 GitHub Release 获取新版 → 下载校验 → PowerShell 热替换 → 失败回滚**

## 特性

- **零侵入**：仅需复制 `self_updater/` 目录，不依赖任何项目内部模块
- **热替换**：通过 PowerShell 脚本在程序退出后完成文件替换，无需额外守护进程
- **多层安全**：SHA256 校验（下载缓存 + 替换前 + 替换后）、自身完整性校验、新版健康检查
- **自动回滚**：替换失败或验证不通过时自动恢复旧版，重试耗尽后禁用该版本
- **版本语义**：支持 `alpha` / `beta` / `rc` 预发布版本比较，构建版本自动跳过
- **可注入**：下载函数可通过构造函数注入（对接 tqdm、自定义协议等）

## 依赖

```
requests>=2.32
tqdm>=4.67
colorama>=0.4.6
```

## 文件结构

```
self_updater/
├── __init__.py       # 包入口，重导出所有公开 API
├── self_utils.py     # 工具函数（版本比较、SHA256、打包检测）
├── self_config.py    # UpdateState（INI 状态文件管理）
├── self_updater.py   # SelfUpdater 核心类
├── self_progress.py  # tqdm 进度条 + colorama 颜色封装
└── README.md
```

## 快速开始

```python
import logging
from self_updater import SelfUpdater, detect_package_type

# 1. 检测运行环境
is_bundled, package_type = detect_package_type()

# 2. 创建更新器
logger = logging.getLogger("MyApp")
updater = SelfUpdater(
    github_repo="you/your-repo",                                       # GitHub 仓库
    asset_pattern=r'^MyApp-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$',     # exe 文件名正则
    app_name="MyApp",                                                  # 应用名称
    current_version="v1.0.0",                                          # 当前版本号
    proxy="",                                                          # HTTP 代理（留空则不使用）
    temp_folder="/tmp/MyApp",                                          # 临时目录
    logger=logger,
    download_func=your_download_with_progress,                         # 可选：注入带进度条的下载函数
    self_update_channel="preview",                                     # 'preview' 或 'stable'
    is_bundled=is_bundled,                                             # 可选：预检测结果
    package_type=package_type,                                         # 可选：打包方式
)

# 3. 检查并准备更新
need_exit = updater.check_self_update()       # 普通升级（仅当远端更旧时升级）
# need_exit = updater.check_self_update(force=True)  # 强制升级（跳过版本比较）

if need_exit:
    sys.exit(0)  # 退出程序，由 PowerShell 接管完成替换
```

## API 参考

### `SelfUpdater.__init__(...)`

| 参数                  | 类型                 | 必填 | 说明                                                   |
| --------------------- | -------------------- | ---- | ------------------------------------------------------ |
| `github_repo`         | `str`                | 是   | GitHub 仓库，格式 `"owner/repo"`                       |
| `asset_pattern`       | `str`                | 是   | exe asset 文件名正则，编译为 `re.compile()`            |
| `app_name`            | `str`                | 是   | 应用名称，影响 PS1 脚本名和缓存目录名                  |
| `current_version`     | `str`                | 是   | 当前版本号（如 `"v1.0.0"`）                            |
| `proxy`               | `str`                | 是   | HTTP/HTTPS 代理地址，留空 `""` 则不使用                |
| `temp_folder`         | `str`                | 是   | 临时文件存储目录                                       |
| `logger`              | `logging.Logger`     | 是   | 日志记录器                                             |
| `download_func`       | `(str, str) -> bool` | 否   | 自定义下载函数 `(url, save_path) -> bool`              |
| `self_update_channel` | `str`                | 否   | 更新通道：`"preview"`（默认）或 `"stable"`             |
| `is_bundled`          | `bool`               | 否   | 预检测的打包标记，避免重复调用 `detect_package_type()` |
| `package_type`        | `str`                | 否   | 预检测的打包方式：`"Nuitka"` 或 `"PyInstaller"`        |

### `SelfUpdater.check_self_update(force=False) -> bool`

检查并准备自身更新。返回 `True` 表示已准备好，调用方应立即 `sys.exit(0)`。

| 参数    | 类型   | 说明                                    |
| ------- | ------ | --------------------------------------- |
| `force` | `bool` | `True` 时跳过版本比较，强制升级到最新版 |

### `SelfUpdater.self_update_verify(...) -> int`

新版程序启动后的健康检查（通常由 `--self-update-verify` 参数触发）。

返回值：`0` 通过，`1` 缺少 SHA256，`2` SHA256 不匹配，`3` 版本号不匹配。

### `SelfUpdater.clean_update_cache(...)`

清理自更新下载缓存 `UpdateCache/` 目录。

### `SelfUpdater.rollback(logger=None) -> bool`

从 `update_state.ini` 读取备份路径，手动恢复旧版。

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

## 进度条 (`self_updater.self_progress`)

| 常量/函数 | 说明 |
|---|---|
| `BAR_FG` | 进度条主体颜色（白色加粗） |
| `BAR_AUX` | 辅助信息颜色（浅灰色） |
| `BAR_OK` | 完成状态颜色（亮绿色） |
| `BAR_WARN` | 警告状态颜色（亮黄色加粗） |
| `BAR_ERR` | 错误/失败颜色（亮红色加粗） |
| `BAR_RST` | 颜色重置 |
| `BAR_FORMAT` | 统一进度条格式字符串 |
| `create_progress_bar(total, desc)` | 创建统一风格的 tqdm 进度条 |
| `format_ok(action, source, dest, total)` | 格式化完成消息（亮绿色） |
| `format_error(desc, reason)` | 格式化错误消息（亮红色加粗） |
| `format_warn(msg)` | 格式化警告消息（亮黄色加粗） |

## 工作流程

```
check_self_update()
├── 检测运行环境（打包/源码）
├── 从 GitHub API 获取最新 release
├── 版本比较（支持 pre-release 语义）
├── 检查失败禁用状态（跳过已标记的坏版本）
├── 自身完整性校验（当前 exe SHA256 对比 GitHub 记录）
├── 下载新版本 exe → SHA256 校验 → 缓存
└── _replace_executable()
    ├── 生成 {app}_Update_Helper.ps1
    ├── 生成 {app}_Update.ps1
    ├── 写入 update_state.ini 状态文件
    └── 启动 PowerShell → 等待 lock 文件确认 → 返回 True
```

PowerShell 接管后的流程：

```
Helper.ps1:
├── 等待主程序退出 (Wait-Process)
├── 调用 Update.ps1 替换文件 (app.exe → backup.exe, new.exe → app.exe)
├── 替换后 SHA256 校验
├── 启动新版程序 --self-update-verify
│   ├── SHA256 自检
│   └── 版本号自检
├── 验证通过 → 提交 (Commit-Update) → 启动新版
└── 验证失败 → 回滚 (Restore-Backup) → 重试 / 禁用
```

## 状态文件

更新过程中会在 exe 同目录生成以下文件：

| 文件                      | 说明                                             |
| ------------------------- | ------------------------------------------------ |
| `update_state.ini`        | 状态机状态（持久化重试计数、文件路径、版本号等） |
| `update_started.lock`     | Helper 就绪信号                                  |
| `update.log`              | PowerShell 日志                                  |
| `{app}_Update_Helper.ps1` | 协调脚本                                         |
| `{app}_Update.ps1`        | 文件替换脚本                                     |
| `*.backup.exe`            | 旧版备份                                         |
| `*.new.exe`               | 新版暂存                                         |

正常情况下更新成功后仅保留 exe，其余文件会被自动清理。

## 注意事项

1. **仅支持 Windows**：PS1 脚本依赖 PowerShell，不跨平台。
2. **Asset 命名规范**：exe asset 必须匹配给定的正则，且包含 `Nuitka` 或 `PyInstaller` 关键字。
3. **Release 要求**：每个 release 需在 body 中包含对应 exe 的 SHA256 值（否则跳过更新）。
4. **缓存机制**：下载的 exe 缓存到 `{temp_folder}/UpdateCache/installs/{version}/`，下次启动直接复用。
5. **失败禁用**：同一版本连续失败 3 次后标记为 `failed_disabled`，后续自动跳过该版本。
