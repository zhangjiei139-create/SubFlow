# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import math
import re
import statistics
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import audio_offset_verifier
import subtitle_tool_core as legacy


MIN_STAGE32_RESIDUAL = 0.15
MAX_MICRO_OFFSET = 0.80
MAX_FINE_SPREAD = 0.35
MIN_FINE_ANCHORS = 3
MAX_FINE_ANCHORS = 5
DEFAULT_TIME_BUDGET = 10.0
CANONICAL_INDEX_OVERLAP = 0.70
CANONICAL_TIME_OVERLAP = 0.70
CLIP_EDGE_MARGIN = 0.50
CLIP_RETRY_PADDING = 1.75


@dataclass(frozen=True)
class FineWord:
    start: float
    end: float
    text: str
    probability: float


@dataclass(frozen=True)
class FineAnchor:
    clip_index: int
    subtitle_time: float
    predicted_time: float
    text: str
    text_score: float
    probability: float
    micro_delta: float
    stage32_residual: float


@dataclass(frozen=True)
class FineAlignmentResult:
    accepted: bool
    suggested_micro_offset: float
    anchors: tuple[FineAnchor, ...]
    spread: float
    mad: float
    confidence: str
    reason: str
    elapsed_seconds: float
    skipped: bool = False

    @property
    def individual_deltas(self) -> tuple[float, ...]:
        return tuple(anchor.micro_delta for anchor in self.anchors)


@dataclass(frozen=True)
class _MatchDiagnostic:
    accepted: bool
    micro_delta: float | None
    score: float
    probability: float
    observed_text: str
    matched_text: str
    matched_start: float | None
    matched_end: float | None
    word_count: int
    has_word_timestamps: bool
    raw_match_count: int
    canonical_match_count: int
    reason: str

@dataclass(frozen=True)
class _Cue:
    start: float
    end: float
    text: str


def _seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return re.findall(r"(?:[a-z0-9']{2,}|[ai])", normalized)


def _clean_cue(event) -> _Cue | None:
    text = re.sub(r"<[^>]+>|\{\\[^}]*\}", " ", event.text or "")
    if any(marker in text for marker in ("♪", "♫", "[", "]", "(", ")")):
        return None
    if "\n-" in text or text.count("\n") > 1:
        return None
    words = _words(text)
    start = _seconds(event.start)
    end = _seconds(event.end)
    duration = end - start
    if not 4 <= len(words) <= 14 or not 0.6 <= duration <= 6.0:
        return None
    if len(re.findall(r"[.!?]", text)) > 1:
        return None
    return _Cue(start, end, " ".join(words))


def should_run(stage32: audio_offset_verifier.AudioOffsetResult) -> tuple[bool, str]:
    if not stage32.accepted or stage32.offset_seconds is None:
        return False, "Stage 3.2 未通过"
    if len(stage32.anchors) < MIN_FINE_ANCHORS:
        return False, f"Stage 3.2 只有 {len(stage32.anchors)} 个锚点"
    if not stage32.residuals:
        return False, "Stage 3.2 没有可用残差"
    maximum = max(stage32.residuals)
    if maximum <= MIN_STAGE32_RESIDUAL:
        return False, f"Stage 3.2 最大残差仅 {maximum:.2f} 秒，结果已足够稳定"
    return True, f"Stage 3.2 最大残差 {maximum:.2f} 秒，进入轻量精修"


