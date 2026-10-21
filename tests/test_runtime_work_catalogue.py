"""Simulated SPARQL statements, never a live catalogue accuracy claim."""
import copy
import json
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

import runtime_work_catalogue as runtime
import online_subtitles as online
import subtitle_tool_core as core
from work_identity_resolver import RecoverySession

WD='http://www.wikidata.org/entity/'
RANK='http://wikiba.se/ontology#NormalRank'


def bind(value,kind='literal'):
    return {'type':kind,'value':str(value)}


def fixture(title='2012',year='2009',specs=None):
    # Synthetic statement transport with the documented long/short runtimes.
    # Q90000x are fixture IDs, not hardcoded production work identities.
    specs=specs or [('Q900001','1190080','14161',158),('Q900002','1364155','',35)]
    rows=[]
    for item,imdb,tmdb,minutes in specs:
        def row(prop,value=None,**extra):
            result={'item':bind(WD+item,'uri'),'name':bind(title),
                'namePredicate':bind('http://www.w3.org/2000/01/rdf-schema#label','uri'),
                'property':bind(WD+prop,'uri'),'statement':bind(WD+'statement/'+item+'-'+prop,'uri'),
                'rank':bind(RANK,'uri')}
            if value is not None:result['value']=bind(value,'uri' if str(value).startswith('http') else 'literal')
            result.update({k:bind(v,'uri' if str(v).startswith('http') else 'literal') for k,v in extra.items()})
            return result
        rows.extend([row('P345','tt'+imdb),row('P1476',title),
            row('P577',year+'-01-01T00:00:00Z',timeValue=year+'-01-01T00:00:00Z',precision='9'),
            row('P31',WD+'Q11424',filmType='true')])
        if tmdb:rows.append(row('P4947',tmdb))
        if minutes is not None:rows.append(row('P2047',minutes,amount='+'+str(minutes),unit=runtime.MINUTE))
    return {'head':{'vars':['item','name','namePredicate','property','statement','rank']},'results':{'bindings':rows}}


def entity_transport(response,title,year):
    entities={}
    for row in response['results']['bindings']:
        qid=row['item']['value'].rsplit('/',1)[-1]
        e=entities.setdefault(qid,{'id':qid,'labels':{'en':{'language':'en','value':title}},'aliases':{'en':[]},'claims':{}})
        prop=row['property']['value'].rsplit('/',1)[-1];raw=row.get('value',{}).get('value','')
        if prop=='P31':fact={'id':raw.rsplit('/',1)[-1]}
        elif prop=='P577':fact={'time':'+'+row.get('timeValue',{}).get('value',year+'-01-01T00:00:00Z').lstrip('+'),'precision':int(row.get('precision',{}).get('value','0'))}
        elif prop=='P2047':fact={'amount':row.get('amount',{}).get('value',''),'unit':row.get('unit',{}).get('value','')}
        elif prop=='P1476':fact={'language':'en','text':raw}
        else:fact=raw
        rank=row.get('rank',{}).get('value','').rsplit('#',1)[-1]
        rank={'NormalRank':'normal','PreferredRank':'preferred','DeprecatedRank':'deprecated'}.get(rank,'unknown')
        sid=row.get('statement',{}).get('value','').rsplit('/',1)[-1]
        claim={'id':sid,'rank':rank,'mainsnak':{'snaktype':'value','datavalue':{'value':fact}},'qualifiers':{}}
        if row.get('qualifierPredicate'):
            qp=row['qualifierPredicate']['value'].rsplit('/',1)[-1]
            claim['qualifiers'][qp]=[{'snaktype':'value','datavalue':{'value':row.get('qualifierValue',{}).get('value','')}}]
        e['claims'].setdefault(prop,[]).append(claim)
    found={'results':{'bindings':[{'item':bind(WD+q,'uri'),'publicationDate':bind(year+'-01-01T00:00:00Z')} for q in entities]}}
    if response.get('truncated'):found['truncated']=True
    return found,{'entities':entities}


class RuntimeCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.guard=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('external HTTP forbidden in unit tests'))
        self.guard.start();self.addCleanup(self.guard.stop)

    def recover(self,response=None,title='2012',year='2009',duration=9470.464,session=None):
        session=session or RecoverySession(cache_path=False);calls=[]
        found,entities=entity_transport(response if response is not None else fixture(title,year),title,year)
        replies=[found,entities]
        def fetch(url,budget):calls.append((url,budget));return replies[len(calls)-1]
        return runtime.recover_runtime_work(title,year,duration,session,fetcher=fetch),session,calls

    def alter(self,response,prop,change,item='Q900002'):
        for row in response['results']['bindings']:
            if row['item']['value']==WD+item and row['property']['value']==WD+prop:change(row)

    def test_query_discovers_by_complete_published_name_and_year_not_candidate_id(self):
        query=runtime.build_query('2012','2009')
        for predicate in ('rdfs:label','skos:altLabel','wdt:P1476'):
            self.assertIn('?item '+predicate+' "2012"@en',query)
        self.assertIn('OPTIONAL { ?item wdt:P577 ?publicationDate . }',query)
        for forbidden in ('Q184605','Q211943','1190080','1364155','validation-','expected','P31','P2047'):
            self.assertNotIn(forbidden,query)

    def test_query_uses_bounded_explicit_predicates_and_correlated_movie_check(self):
        query=runtime.build_query('Alone','2015');self.assertEqual(query.count('UNION'),2)
        self.assertIn('{ SELECT DISTINCT ?item WHERE',query)
        self.assertIn('LIMIT '+str(runtime.ITEM_LIMIT),query);self.assertIn('LIMIT '+str(runtime.ROW_LIMIT),query)
        for schema_join in ('wikibase:claim','wikibase:statementProperty','wikibase:statementValue','P279','FILTER','?statement'):
            self.assertNotIn(schema_join,query)

    def test_literal_in_each_branch_escapes_generic_titles_without_outer_values(self):
        for title in ('2012','Alone','The Call','Quoted "title" \\ path','Léon The Professional'):
            with self.subTest(title=title):
                query=runtime.build_query(title,'2015');literal=json.dumps(title,ensure_ascii=False)+'@en'
                self.assertNotIn('VALUES ?name',query)
                self.assertNotIn('?item ?namePredicate ?name',query)
                self.assertEqual(query.count(literal),3)
                for predicate in ('rdfs:label','skos:altLabel','wdt:P1476'):
                    self.assertIn('?item '+predicate+' '+literal+' .',query)

    def test_numeric_actual_duration_selects_observed_long_work_only(self):
        result,session,calls=self.recover()
        self.assertEqual(result['state'],'runtime_proposal')
        self.assertEqual(result['selected_work']['provider_ids'],{'imdb_id':'1190080','tmdb_id':'14161'})
        self.assertFalse(result['global_identity_confirmed']);self.assertFalse(result['output_authorized'])
        self.assertEqual(len(result['works']),2);self.assertEqual(len(calls),2);self.assertEqual(session.budget.request_count,2)

    def test_nonnumeric_homonyms_use_same_mechanism(self):
        response=fixture('Alone','2015',[('Q900003','100','200',92),('Q900004','101','201',133)])
        result,_,_=self.recover(response,'Alone','2015',92*60)
        self.assertEqual(result['state'],'runtime_proposal')
        self.assertEqual(result['selected_work']['provider_ids']['imdb_id'],'100')

    def test_query_echo_without_actual_published_name_is_not_evidence(self):
        response=fixture()
        for row in response['results']['bindings']:
            row['lookupTitle']=row.pop('name');row.pop('namePredicate')
        works=runtime.parse_catalogue(response,'2012','2009')
        self.assertTrue(works);self.assertTrue(all(w['issues'] for w in works))

    def test_wrong_year_imprecise_date_and_non_movie_rival_remain_pending(self):
        for label,prop,change in [
            ('year','P577',lambda r:r.update(timeValue=bind('2008-01-01T00:00:00Z'))),
            ('precision','P577',lambda r:r.update(precision=bind('8'))),
            ('type','P31',lambda r:r.pop('filmType'))]:
            with self.subTest(label=label):
                response=fixture();self.alter(response,prop,change)
                works=runtime.parse_catalogue(response,'2012','2009')
                self.assertTrue(any(w['issues'] for w in works));self.assertEqual(len(works),2)

    def test_runtime_units_qualifiers_multivalue_and_missing_rival_abstain(self):
        for label in ('unit','qualifier','multiple','missing'):
            with self.subTest(label=label):
                response=fixture()
                if label=='unit':self.alter(response,'P2047',lambda r:r.update(unit=bind(WD+'Q999999','uri')))
                elif label=='qualifier':self.alter(response,'P2047',lambda r:r.update(qualifierPredicate=bind('http://www.wikidata.org/prop/qualifier/P518','uri'),qualifierValue=bind('cut')))
                elif label=='multiple':
                    row=copy.deepcopy(next(r for r in response['results']['bindings'] if r['property']['value']==WD+'P2047'))
                    row['statement']=bind(row['statement']['value']+'-other','uri');row['amount']=bind('154');response['results']['bindings'].append(row)
                else:response['results']['bindings']=[r for r in response['results']['bindings'] if not(r['item']['value']==WD+'Q900002' and r['property']['value']==WD+'P2047')]
                result,_,_=self.recover(response);self.assertEqual(result['state'],'pending');self.assertEqual(len(result['works']),2)

    def test_close_runtime_and_middle_unknown_actual_do_not_choose_nearest(self):
        response=fixture('Alone','2020',[('Q900001','100','200',92),('Q900002','101','201',98)])
        result,_,_=self.recover(response,'Alone','2020',92*60);self.assertEqual(result['state'],'pending')
        result,_,_=self.recover(duration=95*60);self.assertEqual(result['state'],'pending')
        result,session,calls=self.recover(duration=None);self.assertEqual(result['state'],'pending');self.assertIsNone(session.budget);self.assertEqual(calls,[])

    def test_duplicate_provider_ids_do_not_establish_two_independent_works(self):
        response=fixture(specs=[('Q900001','100','200',158),('Q900002','100','200',35)])
        result,_,_=self.recover(response);self.assertEqual(result['state'],'pending')
        self.assertTrue(all('separate_records_share_provider_id' in w['issues'] for w in result['works']))

    def test_candidate_binding_uses_original_ids_and_anchored_year_prefix(self):
        result,_,_=self.recover()
        candidate=SimpleNamespace(feature_title='2009 - 2012',feature_year='2009',feature_imdb_id='1190080',feature_tmdb_id='14161',identity_admission_state='pending')
        before=vars(candidate).copy();self.assertTrue(runtime.candidate_matches_runtime_proposal(candidate,result));self.assertEqual(vars(candidate),before)
        for field,value in [('feature_imdb_id','1364155'),('feature_tmdb_id','other'),('feature_year','2008'),('feature_title','2009 - 2012: Supernova'),('feature_title','2008 - 2012')]:
            with self.subTest(field=field,value=value):
                changed=SimpleNamespace(**before);setattr(changed,field,value)
                self.assertFalse(runtime.candidate_matches_runtime_proposal(changed,result))
        candidate.feature_imdb_id='';candidate.feature_tmdb_id=''
        self.assertFalse(runtime.candidate_matches_runtime_proposal(candidate,result))

    def test_result_evidence_and_selected_record_cannot_be_replaced_by_echo(self):
        result,_,_=self.recover();candidate=SimpleNamespace(feature_title='2012',feature_year='2009',feature_imdb_id='1190080',feature_tmdb_id='14161')
        for field,value in [('works',[]),('evidence',{}),('global_identity_confirmed',True),('selected_work_key','other')]:
            with self.subTest(field=field):
                altered=copy.deepcopy(result);altered[field]=value
                self.assertFalse(runtime.candidate_matches_runtime_proposal(candidate,altered))

    def test_changed_duration_selected_work_or_query_receipt_cannot_be_reused(self):
        result,_,_=self.recover();candidate=SimpleNamespace(feature_title='2012',feature_year='2009',feature_imdb_id='1190080',feature_tmdb_id='14161')
        for label in ('duration','winner','query'):
            with self.subTest(label=label):
                altered=copy.deepcopy(result)
                if label=='duration':altered['actual_duration_seconds']+=1
                elif label=='winner':
                    altered['selected_work']=altered['works'][1];altered['selected_work_key']=altered['works'][1]['work_key']
                else:altered['evidence']['query_sha256']='0'*64
                self.assertFalse(runtime.candidate_matches_runtime_proposal(candidate,altered))

    def test_candidate_movie_type_and_episode_metadata_are_respected(self):
        result,_,_=self.recover()
        base=dict(feature_title='2012',feature_year='2009',feature_imdb_id='1190080',feature_tmdb_id='14161')
        self.assertTrue(runtime.candidate_matches_runtime_proposal(SimpleNamespace(**base,feature_type='Movie'),result))
        for field,value in [('feature_type','tvseries'),('feature_season_number','1'),('feature_episode_number','0'),('feature_parent_imdb_id','100')]:
            with self.subTest(field=field):
                candidate=SimpleNamespace(**base);setattr(candidate,field,value)
                self.assertFalse(runtime.candidate_matches_runtime_proposal(candidate,result))

    def test_far_rival_missing_ids_is_kept_but_selected_work_needs_original_id(self):
        response=fixture();response['results']['bindings']=[r for r in response['results']['bindings']
            if not(r['item']['value']==WD+'Q900002' and r['property']['value'] in (WD+'P345',WD+'P4947'))]
        result,_,_=self.recover(response)
        self.assertEqual(result['state'],'runtime_proposal');self.assertEqual(len(result['works']),2)
        self.assertTrue(result['works'][1]['provider_id_issues'])
        response['results']['bindings']=[r for r in response['results']['bindings'] if r['property']['value'] not in (WD+'P345',WD+'P4947')]
        result,_,_=self.recover(response)
        self.assertEqual(result['state'],'pending');self.assertEqual(result['reason'],'selected_work_provider_ids_missing_or_conflicting')

    def test_prior_payment_and_repeated_calls_never_reset_shared_budget(self):
        session=RecoverySession(cache_path=False);session.evidence_budget().request('already_paid',lambda:None)
        result,_,calls=self.recover(session=session)
        self.assertEqual(result['reason'],'insufficient_shared_slots_for_two_stage');self.assertEqual(calls,[])
        self.assertEqual(session.budget.request_count,1)
        fresh=RecoverySession(cache_path=False);result,_,calls=self.recover(session=fresh)
        self.assertEqual(result['state'],'runtime_proposal');self.assertEqual(len(calls),2)
        result,_,calls=self.recover(session=fresh);self.assertEqual(result['reason'],'insufficient_shared_slots_for_two_stage')
        self.assertEqual(calls,[]);self.assertEqual(fresh.budget.request_count,2)

    def test_expired_and_smaller_session_limits_block_without_http(self):
        for kwargs in ({'max_requests':0},{'time_budget_seconds':0}):
            with self.subTest(kwargs=kwargs):
                result,session,calls=self.recover(session=RecoverySession(cache_path=False,**kwargs))
                self.assertEqual(result['state'],'pending');self.assertEqual(calls,[]);self.assertEqual(session.budget.request_count,0)

    def test_cancel_and_fatal_auth_quota_propagate(self):
        event=threading.Event();event.set();session=RecoverySession(cache_path=False,cancel_event=event)
        with self.assertRaises(core.CancelledError):self.recover(session=session)
        for cls in (online.SubtitleAuthenticationError,online.SubtitleQuotaError):
            with self.subTest(cls=cls):
                def fatal(*_args):raise cls('control')
                session=RecoverySession(cache_path=False)
                with self.assertRaises(cls):runtime.recover_runtime_work('2012','2009',9470.464,session,fetcher=fatal)
                self.assertEqual(session.budget.request_count,1)

    def test_cancellation_after_request_is_not_a_success(self):
        event=threading.Event();session=RecoverySession(cache_path=False,cancel_event=event)
        def fetch(*_args):event.set();return fixture()
        with self.assertRaises(core.CancelledError):runtime.recover_runtime_work('2012','2009',9470.464,session,fetcher=fetch)
        self.assertEqual(session.budget.request_count,1)

    def test_default_fetch_uses_one_standard_public_request_on_same_budget(self):
        import work_identity_resolver as work
        calls=[];replies=entity_transport(fixture(),'2012','2009')
        def read(request,budget,**kwargs):calls.append((request,budget));return json.dumps(replies[len(calls)-1])
        session=RecoverySession(cache_path=False)
        with patch.object(work,'_read_response',side_effect=read):result=runtime.recover_runtime_work('2012','2009',9470.464,session)
        self.assertEqual(result['state'],'runtime_proposal');self.assertEqual(len(calls),2)
        self.assertTrue(calls[0][0].full_url.startswith(runtime.ENDPOINT+'?'))
        self.assertTrue(calls[1][0].full_url.startswith(runtime.ENTITY_API+'?'))
        self.assertTrue(all(item[1] is session.budget for item in calls))
        self.assertEqual(result['evidence']['response_digest_scope'],'canonical_two_source_receipts')
        self.assertTrue(all(s['response_digest_scope']=='response_utf8_body' for s in result['evidence']['sources']))

    def test_row_limit_missing_statement_and_unknown_rank_are_not_silently_dropped(self):
        for label in ('limit','statement','rank','truncated'):
            with self.subTest(label=label):
                response=fixture()
                if label=='limit':response['results']['bindings']=[response['results']['bindings'][0]]*runtime.ROW_LIMIT
                elif label=='statement':response['results']['bindings'][0].pop('statement')
                elif label=='rank':response['results']['bindings'][0]['rank']=bind('unknown','uri')
                else:response['truncated']=True
                if label in ('limit','truncated'):
                    with self.assertRaises(ValueError):runtime.parse_catalogue(response,'2012','2009')
                else:self.assertTrue(any(w['issues'] for w in runtime.parse_catalogue(response,'2012','2009')))




