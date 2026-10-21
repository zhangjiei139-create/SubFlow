"""Bounded, independently corroborated film identity recovery.

Catalogue rows are proposals.  A shorter name never proves an alias: it needs
the public movie record's complete tagline, or a literal published alias.
This module never downloads a subtitle and does not relax timing acceptance.
"""
from __future__ import annotations

import hashlib
import html
from html.parser import HTMLParser
import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace

SCHEMA_VERSION = 2
CACHE_SECONDS = 7 * 24 * 60 * 60
CATALOGUE_LIMIT = 30
_CACHE_LOCK = threading.RLock()


def id_key(value, *, imdb=False):
    value = str(value or '').strip()
    if imdb:
        value = re.sub(r'^tt', '', value, flags=re.I)
    return value.lstrip('0') if re.fullmatch(r'[0-9]+', value) else ''


def title_key(value):
    """Literal words, including articles/connectors; no prefix/fuzzy proof."""
    value = html.unescape(unicodedata.normalize('NFKC', str(value or ''))).casefold()
    value = re.sub(r'^\s*(?:19|20)\d{2}\s+[-–—]\s+', '', value)
    value = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', value)
    return tuple(re.findall(r'[^\W_]+', value, re.UNICODE))


@dataclass(frozen=True)
class WorkProfile:
    title: str
    year: str
    imdb_id: str = ''
    tmdb_id: str = ''
    source: str = ''
    feature_type: str = 'movie'
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolution:
    state: str
    reason: str
    title: str = ''
    year: str = ''
    imdb_id: str = ''
    tmdb_id: str = ''
    evidence: tuple[dict, ...] = ()
    attempts: tuple[str, ...] = ()
    request_count: int = 0
    elapsed_seconds: float = 0.0
    cache_hit: bool = False


class EvidenceBudgetExceeded(RuntimeError):
    pass


