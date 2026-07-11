# ConfigManager

基于 M9A_Update_Assistant 与 ALAS_Logs_Archive 项目实践的 Python INI 配置管理模块，提供：

- **默认配置生成 → 加载/修复 → 版本迁移 → 验证**

## 特性

- **零侵入**：仅依赖 Python 标准库，不依赖项目内部模块。
- **自动修复**：配置损坏时自动清理非法行，缺失键/节自动补充默认值。
- **孤键恢复**：键误归属到错误节或 `DEFAULT` 节时自动还原。
- **版本迁移**：支持 `rename_key` / `rename_value` 迁移规则，自动升级旧配置文件。
- **原子写入**：写文件先写 `.tmp`，再通过 `os.replace()` 替换，避免写入中断导致配置损坏。
- **多 pass 解析**：解析失败时尝试修复；若修复后仍失败，会重新生成默认配置并触发首次运行流程。

## 依赖

无（仅 Python 标准库）。

## 文件结构

```text
config_ini/
├── __init__.py           # 包入口
├── config_manager.py     # ConfigManager 核心类 + resolve_temp_folder()
├── config_migration.py   # 迁移引擎（apply_migrations）
└── README.md
```

## 快速开始

```python
import logging
import sys

from config_ini import ConfigManager
from config_ini.config_migration import MIGRATIONS

logger = logging.getLogger("MyApp")

# 定义你的配置结构。传入 default_sections 时，会与内置默认配置合并；同名键会覆盖内置默认值。
DEFAULT_SECTIONS = {
    "Paths": {
        "install_folder": r"C:\MyApp",
        "temp_folder": "Temp",           # "Temp" → 程序目录/Temp
    },
    "Network": {
        "proxy": "",
        "mirrors": "https://example.com, https://backup.example.com",
    },
    "Update": {
        "enabled": "true",
        "channel": "stable",
    },
}

COMMENTS = {
    "Paths.install_folder": "安装目录",
    "Paths.temp_folder": "临时文件夹（Temp 表示程序目录下的 Temp，留空使用系统 TEMP）",
    "Network.proxy": "HTTP/HTTPS 代理地址（例如 http://127.0.0.1:7890）",
    "Network.mirrors": "镜像源列表，多个地址使用英文逗号分隔",
    "Update.enabled": "是否启用自动更新",
    "Update.channel": "更新通道：preview（含预发布）/ stable（仅正式版）",
}

# 如果配置文件格式有过变化，通过修改模块级 MIGRATIONS 注册迁移。
# 真实项目建议在启动早期集中注册迁移规则，避免运行期反复修改模块级全局状态。
MIGRATIONS.clear()
MIGRATIONS.extend([
    {
        "id": 1,
        "type": "rename_key",
        "section": "Update",
        "old_key": "update_channel",
        "new_key": "channel",
        "description": "Update.update_channel → Update.channel",
    },
    {
        "id": 2,
        "type": "rename_value",
        "section": "Update",
        "key": "channel",
        "old_value": "release",
        "new_value": "preview",
        "description": "channel: release → preview",
    },
])

# 初始化。
config = ConfigManager(
    config_file="config.ini",
    logger=logger,
    default_sections=DEFAULT_SECTIONS,
    comments=COMMENTS,
    app_name="MyApp",
)

# 注册类型标注（可选）。
config.register_path_key("install_folder")
config.register_bool_key("enabled")
config.register_list_key("mirrors")  # 读取时按英文逗号分隔为 list；仅注册，不要求默认配置中一定存在。

# 加载。
config.load()

# 验证。默认实现仅在 temp_folder 非空时尝试创建目录。
if not config.validate():
    sys.exit(1)

# 读取。
print(config.get_attr("channel"))              # "stable"
print(config.get_attr_bool("enabled"))         # True
print(config.get_attr_list("mirrors"))         # ["https://example.com", "https://backup.example.com"]
```

## API 参考

### `ConfigManager.__init__(...)`

签名：

