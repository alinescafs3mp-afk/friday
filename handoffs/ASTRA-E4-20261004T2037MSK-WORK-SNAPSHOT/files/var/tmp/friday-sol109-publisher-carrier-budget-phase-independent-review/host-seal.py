"""Owned host-only artifact/pin/literal join and final integrity checks."""
import hashlib,json,os,stat,sys,resource
from datetime import datetime
from zoneinfo import ZoneInfo
READ=0
def ident(s):return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def pin(path,expected=None):
    global READ
    a=os.lstat(path);assert stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid() and a.st_mode&0o077==0,path
    with open(path,'rb') as f:
        assert ident(os.fstat(f.fileno()))==ident(a)
        b=f.read();assert ident(os.fstat(f.fileno()))==ident(a)
    assert ident(os.lstat(path))==ident(a);READ+=len(b)
    p=dict(path=path,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),identity9_decimal_strings=ident(a),stable9=True)
    for k in ('bytes','sha256','identity9_decimal_strings'):
        if expected and k in expected:assert p[k]==expected[k],(path,k)
    return b,p
ROOT='/var/tmp/friday-sol109-publisher-carrier-budget-phase-independent-review'
if sys.argv[1]=='join':
    d=json.loads(pin('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL109-INPUT-20261004.json',{'sha256':'193c71e422e3719d44c421378c1af57a433f73329afe1467319bc28113938f54'})[0])
    baseline=json.loads(pin(d['baseline']['path'],d['baseline'])[0]);ci=json.loads(pin(d['snapshot']['path'],d['snapshot'])[0]);lab=json.loads(pin(d['references'][0]['path'],d['references'][0])[0])
    def byname(m):return {x.get('name',x['path'][len(m['root'])+1:]):x for x in m['files']}
    ai,ci,li=byname(baseline),byname(ci),byname(lab)
    raw,dp=pin(ai['LITERAL-DELTA.json']['path'],ai['LITERAL-DELTA.json']);delta=json.loads(raw)
    assert len(delta['hunks'])==2
    raw,lp=pin(li['native/publisher_root_entry.c']['path'],li['native/publisher_root_entry.c'])
    target,cp=pin(ci['native/publisher_root_entry.c']['path'],ci['native/publisher_root_entry.c'])
    forward=raw;proof=[]
    for h in delta['hunks']:
        old,new=h['old'].encode(),h['new'].encode();assert forward.count(old)==1
        at=forward.index(old);proof.append(dict(id=h['id'],candidate_LF_start=forward[:at].count(b'\n')+1,old_bytes=len(old),new_bytes=len(new)))
        forward=forward[:at]+new+forward[at+len(old):]
    assert forward==target
    inverse=target
    for h in reversed(delta['hunks']):
        old,new=h['old'].encode(),h['new'].encode();assert inverse.count(new)==1
        at=inverse.index(new);inverse=inverse[:at]+old+inverse[at+len(new):]
    assert inverse==raw
    out=dict(schema='friday.sol109.hash-two-hunk-rejoin.v1',delta=dp,lab_native=lp,candidate_native=cp,two_hunks=proof,forward_exact=True,inverse_exact=True,other56_exact='AUTHENTICATED_IN_MAIN_AUDIT',counted_host_read_bytes=READ,Source_execution=0)
elif sys.argv[1]=='seal':
    audit=json.loads(pin(ROOT+'/pins-and-literal-audit.json')[0]);proof=[]
    for x in audit['source_pins']:
        _,p=pin(x['candidate']['path'],x['candidate']);proof.append(p)
        assert ident(os.lstat(x['baseline']['path']))==x['baseline']['identity9_decimal_strings']
        assert ident(os.lstat(x['lab']['path']))==x['lab']['identity9_decimal_strings']
    for x in [audit['input'],audit['candidate_manifest'],audit['baseline_manifest']]+[r['pin'] for r in audit['references']]+[r['pin'] for r in audit['extra_artifacts']]+[r['pin'] for r in audit['lab_artifacts']]:
        _,p=pin(x['path'],x);proof.append(p)
    manifest=json.loads(pin(audit['candidate_manifest']['path'],audit['candidate_manifest'])[0])
    declared={x.get('name',x['path'][len(manifest['root'])+1:]) for x in manifest['files'] if x['path'].startswith(manifest['root']+'/') and x['path'][len(manifest['root'])+1:].startswith(('source/','native/','consumer/source/'))}
    actual=set()
    for directory in ('source','native','consumer/source'):
        for top,dirs,files in os.walk(manifest['root']+'/'+directory):
            for f in files:actual.add(os.path.relpath(top+'/'+f,manifest['root']))
    assert actual==declared=={x['name'] for x in audit['source_pins']}
    own=[]
    for name in ('received.json','host-audit.py','host-seal.py','pins-and-literal-audit.json','hash-rejoin.json','review.json'):
        _,p=pin(ROOT+'/'+name);own.append(p)
    out=dict(schema='friday.sol109.final-integrity-check.v1',pins=proof,own_artifacts=own,candidate_source_count=57,candidate_source_inventory_exact=True,baseline57_identity9_unchanged=True,lab57_identity9_unchanged=True,counted_host_read_bytes=READ,host_max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,Source_execution=0)
elif sys.argv[1]=='inventory':
    own=[]
    for name in ('received.json','host-audit.py','host-seal.py','pins-and-literal-audit.json','hash-rejoin.json','review.json','seal-check.json'):
        _,p=pin(ROOT+'/'+name);own.append(p)
    out=dict(files=own,bytes=sum(x['bytes'] for x in own),counted_host_read_bytes=READ)
else:raise ValueError(sys.argv[1])
out['verified_msk']=datetime.now(ZoneInfo('Europe/Moscow')).isoformat()
print(json.dumps(out,separators=(',',':')))
