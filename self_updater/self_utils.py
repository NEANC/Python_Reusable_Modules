#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""自更新模块工具函数：版本比较、SHA256 计算、打包检测等。"""

import hashlib
import re
import sys

from pathlib import Path
from typing import Optional, Tuple


def calculate_sha256(file_path: Path) -> str:
    """计算文件的 SHA256 哈希值"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def detect_package_type() -> Tuple[bool, str]:
    """
    检测当前运行环境是否为打包后的可执行文件

    Returns:
        (是否为打包后程序, 打包方式名称)
    """
    is_py_script = Path(sys.argv[0]).suffix.lower() == '.py'
    is_pyinstaller = getattr(sys, 'frozen', False) or hasattr(sys, '_MEIPASS')
    is_nuitka = hasattr(sys, '__compiled__')

    is_bundled = (not is_py_script) or is_pyinstaller or is_nuitka

    if is_pyinstaller:
        package_type = "PyInstaller"
    elif is_nuitka:
        package_type = "Nuitka"
    else:
        package_type = "Nuitka"

    return is_bundled, package_type


def version_to_tuple(v: str) -> Tuple[int, ...]:
    """将版本号字符串转换为元组用于比较，兼容 vX.Y.Z 和 vX.Y.Z-prerelease.N"""
    try:
        v = v.lstrip('v').split('-')[0]
        return tuple(map(int, v.split('.')))
    except Exception:
        return ()


def is_prerelease(v: str) -> bool:
    """检查版本号是否为预发布版本"""
    return bool(re.search(r'-(alpha|beta|rc)', v, re.IGNORECASE))


def is_build_tag(v: str) -> bool:
    """检查版本号是否为构建版本（如 v0.0.1-build.gXXXXXX）"""
    return bool(re.search(r'-build\b', v))


def prerelease_weight(v: str) -> Tuple[int, int]:
    """返回预发布权重：alpha=(1, N), beta=(2, N), rc=(3, N)，缺数字时 N=0"""
    WEIGHT_MAP = {'alpha': 1, 'beta': 2, 'rc': 3}
    match = re.search(r'-(alpha|beta|rc)(?:[-.]?(\d+))?', v, re.IGNORECASE)
    if not match:
        return (0, 0)
    kind = match.group(1).lower()
    num = int(match.group(2)) if match.group(2) else 0
    return (WEIGHT_MAP.get(kind, 0), num)


def version_newer_than(current: str, latest: str) -> bool:
    """
    比较版本号，latest 是否比 current 新

    预发布 → 正式版始终视为升级
    alpha < beta < rc < stable
    """
    cur_tuple = version_to_tuple(current)
    lat_tuple = version_to_tuple(latest)
    if not cur_tuple or not lat_tuple:
        return False

    if cur_tuple < lat_tuple:
        return True
    if cur_tuple > lat_tuple:
        return False

    cur_pre = is_prerelease(current)
    lat_pre = is_prerelease(latest)

    if not cur_pre and lat_pre:
        return False
    if cur_pre and not lat_pre:
        return True
    if cur_pre and lat_pre:
        return prerelease_weight(latest) > prerelease_weight(current)
    return False


def get_exe_path() -> Path:
    """
    获取当前可执行文件的真实路径

    打包模式下 sys.argv[0] 指向用户双击的真实 exe；
    源码模式下指向 .py 脚本本身。
    """
    return Path(sys.argv[0]).resolve()
