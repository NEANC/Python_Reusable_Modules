#!/usr/bin/env python3
# -_- coding: utf-8 -_-

import logging
import time

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import requests

from .progress import create_download_progress_bar, format_ok, format_error

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
DOWNLOAD_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
DOWNLOAD_RETRIES = 3
CHUNK_SIZE = 128 * 1024
USER_AGENT = 'M9A-Update-Assistant'


@dataclass(frozen=True)
class DownloadMetadata:
    """下载元数据。"""

    total_size: int
    supports_range: bool


@dataclass(frozen=True)
class DownloadSegment:
    """下载分段闭区间。"""

    index: int
    start: int
    end: int

    @property
    def length(self) -> int:
        """返回分段字节长度。"""
        return self.end - self.start + 1


class NetworkSpeedMeter:
    """统计网络层 chunk 到达速度。"""

    def __init__(self, time_func=time.monotonic):
        """初始化速度统计器。"""
        self.time_func = time_func
        self.started_at = time_func()
        self.samples = deque()
        self.total_bytes = 0

    def update(self, byte_count: int) -> float:
        """记录网络收到的字节数并返回 bytes/s。"""
        now = self.time_func()
        self.total_bytes += byte_count
        self.samples.append((now, byte_count))
        while self.samples and now - self.samples[0][0] > 1:
            self.samples.popleft()
        window_bytes = sum(size for _, size in self.samples)
        window_seconds = now - self.samples[0][0] if self.samples else 0
        if window_seconds > 0:
            return window_bytes / window_seconds
        elapsed = max(now - self.started_at, 0.001)
        return self.total_bytes / elapsed


