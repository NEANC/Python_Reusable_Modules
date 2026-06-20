# ConfigManager

基于 M9A_Update_Assistant 项目实践的 Python INI 配置管理模块；

提供 **默认配置生成 → 加载/修复 → 版本迁移 → 验证** 的完整逻辑。

## 特性

- **零侵入**：仅依赖标准库 `configparser`，不依赖项目内部模块
- **自动修复**：配置损坏时自动清理非法行，缺失键/节自动补充默认值
- **孤键恢复**：键误归属到错误节时自动还原
- **版本迁移**：支持 `rename_key` / `rename_value` 迁移规则，自动升级旧配置文件
- **原子写入**：写文件先写 `.tmp` 再 `os.replace()`，避免写一半崩溃损坏配置
- **多 pass 解析**：解析失败时尝试修复 → 重新生成，最多 3 次重试

## 依赖

无（仅 Python 标准库）。

## 文件结构

```
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

logger = logging.getLogger("MyApp")

# 定义你的配置结构
DEFAULT_SECTIONS = {
    'Paths': {
        'install_folder': r'C:\MyApp',
        'temp_folder': 'Temp',           # 'Temp' → 程序目录/Temp
    },
    'Network': {
        'proxy': '',
    },
    'Update': {
        'enabled': 'true',
        'channel': 'stable',
    },
}

COMMENTS = {
    'Paths.install_folder': '安装目录',
    'Paths.temp_folder': '临时文件夹（Temp 表示程序目录下的 Temp，留空使用系统 TEMP）',
    'Network.proxy': 'HTTP/HTTPS 代理地址（例如 http://127.0.0.1:7890）',
    'Update.enabled': '是否启用自动更新',
    'Update.channel': '更新通道：preview（含预发布）/ stable（仅正式版）',
}

# 如果配置文件格式有过变化，通过修改模块级 MIGRATIONS 注册迁移
from config_ini.config_migration import MIGRATIONS
MIGRATIONS.clear()
MIGRATIONS.extend([
    {
        'id': 1,
        'type': 'rename_key',
        'section': 'Update',
        'old_key': 'update_channel',
        'new_key': 'channel',
        'description': 'Update.update_channel → Update.channel',
    },
    {
        'id': 2,
        'type': 'rename_value',
        'section': 'Update',
        'key': 'channel',
        'old_value': 'release',
        'new_value': 'preview',
        'description': 'channel: release → preview',
    },
])

# 初始化
config = ConfigManager(
    config_file="config.ini",
    logger=logger,
    default_sections=DEFAULT_SECTIONS,
    comments=COMMENTS,
    app_name="MyApp",
)

# 注册类型标注（可选，让 load() 自动转换/处理）
config.register_path_key("temp_folder")   # 会调用 resolve_temp_folder
config.register_bool_key("enabled")
config.register_list_key("install_folder") # 逗号分隔 → list

# 加载
config.load()

# 验证
if not config.validate():
    sys.exit(1)

# 读取
print(config.get_attr("channel"))         # "stable"
print(config.get_attr_bool("enabled"))    # True
print(config.get_attr_list("install_folder"))  # ["C:\MyApp"]
```

## API 参考

### `ConfigManager.__init__(...)`

| 参数                 | 类型             | 必填 | 说明                                                               |
| -------------------- | ---------------- | ---- | ------------------------------------------------------------------ |
| `config_file`        | `str`            | 是   | 配置文件路径                                                       |
| `logger`             | `logging.Logger` | 是   | 日志记录器                                                         |
| `default_sections`   | `dict`           | 否   | 默认节/键值对：`{"Section": {"key": "default"}}`                   |
| `comments`           | `dict`           | 否   | 键注释：`{"Section.key": "注释"}`                                  |
| `app_name`           | `str`            | 否   | 应用名（用于系统临时目录回退时子文件夹名）                         |
| `first_run_callback` | `() -> None`     | 否   | 首次运行时生成默认配置后的回调（不传则 `input()` + `sys.exit(0)`） |

### 类型注册方法

| 方法                     | 效果                                                   |
| ------------------------ | ------------------------------------------------------ |
| `register_path_key(key)` | `load()` 时对 `temp_folder` 调用 `resolve_temp_folder` |
| `register_list_key(key)` | `get_attr_list()` 返回逗号分隔的列表                   |
| `register_int_key(key)`  | `get_attr_int()` 返回 int                              |
| `register_bool_key(key)` | `get_attr_bool()` 返回 bool                            |

### 读取方法

| 方法                                | 返回类型    |
| ----------------------------------- | ----------- |
| `get_attr(key, default='')`         | `str`       |
| `get_attr_int(key, default=0)`      | `int`       |
| `get_attr_bool(key, default=False)` | `bool`      |
| `get_attr_list(key)`                | `List[str]` |

### 生命周期方法

| 方法         | 说明                                       |
| ------------ | ------------------------------------------ |
| `load()`     | 加载/修复/迁移配置文件，填充内部属性字典   |
| `validate()` | 校验配置合法性（子类可覆盖添加自定义逻辑） |

## 迁移规则格式

```python
MIGRATIONS = [
    {
        'id': 1,                          # 唯一 ID（递增）
        'type': 'rename_key',             # 迁移类型
        'section': 'Update',
        'old_key': 'release_version',
        'new_key': 'channel',
        'description': '日志描述（可选）',
    },
    {
        'id': 2,
        'type': 'rename_value',           # 值迁移
        'section': 'Update',
        'key': 'channel',
        'old_value': 'release',
        'new_value': 'preview',
    },
]
```

`apply_migrations()` 在 INI 中记录已应用的迁移 ID（`[__migrations__]` 节），避免重复执行。

## `resolve_temp_folder(…)`

解析临时文件夹路径的便捷函数：

```python
from config_ini import resolve_temp_folder

# ''      → 系统 TEMP/AppName
# 'Temp'  → 程序目录/Temp
# '/abs'  → /abs
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

[Update]
# 是否启用自动更新
enabled = true
# 更新通道：preview（含预发布）/ stable（仅正式版）
channel = stable

[__migrations__]
1 = done
2 = done
```
