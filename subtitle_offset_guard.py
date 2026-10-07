# -*- coding: utf-8 -*-
from __future__ import annotations

import difflib
import statistics
from dataclasses import dataclass
from pathlib import Path

import audio_offset_verifier
import subtitle_tool_core as legacy


MIN_REGIONS = 3
MIN_TOKEN_CONFIDENCE = 0.45
MIN_CUE_WORDS = 3
MIN_CUE_COVERAGE = 0.60
ORIGINAL_GOOD_SECONDS = 0.80
APPLIED_GOOD_SECONDS = 0.90
MIN_ABSOLUTE_IMPROVEMENT = 0.25
MIN_RELATIVE_IMPROVEMENT = 0.50
MAX_PROPOSAL_DISAGREEMENT = 0.85
REGION_CLUSTER_TOLERANCE_SECONDS = 1.00
MAX_ABSOLUTE_REGION_OFFSET_SECONDS = 10.0
GUARD_RULE_VERSION = "regional-evidence-v6-intra-window-conflict"


@dataclass(frozen=True)
class OffsetGuardDecision:
    eligible: bool
    status: str
    selected_offset: float | None
    proposed_offset: float
    region_offsets: tuple[float, ...]
    baseline_cost: float | None
    shifted_cost: float | None
    reason: str


@dataclass(frozen=True)
class _CueWord:
    text: str
    cue_index: int
    word_index: int
    word_count: int
    cue_start: float
    cue_end: float


def _candidate_words_for_region(
    events: list[legacy.SubtitleEvent],
    *,
    observed_start: float,
    observed_end: float,
    proposed_offset: float,
) -> list[_CueWord]:
    expected_start = observed_start - proposed_offset - 12.0
    expected_end = observed_end - proposed_offset + 12.0
    result: list[_CueWord] = []
    for cue_index, event in enumerate(events):
        cue_start = audio_offset_verifier._seconds(event.start)
        cue_end = audio_offset_verifier._seconds(event.end)
        if cue_end < expected_start or cue_start > expected_end:
            continue
        words = audio_offset_verifier._words(event.text)
        for word_index, word in enumerate(words):
            result.append(_CueWord(
                word,
                cue_index,
                word_index,
                len(words),
                cue_start,
                cue_end,
            ))
    return result


def _cue_offsets_by_clip(
    subtitle_path: str | Path,
    fingerprint: audio_offset_verifier.MovieAudioFingerprint,
    proposed_offset: float,
) -> dict[int, tuple[float, ...]]:
    events = legacy.parse_subtitle(Path(subtitle_path))
    if not events or not fingerprint.words:
        return {}

    offsets: dict[int, tuple[float, ...]] = {}
    for clip_index in fingerprint.clip_indexes:
        observed = sorted(
            (
                word for word in fingerprint.words
                if word.source_index == clip_index
                and word.confidence >= MIN_TOKEN_CONFIDENCE
            ),
            key=lambda word: (word.start, word.end),
        )
        if len(observed) < MIN_CUE_WORDS:
            continue
        candidate = _candidate_words_for_region(
            events,
            observed_start=observed[0].start,
            observed_end=observed[-1].end,
            proposed_offset=proposed_offset,
        )
        if len(candidate) < MIN_CUE_WORDS:
            continue

        matcher = difflib.SequenceMatcher(
            None,
            [word.text for word in candidate],
            [word.text for word in observed],
            autojunk=False,
        )
        by_cue: dict[int, list[tuple[_CueWord, audio_offset_verifier.TimedWord]]] = {}
        for block in matcher.get_matching_blocks():
            for index in range(block.size):
                cue_word = candidate[block.a + index]
                audio_word = observed[block.b + index]
                by_cue.setdefault(cue_word.cue_index, []).append((cue_word, audio_word))

        cue_offsets: list[float] = []
        for matches in by_cue.values():
            unique_indexes = {match[0].word_index for match in matches}
            cue_word = matches[0][0]
            if len(unique_indexes) < MIN_CUE_WORDS:
                continue
            if len(unique_indexes) / max(1, cue_word.word_count) < MIN_CUE_COVERAGE:
                continue
            if min(unique_indexes) > 1 or max(unique_indexes) < cue_word.word_count - 2:
                continue
            first_match = min(matches, key=lambda match: match[0].word_index)
            if first_match[0].word_index != 0:
                # A sentence matched only from its second word cannot locate
                # the subtitle's start without guessing the missing word's
                # duration. It can still establish content, not timing.
                continue
            # Compare like-for-like starts. A subtitle may stay visible long
            # after speech ends, so comparing the two full-span midpoints can
            # invent a fixed offset even when the cue onset is synchronized.
            cue_offsets.append(first_match[1].start - cue_word.cue_start)

        if cue_offsets:
            offsets[clip_index] = tuple(cue_offsets)
    return offsets


