"""Own stock JSON/full literal acyclic binding; no supplied Source parsing/exec."""
import os,json,re,hashlib
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
OLD='/var/tmp/friday-lab865-sol060-a182-node-whole6-all35-all6-raw-bound-schema-connected-source-closure'
def raw(name): return (ROOT/name).read_bytes()
def write(name,data):
    if isinstance(data,str): data=data.encode()
    fd=os.open(ROOT/name,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    with os.fdopen(fd,'wb') as f: f.write(data)
def newjson(obj): return (json.dumps(obj,indent=2,ensure_ascii=False)+'\n').encode()
def rep(t,a,b):
    assert t.count(a)==1,(a[:100],t.count(a))
    return t.replace(a,b)
def literal(t,key,value):
    pat=re.compile(r'^'+re.escape(key)+r' = "[0-9a-f]{64}"$',re.M)
    t,n=pat.subn(key+' = "'+value+'"',t); assert n==1,key
    return t
def paths(obj):
    if isinstance(obj,str): return obj.replace(OLD,str(ROOT))
    if isinstance(obj,list): return [paths(v) for v in obj]
    if isinstance(obj,dict): return {k:paths(v) for k,v in obj.items()}
    return obj
E=paths(json.loads(raw('expectations.json'))); original_cases=E['cases']; original_order=E['case_order']
E['schema']='friday.e4.node.ordinary-case-expectations.sol062.v1'
scope='ARITHMETIC_PARTITION_ONLY_NOT_NEW_STOCK_FACTORY_WIDTH_OR_FULL_DELIVERY_PROOF'
E['publication_source_contract']['bound_scope']=scope
E['sol062_stock_error_raw_qualification']=E.pop('sol060_raw_error_reference_qualification')
E['sol062_stock_error_raw_qualification'].update(
    consumer_join='validate_stock_error_graph(full nodes/values/bytes/aliases) then actual nodes[root] stock cause',
    private_error_journal='fixed original256; no graph or str in catch; complete v2 only at publication boundary',
    exception_error_ref='original error aliases in args/state/notes/groups; joint error/value frontiers',
    projection_failure_before_later_raw_or_knownclose='moved outside designated loops; full stock capacity/implicit allocation proof CODE_OPEN',
    actual_complete_factory_bound='CODE_OPEN',
    original_domain='qualified stock32/path160/clock32/shared900; not universal C heap/frame-local/nativecustom')
assert E['cases']==original_cases and E['case_order']==original_order
eb=newjson(E)
c=raw('caller.py').decode()
c=rep(c,'    "fixed_metadata_bound_bytes": PUBLIC_FIXED_RESERVE,','    "bound_scope": "'+scope+'",\n    "fixed_metadata_bound_bytes": PUBLIC_FIXED_RESERVE,')
# Verifier is the first sealed Source dependency; no current self-hash literal.
v=raw('verifier.py'); vh=hashlib.sha256(v).hexdigest()
K=paths(json.loads(raw('launch-contract.json')))
old_vh=K['source']['sha256']
def current_hashes(obj):
    if isinstance(obj,str): return vh if obj==old_vh else obj
    if isinstance(obj,list): return [current_hashes(x) for x in obj]
    if isinstance(obj,dict): return {k:current_hashes(x) for k,x in obj.items()}
    return obj
K=current_hashes(K)
K['schema']='friday.e4.node.supervised-launch-contract.sol062.v1'
K['source']['bytes']=len(v)
K['independent_prelaunch_custody']['inherited_fds']['5']['bytes']=len(v)
K['error_raw_onceclose']='SOL062 complete deferred original256 journal; joint full stock error/value frontiers and full bothside v2 grammar/bytes/refs; late error aliases retained, no public pending markers. Primary/secondary raw owners precede publication. Per-call finite secondary/capture slots and physical-first known-owner close; both transport cells attempted before fallible copying, pending commits masked without resetting deadline, failed reads not retried. Original onceFD/stock_cause/typed resource/oracles/roles/caps retained. New full factory/width/history/inner32KiB/super64KiB/outer2MiB/durable-delivery proof CODE_OPEN. No runtime credit.'
K['current_actor_receipt_schemas']={'caller':'friday.e4.node.ordinary-stock-outer.sol062.v1','supervisor':'friday.e4.node.actual-supervisor.sol062.v1','verifier':'friday.e4.node.bounded-verifier.sol062.v1'}
K['sol062_error_publication']={'schema':'friday.sol060.error-graph.v2','private_pending_marker_not_public':True,'full_node_value_reference_byte_check':True,'width_factory_native_cost_bound':'CODE_OPEN','complete_durable_before_native_end':'CODE_OPEN','original_arenas_unchanged':{'error_owners':256,'descriptor_history':256,'nodes_per_graph':128,'values_per_graph':128,'frames_per_node':128}}
K['future_Root_requirements']['SOL062']=K['future_Root_requirements'].pop('SOL060')
K['future_Root_requirements']['SOL062']='Independent WHOLE exact final Source6/actual19/numeric9/rawmanifest/self-crosshash/new graph shapes/prefixes/resources/factories before any effects; SourceReady/Root_admission/GO=false. Full raw and accepted durable publication remain mandatory; no capraise/oraclecut/new role.'
kb=newjson(K); kh=hashlib.sha256(kb).hexdigest()
s=raw('supervisor.py').decode(); s=literal(s,'SOURCE_SHA',vh); s=literal(s,'CONTRACT_SHA',kh)
s,n=re.subn(r'("source": \(PREFIX \+ "/verifier.py", )\d+(, SOURCE_SHA,)',lambda m:m[1]+str(len(v))+m[2],s); assert n==1
s,n=re.subn(r'("contract": \(PREFIX \+ "/launch-contract.json", )\d+(, CONTRACT_SHA,)',lambda m:m[1]+str(len(kb))+m[2],s); assert n==1
sb=s.encode(); sh=hashlib.sha256(sb).hexdigest()
c=literal(c,'VHASH',vh); c=literal(c,'CHASH',kh); c=literal(c,'SHASH',sh)
for key,suffix,size,hashkey in [('supervisor','supervisor.py',len(sb),'SHASH'),('verifier','verifier.py',len(v),'VHASH'),('contract','launch-contract.json',len(kb),'CHASH')]:
    pat=re.compile(r'("'+key+r'": \(SOURCE \+ "/'+re.escape(suffix)+r'", )\d+(, '+hashkey+r',)')
    c,n=pat.subn(lambda m:m[1]+str(size)+m[2],c); assert n==1,key
# Every literal relation is prepared before writing any current dependency.
for name,data in [('expectations.json',eb),('launch-contract.json',kb),('supervisor.py',sb),('caller.py',c.encode())]: write(name,data)
print(json.dumps({'acyclic_chain':['verifier','contract','supervisor','caller','external_manifest'],'new6_sizes':{n:len(raw(n)) for n in ('verifier.py','launch-contract.json','supervisor.py','caller.py','expectations.json','ordinary-surplus.txt')},'SourceReady':False,'source_exec':False}))
