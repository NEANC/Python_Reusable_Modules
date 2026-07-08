#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""配置迁移模块：自动将旧配置文件升级到最新格式"""

import configparser
import logging


MIGRATION_MARKER = '__migrations__'

MIGRATIONS = [
    # 示例迁移（可根据未来需求添加）：
    # {
    #     'id': 1,
    #     'type': 'rename_key',     # 重命名键
    #     'section': 'settings',    # 目标节
    #     'old_key': 'old_name',    # 旧键
    #     'new_key': 'new_name',    # 新键
    #     'description': '重命名 [settings].old_name → [settings].new_name', # 日志输出描述
    # },
    # {
    #     'id': 2,
    #     'type': 'rename_value',
    #     'section': 'settings',
    #     'key': 'some_key',
    #     'old_value': 'old_val',
    #     'new_value': 'new_val',
    #     'description': 'settings some_key: old_val → new_val',
    # },
]


def _apply_rename_key(config: configparser.ConfigParser,
                       section: str, old_key: str, new_key: str) -> bool:
    """通用键重命名：section 下 old_key → new_key，保留值"""
    if not config.has_section(section):
        return False
    if not config.has_option(section, old_key):
        return False
    old_val = config.get(section, old_key)
    if not config.has_option(section, new_key):
        config.set(section, new_key, old_val)
    config.remove_option(section, old_key)
    return True


def _apply_rename_value(config: configparser.ConfigParser,
                         section: str, key: str, old_value: str,
                         new_value: str) -> bool:
    """通用值迁移：section 下 key 的 old_value → new_value"""
    if not config.has_section(section):
        return False
    if not config.has_option(section, key):
        return False
    if config.get(section, key).strip() != old_value:
        return False
    config.set(section, key, new_value)
    return True


MIGRATION_HANDLERS = {
    'rename_key': _apply_rename_key,
    'rename_value': _apply_rename_value,
}


def apply_migrations(config: configparser.ConfigParser,
                     logger: logging.Logger) -> bool:
    """
    在内存中应用所有待处理的迁移

    Returns:
        bool: 是否执行了迁移（调用方据此触发文件重建）
    """
    applied = _get_applied_migrations(config, logger)
    changed = False

    for migration in MIGRATIONS:
        mid = migration['id']
        if mid in applied:
            continue
        try:
            handler = MIGRATION_HANDLERS[migration['type']]
            kwargs = {k: v for k, v in migration.items()
                      if k not in ('id', 'type', 'description')}
            if not handler(config, **kwargs):
                continue
            desc = migration.get('description', f'#{mid}')
            logger.info(f"检测到需要迁移 [{mid}]: {desc}")
            _mark_applied(config, mid)
            applied.add(mid)
            changed = True
            logger.info(f"配置迁移 [{mid}] 完成")
        except Exception as e:
            logger.warning(f"配置迁移 [{mid}] 失败: {e}")

    return changed


def _get_applied_migrations(config: configparser.ConfigParser,
                            logger: logging.Logger) -> set:
    """读取已应用迁移 ID，跳过损坏的迁移标记。"""
    applied = set()
    if not config.has_section(MIGRATION_MARKER):
        return applied

    for key, value in config.items(MIGRATION_MARKER):
        if value != 'done':
            continue
        try:
            applied.add(int(key))
        except ValueError:
            logger.warning(f"忽略非法迁移标记: {key} = {value}")
    return applied


def _mark_applied(config: configparser.ConfigParser, migration_id: int) -> None:
    if not config.has_section(MIGRATION_MARKER):
        config.add_section(MIGRATION_MARKER)
    config.set(MIGRATION_MARKER, str(migration_id), 'done')
