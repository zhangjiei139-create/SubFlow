# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import statistics
import time
import unicodedata
from dataclasses import dataclass
from typing import Sequence

_GENERIC_WORDS = {
    "a", "an", "and", "are", "be", "but", "come", "do", "go", "hey", "i", "is",
    "it", "just", "me", "my", "no", "not", "of", "oh", "okay", "on", "so", "that",
    "the", "this", "to", "uh", "um", "we", "what", "yeah", "yes", "you", "your",
}
_CONTRACTIONS = {
    "ain't": ("am", "not"), "aren't": ("are", "not"), "can't": ("can", "not"),
    "couldn't": ("could", "not"), "didn't": ("did", "not"),
    "doesn't": ("does", "not"), "don't": ("do", "not"),
    "hadn't": ("had", "not"), "hasn't": ("has", "not"),
    "haven't": ("have", "not"), "he's": ("he", "is"),
    "i'd": ("i", "would"), "i'll": ("i", "will"), "i'm": ("i", "am"),
    "i've": ("i", "have"), "isn't": ("is", "not"), "it's": ("it", "is"),
    "let's": ("let", "us"), "she's": ("she", "is"),
    "shouldn't": ("should", "not"), "that's": ("that", "is"),
    "they're": ("they", "are"), "they've": ("they", "have"),
    "wasn't": ("was", "not"), "we're": ("we", "are"),
    "weren't": ("were", "not"), "what's": ("what", "is"),
    "won't": ("will", "not"), "wouldn't": ("would", "not"),
    "you're": ("you", "are"), "you've": ("you", "have"),
}

DIAGNOSTIC_VERSION = "local_timing_v1"

@dataclass(frozen=True)
class ClauseCue:
    cue_id: int
    start: float
    end: float
    text: str

@dataclass(frozen=True)
class ClauseWindow:
    start: float
    end: float
    text: str
    cues: tuple[ClauseCue, ...] = ()
    stage3_score: float = 0.0

@dataclass(frozen=True)
class ClauseWindowEvidence:
    start: float
    end: float
    window_cue_ids: tuple[int, ...]
    cue_ranges: tuple[tuple[int, float, float], ...]
    cue_gaps: tuple[float, ...]
    matched_cue_ids: tuple[int, ...]
    matched_words_by_cue: tuple[tuple[int, int], ...]
    content_pairs: tuple[tuple[int, int], ...]
    full_pairs: tuple[tuple[int, int], ...]
    observed_content_count: int
    subtitle_content_count: int
    observed_full_count: int
    subtitle_full_count: int
    subtitle_content_proxy_times: tuple[float, ...]
    stage3_score: float

@dataclass(frozen=True)
class ClauseLocationEvidence:
    start: float
    end: float
    matched_content_words: int
    asr_content_coverage: float
    subtitle_content_coverage: float
    longest_ordered_run: int
    longest_contiguous_run: int
    internal_gap_count: int
    location_score: float
    member_ranges: tuple[tuple[float, float], ...]
    observed_token_count: int = 0
    subtitle_token_count: int = 0
    asr_match_start: int | None = None
    asr_match_end: int | None = None
    subtitle_match_start: int | None = None
    subtitle_match_end: int | None = None
    asr_unmatched_prefix: int = 0
    asr_unmatched_suffix: int = 0
    subtitle_unmatched_prefix: int = 0
    subtitle_unmatched_suffix: int = 0
    matched_cue_ids: tuple[int, ...] = ()
    crosses_cue_boundary: bool = False
    window_evidence: tuple[ClauseWindowEvidence, ...] = ()

@dataclass(frozen=True)
class ClauseAnalysis:
    matches_before_dedup: int
    locations: tuple[ClauseLocationEvidence, ...]
    elapsed_ms: float = 0.0

@dataclass(frozen=True)
class ClauseDiagnostic:
    status: str
    match_type: str
    matches_before_dedup: int
    locations_after_dedup: int
    matched_content_words: int
    asr_content_coverage: float
    subtitle_content_coverage: float
    longest_ordered_run: int
    longest_contiguous_run: int
    internal_gap_count: int
    clause_uniqueness_margin: float
    selected_location_start: float | None
    selected_location_end: float | None
    reason: str
    observed_token_count: int = 0
    subtitle_token_count: int = 0
    asr_match_start: int | None = None
    asr_match_end: int | None = None
    subtitle_match_start: int | None = None
    subtitle_match_end: int | None = None
    asr_unmatched_prefix: int = 0
    asr_unmatched_suffix: int = 0
    subtitle_unmatched_prefix: int = 0
    subtitle_unmatched_suffix: int = 0
    matched_cue_ids: tuple[int, ...] = ()
    crosses_cue_boundary: bool = False
    elapsed_ms: float = 0.0
    window_evidence: tuple[ClauseWindowEvidence, ...] = ()

