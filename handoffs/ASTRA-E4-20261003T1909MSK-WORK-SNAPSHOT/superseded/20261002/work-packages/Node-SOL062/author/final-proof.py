"""Own finite stock metadata/TEXT proof materializer. Never imports supplied Source.

Full byte hashes, literal joins and integer JSON are metadata, not syntax,
factory qualification, tests, runtime or complete producer acceptance.
"""
import os, stat, json, hashlib, re, base64, difflib, time, resource
from pathlib import Path

ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
OLD=Path('/var/tmp/friday-lab865-sol060-a182-node-whole6-all35-all6-raw-bound-schema-connected-source-closure')
NAMES=('caller.py','supervisor.py','verifier.py','launch-contract.json','expectations.json','ordinary-surplus.txt')
ACTORS=NAMES[:3]
CACHE={}; READ=0; LEDGER=[]
PRIOR=77151329
UNTRACKED_CHARGE=67108864
SEAL_RESERVE=33554432
LIMIT=268435456
def nine(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read(path, expected=None):
    global READ
    p=Path(path); key=str(p)
    if key not in CACHE:
        s=p.lstat(); assert stat.S_ISREG(s.st_mode) and s.st_nlink==1,key
        assert PRIOR+UNTRACKED_CHARGE+SEAL_RESERVE+READ+s.st_size<=LIMIT,(key,READ)
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            assert nine(os.fstat(fd))==nine(s)
            blocks=[]
            while True:
                b=os.read(fd,1048576)
                if not b: break
                blocks.append(b)
            data=b''.join(blocks)
            assert nine(os.fstat(fd))==nine(s)
        finally: os.close(fd)
        assert nine(p.lstat())==nine(s) and len(data)==s.st_size
        pin={'path':key,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'identity9_decimal_strings':nine(s),'stable9':True}
        READ+=len(data); CACHE[key]=(data,pin); LEDGER.append(pin)
    data,pin=CACHE[key]
    assert nine(p.lstat())==pin['identity9_decimal_strings']
    if expected:
        assert pin['sha256']==expected['sha256'] and pin['bytes']==expected.get('bytes',expected.get('size')),key
        assert pin['identity9_decimal_strings']==expected['identity9_decimal_strings'],key
    return data,pin
def raw(rel): return read(ROOT/rel)[0]
def load(rel): return json.loads(raw(rel))
def put(rel,obj):
    p=ROOT/rel; p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    data=obj if isinstance(obj,bytes) else (json.dumps(obj,indent=2,ensure_ascii=True)+'\n').encode()
    if p.exists():
        s=p.lstat(); assert stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==1000 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==0o600
        assert str(p).startswith(str(ROOT/'evidence')+'/')
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f: f.write(data)
    assert p.lstat().st_size==len(data)
def normalize():
    assert ROOT.lstat().st_uid==1000 and stat.S_ISDIR(ROOT.lstat().st_mode)
    for directory,dirs,files in os.walk(ROOT,followlinks=False):
        d=Path(directory); assert not d.is_symlink(); os.chmod(d,0o700)
        for name in dirs:
            p=d/name; assert stat.S_ISDIR(p.lstat().st_mode) and not p.is_symlink()
        for name in files:
            p=d/name; s=p.lstat(); assert stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==1000 and s.st_nlink==1
            os.chmod(p,0o600)
def sections(name,data):
    lines=data.decode().splitlines(keepends=True)
    starts=[i for i,line in enumerate(lines) if re.match(r'^(def|class) [A-Za-z_]',line)]
    bounds=sorted(set([0,*starts,len(lines)])); rows=[]; functions={}
    for a,b in zip(bounds,bounds[1:]):
        text=''.join(lines[a:b]); match=re.match(r'(def|class) ([A-Za-z_][A-Za-z_0-9]*)',lines[a])
        row={'file':name,'name':match[2] if match else 'MODULE_INITIALIZATION','kind':match[1] if match else 'module',
             'start_line':a+1,'end_line':b,'full_text':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),
             'semantics':'LEXICAL_FULL_TEXT_NOT_AST_OR_CALLGRAPH','defaults_constructor_finalizers':'complete bytes retained, not executed or proven'}
        rows.append(row)
        if match:
            # Obtain exact indented body for cross-actor lexical equality.
            end=a+1
            while end<len(lines) and (not lines[end].strip() or lines[end].lstrip().startswith('#') or lines[end][0].isspace()): end+=1
            body=''.join(lines[a:end]).rstrip()+'\n'
            functions[match[2]]={'file':name,'line':a+1,'body_sha256':hashlib.sha256(body.encode()).hexdigest(),'body':body,'kind':match[1]}
    assert ''.join(r['full_text'] for r in rows).encode()==data
    nested=[{'line':i+1,'literal':line.rstrip('\n')} for i,line in enumerate(lines) if re.match(r'^\s+(def|class) ',line)]
    return rows,functions,nested
