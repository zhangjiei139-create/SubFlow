import argparse
import concurrent.futures
import contextlib
import contextvars
import hashlib
import http.client
import importlib
import json
import locale
import os
import re
import subprocess
import threading
import tempfile
import time
import traceback
import socket
import shutil
import sys
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

import ai_runtime
import chinese_script_converter
import subtitle_languages
import subtitle_text_formats
import mkv_fast_mux


MKVMERGE = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
MKVEXTRACT = r"C:\Program Files\MKVToolNix\mkvextract.exe"
FFMPEG = r"tools\ffmpeg\bin\ffmpeg.exe"
OLLAMA_HOST = "http://127.0.0.1:11434"
OLLAMA_EXE = "tools\\ollama\\ollama.exe"
DEFAULT_MODEL = "qwen3:8b"

_OLLAMA_STATE_LOCK = threading.RLock()
_OLLAMA_STATE_CHANGED = threading.Condition(_OLLAMA_STATE_LOCK)
_OLLAMA_LEASE_LOCK = _OLLAMA_STATE_LOCK
_OLLAMA_START_LOCK = threading.Lock()
_OLLAMA_UNLOAD_LOCK = threading.Lock()
_OLLAMA_INFERENCE_GATE = threading.Semaphore(1)
_OLLAMA_LEASES = 0
_OLLAMA_ACTIVE_REQUESTS = 0
_OLLAMA_STOPPING = False
_OLLAMA_SERVER_PROCESS: subprocess.Popen | None = None
_OLLAMA_SERVER_HOST: str | None = None
_OLLAMA_DIAGNOSTIC_LOCK = threading.Lock()
_OLLAMA_DIAGNOSTIC_MAX_BYTES = 2 * 1024 * 1024
OLLAMA_STREAM_IDLE_SECONDS = 60
OLLAMA_REQUEST_MAX_SECONDS = 180
OLLAMA_CONTEXT_SIZE = 4096
_OLLAMA_BATCH_STREAM_RECOVERY = contextvars.ContextVar('ollama_batch_stream_recovery', default=False)
_OLLAMA_SPLIT_BUDGET = contextvars.ContextVar('ollama_split_budget', default=None)
TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
SECONV = "tools\\seconv\\seconv.exe"

OCR_LANGUAGE_MAP = {
    "en": "eng", "eng": "eng",
    "zh": "chi_sim", "zh-cn": "chi_sim", "zh-hans": "chi_sim", "chi": "chi_sim", "zho": "chi_sim", "cmn": "chi_sim",
    "zh-tw": "chi_tra", "zh-hk": "chi_tra", "zh-hant": "chi_tra",
    "es": "spa", "spa": "spa",
    "ja": "jpn", "jpn": "jpn",
    "ko": "kor", "kor": "kor",
    "fr": "fra", "fre": "fra", "fra": "fra",
    "de": "deu", "ger": "deu", "deu": "deu",
    "pt": "por", "por": "por",
    "ru": "rus", "rus": "rus",
    "hi": "hin", "hin": "hin",
    "bg": "bul", "bul": "bul",
    "hr": "hrv", "hrv": "hrv",
    "cs": "ces", "cze": "ces", "ces": "ces",
    "da": "dan", "dan": "dan",
    "nl": "nld", "dut": "nld", "nld": "nld",
    "et": "est", "est": "est",
    "fi": "fin", "fin": "fin",
    "it": "ita", "ita": "ita",
    "lv": "lav", "lav": "lav",
    "lt": "lit", "lit": "lit",
    "no": "nor", "nor": "nor",
    "ro": "ron", "rum": "ron", "ron": "ron",
    "sl": "slv", "slv": "slv",
    "sv": "swe", "swe": "swe",
    "th": "tha", "tha": "tha",
    "tr": "tur", "tur": "tur",
    "ar": "ara", "ara": "ara",
    "he": "heb", "heb": "heb",
    "id": "ind", "ind": "ind",
    "pl": "pol", "pol": "pol",
    "uk": "ukr", "ukr": "ukr",
    "vi": "vie", "vie": "vie",
}

LANGUAGES = {
    "zh-CN": ("chi", "Simplified Chinese", "简体中文"),
    "zh-TW": ("chi", "Traditional Chinese", "繁体中文"),
    "en": ("eng", "English", "英文"),
    "es": ("spa", "Spanish", "西班牙语"),
    "ja": ("jpn", "Japanese", "日语"),
    "ko": ("kor", "Korean", "韩语"),
    "fr": ("fre", "French", "法语"),
    "de": ("ger", "German", "德语"),
    "pt": ("por", "Portuguese", "葡萄牙语"),
    "ru": ("rus", "Russian", "俄语"),
    "hi": ("hin", "Hindi", "印地语"),
}

_NON_CJK_TRANSLATION_TARGETS = frozenset(LANGUAGES) - {"zh-CN", "zh-TW", "ja", "ko"}


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resolve_config_path(value: str) -> str:
    if not value:
        return value
    expanded = os.path.expandvars(value)
    path = Path(expanded)
    if path.is_absolute():
        return str(path)
    root_candidate = app_base_dir() / path
    if root_candidate.exists():
        return str(root_candidate)
    internal_candidate = app_base_dir() / "_internal" / path
    if internal_candidate.exists():
        return str(internal_candidate)
    development_candidate = (
        app_base_dir()
        / "_qt_fix_build"
        / "dist"
        / "SubtitleTrackTool-ProMax"
        / "_internal"
        / path
    )
    if development_candidate.exists():
        return str(development_candidate)
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SubFlow"
    installed_candidates = (
        program_files / path,
        program_files / "_internal" / path,
    )
    for installed_candidate in installed_candidates:
        if installed_candidate.exists():
            return str(installed_candidate)
    return str(root_candidate)


