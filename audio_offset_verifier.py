# -*- coding: utf-8 -*-
from __future__ import annotations

import difflib
import hashlib
import json
import math
import os
import re
import shutil
import statistics
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Sequence

import audio_clause_diagnostics
import subtitle_tool_core as legacy


CACHE_VERSION = 4
DEFAULT_PROBE_DURATION = 6.0
DEFAULT_PROBE_FRACTIONS = (
    0.10, 0.18, 0.26, 0.34, 0.42, 0.50,
    0.58, 0.66, 0.74, 0.82, 0.90,
)
DEFAULT_FINGERPRINT_CLIPS = 5
MIN_PROBE_QUALITY = 0.18
MAX_MATCHES_PER_PROBE = 6
FIXED_RESIDUAL_TOLERANCE = 0.85
AFFINE_RESIDUAL_TOLERANCE = 1.20

COMMON_TIMELINE_SCALES: tuple[tuple[str, float], ...] = (
    ("1.000000", 1.0),
    ("24/23.976", 24.0 / 23.976),
    ("23.976/24", 23.976 / 24.0),
    ("25/24", 25.0 / 24.0),
    ("24/25", 24.0 / 25.0),
    ("25/23.976", 25.0 / 23.976),
    ("23.976/25", 23.976 / 25.0),
)

_GENERIC_WORDS = {
    "a", "an", "and", "are", "be", "but", "come", "do", "go", "hey", "i", "is",
    "it", "just", "me", "my", "no", "not", "of", "oh", "okay", "on", "so", "that",
    "the", "this", "to", "uh", "um", "we", "what", "yeah", "yes", "you", "your",
}


@dataclass(frozen=True)
class TimedText:
    start: float
    end: float
    text: str
    source_index: int
    segment_count: int = 1


@dataclass(frozen=True)
class TimedWord:
    start: float
    end: float
    text: str
    source_index: int
    confidence: float = 0.0


@dataclass(frozen=True)
class OffsetAnchor:
    clip_index: int
    movie_time: float
    subtitle_time: float
    offset_seconds: float
    score: float
    margin: float
    observed_text: str = ""
    candidate_text: str = ""
    distinct_location_margin: float = 0.0
    candidate_start: float = 0.0
    candidate_end: float = 0.0
    clause_diagnostic: audio_clause_diagnostics.ClauseDiagnostic | None = None
    observation_start: float = 0.0
    observation_end: float = 0.0
    observation_segment_count: int = 0


@dataclass(frozen=True)
class ConsensusAnchorShadow:
    clip_index: int
    candidate_start: float
    candidate_end: float
    offset_seconds: float
    score: float
    residual: float


@dataclass(frozen=True)
class ConsensusClusterShadow:
    rank: int
    scale_label: str
    offset_seconds: float
    probe_ids: tuple[int, ...]
    median_residual: float
    max_residual: float
    span_seconds: float
    average_score: float
    accepted_by_stage3_2: bool
    selected_by_stage3_2: bool
    anchors: tuple[ConsensusAnchorShadow, ...]


@dataclass(frozen=True)
class ConsensusScaleShadow:
    scale_label: str
    clusters: tuple[ConsensusClusterShadow, ...]
    best_vs_second_reason: str


@dataclass(frozen=True)
class ConsensusAuditShadow:
    diagnostic_version: str
    scales: tuple[ConsensusScaleShadow, ...]
    final_selection_reason: str
    elapsed_ms: float
    error: str = ""


@dataclass(frozen=True)
class AudioOffsetResult:
    accepted: bool
    offset_seconds: float | None
    anchors: tuple[OffsetAnchor, ...]
    clip_scores: tuple[float, ...]
    reason: str
    scale: float = 1.0
    scale_label: str = "1.000000"
    residuals: tuple[float, ...] = ()
    consensus_shadow: ConsensusAuditShadow | None = None

    @property
    def average_score(self) -> float:
        return statistics.fmean(self.clip_scores) if self.clip_scores else 0.0

    @property
    def drift_per_hour(self) -> float:
        return (self.scale - 1.0) * 3600.0

    @property
    def affine(self) -> bool:
        return abs(self.scale - 1.0) > 1e-7

    def equivalent_offset(self, subtitle_time: float) -> float | None:
        if self.offset_seconds is None:
            return None
        return self.offset_seconds + (self.scale - 1.0) * subtitle_time


@dataclass(frozen=True)
class MovieAudioFingerprint:
    """Candidate-independent audio evidence extracted from the movie itself."""

    clip_indexes: tuple[int, ...]
    events: tuple[TimedText, ...]
    qualities: tuple[tuple[int, float], ...]
    words: tuple[TimedWord, ...] = ()

    @property
    def usable_clip_count(self) -> int:
        return len(self.clip_indexes)


@dataclass(frozen=True)
class _CandidateIndex:
    windows: tuple[TimedText, ...]
    clause_windows: tuple[audio_clause_diagnostics.ClauseWindow, ...]
    token_sets: tuple[frozenset[str], ...]
    inverted: dict[str, tuple[int, ...]]
    document_frequency: dict[str, int]


@dataclass(frozen=True)
class _TimelineModel:
    scale_label: str
    scale: float
    offset: float
    anchors: tuple[OffsetAnchor, ...]
    residuals: tuple[float, ...]
    span_seconds: float

    @property
    def average_score(self) -> float:
        return statistics.fmean(anchor.score for anchor in self.anchors) if self.anchors else 0.0

    @property
    def median_residual(self) -> float:
        return statistics.median(self.residuals) if self.residuals else math.inf

    @property
    def max_residual(self) -> float:
        return max(self.residuals) if self.residuals else math.inf


class _DeadlineCancel:
    def __init__(self, cancel, deadline: float | None) -> None:
        self.cancel = cancel
        self.deadline = deadline

    def is_set(self) -> bool:
        return bool(
            (self.cancel is not None and self.cancel.is_set())
            or (self.deadline is not None and time.monotonic() >= self.deadline)
        )

    @property
    def timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline


def _seconds(value: str) -> float:
    hours, minutes, seconds = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _clean(value: str) -> str:
    text = re.sub(r"\{\\[^}]*\}", " ", value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[[^\]]{0,80}\]|\([^)]{0,80}\)", " ", text)
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w']+", " ", text)
    return " ".join(text.split())


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9']{2,}", _clean(value))


def dialogue_score(expected: str, observed: str) -> float:
    """Order-sensitive 0..1 dialogue similarity for timing localization."""
    expected_clean = _clean(expected)
    observed_clean = _clean(observed)
    if not expected_clean or not observed_clean:
        return 0.0

    expected_words = re.findall(r"[a-z0-9']{2,}", expected_clean)
    observed_words = re.findall(r"[a-z0-9']{2,}", observed_clean)
    char_sequence = difflib.SequenceMatcher(None, expected_clean, observed_clean).ratio()
    if not expected_words or not observed_words:
        return char_sequence * 0.7

    token_sequence = difflib.SequenceMatcher(None, expected_words, observed_words).ratio()
    expected_counts = Counter(expected_words)
    observed_counts = Counter(observed_words)
    overlap = sum((expected_counts & observed_counts).values())
    precision = overlap / len(observed_words)
    recall = overlap / len(expected_words)
    bag_f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return 0.50 * token_sequence + 0.30 * char_sequence + 0.20 * bag_f1


def _events(path: Path, source_index: int) -> list[TimedText]:
    return [
        TimedText(_seconds(event.start), _seconds(event.end), event.text, source_index)
        for event in legacy.parse_subtitle(path)
        if _clean(event.text)
    ]


def _windows(
    events: Sequence[TimedText],
    *,
    maximum_span: float = 12.0,
    maximum_events: int = 8,
    minimum_characters: int = 18,
) -> list[TimedText]:
    return _candidate_windows(
        events,
        maximum_span=maximum_span,
        maximum_events=maximum_events,
        minimum_characters=minimum_characters,
    )[0]