class TwoStageIntegrityTests(unittest.TestCase):
    setUp=RuntimeCatalogueTests.setUp
    recover=RuntimeCatalogueTests.recover

    def test_each_actual_response_and_discovery_membership_is_bound(self):
        result,_,_=self.recover()
        candidate=SimpleNamespace(feature_title='2012',feature_year='2009',feature_imdb_id='1190080',feature_tmdb_id='14161')
        self.assertTrue(runtime.candidate_matches_runtime_proposal(candidate,result))
        for which in ('first_body','second_body','second_ids','missing_source','combined','discovery','claim_ids'):
            with self.subTest(which=which):
                altered=copy.deepcopy(result)
                if which=='first_body':altered['evidence']['sources'][0]['response_sha256']='0'*64
                if which=='second_body':altered['evidence']['sources'][1]['response_sha256']='0'*64
                if which=='second_ids':altered['evidence']['sources'][1]['entity_ids']=['Q900999']
                if which=='missing_source':altered['evidence']['sources'].pop()
                if which=='combined':altered['evidence']['response_sha256']='0'*64
                if which=='discovery':altered['discovery_observations'][0]['observed_years']=[]
                if which=='claim_ids':altered['full_claim_ids']=['Q900999']
                self.assertFalse(runtime.candidate_matches_runtime_proposal(candidate,altered))

    def test_late_processing_cannot_leave_a_proposal_published(self):
        session=RecoverySession(cache_path=False);actual_parse=runtime.parse_catalogue
        def late(*args,**kwargs):
            works=actual_parse(*args,**kwargs);session.budget.started-=20;return works
        with patch.object(runtime,'parse_catalogue',side_effect=late):result,_,calls=self.recover(session=session)
        self.assertEqual(len(calls),2);self.assertEqual(result['state'],'pending')
        self.assertEqual(result['reason'],'shared_evidence_budget_exhausted')
        self.assertIsNone(result['selected_work']);self.assertNotIn('selection_binding_sha256',result)

    def test_second_http_auth_quota_and_cancellation_keep_original_types(self):
        import urllib.error
        import work_identity_resolver as work
        replies=entity_transport(fixture(),'2012','2009')
        for status,cls in ((401,online.SubtitleAuthenticationError),(403,online.SubtitleAuthenticationError),(429,online.SubtitleQuotaError)):
            with self.subTest(status=status):
                calls=[];session=RecoverySession(cache_path=False)
                def read(request,budget,**kwargs):
                    calls.append(request)
                    if len(calls)==2:raise urllib.error.HTTPError(request.full_url,status,'control',{},None)
                    return json.dumps(replies[0])
                with patch.object(work,'_read_response',side_effect=read):
                    with self.assertRaises(cls):runtime.recover_runtime_work('2012','2009',9470.464,session)
                self.assertEqual(session.budget.request_count,2)
        event=threading.Event();session=RecoverySession(cache_path=False,cancel_event=event);calls=[]
        def fetch(url,budget):
            calls.append(url)
            if len(calls)==2:event.set()
            return replies[len(calls)-1]
        with self.assertRaises(core.CancelledError):runtime.recover_runtime_work('2012','2009',9470.464,session,fetcher=fetch)
        self.assertEqual(session.budget.request_count,2)