@dataclass
class RequestBudget:
    max_requests: int = 2
    time_budget_seconds: float = 15.0
    cancel_event: object = None
    started: float = field(default_factory=time.monotonic)
    request_count: int = 0
    attempts: list[str] = field(default_factory=list)

    @property
    def remaining(self):
        return max(0.0, self.time_budget_seconds - (time.monotonic() - self.started))

    @property
    def timeout(self):
        self.check()
        return max(0.001, self.remaining)

    def check(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            # Reuse the application's cancellation type, rather than turning a
            # user stop into a recoverable metadata failure.
            import subtitle_tool_core as core
            core.check_cancel(self.cancel_event)
        if self.remaining <= 0:
            raise EvidenceBudgetExceeded('作品身份补证已达到总时间预算')

    def request(self, label, callback):
        self.check()
        if self.request_count >= self.max_requests:
            raise EvidenceBudgetExceeded('作品身份补证已达到额外请求上限')
        self.request_count += 1
        started = time.monotonic()
        try:
            value = callback()
            self.check()
        except Exception:
            self.attempts.append(f'{label}：失败，耗时{time.monotonic() - started:.3f}秒')
            raise
        self.attempts.append(f'{label}：完成，耗时{time.monotonic() - started:.3f}秒')
        return value


@dataclass
class RecoverySession:
    """One movie's lazy, shared extra-evidence budget across providers."""
    cancel_event: object = None
    time_budget_seconds: float = 15.0
    max_requests: int = 2
    cache_path: object = None
    primary_lookup: object = None
    budget: RequestBudget | None = field(default=None, init=False)
    results: dict = field(default_factory=dict, init=False)
    protected_titles: set = field(default_factory=set, init=False)
    revocations: list = field(default_factory=list, init=False)

    @property
    def confirmed_resolution(self):
        if self.revocations:
            return None
        return next((result for result in reversed(tuple(self.results.values()))
                     if result.state == 'confirmed'), None)

    def protect_full_title(self, identity, profiles=()):
        expected = title_key(identity.original_title or identity.title)
        if any(expected in {title_key(p.title), *(title_key(a) for a in p.aliases)}
               for p in profiles):
            self.protected_titles.add(expected)
        return expected in self.protected_titles

    def evidence_budget(self):
        """Start this movie's total evidence clock once, on first use."""
        if self.budget is None:
            self.budget = RequestBudget(min(2, max(0, self.max_requests)),
                min(15.0, max(0.0, self.time_budget_seconds)), self.cancel_event)
        return self.budget

    def revoked_resolution(self, identity):
        expected = title_key(identity.original_title or identity.title)
        year = str(identity.year or '')
        raw = str(getattr(identity, 'raw_title', '') or '')
        for bound, pending in reversed(self.revocations):
            if (expected == title_key(bound.original_title) and year == bound.year
                    and (not getattr(bound, 'raw_title', '') or raw == bound.raw_title)):
                return pending
            if (not year and getattr(bound, 'raw_title', '')
                    and expected == title_key(bound.raw_title)):
                return pending
        return None

    def revoke_resolution(self, identity, result, reason):
        """Revoke this complete input, preserving the shared paid budget."""
        bound = resolution_input_identity(identity, result)
        pending = Resolution('pending', reason, year=bound.year,
            attempts=tuple(self.budget.attempts) if self.budget else result.attempts,
            request_count=self.budget.request_count if self.budget else result.request_count,
            elapsed_seconds=time.monotonic()-self.budget.started if self.budget else result.elapsed_seconds)
        self.revocations.append((bound, pending))
        for key, previous in tuple(self.results.items()):
            if key[:2] != (title_key(bound.original_title), bound.year):
                continue
            previous_input = resolution_input_identity(bound, previous)
            if (getattr(bound, 'raw_title', '')
                    and getattr(previous_input, 'raw_title', '') != bound.raw_title):
                continue
            self.results[key] = pending
        revoke_cached_resolution(bound, self.cache_path)
        return pending

    def resolve(self, identity, profiles=(), *, allow_exact_title_lookup=False):
        if (revoked := self.revoked_resolution(identity)) is not None:
            return revoked
        self.evidence_budget()
        try:
            self.budget.check()
        except EvidenceBudgetExceeded as error:
            return Resolution('pending', str(error), request_count=self.budget.request_count,
                              elapsed_seconds=time.monotonic() - self.budget.started)
        profiles = tuple(profiles)
        key = (title_key(identity.original_title or identity.title), str(identity.year or ''),
               id_key(getattr(identity, 'imdb_id', ''), imdb=True),
               id_key(getattr(identity, 'tmdb_id', '')), bool(allow_exact_title_lookup),
               bool(getattr(identity, 'known_year_alias_entry', False)))
        previous = self.results.get(key)
        if previous is not None:
            if previous.state != 'confirmed' or validate_resolution(identity, previous, profiles):
                return previous
            if profiles and validate_resolution(identity, previous):
                return self.revoke_resolution(identity, previous,
                    '本轮新增目录记录与已确认作品证据冲突，已撤销本输入确认与缓存')
            return Resolution('pending', '本轮复用证据与新增目录记录冲突，未自动确认')
        result = resolve(identity, profiles, cache_path=self.cache_path,
                         cancel_event=self.cancel_event, budget=self.budget,
                         fetch_primary=self.primary_lookup,
                         allow_exact_title_lookup=allow_exact_title_lookup)
        self.results[key] = result
        return result


def filename_proposal_identity(video_path, base):
    """Bind a tail-year proposal to this still-unresolved complete filename."""
    from subtitle_identity_guard import trailing_filename_year_proposal

    if getattr(base, 'year', ''):
        return None
    proposal = trailing_filename_year_proposal(video_path)
    if (proposal is None or title_key(base.original_title or base.title)
            != title_key(proposal.raw_title)):
        return None
    return SimpleNamespace(title=proposal.title, original_title=proposal.title,
                           year=proposal.year,
                           raw_title=proposal.raw_title,
                           imdb_id=getattr(base, 'imdb_id', ''),
                           tmdb_id=getattr(base, 'tmdb_id', ''))


def validate_filename_resolution(video_path, base, result, profiles=()):
    """Keep normal proof validation strict; rebind only the one filename split."""
    if validate_resolution(base, result, profiles):
        return True
    proposal = filename_proposal_identity(video_path, base)
    return proposal is not None and validate_resolution(proposal, result, profiles)


def recover_filename_year(video_path, base, session, profiles=()):
    """Acquire evidence for one filename proposal, never for a known full title."""
    proposal = filename_proposal_identity(video_path, base)
    if proposal is None:
        return None
    if session.protect_full_title(base, profiles):
        return Resolution('pending', '目录已有完整原题记录；保留片名数字，不改用尾年拆分')
    return session.resolve(proposal, profiles, allow_exact_title_lookup=True)


def recover_known_year_alias(base, session, profiles=()):
    """Corroborate the unchanged complete title when its year is already known.

    This is a stronger mode of the existing bounded resolver, not another
    budget or a new filename split. The acquired alias must still match the
    complete input, exact year and both catalogue IDs.
    """
    title = str(base.original_title or base.title)
    year = str(base.year or '')
    if not title_key(title) or not re.fullmatch(r'\d{4}', year):
        return None
    bound = SimpleNamespace(title=base.title, original_title=title, year=year,
        imdb_id=getattr(base, 'imdb_id', ''), tmdb_id=getattr(base, 'tmdb_id', ''),
        known_year_alias_entry=True)
    return session.resolve(bound, profiles, allow_exact_title_lookup=True)


def resolution_input_identity(identity, result):
    """Recover the actual input binding, never the canonical shorter name."""
    tail = [e for e in result.evidence if e.get('acquisition') == 'filename-space-tail-proposal']
    known = [e for e in result.evidence if e.get('acquisition') == 'known-year-complete-title-alias-entry']
    if len(tail) == 1:
        return SimpleNamespace(title=tail[0]['proposed_title'], original_title=tail[0]['proposed_title'],
            year=str(tail[0]['year']), raw_title=tail[0]['raw_title'], imdb_id='', tmdb_id='')
    if len(known) == 1:
        return SimpleNamespace(title=known[0]['title'], original_title=known[0]['title'],
            year=str(known[0]['year']), known_year_alias_entry=True, imdb_id='', tmdb_id='')
    return SimpleNamespace(title=identity.title, original_title=identity.original_title or identity.title,
        year=str(identity.year or ''), raw_title=str(getattr(identity, 'raw_title', '') or ''),
        imdb_id=getattr(identity, 'imdb_id', ''), tmdb_id=getattr(identity, 'tmdb_id', ''))


def revoke_cached_resolution(bound, cache_path=None):
    """Remove all reachable confirmed cache keys for this exact input only."""
    if cache_path is False:
        return
    if cache_path is None:
        import online_subtitles
        cache_path = online_subtitles.settings_path().with_name('work-identity-evidence.json')
    target = Path(cache_path)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with _CACHE_LOCK:
            data = json.loads(target.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('schema') != SCHEMA_VERSION or not isinstance(data.get('entries'), dict):
                return
            expected, year = title_key(bound.original_title), str(bound.year)
            raw = str(getattr(bound, 'raw_title', '') or '')
            remove = []
            for key, row in data['entries'].items():
                source = row.get('input') if isinstance(row, dict) else None
                proof = row.get('resolution') if isinstance(row, dict) else None
                if (not isinstance(source, dict) or not isinstance(proof, dict)
                        or proof.get('state') != 'confirmed'
                        or title_key(source.get('title')) != expected
                        or str(source.get('year') or '') != year
                        or (raw and source.get('raw_title') != raw)):
                    continue
                remove.append(key)
            if not remove:
                return
            for key in remove:
                del data['entries'][key]
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
    except (OSError, ValueError, TypeError):
        # The current session still rejects the input. A later cold invocation
        # must repeat the fresh-profile check before publishing cached proof.
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _read_response(request, budget, *, limit=2_000_000):
    # No redirects to arbitrary hosts, alternate credentials, proxy changes,
    # anti-bot workarounds, or retries are attempted here.
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=budget.timeout) as response:
        content = bytearray()
        while True:
            budget.check()
            # Bound each following socket read by the remaining total budget.
            raw_socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if raw_socket is not None:
                raw_socket.settimeout(budget.timeout)
            reader = getattr(response, 'read1', response.read)
            block = reader(min(64 * 1024, limit + 1 - len(content)))
            if not block:
                break
            content.extend(block)
            if len(content) > limit:
                raise RuntimeError('作品元数据响应过大')
        budget.check()
        return bytes(content).decode('utf-8', errors='replace')


def fetch_secondary_catalogue(title, budget):
    """Use an existing configured secondary provider, never a new API key."""
    import subdl_subtitles
    key = str(subdl_subtitles.load_settings().get('subdl_api_key') or '').strip()
    if not key:
        raise RuntimeError('未配置独立字幕目录，保留身份待确认')
    url = subdl_subtitles.API_V2_BASE + '/movies/search?' + urllib.parse.urlencode(
        {'q': title, 'type': 'movie', 'limit': CATALOGUE_LIMIT})
    request = urllib.request.Request(url, headers={
        'User-Agent': subdl_subtitles.USER_AGENT, 'Accept': 'application/json',
        'Authorization': 'Bearer ' + key})
    try:
        result = json.loads(_read_response(request, budget))
    except urllib.error.HTTPError as error:
        import online_subtitles
        if error.code in (401, 403):
            raise online_subtitles.SubtitleAuthenticationError('SubDL API Key 无效或无权访问。') from error
        if error.code == 429:
            raise online_subtitles.SubtitleQuotaError('SubDL 请求额度已用完。') from error
        raise
    if not isinstance(result, dict):
        raise RuntimeError('独立目录响应格式无效')
    return result


def fetch_primary_catalogue(title, year, budget, *, api_key=None):
    """One bounded exact primary lookup for a numeric filename proposal."""
    import online_subtitles
    key = str(api_key if api_key is not None else
              online_subtitles.load_settings().get('opensubtitles_api_key') or '').strip()
    if not key:
        raise RuntimeError('未配置主字幕目录，保留数字片名身份待确认')
    params = {'query': title, 'year': year, 'type': 'movie', 'query_match': 'exact'}
    normalized_params = {
        name: str(value).casefold() if name == 'query' else value
        for name, value in params.items() if value not in (None, '')}
    url = online_subtitles.API_BASE + '/features?' + urllib.parse.urlencode(sorted(normalized_params.items()))
    request = urllib.request.Request(url, headers={
        'Api-Key': key, 'User-Agent': online_subtitles.USER_AGENT, 'Accept': 'application/json'})
    try:
        response = json.loads(_read_response(request, budget))
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise online_subtitles.SubtitleAuthenticationError('OpenSubtitles API Key 无效或无权访问。') from error
        if error.code == 429:
            raise online_subtitles.SubtitleQuotaError('OpenSubtitles 今日请求或下载额度已用完。') from error
        raise
    return response


def primary_response_complete(response):
    try:
        if (not isinstance(response, dict) or not isinstance(response.get('data'), (list, tuple))
                or len(response['data']) >= CATALOGUE_LIMIT
                or any(response.get(name) for name in ('next', 'next_page', 'has_more', 'truncated'))
                or int(response.get('total_pages') or 1) > 1):
            return False
        return not any(int(response[name]) > len(response['data'])
                       for name in ('total_count', 'total_results', 'total_entries', 'total')
                       if response.get(name) is not None)
    except (TypeError, ValueError):
        return False


def _primary_profiles(response):
    if not primary_response_complete(response):
        raise RuntimeError('主目录完整题名查询结果无效或未完整加载')
    profiles = []
    for row in response['data']:
        if not isinstance(row, dict) or not isinstance(row.get('attributes'), dict):
            raise RuntimeError('主目录影片条目格式无效')
        attrs = row['attributes']
        aliases = attrs.get('title_aka') or []
        aliases = [aliases] if isinstance(aliases, str) else aliases
        aliases = aliases if isinstance(aliases, (list, tuple)) else ()
        names = tuple(dict.fromkeys(str(value) for value in (
            attrs.get('title'), attrs.get('original_title'), *aliases) if isinstance(value, str) and value))
        profiles.append(WorkProfile(str(attrs.get('original_title') or attrs.get('title') or ''),
            str(attrs.get('year') or ''), id_key(attrs.get('imdb_id'), imdb=True),
            id_key(attrs.get('tmdb_id')), 'opensubtitles-features',
            str(attrs.get('feature_type') or attrs.get('type') or row.get('type') or ''), names))
    return profiles


def select_filename_primary_profile(identity, profiles):
    """Choose a complete-title/year double-ID proposal, never hide competition."""
    expected = title_key(identity.original_title or identity.title)
    year = str(identity.year or '')
    primary = [p for p in profiles if isinstance(p, WorkProfile)
               and p.source in {'opensubtitles-features', 'opensubtitles-features-incomplete'}]
    observed = [p for p in primary if p.year == year
             and expected in {title_key(p.title), *(title_key(a) for a in p.aliases)}]
    exact = [p for p in observed if p.source == 'opensubtitles-features']
    if not exact:
        return None, ''
    mappings = {(id_key(p.imdb_id, imdb=True), id_key(p.tmdb_id)) for p in exact
                if id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id)}
    if not mappings:
        imdb_ids = {id_key(p.imdb_id, imdb=True) for p in observed if id_key(p.imdb_id, imdb=True)}
        tmdb_ids = {id_key(p.tmdb_id) for p in observed if id_key(p.tmdb_id)}
        if (len(imdb_ids) > 1 or len(tmdb_ids) > 1
                or any(p.feature_type.casefold() not in {'movie', 'feature', ''} for p in observed)):
            return None, '主目录完整题名记录存在竞争ID或用途冲突，未按ID缩窄搜索'
        # Missing proof is not a contradiction. Keep the existing complete-
        # title independent lookup for e.g. an IMDb-only primary row.
        return None, ''
    if len(mappings) != 1:
        return None, '主目录完整题名同年记录未提供唯一双ID，不按ID挑选首条'
    imdb, tmdb = next(iter(mappings))
    if (any((id_key(p.imdb_id, imdb=True) and id_key(p.imdb_id, imdb=True) != imdb)
            or (id_key(p.tmdb_id) and id_key(p.tmdb_id) != tmdb)
            or p.feature_type.casefold() not in {'movie', 'feature', ''} for p in observed)
            or _profile_conflict(primary, imdb, tmdb, year)):
        return None, '主目录同题同年竞争ID、同ID年份或用途冲突，未按单一ID缩窄搜索'
    return next(p for p in exact if id_key(p.imdb_id, imdb=True) == imdb and id_key(p.tmdb_id) == tmdb), ''


