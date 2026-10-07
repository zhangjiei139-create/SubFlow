"""Conservative TrueHD Matroska passthrough policy and bounded header checks.

The experimental packetizer is global, so eligibility covers every retained
track. Header inspection seeks directly to metadata; it never scans clusters.
"""
from __future__ import annotations

import hashlib
import re
import struct
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


class HeaderCheckError(ValueError):
    pass


SEGMENT, CLUSTER, TRACKS = 0x18538067, 0x1F43B675, 0x1654AE6B
CHAPTERS, ATTACHMENTS, SEEK_HEAD = 0x1043A770, 0x1941A469, 0x114D9B74
LIMITS = {TRACKS: 1024 * 1024, CHAPTERS: 4 * 1024 * 1024,
          ATTACHMENTS: 16 * 1024 * 1024, SEEK_HEAD: 64 * 1024}
MASTER_IDS = {0x45B9, 0xB6, 0x80, 0x8F, 0x55B0, 0x55D0, 0x7670}
UINT_IDS = {0x45BC, 0x45BD, 0x45DB, 0x45DD, 0x73C4, 0x91, 0x92, 0x98,
            0x4598, 0x46AE, 0xB0, 0xBA, 0x54B0, 0x54BA, 0x54B2, 0x54AA,
            0x54BB, 0x54CC, 0x54DD, 0x9A, 0x9D, 0x53B8, 0x9F, 0x6264,
            0x55B1, 0x55B2, 0x55B3, 0x55B4, 0x55B5, 0x55B6, 0x55B7,
            0x55B8, 0x55B9, 0x55BA, 0x55BB, 0x55BC, 0x55BD, 0x7671}
FLOAT_IDS = {0xB5, 0x78B5, *range(0x55D1, 0x55DB), 0x7673, 0x7674, 0x7675}
COMPLEX_CHAPTER_IDS = {0x6E67, 0x6EBC, 0x8F, 0x6944}
DOVI_TYPES = {int.from_bytes(b"dvcC", "big"), int.from_bytes(b"dvvC", "big")}
CHAPTER_LANGUAGE_IETF = {b"eng": b"en", b"chi": b"zh", b"zho": b"zh", b"jpn": b"ja",
                         b"kor": b"ko", b"fre": b"fr", b"fra": b"fr", b"ger": b"de",
                         b"deu": b"de", b"spa": b"es", b"ita": b"it", b"rus": b"ru",
                         b"por": b"pt", b"und": b"und"}


def _vint(data: bytes, pos: int, *, is_id: bool = False):
    if pos >= len(data) or data[pos] == 0:
        raise HeaderCheckError("无效 EBML 头")
    width = next(i for i in range(1, 9) if data[pos] & (1 << (8 - i)))
    if width > (4 if is_id else 8) or pos + width > len(data):
        raise HeaderCheckError("不完整 EBML 头")
    value = int.from_bytes(data[pos:pos + width], "big")
    if not is_id:
        value &= (1 << (7 * width)) - 1
        if value == (1 << (7 * width)) - 1:
            value = None
    return value, pos + width


def _children(data: bytes):
    pos = 0
    while pos < len(data):
        eid, pos = _vint(data, pos, is_id=True)
        size, pos = _vint(data, pos)
        if size is None or pos + size > len(data):
            raise HeaderCheckError("元数据长度无效")
        if eid not in {0xEC, 0xBF}:  # Void and CRC are not metadata values.
            yield eid, data[pos:pos + size]
        pos += size


def _file_vint(handle, *, is_id=False):
    first = handle.read(1)
    if not first:
        raise HeaderCheckError("元数据头缺失")
    if first[0] == 0:
        raise HeaderCheckError("无效元数据头")
    width = next(i for i in range(1, 9) if first[0] & (1 << (8 - i)))
    return _vint(first + handle.read(width - 1), 0, is_id=is_id)[0]


def _element(handle):
    return _file_vint(handle, is_id=True), _file_vint(handle)


def _payload(handle, eid, size):
    if size is None or size > LIMITS.get(eid, 64 * 1024):
        raise HeaderCheckError("元数据超过快速检查范围")
    data = handle.read(size)
    if len(data) != size:
        raise HeaderCheckError("元数据截断")
    return data


