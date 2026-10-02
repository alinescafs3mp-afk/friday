"""Own finite stock full-byte SHA9/pathset/DAG seal; NO supplied Source exec.

Only this new author tree and the exact assigned terminal may be written.
Once the manifest is created all in-tree bytes are immutable.
"""
import os, stat, json, hashlib, base64, datetime, time, resource
from pathlib import Path

ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
TERMINAL=Path('/home/jericho/.jericho/grok-takeover/ASTRA-E4-LAB865-A182-NODE-WHOLE6-ALL35-ALL6-STOCK-RAW-BOUND-CONNECTED-SOURCE-CLOSURE-SOL062-RESULT.json')
ASSIGNMENT='ASTRA-E4-LAB865-A182-NODE-WHOLE6-ALL35-ALL6-STOCK-RAW-BOUND-CONNECTED-SOURCE-CLOSURE-SOL062'
READ=0; WRITE=0; CACHE={}; CAP=268435456; OUT_CAP=16777216; START=1790939859
MSK=datetime.timezone(datetime.timedelta(hours=3)); TASK_SHA='3941b5f8d3aa40f72660a6c386c20132c0a5bdb1a8ec4ebb623e8b8898b1052a'
def nine(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def private(s,directory=False):
    assert s.st_uid==s.st_gid==1000 and stat.S_IMODE(s.st_mode)==(0o700 if directory else 0o600)
    assert stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode) and s.st_nlink==1
def read(p,private_leaf=True):
    global READ
    p=Path(p); s=p.lstat()
    if private_leaf: private(s)
    assert stat.S_ISREG(s.st_mode) and s.st_nlink==1
    fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        assert nine(os.fstat(fd))==nine(s)
        parts=[]
        while True:
            b=os.read(fd,1048576)
            if not b: break
            parts.append(b)
        data=b''.join(parts); assert nine(os.fstat(fd))==nine(s)
    finally: os.close(fd)
    assert len(data)==s.st_size and nine(p.lstat())==nine(s)
    READ+=len(data)
    return data,{'path':str(p),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'identity9_decimal_strings':nine(s),'stable9':True}
def load(rel):
    data,pin=read(ROOT/rel); CACHE[str(ROOT/rel)]=(data,pin); return json.loads(data)
def put(p,obj):
    global WRITE
    p=Path(p); assert p==TERMINAL or p.parent==ROOT or p.parent==ROOT/'author'
    data=(json.dumps(obj,separators=(',',':'),ensure_ascii=True)+'\n').encode()
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
    directory=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)
    WRITE+=len(data); return read(p)[1]
def tree():
    leaves=[]; directories=[]; total=0
    for directory,dirs,files in os.walk(ROOT,followlinks=False):
        d=Path(directory); private(d.lstat(),True); directories.append(str(d.relative_to(ROOT)))
        for name in dirs:
            p=d/name; assert not p.is_symlink(); private(p.lstat(),True)
        for name in files:
            p=d/name; private(p.lstat()); leaves.append(str(p.relative_to(ROOT))); total+=p.lstat().st_size
    assert total<=OUT_CAP
    return sorted(leaves),sorted(directories),total
def hash_tree(expected):
    leaves,dirs,total=tree(); assert leaves==expected
    result={rel:read(ROOT/rel)[1] for rel in leaves}
    again,dirs2,total2=tree(); assert (again,dirs2,total2)==(leaves,dirs,total)
    return result,dirs,total

