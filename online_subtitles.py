# -*- coding: utf-8 -*-
from __future__ import annotations

import gzip
import html
import http.client
import io
import json
import os
import re
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from subtitle_candidate_priority import author_name
from work_identity_resolver import WorkProfile, read_cached_resolution, resolve as resolve_work_identity

from media_title_resolver import (
    TITLE_SETTINGS_LOCK,
    canonical_title_source,
    connector_query_proposals,
    controlled_connector_alias,
    needs_canonical_title,
    reject_canonical_title,
    remember_canonical_title,
    resolve_canonical_title,
    sequel_number,
    sequel_title_conflict,
)
from subtitle_identity_guard import (
    clean_video_title,
    catalogue_title_aliases,
    controlled_film_title_alias,
    container_title,
    container_title_can_replace_filename,
    edition_label,
    filename_container_identity_conflict,
    media_specification,
    parse_filename_and_container,
)


API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "SubFlow/2.0.68"
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
OPEN_TIMEOUT_SECONDS = 15


def _create_ipv4_connection(
    address,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
    **_kwargs,
):
    host, port = address
    last_error: OSError | None = None
    for family, socket_type, protocol, _canonical_name, socket_address in socket.getaddrinfo(
        host,
        port,
        socket.AF_INET,
        socket.SOCK_STREAM,
    ):
        connection = None
        try:
            connection = socket.socket(family, socket_type, protocol)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as exc:
            last_error = exc
            if connection is not None:
                connection.close()
    if last_error is not None:
        raise last_error
    raise OSError(f"无法解析 IPv4 地址：{host}")


class _IPv4HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_ipv4_connection


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_ipv4_connection


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_IPv4HTTPConnection, request)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(
            _IPv4HTTPSConnection,
            request,
            context=self._context,
        )


_IPV4_OPENER = urllib.request.build_opener(_IPv4HTTPHandler(), _IPv4HTTPSHandler())


@dataclass
class MediaIdentity:
    title: str
    original_title: str = ""
    year: str = ""
    feature_id: str = ""
    imdb_id: str = ""
    tmdb_id: str = ""
    edition: str = ""
    specification: str = ""


@dataclass
class SubtitleCandidate:
    file_id: int
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
    feature_title: str = ""
    feature_year: str = ""
    feature_id: str = ""
    feature_imdb_id: str = ""
    feature_tmdb_id: str = ""
    identity_verified: bool = False
    identity_key: str = ""
    feature_type: str = ""
    feature_season_number: str = ""
    feature_episode_number: str = ""
    feature_parent_imdb_id: str = ""
    feature_parent_tmdb_id: str = ""
    author: str = ""
    author_role: str = ""
    priority_reason: str = ""
    identity_admission_state: str = ""
    identity_admission_reason: str = ""
    identity_admission_evidence: tuple[dict, ...] = ()


@dataclass
class SearchMeta:
    total_count: int = 0
    loaded_count: int = 0
    query_mode: str = "title"
    moviehash: str = ""
    truncated: bool = False
    feature_lookup_status: str = ""
    feature_lookup_reason: str = ""
    feature_lookup_attempts: tuple[str, ...] = ()
    fallback_reason: str = ""
    identity_resolution_status: str = ""
    identity_resolution_reason: str = ""
    identity_resolution_requests: int = 0
    identity_resolution_seconds: float = 0.0
    identity_resolution_cache_hit: bool = False
    identity_resolution_evidence: tuple[dict, ...] = ()


class SubtitleAuthenticationError(RuntimeError):
    """The provider rejected the API credential; further retries cannot help."""


class SubtitleQuotaError(RuntimeError):
    """The provider request/download allowance is exhausted."""


@dataclass
class _FeatureLookupDiagnostics:
    status: str = ""
    reason: str = ""
    attempts: list[str] = field(default_factory=list)
    proposal_conflict: bool = False
    profiles: list[WorkProfile] = field(default_factory=list)


# Batch lookups may run concurrently. Keep diagnostics attached to this search
# without changing the public resolver's return type or its call contract.
_FEATURE_LOOKUP_DIAGNOSTICS: ContextVar[_FeatureLookupDiagnostics | None] = ContextVar(
    "opensubtitles_feature_lookup_diagnostics", default=None,
)
_WORK_IDENTITY_CONTEXT: ContextVar[dict | None] = ContextVar('opensubtitles_work_identity_context', default=None)


def _provider_id_key(value: object, *, imdb: bool = False) -> str:
    value = str(value or "").strip()
    if imdb:
        value = re.sub(r"^tt", "", value, flags=re.I)
    # Placeholder strings (for example "unknown") are not shared film IDs.
    # Avoid numeric conversion so arbitrarily long provider values cannot raise.
    return value.lstrip("0") if re.fullmatch(r"[0-9]+", value) else ""


def opensubtitles_movie_hash(video_path: str) -> str:
    """Calculate the 64-bit OpenSubtitles hash without reading the full movie."""
    path = Path(video_path)
    size = path.stat().st_size
    chunk_size = 64 * 1024
    if size < chunk_size * 2:
        return ""
    checksum = size
    with path.open("rb") as handle:
        first = handle.read(chunk_size)
        handle.seek(-chunk_size, os.SEEK_END)
        last = handle.read(chunk_size)
    for (value,) in struct.iter_unpack("<Q", first + last):
        checksum = (checksum + value) & 0xFFFFFFFFFFFFFFFF
    return f"{checksum:016x}"


TITLE_CONNECTORS = {"AND"}
LEADING_ARTICLES = {"THE", "A", "AN"}
ROMAN_NUMERALS = {
    "I": "1",
    "II": "2",
    "III": "3",
    "IV": "4",
    "V": "5",
    "VI": "6",
    "VII": "7",
    "VIII": "8",
    "IX": "9",
    "X": "10",
}
IDENTITY_DESCRIPTORS = {
    "live action", "animated", "animation", "theatrical",
    "theatrical cut", "extended", "extended edition",
    "directors cut", "director s cut", "director cut",
}


def _strip_identity_descriptors(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"^\s*(?:19|20)\d{2}\s+[-–—]\s+", "", value)
    while True:
        match = re.search(r"\s*\(([^()]*)\)\s*$", value)
        if not match:
            break
        descriptor = re.sub(r"[^a-z0-9]+", " ", match.group(1).casefold()).strip()
        if descriptor not in IDENTITY_DESCRIPTORS:
            break
        value = value[:match.start()]
    return value.rstrip(" ([{-–—:")


