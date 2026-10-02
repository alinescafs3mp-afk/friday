"""New bounded stock metadata authoring only; never imports/executes Source."""
import difflib
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

ROOT=Path('/var/tmp/friday-sol056-sol055-whole54-all32-remaining-connected-source-implementation')
OLD=Path('/var/tmp/friday-sol055-a147-a155-whole54-all32-connected-source-repair')
ASSIGNMENT='ASTRA-E4-SOL056-SOL055-WHOLE54-ALL32-REMAINING-CONNECTED-SOURCE-IMPLEMENTATION'
READS=[]
CACHE={}
def nine(s):
    return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read(path,pin=None):
    path=Path(path)
    key=str(path)
    if key not in CACHE:
        a=path.lstat()
        assert stat.S_ISREG(a.st_mode) and a.st_nlink==1
        with path.open('rb') as f:
            b=os.fstat(f.fileno());raw=f.read();c=os.fstat(f.fileno())
        assert nine(a)==nine(b)==nine(c)==nine(path.lstat())
        p={'path':key,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
           'identity9_decimal_strings':nine(c),'stable9':True}
        CACHE[key]=raw,p
        READS.append(p)
    raw,p=CACHE[key]
    assert nine(path.lstat())==p['identity9_decimal_strings']
    if pin:
        for k in ('bytes','sha256','identity9_decimal_strings'):
            if k in pin:assert p[k]==pin[k],(key,k)
    return raw,p