```python
ConfigManager(
    config_file: str,
    logger: logging.Logger,
    default_sections: Optional[Dict[str, Dict[str, str]]] = None,
    comments: Optional[Dict[str, str]] = None,
    app_name: str = "",
    first_run_callback: Optional[Callable[[], None]] = None,
)
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `config_file` | `str` | 是 | 无 | 配置文件路径。 |
| `logger` | `logging.Logger` | 是 | 无 | 日志记录器。 |
| `default_sections` | `dict` / `None` | 否 | `None` | 默认节/键值对，形如 `{"Section": {"key": "default"}}`。传入后会与内置默认配置合并；同名节中的同名键会覆盖内置默认值。 |
| `comments` | `dict` / `None` | 否 | `None` | 键注释，形如 `{"Section.key": "注释"}`。传入后会覆盖同名内置注释。 |
| `app_name` | `str` | 否 | `""` | 应用名，用于空 `temp_folder` 回退到系统临时目录时拼接子目录。 |
| `first_run_callback` | `Callable[[], None]` / `None` | 否 | `None` | 首次运行生成默认配置文件后的回调。不传时，默认调用 `sys.exit(0)`；生成失败时调用 `sys.exit(1)`。 |

> 注意：`default_sections` 和 `comments` 不是完全替换内置默认配置，而是在内置默认值基础上合并。

### 类型注册方法

| 方法 | 效果 |
| --- | --- |
| `register_path_key(key)` | 将键标记为路径类型。`load()` 填充属性后，会对该键的值执行 `strip()`，再复用 `resolve_temp_folder()` 规则解析；它不是通用的“转绝对路径”或普通路径规范化。不存在于默认配置中的键会被跳过。 |
| `register_list_key(key)` | 将键标记为列表类型。当前实现不会在 `load()` 时转换，也不会要求该键存在于默认配置中；若键未进入 `_attrs`，读取时返回空列表；已进入 `_attrs` 时由 `get_attr_list()` 按英文逗号分隔并去除空项。 |
| `register_int_key(key)` | 将键标记为 `int` 类型。`load()` 时会尝试规范化为整数字符串；转换失败则保留原值。读取时由 `get_attr_int()` 返回 `int`，无法转换时返回默认值。 |
| `register_bool_key(key)` | 将键标记为 `bool` 类型。`load()` 时会将 `true`、`1`、`yes`、`on` 规范化为字符串 `"True"`，其他值规范化为字符串 `"False"`。读取时由 `get_attr_bool()` 返回 `bool`。 |

`temp_folder` 是特殊键：只要默认配置中存在 `temp_folder`，`load()` 就会按 `[Paths] temp_folder` 的值调用 `resolve_temp_folder()`，并尝试确保目录存在；无需显式调用 `register_path_key("temp_folder")`。

通过 `register_path_key()` 注册的普通路径键也会复用 `resolve_temp_folder()` 规则：空字符串会按临时目录规则解析，字面量 `Temp` 会解析为程序目录下的 `Temp`。如果需要的是通用路径规范化或普通相对路径转绝对路径，请不要依赖该注册方法。

### 读取方法

| 方法 | 返回类型 | 说明 |
| --- | --- | --- |
| `get_attr(key, default="")` | `str` | 从已加载的属性字典读取原始字符串；不存在时返回 `default`。 |
| `get_attr_int(key, default=0)` | `int` | 将属性值转换为 `int`；转换失败时返回 `default`。 |
| `get_attr_bool(key, default=False)` | `bool` | 仅 `true`、`1`、`yes`、`on`（忽略大小写）视为 `True`。 |
| `get_attr_list(key)` | `List[str]` | 按英文逗号分隔，去除每项首尾空白，并过滤空项。 |

### 生命周期方法

| 方法 | 说明 |
| --- | --- |
| `load()` | 加载、修复、迁移配置文件，并填充内部属性字典。 |
| `validate()` | 校验配置合法性。默认实现仅在 `_attrs["temp_folder"]` 非空时尝试创建目录；创建失败返回 `False`；如果 `temp_folder` 为空，则不会创建目录并视为通过。子类可覆盖添加自定义逻辑。 |

## `load()` 行为

`load()` 的实际流程如下：

1. 如果配置文件不存在，则生成默认配置文件；成功后执行 `first_run_callback`，未提供回调时调用 `sys.exit(0)`。
2. 使用 `ConfigParser(strict=False)` 读取 INI 文件；解析失败时最多处理 3 轮：
   - 第 1 轮失败：清理坏行。无 `=` 的非注释行会被注释为 `# [已修复] ...`，空键名行会被删除。
   - 第 2 轮失败：重新生成默认配置文件，并进入首次运行流程；成功后执行 `first_run_callback`，未提供回调时调用 `sys.exit(0)`，生成失败时调用 `sys.exit(1)`。
   - 第 3 轮只是理论兜底；正常控制流中，第 2 轮解析失败后不会继续进入第 3 轮。
