"""Bounded, evidence-led priority rescue for an audio preparation session.

The owner retains cancellation and its original absolute deadline.  This module
only observes a private FFmpeg progress file and can raise the current borrowed
Popen handle from BELOW_NORMAL to NORMAL once; it never starts a process.
"""
from __future__ import annotations

from collections import deque
import ctypes
import math
import os
from pathlib import Path
import threading
import time

IS_WINDOWS = os.name == "nt"
NORMAL_PRIORITY_CLASS = 0x20
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
_PRIORITY_MASK = 0x40 | 0x4000 | 0x20 | 0x8000 | 0x80 | 0x100
_MAX_READ = 65536
_MAX_LINE = 4096
_FRESH_POLL_SECONDS = 5.0


def _priority_class(process):
    """Read the borrowed, pointer-width Popen handle, without reopening a PID."""
    if not IS_WINDOWS:
        return None
    try:
        native = ctypes.WinDLL("kernel32", use_last_error=True)
        function = native.GetPriorityClass
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_ulong
        handle = int(process._handle)
        if not handle:
            return None
        value = function(ctypes.c_void_p(handle))
        return int(value) if value else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _normal_priority(process):
    """Change only this already-owned process; no descendants or external PIDs."""
    if not IS_WINDOWS:
        return False
    try:
        native = ctypes.WinDLL("kernel32", use_last_error=True)
        function = native.SetPriorityClass
        function.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        function.restype = ctypes.c_int
        handle = int(process._handle)
        return bool(handle and function(ctypes.c_void_p(handle), NORMAL_PRIORITY_CLASS))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class AudioPriorityPolicy:
    """One preparation budget shared by its decoder stages and final VAD."""

    def __init__(self, deadline, budget_seconds, log, initial_promoted=False,
                 clock=time.monotonic, initial_attempted=False):
        self.deadline = float(deadline) if _finite(deadline) else None
        self.budget_seconds = float(budget_seconds) if _finite(budget_seconds) else 0.0
        self.clock = clock
        self.log = log
        self._promoted = bool(initial_promoted)
        self._attempted = bool(initial_promoted or initial_attempted)
        self._promotion = None
        self._last_observation = None
        self._lock = threading.RLock()

    @property
    def eligible(self):
        return IS_WINDOWS and self.deadline is not None and self.budget_seconds >= 60.0

    @property
    def promoted(self):
        with self._lock:
            return self._promoted

    @property
    def attempted(self):
        with self._lock:
            return self._attempted

    def monitor(self, progress_path, media_seconds, future_media_seconds=0):
        return _AudioProcessMonitor(self, progress_path, media_seconds, future_media_seconds)

    def snapshot(self):
        with self._lock:
            return dict(eligible=self.eligible, promoted=self._promoted,
                        attempted=self._attempted, deadline=self.deadline,
                        budget_seconds=self.budget_seconds,
                        promotion=dict(self._promotion) if self._promotion else None,
                        last_observation=dict(self._last_observation) if self._last_observation else None)

    def _log(self, message):
        try:
            self.log(message)
        except Exception:
            # Diagnostics must not turn a usable decoder output into a failure.
            pass


