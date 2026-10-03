"""NEW trusted SOL053 stock metadata helper; never load Source or consumers.

Emit apply_patch text only; the agent applies it through its editing tool.
JSON projections, full byte comparisons, SHA256, stable9 and ordinary wire only.
No import/AST/compile/eval/exec of Source, fixtures, archive or old helper.
"""
import os, stat, sys, json, hashlib, resource, difflib, datetime

ROOT='/var/tmp/friday-sol053-a138-sol052-whole15-69-performing-publisher-connected-source-repair'
OLD='/var/tmp/friday-astra-publisher-a135-whole-performing-root-actor-consumer-source-closure-a138-g1'
CONSUMER='/var/tmp/friday-astra-publisher-a122-whole-connected-source-closure-a128-g1'
REVIEW='/var/tmp/friday-sol052-a138-whole15-performing-publisher-root-source-review'
TASK='/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL053-TASK.txt'
ASSIGNMENT='ASTRA-E4-SOL053-A138-SOL052-WHOLE15-69-PERFORMING-PUBLISHER-CONNECTED-SOURCE-REPAIR'
RESULT='/home/jericho/.jericho/grok-takeover/'+ASSIGNMENT+'-RESULT.json'
ACCEPTED='2026-10-02T06:40:38+03:00'
resource.setrlimit(resource.RLIMIT_AS,(8589934592,)*2)
resource.setrlimit(resource.RLIMIT_CPU,(120,)*2)
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
resource.setrlimit(resource.RLIMIT_NOFILE,(256,)*2)
os.umask(0o077)
READS=[]

def nine(s):
    return list(map(str,(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)))

def wire(value):
    return (json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':'))+'\n').encode('ascii')

def sha(raw):return hashlib.sha256(raw).hexdigest()

def read(path,expected=None):
    s=os.lstat(path)
    assert stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode)==0o600 and s.st_uid==1000 and s.st_nlink==1,(path,'custody')
    assert s.st_size<=16777216,(path,'size')
    fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:
        assert nine(os.fstat(fd))==nine(s)
        raw=bytearray()
        while len(raw)<s.st_size:
            part=os.read(fd,min(65536,s.st_size-len(raw)));assert part;raw.extend(part)
        assert not os.read(fd,1)
        assert nine(os.fstat(fd))==nine(s)==nine(os.lstat(path))
        raw=bytes(raw)
    finally:os.close(fd)
    pin={'path':path,'bytes':len(raw),'sha256':sha(raw),'identity9_decimal_strings':nine(s),'stable9':True,'private600':True,'nlink1':True}
    if expected:
        assert pin['bytes']==expected['bytes'] and pin['sha256']==expected['sha256'] and pin['identity9_decimal_strings']==expected['identity9_decimal_strings'],(path,'pin drift')
    READS.append({'path':path,'bytes':len(raw)})
    assert sum(r['bytes'] for r in READS)<=67108864,'bounded helper read exceeded'
    return raw,pin

def js(path,expected=None):
    raw,pin=read(path,expected);return json.loads(raw),pin

def files(base):
    paths=[];directories=[]
    for d,children,names in os.walk(base,followlinks=False):
        s=os.lstat(d)
        assert stat.S_ISDIR(s.st_mode) and s.st_uid==1000 and stat.S_IMODE(s.st_mode)==0o700,(d,'directory custody')
        directories.append({'path':d,'identity9_decimal_strings':nine(s),'private700':True})
        for name in children:assert not stat.S_ISLNK(os.lstat(d+'/'+name).st_mode)
        for name in names:paths.append(os.path.relpath(d+'/'+name,base))
    return sorted(paths),directories

def patch(items):
    print('*** Begin Patch')
    for path,value in items:
        assert path.startswith(ROOT+'/') or path==RESULT
        raw=value if isinstance(value,bytes) else wire(value)
        assert len(raw)<=16777216
        if os.path.lexists(path):
            assert not path.endswith(('/manifest.json','/seal.json')) and path!=RESULT,'immutable seal/result'
            old,_=read(path)
            print('*** Update File: '+path);print('@@')
            for line in old.decode('utf-8').splitlines():print('-'+line)
            for line in raw.decode('utf-8').splitlines():print('+'+line)
        else:
            print('*** Add File: '+path)
            for line in raw.decode('utf-8').splitlines():print('+'+line)
    print('*** End Patch')
    print('SOL053_METADATA_STATS '+json.dumps({'mode':sys.argv[1:],
        'actual_explicit_read_bytes':sum(r['bytes'] for r in READS),
        'read_events':len(READS),'ru_maxrss_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN','aggregate_RAM':'UNKNOWN_NOT_ZERO_NOT_PROVEN'},sort_keys=True,separators=(',',':')))

