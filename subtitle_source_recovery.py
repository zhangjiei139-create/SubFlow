"""Candidate-scoped search recovery. No catalogue ID or global proof is changed."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata
import urllib.error

import online_subtitles as online
import subdl_subtitles as subdl
import subtitle_tool_core as core
import work_identity_resolver as work
from subtitle_identity_guard import media_signature

SCHEMA = 'candidate-source-continuation-v1'
KEY_PREFIX = 'candidate-source:'


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), default=str).encode()).hexdigest()


def _words(value):
    return tuple(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', str(value or '')).casefold()))


def camel_boundary_proposal(value):
    original = str(value or '')
    proposed = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', original)
    return proposed if proposed != original else None


def eligible_input(identity):
    title = str(getattr(identity, 'original_title', '') or getattr(identity, 'title', '') or '')
    return bool(re.fullmatch(r'\d{4}', str(getattr(identity, 'year', '') or '')) and
                (camel_boundary_proposal(title) or re.fullmatch(r'[1-9]\d*', title)))


def runtime_applicable(identity, candidates, meta=None):
    title = str(getattr(identity, 'original_title', '') or getattr(identity, 'title', '') or '')
    year = str(getattr(identity, 'year', '') or '')
    if not re.fullmatch(r'\d{4}', year):
        return False
    if (not re.fullmatch(r'[1-9]\d*', title) and
            getattr(meta, 'feature_lookup_status', '') != 'ambiguous'):
        return False
    for candidate in candidates:
        actual = str(getattr(candidate, 'feature_title', '') or '')
        candidate_year = str(getattr(candidate, 'feature_year', '') or '')
        prefix = re.match(r'^\s*((?:19|20)\d{2})\s+[-–—]\s+(.+)$', actual)
        if prefix and prefix.group(1) != candidate_year:
            continue
        if prefix and prefix.group(1) == candidate_year == year:
            actual = prefix.group(2)
        if (candidate_year == year and online._titles_equivalent(actual, title) and
                getattr(candidate, 'identity_admission_state', '') == 'pending'):
            return True
    return False


def _input(movie, identity):
    return dict(title=str(identity.original_title or identity.title or ''),
                year=str(identity.year or ''), movie_signature=media_signature(movie),
                movie_path=str(Path(movie).resolve()).casefold(),
                imdb_id=work.id_key(getattr(identity, 'imdb_id', ''), imdb=True),
                tmdb_id=work.id_key(getattr(identity, 'tmdb_id', '')))


def candidate_snapshot(provider, candidate):
    fields = ('file_id', 'file_name', 'release', 'language', 'feature_title', 'feature_year',
              'feature_imdb_id', 'feature_tmdb_id', 'feature_type', 'feature_episode_number',
              'feature_season_number', 'feature_parent_imdb_id', 'feature_parent_tmdb_id',
              'identity_admission_state', 'identity_verified',
              'identity_key')
    value = {name: getattr(candidate, name, None) for name in fields}
    value['provider'] = provider
    value['download_url_sha256'] = hashlib.sha256(
        str(getattr(candidate, 'download_url', '')).encode()).hexdigest()
    return value


def _parent_values(parent, proposed):
    return dict(feature_title=next(str(parent[n]) for n in ('name', 'original_name')
                                  if _words(parent.get(n)) == _words(proposed)),
                feature_year=str(parent['year']), feature_type='movie',
                feature_imdb_id=str(parent.get('imdb_id') or ''),
                feature_tmdb_id=str(parent.get('tmdb_id') or ''))


def _feature_conflict(row, parent):
    for name, expected in parent.items():
        actual = row.get(name)
        if actual in (None, '') or expected in (None, ''):
            continue
        if name == 'feature_title':
            equal = _words(actual) == _words(expected)
        elif name in ('feature_imdb_id', 'feature_tmdb_id'):
            equal = work.id_key(actual, imdb=name == 'feature_imdb_id') == work.id_key(
                expected, imdb=name == 'feature_imdb_id')
        else:
            equal = str(actual).casefold() == str(expected).casefold()
        if not equal:
            return name
    return ''


def matching_parents(response, title, year):
    """Provider parent numbers are provenance, never a count of real works."""
    rows = response.get('results') if isinstance(response, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return []
    result, seen = [], set()
    for parent in rows:
        sd = subdl._subdl_id_key(parent.get('sd_id'))
        if not sd or sd in seen:
            continue
        if (str(parent.get('year') or '') != str(year) or
                _words(title) not in {_words(parent.get(n)) for n in ('name', 'original_name')}):
            continue
        seen.add(sd)
        related = [row for row in rows if subdl._subdl_id_key(row.get('sd_id')) == sd]
        if any(str(row.get('year') or '') != str(year) or
               str(row.get('type') or '').casefold() != 'movie' or
               _words(title) not in {_words(row.get(n)) for n in ('name', 'original_name')}
               for row in related):
            continue
        if any(len({work.id_key(row.get(name), imdb=name == 'imdb_id') for row in related
                    if row.get(name)}) > 1 for name in ('imdb_id', 'tmdb_id')):
            continue
        result.append(parent)
    return result


def list_echo_ids(response):
    """Project only identifiers actually returned in the current list echo."""
    result=[]
    for name in ('results', 'movie', 'movies', 'feature'):
        if response.get(name) is not None:
            values=response[name] if isinstance(response[name],list) else [response[name]]
            for row in values:
                if not isinstance(row,dict):
                    return []
                result.append({field:row.get(field) for field in ('sd_id','imdb_id','tmdb_id')})
    return result


def echo_ids_match_input(identity, parent, echoes):
    if not isinstance(echoes,list) or not echoes:
        return False
    for row in echoes:
        if (not isinstance(row,dict) or not subdl._subdl_id_key(row.get('sd_id')) or
                subdl._subdl_id_key(row.get('sd_id')) != subdl._subdl_id_key(parent.get('sd_id'))):
            return False
        for field in ('imdb_id','tmdb_id'):
            actual=row.get(field)
            for expected in (getattr(identity,field,''),parent.get(field)):
                if (expected and actual and work.id_key(expected,imdb=field=='imdb_id') !=
                        work.id_key(actual,imdb=field=='imdb_id')):
                    return False
    return True


def list_conflict(response, parent, proposed, year, *, input_identity=None):
    """Require a current parent echo; don't hide explicit raw-file conflicts."""
    if not isinstance(response, dict):
        return 'invalid-list'
    echoes = []
    for name in ('results', 'movie', 'movies', 'feature'):
        if response.get(name) is not None:
            value = response[name]
            echoes.extend(value if isinstance(value, list) else [value])
    if not echoes:
        return 'missing-parent-echo'
    for row in echoes:
        if (not isinstance(row, dict) or
                subdl._subdl_id_key(row.get('sd_id')) != subdl._subdl_id_key(parent.get('sd_id')) or
                str(row.get('year') or '') != str(year) or
                str(row.get('type') or '').casefold() != 'movie' or
                _words(proposed) not in {_words(row.get(n)) for n in ('name', 'original_name')}):
            return 'parent-echo-conflict'
        if any(row.get(name) and parent.get(name) and
               work.id_key(row[name], imdb=name == 'imdb_id') !=
               work.id_key(parent[name], imdb=name == 'imdb_id')
               for name in ('imdb_id', 'tmdb_id')):
            return 'parent-ID-conflict'
    if input_identity is not None and not echo_ids_match_input(input_identity,parent,list_echo_ids(response)):
        return 'list-echo-input-ID-conflict'
    values = _parent_values(parent, proposed)
    rows = response.get('subtitles') or []
    rows = [rows] if isinstance(rows, dict) else rows
    for row in rows:
        if not isinstance(row, dict):
            continue
        children = row.get('unpack_files') or []
        children = [children] if isinstance(children, dict) else children
        if any(_feature_conflict(item, values) for item in [row, *children] if isinstance(item, dict)):
            return 'raw-file-parent-conflict'
    return ''


