# -*- coding: utf-8 -*-
"""SubFlow processing core.

This module deliberately reuses the stable remux/Ollama functions from the
finished Advanced product but adds only the new subtitle-source routes here.
"""
from __future__ import annotations

import os
import re
import json
import hashlib
import shutil
import subprocess
import sys
import threading
import concurrent.futures
import difflib
import math
import statistics
import time
import unicodedata
import uuid
import wave
import weakref
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import subtitle_tool_core as legacy
import chinese_script_converter
import audio_offset_verifier
import audio_fine_aligner
import audio_precision_gate
import subtitle_offset_guard
import pgs_local_alignment


class SubtitlePreflightError(RuntimeError):
    """Base class for an external-subtitle preflight terminal condition."""


class SubtitleContentMismatchError(SubtitlePreflightError):
    """The check completed normally and proved that the content does not match."""


class SubtitleVerificationToolError(SubtitlePreflightError):
    """A required local verifier failed; trying another subtitle cannot fix it."""


class SubtitlePreflightTimeoutError(SubtitlePreflightError):
    """The bounded verifier exhausted its hard deadline."""


class SharedImageTimingPreparationError(SubtitleVerificationToolError):
    """Film-level image IO failed; changing text candidates cannot fix it."""


class SharedImageTimingPreparationRequired(RuntimeError):
    """Ask the search controller to perform shared IO outside its match clock."""
    def __init__(self, input_path, subtitle_stream_index, cache_dir, duration):
        super().__init__("局部图片时间点证据不足，需要一次影片共用的后备读取。")
        self.input_path = input_path
        self.subtitle_stream_index = subtitle_stream_index
        self.cache_dir = Path(cache_dir)
        self.duration = duration


def is_preflight_infrastructure_error(value: object) -> bool:
    return isinstance(value, (SubtitleVerificationToolError, SubtitlePreflightTimeoutError)) or str(value).startswith(
        ("字幕核验工具异常：", "字幕体检达到")
    )


LANGUAGE_ALIASES = {
    "eng": "en", "english": "en",
    "spa": "es", "spanish": "es",
    "jpn": "ja", "japanese": "ja",
    "kor": "ko", "korean": "ko",
    "fra": "fr", "fre": "fr", "french": "fr",
    "deu": "de", "ger": "de", "german": "de",
    "por": "pt", "portuguese": "pt",
    "rus": "ru", "russian": "ru",
    "hin": "hi", "hindi": "hi",
    "chi": "zh-CN", "zho": "zh-CN", "zh": "zh-CN", "chs": "zh-CN",
    "cht": "zh-TW", "zh-tw": "zh-TW", "zh-cn": "zh-CN",
}

LATIN_LANGUAGE_WORDS = {
    "en": {"the", "and", "you", "that", "this", "what", "with", "have", "not", "are", "for"},
    "es": {"que", "los", "las", "una", "por", "para", "con", "como", "pero", "está", "del"},
    "fr": {"les", "des", "une", "est", "pas", "pour", "avec", "dans", "mais", "vous", "que"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "für", "auf", "ich", "sie"},
    "pt": {"que", "uma", "não", "por", "para", "com", "como", "mas", "você", "está", "dos"},
}


def normalize_language_code(value: str) -> str:
    code = (value or "").strip().lower().replace("_", "-")
    if code in {"", "auto", "und"}:
        return "und"
    if code in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[code]
    for target, (mkv_code, english_name, _label) in legacy.LANGUAGES.items():
        if code in {target.lower(), mkv_code.lower(), english_name.lower()}:
            return target
    return "und"


def detect_subtitle_language(events: list[legacy.SubtitleEvent], hint: str = "") -> str:
    normalized = normalize_language_code(hint)
    if normalized != "und":
        return normalized
    text = " ".join(event.text for event in events[:300])
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if re.search(r"[\u0400-\u04ff]", text):
        return "ru"
    if re.search(r"[\u3400-\u9fff]", text):
        traditional_markers = len(re.findall(r"[這個們為與說會來時麼還裡後沒對從讓將於]", text))
        simplified_markers = len(re.findall(r"[这个们为与说会来时么还里后没对从让将于]", text))
        return "zh-TW" if traditional_markers > simplified_markers else "zh-CN"
    words = re.findall(r"[A-Za-zÀ-ÿ]+", text.lower())
    scores = {code: sum(word in markers for word in words) for code, markers in LATIN_LANGUAGE_WORDS.items()}
    best = max(scores, key=scores.get, default="und")
    return best if scores.get(best, 0) >= 3 else "und"


def subtitle_track_identity(events: list[legacy.SubtitleEvent], hint: str = "") -> tuple[str, str]:
    language = detect_subtitle_language(events, hint)
    if language == "und":
        return "und", "字幕"
    return language, f"{legacy.LANGUAGES[language][2]}字幕"


FFSUBSYNC_CANDIDATES = (
    Path(legacy.resolve_config_path(r"tools\ffsubsync\ffsubsync.exe")),
)
ALASS_CANDIDATES = (
    Path(legacy.resolve_config_path(r"tools\alass\bin\alass-cli.exe")),
)
ALASS_FALLBACK_TIMEOUT_SECONDS = 180.0
MAX_AUTOMATIC_OFFSET_SECONDS = 10.0
MAX_AUTOMATIC_OFFSET_MILLISECONDS = int(MAX_AUTOMATIC_OFFSET_SECONDS * 1000)
SHARED_SUBTITLE_PROBE_FRACTIONS = (0.22, 0.52, 0.79)
SEMANTIC_SAMPLE_SECONDS = 12.0
MANUAL_CONFIRMATION_PREFIX = "需要人工确认："
MANUAL_CROSS_LANGUAGE_BUDGET_SECONDS = 20.0
_SUBTITLE_DIAGNOSTIC_LOG_LOCK = threading.Lock()
WHISPER_CLI_CANDIDATES = (
    Path(legacy.resolve_config_path(r"tools\whisper\bin\whisper-cli.exe")),
)
WHISPER_MODEL_CANDIDATES = (
    Path(legacy.resolve_config_path(r"tools\whisper\models\ggml-base.bin")),
    Path(legacy.resolve_config_path(r"tools\whisper\models\ggml-small.bin")),
)


def _prepare_bundled_tool_path() -> None:
    ffmpeg_dir = Path(legacy.resolve_config_path(r"tools\ffmpeg\bin"))
    if not ffmpeg_dir.exists():
        return
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    if str(ffmpeg_dir).lower() not in {entry.lower() for entry in entries}:
        os.environ["PATH"] = str(ffmpeg_dir) + (os.pathsep + current if current else "")


_prepare_bundled_tool_path()


def _audio_fine_shadow_enabled() -> bool:
    return os.environ.get("SUBFLOW_AUDIO_FINE_SHADOW", "").strip() == "1"


def _alass_fallback_enabled() -> bool:
    return os.environ.get("SUBFLOW_ALASS_FALLBACK", "1").strip() == "1"


def _tool(candidates: tuple[Path, ...], name: str) -> str:
    dynamic = shutil.which("ffsubsync") if name == "字幕对齐引擎 ffsubsync" else None
    for candidate in (*candidates, *([Path(dynamic)] if dynamic else [])):
        if candidate and candidate.exists():
            return str(candidate)
    raise RuntimeError(f"未找到 {name}。本机预览需要先准备该工具。")


def subtitle_diagnostic_log_path(input_path: str | Path) -> Path:
    source = Path(input_path)
    return source.with_suffix("").with_name(source.stem + "_pro_work") / "subtitle-diagnostics.log"