def _candidate_windows(
    events: Sequence[TimedText],
    *,
    maximum_span: float = 12.0,
    maximum_events: int = 8,
    minimum_characters: int = 18,
) -> tuple[list[TimedText], list[audio_clause_diagnostics.ClauseWindow]]:
    windows: list[TimedText] = []
    clause_windows: list[audio_clause_diagnostics.ClauseWindow] = []
    for index, first in enumerate(events):
        group: list[TimedText] = []
        for event in events[index:index + maximum_events]:
            if event.source_index != first.source_index:
                break
            if event.end - first.start > maximum_span and group:
                break
            group.append(event)
            text = " ".join(item.text for item in group)
            if len(_clean(text)) >= minimum_characters:
                windows.append(TimedText(first.start, group[-1].end, text, first.source_index))
                clause_windows.append(audio_clause_diagnostics.ClauseWindow(
                    first.start,
                    group[-1].end,
                    text,
                    tuple(
                        audio_clause_diagnostics.ClauseCue(
                            cue_index + 1,
                            cue.start,
                            cue.end,
                            cue.text,
                        )
                        for cue_index, cue in enumerate(group, start=index)
                    ),
                ))
    return windows, clause_windows


def _probe_observations(events: Sequence[TimedText]) -> list[TimedText]:
    """One independent observation per movie probe."""
    grouped: dict[int, list[TimedText]] = defaultdict(list)
    for event in events:
        grouped[event.source_index].append(event)

    observations: list[TimedText] = []
    for clip_index in sorted(grouped):
        clip_events = sorted(grouped[clip_index], key=lambda item: (item.start, item.end))
        text = " ".join(item.text for item in clip_events)
        if len(_clean(text)) < 16:
            continue
        observations.append(TimedText(
            clip_events[0].start,
            clip_events[-1].end,
            text,
            clip_index,
            len(clip_events),
        ))
    return observations


def _build_candidate_index(candidate_events: Sequence[TimedText]) -> _CandidateIndex:
    regular_windows, diagnostic_windows = _candidate_windows(candidate_events)
    windows = tuple(regular_windows)
    clause_windows = tuple(diagnostic_windows)
    token_sets: list[frozenset[str]] = []
    inverted_work: dict[str, list[int]] = defaultdict(list)
    for index, window in enumerate(windows):
        tokens = frozenset(_words(window.text))
        token_sets.append(tokens)
        for token in tokens:
            inverted_work[token].append(index)
    inverted = {token: tuple(indexes) for token, indexes in inverted_work.items()}
    document_frequency = {token: len(indexes) for token, indexes in inverted.items()}
    return _CandidateIndex(
        windows, clause_windows, tuple(token_sets), inverted, document_frequency,
    )


def _shortlist_window_indexes(observed: TimedText, index: _CandidateIndex) -> tuple[int, ...]:
    # Use every recognized word, then let document frequency choose the rarest
    # terms. This keeps recall when Whisper misrecognizes the most distinctive word.
    query = set(_words(observed.text))
    if not query or not index.windows:
        return tuple(range(len(index.windows)))

    shared = [word for word in query if word in index.inverted]
    if not shared:
        return tuple(range(len(index.windows)))
    shared.sort(key=lambda word: (index.document_frequency.get(word, 10**9), word))

    candidates: set[int] = set()
    for word in shared[:8]:
        candidates.update(index.inverted[word])
    # Preserve recall when ASR got most distinctive words wrong.
    if len(candidates) < 12:
        for word in shared[8:16]:
            candidates.update(index.inverted[word])
    if not candidates:
        return tuple(range(len(index.windows)))
    return tuple(sorted(candidates))


def _rank_anchor_candidates(
    observed: TimedText,
    candidate_index: _CandidateIndex,
    score: Callable[[str, str], float],
    *,
    maximum: int = MAX_MATCHES_PER_PROBE,
) -> tuple[OffsetAnchor, ...]:
    shortlist = _shortlist_window_indexes(observed, candidate_index)
    scored = tuple(
        (
            score(candidate_index.windows[index].text, observed.text),
            index,
            candidate_index.windows[index],
        )
        for index in shortlist
    )
    ranked = sorted(scored, key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] < 0.50:
        return ()

    best_score = ranked[0][0]
    clause_analysis = audio_clause_diagnostics.analyze_clause_windows(
        observed.text,
        tuple(
            replace(candidate_index.clause_windows[index], stage3_score=match_score)
            for match_score, index, _ in scored
        ),
    )
    output: list[OffsetAnchor] = []
    used_midpoints: list[float] = []
    for rank_index, (match_score, _, candidate) in enumerate(ranked):
        if match_score < 0.50 or match_score < best_score - 0.20:
            break
        subtitle_time = (candidate.start + candidate.end) / 2.0
        if any(abs(subtitle_time - prior) < 0.40 for prior in used_midpoints):
            continue
        next_score = ranked[rank_index + 1][0] if rank_index + 1 < len(ranked) else 0.0
        margin = max(0.0, match_score - next_score) if rank_index == 0 else 0.0
        distinct_alternative = max(
            (
                alternative_score
                for alternative_score, _, alternative in ranked
                if abs(((alternative.start + alternative.end) / 2.0) - subtitle_time) >= 15.0
            ),
            default=0.0,
        )
        movie_time = (observed.start + observed.end) / 2.0
        output.append(OffsetAnchor(
            clip_index=observed.source_index,
            movie_time=movie_time,
            subtitle_time=subtitle_time,
            offset_seconds=movie_time - subtitle_time,
            score=match_score,
            margin=margin,
            observed_text=observed.text,
            candidate_text=candidate.text,
            distinct_location_margin=max(0.0, match_score - distinct_alternative),
            candidate_start=candidate.start,
            candidate_end=candidate.end,
            clause_diagnostic=audio_clause_diagnostics.diagnose_selected_location(
                clause_analysis,
                selected_start=candidate.start,
                selected_end=candidate.end,
            ),
            observation_start=observed.start,
            observation_end=observed.end,
            observation_segment_count=observed.segment_count,
        ))
        used_midpoints.append(subtitle_time)
        if len(output) >= maximum:
            break
    return tuple(output)


def _anchor_options(
    candidate_events: Sequence[TimedText],
    observed_events: Sequence[TimedText],
    score: Callable[[str, str], float],
) -> dict[int, tuple[OffsetAnchor, ...]]:
    candidate_index = _build_candidate_index(candidate_events)
    options: dict[int, tuple[OffsetAnchor, ...]] = {}
    for observed in _probe_observations(observed_events):
        ranked = _rank_anchor_candidates(observed, candidate_index, score)
        if ranked:
            options[observed.source_index] = ranked
    return options


def _select_for_model(
    options: dict[int, tuple[OffsetAnchor, ...]],
    *,
    scale: float,
    offset: float,
    tolerance: float,
) -> tuple[OffsetAnchor, ...]:
    selected: list[OffsetAnchor] = []
    for clip_index in sorted(options):
        eligible: list[tuple[float, float, OffsetAnchor]] = []
        for anchor in options[clip_index]:
            residual = abs(anchor.movie_time - (offset + scale * anchor.subtitle_time))
            if residual <= tolerance:
                objective = anchor.score + 0.10 * anchor.margin - 0.08 * residual
                eligible.append((objective, -residual, anchor))
        if eligible:
            selected.append(max(eligible, key=lambda item: (item[0], item[1]))[2])
    return tuple(selected)