3. 调用 `apply_migrations()` 应用迁移规则。
4. 补齐 `default_sections` 中缺失的节。
5. 恢复孤键：如果模板键误放在 `DEFAULT` 节或其他节，会在目标节缺失该键时移动回正确节。
6. 补齐缺失键。若迁移、孤键恢复或补齐导致配置变更，会重建配置文件；重建时会保留已有值、补充注释，并保留 `[__migrations__]` 节。
7. 按 `default_sections` 填充内部属性字典 `_attrs`。
8. 特殊处理 `temp_folder`：读取 `[Paths] temp_folder`，调用 `resolve_temp_folder()`，并确保目录存在；创建失败时回退到系统临时目录。
9. 对通过 `register_path_key()` 注册的其他路径键调用 `resolve_temp_folder()`。
10. 对通过 `register_int_key()`、`register_bool_key()` 注册的键做字符串规范化。

## 迁移规则格式

当前 `config_migration.py` 支持 2 种迁移类型：`rename_key` 和 `rename_value`。

```python
MIGRATIONS = [
    {
        "id": 1,                          # 唯一 ID，建议递增。
        "type": "rename_key",            # 重命名键。
        "section": "Update",
        "old_key": "release_version",
        "new_key": "channel",
        "description": "日志描述（可选）",
    },
    {
        "id": 2,
        "type": "rename_value",          # 值迁移。
        "section": "Update",
        "key": "channel",
        "old_value": "release",
        "new_value": "preview",
        "description": "日志描述（可选）",
    },
]
```

规则说明：

- `rename_key`：当目标节存在且 `old_key` 存在时，将其值复制到 `new_key`（如果 `new_key` 不存在），随后删除 `old_key`。
- `rename_value`：当目标节和键存在，且当前值 `strip()` 后等于 `old_value` 时，改写为 `new_value`。
- 成功执行的迁移会在 INI 中记录到 `[__migrations__]` 节，格式为 `<id> = done`，避免重复执行。
- `[__migrations__]` 中值不是 `done` 的记录会被忽略；键名无法转换为整数的记录会被视为非法迁移标记，记录 warning 后跳过。
- 未命中条件的迁移不会被标记为已执行；未知类型或参数错误会记录 warning，并继续处理后续迁移。

## `resolve_temp_folder(...)`

解析临时文件夹路径的便捷函数。函数只特殊处理空值和字面量 `Temp`；绝对路径与普通相对路径都会按配置原样返回。

签名：

```python
resolve_temp_folder(
    temp_folder_config: str,
    app_name: str = "",
    program_dir: str = "",
    logger: Optional[logging.Logger] = None,
) -> str
```

解析规则：

| 配置值 | 返回结果 |
| --- | --- |
| 空字符串 | 优先返回 `%TEMP%\<app_name>`；未提供 `app_name` 时返回 `%TEMP%\Temp`。如果 `TEMP` 不存在，则尝试 `LOCALAPPDATA\Temp\<app_name>` 或 `LOCALAPPDATA\Temp`；仍不可用时返回 `<program_dir>\Temp`。 |
| `Temp` | 返回 `<program_dir>\Temp`。`program_dir` 为空时自动取 `sys.argv[0]` 所在目录。 |
| 绝对路径，例如 `C:\Cache` | 原样返回。 |
| 普通相对路径，例如 `cache` | 原样返回。 |

```python
from config_ini import resolve_temp_folder

# ""          → 系统 TEMP 下的应用子目录，或其他回退目录。
# "Temp"      → 程序目录/Temp
# r"C:\Cache" → C:\Cache
# "cache"     → cache
path = resolve_temp_folder("Temp", app_name="MyApp")
```

## 配置文件示例

```ini
[Paths]
# 安装目录
install_folder = C:\MyApp
# 临时文件夹（Temp 表示程序目录下的 Temp，留空使用系统 TEMP）
temp_folder = Temp

[Network]
# HTTP/HTTPS 代理地址（例如 http://127.0.0.1:7890）
proxy =
# 镜像源列表，多个地址使用英文逗号分隔
mirrors = https://example.com, https://backup.example.com

[Update]
# 是否启用自动更新
enabled = true
# 更新通道：preview（含预发布）/ stable（仅正式版）
channel = stable

[__migrations__]
1 = done
2 = done
```
