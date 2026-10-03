"""Future independently Root-owned held-exec bootstrap; stock imports only."""
import hashlib
import importlib.abc
import importlib.util
import json
import os
import sys
import struct
import copy
import contextvars
import types
# lossless-history-codec-start
def _fail(cause):
    raise RuntimeError(cause)


def _history_chunks(items):
    if not items:
        return []
    chunks=[items[i:i + 512] for i in range(0,len(items),512)]
    if len(chunks)>512:_fail("history_chunks")
    return chunks


_HISTORY_ABSENT = -2
_HISTORY_NONE = -1
_HISTORY_FIELDS = (
    "holder", "credit", "status", "identity9_decimal_strings", "attempted_close",
    "close_history", "acquisition_error", "last_known_holder", "last_known_credit",
    "history_arena", "close_policy", "cancellation_acknowledged", "parent_pid",
    "parent_acquisition",
)
_HISTORY_ROW_KEYS = ("slot", "generation", "fd")
_HISTORY_PATTERN_LEN = len(_HISTORY_FIELDS) + 1


def _history_cell_key(value):
    if value is None:
        return ("N",)
    kind = type(value)
    if kind is bool:
        return ("B", value)
    if kind is int:
        return ("I", value)
    if kind is str:
        return ("S", value)
    return ("J", json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))


def encode_history_rows(rows):
    """Share identical identity9 tuples and repeated closed-row columns.

    Slot, generation and fd stay on every row. Absent keys stay absent.
    None stays None and is not the status string UNKNOWN.
    """
    if type(rows) is not list:
        _fail("history_rows")
    cells = []
    cell_at = {}
    patterns = []
    pattern_at = {}
    triples = []

    def intern(value):
        key = _history_cell_key(value)
        found = cell_at.get(key)
        if found is None:
            found = len(cells)
            cell_at[key] = found
            cells.append(value)
        return found

    for row in rows:
        if type(row) is not dict:
            _fail("history_row")
        vector = []
        for name in _HISTORY_FIELDS:
            if name not in row:
                vector.append(_HISTORY_ABSENT)
            elif row[name] is None:
                vector.append(_HISTORY_NONE)
            else:
                vector.append(intern(row[name]))
        extra = {key: value for key, value in row.items() if key not in _HISTORY_FIELDS and key not in _HISTORY_ROW_KEYS}
        vector.append(_HISTORY_ABSENT if not extra else intern(extra))
        key = tuple(vector)
        index = pattern_at.get(key)
        if index is None:
            index = len(patterns)
            pattern_at[key] = index
            patterns.append(vector)

        def cell_or_sentinel(name):
            if name not in row:
                return _HISTORY_ABSENT
            if row[name] is None:
                return _HISTORY_NONE
            if type(row[name]) is not int:
                _fail("history_row_index")
            return row[name]

        triples.append([index, cell_or_sentinel("slot"), cell_or_sentinel("generation"), cell_or_sentinel("fd")])
    codec = {"schema": "friday.a190.lossless-history-rows.v1", "cells": _history_chunks(cells),
        "patterns": _history_chunks(patterns), "rows": _history_chunks(triples)}
    if len(codec["cells"]) > 512 or len(codec["patterns"]) > 512 or len(codec["rows"]) > 512:
        _fail("history_chunks")
    return codec


def _history_flat(chunks, cause):
    if type(chunks) is not list or len(chunks) > 512:
        _fail(cause)
    out = []
    for part in chunks:
        if type(part) is not list or len(part) > 512:
            _fail(cause)
        out.extend(part)
    return out


def decode_history_rows(codec):
    """Rebuild every row in order from the shared tables. Values are copied."""
    if type(codec) is not dict or codec.get("schema") != "friday.a190.lossless-history-rows.v1":
        _fail("history_codec")
    if set(codec) != {"schema", "cells", "patterns", "rows"}:
        _fail("history_codec")
    cells = _history_flat(codec.get("cells"), "history_cells")
    patterns = _history_flat(codec.get("patterns"), "history_patterns")
    triples = _history_flat(codec.get("rows"), "history_rows")
    restored = []
    for triple in triples:
        if type(triple) is not list or len(triple) != 4 or any(type(item) is not int for item in triple):
            _fail("history_triple")
        index = triple[0]
        if not 0 <= index < len(patterns):
            _fail("history_pattern")
        pattern = patterns[index]
        if type(pattern) is not list or len(pattern) != _HISTORY_PATTERN_LEN or any(type(item) is not int for item in pattern):
            _fail("history_pattern")
        row = {}
        for name, ptr in zip(_HISTORY_FIELDS, pattern):
            if ptr == _HISTORY_ABSENT:
                continue
            if ptr == _HISTORY_NONE:
                row[name] = None
                continue
            if not 0 <= ptr < len(cells):
                _fail("history_cell")
            row[name] = copy.deepcopy(cells[ptr])
        extra_ptr = pattern[-1]
        if extra_ptr != _HISTORY_ABSENT:
            if not 0 <= extra_ptr < len(cells) or type(cells[extra_ptr]) is not dict:
                _fail("history_extra")
            row.update(copy.deepcopy(cells[extra_ptr]))
        identity = row.get("identity9_decimal_strings", None)
        if "identity9_decimal_strings" in row and identity is not None:
            if type(identity) is not list or len(identity) != 9 or any(type(part) is not str for part in identity):
                _fail("history_identity9")
        if "close_history" in row and row["close_history"] is not None and type(row["close_history"]) is not list:
            _fail("history_close")
        if "cancellation_acknowledged" in row and row["cancellation_acknowledged"] is not None and type(row["cancellation_acknowledged"]) is not bool:
            _fail("history_flag")
        for name, sentinel in (("slot", triple[1]), ("generation", triple[2]), ("fd", triple[3])):
            if sentinel == _HISTORY_ABSENT:
                continue
            row[name] = None if sentinel == _HISTORY_NONE else sentinel
        restored.append(row)
    return restored


# The existing codec is the only full chronology. Collection descriptors
# select it in order; they never carry another members/order chronology.
def history_collection(count, identity, generation, domain_identity, owner_pid):
    return {"collection_identity": str(identity), "kind": "list",
        "owner_pid": owner_pid, "generation": generation,
        "domain_identity": domain_identity, "journal_row_list": True,
        "whole_domain": False, "row_count": count,
        "projection": "history_codec.rows"}

