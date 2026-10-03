"""Own connected TEXT commit-boundary continuation; no actor execution."""
import os,json
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
def rep(t,a,b,n=1):
    assert t.count(a)==n,(a[:100],t.count(a),n)
    return t.replace(a,b)
c=(ROOT/'caller.py').read_text(); s=(ROOT/'supervisor.py').read_text()
a=c.index('def drain_pipes('); b=c.index('\n\ndef entry_fds(',a)
c=c[:a]+(ROOT/'author/drain-pipes.py.txt').read_text()+c[b:]
c=rep(c,'"pending_chunk": None, "eof": False}',
    '"pending_chunk": None, "eof": False, "read_error_object": None,\n                          "read_error": None, "capture_error_object": None}')
# Serialize every observed cell ONLY after attempting both physical channels.
a=s.index('def retain_produced_transport():'); b=s.index('\n\ndef register_parent(',a)
body=s[a:b]
body=rep(body,'            R["output"][item["label"]] = transport_projection(item)\n','')
body+='''    first_projection = None
    for item in TRANSPORT:
        try:
            R["output"][item["label"]] = transport_projection(item)
        except BaseException as exc:
            item["retain_error_object"] = exc
            item["partial"] = True
            item["retain_error"] = deferred_error(exc)
            first_projection = first_projection or exc
    if first_projection is not None:
        raise first_projection
'''
s=s[:a]+body+s[b:]
for name,text in (('caller.py',c),('supervisor.py',s)):
    fd=os.open(ROOT/name,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    with os.fdopen(fd,'w') as f: f.write(text)
print(json.dumps({'bothchannel_attempt_before_projection':True,'failed_read_not_retried':True,'pending_raw_not_overwritten':True,'source_exec':False}))
