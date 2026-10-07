# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from typing import Callable

import strict_inspection
import subtitle_tool_core as core


def _track_specs(media: dict, track_type: str) -> list[dict]:
    return [track for track in media.get("tracks", []) if track.get("type") == track_type]


def verify_output(
    output_path: str,
    source_path: str | None = None,
    log: Callable[[str], None] = core.log_noop,
) -> dict:
    output = Path(output_path)
    report = strict_inspection.inspect_video(str(output), log=log)
    reasons = list(report.get("reason_codes", []))

    lowered_name = output.name.lower()
    if not (lowered_name.endswith(".mkv") or lowered_name.endswith(".mkv.ready")):
        reasons.append("output_container_must_be_mkv")
    if lowered_name.endswith((".partial.mkv", ".uploading", ".copying")):
        reasons.append("temporary_output_name_not_allowed")
    if output.stat().st_size < 1024 * 1024:
        reasons.append("output_file_too_small")

    comparison = {
        "source_provided": bool(source_path),
        "duration_match": None,
        "video_codec_match": None,
        "video_track_count_match": None,
        "audio_track_count_match": None,
    }
    if source_path:
        source = Path(source_path)
        if not source.is_file():
            reasons.append("source_not_found")
        else:
            source_media = core.inspect_media(str(source))
            output_media = core.inspect_media(str(output))
            source_video = _track_specs(source_media, "video")
            output_video = _track_specs(output_media, "video")
            source_audio = _track_specs(source_media, "audio")
            output_audio = _track_specs(output_media, "audio")
            comparison["video_track_count_match"] = len(source_video) == len(output_video)
            comparison["audio_track_count_match"] = len(source_audio) == len(output_audio)
            comparison["video_codec_match"] = [track.get("codec") for track in source_video] == [
                track.get("codec") for track in output_video
            ]
            source_duration = core.video_track_duration_ns(source_media) or core.media_duration_ns(source_media)
            output_duration = core.video_track_duration_ns(output_media) or core.media_duration_ns(output_media)
            tolerance = max(3_000_000_000, int(source_duration * 0.001)) if source_duration > 0 else 0
            comparison["duration_match"] = (
                source_duration > 0
                and output_duration > 0
                and abs(source_duration - output_duration) <= tolerance
            )
            if not comparison["video_track_count_match"]:
                reasons.append("video_track_count_mismatch")
            if not comparison["audio_track_count_match"]:
                reasons.append("audio_track_count_mismatch")
            if not comparison["video_codec_match"]:
                reasons.append("video_codec_mismatch")
            if not comparison["duration_match"]:
                reasons.append("duration_mismatch")

    passed = report.get("status") == "accepted" and not reasons
    return {
        "schema_version": 1,
        "command": "verify",
        "status": "verified" if passed else "verification_failed",
        "output_path": str(output),
        "source_path": str(source_path or ""),
        "reason_codes": list(dict.fromkeys(reasons)),
        "inspection": report,
        "source_comparison": comparison,
        "validation": {"passed": passed},
    }