def load_tool_config() -> dict:
    candidates = [app_base_dir() / "product_config.json"]
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", app_base_dir())) / "product_config.json"
        if bundled not in candidates:
            candidates.append(bundled)
    for config_path in candidates:
        if not config_path.exists():
            continue
        try:
            return json.loads(config_path.read_text(encoding="utf-8")).get("tools", {})
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def find_ollama_exe(configured: str) -> str:
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    found = shutil.which("ollama")
    if found:
        candidates.append(found)
    candidates.append(str(app_base_dir() / "tools" / "ollama" / "ollama.exe"))
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(str(Path(local_appdata) / "Programs" / "Ollama" / "ollama.exe"))
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(str(Path(program_files) / "SubFlow" / "tools" / "ollama" / "ollama.exe"))
        candidates.append(str(Path(program_files) / "Ollama" / "ollama.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return configured


def _tool_path_candidates(configured: str, fallback: str, exe_name: str) -> list[str]:
    candidates: list[str] = []

    def add(value: str | Path | None) -> None:
        if not value:
            return
        candidate = str(value)
        if candidate not in candidates:
            candidates.append(candidate)

    if configured:
        expanded = os.path.expandvars(configured)
        configured_path = Path(expanded)
        if configured_path.is_absolute():
            add(configured_path)
        else:
            add(resolve_config_path(configured))
            if getattr(sys, "frozen", False):
                add(Path(getattr(sys, "_MEIPASS", app_base_dir())) / configured_path)
    add(resolve_config_path(fallback))
    found = shutil.which(exe_name)
    add(found)
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        program_files = os.environ.get(key)
        if program_files:
            add(Path(program_files) / "MKVToolNix" / exe_name)
    return candidates


def find_existing_tool(configured: str, fallback: str, exe_name: str) -> str:
    candidates = _tool_path_candidates(configured, fallback, exe_name)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return candidates[0] if candidates else configured


_DEFAULT_MKVMERGE = MKVMERGE
_DEFAULT_MKVEXTRACT = MKVEXTRACT
_DEFAULT_FFMPEG = FFMPEG
_DEFAULT_TESSERACT = TESSERACT
_DEFAULT_SECONV = SECONV
_TOOL_CONFIG = load_tool_config()
MKVMERGE = find_existing_tool(_TOOL_CONFIG.get("mkvmerge", ""), _DEFAULT_MKVMERGE, "mkvmerge.exe")
MKVEXTRACT = find_existing_tool(_TOOL_CONFIG.get("mkvextract", ""), _DEFAULT_MKVEXTRACT, "mkvextract.exe")
FFMPEG = find_existing_tool(_TOOL_CONFIG.get("ffmpeg", ""), _DEFAULT_FFMPEG, "ffmpeg.exe")
TESSERACT = find_existing_tool(_TOOL_CONFIG.get("tesseract", ""), _DEFAULT_TESSERACT, "tesseract.exe")
SECONV = find_existing_tool(_TOOL_CONFIG.get("seconv", ""), _DEFAULT_SECONV, "seconv.exe")
OLLAMA_EXE = resolve_config_path(_TOOL_CONFIG.get("ollama", OLLAMA_EXE))
OLLAMA_EXE = find_ollama_exe(OLLAMA_EXE)
OLLAMA_HOST = _TOOL_CONFIG.get("ollama_host", OLLAMA_HOST)
DEFAULT_MODEL = _TOOL_CONFIG.get("ollama_model", DEFAULT_MODEL)
PROCESS_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
EXTERNAL_PROCESS_CREATION_FLAGS = PROCESS_CREATION_FLAGS
if os.name == "nt":
    EXTERNAL_PROCESS_CREATION_FLAGS |= subprocess.BELOW_NORMAL_PRIORITY_CLASS

ASS_TAG_RE = re.compile(r"\{[^}]*\}")
SRT_TIME_RE = re.compile(
    r"(?P<start>\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(?P<end>\d+:\d{2}:\d{2}[,.]\d{1,3})"
)


@dataclass
class Track:
    id: int
    type: str
    codec: str
    language: str
    name: str
    default: bool
    forced: bool
    text_subtitle: bool
    pgs_subtitle: bool
    vobsub_subtitle: bool
    # Matroska writes these statistics into the same `mkvmerge -J` response
    # that is already used for analysis.  Keeping them here lets callers spot
    # an obviously partial subtitle without extracting the track or reading
    # the movie again.
    statistics_frame_count: int | None = None
    statistics_duration_seconds: float | None = None
    statistics_byte_count: int | None = None
    commentary: bool = False
    visual_impaired: bool = False
    original: bool = False
    channels: int | None = None


@dataclass
class SubtitleEvent:
    start: str
    end: str
    text: str


class CancelledError(Exception):
    pass


def log_noop(message: str) -> None:
    _ = message


def check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("用户已停止处理。")


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=PROCESS_CREATION_FLAGS,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def decode_external_output(data: bytes | str | None) -> str:
    """Decode console output without turning a local-codepage path into mojibake."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    encodings = ["utf-8-sig"]
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)
    if os.name == "nt":
        encodings.append("mbcs")
    encodings.append("gb18030")
    tried: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _mkvtoolnix_warning_result(tool_name: str, returncode: int) -> bool:
    # MKVToolNix exit code 1 means that output completed with warnings. The
    # caller still validates the generated subtitle or media before publishing.
    return returncode == 1 and tool_name.lower() in {"mkvmerge.exe", "mkvextract.exe"}


def _mkvmerge_process_metrics(process, *, _kernel32=None) -> dict | None:
    """Read completed-process counters without opening, closing or waiting on it."""
    try:
        if _kernel32 is None and os.name != "nt":
            return None
        handle_value = getattr(process, "_handle", None)
        # CPython's Windows Popen owns this Handle (an int subclass). Never
        # borrow arbitrary objects or take ownership of the native handle.
        if isinstance(handle_value, bool) or not isinstance(handle_value, int) or handle_value <= 0:
            return None
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64) for name in (
                    "read_operations", "write_operations", "other_operations",
                    "read_bytes", "write_bytes", "other_bytes",
                )
            ]

        kernel32 = _kernel32 if _kernel32 is not None else ctypes.WinDLL("kernel32", use_last_error=True)
        get_times = kernel32.GetProcessTimes
        get_times.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(FileTime)] * 4
        get_times.restype = ctypes.c_int
        get_io = kernel32.GetProcessIoCounters
        get_io.argtypes = [ctypes.c_void_p, ctypes.POINTER(IoCounters)]
        get_io.restype = ctypes.c_int
        handle = ctypes.c_void_p(handle_value)
        creation, exit_time, kernel_time, user_time = (FileTime() for _ in range(4))
        result = {"cpu_seconds": None, "read_bytes": None, "write_bytes": None}
        if get_times(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                     ctypes.byref(kernel_time), ctypes.byref(user_time)):
            kernel_ticks = (kernel_time.high << 32) | kernel_time.low
            user_ticks = (user_time.high << 32) | user_time.low
            result["cpu_seconds"] = (kernel_ticks + user_ticks) / 10_000_000
        counters = IoCounters()
        if get_io(handle, ctypes.byref(counters)):
            result["read_bytes"] = int(counters.read_bytes)
            result["write_bytes"] = int(counters.write_bytes)
        return result if any(value is not None for value in result.values()) else None
    except Exception:
        # Diagnostics must never turn a successful remux into a failure.
        return None


_MKVMERGE_PROGRESS_INTERVAL_SECONDS = 5.0


def _system_cpu_times(*, _kernel32=None) -> tuple[int, int] | None:
    """Return cheap cumulative idle/total ticks; diagnostics never start a helper."""
    try:
        if _kernel32 is None and os.name != "nt":
            return None
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

        kernel32 = _kernel32 if _kernel32 is not None else ctypes.WinDLL("kernel32", use_last_error=True)
        get_times = kernel32.GetSystemTimes
        get_times.argtypes = [ctypes.POINTER(FileTime)] * 3
        get_times.restype = ctypes.c_int
        idle, kernel, user = (FileTime() for _ in range(3))
        if not get_times(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = lambda value: (value.high << 32) | value.low
        return ticks(idle), ticks(kernel) + ticks(user)
    except Exception:
        return None


class _MkvmergeProgress:
    """Read actual tool percentages without deriving progress from process I/O."""

    _pattern = re.compile(rb"#GUI#progress\s+(\d{1,3})%")

    def __init__(self, started: float, input_size: int):
        self.started = started
        self.input_size = input_size
        self.next_report = started + _MKVMERGE_PROGRESS_INTERVAL_SECONDS
        self._lock = threading.Lock()
        self._tail = b""
        self._latest: int | None = None
        self._cpu_times = _system_cpu_times()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            data = self._tail + chunk
            for match in self._pattern.finditer(data):
                percent = int(match[1])
                if 0 <= percent <= 100 and (self._latest is None or percent > self._latest):
                    self._latest = percent
            # Keep only enough for a progress record split between pipe reads.
            # CR-only output and unterminated diagnostics cannot grow this buffer.
            self._tail = data[-256:]

    def report(self, log: Callable[[str], None]) -> None:
        now = time.monotonic()
        if now < self.next_report:
            return
        self.next_report = now + _MKVMERGE_PROGRESS_INTERVAL_SECONDS
        elapsed = max(0.001, now - self.started)
        with self._lock:
            percent = self._latest
        progress = "等待工具报告百分比" if percent is None else f"进度 {percent}%"
        message = f"mkvmerge 封装{progress}；已用 {elapsed:.1f} 秒"
        if percent is not None and percent > 0 and self.input_size:
            rate = self.input_size * percent / 100 / elapsed / (1024 ** 2)
            message += f"；估算处理速率 {rate:.1f} MiB/s（按工具进度和主输入大小估算，非磁盘实测速率）"
        cpu_times = _system_cpu_times()
        if cpu_times is not None and self._cpu_times is not None:
            idle = cpu_times[0] - self._cpu_times[0]
            total = cpu_times[1] - self._cpu_times[1]
            if total > 0 and 0 <= idle <= total:
                busy = 100 * (1 - idle / total)
                message += f"；系统 CPU {busy:.0f}%"
                if busy >= 90:
                    message += "（繁忙，可能拖慢封装）"
        self._cpu_times = cpu_times
        log(message + "。")


class _MkvmergeProgressReader:
    """Let Windows communicate() retain pipe ownership while observing chunks."""

    def __init__(self, stream, progress: _MkvmergeProgress):
        self._stream = stream
        self._progress = progress

    def read(self, size=-1):
        if size >= 0:
            data = self._stream.read(size)
            self._progress.feed(data)
            return data
        chunks = []
        # Popen's Windows reader normally calls read() once, blocking until EOF.
        # read1() exposes each available chunk but keeps the same complete result
        # and the same communicate()/tree-cancellation cleanup responsibility.
        while True:
            chunk = self._stream.read1(65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            self._progress.feed(chunk)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def run_command(
    args: list[str],
    log: Callable[[str], None] = log_noop,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    *,
    process_monitor=None,
) -> subprocess.CompletedProcess:
    command_started = time.monotonic()
    tool_name = Path(args[0]).name or args[0]
    log(f"执行外部工具：{tool_name}")
    mkvmerge_input_size = 0
    if tool_name.lower() == "mkvmerge.exe" and "-o" in args:
        try:
            output_index = args.index("-o") + 1
            output_path = Path(args[output_index]).resolve() if output_index < len(args) else None
            input_files = []
            for value in args[1:]:
                candidate = Path(value)
                if candidate.is_file() and (output_path is None or candidate.resolve() != output_path):
                    input_files.append(candidate)
            if input_files:
                mkvmerge_input_size = max(candidate.stat().st_size for candidate in input_files)
                log(f"mkvmerge 封装开始：主输入文件 {mkvmerge_input_size / (1024 ** 3):.2f} GiB。")
        except OSError:
            mkvmerge_input_size = 0
    is_mkvmerge_mux = tool_name.lower() == "mkvmerge.exe" and "-o" in args
    command_args = list(args)
    if is_mkvmerge_mux and "--gui-mode" not in command_args:
        command_args.insert(1, "--gui-mode")
    def monitor_call(method, *values):
        try:
            return getattr(process_monitor, method)(*values)
        except Exception as exc:
            # Observation and optional scheduling must not replace a tool's
            # real success, failure or cancellation with a diagnostic error.
            try:
                log(f"音频准备调度观测失败（{method}）：{type(exc).__name__}: {exc}")
            except Exception:
                pass
            return None

    creation_flags = EXTERNAL_PROCESS_CREATION_FLAGS
    if process_monitor is not None:
        observed_flags = monitor_call("creation_flags", creation_flags)
        if isinstance(observed_flags, int) and not isinstance(observed_flags, bool):
            creation_flags = observed_flags
    try:
        process = subprocess.Popen(
            command_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
            cwd=cwd,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"找不到外部工具：{args[0]}。请安装 MKVToolNix/FFmpeg，或在 product_config.json 里配置正确路径。"
        ) from exc
    if process_monitor is not None:
        monitor_call("started", process)
    mux_progress = _MkvmergeProgress(command_started, mkvmerge_input_size) if is_mkvmerge_mux else None
    if mux_progress is not None and os.name == "nt" and getattr(process, "stdout", None) is not None:
        process.stdout = _MkvmergeProgressReader(process.stdout, mux_progress)
    progress_output_seen = 0
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                log("正在终止当前外部处理进程...")
                terminate_process_tree(process)
                # Reap redirected pipes after killing the tree, before callers
                # attempt to remove files opened by the external tools.
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    # Reader threads can still own these streams if a descendant
                    # failed to exit. Closing a busy BufferedReader could block.
                    log("外部进程输出尚未关闭，保留原停止原因并交由临时文件重试清理。")
                else:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
                raise CancelledError("用户已停止处理。")
            if process_monitor is not None:
                monitor_call("poll", process)
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired as exc:
                if mux_progress is not None:
                    # POSIX communicate() uses select/os.read instead of the Windows
                    # reader. TimeoutExpired exposes its accumulated stdout there.
                    if isinstance(exc.output, bytes):
                        mux_progress.feed(exc.output[progress_output_seen:])
                        progress_output_seen = len(exc.output)
                    mux_progress.report(log)
                continue
    finally:
        if process_monitor is not None:
            monitor_call("finished", process)
    completed = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    stdout_text = decode_external_output(completed.stdout)
    stderr_text = decode_external_output(completed.stderr)
    details = (stdout_text + "\n" + stderr_text).strip()
    if _mkvtoolnix_warning_result(tool_name, completed.returncode):
        warning_lines = [
            line.strip()
            for line in details.splitlines()
            if line.strip().lower().startswith(("warning", "警告", "#gui#warning"))
        ]
        if warning_lines:
            first_warning = warning_lines[0]
            if len(first_warning) > 360:
                first_warning = first_warning[:357] + "..."
            log(
                f"{tool_name} 已完成，但报告 {len(warning_lines)} 条警告："
                f"{first_warning}"
            )
        else:
            log(f"{tool_name} 已完成，但外部工具返回警告状态。")
    elif completed.returncode != 0:
        if re.search(r"无法写入输出文件:\s*112\b", details, flags=re.IGNORECASE) or re.search(
            r"(?:not enough space on the disk|disk full)",
            details,
            flags=re.IGNORECASE,
        ):
            raise RuntimeError(
                "目标磁盘空间不足（Windows 错误 112）。未完成输出会自动清理；"
                "OCR 和翻译缓存会保留，腾出空间后可直接重试。"
            )
        raise RuntimeError(details or f"{tool_name} 执行失败（退出码 {completed.returncode}）。")
    if mkvmerge_input_size:
        elapsed = max(0.001, time.monotonic() - command_started)
        throughput = mkvmerge_input_size / elapsed / (1024 ** 2)
        log(
            f"mkvmerge 封装完成：耗时 {elapsed:.1f} 秒，"
            f"按主输入大小估算的处理速率 {throughput:.1f} MiB/s"
            "（含读取、写入与封装，不是实测磁盘读取速度）。"
        )
        metrics = _mkvmerge_process_metrics(process)
        if metrics is not None:
            cpu = metrics.get("cpu_seconds")
            reads, writes = metrics.get("read_bytes"), metrics.get("write_bytes")
            cpu_label = "未提供" if cpu is None else f"{cpu:.2f} 秒"
            read_label = "未提供" if reads is None else f"{reads} 字节"
            write_label = "未提供" if writes is None else f"{writes} 字节"
            log(
                f"mkvmerge 进程统计：CPU累计 {cpu_label}，"
                f"累计读取 {read_label}，累计写入 {write_label}"
                "（进程计数可能包含缓存与设备I/O，不是实测磁盘吞吐）。"
            )
    return completed


def ollama_available(host: str = OLLAMA_HOST, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def ollama_model_installed(model: str = DEFAULT_MODEL) -> bool:
    """Return whether the configured local translation model is installed."""
    expected = model.strip().lower()
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST.rstrip('/')}/api/tags", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if any(
            str(item.get("name") or item.get("model") or "").strip().lower() == expected
            for item in payload.get("models", [])
        ):
            return True
    except Exception:
        pass

    if ai_runtime.model_artifacts_valid(expected):
        return True

    if not Path(OLLAMA_EXE).is_file():
        return False
    try:
        completed = subprocess.run(
            [OLLAMA_EXE, "list"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=PROCESS_CREATION_FLAGS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        columns and columns[0].strip().lower() == expected
        for line in completed.stdout.splitlines()[1:]
        if (columns := line.split())
    )


def pull_ollama_model(
    model: str = DEFAULT_MODEL,
    log: Callable[[str], None] = log_noop,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> None:
    if not Path(OLLAMA_EXE).is_file():
        raise RuntimeError("未找到 SubFlow 本地 AI 运行库，无法准备翻译模型。")
    log(f"开始从魔搭下载官方翻译模型：{model}。首次约 5GB，请保持网络连接。")
    ensure_ollama_running(log=log, cancel_event=cancel_event)
    destination = ai_runtime.model_download_path()
    log(f"模型存放位置：{ai_runtime.resolve_model_root()}")

    def cancelled() -> bool:
        return bool(cancel_event and cancel_event.is_set())

    def report(value: int, detail: str) -> None:
        if progress:
            progress(value, detail)

    try:
        gguf_path = ai_runtime.download_model(
            destination,
            progress=report,
            log=log,
            cancelled=cancelled,
        )
        if progress:
            progress(100, "模型下载完成，正在自动导入 Ollama...")
        ai_runtime.import_model(
            Path(OLLAMA_EXE),
            gguf_path,
            model_name=model,
            log=log,
            cancelled=cancelled,
        )
    except ai_runtime.DownloadCancelled as exc:
        raise RuntimeError("模型准备已取消。") from exc
    if not ollama_model_installed(model):
        raise RuntimeError(f"模型自动导入后仍未检测到：{model}")
    if progress:
        progress(100, "翻译模型已准备完成")
    log(f"翻译模型已准备完成：{model}")


def ollama_runtime_dir() -> Path:
    """Keep Ollama away from the versioned application install directory."""
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubFlow"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_ollama_diagnostic(message: str, path: Path | None = None) -> None:
    """Keep two bounded local logs, opening files only while holding our lock."""
    try:
        path = path or ollama_runtime_dir() / "logs" / "ollama-engine.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (time.strftime("%Y-%m-%d %H:%M:%S ") + message[:8192] + "\n").encode("utf-8")
        with _OLLAMA_DIAGNOSTIC_LOCK:
            if path.exists() and path.stat().st_size + len(payload) > _OLLAMA_DIAGNOSTIC_MAX_BYTES:
                path.replace(path.with_name("ollama-engine.previous.log"))
            with path.open("ab") as stream:
                stream.write(payload)
    except OSError:
        # A full/locked log directory must never block draining the engine pipe.
        pass


def _safe_ollama_diagnostic_line(line: str) -> str | None:
    """Retain engine diagnostics, never request bodies or generated dialogue."""
    line = line.strip()
    if not line:
        return None
    if re.search(r'["\']?(?:prompt|messages|content|response|thinking)["\']?\s*[:=]', line, re.I):
        return "engine entry omitted: request/response field"
    # Ollama/llama.cpp operational prefixes and fatal messages. Discard arbitrary
    # continuations, which could otherwise include a multi-line request body.
    if re.match(
        r"^(?:time=|\[GIN\]|llama[_ :]|ggml[_ :]|gguf[_ :]|llm[_ :]|load[_ :]|"
        r"CUDA\b|HIP\b|Vulkan\b|GGML\b|fatal\b|panic\b|error\b|warning\b|"
        r"runtime:|signal\b|Exception\b|Traceback\b|goroutine\b|out of memory\b)",
        line, re.I,
    ):
        return line
    return None


def _capture_ollama_diagnostics(process: subprocess.Popen, path: Path) -> None:
    """Continuously drain the owned engine; cap each input line and close the pipe."""
    stream = process.stdout
    if stream is None:
        return
    discarding_long_line = False
    try:
        with stream:
            while True:
                raw = stream.readline(8193)
                if not raw:
                    break
                complete = raw.endswith(b"\n")
                if discarding_long_line or (len(raw) > 8192 and not complete):
                    discarding_long_line = not complete
                    continue
                line = _safe_ollama_diagnostic_line(raw.decode("utf-8", errors="replace"))
                if line:
                    _write_ollama_diagnostic(f"engine pid={process.pid}: {line}", path)
    except (OSError, ValueError) as exc:
        _write_ollama_diagnostic(f"engine pid={process.pid}: capture ended ({type(exc).__name__})", path)
    finally:
        try:
            returncode = process.wait()
            _write_ollama_diagnostic(f"engine pid={process.pid}: exit_code={returncode}", path)
        except OSError:
            pass


def ensure_ollama_running(
    log: Callable[[str], None] = log_noop,
    cancel_event: threading.Event | None = None,
    host: str = OLLAMA_HOST,
    wait_seconds: int = 25,
) -> None:
    global _OLLAMA_SERVER_PROCESS, _OLLAMA_SERVER_HOST
    with _OLLAMA_START_LOCK:
        check_cancel(cancel_event)
        if ollama_available(host):
            return
        if not Path(OLLAMA_EXE).exists():
            raise RuntimeError("未找到 Ollama。需要翻译字幕时，请先安装 Ollama 或关闭目标语言勾选。")

        with _OLLAMA_STATE_CHANGED:
            process = _OLLAMA_SERVER_PROCESS
        created = process is None or process.poll() is not None
        if created:
            log("本地翻译引擎未运行，正在启动 Ollama...")
            runtime_dir = ollama_runtime_dir()
            diagnostic_path = runtime_dir / "logs" / "ollama-engine.log"
            environment = ai_runtime.ollama_environment()
            environment["OLLAMA_DEBUG"] = "false"
            process = subprocess.Popen(
                [OLLAMA_EXE, "serve"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=PROCESS_CREATION_FLAGS,
                cwd=str(runtime_dir),
                env=environment,
            )
            with _OLLAMA_STATE_CHANGED:
                _OLLAMA_SERVER_PROCESS = process
                _OLLAMA_SERVER_HOST = host.rstrip("/")
            _write_ollama_diagnostic(f"engine pid={process.pid}: started", diagnostic_path)
            threading.Thread(
                target=_capture_ollama_diagnostics, args=(process, diagnostic_path),
                name="subflow-ollama-diagnostics", daemon=True,
            ).start()
            log(f"本地翻译引擎诊断日志：{diagnostic_path}")

        try:
            started = time.monotonic()
            while time.monotonic() - started < wait_seconds:
                check_cancel(cancel_event)
                if ollama_available(host):
                    log("Ollama 已启动。")
                    return
                if process.poll() is not None:
                    break
                if cancel_event is not None:
                    cancel_event.wait(0.5)
                else:
                    time.sleep(0.5)
        except BaseException:
            if created:
                terminate_process_tree(process)
                with _OLLAMA_STATE_CHANGED:
                    if _OLLAMA_SERVER_PROCESS is process:
                        _OLLAMA_SERVER_PROCESS = None
                        _OLLAMA_SERVER_HOST = None
            raise
        # Never stop an already running engine that another request may use.
        if created:
            terminate_process_tree(process)
            with _OLLAMA_STATE_CHANGED:
                if _OLLAMA_SERVER_PROCESS is process:
                    _OLLAMA_SERVER_PROCESS = None
                    _OLLAMA_SERVER_HOST = None
    raise RuntimeError("Ollama 启动超时。请手动打开 Ollama 后重试，或取消目标语言勾选。")


def begin_ollama_lease() -> None:
    global _OLLAMA_LEASES
    with _OLLAMA_STATE_CHANGED:
        while _OLLAMA_STOPPING:
            _OLLAMA_STATE_CHANGED.wait(timeout=0.2)
        _OLLAMA_LEASES += 1


def end_ollama_lease() -> bool:
    global _OLLAMA_LEASES
    with _OLLAMA_STATE_CHANGED:
        _OLLAMA_LEASES = max(0, _OLLAMA_LEASES - 1)
        _OLLAMA_STATE_CHANGED.notify_all()
        return _OLLAMA_LEASES == 0


@contextlib.contextmanager
def ollama_request_scope(cancel_event: threading.Event | None = None):
    global _OLLAMA_ACTIVE_REQUESTS
    while not _OLLAMA_INFERENCE_GATE.acquire(timeout=0.25):
        check_cancel(cancel_event)
        if _OLLAMA_SPLIT_BUDGET.get() is not None:
            _OLLAMA_SPLIT_BUDGET.get().check(cancel_event)
    registered = False
    try:
        with _OLLAMA_STATE_CHANGED:
            while _OLLAMA_STOPPING:
                check_cancel(cancel_event)
                if _OLLAMA_SPLIT_BUDGET.get() is not None:
                    _OLLAMA_SPLIT_BUDGET.get().check(cancel_event)
                _OLLAMA_STATE_CHANGED.wait(timeout=0.2)
            _OLLAMA_ACTIVE_REQUESTS += 1
            registered = True
        yield
    finally:
        with _OLLAMA_STATE_CHANGED:
            if registered:
                _OLLAMA_ACTIVE_REQUESTS = max(0, _OLLAMA_ACTIVE_REQUESTS - 1)
            _OLLAMA_STATE_CHANGED.notify_all()
        _OLLAMA_INFERENCE_GATE.release()


def _terminate_owned_ollama_server(log: Callable[[str], None]) -> bool:
    global _OLLAMA_SERVER_PROCESS, _OLLAMA_SERVER_HOST
    with _OLLAMA_STATE_CHANGED:
        process = _OLLAMA_SERVER_PROCESS
    if process is None or process.poll() is not None:
        return False
    terminate_process_tree(process)
    with _OLLAMA_STATE_CHANGED:
        if _OLLAMA_SERVER_PROCESS is process:
            _OLLAMA_SERVER_PROCESS = None
            _OLLAMA_SERVER_HOST = None
    log("本地翻译引擎未能正常释放，已安全结束本次自启动引擎；下次任务将自动重启。")
    return True


def unload_ollama_model(log: Callable[[str], None] = log_noop) -> None:
    global _OLLAMA_STOPPING
    if not _OLLAMA_UNLOAD_LOCK.acquire(blocking=False):
        return
    try:
        with _OLLAMA_STATE_CHANGED:
            if _OLLAMA_LEASES > 0 or _OLLAMA_ACTIVE_REQUESTS > 0 or _OLLAMA_STOPPING:
                return
            _OLLAMA_STOPPING = True
        if not Path(OLLAMA_EXE).exists():
            return
        try:
            completed = subprocess.run(
                [OLLAMA_EXE, "stop", DEFAULT_MODEL],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=PROCESS_CREATION_FLAGS,
                check=False,
            )
            if completed.returncode == 0:
                log("本地翻译模型已从内存释放，硬盘模型文件保留。")
            elif not _terminate_owned_ollama_server(log):
                log("本地翻译模型未能自动从内存释放；主程序已停止等待，不影响后续操作。")
        except subprocess.TimeoutExpired:
            if not _terminate_owned_ollama_server(log):
                log("本地翻译模型释放超过 8 秒；主程序已停止等待，不影响后续操作。")
        except Exception as exc:
            if not _terminate_owned_ollama_server(log):
                log(f"本地翻译模型从内存释放失败：{exc}")
    finally:
        with _OLLAMA_STATE_CHANGED:
            _OLLAMA_STOPPING = False
            _OLLAMA_STATE_CHANGED.notify_all()
        _OLLAMA_UNLOAD_LOCK.release()


def detect_nvidia_gpu() -> tuple[bool, str]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        possible = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        if possible.exists():
            nvidia_smi = str(possible)
    if not nvidia_smi:
        return False, "未检测到 nvidia-smi，可能没有 NVIDIA 显卡或驱动未安装。"
    try:
        completed = subprocess.run(
            [nvidia_smi, "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            creationflags=PROCESS_CREATION_FLAGS,
            check=False,
        )
    except Exception as exc:
        return False, f"NVIDIA 检测失败：{exc}"
    output = completed.stdout.decode("utf-8", errors="replace").strip()
    if completed.returncode == 0 and output:
        return True, output
    error = completed.stderr.decode("utf-8", errors="replace").strip()
    return False, error or "未检测到可用 NVIDIA 显卡。"


def ollama_processor_status(
    log: Callable[[str], None] = log_noop,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str]:
    if not Path(OLLAMA_EXE).exists():
        return "missing", "未找到 Ollama。"
    ensure_ollama_running(log=log, cancel_event=cancel_event)

    try:
        generate_body = json.dumps(
            {
                "model": DEFAULT_MODEL,
                "prompt": "Reply with OK.",
                "stream": False,
                "options": {"num_predict": 1},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{OLLAMA_HOST.rstrip('/')}/api/generate",
            data=generate_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                response.read()
        except Exception as exc:
            return "unknown", f"模型加载测试失败：{exc}"

        try:
            completed = subprocess.run(
                [OLLAMA_EXE, "ps"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                creationflags=PROCESS_CREATION_FLAGS,
                check=False,
            )
        except Exception as exc:
            return "unknown", f"Ollama 运行状态检测失败：{exc}"
        output = completed.stdout.decode("utf-8", errors="replace").strip()
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            return "unknown", error or "Ollama 运行状态检测失败。"

        lower = output.lower()
        if "gpu" in lower:
            return "gpu", output
        if "cpu" in lower:
            return "cpu", output
        return "unknown", output or "Ollama 未返回处理器信息。"
    finally:
        unload_ollama_model(log)


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def inspect_media(video_path: str, cancel_event: threading.Event | None = None) -> dict:
    completed = run_command([MKVMERGE, "-J", video_path], cancel_event=cancel_event)
    return json.loads(completed.stdout.decode("utf-8", errors="replace"))


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _tag_duration_seconds(value: object) -> float | None:
    match = re.fullmatch(
        r"(\d+):(\d+):(\d+)(?:\.(\d+))?",
        str(value or "").strip(),
    )
    if not match:
        return None
    hours, minutes, seconds, fraction = match.groups()
    fractional_seconds = float(f"0.{fraction}") if fraction else 0.0
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + fractional_seconds


def tracks_from_media(data: dict) -> list[Track]:
    tracks: list[Track] = []
    for item in data.get("tracks", []):
        props = item.get("properties", {})
        tracks.append(
            Track(
                id=int(item.get("id", 0)),
                type=str(item.get("type", "")),
                codec=str(item.get("codec", "")),
                language=str(props.get("language_ietf") or props.get("language") or "und"),
                name=str(props.get("track_name") or ""),
                default=bool(props.get("default_track", False)),
                forced=bool(props.get("forced_track", False)),
                text_subtitle=bool(props.get("text_subtitles", False)) or is_text_subtitle_item(item),
                pgs_subtitle=is_pgs_subtitle_item(item),
                vobsub_subtitle=is_vobsub_subtitle_item(item),
                statistics_frame_count=_optional_nonnegative_int(props.get("tag_number_of_frames")),
                statistics_duration_seconds=_tag_duration_seconds(props.get("tag_duration")),
                statistics_byte_count=_optional_nonnegative_int(props.get("tag_number_of_bytes")),
                commentary=props.get("flag_commentary") is True,
                visual_impaired=props.get("flag_visual_impaired") is True,
                original=props.get("flag_original") is True,
                channels=_optional_nonnegative_int(props.get("audio_channels")),
            )
        )
    return tracks


def inspect_tracks(video_path: str) -> list[Track]:
    return tracks_from_media(inspect_media(video_path))


def is_text_subtitle_item(item: dict) -> bool:
    codec = str(item.get("codec", "")).lower()
    codec_id = str(item.get("properties", {}).get("codec_id", "")).lower()
    return any(token in codec or token in codec_id for token in ("s_text", "subrip", "ass", "ssa", "utf8", "webvtt", "text"))


def is_pgs_subtitle_item(item: dict) -> bool:
    codec = str(item.get("codec", "")).lower()
    codec_id = str(item.get("properties", {}).get("codec_id", "")).lower()
    return "pgs" in codec or "pgs" in codec_id or "hdmv" in codec or "hdmv" in codec_id


def is_vobsub_subtitle_item(item: dict) -> bool:
    codec = str(item.get("codec", "")).lower()
    codec_id = str(item.get("properties", {}).get("codec_id", "")).lower()
    return "vobsub" in codec or "vobsub" in codec_id or "dvd_subtitle" in codec or "dvd_subtitle" in codec_id


def subtitle_extension(track: Track) -> str:
    if track.pgs_subtitle:
        return ".sup"
    if track.vobsub_subtitle:
        return ".sub"
    text = f"{track.codec} {track.name}".lower()
    if "ass" in text or "ssa" in text or "substation" in text:
        return ".ass"
    if "webvtt" in text or "vtt" in text:
        return ".vtt"
    return ".srt"


def clean_ass_text(text: str) -> str:
    text = ASS_TAG_RE.sub("", text)
    text = text.replace(r"\N", " ").replace(r"\n", " ").replace(r"\h", " ")
    text = text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_ass(path: Path) -> list[SubtitleEvent]:
    text = read_text(path)
    in_events = False
    fields: list[str] = []
    events: list[SubtitleEvent] = []
    for raw_line in text.splitlines():
        line = raw_line.strip("\ufeff")
        lowered = line.strip().lower()
        if lowered == "[events]":
            in_events = True
            continue
        if not in_events:
            continue
        if lowered.startswith("[") and lowered.endswith("]"):
            break
        if line.startswith("Format:"):
            fields = [item.strip() for item in line.split(":", 1)[1].split(",")]
            continue
        if not line.startswith("Dialogue:") or not fields:
            continue
        payload = line.split(":", 1)[1].lstrip()
        parts = payload.split(",", len(fields) - 1)
        if len(parts) != len(fields):
            continue
        row = dict(zip(fields, parts))
        try:
            start = ass_time_to_srt(row.get("Start", "").strip())
            end = ass_time_to_srt(row.get("End", "").strip())
        except ValueError:
            continue
        events.append(SubtitleEvent(start, end, clean_ass_text(row.get("Text", "").strip())))
    return events


def parse_srt(path: Path) -> list[SubtitleEvent]:
    text = read_text(path).replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text)
    events: list[SubtitleEvent] = []
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        time_index = -1
        match = None
        for idx, line in enumerate(lines):
            match = SRT_TIME_RE.search(line)
            if match:
                time_index = idx
                break
        if match is None or time_index < 0:
            continue
        body = " ".join(lines[time_index + 1 :]).strip()
        body = re.sub(r"<[^>]+>", "", body)
        body = re.sub(r"\s+", " ", body).strip()
        try:
            start = normalize_srt_time(match.group("start"))
            end = normalize_srt_time(match.group("end"))
        except ValueError:
            continue
        events.append(SubtitleEvent(start, end, body))
    return events


def parse_vtt(path: Path) -> list[SubtitleEvent]:
    text = read_text(path).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^\s*WEBVTT[^\n]*\n", "", text, flags=re.I)
    # WebVTT allows mm:ss.mmm as well as hh:mm:ss.mmm. Normalize only
    # timing lines; numbers in dialogue and cue identifiers are untouched.
    text = re.sub(
        r"^(\s*)((?:\d+:)?\d{2}:\d{2}\.\d{3})(\s*-->\s*)((?:\d+:)?\d{2}:\d{2}\.\d{3})",
        lambda m: m[1] + ("00:" if m[2].count(":") == 1 else "") + m[2]
        + m[3] + ("00:" if m[4].count(":") == 1 else "") + m[4],
        text, flags=re.M,
    )
    return parse_srt_from_text(text)


def parse_srt_from_text(text: str) -> list[SubtitleEvent]:
    blocks = re.split(r"\n\s*\n", text)
    events: list[SubtitleEvent] = []
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.splitlines() if line.strip()]
        match = None
        time_index = -1
        for idx, line in enumerate(lines):
            match = SRT_TIME_RE.search(line)
            if match:
                time_index = idx
                break
        if match is None or time_index < 0:
            continue
        body = re.sub(r"<[^>]+>", "", " ".join(lines[time_index + 1 :]))
        body = re.sub(r"\s+", " ", body).strip()
        try:
            start = normalize_srt_time(match.group("start"))
            end = normalize_srt_time(match.group("end"))
        except ValueError:
            continue
        events.append(SubtitleEvent(start, end, body))
    return events


def parse_subtitle(path: Path) -> list[SubtitleEvent]:
    suffix = path.suffix.lower()
    if suffix in (".ass", ".ssa"):
        return parse_ass(path)
    if suffix == ".vtt":
        return parse_vtt(path)
    return parse_srt(path)


def ass_time_to_srt(value: str) -> str:
    match = re.match(r"^\s*(\d+):(\d{2}):(\d{2})[.](\d{1,3})\s*$", value)
    if not match:
        raise ValueError(f"无效的 ASS 时间戳：{value}")
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError(f"无效的 ASS 时间戳：{value}")
    millis = int((fraction + "00")[:3])
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{millis:03d}"


def normalize_srt_time(value: str) -> str:
    value = value.replace(".", ",")
    match = re.match(r"^\s*(\d+):(\d{2}):(\d{2}),(\d{1,3})\s*$", value)
    if not match:
        raise ValueError(f"无效的字幕时间戳：{value}")
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError(f"无效的字幕时间戳：{value}")
    millis = int((fraction + "00")[:3])
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{millis:03d}"


def write_srt(path: Path, events: list[SubtitleEvent], translations: dict[int, str]) -> None:
    lines: list[str] = []
    index = 1
    for source_index, event in enumerate(events, start=1):
        text = translations.get(source_index, "").strip()
        if not text:
            continue
        lines.append(str(index))
        lines.append(f"{event.start} --> {event.end}")
        lines.append(text)
        lines.append("")
        index += 1
    path.write_text("\n".join(lines), encoding="utf-8-sig")


_SUBTITLE_EXTRACTION_LOCKS: dict[str, threading.RLock] = {}
_SUBTITLE_EXTRACTION_LOCKS_GUARD = threading.Lock()


def _extraction_signature(video_path: str, track: Track) -> dict | None:
    try:
        source = Path(video_path).stat()
    except OSError:
        return None
    return {"version": 1, "path": str(Path(video_path).resolve()),
            "size": source.st_size, "mtime_ns": source.st_mtime_ns,
            "track_id": track.id, "codec": track.codec, "language": track.language}


def _extraction_files(path: Path, track: Track) -> list[Path]:
    return [path, path.with_suffix(".idx")] if track.vobsub_subtitle else [path]


def _extraction_hashes(paths: list[Path]) -> dict[str, str]:
    hashes = {}
    for path in paths:
        if path.stat().st_size <= 0:
            raise ValueError("empty extracted subtitle")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        hashes[path.name] = digest.hexdigest()
    return hashes


def extract_subtitles(
    video_path: str,
    requests: list[tuple[Track, Path]],
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> None:
    """Extract a batch once; reuse only atomically completed, source-bound files."""
    with _SUBTITLE_EXTRACTION_LOCKS_GUARD:
        locks = [_SUBTITLE_EXTRACTION_LOCKS.setdefault(key, threading.RLock())
                 for key in sorted({os.path.normcase(str(path.resolve())) for _, path in requests})]
    with contextlib.ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
        pending = []
        for track, path in requests:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            signature = _extraction_signature(video_path, track)
            marker = path.with_name(path.name + ".complete.json")
            try:
                receipt = json.loads(marker.read_text(encoding="utf-8"))
                cached = (signature is not None and receipt["source"] == signature
                          and receipt["files"] == _extraction_hashes(_extraction_files(path, track)))
            except (OSError, ValueError, KeyError, TypeError):
                cached = False
            if cached:
                log(f"复用已完成抽取的字幕：{path}")
                continue
            # A cancellation or crash can leave a nonempty file. It is never
            # a cache hit without the completion receipt and matching hashes.
            marker.unlink(missing_ok=True)
            temporary_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix=".subtitle-extract-", dir=path.parent)))
            pending.append((track, path, temporary_dir / path.name, marker, signature))
        if not pending:
            return
        log(f"一次读取 {len(pending)} 条字幕轨，避免重复扫描影片。")
        run_command([MKVEXTRACT, "tracks", video_path,
                     *(f"{track.id}:{temporary}" for track, _, temporary, _, _ in pending)],
                    log=log, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("字幕抽取已取消。")
        completed = []
        for track, path, temporary, marker, signature in pending:
            if signature != _extraction_signature(video_path, track):
                raise RuntimeError("字幕抽取期间影片发生变化，请重新分析。")
            try:
                hashes = _extraction_hashes(_extraction_files(temporary, track))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"字幕轨 {track.id} 抽取完成后没有生成有效文件。") from exc
            completed.append((track, path, temporary, marker, signature, hashes))
        for track, path, temporary, marker, signature, hashes in completed:
            for source, target in zip(_extraction_files(temporary, track), _extraction_files(path, track)):
                source.replace(target)
            receipt_path = temporary.parent / "complete.json"
            receipt_path.write_text(json.dumps({"source": signature, "files": hashes}), encoding="utf-8")
            receipt_path.replace(marker)


def extract_subtitle(
    video_path: str,
    track: Track,
    output_dir: Path,
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    subtitle_path = output_dir / f"track-{track.id}-{track.language}{subtitle_extension(track)}"
    extract_subtitles(video_path, [(track, subtitle_path)], log, cancel_event)
    return subtitle_path


def ocr_language_for_track(track: Track) -> str:
    language = (track.language or "").strip().lower().replace("_", "-")
    name = (track.name or "").strip().lower()
    if language in {"chi", "zho", "zh", "cmn"}:
        if any(token in name for token in ("traditional", "cantonese", "繁体", "繁體", "粤语", "粵語")):
            return "chi_tra"
        return "chi_sim"
    if language in OCR_LANGUAGE_MAP:
        return OCR_LANGUAGE_MAP[language]
    base = language.split("-", 1)[0]
    if base in OCR_LANGUAGE_MAP:
        return OCR_LANGUAGE_MAP[base]
    raise RuntimeError(f"暂不支持对语言 {track.language or 'und'} 的 PGS 字幕做 OCR。")


def pgs_ocr_worker_command(
    input_path: Path,
    output_path: Path,
    language: str,
    source_video: str = "",
    track_id: int | None = None,
    engine: str = "standard",
) -> list[str]:
    worker_args = [
        "--pgs-ocr-worker",
        "--input", str(input_path),
        "--output", str(output_path),
        "--language", language,
        "--tesseract", TESSERACT,
        "--ocr-engine", engine,
    ]
    if source_video:
        worker_args.extend(["--source-video", source_video])
    if track_id is not None:
        worker_args.extend(["--track-id", str(track_id)])
    if getattr(sys, "frozen", False):
        return [sys.executable, *worker_args]
    gui_script = Path(__file__).resolve().with_name("movie_subtitle_tool_gui.py")
    return [sys.executable, str(gui_script), *worker_args]


def ocr_pgs_subtitle(
    extracted_path: Path,
    track: Track,
    output_dir: Path,
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> Path:
    language = ocr_language_for_track(track)
    output_path = output_dir / f"ocr-track-{track.id}-{track.language}.srt"
    if output_path.exists() and output_path.stat().st_size > 0:
        log(f"复用已识别 PGS 字幕：{output_path}")
        return output_path
    tessdata = Path(TESSERACT).resolve().parent / "tessdata" / f"{language}.traineddata"
    if not Path(TESSERACT).exists():
        raise RuntimeError("PGS OCR 组件缺失：未找到 Tesseract。请重新安装进阶版。")
    if not tessdata.exists():
        raise RuntimeError(f"PGS OCR 语言数据缺失：{language}。请重新安装进阶版或改选其他来源字幕。")
    log(f"正在识别 PGS 图片字幕：{track.language}（此阶段通常需要数分钟）")
    error_log = output_path.with_suffix(output_path.suffix + ".error.log")
    rapid_marker = output_path.with_suffix(output_path.suffix + ".rapid.json")
    rapid_marker.unlink(missing_ok=True)
    primary_error: RuntimeError | None = None
    try:
        run_command(
            pgs_ocr_worker_command(extracted_path, output_path, language),
            log=log,
            cancel_event=cancel_event,
        )
    except RuntimeError as exc:
        if error_log.exists():
            details = error_log.read_text(encoding="utf-8", errors="replace").strip()
            primary_error = RuntimeError(f"PGS OCR 失败：{details}")
        else:
            primary_error = exc
    if output_path.exists() and output_path.stat().st_size > 0:
        if rapid_marker.exists():
            try:
                rapid_detail = json.loads(rapid_marker.read_text(encoding="utf-8"))
                log(
                    "标准 PGS OCR 无法安全完成，RapidOCR CPU "
                    f"{rapid_detail.get('workers', 1)} 路并行兜底完成："
                    f"{rapid_detail.get('recognized', 0)}/{rapid_detail.get('total', 0)} 条"
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                log("RapidOCR CPU 并行兜底完成。")
        else:
            log(f"PGS OCR 完成：{output_path}")
        return output_path
    if not Path(SECONV).is_file():
        if primary_error:
            raise primary_error
        raise RuntimeError("PGS OCR 未生成有效字幕。")

    log("标准 PGS OCR 未生成有效字幕，已切换 Subtitle Edit 兼容 OCR 兜底。")
    output_dir.mkdir(parents=True, exist_ok=True)
    seconv_output = output_dir / f"{extracted_path.stem}.srt"
    if seconv_output.exists():
        seconv_output.unlink()
    environment = os.environ.copy()
    environment["PATH"] = str(Path(TESSERACT).parent) + os.pathsep + environment.get("PATH", "")
    run_command(
        [
            SECONV,
            str(extracted_path),
            "subrip",
            "--ocr-engine:tesseract",
            f"--ocr-language:{language}",
            f"--output-folder:{output_dir}",
            "--overwrite",
        ],
        log=log,
        cancel_event=cancel_event,
        env=environment,
    )
    if seconv_output.exists() and seconv_output.stat().st_size > 0:
        if seconv_output != output_path:
            seconv_output.replace(output_path)
        log(f"Subtitle Edit OCR 兜底完成：{output_path}")
        return output_path
    raise RuntimeError("Subtitle Edit OCR 未生成有效字幕。")


def srt_time_from_milliseconds(value: int) -> str:
    value = max(0, int(value))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def subtitle_time_to_milliseconds(value: str) -> int:
    hours, minutes, remainder = value.strip().replace(".", ",").split(":", 2)
    seconds, milliseconds = remainder.split(",", 1)
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds[:3].ljust(3, "0"))
    )


def shift_subtitle_timeline(source: Path, destination: Path, offset_milliseconds: int) -> Path:
    events = parse_subtitle(source)
    shifted: list[SubtitleEvent] = []
    for event in events:
        start = max(0, subtitle_time_to_milliseconds(event.start) + offset_milliseconds)
        end = max(start + 1, subtitle_time_to_milliseconds(event.end) + offset_milliseconds)
        shifted.append(
            SubtitleEvent(
                srt_time_from_milliseconds(start),
                srt_time_from_milliseconds(end),
                event.text,
            )
        )
    write_srt(
        destination,
        shifted,
        {index: event.text for index, event in enumerate(shifted, 1)},
    )
    return destination


def run_vobsub_ocr_worker(
    input_path: str,
    output_path: str,
    language: str,
    tesseract_path: str,
    source_video: str = "",
    track_id: int | None = None,
) -> int:
    av = importlib.import_module("av")
    np = importlib.import_module("numpy")
    image_module = importlib.import_module("PIL.Image")
    image_ops = importlib.import_module("PIL.ImageOps")
    pytesseract = importlib.import_module("pytesseract")

    tesseract = Path(tesseract_path).resolve()
    os.environ["TESSDATA_PREFIX"] = str(tesseract.parent / "tessdata")
    pytesseract.pytesseract.tesseract_cmd = str(tesseract)

    source = Path(input_path)
    index_path = source.with_suffix(".idx")
    if not index_path.exists():
        raise RuntimeError("VobSub OCR 缺少与 SUB 同名的 IDX 索引文件。")

    def recognize(bitmap, width: int, height: int) -> str:
        pixels = np.frombuffer(bitmap, dtype=np.uint8).reshape(height, width)
        # FFmpeg exposes the four DVD subpicture shades as 0..3. Index 0 is
        # transparent background; invert the remaining shades for Tesseract.
        palette = np.array([255, 15, 51, 102], dtype=np.uint8)
        image = image_module.fromarray(palette[np.minimum(pixels, 3)], mode="L")
        image = image_ops.autocontrast(image)
        if image.height < 90:
            image = image.resize((image.width * 2, image.height * 2), image_module.Resampling.LANCZOS)
        text = pytesseract.image_to_string(image, lang=language, config="--psm 6")
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    results: dict[int, tuple[int, int, str]] = {}
    pending: dict[concurrent.futures.Future, tuple[int, int, int]] = {}
    sequence = 0
    workers = max(1, min(6, os.cpu_count() or 4))

    def collect_done(done) -> None:
        for future in done:
            order, start_ms, end_ms = pending.pop(future)
            results[order] = (start_ms, end_ms, future.result())

    media_path = source_video if source_video and Path(source_video).is_file() else str(index_path)
    with av.open(media_path) as container, concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        subtitle_streams = [item for item in container.streams if item.type == "subtitle"]
        stream = next((item for item in subtitle_streams if track_id is not None and item.index == track_id), None)
        stream = stream or (subtitle_streams[0] if subtitle_streams else None)
        if stream is None:
            raise RuntimeError("没有从 IDX/SUB 文件中读到 VobSub 字幕。")
        for packet in container.demux(stream):
            if packet.pts is None:
                continue
            start_ms = round(float(packet.pts * packet.time_base) * 1000)
            duration_ms = round(float((packet.duration or 0) * packet.time_base) * 1000)
            end_ms = start_ms + (duration_ms if duration_ms > 0 else 4_000)
            for subtitle in packet.decode():
                if not getattr(subtitle, "planes", None) or subtitle.width <= 0 or subtitle.height <= 0:
                    continue
                bitmap = bytes(subtitle.planes[0])
                sequence += 1
                future = executor.submit(recognize, bitmap, subtitle.width, subtitle.height)
                pending[future] = (sequence, start_ms, end_ms)
                if len(pending) >= workers * 3:
                    done, _ = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                    collect_done(done)
        if pending:
            done, _ = concurrent.futures.wait(pending)
            collect_done(done)

    events: list[str] = []
    output_index = 0
    for order in sorted(results):
        start_ms, end_ms, text = results[order]
        if not text:
            continue
        output_index += 1
        events.extend([
            str(output_index),
            f"{srt_time_from_milliseconds(start_ms)} --> {srt_time_from_milliseconds(end_ms)}",
            text,
            "",
        ])
    if not events:
        raise RuntimeError("VobSub OCR 未识别出有效文字。")
    Path(output_path).write_text("\n".join(events), encoding="utf-8-sig")
    return 0


def ocr_vobsub_subtitle(
    extracted_path: Path,
    track: Track,
    output_dir: Path,
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
    source_video: str = "",
) -> Path:
    language = ocr_language_for_track(track)
    output_path = output_dir / f"ocr-track-{track.id}-{track.language}-vobsub.srt"
    if output_path.exists() and output_path.stat().st_size > 0:
        log(f"复用已识别 VobSub 字幕：{output_path}")
        return output_path
    tessdata = Path(TESSERACT).resolve().parent / "tessdata" / f"{language}.traineddata"
    if not Path(TESSERACT).exists():
        raise RuntimeError("VobSub OCR 组件缺失：未找到 Tesseract。请重新安装进阶版。")
    if not tessdata.exists():
        raise RuntimeError(f"VobSub OCR 语言数据缺失：{language}。请改选其他来源字幕。")
    log(f"正在识别 VobSub 图片字幕：{track.language}（此阶段通常需要数分钟）")
    error_log = output_path.with_suffix(output_path.suffix + ".error.log")
    try:
        run_command(
            pgs_ocr_worker_command(extracted_path, output_path, language, source_video, track.id),
            log=log,
            cancel_event=cancel_event,
        )
    except RuntimeError as exc:
        if error_log.exists():
            details = error_log.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(f"VobSub OCR 失败：{details}") from exc
        raise
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("VobSub OCR 未生成有效字幕。")
    log(f"VobSub OCR 完成：{output_path}")
    return output_path


def run_pgs_ocr_worker(
    input_path: str,
    output_path: str,
    language: str,
    tesseract_path: str,
    source_video: str = "",
    track_id: int | None = None,
    ocr_engine: str = "standard",
) -> int:
    if Path(input_path).suffix.lower() == ".sub":
        return run_vobsub_ocr_worker(
            input_path,
            output_path,
            language,
            tesseract_path,
            source_video=source_video,
            track_id=track_id,
        )
    if os.name == "nt":
        appdirs = importlib.import_module("appdirs")

        def windows_folder(csidl_name: str) -> str:
            folders = {
                "CSIDL_APPDATA": os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")),
                "CSIDL_LOCAL_APPDATA": os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")),
                "CSIDL_COMMON_APPDATA": os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            }
            return folders[csidl_name]

        appdirs._get_win_folder = windows_folder

    pytesseract = importlib.import_module("pytesseract")
    options_module = importlib.import_module("pgsrip.options")
    ripper_module = importlib.import_module("pgsrip.ripper")
    sup_module = importlib.import_module("pgsrip.sup")

    tesseract = Path(tesseract_path).resolve()
    os.environ["TESSDATA_PREFIX"] = str(tesseract.parent / "tessdata")
    pytesseract.pytesseract.tesseract_cmd = str(tesseract)

    class OcrLanguage:
        alpha3 = language

        def __str__(self) -> str:
            return language

    class PgsLanguageProxy:
        def __init__(self, source) -> None:
            self._source = source

        @property
        def language(self):
            return OcrLanguage()

        def __getattr__(self, name):
            return getattr(self._source, name)

    options = options_module.Options(overwrite=True, max_workers=max(1, min(8, os.cpu_count() or 4)))
    media = sup_module.Sup(input_path)
    pgs_tracks = list(media.get_pgs_medias(options))
    if not pgs_tracks:
        raise RuntimeError("没有从 SUP 文件中读到 PGS 字幕。")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with pgs_tracks[0] as pgs:
        proxy = PgsLanguageProxy(pgs)
        items = list(pgs.items)
        standard_error: Exception | None = None
        if ocr_engine == "standard":
            ripper = ripper_module.PgsToSrtRipper(proxy, options)
            estimated_bytes = estimate_pgs_composite_bytes(items, ripper.gap, ripper.max_tess_width)
            if estimated_bytes <= 384 * 1024 * 1024:
                try:
                    subtitles = ripper.rip(lambda text: text)
                    subtitles.path = str(output)
                    subtitles.save(encoding="utf-8")
                    if output.exists() and output.stat().st_size > 0:
                        return 0
                except Exception as exc:
                    standard_error = exc
            else:
                standard_error = RuntimeError(
                    f"标准 PGS OCR 合成图预计占用 {estimated_bytes / 1024 / 1024:.0f} MiB，已跳过以避免内存溢出"
                )
        try:
            workers, recognized = run_rapid_pgs_items(items, output)
            marker = output.with_suffix(output.suffix + ".rapid.json")
            marker.write_text(
                json.dumps(
                    {"workers": workers, "recognized": recognized, "total": len(items)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as rapid_error:
            if standard_error is not None:
                raise RuntimeError(
                    f"标准 PGS OCR 失败：{standard_error}\nRapidOCR CPU 兜底失败：{rapid_error}"
                ) from rapid_error
            raise
    return 0


def estimate_pgs_composite_bytes(items, gap: tuple[int, int], max_width: int) -> int:
    if not items:
        return 0
    areas: list[tuple[int, int]] = []
    remaining = sorted(items, key=lambda item: item.height)
    while remaining:
        first = remaining.pop(0)
        area_items = [first] + [item for item in remaining if item.intersect(first)]
        remaining = [item for item in remaining if not item.intersect(first)]
        current: list = []
        current_width = 0
        for item in area_items:
            next_width = current_width + item.width + gap[1]
            if current and next_width > max_width:
                areas.append((
                    max(entry.shape[2] for entry in current) - min(entry.shape[0] for entry in current),
                    sum(entry.width for entry in current) + (len(current) - 1) * gap[1],
                ))
                current = [item]
                current_width = item.width
            else:
                current.append(item)
                current_width = next_width
        if current:
            areas.append((
                max(entry.shape[2] for entry in current) - min(entry.shape[0] for entry in current),
                sum(entry.width for entry in current) + (len(current) - 1) * gap[1],
            ))
    total_height = sum(height for height, _width in areas) + max(0, len(areas) - 1) * gap[0] + 200
    total_width = max(width for _height, width in areas) + 200
    return total_height * total_width


def run_rapid_pgs_items(items, output_path: Path) -> tuple[int, int]:
    if not items:
        raise RuntimeError("没有可供 RapidOCR 识别的 PGS 图片。")
    rapid_module = importlib.import_module("rapidocr")
    image_module = importlib.import_module("PIL.Image")
    pysrt = importlib.import_module("pysrt")
    np = importlib.import_module("numpy")
    cpu_count = max(1, os.cpu_count() or 1)
    workers = 1 if cpu_count < 6 else 2 if cpu_count < 12 else 3
    threads_per_worker = max(1, cpu_count // workers)
    indexed_items = list(enumerate(items))
    chunks = [indexed_items[offset::workers] for offset in range(workers)]

    def recognize_chunk(chunk):
        engine = rapid_module.RapidOCR(params={
            "Global.log_level": "error",
            "EngineConfig.onnxruntime.intra_op_num_threads": threads_per_worker,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
        chunk_results = []
        for index, item in chunk:
            image = item.image.data
            if image.shape[0] < 64:
                scale = max(2, min(3, (64 + image.shape[0] - 1) // image.shape[0]))
                pil_image = image_module.fromarray(image)
                image = np.asarray(
                    pil_image.resize(
                        (pil_image.width * scale, pil_image.height * scale),
                        image_module.Resampling.LANCZOS,
                    )
                )
            result = engine(image, use_cls=False, text_score=0.5)
            text = "\n".join(
                re.sub(r"\s+", " ", value).strip()
                for value in (getattr(result, "txts", None) or ())
                if re.sub(r"\s+", " ", value).strip()
            )
            chunk_results.append((index, item.start, item.end, text))
        return chunk_results

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for chunk_result in executor.map(recognize_chunk, chunks):
            results.extend(chunk_result)
    subtitles = pysrt.SubRipFile()
    for _index, start, end, text in sorted(results):
        if text:
            subtitles.append(pysrt.SubRipItem(index=len(subtitles) + 1, start=start, end=end, text=text))
    if not subtitles:
        raise RuntimeError("RapidOCR 未识别出有效字幕。")
    subtitles.path = str(output_path)
    subtitles.save(encoding="utf-8")
    return workers, len(subtitles)


def pgs_ocr_worker_main(argv: list[str] | None = None) -> int:
    if getattr(sys, "frozen", False):
        if sys.stdin is None:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--pgs-ocr-worker", action="store_true")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--tesseract", required=True)
    parser.add_argument("--source-video", default="")
    parser.add_argument("--track-id", type=int)
    parser.add_argument("--ocr-engine", choices=("standard", "rapidocr"), default="standard")
    args = parser.parse_args(argv)
    error_log = Path(args.output).with_suffix(Path(args.output).suffix + ".error.log")
    try:
        return run_pgs_ocr_worker(
            args.input,
            args.output,
            args.language,
            args.tesseract,
            source_video=args.source_video,
            track_id=args.track_id,
            ocr_engine=args.ocr_engine,
        )
    except Exception:
        error_log.parent.mkdir(parents=True, exist_ok=True)
        error_log.write_text(traceback.format_exc(), encoding="utf-8")
        return 1


def is_matroska_family(path: str) -> bool:
    return Path(path).suffix.lower() in {".mkv", ".mka", ".mks", ".mk3d", ".webm"}


def prepare_work_input(
    input_path: str,
    work_dir: Path,
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> str:
    if is_matroska_family(input_path):
        return input_path

    work_dir.mkdir(parents=True, exist_ok=True)
    normalized = work_dir / "source-normalized.mkv"
    log("检测到非 MKV 输入，先生成临时 MKV 工作副本。")
    run_command([MKVMERGE, "-o", str(normalized), input_path], log=log, cancel_event=cancel_event)
    return str(normalized)


def map_normalized_track_ids(
    input_tracks: list[Track],
    normalized_tracks: list[Track],
) -> dict[int, int]:
    """Map source track IDs to a temporary Matroska work copy by track order.

    MP4/MOV track IDs are not retained by mkvmerge when the file is normalized
    to MKV. Track order within each media type is retained, which makes this a
    stable mapping for subtitle extraction while keeping output selection tied
    to the original file.
    """
    mapping: dict[int, int] = {}
    for track_type in ("video", "audio", "subtitles"):
        original = [track for track in input_tracks if track.type == track_type]
        normalized = [track for track in normalized_tracks if track.type == track_type]
        if len(original) != len(normalized):
            raise RuntimeError(
                "临时 MKV 的轨道数量与原文件不一致，无法安全映射轨道。"
                f" {track_type}: 原文件 {len(original)}，临时文件 {len(normalized)}。"
            )
        mapping.update({source.id: work.id for source, work in zip(original, normalized)})
    return mapping


def strip_thinking(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    text = re.sub(r"```(?:json|text)?", "", text, flags=re.I)
    return text.replace("```", "").strip()


def parse_numbered_response(response: str, expected: int) -> list[str] | None:
    compact = [line.strip() for line in response.splitlines() if line.strip()]
    if len(compact) != expected:
        return None
    matches = [re.match(r"^(\d+)\s*([.)\]:：、）-])\s*(.+)$", line) for line in compact]
    # Decimal amounts (1.5元) cannot be distinguished from an unspaced list
    # marker. Retry rather than silently dropping part of the dialogue.
    if any(m and re.match(r"^\d+[.:：-]\d", line)
           for line, m in zip(compact, matches)):
        return None
    if all(matches) and [int(m[1]) for m in matches] == list(range(1, expected + 1)):
        return [m[3].strip() for m in matches]
    if any(m and 1 <= int(m[1]) <= expected for m in matches):
        return None  # Mixed or duplicated numbering needs a clean retry.
    return compact


@dataclass
class TranslationMetrics:
    """Per-target diagnostics; never retains prompts or generated text."""

    requests: int = 0
    completed_requests: int = 0
    retries: int = 0
    batch_parse_failures: int = 0
    fallback_lines: int = 0
    quality_repairs: int = 0
    context_repairs: int = 0
    request_wall_seconds: float = 0.0
    successful_request_seconds: float = 0.0
    failed_request_seconds: float = 0.0
    cancelled_request_seconds: float = 0.0
    failed_requests: int = 0
    cancelled_requests: int = 0
    retry_wait_seconds: float = 0.0
    translation_wall_seconds: float | None = None
    total_duration_ns: float = 0.0
    load_duration_ns: float = 0.0
    prompt_eval_duration_ns: float = 0.0
    eval_duration_ns: float = 0.0
    prompt_eval_count: int = 0
    eval_count: int = 0
    total_samples: int = 0
    load_samples: int = 0
    prompt_duration_samples: int = 0
    eval_duration_samples: int = 0
    prompt_count_samples: int = 0
    eval_count_samples: int = 0
    diagnostic_log: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
    diagnostic_indices: tuple[int, ...] = field(default=(), repr=False, compare=False)
    diagnostic_count: int = field(default=0, repr=False, compare=False)

    def log_stream_split(self, target_code, error, count, cancel_event=None):
        check_cancel(cancel_event)
        if self.diagnostic_log is None or self.diagnostic_count >= 20:
            return
        self.diagnostic_count += 1
        indices = self.diagnostic_indices
        location = f'字幕第 {indices[0]}—{indices[-1]} 行' if indices else '字幕行范围未提供'
        target = LANGUAGES.get(target_code, ('', '', target_code))[2]
        capped = '；本轮详细异常日志已达20条' if self.diagnostic_count == 20 else ''
        try:
            self.diagnostic_log(f'翻译断流恢复：目标 {target}（{target_code}），{location}；'
                f'{error.diagnostic()}；引擎服务正常，丢弃未完成响应；'
                f'将本批{count}条拆成{count//2}+{count-count//2}条，已完成小段立即保存{capped}。')
        except CancelledError:
            raise
        except Exception:
            pass
        check_cancel(cancel_event)

    def log_batch_fallback(self, target_code: str, reason_code: str, reason: str,
                           elapsed: float, line_count: int,
                           cancel_event: threading.Event | None = None) -> None:
        """Best-effort, bounded context for an existing recovery decision."""
        check_cancel(cancel_event)
        if self.diagnostic_log is None or self.diagnostic_count >= 20:
            return
        self.diagnostic_count += 1
        indices = self.diagnostic_indices
        location = (f"字幕第 {indices[0]}—{indices[-1]} 行" if indices else "字幕行范围未提供")
        target = LANGUAGES.get(target_code, ("", "", target_code))[2]
        # Only a bounded exception description is used, never the model response.
        reason = " ".join(str(reason).split())[:240]
        capped = "；本轮详细异常日志已达20条，后续仍按原策略处理并计入统计" if self.diagnostic_count == 20 else ""
        message = (f"翻译异常：目标 {target}（{target_code}），{location}（本批{line_count}条）；"
                   f"原因类型 {reason_code}；原因：{reason}；本批请求耗时 {max(0.0, elapsed):.2f} 秒；"
                   f"恢复动作：改为逐条重译，本批{line_count}条{capped}。")
        try:
            self.diagnostic_log(message)
        except CancelledError:
            raise
        except Exception:
            pass  # Diagnostic I/O must not discard a recoverable translation.
        check_cancel(cancel_event)

    def record_request_time(self, elapsed: float, outcome: str) -> None:
        elapsed = max(0.0, elapsed)
        self.request_wall_seconds += elapsed
        if outcome == "completed":
            self.successful_request_seconds += elapsed
        elif outcome == "cancelled":
            self.cancelled_requests += 1
            self.cancelled_request_seconds += elapsed
        else:
            self.failed_requests += 1
            self.failed_request_seconds += elapsed

    @contextlib.contextmanager
    def measure_request(self, started: float):
        outcome = "completed"
        try:
            yield
        except CancelledError:
            outcome = "cancelled"
            raise
        except BaseException:
            outcome = "failed"
            raise
        finally:
            self.record_request_time(time.monotonic() - started, outcome)

    def record_done(self, response: dict) -> None:
        self.completed_requests += 1
        fields = (
            ("total_duration", "total_duration_ns", "total_samples", False),
            ("load_duration", "load_duration_ns", "load_samples", False),
            ("prompt_eval_duration", "prompt_eval_duration_ns", "prompt_duration_samples", False),
            ("eval_duration", "eval_duration_ns", "eval_duration_samples", False),
            ("prompt_eval_count", "prompt_eval_count", "prompt_count_samples", True),
            ("eval_count", "eval_count", "eval_count_samples", True),
        )
        for key, total_name, samples_name, integer in fields:
            value = response.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not 0 <= value < float("inf"):
                continue
            if integer and value != int(value):
                continue
            setattr(self, total_name, getattr(self, total_name) + (int(value) if integer else value))
            setattr(self, samples_name, getattr(self, samples_name) + 1)

    def summary(self) -> str:
        def seconds(total: float, samples: int) -> str:
            if not samples:
                return "未提供"
            return f"{total / 1_000_000_000:.2f}秒（{samples}份响应统计）"

        def count(total: int, samples: int) -> str:
            return f"{total}（{samples}份响应统计）" if samples else "未提供"

        wall = (f"{self.translation_wall_seconds:.2f}秒"
                if self.translation_wall_seconds is not None else "未记录")
        return (
            f"翻译模型统计：请求{self.requests}次，完成{self.completed_requests}次，"
            f"重试{self.retries}次，失败{self.failed_requests}次，取消{self.cancelled_requests}次；"
            f"整轮实际耗时{wall}，"
            f"请求实际耗时{self.request_wall_seconds:.2f}秒"
            f"（成功{self.successful_request_seconds:.2f}秒，失败{self.failed_request_seconds:.2f}秒，"
            f"取消{self.cancelled_request_seconds:.2f}秒），重试等待{self.retry_wait_seconds:.2f}秒；"
            f"引擎已完成响应耗时{seconds(self.total_duration_ns, self.total_samples)}，"
            f"模型加载{seconds(self.load_duration_ns, self.load_samples)}，"
            f"提示词处理{seconds(self.prompt_eval_duration_ns, self.prompt_duration_samples)}，"
            f"文本生成{seconds(self.eval_duration_ns, self.eval_duration_samples)}；"
            f"提示词token {count(self.prompt_eval_count, self.prompt_count_samples)}，"
            f"生成token {count(self.eval_count, self.eval_count_samples)}；"
            f"批次格式回退{self.batch_parse_failures}批，逐条重译{self.fallback_lines}条；"
            f"质量修复{self.quality_repairs}次，连续字幕恢复{self.context_repairs}次。"
        )


def call_ollama(
    prompt: str,
    model: str,
    host: str,
    timeout: int,
    cancel_event: threading.Event | None = None,
    metrics: TranslationMetrics | None = None,
    *,
    response_schema: dict | None = None,
    max_tokens: int = 2048,
) -> str:
    check_cancel(cancel_event)
    budget = _OLLAMA_SPLIT_BUDGET.get()
    if budget is not None:
        budget.take(cancel_event)
    if metrics is not None:
        metrics.requests += 1
    body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "think": False,
        "keep_alive": "10m",
        "options": {
            "temperature": 0.12,
            "top_p": 0.9,
            "num_ctx": OLLAMA_CONTEXT_SIZE,
            "num_predict": max(32, min(2048, int(max_tokens))),
        },
    }
    if response_schema is not None:
        body["format"] = response_schema
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    parts: list[str] = []
    tail = ""
    response_chars = 0
    next_repeat_check = 128
    response_frames = 0
    started = time.monotonic()
    last_activity = started
    maximum_seconds = min(OLLAMA_REQUEST_MAX_SECONDS, max(90, int(timeout)))
    socket_timeout = min(OLLAMA_STREAM_IDLE_SECONDS, max(30, int(timeout)))
    if budget is not None:
        remaining = budget.check(cancel_event)
        maximum_seconds = min(maximum_seconds, remaining)
        socket_timeout = min(socket_timeout, remaining)
    timer = metrics.measure_request(started) if metrics is not None else contextlib.nullcontext()
    with timer, ollama_request_scope(cancel_event):
        with urllib.request.urlopen(request, timeout=socket_timeout) as response:
            status = getattr(response, 'status', None)
            headers = getattr(response, 'headers', {})
            content_type = headers.get('Content-Type', '') if hasattr(headers, 'get') else ''
            def stream_error(reason_code, message):
                return TranslationStreamError(message, reason_code=reason_code,
                    http_status=status, response_frames=response_frames, response_chars=response_chars,
                    elapsed=time.monotonic()-started, content_type=content_type)
            while True:
                check_cancel(cancel_event)
                if budget is not None:
                    remaining = budget.check(cancel_event)
                    # urllib's HTTPResponse keeps the socket here. Cap every
                    # blocking read by the shared remaining recovery time.
                    stream_socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if stream_socket is not None:
                        stream_socket.settimeout(min(socket_timeout, remaining))
                now = time.monotonic()
                if now - started >= maximum_seconds:
                    raise TimeoutError(f"本地模型单批处理超过 {maximum_seconds} 秒。")
                try:
                    raw_line = response.readline()
                except (socket.timeout, TimeoutError) as exc:
                    if time.monotonic() - last_activity >= OLLAMA_STREAM_IDLE_SECONDS:
                        raise TimeoutError(
                            f"本地模型连续 {OLLAMA_STREAM_IDLE_SECONDS} 秒没有返回新内容。"
                        ) from exc
                    continue
                if not raw_line:
                    raise stream_error('stream_eof', "本地模型连接提前结束，未收到完成标记；本批译文未采用。")
                last_activity = time.monotonic()
                try:
                    item = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as exc:
                    if not raw_line.endswith(b"\n"):
                        raise stream_error('truncated_json', "本地模型响应在传输中截断；本批译文未采用。") from exc
                    raise
                response_frames += 1
                if item.get("error"):
                    error = item['error']
                    message = error.get('message', '') if isinstance(error, dict) else str(error)
                    if isinstance(message, str) and re.fullmatch(r'The model produced output that does not match the expected [\w -]{1,80} format', message):
                        raise stream_error('engine_output_format', '本地模型引擎的输出格式解析失败；本批译文未采用。')
                    raise RuntimeError(str(item["error"]))
                if item.get("response"):
                    fragment = str(item["response"])
                    parts.append(fragment)
                    tail = (tail + fragment)[-768:]
                    response_chars += len(fragment)
                    if response_chars >= next_repeat_check:
                        next_repeat_check = response_chars + 64
                        if re.search(r"(.{4,64}?)\1{5,}\s*$", " ".join(tail.split())):
                            raise TranslationGenerationError("本地模型持续重复相同片段；已终止本次生成，不原样重试。",
                                                             reason_code="repetition_detected")
                if item.get("done"):
                    if item.get("done_reason") == "length":
                        raise TranslationGenerationError("本地模型达到生成长度上限，未完成的响应未采用。",
                                                         reason_code="generation_length_limit")
                    if metrics is not None:
                        metrics.record_done(item)
                    break
    check_cancel(cancel_event)
    return strip_thinking("".join(parts))


def call_ollama_retry(
    prompt: str,
    model: str,
    host: str,
    timeout: int,
    cancel_event: threading.Event | None = None,
    attempts: int = 2,
    metrics: TranslationMetrics | None = None,
    *,
    response_schema: dict | None = None,
    max_tokens: int | None = None,
) -> str:
    generation_options = {}
    if response_schema is not None:
        generation_options["response_schema"] = response_schema
    if max_tokens is not None:
        generation_options["max_tokens"] = max_tokens
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        check_cancel(cancel_event)
        budget = _OLLAMA_SPLIT_BUDGET.get()
        if budget is not None:
            budget.check_request(cancel_event)
        if attempt > 1 and metrics is not None:
            metrics.retries += 1
        try:
            if metrics is None:
                return call_ollama(prompt, model=model, host=host, timeout=timeout, cancel_event=cancel_event,
                                   **generation_options)
            return call_ollama(
                prompt, model=model, host=host, timeout=timeout,
                cancel_event=cancel_event, metrics=metrics, **generation_options,
            )
        except (socket.timeout, TimeoutError, ConnectionError, http.client.IncompleteRead,
                urllib.error.URLError) as exc:
            last_error = exc
            _write_ollama_diagnostic(
                f"translation attempt={attempt}/{attempts}: {type(exc).__name__} "
                f"errno={getattr(exc, 'errno', None)} winerror={getattr(exc, 'winerror', None)}"
                + (' ' + exc.diagnostic() if isinstance(exc, TranslationStreamError) else '')
            )
            if (isinstance(exc, TranslationStreamError) and exc.split_candidate
                    and _OLLAMA_BATCH_STREAM_RECOVERY.get()):
                check_cancel(cancel_event)
                budget = _OLLAMA_SPLIT_BUDGET.get()
                remaining = budget.check(cancel_event) if budget is not None else 2.0
                healthy = ollama_available(host, timeout=min(2.0, remaining))
                check_cancel(cancel_event)
                if healthy:
                    exc.transport_attempts = attempt
                    raise  # A complete child response is required, never partial text.
            if attempt < attempts:
                # Retry the unfinished batch only. An external or live engine is
                # never killed/replaced; restart only our own confirmed exit.
                with _OLLAMA_STATE_CHANGED:
                    process = _OLLAMA_SERVER_PROCESS
                    restart_owned = (
                        process is not None and process.poll() is not None
                        and _OLLAMA_SERVER_HOST == host.rstrip("/")
                        and _OLLAMA_ACTIVE_REQUESTS == 0 and not _OLLAMA_STOPPING
                    )
                check_cancel(cancel_event)
                if restart_owned:
                    _write_ollama_diagnostic(
                        f"translation recovery: owned engine pid={process.pid} "
                        f"exit_code={process.poll()}; restarting"
                    )
                    ensure_ollama_running(host=host, cancel_event=cancel_event)
                wait_started = time.monotonic() if metrics is not None else None
                try:
                    budget = _OLLAMA_SPLIT_BUDGET.get()
                    delay = min(2, budget.check(cancel_event)) if budget is not None else 2
                    if cancel_event is not None:
                        cancel_event.wait(delay)
                        check_cancel(cancel_event)
                    else:
                        time.sleep(delay)
                finally:
                    if wait_started is not None:
                        metrics.retry_wait_seconds += max(0.0, time.monotonic() - wait_started)
                continue
    raise RuntimeError(
        f"本地模型请求未完成（{type(last_error).__name__}），已达到 {attempts} 次尝试上限；"
        "已完成的翻译缓存保留，请重试以继续。"
        + (' ' + last_error.diagnostic() if isinstance(last_error, TranslationStreamError) else '')
    ) from last_error


def make_prompt(lines: list[str], target_code: str, source_language: str, *, require_target_script: bool = False,
                context_lines: tuple[str, str] | None = None, repair_feedback: str = "") -> str:
    # Keep raw source text as the cache/review identity. Only the model's view
    # excludes recognized display markup, including legacy MicroDVD styling.
    lines = [subtitle_text_formats.translation_text(line) for line in lines]
    if context_lines:
        context_lines = tuple(subtitle_text_formats.translation_text(line) for line in context_lines)
    repair_feedback = subtitle_text_formats.translation_text(repair_feedback)
    target_name = LANGUAGES[target_code][1]
    numbered = "\n".join(f"{idx}. {line}" for idx, line in enumerate(lines, start=1))
    feedback = ("\nThe previous attempt still failed this check: " + repair_feedback
                + "\nWrite a fresh, idiomatic translation in the target language; do not copy the faulty wording.\n"
                if repair_feedback else "")
    if require_target_script and target_code.startswith("zh"):
        context = (
            f"\n仅供理解的相邻台词，不要输出它们：\n前句：{context_lines[0]}\n后句：{context_lines[1]}\n"
            if context_lines else ""
        )
        return (
            f"请把下面的电影字幕翻译为{target_name}。此前的回答残留了英文，请重新翻译。\n"
            "普通对白必须翻译；原文中确有的人名、机构名、品牌、作品名、缩写和代号可以原样保留。\n"
            "按上下文区分名称和普通单词，不要强行翻译名称，也不要把普通对白当作名称照抄。\n"
            "只输出译文，不要添加解释和思考过程；保留名称的原始拼写，不得新增或重复名称。\n"
            "结合上下文理解含义，不要把后句的意思补进本句。\n"
            f"严格输出{len(lines)}行，格式为：1. 中文译文{context}{feedback}\n字幕原文：\n{numbered}\n译文："
        )
    repair_rules = ""
    context = ""
    if require_target_script and target_code in _NON_CJK_TRANSLATION_TARGETS:
        repair_rules = (
            "- Rewrite this line: the previous translation contained text outside the target language.\n"
            "- Do not introduce Chinese, Japanese, or Korean text. A name or quotation in those scripts "
            "may be retained only if it appears verbatim in the source line.\n"
            "- Use context to understand this line, without adding meaning from adjacent lines.\n"
        )
        if context_lines:
            context = (
                "\nAdjacent source lines for context only; do not output them:\n"
                f"Previous: {context_lines[0]}\nNext: {context_lines[1]}\n"
            )
    return f"""Translate these movie subtitle lines into {target_name}.
Source language hint: {source_language or "auto-detect"}.
Rules:
- Output exactly {len(lines)} lines.
- Keep the same numbering format: 1. translated text
- Keep subtitles concise and natural for on-screen reading.
- Preserve names, tone, and meaning.
- Do not add explanations, notes, markdown, or extra lines.
{repair_rules}{context}{feedback}
Subtitle lines:
{numbered}

Translated output:"""


class InvalidTranslationResponseError(RuntimeError):
    """A model response cannot supply even one structurally usable subtitle."""


class TranslationGenerationError(InvalidTranslationResponseError):
    """Completed transport, but generation repeated or exhausted its budget."""

    def __init__(self, message: str, *, reason_code: str = "generation_failure"):
        super().__init__(message)
        self.reason_code = reason_code


class TranslationStreamError(ConnectionError):
    """An HTTP stream ended without a complete generation, not a valid result."""
    def __init__(self, message, *, reason_code, http_status=None, response_frames=0,
                 response_chars=0, elapsed=0., content_type=''):
        super().__init__(message)
        self.reason_code = reason_code
        self.http_status = http_status
        self.response_frames = response_frames
        self.response_chars = response_chars
        self.elapsed = max(0., elapsed)
        self.content_type = str(content_type)[:80]

    @property
    def split_candidate(self):
        return self.http_status == 200 and (self.reason_code == 'engine_output_format' or
            (self.reason_code == 'stream_eof' and self.response_frames > 0 and self.response_chars > 0))

    def diagnostic(self):
        return (f'reason={self.reason_code} http={self.http_status} frames={self.response_frames} '
                f'chars={self.response_chars} elapsed={self.elapsed:.2f}s')


class TranslationRecoveryLimitError(RuntimeError):
    pass


@dataclass
class _TranslationSplitBudget:
    deadline: float
    remaining_requests: int

    def check(self, cancel_event=None):
        check_cancel(cancel_event)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TranslationRecoveryLimitError('失败批次拆分恢复已达到时间预算；已完成的小段缓存保留。')
        return remaining

    def check_request(self, cancel_event=None):
        self.check(cancel_event)
        if self.remaining_requests <= 0:
            raise TranslationRecoveryLimitError('失败批次拆分恢复已达到请求上限；已完成的小段缓存保留。')

    def take(self, cancel_event=None):
        self.check_request(cancel_event)
        self.remaining_requests -= 1


def translate_batch(
    lines: list[str],
    target_code: str,
    source_language: str,
    model: str,
    host: str,
    timeout: int,
    cancel_event: threading.Event | None = None,
    metrics: TranslationMetrics | None = None,
    require_target_script: bool = False,
    context_lines: tuple[str, str] | None = None,
    repair_feedback: str = "",
    on_fallback_translation: Callable[[int, str], None] | None = None,
) -> list[str]:
    check_cancel(cancel_event)
    started = time.monotonic()
    original_indices = metrics.diagnostic_indices if metrics is not None else ()
    published = set()
    budget_token = None

    def publish(offset, text):
        if on_fallback_translation is not None and offset not in published:
            on_fallback_translation(offset, text)
            published.add(offset)

    def request(group):
        token = _OLLAMA_BATCH_STREAM_RECOVERY.set(len(group) > 1)
        try:
            return call_ollama_retry(
                make_prompt(group, target_code, source_language, require_target_script=require_target_script,
                            context_lines=context_lines, repair_feedback=repair_feedback),
                model=model, host=host, timeout=timeout, cancel_event=cancel_event,
                **({'metrics': metrics} if metrics is not None else {}))
        finally:
            _OLLAMA_BATCH_STREAM_RECOVERY.reset(token)

    def run(group, offset):
        nonlocal budget_token
        check_cancel(cancel_event)
        if metrics is not None:
            metrics.diagnostic_indices = original_indices[offset:offset+len(group)]
        request_seconds_before = metrics.request_wall_seconds if metrics is not None else 0.
        generation_error = None
        try:
            response = request(group)
        except TranslationStreamError as exc:
            if len(group) <= 1 or not exc.split_candidate:
                raise
            if _OLLAMA_SPLIT_BUDGET.get() is None:
                budget_token = _OLLAMA_SPLIT_BUDGET.set(_TranslationSplitBudget(
                    started + min(OLLAMA_REQUEST_MAX_SECONDS, max(90, int(timeout))),
                    max(0, 2*len(lines)-1-getattr(exc, 'transport_attempts', 1))))
            _OLLAMA_SPLIT_BUDGET.get().check(cancel_event)
            if metrics is not None:
                metrics.log_stream_split(target_code, exc, len(group), cancel_event)
            middle = len(group)//2
            result = []
            for part, child_offset in ((group[:middle], offset), (group[middle:], offset+middle)):
                child = run(part, child_offset)
                for index, text in enumerate(child):
                    publish(child_offset+index, text)
                result.extend(child)
            return result
        except TranslationGenerationError as exc:
            if len(group) == 1:
                raise
            generation_error = exc
            response = ''
        except TranslationRecoveryLimitError:
            raise
        except RuntimeError as exc:
            if _OLLAMA_SPLIT_BUDGET.get() is not None:
                indices = original_indices[offset:offset+len(group)]
                location = (f'字幕第 {indices[0]}—{indices[-1]} 行' if indices else
                            f'本批第 {offset+1}—{offset+len(group)} 条')
                raise RuntimeError(f'{location}拆分恢复失败：{exc}') from exc
            raise
        parsed = parse_numbered_response(response, len(group))
        if parsed is not None:
            return parsed
        if metrics is not None:
            metrics.batch_parse_failures += 1
            metrics.log_batch_fallback(target_code,
                generation_error.reason_code if generation_error else 'batch_response_format',
                str(generation_error) if generation_error else
                f'模型响应条数或编号不符合本批{len(group)}条字幕要求，未采用该批响应。',
                metrics.request_wall_seconds-request_seconds_before, len(group), cancel_event)
        result = []
        for index, line in enumerate(group):
            check_cancel(cancel_event)
            if metrics is not None:
                metrics.fallback_lines += 1
                metrics.diagnostic_indices = original_indices[offset+index:offset+index+1]
            single = request([line])
            parsed_single = parse_numbered_response(single, 1)
            if not single.strip():
                raise InvalidTranslationResponseError('本地模型返回空响应，未生成有效字幕。')
            text = parsed_single[0] if parsed_single else single.splitlines()[0].strip()
            result.append(text)
            publish(offset+index, text)
        return result

    try:
        return run(lines, 0)
    finally:
        if metrics is not None:
            metrics.diagnostic_indices = original_indices
        if budget_token is not None:
            _OLLAMA_SPLIT_BUDGET.reset(budget_token)


_DIALOGUE_WORDS = frozenset("""a an the i you he she it we they me us my your his her our their is are was were
be been am do does did don't not no yes and or but if of to for from with in on
at this that these those what who why how when where somehow something nothing
hello goodbye thanks thank please sorry okay ok sure right really wait help
come go stop look listen label after before always never now then here there
i'm i'll i've i'd you're you'll you've he's she's it's we're we'll we've they're
don't doesn't didn't can't cannot won't isn't aren't wasn't weren't couldn't
""".split())

_CJK_TEXT_RUN = re.compile(
    r"[\u3005-\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0002fa1f\U00030000-\U000323af"
    r"\u3041-\u3096\u3099-\u309f\u30a1-\u30fa\u30fc-\u30ff\u31f0-\u31ff\uff66-\uff9f"
    r"\u1100-\u11ff\u3131-\u318e\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]+"
)



_IDENTIFIER_QUOTE = r"""["'“”‘’]"""
_IDENTIFIER_LETTER = rf"{_IDENTIFIER_QUOTE}*\s*([A-Z])\s*{_IDENTIFIER_QUOTE}*"
_IDENTIFIER_MATH_OPERATORS = {
    "times": "*", "multipliedby": "*", "乘以": "*", "乘": "*", "×": "*", "*": "*",
    "plus": "+", "加上": "+", "加": "+", "+": "+",
    "minus": "-", "减去": "-", "减": "-",
    "dividedby": "/", "除以": "/", "÷": "/", "/": "/",
    "equals": "=", "等于": "=", "=": "=",
}
_IDENTIFIER_MATH_OPERATOR = (
    r"multiplied\s+by|divided\s+by|times|plus|minus|equals|乘以|乘|加上|加|减去|减|除以|等于|[×*+÷/=]"
)
_IDENTIFIER_LABELS = {
    "platform": ("站台", "月台"),
    "checkpoint": ("检查点", "检查站", "哨卡"),
    "point": ("点位", "位置", "地点", "点"),
    "plan": ("计划", "方案"), "option": ("选项",),
    "section": ("区段", "区域", "节"), "zone": ("区域", "区"),
    "route": ("路线",), "gate": ("登机口", "闸口", "门"),
    "block": ("街区", "区块"), "room": ("房间", "室"),
}


def _identifier_roles(text: str, *, translated: bool) -> list[tuple[tuple, tuple[tuple[int, int], ...]]]:
    """Return explicit identifier occurrences, strongest context first.

    An occurrence is consumed once; protecting one A never protects another A/a.
    Bare capitals and quoted I (a possible pronoun) have no blanket exemption.
    """
    # These patterns distinguish zero/one whitespace and at most two adjacent
    # quote delimiters (a closing and an opening quote), never longer runs.
    # Compact redundant runs before regex scanning to avoid quadratic suffix
    # retries on damaged subtitle text; return letter spans in original indices.
    compact: list[str] = []
    positions: list[int] = []
    run_kind, run_length = '', 0
    for position, character in enumerate(text):
        kind = 'space' if character.isspace() else 'quote' if character in "\"'“”‘’" else ''
        run_length = run_length + 1 if kind and kind == run_kind else 1
        run_kind = kind
        if (kind == 'space' and run_length > 1) or (kind == 'quote' and run_length > 2):
            continue
        compact.append(character)
        positions.append(position)
    text = ''.join(compact)
    result: list[tuple[tuple, tuple[tuple[int, int], ...]]] = []
    occupied: set[int] = set()

    def add(key: tuple, spans: tuple[tuple[int, int], ...]) -> None:
        positions = {position for start, end in spans for position in range(start, end)}
        if not positions or positions & occupied:
            return
        result.append((key, spans))
        occupied.update(positions)

    for match in re.finditer(r"(?<![A-Za-z])(?:[A-Z]\.){2,}(?![A-Za-z])", text):
        add(("initialism", match.group(0)), ((match.start(), match.end()),))
    formula = rf"(?<![A-Za-z]){_IDENTIFIER_LETTER}(?:\s*(?:{_IDENTIFIER_MATH_OPERATOR})\s*{_IDENTIFIER_LETTER})+(?![A-Za-z])"
    for match in re.finditer(formula, text):
        value = match.group(0)
        letters = tuple(re.finditer(r"(?<![A-Za-z])[A-Z](?![A-Za-z])", value))
        operators = tuple(_IDENTIFIER_MATH_OPERATORS[re.sub(r"\s+", "", item.group(0))]
                          for item in re.finditer(_IDENTIFIER_MATH_OPERATOR, value))
        if len(operators) == len(letters) - 1:
            add(("formula", tuple(item.group(0) for item in letters), operators),
                tuple((match.start() + item.start(), match.start() + item.end()) for item in letters))
    for role, names in _IDENTIFIER_LABELS.items():
        if translated:
            label = "(?:" + "|".join(map(re.escape, names)) + ")"
            patterns = (rf"{label}\s*{_IDENTIFIER_LETTER}(?![A-Za-z])",
                        rf"(?<![A-Za-z]){_IDENTIFIER_LETTER}\s*{label}")
        else:
            patterns = (rf"(?<![A-Za-z])(?i:{role})\s+{_IDENTIFIER_LETTER}(?![A-Za-z])",)
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                add(("label", role, match.group(1)), (match.span(1),))
    quoted = rf"{_IDENTIFIER_QUOTE}\s*(A)\s*[,，]?{_IDENTIFIER_QUOTE}"
    for match in re.finditer(quoted, text):
        add(("quoted_letter", match.group(1)), (match.span(1),))
    return [(key, tuple((positions[start], positions[end - 1] + 1) for start, end in spans))
            for key, spans in result]


def _translation_identifier_spans(source: str, translation: str, *, excluded_source_intervals=()) -> set[int]:
    """Match source and translated identifier roles with occurrence budgets."""
    budgets: dict[tuple, int] = {}
    for key, spans in _identifier_roles(source, translated=False):
        if any(start < right and left < end for start, end in spans
               for left, right in excluded_source_intervals):
            continue
        budgets[key] = budgets.get(key, 0) + 1
    protected: set[int] = set()
    for key, spans in _identifier_roles(translation, translated=True):
        if budgets.get(key, 0) <= 0:
            continue
        budgets[key] -= 1
        protected.update(position for start, end in spans for position in range(start, end))
    return protected


_ENTITY_LETTERS = r'A-Za-z\u00c0-\u024f\u1e00-\u1eff'
_ENTITY_CAPITAL = r'A-Z\u00c0-\u00d6\u00d8-\u00de\u0100-\u024f\u1e00-\u1eff'
_ENTITY_WORD = rf'[{_ENTITY_CAPITAL}][{_ENTITY_LETTERS}0-9]*(?:[\x27’.-][{_ENTITY_LETTERS}0-9]+)*\.?'
_ENTITY_CONNECTORS = frozenset(('and', 'of', 'the', 'de', 'del', 'van', 'von', 'der', 'da', 'di'))
_ENTITY_PHRASE = rf'{_ENTITY_WORD}(?:\s+(?:(?i:and|of|the|de|del|van|von|der|da|di)\s+)?{_ENTITY_WORD}){{0,5}}'
_ENTITY_ROLES = (r'company|corporation|corp|inc|ltd|llc|agency|organization|organisation|bureau|'
    r'department|division|foundation|association|institute|university|committee|council|'
    r'band|brand|project|operation|program|programme|film|movie|book|novel|song|album|series|'
    r'team|unit|club|business|group|office|officials|staff|agents|embassy|consulate|'
    r'callsign|call[- ]sign|password')
_ENTITY_HONORIFICS = (r'mr|mrs|ms|miss|dr|doctor|prof|professor|captain|agent|officer|'
    r'detective|general|sergeant|sir|lady|father|sister')
# Grammatical/imperative phrases must not become names just because subtitles
# happen to use Title Case. These are language roles, never a list of names.
_ENTITY_DIALOGUE = frozenset(('i you he she it we they me us my your his her our their '
    'is are was were be been do does did not no yes this that these those what who '
    'why how when where hello goodbye thanks thank please sorry okay ok sure '
    'really wait help come go stop look listen somehow something nothing '
    'take call give get put tell say said told ask asked want need bring let '
    'leave run open close love like should could would must can will may '
    'here there now then welcome').split())
_ENTITY_PREDICATES = frozenset(('said says say told tells asked asks is are was were '
    'has have had does did will would can could should must').split())
_ENTITY_PATTERNS = (
    re.compile(rf'(?<![{_ENTITY_LETTERS}])(?i:{_ENTITY_HONORIFICS})(?:\.\s*|\s+)["“\x27‘]?({_ENTITY_PHRASE})'),
    re.compile(rf'(?<![{_ENTITY_LETTERS}])(?i:(?:name(?:\s+(?:is|was)|[\x27’]s)|named|called|titled|known\s+as|'
               rf'goes\s+by|codenamed|code[- ]named|codename\s+(?:is|was)))\s+["“\x27‘]?({_ENTITY_PHRASE})'),
    re.compile(rf'(?<![{_ENTITY_LETTERS}])(?i:{_ENTITY_ROLES})\.?\s+(?:(?i:called|named|is|was)\s+)?'
               rf'["“\x27‘]?({_ENTITY_PHRASE})'),
    re.compile(rf'(?<![{_ENTITY_LETTERS}])({_ENTITY_PHRASE})\s+(?i:{_ENTITY_ROLES})(?![{_ENTITY_LETTERS}])'),
)
_ENTITY_GENERIC = re.compile(rf'(?<![{_ENTITY_LETTERS}0-9])({_ENTITY_PHRASE})(?![{_ENTITY_LETTERS}0-9])')
_ENTITY_TOKEN = re.compile(rf'[{_ENTITY_LETTERS}]+(?:[\x27’][{_ENTITY_LETTERS}]+)?')


def _translation_name_evidence(source: str, translation: str, *, include_noncolliding=False):
    """Protect exact source name occurrences, not every use of their words.

    Explicit titles/naming/entity roles support ambiguous words. A conservative
    multiword fallback preserves existing mixed-case proper names. Case alone
    does not grant an all-caps sentence name status. Unclear cases retain the
    normal warning, and raw source/cache/review identities are never rewritten.
    """
    if max(len(source), len(translation)) > 16384:
        return set(), []  # Damaged, enormous cues get no speculative name exemption.
    if not any(character.isupper() for character in translation):
        return set(), []
    from bisect import bisect_left, insort
    from collections import deque
    entities: dict[str, set[tuple[int, int]]] = {}

    def add(value: str, start: int, end: int):
        if value.endswith('.') and '.' not in value[:-1]:
            value, end = value[:-1], end - 1
        if not value or len(value) > 512:
            return
        # The existing identifier matcher already owns dotted initials/formulas;
        # a second matcher must not spend the same source occurrence twice.
        identifier_roles = _identifier_roles(value, translated=False)
        identifier_positions = {position for _, spans in identifier_roles
                                for left, right in spans for position in range(left, right)}
        word_positions = {position for match in _ENTITY_TOKEN.finditer(value)
                          for position in range(*match.span())}
        if word_positions and word_positions <= identifier_positions:
            return
        if (not include_noncolliding
                and not ({word.casefold() for word in _ENTITY_TOKEN.findall(value)} & _DIALOGUE_WORDS)):
            return  # No common-word collision needs a name exemption.
        entities.setdefault(value, set()).add((start, end))
        if value.startswith('The ') and len(value) > 4:
            entities.setdefault(value[4:], set()).add((start, end))
        # A translated leading component can leave an intact multiword suffix
        # (for example a brand/model). Its source occurrence is still spent once.
        for suffix in re.finditer(r'(?<=\s)(?=[A-Z])', value):
            alias = value[suffix.start():]
            words = _ENTITY_TOKEN.findall(alias)
            if len([word for word in words if word.casefold() not in _ENTITY_CONNECTORS]) >= 2:
                entities.setdefault(alias, set()).add((start, end))

    for pattern_index, pattern in enumerate(_ENTITY_PATTERNS):
        position = 0
        while (match := pattern.search(source, position)) is not None:
            value, start, end = match[1], *match.span(1)
            if (pattern_index == 2 and source[start-1:start] not in '"“\x27‘'
                    and _ENTITY_TOKEN.findall(value)[0].casefold() in _ENTITY_PREDICATES):
                position = match.start() + 1
                continue
            if start == 0 or source[start-1] not in '"“\x27‘':
                tokens = list(_ENTITY_TOKEN.finditer(value))
                for token in tokens[1:]:
                    if (token[0].casefold() in _ENTITY_PREDICATES
                            or token[0].casefold() in _ENTITY_HONORIFICS.split('|')):
                        value = value[:token.start()].rstrip()
                        # Separate people joined by "and Mr..." are not one name.
                        value = re.sub(r'\s+(?i:and)$', '', value)
                        end = start + len(value)
                        break
            add(value, start, end)
            position = match.start() + 1
    # Initials plus a surname are a name pattern, not a list of approved people.
    for match in re.finditer(rf'(?<![{_ENTITY_LETTERS}])((?:[A-Z]\.\s*){{1,4}}'
                             rf'[{_ENTITY_CAPITAL}][{_ENTITY_LETTERS}]{{1,80}})(?![{_ENTITY_LETTERS}])', source):
        add(match[1], *match.span(1))
    # Bare Title Case is an existing narrow common-word name heuristic. The
    # recovery branch formerly allowed no Latin at all; its new exceptions
    # require positive role evidence rather than extending this heuristic to
    # unknown ordinary words such as title-cased verbs/adjectives.
    for match in (() if include_noncolliding else _ENTITY_GENERIC.finditer(source)):
        value = match[1]
        words = _ENTITY_TOKEN.findall(value)
        substantive = [word for word in words if word.casefold() not in _ENTITY_CONNECTORS]
        if (len(substantive) < 2
                or any(word.casefold() in _ENTITY_DIALOGUE for word in substantive)
                or substantive[0].casefold() in _IDENTIFIER_LABELS
                or not any(any(character.islower() for character in word) for word in substantive)
                or ('and' in {word.casefold() for word in words} and not value.startswith('The '))):
            continue
        add(value, *match.span(1))

    if include_noncolliding:
        # This stricter evidence view is used by the bounded dialogue-recovery
        # branch. Normal mixed-case prose can identify embedded proper tokens,
        # acronyms and digit/camel-case codes; all-caps ordinary cues cannot.
        normal_prose = any(character.islower() for character in source)
        for match in re.finditer(rf'(?<![{_ENTITY_LETTERS}0-9])'
                                 rf'[{_ENTITY_LETTERS}0-9]+(?:[\x27’.-][{_ENTITY_LETTERS}0-9]+)*'
                                 rf'(?![{_ENTITY_LETTERS}0-9])', source):
            value = match[0]
            if value.casefold() in _ENTITY_DIALOGUE | _DIALOGUE_WORDS:
                continue
            letters = [character for character in value if character.isalpha()]
            code = any(character.isdigit() for character in value) and any(character.isupper() for character in value)
            camel = any(character.isupper() for character in value[1:]) and any(character.islower() for character in value)
            action_subject = re.match(
                r'\s+(?i:arrived|called|said|asked|replied|smiled|left|came|went|sent|'
                r'announced|reported|issued|approved|confirmed|responded)\b', source[match.end():]) is not None
            communication_object = re.search(
                r'(?i:\b(?:contact|notify|call|ask|tell|inform|email|write\s+to)\s+(?:the\s+)?)$',
                source[:match.start()]) is not None
            acronym = (len(letters) >= 2 and all(character.isupper() for character in letters)
                       and normal_prose and (action_subject or communication_object))
            person_subject = (value[0].isupper() and any(character.islower() for character in value)
                              and action_subject)
            if code or camel or acronym or person_subject:
                add(value, *match.span())

    candidates = []
    pools = {}
    for value, anchors in entities.items():
        pools[value] = deque(sorted(anchors))
        pattern = rf'(?<![{_ENTITY_LETTERS}0-9]){re.escape(value)}(?![{_ENTITY_LETTERS}0-9])'
        for match in re.finditer(pattern, translation):
            candidates.append((match.start(), match.end(), value))
    protected: set[int] = set()
    spent: list[tuple[int, int]] = []

    def overlaps(anchor):
        position = bisect_left(spent, (anchor[0],))
        return ((position > 0 and spent[position-1][1] > anchor[0])
                or (position < len(spent) and spent[position][0] < anchor[1]))

    # Prefer the longest name at a position; aliases share overlapping anchors.
    for start, end, value in sorted(candidates, key=lambda row: (row[0], row[0] - row[1])):
        if any(position in protected for position in range(start, end)):
            continue
        pool = pools[value]
        while pool and overlaps(pool[0]):
            pool.popleft()
        if not pool:
            continue
        insort(spent, pool.popleft())
        protected.update(range(start, end))
    return protected, spent


def _translation_name_spans(source: str, translation: str, *, include_noncolliding=False) -> set[int]:
    return _translation_name_evidence(source, translation, include_noncolliding=include_noncolliding)[0]


def _translation_preserved_spans(source: str, translation: str, *, include_noncolliding=False) -> set[int]:
    """Share source occurrence ownership between names and identifier roles."""
    names, spent = _translation_name_evidence(source, translation,
                                              include_noncolliding=include_noncolliding)
    return names | _translation_identifier_spans(source, translation, excluded_source_intervals=spent)


def translation_quality_issue(source: str, translation: str, target_code: str) -> str:
    """Catch empty translations and narrow script leaks while preserving source names.

    Retained CJK source text may be a name or quote; only newly introduced CJK
    spans are rejected for supported non-CJK targets. This does not verify meaning.
    """
    source = subtitle_text_formats.translation_text(source)
    translation = subtitle_text_formats.translation_text(translation)
    if not source.strip():
        return ""
    if not translation.strip():
        return "译文为空"
    if target_code in _NON_CJK_TRANSLATION_TARGETS:
        if any(match.group(0) not in source for match in _CJK_TEXT_RUN.finditer(translation)):
            return "译文混入原文没有的中日韩文字，疑似错译"
        return ""
    if not target_code.startswith("zh"):
        return ""
    source_words = {word.casefold() for word in re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", source)}
    translated_tokens = list(re.finditer(r"[A-Za-z]+(?:['’][A-Za-z]+)?", translation))
    collisions = [match for match in translated_tokens if match[0].casefold() in source_words & _DIALOGUE_WORDS]
    protected_spans = (_translation_preserved_spans(source, translation) if collisions
                       else _translation_identifier_spans(source, translation))
    if any(not all(position in protected_spans for position in range(*match.span())) for match in collisions):
        return "中文译文残留原文常用词，疑似漏译"
    if re.search(r"[\u3400-\u9fff]", translation):
        return ""
    # The view has removed recognized display syntax already. Unknown braces
    # and angle-bracket text remain dialogue and must still be checked.
    plain = source.strip()
    words = re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?", plain)
    if not words:
        if not re.search(r"[^\W\d_]", plain):
            return ""  # Music marks, punctuation and numbers have no words to translate.
        return "中文译文仍为外文，疑似漏译"
    same = re.sub(r"\W", "", plain).casefold() == re.sub(r"\W", "", translation).casefold()
    if same and translated_tokens and all(all(position in protected_spans
                                             for position in range(*match.span()))
                                         for match in translated_tokens):
        return ""  # A whole, unchanged name/initialism also need not become Chinese.
    if same and not any(word.casefold() in _DIALOGUE_WORDS for word in words):
        if len(words) <= 3 and all(word[0].isupper() for word in words):
            return ""  # Standalone names (Chloe, Darth Vader) and identifiers (R2).
    return "中文译文仍为外文，疑似漏译"


TRANSLATION_PARSER_VERSION = "explicit-numbering-v2"


def load_cache(
    path: Path,
    target_code: str,
    sources: dict[int, str],
) -> tuple[dict[int, str], int]:
    cache: dict[int, str] = {}
    stale_indices: set[int] = set()
    if not path.exists():
        return cache, 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if item.get("target") != target_code:
                continue
            index = int(item["index"])
            current_source = sources.get(index)
            cached_source = item.get("source")
            if current_source is None or not isinstance(cached_source, str) or cached_source != current_source:
                stale_indices.add(index)
                cache.pop(index, None)
                continue
            translation = str(item["translation"])
            if translation_quality_issue(current_source, translation, target_code):
                stale_indices.add(index)
                cache.pop(index, None)
                continue
            if (item.get("parser_version") != TRANSLATION_PARSER_VERSION
                    and re.search(r"\d", current_source)):
                stale_indices.add(index)
                cache.pop(index, None)
                continue
            cache[index] = translation
            stale_indices.discard(index)
        except (ValueError, KeyError, TypeError):
            continue
    return cache, len(stale_indices)


def append_cache(path: Path, target_code: str, index: int, source: str, translation: str) -> None:
    append_cache_records(path, target_code, [(index, source, translation)])


def append_cache_records(
    path: Path,
    target_code: str,
    records: list[tuple[int, str, str]],
) -> None:
    if not records:
        return
    payload = "".join(
        json.dumps(
            {"target": target_code, "index": index, "source": source, "translation": translation,
             "parser_version": TRANSLATION_PARSER_VERSION},
            ensure_ascii=False,
        )
        + "\n"
        for index, source, translation in records
    )

    def write_record() -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(payload)

    retry_file_operation(write_record, path, "append translation cache")


def retry_file_operation(
    operation: Callable[[], None],
    path: Path,
    description: str,
    delays: tuple[float, ...] = (0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0),
) -> None:
    """Retry transient Windows file-sharing/permission failures for a bounded time."""
    for delay in (*delays, None):
        try:
            operation()
            return
        except PermissionError as exc:
            if delay is None:
                raise PermissionError(
                    f"{description} failed after {len(delays) + 1} attempts: {path}"
                ) from exc
            time.sleep(delay)


def rewrite_cache(
    path: Path,
    target_code: str,
    sources: dict[int, str],
    translations: dict[int, str],
) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        for index in sorted(sources):
            if index not in translations:
                continue
            handle.write(
                json.dumps(
                    {
                        "target": target_code,
                        "index": index,
                        "source": sources[index],
                        "translation": translations[index],
                        "parser_version": TRANSLATION_PARSER_VERSION,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    retry_file_operation(lambda: temp_path.replace(path), path, "replace translation cache")


def _rechecked_identifier_reviews(review_rows: dict[int, dict], target_code: str) -> dict[int, str]:
    """Recheck former label/format false positives without changing user text."""
    if not target_code.startswith("zh"):
        return {}
    import translation_review
    result: dict[int, str] = {}
    for index, item in review_rows.items():
        if (item.get("status") != "needs_review"
                or item.get("reason") != "中文译文残留原文常用词，疑似漏译"):
            continue
        source, translation = item.get("source"), item.get("translation")
        if not isinstance(source, str) or not isinstance(translation, str):
            continue
        try:
            translation_review.validate_text(translation)
        except (ValueError, TypeError):
            continue
        plain_source = subtitle_text_formats.translation_text(source)
        plain_translation = subtitle_text_formats.translation_text(translation)
        if ((_translation_identifier_spans(plain_source, plain_translation)
                or _translation_name_spans(plain_source, plain_translation)
                or plain_source != source or plain_translation != translation)
                and not translation_quality_issue(source, translation, target_code)):
            result[index] = translation
    return result


def _translation_cache_state(
    events: list[SubtitleEvent],
    target_code: str,
    cache_path: Path,
    retry_issues: bool = False,
    output_srt: Path | None = None,
) -> tuple[dict[int, str], dict[int, str], int, list[int]]:
    cleaned = {idx: event.text.strip() for idx, event in enumerate(events, start=1)}
    cache, stale_count = load_cache(cache_path, target_code, cleaned)
    import translation_review
    review_rows = translation_review.restore_issues(events, target_code, cache_path, output_srt)
    if output_srt is not None:
        for index, translation in _rechecked_identifier_reviews(review_rows, target_code).items():
            cache.setdefault(index, translation)
    pending = [idx for idx in range(1, len(events) + 1)
               if idx not in cache and (idx not in review_rows or (retry_issues and review_rows[idx].get('status') == 'needs_review')) and cleaned[idx]]
    return cleaned, cache, stale_count, pending


def needs_model_translation(
    events: list[SubtitleEvent],
    target: str,
    cache_path: Path,
    source_code: str,
    retry_issues: bool = False,
    output_srt: Path | None = None,
) -> bool:
    """Use the exact translation-cache rules before reserving model resources."""
    source_language = subtitle_languages.subtitle_language(source_code)
    target_language = subtitle_languages.subtitle_language(target)
    if source_language == target_language:
        return False
    if chinese_script_converter.is_script_conversion(source_language, target_language):
        return False
    return bool(_translation_cache_state(events, target, cache_path, retry_issues, output_srt)[3])


def translate_events(
    events: list[SubtitleEvent],
    output_srt: Path,
    cache_path: Path,
    target_code: str,
    source_language: str,
    log: Callable[[str], None],
    model: str = DEFAULT_MODEL,
    host: str = OLLAMA_HOST,
    batch_size: int = 12,
    timeout: int = 600,
    cancel_event: threading.Event | None = None,
    retry_issues: bool = False,
) -> None:
    import translation_review
    check_cancel(cancel_event)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    output_srt.parent.mkdir(parents=True, exist_ok=True)
    cleaned, cache, stale_count, pending = _translation_cache_state(events, target_code, cache_path, retry_issues, output_srt)
    issues = translation_review.restore_issues(events, target_code, cache_path, output_srt)
    had_issue_report = bool(issues)
    retry_indices = {idx for idx, item in issues.items()
                     if retry_issues and item.get('status') == 'needs_review' and idx not in cache and cleaned[idx]}
    pending = [idx for idx in range(1, len(events) + 1)
               if idx not in cache and (idx not in issues or idx in retry_indices) and cleaned[idx]]
    translations = {index: item['translation'] for index, item in issues.items()}
    translations.update(cache)
    rechecked = _rechecked_identifier_reviews(issues, target_code)
    rechecked_count = 0
    for index, item in issues.items():
        if index in cache and cache[index] != item['translation']:
            item.update(translation=cache[index], reason='', status='resolved')
        elif index in rechecked and index in cache and cache[index] == item['translation']:
            item.setdefault('original_reason', item['reason'])
            format_changed = any(subtitle_text_formats.translation_text(item[key]) != item[key]
                                 for key in ('source', 'translation'))
            name_rechecked = bool(_translation_name_spans(
                subtitle_text_formats.translation_text(item['source']),
                subtitle_text_formats.translation_text(item['translation'])))
            item.update(reason='', status='resolved', resolution=(
                'display-format-rechecked-v1' if format_changed else
                'source-name-context-rechecked-v1' if name_rechecked else 'identifier-context-rechecked-v1'))
            rechecked_count += 1
    if rechecked_count:
        log(f"旧翻译提醒复核：{rechecked_count}条已确认显示格式标记或保留名称、变量、编号、缩写；复用原译文，不重新请求模型。")
    log(
        f"字幕行数：{len(events)}，缓存匹配：{len(cache)}，"
        f"缓存失效：{stale_count}，待翻译：{len(pending)}"
    )
    if issues:
        if retry_indices:
            log(f"翻译问题记录：{len(issues)}条；本轮重试未解决的{len(retry_indices)}条，已人工确认的译文保持。")
        else:
            unresolved_reviews = sum(item.get('status') == 'needs_review' for item in issues.values())
            if unresolved_reviews:
                log(f"复用问题译文记录：{unresolved_reviews}条；仍保留质量标记，不作为正常翻译缓存。")

    started = time.monotonic()
    cursor = 0
    metrics = TranslationMetrics()
    metrics.diagnostic_log = log

    def save_validated(records: list[tuple[int, str, str]]) -> None:
        changed = [record for record in records if cache.get(record[0]) != record[2]]
        if changed:
            append_cache_records(cache_path, target_code, changed)
            cache.update((index, text) for index, _source, text in changed)
        for index, _source, _text in records:
            issues.pop(index, None)

    try:
        while cursor < len(pending):
            check_cancel(cancel_event)
            batch_indices = pending[cursor : cursor + batch_size]
            batch_lines = [cleaned[idx] for idx in batch_indices]

            def save_fallback(offset: int, text: str) -> None:
                # The next single-line fallback can fail before translate_batch
                # returns. Persist only already validated results from it.
                check_cancel(cancel_event)
                idx = batch_indices[offset]
                try:
                    text = translation_review.validate_text(text.strip().strip('"').strip("'"))
                except ValueError:
                    return
                if not translation_quality_issue(cleaned[idx], text, target_code):
                    save_validated([(idx, cleaned[idx], text)])

            metrics.diagnostic_indices = tuple(batch_indices)
            translated = translate_batch(
                batch_lines, target_code, source_language, model, host, timeout,
                cancel_event=cancel_event, metrics=metrics,
                on_fallback_translation=save_fallback,
            )
            check_cancel(cancel_event)
            if len(translated) != len(batch_indices):
                raise RuntimeError(f"翻译返回行数不一致：{len(translated)} / {len(batch_indices)}")
            recovered: dict[int, str] = {}
            recovery_tried: set[int] = set()
            recoverable_indices = set(batch_indices) - (set(issues) - retry_indices)
            for idx, text in zip(batch_indices, translated):
                text = recovered.get(idx, text)
                if not isinstance(text, str):
                    raise RuntimeError(f"字幕第 {idx} 行模型响应损坏，未生成有效字幕。")
                text = text.strip().strip('"').strip("'")
                try:
                    text = translation_review.validate_text(text)
                except ValueError as exc:
                    raise RuntimeError(f"字幕第 {idx} 行：{exc}") from exc
                issue = translation_quality_issue(cleaned[idx], text, target_code)
                initial_reason = issue
                repair_errors = []
                generation_failed = False
                repair_attempts = 0
                for repair in range(2):
                    if not issue:
                        break
                    check_cancel(cancel_event)
                    log(f"字幕第 {idx} 行：{issue}；仅重译本行（{repair + 1}/2）。")
                    metrics.quality_repairs += 1
                    repair_attempts += 1
                    try:
                        metrics.diagnostic_indices = (idx,)
                        repaired = translate_batch(
                            [cleaned[idx]], target_code, source_language, model, host, timeout,
                            cancel_event=cancel_event, metrics=metrics,
                            require_target_script=True,
                            context_lines=(cleaned.get(idx - 1, ""), cleaned.get(idx + 1, "")),
                            repair_feedback=(f"{issue}. Faulty translation: {text}" if repair else ""),
                        )
                    except TranslationGenerationError as exc:
                        check_cancel(cancel_event)
                        generation_failed = True
                        repair_errors.append(str(exc))
                        log(f"字幕第 {idx} 行：{exc}；改变修复方式。")
                        break
                    except InvalidTranslationResponseError as exc:
                        check_cancel(cancel_event)
                        repair_errors.append(str(exc))
                        log(f"字幕第 {idx} 行修复响应无效：{exc}；保留上一份有效译文。")
                        continue
                    check_cancel(cancel_event)
                    candidate = repaired[0] if isinstance(repaired, (list, tuple)) and len(repaired) == 1 else ""
                    if isinstance(candidate, str):
                        candidate = candidate.strip().strip('"').strip("'")
                    try:
                        candidate = translation_review.validate_text(candidate)
                    except ValueError as exc:
                        repair_errors.append(str(exc))
                        log(f"字幕第 {idx} 行修复响应无效：{exc}；保留上一份有效译文。")
                        continue
                    text = candidate
                    issue = translation_quality_issue(cleaned[idx], text, target_code)
                recovery_diagnostics = []
                if (issue and (generation_failed or idx in retry_indices) and target_code in {"zh-CN", "zh-TW"}
                        and (source_language or "").casefold() in {"en", "eng", "english"}
                        and idx not in recovery_tried):
                    repair_attempts += 1
                    import translation_recovery
                    available = recoverable_indices | (set(cache) if idx in retry_indices else set())
                    available -= set(issues) - {idx}
                    indices = translation_recovery.window_indices(idx, available, cleaned)
                    recovery_tried.update(indices)
                    log(f"字幕第 {idx} 行：{issue}；使用现有模型恢复连续对白（第 {indices[0]}—{indices[-1]} 行）。")
                    try:
                        def recovery_request(prompt, schema):
                            return call_ollama_retry(
                                prompt, model=model, host=host, timeout=timeout,
                                cancel_event=cancel_event, metrics=metrics,
                                response_schema=schema, max_tokens=1536,
                            )
                        segments = translation_recovery.recover(
                            [subtitle_text_formats.translation_text(cleaned[i]) for i in indices], LANGUAGES[target_code][2],
                            recovery_request, recovery_diagnostics, log)
                        check_cancel(cancel_event)
                        for i, segment in zip(indices, segments):
                            translation_review.validate_text(segment)
                            if translation_quality_issue(cleaned[i], segment, target_code):
                                raise ValueError("恢复译文仍未通过目标语言检查。")
                    except (ValueError, InvalidTranslationResponseError) as exc:
                        repair_errors.append(str(exc))
                        log(f"字幕连续对白恢复未采用：{exc}；本行保留质量标记，继续处理其他字幕。")
                    else:
                        save_validated([(i, cleaned[i], segment) for i, segment in zip(indices, segments)])
                        for i, segment in zip(indices, segments):
                            recovered[i] = translations[i] = segment
                        text = recovered[idx]
                        issue = ""
                        metrics.context_repairs += 1
                        log(f"字幕第 {indices[0]}—{indices[-1]} 行：连续对白恢复完成，原时间戳保持。")
                if issue:
                    event = events[idx - 1]
                    issues[idx] = dict(index=idx, start=event.start, end=event.end,
                        source=cleaned[idx], translation=text, reason=issue,
                        original_reason=initial_reason, status='needs_review', repair_attempts=repair_attempts)
                    if repair_errors:
                        issues[idx]['repair_errors'] = repair_errors
                    if recovery_diagnostics:
                        issues[idx]['recovery_diagnostics'] = recovery_diagnostics
                    log(f"字幕第 {idx} 行质量提醒：{issue}；原文：{cleaned[idx]}；译文：{text}；"
                        "自动修复后仍有问题，已标记供编辑，继续生成字幕。")
                    translation_review.save_progress(events, target_code, source_language,
                                                     cache_path, output_srt, issues)
                else:
                    save_validated([(idx, cleaned[idx], text)])
                translations[idx] = text
            cursor += len(batch_indices)
            elapsed = max(0.001, time.monotonic() - started)
            done = len(translations)
            # Reused rows took no time this run and must not inflate throughput.
            remaining = int(max(0, (len(pending) - cursor) * elapsed / cursor))
            log(f"翻译进度：{done}/{len(events)}，预计剩余 {remaining} 秒")
    finally:
        if metrics.requests:
            metrics.translation_wall_seconds = max(0.0, time.monotonic() - started)
            log(f"{LANGUAGES.get(target_code, ('', '', target_code))[2]}：{metrics.summary()}")

    for idx in range(1, len(events) + 1):
        translations.setdefault(idx, "")
        if cleaned[idx]:
            translation_review.validate_text(translations[idx])
        issue = translation_quality_issue(cleaned[idx], translations[idx], target_code)
        if issue and idx not in issues:
            raise RuntimeError(f"字幕第 {idx} 行缺少质量问题记录，未写入成品。")
    check_cancel(cancel_event)
    rewrite_cache(cache_path, target_code, cleaned, cache)
    if issues or had_issue_report:
        translation_review.save_progress(events, target_code, source_language, cache_path,
                                         output_srt, issues, complete=True, translations=translations)
        unresolved = sum(item.get('status') == 'needs_review' for item in issues.values())
        log(f"翻译问题记录：{translation_review.report_path(cache_path)}；仍待修改{unresolved}条。")
    else:
        write_srt(output_srt, events, translations)
    log(f"写入新字幕：{output_srt}")


def make_output_path(input_path: str, output_dir: str | None, suffix: str = ".standard") -> str:
    source = Path(input_path)
    directory = Path(output_dir) if output_dir else source.parent
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{source.stem}{suffix}.mkv")


def select_default_subtitle(
    input_media: dict,
    keep_subtitle_ids: list[int],
    generated_subtitles: list[tuple[Path, str, str, bool]],
    language_preferences: list[str],
    chinese_script_equivalent: bool = False,
) -> tuple[str, int] | None:
    """Choose one default across retained and generated subtitles in profile order."""
    preferences = list(dict.fromkeys(
        subtitle_languages.subtitle_language(code) for code in language_preferences
    ))
    candidates = []

    def add(kind, identity, language, name, was_default, partial=False):
        code = subtitle_languages.subtitle_language(language, name)
        preference_rank = len(preferences)
        match_rank = 3
        for index, preferred in enumerate(preferences):
            if code == preferred:
                preference_rank, match_rank = index, 0
                break
            if preferred in {"zh-CN", "zh-TW"} and code == "zh":
                preference_rank, match_rank = index, 2
                break
            if (
                chinese_script_equivalent
                and preferred in {"zh-CN", "zh-TW"}
                and code in {"zh-CN", "zh-TW"}
            ):
                preference_rank, match_rank = index, 1
                break
        candidates.append((
            (bool(partial), preference_rank, match_rank,
             subtitle_languages.chinese_dialect_rank(language, name) if preferences and code in {"zh-CN", "zh-TW", "zh", "yue"} else 0,
             not was_default, len(candidates)),
            (kind, identity),
        ))

    kept = set(keep_subtitle_ids)
    for track in input_media.get("tracks", []):
        if track.get("type") != "subtitles" or track.get("id") not in kept:
            continue
        props = track.get("properties", {})
        name = str(props.get("track_name") or "")
        partial = bool(props.get("forced_track")) or bool(re.search(
            r"forced|commentary|foreign[ ._-]*only|alien[ ._-]*only|强制|強制|评论|評論|解说|解說",
            name,
            re.IGNORECASE,
        ))
        add(
            "retained", int(track["id"]),
            str(props.get("language_ietf") or props.get("language") or "und"),
            name, bool(props.get("default_track")), partial,
        )
    for index, (_path, code, name, was_default) in enumerate(generated_subtitles):
        add("generated", index, code, name, was_default)
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def mux_video(
    input_path: str,
    output_path: str,
    keep_audio_ids: list[int],
    keep_subtitle_ids: list[int],
    generated_subtitles: list[tuple[Path, str, str, bool]],
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
    preserve_audio_defaults: bool = False,
    subtitle_sync_offsets: dict[int, int] | None = None,
    subtitle_language_preferences: list[str] | None = None,
    chinese_script_equivalent: bool = False,
    input_media: dict | None = None,
    allow_fast_passthrough: bool = True,
) -> None:
    default_subtitle = None
    if subtitle_language_preferences is not None:
        default_subtitle = select_default_subtitle(
            input_media if input_media is not None else inspect_media(input_path),
            keep_subtitle_ids,
            generated_subtitles,
            subtitle_language_preferences,
            chinese_script_equivalent,
        )
        generated_subtitles = [
            (path, code, name, default_subtitle == ("generated", index))
            for index, (path, code, name, _default) in enumerate(generated_subtitles)
        ]
    import subtitle_fonts
    style_result = None
    if subtitle_fonts.CONTEXT.get():
        input_media = input_media if input_media is not None else inspect_media(input_path)
        style_result = subtitle_fonts.prepare_mux(
            input_path, output_path, input_media, keep_subtitle_ids, generated_subtitles,
            subtitle_sync_offsets, default_subtitle, log, cancel_event, chinese_script_equivalent,
        )
        if style_result:
            keep_subtitle_ids = style_result['kept']
            generated_subtitles = style_result['generated']
    args = [MKVMERGE, "-o", output_path]
    if style_result and style_result['fonts']:
        args += ['--enable-legacy-font-mime-types']
    if style_result and style_result['replacements']:
        order=[]
        for track in input_media['tracks']:
            track_id=track['id']
            if track['type']=='video' or (track['type']=='audio' and track_id in keep_audio_ids):
                order.append(f'0:{track_id}')
            elif track_id in style_result['replacements']:
                order.append(f'{style_result["replacements"][track_id]+1}:0')
            elif track['type']=='subtitles' and track_id in keep_subtitle_ids:
                order.append(f'0:{track_id}')
        original_generated_count=len(generated_subtitles)-len(style_result['replacements'])
        order += [f'{i+1}:0' for i in range(original_generated_count)]
        args += ['--track-order', ','.join(order)]
    if keep_audio_ids:
        args += ["--audio-tracks", ",".join(str(item) for item in keep_audio_ids)]
        if not preserve_audio_defaults:
            for index, track_id in enumerate(keep_audio_ids):
                args += ["--default-track", f"{track_id}:{'yes' if index == 0 else 'no'}"]
    else:
        args += ["--no-audio"]
    if keep_subtitle_ids:
        args += ["--subtitle-tracks", ",".join(str(item) for item in keep_subtitle_ids)]
        for track_id, offset_milliseconds in (subtitle_sync_offsets or {}).items():
            if track_id in keep_subtitle_ids and offset_milliseconds:
                args += ["--sync", f"{track_id}:{offset_milliseconds}"]
        if subtitle_language_preferences is not None:
            for track_id in keep_subtitle_ids:
                flag = 'yes' if default_subtitle == ("retained", track_id) else 'no'
                args += ["--default-track", f"{track_id}:{flag}"]
        elif any(item[3] for item in generated_subtitles):
            for track_id in keep_subtitle_ids:
                args += ["--default-track", f"{track_id}:no"]
    else:
        args += ["--no-subtitles"]
    args.append(input_path)

    for subtitle_path, lang_code, track_name, is_default in generated_subtitles:
        mkv_lang = LANGUAGES.get(lang_code, ("und", lang_code, lang_code))[0]
        props = style_result['metadata'].get(str(subtitle_path), {}) if style_result else {}
        mkv_lang = props.get('language_ietf') or props.get('language') or mkv_lang
        for key, option in {'forced_track':'--forced-display-flag', 'enabled_track':'--track-enabled-flag',
                'hearing_impaired':'--hearing-impaired-flag', 'visual_impaired':'--visual-impaired-flag',
                'text_descriptions':'--text-descriptions-flag', 'original':'--original-flag',
                'commentary':'--commentary-flag'}.items():
            if key in props: args += [option, f'0:{int(bool(props[key]))}']
        args += [
            "--language",
            f"0:{mkv_lang}",
            "--track-name",
            f"0:{track_name}",
            "--default-track",
            "0:yes" if is_default else "0:no",
            str(subtitle_path),
        ]

    if style_result:
        # Attach once per content hash, including when multiple languages use
        # the same font. Existing original attachments remain on the main input.
        for font in style_result['fonts']:
            mime='application/x-truetype-font' if Path(font['path']).suffix.lower()=='.ttf' else 'application/vnd.ms-opentype'
            if font.get('embed',True):
                args += ['--attachment-mime-type',mime,'--attachment-name',f'SubFlow-{font["sha256"][:16]}{Path(font["path"]).suffix.lower()}', '--attach-file',font['path']]
            for notice in font.get('notices',[]):
                args += ['--attachment-mime-type','text/plain','--attachment-name',f'SubFlow-{font["sha256"][:16]}-{Path(notice).name}', '--attach-file',notice]

    log(f"封装输出：{output_path}")
    check_cancel(cancel_event)
    fast_plan = mkv_fast_mux.assess(
        input_path, output_path, input_media, keep_audio_ids, keep_subtitle_ids,
        generated_subtitles, MKVMERGE, allowed=allow_fast_passthrough,
    )
    check_cancel(cancel_event)
    if not fast_plan.enabled:
        log(f"封装方式：原封装；{fast_plan.reason}。")
        run_command(args, log=log, cancel_event=cancel_event)
        return
    log(f"封装方式：{fast_plan.audio_kind} 快速直通；保持原音视频、字幕偏移、章节与附件。")
    fast_args = args[:3] + ["--engage", "force_passthrough_packetizer"] + args[3:]
    try:
        run_command(fast_args, log=log, cancel_event=cancel_event)
        check_cancel(cancel_event)
        output_media = inspect_media(output_path, cancel_event=cancel_event)
        audio_defaults = {} if preserve_audio_defaults else {
            track_id: index == 0 for index, track_id in enumerate(keep_audio_ids)
        }
        subtitle_defaults = {}
        if subtitle_language_preferences is not None:
            subtitle_defaults = {track_id: default_subtitle == ("retained", track_id)
                                 for track_id in keep_subtitle_ids}
        elif any(item[3] for item in generated_subtitles):
            subtitle_defaults = {track_id: False for track_id in keep_subtitle_ids}
        generated_specs = [(LANGUAGES.get(code, ("und", code, code))[0], name, default)
                           for _path, code, name, default in generated_subtitles]
        mkv_fast_mux.verify(fast_plan, output_path, output_media,
                            audio_defaults, subtitle_defaults, generated_specs)
        check_cancel(cancel_event)
        log("快速封装头检查通过：编码私有数据、杜比视界配置、章节、附件及字幕标记一致。")
    except CancelledError:
        raise
    except (RuntimeError, OSError, mkv_fast_mux.HeaderCheckError) as exc:
        check_cancel(cancel_event)
        if not mkv_fast_mux.retryable_failure(exc):
            raise
        log(f"快速封装未通过，回到原封装一次：{exc}")
        # This output is still temporary. Never re-run after user cancellation,
        # or publish a fast result whose critical metadata did not pass.
        pending_output = Path(output_path)
        for attempt in range(3):
            try:
                pending_output.unlink(missing_ok=True)
                break
            except OSError:
                if attempt == 2:
                    raise
                if cancel_event is not None:
                    cancel_event.wait(0.1 * (attempt + 1))
                else:
                    time.sleep(0.1 * (attempt + 1))
                check_cancel(cancel_event)
        check_cancel(cancel_event)
        run_command(args, log=log, cancel_event=cancel_event)


def remux_mkv_to_mp4(
    intermediate_mkv: str,
    output_path: str,
    log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> None:
    log(f"转换为 MP4 输出：{output_path}")
    args = [
        FFMPEG,
        "-y",
        "-i",
        intermediate_mkv,
        "-map",
        "0:v?",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-c",
        "copy",
        "-c:s",
        "mov_text",
        "-movflags",
        "+faststart",
        output_path,
    ]
    run_command(args, log=log, cancel_event=cancel_event)


def format_file_size(size: int) -> str:
    return f"{size / (1024 ** 3):.1f} GiB"


def partial_output_path(output_path: str) -> Path:
    output = Path(output_path)
    return output.with_name(f"{output.stem}.partial{output.suffix}")


def remove_incomplete_file(path: Path, log: Callable[[str], None] = log_noop) -> None:
    if not path.exists():
        return
    try:
        path.unlink()
        log(f"已清理未完成文件：{path}")
    except OSError as exc:
        log(f"未完成文件清理失败：{path}；原因：{exc}")


def ensure_output_disk_space(input_path: str, output_path: str, log: Callable[[str], None] = log_noop) -> None:
    source = Path(input_path).resolve()
    output = Path(output_path).resolve()
    if source == output:
        raise RuntimeError("输出文件不能覆盖原视频，请选择新的文件名。")
    output.parent.mkdir(parents=True, exist_ok=True)
    input_size = source.stat().st_size
    working_copies = 1
    if not is_matroska_family(input_path):
        working_copies += 1
    if output.suffix.lower() == ".mp4":
        working_copies += 1
    reserve = max(512 * 1024 * 1024, int(input_size * 0.05))
    required = input_size * working_copies + reserve
    free = shutil.disk_usage(output.parent).free
    log(f"磁盘空间预检：可用 {format_file_size(free)}，建议至少 {format_file_size(required)}")
    if free < required:
        raise RuntimeError(
            f"输出磁盘空间不足：当前可用 {format_file_size(free)}，"
            f"本次处理至少需要约 {format_file_size(required)}。请清理空间或更换输出位置。"
        )


def ensure_mux_disk_space(input_path: str, output_path: str, log: Callable[[str], None] = log_noop) -> None:
    """Recheck space immediately before muxing after potentially long OCR/translation work."""
    source = Path(input_path).resolve()
    output = Path(output_path).resolve()
    input_size = source.stat().st_size
    output_copies = 2 if output.suffix.lower() == ".mp4" else 1
    reserve = max(512 * 1024 * 1024, int(input_size * 0.05))
    required = input_size * output_copies + reserve
    free = shutil.disk_usage(output.parent).free
    log(f"封装前磁盘空间复检：可用 {format_file_size(free)}，至少需要约 {format_file_size(required)}")
    if free < required:
        raise RuntimeError(
            f"封装前发现输出磁盘空间不足：当前可用 {format_file_size(free)}，"
            f"至少需要约 {format_file_size(required)}。OCR 和翻译缓存已保留，"
            "请清理空间或更换输出位置后直接重试。"
        )


def media_duration_ns(data: dict) -> int:
    value = data.get("container", {}).get("properties", {}).get("duration", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def video_track_duration_ns(data: dict) -> int:
    """Return the primary video's tagged duration without trusting other tracks.

    A Matroska container duration is the end of its longest track. A source can
    therefore report a longer container duration because of an unselected audio
    or subtitle track. The video track is the stable value for remux validation.
    """
    for track in data.get("tracks", []):
        if track.get("type") != "video":
            continue
        # The first video is the primary video. A later video track's tag must
        # not stand in for a missing primary-video duration.
        return _duration_tag_ns(track.get("properties", {}).get("tag_duration"))
    return 0


def _duration_tag_ns(value: object) -> int:
    match = re.fullmatch(r"(\d+):(\d+):(\d+)(?:\.(\d+))?", str(value or "").strip())
    if not match:
        return 0
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        return 0
    fraction_ns = int(((fraction or "") + "000000000")[:9])
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1_000_000_000 + fraction_ns


class _VideoDurationProbeCancel:
    """Bound metadata-only probing while retaining the real user cancellation."""

    def __init__(self, cancel_event: threading.Event | None, seconds: float = 5.0):
        self.cancel_event = cancel_event
        self.deadline = time.monotonic() + seconds
        self.timed_out = False

    def is_set(self) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        if time.monotonic() >= self.deadline:
            self.timed_out = True
        return self.timed_out


def _probe_primary_video_duration_ns(
    media_path: str | Path,
    log: Callable[[str], None] = log_noop,
    cancel_event: threading.Event | None = None,
    *,
    media_data: dict | None = None,
) -> int:
    """Read v:0 metadata, or a bounded tail-packet interval if tags are absent.

    ``-nofind_stream_info`` avoids FFmpeg's optional read/decode heuristics.
    Container duration only helps seek near EOF; it never supplies the result.
    The whole fallback shares ten seconds, with no single step over five.
    """
    check_cancel(cancel_event)
    ffprobe = Path(FFMPEG).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if not ffprobe.is_file():
        log("主视频时长补查不可用：未找到 FFmpeg 配套 ffprobe；不会改用容器最长时长。")
        return 0
    deadline = time.monotonic() + 10.0

    def query(extra_args: list[str]) -> dict | None:
        remaining = deadline - time.monotonic()
        check_cancel(cancel_event)
        if remaining <= 0:
            log("主视频时长补查达到 10 秒总上限；未进行全片扫描。")
            return None
        timeout = _VideoDurationProbeCancel(cancel_event, min(5.0, remaining))
        args = [str(ffprobe), "-v", "error", "-nofind_stream_info", "-select_streams", "v:0", *extra_args, "-of", "json", str(media_path)]
        try:
            completed = run_command(args, log=log, cancel_event=timeout)
            check_cancel(cancel_event)
            if timeout.is_set():
                log("主视频时长补查达到 5 秒单步上限或剩余总预算；未进行全片扫描。")
                return None
            # ffprobe can return 0 despite demuxer read errors. In particular,
            # tail packets from a damaged/truncated file are not proof of EOF.
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            if completed.returncode != 0 or stderr:
                log(f"主视频时长补查报告读取错误，未采信结果：{stderr[:240] or completed.returncode}")
                return None
            payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
            return payload if isinstance(payload, dict) else None
        except CancelledError:
            check_cancel(cancel_event)
            if not timeout.timed_out:
                raise
            log("主视频时长补查达到 5 秒单步上限或剩余总预算；未进行全片扫描。")
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
            check_cancel(cancel_event)
            log(f"主视频时长补查未取得可靠数据：{exc}")
        return None

    payload = query(["-show_entries", "stream=duration,start_time:stream_tags=DURATION"])
    if payload is None:
        return 0
    streams = payload.get("streams", [])
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        return 0
    stream = streams[0]

    def seconds_ns(value: object) -> int | None:
        try:
            seconds = Decimal(str(value))
            return int(seconds * 1_000_000_000) if seconds.is_finite() else None
        except (InvalidOperation, ValueError, TypeError):
            return None

    duration = seconds_ns(stream.get("duration"))
    if duration is not None and duration > 0:
        return duration
    tags = stream.get("tags", {})
    if isinstance(tags, dict):
        duration = _duration_tag_ns(next((value for key, value in tags.items() if str(key).upper() == "DURATION"), None))
        if duration > 0:
            return duration

    data = media_data or {}
    container_hint = media_duration_ns(data)
    # A short or missing seek hint would make tail probing a whole-file read.
    if container_hint <= 60_000_000_000:
        return 0
    primary = next((track for track in data.get("tracks", []) if track.get("type") == "video"), {})
    props = primary.get("properties", {})
    index_entries = props.get("num_index_entries")
    if isinstance(index_entries, bool) or (_optional_nonnegative_int(index_entries) or 0) <= 0:
        log("主视频缺少可确认的寻址索引，不启用尾部读包，避免退化为全片顺序读取。")
        return 0
    origin = None
    value = props.get("minimum_timestamp")
    if value is not None and not isinstance(value, bool):
        try:
            parsed = Decimal(str(value))
            if parsed.is_finite() and parsed == parsed.to_integral_value():
                origin = int(parsed)
        except (InvalidOperation, ValueError, TypeError):
            pass
    if origin is None:
        origin = seconds_ns(stream.get("start_time"))
    if origin is None:
        log("主视频起点元数据缺失，限定读取开头 5 个包；不解码影片。")
        head = query(["-read_intervals", "0%+#5", "-show_packets", "-show_entries", "packet=pts_time"])
        if head is None:
            return 0
        packets = head.get("packets", [])
        if not isinstance(packets, list) or not packets:
            return 0
        starts = [seconds_ns(packet.get("pts_time")) if isinstance(packet, dict) else None for packet in packets]
        if not starts or any(value is None for value in starts):
            return 0
        origin = min(starts)

    tail_start = container_hint / 1_000_000_000 - 20.0
    if origin >= int(tail_start * 1_000_000_000):
        return 0
    log("主视频时长标签缺失，限定从容器末尾前 20 秒寻址读取视频包至 EOF；不解码影片。")
    tail = query([
        "-read_intervals", f"{tail_start:.6f}%", "-show_packets",
        "-show_entries", "packet=pts_time,duration_time",
    ])
    if tail is None:
        return 0
    packets = tail.get("packets", [])
    if not isinstance(packets, list) or not packets:
        return 0
    ends = []
    default_frame_ns = _optional_nonnegative_int(props.get("default_duration"))
    for packet in packets:
        if not isinstance(packet, dict):
            return 0
        timestamp = seconds_ns(packet.get("pts_time"))
        packet_duration = seconds_ns(packet.get("duration_time"))
        if packet_duration is None or packet_duration <= 0:
            packet_duration = default_frame_ns
        if timestamp is None or packet_duration is None or packet_duration <= 0 or timestamp < origin:
            return 0
        ends.append(timestamp + packet_duration)
    duration = max(ends) - origin
    if duration <= 0:
        return 0
    log(f"主视频时长由实际视频包起止补查：{duration / 1_000_000_000:.3f} 秒。")
    return duration


def validate_media_output(
    output_path: Path,
    input_media: dict,
    expected_audio_tracks: int,
    expected_subtitle_tracks: int,
    expected_audio_specs: list[dict] | None = None,
    log: Callable[[str], None] = log_noop,
    *,
    input_path: str | Path | None = None,
    cancel_event: threading.Event | None = None,
) -> dict:
    check_cancel(cancel_event)
    if not output_path.exists() or output_path.stat().st_size < 1024 * 1024:
        raise RuntimeError("输出完整性验证失败：文件不存在或体积异常。")
    output_media = inspect_media(str(output_path), cancel_event=cancel_event)
    if output_media.get("errors"):
        raise RuntimeError("输出完整性验证失败：" + "; ".join(output_media["errors"]))
    if not output_media.get("container", {}).get("recognized", False):
        raise RuntimeError("输出完整性验证失败：无法识别媒体容器。")

    input_tracks = input_media.get("tracks", [])
    output_tracks = output_media.get("tracks", [])
    expected_video_tracks = sum(1 for track in input_tracks if track.get("type") == "video")
    actual_video_tracks = sum(1 for track in output_tracks if track.get("type") == "video")
    actual_audio_tracks = sum(1 for track in output_tracks if track.get("type") == "audio")
    actual_subtitle_tracks = sum(1 for track in output_tracks if track.get("type") == "subtitles")
    if actual_video_tracks != expected_video_tracks:
        raise RuntimeError(f"输出完整性验证失败：视频轨 {actual_video_tracks}/{expected_video_tracks}。")
    if actual_audio_tracks != expected_audio_tracks:
        raise RuntimeError(f"输出完整性验证失败：音轨 {actual_audio_tracks}/{expected_audio_tracks}。")
    if actual_subtitle_tracks != expected_subtitle_tracks:
        raise RuntimeError(f"输出完整性验证失败：字幕轨 {actual_subtitle_tracks}/{expected_subtitle_tracks}。")
    import subtitle_audit
    audit = subtitle_audit.current()
    if audit is not None:
        audit.check_output_purposes(output_media)
        log('成品字幕用途检查通过；每轨时间轴和内容验证状态另列于字幕报告。')

    # A matching count is insufficient: an MP4-to-MKV work copy can renumber
    # tracks, accidentally preserving a different language with the same count.
    if expected_audio_specs:
        output_audio = [track for track in output_tracks if track.get("type") == "audio"]
        for position, (expected, actual) in enumerate(zip(expected_audio_specs, output_audio), start=1):
            expected_codec = str(expected.get("codec", "")).lower()
            actual_codec = str(actual.get("codec", "")).lower()
            if expected_codec and actual_codec and expected_codec != actual_codec:
                raise RuntimeError(
                    "输出完整性验证失败：保留音轨编码不一致，"
                    f"第 {position} 条应为 {expected.get('codec')}，实际为 {actual.get('codec')}。"
                )
            expected_props = expected.get("properties", {})
            actual_props = actual.get("properties", {})
            for field, label in (("audio_channels", "声道数"), ("audio_sampling_frequency", "采样率")):
                expected_value = expected_props.get(field)
                actual_value = actual_props.get(field)
                if expected_value and actual_value and expected_value != actual_value:
                    raise RuntimeError(
                        "输出完整性验证失败：保留音轨"
                        f"{label}不一致，第 {position} 条应为 {expected_value}，实际为 {actual_value}。"
                    )

    input_duration = video_track_duration_ns(input_media)
    output_duration = video_track_duration_ns(output_media)
    if input_duration <= 0:
        source_path = input_path or input_media.get("file_name") or input_media.get("_source_path")
        if source_path:
            log("源片缺少主视频时长标签，正在补查主视频流（总计最多 10 秒，不解码影片）。")
            input_duration = _probe_primary_video_duration_ns(source_path, log, cancel_event, media_data=input_media)
        else:
            log("源片主视频时长补查不可用：没有可追溯的源文件路径。")
    if output_duration <= 0:
        log("成品缺少主视频时长标签，正在补查主视频流（总计最多 10 秒，不解码影片）。")
        output_duration = _probe_primary_video_duration_ns(output_path, log, cancel_event, media_data=output_media)
    duration_verified = input_duration > 0 and output_duration > 0
    if duration_verified:
        tolerance = max(3_000_000_000, int(input_duration * 0.001))
        if abs(input_duration - output_duration) > tolerance:
            raise RuntimeError(
                "输出完整性验证失败：主视频时长不一致，"
                f"输入 {input_duration / 1_000_000_000:.1f} 秒，输出 {output_duration / 1_000_000_000:.1f} 秒。"
            )
    else:
        missing = "、".join(label for label, value in (("源片", input_duration), ("成品", output_duration)) if value <= 0)
        log(f"{missing}主视频时长仍未知；轨道完整性已验证，主视频时长未核验。")
    summary = f"视频 {actual_video_tracks}，音轨 {actual_audio_tracks}，字幕 {actual_subtitle_tracks}"
    if duration_verified:
        log(f"输出验证通过：{summary}，主视频时长 {output_duration / 1_000_000_000:.1f} 秒（已与源片比对）。")
    else:
        log(f"输出轨道验证通过：{summary}；主视频时长未核验。")
    return {
        "video_duration_verified": duration_verified,
        "input_video_duration_ns": input_duration,
        "output_video_duration_ns": output_duration,
        "video_tracks": actual_video_tracks,
        "audio_tracks": actual_audio_tracks,
        "subtitle_tracks": actual_subtitle_tracks,
    }


def process_video(
    input_path: str,
    output_path: str,
    keep_audio_ids: list[int],
    keep_subtitle_ids: list[int],
    source_subtitle_id: int | None,
    target_codes: list[str],
    work_dir: str,
    log: Callable[[str], None] = log_noop,
    parallel_targets: int = 2,
    cancel_event: threading.Event | None = None,
    preserve_audio_defaults: bool = False,
    subtitle_sync_offsets: dict[int, int] | None = None,
    source_subtitle_override: str | Path | None = None,
    source_subtitle_offset_milliseconds: int = 0,
    additional_generated_subtitles: list[tuple[Path, str, str, bool]] | None = None,
    subtitle_language_preferences: list[str] | None = None,
    chinese_script_equivalent: bool = False,
    translation_start: Callable[[], None] | None = None,
    translation_end: Callable[[], None] | None = None,
) -> str:
    if (translation_start is None) != (translation_end is None):
        raise ValueError("翻译资源回调必须同时提供开始和结束方法。")
    check_cancel(cancel_event)
    generated: list[tuple[Path, str, str, bool]] = list(additional_generated_subtitles or [])
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    output = Path(output_path)
    partial_output = partial_output_path(output_path)
    intermediate_output = work / "final-before-mp4.mkv"
    remove_incomplete_file(partial_output, log)
    if output.suffix.lower() == ".mp4":
        remove_incomplete_file(intermediate_output, log)
    ensure_output_disk_space(input_path, output_path, log)
    input_media = inspect_media(input_path)
    input_tracks = inspect_tracks(input_path)
    selected_audio_specs = [
        track
        for track in input_media.get("tracks", [])
        if track.get("type") == "audio" and int(track.get("id", -1)) in keep_audio_ids
    ]
    work_input = prepare_work_input(input_path, work, log, cancel_event=cancel_event)
    check_cancel(cancel_event)
    work_tracks = inspect_tracks(work_input)
    source_to_work_ids = (
        {track.id: track.id for track in input_tracks}
        if str(Path(work_input).resolve()) == str(Path(input_path).resolve())
        else map_normalized_track_ids(input_tracks, work_tracks)
    )
    subtitles = {track.id: track for track in work_tracks if track.type == "subtitles"}

    if target_codes:
        if source_subtitle_id is None:
            raise RuntimeError("需要选择一个已有字幕作为翻译来源。")
        work_source_id = source_to_work_ids.get(source_subtitle_id)
        source_track = subtitles.get(work_source_id) if work_source_id is not None else None
        if source_track is None or not (source_track.text_subtitle or source_track.pgs_subtitle or source_track.vobsub_subtitle):
            raise RuntimeError("选择的来源字幕不是可翻译的文本、PGS 或 VobSub 字幕。")
        extracted = (
            Path(source_subtitle_override)
            if source_subtitle_override
            else extract_subtitle(work_input, source_track, work, log, cancel_event=cancel_event)
        )
        check_cancel(cancel_event)
        if source_track.pgs_subtitle:
            extracted = ocr_pgs_subtitle(extracted, source_track, work, log, cancel_event=cancel_event)
            check_cancel(cancel_event)
        elif source_track.vobsub_subtitle:
            extracted = ocr_vobsub_subtitle(
                extracted,
                source_track,
                work,
                log,
                cancel_event=cancel_event,
                source_video=work_input,
            )
            check_cancel(cancel_event)
        if source_subtitle_offset_milliseconds:
            extracted = shift_subtitle_timeline(
                Path(extracted),
                work / f"corrected-source-track-{source_track.id}.srt",
                source_subtitle_offset_milliseconds,
            )
            log(
                f"图片字幕翻译源已套用纠偏："
                f"{source_subtitle_offset_milliseconds / 1000:+.2f} 秒。"
            )
        events = parse_subtitle(extracted)
        if not events:
            raise RuntimeError("没有从来源字幕中解析出有效文本。")
        log(f"来源字幕可翻译行数：{len(events)}")
        source_name = (source_track.name or "").lower()
        if source_track.forced or any(
            marker in source_name
            for marker in ("forced", "commentary", "sign", "song", "强制", "评论", "評論", "解说", "标牌", "標牌")
        ):
            log("警告：当前来源可能是强制、评论或标牌字幕，不一定包含完整对白。")
        if len(events) < 100:
            log(f"警告：来源字幕只有 {len(events)} 行，生成的目标字幕也只会覆盖这些内容。")
        target_codes = list(dict.fromkeys(target_codes))
        source_language = (source_track.language or "und").strip().lower().replace("_", "-")
        source_code = next(
            (
                code
                for code, (mkv_code, english_name, _label) in LANGUAGES.items()
                if source_language in {code.lower(), mkv_code.lower(), english_name.lower()}
            ),
            source_language[:2] if source_language != "und" else "und",
        )
        source_code = {
            "zh": "zh-CN", "chi": "zh-CN", "zho": "zh-CN",
            "chs": "zh-CN", "cht": "zh-TW",
        }.get(source_code.lower(), source_code)
        if source_code == "zh-CN" and ocr_language_for_track(source_track) == "chi_tra":
            source_code = "zh-TW"
        translation_targets = [code for code in target_codes if code != source_code]
        model_translation_targets = [
            code
            for code in translation_targets
            if needs_model_translation(
                events, code,
                work / f"translation-cache-track-{source_track.id}-{code}.jsonl",
                source_code,
                output_srt=work / f"translated-track-{source_track.id}-{code}.srt",
            )
        ]

        def translate_one(target_code: str) -> tuple[str, Path]:
            check_cancel(cancel_event)
            lang_label = LANGUAGES[target_code][2]
            output_srt = work / f"translated-track-{source_track.id}-{target_code}.srt"
            if chinese_script_converter.is_script_conversion(source_code, target_code):
                chinese_script_converter.convert_subtitle_events(
                    events,
                    output_srt,
                    source_code,
                    target_code,
                )
                log(f"{lang_label}本地简繁转换完成：{len(events)} 行；保留原时间轴。")
            else:
                cache_path = work / f"translation-cache-track-{source_track.id}-{target_code}.jsonl"
                translate_events(events, output_srt, cache_path, target_code, source_track.language, log, cancel_event=cancel_event)
            check_cancel(cancel_event)
            return target_code, output_srt

        translation_started = False
        try:
            if model_translation_targets:
                if translation_start is not None:
                    translation_start()
                    translation_started = True
                ensure_ollama_running(log=log, cancel_event=cancel_event)
            if len(translation_targets) <= 1 or parallel_targets <= 1:
                translated_outputs = [translate_one(target_code) for target_code in translation_targets]
            else:
                workers = max(1, min(parallel_targets, len(translation_targets)))
                log(f"并行翻译目标语言：{workers} 路")
                completed_by_code: dict[str, Path] = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    future_map = {
                        executor.submit(translate_one, target_code): target_code
                        for target_code in translation_targets
                    }
                    for future in concurrent.futures.as_completed(future_map):
                        check_cancel(cancel_event)
                        target_code, output_srt = future.result()
                        completed_by_code[target_code] = output_srt
                translated_outputs = [
                    (target_code, completed_by_code[target_code])
                    for target_code in translation_targets
                ]
        finally:
            if translation_started and translation_end is not None:
                translation_end()
        translated_by_code = dict(translated_outputs)
        for index, target_code in enumerate(target_codes):
            lang_label = LANGUAGES[target_code][2]
            if target_code == source_code:
                generated.append((Path(extracted), target_code, f"{lang_label} OCR", False))
            else:
                suffix = (
                    "Converted"
                    if chinese_script_converter.is_script_conversion(source_code, target_code)
                    else "Auto"
                )
                generated.append((translated_by_code[target_code], target_code, f"{lang_label} {suffix}", False))

    if generated and subtitle_language_preferences is None:
        generated = [
            (path, code, name, index == 0)
            for index, (path, code, name, _is_default) in enumerate(generated)
        ]
    check_cancel(cancel_event)
    ensure_mux_disk_space(input_path, output_path, log)
    try:
        if output.suffix.lower() == ".mp4":
            mux_video(
                input_path,
                str(intermediate_output),
                keep_audio_ids,
                keep_subtitle_ids,
                generated,
                log,
                cancel_event=cancel_event,
                preserve_audio_defaults=preserve_audio_defaults,
                subtitle_sync_offsets=subtitle_sync_offsets,
                subtitle_language_preferences=subtitle_language_preferences,
                chinese_script_equivalent=chinese_script_equivalent,
                input_media=input_media,
                allow_fast_passthrough=False,
            )
            check_cancel(cancel_event)
            remux_mkv_to_mp4(str(intermediate_output), str(partial_output), log, cancel_event=cancel_event)
        else:
            mux_video(
                input_path,
                str(partial_output),
                keep_audio_ids,
                keep_subtitle_ids,
                generated,
                log,
                cancel_event=cancel_event,
                preserve_audio_defaults=preserve_audio_defaults,
                subtitle_sync_offsets=subtitle_sync_offsets,
                subtitle_language_preferences=subtitle_language_preferences,
                chinese_script_equivalent=chinese_script_equivalent,
                input_media=input_media,
            )
        check_cancel(cancel_event)
        validate_media_output(
            partial_output,
            input_media,
            expected_audio_tracks=len(keep_audio_ids),
            expected_subtitle_tracks=len(keep_subtitle_ids) + len(generated),
            expected_audio_specs=selected_audio_specs,
            log=log,
            input_path=input_path,
            cancel_event=cancel_event,
        )
        os.replace(partial_output, output)
        if intermediate_output.exists():
            try:
                intermediate_output.unlink()
                log(f"已清理 MP4 中间文件：{intermediate_output}")
            except OSError as exc:
                log(f"MP4 中间文件清理失败：{intermediate_output}；原因：{exc}")
        log(f"正式输出已就绪：{output}")
        return str(output)
    except BaseException:
        remove_incomplete_file(partial_output, log)
        if intermediate_output.exists():
            remove_incomplete_file(intermediate_output, log)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Standard subtitle/audio track tool")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--keep-audio", default="")
    parser.add_argument("--keep-subtitles", default="")
    parser.add_argument("--source-subtitle", type=int)
    parser.add_argument("--targets", default="")
    parser.add_argument("--parallel-targets", type=int, default=2)
    args = parser.parse_args()

    keep_audio = [int(item) for item in args.keep_audio.split(",") if item.strip()]
    keep_subtitles = [int(item) for item in args.keep_subtitles.split(",") if item.strip()]
    targets = [item.strip() for item in args.targets.split(",") if item.strip()]
    process_video(
        input_path=args.input,
        output_path=args.output,
        keep_audio_ids=keep_audio,
        keep_subtitle_ids=keep_subtitles,
        source_subtitle_id=args.source_subtitle,
        target_codes=targets,
        work_dir=args.work_dir,
        log=print,
        parallel_targets=args.parallel_targets,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