@dataclass(frozen=True)
class LocalTimingDiagnostic:
    diagnostic_version: str
    content_match_type: str
    classification: str
    matched_cue_ids: tuple[int, ...]
    match_crosses_cue_boundary: bool
    asr_matched_token_start: int | None
    asr_matched_token_end: int | None
    subtitle_matched_token_start: int | None
    subtitle_matched_token_end: int | None
    composite_window_token_start: int
    composite_window_token_end: int
    asr_unmatched_prefix: int
    asr_unmatched_suffix: int
    subtitle_unmatched_prefix: int
    subtitle_unmatched_suffix: int
    raw_offset_seconds: float
    reason: str

@dataclass(frozen=True)
class CompositeWindowMemberDiagnostic:
    start: float
    end: float
    cue_count: int
    cue_ids: tuple[int, ...]
    cue_gaps: tuple[float, ...]
    total_gap_seconds: float
    maximum_gap_seconds: float
    matched_cue_ids: tuple[int, ...]
    matched_words_by_cue: tuple[tuple[int, int], ...]
    matched_position_normalized: float | None
    midpoint_offset: float
    matched_center_proxy_offset: float | None
    full_start_aligned: bool
    full_end_aligned: bool

@dataclass(frozen=True)
class CompositeWindowGeometryDiagnostic:
    diagnostic_version: str
    available: bool
    observation_segment_count: int
    observation_duration: float
    anchor_midpoint_offset: float
    selected_window_start: float | None
    selected_window_end: float | None
    selected_boundary_type: str
    observation_end_proxy_offset: float | None
    selected_matched_center_proxy_offset: float | None
    window_offset_span: float | None
    members: tuple[CompositeWindowMemberDiagnostic, ...]
    reason: str

def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    normalized = normalized.replace("’", "'").replace("‘", "'")
    raw = re.findall(r"[a-z0-9]+(?:'[a-z]+)?", normalized)
    expanded: list[str] = []
    for token in raw:
        expanded.extend(_CONTRACTIONS.get(token, (token,)))
    return expanded

def _content_tokens(value: str) -> list[str]:
    return [token for token in _tokens(value) if token not in _GENERIC_WORDS]

def _lcs_pairs(left: Sequence[str], right: Sequence[str]) -> tuple[tuple[int, int], ...]:
    table = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for left_index in range(len(left) - 1, -1, -1):
        for right_index in range(len(right) - 1, -1, -1):
            if left[left_index] == right[right_index]:
                table[left_index][right_index] = 1 + table[left_index + 1][right_index + 1]
            else:
                table[left_index][right_index] = max(
                    table[left_index + 1][right_index],
                    table[left_index][right_index + 1],
                )
    pairs: list[tuple[int, int]] = []
    left_index = right_index = 0
    while left_index < len(left) and right_index < len(right):
        if left[left_index] == right[right_index]:
            pairs.append((left_index, right_index))
            left_index += 1
            right_index += 1
        elif table[left_index + 1][right_index] >= table[left_index][right_index + 1]:
            left_index += 1
        else:
            right_index += 1
    return tuple(pairs)

