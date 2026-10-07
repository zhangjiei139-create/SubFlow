# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Sequence

import audio_clause_diagnostics
import audio_offset_verifier

MIN_PRODUCTION_SCORE = 0.72
MIN_DISTINCT_LOCATION_MARGIN = 0.08
MIN_MATCHED_CONTENT_WORDS = 3
MIN_CONTENT_RECALL = 0.60
MAX_HIGH_RESIDUAL = 0.30
MAX_HIGH_LOO_SHIFT = 0.15
MIN_HIGH_ANCHORS = 4
MAX_COMPETING_SUPPORT_GAP = 1
MIN_DISTINCT_COMPETING_OFFSET = MAX_HIGH_LOO_SHIFT

_GENERIC_WORDS = {
    "a", "an", "and", "are", "be", "but", "come", "do", "go", "hey", "i", "is",
    "it", "just", "me", "my", "no", "not", "of", "oh", "okay", "on", "so", "that",
    "the", "this", "to", "uh", "um", "we", "what", "yeah", "yes", "you", "your",
}


@dataclass(frozen=True)
class ProductionAnchorDecision:
    anchor: audio_offset_verifier.OffsetAnchor
    accepted: bool
    reasons: tuple[str, ...]
    matched_content_words: int
    observed_content_words: int
    content_recall: float


@dataclass(frozen=True)
class PrecisionShadowResult:
    model_type: str
    precision_shadow: str
    future_action: str
    production_anchors: tuple[audio_offset_verifier.OffsetAnchor, ...]
    production_anchor_offsets: tuple[float, ...]
    stage3_2_offset: float | None
    production_only_offset: float | None
    offset_difference: float | None
    mad: float | None
    max_residual: float | None
    loo_max_shift: float | None
    normalized_positions: tuple[float, ...]
    coverage_span: float
    max_adjacent_gap: float
    region_coverage: tuple[bool, bool, bool]
    reason: str


@dataclass(frozen=True)
class ProductionSafetyDecision:
    eligible: bool
    status: str
    reason: str
    selected_offset: float | None = None
    competing_offset: float | None = None

def _words(value: str) -> list[str]:
    normalized = audio_offset_verifier._clean(value)
    return re.findall(r"[a-z0-9']{2,}", normalized)


def _content_words(value: str) -> list[str]:
    return [word for word in _words(value) if word not in _GENERIC_WORDS]


def _content_evidence(anchor: audio_offset_verifier.OffsetAnchor) -> tuple[int, int, float]:
    observed = _content_words(anchor.observed_text)
    candidate = _content_words(anchor.candidate_text)
    overlap = sum((Counter(observed) & Counter(candidate)).values())
    recall = overlap / len(observed) if observed else 0.0
    return overlap, len(observed), recall


def classify_production_anchor(
    anchor: audio_offset_verifier.OffsetAnchor,
) -> ProductionAnchorDecision:
    reasons: list[str] = []
    matched, observed_count, recall = _content_evidence(anchor)
    if anchor.score < MIN_PRODUCTION_SCORE:
        reasons.append(f"正文匹配 {anchor.score:.0%} 低于 {MIN_PRODUCTION_SCORE:.0%}")
    if anchor.distinct_location_margin < MIN_DISTINCT_LOCATION_MARGIN:
        reasons.append(
            f"不同时间位置匹配优势 {anchor.distinct_location_margin:.0%} "
            f"低于 {MIN_DISTINCT_LOCATION_MARGIN:.0%}"
        )
    if observed_count < MIN_MATCHED_CONTENT_WORDS:
        reasons.append(f"ASR 实义词仅 {observed_count} 个")
    if matched < MIN_MATCHED_CONTENT_WORDS:
        reasons.append(f"匹配实义词仅 {matched} 个")
    if recall < MIN_CONTENT_RECALL:
        reasons.append(f"ASR 实义词覆盖 {recall:.0%} 低于 {MIN_CONTENT_RECALL:.0%}")
    return ProductionAnchorDecision(
        anchor=anchor,
        accepted=not reasons,
        reasons=tuple(reasons),
        matched_content_words=matched,
        observed_content_words=observed_count,
        content_recall=recall,
    )


