"""Estimate a sampled fixed PGS-to-text timing relation without reading media.

The caller supplies three independently read, padded PGS regions in movie
coordinates and an already audio-aligned text reference. A supported result
concerns the main sampled timing pattern, not dialogue identity or every cue.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Iterable, Sequence

Interval = tuple[float, float]
Window = tuple[float, float]
CORE_MARGIN_SECONDS = 12.0
READ_PADDING_SECONDS = 15.0
MAX_OFFSET_SECONDS = 10.0
OFFSET_BOUNDARY_SECONDS = 9.9
MAX_WINDOW_SECONDS = 480.0
FRAME_RATE_HZ = 100
BOUNDED_SUBWINDOW_LENGTHS = (120.0, 180.0)
BOUNDED_CENTER_DELTAS = (-150.0, -75.0, 0.0, 75.0, 150.0)
MAX_BOUNDED_FINAL_CHECKS = 10


@dataclass(frozen=True, slots=True)
class StartEvidence:
    best_offset_seconds: float
    raw_peak_score: float
    alternative_peak_ratio: float | None
    normalized_support_at_proposal: float
    residual_seconds: float
    distinct_candidate_starts: int
    distinct_reference_starts: int


@dataclass(frozen=True, slots=True)
class ActivityEvidence:
    core: Window
    score: float
    reference_cues: int
    candidate_cues: int
    reference_active_seconds: float
    candidate_active_seconds: float


@dataclass(frozen=True, slots=True)
class AnchorEvidence:
    sufficient: bool
    distinct_candidate_cues: int
    distinct_reference_cues: int
    mutually_nearest_count: int
    matched_minimum_count_ratio: float
    candidate_coverage: float
    reference_coverage: float
    covered_quarters: tuple[int, ...]
    median_start_error_seconds: float | None
    median_end_error_seconds: float | None
    median_duration_difference_seconds: float | None


@dataclass(frozen=True, slots=True)
class RegionEvidence:
    window: Window
    starts: StartEvidence
    activity: ActivityEvidence
    anchors: AnchorEvidence
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalAlignmentResult:
    accepted: bool
    offset_seconds: float | None
    proposed_offset_seconds: float | None
    reason: str
    evidence: tuple[RegionEvidence, ...] = ()
    minimum_start_support: float | None = None
    sigma_seconds: float = .3


@dataclass(frozen=True, slots=True)
class BoundedSearchResult:
    alignment: LocalAlignmentResult
    examined_windows: tuple[int, ...]
    eligible_windows: tuple[int, ...]
    common_offset_count: int
    final_checks: int


class _InvalidData(ValueError):
    pass


def _intervals(values: Iterable[Interval]) -> tuple[Interval, ...]:
    """Exact duplicates do not contribute extra cues or timing evidence."""
    if isinstance(values, (str, bytes, dict)):
        raise _InvalidData("invalid_intervals")
    result = set()
    try:
        for value in values:
            if isinstance(value, (str, bytes, dict)):
                raise _InvalidData("invalid_interval")
            a, b = value
            if isinstance(a, bool) or isinstance(b, bool):
                raise _InvalidData("invalid_timestamp")
            a, b = float(a), float(b)
            if not math.isfinite(a) or not math.isfinite(b) or a < 0 or b <= a:
                raise _InvalidData("invalid_timestamp")
            result.add((a, b))
    except (TypeError, ValueError, OverflowError) as error:
        if isinstance(error, _InvalidData):
            raise
        raise _InvalidData("invalid_intervals") from error
    return tuple(sorted(result))


def _windows(values: Sequence[Window]) -> tuple[Window, ...]:
    try:
        if len(values) != 3:
            raise _InvalidData("three_windows_required")
        # Do not deduplicate windows: duplicated/overlapping regions cannot
        # claim to supply three independent observations.
        result = []
        for value in values:
            a, b = value
            if isinstance(a, bool) or isinstance(b, bool):
                raise _InvalidData("invalid_window")
            a, b = float(a), float(b)
            if (not math.isfinite(a) or not math.isfinite(b) or a < 0
                    or b - a <= 2 * CORE_MARGIN_SECONDS
                    or b - a > MAX_WINDOW_SECONDS + 1e-9):
                raise _InvalidData("invalid_window")
            if result and a < result[-1][1]:
                raise _InvalidData("windows_overlap_or_are_unordered")
            result.append((a, b))
    except (TypeError, ValueError, OverflowError) as error:
        if isinstance(error, _InvalidData):
            raise
        raise _InvalidData("invalid_window") from error
    return tuple(result)


def _nearest_distance(np, points, reference):
    positions = np.searchsorted(reference, points)
    before = reference[np.clip(positions - 1, 0, len(reference) - 1)]
    after = reference[np.clip(positions, 0, len(reference) - 1)]
    return np.minimum(np.abs(points - before), np.abs(points - after))


def _start_curve(np, reference, candidate, window, offsets, sigma):
    lo, hi = window
    # A start is a boundary, not a cue-duration weight. Concurrent starts
    # therefore appear only once even when differently sized cues coincide.
    image = np.asarray(sorted({a for a, _ in candidate
                               if lo + CORE_MARGIN_SECONDS < a < hi - CORE_MARGIN_SECONDS}))
    text = np.asarray(sorted({a for a, _ in reference
                              if lo - CORE_MARGIN_SECONDS < a < hi + CORE_MARGIN_SECONDS}))
    if len(image) < 3 or len(text) < 3:
        raise _InvalidData("too_few_distinct_starts")
    distances = _nearest_distance(np, image[None, :] + offsets[:, None], text)
    values = np.mean(np.exp(-.5 * (distances / sigma) ** 2), axis=1)
    best = int(np.argmax(values))
    baseline = float(np.median(values))
    contrast = float(values[best]) - baseline
    normalized = (values - baseline) / max(contrast, 1e-9)
    remote = np.abs(offsets - offsets[best]) >= 1.0 - 1e-9
    ratio = ((float(np.max(values[remote])) - baseline) / contrast
             if contrast > 1e-9 and remote.any() else None)
    return normalized, (float(offsets[best]), float(values[best]), ratio, len(image), len(text))


def _activity(np, reference, candidate, window, offset) -> ActivityEvidence:
    lo, hi = window[0] + CORE_MARGIN_SECONDS, window[1] - CORE_MARGIN_SECONDS
    length = int(round((hi - lo) * FRAME_RATE_HZ))
    signals = [np.zeros(length, dtype=bool), np.zeros(length, dtype=bool)]
    counts = []
    for dest, items, shift in ((signals[0], reference, 0.), (signals[1], candidate, offset)):
        count = 0
        for a, b in items:
            a, b = a + shift, b + shift
            if b <= lo or a >= hi:
                continue
            first = max(0, int(round((a - lo) * FRAME_RATE_HZ)))
            last = min(length, int(round((b - lo) * FRAME_RATE_HZ)))
            if last > first:
                dest[first:last] = True
                count += 1
        counts.append(count)
    reference_frames, image_frames = (int(signal.sum()) for signal in signals)
    overlap = int(np.count_nonzero(signals[0] & signals[1]))
    return ActivityEvidence((lo, hi), overlap / max(1, min(reference_frames, image_frames)),
                            counts[0], counts[1], reference_frames / FRAME_RATE_HZ,
                            image_frames / FRAME_RATE_HZ)


def _anchors(np, reference, candidate, window, offset) -> AnchorEvidence:
    lo, hi = window[0] + CORE_MARGIN_SECONDS, window[1] - CORE_MARGIN_SECONDS
    image = np.asarray(sorted({(a + offset, b + offset) for a, b in candidate if lo < a + offset < hi}))
    text = np.asarray(sorted({(a, b) for a, b in reference if lo < a < hi}))
    ni, nt = len(image), len(text)
    if not ni or not nt:
        return AnchorEvidence(False, ni, nt, 0, 0., 0., 0., (), None, None, None)
    distance = np.abs(image[:, 0, None] - text[None, :, 0])
    forward, reverse = distance.argmin(axis=1), distance.argmin(axis=0)
    pairs = [(i, int(j)) for i, j in enumerate(forward)
             if reverse[j] == i and distance[i, j] <= .5]
    if not pairs:
        return AnchorEvidence(False, ni, nt, 0, 0., 0., 0., (), None, None, None)
    errors = np.asarray([image[i] - text[j] for i, j in pairs])
    covered = tuple(sorted({min(3, int((image[i, 0] - lo) / (hi - lo) * 4)) for i, _ in pairs}))
    ratio = len(pairs) / min(ni, nt)
    duration_delta = float(np.median(errors[:, 1] - errors[:, 0]))
    sufficient = (len(pairs) >= 15 and ratio >= .75 and len(covered) >= 3
                  and abs(duration_delta) <= 1.)
    return AnchorEvidence(bool(sufficient), ni, nt, len(pairs), ratio,
                          len(pairs) / ni, len(pairs) / nt, covered,
                          float(np.median(errors[:, 0])), float(np.median(errors[:, 1])), duration_delta)


def _region_failure_reasons(starts, activity, anchors) -> tuple[str, ...]:
    reasons = []
    # A shared proposal just inside the boundary must not turn a truncated
    # regional peak into an apparently safe fixed correction.
    if abs(starts.best_offset_seconds) >= OFFSET_BOUNDARY_SECONDS - 1e-9:
        reasons.append("start_peak_at_search_boundary")
    if starts.alternative_peak_ratio is None or starts.alternative_peak_ratio >= .8:
        reasons.append("alternative_start_peak")
    if abs(starts.residual_seconds) > .25 + 1e-9:
        reasons.append("start_residual_inconsistent")
    if min(activity.reference_cues, activity.candidate_cues) < 20:
        reasons.append("activity_cues_insufficient")
    if activity.score < .85:
        reasons.append("activity_overlap_insufficient")
    if not anchors.sufficient:
        reasons.append("mutual_anchors_insufficient")
    return tuple(reasons)


def evaluate_local_alignment(
    reference_intervals: Iterable[Interval],
    windows: Sequence[Window],
    candidate_regions: Sequence[Iterable[Interval]],
    *,
    sigma_seconds: float = .3,
) -> LocalAlignmentResult:
    """Return a supported common fixed correction, or a reason to continue.

    Regions stay separate even if the caller's padded reads contain overlapping
    packets. A small supported correction (<=0.25 seconds) deliberately becomes
    zero, preserving the existing no-move policy. Failure never supplies an
    offset to apply. No raw Gaussian-height threshold participates in approval.
    """
    try:
        if isinstance(sigma_seconds, bool):
            raise _InvalidData("invalid_sigma")
        sigma_seconds = float(sigma_seconds)
        if not math.isfinite(sigma_seconds) or sigma_seconds <= 0:
            raise _InvalidData("invalid_sigma")
        regions = _windows(windows)
        if len(candidate_regions) != 3:
            raise _InvalidData("three_candidate_regions_required")
        reference = _intervals(reference_intervals)
        if not reference:
            raise _InvalidData("empty_reference")
        candidates = []
        for window, values in zip(regions, candidate_regions):
            items = _intervals(values)
            # Preserve real padding for shifting and symmetric clipping, but
            # never borrow cues from the other two regions for confidence.
            items = tuple((a, b) for a, b in items
                          if b > window[0] - READ_PADDING_SECONDS
                          and a < window[1] + READ_PADDING_SECONDS)
            if not items:
                raise _InvalidData("empty_candidate_region")
            candidates.append(items)
    except (TypeError, ValueError, OverflowError) as error:
        reason = str(error) if isinstance(error, _InvalidData) else "invalid_inputs"
        return LocalAlignmentResult(False, None, None, reason)
    try:
        import numpy as np
    except ImportError:
        return LocalAlignmentResult(False, None, None, "numpy_unavailable", sigma_seconds=sigma_seconds)
    curves, metadata = [], []
    try:
        for window, items in zip(regions, candidates):
            values, meta = _start_curve(np, reference, items, window,
                                        np.linspace(-MAX_OFFSET_SECONDS, MAX_OFFSET_SECONDS, 1001), sigma_seconds)
            curves.append(values)
            metadata.append(meta)
    except _InvalidData as error:
        return LocalAlignmentResult(False, None, None, str(error), sigma_seconds=sigma_seconds)
    worst = np.min(np.asarray(curves), axis=0)
    index = int(np.argmax(worst))
    offsets = np.linspace(-MAX_OFFSET_SECONDS, MAX_OFFSET_SECONDS, 1001)
    proposed = float(offsets[index])
    support = float(worst[index])
    global_reasons = []
    if abs(proposed) >= OFFSET_BOUNDARY_SECONDS - 1e-9:
        global_reasons.append("offset_at_search_boundary")
    if support < .65:
        global_reasons.append("shared_start_support_insufficient")
    evidence = []
    for window, items, values, meta in zip(regions, candidates, curves, metadata):
        best, score, ratio, ni, nt = meta
        starts = StartEvidence(best, score, ratio, float(values[index]), best - proposed, ni, nt)
        activity = _activity(np, reference, items, window, proposed)
        anchors = _anchors(np, reference, items, window, proposed)
        evidence.append(RegionEvidence(window, starts, activity, anchors,
                                       _region_failure_reasons(starts, activity, anchors)))
    failures = global_reasons + [reason for region in evidence for reason in region.reasons]
    if failures:
        return LocalAlignmentResult(False, None, proposed, failures[0], tuple(evidence), support, sigma_seconds)
    applied = 0. if abs(proposed) <= .25 + 1e-9 else proposed
    reason = "supported_no_move" if applied == 0. else "supported_fixed_shift"
    return LocalAlignmentResult(True, applied, proposed, reason, tuple(evidence), support, sigma_seconds)


def bounded_subwindows(window: Window) -> tuple[Window, ...]:
    """A fixed, deduplicated grid inside already read coverage: at most 10."""
    low, high = window
    center = (low + high) / 2
    result = []
    for length in BOUNDED_SUBWINDOW_LENGTHS:
        if length > high - low + 1e-9:
            continue
        starts = sorted({min(high - length, max(low, center + delta - length / 2))
                         for delta in BOUNDED_CENTER_DELTAS})
        result.extend((start, start + length) for start in starts)
    return tuple(result)


def evaluate_bounded_alignment(
    reference_intervals: Iterable[Interval],
    read_windows: Sequence[Window],
    candidate_regions: Sequence[Iterable[Interval]],
    *,
    check_cancel: Callable[[], None] | None = None,
) -> BoundedSearchResult:
    """Recheck fixed subwindows without media IO or weaker approval criteria.

    This is only a fallback after the ordinary 2/4/8-minute checks. Each movie
    region supplies its own padded intervals. The fixed grid prevents a search
    for arbitrary convenient excerpts, and final approval always comes from
    the same three-region verifier used by the ordinary path.
    """
    check = check_cancel or (lambda: None)
    check()
    rejected = LocalAlignmentResult(False, None, None, "bounded_windows_insufficient")
    try:
        windows = _windows(read_windows)
        if len(candidate_regions) != 3:
            raise _InvalidData("three_candidate_regions_required")
        reference = _intervals(reference_intervals)
        candidates = tuple(_intervals(region) for region in candidate_regions)
        if not reference:
            raise _InvalidData("empty_reference")
    except (TypeError, ValueError, OverflowError) as error:
        reason = str(error) if isinstance(error, _InvalidData) else "invalid_inputs"
        return BoundedSearchResult(LocalAlignmentResult(False, None, None, reason), (), (), 0, 0)
    try:
        import numpy as np
    except ImportError:
        return BoundedSearchResult(LocalAlignmentResult(False, None, None, "numpy_unavailable"), (), (), 0, 0)
    offsets = np.linspace(-MAX_OFFSET_SECONDS, MAX_OFFSET_SECONDS, 1001)
    rows, examined = [], []
    for coverage, region in zip(windows, candidates):
        grid = bounded_subwindows(coverage)
        examined.append(len(grid))
        eligible_windows = []
        for window in grid:
            check()
            low, high = window
            items = tuple((a, b) for a, b in region
                          if b > low - READ_PADDING_SECONDS and a < high + READ_PADDING_SECONDS)
            try:
                curve, meta = _start_curve(np, reference, items, window, offsets, .3)
            except _InvalidData:
                continue
            best, score, ratio, ni, nt = meta
            if abs(best) >= OFFSET_BOUNDARY_SECONDS - 1e-9 or ratio is None or ratio >= .8:
                continue
            indices = np.flatnonzero((curve >= .65) & (np.abs(offsets - best) <= .25 + 1e-9)
                                     & (np.abs(offsets) < OFFSET_BOUNDARY_SECONDS))
            valid = set()
            for index in indices:
                check()
                proposal = float(offsets[index])
                starts = StartEvidence(best, score, ratio, float(curve[index]), best - proposal, ni, nt)
                activity = _activity(np, reference, items, window, proposal)
                anchors = _anchors(np, reference, items, window, proposal)
                if not _region_failure_reasons(starts, activity, anchors):
                    valid.add(int(index))
            if valid:
                eligible_windows.append((window, items, curve, valid))
        rows.append(eligible_windows)
    eligible_counts = tuple(len(row) for row in rows)
    common = (set.intersection(*[set().union(*(item[3] for item in row)) for row in rows])
              if all(rows) else set())
    checked_combinations = set()
    final_checks = 0
    for index in sorted(common, key=lambda value: abs(offsets[value])):
        check()
        selected = [max((item for item in row if index in item[3]), key=lambda item: item[2][index])
                    for row in rows]
        selected_windows = tuple(item[0] for item in selected)
        if selected_windows in checked_combinations:
            continue
        if final_checks >= MAX_BOUNDED_FINAL_CHECKS:
            break
        checked_combinations.add(selected_windows)
        final_checks += 1
        result = evaluate_local_alignment(reference, selected_windows, [item[1] for item in selected])
        check()
        if result.accepted:
            return BoundedSearchResult(result, tuple(examined), eligible_counts, len(common), final_checks)
    return BoundedSearchResult(rejected, tuple(examined), eligible_counts, len(common), final_checks)
