# -*- coding: utf-8 -*-
from __future__ import annotations

import gzip
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path

from media_title_resolver import (
    TITLE_SETTINGS_LOCK,
    canonical_title_source,
    needs_canonical_title,
    reject_canonical_title,
    remember_canonical_title,
    resolve_canonical_title,
    sequel_number,
    sequel_title_conflict,
)
from subtitle_identity_guard import (
    container_title,
    container_title_can_replace_filename,
    edition_label,
    filename_container_identity_conflict,
    media_specification,
)


API_BASE = "https://api.subdl.com/api/v1"
API_V2_BASE = "https://api.subdl.com/api/v2"
DOWNLOAD_BASE = "https://dl.subdl.com"
USER_AGENT = "SubFlow/2.0.68"
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")


@dataclass
class MediaIdentity:
    title: str
    original_title: str = ""
    year: str = ""
    imdb_id: str = ""
    tmdb_id: str = ""
    provider_id: str = ""
    edition: str = ""
    specification: str = ""


@dataclass
class SubtitleCandidate:
    file_id: str
    file_name: str
    release: str
    language: str
    downloads: int
    rating: float
    trusted: bool
    hearing_impaired: bool
    moviehash_match: bool = False
    recommendation: str = ""
    match_reason: str = ""
    download_url: str = ""
    feature_title: str = ""
    feature_year: str = ""
    identity_verified: bool = False
    identity_key: str = ""


@dataclass
class SearchMeta:
    total_count: int = 0
    loaded_count: int = 0
    query_mode: str = "filename"
    moviehash: str = ""
    truncated: bool = False
    feature_lookup_status: str = ""
    feature_lookup_reason: str = ""
    feature_lookup_attempts: tuple[str, ...] = ()
    fallback_reason: str = ""


@dataclass
class _MovieLookupDiagnostics:
    status: str = ""
    reason: str = ""
    attempts: list[str] = field(default_factory=list)


_MOVIE_LOOKUP_DIAGNOSTICS: ContextVar[_MovieLookupDiagnostics | None] = ContextVar(
    "subdl_movie_lookup_diagnostics", default=None,
)


PROVIDER_TAGS = {
    "DSNP", "NF", "NETFLIX", "AMZN", "AMAZON", "HMAX", "HULU",
    "ATVP", "APPLETV", "PMTP", "PEACOCK",
}
DETAIL_TAGS = {"2160P", "1080P", "720P", "H264", "H265", "X264", "X265", "HDR", "DV", "ATMOS"}
TITLE_STOPWORDS = {"THE", "A", "AN", "OF", "AND", "IN", "ON", "TO", "FOR", "PART"}


def _title_sequence(value: str) -> list[str]:
    ignored = PROVIDER_TAGS | DETAIL_TAGS
    return [
        token for token in re.findall(r"[A-Z0-9]+", value.upper())
        if token not in ignored
        and not re.fullmatch(r"(?:19|20)\d{2}", token)
        and not re.fullmatch(r"\d{3,4}P", token)
        and not token.isdigit()
    ]


def settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubFlow"
    root.mkdir(parents=True, exist_ok=True)
    return root / "online-subtitles.json"


def load_settings() -> dict:
    with TITLE_SETTINGS_LOCK:
        return _load_settings_locked()