def _fixed_estimator(offsets: Sequence[float]) -> float:
    return statistics.median(offsets)


def _mad(values: Sequence[float], center: float) -> float:
    return statistics.median(abs(value - center) for value in values)


def _loo_max_shift(offsets: Sequence[float], center: float) -> float | None:
    if len(offsets) < 2:
        return None
    return max(
        abs(_fixed_estimator(offsets[:index] + offsets[index + 1:]) - center)
        for index in range(len(offsets))
    )


def evaluate_shadow(
    result: audio_offset_verifier.AudioOffsetResult,
    *,
    duration_seconds: float,
) -> tuple[PrecisionShadowResult, tuple[ProductionAnchorDecision, ...]]:
    decisions = tuple(classify_production_anchor(anchor) for anchor in result.anchors)
    production = tuple(decision.anchor for decision in decisions if decision.accepted)
    offsets = tuple(anchor.offset_seconds for anchor in production)
    model_type = "NONE" if not result.accepted or result.offset_seconds is None else (
        "AFFINE" if result.affine else "FIXED"
    )

    production_offset = _fixed_estimator(offsets) if offsets else None
    offset_difference = (
        production_offset - result.offset_seconds
        if production_offset is not None and result.offset_seconds is not None
        else None
    )
    mad = _mad(offsets, production_offset) if production_offset is not None else None
    residuals = tuple(abs(value - production_offset) for value in offsets) if production_offset is not None else ()
    maximum_residual = max(residuals) if residuals else None
    loo_shift = _loo_max_shift(offsets, production_offset) if production_offset is not None else None

    normalized = tuple(sorted(
        max(0.0, min(1.0, anchor.movie_time / duration_seconds))
        for anchor in production
    )) if duration_seconds > 0 else ()
    coverage_span = normalized[-1] - normalized[0] if len(normalized) >= 2 else 0.0
    max_gap = max(
        (right - left for left, right in zip(normalized, normalized[1:])),
        default=0.0,
    )
    regions = (
        any(value < 1.0 / 3.0 for value in normalized),
        any(1.0 / 3.0 <= value < 2.0 / 3.0 for value in normalized),
        any(value >= 2.0 / 3.0 for value in normalized),
    )

    if model_type == "NONE":
        precision = "MODEL_REJECT"
        future_action = "FALLBACK_TO_LEGACY"
        reason = "Stage 3.2 未建立可信时间模型"
    elif model_type == "AFFINE":
        precision = "BORDERLINE"
        future_action = "FALLBACK_TO_LEGACY"
        reason = "当前阶段 affine 只允许诊断，不具备成品资格"
    else:
        failures: list[str] = []
        if len(production) < MIN_HIGH_ANCHORS:
            failures.append(f"production anchors {len(production)} < {MIN_HIGH_ANCHORS}")
        if maximum_residual is None or maximum_residual > MAX_HIGH_RESIDUAL:
            residual_text = "无" if maximum_residual is None else f"{maximum_residual:.3f}s"
            failures.append(f"max residual {residual_text} > {MAX_HIGH_RESIDUAL:.2f}s")
        if loo_shift is None or loo_shift > MAX_HIGH_LOO_SHIFT:
            loo_text = "无" if loo_shift is None else f"{loo_shift:.3f}s"
            failures.append(f"LOO max shift {loo_text} > {MAX_HIGH_LOO_SHIFT:.2f}s")
        precision = "BORDERLINE" if failures else "HIGH"
        future_action = "FALLBACK_TO_LEGACY" if failures else "STAGE3_2_ELIGIBLE"
        reason = "；".join(failures) if failures else "满足第一阶段 Shadow 成品精度实验条件"

    return PrecisionShadowResult(
        model_type=model_type,
        precision_shadow=precision,
        future_action=future_action,
        production_anchors=production,
        production_anchor_offsets=offsets,
        stage3_2_offset=result.offset_seconds,
        production_only_offset=production_offset,
        offset_difference=offset_difference,
        mad=mad,
        max_residual=maximum_residual,
        loo_max_shift=loo_shift,
        normalized_positions=normalized,
        coverage_span=coverage_span,
        max_adjacent_gap=max_gap,
        region_coverage=regions,
        reason=reason,
    ), decisions