def _receipt(movie, identity, provider, candidate, mode, proposal, evidence, parent=None):
    value = dict(schema=SCHEMA, mode=mode, input=_input(movie, identity),
                 candidate=candidate_snapshot(provider, candidate), proposal_title=proposal,
                 evidence=evidence, parent=parent, global_identity_confirmed=False,
                 may_merge_ids=False, may_inherit_alignment=False)
    value['digest'] = _hash(value)
    return value


def qualification_key(receipt, endpoint):
    # This key also seals the actual downloaded bytes/endpoint binding into
    # the original production verification_seal and audio checkpoint.
    return KEY_PREFIX + _hash(dict(receipt=receipt, endpoint=endpoint))


def validate_qualification(movie, identity, provider, candidate, receipt):
    if not isinstance(receipt, dict):
        return False
    unsigned = {k: v for k, v in receipt.items() if k != 'digest'}
    if (receipt.get('schema') != SCHEMA or receipt.get('digest') != _hash(unsigned) or
            receipt.get('input') != _input(movie, identity) or
            receipt.get('candidate') != candidate_snapshot(provider, candidate) or
            receipt.get('global_identity_confirmed') is not False or
            receipt.get('may_merge_ids') is not False or receipt.get('may_inherit_alignment') is not False):
        return False
    if any(str(getattr(candidate, name, '') or '').strip() for name in (
            'feature_season_number','feature_episode_number','feature_parent_imdb_id','feature_parent_tmdb_id')):
        return False
    if any(getattr(identity, name, '') and getattr(candidate, 'feature_' + name, '') and
           work.id_key(getattr(identity, name), imdb=name == 'imdb_id') !=
           work.id_key(getattr(candidate, 'feature_' + name), imdb=name == 'imdb_id')
           for name in ('imdb_id', 'tmdb_id')):
        return False
    if any(getattr(identity, name, '') and receipt.get('parent', {}) and receipt['parent'].get(name) and
           work.id_key(getattr(identity, name), imdb=name == 'imdb_id') !=
           work.id_key(receipt['parent'][name], imdb=name == 'imdb_id')
           for name in ('imdb_id', 'tmdb_id')):
        return False
    if receipt['mode'] == 'camel':
        proposal = camel_boundary_proposal(receipt['input']['title'])
        parent = receipt.get('parent')
        if (provider != 'SubDL' or proposal != receipt.get('proposal_title') or not isinstance(parent, dict)
                or len(matching_parents({'results': [parent]}, proposal, identity.year)) != 1):
            return False
        return (not _feature_conflict(vars(candidate), _parent_values(parent, proposal)) and
                echo_ids_match_input(identity,parent,receipt.get('evidence',{}).get('list_echo_ids')) and
                all(re.fullmatch(r'[0-9a-f]{64}',str(receipt.get('evidence', {}).get(n,'')))
                    for n in ('catalogue_sha256','list_sha256')))
    if receipt['mode'] == 'runtime':
        from runtime_work_catalogue import candidate_matches_runtime_proposal
        evidence=receipt.get('evidence', {})
        selected=(evidence.get('selected_work') or {}).get('provider_ids') or {}
        if (evidence.get('target') != {'title':receipt['input']['title'],'year':receipt['input']['year']} or
                receipt.get('proposal_title') != receipt['input']['title']):
            return False
        if any(receipt['input'].get(name) and selected.get(name) and
               receipt['input'][name] != work.id_key(selected[name],imdb=name=='imdb_id')
               for name in ('imdb_id','tmdb_id')):
            return False
        return bool(candidate_matches_runtime_proposal(candidate, evidence))
    return False


