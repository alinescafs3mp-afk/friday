"""Own bounded stock mechanical/error-codec and acyclic binding refresh."""
from pathlib import Path
import os,json,hashlib,re
ROOT=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
def save(p,s):
    with p.open('w') as h: h.write(s)
    os.chmod(p,0o600)
def pin(p):
    b=p.read_bytes();return len(b),hashlib.sha256(b).hexdigest()
body=(ROOT/'author/error_graph.py.txt').read_text()
for name in ('caller.py','supervisor.py','verifier.py'):
    p=ROOT/name;s=p.read_text();a=s.index('ERROR_OBJECTS = []');b=s.index('FD_ALLOCATION_HISTORY = []' if name=='caller.py' else 'class AllocationFD',a)
    save(p,s[:a]+body+s[b:])
old=json.loads((ROOT/'launch-contract.json').read_bytes())
oldv=old['source']['sha256'];oldvsize=old['source']['bytes'];vs,vh=pin(ROOT/'verifier.py')
s=(ROOT/'launch-contract.json').read_text().replace(oldv,vh)
x=json.loads(s)
def walk(v):
    if isinstance(v,dict):
        if v.get('path')==str(ROOT/'verifier.py') and v.get('bytes')==oldvsize:v['bytes']=vs
        for a in v.values():walk(a)
    elif isinstance(v,list):
        for a in v:walk(a)
walk(x); oldcs,oldch=pin(ROOT/'launch-contract.json')
save(ROOT/'launch-contract.json',json.dumps(x,indent=2)+'\n');cs,ch=pin(ROOT/'launch-contract.json')
p=ROOT/'supervisor.py';s=p.read_text().replace(oldv,vh).replace(oldch,ch)
s=s.replace('"/verifier.py", '+str(oldvsize)+', SOURCE_SHA','"/verifier.py", '+str(vs)+', SOURCE_SHA').replace('"/launch-contract.json", '+str(oldcs)+', CONTRACT_SHA','"/launch-contract.json", '+str(cs)+', CONTRACT_SHA')
save(p,s);ss,sh=pin(p)
p=ROOT/'caller.py';s=p.read_text();oldsh=re.search(r'SHASH = "([0-9a-f]{64})"',s).group(1);olds=int(re.search(r'"/supervisor.py", (\d+), SHASH',s).group(1))
s=s.replace(oldv,vh).replace(oldch,ch).replace(oldsh,sh)
s=s.replace('"/verifier.py", '+str(oldvsize)+', VHASH','"/verifier.py", '+str(vs)+', VHASH').replace('"/launch-contract.json", '+str(oldcs)+', CHASH','"/launch-contract.json", '+str(cs)+', CHASH').replace('"/supervisor.py", '+str(olds)+', SHASH','"/supervisor.py", '+str(ss)+', SHASH')
save(p,s)
p=ROOT/'expectations.json';x=json.loads(p.read_bytes());x['sol060_raw_error_reference_qualification']['new_error_graph']='friday.sol060.error-graph.v2';save(p,json.dumps(x,indent=2)+'\n')
print(json.dumps({'bindings':'refreshed acyclic','verifier':[vs,vh],'contract':[cs,ch],'supervisor':[ss,sh],'caller':pin(ROOT/'caller.py'),'Source_execution':False}))