def fetch_public_movie(tmdb_id, budget):
    key = id_key(tmdb_id)
    if not key:
        raise RuntimeError('缺少有效TMDB ID')
    language = getattr(budget, 'public_language', 'en-US')
    title = getattr(budget, 'public_title', '')
    slug = re.sub(r'[^a-z0-9]+', '-', unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode().casefold()).strip('-')
    suffix = '/titles' if getattr(budget, 'public_titles', False) else ''
    url = 'https://www.themoviedb.org/movie/' + key + ('-' + slug if slug else '') + suffix + '?' + urllib.parse.urlencode({'language': language})
    request = urllib.request.Request(url, headers={
        'User-Agent': 'SubFlow/2.0.81', 'Accept': 'text/html'})
    return _read_response(request, budget)


def _secondary_profiles(response):
    entries = response.get('results') or [] if isinstance(response, dict) else []
    if not isinstance(entries, list):
        return []
    result = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        title = str(row.get('original_name') or row.get('name') or '')
        aliases = tuple(dict.fromkeys(str(row.get(name) or '') for name in
                                     ('name', 'original_name') if row.get(name)))
        result.append(WorkProfile(title, str(row.get('year') or ''),
                                  id_key(row.get('imdb_id'), imdb=True),
                                  id_key(row.get('tmdb_id')), 'subdl-v2',
                                  str(row.get('type') or 'movie'), aliases))
    return result


def secondary_response_complete(response):
    """Only a complete bounded response may establish a new identity mapping."""
    try:
        if (not isinstance(response, dict) or not isinstance(response.get('results'), list)
                or len(response['results']) >= CATALOGUE_LIMIT
                or any(not isinstance(row, dict) for row in response['results'])
                or any(response.get(name) for name in ('next', 'next_page', 'has_more', 'truncated'))
                or int(response.get('total_pages') or 1) > 1):
            return False
        return all(not isinstance(response[name], bool)
                   and str(response[name]).isdigit()
                   and int(response[name]) == len(response['results'])
                   for name in ('total_count', 'total_results', 'total_entries', 'total', 'count')
                   if response.get(name) is not None)
    except (TypeError, ValueError):
        return False


def _combined_display_name(identity, profile):
    """A self-consistent display field proposes a lookup, never an alias."""
    original = unicodedata.normalize('NFKC', profile.title).strip()
    if not original:
        return ''
    for display in profile.aliases:
        normalized = unicodedata.normalize('NFKC', display).strip()
        match = re.fullmatch(r'(.+?)\s+\(' + re.escape(original) + r'\)', normalized)
        if match and title_key(match[1]) == title_key(identity.original_title or identity.title):
            return display
    return ''


def _combined_title_profile(identity, profiles):
    expected, year = title_key(identity.original_title or identity.title), str(identity.year or '')
    proposed = [p for p in profiles if p.year == year and _combined_display_name(identity, p)]
    complete = [p for p in proposed if p.source == 'subdl-v2' and p.feature_type.casefold() == 'movie'
                and id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id)]
    mappings = {(id_key(p.imdb_id, imdb=True), id_key(p.tmdb_id)) for p in complete}
    if len(mappings) != 1:
        return None
    imdb, tmdb = next(iter(mappings))
    related = [p for p in profiles if p.year == year and (
        _combined_display_name(identity, p) or expected in {title_key(p.title), *(title_key(a) for a in p.aliases)})]
    if (any(p.source != 'subdl-v2' or p.feature_type.casefold() != 'movie'
            or (id_key(p.imdb_id, imdb=True) and id_key(p.imdb_id, imdb=True) != imdb)
            or (id_key(p.tmdb_id) and id_key(p.tmdb_id) != tmdb) for p in related)
            or _profile_conflict(profiles, imdb, tmdb, year)):
        return None
    return complete[0]


def _combined_title_receipt(identity, result, record):
    try:
        rows = [WorkProfile(**row) for row in record['catalogue_profiles']]
        envelope = dict(record.get('catalogue_metadata', {}));envelope['results'] = [{} for _ in rows]
        selected = _combined_title_profile(identity, rows)
        return bool(secondary_response_complete(envelope) and selected is not None
            and record.get('input_title') == str(identity.original_title or identity.title)
            and record.get('input_year') == str(identity.year or '')
            and record.get('display_name') == _combined_display_name(identity, selected)
            and record.get('original_name') == selected.title
            and title_key(selected.title) == title_key(result.title)
            and id_key(selected.imdb_id, imdb=True) == result.imdb_id
            and id_key(selected.tmdb_id) == result.tmdb_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def confirmed_combined_title_alias(result, value, year, imdb_id, tmdb_id):
    """Recognize only an exact observed echo backed by full official proof."""
    try:
        records = [e for e in result.evidence
                   if e.get('acquisition') == 'subdl-structured-combined-title-proposal']
        if len(records) != 1:
            return False
        record = records[0]
        base = SimpleNamespace(title=record['input_title'], original_title=record['input_title'],
                               year=record['input_year'], imdb_id='', tmdb_id='')
        bound = resolution_input_identity(base, result)
        return (str(value).strip() == str(record['display_name']).strip()
                and str(year) == result.year and id_key(imdb_id, imdb=True) == result.imdb_id
                and id_key(tmdb_id) == result.tmdb_id and validate_resolution(bound, result))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _numeric_alias_profile(identity, query, profiles):
    """The complete English reading only proposes IDs; public aliases prove them."""
    from subtitle_identity_guard import numeric_title_alias_proposals
    proposals = numeric_title_alias_proposals(identity.original_title or identity.title)
    if not proposals or query != proposals[0]:
        return None
    expected = title_key(query)
    year = str(identity.year or '')
    observed = [p for p in profiles if p.year == year
                and expected in {title_key(p.title), *(title_key(a) for a in p.aliases)}]
    full = [p for p in observed if p.source == 'subdl-v2'
            and p.feature_type.casefold() == 'movie'
            and id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id)]
    pairs = {(id_key(p.imdb_id, imdb=True), id_key(p.tmdb_id)) for p in full}
    if len(pairs) != 1:
        return None
    imdb, tmdb = next(iter(pairs))
    if (any(p.source != 'subdl-v2' or p.feature_type.casefold() != 'movie'
            or (id_key(p.imdb_id, imdb=True) and id_key(p.imdb_id, imdb=True) != imdb)
            or (id_key(p.tmdb_id) and id_key(p.tmdb_id) != tmdb) for p in observed)
            or _profile_conflict(profiles, imdb, tmdb, year)):
        return None
    return full[0]