def append_subtitle_diagnostic_log(input_path: str | Path, message: str) -> None:
    path = subtitle_diagnostic_log_path(input_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _SUBTITLE_DIAGNOSTIC_LOG_LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[{timestamp}] {message}\n")


def persistent_subtitle_logger(
    input_path: str | Path,
    downstream: Callable[[str], None],
) -> Callable[[str], None]:
    if getattr(downstream, "_subflow_persistent_subtitle_logger", False):
        return downstream

    def write(message: str) -> None:
        append_subtitle_diagnostic_log(input_path, message)
        downstream(message)

    setattr(write, "_subflow_persistent_subtitle_logger", True)
    return write


def _run(
    args: list[str],
    log: Callable[[str], None],
    cancel: threading.Event | None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    return legacy.run_command(args, log=log, cancel_event=cancel, cwd=cwd)


def subtitle_events_to_srt(source: Path, destination: Path) -> Path:
    events = legacy.parse_subtitle(source)
    if not events:
        raise RuntimeError("外挂字幕中没有可识别的时间轴内容。请使用 SRT、ASS 或 VTT 文件。")
    legacy.write_srt(destination, events, {index: event.text for index, event in enumerate(events, 1)})
    return destination


def _audio_stream_index(input_path: str, selected_audio_id: int | None) -> int | None:
    if selected_audio_id is None:
        return None
    audio = [track for track in legacy.inspect_tracks(input_path) if track.type == "audio"]
    for index, track in enumerate(audio):
        if track.id == selected_audio_id:
            return index
    return None


def _audio_container_start_delta(input_path: str, selected_audio_id: int | None) -> float:
    """Return the selected audio start relative to video in container timeline seconds."""
    try:
        media = legacy.inspect_media(input_path)
    except Exception:
        return 0.0
    tracks = media.get("tracks", [])
    video = next((track for track in tracks if track.get("type") == "video"), None)
    audio_tracks = [track for track in tracks if track.get("type") == "audio"]
    audio = next((track for track in audio_tracks if track.get("id") == selected_audio_id), None)
    audio = audio or (audio_tracks[0] if audio_tracks else None)
    if video is None or audio is None:
        return 0.0
    try:
        video_start = float(video.get("properties", {}).get("minimum_timestamp", 0) or 0)
        audio_start = float(audio.get("properties", {}).get("minimum_timestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return (audio_start - video_start) / 1_000_000_000


def _embedded_subtitle_reference(
    input_path: str,
    source_language: str,
) -> tuple[str, int, legacy.Track] | None:
    subtitles = [track for track in legacy.inspect_tracks(input_path) if track.type == "subtitles"]
    requested_language = normalize_language_code(source_language)
    candidates: list[tuple[int, int, legacy.Track]] = []
    for stream_index, track in enumerate(subtitles):
        name = (track.name or "").lower()
        if track.forced or "forced" in name or "强制" in name:
            continue
        if not (track.text_subtitle or track.pgs_subtitle or track.vobsub_subtitle):
            continue
        track_language = normalize_language_code(track.language)
        language_score = 3 if requested_language != "und" and track_language == requested_language else 1
        type_score = 2 if track.text_subtitle else 1
        candidates.append((language_score, type_score, track))
    if not candidates:
        return None
    _language_score, _type_score, selected = max(
        candidates,
        key=lambda item: (item[0], item[1], item[2].default),
    )
    stream_index = subtitles.index(selected)
    mode = "pgs" if selected.pgs_subtitle else "subtitle"
    return mode, stream_index, selected


def _infer_pgs_intervals(packets: list[dict]) -> list[tuple[float, float]]:
    parsed: list[tuple[float, float | None, int]] = []
    for packet in packets:
        try:
            pts = float(packet.get("pts_time"))
            size = int(packet.get("size"))
        except (TypeError, ValueError):
            continue
        try:
            duration = float(packet.get("duration_time"))
            if duration <= 0:
                duration = None
        except (TypeError, ValueError):
            duration = None
        parsed.append((pts, duration, size))
    parsed.sort(key=lambda item: item[0])

    intervals: list[tuple[float, float]] = []
    open_start: float | None = None
    for pts, duration, size in parsed:
        if size <= 50:
            if open_start is not None and pts > open_start:
                intervals.append((open_start, min(pts, open_start + 15.0)))
                open_start = None
            continue
        if duration is not None:
            intervals.append((pts, pts + min(duration, 15.0)))
            open_start = None
            continue
        if open_start is None:
            open_start = pts
        elif pts - open_start > 0.25:
            intervals.append((open_start, min(pts, open_start + 8.0)))
            open_start = pts
    if open_start is not None:
        intervals.append((open_start, open_start + 4.0))
    return [
        (start, end) for start, end in intervals
        if end > start and end - start <= 15.0
    ]


def _activity_bins(intervals: list[tuple[float, float]], sample_rate: int = 2) -> set[int]:
    bins: set[int] = set()
    for start, end in intervals:
        first = max(0, int(round(start * sample_rate)))
        last = max(first + 1, int(round(end * sample_rate)))
        bins.update(range(first, last))
    return bins


def _interval_alignment(
    reference_intervals: list[tuple[float, float]],
    candidate_intervals: list[tuple[float, float]],
) -> tuple[float, float]:
    coarse_rate = 2
    coarse_reference_bins = _activity_bins(reference_intervals, coarse_rate)
    coarse_candidate_bins = _activity_bins(candidate_intervals, coarse_rate)
    if not coarse_reference_bins or not coarse_candidate_bins:
        return 0.0, 0.0
    coarse_shift = 0
    coarse_score = -1.0
    for shift in range(-120 * coarse_rate, 120 * coarse_rate + 1):
        overlap = sum((value + shift) in coarse_reference_bins for value in coarse_candidate_bins)
        score = overlap / max(1, min(len(coarse_candidate_bins), len(coarse_reference_bins)))
        if score > coarse_score:
            coarse_score = score
            coarse_shift = shift

    sample_rate = 10
    reference_bins = _activity_bins(reference_intervals, sample_rate)
    candidate_bins = _activity_bins(candidate_intervals, sample_rate)
    best_shift = round((coarse_shift / coarse_rate) * sample_rate)
    best_score = -1.0
    for shift in range(best_shift - sample_rate, best_shift + sample_rate + 1):
        overlap = sum((value + shift) in reference_bins for value in candidate_bins)
        score = overlap / max(1, min(len(candidate_bins), len(reference_bins)))
        if score > best_score:
            best_score = score
            best_shift = shift
    return best_score, best_shift / sample_rate


def _shifted_activity_overlap(
    reference_intervals: list[tuple[float, float]],
    candidate_intervals: list[tuple[float, float]],
    shift_seconds: float,
    start_seconds: float,
    end_seconds: float,
) -> float:
    sample_rate = 10
    reference = [
        (max(start, start_seconds), min(end, end_seconds))
        for start, end in reference_intervals
        if end > start_seconds and start < end_seconds
    ]
    candidate = [
        (max(start + shift_seconds, start_seconds), min(end + shift_seconds, end_seconds))
        for start, end in candidate_intervals
        if end + shift_seconds > start_seconds and start + shift_seconds < end_seconds
    ]
    reference_bins = _activity_bins(
        [(start, end) for start, end in reference if end > start],
        sample_rate,
    )
    candidate_bins = _activity_bins(
        [(start, end) for start, end in candidate if end > start],
        sample_rate,
    )
    if not reference_bins or not candidate_bins:
        return 0.0
    return len(reference_bins & candidate_bins) / max(
        1,
        min(len(reference_bins), len(candidate_bins)),
    )


def _image_ffprobe_command() -> str:
    ffprobe_candidates = (
        Path(legacy.resolve_config_path(r"tools\ffmpeg\bin\ffprobe.exe")),
        Path("dist") / "SubFlow" / "_internal" / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SubFlow" / "_internal" / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
    )
    ffprobe = next((path for path in ffprobe_candidates if path.is_file()), None)
    ffprobe_command = str(ffprobe) if ffprobe is not None else shutil.which("ffprobe")
    if not ffprobe_command:
        raise RuntimeError("未找到 ffprobe，无法读取图片字幕时间点。")
    return ffprobe_command


def _image_cache_signature(input_path: str, version: str) -> dict:
    video = Path(input_path)
    stat = video.stat()
    return {
        "video": str(video.resolve()).casefold(),
        "video_size": stat.st_size,
        "video_mtime_ns": stat.st_mtime_ns,
        "format": version,
    }


def _write_image_cache(path: Path, payload: dict) -> None:
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Cleanup must not replace the cache-write failure.


def _subtitle_stream_metadata(payload: dict) -> list[dict]:
    """Keep empty subtitle streams in the ordinal-to-container-index mapping."""
    streams = sorted(payload.get("streams", []), key=lambda item: int(item["index"]))
    if not streams:
        raise RuntimeError("图片字幕数据缺少容器轨道编号，不能安全对应字幕轨。")
    indices = [int(item["index"]) for item in streams]
    if len(indices) != len(set(indices)):
        raise RuntimeError("图片字幕容器轨道编号重复。")
    return [{"index": index, "codec_name": str(item.get("codec_name", ""))}
            for index, item in zip(indices, streams)]


def _embedded_image_intervals(
    input_path: str,
    subtitle_stream_index: int,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    cache_dir: Path | None = None,
) -> list[tuple[float, float]]:
    signature = _image_cache_signature(input_path, "all-image-subtitle-intervals-v3")

    cache_path = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / "all-image-subtitle-intervals.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("signature") == signature and payload.get("metadata"):
                intervals = [
                    (float(item[0]), float(item[1]))
                    for item in payload.get("streams", {}).get(str(subtitle_stream_index), [])
                ]
                log(f"复用图片字幕时间点缓存：{len(intervals)} 个显示区间。")
                return intervals
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    args = [
        _image_ffprobe_command(),
        "-v", "error",
        "-select_streams", "s",
        "-show_packets",
        "-show_streams",
        "-show_entries", "stream=index,codec_name:packet=stream_index,pts_time,duration_time,size,pos",
        "-of", "json",
        input_path,
    ]
    completed = legacy.run_command(args, log=lambda _message: None, cancel_event=cancel)
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
        metadata = _subtitle_stream_metadata(payload)
        packets = payload.get("packets", [])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("图片字幕时间点数据格式无效。") from exc
    packets_by_stream: dict[int, list[dict]] = {}
    for packet in packets:
        try:
            stream_id = int(packet.get("stream_index"))
        except (TypeError, ValueError):
            continue
        packets_by_stream.setdefault(stream_id, []).append(packet)
    streams = {
        str(index): _infer_pgs_intervals(packets_by_stream.get(item["index"], []))
        for index, item in enumerate(metadata)
    }
    intervals = streams.get(str(subtitle_stream_index), [])
    if cache_path is not None:
        _write_image_cache(cache_path, {"signature": signature, "streams": streams, "metadata": metadata})
    return intervals


def _has_current_full_image_cache(input_path, cache_dir) -> bool:
    try:
        payload = json.loads((cache_dir / "all-image-subtitle-intervals.json").read_text(encoding="utf-8"))
        return (payload.get("signature") == _image_cache_signature(input_path, "all-image-subtitle-intervals-v3")
                and bool(payload.get("metadata")) and isinstance(payload.get("streams"), dict))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def complete_shared_image_timing(request: SharedImageTimingPreparationRequired, log, cancel=None):
    """Read the old fallback once, using its existing film-level IO budget."""
    import continuous_vad
    legacy.check_cancel(cancel)
    limit = continuous_vad.image_preparation_budget_seconds(request.duration)
    deadline = _DeadlineCancel(cancel, time.monotonic() + limit)
    started = time.monotonic()
    log(f"局部图片时间点证据不足，开始一次影片共用的全片后备读取（最多{limit:g}秒）；"
        "本步骤不占候选匹配额度，后续候选复用。")
    try:
        _embedded_image_intervals(request.input_path, request.subtitle_stream_index,
                                  log, deadline, request.cache_dir)
        legacy.check_cancel(deadline)
    except legacy.CancelledError as exc:
        legacy.check_cancel(cancel)
        if deadline.timed_out:
            raise SharedImageTimingPreparationError(
                f"影片图片字幕后备读取达到{limit:g}秒上限；这是共享读取未完成，停止本片，不重复尝试字幕候选。") from exc
        raise
    except Exception as exc:
        raise SharedImageTimingPreparationError(f"影片图片字幕后备读取失败，停止本片：{exc}") from exc
    log(f"影片共用图片字幕后备读取完成，耗时 {time.monotonic() - started:.2f} 秒。")


def _pgs_sample_windows(
    reference_intervals: list[tuple[float, float]],
    duration: float,
) -> list[tuple[int, int]]:
    """Choose one dense, non-overlapping 120-second window in each third."""
    if duration < 360.0:
        return []
    starts = sorted(start for start, end in reference_intervals if end > start)
    windows: list[tuple[int, int]] = []
    for region in range(3):
        low = duration * region / 3
        high = duration * (region + 1) / 3
        midpoint = (low + high) / 2
        best: tuple[int, float, int] | None = None
        for cue_start in starts[bisect_left(starts, low):bisect_left(starts, high)]:
            window_start = int(max(low, min(cue_start - 60.0, high - 120.0)))
            count = bisect_left(starts, window_start + 120) - bisect_left(starts, window_start)
            candidate = (count, -abs(window_start + 60 - midpoint), window_start)
            if best is None or candidate > best:
                best = candidate
        if best is None or best[0] < 20:
            return []
        windows.append((best[2], best[2] + 120))
    return windows


def _merge_image_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
            raise ValueError("图片字幕读取范围无效。")
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _missing_image_ranges(requested, existing) -> list[tuple[float, float]]:
    missing = []
    for start, end in _merge_image_ranges(requested):
        cursor = start
        for low, high in _merge_image_ranges(existing):
            if high <= cursor or low >= end:
                continue
            if low > cursor:
                missing.append((cursor, min(low, end)))
            cursor = max(cursor, high)
            if cursor >= end:
                break
        if cursor < end:
            missing.append((cursor, end))
    return missing


def _pgs_local_packets(input_path, requested, log, cancel, cache_dir):
    """One seekable packet read for all subtitle streams, shared by candidates."""
    legacy.check_cancel(cancel)
    signature = _image_cache_signature(input_path, "pgs-local-packets-v1")
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "all-image-local-packets.json"
    metadata, packets, ranges = [], [], []
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("signature") == signature:
            metadata = _subtitle_stream_metadata(cached)
            ranges = _merge_image_ranges([tuple(map(float, item)) for item in cached["read_ranges"]])
            packets = cached["packets"]
            if not isinstance(packets, list):
                raise ValueError("无效字幕包缓存")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        metadata, packets, ranges = [], [], []
    missing = _missing_image_ranges(requested, ranges)
    if not missing and metadata:
        log("复用图片字幕局部时间点缓存，不重复读取影片。")
        return metadata, packets
    args = [
        _image_ffprobe_command(), "-v", "error", "-select_streams", "s",
        "-read_intervals", ",".join(f"{start:.6f}%{end:.6f}" for start, end in missing),
        "-show_packets", "-show_streams",
        "-show_entries", "stream=index,codec_name:packet=stream_index,pts_time,duration_time,size,pos",
        "-of", "json", input_path,
    ]
    completed = legacy.run_command(args, log=lambda _message: None, cancel_event=cancel)
    legacy.check_cancel(cancel)
    payload = json.loads(completed.stdout.decode("utf-8"))
    new_metadata = _subtitle_stream_metadata(payload)
    if metadata and metadata != new_metadata:
        raise RuntimeError("局部读取前后的字幕轨道编号不一致，不能合并缓存。")
    # Seeking may return an earlier keyframe and repeat packets across ranges.
    # Keep only the requested coverage and deduplicate before inferring clears.
    combined = {}
    coverage = _merge_image_ranges(ranges + missing)
    for packet in packets + payload.get("packets", []):
        try:
            pts = float(packet["pts_time"])
            stream_id = int(packet["stream_index"])
        except (KeyError, TypeError, ValueError):
            continue
        if not any(low <= pts < high for low, high in coverage):
            continue
        key = (stream_id, packet.get("pos"), packet.get("pts_time"),
               packet.get("size"), packet.get("duration_time"))
        combined[key] = packet
    packets = sorted(combined.values(), key=lambda item: (int(item["stream_index"]), float(item["pts_time"])))
    _write_image_cache(path, {"signature": signature, "streams": new_metadata,
                             "packets": packets, "read_ranges": coverage})
    return new_metadata, packets


def _pgs_expanded_windows(initial, duration, length):
    windows = []
    for region, (start, end) in enumerate(initial):
        low, high = duration * region / 3, duration * (region + 1) / 3
        width = min(float(length), high - low)
        left = max(low, min((start + end - width) / 2, high - width))
        windows.append((left, left + width))
    return windows


def _pgs_local_reason(reason: str) -> str:
    return {
        "offset_at_search_boundary": "局部提议的共同偏移触及自动搜索边界",
        "start_peak_at_search_boundary": "某一处的局部最佳偏移触及自动搜索边界",
        "shared_start_support_insufficient": "局部三处尚不能支持同一个固定偏移",
        "alternative_start_peak": "局部存在相近强度的其他偏移，不能确定",
        "start_residual_inconsistent": "局部估计未能支持统一固定偏移",
        "activity_cues_insufficient": "可比较的字幕条目不足",
        "activity_overlap_insufficient": "局部同一时间范围内的字幕显示区间不够一致",
        "mutual_anchors_insufficient": "相互对应的字幕起点不足或分布不够分散",
        "too_few_distinct_starts": "可比较的独立字幕起点不足",
        "empty_candidate_region": "某一区没有有效图片字幕时间点",
    }.get(reason, "局部时间点数据不足或无效")


def _pgs_local_corrections(
    input_path: str,
    subtitle_stream_indices: list[int],
    reference_intervals: list[tuple[float, float]],
    duration: float,
    log: Callable[[str], None],
    cancel: threading.Event | None,
    cache_dir: Path,
) -> dict[int, pgs_local_alignment.LocalAlignmentResult]:
    """Estimate a fixed shift locally, after the text anchor has been corrected."""
    legacy.check_cancel(cancel)
    if not subtitle_stream_indices or len(reference_intervals) < 20:
        return {}
    initial = _pgs_sample_windows(reference_intervals, duration)
    if len(initial) != 3:
        return {}
    started = time.monotonic()
    deadline = _DeadlineCancel(cancel, started + 12.0)
    results = {}
    pending = set(subtitle_stream_indices)
    # Old v2 caches cannot establish subtitle ordinals. Only v3 is reusable.
    try:
        cached = json.loads((cache_dir / "all-image-subtitle-intervals.json").read_text(encoding="utf-8"))
        if cached.get("signature") == _image_cache_signature(input_path, "all-image-subtitle-intervals-v3") and cached.get("metadata"):
            full_metadata = _subtitle_stream_metadata({"streams": cached["metadata"]})
            if not isinstance(cached["streams"], dict):
                raise ValueError("无效全片时间点缓存")
            for ordinal in sorted(pending):
                if ordinal < 0 or ordinal >= len(full_metadata):
                    raise RuntimeError(f"找不到图片字幕序号 {ordinal} 的容器轨道。")
                if full_metadata[ordinal]["codec_name"] != "hdmv_pgs_subtitle":
                    raise RuntimeError(f"字幕序号 {ordinal} 未对应 PGS 轨，不能按图片字幕纠偏。")
            log("已有有效的全片图片字幕时间点缓存，直接复用原有全片比较，不重复局部检查。")
            return {}
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    previous_windows = None
    completed_expansion = False
    last_regions = {}
    for minutes in (2, 4, 8):
        windows = _pgs_expanded_windows(initial, duration, minutes * 60)
        if windows == previous_windows:
            completed_expansion = True
            break
        previous_windows = windows
        requested = [(max(0.0, start - 15), min(duration, end + 15)) for start, end in windows]
        try:
            legacy.check_cancel(deadline)
            log(f"图片字幕局部检查：前、中、后各{minutes}分钟，全部字幕轨共用一次读取；扩展只补读新增范围。")
            metadata, packets = _pgs_local_packets(input_path, requested, log, deadline, cache_dir)
            for ordinal in sorted(pending):
                legacy.check_cancel(deadline)
                if ordinal < 0 or ordinal >= len(metadata):
                    raise RuntimeError(f"找不到图片字幕序号 {ordinal} 的容器轨道。")
                if metadata[ordinal]["codec_name"] != "hdmv_pgs_subtitle":
                    raise RuntimeError(f"字幕序号 {ordinal} 未对应 PGS 轨，不能按图片字幕纠偏。")
                stream_id = metadata[ordinal]["index"]
                regions = [_infer_pgs_intervals([
                    packet for packet in packets
                    if int(packet["stream_index"]) == stream_id and low <= float(packet["pts_time"]) < high
                ]) for low, high in requested]
                result = pgs_local_alignment.evaluate_local_alignment(reference_intervals, windows, regions)
                legacy.check_cancel(deadline)
                results[ordinal] = result
                last_regions[ordinal] = regions
                if result.accepted:
                    offset = result.offset_seconds
                    log(f"图片字幕序号 {ordinal}：三处共同固定偏移 {result.proposed_offset_seconds:+.2f} 秒；"
                        f"{'保持原时间轴' if offset == 0 else '可按此偏移纠正'}，局部{minutes}分钟检查通过。")
                else:
                    log(f"图片字幕序号 {ordinal}：局部{minutes}分钟证据不足：{_pgs_local_reason(result.reason)}")
            pending = {ordinal for ordinal in pending if not results[ordinal].accepted}
            completed_expansion = minutes == 8
            if not pending:
                break
        except legacy.CancelledError:
            # A parent candidate timeout/user cancellation must never be turned
            # into local acceptance or a renewed, longer deadline.
            legacy.check_cancel(cancel)
            if deadline.timed_out:
                log("图片字幕局部检查达到12秒上限，尚未通过的轨道转原有检查。")
                break
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if not any(result.accepted for result in results.values()):
                raise
            log(f"图片字幕局部读取未完成；已通过的轨道保留结果，其余转原有检查：{exc}")
            break
    if pending and completed_expansion:
        log("图片字幕局部扩展证据不足；仅在已读取范围内补查固定子窗口，"
            "每区最多10处，不再次读取影片。")
        for ordinal in sorted(pending):
            try:
                legacy.check_cancel(deadline)
                bounded = pgs_local_alignment.evaluate_bounded_alignment(
                    reference_intervals, previous_windows, last_regions[ordinal],
                    check_cancel=lambda: legacy.check_cancel(deadline),
                )
                legacy.check_cancel(deadline)
                log(f"图片字幕序号 {ordinal}：固定子窗口补查数量 "
                    f"{'/'.join(map(str, bounded.examined_windows))}，符合单区验收条件 "
                    f"{'/'.join(map(str, bounded.eligible_windows))}，"
                    f"共同偏移候选 {bounded.common_offset_count}，三处联合复核 {bounded.final_checks} 次。")
                if bounded.alignment.accepted:
                    result = bounded.alignment
                    results[ordinal] = result
                    chosen = '、'.join(f'{region.window[0]:.0f}～{region.window[1]:.0f}秒'
                                      for region in result.evidence)
                    log(f"图片字幕序号 {ordinal}：固定子窗口 {chosen} 检查通过；"
                        f"三处共同固定偏移 {result.proposed_offset_seconds:+.2f} 秒，"
                        f"{'保持原时间轴' if result.offset_seconds == 0 else '可按此偏移纠正'}。")
                else:
                    log(f"图片字幕序号 {ordinal}：固定子窗口仍证据不足，转原有全片检查。")
            except legacy.CancelledError:
                legacy.check_cancel(cancel)
                if deadline.timed_out:
                    log("图片字幕局部检查达到12秒上限，尚未通过的轨道转原有检查。")
                    break
                raise
    log(f"图片字幕局部检查耗时 {time.monotonic() - started:.2f} 秒；已通过 {sum(result.accepted for result in results.values())}/{len(subtitle_stream_indices)} 轨。")
    return results


def _pgs_sampled_noop(input_path, subtitle_stream_index, reference_intervals,
                      duration, log, cancel, cache_dir) -> bool:
    """Compatibility wrapper: production now also accepts supported shifts."""
    result = _pgs_local_corrections(input_path, [subtitle_stream_index], reference_intervals,
                                    duration, log, cancel, cache_dir).get(subtitle_stream_index)
    return bool(result and result.accepted and result.offset_seconds == 0)


def _interval_events(intervals: list[tuple[float, float]]) -> list[legacy.SubtitleEvent]:
    return [
        legacy.SubtitleEvent(
            legacy.srt_time_from_milliseconds(round(start * 1000)),
            legacy.srt_time_from_milliseconds(round(end * 1000)),
            f"subtitle event {index}",
        )
        for index, (start, end) in enumerate(intervals, 1)
    ]


def _write_interval_srt(intervals: list[tuple[float, float]], destination: Path) -> Path:
    events = _interval_events(intervals)
    legacy.write_srt(
        destination,
        events,
        {index: event.text for index, event in enumerate(events, 1)},
    )
    return destination


def _pgs_reference_candidate(
    input_path: str,
    normalized: Path,
    destination: Path,
    reference_stream_index: int,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    diagnostics: dict[str, float] | None = None,
    cache_dir: Path | None = None,
) -> Path:
    ffprobe_candidates = (
        Path(legacy.resolve_config_path(r"tools\ffmpeg\bin\ffprobe.exe")),
        Path("dist") / "SubFlow" / "_internal" / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SubFlow" / "_internal" / "tools" / "ffmpeg" / "bin" / "ffprobe.exe",
    )
    ffprobe = next((path for path in ffprobe_candidates if path.is_file()), None)
    ffprobe_command = str(ffprobe) if ffprobe is not None else shutil.which("ffprobe")
    if not ffprobe_command:
        raise RuntimeError("未找到 ffprobe，无法读取 PGS 时间点。")
    video = Path(input_path)
    signature = {
        "video_size": video.stat().st_size,
        "video_mtime_ns": video.stat().st_mtime_ns,
        "subtitle_stream_index": reference_stream_index,
        "format": "pgs-intervals-v2",
    }
    cache_path = None
    cached_intervals = None
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"pgs-stream-{reference_stream_index}-intervals.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("signature") == signature:
                cached_intervals = [
                    (float(item[0]), float(item[1]))
                    for item in payload.get("intervals", [])
                ]
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            cached_intervals = None

    if cached_intervals:
        reference_intervals = cached_intervals
        log(f"复用 PGS 时间点缓存：{len(reference_intervals)} 个显示区间。")
    else:
        args = [
            ffprobe_command,
            "-v", "error",
            "-select_streams", f"s:{reference_stream_index}",
            "-show_packets",
            "-show_entries", "packet=pts_time,duration_time,size",
            "-of", "json",
            input_path,
        ]
        completed = legacy.run_command(args, log=lambda _message: None, cancel_event=cancel)
        try:
            packets = json.loads(completed.stdout.decode("utf-8")).get("packets", [])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("PGS 时间点数据格式无效。") from exc
        reference_intervals = _infer_pgs_intervals(packets)
        if cache_path is not None and reference_intervals:
            cache_path.write_text(json.dumps(
                {"signature": signature, "intervals": reference_intervals},
                ensure_ascii=False,
            ), encoding="utf-8")
    candidate_events = legacy.parse_subtitle(normalized)
    candidate_intervals = [
        (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
        for event in candidate_events
    ]
    if len(reference_intervals) < 20 or len(candidate_intervals) < 20:
        raise RuntimeError("PGS 或候选字幕事件过少，不能作为可靠时间参照。")
    coarse_rate = 2
    coarse_reference_bins = _activity_bins(reference_intervals, coarse_rate)
    coarse_candidate_bins = _activity_bins(candidate_intervals, coarse_rate)
    if not coarse_reference_bins or not coarse_candidate_bins:
        raise RuntimeError("PGS 时间点没有形成可比较的字幕活动区间。")

    coarse_shift = 0
    coarse_score = -1.0
    for shift in range(-120 * coarse_rate, 120 * coarse_rate + 1):
        overlap = sum((value + shift) in coarse_reference_bins for value in coarse_candidate_bins)
        score = overlap / max(1, min(len(coarse_candidate_bins), len(coarse_reference_bins)))
        if score > coarse_score:
            coarse_score = score
            coarse_shift = shift

    sample_rate = 10
    reference_bins = _activity_bins(reference_intervals, sample_rate)
    candidate_bins = _activity_bins(candidate_intervals, sample_rate)
    coarse_seconds = coarse_shift / coarse_rate
    best_shift = round(coarse_seconds * sample_rate)
    best_score = -1.0
    for shift in range(best_shift - sample_rate, best_shift + sample_rate + 1):
        overlap = sum((value + shift) in reference_bins for value in candidate_bins)
        score = overlap / max(1, min(len(candidate_bins), len(reference_bins)))
        if score > best_score:
            best_score = score
            best_shift = shift
    if best_score < 0.08:
        raise RuntimeError(f"PGS 时间点相关度过低（{best_score:.0%}），已跳过该参照。")
    offset_seconds = best_shift / sample_rate
    if diagnostics is not None:
        diagnostics["score"] = best_score
        diagnostics["offset_seconds"] = offset_seconds
    log(
        f"PGS 轻量时间参照：{len(reference_intervals)} 个显示区间，"
        f"相关度 {best_score:.0%}，固定偏移 {offset_seconds:+.2f} 秒。"
    )
    return _shifted_srt(normalized, destination, int(round(offset_seconds * 1000)))


def _alignment_candidate(
    input_path: str,
    normalized: Path,
    destination: Path,
    selected_audio_id: int | None,
    mode: str,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    reference_stream_index: int | None = None,
    vad: str = "webrtc",
    diagnostics: dict[str, float] | None = None,
    reference_cache_dir: Path | None = None,
    shared_audio_reference: str | Path | None = None,
    max_offset_seconds: float = MAX_AUTOMATIC_OFFSET_SECONDS,
    audio_timeline_in_video_coordinates: bool = False,
) -> Path:
    if mode == "embedded-pgs":
        if reference_stream_index is None:
            raise RuntimeError("没有指定 PGS 参照轨道。")
        return _pgs_reference_candidate(
            input_path,
            normalized,
            destination,
            reference_stream_index,
            log,
            cancel,
            diagnostics,
            reference_cache_dir,
        )
    shared_audio = Path(shared_audio_reference) if shared_audio_reference else None
    use_shared_audio = bool(
        shared_audio
        and shared_audio.is_file()
        and mode.startswith("audio-")
    )
    reference_input = str(shared_audio) if use_shared_audio else input_path
    args = [
        _tool(FFSUBSYNC_CANDIDATES, "字幕对齐引擎 ffsubsync"),
        reference_input,
        "-i", str(normalized),
        "-o", str(destination),
        "--max-offset-seconds", f"{max_offset_seconds:g}",
        "--no-fix-framerate",
        "--encoding", "utf-8-sig", "--output-encoding", "utf-8-sig",
    ]
    if mode == "subtitle-file":
        pass
    elif mode == "embedded-subtitle":
        args += ["--reference-stream", f"s:{reference_stream_index}"]
    else:
        if not use_shared_audio:
            audio_index = _audio_stream_index(input_path, selected_audio_id)
            if audio_index is not None:
                args += ["--reference-stream", f"a:{audio_index}"]
        if not (use_shared_audio and shared_audio.suffix.lower() in {".npy", ".npz"}):
            args += ["--vad", vad]
        args += ["--frame-rate", "48000"]

    completed = _run(args, log, cancel)
    diagnostic = "\n".join(
        stream.decode("utf-8", errors="replace")
        for stream in (completed.stdout or b"", completed.stderr or b"")
    )
    score_match = re.search(r"score:\s*(-?\d+(?:\.\d+)?)", diagnostic, flags=re.I)
    offset_match = re.search(r"offset seconds:\s*(-?\d+(?:\.\d+)?)", diagnostic, flags=re.I)
    if not destination.exists() or destination.stat().st_size < 32:
        raise SubtitleVerificationToolError("字幕对齐引擎没有生成有效输出。")
    if not score_match or not offset_match:
        raise SubtitleVerificationToolError("字幕对齐引擎未返回偏移和匹配得分。")
    if score_match:
        score = float(score_match.group(1))
        raw_offset = float(offset_match.group(1)) if offset_match else 0.0
        container_delta = (
            _audio_container_start_delta(input_path, selected_audio_id)
            if mode.startswith("audio-") and not audio_timeline_in_video_coordinates
            else 0.0
        )
        offset = raw_offset + container_delta
        # ffsubsync limits cue duration to ten seconds while preprocessing.
        # It supplies the estimate only; always shift our intact input instead
        # of adopting its rewritten subtitle (counts alone cannot catch this).
        _shifted_srt(normalized, destination, int(round(offset * 1000)))
        if abs(container_delta) >= 0.005:
            log(
                f"音轨容器起点相对视频为 {container_delta:+.3f} 秒，"
                "已重新计入字幕时间轴。"
            )
        if diagnostics is not None:
            diagnostics["score"] = score
            diagnostics["offset_seconds"] = offset
            diagnostics["raw_offset_seconds"] = raw_offset
            diagnostics["container_start_delta_seconds"] = container_delta
        log(f"字幕时间轴匹配得分：{score:.1f}，固定偏移：{offset:+.2f} 秒")
    return destination


def _cleanup_alignment_temporary(path: Path, log: Callable[[str], None]) -> None:
    """Cleanup must never hide the preparation error or invalidate a ready cache."""
    for delay in (0.0, 0.05, 0.15, 0.3, 0.5):
        if delay:
            time.sleep(delay)
        try:
            path.unlink(missing_ok=True)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33):
                log(f'临时文件清理未完成：{path.name}；{exc}')
                return
    log(f'临时文件仍被占用，保留待清理：{path.name}；不会复用该临时文件。')


def vad_decode_mode(codec: str) -> str:
    import continuous_vad
    if continuous_vad.is_dts_codec(codec):
        return 'dts-core'
    if any(tag in (codec or '').lower() for tag in ('truehd', 'mlp')):
        return 'truehd-stereo'
    return 'full'


def vad_decoder_options(codec: str) -> list[str]:
    mode = vad_decode_mode(codec)
    return ['-core_only', '1'] if mode == 'dts-core' else (
        ['-downmix', 'stereo'] if mode == 'truehd-stereo' else [])


def vad_window_input_options(movie: str, start: float) -> list[str]:
    """Seek in the video's timeline, not relative to the selected audio start."""
    media = legacy.inspect_media(movie)
    video = next((t for t in media.get('tracks', []) if t.get('type') == 'video'), {})
    origin = float(video.get('properties', {}).get('minimum_timestamp', 0) or 0) / 1e9
    return ['-seek_timestamp', '1', '-ss', f'{origin + start:.6f}']


def _decode_dts_core_for_vad(source, audio_index, destination, log, cancel):
    """Transient PCM bridge: ffsubsync has no input-decoder option passthrough.

    Keep its 48k mono / async resampling, and let the existing alignment layer
    reapply the original container start delta exactly once. No retained audio cache.
    """
    legacy.check_cancel(cancel)
    _run([
        legacy.FFMPEG, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
        '-xerror', '-core_only', '1', '-i', str(source),
        '-map', f'0:a:{audio_index}', '-vn', '-sn', '-dn',
        '-ac', '1', '-acodec', 'pcm_s16le', '-af', 'aresample=async=1',
        '-ar', '48000', '-f', 'wav', str(destination),
    ], log, cancel)
    legacy.check_cancel(cancel)
    try:
        with wave.open(str(destination), 'rb') as wav:
            valid = (wav.getnchannels() == 1 and wav.getsampwidth() == 2
                     and wav.getframerate() == 48000 and wav.getnframes() > 0)
            if not valid or destination.stat().st_size < wav.getnframes() * 2:
                raise RuntimeError('DTS core 解码未生成完整的可用 PCM 音频。')
    except (wave.Error, EOFError, FileNotFoundError) as exc:
        raise RuntimeError('DTS core 解码未生成完整的可用 PCM 音频。') from exc


def _is_dts_core_decode_failure(exc):
    # Retry only decoder/no-core failures; disk, permissions and cancellation
    # must not trigger another full-film read or reset the preparation deadline.
    message = str(exc).lower()
    if any(x in message for x in ('space', 'disk full', '空间不足', 'permission', 'access denied')):
        return False
    return any(x in message for x in (
        'dts core 解码未生成', 'no valid dca', 'invalid data found',
        'error while decoding', 'error decoding', 'decode failed',
        "unrecognized option 'core_only'", "option core_only not found",
        'no core', 'core substream', 'core-only',
    ))


def _sampled_reference_info(reference: Path | str) -> dict | None:
    """Sampled origins are explicit: an NPZ alone carries no coordinates."""
    import sampled_vad as sample
    reference = Path(reference)
    expected_sampled = bool(re.fullmatch(r'speech-(50|60|75)\.npz',reference.name))
    try:
        info = json.loads(reference.with_suffix('.json').read_text(encoding='utf-8'))
        if info.get('timeline') != 'video-relative' or info.get('frame_rate_hz') != 100:
            if expected_sampled or 'range' in info:
                raise SubtitleVerificationToolError('取样指纹的时间原点或帧率缺失，不能当作整片指纹。')
            return None
        span = sample.FrameRange(info['range']['start_frame'], info['range']['end_frame'])
        expected = sample.centered_range(info['signature']['duration_seconds'], info['range']['fraction'])
        if span != expected:
            raise SubtitleVerificationToolError('取样指纹起点与声明的影片范围不一致。')
        if info['fingerprint_sha256'] != hashlib.sha256(reference.read_bytes()).hexdigest():
            raise SubtitleVerificationToolError('取样指纹内容与缓存签名不一致。')
        sample.read_npz(reference, span)
        return info
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if expected_sampled or reference.with_suffix('.json').is_file():
            raise SubtitleVerificationToolError('取样指纹坐标或缓存元数据无效。') from exc
        return None


def _build_sampled_alignment_audio(movie, audio_id, cache_dir, log, cancel,
                                   duration, fraction=.5, previous_reference=None):
    import continuous_vad
    import sampled_vad as sample
    legacy.check_cancel(cancel)
    source = Path(movie)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    tracks = [t for t in legacy.inspect_tracks(movie) if t.type == 'audio']
    selected = next((t for t in tracks if t.id == audio_id), None)
    if selected is None:
        raise SubtitleVerificationToolError('所选取证音轨不存在，不能静默改用其他音轨。')
    index = tracks.index(selected)
    target = sample.centered_range(duration, fraction)
    mode = vad_decode_mode(selected.codec)
    engine = _tool(FFSUBSYNC_CANDIDATES, '字幕对齐引擎 ffsubsync')
    stat = source.stat()
    signature = dict(
        rule=continuous_vad.FINGERPRINT_RULE_VERSION, source=str(source.resolve()), size=stat.st_size,
        mtime_ns=stat.st_mtime_ns, audio_id=audio_id, audio_index=index,
        duration_seconds=duration,
        audio_codec=selected.codec, decode_policy=mode, vad='webrtc',
        frame_rate_hz=100, pcm_rate=48000,
        input_coordinates=vad_window_input_options(movie, target.start_seconds),
        preprocessing='mono-s16le-async1-firstpts0-exact-grid', context_seconds=2,
        engine_sha256=hashlib.sha256(Path(engine).read_bytes()).hexdigest(),
        decoder_sha256=hashlib.sha256(Path(legacy.FFMPEG).read_bytes()).hexdigest(),
    )
    fingerprint = cache / f'speech-{round(fraction * 100)}.npz'
    marker = fingerprint.with_suffix('.json')
    try:
        saved = _sampled_reference_info(fingerprint)
        if (saved and saved['signature'] == signature
                and saved['range'] == dict(start_frame=target.start_frame, end_frame=target.end_frame,
                                           fraction=fraction)):
            log(f'复用影片中间{fraction:.0%}连续VAD指纹（{saved["actual_decode_mode"]}），不重复解码。')
            return fingerprint
    except (OSError, KeyError, TypeError, ValueError, SubtitleVerificationToolError):
        pass
    base_info = None
    if previous_reference is not None:
        base_info = _sampled_reference_info(previous_reference)
        if not base_info:
            raise SubtitleVerificationToolError('扩展所需的原取样坐标缺失，不能拼接。')
        # Ignore the changed seek point, but never reuse a different source,
        # decoder, engine, language track or coordinate-processing policy.
        stable = {k: v for k, v in signature.items() if k != 'input_coordinates'}
        previous_stable = {k: v for k, v in base_info['signature'].items() if k != 'input_coordinates'}
        if stable != previous_stable:
            raise SubtitleVerificationToolError('扩展前后的影片、音轨或解码规则不一致。')
    token = uuid.uuid4().hex
    temporaries = []
    actual_mode = base_info['actual_decode_mode'] if base_info else mode
    started = time.monotonic()
    decode_elapsed = vad_elapsed = 0.0

    def make_segment(span, name):
        nonlocal actual_mode, decode_elapsed, vad_elapsed
        wav = cache / f'pcm-{token}-{name}.wav'
        npz = wav.with_suffix('.npz')
        temporaries.extend((wav, npz))
        options = [] if actual_mode == 'full-fallback' else vad_decoder_options(selected.codec)
        def decode():
            nonlocal decode_elapsed
            before = time.monotonic()
            try:
                _run([
                    legacy.FFMPEG, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
                    '-xerror', *vad_window_input_options(movie, span.start_seconds), *options,
                    '-i', str(source), '-map', f'0:a:{index}', '-vn', '-sn', '-dn',
                    '-t', f'{span.duration_seconds:.2f}', '-ac', '1', '-c:a', 'pcm_s16le',
                    '-af', f'aresample=48000:async=1:first_pts=0,atrim=end_sample={span.pcm_sample_count}',
                    '-ar', '48000', '-f', 'wav', str(wav),
                ], log, cancel)
                legacy.check_cancel(cancel)
                with wave.open(str(wav), 'rb') as pcm:
                    sample.validate_pcm(span, pcm.getnframes(), pcm.getframerate(),
                                        pcm.getnchannels(), pcm.getsampwidth())
                    if wav.stat().st_size < pcm.getnframes() * 2:
                        raise SubtitleVerificationToolError('取样PCM文件不完整。')
            finally:
                decode_elapsed += time.monotonic() - before
        try:
            decode()
        except legacy.CancelledError:
            raise
        except RuntimeError as exc:
            legacy.check_cancel(cancel)
            if base_info or actual_mode != 'dts-core' or not _is_dts_core_decode_failure(exc):
                raise
            log(f'DTS core不可用，当前取样改用完整解码，共用剩余时间。原因：{exc}')
            actual_mode = 'full-fallback'
            options = []
            decode()
        before = time.monotonic()
        _run([engine, str(wav), '--serialize-speech', '--frame-rate', '48000',
              '--vad', 'webrtc', '--reference-stream', 'a:0',
              '--ffmpeg-path', str(Path(legacy.FFMPEG).parent)], log, cancel)
        vad_elapsed += time.monotonic() - before
        legacy.check_cancel(cancel)
        return sample.read_npz(npz, span)

    try:
        log(f'准备影片中间{fraction:.0%}连续VAD：{target.start_seconds:.2f}～{target.end_seconds:.2f}秒；解码方式 {actual_mode}。')
        if base_info:
            base_span = sample.FrameRange(base_info['range']['start_frame'], base_info['range']['end_frame'])
            base = sample.read_npz(previous_reference, base_span)
            plan = sample.extension_plan(base_span, target, duration)
            additions = {}
            for part in plan.segments:
                log(f'仅补取{part.name}侧新增区间 {part.owned_range.start_seconds:.2f}～'
                    f'{part.owned_range.end_seconds:.2f}秒，接缝另带2秒上下文；中间指纹复用。')
                additions[part.name] = make_segment(part.decode_range, part.name)
            payload = sample.stitch_reference(base, plan, additions)
        else:
            payload = make_segment(target, 'middle')
        latest = source.stat()
        if (latest.st_size, latest.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise SubtitleVerificationToolError('影片文件在取证期间发生变化，本次指纹不予缓存。')
        pending = cache / f'speech-{token}.npz'
        pending_marker = cache / f'speech-{token}.json.tmp'
        temporaries.extend((pending, pending_marker))
        sample.write_npz(pending, payload, target)
        info = dict(signature=signature, timeline='video-relative', frame_rate_hz=100,
                    range=dict(start_frame=target.start_frame, end_frame=target.end_frame, fraction=fraction),
                    actual_decode_mode=actual_mode,
                    fingerprint_sha256=hashlib.sha256(pending.read_bytes()).hexdigest(),
                    preparation_seconds=time.monotonic() - started,
                    decode_seconds=decode_elapsed, vad_seconds=vad_elapsed,
                    reused_fraction=base_info['range']['fraction'] if base_info else 0)
        pending_marker.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        legacy.check_cancel(cancel)
        os.replace(pending, fingerprint)
        os.replace(pending_marker, marker)
    except (ValueError, wave.Error, EOFError) as exc:
        raise SubtitleVerificationToolError(f'取样音频或指纹时间轴不完整：{exc}') from exc
    finally:
        for path in temporaries:
            _cleanup_alignment_temporary(path, log)
        log(f'中间{fraction:.0%}指纹准备耗时 {time.monotonic() - started:.2f}秒；'
            f'解码 {decode_elapsed:.2f}秒，VAD {vad_elapsed:.2f}秒。')
    return fingerprint


def _build_continuous_alignment_audio(
    input_path: str,
    selected_audio_id: int | None,
    cache_dir: str | Path,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    *, fraction: float = .5, previous_reference: Path | None = None,
) -> Path:
    import continuous_vad
    legacy.check_cancel(cancel)
    media = legacy.inspect_media(input_path)
    duration_ns = legacy.video_track_duration_ns(media) or legacy.media_duration_ns(media)
    if duration_ns > 0:
        return _build_sampled_alignment_audio(
            input_path, selected_audio_id, cache_dir, log, cancel,
            duration_ns / 1e9, fraction, previous_reference,
        )
    source = Path(input_path)
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    tracks = [t for t in legacy.inspect_tracks(input_path) if t.type == 'audio']
    selected = next((t for t in tracks if t.id == selected_audio_id), None)
    if selected is None:
        raise SubtitleVerificationToolError('所选取证音轨不存在，不能静默改用其他音轨。')
    audio_index = tracks.index(selected)
    decode_mode = vad_decode_mode(selected.codec)
    core_only = decode_mode == 'dts-core'
    fingerprint = cache / 'shared-alignment-speech.npz'
    marker = cache / 'shared-alignment-speech.json'
    stat = source.stat()
    engine = _tool(FFSUBSYNC_CANDIDATES, '字幕对齐引擎 ffsubsync')
    signature = {
        'source': str(source.resolve()), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
        'audio_id': selected_audio_id, 'audio_index': audio_index, 'audio_codec': selected.codec,
        'vad': 'webrtc', 'mode': 'continuous-v4-decoder-policy', 'frame_rate': 48000,
        'preprocessing': 'mono-s16le-aresample-async1',
        'decode_policy': 'dts-core-with-decoder-fallback-v1' if core_only else decode_mode,
        'engine_sha256': hashlib.sha256(Path(engine).read_bytes()).hexdigest(),
    }
    if decode_mode != 'full':
        signature['pcm_decoder_sha256'] = hashlib.sha256(Path(legacy.FFMPEG).read_bytes()).hexdigest()
    try:
        cached = json.loads(marker.read_text(encoding='utf8'))
        allowed_modes = ('dts-core', 'full-fallback') if core_only else (decode_mode,)
        if (cached['signature'] == signature and cached['actual_decode_mode'] in allowed_modes
                and fingerprint.stat().st_size > 128
                and cached['fingerprint_sha256'] == hashlib.sha256(fingerprint.read_bytes()).hexdigest()):
            log(f"复用影片连续VAD指纹（解码方式：{cached['actual_decode_mode']}），不重复解码。")
            return fingerprint
    except (OSError, ValueError, KeyError, TypeError):
        pass

    token = uuid.uuid4().hex
    reference_proxy = cache / f'shared-alignment-{token}{source.suffix or ".mkv"}'
    pcm = cache / f'dts-core-{token}.wav'
    generated = reference_proxy.with_suffix('.npz')
    pcm_generated = pcm.with_suffix('.npz')
    marker_temp = cache / f'speech-{token}.json.tmp'
    started = time.monotonic()
    log('正在读取整条对白音轨建立连续VAD指纹；本片后续候选共用。')
    actual_mode = decode_mode
    try:
        reference = reference_proxy
        ref_index = audio_index
        if core_only:
            log('DTS取证使用 core_only；仅生成临时单声道音频，不改变成品音轨。')
            try:
                _decode_dts_core_for_vad(source, audio_index, pcm, log, cancel)
            except legacy.CancelledError:
                raise
            except RuntimeError as exc:
                legacy.check_cancel(cancel)
                if not _is_dts_core_decode_failure(exc):
                    raise
                log(f'DTS core无法解码，改用原完整解码；共用剩余准备时间，不重新计时。原因：{exc}')
                actual_mode = 'full-fallback'
            else:
                actual_mode = 'dts-core'
                reference, ref_index, generated = pcm, 0, pcm_generated
        elif decode_mode == 'truehd-stereo':
            log('TrueHD取证使用双声道解码；仅生成临时音频，不改变成品音轨。')
            _run([
                legacy.FFMPEG, '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
                '-xerror', *vad_decoder_options(selected.codec), '-i', str(source),
                '-map', f'0:a:{audio_index}', '-vn', '-sn', '-dn', '-ac', '1',
                '-acodec', 'pcm_s16le', '-af', 'aresample=async=1', '-ar', '48000',
                '-f', 'wav', str(pcm),
            ], log, cancel)
            reference, ref_index, generated = pcm, 0, pcm_generated
        legacy.check_cancel(cancel)
        if reference == reference_proxy:
            os.link(source, reference_proxy)
        _run([engine, str(reference), '--serialize-speech', '--frame-rate', '48000',
              '--vad', 'webrtc', '--reference-stream', f'a:{ref_index}'], log, cancel)
        legacy.check_cancel(cancel)
        if not generated.is_file() or generated.stat().st_size <= 128:
            raise RuntimeError('公共语音指纹为空')
        latest = source.stat()
        if (latest.st_size, latest.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise SubtitleVerificationToolError('影片文件在取证期间发生变化，本次指纹不予缓存。')
        saved = dict(signature=signature, actual_decode_mode=actual_mode,
                     fingerprint_sha256=hashlib.sha256(generated.read_bytes()).hexdigest())
        marker_temp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf8')
        os.replace(generated, fingerprint)
        os.replace(marker_temp, marker)
    finally:
        for temporary in {generated, pcm_generated, reference_proxy, pcm, marker_temp}:
            _cleanup_alignment_temporary(temporary, log)
        log(f'连续VAD指纹准备耗时 {time.monotonic() - started:.2f} 秒（含解码、退出与清理）。')
    log(f'影片公共语音指纹准备完成（解码方式：{actual_mode}），后续候选共同复用。')
    return fingerprint


def align_external_subtitle(
    input_path: str,
    subtitle_path: str,
    work_dir: str,
    selected_audio_id: int | None,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
) -> Path:
    return preflight_external_subtitle(input_path, subtitle_path, work_dir,
        selected_audio_id, log, cancel)[0]


def _external_timeline_for_processing(
    input_path: str,
    subtitle_path: str | None,
    work_dir: str,
    selected_audio_id: int | None,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
) -> Path:
    # An artifact name is not evidence of a current-rule verdict. Explicit
    # caller confirmation is handled by process_pro before this helper.
    return align_external_subtitle(
        input_path,
        subtitle_path or "",
        work_dir,
        selected_audio_id,
        log,
        cancel,
    )


def _subtitle_time_seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d+):(\d+)[,.](\d+)", value.strip())
    if not match:
        return 0.0
    hours, minutes, seconds, milliseconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds[:3].ljust(3, "0")) / 1000


@dataclass(frozen=True)
class SubtitleCompleteness:
    event_count: int
    coverage: float
    occupied_bins: int
    events_per_minute: float
    active_ratio: float
    score: float
    accepted: bool

    @property
    def report(self) -> str:
        return (
            f"{self.event_count} 条，覆盖 {self.coverage:.0%}，"
            f"分布 {self.occupied_bins}/10，密度 {self.events_per_minute:.1f} 条/分钟"
        )


def subtitle_completeness(
    events: list[legacy.SubtitleEvent],
    duration_seconds: float,
) -> SubtitleCompleteness:
    if not events:
        return SubtitleCompleteness(0, 0.0, 0, 0.0, 0.0, 0.0, False)

    starts = [_subtitle_time_seconds(event.start) for event in events]
    ends = [_subtitle_time_seconds(event.end) for event in events]
    effective_duration = duration_seconds if duration_seconds > 0 else max(ends, default=0.0)
    if effective_duration <= 0:
        return SubtitleCompleteness(len(events), 0.0, 0, 0.0, 0.0, 0.0, False)

    coverage = min(1.5, max(ends, default=0.0) / effective_duration)
    occupied = {
        min(9, max(0, int(start / effective_duration * 10)))
        for start in starts
        if start <= effective_duration * 1.12
    }
    active_seconds = sum(
        max(0.0, min(12.0, end - start))
        for start, end in zip(starts, ends)
    )
    minutes = max(1.0, effective_duration / 60.0)
    events_per_minute = len(events) / minutes
    active_ratio = min(1.0, active_seconds / effective_duration)
    minimum_events = max(100, int(minutes * 0.8))
    accepted = (
        len(events) >= minimum_events
        and coverage >= 0.65
        and len(occupied) >= 6
        and events_per_minute >= 0.9
        and active_ratio >= 0.04
    )
    score = (
        min(1.0, len(events) / max(1, minimum_events * 2)) * 30
        + min(1.0, coverage) * 25
        + min(1.0, len(occupied) / 8) * 20
        + min(1.0, events_per_minute / 4) * 15
        + min(1.0, active_ratio / 0.18) * 10
    )
    return SubtitleCompleteness(
        len(events),
        coverage,
        len(occupied),
        events_per_minute,
        active_ratio,
        score,
        accepted,
    )


def choose_complete_subtitle_source(
    tracks: list[legacy.Track],
    normalized: dict[int, Path],
    requested_track_id: int | None,
    duration_seconds: float,
    log: Callable[[str], None],
) -> int | None:
    if requested_track_id is None:
        return None
    requested = next((track for track in tracks if track.id == requested_track_id), None)
    if requested is None:
        return requested_track_id
    requested_language = normalize_language_code(requested.language)
    candidates = [
        track for track in tracks
        if normalize_language_code(track.language) == requested_language and track.id in normalized
    ]
    profiles: dict[int, SubtitleCompleteness] = {}
    for track in candidates:
        profile = subtitle_completeness(legacy.parse_subtitle(normalized[track.id]), duration_seconds)
        profiles[track.id] = profile
        log(f"字幕轨 {track.id} 完整度检查：{profile.report}。")

    requested_profile = profiles.get(requested_track_id)
    if requested_profile is not None and requested_profile.accepted:
        return requested_track_id

    accepted = [track for track in candidates if profiles[track.id].accepted]
    if not accepted:
        detail = "；".join(f"轨道 {track.id}：{profiles[track.id].report}" for track in candidates)
        raise RuntimeError(
            f"没有找到完整的{requested_language if requested_language != 'und' else '来源'}字幕轨，"
            f"已阻止使用片段字幕生成不完整翻译。{detail}"
        )
    selected = max(
        accepted,
        key=lambda track: (
            profiles[track.id].score,
            1 if track.default else 0,
            -track.id,
        ),
    )
    log(
        f"原选择字幕轨 {requested_track_id} 疑似片段/强制字幕，"
        f"已自动改用完整字幕轨 {selected.id}。"
    )
    return selected.id


def _clean_dialogue_text(value: str) -> str:
    text = re.sub(r"\{\\[^}]*\}", " ", value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[[^\]]{0,80}\]|\([^)]{0,80}\)", " ", text)
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", " ", text)
    return " ".join(text.split())


@dataclass(frozen=True)
class AffineTimelineMatch:
    offset_seconds: float
    scale: float
    match_count: int
    coverage: float
    median_residual: float
    p90_residual: float

    @property
    def drift_per_hour(self) -> float:
        return (self.scale - 1.0) * 3600.0


COMMON_TIMELINE_SCALES = (
    1.0,
    24.0 / 23.976,
    23.976 / 24.0,
    25.0 / 24.0,
    24.0 / 25.0,
    25.0 / 23.976,
    23.976 / 25.0,
)


def _estimate_affine_timeline(
    candidate: Path,
    reference: Path,
    duration: float,
) -> AffineTimelineMatch | None:
    """Fit reference_time = offset + scale * candidate_time from unique dialogue cues."""
    candidate_events = legacy.parse_subtitle(candidate)
    reference_events = legacy.parse_subtitle(reference)
    candidate_index: dict[str, list[float]] = {}
    reference_index: dict[str, list[float]] = {}
    for event in candidate_events:
        key = _clean_dialogue_text(event.text)
        if len(key) >= 12:
            candidate_index.setdefault(key, []).append(_subtitle_time_seconds(event.start))
    for event in reference_events:
        key = _clean_dialogue_text(event.text)
        if len(key) >= 12:
            reference_index.setdefault(key, []).append(_subtitle_time_seconds(event.start))

    pairs = sorted(
        (candidate_times[0], reference_index[key][0])
        for key, candidate_times in candidate_index.items()
        if len(candidate_times) == 1 and len(reference_index.get(key, ())) == 1
    )
    if len(pairs) < 20:
        return None
    span = pairs[-1][0] - pairs[0][0]
    if duration > 0 and span / duration < 0.55:
        return None
    regions = {
        min(3, max(0, int(candidate_time / max(duration, 1.0) * 4)))
        for candidate_time, _reference_time in pairs
    }
    if duration > 0 and len(regions) < 3:
        return None

    slopes = [
        (reference_b - reference_a) / (candidate_b - candidate_a)
        for index, (candidate_a, reference_a) in enumerate(pairs)
        for candidate_b, reference_b in pairs[index + 1:]
        if candidate_b - candidate_a >= 60.0
    ]
    if not slopes:
        return None
    robust_scale = statistics.median(slopes)
    robust_offset = statistics.median(
        reference_time - robust_scale * candidate_time
        for candidate_time, reference_time in pairs
    )
    inliers = [
        (candidate_time, reference_time)
        for candidate_time, reference_time in pairs
        if abs(reference_time - (robust_offset + robust_scale * candidate_time)) <= 1.5
    ]
    if len(inliers) < 20 or len(inliers) < math.ceil(len(pairs) * 0.55):
        return None

    candidate_mean = statistics.fmean(item[0] for item in inliers)
    reference_mean = statistics.fmean(item[1] for item in inliers)
    denominator = sum((candidate_time - candidate_mean) ** 2 for candidate_time, _ in inliers)
    if denominator <= 0:
        return None
    scale = sum(
        (candidate_time - candidate_mean) * (reference_time - reference_mean)
        for candidate_time, reference_time in inliers
    ) / denominator
    offset = reference_mean - scale * candidate_mean
    residuals = sorted(
        abs(reference_time - (offset + scale * candidate_time))
        for candidate_time, reference_time in inliers
    )
    p90 = residuals[min(len(residuals) - 1, math.ceil(len(residuals) * 0.9) - 1)]
    scale_is_known = min(abs(scale - known) for known in COMMON_TIMELINE_SCALES) <= 0.002
    if not scale_is_known or abs(offset) > MAX_AUTOMATIC_OFFSET_SECONDS:
        return None
    if statistics.median(residuals) > 0.35 or p90 > 0.8:
        return None
    return AffineTimelineMatch(
        offset_seconds=offset,
        scale=scale,
        match_count=len(inliers),
        coverage=span / duration if duration > 0 else 1.0,
        median_residual=statistics.median(residuals),
        p90_residual=p90,
    )


def _dialogue_text_variants(value: str) -> set[str]:
    """Return deterministic same-script dialogue variants for local text matching."""
    raw = re.sub(r"\\N|\\n", "\n", value or "")
    variants: set[str] = set()
    for part in (raw, *raw.splitlines()):
        cleaned = _clean_dialogue_text(part)
        if len(cleaned) >= 12:
            variants.add(cleaned)
        latin_runs = {
            " ".join(run.split())
            for run in re.findall(r"[a-z0-9']+(?:\s+[a-z0-9']+)*", cleaned)
        }
        variants.update(
            run for run in latin_runs
            if len(run) >= 12 and len(run.split()) >= 3
        )
        cjk = "".join(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", cleaned))
        if len(cjk) >= 6:
            variants.add(cjk)
    return variants


def _estimate_fixed_text_timeline(
    candidate: Path,
    reference: Path,
    duration: float,
) -> AffineTimelineMatch | None:
    """Fit a fixed offset from unique exact dialogue shared by two subtitle files."""
    candidate_index: dict[str, list[float]] = {}
    reference_index: dict[str, list[float]] = {}
    for event in legacy.parse_subtitle(candidate):
        event_time = _subtitle_time_seconds(event.start)
        for key in _dialogue_text_variants(event.text):
            candidate_index.setdefault(key, []).append(event_time)
    for event in legacy.parse_subtitle(reference):
        event_time = _subtitle_time_seconds(event.start)
        for key in _dialogue_text_variants(event.text):
            reference_index.setdefault(key, []).append(event_time)

    pairs = sorted({
        (candidate_times[0], reference_index[key][0])
        for key, candidate_times in candidate_index.items()
        if len(candidate_times) == 1 and len(reference_index.get(key, ())) == 1
    })
    if len(pairs) < 20:
        return None
    span = pairs[-1][0] - pairs[0][0]
    if duration > 0 and span / duration < 0.55:
        return None
    regions = {
        min(3, max(0, int(candidate_time / max(duration, 1.0) * 4)))
        for candidate_time, _reference_time in pairs
    }
    if duration > 0 and len(regions) < 3:
        return None

    offset = statistics.median(
        reference_time - candidate_time
        for candidate_time, reference_time in pairs
    )
    inliers = [
        (candidate_time, reference_time)
        for candidate_time, reference_time in pairs
        if abs((reference_time - candidate_time) - offset) <= 1.5
    ]
    if len(inliers) < 20 or len(inliers) < math.ceil(len(pairs) * 0.55):
        return None
    offset = statistics.median(
        reference_time - candidate_time
        for candidate_time, reference_time in inliers
    )
    residuals = sorted(
        abs((reference_time - candidate_time) - offset)
        for candidate_time, reference_time in inliers
    )
    p90 = residuals[min(len(residuals) - 1, math.ceil(len(residuals) * 0.9) - 1)]
    median_residual = statistics.median(residuals)
    if abs(offset) > MAX_AUTOMATIC_OFFSET_SECONDS or median_residual > 0.35 or p90 > 0.8:
        return None
    return AffineTimelineMatch(
        offset_seconds=offset,
        scale=1.0,
        match_count=len(inliers),
        coverage=span / duration if duration > 0 else 1.0,
        median_residual=median_residual,
        p90_residual=p90,
    )


def _estimate_fixed_structure_timeline(
    candidate: Path,
    reference: Path,
    duration: float,
) -> dict[str, object] | None:
    """Compare two complete subtitle activity timelines without reading their language."""
    if duration <= 0:
        return None
    candidate_events = legacy.parse_subtitle(candidate)
    reference_events = legacy.parse_subtitle(reference)
    candidate_intervals = [
        (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
        for event in candidate_events
    ]
    reference_intervals = [
        (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
        for event in reference_events
    ]
    if len(candidate_intervals) < 20 or len(reference_intervals) < 20:
        return None
    whole_score, whole_offset = _interval_alignment(reference_intervals, candidate_intervals)
    regions: list[dict[str, float | int | str]] = []
    for label, left, right in (
        ("early", 0.10, 0.35),
        ("middle", 0.40, 0.65),
        ("late", 0.70, 0.95),
    ):
        region_start = duration * left
        region_end = duration * right
        reference_count = sum(
            1 for start, end in reference_intervals
            if end > region_start and start < region_end
        )
        candidate_count = sum(
            1 for start, end in candidate_intervals
            if end + whole_offset > region_start and start + whole_offset < region_end
        )
        best_offset = whole_offset
        best_score = -1.0
        center = round(whole_offset * 10)
        for step in range(center - 20, center + 21):
            region_offset = step / 10.0
            score = _shifted_activity_overlap(
                reference_intervals,
                candidate_intervals,
                region_offset,
                region_start,
                region_end,
            )
            if score > best_score:
                best_score = score
                best_offset = region_offset
        regions.append({
            "name": label,
            "offset_seconds": best_offset,
            "score": best_score,
            "reference_count": reference_count,
            "candidate_count": candidate_count,
        })
    offsets = [float(region["offset_seconds"]) for region in regions]
    scores = [float(region["score"]) for region in regions]
    offset_span = max(offsets) - min(offsets)
    strong = (
        abs(whole_offset) <= MAX_AUTOMATIC_OFFSET_SECONDS
        and whole_score >= 0.85
        and min(scores) >= 0.80
        and offset_span <= 0.50
        and all(
            int(region["reference_count"]) >= 10
            and int(region["candidate_count"]) >= 10
            for region in regions
        )
    )
    return {
        "strong": strong,
        "score": whole_score,
        "offset_seconds": whole_offset,
        "region_offset_span": offset_span,
        "regions": regions,
    }


def _apply_affine_timeline(
    source: Path,
    destination: Path,
    match: AffineTimelineMatch,
) -> Path:
    source_events = legacy.parse_subtitle(source)
    corrected: list[legacy.SubtitleEvent] = []
    for event in source_events:
        start = max(0, int(round(
            (match.offset_seconds + match.scale * _subtitle_time_seconds(event.start)) * 1000
        )))
        end = max(start + 1, int(round(
            (match.offset_seconds + match.scale * _subtitle_time_seconds(event.end)) * 1000
        )))
        corrected.append(legacy.SubtitleEvent(
            legacy.srt_time_from_milliseconds(start),
            legacy.srt_time_from_milliseconds(end),
            event.text,
        ))
    legacy.write_srt(
        destination,
        corrected,
        {index: event.text for index, event in enumerate(corrected, 1)},
    )
    return destination


def _tag_duration_seconds(track: dict) -> float | None:
    value = str(track.get("properties", {}).get("tag_duration", "") or "")
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _log_media_timeline_health(media: dict, log: Callable[[str], None]) -> None:
    tracks = media.get("tracks", [])
    if not tracks:
        log("影片时间轴快速体检：容器未提供轨道级时间信息，继续使用音频核验。")
        return
    video = next((track for track in tracks if track.get("type") == "video"), None)
    audio = next((track for track in tracks if track.get("type") == "audio"), None)
    if not video or not audio:
        raise RuntimeError("影片缺少可用的视频轨或音轨，时间轴体检未通过。")
    video_start = float(video.get("properties", {}).get("minimum_timestamp", 0) or 0) / 1_000_000_000
    audio_start = float(audio.get("properties", {}).get("minimum_timestamp", 0) or 0) / 1_000_000_000
    video_duration = _tag_duration_seconds(video)
    audio_duration = _tag_duration_seconds(audio)
    start_gap = abs(video_start - audio_start)
    duration_gap = abs(video_duration - audio_duration) if video_duration is not None and audio_duration is not None else None
    if start_gap > 2.0:
        raise RuntimeError(f"影片音视频起点相差 {start_gap:.2f} 秒，时间轴异常，已停止字幕处理。")
    if duration_gap is not None and duration_gap > max(8.0, (video_duration or 0.0) * 0.005):
        raise RuntimeError(f"影片音视频时长相差 {duration_gap:.2f} 秒，疑似轨道异常，已停止字幕处理。")
    gap_text = f"，时长差 {duration_gap:.2f} 秒" if duration_gap is not None else ""
    log(f"影片时间轴快速体检通过：音视频起点差 {start_gap:.2f} 秒{gap_text}。")


def semantic_text_score(subtitle_text: str, transcript_text: str) -> float:
    """Return a tolerant 0..1 dialogue similarity score."""
    expected = _clean_dialogue_text(subtitle_text)
    observed = _clean_dialogue_text(transcript_text)
    if not expected or not observed:
        return 0.0

    sequence = difflib.SequenceMatcher(None, expected, observed).ratio()
    contains_cjk = bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", expected + observed))
    if contains_cjk:
        expected_units = {expected[index:index + 2] for index in range(max(0, len(expected) - 1))}
        observed_units = {observed[index:index + 2] for index in range(max(0, len(observed) - 1))}
    else:
        expected_units = set(re.findall(r"[a-z0-9']{2,}", expected))
        observed_units = set(re.findall(r"[a-z0-9']{2,}", observed))

    if expected_units and observed_units:
        overlap = len(expected_units & observed_units)
        precision = overlap / len(observed_units)
        recall = overlap / len(expected_units)
        token_score = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    else:
        token_score = 0.0
    if contains_cjk:
        return max(sequence, token_score)
    return max(sequence * 0.6, token_score)


def _prepare_aligned_embedded_text_reference(
    input_path: str,
    source_language: str,
    selected_audio_id: int | None,
    cache_dir: Path,
    log: Callable[[str], None],
    cancel: threading.Event | None,
) -> tuple[Path, str] | None:
    reference = _embedded_subtitle_reference(input_path, source_language)
    if reference is None:
        return None
    _mode, _stream_index, track = reference
    requested_language = normalize_language_code(source_language)
    if not track.text_subtitle:
        return None
    if requested_language != "und" and normalize_language_code(track.language) != requested_language:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    aligned = cache_dir / f"track-{track.id}-audio-aligned.srt"
    metadata_path = cache_dir / f"track-{track.id}-audio-aligned.json"
    video = Path(input_path)
    import continuous_vad

    signature = {
        "video_size": video.stat().st_size,
        "video_mtime_ns": video.stat().st_mtime_ns,
        "track_id": track.id,
        "audio_id": selected_audio_id,
        "language": requested_language,
        "alignment_mode": "audio-full-v2-verified-only",
        "alignment_rule_version": continuous_vad.RULE_VERSION,
    }
    try:
        cached = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    if cached == signature and aligned.is_file() and aligned.stat().st_size > 32:
        log(f"复用影片时间轴体检缓存：原字幕轨 {track.id} 已对齐音频。")
        return aligned, "复用已验证的影片音频时间轴"

    extracted = legacy.extract_subtitle(input_path, track, cache_dir, log, cancel)
    normalized = subtitle_events_to_srt(extracted, cache_dir / f"track-{track.id}-source.srt")
    corrected, report = align_embedded_text_track(
        input_path,
        normalized,
        cache_dir,
        selected_audio_id,
        source_language,
        track.id,
        log,
        cancel,
    )
    if not _timeline_report_verified(report):
        log(
            f"原字幕轨 {track.id} 没有取得足够音频证据，不能作为下载字幕的可靠时间标杆；"
            f"{report}"
        )
        return None
    if corrected != aligned:
        shutil.copy2(corrected, aligned)
    metadata_path.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding="utf-8")
    return aligned, report


def _prepare_manual_aligned_text_reference(
    input_path: str,
    source_language: str,
    selected_audio_id: int | None,
    cache_dir: Path,
    log: Callable[[str], None],
    cancel: threading.Event | None,
) -> tuple[Path, str] | None:
    """Audio-check one embedded same-language text track with segmented fixed sync."""
    reference = _embedded_subtitle_reference(input_path, source_language)
    if reference is None:
        return None
    _mode, _stream_index, track = reference
    requested_language = normalize_language_code(source_language)
    if not track.text_subtitle:
        return None
    if requested_language != "und" and normalize_language_code(track.language) != requested_language:
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    aligned = cache_dir / f"track-{track.id}-manual-audio-aligned.srt"
    metadata_path = cache_dir / f"track-{track.id}-manual-audio-aligned.json"
    video = Path(input_path)
    import continuous_vad

    signature = {
        "video_size": video.stat().st_size,
        "video_mtime_ns": video.stat().st_mtime_ns,
        "track_id": track.id,
        "audio_id": selected_audio_id,
        "language": requested_language,
        "alignment_mode": "manual-audio-segmented-v1",
        "alignment_rule_version": continuous_vad.RULE_VERSION,
    }
    try:
        cached = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    if cached == signature and aligned.is_file() and aligned.stat().st_size > 32:
        log(f"复用手动字幕时间参照缓存：原字幕轨 {track.id} 已通过音频核验。")
        return aligned, "复用已通过音频核验的影片原字幕时间轴"

    extracted = legacy.extract_subtitle(input_path, track, cache_dir, log, cancel)
    normalized = subtitle_events_to_srt(
        extracted,
        cache_dir / f"track-{track.id}-manual-source.srt",
    )
    diagnostics: dict[str, float] = {}
    candidate = _alignment_candidate(
        input_path,
        normalized,
        cache_dir / f"track-{track.id}-manual-audio-candidate.srt",
        selected_audio_id,
        "audio-segmented",
        log,
        cancel,
        diagnostics=diagnostics,
    )
    offset = diagnostics.get("offset_seconds", 0.0)
    if abs(offset) > MAX_AUTOMATIC_OFFSET_SECONDS:
        raise RuntimeError(
            f"原字幕轨 {track.id} 的音频固定偏移 {offset:+.2f} 秒超出手动参照安全范围。"
        )
    verification = semantic_spot_check(
        input_path,
        candidate,
        str(cache_dir / f"track-{track.id}-manual-semantic-check"),
        selected_audio_id,
        requested_language,
        0.0,
        log,
        cancel,
        candidate_label=f"原字幕轨 {track.id} 分段音频校正结果",
        raise_on_failure=False,
        allow_fast_accept=True,
    )
    if not verification.accepted:
        raise RuntimeError(
            f"原字幕轨 {track.id} 的音频校正未通过独立台词抽检，不能作为手动字幕时间参照。"
        )
    if candidate != aligned:
        shutil.copy2(candidate, aligned)
    metadata_path.write_text(json.dumps(signature, ensure_ascii=False, indent=2), encoding="utf-8")
    report = (
        f"原字幕轨 {track.id} 已通过分段音频固定偏移 {offset:+.2f} 秒校准；"
        f"{verification.report}"
    )
    return aligned, report


def _manual_reference_first_candidate(
    input_path: str,
    normalized: Path,
    destination: Path,
    selected_audio_id: int | None,
    candidate_language: str,
    duration: float,
    cache_dir: Path,
    log: Callable[[str], None],
    cancel: threading.Event | None,
) -> tuple[Path, str] | None:
    """Resolve a manual subtitle from an audio-verified embedded text timeline."""
    audio_language = _selected_audio_language(input_path, selected_audio_id)
    reference = _embedded_subtitle_reference(input_path, audio_language)
    if reference is None or not reference[2].text_subtitle:
        return None
    _mode, _stream_index, track = reference
    if audio_language != "und" and normalize_language_code(track.language) != audio_language:
        return None

    aligned_reference = _prepare_manual_aligned_text_reference(
        input_path,
        audio_language,
        selected_audio_id,
        cache_dir,
        log,
        cancel,
    )
    if aligned_reference is None:
        return None
    reference_path, reference_report = aligned_reference
    reference_language = normalize_language_code(track.language)
    language_text = candidate_language or "und"
    log(
        f"手动字幕优先使用已校准的影片原字幕轨 {track.id} "
        f"（{reference_language or 'und'}）作时间参照；候选实际语言 {language_text}。"
    )

    text_match = _estimate_fixed_text_timeline(normalized, reference_path, duration)
    if text_match is not None:
        corrected = _shifted_srt(
            normalized,
            destination,
            int(round(text_match.offset_seconds * 1000)),
        )
        label = (
            f"已校准原字幕逐句文字参照固定偏移 {text_match.offset_seconds:+.2f} 秒"
            f"（{text_match.match_count} 条全片匹配，P90 残差 {text_match.p90_residual:.2f} 秒）"
        )
        log(f"{label}；{reference_report}。")
        return corrected, label

    structure = _estimate_fixed_structure_timeline(normalized, reference_path, duration)
    if structure is not None and bool(structure["strong"]):
        offset = float(structure["offset_seconds"])
        corrected = _shifted_srt(
            normalized,
            destination,
            int(round(offset * 1000)),
        )
        label = (
            f"已校准原字幕全片时间结构参照固定偏移 {offset:+.2f} 秒"
            f"（相关度 {float(structure['score']):.0%}，"
            f"前中后偏移跨度 {float(structure['region_offset_span']):.2f} 秒）"
        )
        log(f"{label}；{reference_report}。")
        return corrected, label

    log(
        "手动字幕与已校准原字幕未形成可靠的逐句文字或前中后固定偏移共识；"
        "不会把原字幕未经证明地当作时间真值。"
    )
    return None


def _semantic_samples(
    events: list[legacy.SubtitleEvent],
    duration: float,
) -> list[tuple[float, float, str]]:
    timed = [
        (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end), event.text)
        for event in events
        if _clean_dialogue_text(event.text)
    ]
    if not timed:
        return []

    timeline_end = duration or max(item[1] for item in timed)
    samples: list[tuple[float, float, str]] = []
    region_count = 4
    region_width = timeline_end / region_count if timeline_end else 0.0
    for region_index in range(region_count):
        region_start = region_width * region_index
        region_end = region_width * (region_index + 1)
        region_events = [
            item for item in timed
            if region_start <= (item[0] + item[1]) / 2 < region_end
        ]
        if not region_events:
            target = timeline_end * ((region_index + 0.5) / region_count)
            region_events = [min(timed, key=lambda item: abs((item[0] + item[1]) / 2 - target))]

        candidates: list[tuple[int, float, float, str]] = []
        for center_start, center_end, _center_text in region_events:
            start = max(0.0, center_start - 6.0)
            end = (
                min(timeline_end, start + SEMANTIC_SAMPLE_SECONDS)
                if timeline_end
                else start + SEMANTIC_SAMPLE_SECONDS
            )
            group = [item for item in timed if item[1] >= start and item[0] <= end]
            text = " ".join(item[2] for item in group)
            density = len(_clean_dialogue_text(text))
            candidates.append((density, start, end, text))
        _density, start, end, text = max(candidates, key=lambda item: item[0])
        if timeline_end > 30.0:
            start = min(start, timeline_end - SEMANTIC_SAMPLE_SECONDS)
        if samples and start - samples[-1][0] < 30.0:
            continue
        samples.append((start, max(8.0, min(SEMANTIC_SAMPLE_SECONDS, end - start)), text))
    return samples


@dataclass(frozen=True)
class SemanticCheckResult:
    label: str
    scores: tuple[float, ...]
    sample_count: int
    fast_path: bool = False

    @property
    def valid_count(self) -> int:
        return len(self.scores)

    @property
    def passed_count(self) -> int:
        return sum(score >= 0.50 for score in self.scores)

    @property
    def average(self) -> float:
        return sum(self.scores) / len(self.scores) if self.scores else 0.0

    @property
    def accepted(self) -> bool:
        strict_fast_accept = (
            self.fast_path
            and
            self.valid_count >= 2
            and all(score >= 0.60 for score in self.scores[:2])
            and sum(self.scores[:2]) / 2 >= 0.65
        )
        if strict_fast_accept:
            return True
        minimum_valid = max(3, math.ceil(self.sample_count * 0.5))
        minimum_passed = max(2, math.ceil(self.valid_count * 0.75))
        return (
            self.valid_count >= minimum_valid
            and self.passed_count >= minimum_passed
            and self.average >= 0.55
        )

    @property
    def report(self) -> str:
        return (
            f"{self.label}台词抽检：{self.passed_count}/{self.valid_count} 处通过，"
            f"平均匹配 {self.average:.0%}"
        )


def _semantic_acceptance_still_possible(
    scores: list[float],
    remaining_samples: int,
    sample_count: int,
) -> bool:
    minimum_valid = max(3, math.ceil(sample_count * 0.5))
    passed = sum(score >= 0.50 for score in scores)
    score_sum = sum(scores)
    for future_valid in range(remaining_samples + 1):
        final_valid = len(scores) + future_valid
        if final_valid < minimum_valid:
            continue
        maximum_passed = passed + future_valid
        minimum_passed = max(2, math.ceil(final_valid * 0.75))
        maximum_average = (score_sum + future_valid) / final_valid
        if maximum_passed >= minimum_passed and maximum_average >= 0.55:
            return True
    return False

def _selected_audio_language(input_path: str, selected_audio_id: int | None) -> str:
    audio = [track for track in legacy.inspect_tracks(input_path) if track.type == "audio"]
    selected = next((track for track in audio if track.id == selected_audio_id), None)
    selected = selected or (audio[0] if audio else None)
    return normalize_language_code(selected.language if selected else "")


def _preferred_validation_audio_id(
    input_path: str,
    selected_audio_id: int | None,
    source_language: str,
) -> int | None:
    import continuous_vad
    return continuous_vad.audio_track(legacy.inspect_tracks(input_path), selected_audio_id).id



def _whisper_language(code: str) -> str:
    normalized = normalize_language_code(code)
    return {"zh-CN": "zh", "zh-TW": "zh"}.get(normalized, normalized if normalized != "und" else "auto")


def _semantic_runtime_dir() -> Path:
    bases = [
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("PUBLIC", r"C:\Users\Public"),
    ]
    for base in bases:
        if not base:
            continue
        root = Path(base).resolve() / "SubFlow"
        if not str(root).isascii():
            continue
        try:
            work = root / "semantic-check" / uuid.uuid4().hex
            work.mkdir(parents=True, exist_ok=False)
            return work
        except OSError:
            continue
    raise RuntimeError("无法创建语音内容抽检的临时目录。")


def _audio_drift_guard_enabled(input_path: str) -> bool:
    configured = os.environ.get("SUBFLOW_AUDIO_DRIFT_GUARD", "1").strip()
    if configured == "0":
        return False
    try:
        source = Path(input_path)
        return source.is_file() and source.stat().st_size >= 1024 * 1024
    except OSError:
        return False


@dataclass(frozen=True)
class SharedSubtitleContentAudio:
    duration_seconds: float
    validation_audio_id: int | None
    audio_stream_index: int
    audio_language: str
    fingerprint: audio_offset_verifier.MovieAudioFingerprint | None
    evidence_failures: dict[str, str] = field(default_factory=dict, compare=False, repr=False)
    vad_reference: Path | None = None
    vad_preparation_state: dict = field(default_factory=dict, compare=False, repr=False)


def _legacy_whisper_prepare_shared_subtitle_content_audio(
    input_path: str,
    selected_audio_id: int | None,
    work_dir: str | Path,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    source_language: str = "en",
    time_budget_seconds: float = 60.0,
) -> SharedSubtitleContentAudio:
    """Build candidate-independent Whisper evidence while providers are searched."""
    started = time.monotonic()
    deadline = started + max(5.0, time_budget_seconds)
    preparation_cancel = _DeadlineCancel(cancel, deadline)
    media = legacy.inspect_media(input_path)
    duration_ns = legacy.video_track_duration_ns(media) or legacy.media_duration_ns(media)
    duration = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0
    validation_audio_id = _preferred_validation_audio_id(
        input_path,
        selected_audio_id,
        source_language,
    )
    audio_stream_index = _audio_stream_index(input_path, validation_audio_id) or 0
    audio_language = _selected_audio_language(input_path, validation_audio_id)
    whisper_model = _tool(WHISPER_MODEL_CANDIDATES, "Whisper 识别模型")
    evidence_failures: dict[str, str] = {}
    cache_dir = audio_offset_verifier.persistent_cache_dir(
        input_path,
        audio_stream_index=audio_stream_index,
        whisper_model=whisper_model,
    )
    log(
        "智能字幕：开始准备候选共用的影片台词；"
        "分散音频一次批量交给 Whisper，搜索和下载同时继续。"
    )

    def public_log(message: str) -> None:
        if "计时：" in message:
            log(message.replace("音频指纹", "公共台词"))
        elif "复用持久化" in message:
            log("智能字幕：复用已准备的影片公共台词，不重复运行 FFmpeg/Whisper。")

    fingerprint = audio_offset_verifier.build_movie_audio_fingerprint(
        input_path,
        str(Path(work_dir) / "movie-fingerprint"),
        duration_seconds=duration,
        audio_stream_index=audio_stream_index,
        ffmpeg=legacy.FFMPEG,
        whisper=_tool(WHISPER_CLI_CANDIDATES, "Whisper 识别引擎"),
        whisper_model=whisper_model,
        audio_language=_whisper_language(audio_language),
        translate_to_english=(audio_language != "en"),
        log=public_log,
        probe_fractions=SHARED_SUBTITLE_PROBE_FRACTIONS,
        fingerprint_clips=3,
        cache_dir=str(cache_dir),
        cancel=preparation_cancel,
        deadline=deadline,
        task_failures=evidence_failures,
    )
    if fingerprint.usable_clip_count < 2:
        log(
            "智能字幕：三个公共位置没有取得足够对白证据；"
            "保留影片音轨信息，候选下载后改用候选自身的分散对白定向核验。"
        )
    else:
        log(
            f"智能字幕：三个公共位置准备完成，其中 "
            f"{fingerprint.usable_clip_count} 个位置取得可用对白，"
            f"耗时 {time.monotonic() - started:.2f} 秒。"
        )
    return SharedSubtitleContentAudio(
        duration,
        validation_audio_id,
        audio_stream_index,
        audio_language,
        fingerprint,
        evidence_failures=evidence_failures,
    )


def _audio_drift_rejection(
    result: audio_offset_verifier.AudioOffsetResult | None,
    duration_seconds: float = 0.0,
) -> str:
    if (
        result is None
        or not result.accepted
        or result.offset_seconds is None
        or not result.affine
    ):
        return ""
    strong_anchors = [
        anchor for anchor in result.anchors
        if float(getattr(anchor, "score", 0.0) or 0.0) >= 0.65
    ]
    residuals = [abs(float(value)) for value in result.residuals]
    anchor_times = [
        float(getattr(anchor, "movie_time", 0.0) or 0.0)
        for anchor in strong_anchors
    ]
    anchor_span = max(anchor_times) - min(anchor_times) if len(anchor_times) >= 2 else 0.0
    required_span = max(1200.0, duration_seconds * 0.35) if duration_seconds > 0 else 1200.0
    clearly_proven = (
        len(result.anchors) >= 4
        and len(strong_anchors) >= 4
        and statistics.fmean(
            float(getattr(anchor, "score", 0.0) or 0.0)
            for anchor in strong_anchors
        ) >= 0.75
        and anchor_span >= required_span
        and abs(result.drift_per_hour) >= 0.5
        and bool(residuals)
        and statistics.median(residuals) <= 0.25
        and max(residuals) <= 0.50
    )
    if not clearly_proven:
        return ""
    return (
        f"音频内容验证检测到字幕存在规律性速度差（{result.scale_label}，"
        f"起始偏移 {result.offset_seconds:+.2f} 秒，每小时漂移 "
        f"{result.drift_per_hour:+.2f} 秒）。当前产品只自动修正统一固定偏移，"
        "因此已拒绝这条字幕并尝试下一候选。"
    )


def semantic_spot_check(
    input_path: str,
    aligned_subtitle: str | Path,
    work_dir: str,
    selected_audio_id: int | None,
    source_language: str,
    duration: float,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    candidate_label: str = "",
    raise_on_failure: bool = True,
    allow_fast_accept: bool = False,
) -> SemanticCheckResult:
    events = legacy.parse_subtitle(Path(aligned_subtitle))
    subtitle_language = normalize_language_code(source_language)
    if subtitle_language == "und":
        subtitle_language = detect_subtitle_language(events)
    audio_language = _selected_audio_language(input_path, selected_audio_id)
    samples = _semantic_samples(events, duration)
    if len(samples) < 3:
        raise SubtitleContentMismatchError("字幕时间轴内没有足够的连续对白，无法完成内容抽检。")

    if subtitle_language != "en":
        log("字幕与语音将统一换算为英语后进行内容抽检。")
        legacy.begin_ollama_lease()
        try:
            legacy.ensure_ollama_running(log=log, cancel_event=cancel)
            translated = legacy.translate_batch(
                [item[2] for item in samples],
                "en",
                subtitle_language,
                legacy.DEFAULT_MODEL,
                legacy.OLLAMA_HOST,
                600,
                cancel_event=cancel,
            )
            samples = [
                (start, clip_duration, translated[index])
                for index, (start, clip_duration, _text) in enumerate(samples)
            ]
        finally:
            if legacy.end_ollama_lease():
                legacy.unload_ollama_model(log)

    _ = work_dir
    work = _semantic_runtime_dir()
    stream_index = _audio_stream_index(input_path, selected_audio_id)
    stream_spec = f"0:a:{stream_index}" if stream_index is not None else "0:a:0"
    model = Path(_tool(WHISPER_MODEL_CANDIDATES, "Whisper 识别模型"))
    whisper = _tool(WHISPER_CLI_CANDIDATES, "Whisper 识别引擎")
    whisper_language = _whisper_language(audio_language)
    translate_to_english = audio_language != "en"

    scores: list[float] = []
    fast_accepted = False
    label = candidate_label or "字幕"
    log(f"开始核验{label}：从四个时段优先选择对白密集位置。")
    try:
        for index, (start, clip_duration, expected_text) in enumerate(samples, 1):
            legacy.check_cancel(cancel)
            wav = work / f"sample-{index}.wav"
            output_base = work / f"sample-{index}-speech"
            transcript_path = output_base.with_suffix(".txt")
            transcript_path.unlink(missing_ok=True)
            _run([
                legacy.FFMPEG,
                "-y",
                "-ss", f"{start:.3f}",
                "-i", input_path,
                "-t", f"{clip_duration:.3f}",
                "-map", stream_spec,
                "-vn",
                "-ac", "1",
                "-ar", "16000",
                str(wav),
            ], lambda _message: None, cancel)
            args = [
                whisper,
                "-m", model.name,
                "-f", str(wav),
                "-otxt",
                "-of", str(output_base),
                "-l", whisper_language,
                "-t", "6",
                "-np",
            ]
            if translate_to_english:
                args.append("-tr")
            _run(args, lambda _message: None, cancel, cwd=str(model.parent))
            transcript = transcript_path.read_text(encoding="utf-8", errors="replace") if transcript_path.exists() else ""
            if len(_clean_dialogue_text(transcript)) < 10:
                log(f"{label}抽检 {index}/{len(samples)}：该处没有识别到足够对白，记为无效采样。")
                remaining = len(samples) - index
                if not _semantic_acceptance_still_possible(scores, remaining, len(samples)):
                    log(f"{label}即使剩余抽检全部通过也无法达到验收标准，提前结束。")
                    break
                continue
            score = semantic_text_score(expected_text, transcript)
            scores.append(score)
            log(f"{label}抽检 {index}/{len(samples)}：台词匹配 {score:.0%}。")
            if (
                allow_fast_accept
                and len(scores) == 2
                and all(value >= 0.60 for value in scores)
                and sum(scores) / 2 >= 0.65
            ):
                log(f"{label}前两处远距离抽检均为高匹配，严格快速核验通过，停止剩余抽检。")
                fast_accepted = True
                break
            remaining = len(samples) - index
            if not _semantic_acceptance_still_possible(scores, remaining, len(samples)):
                log(f"{label}即使剩余抽检全部通过也无法达到验收标准，提前结束。")
                break
    finally:
        shutil.rmtree(work, ignore_errors=True)

    result = SemanticCheckResult(label, tuple(scores), len(samples), fast_accepted)
    log(result.report)
    if raise_on_failure and not result.accepted:
        details = "、".join(f"{score:.0%}" for score in scores)
        raise SubtitleContentMismatchError(
            f"字幕台词与影片语音的内容匹配度不足（抽检：{details}）。"
            "该字幕可能属于其他影片或其他剪辑版本，请更换字幕。"
        )
    return result


class _DeadlineCancel:
    def __init__(self, cancel_event, deadline: float | None) -> None:
        self.cancel_event = cancel_event
        self.deadline = deadline

    def is_set(self) -> bool:
        return bool(
            (self.cancel_event is not None and self.cancel_event.is_set())
            or (self.deadline is not None and time.monotonic() >= self.deadline)
        )

    @property
    def timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline


def _preflight_external_subtitle_impl(
    input_path: str,
    subtitle_path: str,
    work_dir: str,
    selected_audio_id: int | None,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    source_language: str = "",
    use_embedded_reference: bool = True,
    candidate_label: str = "下载字幕",
    timeline_cache_dir: str | None = None,
    manual_reference_first: bool = False,
    audio_drift_guard_override: bool | None = None,
    shared_audio_reference: str | Path | None = None,
) -> tuple[Path, str]:
    """Reject incomplete or poorly aligned subtitles before translation starts."""
    source = Path(subtitle_path)
    events = legacy.parse_subtitle(source)
    if not events:
        raise RuntimeError("字幕文件没有可识别的正文和时间轴。")

    media = legacy.inspect_media(input_path)
    _log_media_timeline_health(media, log)
    duration_ns = legacy.video_track_duration_ns(media) or legacy.media_duration_ns(media)
    duration = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0
    subtitle_end = max((_subtitle_time_seconds(event.end) for event in events), default=0.0)
    coverage = subtitle_end / duration if duration > 0 else 0.0
    declared_language = normalize_language_code(source_language)
    detected_language = detect_subtitle_language(events)
    candidate_language = (
        detected_language
        if detected_language != "und"
        else declared_language
    )
    if manual_reference_first and candidate_language != "und":
        if declared_language != "und" and declared_language != candidate_language:
            log(
                f"字幕站标记语言为 {declared_language}，正文识别为 {candidate_language}；"
                "手动预检以实际正文语言为准。"
            )
        source_language = candidate_language
    validation_audio_id = _preferred_validation_audio_id(
        input_path,
        selected_audio_id,
        source_language,
    )
    if validation_audio_id != selected_audio_id:
        language = _selected_audio_language(input_path, validation_audio_id)
        log(f"字幕内容核验优先使用同语言音轨：轨道 {validation_audio_id} · {language}。")

    if duration >= 900 and len(events) < 20:
        raise RuntimeError(f"字幕仅有 {len(events)} 条，疑似片段或强制字幕，不适合作为整片翻译来源。")
    if duration > 0 and coverage < 0.55:
        raise RuntimeError(
            f"字幕只覆盖到影片约 {coverage:.0%} 的位置，疑似不完整字幕或错误影片。"
        )
    if manual_reference_first and duration > 0:
        profile = subtitle_completeness(events, duration)
        if not profile.accepted:
            raise RuntimeError(
                f"手动字幕完整度检查未通过：{profile.report}；"
                "疑似片段、强制字幕或只覆盖部分影片，未启动耗时核验。"
            )
        if subtitle_end > duration + max(180.0, duration * 0.03):
            raise RuntimeError(
                f"手动字幕时间轴比影片长 {subtitle_end - duration:.0f} 秒，"
                "疑似加长版、不同剪辑版本或错误影片，未启动耗时核验。"
            )
    if duration > 0 and subtitle_end > duration + max(180.0, duration * 0.12):
        raise RuntimeError("字幕时间轴明显长于影片，疑似来自其他剪辑版本或错误影片。")

    log(f"字幕结构检查通过：{len(events)} 条，时间轴覆盖约 {coverage:.0%}" if duration else f"字幕结构检查通过：{len(events)} 条")
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    normalized = subtitle_events_to_srt(source, work / "external-source.srt")
    attempted: list[SemanticCheckResult] = []
    checked_content: dict[bytes, SemanticCheckResult] = {}
    cache_root = Path(timeline_cache_dir) if timeline_cache_dir else work / "timeline-health"

    if manual_reference_first and use_embedded_reference:
        try:
            manual_candidate = _manual_reference_first_candidate(
                input_path,
                normalized,
                work / "external-manual-reference-fixed.srt",
                validation_audio_id,
                candidate_language,
                duration,
                cache_root,
                log,
                cancel,
            )
        except legacy.CancelledError:
            raise
        except Exception as exc:
            log(f"手动字幕快速参照不可用，继续检查是否可使用音频兜底：{exc}")
            manual_candidate = None
        if manual_candidate is not None:
            selected_path, report = manual_candidate
            verified = work / "external-verified.srt"
            shutil.copy2(selected_path, verified)
            summary = (
                f"字幕预检通过：{len(events)} 条，覆盖约 {coverage:.0%}；{report}"
            )
            log(summary)
            return verified, summary

    if manual_reference_first:
        audio_language = _selected_audio_language(input_path, validation_audio_id)
        cross_language_english = (
            candidate_language == "en"
            and audio_language not in {"en", "und"}
        )
        if cross_language_english:
            # English subtitles for a spoken non-English film are verifiable:
            # Whisper translates the selected audio to English and the normal
            # fixed-offset guard still decides whether the candidate is safe.
            # Keep the whole manual-candidate path inside one real 20-second
            # budget so enabling cross-language evidence cannot grow without a
            # bound.  A silent film has no audio evidence to compare; that case
            # deliberately returns to the user's explicit confirmation instead
            # of pretending the soundtrack language proves subtitle identity.
            cross_started = time.monotonic()
            cross_deadline = cross_started + MANUAL_CROSS_LANGUAGE_BUDGET_SECONDS
            cross_cancel = _DeadlineCancel(cancel, cross_deadline)
            log(
                f"手动英文字幕与{audio_language}音轨进入跨语言核验："
                "Whisper 将语音翻译为英语后检查正文和固定偏移；"
                f"本候选总耗时最多 {MANUAL_CROSS_LANGUAGE_BUDGET_SECONDS:.0f} 秒。"
            )
            try:
                shared_content = prepare_shared_subtitle_content_audio(
                    input_path,
                    validation_audio_id,
                    work / "manual-cross-language-audio",
                    log,
                    cross_cancel,
                    source_language="en",
                    time_budget_seconds=MANUAL_CROSS_LANGUAGE_BUDGET_SECONDS,
                )
            except legacy.CancelledError as exc:
                root_cancel = _root_cancel_event(cancel)
                if root_cancel is not None and root_cancel.is_set():
                    raise
                raise RuntimeError(
                    "跨语言字幕核验达到20秒时间上限，未能取得足够证据；请更换字幕版本。"
                ) from exc

            if shared_content.fingerprint.usable_clip_count < 2:
                raise RuntimeError(
                    MANUAL_CONFIRMATION_PREFIX
                    + f"手动字幕正文为 en，音轨标记为 {audio_language}，但三个分散位置"
                    "没有取得足够的有效语音。影片可能是默片、仅有配乐或对白极少，"
                    "无法通过音频自动证明字幕内容和固定偏移；请人工确认是否使用该字幕。"
                )

            remaining_seconds = max(0.0, cross_deadline - time.monotonic())
            if remaining_seconds <= 0.0:
                raise RuntimeError(
                    "跨语言字幕核验达到20秒时间上限，未能取得足够证据；请更换字幕版本。"
                )
            try:
                verified, cross_report = preflight_online_subtitle(
                    input_path,
                    str(normalized),
                    str(work / "manual-cross-language-preflight"),
                    validation_audio_id,
                    shared_content,
                    log,
                    cross_cancel,
                    source_language="en",
                    candidate_label=candidate_label,
                    time_budget_seconds=remaining_seconds,
                )
            except legacy.CancelledError as exc:
                root_cancel = _root_cancel_event(cancel)
                if root_cancel is not None and root_cancel.is_set():
                    raise
                raise RuntimeError(
                    "跨语言字幕核验达到20秒时间上限，未能取得足够证据；请更换字幕版本。"
                ) from exc
            summary = (
                f"字幕预检通过：{len(events)} 条，覆盖约 {coverage:.0%}；"
                f"{audio_language}语音经 Whisper 翻译为英语后完成核验；{cross_report}"
            )
            log(summary)
            return verified, summary

        if (
            candidate_language != "und"
            and audio_language != "und"
            and candidate_language != audio_language
        ):
            raise RuntimeError(
                MANUAL_CONFIRMATION_PREFIX
                + f"手动字幕正文为 {candidate_language}，影片主对白音轨为 {audio_language}；"
                "当前本地跨语言音频核验只支持将非英语语音翻译为英语。"
                "本候选需要手动确认或改选同语言/双语字幕。"
            )

    # Run the audio verifier inside the candidate's real remaining deadline.
    shadow_result: audio_offset_verifier.AudioOffsetResult | None = None
    audio_authoritative = (
        os.environ.get("SUBFLOW_AUDIO_OFFSET_AUTHORITATIVE", "0").strip() == "1"
    )
    configured_audio_drift_guard = _audio_drift_guard_enabled(input_path)
    audio_drift_guard = (
        configured_audio_drift_guard
        if audio_drift_guard_override is None
        else bool(audio_drift_guard_override) and configured_audio_drift_guard
    )
    shadow_enabled = (
        os.environ.get("SUBFLOW_AUDIO_OFFSET_SHADOW", "").strip() == "1"
        or audio_authoritative
        or audio_drift_guard
    )
    if shadow_enabled:
        shadow_started = time.monotonic()
        remaining_seconds = 35.0
        if isinstance(cancel, _DeadlineCancel) and cancel.deadline is not None:
            remaining_seconds = max(0.0, cancel.deadline - shadow_started)
        shadow_budget_seconds = min(35.0, remaining_seconds)
        if shadow_budget_seconds <= 1.0:
            raise legacy.CancelledError("字幕候选核验时间已用完。")
        base_cancel = cancel
        shadow_cancel = _DeadlineCancel(base_cancel, shadow_started + shadow_budget_seconds)
        try:
            if audio_authoritative:
                log(
                    f"音频主导正式验证开始：当前候选剩余时间内最多 {shadow_budget_seconds:.0f} 秒；"
                    "验证通过时直接采用音频时间模型，验证不通过才回退旧策略。"
                )
            elif audio_drift_guard:
                log(
                    f"字幕速度差安全检查开始：当前候选剩余时间内最多 {shadow_budget_seconds:.0f} 秒；"
                    "只识别并拒绝规律性速度差，不修改固定偏移结果。"
                )
            else:
                log(
                    f"音频主导旁路验证开始：当前候选剩余时间内最多 {shadow_budget_seconds:.0f} 秒；"
                    "旁路耗时不计入原字幕体检预算，也不改变正式输出。"
                )
            shadow_audio_index = _audio_stream_index(input_path, validation_audio_id) or 0
            shadow_audio_language = _selected_audio_language(input_path, validation_audio_id)
            shadow_subtitle_language = normalize_language_code(source_language)
            if shadow_subtitle_language == "und":
                shadow_subtitle_language = detect_subtitle_language(events)
            shadow_translate_to_english = (
                shadow_audio_language != "en" and shadow_subtitle_language == "en"
            )
            if shadow_translate_to_english:
                log(
                    f"音频主导验证使用{shadow_audio_language}语音识别并翻译为英语，"
                    "再与英文字幕正文匹配。"
                )
            shadow_whisper_model = _tool(WHISPER_MODEL_CANDIDATES, "Whisper 识别模型")
            shadow_cache_dir = audio_offset_verifier.persistent_cache_dir(
                input_path,
                audio_stream_index=shadow_audio_index,
                whisper_model=shadow_whisper_model,
            )
            shadow_result = audio_offset_verifier.verify_file_offset(
                input_path,
                str(normalized),
                str(work / "audio-offset-shadow"),
                duration_seconds=duration,
                audio_stream_index=shadow_audio_index,
                ffmpeg=legacy.FFMPEG,
                whisper=_tool(WHISPER_CLI_CANDIDATES, "Whisper 识别引擎"),
                whisper_model=shadow_whisper_model,
                audio_language=_whisper_language(shadow_audio_language),
                translate_to_english=shadow_translate_to_english,
                log=log,
                cache_dir=str(shadow_cache_dir),
                cancel=shadow_cancel,
                deadline=shadow_cancel.deadline,
            )
        except legacy.CancelledError:
            if base_cancel is not None and base_cancel.is_set():
                raise
            log(
                f"音频主导{'正式' if audio_authoritative else '旁路'}验证达到独立 "
                f"{shadow_budget_seconds:.0f} 秒预算，已停止该验证；回退原严格策略。"
            )
        except Exception as exc:
            log(
                f"音频主导{'正式' if audio_authoritative else '旁路'}验证未完成：{exc}；"
                "回退原严格策略。"
            )
        finally:
            shadow_elapsed = time.monotonic() - shadow_started
            log(
                f"音频主导{'正式' if audio_authoritative else '旁路'}验证阶段耗时："
                f"{shadow_elapsed:.2f} 秒。"
            )

    if shadow_result is not None:
        audio_precision_gate.log_shadow(
            shadow_result,
            duration_seconds=duration,
            log=log,
        )

    drift_rejection = (
        _audio_drift_rejection(shadow_result, duration)
        if audio_drift_guard
        else ""
    )
    if (
        audio_drift_guard
        and shadow_result is not None
        and shadow_result.accepted
        and shadow_result.affine
        and not drift_rejection
    ):
        log(
            "音频内容验证仅发现低可信速度差迹象，按警告处理；"
            "不会仅凭该结果淘汰字幕。"
        )
    if drift_rejection:
        log(drift_rejection)
        raise SubtitleContentMismatchError(drift_rejection)

    production_safety = (
        audio_precision_gate.evaluate_production_safety(shadow_result)
        if audio_authoritative and shadow_result is not None
        else None
    )
    if production_safety is not None:
        log(
            f"音频时间模型成品安全闸门：{production_safety.status}；"
            f"{production_safety.reason}。"
        )

    def verify_candidate(
        path: Path,
        label: str,
        allow_fast_accept: bool = False,
    ) -> tuple[Path, SemanticCheckResult] | None:
        legacy.check_cancel(cancel)
        fingerprint = path.read_bytes()
        previous = checked_content.get(fingerprint)
        if previous is not None:
            log(f"{label}与已核验候选时间轴相同，跳过重复抽检。")
            return (path, previous) if previous.accepted else None
        result: SemanticCheckResult | None = None
        for attempt in range(2):
            try:
                result = semantic_spot_check(
                    input_path,
                    path,
                    work_dir,
                    validation_audio_id,
                    source_language,
                    duration,
                    log,
                    cancel,
                    candidate_label=label,
                    raise_on_failure=False,
                    allow_fast_accept=allow_fast_accept,
                )
                break
            except legacy.CancelledError:
                raise
            except SubtitleContentMismatchError as exc:
                log(f"{label}内容证据不足：{exc}")
                result = SemanticCheckResult(label, (), 4)
                break
            except Exception as exc:
                if attempt == 0 and not (cancel is not None and cancel.is_set()):
                    log(f"{label}核验工具第一次执行失败：{exc}；正在使用新的临时目录重试一次。")
                    continue
                raise SubtitleVerificationToolError(
                    f"字幕核验工具异常：{label}重试后仍未正常完成：{exc}"
                ) from exc
        if result is None:
            raise SubtitleVerificationToolError(f"字幕核验工具异常：{label}没有返回核验结果。")
        checked_content[fingerprint] = result
        attempted.append(result)
        return (path, result) if result.accepted else None

    selected: tuple[Path, SemanticCheckResult] | None = None
    precise_timeline_report: str | None = None

    if (
        audio_authoritative
        and shadow_result is not None
        and shadow_result.accepted
        and shadow_result.offset_seconds is not None
        and production_safety is not None
        and production_safety.eligible
    ):
        if _audio_fine_shadow_enabled():
            fine_budget_seconds = audio_fine_aligner.DEFAULT_TIME_BUDGET
            fine_started = time.monotonic()
            base_cancel = getattr(cancel, "cancel_event", cancel)
            fine_cancel = _DeadlineCancel(base_cancel, time.monotonic() + fine_budget_seconds)
            try:
                audio_fine_aligner.run_shadow(
                    input_path,
                    str(normalized),
                    str(work / "audio-fine-shadow"),
                    shadow_result,
                    duration_seconds=duration,
                    audio_stream_index=shadow_audio_index,
                    ffmpeg=legacy.FFMPEG,
                    whisper=_tool(WHISPER_CLI_CANDIDATES, "Whisper 识别引擎"),
                    whisper_model=shadow_whisper_model,
                    log=log,
                    cancel=fine_cancel,
                )
            except legacy.CancelledError:
                if base_cancel is not None and base_cancel.is_set():
                    raise
                log(
                    f"Stage 3.4 精细对时达到 {fine_budget_seconds:.0f} 秒硬上限；"
                    "Shadow 结果作废，最终字幕继续只使用 Stage 3.2。"
                )
            except Exception as exc:
                log(f"Stage 3.4 精细对时未完成：{exc}；最终字幕继续只使用 Stage 3.2。")
            finally:
                fine_elapsed = time.monotonic() - fine_started
                if isinstance(cancel, _DeadlineCancel) and cancel.deadline is not None:
                    cancel.deadline += fine_elapsed

        authoritative_path = _apply_audio_verifier_timeline(
            normalized,
            work / "external-audio-authoritative.srt",
            shadow_result,
        )
        if shadow_result.affine:
            label = (
                f"音频内容正式时间模型（比例 {shadow_result.scale_label}，"
                f"起始偏移 {shadow_result.offset_seconds:+.2f} 秒，"
                f"每小时漂移 {shadow_result.drift_per_hour:+.2f} 秒，"
                f"{len(shadow_result.anchors)} 个独立锚点）"
            )
        else:
            label = (
                f"音频内容正式固定偏移（{shadow_result.offset_seconds:+.2f} 秒，"
                f"{len(shadow_result.anchors)} 个独立锚点）"
            )

        # The audio verifier has already performed the content identity check.
        # Do not run the old ffsubsync + semantic spot-check again; doing so is
        # exactly what caused the validated +23.91s timeline to be discarded.
        evidence = SemanticCheckResult(
            label,
            tuple(shadow_result.clip_scores),
            max(1, len(shadow_result.clip_scores)),
            fast_path=False,
        )
        selected = (authoritative_path, evidence)
        precise_timeline_report = (
            f"{label}；{shadow_result.reason}"
        )
        log(
            f"音频主导正式验证通过：已采用{label}。"
            "跳过 ffsubsync、旧四点台词抽检及重复原时间轴核验。"
        )
    elif (
        audio_authoritative
        and shadow_result is not None
        and shadow_result.accepted
        and production_safety is not None
        and not production_safety.eligible
    ):
        log(
            f"音频主导模型未获成品资格：{production_safety.reason}；"
            "不选择不确定的替代模型，回退原严格策略。"
        )
    elif audio_authoritative and shadow_result is not None:
        log(
            f"音频主导正式验证未通过：{shadow_result.reason}；"
            "不强行使用结果，回退原严格策略。"
        )

    if selected is None and use_embedded_reference:
        try:
            embedded_timeline = _prepare_aligned_embedded_text_reference(
                input_path,
                source_language,
                validation_audio_id,
                cache_root,
                log,
                cancel,
            )
        except Exception as exc:
            log(f"影片原字幕时间轴体检不可用，继续使用音频兜底：{exc}")
            embedded_timeline = None
        if embedded_timeline is not None:
            embedded_path, embedded_report = embedded_timeline
            affine = _estimate_affine_timeline(normalized, embedded_path, duration)
            if affine is None:
                raise RuntimeError(
                    "候选字幕与已校正到音频的影片原字幕无法形成稳定的固定偏移或标准帧率线性关系；"
                    "疑似不同剪辑版本或非线性漂移，已提前停止该候选，避免进入耗时的模糊匹配。"
                )
            if abs(affine.drift_per_hour) >= 0.5:
                raise SubtitleContentMismatchError(
                    "候选字幕与已校正原字幕存在明显线性漂移；当前只自动应用"
                    "统一固定偏移，请改选同剪辑版本字幕。"
                )
            corrected = _apply_affine_timeline(
                normalized,
                work / "external-affine-corrected.srt",
                affine,
            )
            correction_kind = (
                "固定偏移"
                if abs(affine.drift_per_hour) < 0.5
                else "固定偏移与线性漂移"
            )
            label = (
                f"影片时间轴精确参照校正（{correction_kind} {affine.offset_seconds:+.2f} 秒，"
                f"每小时漂移 {affine.drift_per_hour:+.2f} 秒，"
                f"{affine.match_count} 条逐句匹配，P90 残差 {affine.p90_residual:.2f} 秒）"
            )
            log(f"{label}；{embedded_report}")
            selected = (corrected, SemanticCheckResult(label, (1.0, 1.0, 1.0, 1.0), 4))
            precise_timeline_report = label

    alignment_diagnostics: dict[str, float] = {}
    if selected is None:
        log(f"正在使用十段音频采样计算{candidate_label}的可靠固定偏移。")
        try:
            precise_audio_candidate = _alignment_candidate(
                input_path,
                normalized,
                work / "external-audio-candidate-0.srt",
                validation_audio_id,
                "audio-segmented",
                log,
                cancel,
                vad="webrtc",
                diagnostics=alignment_diagnostics,
                shared_audio_reference=shared_audio_reference,
            )
        except legacy.CancelledError:
            raise
        except Exception as exc:
            raise SubtitleVerificationToolError(
                f"字幕核验工具异常：ffsubsync 未能完成音频对时：{exc}"
            ) from exc
        if shadow_result is not None and shadow_result.accepted and shadow_result.offset_seconds is not None:
            ffsubsync_offset = alignment_diagnostics.get("offset_seconds", 0.0)
            if shadow_result.affine:
                log(
                    f"旁路对照：音频内容定位检测到线性时间关系 {shadow_result.scale_label}，"
                    f"起始偏移 {shadow_result.offset_seconds:+.2f} 秒，"
                    f"每小时漂移 {shadow_result.drift_per_hour:+.2f} 秒；"
                    f"ffsubsync 仅给出固定偏移 {ffsubsync_offset:+.2f} 秒，二者不能直接作单值差；"
                    + (
                        "正式音频验证未接管本候选，当前仅作对照。"
                        if audio_authoritative else
                        "旁路结果仅记录，不改变本次输出。"
                    )
                )
            else:
                difference = abs(shadow_result.offset_seconds - ffsubsync_offset)
                log(
                    f"旁路对照：音频内容定位固定偏移 {shadow_result.offset_seconds:+.2f} 秒，"
                    f"ffsubsync {ffsubsync_offset:+.2f} 秒，差值 {difference:.2f} 秒；"
                    + (
                        "正式音频验证未接管本候选，当前仅作对照。"
                        if audio_authoritative else
                        "旁路结果仅记录，不改变本次输出。"
                    )
                )
        elif shadow_result is not None:
            log(f"旁路对照：音频内容定位未通过（{shadow_result.reason}）；继续观察正式策略。")

        selected = verify_candidate(
            precise_audio_candidate,
            "十段音频采样校正时间轴",
            allow_fast_accept=abs(alignment_diagnostics.get("offset_seconds", 0.0)) <= MAX_AUTOMATIC_OFFSET_SECONDS,
        )
    else:
        precise_audio_candidate = selected[0]

    reported_offset = abs(alignment_diagnostics.get("offset_seconds", 0.0))
    abnormal_failed_offset = (
        selected is None
        and reported_offset > MAX_AUTOMATIC_OFFSET_SECONDS
        and precise_audio_candidate.read_bytes() != normalized.read_bytes()
    )
    if selected is None:
        if abnormal_failed_offset:
            log(
                f"ffsubsync 给出异常大偏移 {reported_offset:.2f} 秒，但校正时间轴未通过台词核验；"
                "拒绝该偏移，继续核验字幕原时间轴。"
            )
        log("全片音频校正结果未通过内容核验，回退检查字幕原时间轴。")
        selected = verify_candidate(normalized, f"{candidate_label}原时间轴", allow_fast_accept=True)

    if selected is None:
        if _alass_fallback_enabled():
            try:
                user_cancel = _root_cancel_event(cancel)
                fallback_cancel = (
                    cancel
                    if isinstance(cancel, _DeadlineCancel)
                    else _DeadlineCancel(
                        user_cancel,
                        time.monotonic() + ALASS_FALLBACK_TIMEOUT_SECONDS,
                    )
                )
                alass_path, alass_offset = _alass_fixed_fallback(
                    input_path,
                    normalized,
                    work / "external-alass-fixed.srt",
                    log,
                    fallback_cancel,
                )
                if abs(alass_offset) > MAX_AUTOMATIC_OFFSET_MILLISECONDS:
                    log(
                        f"alass 提议偏移 {alass_offset / 1000:+.2f} 秒，"
                        f"超过自动处理范围 ±{MAX_AUTOMATIC_OFFSET_SECONDS:g} 秒，已拒绝。"
                    )
                    raise RuntimeError("alass 偏移超出自动安全范围")
                result = semantic_spot_check(
                    input_path,
                    alass_path,
                    str(work / "alass-semantic-check"),
                    validation_audio_id,
                    source_language,
                    duration,
                    log,
                    fallback_cancel,
                    candidate_label="alass 固定偏移兜底结果",
                    raise_on_failure=False,
                )
                if result.accepted:
                    summary = (
                        f"字幕预检通过：{len(events)} 条，覆盖约 {coverage:.0%}；"
                        f"旧策略未确认后由 alass 固定偏移兜底 {alass_offset / 1000:+.2f} 秒，"
                        f"并通过独立台词抽检"
                    )
                    log(summary)
                    return alass_path, summary
                log("alass 固定偏移结果未通过独立台词抽检，已安全拒绝。")
            except legacy.CancelledError:
                if (user_cancel := _root_cancel_event(cancel)) is not None and user_cancel.is_set():
                    raise
                log("alass 固定偏移兜底达到本候选剩余时间上限，已终止并安全回退。")
            except Exception as exc:
                log(f"alass 固定偏移兜底未通过：{exc}")
        reports = "；".join(result.report for result in attempted)
        offset_detail = (
            f"ffsubsync 的异常偏移 {reported_offset:.2f} 秒已被拒绝；"
            if abnormal_failed_offset
            else ""
        )
        raise RuntimeError(
            f"{offset_detail}已依次尝试分段音频和原时间轴，"
            f"仍未找到可靠时间轴：{reports}。"
        )

    selected_path, selected_result = selected
    verified = Path(work_dir) / "external-verified.srt"
    shutil.copy2(selected_path, verified)
    log(f"已选用{precise_timeline_report or selected_result.label}。")
    summary = f"字幕预检通过：{len(events)} 条"
    if duration:
        summary += f"，覆盖约 {coverage:.0%}，音轨时间轴匹配"
    else:
        summary += "，音轨时间轴匹配"
    summary += f"；{precise_timeline_report or selected_result.report}"
    return verified, summary


def _legacy_whisper_preflight_external_subtitle(
    input_path: str,
    subtitle_path: str,
    work_dir: str,
    selected_audio_id: int | None,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    source_language: str = "",
    use_embedded_reference: bool = True,
    candidate_label: str = "下载字幕",
    timeline_cache_dir: str | None = None,
    time_budget_seconds: float | None = 60.0,
    manual_reference_first: bool = False,
    audio_drift_guard_override: bool | None = None,
    shared_audio_reference: str | Path | None = None,
) -> tuple[Path, str]:
    log = persistent_subtitle_logger(input_path, log)
    deadline = (
        time.monotonic() + max(5.0, time_budget_seconds)
        if time_budget_seconds is not None
        else None
    )
    budget_cancel = _DeadlineCancel(cancel, deadline)
    # Keep a manually supplied English text subtitle on the same guard and
    # hash-based positive-verdict cache as an automatically downloaded one.
    # The legacy ten-segment verifier must not silently provide a second,
    # weaker acceptance path for the same English candidate.
    manual_events = legacy.parse_subtitle(Path(subtitle_path)) if manual_reference_first else []
    manual_language = detect_subtitle_language(manual_events) if manual_events else "und"
    if manual_language == "und":
        manual_language = normalize_language_code(source_language)
    if manual_reference_first and manual_language == "en":
        # Reject an obviously wrong cut before starting Whisper.  The final
        # content/timing decision still belongs exclusively to the shared guard.
        manual_media = legacy.inspect_media(input_path)
        duration_ns = legacy.video_track_duration_ns(manual_media) or legacy.media_duration_ns(manual_media)
        duration_seconds = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0
        last_end = max((_subtitle_time_seconds(event.end) for event in manual_events), default=0.0)
        if duration_seconds and last_end > duration_seconds + max(180.0, duration_seconds * 0.03):
            raise RuntimeError(
                f"手动字幕时间轴比影片长 {last_end - duration_seconds:.0f} 秒，"
                "疑似加长版、不同剪辑版本或错误影片，未启动耗时核验。"
            )
        try:
            shared = prepare_shared_subtitle_content_audio(
                input_path, selected_audio_id, Path(work_dir) / "shared-content-audio",
                log, budget_cancel, source_language="en",
                time_budget_seconds=min(60.0, time_budget_seconds or 60.0),
            )
            legacy.check_cancel(budget_cancel)
            remaining = max(0.0, deadline - time.monotonic()) if deadline else 20.0
            return preflight_online_subtitle(
                input_path, subtitle_path, work_dir, selected_audio_id, shared,
                log, budget_cancel, source_language="en", candidate_label=candidate_label,
                time_budget_seconds=min(20.0, remaining),
            )
        except legacy.CancelledError as exc:
            if budget_cancel.timed_out:
                raise SubtitlePreflightTimeoutError(
                    f"字幕体检达到 {time_budget_seconds:.0f} 秒硬上限，已停止该候选。"
                ) from exc
            raise
    try:
        return _preflight_external_subtitle_impl(
            input_path,
            subtitle_path,
            work_dir,
            selected_audio_id,
            log,
            budget_cancel,
            source_language,
            use_embedded_reference,
            candidate_label,
            timeline_cache_dir,
            manual_reference_first,
            audio_drift_guard_override,
            shared_audio_reference,
        )
    except legacy.CancelledError as exc:
        if budget_cancel.timed_out:
            raise SubtitlePreflightTimeoutError(
                f"字幕体检达到 {time_budget_seconds:.0f} 秒硬上限，已终止当前工具并停止该候选。"
            ) from exc
        raise


def _targeted_dialogue_cue_score(
    event: legacy.SubtitleEvent,
    *,
    target_time: float,
    duration: float,
) -> float:
    """Prefer ordinary, distinctive dialogue over lyrics and credit-like text."""
    raw = event.text or ""
    clean = _clean_dialogue_text(raw)
    words = re.findall(r"[A-Za-z']{2,}", clean)
    if not 7 <= len(words) <= 24:
        return -math.inf
    if "♪" in raw or raw.count("...") >= 1:
        return -math.inf
    cue_start = _subtitle_time_seconds(event.start)
    cue_end = _subtitle_time_seconds(event.end)
    cue_duration = cue_end - cue_start
    if cue_duration < 1.0 or cue_duration > 7.5:
        return -math.inf
    unique = {word.lower() for word in words}
    repeated = len(words) - len(unique)
    punctuation = len(re.findall(r"[.!?]", raw))
    commas = len(re.findall(r"[,;:]", raw))
    midpoint = (cue_start + cue_end) / 2.0
    position_penalty = abs(midpoint - target_time) / max(60.0, duration * 0.10)
    return (
        min(len(words), 18) * 0.30
        + punctuation * 1.40
        + commas * 0.30
        - repeated * 0.30
        - abs(cue_duration - 3.5) * 0.12
        - position_penalty
    )


def _select_targeted_dialogue_cues(
    events: list[legacy.SubtitleEvent],
    duration: float,
) -> tuple[legacy.SubtitleEvent, ...]:
    """Freeze one primary and up to two reserves per distant area before ASR."""
    if duration <= 0:
        return ()
    primaries: list[legacy.SubtitleEvent] = []
    reserves: list[legacy.SubtitleEvent] = []
    second_reserves: list[legacy.SubtitleEvent] = []
    minimum_distance = max(180.0, duration * 0.08)
    for fraction in (0.22, 0.52, 0.79):
        target = duration * fraction
        radius = max(300.0, duration * 0.12)
        choices: list[tuple[float, float, legacy.SubtitleEvent]] = []
        for event in events:
            start = _subtitle_time_seconds(event.start)
            end = _subtitle_time_seconds(event.end)
            midpoint = (start + end) / 2.0
            if abs(midpoint - target) > radius:
                continue
            if any(
                abs(midpoint - (
                    _subtitle_time_seconds(prior.start)
                    + _subtitle_time_seconds(prior.end)
                ) / 2.0) < minimum_distance
                for prior in primaries
            ):
                continue
            score = _targeted_dialogue_cue_score(
                event,
                target_time=target,
                duration=duration,
            )
            if math.isfinite(score):
                choices.append((score, -midpoint, event))
        if choices:
            ranked = sorted(choices, key=lambda item: (-item[0], -item[1]))
            primary = ranked[0][2]
            primaries.append(primary)
            primary_time = _subtitle_time_seconds(primary.start)
            area_reserves: list[legacy.SubtitleEvent] = []
            for _score, _time, event in ranked[1:]:
                event_time = _subtitle_time_seconds(event.start)
                if all(
                    abs(event_time - _subtitle_time_seconds(chosen.start)) >= 15.0
                    for chosen in (primary, *area_reserves)
                ):
                    area_reserves.append(event)
                    if len(area_reserves) == 2:
                        break
            reserves.append(area_reserves[0] if area_reserves else primary)
            second_reserves.append(
                area_reserves[1] if len(area_reserves) == 2 else primary
            )
    if len(primaries) != 3:
        return ()
    return tuple(primaries + reserves + second_reserves)


def _extract_targeted_dialogue_fingerprint(
    input_path: str,
    cues: tuple[legacy.SubtitleEvent, ...],
    *,
    work_dir: Path,
    audio_stream_index: int,
    audio_language: str,
    provisional_offset: float,
    start_index: int,
    clip_indexes: tuple[int, ...] | None = None,
    log: Callable[[str], None],
    cancel: threading.Event | None,
    task_failures: dict[str, str] | None = None,
) -> audio_offset_verifier.MovieAudioFingerprint:
    """Transcribe short candidate-selected windows with the bundled base model."""
    padding = 6.0
    specs: list[tuple[int, float, float]] = []
    indexes = clip_indexes or tuple(range(start_index, start_index + len(cues)))
    if len(indexes) != len(cues):
        raise ValueError("候选音频窗口编号与台词数量不一致")
    for sequence, cue in zip(indexes, cues):
        cue_start = _subtitle_time_seconds(cue.start)
        cue_end = _subtitle_time_seconds(cue.end)
        clip_start = max(0.0, cue_start + provisional_offset - padding)
        clip_duration = max(5.0, cue_end - cue_start + padding * 2.0)
        specs.append((sequence, clip_start, clip_duration))

    legacy.check_cancel(cancel)
    evidence_started = time.monotonic()
    transcribed, transcribed_words, failures = audio_offset_verifier.transcribe_exact_windows(
        input_path, specs, audio_stream_index=audio_stream_index,
        ffmpeg=legacy.FFMPEG,
        whisper=_tool(WHISPER_CLI_CANDIDATES, "Whisper 识别引擎"),
        whisper_model=_tool(WHISPER_MODEL_CANDIDATES, "Whisper 识别模型"),
        audio_language=_whisper_language(audio_language),
        translate_to_english=(normalize_language_code(audio_language) != "en"),
        cancel=cancel, log=log, task_failures=task_failures,
    )
    log(f"候选定向核验：{len(cues)}处音频窗口取证含缓存等待{time.monotonic() - evidence_started:.2f}秒。")
    for index, reason in failures.items():
        log(f"影片窗口 {index} 取证失败：{reason}；本次任务改用同区备用点。")
    fingerprint = audio_offset_verifier.MovieAudioFingerprint(
        clip_indexes=indexes,
        events=tuple(
            event for index in indexes for event in transcribed.get(index, [])
        ),
        qualities=(),
        words=tuple(
            word for index in indexes for word in transcribed_words.get(index, [])
        ),
    )
    return fingerprint


def _merge_audio_fingerprints(
    *fingerprints: audio_offset_verifier.MovieAudioFingerprint,
) -> audio_offset_verifier.MovieAudioFingerprint:
    return audio_offset_verifier.MovieAudioFingerprint(
        clip_indexes=tuple(
            index for fingerprint in fingerprints for index in fingerprint.clip_indexes
        ),
        events=tuple(
            event for fingerprint in fingerprints for event in fingerprint.events
        ),
        qualities=(),
        words=tuple(
            word for fingerprint in fingerprints for word in fingerprint.words
        ),
    )


def _targeted_candidate_offset_guard(
    input_path: str,
    normalized: Path,
    events: list[legacy.SubtitleEvent],
    *,
    work_dir: Path,
    shared_content_audio: SharedSubtitleContentAudio,
    provisional_offset: float,
    candidate_label: str,
    log: Callable[[str], None],
    cancel: threading.Event | None,
) -> tuple[subtitle_offset_guard.OffsetGuardDecision, int]:
    if abs(provisional_offset) > MAX_AUTOMATIC_OFFSET_SECONDS:
        raise SubtitleContentMismatchError(
            f"候选字幕初步偏移{provisional_offset:+.2f}秒超过自动范围"
        )
    cues = _select_targeted_dialogue_cues(events, shared_content_audio.duration_seconds)
    if len(cues) < 3:
        raise SubtitleContentMismatchError("候选字幕没有三处适合定向核验的远距离对白。")
    cue_times = "、".join(
        event.start.rsplit(",", 1)[0] for event in cues[:3]
    )
    region_by_clip = {index: index % 3 for index in range(12)}
    fingerprint = shared_content_audio.fingerprint
    public_measurements = subtitle_offset_guard.measured_clip_offsets(
        normalized, fingerprint, provisional_offset
    )
    covered_regions = {
        region_by_clip[index] for index in public_measurements if index in region_by_clip
    }
    missing_regions = tuple(region for region in range(3) if region not in covered_regions)
    log(
        f"{candidate_label}公共取证已有{len(covered_regions)}区可用逐词证据；"
        f"候选定向只补{len(missing_regions)}区：{cue_times}；"
        f"仍只允许±{MAX_AUTOMATIC_OFFSET_SECONDS:g}秒固定偏移。"
    )
    if missing_regions:
        targeted = _extract_targeted_dialogue_fingerprint(
            input_path,
            tuple(cues[region] for region in missing_regions),
            work_dir=work_dir / "targeted-dialogue-missing",
            audio_stream_index=shared_content_audio.audio_stream_index,
            audio_language=shared_content_audio.audio_language,
            provisional_offset=provisional_offset,
            start_index=3,
            clip_indexes=tuple(3 + region for region in missing_regions),
            log=log,
            cancel=cancel,
            task_failures=shared_content_audio.evidence_failures,
        )
        fingerprint = _merge_audio_fingerprints(fingerprint, targeted)
    measured_by_clip = subtitle_offset_guard.measured_clip_offsets(
        normalized, fingerprint, provisional_offset
    )
    conflicted_by_clip = subtitle_offset_guard.conflicted_clip_offsets(
        normalized, fingerprint, provisional_offset
    )
    for index, values in conflicted_by_clip.items():
        log(
            f"{candidate_label}影片窗口 {index} 内{len(values)}句对白的逐词偏移"
            f"跨度{max(values) - min(values):.3f}秒；不把中位数当作可信区域，"
            "保留冲突并检查同区备用点。"
        )
    measured = tuple(measured_by_clip.values())
    refined_offset = statistics.median(measured) if measured else provisional_offset
    decision = subtitle_offset_guard.evaluate_proposed_offset(
        normalized,
        fingerprint,
        refined_offset,
        require_word_evidence=True,
        region_by_clip=region_by_clip,
    )
    used = len(missing_regions)
    log(f"{candidate_label}公共及定向证据核验：{subtitle_offset_guard.describe(decision)}。")
    for region in range(3):
        if any(region_by_clip.get(index) == region for index in measured_by_clip):
            continue
        if not any(region_by_clip.get(index) == region for index in fingerprint.clip_indexes):
            failure = "NO_AUDIO_EVIDENCE"
        elif any(region_by_clip.get(index) == region for index in conflicted_by_clip):
            failure = "INTRA_WINDOW_TIMING_CONFLICT"
        elif any(region_by_clip.get(word.source_index) == region for word in fingerprint.words):
            failure = "CANDIDATE_MATCH_MISS"
        else:
            failure = "EMPTY_TRANSCRIPT"
        log(f"{candidate_label}第{region + 1}区主点未取得逐词对应：{failure}。")
        attempted = [cues[region]]
        for reserve_level in (0, 1):
            cue_index = 3 + region + 3 * reserve_level
            if len(cues) <= cue_index:
                break
            cue = cues[cue_index]
            if cue in attempted:
                continue
            attempted.append(cue)
            legacy.check_cancel(cancel)
            log(f"{candidate_label}第{region + 1}区按预选清单补取第{reserve_level + 1}个备用点。")
            clip_index = 6 + region + 3 * reserve_level
            backup = _extract_targeted_dialogue_fingerprint(
                input_path, (cue,),
                work_dir=work_dir / f"targeted-dialogue-backup-{region}-{reserve_level}",
                audio_stream_index=shared_content_audio.audio_stream_index,
                audio_language=shared_content_audio.audio_language,
                provisional_offset=provisional_offset,
                start_index=clip_index,
                clip_indexes=(clip_index,),
                log=log, cancel=cancel,
                task_failures=shared_content_audio.evidence_failures,
            )
            fingerprint = _merge_audio_fingerprints(fingerprint, backup)
            used += 1
            backup_measurement = subtitle_offset_guard.measured_clip_offsets(
                normalized, backup, provisional_offset
            )
            if backup_measurement:
                measured_by_clip.update(backup_measurement)
                break
            log(f"{candidate_label}第{region + 1}区第{reserve_level + 1}个备用点仍无候选逐词对应。")
    measured = subtitle_offset_guard.measure_region_offsets(
        normalized, fingerprint, provisional_offset, region_by_clip
    )
    if measured:
        refined_offset = statistics.median(measured)
    decision = subtitle_offset_guard.evaluate_proposed_offset(
        normalized, fingerprint, refined_offset,
        require_word_evidence=True, region_by_clip=region_by_clip,
    )
    log(f"{candidate_label}分区补点后统一裁决：{subtitle_offset_guard.describe(decision)}。")
    return decision, used


def _legacy_whisper_preflight_online_subtitle(
    input_path: str,
    subtitle_path: str,
    work_dir: str,
    selected_audio_id: int | None,
    shared_content_audio: SharedSubtitleContentAudio,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    source_language: str = "en",
    candidate_label: str = "在线字幕",
    time_budget_seconds: float = 20.0,
) -> tuple[Path, str]:
    """Verify an online candidate against shared dialogue, then handle offset."""
    log = persistent_subtitle_logger(input_path, log)
    deadline = time.monotonic() + max(5.0, time_budget_seconds)
    budget_cancel = _DeadlineCancel(cancel, deadline)
    source = Path(subtitle_path)
    events = legacy.parse_subtitle(source)
    if not events:
        raise RuntimeError("字幕文件没有可识别的正文和时间轴。")

    duration = shared_content_audio.duration_seconds
    subtitle_end = max(
        (_subtitle_time_seconds(event.end) for event in events),
        default=0.0,
    )
    coverage = subtitle_end / duration if duration > 0 else 0.0
    if duration >= 900 and len(events) < 20:
        raise RuntimeError(
            f"字幕只有 {len(events)} 条，属于片段或强制字幕，不进入台词筛选。"
        )
    if duration > 0 and coverage < 0.55:
        raise RuntimeError(
            f"字幕只覆盖影片约 {coverage:.0%}，属于不完整字幕，不进入台词筛选。"
        )
    if duration > 0 and subtitle_end > duration + max(180.0, duration * 0.12):
        raise RuntimeError("字幕时间轴明显长于影片，不进入台词筛选。")

    detected_language = detect_subtitle_language(events)
    requested_language = normalize_language_code(source_language)
    if (
        requested_language == "en"
        and detected_language not in {"en", "und"}
    ):
        raise RuntimeError(
            f"字幕正文识别为 {detected_language}，不是要求的英文字幕。"
        )

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    video = Path(input_path).resolve()
    video_stat = video.stat()
    input_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    rule_parameters = {
        name: getattr(subtitle_offset_guard, name)
        for name in (
            "MIN_REGIONS", "MIN_TOKEN_CONFIDENCE", "MIN_CUE_WORDS",
            "MIN_CUE_COVERAGE", "ORIGINAL_GOOD_SECONDS", "APPLIED_GOOD_SECONDS",
            "MIN_ABSOLUTE_IMPROVEMENT", "MIN_RELATIVE_IMPROVEMENT",
            "MAX_PROPOSAL_DISAGREEMENT", "REGION_CLUSTER_TOLERANCE_SECONDS",
            "MAX_ABSOLUTE_REGION_OFFSET_SECONDS",
        )
    }
    verdict_signature = {
        "version": subtitle_offset_guard.GUARD_RULE_VERSION,
        "video": str(video).lower(), "video_size": video_stat.st_size,
        "video_mtime_ns": video_stat.st_mtime_ns,
        "audio_stream_index": shared_content_audio.audio_stream_index,
        "audio_language": shared_content_audio.audio_language,
        "source_hash": input_hash,
        "source_language": requested_language,
        "maximum_offset": MAX_AUTOMATIC_OFFSET_SECONDS,
        "guard_parameters": rule_parameters,
    }
    verdict_root = audio_offset_verifier.persistent_cache_dir(
        input_path,
        audio_stream_index=shared_content_audio.audio_stream_index,
        whisper_model=_tool(WHISPER_MODEL_CANDIDATES, "Whisper 识别模型"),
    ) / "verified-subtitles"
    verdict_root.mkdir(parents=True, exist_ok=True)
    verdict_key = hashlib.sha256(
        json.dumps(verdict_signature, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    verdict_meta = verdict_root / f"{verdict_key}.json"
    verdict_srt = verdict_root / f"{verdict_key}.srt"
    try:
        cached = json.loads(verdict_meta.read_text(encoding="utf-8"))
        if (
            cached.get("signature") == verdict_signature
            and cached.get("status") in {"KEEP_ORIGINAL", "APPLY_OFFSET"}
            and verdict_srt.is_file()
            and hashlib.sha256(verdict_srt.read_bytes()).hexdigest() == cached.get("output_hash")
        ):
            verified = work / "online-verified.srt"
            if verified.resolve() != verdict_srt.resolve():
                shutil.copy2(verdict_srt, verified)
            log(f"{candidate_label}复用同原字幕哈希及规则版本的已完成裁决：{cached['status']}。")
            return verified, str(cached["report"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        pass
    # A verified output may be handed back through the manual picker. Match
    # its exact bytes and provenance before treating it as already shifted.
    for prior_meta in verdict_root.glob("*.json"):
        try:
            prior = json.loads(prior_meta.read_text(encoding="utf-8"))
            prior_signature = prior["signature"]
            same_context = all(
                prior_signature.get(key) == value
                for key, value in verdict_signature.items() if key != "source_hash"
            )
            if (
                same_context
                and prior.get("status") in {"KEEP_ORIGINAL", "APPLY_OFFSET"}
                and prior.get("output_hash") == input_hash
                and prior.get("original_input_hash") == prior_signature.get("source_hash")
            ):
                verified = work / "online-verified.srt"
                if source.resolve() != verified.resolve():
                    shutil.copy2(source, verified)
                log(f"{candidate_label}输入已是核验输出，原输入哈希"
                    f"{prior['original_input_hash'][:12]}，已应用偏移"
                    f"{float(prior.get('applied_offset', 0.0)):+.2f}秒；不二次纠偏。")
                return verified, str(prior["report"])
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
    normalized = subtitle_events_to_srt(source, work / "online-source.srt")
    legacy.check_cancel(budget_cancel)
    match_started = time.monotonic()
    quick_offsets: tuple[float, ...] = ()
    if shared_content_audio.fingerprint.usable_clip_count < 3:
        # A full model cannot settle the three-area guard from fewer public
        # regions. Reuse the existing exact-word measurement to obtain a
        # bounded starting hint, then spend the candidate budget on its own
        # preselected dialogue windows.
        quick_offsets = subtitle_offset_guard.measure_region_offsets(
            normalized, shared_content_audio.fingerprint, 0.0
        )
        result = audio_offset_verifier.AudioOffsetResult(
            False, None, (), (), "公共位置不足三个，转候选定向核验"
        )
        log(f"{candidate_label}公共音频仅{shared_content_audio.fingerprint.usable_clip_count}处可用，"
            "略过无法完成三分区裁决的通用建模，直接定向补证。")
    else:
        result = audio_offset_verifier.match_subtitle_to_fingerprint(
            normalized,
            shared_content_audio.fingerprint,
            duration_seconds=duration,
            temporal_corroboration=(shared_content_audio.audio_language != "en"),
        )
    legacy.check_cancel(budget_cancel)
    matched_locations = len({anchor.clip_index for anchor in result.anchors})
    content_anchors = tuple(anchor for anchor in result.anchors if anchor.score >= 0.50)
    content_span = (
        max(anchor.movie_time for anchor in content_anchors)
        - min(anchor.movie_time for anchor in content_anchors)
        if len(content_anchors) >= 2
        else 0.0
    )
    independent_content_match = (
        len({anchor.clip_index for anchor in content_anchors}) >= 2
        and content_span >= max(300.0, duration * 0.20 if duration > 0 else 300.0)
        and statistics.fmean(anchor.score for anchor in content_anchors) >= 0.52
    )
    public_content_confirmed = result.accepted or independent_content_match
    public_elapsed = time.monotonic() - match_started
    if public_content_confirmed:
        evidence_label = (
            f"{matched_locations} 个分散位置形成完整时间模型"
            if result.accepted
            else f"{len(content_anchors)} 个远距离位置确认字幕正文属于本片"
        )
        log(
            f"{candidate_label}公共台词筛选通过：{evidence_label}；"
            f"耗时 {public_elapsed:.2f} 秒。"
        )
    else:
        state = "证据不足" if matched_locations or quick_offsets else "没有取得可用对应"
        log(
            f"{candidate_label}公共台词筛选{state}；耗时 {public_elapsed:.2f} 秒。"
            "这不能单独否定候选，改由候选自身的分散对白定向核验。"
        )

    drift_rejection = _audio_drift_rejection(result, duration)
    if drift_rejection:
        raise SubtitleContentMismatchError(drift_rejection)

    in_range_anchor_offsets = tuple(
        float(anchor.movie_time - anchor.subtitle_time)
        for anchor in (content_anchors or result.anchors)
        if abs(float(anchor.movie_time - anchor.subtitle_time))
        <= MAX_AUTOMATIC_OFFSET_SECONDS
    )
    if result.offset_seconds is not None and not result.affine:
        proposed_offset = float(result.offset_seconds)
        if abs(proposed_offset) > MAX_AUTOMATIC_OFFSET_SECONDS:
            raise SubtitleContentMismatchError(
                f"候选字幕固定偏移 {proposed_offset:+.2f} 秒超过自动范围 "
                f"±{MAX_AUTOMATIC_OFFSET_SECONDS:g} 秒。"
            )
    elif in_range_anchor_offsets:
        # Public wording matches are useful as a bounded starting point.  The
        # value is never applied directly; the exact-word guard must prove it.
        proposed_offset = statistics.median(in_range_anchor_offsets)
    elif len(quick_offsets) >= 2 and (
        max(quick_offsets) - min(quick_offsets)
        <= subtitle_offset_guard.REGION_CLUSTER_TOLERANCE_SECONDS
    ):
        proposed_offset = statistics.median(quick_offsets)
    else:
        # With weak public probes there may be no trustworthy starting value.
        # Zero keeps the candidate-directed extraction local, while its exact
        # word evidence remains responsible for every acceptance decision.
        proposed_offset = 0.0

    guard: subtitle_offset_guard.OffsetGuardDecision | None = None
    targeted_locations = 0
    if public_content_confirmed or len(quick_offsets) >= 2:
        guard = subtitle_offset_guard.evaluate_proposed_offset(
            normalized,
            shared_content_audio.fingerprint,
            proposed_offset,
            require_word_evidence=True,
        )
        log(f"{candidate_label}公共逐词偏移验真：{subtitle_offset_guard.describe(guard)}。")

    if guard is None or guard.status == "NEED_MORE":
        try:
            guard, targeted_locations = _targeted_candidate_offset_guard(
                input_path,
                normalized,
                events,
                work_dir=work,
                shared_content_audio=shared_content_audio,
                provisional_offset=proposed_offset,
                candidate_label=candidate_label,
                log=log,
                cancel=budget_cancel,
            )
        except legacy.CancelledError as exc:
            if budget_cancel.timed_out and not (cancel is not None and cancel.is_set()):
                raise SubtitlePreflightTimeoutError(
                    "候选20秒核验预算耗尽，尚无最终裁决；不缓存为拒绝"
                ) from exc
            raise
    if not guard.eligible or guard.selected_offset is None:
        raise SubtitleContentMismatchError(
            f"候选字幕核验结论 {guard.status}：{guard.reason}"
        )
    if targeted_locations:
        log(
            f"{candidate_label}定向核验通过："
            f"{len(guard.region_offsets)}个独立区域形成{guard.status}裁决，"
            f"实际取证{targeted_locations}处。"
        )

    offset = float(guard.selected_offset)
    verified = work / "online-verified.srt"
    if abs(offset) <= 0.25:
        shutil.copy2(normalized, verified)
        offset_report = "逐词证据确认原时间轴更可靠，保持不变"
    else:
        _shifted_srt(normalized, verified, int(round(offset * 1000)))
        offset_report = f"逐词证据确认后应用固定偏移 {offset:+.2f} 秒"
    legacy.check_cancel(budget_cancel)
    log(f"{candidate_label}后续偏移处理完成：{offset_report}。")
    coverage_report = f"，覆盖约 {coverage:.0%}" if duration > 0 else ""
    report = (
        f"字幕筛选通过：{len(events)} 条{coverage_report}；"
        f"{len(guard.region_offsets)} 个独立区域对应；{offset_report}"
    )
    # Only terminal positive decisions persist. Timeout, NEED_MORE and
    # TIMING_UNRESOLVED must be re-evaluated on a future task.
    temp_srt = verdict_srt.with_suffix(".srt.tmp")
    shutil.copy2(verified, temp_srt)
    temp_srt.replace(verdict_srt)
    output_hash = hashlib.sha256(verdict_srt.read_bytes()).hexdigest()
    meta_payload = {
        "signature": verdict_signature, "status": guard.status,
        "original_input_hash": input_hash, "applied_offset": offset,
        "output_hash": output_hash, "report": report,
    }
    temp_meta = verdict_meta.with_suffix(".json.tmp")
    temp_meta.write_text(json.dumps(meta_payload, ensure_ascii=False), encoding="utf-8")
    temp_meta.replace(verdict_meta)
    return verified, report


def _apply_audio_verifier_timeline(
    source: Path,
    destination: Path,
    result: audio_offset_verifier.AudioOffsetResult,
) -> Path:
    """Apply a timeline model already proven by independent movie-audio anchors.

    Audio verifier semantics:
        movie_time = offset + scale * subtitle_time

    This helper only applies an already-accepted result; it does not relax or
    reinterpret the verifier's acceptance rules.
    """
    if not result.accepted or result.offset_seconds is None:
        raise RuntimeError("音频验证结果未通过，不能应用到字幕时间轴。")

    if not result.affine:
        return _shifted_srt(
            source,
            destination,
            int(round(result.offset_seconds * 1000)),
        )

    source_events = legacy.parse_subtitle(source)
    corrected: list[legacy.SubtitleEvent] = []
    for event in source_events:
        start_seconds = (
            result.offset_seconds
            + result.scale * _subtitle_time_seconds(event.start)
        )
        end_seconds = (
            result.offset_seconds
            + result.scale * _subtitle_time_seconds(event.end)
        )
        start = max(0, int(round(start_seconds * 1000)))
        end = max(start + 1, int(round(end_seconds * 1000)))
        corrected.append(
            legacy.SubtitleEvent(
                legacy.srt_time_from_milliseconds(start),
                legacy.srt_time_from_milliseconds(end),
                event.text,
            )
        )
    legacy.write_srt(
        destination,
        corrected,
        {index: event.text for index, event in enumerate(corrected, 1)},
    )
    return destination



def _subtitle_offset_milliseconds(original: Path, corrected: Path) -> int:
    original_events = legacy.parse_subtitle(original)
    corrected_events = legacy.parse_subtitle(corrected)
    if len(original_events) < 3 or len(original_events) != len(corrected_events):
        raise RuntimeError("自动校正后的字幕结构发生变化，无法安全套用轨道偏移。")
    matched_pairs = [
        (original_event, corrected_event)
        for original_event, corrected_event in zip(original_events, corrected_events)
        if _clean_dialogue_text(original_event.text) == _clean_dialogue_text(corrected_event.text)
    ]
    differences = [
        (_subtitle_time_seconds(corrected_event.start)
         - _subtitle_time_seconds(original_event.start)) * 1000
        for original_event, corrected_event in matched_pairs
    ]
    if len(differences) < max(3, len(original_events) // 2):
        raise RuntimeError("自动校正前后的字幕事件无法稳定对应。")
    median = statistics.median(differences)
    # A negative shift clamps cues before t=0. Those starts cannot express the
    # fixed offset, so compare only cues that were not clipped by that clamp.
    comparable = [
        difference
        for difference, (original_event, _corrected_event) in zip(differences, matched_pairs)
        if not (
            median < 0
            and _subtitle_time_seconds(original_event.start) * 1000 + median < 0
        )
    ]
    if len(comparable) < 3:
        raise RuntimeError("可比较的未钳位字幕事件不足，无法安全套用轨道偏移。")
    deviation = max(abs(value - median) for value in comparable)
    if deviation > 250:
        raise RuntimeError("检测到字幕存在非固定时间轴变化，不能仅用轨道偏移安全纠正。")
    return int(round(median))


def _root_cancel_event(cancel: threading.Event | None) -> threading.Event | None:
    current = cancel
    visited: set[int] = set()
    while current is not None and hasattr(current, "cancel_event"):
        identity = id(current)
        if identity in visited:
            break
        visited.add(identity)
        current = getattr(current, "cancel_event", None)
    return current


def _alass_fixed_fallback(
    input_path: str,
    subtitle_path: Path,
    output_path: Path,
    log: Callable[[str], None],
    cancel: threading.Event | None,
) -> tuple[Path, int]:
    alass = _tool(ALASS_CANDIDATES, "字幕固定偏移兜底 alass")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    ffmpeg = Path(legacy.resolve_config_path(r"toolsfmpeginfmpeg.exe"))
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    env = os.environ.copy()
    env["ALASS_FFMPEG_PATH"] = str(ffmpeg)
    env["ALASS_FFPROBE_PATH"] = str(ffprobe)
    log("原严格策略未能确认字幕，启动 alass fixed-only 保守兜底。")
    legacy.run_command(
        [
            alass,
            input_path,
            str(subtitle_path),
            str(output_path),
            "--no-split",
            "--disable-fps-guessing",
            "--allow-negative-timestamps",
        ],
        log=log,
        cancel_event=cancel,
        env=env,
    )
    if not output_path.is_file():
        raise RuntimeError("alass 未生成字幕输出。")
    offset = _subtitle_offset_milliseconds(subtitle_path, output_path)
    if abs(offset) > 300_000:
        raise RuntimeError(f"alass 返回异常大偏移 {offset / 1000:+.2f} 秒。")
    log(f"alass fixed-only 计算完成：固定偏移 {offset / 1000:+.2f} 秒；开始独立台词抽检。")
    return output_path, offset


def _timelines_share_alignment(
    anchor_events: list[legacy.SubtitleEvent],
    candidate_events: list[legacy.SubtitleEvent],
) -> bool:
    if len(anchor_events) < 20 or len(candidate_events) < 20:
        return False
    anchor_starts = [_subtitle_time_seconds(event.start) for event in anchor_events]
    candidate_starts = [_subtitle_time_seconds(event.start) for event in candidate_events]
    step = max(1, len(candidate_starts) // 120)
    distances = [
        min(abs(start - anchor_start) for anchor_start in anchor_starts)
        for start in candidate_starts[::step]
    ]
    if not distances:
        return False
    close_ratio = sum(distance <= 1.5 for distance in distances) / len(distances)
    return close_ratio >= 0.55 and statistics.median(distances) <= 0.8


def _shifted_srt(source: Path, destination: Path, offset_milliseconds: int) -> Path:
    events = legacy.parse_subtitle(source)
    shifted = []
    for event in events:
        start = max(0, int(round(_subtitle_time_seconds(event.start) * 1000)) + offset_milliseconds)
        end = max(start + 1, int(round(_subtitle_time_seconds(event.end) * 1000)) + offset_milliseconds)
        shifted.append(
            legacy.SubtitleEvent(
                legacy.srt_time_from_milliseconds(start),
                legacy.srt_time_from_milliseconds(end),
                event.text,
            )
        )
    legacy.write_srt(
        destination,
        shifted,
        {index: event.text for index, event in enumerate(shifted, 1)},
    )
    return destination


def _timeline_report_verified(report: str) -> bool:
    """Recognize a passed timeline precheck, not final candidate acceptance."""
    return report.startswith(
        (
            "连续VAD确认固定偏移",
            "连续VAD时间轴核验通过：提议固定偏移",
            "已采用连续VAD固定偏移",
            "逐词音频证据确认固定偏移",
            "逐词证据确认原时间轴更可靠",
        )
    )


def _legacy_whisper_align_embedded_text_track(
    input_path: str,
    normalized_subtitle: Path,
    work_dir: Path,
    selected_audio_id: int | None,
    source_language: str,
    track_id: int,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    shared_audio_reference: str | Path | None = None,
    shared_content_audio: SharedSubtitleContentAudio | None = None,
) -> tuple[Path, str]:
    work_dir.mkdir(parents=True, exist_ok=True)
    video = Path(input_path)
    video_stat = video.stat() if video.exists() else None
    import continuous_vad

    signature = {
        "video_size": video_stat.st_size if video_stat is not None else -1,
        "video_mtime_ns": video_stat.st_mtime_ns if video_stat is not None else -1,
        "subtitle_sha256": hashlib.sha256(normalized_subtitle.read_bytes()).hexdigest(),
        "selected_audio_id": selected_audio_id,
        "source_language": normalize_language_code(source_language),
        "track_id": track_id,
        "max_offset_seconds": MAX_AUTOMATIC_OFFSET_SECONDS,
        "format": "embedded-text-anchor-v5-terminal-verdict-only",
        "guard_rule_version": subtitle_offset_guard.GUARD_RULE_VERSION,
        "alignment_rule_version": continuous_vad.RULE_VERSION,
    }
    cache_meta = work_dir / "verified-anchor.json"
    cache_subtitle = work_dir / "verified-anchor.srt"
    try:
        cached = json.loads(cache_meta.read_text(encoding="utf-8"))
        if cached.get("signature") == signature and cache_subtitle.is_file():
            report = str(cached.get("report", "复用内嵌文本时间轴核验结果"))
            cache_label = "已验证时间锚" if _timeline_report_verified(report) else "未通过的时间轴核验结果"
            log(f"原字幕轨 {track_id} 复用{cache_label}；{report}")
            return cache_subtitle, report
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass

    def cache_result(path: Path, report: str) -> tuple[Path, str]:
        if not _timeline_report_verified(report):
            # Incomplete or timed-out evidence is not a durable rejection.
            return path, report
        if path.resolve() != cache_subtitle.resolve():
            shutil.copy2(path, cache_subtitle)
        cache_meta.write_text(
            json.dumps({"signature": signature, "report": report}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path, report

    events = legacy.parse_subtitle(normalized_subtitle)
    if not events:
        raise RuntimeError(f"原字幕轨 {track_id} 没有可识别的正文和时间轴。")
    media = legacy.inspect_media(input_path)
    duration_ns = legacy.video_track_duration_ns(media) or legacy.media_duration_ns(media)
    duration = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0
    subtitle_end = max((_subtitle_time_seconds(event.end) for event in events), default=0.0)
    coverage = subtitle_end / duration if duration > 0 else 0.0
    if duration >= 900 and len(events) < 20:
        raise RuntimeError(f"原字幕轨 {track_id} 仅有 {len(events)} 条，疑似片段或强制字幕。")
    if duration > 0 and coverage < 0.55:
        raise RuntimeError(f"原字幕轨 {track_id} 只覆盖影片约 {coverage:.0%}，不能自动纠偏。")
    if duration > 0 and subtitle_end > duration + max(180.0, duration * 0.12):
        raise RuntimeError(f"原字幕轨 {track_id} 时间轴明显长于影片，不能自动纠偏。")

    validation_audio_id = _preferred_validation_audio_id(
        input_path,
        selected_audio_id,
        source_language,
    )
    try:
        content_audio = shared_content_audio or prepare_shared_subtitle_content_audio(
            input_path,
            validation_audio_id,
            work_dir / "word-timing-fingerprint",
            log,
            cancel,
            source_language=normalize_language_code(source_language),
            time_budget_seconds=60.0,
        )
    except legacy.CancelledError:
        raise
    except Exception as exc:
        report = f"逐词音频证据未能建立，保留原时间轴：{exc}"
        log(f"原字幕轨 {track_id} {report}")
        return cache_result(normalized_subtitle, report)

    result = audio_offset_verifier.match_subtitle_to_fingerprint(
        normalized_subtitle,
        content_audio.fingerprint,
        duration_seconds=duration,
        temporal_corroboration=(content_audio.audio_language != normalize_language_code(source_language)),
    )
    if (
        not result.accepted
        or result.offset_seconds is None
        or result.affine
        or abs(result.offset_seconds) > MAX_AUTOMATIC_OFFSET_SECONDS
    ):
        report = "没有形成可安全应用的固定音频偏移；保留原时间轴，但不能证明原轴正确"
        log(f"原字幕轨 {track_id} {report}；{result.reason}")
        return cache_result(normalized_subtitle, report)

    proposed_offset = float(result.offset_seconds)
    guard = subtitle_offset_guard.evaluate_proposed_offset(
        normalized_subtitle,
        content_audio.fingerprint,
        proposed_offset,
    )
    log(f"原字幕轨 {track_id} 逐词偏移验真：{subtitle_offset_guard.describe(guard)}。")
    if (
        guard.status == "NEED_MORE"
        and normalize_language_code(source_language) == "en"
        and normalize_language_code(content_audio.audio_language) == "en"
    ):
        # The common probes may miss ordinary dialogue even when they reveal
        # one plausible offset. Reuse the online candidate's preselected
        # three-region backup pool, but keep this embedded-only supplement
        # bounded and never treat one region as a positive verdict.
        targeted_cancel = _DeadlineCancel(cancel, time.monotonic() + 20.0)
        try:
            guard, used = _targeted_candidate_offset_guard(
                input_path,
                normalized_subtitle,
                events,
                work_dir=work_dir / "targeted-dialogue",
                shared_content_audio=content_audio,
                provisional_offset=proposed_offset,
                candidate_label=f"原字幕轨 {track_id}",
                log=log,
                cancel=targeted_cancel,
            )
            log(f"原字幕轨 {track_id} 分区补查实际取证 {used} 处。")
        except legacy.CancelledError:
            if not targeted_cancel.timed_out:
                raise
            log(f"原字幕轨 {track_id} 分区补查达到 20 秒上限；证据仍不足，保持原时间轴。")
        except SubtitleContentMismatchError as exc:
            log(f"原字幕轨 {track_id} 没有足够的适合补查的分散对白：{exc}。")
        except Exception as exc:
            log(f"原字幕轨 {track_id} 分区补查未完成：{exc}；不据此应用偏移。")
    if not guard.eligible or guard.selected_offset is None:
        report = f"建议偏移未获逐词证据支持，保留原时间轴但不能证明原轴正确；{guard.reason}"
        return cache_result(normalized_subtitle, report)

    selected_offset = float(guard.selected_offset)
    if abs(selected_offset) <= 0.25:
        report = f"逐词证据确认原时间轴更可靠；{guard.reason}"
        return cache_result(normalized_subtitle, report)

    corrected = _shifted_srt(
        normalized_subtitle,
        work_dir / "embedded-word-verified.srt",
        int(round(selected_offset * 1000)),
    )
    report = (
        f"逐词音频证据确认固定偏移 {selected_offset:+.2f} 秒；"
        f"{guard.reason}"
    )
    return cache_result(corrected, report)


@dataclass(frozen=True)
class PreparedEmbeddedAnchor:
    """An in-memory handoff from an actual, completed embedded VAD check."""
    movie_path: str
    movie_size: int
    movie_mtime_ns: int
    alignment_rule_version: str
    validation_audio_id: int
    track_id: int
    track_language: str
    source_subtitle_path: Path
    source_sha256: str
    corrected_subtitle_path: Path
    corrected_sha256: str
    offset_ms: int
    report: str


@dataclass(frozen=True)
class PreparedImageTiming:
    """A same-run result issued only after all required image tracks passed."""
    movie_path: str
    movie_size: int
    movie_mtime_ns: int
    rule_version: str
    audio_signature: tuple[int, int, str, str]
    subtitle_signature: tuple[tuple[int, int, str, str, bool, bool, bool], ...]
    image_track_ids: tuple[int, ...]
    aligned_subtitle_sha256: str
    offsets: tuple[tuple[int, int, str], ...]
    nonce: str = field(repr=False, compare=False)


_IMAGE_TIMING_HANDOFF_RULE_VERSION = "image-fixed-local-2-4-8-bounded10-full-v2"
_prepared_image_timings = weakref.WeakValueDictionary()
_prepared_image_timings_lock = threading.Lock()


def _prepared_image_rule_version() -> str:
    import continuous_vad

    return (f"{continuous_vad.RULE_VERSION}:{_IMAGE_TIMING_HANDOFF_RULE_VERSION}:"
            f"{MAX_AUTOMATIC_OFFSET_SECONDS:g}:{pgs_local_alignment.MAX_OFFSET_SECONDS:g}")


def _prepared_image_subtitle_signature(tracks):
    return tuple((track.id, ordinal, str(track.codec), normalize_language_code(track.language),
                  bool(track.text_subtitle), bool(track.pgs_subtitle), bool(track.vobsub_subtitle))
                 for ordinal, track in enumerate(track for track in tracks if track.type == "subtitles"))


def _prepared_image_audio_signature(tracks, selected_audio_id, shared_content_audio):
    actual_id = _prepared_anchor_audio_id(tracks, selected_audio_id, shared_content_audio)
    audio_tracks = [track for track in tracks if track.type == "audio"]
    track = next(track for track in audio_tracks if track.id == actual_id)
    return (track.id, audio_tracks.index(track), str(track.codec), normalize_language_code(track.language))


def _issued_prepared_image_timing(prepared) -> bool:
    if not isinstance(prepared, PreparedImageTiming):
        return False
    with _prepared_image_timings_lock:
        return _prepared_image_timings.get(prepared.nonce) is prepared


def prepared_image_timing_matches_subtitle(prepared, aligned) -> bool:
    """Bind the candidate callback's result to the subtitle finally adopted."""
    if not _issued_prepared_image_timing(prepared):
        return False
    try:
        return hashlib.sha256(Path(aligned).read_bytes()).hexdigest() == prepared.aligned_subtitle_sha256
    except (OSError, TypeError, ValueError):
        return False


def _prepared_image_context(input_path, track_ids, selected_audio_id, shared_content_audio, aligned):
    ids = tuple(sorted(set(track_ids)))
    tracks = legacy.inspect_tracks(input_path)
    available = {track.id for track in tracks if track.type == "subtitles"
                 and (track.pgs_subtitle or track.vobsub_subtitle)}
    if not ids or not set(ids).issubset(available):
        raise ValueError("找不到需保留的图片字幕轨")
    movie = Path(input_path).resolve()
    stat = movie.stat()
    return (str(movie).casefold(), stat.st_size, stat.st_mtime_ns,
            _prepared_image_rule_version(),
            _prepared_image_audio_signature(tracks, selected_audio_id, shared_content_audio),
            _prepared_image_subtitle_signature(tracks), ids,
            hashlib.sha256(Path(aligned).read_bytes()).hexdigest())


def _make_prepared_image_timing(input_path, track_ids, selected_audio_id,
        shared_content_audio, aligned, results, initial_context):
    try:
        ids = tuple(sorted(set(track_ids)))
        if (initial_context is None or not ids
                or any(not results.get(track_id, {}).get("accepted") for track_id in ids)):
            return None
        current_context = _prepared_image_context(
            input_path, track_ids, selected_audio_id, shared_content_audio, aligned,
        )
        if current_context != initial_context:
            return None
        offsets = tuple((track_id, int(results[track_id]["offset_ms"]),
                         str(results[track_id].get("method", "pgs-full-fixed-v1")))
                        for track_id in ids)
        if any(abs(offset) > MAX_AUTOMATIC_OFFSET_SECONDS * 1000 for _, offset, _ in offsets):
            return None
        prepared = PreparedImageTiming(*current_context, offsets, uuid.uuid4().hex)
        with _prepared_image_timings_lock:
            _prepared_image_timings[prepared.nonce] = prepared
        return prepared
    except (OSError, KeyError, StopIteration, TypeError, ValueError, RuntimeError):
        # A failed optimization must not change the candidate's actual verdict.
        return None


def _validated_prepared_image_timing(prepared, input_path, tracks, image_tracks,
        selected_audio_id, shared_content_audio, aligned):
    if not _issued_prepared_image_timing(prepared):
        raise ValueError("缺少本轮已完成的图片字幕核验结果")
    movie = Path(input_path).resolve()
    stat = movie.stat()
    if (prepared.movie_path != str(movie).casefold()
            or (prepared.movie_size, prepared.movie_mtime_ns) != (stat.st_size, stat.st_mtime_ns)
            or prepared.rule_version != _prepared_image_rule_version()):
        raise ValueError("影片或图片字幕核验规则在候选验收后已变化")
    if (prepared.subtitle_signature != _prepared_image_subtitle_signature(tracks)
            or prepared.image_track_ids != tuple(sorted(track.id for track in image_tracks))):
        raise ValueError("保留的图片字幕轨或容器字幕轨序已变化")
    if prepared.audio_signature != _prepared_image_audio_signature(tracks, selected_audio_id, shared_content_audio):
        raise ValueError("当前取证音轨与候选验收时不一致")
    if not prepared_image_timing_matches_subtitle(prepared, aligned):
        raise ValueError("已纠偏文本标杆在候选验收后已变化")
    return prepared


def _prepared_anchor_audio_id(tracks, selected_audio_id, shared_content_audio) -> int:
    import continuous_vad

    actual = continuous_vad.audio_track(tracks, selected_audio_id).id
    if shared_content_audio is not None and shared_content_audio.validation_audio_id != actual:
        raise ValueError("共用音频与当前取证音轨不一致")
    return actual


def _prepared_anchor_events(source: Path, corrected: Path, duration: float, offset_ms: int):
    source_events = legacy.parse_subtitle(source)
    corrected_events = legacy.parse_subtitle(corrected)
    if (not subtitle_completeness(source_events, duration).accepted
            or len(source_events) != len(corrected_events)):
        raise ValueError("内嵌文本标杆已不完整")
    if abs(offset_ms) > MAX_AUTOMATIC_OFFSET_SECONDS * 1000:
        raise ValueError("内嵌标杆偏移超出自动范围")
    for before, after in zip(source_events, corrected_events):
        if before.text != after.text:
            raise ValueError("内嵌标杆正文或顺序已变化")
        for name in ("start", "end"):
            previous = round(_subtitle_time_seconds(getattr(before, name)) * 1000)
            current = round(_subtitle_time_seconds(getattr(after, name)) * 1000)
            if current - previous != offset_ms:
                raise ValueError("内嵌标杆时间轴不是已核验的固定平移")
    return corrected_events


def _make_prepared_embedded_anchor(input_path, track, selected_audio_id,
        shared_content_audio, source, corrected, duration, offset_ms, report):
    import continuous_vad

    adopted = continuous_vad.adopted_alignment_report(report)
    if not adopted and report.startswith("已采用连续VAD固定偏移"):
        adopted = report
    if not adopted:
        return None
    try:
        source, corrected = Path(source).resolve(), Path(corrected).resolve()
        _prepared_anchor_events(source, corrected, duration, offset_ms)
        movie = Path(input_path).resolve()
        stat = movie.stat()
        tracks = legacy.inspect_tracks(input_path)
        audio_id = _prepared_anchor_audio_id(tracks, selected_audio_id, shared_content_audio)
        return PreparedEmbeddedAnchor(
            str(movie).casefold(), stat.st_size, stat.st_mtime_ns,
            continuous_vad.RULE_VERSION, audio_id, track.id,
            normalize_language_code(track.language), source,
            hashlib.sha256(source.read_bytes()).hexdigest(), corrected,
            hashlib.sha256(corrected.read_bytes()).hexdigest(), int(offset_ms), adopted,
        )
    except (OSError, ValueError, RuntimeError):
        # Failure to create an optimization must not override a real verifier
        # result. The formal stage simply performs its existing current checks.
        return None


def _validated_prepared_embedded_anchor(prepared, input_path, tracks,
        embedded_source_id, selected_audio_id, shared_content_audio, duration):
    import continuous_vad

    if not isinstance(prepared, PreparedEmbeddedAnchor):
        raise ValueError("缺少有来源校验的内嵌标杆")
    movie = Path(input_path).resolve()
    stat = movie.stat()
    if (prepared.movie_path != str(movie).casefold()
            or (prepared.movie_size, prepared.movie_mtime_ns) != (stat.st_size, stat.st_mtime_ns)
            or prepared.alignment_rule_version != continuous_vad.RULE_VERSION):
        raise ValueError("影片或纠偏规则在预检后已变化")
    track = next((track for track in tracks if track.id == prepared.track_id
                  and track.type == "subtitles" and track.text_subtitle), None)
    if (track is None or track.id != embedded_source_id
            or normalize_language_code(track.language) != prepared.track_language):
        raise ValueError("当前文本来源与预检标杆不一致")
    if _prepared_anchor_audio_id(tracks, selected_audio_id, shared_content_audio) != prepared.validation_audio_id:
        raise ValueError("当前取证音轨与预检标杆不一致")
    if (hashlib.sha256(prepared.source_subtitle_path.read_bytes()).hexdigest() != prepared.source_sha256
            or hashlib.sha256(prepared.corrected_subtitle_path.read_bytes()).hexdigest() != prepared.corrected_sha256):
        raise ValueError("内嵌标杆文件在预检后已变化")
    if not prepared.report.startswith("已采用连续VAD固定偏移"):
        raise ValueError("内嵌标杆缺少已完成的核验结果")
    _prepared_anchor_events(prepared.source_subtitle_path, prepared.corrected_subtitle_path,
                            duration, prepared.offset_ms)
    return prepared


def prepare_embedded_text_corrections(
    input_path: str,
    keep_subtitle_ids: list[int],
    embedded_source_id: int | None,
    selected_audio_id: int | None,
    work_dir: str,
    log: Callable[[str], None],
    cancel: threading.Event | None = None,
    include_source_alternatives: bool = False,
    verified_external_subtitle: str | None = None,
    shared_audio_reference: str | Path | None = None,
    shared_content_audio: SharedSubtitleContentAudio | None = None,
    verification_status: dict[str, object] | None = None,
    prepared_embedded_anchor: PreparedEmbeddedAnchor | None = None,
    prepared_image_timing: PreparedImageTiming | None = None,
) -> tuple[dict[int, int], dict[int, Path], int | None]:
    legacy.check_cancel(cancel)
    tracks = legacy.inspect_tracks(input_path)
    media = None
    duration = 0.0
    prepared = None
    external_reference = Path(verified_external_subtitle or "")
    if prepared_embedded_anchor is not None and not external_reference.is_file():
        try:
            media = legacy.inspect_media(input_path)
            duration_ns = legacy.video_track_duration_ns(media) or legacy.media_duration_ns(media)
            duration = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0
            prepared = _validated_prepared_embedded_anchor(
                prepared_embedded_anchor, input_path, tracks, embedded_source_id,
                selected_audio_id, shared_content_audio, duration,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            log(f"内嵌标杆预检结果不能复用，重新核验并检查备选字幕：{exc}。")
    wanted_ids = set(keep_subtitle_ids)
    if embedded_source_id is not None:
        wanted_ids.add(embedded_source_id)
    source_track = next((track for track in tracks if track.id == embedded_source_id), None)
    source_language = (
        normalize_language_code(source_track.language)
        if source_track is not None
        else "und"
    )
    if include_source_alternatives and prepared is None and source_track is not None and source_track.text_subtitle:
        wanted_ids.update(
            track.id for track in tracks
            if (
                track.type == "subtitles"
                and track.text_subtitle
                and normalize_language_code(track.language) == source_language
                and not track.forced
                and not any(
                    marker in (track.name or "").lower()
                    for marker in ("forced", "commentary", "sign", "song")
                )
            )
        )
    text_tracks = [
        track for track in tracks
        if track.type == "subtitles" and track.text_subtitle and track.id in wanted_ids
    ]
    image_tracks = [
        track for track in tracks
        if (
            track.type == "subtitles"
            and (track.pgs_subtitle or track.vobsub_subtitle)
            and track.id in wanted_ids
        )
    ]
    if not text_tracks and not image_tracks:
        return {}, {}, embedded_source_id

    text_tracks.sort(
        key=lambda track: (
            0 if normalize_language_code(track.language) == "en" else 1,
            0 if track.id == embedded_source_id else 1,
            0 if track.default else 1,
            track.id,
        )
    )
    work = Path(work_dir) / "embedded-subtitle-correction"
    work.mkdir(parents=True, exist_ok=True)
    extraction_input = legacy.prepare_work_input(
        input_path,
        work / "normalized-input",
        log,
        cancel_event=cancel,
    )
    if str(Path(extraction_input).resolve()) == str(Path(input_path).resolve()):
        extraction_track_ids = {track.id: track.id for track in tracks}
        extraction_tracks = {track.id: track for track in tracks}
    else:
        normalized_tracks = legacy.inspect_tracks(extraction_input)
        extraction_track_ids = legacy.map_normalized_track_ids(tracks, normalized_tracks)
        extraction_tracks = {track.id: track for track in normalized_tracks}
    extracted: dict[int, Path] = {}
    normalized: dict[int, Path] = {}
    extraction_specs: list[str] = []
    for track in text_tracks:
        if prepared is not None and track.id == prepared.track_id:
            normalized[track.id] = prepared.source_subtitle_path
            continue
        track_work = work / f"track-{track.id}"
        track_work.mkdir(parents=True, exist_ok=True)
        extraction_track_id = extraction_track_ids.get(track.id)
        extraction_track = extraction_tracks.get(extraction_track_id)
        if extraction_track is None:
            raise RuntimeError(f"无法定位待校正的原字幕轨 {track.id}。")
        extracted_path = (
            track_work
            / f"track-{extraction_track.id}-{extraction_track.language}"
            f"{legacy.subtitle_extension(extraction_track)}"
        )
        extracted[track.id] = extracted_path
        if not extracted_path.exists() or extracted_path.stat().st_size <= 0:
            extraction_specs.append(f"{extraction_track.id}:{extracted_path}")

    if extraction_specs:
        log(f"一次读取 {len(extraction_specs)} 条候选文本字幕轨，避免重复扫描影片。")
        legacy.run_command(
            [legacy.MKVEXTRACT, "tracks", extraction_input, *extraction_specs],
            log=log,
            cancel_event=cancel,
        )

    for track in text_tracks:
        if prepared is not None and track.id == prepared.track_id:
            continue
        track_work = work / f"track-{track.id}"
        normalized[track.id] = subtitle_events_to_srt(
            extracted[track.id],
            track_work / "original-normalized.srt",
        )

    if media is None:
        media = legacy.inspect_media(input_path)
        duration_ns = legacy.video_track_duration_ns(media) or legacy.media_duration_ns(media)
        duration = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0
    resolved_source_id = embedded_source_id
    offsets: dict[int, int] = {}
    corrected_sources: dict[int, Path] = {}
    anchor_events: list[legacy.SubtitleEvent] = []
    anchor_offset: int | None = None
    verified_reference_events: list[legacy.SubtitleEvent] = []
    if external_reference.is_file():
        verified_reference_events = legacy.parse_subtitle(external_reference)
    if verification_status is not None:
        verification_status.clear()
        verification_status["verified_anchor"] = bool(verified_reference_events)

    if text_tracks and verified_reference_events:
        # A selected and confirmed download is the absolute timeline anchor.
        # Embedded text tracks must be compared to that corrected timeline;
        # independently aligning the first embedded track would overwrite it.
        reference_intervals = [
            (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
            for event in verified_reference_events
        ]
        for track in text_tracks:
            candidate_events = legacy.parse_subtitle(normalized[track.id])
            candidate_intervals = [
                (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
                for event in candidate_events
            ]
            score, offset_seconds = _interval_alignment(
                reference_intervals, candidate_intervals
            )
            if score >= 0.85 and abs(offset_seconds) <= MAX_AUTOMATIC_OFFSET_SECONDS:
                offset = 0 if abs(offset_seconds) <= 0.25 else int(round(offset_seconds * 1000))
                offsets[track.id] = offset
                corrected_sources[track.id] = (
                    _shifted_srt(
                        normalized[track.id],
                        work / f"track-{track.id}" / "corrected-from-download.srt",
                        offset,
                    )
                    if offset else normalized[track.id]
                )
                log(
                    f"原文本字幕轨 {track.id} 与已确认下载字幕时间轴比较："
                    f"相对偏移 {offset / 1000:+.2f} 秒，相关度 {score:.0%}。"
                )
            else:
                offsets[track.id] = 0
                corrected_sources[track.id] = normalized[track.id]
                log(
                    f"原文本字幕轨 {track.id} 无法与已确认下载字幕形成可靠固定偏移"
                    f"（相关度 {score:.0%}、建议 {offset_seconds:+.2f} 秒）；"
                    "保持原时间轴，不另选内嵌轨覆盖下载标杆。"
                )
    elif text_tracks:
        resolved_source_id = (
            choose_complete_subtitle_source(
                text_tracks,
                normalized,
                embedded_source_id,
                duration,
                log,
            )
            if include_source_alternatives and prepared is None and source_track is not None and source_track.text_subtitle
            else embedded_source_id
        )
        align_ids = set(keep_subtitle_ids)
        if resolved_source_id is not None:
            align_ids.add(resolved_source_id)
        if resolved_source_id != embedded_source_id:
            align_ids.discard(embedded_source_id)
        text_tracks = [track for track in text_tracks if track.id in align_ids]
        text_tracks.sort(
            key=lambda track: (
                0 if track.id == resolved_source_id else 1,
                0 if normalize_language_code(track.language) == "en" else 1,
                0 if track.default else 1,
                track.id,
            )
        )

        if text_tracks:
            anchor = text_tracks[0]
            if prepared is not None and anchor.id == prepared.track_id:
                anchor_verified, anchor_report = prepared.corrected_subtitle_path, prepared.report
                log(f"复用预检已采用的内嵌文本标杆轨 {anchor.id}；不重复提取或音频对时。")
            else:
                anchor_verified, anchor_report = align_embedded_text_track(
                    input_path,
                    normalized[anchor.id],
                    work / f"track-{anchor.id}" / "verification",
                    selected_audio_id,
                    anchor.language,
                    anchor.id,
                    log,
                    cancel,
                    shared_audio_reference,
                    shared_content_audio,
                )
            anchor_is_verified = _timeline_report_verified(anchor_report)
            if anchor_is_verified:
                import continuous_vad

                anchor_report = continuous_vad.adopted_alignment_report(anchor_report) or anchor_report
            if not anchor_is_verified:
                anchor_verified = normalized[anchor.id]
            anchor_offset = _subtitle_offset_milliseconds(normalized[anchor.id], anchor_verified)
            offsets[anchor.id] = anchor_offset
            corrected_sources[anchor.id] = anchor_verified
            if anchor_is_verified:
                verified_reference_events = legacy.parse_subtitle(anchor_verified)
                if verification_status is not None:
                    verification_status["verified_anchor"] = True
                    verification_status["track_id"] = anchor.id
                    verification_status["report"] = anchor_report
                    handoff = prepared or _make_prepared_embedded_anchor(
                        input_path, anchor, selected_audio_id, shared_content_audio,
                        normalized[anchor.id], anchor_verified, duration, anchor_offset, anchor_report,
                    )
                    if handoff is not None:
                        verification_status["prepared_anchor"] = handoff
            if anchor_is_verified and abs(anchor_offset) >= 50:
                log(
                    f"原文本字幕轨 {anchor.id} 已通过音轨自动纠偏："
                    f"{anchor_offset / 1000:+.2f} 秒；{anchor_report}"
                )
            elif anchor_is_verified:
                log(f"原文本字幕轨 {anchor.id} 时间轴正常；{anchor_report}")
            else:
                log(
                    f"原文本字幕轨 {anchor.id} 未获足够音频证据，保留原时间轴；"
                    "不能作为其他字幕的纠偏标杆；"
                    f"{anchor_report}"
                )

            for track in text_tracks[1:]:
                candidate_events = legacy.parse_subtitle(normalized[track.id])
                track_work = work / f"track-{track.id}"
                reference_intervals = [
                    (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
                    for event in verified_reference_events
                ]
                candidate_intervals = [
                    (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
                    for event in candidate_events
                ]
                score, offset_seconds = _interval_alignment(reference_intervals, candidate_intervals)
                if score >= 0.85 and abs(offset_seconds) <= MAX_AUTOMATIC_OFFSET_SECONDS:
                    offset = int(round(offset_seconds * 1000))
                    offsets[track.id] = offset
                    corrected_sources[track.id] = _shifted_srt(
                        normalized[track.id],
                        track_work / "corrected-from-anchor.srt",
                        offset,
                    )
                    log(
                        f"原文本字幕轨 {track.id} 已单独与可靠文本标杆比较，"
                        f"计算偏移 {offset / 1000:+.2f} 秒，相关度 {score:.0%}。"
                    )
                    continue
                verified, report = align_embedded_text_track(
                    input_path,
                    normalized[track.id],
                    track_work / "verification",
                    selected_audio_id,
                    track.language,
                    track.id,
                    log,
                    cancel,
                    shared_audio_reference,
                    shared_content_audio,
                )
                track_is_verified = _timeline_report_verified(report)
                if track_is_verified:
                    import continuous_vad

                    report = continuous_vad.adopted_alignment_report(report) or report
                if not track_is_verified:
                    verified = normalized[track.id]
                offset = _subtitle_offset_milliseconds(normalized[track.id], verified)
                offsets[track.id] = offset
                corrected_sources[track.id] = verified
                if track_is_verified and not verified_reference_events:
                    verified_reference_events = legacy.parse_subtitle(verified)
                    if verification_status is not None:
                        verification_status["verified_anchor"] = True
                        verification_status["track_id"] = track.id
                        verification_status["report"] = report
                if track_is_verified and abs(offset) >= 50:
                    log(f"原文本字幕轨 {track.id} 已单独自动纠偏：{offset / 1000:+.2f} 秒；{report}")
                elif track_is_verified:
                    log(f"原文本字幕轨 {track.id} 时间轴正常；{report}")
                else:
                    log(
                        f"原文本字幕轨 {track.id} 未获足够音频证据，保留原时间轴；"
                        f"{report}"
                    )

    subtitle_tracks = [track for track in tracks if track.type == "subtitles"]
    image_cache = work / "image-timing-cache"
    reference_intervals = [
        (_subtitle_time_seconds(event.start), _subtitle_time_seconds(event.end))
        for event in verified_reference_events
    ]
    image_results = {}
    if verification_status is not None:
        verification_status['image_results'] = image_results
    prepared_images = None
    if prepared_image_timing is not None:
        try:
            prepared_images = _validated_prepared_image_timing(
                prepared_image_timing, input_path, tracks, image_tracks,
                selected_audio_id, shared_content_audio, external_reference,
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            log(f"图片字幕候选验收结果不能复用，按原有流程重新核验：{exc}。")
    legacy.check_cancel(cancel)
    prepared_offsets = ({track_id: (offset, method)
                         for track_id, offset, method in prepared_images.offsets}
                        if prepared_images is not None else {})
    if prepared_images is not None:
        log(f"复用本轮已采用文本标杆的图片字幕核验结果，共 {len(prepared_offsets)} 轨；"
            "不重复读取或比较图片字幕时间点。")
    local_results = {}
    local_indices = [subtitle_tracks.index(track) for track in image_tracks if track.pgs_subtitle]
    if prepared_images is None and local_indices and len(reference_intervals) >= 20:
        try:
            local_results = _pgs_local_corrections(
                input_path, local_indices, reference_intervals, duration, log, cancel, image_cache,
            )
        except legacy.CancelledError:
            raise
        except Exception as exc:
            log(f"图片字幕局部时间点检查未完成，未通过的轨道转原有全片检查：{exc}")
    for track in image_tracks:
        legacy.check_cancel(cancel)
        if track.id in prepared_offsets:
            offset, method = prepared_offsets[track.id]
            offsets[track.id] = offset
            image_results[track.id] = {
                'accepted': True, 'offset_ms': offset, 'method': method,
                'reason': '复用本轮针对同一已纠偏文本的候选验收结果',
            }
            continue
        image_results[track.id] = {'accepted': False, 'reason': '没有可用文本标杆'}
        if len(reference_intervals) < 20:
            log(
                f"原图片字幕轨 {track.id} 没有可用的已验证文本标杆；"
                "不扫描整片图片字幕时间点，保持原时间轴。"
            )
            offsets[track.id] = 0
            continue
        stream_index = subtitle_tracks.index(track)
        local = local_results.get(stream_index)
        if local is not None:
            image_results[track.id]['local_evidence'] = asdict(local)
        if local is not None and local.accepted:
            offset = int(round(local.offset_seconds * 1000))
            offsets[track.id] = offset
            image_results[track.id].update({
                'accepted': True, 'offset_ms': offset, 'method': 'pgs-local-fixed-v1',
                'reason': '三处共同固定偏移与同范围时间点检查通过',
                'proposed_offset_seconds': local.proposed_offset_seconds,
            })
            log(f"原图片字幕轨 {track.id} 局部检查通过，"
                f"{'保持原时间轴' if offset == 0 else f'套用偏移 {offset / 1000:+.2f} 秒'}。")
            continue
        if track.pgs_subtitle:
            if _has_current_full_image_cache(input_path, image_cache):
                log(f"原图片字幕轨 {track.id} 直接复用已有全片时间点进行比较。")
            else:
                log(f"原图片字幕轨 {track.id} 局部证据不足，继续原有全片检查。")
        if (getattr(cancel, 'candidate_match_clock', False)
                and not _has_current_full_image_cache(input_path, image_cache)):
            legacy.check_cancel(cancel)
            raise SharedImageTimingPreparationRequired(input_path, stream_index, image_cache, duration)
        try:
            intervals = _embedded_image_intervals(
                input_path,
                stream_index,
                log,
                cancel,
                image_cache,
            )
        except legacy.CancelledError:
            raise
        except Exception as exc:
            log(
                f"原图片字幕轨 {track.id} 时间点读取失败，无法自动纠偏；"
                f"保留原时间轴并继续处理：{exc}"
            )
            image_results[track.id]['reason'] = f'时间点读取失败：{exc}'
            offsets[track.id] = 0
            continue
        if len(intervals) < 20:
            image_results[track.id]['reason'] = f'有效显示区间不足：{len(intervals)}'
            log(
                f"原图片字幕轨 {track.id} 仅提取到 {len(intervals)} 个有效显示区间；"
                "证据不足以纠偏，保留原时间轴并继续处理。"
            )
            offsets[track.id] = 0
            continue
        offset: int | None = None
        if len(verified_reference_events) >= 20:
            score, offset_seconds = _interval_alignment(reference_intervals, intervals)
            image_results[track.id].update(score=score, proposed_offset_seconds=offset_seconds,
                reason=f'相关度{score:.1%}，相对偏移{offset_seconds:+.2f}秒，不满足现有图片字幕核验条件')
            if score >= 0.55 and abs(offset_seconds) <= MAX_AUTOMATIC_OFFSET_SECONDS:
                # Match the existing 250 ms no-op rule used for verified text:
                # sub-frame alignment noise should not rewrite a kept track.
                offset = 0 if abs(offset_seconds) <= 0.25 else int(round(offset_seconds * 1000))
                if offset:
                    log(
                        f"原图片字幕轨 {track.id} 已与验证通过的文本时间轴匹配，"
                        f"套用偏移 {offset / 1000:+.2f} 秒，相关度 {score:.0%}。"
                    )
                else:
                    log(
                        f"原图片字幕轨 {track.id} 与验证通过的文本时间轴一致，"
                        f"保持原时间轴，相关度 {score:.0%}。"
                    )
        if offset is None:
            log(
                f"原图片字幕轨 {track.id} 未能与已验证的文本时间锚形成可靠的"
                f"±{MAX_AUTOMATIC_OFFSET_SECONDS:g} 秒内固定偏移；"
                f"{image_results[track.id]['reason']}；本轨尚未完成纠偏。"
            )
            offset = 0
        else:
            image_results[track.id].update(accepted=True, offset_ms=offset, reason='与已纠偏文本时间轴匹配')
        offsets[track.id] = offset
    return offsets, corrected_sources, resolved_source_id



def prepare_shared_image_timing(input_path, track_ids, work_dir, log, cancel=None):
    """Validate retained tracks; defer packet IO until a corrected text exists."""
    legacy.check_cancel(cancel)
    if not track_ids:
        return
    tracks = [track for track in legacy.inspect_tracks(input_path) if track.type == 'subtitles']
    available = {track.id for track in tracks}
    if not set(track_ids).issubset(available):
        raise SubtitleVerificationToolError('找不到需保留的图片字幕轨。')
    log('图片字幕时间点延后至文本纠偏完成再读取；优先局部检查，全部字幕轨共用。')


def require_image_timeline_anchor(input_path, track_ids, audio_id, work_dir, aligned, log,
        cancel=None, prepared_image_status: dict[str, object] | None = None):
    """Check task suitability after audio alignment; PGS never proves absolute sync."""
    if prepared_image_status is not None:
        prepared_image_status.clear()
    legacy.check_cancel(cancel)
    if not track_ids:
        return {}
    initial_context = None
    if prepared_image_status is not None:
        try:
            initial_context = _prepared_image_context(input_path, track_ids, audio_id, None, aligned)
        except (OSError, StopIteration, TypeError, ValueError, RuntimeError):
            pass
    status = {}
    prepare_embedded_text_corrections(
        input_path, track_ids, None, audio_id, str(work_dir), log, cancel,
        verified_external_subtitle=str(aligned), verification_status=status)
    results = status.get('image_results', {})
    unresolved = [f"轨{track_id}：{results.get(track_id, {}).get('reason', '尚未取得核验结果')}"
        for track_id in track_ids if not results.get(track_id, {}).get('accepted')]
    if unresolved:
        raise SubtitleContentMismatchError(
            '本候选已完成音频对时，但不能指导需保留的图片字幕；' + '；'.join(unresolved))
    legacy.check_cancel(cancel)
    if prepared_image_status is not None:
        prepared = _make_prepared_image_timing(
            input_path, track_ids, audio_id, None, aligned, results, initial_context,
        )
        legacy.check_cancel(cancel)
        if prepared is not None:
            prepared_image_status['prepared_image_timing'] = prepared
    log('本候选已完成音频对时，并可指导需保留的图片字幕。')
    return results


def _prepare_local_chinese_conversions(
    input_path: str,
    conversion_sources: dict[str, int] | None,
    work_dir: str,
    corrected_sources: dict[int, Path],
    subtitle_sync_offsets: dict[int, int],
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> list[tuple[Path, str, str, bool]]:
    """Convert an embedded Chinese text track without changing the main translation source."""
    if not conversion_sources:
        return []

    work = Path(work_dir) / "local-chinese-conversion"
    work.mkdir(parents=True, exist_ok=True)
    input_tracks = legacy.inspect_tracks(input_path)
    input_subtitles = {
        track.id: track for track in input_tracks if track.type == "subtitles"
    }
    work_input: str | None = None
    work_subtitles: dict[int, legacy.Track] = {}
    source_to_work_ids: dict[int, int] = {}
    extracted_cache: dict[int, Path] = {}
    generated: list[tuple[Path, str, str, bool]] = []

    for raw_target, source_id in conversion_sources.items():
        legacy.check_cancel(cancel_event)
        target = normalize_language_code(raw_target)
        if target not in chinese_script_converter.CHINESE_SCRIPT_CODES:
            raise RuntimeError(f"不支持的本地中文转换目标：{raw_target}")
        source_code = "zh-TW" if target == "zh-CN" else "zh-CN"
        source_track = input_subtitles.get(int(source_id))
        if source_track is None or not source_track.text_subtitle:
            raise RuntimeError(
                f"用于{legacy.LANGUAGES[target][2]}本地转换的字幕轨 {source_id} 不是可用文本字幕。"
            )

        extracted = corrected_sources.get(int(source_id))
        if extracted is None:
            if int(source_id) in extracted_cache:
                extracted = extracted_cache[int(source_id)]
            else:
                if work_input is None:
                    work_input = legacy.prepare_work_input(
                        input_path,
                        work,
                        log,
                        cancel_event=cancel_event,
                    )
                    normalized_tracks = legacy.inspect_tracks(work_input)
                    source_to_work_ids = (
                        {track.id: track.id for track in input_tracks}
                        if str(Path(work_input).resolve()) == str(Path(input_path).resolve())
                        else legacy.map_normalized_track_ids(input_tracks, normalized_tracks)
                    )
                    work_subtitles = {
                        track.id: track
                        for track in normalized_tracks
                        if track.type == "subtitles"
                    }
                work_source_id = source_to_work_ids.get(int(source_id))
                work_track = work_subtitles.get(work_source_id) if work_source_id is not None else None
                if work_track is None or not work_track.text_subtitle:
                    raise RuntimeError(f"无法定位用于本地简繁转换的字幕轨 {source_id}。")
                extracted = legacy.extract_subtitle(
                    work_input,
                    work_track,
                    work,
                    log,
                    cancel_event=cancel_event,
                )
                offset = int(subtitle_sync_offsets.get(int(source_id), 0) or 0)
                if offset:
                    extracted = legacy.shift_subtitle_timeline(
                        Path(extracted),
                        work / f"track-{source_id}-timeline-corrected.srt",
                        offset,
                    )
                extracted_cache[int(source_id)] = Path(extracted)

        events = legacy.parse_subtitle(Path(extracted))
        if not events:
            raise RuntimeError(f"字幕轨 {source_id} 没有可转换的正文。")
        output = work / f"track-{source_id}-{target}-converted.srt"
        chinese_script_converter.convert_subtitle_events(
            events,
            output,
            source_code,
            target,
        )
        label = legacy.LANGUAGES[target][2]
        log(f"{label}本地简繁转换完成：{len(events)} 行；复用原字幕时间轴，不启动翻译模型。")
        generated.append((output, target, f"{label} Converted", False))
    return generated

def process_tracks_only(
    input_path: str,
    output_path: str,
    keep_audio_ids: list[int],
    keep_subtitle_ids: list[int],
    work_dir: str,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
    preserve_audio_defaults: bool = False,
    subtitle_sync_offsets: dict[int, int] | None = None,
    generated_subtitles: list[tuple[Path, str, str, bool]] | None = None,
    subtitle_language_preferences: list[str] | None = None,
    chinese_script_equivalent: bool = False,
) -> str:
    """Remux selected tracks and any already-generated local text subtitles."""
    legacy.check_cancel(cancel_event)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    output = Path(output_path)
    partial = legacy.partial_output_path(output_path)
    intermediate = work / "tracks-only-before-mp4.mkv"
    legacy.remove_incomplete_file(partial, log)
    if output.suffix.lower() == ".mp4":
        legacy.remove_incomplete_file(intermediate, log)
    legacy.ensure_output_disk_space(input_path, output_path, log)
    input_media = legacy.inspect_media(input_path)
    selected_specs = [
        item for item in input_media.get("tracks", [])
        if item.get("type") == "audio" and int(item.get("id", -1)) in keep_audio_ids
    ]
    generated = list(generated_subtitles or [])
    if generated:
        if subtitle_language_preferences is None:
            generated = [
                (path, code, name, index == 0)
                for index, (path, code, name, _is_default) in enumerate(generated)
            ]
        log("仅执行本地简繁转换和轨道整理，不启动 OCR、语音识别或翻译模型。")
    else:
        log("封装阶段无新增字幕：仅整理所选音轨和原字幕，本阶段不再启动 OCR、语音识别或翻译模型。")
    try:
        if output.suffix.lower() == ".mp4":
            legacy.mux_video(
                input_path,
                str(intermediate),
                keep_audio_ids,
                keep_subtitle_ids,
                generated,
                log,
                cancel_event,
                preserve_audio_defaults=preserve_audio_defaults,
                subtitle_sync_offsets=subtitle_sync_offsets,
                subtitle_language_preferences=subtitle_language_preferences,
                chinese_script_equivalent=chinese_script_equivalent,
                input_media=input_media,
                allow_fast_passthrough=False,
            )
            legacy.check_cancel(cancel_event)
            legacy.remux_mkv_to_mp4(str(intermediate), str(partial), log, cancel_event)
        else:
            legacy.mux_video(
                input_path,
                str(partial),
                keep_audio_ids,
                keep_subtitle_ids,
                generated,
                log,
                cancel_event,
                preserve_audio_defaults=preserve_audio_defaults,
                subtitle_sync_offsets=subtitle_sync_offsets,
                subtitle_language_preferences=subtitle_language_preferences,
                chinese_script_equivalent=chinese_script_equivalent,
                input_media=input_media,
                allow_fast_passthrough=True,
            )
        legacy.check_cancel(cancel_event)
        legacy.validate_media_output(
            partial,
            input_media,
            len(keep_audio_ids),
            len(keep_subtitle_ids) + len(generated),
            selected_specs,
            log,
            input_path=input_path,
            cancel_event=cancel_event,
        )
        os.replace(partial, output)
        if intermediate.exists():
            legacy.remove_incomplete_file(intermediate, log)
        log(f"正式输出已就绪：{output}")
        return str(output)
    except BaseException:
        legacy.remove_incomplete_file(partial, log)
        if intermediate.exists():
            legacy.remove_incomplete_file(intermediate, log)
        raise


def process_pro(
    input_path: str,
    output_path: str,
    keep_audio_ids: list[int],
    keep_subtitle_ids: list[int],
    source_mode: str,
    embedded_source_id: int | None,
    external_subtitle: str | None,
    audio_source_id: int | None,
    speech_language: str,
    source_language: str,
    target_codes: list[str],
    work_dir: str,
    log: Callable[[str], None],
    parallel_targets: int,
    cancel_event: threading.Event | None,
    preserve_audio_defaults: bool = False,
    shared_audio_reference: str | Path | None = None,
    shared_content_audio: SharedSubtitleContentAudio | None = None,
    trust_embedded_original_timeline: bool = False,
    local_chinese_conversion_sources: dict[str, int] | None = None,
    verified_timeline_reference: str | None = None,
    include_external_source_in_output: bool = True,
    trust_external_original_timeline: bool = False,
    allow_reverse_translation: bool = False,
    subtitle_language_preferences: list[str] | None = None,
    chinese_script_equivalent: bool = False,
    translation_start: Callable[[], None] | None = None,
    translation_end: Callable[[], None] | None = None,
    prepared_embedded_anchor: PreparedEmbeddedAnchor | None = None,
    prepared_image_timing: PreparedImageTiming | None = None,
) -> str:
    """Run the selected subtitle or tracks-only route and safely remux a final file."""
    if (translation_start is None) != (translation_end is None):
        raise ValueError("翻译资源回调必须同时提供开始和结束方法。")
    if trust_embedded_original_timeline:
        subtitle_sync_offsets, corrected_sources, resolved_source_id = {}, {}, embedded_source_id
        log("无搜索处理不运行内嵌字幕时间轴核验；所有保留字幕沿用原时间轴。")
    else:
        subtitle_sync_offsets, corrected_sources, resolved_source_id = prepare_embedded_text_corrections(
            input_path,
            keep_subtitle_ids,
            embedded_source_id,
            audio_source_id,
            work_dir,
            log,
            cancel_event,
            include_source_alternatives=source_mode == "embedded" and bool(target_codes),
            verified_external_subtitle=(
                verified_timeline_reference
                if verified_timeline_reference
                and Path(verified_timeline_reference).is_file()
                else (
                    external_subtitle
                    if source_mode in {"external", "online"}
                    and external_subtitle
                    and Path(external_subtitle).is_file()
                    else None
                )
            ),
            shared_audio_reference=shared_audio_reference,
            shared_content_audio=shared_content_audio,
            prepared_embedded_anchor=prepared_embedded_anchor,
            prepared_image_timing=prepared_image_timing,
        )
    if resolved_source_id != embedded_source_id and resolved_source_id is not None:
        if embedded_source_id in keep_subtitle_ids:
            keep_subtitle_ids = [
                resolved_source_id if track_id == embedded_source_id else track_id
                for track_id in keep_subtitle_ids
            ]
            keep_subtitle_ids = list(dict.fromkeys(keep_subtitle_ids))
        embedded_source_id = resolved_source_id
    local_generated = _prepare_local_chinese_conversions(
        input_path,
        local_chinese_conversion_sources,
        work_dir,
        corrected_sources,
        subtitle_sync_offsets,
        log,
        cancel_event,
    )
    if source_mode == "none":
        return process_tracks_only(
            input_path,
            output_path,
            keep_audio_ids,
            keep_subtitle_ids,
            work_dir,
            log,
            cancel_event,
            preserve_audio_defaults,
            subtitle_sync_offsets,
            generated_subtitles=local_generated,
            subtitle_language_preferences=subtitle_language_preferences,
            chinese_script_equivalent=chinese_script_equivalent,
        )
    if source_mode == "embedded":
        return legacy.process_video(
            input_path, output_path, keep_audio_ids, keep_subtitle_ids,
            embedded_source_id, target_codes, work_dir, log, parallel_targets, cancel_event,
            preserve_audio_defaults,
            subtitle_sync_offsets,
            corrected_sources.get(embedded_source_id),
            (
                subtitle_sync_offsets.get(embedded_source_id, 0)
                if embedded_source_id not in corrected_sources
                else 0
            ),
            additional_generated_subtitles=local_generated,
            subtitle_language_preferences=subtitle_language_preferences,
            chinese_script_equivalent=chinese_script_equivalent,
            translation_start=translation_start,
            translation_end=translation_end,
        )

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    output = Path(output_path)
    partial = legacy.partial_output_path(output_path)
    intermediate = work / "final-before-mp4.mkv"
    legacy.remove_incomplete_file(partial, log)
    legacy.ensure_output_disk_space(input_path, output_path, log)
    if source_mode in {"external", "online"}:
        if trust_external_original_timeline:
            source_srt = subtitle_events_to_srt(
                Path(external_subtitle or ""),
                work / "external-offline-original.srt",
            )
            log("使用已确认的外挂字幕时间轴，不重复运行自动对时。")
        else:
            source_srt = _external_timeline_for_processing(
                input_path,
                external_subtitle,
                work_dir,
                audio_source_id,
                log,
                cancel_event,
            )
    elif source_mode == "audio":
        raise RuntimeError(
            "音轨直接生成整片字幕功能已停用；请使用影片内字幕、在线字幕或手动导入字幕。"
        )
    else:
        raise RuntimeError(f"不支持的字幕来源模式：{source_mode}")
    legacy.check_cancel(cancel_event)
    events = legacy.parse_subtitle(source_srt)
    if not events:
        raise RuntimeError("生成的字幕没有有效文本。")
    source_code, source_track_name = subtitle_track_identity(events, source_language or speech_language)
    target_codes = list(dict.fromkeys(target_codes))
    if not allow_reverse_translation and source_code != "en" and any(
        normalize_language_code(code) == "en" for code in target_codes
    ):
        raise RuntimeError(
            "没有英文文本字幕来源，已阻止使用非英文字幕反向翻译生成英文。"
        )
    if allow_reverse_translation and source_code != "en" and any(
        normalize_language_code(code) == "en" for code in target_codes
    ):
        log("使用所选文本标杆翻译生成英文；输出继承标杆时间轴。")
    if source_code != "und":
        skipped_targets = [code for code in target_codes if normalize_language_code(code) == source_code]
        if skipped_targets:
            log(f"来源字幕已是 {legacy.LANGUAGES.get(source_code, ('', '', source_code))[2]}，跳过同语种重复翻译。")
            target_codes = [code for code in target_codes if normalize_language_code(code) != source_code]
    generated: list[tuple[Path, str, str, bool]] = list(local_generated)
    if include_external_source_in_output:
        generated.insert(0, (source_srt, source_code, source_track_name, False))
    if not include_external_source_in_output:
        log(
            "已验证的在线文本字幕仅作为图片字幕纠偏标杆和翻译来源；"
            "同语言原字幕已经保留，因此不重复写入成品。"
        )
    if target_codes:
        model_targets = [
            target
            for target in target_codes
            if not chinese_script_converter.is_script_conversion(source_code, target)
            and legacy.needs_model_translation(
                events, target, work / f"translation-cache-{target}.jsonl", source_code,
            )
        ]

        def translate_one(target: str) -> tuple[str, Path]:
            translated = work / f"translated-{target}.srt"
            target_label = legacy.LANGUAGES.get(target, ("", "", target))[2]
            if chinese_script_converter.is_script_conversion(source_code, target):
                chinese_script_converter.convert_subtitle_events(
                    events,
                    translated,
                    source_code,
                    target,
                )
                log(f"{target_label}本地简繁转换完成：{len(events)} 行；保留原时间轴。")
                return target, translated
            cache = work / f"translation-cache-{target}.jsonl"
            try:
                legacy.translate_events(
                    events,
                    translated,
                    cache,
                    target,
                    source_code,
                    log,
                    cancel_event=cancel_event,
                )
            except legacy.CancelledError:
                log(f"{target_label}翻译已停止：用户停止处理；已完成的翻译缓存仍然保留。")
                raise
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
                log(f"{target_label}翻译中断：{reason}；已完成的翻译缓存仍然保留。")
                raise RuntimeError(f"{target_label}翻译中断：{reason}") from exc
            return target, translated

        translation_active = False
        try:
            if model_targets:
                if translation_start is not None:
                    translation_start()
                translation_active = True
                legacy.check_cancel(cancel_event)
                legacy.ensure_ollama_running(log=log, cancel_event=cancel_event)
            if len(target_codes) == 1 or parallel_targets <= 1:
                completed = [translate_one(target) for target in target_codes]
            else:
                workers = max(1, min(parallel_targets, len(target_codes)))
                log(f"并行翻译目标语言：{workers} 路")
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    completed = list(executor.map(translate_one, target_codes))
        finally:
            if translation_active:
                if translation_end is not None:
                    translation_end()
                elif translation_start is None:
                    legacy.unload_ollama_model(log)
        for index, (target, translated) in enumerate(completed):
            suffix = (
                "Converted"
                if chinese_script_converter.is_script_conversion(source_code, target)
                else "Auto"
            )
            generated.append((translated, target, f"{legacy.LANGUAGES[target][2]} {suffix}", False))
    if subtitle_language_preferences is None:
        preferred_default_index = 1 if len(generated) > 1 else 0
        generated = [
            (path, code, name, index == preferred_default_index)
            for index, (path, code, name, _is_default) in enumerate(generated)
        ]
    input_media = legacy.inspect_media(input_path)
    selected_specs = [
        item for item in input_media.get("tracks", [])
        if item.get("type") == "audio" and int(item.get("id", -1)) in keep_audio_ids
    ]
    legacy.ensure_mux_disk_space(input_path, output_path, log)
    try:
        if output.suffix.lower() == ".mp4":
            legacy.mux_video(
                input_path,
                str(intermediate),
                keep_audio_ids,
                keep_subtitle_ids,
                generated,
                log,
                cancel_event,
                preserve_audio_defaults=preserve_audio_defaults,
                subtitle_sync_offsets=subtitle_sync_offsets,
                subtitle_language_preferences=subtitle_language_preferences,
                chinese_script_equivalent=chinese_script_equivalent,
                input_media=input_media,
                allow_fast_passthrough=False,
            )
            legacy.remux_mkv_to_mp4(str(intermediate), str(partial), log, cancel_event)
        else:
            legacy.mux_video(
                input_path,
                str(partial),
                keep_audio_ids,
                keep_subtitle_ids,
                generated,
                log,
                cancel_event,
                preserve_audio_defaults=preserve_audio_defaults,
                subtitle_sync_offsets=subtitle_sync_offsets,
                subtitle_language_preferences=subtitle_language_preferences,
                chinese_script_equivalent=chinese_script_equivalent,
                input_media=input_media,
                allow_fast_passthrough=True,
            )
        legacy.validate_media_output(partial, input_media, len(keep_audio_ids), len(keep_subtitle_ids) + len(generated), selected_specs, log,
                                     input_path=input_path, cancel_event=cancel_event)
        os.replace(partial, output)
        log(f"正式输出已就绪：{output}")
        return str(output)
    except BaseException:
        legacy.remove_incomplete_file(partial, log)
        raise


# Active route since 2026-09-29. Earlier Whisper implementations above are
# retained as _legacy_whisper_* for research/regression, never called as fallback.
def prepare_shared_subtitle_content_audio(input_path, selected_audio_id, work_dir, log,
        cancel=None, source_language="en", time_budget_seconds=None):
    import continuous_vad
    return continuous_vad.shared_audio(input_path, selected_audio_id, log, cancel, time_budget_seconds)


def prepare_shared_alignment_audio(input_path, selected_audio_id, cache_dir, log, cancel=None):
    return prepare_shared_subtitle_content_audio(input_path, selected_audio_id, cache_dir, log, cancel).vad_reference


def preflight_online_subtitle(input_path, subtitle_path, work_dir, selected_audio_id,
        shared_content_audio, log, cancel=None, source_language="en", candidate_label="在线字幕",
        time_budget_seconds=20.0):
    import continuous_vad
    return continuous_vad.preflight(input_path, subtitle_path, work_dir, selected_audio_id, log, cancel,
        shared=shared_content_audio, source_language=source_language, candidate_label=candidate_label,
        budget=time_budget_seconds)


def preflight_external_subtitle(input_path, subtitle_path, work_dir, selected_audio_id, log,
        cancel=None, source_language="", use_embedded_reference=True, candidate_label="下载字幕",
        timeline_cache_dir=None, time_budget_seconds=60.0, manual_reference_first=False,
        audio_drift_guard_override=None, shared_audio_reference=None):
    import continuous_vad
    return continuous_vad.preflight(input_path, subtitle_path, work_dir, selected_audio_id, log, cancel,
        source_language=source_language, candidate_label=candidate_label,
        budget=min(20.0, time_budget_seconds or 20.0), output_name="external-verified.srt")


def align_embedded_text_track(input_path, normalized_subtitle, work_dir, selected_audio_id,
        source_language, track_id, log, cancel=None, shared_audio_reference=None, shared_content_audio=None):
    import continuous_vad
    return continuous_vad.preflight(input_path, str(normalized_subtitle), str(work_dir), selected_audio_id,
        log, cancel, shared=shared_content_audio, candidate_label=f"内嵌文本轨 {track_id}",
        output_name="verified-anchor.srt")