def source_delta(relative):
    after,ap=read(ROOT+'/'+relative)
    oldpath=OLD+'/'+relative
    if os.path.exists(oldpath):before,bp=read(oldpath)
    else:before=b'';bp=None
    a=before.splitlines(keepends=True);b=after.splitlines(keepends=True)
    oa=[0];ob=[0]
    for row in a:oa.append(oa[-1]+len(row))
    for row in b:ob.append(ob[-1]+len(row))
    spans=[];rebuilt=bytearray();before_cursor=after_cursor=0
    for tag,ai,aj,bi,bj in difflib.SequenceMatcher(None,a,b,autojunk=False).get_opcodes():
        left=before[oa[ai]:oa[aj]];right=after[ob[bi]:ob[bj]]
        assert oa[ai]==before_cursor and ob[bi]==after_cursor
        span={'kind':tag,'before_byte_range':[oa[ai],oa[aj]],'after_byte_range':[ob[bi],ob[bj]],'before_sha256':sha(left),'after_sha256':sha(right)}
        if tag=='equal':
            assert left==right;span['full_bytes_compared_equal']=True;rebuilt.extend(left)
        else:
            span['complete_before_hex']=left.hex();span['complete_after_hex']=right.hex();rebuilt.extend(bytes.fromhex(span['complete_after_hex']))
        before_cursor=oa[aj];after_cursor=ob[bj];spans.append(span)
    assert before_cursor==len(before) and after_cursor==len(after) and bytes(rebuilt)==after
    result={'schema':'friday.sol053.whole-byte-delta-and-equal-complement.v1','relative_path':relative,'before':bp,'after':ap,
        'status':'NEW_COMPLETE_FILE' if bp is None else 'FULL_BYTES_EQUAL' if before==after else 'CHANGED_FULL_RECONSTRUCTION',
        'before_complete_byte_coverage':len(before),'after_complete_byte_coverage':len(after),'spans':spans,
        'complete_after_reconstructed_equal':True,'syntax_compile_or_execution':False,'GO':False}
    return result

FIX_SPECS=[
('S052-01',['admission.py','common.py'],['def full_snapshot','def parse'],
 'Canonical new Source manifest and separate new canonical consumer enrollment manifest. Original26/consumer70 bytes and old expected SHA remain untouched.',
 'Only new externally enrolled manifest/file pins may be selected; old manifest never silently canonicalized in place.'),
('S052-02',['operations.py','actor_context.py','consumer_bridge.py','fact_bridge.py','root_tool_adapter.py','launcher.py'],['def acquire_material_phase','def refresh_composition','def join_authentication','def selected_phase'],
 'Complete real first12 acquisition precedes dependent operationInput. Independent signed full phase joins fresh raw/class/auth/typed/legacy/closure/presented/page facts and complete original composition, including wheel installed-runtime domain normalization. No observed output is copied into an oracle.',
 'Missing or divergent independent complete phase refuses; producer-specific diagnostic closure bodies and typed SHA must agree.'),
('S052-03',['operations.py','actor_context.py','fact_bridge.py'],['def candidate','def golden','def verify_tree','def join_typed'],
 'Actual candidate/golden source classes emit and retain full typed material observations, then participate in full independently selected phase and original performing/document consumers. Full genuine Git object/tree verification remains.',
 'No Git command or tree verification was run here; selected full object bodies remain future independently provided input.'),
('S052-04',['fact_bridge.py','actor_context.py','consumer_bridge.py','root_tool_adapter.py','launcher.py'],['def validate_final','def consumer_operation_output','def full_binding'],
 'Whole final signed phase joins all actual materials/closures/snapshot/A009 custody, raw/literals/fullpages, all15 exact produced operation bodies, generated/predecessor/member/hierarchy/emitted-manifest/public status bodies, all five final arguments and complete independent golden before final consumer.',
 'Full independent expected/golden/public bodies are required, never fabricated observed kernel/Root/PID facts.'),
('S052-05',['launcher.py','consumer_bridge.py','root_tool_adapter.py'],['def invoke_control','perform-and-retain','two_full_positive'],
 'Fresh final performs exact original control comparison when selected; Root perform-and-retain is constrained to two complete positives while all69 controls use unchanged complete retained-consumer route. All5/all9, complete golden, F0-F11/no-waiver and unsafe NOT_RUN remain.',
 'Controls and two complete positives are not executed or accepted in this Source task.'),
('S052-06',['root_tool_adapter.py','custody.py','lifetime.py','consumer_bridge.py','retention.py'],['def finish','def existing_owner_handoff','def retire_delivery','def retire_lifetime','retention_actual_cleanup','retention_actual_delivery'],
 'Durable terminal contains full actual output pin and complete five-argument/golden binding. Actual ordinary owner close precedes terminal; actual post-terminal close faults and held graph are retained in cleanup tail. Explicit finite same-existing-Root delivery completion closes tail only after native caller durably retains output and drops aliases. Actual retention consumer now requires complete tail and durably retained actual existing-Root completion, exact terminal/tail/output/binding/owner identity and no actual cleanup or IO faults; old terminal-only capsule refuses.',
 'Existing Root integration must call finite retirement and durably retain actual returned completion for independently selected retention. Until then ownership_retired=false; unconfirmed resources stay held.'),
('S052-07',['root_tool_adapter.py','native.py','lifetime.py','observer.py'],['def cleanup_owned_scope','def cleanup_actor','def before_reap'],
 'IO observation failure records original unknown/cause but does not skip finite exact-owned-pidfd signal and bounded terminal confirmation. STOP_UNCONFIRMED retains complete owner graph; no broad kill or fake zero counters.',
 'Failure/stop/close paths have only been read and edited, not fault-injected or executed.'),
('S052-08',['native.py','admission.py','custody.py','lifetime.py','root_tool_adapter.py','observer.py'],['retain_until_terminal','def ownership','def retire_confirmed','def held_lease_graph','signature_tool_dependencies'],
 'Complete same-held tool/dependency/input/mount/pipe/pidfd/capture/reservation graph survives native and Actor unwind while terminal status or FD close is unconfirmed. New external enrollment also supplies complete signature-tool dependencies for bootstrap and selector helpers, held before fork. Same existing Root strong owner installed before fallible handoff serialization; only confirmed exact wait4 permits finite retirement.',
 'Complete STOP graph is intentionally retained, not reported freed; no new daemon, polling or optimistic lease retirement.'),
('S052-09',['root_tool_adapter.py','normalization.py','capacity.py','custody.py'],['def pack_member_batch','def pack_preimage','def actual_operation_streams','def validate_complete_capacity'],
 'Member batch cache uses exact coherent five-field key and complete document/slice refs. Identical complete actual stdout/stderr bodies reuse same held bytes. Whole installed/base/tool/kernel/member/typed/retention/transport/output/read/resident/FD costs are prospectively constrained under original caps before effects.',
 'No sampling, cap increase, representative test or old budget credit. Future actual full inputs and implicit IO may still refuse; declarations are not measurements.'),
('S052-10',['observer.py','root_tool_adapter.py','class_semantics.py','native.py'],['def register','complete_history_requires_full_signed_transport_bundles_before_effect','def authenticate_retained_transport'],
 'Full cumulative process history is preserved under original128 ABI, not512/truncated. Existing full independently signed HTTPS bundle verification is deduplicated only by complete same-held receipt/signature/key identity; every13/106/94 record and original full raw body still consumed. Full helper/history upper is checked before Actor effects.',
 'Future record sets without a legal complete signed batching path or fitting history refuse, not relax the original runtime consumer.'),
('S052-11',['common.py','custody.py','observer.py','extraction.py','class_semantics.py','consumer_bridge.py','launcher.py','actor_bootstrap.py','root_tool_adapter.py','native.py'],['def own_result','def archive_scope','class Decompressed','def json_preflight','consumer_arena','source_size*16','native-returned-invocation-lifetime-before-effect'],
 'Prospective overlapping bytearray+bytes, lexical JSON/encoded strings, actual returned metadata, native invocation/terminal/capture-construction lifetime, xz256MiB/stream/ZIP/ELF/parser state, complete Source code/bundle, Root-to-Actor/control transport and full consumer arena ownership are admitted before allocation and retired only after actual owner/frame/alias lifetime ends.',
 'Absolute inherited/current peak RAM and implicit IO remain UNKNOWN_NOT_ZERO unless independently observed. No allocator/estimate is claimed as runtime proof.')]