def _identity_title_tokens(value: str) -> tuple[str, ...]:
    value = _strip_identity_descriptors(value).replace("’", "'").replace("'", "")
    # OpenSubtitles movie_name may prefix its year. Numeric film titles (1917,
    # 2001: A Space Odyssey, Blade Runner 2049) are part of the identity.
    value = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$", "", value)
    raw_tokens = re.findall(r"[A-Z0-9]+", value.upper())
    tokens = [
        (token if token == "X" and 0 < index < len(raw_tokens) - 1
         and raw_tokens[index + 1].isalpha()
         and raw_tokens[index + 1] not in {"BLURAY", "BDRIP", "REMUX", "WEB", "WEBRIP", "HDR"}
         and raw_tokens[index - 1] not in {"EPISODE", "EP", "PART", "CHAPTER"}
         else ROMAN_NUMERALS.get(token, token))
        for index, token in enumerate(raw_tokens) if token not in TITLE_CONNECTORS
    ]
    while tokens and tokens[0] in LEADING_ARTICLES:
        tokens.pop(0)
    return tuple(tokens)


def _edit_distance_one(left: str, right: str) -> bool:
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, 1):
        current = [row]
        for column, right_char in enumerate(right, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1] == 1


def _titles_typo_equivalent(left: str, right: str) -> bool:
    left_tokens = _identity_title_tokens(left)
    right_tokens = _identity_title_tokens(right)
    if len(left_tokens) != len(right_tokens) or not left_tokens:
        return False
    differences = [(a, b) for a, b in zip(left_tokens, right_tokens) if a != b]
    return (len(differences) == 1 and not any(token.isdigit() for token in differences[0])
            and _edit_distance_one(*differences[0]))


def _titles_equivalent(left: str, right: str) -> bool:
    if needs_canonical_title(left) and needs_canonical_title(right):
        normalize = lambda value: re.sub(r"[^\w]+", "", value, flags=re.UNICODE).casefold()
        return bool(normalize(left) and normalize(left) == normalize(right))
    left_tokens = _identity_title_tokens(left)
    right_tokens = _identity_title_tokens(right)
    return bool(left_tokens and left_tokens == right_tokens)


def _titles_safe_alias(left: str, right: str) -> bool:
    left_tokens = _identity_title_tokens(left)
    right_tokens = _identity_title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens == right_tokens:
        return True
    if _titles_typo_equivalent(left, right):
        return True
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    return len(shorter) < len(longer) and longer[:len(shorter)] == shorter


def _title_query_variants(value: str) -> list[str]:
    title = re.sub(r"\s+", " ", value).strip()
    if not title:
        return []
    variants = [title]
    for variant in (
        re.sub(r"\s+and\s+", " & ", title, flags=re.I),
        re.sub(r"\s*&\s*", " and ", title),
    ):
        variant = re.sub(r"\s+", " ", variant).strip()
        if variant and all(variant.casefold() != item.casefold() for item in variants):
            variants.append(variant)
    return variants


def _candidate_matches_identity(candidate: SubtitleCandidate, identity: MediaIdentity) -> bool:
    expected_titles = {
        value.strip()
        for value in (identity.title, identity.original_title)
        if value and value.strip()
    }
    if candidate.feature_title:
        return any(
            _titles_equivalent(expected, candidate.feature_title)
            or _titles_typo_equivalent(expected, candidate.feature_title)
            or (getattr(candidate, 'feature_year', '') == identity.year
                and _candidate_matches_strong_identity(candidate, identity)
                and controlled_film_title_alias(expected, candidate.feature_title, identity.year))
            for expected in expected_titles
        )

    candidate_tokens = set(_identity_title_tokens(f"{candidate.release} {candidate.file_name}"))
    return any(
        bool(expected_tokens := _identity_title_tokens(expected))
        and set(expected_tokens).issubset(candidate_tokens)
        for expected in expected_titles
    )


def _title_year_identity_key(candidate: SubtitleCandidate, identity: MediaIdentity) -> str:
    """Build a safe fallback identity for broad title/year API results."""
    if not identity.year or not candidate.feature_year:
        return ""
    if str(identity.year).strip() != str(candidate.feature_year).strip():
        return ""
    if not _candidate_matches_identity(candidate, identity):
        return ""
    title = identity.original_title or identity.title
    normalized = "-".join(_identity_title_tokens(title)).lower()
    return f"title-year:{normalized}:{identity.year}" if normalized else ""


def _candidate_matches_strong_identity(candidate: SubtitleCandidate, identity: MediaIdentity) -> bool:
    pairs = (
        (_provider_id_key(getattr(identity, "imdb_id", ""), imdb=True),
         _provider_id_key(getattr(candidate, "feature_imdb_id", ""), imdb=True)),
        (_provider_id_key(getattr(identity, "tmdb_id", "")),
         _provider_id_key(getattr(candidate, "feature_tmdb_id", ""))),
    )
    comparable = [(expected, actual) for expected, actual in pairs if expected and actual]
    return bool(comparable) and all(expected == actual for expected, actual in comparable)


def _candidate_identity_metadata_conflict(candidate: object, identity: object) -> str:
    """Reject explicit contradictions and anonymous broad-search placeholders.

    Ordinary named aliases without IDs remain eligible for content validation.
    A hash or a matching strong ID can establish an anonymous catalogue row;
    the unqualified identity_verified flag cannot.
    """
    for name, imdb in (("imdb_id", True), ("tmdb_id", False)):
        expected = _provider_id_key(getattr(identity, name, ""), imdb=imdb)
        actual = _provider_id_key(getattr(candidate, "feature_" + name, ""), imdb=imdb)
        if expected and actual and expected != actual:
            return f"候选 {name.upper()} 与已确认影片身份冲突"
    feature_type = re.sub(r"[^a-z]", "", str(getattr(candidate, "feature_type", "")).lower())
    episodic = any(_provider_id_key(getattr(candidate, name, ""), imdb="imdb" in name)
                   for name in ("feature_season_number", "feature_episode_number",
                                "feature_parent_imdb_id", "feature_parent_tmdb_id"))
    if feature_type in {"episode", "tvepisode", "tvseries", "series", "tvshow"} or episodic:
        return "候选属于剧集条目，与本片电影身份不符"
    title = str(getattr(candidate, "feature_title", "") or "")
    tokens = _identity_title_tokens(title)
    placeholder = not tokens or " ".join(tokens) in {
        "EMPTY MOVIE", "EMPTY MOVIE SUBSCENE", "UNKNOWN", "UNKNOWN MOVIE",
        "UNTITLED", "UNTITLED MOVIE", "NONE", "NULL", "N A", "UNAVAILABLE",
    }
    if not placeholder:
        return ""
    if (getattr(candidate, "moviehash_match", False)
            or _candidate_matches_strong_identity(candidate, identity)):
        return ""
    # A complete, contiguous title in a release can support a genuinely missing
    # catalogue title. A placeholder title is affirmative bad metadata, so it
    # additionally needs its declared year to agree.
    year = str(getattr(identity, "year", "") or "")
    actual_year = str(getattr(candidate, "feature_year", "") or "")
    if title and (not year or actual_year != year):
        return "候选站点影片身份为占位记录，缺少可核对的年份或身份"
    titles = (getattr(identity, "title", ""), getattr(identity, "original_title", ""))
    for expected_title in titles:
        expected = _identity_title_tokens(str(expected_title or ""))
        if not expected:
            continue
        for name in ("release", "file_name"):
            release = _identity_title_tokens(str(getattr(candidate, name, "") or ""))
            if any(release[index:index + len(expected)] == expected
                   for index in range(len(release) - len(expected) + 1)):
                return ""
    return "候选缺少可确认的影片身份，站点占位信息及发布名均不能证明同片"


