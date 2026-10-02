"""Own inert routing/JSON metadata and acyclic SHA-size rebinding only."""
from pathlib import Path
import os, json, hashlib, re
ROOT=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
OLD='/var/tmp/friday-lab863-a172-node-whole6-all35-error-raw-onceclose-source-closure'
def save(p,b):
    if isinstance(b,str): b=b.encode()
    with p.open('wb') as h: h.write(b)
    os.chmod(p,0o600)
def dump(p,x): save(p,json.dumps(x,indent=2,ensure_ascii=True)+'\n')
def digest(p):
    b=p.read_bytes(); return len(b),hashlib.sha256(b).hexdigest()
# Preserve other error fields, add original complete causal graph, no truncation.
for name in ('caller.py','supervisor.py','verifier.py'):
    p=ROOT/name; s=p.read_text()
    s=s.replace('"stage": "freeze_or_descendants", "type": type(exc).__name__, "message": str(exc)',
                '"stage": "freeze_or_descendants", "error": exact_error(exc)')
    s=re.sub(r'"stage": ("[^"]+"), "type": type\(exc\).__name__, "message": str\(exc\)',
             r'"stage": \1, "error": exact_error(exc)',s)
    s=s.replace('"role":fd.role,"type":type(exc).__name__,"errno":getattr(exc,"errno",None)',
                '"role":fd.role,"type":type(exc).__name__,"errno":getattr(exc,"errno",None),"error":exact_error(exc)')
    if name=='supervisor.py' and '    # This is an explicitly incomplete refusal receipt' in s:
        a=s.index('except BaseException as exc:\n    qualified = False\n    # This is an explicitly incomplete refusal receipt')
        b=s.index('\ntry:\n    emit(public)',a)
        s=s[:a]+'''except BaseException as exc:
    qualified = False
    # Keep original R/raw/cause graph. No shortened/hash-only receipt is proof.
    ERROR_OBJECTS.append(exc)
    R["publication_failure"] = exact_error(exc)
    os._exit(3 if STOP else 2)  # full receipt fit CODE_OPEN, never new output cap
'''+s[b:]
    # Full-normal consumers and producers must share current new record schemas.
    s=s.replace('friday.e4.node.supervised-launch-contract.a172.v1','friday.e4.node.supervised-launch-contract.sol060.v1')
    s=s.replace('friday.e4.node.ordinary-case-expectations.a172.v1','friday.e4.node.ordinary-case-expectations.sol060.v1')
    save(p,s)
vsize,vhash=digest(ROOT/'verifier.py')
contract=json.loads((ROOT/'launch-contract.json').read_bytes().replace(OLD.encode(),str(ROOT).encode()).replace(b'9d906e5e6fb2c8f54929da2ef50399138afceef1fb16a1092ca965fcb3efa8d9',vhash.encode()))
def replace_size(x):
    if isinstance(x,dict):
        if x.get('path')==str(ROOT/'verifier.py') and x.get('bytes')==62097: x['bytes']=vsize
        for v in x.values(): replace_size(v)
    elif isinstance(x,list):
        for v in x: replace_size(v)
replace_size(contract)
contract['schema']='friday.e4.node.supervised-launch-contract.sol060.v1'
contract['error_raw_onceclose']='SOL060 prospective same-owner allocation records, no numeric retired cache, UNKNOWN before native firstclose; raw stdout/stderr/status/writable preowned captured once with full reversible bytes/count/SHA/custody/rc/production charges before stream close; original+secondary causal graphs held. Exact canonical same-object raw preimages avoid redundant wire copies. ALL resource/end/GPG roles unchanged. Mandatory full prefix/producer/error/history bounds and actual complete inner32KiB/supervisor64KiB fit remain CODE_OPEN; no previous review/qualification transferred.'
contract['future_Root_requirements']['SOL060']='Different-author WHOLE exact Source6/new19/error/fullraw/reference grammar/finite histories+all35 shapes+bounds; actual new receiver binding required before original all6 authorized effects. SOURCE_READY/ROOT_ADMISSION/GO remain false; no incomplete capture or failed full serialization is acceptance.'
dump(ROOT/'launch-contract.json',contract)
csize,chash=digest(ROOT/'launch-contract.json')
p=ROOT/'supervisor.py'; s=p.read_text().replace('9d906e5e6fb2c8f54929da2ef50399138afceef1fb16a1092ca965fcb3efa8d9',vhash).replace('587b0beeb6b2b45980463c1d1032efc5c4430d7798f36c42d0e54279c8fd6418',chash)
s=s.replace('"/verifier.py", 62097, SOURCE_SHA','"/verifier.py", '+str(vsize)+', SOURCE_SHA').replace('"/launch-contract.json", 48250, CONTRACT_SHA','"/launch-contract.json", '+str(csize)+', CONTRACT_SHA')
save(p,s); ssize,shash=digest(p)
p=ROOT/'caller.py'; s=p.read_text().replace('9d906e5e6fb2c8f54929da2ef50399138afceef1fb16a1092ca965fcb3efa8d9',vhash).replace('587b0beeb6b2b45980463c1d1032efc5c4430d7798f36c42d0e54279c8fd6418',chash).replace('bcd0fb9f0c5d68468aa896b4a6f9a36f5e32a5d62dc11c6c3bc3787cfa128d10',shash)
s=s.replace('"/supervisor.py", 89782, SHASH','"/supervisor.py", '+str(ssize)+', SHASH').replace('"/verifier.py", 62097, VHASH','"/verifier.py", '+str(vsize)+', VHASH').replace('"/launch-contract.json", 48250, CHASH','"/launch-contract.json", '+str(csize)+', CHASH')
save(p,s)
exp=json.loads((ROOT/'expectations.json').read_bytes().replace(OLD.encode(),str(ROOT).encode()))
exp['schema']='friday.e4.node.ordinary-case-expectations.sol060.v1'
publication=exp['publication_source_contract']
publication.update(schema='friday.sol060.lossless-raw-preimage-publication.v1',case_payload_bound_bytes=[196644]*6,all_case_payload_bound_bytes=196644*6,complete_bound_with_newline_bytes=489472+196644*6+1,fixed_producer_encoding='full raw once; exact canonical same-object preimage refs, checked reversible before emission')
exp['sol060_raw_error_reference_qualification']={'schema':'friday.sol060.same-raw-object.v1','new_error_graph':'friday.sol060.error-graph.v1','mandatory_fullraw':True,'raw_truncation_or_hashonly_proof':False,'closed_all35_metadata_and_history_bound':'CODE_OPEN','inner32KiB_and_supervisor64KiB_fit':'CODE_OPEN','old_A175_bound_transfer':False}
dump(ROOT/'expectations.json',exp)
print(json.dumps({'verifier':[vsize,vhash],'contract':[csize,chash],'supervisor':[ssize,shash],'caller':digest(ROOT/'caller.py'),'expectations':digest(ROOT/'expectations.json'),'whole':'CODE_OPEN','runtime':'NOT_RUN'}))
