"""Own stock SHA9/JSON/literal metadata. No supplied imports/AST/compile/exec."""
import os,json,re,hashlib,base64,difflib,time,stat
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
READ=0; ROWS=[]
def nine(s):return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read(p):
    global READ
    p=Path(p);s=p.lstat();assert stat.S_ISREG(s.st_mode) and s.st_nlink==1 and s.st_size<8*1024*1024
    b=p.read_bytes();READ+=len(b);assert nine(s)==nine(p.lstat())
    row={'path':str(p),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'identity9_decimal_strings':nine(s)}
    ROWS.append(row);return b,row
def write(rel,x):
    p=ROOT/rel;p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(p.parent,0o700)
    b=x if isinstance(x,bytes) else (json.dumps(x,indent=2,ensure_ascii=True)+'\n').encode()
    fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as h:h.write(b)
def obj(rel):return json.loads(read(ROOT/rel)[0])
doc=obj('author/input.json'); names=('caller.py','supervisor.py','verifier.py','launch-contract.json','expectations.json','ordinary-surplus.txt')
new={};pins={};delta=[];diff=[]
for n in names:
    b,p=read(ROOT/n);new[n]=b;pins[n]=p
    orig=next(x for x in doc['Source']['current6'] if Path(x['path']).name==n)
    before,row=read(orig['path']);assert row['sha256']==orig['sha256'] and row['identity9_decimal_strings']==orig['identity9_decimal_strings']
    changed=before!=b
    delta.append({'file':n,'changed':changed,'original_pin':orig,'new_pin':p,'full_before_base64':base64.b64encode(before).decode() if changed else None,'full_after_base64':base64.b64encode(b).decode() if changed else None})
    if changed:diff.extend(difflib.unified_diff(before.decode().splitlines(True),b.decode().splitlines(True),fromfile='LAB863/'+n,tofile='SOL060/'+n))
write('author/whole6-full-byte-delta.json',delta);write('author/source-delta.diff',''.join(diff).encode())
c=new['caller.py'].decode();s=new['supervisor.py'].decode();v=new['verifier.py'].decode();contract=json.loads(new['launch-contract.json']);expect=json.loads(new['expectations.json'])
for key,file in (('VHASH','verifier.py'),('CHASH','launch-contract.json'),('SHASH','supervisor.py')):
    assert re.search(key+r' = "([0-9a-f]{64})"',c).group(1)==pins[file]['sha256']
for key,file in (('SOURCE_SHA','verifier.py'),('CONTRACT_SHA','launch-contract.json')):
    assert re.search(key+r' = "([0-9a-f]{64})"',s).group(1)==pins[file]['sha256']
for code,var in ((c,'VHASH'),(s,'SOURCE_SHA')):assert '"/verifier.py", '+str(pins['verifier.py']['bytes'])+', '+var in code
for code,var in ((c,'CHASH'),(s,'CONTRACT_SHA')):assert '"/launch-contract.json", '+str(pins['launch-contract.json']['bytes'])+', '+var in code
assert '"/supervisor.py", '+str(pins['supervisor.py']['bytes'])+', SHASH' in c
assert contract['source']['sha256']==pins['verifier.py']['sha256'] and contract['source']['bytes']==pins['verifier.py']['bytes']
for code in (c,s,v):assert str(ROOT) in code
assert all(x not in v for x in ('UNCERTAIN_FDS','RETIRED_FDS')) and all(x not in s for x in ('CLOSED_NUMBERS','UNCERTAIN_NUMBERS'))
assert all('friday.sol060.error-graph.v2' in x for x in (c,s,v))
assert 'friday.e4.node.bounded-verifier.sol060.v1' in c and 'friday.e4.node.bounded-verifier.sol060.v1' in s and 'friday.e4.node.bounded-verifier.sol060.v1' in v
assert 'friday.e4.node.actual-supervisor.sol060.v1' in c and 'friday.e4.node.actual-supervisor.sol060.v1' in s
assert 'produced_stream_summaries' not in v and 'supervisor-incomplete-refusal.a048.v1' not in s
assert all(x in c for x in ('wire_receipt','recover_wire_receipt','validate_fullraw_layers','pending_chunk','LAUNCH_OWNERS','SELECT_CLOSE'))
assert all(x in v for x in ('CALL_OWNERS','raw_cell','retain_cell','published_cell','output_charge_bytes','raw_base64'))
assert all(x in s for x in ('restore_transport_raw','same-inner-json-preimage','finish_allocations'))
assert all(x in v and x in s for x in ('ACQUIRE_UNKNOWN','RETIRE_UNKNOWN','attempted_once','CONFIRMED_CLOSED'))
original_exp=json.loads(next(read(x['path'])[0] for x in doc['Source']['current6'] if Path(x['path']).name=='expectations.json'))
for key in ('case_order','cases','common_receipt_contract','future_selection','resource_receipt_schemas'):
    assert expect[key]==original_exp[key],key
