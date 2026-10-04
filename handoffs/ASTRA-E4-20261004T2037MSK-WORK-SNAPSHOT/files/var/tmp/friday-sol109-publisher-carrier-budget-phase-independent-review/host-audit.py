"""Owned host-only JSON/stat/SHA9/literal-LF comparison. No Source parsing/execution."""
import json, os, stat, hashlib, difflib, resource
from datetime import datetime
from zoneinfo import ZoneInfo
READ_BYTES = 0
def identity(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read_pin(path, expected=None):
    global READ_BYTES
    a=os.lstat(path)
    assert stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid() and not a.st_mode & 0o077, path
    with open(path,'rb') as f:
        assert identity(os.fstat(f.fileno()))==identity(a)
        b=f.read()
        assert identity(os.fstat(f.fileno()))==identity(a)
    assert identity(os.lstat(path))==identity(a)
    READ_BYTES+=len(b)
    pin=dict(path=path,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),identity9_decimal_strings=identity(a),stable9=True)
    for k in ('bytes','sha256','identity9_decimal_strings'):
        if expected and k in expected: assert pin[k]==expected[k], (path,k)
    return b,pin
def index(d):
    return {x.get('name',x['path'][len(d['root'])+1:]):x for x in d['files']}
root='/var/tmp/friday-sol109-publisher-carrier-budget-phase-independent-review'
b,ip=read_pin('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL109-INPUT-20261004.json',{'sha256':'193c71e422e3719d44c421378c1af57a433f73329afe1467319bc28113938f54'})
d=json.loads(b)
c,cp=read_pin(d['snapshot']['path'],d['snapshot']); c=json.loads(c)
a,ap=read_pin(d['baseline']['path'],d['baseline']); a=json.loads(a)
ci,ai=index(c),index(a)
refs=[]
for x in d['references']:
    b,p=read_pin(x if isinstance(x,str) else x['path'],None if isinstance(x,str) else x)
    refs.append({'pin':p,'value':json.loads(b)})
lab=refs[0]['value']; li=index(lab)
names={x['name'] for x in d['Source57']}
assert len(names)==57
assert names=={n for n in ci if n.startswith(('source/','native/','consumer/source/'))}
assert names=={n for n in ai if n.startswith(('source/','native/','consumer/source/'))}
assert names=={n for n in li if n.startswith(('source/','native/','consumer/source/'))}
pins=[]; changes=[]; same=[]; cbodies={}; abodies={}; labdiff=[]
for x in d['Source57']:
    name=x['name']; new,np=read_pin(x['path'],x); old,op=read_pin(ai[name]['path'],ai[name]); lb,lp=read_pin(li[name]['path'],li[name])
    assert all(b.endswith(b'\n') for b in (new,old,lb))
    pins.append(dict(name=name,candidate=np,baseline=op,lab=lp,candidate_LF_count=new.count(b'\n'),baseline_LF_count=old.count(b'\n')))
    cbodies[name]=new; abodies[name]=old
    if lb!=new:labdiff.append(name)
    if new==old:same.append(name);continue
    ol,nl=old.splitlines(keepends=True),new.splitlines(keepends=True)
    assert b''.join(ol)==old and b''.join(nl)==new
    hunks=[];fw=[];rv=[]
    for tag,p,q,r,s in difflib.SequenceMatcher(None,ol,nl,autojunk=False).get_opcodes():
        if tag=='equal':fw.extend(ol[p:q]);rv.extend(nl[r:s])
        else:
            fw.extend(nl[r:s]);rv.extend(ol[p:q])
            hunks.append(dict(tag=tag,old_LF_start=p+1,old_LF_end=q,new_LF_start=r+1,new_LF_end=s,old_text=b''.join(ol[p:q]).decode(),new_text=b''.join(nl[r:s]).decode()))
    assert b''.join(fw)==new and b''.join(rv)==old
    changes.append(dict(name=name,hunks=hunks,forward_exact=True,inverse_exact=True))
extras=[];delta=None
for n,x in ci.items():
    if n in names:continue
    b,p=read_pin(x['path'],x)
    if n=='LITERAL-DELTA.json':delta=json.loads(b)
    extras.append(dict(name=n,pin=p,value=json.loads(b) if n in ('INTEGRATION.json','INERT-OBLIGATIONS.json') else None))
assert len(delta['hunks'])==36
forward=dict(abodies); reverse=dict(cbodies); declared=[]
for h in delta['hunks']:
    name=h['file']; old=h['old'].encode(); new=h['new'].encode()
    assert forward[name].count(old)==1,(h['id'],'forward occurrence')
    start=forward[name].index(old)
    forward[name]=forward[name][:start]+new+forward[name][start+len(old):]
    declared.append(dict(id=h['id'],file=name,old_bytes=len(old),new_bytes=len(new)))
for h in reversed(delta['hunks']):
    name=h['file']; old=h['old'].encode(); new=h['new'].encode()
    assert reverse[name].count(new)==1,(h['id'],'inverse occurrence')
    start=reverse[name].index(new)
    reverse[name]=reverse[name][:start]+old+reverse[name][start+len(new):]
assert forward==cbodies and reverse==abodies
labextras=[]
for n,x in li.items():
    if n in names:continue
    b,p=read_pin(x['path'],x)
    labextras.append(dict(name=n,pin=p,value=json.loads(b) if n in ('INTEGRATION.json','INERT-OBLIGATIONS.json','timing.json') else None))
assert labdiff==['native/publisher_root_entry.c']
for ref in refs:
    if 'files' in ref['value']:
        ref['value']={k:v for k,v in ref['value'].items() if k!='files'}
report=dict(schema='friday.sol109.host-literal-audit.v1',verified_msk=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),input=ip,candidate_manifest=cp,baseline_manifest=ap,source_pins=pins,changed=changes,unchanged=same,declared_hunks=declared,declared36_forward_inverse_exact=True,lab_other56_exact=True,lab_changed=labdiff,references=refs,extra_artifacts=extras,lab_artifacts=labextras,counted_host_read_bytes=READ_BYTES,host_max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,Source_execution=0,Source_parsing=0)
print(json.dumps(report,separators=(',',':')))