def _window_evidence(observed_text: str, window: ClauseWindow) -> ClauseLocationEvidence | None:
    observed = _content_tokens(observed_text)
    candidate = _content_tokens(window.text)
    if not observed or not candidate:
        return None
    pairs = _lcs_pairs(observed, candidate)
    if not pairs:
        return None
    contiguous = longest_contiguous = 1
    gaps = 0
    for prior, current in zip(pairs, pairs[1:]):
        if current[0] == prior[0] + 1 and current[1] == prior[1] + 1:
            contiguous += 1
            longest_contiguous = max(longest_contiguous, contiguous)
        else:
            contiguous = 1
            gaps += 1
    matched = len(pairs)
    asr_coverage = matched / len(observed)
    subtitle_coverage = matched / len(candidate)
    asr_start, subtitle_start = pairs[0]
    asr_end, subtitle_end = pairs[-1][0] + 1, pairs[-1][1] + 1

    content_token_cue_ids: list[int] = []
    content_token_proxy_times: list[float] = []
    full_token_cue_ids: list[int] = []
    if window.cues:
        for cue in window.cues:
            content = _content_tokens(cue.text)
            full = _tokens(cue.text)
            duration = max(0.0, cue.end - cue.start)
            content_token_cue_ids.extend([cue.cue_id] * len(content))
            full_token_cue_ids.extend([cue.cue_id] * len(full))
            content_token_proxy_times.extend(
                cue.start + duration * (index + 0.5) / len(content)
                for index in range(len(content))
            )
    else:
        duration = max(0.0, window.end - window.start)
        content_token_proxy_times.extend(
            window.start + duration * (index + 0.5) / len(candidate)
            for index in range(len(candidate))
        )

    cue_ids = tuple(dict.fromkeys(
        content_token_cue_ids[index]
        for _, index in pairs
        if index < len(content_token_cue_ids)
    ))
    matched_counts: dict[int, int] = {}
    for _, index in pairs:
        if index < len(content_token_cue_ids):
            cue_id = content_token_cue_ids[index]
            matched_counts[cue_id] = matched_counts.get(cue_id, 0) + 1

    observed_full = _tokens(observed_text)
    candidate_full = _tokens(window.text)
    full_pairs = _lcs_pairs(observed_full, candidate_full)
    cue_ranges = tuple((cue.cue_id, cue.start, cue.end) for cue in window.cues)
    cue_gaps = tuple(
        max(0.0, right.start - left.end)
        for left, right in zip(window.cues, window.cues[1:])
    )
    member = ClauseWindowEvidence(
        window.start,
        window.end,
        tuple(cue.cue_id for cue in window.cues),
        cue_ranges,
        cue_gaps,
        cue_ids,
        tuple(sorted(matched_counts.items())),
        pairs,
        full_pairs,
        len(observed),
        len(candidate),
        len(observed_full),
        len(candidate_full),
        tuple(content_token_proxy_times),
        window.stage3_score,
    )
    return ClauseLocationEvidence(
        window.start, window.end, matched, asr_coverage, subtitle_coverage,
        matched, longest_contiguous, gaps, min(asr_coverage, subtitle_coverage),
        ((window.start, window.end),),
        len(observed), len(candidate), asr_start, asr_end,
        subtitle_start, subtitle_end, asr_start, len(observed) - asr_end,
        subtitle_start, len(candidate) - subtitle_end, cue_ids, len(cue_ids) > 1,
        (member,),
    )

def _overlap_ratio(left: tuple[float, float], right: tuple[float, float]) -> float:
    overlap = max(0.0, min(left[1], right[1]) - max(left[0], right[0]))
    shorter = min(max(0.0, left[1] - left[0]), max(0.0, right[1] - right[0]))
    return overlap / shorter if shorter > 0 else 0.0

def _same_subtitle_location(
    evidence: ClauseLocationEvidence,
    canonical: ClauseLocationEvidence,
) -> bool:
    current = (evidence.start, evidence.end)
    representative = (canonical.start, canonical.end)
    evidence_cues = set(evidence.matched_cue_ids)
    canonical_cues = set(canonical.matched_cue_ids)
    if evidence_cues and canonical_cues:
        overlap = len(evidence_cues.intersection(canonical_cues))
        return overlap / min(len(evidence_cues), len(canonical_cues)) >= 0.50
    return _overlap_ratio(current, representative) >= 0.50

def _quality_key(evidence: ClauseLocationEvidence) -> tuple[float, int, int, int]:
    return (
        evidence.location_score,
        evidence.longest_contiguous_run,
        evidence.matched_content_words,
        -evidence.internal_gap_count,
    )

