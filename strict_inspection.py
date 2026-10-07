# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import burned_subtitle_detector
import pro_core
import subtitle_tool_core as core
from profile_model import normalize_language


REQUIRED_SUBTITLE_LANGUAGES = ("en", "zh-CN", "de")
FOREIGN_ONLY_MARKERS = (
    "foreign only", "foreign-only", "foreign parts", "foreign parts only",
    "forced", "forced only", "signs & songs", "signs/songs", "signs and songs",
    "non-english parts", "non english parts", "外语", "外語", "仅外语", "僅外語",
    "强制", "強制", "片段", "标牌", "標牌",
)
ACCESSIBILITY_MARKERS = ("hearing impaired", "hearing", "听障", "聽障")


@dataclass
class SubtitleInspection:
    track_id: int
    language: str
    original_language: str
    codec: str
    name: str
    format_kind: str
    default: bool
    forced: bool
    foreign_only: bool
    accessibility: bool
    complete: bool
    matched: bool
    suitable_translation_source: bool
    event_count: int = 0
    coverage_ratio: float | None = None
    reason_codes: list[str] = field(default_factory=list)


def _subtitle_name(track: core.Track) -> str:
    return (track.name or "").strip()


def _has_marker(value: str, markers: tuple[str, ...]) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in markers)


def _is_accessibility_subtitle(name: str) -> bool:
    lowered = name.lower()
    return _has_marker(lowered, ACCESSIBILITY_MARKERS) or bool(
        re.search(r"(?:^|[^a-z])(?:sdh|hi|cc)(?:$|[^a-z])", lowered)
    )


def _subtitle_language(track: core.Track) -> str:
    language = normalize_language(track.language)
    name = _subtitle_name(track).lower()
    if language == "zh-CN" and any(marker in name for marker in ("繁中", "繁體", "繁体", "traditional", "cht")):
        return "zh-TW"
    if language in {"zh-CN", "zh-TW", "und"} and any(
        marker in name for marker in ("简中", "簡中", "简体", "簡體", "simplified", "chs")
    ):
        return "zh-CN"
    return language


def _format_kind(track: core.Track) -> str:
    if track.text_subtitle:
        return "text"
    if track.pgs_subtitle:
        return "pgs"
    if track.vobsub_subtitle:
        return "vobsub"
    return "image_or_unknown"


def _timeline_seconds(value: str) -> float:
    try:
        return pro_core._subtitle_time_seconds(value)
    except (TypeError, ValueError):
        return 0.0


def _inspect_text_track(
    video_path: str,
    track: core.Track,
    duration_seconds: float,
    work_dir: Path,
) -> tuple[int, float | None, list[str], bool]:
    reasons: list[str] = []
    extracted = core.extract_subtitle(video_path, track, work_dir, core.log_noop)
    events = core.parse_subtitle(extracted)
    event_count = len(events)
    profile = pro_core.subtitle_completeness(events, duration_seconds)
    coverage = profile.coverage if duration_seconds > 0 else None

    if not events:
        reasons.append("subtitle_text_empty")
    elif not profile.accepted:
        reasons.append("subtitle_incomplete")
    if duration_seconds > 0:
        subtitle_end = max((_timeline_seconds(event.end) for event in events), default=0.0)
        if subtitle_end > duration_seconds + max(180.0, duration_seconds * 0.12):
            reasons.append("subtitle_timeline_too_long")
    return event_count, coverage, reasons, profile.accepted and not reasons


def _burned_status(result: burned_subtitle_detector.DetectionResult) -> str:
    if result.status == "detected" or result.detected:
        return "burned_confirmed"
    if result.status == "uncertain":
        return "burned_check_failed"
    return "burned_not_detected"


def _selection_rank(item: SubtitleInspection) -> tuple[int, int, int]:
    return (1 if item.accessibility else 0, 0 if item.default else 1, item.track_id)


