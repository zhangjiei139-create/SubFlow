"""Cheap, explainable search ordering; never grants timing acceptance."""
from collections import defaultdict
from pathlib import Path
import re

from subtitle_identity_guard import (
    DISC_SOURCES, clean_video_title, edition_codes, release_metadata, release_tags,
)


def author_name(value):
    if isinstance(value, dict):
        value = value.get("name") or value.get("username") or ""
    return " ".join(value.split())[:96] if isinstance(value, str) else ""


def author_display(candidate):
    return author_name(getattr(candidate, "author", "")) or "未提供"


def candidate_note(candidate):
    name = author_display(candidate)
    role = getattr(candidate, "author_role", "") or "作者 / 上传者"
    reason = getattr(candidate, "priority_reason", "") or "暂无版本匹配线索"
    return f"{role}：{name}；优先依据：{reason}。作者信息不代表已确认不同时间轴。"


def _source_family(tags):
    if tags & DISC_SOURCES:
        return "disc"
    if tags & {"WEBDL", "WEBRIP"}:
        return "web"
    if tags & {"DVD", "DVDRIP"}:
        return "dvd"
    if "HDTV" in tags:
        return "hdtv"
    return ""


def _release_stem(value):
    return re.sub(r"\.(?:mkv|mp4|avi|m2ts|srt|ass|ssa|sub|zip|txt)$", "", value, flags=re.I)


def _release_group(value):
    # Require an actual technical suffix, not the hyphen in a movie title.
    match = re.search(r"-([a-z0-9][a-z0-9_]{1,31})$", _release_stem(value), re.I)
    if not match or not release_tags(value) & {"X264", "X265", "H264", "H265", "HEVC", "BLURAY", "WEBDL", "WEBRIP"}:
        return ""
    group = match[1].casefold()
    return "" if group in {"dl", "rip", "ray", "hd", "ma", "aac", "hevc", "x264", "x265"} else group


def prioritize_candidates(video_path, candidates, identity=None):
    """Keep every candidate. Use author variety only inside equal match tiers.

    Identity/purpose eligibility and downloaded text+timeline dedup stay with
    the caller. Missing edition/author is neither a conflict nor a duplicate.
    """
    title, _year = clean_video_title(str(video_path))
    titles = (title, *(str(getattr(identity, field, "") or "") for field in ("title", "original_title")))
    video_stem = Path(video_path).stem
    metadata = release_metadata(video_stem, titles)
    video_editions = edition_codes(metadata)
    video_source = _source_family(release_tags(metadata))
    video_group = _release_group(video_stem)
    normalize = lambda value: " ".join(re.findall(r"[^\W_]+", value.casefold()))
    video_name = normalize(video_stem)
    tiers = defaultdict(list)
    for candidate in candidates:
        release = str(getattr(candidate, "release", "") or "")
        file_name = str(getattr(candidate, "file_name", "") or "")
        candidate_metadata = " ".join(release_metadata(value, titles) for value in (release, file_name))
        editions = edition_codes(candidate_metadata)
        source = _source_family(release_tags(candidate_metadata))
        hash_match = bool(getattr(candidate, "moviehash_match", False))
        same_name = bool(video_name and any(normalize(_release_stem(value)) == video_name for value in (release, file_name)))
        same_edition = bool(video_editions and editions == video_editions)
        same_source = bool(video_source and source == video_source)
        same_group = bool(video_group and any(_release_group(value) == video_group for value in (release, file_name)))
        signals = (hash_match, same_name, same_edition, same_source, same_group)
        labels = ("影片 Hash 命中", "发行文件名一致", "剪辑版本一致", "片源类型一致", "发布组一致")
        candidate.priority_reason = "、".join(label for yes, label in zip(signals, labels) if yes) or "版本线索不足，保留检查机会"
        tiers[signals].append(candidate)

    ordered = []
    for signals in sorted(tiers, reverse=True):
        occurrences = defaultdict(int)
        rows = []
        for index, candidate in enumerate(tiers[signals]):
            author = author_name(getattr(candidate, "author", "")).casefold()
            # Generic uploader accounts do not identify an independent source.
            known = author not in {"", "anonymous", "unknown", "os-auto", "opensubtitles", "subdl", "n/a"}
            turn = occurrences[author] if known else 0
            if known:
                occurrences[author] += 1
            rows.append((turn, index, candidate))
        ordered.extend(candidate for _turn, _index, candidate in sorted(rows, key=lambda row: row[:2]))
    return ordered