def analyze_clause_windows(observed_text: str, windows: Sequence[ClauseWindow]) -> ClauseAnalysis:
    started = time.perf_counter()
    matches = tuple(
        evidence
        for window in windows
        if (evidence := _window_evidence(observed_text, window)) is not None
        and evidence.matched_content_words >= 2
    )
    canonical: list[ClauseLocationEvidence] = []
    for evidence in sorted(matches, key=_quality_key, reverse=True):
        location_index = next(
            (index for index, location in enumerate(canonical)
             if _same_subtitle_location(evidence, location)),
            None,
        )
        if location_index is None:
            canonical.append(evidence)
            continue
        location = canonical[location_index]
        canonical[location_index] = ClauseLocationEvidence(
            location.start, location.end, location.matched_content_words,
            location.asr_content_coverage, location.subtitle_content_coverage,
            location.longest_ordered_run, location.longest_contiguous_run,
            location.internal_gap_count, location.location_score,
            location.member_ranges + ((evidence.start, evidence.end),),
            location.observed_token_count, location.subtitle_token_count,
            location.asr_match_start, location.asr_match_end,
            location.subtitle_match_start, location.subtitle_match_end,
            location.asr_unmatched_prefix, location.asr_unmatched_suffix,
            location.subtitle_unmatched_prefix, location.subtitle_unmatched_suffix,
            location.matched_cue_ids, location.crosses_cue_boundary,
            location.window_evidence + evidence.window_evidence,
        )
    return ClauseAnalysis(
        len(matches), tuple(canonical), (time.perf_counter() - started) * 1000.0,
    )

def diagnose_selected_location(
    analysis: ClauseAnalysis,
    *,
    selected_start: float,
    selected_end: float,
) -> ClauseDiagnostic:
    selected_range = (selected_start, selected_end)
    selected = next(
        (location for location in analysis.locations
         if any(member == selected_range or _overlap_ratio(member, selected_range) >= 0.50
                for member in location.member_ranges)),
        None,
    )
    if selected is None:
        return ClauseDiagnostic(
            "NO_CLAUSE_MATCH", "NONE", analysis.matches_before_dedup,
            len(analysis.locations), 0, 0.0, 0.0, 0, 0, 0, 0.0,
            None, None, "所选字幕时间位置没有形成可靠的精确 token 子句匹配",
            elapsed_ms=analysis.elapsed_ms,
        )
    alternative_score = max(
        (location.location_score for location in analysis.locations if location is not selected),
        default=0.0,
    )
    uniqueness = max(0.0, selected.location_score - alternative_score)
    basic = (
        selected.matched_content_words >= 3
        and selected.longest_contiguous_run >= 2
        and max(selected.asr_content_coverage, selected.subtitle_content_coverage) >= 0.60
        and min(selected.asr_content_coverage, selected.subtitle_content_coverage) >= 0.35
    )
    strong = (
        selected.matched_content_words >= 4
        and selected.longest_contiguous_run >= 3
        and max(selected.asr_content_coverage, selected.subtitle_content_coverage) >= 0.80
        and min(selected.asr_content_coverage, selected.subtitle_content_coverage) >= 0.50
        and selected.internal_gap_count <= 1
        and uniqueness >= 0.12
    )
    if strong:
        status = "CLAUSE_STRONG_CANDIDATE"
        reason = "精确 token 子句在当前字幕位置连续、有序且具有独立位置优势"
    elif basic:
        status = "CLAUSE_WEAK_CANDIDATE"
        reason = "存在精确 token 有序子句，但证据强度或不同位置唯一性不足"
    else:
        status = "NO_CLAUSE_MATCH"
        reason = "精确 token 子句的词数、覆盖或连续性不足"
    return ClauseDiagnostic(
        status, "CLAUSE_MATCH" if basic else "NONE",
        analysis.matches_before_dedup, len(analysis.locations),
        selected.matched_content_words, selected.asr_content_coverage,
        selected.subtitle_content_coverage, selected.longest_ordered_run,
        selected.longest_contiguous_run, selected.internal_gap_count, uniqueness,
        selected_start, selected_end, reason,
        selected.observed_token_count, selected.subtitle_token_count,
        selected.asr_match_start, selected.asr_match_end,
        selected.subtitle_match_start, selected.subtitle_match_end,
        selected.asr_unmatched_prefix, selected.asr_unmatched_suffix,
        selected.subtitle_unmatched_prefix, selected.subtitle_unmatched_suffix,
        selected.matched_cue_ids, selected.crosses_cue_boundary,
        analysis.elapsed_ms, selected.window_evidence,
    )

