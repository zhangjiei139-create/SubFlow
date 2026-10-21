"""Independent filename proposal/identity tests; catalogue transport is synthetic."""
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import online_subtitles as online
import smart_subtitles as smart
import subdl_subtitles as subdl
import subtitle_identity_guard as guard
import subtitle_tool_core as core
import work_identity_resolver as resolver
from tests.test_catalogue_exceptions_independent import official_titles


# These are naming/metadata fixtures, not claims of live catalogue availability.
FILMS = [
    ('2010 The Year We Make Contact', '1984'),
    ('2001 A Space Odyssey', '1968'),
    ('1917', '2019'), ('1984', '1956'), ('2012', '2009'), ('1408', '2007'),
    ('Class of 1984', '1982'), ('The Legend of 1900', '1998'),
    ('Blade Runner 2049', '2017'), ('Wonder Woman 1984', '2020'),
    ('Dracula 2000', '2000'), ('Apollo 13', '1995'), ('Se7en', '1995'),
    ('Room', '2015'), ('Contact', '1997'), ('Alien', '1979'), ('Brazil', '1985'),
    ('Moon', '2009'), ('21', '2008'), ('8 Mile', '2002'), ('13 Hours', '2016'),
    ('The Thing', '1982'), ('Dune', '2021'),
    ('Three Billboards Outside Ebbing Missouri', '2017'),
]


def candidate(title='2010 The Year We Make Contact', year='1984', imdb='86837', tmdb='4437',
              number=1, **extra):
    return online.SubtitleCandidate(number, 'generic.srt', f'{title}.{year}.BluRay', 'en',
        1, 1.0, False, False, feature_title=title, feature_year=year,
        feature_imdb_id=imdb, feature_tmdb_id=tmdb, **extra)


def secondary_film(title='2010 The Year We Make Contact', year='1984', imdb='86837', tmdb='4437', **extra):
    row = dict(name=title, original_name=title, year=year, imdb_id=imdb, tmdb_id=tmdb, type='movie')
    row.update(extra)
    return row


def titles_page(*, title='2010', year='1984', aliases=('2010: The Year We Make Contact',),
                canonical='https://www.themoviedb.org/movie/4437-2010/titles',
                og_url='https://www.themoviedb.org/movie/4437-2010', og_title='2010',
                header_id='4437', extra=''):
    rows = ''.join('<tr><td>' + alias + '</td><td></td></tr>' for alias in aliases)
    return ('<html><head><title>2010 (1984) - Alternative Titles</title>'
        '<link rel="canonical" href="' + canonical + '">'
        '<meta property="og:url" content="' + og_url + '">'
        '<meta property="og:title" content="' + og_title + '"></head><body>'
        '<div class="header small first"><section class="header small"><div class="title ott_true">'
        '<h2><a href="/movie/' + header_id + '-2010?language=en-US">' + title + '</a>'
        '<span class="tag release_date">(' + year + ')</span></h2></div></section></div>'
        '<div id="media_v4"><div class="white_column"><section class="panel">'
        '<table class="card releases titles"><thead><tr><th class="title">Title</th>'
        '<th class="type">Type</th></tr></thead><tbody>' + rows + '</tbody></table>'
        '</section></div></div>' + extra + '</body></html>')


class TrailingYearIdentityIndependentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            'LOCALAPPDATA': str(self.root / 'Local'), 'APPDATA': str(self.root / 'Roaming'),
            'SUBFLOW_RUNTIME_DIR': str(self.root / 'runtime')}))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')))
        # V2 adds a bounded exact primary query for a numeric split when the
        # earlier whole-filename lookup did not retain profiles. Keep this new
        # transport synthetic, with the same independently declared fixture IDs.
        def primary_response(title, year, _budget, **_kwargs):
            records = []
            for index, (fixture_title, fixture_year) in enumerate(FILMS, 100):
                if (title, year) == (fixture_title, fixture_year):
                    records.append({'attributes': {'title': title, 'original_title': title,
                        'year': year, 'imdb_id': str(index), 'tmdb_id': str(index + 1000),
                        'feature_type': 'movie'}})
            return {'data': records}
        self.stack.enter_context(patch.object(resolver, 'fetch_primary_catalogue', side_effect=primary_response))

    def proposal(self, name):
        return guard.trailing_filename_year_proposal(name)

    def test_24_different_titles_propose_only_the_final_release_year(self):
        self.assertGreaterEqual(len({title for title, _year in FILMS}), 18)
        for title, year in FILMS:
            with self.subTest(title=title, year=year):
                raw = f'{title} {year}'
                proposal = self.proposal(raw + '.mkv')
                self.assertIsNotNone(proposal)
                self.assertEqual((proposal.title, proposal.year), (title, year))
                self.assertEqual(proposal.raw_title, raw)
                self.assertEqual(proposal.year_confidence, 'inferred')
                self.assertEqual(proposal.year_source, 'filename-space-tail-proposal')

    def test_proposing_does_not_change_legacy_parser_or_claim_a_confirmed_year(self):
        for title, year in FILMS:
            with self.subTest(title=title):
                raw = f'{title} {year}'
                self.proposal(raw + '.mkv')
                self.assertEqual(guard.clean_video_title(raw + '.mkv'), (raw, ''))

    def test_bare_numeric_titles_are_not_their_own_release_year(self):
        for name in ('1917.mkv', '1984.mkv', '2012.mkv', '2001.mkv', '1408.mkv', '21.mkv'):
            with self.subTest(name=name):
                self.assertIsNone(self.proposal(name))
                self.assertEqual(guard.clean_video_title(name), (Path(name).stem, ''))

    def test_complete_title_year_digits_are_preserved_by_legacy_parse(self):
        for title in ('Class of 1984', 'The Legend of 1900', 'Blade Runner 2049',
                      'Wonder Woman 1984', 'Dracula 2000', '2010 The Year We Make Contact'):
            with self.subTest(title=title):
                self.assertEqual(guard.clean_video_title(title + '.mkv'), (title, ''))

    def test_non_filename_or_non_trailing_number_cannot_create_proposal(self):
        for name in ('Movie 1984', 'Movie 1984 Extended.mkv', '2010 The Year We Make Contact.mkv',
                     'Movie.txt', 'Movie 1984.exe', 'Movie 84.mkv', 'Movie 19840.mkv'):
            with self.subTest(name=name):
                self.assertIsNone(self.proposal(name))

    def test_future_title_number_is_not_guessed_as_release_year(self):
        self.assertIsNone(self.proposal('Blade Runner 2049.mkv'))
        self.assertEqual(guard.clean_video_title('Blade Runner 2049.mkv'), ('Blade Runner 2049', ''))

    def test_existing_explicit_year_formats_stay_on_the_old_path(self):
        for name in ('Contact.1997.BluRay.mkv', 'Contact (1997).mkv', '1917.2019.2160p.mkv'):
            with self.subTest(name=name):
                self.assertTrue(guard.parse_media_identity(name, kind='filename').year)
                self.assertIsNone(self.proposal(name))

    def test_only_basename_supplies_trailing_year_proposal(self):
        path = str(self.root / '2026 release folder' / '2010 The Year We Make Contact 1984.mkv')
        proposal = self.proposal(path)
        self.assertEqual((proposal.title, proposal.year), ('2010 The Year We Make Contact', '1984'))

    def test_supported_film_and_subtitle_extensions_preserve_same_proposal(self):
        for suffix in ('.mkv', '.MP4', '.avi', '.srt', '.ass'):
            with self.subTest(suffix=suffix):
                proposal = self.proposal('Contact 1997' + suffix)
                self.assertEqual((proposal.title, proposal.year), ('Contact', '1997'))

    def provider_context(self, provider, title, year='', **identity_fields):
        identity = provider.MediaIdentity(title, original_title=title, year=year, **identity_fields)
        self.stack.enter_context(patch.object(provider, 'identify_media', return_value=identity))
        self.stack.enter_context(patch.object(provider, 'filename_container_identity_conflict', return_value=''))
        if provider is online:
            self.stack.enter_context(patch.object(online, 'opensubtitles_movie_hash', return_value=''))
        return identity

    def test_strict_hash_hit_still_precedes_all_filename_recovery(self):
        title = '2010 The Year We Make Contact 1984'
        self.provider_context(online, title)
        item = candidate(moviehash_match=True)
        with patch.object(online, 'opensubtitles_movie_hash', return_value='0123456789abcdef'), \
                patch.object(online, '_search_pages', return_value=([item], 1, False)) as pages, \
                patch.object(online, '_resolve_feature_identity', side_effect=AssertionError('hash must precede catalogue')), \
                patch.object(online, 'resolve_work_identity', side_effect=AssertionError('hash must precede corroboration')):
            _identity, items, meta = online.search('fixture', title + '.mkv', 'en')
        self.assertEqual(items, [item])
        self.assertEqual(meta.query_mode, 'hash')
        self.assertEqual(pages.call_count, 1)
        self.assertEqual(item.identity_key, 'hash:0123456789abcdef')

    def test_existing_complete_title_identity_wins_before_any_split_corroboration(self):
        for title, year in [('Class of 1984', '1982'), ('The Legend of 1900', '1998'),
                            ('Blade Runner 2049', '2017'), ('1917', '2019'),
                            ('1984', '1956'), ('2012', '2009')]:
            with self.subTest(title=title):
                base = online.MediaIdentity(title, original_title=title)
                confirmed = online.MediaIdentity(title, original_title=title, year=year,
                                                  imdb_id='100', tmdb_id='200')
                item = candidate(title, year, '100', '200')
                queried = []
                def primary(_key, identity, **_kw):
                    queried.append(identity.original_title or identity.title)
                    return confirmed
                with patch.object(online, 'identify_media', return_value=base), \
                        patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                        patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                        patch.object(online, '_resolve_feature_identity', side_effect=primary), \
                        patch.object(online, '_search_pages', return_value=([item], 1, False)), \
                        patch.object(online, 'resolve_work_identity', side_effect=AssertionError('intact title already confirmed')):
                    identity, items, meta = online.search('fixture', title + '.mkv', 'en')
                self.assertEqual(queried, [title])
                self.assertEqual(identity.original_title, title)
                self.assertEqual(identity.year, year)
                self.assertEqual(items, [item])
                self.assertEqual(meta.query_mode, 'feature')

    def test_subdl_existing_complete_numeric_title_never_uses_a_shortened_title(self):
        for title, year in [('Class of 1984', '1982'), ('The Legend of 1900', '1998'),
                            ('Blade Runner 2049', '2017'), ('1917', '2019'),
                            ('1984', '1956'), ('2012', '2009')]:
            with self.subTest(title=title):
                base = subdl.MediaIdentity(title, original_title=title)
                movie = secondary_film(title, year, '100', '200', sd_id='sd123')
                item = subdl.SubtitleCandidate('1', 'generic.srt', f'{title}.{year}.BluRay',
                                               'en', 1, 1., False, False)
                queried = []
                def primary(_key, identity):
                    queried.append(identity.original_title or identity.title)
                    return movie
                with patch.object(subdl, 'identify_media', return_value=base), \
                        patch.object(subdl, 'filename_container_identity_conflict', return_value=''), \
                        patch.object(subdl, '_resolve_movie', side_effect=primary), \
                        patch.object(subdl, '_request_v2', return_value={'subtitles': [{'id': '1'}]}), \
                        patch.object(subdl, '_candidates_from_response', return_value=[item]):
                    identity, items, meta = subdl.search('fixture', title + '.mkv', 'en')
                self.assertEqual(queried, [title])
                self.assertEqual(identity.original_title, title)
                self.assertEqual(items, [item])
                self.assertEqual(item.feature_year, year)
                self.assertEqual(meta.query_mode, 'movie-id')

    def test_primary_catalogue_selectors_protect_complete_title_against_first_short_row(self):
        for title, actual_year in [('Class of 1984', '1982'), ('The Legend of 1900', '1998')]:
            with self.subTest(title=title):
                short, trailing = title.rsplit(' ', 1)
                base = online.MediaIdentity(title, original_title=title)
                rows = [secondary_film(short, trailing, '900', '901', sd_id='sd900'),
                        secondary_film(title, actual_year, '100', '200', sd_id='sd100')]
                response = {'data': [{'id': row['sd_id'], 'attributes': {
                    'title': row['name'], 'original_title': row['original_name'], 'year': row['year'],
                    'imdb_id': row['imdb_id'], 'tmdb_id': row['tmdb_id'], 'feature_type': 'Movie'}}
                    for row in rows]}
                selected = online._feature_identity_from_response(response, base)
                self.assertIsNotNone(selected)
                self.assertEqual((selected.original_title, selected.year, selected.imdb_id),
                                 (title, actual_year, '100'))
                secondary = subdl._choose_movie_without_score(subdl.MediaIdentity(title, original_title=title), rows)
                self.assertIsNotNone(secondary)
                self.assertEqual(secondary['imdb_id'], '100')

    def test_primary_catalogue_selectors_do_not_accept_competing_same_title_ids(self):
        identity = online.MediaIdentity('Contact 1997', original_title='Contact 1997')
        rows = [secondary_film('Contact 1997', '1999', '100', '200', sd_id='sd100'),
                secondary_film('Contact 1997', '1999', '101', '201', sd_id='sd101')]
        response = {'data': [{'attributes': {'title': row['name'], 'year': row['year'],
            'imdb_id': row['imdb_id'], 'tmdb_id': row['tmdb_id'], 'feature_type': 'movie'}} for row in rows]}
        self.assertIsNone(online._feature_identity_from_response(response, identity))
        self.assertIsNone(subdl._choose_movie_without_score(
            subdl.MediaIdentity(identity.title, original_title=identity.title), rows))

    def test_untrusted_container_conflict_stops_before_hash_or_proposal_search(self):
        for provider in (online, subdl):
            with self.subTest(provider=provider.__name__), \
                    patch.object(provider, 'filename_container_identity_conflict', return_value='容器身份与文件名冲突'), \
                    patch.object(provider, 'identify_media', side_effect=AssertionError('conflict must stop first')):
                with self.assertRaisesRegex(RuntimeError, '容器身份'):
                    provider.search('fixture', 'Contact 1997.mkv', 'en')

    def test_no_year_existing_unknown_title_never_becomes_an_arbitrary_first_result(self):
        title = 'A Completely Unidentified Film'
        self.provider_context(online, title)
        with patch.object(online, '_resolve_feature_identity', return_value=None), \
                patch.object(online, '_search_pages', return_value=([], 0, False)), \
                patch.object(online, 'resolve_work_identity', side_effect=AssertionError('no release proposal')):
            identity, items, meta = online.search('fixture', title + '.mkv', 'en')
        self.assertEqual(items, [])
        self.assertEqual(identity.year, '')
        self.assertEqual(meta.query_mode, 'unresolved-title')

    def test_smart_keeps_strict_candidate_id_and_year_gates_after_confirmation(self):
        identity = online.MediaIdentity('2010 The Year We Make Contact', year='1984', imdb_id='86837', tmdb_id='4437')
        good = candidate()
        online._apply_identity_admission([good], identity, 'resolved')
        self.assertEqual(good.identity_admission_state, 'auto')
        self.assertEqual(smart._candidate_identity_conflict(good, identity), '')
        self.assertEqual(smart._candidate_role_conflict(good, identity), '')
        wrong_year = candidate(year='1986')
        wrong_id = candidate(imdb='999', tmdb='999')
        online._apply_identity_admission([wrong_year, wrong_id], identity, 'resolved')
        self.assertTrue(smart._candidate_identity_conflict(wrong_id, identity))
        self.assertTrue(smart._candidate_role_conflict(wrong_year, identity))

    def recover(self, filename, rows, *, session=None, base=None, profiles=(),
                cache_path=False, page=None):
        base = base or online.MediaIdentity(Path(filename).stem, original_title=Path(filename).stem)
        session = session or resolver.RecoverySession(cache_path=cache_path)
        calls = []
        def secondary(query, budget):
            calls.append(('secondary', query, budget.request_count, budget.timeout))
            return {'results': rows}
        def public(tmdb, budget):
            calls.append(('public', tmdb, budget.request_count, budget.timeout))
            if page is None:
                raise AssertionError('A literal complete-title match should not need a public page')
            return page
        with patch.object(resolver, 'fetch_secondary_catalogue', side_effect=secondary), \
                patch.object(resolver, 'fetch_public_movie', side_effect=public):
            result = resolver.recover_filename_year(filename, base, session, profiles)
        return result, calls, session, base

    def test_24_titles_require_real_unique_complete_title_year_double_id_evidence(self):
        for index, (title, year) in enumerate(FILMS, 100):
            with self.subTest(title=title):
                name = f'{title} {year}.mkv'
                proof, calls, session, base = self.recover(name,
                    [secondary_film(title, year, str(index), str(index + 1000))],
                    # Model the retained exact record in the original primary
                    # whole-filename response; v2 can now recheck it by ID in
                    # one request. Dedicated v2 tests cover the no-profile path.
                    profiles=([resolver.WorkProfile(title, year, str(index), str(index + 1000),
                        'opensubtitles-features')] if title.isdigit() else ()))
                self.assertEqual(proof.state, 'confirmed')
                self.assertEqual((proof.title, proof.year), (title, year))
                self.assertEqual(len(calls), 1)
                self.assertTrue(resolver.validate_filename_resolution(name, base, proof))
                self.assertFalse(resolver.validate_resolution(base, proof),
                                 'The old unknown-year validator must remain strict')
                self.assertEqual(session.budget.request_count, 1)

    def test_correct_first_row_cannot_hide_a_second_same_title_year_work(self):
        rows = [secondary_film(), secondary_film(imdb='999', tmdb='998')]
        result, calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv', rows)
        self.assertEqual(result.state, 'pending')
        self.assertEqual(len(calls), 1)

    def test_single_id_wrong_year_non_movie_and_saturated_rows_never_confirm(self):
        variants = [
            [secondary_film(imdb='')], [secondary_film(tmdb='')],
            [secondary_film(year='1985')],
            [secondary_film(type='tv')],
            [secondary_film() for _ in range(resolver.CATALOGUE_LIMIT)],
        ]
        for rows in variants:
            with self.subTest(rows=len(rows), sample=rows[0]):
                result, _calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv', rows)
                self.assertEqual(result.state, 'pending')

    def test_an_unrelated_unique_first_row_is_not_same_work_evidence(self):
        for title in ('Contact', '2010', 'A Different Film'):
            with self.subTest(title=title):
                result, calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
                    [secondary_film(title)])
                self.assertEqual(result.state, 'pending')
                self.assertLessEqual(len(calls), 2)

    def test_full_original_catalogue_title_protects_literal_year_digits(self):
        base = online.MediaIdentity('Class of 1984', original_title='Class of 1984')
        profiles = [resolver.WorkProfile('Class of 1984', '1982', '100', '200', 'primary')]
        result, calls, session, _base = self.recover('Class of 1984.mkv',
            [secondary_film('Class of', '1984')], base=base, profiles=profiles)
        self.assertEqual(result.state, 'pending')
        self.assertEqual(calls, [])
        self.assertIsNone(session.budget)

    def test_filename_binding_rejects_changed_year_title_and_known_id(self):
        name = '2010 The Year We Make Contact 1984.mkv'
        proof, _calls, _session, base = self.recover(name, [secondary_film()])
        self.assertEqual(proof.state, 'confirmed')
        self.assertFalse(resolver.validate_filename_resolution('2010 The Year We Make Contact 1985.mkv', base, proof))
        self.assertFalse(resolver.validate_filename_resolution('Other Film 1984.mkv', base, proof))
        self.assertFalse(resolver.validate_filename_resolution(name,
            replace(base, imdb_id='999'), proof))
        self.assertFalse(resolver.validate_filename_resolution(name, base, replace(proof, year='1985')))

    def test_filename_binding_rejects_wrong_source_or_changed_evidence_id(self):
        name = '2010 The Year We Make Contact 1984.mkv'
        proof, _calls, _session, base = self.recover(name, [secondary_film()])
        for change in ({'source': 'https://elsewhere.invalid/movies'}, {'tmdb_id': '999'}):
            with self.subTest(change=change):
                evidence = tuple(dict(row, **change) for row in proof.evidence)
                self.assertFalse(resolver.validate_filename_resolution(name, base, replace(proof, evidence=evidence)))

    def test_confirmed_tail_proof_supplies_year_only_to_its_own_release_conflict_gate(self):
        name = '2010 The Year We Make Contact 1984.mkv'
        proof, _calls, _session, _base = self.recover(name, [secondary_film()])
        self.assertEqual(guard.release_year_conflict(name,
            '2010.The.Year.We.Make.Contact.1984.BluRay.srt', '1984', confirmed_work=proof), '')
        self.assertTrue(guard.release_year_conflict(name,
            '2010.1986.BluRay.srt', '1984', exact_hash=True, confirmed_work=proof))
        self.assertEqual(guard.release_year_conflict('Other Film 1984.mkv',
            'Other.Film.1986.BluRay.srt', '1986', confirmed_work=proof), '')

    def test_invalid_tail_proof_does_not_invent_a_local_release_year(self):
        name = '2010 The Year We Make Contact 1984.mkv'
        unproven = resolver.Resolution('confirmed', 'unproven caller value', '2010', '1984', '86837', '4437')
        self.assertFalse(resolver.validate_filename_resolution(name, online.MediaIdentity(Path(name).stem), unproven))
        self.assertEqual(guard.release_year_conflict(name, '2010.1986.BluRay.srt',
                                                    '1986', confirmed_work=unproven), '')

    def test_cached_tail_proof_cannot_override_current_catalogue_id_conflicts(self):
        name = 'Contact 1997.mkv'
        cache = self.root / 'evidence.json'
        proof, _calls, _session, base = self.recover(name,
            [secondary_film('Contact', '1997', '100', '200')], cache_path=cache)
        current = [resolver.WorkProfile('Contact', '1997', '100', '999', 'current-primary')]
        self.assertFalse(resolver.validate_filename_resolution(name, base, proof, current))
        result, _calls, _session, _base = self.recover(name,
            [secondary_film('Contact', '1997', '100', '200')], cache_path=cache, profiles=current)
        self.assertEqual(result.state, 'pending')

    def test_cached_split_proof_cannot_override_a_new_complete_original_title_record(self):
        name = 'Contact 1997.mkv'
        cache = self.root / 'evidence.json'
        self.recover(name, [secondary_film('Contact', '1997', '100', '200')], cache_path=cache)
        protected = [resolver.WorkProfile('Contact 1997', '2000', '900', '901', 'current-primary')]
        result, calls, _session, _base = self.recover(name, [], cache_path=cache, profiles=protected)
        self.assertEqual(result.state, 'pending')
        self.assertEqual(calls, [])

    def test_literal_full_name_cache_reuse_is_bound_to_proposed_title_and_year(self):
        cache = self.root / 'evidence.json'
        name = 'Contact 1997.mkv'
        result, _calls, _session, base = self.recover(name,
            [secondary_film('Contact', '1997', '100', '200')], cache_path=cache)
        self.assertEqual(result.state, 'confirmed')
        result, calls, _session, _base = self.recover(name, [], cache_path=cache)
        self.assertEqual(result.state, 'confirmed')
        self.assertTrue(result.cache_hit)
        self.assertEqual(calls, [])
        other, calls, _session, _base = self.recover('Contact 1998.mkv', [], cache_path=cache)
        self.assertEqual(other.state, 'pending')
        self.assertEqual(len(calls), 1)
        self.assertFalse(resolver.validate_filename_resolution('Contact 1998.mkv', base, result))

    def test_corrupt_and_expired_cache_never_replace_current_evidence(self):
        cache = self.root / 'evidence.json'
        name = 'Contact 1997.mkv'
        self.recover(name, [secondary_film('Contact', '1997', '100', '200')], cache_path=cache)
        original = cache.read_text(encoding='utf8')
        data = json.loads(original)
        for record in data['entries'].values():
            record['saved_at'] = 0
        for invalid in ('[]', '{invalid json', json.dumps(data)):
            with self.subTest(cache=invalid[:30]):
                cache.write_text(invalid, encoding='utf8')
                result, calls, _session, _base = self.recover(name, [], cache_path=cache)
                self.assertEqual(result.state, 'pending')
                self.assertEqual(len(calls), 1)

    def test_shared_recovery_session_does_not_mint_a_new_two_request_budget(self):
        session = resolver.RecoverySession(max_requests=50, time_budget_seconds=120, cache_path=False)
        for name in ('Contact 1997.mkv', 'Alien 1979.mkv', 'Brazil 1985.mkv'):
            result, _calls, _session, _base = self.recover(name, [], session=session)
            self.assertEqual(result.state, 'pending')
        self.assertEqual(session.budget.max_requests, 2)
        self.assertEqual(session.budget.time_budget_seconds, 15.)
        self.assertEqual(session.budget.request_count, 2)

    def test_recovery_budget_is_lazy_and_deadline_does_not_restart(self):
        now = [100.]
        session = resolver.RecoverySession(cache_path=False)
        self.assertIsNone(session.budget)
        # Supply a real production budget with a controlled start rather than
        # sleeping or depending on dataclass default-factory clock capture.
        session.budget = resolver.RequestBudget(started=now[0], time_budget_seconds=15.)
        def secondary(_title, _budget):
            now[0] += 16.
            return {'results': [secondary_film('Contact', '1997', '100', '200')]}
        with patch.object(resolver.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(resolver, 'fetch_secondary_catalogue', side_effect=secondary):
            result = resolver.recover_filename_year('Contact 1997.mkv',
                online.MediaIdentity('Contact 1997', original_title='Contact 1997'), session)
            again = resolver.recover_filename_year('Alien 1979.mkv',
                online.MediaIdentity('Alien 1979', original_title='Alien 1979'), session)
        self.assertEqual(result.state, 'pending')
        self.assertEqual(again.state, 'pending')
        self.assertEqual(session.budget.request_count, 1)
        self.assertGreaterEqual(result.elapsed_seconds, 15.)

    def test_cancellation_before_or_during_corroboration_propagates_original_type(self):
        for when in ('before', 'during'):
            with self.subTest(when=when):
                event = threading.Event()
                if when == 'before':
                    event.set()
                session = resolver.RecoverySession(cancel_event=event, cache_path=False)
                def secondary(*_args):
                    event.set()
                    return {'results': [secondary_film()]}
                with patch.object(resolver, 'fetch_secondary_catalogue', side_effect=secondary):
                    with self.assertRaises(core.CancelledError):
                        resolver.recover_filename_year('2010 The Year We Make Contact 1984.mkv',
                            online.MediaIdentity('2010 The Year We Make Contact 1984'), session)

    def test_new_tail_year_recovery_does_not_override_preexisting_strong_local_id(self):
        raw = '2010 The Year We Make Contact 1984'
        result, _calls, _session, _base = self.recover(raw + '.mkv', [secondary_film()],
            base=online.MediaIdentity(raw, original_title=raw, imdb_id='999'))
        self.assertEqual(result.state, 'pending')

    def test_a_competing_single_id_row_cannot_be_ignored_beside_one_complete_row(self):
        for other in (secondary_film(imdb='999', tmdb=''), secondary_film(imdb='', tmdb='999')):
            with self.subTest(other=other):
                result, _calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
                    [secondary_film(), other])
                self.assertEqual(result.state, 'pending')

    def test_smart_passes_one_shared_recovery_session_to_both_actual_provider_entries(self):
        movie = self.root / 'Contact 1997.mkv'
        movie.write_bytes(b'isolated controller fixture')
        event = threading.Event()
        base = online.MediaIdentity('Contact 1997', original_title='Contact 1997')
        empty = online.SearchMeta(query_mode='unresolved-title')
        with patch.object(smart, 'PROVIDERS', (('OpenSubtitles', online, 'api_key'),
                                               ('SubDL', subdl, 'subdl_api_key'))), \
                patch.object(online, 'load_settings', return_value={'api_key': 'fixture'}), \
                patch.object(subdl, 'load_settings', return_value={'subdl_api_key': 'fixture'}), \
                patch.object(online, 'search', return_value=(base, [], empty)) as primary, \
                patch.object(subdl, 'search', return_value=(subdl.MediaIdentity(base.title), [], subdl.SearchMeta())) as secondary, \
                patch.object(smart, 'filename_container_identity_conflict', return_value=''), \
                patch.object(smart.pro_core, 'prepare_shared_subtitle_content_audio', return_value=None):
            with self.assertRaises(RuntimeError):
                smart.find_verified_english(str(movie), 0, lambda _message: None, cancel_event=event)
        first = primary.call_args.kwargs.get('identity_recovery_session')
        second = secondary.call_args.kwargs.get('identity_recovery_session')
        self.assertIsInstance(first, resolver.RecoverySession)
        self.assertIs(first, second)
        self.assertIs(first.cancel_event, event)

    def test_smart_accepts_filename_bound_resolution_before_secondary_handoff(self):
        name = '2010 The Year We Make Contact 1984.mkv'
        movie = self.root / name
        movie.write_bytes(b'isolated controller fixture')
        proof, _calls, _session, base = self.recover(name, [secondary_film()])
        self.assertEqual(proof.state, 'confirmed')
        confirmed = online.MediaIdentity(proof.title, original_title=proof.title, year=proof.year,
                                         imdb_id=proof.imdb_id, tmdb_id=proof.tmdb_id)
        meta = online.SearchMeta(query_mode='confirmed-work-id',
            identity_resolution_status='confirmed', identity_resolution_reason=proof.reason,
            identity_resolution_evidence=proof.evidence)
        with patch.object(smart, 'PROVIDERS', (('OpenSubtitles', online, 'api_key'),
                                               ('SubDL', subdl, 'subdl_api_key'))), \
                patch.object(online, 'load_settings', return_value={'api_key': 'fixture'}), \
                patch.object(subdl, 'load_settings', return_value={'subdl_api_key': 'fixture'}), \
                patch.object(online, 'identify_media', return_value=base), \
                patch.object(online, 'search', return_value=(confirmed, [], meta)), \
                patch.object(subdl, 'search', return_value=(subdl.MediaIdentity(base.title), [], subdl.SearchMeta())) as secondary, \
                patch.object(smart, 'filename_container_identity_conflict', return_value=''), \
                patch.object(smart.pro_core, 'prepare_shared_subtitle_content_audio', return_value=None):
            with self.assertRaises(RuntimeError):
                smart.find_verified_english(str(movie), 0, lambda _message: None)
        forwarded = secondary.call_args.kwargs.get('confirmed_work_identity')
        self.assertIsNotNone(forwarded)
        self.assertEqual((forwarded.imdb_id, forwarded.tmdb_id, forwarded.year),
                         (proof.imdb_id, proof.tmdb_id, '1984'))

    def test_online_actual_tail_recovery_survives_all_24_candidate_identity_year_gates(self):
        for index, (title, year) in enumerate(FILMS, 100):
            with self.subTest(title=title), ExitStack() as stack:
                raw = f'{title} {year}'
                base = online.MediaIdentity(raw, original_title=raw)
                canonical = guard.numeric_title_alias_proposals(title)[0] if title.isdigit() else title
                item = candidate(title, year, str(index), str(index + 1000))
                stack.enter_context(patch.object(online, 'identify_media', return_value=base))
                stack.enter_context(patch.object(online, 'filename_container_identity_conflict', return_value=''))
                stack.enter_context(patch.object(online, 'opensubtitles_movie_hash', return_value=''))
                primary = stack.enter_context(patch.object(online, '_resolve_feature_identity', return_value=None))
                pages = stack.enter_context(patch.object(online, '_search_pages', return_value=([item], 1, False)))
                stack.enter_context(patch.object(resolver, 'fetch_secondary_catalogue', return_value={
                    'results': [secondary_film(canonical, year, str(index), str(index + 1000))]}))
                if title.isdigit():
                    stack.enter_context(patch.object(resolver, 'fetch_public_movie',
                        return_value=official_titles(canonical,year,str(index+1000),(title,))))
                else:
                    stack.enter_context(patch.object(resolver, 'fetch_public_movie',
                        side_effect=AssertionError('literal complete title should need no public page')))
                identity, items, meta = online.search('fixture', raw + '.mkv', 'en',
                    identity_recovery_session=resolver.RecoverySession(cache_path=False))
                self.assertEqual(primary.call_args.args[1].title, raw)
                self.assertEqual((identity.original_title, identity.year), (canonical, year))
                self.assertEqual(items, [item])
                self.assertEqual(pages.call_args.args[1].get('imdb_id'), str(index))
                self.assertEqual(item.identity_admission_state, 'auto')
                self.assertEqual(smart._candidate_identity_conflict(item, identity), '')
                self.assertEqual(smart._candidate_role_conflict(item, identity), '')
                self.assertEqual(meta.identity_resolution_status, 'confirmed')

    def test_subdl_actual_tail_recovery_survives_all_24_candidate_identity_year_gates(self):
        for index, (title, year) in enumerate(FILMS, 100):
            with self.subTest(title=title), ExitStack() as stack:
                raw = f'{title} {year}'
                base = subdl.MediaIdentity(raw, original_title=raw)
                canonical = guard.numeric_title_alias_proposals(title)[0] if title.isdigit() else title
                film = secondary_film(canonical, year, str(index), str(index + 1000), sd_id='sd123')
                item = subdl.SubtitleCandidate('1', 'generic.srt', f'{title}.{year}.BluRay',
                                               'en', 1, 1., False, False)
                def request(_key, path, *, params=None):
                    if path == '/movies/search':
                        return {'results': [film]}
                    self.assertEqual(path, '/subtitles/search')
                    self.assertEqual(params['sd_id'], 'sd123')
                    return {'subtitles': [{'id': '1'}]}
                stack.enter_context(patch.object(subdl, 'identify_media', return_value=base))
                stack.enter_context(patch.object(subdl, 'filename_container_identity_conflict', return_value=''))
                primary = stack.enter_context(patch.object(subdl, '_resolve_movie', return_value=None))
                stack.enter_context(patch.object(subdl, '_request_v2', side_effect=request))
                stack.enter_context(patch.object(subdl, '_candidates_from_response', return_value=[item]))
                stack.enter_context(patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [film]}))
                if title.isdigit():
                    stack.enter_context(patch.object(resolver,'fetch_public_movie',
                        return_value=official_titles(canonical,year,str(index+1000),(title,))))
                identity, items, meta = subdl.search('fixture', raw + '.mkv', 'en',
                    identity_recovery_session=resolver.RecoverySession(cache_path=False))
                self.assertEqual(primary.call_args.args[1].title, raw)
                self.assertEqual((identity.original_title, identity.year), (canonical, year))
                self.assertEqual(items, [item])
                self.assertTrue(item.identity_verified)
                self.assertEqual(smart._candidate_identity_conflict(item, identity), '')
                self.assertEqual(smart._candidate_role_conflict(item, identity), '')
                self.assertEqual(meta.identity_resolution_status, 'confirmed')

    def test_formal_alternative_titles_table_is_a_full_alias_proof(self):
        parsed = resolver.parse_public_movie_titles(titles_page(), '4437')
        self.assertEqual((parsed['title'], parsed['year'], parsed['tmdb_id']), ('2010', '1984', '4437'))
        self.assertIn('2010: The Year We Make Contact', parsed['aliases'])
        self.assertEqual(parsed['tagline'], '')
        self.assertEqual(parsed['acquisition'], 'https-public-tmdb-movie-titles')
        proof, calls, session, base = self.recover('2010 The Year We Make Contact 1984.mkv',
            [secondary_film('2010')], page=titles_page())
        self.assertEqual(proof.state, 'confirmed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.budget.request_count, 2)
        self.assertTrue(resolver.validate_filename_resolution('2010 The Year We Make Contact 1984.mkv', base, proof))

    def test_alternative_titles_canonical_og_and_requested_movie_id_must_all_agree(self):
        for changes in (
            {'canonical': 'https://www.themoviedb.org/movie/999-other/titles'},
            {'og_url': 'https://www.themoviedb.org/movie/999-other'},
            {'canonical': 'https://foreign.invalid/movie/4437-2010/titles'},
            {'og_url': 'https://foreign.invalid/movie/4437-2010'},
            {'canonical': 'https://www.themoviedb.org/movie/4437-2010'},
            {'canonical': 'https://www.themoviedb.org/movie/4437-2010/titles?movie=999'},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(RuntimeError):
                    resolver.parse_public_movie_titles(titles_page(**changes), '4437')

    def test_duplicate_canonical_or_og_records_do_not_prove_one_document_identity(self):
        for inserted in ('<link rel="canonical" href="https://www.themoviedb.org/movie/4437-2010/titles">',
                         '<meta property="og:url" content="https://www.themoviedb.org/movie/4437-2010">'):
            with self.subTest(inserted=inserted), self.assertRaises(RuntimeError):
                resolver.parse_public_movie_titles(titles_page().replace('</head>', inserted + '</head>'), '4437')

    def test_alternative_titles_wrong_release_year_or_main_title_stays_pending(self):
        for changes in ({'year': '1985'}, {'title': 'Other Film', 'og_title': 'Other Film'},
                        {'header_id': '999'}, {'title': 'Other Film'}):
            with self.subTest(changes=changes):
                result, calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
                    [secondary_film('2010')], page=titles_page(**changes))
                self.assertEqual(result.state, 'pending')
                self.assertEqual(len(calls), 2)

    def test_alias_text_in_recommendations_or_page_prose_is_not_official_table_evidence(self):
        for extra in ('<section class="recommendations"><a>2010: The Year We Make Contact</a></section>',
                      '<p>2010: The Year We Make Contact is a suggested title.</p>'):
            with self.subTest(extra=extra):
                page = titles_page(aliases=('An Unrelated Alias',), extra=extra)
                parsed = resolver.parse_public_movie_titles(page, '4437')
                self.assertNotIn('2010: The Year We Make Contact', parsed['aliases'])
                result, _calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
                    [secondary_film('2010')], page=page)
                self.assertEqual(result.state, 'pending')

    def test_inert_script_template_style_and_comments_cannot_supply_alias_rows(self):
        fake = '<table class="card releases titles"><tbody><tr><td>2010: The Year We Make Contact</td></tr></tbody></table>'
        for tag in ('script', 'template', 'style', 'noscript', 'comment'):
            wrapped = '<!--' + fake + '-->' if tag == 'comment' else '<' + tag + '>' + fake + '</' + tag + '>'
            with self.subTest(tag=tag):
                page = titles_page(aliases=('An Unrelated Alias',), extra=wrapped)
                result, _calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
                    [secondary_film('2010')], page=page)
                self.assertEqual(result.state, 'pending')

    def test_alias_in_type_column_or_only_partial_title_cannot_confirm(self):
        pages = [titles_page(aliases=('2010 The Year We Make',)),
                 titles_page(aliases=('An Unrelated Alias',)).replace(
                     '<td>An Unrelated Alias</td><td></td>',
                     '<td>An Unrelated Alias</td><td>2010: The Year We Make Contact</td>')]
        for page in pages:
            with self.subTest(page=page[-200:]):
                result, _calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
                    [secondary_film('2010')], page=page)
                self.assertEqual(result.state, 'pending')

    def test_recommendation_block_cannot_supply_an_official_looking_alias_table(self):
        injected = ('<section class="recommendations"><table class="card releases titles"><tbody>'
            '<tr><td>2010: The Year We Make Contact</td><td></td></tr></tbody></table></section>')
        result, _calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
            [secondary_film('2010')], page=titles_page(aliases=('An Unrelated Alias',), extra=injected))
        self.assertEqual(result.state, 'pending')

    def test_recommendation_header_cannot_replace_the_documents_conflicting_main_header(self):
        recommendation = ('<section class="recommendations"><h2><a href="/movie/4437-2010">2010</a>'
                          '<span class="tag release_date">(1984)</span></h2></section>')
        result, _calls, _session, _base = self.recover('2010 The Year We Make Contact 1984.mkv',
            [secondary_film('2010')], page=titles_page(header_id='999', extra=recommendation))
        self.assertEqual(result.state, 'pending')

    def test_public_titles_is_the_second_request_and_never_an_added_third_request(self):
        session = resolver.RecoverySession(cache_path=False)
        urls = []
        def public_response(request, _budget, **_kwargs):
            urls.append(request.full_url)
            return titles_page()
        with patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [secondary_film('2010')]}), \
                patch.object(resolver, '_read_response', side_effect=public_response):
            result = resolver.recover_filename_year('2010 The Year We Make Contact 1984.mkv',
                online.MediaIdentity('2010 The Year We Make Contact 1984'), session)
        self.assertEqual(result.state, 'confirmed')
        self.assertEqual(session.budget.request_count, 2)
        self.assertEqual(len(urls), 1)
        self.assertIn('/movie/4437-2010/titles?', urls[0])

    def test_formal_alias_proof_cache_reuses_zero_requests_and_rejects_id_tampering(self):
        cache = self.root / 'formal-alias-cache.json'
        name = '2010 The Year We Make Contact 1984.mkv'
        proof, _calls, _session, base = self.recover(name, [secondary_film('2010')],
            page=titles_page(), cache_path=cache)
        self.assertEqual(proof.state, 'confirmed')
        warm, calls, _session, _base = self.recover(name, [], cache_path=cache)
        self.assertEqual(warm.state, 'confirmed')
        self.assertTrue(warm.cache_hit)
        self.assertEqual(calls, [])
        public = dict(proof.evidence[-1], tmdb_id='999')
        tampered = replace(proof, evidence=(*proof.evidence[:-1], public))
        self.assertFalse(resolver.validate_filename_resolution(name, base, tampered))

    def test_secondary_and_titles_requests_share_the_same_15_second_deadline(self):
        now = [100.]
        session = resolver.RecoverySession(cache_path=False)
        session.budget = resolver.RequestBudget(started=now[0], time_budget_seconds=15.)
        def secondary(*_args):
            now[0] += 8.
            return {'results': [secondary_film('2010')]}
        def public(*_args):
            now[0] += 8.
            return titles_page()
        with patch.object(resolver.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(resolver, 'fetch_secondary_catalogue', side_effect=secondary), \
                patch.object(resolver, 'fetch_public_movie', side_effect=public):
            result = resolver.recover_filename_year('2010 The Year We Make Contact 1984.mkv',
                online.MediaIdentity('2010 The Year We Make Contact 1984'), session)
        self.assertEqual(result.state, 'pending')
        self.assertEqual(session.budget.request_count, 2)
        self.assertGreaterEqual(result.elapsed_seconds, 15.)

    def test_filename_cache_physically_records_the_raw_input_and_one_split_receipt(self):
        name = '2010 The Year We Make Contact 1984.mkv'
        cache = self.root / 'raw-binding.json'
        proof, _calls, _session, base = self.recover(name, [secondary_film()], cache_path=cache)
        proposal = resolver.filename_proposal_identity(name, base)
        payload = json.loads(cache.read_text(encoding='utf8'))
        self.assertEqual(payload['schema'], resolver.SCHEMA_VERSION)
        self.assertEqual(set(payload['entries']), {resolver._cache_key(proposal, ())})
        row = next(iter(payload['entries'].values()))
        self.assertEqual(row['input']['raw_title'], Path(name).stem)
        self.assertEqual(row['input']['title'], '2010 The Year We Make Contact')
        self.assertEqual(row['input']['year'], '1984')
        receipts = [item for item in proof.evidence
                    if item.get('acquisition') == 'filename-space-tail-proposal']
        self.assertEqual(receipts, [{'acquisition': 'filename-space-tail-proposal',
            'raw_title': Path(name).stem, 'proposed_title': '2010 The Year We Make Contact',
            'year': '1984'}])
        self.assertEqual(resolver.read_cached_resolution(proposal, cache_path=cache).state, 'confirmed')

    def test_changed_missing_or_duplicate_filename_receipt_cannot_reuse_a_cache_proof(self):
        name = '2010 The Year We Make Contact 1984.mkv'
        cache = self.root / 'receipt-mutation.json'
        proof, _calls, _session, base = self.recover(name, [secondary_film()], cache_path=cache)
        proposal = resolver.filename_proposal_identity(name, base)
        original = cache.read_text(encoding='utf8')
        receipt = proof.evidence[0]
        self.assertEqual(receipt['acquisition'], 'filename-space-tail-proposal')
        variants = [(*proof.evidence[1:],), (receipt, *proof.evidence),
                    (dict(receipt, raw_title='Other Film 1984'), *proof.evidence[1:]),
                    (dict(receipt, proposed_title='Other Film'), *proof.evidence[1:]),
                    (dict(receipt, year='1985'), *proof.evidence[1:])]
        for evidence in variants:
            with self.subTest(evidence=evidence[0] if evidence else None):
                tampered = replace(proof, evidence=evidence)
                self.assertFalse(resolver.validate_filename_resolution(name, base, tampered))
                payload = json.loads(original)
                next(iter(payload['entries'].values()))['resolution']['evidence'] = list(evidence)
                cache.write_text(json.dumps(payload), encoding='utf8')
                self.assertIsNone(resolver.read_cached_resolution(proposal, cache_path=cache))
                result, calls, _session, _base = self.recover(name, [], cache_path=cache)
                self.assertEqual(result.state, 'pending')
                self.assertEqual(len(calls), 1)

    def test_cache_scan_rejects_wrong_physical_raw_title_proposed_title_or_year(self):
        name = 'Contact 1997.mkv'
        cache = self.root / 'cache-input-mutation.json'
        _proof, _calls, _session, base = self.recover(name,
            [secondary_film('Contact', '1997', '100', '200')], cache_path=cache)
        proposal = resolver.filename_proposal_identity(name, base)
        original = cache.read_text(encoding='utf8')
        for change in ({'raw_title': 'Other Film 1997'}, {'title': 'Other Film'}, {'year': '1998'}):
            with self.subTest(change=change):
                payload = json.loads(original)
                next(iter(payload['entries'].values()))['input'].update(change)
                cache.write_text(json.dumps(payload), encoding='utf8')
                self.assertIsNone(resolver.read_cached_resolution(proposal, cache_path=cache))

    def test_plain_identity_cache_key_keeps_the_existing_schema_and_normalized_fields(self):
        identity = online.MediaIdentity('Contact', original_title='Contact', year='1997',
                                        imdb_id='tt0118884', tmdb_id='686')
        profiles = [resolver.WorkProfile('Contact', '1997', '118884', '686', 'old-provider',
                                        aliases=('CONTACT',))]
        rows = sorted({(resolver.title_key(p.title), p.year,
            resolver.id_key(p.imdb_id, imdb=True), resolver.id_key(p.tmdb_id), p.feature_type,
            tuple(sorted(resolver.title_key(value) for value in p.aliases))) for p in profiles})
        previous_data = [resolver.SCHEMA_VERSION, resolver.title_key(identity.original_title),
            identity.year, resolver.id_key(identity.imdb_id, imdb=True), identity.tmdb_id, rows]
        previous_key = hashlib.sha256(json.dumps(previous_data, ensure_ascii=False).encode('utf8')).hexdigest()
        self.assertEqual(resolver._cache_key(identity, profiles), previous_key)

    def test_plain_known_year_cache_still_validates_without_a_filename_receipt(self):
        cache = self.root / 'ordinary-cache.json'
        identity = online.MediaIdentity('Contact', original_title='Contact', year='1997')
        proof = resolver.resolve(identity, (), cache_path=cache, allow_exact_title_lookup=True,
            fetch_secondary=lambda *_args: {'results': [secondary_film('Contact', '1997', '100', '200')]})
        self.assertEqual(proof.state, 'confirmed')
        self.assertFalse(any(row.get('acquisition') == 'filename-space-tail-proposal' for row in proof.evidence))
        self.assertTrue(resolver.validate_resolution(identity, proof))
        cached = resolver.read_cached_resolution(identity, cache_path=cache)
        self.assertEqual((cached.imdb_id, cached.tmdb_id), ('100', '200'))
        row = next(iter(json.loads(cache.read_text(encoding='utf8'))['entries'].values()))
        self.assertNotIn('raw_title', row['input'])

    def test_two_raw_inputs_cannot_share_a_filename_cache_key_or_proof_receipt(self):
        name = 'Contact 1997.mkv'
        proof, _calls, _session, base = self.recover(name,
            [secondary_film('Contact', '1997', '100', '200')])
        proposal = resolver.filename_proposal_identity(name, base)
        other = SimpleNamespace(**vars(proposal))
        other.raw_title = 'Contact  1997'
        self.assertNotEqual(resolver._cache_key(proposal, ()), resolver._cache_key(other, ()))
        self.assertFalse(resolver.validate_resolution(other, proof))


if __name__ == '__main__':
    unittest.main()