def validate_history_collection(value, count=None):
    fields={"collection_identity","kind","owner_pid","generation",
        "domain_identity","journal_row_list","whole_domain","row_count","projection"}
    if type(value) is not dict or set(value)!=fields:
        _fail("history_collection")
    if (type(value["collection_identity"]) is not str or value["kind"]!="list"
            or type(value["owner_pid"]) is not int
            or value["journal_row_list"] is not True
            or value["whole_domain"] is not False
            or value["projection"]!="history_codec.rows"
            or type(value["row_count"]) is not int or not 0<=value["row_count"]<=65536):
        _fail("history_collection")
    for name, kind in (("generation",int),("domain_identity",str)):
        if value[name] is not None and type(value[name]) is not kind:
            _fail("history_collection")
    if count is not None and value["row_count"]!=count:
        _fail("history_collection_count")
    return value

def history_counted_chunks(chunks, count, cause):
    if type(count) is not int or not 0<=count<=512*512:
        _fail(cause)
    rows=_history_flat(chunks,cause)
    if len(rows)!=count:
        _fail(cause)
    return rows

def history_book_rows(book):
    if type(book) is not dict or book.get("truncated") is not False:
        _fail("history_book")
    rows=decode_history_rows(book.get("history_codec"))
    if (type(book.get("journal_count")) is not int or len(rows)>65536
            or len(rows)!=book["journal_count"]):
        _fail("history_book_count")
    validate_history_collection(book.get("collection"),len(rows))
    credits=history_counted_chunks(book.get("credit_chunks"),book.get("credit_count"),"history_credits")
    faults=history_counted_chunks(book.get("fault_chunks"),book.get("fault_count"),"history_faults")
    tokens={}
    for credit in credits:
        if (type(credit) is not dict or set(credit)!={"token","slots","closed"}
                or type(credit["slots"]) is not int or not 0<=credit["slots"]<=128
                or type(credit["closed"]) is not bool
                or type(credit["token"]) not in (str,int) or credit["token"] in tokens):
            _fail("history_credit")
        tokens[credit["token"]]=credit
    active=[]
    for row in rows:
        if type(row.get("credit")) not in (str,int) or row["credit"] not in tokens:
            _fail("history_row_credit")
        if row.get("status") in ("ACQUIRED","HELD","UNKNOWN","ROOT_PREOWNED_BIND_PENDING","PREOWNED_CHILD_TABLE"):
            if type(row.get("fd")) is not int:
                _fail("history_row_fd")
            active.append(row["fd"])
    if type(book.get("pending_fds")) is not list or book["pending_fds"]!=sorted(active):
        _fail("history_pending")
    return rows

def history_domain_books(domain):
    if (type(domain) is not dict or set(domain)!={"domain_identity","arena_token","generation",
            "history_count","history_max","journal_count","journal_chunks","truncated"}
            or type(domain["domain_identity"]) is not str or domain["history_max"]!=65536
            or domain["truncated"] is not False):
        _fail("history_domain")
    if type(domain["generation"]) is not int or not 0<=domain["generation"]<=10**21:
        _fail("history_domain_generation")
    if type(domain["history_count"]) is not int or not 0<=domain["history_count"]<=65536:
        _fail("history_domain_count")
    books=history_counted_chunks(domain["journal_chunks"],domain["journal_count"],"history_journals")
    identities=set();row_identities=set();total=0
    for book in books:
        rows=history_book_rows(book);total+=len(rows)
        collection=book["collection"]
        identity=collection["collection_identity"]
        if (collection["domain_identity"]!=domain["domain_identity"]
                or collection["generation"]!=domain["generation"] or identity in identities):
            _fail("history_domain_alias")
        identities.add(identity)
        for row in rows:
            key=tuple((name in row,row.get(name)) for name in ("credit","slot","generation"))
            if key in row_identities:_fail("history_domain_row_duplicate")
            row_identities.add(key)
    if total!=domain["history_count"]:_fail("history_domain_history_count")
    return books

def fork_history_book(packet):
    if type(packet) is not dict or packet.get("schema") not in ("friday.a181.fork-owner-graph.v2","friday.sol090.fork-owner-full.v3"):
        _fail("fork_history_packet")
    reference=packet.get("inherited")
    if type(reference) is not dict or set(reference)!={"collection_identity"}:
        _fail("fork_history_reference")
    books=history_domain_books(packet.get("new_journals"))
    matched=[book for book in books if book["collection"]["collection_identity"]==reference["collection_identity"]]
    if len(matched)!=1:
        _fail("fork_history_reference")
    book=matched[0]
    if (book.get("owner_pid")!=packet.get("owner_pid")
            or book.get("parent_pid")!=packet.get("parent_pid")
            or book["collection"]["owner_pid"]!=packet.get("owner_pid")):
        _fail("fork_history_owner")
    return book,books

# lossless-history-codec-end


