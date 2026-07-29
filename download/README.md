# Python DownloadManager

基于 `requests`、`tqdm` 与 `colorama` 的独立下载模块；
可被普通脚本直接调用，也可作为 `self_updater` 的默认 exe 下载能力。

- **HEAD 探测 → Range 分段 → 多线程下载 → 进度展示 → 失败回退**

---

## 特性

- **独立模块**：位于仓库根目录 `download/`，不依赖 `self_updater`
- **外部可用**：可直接 `from download import DownloadManager`
- **分段下载**：服务器支持 Range 时自动按区间拆分并多线程下载
- **断点续传**：复用目标文件或 `.partN` 分段临时文件继续下载
- **自动降级**：HEAD 探测失败、文件大小未知或不支持 Range 时使用内部单线程续传
- **进度展示**：使用下载专用 tqdm 进度条显示总量、ETA 和网络速度
- **代理支持**：通过 `requests` 的 `proxies` 参数复用 HTTP / HTTPS 代理配置

---

## 依赖

基础依赖：

```text
requests>=2.32
tqdm>=4.67
colorama>=0.4.6
```

---

## 文件结构

```text
download/
├── __init__.py   # 包入口，导出 DownloadManager
├── manager.py    # DownloadManager 下载管理器与分段下载实现
├── progress.py   # 下载专用 tqdm 进度条与消息格式化
└── README.md
```

---

## 快速开始

```python
import logging

from download import DownloadManager

logger = logging.getLogger("MyApp")

manager = DownloadManager(
    proxy="",                  # HTTP / HTTPS 代理；留空则不使用
    temp_folder="./SelfUpdate", # 调用方管理的临时目录或缓存目录
    logger=logger,
    download_threads=4,         # 支持 Range 时使用的最大分段线程数
)

ok = manager.download_file_with_progress(
    url="https://example.com/MyApp.exe",
    save_path="./downloads/MyApp.exe",
)

if not ok:
    logger.error("下载失败")
```

`DownloadManager` 只负责下载文件，不负责业务校验。`self_updater` 使用该模块下载 exe 后，仍由自身流程继续执行 SHA256 校验、替换和回滚。

---

## API 参考

### `DownloadManager.__init__(...)`

| 参数               | 类型             | 必填 | 说明                                                     |
| ------------------ | ---------------- | ---- | -------------------------------------------------------- |
| `proxy`            | `str`            | 是   | HTTP / HTTPS 代理地址，留空 `""` 则不使用               |
| `temp_folder`      | `str`            | 是   | 调用方传入的临时目录；当前下载实现会保存该值以供调用方识别 |
| `logger`           | `logging.Logger` | 是   | 日志记录器                                               |
| `download_threads` | `int`            | 否   | Range 分段下载线程数，默认 `4`                           |

### `DownloadManager.download_file_with_progress(url, save_path) -> bool`

下载文件并显示进度条。

| 参数        | 类型  | 说明       |
| ----------- | ----- | ---------- |
| `url`       | `str` | 下载 URL   |
| `save_path` | `str` | 保存路径   |

返回值：

- `True`：下载完成，目标文件已写入 `save_path`
- `False`：下载失败，调用方可按自身策略重试或中止

行为说明：

1. 自动创建 `save_path` 的父目录。
2. 先通过 HEAD 探测文件大小和 Range 支持情况。
3. 支持 Range 且文件大小有效时，按 `download_threads` 拆分为多个分段。
4. 不支持 Range、文件大小未知或 HEAD 探测失败时，使用内部单线程续传。
5. 下载过程中显示统一风格的进度条。
6. 下载失败时记录日志并返回 `False`，不抛出给调用方处理。

---

## 进度条 (`download.progress`)

| 常量/函数                                           | 说明                                  |
| --------------------------------------------------- | ------------------------------------- |
| `DownloadProgressBar`                               | 下载专用 tqdm 进度条，支持外部速度字段 |
| `create_download_progress_bar(total, desc)`         | 创建下载专用进度条                    |
| `format_ok(action, source, dest, total_bytes)`      | 格式化下载成功消息                    |
| `format_error(desc, reason)`                        | 格式化下载失败消息                    |
| `DOWNLOAD_BAR_FORMAT`                               | 下载进度条格式字符串                  |
| `BAR_FG` / `BAR_AUX` / `BAR_OK` / `BAR_ERR` / `BAR_RST` | 进度条和消息颜色常量              |

---

## 工作流程

```text
download_file_with_progress(url, save_path)
├── 创建目标文件父目录
├── HEAD 探测
│   ├── 读取 Content-Length
│   └── 判断 Accept-Ranges: bytes
├── 判断下载模式
│   ├── 支持 Range 且大小有效：多线程分段下载
│   │   ├── 生成 .partN 分段临时文件
│   │   ├── 每个分段内部重试
│   │   ├── 合并 part 文件到 save_path
│   │   └── 成功后删除 part 文件
│   └── 不支持 Range / 大小未知 / HEAD 失败：内部单线程续传
├── 更新下载进度条和速度字段
└── 成功返回 True，失败返回 False
```

---

## 与 self_updater 的关系

`download` 是独立模块，`self_updater` 是它的使用方。

`self_updater` 默认下载路径等价于：

```python
from download import DownloadManager

manager = DownloadManager(proxy=proxy, temp_folder=temp_folder, logger=logger)
manager.download_file_with_progress(url, save_path)
```

复制或引入 `self_updater` 默认下载能力时，需要同时包含根目录 `download/`。如果调用方传入 `download_func(url, save_path) -> bool` 完全覆盖默认下载行为，则可自行处理下载依赖。

---

## 注意事项

1. **独立边界**：`download` 不导入 `self_updater`，也不承担版本判断、SHA256 校验、替换或回滚职责。
2. **代理与 SOCKS 支持**：代理配置传给 `requests`，SOCKS 是否可用取决于当前 `requests` 环境是否安装了 SOCKS 支持。
3. **临时分段文件**：多线程模式会在目标路径旁创建 `.partN` 文件；下载成功后会清理，失败时会保留以便续传。
4. **内部单线程续传**：这是 `DownloadManager` 的降级路径，不是 `self_updater` 旧版默认下载实现。
5. **下载校验**：本模块只按文件大小判断下载完整性；如需哈希校验，应由调用方在下载成功后自行执行。
