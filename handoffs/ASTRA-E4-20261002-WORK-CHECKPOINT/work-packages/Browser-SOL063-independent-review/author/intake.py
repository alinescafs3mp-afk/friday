"""Own bounded stock metadata only. Never imports, parses AST, or executes Source."""
import os, stat, json, hashlib, re, resource
from pathlib import Path

OUT = Path('/var/tmp/friday-sol063-lab866-browser-whole209-all216-all29-independent-source-review')
BASE = Path('/home/jericho/.jericho/grok-takeover')
INP = BASE / 'ASTRA-E4-SOL063-INPUT-20261002.json'
LAB = Path('/var/tmp/friday-lab866-lab864-a183-browser-whole209-all216-all29-connected-source-closure')
os.umask(0o077)
READ = []; CACHE = {}; EXPECT = {}; DOCS = {}
def nine(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def rd(path,pin=None):
    path=str(path)
    if path in CACHE:
        if pin: check(CACHE[path], pin, path)
        return CACHE[path]
    a=os.lstat(path)
    assert stat.S_ISREG(a.st_mode) and a.st_nlink==1 and a.st_uid==os.getuid() and a.st_mode&0o777==0o600, path
    assert sum(x['bytes'] for x in READ)+a.st_size <= 180*1024*1024, 'intake read reserve'
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        assert nine(os.fstat(fd))==nine(a)
        chunks=[]
        while True:
            b=os.read(fd,1024*1024)
            if not b: break
            chunks.append(b)
        data=b''.join(chunks)
        assert nine(os.fstat(fd))==nine(a)==nine(os.lstat(path))
    finally: os.close(fd)
    row={'path':path,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'identity9_decimal_strings':nine(a)}
    READ.append(row); CACHE[path]=data
    if pin: check(data,pin,path)
    return data
def check(data,pin,path):
    assert len(data)==pin.get('bytes',pin.get('size',len(data))), path+' size'
    assert hashlib.sha256(data).hexdigest()==pin['sha256'], path+' SHA'
    n=pin.get('identity9_decimal_strings',pin.get('identity9'))
    if n is not None: assert nine(os.lstat(path))==[str(x) for x in n],path+' nine'
def pins(x):
    if isinstance(x,dict):
        if isinstance(x.get('path'),str) and x['path'].startswith('/') and re.fullmatch('[0-9a-f]{64}',str(x.get('sha256',''))):
            yield x
        for v in x.values(): yield from pins(v)
    elif isinstance(x,list):
        for v in x: yield from pins(v)
def save(name,obj):
    p=OUT/name; p.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    data=(json.dumps(obj,ensure_ascii=False,indent=2)+'\n').encode()
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(data)

inp=json.loads(rd(INP))
assert hashlib.sha256(CACHE[str(INP)]).hexdigest()=='541ef5fa6d0a386ca29c3f69c1020fb9bcc39a388a23be96a7300cb613f58109'
queue=[(inp['original_LAB866_INPUT_pin'],0)]
seen=set()
while queue:
    pin,depth=queue.pop(0); path=pin['path']
    if path in seen: continue
    seen.add(path); obj=json.loads(rd(path,pin)); DOCS[path]=obj
    for p in pins(obj):
        EXPECT.setdefault(p['path'],p)
        if depth<3 and '/grok-takeover/' in p['path'] and 'INPUT' in Path(p['path']).name and p['path'].endswith('.json'):
            queue.append((p,depth+1))
for p in pins(inp): EXPECT.setdefault(p['path'],p)
# Exact current physical package, full209 references, all eight old/new witnesses.
required={p['path']:p for p in inp['source_pins']}
required.update({x['pin']['path']:x['pin'] for x in inp['current209']})
for x in inp['actual_delta8']:
    required[x['old_pin']['path']]=x['old_pin'];required[x['new_pin']['path']]=x['new_pin']
labdoc=DOCS[inp['original_LAB866_INPUT_pin']['path']]
for key in ['Root_received_pin','Root_A183_independent_review_received_pin']:
    p=inp[key];required[p['path']]=p
for p in labdoc['Root_A183_review_pins']+labdoc['independent_A174']['pins']:
    required[p['path']]=p
# Normative complete original SOL057 review and its explicitly pinned matrices,
# exact current source already above; never reread unrelated unchanged archives.
for path,obj in DOCS.items():
    for key,val in obj.items():
        if key in ('independent_SOL057','original_author_input','original_full_SOL057_input','original_exact_A171_input','original_exact_A174_input','original_normative','original_exact_input'):
            for p in pins(val): required[p['path']]=p
for path,p in required.items(): rd(path,p)
physical=sorted(str(p.relative_to(LAB)) for p in LAB.rglob('*') if p.is_file())
expected=sorted(str(Path(p['path']).relative_to(LAB)) for p in inp['source_pins'])
assert physical==expected, ('package pathset',len(physical),len(expected))
for d in [LAB]+[p for p in LAB.rglob('*') if p.is_dir()]:
    st=os.lstat(d); assert st.st_uid==os.getuid() and stat.S_ISDIR(st.st_mode) and st.st_mode&0o777==0o700,str(d)
delta=[]
for x in inp['actual_delta8']:
    old=CACHE[x['old_pin']['path']];new=CACHE[x['new_pin']['path']]
    assert (old!=new)==x['changed'];delta.append({'name':x['name'],'changed':old!=new,'old':x['old_pin'],'new':x['new_pin']})
assert sum(x['changed'] for x in delta)==2
assert len(inp['current209'])==209 and len({x['name'] for x in inp['current209']})==209
save('author/intake-verification.json',{'input':INP.as_posix(),'physical47':len(physical),'pathset':physical,'current209':209,'actual_changed2_equal207':True,'delta8':delta,'read':READ,'read_bytes':sum(x['bytes'] for x in READ),'normative_documents':list(DOCS),'max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'all_source_execution':'NOT_RUN','implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN'})
save('author/expected-pins.json',list(EXPECT.values()))
save('author/normative-inputs.json',DOCS)
save('author/intake-structural-summary.json',{'documents':{p:{k:('array:'+str(len(v)) if isinstance(v,list) else 'object:'+','.join(v.keys()) if isinstance(v,dict) else v) for k,v in o.items()} for p,o in DOCS.items()},'expected_distinct':len(EXPECT),'verified_distinct':len(READ),'verified_bytes':sum(x['bytes'] for x in READ)})
print(json.dumps({'documents':list(DOCS),'verified':len(READ),'read_bytes':sum(x['bytes'] for x in READ),'package_files':len(physical),'source209_bytes':sum(x['pin']['bytes'] for x in inp['current209']),'changed2_equal207':True,'remaining_normative_candidates':len(EXPECT)-len(READ)}))
