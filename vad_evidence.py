"""Lightweight evidence checks for an already decoded continuous VAD sample.

These are engineering safeguards, not a probability of correct subtitles.
No audio is read or decoded. All timestamps must already share the NPZ's local
origin; this module neither adds a crop origin nor a container start delta.
The correlation deliberately reproduces the installed fixed-offset ffsubsync
engine, including its cue cap, metadata exclusions, rounding and search range.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from zipfile import BadZipFile

import numpy as np

FRAME_RATE = 100
MAX_OFFSET_SECONDS = 10
DIAGNOSTIC_OFFSET_SECONDS = 60
DIAGNOSTIC_GUARD_SECONDS = 70
MIN_DIALOGUE_CUES = 40
MIN_SIGNAL_SECONDS = 30
MIN_DIAGNOSTIC_DURATION_SECONDS = 2 * DIAGNOSTIC_GUARD_SECONDS + 2 * MIN_SIGNAL_SECONDS
REGION_COUNT = 5
MIN_CUES_PER_REGION = 5
MIN_COVERED_REGIONS = 3
ALTERNATIVE_PEAK_RATIO = .95
MAX_PEAK_WIDTH_SECONDS = 1.0

_TIME = re.compile(r"^(-?)(\d+):([0-5]\d):([0-5]\d)[,.](\d{1,6})$")
_MARKUP = re.compile(r"<[^>]+>|\{\\[^}]*\}")
_ENGINE_MARKUP = re.compile(r"<[^>]+>")
_PAIRS = {"(": ")", "{": "}", "[": "]", "（": "）", "【": "】", "「": "」"}
_MUSIC = frozenset("♪♫♬♩🎵🎶")


@dataclass(frozen=True)
class _Cue:
    start: float
    end: float
    text: str


def _seconds(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid cue timestamp")
    if isinstance(value, (int, float, np.number)):
        seconds = float(value)
    else:
        match = _TIME.fullmatch(str(value).strip())
        if not match:
            raise ValueError("Invalid cue timestamp")
        sign, hours, minutes, seconds_part, fraction = match.groups()
        microseconds = (int(hours) * 3600 + int(minutes) * 60 + int(seconds_part)) * 1_000_000
        microseconds += int(fraction.ljust(6, "0"))
        seconds = microseconds / 1_000_000
        if sign:
            seconds = -seconds
    if not math.isfinite(seconds):
        raise ValueError("Invalid cue timestamp")
    return seconds


def _cues(events: Sequence[Any]) -> list[_Cue]:
    result = []
    for event in events:
        if isinstance(event, Mapping):
            start, end, text = event["start"], event["end"], event["text"]
        else:
            start, end, text = event.start, event.end, event.text
        start, end = _seconds(start), _seconds(end)
        if end <= start:
            raise ValueError("Invalid cue interval")
        # ffsubsync's parser drops negative starts before deciding edge metadata.
        if start < 0:
            continue
        result.append(_Cue(start, min(end, start + 10.0), str(text)))
    return result


def _engine_metadata(text: str, edge: bool) -> bool:
    text = _ENGINE_MARKUP.sub("", text).strip()
    return (not text or (text[0] in _PAIRS and text[-1] == _PAIRS[text[0]])
            or all(char.isspace() or char in _MUSIC for char in text)
            or (edge and ("english" in text.lower() or " - " in text)))


def _dialogue(cue: _Cue, edge: bool) -> bool:
    # Lyric cues can participate in the engine's signal; they are not counted
    # as dialogue evidence. Keep that distinction out of the FFT reproduction.
    text = _MARKUP.sub("", cue.text).strip()
    return not _engine_metadata(cue.text, edge) and not any(char in _MUSIC for char in text)


def _subtitle_signal(cues: Sequence[_Cue]) -> np.ndarray:
    max_time = max((cue.end for cue in cues), default=0.0)
    signal = np.zeros(int(max_time * FRAME_RATE) + 2, dtype=np.float64)
    for index, cue in enumerate(cues):
        if _engine_metadata(cue.text, index == 0 or index + 1 == len(cues)):
            continue
        start = int(round(cue.start * FRAME_RATE))
        # Engine rounds duration separately, rather than rounding cue.end.
        end = start + int(round((cue.end - cue.start) * FRAME_RATE))
        signal[start:end] = 1.0
    return signal


def _peak_metrics(values: np.ndarray, offsets: np.ndarray) -> dict[str, Any]:
    best = int(np.argmax(values))
    median = float(np.median(values))
    contrast = float(values[best]) - median
    remote = np.abs(offsets - offsets[best]) >= 1.0 - 1e-9
    ratio = None
    if contrast > 1e-6 and remote.any():
        ratio = (float(np.max(values[remote])) - median) / contrast
    threshold = float(values[best]) - .05 * max(contrast, 0.0)
    left = right = best
    while left > 0 and values[left - 1] >= threshold:
        left -= 1
    while right + 1 < len(values) and values[right + 1] >= threshold:
        right += 1
    return {"offset_seconds": float(offsets[best]),
            "alternative_peak_ratio": ratio,
            "peak_width_seconds": (right - left) / FRAME_RATE,
            "peak_contrast": contrast}


def _correlate(reference: np.ndarray, subtitle: np.ndarray) -> dict[str, Any]:
    total = len(reference) + len(subtitle)
    fft_length = 1 << (total - 1).bit_length()
    extra = fft_length - total
    subtitle_fft = np.fft.rfft(np.pad(2 * subtitle - 1, (extra + len(reference), 0)))
    reference_fft = np.fft.rfft(np.flip(np.pad(2 * reference - 1, (0, len(subtitle) + extra))))
    correlation = np.fft.irfft(subtitle_fft * reference_fft, n=fft_length)
    center = fft_length - 1 - len(subtitle)
    # Exactly as FFTAligner: +10.00 is included, -10.00 is excluded.
    diagnostic_enabled = len(reference) >= MIN_DIAGNOSTIC_DURATION_SECONDS * FRAME_RATE
    radius = (DIAGNOSTIC_OFFSET_SECONDS if diagnostic_enabled else MAX_OFFSET_SECONDS) * FRAME_RATE
    indexes = np.arange(max(0, center - radius), min(fft_length, center + radius))
    values = correlation[indexes]
    offsets = (center - indexes) / FRAME_RATE
    narrow = (offsets > -MAX_OFFSET_SECONDS) & (offsets <= MAX_OFFSET_SECONDS)
    metrics = _peak_metrics(values[narrow], offsets[narrow])
    metrics.update({"diagnostic_evaluated": diagnostic_enabled,
                    "diagnostic_offset_seconds": None,
                    "outside_peak_ratio": None, "outside_range_peak": False})
    if diagnostic_enabled:
        # The FFT is already complete. Inspect another slice of that same
        # array, using one background for both peak strengths. A strong peak
        # outside the application range is a reason to reject this candidate,
        # never a permission to apply a larger offset or decode more audio.
        wide = _peak_metrics(values, offsets)
        baseline = float(np.median(values))
        inside_gain = float(np.max(values[narrow])) - baseline
        wide_gain = float(np.max(values)) - baseline
        ratio = inside_gain / wide_gain if wide_gain > 1e-6 else None
        outside = not (-MAX_OFFSET_SECONDS < wide["offset_seconds"] <= MAX_OFFSET_SECONDS)
        distinct = abs(wide["offset_seconds"] - metrics["offset_seconds"]) >= 1.0 - 1e-9
        metrics.update({"diagnostic_offset_seconds": wide["offset_seconds"],
                        "outside_peak_ratio": ratio,
                        "outside_range_peak": bool(outside and distinct and ratio is not None
                                                   and ratio <= ALTERNATIVE_PEAK_RATIO)})
    return metrics


def subtitle_guard_seconds(sample_duration_seconds: float) -> int:
    """Use a safe common interior for matching and the wider peak diagnostic.

    Short samples retain their existing ten-second matching guard and range;
    they cannot provide the interior needed for a sixty-second diagnostic.
    """
    return (DIAGNOSTIC_GUARD_SECONDS
            if sample_duration_seconds >= MIN_DIAGNOSTIC_DURATION_SECONDS else MAX_OFFSET_SECONDS)


def evaluate_reference(reference: Path, local_events: list,
                       raw_offset: float | None = None) -> dict[str, Any]:
    """Inspect signal support, then optionally audit a fixed-offset tool result.

    ``raw_offset=None`` performs coverage checks only, without building a
    subtitle signal or running an FFT. ``expandable`` is true only for missing
    evidence; range/coordinate/data errors must not silently expand or pass.
    Returned offsets are LOCAL raw offsets, never corrected movie offsets.
    """
    result: dict[str, Any] = {
        "sufficient": False, "reasons": [], "cue_count": 0,
        "spoken_seconds": 0.0, "quiet_seconds": 0.0, "covered_regions": 0,
        "region_cue_counts": [0] * REGION_COUNT, "alternative_peak_ratio": None,
        "peak_width_seconds": None, "offset_seconds": None,
        "expandable": False, "range_limited": False,
        "tool_coordinate_invalid": False, "fft_evaluated": False,
        "diagnostic_evaluated": False, "diagnostic_offset_seconds": None,
        "outside_peak_ratio": None, "outside_range_peak": False,
    }
    reasons = result["reasons"]
    try:
        with np.load(Path(reference), allow_pickle=False) as stored:
            speech = np.asarray(stored["speech"], dtype=np.float64).copy()
        if speech.ndim != 1 or not len(speech) or not np.isfinite(speech).all():
            raise ValueError("Invalid reference frames")
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        reasons.append("reference_invalid")
        return result
    duration = len(speech) / FRAME_RATE
    # DeserializeSpeechTransformer maps every value below 1 to non-speech.
    speech[speech < 1.0] = 0.0
    result["spoken_seconds"] = float(np.count_nonzero(speech >= 1.0) / FRAME_RATE)
    result["quiet_seconds"] = duration - result["spoken_seconds"]
    try:
        cues = _cues(local_events)
    except (ValueError, KeyError, AttributeError, TypeError):
        reasons.append("cue_coordinate_invalid")
        return result
    # A sample-local guarded cue list cannot extend hours beyond its NPZ.
    # Reject coordinate mistakes before allocating the subtitle signal.
    if max((cue.end for cue in cues), default=0) > duration + 20.0:
        reasons.append("cue_coordinate_invalid")
        return result
    bins = [0] * REGION_COUNT
    for index, cue in enumerate(cues):
        if cue.start >= duration or not _dialogue(cue, index == 0 or index + 1 == len(cues)):
            continue
        midpoint = (cue.start + min(cue.end, duration)) / 2
        bins[min(REGION_COUNT - 1, int(midpoint / duration * REGION_COUNT))] += 1
    result["region_cue_counts"] = bins
    result["cue_count"] = sum(bins)
    result["covered_regions"] = sum(count >= MIN_CUES_PER_REGION for count in bins)
    if result["cue_count"] < MIN_DIALOGUE_CUES:
        reasons.append("too_few_dialogue_cues")
    if result["spoken_seconds"] < MIN_SIGNAL_SECONDS:
        reasons.append("too_little_speech")
    if result["quiet_seconds"] < MIN_SIGNAL_SECONDS:
        reasons.append("too_little_quiet")
    if result["covered_regions"] < MIN_COVERED_REGIONS:
        reasons.append("dialogue_concentrated")
    if raw_offset is not None:
        try:
            offset = _seconds(raw_offset)
        except ValueError:
            reasons.append("tool_coordinate_invalid")
            result["tool_coordinate_invalid"] = True
            return result
        result["offset_seconds"] = offset
        if abs(offset) >= 9.9:
            reasons.append("range_limited")
            result["range_limited"] = True
            return result
        # Empty or unsupported samples can stop before the FFT as well.
        if cues:
            metrics = _correlate(speech, _subtitle_signal(cues))
            result.update({key: metrics[key] for key in
                            ("alternative_peak_ratio", "peak_width_seconds")})
            result.update({key: metrics.get(key, result[key]) for key in
                           ("diagnostic_evaluated", "diagnostic_offset_seconds",
                            "outside_peak_ratio", "outside_range_peak")})
            result["fft_evaluated"] = True
            result["computed_offset_seconds"] = metrics["offset_seconds"]
            if abs(metrics["offset_seconds"] - offset) > .0500001:
                reasons.append("tool_coordinate_invalid")
                result["tool_coordinate_invalid"] = True
                return result
            if result["outside_range_peak"]:
                reasons.append("outside_range_peak")
                result["range_limited"] = True
            if metrics["peak_contrast"] <= 1e-6:
                reasons.append("no_distinct_peak")
            elif (metrics["alternative_peak_ratio"] is not None
                  and metrics["alternative_peak_ratio"] >= ALTERNATIVE_PEAK_RATIO):
                reasons.append("alternative_peak")
            if metrics["peak_width_seconds"] >= MAX_PEAK_WIDTH_SECONDS:
                reasons.append("broad_peak")
    result["sufficient"] = not reasons
    result["expandable"] = bool(reasons) and not result["range_limited"]
    return result