def conflicted_clip_offsets(
    subtitle_path: str | Path,
    fingerprint: audio_offset_verifier.MovieAudioFingerprint,
    proposed_offset: float,
) -> dict[int, tuple[float, ...]]:
    """Keep contradictory dialogue within one audio window visible to callers."""
    return {
        index: values
        for index, values in _cue_offsets_by_clip(
            subtitle_path, fingerprint, proposed_offset
        ).items()
        if max(values) - min(values) > REGION_CLUSTER_TOLERANCE_SECONDS
    }


def _measured_clip_offsets(
    subtitle_path: str | Path,
    fingerprint: audio_offset_verifier.MovieAudioFingerprint,
    proposed_offset: float,
) -> dict[int, float]:
    offsets: dict[int, float] = {}
    for index, values in _cue_offsets_by_clip(
        subtitle_path, fingerprint, proposed_offset
    ).items():
        if max(values) - min(values) > REGION_CLUSTER_TOLERANCE_SECONDS:
            continue
        region_offset = statistics.median(values)
        if abs(region_offset) <= MAX_ABSOLUTE_REGION_OFFSET_SECONDS:
            offsets[index] = region_offset
    return offsets


def _region_offsets(
    subtitle_path: str | Path,
    fingerprint: audio_offset_verifier.MovieAudioFingerprint,
    proposed_offset: float,
    region_by_clip: dict[int, int] | None = None,
) -> tuple[float, ...]:
    measured = _measured_clip_offsets(subtitle_path, fingerprint, proposed_offset)
    if region_by_clip is None:
        return tuple(measured[index] for index in fingerprint.clip_indexes if index in measured)
    by_region: dict[int, list[float]] = {}
    for index in fingerprint.clip_indexes:
        if index in measured:
            by_region.setdefault(region_by_clip[index], []).append(measured[index])
    return tuple(statistics.median(values) for values in by_region.values())


def measured_clip_offsets(
    subtitle_path: str | Path,
    fingerprint: audio_offset_verifier.MovieAudioFingerprint,
    proposed_offset: float,
) -> dict[int, float]:
    return _measured_clip_offsets(subtitle_path, fingerprint, float(proposed_offset))


def _largest_consensus_cluster(
    values: tuple[float, ...],
    proposed_offset: float,
) -> tuple[float, ...]:
    """Return the strongest fixed-offset cluster without trusting one outlier.

    Whisper word boundaries and subtitle display lead-in routinely differ by a
    few hundred milliseconds.  Values therefore need to share one narrow band,
    not be bit-for-bit equal.  Selection prefers more regions, then the cluster
    nearest the independently proposed offset, then the tighter cluster.
    """
    ordered = sorted(float(value) for value in values)
    choices: list[tuple[float, ...]] = []
    for left in range(len(ordered)):
        for right in range(left, len(ordered)):
            cluster = tuple(ordered[left:right + 1])
            if cluster[-1] - cluster[0] <= REGION_CLUSTER_TOLERANCE_SECONDS:
                choices.append(cluster)
            else:
                break
    if not choices:
        return ()
    return max(
        choices,
        key=lambda cluster: (
            len(cluster),
            -abs(statistics.median(cluster) - proposed_offset),
            -(cluster[-1] - cluster[0]),
        ),
    )


def measure_region_offsets(
    subtitle_path: str | Path,
    fingerprint: audio_offset_verifier.MovieAudioFingerprint,
    proposed_offset: float,
    region_by_clip: dict[int, int] | None = None,
) -> tuple[float, ...]:
    """Expose exact-word regional measurements for bounded fallback sampling."""
    return _region_offsets(subtitle_path, fingerprint, float(proposed_offset), region_by_clip)


def consensus_region_offsets(
    values: tuple[float, ...],
    proposed_offset: float,
) -> tuple[float, ...]:
    """Expose the same fixed-offset cluster rule used by the final guard."""
    return _largest_consensus_cluster(values, float(proposed_offset))