def code_sites(names,needles):
    output=[]
    for name in names:
        raw,pin=read(ROOT+'/source/'+name)
        lines=raw.decode('utf-8').splitlines()
        hits=[{'line':i,'text':line} for i,line in enumerate(lines,1) if any(n in line for n in needles)]
        declarations=[{'line':i,'text':line} for i,line in enumerate(lines,1) if line.lstrip().startswith(('def ','class '))]
        output.append({'relative_path':'source/'+name,'pin':pin,'text_only_sites':hits,
            'full_dependency_declarations_when_no_direct_anchor':declarations if not hits else [],
            'review_rule':'COMPLETE_FILE_AND_BYTE_DELTA_REQUIRED_NOT_ONLY_LISTED_LINES'})
    return output

def data_mode():
    received,_=js(ROOT+'/received.json');inv,_=js(REVIEW+'/verified-input-inventory.json')
    originals={};current=[]
    for pin in inv['pins']:
        raw,p=read(pin['path'],pin);originals[pin['path']]=raw;current.append(p)
    for row in inv['directories']:
        assert nine(os.lstat(row['path']))==row['identity9_decimal_strings']
    assert files(OLD)[0]==sorted(inv['source_pathset26']) and files(CONSUMER)[0]==sorted(inv['consumer_pathset70'])
    own_files,_=files(ROOT+'/consumer');assert own_files==sorted(inv['consumer_pathset70'])
    copies=[]
    for rel in own_files:
        raw,p=read(ROOT+'/consumer/'+rel);old=originals[CONSUMER+'/'+rel];assert raw==old
        originals_pin=next(v for v in current if v['path']==CONSUMER+'/'+rel)
        copies.append({'relative_path':rel,'original':originals_pin,'local':p,'full_bytes_compared_equal':True})
    join,jpin=js(REVIEW+'/all20-schema-and-all69-original-join.json')
    catalog,catpin=js(ROOT+'/consumer/controls/catalog.json')
    six=[{k:r[k] for k in ('id','scenario','status','cause','stage','match')} for r in catalog['controls']]
    assert len(six)==69 and six==join['all69']
    schemas=[]
    for row in copies:
        if row['relative_path'].startswith('schemas/'):
            schemas.append({'name':row['relative_path'],'original_pin':row['original'],'local_pin':row['local'],'complete_bytes_equal':True})
    assert len(schemas)==20 and len(join['all15'])==15 and len(join['all27'])==27 and len(join['all5'])==5 and len(join['all9'])==9
    historical,hpin=js(REVIEW+'/all58-byte-delta-and-complement.json')
    assert len(historical['rows58'])==58 and historical['unchanged35']==35 and historical['changed23']==23 and historical['deleted0'] and len(historical['additions2'])==2
    for row in historical['rows58']:
        a=originals[row['before']['path']];b=originals[row['after']['path']]
        assert (a==b)==(row['status']=='FULL_BYTES_EQUAL')
    taskraw,taskpin=read(TASK);assert taskpin['sha256']=='353070c052dc2880c33aa38fac5d351e0fcb8afcf90413c067884f7c44e597f3'
    proof={'schema':'friday.sol053.current-whole-physical-provenance.v1','assignment':ASSIGNMENT,'generation':1,'accepted_msk':ACCEPTED,
        'task_pin':taskpin,'input_pin':received['input_pin'],'pre_task_compact_verified_once':received['compact_pin'],
        'all_original_200_pins_reverified':current,'source26_exact':inv['source_pathset26'],'consumer70_exact':copies,
        'source_original_metadata6':[{'relative_path':p,'disposition':'ORIGINAL_UNTOUCHED_NEW_PACKAGE_METADATA_SUPERSEDES_NOT_ACCEPTANCE_TRANSFER'} for p in inv['source_pathset26'] if not p.startswith(('source/','schemas/'))],
        'candidate_execution':0,'children':0,'GO':False}
    schema={'schema':'friday.sol053.complete-schema69-original-obligations.v1','original_proof_pin':jpin,'local_catalog_pin':catpin,'all20':schemas,
        'all15':join['all15'],'all69_exact_six_field_tuples':six,'all27':join['all27'],'all5':join['all5'],'all9':join['all9'],
        'F0_F11':'ALL_ORIGINAL_REQUIRED_NOT_RUN_NO_WAIVER','F10':join['F10'],'two_full_positives':join['two_full_positives'],
        'author_schema_runtime_validation':False,'all_consumer70_original_bytes_unchanged':True,'Source_role_schema_change_only':'capacity_plan',
        'unsafe_historical_IDs':'INERT_NOT_RUN_REQUIRED_NO_WAIVER','GO':False}
    hist={'schema':'friday.sol053.historical58-current70-continuity.v1','original_proof_pin':hpin,'all58':historical,
        'all_original_preimages_reverified_from_pinned_physical_files':True,'current70_local_full_bytes_equal':True,
        'old_reviews_or_gates_acceptance_transferred':False,'GO':False}
    cmanifest={'schema':'friday.sol053.canonical-consumer-enrollment-manifest.v1','members':[{'path':r['relative_path'],'bytes':r['local']['bytes'],'sha256':r['local']['sha256'],'identity9_decimal_strings':r['local']['identity9_decimal_strings']} for r in copies],
        'original70_unchanged':True,'enrollment_files_count':70,'future_consumer_manifest_selects_this_file_not_original_manifest':'enrollment-manifest.json','GO':False}
    evidence={'event_id':'ASTRA-SOL-E4-SOL053-T1','assignment':ASSIGNMENT,'generation':1,'received':True,'accepted_msk':ACCEPTED,'ref':TASK,'sha256':taskpin['sha256'],'pin':taskpin,'intake':'received.json'}
    patch([(ROOT+'/proofs/current-input-and-consumer-provenance.json',proof),(ROOT+'/proofs/schema20-and-control69-obligations.json',schema),
        (ROOT+'/proofs/historical58-and-current70-continuity.json',hist),(ROOT+'/consumer/enrollment-manifest.json',cmanifest),
        (ROOT+'/native-receipt-evidence.json',evidence),(ROOT+'/proofs/data-helper-read-ledger.json',{'events':READS,'actual_read_bytes':sum(r['bytes'] for r in READS),'helper_ru_maxrss_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'aggregate_RAM':'UNKNOWN_NOT_ZERO_NOT_PROVEN','GO':False})])

def fixes_mode():
    old,oldpin=js(REVIEW+'/findings.json')
    assert [r['id'] for r in old['findings']]==[r[0] for r in FIX_SPECS]
    rows=[]
    for spec,original in zip(FIX_SPECS,old['findings']):
        id,names,needles,change,residual=spec
        rows.append({'id':id,'original_complete_finding':original,'actual_author_change':change,
            'whole_source_and_dependencies':code_sites(names,needles),'remaining_future_evidence':residual,
            'author_disposition':'IMPLEMENTED_PENDING_NEW_DIFFERENT_AUTHOR_WHOLE_REVIEW','runtime':'NOT_RUN','acceptance':False})
    patch([(ROOT+'/fix-matrix.json',{'schema':'friday.sol053.all11-connected-author-fix-matrix.v1','original_findings_pin':oldpin,
        'findings':rows,'count':11,'Source_ready':False,'GO':False,'candidate_execution':0,'new_different_author_whole_review_REQUIRED':True})])

def delta_mode(group):
    paths=sorted(p for p in files(ROOT)[0] if p.startswith(('source/','schemas/')))
    assert len(paths)==22
    chosen=paths[group*6:(group+1)*6]
    patch([(ROOT+'/proofs/source-delta/'+p.replace('/','__')+'.json',source_delta(p)) for p in chosen])

def report_mode():
    source=sorted(p for p in files(ROOT)[0] if p.startswith(('source/','schemas/')))
    rows=[];unified=[]
    for p in source:
        d,dp=js(ROOT+'/proofs/source-delta/'+p.replace('/','__')+'.json');rows.append({'relative_path':p,'status':d['status'],'before':d['before'],'after':d['after'],'delta_and_complement_pin':dp})
        after,_=read(ROOT+'/'+p)
        before=read(OLD+'/'+p)[0] if os.path.exists(OLD+'/'+p) else b''
        unified.extend(difflib.unified_diff(before.decode('utf-8').splitlines(keepends=True),after.decode('utf-8').splitlines(keepends=True),fromfile='A138/'+p,tofile='SOL053/'+p))
    costs={
        'schema':'friday.sol053.actual-and-future-cost-disposition.v1','assignment_budget':{'wall_seconds':6600,'seal_reserve_seconds':600,'cumulative_read_bytes':268435456,'memory_bytes':8589934592,'output_bytes':16777216,'local_workers':4,'model_children':0,'network':0,'retries':0},
        'original_product_caps_UNCHANGED':{'artifacts':350,'members_pages':512,'stream_process_ABI':128,'workers':4,'read_bytes':40960000000,'ram_bytes':8589934592,'output_bytes':33554432,'wall_seconds':4200,'seal_reserve_seconds':600},
        'candidate_execution':0,'Source_runtime_CPU_AS_RAM_IO':'NOT_RUN','children':0,'network':0,'local_workers_used':1,
        'known_explicit_intake_helper_reads':4*6556609,
        'measured_data_helper_read_ledger':'proofs/data-helper-read-ledger.json',
        'other_earlier_helper_and_manual_reads':'COVERED_BY_CONSERVATIVE_RESERVES_NOT_CLAIMED_AS_MEASURED_ZERO',
        'nonmeasured_read_reserves_bytes':{'manual_tool_patch_and_whole_copy':100663296,'implicit_metadata_interpreter_loading':33554432,'remaining_stock_metadata_and_seal':50331648,'native_normal_delivery_and_runtime_helpers':33554432},
        'reserves_are_measurements':False,'aggregate_RAM':'UNKNOWN_NOT_ZERO_NOT_PROVEN','implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN','transport_actual_bytes':'UNMEASURED_NOT_ZERO',
        'assignment_resource_PASS':'NOT_PROVEN_FOR_AGGREGATE_IMPLICIT_IO_RAM','metadata_debug_notes':[
            'Initial whole-materialization patch output exceeded tool display cap and was rejected before any file creation. No partial Source write and no candidate execution or effect retry. Then bounded Source-only materialization and complete consumer70 copy succeeded.',
            'System python3 lacks websockets for read-only native queue inspection; first inspect exited before connection. Verified project venv inspection succeeded once with count0. No candidate/native effect executed, no repeated delivery or duplicate compact.'],
        'all11_cost_lifetime_edges':[
            {'id':'S052-01','edge':'full new Source+consumer canonical wire','pre_effect':'full SHA9/exact pathset, INPUT2M lexical reserve','actual':'NOT_RUN'},
            {'id':'S052-02','edge':'complete fresh original composition/class/raw/auth/legacy/fullwheel','pre_effect':'full consumer arena, complete phase signature/raw/page equality before operationInput','actual':'NOT_RUN'},
            {'id':'S052-03','edge':'candidate/golden complete selected tree/body/typed copies','pre_effect':'installed+base+typed+packed retention output and actual Held read credits','actual':'NOT_RUN'},
            {'id':'S052-04','edge':'full five args/generated/public/emitted manifest/golden','pre_effect':'lexical parsed returned object and encoded copies ownership, independent selection','actual':'NOT_RUN'},
            {'id':'S052-05','edge':'all69 exact original tuple and full positives','pre_effect':'mode constrained, all original consumers unchanged','actual':'NOT_RUN'},
            {'id':'S052-06','edge':'actual terminal2M+tail2M+stdout/stderr2M each+sameowner final close','pre_effect':'terminal12,004,096 output/16M read+RAM, preheld FDs, mandatory finite caller completion','actual':'NOT_RUN'},
            {'id':'S052-07','edge':'unknownIO beforeownedpidfd finite stop/reap','pre_effect':'preserve actual kernel/raw IO error and known exact pidfd ownership','actual':'NOT_RUN'},
            {'id':'S052-08','edge':'STOP exec/dependency/input/mount/pipe/buffer/FD/reservation graph','pre_effect':'hold beforefork; no release until actual wait4 and confirmed close; durable sameRoot retained graph','actual':'NOT_RUN'},
            {'id':'S052-09','edge':'whole memberbank/typedbank/slices/streams/transport','pre_effect':'same5tuple key; complete identical stream equality; 13 output components + implicit <=33,554,432; full base reread lower bound=basebytes*regular_count*228<=40,960,000,000; no sampling','actual':'NOT_RUN'},
            {'id':'S052-10','edge':'complete lifetime processhistory/selectors/signature batches','pre_effect':'full actual registration <=128, complete helper upper and legal full signed receipt identity dedup; all bodies still consumed','actual':'NOT_RUN'},
            {'id':'S052-11','edge':'overlapping body/JSON/string/code/codec/parsed-return and consumer arena','pre_effect':'Held2size+65k; preparse lexical object upper; Source16x+bundle/control arenas; decoder256MiB+32*bound; defer parser frame and owned returned metadata to true lifetime','actual':'NOT_RUN'}],
        'remaining_full_future_proofs':[
            'New different-author independent whole code/cost/lifetime/control review of these exact Source21+schema1 and complete unchanged consumer70, not author acceptance.',
            'Current independently signed Root/enrollment/read-only image/tool/dependency/cgroup/complete admissions including newly required signature_tool_dependencies, canonical manifests, full independent expected bodies and all69 goldens/two complete positives; no old Root or oracle credit.',
            'Authoritative Node signed checksum, full browser pins and genuine UnRAR publisher proof remain genuine absent future facts; never invented.',
            'Complete independent Source phase/final selections including current raw/auth/typed/legacy closures and generated member plans; no output copied into an independent oracle.',
            'Actual native caller same-existing-Root finite retirement must persist returned actual post-tail-close completion. Retention capsule terminals now require cleanup_tail_pin and delivery_completion_pin and consume their full actual identities/faults/output/binding. Old terminal-only capsule refuses; without completion delivery lease stays held, not claimed clean.',
            'Absolute inherited/final RAM and implicit IO measurement, known and actual full retention/transport/page/process/consumer cumulative cost under unchanged caps. Nonfitting complete input refuses; no estimate substitutes for measurement.'],
        'no_capacity_increase':True,'no_sampling_or_test_waiver':True,'GO':False}
    readme=(
        'SOL053 — connected author Source repair, NOT acceptance or runtime proof.\n'
        'All11 original SOL052 causes are mapped to actual complete changed code and dependencies in fix-matrix.json.\n'
        'source/ contains21 modules; schemas/ contains the changed Source role schema. consumer/ retains all70 original files byte-for-byte.\n'
        'Select consumer/enrollment-manifest.json for future external enrollment: it is new canonical wire; original consumer/manifest.json remains unchanged.\n'
        'manifest.json is new canonical Source enrollment wire and includes all owned files except itself, including seal.json.\n'
        'seal.json pins the complete fixed set excluding manifest.json and itself; manifest pins seal. No cryptographic self-cycle.\n'
        'Original Source26 metadata is accounted as superseded only in a new package, never rewritten or acceptance-transferred.\n'
        'All22 Source/schema before-after complete interval deltas and equal complements are under proofs/source-delta/.\n'
        'Full historical58/current70, schema20, six-field69/all27/all5/all9/two positives/F0-F11 remain exact and required.\n'
        'No import/AST/compile/eval/exec/test/native/Root/GPG/archive parsing/extraction/install/Git/network/live/gate/fixture reproduction was performed.\n'
        'intake053.py and seal053.py are newly authored trusted bounded stock metadata helpers only, not product Source; never load Source or old helpers.\n'
        'S052-06 existing Root caller MUST durably retain terminal+actual cleanup tail, drop aliases, invoke retire_existing_root_delivery once and durably retain actual returned completion.\n'
        'New external enrollment requires signature_tool_dependencies for bootstrap and selector helpers; no old enrollment is silently reused.\n'
        'Retention capsule terminal rows require pin,producer_id,cleanup_tail_pin,delivery_completion_pin; all actual full bodies and same-owner/output/fault joins are consumed.\n'
        'A returned intent to exit is not confirmed cleanup; STOP_UNCONFIRMED retains the whole same-owner graph.\n'
        'Complete future authorities, bodies/goldens, actual IO/RAM and fitting budgets are absent/unproven. GO=false, Source_ready=false.\n'
        'New different-author WHOLE review and all future required actual gates remain mandatory.\n'
    ).encode('utf-8')
    closure={'schema':'friday.sol053.whole-author-closure-NOT-acceptance.v1','assignment':ASSIGNMENT,'generation':1,'accepted_msk':ACCEPTED,
        'all11':'ACTUAL_CODE_IMPLEMENTED_PENDING_NEW_DIFFERENT_AUTHOR_WHOLE_REVIEW','Source21_and_role_schema1':rows,
        'consumer70':'FULL_BYTES_EQUAL','historical58':'FULL_ORIGINAL_PREIMAGES_REVERIFIED','schema20':'EXACT_UNCHANGED','all69':'EXACT_SIX_FIELD_TUPLES',
        'all15_ABI16_BODY11':'ORIGINAL_REQUIRED','BODY11_excludes_outer_expected_ref':True,'all27_all5_all9_two_full_positives_F0_F11':'NO_WAIVER_REQUIRED_NOT_RUN',
        'runtime':'NOT_RUN','gates':'NOT_RUN','candidate_execution':0,'new_different_author_whole_review_REQUIRED':True,
        'current_Root_grant':False,'effects_granted':False,'author_acceptance':False,'Source_ready':False,'GO':False}
    patch([(ROOT+'/source-delta-index.json',{'rows':rows,'count':22,'complete_before_after_and_equal_complement':True,'GO':False}),
        (ROOT+'/complete-unified.diff',''.join(unified).encode('utf-8')),(ROOT+'/cost-matrix.json',costs),(ROOT+'/README.txt',readme),(ROOT+'/closure.json',closure)])

def audit_mode():
    paths,dirs=files(ROOT)
    source=[p for p in paths if p.startswith(('source/','schemas/'))]
    assert len(source)==22 and sum(p.endswith('.py') for p in source)==21
    delta,index_pin=js(ROOT+'/source-delta-index.json');assert {r['relative_path'] for r in delta['rows']}==set(source)
    validations=[]
    for row in delta['rows']:
        d,dp=js(row['delta_and_complement_pin']['path'],row['delta_and_complement_pin'])
        after,ap=read(ROOT+'/'+row['relative_path'],row['after'])
        before=read(d['before']['path'],d['before'])[0] if d['before'] else b''
        rebuilt=bytearray();bc=ac=0
        for span in d['spans']:
            x,y=span['before_byte_range'];u,v=span['after_byte_range'];assert x==bc and u==ac
            a,b=before[x:y],after[u:v]
            assert sha(a)==span['before_sha256'] and sha(b)==span['after_sha256']
            if span['kind']=='equal':assert a==b;rebuilt.extend(a)
            else:assert bytes.fromhex(span['complete_before_hex'])==a and bytes.fromhex(span['complete_after_hex'])==b;rebuilt.extend(b)
            bc=y;ac=v
        assert bc==len(before) and ac==len(after) and bytes(rebuilt)==after
        validations.append({'relative_path':row['relative_path'],'after_pin':ap,'full_intervals_and_equal_complement_verified':True})
    matrix,matrix_pin=js(ROOT+'/fix-matrix.json');assert len(matrix['findings'])==11
    for f in matrix['findings']:
        for site in f['whole_source_and_dependencies']:read(site['pin']['path'],site['pin'])
    catalog,_=js(ROOT+'/consumer/controls/catalog.json');raw,_=read(ROOT+'/consumer/controls/catalog.json');assert raw==wire(catalog)
    cmanifest,cmp=js(ROOT+'/consumer/enrollment-manifest.json');raw,_=read(cmp['path']);assert raw==wire(cmanifest)
    roles,_=js(ROOT+'/schemas/friday.a138.roles.v1.json');raw,_=read(ROOT+'/schemas/friday.a138.roles.v1.json');assert raw==wire(roles)
    report={'schema':'friday.sol053.stock-byte-and-wire-audit-NOT-source-execution.v1','source_delta_index_pin':index_pin,'fix_matrix_pin':matrix_pin,
        'Source21_role_schema1':validations,'all11_current_code_pins_verified':True,'consumer_catalog_and_new_manifest_wire_canonical':True,
        'role_schema_wire_canonical':True,'actual_owned_files_before_seal':len(paths),'actual_artifact_bytes_before_seal':sum(os.lstat(ROOT+'/'+p).st_size for p in paths),
        'read_events':READS,'actual_explicit_read_bytes':sum(r['bytes'] for r in READS),'helper_ru_maxrss_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'metadata_only':True,'source_import_AST_compile_eval_exec_tests':False,'author_acceptance':False,'Source_ready':False,'GO':False}
    patch([(ROOT+'/proofs/stock-byte-wire-and-current-fix-audit.json',report)])

def seal_mode():
    paths,dirs=files(ROOT);assert 'manifest.json' not in paths and 'seal.json' not in paths
    members=[]
    for relative in paths:
        _,p=read(ROOT+'/'+relative);p['relative_path']=relative;members.append(p)
    assert len(paths)<349 and sum(r['bytes'] for r in members)<16777216
    seal={'schema':'friday.sol053.acyclic-full-fixed-members-seal.v1','assignment':ASSIGNMENT,'generation':1,'fixed_pathset':paths,'members':members,
        'excludes':['seal.json','manifest.json'],'future_manifest_must_include_this_full_seal':True,
        'directory_policy':{'uid':1000,'mode':'0700','symlinks':False,'final_directory_identity9_observed_in_protected_RESULT':True},
        'actual_fixed_artifact_bytes':sum(r['bytes'] for r in members),'metadata_helper_actual_read_bytes':sum(r['bytes'] for r in READS),
        'helper_ru_maxrss_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'aggregate_RAM':'UNKNOWN_NOT_ZERO_NOT_PROVEN',
        'candidate_execution':0,'author_acceptance':False,'Source_ready':False,'GO':False}
    patch([(ROOT+'/seal.json',seal)])

def manifest_mode():
    paths,_=files(ROOT);assert 'seal.json' in paths and 'manifest.json' not in paths
    members=[]
    for rel in paths:
        _,p=read(ROOT+'/'+rel)
        members.append({'path':rel,'bytes':p['bytes'],'sha256':p['sha256'],'identity9_decimal_strings':p['identity9_decimal_strings']})
    assert len(members)<350
    patch([(ROOT+'/manifest.json',{'schema':'friday.sol053.canonical-source-enrollment-manifest.v1','assignment':ASSIGNMENT,'generation':1,'members':members,
        'new_author_package':True,'old_Source_and_consumer_bytes_immutable':True,'new_different_author_whole_review_REQUIRED':True,'Source_ready':False,'GO':False})])

def final_mode():
    manifest,mp=js(ROOT+'/manifest.json');assert read(ROOT+'/manifest.json')[0]==wire(manifest)
    seal,sp=js(ROOT+'/seal.json');paths,dirs=files(ROOT)
    assert sorted(r['path'] for r in manifest['members'])==sorted(p for p in paths if p!='manifest.json')
    for row in manifest['members']:
        expected={'bytes':row['bytes'],'sha256':row['sha256'],'identity9_decimal_strings':row['identity9_decimal_strings']}
        read(ROOT+'/'+row['path'],expected)
    assert sorted(seal['fixed_pathset'])==sorted(p for p in paths if p not in ('manifest.json','seal.json'))
    for p in seal['members']:read(p['path'],p)
    cm,cp=js(ROOT+'/consumer/enrollment-manifest.json');assert read(ROOT+'/consumer/enrollment-manifest.json')[0]==wire(cm)
    cpaths,_=files(ROOT+'/consumer');assert sorted(r['path'] for r in cm['members'])==sorted(p for p in cpaths if p!='enrollment-manifest.json') and len(cm['members'])==70
    for row in cm['members']:
        raw,p=read(ROOT+'/consumer/'+row['path']);assert p['sha256']==row['sha256'] and p['bytes']==row['bytes'] and p['identity9_decimal_strings']==row['identity9_decimal_strings']
        assert raw==read(CONSUMER+'/'+row['path'])[0]
    original,_=js(REVIEW+'/verified-input-inventory.json')
    for pin in original['pins']:read(pin['path'],pin)
    for row in original['directories']:assert nine(os.lstat(row['path']))==row['identity9_decimal_strings']
    total=sum(os.lstat(ROOT+'/'+p).st_size for p in paths)
    assert total<16777216 and len(paths)<=350 and mp['bytes']<=2000000 and cp['bytes']<=2000000
    now=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    elapsed=int((now-datetime.datetime.fromisoformat(ACCEPTED)).total_seconds());assert 0<=elapsed<=6600
    result={'schema':'friday.sol053.author-source-result.v1','assignment':ASSIGNMENT,'generation':1,'accepted_msk':ACCEPTED,'completed_msk':now.isoformat(timespec='seconds'),
        'elapsed_seconds':elapsed,'status':'SOURCE_REPAIR_IMPLEMENTED_PENDING_INDEPENDENT_WHOLE_REVIEW','source_root':ROOT,
        'manifest_sha256':mp['sha256'],'seal_sha256':sp['sha256'],'fix_matrix':'fix-matrix.json','all11':'CODE_IMPLEMENTED_AUTHOR_NOT_ACCEPTANCE',
        'consumer70_schema20_controls69':'FULL_BYTES_AND_EXACT_TUPLES_PRESERVED','source_modules':21,'all15_ABI16_BODY11_F0_F11':'REQUIRED_UNCHANGED_NOT_RUN',
        'metadata_only_seal_verified':True,'new_different_author_whole_review_REQUIRED':True,'candidate_execution':0,'model_children':0,'network':0,
        'artifact_bytes':total,'final_directory_identity9':nine(os.lstat(ROOT)),
        'this_final_helper_actual_read_bytes':sum(r['bytes'] for r in READS),'aggregate_RAM_implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN',
        'runtime':'NOT_RUN','gates':'NOT_RUN','current_Root_grant':False,'Source_ready':False,'GO':False}
    assert len(wire(result))<2000
    patch([(RESULT,result)])

if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='data':data_mode()
    elif mode=='fixes':fixes_mode()
    elif mode=='delta':delta_mode(int(sys.argv[2]))
    elif mode=='report':report_mode()
    elif mode=='audit':audit_mode()
    elif mode=='seal':seal_mode()
    elif mode=='manifest':manifest_mode()
    elif mode=='final':final_mode()
    else:raise RuntimeError('mode')