def coordinates(methods):
    result={}
    for method in methods:
        matches=[]
        for actor in ACTORS:
            if method in FUNCTIONS[actor]:
                f=FUNCTIONS[actor][method]; matches.append({k:f[k] for k in ('file','line','body_sha256')})
        result[method]=matches if matches else {'status':'NO_SAME_NAMED_CURRENT_TOPLEVEL_ROOT','code':'C01','original_obligation_not_removed':True}
    return result
def hash_literal(text,key,sha):
    assert len(re.findall(r'^'+re.escape(key)+r' = "'+sha+'"$',text,re.M))==1,key

normalize()
I=load('author/input.json'); E=load('expectations.json'); K=load('launch-contract.json')
current={name:read(ROOT/name)[1] for name in NAMES}; current_bytes={name:raw(name) for name in NAMES}
old={row['name']:row['pin'] for row in I['current6']}
assert set(old)==set(NAMES)
original={name:read(old[name]['path'],old[name])[0] for name in NAMES}
normative_original={name:raw('author/original-'+name) for name in NAMES}
for name in NAMES:
    assert hashlib.sha256(original[name]).hexdigest()==old[name]['sha256'] and len(original[name])==old[name]['bytes']
TEXT={name:current_bytes[name].decode() for name in ACTORS}
FUNCTIONS={}; complements=[]; nested={}; original_defs={}
for name in ACTORS:
    rows,functions,nested[name]=sections(name,current_bytes[name]); FUNCTIONS[name]=functions; complements+=rows
    _,original_defs[name],_=sections(name,original[name])
    assert set(original_defs[name])<=set(functions),(name,'removed original top-level definition')
put('evidence/whole-actor-full-method-complement.json',{'schema':'friday.sol062.whole3-full-lexical-complement.v1',
    'current_actor_pins':{n:current[n] for n in ACTORS},'partitions_reconstruct_full_original_bytes':True,
    'original_top_level_names':{n:list(original_defs[n]) for n in ACTORS},'all_nested_definition_coordinates':nested,
    'whole_sections':complements,'runtime':'NOT_RUN','proof':'complete lexical text coverage only; NOT semantic closure'})
shared=('retain_error_object','deferred_error','publish_errors','validate_stock_error_graph','exact_error','validate_received_error_graphs','original_stock_cause')
for function in shared:
    bodies=[FUNCTIONS[a][function]['body'] for a in ACTORS]
    assert len(set(bodies))==1,function
for name,text in TEXT.items():
    fd_cap='FD_ALLOCATION_CAP = 256' if name=='caller.py' else 'FD_HISTORY_CAP = 256'
    assert 'ERROR_OBJECT_CAP = 256' in text and fd_cap in text
    assert 'native_field_presence' in text and 'while error_index < len(objects) or value_index < len(values):' in text
    assert len(re.findall(r'\bexact_error\(',text))==2,(name,'projection outside publication')
assert 'publish_errors(record)' in FUNCTIONS['verifier.py']['encode_bounded']['body']
assert 'publish_errors(record)' in FUNCTIONS['supervisor.py']['encode']['body']
assert 'publish_errors(R)' in FUNCTIONS['caller.py']['public_partition_bytes']['body']
assert 'validate_received_error_graphs(value)' in FUNCTIONS['caller.py']['strict_json']['body']
assert 'validate_received_error_graphs(value)' in FUNCTIONS['supervisor.py']['strict_receipt']['body']

# Whole 19 hashes are fresh full bytes, binary/archive content opaque.
reference=load('author/intake-physical19.json'); mapping={'caller':'caller.py','supervisor':'supervisor.py','verifier':'verifier.py','contract':'launch-contract.json','expectations':'expectations.json','plain':'ordinary-surplus.txt'}
physical={}
for key,pin in reference.items():
    physical[key]=current[mapping[key]] if key in mapping else read(pin['path'],pin)[1]