def inspect_video(
    video_path: str,
    log: Callable[[str], None] = core.log_noop,
) -> dict:
    source = Path(video_path)
    if not source.is_file():
        raise FileNotFoundError(f"影片不存在：{video_path}")

    media = core.inspect_media(str(source))
    tracks = core.inspect_tracks(str(source))
    video_tracks = [track for track in tracks if track.type == "video"]
    audio_tracks = [track for track in tracks if track.type == "audio"]
    subtitle_tracks = [track for track in tracks if track.type == "subtitles"]
    duration_ns = core.video_track_duration_ns(media) or core.media_duration_ns(media)
    duration_seconds = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0

    usable_existing_subtitles = [
        track
        for track in subtitle_tracks
        if (
            not track.forced
            and (track.text_subtitle or track.pgs_subtitle or track.vobsub_subtitle)
            and not _has_marker(_subtitle_name(track), FOREIGN_ONLY_MARKERS)
        )
    ]
    if usable_existing_subtitles:
        log("已有可用字幕轨，跳过画面烧录字幕检查。")
        burned = burned_subtitle_detector.DetectionResult(
            False,
            0,
            0,
            "已有可用字幕轨，未启动烧录字幕检查",
            "absent",
            1.0,
        )
        burned_status = "burned_not_detected"
    else:
        log("正在检查画面烧录字幕。")
        try:
            burned = burned_subtitle_detector.detect(
                str(source), media, [track.language for track in audio_tracks]
            )
            burned_status = _burned_status(burned)
        except Exception as exc:
            burned = burned_subtitle_detector.DetectionResult(
                False, 0, 0, f"烧录字幕检查失败：{exc}"
            )
            burned_status = "burned_check_failed"

    inspected_subtitles: list[SubtitleInspection] = []
    with tempfile.TemporaryDirectory(prefix="subflow-inspect-") as temporary:
        work = Path(temporary)
        for track in subtitle_tracks:
            language = _subtitle_language(track)
            name = _subtitle_name(track)
            foreign_only = bool(track.forced) or _has_marker(name, FOREIGN_ONLY_MARKERS)
            accessibility = _is_accessibility_subtitle(name)
            reasons: list[str] = []
            event_count = 0
            coverage: float | None = None
            complete = False

            if not track.text_subtitle:
                reasons.append("image_subtitle_not_allowed")
            elif foreign_only:
                reasons.append("forced_or_fragment_subtitle")
            else:
                try:
                    event_count, coverage, text_reasons, complete = _inspect_text_track(
                        str(source), track, duration_seconds, work / f"track-{track.id}"
                    )
                    reasons.extend(text_reasons)
                except Exception as exc:
                    reasons.append("subtitle_extraction_failed")
                    log(f"字幕轨 {track.id} 提取检查失败：{exc}")

            if language not in REQUIRED_SUBTITLE_LANGUAGES:
                reasons.append("subtitle_language_not_required")
            matched = complete and not foreign_only
            suitable_source = matched and track.text_subtitle and language == "en"
            inspected_subtitles.append(
                SubtitleInspection(
                    track_id=track.id,
                    language=language,
                    original_language=track.language,
                    codec=track.codec,
                    name=name,
                    format_kind=_format_kind(track),
                    default=track.default,
                    forced=track.forced,
                    foreign_only=foreign_only,
                    accessibility=accessibility,
                    complete=complete,
                    matched=matched,
                    suitable_translation_source=suitable_source,
                    event_count=event_count,
                    coverage_ratio=round(coverage, 4) if coverage is not None else None,
                    reason_codes=list(dict.fromkeys(reasons)),
                )
            )

    selected: dict[str, SubtitleInspection] = {}
    for language in REQUIRED_SUBTITLE_LANGUAGES:
        candidates = [
            item for item in inspected_subtitles
            if item.language == language and item.complete and item.format_kind == "text" and not item.foreign_only
        ]
        if candidates:
            selected[language] = min(candidates, key=_selection_rank)

    missing = [language for language in REQUIRED_SUBTITLE_LANGUAGES if language not in selected]
    default_subtitles = [item for item in inspected_subtitles if item.default]
    english_default = (
        len(default_subtitles) == 1
        and selected.get("en") is default_subtitles[0]
    )
    forbidden = [
        item for item in inspected_subtitles
        if item.format_kind != "text"
        or item.language not in REQUIRED_SUBTITLE_LANGUAGES
        or not item.complete
        or item.foreign_only
        or selected.get(item.language) is not item
    ]
    reason_codes: list[str] = []
    if not video_tracks:
        reason_codes.append("video_track_missing")
    if not audio_tracks:
        reason_codes.append("audio_track_missing")
    if burned_status != "burned_not_detected":
        reason_codes.append(burned_status)
    if missing:
        reason_codes.extend(f"missing_complete_text_subtitle:{language}" for language in missing)
    if forbidden:
        reason_codes.append("forbidden_or_extra_subtitle_tracks_present")
    if not missing and not english_default:
        reason_codes.append("default_subtitle_must_be_english")

    if burned_status != "burned_not_detected" or not video_tracks or not audio_tracks:
        status = "needs_review"
    elif missing or forbidden or not english_default:
        status = "needs_processing"
    else:
        status = "accepted"

    stat = source.stat()
    return {
        "schema_version": 1,
        "command": "inspect",
        "status": status,
        "source": {
            "path": str(source),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        },
        "burned_subtitle_check": {
            "status": burned_status,
            "evidence_count": burned.evidence_count,
            "sample_count": burned.sample_count,
            "detail": burned.detail,
        },
        "media": {
            "duration_seconds": round(duration_seconds, 3),
            "video_track_count": len(video_tracks),
            "audio_track_count": len(audio_tracks),
            "subtitle_track_count": len(subtitle_tracks),
        },
        "required_subtitle_languages": list(REQUIRED_SUBTITLE_LANGUAGES),
        "missing_subtitle_languages": missing,
        "selected_subtitle_tracks": {
            language: item.track_id for language, item in selected.items()
        },
        "subtitle_tracks": [asdict(item) for item in inspected_subtitles],
        "forbidden_subtitle_track_ids": [item.track_id for item in forbidden],
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "validation": {
            "passed": status == "accepted",
            "burned_subtitles_absent": burned_status == "burned_not_detected",
            "video_present": bool(video_tracks),
            "audio_present": bool(audio_tracks),
            "required_text_subtitles_complete": not missing,
            "forbidden_subtitles_absent": not forbidden,
            "english_subtitle_is_default": english_default,
        },
    }
