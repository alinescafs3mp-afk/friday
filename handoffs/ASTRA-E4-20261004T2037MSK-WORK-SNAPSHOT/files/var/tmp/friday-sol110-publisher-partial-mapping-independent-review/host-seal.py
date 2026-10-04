"""Own host SHA9/inventory/literal-hunk checker. No Source parser or execution."""
import json,os,stat,hashlib,re,sys,resource
from datetime import datetime
from zoneinfo import ZoneInfo
READ_BYTES=0
ROOT='/var/tmp/friday-sol110-publisher-partial-mapping-independent-review'
def identity(s):
    return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read_pin(path,expected=None):
    global READ_BYTES
    a=os.lstat(path);assert stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid() and not a.st_mode&0o077,path
    with open(path,'rb') as f:
        assert identity(os.fstat(f.fileno()))==identity(a)
        b=f.read();assert identity(os.fstat(f.fileno()))==identity(a)
    assert identity(os.lstat(path))==identity(a)
    READ_BYTES+=len(b)
    p=dict(path=path,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),identity9_decimal_strings=identity(a),stable9=True)
    if expected:
        for k in ('bytes','sha256','identity9_decimal_strings'):
            if k in expected:assert p[k]==expected[k],(path,k)
    return b,p
b,_=read_pin(ROOT+'/pins-and-literal-audit.json');a=json.loads(b)
if sys.argv[1]=='delta':
    item=next(x for x in a['extra_artifacts'] if x['name']=='LITERAL-DELTA')
    b,dp=read_pin(item['pin']['path'],item['pin'])
    sections=[];current=None;h=None
    for line in b.decode().splitlines(keepends=True):
        if line.startswith('FORWARD_A258'):continue
        if line.startswith(('FILE ','INVERSE ')):
            direction='forward' if line.startswith('FILE ') else 'inverse'
            name=line.split(' ',1)[1].strip();current=dict(direction=direction,name=name,hunks=[]);sections.append(current);h=None
        elif line.startswith('@@ '):
            m=re.fullmatch(r'@@ -(\d+),(\d+) \+(\d+),(\d+) @@\n',line);assert m,line
            h=dict(old_start=int(m[1]),old_count=int(m[2]),new_start=int(m[3]),new_count=int(m[4]),old=[],new=[]);current['hunks'].append(h)
        elif line[:1] in ('-','+'):
            assert h is not None
            h['old' if line[0]=='-' else 'new'].append(line[1:].encode())
        else:raise AssertionError(('unrecognized literal delta line',line))
    out=[]
    for s in sections:
        p=next(x for x in a['source_pins'] if x['name']==s['name'])
        oldpin=p['baseline'] if s['direction']=='forward' else p['candidate']
        newpin=p['candidate'] if s['direction']=='forward' else p['baseline']
        old,_=read_pin(oldpin['path'],oldpin);new,_=read_pin(newpin['path'],newpin)
        ol=old.splitlines(keepends=True);r=[];cursor=0
        for h in s['hunks']:
            at=h['old_start']-1
            assert cursor<=at and len(h['old'])==h['old_count'] and len(h['new'])==h['new_count']
            assert ol[at:at+h['old_count']]==h['old'],(s['name'],s['direction'],h['old_start'])
            r+=ol[cursor:at]+h['new'];cursor=at+h['old_count']
        r+=ol[cursor:]
        assert b''.join(r)==new,(s['name'],s['direction'],'whole replay')
        out.append(dict(name=s['name'],direction=s['direction'],hunks=len(s['hunks']),whole_replay_exact=True))
    report=dict(schema='friday.sol110.declared-literal-replay.v1',delta_pin=dp,sections=out,forward_inverse_all3_exact=True)
elif sys.argv[1]=='seal':
    expected=[a['input'],a['candidate_manifest'],a['baseline_manifest']]
    expected+=[x['candidate'] for x in a['source_pins']]+[x['baseline'] for x in a['source_pins']]
    expected+=[x['pin'] for x in a['extra_artifacts']]+[x['pin'] for x in a['references']]
    expected.append(dict(path='/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL110-TASK.txt',sha256='706772006d024382748a9da62b29abda4802bf5b9d044d3a1a05ebe381659a8f'))
    checked=[]
    for p in expected:
        b,q=read_pin(p['path'],p);checked.append(q)
    manifest=json.loads(read_pin(a['candidate_manifest']['path'],a['candidate_manifest'])[0])
    actual=[];all_actual=[]
    for parent,dirs,files in os.walk(manifest['root'],followlinks=False):
        for n in dirs:assert not os.path.islink(parent+'/'+n)
        for n in files:
            rel=os.path.relpath(parent+'/'+n,manifest['root']);assert not os.path.islink(parent+'/'+n)
            all_actual.append(rel)
            if rel.startswith(('source/','native/','consumer/source/')):actual.append(rel)
    assert sorted(actual)==sorted(x['name'] for x in a['source_pins'])
    assert sorted(all_actual)==sorted([x['name'] for x in manifest['files']]+['manifest.json'])
    report=dict(schema='friday.sol110.final-fullSHA9-seal.v1',verified_pins=checked,candidate_inventory57_exact=True,complete_candidate_inventory65_exact=True,baseline57_unchanged=True)
else:raise AssertionError(sys.argv[1])
report.update(verified_msk=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),counted_host_read_bytes=READ_BYTES,helper_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,Source_execution=0,Source_edits=0)
print(json.dumps(report,separators=(',',':')))
