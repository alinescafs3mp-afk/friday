"""Own bounded stock TEXT/JSON metadata materializer. Never executes supplied Source.
Lexical byte spans are not AST/compiler/function qualification. No unsafe bodies copied.
"""
import os, stat, json, hashlib, re, difflib, time, resource
from collections import Counter
from pathlib import Path

OUT=Path('/var/tmp/friday-sol063-lab866-browser-whole209-all216-all29-independent-source-review')
LAB=Path('/var/tmp/friday-lab866-lab864-a183-browser-whole209-all216-all29-connected-source-closure')
BASE=Path('/home/jericho/.jericho/grok-takeover')
A174=Path('/var/tmp/friday-astra-a171-whole209-all216-actual-source-independent-review-a174-g1')
A183=Path('/var/tmp/friday-astra-lab864-browser-connected-source-review-a183-g1')
START=time.monotonic_ns(); READ=[]; CACHE={}; os.umask(0o077)
def nine(s):
    return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def sha(b): return hashlib.sha256(b).hexdigest()
def unique(pairs):
    d={}
    for k,v in pairs:
        assert k not in d,('duplicate JSON key',k)
        d[k]=v
    return d
def parse(b): return json.loads(b,object_pairs_hook=unique,parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
def rd(path,pin=None):
    path=str(path)
    if path not in CACHE:
        a=os.lstat(path)
        assert stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid() and a.st_nlink==1 and a.st_mode&0o777==0o600,path
        assert sum(x['bytes'] for x in READ)+a.st_size<=64*1024*1024,'metadata read reserve'
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            assert nine(os.fstat(fd))==nine(a)
            parts=[]
            while True:
                b=os.read(fd,1048576)
                if not b:break
                parts.append(b)
            raw=b''.join(parts)
            assert nine(os.fstat(fd))==nine(a)==nine(os.lstat(path))
        finally:os.close(fd)
        CACHE[path]=raw
        READ.append({'path':path,'bytes':len(raw),'sha256':sha(raw),'identity9_decimal_strings':nine(a)})
    raw=CACHE[path]
    if pin:
        assert sha(raw)==pin['sha256'] and len(raw)==pin.get('bytes',len(raw)),path+' hash/size'
        n=pin.get('identity9_decimal_strings',pin.get('identity9'))
        if n is not None: assert nine(os.lstat(path))==[str(v) for v in n],path+' nine'
    return raw
def obj(path,pin=None): return parse(rd(path,pin))
def save(name,value):
    path=OUT/name;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    b=(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(b)
def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
intake=obj(OUT/'author/intake-verification.json')
expected={v['path']:v for v in obj(OUT/'author/expected-pins.json')}
inp=obj(BASE/'ASTRA-E4-SOL063-INPUT-20261002.json')
assert sha(CACHE[str(BASE/'ASTRA-E4-SOL063-INPUT-20261002.json')])=='541ef5fa6d0a386ca29c3f69c1020fb9bcc39a388a23be96a7300cb613f58109'
decision=obj(OUT/'author/review-decisions.json')
current={v['name']:v['pin'] for v in inp['current209']}
for p in inp['source_pins']:rd(p['path'],p)
for p in current.values():rd(p['path'],p)
for d in inp['actual_delta8']:
    rd(d['old_pin']['path'],d['old_pin']);rd(d['new_pin']['path'],d['new_pin'])
for p in (A174/'WHOLE216-INDEPENDENT.json',A174/'WHOLE209-INDEPENDENT.json',A183/'whole209.json'):
    rd(p,expected[str(p)])
normdocs=obj(OUT/'author/normative-inputs.json')
prior156=normdocs[str(BASE/'ASTRA-E4-SOL057-INPUT-20261002.json')]['independent_prior_A156']
for k in ('terminal_pin','manifest_pin','Root_received'): rd(prior156[k]['path'],prior156[k])
old156=obj(prior156['manifest_pin']['path'])
for p in old156['members']:rd(p['path'],p)
root156=Path(prior156['manifest_pin']['path']).parent
assert sorted(str(p) for p in root156.rglob('*') if p.is_file())==sorted(old156['exact_pathset_including_manifest'])
extra={
 'G1.py':{'path':'/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1.py','bytes':46980,'sha256':'ba70d584ab4b145195283456b17d98a8ff129a8f7bb3be3595c711fe97d6c223'},
 'R4.py':{'path':'/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1-R4.py','bytes':38388,'sha256':'a68f62ce16883f108992a8f237b9614d964ff0141d9002b5e714fdc2887b414c'}
}
for p in extra.values():rd(p['path'],expected.get(p['path'],p))
allpins={**current,**extra}
# The private full47 package DAG:44 leaves -> pathset -> manifest -> publisher.
publication=obj(LAB/'manifest/manifest.json');pathset=obj(LAB/'seal/pathset.json');payload=obj(LAB/'publish/payload.json')
assert len(publication['files'])==44 and publication['files']==[{k:v[k] for k in ('bytes','path','sha256')} for v in pathset['files']]
for p in pathset['files']:
    rd(LAB/p['path'],p)
assert publication['pathset_sha256']==sha(rd(LAB/'seal/pathset.json')) and publication['pathset_bytes']==len(CACHE[str(LAB/'seal/pathset.json')])
assert payload['terminal_manifest_sha256']==sha(rd(LAB/'manifest/manifest.json')) and payload['terminal_manifest_size']==len(CACHE[str(LAB/'manifest/manifest.json')])
physical=sorted(str(p.relative_to(LAB)) for p in LAB.rglob('*') if p.is_file())
declared=sorted([p['path'] for p in pathset['files']]+['seal/pathset.json','manifest/manifest.json','publish/payload.json'])
assert physical==declared and len(physical)==47
save('publication47.json',{'status':'VERIFIED_FULL_BYTES_PRIVATE_SHA9_EXACT_PATHSET_ACYCLIC','count':47,'paths':physical,'leaf_count':44,'DAG':'44 leaves -> seal/pathset -> manifest/manifest -> publish/payload; no self hash/backedge','manifest_pin':next(p for p in inp['source_pins'] if p['path']==str(LAB/'manifest/manifest.json')),'semantic_current_binders':'Separate M01, not physical publication corruption'})
# Exact byte deltas; no historical raw bodies are copied into a review/repro.
delta=[]
for d in inp['actual_delta8']:
    old=CACHE[d['old_pin']['path']];new=CACHE[d['new_pin']['path']]
    oa=old.splitlines(keepends=True);na=new.splitlines(keepends=True)
    oo=[0];no=[0]
    for line in oa:oo.append(oo[-1]+len(line))
    for line in na:no.append(no[-1]+len(line))
    spans=[]
    for tag,a,b,c,e in difflib.SequenceMatcher(None,oa,na,autojunk=False).get_opcodes():
        spans.append({'kind':tag,'old_start':oo[a],'old_end':oo[b],'new_start':no[c],'new_end':no[e],
         'old_sha256':sha(old[oo[a]:oo[b]]),'new_sha256':sha(new[no[c]:no[e]]),'old_lines':[a+1,b],'new_lines':[c+1,e]})
    assert (old!=new)==d['changed']
    assert sum(s['old_end']-s['old_start'] for s in spans)==len(old) and sum(s['new_end']-s['new_start'] for s in spans)==len(new)
    delta.append({'name':d['name'],'changed':old!=new,'old_pin':d['old_pin'],'new_pin':d['new_pin'],'gapless_fullbyte_partition':spans,'raw_body_copy':'NOT_SUPPLIED_ABSTRACT_REVIEW_ONLY'})
assert sum(d['changed'] for d in delta)==2
prior209={r['name']:r['pin'] for r in obj(A183/'whole209.json')['rows']}
assert set(current)==set(prior209)
changes=[n for n,p in current.items() if p['sha256']!=prior209[n]['sha256'] or p['bytes']!=prior209[n]['bytes']]
assert changes==['A071-CONTROLS.py','A071-SUPERVISOR.py']
save('actual-delta8-equal207.json',{'count209':209,'changed':changes,'equal207':207,'exact_new209_reverified':True,'eight_full_old_new_reads':delta,'equality_basis':'Full verified current bytes with exact byte-count/SHA matching pinned independent A183 old209; eight explicit old/new files read in full','no_automatic_acceptance':True})
# Gapless lexical METHOD/complement index for every actual file, no AST/eval.
indices={}
for name,p in allpins.items():
    raw=CACHE[p['path']];lines=raw.splitlines(keepends=True);offsets=[0]
    for line in lines:offsets.append(offsets[-1]+len(line))
    markers=[]
    if name.endswith(('.py','.c','.h')):
        for i,line in enumerate(lines):
            m=re.match(rb'^(\s*)(?:async\s+)?(def|class)\s+([A-Za-z_][A-Za-z_0-9]*)',line)
            c=re.match(rb'^(?:static\s+)?(?:inline\s+)?[A-Za-z_][A-Za-z_0-9* ]*\s+([A-Za-z_][A-Za-z_0-9]*)\([^;]*\)\s*\{',line)
            if m:markers.append((i,m[2].decode()+':'+m[3].decode(),len(m[1])))
            elif c:markers.append((i,'C-lexical:'+c[1].decode(),0))
    bounds=[(0,'complement/header',0)] if not markers or markers[0][0]!=0 else []
    bounds+=markers
    parts=[]
    for j,(start,label,indent) in enumerate(bounds):
        end=bounds[j+1][0] if j+1<len(bounds) else len(lines)
        a,b=offsets[start],offsets[end]
        parts.append({'label':label,'indent':indent,'first_line':start+1,'last_line':end,'start_byte':a,'end_byte':b,'bytes':b-a,'sha256':sha(raw[a:b])})
    assert sum(p['bytes'] for p in parts)==len(raw)
    indices[name]={'pin':p,'full_bytes_read':True,'lines':len(lines),'lexical_byte_partitions':parts,'semantic_claim':'No AST/compiler or automatic method qualification; complete affected methods manually reviewed, exact unchanged scoped evidence reused'}
save('full-methods-byte-index.json',{'schema':'friday.sol063.lexical-full-byte-method-complement-index.v1','source_count':209,'extra_actual_dependencies':2,'index':indices})
site_rows=[]
for f in decision['findings']:
    for name,a,b in f['sites']:
        if name not in allpins:continue
        p=allpins[name];raw=CACHE[p['path']];lines=raw.splitlines(keepends=True)
        assert 1<=a<=b<=len(lines),(name,a,b,len(lines))
        site_rows.append({'finding':f['id'],'name':name,'pin':p,'lines':[a,b],'complete_range_sha256':sha(b''.join(lines[a-1:b])),'raw_body_copy':False})
save('finding-code-sites.json',site_rows)
# Typed, ordered original obligations, exact field membership and integer/absence.
original=obj(LAB/'ORIGINAL216-EXPECTATIONS.json')['rows'];typed=obj(LAB/'TYPED216-CONTRACTS.json')['rows']
bill=obj(current['A071-BILL.json']['path']);oldmatrix=obj(LAB/'WHOLE216-ACTUAL-SOURCE-MATRIX.json')['rows']
prior216=obj(A174/'WHOLE216-INDEPENDENT.json')['rows'];authorroutes=obj(LAB/'evidence/whole216-performing-routes.json')['rows']
assert len(original)==len(typed)==len(prior216)==len(oldmatrix)==len(authorroutes)==len(bill['control_map'])==216
assert list(bill['control_map'])==[r['id'] for r in original]
for i,(r,t,p,m,a) in enumerate(zip(original,typed,prior216,oldmatrix,authorroutes)):
    assert r['position']==t['position']==p['position']==m['position']==a['position']==i
    assert r['id']==t['id']==p['id']==m['id']==a['id']
    assert canonical(r['expected'])==canonical(t['expected'])==canonical(p['original_expected'])==canonical(m['original_expected'])==canonical(bill['control_map'][r['id']])
    for k,v in r['expected'].items():
        if k in a:assert canonical(a[k])==canonical(v)
    assert len(r['expected'])==9 and t['active'] is True
# Python literal is never evaluated: its exact full bytes are unchanged across the new2.
controls_delta=next(d for d in inp['actual_delta8'] if d['name']=='A071-CONTROLS.py')
table_old=next(l for l in CACHE[controls_delta['old_pin']['path']].splitlines() if l.startswith(b'WHOLE216_TABLE='))
table_new=next(l for l in CACHE[controls_delta['new_pin']['path']].splitlines() if l.startswith(b'WHOLE216_TABLE='))
assert table_old==table_new
safe=obj(LAB/'ORIGINAL29-SAFETY-REQUIREMENTS.json')['rows'];safeids={r['id'] for r in safe};assert len(safe)==len(safeids)==29
rows=[]
for r,t,p in zip(original,typed,prior216):
    e=r['expected'];case=r['id'];owned=e['owned_children']
    gaps=[]
    if owned:
        if case.startswith('outer_'): route='coordinator_outer14';operation='Actual Supervisor.supervise_owned + terminal_check against bounded direct child; original raw/cleanup and both receiving joins incomplete';gaps=['SOL063-C01','SOL063-C04','SOL063-C05','SOL063-C08']
        elif case.startswith('owned_registration_'):route='coordinator_registration5';operation='Same immediate-exit child for all5; original per-ID registration/proc/pidfd/race/postcustody fault selection not implemented';gaps=['SOL063-C01','SOL063-C05','SOL063-C07','SOL063-C08']
        elif case.startswith('deadline_'):route='coordinator_deadline9';operation='Actual original Run.guard with existing capsule ends; no original3-target/receipts/hash/stage_end operations';gaps=['SOL063-C01','SOL063-C05','SOL063-C07']
        elif case.startswith('body_'):route='coordinator_body4';operation='Actual existing G1.write_all via x.m symbol on small owned pipe and EOF; not original G1.worker/BoundedResponse/full3 body and hash relation';gaps=['SOL063-C01','SOL063-C05','SOL063-C07','SOL063-C08']
        else:route='coordinator_abstract31';operation='Generic historical body abstraction; legitimate original benign operation still required and missing';gaps=['SOL063-C01','SOL063-C07']
        caller='A087.prepare_whole216 -> admitted Bootstrap coordinator -> Supervisor.main mode1 -> Controls.controls_whole216 -> coordinator_receipt; returns before native selected producer prepare'
        owner='Actual coordinator owns intended ordinary targets; after-fork journal is not full native generation; neither receiver is joined to this topology'
        registry='Original selected1 producer Root proof absent in this new route; target count remains original requirement, not infrastructure count'
        raw='Operation-labelled records, not both indexed full original producer stdout/stderr; no whole2-receiver transport qualification'
        resources='Original coordinator+3 targets fits canonical inner4 only without extra producer; operation-specific actual guard/cost/phase/cleanup not wholly implemented'
        pointers=[['A071-CONTROLS.py',775,936],['A071-SUPERVISOR.py',538,720]]
    elif e['consumer'] in ('G1.resources','acquisition_resources'):
        route='observer_G1_7' if e['consumer']=='G1.resources' else 'observer_envelope10'
        operation='Actual '+e['consumer']+' call before delegated prepare/join; all selected arguments empty, original controlled resource first-fault operands absent'
        caller='A087.prepare_whole216 -> Bootstrap.clone_into_cgroup121 coordinator -> Supervisor.main mode1 -> Controls.resource_observer_receipt; not an outer preparer relocation'
        owner='Actual original coordinator in inner leaf, not an unentered original observer domain'
        registry='No selected actor/registration/privatewait/Root finish evidence; public and Supervisor producer-required contracts incompatible'
        raw='Empty producer_streams, null after/native evidence; no full original2-stream receiver join'
        resources='G1 finite leaf>=256MiB predicate contradicts actual canonical inner192MiB; envelope exact original membership/negativecause input still required'
        gaps=['SOL063-C01','SOL063-C05','SOL063-C06'];pointers=[['A071-CONTROLS.py',955,1038],['A071-BOOTSTRAP.c',678,695],['G1.py',735,786],['A071-EXECUTOR.py',768,810]]
    else:
        route='delegated136'
        operation='Authentic existing '+str(p['actual_selector'])+' through whole216_perform/a171_perform; scoped prior '+p['independent_family']+' findings/credits retained on matching unchanged dependencies, not runtime acceptance'
        caller='A087.prepare_whole216 -> original selected119 -> Controls.whole216_input -> controls_whole216 native prepare/actual role0 producer -> whole216_perform/a171_perform -> Controls oracle -> Supervisor -> A087 oracle'
        owner='Same actual native role0 producer PID/birth parent; unchanged owner/join/start/finish/privatewait ACK custody still required'
        registry='Existing native selected producer evidence remains actual requirement; no arbitrary caller authority or owner reset'
        raw='Controls length-frame restoration is genuine but A087 remains single-JSON original-stream consumer; large complete receipt copies exceed bound'
        resources='Actual production_resources before delegated operation; original caps/ends remain unchanged; no observed runtime cost credit'
        gaps=['SOL063-C02','SOL063-C03']
        if p['independent_family'] in ('retained','output'):gaps.append('SOL063-C08')
        if case=='output_disk':gaps.append('SOL063-C09')
        if p['independent_family'] in ('original_phase_control','terminal_serializer') or case=='guarded_write':gaps.append('SOL063-C07')
        pointers=[['A071-CONTROLS.py',414,626],['A071-CONTROLS.py',1019,1240],['A071-CONTROLS.py',1518,1879]]
    rows.append({'position':r['position'],'id':case,'original_expected':e,'original_binding':p['original_binding'],'current_typed_producer':t['producer'],
      'route':route,'independent_verdict':'CONNECTED_SOURCE_OPEN_NOT_RUNTIME_QUALIFIED','code_findings':gaps,'static_primitive_credit':p['independent_verdict'],
      'prior_static_credit_policy':'No inherited runtime/newbyte acceptance; prior per-ID semantics used only on full byte/pin match; changed wrappers reconsidered here',
      'actual_operation':operation,'caller':caller,'performing_sites':pointers,'current_receivers':['A071-SUPERVISOR.py','A087-PUBLIC-DRIVER.py'],
      'six_dimensions':{'input':{'exact_original_order_fields_types':True,'producer':t['producer'],'actual_route':route,'mandatory_operand_and_firstfault':operation},
      'owner':owner,'registry':registry,'producer':operation,'causal_output':{'required':e,'raw':raw,'phase':'Original stage/cause/body/started_routes/owned_children/hashes are independent required values, not copied outcomes; actual phase/end relation and all errors/cleanup remain mandatory','receiver_status':'OPEN exact gaps above'},'resources':resources},
      'raw_body_hash_phase_end':'Both stdout/stderr plus exact original full body/hash/cardinality/cause/stage/error/FD/cleanup/ends required on success and refusal; NOT_RUN',
      'safety29_member':case in safeids,'safe_functional_required':True,'unsafe_historical_methods':'ABSTRACT_REQUIRED_NOT_RUN required=true waiver=false; no operational body/fixture/repro',
      'runtime':'NOT_RUN','SourceReady':False,'Root_admission':False,'GO':False})
counts=dict(Counter(r['route'] for r in rows))
assert counts=={'delegated136':136,'coordinator_abstract31':31,'coordinator_deadline9':9,'observer_G1_7':7,'coordinator_body4':4,'coordinator_outer14':14,'coordinator_registration5':5,'observer_envelope10':10},counts
save('whole216-independent.json',{'schema':'friday.sol063.original216-independent-connected-review.v1','count':216,'original_fields_types_and_order_equal':True,'Python_table_unchanged_fullbytes_sha256':sha(table_new),'Python_table_AST_or_eval':'NOT_RUN','route_counts':counts,'original216_pin':next(p for p in inp['source_pins'] if p['path']==str(LAB/'ORIGINAL216-EXPECTATIONS.json')),'rows':rows,'runtime':'NOT_RUN','SourceReady':False,'GO':False})
newrows={r['id']:r for r in rows};safety=[]
for r in safe:
    nr=newrows[r['id']];assert r['position']==nr['position'] and canonical(r['original_expected'])==canonical(nr['original_expected'])
    safety.append({'position':r['position'],'id':r['id'],'original_expected':r['original_expected'],
      'independent_classification':'BENIGN_FUNCTIONAL_OBLIGATION_REMAINS_REQUIRED; no dangerousness inferred from name/consumer/cause',
      'historical_operational_mechanisms':{'status':'ABSTRACT_REQUIRED_NOT_RUN','required':True,'waiver':False,'payload_fixture_generator_repro_exploit':'NOT_SUPPLIED'},
      'actual_current_operation':nr['actual_operation'],'current_route':nr['route'],'code_findings':nr['code_findings'],
      'scope_credit':'Actual CA parser scoped prior credit at position93; write_all primitive scoped credit for125-128; no original full3 worker acceptance',
      'safe_performing_operation_required':True,'original_positive_and_negative_obligation_unchanged':True,'runtime':'NOT_RUN','SourceReady':False,'GO':False})
save('exact29-independent.json',{'count':29,'all_original_order_expectations_preserved':True,'exact_unsafe_operational_ID_classifier_established':False,'safe_functional_waiver':False,'rows':safety})
old174={r['name']:r for r in obj(A174/'WHOLE209-INDEPENDENT.json')['rows']}
wholerows=[]
for name,p in current.items():
    prior=old174[name]
    refs=[f['id'] for f in decision['findings'] if any(s[0]==name for s in f['sites'])]
    historical=name.startswith(('UPSTREAM-','INPUT-A079-CURRENT-','REVIEW-','CONTRACT-'))
    wholerows.append({'name':name,'current_pin':p,'fullbytes_SHA9_verified':True,'changed_from_A183':name in changes,
      'role':'HISTORICAL_OR_NORMATIVE_NOT_SELECTED_EXECUTION' if historical else 'ACTUAL_CURRENT_SOURCE_OR_REQUIRED_DATA_CONTRACT',
      'current_findings':refs,'prior_static_verdict':prior['verdict'],'prior_static_findings_reference_only':prior['findings'],
      'same_as_A174_bytes':p['sha256']==prior['pin']['sha256'] and p['bytes']==prior['pin']['bytes'],
      'method_complement_index':'full-methods-byte-index.json:index.'+name,
      'review_policy':'Complete actual Source references/read; semantically affected whole methods traced; unchanged exact prior static findings/credits reused, NOT newly qualified. Historical mechanisms remain abstract and not executable.',
      'source_execution':'NOT_RUN','runtime_acceptance':False})
save('whole209-independent.json',{'count':209,'changed2_equal207_A183':True,'rows':wholerows,'current_source_bytes':sum(p['bytes'] for p in current.values()),'no_blanket_whole_acceptance':True})
# New current binding overlay verified; older internal views remain separate.
bindings=obj(LAB/'current209-bindings.json')['bindings'];assert len(bindings)==209
for row in bindings:
    p=current[row['name']];assert row['pin']['sha256']==p['sha256'] and row['pin']['bytes']==p['bytes'] and row['pin']['path']==p['path']
oldcurrent=obj(LAB/'CURRENT-SOURCE209.json')['members']
stale=[{'name':r['name'],'old_view_pin':r['pin'],'actual_pin':current[r['name']]} for r in oldcurrent if r['pin']['sha256']!=current[r['name']]['sha256']]
assert {r['name'] for r in stale}==set(changes)
save('current-binders-review.json',{'new_current209_bindings':'EXACT209_VERIFIED','publication47':'VALID','older_internal_current_view_stale2':stale,'earlier_delta':'A158->LAB864 not LAB864->LAB866; neither executed nor copied as current body reconstruction','finding':'SOL063-M01','actual_current_delta_witness':'actual-delta8-equal207.json','future_consumer_bundle':'Must coherently match original A087 source-manifest/schema/compiled/currentSHA contract; future assembly alone not CODE'})
save('all6-independent.json',{'dimensions':{
 'input':{'status':'ORIGINAL216_TYPES_ORDER_FIELDS_PRESERVED; per-ID selected negative/target inputs incomplete','findings':['SOL063-C06','SOL063-C07'],'sites':['whole216_input','a171_validate_input']},
 'owner':{'status':'GENUINE_NATIVE_OWNER_CREDITS_RETAINED; new coordinator/observer/error custody join open','findings':['SOL063-C01','SOL063-C05','SOL063-C08']},
 'registry':{'status':'Original native actual role/generation proof preserved as required; new route does not produce both-receiver selected evidence; actual target registration faults missing','findings':['SOL063-C01','SOL063-C07']},
 'producer':{'status':'Authentic partial consumers preserved; full original owned3/body/hash/phase/benign controls not delivered','findings':['SOL063-C06','SOL063-C07','SOL063-C09']},
 'causal_output':{'status':'Firstfault/sticky/raw/frame improvements genuine; full prefix/EOF/error/custody/two-receiver join open','findings':['SOL063-C01','SOL063-C02','SOL063-C04','SOL063-C05','SOL063-C08']},
 'resources':{'status':'Original caps/ends retained; concrete leaf/copy/FSIZE/normal acquisition conflicts persist; runtime whole cost unknown','findings':['SOL063-C03','SOL063-C06','SOL063-C08','SOL063-C09']}},
 'per_ID_complete':'whole216-independent.json','all_original_methods':'full-methods-byte-index.json','both_receivers_current_pins':{k:current[k] for k in ('A071-SUPERVISOR.py','A087-PUBLIC-DRIVER.py')},'caps':decision['preserved_caps'],'gates':'All original required NOT_RUN no waiver','SourceReady':False,'Root_admission':False,'GO':False})
save('review.json',{**decision,'coverage':{'full47_publication':'VERIFIED','current209':'FULL_SHA9_2CHANGED_207EQUAL_VERIFIED','original216':'EXACT_TYPES_ORDER_FIELDS_ALL216_INDEPENDENT_JOIN_MATRIX','original29':'EXACT29_REQUIRED_SAFE_AND_ABSTRACT_UNSAFE_NO_WAIVER','all6':'FULL_CONNECTED_DIMENSION_MATRIX','all_methods':'FULLBYTE_LEXICAL_METHOD_COMPLEMENT_INDEX; affected semantic dependencies manually reviewed; exact prior unchanged static evidence reused','both_receivers':'INCOMPATIBLE C01/C02','source_execution':'NOT_RUN'},'proof_files':['publication47.json','actual-delta8-equal207.json','finding-code-sites.json','whole209-independent.json','whole216-independent.json','exact29-independent.json','all6-independent.json','full-methods-byte-index.json','current-binders-review.json']})
save('author/materialization-read-ledger.json',{'reads':READ,'read_bytes':sum(r['bytes'] for r in READ),'elapsed_ns':str(time.monotonic_ns()-START),'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'user_CPU_sec':resource.getrusage(resource.RUSAGE_SELF).ru_utime,'system_CPU_sec':resource.getrusage(resource.RUSAGE_SELF).ru_stime,'implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN','source_import_AST_compile_eval_exec_tests':'NOT_RUN','model_children':0,'network':0})
print(json.dumps({'status':'INDEPENDENT_REJECT_CURRENT_WHOLE_SOURCE','package47':47,'source209':209,'delta2_equal207':True,'whole216':216,'safety29':29,'routes':counts,'read_bytes':sum(r['bytes'] for r in READ),'output_files':len([p for p in OUT.rglob('*') if p.is_file()]),'output_bytes':sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file())}))
