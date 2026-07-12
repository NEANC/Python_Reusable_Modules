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

from self_updater import ps1_fragments
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

    def make_updater(self, current_version="v1.0.0", app_name="App"):
        """创建测试用 SelfUpdater 实例。"""
        return SelfUpdater(
            github_repo="owner/repo",
            asset_pattern=r"^App-(Nuitka|PyInstaller)-v[\d.]+.*\.exe$",
            app_name=app_name,
            current_version=current_version,
            proxy="",
            logger=logging.getLogger("SelfUpdaterTest"),
            is_bundled=True,
            package_type="Nuitka",
        )

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

    def test_generated_ps1_has_sha256_fallbacks(self):
        """生成的 PS1 应包含 SHA256 多路径 fallback。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            updater = self.make_updater()
            updater._generate_helper_ps1(Path(temp_dir))
            updater._generate_update_ps1(Path(temp_dir))

            helper_text = (Path(temp_dir) / "App_Update_Helper.ps1").read_text(encoding="utf-8-sig")
            update_text = (Path(temp_dir) / "App_Update.ps1").read_text(encoding="utf-8-sig")
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
            updater = self.make_updater()
            updater._generate_helper_ps1(Path(temp_dir))
            updater._generate_update_ps1(Path(temp_dir))

            helper_text = (Path(temp_dir) / "App_Update_Helper.ps1").read_text(encoding="utf-8-sig")
            update_text = (Path(temp_dir) / "App_Update.ps1").read_text(encoding="utf-8-sig")

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


if __name__ == "__main__":
    unittest.main()
