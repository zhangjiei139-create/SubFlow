"""Bounded independent runtime proposals for an observed title/year work set.

Two shared evidence-budget GETs obtain published statements. A runtime proposal
only makes an unchanged specific subtitle candidate eligible for the existing
checks; it never confirms global identity, merges IDs, or supplies an offset.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date
import hashlib
import json
import math
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT='https://query.wikidata.org/sparql'
ITEM_LIMIT=31
ROW_LIMIT=2001
POLICY_VERSION='observed-runtime-match2pct-60to180-gray10pct-min600-v1'
FROZEN_POLICY_SHA256='12a5142927a050533e55dd41e118e10d5f88db3baa91bb30a99a2f264a414e59'
NAME_PREDICATES=frozenset(('http://www.w3.org/2000/01/rdf-schema#label',
    'http://www.w3.org/2004/02/skos/core#altLabel','http://www.wikidata.org/prop/direct/P1476'))
PROPERTIES=frozenset(('P345','P4947','P2047','P577','P1476','P31','P57'))
MINUTE='http://www.wikidata.org/entity/Q7727'

ENTITY_API='https://www.wikidata.org/w/api.php'
WD='http://www.wikidata.org/entity/'
PUBLISHED_NAME_PREDICATES={'label':'http://www.w3.org/2000/01/rdf-schema#label',
    'alias':'http://www.w3.org/2004/02/skos/core#altLabel','original':'http://www.wikidata.org/prop/direct/P1476'}
FILM_TYPES=frozenset(('Q11424','Q24862'))
NON_FILM_TYPES=frozenset(('Q577','Q4167410','Q482994','Q7366','Q571','Q8261','Q7889','Q5398426'))
RANKS={'normal':'NormalRank','preferred':'PreferredRank','deprecated':'DeprecatedRank'}
ACQUISITION='https-public-wikidata-name-dates-complete-entities'



def _words(value):
    return tuple(re.findall(r'[^\W_]+',unicodedata.normalize('NFKC',str(value or '')).casefold()))


def _id(value,imdb=False):
    value=str(value or '').strip()
    if imdb:value=re.sub('^tt','',value,flags=re.I)
    return value.lstrip('0') if re.fullmatch(r'\d+',value) else ''


def _positive(value):
    if not isinstance(value,(float,int)) or isinstance(value,bool):return False
    try:return math.isfinite(value) and value>0
    except (ValueError,TypeError,OverflowError):return False


def build_query(title,year):
    title=str(title or '').strip();year=str(year or '')
    if (not title or len(title)>200 or any(ord(c)<32 for c in title)
            or not re.fullmatch(r'(?:19|20)\d{2}',year) or int(year)>date.today().year+1):
        raise ValueError('complete known title/year required')
    literal=json.dumps(title,ensure_ascii=False)+'@en'
    branches=' UNION '.join('{ ?item '+p+' '+literal+' . }' for p in ('rdfs:label','skos:altLabel','wdt:P1476'))
    return ('PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n'
        'PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n'
        'PREFIX wdt: <http://www.wikidata.org/prop/direct/>\n'
        'SELECT DISTINCT ?item ?publicationDate WHERE {\n'
        ' { SELECT DISTINCT ?item WHERE { '+branches+' } LIMIT 31 }\n'
        ' OPTIONAL { ?item wdt:P577 ?publicationDate . }\n'
        '} LIMIT 2001')


def _binding(row,name):
    value=row.get(name)
    return str(value.get('value','')) if isinstance(value,dict) else ''



def discovery_ids(payload):
    rows=payload.get('results',{}).get('bindings') if isinstance(payload,dict) else None
    if isinstance(payload,dict) and any(payload.get(k) for k in ('truncated','has_more','next','next_page')):
        raise ValueError('discovery_reports_truncation')
    if not isinstance(rows,list) or len(rows)>=2001:raise ValueError('discovery_missing_or_at_limit')
    ids=[]
    for row in rows:
        value=row.get('item',{}).get('value','') if isinstance(row,dict) else ''
        if not re.fullmatch(r'https?://www\.wikidata\.org/entity/Q[1-9]\d*',value):
            raise ValueError('discovery_entity_invalid')
        qid=value.rsplit('/',1)[-1]
        if qid not in ids:ids.append(qid)
    if len(ids)>=31:raise ValueError('discovery_name_item_limit_reached')
    return sorted(ids)

def discovery_plan(payload,year):
    ids=discovery_ids(payload);rows=payload['results']['bindings'];observations=[];selected=[]
    for qid in ids:
        dates=sorted({row.get('publicationDate',{}).get('value','') for row in rows
            if row['item']['value'].rsplit('/',1)[-1]==qid and row.get('publicationDate',{}).get('value')})
        observed_years=sorted({value.lstrip('+')[:4] for value in dates if re.fullmatch(r'\+?\d{4}-.*',value)})
        matched=str(year) in observed_years
        observations.append({'work_key':'wikidata:'+qid,'direct_publication_dates':dates,'observed_years':observed_years,
            'disposition':'target_year_observed_fetch_full_claims' if matched else
                'no_publication_year_observed_not_in_target_year_scope' if not observed_years else
                'other_observed_year_not_in_target_year_scope',
            'claim_precision_verified':False,'global_absence_claimed':False})
        if matched:selected.append(qid)
    return selected,observations

def entity_api_url(ids):
    if not ids or len(ids)>30 or len(set(ids))!=len(ids) or any(not re.fullmatch(r'Q[1-9]\d*',q) for q in ids):
        raise ValueError('entity_batch_invalid')
    return ENTITY_API+'?'+urllib.parse.urlencode(sorted({'action':'wbgetentities','format':'json',
        'ids':'|'.join(ids),'props':'claims|labels|aliases','languages':'en'}.items()))

def value(snak):
    return snak.get('datavalue',{}).get('value') if isinstance(snak,dict) and snak.get('snaktype')=='value' else None

def active_claims(claims,prop):
    return [c for c in claims.get(prop,[]) if isinstance(c,dict) and c.get('rank')!='deprecated']

def public_name(entity,title):
    label=entity.get('labels',{}).get('en',{}).get('value','')
    if label and _words(label)==_words(title):return label,PUBLISHED_NAME_PREDICATES['label']
    for entry in entity.get('aliases',{}).get('en',[]):
        name=entry.get('value','') if isinstance(entry,dict) else ''
        if name and _words(name)==_words(title):return name,PUBLISHED_NAME_PREDICATES['alias']
    for claim in active_claims(entity.get('claims',{}),'P1476'):
        name=value(claim.get('mainsnak',{}))
        if isinstance(name,dict) and name.get('language')=='en' and _words(name.get('text',''))==_words(title):
            return name['text'],PUBLISHED_NAME_PREDICATES['original']
    return None

def convert_entities(payload,ids,title,year):
    """Bind every discovered entity, retain possible target-year deficiencies."""
    if not isinstance(payload,dict) or payload.get('error') or payload.get('redirects') or payload.get('normalized'):
        raise ValueError('entity_api_error_redirect_or_normalization')
    entities=payload.get('entities')
    if not isinstance(entities,dict) or set(entities)!=set(ids):raise ValueError('entity_batch_not_exact')
    rows=[];observations=[];unresolved=[]
    for qid in ids:
        entity=entities[qid]
        if not isinstance(entity,dict) or entity.get('id')!=qid or 'missing' in entity:
            raise ValueError('discovered_entity_missing_or_rebound')
        name=public_name(entity,title)
        if not name:raise ValueError('discovered_name_not_reconfirmed_in_actual_entity')
        claims=entity.get('claims')
        if not isinstance(claims,dict):raise ValueError('entity_claims_missing')
        types=set()
        for claim in active_claims(claims,'P31'):
            fact=value(claim.get('mainsnak',{}))
            if isinstance(fact,dict) and fact.get('id'):types.add(fact['id'])
        years=set();date_unknown=False
        for claim in active_claims(claims,'P577'):
            fact=value(claim.get('mainsnak',{}))
            if (not isinstance(fact,dict) or not isinstance(fact.get('precision'),int) or fact['precision']<9
                    or not re.fullmatch(r'\+?\d{4}-.*',str(fact.get('time','')))):
                date_unknown=True;continue
            years.add(fact['time'].lstrip('+')[:4])
        observation={'work_key':'wikidata:'+qid,'published_name':name[0],'name_predicate':name[1],
            'active_type_ids':sorted(types),'precise_publication_years':sorted(years),'date_missing_or_imprecise':date_unknown or not years}
        if years and str(year) not in years and not date_unknown:
            observation['disposition']='known_other_year';observations.append(observation);continue
        if types and types<=NON_FILM_TYPES:
            observation['disposition']='known_non_movie_generic_type';observations.append(observation);continue
        if not years:
            observation['disposition']='publication_year_unknown';unresolved.append('wikidata:'+qid)
        elif not types or not types<=FILM_TYPES:
            observation['disposition']='possible_target_year_type_unknown_or_conflicting';unresolved.append('wikidata:'+qid)
        else:observation['disposition']='target_year_movie_for_full_parser'
        observations.append(observation)
        for prop in PROPERTIES:
            for claim in claims.get(prop,[]):
                if not isinstance(claim,dict):raise ValueError('malformed_full_claim')
                sid=str(claim.get('id',''))
                # Preserve missing statement/rank as parser-diagnosable defects.
                row={'item':{'type':'uri','value':WD+qid},'name':{'type':'literal','value':name[0]},
                    'namePredicate':{'type':'uri','value':name[1]},'property':{'type':'uri','value':WD+prop},
                    'statement':{'type':'uri','value':WD+'statement/'+sid.replace('$','-') if sid else ''},
                    'rank':{'type':'uri','value':'http://wikiba.se/ontology#'+RANKS.get(claim.get('rank'),'UnknownRank')}}
                fact=value(claim.get('mainsnak',{}))
                if fact is not None:
                    if isinstance(fact,dict):
                        if 'id' in fact:row['value']={'type':'uri','value':WD+fact['id']}
                        elif 'text' in fact:row['value']={'type':'literal','value':str(fact['text'])}
                        else:row['value']={'type':'literal','value':json.dumps(fact,sort_keys=True,separators=(',',':'))}
                        if prop=='P2047':
                            for source,target in (('amount','amount'),('unit','unit')):
                                if source in fact:row[target]={'type':'literal','value':str(fact[source])}
                        if prop=='P577':
                            for source,target in (('time','timeValue'),('precision','precision')):
                                if source in fact:row[target]={'type':'literal','value':str(fact[source]).lstrip('+') if source=='time' else str(fact[source])}
                        if prop=='P31' and fact.get('id') in FILM_TYPES:row['filmType']={'type':'literal','value':'true'}
                    else:row['value']={'type':'literal','value':str(fact)}
                qualifiers=claim.get('qualifiers',{})
                if not isinstance(qualifiers,dict):raise ValueError('malformed_qualifiers')
                if not qualifiers:rows.append(row)
                for qp,snaks in qualifiers.items():
                    if not re.fullmatch(r'P[1-9]\d*',qp) or not isinstance(snaks,list) or not snaks:
                        raise ValueError('malformed_qualifier_values')
                    for snak in snaks:
                        copy=dict(row);copy['qualifierPredicate']={'type':'uri','value':'http://www.wikidata.org/prop/qualifier/'+qp}
                        copy['qualifierValue']={'type':'literal','value':json.dumps(snak,ensure_ascii=False,sort_keys=True,separators=(',',':'))}
                        rows.append(copy)
    return {'head':{},'results':{'bindings':rows}},observations,unresolved


def parse_catalogue(response,title,year):
    """Parse all observed rivals; never drop an inconvenient incomplete row."""
    if not isinstance(response,dict) or not isinstance(response.get('results',{}).get('bindings'),list):
        raise ValueError('invalid independent catalogue result')
    rows=response['results']['bindings']
    if any(response.get(k) for k in ('truncated','has_more','next','next_page')):
        raise ValueError('independent catalogue reports incomplete results')
    if len(rows)>=ROW_LIMIT:raise ValueError('independent catalogue row limit reached')
    groups=defaultdict(list)
    for row in rows:
        if not isinstance(row,dict):raise ValueError('invalid independent catalogue row')
        item=_binding(row,'item')
        if not re.fullmatch(r'https?://www\.wikidata\.org/entity/Q[1-9]\d*',item):
            raise ValueError('catalogue row lacks published work entity')
        groups[item.rsplit('/',1)[-1]].append(row)
    if len(groups)>=ITEM_LIMIT:raise ValueError('independent catalogue item limit reached')
    works=[]
    for item,observed in groups.items():
        issues=[];names=set();statements={}
        for row in observed:
            name=_binding(row,'name');predicate=_binding(row,'namePredicate')
            if not name or predicate not in NAME_PREDICATES or _words(name)!=_words(title):
                issues.append('published_complete_name_not_bound_to_target')
            else:names.add(name)
            prop=_binding(row,'property').rsplit('/',1)[-1]
            sid=_binding(row,'statement');rank=_binding(row,'rank').rsplit('#',1)[-1]
            if prop not in PROPERTIES or not re.fullmatch(r'https?://www\.wikidata\.org/entity/statement/[^\s]+',sid):
                issues.append('statement_source_missing_or_invalid');continue
            if rank not in ('NormalRank','PreferredRank','DeprecatedRank'):
                issues.append('unknown_statement_rank');continue
            key=(prop,sid)
            facts=statements.setdefault(key,{'property':prop,'statement':sid,'rank':rank,
                'values':set(),'amounts':set(),'units':set(),'dates':set(),'precisions':set(),'qualifiers':set(),'film_type':set()})
            if facts['rank']!=rank:issues.append('statement_rank_conflict')
            for target,source in (('values','value'),('amounts','amount'),('units','unit'),
                    ('dates','timeValue'),('precisions','precision'),('film_type','filmType')):
                value=_binding(row,source)
                if value:facts[target].add(value)
            qp=_binding(row,'qualifierPredicate');qv=_binding(row,'qualifierValue')
            if qp:facts['qualifiers'].add((qp,qv))
        live=[v for v in statements.values() if v['rank']!='DeprecatedRank']
        byprop=defaultdict(list)
        for fact in live:byprop[fact['property']].append(fact)
        imdb_ids={_id(v,True) for fact in byprop['P345'] for v in fact['values']}
        tmdb_ids={_id(v) for fact in byprop['P4947'] for v in fact['values']}
        id_issues=[]
        if '' in imdb_ids or len(imdb_ids)>1:id_issues.append('conflicting_imdb_ids')
        if '' in tmdb_ids or len(tmdb_ids)>1:id_issues.append('conflicting_tmdb_ids')
        if not imdb_ids:id_issues.append('missing_imdb_id')
        if not tmdb_ids:id_issues.append('missing_tmdb_id')
        years=set()
        for fact in byprop['P577']:
            if len(fact['dates'])!=1 or len(fact['precisions'])!=1:
                issues.append('missing_or_ambiguous_publication_date_precision');continue
            observed_date=next(iter(fact['dates']));precision=next(iter(fact['precisions']))
            if not re.fullmatch(r'\+?\d{4}-.*',observed_date) or not precision.isdigit() or int(precision)<9:
                issues.append('publication_year_not_precise');continue
            years.add(observed_date.lstrip('+')[:4])
        if years!={str(year)}:issues.append('publication_year_conflict_or_missing')
        if not byprop['P31'] or any(f['film_type']!={'true'} or not f['values'] for f in byprop['P31']):
            issues.append('missing_or_non_movie_type')
        runtimes=set()
        for fact in byprop['P2047']:
            if fact['qualifiers']:issues.append('runtime_version_qualifiers_need_confirmation')
            if fact['units']!={MINUTE} or len(fact['amounts'])!=1:
                issues.append('unknown_runtime_unit_or_ambiguous_quantity');continue
            try:seconds=float(next(iter(fact['amounts'])))*60
            except (ValueError,TypeError,OverflowError):seconds=None
            if not _positive(seconds):issues.append('invalid_runtime_quantity')
            else:runtimes.add(seconds)
        if len(runtimes)!=1:issues.append('missing_or_multiple_runtime_variants')
        # Preserve all observed statements, including deprecated and qualifiers.
        evidence=[{k:sorted(v) if isinstance(v,set) else v for k,v in fact.items()} for fact in statements.values()]
        works.append({'work_key':'wikidata:'+item,'title':next(iter(sorted(names))) if names else '',
            'year':str(year),'runtime_seconds':next(iter(runtimes)) if len(runtimes)==1 and not any('runtime' in x for x in issues) else None,
            'provider_ids':{'imdb_id':next(iter(imdb_ids)) if len(imdb_ids)==1 else '',
                'tmdb_id':next(iter(tmdb_ids)) if len(tmdb_ids)==1 else ''},
            'provider_id_issues':id_issues,
            'provider_id_observations':{'imdb_ids':sorted(imdb_ids),'tmdb_ids':sorted(tmdb_ids)},
            'issues':sorted(set(issues)),'published_names':sorted(names),'statements':evidence})
    known={}
    for work in works:
        for namespace,value in work['provider_ids'].items():
            if not value:continue
            key=(namespace,value)
            if key in known and known[key]!=work['work_key']:
                work['issues'].append('separate_records_share_provider_id')
                next(w for w in works if w['work_key']==known[key])['issues'].append('separate_records_share_provider_id')
            known[key]=work['work_key']
    return works


def choose_by_runtime(actual_seconds,works):
    """Exact frozen match/gray selector; metadata validation occurs separately."""
    result={'state':'pending','reason':'','selected_work_key':None,'observations':[],
        'global_identity_confirmed':False,'output_authorized':False,'scope':'observed_work_set_only'}
    def finish(reason,selected=None):
        result.update(reason=reason,selected_work_key=selected,state='runtime_proposal' if selected else 'pending');return result
    if not _positive(actual_seconds):return finish('missing_or_invalid_actual_duration')
    if len(works)<2:return finish('runtime_disambiguation_requires_multiple_observed_works')
    keys=[w.get('work_key') for w in works]
    if any(not isinstance(k,str) or not k for k in keys) or len(set(keys))!=len(keys):
        return finish('observed_work_records_not_distinct')
    for work in works:
        listed=work.get('runtime_seconds')
        if not _positive(listed):return finish('known_rival_runtime_missing_or_ambiguous')
        delta=abs(actual_seconds-listed);near=max(60.,min(180.,.02*listed));gray=max(600.,.10*listed)
        result['observations'].append({'work_key':work['work_key'],'listed_seconds':listed,'delta_seconds':delta,
            'match_band_seconds':near,'gray_band_seconds':gray,'band':'match' if delta<=near else 'gray' if delta<=gray else 'outside'})
    matches=[r for r in result['observations'] if r['band']=='match']
    if len(matches)!=1:return finish('no_unique_near_match')
    winner=matches[0]['work_key']
    if any(r['band']!='outside' for r in result['observations'] if r['work_key']!=winner):return finish('another_work_still_close')
    return finish('one_near_match_and_no_close_known_rival',winner)


def _selection_binding(result):
    """Local input integrity only, never a signature of catalogue truth."""
    evidence=result.get('evidence') or {}
    payload={'target':result.get('target'),'actual_duration_seconds':result.get('actual_duration_seconds'),
        'selected_work_key':result.get('selected_work_key'),
        'evidence':evidence,'discovery_observations':result.get('discovery_observations'),
        'source_observations':result.get('source_observations'),'full_claim_ids':result.get('full_claim_ids'),
        'unresolved_entities':result.get('unresolved_entities'),
        'works':[{k:w.get(k) for k in ('work_key','title','year','runtime_seconds','provider_ids','issues','provider_id_issues')}
            for w in result.get('works',[])]}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')).hexdigest()


def _source_digest(sources):
    return hashlib.sha256(json.dumps(sources,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')).hexdigest()


def _source_payload(response):
    if isinstance(response,dict):
        raw=json.dumps(response,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf8')
        return response,hashlib.sha256(raw).hexdigest(),'canonical_parsed_response'
    raw=response.encode('utf8') if isinstance(response,str) else bytes(response)
    return json.loads(raw.decode('utf8')),hashlib.sha256(raw).hexdigest(),'response_utf8_body'


def fetch_catalogue(url,budget):
    """One public stage GET, no key, retry, redirect or fallback endpoint."""
    from work_identity_resolver import _read_response
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment or (
            (parsed.netloc,parsed.path) not in (('query.wikidata.org','/sparql'),('www.wikidata.org','/w/api.php'))):
        raise ValueError('unexpected independent catalogue endpoint')
    request=urllib.request.Request(url,headers={'User-Agent':'SubFlow/RuntimeCatalogue','Accept':'application/json'})
    try:return _read_response(request,budget,limit=5_000_000)
    except urllib.error.HTTPError as error:
        import online_subtitles as online
        if error.code in (401,403):raise online.SubtitleAuthenticationError('公开片目服务拒绝访问；未重试或绕过。') from error
        if error.code==429:raise online.SubtitleQuotaError('公开片目服务请求受限；未重试。') from error
        raise


def recover_runtime_work(title,year,actual_duration,recovery_session,*,fetcher=None,cancel_event=None):
    result={'state':'pending','reason':'','target':{'title':str(title or ''),'year':str(year or '')},
        'actual_duration_seconds':actual_duration,'selected_work':None,'works':[],'evidence':{},
        'global_identity_confirmed':False,'output_authorized':False}
    if not _positive(actual_duration):result['reason']='missing_or_invalid_actual_duration';return result
    try:query=build_query(title,year)
    except ValueError:result['reason']='complete_known_title_and_year_required';return result
    import subtitle_tool_core as core
    import online_subtitles as online
    from work_identity_resolver import EvidenceBudgetExceeded
    core.check_cancel(cancel_event)
    budget=recovery_session.evidence_budget()
    if cancel_event is not None and budget.cancel_event is None:budget.cancel_event=cancel_event
    started=time.monotonic()
    try:
        budget.check()
        if budget.max_requests-budget.request_count<2:
            result['reason']='insufficient_shared_slots_for_two_stage';return result
        fetch=fetcher or fetch_catalogue
        url=ENDPOINT+'?'+urllib.parse.urlencode(sorted({'format':'json','query':query}.items()))
        first=budget.request('完整名称实体及日期发现',lambda:fetch(url,budget))
        core.check_cancel(cancel_event)
        payload,first_sha,first_scope=_source_payload(first)
        ids,observations=discovery_plan(payload,year)
        result['discovery_observations']=observations;result['full_claim_ids']=ids
        result['discovered_ids']=[entry['work_key'].split(':',1)[-1] for entry in observations]
        if not ids:result['reason']='no_observed_target_year_name_entities';return result
        second=budget.request('目标年份实体完整声明',lambda:fetch(entity_api_url(ids),budget))
        core.check_cancel(cancel_event)
        entities,second_sha,second_scope=_source_payload(second)
        converted,source_observations,unresolved=convert_entities(entities,ids,title,year)
        result['source_observations']=source_observations;result['unresolved_entities']=unresolved
        works=parse_catalogue(converted,title,year);result['works']=works
        sources=[{'stage':'discovery','endpoint':ENDPOINT,'query_sha256':hashlib.sha256(query.encode('utf8')).hexdigest(),
            'response_sha256':first_sha,'response_digest_scope':first_scope},
            {'stage':'complete_entities','endpoint':ENTITY_API,'entity_ids':ids,
                'response_sha256':second_sha,'response_digest_scope':second_scope}]
        result['evidence']={'endpoint':ENDPOINT,'acquisition':ACQUISITION,
            'query_sha256':hashlib.sha256(query.encode('utf8')).hexdigest(),
            'response_sha256':_source_digest(sources),'response_digest_scope':'canonical_two_source_receipts',
            'sources':sources,
            'observed_set_complete_within_limits':True,'global_catalogue_completeness_claimed':False,
            'name_scope':'finite observed exact English published names with directly observed target year',
            'policy_version':POLICY_VERSION,'frozen_policy_sha256':FROZEN_POLICY_SHA256}
        if unresolved:result['reason']='observed_possible_target_work_incomplete'
        elif any(w['issues'] for w in works):result['reason']='observed_work_metadata_missing_or_conflicting'
        else:
            choice=choose_by_runtime(actual_duration,works);result.update({k:v for k,v in choice.items() if k not in ('scope',)})
            result['selector_observations']=choice['observations']
            if choice['selected_work_key']:
                result['selected_work']=next(w for w in works if w['work_key']==choice['selected_work_key'])
                selected=result['selected_work']
                if (not any(selected['provider_ids'].values())
                        or any('conflicting' in x for x in selected['provider_id_issues'])):
                    result.update(state='pending',reason='selected_work_provider_ids_missing_or_conflicting',
                        selected_work=None,selected_work_key=None)
                else:
                    budget.check();core.check_cancel(cancel_event)
                    result['selection_binding_sha256']=_selection_binding(result)
    except (core.CancelledError,online.SubtitleAuthenticationError,online.SubtitleQuotaError):raise
    except EvidenceBudgetExceeded:
        result.update(state='pending',reason='shared_evidence_budget_exhausted',selected_work=None,selected_work_key=None)
        result.pop('selection_binding_sha256',None)
    except Exception as error:
        result.update(state='pending',reason='runtime_catalogue_unavailable_'+type(error).__name__,selected_work=None,selected_work_key=None)
        result.pop('selection_binding_sha256',None)
    finally:
        result.update(request_count=budget.request_count,elapsed_seconds=time.monotonic()-started,
            shared_budget_elapsed_seconds=time.monotonic()-budget.started,remaining_seconds=budget.remaining)
    return result


def candidate_matches_runtime_proposal(candidate,result):
    """Only compare original fields; never borrow, merge or overwrite IDs."""
    if result.get('state')!='runtime_proposal' or result.get('global_identity_confirmed') is not False:return False
    feature_type=str(getattr(candidate,'feature_type','') or '').strip().casefold()
    if feature_type and feature_type!='movie':return False
    if any(str(getattr(candidate,name,'') or '').strip() for name in ('feature_season_number','feature_episode_number',
            'feature_parent_imdb_id','feature_parent_tmdb_id')):return False
    work=result.get('selected_work') or {};target=result.get('target') or {};year=str(target.get('year') or '')
    evidence=result.get('evidence') or {}
    if (work.get('issues') or work.get('work_key')!=result.get('selected_work_key')
            or work not in result.get('works',[]) or not _positive(work.get('runtime_seconds'))
            or evidence.get('endpoint')!=ENDPOINT
            or evidence.get('acquisition')!=ACQUISITION
            or not re.fullmatch(r'[0-9a-f]{64}',str(evidence.get('response_sha256','')))
            or not _positive(result.get('actual_duration_seconds'))):return False
    if any(w.get('issues') for w in result.get('works',[])):return False
    sources=evidence.get('sources');ids=result.get('full_claim_ids')
    if (not isinstance(sources,list) or len(sources)!=2 or any(not isinstance(s,dict) for s in sources)
            or not isinstance(ids,list) or not ids or len(ids)>30
            or any(not isinstance(q,str) or not re.fullmatch(r'Q[1-9]\d*',q) for q in ids) or len(set(ids))!=len(ids)
            or [(s.get('stage'),s.get('endpoint')) for s in sources]!=[('discovery',ENDPOINT),('complete_entities',ENTITY_API)]
            or sources[1].get('entity_ids')!=ids or sources[0].get('query_sha256')!=evidence.get('query_sha256')
            or any(not re.fullmatch(r'[0-9a-f]{64}',str(s.get('response_sha256',''))) or
                s.get('response_digest_scope') not in ('canonical_parsed_response','response_utf8_body') for s in sources)
            or evidence.get('response_digest_scope')!='canonical_two_source_receipts'
            or evidence.get('response_sha256')!=_source_digest(sources)
            or evidence.get('policy_version')!=POLICY_VERSION or evidence.get('frozen_policy_sha256')!=FROZEN_POLICY_SHA256
            or evidence.get('global_catalogue_completeness_claimed') is not False
            or any(w.get('work_key') not in {'wikidata:'+q for q in ids} for w in result.get('works',[]))
            or result.get('unresolved_entities')):return False
    try:
        if evidence.get('query_sha256')!=hashlib.sha256(build_query(target.get('title'),year).encode('utf8')).hexdigest():return False
    except ValueError:return False
    if result.get('selection_binding_sha256')!=_selection_binding(result):return False
    selected=choose_by_runtime(result['actual_duration_seconds'],result['works'])
    if selected['state']!='runtime_proposal' or selected['selected_work_key']!=work['work_key']:return False
    if any('conflicting' in x for x in work.get('provider_id_issues',[])):return False
    feature_year=str(getattr(candidate,'feature_year','') or '')
    if not year or feature_year!=year or str(work.get('year') or '')!=year:return False
    title=str(getattr(candidate,'feature_title','') or '')
    prefix=re.match(r'^\s*((?:19|20)\d{2})\s+[-–—]\s+(.+)$',title)
    if prefix and prefix.group(1)!=feature_year:return False
    if prefix and prefix.group(1)==feature_year:title=prefix.group(2)
    import online_subtitles as online
    if (not online._titles_equivalent(title,target.get('title',''))
            or not online._titles_equivalent(work.get('title',''),target.get('title',''))):return False
    ids=work.get('provider_ids') or {};comparable=False
    for namespace,field in (('imdb_id','feature_imdb_id'),('tmdb_id','feature_tmdb_id')):
        expected=_id(ids.get(namespace),namespace=='imdb_id');actual=_id(getattr(candidate,field,''),namespace=='imdb_id')
        if str(getattr(candidate,field,'') or '').strip() and not actual:return False
        if expected and actual:
            if expected!=actual:return False
            comparable=True
    return comparable
