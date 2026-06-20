# LoggerManager

基于 M9A_Update_Assistant 与 ALAS_Logs_Archive 项目实践的 Python 日志管理模块，提供：

- **控制台彩色输出 + 文件日志 + 旧日志自动清理**。

## 特性

- **控制台彩色**：基于 colorama，DEBUG 青色 / INFO 白色 / WARNING 黄色 / ERROR 红色 / CRITICAL 红底黑字
- **文件日志**：自动创建日志目录，按时间戳命名
- **旧日志清理**：超出数量上限的旧日志自动删除（按修改时间保留最新的）
- **零侵入**：不依赖项目内部模块，修改两个模块级常量即可适配

## 依赖

```
colorama>=0.4.6
```

## 文件结构

```
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
cleanup_old_logs(logger, max_files=5)
```

### 适配到你的项目

修改 `logger_manager.py` 顶部的模块级常量：

```python
LOG_DIR = "logs"            # 日志文件夹
LOG_PREFIX = "MyApp"        # 日志文件名前缀 → MyApp_20260620_143000.log
```

或者在函数调用时直接传入：

```python
from logger.logger_manager import add_file_logger, cleanup_old_logs

add_file_logger(logger, version="v1.0.0", log_dir="my_logs", log_prefix="MyApp")
cleanup_old_logs(logger, max_files=10, log_dir="my_logs", log_prefix="MyApp")
```

## API 参考

### `setup_logger(name="M9AUpdateAssistant") → logging.Logger`

创建并配置控制台日志记录器（含彩色输出）。控制台级别为 INFO，logger 级别为 DEBUG。

### `add_file_logger(logger, version="", log_dir=None, log_prefix=None) → logging.FileHandler`

添加文件日志处理器。文件格式：`{LOG_DIR}/{LOG_PREFIX}_{YYYYMMDD_HHMMSS}.log`

| 参数         | 类型             | 说明                                    |
| ------------ | ---------------- | --------------------------------------- |
| `logger`     | `logging.Logger` | 已有的日志记录器                        |
| `version`    | `str`            | 软件版本号（可选，写入文件日志第一行）  |
| `log_dir`    | `str`            | 日志文件夹（默认取模块级 `LOG_DIR`）    |
| `log_prefix` | `str`            | 文件名前缀（默认取模块级 `LOG_PREFIX`） |

### `cleanup_old_logs(logger, max_files, log_dir=None, log_prefix=None)`

清理超出数量限制的旧日志文件。

| 参数         | 类型             | 说明                                    |
| ------------ | ---------------- | --------------------------------------- |
| `logger`     | `logging.Logger` | 日志记录器                              |
| `max_files`  | `int`            | 最大保留数量                            |
| `log_dir`    | `str`            | 日志文件夹（默认取模块级 `LOG_DIR`）    |
| `log_prefix` | `str`            | 文件名前缀（默认取模块级 `LOG_PREFIX`） |

### `raw_read_save_enabled(config_file, section='Logs', key='save_enabled') → bool`

在完整加载配置前，快速读取配置文件判断是否启用日志保存。

| 参数          | 类型  | 说明                            |
| ------------- | ----- | ------------------------------- |
| `config_file` | `str` | 配置文件路径                    |
| `section`     | `str` | 配置节名（默认 `Logs`）         |
| `key`         | `str` | 配置键名（默认 `save_enabled`） |

### `ColoredFormatter`

自定义 `logging.Formatter` 子类，输出带颜色的日志行。颜色映射：

| 级别     | 颜色           |
| -------- | -------------- |
| DEBUG    | 青色           |
| INFO     | 白色           |
| WARNING  | 黄色           |
| ERROR    | 红色           |
| CRITICAL | 红底+黑字+加粗 |
