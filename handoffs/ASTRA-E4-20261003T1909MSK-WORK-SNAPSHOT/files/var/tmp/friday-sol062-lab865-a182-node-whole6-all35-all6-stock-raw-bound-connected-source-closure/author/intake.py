"""Own trusted stock full-byte/int9 intake; never parses/executes Source."""
import os, stat, json, hashlib, re
from pathlib import Path

ROOT = Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
OLD = Path('/var/tmp/friday-lab865-sol060-a182-node-whole6-all35-all6-raw-bound-schema-connected-source-closure')
INPUT = Path('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL062-INPUT-20261002.json')
CACHE = {}; LEDGER = []
def identity(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read(path, expected=None):
    path=str(path)
    if path in CACHE:
        data,pin=CACHE[path]
        assert identity(os.lstat(path)) == pin['identity9_decimal_strings']
    else:
        before=os.lstat(path); assert stat.S_ISREG(before.st_mode) and before.st_nlink==1
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            opened=os.fstat(fd); assert identity(opened)==identity(before)
            parts=[]
            while True:
                part=os.read(fd,1048576)
                if not part: break
                parts.append(part)
            data=b''.join(parts); assert identity(os.fstat(fd))==identity(before)
        finally: os.close(fd)
        assert identity(os.lstat(path))==identity(before) and len(data)==before.st_size
        pin={'path':path,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'identity9_decimal_strings':identity(before),'stable9':True}
        CACHE[path]=(data,pin); LEDGER.append(pin)
    if expected:
        assert pin['sha256']==expected['sha256'], path
        assert pin['bytes']==expected.get('bytes',expected.get('size')),path
        if 'identity9_decimal_strings' in expected: assert pin['identity9_decimal_strings']==expected['identity9_decimal_strings'],path
    return data,pin
def put(rel,data):
    p=ROOT/rel; p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if isinstance(data,(dict,list)): data=(json.dumps(data,indent=2,ensure_ascii=False)+'\n').encode()
    if isinstance(data,str): data=data.encode()
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f: f.write(data)
    return p
def pins(obj):
    if isinstance(obj,dict):
        if set(('path','sha256'))<=obj.keys() and ('bytes' in obj or 'size' in obj): yield obj
        else:
            for value in obj.values(): yield from pins(value)
    elif isinstance(obj,list):
        for value in obj: yield from pins(value)

raw,input_pin=read(INPUT); assert input_pin['sha256']=='e4cbf8ef5a96d90564bb17d324155170c876f8ae08e44907d70ce05ddf41c624'
I=json.loads(raw); put('author/input.json',raw)
task,task_pin=read('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL062-TASK.txt'); assert task_pin['sha256']=='3941b5f8d3aa40f72660a6c386c20132c0a5bdb1a8ec4ebb623e8b8898b1052a'; put('author/task.txt',task)
compact,cp=read('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL062-PRE-TASK-COMPACT-20261002.json'); C=json.loads(compact)
assert C['assignment']==I['assignment'] and C['generation']==1 and C['same_tui'] and C['completion']['item_type']=='contextCompaction' and C['completion']['status']=='completed'
assert C['thread_id']=='01a0d516-a446-7131-a123-faeea4bb8e8c' and C['after']['queue_count']==0
put('author/pre-task-compact.json',compact)
notice,np=read('/home/jericho/.jericho/runtime/sol-link-native-receipts/ASTRA-SOL-E4-SOL062-T1.json'); N=json.loads(notice)
assert N['sha256']==task_pin['sha256'] and N['assignment_id']==I['assignment'] and N['recipient_generation']=='d5afa5e1-60d7-457b-931b-fd12001a35a4' and N['sender_generation']=='acba5992-6a22-4e73-b141-30f35bb631a7'
put('author/native-intake-receipt.json',notice)
put('author/received.json',{'schema':'friday.sol062.received.v1','assignment':I['assignment'],'generation':1,'event_id':N['event_id'],'ref':str(task_pin['path']),'sha256':task_pin['sha256'],'input':input_pin,'accepted_msk':'2026-10-02T14:17:39+03:00','accepted_epoch_seconds':1790939859,'pre_task_compact':cp,'no_repeated_compact':True,'model_children':0})
for pin in pins(I): read(pin['path'],pin)
for row in I['current6']: put(row['name'],CACHE[row['pin']['path']][0])
for pin in I['current_source_pins']:
    rel=Path(pin['path']).relative_to(OLD)
    if str(rel) not in {r['name'] for r in I['current6']}: put('provenance/LAB865/'+str(rel),CACHE[pin['path']][0])
for key in ('original_A182_input_pin','original_LAB865_input_pin','current_Root_received_pin','A182_independent_review_received_pin'):
    put('author/exact-'+key+'.json',CACHE[I[key]['path']][0])
A=json.loads(CACHE[I['original_A182_input_pin']['path']][0]); original=[]
for pin in pins(A['original_normative_links']):
    data,p=read(pin['path'],pin); original.append(p)
    put('author/original-'+Path(pin['path']).name,data)
plan=json.loads(CACHE[str(OLD/'author/current19-read-plan.json')][0])
physical={}
for key,pin in plan['selected19_reference_only'].items():
    data,p=read(pin['path'],pin); physical[key]=p
assert len(physical)==19
put('author/intake-physical19.json',physical)
sections=[]
for row in I['current6']:
    data=CACHE[row['pin']['path']][0]
    if not row['name'].endswith('.py'): continue
    lines=data.decode().splitlines(keepends=True); starts=[i for i,line in enumerate(lines) if re.match(r'^(class|def) [A-Za-z_]',line)]
    bounds=[0]+starts+[len(lines)]
    for a,b in zip(bounds,bounds[1:]):
        if a==b: continue
        text=''.join(lines[a:b]); sections.append({'file':row['name'],'start_line':a+1,'end_line':b,'full_text':text,'sha256':hashlib.sha256(text.encode()).hexdigest()})
put('author/intake-full-text-sections.json',{'grammar':'plain lexical TEXT, not AST/callgraph/syntax/acceptance','sections':sections})
put('author/intake-read-ledger.json',{'full_byte_read_bytes':sum(p['bytes'] for p in LEDGER),'full_read_pins':LEDGER,'whole_original_normative_links':original,'supply_execution':False})
print(json.dumps({'primary_pins':len(list(pins(I))),'unique_full_reads':len(LEDGER),'read_bytes':sum(p['bytes'] for p in LEDGER),'text_sections':len(sections),'original_normative_keys':list(A['original_normative_links']),'physical19_bytes':sum(p['bytes'] for p in physical.values()),'old_root_bytes':sum(p['bytes'] for p in I['current_source_pins'])}))
