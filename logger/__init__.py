#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""
日志管理模块

提供应用程序的日志管理能力：
  - 控制台彩色日志输出（colorama）
  - 文件日志写入
  - 旧日志自动清理
  - 可移植到其他项目（通过模块级常量 LOG_DIR / LOG_PREFIX 适配）

用法示例::

    from logger import setup_logger, add_file_logger, cleanup_old_logs

    logger = setup_logger("MyApp")
    add_file_logger(logger, version="v1.0.0")
    cleanup_old_logs(logger, max_files=5, max_days=7)
"""

from .logger_manager import (
    ColoredFormatter,
    add_file_logger,
    cleanup_old_logs,
    raw_read_save_enabled,
    setup_logger,
)

__all__ = [
    "ColoredFormatter",
    "add_file_logger",
    "cleanup_old_logs",
    "raw_read_save_enabled",
    "setup_logger",
]
