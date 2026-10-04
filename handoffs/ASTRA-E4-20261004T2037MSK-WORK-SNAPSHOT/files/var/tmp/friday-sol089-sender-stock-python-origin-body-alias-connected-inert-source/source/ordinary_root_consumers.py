"""A132 all32 causal consumers of the independently Root-held chronology.

No candidate is imported here. Existing native/domain schemas are inert JSON
inputs pinned in all32-contract-bindings.json. Source producer claims and Root
recipient observations remain separate. None of these functions issues a grant
or supplies today's PID/time/usage/custody values. Unknowns refuse full closure.
The graph is executable Source, not an executed golden or independent review.
"""
import hashlib
import json
import math
import re
import time
import source_grammar as G
import native_grammar as N
import sender_operand_codec as O

CASES = (
    "positive", "nonzero-dispatcher", "body", "deadline", "pid-generation", "pin",
    "read-write", "stale-identity", "unsealed", "wrong-field", "wrong-role",
    "adopted-child-pending", "blocked-SIGCHLD", "body-context-bool", "clock-drift",
    "controller-output-bound", "final-ancestor-boundary", "handle-close-error",
    "initial-scope-nonempty", "ordinary-regular-artifact-refusal", "proc-acquisition-error",
    "report-custody-bool", "report-namespace-bool", "report-resource-bool", "sender-read-bound",
    "spawn-constructor-error", "strict-inherited-fd", "terminal-original-end",
    "terminal-pipe-error", "terminal-tree-list-missed", "usage-integer-bool", "wait4-unavailable")

FIRST_CAUSES = {
    "body": ("issuer_body", "canonical object wire"), "deadline": ("fixed_deadline",),
    "pid-generation": ("pid_generation",), "pin": ("pin_mismatch",),
    "read-write": ("fd_not_readonly",), "stale-identity": ("stale_identity",),
    "unsealed": ("seals_incomplete",), "wrong-field": ("exact_key_set",),
    "wrong-role": ("phase_or_role",), "blocked-SIGCHLD": ("SIGCHLD_blocked",),
    "body-context-bool": ("phase_or_role", "fixed_deadline", "resources_topology_changed"),
    "clock-drift": ("clock drift",), "final-ancestor-boundary": ("stale_identity",),
    "initial-scope-nonempty": ("initial_own_scope_not_empty",),
    "report-custody-bool": ("controller_report",),
    "report-namespace-bool": ("ancestry_spec_mismatch", "controller_report"),
    "report-resource-bool": ("resources_topology_changed",), "sender-read-bound": ("read_bound",),
    "usage-integer-bool": ("telemetry_type",)}

VARIABLE_KEYS = {"holder_pid", "parent_pid", "holder_start_ticks", "wall0", "mono0",
    "original_wall_end", "original_monotonic_end", "identities", "wait4_status",
    "wait4_usage", "fd_names", "error_errno"}

def require(ok, cause):
    if not ok:
        raise ValueError(cause)

def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
        separators=(",", ":")) + "\n").encode("ascii")

def same_typed(left,right):
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return set(left)==set(right) and all(same_typed(left[key],right[key]) for key in left)
    if type(left) in (list,tuple):
        return len(left)==len(right) and all(same_typed(a,b) for a,b in zip(left,right))
    return left==right