pub=expect['publication_source_contract'];assert pub['complete_bound_with_newline_bytes']==1669337
write('author/current-source-joins.json',{'schema':'friday.sol060.whole6.text-byte-joins.v1','pins':pins,'changed':sum(x['changed'] for x in delta),'equal':sum(not x['changed'] for x in delta),'acyclic_source_chain':['verifier','contract','supervisor','caller','expectations','external_source_manifest'],'literal_checks_pass':True,'original_case_order_oracles_resources_future_selection_equal':True,'method':'full bytes/SHA9/stock exact integer JSON and literal relations only','syntax_AST_compile_behavior':'NOT_RUN','whole_verdict':'CODE_OPEN','SourceReady':False,'Root_admission':False,'GO':False})
# Whole actor complement: every top-level function/class body and lexical calls,
# including defaults and unchanged native/ownership/resource/custody parts.
complement={}
for n,code in (('caller.py',c),('supervisor.py',s),('verifier.py',v)):
    lines=code.splitlines(True);defs=[(i,re.match(r'^(?:def|class) ([A-Za-z_][A-Za-z_0-9]*)',line).group(1)) for i,line in enumerate(lines) if re.match(r'^(?:def|class) ',line)]
    rows=[]
    for pos,(start,name) in enumerate(defs):
        end=defs[pos+1][0] if pos+1<len(defs) else len(lines)
        body=''.join(lines[start:end]);rows.append({'name':name,'line':start+1,'end_line_exclusive':end+1,'full_text':body,'sha256':hashlib.sha256(body.encode()).hexdigest(),'literal_direct_names':sorted(set(re.findall(r'(?<![.\w])([A-Za-z_]\w*)\(',body))),'NOT_AST_OR_REACHABILITY_PROOF':True})
    complement[n]={'pin':pins[n],'rows':rows,'all_full_file_text_pinned':True}
write('author/whole-actor-full-method-complement.json',complement)
old35=obj('provenance/LAB863/evidence/all35-producer-consumer-matrix.json');assert len(old35['rows'])==35
line_map={r['name']:r['line'] for r in complement['caller.py']['rows']}
mat35=[]
for row in old35['rows']:
    extra=['exact_error','preown_fd','publish_fd','allocate_owned','allocate_pipe','retire_and_close_owned','close_final','wire_receipt','recover_wire_receipt','capture_pipe','drain_pipes','validate_fullraw_layers']
    mat35.append({'original_full_row':row,'slot_preserved':row['slot'],'original_store_preserved':row['store'],'original_roots_current_lines':{r:line_map.get(r) for r in row['roots']},'connected_current_new_methods':{r:line_map.get(r) for r in extra},'primary_secondary':'complete projected graphs/retained original objects; full native reconstruction CODE_OPEN','raw_normal_error_prefix':'preowned chunks and pending returns/full raw preimages; incomplete capture NOT fullraw','resource_end':'original AS/CPU/FD/output/shared900/one cleanup unchanged','domain':'all original900 ASCII/type32/native32/path160/clock32/2byte escapes preserved; NEW graph factory/field/alias/custody proof CODE_OPEN','old_A175_qualification_transferred':False,'new_shape_bound_and_lifetime_cost':'C01/C02/C03/C04/C05 CODE_OPEN','runtime':'NOT_RUN'})