class DownloadManager:
    """下载管理器，负责文件下载与缓存检查"""

    def __init__(self, proxy: str, temp_folder: str, logger: logging.Logger,
                 download_threads: int = 4):
        """
        初始化下载管理器

        Args:
            proxy: 代理地址
            temp_folder: 临时文件夹路径
            logger: 日志记录器
            download_threads: 下载线程数
        """
        self.proxy = proxy
        self.temp_folder = temp_folder
        self.logger = logger
        self.download_threads = download_threads

    def _build_proxies(self):
        """构建 requests 代理参数。"""
        if not self.proxy:
            return None
        return {'http': self.proxy, 'https': self.proxy}

    def _get_download_metadata(self, session, url: str) -> DownloadMetadata:
        """通过 HEAD 获取下载元数据。"""
        try:
            with session.head(
                url,
                headers={'User-Agent': USER_AGENT},
                timeout=DOWNLOAD_TIMEOUT,
                proxies=self._build_proxies(),
                allow_redirects=True,
            ) as response:
                response.raise_for_status()
                try:
                    total_size = int(response.headers.get('Content-Length', '0'))
                except ValueError:
                    total_size = 0
                supports_range = response.headers.get('Accept-Ranges', '').lower() == 'bytes'
                return DownloadMetadata(
                    total_size=max(total_size, 0),
                    supports_range=supports_range,
                )
        except requests.RequestException as exc:
            self.logger.debug(f"HEAD 探测失败，降级单线程下载: {exc}")
            return DownloadMetadata(total_size=0, supports_range=False)

    def _split_segments(self, total_size: int, threads: int) -> list[DownloadSegment]:
        """将文件大小拆分为闭区间分段。"""
        segment_count = min(max(threads, 1), total_size)
        base_size, remainder = divmod(total_size, segment_count)
        segments = []
        start = 0
        for index in range(segment_count):
            length = base_size + (1 if index < remainder else 0)
            end = start + length - 1
            segments.append(DownloadSegment(index, start, end))
            start = end + 1
        return segments

    def _format_speed(self, bytes_per_second: float) -> str:
        """格式化网络下载速度。"""
        if bytes_per_second < 1024:
            return f"{bytes_per_second:.2f}B/s"
        if bytes_per_second < 1024 * 1024:
            return f"{bytes_per_second / 1024:.2f}KiB/s"
        return f"{bytes_per_second / 1024 / 1024:.2f}MiB/s"

    def _extract_total_size_from_get_response(self, response, existing_size: int) -> int:
        """从 GET 响应头推导完整文件大小。"""
        content_range = response.headers.get('Content-Range', '')
        if content_range.startswith('bytes ') and '/' in content_range:
            total_part = content_range.rsplit('/', 1)[1]
            if total_part.isdigit():
                return int(total_part)
        content_length = response.headers.get('Content-Length')
        if response.status_code == 200 and content_length and content_length.isdigit():
            return int(content_length)
        return 0

    def _update_progress(self, pbar, progress_lock: Lock, speed_meter: NetworkSpeedMeter,
                         byte_count: int) -> None:
        """更新进度条和网络速度。"""
        with progress_lock:
            speed = speed_meter.update(byte_count)
            pbar.update(byte_count)
            pbar.download_rate_fmt = self._format_speed(speed)
            pbar.refresh()

    def _download_single_threaded(self, session, url: str, target_path: Path,
                                  total_size: int, pbar, speed_meter: NetworkSpeedMeter,
                                  progress_lock: Lock) -> bool:
        """单线程下载，支持续传和重试。

        Args:
            session: requests Session。
            url: 下载 URL。
            target_path: 目标文件路径。
            total_size: 已知总大小（0 表示未知）。
            pbar: tqdm 进度条实例。
            speed_meter: 网速统计器。
            progress_lock: 进度更新锁。

        Returns:
            bool: 是否下载成功。
        """
        target = Path(target_path)

        # GET 可从响应头推导完整总大小时用于最终校验
        known_total = total_size

        # 已知总大小且文件已完整
        if known_total > 0 and target.exists() and target.stat().st_size == known_total:
            return True

        existing_size = 0
        if target.exists():
            existing_size = target.stat().st_size

        # 本地文件大于已知总大小，覆盖重下
        if known_total > 0 and existing_size >= known_total:
            existing_size = 0

        headers = {'User-Agent': USER_AGENT}
        if existing_size > 0:
            headers['Range'] = f'bytes={existing_size}-'

        for attempt in range(DOWNLOAD_RETRIES):
            try:
                with session.get(
                    url,
                    headers=headers,
                    timeout=DOWNLOAD_TIMEOUT,
                    proxies=self._build_proxies(),
                    stream=True,
                ) as response:

                    if response.status_code == 206:
                        response.raise_for_status()
                        # 从 Content-Range 推导真实 total
                        real_total = self._extract_total_size_from_get_response(
                            response, existing_size,
                        )
                        if real_total > 0:
                            known_total = real_total
                            with progress_lock:
                                pbar.total = real_total
                                pbar.refresh()
                        # 续传追加
                        with open(target, 'ab') as f:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                                    self._update_progress(pbar, progress_lock, speed_meter, len(chunk))

                        # C1: 使用可变的 known_total 做校验，而非固定入参 total_size
                        if known_total == 0:
                            return target.exists() and target.stat().st_size > 0
                        if target.stat().st_size != known_total:
                            existing_size = target.stat().st_size
                            headers['Range'] = f'bytes={existing_size}-'
                            continue
                        return True

                    elif response.status_code == 200:
                        response.raise_for_status()
                        # 覆盖从头下载（进度条复位需在锁保护下）
                        content_length = response.headers.get('Content-Length')
                        with progress_lock:
                            pbar.n = 0
                            if content_length and content_length.isdigit():
                                known_total = int(content_length)
                                pbar.total = known_total
                            pbar.refresh()

                        with open(target, 'wb') as f:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                                    self._update_progress(pbar, progress_lock, speed_meter, len(chunk))

                        # C1: 使用可变的 known_total 做校验
                        if known_total == 0:
                            return target.exists() and target.stat().st_size > 0
                        if target.stat().st_size != known_total:
                            existing_size = target.stat().st_size
                            headers['Range'] = f'bytes={existing_size}-'
                            continue
                        return True

                    elif response.status_code == 416:
                        # I2: 416 进入重试（规格要求），不直接返回 False
                        # 文件完整时直接成功
                        if known_total > 0 and target.exists() and target.stat().st_size == known_total:
                            return True
                        # 不完整 → 记录失败并进入重试循环
                        self.logger.debug(
                            f"下载尝试 {attempt + 1}: 416 Range Not Satisfiable，"
                            f"文件大小={target.stat().st_size if target.exists() else 0}，"
                            f"已知总大小={known_total}",
                        )

                    else:
                        response.raise_for_status()

            except requests.RequestException as exc:
                self.logger.debug(f"下载尝试 {attempt + 1} 失败: {exc}")

            # 重试前重新读取文件大小，更新 Range 偏移
            existing_size = target.stat().st_size if target.exists() else 0
            if known_total > 0 and existing_size >= known_total:
                existing_size = 0
            if existing_size > 0:
                headers['Range'] = f'bytes={existing_size}-'
            elif 'Range' in headers:
                del headers['Range']

            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(1)

        return False

    def _get_part_path(self, save_path: Path, index: int) -> Path:
        """返回分段临时文件路径。

        Args:
            save_path: 目标文件路径。
            index: 分段索引。

        Returns:
            Path: part 文件路径。
        """
        return Path(f"{save_path}.part{index}")

    def _get_valid_part_size(self, save_path: Path, segment: DownloadSegment) -> int:
        """判断 part 文件有效性并返回有效字节数。

        Args:
            save_path: 目标文件路径。
            segment: 下载分段。

        Returns:
            int: 0 表示需重新下载，-1 表示已完成，
                正数表示已有字节数（用于续传）。
        """
        part_path = self._get_part_path(save_path, segment.index)
        if not part_path.exists():
            return 0
        size = part_path.stat().st_size
        if size < segment.length:
            return size
        if size == segment.length:
            return -1
        # size > segment.length，异常情况，删除重下
        part_path.unlink()
        return 0

    def _content_range_matches(self, segment: DownloadSegment, request_start: int,
                               response_headers: dict) -> bool:
        """校验响应 Content-Range 与请求区间是否一致。

        Args:
            segment: 下载分段。
            request_start: 请求的起始字节。
            response_headers: 响应头字典。

        Returns:
            bool: Content-Range 是否匹配。
        """
        content_range = response_headers.get('Content-Range', '')
        if not content_range.startswith('bytes '):
            return False
        try:
            range_part = content_range.split(' ', 1)[1].split('/')[0]
            start_str, end_str = range_part.split('-')
            resp_start = int(start_str)
            resp_end = int(end_str)
        except (ValueError, IndexError):
            return False
        return resp_start == request_start and resp_end == segment.end

    def _download_part(self, session, url: str, save_path: Path, segment: DownloadSegment,
                       pbar, speed_meter: NetworkSpeedMeter, progress_lock: Lock) -> bool:
        """下载单个分段到 part 文件，内部执行 3 次重试。

        Args:
            session: 独立 requests Session。
            url: 下载 URL。
            save_path: 目标文件路径（用于生成 part 路径）。
            segment: 下载分段。
            pbar: tqdm 进度条实例。
            speed_meter: 网速统计器。
            progress_lock: 进度更新锁。

        Returns:
            bool: 分段是否下载成功。
        """
        valid_size = self._get_valid_part_size(save_path, segment)
        if valid_size == -1:
            return True

        range_start = segment.start + valid_size

        for attempt in range(DOWNLOAD_RETRIES):
            headers = {'User-Agent': USER_AGENT}
            headers['Range'] = f'bytes={range_start}-{segment.end}'

            try:
                with session.get(
                    url,
                    headers=headers,
                    timeout=DOWNLOAD_TIMEOUT,
                    proxies=self._build_proxies(),
                    stream=True,
                ) as response:

                    if response.status_code == 206:
                        if not self._content_range_matches(segment, range_start, response.headers):
                            part_path = self._get_part_path(save_path, segment.index)
                            if part_path.exists():
                                if valid_size > 0:
                                    with progress_lock:
                                        pbar.n = max(0, pbar.n - valid_size)
                                        pbar.refresh()
                                part_path.unlink()
                            range_start = segment.start
                            valid_size = 0
                            continue

                        response.raise_for_status()

                        part_path = self._get_part_path(save_path, segment.index)
                        mode = 'ab' if range_start > segment.start else 'wb'
                        with open(part_path, mode) as f:
                            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                                if chunk:
                                    f.write(chunk)
                                    self._update_progress(pbar, progress_lock, speed_meter, len(chunk))

                        part_size = part_path.stat().st_size
                        if part_size == segment.length:
                            return True
                        # 写入后 part 不完整，继续重试续传
                        valid_size = part_size
                        range_start = segment.start + valid_size
                        continue

                    elif response.status_code == 200:
                        continue

                    elif response.status_code == 416:
                        part_path = self._get_part_path(save_path, segment.index)
                        if part_path.exists() and part_path.stat().st_size == segment.length:
                            return True
                        # I1: 416 不完整时不删除 part，进入重试
                        valid_size = self._get_valid_part_size(save_path, segment)
                        if valid_size == -1:
                            return True
                        range_start = segment.start + max(valid_size, 0)
                        continue

                    else:
                        response.raise_for_status()

            except requests.RequestException as exc:
                self.logger.debug(f"分段 {segment.index} 下载尝试 {attempt + 1} 失败: {exc}")

            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(1)

        return False

    def _merge_parts(self, save_path: Path, segments: list[DownloadSegment]) -> bool:
        """按顺序合并所有 part 文件（调用方负责校验成功后删除）。

        Args:
            save_path: 目标文件路径。
            segments: 下载分段列表。

        Returns:
            bool: 合并是否成功。
        """
        try:
            with open(save_path, 'wb') as target:
                for segment in segments:
                    part_path = self._get_part_path(save_path, segment.index)
                    with open(part_path, 'rb') as part:
                        while True:
                            chunk = part.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            target.write(chunk)
            return True
        except Exception:
            return False

    def _download_multithreaded(self, url: str, target_path: Path, total_size: int,
                                pbar, speed_meter: NetworkSpeedMeter,
                                progress_lock: Lock) -> bool:
        """多线程分段下载协调器。

        Args:
            url: 下载 URL。
            target_path: 目标文件路径。
            total_size: 文件总大小。
            pbar: tqdm 进度条实例。
            speed_meter: 网速统计器。
            progress_lock: 进度更新锁。

        Returns:
            bool: 是否全部下载成功。
        """
        segments = self._split_segments(total_size, self.download_threads)

        sessions = [requests.Session() for _ in segments]
        try:
            with ThreadPoolExecutor(max_workers=len(segments)) as executor:
                futures = {}
                for i, segment in enumerate(segments):
                    future = executor.submit(
                        self._download_part,
                        sessions[i],
                        url,
                        target_path,
                        segment,
                        pbar,
                        speed_meter,
                        progress_lock,
                    )
                    futures[future] = segment

                for future in as_completed(futures):
                    if not future.result():
                        return False

            if not self._merge_parts(target_path, segments):
                return False

            if target_path.exists() and target_path.stat().st_size == total_size:
                for segment in segments:
                    part_path = self._get_part_path(target_path, segment.index)
                    if part_path.exists():
                        part_path.unlink()
                return True
            return False
        finally:
            for session in sessions:
                session.close()

    def _get_existing_bytes(self, target_path: Path, total_size: int) -> int:
        """获取进度条初始已完成字节数。

        Args:
            target_path: 目标文件路径。
            total_size: 已知总大小（0 表示未知）。

        Returns:
            int: 已完成字节数。
        """
        if not target_path.exists():
            return 0
        current_size = target_path.stat().st_size
        if total_size > 0 and current_size > total_size:
            return 0
        return current_size

    def download_file_with_progress(self, url: str, save_path: str) -> bool:
        """下载文件并显示进度条。

        Args:
            url: 下载 URL。
            save_path: 保存路径。

        Returns:
            bool: 操作是否成功。
        """
        file_name = Path(url).name
        self.logger.info(f"开始下载文件: {file_name}")
        self.logger.debug(f"下载 URL: {url}")

        pbar = None
        try:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)

            metadata_session = None
            download_session = None
            try:
                metadata_session = requests.Session()
                metadata = self._get_download_metadata(metadata_session, url)
            finally:
                if metadata_session is not None:
                    metadata_session.close()

            target = Path(save_path)
            use_multithread = (
                self.download_threads >= 2
                and metadata.total_size > 0
                and metadata.supports_range
            )

            # C2: 多线程模式从 part 文件累计已完成字节
            if use_multithread:
                existing_bytes = 0
                segments = self._split_segments(metadata.total_size, self.download_threads)
                for segment in segments:
                    valid_size = self._get_valid_part_size(target, segment)
                    if valid_size == -1:
                        existing_bytes += segment.length
                    elif valid_size > 0:
                        existing_bytes += valid_size
            else:
                existing_bytes = self._get_existing_bytes(target, metadata.total_size)

            pbar_total = metadata.total_size if metadata.total_size > 0 else max(existing_bytes, 1)
            pbar = create_download_progress_bar(
                total=pbar_total,
                desc=f"下载 {file_name}",
            )
            if existing_bytes > 0:
                pbar.n = existing_bytes
                pbar.refresh()

            speed_meter = NetworkSpeedMeter()
            progress_lock = Lock()

            if use_multithread:
                success = self._download_multithreaded(
                    url,
                    target,
                    metadata.total_size,
                    pbar,
                    speed_meter,
                    progress_lock,
                )
            else:
                try:
                    download_session = requests.Session()
                    success = self._download_single_threaded(
                        download_session,
                        url,
                        target,
                        metadata.total_size,
                        pbar,
                        speed_meter,
                        progress_lock,
                    )
                finally:
                    if download_session is not None:
                        download_session.close()

            if success:
                downloaded_size = target.stat().st_size
                if pbar is not None:
                    pbar.close()
                    pbar = None
                print(format_ok("下载", file_name, save_path, downloaded_size))
                self.logger.debug(
                    f"下载完成，文件大小: {downloaded_size / (1024 * 1024):.2f} MB，"
                    f"保存路径: {save_path}"
                )
                return True
            else:
                print(format_error(f"下载 {file_name}", "下载失败"))
                return False
        except Exception as e:
            self.logger.error(f"下载文件时发生错误: {e}")
            print(format_error(f"下载 {file_name}", str(e)))
            return False
        finally:
            if pbar is not None:
                pbar.close()
