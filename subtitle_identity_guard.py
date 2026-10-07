from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


DISC_SOURCES = {"BLURAY", "BDRIP", "REMUX"}
THEATRICAL_SOURCES = {"CAM", "HDCAM", "TS", "HDTS", "TC", "HDTC", "TELESYNC", "TELECINE"}
SOURCE_TAGS = DISC_SOURCES | THEATRICAL_SOURCES | {"WEBDL", "WEBRIP", "HDTV", "DVDRIP"}
EDITION_PATTERNS = {
    "extended": r"\bextended(?:[ ._-]+(?:cut|edition))?\b",
    "theatrical": r"\btheatrical(?:[ ._-]+cut)?\b",
    "director": r"\bdirector(?:'s|s)?[ ._-]+cut\b",
    "unrated": r"\bunrated(?:[ ._-]+(?:cut|edition))?\b",
    "final": r"\bfinal[ ._-]+cut\b",
    "ultimate": r"\bultimate[ ._-]+cut\b",
}
EDITION_LABELS = {
    "extended": "加长版",
    "theatrical": "院线版",
    "director": "导演剪辑版",
    "unrated": "未分级版",
    "final": "最终剪辑版",
    "ultimate": "终极剪辑版",
}
SOURCE_LABELS = {
    "REMUX": "REMUX",
    "BLURAY": "Blu-ray",
    "BDRIP": "BDRip",
    "WEBDL": "WEB-DL",
    "WEBRIP": "WEBRip",
    "HDTV": "HDTV",
    "DVDRIP": "DVDRip",
}
LANGUAGE_ALIASES = {
    "en": {"en", "eng", "english"},
    "fr": {"fr", "fre", "fra", "french"},
    "de": {"de", "ger", "deu", "german"},
    "es": {"es", "spa", "spanish"},
    "it": {"it", "ita", "italian"},
    "pt": {"pt", "por", "portuguese"},
    "ru": {"ru", "rus", "russian"},
    "zh": {"zh", "chi", "zho", "chinese"},
}