write('author/all35-connected-matrix.json',{'schema':'friday.sol060.original35-connected-full-matrix.v1','rows':mat35,'original35_cardinality':35,'current_source_pins':pins,'new_catch_raw_codec_factories':'whole-method complement, not a narrowed35-only review','all30_F0_F11_gates_live':'MANDATORY_NOT_RUN','unsafe_history':'ABSTRACT_REQUIRED_NOT_RUN_NO_OPERATIONAL_REPRO_WAIVER'})
rows6=[]
for i,name in enumerate(expect['case_order']):
    rows6.append({'case':name,'index':i,'original_expectation':expect['cases'][name] if isinstance(expect['cases'],dict) else expect['cases'][i],'actors':['caller.py','supervisor.py' if i<5 else 'verifier.py','verifier.py' if i<3 else None],'raw_custody':'stdout/stderr inclusive overflow and pending raw kept; canonical full supervisor/inner exact reversible preimages; writable/base64/count/SHA/prodcharge verified bothside','primary_error':'launch_firstfault/call failure original with separate capture/close/reap errors','normal_error_prefix':'uncreated/spawn/timeout/resource/poll/read/decode/settlement/capture/constructor/cleanup/publication included; remainingC01-C05','end':'existing absolute phase/shared900/end cleanup; no refresh','native_receipt_or_gate':'NOT_RUN','CODE_OPEN':['C01','C02','C03','C04','C05']})