def _fit_known_scale(
    options: dict[int, tuple[OffsetAnchor, ...]],
    *,
    scale_label: str,
    scale: float,
    tolerance: float,
) -> _TimelineModel | None:
    hypotheses = [
        anchor.movie_time - scale * anchor.subtitle_time
        for anchors in options.values()
        for anchor in anchors
    ]
    best: _TimelineModel | None = None
    for hypothesis in hypotheses:
        selected = _select_for_model(options, scale=scale, offset=hypothesis, tolerance=tolerance)
        if len(selected) < 2:
            continue
        # Robust intercept for a fixed known scale, then reselect once.
        offset = statistics.median(
            anchor.movie_time - scale * anchor.subtitle_time
            for anchor in selected
        )
        selected = _select_for_model(options, scale=scale, offset=offset, tolerance=tolerance)
        if len(selected) < 2:
            continue
        offset = statistics.median(
            anchor.movie_time - scale * anchor.subtitle_time
            for anchor in selected
        )
        residuals = tuple(
            abs(anchor.movie_time - (offset + scale * anchor.subtitle_time))
            for anchor in selected
        )
        span = max(anchor.movie_time for anchor in selected) - min(anchor.movie_time for anchor in selected)
        model = _TimelineModel(scale_label, scale, offset, selected, residuals, span)
        if best is None:
            best = model
            continue
        current_key = (
            len(model.anchors),
            -model.median_residual,
            model.span_seconds,
            model.average_score,
            -model.max_residual,
        )
        best_key = (
            len(best.anchors),
            -best.median_residual,
            best.span_seconds,
            best.average_score,
            -best.max_residual,
        )
        if current_key > best_key:
            best = model
    return best


def _model_acceptance_reasons(
    model: _TimelineModel,
    duration_seconds: float,
    options: dict[int, tuple[OffsetAnchor, ...]],
) -> tuple[bool, tuple[str, ...]]:
    """Evaluate strong identification evidence separately from corroboration."""
    reasons: list[str] = []
    anchors = tuple(model.anchors)
    strong = [anchor for anchor in anchors if anchor.score >= 0.60]
    corroborating = [anchor for anchor in anchors if anchor.score >= 0.50]

    if not anchors:
        return False, ("没有时间锚点",)

    strong_probe_count = sum(
        1 for probe_options in options.values()
        if probe_options and max(anchor.score for anchor in probe_options) >= 0.60
    )
    required_consensus = max(2, math.ceil(strong_probe_count * 0.70))
    if len(model.anchors) < required_consensus:
        reasons.append(
            f"模型仅解释 {len(model.anchors)} 个探针，强匹配探针共有 {strong_probe_count} 个，"
            f"至少需要解释 {required_consensus} 个"
        )

    if model.scale_label == "1.000000":
        # Two far-apart strong anchors determine a fixed offset. Lower-score
        # anchors may corroborate the same geometry but never replace a strong anchor.
        minimum_fixed_span = max(
            300.0,
            duration_seconds * 0.25 if duration_seconds > 0 else 300.0,
        )
        strong_scores = [anchor.score for anchor in strong]
        if len(strong) < 2:
            reasons.append(f"固定偏移只有 {len(strong)} 个强锚点，至少需要 2 个")
        elif statistics.fmean(strong_scores) < 0.70:
            reasons.append(
                f"强锚点平均文本匹配 {statistics.fmean(strong_scores):.0%}，低于 70%"
            )
        if len(corroborating) < 2:
            reasons.append("固定偏移缺少至少 2 个可用独立锚点")
        if model.span_seconds < minimum_fixed_span:
            reasons.append(
                f"固定偏移锚点跨度 {model.span_seconds:.0f} 秒，"
                f"低于要求的 {minimum_fixed_span:.0f} 秒"
            )
        if model.max_residual > 0.85:
            reasons.append(
                f"固定偏移最大残差 {model.max_residual:.2f} 秒，高于 0.85 秒"
            )
        return not reasons, tuple(reasons)

    minimum_span = max(900.0, duration_seconds * 0.20 if duration_seconds > 0 else 900.0)
    if len(anchors) < 3:
        reasons.append(f"线性漂移只有 {len(anchors)} 个锚点，至少需要 3 个")
    if len(strong) < 3:
        reasons.append(f"线性漂移只有 {len(strong)} 个强锚点，至少需要 3 个")
    elif statistics.fmean(anchor.score for anchor in strong) < 0.68:
        reasons.append(
            f"线性漂移强锚点平均文本匹配 "
            f"{statistics.fmean(anchor.score for anchor in strong):.0%}，低于 68%"
        )
    if model.span_seconds < minimum_span:
        reasons.append(
            f"线性漂移锚点跨度 {model.span_seconds:.0f} 秒，"
            f"低于要求的 {minimum_span:.0f} 秒"
        )
    if model.max_residual > AFFINE_RESIDUAL_TOLERANCE:
        reasons.append(
            f"线性漂移最大残差 {model.max_residual:.2f} 秒，"
            f"高于 {AFFINE_RESIDUAL_TOLERANCE:.2f} 秒"
        )
    return not reasons, tuple(reasons)


def _model_is_acceptable(
    model: _TimelineModel,
    duration_seconds: float,
    options: dict[int, tuple[OffsetAnchor, ...]],
) -> bool:
    accepted, _reasons = _model_acceptance_reasons(model, duration_seconds, options)
    return accepted


def _choose_timeline_model(
    options: dict[int, tuple[OffsetAnchor, ...]],
    duration_seconds: float,
) -> tuple[_TimelineModel | None, tuple[_TimelineModel, ...]]:
    models: list[_TimelineModel] = []
    for label, scale in COMMON_TIMELINE_SCALES:
        tolerance = FIXED_RESIDUAL_TOLERANCE if label == "1.000000" else AFFINE_RESIDUAL_TOLERANCE
        model = _fit_known_scale(
            options,
            scale_label=label,
            scale=scale,
            tolerance=tolerance,
        )
        if model is not None:
            models.append(model)

    acceptable = [
        model for model in models
        if _model_is_acceptable(model, duration_seconds, options)
    ]
    if not acceptable:
        return None, tuple(models)

    # More independent probes wins first.  If fixed and affine explain the same
    # evidence almost equally well, prefer fixed to avoid inventing drift.
    acceptable.sort(
        key=lambda model: (
            len(model.anchors),
            -model.median_residual,
            model.span_seconds,
            model.average_score,
        ),
        reverse=True,
    )
    best = acceptable[0]
    fixed = next((model for model in acceptable if model.scale_label == "1.000000"), None)
    if fixed is not None and best.scale_label != "1.000000":
        # Subtitle-cue boundaries and Whisper segment boundaries are not identical.
        # A single noisy probe can therefore sit about a second away from an
        # otherwise near-exact fixed-offset solution.  Do not let a standard
        # frame-rate model manufacture drift merely because it can absorb that
        # one boundary-jitter probe.
        if len(fixed.anchors) == len(best.anchors):
            if fixed.median_residual <= best.median_residual + 0.25:
                best = fixed
        else:
            near_complete_fixed_core = (
                len(fixed.anchors) >= 3
                and len(fixed.anchors) >= math.ceil(len(best.anchors) * 0.80)
                and fixed.span_seconds >= best.span_seconds * 0.80
            )
            clearly_tighter_fixed_geometry = (
                fixed.median_residual <= 0.20
                and best.median_residual >= fixed.median_residual + 0.25
            )
            if near_complete_fixed_core and clearly_tighter_fixed_geometry:
                best = fixed
    return best, tuple(models)


def _consensus_model_key(model: _TimelineModel) -> tuple[float, ...]:
    return (
        float(len(model.anchors)),
        -model.median_residual,
        model.span_seconds,
        model.average_score,
        -model.max_residual,
    )


def _consensus_model_signature(model: _TimelineModel) -> tuple[tuple[object, ...], ...]:
    return tuple(sorted(
        (
            anchor.clip_index,
            round(anchor.candidate_start, 3),
            round(anchor.candidate_end, 3),
            round(anchor.subtitle_time, 3),
        )
        for anchor in model.anchors
    ))


