# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
import concurrent.futures
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

import pro_core
import subtitle_tool_core as core
import burned_subtitle_detector
import chinese_script_converter
from subtitle_languages import chinese_dialect_rank, subtitle_language
from subtitle_identity_guard import media_specification
from profile_model import (
    AUDIO_FORMATS,
    BatchPlan,
    PreferenceProfile,
    audio_format,
    is_commentary,
    language_label,
    normalize_language,
)


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v"}
OUTPUT_SUFFIX = ".SF.mkv"


def is_output_path(path: str | Path) -> bool:
    return Path(path).name.lower().endswith(OUTPUT_SUFFIX.lower())
UNIVERSAL_AUDIO_ORDER = ("AC-3", "AAC", "E-AC-3", "Opus")
DEFAULT_UNIVERSAL_AUDIO = "AC-3"
_ANALYSIS_CACHE_LOCK = threading.Lock()
_ANALYSIS_CACHE_LIMIT = 300


class OfflineSubtitleUnavailableError(RuntimeError):
    """The no-search batch mode cannot safely create the requested subtitles."""


class _TranslationResourceScope:
    """Reserve the model while queued, but release the AI slot before muxing."""

    def __init__(self, gate, log, cancel_event):
        self.gate = gate
        self.log = log
        self.cancel_event = cancel_event
        self.leased = False
        self.acquired = False

    def start(self):
        if self.leased:
            return
        core.check_cancel(self.cancel_event)
        # Waiting translators also protect the warm model from being unloaded
        # by the preceding film. The inference lane remains single-request.
        core.begin_ollama_lease()
        self.leased = True
        try:
            if self.gate is not None:
                started = time.monotonic()
                self.log("等待可用的 AI 翻译资源…")
                while not self.gate.acquire(timeout=0.5):
                    core.check_cancel(self.cancel_event)
                self.acquired = True
                core.check_cancel(self.cancel_event)
                self.log(f"AI 翻译资源已取得，排队耗时 {time.monotonic() - started:.2f} 秒。")
        except BaseException:
            self.finish()
            raise

    def finish(self):
        if self.acquired:
            self.acquired = False
            self.gate.release()
        if self.leased:
            self.leased = False
            if core.end_ollama_lease():
                core.unload_ollama_model(self.log)


class _AnyCancel:
    def __init__(self, *events: threading.Event | None):
        self.events = events

    def is_set(self) -> bool:
        return any(event is not None and event.is_set() for event in self.events)


def process_log_path(plan: BatchPlan) -> Path:
    output = Path(plan.output_path)
    return Path(str(output.with_suffix("")) + "_work") / "process.log"


def append_process_log(plan: BatchPlan, message: str) -> None:
    path = process_log_path(plan)
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"[{timestamp}] {message}\n")


def persistent_process_logger(
    plan: BatchPlan,
    downstream: Callable[[str], None],
) -> Callable[[str], None]:
    def write(message: str) -> None:
        append_process_log(plan, message)
        downstream(message)

    return write


