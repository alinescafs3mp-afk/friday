"""Own exact TEXT continuation, with original templates and no supplied execution."""
import os,re,json,hashlib
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
def read(rel): return (ROOT/rel).read_text()
def write(rel,text):
    fd=os.open(ROOT/rel,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    with os.fdopen(fd,'w') as f: f.write(text)
def rep(text,a,b,n=1):
    assert text.count(a)==n,(a[:90],text.count(a),n)
    return text.replace(a,b)
projection=read('author/stock-error-projection.py.txt')
receiver='''def validate_received_error_graphs(record):
    # Whole decoded receipt is bounded by its unchanged original channel cap.
    if type(record) is dict:
        require("$private_pending_error" not in record, "private error owner is not durable public evidence")
        if record.get("schema") == "friday.sol060.error-graph.v2":
            validate_stock_error_graph(record)
            return
        for child in record.values():
            validate_received_error_graphs(child)
    elif type(record) is list:
        for child in record:
            validate_received_error_graphs(child)


'''
for name in ('caller.py','supervisor.py','verifier.py'):
    text=read(name)
    a=text.index('def exact_error(exc):'); b=text.index('def original_stock_cause(graph):',a)
    text=text[:a]+projection+'\n\n'+receiver+text[b:]
    if name!='caller.py':
        text=rep(text,'"acquire_error_object": None, "close_error_object": None,','"acquire_error_object": None, "acquire_error": None, "close_error_object": None,')
        text=rep(text,'\n        record["acquire_error_object"] = exc\n','\n        record["acquire_error_object"] = exc\n        record["acquire_error"] = deferred_error(exc)\n')
        text=rep(text,'\n            record["acquire_error_object"] = exc\n','\n            record["acquire_error_object"] = exc\n            record["acquire_error"] = deferred_error(exc)\n')
        text=rep(text,'"generation", "where", "number", "state", "attempted_once", "close_error"','"generation", "where", "number", "state", "attempted_once", "acquire_error", "close_error"')
    else:
        # Exact acquisition faults and partial wrappers must reach the full history.
        text=rep(text,'\n        record["error_object"] = exc\n        raise','\n        record["error_object"] = exc\n        record["error"] = deferred_error(exc)\n        raise')
        text=rep(text,'\n            record["error_object"] = exc\n        raise','\n            record["error_object"] = exc\n            record["error"] = deferred_error(exc)\n        raise')
    if name in ('caller.py','supervisor.py'):
        text=rep(text,'    finite(value)\n    return value','    finite(value)\n    validate_received_error_graphs(value)\n    return value')
    # Preserve resource schemas/role/oracles; current actor envelope is coherently versioned.
    for old,new in (
        ('friday.e4.node.bounded-verifier.sol060.v1','friday.e4.node.bounded-verifier.sol062.v1'),
        ('friday.e4.node.actual-supervisor.sol060.v1','friday.e4.node.actual-supervisor.sol062.v1'),
        ('friday.e4.node.ordinary-stock-outer.sol060.v1','friday.e4.node.ordinary-stock-outer.sol062.v1'),
        ('friday.e4.node.ordinary-case-expectations.sol060.v1','friday.e4.node.ordinary-case-expectations.sol062.v1')):
        text=text.replace(old,new)
    write(name,text)
print(json.dumps({'joint_graph_producer_and_receivers':3,'late_error_refs_materialized':True,'source_exec':False}))
