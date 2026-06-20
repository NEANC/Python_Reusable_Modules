#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""
tqdm 进度条统一配置、colorama 颜色常量与辅助函数。

其他模块通过 import 本模块复用进度条样式，避免重复定义。
"""

from colorama import Fore, Style
from tqdm import tqdm  # 统一入口，方便其他模块通过本模块获取 tqdm


# ── 进度条颜色常量 ──
BAR_FG = Style.BRIGHT + Fore.WHITE              # 进度条主体：白色加粗
BAR_AUX = Fore.LIGHTBLACK_EX                    # 辅助信息：浅灰色
BAR_OK = Fore.LIGHTGREEN_EX                     # 完成状态：亮绿色
BAR_WARN = Style.BRIGHT + Fore.LIGHTYELLOW_EX   # 警告状态：亮黄色加粗
BAR_ERR = Style.BRIGHT + Fore.LIGHTRED_EX       # 错误/失败：亮红色加粗
BAR_RST = Style.RESET_ALL                       # 颜色重置

# ── 进度条格式 ──
BAR_FORMAT = (
    '{desc}: '                                  # 任务名称（默认终端色）
    + BAR_FG + '{bar}' + BAR_RST + ' '          # 进度条（白色加粗）
    + BAR_AUX                                   # 辅助信息开始（浅灰色）
    + '{n_fmt}/{total_fmt} | ETA: {remaining} | {rate_fmt}'
    + BAR_RST                                   # 辅助信息结束
)


def create_progress_bar(total: int, desc: str, disable: bool = False,
                        leave: bool = False) -> tqdm:
    """
    创建统一风格的 tqdm 进度条

    Args:
        total: 总大小（字节）
        desc: 任务描述
        disable: 是否禁用
        leave: 完成后是否保留进度条

    Returns:
        tqdm 实例
    """
    return tqdm(
        total=total, unit='B', unit_scale=True, unit_divisor=1024,
        desc=desc, bar_format=BAR_FORMAT, disable=disable, leave=leave,
    )


def format_ok(action: str, source: str, dest: str, total_bytes: int) -> str:
    """格式化完成消息（亮绿色）

    Args:
        action: 操作名称
        source: 源文件名
        dest: 目标路径
        total_bytes: 总大小（字节）
    """
    size_str = tqdm.format_sizeof(total_bytes, 'B', 1024)
    return f"{BAR_OK}{action}完成 {source} -> {dest} | 大小: {size_str}{BAR_RST}"


def format_error(desc: str, reason: str) -> str:
    """格式化错误消息（亮红色加粗）"""
    return f"{BAR_ERR}{desc}: 失败 {reason}{BAR_RST}"


def format_warn(msg: str) -> str:
    """格式化警告消息（亮黄色加粗）"""
    return f"{BAR_WARN}{msg}{BAR_RST}"