assert len(physical)==19
sizes={key:pin['bytes'] for key,pin in physical.items()}
sup_names=('prlimit','python','supervisor','contract','caller','expectations','plain')
ver_names=('prlimit','python','verifier','contract','caller','expectations','plain')
full=sum(sizes.values()); sup=sum(sizes[n] for n in sup_names); ver=sum(sizes[n] for n in ver_names)
buckets={'hash_initial':full+19,'hash_subsets':10*(sup+7)+2*(ver+7),'hash_terminal':full+19,
    'selection':14*65537,'child':6*32768,'proc_bootstrap':29186,'proc_cleanup':6222482,'proc_terminal':24834,'expectations_body':sizes['expectations']+1}
for index in range(6):
    buckets['transport_'+str(index)]=65536+32768+2
    buckets['proc_launch_'+str(index)]=155917
    buckets['proc_close_'+str(index)]=28931
fixed=sum(buckets.values()); running=LIMIT-fixed; assert running>=94216
buckets['proc_running']=running; assert sum(buckets.values())==LIMIT
selection={'schema':'friday.e4.node.ordinary-root-selection-data.a082.v1','pins':{key:{'path':p['path'],'size':p['bytes'],'sha256':p['sha256'],'identity9':[int(v) for v in p['identity9_decimal_strings']]} for key,p in physical.items()}}
encoded=(json.dumps(selection,indent=2,ensure_ascii=True)+'\n').encode(); decoded=json.loads(encoded)
assert decoded==selection and len(encoded)<=65536
assert all(type(v) is int for p in decoded['pins'].values() for v in p['identity9'])
put('evidence/actual19-numeric9-data-EVIDENCE-NOT-ROOT-SELECTION.json',encoded)
put('evidence/current19-read-plan.json',{'schema':'friday.sol062.final19-stock-metadata-plan.v1','current19':physical,'current19_full_bytes':full,
    'supervisor_subset_names':sup_names,'supervisor_subset_bytes':sup,'verifier_subset_names':ver_names,'verifier_subset_bytes':ver,
    'buckets':buckets,'fixed_reserved_bytes':fixed,'running_reserved_bytes':running,'minimum_observe_exit_recheck':94216,
    'total_reserved_bytes':sum(buckets.values()),'selection_raw_bytes':len(encoded),'numeric9_roundtrip':'stock integer JSON full9 preserved; never V8 Number',
    'Source_install_plan':'derives actual final local9 plus rebound FIXED sizes; initial/terminal nineteen EOF bytes and subset seven EOF bytes retained',
    'Root_authority':False,'selection_role':'EVIDENCE_ONLY; independent Root must choose/requalify actual full19 before separate native effects',
    'old19_approval_or_clock_transferred':False,'runtime_actual_IO_RAM_CPU':'UNKNOWN_NOT_ZERO_NOT_PROVEN','archive_parse_extract_execute':False})

vh=current['verifier.py']['sha256']; ch=current['launch-contract.json']['sha256']; sh=current['supervisor.py']['sha256']
assert K['source']['path']==str(ROOT/'verifier.py') and K['source']['sha256']==vh and K['source']['bytes']==sizes['verifier']
assert K['independent_prelaunch_custody']['inherited_fds']['5']['sha256']==vh and K['independent_prelaunch_custody']['inherited_fds']['5']['bytes']==sizes['verifier']
hash_literal(TEXT['supervisor.py'],'SOURCE_SHA',vh); hash_literal(TEXT['supervisor.py'],'CONTRACT_SHA',ch)
for key,sha in [('VHASH',vh),('CHASH',ch),('SHASH',sh)]: hash_literal(TEXT['caller.py'],key,sha)
for name,key,hkey in [('verifier.py','verifier','VHASH'),('launch-contract.json','contract','CHASH'),('supervisor.py','supervisor','SHASH')]:
    literal='"'+key+'": (SOURCE + "/'+name+'", '+str(current[name]['bytes'])+', '+hkey+','
    assert literal in TEXT['caller.py'],literal
for name,key,hkey in [('verifier.py','source','SOURCE_SHA'),('launch-contract.json','contract','CONTRACT_SHA')]:
    literal='"'+key+'": (PREFIX + "/'+name+'", '+str(current[name]['bytes'])+', '+hkey+','
    assert literal in TEXT['supervisor.py'],literal
for name in ACTORS:
    assert current[name]['sha256'] not in TEXT[name],(name,'self hash cycle')