def release_tags(value: str) -> set[str]:
    normalized = value.upper()
    replacements = {
        "WEB-DL": "WEBDL",
        "WEB.DL": "WEBDL",
        "WEB DL": "WEBDL",
        "WEB-RIP": "WEBRIP",
        "WEB.RIP": "WEBRIP",
        "BLU-RAY": "BLURAY",
        "BLU.RAY": "BLURAY",
        "H.264": "H264",
        "H.265": "H265",
        "TELE-SYNC": "TELESYNC",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    return set(re.findall(r"[A-Z0-9]+", normalized))


def source_conflict(video_path: str, candidate_text: str) -> str:
    input_sources = release_tags(Path(video_path).stem) & SOURCE_TAGS
    candidate_sources = release_tags(candidate_text) & SOURCE_TAGS
    if input_sources & DISC_SOURCES and candidate_sources & THEATRICAL_SOURCES:
        return "蓝光/REMUX 影片禁止使用 HDTS/TS/CAM/TC 等影院片源字幕"
    return ""


def edition_codes(value: str) -> set[str]:
    normalized = value.casefold()
    editions = {
        name for name, pattern in EDITION_PATTERNS.items()
        if re.search(pattern, normalized, flags=re.I)
    }
    if _has_director_cut_release_tag(normalized):
        editions.add("director")
    return editions


def _has_director_cut_release_tag(value: str) -> bool:
    """Recognize DC only in the technical part of a dated release name.

    A plain DC word can name a place, publisher, or film. Require a preceding
    release year and an adjacent source/resolution/codec tag before treating
    the abbreviation as an explicit Director's Cut label. Unclear abbreviations
    stay unknown and continue through the existing timeline checks.
    """
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    technical_tags = {
        "uhd", "bluray", "bdrip", "remux", "webdl", "webrip", "hdtv", "dvdrip",
        "x264", "x265", "h264", "h265", "hevc", "avc",
    }
    def is_technical(token: str) -> bool:
        return token in technical_tags or bool(
            re.fullmatch(r"(?:480|576|720|1080|2160|4320)[pi]", token)
        )

    for index, token in enumerate(tokens):
        if token != "dc" or not any(
            re.fullmatch(r"(?:19|20)\d{2}", earlier) for earlier in tokens[:index]
        ):
            continue
        previous = tokens[index - 1] if index else ""
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        # DC Comics/League/etc. remains a name even after a technical token.
        # A trailing DC is accepted only at the end of the release/file stem.
        if is_technical(following) or (
            is_technical(previous) and following in {"", "mkv", "mp4", "avi", "srt", "ass", "ssa"}
        ):
            return True
    return False


def edition_label(value: str, unknown: str = "剪辑版本未标明") -> str:
    editions = edition_codes(value)
    if not editions:
        return unknown
    return " / ".join(EDITION_LABELS[name] for name in sorted(editions))


def media_specification(video_path: str, duration_seconds: float = 0.0, file_size_bytes: int | None = None) -> str:
    stem = Path(video_path).stem
    edition = edition_label(stem)
    tags = release_tags(stem)
    source = next((label for code, label in SOURCE_LABELS.items() if code in tags), "片源类型未标明")
    details = []
    if "IMAX" in tags:
        details.append("IMAX")
    if re.search(r"\bopen[ ._-]*matte\b", stem, flags=re.I):
        details.append("Open Matte")
    if re.search(r"\b120\s*fps\b", stem, flags=re.I):
        details.append("120fps")
    parts = [edition, source]
    if duration_seconds > 0:
        total = int(round(duration_seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts.append(f"片长 {hours}:{minutes:02d}:{seconds:02d}")
    if file_size_bytes is not None and file_size_bytes >= 0:
        parts.append(f"大小 {file_size_bytes / (1024 ** 3):.2f}G")
    parts.extend(details)
    return " · ".join(parts)


def edition_conflict(video_path: str, candidate_text: str) -> str:
    """Reject only an explicit local/candidate cut mismatch."""
    local_editions = edition_codes(Path(video_path).stem)
    candidate_editions = edition_codes(candidate_text)
    if local_editions and candidate_editions and local_editions.isdisjoint(candidate_editions):
        local_label = " / ".join(EDITION_LABELS[name] for name in sorted(local_editions))
        candidate_label = " / ".join(EDITION_LABELS[name] for name in sorted(candidate_editions))
        return f"剪辑版本冲突：影片为 {local_label}，字幕候选为 {candidate_label}"
    return ""


def release_year_conflict(
    video_path: str,
    candidate_text: str,
    feature_year: str = "",
    *,
    exact_hash: bool = False,
) -> str:
    """Reject a different movie year before any expensive subtitle validation."""
    video_years = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", Path(video_path).stem)
    if not video_years:
        return ""
    # A title may itself contain a year (for example "The Legend of 1900").
    # The release year is normally the last year token in the filename.
    expected = int(video_years[-1])
    # A year written in the release/file name is direct evidence. Provider
    # metadata (and even a hash label) must not cancel an explicit conflict.
    release_years = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", candidate_text)
    release_conflicts = sorted({int(value) for value in release_years if abs(int(value) - expected) >= 2})
    release_matches = {int(value) for value in release_years if abs(int(value) - expected) <= 1}
    if release_conflicts and not release_matches:
        return f"年份冲突：影片为 {expected}，字幕文件名标记为 {release_conflicts[0]}"
    if not release_years and re.fullmatch(r"19\d{2}|20\d{2}", str(feature_year).strip()):
        provider_year = int(str(feature_year).strip())
        if abs(provider_year - expected) >= 2:
            return f"年份冲突：影片为 {expected}，字幕站点标记为 {provider_year}"
    return ""


def filename_container_identity_conflict(video_path: str) -> str:
    """Stop when the movie filename and container state conflicting film years."""
    embedded = container_title(video_path)
    if not embedded:
        return ""
    filename_years = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", Path(video_path).stem)
    embedded_years = re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", embedded)
    if not filename_years or not embedded_years:
        return ""
    filename_year = int(filename_years[-1])
    embedded_year = int(embedded_years[-1])
    if abs(filename_year - embedded_year) < 2:
        return ""
    # Suggest a legal filename without treating metadata as verified content.
    suggested_stem = re.sub(r'[<>"/\\|?*\x00-\x1f]', "_", embedded)
    suggested_stem = re.sub(r":\s*", "：", suggested_stem).strip().rstrip(". ")
    suggested_name = suggested_stem + (Path(video_path).suffix or ".mkv")
    return (
        "影片身份冲突：文件名与影片内部标题不一致。\n\n"
        f"文件名：{Path(video_path).name}\n"
        f"影片内部标题：{embedded}\n\n"
        "请先播放片头，确认实际是哪部影片。\n"
        "若片头与内部标题一致，可将影片文件名改为：\n"
        f"{suggested_name}\n\n"
        "改正名称后，请从影片列表移除旧记录，再重新添加并分析。\n"
        "为避免匹配另一部影片的字幕，暂时停止搜索。"
    )


def identity_conflict_notice(message: str) -> str:
    """Display this user-action warning without a worker exception prefix."""
    detail = message.removeprefix("RuntimeError: ")
    prefix = "影片身份冲突："
    return detail[len(prefix):] if detail.startswith(prefix) else ""


def subtitle_language_label_conflict(candidate_text: str, requested: str = "en") -> str:
    """Reject metadata that explicitly identifies a mixed-language subtitle."""
    normalized = re.sub(r"[\[\](){}]", " ", candidate_text.casefold())
    # MULTI alone commonly labels the movie's audio release. It does not tell
    # us whether the separately offered English subtitle contains mixed text.
    # Keep explicit subtitle/language labels; the downloaded body is checked
    # independently before it can become a translation source.
    if re.search(
        r"\b(?:multi[ ._-]*(?:lang(?:uage)?s?|lingual|sub(?:title)?s?)"
        r"|bilingual|dual[ ._-]*sub(?:title)?s?)\b",
        normalized,
    ):
        return "字幕候选明确标记为多语言/双语，不能作为纯英文翻译源"
    joined_pair = re.search(
        r"\b(?:en|eng|english)\s*[+&,/_.-]\s*(?:fr|fre|fra|french|de|ger|deu|german|es|spa|spanish|it|ita|pt|por|ru|rus|zh|chi|zho)\b",
        normalized,
    )
    if joined_pair:
        return "字幕候选同时标记英文和其他语言，不能作为纯英文翻译源"
    return ""


def container_title(video_path: str) -> str:
    try:
        import subtitle_tool_core as core

        media = core.inspect_media(video_path)
    except Exception:
        return ""
    container = media.get("container") or {}
    properties = container.get("properties") or {}
    return str(properties.get("title") or container.get("title") or "").strip()


def container_title_can_replace_filename(filename_title: str, embedded_title: str) -> bool:
    """A container title is untrusted metadata, not a competing movie identity."""
    filename_words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", filename_title.casefold())
    embedded_words = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", embedded_title.casefold())
    if not embedded_words:
        return False
    if not filename_words or filename_words in (["movie"], ["video"], ["film"], ["untitled"], ["unknown"]):
        return True
    if filename_words == embedded_words:
        return True
    # Preserve the established one-word truncation recovery for titles such as
    # "Freakier" -> "Freakier Friday". Short complete titles like "Fury"
    # must not be replaced by a longer, unrelated muxer title.
    return (
        len(filename_words) == 1
        and len(filename_words[0]) >= 7
        and len(embedded_words) == 2
        and embedded_words[0] == filename_words[0]
    )


def verification_seal(
    video_path: str,
    subtitle_path: str,
    provider: str,
    release: str,
    identity_key: str,
) -> str:
    # Bind the accepted timeline to the current verdict rules while keeping
    # acoustic caches independent. Import lazily to avoid startup cycles.
    import continuous_vad

    subtitle = Path(subtitle_path)
    payload = {
        "video": str(Path(video_path).resolve()).casefold(),
        "subtitle_sha256": hashlib.sha256(subtitle.read_bytes()).hexdigest(),
        "provider": provider,
        "release": release,
        "identity_key": identity_key,
        "alignment_rule_version": continuous_vad.RULE_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
