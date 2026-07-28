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
