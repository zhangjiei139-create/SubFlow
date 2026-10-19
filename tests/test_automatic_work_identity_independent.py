"""Independent black-box acceptance cases; all external transport is synthetic."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

import subtitle_tool_core as core
import online_subtitles as online
import work_identity_resolver as resolver
import smart_subtitles as smart
import subdl_subtitles as subdl
import subtitle_audit


def film(name='Mirror Film', year='2007', imdb='100', tmdb='200', **extra):
    return dict(name=name, original_name=name, year=year, imdb_id=imdb,
                tmdb_id=tmdb, type='movie', **extra)


def page(name='Orbit Motion', year='2007', tmdb='200', tagline='The future begins.',
         aliases=None, schema_year=None, visible=True, extra=''):
    row = {'@type': 'Movie', 'name': name,
           'url': 'https://www.themoviedb.org/movie/' + str(tmdb) + '-example',
           'releasedEvent': [{'startDate': str(schema_year or year) + '-01-01'}]}
    if aliases is not None:
        row['alternateName'] = aliases
    header = ('<h2><a href="/movie/' + str(tmdb) + '-example">' + name + '</a>'
              '<span class="tag release_date">(' + year + ')</span></h2>') if visible else ''
    return ('<script type="application/ld+json">' + json.dumps(row) + '</script>'
            + header + '<h3 class="tagline">' + tagline + '</h3>' + extra)


class AutomaticIdentityAcceptance(unittest.TestCase):
    def setUp(self):
        self.identity = online.MediaIdentity('Mirror Film', year='2007')
        self.profiles = [resolver.WorkProfile('Mirror Film', '2007', '100', '200', 'opensubtitles'),
                         resolver.WorkProfile('Mirror Film', '2007', '101', '', 'opensubtitles')]

    def resolve(self, identity=None, profiles=None, rows=None, html=None, **kw):
        calls = []
        def secondary(title, budget):
            calls.append(('secondary', title, budget.timeout))
            return {'results': [film()] if rows is None else rows}
        def public(tmdb, budget):
            calls.append(('public', tmdb, budget.timeout))
            return page() if html is None else html
        result = resolver.resolve(identity or self.identity, self.profiles if profiles is None else profiles,
                                  fetch_secondary=secondary, fetch_public=public,
                                  cache_path=kw.pop('cache_path', False), **kw)
        return result, calls

    def alias(self, local='Orbit Motion The future begins', **kw):
        return self.resolve(online.MediaIdentity(local, year='2007'),
                            [resolver.WorkProfile('Orbit Motion', '2007', '100', '200', 'opensubtitles')],
                            rows=[film('Orbit Motion')], **kw)

    def test_unique_independent_double_id_resolves_competing_primary(self):
        result, calls = self.resolve()
        self.assertEqual(result.state, 'confirmed')
        self.assertEqual((result.imdb_id, result.tmdb_id), ('100', '200'))
        self.assertEqual(len(calls), 1)

    def test_same_name_year_two_secondary_double_id_mappings_stay_pending(self):
        result, _ = self.resolve(rows=[film(), film(imdb='101', tmdb='201')])
        self.assertEqual(result.state, 'pending')

    def test_single_id_secondary_cannot_resolve_competition(self):
        for field in ('imdb_id', 'tmdb_id'):
            row = film(); row[field] = ''
            with self.subTest(field=field):
                result, _ = self.resolve(rows=[row])
                self.assertEqual(result.state, 'pending')

    def test_secondary_wrong_year_is_not_corroboration(self):
        result, _ = self.resolve(rows=[film(year='2008')])
        self.assertEqual(result.state, 'pending')

    def test_one_sided_primary_id_contradiction_must_not_be_ignored(self):
        for other in (resolver.WorkProfile('Mirror Film', '2007', '100', '299'),
                      resolver.WorkProfile('Mirror Film', '2007', '199', '200')):
            with self.subTest(other=other):
                result, _ = self.resolve(profiles=[self.profiles[0], other])
                self.assertEqual(result.state, 'pending')

    def test_secondary_same_id_missing_other_id_cannot_hide_contradiction(self):
        for other in (film(imdb='100', tmdb='299'), film(imdb='199', tmdb='200')):
            with self.subTest(other=other):
                result, _ = self.resolve(rows=[film(), other])
                self.assertEqual(result.state, 'pending')

    def test_existing_confirmed_identity_cannot_be_overridden(self):
        for values in ({'imdb_id': '999'}, {'tmdb_id': '999'}):
            identity = online.MediaIdentity('Mirror Film', year='2007', **values)
            result, _ = self.resolve(identity=identity)
            self.assertEqual(result.state, 'pending')

    def test_complete_tagline_relation_is_confirmed(self):
        result, calls = self.alias()
        self.assertEqual(result.state, 'confirmed')
        self.assertEqual(len(calls), 2)

    def test_partial_tagline_or_prefix_alone_does_not_confirm(self):
        for local in ('Orbit Motion The future', 'Orbit Motion begins', 'Orbit Motion The begins'):
            with self.subTest(local=local):
                result, _ = self.alias(local=local)
                self.assertEqual(result.state, 'pending')

    def test_published_full_alias_is_accepted_but_not_partial_alias(self):
        result, _ = self.alias(local='Orbit Motion New Horizon', html=page(aliases=['Orbit Motion New Horizon']))
        self.assertEqual(result.state, 'confirmed')
        result, _ = self.alias(local='Orbit Motion New', html=page(aliases=['Orbit Motion New Horizon']))
        self.assertEqual(result.state, 'pending')

    def test_foreign_tmdb_url_and_wrong_tmdb_id_are_rejected(self):
        for html in (page(tmdb='201'), page().replace('www.themoviedb.org', 'elsewhere.example')):
            result, _ = self.alias(html=html)
            self.assertEqual(result.state, 'pending')

    def test_public_schema_and_visible_year_must_agree(self):
        result, _ = self.alias(html=page(schema_year='2008'))
        self.assertEqual(result.state, 'pending')

    def test_related_movie_release_date_does_not_supply_primary_year(self):
        # The requested work's primary header is absent. A related-work block
        # is the only place carrying the desired release_date and tagline.
        html = page(visible=False).replace('<h3 class="tagline">The future begins.</h3>', '')
        html += ('<section class="recommendations"><h2><a href="/movie/999-other">Other</a>'
                 '<span class="tag release_date">(2007)</span></h2>'
                 '<h3 class="tagline">The future begins.</h3></section>')
        result, _ = self.alias(html=html)
        self.assertEqual(result.state, 'pending')

    def test_multiple_movie_schema_records_do_not_confirm_selected_one(self):
        result, _ = self.alias(html=page() + page(name='Other Film', tmdb='999'))
        self.assertEqual(result.state, 'pending')

    def test_saturated_secondary_limit_is_not_unique_catalogue_proof(self):
        # Even all duplicate rows at the production catalogue's request limit
        # rows at that limit cannot establish there is no second mapping later.
        result, _ = self.resolve(rows=[film() for _ in range(resolver.CATALOGUE_LIMIT)])
        self.assertEqual(result.state, 'pending')

    def test_timeout_is_pending_and_does_not_write_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            def timeout(*_):
                raise TimeoutError('synthetic timeout')
            result = resolver.resolve(self.identity, self.profiles, fetch_secondary=timeout, cache_path=target)
            self.assertEqual(result.state, 'pending')
            self.assertFalse(target.exists())

    def test_cancel_uses_application_cancel_type(self):
        event = threading.Event(); event.set()
        with self.assertRaises(core.CancelledError):
            self.resolve(cancel_event=event)

    def test_cancel_without_any_profile_or_year_is_not_hidden_as_pending(self):
        event = threading.Event(); event.set()
        with self.assertRaises(core.CancelledError):
            self.resolve(identity=online.MediaIdentity('Unknown Film'), profiles=[], cancel_event=event)

    def test_public_proof_validation_rebinds_year_and_known_ids(self):
        result, _ = self.resolve()
        self.assertTrue(resolver.validate_resolution(self.identity, result))
        for identity in (online.MediaIdentity('Mirror Film', year='2008'),
                         online.MediaIdentity('Mirror Film', year='2007', imdb_id='999'),
                         online.MediaIdentity('Mirror Film', year='2007', tmdb_id='999'),
                         online.MediaIdentity('Different Film', year='2007')):
            with self.subTest(identity=identity):
                self.assertFalse(resolver.validate_resolution(identity, result))

    def test_one_request_budget_does_not_start_tagline_page(self):
        result, calls = self.alias(max_requests=1)
        self.assertEqual(result.state, 'pending')
        self.assertEqual([call[0] for call in calls], ['secondary'])

    def test_fresh_cache_avoids_metadata_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            first, _ = self.resolve(cache_path=target)
            second, calls = self.resolve(cache_path=target)
            self.assertEqual(first.state, 'confirmed')
            self.assertTrue(second.cache_hit)
            self.assertEqual(calls, [])

    def test_changed_profiles_cannot_reuse_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            self.resolve(cache_path=target)
            result, calls = self.resolve(profiles=[resolver.WorkProfile('Mirror Film', '2007', '199', '299')],
                                         cache_path=target)
            self.assertFalse(result.cache_hit)
            self.assertEqual(result.state, 'pending')
            self.assertEqual(len(calls), 1)

    def test_stale_cache_requires_new_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            self.resolve(cache_path=target)
            data = json.loads(target.read_text(encoding='utf8'))
            for row in data['entries'].values():
                row['saved_at'] = time.time() - resolver.CACHE_SECONDS - 1
            target.write_text(json.dumps(data), encoding='utf8')
            result, calls = self.resolve(cache_path=target)
            self.assertFalse(result.cache_hit)
            self.assertEqual(result.state, 'confirmed')
            self.assertEqual(len(calls), 1)

    def test_cache_resolution_cannot_disagree_with_its_own_bound_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            self.resolve(cache_path=target)
            data = json.loads(target.read_text(encoding='utf8'))
            for row in data['entries'].values():
                row['resolution']['imdb_id'] = '999'
                row['resolution']['title'] = 'Wrong Film'
                row['resolution']['year'] = '1998'
            target.write_text(json.dumps(data), encoding='utf8')
            result, calls = self.resolve(cache_path=target)
            self.assertFalse(result.cache_hit)
            self.assertEqual(result.imdb_id, '100')
            self.assertEqual(len(calls), 1)

    def test_malformed_cache_shapes_do_not_crash_or_accept_identity(self):
        for content in ('[]', 'null', '{"schema":1,"entries":[]}', '{broken'):
            with tempfile.TemporaryDirectory() as directory, self.subTest(content=content):
                target = Path(directory) / 'cache.json'; target.write_text(content, encoding='utf8')
                result, calls = self.resolve(cache_path=target)
                self.assertEqual(result.state, 'confirmed')
                self.assertFalse(result.cache_hit)
                self.assertEqual(len(calls), 1)

    def test_default_secondary_uses_existing_subdl_key_field(self):
        with patch('subdl_subtitles.load_settings', return_value={'subdl_api_key': 'private-fixture-key'}), \
                patch.object(resolver, '_read_response', return_value='{"results":[]}') as read:
            result = resolver.fetch_secondary_catalogue('Mirror Film', resolver.RequestBudget())
            self.assertEqual(result, {'results': []})
            read.assert_called_once()
            request = read.call_args.args[0]
            self.assertIn('private-fixture-key', request.get_header('Authorization'))

    def test_default_secondary_missing_key_does_not_call_network(self):
        with patch('subdl_subtitles.load_settings', return_value={}), \
                patch.object(resolver, '_read_response') as read:
            with self.assertRaises(RuntimeError):
                resolver.fetch_secondary_catalogue('Mirror Film', resolver.RequestBudget())
            read.assert_not_called()

    def test_nested_malformed_cache_evidence_is_ignored(self):
        for evidence in (['broken'], {'acquisition': 'broken'}, [None], [3]):
            with tempfile.TemporaryDirectory() as directory, self.subTest(evidence=evidence):
                target = Path(directory) / 'cache.json'
                self.resolve(cache_path=target)
                data = json.loads(target.read_text(encoding='utf8'))
                for row in data['entries'].values():
                    row['resolution']['evidence'] = evidence
                target.write_text(json.dumps(data), encoding='utf8')
                result, calls = self.resolve(cache_path=target)
                self.assertFalse(result.cache_hit)
                self.assertEqual(result.state, 'confirmed')
                self.assertEqual(len(calls), 1)

    def test_early_cache_read_requires_exact_input_year_and_known_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            self.resolve(cache_path=target)
            self.assertIsNotNone(resolver.read_cached_resolution(self.identity, self.profiles, cache_path=target))
            for identity in (online.MediaIdentity('Mirror Film', year='2008'),
                             online.MediaIdentity('Other Film', year='2007'),
                             online.MediaIdentity('Mirror Film', year='2007', imdb_id='999'),
                             online.MediaIdentity('Mirror Film', year='2007', tmdb_id='999')):
                with self.subTest(identity=identity):
                    self.assertIsNone(resolver.read_cached_resolution(identity, self.profiles, cache_path=target))

    def test_early_cache_read_does_not_select_between_competing_saved_work_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            self.resolve(cache_path=target)
            second, _ = self.resolve(profiles=[resolver.WorkProfile('Mirror Film', '2007', '101', '201')],
                                     rows=[film(imdb='101', tmdb='201')], cache_path=target)
            self.assertEqual(second.state, 'confirmed')
            self.assertIsNone(resolver.read_cached_resolution(self.identity, (), cache_path=target))

    def test_early_cache_read_rejects_current_cross_id_contradiction(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cache.json'
            self.resolve(cache_path=target)
            conflicting = [resolver.WorkProfile('Mirror Film', '2007', '100', '299')]
            self.assertIsNone(resolver.read_cached_resolution(self.identity, conflicting, cache_path=target))


class ProviderIntegrationAcceptance(unittest.TestCase):
    @staticmethod
    def candidate(number=1, title='Mirror Film', year='2007', imdb='100', tmdb='200', **kwargs):
        return online.SubtitleCandidate(number, 'generic.srt', 'generic.BluRay', 'en', 1, 1.0, False, False,
                                        feature_title=title, feature_year=year,
                                        feature_imdb_id=imdb, feature_tmdb_id=tmdb, **kwargs)

    def test_normal_resolved_feature_uses_no_extra_resolver_requests(self):
        identity = online.MediaIdentity('Mirror Film', year='2007', imdb_id='100', tmdb_id='200')
        item = self.candidate()
        with patch.object(online, 'identify_media', return_value=identity), \
                patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                patch.object(online, '_resolve_feature_identity', return_value=identity), \
                patch.object(online, '_search_pages', return_value=([item], 1, False)), \
                patch.object(online, 'resolve_work_identity') as extra:
            _, items, _ = online.search('test-key', 'Mirror.Film.2007.mkv', 'en')
        extra.assert_not_called()
        self.assertEqual(items, [item])
        self.assertEqual(item.identity_admission_state, 'auto')

    def test_existing_confirmed_localized_path_does_not_start_new_metadata(self):
        base = online.MediaIdentity('镜像电影', year='2007')
        resolved = online.MediaIdentity('镜像电影', original_title='Mirror Film', year='2007', imdb_id='100', tmdb_id='200')
        item = self.candidate()
        meta = online.SearchMeta(query_mode='feature', feature_lookup_status='resolved')
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'online.json'
            online.remember_canonical_title('镜像电影', '2007', 'Mirror Film', settings,
                                           confirmed_year='2007', confirmed_identity='imdb:100')
            self.assertEqual(online.canonical_title_source('镜像电影', '2007', settings), '缓存')
            with patch.object(online, 'settings_path', return_value=settings), \
                    patch.object(online, 'identify_media', return_value=base), \
                    patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                    patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                    patch.object(online, '_search_localized_title', return_value=(resolved, [item], meta)), \
                    patch.object(online, '_recover_work_identity', return_value=None) as extra:
                _, items, _ = online.search('test-key', '镜像电影.2007.mkv', 'en')
        extra.assert_not_called()
        self.assertEqual(items, [item])

    def test_uncached_native_metadata_success_never_starts_model_path(self):
        base = online.MediaIdentity('镜像电影', year='2007')
        resolved = online.MediaIdentity('镜像电影', original_title='Mirror Film', year='2007', imdb_id='100', tmdb_id='200')
        item = self.candidate()
        meta = online.SearchMeta(query_mode='confirmed-work-id', feature_lookup_status='resolved')
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'online.json'
            with patch.object(online, 'settings_path', return_value=settings), \
                    patch.object(online, 'identify_media', return_value=base), \
                    patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                    patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                    patch.object(online, '_recover_work_identity', return_value=(resolved, [item], meta)) as metadata, \
                    patch.object(online, '_search_localized_title', side_effect=AssertionError('model path must not start')) as old, \
                    patch.object(online, 'resolve_canonical_title', side_effect=AssertionError('model must not start')) as model:
                _, items, _ = online.search('test-key', '镜像电影.2007.mkv', 'en')
            metadata.assert_called_once()
            old.assert_not_called()
            model.assert_not_called()
            self.assertEqual(items, [item])

    def test_uncached_native_metadata_failure_keeps_original_model_fallback(self):
        base = online.MediaIdentity('镜像电影', year='2007')
        resolved = online.MediaIdentity('镜像电影', original_title='Mirror Film', year='2007', imdb_id='100', tmdb_id='200')
        item = self.candidate()
        meta = online.SearchMeta(query_mode='feature', feature_lookup_status='resolved')
        calls = []
        def metadata(*_):
            calls.append('metadata'); return None
        def legacy(*_):
            calls.append('original'); return resolved, [item], meta
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(online, 'settings_path', return_value=Path(directory) / 'online.json'), \
                    patch.object(online, 'identify_media', return_value=base), \
                    patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                    patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                    patch.object(online, '_recover_work_identity', side_effect=metadata), \
                    patch.object(online, '_search_localized_title', side_effect=legacy):
                _, items, _ = online.search('test-key', '镜像电影.2007.mkv', 'en')
        self.assertEqual(calls, ['metadata', 'original'])
        self.assertEqual(items, [item])

    def test_exact_hash_fast_path_uses_no_extra_resolver(self):
        identity = online.MediaIdentity('Mirror Film', year='2007')
        item = self.candidate(moviehash_match=True)
        with patch.object(online, 'identify_media', return_value=identity), \
                patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                patch.object(online, 'opensubtitles_movie_hash', return_value='0123456789abcdef'), \
                patch.object(online, '_search_pages', return_value=([item], 1, False)), \
                patch.object(online, 'resolve_work_identity') as extra:
            _, items, meta = online.search('test-key', 'Mirror.Film.2007.mkv', 'en')
        extra.assert_not_called()
        self.assertEqual(meta.query_mode, 'hash')
        self.assertEqual(items, [item])
        self.assertEqual(item.identity_key, 'hash:0123456789abcdef')

    def test_manual_search_preserves_pending_and_filtered_rows(self):
        identity = online.MediaIdentity('Mirror Film', year='2007')
        first, second = self.candidate(), self.candidate(2, imdb='101', tmdb='201')
        wrong = self.candidate(3, title='Unrelated Film', imdb='500', tmdb='600')
        def no_feature(_key, _identity, **_kw):
            diagnostic = online._FEATURE_LOOKUP_DIAGNOSTICS.get()
            diagnostic.status = 'ambiguous'; diagnostic.reason = 'competing work IDs'
            return None
        with patch.object(online, 'identify_media', return_value=identity), \
                patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                patch.object(online, '_resolve_feature_identity', side_effect=no_feature), \
                patch.object(online, '_search_pages', return_value=([first, second, wrong], 3, False)), \
                patch.object(online, 'resolve_work_identity', return_value=resolver.Resolution('pending', 'no proof')):
            _, items, _ = online.search('test-key', 'Mirror.Film.2007.mkv', 'en')
        self.assertEqual(items, [first, second, wrong])
        self.assertEqual([item.identity_admission_state for item in items], ['pending', 'pending', 'filtered'])
        self.assertFalse(first.identity_verified)
        self.assertFalse(second.identity_verified)

    def test_string_hash_never_acts_as_positive_admission(self):
        identity = online.MediaIdentity('Orbit Motion The future begins', year='2007')
        item = self.candidate(title='Orbit Motion', moviehash_match='true')
        online._apply_identity_admission([item], identity, 'ambiguous')
        self.assertEqual(item.identity_admission_state, 'pending')

    def test_known_id_conflict_beats_boolean_hash_admission(self):
        identity = online.MediaIdentity('Mirror Film', year='2007', imdb_id='999')
        item = self.candidate(moviehash_match=True)
        online._apply_identity_admission([item], identity, 'resolved')
        self.assertEqual(item.identity_admission_state, 'filtered')

    def test_automatic_pending_never_downloads_or_claims_timing_failure(self):
        identity = online.MediaIdentity('Mirror Film', year='2007')
        item = self.candidate(identity_admission_state='pending', identity_admission_reason='作品ID尚未确认')
        download = Mock(side_effect=AssertionError('pending source must not download'))
        service = SimpleNamespace(load_settings=lambda: {'key': 'test-key'}, download=download,
                                  search=lambda *_, **__: (identity, [item], online.SearchMeta(query_mode='title-year')))
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / 'Mirror.Film.2007.mkv'; movie.write_bytes(b'fixture')
            audit = subtitle_audit.FilmAudit(movie, Path(directory) / 'report')
            with patch.object(smart, 'PROVIDERS', (('Test', service, 'key'),)), \
                    patch.object(smart, 'filename_container_identity_conflict', return_value=''), \
                    patch.object(smart.pro_core, 'prepare_shared_subtitle_content_audio', return_value=None), \
                    subtitle_audit.scope(audit):
                with self.assertRaises(RuntimeError) as caught:
                    smart.find_verified_english(str(movie), 0, messages.append)
            self.assertEqual(download.call_count, 0)
            self.assertTrue(any(row['state'] == 'identity_pending' for row in audit.records))
            self.assertFalse(any(row.get('attempted') for row in audit.records))
            self.assertNotIn('均未通过安全核验', str(caught.exception))

    def test_positive_identity_does_not_override_commentary_role(self):
        identity = online.MediaIdentity('Mirror Film', year='2007', imdb_id='100', tmdb_id='200')
        item = self.candidate(identity_admission_state='auto')
        item.release = 'Mirror.Film.2007.Commentary'; item.file_name = 'Mirror.Film.commentary.srt'
        self.assertTrue(smart._candidate_role_conflict(item, identity))

    def test_subdl_normal_candidate_remains_compatible_without_new_fields(self):
        identity = subdl.MediaIdentity('Mirror Film', year='2007', imdb_id='100', tmdb_id='200')
        item = subdl.SubtitleCandidate('123', 'Mirror.Film.2007.srt', 'Mirror.Film.2007.BluRay',
                                     'en', 1, 1.0, False, False,
                                     feature_title='Mirror Film', feature_year='2007',
                                     identity_verified=True, identity_key='imdb:100')
        self.assertEqual(smart._candidate_identity_conflict(item, identity), '')

    def test_automatic_search_forwards_cancel_event_to_identity_metadata(self):
        event = threading.Event()
        identity = online.MediaIdentity('Mirror Film', year='2007')
        meta = online.SearchMeta(query_mode='title-year')
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / 'Mirror.Film.2007.mkv'; movie.write_bytes(b'fixture')
            audit = subtitle_audit.FilmAudit(movie, Path(directory) / 'report')
            with patch.object(smart, 'PROVIDERS', (('OpenSubtitles', online, 'api_key'),)), \
                    patch.object(online, 'load_settings', return_value={'api_key': 'test-key'}), \
                    patch.object(online, 'search', return_value=(identity, [], meta)) as search, \
                    patch.object(smart, 'filename_container_identity_conflict', return_value=''), \
                    patch.object(smart.pro_core, 'prepare_shared_subtitle_content_audio', return_value=None), \
                    subtitle_audit.scope(audit):
                with self.assertRaises(RuntimeError):
                    smart.find_verified_english(str(movie), 0, lambda *_: None, cancel_event=event)
            self.assertIs(search.call_args.kwargs.get('cancel_event'), event)

    def test_warm_confirmed_identity_skips_broad_and_metadata_requests(self):
        identity = online.MediaIdentity('Mirror Film', year='2007')
        profiles = [resolver.WorkProfile('Mirror Film', '2007', '100', '200', 'opensubtitles'),
                    resolver.WorkProfile('Mirror Film', '2007', '101', '', 'opensubtitles')]
        item = self.candidate()
        calls = []
        def unresolved(_key, _identity, **_kw):
            diagnostic = online._FEATURE_LOOKUP_DIAGNOSTICS.get()
            diagnostic.status = 'ambiguous'; diagnostic.reason = 'competing work IDs'
            diagnostic.profiles.extend(profiles)
            return None
        def pages(_key, params, _language, **_kw):
            calls.append(params)
            self.assertNotIn('query', params, 'warm work proof must avoid broad keyword search')
            self.assertEqual(params.get('imdb_id'), '100')
            return [item], 1, False
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / 'online.json'
            target = settings.with_name('work-identity-evidence.json')
            resolver.resolve(identity, profiles, fetch_secondary=lambda *_: {'results': [film()]}, cache_path=target)
            with patch.object(online, 'settings_path', return_value=settings), \
                    patch.object(online, 'identify_media', return_value=identity), \
                    patch.object(online, 'filename_container_identity_conflict', return_value=''), \
                    patch.object(online, 'opensubtitles_movie_hash', return_value=''), \
                    patch.object(online, '_resolve_feature_identity', side_effect=unresolved), \
                    patch.object(online, '_search_pages', side_effect=pages), \
                    patch.object(resolver, 'fetch_secondary_catalogue', side_effect=AssertionError('warm must not acquire metadata')):
                resolved, items, meta = online.search('test-key', 'Mirror.Film.2007.mkv', 'en')
            self.assertEqual(resolved.imdb_id, '100')
            self.assertEqual(items, [item])
            self.assertTrue(meta.identity_resolution_cache_hit)
            self.assertEqual(meta.identity_resolution_requests, 0)
            self.assertEqual(len(calls), 1)


class CrossProviderProofAcceptance(unittest.TestCase):
    def setUp(self):
        self.identity = subdl.MediaIdentity('Mirror Film', year='2007')
        self.proof = resolver.resolve(self.identity,
                                      [resolver.WorkProfile('Mirror Film', '2007', '100', '200')],
                                      fetch_secondary=lambda *_: {'results': [film()]}, cache_path=False)
        self.assertEqual(self.proof.state, 'confirmed')

    def invoke(self, rows, *, identity=None, proof=None, subtitle_metadata=None):
        requests = []
        item = subdl.SubtitleCandidate('item1', 'Mirror.Film.2007.srt', 'Mirror.Film.2007.BluRay',
                                      'en', 1, 1.0, False, False)
        def request(_key, path, *, params=None):
            requests.append((path, params))
            if path == '/movies/search':
                return {'results': rows}
            self.assertEqual(path, '/subtitles/search')
            return {'subtitles': [{'id': 'item1'}], **(subtitle_metadata or {})}
        with patch.object(subdl, 'identify_media', return_value=identity or self.identity), \
                patch.object(subdl, 'filename_container_identity_conflict', return_value=''), \
                patch.object(subdl, '_request_v2', side_effect=request), \
                patch.object(subdl, '_candidates_from_response', return_value=[item]), \
                patch.object(subdl, '_resolve_movie', side_effect=AssertionError('confirmed proof must not broad retry')):
            result = subdl.search('test-key', 'Mirror.Film.2007.mkv', 'en',
                                  confirmed_work_identity=self.proof if proof is None else proof)
        return result, requests

    def test_confirmed_identity_is_rechecked_then_scoped_to_secondary_movie_id(self):
        row = film(); row['sd_id'] = 'sd123'
        (identity, items, _), calls = self.invoke([row])
        self.assertEqual(len(calls), 2)
        self.assertIn('100', calls[0][1]['q'])
        self.assertEqual(calls[1][1]['sd_id'], 'sd123')
        self.assertEqual((identity.imdb_id, identity.tmdb_id), ('100', '200'))
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].identity_verified)

    def test_cross_provider_conflicting_tmdb_cannot_restart_broad_search(self):
        row = film(tmdb='999'); row['sd_id'] = 'sd999'
        (_, items, meta), calls = self.invoke([row])
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')
        self.assertEqual(len(calls), 1)

    def test_cross_provider_conflicting_imdb_cannot_restart_broad_search(self):
        row = film(imdb='999'); row['sd_id'] = 'sd999'
        (_, items, meta), calls = self.invoke([row])
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')
        self.assertEqual(len(calls), 1)

    def test_cross_provider_wrong_year_does_not_use_existing_id(self):
        row = film(year='2008'); row['sd_id'] = 'sd999'
        (_, items, meta), calls = self.invoke([row])
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')
        self.assertEqual(len(calls), 1)

    def test_cross_provider_competing_same_title_ids_are_not_silently_ignored(self):
        first = film(); first['sd_id'] = 'sd123'
        second = film(imdb='101', tmdb='201'); second['sd_id'] = 'sd124'
        (_, items, meta), calls = self.invoke([first, second])
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')
        self.assertEqual(len(calls), 1)

    def test_cross_provider_proof_for_different_movie_is_not_used(self):
        row = film(); row['sd_id'] = 'sd123'
        (_, items, meta), calls = self.invoke([row], identity=subdl.MediaIdentity('Different Film', year='2007'))
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')
        self.assertEqual(calls, [])

    def test_subtitle_response_numeric_site_id_matches_prefixed_lookup_id(self):
        lookup = film(); lookup['sd_id'] = 'sd123'
        echo = film(); echo['sd_id'] = 123
        (_, items, meta), calls = self.invoke([lookup], subtitle_metadata={'results': [echo]})
        self.assertEqual(len(items), 1)
        self.assertEqual(meta.feature_lookup_status, 'resolved')
        self.assertEqual(len(calls), 2)

    def test_subtitle_response_other_movie_metadata_is_not_accepted(self):
        lookup = film(); lookup['sd_id'] = 'sd123'
        echo = film('Unrelated Film', imdb='900', tmdb='901'); echo['sd_id'] = 'sd123'
        (_, items, meta), calls = self.invoke([lookup], subtitle_metadata={'results': [echo]})
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')
        self.assertEqual(len(calls), 2)

    def test_subtitle_response_same_id_wrong_year_is_not_accepted(self):
        lookup = film(); lookup['sd_id'] = 'sd123'
        echo = film(year='2008'); echo['sd_id'] = 123
        (_, items, meta), _ = self.invoke([lookup], subtitle_metadata={'results': [echo]})
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')

    def test_subtitle_response_same_id_conflicting_mapping_is_not_accepted(self):
        lookup = film(); lookup['sd_id'] = 'sd123'
        echo = film(tmdb='999'); echo['sd_id'] = 123
        (_, items, meta), _ = self.invoke([lookup], subtitle_metadata={'results': [echo]})
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')

    def test_subtitle_response_different_site_id_does_not_use_lookup_scope(self):
        lookup = film(); lookup['sd_id'] = 'sd123'
        echo = film(); echo['sd_id'] = 124
        (_, items, meta), _ = self.invoke([lookup], subtitle_metadata={'results': [echo]})
        self.assertEqual(items, [])
        self.assertEqual(meta.feature_lookup_status, 'identity-conflict')


if __name__ == '__main__':
    unittest.main(verbosity=2)