class StockChildJournal:
    """Stock bridge to the exact table prospectively owned before fork/exec."""
    def seed_preowned(self,packet,pid,fds):
        # Exact already received pre-exec graph, retained BEFORE constructor.
        book,books=fork_history_book(packet)
        if packet['owner_pid']!=pid or packet['parent_pid']!=os.getppid():
            raise RuntimeError('bootstrap-owner-identity')
        self.rows=[];self.grants={};self.faults=[];self.state=None;self.meter=None
        self.credit_rows=[];self.pre_exec_owner_graph=packet;self.binding_complete=False
        self.expected_fds=tuple(fds)
        for fd in fds:
            matches=[row for part in books for row in history_book_rows(part)
                if row.get('fd')==fd and row.get('status')=='HELD']
            if len(matches)!=1:raise RuntimeError('bootstrap-preowned-actual-row')
            row=dict(matches[0]);row['parent_acquisition']=matches[0];row['journal']=self
            row['close_cell']={'attempt':1,'status':'NOT_ATTEMPTED','error':None}
            self.rows.append(row)
    def __init__(self,fds):
        if getattr(self,'pre_exec_owner_graph',None) is not None:
            if tuple(fds)!=self.expected_fds:raise RuntimeError('bootstrap-preowned-constructor-drift')
            return
        self.rows=[];self.grants={};self.faults=[];self.state=None;self.meter=None
        self.credit_rows=[];self.pre_exec_owner_graph=None
        for fd in fds:
            self.rows.append({"fd":fd,"status":"ROOT_PREOWNED_BIND_PENDING","credit":None,
                "identity9_decimal_strings":None,"generation":None,"slot":None,
                "holder":"bootstrap-inherited","last_known_holder":"bootstrap-inherited",
                "last_known_credit":None,"attempted_close":None,"close_history":[],
                "close_cell":{"attempt":1,"status":"NOT_ATTEMPTED","error":None},"journal":self})
    @property
    def fds(self):return {r['fd'] for r in self.rows if r['status'] in ('HELD','UNKNOWN','ROOT_PREOWNED_BIND_PENDING')}
    def bind(self,packet,pid):
        if packet['owner_pid']!=pid or packet['parent_pid']!=os.getppid():raise RuntimeError('bootstrap-owner-identity')
        inherited_book,books=fork_history_book(packet)
        latest={}
        # The inherited projection names a book already present exactly once
        # in the primary domain; no second decoded/inherited chronology.
        for book in books:
            for r in history_book_rows(book):
                if r.get('status')=='HELD':latest[r['fd']]=r
        for row in self.rows:
            prior=latest.get(row['fd'])
            if prior is None:raise RuntimeError('bootstrap-owner-row')
            info=os.fstat(row['fd'])
            identity=[str(x) for x in (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)]
            if identity!=prior['identity9_decimal_strings']:raise RuntimeError('bootstrap-owner-drift')
            row.update({k:v for k,v in prior.items() if k not in ('journal','close_cell')})
            row['parent_acquisition']=prior;row['journal']=self
        credits={}
        for book in books:
            for credit in history_counted_chunks(book['credit_chunks'],book['credit_count'],'bootstrap-credit-count'):
                prior=credits.get(credit['token'])
                if prior is not None and prior!=credit:raise RuntimeError('bootstrap-credit-alias')
                credits[credit['token']]=credit
        self.credit_rows=list(credits.values())
        known={row['fd'] for row in self.rows}
        for fd,prior in latest.items():
            if fd not in known and prior.get('close_policy')=='KEEP':
                row=dict(prior);row['parent_acquisition']=prior;row['journal']=self
                row['close_cell']={'attempt':1,'status':'NOT_ATTEMPTED','error':None}
                self.rows.append(row)
        self.pre_exec_owner_graph=packet;self.binding_complete=True
    def connect(self,meter):
        from observer import Reservation
        self.meter=meter;self.state=meter.fd_state;self.state.attach(self)
        for fact in self.credit_rows:
            c=Reservation(meter,fact['token'],fact['slots']);c.fd_rows={};c.fd_serial=0
            self.grants[c.token]=c
        for row in self.rows:
            c=self.grants[row['credit']];c.fd_rows[row['slot']]=row;c.fd_serial=max(c.fd_serial,row['slot'])
            self.state.count+=1;self.state.generation=max(self.state.generation,row['generation'])
            self.state.note_row(row)
        meter.retain_local_owner(self)
    def close_one(self,fd):
        r=next((r for r in self.rows if r['fd']==fd and r['status']=='HELD'),None)
        if r is None:return
        a=r['close_cell'];r['close_history'].append(a);a['status']='ATTEMPTED';r['attempted_close']='ATTEMPTED'
        try:os.close(fd)
        except BaseException as exc:
            r['status']='UNKNOWN';r['attempted_close']='UNKNOWN';a['status']='UNKNOWN'
            a['error']={'class':type(exc).__name__,'errno':getattr(exc,'errno',None),'text':str(exc)};self.faults.append(a['error'])
        else:
            r['status']='CLOSED';r['attempted_close']='CLOSED';a['status']='CLOSED'
            if self.grants:self.grants[r['credit']].fd_rows.pop(r['slot'],None)
        if self.meter is not None:self.meter.publish_local_owners()
    def graph(self):
        from lifetime import collection_for
        rows=[{k:v for k,v in r.items() if k not in ('journal','close_cell')} for r in self.rows]
        return {'history_codec':encode_history_rows(rows),'journal_count':len(rows),'pending_fds':sorted(self.fds),
            'fault_count':len(self.faults),'fault_chunks':_history_chunks(self.faults),'truncated':False,
            'credit_count':len(self.credit_rows),'credit_chunks':_history_chunks(self.credit_rows),
            'pre_exec_owner_graph':self.pre_exec_owner_graph,
            'collection':collection_for(rows,self.state,id(self.rows))}


_PREFIX_SOURCE_MODULES=frozenset((
    'actor_context','admission','capacity','class_semantics','common',
    'consumer_bridge','custody','extraction','fact_bridge','independent_selector','launcher','lifetime',
    'native','normalization','observer','operations','retention','roles','root_tool_adapter','snapshot_producer',
    'actor_bootstrap','existing_root_caller','owned_prefix_bank','authority','bill','body_scope','canonical','capability','contract','declared_controls',
    'document_vector','document_windows','effects','fixtures','formats','ingress','material_literals',
    'performing_contracts','pins','producer_consumer','receipt_chain','recipe_planner','resource_meter',
    'runtime_consumer','schema_validate','semantics','whole_join'))
_PREFIX_BOOT_CLASSES=frozenset(('StockChildJournal','StockChannel','VerifiedSource'))
_PREFIX_CODE_FIELDS=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals',
    'co_stacksize','co_flags','co_code','co_consts','co_names','co_varnames',
    'co_filename','co_name','co_qualname','co_firstlineno','co_linetable',
    'co_exceptiontable','co_freevars','co_cellvars')
_PREFIX_NODE_CAP=262144
_PREFIX_WIRE=2_000_000