old_E=json.loads(original['expectations.json']); assert E['cases']==old_E['cases'] and E['case_order']==old_E['case_order']
assert E['cases']==json.loads(normative_original['expectations.json'])['cases'] and E['case_order']==json.loads(normative_original['expectations.json'])['case_order']
assert E['publication_source_contract']['bound_scope']=='ARITHMETIC_PARTITION_ONLY_NOT_NEW_STOCK_FACTORY_WIDTH_OR_FULL_DELIVERY_PROOF'
assert K['current_actor_receipt_schemas']['caller'] in TEXT['caller.py'] and K['current_actor_receipt_schemas']['supervisor'] in TEXT['supervisor.py'] and K['current_actor_receipt_schemas']['verifier'] in TEXT['verifier.py']
assert K['resource_schema_ids']==json.loads(original['launch-contract.json'])['resource_schema_ids'] if 'resource_schema_ids' in K else True
put('evidence/current-source-joins.json',{'schema':'friday.sol062.source6-acyclic-bothside-joins.v1','current6':current,
    'DAG':['verifier full bytes','contract Source+argv+inherited5','supervisor SOURCE_SHA CONTRACT_SHA sizes','caller VHASH CHASH SHASH sizes','external seal','external manifest','external terminal'],
    'bothside_actual_envelope_schemas':K['current_actor_receipt_schemas'],'all_shared_full_functions_equal':coordinates(shared),
    'error_shape':'complete v2 both receivers; native_field_presence added, fixed original256 private journal, joint error/value frontiers, complete bytes SHA/base64/count/typed refs, no pending marker durable public evidence',
    'original_resource_schema_ids':{'supervisor':'friday.e4.node.supervisor-resources.a172.v1','verifier':'friday.e4.node.verifier-resources.a172.v1'},
    'all_original_case_literals_preserved':True,'runtime':'NOT_RUN','SourceReady':False,'CODE':'C01-C05 complete stock factory/full publication proof open'})

# Full original rows are preserved, not old approval transplanted to new fields.
old35=load('provenance/LAB865/author/all35-connected-matrix.json')['rows']; assert len(old35)==35
connected=('retain_error_object','deferred_error','publish_errors','exact_error','validate_stock_error_graph','validate_received_error_graphs',
    'preown_fd','allocate_owned','allocate_pipe','retire_and_close_owned','retire_record','finish_allocations','close_final',
    'retain_cell','call','capture_pipe','drain_pipes','drain','retain_produced_transport','wire_receipt','recover_wire_receipt','validate_fullraw_layers')
joined=coordinates(connected)
rows=[]
for oldrow in old35:
    row=oldrow['original_full_row']
    rows.append({'original_full_row':row,'slot':row['slot'],'original_store':row['store'],'original_roots':row['roots'],
        'actual_current_root_bodies':coordinates(row['roots']),'connected_new_methods':joined,
        'factory_domain':'original stock type32/native32/path160/clock32/shared900 two-byte escapes; no universal native custom/C heap requirement',
        'new_copies_aliases_state_notes_bytes_frames_constructor_history':'complete new stock-shape/factory capacity and byte+cost proof C01/C02/C04/C05 CODE_OPEN',
        'primary_secondary_raw_prefix_cleanup':'physical owners retained before projection; C03 unread/unknown/fstat/append/partial/delivery remains CODE_OPEN',
        'old_qualified_error_bounds_apply_to_new_fields':False,'runtime':'NOT_RUN'})
put('evidence/all35-connected-matrix.json',{'schema':'friday.sol062.original35-current-connected.v1','rows':rows,'original35_count':35,'full_source_complement':'whole-actor-full-method-complement.json','not_only_original_roots':True})

case_rows=[]
for case in E['cases']:
    case_rows.append({'case':case['case'],'full_unchanged_oracle':case,'runtime':'NOT_RUN',
      'normal_raw':'complete original stdout/stderr/writable preimages/count/SHA/EOF+exact inner canonical same-object recovery required',
      'error_raw':'private first original/tb and bounded secondary slots before later close; all known cells attempted before public copying',
      'partial_prefix':'unknown owner/fstat/unreaped/short/append/pending/EOF/quota drift never accepted as full raw; C03',
      'defaults_absence':'native_field_presence separates absence from explicit null; UNKNOWN native resource stays null+reason, never synthetic0',
      'constructor_cleanup':'original prospective256 returnednumber/pipepair/wrapper identity; RETIRE_UNKNOWN before physical firstclose; subsequent known firstclose attempted before error graph publication; C01/C03 still open',
      'full_producer_consumer_bound':'inner32768/super65536/outer2097152 and current fixed GPG factory/new graphs/history all copies MUST fit and be delivered before original end; C01/C02/C04 CODE_OPEN',
      'full_phase_end':'no fresh deadline, expired parking, oracle cut, capraise, dropped fields or fake telemetry'})