def decode(value,held=None,defer_stock=False):
    if value is None or type(value) in (str,bool,int):
        return value
    if type(value) is list:
        return [decode(v,held,defer_stock) for v in value]
    if type(value) is dict:
        kind = value.get("type")
        if kind=="stock_operand_member":
            require(type(value) is dict and set(value)=={"type","graph","member"}
                and held is not None and getattr(held,"operand_registry",None) is not None,
                "whole stock member graph in actual Root custody")
            held.operand_registry.validate(value["graph"])
            if defer_stock:return value
            return held.operand_registry.member(value)
        if kind=="stock_operand_graph":
            require(held is not None and getattr(held,"operand_registry",None) is not None,
                "actual Root-held full stock body registry, not Source-issued authority")
            registry=held.operand_registry
            registry.validate(value)
            if defer_stock:return value
            return registry.decode(value)
        if kind=="buffer_snapshot":
            require(set(value)=={"type","buffer_kind","bytes","nbytes","readonly","format",
                "itemsize","ndim","shape","strides","suboffsets","contiguous"}
                and value["buffer_kind"] in ("bytearray","memoryview")
                and type(value["nbytes"]) is int and value["nbytes"]>=0,
                "exact full actual raw buffer snapshot schema")
            raw=decode(value["bytes"],held,defer_stock)
            require(type(raw) is bytes and len(raw)==value["nbytes"],"all actual mutable/view bytes retained")
            return {**value,"bytes":raw}
        if kind=="error_ref":
            require(set(value)=={"type","id"} and type(value["id"]) is int and value["id"]>=0,"exact causal argument alias")
            return value
        if kind=="held_bytes":
            require(held is not None,"Source byte reference requires an actual independently Root-held full object")
            return held.full_byte_reference(value)
        if kind == "float_hex":
            require(set(value)=={"type","value"} and type(value["value"]) is str,
                "exact typed float representation")
            actual=float.fromhex(value["value"])
            require(math.isfinite(actual) and actual.hex()==value["value"],"canonical finite hex float")
            return actual
        if kind == "mapping":
            require(set(value)=={"type","value"} and type(value["value"]) is list,"exact mapping representation")
            result={}
            for pair in value["value"]:
                require(type(pair) is list and len(pair)==2,"exact ordered mapping pair")
                key,item=decode(pair[0],held,defer_stock),decode(pair[1],held,defer_stock)
                require(key not in result,"duplicate decoded mapping key refused, never overwritten")
                result[key]=item
            return result
        if kind == "bytes":
            require(set(value)=={"type","length","raw_hex","sha256"}
                and type(value["length"]) is int and value["length"]>=0
                and type(value["raw_hex"]) is str and type(value["sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}",value["sha256"]) is not None,"exact full byte representation")
            raw = bytes.fromhex(value["raw_hex"])
            require(raw.hex()==value["raw_hex"] and len(raw) == value["length"] and hashlib.sha256(raw).hexdigest() == value["sha256"],
                "exact whole returned bytes, no projection")
            return raw
        if kind in ("identity9", "set"):
            require(set(value)=={"type","value"} and type(value["value"]) is list,"exact typed collection")
            if kind=="identity9":
                require(len(value["value"])==9 and all(type(v) is int and v>=0 for v in value["value"]),
                    "exact full identity9, bool refused")
            return decode(value["value"],held,defer_stock)
        if kind == "path":
            require(set(value)=={"type","value"} and type(value["value"]) in (str,bytes),"exact represented path")
            return value["value"]
        if kind=="completed_process":
            require(set(value)=={"type","args","returncode","stdout","stderr"}
                and type(value["returncode"]) is int,"exact actual completed process result")
            return {key:decode(item,held,defer_stock) for key,item in value.items()}
        shapes={"selector_key":{"type","fd","events","data"},
            "selector_map":{"type","entries"},
            "callable_reference":{"type","module","qualname"},
            "future_state":{"type","done","cancelled"},
            "integer_subtype":{"type","name","value"},
            "file_handle":{"type","fd","name"},"process":{"type","pid","returncode"}}
        require(kind in shapes and set(value)==shapes[kind],"unknown or extra typed representation refused")
        return {key:decode(item,held,defer_stock) for key,item in value.items()}
    raise ValueError("unrepresented Source value type")

def source_error(error,held=None):
    if error is None:
        return None
    require(type(error) is dict and set(error)=={"type","module","errno","args","text","filename","filename2","graph"}
        and type(error["module"]) is str
        and type(error["type"]) is str and type(error["text"]) is str
        and (error["errno"] is None or type(error["errno"]) is int),"full original Source exception grammar")
    graph=error['graph']
    require(type(graph) is dict and set(graph)=={'schema','root','nodes','truncated'}
        and graph['schema']=='friday.error-graph.v1' and graph['root']==0
        and graph['truncated'] is False and type(graph['nodes']) is list and graph['nodes'],
        'full graph required, no bounded projection silently promoted to raw custody')
    def argument_links(part):
        if type(part) is list:
            for item in part:argument_links(item)
        elif type(part) is dict:
            if part.get('type')=='error_ref':
                require(set(part)=={'type','id'} and type(part['id']) is int
                    and 0<=part['id']<len(graph['nodes']),'argument error alias resolves in this exact causal graph')
            else:
                for item in part.values():argument_links(item)
    for number,node in enumerate(graph['nodes']):
        require(set(node)=={'id','type','module','args','text','errno','filename','filename2',
            'state','notes','frames','cause','context','suppress_context','groups'}
            and type(node['id']) is int and node['id']==number
            and type(node['type']) is str and type(node['module']) is str and type(node['text']) is str
            and type(node['groups']) is list,
            'complete exact graph node and retained alias identity')
        for link in [node['cause'],node['context'],*node['groups']]:
            require(link is None or type(link) is int and 0<=link<len(graph['nodes']), 'all causal links resolve')
        require(type(node['suppress_context']) is bool and type(node['frames']) is list,
            'suppressed context is retained separately, no missing traceback suffix')
        for key in ('args','state','notes','filename','filename2'):
            argument_links(node[key]);decode(node[key],held)
    require(all(same_typed(graph['nodes'][0][key],error[key])
        for key in ('type','module','errno','args','text','filename','filename2')),
        'same original event error and causal graph root')
    return {**error,"args":decode(error["args"],held),"filename":decode(error["filename"],held),
        "filename2":decode(error["filename2"],held)}

class RootChronology:
    def __init__(self, independently_held_ledger):
        # The actual future Root owns/seals this object. Source cannot substitute
        # a producer's wire or metadata dictionary for the held reader operation.
        self.held = independently_held_ledger
        self.source = []
        self.root = []
        self.actor_last = {}
        self.actor_terminal = {}
        self.operand_after = {}
        for index, row in enumerate(independently_held_ledger.rows_from_held()):
            if row["namespace"] == "Root_recipient":
                self.root.append((index, row))
                continue
            require(row["namespace"] == "Source_producer", "Root exact two observation namespaces")
            event = row["event"]
            require(type(row) is dict and set(row)=={"namespace","event"}
                and type(event) is dict and set(event)=={"schema","actor","pid","parent_pid","sequence",
                    "operation","arguments","result","error","producer_data_not_Root_authority"}
                and event["schema"]=="friday.a137.actor-event.v3"
                and type(event["actor"]) is str and type(event["parent_pid"]) is int
                and event["parent_pid"]>0 and type(event["operation"]) is str
                and event["producer_data_not_Root_authority"] is True,"full exact Source event grammar")
            pid, sequence = event["pid"], event["sequence"]
            require(type(pid) is int and pid > 0 and type(sequence) is int
                and sequence == self.actor_last.get(pid, -1) + 1, "Root full per-actor chronology")
            self.actor_last[pid] = sequence
            registry=getattr(self.held,"operand_registry",None)
            if event["operation"]=="selector.allocate" and event["error"] is None:
                require(registry is not None,"actual selected stock image/body registry")
                registry.ingest_birth(index,event,event["result"])
            decoded = {**event, "arguments": decode(event["arguments"],self.held,True), "result": decode(event["result"],self.held,True),
                "error":source_error(event["error"],self.held)}
            if event["operation"] == "bootstrap.reached_row":
                row = decoded["result"]
                require(type(row) is list and len(row) == 5, "Root full original reached bootstrap row")
                # Keep the actual arrival row and the original within-bootstrap
                # order. These Source results stay Source data in Root custody.
                def bootstrap_value(item):
                    if type(item) is list:
                        if item and item[0] == "bytes_full":
                            raw = bytes.fromhex(item[2])
                            require(len(raw) == item[1], "full bootstrap byte value")
                            return raw
                        if item and item[0] == "float_hex":
                            return float.fromhex(item[1])
                        # The old FullEventJournal represents dictionary entries
                        # as ordered pairs; only string-key pairs form a mapping.
                        if item and all(type(pair) is list and len(pair) == 2
                                and type(pair[0]) is str for pair in item):
                            result={}
                            for pair in item:
                                require(pair[0] not in result,"duplicate bootstrap mapping key refused")
                                result[pair[0]]=bootstrap_value(pair[1])
                            return result
                        return [bootstrap_value(v) for v in item]
                    return item
                result = row[3]
                require(type(result) is list and (len(result)==2 and result[0]=="returned"
                    or len(result)==9 and result[0]=="error"),"full bootstrap result/error grammar")
                decoded={**decoded,"operation":row[1],
                    "arguments":[row[0],bootstrap_value(row[2]),row[4]],
                    "result":bootstrap_value(result[1]) if result[0] == "returned" else None,
                    "error":None if result[0] == "returned" else {"type":result[1],"errno":result[2],
                        "module":result[3],"args":bootstrap_value(result[4]),"text":result[5],
                        "filename":bootstrap_value(result[6]),"filename2":bootstrap_value(result[7]),
                        'graph':bootstrap_value(result[8])},
                    "bootstrap_original_row":True,"actual_arrived_event":decoded}
            self.source.append((index, decoded))
            if event["operation"]=="selector.body.after":
                require(event["error"] is None and registry is not None,"whole original Python post-body cut required")
                registry.validate(event["result"])
                owner,args,scope=decoded["arguments"]
                require(type(args) is list and len(args)==2 and type(args[0]) is str
                    and type(args[1]) is int and args[1]>=0,"actual completed source sequence, not an expected body")
                key=(pid,args[1])
                require(key not in self.operand_after,"one post-body per actually completed effect")
                completed=[(n,e) for n,e in self.source if n<index and e["pid"]==pid and e["sequence"]==args[1]]
                require(len(completed)==1 and completed[0][1]["operation"]==args[0]
                    and completed[0][1]["arguments"][0]==owner,"post-body joined to same actual effect/owner generation")
                self.operand_after[key]=(index,event["result"])
            if event["operation"] == "actor.terminal":
                require(pid not in self.actor_terminal, "Root original terminal unique")
                self.actor_terminal[pid] = decoded
        self.terminal = [r for _, r in self.root if r["operation"] == "postdelivery.actual_terminal"]
        require(len(self.terminal)<=1,"Root terminal cannot duplicate or overwrite chronology")
        self.failed_prefix=[r for _,r in self.root if r["operation"]=="collector.failed_prefix"]
        require(self.terminal or len(self.failed_prefix)==1,"actual complete terminal or explicit held failed prefix required")

    def first_guard(self):
        for index, event in self.source:
            if event["operation"] == "guard.refused":
                args = event["arguments"]
                require(type(args) is list and len(args) == 3 and args[0] == "sender",
                    "actual sender guard and reached scope")
                return index, args[1][0], args[2]
        return None

    def operations(self, name):
        return [(index, event) for index, event in self.source if event["operation"] == name]

    def bind_variables(self, values, bindings,case,fault):
        require(type(values) is dict and set(values) == VARIABLE_KEYS
            and type(bindings) is dict and set(bindings) == VARIABLE_KEYS, "all original variables explicitly bound")
        roots = {index: row for index, row in self.root}
        sources = {index: event for index, event in self.source}
        purposes={
            "holder_pid":("controller.custody_independently_held",("holder_pid",)),
            "holder_start_ticks":("controller.custody_independently_held",("holder_start_ticks",)),
            "parent_pid":("Root.child.held_generation",("pid",)),
            "wall0":("original.anchors",("result","wall0")),
            "mono0":("original.anchors",("result","mono0")),
            "original_wall_end":("sender.returned_outcome",("result","delivery","original_wall_end")),
            "original_monotonic_end":("sender.returned_outcome",("result","delivery","original_monotonic_end")),
            "identities":("grammar.enter",("arguments",1,1,0,"identities")),
            "wait4_status":("wait4",("result",1)),
            "wait4_usage":("wait4",("result",2)),
            "fd_names":("listdir",("result",)),
            "error_errno":(None,("error","errno"))}
        for name in VARIABLE_KEYS:
            reference = bindings[name]
            operation,path=purposes[name]
            candidates=[]
            for index,row in self.root+self.source:
                if operation is not None and row["operation"]!=operation:continue
                if name=="identities" and (row.get("arguments",[None,[]])[1][:1]!=["validate_claim"]):continue
                if name=="parent_pid" and row.get("relation")!="direct_caller":continue
                if name in ("wait4_status","wait4_usage") and (not row.get("result") or row["result"][0]!=values["holder_pid"]):continue
                try:observed=node(row,list(path))
                except (KeyError,IndexError,TypeError):continue
                if name=="fd_names":
                    packed=row.get("arguments")
                    if not (type(packed) is list and len(packed)==3 and packed[0]=="sender"
                        and packed[1][0]==["/proc/self/fd"] and "Session.fd_guard" in packed[2]):continue
                if name=="error_errno":
                    if observed is None:continue
                    # Errno belongs to the case's ACTUAL first fault, not to a
                    # convenient arbitrary later recorder/cleanup/native error.
                    if type(fault) is not dict or index!=fault.get("event_index"):continue
                candidates.append((index,row,observed))
            if values[name] is None or name in ("identities","fd_names") and values[name]==[]:
                if name=="error_errno" and case in ("positive","nonzero-dispatcher","ordinary-regular-artifact-refusal"):
                    outcomes=self.operations("sender.returned_outcome")
                    require(len(outcomes)==1 and reference=={"state":"NO_INITIATING_ERRNO","returned_index":outcomes[0][0]}
                        and outcomes[0][1]["result"]["status"]=="RUN_REPORTED",
                        "original no-initiating-errno domain binds a reached complete ordinary return, not event absence")
                    continue
                if name=="error_errno" and type(reference) is dict and reference.get("state")=="REACHED_NONE":
                    require(set(reference)=={"state","event_index"} and reference["event_index"] in sources,
                        "reached None errno has its exact actual original exception")
                    observed=sources[reference["event_index"]]
                    first=self.first_guard()
                    require(observed["error"] is not None and observed["error"]["errno"] is None
                        and first is not None and observed["error"]["type"]=="Refusal"
                        and observed["error"]["text"]==first[1]
                        and observed["arguments"][-1]==first[2],
                        "original first refusing scope has a reached non-errno guard fault")
                    continue
                if case=="strict-inherited-fd" and type(reference) is dict and reference.get("state")=="NOT_REACHED":
                    entered=[e for _,e in self.source if e["operation"]=="method.enter" and type(e.get("arguments")) is list and e["arguments"][:1]==["sender"]]
                    require(not candidates and not entered,"strict inherited fd unreached has no Sender method entry")
                    continue
                require(type(reference) is dict and set(reference)=={"state","cut_index","method_call"}
                    and reference["state"]=="NOT_REACHED" and not candidates,
                    "absence alone is never a causal NOT_REACHED proof")
                cuts=[(index,e) for index,e in self.source if index==reference["cut_index"]
                    and e["operation"]=="method.error" and e["arguments"][1][1]==reference["method_call"]]
                require(len(cuts)==1 and cuts[0][1]["error"] is not None,
                    "NOT_REACHED requires actual owned failed-method cut")
                # Full method/error/phase replay is mandatory before this new
                # negative interface can credit an unreached branch. Until that
                # exact cut has been reconstructed, it stays a genuine code gap.
                replay=[r for r in method_sites(self) if r["end_index"]==reference["cut_index"]]
                require(len(replay)==1 and replay[0]["full_body_replay"] is True,
                    "unreached-purpose cut requires complete original method-error replay")
                continue
            require(type(reference) is dict and set(reference) == {"namespace", "event_index", "path"},
                "variable exact reached observation reference")
            require(reference["namespace"] in ("Root_recipient","Source_producer")
                and type(reference["event_index"]) is int,"exact binding namespace/index, never fallback")
            rows = roots if reference["namespace"] == "Root_recipient" else sources
            require(reference["event_index"] in rows and type(reference["path"]) is list
                and all(type(key) in (str,int) for key in reference["path"]),
                "variable actual held row exists")
            actual = rows[reference["event_index"]]
            require(tuple(reference["path"])==path and any(index==reference["event_index"]
                for index,_,_ in candidates),"exact frozen original variable purpose/operation/path")
            for key in reference["path"]:
                actual = actual[key]
            require(type(actual) is type(values[name]) and actual == values[name],
                "variable same actual value/type and reached observation")
        for name in ("holder_pid", "parent_pid", "holder_start_ticks"):
            require(values[name] is None or type(values[name]) is int and values[name] > 0,
                "PID/ticks exact integer, bool refused")
        for name in ("wall0", "mono0", "original_wall_end", "original_monotonic_end"):
            require(values[name] is None or type(values[name]) in (float, int) and math.isfinite(values[name]),
                "actual original clock finite")
        require(type(values["identities"]) is list,"all actual identity variables explicitly listed")
        for item in values["identities"]:
            require(type(item) is list and len(item) == 9 and all(type(v) is int and v >= 0 for v in item),
                "every full9 metadata variable, no field drop")
        usage = values["wait4_usage"]
        require(usage is None or type(usage) is list and len(usage) == 16
            and all(type(v) in (float, int) and math.isfinite(v) and v >= 0 for v in usage[:2])
            and all(type(v) is int and v >= 0 for v in usage[2:]), "owned exact usage16")
        for name in ("wait4_status","error_errno"):
            require(values[name] is None or type(values[name]) is int,"status/errno exact int, bool refused")
        require(type(values["fd_names"]) is list and all(type(v) is str and v.isascii() and v.isdecimal()
            for v in values["fd_names"]),"actual descriptor names exact ASCII decimals")

    def native_prefix(self, native_wire, native_exit, allow_failed_delivery=False):
        captured = b"".join(bytes.fromhex(r["raw_hex"]) for _, r in self.root
            if r["operation"] == "caller.stdout.received")
        require(captured == native_wire and len(captured) <= 262144,
            "Root exact original full captured prefix, no native envelope raise")
        delivered = b"".join(bytes.fromhex(r["raw_prefix_hex"]) for _, r in self.root
            if r["operation"] == "original_fd1.write_returned" and not r["would_block"])
        require(captured.startswith(delivered),"actual delivery is the exact captured prefix")
        require(allow_failed_delivery or delivered == captured,"complete delivery required outside failed-prefix cases")
        if not self.terminal:
            require(allow_failed_delivery and type(native_exit) is int and native_exit==125,
                "failed Root prefix never fabricates actual child exit/complete handoff")
            return {"captured":captured,"delivered":delivered,"complete":False}
        terminal = self.terminal[0]
        require(type(native_exit) is int and terminal["caller_native_exit"] == native_exit
            and terminal["original_stdout_bytes"] == len(captured)
            and terminal["original_stdout_sha256"] == hashlib.sha256(captured).hexdigest(),
            "independent exact actual child native exit and channel SHA")
        return {"captured":captured,"delivered":delivered,"complete":delivered==captured}

    def bind_inputs(self, documents, native):
        require(type(documents) is dict and set(documents)<= {"stage1","stage2","expectation","body","report"},
            "actual separately bound complete input document namespaces")
        observations = {}
        self.input_raw={}
        for _, row in self.root:
            if row["operation"] == "collector.owned_before_launch":
                self.input_raw["stage1"]=bytes.fromhex(row["original_stage1_hex"])
                observations["stage1"] = json.loads(self.input_raw["stage1"])
            elif row["operation"] == "stage2.independent_input_checked":
                self.input_raw["stage2"]=bytes.fromhex(row["raw_hex"])
                observations["stage2"] = json.loads(self.input_raw["stage2"])
            elif row["operation"] == "controller.custody_independently_held":
                for name,held in zip(("expectation","body"),row["objects"]):
                    self.input_raw[name]=bytes.fromhex(held["raw_hex"])
                    observations[name] = json.loads(self.input_raw[name])
        if native.get("reported_controller_output_claim") is not None:
            observations["report"] = native["reported_controller_output_claim"]
        for name, value in documents.items():
            if name not in observations and name in ("expectation","body"):
                expected=canonical(value)
                reached=[(index,event) for index,event in self.source if event["operation"]=="read.result"
                    and type(event["result"]) is bytes and event["result"]==expected]
                require(len(reached)==1,"early document binds one full actual performing read, not a supplied model")
                self.input_raw[name]=expected
                observations[name]=json.loads(expected)
            require(name in observations and same_typed(value,observations[name]),
                "fault input actual independent custody/native capture, never a supplied unbound model")
        return observations

    def captured_producers(self,native):
        """Full actual original writers and reads; Source bytes stay Source data."""
        segments=[];combined={1:bytearray(),2:bytearray()};workers={};fd_paths={}
        self.failed_physical_owned=[]
        required=set();worker_reads={};physical_orders={1:[],2:[]}
        for index,event in self.source:
            operation=event["operation"];arguments=event["arguments"]
            if operation=="dispatcher.worker_launch.begin":
                required.update(event["result"][key] for key in ("stdout_ref","stderr_ref"))
            if operation=="receipt.raw_member.open.end" and event["error"] is None:
                fd_paths[(event["pid"],event["result"])]=arguments[0][0]
            if operation=="receipt.raw_member.read.end" and event["error"] is None:
                fd=arguments[0][0];path=fd_paths.get((event["pid"],fd))
                if path is not None:
                    relative=path.removeprefix(G.PACKAGE+"/")
                    if relative in required:
                        raw=event["result"]
                        require(type(raw) is bytes,"full exclusive worker raw file bytes")
                        if relative in worker_reads:require(worker_reads[relative]==raw,"same immutable exclusive worker stream across consumers")
                        worker_reads[relative]=raw
            channel_early=arguments[2] if type(arguments) is list and len(arguments)==3 and type(arguments[2]) is dict else None
            if type(channel_early) is dict and channel_early.get("physical_kind")=="flush_no_new_owned_bytes":continue
            if event["actor"] not in ("controller","dispatcher"):continue
            if event["error"] is not None:
                payload=None if channel_early is None else channel_early.get("physical_payload_before")
                count=event["result"]
                if type(payload) is bytes and type(count) is int and 0<=count<=len(payload):
                    pass
                elif type(payload) is bytes:
                    self.failed_physical_owned.append({"owned_bytes":len(payload),"sequence":index,"pid":event["pid"]})
                    continue
                else:continue
            raw=None;fd=None
            if operation.endswith(".internal.os.write"):
                fd,requested=arguments[0][:2];count=event["result"]
                channel=arguments[2] if len(arguments)==3 else None
                if type(channel) is dict and 'physical_payload_before' in channel:
                    requested=channel['physical_payload_before']
                    require(type(requested) is bytes and type(count) is int and 0<=count<=len(requested),
                        'original full raw before-syscall owner and exact returned physical prefix')
                require(type(requested) is bytes and type(count) is int and 0<=count<=len(requested),"actual writer prefix count")
                raw=requested[:count]
            elif operation.endswith(".internal.os.writev") or operation.endswith(".internal.os.pwritev"):
                channel=arguments[2] if len(arguments)==3 else None
                require(type(channel) is dict and type(channel.get("physical_payload_before")) is bytes,
                    "vector physical payload is the owned before-call bytes")
                requested=channel["physical_payload_before"];count=event["result"];fd=channel.get("fd")
                require(type(count) is int and 0<=count<=len(requested),"vector returned physical prefix")
                raw=requested[:count]
            elif operation.endswith(".internal.raw.write") and len(arguments)==3:
                channel=arguments[2];fd=channel["fd"];requested=arguments[0][0];count=event["result"]
                if 'physical_payload_before' in channel:requested=channel['physical_payload_before']
                if type(requested) is dict and requested.get("type")=="buffer_snapshot":requested=requested["bytes"]
                require(type(requested) in (bytes,bytearray) and type(count) is int and 0<=count<=len(requested)
                    and type(channel.get("generation")) is list and len(channel["generation"])==2,
                    "actual encoded raw writer/held channel generation/returned physical prefix")
                raw=bytes(requested[:count])
            elif operation.endswith(".internal.stream.write") and len(arguments)==3:
                channel=arguments[2];fd=channel["fd"];requested=arguments[0][0];count=event["result"]
                require(type(count) is int and 0<=count<=len(requested),"actual text/binary writer count")
                if fd in (1,2):
                    # A semantic text/buffered acceptance is NOT physical data.
                    # The actual underlying raw.write rows above carry bytes.
                    if channel.get("unbuffered_stock_fileio") is not True:continue
                    require(type(requested) is bytes,"actual unbuffered writer bytes")
                raw=requested[:count]
                if type(raw) is str:
                    # Keep semantic text acceptance in self.source. It is never
                    # inserted into a physical pipe body by a re-encoding guess.
                    raw=None
            if fd in (1,2) and raw:
                require(type(raw) is bytes,"actual producer raw bytes, never filler/silent-zero")
                channel=arguments[2] if len(arguments)==3 else None
                require(type(channel) is dict and type(channel.get("physical_sequence")) is int
                    and type(channel.get("identity9")) is list and len(channel["identity9"])==9,
                    "writer must carry actual raw pipe generation and intra-actor physical order")
                segments.append({"sequence":index,"producer":event["actor"],"pid":event["pid"],
                    "fd":fd,"bytes":raw,"physical_sequence":channel["physical_sequence"],
                    "pipe_identity9":channel["identity9"],"pipe_buf":channel["pipe_buf"]})
                combined[fd].extend(raw)
        require(set(worker_reads)==required,"every exclusive worker stdout AND stderr fully consumed; no absent stream as zero")
        mapping=native.get("controller_original_fd_map")
        if mapping is not None:
            for name,fd in mapping.items():
                actual=b"".join(event["result"] for _,event in self.source if event["operation"]=="read.result"
                    and event["error"] is None and event["arguments"][1][0]==fd)
                target=1 if name=="stdout" else 2
                owner={'segments':[s for s in segments if s['fd']==target], 'actual':actual,'recipe':None,'error':None}
                if not hasattr(self,'physical_prefix_custody'):self.physical_prefix_custody=[]
                self.physical_prefix_custody.append(owner)
                try:owner['recipe']=physical_pipe_merge(owner['segments'],actual,self.held.lifetime.tail.ends)
                except BaseException as error:owner['error']=error;raise
                recipe=owner['recipe'];ordered=recipe['read_parts']
                physical_orders[target]=[{**row,"physical_rank":rank} for rank,row in enumerate(ordered)]
                combined[target]=bytearray(actual)
                observed=native["controller_pipe_observation"][str(fd)]
                if observed["eof"] and not observed["capture_stopped"]:
                    require(not recipe['unread_segments'] and all(not row['unread_suffix'] for row in ordered),
                        'actual EOF requires no retained unread writer suffix')
        def worker_reader(path):
            require(path in worker_reads,"exact held exclusive worker reference")
            return worker_reads[path]
        def byte_producer(index,length,pin):
            event=dict(self.source).get(index)
            require(event is not None,"actual byte commitment event exists")
            candidates=[]
            def walk(value):
                if type(value) is bytes and len(value)==length and hashlib.sha256(value).hexdigest()==pin:candidates.append(value)
                elif type(value) is dict:
                    for item in value.values():walk(item)
                elif type(value) in (list,tuple):
                    for item in value:walk(item)
            walk(event)
            require(candidates and all(raw==candidates[0] for raw in candidates),"full actual bytes tied to original event, not arbitrary producer callback")
            return candidates[0]
        require(mapping is not None or not segments,"no physical writer order without the actual original read-channel map")
        segments=physical_orders[1]+physical_orders[2]
        return segments,bytes(combined[1]),bytes(combined[2]),worker_reads,worker_reader,byte_producer

    def grammar(self,documents,variables,case):
        # These are verbatim ordered performing predicates, not user-selected
        # expected types/values or an advisory schema dictionary. A bad input is
        # accepted as an ordinary refusal only against its actual first guard.
        stage1=documents.get("stage1")
        stage2=documents.get("stage2")
        result=[]
        for name in ("stage1","expectation","body","stage2","report"):
            if name not in documents:
                continue
            try:
                if name in self.input_raw:
                    parsed=G.object_wire(self.input_raw[name],telemetry=name=="report")
                    require(same_typed(parsed,documents[name]),"pure grammar exact actual input bytes")
                if name=="stage1":
                    G.validate_stage1(stage1)
                elif name=="expectation":
                    G.expectation_shape(documents[name])
                elif name=="body":
                    G.body_shape(documents[name])
                elif name=="stage2":
                    custody=[row for _,row in self.root if row["operation"]=="controller.custody_independently_held"]
                    require(stage1 is not None and len(custody)==1,"actual prior stage1 and independent held custody")
                    held=[{"identity":item["identity9"],"sha256":item["sha256"]} for item in custody[0]["objects"]]
                    G.validate_stage2(stage2,stage1,hashlib.sha256(self.input_raw["stage1"]).hexdigest(),
                        custody[0]["holder_pid"],custody[0]["holder_start_ticks"],held)
                else:
                    require(stage1 is not None and stage2 is not None,"report only after actual stage2")
                    G.validate_report(documents[name],stage2,stage1)
                result.append({"document":name,"first_predicate":"ALL_ORDERED_REACHED_PREDICATES_RETURNED"})
            except G.Refusal as error:
                first=self.first_guard()
                require(first is not None and first[1]==str(error) and case not in ("positive","nonzero-dispatcher"),
                    "actual earliest refusal equals ordered performing nested grammar")
                result.append({"document":name,"first_predicate":str(error),"actual_guard_index":first[0],
                    "actual_guard_scope":first[2]})
                break
        return result

    def actual_grammar_sites(self):
        allowed={"exact","control_types","encode","object_wire","source_map","expectation_shape",
            "body_shape","context_shape","namespace_shape","custody_shape","validate_stage1","validate_init",
            "validate_stage2","validate_claim","validate_report","check_held_observation"}
        checked=[]
        for index,event in self.source:
            if event["operation"]!="grammar.enter":
                continue
            packed=event["arguments"]
            require(type(packed) is list and len(packed)==3 and packed[0]=="sender"
                and type(packed[1]) is list and len(packed[1])==3,
                "actual performing pure grammar invocation and reached guard scope")
            name,args,kwargs=packed[1]
            scope=packed[2]
            require(name in allowed and type(args) is list and type(kwargs) is dict
                and type(scope) is list and scope and scope[-1]==name,
                "exact Root-owned copy of an actual named ordered pure predicate")
            ends=[(at,later) for at,later in self.source if at>index and later["pid"]==event["pid"]
                and later["operation"] in ("grammar.return","grammar.error")
                and later["arguments"]==["sender",[name],scope]]
            require(ends,"actual grammar end/error reached; never fabricated from optional input documents")
            end_index,actual=ends[0]
            prior_log=G.PREDICATE_LOG
            G.PREDICATE_LOG=[]
            try:
                result=getattr(G,name)(*args,**kwargs)
            except Exception as error:
                require(actual["operation"]=="grammar.error" and actual["error"] is not None
                    and actual["error"]["type"]==type(error).__name__ and actual["error"]["text"]==str(error)
                    and same_typed(list(actual["error"]["args"]),list(error.args))
                    and actual["error"]["module"]==("a088_checked_staged_sender" if isinstance(error,G.Refusal) else type(error).__module__)
                    and same_typed(actual["error"]["errno"],getattr(error,"errno",None))
                    and same_typed(actual["error"]["filename"],getattr(error,"filename",None))
                    and same_typed(actual["error"]["filename2"],getattr(error,"filename2",None)),
                    "ordered pure predicate or original stdlib error equals the full actual Source exception")
                guards=[(at,later) for at,later in self.source if index<at<end_index
                    and later["operation"]=="guard.refused" and later["pid"]==event["pid"]]
                require(not isinstance(error,G.Refusal) or guards
                    and guards[0][1]["arguments"][1]==[str(error)],
                    "actual first nested rejecting predicate; stdlib errors need no fabricated guard")
                checked.append({"name":name,"begin_index":index,"end_index":end_index,
                    "first_guard_index":None if not guards else guards[0][0],
                    "cause":str(error),"scope":scope,"error_type":type(error).__name__})
            else:
                require(actual["operation"]=="grammar.return" and actual["error"] is None
                    and same_typed(result,actual["result"]),"full typed returned pure grammar result")
                checked.append({"name":name,"begin_index":index,"end_index":end_index,"scope":scope,"returned":True})
            finally:
                ordered=G.PREDICATE_LOG;G.PREDICATE_LOG=prior_log
            source_predicates=[(at,later) for at,later in self.source if index<at<end_index
                and later["operation"]=="grammar.predicate" and later["pid"]==event["pid"]]
            require(len(ordered)==len(source_predicates),"every reached ordered nested predicate, no omitted successful priors")
            joined=[]
            for expected,(at,observed) in zip(ordered,source_predicates):
                source_site=observed["arguments"][1][0]
                row=G.source_predicate(source_site)
                require(row["id"]==expected["site"] and type(observed["result"]) is type(expected["ok"])
                    and observed["result"]==expected["ok"],"same frozen exact per-field expression and original ordering")
                joined.append({"event_index":at,"predicate_id":row["id"],"ok":observed["result"]})
            checked[-1]["ordered_predicates"]=joined
        return checked

def physical_pipe_merge(segments,actual_read_body,ends):
    """Derive physical PREFIX from actual bytes and each writer's program order.
    Per-actor serials are not global pipe order. Equal-prefix ambiguity is an
    explicit remaining CODE domain, never arbitrarily assigned by pid/arrival.
    Full original written suffixes remain owned, not declared read or complete.
    """
    require(type(actual_read_body) in (bytes,bytearray),'actual full physical read body')
    actual=bytes(actual_read_body);queues={};seen=set()
    expanded=[]
    for row in segments:
        raw=row["bytes"]
        require(type(raw) is bytes and type(row.get("pipe_buf")) is int and row["pipe_buf"]>0 and len(raw)>0,
            "physical segment owns a positive recorded pipe buffer")
        size=row["pipe_buf"]
        for piece in range((len(raw)+size-1)//size):
            part=raw[piece*size:(piece+1)*size]
            item=dict(row);item["bytes"]=part;item["piece"]=piece
            expanded.append(item)
    for row in expanded:
        require(0<len(row["bytes"])<=row["pipe_buf"],
            "physical segment length stays inside the recorded pipe buffer")
        key=(row['pid'],tuple(row['pipe_identity9']),row['physical_sequence'],row['piece'])
        require(key not in seen,'writer generation/sequence unique without false cross-pid collision')
        seen.add(key);queues.setdefault((row['pid'],tuple(row['pipe_identity9'])),[]).append(row)
    require(len({tuple(row['pipe_identity9']) for row in segments})<=1,'one actually held pipe generation')
    writers=sorted(queues)
    lists=[sorted(queues[key],key=lambda row:(row['physical_sequence'],row['piece'])) for key in writers]
    stack=[(tuple(0 for _ in lists),0,[])];solutions=[]
    while stack and len(solutions)<2:
        require(min(ends[0]-time.time(),ends[1]-time.monotonic())>0,'original Root replay dual end, no refresh')
        positions,offset,parts=stack.pop()
        if offset==len(actual):
            unread=[row for group,pos in zip(lists,positions) for row in group[pos:]]
            solutions.append({'read_parts':parts,'unread_segments':unread});continue
        for writer,(group,pos) in enumerate(zip(lists,positions)):
            if pos==len(group):continue
            row=group[pos];written=row['bytes'];count=min(len(written),len(actual)-offset)
            if written[:count]!=actual[offset:offset+count]:continue
            next_positions=list(positions);next_positions[writer]+=1
            part={**row,'bytes':written[:count],'written_bytes':written,'unread_suffix':written[count:]}
            stack.append((tuple(next_positions),offset+count,parts+[part]))
    require(len(solutions)==1,'unique original physical prefix; equal-prefix provenance still CODE_OPEN if ambiguous')
    return solutions[0]
class ObservedMethodReplay:
    """Execute pinned method BODY against only reached observed results.

    This is not live credit or replay by supplied expected predicate booleans.
    Unknown/opaque self-state refuses replay instead of native fallthrough.
    Every permitted effect consumes its exact prior observed syscall in order.
    """
    METHODS={"Clock","Named","Directory","Generation","OwnScope","Held","IO","Session","AcquisitionOwner","Budget","ReadJournal","_FailureOwner"}
    EFFECTS={"lstat","fstat","stat","readlink","listdir","open","close","read","pread","write","pwrite",
        "dup2","pidfd_open","set_blocking","getpid","geteuid","getegid","wait4","fcntl","time","monotonic",
        "getrlimit","setrlimit","getrusage","pthread_sigmask","signal","getsignal","waitstatus_to_exitcode","selector.create","selector.register","selector.select","selector.close","selector.allocate"}
    JOURNAL={'phase','guarded_read','read.result','read.denied','run.failure','close.failure',
        'controller.pipe.close','native.before_delivery','early.write','write','guard.refused',
        'recorder.check.state','recorder.wire.state'}
    def __init__(self,events,held):
        self.events=events;self.held=held;self.cursor=0;self.objects={};self.reverse={}
        self.operands=None;self.begin_index=None
    def restore(self,graph):
        require(graph["schema"] in ("friday.sol056.method-state-graph.v1","friday.sol089.method-state-graph.v2")
            and graph["complete_replayable_state"] is True and graph["opaque_owned_ids"]==[],
            "full actual nonopaque owned self/arguments before method replay")
        nodes={n["id"]:n for n in graph["nodes"]}
        require(len(nodes)==len(graph["nodes"]),"method graph stable IDs unique")
        def leaf(value):
            if type(value) is not dict:return value
            if value.get("kind")=="float":return float.fromhex(value["hex"])
            if value.get("kind")=="bytes":return bytes.fromhex(value["hex"])
            if value.get("kind")=="owned_bytes":return self.held.full_byte_reference({
                "type":"held_bytes","length":value["bytes"],"sha256":value["sha256"]})
            if value.get("kind")=="identity9":return value["value"]
            require(set(value)=={"ref"} and value["ref"] in nodes,"complete stable method-state reference")
            number=value["ref"]
            if number in self.objects:return self.objects[number]
            row=nodes[number];kind=row["kind"]
            identity=(self.operands.pid,number) if self.operands is not None else None
            existing=None if identity is None else self.operands.objects.get(identity)
            if kind=="int":obj=int(row["state"])
            elif kind=="str":obj=row["state"].encode("utf-8","surrogatepass").decode("utf-8","surrogatepass")
            elif kind=="float":obj=float.fromhex(row["state"])
            elif kind=="bytes":
                state=row["state"]
                raw=(self.held.full_byte_reference({"type":"held_bytes","length":state["bytes"],"sha256":state["sha256"]})
                    if "sha256" in state else bytes.fromhex(state["hex"]))
                obj=bytes(bytearray(raw))
            elif kind=="object":
                require(row["class"] in self.METHODS,"only exact pinned class bodies may be reconstructed")
                if row["class"] in ("Budget","ReadJournal"):
                    # Exact pure copies, not import/execute of the performing
                    # caller (whose top-level native bootstrap is effectful).
                    _cls=getattr(G,row["class"])
                else:_cls=getattr(G,row["class"])
                obj=_cls.__new__(_cls)
            elif kind=="dict":obj={}
            elif kind in ("list","tuple"):obj=[]
            elif kind in ("set","frozenset"):obj=set()
            elif kind=="bytearray":obj=bytearray.fromhex(row["state"])
            elif kind=="error":
                import builtins
                _type=row['state']['type']
                _cls=G.Refusal if _type=='Refusal' else getattr(builtins,_type,None)
                require(isinstance(_cls,type) and issubclass(_cls,BaseException), 'no invented exception type')
                obj=_cls(*leaf(row['state']['args']))
                # Match the effect-error receiver: restore filename-bearing
                # constructor state before comparing the original full text.
                for name in ('filename','filename2'):
                    if name in row['state']:
                        expected=leaf(row['state'][name])
                        if not same_typed(getattr(obj,name,None),expected):setattr(obj,name,expected)
                require(str(obj)==row['state']['text'] and getattr(obj,'errno',None)==row['state']['errno'],
                    'original exact constructor args, not text-only replacement')
            elif kind=="stock_operand":
                require(self.operands is not None,"method has actual same-generation operand registry")
                obj=self.operands.before(row["state"],self.begin_index)
            elif kind=="selector_facade":
                obj=self.replay_selector(False)
            elif kind=="facade":
                import os,time,fcntl,resource,selectors,signal
                _modules={m.__name__:m for m in (os,time,fcntl,resource,selectors,signal)}
                _name=row['state']['name']
                if _name=='FullEventJournal':obj=self.journal
                elif _name=='Refusal':obj=G.Refusal
                elif _name=='_ObservedSelector':obj=self.replay_selector(False)
                else:
                    require(_name in _modules,'unknown facade is an actual CODE gap, no inert dummy object')
                    obj=self.module(_modules[_name]);obj.__name__=_name
            else:raise ValueError("opaque/native/unknown state is not replayed")
            if existing is not None:
                require(type(existing) is type(obj) or kind=="tuple" and type(existing) is tuple or kind=="frozenset" and type(existing) is frozenset,
                    "method input and independent stock registry share exact types")
                if kind in ("int","str","float","bytes"):
                    require(same_typed(existing,obj),"same whole immutable body across method/stock graphs")
                obj=existing
            self.objects[number]=obj;self.reverse[id(obj)]=number
            if identity is not None:
                prior=self.operands.reverse.get(id(obj))
                require(prior is None or prior==identity,"different Source IDs cannot collapse in receiver")
                self.operands.objects[identity]=obj;self.operands.reverse[id(obj)]=identity
            if kind=="object":vars(obj).update({k:leaf(v) for k,v in row["state"].items()})
            elif kind=="selector_facade":
                vars(obj).clear()
                vars(obj).update({k:leaf(v) for k,v in row["state"].items()})
            elif kind=="dict":
                pairs=[(leaf(k),leaf(v)) for k,v in row["state"]]
                require(len({k for k,v in pairs})==len(pairs),"method dictionary aliases do not hide duplicate keys")
                obj.clear();obj.update(pairs)
            elif kind=="list":obj[:]=[leaf(v) for v in row["state"]]
            elif kind=="set":obj.clear();obj.update(leaf(v) for v in row["state"])
            if kind in ("tuple","frozenset"):
                parts=[leaf(v) for v in row["state"]]
                candidate=tuple(parts) if kind=="tuple" else frozenset(parts)
                if existing is not None:
                    require(same_typed(existing,candidate),"full immutable method alias is not rebound")
                    obj=existing
                else:obj=candidate
                self.objects[number]=obj;self.reverse[id(obj)]=number
                if identity is not None:
                    self.operands.objects[identity]=obj;self.operands.reverse[id(obj)]=identity
            return obj
        return leaf(graph["root"])
    def replay_selector(self, perform_create, owner="sender"):
        driver=self
        class _ObservedSelector:
            def __init__(self, perform_create):
                self.close_attempted=False
                self.close_error=None
                self.owner=owner
                self.raw=None
                if not perform_create:
                    return
                require(driver.operands is not None,"same actual stock operand generation before caller allocation")
                descriptor=driver.operands.catalog.descriptor(driver.operands.catalog.selected)
                self.raw=driver.call("selector.allocate",(descriptor,),{},origin_owner=owner)
                try:
                    driver.journal.call(owner,"selector.create",None,self.raw)
                except BaseException as error:
                    self.create_error=error
                    raise
            def register(self,*args,**kwargs):
                return driver.journal.call(self.owner,"selector.register",None,*args,**kwargs)
            def select(self,*args,**kwargs):
                return driver.journal.call(self.owner,"selector.select",None,*args,**kwargs)
            def close(self,*args,**kwargs):
                if self.close_attempted:
                    if self.close_error is not None:
                        raise RuntimeError("selector ambiguous close: no retry")
                    return
                self.close_attempted=True
                try:
                    return driver.journal.call(self.owner,"selector.close",None,*args,**kwargs)
                except BaseException as error:
                    self.close_error=error
                    raise
            def __enter__(self):
                return self
            def __exit__(self,*args):
                if self.close_attempted:
                    if self.close_error is not None:
                        raise RuntimeError("selector ambiguous close: no retry")
                    return
                self.close_attempted=True
                try:
                    driver.journal.call(self.owner,"selector.close",None)
                except BaseException as error:
                    self.close_error=error
                    if args[1] is None:
                        raise
        return _ObservedSelector(perform_create)
    def module(self,original):
        driver=self
        class StrictObservedAPI:
            def __getattr__(self,name):
                if name=="DefaultSelector":
                    return lambda: driver.replay_selector(True)
                if name in driver.EFFECTS:
                    def performing_api(*args,**kwargs):
                        return driver.journal.call("sender",name,None,*args,**kwargs)
                    return performing_api
                require(name.isupper() and hasattr(original,name),"replay has no unobserved native callable")
                return getattr(original,name)
        return StrictObservedAPI()
    def restore_error(self,event):
        import builtins
        graph=event['graph'];nodes=graph['nodes'];objects={};busy=set()
        def datum(value):
            if type(value) is list:return [datum(v) for v in value]
            if type(value) is dict and value.get('type')=='error_ref':return make(value['id'])
            if type(value) is dict and value.get('type')=='mapping':return {datum(k):datum(v) for k,v in value['value']}
            return decode(value,self.held)
        def make(number):
            require(type(number) is int and 0<=number<len(nodes),'exact original error alias index')
            if number in objects:return objects[number]
            require(number not in busy,'cyclic constructor arguments require raw native owner, not fabricated constructor')
            busy.add(number);node=nodes[number]
            cls=G.Refusal if node['type']=='Refusal' else getattr(builtins,node['type'],None)
            require(node['type']=='Refusal' or node['module']=='builtins','only already approved exception classes')
            require(isinstance(cls,type) and issubclass(cls,BaseException),'actual original stock exception class')
            obj=cls(*datum(node['args']));objects[number]=obj;busy.remove(number)
            for name in ('filename','filename2'):
                if name in node:
                    expected=datum(node[name])
                    if not same_typed(getattr(obj,name,None),expected):setattr(obj,name,expected)
            require(type(obj).__name__==node['type'] and str(obj)==node['text'],'same original full constructor arguments and text')
            return obj
        for number in range(len(nodes)):make(number)
        for node in nodes:
            obj=objects[node['id']]
            obj.__dict__.update(datum(node['state']))
            obj.__cause__=None if node['cause'] is None else objects[node['cause']]
            obj.__context__=None if node['context'] is None else objects[node['context']]
            obj.__suppress_context__=node['suppress_context']
            if node['notes'] is not None:
                notes=datum(node['notes'])
                require(type(notes) is list and all(type(v) is str for v in notes),'full original exception notes')
                obj.__notes__=notes
            for name in ('errno','filename','filename2'):
                expected=datum(node[name])
                if not same_typed(getattr(obj,name,None),expected):setattr(obj,name,expected)
            require([objects[i] for i in node['groups']]==list(getattr(obj,'exceptions',())),
                'same original nested group member aliases')
        # Native traceback/frame locals/C heap are not reconstructed from text.
        # Actual raw graph and original frame coordinates stay in the held owner.
        return objects[graph['root']]
    def call(self,name,args,kwargs,origin_owner=None):
        require(self.cursor<len(self.events),"replay operation has an actual reached result")
        index,event=self.events[self.cursor];self.cursor+=1
        prior_held=self.held.operand_registry
        if self.operands is not None:
            self.operands.cut=index
            self.held.operand_registry=self.operands
        try:
            arguments=decode(event["arguments"],self.held)
        finally:self.held.operand_registry=prior_held
        event={**event,"arguments":arguments}
        require(event["operation"]==name and same_typed(event["arguments"][1],[list(args),kwargs]),
            "literal method operation/operands equal prior captured Source effect")
        if name=="selector.allocate":
            if event["error"] is not None:raise self.restore_error(event["error"])
            require(self.operands is not None and origin_owner is not None,"performing origin call, never expected argument copy")
            raw=self.operands.allocate(origin_owner,args[0])
            key=self.operands.key(event["result"])
            require(self.operands.birth_by_origin[key][0]==index,
                "same actual observed allocation row before constructor effect")
            self.operands.match(event["result"],raw)
            return raw
        if name in ("selector.create","selector.register","selector.select","selector.close"):
            post=self.chronology.operand_after.get((event["pid"],event["sequence"]))
            require(post is not None and post[0]>index,"actual partial/success/error post-body, never zero/opaque replacement")
            self.operands.cut=post[0]
            self.operands.decode(post[1],restore=True)
        if event["error"] is not None:raise self.restore_error(event["error"])
        def stock_result(part):
            if type(part) is list:return [stock_result(x) for x in part]
            if type(part) is dict and part.get("type")=="stock_operand_member":return self.operands.member(part)
            if type(part) is dict and part.get("type")=="stock_operand_graph":return self.operands.decode(part)
            if type(part) is dict:return {k:stock_result(v) for k,v in part.items()}
            return part
        result=stock_result(event["result"])
        if name in ("lstat","fstat","stat"):
            require(type(result) is list and len(result)==9,"observed metadata exact9")
            class ExactIdentity:
                pass
            info=ExactIdentity()
            for key,value in zip(("st_dev","st_ino","st_mode","st_uid","st_gid","st_nlink","st_size","st_mtime_ns","st_ctime_ns"),result):
                setattr(info,key,value)
            return info
        return result
    def after(self,graph,actual):
        require(graph["complete_replayable_state"] is True and not graph["opaque_owned_ids"],
            "complete actual post-state, never an opaque state promotion")
        nodes={n["id"]:n for n in graph["nodes"]};seen=set()
        def bind(wire,value):
            if type(wire) is not dict:
                require(same_typed(wire,value),"full actual method scalar state transition")
                return
            if wire.get("kind")=="float":require(type(value) is float and value.hex()==wire["hex"],"actual float state");return
            if wire.get("kind")=="bytes":require(type(value) is bytes and value.hex()==wire["hex"],"actual full byte state");return
            if wire.get('kind')=='identity9':
                actual9=G.identity(value) if hasattr(value,'st_dev') else value
                require(same_typed(wire['value'],actual9),'all nine original metadata fields');return
            if wire.get("kind")=="owned_bytes":
                require(type(value) is bytes and len(value)==wire["bytes"]
                    and self.held.full_byte_reference({"type":"held_bytes","length":wire["bytes"],"sha256":wire["sha256"]})==value,
                    "full actual independently held bytes, never checksum-only state replay")
                return
            number=wire["ref"];row=nodes[number]
            if number in self.objects:require(self.objects[number] is value,"method state preserves actual alias identity")
            else:self.objects[number]=value;self.reverse[id(value)]=number
            if number in seen:return
            seen.add(number);kind=row["kind"]
            if kind in ("int","str","float","bytes"):
                require(type(value).__name__==kind,"complete original typed immutable method state")
                if kind=="bytes":
                    state=row["state"]
                    raw=self.held.full_byte_reference({"type":"held_bytes","length":state["bytes"],"sha256":state["sha256"]}) if "sha256" in state else bytes.fromhex(state["hex"])
                    require(value==raw,"complete original bytes, never hash-only alias state")
                else:
                    body=str(value) if kind=="int" else value.hex() if kind=="float" else value
                    require(body==row["state"],"full immutable argument/body alias")
            elif kind=="object":
                require(type(value).__name__==row["class"] and set(vars(value))==set(row["state"]),"full pinned method object state keyset")
                for key,part in row["state"].items():bind(part,vars(value)[key])
            elif kind=="dict":
                require(type(value) is dict and len(value)==len(row["state"]),"full method dict state")
                for (a,b),(k,v) in zip(row["state"],value.items()):bind(a,k);bind(b,v)
            elif kind in ("list","tuple"):
                require(type(value).__name__==kind and len(value)==len(row["state"]),"full ordered method sequence")
                for a,b in zip(row["state"],value):bind(a,b)
            elif kind=="bytearray":require(type(value) is bytearray and value.hex()==row["state"],"full mutable byte post-state")
            elif kind in ("set","frozenset"):
                require(type(value).__name__==kind and len(value)==len(row["state"]),"full set post-state")
                pool=list(value)
                for part in row["state"]:
                    missing=object();hit=missing
                    for item in pool:
                        if type(part) is not dict:
                            matched=part==item
                        elif part.get("kind")=="float":
                            matched=type(item) is float and item.hex()==part.get("hex")
                        elif part.get("kind")=="bytes":
                            matched=type(item) is bytes and item.hex()==part.get("hex")
                        elif set(part)=={"ref"}:
                            matched=self.objects.get(part["ref"]) is item or self.reverse.get(id(item))==part["ref"]
                        else:matched=False
                        if matched:
                            hit=item;break
                    require(hit is not missing,"set member present")
                    pool.remove(hit)
            elif kind=="error":
                require(isinstance(value,BaseException) and type(value).__name__==row["state"]["type"]
                    and getattr(value,"errno",None)==row["state"].get("errno")
                    and str(value)==row["state"]["text"]
                    and same_typed(getattr(value,"filename",None), row["state"].get("filename"))
                    and same_typed(getattr(value,"filename2",None), row["state"].get("filename2")),"actual error type errno and text")
                bind(row["state"]["args"],value.args)
            elif kind=="stock_operand":
                require(self.operands is not None,"actual body/alias receiver")
                self.operands.match(row["state"],value)
            elif kind=="selector_facade":
                require(type(value).__name__==row["class"] and set(vars(value))==set(row["state"]),
                    "full actual facade fields, including raw operand/error/close state")
                for key,part in row["state"].items():bind(part,vars(value)[key])
            elif kind=="facade":
                require(type(value).__name__==row["class"] or getattr(value,"__name__",None)==row["state"].get("name")
                    or row["state"].get("name")=="FullEventJournal" and value is self.journal
                    or row["state"].get("name")=="Refusal" and value is G.Refusal,"actual facade state")
            else:raise ValueError("unimplemented native/set state reconstruction remains open")
        bind(graph["root"],actual)
    def run(self,name,before,after,actual_error=None):
        cls,method=name.split(".",1)
        originals={k:getattr(G,k,None) for k in ("os","time","fcntl","resource","selectors","signal","ctypes")}
        import os,time,fcntl,resource,selectors,signal
        modules={"os":os,"time":time,"fcntl":fcntl,"resource":resource,"selectors":selectors,"signal":signal}
        prior_log=G.PREDICATE_LOG;G.PREDICATE_LOG=[]
        prior_observer=G.METHOD_STATE_OBSERVER
        G.METHOD_STATE_OBSERVER=getattr(self,'predicate_callback',None)
        prior_trace=getattr(G,'TRACE',None);driver=self
        class StrictJournal:
            def note(self,owner,operation,arguments,result=None,error=None):
                require(driver.cursor<len(driver.events),'actual recorder operation required')
                _,observed=driver.events[driver.cursor];driver.cursor+=1
                require(observed['operation']==operation and observed['arguments'][0]==owner
                    and same_typed(observed['arguments'][1],arguments), 'literal recorder operands and phase')
                if error is None:
                    require(observed['error'] is None and same_typed(observed['result'],result),
                        'same actually performed recorder result')
                else:
                    require(observed['error'] is not None and observed['error']['type']==type(error).__name__
                        and observed['error']['text']==str(error), 'same actual recorder first error')
            def state(self,operation):
                require(driver.cursor<len(driver.events),'actual recorder state before this operation')
                _,event=driver.events[driver.cursor];driver.cursor+=1
                require(event['operation']=='recorder.'+operation+'.state'
                    and event['arguments'][0]=='sender' and event['arguments'][1]==[]
                    and event['error'] is None,'same actual journal operation and held full input')
                state=event['result']
                require(type(state) is dict and set(state)=={'schema','failed','calls','pending','recording_failure','producer'}
                    and state['schema']=='friday.sol061.recorder-state.v1'
                    and type(state['failed']) is bool and type(state['calls']) is int and state['calls']>=0
                    and type(state['pending']) is list,'complete exact recorder transition input')
                if state['producer'] is None:
                    require(len(state['pending'])==state['calls'],'all original pending rows, no count fixture')
                else:
                    require(state['pending']==[],'attached journal does not invent bootstrap rows')
                    if state['recording_failure'] is None and not state['failed']:
                        RootSourceTrace(driver.chronology).check_ref(G.RecorderSemantics.wire(state))
                return state
            def call(self,owner,operation,function,*args,**kwargs):
                cleanup=operation=='close' or operation.endswith('.close')
                if not cleanup:self.check()
                if not cleanup:self.check()
                return driver.call(operation,args,kwargs)
            def wire(self):return G.RecorderSemantics.wire(self.state('wire'))
            def check(self):return G.RecorderSemantics.check(self.state('check'))
        driver.journal=StrictJournal()
        packed=self.restore(before);args,kwargs=packed
        require(type(args) is tuple and (cls=='staged' and method=='perform_staged_send'
            or cls in self.METHODS and args and type(args[0]).__name__==cls),
            'actual named pinned performing method and owned self')
        raw_call=G.perform_staged_send if cls=="staged" else getattr(getattr(G,cls),method)
        undo=G.install_performing_wrappers(driver.journal)
        try:
            G.TRACE=driver.journal
            for key in originals:setattr(G,key,self.module(modules.get(key,object())))
            result=None;replayed_error=None
            cleanup=name.endswith(".close") or name=="Session.cleanup"
            try:
                if not cleanup:
                    driver.journal.check()
                    driver.journal.check()
                result=raw_call(*args,**kwargs)
            except BaseException as error:
                replayed_error=error
            if actual_error is None:
                require(replayed_error is None,"method body cannot replace an actual successful return with refusal")
            else:
                require(replayed_error is not None and type(replayed_error).__name__==actual_error["type"]
                    and same_typed(list(replayed_error.args),list(actual_error["args"]))
                    and str(replayed_error)==actual_error["text"]
                    and same_typed(getattr(replayed_error,"errno",None),actual_error["errno"])
                    and same_typed(getattr(replayed_error,"filename",None),actual_error["filename"])
                    and same_typed(getattr(replayed_error,"filename2",None),actual_error["filename2"]),
                    "entire method's first original stdlib/guard error equals its reached actual outcome")
            require(self.cursor==len(self.events),"every actual prior effect consumed by complete method body")
            self.after(after,[args,kwargs,result])
            return {"returned":actual_error is None,"original_error_matched":actual_error is not None,
                "ordered_predicates":list(G.PREDICATE_LOG),
                "first_fault":next((row for row in G.PREDICATE_LOG if row.get("ok") is not True),None),
                "whole_state_and_prior_effects":True}
        finally:
            undo()
            for key,value in originals.items():setattr(G,key,value)
            G.PREDICATE_LOG=prior_log
            G.METHOD_STATE_OBSERVER=prior_observer
            G.TRACE=prior_trace

def method_sites(chronology):
    checked=[];stack={}
    for index,event in chronology.source:
        if event["operation"] not in ("method.enter","method.return","method.error"):continue
        owner,arguments,scope=event["arguments"]
        name,call,parent=arguments
        require(owner=="sender" and type(call) is int,"actual method entry identifier")
        if event["operation"]=="method.enter":
            require((event["pid"],call) not in stack,"method generation entered once")
            stack[event["pid"],call]=(index,event)
            continue
        require((event["pid"],call) in stack,"method return/error requires actual prior entry")
        at,start=stack.pop((event["pid"],call))
        require(start["arguments"]==event["arguments"],"exact method/phase/call parent correspondence")
        effects=[(n,e) for n,e in chronology.source if at<n<index and e["pid"]==event["pid"]
            and e["operation"] in (ObservedMethodReplay.EFFECTS|ObservedMethodReplay.JOURNAL)]
        row={"name":name,"call":call,"begin_index":at,"end_index":index,"parent":parent,
            "captured_prior_operations":len(effects),"full_body_replay":False,"status":"OPEN"}
        try:
            after=event["result"]
            if event["operation"]=="method.error":
                states=[e for n,e in chronology.source if at<n<index and e["pid"]==event["pid"]
                    and e["operation"]=="method.after" and e["arguments"]==event["arguments"]]
                require(len(states)==1,"original method error requires its full actually retained post-state")
                after=states[0]["result"]
            driver=ObservedMethodReplay(effects,chronology.held)
            driver.chronology=chronology
            driver.begin_index=at
            require(getattr(chronology.held,"operand_registry",None) is not None,"actual qualified stock registry")
            driver.operands=chronology.held.operand_registry.fork(at,event["pid"])
            # Validate state AT the predicate, not a later mutated alias. The
            # observed graph was made by the actual Source before its require.
            actual_predicates=[(n,e) for n,e in chronology.source if at<n<index and e['pid']==event['pid']
                and e['operation']=='grammar.predicate']
            nested_calls={e["arguments"][1][1] for n,e in chronology.source if at<n<index and e["pid"]==event["pid"]
                and e["operation"]=="method.enter"}
            calls={call}|nested_calls
            state_rows=[(n,e) for n,e in chronology.source if at<n<index and e['pid']==event['pid']
                and e['operation']=='method.predicate.state' and e['arguments'][1][1] in calls]
            predicate_cursor=[0];state_cursor=[0]
            def at_predicate(frame):
                k=predicate_cursor[0]
                require(k<len(actual_predicates),'every predicted predicate needs actual reached observation')
                n,observed=actual_predicates[k];predicate_cursor[0]+=1
                site=G.PREDICATE_SITE_MAP[frame.f_lineno]
                require(G.source_predicate(observed['arguments'][1][0])['id']==site,
                    'exact original per-expression order, not expected outcome fixture')
                locals_now={key:item for key,item in frame.f_locals.items() if key!='self'
                    and type(item) in (type(None),str,bool,int,float,bytes,list,tuple,dict,set,frozenset)}
                require(same_typed(observed['arguments'][1][1],locals_now),'actual ordered predicate full locals')
                if 'self' in frame.f_locals:
                    j=state_cursor[0];require(j<len(state_rows),'predicate actual self-state required')
                    m,state=state_rows[j];state_cursor[0]+=1
                    require(m<n,'state captured before the same actual predicate')
                    driver.after(state['result'],frame.f_locals['self'])
            driver.predicate_callback=at_predicate
            row["body_result"]=driver.run(name,start["result"],after,event["error"])
            require(predicate_cursor[0]==len(actual_predicates) and state_cursor[0]==len(state_rows),
                'all actual predicate/state rows consumed, including first fault and no later invented rows')
            predicted=row['body_result']['ordered_predicates']
            require(len(predicted)==len(actual_predicates), 'full predicted and actual predicate sequence')
            for prior,(_,seen) in zip(predicted,actual_predicates):
                require(type(prior['ok']) is type(seen['result']) and prior['ok']==seen['result'],
                    'same ordered actual boolean/first fault, no normalization')
            fault=row['body_result']['first_fault']
            earliest=next((seen['result'] for _,seen in actual_predicates if seen['result'] is not True),None)
            if fault is None:
                require(earliest is None,'successful body has no actual first fault')
            else:
                require(earliest is not None and fault['ok']==earliest and fault['ok'] is not True,
                    'first fault is the earliest actual failed predicate')
            row.update(full_body_replay=True,status="LOCAL_BODY_AND_SELFSTATE_REPLAYED")
        except Exception as error:row["exact_replay_residual"]=str(error)
        checked.append(row)
    require(not stack,"every reached method owns an actual return/error, never absence-as-completion")
    return checked

class RootSourceTrace:
    def __init__(self,chronology):
        self.chronology=chronology

    def check_ref(self,wire):
        require(type(wire) is dict,"full actual native Source trace object")
        if wire.get("schema")=="friday.a132.bootstrap-reached-full-rows.v1":
            require(set(wire)=={"schema","events","calls","complete","recording_failure","Root_owned_sideband_attached"}
                and type(wire["events"]) is list and type(wire["calls"]) is int
                and wire["calls"]==len(wire["events"]) and wire["complete"] is True
                and wire["recording_failure"] is None and wire["Root_owned_sideband_attached"] is False,
                "full bootstrap trace grammar, never Root authentication")
            actual=[event["actual_arrived_event"]["result"] for _,event in self.chronology.source
                if event.get("bootstrap_original_row")]
            require(len(actual)>=len(wire["events"]) and same_typed(actual[:len(wire["events"])],wire["events"]),
                "every original pending bootstrap row actually reached Root-held arrival")
            return
        require(set(wire)=={"schema","producer","calls","complete","recording_failure","Source_issued_Root_fact"}
            and wire["schema"]=="friday.a132.Root-held-full-events-ref.v1"
            and type(wire["calls"]) is int and wire["calls"]>=0 and wire["complete"] is True
            and wire["recording_failure"] is None and wire["Source_issued_Root_fact"] is False,
            "full exact Source reference grammar")
        producer=wire["producer"]
        require(type(producer) is dict and set(producer)=={"schema","actor","pid","event_count","frames_sent",
            "transport_successful_bytes","transport_attempts","recording_failure","producer_complete_claim",
            "independent_Root_confirmation"} and producer["schema"]=="friday.a132.Root-held-actor-events-ref.v1"
            and producer["recording_failure"] is None and producer["producer_complete_claim"] is True
            and producer["independent_Root_confirmation"]=="REQUIRED_NOT_SUPPLIED_BY_SOURCE",
            "exact performing producer reference, never promoted to Root fact")
        for key in ("pid","event_count","frames_sent","transport_successful_bytes","transport_attempts"):
            require(type(producer[key]) is int and producer[key]>=0,"actual producer counters exact int, bool refused")
        events=[event for _,event in self.chronology.source if event["pid"]==producer["pid"]
            and event["sequence"]<producer["event_count"]]
        require(len(events)==producer["event_count"] and all(event["actor"]==producer["actor"] for event in events),
            "exact complete per-actor original prefix in independently held Root ledger")
        frames=[row for _,row in self.chronology.root if row["operation"]=="sideband.frame_received"
            and row["pid_claim"]==producer["pid"] and row["sequence_claim"]<producer["event_count"]]
        require(len(frames)==producer["frames_sent"] and sum(row["frame_bytes"] for row in frames)==producer["transport_successful_bytes"]
            and producer["transport_attempts"]>=len(frames),"actual Root receiver frame/byte correspondence")

    def replay(self,wire):
        self.check_ref(wire)
        if "events" in wire:
            yield from (row[:4] for row in wire["events"])
            return
        producer=wire["producer"]
        for _,event in self.chronology.source:
            if event["pid"]!=producer["pid"] or event["sequence"]>=producer["event_count"]:
                continue
            if event.get("bootstrap_original_row"):
                yield event["actual_arrived_event"]["result"][:4]
            elif type(event["arguments"]) is list and len(event["arguments"])==3:
                owner,args,_scope=event["arguments"]
                yield [owner,event["operation"],args,["returned",event["result"]]
                    if event["error"] is None else ["error",event["error"]["type"],event["error"]["errno"],
                        event["error"]["module"],event["error"]["args"],event["error"]["text"],
                        event["error"]["filename"],event["error"]["filename2"]]]

def node(document, path):
    result = document
    for key in path:
        result = result[key]
    return result

def fault_relation(case,fault,input_documents,chronology):
    # Expectations are literal pinned performing expressions, never supplied
    # type/value/keyset/path selectors. The request can only name the actual
    # earliest reached rejection or full original operation error.
    require(type(fault) is dict and set(fault)=={"event_index","predicate_id"},
        "frozen exact first-fault reference only; arbitrary expected/path refused")
    predicates=[(index,event) for index,event in chronology.source
        if event["operation"]=="grammar.predicate"]
    rejecting=[(index,event) for index,event in predicates if not event["result"]]
    if case in FIRST_CAUSES and rejecting:
        index,event=rejecting[0]
        row=G.source_predicate(event["arguments"][1][0])
        require(fault=={"event_index":index,"predicate_id":row["id"]},
            "exact first originally required per-field predicate, not a family label")
        first=chronology.first_guard()
        require(first is not None and first[0]>index and first[1] in FIRST_CAUSES[case]
            and first[2]==event["arguments"][2],
            "ordered prior reachability and exact original first refusing scope/cause")
        return row
    error_events=[(index,event) for index,event in chronology.source if event["error"] is not None
        and not event["operation"].endswith(".begin")]
    if case in ("proc-acquisition-error","spawn-constructor-error"):
        allowed=("open","pidfd_open") if case=="proc-acquisition-error" else ("spawn.error",)
        error_events=[row for row in error_events if row[1]["operation"] in allowed]
    if case=="sender-read-bound":
        denials=chronology.operations("read.denied")
        require(denials and fault=={"event_index":denials[0][0],"predicate_id":"original.sender.read_bound"},
            "first full original cap-prefix denial")
        return {"id":"original.sender.read_bound"}
    terminals={"adopted-child-pending":"cleanup","terminal-tree-list-missed":"cleanup",
        "wait4-unavailable":"cleanup","handle-close-error":"cleanup",
        "terminal-original-end":"delivery","terminal-pipe-error":"delivery"}
    if case in terminals:
        returned=chronology.operations("sender.returned_outcome")
        require(len(returned)==1 and returned[0][1]["result"][terminals[case]]["confirmed"] is False
            and fault=={"event_index":returned[0][0],"predicate_id":"original.case."+case},
            "fixed original actual cleanup/delivery domain, never arbitrary expected fields")
        return {"id":fault["predicate_id"]}
    require(error_events and fault=={"event_index":error_events[0][0],
        "predicate_id":"original.operation."+error_events[0][1]["operation"]},
        "earliest actually reached full original operation/stdlib/cleanup exception")
    return {"id":fault["predicate_id"],"error":error_events[0][1]["error"]}

def read_prefix_denial(chronology, native):
    charged = calls = 0
    first = None
    for _, event in chronology.source:
        if event["operation"] not in ("guarded_read", "read.denied"):
            continue
        if event["operation"] == "read.denied":
            row = event["result"]
            if first is None:
                first = row
            require(type(row["requested_cap"]) is int and row["requested_cap"] > 0
                and row["prefix_events"] == calls and row["prefix_returned_bytes"] == charged
                and row["charged"] is False and row["asserted_cap_plus_one"] is False
                and charged + row["requested_cap"] > 33554432,
                "original actual complete prefix denial, not cap+1 substitution")
            calls += 1
            continue
        row = event["result"]
        if type(row) is list:
            row = row[0]
        require(type(row) is dict and type(row["repeat"]) is int and row["repeat"] > 0
            and type(row["charged_observed"]) is bool, "actual guarded read row")
        calls += row["repeat"]
        if row["charged_observed"]:
            charged += row["returned_bytes"]
    require(first is not None and first == native["read_denied"]
        and charged == native["sender_read_bytes_observed"], "whole original read witness bound to native result")

def case_result(case, native, returned, native_exit, chronology):
    if case == "positive":
        require(native_exit == 0 and returned["status"] == "RUN_REPORTED"
            and returned["reported_entrypoint_returncodes"] == [0]
            and returned["cleanup"]["confirmed"] is True and returned["delivery"]["confirmed"] is True,
            "actual original complete ordinary success")
    elif case == "nonzero-dispatcher":
        require(native_exit == 125 and returned["status"] == "RUN_REPORTED"
            and type(returned["reported_entrypoint_returncodes"]) is list
            and len(returned["reported_entrypoint_returncodes"]) == 1
            and type(returned["reported_entrypoint_returncodes"][0]) is int
            and returned["reported_entrypoint_returncodes"][0] != 0, "actual real nonzero dispatcher")
    else:
        require(native_exit == 125, "every original ordinary negative native125")
    if case == "strict-inherited-fd":
        require(native["cause"] == "caller_bootstrap" and native["reason"] == "strict_inherited_0_1_2",
            "actual strict entry refusal, not a stock prelude fiction")
    if case == "sender-read-bound":
        read_prefix_denial(chronology, native)
    if case == "controller-output-bound":
        require(any(r["capture_stopped"] is True and r["overflow_probe_hex"]
            for r in native["controller_pipe_observation"].values()), "actual stopped prefix and first overflow probe")
    if case in ("adopted-child-pending", "terminal-tree-list-missed", "wait4-unavailable", "handle-close-error"):
        require(returned["cleanup"]["confirmed"] is False, "sticky UNKNOWN qualified owned cleanup")
    if case in ("terminal-original-end", "terminal-pipe-error"):
        require(returned["delivery"]["confirmed"] is False, "actual failed original terminal handoff")
    if case == "terminal-original-end":
        require(returned["delivery"].get("reason") == "failed_handoff_deadline", "same original dual ends, no refresh")
    if case == "terminal-pipe-error":
        require(returned["delivery"].get("error_type") in ("OSError", "BrokenPipeError"), "actual pipe error type")
    if case == "ordinary-regular-artifact-refusal":
        require(native["reported_controller_output_claim"]["status"] == "RUN_RETURNED"
            and native["reported_entrypoint_returncodes"]==[1]
            and returned["reported_entrypoint_returncodes"]==[1],
            "exact original regular-artifact negative: RUN_RETURNED controller reports dispatcher1")

class All32RootConsumer:
    def __init__(self, pinned_schema_data, independently_held_ledger):
        require(type(pinned_schema_data) is dict and set(pinned_schema_data)=={"schema","GO","cases","count","no_case_cut","runtime"}
            and type(pinned_schema_data["count"]) is int and pinned_schema_data["count"] == 32
            and type(pinned_schema_data["cases"]) is list and len(pinned_schema_data["cases"])==32
            and {r["case"] for r in pinned_schema_data["cases"]} == set(CASES), "exact full original all32, duplicate cases refused")
        self.schemas = {r["case"]: r for r in pinned_schema_data["cases"]}
        self.chronology = RootChronology(independently_held_ledger)

    def consume(self, case, variables, variable_bindings, input_documents,
            fault, native, returned, native_wire, native_exit):
        require(case in self.schemas, "original all32 case, no subset acceptance")
        if not self.chronology.source:
            stdout=b"".join(bytes.fromhex(row["raw_hex"]) for _,row in self.chronology.root
                if row["operation"]=="caller.stdout.received")
            stderr=b"".join(decode(row["raw"],self.chronology.held) for _,row in self.chronology.root
                if row["operation"]=="caller.stderr.received")
            require(stdout==native_wire and native is None and returned is None and not input_documents,
                "actual stock-prelude failure never fabricated as Caller/Sender ordinary JSON")
            result=N.TypedOrdinaryContract().consume_stock_prelude(case,stdout,stderr,native_exit,False)
            result.update(ordinary_case_reached=False,Source_contract_checked=False)
            return result
        self.chronology.bind_variables(variables, variable_bindings,case,fault)
        prefix=self.chronology.native_prefix(native_wire,native_exit,
            allow_failed_delivery=case in ("terminal-original-end","terminal-pipe-error","strict-inherited-fd"))
        # A complete final line is compared byte-for-byte with the actual caller
        # capture. Partial/bootstrap lines remain explicit distinct failure domains.
        lines=native_wire.splitlines(keepends=True)
        actual_returns=self.chronology.operations("sender.returned_outcome")
        if case=="strict-inherited-fd":
            require(returned is None and not actual_returns,"unreached Sender return never supplied as model")
        else:
            require(len(actual_returns)==1 and same_typed(returned,actual_returns[0][1]["result"]),
                "whole returned/postdelivery outcome binds the actual Source producer result")
        intended=[e["result"] for _,e in self.chronology.source
            if e["operation"]=="native.before_delivery" and type(e["result"]) is bytes]
        require(len(intended)==1,"one complete actual intended native producer body required")
        source_failed=case in ("terminal-original-end","terminal-pipe-error","strict-inherited-fd")
        expected_wire=N.OrdinaryNativeContract.bootstrap_wire(native) if native.get("cause")=="caller_bootstrap" else canonical(native)
        require(intended[0]==expected_wire,"intended full native body uses exact performing byte recipe")
        if prefix["complete"] and not source_failed:
            require(lines and lines[-1].endswith(b"\n") and canonical(native) == lines[-1],
                "exact full actual pre-delivery native object, no swapped report schema")
        else:
            # The complete intended body must itself have arrived as a full
            # Source producer result. Its genuine bytes, never a supplied model,
            # determine the exact possibly-empty original-channel prefix.
            prior=(b"" if returned is None else b"".join(bytes.fromhex(row["wire_hex"])[:row["successful_prefix_bytes"]]
                for row in returned.get("root_custody_request_observations",[])))
            require(native_wire.startswith(prior) and intended[0].startswith(native_wire[len(prior):]),
                "failed complete request+terminal prefix binds actual intended producer bytes")
        N.FullEventJournal=RootSourceTrace(self.chronology)
        native_rules=N.OrdinaryNativeContract()
        if native.get("cause")=="caller_bootstrap":
            native_rules.outside_early_capture(intended[0],native_wire,native_exit,bootstrap=True)
        elif native.get("spawned") is False:
            native_rules.before_early_delivery(native,intended[0])
            native_rules.outside_early_capture(intended[0],native_wire,native_exit)
        else:
            native_rules.before_delivery(native,intended[0],native["root_custody_request_bytes_written"])
            native_rules.after_delivery(intended[0],returned,len(native_wire))
            native_rules.outside_capture(returned,intended[0],native_wire,native_exit)
        streams=self.chronology.captured_producers(native)
        segments,controller_stdout,controller_stderr,worker_files,worker_reader,byte_producer=streams
        ordinary=N.TypedOrdinaryContract(worker_reader)
        original_relations=ordinary.consume(case,variables,native if returned is None else returned,
            intended[0],native_wire,native_exit,segments,controller_stdout,controller_stderr,worker_files,byte_producer)
        captured_stderr=b"".join(decode(row["raw"],self.chronology.held) for _,row in self.chronology.root
            if row["operation"]=="caller.stderr.received")
        delivered_stderr=b"".join(bytes.fromhex(row["raw_prefix_hex"]) for _,row in self.chronology.root
            if row["operation"]=="original_fd2.write_returned")
        require(captured_stderr.startswith(delivered_stderr) and (source_failed or captured_stderr==delivered_stderr),
            "full original stderr/prefix delivery, never assumed silence")
        self.chronology.bind_inputs(input_documents,native)
        nested_grammar=self.chronology.actual_grammar_sites()
        effectful_methods=method_sites(self.chronology)
        if case in ("positive","nonzero-dispatcher"):
            require({row["name"] for row in nested_grammar}>={"validate_stage1","body_shape","validate_claim",
                "validate_stage2","validate_report","check_held_observation","context_shape","namespace_shape","custody_shape"},
                "complete actually reached performing nested grammar in either ordinary full branch")
        if case not in ("positive", "nonzero-dispatcher"):
            fault_relation(case, fault, input_documents, self.chronology)
        case_result(case, native, returned, native_exit, self.chronology)
        template = self.schemas[case]
        # These exact inherited contracts are kept whole. Every nested grammar,
        # byte recipe, first-fault reachability and all variable crosslinks remain
        # separate required work where the bound proof below is absent; labels
        # and independently held hashes alone never promote that work to closure.
        return {"case":case,"actual_reached_relations_checked":True,
            "original_typed_consumer_relations":original_relations,
            "original_producer_segments":len(segments),"exclusive_worker_streams":sorted(worker_files),
            "full_actual_caller_stderr_bytes":len(captured_stderr),
            "ordered_performing_nested_grammar":nested_grammar,
            "ordered_effectful_method_state_replays":effectful_methods,
            "whole_effectful_method_closure":bool(effectful_methods) and all(row["full_body_replay"] for row in effectful_methods),
            "inherited_contract_pin":template["inherited_contract_pin"],
            "joined_r01_r07":"AUTHORED_NOT_RUN",
            "complete_all_nested_schema_and_variable_closure":False,
            "complete_all_actor_coverage":False,"whole_resource_proof":False,
            "Source_contract_checked":False,"full_scope_status":"FINITE_UNSATISFIED_DECISION",
            "runtime_credit":False,"gate_credit":False,"release_credit":False,"GO":False}

def drive_existing_outer_native(tail,launcher):
    """Called same-parent cleanup. Direct caller wait is NOT helper native end.
    This Python driver retains actual failure objects and aliases; it cannot
    certify its own C finalization/last heap/FD retirement. That ABI stays CODE.
    Creations are observed first. Descriptor cleanup is recorded while the
    recorder is live. The tail is sealed next. The existing outside owner then
    holds the recorder-close outcome and only then closes that descriptor.
    """
    owner={'launcher':launcher,'waits':[],'descriptor_cleanup':None,'finish':None,
        'last_close':None,'raw_error':None,'same_existing_parent':True,
        'helper_native_end_verified':False,
        'native_tool_helper_retirement':'NOT_QUALIFIED'}
    tail.own_raw(owner)
    rows=list(getattr(launcher,"generation_custody",{}).values())
    try:
        for generation in rows:
            if generation.get("wait4") is None:owner['waits'].append(tail.outside_wait4(generation))
        owner['descriptor_cleanup']=tail.settle_helper_and_tool(launcher)
        owner['finish']=tail.finish()
        tail.final_close_by_native_recipient(owner)
        owner['helper_native_end_verified']=False
    except BaseException as error:
        owner['raw_error']=error;owner['traceback']=error.__traceback__
        owner['helper_native_end_verified']=False
        raise
    return owner
