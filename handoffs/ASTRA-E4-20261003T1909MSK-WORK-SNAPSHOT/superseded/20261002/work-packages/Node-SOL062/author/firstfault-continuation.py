"""Own final first-fault/onceclose TEXT prefix repair, no Source execution."""
import os,json
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
def rep(t,a,b,n=1):
    assert t.count(a)==n,(a[:100],t.count(a),n)
    return t.replace(a,b)
texts={n:(ROOT/n).read_text() for n in ('caller.py','supervisor.py','verifier.py')}
for name in ('supervisor.py','verifier.py'):
    t=texts[name]
    t=rep(t,'        record["close_error"] = deferred_error(exc)\n','')
    t=rep(t,'    unresolved = [record for record in FD_HISTORY[:FD_HISTORY_USED] if record["state"] not in',
       '    # All known physical firstcloses have been attempted before public references.\n    for record in FD_HISTORY[:FD_HISTORY_USED]:\n        if record["close_error_object"] is not None:\n            record["close_error"] = deferred_error(record["close_error_object"])\n    unresolved = [record for record in FD_HISTORY[:FD_HISTORY_USED] if record["state"] not in')
    texts[name]=t
v=texts['verifier.py']
v=rep(v,'        secondary_public[secondary_used] = deferred_error(exc)\n','')
v=rep(v,'             "popen": None}','             "popen": None, "secondary_used": 0, "publication_error": None}')
v=rep(v,'        item["failure"] = deferred_error(exc)\n','')
v=rep(v,'''        owner["secondary_used"] = secondary_used
        item["secondary_errors"] = secondary_public[:secondary_used]''','''        owner["secondary_used"] = secondary_used
        try:
            if primary is not None:
                item["failure"] = deferred_error(primary)
            for i in range(secondary_used):
                secondary_public[i] = deferred_error(secondary_objects[i])
            item["secondary_errors"] = secondary_public[:secondary_used]
        except BaseException as exc:
            owner["publication_error"] = exc
            # Never replace the original first fault with a publication fault.''')
v=rep(v,'''    if secondary_used:
        raise secondary_objects[0]
    require(item["capture_complete"]''','''    if secondary_used:
        raise secondary_objects[0]
    if owner["publication_error"] is not None:
        raise owner["publication_error"]
    require(item["capture_complete"]''')
texts['verifier.py']=v
c=texts['caller.py']
c=rep(c,'''        graph = deferred_error(exc)
        if record is not None:
            record["error"] = graph
        require(len(FD_CLOSE_FAILURES) < 256, "bounded closure failure evidence exceeded")
        FD_CLOSE_FAILURES.append({"fd": int(fd), "lease_generation": fd.lease_generation,
            "role": fd.role, "type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "error": graph})
        raise''','''        # Error/public graphs are derived from the retained history only AFTER
        # the finite final known-owner firstclose pass. Do not replace this cause.
        raise''')
a=c.index('def close_final():'); b=c.index('\n\ndef encode_complete():',a)
c=c[:a]+(ROOT/'author/close-final.py.txt').read_text()+c[b:]
texts['caller.py']=c
for n,t in texts.items():
    fd=os.open(ROOT/n,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    with os.fdopen(fd,'w') as f: f.write(t)
print(json.dumps({'call_firstfault_rethrow_separated':True,'caller_physical_close_before_publication':True,'retire_native_error_not_replaced_by_projection':True,'source_exec':False}))