def _analysis_cache_path() -> Path:
    configured = os.environ.get("SUBFLOW_RUNTIME_DIR", "").strip()
    root = (
        Path(configured)
        if configured
        else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubFlow"
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / "analysis-media-cache.json"


def _read_analysis_cache() -> dict[str, dict]:
    try:
        payload = json.loads(_analysis_cache_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_analysis_cache(cache: dict[str, dict]) -> None:
    path = _analysis_cache_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _inspect_media_cached(
    path: str, log: Callable[[str], None],
    cancel_event: threading.Event | None = None,
) -> tuple[dict, bool]:
    core.check_cancel(cancel_event)
    source = Path(path).resolve()
    stat = source.stat()
    key = str(source).lower()
    with _ANALYSIS_CACHE_LOCK:
        cached = _read_analysis_cache().get(key)
    if (
        isinstance(cached, dict)
        and cached.get("size") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
        and isinstance(cached.get("media"), dict)
    ):
        log(f"{source.name}：分析缓存命中，复用容器轨道信息。")
        return cached["media"], True

    media = core.inspect_media(str(source), cancel_event=cancel_event)
    core.check_cancel(cancel_event)
    with _ANALYSIS_CACHE_LOCK:
        cache = _read_analysis_cache()
        cache.pop(key, None)
        cache[key] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "media": media,
        }
        if len(cache) > _ANALYSIS_CACHE_LIMIT:
            cache = dict(list(cache.items())[-_ANALYSIS_CACHE_LIMIT:])
        try:
            _write_analysis_cache(cache)
        except OSError:
            pass
    return media, False


def _finish_analysis(plan: BatchPlan, started: float, log: Callable[[str], None]) -> BatchPlan:
    if plan.media_specification and not plan.detail.startswith("影片规格："):
        plan.detail = f"影片规格：{plan.media_specification}\n{plan.detail}".rstrip()
    log(f"{Path(plan.path).name}：分析完成，耗时 {time.monotonic() - started:.2f} 秒。")
    return plan


def _main_audio(audio_tracks):
    candidates = [track for track in audio_tracks if not is_commentary(track)] or list(audio_tracks)
    return next((track for track in candidates if track.default), candidates[0] if candidates else None)


def _translation_source_language(plan: BatchPlan, tracks) -> str:
    if plan.source_mode in {"external", "online"} and plan.external_subtitle:
        return normalize_language(plan.external_language)
    if plan.source_mode == "embedded" and plan.source_subtitle_id is not None:
        source_track = next(
            (track for track in tracks if track.id == plan.source_subtitle_id and track.type == "subtitles"),
            None,
        )
        if source_track is not None:
            return _subtitle_language(source_track)
    return "und"


def _effective_translation_targets(plan: BatchPlan, targets: list[str], tracks, main_track) -> list[str]:
    source_language = _translation_source_language(plan, tracks)
    unique_targets = list(dict.fromkeys(targets))
    if source_language == "und":
        return unique_targets
    return [code for code in unique_targets if normalize_language(code) != source_language]



def _describe_tracks(tracks) -> str:
    labels = []
    for track in tracks:
        codec = (track.codec or "").lower()
        if track.pgs_subtitle:
            subtitle_format = "PGS"
        elif track.vobsub_subtitle:
            subtitle_format = "VobSub"
        elif "ass" in codec or "ssa" in codec:
            subtitle_format = "ASS"
        elif "webvtt" in codec or "vtt" in codec:
            subtitle_format = "VTT"
        elif track.text_subtitle:
            subtitle_format = "SRT"
        else:
            subtitle_format = track.codec or "未知"
        value = f"{_subtitle_language_label(track)} / {subtitle_format}"
        labels.append(value)
    return "、".join(labels) if labels else "无"


def _describe_audio_tracks(audio_tracks, main_audio) -> str:
    labels = []
    for track in audio_tracks:
        language = normalize_language(track.language)
        marker = "（主音轨）" if main_audio and track.id == main_audio.id else ""
        labels.append(f"{language} / {audio_format(track.codec)}{marker}")
    return "，".join(labels) if labels else "无"


def _best_universal_audio(audio_tracks, main_audio=None):
    candidates = [
        track for track in audio_tracks
        if not is_commentary(track) and audio_format(track.codec) in UNIVERSAL_AUDIO_ORDER
    ]
    if not candidates:
        return None
    if main_audio in candidates:
        return main_audio
    return min(candidates, key=lambda track: (
        0 if track.default else 1,
        UNIVERSAL_AUDIO_ORDER.index(audio_format(track.codec)),
        audio_tracks.index(track),
    ))


def _best_quality_audio(audio_tracks, main_audio=None):
    candidates = [
        track for track in audio_tracks
        if not is_commentary(track) and audio_format(track.codec) not in UNIVERSAL_AUDIO_ORDER
    ]
    if main_audio in candidates:
        return main_audio
    return next((track for track in candidates if track.default), candidates[0] if candidates else None)


def _audio_policy_selection(
    audio_tracks,
    main_audio,
    policy: str,
) -> tuple[list[int], list[str], str, int | None]:
    if policy == "native":
        return (
            [track.id for track in audio_tracks],
            [],
            f"原生音频：保留全部 {len(audio_tracks)} 条原音轨，不转码、不新增",
            None,
        )

    english_tracks = [
        track for track in audio_tracks
        if not is_commentary(track) and normalize_language(track.language) == "en"
    ]
    english_main = main_audio if main_audio in english_tracks else None
    english_universal = _best_universal_audio(english_tracks, english_main)
    english_quality = _best_quality_audio(english_tracks, english_main)
    english_source = english_quality or english_universal
    if policy == "compact":
        if english_universal:
            return (
                [english_universal.id],
                [],
                f"精简兼容：仅保留一条英语 {audio_format(english_universal.codec)} 通用音轨",
                None,
            )
        return (
            [],
            [DEFAULT_UNIVERSAL_AUDIO],
            f"精简兼容：由英语原音轨生成一条 {DEFAULT_UNIVERSAL_AUDIO} 通用音轨，其余音轨不写入成品",
            english_source.id if english_source else None,
        )

    kept = []
    if english_quality:
        kept.append(english_quality.id)
    if english_universal and english_universal.id not in kept:
        kept.append(english_universal.id)

    other_languages: list[str] = []
    for track in audio_tracks:
        language = normalize_language(track.language)
        if language not in {"en", "und"} and language not in other_languages:
            other_languages.append(language)
    other_kept = []
    for language in other_languages:
        language_tracks = [
            track for track in audio_tracks
            if not is_commentary(track) and normalize_language(track.language) == language
        ]
        compatible = _best_universal_audio(
            language_tracks,
            main_audio if main_audio in language_tracks else None,
        )
        if compatible:
            kept.append(compatible.id)
            other_kept.append(f"{language_label(language)} {audio_format(compatible.codec)}")

    generated = [] if english_universal else [DEFAULT_UNIVERSAL_AUDIO]
    english_description = []
    if english_quality:
        english_description.append(audio_format(english_quality.codec) + " 高品质音轨")
    if english_universal:
        english_description.append(audio_format(english_universal.codec) + " 通用音轨")
    elif english_source:
        english_description.append("生成 " + DEFAULT_UNIVERSAL_AUDIO + " 通用音轨")
    action = "通用兼容：英语保留" + "和".join(english_description)
    if other_kept:
        action += "；其他语言各保留一条兼容音轨（" + "、".join(other_kept) + "）"
    return kept, generated, action, english_source.id if generated and english_source else None


def _subtitle_name(track) -> str:
    return (getattr(track, "name", "") or "").strip()


def _subtitle_language(track) -> str:
    return subtitle_language(getattr(track, "language", "und"), _subtitle_name(track))


def _subtitle_language_label(track) -> str:
    code = _subtitle_language(track)
    if code == "zh":
        return "中文（简繁未标记）"
    if code == "yue":
        return "粤语字幕"
    return language_label(code)


def _chinese_subtitle_rank(track, target: str, movie_duration: float = 0.0):
    code = _subtitle_language(track)
    return (
        0 if code == target else 1 if code in chinese_script_converter.CHINESE_SCRIPT_CODES else 2,
        chinese_dialect_rank(getattr(track, "language", "und"), _subtitle_name(track)),
        *_subtitle_rank(track, movie_duration),
    )


def _subtitle_matches_download(track, downloaded_language: str) -> bool:
    code = _subtitle_language(track)
    return code == downloaded_language or (
        code == "zh" and downloaded_language in chinese_script_converter.CHINESE_SCRIPT_CODES
    )


def _subtitle_coverage(
    tracks,
    desired: list[str],
    script_equivalent: bool,
    additional_languages: tuple[str, ...] = (),
) -> set[str]:
    covered = {_subtitle_language(track) for track in tracks} | set(additional_languages)
    if "zh" in covered or (
        script_equivalent and covered.intersection(chinese_script_converter.CHINESE_SCRIPT_CODES)
    ):
        covered.update(code for code in desired if code in chinese_script_converter.CHINESE_SCRIPT_CODES)
    return covered


def _is_foreign_only_subtitle(track) -> bool:
    name = _subtitle_name(track).lower()
    partial_markers = (
        "foreign only", "foreign-only", "foreign parts", "foreign parts only",
        "alien only", "aliens only", "alien-only", "aliens-only",
        "forced", "forced only", "signs & songs", "signs/songs", "signs and songs",
        "non-english parts", "non english parts",
        "外语", "外語", "仅外语", "僅外語", "强制", "強制", "片段", "标牌", "標牌",
    )
    if any(marker in name for marker in partial_markers):
        return True
    full_markers = (
        "bilingual", "dual language", "full subtitle", "full subtitles",
        "complete subtitle", "完整字幕", "全字幕", "双语", "雙語",
    )
    explicitly_full = any(marker in name for marker in full_markers)
    return bool(getattr(track, "forced", False)) and not explicitly_full


def _statistical_incomplete_reason(track, movie_duration: float = 0.0) -> str:
    """Return a reason only when container statistics prove a track is partial.

    These fields come from the existing mkvmerge analysis, so this gate must
    stay deliberately conservative.  Ambiguous statistics are left for the
    normal subtitle verification path instead of being rejected here.
    """
    if not (
        getattr(track, "text_subtitle", False)
        or getattr(track, "pgs_subtitle", False)
        or getattr(track, "vobsub_subtitle", False)
    ) or movie_duration < 900.0:
        return ""
    frame_count = getattr(track, "statistics_frame_count", None)
    active_span = getattr(track, "statistics_duration_seconds", None)
    if frame_count is not None and frame_count < 20:
        return f"容器统计仅 {frame_count} 条"
    if (
        frame_count is not None
        and active_span is not None
        and frame_count < 100
        and active_span < movie_duration * 0.20
    ):
        return f"容器统计仅 {frame_count} 条、有效跨度约 {active_span:.0f} 秒"
    return ""


def _is_incomplete_subtitle(track, movie_duration: float = 0.0) -> bool:
    return _is_foreign_only_subtitle(track) or bool(
        _statistical_incomplete_reason(track, movie_duration)
    )


def _foreign_only_note(tracks, movie_duration: float = 0.0) -> str:
    marked = [track for track in tracks if _is_incomplete_subtitle(track, movie_duration)]
    if not marked:
        return ""
    labels = []
    for track in marked:
        language = language_label(track.language)
        name = _subtitle_name(track)
        reason = _statistical_incomplete_reason(track, movie_duration)
        label = f"{language}({name})" if name else language
        if reason:
            label += f"[{reason}]"
        if label not in labels:
            labels.append(label)
    return "检测到片段/强制字幕：" + "、".join(labels) + "；已按非完整字幕处理，不当作完整字幕或翻译来源。"


def _subtitle_rank(track, movie_duration: float = 0.0) -> tuple[int, int, int]:
    name = _subtitle_name(track).lower()
    accessibility = any(token in name for token in ("sdh", "hearing", "cc", "听障"))
    return (
        1 if _is_incomplete_subtitle(track, movie_duration) else 0,
        1 if accessibility else 0,
        0 if track.default else 1,
    )


def _is_image_subtitle(track) -> bool:
    return bool(track.pgs_subtitle or track.vobsub_subtitle)


def _is_complete_english_text(track, movie_duration: float = 0.0) -> bool:
    return (
        track.text_subtitle
        and not _is_incomplete_subtitle(track, movie_duration)
        and normalize_language(track.language) == "en"
    )


def _is_complete_subtitle_source(track, movie_duration: float = 0.0) -> bool:
    return bool(
        (track.text_subtitle or track.pgs_subtitle or track.vobsub_subtitle)
        and not _is_incomplete_subtitle(track, movie_duration)
    )


def _offline_source_track(tracks, main_track, movie_duration: float = 0.0):
    """Choose an unverified timing reference for the explicit no-search mode.

    The main-audio language wins, then English, then any complete subtitle.
    Text is preferred within each tier because it avoids image OCR.
    """
    candidates = [
        track for track in tracks
        if track.type == "subtitles" and _is_complete_subtitle_source(track, movie_duration)
    ]
    if not candidates:
        return None
    audio_language = normalize_language(main_track.language) if main_track is not None else "und"

    def rank(track) -> tuple[int, int, int, int, int, int]:
        language = _subtitle_language(track)
        if audio_language != "und" and language == audio_language:
            language_tier = 0
        elif language == "en":
            language_tier = 1
        else:
            language_tier = 2
        return (
            language_tier,
            0 if track.text_subtitle else 1,
            *_subtitle_rank(track, movie_duration),
            track.id,
        )

    return min(candidates, key=rank)


def ordered_text_anchors(tracks, main_track, duration=0.0):
    import continuous_vad
    audio = continuous_vad.audio_track(tracks, main_track.id if main_track else None)
    language = normalize_language(audio.language)
    candidates = [t for t in tracks if t.type == "subtitles" and t.text_subtitle
        and not _is_incomplete_subtitle(t, duration)
        and not any(x in (t.name or "").lower() for x in ("commentary", "评论", "評論", "解说"))]

    def rank(track):
        subtitle_language = _subtitle_language(track)
        if language != "und" and subtitle_language == language:
            language_tier = 0
        elif subtitle_language == "en":
            language_tier = 1
        else:
            language_tier = 2
        return (language_tier, *_subtitle_rank(track, duration), track.id)

    return sorted(candidates, key=rank)


def plan_requires_online_subtitle_service(
    plan: BatchPlan,
    profile: PreferenceProfile,
) -> bool:
    """Mirror the process-time conditions that would start subtitle search."""
    verified_english = bool(
        plan.external_subtitle_verified
        and plan.external_subtitle
    )
    if verified_english:
        return False
    translation_candidates = [
        code for code in plan.missing_subtitle_languages
        if code not in plan.local_chinese_conversion_sources
    ]
    needs_translation_source = bool(
        translation_candidates and not plan.has_complete_text
    )
    needs_image_anchor = bool(
        plan.has_retained_image_subtitles
        and not plan.has_complete_text
    )
    return needs_translation_source or needs_image_anchor


def _duration_from_track_statistics(tracks) -> float:
    video_durations = [
        float(track.statistics_duration_seconds)
        for track in tracks
        if track.type == "video" and track.statistics_duration_seconds is not None
    ]
    if video_durations:
        return max(video_durations)
    available = [
        float(track.statistics_duration_seconds)
        for track in tracks
        if track.statistics_duration_seconds is not None
    ]
    return max(available, default=0.0)


def _refresh_incomplete_subtitle_selections(
    plan: BatchPlan,
    profile: PreferenceProfile,
    tracks,
    movie_duration: float,
    log: Callable[[str], None],
) -> None:
    """Recheck a saved analysis plan before processing without reading media again."""
    by_id = {track.id: track for track in tracks if track.type == "subtitles"}
    desired = set(profile.subtitle_languages)
    refreshed_ids: list[int] = []
    for track_id in plan.subtitle_ids:
        selected = by_id.get(track_id)
        if selected is None or not _is_incomplete_subtitle(selected, movie_duration):
            if selected is not None:
                refreshed_ids.append(track_id)
            continue
        language = _subtitle_language(selected)
        replacements = [
            track
            for track in by_id.values()
            if (
                _subtitle_language(track) == language
                and not _is_incomplete_subtitle(track, movie_duration)
            )
        ]
        if replacements:
            replacement = min(
                replacements,
                key=lambda track: (
                    0 if track.text_subtitle == selected.text_subtitle else 1,
                    *_subtitle_rank(track, movie_duration),
                    track.id,
                ),
            )
            refreshed_ids.append(replacement.id)
            log(
                f"正式处理前复核：字幕轨 {selected.id} 已由容器统计确认不完整，"
                f"自动改用同语种完整字幕轨 {replacement.id}。"
            )
        else:
            if language in desired and language not in plan.missing_subtitle_languages:
                plan.missing_subtitle_languages.append(language)
            log(
                f"正式处理前复核：字幕轨 {selected.id} 已由容器统计确认不完整，"
                "已从保留列表移除。"
            )
    plan.subtitle_ids = list(dict.fromkeys(refreshed_ids))

    source = by_id.get(plan.source_subtitle_id)
    if source is not None and _is_incomplete_subtitle(source, movie_duration):
        language = _subtitle_language(source)
        alternatives = [
            track
            for track in by_id.values()
            if (
                track.text_subtitle
                and _subtitle_language(track) == language
                and not _is_incomplete_subtitle(track, movie_duration)
            )
        ]
        if alternatives:
            replacement = min(
                alternatives,
                key=lambda track: (*_subtitle_rank(track, movie_duration), track.id),
            )
            plan.source_subtitle_id = replacement.id
            log(
                f"正式处理前复核：字幕来源轨 {source.id} 不完整，"
                f"自动改用完整文本轨 {replacement.id}。"
            )
        else:
            plan.source_subtitle_id = None
            if plan.source_mode == "embedded":
                plan.source_mode = "none"
            log(f"正式处理前复核：字幕来源轨 {source.id} 不完整，已禁止作为翻译标杆。")

    refreshed_conversions: dict[str, int] = {}
    for target, source_id in plan.local_chinese_conversion_sources.items():
        conversion_source = by_id.get(source_id)
        if conversion_source is None:
            if target not in plan.missing_subtitle_languages:
                plan.missing_subtitle_languages.append(target)
            continue
        if not _is_incomplete_subtitle(conversion_source, movie_duration):
            refreshed_conversions[target] = source_id
            continue
        language = _subtitle_language(conversion_source)
        alternatives = [
            track
            for track in by_id.values()
            if (
                track.text_subtitle
                and _subtitle_language(track) == language
                and not _is_incomplete_subtitle(track, movie_duration)
            )
        ]
        if alternatives:
            replacement = min(
                alternatives,
                key=lambda track: (*_subtitle_rank(track, movie_duration), track.id),
            )
            refreshed_conversions[target] = replacement.id
            log(
                f"正式处理前复核：本地简繁转换来源轨 {source_id} 不完整，"
                f"自动改用完整文本轨 {replacement.id}。"
            )
        elif target not in plan.missing_subtitle_languages:
            plan.missing_subtitle_languages.append(target)
    plan.local_chinese_conversion_sources = refreshed_conversions

    plan.has_complete_text = any(t.text_subtitle and not _is_incomplete_subtitle(t, movie_duration) for t in by_id.values())
    plan.has_complete_english_text = any(
        _is_complete_english_text(track, movie_duration) for track in by_id.values()
    )
    plan.has_complete_subtitle = any(
        _is_complete_subtitle_source(track, movie_duration) for track in by_id.values()
    )
    plan.has_retained_image_subtitles = any(
        track_id in plan.subtitle_ids and _is_image_subtitle(track)
        for track_id, track in by_id.items()
    )


def analyze_video(
    path: str,
    profile: PreferenceProfile,
    external_subtitle: str = "",
    external_language: str = "",
    external_subtitle_verified: bool = False,
    external_subtitle_verification: str = "",
    external_subtitle_provider: str = "",
    external_subtitle_release: str = "",
    external_subtitle_identity_key: str = "",
    external_subtitle_seal: str = "",
    log: Callable[[str], None] = core.log_noop,
    cancel_event: threading.Event | None = None,
    external_subtitle_origin: str = "",
    manual_confirmation_hash: str = "",
) -> BatchPlan:
    core.check_cancel(cancel_event)
    analysis_started = time.monotonic()
    metadata_started = time.monotonic()
    media_data, cache_hit = _inspect_media_cached(path, log, cancel_event)
    tracks = core.tracks_from_media(media_data)
    log(
        f"{Path(path).name}：容器轨道分析耗时 {time.monotonic() - metadata_started:.2f} 秒"
        f"{'（缓存）' if cache_hit else ''}。"
    )
    audio_tracks = [track for track in tracks if track.type == "audio"]
    subtitle_tracks = [track for track in tracks if track.type == "subtitles"]
    main_audio = _main_audio(audio_tracks)
    output = str(Path(path).with_suffix("")) + OUTPUT_SUFFIX
    plan = BatchPlan(path=path, profile_slot=profile.slot, output_path=output)
    duration_ns = core.video_track_duration_ns(media_data) or core.media_duration_ns(media_data)
    duration_seconds = duration_ns / 1_000_000_000 if duration_ns else 0.0
    try:
        file_size_bytes = Path(path).stat().st_size
    except OSError:
        file_size_bytes = None
    plan.media_specification = media_specification(
        path,
        duration_seconds,
        file_size_bytes,
    )
    plan.external_subtitle = external_subtitle
    plan.external_language = external_language
    plan.external_subtitle_verified = external_subtitle_verified
    plan.external_subtitle_verification = external_subtitle_verification
    plan.external_subtitle_provider = external_subtitle_provider
    plan.external_subtitle_release = external_subtitle_release
    plan.external_subtitle_identity_key = external_subtitle_identity_key
    plan.external_subtitle_seal = external_subtitle_seal
    plan.external_subtitle_origin = external_subtitle_origin
    plan.manual_confirmation_hash = manual_confirmation_hash
    plan.main_audio = (
        f"{language_label(main_audio.language)} / {audio_format(main_audio.codec)}" if main_audio else "无音轨"
    )
    plan.audio_tracks = _describe_audio_tracks(audio_tracks, main_audio)
    plan.audio_codecs = "、".join(dict.fromkeys(audio_format(track.codec) for track in audio_tracks)) or "无"
    plan.subtitles = _describe_tracks(subtitle_tracks)
    plan.has_complete_text = any(t.text_subtitle and not _is_incomplete_subtitle(t, duration_seconds) for t in subtitle_tracks)
    plan.has_complete_english_text = any(
        _is_complete_english_text(track, duration_seconds) for track in subtitle_tracks
    )
    plan.has_complete_subtitle = any(
        _is_complete_subtitle_source(track, duration_seconds) for track in subtitle_tracks
    )
    plan.has_image_subtitles = any(_is_image_subtitle(track) for track in subtitle_tracks)

    if not audio_tracks:
        plan.status = "blocked"
        plan.status_label = "无法处理"
        plan.summary = "影片没有可用音轨"
        plan.detail = "未检测到音轨。批量处理不会生成新的配音，需先更换片源。"
        return _finish_analysis(plan, analysis_started, log)

    audio_policy = profile.audio_policy if profile.audio_policy in {"native", "universal", "compact"} else "universal"
    has_und_audio = any(normalize_language(track.language) == "und" for track in audio_tracks)
    has_english_audio = any(
        normalize_language(track.language) == "en" and not is_commentary(track)
        for track in audio_tracks
    )
    if has_und_audio:
        plan.audio_ids = [track.id for track in audio_tracks]
        plan.missing_audio_formats = []
        plan.audio_passthrough_warning = True
        plan.audio_warning_detail = "存在未标记语言音轨，音频保持原样"
        audio_policy_action = plan.audio_warning_detail
    elif audio_policy != "native" and not has_english_audio:
        plan.audio_ids = [track.id for track in audio_tracks]
        plan.missing_audio_formats = []
        plan.audio_passthrough_warning = True
        plan.audio_warning_detail = "未检测到英语音轨，音频保持原样"
        audio_policy_action = plan.audio_warning_detail
    else:
        (
            plan.audio_ids,
            plan.missing_audio_formats,
            audio_policy_action,
            plan.generated_audio_source_id,
        ) = _audio_policy_selection(
            audio_tracks,
            main_audio,
            audio_policy,
        )

    full_subtitle_tracks = [
        track
        for track in subtitle_tracks
        if not _is_incomplete_subtitle(track, duration_seconds)
    ]
    has_any_embedded_subtitle = bool(subtitle_tracks)
    missing_english_source_note = ""
    output_subtitle_tracks = full_subtitle_tracks
    foreign_only_note = _foreign_only_note(subtitle_tracks, duration_seconds)
    normalized_subs = [(track, _subtitle_language(track)) for track in output_subtitle_tracks]
    desired = list(dict.fromkeys(profile.subtitle_languages))
    equivalent_languages: set[str] = set()
    for target in desired:
        candidates = [track for track, code in normalized_subs if code == target]
        if (
            not candidates
            and profile.chinese_script_equivalent
            and target in chinese_script_converter.CHINESE_SCRIPT_CODES
        ):
            candidates = [
                track for track, code in normalized_subs
                if code in chinese_script_converter.CHINESE_SCRIPT_CODES or code == "zh"
            ]
        if not candidates and target in chinese_script_converter.CHINESE_SCRIPT_CODES:
            # A broad Chinese tag does not establish a script. Keep it as a
            # labelled fallback instead of forcing a fresh model translation.
            candidates = [track for track, code in normalized_subs if code == "zh"]
        if candidates:
            if target in chinese_script_converter.CHINESE_SCRIPT_CODES:
                selected = min(candidates, key=lambda track: _chinese_subtitle_rank(track, target, duration_seconds))
                if _subtitle_language(selected) != target:
                    equivalent_languages.add(target)
            else:
                selected = min(candidates, key=lambda track: _subtitle_rank(track, duration_seconds))
            if selected.id not in plan.subtitle_ids:
                plan.subtitle_ids.append(selected.id)
    existing_languages = {code for _track, code in normalized_subs}
    existing_languages.update(equivalent_languages)
    external_path = Path(external_subtitle) if external_subtitle else None
    external_code = normalize_language(external_language)
    external_is_usable = bool(external_path and external_path.is_file())
    if external_subtitle_verified and not external_is_usable:
        plan.status = "review"
        plan.status_label = "需要确认"
        plan.summary = "已验证英文字幕文件已丢失"
        plan.detail = "在线字幕已经通过核验，但正式处理前文件不存在；已阻止静默回退到其他字幕源。"
        return _finish_analysis(plan, analysis_started, log)
    if external_is_usable and not external_subtitle_verified:
        plan.status = "review"
        plan.status_label = "需要确认"
        plan.summary = "外挂字幕尚未通过音轨预检"
        plan.detail = (
            "已选择外挂或在线字幕，但尚未确认它与本片的时间轴匹配。"
            "请重新下载并完成预检后再开始批量处理。"
        )
        return _finish_analysis(plan, analysis_started, log)

    usable_complete_subtitle_tracks = [
        track
        for track in full_subtitle_tracks
        if track.text_subtitle or track.pgs_subtitle or track.vobsub_subtitle
    ]
    source_file = Path(path)
    should_scan_burned = (
        source_file.is_file()
        and not has_any_embedded_subtitle
        and not external_is_usable
    )
    if should_scan_burned:
        core.check_cancel(cancel_event)
        burned_started = time.monotonic()
        try:
            burned = burned_subtitle_detector.detect(
                path,
                media_data,
                [track.language for track in audio_tracks],
            )
        except Exception:
            burned = burned_subtitle_detector.DetectionResult(False, 0, 0, "烧录字幕检测未完成")
        core.check_cancel(cancel_event)
        log(f"{Path(path).name}：烧录字幕快速检查耗时 {time.monotonic() - burned_started:.2f} 秒。")
        if burned.detected:
            plan.burned_subtitle = True
            plan.burned_subtitle_detail = burned.detail
            plan.status = "burned"
            plan.status_label = "不可操作"
            plan.task_state = "blocked"
            plan.task_status_label = "不可操作"
            plan.summary = "检测到画面烧录字幕，不执行轨道处理"
            plan.detail = (
                f"{burned.detail}。烧录字幕已经成为视频画面的一部分，无法像字幕轨一样删除；"
                "继续新增字幕会发生重叠，因此本片已停止自动处理。"
            )
            return _finish_analysis(plan, analysis_started, log)
        if burned.status == "uncertain":
            plan.status = "review"
            plan.status_label = "需要确认"
            plan.task_state = "waiting"
            plan.task_status_label = "等待处理"
            plan.summary = "烧录字幕检测结果不确定"
            plan.burned_subtitle_detail = (
                f"{burned.detail}。证据不足，不按烧录字幕阻断；"
                "开始处理后将继续按字幕轨道与智能字幕策略执行。"
            )
    if external_is_usable and external_code in desired:
        existing_languages.add(external_code)
        plan.source_mode = "online"
    plan.missing_subtitle_languages = [code for code in desired if code not in existing_languages]

    usable_sources = [
        track for track in full_subtitle_tracks
        if track.text_subtitle or track.pgs_subtitle or track.vobsub_subtitle
    ]
    local_chinese_sources: dict[str, core.Track] = {}
    if not profile.chinese_script_equivalent:
        for target in plan.missing_subtitle_languages:
            if target not in chinese_script_converter.CHINESE_SCRIPT_CODES:
                continue
            counterpart = "zh-TW" if target == "zh-CN" else "zh-CN"
            candidates = [
                track
                for track in full_subtitle_tracks
                if track.text_subtitle and _subtitle_language(track) == counterpart
            ]
            if candidates:
                local_chinese_sources[target] = min(
                    candidates,
                    key=lambda track: _subtitle_rank(track, duration_seconds),
                )
    plan.local_chinese_conversion_sources = {
        target: track.id for target, track in local_chinese_sources.items()
    }
    text_sources = ordered_text_anchors(tracks, main_audio, duration_seconds)
    source = text_sources[0] if text_sources else None

    if plan.missing_subtitle_languages:
        # A subtitle explicitly selected by the user must stay the source.
        # Otherwise an embedded track can silently take precedence and the
        # downloaded subtitle is never included in the output.
        needs_translation_source = any(
            target not in plan.local_chinese_conversion_sources
            for target in plan.missing_subtitle_languages
        )
        if not needs_translation_source:
            pass
        elif external_is_usable:
            plan.source_mode = "online"
        elif source:
            plan.source_mode = "embedded"
            plan.source_subtitle_id = source.id
            if (
                source in output_subtitle_tracks
                and _subtitle_language(source) in desired
                and source.id not in plan.subtitle_ids
            ):
                plan.subtitle_ids.append(source.id)
        else:
            wanted = "、".join(language_label(code) for code in plan.missing_subtitle_languages)
            missing_english_source_note = (
                f"偏好要求最终保留 {wanted}，但影片没有可用的完整内嵌文本字幕来源。"
                "处理时将尝试搜索并核验英文文本字幕；若仍未找到会给出提醒，"
                "已有可用文本时优先使用其时间轴。"
            )
            # Row colour describes subtitle completeness only. A complete
            # non-English subtitle must not turn yellow merely because later
            # processing needs to find an English text source.
            if not usable_complete_subtitle_tracks:
                plan.status = "review"
                plan.status_label = "需要确认"
                plan.summary = "缺少完整字幕：仅有片段/强制字幕" if foreign_only_note else "缺少完整字幕来源"
                detail = missing_english_source_note
                if foreign_only_note:
                    detail += "\n" + foreign_only_note
                if plan.burned_subtitle_detail:
                    detail = f"{plan.burned_subtitle_detail}\n{detail}"
                plan.detail = detail
                return _finish_analysis(plan, analysis_started, log)

    if (profile.replace_downloaded_subtitle and external_is_usable
            and external_subtitle_verified and external_subtitle_origin == "download"):
        plan.subtitle_ids = [
            track_id for track_id in plan.subtitle_ids
            if not _subtitle_matches_download(
                next(track for track in subtitle_tracks if track.id == track_id), external_code
            )
        ]
        retained = [track for track in subtitle_tracks if track.id in plan.subtitle_ids]
        coverage = _subtitle_coverage(
            retained, desired, profile.chinese_script_equivalent, (external_code,)
        )
        plan.missing_subtitle_languages = [code for code in desired if code not in coverage]

    audio_actions = [audio_policy_action]

    subtitle_actions = []
    if plan.subtitle_ids:
        kept_languages = [
            _subtitle_language_label(track) for track in subtitle_tracks if track.id in plan.subtitle_ids
        ]
        subtitle_actions.append("保留 " + "、".join(dict.fromkeys(kept_languages)))
    if external_is_usable and external_code in desired:
        subtitle_actions.append("套用外挂" + language_label(external_code) + "（预检通过）")
    if (profile.replace_downloaded_subtitle and external_is_usable
            and external_subtitle_verified and external_subtitle_origin == "download"):
        subtitle_actions.append("保留下载" + language_label(external_code) + "并替换同语言内嵌字幕")
    if plan.missing_subtitle_languages:
        subtitle_actions.append("新增 " + "、".join(language_label(code) for code in plan.missing_subtitle_languages))
    if foreign_only_note:
        subtitle_actions.append("片段/强制字幕不计为完整字幕")
    if not desired:
        subtitle_actions.append("不要求新增字幕")

    if plan.status != "review":
        plan.status = "ready"
        plan.status_label = "可以处理"
    plan.has_retained_image_subtitles = any(
        track.id in plan.subtitle_ids and _is_image_subtitle(track)
        for track in subtitle_tracks
    )
    plan.summary = "；".join(audio_actions + subtitle_actions)
    processing_detail = (
        f"影片：{Path(path).name}\n"
        f"套用：偏好 {profile.slot} · {profile.name}\n"
        f"音轨：{'；'.join(audio_actions)}\n"
        f"字幕：{'；'.join(subtitle_actions)}\n"
        + (f"提示：{foreign_only_note}\n" if foreign_only_note else "")
        + (f"提示：{missing_english_source_note}\n" if missing_english_source_note else "")
        + (f"预检：{external_subtitle_verification}\n" if external_subtitle_verification else "")
        + f"输出：{plan.output_path}"
    )
    plan.detail = (
        f"{plan.burned_subtitle_detail}\n{processing_detail}"
        if plan.burned_subtitle_detail
        else processing_detail
    )
    return _finish_analysis(plan, analysis_started, log)


def _audio_stream_index(path: str, track_id: int) -> int:
    tracks = [track for track in core.inspect_tracks(path) if track.type == "audio"]
    for index, track in enumerate(tracks):
        if track.id == track_id:
            return index
    raise RuntimeError("无法定位用于音频兼容转换的主音轨。")


def _transcode_audio(
    input_path: str,
    track_id: int,
    target_format: str,
    destination: Path,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
) -> Path:
    settings = {
        # The generated track is a compatibility companion, so AAC uses the
        # fast coder. The original high-quality track remains untouched.
        "AAC": ("aac", "384k", ".m4a", ["-aac_coder", "fast"]),
        "AC-3": ("ac3", "640k", ".ac3", []),
        "E-AC-3": ("eac3", "640k", ".eac3", []),
        "Opus": ("libopus", "256k", ".opus", []),
    }
    if target_format not in settings:
        raise RuntimeError(f"不支持自动生成音频格式：{target_format}")
    codec, bitrate, extension, encoder_options = settings[target_format]
    output = destination.with_suffix(extension)
    stream_index = _audio_stream_index(input_path, track_id)
    log(f"正在由主音轨生成兼容音轨：{target_format}（只转换音频，不转换视频）")
    media = core.inspect_media(input_path)
    duration_ns = core.video_track_duration_ns(media) or core.media_duration_ns(media)
    duration_seconds = duration_ns / 1_000_000_000 if duration_ns else 0.0
    args = [
        core.FFMPEG, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", input_path, "-map", f"0:a:{stream_index}", "-vn", "-sn", "-dn",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-c:a", codec, "-b:a", bitrate, *encoder_options,
        "-progress", "pipe:1", "-nostats", str(output),
    ]
    try:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到外部工具：{core.FFMPEG}") from exc
    last_percent = -1
    started_at = time.monotonic()
    assert process.stdout is not None
    progress_lines: queue.Queue[str | None] = queue.Queue()
    output_tail: list[str] = []

    def drain_output() -> None:
        try:
            for value in process.stdout:
                progress_lines.put(value)
        finally:
            progress_lines.put(None)

    threading.Thread(target=drain_output, daemon=True).start()
    try:
        while True:
            core.check_cancel(cancel_event)
            try:
                line = progress_lines.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None:
                    continue
                continue
            if line is None:
                break
            if line:
                output_tail.append(line.strip())
                if len(output_tail) > 30:
                    output_tail.pop(0)
                key, separator, raw_value = line.strip().partition("=")
                if separator and key in {"out_time_us", "out_time_ms"} and duration_seconds > 0:
                    try:
                        elapsed = int(raw_value) / 1_000_000
                    except ValueError:
                        elapsed = 0.0
                    percent = max(0, min(99, int(elapsed / duration_seconds * 100)))
                    if percent > last_percent:
                        last_percent = percent
                        elapsed_wall = max(0.1, time.monotonic() - started_at)
                        remaining = int(elapsed_wall * (100 - percent) / percent) if percent else 0
                        eta = f"，预计剩余 {remaining} 秒" if percent else ""
                        log(f"兼容音频进度：{percent}%{eta}")
    except BaseException:
        core.terminate_process_tree(process)
        raise
    process.wait()
    if process.returncode != 0:
        raise RuntimeError("\n".join(output_tail).strip() or f"{target_format} 音轨转换失败。")
    log("兼容音频进度：100%")
    if not output.exists() or output.stat().st_size < 1024:
        raise RuntimeError(f"未生成有效的 {target_format} 音轨。")
    return output


def _finalize_with_audio(
    base_path: Path,
    final_path: Path,
    generated_audio: list[tuple[Path, str, str]],
    keep_base_audio: bool,
    input_path: str,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
    generated_audio_default: bool = False,
) -> None:
    partial = core.partial_output_path(str(final_path))
    core.remove_incomplete_file(partial, log)
    args = [core.MKVMERGE, "-o", str(partial)]
    if not keep_base_audio:
        args.append("--no-audio")
    elif generated_audio_default:
        for track in core.inspect_tracks(str(base_path)):
            if track.type == "audio":
                args += ["--default-track", f"{track.id}:no"]
    args.append(str(base_path))
    for index, (audio_path, label, language) in enumerate(generated_audio):
        is_default = index == 0 and (generated_audio_default or not keep_base_audio)
        mkv_language = core.LANGUAGES.get(language, ("eng", language, language))[0]
        args += [
            "--language",
            f"0:{mkv_language}",
            "--track-name",
            f"0:{label} Compatible",
            "--default-track",
            f"0:{'yes' if is_default else 'no'}",
            str(audio_path),
        ]
    log(f"正在封装批量输出：{partial}")
    try:
        core.run_command(args, log=log, cancel_event=cancel_event)
        input_media = core.inspect_media(input_path)
        output_media = core.inspect_media(str(partial))
        expected_audio = sum(1 for item in output_media.get("tracks", []) if item.get("type") == "audio")
        expected_subs = sum(1 for item in output_media.get("tracks", []) if item.get("type") == "subtitles")
        core.validate_media_output(
            partial, input_media, expected_audio, expected_subs, None, log,
            input_path=input_path, cancel_event=cancel_event,
        )
        os.replace(partial, final_path)
    except BaseException:
        core.remove_incomplete_file(partial, log)
        raise


def process_plan(
    plan: BatchPlan,
    profile: PreferenceProfile,
    log: Callable[[str], None],
    cancel_event: threading.Event | None,
    parallel_targets: int = 2,
    ai_gate: threading.Semaphore | None = None,
    allow_online_search: bool = True,
) -> str:
    if plan.status not in {"ready", "review", "processing"}:
        raise RuntimeError(f"该影片尚未满足处理条件：{plan.status_label}")
    if plan.external_subtitle_verified and not Path(plan.external_subtitle).is_file():
        raise RuntimeError("已验证英文字幕文件不存在，已阻止静默回退到图片字幕或其他来源。")
    if plan.manual_confirmation_hash and (
        not Path(plan.external_subtitle).is_file()
        or hashlib.sha256(Path(plan.external_subtitle).read_bytes()).hexdigest()
        != plan.manual_confirmation_hash
    ):
        raise RuntimeError("人工确认的字幕文件已变化，必须重新核听确认。")
    if plan.source_mode == "online" and not plan.external_subtitle_verified:
        raise RuntimeError("在线字幕尚未通过音轨时间轴预检，已阻止进入正式处理。")
    if plan.source_mode == "online" and plan.external_subtitle_provider:
        import smart_subtitles

        smart_subtitles.verify_before_processing(
            plan.path,
            plan.external_subtitle,
            plan.external_subtitle_provider,
            plan.external_subtitle_release,
            plan.external_subtitle_identity_key,
            plan.external_subtitle_seal,
        )
    core.check_cancel(cancel_event)
    source = Path(plan.path)
    output = Path(plan.output_path)
    work = Path(str(output.with_suffix("")) + "_work")
    work.mkdir(parents=True, exist_ok=True)
    log = persistent_process_logger(plan, log)
    log("处理阶段开始；后续搜索、身份、时间轴与翻译信息将保存到本片日志。")
    base_output = work / "base-processed.mkv"
    if output.exists() and output.resolve() == source.resolve():
        raise RuntimeError("输出文件不能覆盖原影片。")

    all_tracks = core.inspect_tracks(plan.path)
    main_track = _main_audio([track for track in all_tracks if track.type == "audio"])
    if not main_track:
        raise RuntimeError("影片没有可用音轨。")
    runtime_duration = _duration_from_track_statistics(all_tracks)
    _refresh_incomplete_subtitle_selections(
        plan,
        profile,
        all_tracks,
        runtime_duration,
        log,
    )
    targets = list(plan.missing_subtitle_languages)
    local_conversion_targets = [
        code for code in targets if code in plan.local_chinese_conversion_sources
    ]
    translation_candidates = [
        code for code in targets if code not in plan.local_chinese_conversion_sources
    ]
    source_track = next(
        (
            track for track in all_tracks
            if track.type == "subtitles" and track.id == plan.source_subtitle_id
        ),
        None,
    )
    retained_image_tracks = [
        track for track in all_tracks
        if track.id in plan.subtitle_ids and _is_image_subtitle(track)
    ]
    anchor_candidates = ordered_text_anchors(all_tracks, main_track, runtime_duration)
    embedded_timeline_anchor = None
    prepared_embedded_anchor = None
    prepared_image_timing = None
    shared_audio_reference = None
    shared_content_audio = None
    trust_embedded_original_timeline = not allow_online_search
    if not allow_online_search:
        log("无搜索处理：沿用现有字幕时间轴，不启动在线搜索或自动纠偏。")
    elif not (plan.external_subtitle_verified and Path(plan.external_subtitle).is_file()):
        for candidate in anchor_candidates:
            core.check_cancel(cancel_event)
            status = {}
            try:
                if shared_content_audio is None:
                    shared_content_audio = pro_core.prepare_shared_subtitle_content_audio(
                        plan.path, main_track.id, work, log, cancel_event)
                    shared_audio_reference = shared_content_audio.vad_reference
                pro_core.prepare_embedded_text_corrections(
                    plan.path, [candidate.id], candidate.id, shared_content_audio.validation_audio_id,
                    str(work), log, cancel_event, shared_audio_reference=shared_audio_reference,
                    shared_content_audio=shared_content_audio, verification_status=status)
            except core.CancelledError:
                raise
            except (pro_core.SubtitleVerificationToolError, pro_core.SubtitlePreflightTimeoutError):
                raise
            except Exception as exc:
                log(f"内嵌文本轨 {candidate.id} 未能建立标杆：{exc}")
                continue
            if status.get("verified_anchor"):
                embedded_timeline_anchor = candidate
                prepared_embedded_anchor = status.get("prepared_anchor")
                source_track = candidate
                plan.source_subtitle_id = candidate.id
                if translation_candidates:
                    plan.source_mode = "embedded"
                log(f"选用内嵌{language_label(candidate.language)}文本轨 {candidate.id}作为时间标杆，不下载替代文本。")
                break
        if anchor_candidates and embedded_timeline_anchor is None:
            plan.source_subtitle_id = None
            log("内嵌文本均未建立可用标杆，随后尝试下载英文文本。")

    verified_english = (
        plan.external_subtitle_verified
        and normalize_language(plan.external_language) == "en"
        and Path(plan.external_subtitle).is_file()
    )
    external_source_usable = bool(
        plan.external_subtitle_verified
        and Path(plan.external_subtitle).is_file()
    )
    if not allow_online_search and translation_candidates:
        audio_language = normalize_language(main_track.language)
        external_language = normalize_language(plan.external_language)
        embedded_offline_source = _offline_source_track(
            all_tracks,
            main_track,
            runtime_duration,
        )
        embedded_language = (
            _subtitle_language(embedded_offline_source)
            if embedded_offline_source is not None
            else "und"
        )
        external_is_same_language = bool(
            external_source_usable
            and audio_language != "und"
            and external_language == audio_language
        )
        embedded_is_same_language = bool(
            embedded_offline_source is not None
            and audio_language != "und"
            and embedded_language == audio_language
        )
        if external_is_same_language:
            plan.source_mode = "external"
            plan.source_subtitle_id = None
        elif embedded_is_same_language:
            plan.source_mode = "embedded"
            plan.source_subtitle_id = embedded_offline_source.id
        elif verified_english:
            plan.source_mode = "external"
            plan.source_subtitle_id = None
        elif embedded_offline_source is not None:
            plan.source_mode = "embedded"
            plan.source_subtitle_id = embedded_offline_source.id
        elif external_source_usable:
            plan.source_mode = "external"
            plan.source_subtitle_id = None
        else:
            reason = (
                "无搜索处理已跳过：影片没有完整字幕，或仅有片段/强制字幕；"
                "无法在不下载字幕的情况下生成所需字幕。"
            )
            log(reason)
            raise OfflineSubtitleUnavailableError(reason)

        if plan.source_mode == "embedded":
            chosen = next(
                track for track in all_tracks
                if track.type == "subtitles" and track.id == plan.source_subtitle_id
            )
            log(
                f"无搜索处理选用现有{language_label(chosen.language)}"
                f"{'文本' if chosen.text_subtitle else '图片'}字幕作为时间轴参照；"
                "该时间轴未经核验。"
            )
        else:
            log(
                f"无搜索处理选用已导入的{language_label(plan.external_language)}文本字幕"
                "作为时间轴参照；本次不再次核验或纠偏。"
            )
    elif translation_candidates and external_source_usable:
        plan.source_mode = "online"
        plan.source_subtitle_id = None
        log("已锁定通过核验的文本字幕；正式处理沿用此字幕及其已确认时间轴。")
    log(f"开始批量项：{source.name}")
    needs_translation_english_source = bool(
        translation_candidates
        and embedded_timeline_anchor is None
        and not external_source_usable
    )
    needs_image_timeline_anchor = bool(
        retained_image_tracks
        and embedded_timeline_anchor is None
        and not external_source_usable
    )
    needs_text_timeline_anchor = bool(
        anchor_candidates and embedded_timeline_anchor is None and not external_source_usable
    )
    needs_english_text_source = bool(
        needs_translation_english_source or needs_image_timeline_anchor or needs_text_timeline_anchor
    )
    if needs_english_text_source and allow_online_search:
        import smart_subtitles

        if needs_image_timeline_anchor and not needs_translation_english_source:
            if profile.replace_downloaded_subtitle:
                log(
                    "需要校正原图片字幕时间轴，正在搜索可靠英文文本字幕；"
                    "核验通过后按偏好写入成品并替换同语言内嵌字幕。"
                )
            else:
                log(
                    "需要校正原图片字幕时间轴，正在搜索可靠英文文本字幕作为临时时间标杆；"
                    "核验后不自动写入成品。"
                )
        elif needs_text_timeline_anchor and not needs_translation_english_source:
            log("内嵌文本未能完成纠偏，正在搜索英文文本作为时间标杆。")
        elif plan.has_image_subtitles:
            log("检测到图片字幕且需要新增字幕，优先搜索可靠英文文本字幕以避免耗时 OCR。")
        else:
            log("缺少完整英文字幕来源，正在自动搜索并严格核验可靠英文文本字幕。")
        required_image_ids = [track.id for track in retained_image_tracks
            if not (profile.replace_downloaded_subtitle and normalize_language(track.language) == 'en')]
        candidate_image_timing = None

        def prepare_candidate_images(preparation_cancel):
            pro_core.prepare_shared_image_timing(
                plan.path, required_image_ids, str(work), log, preparation_cancel)

        def check_candidate_images(aligned, candidate_cancel):
            nonlocal candidate_image_timing
            # Any retry, candidate change or failure discards the previous
            # candidate's handoff before running the actual acceptance checks.
            candidate_image_timing = None
            if not required_image_ids:
                return
            image_status = {}
            pro_core.require_image_timeline_anchor(
                plan.path, required_image_ids, main_track.id, str(work), aligned,
                log, candidate_cancel, prepared_image_status=image_status)
            candidate_image_timing = image_status.get("prepared_image_timing")

        try:
            core.check_cancel(cancel_event)
            # Online search/download phases are gated inside smart_subtitles.
            # Do not serialize film-level VAD/PGS IO or local matching here.
            smart_result = smart_subtitles.find_verified_english(
                plan.path,
                main_track.id,
                log,
                cancel_event,
                candidate_acceptance=check_candidate_images if required_image_ids else None,
                candidate_preparation=prepare_candidate_images if required_image_ids else None,
            )
        except core.CancelledError:
            raise
        except (pro_core.SubtitlePreflightTimeoutError, pro_core.SharedImageTimingPreparationError):
            # The film's audio preparation failed, not its downloaded subtitles.
            # Do not retry every subtitle or misreport this as a missing source.
            raise
        except Exception as exc:
            if needs_translation_english_source:
                raise RuntimeError(
                    "没有找到可核验的英文文本字幕来源；已停止本片，"
                    f"请手动导入或选择字幕后核听：{exc}"
                ) from exc
            raise RuntimeError(
                "保留的字幕没有可核验的英文文本时间标杆；"
                f"无法安全纠偏，已停止本片：{exc}"
            ) from exc
        else:
            core.check_cancel(cancel_event)
            if pro_core.prepared_image_timing_matches_subtitle(
                    candidate_image_timing, smart_result.subtitle_path):
                prepared_image_timing = candidate_image_timing
            if needs_translation_english_source:
                plan.source_mode = "online"
                plan.source_subtitle_id = None
            plan.external_subtitle = smart_result.subtitle_path
            plan.external_language = smart_result.language or "en"
            plan.external_subtitle_verified = True
            plan.external_subtitle_verification = smart_result.report
            plan.external_subtitle_provider = smart_result.provider
            plan.external_subtitle_release = smart_result.release
            plan.external_subtitle_identity_key = smart_result.identity_key
            plan.external_subtitle_seal = smart_result.verification_seal
            plan.external_subtitle_origin = "download"
            plan.manual_confirmation_hash = ""
            if profile.replace_downloaded_subtitle:
                log(
                    "可靠英文文本字幕已通过核验；按偏好写入成品并替换"
                    "同语言内嵌字幕，其余选中字幕参照此时间轴。"
                )
            elif needs_image_timeline_anchor and not needs_translation_english_source:
                log(
                    "可靠英文文本字幕已通过核验，仅作为图片字幕的时间标杆；"
                    "不会因而自动写入成品。"
                )
            elif needs_text_timeline_anchor and not needs_translation_english_source:
                log("下载文本已完成纠偏，将指导内嵌字幕时间轴。")
            else:
                log("可靠英文文本字幕已通过核验，将作为翻译来源。")
            plan.status = "ready"
            plan.status_label = "可以处理"
    elif needs_image_timeline_anchor and not allow_online_search:
        log(
            "无搜索处理没有取得英文文本时间标杆；保留图片字幕原时间轴，"
            "不将其标记为已核验或已纠偏。"
        )
    replace_downloaded = bool(
        profile.replace_downloaded_subtitle
        and plan.external_subtitle_verified
        and plan.external_subtitle_origin == "download"
        and Path(plan.external_subtitle).is_file()
    )
    if replace_downloaded:
        downloaded_language = normalize_language(plan.external_language)
        same_language_ids = {
            track.id for track in all_tracks
            if track.type == "subtitles"
            and _subtitle_matches_download(track, downloaded_language)
        }
        replaced_ids = same_language_ids.intersection(plan.subtitle_ids)
        plan.subtitle_ids = [
            track_id for track_id in plan.subtitle_ids
            if track_id not in replaced_ids
        ]
        plan.source_mode = "online"
        plan.source_subtitle_id = None
        replaced_conversion_targets = [
            target for target, track_id in plan.local_chinese_conversion_sources.items()
            if track_id in same_language_ids
        ]
        for target in replaced_conversion_targets:
            plan.local_chinese_conversion_sources.pop(target, None)
            if target in local_conversion_targets:
                local_conversion_targets.remove(target)
            if target not in translation_candidates:
                translation_candidates.append(target)
        if replaced_conversion_targets:
            log(
                "已改用下载字幕为简繁转换来源；不再读取被替换的内嵌中文轨。"
            )
        retained = [track for track in all_tracks if track.id in plan.subtitle_ids]
        coverage = _subtitle_coverage(
            retained, profile.subtitle_languages, profile.chinese_script_equivalent,
            (downloaded_language,),
        )
        for target in profile.subtitle_languages:
            if target not in coverage and target not in translation_candidates:
                translation_candidates.append(target)
            if target not in coverage and target not in plan.missing_subtitle_languages:
                plan.missing_subtitle_languages.append(target)
        log(
            f"按偏好保留已核验的下载{language_label(downloaded_language)}字幕；"
            f"替换成品中同语言内嵌轨 {sorted(replaced_ids)}。"
        )
    selected_image_tracks = [
        track for track in all_tracks
        if track.id in plan.subtitle_ids and _is_image_subtitle(track)
    ]
    if selected_image_tracks:
        image_labels = "、".join(
            f"轨 {track.id}（{language_label(_subtitle_language(track))}）"
            for track in selected_image_tracks
        )
        log(f"成品保留的原图片字幕：{image_labels}。")
    elif plan.has_image_subtitles:
        log("影片含有图片字幕，但当前偏好未保留这些轨道，不写入成品。")
    translation_targets = _effective_translation_targets(
        plan,
        translation_candidates,
        all_tracks,
        main_track,
    )
    if not allow_online_search and plan.source_mode == "embedded":
        offline_source = next(
            (
                track for track in all_tracks
                if track.type == "subtitles" and track.id == plan.source_subtitle_id
            ),
            None,
        )
        if offline_source is not None and _is_image_subtitle(offline_source):
            source_language = _subtitle_language(offline_source)
            # When image subtitles are excluded from the final file, OCR of the
            # same language is still a real missing target, not a duplicate.
            for target in translation_candidates:
                if normalize_language(target) == source_language and target not in translation_targets:
                    translation_targets.append(target)
    skipped_targets = [code for code in translation_candidates if code not in translation_targets]
    if skipped_targets:
        skipped_labels = "、".join(language_label(code) for code in skipped_targets)
        if translation_targets:
            remaining_labels = "、".join(language_label(code) for code in translation_targets)
            log(f"字幕来源已是{skipped_labels}，仅跳过这些语言的翻译；仍需翻译：{remaining_labels}。")
        else:
            log(f"字幕来源已是{skipped_labels}，没有其他待翻译语言，不启动本地翻译模型。")

    if local_conversion_targets:
        labels = "、".join(language_label(code) for code in local_conversion_targets)
        log(f"检测到可复用的中文文本字幕；{labels}将使用本地简繁词组转换，不启动翻译模型。")
    retained_subtitle_languages = {
        _subtitle_language(track)
        for track in all_tracks
        if track.type == "subtitles" and track.id in plan.subtitle_ids
    }
    external_source_language = normalize_language(plan.external_language)
    requested_subtitle_languages = {
        normalize_language(code) for code in profile.subtitle_languages
    }
    include_external_source_in_output = bool(
        plan.source_mode in {"external", "online"}
        and (replace_downloaded or (
            external_source_language in requested_subtitle_languages
            and external_source_language not in retained_subtitle_languages
        ))
    )
    translation_scope = _TranslationResourceScope(ai_gate, log, cancel_event)
    try:
        pro_core.process_pro(
            input_path=plan.path,
            output_path=str(base_output if plan.missing_audio_formats else output),
            keep_audio_ids=plan.audio_ids,
            keep_subtitle_ids=plan.subtitle_ids,
            source_mode=plan.source_mode,
            embedded_source_id=plan.source_subtitle_id,
            external_subtitle=plan.external_subtitle or None,
            audio_source_id=main_track.id,
            speech_language=normalize_language(main_track.language),
            source_language=plan.external_language,
            target_codes=translation_targets,
            subtitle_language_preferences=list(profile.subtitle_languages),
            chinese_script_equivalent=profile.chinese_script_equivalent,
            local_chinese_conversion_sources=plan.local_chinese_conversion_sources,
            work_dir=str(work),
            log=log,
            parallel_targets=parallel_targets,
            cancel_event=cancel_event,
            trust_embedded_original_timeline=trust_embedded_original_timeline,
            trust_external_original_timeline=(not allow_online_search or plan.external_subtitle_verified),
            allow_reverse_translation=True,
            preserve_audio_defaults=(
                profile.audio_policy == "native" or plan.audio_passthrough_warning
            ),
            shared_audio_reference=shared_audio_reference,
            shared_content_audio=shared_content_audio,
            verified_timeline_reference=(
                plan.external_subtitle
                if plan.external_subtitle_verified
                and Path(plan.external_subtitle).is_file()
                else None
            ),
            include_external_source_in_output=include_external_source_in_output,
            translation_start=translation_scope.start,
            translation_end=translation_scope.finish,
            prepared_embedded_anchor=prepared_embedded_anchor,
            prepared_image_timing=prepared_image_timing,
        )
    finally:
        translation_scope.finish()
    if not plan.missing_audio_formats:
        return str(output)

    generated_source_id = plan.generated_audio_source_id or main_track.id
    generated = [
        (
            _transcode_audio(
                plan.path,
                generated_source_id,
                fmt,
                work / f"compatible-{fmt}",
                log,
                cancel_event,
            ),
            fmt,
            "en",
        )
        for fmt in plan.missing_audio_formats
    ]
    _finalize_with_audio(
        base_output,
        output,
        generated,
        bool(plan.audio_ids),
        plan.path,
        log,
        cancel_event,
        generated_audio_default=profile.audio_policy == "compact",
    )
    if base_output.exists():
        base_output.unlink()
    log(f"批量项完成：{output}")
    return str(output)


def videos_in_folder(folder: str) -> list[str]:
    videos: list[str] = []
    for current_dir, dir_names, file_names in os.walk(folder):
        dir_names.sort(key=str.casefold)
        for file_name in sorted(file_names, key=str.casefold):
            path = Path(current_dir, file_name)
            if is_output_path(path):
                continue
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                videos.append(str(path))
    return videos


def enough_space(paths: list[str]) -> tuple[bool, str]:
    by_drive: dict[str, int] = {}
    for value in paths:
        path = Path(value)
        root = path.anchor
        by_drive[root] = by_drive.get(root, 0) + path.stat().st_size * 2
    for root, required in by_drive.items():
        free = shutil.disk_usage(root).free
        if free < required:
            return False, f"{root} 可用 {free / 1024**3:.1f} GiB，批量处理中建议至少 {required / 1024**3:.1f} GiB。"
    return True, "磁盘空间满足当前批次的保守估算。"