probe=runtime

def entity(qid,title='Alone',year='2015',minutes=92,imdb='123',type_id='Q11424'):
    def claim(prop,fact):
        return {'id':qid+'$'+prop,'rank':'normal','mainsnak':{'snaktype':'value',
            'datavalue':{'value':fact}},'qualifiers':{}}
    return {'id':qid,'labels':{'en':{'language':'en','value':title}},'aliases':{'en':[]},'claims':{
        'P31':[claim('P31',{'id':type_id})],
        'P577':[claim('P577',{'time':'+'+year+'-01-01T00:00:00Z','precision':9})],
        'P2047':[claim('P2047',{'amount':'+'+str(minutes),'unit':runtime.MINUTE})],
        'P345':[claim('P345','tt'+imdb)]}}

def payload():return {'entities':{'Q900001':entity('Q900001'),
    'Q900002':entity('Q900002',minutes=133,imdb='124')}}

def _study_discovery_ids(ids):return {'results':{'bindings':[{'item':{'type':'uri','value':probe.WD+qid},'publicationDate':{'type':'literal','value':'2015-01-01T00:00:00Z'}} for qid in ids]}}

def _study_discovery_records(records):
    rows=[]
    for qid,years in records:
        for year in years or [None]:
            row={'item':{'type':'uri','value':probe.WD+qid}}
            if year:row['publicationDate']={'type':'literal','value':year+'-01-01T00:00:00Z'}
            rows.append(row)
    return {'results':{'bindings':rows}}

