"""Own literal substitution/reversal proof, not Source parsing or execution."""
import json,hashlib,os,stat,resource
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT='/var/tmp/friday-sol111-publisher-error-budget-independent-review'
reads=0
def nine(s):return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def load(p):
 global reads
 s=os.lstat(p['path']);assert stat.S_ISREG(s.st_mode)
 with open(p['path'],'rb') as f:
  assert nine(os.fstat(f.fileno()))==nine(s);b=f.read();assert nine(os.fstat(f.fileno()))==nine(s)
 assert nine(os.lstat(p['path']))==nine(s)
 for k,v in dict(bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),identity9_decimal_strings=nine(s)).items():
  if k in p:assert p[k]==v,(p['path'],k)
 reads+=len(b);return b
a=json.loads(load(dict(path=ROOT+'/pins-and-literal-audit.json')))
x=next(x for x in a['extra_artifacts'] if x['package']=='candidate' and x['name']=='LITERAL-DELTA.json')
d=json.loads(load(x['pin']))
assert len(d['hunks'])==30
proof=[]
for row in a['source_pins']:
 hs=[h for h in d['hunks'] if h['file']==row['name']]
 if not hs:continue
 old=load(row['baseline']);new=load(row['candidate']);cur=old
 for h in hs:
  p=h['old'].encode();q=h['new'].encode()
  assert cur.count(p)==1,(h['id'],'forward unique');cur=cur.replace(p,q,1)
 assert cur==new,(row['name'],'forward whole')
 for h in reversed(hs):
  p=h['new'].encode();q=h['old'].encode()
  assert cur.count(p)==1,(h['id'],'inverse unique');cur=cur.replace(p,q,1)
 assert cur==old,(row['name'],'inverse whole')
 proof.append(dict(file=row['name'],hunks=len(hs),unique_forward_inverse=True,whole_exact=True))
report=dict(schema='friday.sol111.authored-literal-replay.v1',delta_pin=x['pin'],declared_hunks=30,proof=proof,verified_msk=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),counted_host_read_bytes=reads,helper_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,Source_parsing=0,Source_execution=0)
print(json.dumps(report,separators=(',',':')))
