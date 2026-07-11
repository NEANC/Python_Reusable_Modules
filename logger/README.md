# LoggerManager

`logger` 是一个可移植的 Python 日志管理模块，提供控制台彩色输出、文件日志写入、旧日志清理和日志保存配置预读取能力。

## 特性

- **控制台彩色输出**：基于 colorama，仅作用于控制台日志。
- **默认不保存文件日志**：只调用 `setup_logger()` 时仅输出控制台日志，需要保存时再调用 `add_file_logger()`。
- **文件日志写入**：自动创建日志目录，按 `YYYYMMDD_HHMMSS` 时间戳生成 `.log` 文件。
- **旧日志清理**：默认最多保留 15 个、最多保留 7 天，按文件名前缀限定清理范围。
- **配置预读取**：在完整加载配置前读取 `save_enabled`，配置缺失、键缺失或解析失败时默认不保存。
- **低侵入**：不依赖项目内部模块，仅额外依赖 colorama；修改 `LOG_DIR` 和 `LOG_PREFIX` 即可适配其他项目。

## 依赖

```text
colorama>=0.4.6
```

## 文件结构

```text
logger/
├── __init__.py           # 包入口
├── logger_manager.py     # ColoredFormatter + setup_logger + 文件日志 + 清理
└── README.md
```

## 快速开始

```python
from logger import setup_logger, add_file_logger, cleanup_old_logs

# 1. 创建控制台日志记录器
logger = setup_logger("MyApp")

# 2. 可选：添加文件日志
add_file_logger(logger, version="v1.0.0")

# 3. 使用
logger.info("应用启动")
logger.debug("调试信息")
logger.warning("警告")
logger.error("错误")

# 4. 启动时清理旧日志
# max_days 默认值为 7；max_files 默认值为 15。
cleanup_old_logs(logger, max_files=5, max_days=7)
```

### 适配到你的项目

默认模块级常量位于 `logger_manager.py` 顶部：

```python
LOG_DIR = "logs"             # 日志文件夹
LOG_PREFIX = "M9A_Update"    # 日志文件名前缀
```

在默认配置下，文件日志名格式如下：

```text
logs/M9A_Update_YYYYMMDD_HHMMSS.log
```

示例：

```text
logs/M9A_Update_20260620_143000.log
```

也可以在函数调用时直接传入 `log_dir` 和 `log_prefix` 覆盖默认值：

```python
from logger.logger_manager import add_file_logger, cleanup_old_logs

add_file_logger(logger, version="v1.0.0", log_dir="my_logs", log_prefix="MyApp")
cleanup_old_logs(logger, max_files=10, max_days=7, log_dir="my_logs", log_prefix="MyApp")
```

此时文件日志名格式为：

```text
my_logs/MyApp_YYYYMMDD_HHMMSS.log
```

## API 参考

### `setup_logger(name="M9AUpdateAssistant") -> logging.Logger`

创建并配置控制台日志记录器，返回 `logging.Logger`。

行为说明：

- 默认 logger 名称为 `M9AUpdateAssistant`。
- logger 级别为 `DEBUG`。
- 控制台 handler 级别为 `INFO`，因此 `DEBUG` 日志默认不会输出到控制台。
- 控制台格式为 `%(levelname)s | %(asctime)s.%(msecs)03d | %(message)s`，时间格式为 `%H:%M:%S`。
- `logger.propagate = False`，避免日志继续向上级 logger 传播。
- 重复调用同一个 logger 时，会复用本模块创建的控制台 handler，避免控制台重复输出。

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | `str` | `"M9AUpdateAssistant"` | 日志记录器名称。 |

### `add_file_logger(logger, version="", log_dir=None, log_prefix=None) -> logging.FileHandler`

为已有 logger 添加文件日志 handler，返回 `logging.FileHandler`。

行为说明：