put('evidence/all6-normal-error-raw-custody-matrix.json',{'schema':'friday.sol062.all6-current-normal-error-partial.v1','case_order':E['case_order'],'rows':case_rows,'all6_REQUIRED':True,'execution_or_waiver':False})
three=load('provenance/LAB865/author/all3-F1-F2-matrix.json')
three.update(schema='friday.sol062.original3-F1F2-D1D2-current.v1',F1={'current_methods':coordinates(('preown_fd','allocate_owned','allocate_pipe','retire_record','retire_and_close_owned','finish_allocations','close_final')),'private_physical_close_phase_before_public_graph':'implemented designated loops; all implicit operations/history fit not proven C01/C03','original_allocation_history_capacity':256,'unconfirmedclose_retry':False},
    F2={'current_methods':coordinates(('call','retain_cell','capture_pipe','drain_pipes','drain','retain_produced_transport','validate_fullraw_layers')),'original_complete_raw_durable_required':True,'pending_commit':'retained owners; next arithmetic before masked commit; failed read/pending offset not overwritten by cleanup reread','complete_allprefix_delivery':'C02/C03/C04 CODE_OPEN'},
    D2={'all_phase_memory_raw_self_wait4_highwater_current_Vm_fields':'original rules retained; unknown never0; no peak subtraction','full_pins':'actual19 recomputed, not execution qualification','original_phase_clock':'unchanged single deadline and one cleanup'},runtime='NOT_RUN')
put('evidence/all3-F1-F2-D1-D2-matrix.json',three)

phases=('bootstrap','allocation','first native error','raw production','pending raw commit','exact reap','raw capture','known-owner firstclose','publication projection','encoding','durable receiver delivery','terminal')
phase_rows=[]
for actor in ACTORS:
    for phase in phases:
        phase_rows.append({'actor':actor,'phase':phase,'Source_pin':current[actor],
            'methods':coordinates({'caller.py':('launch','drain_pipes','close_final','public_partition_bytes','encode_complete','emit_complete'),
                                   'supervisor.py':('launch','drain','retain_produced_transport','finish_allocations','encode','emit'),
                                   'verifier.py':('call','retain_cell','finish_allocations','encode_bounded','emit')}[actor]),
            'primary_secondary':'private original object+first caught traceback before graph; fallible projection not native settlement proof',
            'RAM_CPU_FD_explicit_implicit_IO':'original caps and typed honest metrics retained; whole new lifetime/implicit costs UNKNOWN_NOT_ZERO_NOT_PROVEN',
            'copies_factory_encoded_fit_and_native_end':'C01-C05 CODE_OPEN where not joined; future independent image/native qualification is separately NOT_RUN',
            'clock':'original finite scope, no resetting cleanup/start deadline','runtime':'NOT_RUN'})
put('evidence/resource-error-raw-phase-end-matrix.json',{'schema':'friday.sol062.whole3-resource-error-raw-phase.v1','rows':phase_rows,
    'original_common_receipt':E['common_receipt_contract'],'publication_partition':E['publication_source_contract'],
    'capacity_note':'prefunding256 does not prove actual new full stock factory width/history or complete delivery; original metadata partition not new proof',
    'SourceReady':False,'Root_admission':False,'GO':False})

delta=[]; diffs=[]
for name in NAMES:
    before=original[name]; after=current_bytes[name]; equal=before==after
    delta.append({'name':name,'old_pin':old[name],'current_pin':current[name],'equal':equal,'before_full_base64':base64.b64encode(before).decode(),
        'after_full_base64':base64.b64encode(after).decode(),'complete_before_after_bytes':True,'absent_placeholder':False})
    if not equal:
        diffs.extend(difflib.unified_diff(before.decode().splitlines(keepends=True),after.decode().splitlines(keepends=True),fromfile='LAB865/'+name,tofile='SOL062/'+name))
