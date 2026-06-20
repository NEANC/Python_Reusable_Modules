#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""
自更新模块

提供应用程序的自我更新能力：
  - 从 GitHub Release 检测新版本
  - 下载、校验、替换可执行文件
  - PowerShell 脚本驱动的热替换与回滚
  - 可移植到其他项目（通过构造函数注入参数）

用法示例::

    from self_updater import SelfUpdater

    updater = SelfUpdater(
        github_repo="you/your-repo",
        asset_pattern="^YourApp-(Nuitka|PyInstaller)-v...exe$",
        app_name="YourApp",
        current_version="v1.0.0",
        proxy="",
        temp_folder="/tmp/your-app",
        logger=your_logger,
        download_func=your_download_with_progress,
    )
    need_exit = updater.check_self_update()
"""

from .self_config import UpdateState
from .self_updater import SelfUpdater
from .self_utils import (
    calculate_sha256,
    detect_package_type,
    get_exe_path,
    is_build_tag,
    is_prerelease,
    version_newer_than,
    version_to_tuple,
)

__all__ = [
    "SelfUpdater",
    "UpdateState",
    "calculate_sha256",
    "detect_package_type",
    "get_exe_path",
    "is_build_tag",
    "is_prerelease",
    "version_newer_than",
    "version_to_tuple",
]