def _cue_candidates(
    subtitle_path: str,
    stage32: audio_offset_verifier.AudioOffsetResult,
    duration_seconds: float,
) -> list[tuple[audio_offset_verifier.OffsetAnchor, float, _Cue]]:
    cues = [
        cue
        for event in legacy.parse_subtitle(Path(subtitle_path))
        if (cue := _clean_cue(event)) is not None
    ]
    residuals = stage32.residuals or (0.0,) * len(stage32.anchors)
    candidates: list[tuple[audio_offset_verifier.OffsetAnchor, float, _Cue]] = []
    for anchor, residual in zip(stage32.anchors, residuals):
        if anchor.score < 0.50:
            continue
        nearby = sorted(
            (
                (abs((cue.start + cue.end) / 2 - anchor.subtitle_time), cue)
                for cue in cues
                if abs((cue.start + cue.end) / 2 - anchor.subtitle_time) <= 24.0
            ),
            key=lambda item: item[0],
        )
        if nearby:
            candidates.append((anchor, float(residual), nearby[0][1]))

    ranked = sorted(
        candidates,
        key=lambda item: (-item[0].score, abs(item[1]), item[2].start),
    )
    selected: list[tuple[audio_offset_verifier.OffsetAnchor, float, _Cue]] = []
    minimum_gap = max(60.0, duration_seconds * 0.10)
    for item in ranked:
        movie_time = stage32.offset_seconds + stage32.scale * ((item[2].start + item[2].end) / 2)
        if all(
            abs(
                movie_time
                - (stage32.offset_seconds + stage32.scale * ((current[2].start + current[2].end) / 2))
            ) >= minimum_gap
            for current in selected
        ):
            selected.append(item)
        if len(selected) >= MAX_FINE_ANCHORS:
            break
    if len(selected) < MIN_FINE_ANCHORS:
        for item in ranked:
            if item not in selected:
                selected.append(item)
            if len(selected) >= MAX_FINE_ANCHORS:
                break
    return sorted(selected, key=lambda item: item[2].start)


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise legacy.CancelledError("Stage 3.4 已停止。")


def _clip_window(
    cue: _Cue,
    stage32: audio_offset_verifier.AudioOffsetResult,
    extra_padding: float = 0.0,
) -> tuple[float, float]:
    predicted_start = stage32.offset_seconds + stage32.scale * cue.start
    predicted_end = stage32.offset_seconds + stage32.scale * cue.end
    padding = 2.75 + extra_padding
    clip_start = max(0.0, predicted_start - padding)
    clip_duration = min(
        10.0 + 2.0 * extra_padding,
        max(6.0 + 2.0 * extra_padding, predicted_end - predicted_start + 5.5 + 2.0 * extra_padding),
    )
    return clip_start, clip_duration


def _extract_clips(
    movie_path: str,
    work: Path,
    selected: Sequence[tuple[audio_offset_verifier.OffsetAnchor, float, _Cue]],
    stage32: audio_offset_verifier.AudioOffsetResult,
    *,
    audio_stream_index: int,
    ffmpeg: str,
    cancel,
    extra_padding: float = 0.0,
    name_prefix: str = "fine",
) -> tuple[dict[int, Path], dict[int, float], dict[int, float]]:
    wavs: dict[int, Path] = {}
    starts: dict[int, float] = {}
    ends: dict[int, float] = {}
    for index, (_anchor, _residual, cue) in enumerate(selected):
        _check_cancel(cancel)
        clip_start, clip_duration = _clip_window(cue, stage32, extra_padding)
        wav = work / f"{name_prefix}-{index}.wav"
        legacy.run_command(
            [
                ffmpeg, "-y", "-ss", f"{clip_start:.3f}", "-i", movie_path,
                "-t", f"{clip_duration:.3f}", "-map", f"0:a:{audio_stream_index}",
                "-vn", "-ac", "1", "-ar", "16000", str(wav),
            ],
            log=lambda _message: None,
            cancel_event=cancel,
        )
        wavs[index] = wav
        starts[index] = clip_start
        ends[index] = clip_start + clip_duration
    return wavs, starts, ends

def _token_words(payload: dict, clip_start: float) -> list[FineWord]:
    words: list[FineWord] = []
    current_text = ""
    current_start = 0.0
    current_end = 0.0
    probabilities: list[float] = []

    def flush() -> None:
        nonlocal current_text, current_start, current_end, probabilities
        normalized = "".join(re.findall(r"[A-Za-z0-9']+", current_text)).lower()
        if len(normalized) >= 2 or normalized in {"a", "i"}:
            words.append(
                FineWord(
                    clip_start + current_start,
                    clip_start + current_end,
                    normalized,
                    statistics.fmean(probabilities) if probabilities else 0.0,
                )
            )
        current_text = ""
        current_start = 0.0
        current_end = 0.0
        probabilities = []

    for segment in payload.get("transcription", []):
        for token in segment.get("tokens", []):
            raw = str(token.get("text", ""))
            if not raw or raw.startswith("[_"):
                continue
            pieces = re.findall(r"[A-Za-z0-9']+", raw)
            if not pieces:
                if current_text and re.search(r"[.!?,;:)]", raw):
                    flush()
                continue
            offsets = token.get("offsets", {})
            start = float(offsets.get("from", 0) or 0) / 1000.0
            end = float(offsets.get("to", 0) or 0) / 1000.0
            probability = float(token.get("p", 0.0) or 0.0)
            starts_new = bool(re.match(r"\s", raw)) or not current_text
            if starts_new and current_text:
                flush()
            if not current_text:
                current_start = start
            current_text += "".join(pieces)
            current_end = max(current_end, end)
            probabilities.append(probability)
            if re.search(r"[.!?,;:)]\s*$", raw):
                flush()
    flush()
    return words