class TwoPhaseControls(unittest.TestCase):
    def setUp(self):
        self.guard=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('external HTTP forbidden'))
        self.guard.start();self.addCleanup(self.guard.stop)

    def acquire(self,body=None,session=None,ids=None):
        body=body if body is not None else payload();ids=ids or list(body['entities'])
        responses=[json.dumps(_study_discovery_ids(ids)),json.dumps(body)];calls=[]
        def fetch(url,budget):calls.append((url,budget));return responses[len(calls)-1]
        result=probe.recover_runtime_work('Alone','2015',92*60,session or RecoverySession(cache_path=False),fetcher=fetch)
        return result,calls

    def test_bare_discovery_literal_only_and_no_work_ids_or_metadata_join(self):
        query=probe.build_query('Quoted "Alone" \\ title','2015')
        literal=json.dumps('Quoted "Alone" \\ title',ensure_ascii=False)+'@en'
        self.assertEqual(query.count(literal),3);self.assertIn('SELECT DISTINCT ?item',query)
        for field in ('P31','P2047','?year','2015','?statement','VALUES','Q900001'):
            self.assertNotIn(field,query)
        self.assertIn('LIMIT 31',query)

    def test_two_actual_response_views_feed_original_parser_and_selector(self):
        result,calls=self.acquire()
        self.assertEqual(result['state'],'runtime_proposal');self.assertEqual(result['selected_work_key'],'wikidata:Q900001')
        self.assertEqual(len(calls),2);self.assertIs(calls[0][1],calls[1][1])
        self.assertEqual(result['request_count'],2);self.assertEqual(len(result['evidence']['sources']),2)
        self.assertFalse(result['global_identity_confirmed']);self.assertFalse(result['output_authorized'])
        self.assertIn('ids=Q900001%7CQ900002',calls[1][0]);self.assertIn('props=claims%7Clabels%7Caliases',calls[1][0])

    def test_discovery_cap_bad_uri_and_missing_or_extra_entity_are_pending_sources(self):
        with self.assertRaises(ValueError):probe.discovery_ids(_study_discovery_ids(['Q'+str(1000+i) for i in range(31)]))
        with self.assertRaises(ValueError):probe.discovery_ids({'results':{'bindings':[{'item':{'value':'https://wrong/Q1'}}]}})
        for which in ('missing','extra','rebound','label'):
            with self.subTest(which=which):
                body=payload()
                if which=='missing':body['entities'].pop('Q900002')
                if which=='extra':body['entities']['Q900003']=entity('Q900003')
                if which=='rebound':body['entities']['Q900002']['id']='Q900003'
                if which=='label':body['entities']['Q900002']['labels']['en']['value']='Different film'
                with self.assertRaises(ValueError):probe.convert_entities(body,['Q900001','Q900002'],'Alone','2015')

    def test_other_year_and_known_nonmovie_excluded_with_explicit_observations(self):
        body=payload();body['entities']['Q900003']=entity('Q900003',year='2014')
        calendar=entity('Q900004',type_id='Q577');calendar['claims'].pop('P577')
        body['entities']['Q900004']=calendar
        result,_=self.acquire(body)
        self.assertEqual(result['state'],'runtime_proposal');self.assertEqual(len(result['works']),2)
        reasons={o['work_key']:o['disposition'] for o in result['source_observations']}
        self.assertEqual(reasons['wikidata:Q900003'],'known_other_year')
        self.assertEqual(reasons['wikidata:Q900004'],'known_non_movie_generic_type')

    def test_unknown_type_missing_runtime_or_uncertain_year_rival_not_dropped(self):
        for defect in ('unknown_type','missing_type','missing_runtime','missing_year','precision','qualifier'):
            with self.subTest(defect=defect):
                body=payload();claims=body['entities']['Q900002']['claims']
                if defect=='unknown_type':claims['P31'][0]['mainsnak']['datavalue']['value']['id']='Q900999'
                if defect=='missing_type':claims.pop('P31')
                if defect=='missing_runtime':claims.pop('P2047')
                if defect=='missing_year':claims.pop('P577')
                if defect=='precision':claims['P577'][0]['mainsnak']['datavalue']['value']['precision']=8
                if defect=='qualifier':claims['P2047'][0]['qualifiers']={'P518':[{'snaktype':'novalue'}]}
                result,_=self.acquire(body)
                self.assertEqual(result['state'],'pending');self.assertEqual(len(result['works']),2)

    def test_insufficient_shared_slots_fails_before_first_fetch(self):
        session=RecoverySession(cache_path=False);session.evidence_budget().request('prior provider',lambda:None)
        result,calls=self.acquire(session=session)
        self.assertEqual(result['state'],'pending');self.assertEqual(calls,[])
        self.assertEqual(result['request_count'],1)

    def test_discovered_names_reconfirmed_through_alias_or_active_original_title(self):
        for mode in ('alias','original','deprecated_original'):
            with self.subTest(mode=mode):
                body=payload();e=body['entities']['Q900002'];e['labels']['en']['value']='Localized name'
                if mode=='alias':e['aliases']['en']=[{'language':'en','value':'Alone'}]
                else:
                    e['claims']['P1476']=[{'id':'Q900002$P1476','rank':'deprecated' if mode.startswith('deprecated') else 'normal',
                        'mainsnak':{'snaktype':'value','datavalue':{'value':{'language':'en','text':'Alone'}}},'qualifiers':{}}]
                if mode.startswith('deprecated'):
                    with self.assertRaises(ValueError):probe.convert_entities(body,['Q900001','Q900002'],'Alone','2015')
                else:self.assertEqual(self.acquire(body)[0]['state'],'runtime_proposal')

    def test_whole_entity_competing_type_and_ids_never_hidden(self):
        body=payload();e=body['entities']['Q900002']
        other=copy.deepcopy(e['claims']['P31'][0]);other['id']='Q900002$OtherType';other['mainsnak']['datavalue']['value']['id']='Q900999'
        e['claims']['P31'].append(other)
        result,_=self.acquire(body);self.assertEqual(result['state'],'pending')
        self.assertIn('missing_or_non_movie_type',result['works'][1]['issues'])