def _canonical(data: bytes, parent=0, depth=0):
    if depth > 16:
        raise HeaderCheckError("元数据嵌套过深")
    values = {}
    for eid, item in _children(data):
        if eid in COMPLEX_CHAPTER_IDS or (eid == 0x45DD and int.from_bytes(item, "big")):
            raise HeaderCheckError("有序或关联章节沿用原封装")
        if eid in MASTER_IDS:
            value = _canonical(item, eid, depth + 1)
        elif eid in UINT_IDS:
            value = int.from_bytes(item, "big")
        elif eid in FLOAT_IDS:
            if len(item) not in {4, 8}:
                raise HeaderCheckError("浮点元数据无效")
            value = struct.unpack(">f" if len(item) == 4 else ">d", item)[0]
        else:
            value = item
        values.setdefault(eid, []).append(value)
    # Normalize equivalent omitted/explicit defaults written by mkvmerge.
    defaults = {0x45B9: {0x45BD: 0, 0x45DB: 0, 0x45DD: 0},
                0xB6: {0x98: 0, 0x4598: 1}, 0x80: {0x437C: b"eng"}}
    for eid, value in defaults.get(parent, {}).items():
        values.setdefault(eid, [value])
    if parent == 0x80 and 0x437D not in values:
        inferred = [CHAPTER_LANGUAGE_IETF.get(lang) for lang in values.get(0x437C, [])]
        if inferred and all(lang is not None for lang in inferred):
            values[0x437D] = inferred
    return tuple((eid, tuple(items)) for eid, items in sorted(values.items()))


def read_headers(path: str | Path) -> dict:
    """Read at most bounded metadata blocks, using SeekHead after first Cluster."""
    blocks, seeks = {}, {}
    with Path(path).open("rb") as handle:
        eid, size = _element(handle)
        if eid != 0x1A45DFA3:
            raise HeaderCheckError("输入不是 Matroska 容器")
        ebml = dict(_children(_payload(handle, eid, size)))
        if ebml.get(0x4282) != b"matroska":
            raise HeaderCheckError("输入不是 Matroska 容器")
        for _ in range(16):
            eid, size = _element(handle)
            if eid == SEGMENT:
                segment_start = handle.tell()
                break
            if size is None:
                raise HeaderCheckError("Segment 之前存在未知长度")
            handle.seek(size, 1)
        else:
            raise HeaderCheckError("未找到 Matroska Segment")
        for _ in range(64):
            eid, size = _element(handle)
            if eid == CLUSTER:
                break
            if eid in LIMITS:
                data = _payload(handle, eid, size)
                if eid == SEEK_HEAD:
                    for child, entry in _children(data):
                        if child == 0x4DBB:
                            fields = dict(_children(entry))
                            if 0x53AB in fields and 0x53AC in fields:
                                seeks[int.from_bytes(fields[0x53AB], "big")] = (
                                    segment_start + int.from_bytes(fields[0x53AC], "big"))
                else:
                    if eid in blocks:
                        raise HeaderCheckError("重复元数据区块")
                    blocks[eid] = data
            elif size is not None:
                handle.seek(size, 1)
            else:
                raise HeaderCheckError("Tracks 之前存在未知长度")
        if TRACKS not in blocks:
            raise HeaderCheckError("首个 Cluster 之前没有 Tracks")
        for target in (CHAPTERS, ATTACHMENTS):
            if target in seeks and target not in blocks:
                handle.seek(seeks[target])
                eid, size = _element(handle)
                if eid != target:
                    raise HeaderCheckError("元数据索引不一致")
                blocks[eid] = _payload(handle, eid, size)
    tracks = []
    for eid, entry in _children(blocks[TRACKS]):
        if eid != 0xAE:
            continue
        fields = dict(_children(entry))
        mappings = []
        for child, data in _children(entry):
            if child == 0x41E4:
                mapping = dict(_children(data))
                mappings.append((int.from_bytes(mapping.get(0x41E7, b"\0"), "big"),
                                 mapping.get(0x41ED, b""),
                                 int.from_bytes(mapping.get(0x41F0, b"\0"), "big"),
                                 mapping.get(0x41A4, b"")))
        tracks.append({
            "number": int.from_bytes(fields.get(0xD7, b"\0"), "big"),
            "uid": int.from_bytes(fields.get(0x73C5, b"\0"), "big"),
            "default_duration": int.from_bytes(fields.get(0x23E383, b"\0"), "big"),
            "codec_delay": int.from_bytes(fields.get(0x56AA, b"\0"), "big"),
            "seek_preroll": int.from_bytes(fields.get(0x56BB, b"\0"), "big"),
            "max_addition_id": int.from_bytes(fields.get(0x55EE, b"\0"), "big"),
            "codec": fields.get(0x86, b"").decode("ascii", errors="replace"),
            "private": hashlib.sha256(fields.get(0x63A2, b"")).digest(),
            "mappings": tuple(mappings),
            "video": _canonical(fields.get(0xE0, b"")),
            "audio": _canonical(fields.get(0xE1, b"")),
        })
    chapters = blocks.get(CHAPTERS, b"")
    chapter_entries = _canonical(chapters)
    attachments = []
    for eid, entry in _children(blocks.get(ATTACHMENTS, b"")):
        if eid == 0x61A7:
            # All attachment fields including payload, filename, MIME and UID.
            attachments.append(tuple(sorted((child, hashlib.sha256(data).digest())
                                             for child, data in _children(entry))))
    return {"tracks": tracks, "chapters": chapter_entries,
            "attachments": tuple(attachments)}


