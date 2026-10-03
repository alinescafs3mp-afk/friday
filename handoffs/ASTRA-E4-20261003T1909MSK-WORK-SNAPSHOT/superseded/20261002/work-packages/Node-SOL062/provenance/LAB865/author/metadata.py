#!/usr/bin/env python3
"""Owned stock metadata only: never import/parse/execute supplied Source."""
import os, sys, json, hashlib, stat, time
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
INPUT=Path('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL060-INPUT-20261002.json')
READ=0
LEDGER=[]
def nine(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def acquire(p, expected=None):
    global READ
    p=Path(p); s=p.lstat()
    assert stat.S_ISREG(s.st_mode) and s.st_nlink==1
    assert s.st_size <= 8*1024*1024, str(p)
    assert READ+s.st_size < 100*1024*1024
    b=p.read_bytes(); READ+=len(b); e=p.lstat()
    sha=hashlib.sha256(b).hexdigest(); assert nine(s)==nine(e)
    row={'path':str(p),'bytes':len(b),'sha256':sha,'identity9_decimal_strings':nine(s)}
    if expected:
        assert sha==expected['sha256'], str(p)
        if 'bytes' in expected: assert len(b)==expected['bytes'],str(p)
        if 'identity9_decimal_strings' in expected: assert nine(s)==expected['identity9_decimal_strings'],str(p)
    LEDGER.append(row); return b,row
def write(rel, obj):
    p=ROOT/rel; p.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    for parent in (p.parent,*p.parent.parents):
        if parent==ROOT.parent: break
        os.chmod(parent,0o700)
    b=(json.dumps(obj,indent=2,ensure_ascii=True)+'\n').encode() if not isinstance(obj,bytes) else obj
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as h: h.write(b)
def pins(x, skip=()):
    if isinstance(x,dict):
        if isinstance(x.get('path'),str) and 'sha256' in x: yield x
        else:
            for k,v in x.items():
                if k not in skip: yield from pins(v,skip)
    elif isinstance(x,list):
        for v in x: yield from pins(v,skip)
def intake():
    b,_=acquire(INPUT,{'sha256':'942b90a238ea573e7bf5910325742a23313c641d3912bdec00b2d6f92e5c3294'})
    doc=json.loads(b); docs=[doc]; seen={str(INPUT)}; cache={str(INPUT):b}
    # Entire pinned textual scopes, including original35 factories, not old binary/current-stock qualification.
    for d in list(pins(doc)):
        if d['path'] in seen: continue
        b,_=acquire(d['path'],d); seen.add(d['path']); cache[d['path']]=b
        if d['path'].endswith('INPUT-20261002.json'): docs.append(json.loads(b))
    for doc2 in docs[1:]:
        for d in pins(doc2,('current_selected19_before_new_bytes','selected_image')):
            if d['path'] in seen: continue
            if d.get('bytes',0)>8*1024*1024 or d['path'].startswith('/usr/'):
                continue
            b,_=acquire(d['path'],d); seen.add(d['path']); cache[d['path']]=b
    # Exact descendants A172/A175/scope were read above; no recursive full historical chain.
    source=Path(doc['Source']['root'])
    for d in doc['Source']['pins']:
        rel=Path(d['path']).relative_to(source)
        out=rel if rel.name in ('caller.py','supervisor.py','verifier.py','launch-contract.json','expectations.json','ordinary-surplus.txt') else Path('provenance/LAB863')/rel
        write(str(out),cache[d['path']])
    write('author/input.json',cache[str(INPUT)])
    write('author/received.json',{'event_id':'ASTRA-SOL-E4-SOL060-T1','assignment':doc['assignment'],'generation':1,'task_ref':'/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL060-TASK.txt','sha256':'0c84dc3f498bcbcaff7bf5c801d560925f887c42f0e63a1f4180ef3f339a98ee','accepted_at':'2026-10-02T12:44:44.680479643+03:00','accepted_epoch_ns':1790934284680479643,'compact':'VERIFIED_ONCE','queue':'exact_task_already_started; checkpoint_count0','effects':'INERT_TEXT_JSON_AND_STOCK_METADATA_ONLY'})
    write('author/intake-read-ledger.json',{'read_bytes':READ,'rows':LEDGER,'binary_current19':'historical declarations retained; NOT current qualification','whole_IO_RAM':'UNKNOWN_NOT_ZERO'})
    summary=[]
    for path,b in cache.items():
        if path.endswith('.json'):
            try: x=json.loads(b)
            except (ValueError,UnicodeError): continue
            if isinstance(x,dict):
                summary.append({'path':path,'keys':list(x),'scope':{k:v for k,v in x.items() if k in ('scope','directions','findings','errors','gaps','verdict','SourceReady','Root_admission','GO')}})
    write('author/intake-text-scopes.json',summary)
    print(json.dumps({'read_bytes':READ,'pins':len(LEDGER),'documents':len(summary),'source6':'copied inert','no_supplied_code_executed':True}))
if __name__=='__main__':
    assert sys.argv[1]=='intake'
    intake()
