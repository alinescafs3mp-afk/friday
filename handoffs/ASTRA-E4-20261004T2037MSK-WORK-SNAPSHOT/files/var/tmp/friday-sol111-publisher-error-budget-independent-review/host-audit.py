"""Own stock host JSON/stat/SHA9/literal diff only; never parse/execute Source."""
import json,os,stat,hashlib,difflib,resource,sys
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT='/var/tmp/friday-sol111-publisher-error-budget-independent-review'
READ=0
def identity(s):
 return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def pin(path,expected=None):
 global READ
 s=os.lstat(path)
 assert stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and not s.st_mode&0o077,path
 with open(path,'rb') as f:
  assert identity(os.fstat(f.fileno()))==identity(s)
  b=f.read()
  assert identity(os.fstat(f.fileno()))==identity(s)
 assert identity(os.lstat(path))==identity(s)
 READ+=len(b)
 p=dict(path=path,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),identity9_decimal_strings=identity(s),stable9=True)
 if expected:
  if 'identity9' in expected:assert p['identity9_decimal_strings']==expected['identity9'].split(':')
  for k in ('bytes','sha256','identity9_decimal_strings'):
   if k in expected:assert p[k]==expected[k],(path,k)
 return b,p
def index(d):
 r={}
 for x in d['files']:
  n=x['name'] if 'name' in x else os.path.relpath(x['path'],d['root'])
  assert n not in r
  r[n]=dict(x,path=d['root']+'/'+n)
 return r
def is_source(n):return n.startswith(('source/','native/','consumer/source/'))
def inventory(d,ix):
 actual=[]
 for parent,dirs,files in os.walk(d['root'],followlinks=False):
  for n in dirs:assert not os.path.islink(parent+'/'+n)
  for n in files:
   assert not os.path.islink(parent+'/'+n)
   actual.append(os.path.relpath(parent+'/'+n,d['root']))
 assert sorted(actual)==sorted(list(ix)+['manifest.json'])
 return dict(root=d['root'],file_count=len(actual),exact=True)
mode=sys.argv[1]
if mode=='audit':
 b,ip=pin('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL111-INPUT-20261004.json',dict(sha256='b3fc2bd51afdad6116fc345eff0211a77f68e473de6ff644dd4b22fe7368acc1'));d=json.loads(b)
 b,cp=pin(d['snapshot']['path'],d['snapshot']);c=json.loads(b);ci=index(c)
 b,bp=pin(d['baseline']['path'],d['baseline']);a=json.loads(b);ai=index(a)
 b,lp=pin(d['references'][0]['path'],d['references'][0]);lab=json.loads(b);li=index(lab)
 names={x['name'] for x in d['Source57']}
 assert len(names)==57 and names=={n for n in ci if is_source(n)}=={n for n in ai if is_source(n)}=={n for n in li if is_source(n)}
 pins=[];changes=[];same=[];labjoin=[];cb=ab=lb=0
 for x in d['Source57']:
  n=x['name'];new,np=pin(x['path'],x);old,op=pin(ai[n]['path'],ai[n]);other,lop=pin(li[n]['path'],li[n])
  for k in ('bytes','sha256','identity9_decimal_strings'):assert np[k]==ci[n][k]
  assert new.endswith(b'\n') and old.endswith(b'\n') and other.endswith(b'\n')
  cb+=len(new);ab+=len(old);lb+=len(other)
  pins.append(dict(name=n,candidate=np,baseline=op,lab=lop,candidate_LF=new.count(b'\n'),baseline_LF=old.count(b'\n')))
  if new!=other:labjoin.append(n)
  if new==old:same.append(n);continue
  ol=old.splitlines(keepends=True);nl=new.splitlines(keepends=True);h=[]
  for tag,p,q,r,s in difflib.SequenceMatcher(None,ol,nl,autojunk=False).get_opcodes():
   if tag!='equal':h.append(dict(tag=tag,old_start=p+1,old_end=q,new_start=r+1,new_end=s,old_text=b''.join(ol[p:q]).decode(),new_text=b''.join(nl[r:s]).decode()))
  changes.append(dict(name=n,hunks=h))
 extras=[]
 for source,ix,label in ((c,ci,'candidate'),(lab,li,'lab')):
  for n,x in ix.items():
   if n in names:continue
   b,p=pin(x['path'],x)
   extras.append(dict(package=label,name=n,pin=p,value=json.loads(b) if n.endswith('.json') else b.decode()))
 refs=[]
 for x in d['references'][1:]+[d['pre_task_compact']['completed']]:
  b,p=pin(x if isinstance(x,str) else x['path'],None if isinstance(x,str) else x);v=json.loads(b)
  refs.append(dict(pin=p,value={k:q for k,q in v.items() if k not in ('files','pins','source_pins','Source57')}))
 _,tp=pin('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL111-TASK.txt',dict(sha256='d48a070b7e5b13fcb0def90029482b11570fb161d6813a673636028c62e0c783'))
 report=dict(schema='friday.sol111.host-literal-audit.v1',input=ip,task=tp,candidate_manifest=cp,baseline_manifest=bp,lab_manifest=lp,source_pins=pins,changes=changes,unchanged=same,candidate_vs_lab_changed=labjoin,candidate_bytes=cb,baseline_bytes=ab,lab_bytes=lb,extra_artifacts=extras,references=refs,inventory=[inventory(c,ci),inventory(a,ai),inventory(lab,li)])
elif mode=='seal':
 b,_=pin(ROOT+'/pins-and-literal-audit.json');a=json.loads(b)
 expected=[a[k] for k in ('input','task','candidate_manifest','baseline_manifest','lab_manifest')]
 expected += [p[key] for p in a['source_pins'] for key in ('candidate','baseline','lab')]
 expected += [x['pin'] for x in a['extra_artifacts']+a['references']]
 checked=[]
 for p in expected:checked.append(pin(p['path'],p)[1])
 inventories=[]
 for key in ('candidate_manifest','baseline_manifest','lab_manifest'):
  b,_=pin(a[key]['path'],a[key]);d=json.loads(b);inventories.append(inventory(d,index(d)))
 report=dict(schema='friday.sol111.final-fullSHA9-seal.v1',verified_pins=checked,inventories=inventories)
else:raise AssertionError(mode)
report.update(verified_msk=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),counted_host_read_bytes=READ,helper_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,Source_execution=0,Source_parsing=0)
print(json.dumps(report,separators=(',',':')))