class TwoPhaseYearSubsetControls(unittest.TestCase):
    def setUp(self):
        p=patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('external HTTP forbidden'))
        p.start();self.addCleanup(p.stop)

    def acquire(self,records=None,body=None,session=None):
        records=records if records is not None else [('Q900001',['2015']),('Q900002',['2015'])]
        responses=[json.dumps(_study_discovery_records(records)),json.dumps(body if body is not None else payload())];calls=[]
        def fetch(url,budget):calls.append((url,budget));return responses[len(calls)-1]
        result=probe.recover_runtime_work('Alone','2015',92*60,session or RecoverySession(cache_path=False),fetcher=fetch)
        return result,calls

    def test_discovery_item_cap_barrier_before_optional_direct_dates(self):
        query=probe.build_query('Alone','2015')
        self.assertIn('{ SELECT DISTINCT ?item WHERE',query)
        self.assertIn('LIMIT 31 }',query);self.assertIn('OPTIONAL { ?item wdt:P577 ?publicationDate . }',query)
        self.assertIn('LIMIT 2001',query);self.assertEqual(query.count('"Alone"@en'),3)
        for forbidden in ('P31','P2047','P279','FILTER','2015','?statement','Q900001'):
            self.assertNotIn(forbidden,query)

    def test_all_names_retained_but_only_observed_target_year_batch_requested(self):
        records=[('Q900001',['2015']),('Q900002',['2015']),('Q900003',['2014']),('Q900004',[])]
        result,calls=self.acquire(records)
        self.assertEqual(result['state'],'runtime_proposal');self.assertEqual(len(calls),2)
        self.assertEqual(result['discovered_ids'],[r[0] for r in records])
        self.assertEqual(result['full_claim_ids'],['Q900001','Q900002'])
        self.assertEqual(len(result['discovery_observations']),4)
        self.assertNotIn('Q900003',calls[1][0]);self.assertNotIn('Q900004',calls[1][0])
        self.assertTrue(all(not o['global_absence_claimed'] for o in result['discovery_observations']))
        self.assertFalse(result['global_identity_confirmed']);self.assertFalse(result['output_authorized'])

    def test_item_cap_counts_before_year_subset_and_row_cap_never_hidden(self):
        records=[('Q'+str(900000+i),['1901']) for i in range(31)]
        with self.assertRaises(ValueError):probe.discovery_plan(_study_discovery_records(records),'2015')
        repeated=_study_discovery_records([('Q900001',['2015'])]);repeated['results']['bindings']*=2001
        with self.assertRaises(ValueError):probe.discovery_plan(repeated,'2015')
        two=_study_discovery_records([('Q900001',['2014','2015']),('Q900002',['2015'])])
        selected,observations=probe.discovery_plan(two,'2015')
        self.assertEqual(selected,['Q900001','Q900002']);self.assertEqual(len(observations),2)

    def test_full_claims_reject_discovery_year_with_insufficient_precision_or_unknown_type(self):
        for field in ('precision','type','runtime','qualifier'):
            with self.subTest(field=field):
                body=payload();claims=body['entities']['Q900002']['claims']
                if field=='precision':claims['P577'][0]['mainsnak']['datavalue']['value']['precision']=8
                if field=='type':claims['P31'][0]['mainsnak']['datavalue']['value']['id']='Q900999'
                if field=='runtime':claims.pop('P2047')
                if field=='qualifier':claims['P2047'][0]['qualifiers']={'P518':[{'snaktype':'novalue'}]}
                result,calls=self.acquire(body=body)
                self.assertEqual(result['state'],'pending');self.assertEqual(len(result['works']),2)
                self.assertEqual(len(calls),2)

    def test_no_target_year_uses_one_request_and_retains_missing_year_reason(self):
        result,calls=self.acquire(records=[('Q900003',['2014']),('Q900004',[])])
        self.assertEqual(len(calls),1);self.assertEqual(result['state'],'pending')
        self.assertEqual(result['reason'],'no_observed_target_year_name_entities')
        self.assertEqual(len(result['discovery_observations']),2)

    def test_prior_paid_request_does_not_reset_or_start_discovery(self):
        session=RecoverySession(cache_path=False);session.evidence_budget().request('prior',lambda:None)
        result,calls=self.acquire(session=session)
        self.assertEqual(calls,[]);self.assertEqual(result['request_count'],1)
        self.assertEqual(result['reason'],'insufficient_shared_slots_for_two_stage')

    def test_selected_batch_entity_must_be_exact_current_name_and_discovered_id(self):
        for field in ('extra','missing','name'):
            with self.subTest(field=field):
                body=payload()
                if field=='extra':body['entities']['Q900003']=body['entities']['Q900002']
                if field=='missing':body['entities'].pop('Q900002')
                if field=='name':body['entities']['Q900002']['labels']['en']['value']='Another work'
                with self.assertRaises(ValueError):probe.convert_entities(body,['Q900001','Q900002'],'Alone','2015')

if __name__=='__main__':unittest.main()
