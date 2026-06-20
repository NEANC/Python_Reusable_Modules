#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""
INI 配置管理模块

提供应用程序的配置管理能力：
  - 自动生成带注释的默认配置文件
  - INI 加载、损坏修复、孤键恢复
  - 版本迁移（键重命名、值重命名）
  - 可移植到其他项目（通过 DEFAULT_SECTIONS / COMMENTS 注入配置结构）

用法示例::

    from config_ini import ConfigManager, resolve_temp_folder

    DEFAULT_SECTIONS = {
        'Paths': {'install_folder': 'C:\\MyApp', 'temp_folder': 'Temp'},
        'Network': {'proxy': ''},
    }
    COMMENTS = {
        'Paths.install_folder': '安装目录',
        'Paths.temp_folder': '临时目录（Temp 表示程序目录下的 Temp）',
        'Network.proxy': 'HTTP 代理地址',
    }

    config = ConfigManager(
        config_file="config.ini",
        logger=logger,
        default_sections=DEFAULT_SECTIONS,
        comments=COMMENTS,
        app_name="MyApp",
    )
    config.load()
    if not config.validate():
        sys.exit(1)

    install_folder = config.get_attr("install_folder")
"""

from .config_manager import ConfigManager, resolve_temp_folder
from .config_migration import apply_migrations

__all__ = [
    "ConfigManager",
    "resolve_temp_folder",
    "apply_migrations",
]
