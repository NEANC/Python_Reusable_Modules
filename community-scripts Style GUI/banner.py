#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unicodedata

import colorama

VERSION = "v0.0.0"
DEFAULT_BANNER_ART = r"""  ____  _         _        ____ _   _ ___ 
 / ___|| |_ _   _| | ___  / ___| | | |_ _|
 \___ \| __| | | | |/ _ \| |  _| | | || |
  ___) | |_| |_| | |  __/| |_| | |_| || |
 |____/ \__|\__, |_|\___| \____|\___/|___|
             |___/"""
_MIN_DIVIDER_WIDTH = 32
_VARIATION_SELECTORS = ('\ufe0e', '\ufe0f')
_ZERO_WIDTH_JOINER = '\u200d'


def _initialize_colorama():
    """兼容初始化新旧版本的 Colorama。"""
    initializer = getattr(colorama, 'just_fix_windows_console', None)
    if initializer is not None:
        initializer()
        return
    colorama.init()


def _build_info_line(version, license_name):
    parts = []
    if version:
        parts.append(f"Version: {version}")
    if license_name:
        parts.append(f"License: {license_name}")
    return "     ".join(parts)


def _terminal_width(text):
    """计算文本在终端中的基础单元格宽度。"""
    width = 0
    index = 0
    text = text or ''
    while index < len(text):
        ch = text[index]
        if unicodedata.category(ch).startswith('M') or ch in _VARIATION_SELECTORS:
            index += 1
            continue
        if ch == _ZERO_WIDTH_JOINER:
            index += 1
            continue

        sequence_width = 2 if unicodedata.east_asian_width(ch) in ('F', 'W') else 1
        index += 1
        has_emoji_presentation = False
        has_joiner = False

        while index < len(text):
            ch = text[index]
            if ch in _VARIATION_SELECTORS:
                has_emoji_presentation = has_emoji_presentation or ch == '\ufe0f'
                index += 1
                continue
            if unicodedata.category(ch).startswith('M') or '\U0001f3fb' <= ch <= '\U0001f3ff':
                index += 1
                continue
            if ch != _ZERO_WIDTH_JOINER:
                break
            has_joiner = True
            index += 1
            if index < len(text):
                index += 1

        width += 2 if has_emoji_presentation or has_joiner else sequence_width
    return width


def _divider_width(app_name, subtitle, info_line):
    return max(
        _MIN_DIVIDER_WIDTH,
        _terminal_width(app_name),
        _terminal_width(subtitle),
        _terminal_width(info_line),
    )


def print_info(
    app_name="Application",
    subtitle="Terminal Application",
    version=VERSION,
    license_name="",
    banner_art=DEFAULT_BANNER_ART,
):
    """打印可配置启动横幅。"""
    _initialize_colorama()
    info_line = _build_info_line(version, license_name)
    width = _divider_width(app_name, subtitle, info_line)

    print()
    if banner_art:
        print(colorama.Fore.CYAN + banner_art + colorama.Style.RESET_ALL)
        print()
    if app_name:
        print(colorama.Fore.WHITE + app_name + colorama.Style.RESET_ALL)
    if subtitle:
        print(colorama.Fore.WHITE + subtitle + colorama.Style.RESET_ALL)
    if app_name or subtitle or info_line:
        print(colorama.Fore.LIGHTBLACK_EX + " " + "\u2500" * width + colorama.Style.RESET_ALL)
    if info_line:
        print(colorama.Fore.BLUE + f" {info_line}" + colorama.Style.RESET_ALL)
    print()
