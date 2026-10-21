"""Known-year complete-title recovery; all transports are offline fixtures."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import copy
import os
import tempfile
import threading
import urllib.error
import unittest

import online_subtitles as online
import smart_subtitles as smart
import subdl_subtitles as subdl
import subtitle_identity_guard as guard
import subtitle_tool_core as core
import work_identity_resolver as work
from tests.test_trailing_year_proposals import row, titles_page, FULL, NAME
from tests.test_catalogue_exceptions_independent import official_titles


class KnownYearAliasEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'LOCALAPPDATA': str(self.root/'Local'),
            'APPDATA': str(self.root/'Roaming'), 'SUBFLOW_RUNTIME_DIR': str(self.root/'runtime')}))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(online, 'settings_path', return_value=self.root/'settings.json'))

    def identity(self, title=FULL, year='1984', **kwargs):
        return online.MediaIdentity(title, title, year, **kwargs)

    def recover(self, base=None, profiles=(), rows=None, page=None, session=None):
        session = session or work.RecoverySession(cache_path=False)
        with patch.object(work, 'fetch_secondary_catalogue', return_value={'results': rows if rows is not None else [row()]} ) as secondary, \
             patch.object(work, 'fetch_public_movie', return_value=page if page is not None else titles_page()) as public:
            result=work.recover_known_year_alias(base or self.identity(),session,profiles)
        return result,session,secondary,public

    def candidate(self, **kw):
        return online.SubtitleCandidate(1,'2010.1984.srt','2010.1984.BluRay','en',1,1,False,False,
            feature_title='2010',feature_year='1984',feature_imdb_id='86837',feature_tmdb_id='4437',**kw)

    def test_confirm_requires_complete_official_alias_and_retains_known_input(self):
        base=self.identity();result,session,secondary,public=self.recover(base)
        self.assertEqual((result.state,result.title,result.year),('confirmed','2010','1984'))
        self.assertTrue(work.validate_resolution(base,result))
        self.assertEqual(secondary.call_args.args[0],FULL)
        self.assertTrue(public.call_args.args[1].public_titles)
        self.assertEqual(session.budget.request_count,2)
        self.assertEqual((base.title,base.year,base.raw_title),(FULL,'1984',''))
        self.assertEqual(sum(e['acquisition']=='known-year-complete-title-alias-entry' for e in result.evidence),1)
        self.assertFalse(any(e['acquisition']=='filename-space-tail-proposal' for e in result.evidence))

    def test_known_year_filename_variants_preserve_complete_title(self):
        for name in ('2010 The Year We Make Contact (1984).mkv','2010 The Year We Make Contact（1984）.mkv',
                     '2010.The.Year.We.Make.Contact.1984.BluRay.mkv','2010_The_Year_We_Make_Contact_1984_1080p.mkv'):
            with self.subTest(name=name):
                title,year=guard.clean_video_title(name)
                self.assertEqual((title,year),(FULL,'1984'))
                result,*_=self.recover(self.identity(title,year))
                self.assertEqual(result.state,'confirmed')

    def test_short_numeric_profile_never_changes_the_query_to_title_plus_year(self):
        p=work.WorkProfile('2010','1984','86837','4437','opensubtitles-candidate')
        result,_,fetch,_=self.recover(profiles=(p,))
        self.assertEqual(result.state,'confirmed')
        self.assertEqual(fetch.call_args.args[0],FULL)

    def test_wrong_official_alias_year_and_id_remain_pending(self):
        for rows,page in (([row()],titles_page(alias='2010 Unrelated Words')),
                          ([row()],titles_page(year='1985')),([row(year='1985')],titles_page()),
                          ([row()],titles_page(canonical_id='999'))):
            with self.subTest(rows=rows,page=page):self.assertEqual(self.recover(rows=rows,page=page)[0].state,'pending')

    def test_secondary_competing_ids_and_type_remain_pending(self):
        for extra in (row(imdb='999',tmdb='998'),row(imdb='999',tmdb=''),dict(row(),type='tv')):
            with self.subTest(extra=extra):self.assertEqual(self.recover(rows=[row(),extra])[0].state,'pending')

    def test_primary_complete_title_competition_cannot_recover(self):
        profiles=(work.WorkProfile(FULL,'1984','86837','4437','opensubtitles-features'),
                  work.WorkProfile(FULL,'1984','999','998','opensubtitles-features'))
        result,_,fetch,_=self.recover(profiles=profiles)
        self.assertEqual(result.state,'pending');fetch.assert_not_called()

    def test_explicit_local_id_conflict_remains_pending(self):
        result,*_=self.recover(base=self.identity(imdb_id='999'))
        self.assertEqual(result.state,'pending')

    def test_shared_zero_request_generic_pending_does_not_block_strong_mode(self):
        session=work.RecoverySession(cache_path=False)
        weak=session.resolve(self.identity(),())
        self.assertEqual((weak.state,weak.request_count),('pending',0))
        budget=session.budget
        result,session,*_=self.recover(session=session)
        self.assertEqual(result.state,'confirmed');self.assertIs(session.budget,budget)
        self.assertEqual(session.budget.request_count,2)

    def test_actual_online_context_allows_only_zero_paid_generic_to_known_year_upgrade(self):
        base=self.identity();session=work.RecoverySession(cache_path=False)
        context={'attempted':False,'resolution':None,'recovery':session,'cancel_event':None,
                 'metadata_budget_seconds':15.,'language':'en'}
        token=online._WORK_IDENTITY_CONTEXT.set(context)
        self.addCleanup(online._WORK_IDENTITY_CONTEXT.reset,token)
        self.assertIsNone(online._recover_work_identity('fixture',base,[],online.SearchMeta()))
        self.assertTrue(context['attempted']);self.assertEqual(context['resolution'].request_count,0)
        budget=session.budget
        lookup=online._FeatureLookupDiagnostics(status='unresolved')
        with patch.object(work,'fetch_secondary_catalogue',return_value={'results':[row()]}), \
             patch.object(work,'fetch_public_movie',return_value=titles_page()), \
             patch.object(online,'_search_pages',return_value=([self.candidate()],1,False)):
            recovered=online._recover_work_identity('fixture',base,[],online.SearchMeta(),lookup)
        self.assertIsNotNone(recovered);self.assertIs(session.budget,budget)
        self.assertEqual(context['resolution'].state,'confirmed');self.assertEqual(budget.request_count,2)
        with patch.object(work,'recover_known_year_alias') as again:
            self.assertIsNone(online._recover_work_identity('fixture',base,[],online.SearchMeta(),lookup))
        again.assert_not_called()

    def test_shared_budget_is_not_reset_after_one_paid_request(self):
        session=work.RecoverySession(cache_path=False)
        session.budget=work.RequestBudget()
        session.budget.request('already paid',lambda:None)
        budget=session.budget
        result,session,_,public=self.recover(session=session)
        self.assertEqual(result.state,'pending');self.assertIs(session.budget,budget)
        self.assertEqual(budget.request_count,2);public.assert_not_called()

    def test_expired_budget_and_user_cancel_keep_their_meanings(self):
        result,session,fetch,_=self.recover(session=work.RecoverySession(cache_path=False,time_budget_seconds=0))
        self.assertEqual(result.state,'pending');fetch.assert_not_called()
        event=threading.Event();event.set()
        with self.assertRaises(core.CancelledError):self.recover(session=work.RecoverySession(cache_path=False,cancel_event=event))

    def test_authentication_and_quota_errors_are_not_no_results(self):
        for error in (online.SubtitleAuthenticationError('bad'),online.SubtitleQuotaError('quota')):
            with self.subTest(error=type(error)),patch.object(work,'fetch_secondary_catalogue',side_effect=error):
                with self.assertRaises(type(error)):work.recover_known_year_alias(self.identity(),work.RecoverySession(cache_path=False))

    def test_actual_secondary_http_auth_and_quota_statuses_are_not_pending(self):
        for status,expected in ((401,online.SubtitleAuthenticationError),
                                (403,online.SubtitleAuthenticationError),(429,online.SubtitleQuotaError)):
            with self.subTest(status=status),patch.object(subdl,'load_settings',return_value={'subdl_api_key':'fixture'}), \
                 patch.object(work,'_read_response',side_effect=urllib.error.HTTPError(
                     'https://api.subdl.com/api/v2/movies/search',status,'fixture',{},None)):
                with self.assertRaises(expected):
                    work.recover_known_year_alias(self.identity(),work.RecoverySession(cache_path=False))

    def test_warm_session_new_competing_primary_id_rejects(self):
        result,session,*_=self.recover()
        profiles=(work.WorkProfile(FULL,'1984','999','998','opensubtitles-features'),)
        repeated,_,fetch,_=self.recover(session=session,profiles=profiles)
        self.assertEqual(result.state,'confirmed');self.assertEqual(repeated.state,'pending');fetch.assert_not_called()

    def test_cache_physically_binds_full_known_input_and_revalidates_conflicts(self):
        cp=self.root/'proof.json';result,*_=self.recover(session=work.RecoverySession(cache_path=cp))
        data=json.loads(cp.read_text());entry=next(iter(data['entries'].values()))
        self.assertEqual(entry['input']['title'],FULL);self.assertEqual(entry['input']['year'],'1984')
        self.assertTrue(entry['input']['known_year_alias_entry'])
        cached,_,fetch,_=self.recover(session=work.RecoverySession(cache_path=cp))
        self.assertTrue(cached.cache_hit);fetch.assert_not_called()
        competitor=(work.WorkProfile(FULL,'1984','999','998','opensubtitles-features'),)
        self.assertIsNone(work.read_cached_resolution(self.identity(),competitor,cache_path=cp))
        for title,year in ((FULL+' Other','1984'),(FULL,'1985')):
            self.assertFalse(work.validate_resolution(self.identity(title,year),result))

    def test_online_known_year_unresolved_entry_returns_confirmed_id_candidates(self):
        base=self.identity();good=self.candidate();session=work.RecoverySession(cache_path=False)
        with patch.object(online,'identify_media',return_value=base),patch.object(online,'filename_container_identity_conflict',return_value=''), \
             patch.object(online,'_resolve_feature_identity',return_value=None), \
             patch.object(online,'_search_pages',side_effect=[([],0,False),([good],1,False)]), \
             patch.object(work,'fetch_secondary_catalogue',return_value={'results':[row()]}), \
             patch.object(work,'fetch_public_movie',return_value=titles_page()):
            identity,rows,meta=online.search('fixture','film.mkv','en',skip_hash=True,identity_recovery_session=session)
        self.assertEqual(meta.identity_resolution_status,'confirmed');self.assertEqual(len(rows),1)
        self.assertEqual(rows[0].identity_admission_state,'auto');self.assertEqual(identity.imdb_id,'86837')
        self.assertEqual(session.budget.request_count,2)

    def test_known_year_numeric_alias_permission_requires_its_own_complete_binding(self):
        result,*_=self.recover(base=self.identity('1984','1984'),
            rows=[row('Nineteen Eighty-Four','1984','87804','9314')],page=official_titles())
        self.assertEqual(result.state,'confirmed')
        identity=self.identity('Nineteen Eighty-Four','1984',imdb_id='87804',tmdb_id='9314')
        item=self.candidate();item.feature_title='1984';item.feature_imdb_id='87804';item.feature_tmdb_id='9314'
        item.identity_admission_state='auto';item.identity_admission_evidence=result.evidence
        self.assertTrue(work.confirmed_numeric_candidate_alias(item,identity))
        self.assertEqual(smart._candidate_identity_conflict(item,identity),'')
        item.identity_admission_evidence=tuple(e for e in result.evidence
            if e.get('acquisition')!='known-year-complete-title-alias-entry')
        self.assertFalse(work.confirmed_numeric_candidate_alias(item,identity))
        self.assertTrue(smart._candidate_identity_conflict(item,identity))

    def test_smart_shares_one_lazy_budget_for_known_year_across_both_sites(self):
        movie=self.root/'Contact (1997).mkv';movie.write_bytes(b'controller fixture only')
        event=threading.Event();base=self.identity('Contact','1997')
        with patch.object(smart,'PROVIDERS',(('OpenSubtitles',online,'api_key'),('SubDL',subdl,'subdl_api_key'))), \
             patch.object(online,'load_settings',return_value={'api_key':'fixture'}), \
             patch.object(subdl,'load_settings',return_value={'subdl_api_key':'fixture'}), \
             patch.object(online,'search',return_value=(base,[],online.SearchMeta())) as first, \
             patch.object(subdl,'search',return_value=(base,[],subdl.SearchMeta())) as second, \
             patch.object(smart,'filename_container_identity_conflict',return_value=''), \
             patch.object(smart.pro_core,'prepare_shared_subtitle_content_audio',return_value=None):
            with self.assertRaises(RuntimeError):smart.find_verified_english(str(movie),0,lambda _:None,cancel_event=event)
        session=first.call_args.kwargs['identity_recovery_session']
        self.assertIs(session,second.call_args.kwargs['identity_recovery_session'])
        self.assertIs(session.cancel_event,event);self.assertIsNone(session.budget)

    def test_online_hash_hit_and_already_admissible_fallback_skip_extra_lookup(self):
        base=self.identity();good=self.candidate(moviehash_match=True)
        with patch.object(online,'identify_media',return_value=base),patch.object(online,'filename_container_identity_conflict',return_value=''), \
             patch.object(online,'opensubtitles_movie_hash',return_value='abc'), \
             patch.object(online,'_search_pages',return_value=([good],1,False)),patch.object(work,'recover_known_year_alias') as recover:
            _,_,meta=online.search('fixture','film.mkv','en')
        self.assertEqual(meta.query_mode,'hash');recover.assert_not_called()
        literal=self.candidate();literal.feature_title=FULL
        with patch.object(online,'identify_media',return_value=base),patch.object(online,'filename_container_identity_conflict',return_value=''), \
             patch.object(online,'_resolve_feature_identity',return_value=None), \
             patch.object(online,'_search_pages',return_value=([literal],1,False)),patch.object(work,'recover_known_year_alias') as recover:
            _,rows,_=online.search('fixture','film.mkv','en',skip_hash=True)
        self.assertEqual(rows[0].identity_admission_state,'auto');recover.assert_not_called()

    def test_subdl_known_year_failed_lookup_can_reuse_same_bounded_proof(self):
        base=subdl.MediaIdentity(FULL,FULL,'1984');session=work.RecoverySession(cache_path=False)
        with patch.object(subdl,'identify_media',return_value=base),patch.object(subdl,'filename_container_identity_conflict',return_value=''), \
             patch.object(subdl,'_resolve_movie',return_value=None), \
             patch.object(work,'fetch_secondary_catalogue',return_value={'results':[row()]}), \
             patch.object(work,'fetch_public_movie',return_value=titles_page()), \
             patch.object(subdl,'_request_v2',side_effect=[{'results':[row()]},{'subtitles':[]} ]):
            identity,rows,meta=subdl.search('fixture','film.mkv','en',identity_recovery_session=session)
        self.assertEqual((identity.imdb_id,meta.identity_resolution_status),('86837','confirmed'))
        self.assertEqual(session.budget.request_count,2)


class KnownYearAliasClosureTests(unittest.TestCase):
    setUp=KnownYearAliasEntryTests.setUp
    identity=KnownYearAliasEntryTests.identity
    recover=KnownYearAliasEntryTests.recover
    candidate=KnownYearAliasEntryTests.candidate
    english='The Legend of 1900'
    original="La leggenda del pianista sull'oceano"

    def combined_row(self, **changes):
        result=row(self.english+' ('+self.original+')','1998','120731','10376')
        result['original_name']=self.original;result.update(changes);return result

    def combined_page(self, **changes):
        values=dict(title=self.english,year='1998',tmdb='10376',aliases=(self.english,self.original))
        values.update(changes);return official_titles(**values)

    def combined(self, rows=None, page=None, session=None):
        return self.recover(base=self.identity(self.english,'1998'),
            rows=rows if rows is not None else [self.combined_row()],
            page=page if page is not None else self.combined_page(),session=session)

    def test_combined_display_only_proposes_and_official_full_alias_proves_both_names(self):
        proof,session,_,public=self.combined()
        self.assertEqual((proof.state,proof.title,proof.year),('confirmed',self.original,'1998'))
        self.assertEqual(session.budget.request_count,2);self.assertTrue(public.call_args.args[1].public_titles)
        source=next(e for e in proof.evidence if e['acquisition']=='https-subdl-v2-unique-title-year-double-id')
        self.assertNotIn(self.english,source['aliases'])
        self.assertTrue(work.validate_resolution(self.identity(self.english,'1998'),proof))

    def test_combined_rule_is_generic_for_a_second_unrelated_title(self):
        base=self.identity('The Crossing','2001');original="L'attraversata"
        r=row('The Crossing ('+original+')','2001','777','888');r['original_name']=original
        proof,*_=self.recover(base=base,rows=[r],page=official_titles(
            title='The Crossing',year='2001',tmdb='888',aliases=('The Crossing',original)))
        self.assertEqual(proof.state,'confirmed');self.assertTrue(work.validate_resolution(base,proof))

    def test_combined_prefix_mismatched_original_and_suffix_material_are_not_proposals(self):
        for changes in ({'name':'The Legend ('+self.original+')'},
                        {'name':self.english+' ('+self.original+') More Words'},
                        {'name':self.english+' (A Different Original)'},
                        {'original_name':'A Different Original'}):
            with self.subTest(changes=changes):
                proof,_,_,public=self.combined(rows=[self.combined_row(**changes)])
                self.assertEqual(proof.state,'pending');public.assert_not_called()

    def test_combined_competition_missing_ids_wrong_year_and_tv_remain_pending(self):
        variants=([self.combined_row(imdb_id='')],[self.combined_row(tmdb_id='')],
            [self.combined_row(year=1999)],[self.combined_row(type='tv')],
            [self.combined_row(),self.combined_row(imdb_id='999',tmdb_id='998')],
            [self.combined_row(),self.combined_row(imdb_id='999',tmdb_id='')])
        for rows in variants:
            with self.subTest(rows=rows):self.assertEqual(self.combined(rows=rows)[0].state,'pending')

    def test_combined_public_missing_full_alias_wrong_year_wrong_tmdb_remain_pending(self):
        for page in (self.combined_page(aliases=(self.original,),title='An Unrelated Public Name'),
                     self.combined_page(aliases=(self.english,)),self.combined_page(year='1999'),
                     self.combined_page(tmdb='999')):
            with self.subTest(page=page):self.assertEqual(self.combined(page=page)[0].state,'pending')

    def test_combined_incomplete_response_cannot_use_official_second_request(self):
        for metadata in ({'has_more':True},{'total_pages':2},{'total_count':2}):
            with self.subTest(metadata=metadata),patch.object(work,'fetch_secondary_catalogue',
                    return_value={'results':[self.combined_row()],**metadata}),patch.object(work,'fetch_public_movie') as public:
                proof=work.recover_known_year_alias(self.identity(self.english,'1998'),work.RecoverySession(cache_path=False))
                self.assertEqual(proof.state,'pending');public.assert_not_called()

    def test_combined_cache_receipt_missing_or_mutated_cannot_rebind(self):
        proof,*_=self.combined();base=self.identity(self.english,'1998')
        for mutation in (None,{'input_title':'Other'},{'input_year':'1999'},
                         {'display_name':'Other ('+self.original+')'},
                         {'catalogue_metadata':{'has_more':True}}):
            entries=[]
            for e in proof.evidence:
                if e['acquisition']=='subdl-structured-combined-title-proposal':
                    if mutation is not None:entries.append(dict(e,**mutation))
                else:entries.append(e)
            with self.subTest(mutation=mutation):self.assertFalse(work.validate_resolution(base,replace(proof,evidence=tuple(entries))))
        entries=[]
        for e in proof.evidence:
            if e['acquisition']=='https-public-tmdb-movie-titles':continue
            if e['acquisition']=='https-subdl-v2-unique-title-year-double-id':
                e=dict(e,aliases=[*e['aliases'],self.english])
            entries.append(e)
        self.assertFalse(work.validate_resolution(base,replace(proof,evidence=tuple(entries))))

    def test_combined_subdl_exact_echo_is_revalidated_without_new_identity_request(self):
        proof,*_=self.combined();base=subdl.MediaIdentity(self.english,self.english,'1998')
        echo=self.combined_row();echo.pop('original_name')
        with patch.object(subdl,'identify_media',return_value=base),patch.object(subdl,'filename_container_identity_conflict',return_value=''), \
             patch.object(subdl,'_request_v2',side_effect=[{'results':[self.combined_row()]},{'results':[echo],'subtitles':[]} ]):
            _,_,meta=subdl.search('fixture','film.mkv','en',confirmed_work_identity=proof)
        self.assertEqual(meta.identity_resolution_status,'confirmed')
        self.assertFalse(work.confirmed_combined_title_alias(proof,echo['name']+' suffix','1998','120731','10376'))
        self.assertFalse(work.confirmed_combined_title_alias(proof,echo['name'],'1998','120731','999'))

    def clover(self):return self.identity('10 Cloverfield Lane','2016')
    def clover_row(self):return row('10 Cloverfield Lane','2016','1179933','333371')
    def wrong_fresh(self):
        item=self.candidate();item.feature_title='2016 - Untitled Cloverfield Sequel';item.feature_year='2016'
        item.feature_imdb_id='1179933';item.feature_tmdb_id='145222';return item

    def test_actual_online_fresh_conflict_revokes_meta_context_session_cache_and_rows(self):
        base=self.clover();cp=self.root/'clover-cache.json';session=work.RecoverySession(cache_path=cp)
        with patch.object(online,'identify_media',return_value=base),patch.object(online,'filename_container_identity_conflict',return_value=''), \
             patch.object(online,'_resolve_feature_identity',return_value=None), \
             patch.object(online,'_search_pages',side_effect=[([],0,False),([self.wrong_fresh()],1,False)]), \
             patch.object(work,'fetch_secondary_catalogue',return_value={'results':[self.clover_row()]}):
            _,items,meta=online.search('fixture','10 Cloverfield Lane (2016).mkv','en',skip_hash=True,identity_recovery_session=session)
        self.assertEqual(meta.identity_resolution_status,'pending');self.assertEqual(items[0].identity_admission_state,'filtered')
        self.assertIsNone(session.confirmed_resolution);self.assertEqual(json.loads(cp.read_text())['entries'],{})
        self.assertEqual(session.budget.request_count,1)

    def test_revocation_removes_all_same_input_keys_and_preserves_unrelated_cache(self):
        cp=self.root/'proof.json';session=work.RecoverySession(cache_path=cp)
        proof,*_=self.recover(base=self.clover(),rows=[self.clover_row()],session=session)
        data=json.loads(cp.read_text());entry=next(iter(data['entries'].values()))
        data['entries']['different-profile-key']=copy.deepcopy(entry)
        cp.write_text(json.dumps(data),encoding='utf-8')
        self.recover(base=self.identity('Other Film','2001'),rows=[row('Other Film','2001','777','888')],
                     session=work.RecoverySession(cache_path=cp))
        budget=session.budget;pending=session.revoke_resolution(self.clover(),proof,'fresh conflict')
        self.assertEqual(pending.state,'pending');self.assertIs(session.budget,budget)
        data=json.loads(cp.read_text());self.assertEqual(len(data['entries']),1)
        self.assertEqual(next(iter(data['entries'].values()))['input']['title'],'Other Film')
        self.assertIsNone(work.read_cached_resolution(self.clover(),cache_path=cp))
        self.assertIsNotNone(work.read_cached_resolution(self.identity('Other Film','2001'),cache_path=cp))

    def test_revoked_session_blocks_normal_subdl_success_and_explicit_stale_proof(self):
        session=work.RecoverySession(cache_path=False)
        proof,*_=self.recover(base=self.clover(),rows=[self.clover_row()],session=session)
        session.revoke_resolution(self.clover(),proof,'fresh conflict')
        for stale in (None,proof):
            with self.subTest(stale=bool(stale)),patch.object(subdl,'identify_media',return_value=subdl.MediaIdentity('10 Cloverfield Lane','10 Cloverfield Lane','2016')), \
                 patch.object(subdl,'filename_container_identity_conflict',return_value=''), \
                 patch.object(subdl,'_resolve_movie',return_value=self.clover_row()) as ordinary, \
                 patch.object(subdl,'_request_v2') as request:
                _,items,meta=subdl.search('fixture','10 Cloverfield Lane (2016).mkv','en',
                    identity_recovery_session=session,confirmed_work_identity=stale)
            self.assertEqual(items,[]);self.assertEqual(meta.identity_resolution_status,'pending')
            ordinary.assert_not_called();request.assert_not_called()

    def test_unknown_tail_year_uses_raw_binding_when_revoked_and_blocks_subdl(self):
        cp=self.root/'tail-cache.json';session=work.RecoverySession(cache_path=cp)
        base=online.MediaIdentity(Path(NAME).stem,Path(NAME).stem)
        with patch.object(work,'fetch_secondary_catalogue',return_value={'results':[row()]}),patch.object(work,'fetch_public_movie',return_value=titles_page()):
            proof=work.recover_filename_year(NAME,base,session)
        resolved=replace(base,title=proof.title,original_title=proof.title,year=proof.year,raw_title=base.title)
        item=self.candidate();item.feature_tmdb_id='999'
        context={'attempted':True,'resolution':proof,'recovery':session,'language':'en'}
        token=online._WORK_IDENTITY_CONTEXT.set(context)
        try:
            with patch.object(online,'_search_pages',return_value=([item],1,False)):
                _,_,meta=online._search_confirmed_work_identity('fixture',resolved,[],online.SearchMeta(),proof)
        finally:online._WORK_IDENTITY_CONTEXT.reset(token)
        self.assertEqual(meta.identity_resolution_status,'pending');self.assertIsNone(session.confirmed_resolution)
        self.assertIsNotNone(session.revoked_resolution(base));self.assertEqual(json.loads(cp.read_text())['entries'],{})
        with patch.object(subdl,'identify_media',return_value=subdl.MediaIdentity(base.title,base.title)), \
             patch.object(subdl,'filename_container_identity_conflict',return_value=''),patch.object(subdl,'_resolve_movie') as lookup:
            _,rows,meta=subdl.search('fixture',NAME,'en',identity_recovery_session=session,confirmed_work_identity=proof)
        self.assertEqual(rows,[]);self.assertEqual(meta.identity_resolution_status,'pending');lookup.assert_not_called()

    def test_warm_new_primary_conflict_revokes_getter_and_cache_without_spending(self):
        cp=self.root/'warm-cache.json';session=work.RecoverySession(cache_path=cp)
        proof,*_=self.recover(session=session);paid=session.budget.request_count
        p=work.WorkProfile(FULL,'1984','999','998','opensubtitles-features')
        repeated,*_=self.recover(session=session,profiles=(p,))
        self.assertEqual(repeated.state,'pending');self.assertIsNone(session.confirmed_resolution)
        self.assertEqual(session.budget.request_count,paid)
        self.assertIsNone(work.read_cached_resolution(self.identity(),cache_path=cp))

    def test_new_cold_cache_conflict_invalidates_the_old_reusable_input(self):
        cp=self.root/'cold-cache.json';self.recover(session=work.RecoverySession(cache_path=cp))
        p=work.WorkProfile(FULL,'1984','999','998','opensubtitles-features')
        self.assertIsNone(work.read_cached_resolution(self.identity(),(p,),cache_path=cp))
        self.assertIsNone(work.read_cached_resolution(self.identity(),cache_path=cp))

    def test_empty_fresh_response_does_not_revoke_a_valid_alias(self):
        session=work.RecoverySession(cache_path=False);proof,*_=self.recover(session=session)
        context={'attempted':True,'resolution':proof,'recovery':session,'language':'en'}
        token=online._WORK_IDENTITY_CONTEXT.set(context)
        try:
            with patch.object(online,'_search_pages',return_value=([],0,False)):
                _,_,meta=online._search_confirmed_work_identity('fixture',self.identity(),[],online.SearchMeta(),proof)
        finally:online._WORK_IDENTITY_CONTEXT.reset(token)
        self.assertEqual(meta.identity_resolution_status,'confirmed');self.assertIs(session.confirmed_resolution,proof)


if __name__=='__main__':unittest.main()