assert sum(r['equal'] for r in delta)==1 and delta[-1]['equal']
put('evidence/whole6-full-byte-delta-and-equal.json',{'schema':'friday.sol062.whole6-full-byte-delta.v1','rows':delta,'changed5_equal1':True,'runtime_credit':False})
put('evidence/source-delta.diff',''.join(diffs).encode())
provenance=[]
for p in I['current_source_pins']:
    rel=Path(p['path']).relative_to(OLD)
    if str(rel) in NAMES:
        provenance.append({'old_pin':p,'current_pin':current[str(rel)],'status':'CURRENT_COHERENT_SOURCE_DELTA' if p['sha256']!=current[str(rel)]['sha256'] else 'CURRENT_EQUAL_FULL_BYTES'})
    else:
        newpath=ROOT/'provenance/LAB865'/rel; b,np=read(newpath)
        assert np['sha256']==p['sha256'] and np['bytes']==p['bytes'],str(rel)
        provenance.append({'old_pin':p,'inert_copy_pin':np,'status':'EXACT_INERT_PROVENANCE_ONLY_NOT_CURRENT_ACCEPTANCE'})
put('evidence/full-original-source-cohort-binding.json',{'schema':'friday.sol062.full-current-input-cohort.v1','rows':provenance,'count':len(provenance),'historical_helpers_manifests_results':'inert provenance only; never executed/accepted as current','originals_edited':False})
put('evidence/helper-correction-record.json',{'schema':'friday.sol062.owned-helper-correction.v1','events':[{'helper':'author/continue-transform.py','observed_exit':1,'reason':'owned literal pattern assertion matched nested indentation twice','effects_before_assertion':'no Source writes','repair':'anchored exact newline in own literal template before materialization','Source_or_native_execution_retry':False},{'helper':'author/final-proof.py','observed_exit':1,'reason':'own metadata helper distinguished older normative original files from latest LAB865 current6; SHA assertion caught wrong delta baseline','effects_before_assertion':'only own private modes normalized; no Source bytes or evidence written','repair':'exact INPUT current6 pins are now full byte+9 verified delta baseline; older originals retained independently for normativity','Source_or_native_execution_retry':False}],
    'historical_author_fragments':'inert earlier templates; final actual bodies+shared equality are normative current Source, native_field_presence supersedes older stock-error-projection template','all_costs_charged':'included conservative uninstrumented allowance; unknown implicit IO not0'})
put('evidence/unsafe-history-required-not-run.json',{'schema':'friday.sol062.abstract-required-historical-controls.v1','status':'ABSTRACT_REQUIRED_NOT_RUN','required':True,'waiver':False,'payload':None,'fixture':None,'generator':None,'repro':None,'exploit':None,'legitimate_owned_child_EOF_write_deadline_cleanup':'still REQUIRED; no source/domain/oracle removal','native_live_gates':'NOT_RUN'})
put('evidence/proof-read-ledger.json',{'full_read_bytes':READ,'full_byte_pins':LEDGER,'prior_instrumented_read_bytes':PRIOR,
    'uninstrumented_literal_helper_shell_reads_conservative_charge_bytes':UNTRACKED_CHARGE,
    'future_full_seal_terminal_max_read_reserve_bytes':SEAL_RESERVE,
    'author_conservative_reserved_total_bytes':PRIOR+READ+UNTRACKED_CHARGE+SEAL_RESERVE,'author_read_cap':LIMIT,
    'author_own_proof_peak_RSS_native_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    'author_own_proof_raw_resource':{'user_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_utime,'system_seconds':resource.getrusage(resource.RUSAGE_SELF).ru_stime},
    'whole_Source_RAM_CPU_implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN','model_children':0,'network':0,'Source_executions':0,'Source_AST_compiler_tests_native_Root_GPG_Node_archiveparse_live_gates':'NOT_RUN','read_credit_is_NOT_runtime_or_gate_PASS':True})
print(json.dumps({'own_proof_read_bytes':READ,'reserved_read_total':PRIOR+READ+UNTRACKED_CHARGE+SEAL_RESERVE,
    'full19_bytes':full,'fixed_bytes':fixed,'running_bytes':running,'full_top_level_sections':len(complements),'all35':len(rows),'all6':len(case_rows),
    'source6_sizes':{n:current[n]['bytes'] for n in NAMES},'CODE':'OPEN','SourceReady':False,'runtime':'NOT_RUN'}))