def _enumerate_consensus_models(
    options: dict[int, tuple[OffsetAnchor, ...]],
    *,
    scale_label: str,
    scale: float,
    tolerance: float,
    maximum: int = 3,
) -> tuple[_TimelineModel, ...]:
    hypotheses = [
        anchor.movie_time - scale * anchor.subtitle_time
        for anchors in options.values()
        for anchor in anchors
    ]
    unique: dict[tuple[tuple[object, ...], ...], _TimelineModel] = {}
    for hypothesis in hypotheses:
        selected = _select_for_model(
            options,
            scale=scale,
            offset=hypothesis,
            tolerance=tolerance,
        )
        if len(selected) < 2:
            continue
        offset = statistics.median(
            anchor.movie_time - scale * anchor.subtitle_time
            for anchor in selected
        )
        selected = _select_for_model(
            options,
            scale=scale,
            offset=offset,
            tolerance=tolerance,
        )
        if len(selected) < 2:
            continue
        offset = statistics.median(
            anchor.movie_time - scale * anchor.subtitle_time
            for anchor in selected
        )
        residuals = tuple(
            abs(anchor.movie_time - (offset + scale * anchor.subtitle_time))
            for anchor in selected
        )
        span = max(anchor.movie_time for anchor in selected) - min(
            anchor.movie_time for anchor in selected
        )
        model = _TimelineModel(
            scale_label,
            scale,
            offset,
            selected,
            residuals,
            span,
        )
        signature = _consensus_model_signature(model)
        prior = unique.get(signature)
        if prior is None or _consensus_model_key(model) > _consensus_model_key(prior):
            unique[signature] = model
    ranked = sorted(unique.values(), key=_consensus_model_key, reverse=True)
    return tuple(ranked[:maximum])


def _explain_consensus_preference(
    best: _TimelineModel | None,
    second: _TimelineModel | None,
) -> str:
    if best is None:
        return "没有形成至少由 2 个独立探针支持的集群"
    if second is None:
        return "只有一个可比较集群"
    if len(best.anchors) != len(second.anchors):
        return f"独立探针支持数胜出：{len(best.anchors)} 对 {len(second.anchors)}"
    if not math.isclose(best.median_residual, second.median_residual, abs_tol=1e-12):
        return (
            "独立探针数相同，中位残差更低："
            f"{best.median_residual:.3f}s 对 {second.median_residual:.3f}s"
        )
    if not math.isclose(best.span_seconds, second.span_seconds, abs_tol=1e-12):
        return (
            "探针数和中位残差相同，时间跨度更大："
            f"{best.span_seconds:.1f}s 对 {second.span_seconds:.1f}s"
        )
    if not math.isclose(best.average_score, second.average_score, abs_tol=1e-12):
        return (
            "前序指标相同，平均文本匹配更高："
            f"{best.average_score:.1%} 对 {second.average_score:.1%}"
        )
    return (
        "前序指标相同，最大残差更低："
        f"{best.max_residual:.3f}s 对 {second.max_residual:.3f}s"
    )


def _same_timeline_model(left: _TimelineModel | None, right: _TimelineModel) -> bool:
    return bool(
        left is not None
        and left.scale_label == right.scale_label
        and math.isclose(left.offset, right.offset, abs_tol=1e-9)
        and _consensus_model_signature(left) == _consensus_model_signature(right)
    )


