# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from contextlib import contextmanager
import concurrent.futures
import socket
import hashlib
from pathlib import Path
import re
import threading
from typing import Callable
import time

import online_subtitles
import pro_core
import subdl_subtitles
import subtitle_tool_core as core
from continuous_vad import adopted_alignment_report
from subtitle_identity_guard import (
    edition_conflict,
    filename_container_identity_conflict,
    release_year_conflict,
    source_conflict,
    subtitle_language_label_conflict,
    verification_seal,
)


PROVIDERS = (
    ("OpenSubtitles", online_subtitles, "opensubtitles_api_key"),
    ("SubDL", subdl_subtitles, "subdl_api_key"),
)

_PROVIDER_HOSTS = {
    "OpenSubtitles": "api.opensubtitles.com",
    "SubDL": "api.subdl.com",
}


def _search_result_summary(search_meta, loaded_count, queue_count, filtered_counts):
    """Distinguish service totals, this page batch and the usable queue."""
    total = getattr(search_meta, "total_count", None)
    total_label = (
        str(total) if isinstance(total, int) and total >= loaded_count else "未提供"
    )
    filtered = sum(filtered_counts.values())
    details = "、".join(
        f"{label} {filtered_counts.get(key, 0)}"
        for key, label in (
            ("identity", "影片身份"), ("role", "字幕用途"),
            ("release", "片源/版本/语言"), ("duplicate", "重复或已检查"),
        )
        if filtered_counts.get(key, 0)
    )
    suffix = "；尚有结果未加载，本轮受分页上限限制" if getattr(search_meta, "truncated", False) else ""
    return (
        f"站点总计 {total_label} 条；本轮加载 {loaded_count} 条，"
        f"过滤 {filtered} 条" + (f"（{details}）" if details else "")
        + f"，待核验 {queue_count} 条{suffix}。"
    )


def configured_provider_names() -> list[str]:
    """Return providers that can actually be used by automatic search."""
    return [
        provider_name
        for provider_name, service, key_name in PROVIDERS
        if str(service.load_settings().get(key_name, "")).strip()
    ]


def subtitle_service_available(timeout_seconds: float = 1.5) -> bool:
    """Quickly check whether at least one configured subtitle API is reachable.

    A TCP connection is enough here.  The real API request still validates the
    key when searching, while this gate avoids spending API quota merely to
    decide whether the offline prompt is needed.
    """
    configured = configured_provider_names()
    if not configured:
        return False

    def reachable(provider_name: str) -> bool:
        host = _PROVIDER_HOSTS.get(provider_name)
        if not host:
            return False
        try:
            with socket.create_connection((host, 443), timeout=timeout_seconds):
                return True
        except OSError:
            return False

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=len(configured),
        thread_name_prefix="subtitle-service-probe",
    ) as executor:
        return any(executor.map(reachable, configured))
SMART_TIME_BUDGET_SECONDS = 20.0
# Whole-film preparation uses continuous_vad's duration-based limit. This is
# independent of each candidate's unchanged 20-second matching budget.
SMART_AUDIO_PREPARATION_SECONDS = None
SMART_DOWNLOAD_WORKERS = 3
SMART_DOWNLOAD_PREFETCH = 2
SMART_DOWNLOAD_ATTEMPT_MULTIPLIER = 2
# Only online phases share this gate. A film's VAD/PGS preparation and local
# candidate verification must never hold it. Keep each established download
# wave bounded, rather than multiplying API traffic by the movie worker count.
# Both providers share a settings file, so searches/configuration snapshots
# also use the same gate instead of independent per-provider locks.
_ONLINE_SERVICE_GATE = threading.Lock()
MOJIBAKE_MARKERS = ("�", "锟斤拷", "脡", "脿", "â€")
FOREIGN_WORDS = {
    "fr": {"avec", "dans", "elle", "est", "mais", "nous", "pas", "pour", "que", "une", "vous"},
    "de": {"aber", "das", "der", "die", "ein", "eine", "ich", "ist", "nicht", "und", "wir"},
    "es": {"como", "con", "ella", "esta", "los", "para", "pero", "por", "que", "una"},
    "it": {"che", "con", "della", "non", "per", "sono", "una"},
    "pt": {"com", "ela", "não", "para", "por", "que", "uma"},
}

# Common English function words used only as *positive evidence* that a Latin-script
# subtitle really is English.  This makes the contamination guard resistant to
# character names or ordinary English words that happen to equal a foreign marker
# (for example Cinderella's "Ella", English "die", or English "per").
ENGLISH_EVIDENCE_WORDS = {
    "the", "and", "you", "that", "this", "what", "with", "have", "not", "are",
    "for", "your", "was", "but", "they", "his", "her", "she", "he", "we", "to",
    "of", "in", "is", "it", "on", "be", "as", "at", "do", "my", "there", "here",
    "know", "get", "go", "come", "will", "would", "could", "should",
}