def settings_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubFlow"
    root.mkdir(parents=True, exist_ok=True)
    return root / "online-subtitles.json"


def load_settings() -> dict:
    with TITLE_SETTINGS_LOCK:
        try:
            return json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


def save_settings(api_key: str) -> None:
    with TITLE_SETTINGS_LOCK:
        settings = load_settings()
        settings["opensubtitles_api_key"] = api_key.strip()
        settings_path().write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


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
        filename, internal = parse_filename_and_container(video_path, embedded_title)
        title, year = filename.title, filename.year
        resolved_title, embedded_year = internal.title, internal.year
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


def _episode_number(value: str) -> str:
    match = re.search(
        r"\b(?:episode|ep\.?)[ .:_-]*(viii|vii|vi|iv|ix|iii|ii|i|v|[1-9])\b",
        value, flags=re.I,
    )
    if not match:
        return ""
    return ROMAN_NUMERALS.get(match.group(1).upper(), match.group(1))


def _feature_title_variants(identity: MediaIdentity, *, strict: bool = False) -> list[str]:
    titles = list(dict.fromkeys(value for value in (identity.original_title, identity.title) if value))
    for title in list(titles):
        titles.extend(catalogue_title_aliases(title, identity.year))
    # Some sites file the 1977 film under its original title, "Star Wars".
    # These aliases still require the exact film year in strict ID resolution.
    if strict:
        for title in list(titles):
            match = re.fullmatch(
                r"star\s+wars\s*[:：-]?\s*(?:episode|ep\.?)\s+"
                r"(?:viii|vii|vi|iv|ix|iii|ii|i|v|[1-9])\s*[-:：]\s*(.+)",
                title, flags=re.I,
            )
            if match:
                titles.append(match.group(1).strip())
                if identity.year == "1977" and _episode_number(title) == "4":
                    titles.append("Star Wars")
    return list(dict.fromkeys(titles))