def _build_consensus_audit(
    options: dict[int, tuple[OffsetAnchor, ...]],
    *,
    duration_seconds: float,
    selected_model: _TimelineModel | None,
    scale_models: Sequence[_TimelineModel],
) -> ConsensusAuditShadow:
    started = time.perf_counter()
    scale_results: list[ConsensusScaleShadow] = []
    for scale_label, scale in COMMON_TIMELINE_SCALES:
        tolerance = (
            FIXED_RESIDUAL_TOLERANCE
            if scale_label == "1.000000"
            else AFFINE_RESIDUAL_TOLERANCE
        )
        models = _enumerate_consensus_models(
            options,
            scale_label=scale_label,
            scale=scale,
            tolerance=tolerance,
        )
        clusters: list[ConsensusClusterShadow] = []
        for rank, model in enumerate(models, start=1):
            anchors = tuple(
                ConsensusAnchorShadow(
                    clip_index=anchor.clip_index,
                    candidate_start=anchor.candidate_start,
                    candidate_end=anchor.candidate_end,
                    offset_seconds=anchor.movie_time - scale * anchor.subtitle_time,
                    score=anchor.score,
                    residual=residual,
                )
                for anchor, residual in zip(model.anchors, model.residuals)
            )
            clusters.append(ConsensusClusterShadow(
                rank=rank,
                scale_label=scale_label,
                offset_seconds=model.offset,
                probe_ids=tuple(anchor.clip_index for anchor in model.anchors),
                median_residual=model.median_residual,
                max_residual=model.max_residual,
                span_seconds=model.span_seconds,
                average_score=model.average_score,
                accepted_by_stage3_2=_model_is_acceptable(
                    model,
                    duration_seconds,
                    options,
                ),
                selected_by_stage3_2=_same_timeline_model(selected_model, model),
                anchors=anchors,
            ))
        scale_results.append(ConsensusScaleShadow(
            scale_label=scale_label,
            clusters=tuple(clusters),
            best_vs_second_reason=_explain_consensus_preference(
                models[0] if models else None,
                models[1] if len(models) > 1 else None,
            ),
        ))

    acceptable_models = [
        model for model in scale_models
        if _model_is_acceptable(model, duration_seconds, options)
    ]
    acceptable_models.sort(
        key=lambda model: (
            len(model.anchors),
            -model.median_residual,
            model.span_seconds,
            model.average_score,
        ),
        reverse=True,
    )
    nominal = acceptable_models[0] if acceptable_models else None
    if selected_model is None:
        final_reason = "没有 Stage 3.2 可接受模型，正式流程按原逻辑回退"
    elif nominal is not None and _same_timeline_model(selected_model, nominal):
        runner = acceptable_models[1] if len(acceptable_models) > 1 else None
        final_reason = _explain_consensus_preference(selected_model, runner)
    else:
        final_reason = (
            "固定模型保护规则覆盖了名义排序第一的 affine 模型；"
            "正式选择仍完全沿用现有 Stage 3.2"
        )
    return ConsensusAuditShadow(
        diagnostic_version="stage3_2_existing_consensus_audit_v1",
        scales=tuple(scale_results),
        final_selection_reason=final_reason,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


def _safe_consensus_audit(
    options: dict[int, tuple[OffsetAnchor, ...]],
    *,
    duration_seconds: float,
    selected_model: _TimelineModel | None,
    scale_models: Sequence[_TimelineModel],
) -> ConsensusAuditShadow:
    started = time.perf_counter()
    try:
        return _build_consensus_audit(
            options,
            duration_seconds=duration_seconds,
            selected_model=selected_model,
            scale_models=scale_models,
        )
    except Exception as exc:
        return ConsensusAuditShadow(
            diagnostic_version="stage3_2_existing_consensus_audit_v1",
            scales=(),
            final_selection_reason="Shadow 审计失败，正式 Stage 3.2 结果不受影响",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            error=f"{type(exc).__name__}: {exc}",
        )

def _diagnostic_reason(
    options: dict[int, tuple[OffsetAnchor, ...]],
    models: Sequence[_TimelineModel],
    duration_seconds: float,
) -> tuple[str, tuple[OffsetAnchor, ...]]:
    if not options:
        return "没有找到可靠的影片对白与候选字幕文本对应", ()

    # Prefer the model that explains the most independent probes for diagnostics.
    ranked = sorted(
        models,
        key=lambda model: (
            len(model.anchors),
            -model.median_residual,
            model.span_seconds,
            model.average_score,
        ),
        reverse=True,
    )
    if ranked:
        best = ranked[0]
        if best.scale_label != "1.000000" and len(best.anchors) == 2:
            detail = (
                f"2 个独立探针提示标准线性漂移 {best.scale_label} "
                f"（约 {((best.scale - 1.0) * 3600):+.2f} 秒/小时），"
                "但线性漂移至少需要 3 个独立探针才能放行"
            )
            return detail, best.anchors
        if len(best.anchors) >= 2:
            _accepted, failures = _model_acceptance_reasons(best, duration_seconds, options)
            failure_text = "；".join(failures) if failures else "未进入最终可接受模型集合"
            detail = (
                f"最佳时间模型解释 {len(best.anchors)} 个独立探针，"
                f"比例 {best.scale_label}，最大残差 {best.max_residual:.2f} 秒；"
                f"拒绝原因：{failure_text}"
            )
            return detail, best.anchors

    strongest = tuple(
        max(anchors, key=lambda anchor: (anchor.score, anchor.margin))
        for _clip, anchors in sorted(options.items())
    )
    detail = "；".join(
        f"探针 {anchor.clip_index + 1} 最佳 {anchor.offset_seconds:+.2f}s/{anchor.score:.0%}"
        for anchor in strongest[:5]
    )
    return f"没有得到足够的独立一致时间证据；{detail}", strongest


def locate_offset(
    candidate_events: Sequence[TimedText],
    observed_events: Sequence[TimedText],
    *,
    score: Callable[[str, str], float] = dialogue_score,
    duration_seconds: float = 0.0,
) -> AudioOffsetResult:
    """Locate either a fixed offset or a known standard linear timeline drift."""
    options = _anchor_options(candidate_events, observed_events, score)
    model, models = _choose_timeline_model(options, duration_seconds)
    consensus_shadow = _safe_consensus_audit(
        options,
        duration_seconds=duration_seconds,
        selected_model=model,
        scale_models=models,
    )
    if model is None:
        reason, anchors = _diagnostic_reason(options, models, duration_seconds)
        return AudioOffsetResult(
            False,
            None,
            anchors,
            (),
            reason,
            consensus_shadow=consensus_shadow,
        )

    scores = tuple(anchor.score for anchor in model.anchors)
    if model.scale_label == "1.000000":
        reason = (
            f"{len(model.anchors)} 个独立音频锚点支持固定偏移，"
            f"最大残差 {model.max_residual:.2f} 秒"
        )
    else:
        reason = (
            f"{len(model.anchors)} 个独立音频锚点支持标准线性时间关系 {model.scale_label}，"
            f"起始偏移 {model.offset:+.2f} 秒，每小时漂移 {(model.scale - 1.0) * 3600:+.2f} 秒，"
            f"最大残差 {model.max_residual:.2f} 秒"
        )
    return AudioOffsetResult(
        True,
        model.offset,
        model.anchors,
        scores,
        reason,
        scale=model.scale,
        scale_label=model.scale_label,
        residuals=model.residuals,
        consensus_shadow=consensus_shadow,
    )


def _clip_start(duration_seconds: float, clip_duration: float, fraction: float) -> float:
    if duration_seconds <= clip_duration:
        return 0.0
    return max(0.0, min(duration_seconds - clip_duration, duration_seconds * fraction - clip_duration / 2))


def persistent_cache_dir(
    input_path: str,
    *,
    audio_stream_index: int,
    whisper_model: str,
) -> Path:
    """Stable cache location across retries, candidates and application runs."""
    video = Path(input_path).resolve()
    model = Path(whisper_model).resolve()
    try:
        video_stat = video.stat()
        video_size = video_stat.st_size
        video_mtime = video_stat.st_mtime_ns
    except OSError:
        video_size = 0
        video_mtime = 0
    try:
        model_stat = model.stat()
        model_size = model_stat.st_size
        model_mtime = model_stat.st_mtime_ns
    except OSError:
        model_size = 0
        model_mtime = 0
    identity = json.dumps({
        "path": str(video).lower(),
        "size": video_size,
        "mtime_ns": video_mtime,
        "audio_stream_index": int(audio_stream_index),
        "model": model.name,
        "model_size": model_size,
        "model_mtime_ns": model_mtime,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    key = hashlib.sha256(identity).hexdigest()[:24]
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubFlow"
    path = base / "audio-fingerprints" / key
    path.mkdir(parents=True, exist_ok=True)
    return path


# Exact-window evidence is shared by public, candidate-directed, manual and
# automatic checks. The per-movie lock makes concurrent callers single-flight.
_WINDOW_LOCKS_GUARD = threading.Lock()
_WINDOW_LOCKS: dict[str, threading.RLock] = {}
_RECENT_WINDOW_FAILURES: dict[str, tuple[float, str]] = {}
TRANSIENT_FAILURE_TTL_SECONDS = 60.0
WINDOW_EVIDENCE_VERSION = "exact-window-v1-mono16k-word-json"


def _window_evidence_identity(
    input_path: str, start: float, length: float, *, audio_stream_index: int,
    ffmpeg: str, whisper: str, whisper_model: str, audio_language: str,
    translate_to_english: bool,
) -> dict[str, object]:
    def file_id(value: str) -> list[object]:
        path = Path(value).resolve()
        try:
            stat = path.stat()
            return [str(path).lower(), stat.st_size, stat.st_mtime_ns]
        except OSError:
            return [str(path).lower(), -1, -1]
    return {
        "version": WINDOW_EVIDENCE_VERSION,
        "movie": file_id(input_path),
        "audio_stream_index": audio_stream_index,
        # These are the exact start/end times actually passed to FFmpeg.
        "start": f"{start:.3f}",
        "end": f"{Decimal(f'{start:.3f}') + Decimal(f'{length:.3f}'):.3f}",
        "preprocessing": "-vn -ac 1 -ar 16000 wav",
        "ffmpeg": file_id(ffmpeg), "whisper": file_id(whisper),
        "model": file_id(whisper_model),
        "language": audio_language, "translate": translate_to_english,
        "asr_args": "-t 6 -np -osrt -ojf word-json",
    }


def transcribe_exact_windows(
    input_path: str, specs: Sequence[tuple[int, float, float]], *,
    audio_stream_index: int, ffmpeg: str, whisper: str, whisper_model: str,
    audio_language: str, translate_to_english: bool, cancel,
    log: Callable[[str], None] = print,
    task_failures: dict[str, str] | None = None,
) -> tuple[dict[int, list[TimedText]], dict[int, list[TimedWord]], dict[int, str]]:
    """Get candidate-independent evidence; never equate nearby windows."""
    cache_dir = persistent_cache_dir(
        input_path, audio_stream_index=audio_stream_index, whisper_model=whisper_model
    ) / "exact-windows"
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_key = str(cache_dir.resolve()).lower()
    with _WINDOW_LOCKS_GUARD:
        lock = _WINDOW_LOCKS.setdefault(lock_key, threading.RLock())
    events: dict[int, list[TimedText]] = {}
    words: dict[int, list[TimedWord]] = {}
    failures: dict[int, str] = {}
    with lock:
        missing: list[tuple[int, float, float, Path, dict[str, object], str]] = []
        for index, start, length in specs:
            if cancel is not None and cancel.is_set():
                raise legacy.CancelledError("用户已停止处理或取证预算耗尽。")
            identity = _window_evidence_identity(
                input_path, start, length, audio_stream_index=audio_stream_index,
                ffmpeg=ffmpeg, whisper=whisper, whisper_model=whisper_model,
                audio_language=audio_language, translate_to_english=translate_to_english,
            )
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            failure_key = f"{lock_key}:{digest}"
            if task_failures is not None and digest in task_failures:
                failures[index] = task_failures[digest]
                continue
            recent = _RECENT_WINDOW_FAILURES.get(failure_key)
            if recent is not None:
                if time.monotonic() - recent[0] < TRANSIENT_FAILURE_TTL_SECONDS:
                    failures[index] = recent[1]
                    if task_failures is not None:
                        task_failures[digest] = recent[1]
                    continue
                _RECENT_WINDOW_FAILURES.pop(failure_key, None)
            path = cache_dir / f"{digest}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("identity") == identity and payload.get("events"):
                    events[index] = [
                        TimedText(item.start, item.end, item.text, index)
                        for item in _deserialize_events(payload["events"])
                    ]
                    words[index] = [
                        TimedWord(item.start, item.end, item.text, index, item.confidence)
                        for item in _deserialize_words(payload.get("words", []))
                    ]
                    log(f"取证缓存命中：{start:.3f}～{start + length:.3f}秒。")
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                pass
            missing.append((index, start, length, path, identity, digest))
        if missing:
            with tempfile.TemporaryDirectory(prefix="subflow-evidence-") as folder:
                work = Path(folder)
                # Extract individually so one damaged window does not destroy
                # valid evidence from other regions.
                wavs: dict[int, Path] = {}
                starts: dict[int, float] = {}
                for index, start, length, _path, _identity, digest in missing:
                    try:
                        wavs.update(_extract_clips(
                            input_path, work, [(index, start, length)],
                            audio_stream_index=audio_stream_index, ffmpeg=ffmpeg, cancel=cancel,
                        ))
                        starts[index] = start
                    except legacy.CancelledError:
                        raise
                    except Exception as exc:
                        failures[index] = f"EXTRACTION_ERROR: {exc}"
                        _RECENT_WINDOW_FAILURES[f"{lock_key}:{digest}"] = (time.monotonic(), failures[index])
                        if task_failures is not None:
                            task_failures[digest] = failures[index]
                if wavs:
                    try:
                        transcribed, transcribed_words = _transcribe_clips(
                            wavs, starts, whisper=whisper, whisper_model=whisper_model,
                            audio_language=audio_language,
                            translate_to_english=translate_to_english,
                            cancel=cancel, include_words=True,
                        )
                    except legacy.CancelledError:
                        raise
                    except Exception as exc:
                        transcribed, transcribed_words = {}, {}
                        for index in wavs:
                            failures[index] = f"ASR_ERROR: {exc}"
                    for index, _start, _length, path, identity, digest in missing:
                        if index not in wavs or index in failures:
                            if index in failures and task_failures is not None:
                                task_failures[digest] = failures[index]
                            if index in failures:
                                _RECENT_WINDOW_FAILURES[f"{lock_key}:{digest}"] = (time.monotonic(), failures[index])
                            continue
                        clip_events = transcribed.get(index, [])
                        if not clip_events:
                            failures[index] = "EMPTY_TRANSCRIPT"
                            _RECENT_WINDOW_FAILURES[f"{lock_key}:{digest}"] = (time.monotonic(), failures[index])
                            if task_failures is not None:
                                task_failures[digest] = failures[index]
                            continue
                        events[index] = clip_events
                        words[index] = transcribed_words.get(index, [])
                        payload = {
                            "identity": identity,
                            "events": _serialize_events(clip_events),
                            "words": _serialize_words(words[index]),
                        }
                        temp = path.with_suffix(".tmp")
                        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                        temp.replace(path)
    return events, words, failures


def _cache_signature(
    input_path: str,
    *,
    audio_stream_index: int,
    whisper_model: str,
    audio_language: str,
    translate_to_english: bool,
    probe_duration: float,
    probe_fractions: Sequence[float],
    fingerprint_clips: int,
) -> dict[str, object]:
    video = Path(input_path).resolve()
    model = Path(whisper_model).resolve()
    try:
        video_stat = video.stat()
        video_size = video_stat.st_size
        video_mtime_ns = video_stat.st_mtime_ns
    except OSError:
        video_size = 0
        video_mtime_ns = 0
    try:
        model_stat = model.stat()
        model_size = model_stat.st_size
        model_mtime_ns = model_stat.st_mtime_ns
    except OSError:
        model_size = 0
        model_mtime_ns = 0
    return {
        "version": CACHE_VERSION,
        "video_path": str(video),
        "video_size": video_size,
        "video_mtime_ns": video_mtime_ns,
        "audio_stream_index": int(audio_stream_index),
        "whisper_model_name": model.name,
        "whisper_model_size": model_size,
        "whisper_model_mtime_ns": model_mtime_ns,
        "audio_language": audio_language,
        "translate_to_english": bool(translate_to_english),
        "probe_duration": round(float(probe_duration), 3),
        "probe_fractions": [round(float(value), 4) for value in probe_fractions],
        "fingerprint_clips": int(fingerprint_clips),
        "algorithm": "candidate-independent-grid-affine-v3-word-timing",
    }


def _cache_path(cache_dir: str | Path | None) -> Path | None:
    if cache_dir is None:
        return None
    path = Path(cache_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path / "audio-fingerprint-v3.json"


def _serialize_events(events: Sequence[TimedText]) -> list[dict[str, object]]:
    return [
        {"start": event.start, "end": event.end, "text": event.text, "source_index": event.source_index}
        for event in events
    ]


def _deserialize_events(payload: Sequence[dict[str, object]]) -> list[TimedText]:
    events: list[TimedText] = []
    for item in payload:
        try:
            events.append(TimedText(float(item["start"]), float(item["end"]), str(item["text"]), int(item["source_index"])))
        except (KeyError, TypeError, ValueError):
            continue
    return events


def _serialize_words(words: Sequence[TimedWord]) -> list[dict[str, object]]:
    return [
        {
            "start": word.start,
            "end": word.end,
            "text": word.text,
            "source_index": word.source_index,
            "confidence": word.confidence,
        }
        for word in words
    ]


def _deserialize_words(payload: Sequence[dict[str, object]]) -> list[TimedWord]:
    words: list[TimedWord] = []
    for item in payload:
        try:
            words.append(TimedWord(
                float(item["start"]),
                float(item["end"]),
                str(item["text"]),
                int(item["source_index"]),
                float(item.get("confidence", 0.0)),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return words


def _load_cache(
    path: Path | None,
    signature: dict[str, object],
) -> tuple[dict[int, list[TimedText]], dict[int, list[TimedWord]], set[int], tuple[int, ...]]:
    if path is None or not path.is_file():
        return {}, {}, set(), ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}, set(), ()
    if payload.get("signature") != signature:
        return {}, {}, set(), ()
    clips: dict[int, list[TimedText]] = {}
    word_clips: dict[int, list[TimedWord]] = {}
    for item in payload.get("clips", []):
        try:
            clip_index = int(item["clip_index"])
        except (KeyError, TypeError, ValueError):
            continue
        clips[clip_index] = _deserialize_events(item.get("events", []))
        word_clips[clip_index] = _deserialize_words(item.get("words", []))
    completed: set[int] = set()
    for value in payload.get("completed_clip_indexes", []):
        try:
            completed.add(int(value))
        except (TypeError, ValueError):
            continue
    selected: list[int] = []
    for value in payload.get("selected_clip_indexes", []):
        try:
            selected.append(int(value))
        except (TypeError, ValueError):
            continue
    return clips, word_clips, completed, tuple(selected)


def _save_cache(
    path: Path | None,
    signature: dict[str, object],
    clips: dict[int, list[TimedText]],
    word_clips: dict[int, list[TimedWord]],
    completed: set[int],
    selected: Sequence[int],
) -> None:
    if path is None:
        return
    payload = {
        "signature": signature,
        "completed_clip_indexes": sorted(completed),
        "selected_clip_indexes": list(selected),
        "clips": [
            {
                "clip_index": index,
                "events": _serialize_events(clips.get(index, [])),
                "words": _serialize_words(word_clips.get(index, [])),
            }
            for index in sorted(completed)
        ],
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _run_tool(args: list[str], *, cancel, cwd: str | None = None) -> None:
    legacy.run_command(args, log=lambda _message: None, cancel_event=cancel, cwd=cwd)


def _extract_clips(
    input_path: str,
    work: Path,
    clip_specs: Sequence[tuple[int, float, float]],
    *,
    audio_stream_index: int,
    ffmpeg: str,
    cancel,
) -> dict[int, Path]:
    wavs: dict[int, Path] = {}
    for clip_index, start, clip_duration in clip_specs:
        if cancel is not None and cancel.is_set():
            raise legacy.CancelledError("用户已停止处理。")
        wav = work / f"clip-{clip_index}.wav"
        _run_tool([
            ffmpeg, "-y", "-ss", f"{start:.3f}", "-i", input_path,
            "-t", f"{clip_duration:.3f}", "-map", f"0:a:{audio_stream_index}",
            "-vn", "-ac", "1", "-ar", "16000", str(wav),
        ], cancel=cancel)
        wavs[clip_index] = wav
    return wavs


def _transcribe_clips(
    wavs: dict[int, Path],
    starts: dict[int, float],
    *,
    whisper: str,
    whisper_model: str,
    audio_language: str,
    translate_to_english: bool,
    cancel,
    include_words: bool = False,
) -> dict[int, list[TimedText]] | tuple[dict[int, list[TimedText]], dict[int, list[TimedWord]]]:
    if not wavs:
        return ({}, {}) if include_words else {}
    whisper_language = (audio_language or "auto").strip().lower()
    if whisper_language in {"und", "unknown"}:
        whisper_language = "auto"
    result: dict[int, list[TimedText]] = {}
    word_result: dict[int, list[TimedWord]] = {}
    # whisper.cpp on Windows can fail on a long path containing CJK text even
    # though FFmpeg created the WAV successfully. The probe set is only a few
    # megabytes, so stage it under the system temp directory with short ASCII
    # names and keep the persistent cache in its original location.
    with tempfile.TemporaryDirectory(prefix="subflow-whisper-") as staging_name:
        staging = Path(staging_name)
        staged: dict[int, Path] = {}
        for sequence, clip_index in enumerate(sorted(wavs)):
            staged_wav = staging / f"probe-{sequence:02d}.wav"
            shutil.copyfile(wavs[clip_index], staged_wav)
            staged[clip_index] = staged_wav
        args = [
            whisper, "-m", Path(whisper_model).name, "-l", whisper_language,
            "-t", "6", "-np", "-osrt", "-ojf",
        ]
        if translate_to_english:
            args.append("-tr")
        args.extend(str(staged[index]) for index in sorted(staged))
        _run_tool(args, cancel=cancel, cwd=str(Path(whisper_model).parent))
        for clip_index, wav in staged.items():
            transcript = Path(str(wav) + ".srt")
            if not transcript.exists():
                result[clip_index] = []
                word_result[clip_index] = []
                continue
            start = starts[clip_index]
            result[clip_index] = [
                TimedText(start + event.start, start + event.end, event.text, clip_index)
                for event in _events(transcript, clip_index)
            ]
            json_path = Path(str(wav) + ".json")
            words: list[TimedWord] = []
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            for segment in payload.get("transcription", []):
                for token in segment.get("tokens", []):
                    normalized = _words(str(token.get("text", "")))
                    if len(normalized) != 1:
                        continue
                    try:
                        offsets = token["offsets"]
                        token_start = start + float(offsets["from"]) / 1000.0
                        token_end = start + float(offsets["to"]) / 1000.0
                        confidence = float(token.get("p", 0.0))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if token_end < token_start:
                        continue
                    words.append(TimedWord(
                        token_start,
                        token_end,
                        normalized[0],
                        clip_index,
                        confidence,
                    ))
            word_result[clip_index] = words
    return (result, word_result) if include_words else result


def _flatten_clips(clips: dict[int, list[TimedText]], indexes: Sequence[int]) -> list[TimedText]:
    return [event for index in indexes for event in clips.get(index, [])]


def _dialogue_quality(events: Sequence[TimedText]) -> float:
    text = _clean(" ".join(event.text for event in events))
    words = re.findall(r"[a-z0-9']{2,}", text)
    if len(words) < 4:
        return 0.0
    unique = set(words)
    distinctive = {word for word in unique if word not in _GENERIC_WORDS}
    length_score = min(1.0, len(words) / 10.0)
    unique_score = min(1.0, len(unique) / max(1.0, len(words) * 0.70))
    distinctive_score = min(1.0, len(distinctive) / 5.0)
    maximum_frequency = max(words.count(word) for word in unique)
    repetition_ratio = maximum_frequency / len(words)
    repetition_penalty = max(0.0, (repetition_ratio - 0.28) * 1.2)
    return max(0.0, min(1.0, 0.38 * length_score + 0.25 * unique_score + 0.37 * distinctive_score - repetition_penalty))


def _probe_is_usable(events: Sequence[TimedText]) -> bool:
    words = _words(" ".join(event.text for event in events))
    distinctive = {word for word in words if word not in _GENERIC_WORDS}
    return len(words) >= 5 and len(distinctive) >= 2 and _dialogue_quality(events) >= MIN_PROBE_QUALITY


def _usable_fingerprint_indexes(
    clips: dict[int, list[TimedText]],
    starts: dict[int, float],
) -> tuple[tuple[int, ...], tuple[tuple[int, float], ...]]:
    qualities = {index: _dialogue_quality(events) for index, events in clips.items()}
    usable = tuple(sorted(
        (index for index, events in clips.items() if events and _probe_is_usable(events)),
        key=lambda index: starts[index],
    ))
    return usable, tuple(sorted(qualities.items()))


def _select_fingerprint_indexes(
    clips: dict[int, list[TimedText]],
    starts: dict[int, float],
    *,
    duration_seconds: float,
    maximum: int,
) -> tuple[tuple[int, ...], tuple[tuple[int, float], ...]]:
    qualities = {index: _dialogue_quality(events) for index, events in clips.items()}
    usable = [index for index, events in clips.items() if events and _probe_is_usable(events)]
    if not usable:
        return (), tuple(sorted(qualities.items()))
    selected = sorted(usable, key=lambda index: (qualities[index], -index), reverse=True)[:maximum]
    selected.sort(key=lambda index: starts[index])
    return tuple(selected), tuple(sorted(qualities.items()))


def build_movie_audio_fingerprint(
    input_path: str,
    work_dir: str,
    *,
    duration_seconds: float,
    audio_stream_index: int,
    ffmpeg: str,
    whisper: str,
    whisper_model: str,
    audio_language: str = "en",
    translate_to_english: bool = False,
    log: Callable[[str], None] = print,
    probe_fractions: Iterable[float] = DEFAULT_PROBE_FRACTIONS,
    probe_duration: float = DEFAULT_PROBE_DURATION,
    fingerprint_clips: int = DEFAULT_FINGERPRINT_CLIPS,
    cache_dir: str | None = None,
    cancel=None,
    deadline: float | None = None,
    task_failures: dict[str, str] | None = None,
) -> MovieAudioFingerprint:
    work = Path(work_dir).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    fractions = tuple(float(value) for value in probe_fractions)
    if len(fractions) < 3:
        raise ValueError("probe_fractions 至少需要三个分散采样位置。")
    if any(not 0.0 < value < 1.0 for value in fractions):
        raise ValueError("probe_fractions 必须位于 0 到 1 之间。")
    probe_duration = max(5.0, min(12.0, float(probe_duration)))
    fingerprint_clips = max(3, min(int(fingerprint_clips), len(fractions)))
    combined_cancel = _DeadlineCancel(cancel, deadline)

    # cache_dir remains accepted for callers of the old API; exact-window
    # evidence is stored at the shared movie cache location instead.
    clips: dict[int, list[TimedText]] = {}
    word_clips: dict[int, list[TimedWord]] = {}
    starts = {
        index: _clip_start(duration_seconds, probe_duration, fraction)
        for index, fraction in enumerate(fractions)
    }
    expected = set(starts)
    missing = sorted(expected)

    if missing:
        log(
            "音频主导旁路验证：先独立建立影片音频指纹；"
            f"在全片 {len(fractions)} 个分散位置各取 {probe_duration:.0f} 秒，"
            "一次加载 Whisper；取样完全先于候选字幕匹配。"
        )
        specs = [(index, starts[index], probe_duration) for index in missing]
        extract_started = time.monotonic()
        transcribed, transcribed_words, failures = transcribe_exact_windows(
            input_path, specs, audio_stream_index=audio_stream_index,
            ffmpeg=ffmpeg, whisper=whisper, whisper_model=whisper_model,
            audio_language=audio_language,
            translate_to_english=translate_to_english,
            cancel=combined_cancel, log=log, task_failures=task_failures,
        )
        log(f"音频指纹计时：{len(missing)} 个影片探针取证含缓存等待 {time.monotonic() - extract_started:.2f} 秒。")
        for index in missing:
            clips[index] = transcribed.get(index, [])
            word_clips[index] = transcribed_words.get(index, [])
            if not clips[index] and index in failures:
                log(f"影片探针 {index} 取证失败：{failures[index]}")

    usable, qualities = _usable_fingerprint_indexes(clips, starts)
    preferred, _ = _select_fingerprint_indexes(
        clips, starts,
        duration_seconds=duration_seconds,
        maximum=fingerprint_clips,
    )

    quality_map = dict(qualities)
    if usable:
        preferred_description = "；".join(
            f"探针 {index + 1}@{starts[index] / 60:.1f} 分钟 质量 {quality_map.get(index, 0.0):.0%}"
            for index in preferred
        )
        log(
            f"影片音频指纹保留 {len(usable)} 个候选无关对白探针用于时间建模；"
            f"质量最高的 {len(preferred)} 个为：{preferred_description}。"
        )
    else:
        log("影片音频指纹没有找到可用对白探针。")

    return MovieAudioFingerprint(
        clip_indexes=usable,
        events=tuple(_flatten_clips(clips, usable)),
        qualities=qualities,
        words=tuple(
            word
            for index in usable
            for word in word_clips.get(index, [])
        ),
    )


def verify_file_offset(
    input_path: str,
    subtitle_path: str,
    work_dir: str,
    *,
    duration_seconds: float,
    audio_stream_index: int,
    ffmpeg: str,
    whisper: str,
    whisper_model: str,
    audio_language: str = "en",
    translate_to_english: bool = False,
    log: Callable[[str], None] = print,
    probe_fractions: Iterable[float] = DEFAULT_PROBE_FRACTIONS,
    probe_duration: float = DEFAULT_PROBE_DURATION,
    fingerprint_clips: int = DEFAULT_FINGERPRINT_CLIPS,
    cache_dir: str | None = None,
    cancel=None,
    deadline: float | None = None,
) -> AudioOffsetResult:
    total_started = time.monotonic()
    fingerprint = build_movie_audio_fingerprint(
        input_path,
        str(Path(work_dir) / "movie-fingerprint"),
        duration_seconds=duration_seconds,
        audio_stream_index=audio_stream_index,
        ffmpeg=ffmpeg,
        whisper=whisper,
        whisper_model=whisper_model,
        audio_language=audio_language,
        translate_to_english=translate_to_english,
        log=log,
        probe_fractions=probe_fractions,
        probe_duration=probe_duration,
        fingerprint_clips=fingerprint_clips,
        cache_dir=cache_dir,
        cancel=cancel,
        deadline=deadline,
    )

    # Candidate is intentionally read only after the movie fingerprint is fixed.
    candidate = _events(Path(subtitle_path), -1)
    if fingerprint.usable_clip_count < 2:
        result = AudioOffsetResult(
            False, None, (), (),
            f"影片音频指纹只有 {fingerprint.usable_clip_count} 个可用独立对白探针，证据不足",
        )
    else:
        match_started = time.monotonic()
        result = locate_offset(candidate, fingerprint.events, duration_seconds=duration_seconds)
        log(f"音频指纹计时：候选字幕文本定位/时间建模 {time.monotonic() - match_started:.3f} 秒。")

    if result.accepted and result.affine:
        log(
            f"音频主导旁路验证：线性时间模型通过；比例 {result.scale_label}，"
            f"起始偏移 {result.offset_seconds:+.2f} 秒，"
            f"每小时漂移 {result.drift_per_hour:+.2f} 秒；{result.reason}。"
        )
    else:
        offset_text = "未知" if result.offset_seconds is None else f"{result.offset_seconds:+.2f} 秒"
        log(f"音频主导旁路验证：偏移 {offset_text}；{result.reason}。")
    for anchor, residual in zip(result.anchors, result.residuals or (0.0,) * len(result.anchors)):
        equivalent = result.equivalent_offset(anchor.subtitle_time)
        equivalent_text = f"，该时点等效偏移 {equivalent:+.2f} 秒" if equivalent is not None and result.affine else ""
        residual_text = f"，模型残差 {residual:.2f} 秒" if result.residuals else ""
        log(
            f"音频锚点 {anchor.clip_index + 1}：原始时差 {anchor.offset_seconds:+.2f} 秒，"
            f"匹配 {anchor.score:.0%}{equivalent_text}{residual_text}。"
        )
    log(f"音频主导旁路验证总耗时：{time.monotonic() - total_started:.2f} 秒。")
    return result


def match_subtitle_to_fingerprint(
    subtitle_path: str | Path,
    fingerprint: MovieAudioFingerprint,
    *,
    duration_seconds: float,
    temporal_corroboration: bool = False,
) -> AudioOffsetResult:
    """Match one subtitle to an already-built movie fingerprint.

    This performs no FFmpeg or Whisper work.  Numeric similarities remain an
    internal tolerance mechanism; callers can expose only evidence states.
    """
    candidate = _events(Path(subtitle_path), -1)
    if fingerprint.usable_clip_count < 2:
        return AudioOffsetResult(
            False,
            None,
            (),
            (),
            f"影片公共台词只有 {fingerprint.usable_clip_count} 个可用分散位置",
        )
    result = locate_offset(
        candidate,
        fingerprint.events,
        duration_seconds=duration_seconds,
    )
    if result.accepted or not temporal_corroboration:
        return result
    corroborated = _locate_with_temporal_corroboration(
        candidate,
        fingerprint,
        duration_seconds=duration_seconds,
    )
    return corroborated if corroborated is not None else result


def _locate_with_temporal_corroboration(
    candidate_events: Sequence[TimedText],
    fingerprint: MovieAudioFingerprint,
    *,
    duration_seconds: float,
) -> AudioOffsetResult | None:
    """Confirm weak translated wording only when distant timing also agrees.

    This is limited to the caller-selected cross-language path.  It never
    creates evidence from timing alone: at least three distant clips need
    meaningful wording overlap, including one stronger location, and their
    independently observed offsets must form one tight fixed-offset cluster.
    """
    evidence: list[OffsetAnchor] = []
    for clip_index in fingerprint.clip_indexes:
        observations = [
            event for event in fingerprint.events
            if event.source_index == clip_index
        ]
        choices: list[tuple[float, float, TimedText, TimedText]] = []
        for observed in observations:
            movie_time = (observed.start + observed.end) / 2.0
            for candidate in candidate_events:
                subtitle_time = (candidate.start + candidate.end) / 2.0
                offset = movie_time - subtitle_time
                if abs(offset) > 10.0:
                    continue
                similarity = dialogue_score(observed.text, candidate.text)
                if similarity >= 0.32:
                    choices.append((similarity, -abs(offset), observed, candidate))
        if not choices:
            continue
        similarity, _distance_key, observed, candidate = max(
            choices,
            key=lambda item: (item[0], item[1]),
        )
        movie_time = (observed.start + observed.end) / 2.0
        subtitle_time = (candidate.start + candidate.end) / 2.0
        evidence.append(OffsetAnchor(
            clip_index=clip_index,
            movie_time=movie_time,
            subtitle_time=subtitle_time,
            offset_seconds=movie_time - subtitle_time,
            score=similarity,
            margin=0.0,
            observed_text=observed.text,
            candidate_text=candidate.text,
            candidate_start=candidate.start,
            candidate_end=candidate.end,
            observation_start=observed.start,
            observation_end=observed.end,
            observation_segment_count=observed.segment_count,
        ))
    if len(evidence) < 3:
        return None

    median_offset = statistics.median(anchor.offset_seconds for anchor in evidence)
    inliers = tuple(
        anchor for anchor in evidence
        if abs(anchor.offset_seconds - median_offset) <= 0.85
    )
    if len(inliers) < 3:
        return None
    if max(anchor.score for anchor in inliers) < 0.42:
        return None
    if statistics.fmean(anchor.score for anchor in inliers) < 0.38:
        return None
    span = max(anchor.movie_time for anchor in inliers) - min(
        anchor.movie_time for anchor in inliers
    )
    minimum_span = max(
        600.0,
        duration_seconds * 0.25 if duration_seconds > 0 else 600.0,
    )
    if span < minimum_span:
        return None

    offset = statistics.median(anchor.offset_seconds for anchor in inliers)
    residuals = tuple(abs(anchor.offset_seconds - offset) for anchor in inliers)
    if max(residuals, default=0.0) > 0.85:
        return None
    return AudioOffsetResult(
        True,
        offset,
        inliers,
        tuple(anchor.score for anchor in inliers),
        f"{len(inliers)} 个跨语言远距离位置共同支持固定偏移",
        scale=1.0,
        scale_label="1.000000",
        residuals=residuals,
    )