assert not TERMINAL.exists() and not (ROOT/'seal.json').exists() and not (ROOT/'manifest.json').exists()
# Normalize only the newly created trusted sealing helper, never original files.
helper=ROOT/'author/seal-final.py'; s=helper.lstat(); assert stat.S_ISREG(s.st_mode) and s.st_uid==s.st_gid==1000 and s.st_nlink==1
if stat.S_IMODE(s.st_mode)!=0o600: os.chmod(helper,0o600)
tree()
ledger=load('evidence/proof-read-ledger.json')
base_charge=ledger['prior_instrumented_read_bytes']+ledger['full_read_bytes']+ledger['uninstrumented_literal_helper_shell_reads_conservative_charge_bytes']
assert base_charge==219940675 and ledger['future_full_seal_terminal_max_read_reserve_bytes']==33554432
joins=load('evidence/current-source-joins.json'); plan=load('evidence/current19-read-plan.json')
delta=load('evidence/whole6-full-byte-delta-and-equal.json'); complement=load('evidence/whole-actor-full-method-complement.json')
rows35=load('evidence/all35-connected-matrix.json'); rows6=load('evidence/all6-normal-error-raw-custody-matrix.json')
cohort=load('evidence/full-original-source-cohort-binding.json')
input_=load('author/input.json'); received=load('author/received.json'); compact=load('author/pre-task-compact.json')
assert input_['assignment']==received['assignment']==compact['assignment']==ASSIGNMENT
assert received['generation']==compact['generation']==1 and received['accepted_epoch_seconds']==START
assert compact['completion']['item_type']=='contextCompaction' and compact['completion']['status']=='completed'
assert hashlib.sha256(read(ROOT/'author/task.txt')[0]).hexdigest()==TASK_SHA
assert hashlib.sha256(CACHE[str(ROOT/'author/input.json')][0]).hexdigest()=='e4cbf8ef5a96d90564bb17d324155170c876f8ae08e44907d70ce05ddf41c624'
assert len(rows35['rows'])==35 and len(rows6['rows'])==6 and len(cohort['rows'])==len(input_['current_source_pins'])
assert plan['current19_full_bytes']==72457444 and plan['fixed_reserved_bytes']==247788428 and plan['running_reserved_bytes']==20647028
assert sum(plan['buckets'].values())==CAP and plan['running_reserved_bytes']>=94216
for key,p in plan['current19'].items():
    assert nine(os.lstat(p['path']))==p['identity9_decimal_strings'],(key,'19 drift since full metadata pass')
    if key in ('caller','supervisor','verifier','contract','expectations','plain'): private(os.lstat(p['path']))
source={name:read(p['path']) for name,p in joins['current6'].items()}
for name,p in joins['current6'].items(): assert source[name][1]==p
for row in delta['rows']:
    before=base64.b64decode(row['before_full_base64'],validate=True); after=base64.b64decode(row['after_full_base64'],validate=True)
    assert before==after if row['equal'] else before!=after
    assert len(before)==row['old_pin']['bytes'] and hashlib.sha256(before).hexdigest()==row['old_pin']['sha256']
    assert after==source[row['name']][0] and row['current_pin']==source[row['name']][1]
for actor in ('caller.py','supervisor.py','verifier.py'):
    sections=[r for r in complement['whole_sections'] if r['file']==actor]
    assert ''.join(r['full_text'] for r in sections).encode()==source[actor][0]
    assert all(hashlib.sha256(r['full_text'].encode()).hexdigest()==r['sha256'] for r in sections)
numeric=load('evidence/actual19-numeric9-data-EVIDENCE-NOT-ROOT-SELECTION.json')
assert set(numeric)=={'schema','pins'} and len(numeric['pins'])==19
assert all(type(v) is int for p in numeric['pins'].values() for v in p['identity9'])
for key,p in numeric['pins'].items():
    expected=plan['current19'][key]
    assert set(p)=={'path','size','sha256','identity9'} and p['path']==expected['path'] and p['size']==expected['bytes'] and p['sha256']==expected['sha256']
    assert [str(v) for v in p['identity9']]==expected['identity9_decimal_strings']
unmapped={str(r['slot']):[key for key,v in r['actual_current_root_bodies'].items() if isinstance(v,dict)] for r in rows35['rows']}
unmapped={k:v for k,v in unmapped.items() if v}
assert not unmapped,unmapped

