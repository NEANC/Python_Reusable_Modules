#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""self_updater 模块的回归测试。"""

import configparser
import hashlib
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
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

    def make_updater(self, current_version="v1.0.0", app_name="App", temp_folder=None, **kwargs):
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
            **kwargs,
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

        self.assertIn("function ConvertTo-WindowsCommandLineArg", args)
        self.assertIn("function Get-RetryOrDefault", retry)
        self.assertIn("function Remove-WithRetry", cleanup)
        self.assertIn("function Commit-Update", lifecycle)
        self.assertIn("function Restore-Backup", lifecycle)
        self.assertIn("function Start-ProcWait", lifecycle)
        self.assertIn("function Start-NormalAppVisible", lifecycle)

    def test_helper_lifecycle_uses_process_start_info_and_verified_commit(self):
        """生命周期片段应使用 ProcessStartInfo 启动并在提交前保持待验证状态。"""
        state = ps1_fragments.generate_common_state_functions_ps1()
        lifecycle = ps1_fragments.generate_helper_lifecycle_functions_ps1()

        self.assertIn('throw "Write-IniValue failed:', state)
        self.assertIn('Read-IniValue "State" "state"', lifecycle)
        self.assertIn('Read-IniValue "Retry" "retry_count"', lifecycle)
        self.assertIn("System.Diagnostics.ProcessStartInfo", lifecycle)
        self.assertNotIn("Start-Process @startArgs", lifecycle)
        self.assertGreaterEqual(lifecycle.count("ConvertTo-WindowsCommandLineArg"), 2)

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

    def test_self_updater_validates_and_copies_launch_configuration(self):
        """构造应校验启动协议参数并复制白名单，不保留调用方引用。"""
        whitelist = {"--config": "value", "--portable": "flag"}
        updater = self.make_updater(
            post_update_action="exit",
            passthrough_args_whitelist=whitelist,
        )
        whitelist["--later"] = "flag"

        self.assertEqual("exit", updater.post_update_action)
        self.assertNotIn("--later", updater.passthrough_args_whitelist)

    def test_self_updater_rejects_invalid_launch_configuration(self):
        """构造应拒绝非法的启动参数白名单。"""
        invalid_whitelists = (
            {"config": "value"},
            {"--bad=name": "value"},
            {"--bad name": "flag"},
            {"--bad\nname": "flag"},
            {"--config": "unknown"},
            {"": "value"},
            {"--": "flag"},
            ("--config", "value"),
            ["--config", "value"],
        )
        for whitelist in invalid_whitelists:
            with self.subTest(whitelist=whitelist):
                with self.assertRaises(ValueError):
                    self.make_updater(passthrough_args_whitelist=whitelist)

    def test_self_updater_rejects_invalid_post_update_action(self):
        """构造应拒绝未知的 post_update_action 值。"""
        for action in ("restart", "START", "", None):
            with self.subTest(post_update_action=action):
                with self.assertRaises(ValueError):
                    self.make_updater(post_update_action=action)

    def test_self_updater_accepts_valid_post_update_action(self):
        """构造应接受合法的 post_update_action 值 start 与 exit。"""
        start_updater = self.make_updater(post_update_action="start")
        self.assertEqual("start", start_updater.post_update_action)

        exit_updater = self.make_updater(post_update_action="exit")
        self.assertEqual("exit", exit_updater.post_update_action)

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

    def test_update_state_preserves_launch_args_without_interpolation(self):
        """UpdateState 不应插值 LaunchArgs 的 % 占位符，且不补写 schema_version。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateState(base_dir=temp_dir)
            state.set("LaunchArgs", "passthrough_args_json", '["%APPDATA%","值"]')
            state.save()

            loaded = UpdateState.load(base_dir=temp_dir)

            self.assertEqual(
                '["%APPDATA%","值"]',
                loaded.get("LaunchArgs", "passthrough_args_json"),
            )
            self.assertEqual("", loaded.get("Protocol", "schema_version", fallback=""))

    def test_update_state_defaults_include_launch_args_and_protocol(self):
        """UpdateState 默认应含 LaunchArgs 键，Protocol 节无 schema_version 键。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateState(base_dir=temp_dir)

            self.assertEqual("start", state.get("LaunchArgs", "post_update_action"))
            self.assertEqual("[]", state.get("LaunchArgs", "passthrough_args_json"))
            self.assertEqual("", state.get("Protocol", "schema_version", fallback=""))

            state.save()
            loaded = UpdateState.load(base_dir=temp_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual("start", loaded.get("LaunchArgs", "post_update_action"))
            self.assertEqual("[]", loaded.get("LaunchArgs", "passthrough_args_json"))
            self.assertEqual("", loaded.get("Protocol", "schema_version", fallback=""))

    def test_update_state_schema_version_not_reintroduced_on_save(self):
        """旧协议状态文件保存后不应被补写 schema_version 默认值。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateState(base_dir=temp_dir)
            state["state"] = "verified"
            state.save()
            state_file = Path(temp_dir) / "update_state.ini"

            # 用 configparser 重写状态文件，去掉 [Protocol] 节，模拟旧协议文件
            parser = configparser.ConfigParser()
            parser.read(state_file, encoding="utf-8")
            parser.remove_section("Protocol")
            with state_file.open("w", encoding="utf-8") as f:
                parser.write(f)

            loaded = UpdateState.load(base_dir=temp_dir)
            self.assertIsNotNone(loaded)
            loaded.save()
            reloaded = UpdateState.load(base_dir=temp_dir)

            self.assertIsNotNone(reloaded)
            self.assertEqual("", reloaded.get("Protocol", "schema_version", fallback=""))

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
                updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            for runtime_file in runtime_files:
                self.assertFalse(runtime_file.exists(), runtime_file)
            self.assertTrue(runtime_dir.exists())
            self.assertTrue(unknown_runtime_file.exists())
            self.assertTrue(update_log.exists())
            self.assertTrue(old_residue.exists())
            self.assertTrue(unrecorded_update_ps1.exists())
            self.assertTrue(unrecorded_backup_exe.exists())
            self.assertTrue((program_dir / UpdateState.STATE_FILE_NAME).exists())

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
                result = updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertFalse(result)
            self.assertTrue(important_file.exists())
            self.assertFalse(runtime_file.exists())
            self.assertTrue((program_dir / UpdateState.STATE_FILE_NAME).exists())

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
                result = updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(result)
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
                result = updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(result)
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
                result = updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertFalse(result)
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
                result = updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertFalse(result)
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
                result = updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertFalse(result)
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

    def _prepare_launch_snapshot_state(self, program_dir: Path) -> None:
        """构造含协议与启动参数快照的回滚后状态文件。"""
        state = UpdateState(base_dir=program_dir)
        state["state"] = "rollback_done"
        state["target"] = str(program_dir / "App.exe")
        state["new_version"] = "v1.2.0"
        state.set("Protocol", "schema_version", "2")
        state.set("LaunchArgs", "post_update_action", "exit")
        state.set("LaunchArgs", "passthrough_args_json", '["--config","original.ini"]')
        state.save()

    def _run_update_prepare(self, updater, current_exe: Path, argv, retry=False):
        """在给定 argv 下执行一次完整的更新准备流程。"""
        with patch("self_updater.self_updater.sys.argv", argv), \
                patch("self_updater.self_updater.get_exe_path", return_value=current_exe), \
                patch("self_updater.self_updater.subprocess.Popen",
                      return_value=FakeExitedProcess()), \
                patch.object(updater, "_check_system_environment", return_value=True), \
                patch.object(updater, "_fetch_latest_release",
                             return_value={"tag_name": "v1.2.0"}), \
                patch.object(updater, "_match_asset",
                             return_value=("https://example.invalid/App.exe", "App.exe")), \
                patch.object(updater, "_get_asset_sha256", return_value="a" * 64), \
                patch.object(updater, "_fetch_current_release_sha256", return_value=""), \
                patch("self_updater.self_updater.calculate_sha256", return_value="a" * 64):
            updater._download_func = (
                lambda url, save_path: Path(save_path).write_bytes(b"new") or True
            )
            updater.check_self_update(retry=retry)

    def test_collect_passthrough_args_applies_whitelist_protocol(self):
        """按白名单采集参数：分离值、等号值、内部参数、缺值、终止符与重复项。"""
        updater = self.make_updater(
            passthrough_args_whitelist={
                "--config": "value",
                "--profile": "value",
                "--portable": "flag",
            },
        )
        argv = [
            "--config", "app.ini", "--unknown", "ignored",
            "--portable", "--self-update-verify", "--profile=work",
            "--config", "--update", "--profile=", "--", "--portable",
        ]

        result = updater._collect_passthrough_args(argv)

        self.assertEqual(
            ["--config", "app.ini", "--portable", "--profile=work", "--profile="],
            result,
        )

    def test_collect_passthrough_args_rejects_control_characters(self):
        """透传参数值含控制字符时不应采集，且应记录 warning。"""
        updater = self.make_updater(
            passthrough_args_whitelist={"--config": "value"},
        )

        with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
            result = updater._collect_passthrough_args(
                ["--config", "a\nb", "--config=a\rb", "--config=a\0b"],
            )

        self.assertEqual([], result)
        self.assertEqual(3, len(captured.output))
        for message in captured.output:
            self.assertIn("参数值含控制字符", message)

    def test_collect_passthrough_args_consumes_internal_value_separated_form(self):
        """内部 value 参数的分离形式应消费下一 token 但不记录。"""
        updater = self.make_updater(passthrough_args_whitelist={"--config": "value"})

        result = updater._collect_passthrough_args(
            ["--expected-version", "v1.2.0", "--config", "app.ini"],
        )

        self.assertEqual(["--config", "app.ini"], result)

    def test_collect_passthrough_args_skips_internal_value_equal_form(self):
        """内部 value 参数的等号形式应只跳过自身。"""
        updater = self.make_updater(passthrough_args_whitelist={"--config": "value"})

        result = updater._collect_passthrough_args(
            ["--expected-version=v1.2.0", "--config", "app.ini"],
        )

        self.assertEqual(["--config", "app.ini"], result)

    def test_collect_passthrough_args_internal_flag_allows_following_whitelist(self):
        """内部 flag 只跳自身，其后白名单参数仍应被识别收集。"""
        updater = self.make_updater(
            passthrough_args_whitelist={"--config": "value", "--portable": "flag"},
        )

        result = updater._collect_passthrough_args(
            ["--self-update-verify", "--portable", "--config", "app.ini"],
        )

        self.assertEqual(["--portable", "--config", "app.ini"], result)

    def test_collect_passthrough_args_preserves_duplicates_order(self):
        """重复的白名单参数应全部保留并保持相对顺序。"""
        updater = self.make_updater(passthrough_args_whitelist={"--config": "value"})

        result = updater._collect_passthrough_args(
            ["--config", "a.ini", "--config", "b.ini", "--config", "c.ini"],
        )

        self.assertEqual(
            ["--config", "a.ini", "--config", "b.ini", "--config", "c.ini"],
            result,
        )

    def test_collect_passthrough_args_keeps_dash_prefixed_equal_value(self):
        """等号形式的值以 -- 开头时保留原始 token。"""
        updater = self.make_updater(passthrough_args_whitelist={"--name": "value"})

        result = updater._collect_passthrough_args(["--name=--literal"])

        self.assertEqual(["--name=--literal"], result)

    def test_collect_passthrough_args_ignores_flag_equal_form(self):
        """白名单 flag 出现 =value 形式时不应收集。"""
        updater = self.make_updater(passthrough_args_whitelist={"--portable": "flag"})

        result = updater._collect_passthrough_args(["--portable=on"])

        self.assertEqual([], result)

    def test_collect_passthrough_args_skips_missing_value_before_flag(self):
        """value 参数下一 token 以 -- 开头时应跳过并记录 warning。"""
        updater = self.make_updater(
            passthrough_args_whitelist={"--config": "value", "--portable": "flag"},
        )

        with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
            result = updater._collect_passthrough_args(["--config", "--portable"])

        self.assertEqual(["--portable"], result)
        self.assertIn("参数缺少值", "\n".join(captured.output))

    def test_collect_passthrough_args_skips_missing_value_at_end(self):
        """value 参数在 argv 末尾缺值时应跳过并记录 warning。"""
        updater = self.make_updater(passthrough_args_whitelist={"--config": "value"})

        with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
            result = updater._collect_passthrough_args(["--config"])

        self.assertEqual([], result)
        self.assertIn("参数缺少值", "\n".join(captured.output))

    def test_collect_passthrough_args_without_whitelist_collects_nothing(self):
        """未配置白名单时采集结果应为空。"""
        updater = self.make_updater()

        result = updater._collect_passthrough_args(["--config", "app.ini"])

        self.assertEqual([], result)

    def test_retry_reuses_original_launch_snapshot(self):
        """retry=True 且已有同版本有效状态时应沿用原启动参数快照。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program_dir = root / "program"
            program_dir.mkdir()
            current_exe = program_dir / "App.exe"
            current_exe.write_bytes(b"old")
            temp_folder = root / "self-update"
            updater = self.make_updater(
                temp_folder=str(temp_folder),
                post_update_action="exit",
                passthrough_args_whitelist={"--config": "value"},
            )
            self._prepare_launch_snapshot_state(program_dir)

            self._run_update_prepare(
                updater,
                current_exe,
                [str(current_exe), "--retry-update"],
                retry=True,
            )

            loaded = UpdateState.load(base_dir=program_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual("2", loaded.get("Protocol", "schema_version"))
            self.assertEqual("exit", loaded.get("LaunchArgs", "post_update_action"))
            self.assertEqual(
                '["--config","original.ini"]',
                loaded.get("LaunchArgs", "passthrough_args_json"),
            )

    def test_normal_update_recollects_same_version_launch_snapshot(self):
        """普通 check_self_update 即使已有同版本状态也应重新采集当前 argv。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program_dir = root / "program"
            program_dir.mkdir()
            current_exe = program_dir / "App.exe"
            current_exe.write_bytes(b"old")
            temp_folder = root / "self-update"
            updater = self.make_updater(
                temp_folder=str(temp_folder),
                post_update_action="start",
                passthrough_args_whitelist={"--config": "value"},
            )
            self._prepare_launch_snapshot_state(program_dir)

            self._run_update_prepare(
                updater,
                current_exe,
                [str(current_exe), "--config", "new.ini"],
            )

            loaded = UpdateState.load(base_dir=program_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual("2", loaded.get("Protocol", "schema_version"))
            self.assertEqual("start", loaded.get("LaunchArgs", "post_update_action"))
            self.assertEqual(
                '["--config","new.ini"]',
                loaded.get("LaunchArgs", "passthrough_args_json"),
            )

    def test_retry_chain_preserves_original_launch_snapshot(self):
        """首次写快照后模拟回滚重试，retry=True 时原快照保持不变。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program_dir = root / "program"
            program_dir.mkdir()
            current_exe = program_dir / "App.exe"
            current_exe.write_bytes(b"old")
            temp_folder = root / "self-update"
            updater = self.make_updater(
                temp_folder=str(temp_folder),
                post_update_action="exit",
                passthrough_args_whitelist={"--config": "value"},
            )

            # 首次普通更新：采集 --config original.ini 写入快照
            self._run_update_prepare(
                updater,
                current_exe,
                [str(current_exe), "--config", "original.ini"],
            )

            # 模拟 helper 回滚完成并增加重试计数
            state = UpdateState.load(base_dir=program_dir)
            self.assertIsNotNone(state)
            state.set("State", "state", "rollback_done")
            state.set("Retry", "retry_count", "1")
            state.save()

            # 重试：当前 argv 仅含 --retry-update，应沿用原快照
            self._run_update_prepare(
                updater,
                current_exe,
                [str(current_exe), "--retry-update"],
                retry=True,
            )

            loaded = UpdateState.load(base_dir=program_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual("2", loaded.get("Protocol", "schema_version"))
            self.assertEqual("exit", loaded.get("LaunchArgs", "post_update_action"))
            self.assertEqual(
                '["--config","original.ini"]',
                loaded.get("LaunchArgs", "passthrough_args_json"),
            )

    def test_resolve_launch_snapshot_recovers_from_corrupt_state(self):
        """retry=True 且状态文件非法 UTF-8 时应视为损坏并重新采集，而非抛异常。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program_dir = root / "program"
            program_dir.mkdir()
            current_exe = program_dir / "App.exe"
            current_exe.write_bytes(b"old")
            temp_folder = root / "self-update"
            updater = self.make_updater(
                temp_folder=str(temp_folder),
                post_update_action="exit",
                passthrough_args_whitelist={"--config": "value"},
            )
            paths = updater._build_update_runtime_paths(current_exe, "v1.2.0")
            paths["state_file"].write_bytes(b"\xff\xfe\x00garbage")

            with patch(
                "self_updater.self_updater.sys.argv",
                [str(current_exe), "--retry-update", "--config", "new.ini"],
            ):
                with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                    args, action = updater._resolve_launch_args_snapshot(
                        paths, "v1.2.0", retry=True,
                    )

            self.assertEqual(["--config", "new.ini"], args)
            self.assertEqual("exit", action)
            self.assertIn("读取状态文件失败", "\n".join(captured.output))

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
            "function ConvertTo-WindowsCommandLineArg",
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

    def test_helper_contains_start_and_exit_post_update_branches(self):
        """生成的 Helper 应包含 start/exit 更新后动作分支与 cleanup 启动器调用。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater, _, paths = self._make_runtime_paths(Path(temp_dir))
            updater._generate_helper_ps1(paths)
            content = paths["helper_ps1"].read_text(encoding="utf-8-sig")

            verify_index = content.index("$verifyCode =")
            commit_index = content.index("Commit-Update", verify_index)
            self.assertLess(verify_index, commit_index)
            self.assertIn("Start-NormalAppVisible $target $passthroughArgs", content)
            self.assertIn("--self-update-cleanup-parent-pid", content)
            self.assertIn("$PID", content)
            self.assertNotIn("Start-ProcWait $target @('--self-update-cleanup'", content)
            self.assertIn('Write-Log "WARN" "cleanup start failed:', content)

    def test_helper_launch_protocol_contracts(self):
        """Helper 应支持旧协议回退、拒绝未知协议版本且提交前不标记已验证。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater, _, paths = self._make_runtime_paths(Path(temp_dir))
            updater._generate_helper_ps1(paths)
            content = paths["helper_ps1"].read_text(encoding="utf-8-sig")

            self.assertIn("if ($schemaVersion -eq '') {", content)
            self.assertIn("$postUpdateAction = 'start'", content)
            self.assertIn("$passthroughArgs = @()", content)
            self.assertIn('Write-Log "ERROR" "unsupported schema_version:', content)
            self.assertIn("exit 4", content)

            verify_index = content.index("$verifyCode =")
            commit_index = content.index("Commit-Update", verify_index)
            self.assertNotIn('Set-UpdateStatus "verified"', content[:commit_index])

    def test_helper_launch_args_parse_contracts(self):
        """Get-PassthroughArgs 应对损坏/非数组/混合类型 JSON 置空参数并记录 warning。"""
        launch_args = ps1_fragments.generate_helper_launch_args_functions_ps1()

        self.assertIn("function Get-LaunchProtocolVersion", launch_args)
        self.assertIn("function Read-LaunchAction", launch_args)
        self.assertIn("function Get-PassthroughArgs", launch_args)
        self.assertIn('Read-IniValue "LaunchArgs" "passthrough_args_json"', launch_args)
        self.assertIn('Write-Log "WARN" "passthrough_args_json root is not an array', launch_args)
        self.assertIn('Write-Log "WARN" "passthrough_args_json parse failed:', launch_args)
        self.assertIn('Write-Log "WARN" "passthrough_args_json contains non-string item', launch_args)
        self.assertIn("^\\[\\s*\\]$", launch_args)
        self.assertIn("return ,@()", launch_args)

    def test_readme_documents_verify_version_func_requirement(self):
        """README 应说明未传 version_func 时只校验 SHA256。"""
        readme_path = Path(__file__).resolve().parents[1] / "self_updater" / "README.md"
        text = readme_path.read_text(encoding="utf-8")

        self.assertIn("未传 version_func 时仅校验 SHA256", text)
        self.assertNotIn("return  # 或 sys.exit(1)", text)

    def test_readme_documents_post_update_cleanup_protocol(self):
        """README 应完整记录清理协议、透传白名单与新入口顺序。"""
        readme_path = Path(__file__).resolve().parents[1] / "self_updater" / "README.md"
        content = readme_path.read_text(encoding="utf-8")

        self.assertIn("post_update_action", content)
        self.assertIn("passthrough_args_whitelist", content)
        self.assertIn("--self-update-cleanup-parent-pid", content)
        self.assertIn("def create_updater", content)
        self.assertIn("cleanup_update_residue", content)
        self.assertNotIn("updater._cleanup_update_residue", content)
        self.assertNotIn('"--Update"', content)
        self.assertIn("self_update_cleanup_parent_pid < 0", content)
        self.assertIn("不进入正常业务主循环", content)
        self.assertIn("不触发更新重试", content)

        # 要求 3：启动入口按固定顺序（verify → retry → failed → cleanup → update）
        entry_line = next(
            line for line in content.splitlines()
            if "启动入口按固定顺序检查" in line
        )
        verify_pos = entry_line.index("`--self-update-verify`")
        retry_pos = entry_line.index("`--retry-update`")
        failed_pos = entry_line.index("`--update-failed`")
        cleanup_pos = entry_line.index("`--self-update-cleanup`")
        update_pos = entry_line.index("`--update`")
        self.assertLess(verify_pos, retry_pos)
        self.assertLess(retry_pos, failed_pos)
        self.assertLess(failed_pos, cleanup_pos)
        self.assertLess(cleanup_pos, update_pos)

        # 要求 4：post_update_action="exit" 流程说明
        self.assertIn('post_update_action="exit"', content)

        # 要求 6：透传只覆盖 argv，不覆盖 cwd 与 env
        self.assertIn("不覆盖 cwd 与 env", content)

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
            installs_dir = cache_dir / "installs"
            installs_dir.mkdir()
            (installs_dir / "delete.txt").write_text("delete", encoding="utf-8")
            link = installs_dir / "external-link"
            try:
                link.symlink_to(external_dir, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"当前环境无法创建符号链接: {error}")

            SelfUpdater.clean_update_cache(str(root), logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(link.exists())
            self.assertTrue((external_dir / "keep.txt").exists())
            self.assertFalse((installs_dir / "delete.txt").exists())
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
            installs_dir = cache_dir / "installs"
            installs_dir.mkdir()
            fake_link = installs_dir / "fake-link"
            fake_link.write_bytes(b"payload")
            (installs_dir / "delete.txt").write_text("delete", encoding="utf-8")

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
            self.assertFalse((installs_dir / "delete.txt").exists())
            self.assertTrue(cache_dir.exists())

    def test_create_update_cache_writes_marker_before_download(self):
        """新建下载缓存时标记应写入 UpdateCache 根目录。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"

            marker_path = updater._create_update_cache_marker(cache_dir, temp_dir)

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
            marker_path = updater._create_update_cache_marker(cache_dir, temp_dir)
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"cache")

            self.assertTrue(marker_path.exists())

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
                    updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertFalse(marker_path.exists())
            self.assertFalse(dangling_target.exists())

    def test_create_update_cache_marker_accepts_existing_valid_marker(self):
        """重复检查有效缓存标记不应改写其内容。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = Path(temp_dir) / "UpdateCache" / "installs" / "v1.2.0"
            marker_path = updater._create_update_cache_marker(cache_dir, temp_dir)

            updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertEqual(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                marker_path.read_text(encoding="ascii"),
            )

    def test_create_update_cache_marker_rejects_historical_cache_without_marker(self):
        """历史缓存没有标记时不得补写标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"historical-cache")
            marker_path = root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE

            with self.assertRaises(OSError):
                updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertTrue(cached_file.exists())
            self.assertFalse(marker_path.exists())

    def test_create_update_cache_marker_rejects_invalid_existing_marker(self):
        """已有无效缓存标记时不得下载或改写标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            marker_path = root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            marker_path.write_text("invalid", encoding="ascii")

            with self.assertRaises(OSError):
                updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertEqual("invalid", marker_path.read_text(encoding="ascii"))

    def test_create_update_cache_marker_rejects_existing_unmarked_root_before_mkdir(self):
        """既有无标记根目录应在创建 installs 前被拒绝且目录树不变。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_root = root / "UpdateCache"
            cache_root.mkdir()
            before_paths = sorted(str(path.relative_to(root)) for path in root.rglob("*"))

            with self.assertRaises(OSError):
                updater._create_update_cache_marker(
                    cache_root / "installs" / "v1.2.0",
                    temp_dir,
                )

            after_paths = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            self.assertEqual(before_paths, after_paths)

    def test_create_update_cache_marker_rejects_invalid_root_before_mkdir(self):
        """既有无效标记根目录应在创建 installs 前被拒绝且目录树不变。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_root = root / "UpdateCache"
            cache_root.mkdir()
            (cache_root / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                "invalid",
                encoding="ascii",
            )
            before_paths = sorted(str(path.relative_to(root)) for path in root.rglob("*"))

            with self.assertRaises(OSError):
                updater._create_update_cache_marker(
                    cache_root / "installs" / "v1.2.0",
                    temp_dir,
                )

            after_paths = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
            self.assertEqual(before_paths, after_paths)

    def test_create_update_cache_marker_flushes_and_fsyncs_before_atomic_publish(self):
        """新根标记应在刷盘后以同目录原子不覆盖发布。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"

            with patch("self_updater.self_updater.os.fsync") as fsync:
                marker_path = updater._create_update_cache_marker(cache_dir, temp_dir)

            fsync.assert_called_once()
            self.assertEqual(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                marker_path.read_text(encoding="ascii"),
            )
            self.assertEqual(
                [],
                list((root / "UpdateCache").glob(".self_updater_cache.tmp-*")),
            )

    def test_clean_update_cache_preserves_interrupted_marker_staging_file(self):
        """有效标记不授权删除根目录中断残留的标记临时文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_root = root / "UpdateCache"
            installs_dir = cache_root / "installs" / "v1.2.0"
            installs_dir.mkdir(parents=True)
            (cache_root / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            staging_path = cache_root / ".self_updater_cache.tmp-interrupted"
            staging_path.write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            (installs_dir / "App.exe").write_bytes(b"cache")

            SelfUpdater.clean_update_cache(temp_dir, logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(staging_path.exists())
            self.assertFalse(installs_dir.exists())

    def test_clean_update_cache_skips_file_cache_root(self):
        """UpdateCache 为普通文件时清理应告警并保留该文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache_root = root / "UpdateCache"
            cache_root.write_bytes(b"not-a-directory")

            with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                SelfUpdater.clean_update_cache(temp_dir, logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(cache_root.is_file())
            self.assertIn("跳过不安全的缓存目录", "\n".join(captured.output))

    def test_clean_update_cache_skips_root_symlink_and_preserves_target(self):
        """UpdateCache 根目录为符号链接时不得清理外部目标。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external_root = root / "external"
            external_root.mkdir()
            cached_file = external_root / "App.exe"
            cached_file.write_bytes(b"cache")
            (external_root / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            cache_root = root / "UpdateCache"
            try:
                cache_root.symlink_to(external_root, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"当前环境无法创建符号链接: {error}")

            with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                SelfUpdater.clean_update_cache(temp_dir, logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(cache_root.is_symlink())
            self.assertTrue(cached_file.exists())

    def test_clean_update_cache_skips_root_junction_and_preserves_target(self):
        """UpdateCache 根目录为 junction 时不得清理外部目标。"""
        if not sys.platform.startswith("win"):
            self.skipTest("仅 Windows 支持 junction")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external_root = root / "external"
            external_root.mkdir()
            cached_file = external_root / "App.exe"
            cached_file.write_bytes(b"cache")
            (external_root / SelfUpdater._UPDATE_CACHE_MARKER_FILE).write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            cache_root = root / "UpdateCache"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(cache_root), str(external_root)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"无法创建 junction: {result.stderr}")

            with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                SelfUpdater.clean_update_cache(temp_dir, logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(cache_root.exists())
            self.assertTrue(cached_file.exists())

    def test_check_self_update_stops_before_download_for_invalid_marker(self):
        """缓存标记无效时 check_self_update 应在下载前中止。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=temp_dir)
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            marker_path = root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            marker_path.write_text("invalid", encoding="ascii")
            release_info = {"tag_name": "v1.2.0"}

            with patch.object(updater, "_check_system_environment", return_value=True), patch.object(
                    updater,
                    "_fetch_latest_release",
                    return_value=release_info,
            ), patch.object(updater, "_match_asset", return_value=("https://example.invalid/App.exe", "App.exe")), patch.object(
                    updater,
                    "_get_asset_sha256",
                    return_value="a" * 64,
            ), patch.object(updater, "_fetch_current_release_sha256", return_value=""), patch.object(
                    updater,
                    "_download_func",
            ) as download_func:
                self.assertFalse(updater.check_self_update())

            download_func.assert_not_called()

    def test_check_self_update_has_valid_marker_before_download(self):
        """完整更新流程调用下载前必须已有有效根目录标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self.make_updater(temp_folder=temp_dir)
            release_info = {"tag_name": "v1.2.0"}
            observed_marker_contents = []

            def download_func(url, save_path):
                """记录下载调用前的缓存标记内容。"""
                marker_path = (
                    Path(temp_dir)
                    / "UpdateCache"
                    / SelfUpdater._UPDATE_CACHE_MARKER_FILE
                )
                observed_marker_contents.append(marker_path.read_text(encoding="ascii"))
                Path(save_path).write_bytes(b"new")
                return True

            updater._download_func = download_func
            with patch.object(updater, "_check_system_environment", return_value=True), patch.object(
                    updater,
                    "_fetch_latest_release",
                    return_value=release_info,
            ), patch.object(
                    updater,
                    "_match_asset",
                    return_value=("https://example.invalid/App.exe", "App.exe"),
            ), patch.object(
                    updater,
                    "_get_asset_sha256",
                    return_value="a" * 64,
            ), patch.object(
                    updater,
                    "_fetch_current_release_sha256",
                    return_value="",
            ), patch("self_updater.self_updater.calculate_sha256", return_value="a" * 64), patch.object(
                    updater,
                    "_replace_executable",
            ):
                self.assertTrue(updater.check_self_update())

            self.assertEqual(
                [SelfUpdater._UPDATE_CACHE_MARKER_CONTENT],
                observed_marker_contents,
            )

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
            installs_dir = cache_dir / "installs"
            installs_dir.mkdir()
            (installs_dir / "delete.txt").write_text("delete", encoding="utf-8")
            junction = installs_dir / "external-junction"
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
            self.assertFalse((installs_dir / "delete.txt").exists())
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
                updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(target_file.exists())
            self.assertTrue(link_file.exists())
            self.assertTrue((program_dir / UpdateState.STATE_FILE_NAME).exists())

    def test_cleanup_update_residue_skips_runtime_dir_with_reparse_ancestor_via_mock(self):
        """运行时目录祖先为重解析点时不得删除残留文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_file = paths["new_file"]
            runtime_file.write_text("runtime", encoding="utf-8")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(paths["runtime_dir"])
            state["new_file"] = str(runtime_file)
            state.save()
            reparse_ancestor = Path(updater.temp_folder)
            real_unsafe_path = SelfUpdater._is_unsafe_path.__func__

            def fake_unsafe_path(cls, path):
                """仅将 runtime_dir 的祖先目录视为重解析点。"""
                if Path(path) == reparse_ancestor:
                    return True
                return real_unsafe_path(cls, path)

            with patch.object(
                    SelfUpdater,
                    "_is_unsafe_path",
                    classmethod(fake_unsafe_path),
            ), patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                    updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(runtime_file.exists())

    def test_cleanup_update_residue_skips_residue_with_reparse_ancestor_via_mock(self):
        """残留文件祖先为重解析点时不得删除该文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            nested_dir = paths["runtime_dir"] / "nested"
            nested_dir.mkdir()
            residue_file = nested_dir / "App.new.exe"
            residue_file.write_text("runtime", encoding="utf-8")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(paths["runtime_dir"])
            state["new_file"] = str(residue_file)
            state.save()
            real_unsafe_path = SelfUpdater._is_unsafe_path.__func__

            def fake_unsafe_path(cls, path):
                """仅将残留文件父目录视为重解析点。"""
                if Path(path) == nested_dir:
                    return True
                return real_unsafe_path(cls, path)

            with patch.object(
                    SelfUpdater,
                    "_is_unsafe_path",
                    classmethod(fake_unsafe_path),
            ), patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                    updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(residue_file.exists())

    def test_cleanup_update_residue_skips_runtime_dir_junction(self):
        """运行时目录为 Windows junction 时应保留连接与外部残留。"""
        if not sys.platform.startswith("win"):
            self.skipTest("仅 Windows 支持 junction")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            program_dir = root / "program"
            temp_folder = root / "self-update"
            external_dir = root / "external"
            program_dir.mkdir()
            temp_folder.mkdir()
            external_dir.mkdir()
            current_exe = program_dir / "App.exe"
            current_exe.write_bytes(b"old")
            junction = temp_folder / "v1.2.0"
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external_dir)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"无法创建 junction: {result.stderr}")
            residue_file = junction / "App.new.exe"
            residue_file.write_text("runtime", encoding="utf-8")
            updater = self.make_updater(temp_folder=str(temp_folder))
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(junction)
            state["new_file"] = str(residue_file)
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                with self.assertLogs("SelfUpdaterTest", level="WARNING"):
                    updater.cleanup_update_residue(logging.getLogger("SelfUpdaterTest"))

            self.assertTrue(junction.exists())
            self.assertTrue(residue_file.exists())
            self.assertTrue((external_dir / "App.new.exe").exists())

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

    def test_create_update_cache_marker_rejects_link_ancestor(self):
        """UpdateCache 祖先路径为链接或重解析点时不应写入标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            external_dir = root / "external"
            external_dir.mkdir()

            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self):
                """将 UpdateCache 目录视为符号链接。"""
                if str(self) == str(root / "UpdateCache"):
                    return True
                return real_is_symlink(self)

            with patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                with self.assertRaises(OSError):
                    updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertFalse(
                (external_dir / "installs").exists(),
                "不应在外部目录创建子目录",
            )
            self.assertFalse(
                (root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE).exists(),
            )

    def test_create_update_cache_marker_rejects_link_temp_folder(self):
        """原始临时目录为链接或重解析点时不应创建缓存标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self):
                """仅将原始临时目录视为符号链接。"""
                return str(self) == str(root) or real_is_symlink(self)

            with patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                with self.assertRaises(OSError):
                    updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertFalse((root / "UpdateCache").exists())

    def test_create_update_cache_marker_rejects_temp_folder_junction(self):
        """临时目录为 Windows junction 时不得向外部目标写入缓存。"""
        if not sys.platform.startswith("win"):
            self.skipTest("仅 Windows 支持 junction")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            external_dir = root / "external"
            junction = root / "temp-junction"
            external_dir.mkdir()
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(external_dir)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"无法创建 junction: {result.stderr}")
            updater = self.make_updater(temp_folder=str(junction))
            cache_dir = junction / "UpdateCache" / "installs" / "v1.2.0"

            with self.assertRaises(OSError):
                updater._create_update_cache_marker(cache_dir, str(junction))

            self.assertFalse((external_dir / "UpdateCache").exists())

    def test_create_update_cache_marker_rejects_cache_ancestor_junctions(self):
        """缓存链中的 Windows junction 均不得导致外部目录写入。"""
        if not sys.platform.startswith("win"):
            self.skipTest("仅 Windows 支持 junction")
        junction_levels = ("UpdateCache", "installs", "version")
        for level in junction_levels:
            with self.subTest(level=level), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                external_dir = root / "external"
                external_dir.mkdir()
                if level == "UpdateCache":
                    junction = root / "UpdateCache"
                elif level == "installs":
                    (root / "UpdateCache").mkdir()
                    junction = root / "UpdateCache" / "installs"
                else:
                    (root / "UpdateCache" / "installs").mkdir(parents=True)
                    junction = root / "UpdateCache" / "installs" / "v1.2.0"
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(junction), str(external_dir)],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    self.skipTest(f"无法创建 junction: {result.stderr}")
                updater = self.make_updater(temp_folder=str(root))
                cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"

                with self.assertRaises(OSError):
                    updater._create_update_cache_marker(cache_dir, str(root))

                self.assertFalse(
                    (external_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE).exists(),
                )

    def test_create_update_cache_marker_rejects_invalid_structure(self):
        """缓存目录不是 UpdateCache/installs/version 精确结构时应拒绝。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            invalid_paths = (
                root / "UpdateCache" / "other" / "v1.2.0",
                root / "UpdateCache" / "installs" / "v1.2.0" / "extra",
                root / "arbitrary" / "v1.2.0",
            )

            for cache_dir in invalid_paths:
                with self.subTest(cache_dir=cache_dir):
                    with self.assertRaises(OSError):
                        updater._create_update_cache_marker(cache_dir, temp_dir)
                    self.assertFalse(cache_dir.exists())

    def test_create_update_cache_marker_rejects_link_install_dir(self):
        """installs 目录为链接或重解析点时不应写入标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            marker_path = root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE

            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self):
                """将 installs 目录视为符号链接。"""
                if str(self) == str(root / "UpdateCache" / "installs"):
                    return True
                return real_is_symlink(self)

            with patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                with self.assertRaises(OSError):
                    updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertFalse(marker_path.exists())

    def test_create_update_cache_marker_rejects_link_cache_dir(self):
        """版本缓存目录为链接或重解析点时不应写入标记。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            cache_dir = root / "UpdateCache" / "installs" / "v1.2.0"
            cache_dir.mkdir(parents=True)
            marker_path = root / "UpdateCache" / SelfUpdater._UPDATE_CACHE_MARKER_FILE

            real_is_symlink = Path.is_symlink

            def fake_is_symlink(self):
                """将版本缓存目录视为符号链接。"""
                if str(self) == str(cache_dir):
                    return True
                return real_is_symlink(self)

            with patch.object(
                    Path,
                    "is_symlink",
                    autospec=True,
                    side_effect=fake_is_symlink,
            ):
                with self.assertRaises(OSError):
                    updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertFalse(marker_path.exists())

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
                updater._create_update_cache_marker(cache_dir, temp_dir)

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
                    updater._create_update_cache_marker(cache_dir, temp_dir)

            self.assertEqual("tampered", marker_path.read_text(encoding="ascii"))

    def test_remove_marked_cache_contents_logs_iterdir_error(self):
        """缓存目录枚举失败时应记录 warning 并返回。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "UpdateCache"
            cache_dir.mkdir()
            logger = logging.getLogger("SelfUpdaterTest")

            with patch.object(Path, "iterdir", side_effect=OSError("access denied")):
                with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                    SelfUpdater._remove_marked_cache_contents(cache_dir, logger)

            self.assertIn("枚举缓存目录失败", "\n".join(captured.output))

    def test_remove_marked_cache_contents_logs_unlink_oserror(self):
        """缓存普通文件删除失败时应记录 warning 并保留文件。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "UpdateCache"
            cache_dir.mkdir()
            cached_file = cache_dir / "App.exe"
            cached_file.write_bytes(b"cache")

            with patch.object(Path, "unlink", autospec=True, side_effect=OSError("access denied")):
                with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                    SelfUpdater._remove_marked_cache_contents(
                        cache_dir,
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertTrue(cached_file.exists())
            self.assertIn("清理缓存文件失败", "\n".join(captured.output))

    def test_remove_empty_directories_logs_iterdir_error(self):
        """空目录枚举失败时应记录 warning 并返回。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "runtime"
            root.mkdir()
            logger = logging.getLogger("SelfUpdaterTest")

            with patch.object(Path, "iterdir", side_effect=OSError("access denied")):
                with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                    SelfUpdater._remove_empty_directories(root, logger)

            self.assertIn("枚举目录失败", "\n".join(captured.output))

    def test_update_state_delete_propagates_oserror(self):
        """状态文件删除失败时 UpdateState 应将 OSError 传播给调用方。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            state = UpdateState(base_dir=Path(temp_dir))

            with patch.object(Path, "unlink", side_effect=OSError("access denied")):
                with self.assertRaises(OSError):
                    state.delete()

    def test_cleanup_update_residue_logs_state_delete_oserror(self):
        """残留清理删除状态文件失败时应记录 warning 且不崩溃。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(paths["runtime_dir"])
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                with patch.object(
                        UpdateState,
                        "delete",
                        side_effect=OSError("access denied"),
                ):
                    with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                        result = updater.cleanup_update_residue(
                            logging.getLogger("SelfUpdaterTest"),
                        )

            self.assertFalse(result)
            self.assertIn("删除更新状态文件失败", "\n".join(captured.output))

    def test_cleanup_update_residue_no_state_file_returns_true(self):
        """无状态文件时清理残留应返回 True 且不抛异常。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater = self.make_updater(temp_folder=str(root))
            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: None
                result = updater.cleanup_update_residue(
                    logging.getLogger("SelfUpdaterTest"),
                )

            self.assertTrue(result)

    def test_cleanup_update_residue_non_verified_state_returns_true_and_keeps_state(self):
        """状态非 verified 时清理残留应返回 True 且保留状态文件不修改。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            state = UpdateState(base_dir=program_dir)
            state["state"] = "rollback_done"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_version"] = "v1.2.0"
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                result = updater.cleanup_update_residue(
                    logging.getLogger("SelfUpdaterTest"),
                )

            self.assertTrue(result)
            loaded = UpdateState.load(base_dir=program_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual("rollback_done", loaded["state"])
            self.assertEqual("v1.2.0", loaded["new_version"])

    def test_cleanup_update_residue_preserves_state_after_runtime_dir_enumeration_failure(self):
        """运行时目录枚举失败时清理残留应返回 False 并保留状态文件。"""
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

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                with patch.object(Path, "iterdir", side_effect=OSError("access denied")):
                    result = updater.cleanup_update_residue(
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertFalse(result)
            self.assertTrue((program_dir / UpdateState.STATE_FILE_NAME).exists())
            loaded = UpdateState.load(base_dir=program_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual("verified", loaded["state"])

    def test_remove_marked_cache_contents_logs_rmdir_oserror(self):
        """缓存空目录删除失败时应记录 warning。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "UpdateCache"
            cache_dir.mkdir()

            with patch.object(Path, "rmdir", autospec=True, side_effect=OSError("access denied")):
                with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                    SelfUpdater._remove_marked_cache_contents(
                        cache_dir,
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertIn("删除缓存目录失败", "\n".join(captured.output))

    def test_remove_empty_directories_logs_rmdir_oserror(self):
        """残留空目录删除失败时应记录 warning。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir) / "runtime"
            runtime_dir.mkdir()

            with patch.object(Path, "rmdir", autospec=True, side_effect=OSError("access denied")):
                with self.assertLogs("SelfUpdaterTest", level="WARNING") as captured:
                    SelfUpdater._remove_empty_directories(
                        runtime_dir,
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertIn("删除空目录失败", "\n".join(captured.output))

    def test_cleanup_update_residue_preserves_state_after_nested_delete_failure(self):
        """残留文件删除失败时应返回 False 并保留状态文件供重试。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            residue_file = paths["new_file"]
            residue_file.write_text("runtime", encoding="utf-8")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_file"] = str(residue_file)
            state["backup_file"] = str(paths["backup_file"])
            state["helper_ps1"] = str(paths["helper_ps1"])
            state["update_ps1"] = str(paths["update_ps1"])
            state["lock_file"] = str(paths["lock_file"])
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                with patch.object(Path, "unlink", autospec=True, side_effect=OSError("access denied")):
                    result = updater.cleanup_update_residue(
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertFalse(result)
            self.assertTrue(residue_file.exists())
            self.assertTrue((program_dir / UpdateState.STATE_FILE_NAME).exists())

    def test_cleanup_retry_does_not_trigger_update_retry(self):
        """清理失败保留 verified 状态，重试清理不触发更新检查或回滚。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            updater, current_exe, paths = self._make_runtime_paths(root)
            program_dir = paths["program_dir"]
            runtime_dir = paths["runtime_dir"]
            residue_file = paths["new_file"]
            residue_file.write_text("runtime", encoding="utf-8")
            state = UpdateState(base_dir=program_dir)
            state["state"] = "verified"
            state["target"] = str(current_exe)
            state["runtime_dir"] = str(runtime_dir)
            state["new_file"] = str(residue_file)
            state["backup_file"] = str(paths["backup_file"])
            state["helper_ps1"] = str(paths["helper_ps1"])
            state["update_ps1"] = str(paths["update_ps1"])
            state["lock_file"] = str(paths["lock_file"])
            state["retry_count"] = "2"
            state.save()

            with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                    base_dir=program_dir,
                )
                with patch.object(Path, "unlink", autospec=True, side_effect=OSError("access denied")):
                    first_result = updater.cleanup_update_residue(
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertFalse(first_result)
            loaded = UpdateState.load(base_dir=program_dir)
            self.assertIsNotNone(loaded)
            self.assertEqual("verified", loaded["state"])
            self.assertEqual("2", loaded.get("Retry", "retry_count", fallback="0"))

            with patch.object(updater, "check_self_update") as mock_check, \
                    patch.object(SelfUpdater, "rollback") as mock_rollback:
                with patch("self_updater.self_updater.UpdateState", wraps=UpdateState) as state_cls:
                    state_cls.load.side_effect = lambda *args, **kwargs: UpdateState.load(
                        base_dir=program_dir,
                    )
                    second_result = updater.cleanup_update_residue(
                        logging.getLogger("SelfUpdaterTest"),
                    )

            self.assertTrue(second_result)
            self.assertFalse((program_dir / UpdateState.STATE_FILE_NAME).exists())
            mock_check.assert_not_called()
            mock_rollback.assert_not_called()

    def test_clean_update_cache_preserves_marker_after_nested_enumeration_failure(self):
        """clean_update_cache 深层枚举失败时应返回 False 且保留 marker。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing_dir = root / "missing"
            self.assertTrue(SelfUpdater.clean_update_cache(str(missing_dir), None))

            cache_dir = root / "UpdateCache"
            installs_dir = cache_dir / "installs" / "v1.2.0"
            installs_dir.mkdir(parents=True)
            marker_path = cache_dir / SelfUpdater._UPDATE_CACHE_MARKER_FILE
            marker_path.write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            (installs_dir / "App.exe").write_bytes(b"cache")
            self.assertTrue(SelfUpdater.clean_update_cache(str(root), None))
            self.assertFalse(cache_dir.exists())

            cache_dir.mkdir()
            marker_path.write_text("invalid", encoding="ascii")
            (cache_dir / "App.exe").write_bytes(b"cache")
            self.assertFalse(SelfUpdater.clean_update_cache(str(root), None))
            self.assertTrue(cache_dir.exists())

            file_cache = root / "UpdateCacheFile"
            file_cache.write_bytes(b"not-a-directory")
            self.assertFalse(SelfUpdater.clean_update_cache(str(file_cache), None))
            self.assertTrue(file_cache.exists())

            installs_dir = cache_dir / "installs"
            installs_dir.mkdir(exist_ok=True)
            (installs_dir / "App.exe").write_bytes(b"cache")
            marker_path.write_text(
                SelfUpdater._UPDATE_CACHE_MARKER_CONTENT,
                encoding="ascii",
            )
            with patch.object(Path, "iterdir", side_effect=OSError("access denied")):
                enum_result = SelfUpdater.clean_update_cache(str(root), None)
            self.assertFalse(enum_result)
            self.assertTrue(marker_path.exists())

            with patch.object(Path, "unlink", autospec=True, side_effect=OSError("access denied")):
                delete_result = SelfUpdater.clean_update_cache(str(root), None)
            self.assertFalse(delete_result)
            self.assertTrue(marker_path.exists())

    def test_self_update_cleanup_waits_for_helper_before_residue(self):
        """self_update_cleanup 应先等待 helper 退出再清理残留。"""
        updater = self.make_updater()
        with patch.object(SelfUpdater, "_wait_for_process_exit", return_value=True) as wait_for_exit, \
                patch.object(SelfUpdater, "clean_update_cache", return_value=True) as cache, \
                patch.object(SelfUpdater, "cleanup_update_residue", return_value=True) as residue:
            result = updater.self_update_cleanup(helper_pid=1234)

        self.assertEqual(0, result)
        wait_for_exit.assert_called_once_with(1234, 60.0, updater.logger)
        residue.assert_called_once()
        cache.assert_called_once()

    def test_self_update_cleanup_skips_residue_when_wait_fails(self):
        """等待 helper 失败时应跳过残留清理但仍执行缓存清理。"""
        updater = self.make_updater()
        with patch.object(SelfUpdater, "_wait_for_process_exit", return_value=False), \
                patch.object(SelfUpdater, "clean_update_cache", return_value=True) as cache, \
                patch.object(SelfUpdater, "cleanup_update_residue") as residue:
            result = updater.self_update_cleanup(helper_pid=1234)

        self.assertEqual(1, result)
        residue.assert_not_called()
        cache.assert_called_once()

    def test_self_update_cleanup_without_helper_pid(self):
        """未提供 helper PID 时应不等待并执行全部清理。"""
        updater = self.make_updater()
        with patch.object(SelfUpdater, "_wait_for_process_exit") as wait_for_exit, \
                patch.object(SelfUpdater, "clean_update_cache", return_value=True) as cache, \
                patch.object(SelfUpdater, "cleanup_update_residue", return_value=True) as residue:
            result = updater.self_update_cleanup()

        self.assertEqual(0, result)
        wait_for_exit.assert_not_called()
        residue.assert_called_once()
        cache.assert_called_once()

    def test_self_update_cleanup_rejects_negative_helper_pid(self):
        """负数 helper PID 应被拒绝并抛出 ValueError。"""
        updater = self.make_updater()
        with self.assertRaises(ValueError):
            updater.self_update_cleanup(helper_pid=-1)

    def test_self_update_cleanup_continues_after_cleanup_exception(self):
        """任一清理抛异常时应继续另一项并返回 1。"""
        updater = self.make_updater()
        with patch.object(SelfUpdater, "_wait_for_process_exit", return_value=True), \
                patch.object(SelfUpdater, "clean_update_cache", return_value=True) as cache, \
                patch.object(
                    SelfUpdater,
                    "cleanup_update_residue",
                    side_effect=RuntimeError("boom"),
                ):
            result = updater.self_update_cleanup(helper_pid=1234)

        self.assertEqual(1, result)
        cache.assert_called_once()

    def test_wait_for_process_exit_uses_synchronize_handle(self):
        """等待 helper 退出应使用 SYNCHRONIZE 句柄、毫秒超时并关闭句柄。"""
        updater = self.make_updater()
        mock_kernel32 = Mock()
        handle = 4321
        with patch.object(SelfUpdater, "_get_kernel32", return_value=mock_kernel32):
            mock_kernel32.OpenProcess.return_value = handle
            mock_kernel32.WaitForSingleObject.return_value = 0
            self.assertTrue(
                updater._wait_for_process_exit(4567, 60.0, updater.logger),
            )
            mock_kernel32.OpenProcess.assert_called_once_with(0x00100000, False, 4567)
            mock_kernel32.WaitForSingleObject.assert_called_once_with(handle, 60000)
            mock_kernel32.CloseHandle.assert_called_once_with(handle)

            mock_kernel32.OpenProcess.reset_mock()
            mock_kernel32.WaitForSingleObject.reset_mock()
            mock_kernel32.CloseHandle.reset_mock()
            mock_kernel32.OpenProcess.return_value = handle
            mock_kernel32.WaitForSingleObject.return_value = 258
            self.assertFalse(
                updater._wait_for_process_exit(4567, 60.0, updater.logger),
            )
            mock_kernel32.CloseHandle.assert_called_once_with(handle)

            mock_kernel32.OpenProcess.reset_mock()
            mock_kernel32.WaitForSingleObject.reset_mock()
            mock_kernel32.CloseHandle.reset_mock()
            mock_kernel32.OpenProcess.return_value = handle
            mock_kernel32.WaitForSingleObject.return_value = 0xFFFFFFFF
            self.assertFalse(
                updater._wait_for_process_exit(4567, 60.0, updater.logger),
            )
            mock_kernel32.CloseHandle.assert_called_once_with(handle)

            mock_kernel32.OpenProcess.reset_mock()
            mock_kernel32.WaitForSingleObject.reset_mock()
            mock_kernel32.CloseHandle.reset_mock()
            mock_kernel32.OpenProcess.return_value = None
            self.assertFalse(
                updater._wait_for_process_exit(4567, 60.0, updater.logger),
            )
            mock_kernel32.CloseHandle.assert_not_called()

            mock_kernel32.OpenProcess.reset_mock()
            mock_kernel32.WaitForSingleObject.reset_mock()
            mock_kernel32.CloseHandle.reset_mock()
            mock_kernel32.OpenProcess.return_value = handle
            mock_kernel32.WaitForSingleObject.side_effect = OSError("kernel32 error")
            self.assertFalse(
                updater._wait_for_process_exit(4567, 60.0, updater.logger),
            )
            mock_kernel32.CloseHandle.assert_called_once_with(handle)


class PowerShell51IntegrationTest(unittest.TestCase):
    """Windows PowerShell 5.1 集成测试（仅在本机可用时运行）。"""

    @classmethod
    def setUpClass(cls):
        """检测 Windows PowerShell 5.1 是否可用，不可用时跳过本类测试。"""
        if os.name != "nt" or not shutil.which("powershell.exe"):
            raise unittest.SkipTest("Windows PowerShell 5.1 不可用，跳过集成测试")

    def setUp(self):
        """初始化残留进程记录。"""
        self._started_pids = []

    def tearDown(self):
        """清理残留的捕获进程，避免测试相互干扰。"""
        for pid in self._started_pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F", "/T"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass
        self._started_pids = []

    def _write_argv_capture_script(self, root: Path) -> Path:
        """创建捕获 sys.argv[1:] 并写入 JSON 的 Python 捕获脚本。"""
        script = root / "capture_argv.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "# -_- coding: utf-8 -_-\n"
            "\n"
            "import json\n"
            "import sys\n"
            "\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
            "    json.dump(sys.argv[2:], handle, ensure_ascii=False)\n",
            encoding="utf-8",
        )
        return script

    @staticmethod
    def _ps_single_quote(value) -> str:
        """将字符串转义为 PowerShell 单引号字符串字面量。"""
        return "'" + str(value).replace("'", "''") + "'"

    @classmethod
    def _ps_string_literal(cls, value) -> str:
        """将字符串转为 PowerShell 字面量，回车用 [char]13 拼接避免脚本内裸 CR。"""
        text = str(value)
        if "\r" in text:
            parts = [cls._ps_single_quote(part) for part in text.split("\r")]
            return " + [char]13 + ".join(parts)
        return cls._ps_single_quote(text)

    def _convert_encoding_check_script(self, out_file: Path, value: str) -> str:
        """构造调用 ConvertTo-WindowsCommandLineArg 并写出编码结果的 PS 脚本。"""
        base = ps1_fragments.generate_common_base_functions_ps1()
        args = ps1_fragments.generate_helper_argument_functions_ps1()
        return (
            "$ErrorActionPreference = 'Stop'\n"
            + base
            + args
            + "$encoded = ConvertTo-WindowsCommandLineArg ("
            + self._ps_string_literal(value)
            + ")\n"
            + "[System.IO.File]::WriteAllText("
            + self._ps_single_quote(str(out_file))
            + ", $encoded, [System.Text.Encoding]::UTF8)\n"
        )

    def _run_ps(self, script: str):
        """在 Windows PowerShell 5.1 中执行脚本，返回 (returncode, stdout, stderr)。"""
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def _argv_capture_script(self, capture_script: Path, out_file: Path,
                             argv) -> str:
        """构造调用 Start-ProcWait 捕获 argv 的 PowerShell 脚本。"""
        base = ps1_fragments.generate_common_base_functions_ps1()
        state = ps1_fragments.generate_common_state_functions_ps1()
        args = ps1_fragments.generate_helper_argument_functions_ps1()
        lifecycle = ps1_fragments.generate_helper_lifecycle_functions_ps1()
        items = [self._ps_single_quote(capture_script), self._ps_single_quote(out_file)]
        items += [self._ps_string_literal(item) for item in argv]
        array = ", ".join(items)
        return (
            "$ErrorActionPreference = 'Stop'\n"
            + base
            + state
            + args
            + lifecycle
            + "$code = Start-ProcWait "
            + self._ps_single_quote(sys.executable)
            + " @(" + array + ") 60 $true\n"
            + "exit $code\n"
        )

    def test_start_proc_wait_round_trips_windows_argv(self):
        """Start-ProcWait 应按 Windows 命令行规则往返传递参数。"""
        cases = {
            "zero": [],
            "single": ["hello"],
            "multiple": ["a", "b", "c"],
            "empty_string": [""],
            "spaces": ["with space", " two "],
            "single_quote": ["it's a test"],
            "double_quote": ['say "hi"'],
            "unicode": ["中文参数"],
            "trailing_backslash": ["trailing\\"],
            "backtick": ["back`tick"],
            "dollar_paren": ["$()"],
            "semicolon": ["a;b"],
            "pipe": ["a|b"],
            "percent_env": ["%VAR%"],
            "carriage_return": ["line1\rline2"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capture_script = self._write_argv_capture_script(root)
            out_file = root / "argv.json"
            for name, argv in cases.items():
                with self.subTest(case=name):
                    if "\r" in "".join(argv):
                        # Windows PowerShell 5.1 基于 .NET Framework，其 Process.Start
                        # 会解析并重建 Arguments（CR/LF 被当作空白分隔符），含 CR 参数
                        # 无法逐字 round-trip，属于平台限制。此处验证编码函数对含 CR
                        # 参数输出引号包裹（快速路径字符类包含 \r，不裸传拆分）。
                        enc_out = root / ("encoded_" + name + ".txt")
                        script = self._convert_encoding_check_script(enc_out, argv[0])
                        code, stdout, stderr = self._run_ps(script)
                        self.assertEqual(
                            0, code,
                            msg="powershell failed:\n{0}\n{1}".format(stdout, stderr),
                        )
                        with open(enc_out, "r", encoding="utf-8-sig",
                                  newline="") as handle:
                            encoded_text = handle.read()
                        self.assertEqual('"' + argv[0] + '"', encoded_text)
                        continue
                    if out_file.exists():
                        out_file.unlink()
                    script = self._argv_capture_script(capture_script, out_file, argv)
                    code, stdout, stderr = self._run_ps(script)
                    self.assertEqual(
                        0, code, msg="powershell failed:\n{0}\n{1}".format(stdout, stderr),
                    )
                    loaded = json.loads(out_file.read_text(encoding="utf-8"))
                    self.assertEqual(argv, loaded)

    def test_start_normal_app_visible_round_trips_windows_argv(self):
        """Start-NormalAppVisible 应按 Windows 命令行规则往返传递参数。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capture_script = self._write_argv_capture_script(root)
            out_file = root / "argv.json"
            pid_file = root / "pid.txt"

            argv = ["--config", r"C:\path with space\app.ini", 'quote"x', "中文"]
            base = ps1_fragments.generate_common_base_functions_ps1()
            state = ps1_fragments.generate_common_state_functions_ps1()
            args = ps1_fragments.generate_helper_argument_functions_ps1()
            lifecycle = ps1_fragments.generate_helper_lifecycle_functions_ps1()
            items = [self._ps_single_quote(capture_script), self._ps_single_quote(out_file)]
            items += [self._ps_single_quote(item) for item in argv]
            array = ", ".join(items)
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                + base
                + state
                + args
                + lifecycle
                + "$p = Start-NormalAppVisible "
                + self._ps_single_quote(sys.executable)
                + " @(" + array + ")\n"
                + "$p.Id | Out-File -FilePath "
                + self._ps_single_quote(str(pid_file))
                + " -Encoding ascii\n"
            )
            code, stdout, stderr = self._run_ps(script)
            self.assertEqual(
                0, code, msg="powershell failed:\n{0}\n{1}".format(stdout, stderr),
            )

            if pid_file.exists():
                self._started_pids.append(
                    int(pid_file.read_text(encoding="ascii").strip()),
                )

            loaded = None
            deadline = time.time() + 10
            while time.time() < deadline:
                if out_file.exists():
                    try:
                        loaded = json.loads(out_file.read_text(encoding="utf-8"))
                        break
                    except (ValueError, OSError):
                        pass
                time.sleep(0.1)
            self.assertIsNotNone(loaded, "捕获脚本未在 10 秒内完成")
            self.assertEqual(argv, loaded)

    def test_write_ini_value_preserves_launch_args_json(self):
        """Write-IniValue 与 Read-IniValue 应保持启动参数 JSON 特殊字符往返一致。"""
        payloads = {
            "percent": "%VAR%",
            "semicolon": "a;b",
            "hash": "#tag",
            "equals": "k=v",
            "unicode": "中文参数",
            "empty": "",
            "trailing_backslash": "C:\\path\\",
            "combined_json": json.dumps(
                ["a b", 'x"y', "%VAR%", "", "中文"], ensure_ascii=False,
            ),
        }
        base = ps1_fragments.generate_common_base_functions_ps1()
        state = ps1_fragments.generate_common_state_functions_ps1()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name, payload in payloads.items():
                with self.subTest(case=name):
                    state_file = root / ("state_" + name + ".ini")
                    state_file.write_text(
                        "[State]\nstate = pending_new_verify\n",
                        encoding="utf-8",
                    )
                    log_file = root / "update.log"
                    out_file = root / ("readback_" + name + ".txt")
                    script = (
                        "$ErrorActionPreference = 'Stop'\n"
                        + base
                        + state
                        + "$stateFile = " + self._ps_single_quote(str(state_file)) + "\n"
                        + "$logFile = " + self._ps_single_quote(str(log_file)) + "\n"
                        + 'Write-IniValue "LaunchArgs" "passthrough_args_json" '
                        + self._ps_single_quote(payload) + "\n"
                        + '$readBack = Read-IniValue "LaunchArgs" "passthrough_args_json"\n'
                        + "[System.IO.File]::WriteAllText("
                        + self._ps_single_quote(str(out_file))
                        + ", $readBack, [System.Text.Encoding]::UTF8)\n"
                    )
                    code, stdout, stderr = self._run_ps(script)
                    self.assertEqual(
                        0, code, msg="powershell failed:\n{0}\n{1}".format(stdout, stderr),
                    )
                    read_back = out_file.read_text(encoding="utf-8-sig")
                    self.assertEqual(payload, read_back)

    def _write_helper_flow_state(self, root: Path, action="exit") -> Path:
        """创建 Helper 主流程测试用状态文件并返回路径。"""
        state_file = root / "update_state.ini"
        state_file.write_text(
            "[State]\n"
            "state = downloaded_verified\n"
            "[Files]\n"
            "target = " + str(root / "App.exe") + "\n"
            "backup_file = " + str(root / "App.backup.exe") + "\n"
            "[Version]\n"
            "new_version = v1.2.0\n"
            "[Protocol]\n"
            "schema_version = 2\n"
            "[LaunchArgs]\n"
            "post_update_action = " + action + "\n"
            "passthrough_args_json = []\n",
            encoding="utf-8",
        )
        return state_file

    def _helper_main_flow_script(self, state_file: Path, log_file: Path,
                                 lock_file: Path, cleanup_starter_body: str,
                                 argv_file: Path = None) -> str:
        """构造执行 Helper 主流程分支的 PS 脚本，注入 cleanup 启动器替身。"""
        base = ps1_fragments.generate_common_base_functions_ps1()
        state = ps1_fragments.generate_common_state_functions_ps1()
        args = ps1_fragments.generate_helper_argument_functions_ps1()
        sha256 = ps1_fragments.generate_sha256_function_ps1()
        retry = ps1_fragments.generate_helper_retry_functions_ps1()
        cleanup = ps1_fragments.generate_helper_file_cleanup_functions_ps1()
        move = ps1_fragments.generate_move_with_retry_ps1()
        launch_args = ps1_fragments.generate_helper_launch_args_functions_ps1()
        lifecycle = ps1_fragments.generate_helper_lifecycle_functions_ps1()
        main_flow = ps1_fragments.generate_helper_main_flow_ps1()
        lines = [
            "$ErrorActionPreference = 'Stop'",
            "$scriptTag = 'Test'",
            "$stateFile = " + self._ps_single_quote(str(state_file)),
            "$logFile = " + self._ps_single_quote(str(log_file)),
            "$lockFile = " + self._ps_single_quote(str(lock_file)),
            "$updatePs1 = " + self._ps_single_quote(str(lock_file) + ".update.ps1"),
            "$ParentPid = 0",
            "try { New-Item -Path $lockFile -ItemType File -Force | Out-Null } catch {}",
        ]
        if argv_file is not None:
            lines.append("$argvFile = " + self._ps_single_quote(str(argv_file)))
        script = "\n".join(lines) + "\n"
        script += base + state + args + sha256 + retry + cleanup + move
        script += launch_args + lifecycle
        script += cleanup_starter_body + "\n"
        script += "function Start-ProcWait { return 0 }\n"
        script += main_flow + "\n"
        return script

    def _run_helper_flow(self, root: Path, cleanup_starter_body: str,
                         argv_file: Path = None):
        """运行 Helper 主流程分支脚本并返回执行结果与产物内容。"""
        state_file = self._write_helper_flow_state(root)
        log_file = root / "update.log"
        lock_file = root / "update_started.lock"
        script = self._helper_main_flow_script(
            state_file, log_file, lock_file, cleanup_starter_body, argv_file,
        )
        started = time.time()
        code, stdout, stderr = self._run_ps(script)
        elapsed = time.time() - started
        log_text = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
        state_text = state_file.read_text(encoding="utf-8")
        return code, stdout, stderr, log_text, state_text, elapsed

    def _assert_cleanup_failure_outcome(self, code, stdout, stderr,
                                        log_text, state_text):
        """断言 cleanup 启动失败时 Helper 仍成功退出且保持 verified 状态。"""
        self.assertEqual(
            0, code, msg="powershell failed:\n{0}\n{1}".format(stdout, stderr),
        )
        parser = configparser.ConfigParser()
        parser.read_string(state_text)
        self.assertEqual("verified", parser.get("State", "state"))
        self.assertIn("cleanup start failed:", log_text)
        self.assertNotIn("rollback", log_text)
        self.assertNotIn("回滚", log_text)

    @staticmethod
    def _cleanup_starter_recording_body() -> str:
        """返回记录传入 argv 的 cleanup 启动器替身公共开头。"""
        return (
            "$script:CleanupStarter = {\n"
            "    param($filePath, [string[]]$argList)\n"
            "    $json = @($argList) | ConvertTo-Json -Compress\n"
            "    [System.IO.File]::WriteAllText($argvFile, $json, [System.Text.Encoding]::UTF8)\n"
        )

    def test_cleanup_start_exception_keeps_helper_success(self):
        """Start-CleanupApp 抛异常时 Helper 应记录 warning、保持 verified 并退出 0。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            argv_file = root / "argv.json"
            cleanup_starter = (
                self._cleanup_starter_recording_body()
                + "    throw 'cleanup starter boom'\n"
                + "}\n"
            )
            code, stdout, stderr, log_text, state_text, _ = self._run_helper_flow(
                root, cleanup_starter, argv_file,
            )
            self._assert_cleanup_failure_outcome(
                code, stdout, stderr, log_text, state_text,
            )
            loaded = json.loads(argv_file.read_text(encoding="utf-8-sig"))
            self.assertEqual(
                ["--self-update-cleanup", "--self-update-cleanup-parent-pid", loaded[2]],
                loaded,
            )
            self.assertRegex(loaded[2], r"^\d+$")

    def test_cleanup_start_false_keeps_helper_success(self):
        """Start-CleanupApp 返回 $false 时 Helper 应记录 warning、保持 verified 并退出 0。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            argv_file = root / "argv.json"
            cleanup_starter = (
                self._cleanup_starter_recording_body()
                + "    return $false\n"
                + "}\n"
            )
            code, stdout, stderr, log_text, state_text, _ = self._run_helper_flow(
                root, cleanup_starter, argv_file,
            )
            self._assert_cleanup_failure_outcome(
                code, stdout, stderr, log_text, state_text,
            )
            loaded = json.loads(argv_file.read_text(encoding="utf-8-sig"))
            self.assertEqual(
                ["--self-update-cleanup", "--self-update-cleanup-parent-pid", loaded[2]],
                loaded,
            )

    def test_cleanup_start_without_process_keeps_helper_success(self):
        """Start-CleanupApp 返回无 PID 的无效对象时 Helper 应记录 warning 并退出 0。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            argv_file = root / "argv.json"
            cleanup_starter = (
                self._cleanup_starter_recording_body()
                + "    return [pscustomobject]@{ Name = 'invalid-process' }\n"
                + "}\n"
            )
            code, stdout, stderr, log_text, state_text, _ = self._run_helper_flow(
                root, cleanup_starter, argv_file,
            )
            self._assert_cleanup_failure_outcome(
                code, stdout, stderr, log_text, state_text,
            )
            loaded = json.loads(argv_file.read_text(encoding="utf-8-sig"))
            self.assertEqual(
                ["--self-update-cleanup", "--self-update-cleanup-parent-pid", loaded[2]],
                loaded,
            )

    def test_cleanup_start_success_does_not_wait(self):
        """Start-CleanupApp 成功后 Helper 应立即退出，不等待也不读取 ExitCode。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            argv_file = root / "argv.json"
            # 返回当前 PowerShell 进程对象（有效 Process、PID>0），不启动真实子进程，
            # 避免 PowerShell 5.1 在 -Command 模式下 Start-Process 与函数片段的引擎缺陷。
            cleanup_starter = (
                self._cleanup_starter_recording_body()
                + "    return (Get-Process -Id $PID)\n"
                + "}\n"
            )
            code, stdout, stderr, log_text, state_text, elapsed = self._run_helper_flow(
                root, cleanup_starter, argv_file,
            )
            self.assertEqual(
                0, code, msg="powershell failed:\n{0}\n{1}".format(stdout, stderr),
            )
            parser = configparser.ConfigParser()
            parser.read_string(state_text)
            self.assertEqual("verified", parser.get("State", "state"))
            self.assertIn("cleanup process started", log_text)
            self.assertNotIn("rollback", log_text)
            self.assertLess(elapsed, 20, msg="Helper 不应等待 cleanup 进程退出")

            loaded = json.loads(argv_file.read_text(encoding="utf-8-sig"))
            self.assertEqual(
                ["--self-update-cleanup", "--self-update-cleanup-parent-pid", loaded[2]],
                loaded,
            )

            # 启动器与 exit 分支不得包含等待或退出码读取调用
            lifecycle = ps1_fragments.generate_helper_lifecycle_functions_ps1()
            starter_block = lifecycle.split("$script:CleanupStarter = {", 1)[1].split(
                "function Start-CleanupApp", 1,
            )[0]
            cleanup_fn = lifecycle.split("function Start-CleanupApp", 1)[1]
            self.assertNotIn("WaitForExit", starter_block)
            self.assertNotIn("ExitCode", starter_block)
            self.assertNotIn("WaitForExit", cleanup_fn)
            self.assertNotIn("ExitCode", cleanup_fn)
            main_flow = ps1_fragments.generate_helper_main_flow_ps1()
            exit_branch = main_flow[main_flow.index("Start-CleanupApp"):]
            self.assertNotIn("WaitForExit", exit_branch)
            self.assertNotIn("ExitCode", exit_branch)

    def test_cleanup_waits_for_helper_before_deleting_runtime(self):
        """self_update_cleanup 应等待 helper 进程退出后才删除运行时残留。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            temp_folder = root / "selfupdate"
            runtime_dir = temp_folder / "v1.2.0"
            runtime_dir.mkdir(parents=True)
            helper_ps1 = runtime_dir / "TestApp_Update_Helper.ps1"
            helper_ps1.write_text("# helper script stub\n", encoding="utf-8")
            state_file = temp_folder / "update_state.ini"
            state_file.write_text(
                "[State]\n"
                "state = verified\n"
                "[Files]\n"
                "target = " + str(root / "App.exe") + "\n"
                "backup_file = " + str(runtime_dir / "App.backup.exe") + "\n"
                "new_file = " + str(runtime_dir / "App.new.exe") + "\n"
                "runtime_dir = " + str(runtime_dir) + "\n"
                "helper_ps1 = " + str(helper_ps1) + "\n"
                "update_ps1 = " + str(runtime_dir / "TestApp_Update.ps1") + "\n"
                "lock_file = " + str(runtime_dir / "update_started.lock") + "\n",
                encoding="utf-8",
            )

            # helper 替身：短暂存活 5 秒后自然退出（留足 cleanup 启动与轮询时间差）
            helper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._started_pids.append(helper.pid)

            # cleanup 替身：真实调用 self_update_cleanup(helper_pid=helper.pid)
            stub = root / "cleanup_stub.py"
            stub.write_text(_CLEANUP_STUB_SOURCE, encoding="utf-8")
            project_root = Path(__file__).resolve().parent.parent
            cleanup_proc = subprocess.Popen(
                [sys.executable, str(stub), str(temp_folder), str(temp_folder),
                 str(project_root), str(helper.pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                # helper 存活期间轮询：残留与状态文件必须仍存在（避免竞态删除）
                poll_deadline = time.time() + 2.5
                poll_hits = 0
                while time.time() < poll_deadline:
                    self.assertTrue(
                        helper_ps1.exists(),
                        "helper 存活期间残留文件不应被删除",
                    )
                    self.assertTrue(
                        state_file.exists(),
                        "helper 存活期间状态文件不应被删除",
                    )
                    poll_hits += 1
                    time.sleep(0.2)
                self.assertGreaterEqual(
                    poll_hits, 1,
                    "helper 存活期间至少成功轮询到一次残留文件",
                )
                stdout, stderr = cleanup_proc.communicate(timeout=60)
            finally:
                if cleanup_proc.poll() is None:
                    cleanup_proc.kill()

            self.assertEqual(
                0, cleanup_proc.returncode,
                msg="cleanup stub failed:\n{0}\n{1}".format(stdout, stderr),
            )
            result_file = temp_folder / "cleanup_result.json"
            self.assertTrue(result_file.exists(), "cleanup 替身未写出结果文件")
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(0, result["code"], msg=result.get("error", ""))
            self.assertGreaterEqual(
                result["elapsed"], 2.5,
                "cleanup 应等待 helper 退出（替身存活约 5 秒）",
            )
            # helper 退出后 cleanup 完成全部残留删除
            self.assertFalse(helper_ps1.exists(), "helper 退出后残留文件应被删除")
            self.assertFalse(runtime_dir.exists(), "helper 退出后运行时目录应被删除")
            self.assertFalse(state_file.exists(), "helper 退出后状态文件应被删除")
            helper.wait(timeout=10)

    def test_cleanup_failure_never_starts_update_protocol(self):
        """cleanup 启动失败时 Helper 不得产生任何更新协议子进程参数。"""
        forbidden_flags = (
            "--retry-update", "--update", "--update-force", "--update-failed",
        )
        failure_bodies = {
            "throw": "    throw 'cleanup starter boom'\n}\n",
            "false": "    return $false\n}\n",
            "invalid_process": (
                "    return [pscustomobject]@{ Name = 'invalid-process' }\n}\n"
            ),
        }
        for name, tail in failure_bodies.items():
            with self.subTest(failure=name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    root = Path(temp_dir)
                    argv_file = root / "argv.json"
                    cleanup_starter = self._cleanup_starter_recording_body() + tail
                    code, stdout, stderr, log_text, state_text, _ = self._run_helper_flow(
                        root, cleanup_starter, argv_file,
                    )
                    self._assert_cleanup_failure_outcome(
                        code, stdout, stderr, log_text, state_text,
                    )
                    loaded = json.loads(argv_file.read_text(encoding="utf-8-sig"))
                    self.assertEqual(
                        ["--self-update-cleanup", "--self-update-cleanup-parent-pid",
                         loaded[2]],
                        loaded,
                    )
                    for flag in forbidden_flags:
                        self.assertNotIn(flag, loaded)

    def test_cleanup_real_child_process_helper_exits_immediately(self):
        """Helper 启动 cleanup 真实子进程后应立即返回，不等待其完成。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pid_file = root / "child_pid.txt"
            elapsed_file = root / "elapsed_ms.txt"
            alive_file = root / "child_alive.txt"
            base = ps1_fragments.generate_common_base_functions_ps1()
            state = ps1_fragments.generate_common_state_functions_ps1()
            args = ps1_fragments.generate_helper_argument_functions_ps1()
            lifecycle = ps1_fragments.generate_helper_lifecycle_functions_ps1()
            script = (
                "$ErrorActionPreference = 'Stop'\n"
                + base + state + args + lifecycle
                + "$sw = [System.Diagnostics.Stopwatch]::StartNew()\n"
                + "$proc = & $script:CleanupStarter "
                + self._ps_single_quote(sys.executable)
                + " @('-c', 'import time; time.sleep(5)')\n"
                + "$sw.Stop()\n"
                + "if ($null -eq $proc) { throw 'cleanup starter returned null' }\n"
                + "$proc.Id | Out-File -FilePath "
                + self._ps_single_quote(str(pid_file)) + " -Encoding ascii\n"
                + "$sw.ElapsedMilliseconds | Out-File -FilePath "
                + self._ps_single_quote(str(elapsed_file)) + " -Encoding ascii\n"
                + "$alive = $false\n"
                + "try { if (Get-Process -Id $proc.Id -ErrorAction Stop) { $alive = $true } } catch {}\n"
                + "$alive | Out-File -FilePath "
                + self._ps_single_quote(str(alive_file)) + " -Encoding ascii\n"
                + "exit 0\n"
            )
            code, stdout, stderr = self._run_ps(script)
            self.assertEqual(
                0, code, msg="powershell failed:\n{0}\n{1}".format(stdout, stderr),
            )
            child_pid = int(pid_file.read_text(encoding="ascii").strip())
            self._started_pids.append(child_pid)
            elapsed_ms = int(elapsed_file.read_text(encoding="ascii").strip())
            self.assertLess(elapsed_ms, 2000, "Helper 不应等待 cleanup 子进程完成")
            # 存活检查必须在 PowerShell 内、启动后立即进行：子进程继承了
            # powershell 的管道句柄，外部等待会阻塞到子进程退出而无法观察到存活
            alive_text = alive_file.read_text(encoding="ascii").strip()
            self.assertEqual(
                "True", alive_text,
                "cleanup 子进程应仍在运行（启动方未等待其退出）",
            )


_CLEANUP_STUB_SOURCE = '''#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""更新清理替身：真实调用 self_update_cleanup 并记录结果。"""

import json
import logging
import sys
import time

from pathlib import Path


def main() -> int:
    """执行更新清理并写出结果文件。"""
    # 参数：state_dir temp_folder project_root helper_pid
    state_dir = Path(sys.argv[1])
    temp_folder = Path(sys.argv[2])
    project_root = Path(sys.argv[3])
    helper_pid = int(sys.argv[4])

    # 让 UpdateState.load() 从状态文件所在目录加载 update_state.ini
    sys.argv[0] = str(state_dir / "app_entry.exe")

    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from self_updater.self_updater import SelfUpdater

    logger = logging.getLogger("cleanup_stub")
    logger.addHandler(logging.NullHandler())
    updater = SelfUpdater(
        github_repo="owner/repo",
        asset_pattern=r"App.*",
        app_name="TestApp",
        current_version="v1.0.0",
        proxy="",
        logger=logger,
        temp_folder=str(temp_folder),
    )
    started = time.time()
    try:
        code = updater.self_update_cleanup(helper_pid=helper_pid)
    except Exception as error:
        code = 99
        error_text = repr(error)
    else:
        error_text = ""
    elapsed = time.time() - started
    result = {"code": code, "elapsed": elapsed, "error": error_text}
    result_file = state_dir / "cleanup_result.json"
    result_file.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


if __name__ == "__main__":
    unittest.main()
