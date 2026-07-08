#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""logger 模块代码审查修复的回归测试。"""

import configparser
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from logger import add_file_logger
from logger import cleanup_old_logs
from logger import raw_read_save_enabled
from logger import setup_logger


class LoggerReviewFixesTest(unittest.TestCase):
    """验证 logger 审查反馈中的关键行为。"""

    def tearDown(self) -> None:
        """清理测试中创建的 logger handler。"""
        for name in ["test_console_dedupe", "test_file_dedupe", "test_cleanup"]:
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_setup_logger_does_not_duplicate_console_handler(self) -> None:
        """重复初始化同名 logger 时不应重复添加控制台 handler。"""
        logger = setup_logger("test_console_dedupe")
        setup_logger("test_console_dedupe")

        stream_handlers = [
            handler for handler in logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
        ]

        self.assertEqual(1, len(stream_handlers))
        self.assertEqual(logging.DEBUG, logger.level)
        self.assertFalse(logger.propagate)

    def test_console_formatter_uses_default_required_format(self) -> None:
        """控制台日志格式应符合默认规则。"""
        logger = setup_logger("test_console_dedupe")
        handler = next(
            handler for handler in logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
        )

        self.assertEqual(
            "%(levelname)s | %(asctime)s.%(msecs)03d | %(message)s",
            handler.formatter._style._fmt,
        )
        self.assertEqual("%H:%M:%S", handler.formatter.datefmt)

    def test_add_file_logger_uses_nested_dir_and_deduplicates_handler(self) -> None:
        """文件日志应支持嵌套目录，并避免重复添加同一路径 handler。"""
        logger = setup_logger("test_file_dedupe")
        with tempfile.TemporaryDirectory() as temp_dir:
            nested_dir = Path(temp_dir) / "nested" / "logs"

            first_handler = add_file_logger(
                logger,
                version="v1.0.0",
                log_dir=str(nested_dir),
                log_prefix="App",
            )
            second_handler = add_file_logger(
                logger,
                version="v1.0.0",
                log_dir=str(nested_dir),
                log_prefix="App",
            )

            file_handlers = [
                handler for handler in logger.handlers
                if isinstance(handler, logging.FileHandler)
            ]

            try:
                self.assertTrue(nested_dir.exists())
                self.assertIs(first_handler, second_handler)
                self.assertEqual(1, len(file_handlers))
                self.assertRegex(
                    Path(first_handler.baseFilename).name,
                    r"^App_\d{8}_\d{6}\.log$",
                )
                self.assertEqual(
                    "%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
                    first_handler.formatter._style._fmt,
                )
            finally:
                logger.removeHandler(first_handler)
                first_handler.close()

    def test_raw_read_save_enabled_defaults_to_false(self) -> None:
        """缺失或异常配置默认不保存日志。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing.ini"
            broken_path = Path(temp_dir) / "broken.ini"
            true_path = Path(temp_dir) / "true.ini"

            broken_path.write_text("[Logs\nsave_enabled = true", encoding="utf-8")

            config = configparser.ConfigParser()
            config["Logs"] = {"save_enabled": "true"}
            with true_path.open("w", encoding="utf-8") as file_obj:
                config.write(file_obj)

            self.assertFalse(raw_read_save_enabled(str(missing_path)))
            self.assertFalse(raw_read_save_enabled(str(broken_path)))
            self.assertTrue(raw_read_save_enabled(str(true_path)))

    def test_cleanup_old_logs_supports_zero_and_days_limit(self) -> None:
        """日志清理应支持保留 0 个文件和按日期清理。"""
        logger = setup_logger("test_cleanup")
        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)
            old_log = log_dir / "App_old.log"
            new_log = log_dir / "App_new.log"
            other_log = log_dir / "Other_old.log"

            for path in [old_log, new_log, other_log]:
                path.write_text("log", encoding="utf-8")

            old_time = time.time() - 9 * 24 * 60 * 60
            os.utime(old_log, (old_time, old_time))
            os.utime(other_log, (old_time, old_time))

            cleanup_old_logs(
                logger,
                max_files=15,
                max_days=7,
                log_dir=str(log_dir),
                log_prefix="App",
            )

            self.assertFalse(old_log.exists())
            self.assertTrue(new_log.exists())
            self.assertTrue(other_log.exists())

            cleanup_old_logs(
                logger,
                max_files=0,
                max_days=7,
                log_dir=str(log_dir),
                log_prefix="App",
            )

            self.assertFalse(new_log.exists())
            self.assertTrue(other_log.exists())


if __name__ == "__main__":
    unittest.main()