def comparison_identity(identity, receipt):
    """Only role/release comparison sees the complete proposal; source is unchanged."""
    fields = dict(vars(identity))
    fields.update(title=receipt['proposal_title'], original_title=receipt['proposal_title'])
    return SimpleNamespace(**fields)


def recover_camel(movie, identity, key, session, log, cancel_event=None):
    proposal = camel_boundary_proposal(identity.original_title or identity.title)
    if not proposal or not identity.year or not key:
        return [], {}
    budget = session.evidence_budget()
    try:
        response = budget.request('完整Camel边界片名提案',
                                  lambda: work.fetch_secondary_catalogue(proposal, budget))
    except work.EvidenceBudgetExceeded as error:
        log(f'智能字幕：片名候选补查预算不足：{error}')
        return [], {}
    candidates, receipts = [], {}
    parents = matching_parents(response, proposal, identity.year)
    log(f'智能字幕：连写片名仅提出“{proposal}”；{len(parents)} 个同完整题年目录分别取字幕，未确认全球作品ID。')
    for parent in parents:
        core.check_cancel(cancel_event)
        try:
            listing = subdl._request_v2(key, '/subtitles/search', params={
                'sd_id': parent['sd_id'], 'languages': subdl.LANGUAGE_CODES['en'],
                'subs_per_page': 30, 'unpack': 1})
        except RuntimeError as error:
            cause = error.__cause__
            if isinstance(cause, urllib.error.HTTPError):
                if cause.code in (401, 403):
                    raise online.SubtitleAuthenticationError('SubDL 候选目录访问未获授权，停止补查。') from error
                if cause.code == 429:
                    raise online.SubtitleQuotaError('SubDL 候选目录额度受限，停止补查。') from error
            raise
        if reason := list_conflict(listing, parent, proposal, identity.year,input_identity=identity):
            log(f'智能字幕：目录 {parent["sd_id"]} 候选绑定未通过：{reason}；保留其他来源。')
            continue
        rows = subdl._candidates_from_response(listing, 'en')
        if isinstance(rows, tuple):
            rows = rows[0]
        evidence = dict(catalogue_sha256=_hash(response), list_sha256=_hash(listing),
                        origin_sd_id=str(parent['sd_id']), exhaustive_catalogue_claim=False,
                        list_echo_ids=list_echo_ids(listing))
        for candidate in rows:
            receipt = _receipt(movie, identity, 'SubDL', candidate, 'camel', proposal, evidence, dict(parent))
            if validate_qualification(movie, identity, 'SubDL', candidate, receipt):
                candidates.append(candidate)
                receipts[id(candidate)] = receipt
    return candidates, receipts