write('author/all6-normal-error-raw-custody-matrix.json',{'schema':'friday.sol060.original6-connected.v1','rows':rows6,'original_gpg_calls':6,'first2_no_keyring_only':True,'other4_explicit_rings':True,'public_oracles_unchanged':True,'new_runtime_credit':False})
write('author/all3-F1-F2-matrix.json',{'schema':'friday.sol060.original3-F1-F2-connected.v1','all3':[{'id':'A16901','credit':'typed actual phase resources+telemetry bothside unchanged; error graph extended','future_actual':'NOT_RUN'},{'id':'A16902','credit':'--no-keyring only first2 / explicit original rings other4; authority/rc/fingerprint/signature/body/archive/negative checks unchanged','LAB8614_actual_readonly_agent_trustdb_commonconf':'NOT_RUN'},{'id':'A16903','credit':'caller terminal exact owned proc generation immutable_input9 versus scoped proc retirement token unchanged','historical_RED10_preclose_Refusals':'UNCHANGED; native unconfirmed-close empty; missing field UNKNOWN','future_proc_provider':'NOT_RUN'}],'F1':{'attempt':'allocation tokens/prospective history before effect; RETIRE_UNKNOWN before close; old numeric caches removed; finite knownreturned cleanup','genuine_text_delta':True,'full_bound_partial_constructor_native_retirement':'CODE_OPEN'},'F2':{'attempt':'preowned full raw/writable/base64/produced countcharge/rc and original+secondary errors before close; no hashonly oversized replacement','bothside_receivers':'supervisor.validate_common_inner/caller.validate_fullraw_layers and reversible originalraw codecs','full_allprefix_inner32KiB_and_lifetime':'CODE_OPEN'},'D1':{'GPG_CPU':20,'GPG_wall':30,'firstcall_dynamic_quota_max':21845,'no_executable_cap_increase':True}})
# New Source19 uses historical unchanged dependency declarations, never a fresh
# actual Root materialization/accepted receipt or int9 fd3 execution grant.
a178=json.loads(read(doc['original_exact_A178_input']['path'])[0]);selected=dict(a178['current_selected19_before_new_bytes'])
mapping={'caller':'caller.py','supervisor':'supervisor.py','verifier':'verifier.py','contract':'launch-contract.json','expectations':'expectations.json','plain':'ordinary-surplus.txt'}
for k,n in mapping.items():selected[k]=pins[n]
assert len(selected)==19
sizes={k:row['bytes'] for k,row in selected.items()}
full=sum(sizes.values());sup=sum(sizes[k] for k in ('prlimit','python','supervisor','contract','caller','expectations','plain'));ver=sum(sizes[k] for k in ('prlimit','python','verifier','contract','caller','expectations','plain'))
oldplan=obj('provenance/LAB863/performing-read-budget.json');buckets=dict(oldplan['buckets']);buckets.pop('proc_running')
buckets.update(hash_initial=full+19,hash_terminal=full+19,hash_subsets=10*(sup+7)+2*(ver+7),expectations_body=sizes['expectations']+1)
fixed=sum(buckets.values());running=268435456-fixed;assert running>=94216
buckets['proc_running']=running;assert sum(buckets.values())==268435456
write('author/current19-read-plan.json',{'schema':'friday.sol060.new19-performing-read-arithmetic.v1','selected19_reference_only':selected,'unchanged_dependency_pins':'HISTORICAL_DECLARATIONS_NOT_FRESH_CURRENT_QUALIFICATION','new_source6_SHA9':pins,'future_Root_numeric_int9_schema_unchanged':'friday.e4.node.ordinary-root-selection-data.a082.v1/{schema,pins}19/{path,size,sha256,identity9 int[9]}','sizes':sizes,'full19_bytes':full,'supervisor_subset_bytes':sup,'verifier_subset_bytes':ver,'hash_subsets':buckets['hash_subsets'],'fixed_nonrunning':fixed,'running':running,'buckets':buckets,'sum':sum(buckets.values()),'cap':268435456,'original_cleanup_components':oldplan['cleanup_components'],'GPG_CPU':20,'GPG_wall':30,'dynamic_quota':'floor((65536-TOTAL_OUTPUT)/(3+writable_slots)); <=21845 firstcall','original_caps':oldplan['original_outer_caps'],'original_inner_caps':oldplan['original_inner_caps'],'Source_IO_RAM_CPU_actual':'UNKNOWN_NOT_ZERO_NOT_PROVEN','no_author_budget_transfer':True,'Root_admission':False,'runtime':'NOT_RUN'})
write('author/raw-error-bound-matrix.json',{'schema':'friday.sol060.new-bound-not-prior-carry.v1','raw_outer_original_caps':{'stdout':65536,'stderr':32768,'whole':2097152},'six_raw_cells':[196644]*6,'raw_sum':1179864,'original_metadata_reserve_NOT_new_shape_proof':489472,'proposed_whole_partition':1669337,'margin':427815,'prior_A1752095321_margin1831_carried':False,'GPG_aggregate_original':65536,'full_base64_for65536':4*((65536+2)//3),'inner_cap_original':32768,'supervisor_cap_original':65536,'raw_error_duplicate_value_preimages_history_factory_shapes':'CODE_OPEN','error_cells_original':35,'original_each2048_total71680':'preserved reservation NOT new graph bound','all35_native_factories':'complete retained original rows plus current-method complement; new shape proof CODE_OPEN','fullraw_partial_capped_hashonly_unknown':'NOT_PROVEN_NOT_FULLRAW','no_cap_or_oracle_cut':True})
write('author/final-read-ledger.json',{'exact_metadata_read_bytes':READ,'rows':ROWS,'whole_IO_RAM':'UNKNOWN_NOT_ZERO','tool_patch_literal_helpers_read_reserve_bytes':100663296,'intake_exact':4794480,'no_supplied_syntax_AST_compile_import_exec_tests':True})
print(json.dumps({'method':'STOCK_TEXT_JSON_SHA9_ONLY','changed':sum(x['changed'] for x in delta),'equal':sum(not x['changed'] for x in delta),'original35':35,'original6':6,'all3_F1F2':True,'full19':full,'hash_subsets':buckets['hash_subsets'],'fixed':fixed,'running':running,'read_sum':sum(buckets.values()),'metadata_reads':READ,'whole':'CODE_OPEN'}))
