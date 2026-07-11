#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""config_ini 模块的回归测试。"""

import configparser
import io
import logging
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from config_ini.config_manager import ConfigManager
from config_ini.config_migration import MIGRATIONS
from config_ini.config_migration import apply_migrations


DEFAULT_SECTIONS = {
    "Paths": {
        "temp_folder": "Temp",
        "install_folder": r"C:\\MyApp",
    },
    "Update": {
        "channel": "stable",
    },
}

COMMENTS = {
    "Paths.temp_folder": "临时目录",
    "Paths.install_folder": "安装目录",
    "Update.channel": "更新通道",
}


class ConfigIniReviewFixesTest(unittest.TestCase):
    """覆盖 config_ini 代码审查反馈中的关键修复项。"""

    def setUp(self):
        """保存全局迁移配置，避免测试互相影响。"""
        self._old_migrations = list(MIGRATIONS)
        self.logger = logging.getLogger("ConfigIniReviewFixesTest")

    def tearDown(self):
        """恢复全局迁移配置。"""
        MIGRATIONS.clear()
        MIGRATIONS.extend(self._old_migrations)

    def make_manager(self, config_path: Path) -> ConfigManager:
        """创建测试用 ConfigManager。"""
        return ConfigManager(
            config_file=str(config_path),
            logger=self.logger,
            default_sections=DEFAULT_SECTIONS,
            comments=COMMENTS,
            app_name="TestApp",
            first_run_callback=lambda: None,
        )

    def test_apply_migrations_ignores_invalid_applied_ids(self):
        """迁移标记中存在非法 ID 时不应中断迁移流程。"""
        config = configparser.ConfigParser(strict=False)
        config.read_file(io.StringIO(
            "[Update]\n"
            "old_channel = preview\n"
            "[__migrations__]\n"
            "abc = done\n"
        ))
        MIGRATIONS.clear()
        MIGRATIONS.append({
            "id": 1,
            "type": "rename_key",
            "section": "Update",
            "old_key": "old_channel",
            "new_key": "channel",
            "description": "old_channel -> channel",
        })

        changed = apply_migrations(config, self.logger)

        self.assertTrue(changed)
        self.assertEqual("preview", config.get("Update", "channel"))
        self.assertEqual("done", config.get("__migrations__", "1"))

    def test_generate_default_config_does_not_call_input_without_callback(self):
        """未提供首次运行回调时生成默认配置不应阻塞等待 input。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            manager = ConfigManager(
                config_file=str(config_path),
                logger=self.logger,
                default_sections=DEFAULT_SECTIONS,
                comments=COMMENTS,
                app_name="TestApp",
            )

            with patch("builtins.input", side_effect=AssertionError("不应调用 input")):
                with self.assertRaises(SystemExit) as context:
                    manager.load()

            self.assertEqual(0, context.exception.code)
            self.assertTrue(config_path.exists())

    def test_load_resets_parser_between_calls(self):
        """重复 load 时不应保留配置文件中已删除的旧键。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text(
                "[Paths]\n"
                "temp_folder = Temp\n"
                "install_folder = C:\\MyApp\n"
                "stale_key = old\n"
                "[Update]\n"
                "channel = stable\n",
                encoding="utf-8",
            )
            manager = self.make_manager(config_path)
            manager.load()
            self.assertIn("stale_key", dict(manager.config.items("Paths")))

            config_path.write_text(
                "[Paths]\n"
                "temp_folder = Temp\n"
                "install_folder = C:\\MyApp\n"
                "[Update]\n"
                "channel = stable\n",
                encoding="utf-8",
            )
            manager.load()

            self.assertNotIn("stale_key", dict(manager.config.items("Paths")))

    def test_regenerate_config_preserves_migration_marker(self):
        """重建配置文件时应保留已应用迁移记录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text(
                "[Paths]\n"
                "temp_folder = Temp\n"
                "install_folder = C:\\MyApp\n"
                "[Update]\n"
                "old_channel = preview\n",
                encoding="utf-8",
            )
            MIGRATIONS.clear()
            MIGRATIONS.append({
                "id": 7,
                "type": "rename_key",
                "section": "Update",
                "old_key": "old_channel",
                "new_key": "channel",
                "description": "old_channel -> channel",
            })
            manager = self.make_manager(config_path)

            manager.load()

            text = config_path.read_text(encoding="utf-8")
            self.assertIn("[__migrations__]", text)
            self.assertIn("7 = done", text)

    def test_load_sanitizes_key_value_before_first_section(self):
        """节外键值行不应导致合法配置被重建为默认配置。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            first_run_calls = []
            config_path.write_text(
                "temp_folder = Temp\n"
                "[Paths]\n"
                "install_folder = C:\\CustomApp\n"
                "[Update]\n"
                "channel = preview\n",
                encoding="utf-8",
            )
            manager = ConfigManager(
                config_file=str(config_path),
                logger=self.logger,
                default_sections=DEFAULT_SECTIONS,
                comments=COMMENTS,
                app_name="TestApp",
                first_run_callback=lambda: first_run_calls.append("called"),
            )

            manager.load()

            self.assertEqual([], first_run_calls)
            self.assertEqual(r"C:\CustomApp", manager.get_attr("install_folder"))
            self.assertEqual("preview", manager.get_attr("channel"))

    def test_registered_path_keys_are_resolved(self):
        """register_path_key 应对注册的路径键生效。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.ini"
            config_path.write_text(
                "[Paths]\n"
                "temp_folder = Temp\n"
                "install_folder = Temp\n"
                "[Update]\n"
                "channel = stable\n",
                encoding="utf-8",
            )
            manager = self.make_manager(config_path)
            manager.register_path_key("install_folder")

            manager.load()

            self.assertTrue(Path(manager.get_attr("install_folder")).is_absolute())
            self.assertTrue(manager.get_attr("install_folder").endswith("Temp"))


if __name__ == "__main__":
    unittest.main()