def _feature_identity_from_response(
    response: dict, identity: MediaIdentity, *, strict: bool = False,
    diagnostics: _FeatureLookupDiagnostics | None = None,
) -> MediaIdentity | None:
    matches: list[tuple[MediaIdentity, set[tuple[str, str]]]] = []
    exact_year_matches: list[tuple[MediaIdentity, set[tuple[str, str]]]] = []
    observed: list[tuple[set[tuple[str, str]], str, set[str]]] = []
    rejected: dict[str, int] = {}
    expected_titles = _feature_title_variants(identity, strict=strict)
    expected_episode = _episode_number(identity.original_title or identity.title)
    localized = needs_canonical_title(identity.title)
    localized_query = needs_canonical_title(identity.original_title or identity.title)
    entries = response.get("data") or []

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    def finish(status: str, reason: str) -> None:
        if diagnostics is not None:
            diagnostics.status = status
            diagnostics.reason = reason

    if not isinstance(entries, (list, tuple)):
        finish("invalid-response", "影片条目响应格式无效")
        return None
    for item in entries:
        attrs = item.get("attributes") or {}
        feature_type = str(
            attrs.get("feature_type") or attrs.get("type") or item.get("type") or ""
        ).lower()
        if feature_type and feature_type not in {"movie", "feature"}:
            reject("不是电影条目")
            continue
        title = str(attrs.get("title") or attrs.get("movie_name") or attrs.get("name") or "")
        original_title = str(attrs.get("original_title") or title)
        candidate_titles = (title, original_title)
        proposal_matches = any(
            expected and value and _titles_equivalent(expected, value)
            for expected in expected_titles for value in candidate_titles
        )
        imdb_id = re.sub(r"^tt", "", str(attrs.get("imdb_id") or "").strip(), flags=re.I)
        tmdb_id = str(attrs.get("tmdb_id") or "").strip()
        year = str(attrs.get("year") or "").strip()
        if diagnostics is not None:
            aliases = attrs.get('title_aka') or []
            aliases = [aliases] if isinstance(aliases, str) else aliases
            aliases = tuple(value for value in aliases if isinstance(value, str)) if isinstance(aliases, (list, tuple)) else ()
            diagnostics.profiles.append(WorkProfile(
                original_title or title, year, imdb_id, tmdb_id, 'opensubtitles-features',
                feature_type, tuple(dict.fromkeys((title, original_title, *aliases)))))
        id_keys = set()
        if imdb_key := _provider_id_key(imdb_id, imdb=True):
            id_keys.add(("imdb", imdb_key))
        else:
            imdb_id = ""
        if tmdb_key := _provider_id_key(tmdb_id):
            id_keys.add(("tmdb", tmdb_key))
        else:
            tmdb_id = ""
        episodes = {number for value in candidate_titles if (number := _episode_number(value))}
        if localized:
            episodes.update(number for value in candidate_titles if (number := sequel_number(value)))
        observed.append((id_keys, year, episodes))
        if localized and sequel_title_conflict(identity.title, candidate_titles):
            reject("续集编号冲突")
            if diagnostics is not None and proposal_matches:
                diagnostics.proposal_conflict = True
            continue
        if strict and expected_episode and any(
            number and number != expected_episode
            for number in (_episode_number(value) for value in candidate_titles)
        ):
            reject("集数冲突")
            continue
        matched_title = next(
            (
                candidate_title
                for expected in expected_titles
                for candidate_title in candidate_titles
                if expected and candidate_title
                and _titles_equivalent(expected, candidate_title)
            ),
            "",
        )
        if not matched_title:
            matched_title = next(
                (
                    candidate_title
                    for expected in expected_titles
                    for candidate_title in candidate_titles
                    if expected and candidate_title
                    and _titles_typo_equivalent(expected, candidate_title)
                ),
                "",
            )
        connector_match = False
        if not matched_title and identity.year and year == identity.year and id_keys:
            matched_title = next((candidate_title for expected in expected_titles
                                  for candidate_title in candidate_titles
                                  if feature_type in {"movie", "feature"}
                                  and controlled_connector_alias(expected, candidate_title)), "")
            connector_match = bool(matched_title)
        if not matched_title:
            # A native-name query may return only the catalogue's English name.
            # Use its exact original year, explicit installment (when present),
            # a strong ID and uniqueness below; never a free model guess.
            expected_sequel = sequel_number(identity.title)
            actual_sequels = {sequel_number(value) for value in candidate_titles}
            if (localized_query and identity.year and year == identity.year and id_keys
                    and (not expected_sequel or expected_sequel in actual_sequels)):
                matched_title = original_title or title
        if not matched_title:
            reject("片名不符")
            continue
        if (strict or localized) and not (imdb_id or tmdb_id):
            reject("缺少 IMDb/TMDB ID")
            continue
        if (strict or localized) and identity.year and year != identity.year:
            reject("年份冲突" if year else "缺少年份")
            if diagnostics is not None and proposal_matches and year:
                diagnostics.proposal_conflict = True
            continue
        if identity.year and year:
            try:
                if abs(int(identity.year) - int(year)) > 1:
                    reject("年份冲突")
                    continue
            except ValueError:
                if identity.year != year:
                    reject("年份格式或内容不符")
                    continue
        resolved_original = ((identity.original_title or identity.title)
                             if strict and not localized_query and not connector_match
                             else matched_title)
        if localized and needs_canonical_title(resolved_original):
            resolved_original = next((value for value in (original_title, title)
                                      if not needs_canonical_title(value) and re.search(r"[A-Za-z]{2,}", value)),
                                     resolved_original)
        matches.append((MediaIdentity(
            title=identity.title,
            # Keep the full local title (including its episode number) so a
            # shorter site alias cannot disable later candidate conflict gates.
            original_title=resolved_original,
            year=identity.year or year,
            feature_id=str(attrs.get("feature_id") or item.get("id") or ""),
            edition=identity.edition,
            specification=identity.specification,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
        ), id_keys))
        if identity.year and year == identity.year:
            exact_year_matches.append(matches[-1])

    # A same-name catalogue row without a year is weaker than a dated match.
    # Never let it force a broad search; multiple dated IDs still remain ambiguous.
    if exact_year_matches:
        matches = exact_year_matches
    if not matches:
        details = "；".join(f"{reason}{count}条" for reason, count in rejected.items())
        status = (
            "missing-id" if "缺少 IMDb/TMDB ID" in rejected
            else "identity-conflict" if any("年份冲突" in reason or "集数" in reason or "续集" in reason for reason in rejected)
            else "no-match"
        )
        finish(status, f"收到{len(entries)}条影片记录，无可锁定身份" + (f"（{details}）" if details else ""))
        return None

    # Providers can return several catalogue rows for the very same film.
    # Merge only shared strong IDs; equal titles alone must never merge films.
    groups: list[list[tuple[MediaIdentity, set[tuple[str, str]]]]] = []
    for match in matches:
        linked = [index for index, group in enumerate(groups)
                  if match[1] and any(match[1] & member[1] for member in group)]
        if not linked:
            groups.append([match])
            continue
        merged = [match]
        for index in reversed(linked):
            merged.extend(groups.pop(index))
        groups.append(merged)
    if len(groups) != 1:
        finish("ambiguous", f"片名筛选后仍有{len(groups)}个不同影片身份，未锁定")
        return None

    group = groups[0]
    keys = set().union(*(member[1] for member in group))
    related = [profile for profile in observed if keys and profile[0] & keys]
    related_keys = set().union(*(profile[0] for profile in related)) if related else keys
    years = {profile[1] for profile in related if profile[1]}
    episodes = set().union(*(profile[2] for profile in related)) if related else set()
    # Conflicting duplicate rows are not evidence of a unique film, even if one
    # row happened to pass the earlier title/year filter.
    conflicts = []
    if any(len({value for kind, value in related_keys if kind == source}) > 1
           for source in ("imdb", "tmdb")):
        conflicts.append("ID映射冲突")
    if len(years) > 1:
        conflicts.append("年份冲突")
    if len(episodes) > 1 or (strict and expected_episode and episodes and episodes != {expected_episode}):
        conflicts.append("集数冲突")
    if conflicts:
        finish("identity-conflict", "同ID影片记录存在" + "、".join(conflicts) + "，未合并或锁定")
        return None

    # Prefer the most informative row after all members proved consistent.
    resolved = max(group, key=lambda member: len(member[1]))[0]
    status = "resolved" if keys else "resolved-without-id"
    finish(status, f"{len(matches)}条匹配记录归并为1个影片身份"
           + ("（同ID重复条目已合并）" if len(matches) > 1 else "")
           + ("；缺少 IMDb/TMDB ID" if not keys else ""))
    return resolved


def _resolve_feature_identity(
    api_key: str, identity: MediaIdentity, *, strict: bool = False,
) -> MediaIdentity | None:
    diagnostics = _FEATURE_LOOKUP_DIAGNOSTICS.get()
    title = identity.original_title or identity.title
    if not title:
        return None
    queries = (_feature_title_variants(identity, strict=True) if strict
               else _title_query_variants(title))
    attempts = [(query, exact) for exact in ((True,) if strict else (True, False)) for query in queries]
    # Only a title with a substantial unchanged subtitle can generate this one
    # retry. The response still needs exact year, strong IDs and unique identity.
    if identity.year:
        attempts.extend((query, False) for query in connector_query_proposals(title)
                        if query.casefold() not in {value.casefold() for value in queries})
    for query, exact in attempts:
        params = {"query": query, "type": "movie"}
        if exact:
            params["query_match"] = "exact"
        try:
            response = _request(api_key, "/features", params=params)
        except (SubtitleAuthenticationError, SubtitleQuotaError) as exc:
            if diagnostics is not None:
                diagnostics.status = "authentication-error" if isinstance(exc, SubtitleAuthenticationError) else "quota-error"
                diagnostics.reason = str(exc)
                diagnostics.attempts.append(f"{query}：{diagnostics.reason}")
            raise
        except Exception as exc:
            if diagnostics is not None:
                diagnostics.status = "request-failed"
                safe_error = str(exc).replace(api_key, "[已隐藏]") if api_key else str(exc)
                diagnostics.reason = f"影片条目请求失败（{type(exc).__name__}）：{safe_error[:240]}"
                diagnostics.attempts.append(f"{query}：{diagnostics.reason}")
            continue
        resolved = _feature_identity_from_response(
            response, identity, strict=strict, diagnostics=diagnostics,
        )
        if diagnostics is not None:
            mode = "精确" if exact else "宽搜后身份筛选"
            diagnostics.attempts.append(f"{mode} {query}：{diagnostics.reason}")
        if resolved:
            return resolved
        if (needs_canonical_title(identity.title) and diagnostics is not None
                and diagnostics.status == "identity-conflict"):
            # This English proposal is already contradicted by the site's
            # catalogue. Search the original name next, not more variants
            # of the same wrong sequel.
            return None
    return None


