#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""self_updater 模块的回归测试。"""

import hashlib
import inspect
import logging
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import requests

from self_updater import ps1_fragments
from self_updater.self_config import UpdateState
from self_updater.self_updater import SelfUpdater


class FakeResponse:
    """用于模拟 requests.get 返回值的响应对象。"""

    def __init__(self, payload, status_code=200):
        """初始化模拟响应。"""
        self._payload = payload
        self.status_code = status_code
        self.raise_called = False

    def json(self):
        """返回预设的 JSON 数据。"""
        return self._payload

    def iter_content(self, chunk_size=1048576):
        """模拟分块读取响应体。"""
        payload = self._payload
        if isinstance(payload, bytes):
            yield payload
        else:
            yield str(payload).encode("utf-8")

    def raise_for_status(self):
        """模拟成功响应。"""
        self.raise_called = True
        return None


class FakeRequestFailureResponse:
    """用于模拟 raise_for_status 失败的响应对象。"""

    def iter_content(self, chunk_size=1048576):
        """失败场景下不返回任何分块。"""
        return iter(())

    def raise_for_status(self):
        """抛出 requests 请求异常。"""
        raise requests.RequestException("boom")


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

    def make_updater(self, current_version="v1.0.0", app_name="App", temp_folder=None):
        """创建测试用 SelfUpdater 实例。"""
        return SelfUpdater(
            github_repo="owner/repo",
            asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
            app_name=app_name,
            current_version=current_version,
            proxy="",
            logger=logging.getLogger("SelfUpdaterTest"),
            temp_folder=temp_folder,
            is_bundled=True,
            package_type="Nuitka",
        )

    def _make_runtime_paths(self, root: Path, temp_folder_name="self-update"):
        """构造测试用程序目录、临时目录和运行时路径。"""
        program_dir = root / "program"
        temp_folder = root / temp_folder_name
        program_dir.mkdir()
        current_exe = program_dir / "App.exe"
        current_exe.write_bytes(b"old")
        updater = self.make_updater(temp_folder=str(temp_folder))
        paths = updater._build_update_runtime_paths(current_exe, "v1.2.0")
        paths["runtime_dir"].mkdir(parents=True, exist_ok=True)
        return updater, current_exe, paths

    @staticmethod
    def _ps1_expected_path(path: Path) -> str:
        """转换测试期望的 PowerShell 双引号路径内容。"""
        return str(path).replace('`', '``').replace('"', '`"').replace('$', '`$')

    def test_resolve_temp_folder_uses_localappdata_selfupdate_by_default(self):
        """未传 temp_folder 时应默认使用 LOCALAPPDATA 下的应用自更新目录。"""
        with patch.dict("os.environ", {"LOCALAPPDATA": r"C:\Users\User\AppData\Local"}, clear=True):
            with patch("pathlib.Path.mkdir"):
                updater = self.make_updater(app_name="App")

        self.assertEqual(
            str(Path(r"C:\Users\User\AppData\Local") / "App" / "SelfUpdate"),
            updater.temp_folder,
        )

    def test_resolve_temp_folder_keeps_explicit_value(self):
        """传入 temp_folder 时应直接使用调用方指定目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = SelfUpdater(
                github_repo="owner/repo",
                asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
                app_name="App",
                current_version="v1.0.0",
                proxy="",
                logger=logging.getLogger("SelfUpdaterTest"),
                temp_folder=temp_dir,
                is_bundled=True,
                package_type="Nuitka",
            )

        self.assertEqual(temp_dir, updater.temp_folder)

    def test_build_update_runtime_paths_separates_program_and_runtime_files(self):
        """运行时路径 helper 应区分程序目录文件和 runtime_dir 文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program_dir = root / "program"
            temp_folder = root / "self-update"
            program_dir.mkdir()
            current_exe = program_dir / "App.exe"
            current_exe.write_bytes(b"old")
            updater = SelfUpdater(
                github_repo="owner/repo",
                asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
                app_name="App",
                current_version="v1.0.0",
                proxy="",
                logger=logging.getLogger("SelfUpdaterTest"),
                temp_folder=str(temp_folder),
                is_bundled=True,
                package_type="Nuitka",
            )

            paths = updater._build_update_runtime_paths(current_exe, "v1.2.0")

            self.assertEqual(program_dir, paths["program_dir"])
            self.assertEqual(program_dir / "update_state.ini", paths["state_file"])
            self.assertEqual(program_dir / "update.log", paths["log_file"])
            self.assertEqual(temp_folder, paths["temp_folder"])
            self.assertEqual(temp_folder / "v1.2.0", paths["runtime_dir"])
            self.assertEqual(temp_folder / "v1.2.0" / "App_Update_Helper.ps1", paths["helper_ps1"])
            self.assertEqual(temp_folder / "v1.2.0" / "App_Update.ps1", paths["update_ps1"])
            self.assertEqual(temp_folder / "v1.2.0" / "update_started.lock", paths["lock_file"])
            self.assertEqual(temp_folder / "v1.2.0" / "App.new.exe", paths["new_file"])
            self.assertEqual(temp_folder / "v1.2.0" / "App.backup.exe", paths["backup_file"])

    def test_resolve_temp_folder_falls_back_to_program_selfupdate(self):
        """LOCALAPPDATA 不可用或创建失败时应回退到程序目录 SelfUpdate。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            program_path = Path(temp_dir) / "App.exe"
            mkdir_error = OSError("cannot create local appdata")
            with patch.dict("os.environ", {}, clear=True):
                with patch("self_updater.self_updater.sys.argv", [str(program_path)]):
                    with patch("pathlib.Path.mkdir", side_effect=mkdir_error):
                        updater = self.make_updater(app_name="App")

            self.assertEqual(str(Path(temp_dir) / "SelfUpdate"), updater.temp_folder)

    def _assert_sha256_fallbacks(self, content, script_name):
        """断言生成脚本包含 SHA256 多路径回退。"""
        self.assertIn("function Get-SHA256($filePath)", content, script_name)
        self.assertIn("[System.IO.File]::OpenRead($filePath)", content, script_name)
        self.assertIn("[System.Security.Cryptography.SHA256]::Create()", content, script_name)
        self.assertIn("$sha256.Dispose()", content, script_name)
        self.assertIn("$stream.Dispose()", content, script_name)
        self.assertIn("Get-Command Get-FileHash -ErrorAction SilentlyContinue", content, script_name)
        self.assertIn("Get-FileHash -Algorithm SHA256 -LiteralPath $filePath", content, script_name)
        self.assertIn("certutil.exe -hashfile", content, script_name)
        self.assertIn("^[0-9A-Fa-f]{64}$", content, script_name)
        self.assertIn('throw "Get-SHA256 failed:', content, script_name)

    def test_sha256_fragment_contains_fallbacks(self):
        """SHA256 片段应包含三层 fallback。"""
        content = ps1_fragments.generate_sha256_function_ps1()

        self._assert_sha256_fallbacks(content, "SHA256 fragment")
        self.assertEqual(1, content.count("function Get-SHA256($filePath)"))

    def test_common_fragments_include_shared_functions(self):
        """公共片段应包含 Helper 与 Update 共享函数。"""
        base = ps1_fragments.generate_common_base_functions_ps1()
        state = ps1_fragments.generate_common_state_functions_ps1()
        move = ps1_fragments.generate_move_with_retry_ps1()

        self.assertIn("function Normalize-IniValue", base)
        self.assertIn("function Assert-NotEmpty", base)
        self.assertIn("function Write-Log", base)
        self.assertIn("function Read-IniValue", state)
        self.assertIn("function Write-IniValue", state)
        self.assertIn("function Set-UpdateStatus", state)
        self.assertIn("function Move-WithRetry", move)

    def test_helper_fragments_include_helper_only_functions(self):
        """Helper 独有片段应包含 Helper 专用函数。"""
        args = ps1_fragments.generate_helper_argument_functions_ps1()
        retry = ps1_fragments.generate_helper_retry_functions_ps1()
        cleanup = ps1_fragments.generate_helper_file_cleanup_functions_ps1()
        lifecycle = ps1_fragments.generate_helper_lifecycle_functions_ps1()

        self.assertIn("function Quote-Arg", args)
        self.assertIn("function Get-RetryOrDefault", retry)
        self.assertIn("function Remove-WithRetry", cleanup)
        self.assertIn("function Commit-Update", lifecycle)
        self.assertIn("function Restore-Backup", lifecycle)
        self.assertIn("function Start-ProcWait", lifecycle)
        self.assertIn("function Start-NormalAppVisible", lifecycle)

    def test_fetch_current_release_sha256_requires_exact_package_type(self):
        """当前版本完整性校验不应降级匹配其他打包方式。"""
        pyinstaller_sha256 = "a" * 64
        release_info = {
            "assets": [
                {
                    "name": "App-PyInstaller-v1.0.0.exe",
                    "browser_download_url": "https://example.invalid/App-PyInstaller-v1.0.0.exe",
                    "digest": f"sha256:{pyinstaller_sha256}",
                },
            ],
        }
        updater = self.make_updater()

        with patch("self_updater.self_updater.requests.get", return_value=FakeResponse(release_info)):
            actual = updater._fetch_current_release_sha256("Nuitka")

        self.assertEqual("", actual)

    def test_match_asset_keeps_fallback_for_download_flow(self):
        """下载新版本流程仍允许降级匹配另一种打包方式。"""
        release_info = {
            "assets": [
                {
                    "name": "App-PyInstaller-v1.2.0.exe",
                    "browser_download_url": "https://example.invalid/App-PyInstaller-v1.2.0.exe",
                },
            ],
        }
        updater = self.make_updater()

        exe_url, exe_name = updater._match_asset(release_info, "Nuitka")

        self.assertEqual("https://example.invalid/App-PyInstaller-v1.2.0.exe", exe_url)
        self.assertEqual("App-PyInstaller-v1.2.0.exe", exe_name)

    def test_app_name_rejects_unsafe_values(self):
        """应用名称应拒绝空值、路径分隔符和脚本注入字符。"""
        unsafe_names = (
            "",
            "Bad;Name",
            "Bad/Name",
            "Bad\\Name",
            "Bad:Name",
            "Bad*Name",
            "Bad?Name",
            "Bad\"Name",
            "Bad<Name",
            "Bad>Name",
            "Bad|Name",
            "Bad&Name",
            "Bad`Name",
            "Bad$Name",
            ".",
            "..",
            "...",
            "App.",
            ".App",
            "CON",
            "NUL",
            "AUX",
            "PRN",
            "COM1",
            "LPT1",
            "CON.txt",
            "NUL.log",
            "AUX.any",
            "PRN.1",
            "COM1.exe",
            "LPT1.tmp",
        )
        for app_name in unsafe_names:
            with self.subTest(app_name=app_name):
                with self.assertRaises(ValueError):
                    self.make_updater(app_name=app_name)

    def test_app_name_accepts_safe_value(self):
        """应用名称应允许字母、数字、下划线、点和连字符。"""
        updater = self.make_updater(app_name="App_Name-1.0")

        self.assertEqual("App_Name-1.0", updater.app_name)

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

    def test_update_state_defaults_include_runtime_path_fields(self):
        """状态文件默认字段应包含运行时路径字段。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateState(base_dir=temp_dir)

            self.assertEqual("", state["runtime_dir"])
            self.assertEqual("", state["helper_ps1"])
            self.assertEqual("", state["update_ps1"])
            self.assertEqual("", state["lock_file"])

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

    def test_replace_executable_records_runtime_paths_in_state(self):
        """替换准备流程应把运行时文件路径写入状态文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program_dir = root / "program"
            temp_folder = root / "self-update"
            program_dir.mkdir()
            current_exe = program_dir / "App.exe"
            current_exe.write_bytes(b"old")
            tmp_path = root / "downloaded.exe"
            tmp_path.write_bytes(b"new")
            sha_path = root / "downloaded.sha256"
            sha_path.write_text(hashlib.sha256(b"new").hexdigest(), encoding="ascii")
            updater = SelfUpdater(
                github_repo="owner/repo",
                asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
                app_name="App",
                current_version="v1.0.0",
                proxy="",
                logger=logging.getLogger("SelfUpdaterTest"),
                temp_folder=str(temp_folder),
                is_bundled=True,
                package_type="Nuitka",
            )

            with patch("self_updater.self_updater.get_exe_path", return_value=current_exe), \
                    patch("self_updater.self_updater.subprocess.Popen", return_value=FakeExitedProcess()):
                with self.assertRaises(RuntimeError):
                    updater._replace_executable(
                        tmp_path,
                        sha_path,
                        "v1.2.0",
                        "old-sha",
                        hashlib.sha256(b"new").hexdigest(),
                    )

            loaded = UpdateState.load(base_dir=program_dir)
            runtime_dir = temp_folder / "v1.2.0"
            self.assertIsNotNone(loaded)
            self.assertEqual(str(runtime_dir), loaded["runtime_dir"])
            self.assertEqual(str(runtime_dir / "App.new.exe"), loaded["new_file"])
            self.assertEqual(str(runtime_dir / "App.backup.exe"), loaded["backup_file"])
            self.assertEqual(str(runtime_dir / "App_Update_Helper.ps1"), loaded["helper_ps1"])
            self.assertEqual(str(runtime_dir / "App_Update.ps1"), loaded["update_ps1"])
            self.assertEqual(str(runtime_dir / "update_started.lock"), loaded["lock_file"])
            self.assertTrue((runtime_dir / "App.new.exe").exists())
            self.assertTrue((runtime_dir / "App_Update_Helper.ps1").exists())
            self.assertTrue((runtime_dir / "App_Update.ps1").exists())
            self.assertFalse((program_dir / "App.new.exe").exists())
            self.assertFalse((program_dir / "App_Update_Helper.ps1").exists())

    def test_rollback_restores_backup_from_runtime_dir(self):
        """回滚应使用状态文件中记录的 runtime_dir 备份文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            backup_file = runtime_dir / "App.backup.exe"
            current_exe.write_bytes(b"current-version")
            backup_file.write_bytes(b"backup-version")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "rollback"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["backup_file"] = str(backup_file)
            state.save()

            with patch("self_updater.self_config.sys.argv", [str(current_exe)]):
                result = updater.rollback(logging.getLogger("SelfUpdaterTest"))

            loaded = UpdateState.load(base_dir=program_dir)
            self.assertTrue(result)
            self.assertEqual(b"backup-version", current_exe.read_bytes())
            self.assertFalse(backup_file.exists())
            self.assertIsNotNone(loaded)
            self.assertEqual("rollback_done", loaded["state"])

    def test_cleanup_update_residue_uses_verified_state_runtime_paths_only(self):
        """清理更新残留时应只按 verified 状态文件中的运行时路径删除。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_file"] = str(paths["new_file"])
            state["backup_file"] = str(paths["backup_file"])
            state["helper_ps1"] = str(paths["helper_ps1"])
            state["update_ps1"] = str(paths["update_ps1"])
            state["lock_file"] = str(paths["lock_file"])
            state.save()
            runtime_files = (
                paths["new_file"],
                paths["backup_file"],
                paths["helper_ps1"],
                paths["update_ps1"],
                paths["lock_file"],
            )
            for runtime_file in runtime_files:
                runtime_file.write_text("runtime", encoding="utf-8")
            update_log = program_dir / "update.log"
            old_residue = program_dir / "App_Update.ps1"
            unrecorded_update_ps1 = program_dir / "Other_Update.ps1"
            unrecorded_backup_exe = program_dir / "Other.backup.exe"
            unknown_runtime_file = runtime_dir / "unknown.tmp"
            update_log.write_text("log", encoding="utf-8")
            old_residue.write_text("old residue", encoding="utf-8")
            unrecorded_update_ps1.write_text("unrecorded ps1", encoding="utf-8")
            unrecorded_backup_exe.write_text("unrecorded backup", encoding="utf-8")
            unknown_runtime_file.write_text("unknown", encoding="utf-8")

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(base_dir=program_dir)
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            for runtime_file in runtime_files:
                self.assertFalse(runtime_file.exists(), runtime_file)
            self.assertTrue(runtime_dir.exists())
            self.assertTrue(unknown_runtime_file.exists())
            self.assertTrue(update_log.exists())
            self.assertTrue(old_residue.exists())
            self.assertTrue(unrecorded_update_ps1.exists())
            self.assertTrue(unrecorded_backup_exe.exists())
            self.assertFalse((program_dir / UpdateState.STATE_FILE_NAME).exists())

    def test_cleanup_update_residue_skips_files_outside_runtime_dir(self):
        """清理更新残留时不应删除 runtime_dir 外部的记录路径。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            important_file = program_dir / "important.txt"
            runtime_file = runtime_dir / "App.new.exe"
            important_file.write_text("important", encoding="utf-8")
            runtime_file.write_text("runtime", encoding="utf-8")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_file"] = str(important_file)
            state["backup_file"] = str(runtime_file)
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(base_dir=program_dir)
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(important_file.exists())
            self.assertFalse(runtime_file.exists())
            self.assertFalse((program_dir / UpdateState.STATE_FILE_NAME).exists())

    def test_cleanup_update_residue_removes_empty_runtime_dir(self):
        """清理更新残留后应删除已清空的运行时目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_file"] = str(paths["new_file"])
            state["backup_file"] = str(paths["backup_file"])
            state["helper_ps1"] = str(paths["helper_ps1"])
            state["update_ps1"] = str(paths["update_ps1"])
            state["lock_file"] = str(paths["lock_file"])
            state.save()
            runtime_files = (
                paths["new_file"],
                paths["backup_file"],
                paths["helper_ps1"],
                paths["update_ps1"],
                paths["lock_file"],
            )
            for runtime_file in runtime_files:
                runtime_file.write_text("runtime", encoding="utf-8")

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(base_dir=program_dir)
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            for runtime_file in runtime_files:
                self.assertFalse(runtime_file.exists(), runtime_file)
            self.assertFalse(runtime_dir.exists())
            self.assertFalse((program_dir / UpdateState.STATE_FILE_NAME).exists())

    def test_cleanup_update_residue_removes_empty_runtime_subdirectories(self):
        """清理更新残留后应删除运行时目录内的空子目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            nested_dir = runtime_dir / "nested" / "empty"
            nested_dir.mkdir(parents=True, exist_ok=True)
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_file"] = str(paths["new_file"])
            state["backup_file"] = str(paths["backup_file"])
            state["helper_ps1"] = str(paths["helper_ps1"])
            state["update_ps1"] = str(paths["update_ps1"])
            state["lock_file"] = str(paths["lock_file"])
            state.save()
            runtime_files = (
                paths["new_file"],
                paths["backup_file"],
                paths["helper_ps1"],
                paths["update_ps1"],
                paths["lock_file"],
            )
            for runtime_file in runtime_files:
                runtime_file.write_text("runtime", encoding="utf-8")

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(base_dir=program_dir)
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertFalse(nested_dir.exists())
            self.assertFalse((runtime_dir / "nested").exists())
            self.assertFalse(runtime_dir.exists())
            self.assertFalse((program_dir / UpdateState.STATE_FILE_NAME).exists())

    def test_cleanup_update_residue_skips_runtime_dir_outside_temp_folder(self):
        """状态文件中的外部运行时目录不得被清理。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            external_dir = root / "external"
            external_dir.mkdir()
            external_file = external_dir / "App.new.exe"
            external_file.write_text("external", encoding="utf-8")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(external_dir)
            state["new_file"] = str(external_file)
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(external_dir.exists())
            self.assertTrue(external_file.exists())

    def test_cleanup_update_residue_skips_runtime_dir_equal_to_temp_folder(self):
        """状态文件中的运行时目录等于临时目录时不得清理。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            temp_folder = Path(updater.temp_folder)
            residue_file = temp_folder / "App.new.exe"
            residue_file.write_text("residue", encoding="utf-8")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(temp_folder)
            state["new_file"] = str(residue_file)
            state["backup_file"] = str(temp_folder / "App.backup.exe")
            state["helper_ps1"] = str(temp_folder / "App_Update_Helper.ps1")
            state["update_ps1"] = str(temp_folder / "App_Update.ps1")
            state["lock_file"] = str(temp_folder / "update_started.lock")
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(residue_file.exists())
            self.assertTrue(temp_folder.exists())

    def test_cleanup_update_residue_preserves_external_empty_subdirectories(self):
        """状态文件中的外部运行时目录及其空子目录不得被清理。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            external_dir = root / "external"
            external_dir.mkdir()
            external_file = external_dir / "App.new.exe"
            external_file.write_text("external", encoding="utf-8")
            external_empty_dir = external_dir / "nested" / "empty"
            external_empty_dir.mkdir(parents=True, exist_ok=True)
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(external_dir)
            state["new_file"] = str(external_file)
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(external_dir.exists())
            self.assertTrue(external_file.exists())
            self.assertTrue(external_empty_dir.exists())

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

    def test_generated_ps1_uses_injected_program_state_and_runtime_paths(self):
        """生成的 PS1 应使用注入的程序状态、日志和运行时路径。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, _, paths = self._make_runtime_paths(root, "self$`update")
            updater._generate_helper_ps1(paths)
            updater._generate_update_ps1(paths)

            helper_text = paths["helper_ps1"].read_text(encoding="utf-8-sig")
            update_text = paths["update_ps1"].read_text(encoding="utf-8-sig")
            expected_runtime_dir = self._ps1_expected_path(paths["runtime_dir"])
            expected_state_file = self._ps1_expected_path(paths["state_file"])
            expected_log_file = self._ps1_expected_path(paths["log_file"])
            expected_lock_file = self._ps1_expected_path(paths["lock_file"])
            expected_update_ps1 = self._ps1_expected_path(paths["update_ps1"])

            self.assertIn(f'$runtimeDir = "{expected_runtime_dir}"', helper_text)
            self.assertIn(f'$runtimeDir = "{expected_runtime_dir}"', update_text)
            self.assertIn('$scriptDir  = $runtimeDir', helper_text)
            self.assertIn('$scriptDir  = $runtimeDir', update_text)
            self.assertIn(f'$stateFile  = "{expected_state_file}"', helper_text)
            self.assertIn(f'$stateFile  = "{expected_state_file}"', update_text)
            self.assertIn(f'$logFile    = "{expected_log_file}"', helper_text)
            self.assertIn(f'$logFile    = "{expected_log_file}"', update_text)
            self.assertIn(f'$lockFile   = "{expected_lock_file}"', helper_text)
            self.assertIn(f'$updatePs1  = "{expected_update_ps1}"', helper_text)
            self.assertNotIn('$stateFile = Join-Path $scriptDir "update_state.ini"', helper_text)
            self.assertNotIn('$stateFile  = Join-Path $scriptDir "update_state.ini"', update_text)
            self.assertNotIn('$logFile   = Join-Path $scriptDir "update.log"', helper_text)
            self.assertNotIn('$logFile    = Join-Path $scriptDir "update.log"', update_text)

    def test_generated_ps1_writes_current_step(self):
        """生成的 PS1 状态字段应写 current_step 而不是 step。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater, _, paths = self._make_runtime_paths(Path(temp_dir))
            updater._generate_helper_ps1(paths)
            updater._generate_update_ps1(paths)

            helper_text = paths["helper_ps1"].read_text(encoding="utf-8-sig")
            update_text = paths["update_ps1"].read_text(encoding="utf-8-sig")

            self.assertIn('Write-IniValue "State" "current_step" $step', helper_text)
            self.assertIn('Write-IniValue "State" "current_step" $step', update_text)
            self.assertNotIn('Write-IniValue "State" "step" $step', helper_text)
            self.assertNotIn('Write-IniValue "State" "step" $step', update_text)

    def test_generated_ps1_has_sha256_fallbacks(self):
        """生成的 PS1 应包含 SHA256 多路径 fallback。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater, _, paths = self._make_runtime_paths(Path(temp_dir))
            updater._generate_helper_ps1(paths)
            updater._generate_update_ps1(paths)

            helper_text = paths["helper_ps1"].read_text(encoding="utf-8-sig")
            update_text = paths["update_ps1"].read_text(encoding="utf-8-sig")
            combined_text = helper_text + update_text

            self.assertEqual(2, combined_text.count("function Get-SHA256($filePath)"))
            self._assert_sha256_fallbacks(helper_text, "Helper.ps1")
            self._assert_sha256_fallbacks(update_text, "Update.ps1")
            self.assertIn("$actual = Get-SHA256 $target", helper_text)
            self.assertIn("$actual = Get-SHA256 $newFile", update_text)

    def test_generated_ps1_uses_expected_shared_and_helper_fragments(self):
        """生成的 Helper 与 Update 脚本应按职责拼接共享和专用函数。"""
        shared_functions = (
            "function Normalize-IniValue",
            "function Read-IniValue",
            "function Write-IniValue",
            "function Set-UpdateStatus",
            "function Move-WithRetry",
        )
        helper_only_functions = (
            "function Quote-Arg",
            "function Restore-Backup",
            "function Start-ProcWait",
            "function Start-NormalAppVisible",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            updater, _, paths = self._make_runtime_paths(Path(temp_dir))
            updater._generate_helper_ps1(paths)
            updater._generate_update_ps1(paths)

            helper_text = paths["helper_ps1"].read_text(encoding="utf-8-sig")
            update_text = paths["update_ps1"].read_text(encoding="utf-8-sig")

            for function_name in shared_functions:
                with self.subTest(script="Helper.ps1", function_name=function_name):
                    self.assertEqual(1, helper_text.count(function_name))
                with self.subTest(script="Update.ps1", function_name=function_name):
                    self.assertEqual(1, update_text.count(function_name))

            for function_name in helper_only_functions:
                with self.subTest(script="Helper.ps1", function_name=function_name):
                    self.assertIn(function_name, helper_text)
                with self.subTest(script="Update.ps1", function_name=function_name):
                    self.assertNotIn(function_name, update_text)

            self.assertLess(
                helper_text.index("function Write-Log"),
                helper_text.index("function Write-IniValue"),
            )
            self.assertLess(
                helper_text.index("function Write-IniValue"),
                helper_text.index("function Set-UpdateStatus"),
            )
            self.assertLess(
                update_text.index("function Write-Log"),
                update_text.index("function Write-IniValue"),
            )
            self.assertLess(
                update_text.index("function Write-IniValue"),
                update_text.index("function Set-UpdateStatus"),
            )

    def test_readme_documents_verify_version_func_requirement(self):
        """README 应说明未传 version_func 时只校验 SHA256。"""
        readme_path = Path(__file__).resolve().parents[1] / "self_updater" / "README.md"
        text = readme_path.read_text(encoding="utf-8")

        self.assertIn("未传 version_func 时仅校验 SHA256", text)
        self.assertNotIn("return  # 或 sys.exit(1)", text)

    def test_download_and_verify_still_checks_sha256_after_download(self):
        """下载成功后仍应由现有流程执行 SHA256 校验。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tmp_path = root / "App.exe"
            sha_path = root / "App.sha256"
            expected_sha256 = hashlib.sha256(b"expected").hexdigest()
            sha_path.write_text(expected_sha256, encoding="ascii")
            updater = self.make_updater(temp_folder=temp_dir)

            def fake_download(url, save_path):
                """写入错误内容，触发 SHA256 校验失败。"""
                Path(save_path).write_bytes(b"wrong")
                return True

            updater._download_func = fake_download

            result = updater._download_and_verify(
                tmp_path,
                sha_path,
                "https://example.invalid/App.exe",
                expected_sha256,
                "v1.2.0",
            )

            self.assertFalse(result)
            self.assertFalse(tmp_path.exists())
            self.assertFalse(sha_path.exists())

    def test_default_download_uses_download_manager(self):
        """默认下载应委托给独立 download.DownloadManager。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = SelfUpdater(
                github_repo="owner/repo",
                asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
                app_name="App",
                current_version="v1.0.0",
                proxy="http://127.0.0.1:7890",
                logger=logging.getLogger("SelfUpdaterTest"),
                temp_folder=temp_dir,
                is_bundled=True,
                package_type="Nuitka",
            )
            save_path = str(Path(temp_dir) / "App.exe")

            with patch("self_updater.self_updater.DownloadManager") as manager_class:
                manager = manager_class.return_value
                manager.download_file_with_progress.return_value = True

                result = updater._default_download("https://example.invalid/App.exe", save_path)

            self.assertTrue(result)
            manager_class.assert_called_once_with(
                proxy="http://127.0.0.1:7890",
                temp_folder=temp_dir,
                logger=updater.logger,
            )
            manager.download_file_with_progress.assert_called_once_with(
                "https://example.invalid/App.exe",
                save_path,
            )

    def test_custom_download_func_skips_download_manager(self):
        """传入 download_func 时不应创建默认 DownloadManager。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            custom_download = Mock(return_value=True)
            updater = SelfUpdater(
                github_repo="owner/repo",
                asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
                app_name="App",
                current_version="v1.0.0",
                proxy="",
                logger=logging.getLogger("SelfUpdaterTest"),
                temp_folder=temp_dir,
                download_func=custom_download,
                is_bundled=True,
                package_type="Nuitka",
            )
            save_path = str(Path(temp_dir) / "App.exe")

            with patch("self_updater.self_updater.DownloadManager") as manager_class:
                result = updater._download_func("https://example.invalid/App.exe", save_path)

            self.assertTrue(result)
            custom_download.assert_called_once_with("https://example.invalid/App.exe", save_path)
            manager_class.assert_not_called()

    def test_init_rejects_removed_download_backend_options_by_type_error(self):
        """旧下载后端参数已移除，应由构造函数自然抛出 TypeError。"""
        with self.assertRaises(TypeError):
            SelfUpdater(
                github_repo="owner/repo",
                asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
                app_name="App",
                current_version="v1.0.0",
                proxy="",
                logger=logging.getLogger("SelfUpdaterTest"),
                temp_folder="C:/Temp/App",
                is_bundled=True,
                package_type="Nuitka",
                download_backend="pypdl",
            )

    def test_self_updater_module_no_longer_contains_pypdl_or_legacy_download_helpers(self):
        """SelfUpdater 不应继续保留 PYPDL 或旧单线程默认下载实现。"""
        import self_updater.self_updater as self_updater_module

        source = inspect.getsource(self_updater_module)

        self.assertNotIn("pypdl", source.lower())
        self.assertNotIn("_download_with_pypdl", source)
        self.assertNotIn("_download_with_requests", source)
        self.assertNotIn("download_backend", source)
        self.assertNotIn("download_segments", source)
        self.assertNotIn("download_retries", source)
        self.assertNotIn("download_timeout", source)

    def test_path_is_reparse_point_rejects_symbolic_link(self):
        """符号链接应被识别为不可递归处理的路径。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            link = root / "link"
            target.mkdir()
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"当前环境无法创建符号链接: {error}")

            self.assertTrue(SelfUpdater._is_unsafe_path(link))
            self.assertFalse(SelfUpdater._is_unsafe_path(target))

    def test_remove_empty_directories_does_not_follow_junction(self):
        """空目录清理不应跟随 junction 删除外部目标内容。"""
        if not sys.platform.startswith("win"):
            self.skipTest("仅 Windows 支持 junction")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime_dir = root / "runtime"
            runtime_dir.mkdir()
            external_target = root / "external_target"
            (external_target / "empty_sub").mkdir(parents=True)
            junction = runtime_dir / "junction"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external_target)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"无法创建 junction: {result.stderr}")

            SelfUpdater._remove_empty_directories(
                runtime_dir,
                logging.getLogger("SelfUpdaterTest"),
            )

            self.assertTrue((external_target / "empty_sub").exists())
            self.assertTrue(junction.exists())

    def test_is_unsafe_directory_detects_symlink(self):
        """is_symlink 返回 True 的路径应被判为不安全。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dir"
            path.mkdir()
            with patch.object(Path, "is_symlink", return_value=True):
                self.assertTrue(SelfUpdater._is_unsafe_path(path))

    def test_is_unsafe_directory_detects_reparse_point_flag(self):
        """含 Windows reparse point 标志的目录应被判为不安全。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dir"
            path.mkdir()
            fake_stat = Mock(st_file_attributes=SelfUpdater._FILE_ATTRIBUTE_REPARSE_POINT)
            with patch.object(Path, "is_symlink", return_value=False), \
                    patch.object(Path, "lstat", return_value=fake_stat):
                self.assertTrue(SelfUpdater._is_unsafe_path(path))

    def test_is_unsafe_directory_returns_true_when_lstat_fails(self):
        """lstat 抛 OSError 时应保守判为不安全。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dir"
            path.mkdir()
            with patch.object(Path, "lstat", side_effect=OSError("lstat failed")):
                self.assertTrue(SelfUpdater._is_unsafe_path(path))

    def test_is_unsafe_directory_accepts_plain_directory(self):
        """普通目录（非符号链接、无 reparse point 位）应判为安全。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dir"
            path.mkdir()
            with patch.object(Path, "is_symlink", return_value=False):
                self.assertFalse(SelfUpdater._is_unsafe_path(path))

    def test_clean_update_cache_removes_only_marked_cache(self):
        """仅带有效标记的 UpdateCache 应被清理。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_dir = root / "UpdateCache"
            version_dir = root / "v1.2.0"
            cache_dir.mkdir()
            version_dir.mkdir()
            (cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            (cache_dir / "installs" / "v1.2.0").mkdir(parents=True)
            (cache_dir / "installs" / "v1.2.0" / "App.exe").write_bytes(b"cache")

            SelfUpdater.clean_update_cache(str(root), logging.getLogger("SelfUpdaterTest"))

            self.assertFalse(cache_dir.exists())
            self.assertTrue(version_dir.exists())
            self.assertTrue(root.exists())

    def test_clean_update_cache_skips_unmarked_cache(self):
        """无有效标记的 UpdateCache 不应被清理。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "UpdateCache"
            cache_dir.mkdir()
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"cache")

            with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                SelfUpdater.clean_update_cache(temp_dir, logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(cached_file.exists())

    def test_clean_update_cache_skips_invalid_marker(self):
        """标记内容无效的 UpdateCache 不应被清理。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "UpdateCache"
            cache_dir.mkdir()
            (cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                "invalid-marker-content",
                encoding="ascii",
            )
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"cache")

            with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                SelfUpdater.clean_update_cache(temp_dir, logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(cached_file.exists())

    def test_clean_update_cache_preserves_link_and_target(self):
        """缓存中的链接及其目标不应被删除。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_dir = root / "UpdateCache"
            external_dir = root / "external"
            cache_dir.mkdir()
            external_dir.mkdir()
            (external_dir / "keep.txt").write_text("keep", encoding="utf-8")
            (cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            (cache_dir / "delete.txt").write_text("delete", encoding="utf-8")
            link = cache_dir / "external-link"
            try:
                link.symlink_to(external_dir, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"当前环境无法创建符号链接: {error}")

            SelfUpdater.clean_update_cache(str(root), logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(link.exists())
            self.assertTrue((external_dir / "keep.txt").exists())
            self.assertFalse((cache_dir / "delete.txt").exists())
            self.assertTrue(cache_dir.exists())

    def test_clean_update_cache_preserves_symlink_child_via_mock(self):
        """is_symlink 返回 True 的缓存子项应被保留（mock 验证链接分支）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_dir = root / "UpdateCache"
            cache_dir.mkdir()
            (cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            fake_link = cache_dir / "fake-link"
            fake_link.write_bytes(b"payload")
            (cache_dir / "delete.txt").write_text("delete", encoding="utf-8")

            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self):
                """仅对 fake-link 路径返回 True。"""
                return str(self) == str(fake_link) or real_is_symlink(self)

            with patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                SelfUpdater.clean_update_cache(
                    str(root),
                    logging.getLogger("SelfUpdaterTest"),
                )

            self.assertTrue(fake_link.exists())
            self.assertFalse((cache_dir / "delete.txt").exists())
            self.assertTrue(cache_dir.exists())

    def test_create_update_cache_writes_marker_before_download(self):
        """新建下载缓存时标记应写入 UpdateCache 根目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"

            marker_path = updater._create_update_cache_marker(cache_dir)

            self.assertEqual(
                root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE,
                marker_path,
            )
            self.assertEqual(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                marker_path.read_text(encoding="ascii"),
            )

    def test_clean_update_cache_recognizes_created_marker(self):
        """创建标记后 clean_update_cache 应能识别并清理该缓存。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"cache")

            updater._create_update_cache_marker(cache_dir)

            SelfUpdater.clean_update_cache(
                str(root),
                logging.getLogger("SelfUpdaterTest"),
            )

            self.assertFalse(cache_dir.exists())
            self.assertFalse((root / "UpdateCache").exists())

    def test_create_update_cache_marker_rejects_dangling_link_via_mock(self):
        """标记路径为悬空符号链接时应拒绝写入，不创建外部目标。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            marker_path = root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            dangling_target = root / "dangling-target"

            real_lstat = Path.lstat
            real_is_symlink = Path.is_symlink

            def fake_lstat(self):
                """标记节点存在（悬空链接），但其目标不存在。"""
                if str(self) == str(marker_path):
                    return cache_dir.stat()
                return real_lstat(self)

            def fake_is_symlink(self):
                """仅将标记路径视为符号链接。"""
                return str(self) == str(marker_path) or real_is_symlink(self)

            with patch.object(
                    Path,
                    "lstat",
                    autospec=True,
                    side_effect=fake_lstat,
            ), patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                with self.assertRaises(OSError):
                    updater._create_update_cache_marker(cache_dir)

            self.assertFalse(marker_path.exists())
            self.assertFalse(dangling_target.exists())

    def test_create_update_cache_marker_is_idempotent(self):
        """重复创建缓存标记不应覆盖已有标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = Path(temp_dir) / "UpdateCache" / "installs" / "v1.2.0"
            marker_path = updater._create_update_cache_marker(cache_dir)
            marker_path.write_text("tampered", encoding="ascii")

            updater._create_update_cache_marker(cache_dir)

            self.assertEqual("tampered", marker_path.read_text(encoding="ascii"))

    def test_clean_update_cache_preserves_junction_and_target(self):
        """缓存内的 junction 与外部目标应保留，普通缓存文件仍清理。"""
        if not sys.platform.startswith("win"):
            self.skipTest("仅 Windows 支持 junction")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_dir = root / "UpdateCache"
            external_dir = root / "external"
            cache_dir.mkdir()
            external_dir.mkdir()
            (external_dir / "keep.txt").write_text("keep", encoding="utf-8")
            (cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            (cache_dir / "delete.txt").write_text("delete", encoding="utf-8")
            junction = cache_dir / "external-junction"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external_dir)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"无法创建 junction: {result.stderr}")

            SelfUpdater.clean_update_cache(str(root), logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(junction.exists())
            self.assertTrue((external_dir / "keep.txt").exists())
            self.assertFalse((cache_dir / "delete.txt").exists())
            self.assertTrue(cache_dir.exists())

    def test_cleanup_update_residue_skips_symlink_residue_file(self):
        """残留文件为符号链接时应保留链接节点与目标文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            target_file = runtime_dir / "App.new.exe"
            target_file.write_text("target", encoding="utf-8")
            link_file = runtime_dir / "App.link.exe"
            try:
                link_file.symlink_to(target_file)
            except OSError as error:
                self.skipTest(f"当前环境无法创建符号链接: {error}")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_file"] = str(link_file)
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                updater._cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(target_file.exists())
            self.assertTrue(link_file.exists())
            self.assertFalse((program_dir / UpdateState.STATE_FILE_NAME).exists())

    def test_is_unsafe_path_accepts_plain_file(self):
        """普通文件应判为安全。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "file.txt"
            file_path.write_text("data", encoding="utf-8")

            self.assertFalse(SelfUpdater._is_unsafe_path(file_path))

    def test_is_unsafe_path_rejects_missing_path(self):
        """不存在的路径应保守判为不安全。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_path = Path(temp_dir) / "missing"

            self.assertTrue(SelfUpdater._is_unsafe_path(missing_path))

    def test_create_update_cache_marker_rejects_link_marker(self):
        """缓存标记路径为链接或重解析点时不应写入。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = Path(temp_dir) / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            marker_path = Path(temp_dir) / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            external_target = Path(temp_dir) / "external-target"
            external_target.write_text("outside", encoding="utf-8")
            try:
                marker_path.symlink_to(external_target)
            except OSError as error:
                self.skipTest(f"当前环境无法创建符号链接: {error}")

            with self.assertRaises(OSError):
                updater._create_update_cache_marker(cache_dir)

            self.assertEqual("outside", external_target.read_text(encoding="utf-8"))

    def test_clean_update_cache_skips_link_marker(self):
        """缓存标记为链接或重解析点时不应清理缓存。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_dir = root / "UpdateCache"
            cache_dir.mkdir()
            external_target = root / "external-target"
            external_target.write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"cache")
            marker_path = cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            try:
                marker_path.symlink_to(external_target)
            except OSError as error:
                self.skipTest(f"当前环境无法创建符号链接: {error}")

            with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                SelfUpdater.clean_update_cache(
                    str(root),
                    logging.getLogger("SelfUpdaterTest"),
                )

            self.assertTrue(cached_file.exists())
            self.assertEqual(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                external_target.read_text(encoding="ascii"),
            )

    def test_clean_update_cache_skips_link_marker_via_mock(self):
        """is_symlink 返回 True 的缓存标记应跳过清理（mock 验证链接分支）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_dir = root / "UpdateCache"
            cache_dir.mkdir()
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"cache")
            marker_path = cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            marker_path.write_text("tampered", encoding="ascii")

            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self):
                """仅对标记路径返回 True。"""
                return str(self) == str(marker_path) or real_is_symlink(self)

            with patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                    SelfUpdater.clean_update_cache(
                        str(root),
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertTrue(cached_file.exists())

    def test_create_update_cache_marker_rejects_link_marker_via_mock(self):
        """is_symlink 返回 True 的标记路径应拒绝写入（mock 验证链接分支）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            marker_path = root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            marker_path.write_text("tampered", encoding="ascii")

            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self):
                """仅对标记路径返回 True。"""
                return str(self) == str(marker_path) or real_is_symlink(self)

            with patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                with self.assertRaises(OSError):
                    updater._create_update_cache_marker(cache_dir)

            self.assertEqual("tampered", marker_path.read_text(encoding="ascii"))


if __name__ == "__main__":
    unittest.main()
