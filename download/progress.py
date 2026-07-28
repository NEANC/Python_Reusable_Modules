#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""下载模块专用进度条和消息格式化工具。"""

from colorama import Fore
from colorama import Style
from tqdm import tqdm

BAR_FG = Style.BRIGHT + Fore.WHITE
BAR_AUX = Fore.LIGHTBLACK_EX
BAR_OK = Fore.LIGHTGREEN_EX
BAR_ERR = Style.BRIGHT + Fore.LIGHTRED_EX
BAR_RST = Style.RESET_ALL

BAR_FORMAT = (
    "{desc}: "
    + BAR_FG + "{bar}" + BAR_RST + " "
    + BAR_AUX
    + "{n_fmt}/{total_fmt} | ETA: {remaining} | {rate_fmt}"
    + BAR_RST
)
DOWNLOAD_BAR_FORMAT = BAR_FORMAT.replace("{rate_fmt}", "{download_rate_fmt}")


class DownloadProgressBar(tqdm):
    """下载专用 tqdm 进度条，使用外部传入的网络速度字段。"""

    def __init__(self, *args, **kwargs):
        """初始化下载进度条。"""
        self.download_rate_fmt = ""
        super().__init__(*args, **kwargs)

    @property
    def format_dict(self):
        """向 tqdm 格式模板注入下载速度字段。"""
        data = super().format_dict
        data["download_rate_fmt"] = self.download_rate_fmt
        return data


def create_download_progress_bar(total: int, desc: str, disable: bool = False,
                                 leave: bool = False) -> tqdm:
    """创建下载专用 tqdm 进度条。"""
    return DownloadProgressBar(
        total=total,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=desc,
        bar_format=DOWNLOAD_BAR_FORMAT,
        disable=disable,
        leave=leave,
    )


def format_ok(action: str, source: str, dest: str, total_bytes: int) -> str:
    """格式化下载成功消息。"""
    size_str = tqdm.format_sizeof(total_bytes, "B", 1024)
    return f"{BAR_OK}{action}完成 {source} -> {dest} | 大小: {size_str}{BAR_RST}"


def format_error(desc: str, reason: str) -> str:
    """格式化下载失败消息。"""
    return f"{BAR_ERR}{desc}: 失败 {reason}{BAR_RST}"