def patch(path,raw):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    text=raw.decode('utf-8')
    assert not text or text.endswith('\n'),str(path)
    if path.exists():
        old,_=read(path)
        if raw==old:return
        body='*** Update File: '+str(path)+'\n@@\n'
        body+=''.join('-'+x+'\n' for x in old.decode().splitlines())
    else:body='*** Add File: '+str(path)+'\n'
    body+=''.join('+'+x+'\n' for x in text.splitlines())
    r=subprocess.run(['apply_patch'],input=('*** Begin Patch\n'+body+'*** End Patch\n').encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    assert r.returncode==0,r.stderr.decode()
    os.chmod(path,0o600)
    CACHE.pop(str(path),None)
def put(name,obj):
    patch(ROOT/name,(json.dumps(obj,sort_keys=True,ensure_ascii=True,indent=2)+'\n').encode())
def intake():
    os.chmod(ROOT,0o700);os.chmod(ROOT/'metadata.py',0o600)
    inp,ip=read('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL056-INPUT-20261002.json',{'sha256':'7bcca525493ca9f8b87fcc4183165ba5902bc9d97a612ad40817b4771ad1b925'})
    task,tp=read('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL056-TASK.txt',{'sha256':'380c195f0121f9ac58142845a3316f93403e8885c6f704d3e689fdcb7336a1a1'})
    value=json.loads(inp)
    compact,cp=read(value['pre_task_compact']['pin']['path'],value['pre_task_compact']['pin'])
    c=json.loads(compact)
    assert c['assignment']==ASSIGNMENT and c['generation']==1 and c['same_tui'] is True
    assert c['thread_id']=='01a0d516-a446-7131-a123-faeea4bb8e8c'
    assert c['completion']['status']=='completed' and c['completion']['item_type']=='contextCompaction' and c['completion']['error'] is None
    accepted=int(time.time())
    for family in ('Source','independent_review'):
        for k in ('terminal_pin','manifest_pin','Root_received'):
            p=value[family][k]
            if isinstance(p,dict):read(p['path'],p)
    read(value['Source']['seal_pin']['path'],value['Source']['seal_pin'])
    manifest=json.loads(read(OLD/'manifest.json')[0])
    pins={p['path']:p for p in manifest['members']}
    original_received=json.loads(read(OLD/'received.json',pins[str(OLD/'received.json')])[0])
    for p in original_received['reads']:
        assert nine(Path(p['path']).lstat())==p['identity9_decimal_strings'],p['path']
    index=json.loads(read(OLD/'index/full-source.json',pins[str(OLD/'index/full-source.json')])[0])
    materialized=[]
    for row in index['members']:
        relative='Source54/'+row['path']
        raw,p=read(OLD/relative,pins[str(OLD/relative)])
        patch(ROOT/relative,raw)
        materialized.append({'relative':relative,'original':p})
    for name in ('caller','controller','native_grammar','ordinary_root_consumers','root_native_launcher','source_grammar','staged_sender'):
        relative='source/'+name+'.py'
        raw,p=read(OLD/relative,pins[str(OLD/relative)])
        patch(ROOT/relative,raw);materialized.append({'relative':relative,'original':p})
    metadata={}
    for p in manifest['members']:
        relative=str(Path(p['path']).relative_to(OLD))
        if relative.startswith(('inherited-review/','inherited-contracts/')) or relative=='RESIDUAL.txt':
            raw,_=read(p['path'],p)
            dest=relative if relative!='RESIDUAL.txt' else 'prior-residual.txt'
            patch(ROOT/dest,raw)
            if relative.endswith('.json'):metadata[relative]=json.loads(raw)
    assert len(materialized)==61
    put('received.json',{'assignment':ASSIGNMENT,'generation':1,'accepted_epoch':accepted,
        'accepted_MSK':time.strftime('%Y-%m-%d %H:%M:%S MSK',time.localtime(accepted)),
        'wall_end_epoch':accepted+6600,'freeze_end_epoch':accepted+6000,'task':tp,'input':ip,
        'compact':cp,'compact_completed_verified':True,'materialized':materialized,
        'reused_original_read_proofs':original_received['reads'],
        'reused_unchanged_fullSHA9_under_current_writefence':True,
        'intake_reads':READS,'intake_explicit_bytes':sum(p['bytes'] for p in READS),
        'model_children':0,'SourceReady':False,'GO':False})
    print(json.dumps({'accepted_epoch':accepted,'materialized':len(materialized),
        'read_bytes':sum(p['bytes'] for p in READS),'full_metadata_loaded':list(metadata)}))
def method_bodies():
    sender=read(ROOT/'source/staged_sender.py')[0].decode().splitlines(True)
    caller=read(ROOT/'source/caller.py')[0].decode().splitlines(True)
    grammar=read(ROOT/'source/source_grammar.py')[0].decode()
    assert 'SOL056_METHOD_BODIES_BEGIN' not in grammar
    rows=json.loads(read(ROOT/'inherited-contracts/ordered-predicate-table.json')[0])['rows']
    start=next(i for i,l in enumerate(sender) if l.startswith('class Clock:'))
    stop=next(i for i,l in enumerate(sender) if l.startswith('def controller_limits('))
    reportstop=next(i for i,l in enumerate(sender) if l.startswith('class Session:'))
    sessionstop=next(i for i,l in enumerate(sender) if l.startswith('def perform_staged_send('))
    acquirestart=next(i for i,l in enumerate(caller) if l.startswith('class AcquisitionOwner:'))
    acquirestop=next(i for i,l in enumerate(caller) if l.startswith('class FullEventJournal:'))
    # All effectful class bodies, not an advisory list of predicate booleans.
    # Runtime consumer installs strict observed-result-only APIs before replay.
    selected=[(caller[acquirestart:acquirestop],None),
        (sender[start:stop],start),(sender[reportstop:sessionstop],reportstop)]
    blocks='\n# SOL056_METHOD_BODIES_BEGIN: inert author text, strict replay APIs only.\n'
    maps={};preservation=[]
    for lines,original_start in selected:
        new_start=len((grammar+blocks).splitlines())
        blocks+=''.join(lines)+'\n'
        if original_start is not None:
            for row in rows:
                if original_start+1<=row['line_start']<=original_start+len(lines):
                    for n in range(row['line_start'],row['line_end']+1):
                        maps[new_start+n-original_start]=row['id']
                    preservation.append({'id':row['id'],'sender_lines':[row['line_start'],row['line_end']],
                        'replay_lines':[new_start+row['line_start']-original_start,new_start+row['line_end']-original_start],
                        'literal_effectful_expression_preserved':True})
    blocks+='PREDICATE_SITE_MAP.update('+repr(maps)+')\n'
    patch(ROOT/'source/source_grammar.py',(grammar+blocks).encode())
    put('effectful-method-body-correspondence.json',{'method_classes':['Clock','Named','Directory','Generation','OwnScope','Held','IO','Session'],
        'AcquisitionOwner_body':True,'ordered_method_predicates':preservation,
        'full_class_body_exact_text_from_pinned_sender':True,'no_execution':True,
        'full_phase_replay_completed':False,'GO':False})
    put('method-body-read-ledger.json',{'reads':READS,'explicit_bytes':sum(p['bytes'] for p in READS)})
    print(json.dumps({'effectful_predicates':len(preservation),'grammar_bytes':len(grammar+blocks)}))
def finalize():
    received=json.loads(read(ROOT/'received.json')[0])
    assert time.time()<received['freeze_end_epoch']
    index=json.loads(read(OLD/'index/full-source.json')[0]);members=[]
    for row in index['members']:
        raw,p=read(ROOT/'Source54'/row['path'])
        members.append({**p,'path':row['path'],'source_path':str(ROOT/'Source54'/row['path']),
            'mode':'600','uid':1000,'gid':1000,'nlink':1})
    current={**index,'schema':'friday.sol056.whole54-current-index.v1','assignment':ASSIGNMENT,
        'root':str(ROOT),'members':members,'mandatory_source_hashes':{m['path']:m['sha256'] for m in members},
        'Source_ready':False,'GO':False,'Root_grant':False,'runtime':'NOT_RUN'}
    put('index/full-source.json',current)
    pin=read(ROOT/'index/full-source.json')[1]
    old_sha=hashlib.sha256(read(OLD/'index/full-source.json')[0]).hexdigest()
    for name in ('controller','staged_sender','source_grammar'):
        path=ROOT/'source'/f'{name}.py';raw=read(path)[0]
        revised=raw.decode().replace(str(OLD),str(ROOT)).replace(old_sha,pin['sha256'])
        revised='\n'.join('INDEX_SHA = '+json.dumps(pin['sha256']) if line.startswith('INDEX_SHA = ') else line
            for line in revised.splitlines())+'\n'
        patch(path,revised.encode())
    # Literal text/hash comparisons only, never Source import/AST/compile/test.
    previous=json.loads(read(OLD/'frozen132-pure16-preservation.json')[0])
    predicates=json.loads(read(ROOT/'inherited-contracts/ordered-predicate-table.json')[0])['rows']
    sender=read(ROOT/'source/staged_sender.py')[0].decode().splitlines()
    oldsender=read(OLD/'source/staged_sender.py')[0].decode().splitlines()
    grammar=read(ROOT/'source/source_grammar.py')[0].decode().splitlines()
    oldgrammar=read(OLD/'source/source_grammar.py')[0].decode().splitlines()
    pure=[]
    for row in previous['pure']:
        a,b=row['sender_lines'];c,d=row['grammar_lines']
        assert sender[a-1:b]==oldsender[a-1:b],row['function']
        assert grammar[c-1:d]==oldgrammar[c-1:d],row['function']
        assert ''.join(''.join(sender[a-1:b]).split())==''.join(''.join(grammar[c-1:d]).split())
        pure.append({**row,'current_same_literal':True})
    frozen=[]
    for row in predicates:
        a,b=row['line_start'],row['line_end']
        text='\n'.join(sender[a-1:b])
        assert text=='\n'.join(oldsender[a-1:b]),row['id']
        assert ''.join(text.split())==''.join(row['ordered_expression'].split())
        frozen.append({'id':row['id'],'sender_lines':[a,b],'current_same_literal':True})
    assert len(pure)==16 and len(frozen)==132
    bodies=json.loads(read(ROOT/'effectful-method-body-correspondence.json')[0])
    for row in bodies['ordered_method_predicates']:
        a,b=row['sender_lines'];c,d=row['replay_lines']
        assert sender[a-1:b]==grammar[c-1:d],row['id']
    put('frozen132-pure16-current.json',{'pure':pure,'predicates':frozen,
        'effectful_body_predicates':len(bodies['ordered_method_predicates']),
        'scope':'literal textual preservation only, not semantic independent acceptance','GO':False})
    rows=[];diff=[]
    for row in received['materialized']:
        old,op=read(row['original']['path'],row['original'])
        new,np=read(ROOT/row['relative'])
        changed=old!=new;parts=[]
        for offset in range(0,max(len(old),len(new)),65536):
            a,b=old[offset:offset+65536],new[offset:offset+65536]
            parts.append({'offset':offset,'old_bytes':len(a),'new_bytes':len(b),
                'old_sha256':hashlib.sha256(a).hexdigest(),'new_sha256':hashlib.sha256(b).hexdigest(),'equal':a==b})
        rows.append({'relative':row['relative'],'original':op,'current':np,'changed':changed,'full_partitions':parts})
        if changed:diff.extend(difflib.unified_diff(old.decode().splitlines(True),new.decode().splitlines(True),
            fromfile=op['path'],tofile=np['path']))
    patch(ROOT/'source-changes.patch',''.join(diff).encode())
    full54=[r for r in rows if r['relative'].startswith('Source54/')]
    actors=[r for r in rows if r['relative'].startswith('source/')]
    assert len(full54)==54 and len(actors)==7
    caller_bytes=len(read(ROOT/'source/caller.py')[0]);sender_bytes=len(read(ROOT/'source/staged_sender.py')[0])
    assert caller_bytes<=65536 and sender_bytes<=65536
    put('full-byte-delta.json',{'rows':rows,'Source54':54,'Source54_changed':[r['relative'] for r in full54 if r['changed']],
        'Source54_equal_complement':[r['relative'] for r in full54 if not r['changed']],
        'actors_changed':[r['relative'] for r in actors if r['changed']],
        'full_complement_exact':True,'SourceReady':False,'GO':False})
    for p in received['reused_original_read_proofs']:
        assert nine(Path(p['path']).lstat())==p['identity9_decimal_strings'],p['path']
    put('original-input-current9.json',{'pins':received['reused_original_read_proofs'],
        'unchanged_full_SHA_proofs_reused_from_verified_SOL055':True,'all_current9_equal':True,'GO':False})
    total=sum(m['bytes'] for m in members)
    history=[]
    if (ROOT/'author-final-code-read-ledger.json').exists():
        prior=json.loads(read(ROOT/'author-final-code-read-ledger.json')[0])
        history=prior.get('prior_finalize_passes',[])+[{'explicit_bytes':prior['explicit_bytes'],
            'index_sha256':prior.get('index_sha256'),'reason':'actual changed-byte/dependency/current-index refresh; not Source runtime retry'}]
    put('author-final-code-read-ledger.json',{'reads':READS,'explicit_bytes':sum(p['bytes'] for p in READS),
        'index_sha256':pin['sha256'],'prior_finalize_passes':history,
        'caller_bytes':caller_bytes,'sender_bytes':sender_bytes,'Source54_bytes':total,
        'Source_three_scan_conditional_bytes':total*3,'Source_three_scan_other_read_margin':16777216-total*3,
        'Source_one_owned_scan_conditional_bytes':total,'Source_one_owned_scan_other_read_margin':16777216-total,
        'conditional_not_allpath_proof':True,'no_Source_execution':True,'GO':False})
    print(json.dumps({'Source54_changed':sum(r['changed'] for r in full54),'actors_changed':sum(r['changed'] for r in actors),
        'Source54_bytes':total,'caller_bytes':caller_bytes,'sender_bytes':sender_bytes,
        'index_sha256':pin['sha256'],'frozen132':len(frozen),'pure16':len(pure),
        'method_body_predicates':len(bodies['ordered_method_predicates']),'explicit_bytes':sum(p['bytes'] for p in READS)}))
def performing_body():
    sender=read(ROOT/'source/staged_sender.py')[0].decode().splitlines(True)
    grammar=read(ROOT/'source/source_grammar.py')[0].decode()
    start=next(i for i,l in enumerate(sender) if l.startswith('def perform_staged_send('))
    rowmeta=json.loads(read(ROOT/'effectful-method-body-correspondence.json')[0])
    predicates=json.loads(read(ROOT/'inherited-contracts/ordered-predicate-table.json')[0])['rows']
    assert '\ndef perform_staged_send(' not in grammar
    newstart=len(grammar.splitlines())
    appended=''.join(sender[start:])+'\n'
    maps={}
    for row in predicates:
        if row['line_start']>start:
            for n in range(row['line_start'],row['line_end']+1):maps[newstart+n-start]=row['id']
            rowmeta['ordered_method_predicates'].append({'id':row['id'],
                'sender_lines':[row['line_start'],row['line_end']],
                'replay_lines':[newstart+row['line_start']-start,newstart+row['line_end']-start],
                'literal_effectful_expression_preserved':True})
    appended+='PREDICATE_SITE_MAP.update('+repr(maps)+')\n'
    patch(ROOT/'source/source_grammar.py',(grammar+appended).encode())
    rowmeta['perform_staged_send_body_and_actual_wrapper']=True
    put('effectful-method-body-correspondence.json',rowmeta)
    put('performing-body-read-ledger.json',{'reads':READS,'explicit_bytes':sum(p['bytes'] for p in READS)})
    print(json.dumps({'full_effectful_body_predicates':len(rowmeta['ordered_method_predicates'])}))
def package():
    received=json.loads(read(ROOT/'received.json')[0]);delta=json.loads(read(ROOT/'full-byte-delta.json')[0])
    index=json.loads(read(ROOT/'index/full-source.json')[0]);code=json.loads(read(ROOT/'author-final-code-read-ledger.json')[0])
    assert time.time()<received['freeze_end_epoch']
    for row in delta['rows']:
        assert nine(Path(row['current']['path']).lstat())==row['current']['identity9_decimal_strings']
        assert read(row['current']['path'],row['current'])[1]['sha256']==row['current']['sha256']
    statuses={
        'A144-F01':('PRESERVED_STATIC_CAUSE_CURRENT_BINDINGS_CHECKED','index/controller/Sender/pure+method grammar coherent'),
        'A144-F02':('ATOMIC_RAW_READ_AND_OWNED_REUSE_ATTEMPT_OPEN','startup/stock/error/native physical completeness unimplemented'),
        'A144-F03':('RAW_EXCEPTION_LINK_GRAPH_ATTEMPT_OPEN','raw-before-projection preserved; full native/error capacity still open'),
        'A144-F04':('PRESERVED_LOCAL_REPAIR_CONNECTED_OPEN','one close attempt; wrappers permit cleanup despite recording damage'),
        'A144-F05':('PRESERVED_LOCAL_REPAIR_CONNECTED_OPEN','one attach attempt/raw partial generations retained, outer end still open'),
        'A144-F06':('IMMUTABLE_BYTE_REUSE_AND_RAW_GRAPH_ATTEMPT_OPEN','no complete damage-safe pre-reserved allocator/transport arena'),
        'A144-F07':('CALLED_PREBIRTH_PARENT_REAL_ALIAS_RECEIVER_ATTEMPT_OPEN','direct caller generation is NOT actual outer native helper/tool end'),
        'A144-F08':('CALLED_ADOPTED_INCOMPLETE_PREFIX_CONSUMER_OPEN','complete=false; full outside native durability/capacity still missing'),
        'A144-F09':('ALL_METHOD_CAPTURE_72_BODY_PREDICATES_REPLAY_ATTEMPT_OPEN','opaque Budget/native/error/phase facades and full transitions still open'),
        'A144-F10':('PURPOSE_BOUND_AND_PHYSICAL_PIPE_MERGE_ATTEMPT_OPEN','full32 cuts/generations/negative semantics/cross-channel chronology incomplete'),
        'A144-F11':('CALLED_RAW_LOADER_ERRPIPE_ADAPTERS_ATTEMPT_OPEN','pre-first-startup/extension/recorder/stdlib internals incomplete'),
        'A144-F12':('PRESERVED_STATIC_ORIGINAL_CAUSE_REPAIR','identity9 remains before tuple')}
    prior=json.loads(read(ROOT/'inherited-review/evidence-matrix.json')[0])
    put('closure-matrix.json',{'findings':[{**row,'SOL056_status':statuses[row['id']][0],
        'SOL056_exact_residual':statuses[row['id']][1],'historical_references_not_current_semantic_review':True,
        'current_residual':'RESIDUAL.txt'} for row in prior['findings']],
        'retained_N01_N02_N03_N04_attempts':True,'original_findings':12,
        'whole_completed':False,'independent_changed_byte_review':'REQUIRED_NOT_RUN','GO':False})
    oldcases=json.loads(read(ROOT/'inherited-review/all32-classifications.json')[0])['cases']
    causal=json.loads(read(ROOT/'inherited-review/original32-causal-obligations.json')[0])['rows']
    causals={r['case']:r for r in causal}
    assert len(oldcases)==len(causals)==32
    put('all32-current-matrix.json',{'count':32,'no_case_cut':True,'cases':[
        {**row,'original_full_causal_obligations':causals[row['case']],
         'current_code_attempts':['actual_method_state_and_error_replay','case_firstfault_errno','exact_Session_fd_guard',
            'causal_failed_method_cut_not_event_absence','actual_raw_atomic_pipe_prefix_and_read_merge'],
         'complete_case_causal_closure':False,'runtime':'NOT_RUN','GO':False} for row in oldcases],
        'original_bindings':'inherited-contracts/all32-contract-bindings.json','GO':False})
    put('whole54-current-matrix.json',{'count':54,'members':[
        {**row,'transitive_required':True,'current_semantic_independent_review':'REQUIRED_NOT_RUN',
         'runtime':'NOT_RUN','GO':False} for row in delta['rows'] if row['relative'].startswith('Source54/')],
        'full_original_control_inventory':next(m for m in index['members'] if m['path']=='schemas/control-inventory.v1.json'),
        'original_scope_counts':json.loads(read(ROOT/'inherited-review/control-obligation-counts.json')[0]),
        'fixed63':True,'accepted49':True,'full56':True,'selfcheck96':True,
        'all30_F0F11':'REQUIRED_NOT_RUN','unsafe_history':'ABSTRACTION_ONLY_REQUIRED_NOT_RUN',
        'all_independent_live_final_gates':'REQUIRED_NOT_RUN','GO':False})
    def point(file,needle):
        lines=read(ROOT/file)[0].decode().splitlines()
        found=[n+1 for n,line in enumerate(lines) if needle in line]
        assert found,(file,needle)
        return {'path':str(ROOT/file),'needle':needle,'lines':found}
    interfaces={
        'existing_parent_receiver':[point('source/root_native_launcher.py','def select_before_birth('),
            point('source/root_native_launcher.py','def bind_native_generation('),point('source/root_native_launcher.py','def adopt('),
            point('source/root_native_launcher.py','def outside_wait4(')],
        'prefix':[point('source/root_native_launcher.py','def consume_adopted_incomplete_prefix(')],
        'physical_read':[point('source/controller.py','class SourceReadMeter(_SemanticReadMeter)'),
            point('Source54/tests/receipt_contract.py','class ObservedFileIO('),point('Source54/tests/receipt_contract.py','class ObservedRaw(')],
        'loader_and_internal':[point('Source54/tests/receipt_contract.py','def source_open_code('),
            point('source/caller.py','sender.subprocess.os=')],
        'immutable_bytes':[point('Source54/tests/receipt_contract.py','receipt.Source_generation.bound_reuse'),
            point('source/root_native_launcher.py','self.allowed_byte_pins={}')],
        'methods':[point('source/caller.py','def method_enter('),point('source/caller.py','def predicate_state('),
            point('source/ordinary_root_consumers.py','class ObservedMethodReplay:'),point('source/ordinary_root_consumers.py','def method_sites(')],
        'physical_writer':[point('source/controller.py','pipe_buf=os.fpathconf('),
            point('source/ordinary_root_consumers.py','def physical_pipe_merge('),point('source/native_grammar.py','ordinary_actual_per_channel_physical_writer_proof')],
        'purposes':[point('source/ordinary_root_consumers.py','absence alone is never a causal NOT_REACHED proof'),
            point('source/ordinary_root_consumers.py','NO_INITIATING_ERRNO')]}
    put('consumer-interface-matrix.json',{'interfaces':interfaces,
        'schemas':{'raw_buffer':'buffer_snapshot full kind/bytes/geometry',
            'method_state':'friday.sol056.method-state-graph.v1 complete/opaque explicit',
            'incomplete_prefix':'friday.sol056.accepted-incomplete-prefix.v1 complete=false',
            'segments':'sequence/source-row + pid/physical_sequence/pipe_identity9/pipe_buf/physical_rank',
            'unreached':'actual owned cut_index + method_call + full body/error replay',
            'no_initiating_errno':'reached sender.returned_outcome in original positive/nonzero/artifact-refusal domain'},
        'changed_consumer_schema_requires_independent_review':True,'whole_interface_acceptance':False,'GO':False})
    known=received['intake_explicit_bytes']
    for name in ('method-body-read-ledger.json','performing-body-read-ledger.json'):
        known+=json.loads(read(ROOT/name)[0])['explicit_bytes']
    known+=sum(p['explicit_bytes'] for p in code.get('prior_finalize_passes',[]))+code['explicit_bytes']
    # First finalize was overwritten before history support; exact cost from
    # its completed stock helper output is retained here, not silently dropped.
    known+=11917417
    reserves={'manual_tools_and_repeated_text_reads':67108864,'stock_imports_internal_IO':33554432,
        'apply_patch_internal_and_author_tool_reads':33554432,'seal_and_final_audit':16777216,
        'normal_native_RESULT_handoff':16777216}
    upper=known+sum(p['bytes'] for p in READS)+sum(reserves.values())
    assert upper<268435456,(known,upper)
    put('cost-resource-custody-matrix.json',{'accepted_epoch':received['accepted_epoch'],
        'author_hard_end_epoch':received['wall_end_epoch'],'substantive_freeze_epoch':int(time.time()),
        'known_completed_helper_reads_before_package':known,
        'missing_first_finalize_explicit_bytes_restored':11917417,
        'unmeasured_planned_conservative_reserves_not_observations':reserves,
        'declared_author_read_upper':upper,'author_read_cap':268435456,'author_output_cap':16777216,
        'Source54_bytes':code['Source54_bytes'],'Source_three_scans_conditional_bytes':3*code['Source54_bytes'],
        'Source_three_scans_other_read_margin':16777216-3*code['Source54_bytes'],
        'Source_one_held_scan_conditional_bytes':code['Source54_bytes'],
        'Source_one_held_scan_other_read_margin':16777216-code['Source54_bytes'],
        'whole_Source_implicit_IO':'UNKNOWN_NOT_ZERO','aggregate_RAM':'UNKNOWN_NOT_ZERO',
        'caller_bytes':code['caller_bytes'],'sender_bytes':code['sender_bytes'],
        'literal_caps_each':65536,'native_cap':262144,'Sender_read_cap':33554432,
        'same_controller_receiver_read_cap':16777216,'Source_AS_soft':67108864,'Source_CPU':300,
        'workers':4,'worker_and_dispatcher_AS_each':1610612736,'whole_bill':8589934592,
        'nominal_five_large_AS':8053063680,'plus_three_small_AS':8254390272,
        'conditional_nominal_margin_before_UNKNOWN_overhead':335544320,
        'Root_byte_cache_aliases_exports':'ADDITIONAL_COST_NOT_ZERO_NOT_A_SOURCE_ALLOWANCE',
        'whole_fit':'NOT_PROVEN','original_dual_ends':'UNCHANGED_NO_REFRESH','runtime_retries':0,
        'model_children':0,'network':0,'runtime':'NOT_RUN','gates':'NOT_RUN','GO':False})
    put('package-read-ledger.json',{'reads':READS,'explicit_bytes':sum(p['bytes'] for p in READS)})
    print(json.dumps({'package_reads':sum(p['bytes'] for p in READS),'declared_author_read_upper':upper,
        'whole54':54,'all32':32,'whole_completed':False}))
if __name__=='__main__':
    if sys.argv[1]=='intake':intake()
    if sys.argv[1]=='method-bodies':method_bodies()
    if sys.argv[1]=='finalize':finalize()
    if sys.argv[1]=='performing-body':performing_body()
    if sys.argv[1]=='package':package()