def recover_runtime(movie, identity, candidates, session, duration, log, cancel_event=None):
    from runtime_work_catalogue import recover_runtime_work, candidate_matches_runtime_proposal
    evidence = recover_runtime_work(identity.original_title or identity.title, identity.year,
                                    duration, session, cancel_event=cancel_event)
    if evidence.get('state') != 'runtime_proposal':
        log('智能字幕：独立同名作品片长资料不足或有近似竞争，未增加候选资格。')
        return [], {}
    results, receipts = [], {}
    for candidate in candidates:
        if candidate_matches_runtime_proposal(candidate, evidence):
            receipt = _receipt(movie, identity, 'OpenSubtitles', candidate, 'runtime',
                               identity.original_title or identity.title, evidence)
            if validate_qualification(movie, identity, 'OpenSubtitles', candidate, receipt):
                results.append(candidate)
                receipts[id(candidate)] = receipt
    log(f'智能字幕：实际片长仅在已观察同名同年作品中提出来源；{len(results)} 条原候选继续原核验，全球ID仍待确认。')
    return results, receipts


def movie_duration(movie):
    media = core.inspect_media(movie)
    nanoseconds = core.video_track_duration_ns(media) or core.media_duration_ns(media)
    return nanoseconds / 1_000_000_000 if nanoseconds > 0 else None


def endpoint_record(path, duration):
    events = core.parse_subtitle(Path(path))
    maximum = max((core.subtitle_time_to_milliseconds(event.end) / 1000 for event in events), default=None)
    if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
            not math.isfinite(duration) or duration <= 0 or maximum is None):
        raise ValueError('恢复来源缺少真实影片时长或字幕末尾，不能自动粗筛')
    gap = duration - maximum
    if abs(gap) > 840:
        raise ValueError(f'恢复字幕末尾与影片相差 {gap:+.3f} 秒，超过840秒粗筛范围')
    return dict(raw_path=str(Path(path).resolve()), raw_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                duration_seconds=duration, raw_max_end_seconds=maximum, signed_gap_seconds=gap)


def validate_checkpoint(movie, checkpoint):
    scope = checkpoint.get('source_qualification')
    if not isinstance(scope, dict) or not checkpoint.get('identity_key', '').startswith(KEY_PREFIX):
        return False
    receipt, endpoint = scope.get('receipt'), scope.get('endpoint')
    if not isinstance(receipt, dict) or not isinstance(endpoint, dict):
        return False
    candidate = SimpleNamespace(**receipt.get('candidate', {}))
    # The snapshot stores the URL digest separately; no URL or credential is persisted.
    unsigned = {k: v for k, v in receipt.items() if k != 'digest'}
    if (receipt.get('schema') != SCHEMA or receipt.get('digest') != _hash(unsigned) or
            qualification_key(receipt, endpoint) != checkpoint['identity_key'] or
            receipt.get('input', {}).get('movie_signature') != media_signature(movie)):
        return False
    identity = SimpleNamespace(title=receipt['input']['title'], original_title=receipt['input']['title'],
                               year=receipt['input']['year'],imdb_id=receipt['input'].get('imdb_id',''),
                               tmdb_id=receipt['input'].get('tmdb_id',''))
    # Recheck the complete qualification independently of its digest.
    snapshot = dict(receipt['candidate'])
    candidate.download_url = ''
    check = dict(receipt)
    check['candidate'] = candidate_snapshot(snapshot['provider'], candidate)
    check['digest'] = _hash({k: v for k, v in check.items() if k != 'digest'})
    if not validate_qualification(movie, identity, snapshot['provider'], candidate, check):
        return False
    try:
        return endpoint_record(endpoint['raw_path'], endpoint['duration_seconds']) == endpoint
    except (OSError, ValueError, TypeError, KeyError):
        return False
