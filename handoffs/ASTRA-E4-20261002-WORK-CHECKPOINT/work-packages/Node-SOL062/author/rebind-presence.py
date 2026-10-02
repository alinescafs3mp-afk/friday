"""Own new changed-snapshot acyclic byte rebind, not repeat Source execution."""
import os,json,re,hashlib
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
def read(n): return (ROOT/n).read_bytes()
def encode(o): return (json.dumps(o,indent=2,ensure_ascii=False)+'\n').encode()
def write(n,d):
    fd=os.open(ROOT/n,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    with os.fdopen(fd,'wb') as f: f.write(d)
def lit(t,k,v):
    t,n=re.subn(r'^'+re.escape(k)+r' = "[0-9a-f]{64}"$',k+' = "'+v+'"',t,flags=re.M); assert n==1
    return t
v=read('verifier.py'); vh=hashlib.sha256(v).hexdigest()
k=json.loads(read('launch-contract.json')); old=k['source']['sha256']
def hashref(x):
    if isinstance(x,str): return vh if x==old else x
    if isinstance(x,list): return [hashref(v) for v in x]
    if isinstance(x,dict): return {a:hashref(b) for a,b in x.items()}
    return x
k=hashref(k); k['source']['bytes']=len(v); k['independent_prelaunch_custody']['inherited_fds']['5']['bytes']=len(v)
k['sol062_error_publication']['stock_native_optional_absence']='Explicit per-node errno/filename/filename2/__notes__ attribute presence; absent and present-null distinct, full values retained'
kb=encode(k); kh=hashlib.sha256(kb).hexdigest()
s=lit(lit(read('supervisor.py').decode(),'SOURCE_SHA',vh),'CONTRACT_SHA',kh)
for key,suffix,size,hashkey in [('source','verifier.py',len(v),'SOURCE_SHA'),('contract','launch-contract.json',len(kb),'CONTRACT_SHA')]:
    s,n=re.subn(r'("'+key+r'": \(PREFIX \+ "/'+re.escape(suffix)+r'", )\d+(, '+hashkey+r',)',lambda m:m[1]+str(size)+m[2],s); assert n==1
sb=s.encode(); sh=hashlib.sha256(sb).hexdigest()
c=read('caller.py').decode()
for key,value in [('VHASH',vh),('CHASH',kh),('SHASH',sh)]: c=lit(c,key,value)
for key,suffix,size,hashkey in [('supervisor','supervisor.py',len(sb),'SHASH'),('verifier','verifier.py',len(v),'VHASH'),('contract','launch-contract.json',len(kb),'CHASH')]:
    c,n=re.subn(r'("'+key+r'": \(SOURCE \+ "/'+re.escape(suffix)+r'", )\d+(, '+hashkey+r',)',lambda m:m[1]+str(size)+m[2],c); assert n==1
for name,data in [('launch-contract.json',kb),('supervisor.py',sb),('caller.py',c.encode())]: write(name,data)
print(json.dumps({'REPEAT_BECAUSE':'actual native-field-presence bytes changed all3 after initial binding','Source_changed_snapshots_rebound':True,'source_exec':False}))