def _transcribe(
    wavs: dict[int, Path],
    starts: dict[int, float],
    *,
    whisper: str,
    whisper_model: str,
    cancel,
) -> dict[int, list[FineWord]]:
    if not wavs:
        return {}
    ordered = [wavs[index] for index in sorted(wavs)]
    legacy.run_command(
        [
            whisper, "-m", Path(whisper_model).name, "-l", "en", "-t", "6",
            "-np", "-ojf", "-sow", *map(str, ordered),
        ],
        log=lambda _message: None,
        cancel_event=cancel,
        cwd=str(Path(whisper_model).parent),
    )
    result: dict[int, list[FineWord]] = {}
    for index, wav in wavs.items():
        json_path = Path(str(wav) + ".json")
        if not json_path.is_file():
            result[index] = []
            continue
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        result[index] = _token_words(payload, starts[index])
    return result


def _overlap_ratio(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
    overlap = max(0.0, min(end_a, end_b) - max(start_a, start_b))
    shorter = min(end_a - start_a, end_b - start_b)
    return overlap / shorter if shorter > 0 else 0.0


def _same_canonical_match(
    left: tuple[float, int, int],
    right: tuple[float, int, int],
    observed: Sequence[FineWord],
) -> bool:
    _left_score, left_start, left_end = left
    _right_score, right_start, right_end = right
    index_overlap = _overlap_ratio(left_start, left_end, right_start, right_end)
    left_time_start = observed[left_start].start
    left_time_end = observed[left_end - 1].end
    right_time_start = observed[right_start].start
    right_time_end = observed[right_end - 1].end
    time_overlap = _overlap_ratio(left_time_start, left_time_end, right_time_start, right_time_end)
    return index_overlap >= CANONICAL_INDEX_OVERLAP and time_overlap >= CANONICAL_TIME_OVERLAP


def _canonicalize_matches(
    ranked: Sequence[tuple[float, int, int]],
    observed: Sequence[FineWord],
) -> list[tuple[float, int, int]]:
    canonical: list[tuple[float, int, int]] = []
    for candidate in ranked:
        if any(_same_canonical_match(candidate, existing, observed) for existing in canonical):
            continue
        canonical.append(candidate)
    return canonical


def _match_words(
    expected: _Cue,
    observed: Sequence[FineWord],
    predicted_start: float,
    predicted_end: float,
) -> _MatchDiagnostic:
    expected_words = _words(expected.text)
    observed_text = " ".join(word.text for word in observed)
    has_word_timestamps = any(word.end > word.start for word in observed)
    if len(expected_words) < 4 or len(observed) < 3:
        reason = "cue 有效词不足" if len(expected_words) < 4 else "ASR 缺词或 word timestamp 不完整"
        return _MatchDiagnostic(
            False, None, 0.0, 0.0, observed_text, "", None, None,
            len(observed), has_word_timestamps, 0, 0, reason,
        )
    ranked: list[tuple[float, int, int]] = []
    lower = max(3, len(expected_words) - 3)
    upper = min(len(observed), len(expected_words) + 4)
    for start in range(len(observed)):
        for length in range(lower, upper + 1):
            end = start + length
            if end > len(observed):
                break
            phrase = " ".join(word.text for word in observed[start:end])
            score = audio_offset_verifier.dialogue_score(expected.text, phrase)
            ranked.append((score, start, end))
    if not ranked:
        return _MatchDiagnostic(
            False, None, 0.0, 0.0, observed_text, "", None, None,
            len(observed), has_word_timestamps, 0, 0, "无法生成词级匹配窗口",
        )
    ranked.sort(reverse=True)
    canonical = _canonicalize_matches(ranked, observed)
    score, start, end = canonical[0]
    second = canonical[1][0] if len(canonical) > 1 else 0.0
    matched = observed[start:end]
    probability = statistics.fmean(word.probability for word in matched)
    matched_text = " ".join(word.text for word in matched)
    if score < 0.72:
        reason = "文本匹配不足"
    elif score - second < 0.02:
        reason = "多义匹配，存在不同音频位置的近似结果"
    elif probability < 0.42:
        reason = "word timestamp 置信度不足"
    else:
        reason = "词级文本与时间证据通过"
    actual_midpoint = (matched[0].start + matched[-1].end) / 2
    predicted_midpoint = (predicted_start + predicted_end) / 2
    delta = actual_midpoint - predicted_midpoint
    accepted = score >= 0.72 and score - second >= 0.02 and probability >= 0.42
    return _MatchDiagnostic(
        accepted,
        delta,
        score,
        probability,
        observed_text,
        matched_text,
        matched[0].start,
        matched[-1].end,
        len(observed),
        has_word_timestamps,
        len(ranked),
        len(canonical),
        reason,
    )


def _near_clip_boundary(match: _MatchDiagnostic, clip_start: float, clip_end: float) -> bool:
    if match.matched_start is None or match.matched_end is None:
        return False
    return (
        match.matched_start - clip_start < CLIP_EDGE_MARGIN
        or clip_end - match.matched_end < CLIP_EDGE_MARGIN
    )


def _reject_boundary_match(match: _MatchDiagnostic) -> _MatchDiagnostic:
    return _MatchDiagnostic(
        False,
        match.micro_delta,
        match.score,
        match.probability,
        match.observed_text,
        match.matched_text,
        match.matched_start,
        match.matched_end,
        match.word_count,
        match.has_word_timestamps,
        match.raw_match_count,
        match.canonical_match_count,
        "扩展片段后匹配仍贴近音频边缘",
    )

def _weighted_median(values: Sequence[tuple[float, float]]) -> float:
    ordered = sorted(values)
    total = sum(weight for _value, weight in ordered)
    threshold = total / 2
    accumulated = 0.0
    for value, weight in ordered:
        accumulated += weight
        if accumulated >= threshold:
            return value
    return ordered[-1][0]


def aggregate_deltas(anchors: Sequence[FineAnchor]) -> FineAlignmentResult:
    started = time.monotonic()
    if len(anchors) < MIN_FINE_ANCHORS:
        return FineAlignmentResult(
            False, 0.0, tuple(anchors), math.inf, math.inf, "NONE",
            f"只有 {len(anchors)} 个有效 fine anchors，证据不足", time.monotonic() - started,
        )
    initial = statistics.median(anchor.micro_delta for anchor in anchors)
    deviations = [abs(anchor.micro_delta - initial) for anchor in anchors]
    mad = statistics.median(deviations)
    tolerance = max(0.18, 3.0 * mad)
    filtered = tuple(anchor for anchor in anchors if abs(anchor.micro_delta - initial) <= tolerance)
    if len(filtered) < MIN_FINE_ANCHORS:
        return FineAlignmentResult(
            False, 0.0, filtered, math.inf, mad, "LOW",
            "MAD 剔除离群点后不足 3 个锚点", time.monotonic() - started,
        )
    weighted = [
        (
            anchor.micro_delta,
            max(0.01, anchor.text_score * anchor.probability / (1.0 + abs(anchor.stage32_residual))),
        )
        for anchor in filtered
    ]
    micro = _weighted_median(weighted)
    spread = max(anchor.micro_delta for anchor in filtered) - min(anchor.micro_delta for anchor in filtered)
    signs = [1 if anchor.micro_delta > 0.08 else -1 if anchor.micro_delta < -0.08 else 0 for anchor in filtered]
    nonzero = [sign for sign in signs if sign]
    consistent = not nonzero or max(nonzero.count(1), nonzero.count(-1)) >= math.ceil(len(nonzero) * 0.67)
    if abs(micro) > MAX_MICRO_OFFSET:
        reason = f"建议微调 {micro:+.2f} 秒超过 ±{MAX_MICRO_OFFSET:.2f} 秒安全范围"
        return FineAlignmentResult(False, 0.0, filtered, spread, mad, "LOW", reason, time.monotonic() - started)
    if spread > MAX_FINE_SPREAD or not consistent:
        reason = f"锚点分散 {spread:.2f} 秒或方向不一致，结果不确定"
        return FineAlignmentResult(False, 0.0, filtered, spread, mad, "LOW", reason, time.monotonic() - started)
    average_score = statistics.fmean(anchor.text_score for anchor in filtered)
    confidence = "HIGH" if spread <= 0.15 and average_score >= 0.82 else "MEDIUM"
    return FineAlignmentResult(
        True, micro, filtered, spread, mad, confidence,
        f"{len(filtered)} 个 fine anchors 一致", time.monotonic() - started,
    )


def run_shadow(
    movie_path: str,
    subtitle_path: str,
    work_dir: str,
    stage32: audio_offset_verifier.AudioOffsetResult,
    *,
    duration_seconds: float,
    audio_stream_index: int,
    ffmpeg: str,
    whisper: str,
    whisper_model: str,
    log: Callable[[str], None] = print,
    cancel=None,
    force: bool = False,
) -> FineAlignmentResult:
    started = time.monotonic()
    enabled, reason = should_run(stage32)
    if not enabled and not force:
        result = FineAlignmentResult(
            False, 0.0, (), 0.0, 0.0, "SKIPPED", reason,
            time.monotonic() - started, skipped=True,
        )
        log(f"Stage 3.4 精细对时跳过：{reason}。")
        return result

    log("Stage 3.4 精细对时诊断开始。")
    log(
        f"Stage 3.2 粗模型：offset {stage32.offset_seconds:+.2f}s, "
        f"scale {stage32.scale:.6f}。"
    )
    selected = _cue_candidates(subtitle_path, stage32, duration_seconds)
    if len(selected) < MIN_FINE_ANCHORS:
        result = FineAlignmentResult(
            False, 0.0, (), math.inf, math.inf, "LOW",
            f"只有 {len(selected)} 个满足条件的字幕锚点", time.monotonic() - started,
        )
        log(f"Fine alignment shadow result：{result.reason}；micro offset 保持 +0.00s。")
        log("Stage 3.4 当前为 Shadow 模式，不修改最终字幕。")
        return result

    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    wavs, starts, ends = _extract_clips(
        movie_path, work, selected, stage32,
        audio_stream_index=audio_stream_index,
        ffmpeg=ffmpeg,
        cancel=cancel,
    )
    transcripts = _transcribe(
        wavs, starts,
        whisper=whisper,
        whisper_model=whisper_model,
        cancel=cancel,
    )

    matches: dict[int, _MatchDiagnostic] = {}
    resampled: set[int] = set()
    retry_indexes: list[int] = []
    for index, (_anchor, _residual, cue) in enumerate(selected):
        predicted_start = stage32.offset_seconds + stage32.scale * cue.start
        predicted_end = stage32.offset_seconds + stage32.scale * cue.end
        match = _match_words(cue, transcripts.get(index, ()), predicted_start, predicted_end)
        matches[index] = match
        if match.accepted and _near_clip_boundary(match, starts[index], ends[index]):
            retry_indexes.append(index)

    initial_raw_total = sum(match.raw_match_count for match in matches.values())
    initial_canonical_total = sum(match.canonical_match_count for match in matches.values())
    log(
        f"Stage 3.4 初次匹配汇总：候选 fine anchors {len(selected)}，"
        f"去重前匹配 {initial_raw_total}，去重后匹配 {initial_canonical_total}，"
        f"需要边界重采样 {len(retry_indexes)}。"
    )
    for index in retry_indexes:
        match = matches[index]
        log(
            f"Fine anchor candidate {index + 1}：初次匹配贴近片段边缘，"
            f"将左右各扩展 {CLIP_RETRY_PADDING:.2f} 秒后重采样；"
            f"初次 micro delta {match.micro_delta:+.3f}s。"
        )

    if retry_indexes:
        retry_selected = [selected[index] for index in retry_indexes]
        retry_wavs, retry_starts, retry_ends = _extract_clips(
            movie_path, work, retry_selected, stage32,
            audio_stream_index=audio_stream_index,
            ffmpeg=ffmpeg,
            cancel=cancel,
            extra_padding=CLIP_RETRY_PADDING,
            name_prefix="fine-retry",
        )
        retry_transcripts = _transcribe(
            retry_wavs, retry_starts,
            whisper=whisper,
            whisper_model=whisper_model,
            cancel=cancel,
        )
        for retry_index, original_index in enumerate(retry_indexes):
            _anchor, _residual, cue = selected[original_index]
            predicted_start = stage32.offset_seconds + stage32.scale * cue.start
            predicted_end = stage32.offset_seconds + stage32.scale * cue.end
            retry_match = _match_words(
                cue,
                retry_transcripts.get(retry_index, ()),
                predicted_start,
                predicted_end,
            )
            if retry_match.accepted and _near_clip_boundary(
                retry_match, retry_starts[retry_index], retry_ends[retry_index]
            ):
                retry_match = _reject_boundary_match(retry_match)
            matches[original_index] = retry_match
            starts[original_index] = retry_starts[retry_index]
            ends[original_index] = retry_ends[retry_index]
            resampled.add(original_index)

    fine_anchors: list[FineAnchor] = []
    raw_match_total = 0
    canonical_match_total = 0
    for index, (anchor, residual, cue) in enumerate(selected):
        predicted_start = stage32.offset_seconds + stage32.scale * cue.start
        predicted_end = stage32.offset_seconds + stage32.scale * cue.end
        match = matches[index]
        raw_match_total += match.raw_match_count
        canonical_match_total += match.canonical_match_count
        log(f"Fine anchor candidate {index + 1}：Stage 3.2 probe {anchor.clip_index}。")
        log(f"  字幕文本：{cue.text}")
        log(
            f"  预测影片时间：{(predicted_start + predicted_end) / 2:.3f}s；"
            f"短音频范围：{starts[index]:.3f}s - {ends[index]:.3f}s。"
        )
        log(
            f"  匹配窗口：去重前 {match.raw_match_count}，"
            f"canonical 去重后 {match.canonical_match_count}。"
        )
        log(f"  边界重采样：{'是' if index in resampled else '否'}。")
        log(f"  Whisper 识别文本：{match.observed_text or '<空>'}")
        log(
            f"  word timestamps：{'有' if match.has_word_timestamps else '无'}，"
            f"有效词 {match.word_count} 个。"
        )
        if match.matched_text:
            log(
                f"  匹配词范围：{match.matched_text} "
                f"({match.matched_start:.3f}s - {match.matched_end:.3f}s)。"
            )
        else:
            log("  匹配词范围：无。")
        delta_label = f"{match.micro_delta:+.3f}s" if match.micro_delta is not None else "不可计算"
        log(
            f"  文本匹配：{match.score:.0%}；词时间置信度：{match.probability:.0%}；"
            f"micro delta：{delta_label}。"
        )
        log(f"  结论：{'接受' if match.accepted else '拒绝'}；原因：{match.reason}。")
        if not match.accepted or match.micro_delta is None:
            continue
        fine = FineAnchor(
            anchor.clip_index,
            (cue.start + cue.end) / 2,
            (predicted_start + predicted_end) / 2,
            cue.text,
            match.score,
            match.probability,
            match.micro_delta,
            residual,
        )
        fine_anchors.append(fine)
        log(
            f"Fine anchor {len(fine_anchors)}：text score {match.score:.0%}，"
            f"micro delta {match.micro_delta:+.2f}s。"
        )

    median_micro = statistics.median(anchor.micro_delta for anchor in fine_anchors) if fine_anchors else 0.0
    log(
        f"Stage 3.4 匹配汇总：候选 fine anchors {len(selected)}，"
        f"去重前匹配 {raw_match_total}，去重后匹配 {canonical_match_total}，"
        f"边界重采样 {len(resampled)}，有效 anchors {len(fine_anchors)}，"
        f"median micro offset {median_micro:+.3f}s。"
    )
    aggregate = aggregate_deltas(fine_anchors)
    elapsed = time.monotonic() - started
    result = FineAlignmentResult(
        aggregate.accepted,
        aggregate.suggested_micro_offset,
        aggregate.anchors,
        aggregate.spread,
        aggregate.mad,
        aggregate.confidence,
        aggregate.reason,
        elapsed,
        aggregate.skipped,
    )
    suggested_final = stage32.offset_seconds + result.suggested_micro_offset
    log(
        "Fine alignment shadow result："
        f"suggested micro offset {result.suggested_micro_offset:+.2f}s，"
        f"suggested final offset {suggested_final:+.2f}s，"
        f"anchor count {len(result.anchors)}，spread {result.spread:.2f}s，"
        f"MAD {result.mad:.2f}s，confidence {result.confidence}。"
    )
    log(f"Stage 3.4 诊断耗时：{elapsed:.2f} 秒；{result.reason}。")
    log("Stage 3.4 当前为 Shadow 模式，不修改最终字幕。")
    return result