def evaluate_production_safety(
    result: audio_offset_verifier.AudioOffsetResult,
) -> ProductionSafetyDecision:
    if not result.accepted or result.offset_seconds is None:
        return ProductionSafetyDecision(
            False,
            "MODEL_REJECT",
            "Stage 3.2 未建立可信时间模型",
        )
    if result.affine:
        return ProductionSafetyDecision(
            False,
            "BORDERLINE",
            "affine 模型当前不具备直接写入成品资格，回退旧验证链",
            selected_offset=result.offset_seconds,
        )
    if abs(result.offset_seconds) > 10.0:
        return ProductionSafetyDecision(
            False, "BORDERLINE", "固定偏移超过自动处理范围 ±10 秒",
            selected_offset=result.offset_seconds,
        )
    # The audit compares competing clusters; it does not prove the selected
    # cluster has enough independent, well-aligned dialogue evidence itself.
    if len({anchor.clip_index for anchor in result.anchors}) < 3:
        return ProductionSafetyDecision(
            False, "BORDERLINE", "少于三处独立音频锚点",
            selected_offset=result.offset_seconds,
        )
    anchor_offsets = [anchor.offset_seconds for anchor in result.anchors]
    if max(abs(value - result.offset_seconds) for value in anchor_offsets) > 0.85:
        return ProductionSafetyDecision(
            False, "BORDERLINE", "音频锚点残差超过 0.85 秒",
            selected_offset=result.offset_seconds,
        )
    if sum(classify_production_anchor(anchor).accepted for anchor in result.anchors) < 2:
        return ProductionSafetyDecision(
            False, "BORDERLINE", "少于两处高质量逐词内容锚点",
            selected_offset=result.offset_seconds,
        )

    audit = result.consensus_shadow
    if audit is None or audit.error:
        detail = "缺少共识审计" if audit is None else f"共识审计失败：{audit.error}"
        return ProductionSafetyDecision(
            False,
            "BORDERLINE",
            f"{detail}，无法证明 Stage 3.2 模型达到成品安全条件",
            selected_offset=result.offset_seconds,
        )

    fixed_scale = next(
        (scale for scale in audit.scales if scale.scale_label == "1.000000"),
        None,
    )
    selected = next(
        (
            cluster
            for scale in audit.scales
            for cluster in scale.clusters
            if cluster.selected_by_stage3_2
        ),
        None,
    )
    if selected is None or selected.scale_label != "1.000000" or fixed_scale is None:
        return ProductionSafetyDecision(
            False,
            "BORDERLINE",
            "共识审计未能定位正式 fixed 模型，回退旧验证链",
            selected_offset=result.offset_seconds,
        )

    epsilon = 1e-9
    fragile_competitors = []
    for cluster in fixed_scale.clusters:
        if cluster.selected_by_stage3_2:
            continue
        support_gap = len(selected.probe_ids) - len(cluster.probe_ids)
        if support_gap < 0 or support_gap > MAX_COMPETING_SUPPORT_GAP:
            continue
        if abs(cluster.offset_seconds - selected.offset_seconds) <= MIN_DISTINCT_COMPETING_OFFSET:
            continue
        no_worse_median = cluster.median_residual <= selected.median_residual + epsilon
        no_worse_maximum = cluster.max_residual <= selected.max_residual + epsilon
        strictly_tighter = (
            cluster.median_residual < selected.median_residual - epsilon
            or cluster.max_residual < selected.max_residual - epsilon
        )
        if no_worse_median and no_worse_maximum and strictly_tighter:
            fragile_competitors.append(cluster)

    if fragile_competitors:
        competitor = min(
            fragile_competitors,
            key=lambda cluster: (
                -len(cluster.probe_ids),
                cluster.median_residual,
                cluster.max_residual,
            ),
        )
        return ProductionSafetyDecision(
            False,
            "BORDERLINE",
            (
                "当前 fixed 模型仅靠很小的探针数优势胜出，但存在不同偏移且残差更紧的"
                f"可接受集群：支持数 {len(selected.probe_ids)} 对 {len(competitor.probe_ids)}，"
                f"offset {selected.offset_seconds:+.3f}s 对 {competitor.offset_seconds:+.3f}s，"
                f"median/max residual {selected.median_residual:.3f}/{selected.max_residual:.3f}s "
                f"对 {competitor.median_residual:.3f}/{competitor.max_residual:.3f}s"
            ),
            selected_offset=selected.offset_seconds,
            competing_offset=competitor.offset_seconds,
        )

    return ProductionSafetyDecision(
        True,
        "HIGH",
        "未发现能够以近似探针支持数和更紧残差挑战正式 fixed 模型的不同偏移集群",
        selected_offset=selected.offset_seconds,
    )

