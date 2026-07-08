#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""
配置管理器：配置的初始化、加载、验证、迁移。

通过 DEFAULT_SECTIONS / COMMENTS 字典注入项目专属配置结构，
不依赖任何项目内部模块。
"""

import logging
import os
import re
import sys
import configparser

from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config_migration import apply_migrations


def _get_program_dir() -> str:
    """获取程序真实所在目录（兼容所有打包方式及源码运行）"""
    return str(Path(sys.argv[0]).resolve().parent)


def resolve_temp_folder(temp_folder_config: str, app_name: str = '',
                         program_dir: str = '',
                         logger: Optional[logging.Logger] = None) -> str:
    """根据配置值解析临时文件夹路径

    Args:
        temp_folder_config: 配置中的 temp_folder 值（已 strip）
        app_name: 应用名称（用于系统临时目录下的子文件夹名）
        program_dir: 程序根目录，空则自动检测
        logger: 可选的日志记录器

    Returns:
        解析后的临时文件夹绝对路径
    """
    if not program_dir:
        program_dir = _get_program_dir()

    if not temp_folder_config:
        system_temp = os.environ.get('TEMP', '')
        if system_temp:
            result = os.path.join(system_temp, app_name) if app_name else os.path.join(system_temp, 'Temp')
        else:
            local_app_data = os.environ.get('LOCALAPPDATA', '')
            if local_app_data:
                result = os.path.join(local_app_data, 'Temp', app_name) if app_name else os.path.join(local_app_data, 'Temp')
            else:
                result = os.path.join(program_dir, 'Temp')
        if logger:
            logger.info(f"配置为空，使用系统临时文件夹: {result}")
        return result
    if temp_folder_config == 'Temp':
        return os.path.join(program_dir, 'Temp')
    return temp_folder_config


# ── 内置默认配置（用户可通过 __init__ 参数覆盖） ──
_DEFAULT_SECTIONS: Dict[str, Dict[str, str]] = {
    'Paths': {
        'install_folder': r'C:\MyApp',
        'temp_folder': 'Temp',
    },
    'Network': {
        'proxy': '',
    },
    'Update': {
        'enabled': 'true',
        'channel': 'stable',
    },
}

_DEFAULT_COMMENTS: Dict[str, str] = {
    'Paths.install_folder': '安装目录',
    'Paths.temp_folder': '临时文件夹（Temp 表示程序目录下的 Temp，留空使用系统 TEMP）',
    'Network.proxy': 'HTTP/HTTPS 代理地址（例如 http://127.0.0.1:7890）',
    'Update.enabled': '是否启用自动更新',
    'Update.channel': '更新通道：preview（含预发布）/ stable（仅正式版）',
}


class ConfigManager:
    """配置管理器，负责配置初始化、加载、验证"""

    def __init__(self, config_file: str, logger: logging.Logger,
                 default_sections: Optional[Dict[str, Dict[str, str]]] = None,
                 comments: Optional[Dict[str, str]] = None,
                 app_name: str = '',
                 first_run_callback: Optional[Callable[[], None]] = None):
        """
        初始化配置管理器

        Args:
            config_file: 配置文件路径
            logger: 日志记录器
            default_sections: 默认节和键值对，形如 {'Section': {'key': 'default'}}
            comments: 键注释，形如 {'Section.key': '注释内容'}
            app_name: 应用名称（用于系统临时目录回退）
            first_run_callback: 首次运行（无配置文件时生成默认配置后）的回调，
                                不传则直接 sys.exit(0)
        """
        self.config_file = config_file
        self.logger = logger
        self.config = configparser.ConfigParser(strict=False)

        # 合并：先内置默认，再用户值覆盖
        merged_sections: Dict[str, Dict[str, str]] = {}
        for section, keys in _DEFAULT_SECTIONS.items():
            merged_sections[section] = dict(keys)
        if default_sections:
            for section, keys in default_sections.items():
                if section in merged_sections:
                    merged_sections[section].update(keys)
                else:
                    merged_sections[section] = dict(keys)
        self.default_sections = merged_sections

        merged_comments: Dict[str, str] = {}
        merged_comments.update(_DEFAULT_COMMENTS)
        if comments:
            merged_comments.update(comments)
        self.comments = merged_comments
        self.app_name = app_name
        self._first_run_callback = first_run_callback

        # ── 缓存所有「路径类」键名，用于 load() 中 resolve_temp_folder ──
        self._path_keys: set = set()
        self._list_keys: set = set()
        self._int_keys: set = set()
        self._bool_keys: set = set()

        # 动态属性字典
        self._attrs: Dict[str, str] = {}

    def _build_default_config(self) -> str:
        """从 default_sections + comments 生成默认配置文件内容"""
        lines = []
        for section, keys in self.default_sections.items():
            lines.append(f'[{section}]')
            for key, val in keys.items():
                comment = self.comments.get(f'{section}.{key}', '')
                if comment:
                    for cl in comment.split('\n'):
                        lines.append(f'# {cl}')
                lines.append(f'{key} = {val}')
            lines.append('')
        return '\n'.join(lines)

    def _generate_default_config(self) -> None:
        """生成默认配置文件并退出（首次运行）"""
        default_config = self._build_default_config()
        tmp_path = self.config_file + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write(default_config)
            os.replace(tmp_path, self.config_file)
            self.logger.info(f"已生成默认配置文件: {self.config_file}")
            self.logger.info("请修改配置文件后重新运行软件。")
        except OSError as e:
            self.logger.error(f"生成配置文件失败: {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            sys.exit(1)
        if self._first_run_callback:
            self._first_run_callback()
        else:
            sys.exit(0)

    def _regenerate_config_file(self) -> None:
        """
        重建配置文件，保留所有已有值，仅补充缺失的模板键。
        """
        lines = []
        for section in self.config.sections():
            if section.upper() == 'DEFAULT' or section == '__migrations__':
                continue
            lines.append(f'[{section}]')
            template = self.default_sections.get(section, {})
            written_keys = set()

            for key, default_val in template.items():
                written_keys.add(key)
                comment = self.comments.get(f'{section}.{key}', '')
                if comment:
                    for cl in comment.split('\n'):
                        lines.append(f'# {cl}')
                current = self.config.get(section, key, fallback=default_val)
                lines.append(f'{key} = {current}')

            for key, val in self.config.items(section):
                if key not in written_keys and key not in (self.config.defaults() or {}):
                    if not key.strip():
                        continue
                    lines.append(f'{key} = {val}')

            lines.append('')

        for section, keys in self.default_sections.items():
            if not self.config.has_section(section):
                lines.append(f'[{section}]')
                for key, val in keys.items():
                    comment = self.comments.get(f'{section}.{key}', '')
                    if comment:
                        for cl in comment.split('\n'):
                            lines.append(f'# {cl}')
                    lines.append(f'{key} = {val}')
                lines.append('')

        if self.config.has_section('__migrations__'):
            lines.append('[__migrations__]')
            for key, val in self.config.items('__migrations__'):
                lines.append(f'{key} = {val}')
            lines.append('')

        tmp_path = self.config_file + '.tmp'
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            os.replace(tmp_path, self.config_file)
        except OSError as e:
            self.logger.error(f"写入配置文件失败: {e}")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _sanitize_config_file(self) -> None:
        """逐行清理损坏行：空键值行删除，无 = 行注释掉"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            return

        fixed = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith(';'):
                new_lines.append(line)
                continue
            if re.match(r'^\[.+\]$', stripped):
                new_lines.append(line)
                continue
            if '=' not in stripped:
                new_lines.append(f'# [已修复] {line}')
                fixed = True
                continue
            key, sep, val = stripped.partition('=')
            if not key.strip():
                fixed = True
                continue
            new_lines.append(line)

        if fixed:
            tmp_path = self.config_file + '.tmp'
            try:
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    f.writelines(new_lines)
                os.replace(tmp_path, self.config_file)
            except OSError as e:
                self.logger.error(f"修复配置文件失败: {e}")
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _recover_orphan_keys(self) -> bool:
        """将误归属的模板键还原到正确的节，返回是否做了修改"""
        changed = False
        defaults = self.config.defaults()
        if defaults:
            for key, val in list(defaults.items()):
                for section, keys in self.default_sections.items():
                    if (key in keys and self.config.has_section(section)
                            and not self.config.has_option(section, key)):
                        self.config.set(section, key, val)
                        self.config.remove_option('DEFAULT', key)
                        self.logger.warning(f"键 {key} 已还原到 [{section}]")
                        changed = True
                        break

        for source_section in list(self.config.sections()):
            if source_section.upper() == 'DEFAULT' or source_section == '__migrations__':
                continue
            template = self.default_sections.get(source_section, {})
            for key, val in list(self.config.items(source_section)):
                if not key.strip():
                    continue
                if key in (self.config.defaults() or {}):
                    continue
                if key in template:
                    continue
                for tgt_section, tgt_keys in self.default_sections.items():
                    if (key in tgt_keys and tgt_section != source_section
                            and self.config.has_section(tgt_section)
                            and not self.config.has_option(tgt_section, key)):
                        self.config.set(tgt_section, key, val)
                        self.config.remove_option(source_section, key)
                        self.logger.warning(
                            f"键 {key}={val} 从 [{source_section}] 还原到 [{tgt_section}]"
                        )
                        changed = True
                        break
        return changed

    def _ensure_temp_folder_exists(self) -> None:
        """确保临时文件夹存在，若创建失败则回退到系统临时文件夹"""
        temp_folder = self._attrs.get('temp_folder', '')
        if not temp_folder or os.path.exists(temp_folder):
            return
        try:
            os.makedirs(temp_folder, exist_ok=True)
            self.logger.info(f"已创建临时文件夹: {temp_folder}")
        except (OSError, PermissionError) as e:
            self.logger.warning(f"无法创建临时文件夹 {temp_folder}: {e}")
            system_temp = os.environ.get('TEMP', '')
            if system_temp:
                fallback = os.path.join(system_temp, self.app_name) if self.app_name else os.path.join(system_temp, 'Temp')
            else:
                local_app_data = os.environ.get('LOCALAPPDATA', '')
                if local_app_data:
                    fallback = os.path.join(local_app_data, 'Temp', self.app_name) if self.app_name else os.path.join(local_app_data, 'Temp')
                else:
                    fallback = os.path.join(_get_program_dir(), 'Temp')
            self._attrs['temp_folder'] = fallback
            self.logger.info(f"使用系统临时文件夹: {fallback}")
            os.makedirs(fallback, exist_ok=True)

    def register_path_key(self, key: str) -> None:
        """将配置键标记为路径类型（load 时会自动调用 resolve_temp_folder）"""
        self._path_keys.add(key)

    def register_list_key(self, key: str) -> None:
        """将配置键标记为逗号分隔列表类型（load 时会自动 split）"""
        self._list_keys.add(key)

    def register_int_key(self, key: str) -> None:
        """将配置键标记为 int 类型"""
        self._int_keys.add(key)

    def register_bool_key(self, key: str) -> None:
        """将配置键标记为 bool 类型"""
        self._bool_keys.add(key)

    def get_attr(self, key: str, default: str = '') -> str:
        """读取已加载的配置属性"""
        return self._attrs.get(key, default)

    def get_attr_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self._attrs.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def get_attr_bool(self, key: str, default: bool = False) -> bool:
        val = self._attrs.get(key, str(default).lower())
        return val.lower() in ('true', '1', 'yes', 'on')

    def get_attr_list(self, key: str) -> List[str]:
        val = self._attrs.get(key, '')
        if val:
            return [item.strip() for item in val.split(',') if item.strip()]
        return []

    def load(self) -> None:
        """加载配置文件并填充内部属性"""
        if not os.path.exists(self.config_file):
            self.logger.info("配置文件不存在，将生成默认配置文件")
            self._generate_default_config()

        # 读取配置文件
        self.config = configparser.ConfigParser(strict=False)
        for pass_num in range(3):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.config.read_file(f)
                break
            except configparser.Error as e:
                if pass_num == 0:
                    self.logger.warning(f"配置文件解析错误，正在尝试修复: {e}")
                    self._sanitize_config_file()
                elif pass_num == 1:
                    self.logger.critical("修复失败，将重新生成配置文件")
                    self._generate_default_config()
                else:
                    self.logger.critical(f"配置文件无法修复: {e}")
                    self.logger.critical(f"配置文件 {self.config_file} 已损坏且无法自动修复。")
                    self.logger.critical("请检查文件内容或删除后重新运行软件以生成默认配置。")
                    input("按任意键退出...")
                    raise SystemExit(1)

        # ── 应用迁移 ──
        migrated = apply_migrations(self.config, self.logger)
        dirty = migrated

        # ── 补充缺失节 ──
        for section in self.default_sections:
            if not self.config.has_section(section):
                self.config.add_section(section)
                dirty = True

        # ── 孤键恢复 ──
        orphaned = self._recover_orphan_keys()

        # ── 补充缺失键 ──
        for section, keys in self.default_sections.items():
            for key, val in keys.items():
                if not self.config.has_option(section, key):
                    self.config.set(section, key, val)
                    dirty = True
                    self.logger.warning(f"配置节: [{section}] 缺少键: {key}，已自动补充默认值")

        if dirty or orphaned:
            self._regenerate_config_file()

        # ── 填充属性 ──
        self._attrs.clear()
        for section, keys in self.default_sections.items():
            for key in keys:
                val = self.config.get(section, key, fallback='')
                self._attrs[key] = val

        # 临时文件夹特殊处理
        if 'temp_folder' in self._path_keys or 'temp_folder' in self._attrs:
            temp_folder_config = self.config.get('Paths', 'temp_folder', fallback='Temp').strip()
            self._attrs['temp_folder'] = resolve_temp_folder(
                temp_folder_config, self.app_name, _get_program_dir(), self.logger
            )
            self._ensure_temp_folder_exists()

        for key in self._path_keys:
            if key == 'temp_folder' or key not in self._attrs:
                continue
            self._attrs[key] = resolve_temp_folder(
                self._attrs[key].strip(), self.app_name, _get_program_dir(), self.logger
            )

        # 类型转换
        for key in self._int_keys:
            if key in self._attrs:
                try:
                    self._attrs[key] = str(int(self._attrs[key]))
                except ValueError:
                    pass

        for key in self._bool_keys:
            if key in self._attrs:
                val = self._attrs[key].lower()
                self._attrs[key] = str(val in ('true', '1', 'yes', 'on'))

    def validate(self) -> bool:
        """
        验证配置文件是否合法

        子类可覆盖此方法添加项目特定的校验逻辑。
        默认仅检查临时文件夹可写。

        Returns:
            bool: 配置是否合法
        """
        temp_folder = self._attrs.get('temp_folder', '')
        if temp_folder:
            try:
                Path(temp_folder).mkdir(parents=True, exist_ok=True)
                self.logger.debug(f"临时文件夹路径: {temp_folder}")
            except Exception as e:
                self.logger.error(f"临时文件夹路径错误: {e}")
                return False

        self.logger.info("配置验证通过")
        return True
