"""Bounded, independently corroborated film identity recovery.

Catalogue rows are proposals.  A shorter name never proves an alias: it needs
the public movie record's complete tagline, or a literal published alias.
This module never downloads a subtitle and does not relax timing acceptance.
"""
from __future__ import annotations

import hashlib
import html
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
    result = json.loads(_read_response(request, budget))
    if not isinstance(result, dict):
        raise RuntimeError('独立目录响应格式无效')
    return result


def fetch_public_movie(tmdb_id, budget):
    key = id_key(tmdb_id)
    if not key:
        raise RuntimeError('缺少有效TMDB ID')
    language = getattr(budget, 'public_language', 'en-US')
    title = getattr(budget, 'public_title', '')
    slug = re.sub(r'[^a-z0-9]+', '-', unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode().casefold()).strip('-')
    url = 'https://www.themoviedb.org/movie/' + key + ('-' + slug if slug else '') + '?' + urllib.parse.urlencode({'language': language})
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


def _cache_key(identity, profiles):
    rows = sorted({(title_key(p.title), p.year, id_key(p.imdb_id, imdb=True), id_key(p.tmdb_id), p.feature_type,
                    tuple(sorted(title_key(value) for value in p.aliases)))
                   for p in profiles})
    data = [SCHEMA_VERSION, title_key(identity.original_title or identity.title),
            str(identity.year or ''), id_key(getattr(identity, 'imdb_id', ''), imdb=True),
            id_key(getattr(identity, 'tmdb_id', '')), rows]
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


def _profile_conflict(profiles, imdb_id, tmdb_id, year):
    for profile in profiles:
        imdb = id_key(profile.imdb_id, imdb=True)
        tmdb = id_key(profile.tmdb_id)
        if not ((imdb and imdb == imdb_id) or (tmdb and tmdb == tmdb_id)):
            continue
        if (imdb and imdb != imdb_id) or (tmdb and tmdb != tmdb_id):
            return True
        if profile.year and profile.year != year:
            return True
        if profile.feature_type.casefold() not in {'movie', 'feature', ''}:
            return True
    return False


def _cache_is_bound(identity, profiles, result):
    if result.year != str(identity.year or '') or not result.imdb_id or not result.tmdb_id:
        return False
    expected = title_key(identity.original_title or identity.title)
    secondary = [entry for entry in result.evidence if entry.get('acquisition') == 'https-subdl-v2-unique-title-year-double-id']
    if len(secondary) != 1:
        return False
    entry = secondary[0]
    if entry.get('source') != 'https://api.subdl.com/api/v2/movies/search':
        return False
    if (entry.get('year') != result.year or id_key(entry.get('imdb_id'), imdb=True) != result.imdb_id
            or id_key(entry.get('tmdb_id')) != result.tmdb_id or title_key(entry.get('title')) != title_key(result.title)):
        return False
    if _profile_conflict(profiles, result.imdb_id, result.tmdb_id, result.year):
        return False
    for name in ('imdb_id', 'tmdb_id'):
        known = id_key(getattr(identity, name, ''), imdb=name == 'imdb_id')
        if known and known != getattr(result, name):
            return False
    aliases = (entry.get('title'), *(entry.get('aliases') or ()))
    if expected in {title_key(value) for value in aliases if isinstance(value, str)}:
        return True
    for public in result.evidence:
        if public.get('acquisition') != 'https-public-tmdb-movie-schema':
            continue
        if public.get('year') != result.year or id_key(public.get('tmdb_id')) != result.tmdb_id:
            continue
        source = urllib.parse.urlparse(str(public.get('source') or ''))
        if source.scheme != 'https' or source.hostname not in {'www.themoviedb.org', 'themoviedb.org'} or not re.match(r'^/movie/' + re.escape(result.tmdb_id) + r'(?:-|/|$)', source.path):
            continue
        public_aliases = (public.get('title'), *(public.get('aliases') or ()))
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


