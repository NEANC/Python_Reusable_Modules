#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""self_updater 模块的回归测试。"""

import hashlib
import logging
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from self_updater.self_config import UpdateState
from self_updater.self_updater import SelfUpdater


class FakeResponse:
    """用于模拟 requests.get 返回值的响应对象。"""

    def __init__(self, payload, status_code=200):
        """初始化模拟响应。"""
        self._payload = payload
        self.status_code = status_code

    def json(self):
        """返回预设的 JSON 数据。"""
        return self._payload

    def raise_for_status(self):
        """模拟成功响应。"""
        return None


class FakeExitedProcess:
    """用于模拟已退出的 PowerShell helper 进程。"""

    returncode = 123

    def poll(self):
        """返回非 None，表示进程已退出。"""
        return self.returncode

    def kill(self):
        """模拟终止进程。"""
        return None


class SelfUpdaterReviewFixesTest(unittest.TestCase):
    """覆盖代码审查反馈中的关键修复项。"""

    def make_updater(self, current_version="v1.0.0"):
        """创建测试用 SelfUpdater 实例。"""
        return SelfUpdater(
            github_repo="owner/repo",
            asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\\d.]+.*\\.exe$",
            app_name="App",
            current_version=current_version,
            proxy="",
            logger=logging.getLogger("SelfUpdaterTest"),
            is_bundled=True,
            package_type="Nuitka",
        )

    def test_update_state_uses_explicit_base_dir(self):
        """UpdateState 应支持显式目录，避免依赖 sys.argv[0]。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateState(base_dir=temp_dir)
            state["state"] = "verified"
            state["target"] = str(Path(temp_dir) / "App.exe")
            state.save()

            loaded = UpdateState.load(base_dir=temp_dir)

            self.assertIsNotNone(loaded)
            self.assertEqual("verified", loaded["state"])
            self.assertTrue((Path(temp_dir) / "update_state.ini").exists())

    def test_update_state_defaults_include_ps1_status_fields(self):
        """Python 与 PowerShell 使用的状态字段应保持一致。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateState(base_dir=temp_dir)

            self.assertEqual("", state["current_step"])
            self.assertEqual("", state["level"])

    def test_preview_channel_selects_highest_valid_release(self):
        """preview 通道应按语义版本选择最高有效 release。"""
        releases = [
            {"draft": False, "tag_name": "v1.1.0"},
            {"draft": False, "tag_name": "v1.3.0-build.gabc"},
            {"draft": False, "tag_name": "v1.2.0"},
            {"draft": True, "tag_name": "v9.9.9"},
        ]
        updater = self.make_updater(current_version="v1.0.0")

        with patch("self_updater.self_updater.requests.get", return_value=FakeResponse(releases)):
            release = updater._fetch_latest_release()

        self.assertIsNotNone(release)
        self.assertEqual("v1.2.0", release["tag_name"])

    def test_replace_executable_records_helper_start_failure(self):
        """helper 启动失败时应写入状态文件的 last_error。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            current_exe = base_dir / "App.exe"
            current_exe.write_bytes(b"old")
            tmp_path = base_dir / "downloaded.exe"
            tmp_path.write_bytes(b"new")
            sha_path = base_dir / "downloaded.sha256"
            sha_path.write_text(hashlib.sha256(b"new").hexdigest(), encoding="ascii")
            updater = self.make_updater()

            with patch("self_updater.self_updater.get_exe_path", return_value=current_exe), \
                    patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls, \
                    patch("self_updater.self_updater.subprocess.Popen", return_value=FakeExitedProcess()):
                state_cls.side_effect = lambda *args, **kwargs: UpdateState(base_dir=base_dir)
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(base_dir=base_dir)
                with self.assertRaises(RuntimeError):
                    updater._replace_executable(
                        tmp_path,
                        sha_path,
                        "v1.2.0",
                        "old-sha",
                        hashlib.sha256(b"new").hexdigest(),
                    )

            loaded = UpdateState.load(base_dir=base_dir)
            self.assertIsNotNone(loaded)
            self.assertIn("helper.ps1", loaded["last_error"])

    def test_generated_ps1_writes_current_step(self):
        """生成的 PS1 状态字段应写 current_step 而不是 step。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self.make_updater()
            updater._generate_helper_ps1(Path(temp_dir))
            updater._generate_update_ps1(Path(temp_dir))

            helper_text = (Path(temp_dir) / "App_Update_Helper.ps1").read_text(encoding="utf-8-sig")
            update_text = (Path(temp_dir) / "App_Update.ps1").read_text(encoding="utf-8-sig")

            self.assertIn('Write-IniValue "State" "current_step" $step', helper_text)
            self.assertIn('Write-IniValue "State" "current_step" $step', update_text)
            self.assertNotIn('Write-IniValue "State" "step" $step', helper_text)
            self.assertNotIn('Write-IniValue "State" "step" $step', update_text)

    def test_generated_ps1_uses_dotnet_sha256_instead_of_get_file_hash(self):
        """生成的 PS1 不应依赖 Get-FileHash cmdlet。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self.make_updater()
            updater._generate_helper_ps1(Path(temp_dir))
            updater._generate_update_ps1(Path(temp_dir))

            helper_text = (Path(temp_dir) / "App_Update_Helper.ps1").read_text(encoding="utf-8-sig")
            update_text = (Path(temp_dir) / "App_Update.ps1").read_text(encoding="utf-8-sig")
            combined_text = helper_text + update_text

            self.assertNotIn("Get-FileHash", combined_text)
            self.assertEqual(2, combined_text.count("function Get-SHA256"))
            self.assertEqual(2, combined_text.count("[System.Security.Cryptography.SHA256]::Create()"))
            self.assertIn("$actual = Get-SHA256 $target", helper_text)
            self.assertIn("$actual = Get-SHA256 $newFile", update_text)
            self.assertGreaterEqual(combined_text.count("$sha256.Dispose()"), 2)
            self.assertGreaterEqual(combined_text.count("$stream.Dispose()"), 2)

    def test_readme_documents_verify_version_func_requirement(self):
        """README 应说明未传 version_func 时只校验 SHA256。"""
        readme_path = Path(__file__).resolve().parents[1] / "self_updater" / "README.md"
        text = readme_path.read_text(encoding="utf-8")

        self.assertIn("未传 version_func 时仅校验 SHA256", text)
        self.assertNotIn("return  # 或 sys.exit(1)", text)


if __name__ == "__main__":
    unittest.main()