def build_early_stock_prefix(channel,book,error,raw,value,launch):
    # Stock prefix encoder. It imports no Source module and runs before admission.
    # Sealed bytes, the decoded bundle and the launch fact are preowned aliases.
    # Root already holds those bodies. Other selected values are arena nodes.
    # Foreign modules and builtins stay explicit non-bodies and cannot be complete.
    nodes=[];owners=[];seen={};pending=[];foreign=[];unsupported=[];token_nodes=[]
    def plant(obj,row):
        key=id(obj)
        if key in seen:raise RuntimeError('prefix-alias-dup')
        index=len(nodes)
        if index>=_PREFIX_NODE_CAP:raise RuntimeError('prefix-node-cap')
        seen[key]=index;nodes.append(row);owners.append(obj)
        return index
    def ref(obj):
        key=id(obj)
        if key in seen:
            index=seen[key]
            if owners[index] is not obj:raise RuntimeError('prefix-identity-reuse')
            return index
        index=len(nodes)
        if index>=_PREFIX_NODE_CAP:raise RuntimeError('prefix-node-cap')
        seen[key]=index;nodes.append(None);owners.append(obj);pending.append(obj)
        return index
    def emit_foreign(obj,reason):
        kind=type(obj)
        name=getattr(obj,'__name__',None)
        if type(name) is not str:name=None
        module=kind.__module__ if type(kind.__module__) is str else ''
        body={'module':module,'class':kind.__name__,'name':name}
        foreign.append({'module':module,'class':kind.__name__,'name':name,'reason':reason})
        return ['foreign-unwalked',body]
    def emit_unsupported(obj,reason):
        kind=type(obj)
        module=kind.__module__ if type(getattr(kind,'__module__',None)) is str else ''
        body={'module':module,'class':kind.__name__,'reason':reason}
        unsupported.append(dict(body))
        return ['unsupported',body]
    def kind_of(obj):
        if obj is None or obj is Ellipsis or obj is contextvars.Token.MISSING:
            return 'take'
        kind=type(obj)
        if kind in (bool,int,str,float,list,tuple,dict,bytes,bytearray,memoryview,set,frozenset,
                    range,slice,contextvars.ContextVar,contextvars.Context,contextvars.Token,
                    types.CodeType,types.TracebackType,types.FrameType,property,
                    types.FunctionType,types.MethodType,types.CellType,staticmethod,classmethod):
            return 'take'
        if isinstance(obj,BaseException):return 'take'
        if kind in (types.BuiltinFunctionType,types.BuiltinMethodType):return 'foreign'
        if kind is types.ModuleType:
            name=getattr(obj,'__name__',None)
            if name in _PREFIX_SOURCE_MODULES or name in ('__main__','actor_bootstrap'):return 'take'
            return 'foreign'
        if kind is type:return 'take'
        if kind.__module__ in _PREFIX_SOURCE_MODULES and hasattr(obj,'__dict__'):return 'take'
        if kind.__module__ in ('__main__','actor_bootstrap') and kind.__name__ in _PREFIX_BOOT_CLASSES and hasattr(obj,'__dict__'):
            return 'take'
        return 'block'
    def collect(mapping):
        taken={};names=[]
        for name,item in mapping.items():
            if type(name) is not str:return None
            if len(name)>2 and name.startswith('__') and name.endswith('__'):continue
            kind=kind_of(item)
            if kind=='foreign' or kind=='block':names.append(name)
            else:taken[name]=item
        return taken,names
    def hex_chunks(blob):
        return [bytes(blob[i:i+16384]).hex() for i in range(0,len(blob),16384)]
    def bytes_row(blob):
        label='bytes' if type(blob) is bytes else 'bytearray'
        return [label,channel.add(blob)]
    def row_for(obj):
        if obj is None:return ['none',None]
        if obj is Ellipsis:return ['ellipsis',None]
        if obj is contextvars.Token.MISSING:return ['token-missing',None]
        kind=type(obj)
        if kind is bool:return ['bool',obj]
        if kind is int:
            text=str(obj)
            if len(text)>4096:return emit_unsupported(obj,'int-width')
            return ['int',text]
        if kind is float:return ['float64',struct.pack('>d',obj).hex()]
        if kind is str:return ['str',obj]
        if kind in (bytes,bytearray):
            made=bytes_row(obj)
            if made is None:return emit_unsupported(obj,'bytes-unplaced')
            return made
        if kind is memoryview:
            return ['memoryview',{'object':ref(obj.obj),'full_bytes':ref(obj.tobytes()),
                'format':obj.format,'shape':ref(obj.shape),'strides':ref(obj.strides),'readonly':obj.readonly}]
        if kind in (list,tuple):
            return ['list' if kind is list else 'tuple',[ref(item) for item in obj]]
        if kind is dict:
            return ['dict',[[ref(key),ref(item)] for key,item in obj.items()]]
        if kind in (set,frozenset):
            return ['set' if kind is set else 'frozenset',[ref(item) for item in obj]]
        if kind is range:return ['range',[ref(obj.start),ref(obj.stop),ref(obj.step)]]
        if kind is slice:return ['slice',[ref(obj.start),ref(obj.stop),ref(obj.step)]]
        if kind is contextvars.ContextVar:
            empty=contextvars.Context()
            try:default=empty.run(obj.get);has_default=True
            except LookupError:default=None;has_default=False
            active=contextvars.copy_context();present=obj in active
            return ['context-var',{'name':obj.name,'has_default':has_default,'default':ref(default),
                'present':present,'current':ref(active[obj] if present else None)}]
        if kind is contextvars.Context:
            return ['context',[[ref(var),ref(item)] for var,item in obj.items()]]
        if kind is contextvars.Token:
            row=['context-token',{'var':ref(obj.var),'old_value':ref(obj.old_value),'transition':None}]
            token_nodes.append(obj)
            return row
        if kind is types.CodeType:
            if not all(hasattr(obj,name) for name in _PREFIX_CODE_FIELDS):
                return emit_unsupported(obj,'code-fields')
            return ['code-body',{name:ref(getattr(obj,name)) for name in _PREFIX_CODE_FIELDS}]
        if isinstance(obj,BaseException):
            notes=getattr(obj,'__notes__',None)
            if type(obj.__suppress_context__) is not bool:return emit_unsupported(obj,'suppress-context')
            return ['error',{'module':kind.__module__,'class':kind.__name__,'args':ref(obj.args),
                'attributes':ref(obj.__dict__),'cause':ref(obj.__cause__),'context':ref(obj.__context__),
                'suppress_context':obj.__suppress_context__,'traceback':ref(obj.__traceback__),'notes':ref(notes)}]
        if kind is types.TracebackType:
            return ['traceback',{'next':ref(obj.tb_next),'frame':ref(obj.tb_frame),'line':obj.tb_lineno,'lasti':obj.tb_lasti}]
        if kind is types.FrameType:
            return ['frame',{'filename':obj.f_code.co_filename,'name':obj.f_code.co_name,'line':obj.f_lineno,
                'locals':ref(obj.f_locals),'globals':ref(obj.f_globals),'code':ref(obj.f_code),
                'lasti':obj.f_lasti,'trace':ref(obj.f_trace),'trace_lines':obj.f_trace_lines,
                'trace_opcodes':obj.f_trace_opcodes}]
        if kind is types.FunctionType:
            values=[];empty=[]
            if obj.__closure__:
                for cell in obj.__closure__:
                    try:
                        values.append(cell.cell_contents);empty.append(False)
                    except ValueError:
                        values.append(None);empty.append(True)
            module=obj.__module__ if type(obj.__module__) is str else None
            qual=obj.__qualname__ if type(getattr(obj,'__qualname__',None)) is str else obj.__name__
            return ['function-body',{'module':module,'qualname':qual,'defaults':ref(obj.__defaults__),
                'kwdefaults':ref(obj.__kwdefaults__),'function_dict':ref(obj.__dict__),
                'closure':ref(tuple(values)),'closure_empty':empty,'code':ref(obj.__code__),
                'globals':ref(obj.__globals__),'annotations':ref(obj.__annotations__),
                'builtins':ref(obj.__builtins__),'closure_cells':ref(obj.__closure__)}]
        if kind is types.CellType:
            try:contents=obj.cell_contents;empty=False
            except ValueError:contents=None;empty=True
            return ['closure-cell',{'empty':empty,'contents':ref(contents)}]
        if kind is types.MethodType:
            return ['bound-method',{'self':ref(obj.__self__),'function':ref(obj.__func__)}]
        if kind in (staticmethod,classmethod):
            label='staticmethod' if kind is staticmethod else 'classmethod'
            return [label,{'function':ref(obj.__func__),'attributes':ref(obj.__dict__)}]
        if kind is property:
            return ['property',{'get':ref(obj.fget),'set':ref(obj.fset),'delete':ref(obj.fdel),'doc':ref(obj.__doc__)}]
        if kind is types.ModuleType:
            name=getattr(obj,'__name__',None)
            if type(name) is not str or (name not in _PREFIX_SOURCE_MODULES and name not in ('__main__','actor_bootstrap')):
                return emit_foreign(obj,'foreign-stock-not-owned')
            file=getattr(obj,'__file__',None)
            if type(file) is not str:file=None
            collected=collect(obj.__dict__)
            if collected is None:return emit_unsupported(obj,'module-binding-blocked')
            taken,names=collected
            if names:foreign.append({'module':name,'class':'module','name':name,'reason':'foreign-bindings'})
            pin=None
            if file is not None and type(value) is dict:
                for source_row in value.get('sources') or ():
                    if type(source_row) is dict and source_row.get('path')==file and type(source_row.get('source')) is str and type(source_row.get('sha256')) is str:
                        blob=source_row['source'].encode('utf-8')
                        if hashlib.sha256(blob).hexdigest()==source_row['sha256']:
                            pin={'sha256':source_row['sha256'],'bytes':len(blob)}
                            break
            return ['module-bindings',{'module':name,'file':file,'mutable':ref(taken),'foreign_names':list(names),'preimage':pin}]
        if kind is type:
            module=obj.__module__ if type(obj.__module__) is str else None
            allowed=module in _PREFIX_SOURCE_MODULES or (module in ('__main__','actor_bootstrap') and obj.__name__ in _PREFIX_BOOT_CLASSES)
            if not allowed:return emit_foreign(obj,'foreign-stock-not-owned')
            collected=collect(obj.__dict__)
            if collected is None:return emit_unsupported(obj,'class-binding-blocked')
            taken,names=collected
            if names:foreign.append({'module':module or '','class':obj.__name__,'name':obj.__qualname__,'reason':'foreign-bindings'})
            return ['qualified-class',{'module':module,'qualname':obj.__qualname__,'fields':ref(taken),'foreign_names':list(names)}]
        if kind.__module__ in ('__main__','actor_bootstrap') and kind.__name__ in _PREFIX_BOOT_CLASSES and hasattr(obj,'__dict__'):
            return ['bootstrap-object',{'class':kind.__name__,'state':ref(obj.__dict__)}]
        if kind.__module__ in _PREFIX_SOURCE_MODULES and hasattr(obj,'__dict__'):
            return ['source-object',{'module':kind.__module__,'class':kind.__name__,'state':ref(obj.__dict__)}]
        if kind_of(obj)=='foreign':return emit_foreign(obj,'foreign-stock-not-owned')
        return emit_foreign(obj,'not-selected-stock')
    sealed_sha=None
    bundle_ref=None
    if type(raw) is bytes and 1<=len(raw)<=_PREFIX_WIRE:
        sealed_sha=hashlib.sha256(raw).hexdigest()
        bundle_ref={'encoding':'parent-sealed-memfd','bytes':len(raw),'sha256':sealed_sha}
        plant(raw,['preowned-alias',{'role':'sealed-bundle-bytes','bytes':len(raw),'sha256':sealed_sha,'keys':None}])
        if type(value) is dict and all(type(key) is str for key in value):
            plant(value,['preowned-alias',{'role':'decoded-sealed-bundle','bytes':len(raw),'sha256':sealed_sha,'keys':sorted(value)}])
    if launch is not None:
        encoded=json.dumps(launch,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
        if len(encoded)>_PREFIX_WIRE:raise RuntimeError('prefix-launch-size')
        plant(launch,['preowned-alias',{'role':'pre-exec-launch','bytes':len(encoded),'sha256':hashlib.sha256(encoded).hexdigest(),'keys':None}])
    roots=[ref(error)]
    for obj in (raw,value,launch):
        if obj is not None and id(obj) in seen:roots.append(seen[id(obj)])
    at=0
    while at<len(pending):
        current=pending[at];at+=1
        nodes[seen[id(current)]]=row_for(current)
    if any(row is None for row in nodes):raise RuntimeError('prefix-unfilled')
    for token in token_nodes:
        matches=[]
        for index,item in enumerate(owners):
            if type(item) is dict and item.get('sol070_context_transition') is True and item.get('token') is token:
                matches.append((index,item))
        index=seen[id(token)]
        if len(matches)!=1 or matches[0][1].get('var') is not token.var or type(matches[0][1].get('reset_confirmed')) is not bool:
            unsupported.append({'module':'_contextvars','class':'Token','reason':'actual_transition_missing'})
        else:
            nodes[index][1]['transition']=matches[0][0]
    rows=[{key:item for key,item in row.items() if key not in ('journal','close_cell')}
        for row in getattr(book,'rows',())]
    if book is None or getattr(book,'pre_exec_owner_graph',None) is None or not getattr(book,'binding_complete',False):
        # Selected error bytes are not a complete inherited FD bind.
        # Partial actual object/rows stay held through the error traceback.
        unsupported.append({'module':'actor_bootstrap','class':'StockChildJournal',
            'reason':'actual_prefix_FD_binding_missing'})
    error_graph_complete=not unsupported
    complete=bool(error_graph_complete and not foreign)
    return {'type':'bootstrap-owner-prefix','owner_pid':os.getpid(),'prepaid_token':channel.owner_export_token,
        'schema':'friday.sol090.stock-bootstrap-prefix.v6','history_codec':encode_history_rows(rows),
        # Diagnostic fact of an exact admitted-loader import attempt, not
        # admission authority, native completion or successful import credit.
        'sealed_bundle':bundle_ref,'admitted_source_import':[dict(row) for row in channel.source_import_attempts],
        'error_arena':{'schema':'friday.sol086.early-stock-prefix-arena.v2','physical_body':channel.finish(),'roots':roots,'node_count':len(nodes),
            'chunks':[nodes[i:i+512] for i in range(0,len(nodes),512)],'truncated':False},
        'alias_roles':{'sealed-bundle-bytes':seen.get(id(raw)) if type(raw) is bytes and sealed_sha is not None else None,
            'decoded-sealed-bundle':seen.get(id(value)) if type(value) is dict and sealed_sha is not None and all(type(key) is str for key in value) else None,
            'pre-exec-launch':seen.get(id(launch)) if launch is not None else None,
            'active-error':seen[id(error)]},
        'complete':complete,'error_graph_complete':error_graph_complete,
        'foreign_unwalked':foreign,'unsupported':unsupported,'truncated':False,'ownership_retired':False}

class StockChannel:
    """Reviewed-stock bootstrap framing on the already Root-created pipes.

    No selected Source import is needed here. The parent owns the complete
    bundle/parser/initial transport allocation before fork, and supplies the
    exact live bundle hash token only in its sealed authenticated bundle.
    """
    def __init__(self,outgoing,incoming,owner_export_token=None):
        # Reuse exactly seeded endpoints; preserve partial constructor custody.
        if getattr(self,'preowned_seed',False):
            if (outgoing,incoming,owner_export_token)!=(self.outgoing,self.incoming,self.owner_export_token):
                raise RuntimeError('bootstrap-preowned-channel-drift')
            return
        self.seed_preowned(outgoing,incoming,owner_export_token)
    def seed_preowned(self,outgoing,incoming,owner_export_token):
        self.preowned_seed=True;self.preowned_launch=None
        self.outgoing,self.incoming=outgoing,incoming
        self.owner_export_token=owner_export_token;self.last_raw=None
        self.last_prepaid=False;self.prefix_attempted=False
        self.prefix_errors=[];self.prefix_packet=None;self.prefix_wire=None
        self.body_binding=None;self.body_count=0;self.body_aliases=[];self.body_finished=False
        self.source_import_attempts=[]

    def body_call(self, request):
        """Existing prepaid Root channel, never ordinary reserve after failure."""
        request=dict(request,owner_pid=os.getpid(),prepaid_token=self.owner_export_token)
        raw=json.dumps(request,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')+b'\n'
        if len(raw)>2_000_000:raise RuntimeError('prepared-body-control-capacity')
        for part in (struct.pack('>Q',len(raw)|(1<<63)),raw):
            at=0
            while at<len(part):
                n=os.write(self.outgoing,memoryview(part)[at:])
                if n<=0:raise RuntimeError('prepared-body-control-short')
                at+=n
        reply=self.read()
        if reply.get('ok') is not True:
            exc=RuntimeError('prepared-body-Root-refused')
            exc.actual_reply=reply;exc.actual_reply_raw=self.last_raw
            raise exc
        return reply['value']

    def body_begin(self):
        if self.body_binding is None:
            self.body_binding=self.body_call({'type':'owner-body-begin'})
        return self.body_binding

    def add(self, value):
        if type(value) not in (bytes,bytearray):raise RuntimeError('prepared-body-type')
        binding=self.body_begin();total=self.body_count+len(value)
        wire=2*total+((total+32767)//32768)*512+1024
        # Every actual byte/copy/chunk/RPC/read/hash is nonzero. These limits
        # are the existing prepaid pools, not lower original body/case caps.
        if (4*total>binding['reads'] or 2*total>binding['output']
                or 2*total>binding['hash_bytes'] or 6*total+131072>binding['allocation']
                or wire>binding['wire_reads'] or wire>binding['wire_output']):
            raise RuntimeError('prepared-body-existing-credit-before-materialization')
        snapshot=value if type(value) is bytes else bytes(value)
        offset=self.body_count
        for at in range(0,len(snapshot),32768):
            part=snapshot[at:at+32768]
            reply=self.body_call({'type':'owner-body-chunk','offset':self.body_count,'body':part.hex()})
            self.body_count+=len(part)
            if reply!={'bytes':self.body_count}:raise RuntimeError('prepared-body-Root-count')
        self.body_aliases.append((value,snapshot))
        return {'offset':offset,'bytes':len(snapshot)}

    def finish(self):
        if self.body_finished:raise RuntimeError('prepared-body-finish-once')
        binding=self.body_begin()
        for value,snapshot in self.body_aliases:
            if len(value)!=len(snapshot) or memoryview(value)!=memoryview(snapshot):
                raise RuntimeError('prepared-body-actual-mutable-drift')
        self.body_finished=True
        result=self.body_call({'type':'owner-body-end','bytes':self.body_count})
        if result!={'endpoint':binding['endpoint'],'bytes':self.body_count}:
            raise RuntimeError('prepared-body-Root-descriptor')
        return result

    def read(self):
        def receive(size):
            result=bytearray()
            while len(result)<size:
                part=os.read(self.incoming,min(65536,size-len(result)))
                if not part:raise RuntimeError("bootstrap-control-eof")
                result.extend(part)
            return bytes(result)
        header=struct.unpack('>Q',receive(8))[0]
        self.last_prepaid=bool(header&(1<<63));size=header&((1<<63)-1)
        if self.last_prepaid and self.owner_export_token is None:raise RuntimeError('bootstrap-prepaid-not-owned')
        if size>2_000_000:raise RuntimeError("bootstrap-control-size")
        def pairs(rows):
            result={}
            for key,value in rows:
                if key in result:raise RuntimeError("bootstrap-duplicate-key")
                result[key]=value
            return result
        self.last_raw=receive(size)
        return json.loads(self.last_raw,object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(RuntimeError("bootstrap-numeric")))
    def commit_hash(self,token,size):
        # Only this bounded fixed scalar request exists before Source admit.
        if type(token) is not int or type(size) is not int or size<0:
            raise RuntimeError("bootstrap-credit")
        raw=json.dumps({"type":"commit","token":token,"reads":0,"output":0,"hash_bytes":size},
            sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode("ascii")+b"\n"
        for body in (struct.pack(">Q",len(raw)),raw):
            at=0
            while at<len(body):
                written=os.write(self.outgoing,memoryview(body)[at:])
                if written<=0:raise RuntimeError("bootstrap-control-short")
                at+=written
        response=self.read()
        if response.get('ok') is not True:
            error=RuntimeError('bootstrap-credit-refused')
            error.root_response=response;error.root_response_raw=self.last_raw
            raise error
    def fail_prefix(self,book,error,raw,value,launch):
        if self.prefix_attempted or self.owner_export_token is None:raise RuntimeError('bootstrap-prefix-attempt')
        self.prefix_attempted=True
        self.retained_prefix={'book':book,'error':error,'raw':raw,'value':value,'launch':launch}
        def dump(packet):
            return json.dumps(packet,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')+b'\n'
        wire=None
        try:
            packet=build_early_stock_prefix(self,book,error,raw,value,launch)
            self.prefix_packet=packet
            trial=dump(packet)
            if 1<=len(trial)<=2_000_000:wire=trial
        except (TypeError,ValueError,RuntimeError,OverflowError,RecursionError,MemoryError) as secondary:
            # Keep the actual secondary with its primary/context/TB/body before
            # constructing any diagnostic fallback. This is not native handover.
            self.prefix_errors.append(secondary)
            wire=None
        if wire is None:
            # Incomplete v1 only. A prefix that does not fit is not reported complete.
            tb=error.__traceback__;v1_frames=[]
            while tb is not None:
                v1_frames.append({'filename':tb.tb_frame.f_code.co_filename,'name':tb.tb_frame.f_code.co_name,
                    'line':tb.tb_lineno,'locals_identity':str(id(tb.tb_frame.f_locals))})
                tb=tb.tb_next
            rows=[{k:v for k,v in row.items() if k not in ('journal','close_cell')}
                for row in getattr(book,'rows',())]
            raw_hex=None if type(raw) is not bytes or len(raw)>65536 else raw.hex()
            control_hex=None if self.last_raw is None or len(self.last_raw)>65536 else self.last_raw.hex()
            wire=dump({'type':'bootstrap-owner-prefix','owner_pid':os.getpid(),'prepaid_token':self.owner_export_token,
                'schema':'friday.a190.stock-bootstrap-prefix.v1','rows':rows,'raw_bundle':raw_hex,
                'decoded_bundle_present':value is not None,'pre_exec_launch':None,'root_response_raw':control_hex,
                'error':{'module':type(error).__module__,'class':type(error).__name__,'text':str(error)},
                'traceback_frames':v1_frames,'complete':False,'truncated':False})
        if len(wire)>2_000_000:
            bound=RuntimeError('bootstrap-complete-prefix-wire-bound-open')
            bound.retained_prefix=self.retained_prefix
            raise bound
        self.prefix_wire=wire
        for part in (struct.pack('>Q',len(wire)|(1<<63)),wire):
            at=0
            while at<len(part):
                count=os.write(self.outgoing,memoryview(part)[at:])
                if count<=0:raise RuntimeError('bootstrap-prefix-delivery')
                at+=count
        reply=self.read()
        self.prefix_reply=reply;self.prefix_reply_raw=self.last_raw
        if reply.get('ok') is not True:raise RuntimeError('bootstrap-prefix-receiver-refused')
        return reply['value']
class VerifiedSource(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def __init__(self, rows):
        self.rows={}
        self.channel=None
        if type(rows) is not list or not 1<=len(rows)<=128:raise RuntimeError("source-bundle-count")
        for row in rows:
            if set(row)!={"name","path","sha256","source"} or row["name"] in self.rows:
                raise RuntimeError("source-bundle-schema")
            raw=row["source"].encode("utf-8")
            if len(raw)>2_000_000 or type(row["sha256"]) is not str or len(row["sha256"])!=64:
                raise RuntimeError("source-bundle-sha")
            self.rows[row["name"]]=(row["path"],raw,row["sha256"])
        self._pending=True
    def admit(self, channel,token):
        if channel is None or not self._pending:
            raise RuntimeError("source-bundle-sha")
        for name,(path,raw,expected) in self.rows.items():
            channel.commit_hash(token,len(raw))
            if hashlib.sha256(raw).hexdigest()!=expected:
                raise RuntimeError("source-bundle-sha")
            self.rows[name]=(path,raw)
        self._pending=False
        self.channel=channel
    def find_spec(self,name,path=None,target=None):
        if name in self.rows:return importlib.util.spec_from_loader(name,self,origin=self.rows[name][0])
        return None
    def create_module(self,spec):return None
    def exec_module(self,module):
        if self._pending:raise RuntimeError("source-bundle-sha")
        path,raw=self.rows[module.__name__]
        module.__file__=path
        # Exact verified bytes and the existing channel are already admitted.
        # Preserve even a compile/import error prefix as an import attempt;
        # do not mislabel later Source constructor failures as pre-import.
        attempt={'name':module.__name__,'state':'STARTED'}
        self.channel.source_import_attempts.append(attempt)
        try:exec(compile(raw,path,"exec",dont_inherit=True),module.__dict__)
        except BaseException as error:
            attempt['state']='ERROR';self.channel.prefix_errors.append(error);raise
        else:attempt['state']='COMPLETE'


def main():
    child_book=stock=None
    raw=value=launch=loader=meter=consumer=channel=None
    try:
        if len(sys.argv)!=5:raise RuntimeError("bootstrap-argv")
        bundle_fd,request_fd,response_fd,export_token=map(int,sys.argv[1:])
        # Existing Root-selected endpoints/token must be known first. Parse or
        # StockChannel construction failure still needs the existing native
        # prefix supplier; never guess an FD/token or make a late channel.
        stock=StockChannel.__new__(StockChannel)
        stock.seed_preowned(request_fd,response_fd,export_token)
        stock.__init__(request_fd,response_fd,export_token)
        launch=stock.read()['launch_fact'];stock.preowned_launch=launch
        # Existing channel precedes journal construction and keeps its partial
        # actual object through constructor failure.
        child_book=StockChildJournal.__new__(StockChildJournal)
        child_book.seed_preowned(launch['bootstrap_child_ownership'],os.getpid(),
            (bundle_fd,request_fd,response_fd))
        child_book.__init__((bundle_fd,request_fd,response_fd))
        child_book.bind(launch['bootstrap_child_ownership'],os.getpid())
        return _perform_bootstrap(bundle_fd,request_fd,response_fd,export_token,child_book,stock)
    except BaseException as primary:
        # The broad stock boundary includes bind/admit/import/connect/configure
        # and constructor failure, not just the ordinary consumer body.
        frame=primary.__traceback__
        while frame is not None:
            if frame.tb_frame.f_code.co_name=='_perform_bootstrap':
                held=frame.tb_frame.f_locals
                raw=held.get('raw');value=held.get('value');launch=held.get('launch')
                meter=held.get('meter')
                break
            frame=frame.tb_next
        # A partial MeterRPC constructor cannot hide the original with a
        # secondary AttributeError from an uninitialized completion field.
        if (stock is not None and hasattr(stock,'prefix_errors')
                and hasattr(stock,'body_finished')
                and not getattr(meter,'owner_export_attempted',False)):
            try:stock.fail_prefix(child_book,primary,raw,value,launch)
            except BaseException as export_error:
                stock.prefix_errors.append(export_error)
                primary.owner_export_error=export_error
        raise

def _perform_bootstrap(bundle_fd,request_fd,response_fd,export_token,child_book,stock):
    import fcntl
    seals=fcntl.fcntl(bundle_fd,fcntl.F_GET_SEALS)
    required=fcntl.F_SEAL_WRITE|fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL
    if seals&required!=required:raise RuntimeError("bundle-not-sealed")
    size=os.fstat(bundle_fd).st_size
    if not 1<=size<=2_000_000:raise RuntimeError("bundle-size")
    raw=os.pread(bundle_fd,size,0)
    if len(raw)!=size:raise RuntimeError("bundle-short")
    # The parent authenticated admission and verified all source bytes before
    # creating this sealed memfd. A caller fd/label is never Root provenance.
    value=json.loads(raw)
    if set(value)!={"sources","admission","ordinary","parent_pid","consumer_files","bootstrap_hash_token","owner_export_token"}:
        raise RuntimeError("bundle-fields")
    if value["parent_pid"]!=os.getppid():raise RuntimeError("bootstrap-parent")
    preimage_pins=[{"path":row["path"],"sha256":row["sha256"],"bytes":len(row["source"].encode("utf-8"))} for row in value["sources"]]
    preimage_pins.extend(value["consumer_files"])
    loader=VerifiedSource(value["sources"])
    launch=stock.preowned_launch
    if launch is None or not child_book.binding_complete:
        raise RuntimeError('bootstrap-actual-preowned-launch-binding')
    loader.admit(stock,value["bootstrap_hash_token"])
    sys.meta_path.insert(0,loader)
    # VerifiedSource admission/importer precedes the first selected import.
    from observer import bind_selected_preimages
    bind_selected_preimages(preimage_pins)
    from launcher import actor_main, ControlChannel
    from observer import MeterRPC
    from consumer_bridge import HeldConsumerLoader
    # One Root channel owns preparation/import admission as well as execution.
    if value['owner_export_token']!=export_token:raise RuntimeError('bootstrap-export-token-drift')
    channel=ControlChannel(request_fd,response_fd,child_book,export_token)
    meter=MeterRPC.__new__(MeterRPC)
    meter.__init__(channel.call,value['owner_export_token'],channel.terminal_call)
    meter.owner_body_carrier=stock
    consumer=None
    try:
        child_book.connect(meter)
        channel.configure(meter)
        consumer=HeldConsumerLoader(value["consumer_files"],meter)
        consumer.acquire().enter()
        actor_main(request_fd,response_fd,value["admission"],launch,value["ordinary"],consumer.module,
                   initial_launch=launch,existing_channel=channel,existing_meter=meter)
    finally:
        primary=sys.exc_info()[1]
        if primary is not None:meter.retain_error_arena(primary)
        cleanup_error=None
        try:
            if consumer is not None:consumer.close()
        except BaseException as exc:
            cleanup_error=exc;meter.retain_error_arena(exc)
        child_book.close_one(bundle_fd)
        # This is the sole final attempt, after all ordinary/bootstrap close
        # facts. The final two transport rows deliberately remain HELD in the
        # complete accepted graph until Root confirms process/table retirement.
        # No later local close can create unexported UNKNOWN histories.
        try:meter.publish_local_owners(terminal=True)
        except BaseException as exc:
            meter.retain_error_arena(exc)
            if primary is None and cleanup_error is None:raise
            if primary is not None:primary.owner_export_error=exc
            else:cleanup_error.owner_export_error=exc
        if primary is None and cleanup_error is not None:raise cleanup_error


if __name__=="__main__":
    main()