class _AudioProcessMonitor:
    def __init__(self, policy, progress_path, media_seconds, future_media_seconds):
        self.policy = policy
        self.path = Path(progress_path) if progress_path is not None else None
        self.media_seconds = float(media_seconds) if _finite(media_seconds) else 0.0
        self.future_media_seconds = (float(future_media_seconds)
                                     if _finite(future_media_seconds) else -1.0)
        self._process = None
        self._started = None
        self._initial_priority = None
        self._finished = False
        self._offset = 0
        self._tail = b""
        self._block = {}
        self._last_poll = None
        self._last_media = None
        self._samples = deque(maxlen=128)
        self._risk = 0
        self._valid_reports = 0
        self._invalid = False

    def creation_flags(self, default_flags):
        if IS_WINDOWS and self.policy.promoted:
            return (default_flags & ~_PRIORITY_MASK) | NORMAL_PRIORITY_CLASS
        return default_flags

    def started(self, process):
        if self._process is not None:
            return
        self._process = process
        self._started = self.policy.clock()
        self._initial_priority = _priority_class(process)
        label = {BELOW_NORMAL_PRIORITY_CLASS: "低于正常", NORMAL_PRIORITY_CLASS: "普通"}.get(
            self._initial_priority, "未能读取")
        self.policy._log(f"音频准备进程优先级：{label}；原准备截止时间保持。")

    def _alive(self, process):
        if process is not self._process or self._finished:
            return False
        try:
            return process.poll() is None
        except (AttributeError, OSError):
            return False

    def poll(self, process):
        if not self._alive(process) or self.path is None:
            return
        now = self.policy.clock()
        self._consume(now, allow_promotion=True)

    def finished(self, process):
        if process is not self._process or self._finished:
            return
        if self.path is not None:
            self._consume(self.policy.clock(), allow_promotion=False)
        self._finished = True

    def _clear_evidence(self):
        self._samples.clear()
        self._risk = 0
        self._valid_reports = 0

    def _consume(self, now, *, allow_promotion):
        if self._last_poll is not None and now - self._last_poll > _FRESH_POLL_SECONDS:
            # Reports accumulated while the observer was absent have no reliable
            # arrival times.  Rebuild a live window rather than treating them as fresh.
            self._clear_evidence()
        self._last_poll = now
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                if size < self._offset:
                    self._invalid = True
                    self._clear_evidence()
                    return
                if size - self._offset > _MAX_READ:
                    # Bound parsing work.  No inference is made from skipped reports.
                    self._offset = size
                    self._tail = b""
                    self._block = {}
                    self._clear_evidence()
                    return
                stream.seek(self._offset)
                chunk = stream.read(_MAX_READ)
                self._offset += len(chunk)
        except (OSError, ValueError):
            self._risk = 0
            return
        if not chunk:
            return
        data = self._tail + chunk
        lines = data.split(b"\n")
        self._tail = lines.pop()
        if len(self._tail) > _MAX_LINE:
            self._tail = b""
            self._invalid = True
            self._clear_evidence()
            return
        reports = []
        for line in lines:
            if len(line) > _MAX_LINE:
                self._invalid = True
                self._clear_evidence()
                continue
            key, separator, value = line.strip().partition(b"=")
            if not separator:
                continue
            if key == b"out_time_us":
                self._block[key] = value
            elif key == b"progress":
                reports.append((self._block.get(b"out_time_us"), value))
                self._block = {}
        if not reports:
            return
        # Multiple queued records have the same observation time; only the last
        # report can be a fresh sample.  They never count as a risk streak.
        if len(reports) > 1:
            self._risk = 0
        raw, status = reports[-1]
        if status not in (b"continue", b"end") or raw is None:
            self._risk = 0
            return
        try:
            media = int(raw) / 1_000_000.0
        except (ValueError, OverflowError):
            self._risk = 0
            return
        if (not math.isfinite(media) or media < 0 or self.media_seconds <= 0
                or self.future_media_seconds < 0 or media > self.media_seconds + 1.0):
            self._risk = 0
            return
        if self._last_media is not None and media < self._last_media:
            self._invalid = True
            self._clear_evidence()
            return
        if self._last_media is not None and media == self._last_media:
            self._risk = 0
            return
        self._last_media = media
        if self._invalid or media <= 0:
            return
        self._valid_reports += 1
        self._samples.append((now, media))
        while self._samples and now - self._samples[0][0] > 25.0:
            self._samples.popleft()
        if status == b"end" or media >= self.media_seconds:
            self._risk = 0
            return
        if not allow_promotion:
            return
        self._consider(now, media)

    def _consider(self, now, media):
        policy = self.policy
        if (not policy.eligible or policy.promoted or policy._attempted
                or self._initial_priority != BELOW_NORMAL_PRIORITY_CLASS
                or self._started is None or self._valid_reports < 3):
            return
        remaining = policy.deadline - now
        elapsed = policy.budget_seconds - remaining
        if remaining <= 0 or elapsed < 30.0 or now <= self._started:
            self._risk = 0
            return
        window = next((sample for sample in reversed(self._samples)
                       if 10.0 <= now - sample[0] <= 25.0), None)
        if window is None:
            self._risk = 0
            return
        recent_rate = (media - window[1]) / (now - window[0])
        # A fast first section must not hide CPU contention arriving later.
        # Warmup, a live >=10-second window and three risky reports suppress
        # startup/brief stalls without keeping a stale whole-process average.
        rate = recent_rate
        if not math.isfinite(rate) or rate <= 0:
            self._risk = 0
            return
        decode_eta = (max(0.0, self.media_seconds - media) + self.future_media_seconds) / rate
        eta = decode_eta + 15.0 + 5.0
        evidence = dict(elapsed_seconds=round(elapsed, 3), remaining_seconds=round(remaining, 3),
                        media_seconds=round(media, 6), total_media_seconds=self.media_seconds,
                        future_media_seconds=self.future_media_seconds,
                        rate=round(rate, 6), decode_eta_seconds=round(decode_eta, 3),
                        vad_reserve_seconds=15.0, safety_seconds=5.0,
                        estimated_remaining_seconds=round(eta, 3))
        with policy._lock:
            policy._last_observation = evidence
            self._risk = self._risk + 1 if eta > remaining else 0
            if self._risk < 3 or policy._attempted:
                return
            # Recheck the same borrowed handle immediately before mutating it.
            if not self._alive(self._process) or _priority_class(self._process) != BELOW_NORMAL_PRIORITY_CLASS:
                self._risk = 0
                return
            policy._attempted = True
            success = _normal_priority(self._process)
            actual = _priority_class(self._process)
            policy._promoted = bool(success and actual == NORMAL_PRIORITY_CLASS)
            policy._promotion = dict(evidence, success=policy._promoted,
                                     priority_before=BELOW_NORMAL_PRIORITY_CLASS,
                                     priority_after=actual)
            if policy._promoted:
                policy._log(
                    f"音频准备临近预算风险：已用{elapsed:.1f}秒，剩余{remaining:.1f}秒，"
                    f"预计还需{eta:.1f}秒（含VAD及余量）；仅当前取证进程临时升为普通优先级，原截止时间保持。")
            else:
                policy._log("音频准备尝试提升当前进程优先级未确认成功；继续原处理，原截止时间保持。")
