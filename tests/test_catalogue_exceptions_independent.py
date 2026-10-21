"""Independent catalogue-year and numeric-alias tests; no real network/media."""
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
import work_identity_resolver as work


def profile(title='300', year='2006', imdb='416449', tmdb='1271', kind='movie'):
    return work.WorkProfile(title, year, imdb, tmdb, 'opensubtitles-features', kind, (title,))


def movie(title='300', year='2007', imdb='416449', tmdb='1271', kind='movie'):
    return {'name': title, 'original_name': title, 'year': year, 'imdb_id': imdb,
            'tmdb_id': tmdb, 'type': kind, 'sd_id': 'sd123'}


def official_movie(title='300', year='2007', tmdb='1271', kind='Movie', url=None):
    url = url or 'https://www.themoviedb.org/movie/' + tmdb
    record = {'@type': kind, 'name': title, 'url': url,
              'releasedEvent': [{'startDate': year + '-03-09'}]}
    return ('<html><head><link rel="canonical" href="' + url + '"></head><body>'
            '<h2><a href="/movie/' + tmdb + '">' + title + '</a>'
            '<span class="release_date">(' + year + ')</span></h2>'
            '<script type="application/ld+json">' + json.dumps(record) + '</script></body></html>')


def official_titles(title='Nineteen Eighty-Four', year='1984', tmdb='9314', aliases=('1984',)):
    url = 'https://www.themoviedb.org/movie/' + tmdb
    rows = ''.join('<tr><td>' + alias + '</td><td></td></tr>' for alias in aliases)
    return ('<html><head><title>' + title + ' (' + year + ') - Alternative Titles</title>'
            '<link rel="canonical" href="' + url + '/titles">'
            '<meta property="og:url" content="' + url + '">'
            '<meta property="og:title" content="' + title + '"></head><body>'
            '<section class="header small"><div class="title"><h2><a href="/movie/' + tmdb + '">'
            + title + '</a><span class="release_date">(' + year + ')</span></h2></div></section>'
            '<div id="media_v4"><div class="white_column"><section class="panel">'
            '<table class="card releases titles"><tbody>' + rows + '</tbody></table>'
            '</section></div></div></body></html>')


class CatalogueExceptionsIndependentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': str(self.root/'Local'),
            'APPDATA': str(self.root/'Roaming'), 'SUBFLOW_RUNTIME_DIR': str(self.root/'runtime')}))
        for target in ('socket.socket.connect', 'socket.create_connection', 'urllib.request.OpenerDirector.open'):
            self.stack.enter_context(patch(target, side_effect=AssertionError('network forbidden')))

    def recover(self, *, name='300 2006.mkv', profiles=None, rows=None, page=None, session=None, cache=False,
                metadata=None):
        profiles = [profile()] if profiles is None else profiles
        rows = [movie()] if rows is None else rows
        page = official_movie() if page is None else page
        session = session or work.RecoverySession(cache_path=cache)
        base = online.MediaIdentity(Path(name).stem, original_title=Path(name).stem)
        calls = []
        def secondary(query, budget):
            calls.append(('secondary', query, budget.request_count))
            return {'results': rows, **(metadata or {})}
        def public(tmdb, budget):
            calls.append(('public', tmdb, budget.request_count))
            return page
        with patch.object(work, 'fetch_secondary_catalogue', side_effect=secondary), \
                patch.object(work, 'fetch_public_movie', side_effect=public):
            result = work.recover_filename_year(name, base, session, profiles)
        return result, calls, session, base

    def test_same_double_id_adjacent_catalogue_years_require_bound_official_receipt(self):
        result, calls, session, base = self.recover()
        self.assertEqual(result.state, 'confirmed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.budget.request_count, 2)
        receipt = work.catalogue_year_evidence(result)
        self.assertIsNotNone(receipt)
        self.assertEqual((receipt['input_year'], receipt['canonical_year']), ('2006', '2007'))
        self.assertEqual(receipt['source_years'], {'input': ['2006'],
            'opensubtitles-features': ['2006'], 'subdl-v2': ['2007']})
        self.assertEqual(receipt['official_type'], 'movie')
        self.assertTrue(work.validate_filename_resolution('300 2006.mkv', base, result, [profile()]))

    def test_year_permission_is_source_title_and_both_id_specific(self):
        proof, _calls, _session, _base = self.recover()
        for source, year in (('opensubtitles-features', '2006'), ('subdl-v2', '2007')):
            with self.subTest(source=source):
                self.assertTrue(work.catalogue_year_matches(proof, source, year,
                    title='300', imdb_id='416449', tmdb_id='1271'))
        for source, year, title, imdb, tmdb in (
            ('subdl-v2', '2006', '300', '416449', '1271'),
            ('opensubtitles-features', '2007', '300', '416449', '1271'),
            ('unknown-provider', '2007', '300', '416449', '1271'),
            ('subdl-v2', '2007', 'Other Film', '416449', '1271'),
            ('subdl-v2', '2007', '300', '999', '1271'),
            ('subdl-v2', '2007', '300', '416449', '999'),
            ('subdl-v2', '2007', '300', '416449', ''),
        ):
            with self.subTest(source=source, year=year, title=title, imdb=imdb, tmdb=tmdb):
                self.assertFalse(work.catalogue_year_matches(proof, source, year,
                    title=title, imdb_id=imdb, tmdb_id=tmdb))

    def test_two_or_more_year_gap_never_receives_a_year_receipt(self):
        for year in ('2004', '2008', '2010'):
            with self.subTest(year=year):
                proof, _calls, _session, _base = self.recover(rows=[movie(year=year)],
                    page=official_movie(year=year))
                self.assertEqual(proof.state, 'pending')
                self.assertIsNone(work.catalogue_year_evidence(proof))

    def test_official_third_year_primary_year_or_wrong_movie_cannot_authorize(self):
        for page in (official_movie(year='2008'), official_movie(year='2006'),
                     official_movie(title='Other Film'), official_movie(tmdb='999'),
                     official_movie(kind='TVSeries'), official_movie(url='https://other.invalid/movie/1271')):
            with self.subTest(page=page[:130]):
                proof, _calls, _session, _base = self.recover(page=page)
                self.assertEqual(proof.state, 'pending')
                self.assertIsNone(work.catalogue_year_evidence(proof))

    def test_one_matching_id_missing_id_or_wrong_title_type_cannot_reconcile_years(self):
        for row in (movie(imdb='999'), movie(tmdb='999'), movie(imdb=''), movie(tmdb=''),
                    movie(title='Other Film'), movie(kind='tv')):
            with self.subTest(row=row):
                proof, _calls, _session, _base = self.recover(rows=[row])
                self.assertEqual(proof.state, 'pending')
                self.assertIsNone(work.catalogue_year_evidence(proof))
        proof, _calls, _session, _base = self.recover(profiles=[profile(tmdb='')])
        self.assertEqual(proof.state, 'pending')

    def test_primary_original_year_must_equal_input_year_without_competition(self):
        for records in ([profile(year='2005')], [profile(), profile(imdb='999', tmdb='998')],
                        [profile(), profile(year='2007')], [profile(), profile(kind='tv')]):
            with self.subTest(records=records):
                proof, _calls, _session, _base = self.recover(profiles=records)
                self.assertEqual(proof.state, 'pending')
                self.assertIsNone(work.catalogue_year_evidence(proof))

    def test_secondary_competing_rows_and_saturation_cannot_be_hidden_by_first_row(self):
        for rows in ([movie(), movie(imdb='999', tmdb='998')], [movie(), movie(kind='tv')],
                     [movie() for _ in range(work.CATALOGUE_LIMIT)]):
            with self.subTest(count=len(rows)):
                proof, _calls, _session, _base = self.recover(rows=rows)
                self.assertEqual(proof.state, 'pending')

    def test_missing_or_tampered_year_receipt_never_extends_a_source_year(self):
        proof, _calls, _session, base = self.recover()
        receipt = work.catalogue_year_evidence(proof)
        self.assertIsNotNone(receipt)
        for mutation in (None, {'input_year': '2005'}, {'canonical_year': '2008'},
                         {'official_type': 'tv'}, {'source_years': {'subdl-v2': ['2006','2007']}}):
            with self.subTest(mutation=mutation):
                # Spell the selection explicitly: other acquisition entries
                # must remain byte-for-byte the original metadata proof.
                evidence = tuple((dict(entry, **mutation) if mutation is not None else None)
                    if entry.get('acquisition') == receipt['acquisition'] else entry
                    for entry in proof.evidence)
                altered = replace(proof, evidence=tuple(entry for entry in evidence if entry is not None))
                self.assertIsNone(work.catalogue_year_evidence(altered))
                self.assertFalse(work.catalogue_year_matches(altered, 'subdl-v2', '2007',
                    title='300', imdb_id='416449', tmdb_id='1271'))
                self.assertFalse(work.validate_filename_resolution('300 2006.mkv', base, altered, [profile()]))

    def test_year_receipt_disk_and_session_reuse_revalidate_current_profiles(self):
        cache = self.root/'year-proof.json'
        session = work.RecoverySession(cache_path=cache)
        proof, _calls, _session, base = self.recover(session=session)
        self.assertEqual(proof.state, 'confirmed')
        original_cache = cache.read_text(encoding='utf-8')
        for reuse in (session, work.RecoverySession(cache_path=cache)):
            with self.subTest(memory=reuse is session):
                cache.write_text(original_cache, encoding='utf-8')
                warm, calls, _session, _base = self.recover(session=reuse, rows=[], page='')
                self.assertEqual(warm.state, 'confirmed')
                self.assertEqual(calls, [])
                self.assertIsNotNone(work.catalogue_year_evidence(warm))
                self.assertFalse(work.validate_filename_resolution('300 2007.mkv', base, warm, [profile()]))
                pending, calls, _session, _base = self.recover(session=reuse,
                    profiles=[profile(), profile(imdb='999', tmdb='998')])
                self.assertEqual(pending.state, 'pending')
                self.assertEqual(calls, [])

    def test_receipt_identity_provenance_and_duplicate_entries_cannot_be_forged(self):
        proof, _calls, _session, base = self.recover()
        receipt = work.catalogue_year_evidence(proof)
        self.assertIsNotNone(receipt)
        for section, changes in (
            ('primary_profile', {'source':'unknown-provider'}),
            ('primary_profile', {'title':'Other Film','aliases':['Other Film']}),
            ('primary_profile', {'year':'2005'}),
            ('primary_profile', {'imdb_id':'999'}),
            ('primary_profile', {'tmdb_id':'999'}),
            ('primary_profile', {'feature_type':'tv'}),
            ('secondary_profile', {'source':'unknown-provider'}),
            ('secondary_profile', {'year':'2008'}),
            ('secondary_profile', {'imdb_id':'999'}),
            ('official', {'title':'Other Film'}),
            ('official', {'year':'2008'}),
            ('official', {'tmdb_id':'999'}),
            ('official', {'source':'https://other.invalid/movie/1271'}),
        ):
            with self.subTest(section=section, changes=changes):
                forged = json.loads(json.dumps(receipt))
                forged[section].update(changes)
                evidence = tuple(forged if entry.get('acquisition') == receipt['acquisition'] else entry
                                 for entry in proof.evidence)
                altered = replace(proof, evidence=evidence)
                self.assertIsNone(work.catalogue_year_evidence(altered))
                self.assertFalse(work.catalogue_year_matches(altered,'subdl-v2','2007',
                    title='300',imdb_id='416449',tmdb_id='1271'))
                self.assertFalse(work.validate_filename_resolution('300 2006.mkv',base,altered,[profile()]))
        duplicate = replace(proof, evidence=(*proof.evidence, receipt))
        self.assertIsNone(work.catalogue_year_evidence(duplicate))
        for altered in (replace(proof,year='2006'), replace(proof,imdb_id='999'),
                        replace(proof,tmdb_id='999'), replace(proof,state='pending')):
            with self.subTest(state=altered.state, year=altered.year, imdb=altered.imdb_id, tmdb=altered.tmdb_id):
                self.assertIsNone(work.catalogue_year_evidence(altered))

    def test_year_official_lookup_shares_deadline_and_cancellation(self):
        now = [100.]
        session = work.RecoverySession(cache_path=False)
        session.budget = work.RequestBudget(started=now[0], time_budget_seconds=15.)
        def secondary(*_args):
            now[0] += 8.
            return {'results': [movie()]}
        def public(*_args):
            now[0] += 8.
            return official_movie()
        with patch.object(work.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(work, 'fetch_secondary_catalogue', side_effect=secondary), \
                patch.object(work, 'fetch_public_movie', side_effect=public):
            result = work.recover_filename_year('300 2006.mkv', online.MediaIdentity('300 2006'), session, [profile()])
        self.assertEqual(result.state, 'pending')
        self.assertEqual(session.budget.request_count, 2)
        event = threading.Event()
        session = work.RecoverySession(cache_path=False, cancel_event=event)
        def cancelled(*_args):
            event.set()
            return official_movie()
        with patch.object(work, 'fetch_secondary_catalogue', return_value={'results':[movie()]}), \
                patch.object(work, 'fetch_public_movie', side_effect=cancelled), self.assertRaises(core.CancelledError):
            work.recover_filename_year('300 2006.mkv', online.MediaIdentity('300 2006'), session, [profile()])

    def test_numeric_english_reading_is_only_a_bounded_search_proposal(self):
        self.assertIn('nineteen eighty four', guard.numeric_title_alias_proposals('1984'))
        for title in ('1917','1984','2012','300','1408','2001','8','21'):
            with self.subTest(title=title):
                self.assertLessEqual(len(guard.numeric_title_alias_proposals(title)), 2)
                self.assertEqual(guard.clean_video_title(title+'.mkv'), (title, ''))
        for title in ('01984','A1984','1984 Remux','Blade Runner 2049','1984.0','10000'):
            with self.subTest(title=title):
                self.assertEqual(guard.numeric_title_alias_proposals(title), ())

    def test_numeric_alias_requires_two_requests_and_official_original_number_alias(self):
        proof, calls, session, base = self.recover(name='1984 1984.mkv', profiles=[],
            rows=[movie('Nineteen Eighty-Four','1984','87804','9314')], page=official_titles())
        self.assertEqual(proof.state, 'confirmed')
        self.assertEqual(len(calls), 2)
        self.assertEqual(session.budget.request_count, 2)
        self.assertEqual(work.title_key(calls[0][1]), work.title_key('Nineteen Eighty-Four'))
        self.assertEqual(calls[1][1], '9314')
        self.assertTrue(work.validate_filename_resolution('1984 1984.mkv', base, proof))
        self.assertIsNone(work.catalogue_year_evidence(proof))

    def test_spelled_numeric_title_without_an_official_number_alias_stays_pending(self):
        for page in (official_titles(aliases=('Other Alias',)), official_titles(year='1985'),
                     official_titles(tmdb='999'), official_titles(title='Other Film')):
            with self.subTest(page=page[:100]):
                proof, _calls, _session, _base = self.recover(name='1984 1984.mkv', profiles=[],
                    rows=[movie('Nineteen Eighty-Four','1984','87804','9314')], page=page)
                self.assertEqual(proof.state, 'pending')

    def test_numeric_alias_cannot_hide_wrong_year_type_missing_ids_or_competitors(self):
        good = movie('Nineteen Eighty-Four','1984','87804','9314')
        for rows in ([dict(good,year='1985')], [dict(good,type='tv')], [dict(good,imdb_id='')],
                     [dict(good,tmdb_id='')], [good,dict(good,imdb_id='999',tmdb_id='998')],
                     [good,dict(good,imdb_id='999',tmdb_id='')]):
            with self.subTest(rows=rows):
                proof, _calls, _session, _base = self.recover(name='1984 1984.mkv', profiles=[],
                    rows=rows,page=official_titles())
                self.assertEqual(proof.state, 'pending')

    def test_existing_numeric_original_profile_keeps_id_path_before_spelling_proposals(self):
        proof, calls, session, _base = self.recover(name='1984 1984.mkv',
            profiles=[profile('1984','1984','87804','9314')],
            rows=[movie('1984','1984','87804','9314')], page='')
        self.assertEqual(proof.state, 'confirmed')
        self.assertEqual(calls, [('secondary','tt0087804',1)])
        self.assertEqual(session.budget.request_count, 1)

    def test_numeric_alias_failure_does_not_restart_budget_or_query_second_spelling(self):
        session = work.RecoverySession(cache_path=False)
        first, calls, _session, _base = self.recover(name='1984 1984.mkv',profiles=[],rows=[],session=session)
        self.assertEqual(first.state,'pending')
        self.assertEqual(len(calls),1)
        second, calls, _session, _base = self.recover(name='1984 1984.mkv',profiles=[],rows=[],session=session)
        self.assertEqual(second.state,'pending')
        self.assertEqual(calls,[])
        self.assertLessEqual(session.budget.request_count,2)

    def test_actual_online_entry_keeps_confirmed_primary_observed_year_candidate(self):
        name='300 2006.mkv'
        base=online.MediaIdentity('300 2006',original_title='300 2006')
        response={'data':[{'attributes':{'title':'300','original_title':'300','year':'2006',
            'imdb_id':'416449','tmdb_id':'1271','feature_type':'movie'}}]}
        item=online.SubtitleCandidate(1,'300.srt','300.2006.BluRay','en',1,1.,False,False,
            feature_title='300',feature_year='2006',feature_imdb_id='416449',feature_tmdb_id='1271')
        def original(_key,identity,**_kwargs):
            return online._feature_identity_from_response(response,identity,
                diagnostics=online._FEATURE_LOOKUP_DIAGNOSTICS.get())
        with patch.object(online,'identify_media',return_value=base), \
                patch.object(online,'filename_container_identity_conflict',return_value=''), \
                patch.object(online,'opensubtitles_movie_hash',return_value=''), \
                patch.object(online,'_resolve_feature_identity',side_effect=original), \
                patch.object(online,'_search_pages',return_value=([item],1,False)), \
                patch.object(work,'fetch_secondary_catalogue',return_value={'results':[movie()]}), \
                patch.object(work,'fetch_public_movie',return_value=official_movie()):
            identity,items,meta=online.search('fixture',name,'en',
                identity_recovery_session=work.RecoverySession(cache_path=False))
        self.assertEqual(identity.year,'2007')
        self.assertEqual(items,[item])
        self.assertEqual(meta.identity_resolution_status,'confirmed')
        self.assertEqual(item.identity_admission_state,'auto')

    def test_actual_secondary_handoff_requires_year_receipt_for_the_changed_year(self):
        proof,_calls,_session,_base=self.recover()
        self.assertEqual(proof.state,'confirmed')
        item=subdl.SubtitleCandidate('1','300.srt','300.2007.BluRay','en',1,1.,False,False)
        def request(_key,path,**_kwargs):
            return {'results':[movie()]} if path=='/movies/search' else {'subtitles':[{'id':'1'}]}
        with patch.object(subdl,'identify_media',return_value=subdl.MediaIdentity('300 2006')), \
                patch.object(subdl,'filename_container_identity_conflict',return_value=''), \
                patch.object(subdl,'_request_v2',side_effect=request), \
                patch.object(subdl,'_candidates_from_response',return_value=[item]):
            identity,items,meta=subdl.search('fixture','300 2006.mkv','en',confirmed_work_identity=proof)
        self.assertEqual(identity.year,'2007')
        self.assertEqual(items,[item])
        self.assertEqual(item.feature_year,'2007')
        self.assertEqual(meta.identity_resolution_status,'confirmed')
        receipt=work.catalogue_year_evidence(proof)
        altered=replace(proof,evidence=tuple(entry for entry in proof.evidence
            if entry.get('acquisition')!=receipt['acquisition']))
        with patch.object(subdl,'identify_media',return_value=subdl.MediaIdentity('300 2006')), \
                patch.object(subdl,'filename_container_identity_conflict',return_value=''), \
                patch.object(subdl,'_request_v2',side_effect=AssertionError('No year expansion without receipt')):
            _identity,items,meta=subdl.search('fixture','300 2006.mkv','en',confirmed_work_identity=altered)
        self.assertEqual(items,[])
        self.assertNotEqual(meta.identity_resolution_status,'confirmed')

    def test_numeric_cache_requires_the_bound_proposal_and_complete_catalogue_receipt(self):
        name='1984 1984.mkv'
        cache=self.root/'numeric-alias.json'
        proof,_calls,_session,base=self.recover(name=name,profiles=[],cache=cache,
            rows=[movie('Nineteen Eighty-Four','1984','87804','9314')],page=official_titles())
        self.assertEqual(proof.state,'confirmed')
        original=cache.read_text(encoding='utf8')
        receipt=next(row for row in proof.evidence if row.get('acquisition')=='numeric-title-alias-proposal')
        warm,calls,_session,_base=self.recover(name=name,profiles=[],rows=[],page='',cache=cache)
        self.assertEqual(warm.state,'confirmed')
        self.assertEqual(calls,[])
        self.assertTrue(warm.cache_hit)
        competitor=dict(receipt['catalogue_profiles'][0],imdb_id='999',tmdb_id='')
        mutations=[None, {'original_title':'2001'}, {'input_year':'1985'},
            {'query':'one thousand nine hundred eighty four'},
            {'catalogue_profiles':[*receipt['catalogue_profiles'],competitor]},
            {'catalogue_metadata':{'total_pages':2}}, {'catalogue_metadata':{'total_count':2}}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed=tuple((dict(row,**mutation) if mutation is not None else None)
                    if row.get('acquisition')==receipt['acquisition'] else row for row in proof.evidence)
                altered=replace(proof,evidence=tuple(row for row in changed if row is not None))
                self.assertFalse(work.validate_filename_resolution(name,base,altered))
                payload=json.loads(original)
                next(iter(payload['entries'].values()))['resolution']['evidence']=list(altered.evidence)
                cache.write_text(json.dumps(payload),encoding='utf8')
                fresh,calls,_session,_base=self.recover(name=name,profiles=[],rows=[],cache=cache)
                self.assertEqual(fresh.state,'pending')
                self.assertEqual(len(calls),1)

    def test_numeric_catalogue_page_metadata_and_observed_conflicts_prevent_confirmation(self):
        good=movie('Nineteen Eighty-Four','1984','87804','9314')
        for metadata in ({'next_page':2},{'has_more':True},{'truncated':True},{'total_pages':2},
                         {'total_count':2},{'total_results':2},{'count':0},{'count':True}):
            with self.subTest(metadata=metadata):
                proof,calls,_session,_base=self.recover(name='1984 1984.mkv',profiles=[],rows=[good],
                    page=official_titles(),metadata=metadata)
                self.assertEqual(proof.state,'pending')
                self.assertEqual(len(calls),1)
        for records in ([profile('1984','1984','999','')],
                        [profile('1984','1984','','999')],
                        [profile('1984','1984','87804','9314',kind='tv')]):
            with self.subTest(records=records):
                proof,_calls,_session,_base=self.recover(name='1984 1984.mkv',profiles=records,
                    rows=[good],page=official_titles())
                self.assertEqual(proof.state,'pending')

    def test_numeric_official_alias_proof_shares_deadline_and_preserves_cancellation(self):
        now=[100.]
        session=work.RecoverySession(cache_path=False)
        session.budget=work.RequestBudget(started=100.,time_budget_seconds=15.)
        def secondary(*_args):
            now[0]+=8.
            return {'results':[movie('Nineteen Eighty-Four','1984','87804','9314')]}
        def public(*_args):
            now[0]+=8.
            return official_titles()
        with patch.object(work.time,'monotonic',side_effect=lambda:now[0]), \
                patch.object(work,'fetch_secondary_catalogue',side_effect=secondary), \
                patch.object(work,'fetch_public_movie',side_effect=public):
            result=work.recover_filename_year('1984 1984.mkv',online.MediaIdentity('1984 1984'),session)
        self.assertEqual(result.state,'pending')
        self.assertEqual(session.budget.request_count,2)
        event=threading.Event()
        session=work.RecoverySession(cache_path=False,cancel_event=event)
        def cancelled(*_args):
            event.set()
            return official_titles()
        with patch.object(work,'fetch_secondary_catalogue',return_value={
                'results':[movie('Nineteen Eighty-Four','1984','87804','9314')]}), \
                patch.object(work,'fetch_public_movie',side_effect=cancelled),self.assertRaises(core.CancelledError):
            work.recover_filename_year('1984 1984.mkv',online.MediaIdentity('1984 1984'),session)

    def test_smart_numeric_alias_permission_requires_real_bound_proof_beyond_auto_state(self):
        proof,_calls,_session,_base=self.recover(name='1984 1984.mkv',profiles=[],
            rows=[movie('Nineteen Eighty-Four','1984','87804','9314')],page=official_titles())
        self.assertEqual(proof.state,'confirmed')
        identity=online.MediaIdentity(proof.title,original_title=proof.title,year=proof.year,
            imdb_id=proof.imdb_id,tmdb_id=proof.tmdb_id,raw_title='1984 1984')
        item=online.SubtitleCandidate(1,'1984.srt','1984.1984.BluRay','en',1,1.,False,False,
            feature_title='1984',feature_year='1984',feature_imdb_id='87804',feature_tmdb_id='9314',
            identity_admission_state='auto',identity_admission_evidence=proof.evidence)
        self.assertEqual(smart._candidate_identity_conflict(item,identity),'')
        evidence_variants=[(),tuple(row for row in proof.evidence
            if row.get('acquisition')!='numeric-title-alias-proposal'),
            tuple(dict(row,source='https://other.invalid/movie/9314/titles')
                if row.get('acquisition')=='https-public-tmdb-movie-titles' else row for row in proof.evidence),
            tuple(dict(row,aliases=[]) if row.get('acquisition')=='https-public-tmdb-movie-titles'
                else row for row in proof.evidence)]
        for evidence in evidence_variants:
            with self.subTest(evidence_count=len(evidence)):
                self.assertTrue(smart._candidate_identity_conflict(
                    replace(item,identity_admission_evidence=evidence),identity))
        for altered in (replace(item,feature_imdb_id='999'),replace(item,feature_tmdb_id='999'),
                        replace(item,feature_year='1986'),replace(item,feature_title='Other Film',
                            release='Other.Film.1984.BluRay',file_name='Other.Film.srt')):
            with self.subTest(title=altered.feature_title,year=altered.feature_year,
                              imdb=altered.feature_imdb_id,tmdb=altered.feature_tmdb_id):
                self.assertTrue(smart._candidate_identity_conflict(altered,identity))


if __name__ == '__main__':
    unittest.main()
