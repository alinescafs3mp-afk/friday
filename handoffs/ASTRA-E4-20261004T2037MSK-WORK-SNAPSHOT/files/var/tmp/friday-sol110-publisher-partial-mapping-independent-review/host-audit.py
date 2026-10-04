"""Own host JSON/stat/SHA9/literal LF audit only. Never parses or executes Source."""
import json, os, stat, hashlib, difflib, resource
from datetime import datetime
from zoneinfo import ZoneInfo
READ_BYTES=0
def identity(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read_pin(path,expected=None):
    global READ_BYTES
    a=os.lstat(path)
    assert stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid() and not a.st_mode&0o077,path
    with open(path,'rb') as f:
        assert identity(os.fstat(f.fileno()))==identity(a)
        b=f.read()
        assert identity(os.fstat(f.fileno()))==identity(a)
    assert identity(os.lstat(path))==identity(a)
    READ_BYTES+=len(b)
    p=dict(path=path,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),identity9_decimal_strings=identity(a),stable9=True)
    if expected:
        if 'identity9' in expected: assert p['identity9_decimal_strings']==expected['identity9'].split(':')
        for k in ('bytes','sha256','identity9_decimal_strings'):
            if k in expected:assert p[k]==expected[k],(path,k)
    return b,p
def index(d):
    out={}
    for x in d['files']:
        n=x['name'] if 'name' in x else x['path'][len(d['root'])+1:]
        out[n]=dict(x,path=d['root']+'/'+n)
    return out
ipath='/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL110-INPUT-20261004.json'
b,ip=read_pin(ipath,dict(sha256='e6bda26c1f864e45abf8a25ad7fd060a9181df2a1263f805c12c86e21ee3e939')); d=json.loads(b)
b,cp=read_pin(d['snapshot']['path'],d['snapshot']); c=json.loads(b);ci=index(c)
b,bp=read_pin(d['baseline']['path'],d['baseline']); a=json.loads(b);ai=index(a)
names={x['path'][len(c['root'])+1:] for x in d['Source57']}
assert len(names)==57 and names=={n for n in ci if n.startswith(('source/','native/','consumer/source/'))}=={n for n in ai if n.startswith(('source/','native/','consumer/source/'))}
pins=[];changes=[];same=[];nb={};ob={}
for x in d['Source57']:
    n=x['path'][len(c['root'])+1:]
    new,np=read_pin(x['path'],x);old,op=read_pin(ai[n]['path'],ai[n]);read_pin(ci[n]['path'],ci[n])
    assert new.endswith(b'\n') and old.endswith(b'\n')
    nb[n]=new;ob[n]=old
    pins.append(dict(name=n,candidate=np,baseline=op,new_LF_count=new.count(b'\n'),old_LF_count=old.count(b'\n')))
    if new==old:same.append(n);continue
    ol,nl=old.splitlines(keepends=True),new.splitlines(keepends=True);fw=[];rv=[];hunks=[]
    for tag,p,q,r,s in difflib.SequenceMatcher(None,ol,nl,autojunk=False).get_opcodes():
        fw+=nl[r:s];rv+=ol[p:q]
        if tag!='equal':hunks.append(dict(tag=tag,old_LF_start=p+1,old_LF_end=q,new_LF_start=r+1,new_LF_end=s,old_text=b''.join(ol[p:q]).decode(),new_text=b''.join(nl[r:s]).decode()))
    assert b''.join(fw)==new and b''.join(rv)==old
    changes.append(dict(name=n,hunks=hunks,forward_exact=True,inverse_exact=True))
extras=[]
for n,x in ci.items():
    if n in names:continue
    b,p=read_pin(x['path'],x)
    extras.append(dict(name=n,pin=p,value=json.loads(b) if n.endswith('.json') else b.decode()))
refs=[]
for x in d['references']:
    b,p=read_pin(x if isinstance(x,str) else x['path'],None if isinstance(x,str) else x)
    v=json.loads(b)
    refs.append(dict(pin=p,value={k:q for k,q in v.items() if k not in ('pins','files')}))
report=dict(schema='friday.sol110.host-literal-audit.v1',verified_msk=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),input=ip,candidate_manifest=cp,baseline_manifest=bp,source_pins=pins,changes=changes,unchanged=same,candidate_source_bytes=sum(len(x) for x in nb.values()),baseline_source_bytes=sum(len(x) for x in ob.values()),extra_artifacts=extras,references=refs,counted_host_read_bytes=READ_BYTES,helper_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,Source_execution=0,Source_parsing=0)
print(json.dumps(report,separators=(',',':')))
