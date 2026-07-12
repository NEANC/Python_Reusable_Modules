#!/usr/bin/env python3
# -_- coding: utf-8 -_-

"""
自更新器核心模块。

负责：
  - 从 GitHub Release 获取最新版本信息
  - 版本比较（支持 pre-release 语义）
  - 下载新版本 exe 并校验 SHA256
  - 生成 PowerShell 脚本完成热替换
  - 新版健康检查验证
  - 失败回滚

可移植到其他项目，通过构造函数注入参数即可。
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import requests

from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .ps1_fragments import generate_common_base_functions_ps1
from .ps1_fragments import generate_common_state_functions_ps1
from .ps1_fragments import generate_helper_argument_functions_ps1
from .ps1_fragments import generate_helper_file_cleanup_functions_ps1
from .ps1_fragments import generate_helper_lifecycle_functions_ps1
from .ps1_fragments import generate_helper_retry_functions_ps1
from .ps1_fragments import generate_move_with_retry_ps1
from .ps1_fragments import generate_sha256_function_ps1
from .self_config import UpdateState
from .self_utils import (
    calculate_sha256,
    detect_package_type,
    get_exe_path,
    is_build_tag,
    version_newer_than,
    version_to_tuple,
)


def _get_existing_retry_count(base_dir: Optional[Path] = None) -> str:
    """读取已存在的 update_state.ini 中的 retry_count，若无则返回 '0'"""
    existing = UpdateState.load(base_dir=base_dir)
    if existing:
        return existing.get("Retry", "retry_count", fallback="0")
    return "0"


class SelfUpdater:
    """自更新器，负责自我更新检查、下载、替换、回滚"""

    _APP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
    _WINDOWS_RESERVED_NAMES = {
        "CON", "NUL", "AUX", "PRN",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }

    def __init__(self, github_repo: str, asset_pattern: str, app_name: str,
                 current_version: str, proxy: str,
                 logger: logging.Logger,
                 temp_folder: Optional[str] = None,
                 download_func: Optional[Callable[[str, str], bool]] = None,
                 self_update_channel: str = 'preview',
                 is_bundled: Optional[bool] = None,
                 package_type: Optional[str] = None):
        """
        初始化自更新器

        Args:
            github_repo: GitHub 仓库（格式 "owner/repo"）
            asset_pattern: exe asset 文件名正则模式
            app_name: 应用名称（用于 PS1 脚本和缓存目录命名）
            current_version: 当前版本号（如 v1.0.0）
            proxy: 代理地址（空字符串表示无代理）
            temp_folder: 临时文件夹路径，不传则自动解析（系统缓存 > 脚本目录）
            logger: 日志记录器
            download_func: 下载回调 (url, save_path) -> bool，不传则使用内置 requests 下载
            self_update_channel: 更新通道 ('preview', 'stable')
            is_bundled: 外部预检测的是否为打包程序（可选）
            package_type: 外部预检测的打包方式（可选）
        """
        self._validate_app_name(app_name)
        self.github_repo = github_repo
        self.asset_regex = re.compile(asset_pattern)
        self.app_name = app_name
        self.current_version = current_version
        self.proxy = proxy
        self.logger = logger
        self.temp_folder = self._resolve_temp_folder(temp_folder)
        self._download_func = download_func or self._default_download
        self.self_update_channel = self_update_channel
        self._is_bundled = is_bundled
        self._package_type = package_type

    @classmethod
    def _validate_app_name(cls, app_name: str) -> None:
        """校验应用名称只能包含安全的文件名字符。"""
        app_name_base = app_name.split(".", 1)[0].upper() if app_name else ""
        invalid_name = (
            not app_name
            or not cls._APP_NAME_PATTERN.fullmatch(app_name)
            or app_name.strip(".") == ""
            or app_name.startswith(".")
            or app_name.endswith(".")
            or app_name_base in cls._WINDOWS_RESERVED_NAMES
        )
        if invalid_name:
            raise ValueError(
                "app_name 只能包含英文字母、数字、下划线、点和连字符，"
                "且不能为纯点号、首尾点号或 Windows 保留设备名"
            )

    def _resolve_temp_folder(self, temp_folder: Optional[str]) -> str:
        """解析自更新临时目录，优先使用 LOCALAPPDATA，失败时回退程序目录。"""
        if temp_folder:
            return temp_folder

        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            local_dir = Path(local_appdata) / self.app_name / "SelfUpdate"
            try:
                local_dir.mkdir(parents=True, exist_ok=True)
                return str(local_dir)
            except OSError as e:
                self.logger.warning(f"创建 LOCALAPPDATA 自更新目录失败，将回退程序目录: {e}")

        program_dir = Path(sys.argv[0]).resolve().parent
        return str(program_dir / "SelfUpdate")

    def _resolve_runtime_dir(self, new_version: str) -> Path:
        """解析本次更新运行时目录。"""
        return Path(self.temp_folder) / new_version

    def _build_update_runtime_paths(self, current_exe: Path,
                                    new_version: str) -> Dict[str, Path]:
        """构建本次更新涉及的运行时路径。"""
        program_dir = current_exe.parent
        temp_folder = Path(self.temp_folder)
        runtime_dir = self._resolve_runtime_dir(new_version)
        return {
            "program_dir": program_dir,
            "state_file": program_dir / UpdateState.STATE_FILE_NAME,
            "log_file": program_dir / "update.log",
            "temp_folder": temp_folder,
            "runtime_dir": runtime_dir,
            "helper_ps1": runtime_dir / f"{self.app_name}_Update_Helper.ps1",
            "update_ps1": runtime_dir / f"{self.app_name}_Update.ps1",
            "lock_file": runtime_dir / "update_started.lock",
            "new_file": runtime_dir / f"{current_exe.stem}.new.exe",
            "backup_file": runtime_dir / f"{current_exe.stem}.backup.exe",
        }

    def _default_download(self, url: str, save_path: str) -> bool:
        """内置下载实现（无进度条），外部可注入带进度的下载函数覆盖"""
        try:
            headers = {'User-Agent': 'SelfUpdater'}
            proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
            response = requests.get(url, headers=headers, proxies=proxies,
                                    timeout=120, stream=True)
            response.raise_for_status()
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1048576):
                    if chunk:
                        f.write(chunk)
            return True
        except requests.RequestException as e:
            self.logger.error(f"下载失败: {e}")
            return False

    def _resolve_channel(self) -> str:
        """解析通道配置，兼容旧值"""
        if self.self_update_channel in ('preview', 'release'):
            return 'preview'
        if self.self_update_channel in ('stable', 'latest'):
            return 'stable'
        return 'preview'

    def _make_headers(self) -> Dict[str, str]:
        """构建请求头"""
        return {'User-Agent': self.app_name}

    def _make_proxies(self) -> Optional[Dict[str, str]]:
        """构建代理配置"""
        return {'http': self.proxy, 'https': self.proxy} if self.proxy else None

    def _fetch_latest_release(self) -> Optional[Dict]:
        """从 GitHub API 获取最新 release 信息"""
        try:
            headers = self._make_headers()
            proxies = self._make_proxies()
            channel = self._resolve_channel()

            if channel == 'preview':
                api_url = f"https://api.github.com/repos/{self.github_repo}/releases"
                response = requests.get(api_url, headers=headers, proxies=proxies, timeout=30)
                response.raise_for_status()
                release_info = self._select_preview_release(response.json())
                if not release_info:
                    self.logger.error("未找到任何有效的 release")
                    return None
            else:
                api_url = f"https://api.github.com/repos/{self.github_repo}/releases/latest"
                response = requests.get(api_url, headers=headers, proxies=proxies, timeout=30)
                response.raise_for_status()
                release_info = response.json()

            self.logger.debug(f"GitHub 版本: {release_info.get('tag_name', 'Unknown')}")
            return release_info
        except requests.RequestException as e:
            self.logger.critical(f"获取 GitHub release 信息失败: {e}")
            return None
        except Exception as e:
            self.logger.critical(f"获取 GitHub release 时出错: {e}")
            return None

    def _select_preview_release(self, releases: List[Dict]) -> Optional[Dict]:
        """从 preview release 列表中选择语义版本最高的可用版本。"""
        selected = None
        for release in releases:
            tag_name = release.get('tag_name', '')
            if release.get('draft') or is_build_tag(tag_name) or not version_to_tuple(tag_name):
                continue
            if selected is None or version_newer_than(selected.get('tag_name', ''), tag_name):
                selected = release
        return selected

    def _get_asset_sha256(self, release_info: Dict, asset_name: str) -> str:
        """从 release 的 assets 和 body 中提取指定文件的 SHA256"""
        assets = release_info.get('assets', [])
        for asset in assets:
            if asset.get('name') == asset_name:
                digest = asset.get('digest', '')
                if digest.startswith('sha256:'):
                    return digest[7:]
        body = release_info.get('body', '')
        for line in body.split('\n'):
            if asset_name in line and 'sha256' in line.lower():
                match = re.search(r'[0-9a-f]{64}', line.lower())
                if match:
                    return match.group(0)
        return ""

    def _match_asset(self, release_info: Dict,
                     package_type: str) -> Tuple[str, str]:
        """从 release assets 中匹配对应打包方式的 exe 文件"""
        primary_keyword = package_type
        secondary_keyword = "PyInstaller" if package_type == "Nuitka" else "Nuitka"
        assets = release_info.get('assets', [])

        for asset in assets:
            asset_name = asset.get('name', '')
            if self.asset_regex.match(asset_name) and primary_keyword in asset_name:
                self.logger.info(f"找到 {primary_keyword} 版本: {asset_name}")
                return asset.get('browser_download_url', ''), asset_name

        for asset in assets:
            asset_name = asset.get('name', '')
            if self.asset_regex.match(asset_name) and secondary_keyword in asset_name:
                self.logger.info(
                    f"未找到 {primary_keyword} 版本，降级使用 {secondary_keyword} 版本: {asset_name}"
                )
                return asset.get('browser_download_url', ''), asset_name

        self.logger.critical("未找到符合命名规范的 exe 文件")
        return '', ''

    def _match_asset_exact(self, release_info: Dict,
                           package_type: str) -> Tuple[str, str]:
        """从 release assets 中精确匹配指定打包方式的 exe 文件。"""
        assets = release_info.get('assets', [])
        for asset in assets:
            asset_name = asset.get('name', '')
            if self.asset_regex.match(asset_name) and package_type in asset_name:
                self.logger.info(f"找到 {package_type} 版本: {asset_name}")
                return asset.get('browser_download_url', ''), asset_name

        self.logger.warning(f"未找到 {package_type} 版本的当前 release exe 文件")
        return '', ''

    def _fetch_current_release_sha256(self, package_type: str) -> str:
        """从 GitHub API 获取当前版本的 exe asset 的 SHA256"""
        try:
            api_url = (
                f"https://api.github.com/repos/{self.github_repo}"
                f"/releases/tags/{self.current_version}"
            )
            headers = self._make_headers()
            proxies = self._make_proxies()

            response = requests.get(api_url, headers=headers, proxies=proxies, timeout=30)
            if response.status_code == 404:
                self.logger.debug("tag 名称精确匹配未找到，遍历查找...")
                release_info = self._match_release_by_tag(headers, proxies)
            else:
                response.raise_for_status()
                release_info = response.json()

            if not release_info:
                self.logger.warning(
                    f"GitHub 上未找到当前版本 {self.current_version} 的 release"
                )
                return ""

            _, exe_name = self._match_asset_exact(release_info, package_type)
            if not exe_name:
                return ""

            return self._get_asset_sha256(release_info, exe_name)
        except requests.RequestException as e:
            self.logger.warning(f"获取当前版本 SHA256 失败: {e}")
            return ""

    def _match_release_by_tag(self, headers: dict,
                               proxies: dict) -> Optional[Dict]:
        """遍历 releases 列表，按 tag_name 大小写不敏感匹配当前版本"""
        try:
            api_url = f"https://api.github.com/repos/{self.github_repo}/releases"
            params = {'per_page': 50}
            response = requests.get(api_url, headers=headers, proxies=proxies,
                                    params=params, timeout=30)
            response.raise_for_status()
            for release in response.json():
                if release.get('draft'):
                    continue
                if release.get('tag_name', '').lower() == self.current_version.lower():
                    return release
        except requests.RequestException as e:
            self.logger.debug(f"遍历 releases 匹配失败: {e}")
        return None

    # ── 公共方法 ──

    def _check_system_environment(self) -> bool:
        """
        检查当前系统是否支持自我更新：
          - Windows 操作系统
          - PowerShell 5.1 或更高版本
        """
        if sys.platform != 'win32':
            self.logger.critical("自我更新仅支持 Windows 操作系统")
            return False

        try:
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-Command', '$PSVersionTable.PSVersion.Major'],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                self.logger.critical("无法获取 PowerShell 版本信息")
                return False
            major_ver = int(result.stdout.strip())
            if major_ver < 5:
                self.logger.critical(
                    f"PowerShell 版本过低: {major_ver}.x，需要 5.1 或更高版本"
                )
                return False
            self.logger.debug(f"PowerShell 版本: {major_ver}.x，环境检查通过")
        except (ValueError, subprocess.TimeoutExpired) as e:
            self.logger.critical(f"检测 PowerShell 版本失败: {e}")
            return False

        return True

    def check_self_update(self, force: bool = False) -> bool:
        """
        检查并准备自身更新

        Args:
            force: 是否强制更新

        Returns:
            bool: 是否需要退出以完成更新
        """
        self.logger.info("开始检查软件版本...")

        # ── 系统环境检查 ──
        if not self._check_system_environment():
            return False

        if self._is_bundled is None:
            self._is_bundled, self._package_type = detect_package_type()
        if not self._is_bundled:
            self.logger.warning("当前为调试模式，跳过更新检查")
            return False

        try:
            release_info = self._fetch_latest_release()
            if not release_info:
                return False

            latest_version = release_info.get('tag_name', '')
            if not latest_version:
                self.logger.error("未能获取版本号")
                return False

            self.logger.debug(f"远程版本: {latest_version}")

            if not force and is_build_tag(self.current_version):
                self.logger.info("当前为 Build 版本，跳过更新")
                return False

            if not force:
                if version_newer_than(self.current_version, latest_version):
                    self.logger.info(f"检测到新版本: {latest_version}")
                else:
                    cur_tuple = version_to_tuple(self.current_version)
                    lat_tuple = version_to_tuple(latest_version)
                    if cur_tuple and lat_tuple:
                        self.logger.info("当前版本已最新")
                    else:
                        self.logger.error("版本号校验错误，跳过更新")
                    return False
            else:
                self.logger.info(f"强制更新模式，目标版本: {latest_version}")

            existing_state = UpdateState.load()
            if existing_state and existing_state.get("State", "state", fallback="") == "failed_disabled":
                failed_ver = existing_state["new_version"]
                if failed_ver == latest_version:
                    self.logger.info(f"版本 {latest_version} 存在更新失败记录，跳过更新")
                    return False
                self.logger.debug("新版本与失败记录不同，清除失败状态继续")
                existing_state.delete()

            package_type = self._package_type or "Nuitka"
            exe_url, exe_name = self._match_asset(release_info, package_type)
            if not exe_url:
                return False

            cache_dir = Path(self.temp_folder) / "UpdateCache" / "installs" / latest_version
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_dir / f"{self.app_name}-{latest_version}.exe"
            sha_path = cache_dir / f"{self.app_name}-{latest_version}.sha256"

            new_sha256 = self._get_asset_sha256(release_info, exe_name)
            if not new_sha256:
                self.logger.critical("Github API 中未找到 SHA256 校验值，跳过更新")
                return False

            old_sha256 = self._fetch_current_release_sha256(package_type)
            if old_sha256:
                current_exe = get_exe_path()
                actual_current = calculate_sha256(current_exe)
                if actual_current != old_sha256:
                    self.logger.critical("当前程序 SHA256 与 GitHub 记录不一致，放弃更新")
                    self.logger.warning(f"GitHub: {old_sha256}")
                    self.logger.warning(f"本地:   {actual_current}")
                    return False
                self.logger.info("当前版本 SHA256 校验通过")
            else:
                self.logger.warning("未能获取当前版本 SHA256，跳过自身完整性校验")

            sha_path.write_text(new_sha256, encoding='ascii')

            if not self._download_and_verify(tmp_path, sha_path, exe_url,
                                              new_sha256, latest_version):
                return False

            self._replace_executable(tmp_path, sha_path, latest_version,
                                      old_sha256, new_sha256)
            return True
        except requests.RequestException as e:
            self.logger.critical(f"获取 GitHub release 信息失败: {e}")
            return False
        except Exception as e:
            self.logger.critical(f"检查软件更新时出错: {e}")
            return False

    def _download_and_verify(self, tmp_path: Path, sha_path: Path,
                              exe_url: str, new_sha256: str,
                              latest_version: str) -> bool:
        """下载新版本 exe 并校验 SHA256"""
        if tmp_path.exists():
            actual = calculate_sha256(tmp_path)
            if actual == new_sha256:
                self.logger.info(f"缓存文件校验通过，跳过下载: {tmp_path}")
                return True
            self.logger.warning("缓存文件 SHA256 校验失败，将重新下载")
            tmp_path.unlink(missing_ok=True)
            sha_path.unlink(missing_ok=True)

        max_retries = 3
        for attempt in range(max_retries):
            file_name = Path(exe_url).name
            if attempt > 0:
                self.logger.info(f"重试下载更新文件（{attempt + 1}/{max_retries}）: {file_name}")
            else:
                self.logger.info(f"开始下载更新文件: {file_name}")

            if not self._download_func(exe_url, str(tmp_path)):
                self.logger.error("下载失败")
                continue

            actual = calculate_sha256(tmp_path)
            if actual == new_sha256:
                self.logger.info("新版本已下载并校验通过")
                return True
            self.logger.error("SHA256 校验失败，准备重试")
        else:
            self.logger.critical("软件更新下载校验失败，已达到最大重试次数，跳过更新")
            tmp_path.unlink(missing_ok=True)
            sha_path.unlink(missing_ok=True)
            return False

    def _replace_executable(self, tmp_path: Path, sha_path: Path,
                             new_version: str, old_sha256: str,
                             new_sha256: str) -> None:
        """
        准备替换：生成 helper.ps1 / update.ps1 → 写 INI → 启动 PowerShell

        Raises:
            RuntimeError: helper.ps1 启动失败
        """
        current_exe = get_exe_path()
        paths = self._build_update_runtime_paths(current_exe, new_version)
        paths["runtime_dir"].mkdir(parents=True, exist_ok=True)

        shutil.copy2(tmp_path, paths["new_file"])
        self.logger.info(f"新版本已暂存: {paths['new_file']}")

        state = UpdateState(file_path=paths["state_file"])
        state["state"] = "downloaded_verified"
        state["target"] = str(current_exe)
        state["new_file"] = str(paths["new_file"])
        state["backup_file"] = str(paths["backup_file"])
        state["runtime_dir"] = str(paths["runtime_dir"])
        state["helper_ps1"] = str(paths["helper_ps1"])
        state["update_ps1"] = str(paths["update_ps1"])
        state["lock_file"] = str(paths["lock_file"])
        state["old_version"] = self.current_version
        state["new_version"] = new_version
        state["old_sha256"] = old_sha256
        state["new_sha256"] = new_sha256
        state.set("Retry", "retry_count", _get_existing_retry_count(paths["program_dir"]))
        state.set("Retry", "max_retry", "3")
        state.save()

        self._generate_helper_ps1(paths)
        self._generate_update_ps1(paths)
        self.logger.info(f"已生成更新脚本到目录: {paths['runtime_dir']}")

        state.transition("helper_started")

        self.logger.info("启动更新进程...")
        lock_file = paths["lock_file"]
        if lock_file.exists():
            lock_file.unlink()

        proc = subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(paths["helper_ps1"]),
                "-ParentPid", str(os.getpid()),
            ],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW,
        )

        deadline = time.time() + 15
        while time.time() < deadline:
            if lock_file.exists():
                return
            if proc.poll() is not None:
                message = f"启动更新脚本失败：helper.ps1 异常退出，退出码 {proc.returncode}"
                state["last_error"] = message
                state.save()
                raise RuntimeError(message)
            time.sleep(0.1)

        try:
            proc.kill()
        except Exception:
            pass
        message = "启动更新脚本失败：helper.ps1 未在 15 秒内就绪"
        state["last_error"] = message
        state.save()
        raise RuntimeError(message)

    # ── PS1 脚本生成（从 modules/self_updater.py 完整复用） ──

    def _generate_helper_ps1(self, paths: Dict[str, Path]) -> None:
        """
        生成 {app}_Update_Helper.ps1
        """.format(app=self.app_name)
        script_path = paths["helper_ps1"]
        runtime_dir = self._ps1_double_quoted_path(paths["runtime_dir"])
        state_file = self._ps1_double_quoted_path(paths["state_file"])
        log_file = self._ps1_double_quoted_path(paths["log_file"])
        lock_file = self._ps1_double_quoted_path(paths["lock_file"])
        update_ps1 = self._ps1_double_quoted_path(paths["update_ps1"])
        ps1_content = textwrap.dedent(r"""
            <#
            .SYNOPSIS
                __APP___Update_Helper
            .DESCRIPTION
                等待主进程退出 → 调用 update.ps1 替换 → 验证新版 → 提交或回滚
            #>
            param([int]$ParentPid)

            $runtimeDir = "__RUNTIME_DIR__"
            $scriptDir  = $runtimeDir
            $scriptName = Split-Path -Leaf $MyInvocation.MyCommand.Path
            $scriptTag  = ($scriptName -split '_')[-1]
            $lockFile   = "__LOCK_FILE__"

            try { New-Item -Path $lockFile -ItemType File -Force | Out-Null } catch {}

            $stateFile  = "__STATE_FILE__"
            $logFile    = "__LOG_FILE__"
            $updatePs1  = "__UPDATE_PS1__"
        """).replace("__APP__", self.app_name)
        ps1_content = ps1_content.replace("__RUNTIME_DIR__", runtime_dir)
        ps1_content = ps1_content.replace("__LOCK_FILE__", lock_file)
        ps1_content = ps1_content.replace("__STATE_FILE__", state_file)
        ps1_content = ps1_content.replace("__LOG_FILE__", log_file)
        ps1_content = ps1_content.replace("__UPDATE_PS1__", update_ps1)
        ps1_content += generate_common_base_functions_ps1()
        ps1_content += generate_helper_argument_functions_ps1()
        ps1_content += generate_sha256_function_ps1()
        ps1_content += generate_common_state_functions_ps1()
        ps1_content += generate_helper_retry_functions_ps1()
        ps1_content += generate_helper_file_cleanup_functions_ps1()
        ps1_content += generate_move_with_retry_ps1()
        ps1_content += generate_helper_lifecycle_functions_ps1()
        ps1_content += textwrap.dedent(r"""

            try {
                Set-UpdateStatus "helper_started" "helper_started" "更新 Helper 已启动" 10 "INFO"

                if ($ParentPid -gt 0) {
                    Set-UpdateStatus "helper_started" "wait_parent_exit" "等待主程序退出，PID: $ParentPid" 15 "INFO"
                    try { Wait-Process -Id $ParentPid -Timeout 60 -ErrorAction Stop }
                    catch {
                        $p = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
                        if ($p) { throw "parent still alive: $ParentPid" }
                    }
                }

                Set-UpdateStatus "replacing" "run_update_script" "开始执行文件替换脚本" 30 "INFO"
                $updateCode = Start-ProcWait "powershell.exe" @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $updatePs1) 120
                if ($updateCode -ne 0) {
                    Restore-Backup "update.ps1 failed: exit $updateCode"
                }

                Set-UpdateStatus "replacing" "verify_target_hash" "校验替换后的目标文件 SHA256" 60 "INFO"
                $target    = Read-IniValue "Files" "target"
                $newSha256 = Read-IniValue "Version" "new_sha256"
                Assert-NotEmpty "Files.target" $target
                if ($newSha256) {
                    $actual = Get-SHA256 $target
                    if ($actual -ne $newSha256.ToLowerInvariant()) {
                        Restore-Backup "target hash mismatch after replace"
                    }
                }

                Set-UpdateStatus "pending_new_verify" "start_new_exe_verify" "启动新版程序进行自检" 75 "INFO"
                $newVersion = Read-IniValue "Version" "new_version"
                $verifyArgs = @('--self-update-verify')
                if ($newSha256) {
                    $verifyArgs += @('--expected-sha256', $newSha256)
                }
                if ($newVersion) {
                    $verifyArgs += @('--expected-version', $newVersion)
                }
                $verifyCode = Start-ProcWait $target $verifyArgs 60 $true
                if ($verifyCode -ne 0) {
                    Restore-Backup "verify failed: exit $verifyCode"
                }

                Set-UpdateStatus "verified" "start_normal_app" "新版验证通过，启动主程序" 100 "INFO"
                Commit-Update
                Start-NormalAppVisible $target
                exit 0
            } catch {
                Write-Log "ERROR" "helper error: $($_.Exception.Message)"
                Restore-Backup $_.Exception.Message
            }
        """).lstrip("\n")

        script_path.write_text(ps1_content, encoding='utf-8-sig')

    def _generate_update_ps1(self, paths: Dict[str, Path]) -> None:
        """
        生成 {app}_Update.ps1
        """.format(app=self.app_name)
        script_path = paths["update_ps1"]
        runtime_dir = self._ps1_double_quoted_path(paths["runtime_dir"])
        state_file = self._ps1_double_quoted_path(paths["state_file"])
        log_file = self._ps1_double_quoted_path(paths["log_file"])
        ps1_content = textwrap.dedent(r"""
            <#
            .SYNOPSIS
                __APP___Update
            .DESCRIPTION
                替换 app.exe 为新版本：app.exe - app.backup.exe, app.new.exe - app.exe
            #>

            $runtimeDir = "__RUNTIME_DIR__"
            $scriptDir  = $runtimeDir
            $scriptName = Split-Path -Leaf $MyInvocation.MyCommand.Path
            $scriptTag  = ($scriptName -split '_')[-1]
            $stateFile  = "__STATE_FILE__"
            $logFile    = "__LOG_FILE__"
        """).replace("__APP__", self.app_name)
        ps1_content = ps1_content.replace("__RUNTIME_DIR__", runtime_dir)
        ps1_content = ps1_content.replace("__STATE_FILE__", state_file)
        ps1_content = ps1_content.replace("__LOG_FILE__", log_file)
        ps1_content += generate_common_base_functions_ps1() + generate_sha256_function_ps1()
        ps1_content += generate_common_state_functions_ps1() + generate_move_with_retry_ps1() + textwrap.dedent(r"""

            try {
                Set-UpdateStatus "replacing" "read_state" "读取更新状态文件" 35 "INFO"

                $target    = Read-IniValue "Files" "target"
                $newFile   = Read-IniValue "Files" "new_file"
                $backup    = Read-IniValue "Files" "backup_file"
                $newSha256 = Read-IniValue "Version" "new_sha256"

                Assert-NotEmpty "Files.target" $target
                Assert-NotEmpty "Files.new_file" $newFile
                Assert-NotEmpty "Files.backup_file" $backup
                if ($target -eq $newFile -or $target -eq $backup -or $newFile -eq $backup) {
                    throw "invalid file paths: target/new_file/backup_file must be different"
                }

                Set-UpdateStatus "replacing" "check_new_file" "检查新版本文件是否存在: $newFile" 40 "INFO"
                if (!(Test-Path -LiteralPath $newFile)) {
                    throw "new file not found: $newFile"
                }

                if ($newSha256) {
                    Set-UpdateStatus "replacing" "verify_new_file_hash" "校验新版本文件 SHA256" 45 "INFO"
                    $actual = Get-SHA256 $newFile
                    if ($actual -ne $newSha256.ToLowerInvariant()) {
                        throw "new file SHA256 mismatch: expected $newSha256, got $actual"
                    }
                }

                if (Test-Path -LiteralPath $backup) {
                    Set-UpdateStatus "replacing" "remove_old_backup" "删除旧备份文件: $backup" 50 "INFO"
                    Remove-Item -LiteralPath $backup -Force -ErrorAction Stop
                }

                if (Test-Path -LiteralPath $target) {
                    Set-UpdateStatus "replacing" "move_target_to_backup" "备份当前程序: $target -> $backup" 55 "INFO"
                    Move-WithRetry $target $backup 60
                }

                Set-UpdateStatus "replacing" "move_new_to_target" "替换为新版本: $newFile -> $target" 60 "INFO"
                Move-WithRetry $newFile $target 60

                Set-UpdateStatus "replacing" "replace_done" "文件替换完成" 65 "INFO"
                exit 0
            } catch {
                Set-UpdateStatus "failed_disabled" "replace_failed" "文件替换失败: $($_.Exception.Message)" 100 "ERROR"
                Write-Error $_.Exception.Message
                exit 1
            }
        """).lstrip("\n")

        script_path.write_text(ps1_content, encoding='utf-8-sig')

    # ── 静态工具方法 ──

    @staticmethod
    def _ps1_double_quoted_path(path: Path) -> str:
        """将路径转换为 PowerShell 双引号字符串内容。"""
        return str(path).replace('`', '``').replace('"', '`"').replace('$', '`$')

    @staticmethod
    def self_update_verify(expected_sha256: str = "",
                           expected_version: str = "",
                           sha256_calc: Optional[Callable[[Path], str]] = None,
                           version_func: Optional[Callable[[], str]] = None) -> int:
        """
        新版程序健康检查

        Args:
            expected_sha256: 期望的 SHA256（不传则从状态文件读取）
            expected_version: 期望的版本号（不传则从状态文件读取）
            sha256_calc: SHA256 计算函数（不传则使用内建）
            version_func: 获取当前版本号的函数（不传则从状态文件读取）

        Returns:
            0 表示验证通过，非 0 表示失败
        """
        logger = logging.getLogger("SelfUpdater")

        if not expected_sha256:
            state = UpdateState.load()
            if state:
                expected_sha256 = state["new_sha256"]
                expected_version = expected_version or state["new_version"]

        if not expected_sha256:
            logger.critical("未找到 SHA256，无法验证")
            return 1

        calc = sha256_calc or calculate_sha256
        current_exe = get_exe_path()
        actual_sha256 = calc(current_exe)

        if actual_sha256 != expected_sha256:
            logger.critical(
                f"SHA256 不匹配:\n"
                f"GitHub: {expected_sha256}\n"
                f"本地:   {actual_sha256}"
            )
            return 2

        if expected_version and version_func:
            actual_version = version_func()
            if actual_version and actual_version != expected_version:
                logger.critical(
                    f"版本号不匹配:\n"
                    f"GitHub: {expected_version}\n"
                    f"本地:   {actual_version}"
                )
                return 3

        logger.info("新版验证全部通过")
        return 0

    @staticmethod
    def _cleanup_update_residue(logger: Optional[logging.Logger] = None) -> None:
        """
        按状态文件记录的精确路径清理上次成功更新后的残留文件。

        Args:
            logger: 日志记录器
        """
        logger = logger or logging.getLogger("SelfUpdater")
        state = UpdateState.load()
        if not state:
            return

        current_state = state.get("State", "state", fallback="")
        if current_state != "verified":
            return

        logger.info("清理上次更新残留文件...")
        cleanup_files = [
            Path(file_path)
            for file_path in (
                state["backup_file"],
                state["new_file"],
                state["helper_ps1"],
                state["update_ps1"],
                state["lock_file"],
            )
            if file_path
        ]

        for file_path in cleanup_files:
            try:
                if file_path.exists():
                    file_path.unlink()
                    logger.debug(f"已删除残留文件: {file_path}")
            except OSError:
                pass

        runtime_dir = Path(state["runtime_dir"]) if state["runtime_dir"] else None
        if runtime_dir and runtime_dir.exists():
            try:
                runtime_dir.rmdir()
                logger.debug(f"已删除空运行时目录: {runtime_dir}")
            except OSError:
                pass

        # 最后删除状态文件自身
        try:
            state.delete()
        except Exception:
            pass

    @staticmethod
    def clean_update_cache(temp_folder: str, logger: logging.Logger) -> None:
        """
        清理自更新缓存目录 UpdateCache

        Args:
            temp_folder: 临时文件夹路径
            logger: 日志记录器
        """
        cache_dir = Path(temp_folder) / "UpdateCache"
        if cache_dir.exists():
            try:
                shutil.rmtree(cache_dir)
                logger.info("已清理自更新缓存目录")
            except OSError as e:
                logger.warning(f"清理自更新缓存目录失败: {e}")

    @staticmethod
    def rollback(logger: Optional[logging.Logger] = None) -> bool:
        """
        从 INI 状态文件读取 backup_file 路径，恢复旧版

        Returns:
            恢复是否成功
        """
        logger = logger or logging.getLogger("SelfUpdater")
        state = UpdateState.load()
        if not state:
            logger.critical("未找到更新状态文件，无法回滚")
            return False

        backup_file = Path(state["backup_file"])
        target = Path(state["target"])

        if not backup_file.exists():
            logger.critical(f"备份文件不存在: {backup_file}")
            return False

        try:
            if target.exists():
                target.unlink()
            backup_file.rename(target)
            logger.info(f"已回滚: {target}")
            state.transition("rollback_done")
            return True
        except OSError as e:
            logger.critical(f"回滚失败: {e}")
            return False