put(ROOT/'author/final-verification.json',{'schema':'friday.sol062.owned-stock-final-verification.v1','current6_full_sha9':joins['current6'],
    'full35_roots_rejoined_to_current_actual_full_text':True,'whole_actor_sections':len(complement['whole_sections']),
    'all6_case_oracle_count':6,'original_cohort_count':len(cohort['rows']),'changed5_equal1_complete_bytes_verified':True,
    'exact_numeric19_schema_and_all171_integers':True,'fresh_full19_metadata_reuse_at_boundary':'all original13 and current6 still exact9; no old Source/native qualification inherited',
    'SourceReady':False,'Root_admission':False,'GO':False,'runtime_tests_AST_compiler_native_GPG_Node_archiveparse_live_gates':'NOT_RUN',
    'all_CODE':{'C01':'whole original stock new shape/history/width/copies/constructor capacity-cost proof','C02':'inner32KiB/super64KiB/outer2MiB complete fixed-factory shape and durable delivery fit','C03':'unknown/unsettled/fstat/read/append/partial unread raw retirement','C04':'all original stock causal/alias/presence and full end acceptance','C05':'new full both-side RAM/CPU/FD/implicit IO/resource end acceptance'},
    'own_metadata_helper_faults_not_Source_retry':[{'helper':'final-proof.py','exit':1,'fault':'wrong older normative versus current LAB865 delta baseline','Source_writes':False},
        {'helper':'final-proof.py','exit':1,'fault':'private FD cap symbolic name differs caller versus supervisor/verifier; both original256','Source_writes':False,'effects':'only own preseal evidence complement written; then recomputed on exact snapshot'}],
    'resource_ledger':'base full instrumented reads plus conservative67MiB covers uninstrumented shell/helper reads and finite own helper corrections; final full seal measured separately; implicit whole IO unknown not0',
    'model_children':0,'network':0,'Source_execution_retries':0,'author_acceptance_epoch_seconds':START})

payload_paths,dirs,initial_total=tree()
assert len(payload_paths)==len(set(payload_paths)) and all('..' not in Path(p).parts for p in payload_paths)
payload_pins={rel:read(ROOT/rel)[1] for rel in payload_paths}
expected=sorted(payload_paths+['seal.json','manifest.json'])
put(ROOT/'seal.json',{'schema':'friday.sol062.full-byte9-payload-seal.v1','assignment':ASSIGNMENT,'generation':1,
    'payload_pathset':payload_paths,'payload_pins':payload_pins,'final_exact_pathset':expected,'directory_pathset':dirs,
    'private_contract':'all dirs700/files600 uidgid1000 regular nlink1 no symlinks; final whole pathset/byte9 independently checked twice by own stock helper',
    'DAG':'payload -> seal -> manifest -> external terminal; no own selfhash','historical_manifests_helpers':'inert provenance only','SourceReady':False,'Root_admission':False,'GO':False})
seal_pin=read(ROOT/'seal.json')[1]
manifest_pin=put(ROOT/'manifest.json',{'schema':'friday.sol062.acyclic-full-pathset-manifest.v1','assignment':ASSIGNMENT,'generation':1,
    'seal':seal_pin,'current6':joins['current6'],'final_exact_pathset':expected,'payload_count':len(payload_paths),
    'full_Source':'CODE_OPEN; finite connected author slice complete, not Source closure','remaining_CODE':'RESIDUAL-SOL062.txt',
    'current19_readmath':'evidence/current19-read-plan.json','source_method_error_raw_phase_delta_proofs':'evidence/',
    'original_cohort':'evidence/full-original-source-cohort-binding.json','SourceReady':False,'Root_admission':False,'GO':False,
    'runtime_AST_compiler_ELF_ABI_tests_native_Root_GPG_Node_archiveparse_live_gates':'NOT_RUN',
    'sealed_tree_edit_rule':'IMMUTABLE after manifest; no payload/seal/manifest selfhash cycle'})
