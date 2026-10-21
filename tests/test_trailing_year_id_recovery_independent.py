"""Independent v2 catalogue-ID recovery tests; all transports are synthetic."""
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import online_subtitles as online
import smart_subtitles as smart
import subdl_subtitles as subdl
import subtitle_identity_guard as guard
import subtitle_tool_core as core
import work_identity_resolver as resolver
from tests.test_catalogue_exceptions_independent import official_titles


def primary(title='Fury', year='2014', imdb='2713180', tmdb='228150', *, aliases=(), kind='movie'):
    return resolver.WorkProfile(title, year, imdb, tmdb, 'opensubtitles-features', kind, aliases)


def secondary(title='Fury', year='2014', imdb='2713180', tmdb='228150', *, aliases=(), kind='movie'):
    return {'name': title, 'original_name': title, 'year': year, 'imdb_id': imdb,
            'tmdb_id': tmdb, 'type': kind, 'sd_id': 'sd123', 'aliases': aliases}


def primary_response(records, **extra):
    data = {'data': [{'attributes': {'title': record.title, 'original_title': record.title,
        'title_aka': list(record.aliases), 'year': record.year, 'imdb_id': record.imdb_id,
        'tmdb_id': record.tmdb_id, 'feature_type': record.feature_type}} for record in records]}
    data.update(extra)
    return data


class TrailingYearIdRecoveryIndependentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': str(self.root / 'Local'),
            'APPDATA': str(self.root / 'Roaming'), 'SUBFLOW_RUNTIME_DIR': str(self.root / 'runtime')}))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')))

    def recover(self, name='Fury 2014.mkv', *, profiles=None, rows=None, session=None, cache=False,
                page=None, metadata=None):
        profiles = [primary()] if profiles is None else profiles
        rows = [secondary()] if rows is None else rows
        session = session or resolver.RecoverySession(cache_path=cache)
        base = online.MediaIdentity(Path(name).stem, original_title=Path(name).stem)
        calls = []
        def fetch(query, budget):
            calls.append((query, budget.request_count, budget.timeout))
            return {'results': rows, **(metadata or {})}
        def public(tmdb,budget):
            if page is None:
                raise AssertionError('exact ID recheck needs no public page')
            return page(tmdb,budget) if callable(page) else page
        with patch.object(resolver, 'fetch_secondary_catalogue', side_effect=fetch), \
                patch.object(resolver, 'fetch_public_movie', side_effect=public):
            result = resolver.recover_filename_year(name, base, session, profiles)
        return result, calls, session, base

    def test_complete_split_primary_record_uses_id_instead_of_a_saturated_title_search(self):
        cases = [('Fury', '2014', '2713180', '228150'), ('The Thing', '1982', '84787', '1091'),
                 ('Brazil', '1985', '88846', '68'), ('300', '2006', '416449', '1271'),
                 ('1984', '1984', '87804', '9314'), ('1917', '2019', '8579674', '530915')]
        for title, year, imdb, tmdb in cases:
            with self.subTest(title=title):
                name = f'{title} {year}.mkv'
                proof, calls, session, base = self.recover(name,
                    profiles=[primary(title, year, imdb, tmdb)], rows=[secondary(title, year, imdb, tmdb)])
                self.assertEqual(proof.state, 'confirmed')
                self.assertEqual([call[0] for call in calls], ['tt' + imdb.zfill(7)])
                self.assertEqual(session.budget.request_count, 1)
                self.assertTrue(resolver.validate_filename_resolution(name, base, proof))

    def test_a_secondary_first_row_cannot_override_the_primary_double_id(self):
        for rows in ([secondary(imdb='999', tmdb='998')],
                     [secondary(imdb='999', tmdb='998'), secondary()]):
            with self.subTest(rows=len(rows)):
                proof, calls, _session, _base = self.recover(rows=rows)
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(len(calls), 1)

    def test_real_2012_shape_single_id_competitor_is_not_hidden_by_the_double_id_row(self):
        profiles = [primary('2012', '2009', '1190080', '14161'),
                    primary('2012', '2009', '1364155', '')]
        for records in (profiles, list(reversed(profiles))):
            with self.subTest(order=records[0].imdb_id):
                proof, calls, _session, _base = self.recover('2012 2009.mkv', profiles=records,
                    rows=[secondary('2012', '2009', '1190080', '14161')])
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(calls, [])

    def test_two_primary_double_id_mappings_stay_pending_in_both_orders(self):
        profiles = [primary(), primary(imdb='999', tmdb='998')]
        for records in (profiles, list(reversed(profiles))):
            with self.subTest(order=records[0].imdb_id):
                proof, calls, _session, _base = self.recover(profiles=records)
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(calls, [])

    def test_primary_same_id_with_changed_year_type_or_other_id_is_a_conflict(self):
        for conflict in (primary(year='2015'), primary(title='Another Film', year='2015'),
                         primary(kind='tv'), primary(title='Another Film', kind='tv'),
                         primary(imdb='2713180', tmdb='999'), primary(imdb='999', tmdb='228150')):
            with self.subTest(conflict=conflict):
                proof, calls, _session, _base = self.recover(profiles=[primary(), conflict])
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(calls, [])

    def test_incomplete_primary_ids_cannot_authorize_a_by_id_query(self):
        for record in (primary(imdb=''), primary(tmdb=''), primary(imdb='', tmdb='')):
            with self.subTest(record=record):
                proof, calls, _session, _base = self.recover(profiles=[record], rows=[])
                self.assertEqual(proof.state, 'pending')
                self.assertFalse(any(query.startswith('tt') for query, _count, _timeout in calls))

    def test_secondary_by_id_still_requires_matching_year_type_and_both_ids(self):
        for row in (secondary(year='2015'), secondary(kind='tv'), secondary(imdb=''),
                    secondary(tmdb=''), secondary(imdb='999'), secondary(tmdb='999')):
            with self.subTest(row=row):
                proof, calls, _session, _base = self.recover(rows=[row])
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(len(calls), 1)

    def test_secondary_by_id_cannot_ignore_same_id_wrong_year_or_type_records(self):
        for conflict in (secondary(year='2015'), secondary(kind='tv'),
                         secondary(title='Another Film', year='2015'),
                         secondary(title='Another Film', kind='tv')):
            with self.subTest(conflict=conflict):
                proof, calls, _session, _base = self.recover(rows=[secondary(), conflict])
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(len(calls), 1)

    def test_intact_original_catalogue_title_precedes_even_a_good_split_double_id(self):
        for title, year, split in (('Class of 1984', '1982', 'Class of'),
                                   ('The Legend of 1900', '1998', 'The Legend of')):
            with self.subTest(title=title):
                tail = title.rsplit(' ', 1)[1]
                proof, calls, session, _base = self.recover(title + '.mkv',
                    profiles=[primary(title, year), primary(split, tail)], rows=[secondary(split, tail)])
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(calls, [])
                self.assertIsNone(session.budget)

    def test_by_id_cache_proof_is_rechecked_against_current_primary_ids_and_year(self):
        cache = self.root / 'id-proof.json'
        proof, _calls, _session, base = self.recover(cache=cache)
        self.assertEqual(proof.state, 'confirmed')
        self.assertFalse(resolver.validate_filename_resolution('Fury 2014.mkv', base, proof,
            [primary(imdb='2713180', tmdb='999')]))
        self.assertFalse(resolver.validate_filename_resolution('Fury 2014.mkv', base, proof,
            [primary(year='2015')]))
        pending, calls, _session, _base = self.recover(cache=cache,
            profiles=[primary(), primary(imdb='2713180', tmdb='999')])
        self.assertEqual(pending.state, 'pending')
        self.assertEqual(calls, [])

    def test_new_nonoverlapping_primary_competitor_invalidates_disk_and_session_proof(self):
        cache = self.root / 'new-ambiguity.json'
        session = resolver.RecoverySession(cache_path=cache)
        proof, _calls, _session, base = self.recover(session=session)
        self.assertEqual(proof.state, 'confirmed')
        for source in ('opensubtitles-features', 'opensubtitles-features-incomplete'):
            with self.subTest(source=source):
                # Incomplete pages cannot prove uniqueness, but their observed
                # explicit competing ID must not disappear as contrary evidence.
                competing = [primary(), replace(primary(imdb='999', tmdb='998'), source=source)]
                self.assertFalse(resolver.validate_filename_resolution('Fury 2014.mkv', base, proof, competing))
                for reuse_session in (session, resolver.RecoverySession(cache_path=cache)):
                    with self.subTest(memory=reuse_session is session):
                        pending, calls, _session, _base = self.recover(session=reuse_session, profiles=competing)
                        self.assertEqual(pending.state, 'pending')
                        self.assertEqual(calls, [])

    def test_primary_official_alias_is_retained_as_its_own_evidence(self):
        name = 'The Legend of 1900 1998.mkv'
        title = 'The Legend of 1900'
        record = primary('1900', '1998', '120734', '10376', aliases=(title,))
        proof, calls, _session, base = self.recover(name, profiles=[record],
            rows=[secondary('1900', '1998', '120734', '10376')])
        self.assertEqual(proof.state, 'confirmed')
        self.assertEqual([query for query, _count, _timeout in calls], ['tt0120734'])
        self.assertTrue(resolver.validate_filename_resolution(name, base, proof))
        secondary_entries = [row for row in proof.evidence
            if row.get('acquisition') == 'https-subdl-v2-unique-title-year-double-id']
        self.assertEqual(len(secondary_entries), 1)
        self.assertNotIn(title, secondary_entries[0].get('aliases', ()),
                         'Do not falsely attribute the primary official alias to SubDL')
        primary_entries = [row for row in proof.evidence
            if title in row.get('aliases', ()) and 'opensubtitles' in str(row.get('source', ''))]
        self.assertEqual(len(primary_entries), 1)
        for change in ({'imdb_id': '999'}, {'tmdb_id': '999'}, {'year': '1999'}, {'aliases': ()}):
            with self.subTest(change=change):
                altered = tuple(dict(row, **change) if row is primary_entries[0] else row
                                for row in proof.evidence)
                self.assertFalse(resolver.validate_filename_resolution(name, base,
                    replace(proof, evidence=altered)))

    def test_numeric_split_without_profiles_uses_exact_primary_then_independent_id(self):
        # Historical method name retained; no-profile acquisition now uses the
        # complete English reading, then an official original-number alias.
        name = '1984 1984.mkv'
        session = resolver.RecoverySession(cache_path=False)
        seen = []
        session.primary_lookup = lambda *_args:self.fail('No repeated numeric primary lookup')
        def public(tmdb,budget):
            seen.append((tmdb,budget.request_count,budget.timeout))
            return official_titles()
        proof, calls, _session, base = self.recover(name, profiles=[], session=session,
            rows=[secondary('Nineteen Eighty-Four', '1984', '87804', '9314')],page=public)
        self.assertEqual(proof.state, 'confirmed')
        self.assertEqual([(tmdb,count) for tmdb,count,_timeout in seen],[('9314',2)])
        self.assertEqual([query for query, _count, _timeout in calls], ['nineteen eighty four'])
        self.assertEqual((calls[0][1],seen[0][1]), (1, 2))
        self.assertEqual(session.budget.request_count, 2)
        self.assertTrue(resolver.validate_filename_resolution(name, base, proof))

    def test_numeric_primary_ambiguity_does_not_spend_the_second_request(self):
        reading=guard.numeric_title_alias_proposals('2012')[0]
        cases = [[secondary(reading, '2009', '1190080', '14161'), secondary(reading, '2009', '1364155', '')],
                 [secondary(reading, '2009', '1190080', '14161'), secondary(reading, '2009', '999', '998')]]
        for records in cases:
            with self.subTest(records=records):
                session = resolver.RecoverySession(cache_path=False)
                session.primary_lookup = lambda *_args:self.fail('No repeated numeric primary lookup')
                proof, calls, _session, _base = self.recover('2012 2009.mkv', profiles=[], session=session,
                    rows=records)
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(len(calls), 1)
                self.assertEqual(session.budget.request_count, 1)

    def test_numeric_primary_wrong_year_type_or_empty_results_cannot_authorize_by_id(self):
        for records in ([], [secondary('Nineteen Eighty-Four', '1985', '87804', '9314')],
                        [secondary('Nineteen Eighty-Four', '1984', '87804', '9314', kind='tv')]):
            with self.subTest(records=records):
                session = resolver.RecoverySession(cache_path=False)
                session.primary_lookup = lambda *_args:self.fail('No repeated numeric primary lookup')
                proof, calls, _session, _base = self.recover('1984 1984.mkv', profiles=[], session=session,
                    rows=records)
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(len(calls), 1)
                self.assertEqual(session.budget.request_count, 1)

    def test_incomplete_numeric_primary_page_is_not_unique_evidence(self):
        for metadata in ({'page': 1, 'total_pages': 2}, {'total_count': 2},
                         {'total_results': 2}, {'total_entries': 2}, {'total': 2}):
            with self.subTest(metadata=metadata):
                session = resolver.RecoverySession(cache_path=False)
                session.primary_lookup = lambda *_args:self.fail('No repeated numeric primary lookup')
                proof, calls, _session, _base = self.recover('1984 1984.mkv', profiles=[], session=session,
                    rows=[secondary('Nineteen Eighty-Four', '1984', '87804', '9314')],metadata=metadata)
                self.assertEqual(proof.state, 'pending')
                self.assertEqual(len(calls), 1)
                self.assertEqual(session.budget.request_count, 1)

    def test_bare_numeric_movie_title_never_triggers_the_split_primary_callback(self):
        for name in ('1917.mkv', '1984.mkv', '2012.mkv'):
            with self.subTest(name=name):
                session = resolver.RecoverySession(cache_path=False)
                session.primary_lookup = lambda *_args: self.fail('Bare title is not a tail-year proposal')
                result, calls, _session, _base = self.recover(name, profiles=[], session=session)
                self.assertIsNone(result)
                self.assertEqual(calls, [])
                self.assertIsNone(session.budget)

    def test_numeric_primary_and_secondary_share_one_15_second_deadline(self):
        now = [100.]
        session = resolver.RecoverySession(cache_path=False)
        session.budget = resolver.RequestBudget(started=now[0], time_budget_seconds=15.)
        def lookup_secondary(*_args):
            now[0] += 8.
            return {'results': [secondary('Nineteen Eighty-Four', '1984', '87804', '9314')]}
        def lookup_public(*_args):
            now[0] += 8.
            return official_titles()
        session.primary_lookup = lambda *_args:self.fail('No repeated numeric primary lookup')
        with patch.object(resolver.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(resolver, 'fetch_secondary_catalogue', side_effect=lookup_secondary), \
                patch.object(resolver,'fetch_public_movie',side_effect=lookup_public):
            result = resolver.recover_filename_year('1984 1984.mkv',
                online.MediaIdentity('1984 1984'), session)
        self.assertEqual(result.state, 'pending')
        self.assertEqual(session.budget.request_count, 2)
        self.assertGreaterEqual(result.elapsed_seconds, 15.)

    def test_cancellation_during_numeric_primary_does_not_fall_back_to_secondary(self):
        event = threading.Event()
        session = resolver.RecoverySession(cancel_event=event, cache_path=False)
        def lookup(*_args):
            event.set()
            return {'results':[secondary('Nineteen Eighty-Four','1984','87804','9314')]}
        session.primary_lookup = lambda *_args:self.fail('No repeated numeric primary lookup')
        with patch.object(resolver, 'fetch_secondary_catalogue', side_effect=lookup), \
                patch.object(resolver,'fetch_public_movie',side_effect=AssertionError('cancelled')):
            with self.assertRaises(core.CancelledError):
                resolver.recover_filename_year('1984 1984.mkv', online.MediaIdentity('1984 1984'), session)

    def test_numeric_confirmed_proof_reuses_zero_requests_on_disk_and_in_memory(self):
        name = '1984 1984.mkv'
        cache = self.root / 'numeric-proof.json'
        session = resolver.RecoverySession(cache_path=cache)
        session.primary_lookup = lambda *_args:self.fail('No repeated numeric primary lookup')
        proof, calls, _session, _base = self.recover(name, profiles=[], session=session,
            rows=[secondary('Nineteen Eighty-Four', '1984', '87804', '9314')],page=official_titles())
        self.assertEqual(proof.state, 'confirmed')
        self.assertEqual((len(calls), session.budget.request_count), (1, 2))
        for reuse_session in (session, resolver.RecoverySession(cache_path=cache)):
            with self.subTest(memory=reuse_session is session):
                reuse_session.primary_lookup = lambda *_args: self.fail('No repeated primary lookup')
                warm, calls, _session, _base = self.recover(name, profiles=[], rows=[], session=reuse_session)
                self.assertEqual(warm.state, 'confirmed')
                self.assertEqual(calls, [])
                if reuse_session is not session:
                    self.assertTrue(warm.cache_hit)
                    self.assertEqual(reuse_session.budget.request_count, 0)

    def test_by_id_primary_trust_is_not_granted_to_another_provider_profile(self):
        record = replace(primary(), source='subdl-v2')
        proof, calls, _session, _base = self.recover(profiles=[record], rows=[])
        self.assertEqual(proof.state, 'pending')
        self.assertFalse(any(query.startswith('tt') for query, _count, _timeout in calls))

    def test_actual_primary_diagnostics_keep_nonmovie_rows_before_selector_filters_them(self):
        diagnostics = online._FeatureLookupDiagnostics()
        response = primary_response([primary(), primary(kind='tv')])
        selected = online._feature_identity_from_response(response,
            online.MediaIdentity('Fury 2014', original_title='Fury 2014'), diagnostics=diagnostics)
        self.assertIsNone(selected)
        self.assertEqual(len(diagnostics.profiles), 2)
        self.assertIn('tv', [record.feature_type for record in diagnostics.profiles])

    def test_original_incomplete_primary_page_cannot_authorize_the_by_id_shortcut(self):
        for metadata in ({'page': 1, 'total_pages': 2}, {'has_more': True}, {'truncated': True},
                         {'total_count': 2}, {'total_results': 2}, {'total_entries': 2}, {'total': 2}):
            with self.subTest(metadata=metadata), ExitStack() as stack:
                response = primary_response([primary()], **metadata)
                base = online.MediaIdentity('Fury 2014', original_title='Fury 2014')
                item = online.SubtitleCandidate(1, 'Fury.srt', 'Fury.2014.BluRay', 'en',
                    1, 1., False, False, feature_title='Fury', feature_year='2014',
                    feature_imdb_id='2713180', feature_tmdb_id='228150')
                queries = []
                def original(_key, identity, **_kwargs):
                    return online._feature_identity_from_response(response, identity,
                        diagnostics=online._FEATURE_LOOKUP_DIAGNOSTICS.get())
                def independent(query, _budget):
                    queries.append(query)
                    # The title query is saturated, as in the live failing
                    # examples. An unjustified ID shortcut would falsely pass.
                    return {'results': [secondary()] if query.startswith('tt') else
                            [secondary() for _ in range(resolver.CATALOGUE_LIMIT)]}
                stack.enter_context(patch.object(online, 'identify_media', return_value=base))
                stack.enter_context(patch.object(online, 'filename_container_identity_conflict', return_value=''))
                stack.enter_context(patch.object(online, 'opensubtitles_movie_hash', return_value=''))
                stack.enter_context(patch.object(online, '_resolve_feature_identity', side_effect=original))
                stack.enter_context(patch.object(online, '_search_pages', return_value=([item], 1, False)))
                stack.enter_context(patch.object(resolver, 'fetch_secondary_catalogue', side_effect=independent))
                _identity, items, meta = online.search('fixture', 'Fury 2014.mkv', 'en',
                    identity_recovery_session=resolver.RecoverySession(cache_path=False))
                self.assertEqual(items, [])
                self.assertNotEqual(meta.identity_resolution_status, 'confirmed')
                self.assertFalse(any(query.startswith('tt') for query in queries))

    def test_recorded_cloverfield_single_id_conflict_is_not_published_cached_or_reused(self):
        # Safe projection of actual catalogue-positive-v2-final/19 responses:
        # original primary IMDb=3646940 with no TMDB, independent catalogue
        # IMDb=1179933/TMDB=333371, same complete title and release year.
        name = '10 Cloverfield Lane 2016.mkv'
        observed = primary('10 cloverfield lane', '2016', '3646940', '',
                           aliases=('10 Cloverfield Lane',))
        independent = secondary('10 Cloverfield Lane', '2016', '1179933', '333371')
        cache = self.root / 'recorded-conflict.json'
        session = resolver.RecoverySession(cache_path=cache)
        pending, calls, _session, _base = self.recover(name,
            profiles=[observed], rows=[independent], session=session)
        self.assertEqual(pending.state, 'pending')
        self.assertEqual((pending.imdb_id, pending.tmdb_id, pending.evidence), ('', '', ()))
        self.assertIsNone(session.confirmed_resolution)
        self.assertFalse(cache.exists())
        self.assertEqual(len(calls), 1)
        again, calls, _session, _base = self.recover(name,
            profiles=[observed], rows=[independent], session=session)
        self.assertEqual(again.state, 'pending')
        self.assertEqual(calls, [])
        self.assertIsNone(session.confirmed_resolution)
        with patch.object(subdl, 'filename_container_identity_conflict', return_value=''), \
                patch.object(subdl, 'identify_media', return_value=subdl.MediaIdentity(Path(name).stem)), \
                patch.object(subdl, '_request_v2', side_effect=AssertionError('Pending proof must not be queried')):
            _identity, items, meta = subdl.search('fixture', name, 'en', confirmed_work_identity=pending)
        self.assertEqual(items, [])
        self.assertNotEqual(meta.identity_resolution_status, 'confirmed')

    def test_actual_provider_entries_do_not_leak_conflicting_proof_via_meta_or_session(self):
        name = '10 Cloverfield Lane 2016.mkv'
        base = online.MediaIdentity(Path(name).stem, original_title=Path(name).stem)
        observed = primary('10 cloverfield lane', '2016', '3646940', '',
                           aliases=('10 Cloverfield Lane',))
        response = primary_response([observed])
        independent = secondary('10 Cloverfield Lane', '2016', '1179933', '333371')
        session = resolver.RecoverySession(cache_path=self.root / 'provider-conflict.json')
        def original(_key, identity, **_kwargs):
            return online._feature_identity_from_response(response, identity,
                diagnostics=online._FEATURE_LOOKUP_DIAGNOSTICS.get())
        with patch.object(online, 'identify_media', return_value=base), \
                patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                patch.object(online, '_resolve_feature_identity', side_effect=original), \
                patch.object(online, '_search_pages', side_effect=AssertionError('No unconfirmed subtitle query')), \
                patch.object(resolver, 'fetch_secondary_catalogue', return_value={'results': [independent]}) as secondary_call:
            _identity, items, meta = online.search('fixture', name, 'en', identity_recovery_session=session)
            self.assertEqual(items, [])
            self.assertEqual(meta.identity_resolution_status, 'pending')
            self.assertIsNone(session.confirmed_resolution)
            with patch.object(subdl, 'identify_media', return_value=subdl.MediaIdentity(base.title)), \
                    patch.object(subdl, 'filename_container_identity_conflict', return_value=''), \
                    patch.object(subdl, '_resolve_movie', return_value=None), \
                    patch.object(subdl, '_request_v2', side_effect=AssertionError('No conflicting by-ID reuse')):
                _identity, items, meta = subdl.search('fixture', name, 'en',
                    identity_recovery_session=session, confirmed_work_identity=session.confirmed_resolution)
            self.assertEqual(items, [])
            self.assertEqual(meta.identity_resolution_status, 'pending')
            self.assertIsNone(session.confirmed_resolution)
            self.assertEqual(secondary_call.call_count, 1)

    def test_consistent_single_primary_id_can_still_confirm_and_persist(self):
        name = '10 Cloverfield Lane 2016.mkv'
        for imdb, tmdb in (('1179933', ''), ('', '333371')):
            with self.subTest(imdb=imdb, tmdb=tmdb):
                cache = self.root / ('matching-' + (imdb or tmdb) + '.json')
                record = primary('10 Cloverfield Lane', '2016', imdb, tmdb)
                proof, calls, session, base = self.recover(name, profiles=[record],
                    rows=[secondary('10 Cloverfield Lane', '2016', '1179933', '333371')], cache=cache)
                self.assertEqual(proof.state, 'confirmed')
                self.assertTrue(resolver.validate_filename_resolution(name, base, proof, [record]))
                self.assertIsNotNone(session.confirmed_resolution)
                self.assertEqual(len(calls), 1)
                self.assertTrue(cache.exists())

    def test_by_id_recovery_does_not_reset_the_shared_request_or_deadline_limits(self):
        session = resolver.RecoverySession(cache_path=False, max_requests=50, time_budget_seconds=120)
        for title, year in (('Fury', '2014'), ('Alien', '1979'), ('Contact', '1997')):
            self.recover(f'{title} {year}.mkv', profiles=[primary(title, year)],
                rows=[secondary(title, year)], session=session)
        self.assertEqual(session.budget.max_requests, 2)
        self.assertEqual(session.budget.time_budget_seconds, 15.)
        self.assertEqual(session.budget.request_count, 2)

    def test_user_cancellation_during_by_id_validation_preserves_original_type(self):
        event = threading.Event()
        session = resolver.RecoverySession(cancel_event=event, cache_path=False)
        def fetch(*_args):
            event.set()
            return {'results': [secondary()]}
        with patch.object(resolver, 'fetch_secondary_catalogue', side_effect=fetch):
            with self.assertRaises(core.CancelledError):
                resolver.recover_filename_year('Fury 2014.mkv', online.MediaIdentity('Fury 2014'),
                                               session, [primary()])


if __name__ == '__main__':
    unittest.main()
