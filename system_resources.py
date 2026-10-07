# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass


CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_physical", ctypes.c_ulonglong),
        ("available_physical", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("available_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("available_virtual", ctypes.c_ulonglong),
        ("available_extended_virtual", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class HardwareProfile:
    logical_cpus: int
    memory_gib: float
    gpu_count: int
    gpu_memory_mib: tuple[int, ...]
    has_nvme: bool
    recommended_mode: str
    memory_load_percent: float
    tier_key: str
    tier_label: str
    tier_summary: str
    reasons: tuple[str, ...]

    @property
    def work_mode_label(self) -> str:
        gpu_mib = max(self.gpu_memory_mib, default=0)
        if self.logical_cpus >= 16 and self.memory_gib >= 64 and self.has_nvme and gpu_mib >= 24 * 1024:
            return "性能"
        if self.logical_cpus >= 12 and self.memory_gib >= 32 and self.has_nvme and gpu_mib >= 16 * 1024:
            return "均衡"
        if self.logical_cpus >= 8 and self.memory_gib >= 16 and gpu_mib >= 8 * 1024:
            return "基础"
        return "入门"

    @property
    def translation_parallelism(self) -> int:
        gpu_mib = max(self.gpu_memory_mib, default=0)
        if gpu_mib >= 32 * 1024 and self.memory_gib >= 64 and self.logical_cpus >= 16:
            return 4
        if gpu_mib >= 24 * 1024 and self.memory_gib >= 48 and self.logical_cpus >= 12:
            return 3
        if gpu_mib >= 16 * 1024 and self.memory_gib >= 32 and self.logical_cpus >= 12:
            return 2
        return 1

    @property
    def batch_movie_workers(self) -> int:
        if self.recommended_mode == "高性能模式":
            return 4
        if self.recommended_mode == "均衡模式":
            return 2
        return 1

    @property
    def ai_slots(self) -> int:
        return min(self.batch_movie_workers, self.translation_parallelism)


def memory_status() -> tuple[float, float]:
    status = _MemoryStatus()
    status.length = ctypes.sizeof(_MemoryStatus)
    if os.name == "nt" and ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return float(status.memory_load), status.total_physical / 1024**3
    return 0.0, 0.0


def _run_hidden(args: list[str], timeout: int = 8) -> str:
    try:
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _nvidia_memory() -> tuple[int, ...]:
    command = shutil.which("nvidia-smi")
    if not command and os.name == "nt":
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        if os.path.exists(candidate):
            command = candidate
    if not command:
        return ()
    output = _run_hidden([command, "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
    values: list[int] = []
    for line in output.splitlines():
        match = re.search(r"\d+", line)
        if match:
            values.append(int(match.group()))
    return tuple(values)


def _has_nvme() -> bool:
    if os.name != "nt":
        return False
    output = _run_hidden([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "@(Get-PhysicalDisk -ErrorAction SilentlyContinue | Where-Object {$_.BusType -eq 'NVMe'}).Count",
    ])
    match = re.search(r"\d+", output)
    return bool(match and int(match.group()) > 0)


def hardware_requirements_text() -> str:
    return (
        "入门：4 线程 CPU、8GB 内存，适合单片基础整理。\n"
        "基础：8 线程 CPU、16GB 内存、NVIDIA 8GB 显存，适合常规单片与轻量批量。\n"
        "均衡：12 线程 CPU、32GB 内存、NVMe SSD、NVIDIA 16GB 显存，适合批量与 AI 字幕。\n"
        "性能：16 线程 CPU、64GB 内存、NVMe SSD、NVIDIA 24GB 显存，适合高并发生产。\n"
        "磁盘空间建议按影片大小预留 2 倍以上，4K 批量处理建议预留 100GB 以上。"
    )


def _hardware_tier(logical_cpus: int, memory_gib: float, gpu_memory: tuple[int, ...], has_nvme: bool) -> tuple[str, str, str, tuple[str, ...]]:
    gpu_mib = max(gpu_memory) if gpu_memory else 0
    reasons: list[str] = []
    if logical_cpus < 4:
        reasons.append(f"CPU 线程数 {logical_cpus}，低于最低建议 4 线程")
    elif logical_cpus >= 12:
        reasons.append(f"CPU {logical_cpus} 线程，适合均衡或性能工作模式")
    elif logical_cpus >= 8:
        reasons.append(f"CPU {logical_cpus} 线程，适合基础工作模式")
    else:
        reasons.append(f"CPU {logical_cpus} 线程，适合入门工作模式")

    if memory_gib < 8:
        reasons.append(f"内存 {memory_gib:.1f}GB，低于最低建议 8GB")
    elif memory_gib >= 32:
        reasons.append(f"内存 {memory_gib:.1f}GB，达到推荐档")
    elif memory_gib >= 16:
        reasons.append(f"内存 {memory_gib:.1f}GB，适合中等批量")
    else:
        reasons.append(f"内存 {memory_gib:.1f}GB，建议一次处理少量影片")

    if gpu_mib >= 8 * 1024:
        reasons.append(f"NVIDIA 显存 {gpu_mib / 1024:.0f}GB，适合 OCR/音频字幕任务")
    elif gpu_mib:
        reasons.append(f"NVIDIA 显存 {gpu_mib / 1024:.0f}GB，重任务会偏慢")
    else:
        reasons.append("未检测到 NVIDIA 显卡，AI/OCR 类任务主要依赖 CPU")

    reasons.append("检测到 NVMe SSD，磁盘吞吐较好" if has_nvme else "未检测到 NVMe SSD，批量读写建议保守并行")

    if logical_cpus < 4 or memory_gib < 8:
        return ("below_min", "低于最低配置", "不建议批量处理，建议补足硬件后再使用。", tuple(reasons))
    if logical_cpus >= 12 and memory_gib >= 32 and has_nvme and gpu_mib >= 8 * 1024:
        return ("recommended", "推荐舒适", "适合批量处理、OCR 和从音频生成字幕。", tuple(reasons))
    if logical_cpus >= 8 and memory_gib >= 16:
        return ("standard", "标准可用", "介于最低与推荐之间，适合稳定/均衡模式。", tuple(reasons))
    return ("entry", "入门可用", "能运行，建议稳定模式、一次处理少量影片。", tuple(reasons))


def detect_hardware() -> HardwareProfile:
    load, memory_gib = memory_status()
    logical_cpus = os.cpu_count() or 1
    gpu_memory = _nvidia_memory()
    has_nvme = _has_nvme()
    high_gpu = (len(gpu_memory) >= 2 and min(gpu_memory) >= 10 * 1024) or (gpu_memory and max(gpu_memory) >= 16 * 1024)
    if memory_gib >= 48 and logical_cpus >= 12 and has_nvme and high_gpu:
        recommended = "高性能模式"
    elif memory_gib >= 16 and logical_cpus >= 8:
        recommended = "均衡模式"
    else:
        recommended = "稳定模式"
    tier_key, tier_label, tier_summary, reasons = _hardware_tier(logical_cpus, memory_gib, gpu_memory, has_nvme)
    return HardwareProfile(
        logical_cpus, memory_gib, len(gpu_memory), gpu_memory, has_nvme, recommended,
        load, tier_key, tier_label, tier_summary, reasons,
    )


def hardware_status_text(profile: HardwareProfile) -> str:
    return f"本机档位：{profile.work_mode_label}工作模式"


def hardware_detail_text(profile: HardwareProfile) -> str:
    gpu = "未检测到 NVIDIA 显卡"
    if profile.gpu_memory_mib:
        gpu = "、".join(f"NVIDIA {value / 1024:.0f}GB" for value in profile.gpu_memory_mib)
    storage = "检测到 NVMe SSD" if profile.has_nvme else "未检测到 NVMe SSD"
    return (
        f"CPU：{profile.logical_cpus} 线程\n"
        f"内存：{profile.memory_gib:.1f}GB，总占用 {profile.memory_load_percent:.0f}%\n"
        f"显卡：{gpu}\n"
        f"磁盘：{storage}"
    )


def hardware_reason_text(profile: HardwareProfile) -> str:
    return "简短原因：" + "；".join(profile.reasons[:4])


def _disk_busy_percent() -> float | None:
    if os.name != "nt":
        return None
    output = _run_hidden([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "$d=Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -ErrorAction SilentlyContinue | "
        "Where-Object {$_.Name -eq '_Total'} | Select-Object -First 1 -ExpandProperty PercentDiskTime; "
        "if($null -ne $d){$d}",
    ])
    match = re.search(r"\d+(?:\.\d+)?", output)
    return float(match.group()) if match else None


class RuntimeGuard:
    def __init__(self, memory_limit: float = 85.0, disk_limit: float = 92.0) -> None:
        self.memory_limit = memory_limit
        self.disk_limit = disk_limit
        self._last_disk_check = 0.0
        self._disk_busy: float | None = None

    def can_start_next(self) -> tuple[bool, str]:
        memory_load, _total = memory_status()
        if memory_load >= self.memory_limit:
            return False, f"内存占用已达 {memory_load:.0f}%，暂停启动下一部影片。"
        now = time.monotonic()
        if now - self._last_disk_check >= 5.0:
            try:
                self._disk_busy = _disk_busy_percent()
            except Exception:
                self._disk_busy = None
            self._last_disk_check = now
        if self._disk_busy is not None and self._disk_busy >= self.disk_limit:
            return False, f"磁盘负载已达 {self._disk_busy:.0f}%，正在等待负载下降。"
        return True, ""