def build_local_timing_diagnostic(
    diagnostic: ClauseDiagnostic | None,
    *,
    full_match: bool,
    raw_offset_seconds: float,
) -> LocalTimingDiagnostic:
    content_type = "FULL" if full_match else (
        "CLAUSE" if diagnostic is not None and diagnostic.status == "CLAUSE_STRONG_CANDIDATE"
        else "NONE"
    )
    if diagnostic is None or content_type == "NONE" or diagnostic.asr_match_start is None:
        classification = "UNRESOLVED"
        reason = "没有可用于结构诊断的 FULL/CLAUSE strong 精确 token 匹配"
    elif diagnostic.crosses_cue_boundary:
        classification = "CROSS_CUE"
        reason = "匹配正文跨越多个原始字幕 cue，无法由组合窗口代表单一时间边界"
    else:
        start_aligned = diagnostic.asr_unmatched_prefix == 0 and diagnostic.subtitle_unmatched_prefix == 0
        end_aligned = diagnostic.asr_unmatched_suffix == 0 and diagnostic.subtitle_unmatched_suffix == 0
        if start_aligned and end_aligned:
            classification = "BOTH_BOUNDARIES"
            reason = "ASR 与字幕匹配正文的首尾边界均对齐"
        elif start_aligned:
            classification = "START_ALIGNED"
            reason = "ASR 与字幕匹配正文仅起始边界对齐"
        elif end_aligned:
            classification = "END_ALIGNED"
            reason = "ASR 与字幕匹配正文仅结束边界对齐"
        else:
            classification = "INTERIOR_ONLY"
            reason = "匹配正文位于至少一侧文本内部，不能证明 cue 时间边界"
    return LocalTimingDiagnostic(
        DIAGNOSTIC_VERSION, content_type, classification,
        diagnostic.matched_cue_ids if diagnostic else (),
        diagnostic.crosses_cue_boundary if diagnostic else False,
        diagnostic.asr_match_start if diagnostic else None,
        diagnostic.asr_match_end if diagnostic else None,
        diagnostic.subtitle_match_start if diagnostic else None,
        diagnostic.subtitle_match_end if diagnostic else None,
        0, diagnostic.subtitle_token_count if diagnostic else 0,
        diagnostic.asr_unmatched_prefix if diagnostic else 0,
        diagnostic.asr_unmatched_suffix if diagnostic else 0,
        diagnostic.subtitle_unmatched_prefix if diagnostic else 0,
        diagnostic.subtitle_unmatched_suffix if diagnostic else 0,
        raw_offset_seconds, reason,
    )

def summarize_local_timing(
    diagnostics: Sequence[LocalTimingDiagnostic],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[float]] = {}
    for item in diagnostics:
        grouped.setdefault(item.classification, []).append(item.raw_offset_seconds)
    output: dict[str, dict[str, float | int]] = {}
    for classification, offsets in grouped.items():
        median = statistics.median(offsets)
        output[classification] = {
            "count": len(offsets),
            "median": median,
            "mad": statistics.median(abs(value - median) for value in offsets),
            "minimum": min(offsets),
            "maximum": max(offsets),
            "span": max(offsets) - min(offsets),
        }
    return output


def _full_boundary_type(member: ClauseWindowEvidence) -> tuple[str, bool, bool]:
    if not member.full_pairs:
        return "EDGE_UNRESOLVED", False, False
    start_aligned = member.full_pairs[0] == (0, 0)
    end_aligned = member.full_pairs[-1] == (
        member.observed_full_count - 1,
        member.subtitle_full_count - 1,
    )
    if start_aligned and end_aligned:
        return "BOTH_BOUNDARIES", True, True
    if start_aligned:
        return "START_ALIGNED", True, False
    if end_aligned:
        return "END_ALIGNED", False, True
    return "INTERIOR_ONLY", False, False