def _log_consensus_shadow(
    result: audio_offset_verifier.AudioOffsetResult,
    *,
    log: Callable[[str], None],
) -> None:
    audit = result.consensus_shadow
    if audit is None:
        return
    log(
        f"Existing Consensus Audit Shadow 开始："
        f"diagnostic_version={audit.diagnostic_version}；"
        f"耗时={audit.elapsed_ms:.2f}ms。"
    )
    if audit.error:
        log(
            f"Existing Consensus Audit Shadow 未完成：{audit.error}。"
            "正式 Stage 3.2 与最终字幕不受影响。"
        )
        return
    for scale in audit.scales:
        if not scale.clusters:
            log(
                f"Consensus scale {scale.scale_label}：无可比较集群；"
                f"{scale.best_vs_second_reason}。"
            )
            continue
        log(
            f"Consensus scale {scale.scale_label}："
            f"保留 {len(scale.clusters)} 个集群；{scale.best_vs_second_reason}。"
        )
        for cluster in scale.clusters:
            probes = ",".join(str(probe + 1) for probe in cluster.probe_ids)
            selected = "；STAGE3_2_SELECTED" if cluster.selected_by_stage3_2 else ""
            accepted = "ACCEPTABLE" if cluster.accepted_by_stage3_2 else "NOT_ACCEPTABLE"
            log(
                f"  Cluster {cluster.rank}：offset={cluster.offset_seconds:+.3f}s；"
                f"distinct_probes={len(cluster.probe_ids)} [{probes}]；"
                f"median/max residual={cluster.median_residual:.3f}/{cluster.max_residual:.3f}s；"
                f"span={cluster.span_seconds:.1f}s；average_text={cluster.average_score:.1%}；"
                f"{accepted}{selected}。"
            )
            selections = " | ".join(
                f"P{anchor.clip_index + 1}@{anchor.candidate_start:.3f}-{anchor.candidate_end:.3f}"
                f" offset={anchor.offset_seconds:+.3f}s residual={anchor.residual:.3f}s"
                f" text={anchor.score:.1%}"
                for anchor in cluster.anchors
            )
            log(f"    selected_windows：{selections}")
    log(
        f"Existing Consensus Audit Shadow 最终选择：{audit.final_selection_reason}。"
        "当前仅审计现有共识，不修改 Stage 3.2、Precision Gate 或最终字幕。"
    )

