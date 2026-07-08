#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""
日志管理器：控制台彩色输出 + 文件日志 + 旧日志清理。

通过模块级常量 LOG_DIR / LOG_PREFIX 适配不同项目，
不依赖任何项目内部模块。
"""

import configparser
import logging

import colorama

from datetime import datetime
from pathlib import Path
from time import time
from typing import Optional

# ── 模块级配置（移植时修改这些常量即可） ──
LOG_DIR = "logs"                     # 日志文件夹路径
LOG_PREFIX = "M9A_Update"            # 日志文件名前缀
CONSOLE_FORMAT = '%(levelname)s | %(asctime)s.%(msecs)03d | %(message)s'
FILE_FORMAT = '%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s'
_HANDLER_OWNER = "logger_manager"


def _is_owned_handler(handler: logging.Handler, handler_type: str) -> bool:
    """判断 handler 是否由本模块创建。"""
    return (
        getattr(handler, "_logger_manager_owner", None) == _HANDLER_OWNER
        and getattr(handler, "_logger_manager_type", None) == handler_type
    )


def _find_file_handler(logger: logging.Logger, log_dir: Path,
                       prefix: str) -> Optional[logging.FileHandler]:
    """查找指定目录和前缀对应的文件日志 handler。"""
    resolved_dir = log_dir.resolve()
    for handler in logger.handlers:
        if not isinstance(handler, logging.FileHandler):
            continue
        if not _is_owned_handler(handler, "file"):
            continue
        handler_dir = Path(handler.baseFilename).parent.resolve()
        handler_prefix = getattr(handler, "_logger_manager_prefix", None)
        if handler_dir == resolved_dir and handler_prefix == prefix:
            return handler
    return None


class ColoredFormatter(logging.Formatter):
    """带颜色的日志格式化器，仅作用于控制台输出"""

    LEVEL_COLORS = {
        'DEBUG': colorama.Fore.CYAN,
        'INFO': colorama.Fore.WHITE,
        'WARNING': colorama.Fore.YELLOW,
        'ERROR': colorama.Fore.RED,
        'CRITICAL': colorama.Back.RED + colorama.Fore.BLACK + colorama.Style.BRIGHT,
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        colorama.init(autoreset=True)

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelname, colorama.Fore.WHITE)
        result = super().format(record)
        return f"{color}{result}{colorama.Style.RESET_ALL}"


def setup_logger(name: str = "M9AUpdateAssistant") -> logging.Logger:
    """
    创建并配置控制台日志记录器

    Args:
        name: 日志记录器名称

    Returns:
        配置好的日志记录器
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for handler in logger.handlers:
        if _is_owned_handler(handler, "console"):
            return logger

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler._logger_manager_owner = _HANDLER_OWNER
    console_handler._logger_manager_type = "console"
    console_formatter = ColoredFormatter(
        CONSOLE_FORMAT,
        datefmt='%H:%M:%S',
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    return logger


def raw_read_save_enabled(config_file: str,
                           section: str = 'Logs',
                           key: str = 'save_enabled') -> bool:
    """在加载完整配置前，粗读配置文件判断是否启用日志保存

    Args:
        config_file: 配置文件路径
        section: 配置节名
        key: 配置键名

    Returns:
        是否启用日志保存
    """
    if not Path(config_file).exists():
        return False
    try:
        raw = configparser.ConfigParser()
        raw.read(config_file, encoding='utf-8')
        return raw.getboolean(section, key, fallback=False)
    except Exception:
        return False


def add_file_logger(logger: logging.Logger, version: str = "",
                     log_dir: Optional[str] = None,
                     log_prefix: Optional[str] = None) -> logging.FileHandler:
    """
    添加文件日志记录器

    Args:
        logger: 已有的日志记录器
        version: 当前软件版本号
        log_dir: 日志文件夹路径（默认使用模块级 LOG_DIR）
        log_prefix: 日志文件名前缀（默认使用模块级 LOG_PREFIX）

    Returns:
        文件日志处理器
    """
    log_dir = Path(log_dir or LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    prefix = log_prefix or LOG_PREFIX
    existing_handler = _find_file_handler(logger, log_dir, prefix)
    if existing_handler:
        return existing_handler

    log_file = log_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler._logger_manager_owner = _HANDLER_OWNER
    file_handler._logger_manager_type = "file"
    file_handler._logger_manager_prefix = prefix
    formatter = logging.Formatter(
        FILE_FORMAT,
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if version:
        logger.debug(f"当前软件版本: {version}")
    return file_handler


def cleanup_old_logs(logger: logging.Logger, max_files: int = 15,
                      max_days: int = 7,
                      log_dir: Optional[str] = None,
                      log_prefix: Optional[str] = None) -> None:
    """
    清理多余的日志文件

    Args:
        logger: 日志记录器
        max_files: 最大日志文件数量，0 表示不按数量保留任何匹配日志
        max_days: 最大日志保留天数，0 表示不按日期保留任何匹配日志
        log_dir: 日志文件夹路径（默认使用模块级 LOG_DIR）
        log_prefix: 日志文件名前缀（默认使用模块级 LOG_PREFIX）
    """
    if max_files < 0:
        raise ValueError("max_files 不能小于 0")
    if max_days < 0:
        raise ValueError("max_days 不能小于 0")

    log_dir = Path(log_dir or LOG_DIR)
    if not log_dir.exists():
        return

    prefix = log_prefix or LOG_PREFIX
    log_files = list(log_dir.glob(f"{prefix}_*.log"))
    deleted_paths = set()
    cutoff_time = time() - max_days * 24 * 60 * 60

    for log_file in log_files:
        if log_file.stat().st_mtime >= cutoff_time:
            continue
        try:
            log_file.unlink()
            deleted_paths.add(log_file)
            logger.debug(f"已删除过期日志文件: {log_file}")
        except Exception as e:
            logger.error(f"删除日志文件 {log_file} 失败: {e}")

    remaining_files = [path for path in log_files if path not in deleted_paths and path.exists()]
    remaining_files.sort(key=lambda x: x.stat().st_mtime)
    files_to_delete = remaining_files if max_files == 0 else remaining_files[:-max_files]

    for log_file in files_to_delete:
        try:
            log_file.unlink()
            deleted_paths.add(log_file)
            logger.debug(f"已删除日志文件: {log_file}")
        except Exception as e:
            logger.error(f"删除日志文件 {log_file} 失败: {e}")

    if deleted_paths:
        logger.info(f"已清理 {len(deleted_paths)} 个日志文件")