# Full tree double pass: all content bytes and every nine-field identity.
first,dirs1,total1=hash_tree(expected); second,dirs2,total2=hash_tree(expected)
assert first==second and dirs1==dirs2 and total1==total2 and first['manifest.json']==manifest_pin
for rel,p in payload_pins.items(): assert first[rel]==p
assert first['seal.json']==seal_pin
assert READ<=33554432 and base_charge+READ+8192<=CAP
finished_epoch=int(time.time()); elapsed=finished_epoch-START
assert 0<elapsed<6600 and elapsed<=6000
finished=datetime.datetime.fromtimestamp(finished_epoch,MSK).isoformat()
terminal={'schema':'friday.sol062.result.v1','assignment':ASSIGNMENT,'generation':1,'task_sha256':TASK_SHA,
    'disposition':'FINITE_CONNECTED_AUTHOR_PACKAGE_COMPLETE; WHOLE_SOURCE_CODE_OPEN','root':str(ROOT),'manifest':manifest_pin,
    'remaining_CODE':['C01','C02','C03','C04','C05'],'residual':'RESIDUAL-SOL062.txt','proofs':'evidence/',
    'SourceReady':False,'Root_admission':False,'GO':False,'runtime_AST_compiler_ELF_ABI_tests_native_Root_GPG_Node_archiveparse_live_gates':'NOT_RUN',
    'scope':'inert performing Source6/schema/actual19/literal DAG + full35/6/3/method/error/raw/phase/resources/full byte delta/equal; not independent acceptance',
    'current19_bytes':72457444,'fixed_read_bytes':247788428,'running_read_bytes':20647028,'total_read_reservation':CAP,
    'author_read_conservative_upper_bound_bytes':base_charge+READ+8192,'final_tree_files':len(expected),'final_tree_bytes':total1,
    'final_full_byte9_pathset_double_verified':True,'whole_IO_RAM_CPU':'UNKNOWN_NOT_ZERO_NOT_PROVEN','model_children':0,'network':0,
    'unsafe_history':'ABSTRACT_REQUIRED_NOT_RUN; required=true; waiver=false; payload/fixture/generator/repro/exploit=null',
    'accepted_msk':'2026-10-02T14:17:39+03:00','finished_msk':finished,'elapsed_seconds':elapsed}
body=(json.dumps(terminal,separators=(',',':'),ensure_ascii=True)+'\n').encode()
assert len(body)<=2000,(len(body),'normal terminal contract cap')
assert total1+len(body)<=OUT_CAP
notice=(f'PEER Sol RESULT id=SOL-SOL062-R1 assignment={ASSIGNMENT} assignment_generation=1 assignment_action=complete '
    f'from_epoch=d5afa5e1-60d7-457b-931b-fd12001a35a4 to_epoch=acba5992-6a22-4e73-b141-30f35bb631a7 ref={TERMINAL} sha256='+hashlib.sha256(body).hexdigest())
assert len(notice.encode())<=500,len(notice.encode())
tp=put(TERMINAL,terminal); t1=read(TERMINAL); t2=read(TERMINAL)
assert t1==t2 and t1[1]==tp and t1[0]==body
assert base_charge+READ<=terminal['author_read_conservative_upper_bound_bytes'] and READ<=33554432
paths3,dirs3,total3=tree(); assert (paths3,dirs3,total3)==(expected,dirs1,total1)
assert nine(os.lstat(ROOT/'manifest.json'))==manifest_pin['identity9_decimal_strings']
print(json.dumps({'terminal':tp,'contract_bytes':len(body),'notification_bytes':len(notice.encode()),
    'manifest_sha256':manifest_pin['sha256'],'full_tree_files':len(expected),'full_tree_bytes':total1,'seal_read_measured_bytes':READ,
    'author_conservative_read_upper_bound_bytes':terminal['author_read_conservative_upper_bound_bytes'],'author_own_seal_peak_RSS_native_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    'finished_msk':finished,'elapsed_seconds':elapsed,'immutable':True,'SourceReady':False,'runtime':'NOT_RUN'}))