def log_shadow(
    result: audio_offset_verifier.AudioOffsetResult,
    *,
    duration_seconds: float,
    log: Callable[[str], None] = print,
) -> PrecisionShadowResult:
    shadow, decisions = evaluate_shadow(result, duration_seconds=duration_seconds)
    log("Production Precision Gate Shadow 开始：候选身份已由前置流程判定为 VALID。")
    _log_consensus_shadow(result, log=log)
    local_timing_diagnostics = []
    local_timing_elapsed_ms = 0.0
    geometry_diagnostics = []
    geometry_elapsed_ms = 0.0
    for decision in decisions:
        anchor = decision.anchor
        status = "接受" if decision.accepted else "拒绝"
        detail = "通过独立内容资格" if decision.accepted else "；".join(decision.reasons)
        log(
            f"Production anchor 探针 {anchor.clip_index + 1}：{status}；"
            f"正文匹配 {anchor.score:.0%}，不同位置优势 {anchor.distinct_location_margin:.0%}，"
            f"实义词匹配 {decision.matched_content_words}/{decision.observed_content_words}，"
            f"覆盖 {decision.content_recall:.0%}；{detail}。"
        )
        clause = anchor.clause_diagnostic
        if clause is not None:
            log(
                f"CLAUSE Shadow 探针 {anchor.clip_index + 1}：{clause.status}；"
                f"去重前/后 {clause.matches_before_dedup}/{clause.locations_after_dedup}；"
                f"matched_content_words={clause.matched_content_words}；"
                f"ASR覆盖={clause.asr_content_coverage:.0%}；"
                f"字幕覆盖={clause.subtitle_content_coverage:.0%}；"
                f"longest_ordered_run={clause.longest_ordered_run}；"
                f"longest_contiguous_run={clause.longest_contiguous_run}；"
                f"internal_gaps={clause.internal_gap_count}；"
                f"clause_uniqueness_margin={clause.clause_uniqueness_margin:.0%}；"
                f"match_type={clause.match_type}；{clause.reason}。"
            )
        local_timing_started = time.perf_counter()
        local_timing = audio_clause_diagnostics.build_local_timing_diagnostic(
            clause,
            full_match=decision.accepted,
            raw_offset_seconds=anchor.offset_seconds,
        )
        local_timing_elapsed_ms += (time.perf_counter() - local_timing_started) * 1000.0
        local_timing_diagnostics.append(local_timing)
        cue_ids = ",".join(str(number) for number in local_timing.matched_cue_ids) or "无"
        log(
            f"Local Timing V1 探针 {anchor.clip_index + 1}："
            f"diagnostic_version={local_timing.diagnostic_version}；"
            f"content_type={local_timing.content_match_type}；"
            f"classification={local_timing.classification}；"
            f"matched_cue_ids=[{cue_ids}]；cross_cue={local_timing.match_crosses_cue_boundary}；"
            f"ASR tokens={local_timing.asr_matched_token_start}:{local_timing.asr_matched_token_end}，"
            f"unmatched={local_timing.asr_unmatched_prefix}/{local_timing.asr_unmatched_suffix}；"
            f"字幕 tokens={local_timing.subtitle_matched_token_start}:{local_timing.subtitle_matched_token_end}，"
            f"window={local_timing.composite_window_token_start}:{local_timing.composite_window_token_end}，"
            f"unmatched={local_timing.subtitle_unmatched_prefix}/{local_timing.subtitle_unmatched_suffix}；"
            f"raw_offset={local_timing.raw_offset_seconds:+.3f}s；{local_timing.reason}。"
        )
        geometry_started = time.perf_counter()
        geometry = audio_clause_diagnostics.build_composite_window_geometry(
            clause,
            observation_start=anchor.observation_start,
            observation_end=anchor.observation_end,
            observation_segment_count=anchor.observation_segment_count,
            raw_offset_seconds=anchor.offset_seconds,
        )
        geometry_elapsed_ms += (time.perf_counter() - geometry_started) * 1000.0
        geometry_diagnostics.append(geometry)
        end_proxy = (
            "UNAVAILABLE" if geometry.observation_end_proxy_offset is None
            else f"{geometry.observation_end_proxy_offset:+.3f}s"
        )
        center_proxy = (
            "UNAVAILABLE" if geometry.selected_matched_center_proxy_offset is None
            else f"{geometry.selected_matched_center_proxy_offset:+.3f}s"
        )
        window_span = (
            "UNAVAILABLE" if geometry.window_offset_span is None
            else f"{geometry.window_offset_span:.3f}s"
        )
        log(
            f"Composite Window Geometry 探针 {anchor.clip_index + 1}："
            f"diagnostic_version={geometry.diagnostic_version}；"
            f"observation segments={geometry.observation_segment_count}；"
            f"observation duration={geometry.observation_duration:.3f}s；"
            f"boundary={geometry.selected_boundary_type}；"
            f"midpoint={geometry.anchor_midpoint_offset:+.3f}s；"
            f"observation_end_proxy={end_proxy}；matched_center_proxy={center_proxy}；"
            f"window_offset_span={window_span}。"
        )
        for member_index, member in enumerate(geometry.members, start=1):
            matched_distribution = ",".join(
                f"{cue_id}:{count}" for cue_id, count in member.matched_words_by_cue
            ) or "无"
            gaps = ",".join(f"{value:.3f}" for value in member.cue_gaps) or "无"
            normalized = (
                "UNAVAILABLE" if member.matched_position_normalized is None
                else f"{member.matched_position_normalized:.3f}"
            )
            member_center = (
                "UNAVAILABLE" if member.matched_center_proxy_offset is None
                else f"{member.matched_center_proxy_offset:+.3f}s"
            )
            log(
                f"  Window {member_index}：cue_count={member.cue_count}；"
                f"duration={member.end - member.start:.3f}s；gaps=[{gaps}]；"
                f"matched_by_cue=[{matched_distribution}]；"
                f"matched_position={normalized}；midpoint={member.midpoint_offset:+.3f}s；"
                f"matched_center_proxy={member_center}；"
                f"full_start/end={member.full_start_aligned}/{member.full_end_aligned}。"
            )
    for classification, metrics in sorted(
        audio_clause_diagnostics.summarize_local_timing(local_timing_diagnostics).items()
    ):
        log(
            f"Local Timing V1 分布 {classification}：count={metrics['count']}；"
            f"median={metrics['median']:+.3f}s；MAD={metrics['mad']:.3f}s；"
            f"min/max={metrics['minimum']:+.3f}/{metrics['maximum']:+.3f}s；"
            f"span={metrics['span']:.3f}s。"
        )
    log(
        f"Local Timing V1 诊断耗时：{local_timing_elapsed_ms:.2f} ms。"
        "当前为 Shadow，仅描述文本/cue 结构，不修改 production anchor、Precision Gate 或最终字幕。"
    )
    comparisons = audio_clause_diagnostics.summarize_geometry_comparisons(
        geometry_diagnostics
    )
    for name, comparison in comparisons.items():
        midpoint = comparison["midpoint"]
        proxy = comparison["proxy"]
        log(
            f"Geometry 公平对照 {name}：anchor_total={comparison['anchor_total']}；"
            f"anchor_comparable={comparison['anchor_comparable']}；"
            f"midpoint MAD/span={midpoint['mad']}/{midpoint['span']}；"
            f"proxy MAD/span={proxy['mad']}/{proxy['span']}。"
        )
    log(
        f"Composite Window Geometry 诊断耗时：{geometry_elapsed_ms:.2f} ms。"
        "当前为 Shadow，不修改 Stage 3.2、production anchors、Precision Gate 或最终字幕。"
    )
    offsets = ", ".join(f"{number:+.3f}s" for number in shadow.production_anchor_offsets) or "无"
    positions = ", ".join(f"{number:.3f}" for number in shadow.normalized_positions) or "无"
    regions = "/".join("有" if covered else "无" for covered in shadow.region_coverage)

    def formatted(number: float | None, suffix: str = "s") -> str:
        return "无" if number is None or not math.isfinite(number) else f"{number:+.3f}{suffix}"

    log(
        f"Precision Shadow：model_type={shadow.model_type}，"
        f"precision_shadow={shadow.precision_shadow}，future_action={shadow.future_action}。"
    )
    log(
        f"Precision Shadow 指标：production_anchor_offsets=[{offsets}]；"
        f"stage3_2_offset={formatted(shadow.stage3_2_offset)}；"
        f"production_only_offset={formatted(shadow.production_only_offset)}；"
        f"offset_difference={formatted(shadow.offset_difference)}；"
        f"MAD={formatted(shadow.mad)}；max_residual={formatted(shadow.max_residual)}；"
        f"LOO_max_shift={formatted(shadow.loo_max_shift)}。"
    )
    log(
        f"Precision Shadow 覆盖：normalized_positions=[{positions}]；"
        f"coverage_span={shadow.coverage_span:.3f}；max_adjacent_gap={shadow.max_adjacent_gap:.3f}；"
        f"early/middle/late={regions}。"
    )
    log(f"Precision Shadow 结论：{shadow.reason}。当前为 Shadow，不修改最终字幕或候选状态。")
    return shadow
