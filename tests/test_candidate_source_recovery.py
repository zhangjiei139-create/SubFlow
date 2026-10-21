"""Offline candidate-scoped recovery and its existing production controller."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import online_subtitles as online
import pro_core as pro
import smart_subtitles as smart
import subdl_subtitles as subdl
import subtitle_source_recovery as recovery
import subtitle_tool_core as core
import work_identity_resolver as work


class CandidateSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.movie = self.root / 'MidnightRun.1988.mkv'
        self.movie.write_bytes(b'private controller fixture')
        self.identity = online.MediaIdentity('MidnightRun', 'MidnightRun', '1988')
        self.parent = dict(sd_id='sd123', name='Midnight Run', original_name='Midnight Run',
                           year=1988, type='movie', imdb_id='', tmdb_id='')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, LOCALAPPDATA=str(self.root/'local'),
            APPDATA=str(self.root/'roaming'), SUBFLOW_RUNTIME_DIR=str(self.root/'runtime')))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(smart, 'filename_container_identity_conflict', return_value=''))

    def candidate(self):
        return SimpleNamespace(file_id='file-1', file_name='Midnight.Run.1988.srt',
            release='Midnight.Run.1988.BluRay', language='en', downloads=1, rating=1,
            trusted=False, hearing_impaired=False, download_url='https://fixture.invalid/sub.zip',
            feature_title='', feature_year='', identity_key='', identity_verified=False,
            identity_admission_state='', author='', author_role='', priority_reason='', moviehash_match=False)

    def receipt(self, candidate=None):
        return recovery._receipt(str(self.movie), self.identity, 'SubDL', candidate or self.candidate(),
            'camel', 'Midnight Run', {'catalogue_sha256':'a'*64,'list_sha256':'b'*64,
            'list_echo_ids':[{'sd_id':123,'imdb_id':'','tmdb_id':''}]}, self.parent)

    def listing(self, parent=None):
        return dict(results=[dict(parent or self.parent, sd_id=123)], subtitles=[])

    def srt(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('1\n00:00:01,000 --> 00:00:02,000\nThis is the actual dialogue for the film.\n', encoding='utf8')
        return path

    def test_camel_is_mechanical_and_preserves_numbers(self):
        for raw, expected in (('MidnightRun','Midnight Run'),('Nineteen EightyFour','Nineteen Eighty Four'),
                              ('1917',None),('Se7en',None),('Already Separate',None)):
            with self.subTest(raw=raw):self.assertEqual(recovery.camel_boundary_proposal(raw),expected)

    def test_multiple_parent_entries_are_not_work_competition(self):
        other=dict(self.parent,sd_id='sd124',imdb_id='123',tmdb_id='456')
        self.assertEqual(recovery.matching_parents({'results':[self.parent,other]},'Midnight Run','1988'),[self.parent,other])
        self.assertEqual(self.parent['imdb_id'],'')

    def test_wrong_year_type_and_same_parent_conflict_are_rejected(self):
        for extra in (dict(self.parent,year=1989),dict(self.parent,type='tv'),
                      dict(self.parent,name='Other Film',original_name='Other Film')):
            with self.subTest(extra=extra):
                self.assertEqual(recovery.matching_parents({'results':[self.parent,extra]},'Midnight Run','1988'),[])

    def test_truncated_catalogue_does_not_claim_global_uniqueness(self):
        self.assertEqual(recovery.matching_parents({'results':[self.parent],'has_more':True},'Midnight Run','1988'),[self.parent])

    def test_sd_prefix_numeric_echo_is_equivalent_without_rewriting(self):
        listing=self.listing();before=copy.deepcopy(listing)
        self.assertEqual(recovery.list_conflict(listing,self.parent,'Midnight Run','1988'),'')
        self.assertEqual(listing,before);self.assertEqual(self.parent['sd_id'],'sd123')
        listing['results'][0]['sd_id']=999
        self.assertTrue(recovery.list_conflict(listing,self.parent,'Midnight Run','1988'))

    def test_missing_echo_and_explicit_raw_conflict_reject(self):
        self.assertTrue(recovery.list_conflict({'subtitles':[]},self.parent,'Midnight Run','1988'))
        for row in ({'feature_year':'1989'},{'unpack_files':[{'feature_title':'Other Film'}]}):
            listing=self.listing();listing['subtitles']=[row]
            with self.subTest(row=row):self.assertTrue(recovery.list_conflict(listing,self.parent,'Midnight Run','1988'))

    def test_current_list_echo_ID_conflict_rejects_only_its_parent(self):
        identity=online.MediaIdentity('MidnightRun','MidnightRun','1988',imdb_id='123')
        first=dict(self.parent);second=dict(self.parent,sd_id='sd124')
        bad=self.listing();bad['results'][0]['imdb_id']='999'
        good=dict(results=[dict(second,imdb_id='tt0000123')],subtitles=[])
        candidate=self.candidate();before=copy.deepcopy(vars(candidate));session=work.RecoverySession(cache_path=False)
        with patch.object(work,'fetch_secondary_catalogue',return_value={'results':[first,second]}), \
             patch.object(subdl,'_request_v2',side_effect=[bad,good]), \
             patch.object(subdl,'_candidates_from_response',return_value=[candidate]) as parse:
            rows,receipts=recovery.recover_camel(str(self.movie),identity,'fixture',session,lambda _:None)
        self.assertEqual(rows,[candidate]);self.assertEqual(parse.call_count,1)
        self.assertEqual(receipts[id(candidate)]['parent']['sd_id'],'sd124')
        self.assertEqual(receipts[id(candidate)]['evidence']['list_echo_ids'][0]['imdb_id'],'tt0000123')
        self.assertEqual(vars(candidate),before);self.assertEqual(session.budget.request_count,1)

    def test_warm_qualification_rechecks_echo_ID_projection(self):
        identity=online.MediaIdentity('MidnightRun','MidnightRun','1988',imdb_id='123')
        candidate=self.candidate()
        evidence={'catalogue_sha256':'a'*64,'list_sha256':'b'*64,
            'list_echo_ids':[{'sd_id':123,'imdb_id':'tt0000123','tmdb_id':''}]}
        good=recovery._receipt(str(self.movie),identity,'SubDL',candidate,'camel','Midnight Run',evidence,self.parent)
        self.assertTrue(recovery.validate_qualification(str(self.movie),identity,'SubDL',candidate,good))
        for change in (None,[],[{'sd_id':123,'imdb_id':'999','tmdb_id':''}],
                       [{'sd_id':999,'imdb_id':'123','tmdb_id':''}]):
            bad_evidence=copy.deepcopy(evidence)
            if change is None:bad_evidence.pop('list_echo_ids')
            else:bad_evidence['list_echo_ids']=change
            bad=recovery._receipt(str(self.movie),identity,'SubDL',candidate,'camel','Midnight Run',bad_evidence,self.parent)
            with self.subTest(change=change):
                self.assertFalse(recovery.validate_qualification(str(self.movie),identity,'SubDL',candidate,bad))

    def test_qualification_keeps_global_pending_and_original_fields(self):
        candidate=self.candidate();before=copy.deepcopy(vars(candidate));receipt=self.receipt(candidate)
        self.assertTrue(recovery.validate_qualification(str(self.movie),self.identity,'SubDL',candidate,receipt))
        self.assertEqual(vars(candidate),before)
        self.assertFalse(receipt['global_identity_confirmed'])
        self.assertFalse(receipt['may_merge_ids']);self.assertFalse(receipt['may_inherit_alignment'])

    def test_receipt_input_candidate_parent_and_source_tamper_reject(self):
        candidate=self.candidate();receipt=self.receipt(candidate)
        for change in (lambda r:r['input'].update(year='1989'),lambda r:r['parent'].update(sd_id='999'),
                       lambda r:r['candidate'].update(file_id='other'),lambda r:r.update(mode='ordinary')):
            bad=copy.deepcopy(receipt);change(bad)
            with self.subTest(change=change):self.assertFalse(recovery.validate_qualification(str(self.movie),self.identity,'SubDL',candidate,bad))
        candidate.file_id='changed'
        self.assertFalse(recovery.validate_qualification(str(self.movie),self.identity,'SubDL',candidate,receipt))

    def test_known_local_ID_contradiction_and_episode_reject(self):
        candidate=self.candidate();parent=dict(self.parent,imdb_id='123')
        receipt=recovery._receipt(str(self.movie),self.identity,'SubDL',candidate,'camel','Midnight Run',
            {'catalogue_sha256':'a'*64,'list_sha256':'b'*64},parent)
        identity=online.MediaIdentity('MidnightRun','MidnightRun','1988',imdb_id='999')
        self.assertFalse(recovery.validate_qualification(str(self.movie),identity,'SubDL',candidate,receipt))
        candidate.feature_episode_number='1';receipt=self.receipt(candidate)
        self.assertFalse(recovery.validate_qualification(str(self.movie),self.identity,'SubDL',candidate,receipt))

    def test_camel_parent_series_metadata_cannot_bypass_original_episode_gate(self):
        for name in ('feature_parent_imdb_id','feature_parent_tmdb_id'):
            candidate=self.candidate();setattr(candidate,name,'999');receipt=self.receipt(candidate)
            with self.subTest(name=name):
                self.assertFalse(recovery.validate_qualification(str(self.movie),self.identity,'SubDL',candidate,receipt))
                self.assertEqual(receipt['candidate'][name],'999')

    def test_raw_ID_cannot_conflict_with_local_ID_when_parent_ID_missing(self):
        identity=online.MediaIdentity('MidnightRun','MidnightRun','1988',imdb_id='999')
        candidate=self.candidate();candidate.feature_imdb_id='123'
        receipt=recovery._receipt(str(self.movie),identity,'SubDL',candidate,'camel','Midnight Run',
            {'catalogue_sha256':'a'*64,'list_sha256':'b'*64},self.parent)
        self.assertFalse(recovery.validate_qualification(str(self.movie),identity,'SubDL',candidate,receipt))

    def test_scoped_installment_keeps_original_explicit_release_check(self):
        candidate=self.candidate();candidate.release='Star Wars Episode II 2002'
        identity=online.MediaIdentity('Star Wars Episode III','Star Wars Episode III','2005')
        self.assertIn('第 2 部',smart._candidate_installment_conflict(candidate,identity))
        self.assertEqual(smart._candidate_installment_conflict(candidate,None),'')

    def test_one_shared_budget_for_all_parents_and_no_candidate_mutation(self):
        second=dict(self.parent,sd_id='sd124',imdb_id='123',tmdb_id='456')
        rows=[self.candidate(),self.candidate()];before=[copy.deepcopy(vars(c)) for c in rows]
        session=work.RecoverySession(cache_path=False)
        with patch.object(work,'fetch_secondary_catalogue',return_value={'results':[self.parent,second]}), \
             patch.object(subdl,'_request_v2',side_effect=[self.listing(),dict(results=[second],subtitles=[])]), \
             patch.object(subdl,'_candidates_from_response',side_effect=[[rows[0]],[rows[1]]]):
            result,receipts=recovery.recover_camel(str(self.movie),self.identity,'fixture',session,lambda _:None)
        self.assertEqual(result,rows);self.assertEqual(len(receipts),2)
        self.assertEqual(session.budget.request_count,1)
        self.assertEqual([vars(c) for c in rows],before)

    def test_exhausted_budget_does_not_reset_or_make_HTTP(self):
        session=work.RecoverySession(cache_path=False,max_requests=0)
        with patch.object(work,'fetch_secondary_catalogue') as fetch:
            rows,receipts=recovery.recover_camel(str(self.movie),self.identity,'fixture',session,lambda _:None)
        self.assertEqual((rows,receipts),([],{}));fetch.assert_not_called()
        self.assertEqual(session.budget.request_count,0)

    def test_auth_quota_and_real_cancel_propagate(self):
        for error in (online.SubtitleAuthenticationError('auth'),online.SubtitleQuotaError('quota'),core.CancelledError('cancel')):
            with self.subTest(error=type(error).__name__),patch.object(work,'fetch_secondary_catalogue',side_effect=error):
                with self.assertRaises(type(error)):
                    recovery.recover_camel(str(self.movie),self.identity,'fixture',work.RecoverySession(cache_path=False),lambda _:None)

    def test_endpoint_coarse_gate_never_confirms_identity(self):
        source=self.srt(self.root/'raw.srt')
        self.assertEqual(recovery.endpoint_record(source,842)['signed_gap_seconds'],840)
        with self.assertRaises(ValueError):recovery.endpoint_record(source,842.001)
        with self.assertRaises(ValueError):recovery.endpoint_record(source,None)

    def test_runtime_applicability_requires_real_full_pending_title(self):
        identity=online.MediaIdentity('2012','2012','2009');candidate=self.candidate()
        candidate.feature_title='2009 - 2012';candidate.feature_year='2009';candidate.identity_admission_state='pending'
        before=copy.deepcopy(vars(candidate))
        self.assertTrue(recovery.runtime_applicable(identity,[candidate]))
        self.assertEqual(vars(candidate),before)
        for name in ('2012: Supernova','2008 - 2012','Other Film'):
            candidate.feature_title=name
            with self.subTest(name=name):self.assertFalse(recovery.runtime_applicable(identity,[candidate]))

    def controller(self, acceptance=None, ordinary=False):
        raw=self.candidate();candidate=self.candidate()
        raw.feature_title='Midnight Run';raw.feature_year='1988';raw.identity_admission_state='pending'
        if ordinary:
            raw.feature_title='MidnightRun';raw.identity_admission_state='auto';raw.identity_verified=True
        receipt=self.receipt(candidate)
        session_seen=[]
        def normal(*args,**kwargs):
            session_seen.append(kwargs['identity_recovery_session'])
            return self.identity,[raw],online.SearchMeta(query_mode='title-year')
        self.stack.enter_context(patch.object(smart,'PROVIDERS',
            (('OpenSubtitles',online,'opensubtitles_api_key'),('SubDL',subdl,'subdl_api_key'))))
        self.stack.enter_context(patch.object(online,'load_settings',return_value={'opensubtitles_api_key':'fixture'}))
        self.stack.enter_context(patch.object(subdl,'load_settings',return_value={'subdl_api_key':'fixture'}))
        self.stack.enter_context(patch.object(online,'search',side_effect=normal))
        self.stack.enter_context(patch.object(subdl,'search',side_effect=AssertionError('unexpected normal downstream')))
        recover=self.stack.enter_context(patch.object(recovery,'recover_camel',return_value=([candidate],{id(candidate):receipt})))
        self.stack.enter_context(patch.object(recovery,'movie_duration',return_value=10))
        self.stack.enter_context(patch.object(smart,'_work_directory',return_value=self.root/'work'))
        shared=SimpleNamespace(duration_seconds=10,validation_audio_id=1,revision=0)
        prepare=self.stack.enter_context(patch.object(pro,'prepare_shared_subtitle_content_audio',return_value=shared))
        self.stack.enter_context(patch.object(smart,'_downloaded_candidate_structure_conflict',return_value=''))
        self.stack.enter_context(patch.object(smart,'subtitle_content_conflict',return_value=''))
        def download(_key,item,directory):return self.srt(Path(directory)/'raw.srt')
        download_mock=self.stack.enter_context(patch.object(subdl,'download',side_effect=download))
        self.stack.enter_context(patch.object(online,'download',side_effect=download))
        verify=self.stack.enter_context(patch.object(pro,'preflight_online_subtitle',
            side_effect=lambda _movie,path,*a,**kw:(Path(path),'连续VAD时间轴核验通过：提议固定偏移 +0.00 秒')))
        return lambda:smart.find_verified_english(str(self.movie),1,lambda _:None,candidate_acceptance=acceptance),recover,prepare,verify,download_mock

    def test_original_controller_receipt_then_audio_retained_and_checkpoint(self):
        accept=Mock();run,recover,prepare,verify,download=self.controller(accept)
        result=run();self.assertTrue(result.identity_key.startswith(recovery.KEY_PREFIX))
        self.assertEqual((recover.call_count,prepare.call_count,verify.call_count,accept.call_count),(1,1,1,1))
        saved=json.loads((self.root/'work/audio-verified-source.json').read_text(encoding='utf8'))
        self.assertEqual(saved['state'],'completed')
        self.assertTrue(recovery.validate_checkpoint(str(self.movie),saved['checkpoint']))

    def test_after_audio_PGS_failure_stops_and_warm_checkpoint_rechecks_retained(self):
        error=pro.RetainedSubtitleVerificationError('unverified',{},None)
        accept=Mock(side_effect=error);run,recover,prepare,verify,download=self.controller(accept)
        with self.assertRaises(pro.RetainedSubtitleVerificationError):run()
        saved=json.loads((self.root/'work/audio-verified-source.json').read_text(encoding='utf8'))
        self.assertEqual(saved['state'],'retained_unverified')
        self.assertTrue(recovery.validate_checkpoint(str(self.movie),saved['checkpoint']))
        accept.side_effect=None
        result=run();self.assertTrue(Path(result.subtitle_path).is_file())
        self.assertEqual((recover.call_count,prepare.call_count,verify.call_count,download.call_count),(1,1,1,1))
        self.assertEqual(accept.call_count,2)

    def test_checkpoint_missing_receipt_or_raw_modified_cannot_reuse(self):
        run,*_=self.controller();run()
        saved=json.loads((self.root/'work/audio-verified-source.json').read_text(encoding='utf8'))['checkpoint']
        missing=copy.deepcopy(saved);missing.pop('source_qualification')
        with self.assertRaises(RuntimeError):smart._validated_audio_checkpoint(str(self.movie),1,missing)
        raw=Path(saved['source_qualification']['endpoint']['raw_path']);raw.write_text('changed',encoding='utf8')
        self.assertFalse(recovery.validate_checkpoint(str(self.movie),saved))

    def test_normal_auto_source_does_not_request_new_evidence_or_endpoint(self):
        run,recover,prepare,verify,download=self.controller(ordinary=True)
        with patch.object(recovery,'endpoint_record',side_effect=AssertionError('normal endpoint must remain unchanged')):
            run()
        recover.assert_not_called();self.assertEqual(verify.call_count,1)

    def test_rejected_hash_waits_for_original_title_year_before_new_recovery(self):
        run,recover,prepare,verify,download=self.controller(ordinary=True)
        bad=self.candidate();bad.release='Midnight.Run.1988.Director.Commentary'
        bad.moviehash_match=True;bad.identity_verified=True
        normal=self.candidate();normal.feature_title='MidnightRun';normal.feature_year='1988'
        normal.identity_admission_state='auto';normal.identity_verified=True
        with patch.object(online,'search',side_effect=[
            (self.identity,[bad],online.SearchMeta(query_mode='hash')),
            (self.identity,[normal],online.SearchMeta(query_mode='title-year'))]) as search:
            run()
        self.assertEqual(search.call_count,2)
        self.assertTrue(search.call_args_list[1].kwargs['skip_hash'])
        recover.assert_not_called();self.assertEqual(verify.call_count,1)

    def test_online_camel_miss_defers_old_identity_request_on_same_session(self):
        raw=self.candidate();raw.feature_title='Midnight Run';raw.feature_year='1988'
        session=work.RecoverySession(cache_path=False)
        with patch.object(online,'identify_media',return_value=self.identity), \
             patch.object(online,'filename_container_identity_conflict',return_value=''), \
             patch.object(online,'_resolve_feature_identity',return_value=None), \
             patch.object(online,'_search_pages',return_value=([raw],1,False)), \
             patch.object(work,'read_cached_resolution',return_value=None), \
             patch.object(online,'read_cached_resolution',return_value=None), \
             patch.object(work,'recover_known_year_alias',side_effect=AssertionError('old extra miss must be deferred')):
            identity,rows,meta=online.search('fixture',str(self.movie),'en',skip_hash=True,
                identity_recovery_session=session,defer_candidate_recovery=True)
        self.assertEqual((identity.title,identity.year),('MidnightRun','1988'))
        self.assertEqual(meta.identity_resolution_status,'pending');self.assertIsNone(session.budget)

    def test_numeric_original_controller_uses_runtime_then_original_gates(self):
        from tests.test_runtime_work_catalogue import fixture, entity_transport
        import runtime_work_catalogue as runtime
        self.identity=online.MediaIdentity('2012','2012','2009')
        self.movie=self.root/'2012.2009.mkv';self.movie.write_bytes(b'private numeric movie fixture')
        raw=self.candidate();raw.feature_title='2009 - 2012';raw.feature_year='2009'
        raw.feature_imdb_id='1190080';raw.feature_tmdb_id='14161';raw.feature_type='Movie'
        raw.identity_admission_state='pending';raw.release='2012.2009.BluRay';raw.file_name='2012.2009.srt'
        before=copy.deepcopy(vars(raw))
        self.stack.enter_context(patch.object(smart,'PROVIDERS',(('OpenSubtitles',online,'opensubtitles_api_key'),)))
        self.stack.enter_context(patch.object(online,'load_settings',return_value={'opensubtitles_api_key':'fixture'}))
        search=self.stack.enter_context(patch.object(online,'search',side_effect=[
            (self.identity,[],online.SearchMeta(query_mode='hash')),
            (self.identity,[raw],online.SearchMeta(query_mode='title-year'))]))
        self.stack.enter_context(patch.object(runtime,'fetch_catalogue',
            side_effect=entity_transport(fixture(),'2012','2009')))
        self.stack.enter_context(patch.object(recovery,'movie_duration',return_value=9470.464))
        self.stack.enter_context(patch.object(recovery,'endpoint_record',return_value={'raw_path':'test','raw_sha256':'f'*64}))
        self.stack.enter_context(patch.object(smart,'_work_directory',return_value=self.root/'work'))
        self.stack.enter_context(patch.object(smart,'_downloaded_candidate_structure_conflict',return_value=''))
        self.stack.enter_context(patch.object(smart,'subtitle_content_conflict',return_value=''))
        self.stack.enter_context(patch.object(pro,'prepare_shared_subtitle_content_audio',return_value=SimpleNamespace(validation_audio_id=1,revision=0)))
        self.stack.enter_context(patch.object(online,'download',side_effect=lambda k,c,d:self.srt(Path(d)/'raw.srt')))
        verify=self.stack.enter_context(patch.object(pro,'preflight_online_subtitle',
            side_effect=lambda _movie,path,*a,**kw:(Path(path),'连续VAD时间轴核验通过：提议固定偏移 +0.00 秒')))
        result=smart.find_verified_english(str(self.movie),1,lambda _:None)
        self.assertTrue(result.identity_key.startswith(recovery.KEY_PREFIX));self.assertEqual(verify.call_count,1)
        self.assertEqual(search.call_count,2)
        self.assertIs(search.call_args_list[0].kwargs['identity_recovery_session'],
                      search.call_args_list[1].kwargs['identity_recovery_session'])
        self.assertEqual(search.call_args_list[0].kwargs['identity_recovery_session'].budget.request_count,2)
        for name in before:self.assertEqual(getattr(raw,name),before[name]) if name!='priority_reason' else None
        self.assertEqual(raw.identity_admission_state,'pending');self.assertFalse(raw.identity_verified)


if __name__ == '__main__':unittest.main()