class _DeadlineCancel:
    def __init__(self, cancel_event, deadline: float) -> None:
        self.cancel_event = cancel_event
        self.deadline = deadline

    def is_set(self) -> bool:
        return bool(
            (self.cancel_event is not None and self.cancel_event.is_set())
            or (self.deadline is not None and time.monotonic() >= self.deadline)
        )

    @property
    def timed_out(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline


class _CombinedCancel:
    def __init__(self, *events) -> None:
        self.events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


@contextmanager
def _online_service_phase(cancel_event):
    core.check_cancel(cancel_event)
    while not _ONLINE_SERVICE_GATE.acquire(timeout=0.10):
        core.check_cancel(cancel_event)
    try:
        core.check_cancel(cancel_event)
        yield
    finally:
        _ONLINE_SERVICE_GATE.release()


def _download_wave(executor, wave, cancel_event):
    """Run one bounded network wave, releasing the gate after workers exit.

    A cancelled wait must not unlock while a request still uses the API. This
    preserves the previous request-drain behavior, while moving local subtitle
    parsing, VAD preparation and acceptance outside the online gate.
    """
    with _online_service_phase(cancel_event):
        futures = {}
        try:
            for item in wave:
                core.check_cancel(cancel_event)
                future = executor.submit(
                    item["service"].download,
                    item["key"],
                    item["candidate"],
                    str(item["directory"]),
                )
                futures[future] = item
            pending = set(futures)
            outcomes = {}
            while pending:
                done, pending = concurrent.futures.wait(
                    pending,
                    timeout=0.10,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                core.check_cancel(cancel_event)
                for future in done:
                    item = futures[future]
                    try:
                        outcomes[id(item)] = (Path(future.result()), None)
                    except Exception as exc:
                        outcomes[id(item)] = (None, exc)
            return outcomes
        finally:
            # Provider urllib calls have their existing finite timeouts and
            # cannot be interrupted safely. Cancel queued tasks, then drain
            # active requests before admitting another film's online phase.
            for future in futures:
                future.cancel()
            if futures:
                concurrent.futures.wait(futures)


def candidate_key(provider: str, candidate: object) -> str:
    """Return a stable-enough key for skipping a candidate already rejected in this run."""
    identity = (
        str(getattr(candidate, "file_id", "") or "").strip()
        or str(getattr(candidate, "download_url", "") or "").strip()
        or "|".join(
            (
                str(getattr(candidate, "release", "") or "").strip(),
                str(getattr(candidate, "file_name", "") or "").strip(),
            )
        )
    )
    return f"{provider.strip().lower()}|{identity.lower()}"


def _normalized_candidate_metadata(value: object) -> str:
    text = str(value or "").casefold()
    for _index in range(3):
        text = re.sub(r"\.(?:zip|rar|7z|gz|srt|ass|ssa|vtt|sub)$", "", text)
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    noise = {
        "en", "eng", "english", "subtitle", "subtitles", "sub",
        "srt", "ass", "ssa", "vtt", "utf8", "utf", "8",
    }
    return " ".join(token for token in tokens if token not in noise)


def candidate_metadata_keys(candidate: object) -> set[str]:
    """Return conservative cross-provider keys usable before downloading files."""
    variant = "hi" if bool(getattr(candidate, "hearing_impaired", False)) else "std"
    keys: set[str] = set()
    for name in ("release", "file_name"):
        normalized = _normalized_candidate_metadata(getattr(candidate, name, ""))
        token_count = len(normalized.split())
        if token_count >= 3 or len(normalized) >= 20:
            keys.add(f"{variant}|{normalized}")
    return keys


def subtitle_content_fingerprint(path: str | Path) -> str:
    """Hash canonical timeline and dialogue so provider metadata cannot defeat deduplication."""
    subtitle = Path(path)
    events = core.parse_subtitle(subtitle)
    if events:
        rows = []
        for event in events:
            text = re.sub(r"<[^>]+>|\{\\[^}]+\}", "", event.text)
            text = re.sub(r"\s+", " ", text).strip().casefold()
            rows.append(f"{event.start}|{event.end}|{text}")
        payload = "\n".join(rows).encode("utf-8")
    else:
        payload = subtitle.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def subtitle_content_conflict(path: str | Path, requested: str = "en") -> str:
    events = core.parse_subtitle(Path(path))
    if len(events) < 20:
        return ""
    text = " ".join(event.text for event in events[:600])
    opening_text = " ".join(event.text for event in events[:80]).casefold()
    direct_commentary = re.search(
        r"\b(?:audio|director(?:'s)?|filmmaker) commentary\b|\bwelcome to (?:the|this) commentary\b",
        opening_text,
    )
    role_count = sum(
        bool(re.search(rf"\b{role}\b", opening_text))
        for role in ("writer", "director", "producer", "executive producer")
    )
    role_introduction = re.search(r"\b(?:my name is|i am|i'm)\b", opening_text)
    if direct_commentary or (role_introduction and role_count >= 2):
        return "字幕正文疑似导演或制作人员评论轨，不是影片正片对白"
    latin_mojibake_count = text.count("Ã") + text.count("Â")
    if any(marker in text for marker in MOJIBAKE_MARKERS) or latin_mojibake_count >= 3:
        return "字幕正文存在乱码，禁止进入翻译和封装"
    letters = re.findall(r"[A-Za-zÀ-ÿ]+", text.casefold())
    non_latin = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]", text))
    if requested == "en" and non_latin >= max(12, len(letters) // 20):
        return "字幕正文混入大量非英文内容，不能作为纯英文翻译源"
    if requested == "en" and letters:
        words = [word.casefold() for word in letters]
        counts = Counter(words)
        english_score = sum(counts[word] for word in ENGLISH_EVIDENCE_WORDS)
        foreign_profiles = {
            language: (
                sum(counts[word] for word in markers),
                sum(1 for word in markers if counts[word] > 0),
            )
            for language, markers in FOREIGN_WORDS.items()
        }
        language, (score, distinct_markers) = max(
            foreign_profiles.items(),
            key=lambda item: (item[1][0], item[1][1]),
        )

        # The old guard rejected on an absolute count >=18.  That makes a single
        # proper name enough to condemn a long English subtitle: Cinderella has
        # the character "Ella", which is also Spanish for "she".  Instead require
        # diversified foreign-language evidence *and* material weight relative to
        # clear English evidence.  A real Spanish/French/etc. subtitle still trips
        # this easily; one repeated name/ambiguous word cannot.
        foreign_dominates_enough = score >= max(18, int(english_score * 0.18))
        if score >= 18 and distinct_markers >= 3 and foreign_dominates_enough:
            return (
                f"字幕正文包含大量{language}语言内容"
                f"（外语特征词 {score}，英语特征词 {english_score}，外语特征种类 {distinct_markers}），"
                "不能作为纯英文翻译源"
            )
    return ""


@dataclass
class SmartSubtitleResult:
    subtitle_path: str
    language: str
    report: str
    provider: str
    candidate: object
    identity_key: str = ""
    release: str = ""
    verification_seal: str = ""


def verify_before_processing(
    video_path: str,
    subtitle_path: str,
    provider: str,
    release: str,
    identity_key: str,
    expected_seal: str,
) -> None:
    if not provider or not identity_key or not expected_seal:
        raise RuntimeError("智能字幕缺少影片身份锁，禁止进入翻译和封装。")
    content_conflict = subtitle_content_conflict(subtitle_path, "en")
    if content_conflict:
        raise RuntimeError(content_conflict)
    actual = verification_seal(video_path, subtitle_path, provider, release, identity_key)
    if actual != expected_seal:
        raise RuntimeError("字幕或纠偏规则在预检后发生变化，请重新核验后再进入翻译和封装。")


def _downloaded_candidate_structure_conflict(
    video_path: str,
    subtitle_path: str | Path,
) -> str:
    conflict = subtitle_content_conflict(subtitle_path, "en")
    if conflict:
        return conflict
    source = Path(video_path)
    try:
        if source.stat().st_size < 1024 * 1024:
            return ""
    except OSError:
        return ""
    events = core.parse_subtitle(Path(subtitle_path))
    if not events:
        return "字幕文件没有可识别的正文和时间轴"
    try:
        media = core.inspect_media(video_path)
        duration_ns = core.video_track_duration_ns(media) or core.media_duration_ns(media)
        duration = duration_ns / 1_000_000_000 if duration_ns > 0 else 0.0
    except Exception:
        duration = 0.0
    if duration <= 0:
        return ""
    subtitle_end = max(
        (pro_core._subtitle_time_seconds(event.end) for event in events),
        default=0.0,
    )
    coverage = subtitle_end / duration
    if duration >= 900 and len(events) < 20:
        return f"字幕只有 {len(events)} 条，属于片段或强制字幕"
    if coverage < 0.55:
        return f"字幕只覆盖影片约 {coverage:.0%}，属于不完整字幕"
    if subtitle_end > duration + max(180.0, duration * 0.12):
        return "字幕时间轴明显长于影片"
    return ""


def _candidate_role_conflict(candidate: object, identity: object | None = None) -> str:
    text = " ".join(
        str(getattr(candidate, name, "") or "")
        for name in ("release", "file_name")
    )
    if re.search(r"\bcommentar(?:y|ies)\b", text, flags=re.I):
        return "候选明确标记为评论音轨字幕，不是影片正片字幕"
    if re.search(r"\b(?:official\s+)?(?:main\s+)?trailer\b|\bteaser\b|\bsample\b", text, flags=re.I):
        return "候选明确标记为预告片或样片字幕，不是影片正片字幕"
    if re.search(
        r"\bforced(?:\s+only)?\b|\bforeign\s+(?:parts?\s+)?only\b|\baliens?[ ._-]+only\b|\bsigns?\s*(?:&|and|/)\s*songs?\b",
        text,
        flags=re.I,
    ):
        return "候选明确标记为片段或强制字幕，不是完整字幕"
    if re.search(r"(?<![A-Za-z0-9])(?:cd|disc|disk)[ ._-]?[1-9](?![A-Za-z0-9])", text, flags=re.I):
        return "候选明确标记为 CD/Disc 分卷字幕；单卷不覆盖整片，当前不拼接多卷字幕"

    expected_year = str(getattr(identity, "year", "") or "").strip()
    candidate_year = str(getattr(candidate, "feature_year", "") or "").strip()
    expected_match = re.search(r"(?:19|20)\d{2}", expected_year)
    candidate_match = re.search(r"(?:19|20)\d{2}", candidate_year)
    if expected_match and candidate_match:
        expected_value = int(expected_match.group())
        candidate_value = int(candidate_match.group())
        if abs(expected_value - candidate_value) > 1:
            return f"候选影片年份 {candidate_value} 与原片 {expected_value} 明确冲突"
    return ""


def _candidate_recut_conflict(
    video_path: str, candidate: object, identity: object | None = None,
) -> str:
    """Keep explicit fan/recut releases out of an unmarked film's candidates.

    A word belonging to the actual film title (for example Brideshead
    Revisited) is not a version label. Matching explicit labels on both sides
    are allowed to proceed to the existing timeline checks.
    """
    patterns = {
        "Revisited": r"\brevisited\b",
        "粉丝改版": r"\bfan\s*(?:edit|cut|edition)\b",
        "重新剪辑版": r"\bre\s*(?:cut|edit(?:ed)?)\b",
        "Despecialized": r"\bde\s*speciali[sz]ed\b",
    }
    def normalized(value: str) -> str:
        return re.sub(r"[\W_]+", " ", value.casefold()).strip()

    titles = [str(getattr(identity, name, "") or "")
              for name in ("title", "original_title")]
    # Title words must never become an edition blacklist. The movie's own
    # identity is authoritative here; an unrelated candidate title cannot
    # suppress a release's explicit recut label.
    title_labels = {
        label for label, pattern in patterns.items()
        if any(re.search(pattern, normalized(title)) for title in titles if title)
    }
    def labels(value: str) -> set[str]:
        text = normalized(value)
        return {label for label, pattern in patterns.items()
                if label not in title_labels and re.search(pattern, text)}

    release = " ".join(str(getattr(candidate, name, "") or "")
                       for name in ("release", "file_name"))
    candidate_labels = labels(release)
    if not candidate_labels:
        return ""
    local_labels = labels(Path(video_path).stem)
    specific_labels = {"Revisited", "Despecialized"}
    candidate_specific = candidate_labels & specific_labels
    local_specific = local_labels & specific_labels
    if (candidate_labels == local_labels or
            (candidate_specific and candidate_specific == local_specific)):
        return ""
    description = " / ".join(sorted(candidate_labels))
    return f"候选明确标记为重剪/粉丝改版（{description}），本片未标明相同版本，暂不作为自动时间标杆"


def _candidate_identity_conflict(candidate: object, identity: object | None) -> str:
    """Reject only a corroborated, explicit different-film title before download."""
    if identity is None:
        return ""
    expected_title = str(
        getattr(identity, "original_title", "") or getattr(identity, "title", "") or ""
    )
    feature_title = str(getattr(candidate, "feature_title", "") or "")
    release = " ".join(str(getattr(candidate, name, "") or "") for name in ("release", "file_name"))
    def episode_number(value: str) -> int | None:
        match = re.search(
            r"\b(?:episode|ep\.?|star[ ._-]+wars)[ .:_-]*"
            r"(viii|vii|vi|iv|ix|iii|ii|i|v|[1-9])\b",
            value, flags=re.I,
        )
        if not match:
            return None
        token = match.group(1).upper()
        return int(token) if token.isdigit() else {
            "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5,
            "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
        }[token]
    expected_episode = episode_number(expected_title)
    release_episode = episode_number(release)
    if expected_episode is not None and release_episode is not None and expected_episode != release_episode:
        return f"候选标记为第 {release_episode} 部，本片为第 {expected_episode} 部"
    expected = set(online_subtitles._identity_title_tokens(expected_title))
    feature = set(online_subtitles._identity_title_tokens(feature_title))
    release_tokens = set(online_subtitles._identity_title_tokens(release))
    if expected and feature and expected.isdisjoint(feature) and feature.issubset(release_tokens):
        return f"候选明确属于另一影片：{feature_title}；本片为 {expected_title}"
    # A short title may also be a word inside a different film's full title.
    # Require the provider's extra title word to appear in the release too;
    # edition labels alone do not establish a different film.
    edition_words = {
        "THE", "A", "AN", "OF", "AND", "PART", "CHAPTER", "EPISODE",
        "FILM", "MOVIE", "EXTENDED", "CUT", "DIRECTOR", "DIRECTORS",
        "THEATRICAL", "EDITION", "SPECIAL", "VERSION", "UNRATED",
        "ULTIMATE", "REMASTERED",
    }
    distinct_title_words = feature - expected - edition_words
    if (
        expected & feature
        and distinct_title_words & release_tokens
        and not online_subtitles._titles_typo_equivalent(expected_title, feature_title)
    ):
        return f"候选明确属于另一影片：{feature_title}；本片为 {expected_title}"
    return ""


def _provider_limit_reason(provider_name: str, exc: Exception) -> str:
    text = str(exc)
    if provider_name == "OpenSubtitles" and isinstance(exc, online_subtitles.SubtitleQuotaError):
        return text
    lowered = text.casefold()
    common_markers = ("rate limit", "quota", "too many requests", "429")
    if any(marker in lowered for marker in common_markers):
        return text
    if provider_name == "OpenSubtitles" and (
        "allowed 100 subtitles" in lowered or "请求失败（406）" in text
    ):
        return text
    if provider_name == "SubDL" and "下载次数受限" in text:
        return text
    return ""


def find_verified_english(
    video_path: str,
    selected_audio_id: int | None,
    log: Callable[[str], None],
    cancel_event=None,
    max_candidates: int = 5,
    excluded_candidate_keys: set[str] | None = None,
    excluded_subtitle_fingerprints: set[str] | None = None,
    time_budget_seconds: float = SMART_TIME_BUDGET_SECONDS,
    verification_permission: threading.Event | None = None,
    candidate_acceptance: Callable[[Path, object], None] | None = None,
    candidate_preparation: Callable[[object], None] | None = None,
) -> SmartSubtitleResult:
    """Find the first content-verified subtitle without candidate scoring.

    Each provider may formally verify at most ``max_candidates`` different
    candidates. OpenSubtitles runs first; if none passes, SubDL receives its
    own equal allowance. Download/refill attempts are also bounded.
    """
    log = pro_core.persistent_subtitle_logger(video_path, log)
    if conflict := filename_container_identity_conflict(video_path):
        log(f"智能字幕：{conflict}。")
        raise RuntimeError(conflict)
    failures: list[str] = []
    search_details: list[str] = []
    returned_candidates = 0
    downloadable_candidates = 0
    destination_root = Path(video_path).with_suffix("").with_name(
        Path(video_path).stem + "_pro_work"
    ) / "smart-subtitles"
    excluded = set(excluded_candidate_keys or ())
    seen_fingerprints = set(excluded_subtitle_fingerprints or ())
    seen_candidate_keys: set[str] = set()
    batch_size = max(1, int(max_candidates))
    candidate_budget_seconds = min(
        SMART_TIME_BUDGET_SECONDS,
        max(5.0, time_budget_seconds),
    )
    configured_providers = []
    with _online_service_phase(cancel_event):
        for provider_name, service, key_name in PROVIDERS:
            key = str(service.load_settings().get(key_name, "")).strip()
            if key:
                configured_providers.append((provider_name, service, key_name, key))
    if not configured_providers:
        raise RuntimeError("未配置 OpenSubtitles 或 SubDL API Key，请先使用“手动字幕”完成设置。")
    search_stages = []
    for provider in configured_providers:
        search_stages.append((*provider, False))
        if provider[0] == "OpenSubtitles":
            # This stage is entered only after hash candidates have all failed.
            search_stages.append((*provider, True))

    content_stop = threading.Event()
    content_cancel = _CombinedCancel(cancel_event, content_stop)
    content_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="smart-subtitle-continuous-vad",
    )
    content_future = content_executor.submit(
        pro_core.prepare_shared_subtitle_content_audio,
        video_path,
        selected_audio_id,
        destination_root / "shared-content-audio",
        log,
        content_cancel,
        "en",
        SMART_AUDIO_PREPARATION_SECONDS,
    )
    content_future.add_done_callback(lambda _future: content_executor.shutdown(wait=False))
    log("智能字幕：影片连续VAD指纹准备与在线字幕搜索同时启动。")

    shared_content: pro_core.SharedSubtitleContentAudio | None = None
    shared_content_resolved = False
    attempts = 0
    timed_out_attempts = 0
    serial = 0
    provider_usage: dict[str, list[int]] = {}
    download_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=SMART_DOWNLOAD_WORKERS,
        thread_name_prefix="smart-subtitle-download",
    )
    try:
        hash_fallback_ready = False
        for provider_name, service, _key_name, key, title_fallback in search_stages:
            if title_fallback and not hash_fallback_ready:
                continue
            if title_fallback:
                hash_fallback_ready = False
                used_checks, used_downloads = provider_usage.get(provider_name, [0, 0])
                if (used_checks >= batch_size or
                        used_downloads >= batch_size * SMART_DOWNLOAD_ATTEMPT_MULTIPLIER):
                    continue
            core.check_cancel(cancel_event)
            if title_fallback:
                log("智能字幕：OpenSubtitles 哈希候选未通过，使用本站剩余名额按片名和年份补搜；复用已有音频指纹。")
            else:
                log(f"智能字幕：正在通过 {provider_name} 搜索英文字幕。")
            try:
                with _online_service_phase(cancel_event):
                    if title_fallback:
                        identity, candidates, search_meta = service.search(
                            key, video_path, "en", skip_hash=True,
                        )
                    else:
                        identity, candidates, search_meta = service.search(key, video_path, "en")
            except core.CancelledError:
                raise
            except Exception as exc:
                reason = f"{provider_name} 搜索失败：{exc}"
                failures.append(reason)
                log(f"智能字幕：{reason}")
                continue
            if not title_fallback and provider_name == "OpenSubtitles":
                hash_fallback_ready = getattr(search_meta, "query_mode", "") == "hash"
            log(
                f"智能字幕：{provider_name} 搜索方式="
                f"{getattr(search_meta, 'query_mode', '未记录')}；影片身份="
                f"{getattr(identity, 'original_title', '') or getattr(identity, 'title', '') or '未识别'}"
                f"（{getattr(identity, 'year', '') or '年份未识别'}）。"
            )
            lookup_attempts = getattr(search_meta, "feature_lookup_attempts", ()) or ()
            for lookup_attempt in lookup_attempts:
                log(f"智能字幕：{provider_name} 影片身份查询记录：{lookup_attempt}。")
            fallback_reason = getattr(search_meta, "fallback_reason", "")
            if fallback_reason:
                log(f"智能字幕：{provider_name} 精确搜索回退原因：{fallback_reason}。")
            elif getattr(search_meta, "feature_lookup_reason", ""):
                log(
                    f"智能字幕：{provider_name} 影片身份查询结果："
                    f"{search_meta.feature_lookup_reason}。"
                )
            lookup_reason = fallback_reason or getattr(search_meta, "feature_lookup_reason", "")
            if lookup_reason:
                search_details.append(f"{provider_name}：{lookup_reason}")
            returned_candidates += len(candidates)
            queue: list[object] = []
            filtered_counts = Counter()
            stage_candidate_keys: set[str] = set()
            for candidate in candidates:
                key_value = candidate_key(provider_name, candidate)
                if (key_value in excluded or key_value in seen_candidate_keys
                        or key_value in stage_candidate_keys):
                    filtered_counts["duplicate"] += 1
                    continue
                stage_candidate_keys.add(key_value)
                role_conflict = _candidate_role_conflict(candidate, identity)
                if role_conflict:
                    filtered_counts["role"] += 1
                    failures.append(f"{provider_name} · {role_conflict}")
                    log(
                        f"智能字幕：{provider_name} 候选 "
                        f"{getattr(candidate, 'release', '') or getattr(candidate, 'file_name', '')} "
                        f"未进入正文核验：{role_conflict}。"
                    )
                    continue
                identity_conflict = _candidate_identity_conflict(candidate, identity)
                if identity_conflict:
                    filtered_counts["identity"] += 1
                    failures.append(f"{provider_name} · {identity_conflict}")
                    log(
                        f"智能字幕：{provider_name} 候选 "
                        f"{getattr(candidate, 'release', '') or getattr(candidate, 'file_name', '')} "
                        f"未进入正文核验：{identity_conflict}。"
                    )
                    continue
                release_text = " ".join(
                    str(getattr(candidate, name, "") or "")
                    for name in ("release", "file_name")
                )
                explicit_conflict = next((
                    reason for reason in (
                        source_conflict(video_path, release_text),
                        edition_conflict(video_path, release_text),
                        _candidate_recut_conflict(video_path, candidate, identity),
                        release_year_conflict(
                            video_path,
                            release_text,
                            str(getattr(candidate, "feature_year", "") or ""),
                            exact_hash=bool(getattr(candidate, "moviehash_match", False)),
                        ),
                        subtitle_language_label_conflict(release_text, "en"),
                    ) if reason
                ), "")
                if explicit_conflict:
                    filtered_counts["release"] += 1
                    failures.append(f"{provider_name} · {explicit_conflict}")
                    log(f"智能字幕：候选 {release_text} 未进入下载：{explicit_conflict}。")
                    continue
                queue.append(candidate)
            downloadable_candidates += len(queue)
            log(
                f"智能字幕：{provider_name} "
                + _search_result_summary(search_meta, len(candidates), len(queue), filtered_counts)
            )
            if not queue:
                log(f"智能字幕：{provider_name} 没有返回可下载的英文候选。")
                continue
            log(
                f"智能字幕：{provider_name} 筛选后 {len(queue)} 条候选；"
                f"不按相关度、下载量或可信标记评分，按站点顺序分批检查。"
            )

            cursor = 0
            provider_attempts_before = attempts
            usage = provider_usage.setdefault(provider_name, [0, 0])
            provider_attempts, download_attempts = usage
            hash_stage = provider_name == "OpenSubtitles" and getattr(search_meta, "query_mode", "") == "hash"
            # Keep the established per-provider limit across both searches.
            # Reserve part of it for title/year candidates after bad hash hits.
            stage_attempt_limit = max(1, batch_size // 2) if hash_stage else batch_size
            download_attempt_limit = (
                stage_attempt_limit if hash_stage else batch_size
            ) * SMART_DOWNLOAD_ATTEMPT_MULTIPLIER
            provider_halted = False
            provider_limit_detail = ""
            while (
                cursor < len(queue)
                and provider_attempts < stage_attempt_limit
                and download_attempts < download_attempt_limit
                and not provider_halted
            ):
                prepared: list[dict[str, object]] = []
                remaining_slots = min(
                    stage_attempt_limit - provider_attempts, SMART_DOWNLOAD_PREFETCH
                )
                while (
                    cursor < len(queue)
                    and len(prepared) < remaining_slots
                    and download_attempts < download_attempt_limit
                    and not provider_halted
                ):
                    needed = min(
                        remaining_slots - len(prepared),
                        download_attempt_limit - download_attempts,
                        SMART_DOWNLOAD_WORKERS,
                    )
                    wave_candidates = queue[cursor:cursor + needed]
                    cursor += len(wave_candidates)
                    download_attempts += len(wave_candidates)
                    usage[1] = download_attempts
                    wave: list[dict[str, object]] = []
                    for candidate in wave_candidates:
                        seen_candidate_keys.add(candidate_key(provider_name, candidate))
                        serial += 1
                        release = (
                            getattr(candidate, "release", "")
                            or getattr(candidate, "feature_title", "")
                            or f"候选 {serial}"
                        )
                        candidate_dir = destination_root / f"{serial:03d}-{provider_name.lower()}"
                        log(f"智能字幕：下载候选 {serial}：{release}")
                        wave.append({
                            "provider": provider_name,
                            "service": service,
                            "key": key,
                            "candidate": candidate,
                            "release": str(release),
                            "directory": candidate_dir,
                        })
                    outcomes = _download_wave(download_executor, wave, cancel_event)

                    for item in wave:
                        downloaded, download_error = outcomes[id(item)]
                        release = str(item["release"])
                        try:
                            if download_error is not None:
                                raise download_error
                            assert downloaded is not None
                            structure_conflict = _downloaded_candidate_structure_conflict(
                                video_path,
                                downloaded,
                            )
                            if structure_conflict:
                                raise RuntimeError(structure_conflict)
                            fingerprint = subtitle_content_fingerprint(downloaded)
                            if fingerprint in seen_fingerprints:
                                log(
                                    f"智能字幕：{release} 与已有字幕正文和时间轴完全相同，"
                                    "不占正式候选名额。"
                                )
                                continue
                            seen_fingerprints.add(fingerprint)
                            prepared_item = {
                                "provider": provider_name,
                                "candidate": item["candidate"],
                                "release": release,
                                "directory": item["directory"],
                                "subtitle": downloaded,
                                "fingerprint": fingerprint,
                            }
                            # Identical dialogue can belong to different cuts or
                            # carry different fixed offsets. Only the fingerprint
                            # above (dialogue *and* timeline) is a duplicate.
                            prepared.append(prepared_item)
                        except core.CancelledError:
                            raise
                        except Exception as exc:
                            reason = f"{provider_name} · {release} 下载后筛选未通过：{exc}"
                            failures.append(reason)
                            limit_detail = _provider_limit_reason(provider_name, exc)
                            authentication_failed = (
                                provider_name == "OpenSubtitles"
                                and isinstance(exc, online_subtitles.SubtitleAuthenticationError)
                            )
                            if limit_detail or authentication_failed:
                                provider_halted = True
                                provider_limit_detail = limit_detail or str(exc)
                                hash_fallback_ready = False
                                stop_cause = "认证失败" if authentication_failed else "额度或频率限制"
                                log(
                                    f"智能字幕：{provider_name} 已触发{stop_cause}，"
                                    "停止该站剩余下载并切换下一站。"
                                )
                            else:
                                log(f"智能字幕：{reason}；继续补充本批候选。")

                if not prepared:
                    if provider_halted:
                        break
                    continue
                if verification_permission is not None:
                    log(
                        f"智能字幕：本批已准备 {len(prepared)} 条不同候选；"
                        "等待内嵌文本时间轴结论后再启动内容筛选。"
                    )
                    while not verification_permission.wait(0.10):
                        core.check_cancel(cancel_event)

                if not shared_content_resolved:
                    shared_content_resolved = True
                    try:
                        shared_content = content_future.result()
                    except core.CancelledError as exc:
                        core.check_cancel(cancel_event)
                        raise RuntimeError(
                            "影片连续VAD指纹准备达到时间上限，无法安全核验在线字幕候选。"
                        ) from exc
                    except pro_core.SubtitlePreflightTimeoutError:
                        raise
                    except Exception as exc:
                        raise RuntimeError(
                            f"影片连续VAD指纹准备失败，无法安全核验在线字幕候选：{exc}"
                        ) from exc

                    if shared_content is not None and candidate_preparation is not None:
                        # Film-level IO is shared by every candidate. It must
                        # finish before starting any candidate's 20s clock.
                        candidate_preparation(content_cancel)

                if shared_content is None:
                    raise RuntimeError("影片连续VAD指纹准备未返回有效结果，无法安全核验在线字幕候选。")

                log(
                    f"智能字幕：开始核验本批 {len(prepared)} 条候选；"
                    f"每条独立最多 {candidate_budget_seconds:.0f} 秒。"
                )
                for item in prepared:
                    if provider_attempts >= stage_attempt_limit:
                        break
                    core.check_cancel(cancel_event)
                    attempts += 1
                    provider_attempts += 1
                    usage[0] = provider_attempts
                    candidate = item["candidate"]
                    release = str(item["release"])
                    candidate_dir = Path(item["directory"])
                    downloaded = Path(item["subtitle"])
                    candidate_cancel = _DeadlineCancel(
                        cancel_event,
                        time.monotonic() + candidate_budget_seconds,
                    )
                    candidate_cancel.candidate_match_clock = True
                    log(f"智能字幕：正式核验第 {attempts} 条候选：{release}")
                    try:
                        aligned, report = pro_core.preflight_online_subtitle(
                            video_path,
                            str(downloaded),
                            str(candidate_dir / "preflight"),
                            selected_audio_id,
                            shared_content,
                            log,
                            candidate_cancel,
                            getattr(candidate, "language", "") or "en",
                            candidate_label=f"{provider_name}候选",
                            time_budget_seconds=candidate_budget_seconds,
                        )
                        if candidate_acceptance is not None:
                            try:
                                candidate_acceptance(Path(aligned), candidate_cancel)
                            except pro_core.SharedImageTimingPreparationRequired as request:
                                core.check_cancel(candidate_cancel)
                                remaining = max(0.0, candidate_cancel.deadline - time.monotonic())
                                log(
                                    "智能字幕：局部图片字幕证据不足，按影片共用预算补读一次全片；"
                                    f"当前候选剩余核验额度 {remaining:.2f} 秒，读取后继续同一已对齐字幕。"
                                )
                                pro_core.complete_shared_image_timing(request, log, content_cancel)
                                core.check_cancel(cancel_event)
                                candidate_cancel.deadline = time.monotonic() + remaining
                                try:
                                    candidate_acceptance(Path(aligned), candidate_cancel)
                                except pro_core.SharedImageTimingPreparationRequired as repeated:
                                    raise pro_core.SharedImageTimingPreparationError(
                                        "影片共用图片字幕时间点补读后仍不可用，未再次扫描或采用本候选。"
                                    ) from repeated
                        core.check_cancel(candidate_cancel)
                        identity_key = (
                            str(getattr(candidate, "identity_key", "") or "")
                            or candidate_key(provider_name, candidate)
                        )
                        seal = verification_seal(
                            video_path,
                            str(aligned),
                            provider_name,
                            release,
                            identity_key,
                        )
                        core.check_cancel(candidate_cancel)
                        adopted_report = adopted_alignment_report(report)
                        if adopted_report:
                            log(f"{provider_name}候选：{adopted_report}")
                        return SmartSubtitleResult(
                            subtitle_path=str(aligned),
                            language=getattr(candidate, "language", "") or "en",
                            report=adopted_report or report,
                            provider=provider_name,
                            candidate=candidate,
                            identity_key=identity_key,
                            release=release,
                            verification_seal=seal,
                        )
                    except pro_core.SharedImageTimingPreparationError:
                        # Shared movie IO is not a failed subtitle candidate.
                        raise
                    except core.CancelledError:
                        if cancel_event is not None and cancel_event.is_set():
                            raise
                        if candidate_cancel.timed_out:
                            timed_out_attempts += 1
                            reason = (
                                f"{provider_name} · {release}：本条候选独立核验达到 "
                                f"{candidate_budget_seconds:.0f} 秒上限"
                            )
                            failures.append(reason)
                            log(f"智能字幕：候选核验超时，进入下一条。{reason}")
                            continue
                        core.check_cancel(cancel_event)
                        raise
                    except pro_core.SubtitlePreflightTimeoutError as exc:
                        timed_out_attempts += 1
                        reason = f"{provider_name} · {release}：{exc}"
                        failures.append(reason)
                        log(f"智能字幕：候选核验超时，进入下一条。{reason}")
                    except Exception as exc:
                        reason = f"{provider_name} · {release}：{exc}"
                        failures.append(reason)
                        log(f"智能字幕：候选未通过，进入下一条。{reason}")
                if provider_halted:
                    break
                if provider_attempts >= stage_attempt_limit:
                    if hash_stage and stage_attempt_limit < batch_size:
                        log(f"智能字幕：哈希搜索已核验 {provider_attempts} 条，本站还保留 {batch_size-provider_attempts} 条名额供片名年份补搜。")
                    else:
                        log(
                            f"智能字幕：{provider_name} 已核验完本片最多 "
                            f"{batch_size} 条有效候选，切换下一站。"
                        )
                    break
                if download_attempts >= download_attempt_limit:
                    log(
                        f"智能字幕：{provider_name} 已达到本片最多 "
                        f"{download_attempt_limit} 次下载尝试，停止本轮下载。"
                    )
                    break
                if cursor < len(queue):
                    log(
                        f"智能字幕：{provider_name} 当前一批没有通过，"
                        "在本站限额内继续补充下一批。"
                    )

            if provider_halted and provider_limit_detail:
                failures.append(f"{provider_name} 已停止：{provider_limit_detail}")
                if provider_name == "OpenSubtitles":
                    hash_fallback_ready = False
            if attempts == provider_attempts_before:
                log(
                    f"智能字幕：{provider_name} 没有候选进入正式内容核验。"
                )
            else:
                log(
                    f"智能字幕：{provider_name} 已正式核验 {provider_attempts} 条有效候选，"
                    "仍没有通过安全核验的字幕。"
                )
    finally:
        content_stop.set()
        download_executor.shutdown(wait=True, cancel_futures=True)

    if attempts == 0:
        if downloadable_candidates:
            outcome = "候选下载或预检未通过，没有字幕进入时间轴核验。"
        elif returned_candidates:
            outcome = "站点返回的候选均在下载前被排除，没有字幕进入时间轴核验。"
        else:
            outcome = "没有找到可下载的英文字幕候选，没有字幕进入时间轴核验。"
        detail = failures[-1] if failures else ("；".join(search_details) or "两站未提供可用候选。")
        raise RuntimeError(f"{outcome}建议改用手动字幕选择。最后结果：{detail}")
    detail = failures[-1] if failures else "已进入核验的候选均未通过。"
    timeout_detail = (
        f"其中 {timed_out_attempts} 条使用完各自 {candidate_budget_seconds:.0f} 秒额度。"
        if timed_out_attempts
        else ""
    )
    raise RuntimeError(
        f"已检验 {attempts} 条英文字幕，均未通过安全核验。"
        f"{timeout_detail}"
        f"建议改用手动字幕选择。最后结果：{detail}"
    )
