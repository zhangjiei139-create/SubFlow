# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import subtitle_tool_core as core


DETECTOR_VERSION = 9
AUDIO_RATIOS = (0.30, 0.65)
AUDIO_WINDOW_SECONDS = 20.0
AUDIO_SAMPLE_RATE = 16_000
VAD_BLOCK_SECONDS = 0.5
KEYFRAME_LOOKBACK_SECONDS = 10.0
FRAME_INTERVAL_SECONDS = 0.5
AFTER_SPEECH_SECONDS = 1.5
SHORT_DECODE_SECONDS = 2.0
MAX_SPEECH_INTERVAL_ATTEMPTS = 3
MINIMUM_ATTEMPTED_SAMPLES = 1
DETECTION_TIME_BUDGET_SECONDS = 20.0
_CACHE_LOCK = threading.Lock()
_DETECTION_GATE = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class DetectionResult:
    detected: bool
    evidence_count: int
    sample_count: int
    detail: str
    status: str = ""
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.status:
            status = "detected" if self.detected else (
                "absent" if self.sample_count >= MINIMUM_ATTEMPTED_SAMPLES else "uncertain"
            )
            object.__setattr__(self, "status", status)

    @property
    def conclusive(self) -> bool:
        return self.status in {"detected", "absent"}


def _cache_path() -> Path:
    configured = os.environ.get("SUBFLOW_RUNTIME_DIR", "").strip()
    root = (
        Path(configured)
        if configured
        else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubFlow"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / "burned-subtitle-cache.json"


def _sampled_file_hash(path: Path) -> str:
    size = path.stat().st_size
    chunk_size = 64 * 1024
    offsets = (0, max(0, size // 2 - chunk_size // 2), max(0, size - chunk_size))
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            digest.update(stream.read(chunk_size))
    return digest.hexdigest()


def _cache_key(path: Path) -> str:
    stat = path.stat()
    # Deliberately omit the path so a rename or move does not trigger another
    # expensive scan of the same video.
    return f"{stat.st_size}|{stat.st_mtime_ns}|{_sampled_file_hash(path)}|v{DETECTOR_VERSION}"


def _read_cache() -> dict:
    try:
        value = json.loads(_cache_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    path = _cache_path()
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(cache, ensure_ascii=True), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)


def _duration_seconds(video: Path, media_data: dict) -> float:
    duration_ns = core.video_track_duration_ns(media_data) or core.media_duration_ns(media_data)
    if duration_ns > 0:
        return duration_ns / 1_000_000_000
    ffprobe = Path(core.FFMPEG).with_name("ffprobe.exe")
    if not ffprobe.is_file():
        return 0.0
    try:
        completed = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            check=False,
        )
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
        return max(0.0, float(payload.get("format", {}).get("duration", 0.0)))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0.0


def _available_ocr_languages(audio_languages: list[str]) -> str:
    tessdata = Path(core.TESSERACT).resolve().parent / "tessdata"
    preferred = ["eng", "chi_sim", "chi_tra"]
    for language in audio_languages:
        code = core.OCR_LANGUAGE_MAP.get((language or "").lower())
        if code and code not in preferred:
            preferred.append(code)
    available = [code for code in preferred if (tessdata / f"{code}.traineddata").is_file()]
    return "+".join(available)



def _ocr_lines(image: Path, languages: str, timeout_seconds: float = 8.0) -> list[str]:
    environment = os.environ.copy()
    environment["TESSDATA_PREFIX"] = str(Path(core.TESSERACT).resolve().parent / "tessdata")
    collected: list[str] = []
    for page_mode in ("6", "11"):
        completed = subprocess.run(
            [core.TESSERACT, str(image), "stdout", "-l", languages, "--psm", page_mode, "tsv"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(1.0, timeout_seconds),
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            env=environment,
            check=False,
        )
        if completed.returncode != 0:
            continue
        rows = completed.stdout.decode("utf-8", errors="replace").splitlines()[1:]
        grouped: dict[tuple[str, str, str, str], list[str]] = {}
        for row in rows:
            columns = row.split("\t", 11)
            if len(columns) != 12:
                continue
            try:
                confidence = float(columns[10])
            except ValueError:
                continue
            text = columns[11].strip()
            if confidence < 50 or not text:
                continue
            key = (columns[1], columns[2], columns[3], columns[4])
            grouped.setdefault(key, []).append(text)
        for line in (" ".join(words) for words in grouped.values() if words):
            if line not in collected:
                collected.append(line)
    return collected


def _meaningful_line(text: str) -> bool:
    value = re.sub(r"\s+", " ", text).strip()
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value))
    if cjk >= 3:
        return True
    words = re.findall(r"[A-Za-zÀ-ÿ]{2,}", value)
    letters = sum(len(word) for word in words)
    return len(words) >= 3 and letters >= 8


def _normalized_evidence_text(value: str) -> str:
    """Normalize OCR text for retained experimental evidence comparisons."""
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", value).lower()


def _runtime_dir() -> Path:
    configured = os.environ.get("SUBFLOW_RUNTIME_DIR", "").strip()
    roots = []
    if configured:
        roots.append(Path(configured).resolve() / "burned-check")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        roots.append(Path(local_appdata).resolve() / "SubFlow" / "burned-check")
    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    if public:
        roots.append(Path(public).resolve() / "SubFlow" / "burned-check")
    roots.append(Path.cwd().resolve() / ".subflow-runtime" / "burned-check")
    for root in roots:
        if not str(root).isascii():
            continue
        try:
            work = root / uuid.uuid4().hex
            work.mkdir(parents=True, exist_ok=False)
            return work
        except OSError:
            continue
    raise RuntimeError("无法创建烧录字幕检测临时目录")



@dataclass(frozen=True)
class VisualScreen:
    state: str
    score: float = 0.0
    reason: str = ""


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _speech_intervals(
    video: Path,
    duration: float,
    ratio: float,
    deadline: float,
) -> tuple[list[dict[str, float]], float, bool]:
    centre = duration * ratio
    window_start = max(
        0.0,
        min(max(0.0, duration - AUDIO_WINDOW_SECONDS), centre - AUDIO_WINDOW_SECONDS / 2.0),
    )
    remaining = _remaining(deadline)
    if remaining <= 0.5:
        return [], window_start, False
    try:
        completed = subprocess.run(
            [
                core.FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{window_start:.3f}",
                "-i",
                str(video),
                "-t",
                f"{AUDIO_WINDOW_SECONDS:g}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(AUDIO_SAMPLE_RATE),
                "-f",
                "s16le",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.5, min(10.0, remaining)),
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], window_start, False
    if completed.returncode != 0:
        return [], window_start, False
    try:
        import numpy as np

        samples = np.frombuffer(completed.stdout, dtype="<i2").astype(np.float32) / 32768.0
        block_size = int(AUDIO_SAMPLE_RATE * VAD_BLOCK_SECONDS)
        blocks: list[tuple[bool, float]] = []
        for offset in range(0, len(samples) - block_size + 1, block_size):
            block = samples[offset:offset + block_size]
            rms = float(np.sqrt(np.mean(block * block) + 1e-12))
            dbfs = 20.0 * math.log10(max(rms, 1e-8))
            zcr = float(np.mean(np.signbit(block[1:]) != np.signbit(block[:-1])))
            spectrum = np.abs(np.fft.rfft(block * np.hanning(len(block)))) ** 2
            frequencies = np.fft.rfftfreq(len(block), 1.0 / AUDIO_SAMPLE_RATE)
            total = float(np.sum(spectrum[frequencies <= 7600]) + 1e-12)
            band_ratio = float(
                np.sum(spectrum[(frequencies >= 120) & (frequencies <= 3800)]) / total
            )
            active = dbfs >= -42.0 and 0.008 <= zcr <= 0.32 and band_ratio >= 0.55
            blocks.append((active, dbfs + band_ratio * 12.0))
    except Exception:
        return [], window_start, False

    intervals: list[dict[str, float]] = []
    index = 0
    while index < len(blocks):
        if not blocks[index][0]:
            index += 1
            continue
        first = index
        while index + 1 < len(blocks) and blocks[index + 1][0]:
            index += 1
        last = index
        peak_index = max(range(first, last + 1), key=lambda item: blocks[item][1])
        intervals.append({
            "start": window_start + first * VAD_BLOCK_SECONDS,
            "end": window_start + (last + 1) * VAD_BLOCK_SECONDS,
            "peak": window_start + (peak_index + 0.5) * VAD_BLOCK_SECONDS,
            "peak_score": blocks[peak_index][1],
            "duration": (last - first + 1) * VAD_BLOCK_SECONDS,
        })
        index += 1
    return intervals, window_start, True


def _keyframes_near_window(
    video: Path,
    window_start: float,
    deadline: float,
) -> tuple[list[float], bool]:
    ffprobe = Path(core.FFMPEG).with_name("ffprobe.exe")
    if not ffprobe.is_file():
        return [], False
    query_start = max(0.0, window_start - KEYFRAME_LOOKBACK_SECONDS)
    query_duration = AUDIO_WINDOW_SECONDS + (window_start - query_start)
    remaining = _remaining(deadline)
    if remaining <= 0.5:
        return [], False
    try:
        completed = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-skip_frame",
                "nokey",
                "-read_intervals",
                f"{query_start:.3f}%+{query_duration:g}",
                "-show_entries",
                "frame=best_effort_timestamp_time,key_frame,pict_type",
                "-of",
                "json",
                str(video),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.5, min(10.0, remaining)),
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], False
    if completed.returncode != 0:
        return [], False
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
        keyframes = []
        for frame in payload.get("frames", []):
            if int(frame.get("key_frame", 0) or 0) != 1 and frame.get("pict_type") != "I":
                continue
            keyframes.append(float(frame["best_effort_timestamp_time"]))
        return sorted(set(keyframes)), True
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [], False