def evaluate_proposed_offset(
    subtitle_path: str | Path,
    fingerprint: audio_offset_verifier.MovieAudioFingerprint,
    proposed_offset: float,
    *,
    require_word_evidence: bool = False,
    region_by_clip: dict[int, int] | None = None,
) -> OffsetGuardDecision:
    proposed_offset = float(proposed_offset)
    region_offsets = _region_offsets(subtitle_path, fingerprint, proposed_offset, region_by_clip)
    conflicts = conflicted_clip_offsets(subtitle_path, fingerprint, proposed_offset)
    if conflicts:
        index, values = next(iter(conflicts.items()))
        return OffsetGuardDecision(
            False, "TIMING_UNRESOLVED", None, proposed_offset,
            region_offsets, None, None,
            (
                f"同一音频窗口{index}内{len(values)}句对白的逐词偏移跨度"
                f"{max(values) - min(values):.3f}秒，不能取中位数掩盖；"
                "须保留冲突并核查备用点"
            ),
        )
    if region_by_clip is not None:
        raw = _measured_clip_offsets(subtitle_path, fingerprint, proposed_offset)
        within_region: dict[int, list[float]] = {}
        for clip_index, value in raw.items():
            within_region.setdefault(region_by_clip[clip_index], []).append(value)
        if any(
            max(values) - min(values) > REGION_CLUSTER_TOLERANCE_SECONDS
            for values in within_region.values() if len(values) > 1
        ):
            return OffsetGuardDecision(
                False, "TIMING_UNRESOLVED", None, proposed_offset,
                region_offsets, None, None,
                "同一区域主点与备用点的可信偏移冲突，不能取中位数掩盖",
            )
    if (
        len(region_offsets) >= 2
        and all(abs(value) <= ORIGINAL_GOOD_SECONDS for value in region_offsets)
    ):
        baseline_cost = statistics.median(abs(value) for value in region_offsets)
        shifted_cost = statistics.median(abs(value - proposed_offset) for value in region_offsets)
        return OffsetGuardDecision(
            True, "KEEP_ORIGINAL", 0.0, proposed_offset, region_offsets,
            baseline_cost, shifted_cost,
            f"全部{len(region_offsets)}个独立区域原轴残差均不超过"
            f"{ORIGINAL_GOOD_SECONDS:.2f}秒；只允许保留原时间轴",
        )
    if len(region_offsets) < MIN_REGIONS:
        return OffsetGuardDecision(
            False,
            "NEED_MORE",
            None,
            proposed_offset,
            region_offsets,
            None,
            None,
            f"逐词同句证据只有{len(region_offsets)}个独立区域，少于{MIN_REGIONS}个",
        )

    consensus_offsets = _largest_consensus_cluster(region_offsets, proposed_offset)
    if len(consensus_offsets) != len(region_offsets):
        return OffsetGuardDecision(
            False,
            "TIMING_UNRESOLVED",
            None,
            proposed_offset,
            region_offsets,
            None,
            None,
            (
                f"{len(region_offsets)}个逐词区域存在可信时间冲突，"
                "不能丢弃离群证据后应用偏移"
            ),
        )

    center = statistics.median(consensus_offsets)
    baseline_cost = statistics.median(abs(value) for value in consensus_offsets)
    shifted_cost = statistics.median(
        abs(value - proposed_offset) for value in consensus_offsets
    )
    improvement = baseline_cost - shifted_cost
    relative_improvement = (
        improvement / baseline_cost if baseline_cost > 1e-9 else 0.0
    )
    proposal_disagreement = abs(center - proposed_offset)

    if (
        baseline_cost <= ORIGINAL_GOOD_SECONDS
        and (
            shifted_cost >= baseline_cost * 0.75
            or improvement < MIN_ABSOLUTE_IMPROVEMENT
        )
    ):
        return OffsetGuardDecision(
            True,
            "KEEP_ORIGINAL",
            0.0,
            proposed_offset,
            region_offsets,
            baseline_cost,
            shifted_cost,
            (
                f"原时间轴逐词残差中位数{baseline_cost:.3f}秒，"
                f"应用建议后为{shifted_cost:.3f}秒；原时间轴更可靠"
            ),
        )

    if (
        shifted_cost <= APPLIED_GOOD_SECONDS
        and improvement >= MIN_ABSOLUTE_IMPROVEMENT
        and relative_improvement >= MIN_RELATIVE_IMPROVEMENT
        and proposal_disagreement <= MAX_PROPOSAL_DISAGREEMENT
    ):
        return OffsetGuardDecision(
            True,
            "APPLY_OFFSET",
            proposed_offset,
            proposed_offset,
            region_offsets,
            baseline_cost,
            shifted_cost,
            (
                f"逐词残差中位数由{baseline_cost:.3f}秒降至{shifted_cost:.3f}秒，"
                f"{len(consensus_offsets)}/{len(region_offsets)}个远距离区域形成共识，"
                f"支持固定偏移{proposed_offset:+.3f}秒"
            ),
        )

    return OffsetGuardDecision(
        False,
        "REJECTED",
        None,
        proposed_offset,
        region_offsets,
        baseline_cost,
        shifted_cost,
        (
            f"原时间轴/应用后逐词残差为{baseline_cost:.3f}/{shifted_cost:.3f}秒，"
            f"区域中位偏移{center:+.3f}秒与建议相差{proposal_disagreement:.3f}秒；"
            "没有证明该偏移能安全改善时间轴"
        ),
    )


def describe(decision: OffsetGuardDecision) -> str:
    regions = "、".join(f"{value:+.3f}" for value in decision.region_offsets) or "无"
    return f"{decision.status}；区域偏移[{regions}]；{decision.reason}"
