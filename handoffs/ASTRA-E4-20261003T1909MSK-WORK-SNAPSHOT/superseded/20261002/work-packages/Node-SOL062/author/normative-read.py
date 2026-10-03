"""Own complete stock-byte normative complement, no native/Source parsing."""
import os,stat,hashlib,json
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
def ident(s): return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
prior=json.loads((ROOT/'author/intake-read-ledger.json').read_bytes())
known={p['path']:p for p in prior['full_read_pins']}; rows=[]; actual_bytes=0
def pins(obj):
    if isinstance(obj,dict):
        if 'path' in obj and 'sha256' in obj and ('bytes' in obj or 'size' in obj): yield obj
        else:
            for v in obj.values(): yield from pins(v)
    elif isinstance(obj,list):
        for v in obj: yield from pins(v)
def read(pin):
    global actual_bytes
    path=pin['path']; before=os.lstat(path)
    assert stat.S_ISREG(before.st_mode) and before.st_nlink==1 and ident(before)==pin['identity9_decimal_strings']
    if path in known:
        assert known[path]['sha256']==pin['sha256'] and known[path]['identity9_decimal_strings']==ident(before)
        rows.append({'pin':pin,'method':'REUSED_THIS_ASSIGNMENT_COMPLETE_BYTE_READ_WITH_CURRENT_UNCHANGED9'})
        # Only small JSON needs another body read to traverse its normative links.
        if not path.endswith('.json'): return None
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        assert ident(os.fstat(fd))==ident(before)
        pieces=[]
        while True:
            part=os.read(fd,1048576)
            if not part: break
            actual_bytes+=len(part); assert prior['full_byte_read_bytes']+actual_bytes<160*1024*1024
            pieces.append(part)
        raw=b''.join(pieces)
        assert ident(os.fstat(fd))==ident(before)
    finally: os.close(fd)
    assert ident(os.lstat(path))==ident(before) and len(raw)==pin.get('bytes',pin.get('size')) and hashlib.sha256(raw).hexdigest()==pin['sha256']
    known[path]=pin; rows.append({'pin':pin,'method':'FULL_STOCK_BYTE_SHA9'})
    return raw
def put(rel,obj):
    p=ROOT/rel
    data=(json.dumps(obj,indent=2,ensure_ascii=False)+'\n').encode() if not isinstance(obj,bytes) else obj
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f: f.write(data)
A=json.loads((ROOT/'author/original-ASTRA-E4-A178-INPUT-20261002.json').read_bytes())
keys=['original_exact_LAB863_input','original_exact_A175_input','original_exact_A172_input','original_scope_pin','original_full_scope_materials']
for key in keys:
    for pin in pins(A[key]):
        raw=read(pin)
        if raw is not None and pin['path'].endswith('.json') and key!='original_full_scope_materials':
            put('author/normative-'+Path(pin['path']).name,raw)
put('author/normative-full-read-ledger.json',{'schema':'friday.sol062.original-normative-complement.v1','read_bytes':actual_bytes,'pins':rows,'prior_same_assignment_read_bytes':prior['full_byte_read_bytes'],'Source_Runtime_Gates':'NOT_RUN','no_old_acceptance_transferred':True})
print(json.dumps({'normative_full9_pins':len(rows),'new_full_read_bytes':actual_bytes,'combined_instrumented_read_bytes':actual_bytes+prior['full_byte_read_bytes']}))