- `log_dir` 为空时使用模块级常量 `LOG_DIR`，默认值为 `logs`。
- `log_prefix` 为空时使用模块级常量 `LOG_PREFIX`，默认值为 `M9A_Update`。
- 会自动创建日志目录，支持嵌套目录。
- 文件日志名格式为 `{log_dir}/{log_prefix}_YYYYMMDD_HHMMSS.log`。
- 文件 handler 级别为 `DEBUG`。
- 文件日志格式为 `%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s`，时间格式为 `%Y-%m-%d %H:%M:%S`。
- 对同一个 `logger + log_dir + log_prefix` 组合重复调用时，会复用已有 file handler，避免日志重复写入。
- 只有在新建 file handler 且 `version` 非空时，才会写入一条 `DEBUG` 级别的版本日志：`当前软件版本: {version}`；复用已有 file handler 时不会重复写入版本信息。

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `logger` | `logging.Logger` | 必填 | 已有的日志记录器。 |
| `version` | `str` | `""` | 软件版本号；非空时写入文件日志。 |
| `log_dir` | `Optional[str]` | `None` | 日志文件夹；默认取模块级 `LOG_DIR`。 |
| `log_prefix` | `Optional[str]` | `None` | 文件名前缀；默认取模块级 `LOG_PREFIX`。 |

### `cleanup_old_logs(logger, max_files=15, max_days=7, log_dir=None, log_prefix=None) -> None`

按文件名前缀清理旧日志。

行为说明：

- `log_dir` 为空时使用模块级常量 `LOG_DIR`，默认值为 `logs`。
- `log_prefix` 为空时使用模块级常量 `LOG_PREFIX`，默认值为 `M9A_Update`。
- 仅清理 `log_dir` 中匹配 `{log_prefix}_*.log` 的文件，不影响其他前缀或其他扩展名的文件。
- 如果 `log_dir` 不存在，直接返回。
- 默认先删除超过 7 天的匹配日志，再在剩余文件中最多保留 15 个最新日志。
- `max_files=0` 表示数量清理阶段不保留任何剩余匹配日志。
- `max_days=0` 表示日期清理阶段不保留任何已过当前时间的匹配日志，通常会删除已有匹配日志。
- `max_files` 或 `max_days` 为负数时抛出 `ValueError`。
- 删除成功会写入 `DEBUG` 日志；本次有文件被删除时，会写入一条 `INFO` 汇总日志。

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `logger` | `logging.Logger` | 必填 | 日志记录器。 |
| `max_files` | `int` | `15` | 最大保留数量。 |
| `max_days` | `int` | `7` | 最大保留天数。 |
| `log_dir` | `Optional[str]` | `None` | 日志文件夹；默认取模块级 `LOG_DIR`。 |
| `log_prefix` | `Optional[str]` | `None` | 文件名前缀；默认取模块级 `LOG_PREFIX`。 |

### `raw_read_save_enabled(config_file, section="Logs", key="save_enabled") -> bool`

在完整加载配置前，快速读取配置文件，判断是否启用日志保存。

行为说明：

- 配置文件不存在时返回 `False`。
- 配置节缺失、配置键缺失或解析失败时返回 `False`。
- 使用 `ConfigParser.getboolean()` 解析布尔值，因此支持 INI 布尔值写法，例如 `true`、`false`、`yes`、`no`、`1`、`0`。
- 读取编码为 UTF-8。

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `config_file` | `str` | 必填 | 配置文件路径。 |
| `section` | `str` | `"Logs"` | 配置节名。 |
| `key` | `str` | `"save_enabled"` | 配置键名。 |

## `ColoredFormatter`

`ColoredFormatter` 是自定义 `logging.Formatter` 子类，用于输出带颜色的日志行。当前模块仅在 `setup_logger()` 创建的控制台 handler 中使用它，因此颜色只作用于控制台输出，不作用于文件日志。

颜色映射如下：

| 级别 | 颜色 |
| --- | --- |
| `DEBUG` | 青色 |
| `INFO` | 白色 |
| `WARNING` | 黄色 |
| `ERROR` | 红色 |
| `CRITICAL` | 红底 + 黑字 + 加粗 |

未命中的日志级别默认使用白色。`ColoredFormatter` 初始化时会调用 `colorama.init(autoreset=True)`，并在格式化结果末尾追加 `Style.RESET_ALL`。