def parse_public_movie(page, tmdb_id):
    """Read only the movie's bound schema record and complete visible tagline."""
    expected = id_key(tmdb_id)
    movies = []
    for raw in re.findall(r'<script\b[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>',
                          page, flags=re.I | re.S):
        raw = re.sub(r'/\*.*?\*/', '', raw, flags=re.S).strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        entries = data if isinstance(data, list) else [data]
        for item in entries:
            if isinstance(item, dict) and item.get('@type') == 'Movie':
                movies.append(item)
    if len(movies) != 1:
        raise RuntimeError('公开条目不是唯一电影记录')
    row = movies[0]
    url = str(row.get('url') or '')
    parsed = urllib.parse.urlparse(url)
    match = re.match(r'^/movie/([0-9]+)(?:-|/|$)', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname not in {'www.themoviedb.org', 'themoviedb.org'} or not match or id_key(match.group(1)) != expected:
        raise RuntimeError('公开条目URL与请求作品ID不一致')
    years = set()
    for entry in row.get('releasedEvent') or []:
        value = str(entry.get('startDate') or '') if isinstance(entry, dict) else ''
        if re.match(r'^\d{4}-\d{2}-\d{2}', value):
            years.add(value[:4])
    # The visible title year identifies the catalogue's original movie year;
    # additional premiere dates cannot turn a different-year movie into proof.
    headers = re.findall(r'<h2\b[^>]*>(.*?)</h2>', page, flags=re.I | re.S)
    bound_headers = [block for block in headers if re.search(
        r'<a\b[^>]*href=[\"\'](?:https://www\.themoviedb\.org)?/movie/' + re.escape(expected) + r'(?:-|\?|[\"\'])', block, flags=re.I)]
    if len(bound_headers) != 1:
        raise RuntimeError('公开条目缺少与请求作品ID绑定的主标题')
    visible = re.findall(r'<span\b[^>]*class=[\"\'][^\"\']*\brelease_date\b[^\"\']*[\"\'][^>]*>\s*\((\d{4})\)\s*</span>', bound_headers[0], flags=re.I)
    original_years = set(visible)
    if len(original_years) != 1:
        raise RuntimeError('公开条目缺少唯一原始发行年份')
    year = next(iter(original_years))
    if years and year not in years:
        raise RuntimeError('公开条目年份字段冲突')
    tags = re.findall(r'<h3\b[^>]*class=[\"\'][^\"\']*\btagline\b[^\"\']*[\"\'][^>]*>(.*?)</h3>', page, flags=re.I | re.S)
    tagline = html.unescape(re.sub('<[^>]+>', '', tags[0])).strip() if len(tags) == 1 else ''
    aliases = row.get('alternateName') or []
    aliases = [aliases] if isinstance(aliases, str) else aliases
    aliases = tuple(value for value in aliases if isinstance(value, str)) if isinstance(aliases, list) else ()
    return {'title': str(row.get('name') or ''), 'year': year, 'tmdb_id': expected,
            'tagline': tagline, 'aliases': aliases, 'source': url,
            'acquisition': 'https-public-tmdb-movie-schema'}


class _PublicTitlesMarkup(HTMLParser):
    """Ignore inert text before reading the site's actual alias table markup."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.inert = []
        self.metadata = []
        self.stack = []
        self.header_parts = []
        self.alias_parts = []

    def scopes(self):
        classes = [set(attrs.get('class', '').split()) for _tag, attrs in self.stack]
        excluded = any(tag in {'nav', 'aside', 'footer'} or values & {
            'recommendations', 'recommendation', 'trending', 'search_results'}
            for (tag, _attrs), values in zip(self.stack, classes))
        header = (not excluded and any({'header', 'small'} <= values for values in classes)
                  and any('title' in values for values in classes))
        aliases = (not excluded and any(attrs.get('id') == 'media_v4' for _tag, attrs in self.stack)
                   and any('white_column' in values for values in classes)
                   and any(tag == 'section' and 'panel' in values
                           for (tag, _attrs), values in zip(self.stack, classes)))
        return header, aliases

    def append(self, markup):
        header, aliases = self.scopes()
        if header:
            self.header_parts.append(markup)
        if aliases:
            self.alias_parts.append(markup)

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.inert.append(tag)
            return
        if self.inert:
            return
        attrs = dict(attrs)
        if tag in {'link', 'meta'} and any(parent == 'head' for parent, _attrs in self.stack):
            self.metadata.append((tag, attrs))
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append((tag, attrs))
        values = ''.join(' ' + key + '="' + html.escape(value or '', quote=True) + '"'
                         for key, value in attrs.items())
        self.append('<' + tag + values + '>')

    def handle_endtag(self, tag):
        if self.inert:
            if self.inert[-1] == tag:
                self.inert.pop()
            return
        self.append('</' + tag + '>')
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.inert:
            self.append(html.escape(data))


def parse_public_movie_titles(page, tmdb_id):
    """Read complete aliases only from a movie-bound official titles table."""
    expected = id_key(tmdb_id)
    parser = _PublicTitlesMarkup()
    parser.feed(page)
    header_markup = ''.join(parser.header_parts)
    alias_markup = ''.join(parser.alias_parts)
    canonical = [attrs.get('href', '') for tag, attrs in parser.metadata
                 if tag == 'link' and 'canonical' in attrs.get('rel', '').split()]
    og_urls = [attrs.get('content', '') for tag, attrs in parser.metadata
               if tag == 'meta' and attrs.get('property') == 'og:url']
    og_titles = [attrs.get('content', '') for tag, attrs in parser.metadata
                 if tag == 'meta' and attrs.get('property') == 'og:title']
    def bound_url(value, titles=False):
        url = urllib.parse.urlparse(value)
        tail = '/titles' if titles else ''
        return (url.scheme == 'https' and url.hostname in {'www.themoviedb.org', 'themoviedb.org'}
                and not url.query and not url.fragment
                and re.fullmatch(r'/movie/' + re.escape(expected) + r'(?:-[^/]+)?' + tail, url.path))
    if (not expected or len(canonical) != 1 or len(og_urls) != 1 or len(og_titles) != 1
            or not bound_url(canonical[0], True) or not bound_url(og_urls[0])):
        raise RuntimeError('公开别名页canonical/OG与唯一请求作品ID不一致')
    headers = re.findall(r'<h2\b[^>]*>(.*?)</h2>', header_markup, flags=re.I | re.S)
    bound = []
    for block in headers:
        anchors = re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.I | re.S)
        linked = [(href, text) for href, text in anchors if re.match(
            r'^(?:https://www\.themoviedb\.org)?/movie/' + re.escape(expected) + r'(?:-|\?|$)', html.unescape(href))]
        years = re.findall(r'<span\b[^>]*class="[^"]*\brelease_date\b[^"]*"[^>]*>\s*\((\d{4})\)\s*</span>', block, flags=re.I)
        if linked:
            bound.append((linked, years))
    if len(bound) != 1 or len(bound[0][0]) != 1 or len(bound[0][1]) != 1:
        raise RuntimeError('公开别名页缺少唯一绑定主标题及发行年份')
    title = html.unescape(re.sub('<[^>]+>', '', bound[0][0][0][1])).strip()
    year = bound[0][1][0]
    if not title or title_key(title) != title_key(og_titles[0]):
        raise RuntimeError('公开别名页主标题与OG片名冲突')
    aliases = []
    for attributes, table in re.findall(r'<table\b([^>]*)>(.*?)</table>', alias_markup, flags=re.I | re.S):
        classes = re.search(r'\bclass="([^"]*)"', attributes, flags=re.I)
        if not classes or not {'card', 'releases', 'titles'} <= set(classes.group(1).split()):
            continue
        for body in re.findall(r'<tbody\b[^>]*>(.*?)</tbody>', table, flags=re.I | re.S):
            for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', body, flags=re.I | re.S):
                cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row, flags=re.I | re.S)
                if cells:
                    alias = html.unescape(re.sub('<[^>]+>', '', cells[0])).strip()
                    if alias:
                        aliases.append(alias)
    if not aliases:
        raise RuntimeError('公开别名页缺少正式别名表首列记录')
    return {'title': title, 'year': year, 'tmdb_id': expected, 'tagline': '',
            'aliases': tuple(dict.fromkeys(aliases)), 'source': canonical[0],
            'acquisition': 'https-public-tmdb-movie-titles'}


def _cache_key(identity, profiles):
    rows = sorted({(title_key(p.title), p.year, id_key(p.imdb_id, imdb=True), id_key(p.tmdb_id), p.feature_type,
                    tuple(sorted(title_key(value) for value in p.aliases)))
                   for p in profiles})
    data = [SCHEMA_VERSION, title_key(identity.original_title or identity.title),
            str(identity.year or ''), id_key(getattr(identity, 'imdb_id', ''), imdb=True),
            id_key(getattr(identity, 'tmdb_id', '')), rows]
    if getattr(identity, 'raw_title', ''):
        data.append(('filename-raw-title', str(identity.raw_title)))
    if getattr(identity, 'known_year_alias_entry', False):
        data.append(('known-year-complete-title-alias-entry', True))
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode('utf-8')).hexdigest()


def _decode_cache_entry(row):
    try:
        if not isinstance(row, dict):
            return None
        if not row or not 0 <= time.time() - float(row['saved_at']) < CACHE_SECONDS:
            return None
        value = row['resolution']
        if not isinstance(value, dict):
            return None
        if value.get('state') != 'confirmed' or not value.get('evidence'):
            return None
        if not isinstance(value['evidence'], (list, tuple)) or not all(isinstance(item, dict) for item in value['evidence']):
            return None
        evidence = tuple(value['evidence'])
        if not any(item.get('acquisition') == 'https-subdl-v2-unique-title-year-double-id' for item in evidence):
            return None
        return Resolution('confirmed', value['reason'], value['title'], value['year'],
                          value['imdb_id'], value['tmdb_id'], evidence, cache_hit=True)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read_cache(path, key):
    if path is False:
        return None
    try:
        with _CACHE_LOCK:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema') != SCHEMA_VERSION or not isinstance(data.get('entries'), dict):
            return None
        return _decode_cache_entry(data['entries'].get(key))
    except (OSError, ValueError, TypeError):
        return None


def _write_cache(path, key, result, identity):
    if path is False:
        return
    target = Path(path)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with _CACHE_LOCK:
            try:
                data = json.loads(target.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict) or data.get('schema') != SCHEMA_VERSION or not isinstance(data.get('entries'), dict):
                data = {'schema': SCHEMA_VERSION, 'entries': {}}
            data['entries'][key] = {
                'saved_at': time.time(), 'resolution': asdict(result),
                'input': {'title': str(identity.original_title or identity.title),
                          'year': str(identity.year or ''),
                          'imdb_id': id_key(getattr(identity, 'imdb_id', ''), imdb=True),
                          'tmdb_id': id_key(getattr(identity, 'tmdb_id', ''))}}
            if getattr(identity, 'raw_title', ''):
                data['entries'][key]['input']['raw_title'] = str(identity.raw_title)
            if getattr(identity, 'known_year_alias_entry', False):
                data['entries'][key]['input']['known_year_alias_entry'] = True
            data['entries'] = dict(list(data['entries'].items())[-256:])
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(target)
    except (OSError, ValueError, TypeError):
        # Failure to store a confirmed proof must not invalidate the proof.
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def catalogue_year_evidence(result):
    """Validate one narrow catalogue-year correction, never a release-year list."""
    try:
        if not isinstance(result, Resolution) or result.state != 'confirmed':
            return None
        entries = [item for item in result.evidence
                   if item.get('acquisition') == 'https-public-tmdb-catalogue-year-correction']
        if len(entries) != 1:
            return None
        record = entries[0]
        primary = WorkProfile(**record['primary_profile'])
        secondary = WorkProfile(**record['secondary_profile'])
        original = str(record['input_year'])
        canonical = str(record['canonical_year'])
        official = record['official']
        if (not re.fullmatch(r'(?:19|20)\d{2}', original)
                or not re.fullmatch(r'(?:19|20)\d{2}', canonical)
                or abs(int(original) - int(canonical)) != 1
                or canonical != result.year or primary.year != original or secondary.year != canonical
                or record.get('official_type') != 'movie'
                or primary.source != 'opensubtitles-features' or secondary.source != 'subdl-v2'
                or primary.feature_type.casefold() not in {'movie', 'feature', ''}
                or secondary.feature_type.casefold() != 'movie'):
            return None
        for profile in (primary, secondary):
            if (id_key(profile.imdb_id, imdb=True) != result.imdb_id
                    or id_key(profile.tmdb_id) != result.tmdb_id):
                return None
        primary_names = {title_key(primary.title), *(title_key(a) for a in primary.aliases)}
        secondary_names = {title_key(secondary.title), *(title_key(a) for a in secondary.aliases)}
        if (not primary_names & secondary_names or title_key(result.title) not in secondary_names
                or official.get('acquisition') != 'https-public-tmdb-movie-schema'
                or id_key(official.get('tmdb_id')) != result.tmdb_id
                or official.get('year') != canonical
                or title_key(official.get('title', '')) not in primary_names & secondary_names):
            return None
        source = urllib.parse.urlparse(str(official.get('source') or ''))
        if (source.scheme != 'https' or source.hostname not in {'www.themoviedb.org', 'themoviedb.org'}
                or source.query or source.fragment or not re.fullmatch(
                    r'/movie/' + re.escape(result.tmdb_id) + r'(?:-[^/]+)?', source.path)):
            return None
        expected_sources = {'input': [original], 'opensubtitles-features': [original], 'subdl-v2': [canonical]}
        if record.get('source_years') != expected_sources:
            return None
        return record
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def catalogue_year_matches(result, source, year, *, title, imdb_id, tmdb_id):
    """Match only this receipt's observed provider year and exact work IDs."""
    record = catalogue_year_evidence(result)
    if (record is None or id_key(imdb_id, imdb=True) != result.imdb_id
            or id_key(tmdb_id) != result.tmdb_id
            or str(year) not in record['source_years'].get(source, ())):
        return False
    profile_name = {'opensubtitles-features': 'primary_profile', 'subdl-v2': 'secondary_profile'}.get(source)
    if profile_name is None:
        return False
    profile = record[profile_name]
    return title_key(title) in {title_key(profile['title']), *(title_key(a) for a in profile.get('aliases', ())) }


def _profile_conflict(profiles, imdb_id, tmdb_id, year, *, year_evidence=None):
    for profile in profiles:
        imdb = id_key(profile.imdb_id, imdb=True)
        tmdb = id_key(profile.tmdb_id)
        if not ((imdb and imdb == imdb_id) or (tmdb and tmdb == tmdb_id)):
            continue
        if (imdb and imdb != imdb_id) or (tmdb and tmdb != tmdb_id):
            return True
        if profile.year and profile.year != year:
            source = ('opensubtitles-features' if profile.source == 'opensubtitles-subtitles'
                      and imdb == imdb_id and tmdb == tmdb_id else profile.source)
            if (year_evidence is None or profile.year not in year_evidence['source_years'].get(source, ())
                    or title_key(profile.title) not in {
                        title_key(year_evidence['primary_profile']['title']),
                        *(title_key(a) for a in year_evidence['primary_profile'].get('aliases', ())),
                        title_key(year_evidence['secondary_profile']['title']),
                        *(title_key(a) for a in year_evidence['secondary_profile'].get('aliases', ())) }):
                return True
        if profile.feature_type.casefold() not in {'movie', 'feature', ''}:
            return True
    return False


def _cache_is_bound(identity, profiles, result):
    year_record = catalogue_year_evidence(result)
    has_year_record = any(item.get('acquisition') == 'https-public-tmdb-catalogue-year-correction' for item in result.evidence)
    if has_year_record and year_record is None:
        return False
    if ((result.year != str(identity.year or '') and (
            year_record is None or year_record['input_year'] != str(identity.year or '')))
            or not result.imdb_id or not result.tmdb_id):
        return False
    expected = title_key(identity.original_title or identity.title)
    complete_names = {expected, title_key(result.title)}
    for profile in profiles:
        if (profile.source != 'opensubtitles-subtitles'
                or profile.year not in {str(identity.year or ''), result.year}
                or not complete_names & {title_key(profile.title), *(title_key(a) for a in profile.aliases)}):
            continue
        if ((id_key(profile.imdb_id, imdb=True) and id_key(profile.imdb_id, imdb=True) != result.imdb_id)
                or (id_key(profile.tmdb_id) and id_key(profile.tmdb_id) != result.tmdb_id)
                or profile.feature_type.casefold() not in {'movie', 'feature', ''}):
            return False
    raw_title = str(getattr(identity, 'raw_title', '') or '')
    known_entries = [entry for entry in result.evidence
                     if entry.get('acquisition') == 'known-year-complete-title-alias-entry']
    if known_entries or getattr(identity, 'known_year_alias_entry', False):
        if (len(known_entries) != 1
                or title_key(known_entries[0].get('title', '')) != expected
                or known_entries[0].get('year') != str(identity.year or '')):
            return False
    if raw_title:
        bindings = [entry for entry in result.evidence
                    if entry.get('acquisition') == 'filename-space-tail-proposal']
        if (len(bindings) != 1 or bindings[0].get('raw_title') != raw_title
                or title_key(bindings[0].get('proposed_title', '')) != expected
                or bindings[0].get('year') != str(identity.year or '')):
            return False
    if raw_title or known_entries or any(entry.get('acquisition') == 'filename-space-tail-proposal' for entry in result.evidence):
        current, conflict = select_filename_primary_profile(identity, profiles)
        if conflict or (current is not None and (
                id_key(current.imdb_id, imdb=True) != result.imdb_id or id_key(current.tmdb_id) != result.tmdb_id)):
            return False
        if any((id_key(p.imdb_id, imdb=True) and id_key(p.imdb_id, imdb=True) != result.imdb_id)
               or (id_key(p.tmdb_id) and id_key(p.tmdb_id) != result.tmdb_id)
               for p in profiles if p.source in {'opensubtitles-features', 'opensubtitles-features-incomplete'}
               and p.year == str(identity.year or '')
               and expected in {title_key(p.title), *(title_key(a) for a in p.aliases)}):
            return False
    secondary = [entry for entry in result.evidence if entry.get('acquisition') == 'https-subdl-v2-unique-title-year-double-id']
    if len(secondary) != 1:
        return False
    entry = secondary[0]
    if entry.get('source') != 'https://api.subdl.com/api/v2/movies/search':
        return False
    if (entry.get('year') != result.year or id_key(entry.get('imdb_id'), imdb=True) != result.imdb_id
            or id_key(entry.get('tmdb_id')) != result.tmdb_id or title_key(entry.get('title')) != title_key(result.title)):
        return False
    if _profile_conflict(profiles, result.imdb_id, result.tmdb_id, result.year, year_evidence=year_record):
        return False
    for name in ('imdb_id', 'tmdb_id'):
        known = id_key(getattr(identity, name, ''), imdb=name == 'imdb_id')
        if known and known != getattr(result, name):
            return False
    aliases = (entry.get('title'), *(entry.get('aliases') or ()))
    primary_entries = [item for item in result.evidence
                       if item.get('acquisition') == 'https-opensubtitles-exact-title-year-double-id']
    primary_alias = False
    if primary_entries:
        if len(primary_entries) != 1:
            return False
        primary = primary_entries[0]
        rows = [WorkProfile(**row) for row in primary.get('profiles', ())]
        selected, conflict = select_filename_primary_profile(identity, rows)
        names = {title_key(primary.get('title', '')), *(title_key(a) for a in primary.get('aliases', ()))}
        secondary_names = {title_key(value) for value in aliases if isinstance(value, str)}
        if (conflict or selected is None
                or primary.get('source') != 'https://api.opensubtitles.com/api/v1/features'
                or primary.get('year') != (year_record['input_year'] if year_record else result.year)
                or id_key(primary.get('imdb_id'), imdb=True) != result.imdb_id
                or id_key(primary.get('tmdb_id')) != result.tmdb_id
                or id_key(selected.imdb_id, imdb=True) != result.imdb_id
                or id_key(selected.tmdb_id) != result.tmdb_id
                or title_key(primary.get('title', '')) != title_key(selected.title)
                or expected not in names or not names & secondary_names):
            return False
        primary_alias = True
    numeric_entries = [item for item in result.evidence
                       if item.get('acquisition') == 'numeric-title-alias-proposal']
    numeric_alias = (len(expected) == 1 and expected[0].isdigit()
                     and expected not in {title_key(value) for value in aliases if isinstance(value, str)}
                     and not primary_alias)
    if numeric_alias or numeric_entries:
        if len(numeric_entries) != 1 or not numeric_alias:
            return False
        numeric = numeric_entries[0]
        rows = [WorkProfile(**row) for row in numeric.get('catalogue_profiles', ())]
        envelope = dict(numeric.get('catalogue_metadata', {}))
        envelope['results'] = [{} for _ in rows]
        selected = _numeric_alias_profile(identity, numeric.get('query'), rows)
        if (numeric.get('original_title') != str(identity.original_title or identity.title)
                or numeric.get('input_year') != str(identity.year or '')
                or not secondary_response_complete(envelope) or selected is None
                or title_key(selected.title) != title_key(result.title)
                or id_key(selected.imdb_id, imdb=True) != result.imdb_id
                or id_key(selected.tmdb_id) != result.tmdb_id):
            return False
    combined_entries = [item for item in result.evidence
                        if item.get('acquisition') == 'subdl-structured-combined-title-proposal']
    if bool(entry.get('structured_title_proposal_required')) != bool(combined_entries):
        return False
    if combined_entries:
        if len(combined_entries) != 1 or not _combined_title_receipt(identity, result, combined_entries[0]):
            return False
    if not combined_entries and expected in {title_key(value) for value in aliases if isinstance(value, str)}:
        return True
    if primary_alias and not combined_entries:
        return True
    for public in result.evidence:
        if public.get('acquisition') not in {'https-public-tmdb-movie-schema', 'https-public-tmdb-movie-titles'}:
            continue
        if public.get('year') != result.year or id_key(public.get('tmdb_id')) != result.tmdb_id:
            continue
        source = urllib.parse.urlparse(str(public.get('source') or ''))
        if source.scheme != 'https' or source.hostname not in {'www.themoviedb.org', 'themoviedb.org'} or not re.match(r'^/movie/' + re.escape(result.tmdb_id) + r'(?:-|/|$)', source.path):
            continue
        if (public.get('acquisition') == 'https-public-tmdb-movie-titles'
                and (not source.path.endswith('/titles') or source.query or source.fragment
                     or (not combined_entries and title_key(public.get('title', '')) != title_key(entry.get('title', ''))))):
            continue
        public_aliases = (public.get('title'), *(public.get('aliases') or ()))
        if combined_entries:
            names = {title_key(value) for value in public_aliases if isinstance(value, str)}
            if (public.get('acquisition') == 'https-public-tmdb-movie-titles'
                    and expected in names and title_key(entry.get('title', '')) in names):
                return True
            continue
        if numeric_alias:
            if (public.get('acquisition') == 'https-public-tmdb-movie-titles'
                    and expected in {title_key(value) for value in public.get('aliases', ())
                                     if isinstance(value, str)}):
                return True
            continue
        if expected in {title_key(value) for value in public_aliases if isinstance(value, str)}:
            return True
        if public.get('tagline') and expected == title_key(str(public.get('title') or '') + ' ' + public['tagline']):
            return True
    return False


def validate_resolution(identity, result, profiles=()):
    """Rebind a stored/cross-provider proof to the complete current identity."""
    try:
        return isinstance(result, Resolution) and result.state == 'confirmed' and _cache_is_bound(identity, profiles, result)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def confirmed_numeric_candidate_alias(candidate, identity):
    """Recognize the original numeric title only with its full acquired proof."""
    try:
        evidence = tuple(getattr(candidate, 'identity_admission_evidence', ()))
        readings = [item for item in evidence
                    if item.get('acquisition') == 'numeric-title-alias-proposal']
        if len(readings) != 1 or getattr(candidate, 'identity_admission_state', '') != 'auto':
            return False
        original = readings[0]['original_title']
        year = str(identity.year or '')
        imdb, tmdb = id_key(identity.imdb_id, imdb=True), id_key(identity.tmdb_id)
        if (not imdb or not tmdb or not year
                or title_key(getattr(candidate, 'feature_title', '')) != title_key(original)
                or str(getattr(candidate, 'feature_year', '') or '') != year
                or id_key(getattr(candidate, 'feature_imdb_id', ''), imdb=True) != imdb
                or id_key(getattr(candidate, 'feature_tmdb_id', '')) != tmdb):
            return False
        bindings = [item for item in evidence
                    if item.get('acquisition') == 'filename-space-tail-proposal']
        known = [item for item in evidence
                 if item.get('acquisition') == 'known-year-complete-title-alias-entry']
        if len(bindings) == 1 and bindings[0].get('raw_title') and not known:
            bound = SimpleNamespace(title=original, original_title=original, year=year,
                                    imdb_id=imdb, tmdb_id=tmdb,
                                    raw_title=bindings[0]['raw_title'])
        elif not bindings and len(known) == 1:
            bound = SimpleNamespace(title=original, original_title=original, year=year,
                                    imdb_id=imdb, tmdb_id=tmdb, known_year_alias_entry=True)
        else:
            return False
        result = Resolution('confirmed', '', identity.original_title or identity.title,
                            year, imdb, tmdb, evidence)
        return validate_resolution(bound, result)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def read_cached_resolution(identity, profiles=(), cache_path=None):
    """Read a complete-input proof before repeating broad catalogue queries.

    It does not turn a partial title/profile into an alias. New contradictory
    profiles revoke confirmed cache records for the same complete input.
    Conflicting confirmed IDs for the same input are an unresolved cache, even
    if one happened to be saved later or encountered first.
    """
    expected = title_key(identity.original_title or identity.title)
    year = str(identity.year or '')
    if cache_path is False or not expected or not re.fullmatch(r'\d{4}', year):
        return None
    if cache_path is None:
        import online_subtitles
        cache_path = online_subtitles.settings_path().with_name('work-identity-evidence.json')
    try:
        with _CACHE_LOCK:
            data = json.loads(Path(cache_path).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema') != SCHEMA_VERSION or not isinstance(data.get('entries'), dict):
            return None
        matches = []
        source_identity = SimpleNamespace(title=identity.title,
                                          original_title=identity.original_title,
                                          year=year, imdb_id='', tmdb_id='',
                                          raw_title=getattr(identity, 'raw_title', ''))
        for row in data['entries'].values():
            bound = row.get('input') if isinstance(row, dict) else None
            if not isinstance(bound, dict) or str(bound.get('year') or '') != year or title_key(bound.get('title')) != expected:
                continue
            if getattr(identity, 'raw_title', '') and bound.get('raw_title') != identity.raw_title:
                continue
            resolution = _decode_cache_entry(row)
            if resolution is None or not validate_resolution(source_identity, resolution):
                continue
            if any(id_key(bound.get(name), imdb=name == 'imdb_id')
                   and id_key(bound.get(name), imdb=name == 'imdb_id') != getattr(resolution, name)
                   for name in ('imdb_id', 'tmdb_id')):
                continue
            matches.append(resolution)
        ids = {(row.imdb_id, row.tmdb_id) for row in matches}
        if len(ids) != 1:
            return None
        if validate_resolution(identity, matches[0], profiles):
            return matches[0]
        if profiles and validate_resolution(identity, matches[0]):
            revoke_cached_resolution(resolution_input_identity(identity, matches[0]), cache_path)
        return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def resolve(identity, profiles, *, fetch_secondary=None, fetch_public=None,
            cancel_event=None, cache_path=None, max_requests=2, time_budget_seconds=15.0,
            budget=None, allow_exact_title_lookup=False, fetch_primary=None):
    """Return confirmed only after independent exact-year and double-ID proof.

    A profile's shorter prefix proposes a public metadata check, never confirms
    an alias. Same-year competing IDs stay pending when corroboration fails.
    ``cache_path=False`` disables persistence for a caller's isolated trial.
    """
    budget = budget if budget is not None else RequestBudget(max_requests, time_budget_seconds, cancel_event)
    if cancel_event is not None:
        import subtitle_tool_core as core
        core.check_cancel(cancel_event)
    expected = title_key(identity.original_title or identity.title)
    year = str(identity.year or '')
    profiles = tuple(p for p in profiles if isinstance(p, WorkProfile))
    canonical_year = year
    year_record = None
    fetch_secondary = fetch_secondary or fetch_secondary_catalogue
    fetch_public = fetch_public or fetch_public_movie

    def filename_numeric_prefix(actual):
        # This entry is only used for a bound filename tail-year proposal.
        # A numeric catalogue short name still needs the unchanged public
        # complete-title/alias/tagline proof below; prefix alone proves nothing.
        return (allow_exact_title_lookup and len(actual) == 1 and actual[0].isdigit()
                and len(expected) > 1 and expected[:1] == actual)

    def finish(state, reason, proof=None, evidence=()):
        if cancel_event is not None:
            import subtitle_tool_core as core
            core.check_cancel(cancel_event)
        if state == 'confirmed' and getattr(identity, 'raw_title', ''):
            evidence = ({'acquisition': 'filename-space-tail-proposal',
                         'raw_title': str(identity.raw_title),
                         'proposed_title': str(identity.original_title or identity.title),
                         'year': year}, *evidence)
        if state == 'confirmed' and getattr(identity, 'known_year_alias_entry', False):
            evidence = ({'acquisition': 'known-year-complete-title-alias-entry',
                         'title': str(identity.original_title or identity.title),
                         'year': year}, *evidence)
        result = Resolution(state, reason, proof.title if proof else '', canonical_year,
                            id_key(proof.imdb_id, imdb=True) if proof else '',
                            id_key(proof.tmdb_id) if proof else '', tuple(evidence),
                            tuple(budget.attempts), budget.request_count,
                            time.monotonic() - budget.started)
        if (state == 'confirmed' and allow_exact_title_lookup
                and not validate_resolution(identity, result, profiles)):
            return Resolution('pending', '完整片名同年目录ID或证据绑定冲突，未发布或缓存确认结果',
                              year=year, attempts=tuple(budget.attempts),
                              request_count=budget.request_count,
                              elapsed_seconds=time.monotonic() - budget.started)
        if state == 'confirmed':
            _write_cache(cache_path, cache_key, result, identity)
        return result

    if not expected or not re.fullmatch(r'\d{4}', year):
        return finish('pending', '完整片名或明确发行年份不足，身份补证未启动')
    eligible = []
    for p in profiles:
        actual = title_key(p.title)
        if p.year != year or p.feature_type.casefold() not in {'movie', 'feature', ''}:
            continue
        exact = actual == expected or expected in [title_key(a) for a in p.aliases if a]
        prefix = ((len(actual) >= 2 or filename_numeric_prefix(actual))
                  and len(actual) < len(expected) and expected[:len(actual)] == actual)
        if (exact or prefix) and id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id):
            eligible.append((p, exact))
    native = bool(re.search(r'[\u3400-\u9fff\U00020000-\U0003134f]', identity.original_title or identity.title))
    if not eligible and not native and not allow_exact_title_lookup:
        return finish('pending', '现有目录没有带双ID的完整片名或可核查别名提议')
    # Do not access or create cache/config paths for an explicitly wrong-year
    # or otherwise ineligible catalogue response.
    if cache_path is None:
        import online_subtitles
        cache_path = online_subtitles.settings_path().with_name('work-identity-evidence.json')
    cache_key = _cache_key(identity, profiles)
    cached = _read_cache(cache_path, cache_key)
    if cached and validate_resolution(identity, cached, profiles):
        budget.check()
        return cached
    # Select a query spelling, not a film: the independent response must still
    # prove one exact title/year double-ID mapping below.
    proposal, was_exact = max(eligible, key=lambda pair: (pair[1], len(title_key(pair[0].title)))) if eligible else (None, False)
    proposed_title = re.sub(r'^\s*(?:19|20)\d{2}\s+[-–—]\s+', '', proposal.title).strip() if proposal else (identity.original_title or identity.title)
    query = proposed_title
    if proposal is not None and not was_exact:
        proposed_rows = [p for p in profiles if p.year == year and title_key(p.title) == title_key(proposed_title)]
        proposed_ids = {id_key(p.imdb_id, imdb=True) for p in proposed_rows if id_key(p.imdb_id, imdb=True)}
        if len(proposed_ids) != 1:
            return finish('pending', '短名提议本身存在竞争作品ID，不能自动缩名确认')
        # Include the original release year in this catalogue query, without
        # assuming its returned first record or a chosen-ID page is unique.
        query = ((identity.original_title or identity.title)
                 if getattr(identity, 'known_year_alias_entry', False)
                 else proposed_title + ' ' + year)
    try:
        id_profile = None
        numeric_query = None
        if allow_exact_title_lookup:
            id_profile, primary_conflict = select_filename_primary_profile(identity, profiles)
            if primary_conflict:
                return finish('pending', primary_conflict)
            if id_profile is None and len(expected) == 1 and expected[0].isdigit():
                from subtitle_identity_guard import numeric_title_alias_proposals
                readings = numeric_title_alias_proposals(identity.original_title or identity.title)
                if not readings:
                    return finish('pending', '数字片名没有可核查的完整读法提案；保留原数字片名')
                numeric_query = query = readings[0]
            if id_profile is not None:
                # All same-title/year primary records have already been checked;
                # this is a corroboration query, never picking a first film ID.
                proposal, was_exact = id_profile, True
                proposed_title = id_profile.title
                query = 'tt' + id_key(id_profile.imdb_id, imdb=True).zfill(7)
                eligible = [*eligible, (id_profile, True)]
        response = budget.request('独立目录片名/年份交叉核对', lambda: fetch_secondary(query, budget))
        all_secondary = _secondary_profiles(response)
        if len(response.get('results') or []) >= CATALOGUE_LIMIT:
            return finish('pending', '独立目录结果达到分页上限，不能证明身份唯一')
        # A display such as "English title (exact original_name)" is only
        # a proposal. Keep the provider's original aliases unmodified and
        # require an independent official page to prove both full names.
        literal = [p for p in all_secondary if p.year == year and p.feature_type.casefold() == 'movie'
                   and expected in {title_key(p.title), *(title_key(a) for a in p.aliases)}
                   and id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id)]
        combined = (_combined_title_profile(identity, all_secondary)
                    if allow_exact_title_lookup and id_profile is None and numeric_query is None and not literal else None)
        if combined is not None:
            if not secondary_response_complete(response):
                return finish('pending', '复合片名目录未完整加载，未提出正式别名确认')
            if _profile_conflict(profiles, combined.imdb_id, combined.tmdb_id, year):
                return finish('pending', '复合片名提案与已观察目录ID、年份或用途冲突')
            budget.public_title, budget.public_titles = combined.title, True
            page = budget.request('复合显示片名：公开正式完整别名核对',
                lambda: fetch_public(combined.tmdb_id, budget))
            public = parse_public_movie_titles(page, combined.tmdb_id)
            names = {title_key(public['title']), *(title_key(a) for a in public['aliases'])}
            if public['year'] != year or expected not in names or title_key(combined.title) not in names:
                return finish('pending', '正式同ID电影别名表未同时确认完整输入片名、原名和准确年份')
            evidence = [{'title': combined.title, 'year': year, 'imdb_id': combined.imdb_id,
                'tmdb_id': combined.tmdb_id, 'aliases': list(combined.aliases),
                'structured_title_proposal_required': True,
                'source': 'https://api.subdl.com/api/v2/movies/search',
                'acquisition': 'https-subdl-v2-unique-title-year-double-id'},
                {'acquisition': 'subdl-structured-combined-title-proposal',
                 'input_title': str(identity.original_title or identity.title), 'input_year': year,
                 'display_name': _combined_display_name(identity, combined), 'original_name': combined.title,
                 'catalogue_profiles': [asdict(p) for p in all_secondary],
                 'catalogue_metadata': {name: response[name] for name in (
                     'next', 'next_page', 'has_more', 'truncated', 'total_pages', 'total_count',
                     'total_results', 'total_entries', 'total', 'count') if name in response}}, public]
            return finish('confirmed', '复合显示名仅作提案；正式同ID别名表同时确认完整片名、原名与年份', combined, evidence)
        if numeric_query is not None:
            if not secondary_response_complete(response):
                return finish('pending', '数字读法目录响应不完整，不能证明唯一作品身份')
            proof = _numeric_alias_profile(identity, numeric_query, all_secondary)
            if proof is None:
                return finish('pending', '首个完整数字读法未取得无冲突的同年唯一双ID；保留原数字片名')
            if _profile_conflict(profiles, proof.imdb_id, proof.tmdb_id, year):
                return finish('pending', '数字读法提案与现有目录ID、年份或用途冲突')
            budget.public_title, budget.public_titles = proof.title, True
            page = budget.request('数字原题：公开Alternative Titles正式别名核对',
                lambda: fetch_public(proof.tmdb_id, budget))
            public = parse_public_movie_titles(page, proof.tmdb_id)
            if (public['year'] != year
                    or title_key(public['title']) not in {title_key(proof.title), *(title_key(a) for a in proof.aliases)}
                    or expected not in {title_key(a) for a in public['aliases']}):
                return finish('pending', '正式同ID电影别名表未确认原数字片名及准确年份')
            evidence = [
                {'title': proof.title, 'year': proof.year, 'imdb_id': proof.imdb_id,
                 'tmdb_id': proof.tmdb_id, 'aliases': list(proof.aliases),
                 'source': 'https://api.subdl.com/api/v2/movies/search',
                 'acquisition': 'https-subdl-v2-unique-title-year-double-id'},
                {'acquisition': 'numeric-title-alias-proposal',
                 'original_title': str(identity.original_title or identity.title), 'input_year': year,
                 'query': numeric_query, 'catalogue_profiles': [asdict(p) for p in all_secondary],
                 'catalogue_metadata': {name: response[name] for name in (
                     'next', 'next_page', 'has_more', 'truncated', 'total_pages', 'total_count',
                     'total_results', 'total_entries', 'total', 'count') if name in response}},
                public]
            return finish('confirmed', '完整数字读法仅作检索提案；唯一双ID及正式别名表确认原数字片名，原输入与缓存绑定保持',
                          proof, evidence)
        primary_names = ({title_key(id_profile.title), *(title_key(a) for a in id_profile.aliases)}
                         if id_profile is not None else set())
        if id_profile is not None:
            expected_imdb = id_key(id_profile.imdb_id, imdb=True)
            expected_tmdb = id_key(id_profile.tmdb_id)
            related_profiles = []
            for p in all_secondary:
                names = {title_key(p.title), *(title_key(a) for a in p.aliases)}
                related = (id_key(p.imdb_id, imdb=True) == expected_imdb
                           or id_key(p.tmdb_id) == expected_tmdb
                           or bool(names & primary_names))
                if related and (id_key(p.imdb_id, imdb=True) != expected_imdb
                                or id_key(p.tmdb_id) != expected_tmdb
                                or p.feature_type.casefold() != 'movie' or not names & primary_names):
                    return finish('pending', '独立目录按ID复核返回缺失/冲突ID、年份、用途或完整题名，未采纳')
                if related:
                    related_profiles.append(p)
            observed_years = {p.year for p in related_profiles}
            if observed_years and observed_years != {year}:
                if not secondary_response_complete(response):
                    return finish('pending', '独立目录未完整加载，不以部分记录纠正目录年份')
                if (len(observed_years) != 1 or not re.fullmatch(r'(?:19|20)\d{2}', next(iter(observed_years)))
                        or abs(int(next(iter(observed_years))) - int(year)) != 1):
                    return finish('pending', '同双ID目录年份差异超出受证据约束的相邻年范围，未更改原年份')
                secondary_year = next(iter(observed_years))
                budget.public_title = id_profile.title
                budget.public_titles = False
                page = budget.request('同双ID目录年份差异：正式电影主年份核对',
                    lambda: fetch_public(expected_tmdb, budget))
                official = parse_public_movie(page, expected_tmdb)
                source_names = {title_key(related_profiles[0].title),
                                *(title_key(a) for a in related_profiles[0].aliases)}
                if (official['year'] != secondary_year
                        or title_key(official['title']) not in primary_names & source_names):
                    return finish('pending', '正式电影条目的完整片名或主年份未确认该目录年份纠正')
                canonical_year = secondary_year
                year_record = {'acquisition': 'https-public-tmdb-catalogue-year-correction',
                    'input_year': year, 'canonical_year': canonical_year,
                    'primary_profile': asdict(id_profile), 'secondary_profile': asdict(related_profiles[0]),
                    'official': official, 'official_type': 'movie',
                    'source_years': {'input': [year], 'opensubtitles-features': [year],
                                     'subdl-v2': [canonical_year]}}
        secondary = [p for p in all_secondary
                     if p.year == canonical_year and p.feature_type.casefold() == 'movie'
                     and (proposal is None or title_key(proposed_title) in {title_key(p.title), *(title_key(a) for a in p.aliases)}
                          or (id_profile is not None and bool(primary_names & {title_key(p.title), *(title_key(a) for a in p.aliases)})))
                     and (not allow_exact_title_lookup or any(
                         key == expected or filename_numeric_prefix(key) or key in primary_names
                         for key in {title_key(p.title), *(title_key(a) for a in p.aliases)}))
                     and id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id)]
        mappings = {(id_key(p.imdb_id, imdb=True), id_key(p.tmdb_id)) for p in secondary}
        if len(mappings) != 1:
            return finish('pending', '独立目录未确认唯一完整片名、年份及双ID映射')
        proof = secondary[0]
        matching = [(p, exact) for p, exact in eligible
                    if id_key(p.imdb_id, imdb=True) == proof.imdb_id and id_key(p.tmdb_id) == proof.tmdb_id]
        if not matching and proposal is not None:
            return finish('pending', '独立目录双ID与原候选提议不一致，未采纳')
        if _profile_conflict((*profiles, *all_secondary), proof.imdb_id, proof.tmdb_id,
                             canonical_year, year_evidence=year_record):
            return finish('pending', '目录存在同ID异映射、年份或用途冲突，未自动合并')
        # A same-title/year competing single-ID secondary row is not silently
        # discarded merely because another row offers both IDs.
        related_secondary = [p for p in all_secondary if p.year == canonical_year
                             and (title_key(proposed_title) in {title_key(p.title), *(title_key(a) for a in p.aliases)}
                                  or (allow_exact_title_lookup and title_key(proof.title)
                                      in {title_key(p.title), *(title_key(a) for a in p.aliases)}))]
        if any((id_key(p.imdb_id, imdb=True) and id_key(p.imdb_id, imdb=True) != proof.imdb_id)
               or (id_key(p.tmdb_id) and id_key(p.tmdb_id) != proof.tmdb_id) for p in related_secondary):
            return finish('pending', '独立目录同名同年仍有竞争作品ID，未自动确认')
        for name, actual in (('imdb_id', proof.imdb_id), ('tmdb_id', proof.tmdb_id)):
            known = id_key(getattr(identity, name, ''), imdb=name == 'imdb_id')
            if known and known != actual:
                return finish('pending', '独立目录与本地已确认作品ID冲突')
        evidence = [{'title': proof.title, 'year': proof.year, 'imdb_id': proof.imdb_id,
                     'tmdb_id': proof.tmdb_id, 'aliases': list(proof.aliases),
                     'source': 'https://api.subdl.com/api/v2/movies/search',
                     'acquisition': 'https-subdl-v2-unique-title-year-double-id'}]
        if id_profile is not None:
            evidence.insert(0, {'title': id_profile.title, 'year': year,
                'imdb_id': id_key(id_profile.imdb_id, imdb=True), 'tmdb_id': id_key(id_profile.tmdb_id),
                'aliases': list(id_profile.aliases), 'source': 'https://api.opensubtitles.com/api/v1/features',
                'acquisition': 'https-opensubtitles-exact-title-year-double-id',
                'profiles': [asdict(p) for p in profiles if p.source == 'opensubtitles-features']})
            if year_record is not None:
                evidence.append(year_record)
                return finish('confirmed', f'同标题及唯一双ID一致；原目录/输入年{year}与独立目录年{canonical_year}不同，正式同ID电影主年确认{canonical_year}；保留原年并按官方年继续',
                              proof, evidence)
            return finish('confirmed', '主目录完整题名/别名与年份唯一双ID，独立目录按ID复核一致', proof, evidence)
        literal_alias = any(expected == title_key(value) for value in (proof.title, *proof.aliases))
        if literal_alias:
            return finish('confirmed', '独立目录确认完整片名、原年份和一致双ID', proof, evidence)
        if native:
            budget.public_language = 'zh-CN'
        budget.public_title = proof.title
        budget.public_titles = bool(allow_exact_title_lookup)
        page = budget.request('公开电影条目完整别名/宣传语核对', lambda: fetch_public(proof.tmdb_id, budget))
        public = (parse_public_movie_titles(page, proof.tmdb_id) if budget.public_titles
                  else parse_public_movie(page, proof.tmdb_id))
        if public['year'] != year or (not native and title_key(public['title']) not in {title_key(proof.title), *(title_key(a) for a in proof.aliases)}):
            return finish('pending', '公开条目片名或原始年份与独立目录不一致')
        exact_alias = expected in {title_key(public['title']), *(title_key(a) for a in public['aliases'])}
        exact_tagline = bool(public['tagline'] and expected == title_key(public['title'] + ' ' + public['tagline']))
        if not exact_alias and not exact_tagline:
            return finish('pending', '完整本地片名不等于公开别名或正片名加完整宣传语')
        evidence.append(public)
        reason = ('独立双ID交叉确认，完整片名与公开Alternative Titles正式别名逐词对应'
                  if budget.public_titles else '独立双ID交叉确认，完整本地片名与公开别名/宣传语逐词对应')
        return finish('confirmed', reason, proof, evidence)
    except Exception as error:
        # Cancellation remains the application's original exception, never
        # hidden in a pending result. Errors log only a type, not credentials.
        if cancel_event is not None and cancel_event.is_set():
            import subtitle_tool_core as core
            core.check_cancel(cancel_event)
        import online_subtitles
        if isinstance(error, (online_subtitles.SubtitleAuthenticationError, online_subtitles.SubtitleQuotaError)):
            raise
        reason = str(error) if isinstance(error, EvidenceBudgetExceeded) else f'作品元数据补证未完成（{type(error).__name__}），保留待确认'
        return finish('pending', reason)
