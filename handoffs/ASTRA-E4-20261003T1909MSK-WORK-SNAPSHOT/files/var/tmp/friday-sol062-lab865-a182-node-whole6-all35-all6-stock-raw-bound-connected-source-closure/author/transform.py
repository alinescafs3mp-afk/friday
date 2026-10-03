"""Own exact bounded literal mechanical Source-text authorship; no Source exec."""
import os,json,hashlib,re
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
def read(rel): return (ROOT/rel).read_bytes()
def write(rel,text):
    p=ROOT/rel; data=text.encode() if isinstance(text,str) else text
    fd=os.open(p,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    with os.fdopen(fd,'wb') as f: f.write(data)
def replace(text,before,after,count=1):
    assert text.count(before)==count, (before[:100],text.count(before),count)
    return text.replace(before,after)
fragment=read('author/deferred-error-journal.py.txt').decode()
changed=[]
for name in ('caller.py','supervisor.py','verifier.py'):
    old=read(name); text=old.decode()
    text=text.replace('/var/tmp/friday-lab865-sol060-a182-node-whole6-all35-all6-raw-bound-schema-connected-source-closure',str(ROOT))
    # All existing catch sites become private raw-owner references, not graph projection.
    text=text.replace('exact_error(', 'deferred_error(')
    text=replace(text,'def deferred_error(exc):','def exact_error(exc):')
    start=text.index('ERROR_OBJECT_CAP = 256\n'); end=text.index('def exact_error(exc):',start)
    text=text[:start]+fragment+'\n\n'+text[end:]
    text=replace(text,'        if type(value) in (type(None), bool, int, str):','        if isinstance(value, BaseException):\n            return {"error_ref": ref(value)}\n        if type(value) in (type(None), bool, int, str):')
    text=replace(text,'        frames, tb = [], obj.__traceback__','        frames, tb = [], (ERROR_TRACEBACKS[retain_error_object(obj)]\n                          if any(obj is held for held in ERROR_OBJECTS[:ERROR_USED])\n                          else obj.__traceback__)')
    # Value processing can discover exception objects inside args/state/group.
    # Drain the error and value frontiers jointly instead of losing late refs.
    text=replace(text,'    i = 0\n    while i < len(objects):','    i = 0\n    while i < len(objects):')
    # Extend the original reader, preserving full graph as the actual failure.
    start=text.index('def original_stock_cause(graph):'); end=text.index('\n\n',text.index('    return stock',start))
    text=text[:start]+'def original_stock_cause(graph):\n    return validate_stock_error_graph(graph)'+text[end:]
    if name=='verifier.py':
        text=replace(text,'def encode_bounded(record):\n','def encode_bounded(record):\n    publish_errors(record)\n')
    elif name=='supervisor.py':
        text=replace(text,'def encode(record):\n','def encode(record):\n    publish_errors(record)\n')
    else:
        text=replace(text,'def public_partition_bytes():\n','def public_partition_bytes():\n    publish_errors(R)\n')
        # Retain the actual native close fault before making a private reference.
        text=replace(text,'        graph = deferred_error(exc)\n        if record is not None:\n            record["error_object"] = exc\n            record["error"] = graph',
          '        if record is not None:\n            record["error_object"] = exc\n        graph = deferred_error(exc)\n        if record is not None:\n            record["error"] = graph')
    write(name,text); changed.append({'file':name,'before':hashlib.sha256(old).hexdigest(),'after':hashlib.sha256(text.encode()).hexdigest(),'bytes':len(text.encode())})
print(json.dumps({'actual_connected_text_delta':changed,'source_exec':False}))