def build_composite_window_geometry(
    diagnostic: ClauseDiagnostic | None,
    *,
    observation_start: float,
    observation_end: float,
    observation_segment_count: int,
    raw_offset_seconds: float,
) -> CompositeWindowGeometryDiagnostic:
    observation_duration = max(0.0, observation_end - observation_start)
    if diagnostic is None or not diagnostic.window_evidence:
        return CompositeWindowGeometryDiagnostic(
            "composite_window_geometry_v1", False, observation_segment_count,
            observation_duration, raw_offset_seconds, None, None,
            "EDGE_UNRESOLVED", None, None, None, (),
            "没有保留下来的同位置组合窗口证据",
        )

    observation_midpoint = (observation_start + observation_end) / 2.0
    member_sources = list(diagnostic.window_evidence)
    positive_scores = [member.stage3_score for member in member_sources if member.stage3_score > 0.0]
    if positive_scores:
        best_stage3_score = max(positive_scores)
        member_sources = [
            member for member in member_sources
            if member.stage3_score >= 0.50
            and member.stage3_score >= best_stage3_score - 0.20
        ]
    members: list[CompositeWindowMemberDiagnostic] = []
    for member in member_sources:
        matched_subtitle_indexes = [pair[1] for pair in member.content_pairs]
        normalized = (
            statistics.median(
                (index + 0.5) / member.subtitle_content_count
                for index in matched_subtitle_indexes
            )
            if matched_subtitle_indexes and member.subtitle_content_count
            else None
        )
        subtitle_proxy_times = [
            member.subtitle_content_proxy_times[index]
            for index in matched_subtitle_indexes
            if index < len(member.subtitle_content_proxy_times)
        ]
        asr_proxy_times = [
            observation_start
            + observation_duration * (index + 0.5) / member.observed_content_count
            for index, _ in member.content_pairs
            if member.observed_content_count
        ]
        matched_center_proxy = (
            statistics.median(asr_proxy_times) - statistics.median(subtitle_proxy_times)
            if asr_proxy_times and len(asr_proxy_times) == len(subtitle_proxy_times)
            else None
        )
        _, full_start_aligned, full_end_aligned = _full_boundary_type(member)
        cue_gaps = member.cue_gaps
        members.append(CompositeWindowMemberDiagnostic(
            member.start,
            member.end,
            len(member.window_cue_ids),
            member.window_cue_ids,
            cue_gaps,
            sum(cue_gaps),
            max(cue_gaps, default=0.0),
            member.matched_cue_ids,
            member.matched_words_by_cue,
            normalized,
            observation_midpoint - (member.start + member.end) / 2.0,
            matched_center_proxy,
            full_start_aligned,
            full_end_aligned,
        ))

    selected_index = next(
        (
            index
            for index, member in enumerate(member_sources)
            if member.start == diagnostic.selected_location_start
            and member.end == diagnostic.selected_location_end
        ),
        0,
    )
    selected_source = member_sources[selected_index]
    selected = members[selected_index]
    boundary_type, _, full_end_aligned = _full_boundary_type(selected_source)
    end_proxy = observation_end - selected.end if full_end_aligned else None
    midpoint_offsets = [member.midpoint_offset for member in members]
    return CompositeWindowGeometryDiagnostic(
        "composite_window_geometry_v1",
        True,
        observation_segment_count,
        observation_duration,
        raw_offset_seconds,
        selected.start,
        selected.end,
        boundary_type,
        end_proxy,
        selected.matched_center_proxy_offset,
        max(midpoint_offsets) - min(midpoint_offsets) if midpoint_offsets else None,
        tuple(members),
        "仅使用已有 ASR observation、字幕 cue 与 CLAUSE token 证据；未运行新 Whisper",
    )


def _metric_payload(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "median": None, "mad": None, "span": None}
    center = statistics.median(values)
    return {
        "count": len(values),
        "median": center,
        "mad": statistics.median(abs(value - center) for value in values),
        "span": max(values) - min(values),
    }


def summarize_geometry_comparisons(
    diagnostics: Sequence[CompositeWindowGeometryDiagnostic],
) -> dict[str, dict[str, object]]:
    def comparison(name: str, getter) -> tuple[str, dict[str, object]]:
        comparable = [
            item for item in diagnostics
            if item.available and getter(item) is not None
        ]
        midpoint = [item.anchor_midpoint_offset for item in comparable]
        proxy = [float(getter(item)) for item in comparable]
        return name, {
            "anchor_total": len(diagnostics),
            "anchor_comparable": len(comparable),
            "midpoint": _metric_payload(midpoint),
            "proxy": _metric_payload(proxy),
        }

    return dict((
        comparison(
            "midpoint_vs_observation_end_proxy",
            lambda item: item.observation_end_proxy_offset,
        ),
        comparison(
            "midpoint_vs_matched_center_proxy",
            lambda item: item.selected_matched_center_proxy_offset,
        ),
    ))
