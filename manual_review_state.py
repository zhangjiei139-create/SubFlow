"""Local provenance and trust boundaries for explicit manual subtitle review.

No provider settings, tokens, or candidate metadata are stored here. A review
always starts from the same content-addressed source, never its shifted output.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from subtitle_identity_guard import media_signature

ORIGIN_FIELDS = {"raw_path", "baseline_path", "raw_sha256", "language", "movie_path", "media_signature"}


def sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hash_matches(path, expected) -> bool:
    try:
        return bool(expected) and sha256(path) == expected
    except OSError:
        return False


def clear_trust(plan, *, required: bool = True) -> None:
    plan.audio_verified_source = {}
    plan.external_subtitle_verified = False
    plan.external_subtitle_verification = ""
    plan.external_subtitle_media_signature = {}
    plan.external_subtitle_content_hash = ""
    plan.manual_confirmation_hash = ""
    for name in ("provider", "release", "identity_key", "seal"):
        setattr(plan, "external_subtitle_" + name, "")
    plan.manual_review_required = required


def subtitle_editable(window, plan) -> bool:
    return bool(plan is not None
                and not getattr(window, "batch_running", False)
                and not any(getattr(item, "status", "") == "processing"
                            for item in getattr(window, "batch_plans", {}).values())
                and getattr(plan, "status", "") not in {"burned", "processing"}
                and not getattr(plan, "burned_subtitle", False)
                and getattr(plan, "task_state", "") != "processing")


def review_action_states(window, plan) -> dict[str, bool]:
    """Separate a saved unfinished review from voluntary rechecking.

    Availability is cheap enough for the list refresh; content/film identity
    is still checked when opening the review, before any cached audio is used.
    """
    available = subtitle_editable(window, plan)
    source = getattr(plan, "external_subtitle", "") if plan is not None else ""
    try:
        available = bool(available and source and Path(source).is_file())
    except (OSError, ValueError):
        available = False
    if plan is not None and getattr(plan, "status_label", "") == "需换字幕":
        available = False
    pending = bool(available and getattr(plan, "manual_review_required", False))
    return {"pending": pending, "active": bool(available and not pending)}


def cache_review_audio(plan, evidence: dict, audio_id) -> None:
    """Retain immutable short-audio inputs, never a confirmed shifted output."""
    try:
        snapshot = json.loads(json.dumps(evidence))
        paths = {str(Path(evidence["review_dir"]) / "manual-source.srt")}
        paths.update(str(clip[key]) for clip in evidence["clips"]
                     for key in ("audio", "slow_audio") if clip.get(key))
        plan._manual_audio_cache = {
            "evidence": snapshot,
            "source_sha256": evidence["source_sha256"],
            "movie_path": str(Path(plan.path).resolve()),
            "media_signature": media_signature(plan.path),
            "audio_id": evidence.get("validation_audio_id", audio_id),
            "language": plan.external_language,
            "hashes": {path: sha256(path) for path in paths},
        }
    except (OSError, ValueError, KeyError, TypeError):
        # Audio reuse is optional; a normal preparation remains available.
        plan._manual_audio_cache = None


def reuse_review_audio(plan, subtitle, work: Path, audio_id) -> dict | None:
    """Share checked audio while giving each reopened review fresh outputs."""
    cached = getattr(plan, "_manual_audio_cache", None)
    if not cached:
        return None
    try:
        if (cached["source_sha256"] != sha256(subtitle)
                or cached["movie_path"] != str(Path(plan.path).resolve())
                or cached["media_signature"] != media_signature(plan.path)
                or cached["audio_id"] != audio_id
                or cached["language"] != plan.external_language
                or not all(hash_matches(path, digest) for path, digest in cached["hashes"].items())):
            return None
        evidence = json.loads(json.dumps(cached["evidence"]))
        work.mkdir(parents=True, exist_ok=False)
        shutil.copy2(Path(evidence["review_dir"]) / "manual-source.srt", work / "manual-source.srt")
        evidence["review_dir"] = str(work)
        evidence["source"] = str(subtitle)
        return evidence
    except (OSError, ValueError, KeyError, TypeError):
        return None


def preserve_origin(old, new) -> None:
    new.audio_verified_source = json.loads(json.dumps(getattr(old, 'audio_verified_source', {}) or {}))
    new.subtitle_report_path = getattr(old, 'subtitle_report_path', '')
    new.subtitle_verification_states = json.loads(json.dumps(getattr(old, 'subtitle_verification_states', []) or []))
    new.manual_review_origin = {key: value for key, value in (getattr(old, "manual_review_origin", {}) or {}).items()
                                if key in ORIGIN_FIELDS}
    new.manual_review_required = bool(getattr(old, "manual_review_required", False))
    new.pending_review_audio_id = getattr(old, "pending_review_audio_id", None)
    new.manual_review_resume_processing = bool(getattr(old, "manual_review_resume_processing", False))
    cached = getattr(old, "_manual_audio_cache", None)
    if cached:
        new._manual_audio_cache = json.loads(json.dumps(cached))
    if new.manual_review_required:
        clear_trust(new)
        new.external_subtitle = old.external_subtitle
        new.external_language = old.external_language
        new.external_subtitle_origin = old.external_subtitle_origin
        new.status, new.status_label = "review", "需要确认"
        new.summary = "人工核听尚未确认，请完成三段核听或重新选择字幕"
        if new.manual_review_resume_processing:
            new.task_state, new.task_status_label = "failed", "处理失败"
            new.status_label = "处理失败"
            new.summary = "处理失败：自动时间轴证据不足；建议：核听当前字幕"


def capture_origin(plan, source, language: str) -> Path:
    source = Path(source).resolve()
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    movie = Path(plan.path)
    baseline = movie.with_name(movie.stem + "_pro_work") / "manual-baselines" / digest / ("source" + source.suffix.lower())
    baseline.parent.mkdir(parents=True, exist_ok=True)
    try:
        with baseline.open("xb") as output:
            output.write(content)
    except FileExistsError:
        pass
    if sha256(baseline) != digest:
        raise ValueError("原字幕基线副本已变化，请重新导入原字幕。")
    plan.manual_review_origin = {
        "raw_path": str(source), "baseline_path": str(baseline),
        "raw_sha256": digest, "language": str(language),
        "movie_path": str(movie.resolve()), "media_signature": media_signature(plan.path),
    }
    return baseline


def _verify_current_output(plan) -> None:
    expected = (getattr(plan, "manual_confirmation_hash", "")
                or getattr(plan, "external_subtitle_content_hash", ""))
    if expected and sha256(plan.external_subtitle) != expected:
        raise ValueError("已核验的字幕文件发生变化，请重新选择原字幕。")


def _from_verdict(plan, output: Path) -> Path:
    """Recover older automatic output only with hashes AND cue mapping proof."""
    import subtitle_tool_core as core
    marker = output.parent / "vad-verdict.json"
    verdict = json.loads(marker.read_text(encoding="utf-8"))
    signature = verdict["signature"]
    if sha256(output) != verdict["output_sha256"]:
        raise ValueError("自动对齐结果与记录摘要不符。")
    if media_signature(plan.path) != {key: signature[key] for key in ("size", "mtime_ns")}:
        raise ValueError("影片已变化，不能沿用原字幕基线。")
    if signature.get("video") and Path(signature["video"]).resolve() != Path(plan.path).resolve():
        raise ValueError("自动记录属于其他影片路径，请重新选择原字幕。")
    candidates = [p for folder in (output.parent, output.parent.parent)
                  for p in folder.iterdir() if p.is_file() and p.suffix.lower() in {".srt", ".ass", ".ssa", ".vtt"}]
    raw = next((p for p in candidates if sha256(p) == signature["subtitle_sha256"]), None)
    if raw is None:
        raise ValueError("找不到摘要一致的原字幕，请重新选择原始字幕文件。")
    before, after = core.parse_subtitle(raw), core.parse_subtitle(output)
    def ms(value):
        h, m, s = value.replace(",", ".").split(":")
        return round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)
    delta = round(float(verdict["applied_offset"] if "applied_offset" in verdict else verdict["proposal_offset"]) * 1000)
    if not before or len(before) != len(after) or any(
        a.text != b.text or ms(b.start) != max(0, ms(a.start) + delta)
        or ms(b.end) != max(ms(b.start) + 1, ms(a.end) + delta)
        for a, b in zip(before, after)
    ):
        raise ValueError("原字幕与对齐输出的时间映射无法证实，请重新选择原字幕。")
    return capture_origin(plan, raw, plan.external_language)


def resolve_origin(plan) -> Path:
    _verify_current_output(plan)
    origin = getattr(plan, "manual_review_origin", {}) or {}
    if origin:
        if (Path(origin.get("movie_path", "")).resolve() != Path(plan.path).resolve()
                or origin.get("media_signature") != media_signature(plan.path)):
            raise ValueError("影片已变化，请重新选择字幕。")
        baseline = Path(origin["baseline_path"])
        if sha256(baseline) != origin["raw_sha256"]:
            raise ValueError("原字幕基线副本已变化，请重新导入原字幕。")
        return baseline
    output = Path(plan.external_subtitle)
    marker = output.parent / "manual-confirmation.json"
    if marker.is_file():
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if sha256(output) != saved["output_sha256"] or not saved.get("manual_review_origin"):
            raise ValueError("人工结果缺少可信原字幕基线，请重新选择原字幕。")
        restored = saved["manual_review_origin"]
        if not isinstance(restored, dict) or saved.get("original_sha256") != restored.get("raw_sha256"):
            raise ValueError("人工确认记录与原字幕摘要不符，请重新选择原字幕。")
        plan.manual_review_origin = {key: value for key, value in restored.items() if key in ORIGIN_FIELDS}
        return resolve_origin(plan)
    if (output.parent / "vad-verdict.json").is_file():
        return _from_verdict(plan, output)
    # Verified outputs without provenance may already contain an offset.
    if getattr(plan, "external_subtitle_verified", False) or output.name in {"online-verified.srt", "manual-confirmed.srt"}:
        raise ValueError("当前字幕缺少原始基线，不能再次叠加修正；请重新导入原字幕。")
    return capture_origin(plan, output, plan.external_language)