@lru_cache(maxsize=16)
def _tool_version(path: str, size: int, mtime_ns: int) -> bool:
    del size, mtime_ns  # Part of cache identity; replacements invalidate it.
    try:
        result = subprocess.run([path, "--version"], capture_output=True, timeout=5,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0 and bool(re.match(rb"mkvmerge v100\.0(?:\s|$)", result.stdout))
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass(frozen=True)
class FastMuxPlan:
    enabled: bool
    reason: str
    headers: dict | None = None
    retained: tuple[dict, ...] = ()


def assess(input_path, output_path, media, audio_ids, subtitle_ids, generated, tool,
           *, allowed=True) -> FastMuxPlan:
    no = lambda reason: FastMuxPlan(False, reason)
    if not allowed or Path(input_path).suffix.lower() != ".mkv" or Path(output_path).suffix.lower() != ".mkv":
        return no("仅最终 MKV 输出使用快速封装")
    tracks = (media or {}).get("tracks", [])
    selected_audio = [t for t in tracks if t.get("type") == "audio" and t.get("id") in audio_ids]
    if not selected_audio or len(selected_audio) != len(set(audio_ids)):
        return no("没有可确认的 TrueHD 保留音轨")
    if any(t.get("properties", {}).get("codec_id") != "A_TRUEHD" for t in selected_audio):
        return no("保留了 DTS 或其他尚未覆盖的音轨编码")
    videos = [t for t in tracks if t.get("type") == "video"]
    if len(videos) != 1 or videos[0].get("properties", {}).get("codec_id") != "V_MPEGH/ISO/HEVC":
        return no("视频结构超出已验证的单 HEVC 范围")
    if any(t.get("type") not in {"video", "audio", "subtitles"} for t in tracks):
        return no("包含尚未覆盖的轨道类型")
    selected_subs = [t for t in tracks if t.get("type") == "subtitles" and t.get("id") in subtitle_ids]
    if len(selected_subs) != len(set(subtitle_ids)) or any(
            t.get("properties", {}).get("codec_id") not in {"S_HDMV/PGS", "S_TEXT/UTF8"}
            for t in selected_subs) or any(Path(g[0]).suffix.lower() != ".srt" for g in generated):
        return no("字幕结构超出已验证的 PGS/SRT 范围")
    try:
        if Path(input_path).resolve() == Path(output_path).resolve():
            return no("输出不能覆盖输入")
        stat = Path(tool).stat()
        if not _tool_version(str(Path(tool).resolve()), stat.st_size, stat.st_mtime_ns):
            return no("当前 MKVToolNix 版本不在本次验证范围内")
        headers = read_headers(input_path)
        numbers = {h["number"]: h for h in headers["tracks"]}
        retained = tuple(sorted((t for t in tracks if t.get("type") == "video" or
                                (t.get("type") == "audio" and t.get("id") in audio_ids) or
                                (t.get("type") == "subtitles" and t.get("id") in subtitle_ids)),
                               key=lambda t: {"video": 0, "audio": 1, "subtitles": 2}[t["type"]]))
        selected_headers = []
        for t in retained:
            props = t.get("properties", {})
            h = numbers.get(props.get("number"))
            if h is None or h["codec"] != props.get("codec_id"):
                return no("轨道头与分析结果不一致")
            for mapping_type, config, _value, _name in h["mappings"]:
                if mapping_type not in DOVI_TYPES or t["type"] != "video" or len(config) != 24:
                    return no("未知视频增强层配置，沿用原封装")
                if not (config[:2] == b"\x01\x00" and config[2] >> 1 == 8 and
                        config[3] & 7 == 5 and config[4] >> 4 == 1):
                    return no("杜比视界并非已验证的 DV8.1 单层结构")
            selected_headers.append(h)
        if len(headers["attachments"]) != len((media or {}).get("attachments", [])):
            return no("附件头无法完整核对")
        chapter_count = sum(int(c.get("num_entries", 0)) for c in (media or {}).get("chapters", []))
        if chapter_count and not headers["chapters"]:
            return no("章节头无法完整核对")
        headers = {**headers, "tracks": selected_headers}
        return FastMuxPlan(True, "TrueHD 单层 HEVC，PGS/SRT，已验证的 MKVToolNix 100.0", headers, retained)
    except (OSError, HeaderCheckError, ValueError, TypeError) as exc:
        return no(f"快速封装前置检查未通过：{exc}")


def verify(plan: FastMuxPlan, output_path, output_media, audio_defaults, subtitle_defaults,
           generated_specs) -> None:
    """Verify copied critical headers and expected metadata, without media decoding."""
    output_headers = read_headers(output_path)
    original = plan.headers
    expected_count = len(plan.retained) + len(generated_specs)
    if len(output_headers["tracks"]) != expected_count or len(output_media.get("tracks", [])) != expected_count:
        raise HeaderCheckError("快速封装轨道数量不一致")
    for source, actual in zip(original["tracks"], output_headers["tracks"]):
        if any(source[field] != actual[field] for field in (
                "codec", "private", "mappings", "video", "audio", "uid",
                "default_duration", "codec_delay", "seek_preroll", "max_addition_id")):
            raise HeaderCheckError("快速封装编码私有数据、杜比视界或音视频头发生变化")
    if any(original[field] != output_headers[field] for field in ("chapters", "attachments")):
        raise HeaderCheckError("快速封装章节或附件发生变化")
    output_tracks = output_media["tracks"]
    for source, actual in zip(plan.retained, output_tracks):
        props, out = source.get("properties", {}), actual.get("properties", {})
        if source.get("type") != actual.get("type"):
            raise HeaderCheckError("快速封装轨道类型顺序变化")
        if (props.get("language", "eng") != out.get("language", "eng") or
                (props.get("language_ietf") and props["language_ietf"] != out.get("language_ietf")) or
                (props.get("track_name") or "") != (out.get("track_name") or "") or
                bool(props.get("forced_track")) != bool(out.get("forced_track"))):
            raise HeaderCheckError("快速封装保留轨道的语言或标记变化")
        expected_default = (audio_defaults if source["type"] == "audio" else
                            subtitle_defaults if source["type"] == "subtitles" else {}).get(
                                source["id"], props.get("default_track"))
        if expected_default != out.get("default_track"):
            raise HeaderCheckError("快速封装默认轨道标记不符合设置")
    for (lang, name, default), actual in zip(generated_specs, output_tracks[len(plan.retained):]):
        props = actual.get("properties", {})
        if (actual.get("type") != "subtitles" or props.get("codec_id") != "S_TEXT/UTF8" or
                props.get("language") != lang or props.get("track_name", "") != name or
                bool(props.get("default_track")) != default or bool(props.get("forced_track"))):
            raise HeaderCheckError("快速封装新增字幕的编码、语言或标记不符合设置")


def retryable_failure(exc: Exception) -> bool:
    # A second packetizer cannot fix capacity, permissions or missing tools.
    text = str(exc).lower()
    return not any(token in text for token in (
        "no space", "disk full", "not enough space", "permission denied", "access denied",
        "cannot open", "could not open", "could not be opened", "找不到外部工具", "空间不足",
        "磁盘已满", "拒绝访问", "winerror 112", "winerror 5"))