def read_cached_resolution(identity, profiles=(), cache_path=None):
    """Read a complete-input proof before repeating broad catalogue queries.

    This is read-only and does not turn a partial title/profile into an alias.
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
                                          year=year, imdb_id='', tmdb_id='')
        for row in data['entries'].values():
            bound = row.get('input') if isinstance(row, dict) else None
            if not isinstance(bound, dict) or str(bound.get('year') or '') != year or title_key(bound.get('title')) != expected:
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
        return matches[0] if validate_resolution(identity, matches[0], profiles) else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def resolve(identity, profiles, *, fetch_secondary=None, fetch_public=None,
            cancel_event=None, cache_path=None, max_requests=2, time_budget_seconds=15.0):
    """Return confirmed only after independent exact-year and double-ID proof.

    A profile's shorter prefix proposes a public metadata check, never confirms
    an alias. Same-year competing IDs stay pending when corroboration fails.
    ``cache_path=False`` disables persistence for a caller's isolated trial.
    """
    budget = RequestBudget(max_requests, time_budget_seconds, cancel_event)
    if cancel_event is not None:
        import subtitle_tool_core as core
        core.check_cancel(cancel_event)
    expected = title_key(identity.original_title or identity.title)
    year = str(identity.year or '')
    profiles = tuple(p for p in profiles if isinstance(p, WorkProfile))
    fetch_secondary = fetch_secondary or fetch_secondary_catalogue
    fetch_public = fetch_public or fetch_public_movie

    def finish(state, reason, proof=None, evidence=()):
        if cancel_event is not None:
            import subtitle_tool_core as core
            core.check_cancel(cancel_event)
        result = Resolution(state, reason, proof.title if proof else '', year,
                            id_key(proof.imdb_id, imdb=True) if proof else '',
                            id_key(proof.tmdb_id) if proof else '', tuple(evidence),
                            tuple(budget.attempts), budget.request_count,
                            time.monotonic() - budget.started)
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
        prefix = len(actual) >= 2 and len(actual) < len(expected) and expected[:len(actual)] == actual
        if (exact or prefix) and id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id):
            eligible.append((p, exact))
    native = bool(re.search(r'[\u3400-\u9fff\U00020000-\U0003134f]', identity.original_title or identity.title))
    if not eligible and not native:
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
        query = proposed_title + ' ' + year
    try:
        response = budget.request('独立目录片名/年份交叉核对', lambda: fetch_secondary(query, budget))
        all_secondary = _secondary_profiles(response)
        if len(response.get('results') or []) >= CATALOGUE_LIMIT:
            return finish('pending', '独立目录结果达到分页上限，不能证明身份唯一')
        secondary = [p for p in all_secondary
                     if p.year == year and p.feature_type.casefold() == 'movie'
                     and (proposal is None or title_key(proposed_title) in {title_key(p.title), *(title_key(a) for a in p.aliases)})
                     and id_key(p.imdb_id, imdb=True) and id_key(p.tmdb_id)]
        mappings = {(id_key(p.imdb_id, imdb=True), id_key(p.tmdb_id)) for p in secondary}
        if len(mappings) != 1:
            return finish('pending', '独立目录未确认唯一完整片名、年份及双ID映射')
        proof = secondary[0]
        matching = [(p, exact) for p, exact in eligible
                    if id_key(p.imdb_id, imdb=True) == proof.imdb_id and id_key(p.tmdb_id) == proof.tmdb_id]
        if not matching and proposal is not None:
            return finish('pending', '独立目录双ID与原候选提议不一致，未采纳')
        if _profile_conflict((*profiles, *all_secondary), proof.imdb_id, proof.tmdb_id, year):
            return finish('pending', '目录存在同ID异映射、年份或用途冲突，未自动合并')
        # A same-title/year competing single-ID secondary row is not silently
        # discarded merely because another row offers both IDs.
        related_secondary = [p for p in all_secondary if p.year == year
                             and title_key(proposed_title) in {title_key(p.title), *(title_key(a) for a in p.aliases)}]
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
        literal_alias = any(expected == title_key(value) for value in (proof.title, *proof.aliases))
        if literal_alias:
            return finish('confirmed', '独立目录确认完整片名、原年份和一致双ID', proof, evidence)
        if native:
            budget.public_language = 'zh-CN'
        budget.public_title = proof.title
        page = budget.request('公开电影条目完整别名/宣传语核对', lambda: fetch_public(proof.tmdb_id, budget))
        public = parse_public_movie(page, proof.tmdb_id)
        if public['year'] != year or (not native and title_key(public['title']) not in {title_key(proof.title), *(title_key(a) for a in proof.aliases)}):
            return finish('pending', '公开条目片名或原始年份与独立目录不一致')
        exact_alias = expected in {title_key(public['title']), *(title_key(a) for a in public['aliases'])}
        exact_tagline = bool(public['tagline'] and expected == title_key(public['title'] + ' ' + public['tagline']))
        if not exact_alias and not exact_tagline:
            return finish('pending', '完整本地片名不等于公开别名或正片名加完整宣传语')
        evidence.append(public)
        return finish('confirmed', '独立双ID交叉确认，完整本地片名与公开别名/宣传语逐词对应', proof, evidence)
    except Exception as error:
        # Cancellation remains the application's original exception, never
        # hidden in a pending result. Errors log only a type, not credentials.
        if cancel_event is not None and cancel_event.is_set():
            import subtitle_tool_core as core
            core.check_cancel(cancel_event)
        reason = str(error) if isinstance(error, EvidenceBudgetExceeded) else f'作品元数据补证未完成（{type(error).__name__}），保留待确认'
        return finish('pending', reason)