def _request(api_key: str, path: str, *, params: dict | None = None, payload: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    if params:
        normalized_params = {
            key: str(value).casefold() if key == "query" else value
            for key, value in params.items()
            if value not in (None, "") and not (key == "page" and int(value) == 1)
        }
        url += "?" + urllib.parse.urlencode(sorted(normalized_params.items()))
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Api-Key": api_key.strip(), "User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with _IPV4_OPENER.open(request, timeout=OPEN_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("message", detail)
        except json.JSONDecodeError:
            message = detail
        if exc.code in (401, 403):
            raise SubtitleAuthenticationError("OpenSubtitles API Key 无效或无权访问。") from exc
        if exc.code == 429:
            raise SubtitleQuotaError("OpenSubtitles 今日请求或下载额度已用完。") from exc
        raise RuntimeError(f"OpenSubtitles 请求失败（{exc.code}）：{message}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 OpenSubtitles：{exc.reason}") from exc


def validate_api_key(api_key: str) -> None:
    """Verify search access without a login, download, or settings write.

    The API-Key-protected /subtitles route checks the same credential used by
    search. A fixed IMDb ID keeps this to one small page; an empty result is
    valid. Public format/language lists cannot establish credential validity.
    """
    key = api_key.strip()
    if not key:
        raise RuntimeError("请先填写 OpenSubtitles API Key。")
    try:
        response = _request(key, "/subtitles", params={"imdb_id": 1, "languages": "en"})
    except (SubtitleAuthenticationError, SubtitleQuotaError) as exc:
        raise type(exc)(str(exc).replace(key, "[已隐藏]")) from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError("OpenSubtitles 返回的数据格式异常，未保存 Key，请稍后重试。") from exc
    except TimeoutError as exc:
        raise RuntimeError("OpenSubtitles 验证请求超时，未保存 Key，请稍后重试。") from exc
    except RuntimeError as exc:
        raise RuntimeError(str(exc).replace(key, "[已隐藏]")) from exc

    if not isinstance(response, dict):
        raise RuntimeError("OpenSubtitles 返回的数据格式异常，未保存 Key，请稍后重试。")
    status = response.get("status")
    if status in (401, 403, "401", "403"):
        raise SubtitleAuthenticationError("OpenSubtitles API Key 无效或无权访问。")
    if status in (429, "429"):
        raise SubtitleQuotaError("OpenSubtitles 今日请求或下载额度已用完。")
    if (status not in (None, 200, "200") or response.get("success") is False
            or response.get("errors") or response.get("error")):
        raise RuntimeError("OpenSubtitles 未通过验证，未保存 Key，请稍后重试。")
    pagination = ("total_pages", "total_count", "per_page", "page")
    if (not isinstance(response.get("data"), list)
            or any(type(response.get(name)) is not int for name in pagination)
            or response["total_pages"] < 0 or response["total_count"] < 0
            or response["per_page"] <= 0 or response["page"] != 1):
        raise RuntimeError("OpenSubtitles 返回的数据格式异常，未保存 Key，请稍后重试。")


def _candidates_from_response(response: dict, language: str) -> list[SubtitleCandidate]:
    results: list[SubtitleCandidate] = []
    for item in response.get("data", []):
        attrs = item.get("attributes") or {}
        feature = attrs.get("feature_details") or {}
        files = attrs.get("files") or []
        if not files:
            continue
        file = files[0]
        try:
            file_id = int(file.get("file_id"))
        except (TypeError, ValueError):
            continue
        results.append(SubtitleCandidate(
            file_id=file_id,
            file_name=str(file.get("file_name") or f"subtitle-{file_id}.srt"),
            release=str(attrs.get("release") or file.get("file_name") or "未命名字幕"),
            language=str(attrs.get("language") or language),
            downloads=int(attrs.get("download_count") or 0),
            rating=float(attrs.get("ratings") or 0),
            trusted=bool(attrs.get("from_trusted")),
            hearing_impaired=bool(attrs.get("hearing_impaired")),
            moviehash_match=attrs.get("moviehash_match") is True,
            author=author_name(attrs.get("uploader")),
            author_role="上传者",
            feature_title=str(
                feature.get("movie_name") or feature.get("title") or feature.get("name") or ""
            ),
            feature_year=str(feature.get("year") or ""),
            feature_id=str(feature.get("feature_id") or ""),
            feature_imdb_id=str(feature.get("imdb_id") or "").removeprefix("tt"),
            feature_tmdb_id=str(feature.get("tmdb_id") or ""),
            feature_type=str(feature.get("feature_type") or ""),
            feature_season_number=str(feature.get("season_number") or ""),
            feature_episode_number=str(feature.get("episode_number") or ""),
            feature_parent_imdb_id=str(feature.get("parent_imdb_id") or ""),
            feature_parent_tmdb_id=str(feature.get("parent_tmdb_id") or ""),
        ))
    return results


def _search_pages(
    api_key: str,
    params: dict[str, str | int],
    language: str,
    max_pages: int = 3,
) -> tuple[list[SubtitleCandidate], int, bool]:
    results: list[SubtitleCandidate] = []
    total_count = 0
    total_pages = 1
    for page in range(1, max_pages + 1):
        page_params = dict(params)
        page_params["page"] = page
        response = _request(api_key, "/subtitles", params=page_params)
        total_count = int(response.get("total_count") or total_count or 0)
        total_pages = max(1, int(response.get("total_pages") or 1))
        results.extend(_candidates_from_response(response, language))
        if page >= total_pages:
            break
    unique = {item.file_id: item for item in results}
    return list(unique.values()), total_count or len(unique), total_pages > max_pages


def _search_localized_title(
    api_key: str, base: MediaIdentity, language: str, moviehash: str,
) -> tuple[MediaIdentity, list[SubtitleCandidate], SearchMeta]:
    """A model/cache title is a query proposal until the provider confirms it."""
    config = settings_path()
    source = canonical_title_source(base.title, base.year, config)
    canonical = resolve_canonical_title(base.title, base.year, config)
    attempts = [f"英文片名候选来源={source}" + (f"：{canonical}" if canonical else "；无可用候选")]
    fallback = ""
    last_lookup = _FeatureLookupDiagnostics()
    proposals = [replace(base, original_title=canonical)] if canonical else []
    proposals.append(replace(base, original_title=base.title))
    attempted_english: list[str] = []

    def recover_after(index: int) -> None:
        if not attempted_english or len(attempted_english) >= 2:
            return
        recovered = resolve_canonical_title(
            base.title, base.year, config, excluded_titles=tuple(attempted_english),
        )
        if (recovered and recovered.casefold() not in {value.casefold() for value in attempted_english}
                and not needs_canonical_title(recovered)):
            proposals.insert(index + 1, replace(base, original_title=recovered))
            attempts.append(f"保留原年份和续集编号，恢复英文片名候选：{recovered}；仍需站点确认")

    for index, proposal in enumerate(proposals):
        native_query = needs_canonical_title(proposal.original_title)
        if not native_query:
            attempted_english.append(proposal.original_title)
        lookup = _FeatureLookupDiagnostics()
        if sequel_title_conflict(base.title, (proposal.original_title,)):
            lookup.status = "identity-conflict"
            lookup.reason = "英文片名候选与原文件的续集编号冲突"
            lookup.proposal_conflict = True
            feature = None
        else:
            token = _FEATURE_LOOKUP_DIAGNOSTICS.set(lookup)
            try:
                feature = _resolve_feature_identity(api_key, proposal)
            finally:
                _FEATURE_LOOKUP_DIAGNOSTICS.reset(token)
        if not lookup.status:
            lookup.status = "resolved" if feature is not None else "unresolved"
            lookup.reason = "已确认站点影片身份" if feature is not None else "未确认站点影片身份"
        attempts.extend(lookup.attempts or [f"{proposal.original_title}：{lookup.reason}"])
        last_lookup = lookup

        # Unconfirmed English proposals must not become the base identity.
        # Definite conflicts are shared so the next provider skips the bad name.
        if feature is None and not native_query and lookup.status == "identity-conflict":
            if lookup.proposal_conflict:
                reject_canonical_title(base.title, base.year, proposal.original_title, lookup.reason, config)
            fallback = f"英文片名候选“{proposal.original_title}”被否决：{lookup.reason}；保留原年份和续集编号补查其他英文名及原片名"
            attempts.append(fallback)
            recover_after(index)
            continue

        params: dict[str, str | int] = {"languages": language, "type": "movie"}
        if feature is not None and feature.imdb_id:
            params["imdb_id"] = feature.imdb_id
        elif feature is not None and feature.tmdb_id:
            params["tmdb_id"] = feature.tmdb_id
        else:
            params["query"] = proposal.original_title
            if base.year:
                params["year"] = base.year
        results, total, truncated = _search_pages(api_key, params, language)
        resolved = feature or proposal
        if feature is None:
            # Metadata-bearing title/year results can confirm an English
            # proposal even when the features endpoint has no catalogue row.
            confirmed = [item for item in results
                         if _title_year_identity_key(item, proposal)
                         and not sequel_title_conflict(base.title, (item.feature_title,))]
            if native_query:
                confirmed = [item for item in confirmed if _titles_equivalent(base.title, item.feature_title)]
            ids = {("imdb", value) for item in confirmed
                   if (value := _provider_id_key(item.feature_imdb_id, imdb=True))}
            ids.update(("tmdb", value) for item in confirmed
                       if (value := _provider_id_key(item.feature_tmdb_id)))
            # A title/year string alone cannot confirm a model-generated alias.
            # Require one catalogue identity, and reject conflicting IDs of the
            # same type before any subtitle enters verification.
            if not ids or any(len({value for kind, value in ids if kind == provider}) > 1
                              for provider in ("imdb", "tmdb")):
                confirmed = []
            else:
                confirmed = [item for item in confirmed if
                             _provider_id_key(item.feature_imdb_id, imdb=True)
                             or _provider_id_key(item.feature_tmdb_id)]
            results = confirmed
        if results:
            identity_key = (f"imdb:{feature.imdb_id}" if feature and feature.imdb_id else
                            f"tmdb:{feature.tmdb_id}" if feature and feature.tmdb_id else "")
            for item in results:
                item.identity_verified = (_candidate_matches_strong_identity(item, resolved)
                                          if feature else bool(_title_year_identity_key(item, proposal)))
                item.identity_key = identity_key or _title_year_identity_key(item, proposal)
                item.recommendation = "待内容验证"
                item.match_reason = "原年份和影片身份确认；仍需下载后验证字幕正文"
            if not needs_canonical_title(resolved.original_title):
                confirmed_id = identity_key or next((f"imdb:{item.feature_imdb_id}" for item in results
                                                     if _provider_id_key(item.feature_imdb_id, imdb=True)), "")
                confirmed_id = confirmed_id or next((f"tmdb:{item.feature_tmdb_id}" for item in results
                                                     if _provider_id_key(item.feature_tmdb_id)), "")
                remember_canonical_title(base.title, base.year, resolved.original_title,
                                         config, confirmed_year=base.year, confirmed_identity=confirmed_id)
            return resolved, results, SearchMeta(
                total_count=total, loaded_count=len(results), query_mode="feature" if feature else "title-year",
                moviehash=moviehash, truncated=truncated,
                feature_lookup_status=lookup.status, feature_lookup_reason=lookup.reason,
                feature_lookup_attempts=tuple(attempts), fallback_reason=fallback,
            )
        if not native_query:
            fallback = (f"{source}英文片名候选未取得确认且可用的字幕；"
                        f"保留原年份{base.year or '未知'}补查其他英文名及原片名“{base.title}”")
            attempts.append(fallback)
            recover_after(index)
    return base, [], SearchMeta(
        query_mode="unresolved-title" if not canonical else "title-year", moviehash=moviehash,
        feature_lookup_status=last_lookup.status, feature_lookup_reason=last_lookup.reason,
        feature_lookup_attempts=tuple(attempts), fallback_reason=fallback,
    )


def _candidate_work_profiles(candidates):
    return [WorkProfile(
        str(getattr(item, 'feature_title', '') or ''), str(getattr(item, 'feature_year', '') or ''),
        str(getattr(item, 'feature_imdb_id', '') or ''), str(getattr(item, 'feature_tmdb_id', '') or ''),
        'opensubtitles-subtitles', str(getattr(item, 'feature_type', '') or 'movie'))
        for item in candidates]


def _apply_identity_admission(candidates, identity, status='', evidence=()):
    """Positive same-work admission, before the unchanged purpose/timing gates.

    Keep all rows for manual search. Automatic callers must respect state.
    In particular, ambiguous catalogue IDs cannot become verified merely by
    a later title/year fallback.
    """
    exact_rows = [item for item in candidates
                  if getattr(item, 'feature_title', '') and _title_year_identity_key(item, identity)]
    ids = {('imdb', value) for item in exact_rows
           if (value := _provider_id_key(item.feature_imdb_id, imdb=True))}
    ids.update(('tmdb', value) for item in exact_rows
               if (value := _provider_id_key(item.feature_tmdb_id)))
    competing = any(len({value for source, value in ids if source == provider}) > 1
                    for provider in ('imdb', 'tmdb'))
    ambiguous = status in {'ambiguous', 'identity-conflict'} or competing
    for item in candidates:
        conflict = _candidate_identity_metadata_conflict(item, identity)
        if conflict:
            state, reason = 'filtered', conflict
        elif item.moviehash_match is True:
            state, reason = 'auto', '站点逐条明确Hash命中，仍需用途、正文及时间轴核验'
        elif _candidate_matches_strong_identity(item, identity):
            state, reason = 'auto', '与已确认作品ID一致，仍需用途、正文及时间轴核验'
        elif getattr(item, 'feature_title', '') and _title_year_identity_key(item, identity):
            if ambiguous:
                state, reason = 'pending', '片名和年份相符，但作品ID歧义未解除，不能降格为确认'
            else:
                state, reason = 'auto', '完整目录片名及明确年份一致，仍需用途、正文及时间轴核验'
        else:
            expected = _identity_title_tokens(identity.original_title or identity.title)
            actual = _identity_title_tokens(item.feature_title)
            same_year = bool(identity.year and identity.year == item.feature_year)
            prefix = bool(len(actual) >= 2 and len(actual) < len(expected) and expected[:len(actual)] == actual)
            if not actual or (same_year and prefix) or (actual == expected and not item.feature_year):
                state, reason = 'pending', '候选尚缺完整同片身份或别名证据，保留供字幕选择确认'
            else:
                state, reason = 'filtered', '宽搜记录缺少明确Hash、一致作品ID或完整片名年份证据'
        item.identity_admission_state = state
        item.identity_admission_reason = reason
        item.identity_admission_evidence = tuple(evidence) if state == 'auto' else ()
        item.identity_verified = state == 'auto'
        if state != 'auto':
            item.identity_key = f'opensubtitles-file:{item.file_id}'
            item.recommendation = '身份待确认' if state == 'pending' else '影片身份不符'
        elif getattr(identity, 'imdb_id', ''):
            item.identity_key = 'imdb:' + str(identity.imdb_id)
        elif getattr(identity, 'tmdb_id', ''):
            item.identity_key = 'tmdb:' + str(identity.tmdb_id)
        elif item.moviehash_match is not True:
            # Preserve the established verification/checkpoint key for an
            # unambiguous full-title/year fallback without catalogue IDs.
            # This branch must never overwrite an explicit hash identity.
            item.identity_key = _title_year_identity_key(item, identity) or item.identity_key
        item.match_reason = reason
    return candidates


def _resolution_meta(meta, resolution):
    meta.identity_resolution_status = resolution.state
    meta.identity_resolution_reason = resolution.reason
    meta.identity_resolution_requests = resolution.request_count
    meta.identity_resolution_seconds = resolution.elapsed_seconds
    meta.identity_resolution_cache_hit = resolution.cache_hit
    meta.identity_resolution_evidence = resolution.evidence
    return meta


def _recover_work_identity(api_key, identity, candidates, meta, lookup=None):
    context = _WORK_IDENTITY_CONTEXT.get()
    if context is not None and context['attempted']:
        return None
    if context is not None:
        context['attempted'] = True
    profiles = tuple(getattr(lookup, 'profiles', ()) or ()) + tuple(_candidate_work_profiles(candidates))
    resolution = resolve_work_identity(identity, profiles,
                                       cancel_event=context.get('cancel_event') if context else None,
                                       time_budget_seconds=min(15.0, max(0.0, context.get('metadata_budget_seconds', 15.0))) if context else 15.0)
    if context is not None:
        context['resolution'] = resolution
    _resolution_meta(meta, resolution)
    if lookup is not None:
        lookup.attempts.extend(resolution.attempts)
    if resolution.state != 'confirmed':
        return None
    return _search_confirmed_work_identity(api_key, identity, candidates, meta, resolution, lookup)


def _search_confirmed_work_identity(api_key, identity, candidates, meta, resolution, lookup=None):
    context = _WORK_IDENTITY_CONTEXT.get()
    if context is not None:
        context['resolution'] = resolution
    _resolution_meta(meta, resolution)
    resolved = replace(identity, original_title=resolution.title, year=resolution.year,
                       imdb_id=resolution.imdb_id, tmdb_id=resolution.tmdb_id)
    # The confirmed ID, never the shorter name, scopes the actual subtitle query.
    params = {'languages': context.get('language', 'en') if context else 'en',
              'imdb_id': resolved.imdb_id, 'type': 'movie'}
    fresh, total, truncated = _search_pages(api_key, params, params['languages'])
    # Preserve already discovered candidates for manual inspection and audit;
    # nonmatching rows remain filtered and cannot consume download attempts.
    combined = {item.file_id: item for item in (*candidates, *fresh)}
    results = list(combined.values())
    _apply_identity_admission(results, resolved, 'resolved', resolution.evidence)
    meta.total_count = total
    meta.loaded_count = len(results)
    meta.truncated = truncated
    meta.query_mode = 'confirmed-work-id'
    meta.feature_lookup_status = 'resolved'
    meta.feature_lookup_reason = resolution.reason
    meta.feature_lookup_attempts = tuple(getattr(lookup, 'attempts', ())) or resolution.attempts
    meta.fallback_reason = ''
    return resolved, results, meta


def search(api_key: str, video_path: str, language: str, *, skip_hash: bool = False,
           cancel_event=None, identity_time_budget_seconds: float = 15.0) -> tuple[MediaIdentity, list[SubtitleCandidate], SearchMeta]:
    context = {'cancel_event': cancel_event, 'attempted': False, 'resolution': None, 'language': language,
               'metadata_budget_seconds': identity_time_budget_seconds}
    token = _WORK_IDENTITY_CONTEXT.set(context)
    try:
        identity, candidates, meta = _search_impl(api_key, video_path, language, skip_hash=skip_hash)
        if context['resolution'] is not None and not meta.identity_resolution_status:
            _resolution_meta(meta, context['resolution'])
        _apply_identity_admission(candidates, identity, meta.feature_lookup_status,
                                  meta.identity_resolution_evidence)
        return identity, candidates, meta
    finally:
        _WORK_IDENTITY_CONTEXT.reset(token)


def _search_impl(
    api_key: str, video_path: str, language: str, *, skip_hash: bool = False,
) -> tuple[MediaIdentity, list[SubtitleCandidate], SearchMeta]:
    if not api_key.strip():
        raise RuntimeError("请先填写 OpenSubtitles API Key。")
    if conflict := filename_container_identity_conflict(video_path):
        raise RuntimeError(conflict)
    base_identity = identify_media(video_path)
    identity = base_identity
    common: dict[str, str | int] = {"languages": language}

    moviehash = ""
    if not skip_hash:
        try:
            moviehash = opensubtitles_movie_hash(video_path)
        except OSError:
            pass
    if moviehash:
        exact, total, truncated = _search_pages(
            api_key,
            {**common, "moviehash": moviehash, "moviehash_match": "only"},
            language,
            max_pages=2,
        )
        # The provider can return suggested subtitles alongside a hash query.
        # Only its explicit per-item hash matches are safe to treat as exact.
        exact = [item for item in exact if item.moviehash_match]
        for item in exact:
            item.identity_verified = True
            item.identity_key = f"hash:{moviehash}"
            item.recommendation = "待内容验证"
            item.match_reason = "影片 Hash 命中；仍需下载后验证字幕正文"
        if exact:
            return identity, exact, SearchMeta(total_count=total, loaded_count=len(exact), query_mode="hash", moviehash=moviehash, truncated=truncated)

    if needs_canonical_title(identity.title):
        # Reading this source does not start the local model. Already confirmed
        # aliases (or their bounded series proposal) retain their existing fast
        # provider-confirmation path. Without one, try lightweight native-name
        # metadata before a possible first 90-second model request.
        source = canonical_title_source(identity.title, identity.year, settings_path())
        cached_proposal = source in {'缓存', '已确认系列别名与续集编号'}
        localized = (_search_localized_title(api_key, base_identity, language, moviehash)
                     if cached_proposal else None)
        if localized is not None and localized[1]:
            return localized
        native_meta = localized[2] if localized is not None else SearchMeta(
            query_mode='unresolved-title', moviehash=moviehash,
            feature_lookup_status='unresolved', feature_lookup_reason='原片名尚无已确认英文别名')
        recovered = _recover_work_identity(api_key, identity,
                                            localized[1] if localized is not None else [], native_meta)
        if recovered is not None:
            return recovered
        return localized if localized is not None else _search_localized_title(
            api_key, base_identity, language, moviehash)

    # A hash miss/rejection must not carry the site's hash identity forward.
    # Resolve the local film afresh; only a unique title/year-confirmed ID may
    # narrow this retry before falling back to a broad title/year query.
    lookup = _FeatureLookupDiagnostics()
    token = _FEATURE_LOOKUP_DIAGNOSTICS.set(lookup)
    try:
        feature_identity = (_resolve_feature_identity(api_key, identity, strict=True)
                            if skip_hash else _resolve_feature_identity(api_key, identity))
    finally:
        _FEATURE_LOOKUP_DIAGNOSTICS.reset(token)
    # Alternate/custom resolvers may not supply diagnostics. Keep their return
    # contract compatible and make the fallback observable in that case too.
    if not lookup.status:
        lookup.status = "resolved" if feature_identity is not None else "unresolved"
        lookup.reason = "已解析站点影片身份" if feature_identity is not None else "未取得可锁定的站点影片身份"
    fallback_reason = lookup.reason if feature_identity is None else ""
    if feature_identity is not None:
        feature_params = dict(common)
        if feature_identity.imdb_id:
            feature_params["imdb_id"] = feature_identity.imdb_id
        elif feature_identity.tmdb_id:
            feature_params["tmdb_id"] = feature_identity.tmdb_id
        else:
            feature_params["query"] = feature_identity.original_title or feature_identity.title
            feature_params["year"] = feature_identity.year
            feature_params["type"] = "movie"
        results, total, truncated = _search_pages(api_key, feature_params, language)
        identity_key = (
            f"imdb:{feature_identity.imdb_id}" if feature_identity.imdb_id else
            f"tmdb:{feature_identity.tmdb_id}" if feature_identity.tmdb_id else
            f"feature:{feature_identity.feature_id}"
        )
        for item in results:
            item.identity_verified = _candidate_matches_strong_identity(item, feature_identity)
            item.identity_key = identity_key
            item.recommendation = "待内容验证"
            item.match_reason = "站点影片条目命中；仍需下载后验证字幕正文"
        if results:
            return feature_identity, results, SearchMeta(
                total_count=total,
                loaded_count=len(results),
                query_mode="feature",
                moviehash=moviehash,
                truncated=truncated,
                feature_lookup_status=lookup.status,
                feature_lookup_reason=lookup.reason,
                feature_lookup_attempts=tuple(lookup.attempts),
            )
        fallback_reason = "已定位影片身份，但该身份未返回字幕；改用原片名和年份搜索"

    if (feature_identity is None and base_identity.year
            and lookup.status != 'identity-conflict' and not lookup.proposal_conflict):
        cached = read_cached_resolution(base_identity, lookup.profiles)
        if cached is not None:
            context = _WORK_IDENTITY_CONTEXT.get()
            if context is not None:
                context['attempted'] = True
            lookup.attempts.append('复用完整片名和年份绑定的已确认作品证据；跳过重复宽搜')
            meta = SearchMeta(moviehash=moviehash, feature_lookup_attempts=tuple(lookup.attempts))
            return _search_confirmed_work_identity(api_key, base_identity, [], meta, cached, lookup)

    if not base_identity.year:
        reason = ("未取得可用的已确认影片身份，文件名也没有年份；停止按未确认片名宽搜。"
                  "请补全正确片名和年份，或使用字幕选择。")
        return feature_identity or base_identity, [], SearchMeta(
            query_mode="unresolved-title", moviehash=moviehash,
            feature_lookup_status=lookup.status, feature_lookup_reason=lookup.reason,
            feature_lookup_attempts=tuple(lookup.attempts), fallback_reason=reason,
        )

    # A tentative provider feature must never replace the original filename
    # identity. If the provider-id path is empty, retry the user's actual title.
    identity = base_identity
    params = dict(common)
    params["query"] = identity.original_title or identity.title
    params["type"] = "movie"
    if identity.year:
        params["year"] = identity.year
    results, total, truncated = _search_pages(api_key, params, language)
    meta = SearchMeta(
        total_count=total,
        loaded_count=len(results),
        query_mode="title-year",
        moviehash=moviehash,
        truncated=truncated,
        feature_lookup_status=lookup.status,
        feature_lookup_reason=lookup.reason,
        feature_lookup_attempts=tuple(lookup.attempts),
        fallback_reason=fallback_reason,
    )
    if feature_identity is None:
        recovered = _recover_work_identity(api_key, identity, results, meta, lookup)
        if recovered is not None:
            return recovered
    _apply_identity_admission(results, identity, lookup.status)
    return identity, results, meta


def _extract_download(data: bytes, file_name: str, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [name for name in archive.namelist() if Path(name).suffix.lower() in SUBTITLE_EXTENSIONS]
            if not members:
                raise RuntimeError("下载包中没有可用的字幕文件。")
            chosen = members[0]
            output = destination / Path(chosen).name
            output.write_bytes(archive.read(chosen))
            return output
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS:
        suffix = ".srt"
    output = destination / f"online-subtitle{suffix}"
    output.write_bytes(data)
    return output


def download(api_key: str, candidate: SubtitleCandidate, destination: str) -> Path:
    response = _request(api_key, "/download", payload={"file_id": candidate.file_id})
    link = str(response.get("link") or "")
    if not link:
        raise RuntimeError("字幕服务没有返回下载地址。")
    request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
    try:
        with _IPV4_OPENER.open(request, timeout=45) as file_response:
            data = file_response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"字幕文件下载失败：{exc.reason}") from exc
    return _extract_download(data, candidate.file_name, Path(destination))