def _load_settings_locked() -> dict:
    try:
        settings = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {}
    if not settings.get("subdl_api_key"):
        legacy = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "SubtitleTrackTool-Advanced-Pro-CN"
            / "online-subtitles.json"
        )
        try:
            legacy_settings = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            legacy_settings = {}
        legacy_key = str(legacy_settings.get("subdl_api_key", "")).strip()
        if legacy_key:
            settings["subdl_api_key"] = legacy_key
            settings_path().write_text(
                json.dumps(settings, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return settings


def save_settings(api_key: str) -> None:
    with TITLE_SETTINGS_LOCK:
        settings = load_settings()
        settings.pop("assrt_token", None)
        settings["subdl_api_key"] = api_key.strip()
        settings_path().write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_video_title(path: str) -> tuple[str, str]:
    stem = Path(path).stem
    year_matches = list(re.finditer(r"\b(19\d{2}|20\d{2})\b", stem))
    year_match = year_matches[-1] if year_matches else None
    year = year_match.group(1) if year_match else ""
    title = re.sub(r"[._]+", " ", stem)
    if year_match:
        title = title[: title.find(year)].strip()
    title = re.sub(r"\[[^\]]*\]|【[^】]*】|\([^)]*(?:www\.|@)[^)]*\)", " ", title, flags=re.I)
    title = re.sub(
        r"\b(?:2160p|1080p|720p|BluRay|WEB[- .]?DL|WEBRip|BDRip|HDR|DV|x26[45]|H\.?26[45]|REMUX|AAC|DDP?\d?(?:\.\d)?|Atmos)\b.*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\s+", " ", title).strip(" -._([{<")
    return title or Path(path).stem, year


def identify_media(video_path: str) -> MediaIdentity:
    title, year = clean_video_title(video_path)
    embedded_title = container_title(video_path)
    if embedded_title:
        embedded_title = re.sub(
            r"\s*[-–—]?\s*\b(?:blu[- .]?ray|remux|web[- .]?dl|webrip|bdrip|hdtv)\b.*$",
            "",
            embedded_title,
            flags=re.I,
        ).strip()
        resolved_title, embedded_year = clean_video_title(embedded_title)
        if container_title_can_replace_filename(title, resolved_title):
            title = resolved_title
            year = year or embedded_year
    return MediaIdentity(
        title=title,
        original_title=title,
        year=year,
        edition=edition_label(Path(video_path).stem),
        specification=media_specification(video_path),
    )


def _error_message(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "未知错误")
    return str(error or payload.get("message") or payload.get("detail") or "未知错误")


def _request(api_key: str, path: str, *, params: dict | None = None) -> dict:
    key = api_key.strip()
    if not key:
        raise RuntimeError("请先填写 SubDL API Key。")
    query = {"api_key": key}
    if params:
        query.update({name: value for name, value in params.items() if value not in (None, "")})
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            payload = {}
        if exc.code == 401:
            raise RuntimeError("SubDL API Key 无效，请重新复制账户面板中的 Key。") from exc
        if exc.code in (403, 429):
            raise RuntimeError("SubDL 暂时拒绝请求，可能触发短时限流，请稍后再试。") from exc
        if exc.code == 422:
            raise RuntimeError("SubDL 接口暂时无法处理请求，请稍后再试。") from exc
        message = _error_message(payload) if payload else detail
        raise RuntimeError(f"SubDL 请求失败（{exc.code}）：{message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 SubDL：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("SubDL 返回的数据格式异常，请稍后再试。") from exc

    if payload.get("status") is False or payload.get("success") is False:
        message = _error_message(payload)
        if "can't find movie" in message.lower() or "not found" in message.lower():
            return payload
        raise RuntimeError(f"SubDL 请求失败：{message}")
    return payload


def _request_v2(api_key: str, path: str, *, params: dict | None = None) -> dict:
    key = api_key.strip()
    if not key:
        raise RuntimeError("请先填写 SubDL API Key。")
    query = {
        name: value for name, value in (params or {}).items()
        if value not in (None, "")
    }
    url = f"{API_V2_BASE}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(detail) if detail else {}
        except json.JSONDecodeError:
            payload = {}
        if exc.code == 401:
            raise RuntimeError("SubDL API Key 无效，请重新复制账户面板中的 Key。") from exc
        if exc.code in (403, 429):
            raise RuntimeError("SubDL 请求过于频繁或当日额度已用完，请稍后再试。") from exc
        message = _error_message(payload) if payload else detail
        raise RuntimeError(f"SubDL 请求失败（{exc.code}）：{message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 SubDL：{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("SubDL 返回的数据格式异常，请稍后再试。") from exc

    if payload.get("status") is False or payload.get("success") is False:
        message = _error_message(payload)
        if "can't find movie" in message.lower() or "not found" in message.lower():
            return payload
        raise RuntimeError(f"SubDL 请求失败：{message}")
    return payload


def validate_token(api_key: str) -> int | None:
    response = _request(api_key, "/me")
    for container in (response, response.get("data") or {}, response.get("account") or {}):
        for name in ("remaining_requests", "requests_remaining", "request_quota", "quota"):
            value = container.get(name)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                pass
    return None


LANGUAGE_CODES = {
    "zh-cn": "ZH",
    "zh-tw": "ZH_BG",
    "en": "EN",
    "ja": "JA",
    "ko": "KO",
    "es": "ES",
    "fr": "FR",
    "de": "DE",
    "ru": "RU",
}


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _language_matches(item: dict, requested: str) -> bool:
    actual = str(item.get("language") or "").strip().upper()
    expected = {code.strip().upper() for code in LANGUAGE_CODES.get(requested, "").split(",") if code.strip()}
    if actual and expected:
        return actual in expected
    label = " ".join(str(item.get(name) or "") for name in ("language", "lang", "name", "release_name")).lower()
    if requested == "zh-cn":
        return not any(token in label for token in ("traditional", "zh-hant", "cht", "big5", "繁体", "繁體"))
    if requested == "zh-tw":
        return not any(token in label for token in ("simplified", "zh-hans", "chs", "简体", "簡體"))
    return True


def _candidate_from_item(item: dict, index: int) -> SubtitleCandidate | None:
    url = str(item.get("url") or item.get("download_url") or "")
    file_id = str(item.get("n_id") or item.get("id") or item.get("file_n_id") or url or index)
    if not file_id:
        return None
    releases = item.get("releases") or []
    if isinstance(releases, list):
        releases_text = " / ".join(str(value) for value in releases[:3])
    else:
        releases_text = str(releases)
    file_name = str(item.get("name") or item.get("file_name") or Path(urllib.parse.urlparse(url).path).name or f"subtitle-{index}.zip")
    release = str(item.get("release_name") or releases_text or file_name)
    language = str(item.get("language") or item.get("lang") or "未知")
    downloads = _as_int(item.get("download_count") or item.get("downloads") or item.get("downloaded"))
    rating = _as_float(item.get("rating") or item.get("rate") or item.get("score"))
    hearing_impaired = bool(item.get("hi") or item.get("hearing_impaired"))
    trusted = rating >= 8 or downloads >= 1000
    return SubtitleCandidate(
        file_id=file_id,
        file_name=file_name,
        release=release,
        language=language,
        downloads=downloads,
        rating=rating,
        trusted=trusted,
        hearing_impaired=hearing_impaired,
        download_url=url,
    )


def _candidates_from_response(response: dict, language: str) -> list[SubtitleCandidate]:
    raw_items = response.get("subtitles") or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    results: list[SubtitleCandidate] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict) or not _language_matches(item, language):
            continue
        unpacked = item.get("unpack_files") or []
        if isinstance(unpacked, dict):
            unpacked = [unpacked]
        source_items = unpacked if unpacked else [item]
        for child_index, child in enumerate(source_items):
            if not isinstance(child, dict):
                continue
            merged = dict(item)
            merged.update(child)
            candidate = _candidate_from_item(merged, index * 100 + child_index)
            if candidate:
                results.append(candidate)
    unique: dict[str, SubtitleCandidate] = {}
    for item in results:
        unique[f"{item.file_id}|{item.download_url}"] = item
    return list(unique.values())


def _search_once(api_key: str, params: dict, language: str) -> tuple[list[SubtitleCandidate], int]:
    response = _request(api_key, "/subtitles", params=params)
    candidates = _candidates_from_response(response, language)
    return candidates, len(response.get("subtitles") or [])


def _safe_search_text(value: str) -> str:
    # SubDL rejects punctuation such as apostrophes and vertical bars in title search.
    cleaned = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).replace("_", " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def _one_edit_apart(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    index = 0
    while index < len(left) and left[index] == right[index]:
        index += 1
    return left[index:] == right[index + 1:]


def _movie_title_kind(identity: MediaIdentity, item: dict) -> str:
    expected = _safe_search_text(identity.original_title or identity.title).casefold()
    if not expected:
        return ""
    names = [
        _safe_search_text(str(item.get(name) or "")).casefold()
        for name in ("name", "original_name", "slug")
    ]
    if expected in names:
        return "exact"
    expected_words = expected.split()
    for name in names:
        words = name.split()
        if len(words) != len(expected_words) or not words:
            continue
        differences = [
            (left, right)
            for left, right in zip(expected_words, words)
            if left != right
        ]
        if len(differences) == 1 and _one_edit_apart(*differences[0]):
            return "typo"
    return ""


def _movie_year_kind(identity: MediaIdentity, item: dict) -> str:
    try:
        expected = int(identity.year) if identity.year else 0
        actual = int(item.get("year") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if not expected or not actual:
        return "unknown"
    distance = abs(expected - actual)
    if distance == 0:
        return "exact"
    if distance == 1:
        return "adjacent"
    return ""


def _resolved_movie_name(identity: MediaIdentity, movie: dict) -> str:
    names = [str(movie.get(name) or "") for name in ("original_name", "name")]
    if not needs_canonical_title(identity.original_title or identity.title):
        # A matching public name must not be replaced by an unrelated provider
        # original-name suffix after the identity gate has already accepted it.
        for kind in ("exact", "typo"):
            for name in names:
                if name and _movie_title_kind(identity, {"name": name}) == kind:
                    return name
    return next((name for name in names if name and not needs_canonical_title(name)),
                next((name for name in names if name), identity.original_title or identity.title))


def _choose_movie_without_score(identity: MediaIdentity, items: list[dict]) -> dict | None:
    # Factual groups replace the former weighted score.  Provider order is
    # preserved inside each group.
    localized = needs_canonical_title(identity.title)
    native_query = needs_canonical_title(identity.original_title or identity.title)
    eligible = [item for item in items if not sequel_title_conflict(
        identity.title, tuple(str(item.get(name) or "") for name in ("name", "original_name")),
    )]
    if native_query:
        # Native-name catalogue queries may return only an English name. The
        # original year and explicit installment must agree, with one unique
        # provider film ID; do not simply take the site's first result.
        expected_sequel = sequel_number(identity.title)
        native_matches = [item for item in eligible
                          if identity.year and _movie_year_kind(identity, item) == "exact"
                          and item.get("sd_id")
                          and (not expected_sequel or expected_sequel in {
                              sequel_number(str(item.get(name) or ""))
                              for name in ("name", "original_name")})]
        ids = {str(item["sd_id"]) for item in native_matches}
        return native_matches[0] if len(ids) == 1 else None
    groups = (
        ("exact", {"exact"}),
        ("exact", {"adjacent"}),
        ("typo", {"exact"}),
        ("typo", {"adjacent"}),
    )
    if not identity.year and not localized:
        groups += (("exact", {"unknown"}), ("typo", {"unknown"}))
    for title_kind, year_kinds in groups:
        if localized and "adjacent" in year_kinds:
            continue
        for item in eligible:
            if (
                _movie_title_kind(identity, item) == title_kind
                and _movie_year_kind(identity, item) in year_kinds
            ):
                return item
    return None


def _movie_queries(identity: MediaIdentity) -> list[str]:
    exact = _safe_search_text(identity.original_title or identity.title)
    queries = [exact] if exact else []
    ordered = [
        token for token in _title_sequence(exact)
        if token not in TITLE_STOPWORDS and not token.isdigit()
    ]
    if ordered:
        broad = " ".join(ordered[:2])
        if identity.year:
            broad = f"{broad} {identity.year}"
        if broad and broad.lower() != exact.lower():
            queries.append(broad)
    if len(ordered) >= 2:
        # A provider cannot fuzzy-match a misspelled title if the bad token is
        # present in every query.  Retry by omitting one content token while
        # retaining the remaining title context and release year.  The result
        # still has to pass the normal title/year identity score below.
        for omitted_index in range(len(ordered)):
            fallback_tokens = [
                token for index, token in enumerate(ordered)
                if index != omitted_index
            ]
            fallback = " ".join(fallback_tokens)
            if identity.year:
                fallback = f"{fallback} {identity.year}"
            fallback = fallback.strip()
            if fallback and all(fallback.casefold() != item.casefold() for item in queries):
                queries.append(fallback)
    return queries


def _resolve_movie(api_key: str, identity: MediaIdentity) -> dict | None:
    candidates: dict[str, dict] = {}
    diagnostics = _MOVIE_LOOKUP_DIAGNOSTICS.get()
    for query in _movie_queries(identity):
        response = _request_v2(
            api_key,
            "/movies/search",
            params={"q": query, "type": "movie", "limit": 10},
        )
        for item in response.get("results") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("sd_id") or item.get("imdb_id") or item.get("tmdb_id") or "")
            if key:
                candidates[key] = item
        if candidates:
            selected = _choose_movie_without_score(identity, list(candidates.values()))
            if diagnostics is not None:
                matched_titles = [item for item in candidates.values() if _movie_title_kind(identity, item)]
                definite_conflicts = [item for item in matched_titles
                                      if _movie_year_kind(identity, item) == ""
                                      or (needs_canonical_title(identity.title)
                                          and _movie_year_kind(identity, item) == "adjacent")
                                      or sequel_title_conflict(identity.title, tuple(
                                          str(item.get(name) or "") for name in ("name", "original_name")))]
                diagnostics.status = "resolved" if selected else "identity-conflict" if definite_conflicts else "unresolved"
                diagnostics.reason = ("已确认站点片名、年份和影片ID" if selected else
                                      "站点片名候选存在年份或续集编号冲突" if definite_conflicts else
                                      "未取得片名、年份及唯一影片ID一致的记录")
                diagnostics.attempts.append(f"{query}：{diagnostics.reason}")
            if selected is not None:
                return selected
            if (needs_canonical_title(identity.title) and diagnostics is not None
                    and diagnostics.status == "identity-conflict"):
                return None
        elif diagnostics is not None:
            diagnostics.status = "unresolved"
            diagnostics.reason = "站点未返回可核验的影片条目"
            diagnostics.attempts.append(f"{query}：{diagnostics.reason}")
    return _choose_movie_without_score(identity, list(candidates.values()))


def search(api_key: str, video_path: str, language: str) -> tuple[MediaIdentity, list[SubtitleCandidate], SearchMeta]:
    if conflict := filename_container_identity_conflict(video_path):
        raise RuntimeError(conflict)
    base_identity = identify_media(video_path)
    identity = base_identity
    localized = needs_canonical_title(identity.title)
    config = settings_path() if localized else None
    source = canonical_title_source(identity.title, identity.year, config) if localized else ""
    canonical = resolve_canonical_title(identity.title, identity.year, config) if localized else ""
    attempts = ([f"英文片名候选来源={source}" + (f"：{canonical}" if canonical else "；无可用候选")]
                if localized else [])
    fallback_reason = ""
    proposals = ([replace(base_identity, original_title=canonical)] if canonical else [])
    proposals.append(replace(base_identity))
    attempted_english: list[str] = []

    def recover_after(index: int) -> None:
        if not attempted_english or len(attempted_english) >= 2:
            return
        recovered = resolve_canonical_title(
            base_identity.title, base_identity.year, config, excluded_titles=tuple(attempted_english),
        )
        if (recovered and recovered.casefold() not in {value.casefold() for value in attempted_english}
                and not needs_canonical_title(recovered)):
            proposals.insert(index + 1, replace(base_identity, original_title=recovered))
            attempts.append(f"保留原年份和续集编号，恢复英文片名候选：{recovered}；仍需站点确认")

    movie = None
    lookup = _MovieLookupDiagnostics()
    for index, proposal in enumerate(proposals):
        identity = proposal
        if localized and not needs_canonical_title(proposal.original_title):
            attempted_english.append(proposal.original_title)
        lookup = _MovieLookupDiagnostics()
        if localized and sequel_title_conflict(base_identity.title, (proposal.original_title,)):
            lookup.status = "identity-conflict"
            lookup.reason = "英文片名候选与原文件的续集编号冲突"
        else:
            token = _MOVIE_LOOKUP_DIAGNOSTICS.set(lookup)
            try:
                movie = _resolve_movie(api_key, proposal)
            finally:
                _MOVIE_LOOKUP_DIAGNOSTICS.reset(token)
        if not lookup.status:
            lookup.status = "resolved" if movie else "unresolved"
            lookup.reason = "已确认站点影片身份" if movie else "未确认站点影片身份"
        attempts.extend(lookup.attempts or [f"{proposal.original_title}：{lookup.reason}"])
        if movie and movie.get("sd_id"):
            break
        movie = None
        if localized and not needs_canonical_title(proposal.original_title):
            if lookup.status == "identity-conflict":
                reject_canonical_title(base_identity.title, base_identity.year, proposal.original_title, lookup.reason, config)
            fallback_reason = (f"{source}英文片名候选未确认：{lookup.reason}；"
                               f"保留原年份{base_identity.year or '未知'}补查其他英文名及原片名“{base_identity.title}”")
            attempts.append(fallback_reason)
            recover_after(index)
    language_code = LANGUAGE_CODES.get(language, "ZH")
    if not movie:
        return base_identity, [], SearchMeta(
            query_mode="unresolved-title" if localized else "title-year",
            feature_lookup_status=lookup.status, feature_lookup_reason=lookup.reason,
            feature_lookup_attempts=tuple(attempts), fallback_reason=fallback_reason,
        )

    resolved_name = _resolved_movie_name(identity, movie)
    resolved_identity = MediaIdentity(
        title=identity.title,
        original_title=resolved_name,
        year=identity.year,
        imdb_id=str(movie.get("imdb_id") or "").removeprefix("tt"),
        tmdb_id=str(movie.get("tmdb_id") or ""),
        edition=identity.edition,
        specification=identity.specification,
        provider_id=str(movie.get("sd_id") or ""),
    )
    response = _request_v2(
        api_key,
        "/subtitles/search",
        params={
            "sd_id": movie.get("sd_id"),
            "languages": language_code,
            "subs_per_page": 30,
            "unpack": 1,
        },
    )
    results = _candidates_from_response(response, language)
    resolved_year = str(movie.get("year") or identity.year or "")
    if results and localized:
        remember_canonical_title(base_identity.title, base_identity.year, resolved_name,
                                 config, confirmed_year=str(movie.get("year") or ""),
                                 confirmed_identity=f"subdl:{movie['sd_id']}")
    for item in results:
        item.feature_title = resolved_name
        item.feature_year = resolved_year
        item.identity_verified = True
        if resolved_identity.imdb_id:
            item.identity_key = f"imdb:{resolved_identity.imdb_id}"
        elif resolved_identity.tmdb_id:
            item.identity_key = f"tmdb:{resolved_identity.tmdb_id}"
        else:
            item.identity_key = f"subdl:{resolved_identity.provider_id}"
        item.recommendation = "待内容验证"
        item.match_reason = "站点影片条目命中；最终以下载后的正文验证为准"
    total = len(response.get("subtitles") or [])

    return resolved_identity, results, SearchMeta(
        total_count=total,
        loaded_count=len(results),
        query_mode="movie-id",
        truncated=total >= 30,
        feature_lookup_status=lookup.status, feature_lookup_reason=lookup.reason,
        feature_lookup_attempts=tuple(attempts), fallback_reason=fallback_reason,
    )


def _extract_download(data: bytes, file_name: str, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [name for name in archive.namelist() if Path(name).suffix.lower() in SUBTITLE_EXTENSIONS]
            if not members:
                raise RuntimeError("下载包中没有可用的文本字幕文件。")
            chosen = min(members, key=lambda name: (Path(name).suffix.lower() != ".srt", len(name)))
            output = destination / Path(chosen).name
            output.write_bytes(archive.read(chosen))
            return output
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if data.startswith(b"Rar!\x1a\x07"):
        raise RuntimeError("SubDL 返回了 RAR 压缩包，当前无法自动解压，请换一个候选字幕。")
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS:
        suffix = ".srt"
    safe_name = Path(file_name).name if Path(file_name).suffix.lower() in SUBTITLE_EXTENSIONS else f"subdl-subtitle{suffix}"
    output = destination / safe_name
    output.write_bytes(data)
    return output


def download(api_key: str, candidate: SubtitleCandidate, destination: str) -> Path:
    link = candidate.download_url.strip()
    if not link:
        raise RuntimeError("SubDL 没有返回可用的字幕下载地址，请换一个候选字幕。")
    if link.startswith("/"):
        link = DOWNLOAD_BASE + link
    request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise RuntimeError("SubDL 下载次数受限，请稍后再试。") from exc
        raise RuntimeError(f"SubDL 字幕下载失败（{exc.code}）。") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"SubDL 字幕文件下载失败：{exc.reason}") from exc
    return _extract_download(data, candidate.file_name, Path(destination))
