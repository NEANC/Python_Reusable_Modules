#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""download 独立模块测试。"""

import importlib
import inspect
import unittest


class DownloadModuleTest(unittest.TestCase):
    """覆盖 download 模块的公开导入和进度条能力。"""

    def test_download_progress_exports_download_progress_bar(self):
        """download.progress 应提供下载专用进度条能力。"""
        progress = importlib.import_module("download.progress")

        self.assertTrue(hasattr(progress, "DownloadProgressBar"))
        self.assertTrue(hasattr(progress, "create_download_progress_bar"))
        self.assertTrue(hasattr(progress, "format_ok"))
        self.assertTrue(hasattr(progress, "format_error"))

    def test_create_download_progress_bar_injects_download_rate_field(self):
        """下载进度条应支持 download_rate_fmt 字段。"""
        from download.progress import create_download_progress_bar

        bar = create_download_progress_bar(total=1, desc="下载测试", disable=True)
        try:
            bar.download_rate_fmt = "1.00KiB/s"
            self.assertEqual("1.00KiB/s", bar.format_dict["download_rate_fmt"])
        finally:
            bar.close()

    def test_download_progress_does_not_import_self_updater(self):
        """download.progress 不应依赖 self_updater。"""
        progress = importlib.import_module("download.progress")
        source = inspect.getsource(progress)

        self.assertNotIn("self_updater", source)

    def test_download_manager_can_be_imported_from_public_package(self):
        """外部调用方应能从 download 直接导入 DownloadManager。"""
        from download import DownloadManager
        from download.manager import DownloadManager as ManagerFromModule

        self.assertIs(ManagerFromModule, DownloadManager)

    def test_download_manager_module_does_not_import_self_updater(self):
        """download.manager 不应依赖 self_updater。"""
        manager = importlib.import_module("download.manager")
        source = inspect.getsource(manager)

        self.assertNotIn("self_updater", source)
        self.assertIn("from .progress import", source)

    def test_download_manager_stores_constructor_arguments(self):
        """DownloadManager 应保持独立可实例化。"""
        import logging
        from download import DownloadManager

        logger = logging.getLogger("DownloadModuleTest")
        manager = DownloadManager(
            proxy="http://127.0.0.1:7890",
            temp_folder="C:/Temp/App",
            logger=logger,
            download_threads=2,
        )

        self.assertEqual("http://127.0.0.1:7890", manager.proxy)
        self.assertEqual("C:/Temp/App", manager.temp_folder)
        self.assertIs(logger, manager.logger)
        self.assertEqual(2, manager.download_threads)

    def test_legacy_self_updater_download_modules_are_removed(self):
        """旧 self_updater 下载入口不应继续存在。"""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]

        self.assertFalse((repo_root / "self_updater" / "download_manager.py").exists())
        self.assertFalse((repo_root / "self_updater" / "progress_bar.py").exists())

    def test_readme_documents_download_module_relationship(self):
        """README 应说明 self_updater 与 download 模块关系。"""
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        root_readme = (repo_root / "README.md").read_text(encoding="utf-8")
        updater_readme = (repo_root / "self_updater" / "README.md").read_text(encoding="utf-8")
        combined = root_readme + "\n" + updater_readme

        self.assertIn("from download import DownloadManager", combined)
        self.assertIn("download_func", updater_readme)
        self.assertIn("download_file_with_progress", combined)
        self.assertNotIn("PYPDL", combined)
        self.assertNotIn("pypdl", combined)
        self.assertNotIn("download_backend", combined)
        self.assertNotIn("download_segments", combined)
        self.assertNotIn("download_retries", combined)
        self.assertNotIn("download_timeout", combined)