def _speech_decode_attempts(
    intervals: list[dict[str, float]],
    keyframes: list[float],
) -> list[tuple[dict[str, float], float]]:
    attempts: list[tuple[dict[str, float], float]] = []
    for interval in sorted(intervals, key=lambda item: item["start"]):
        inside = [
            value
            for value in keyframes
            if interval["start"] <= value <= interval["end"]
        ]
        if inside:
            attempts.append((interval, min(inside)))
            continue
        preceding = [value for value in keyframes if value <= interval["start"]]
        if preceding:
            attempts.append((interval, max(preceding)))
    return attempts[:MAX_SPEECH_INTERVAL_ATTEMPTS]


def _select_speech_interval(
    intervals: list[dict[str, float]],
    keyframes: list[float],
) -> tuple[dict[str, float], float] | None:
    attempts = _speech_decode_attempts(intervals, keyframes)
    return attempts[0] if attempts else None


def _extract_speech_frames(
    video: Path,
    interval: dict[str, float],
    keyframe: float,
    destination: Path,
    deadline: float,
) -> list[Path]:
    remaining = _remaining(deadline)
    if remaining <= 0.5:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    lead_in = max(0.0, interval["start"] - keyframe)
    capture_duration = min(
        SHORT_DECODE_SECONDS,
        max(FRAME_INTERVAL_SECONDS, interval["duration"] + AFTER_SPEECH_SECONDS),
    )
    frame_limit = max(1, math.ceil(capture_duration / FRAME_INTERVAL_SECONDS))
    filter_graph = (
        f"fps={1.0 / FRAME_INTERVAL_SECONDS:g},"
        "crop=iw:ih*0.42:0:ih*0.58,"
        "scale=1280:-2:force_original_aspect_ratio=decrease"
    )
    pattern = destination / "frame-%02d.png"
    try:
        completed = subprocess.run(
            [
                core.FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{keyframe:.6f}",
                "-noaccurate_seek",
                "-i",
                str(video),
                "-ss",
                f"{lead_in:.6f}",
                "-t",
                f"{capture_duration:.6f}",
                "-an",
                "-vf",
                filter_graph,
                "-frames:v",
                str(frame_limit),
                "-vsync",
                "0",
                "-y",
                str(pattern),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(0.5, min(15.0, remaining)),
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return sorted(destination.glob("frame-*.png"))[:frame_limit]


def _visual_screen(image_path: Path) -> VisualScreen:
    try:
        import cv2
        import numpy as np

        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return VisualScreen("unknown", reason="image-unreadable")
        height, width = image.shape[:2]
        if height < 40 or width < 160:
            return VisualScreen("unknown", reason="image-too-small")

        channels = list(cv2.split(image))
        channels.append(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
        best_score = 0.0
        best_reason = ""
        for channel_index, channel in enumerate(channels):
            channel = cv2.GaussianBlur(channel, (3, 3), 0)
            edges = cv2.Canny(channel, 35, 110)
            contours, _hierarchy = cv2.findContours(
                edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
            )
            components: list[tuple[int, int, int, int]] = []
            for contour in contours:
                x, y, width_box, height_box = cv2.boundingRect(contour)
                if not (4 <= height_box <= max(14, int(height * 0.22))):
                    continue
                if not (2 <= width_box <= max(24, int(width * 0.12))):
                    continue
                aspect = width_box / max(1.0, height_box)
                if not (0.08 <= aspect <= 8.0) or width_box * height_box < 16:
                    continue
                components.append((x, y, width_box, height_box))
            if len(components) < 6:
                continue

            for anchor in components:
                anchor_height = anchor[3]
                anchor_center = anchor[1] + anchor_height / 2.0
                row = [
                    component
                    for component in components
                    if 0.55 * anchor_height <= component[3] <= 1.8 * anchor_height
                    and abs(component[1] + component[3] / 2.0 - anchor_center)
                    <= max(4.0, anchor_height * 0.55)
                ]
                if len(row) < 6:
                    continue
                row.sort(key=lambda item: (item[0], item[2], item[1]))
                unique: list[tuple[int, int, int, int]] = []
                for component in row:
                    if unique and (
                        abs(component[0] - unique[-1][0]) <= 2
                        and abs(component[2] - unique[-1][2]) <= 2
                    ):
                        continue
                    unique.append(component)
                if len(unique) < 8:
                    continue

                heights = np.asarray([item[3] for item in unique], dtype=np.float32)
                centres = np.asarray(
                    [item[1] + item[3] / 2.0 for item in unique], dtype=np.float32
                )
                median_height = float(np.median(heights))
                height_cv = float(np.std(heights) / max(1.0, np.mean(heights)))
                baseline_spread = float(np.std(centres) / max(1.0, median_height))
                span_ratio = (
                    max(item[0] + item[2] for item in unique)
                    - min(item[0] for item in unique)
                ) / max(1.0, width)
                if not (0.07 <= span_ratio <= 0.88):
                    continue
                if height_cv > 0.28 or baseline_spread > 0.18:
                    continue

                score = min(1.0, len(unique) / 20.0) * 0.45
                score += min(1.0, span_ratio / 0.25) * 0.25
                score += max(0.0, 1.0 - height_cv / 0.28) * 0.15
                score += max(0.0, 1.0 - baseline_spread / 0.18) * 0.15
                if score > best_score:
                    best_score = score
                    best_reason = f"regular-text-line-channel-{channel_index}"
                if score >= 0.62:
                    return VisualScreen("suspicious", score, best_reason)
        if best_score >= 0.52:
            return VisualScreen("suspicious", best_score, best_reason or "regular-text-line")
        return VisualScreen("clear", best_score, "no-aligned-text-structure")
    except Exception as exc:
        return VisualScreen("unknown", reason=f"screen-error:{exc}")


def _first_meaningful_line(lines: list[str]) -> str:
    return next((line for line in lines if _meaningful_line(line)), "")



@dataclass(frozen=True)
class WindowScan:
    ratio: float
    complete: bool
    frame_count: int
    text: str = ""
    second: float = 0.0
    reason: str = ""


def _scan_window(
    video: Path,
    duration: float,
    ratio: float,
    languages: str,
    root: Path,
    deadline: float,
) -> WindowScan:
    label = f"{int(round(ratio * 100))}%"
    intervals, window_start, vad_completed = _speech_intervals(
        video, duration, ratio, deadline
    )
    if not vad_completed:
        return WindowScan(ratio, False, 0, reason=f"{label}位置音频分析未完成")
    if not intervals:
        return WindowScan(ratio, True, 0, reason=f"{label}位置没有找到可用讲话段")

    keyframes, keyframes_completed = _keyframes_near_window(
        video, window_start, deadline
    )
    if not keyframes_completed:
        return WindowScan(ratio, False, 0, reason=f"{label}位置关键帧查询未完成")
    attempts = _speech_decode_attempts(intervals, keyframes)
    if not attempts:
        return WindowScan(
            ratio,
            False,
            0,
            reason=f"{label}位置讲话段附近没有找到可用关键帧",
        )

    total_frames = 0
    completed_attempts = 0
    for attempt_index, (interval, keyframe) in enumerate(attempts, start=1):
        frames = _extract_speech_frames(
            video,
            interval,
            keyframe,
            root / f"frames-{int(round(ratio * 100))}-{attempt_index}",
            deadline,
        )
        if not frames:
            if _remaining(deadline) <= 1.0:
                return WindowScan(
                    ratio,
                    False,
                    total_frames,
                    reason="烧录字幕检测总时间已用完",
                )
            continue

        completed_attempts += 1
        total_frames += len(frames)
        screens = [(frame, _visual_screen(frame)) for frame in frames]
        candidates = [
            (frame, screen)
            for frame, screen in screens
            if screen.state != "clear"
        ]
        candidates.sort(key=lambda item: item[1].score, reverse=True)
        frame_start = max(interval["start"], keyframe)
        for frame, _screen in candidates:
            remaining = _remaining(deadline)
            if remaining <= 1.0:
                return WindowScan(
                    ratio,
                    False,
                    total_frames,
                    reason="烧录字幕检测总时间已用完",
                )
            try:
                lines = _ocr_lines(
                    frame,
                    languages,
                    timeout_seconds=min(3.0, max(1.0, remaining / 2.0)),
                )
            except (OSError, subprocess.SubprocessError):
                return WindowScan(
                    ratio,
                    False,
                    total_frames,
                    reason=f"{label}位置字幕文字识别未完成",
                )
            text = _first_meaningful_line(lines)
            if text:
                frame_number = frames.index(frame)
                return WindowScan(
                    ratio,
                    True,
                    total_frames,
                    text=text[:160],
                    second=round(
                        frame_start + frame_number * FRAME_INTERVAL_SECONDS,
                        3,
                    ),
                )

    if completed_attempts == 0:
        return WindowScan(
            ratio,
            False,
            total_frames,
            reason=f"{label}位置讲话段连续解码未完成",
        )
    return WindowScan(
        ratio,
        True,
        total_frames,
        reason=f"{label}位置未检测到烧录字幕",
    )


def _detect_impl(
    video_path: str,
    media_data: dict | None = None,
    audio_languages: list[str] | None = None,
    time_budget_seconds: float = DETECTION_TIME_BUDGET_SECONDS,
) -> DetectionResult:
    video = Path(video_path)
    if not video.is_file():
        return DetectionResult(False, 0, 0, "影片文件不存在")
    key = _cache_key(video)
    with _CACHE_LOCK:
        cached = _read_cache().get(key)
    if isinstance(cached, dict):
        return DetectionResult(
            bool(cached.get("detected")),
            int(cached.get("evidence_count", 0)),
            int(cached.get("sample_count", 0)),
            str(cached.get("detail", "")),
            str(cached.get("status", "")),
            float(cached.get("confidence", 0.0) or 0.0),
        )

    if not Path(core.FFMPEG).is_file() or not Path(core.TESSERACT).is_file():
        return DetectionResult(False, 0, 0, "烧录字幕检测组件不可用", "uncertain", 0.0)
    media_data = media_data or core.inspect_media(str(video))
    duration = _duration_seconds(video, media_data)
    languages = _available_ocr_languages(audio_languages or [])
    if duration <= 30 or not languages:
        return DetectionResult(False, 0, 0, "影片过短或 OCR 语言库不可用", "uncertain", 0.0)

    deadline = time.monotonic() + max(5.0, time_budget_seconds)
    root = _runtime_dir()
    scans: list[WindowScan] = []
    evidence: list[dict[str, object]] = []
    try:
        for ratio in AUDIO_RATIOS:
            scan = _scan_window(
                video,
                duration,
                ratio,
                languages,
                root,
                deadline,
            )
            scans.append(scan)
            if scan.text:
                evidence.append({
                    "ratio": ratio,
                    "second": scan.second,
                    "text": scan.text,
                })
                break
            if _remaining(deadline) <= 1.0:
                break
    finally:
        shutil.rmtree(root, ignore_errors=True)

    frame_count = sum(scan.frame_count for scan in scans)
    if evidence:
        source_ratio = int(round(float(evidence[0]["ratio"]) * 100))
        status = "detected"
        detected = True
        confidence = 0.90
        detail = (
            f"在影片{source_ratio}%附近的对话窗口识别到烧录字幕："
            f"{str(evidence[0]['text'])[:80]}"
        )
    elif len(scans) == len(AUDIO_RATIOS) and all(scan.complete for scan in scans):
        status = "absent"
        detected = False
        confidence = 0.86
        detail = (
            f"依次检查影片30%和65%附近的20秒对话窗口，"
            f"共筛查 {frame_count} 张连续画面，未检测到烧录字幕"
        )
    else:
        status = "uncertain"
        detected = False
        confidence = 0.0
        reasons = [scan.reason for scan in scans if not scan.complete and scan.reason]
        if len(scans) < len(AUDIO_RATIOS):
            reasons.append("65%位置未能完成检查")
        detail = f"{'；'.join(reasons) or '有效检查未完成'}，暂不能确认是否存在烧录字幕"

    result = DetectionResult(
        detected,
        len(evidence),
        frame_count,
        detail,
        status,
        confidence,
    )
    if result.conclusive:
        with _CACHE_LOCK:
            cache = _read_cache()
            cache[key] = {
                "detected": result.detected,
                "evidence_count": result.evidence_count,
                "sample_count": result.sample_count,
                "detail": result.detail,
                "status": result.status,
                "confidence": result.confidence,
                "evidence": evidence,
            }
            if len(cache) > 200:
                cache = dict(list(cache.items())[-200:])
            _write_cache(cache)
    return result


def detect(
    video_path: str,
    media_data: dict | None = None,
    audio_languages: list[str] | None = None,
    time_budget_seconds: float = DETECTION_TIME_BUDGET_SECONDS,
) -> DetectionResult:

    with _DETECTION_GATE:
        return _detect_impl(
            video_path,
            media_data,
            audio_languages,
            time_budget_seconds,
        )
