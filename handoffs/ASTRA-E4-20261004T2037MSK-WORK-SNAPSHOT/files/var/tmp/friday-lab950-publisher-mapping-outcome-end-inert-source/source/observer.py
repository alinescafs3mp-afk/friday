"""Root-owned prospective budget and real kernel observations.

The supervisor, never the performer, owns this object. Reservations are not
observations. Absolute inherited peaks are retained without subtraction.
"""
import os
import sys
import contextlib
import contextvars
import resource
import stat
import time
import types
import struct
from common import (Refused, READ_MAX, OUTPUT_MAX, RAM_MAX, SLOTS_MAX, DOCUMENT_MAX,
                    WORKERS_MAX, WALL_MAX, REFUSAL_RESERVE, mono, integer,
                    canonical, parse, error_fact)
from custody import open_absolute, identity9
from lifetime import OwnedFDs, FDState

_SOURCE_OBJECT_MODULES=frozenset(('actor_context','admission','capacity','alljob_cohort','class_semantics','common',
    'consumer_bridge','custody','extraction','fact_bridge','independent_selector','launcher','lifetime',
    'native','normalization','observer','operations','retention','roles','root_tool_adapter','snapshot_producer',
    'actor_bootstrap','authority','bill','body_scope','canonical','capability','contract','declared_controls',
    'document_vector','document_windows','effects','fixtures','formats','ingress','material_literals',
    'performing_contracts','pins','producer_consumer','receipt_chain','recipe_planner','resource_meter',
    'runtime_consumer','schema_validate','semantics','whole_join','existing_root_caller','owned_prefix_bank','selected_owned_values','master_pool'))

from selected_owned_values import (own_context_var, own_context_set,
    preown_secondary_selected, secondary_own_records, secondary_frame_locals,
    consume_secondary_own_cut)
_PREIMAGE_PINS = own_context_var("selected_preimage_pins", default=None)

def bind_selected_preimages(pins):
    """Fail closed unless each enrolled path has one complete byte/sha pin."""
    if type(pins) not in (list, tuple) or not pins:
        raise Refused("selected_preimage_absent", "terminal")
    rows = []
    seen = set()
    for pin in pins:
        if type(pin) is not dict:
            raise Refused("selected_preimage_pin", "terminal")
        path = pin.get("path")
        digest = pin.get("sha256")
        size = pin.get("bytes")
        if type(path) is not str or path in seen or type(digest) is not str or len(digest) != 64 or type(size) is not int or size < 0:
            raise Refused("selected_preimage_ambiguous", "terminal")
        seen.add(path)
        rows.append({"path": path, "sha256": digest, "bytes": size})
    own_context_set(_PREIMAGE_PINS,tuple(rows))

def _preimage_pin(path):
    pins = _PREIMAGE_PINS.get()
    if not pins or type(path) is not str:
        return None
    found = [pin for pin in pins if pin.get("path") == path]
    if len(found) != 1:
        return None
    return found[0]

def _classify_binding(item):
    """take selected mutable state, skip foreign/import heaps, block the rest."""
    if len(getattr(item, "__name__", "") or "") > 2:
        pass
    if item is None or type(item) is bool or type(item) is int or (type(item) is str and len(item) <= 4096):
        return "take"
    if type(item) in (list, tuple, dict, bytes, bytearray, set, frozenset, float,
                      range, slice, contextvars.ContextVar, contextvars.Context,
                      contextvars.Token, types.CodeType, property):
        return "take"
    if type(item) in (types.BuiltinFunctionType, types.BuiltinMethodType):
        return "skip"
    if type(item) is types.ModuleType:
        return "take" if getattr(item, "__name__", None) in _SOURCE_OBJECT_MODULES else "skip"
    if type(item) is types.FunctionType or type(item) is types.MethodType or type(item) is type:
        return "take" if getattr(item, "__module__", None) in _SOURCE_OBJECT_MODULES else "skip"
    func = item.__func__ if type(item) in (staticmethod, classmethod) else None
    if type(func) is types.FunctionType:
        return "take" if getattr(func, "__module__", None) in _SOURCE_OBJECT_MODULES else "skip"
    if isinstance(item, BaseException) or type(item).__module__ in _SOURCE_OBJECT_MODULES:
        return "take"
    return "block"

def _selected_mutable_bindings(module):
    selected = {}
    for attr, item in module.__dict__.items():
        if len(attr) > 2 and attr.startswith("__") and attr.endswith("__"):
            continue
        kind = _classify_binding(item)
        if kind == "skip":
            continue
        if kind == "block":
            return None
        selected[attr] = item
    return selected

def _actual_fd_collection(value):
    """Recognize actual row dictionaries, not a copied four-field shape."""
    kind=type(value)
    if kind is list:
        items=value
    elif kind is dict:
        if not value or any(type(key) not in (str,int) for key in value):return None
        items=value.values()
    else:return None
    if not value or any(type(item) is not dict for item in items):return None
    from lifetime import live_fd_states
    found=None
    for state in live_fd_states():
        if all(id(item) in state.row_ids for item in items):
            if found is not None:return None
            found=state
    if found is None:return None
    return ({"collection_identity":str(id(value)),"kind":"list" if kind is list else "dict",
        "owner_pid":os.getpid(),"generation":found.generation,"domain_identity":str(id(found)),
        "journal_row_list":any(value is book.rows for book in found.journals),
        "whole_domain":False,"row_count":len(value)},found)

def _fd_root_chunks(items,ref):
    # Chunk the one indispensable actual body/key relation as it is built.
    # No whole-history members, numeric order, keys or flat root copy.
    chunks=[];part=[]
    for item in items:
        part.append(ref(item))
        if len(part)==512:chunks.append(part);part=[]
    if part:chunks.append(part)
    if len(chunks)>512:raise Refused('full_value_fd_reference_capacity','terminal')
    return chunks

def full_value_arena(roots,history_states=(),body_carrier=None):
    """Actual selected stock values, preserving repeated identities.

    Unsupported stock/Source objects remain explicit incompleteness. Their
    identity is diagnostic and can never cause a complete receiver receipt.
    No arbitrary interpreter heap is presumed enrolled by this encoder.
    Native type layout/private state and aggregate costs are NOT qualified by
    these public-field Source nodes. Both performing receivers still require
    their actual enrollment, retained body and finite end relations.
    """
    if body_carrier is None:raise Refused('actual_prepared_full_body_carrier_required','terminal')
    retain=getattr(body_carrier,'retain_cohort_before_effect',None)
    if retain is not None:retain()
    nodes=[];seen={};unsupported=[];owners=[];pending=[];token_nodes=[]
    edge_routes=[];active_parent=-1;support_nodes={};skip_expand=set()
    history_domains={}
    def enroll_history(state):
        identity=str(id(state))
        prior=history_domains.get(identity)
        if prior is None:
            history_domains[identity]=(state,state.graph())
        elif prior[0] is not state:raise Refused('full_value_history_identity_reuse','terminal')
    for state in history_states:enroll_history(state)
    def ref(value,data=True,support=None):
        key=id(value)
        if key in seen:
            index=seen[key]
            if owners[index] is not value:raise Refused('full_value_identity_reuse','terminal')
        else:
            index=len(nodes)
            if index>=262144:raise Refused('full_value_node_capacity','terminal')
            seen[key]=index;nodes.append(None);owners.append(value);pending.append((index,value))
        if len(edge_routes)>=1048576:raise Refused('full_value_edge_capacity','terminal')
        edge_routes.append([active_parent,index,data])
        if not data:
            if type(support) is not int or support<1:raise Refused('actual_runtime_support_producer','terminal')
            support_nodes.setdefault(index,support)
        return index
    native_cuts={}
    from selected_owned_values import (own_mapping,public_stock_cut,own_context_body,
        own_binding,own_hash_body,own_function_body,own_runtime_support,own_class_body,
        preown_secondary_selected, secondary_own_records, secondary_frame_locals,
        consume_secondary_own_cut)
    selected=[ref(value) for value in roots]
    preown_secondary_selected(tuple(roots),_SOURCE_OBJECT_MODULES,body_carrier)
    body_carrier.secondary_own_cuts=secondary_own_records()
    consume_secondary_own_cut(body_carrier.secondary_own_cuts,body_carrier)
    at=0
    while at<len(pending):
        index,value=pending[at];at+=1;key=id(value);active_parent=index
        t=type(value)
        selected_mapping=own_mapping(value)
        selected_public=public_stock_cut(value,_preimage_pin)
        selected_hash=own_hash_body(value)
        retired_reader=getattr(body_carrier,'retired_mapping_state',None)
        retired=None if retired_reader is None else retired_reader(value)
        if index in support_nodes:
            row=['runtime-support',{'producer_serial':support_nodes[index]}]
        elif index in skip_expand:
            row=['unsupported',str(key)]
        elif retired is not None:
            body_carrier.retain_retired_mapping_node(index,retired)
            row=['selected-retired-mmap',{'state':ref(retired),
                'full_cut_ledgers':ref(retired['full_cut_ledgers']),
                'actual_row':ref(retired['actual_row']),'actual_credit':ref(retired['actual_credit']),
                'actual_close_record':ref(retired['actual_close_record'])}]
        elif selected_mapping is not None and selected_mapping.close_attempted and not selected_mapping.closed_confirmed:
            unsupported.append({'identity':str(key),'module':'mmap','class':'mmap',
                'reason':'mapping_close_outcome_unknown'})
            row=['unsupported','mapping_close_outcome_unknown']
        elif (selected_mapping is not None and selected_mapping.creation.get('attempted') is True
                and selected_mapping.creation.get('returned') is not True
                and selected_mapping.creation.get('original_error') is None
                and selected_mapping.creation.get('metadata_error') is None):
            unsupported.append({'identity':str(key),'module':'mmap','class':'mmap',
                'reason':'mapping_return_recovery_unconfirmed'})
            row=['unsupported','mapping_return_recovery_unconfirmed']
        elif selected_mapping is not None:
            cut=selected_mapping.cut(body_carrier)
            row=['selected-owned-mmap',{'state':ref(cut),'full_bytes':ref(cut['full_bytes']),
                'actual_row':ref(selected_mapping.row),'actual_credit':ref(selected_mapping.credit)}]
        elif selected_hash is not None:
            row=['selected-own-hash',{'state':ref(selected_hash)}]
            # Complete OWN constructor/operation/error prefix is distinct from
            # computational state_complete and from foreign runtime heap/end.
            # The typed reader preserves uncertainty; no later success repairs
            # an earlier unknown actual update and no end is asserted here.
        elif selected_public is not None:
            # PUBLIC body now flows through BOTH readers. Missing complete
            # native state remains explicit; never turn an interface into full
            # stock/body acceptance by relabeling the original requirement.
            row=['selected-public-stock',{'state':ref(selected_public)}]
            unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__,
                'reason':'selected_public_native_state_not_complete_CODE'})
        elif t.__name__=='PyCapsule':
            from existing_root_caller import _native
            module=_native()
            if module.is_context(value):
                cut=native_cuts.get(key)
                if cut is None:cut=module.owned_state(value);native_cuts[key]=cut
                row=['selected-native-context',{'state':ref(cut)}]
            else:
                unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__})
                row=['unsupported',str(key)]
        elif t is types.ModuleType and getattr(value,'__name__',None)=='publisher_owned_custody':
            from existing_root_caller import _native
            module=_native()
            if module.is_module(value):
                row=['selected-native-module',{'module':'publisher_owned_custody','context':ref(module.current())}]
            else:
                unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__})
                row=['unsupported',str(key)]
        elif value is None:row=['none',None]
        elif t is bool:row=['bool',value]
        elif t is int:row=['int',str(value)]
        elif t is float:row=['float64',struct.pack('>d',value).hex()]
        elif t is str:row=['str',value]
        elif t in (bytes,bytearray):
            row=['bytes' if t is bytes else 'bytearray',body_carrier.add(value)]
        elif t in (list,tuple):
            found=_actual_fd_collection(value) if t is list else None
            if found is not None:
                body,state=found;enroll_history(state)
                body['body_chunks']=_fd_root_chunks(value,ref)
                body['key_chunks']=None
                row=['fd-collection',body]
            else:row=['list' if t is list else 'tuple',[ref(v) for v in tuple(value)]]
        elif t is dict:
            found=_actual_fd_collection(value)
            if found is not None:
                body,state=found;enroll_history(state)
                body['body_chunks']=_fd_root_chunks(value.values(),ref)
                body['key_chunks']=_fd_root_chunks(value.keys(),ref)
                row=['fd-collection',body]
            else:
                binding=own_binding(value)
                pairs=[]
                for k,v in tuple(value.items()):
                    support=own_runtime_support(v,value,k,1) if binding is not None else None
                    pairs.append([ref(k),ref(v,data=support is None,support=support)])
                row=['dict',pairs]
        elif t in (set,frozenset):
            row=['set' if t is set else 'frozenset',[ref(v) for v in value]]
        elif t is range:row=['range',[ref(value.start),ref(value.stop),ref(value.step)]]
        elif t is slice:row=['slice',[ref(value.start),ref(value.stop),ref(value.step)]]
        elif value is contextvars.Token.MISSING:row=['token-missing',None]
        elif value is Ellipsis:row=['ellipsis',None]
        elif t is contextvars.ContextVar:
            body=own_context_body(value)
            if body is None:
                unsupported.append({'identity':str(key),'module':'_contextvars','class':'ContextVar',
                    'reason':'actual_constructor_default_binding_missing'})
                row=['unsupported',str(key)]
            else:
                row=['context-var',{'name':body['name'],'has_default':body['has_default'],
                    'default':ref(body['default']),'present':body['present'],'current':ref(body['current'])}]
        elif t is contextvars.Context:
            row=['context',[[ref(var),ref(item)] for var,item in value.items()]]
        elif t is contextvars.Token:
            body=own_context_body(value)
            if body is None:
                unsupported.append({'identity':str(key),'module':'_contextvars','class':'Token',
                    'reason':'actual_set_reset_context_body_missing'})
                row=['unsupported',str(key)]
            else:
                row=['context-token',{'var':ref(body['var']),'old_value':ref(body['old_value']),
                    'transition':ref(body['transition']),'actual_context':ref(body['actual_context']),
                    'used':body['used']}]
        elif t is types.CodeType:
            names=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals',
                'co_stacksize','co_flags','co_code','co_consts','co_names','co_varnames',
                'co_filename','co_name','co_qualname','co_firstlineno','co_linetable',
                'co_exceptiontable','co_freevars','co_cellvars')
            if not all(hasattr(value,name) for name in names):
                unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__})
                row=['unsupported',str(key)]
            else:row=['code-body',{name:ref(getattr(value,name)) for name in names}]
        elif isinstance(value,BaseException):
            row=['error',{'module':t.__module__,'class':t.__name__,'args':ref(value.args),
                'attributes':ref(value.__dict__),'cause':ref(value.__cause__),
                'context':ref(value.__context__),'suppress_context':value.__suppress_context__,
                'traceback':ref(value.__traceback__)}]
        elif t is types.TracebackType:
            row=['traceback',{'next':ref(value.tb_next),'frame':ref(value.tb_frame),
                'line':value.tb_lineno,'lasti':value.tb_lasti}]
        elif t is types.FrameType:
            # Selected mutable globals are represented. Foreign import and
            # interpreter heaps are not walked. An ordinary unrepresentable
            # selected value stays CODE incompleteness, not a future-image gap.
            # Actual dictionary identity joins functions/frame/global aliases.
            # Foreign stock values remain explicit unsupported nodes; this
            # does not recursively inspect their private interpreter heap.
            globals_obj=value.f_globals
            binding=own_binding(globals_obj)
            if binding is not None:
                globals_ref=ref(globals_obj)
            else:
                support=own_runtime_support(globals_obj,value,value.f_code,3)
                if support is not None:
                    globals_ref=ref(globals_obj,data=False,support=support)
                else:
                    first=id(globals_obj) not in seen
                    globals_ref=ref(globals_obj)
                    if first:
                        skip_expand.add(globals_ref)
                        unsupported.append({'identity':str(id(globals_obj)),'module':t.__module__,
                            'class':'frame-globals','reason':'frame_globals_namespace_relation_unproven'})
            row=['frame',{'filename':value.f_code.co_filename,'name':value.f_code.co_name,
                'line':value.f_lineno,'locals':ref(secondary_frame_locals(value,body_carrier)),'globals':globals_ref,
                'code':ref(value.f_code),'lasti':value.f_lasti,
                'trace':ref(value.f_trace),'trace_lines':value.f_trace_lines,
                'trace_opcodes':value.f_trace_opcodes}]
        elif t.__module__ in _SOURCE_OBJECT_MODULES and hasattr(value,'__dict__'):
            binding=own_binding(t)
            if binding is None:
                unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__,
                    'reason':'actual_type_compiled_full_body_binding_missing'})
                row=['unsupported',str(key)]
            else:
                row=['Source-object',{'module':t.__module__,'class':t.__name__,
                    'state':ref(value.__dict__),'binding':ref(binding)}]
        elif t.__name__=='StockChildJournal' and t.__module__=='__main__' and hasattr(value,'__dict__'):
            unsupported.append({'identity':str(key),'module':'__main__','class':'StockChildJournal',
                'reason':'actual_child_compiled_full_body_binding_missing'})
            row=['unsupported',str(key)]
        elif t is types.MethodType:
            row=['bound-method',{'self':ref(value.__self__),'function':ref(value.__func__)}]
        elif t in (staticmethod,classmethod):
            row=['staticmethod' if t is staticmethod else 'classmethod',
                {'function':ref(value.__func__),'attributes':ref(value.__dict__)}]
        elif t is property:
            row=['property',{'get':ref(value.fget),'set':ref(value.fset),
                'delete':ref(value.fdel),'doc':ref(value.__doc__)}]
        elif t is memoryview:
            row=['memoryview',{'object':ref(value.obj),'full_bytes':ref(value.tobytes()),
                'format':value.format,'shape':ref(value.shape),'strides':ref(value.strides),
                'readonly':value.readonly}]
        elif t is types.FunctionType:
            code=value.__code__;binding=own_binding(value.__globals__);native_fields=own_function_body(value)
            if binding is None or native_fields is None or binding[1] is not value.__globals__:
                unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__,
                    'reason':'actual_function_compiled_body_binding_missing'})
                row=['unsupported',str(key)]
            else:
                mod=binding[0];pin=binding[3];support=own_runtime_support(native_fields['builtins'],value,None,2)
                row=['qualified-function',{'module':mod.__name__,'qualname':value.__qualname__,
                    'filename':pin['path'],'firstlineno':code.co_firstlineno,'defaults':ref(value.__defaults__),
                    'kwdefaults':ref(value.__kwdefaults__),'function_dict':ref(value.__dict__),
                    'closure':ref(value.__closure__),'globals':ref(value.__globals__),'module_body':ref(mod),
                    'annotations':ref(native_fields['annotations']),'annotate':ref(native_fields['annotate']),
                    'annotations_present':native_fields['annotations_present'],
                    'annotate_present':native_fields['annotate_present'],
                    'builtins':ref(native_fields['builtins'],data=support is None,support=support),
                    'code':ref(code),'binding':ref(binding),
                    'preimage_sha256':pin['sha256'],'preimage_bytes':pin['bytes']}]
        elif t is types.CellType:
            try:contents=value.cell_contents;empty=False
            except ValueError:contents=None;empty=True
            row=['closure-cell',{'empty':empty,'contents':ref(contents)}]
        elif t is types.ModuleType:
            binding=own_binding(value)
            if binding is not None and binding[0] is value:
                pin=binding[3]
                row=['qualified-module',{'module':value.__name__,'file':pin['path'],
                    'preimage_sha256':pin['sha256'],'preimage_bytes':pin['bytes'],
                    'mutable':ref(binding[1]),'binding':ref(binding)}]
            else:
                unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__,
                    'reason':'actual_module_compiled_full_body_binding_missing'})
                row=['unsupported',str(key)]
        elif isinstance(value,type):
            binding=own_binding(value);fields=own_class_body(value)
            if binding is None or fields is None:
                unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__,
                    'reason':'actual_class_compiled_body_binding_missing'})
                row=['unsupported',str(key)]
            else:
                row=['qualified-class',{'module':binding[0].__name__,'qualname':value.__qualname__,
                    'fields':ref(fields),'binding':ref(binding)}]
        elif t is types.BuiltinFunctionType or t is types.BuiltinMethodType:
            unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__})
            row=['unsupported',str(key)]
        else:
            unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__})
            row=['unsupported',str(key)]
        nodes[index]=row
    # Native original set/reset registry supplied exact token/context/used
    # body above; no name, copied context or posthoc search creates its binding.
    domains=[graph for state,graph in history_domains.values()]
    from common import _history_chunks
    physical_body=body_carrier.finish()
    required=set(selected);edges={}
    for parent,child,data in edge_routes:
        if parent>=0 and data:edges.setdefault(parent,[]).append(child)
    todo=list(required)
    for parent in todo:
        for child in edges.get(parent,()):
            if child not in required:required.add(child);todo.append(child)
    for index in support_nodes:
        if index in required:
            unsupported.append({'node':index,'reason':'runtime_support_reached_as_required_own_data'})
    return {'schema':'friday.sol105.stock-full-value-arena.v9','roots':selected,
        'physical_body':physical_body,
        'node_count':len(nodes),'chunks':[nodes[i:i+512] for i in range(0,len(nodes),512)],
        'edge_routes':{'count':len(edge_routes),'chunks':[edge_routes[i:i+512] for i in range(0,len(edge_routes),512)]},
        'history_domains':{'count':len(domains),'chunks':_history_chunks(domains)},
        'unsupported':unsupported,'complete':not unsupported,'truncated':False}


def fd_collection_roots(body,name):
    chunks=body.get(name);count=body.get('row_count')
    if type(count) is not int or not 0<=count<=512*512 or type(chunks) is not list or len(chunks)>512:
        raise Refused('full_value_fd_chunks','terminal')
    seen=0
    for part in chunks:
        if type(part) is not list or not 1<=len(part)<=512:raise Refused('full_value_fd_chunks','terminal')
        for root in part:
            if type(root) is not int:raise Refused('full_value_fd_reference','terminal')
            seen+=1;yield root
    if seen!=count:raise Refused('full_value_fd_count','terminal')

def fd_value_fields(nodes,root):
    if type(root) is not int or not 0<=root<len(nodes) or nodes[root][0]!='dict':
        raise Refused('full_value_fd_bodies','terminal')
    fields={}
    for key,value in nodes[root][1]:
        if nodes[key][0]!='str' or nodes[key][1] in fields:
            raise Refused('full_value_fd_body_duplicate','terminal')
        fields[nodes[key][1]]=value
    return fields

def full_value_literal(nodes,root,active=()):
    # Only actual JSON-valued reference data, never guessed expected operands.
    if root in active or len(active)>=24:raise Refused('full_value_literal_cycle','terminal')
    kind,body=nodes[root]
    if kind=='none':return None
    if kind=='bool' or kind=='str':return body
    if kind=='int':return int(body)
    if kind=='list':return [full_value_literal(nodes,i,active+(root,)) for i in body]
    if kind=='dict':
        result={}
        for k,v in body:
            key=full_value_literal(nodes,k,active+(root,))
            if type(key) is not str or key in result:raise Refused('full_value_literal_key','terminal')
            result[key]=full_value_literal(nodes,v,active+(root,))
        return result
    raise Refused('full_value_literal_type','terminal')

def full_value_matches(nodes,root,expected,active=()):
    if root in active or len(active)>=24:raise Refused('full_value_fd_row_cycle','terminal')
    kind,body=nodes[root];t=type(expected)
    if expected is None:return kind=='none' and body is None
    if t is bool:return kind=='bool' and body is expected
    if t is int:return kind=='int' and body==str(expected)
    if t is str:return kind=='str' and body==expected
    if t is list:
        return (kind=='list' and len(body)==len(expected)
            and all(full_value_matches(nodes,i,v,active+(root,)) for i,v in zip(body,expected)))
    if t is dict:
        if kind!='dict':return False
        fields=fd_value_fields(nodes,root)
        return (set(fields)==set(expected)
            and all(full_value_matches(nodes,fields[k],v,active+(root,)) for k,v in expected.items()))
    raise Refused('full_value_fd_row_type','terminal')

def full_value_history_domains(arena):
    from common import exact,history_counted_chunks
    bank=exact(arena['history_domains'],('count','chunks'),'full_value_history_bank')
    return history_counted_chunks(bank['chunks'],bank['count'],'full_value_history_bank')

def full_value_history_domain(arena,reference):
    from common import exact
    exact(reference,('domain_identity',),'full_value_history_reference')
    found=[d for d in full_value_history_domains(arena) if d['domain_identity']==reference['domain_identity']]
    if len(found)!=1:raise Refused('full_value_history_reference','terminal')
    return found[0]

def validate_fd_value_histories(nodes,domains):
    from common import history_domain_books,history_book_rows
    primary={};identities=set()
    def row_key(row):
        # Absence and None remain distinct even in the membership key.
        return tuple((name in row,row.get(name)) for name in ('credit','slot','generation'))
    def journal_collection(fields):
        # The actual row keeps its current journal backedge after transfer.
        # Bind that Source object's real rows node, not an owner-name marker.
        journal=fields.get('journal')
        if journal is None or nodes[journal][0]!='Source-object':
            raise Refused('full_value_fd_journal_alias','terminal')
        body=nodes[journal][1]
        if (body['module'],body['class']) not in (
                ('lifetime','OwnedFDs'),('lifetime','ForkOwner'),
                ('actor_bootstrap','StockChildJournal')):
            raise Refused('full_value_fd_journal_alias','terminal')
        state=fd_value_fields(nodes,body['state'])
        collection=state.get('rows')
        if collection is None or nodes[collection][0]!='fd-collection':
            raise Refused('full_value_fd_journal_rows','terminal')
        return nodes[collection][1]
    for domain in domains:
        identity=domain['domain_identity']
        if identity in identities:raise Refused('full_value_history_domain_duplicate','terminal')
        identities.add(identity);rows={}
        books=history_domain_books(domain)
        for book in books:
            collection=book['collection']['collection_identity']
            for ordinal,row in enumerate(history_book_rows(book)):
                key=row_key(row)
                if key in rows:raise Refused('full_value_history_row_duplicate','terminal')
                # Local validation index only; no redundant wire chronology.
                rows[key]=(row,collection,ordinal)
        if len(rows)!=domain['history_count']:raise Refused('full_value_history_count','terminal')
        primary[identity]=(domain,rows,books)
    collections=set()
    for kind,body in nodes:
        if kind!='fd-collection':continue
        ident=body['collection_identity']
        if ident in collections:raise Refused('full_value_fd_collection_identity','terminal')
        collections.add(ident)
        selected=primary.get(body['domain_identity'])
        if selected is None or selected[0]['generation']!=body['generation']:
            raise Refused('full_value_fd_primary','terminal')
        books=selected[2]
        if any(b['collection']['owner_pid']!=body['owner_pid'] for b in books):
            raise Refused('full_value_fd_owner','terminal')
        journal_book=None;owned_count=0
        if body['journal_row_list']:
            journal_book=next((b for b in books
                if b['collection']['collection_identity']==ident),None)
            if body['kind']!='list' or journal_book is None:
                raise Refused('full_value_fd_journal_primary','terminal')
        for root in fd_collection_roots(body,'body_chunks'):
            fields=fd_value_fields(nodes,root)
            actual={name:full_value_literal(nodes,fields[name]) for name in ('credit','slot','generation') if name in fields}
            entry=selected[1].get(row_key(actual))
            row=None if entry is None else entry[0]
            ordinary={name:index for name,index in fields.items() if name not in ('journal','close_cell')}
            if (row is None or set(ordinary)!=set(row)
                    or any(not full_value_matches(nodes,ordinary[name],value) for name,value in row.items())):
                raise Refused('full_value_fd_primary_body','terminal')
            current=journal_collection(fields)
            if (not current['journal_row_list'] or current['kind']!='list'
                    or current['collection_identity']!=entry[1]
                    or current['domain_identity']!=body['domain_identity']
                    or current['generation']!=body['generation']
                    or current['owner_pid']!=body['owner_pid']):
                raise Refused('full_value_fd_primary_alias','terminal')
            if journal_book is not None and entry[1]==ident:
                if entry[2]!=owned_count:
                    raise Refused('full_value_fd_journal_order','terminal')
                owned_count+=1
        # OwnedFDs.transfer retains the original list reference to the same row.
        # Its current-owner projection is filtered; those two counts may differ.
        # Bind the complete ordered OWNED projection without discarding the
        # foreign row bodies or forcing general subsets to be whole journals.
        if journal_book is not None and owned_count!=journal_book['journal_count']:
            raise Refused('full_value_fd_journal_count','terminal')
    return primary


def validate_retired_mapping_node(nodes,node_index,body):
    # This is the encoded half of the same carrier's retained-object check.
    # It validates full historical bytes and the actual close relation; it
    # never requires the closed mapping or its backing FD to be readable.
    state=fd_value_fields(nodes,body['state'])
    names={'schema','actual_mapping','original_birth','full_cut_ledgers',
        'full_transfer_snapshot','actual_row','actual_credit','actual_close_record'}
    if (set(state)!=names
            or nodes[state['schema']]!=['str','friday.sol095.owned-retired-mmap-full-state.v1']
            or state['actual_mapping']!=node_index):
        raise Refused('selected_retired_mmap_state','terminal')
    for name in ('full_cut_ledgers','actual_row','actual_credit','actual_close_record'):
        if state[name]!=body[name]:raise Refused('selected_retired_mmap_state_alias','terminal')
    birth_kind,birth_body=nodes[state['original_birth']]
    if (birth_kind!='Source-object' or birth_body['module']!='selected_owned_values'
            or birth_body['class']!='OwnedMappingBirth'):
        raise Refused('selected_retired_mmap_original_birth','terminal')
    birth=fd_value_fields(nodes,birth_body['state'])
    required={'mapping','row','credit','width','close_attempted','closed_confirmed','close_error'}
    if (not required<=set(birth) or birth['mapping']!=node_index
            or birth['row']!=body['actual_row'] or birth['credit']!=body['actual_credit']
            or nodes[birth['close_attempted']]!=['bool',True]
            or nodes[birth['closed_confirmed']]!=['bool',True]
            or nodes[birth['close_error']]!=['none',None]):
        raise Refused('selected_retired_mmap_birth_transition','terminal')
    width=full_value_literal(nodes,birth['width'])
    if type(width) is not int or not 1<=width<=80_000_000:
        raise Refused('selected_retired_mmap_width','terminal')
    close_kind,close=nodes[body['actual_close_record']]
    if (close_kind!='tuple' or len(close)!=8
            or close[:5]!=[state['original_birth'],node_index,body['actual_row'],
                body['actual_credit'],state['full_transfer_snapshot']]
            or nodes[close[5]]!=['bool',True] or nodes[close[6]]!=['bool',True]
            or nodes[close[7]]!=['none',None]):
        raise Refused('selected_retired_mmap_close_record','terminal')
    transfer=fd_value_fields(nodes,state['full_transfer_snapshot'])
    if not {'kind','body','physical','reader'}<=set(transfer) or nodes[transfer['physical']][0]!='bytes':
        raise Refused('selected_retired_mmap_transfer_body','terminal')
    reader_kind,reader=nodes[transfer['reader']]
    if (reader_kind!='Source-object' or reader['module']!='existing_root_caller'
            or reader['class']!='NativeMemoryBody'):
        raise Refused('selected_retired_mmap_transfer_reader','terminal')
    reader_state=fd_value_fields(nodes,reader['state'])
    if 'mapping_cut_ledger' not in reader_state:
        raise Refused('selected_retired_mmap_transfer_cuts','terminal')
    all_kind,all_ledgers=nodes[reader_state['mapping_cut_ledger']]
    kind,ledgers=nodes[body['full_cut_ledgers']]
    if kind!='tuple' or not ledgers or all_kind!='tuple':
        raise Refused('selected_retired_mmap_cut_inventory','terminal')
    selected=[]
    for item in all_ledgers:
        item_kind,refs=nodes[item]
        if item_kind!='tuple' or len(refs)!=7:
            raise Refused('selected_retired_mmap_original_ledger','terminal')
        if refs[0]==state['original_birth']:selected.append(item)
    if selected!=ledgers:
        raise Refused('selected_retired_mmap_transfer_cut_alias','terminal')
    scalar_names=('schema','creator_pid','actual_pid','role','backing_birth9',
        'backing_cut9','width','position','access','closed','cohort_before_effect')
    def scalar(root):
        kind,value=nodes[root]
        if kind=='tuple':
            if (len(value)!=9 or any(nodes[i][0]!='str' for i in value)
                    or any(str(int(nodes[i][1]))!=nodes[i][1] for i in value)):
                raise Refused('selected_retired_mmap_identity9','terminal')
            return tuple(nodes[i][1] for i in value)
        if kind not in ('str','int','bool'):
            raise Refused('selected_retired_mmap_cut_scalar','terminal')
        return full_value_literal(nodes,root)
    for item in ledgers:
        ledger=nodes[item][1]
        if (ledger[0]!=state['original_birth'] or ledger[2]!=node_index
                or ledger[4]!=body['actual_row'] or ledger[5]!=body['actual_credit']):
            raise Refused('selected_retired_mmap_full_cut_alias','terminal')
        cut=fd_value_fields(nodes,ledger[1])
        if (set(cut)!=set(scalar_names)|{'actual_row','actual_credit','full_bytes'}
                or nodes[cut['schema']]!=['str','friday.sol091.selected-owned-mmap-public-cut.v1']
                or cut['actual_row']!=body['actual_row'] or cut['actual_credit']!=body['actual_credit']
                or cut['full_bytes']!=ledger[3] or nodes[ledger[3]][0]!='bytes'
                or nodes[ledger[3]][1]['bytes']!=width or scalar(cut['width'])!=width
                or nodes[cut['closed']]!=['bool',False] or nodes[cut['access']]!=['str','WRITE']
                or nodes[cut['cohort_before_effect']][0]!='bool'):
            raise Refused('selected_retired_mmap_full_historical_cut','terminal')
        position=scalar(cut['position'])
        if type(position) is not int or not 0<=position<=width:
            raise Refused('selected_retired_mmap_historical_position','terminal')
        frozen_kind,frozen=nodes[ledger[6]]
        if frozen_kind!='tuple' or len(frozen)!=len(scalar_names):
            raise Refused('selected_retired_mmap_frozen_scalars','terminal')
        for name,old in zip(scalar_names,frozen):
            if type(scalar(cut[name])) is not type(scalar(old)) or scalar(cut[name])!=scalar(old):
                raise Refused('selected_retired_mmap_historical_drift','terminal')
        before=scalar(cut['backing_birth9']);after=scalar(cut['backing_cut9'])
        if (type(before) is not tuple or type(after) is not tuple
                or before[:6]!=after[:6] or after[6]!=str(width)):
            raise Refused('selected_retired_mmap_historical_backing','terminal')


def validate_mapping_return_ledger(nodes,body):
    state=fd_value_fields(nodes,body['state'])
    if 'creation' not in state:
        return
    creation=fd_value_fields(nodes,state['creation'])
    if 'schema' not in creation or nodes[creation['schema']]!=['str','friday.sol106.actual-mmap-constructor.v1']:
        return
    attempted=nodes[creation['attempted']]
    returned=nodes[creation['returned']]
    complete=nodes[creation['state_complete']]
    original=nodes[creation['original_error']][0]
    metadata=nodes[creation['metadata_error']][0]
    alias=nodes[creation['returned_mapping']][0]
    if attempted==['bool',True] and returned!=['bool',True] and original=='none' and metadata=='none':
        raise Refused('mapping_return_recovery_unconfirmed','terminal')
    if returned!=['bool',True] and alias!='none' and original=='none':
        raise Refused('mapping_return_recovery_unconfirmed','terminal')
    if complete==['bool',True] and (returned!=['bool',True] or original!='none' or metadata!='none'):
        raise Refused('mapping_return_false_complete','terminal')
    if returned==['bool',True]:
        if alias=='none' or 'mapping' not in state or nodes[state['mapping']][0]=='none':
            raise Refused('mapping_return_alias_missing','terminal')
        if state['mapping']!=creation['returned_mapping']:
            raise Refused('mapping_return_alias_split','terminal')


def validate_full_value_arena(arena,body_reader=None):
    """Strict nested body/index validation, not independent native acceptance."""
    from common import exact,integer
    v9=arena.get('schema')=='friday.sol105.stock-full-value-arena.v9'
    exact(arena,('schema','roots','node_count','chunks','history_domains','physical_body','unsupported','complete','truncated')+ (('edge_routes',) if v9 else ()),'full_value_arena')
    if arena['schema'] not in ('friday.sol086.stock-full-value-arena.v5','friday.sol090.stock-full-value-arena.v6','friday.sol091.stock-full-value-arena.v7','friday.astra.a232.stock-full-value-arena.v8','friday.sol105.stock-full-value-arena.v9') or arena['truncated'] is not False or arena['complete'] is not True or arena['unsupported']!=[]:
        raise Refused('full_value_arena_incomplete','terminal')
    count=integer(arena['node_count'],262144)
    if type(arena['chunks']) is not list or len(arena['chunks'])>512:
        raise Refused('full_value_arena_count','terminal')
    if any(type(chunk) is not list or len(chunk)>512 for chunk in arena['chunks']):
        raise Refused('full_value_arena_count','terminal')
    nodes=[row for chunk in arena['chunks'] for row in chunk]
    if len(nodes)!=count or any(type(chunk) is not list or len(chunk)>512 for chunk in arena['chunks']):raise Refused('full_value_arena_count','terminal')
    if body_reader is None:raise Refused('prepared_body_full_reader_required','terminal')
    if getattr(body_reader,'parent_region_journal',None) is False:
        raise Refused('prefix_parent_region_journal_required','terminal')
    body_reader.validate_spans(nodes,arena['physical_body'])
    def index(i):
        if type(i) is not int or not 0<=i<count:raise Refused('full_value_arena_reference','terminal')
    for i in arena['roots']:index(i)
    for row in nodes:
        if type(row) is not list or len(row)!=2:raise Refused('full_value_arena_row','terminal')
        kind,body=row
        if kind=='runtime-support':
            if not v9:raise Refused('actual_runtime_support_v9_required','terminal')
            exact(body,('producer_serial',),'actual_runtime_support')
            integer(body['producer_serial'],262144,1)
            body_reader.require_runtime_support(body['producer_serial'])
        elif kind=='selected-own-hash':
            if not v9:raise Refused('actual_hash_ledger_v9_required','terminal')
            exact(body,('state',),'actual_hash_ledger');index(body['state'])
            if nodes[body['state']][0]!='dict':raise Refused('actual_hash_ledger_body','terminal')
        elif kind=='selected-retired-mmap':
            if arena['schema'] not in ('friday.astra.a232.stock-full-value-arena.v8','friday.sol105.stock-full-value-arena.v9'):
                raise Refused('selected_retired_mmap_v8_required','terminal')
            exact(body,('state','full_cut_ledgers','actual_row','actual_credit',
                'actual_close_record'),'selected_retired_mmap')
            for value in body.values():index(value)
            if (nodes[body['state']][0]!='dict' or nodes[body['full_cut_ledgers']][0]!='tuple'
                    or nodes[body['actual_row']][0]!='dict'
                    or nodes[body['actual_close_record']][0]!='tuple'):
                raise Refused('selected_retired_mmap_full_body','terminal')
        elif kind in ('selected-owned-mmap','selected-public-stock'):
            if arena['schema'] not in ('friday.sol091.stock-full-value-arena.v7','friday.astra.a232.stock-full-value-arena.v8','friday.sol105.stock-full-value-arena.v9'):
                raise Refused('selected_own_public_v7_required','terminal')
            if kind=='selected-owned-mmap':
                exact(body,('state','full_bytes','actual_row','actual_credit'),'selected_own_mmap')
                for value in body.values():index(value)
                if nodes[body['state']][0]!='dict' or nodes[body['full_bytes']][0]!='bytes' or nodes[body['actual_row']][0]!='dict':
                    raise Refused('selected_own_mmap_full_body','terminal')
            else:
                exact(body,('state',),'selected_public_stock');index(body['state'])
                # Public/native completeness is not inferred from a schema.
                raise Refused('selected_public_native_state_not_complete_CODE','terminal')
        elif kind in ('selected-native-context','selected-native-module'):
            if arena['schema'] not in ('friday.sol090.stock-full-value-arena.v6','friday.sol091.stock-full-value-arena.v7','friday.astra.a232.stock-full-value-arena.v8','friday.sol105.stock-full-value-arena.v9'):
                raise Refused('native_owned_body_v6_required','terminal')
            if kind=='selected-native-context':
                exact(body,('state',),'selected_native_state');index(body['state'])
                if nodes[body['state']][0]!='dict':raise Refused('selected_native_state','terminal')
            else:
                exact(body,('module','context'),'selected_native_module')
                if body['module']!='publisher_owned_custody':raise Refused('selected_native_module','terminal')
                index(body['context'])
                if nodes[body['context']][0]!='selected-native-context':
                    raise Refused('selected_native_module_actual_owner','terminal')
        elif kind=='none':
            if body is not None:raise Refused('full_value_arena_scalar','terminal')
        elif kind=='bool':
            if type(body) is not bool:raise Refused('full_value_arena_scalar','terminal')
        elif kind=='int':
            if type(body) is not str or str(int(body))!=body:raise Refused('full_value_arena_integer','terminal')
        elif kind=='str':
            if type(body) is not str:raise Refused('full_value_arena_scalar','terminal')
        elif kind=='float64':
            if type(body) is not str or len(body)!=16 or any(c not in '0123456789abcdef' for c in body):raise Refused('full_value_float64','terminal')
        elif kind in ('token-missing','ellipsis'):
            if body is not None:raise Refused('full_value_singleton','terminal')
        elif kind in ('bytes','bytearray'):
            exact(body,('offset','bytes'),'full_value_arena_bytes')
            # Full same-owner Held readback/span coverage was checked above.
            # Original immutable/mutable kind and repeated reference identity
            # are retained; this is not a native layout/alias qualification.
        elif kind in ('list','tuple','set','frozenset','range','slice'):
            if type(body) is not list:raise Refused('full_value_arena_sequence','terminal')
            if kind in ('range','slice') and len(body)!=3:raise Refused('full_value_arena_sequence','terminal')
            if kind in ('set','frozenset') and len(set(body))!=len(body):raise Refused('full_value_set_duplicate','terminal')
            for i in body:index(i)
        elif kind in ('dict','context'):
            if type(body) is not list:raise Refused('full_value_arena_dict','terminal')
            for pair in body:
                if type(pair) is not list or len(pair)!=2:raise Refused('full_value_arena_dict','terminal')
                index(pair[0]);index(pair[1])
        elif kind=='context-var':
            exact(body,('name','has_default','default','present','current'),'full_value_context_var')
            if type(body['name']) is not str or type(body['has_default']) is not bool or type(body['present']) is not bool:raise Refused('full_value_context_var','terminal')
            index(body['default']);index(body['current'])
        elif kind=='context-token':
            exact(body,('var','old_value','transition')+(('actual_context','used') if v9 else ()),'full_value_context_token')
            for name in ('var','old_value','transition'):index(body[name])
            if v9:
                index(body['actual_context'])
                if type(body['used']) is not bool:raise Refused('full_value_context_token_used','terminal')
        elif kind=='code-body':
            names=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals',
                'co_stacksize','co_flags','co_code','co_consts','co_names','co_varnames',
                'co_filename','co_name','co_qualname','co_firstlineno','co_linetable',
                'co_exceptiontable','co_freevars','co_cellvars')
            exact(body,names,'full_value_code_body')
            for value in body.values():index(value)
        elif kind=='error':
            exact(body,('module','class','args','attributes','cause','context','suppress_context','traceback'),'full_value_error')
            for name in ('args','attributes','cause','context','traceback'):index(body[name])
            if type(body['module']) is not str or type(body['class']) is not str or type(body['suppress_context']) is not bool:raise Refused('full_value_error_type','terminal')
        elif kind=='traceback':
            exact(body,('next','frame','line','lasti'),'full_value_traceback');index(body['next']);index(body['frame'])
            if type(body['line']) is not int or type(body['lasti']) is not int:raise Refused('full_value_traceback','terminal')
        elif kind=='frame':
            exact(body,('filename','name','line','locals','globals','code','lasti','trace','trace_lines','trace_opcodes'),'full_value_frame')
            for name in ('locals','globals','code','trace'):index(body[name])
            if type(body['lasti']) is not int or type(body['trace_lines']) is not bool or type(body['trace_opcodes']) is not bool:raise Refused('full_value_frame','terminal')
            if type(body['filename']) is not str or type(body['name']) is not str or type(body['line']) is not int:raise Refused('full_value_frame','terminal')
        elif kind=='Source-object':
            exact(body,('module','class','state')+(('binding',) if v9 else ()),'full_value_Source_object')
            if v9:index(body['binding'])
            if body['module'] not in _SOURCE_OBJECT_MODULES or type(body['class']) is not str:raise Refused('full_value_Source_type','terminal')
            index(body['state'])
        elif kind=='bound-method':
            exact(body,('self','function'),'full_value_bound_method');index(body['self']);index(body['function'])
        elif kind in ('staticmethod','classmethod'):
            exact(body,('function','attributes'),'full_value_method_descriptor');index(body['function']);index(body['attributes'])
        elif kind=='property':
            exact(body,('get','set','delete','doc'),'full_value_property')
            for i in body.values():index(i)
        elif kind=='memoryview':
            exact(body,('object','full_bytes','format','shape','strides','readonly'),'full_value_memoryview')
            for name in ('object','full_bytes','shape','strides'):index(body[name])
            if type(body['readonly']) is not bool or type(body['format']) is not str:raise Refused('full_value_memoryview','terminal')
        elif kind=='qualified-function':
            exact(body,('module','qualname','filename','firstlineno','defaults','kwdefaults','function_dict','closure','globals',
                'module_body','annotations','builtins','code','preimage_sha256','preimage_bytes')+(('annotate','annotations_present','annotate_present','binding') if v9 else ()),'full_value_qualified_function')
            if body['module'] not in _SOURCE_OBJECT_MODULES or type(body['qualname']) is not str or type(body['filename']) is not str or type(body['firstlineno']) is not int or type(body['globals']) is not int:
                raise Refused('full_value_qualified_function','terminal')
            if type(body['preimage_sha256']) is not str or len(body['preimage_sha256'])!=64 or type(body['preimage_bytes']) is not int or body['preimage_bytes']<0:
                raise Refused('full_value_qualified_function','terminal')
            index(body['defaults']);index(body['kwdefaults']);index(body['function_dict']);index(body['closure']);index(body['globals'])
            for name in ('code','module_body','annotations','builtins'):index(body[name])
            if v9:
                index(body['annotate']);index(body['binding'])
                if type(body['annotations_present']) is not bool or type(body['annotate_present']) is not bool:
                    raise Refused('actual_unforced_annotations_body','terminal')
        elif kind=='closure-cell':
            exact(body,('empty','contents'),'full_value_closure_cell');index(body['contents'])
            if type(body['empty']) is not bool:raise Refused('full_value_closure_cell','terminal')
            if body['empty'] and nodes[body['contents']]!=['none',None]:raise Refused('full_value_closure_cell','terminal')
        elif kind=='qualified-module':
            exact(body,('module','file','preimage_sha256','preimage_bytes','mutable')+(('binding',) if v9 else ()),'full_value_qualified_module')
            if v9:index(body['binding'])
            if body['module'] not in _SOURCE_OBJECT_MODULES or type(body['file']) is not str or type(body['mutable']) is not int:
                raise Refused('full_value_qualified_module','terminal')
            if type(body['preimage_sha256']) is not str or len(body['preimage_sha256'])!=64 or type(body['preimage_bytes']) is not int or body['preimage_bytes']<0:
                raise Refused('full_value_qualified_module','terminal')
            index(body['mutable'])
        elif kind=='qualified-class':
            exact(body,('module','qualname','fields')+(('binding',) if v9 else ()),'full_value_qualified_class')
            if v9:index(body['binding'])
            if body['module'] not in _SOURCE_OBJECT_MODULES or type(body['qualname']) is not str:
                raise Refused('full_value_qualified_class','terminal')
            index(body['fields'])
        elif kind=='fd-collection':
            exact(body,('collection_identity','kind','owner_pid','generation','domain_identity',
                'journal_row_list','whole_domain','row_count','body_chunks','key_chunks'),'full_value_fd_collection')
            if (type(body['collection_identity']) is not str or body['kind'] not in ('list','dict')
                    or type(body['owner_pid']) is not int or type(body['generation']) is not int
                    or type(body['domain_identity']) is not str
                    or type(body['journal_row_list']) is not bool or body['whole_domain'] is not False):
                raise Refused('full_value_fd_collection','terminal')
            integer(body['row_count'],262144)
            roots=fd_collection_roots(body,'body_chunks')
            for root in roots:
                index(root)
                if nodes[root][0]!='dict':raise Refused('full_value_fd_bodies','terminal')
            if body['kind']=='list':
                if body['key_chunks'] is not None:raise Refused('full_value_fd_keys','terminal')
            else:
                keys=set()
                for root in fd_collection_roots(body,'key_chunks'):
                    index(root);kind_key,key=nodes[root]
                    if kind_key not in ('int','str'):raise Refused('full_value_fd_keys','terminal')
                    tagged=(kind_key,key)
                    if tagged in keys:raise Refused('full_value_fd_key_duplicate','terminal')
                    keys.add(tagged)
        elif kind=='unsupported' and body=='mapping_close_outcome_unknown':
            raise Refused('mapping_close_outcome_unknown','terminal')
        elif kind=='unsupported' and body=='mapping_return_recovery_unconfirmed':
            raise Refused('mapping_return_recovery_unconfirmed','terminal')
        else:raise Refused('full_value_arena_unsupported','terminal')
    for node_index,row in enumerate(nodes):
        kind,body=row
        if kind=='Source-object' and body['module']=='selected_owned_values' and body['class']=='OwnedMappingBirth':
            validate_mapping_return_ledger(nodes,body)
        if kind=='selected-own-hash':
            state=fd_value_fields(nodes,body['state'])
            fields={'schema','mode','name','constructor_original','constructor_bytes',
                'parent','parent_operations_at_copy','runtime_h','actual_attempted','constructed','updates',
                'original_error','metadata_error','state_complete'}
            if set(state)!=fields or nodes[state['schema']]!=['str','friday.astra256.own-hash-input-ledger.v2']:
                raise Refused('actual_hash_constructor_full_ledger','terminal')
            if (nodes[state['constructed']]!=['bool',True] or nodes[state['actual_attempted']]!=['bool',True]
                    or nodes[state['state_complete']][0]!='bool'):
                raise Refused('actual_hash_actual_constructor_prefix','terminal')
            state_complete=nodes[state['state_complete']][1]
            if state_complete and (nodes[state['original_error']]!=['none',None]
                    or nodes[state['metadata_error']]!=['none',None]):
                raise Refused('actual_hash_false_complete_constructor','terminal')
            if (nodes[state['original_error']][0] not in ('none','error')
                    or nodes[state['metadata_error']][0] not in ('none','error')
                    or nodes[state['original_error']]!=['none',None]):
                # A selected hash node denotes an actual returned constructor.
                # Its later bookkeeping error is not a failed stock factory.
                raise Refused('actual_hash_original_constructor_error','terminal')
            mode=full_value_literal(nodes,state['mode'])
            if mode not in ('sha256','new','copy') or nodes[state['name']][0]!='str' or state['runtime_h']!=node_index:
                raise Refused('actual_hash_constructor_runtime_alias','terminal')
            if nodes[state['constructor_original']][0] not in ('bytes','bytearray','memoryview') or nodes[state['constructor_bytes']][0]!='bytes':
                raise Refused('actual_hash_full_constructor_operand','terminal')
            if mode=='copy':
                parent=nodes[state['parent']]
                if parent[0]!='Source-object' or parent[1]['module']!='resource_meter' or parent[1]['class']!='_Hash':
                    raise Refused('actual_hash_copy_original_parent','terminal')
                count=full_value_literal(nodes,state['parent_operations_at_copy'])
                if type(count) is not int or count<1 or nodes[state['constructor_bytes']][1]['bytes']!=0:
                    raise Refused('actual_hash_copy_original_prefix','terminal')
                parent_fields=fd_value_fields(nodes,parent[1]['state'])
                if set(parent_fields)!={'h','own_state'}:raise Refused('actual_hash_copy_parent_state','terminal')
                parent_ledger=fd_value_fields(nodes,parent_fields['own_state'])
                if (nodes[parent_ledger['updates']][0]!='list' or count>1+len(nodes[parent_ledger['updates']][1])
                        or parent_fields['h']!=parent_ledger['runtime_h']
                        or nodes[parent_fields['h']][0]!='selected-own-hash'):
                    raise Refused('actual_hash_copy_original_prefix','terminal')
                if state_complete:
                    # A later parent mutation/error cannot retroactively alter
                    # this genuine copy. Verify precisely the retained prefix
                    # ordinal captured BEFORE the actual copy, not later state.
                    if (nodes[parent_ledger['constructed']]!=['bool',True]
                            or nodes[parent_ledger['original_error']]!=['none',None]
                            or nodes[parent_ledger['metadata_error']]!=['none',None]):
                        raise Refused('actual_hash_copy_parent_constructor','terminal')
                    for prior in nodes[parent_ledger['updates']][1][:count-1]:
                        pf=fd_value_fields(nodes,prior)
                        if (nodes[pf['actual_attempted']]!=['bool',True] or
                                nodes[pf['confirmed']]!=['bool',True] or
                                nodes[pf['original_error']]!=['none',None] or
                                nodes[pf['metadata_error']]!=['none',None]):
                            raise Refused('actual_hash_false_complete_copy_prefix','terminal')
            elif nodes[state['parent']]!=['none',None] or nodes[state['parent_operations_at_copy']]!=['none',None]:
                raise Refused('actual_hash_constructor_no_parent','terminal')
            updates=nodes[state['updates']]
            if updates[0]!='list':raise Refused('actual_hash_update_full_ledger','terminal')
            for operation in updates[1]:
                row=fd_value_fields(nodes,operation)
                if (set(row)!={'original_input','full_bytes','attempted','actual_attempted','confirmed','original_error','metadata_error'}
                        or nodes[row['attempted']]!=['bool',True]
                        or nodes[row['actual_attempted']][0]!='bool' or nodes[row['confirmed']][0]!='bool'
                        or nodes[row['original_error']][0] not in ('none','error')
                        or nodes[row['metadata_error']][0] not in ('none','error')
                        or nodes[row['full_bytes']][0]!='bytes'
                        or nodes[row['original_input']][0] not in ('bytes','bytearray','memoryview')):
                    raise Refused('actual_hash_full_update_prefix','terminal')
                actual_attempted=nodes[row['actual_attempted']][1];confirmed=nodes[row['confirmed']][1]
                if confirmed and (not actual_attempted or nodes[row['original_error']]!=['none',None]):
                    raise Refused('actual_hash_false_confirmed_update','terminal')
                if nodes[row['metadata_error']]!=['none',None] and not confirmed:
                    raise Refused('actual_hash_metadata_before_return','terminal')
                if state_complete and (not confirmed or not actual_attempted
                        or nodes[row['metadata_error']]!=['none',None]):
                    raise Refused('actual_hash_false_complete_update','terminal')
            # Carrier validates the FULL actual byte spans separately. Native
            # records bind actual input/ledger/runtime refs BEFORE each effect.
            # ABI/cost/workload acceptance is NOT established by this shape.
        elif kind=='selected-retired-mmap':
            validate_retired_mapping_node(nodes,node_index,body)
        elif kind=='selected-owned-mmap':
            state={}
            for key,value in nodes[body['state']][1]:
                if nodes[key][0]!='str' or nodes[key][1] in state:
                    raise Refused('selected_own_mmap_state','terminal')
                state[nodes[key][1]]=value
            fields={'schema','creator_pid','actual_pid','role','backing_birth9','backing_cut9',
                'actual_row','actual_credit','width','position','access','closed','full_bytes',
                'cohort_before_effect'}
            if set(state)!=fields or nodes[state['schema']]!=['str','friday.sol091.selected-owned-mmap-public-cut.v1']:
                raise Refused('selected_own_mmap_schema','terminal')
            if nodes[state['cohort_before_effect']][0]!='bool':
                raise Refused('selected_own_mmap_cohort_cut','terminal')
            # The bool records the before-write image. It is not completeness.
            if (state['actual_row']!=body['actual_row'] or state['actual_credit']!=body['actual_credit']
                    or state['full_bytes']!=body['full_bytes'] or nodes[state['closed']]!=['bool',False]
                    or nodes[state['access']]!=['str','WRITE']):
                raise Refused('selected_own_mmap_actual_alias','terminal')
            width=full_value_literal(nodes,state['width']);position=full_value_literal(nodes,state['position'])
            if (type(width) is not int or not 1<=width<=80_000_000 or
                    type(position) is not int or not 0<=position<=width or
                    nodes[body['full_bytes']][1]['bytes']!=width):
                raise Refused('selected_own_mmap_complete_width','terminal')
            def nine(root):
                kind,refs=nodes[root]
                if kind!='tuple' or len(refs)!=9 or any(nodes[r][0]!='str' or str(int(nodes[r][1]))!=nodes[r][1] for r in refs):
                    raise Refused('selected_own_mmap_identity9','terminal')
                return tuple(nodes[r][1] for r in refs)
            birth=nine(state['backing_birth9']);cut=nine(state['backing_cut9'])
            if birth[:6]!=cut[:6] or cut[6]!=str(width):
                raise Refused('selected_own_mmap_backing_birth','terminal')
            row=fd_value_fields(nodes,body['actual_row'])
            row9=nine(row['identity9_decimal_strings']) if nodes[row['identity9_decimal_strings']][0]=='tuple' else None
            # Actual FD histories keep their original list spelling. Compare
            # the live birth's first six identity components without rebinding.
            if row9 is None:
                kind,refs=nodes[row['identity9_decimal_strings']]
                if kind!='list' or len(refs)!=9 or any(nodes[r][0]!='str' for r in refs):
                    raise Refused('selected_own_mmap_original_row','terminal')
                row9=tuple(nodes[r][1] for r in refs)
            if row9[:6]!=birth[:6]:raise Refused('selected_own_mmap_original_row','terminal')
        elif kind=='selected-native-context':
            state={}
            for key,value in nodes[body['state']][1]:
                if nodes[key][0]!='str' or nodes[key][1] in state:
                    raise Refused('selected_native_state_fields','terminal')
                state[nodes[key][1]]=value
            fields={'schema','owner_pid','root_fact','qualification','held_root_tool_preimage',
                'source_qualification','anchors','banks','errors','result','source_end','serial',
                'charged_allocation','bank_total','end_attempted','final_close_attempted',
                'final_close_confirmed','final_raw','role','actual_pid','actual_last_close','actual_native_close_records'}
            schema=nodes[state['schema']] if 'schema' in state else None
            if schema in (['str','friday.sol091.selected-owned-native-public-state.v3'],
                          ['str','friday.sol100.selected-owned-native-public-state.v4']):
                fields=fields|{'prefix_error_cell','after_document_error_cell'}
            if schema==['str','friday.sol100.selected-owned-native-public-state.v4']:
                fields=fields|{'original_master_pool','actual_native_prepared_rows'}
                if (nodes[state['original_master_pool']][0]!='dict'
                        or nodes[state['actual_native_prepared_rows']][0]!='list'):
                    raise Refused('actual_original_pool_prepared_owned_cut','terminal')
            if set(state)!=fields or schema not in (['str','friday.sol090.selected-owned-native-public-state.v2'],
                    ['str','friday.sol091.selected-owned-native-public-state.v3'],
                    ['str','friday.sol100.selected-owned-native-public-state.v4']):
                raise Refused('selected_native_state_schema','terminal')
            if nodes[state['role']] not in (['str','OWNING_ROOT'],['str','INHERITED_READONLY_SHADOW']):
                raise Refused('selected_native_state_role','terminal')
            for name in ('owner_pid','actual_pid','serial','charged_allocation','bank_total',
                         'end_attempted','final_close_attempted','final_close_confirmed'):
                if nodes[state[name]][0]!='int' or int(nodes[state[name]][1])<0:
                    raise Refused('selected_native_state_integer','terminal')
            if nodes[state['held_root_tool_preimage']][0]!='bytes':
                raise Refused('selected_native_complete_tool_preimage','terminal')
            if nodes[state['anchors']][0]!='dict' or nodes[state['banks']][0]!='dict' or nodes[state['errors']][0]!='tuple':
                raise Refused('selected_native_complete_banks','terminal')
            close_node=nodes[state['actual_last_close']]
            if close_node[0]!='dict':raise Refused('selected_native_actual_close','terminal')
            close_fields={}
            for key,item in close_node[1]:
                if nodes[key][0]!='str' or nodes[key][1] in close_fields:raise Refused('selected_native_actual_close','terminal')
                close_fields[nodes[key][1]]=item
            if set(close_fields)!={'exact_row','syscall_rc','syscall_errno','publication_confirmed'}:
                raise Refused('selected_native_actual_close','terminal')
            for name in ('syscall_rc','syscall_errno','publication_confirmed'):
                if nodes[close_fields[name]][0]!='int':raise Refused('selected_native_actual_close','terminal')
            if nodes[state['actual_native_close_records']][0]!='list':
                raise Refused('selected_native_actual_close_records','terminal')
            for item in nodes[state['actual_native_close_records']][1]:
                if nodes[item][0]!='dict':raise Refused('selected_native_actual_close_records','terminal')
                fields={}
                for key,value in nodes[item][1]:
                    if nodes[key][0]!='str' or nodes[key][1] in fields:raise Refused('selected_native_actual_close_records','terminal')
                    fields[nodes[key][1]]=value
                if set(fields)!={'exact_row','actual_called','syscall_rc','syscall_errno','publication_confirmed'}:
                    raise Refused('selected_native_actual_close_records','terminal')
                for name in ('actual_called','syscall_rc','syscall_errno','publication_confirmed'):
                    if nodes[fields[name]][0]!='int':raise Refused('selected_native_actual_close_records','terminal')
            if schema in (['str','friday.sol091.selected-owned-native-public-state.v3'],
                          ['str','friday.sol100.selected-owned-native-public-state.v4']):
                expected={'phase','original_type','original_error','original_traceback','actual_document',
                    'saved','syscall_attempted','syscall_rc','syscall_errno','actual_written'}
                for name in ('prefix_error_cell','after_document_error_cell'):
                    row_kind,pairs=nodes[state[name]]
                    cell={nodes[k][1]:v for k,v in pairs} if row_kind=='dict' and all(nodes[k][0]=='str' for k,v in pairs) else {}
                    if set(cell)!=expected or len(pairs)!=len(expected):
                        raise Refused('actual_native_preowned_error_cell','terminal')
                    if nodes[cell['phase']][0]!='str':raise Refused('actual_native_error_phase','terminal')
                    for scalar in ('saved','syscall_attempted','syscall_rc','syscall_errno','actual_written'):
                        if nodes[cell[scalar]][0]!='int':raise Refused('actual_native_error_scalar','terminal')
            # Structural body correspondence is NOT a Root grant/ABI theorem.
            # Receipt acceptance separately calls actual native context.matches.
        elif kind=='qualified-function':
            ck,cb=nodes[body['code']]
            if ck!='code-body':raise Refused('qualified_function_actual_code','terminal')
            if nodes[cb['co_filename']]!=['str',body['filename']] or nodes[cb['co_firstlineno']]!=['int',str(body['firstlineno'])]:raise Refused('qualified_function_code_correspondence','terminal')
            if nodes[body['globals']][0]!='dict':raise Refused('qualified_function_actual_bindings','terminal')
            if v9:
                if (body['annotations_present'] and nodes[body['annotations']][0]!='dict') or (
                        not body['annotations_present'] and nodes[body['annotations']]!=['none',None]):
                    raise Refused('actual_unforced_annotations_body','terminal')
                if not body['annotate_present'] and nodes[body['annotate']]!=['none',None]:
                    raise Refused('actual_unforced_annotate_body','terminal')
                if nodes[body['builtins']][0]!='runtime-support':raise Refused('actual_function_builtins_support','terminal')
            elif nodes[body['annotations']][0]!='dict' or nodes[body['builtins']][0]!='dict':
                raise Refused('qualified_function_actual_bindings','terminal')
            closure=nodes[body['closure']]
            if closure!=['none',None] and (closure[0]!='tuple' or any(nodes[cell][0]!='closure-cell' for cell in closure[1])):
                raise Refused('qualified_function_actual_cells','terminal')
            gk,gb=nodes[body['module_body']]
            if gk!='qualified-module' or gb['module']!=body['module'] or gb['file']!=body['filename']:
                raise Refused('qualified_function_globals','terminal')
            if gb['preimage_sha256']!=body['preimage_sha256'] or gb['preimage_bytes']!=body['preimage_bytes']:
                raise Refused('qualified_function_preimage','terminal')
            if gb['mutable']!=body['globals']:raise Refused('qualified_function_global_alias','terminal')
        elif kind=='qualified-module':
            mk,mb=nodes[body['mutable']]
            if mk!='dict':raise Refused('qualified_module_mutable','terminal')
        elif kind=='frame':
            if nodes[body['code']][0]!='code-body':raise Refused('full_value_frame_code','terminal')
        elif kind=='context':
            variables=set()
            for var,value in body:
                if nodes[var][0]!='context-var' or var in variables:raise Refused('full_value_context_binding','terminal')
                variables.add(var)
        elif kind=='context-token':
            if nodes[body['var']][0]!='context-var' or nodes[body['transition']][0]!='dict':raise Refused('full_value_token_binding','terminal')
            transition={}
            for key,value in nodes[body['transition']][1]:
                if nodes[key][0]!='str' or nodes[key][1] in transition:raise Refused('full_value_token_transition','terminal')
                transition[nodes[key][1]]=value
            fields=('sol070_context_transition','token','var','before','set_value','reset_attempted','reset_confirmed','reset_error')+(('actual_context','used') if v9 else ())
            if set(transition)!=set(fields):raise Refused('full_value_token_transition','terminal')
            if v9:
                if transition['actual_context']!=body['actual_context'] or nodes[body['actual_context']][0]!='context' or nodes[transition['used']]!=['bool',body['used']]:
                    raise Refused('actual_context_token_alias_and_used','terminal')
                if body['used'] is not nodes[transition['reset_confirmed']][1]:raise Refused('actual_context_token_reset_state','terminal')
            if nodes[transition['sol070_context_transition']]!=['bool',True] or transition['token']!=node_index or transition['var']!=body['var']:raise Refused('full_value_token_transition','terminal')
            if nodes[transition['before']][0]!='context' or nodes[transition['reset_attempted']][0]!='bool' or nodes[transition['reset_confirmed']][0]!='bool':raise Refused('full_value_token_transition','terminal')
            if nodes[transition['reset_confirmed']][1] is True and (nodes[transition['reset_attempted']][1] is not True or nodes[transition['reset_error']]!=['none',None]):raise Refused('full_value_token_transition','terminal')
        elif kind=='code-body':
            int_fields=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize','co_flags','co_firstlineno')
            str_fields=('co_filename','co_name','co_qualname')
            tuple_fields=('co_consts','co_names','co_varnames','co_freevars','co_cellvars')
            for name in int_fields:
                if nodes[body[name]][0]!='int' or int(nodes[body[name]][1])<0:raise Refused('full_value_code_field','terminal')
            for name in str_fields:
                if nodes[body[name]][0]!='str':raise Refused('full_value_code_field','terminal')
            for name in tuple_fields:
                if nodes[body[name]][0]!='tuple':raise Refused('full_value_code_field','terminal')
            for name in ('co_code','co_linetable','co_exceptiontable'):
                if nodes[body[name]][0]!='bytes':raise Refused('full_value_code_field','terminal')
        elif kind=='context-var':
            if not body['has_default'] and nodes[body['default']]!=['none',None]:raise Refused('full_value_context_default','terminal')
            if not body['present'] and nodes[body['current']]!=['none',None]:raise Refused('full_value_context_current','terminal')
    if v9:
        # Typed references are determined from EACH node shape, not trusted
        # producer counts. Routes must cover every real field/alias exactly.
        scalar=frozenset(('none','bool','int','str','float64','token-missing','ellipsis','bytes','bytearray','runtime-support'))
        fixed={
            'selected-own-hash':('state',),'selected-owned-mmap':('state','full_bytes','actual_row','actual_credit'),
            'selected-retired-mmap':('state','full_cut_ledgers','actual_row','actual_credit','actual_close_record'),
            'selected-native-context':('state',),'selected-native-module':('context',),
            'context-var':('default','current'),'context-token':('var','old_value','transition','actual_context'),
            'code-body':('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize','co_flags','co_code','co_consts','co_names','co_varnames','co_filename','co_name','co_qualname','co_firstlineno','co_linetable','co_exceptiontable','co_freevars','co_cellvars'),
            'error':('args','attributes','cause','context','traceback'),'traceback':('next','frame'),
            'frame':('locals','globals','code','trace'),'Source-object':('state','binding'),
            'bound-method':('self','function'),'staticmethod':('function','attributes'),'classmethod':('function','attributes'),
            'property':('get','set','delete','doc'),'memoryview':('object','full_bytes','shape','strides'),
            'qualified-function':('defaults','kwdefaults','function_dict','closure','globals','module_body','annotations','annotate','builtins','code','binding'),
            'closure-cell':('contents',),'qualified-module':('mutable','binding'),'qualified-class':('fields','binding')}
        expected={}
        for parent,(kind,body) in enumerate(nodes):
            if kind in scalar:children=[]
            elif kind in ('list','tuple','set','frozenset','range','slice'):children=body
            elif kind in ('dict','context'):children=[item for pair in body for item in pair]
            elif kind=='fd-collection':
                children=list(fd_collection_roots(body,'body_chunks'))
                if body['kind']=='dict':children+=list(fd_collection_roots(body,'key_chunks'))
            elif kind in fixed:children=[body[name] for name in fixed[kind]]
            else:raise Refused('actual_typed_data_route_kind','terminal')
            for child in children:
                index(child);pair=(parent,child);expected[pair]=expected.get(pair,0)+1
        routes=arena['edge_routes'];exact(routes,('count','chunks'),'actual_data_routes')
        integer(routes['count'],1048576)
        if type(routes['chunks']) is not list or len(routes['chunks'])>2048 or any(
                type(chunk) is not list or not 1<=len(chunk)<=512 for chunk in routes['chunks']):
            raise Refused('actual_data_routes_chunks','terminal')
        actual={};root_refs=[];data_edges={};read_count=0
        for chunk in routes['chunks']:
            for edge in chunk:
                if type(edge) is not list or len(edge)!=3 or type(edge[0]) is not int or type(edge[2]) is not bool:
                    raise Refused('actual_data_routes_fields','terminal')
                parent,child,data=edge;index(child);read_count+=1
                if parent==-1:
                    if data is not True:raise Refused('actual_required_root_route','terminal')
                    root_refs.append(child);continue
                index(parent);pair=(parent,child);actual[pair]=actual.get(pair,0)+1
                if not data and nodes[child][0]!='runtime-support':
                    raise Refused('actual_support_route_producer_required','terminal')
                if data:data_edges.setdefault(parent,[]).append(child)
        if read_count!=routes['count'] or root_refs!=arena['roots'] or actual!=expected:
            raise Refused('actual_data_routes_full_field_coverage','terminal')
        required=set(arena['roots']);todo=list(required)
        for parent in todo:
            for child in data_edges.get(parent,()):
                if child not in required:required.add(child);todo.append(child)
        if any(nodes[child][0]=='runtime-support' for child in required):
            raise Refused('runtime_support_cannot_replace_required_own_data','terminal')
        # Full actual held-body relation, with raw Source bytes and original
        # code/globals aliases. The body carrier/native registry remains the
        # actual custody supplier, independent compilation acceptance NOT_RUN.
        held_body_checks=set()
        for slot,(kind,body) in enumerate(nodes):
            if kind not in ('Source-object','qualified-module','qualified-class','qualified-function'):continue
            k,refs=nodes[body['binding']]
            if k!='tuple' or len(refs)!=6:raise Refused('actual_compiled_binding_tuple','terminal')
            module,namespace,code,pin,raw,confirmed=refs
            if nodes[module][0]!='qualified-module' or nodes[namespace][0]!='dict' or nodes[code][0]!='code-body' or nodes[pin][0]!='dict' or nodes[raw][0]!='bytes' or nodes[confirmed][0]!='bool':
                raise Refused('actual_compiled_binding_full_body','terminal')
            mb=nodes[module][1]
            if mb['mutable']!=namespace:raise Refused('actual_compiled_namespace_alias','terminal')
            pf=fd_value_fields(nodes,pin)
            if not {'path','bytes','sha256'}<=set(pf):
                raise Refused('actual_binding_full_original_pin','terminal')
            if (nodes[pf['path']]!=['str',mb['file']] or
                    nodes[pf['bytes']]!=['int',str(mb['preimage_bytes'])] or
                    nodes[pf['sha256']]!=['str',mb['preimage_sha256']] or
                    nodes[raw][1]['bytes']!=mb['preimage_bytes'] or
                    nodes[nodes[code][1]['co_filename']]!=['str',mb['file']]):
                raise Refused('actual_binding_full_pin_code_raw_correspondence','terminal')
            mk,mrefs=nodes[mb['binding']]
            if mk!='tuple' or mrefs!=refs:
                raise Refused('actual_binding_module_supplier_aliases','terminal')
            # Physical reader verifies ALL held Source bytes against the
            # actual retained original supplier, not SHA/path/type alone.
            check=getattr(body_reader,'require_held_binding',None)
            if check is None:raise Refused('actual_held_binding_recipient_missing','terminal')
            pair=(pin,raw)
            if pair not in held_body_checks:
                check(nodes,pin,raw,arena['physical_body']);held_body_checks.add(pair)
            if kind=='qualified-module' and (slot!=module or body['mutable']!=namespace):
                raise Refused('actual_module_binding_alias','terminal')
            if kind=='qualified-function' and (body['globals']!=namespace or body['module_body']!=module):
                raise Refused('actual_function_binding_alias','terminal')
    validate_fd_value_histories(nodes,full_value_history_domains(arena))
    return nodes


def raw_file(path, maximum=65536, owner=None):
    if owner is None:raise Refused("raw_file_existing_owner")
    credit=owner.observation_credit if hasattr(owner,"observation_credit") else owner
    book = OwnedFDs(credit=credit)
    credit.retain_journal(book)
    fd = -1
    try:
        credit.before_read(maximum+1)
        fd = open_absolute(path, journal=book)
        credit.before_read(maximum+1)
        raw = os.read(fd, maximum+1)
        credit.read_debit(len(raw))
        if len(raw) > maximum: raise Refused("observer_raw_size")
        return raw.decode("ascii")
    finally:
        if fd >= 0:
            book.close_one(fd)
        else:
            book.close()
        if book.fds:
            import sys
            current = sys.exc_info()[1]
            fact = {"fds": [{"fd": held, "holder": book.meta.get(held, {}).get("holder"),
                "credit": book.meta.get(held, {}).get("credit"),
                "identity9_decimal_strings": book.meta.get(held, {}).get("identity9_decimal_strings"),
                "status": book.meta.get(held, {}).get("status")} for held in sorted(book.fds)]}
            if current is None:
                exc = Refused("temporary_fd_close_unconfirmed", detail=fact)
                exc.retained_journal = book
                raise exc
            current.retained_journal = book
        else:
            credit.journals.remove(book)


def proc_start(pid, owner):
    raw = raw_file("/proc/"+str(pid)+"/stat", 8192,owner)
    close = raw.rfind(")")
    if close < 0: raise Refused("process_identity")
    fields = raw[close+2:].split()
    return {"pid":pid,"start_ticks":int(fields[19]),"raw_stat":raw}


def proc_io(pid, owner):
    raw = raw_file("/proc/"+str(pid)+"/io", 8192,owner)
    rows = {}
    for line in raw.splitlines():
        k, v = line.split(":", 1)
        if k in rows or not v.strip().isdigit(): raise Refused("observer_io")
        rows[k] = int(v.strip())
    required = {"rchar","wchar","syscr","syscw","read_bytes","write_bytes","cancelled_write_bytes"}
    if set(rows) != required: raise Refused("observer_io")
    return {"raw":raw,"counters":rows}


class Reservation:
    def __init__(self, meter, token, slots=0):
        self.meter, self.token, self.closed = meter, token, False
        self.slots=slots
    def commit(self, reads=0, output=0, hash_bytes=0):
        if self.closed: raise Refused("retired_reservation")
        self.meter.commit(self.token, reads, output, hash_bytes)
    def release(self):
        if not self.closed:
            if getattr(self,'fd_rows',{}):return False
            accepted=self.meter.release(self.token)
            if accepted is False:return False
            self.closed = True
        return True
    def retire_slots(self):
        if self.closed or getattr(self,'fd_rows',{}):return False
        self.meter.retire_slots(self.token);self.slots=0;return True


class RootObserver:
    """Independent actual parent owns actor, every native child and terminal."""
    def __init__(self, cgroup, admission, observer_id, started_ns=None, carried_hash=0, preowner=None):
        self.owner_pid = os.getpid()
        self.observer_id, self.cgroup, self.admission = observer_id, cgroup, admission
        self.started=self.deadline=self.reserve_deadline=None
        self.explicit_read = self.explicit_output = self.live_alloc = self.live_slots = 0
        self.explicit_hash = integer(carried_hash, READ_MAX)
        self.peak_slots = self.peak_alloc = 0
        self.pending, self.next_token, self.events, self.errors = {}, 1, [], []
        self.last_observed_physical=0
        self.reservation_preview_read=self.reservation_preview_hash=0
        self.processes, self.pidfds, self.final_io, self.waits = {}, {}, {}, []
        self.partial, self.cleanup_faults = [], []
        self.prefix_reservation_extensions = []
        self.prefix_output_added = 0
        self.owned_results={}
        self.owned_digests={}
        self.held_leases={}
        self.local_owners={};self.local_serial=0
        if preowner is None or preowner.pid!=self.owner_pid or preowner.closed:
            raise Refused("observer_existing_stock_credit")
        self.master_pool=preowner.master_pool
        # The SAME native Root has already spent cold-init/bootstrap time and
        # may have tightened its deadline to the signed admission. Do not
        # reconstruct a fresh 4200-second interval in the Source consumer.
        # Reuse this actual full native cut for bootstrap below, rather than
        # allocating another independent state snapshot just to read clocks.
        master=self.master_pool.state()
        if (type(master) is not dict
                or master.get("schema")!="friday.sol100.original-persistent-master-full-cut.v1"
                or type(master.get("owner_pid")) is not int
                or master["owner_pid"]!=self.owner_pid):
            raise Refused("observer_original_master_clock_owner")
        started=master.get("started_ns")
        deadline=master.get("deadline_ns")
        work_deadline=master.get("work_deadline_ns")
        now=mono()
        if (type(started) is not int or type(deadline) is not int or type(work_deadline) is not int
                or not 0<started<=now<work_deadline<deadline
                or deadline>started+WALL_MAX*10**9
                or deadline-work_deadline!=600*10**9
                or (started_ns is not None and
                    (type(started_ns) is not int or started_ns!=started))):
            raise Refused("observer_original_master_clock_relation")
        self.started=started
        self.deadline=deadline
        self.reserve_deadline=work_deadline
        self.observation_credit=preowner;preowner.observer=self
        self.fd_state=preowner.fd_state
        self.explicit_read=preowner.read_bytes
        self.pending[preowner.token]={"purpose":"same-root-stock-observation-arena",
            "reads":0,"output":0,"hash_bytes":0,"allocation":preowner.allocation,"slots":preowner.slots}
        self.live_alloc=preowner.allocation;self.live_slots=preowner.slots
        # Full stdout/stderr and the terminal envelope have separate physical
        # preimages; pipe capture is also actual output. Credit is not a meter.
        # Retain the actual full-positive proposal before the original Root
        # output store/helper effects. No output over-cap is ever reserved.
        # None is the real pre-signature mode, NOT an invented signed plan.
        self.whole_layout_first_effect_qualified = False
        self.terminal_credit = REFUSAL_RESERVE+INPUT_MAX_TERMINAL*6
        self.root_io_before = proc_io(self.owner_pid,self)
        self.root_usage_before = resource.getrusage(resource.RUSAGE_SELF)
        self.before = self.sample()
        if self.before["pids_current"] != 0 or self.before["processes"] or self.before["memory_current"] != 0:
            raise Refused("cgroup_not_empty")
        # Externally provisioned cgroup limits are verified, never raised here.
        if self.before["memory_max"] != RAM_MAX or self.before["pids_max"] != WORKERS_MAX-1:
            raise Refused("cgroup_limits")
        # Native entry performed the ONE actual signature helper before
        # Source loading. Its full exact native record seeds this same
        # history; do not reset a real cgroup peak or spawn another helper.
        bootstrap=master.get("actual_bootstrap")
        if type(bootstrap) is not dict or bootstrap.get("exit_code")!=0:
            raise Refused("actual_preSource_bootstrap_history")
        if bootstrap.get("lifetime_phase")!="PUBLIC_STOCK_PROCESS_EXITED":
            raise Refused("actual_preSource_bootstrap_phase")
        for name in ("public_stock_parent_legal_retirement_accounted",
                     "public_stock_observables_consumed",
                     "stock_exec_prefix_retained","stock_image_replacement_confirmed",
                     "kernel_process_lifetime_ended","atomic_spawn_pidfd_bound",
                     "existing_parent_bank_full_read","parent_FD_ends",
                     "child_IO_known","final_IO_attempted_once"):
            if type(bootstrap.get(name)) is not int or bootstrap[name]!=1:
                raise Refused("actual_preSource_bootstrap_" + name)
        if type(bootstrap.get("last_child_transport_end_confirmed")) is not int or bootstrap["last_child_transport_end_confirmed"]!=0:
            raise Refused("actual_preSource_false_postexec_return")
        native_before=bootstrap["before_cgroup"];native_after=bootstrap["after_cgroup"]
        if (native_before["memory_peak"]!=0 or native_before["pids_current"]!=0
                or native_after["pids_current"]!=0 or native_after["memory_current"]!=0
                or self.before["memory_peak"]!=native_after["memory_peak"]):
            raise Refused("cgroup_prior_peak")
        actual=bootstrap["actual_process"];pid=actual["pid"]
        if type(pid) is not int or pid<=0 or pid==self.owner_pid:
            raise Refused("actual_preSource_bootstrap_process")
        self.processes[pid]=actual
        self.final_io[pid]=bootstrap["actual_final_io"]
        self.waits.append({"pid":pid,"raw_wait_status":bootstrap["raw_wait_status"],
            "native_preSource_record":bootstrap})
        self.register(self.owner_pid, None, external_root=True)
        self.terminal_hold = self._reserve("full-terminal-forward", output=self.terminal_credit,
            # Internal split only: full body/cut/decoder owners keep the original
            # signed whole limits. _reserve still checks aggregate before effects.
            # These are reservations, NOT measured cost or required-case credit.
            reads=INPUT_MAX_TERMINAL*64, allocation=INPUT_MAX_TERMINAL*768+REFUSAL_RESERVE, slots=0,
            hash_bytes=INPUT_MAX_TERMINAL*32, final=True)
        from alljob_cohort import bind_alljob_external_cohort
        self.alljob_cohort = bind_alljob_external_cohort(self, _PREIMAGE_PINS.get())
        self.prospective_terminal_layout = self.alljob_cohort

    def _owned(self):
        if os.getpid() != self.owner_pid: raise Refused("observer_owner")

    def before_physical_sample(self,maximum,additional_hash=0):
        """Guard each real sample against the complete current Root debit.

        The max is one physical observation, never added twice to explicit
        physical reads. Pending memory-hash passes remain separately additive.
        """
        integer(maximum,READ_MAX);integer(additional_hash,READ_MAX)
        physical=max(self.explicit_read,self.last_observed_physical)
        pending_read=sum(r["reads"] for r in self.pending.values())
        pending_hash=sum(r["hash_bytes"] for r in self.pending.values())
        if physical+self.explicit_hash+pending_read+pending_hash+maximum+additional_hash+self.reservation_preview_read+self.reservation_preview_hash>READ_MAX:
            raise Refused("aggregate_read")

    def sample(self):
        raw = {}
        for name in ("memory.current","memory.peak","memory.max","pids.current",
                     "pids.max","cgroup.procs","io.stat","cgroup.events"):
            raw[name] = raw_file(self.cgroup+"/"+name,owner=self)
        def number(name):
            value = raw[name].strip()
            if not value.isdigit(): raise Refused("observer_metric_missing")
            return int(value)
        processes = []
        for value in raw["cgroup.procs"].split():
            if not value.isdigit(): raise Refused("observer_processes")
            processes.append(int(value))
        if len(processes) > WORKERS_MAX-1 or len(set(processes)) != len(processes):
            raise Refused("worker_cap")
        block_read = block_write = 0
        for line in raw["io.stat"].splitlines():
            fields = line.split()
            if not fields or ":" not in fields[0]: raise Refused("observer_io")
            nums = {}
            for field in fields[1:]:
                k, v = field.split("=",1)
                if k in nums or not v.isdigit(): raise Refused("observer_io")
                nums[k] = int(v)
            if not {"rbytes","wbytes","rios","wios"}.issubset(nums): raise Refused("observer_io")
            block_read += nums["rbytes"]; block_write += nums["wbytes"]
        # An empty io.stat is an actual empty kernel counter set; its complete
        # raw file is retained. Failure to read it is not converted to zero.
        return {"at_ns":mono(),"raw":raw,"memory_current":number("memory.current"),
            "memory_peak":number("memory.peak"),"memory_max":number("memory.max"),
            "pids_current":number("pids.current"),"pids_max":number("pids.max"),
            "processes":processes,"block_read":block_read,"block_write":block_write}

    def register(self, pid, pidfd, external_root=False):
        self._owned()
        if pid in self.processes: raise Refused("duplicate_process")
        # Complete history itself participates in the original ABI128. No
        # truncation or exporting only active processes can satisfy that ABI.
        if len(self.processes) >= SLOTS_MAX: raise Refused("process_history_cap")
        fact = proc_start(pid,self)
        if not external_root:
            current = self.sample()
            if pid not in current["processes"]: raise Refused("child_cgroup")
            if pidfd is None: raise Refused("child_pidfd")
        self.processes[pid] = fact
        if pidfd is not None: self.pidfds[pid] = pidfd

    def sample_processes(self):
        self._owned()
        io = {}; missing = []
        for pid, identity in self.processes.items():
            if pid in self.final_io:
                io[pid] = self.final_io[pid]
                continue
            try:
                if proc_start(pid,self)["start_ticks"] != identity["start_ticks"]:
                    raise Refused("pid_reused")
                io[pid] = proc_io(pid,self)
            except (OSError, Refused) as exc:
                missing.append({"pid":pid,"error":error_fact(exc,"execution")})
        return io, missing

    def check(self, final=False):
        self._owned()
        now = mono()
        if now > (self.deadline if final else self.reserve_deadline):
            raise Refused("whole_deadline", "terminal" if final else "execution")
        sample = self.sample()
        if any(pid not in self.processes for pid in sample["processes"]):raise Refused("unexpected_unowned_process","execution")
        io, missing = self.sample_processes()
        if missing: raise Refused("actual_io_unknown", "execution", missing)
        actual_read = sum(row["counters"]["rchar"] for row in io.values())
        actual_write = sum(row["counters"]["wchar"] for row in io.values())
        physical_read=sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"]
        physical_write=sample["block_write"]+io[self.owner_pid]["counters"]["write_bytes"]
        # Sum of absolute maxima is conservative over the actual simultaneous
        # aggregate, and includes all parent preparation/import/terminal costs.
        own_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
        upper_peak = sample["memory_peak"]+own_peak
        pending_read = sum(r["reads"] for r in self.pending.values())
        pending_hash = sum(r["hash_bytes"] for r in self.pending.values())
        pending_output = sum(r["output"] for r in self.pending.values())
        physical = max(self.explicit_read, actual_read, physical_read)
        self.last_observed_physical=physical
        transport = max(self.explicit_output, actual_write, physical_write)
        # Memory-hash passes are additive. Kernel rchar does not measure them.
        if physical + self.explicit_hash + pending_read + pending_hash > READ_MAX: raise Refused("aggregate_read")
        if transport + pending_output > OUTPUT_MAX: raise Refused("aggregate_output")
        if upper_peak > RAM_MAX: raise Refused("aggregate_ram")
        if sample["pids_current"]+1 > WORKERS_MAX: raise Refused("worker_cap")
        # SAME original native pool also contains native-before and bootstrap
        # debits. This persists actual absolute maxima, never sums them twice
        # or treats this Source mirror as a second whole allowance.
        self.master_pool.observe(physical,transport,upper_peak,sample["pids_current"]+1)
        # Unknown values are fatal to a successful receipt, never labeled OBSERVED.
        return sample, io, upper_peak

    def _reserve(self, purpose, reads=0, output=0, allocation=0, slots=0, hash_bytes=0, final=False):
        self._owned()
        for value, cap in ((reads,READ_MAX),(output,OUTPUT_MAX),(allocation,RAM_MAX),(slots,SLOTS_MAX),(hash_bytes,READ_MAX)):
            integer(value, cap)
        self.reservation_preview_read=reads;self.reservation_preview_hash=hash_bytes
        try:sample,io,peak=self.check(final)
        finally:self.reservation_preview_read=self.reservation_preview_hash=0
        if self.live_slots+slots > SLOTS_MAX or peak+self.live_alloc+allocation > RAM_MAX:
            raise Refused("capacity_before_effect")
        actual_read=max(self.explicit_read,sum(r["counters"]["rchar"] for r in io.values()),sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"])
        actual_output=max(self.explicit_output,sum(r["counters"]["wchar"] for r in io.values()),sample["block_write"]+io[self.owner_pid]["counters"]["write_bytes"])
        pending_hash=sum(x["hash_bytes"] for x in self.pending.values())
        if actual_read+sum(x["reads"] for x in self.pending.values())+reads+self.explicit_hash+pending_hash+hash_bytes > READ_MAX:
            raise Refused("aggregate_read")
        if actual_output+sum(x["output"] for x in self.pending.values())+output > OUTPUT_MAX:
            raise Refused("aggregate_output")
        token = self.master_pool.reserve(reads,output,hash_bytes,allocation,slots)
        # Native receipt is actual same-role issuance; Source stores a mirror.
        # A mirror allocation failure leaves the original native row pending.
        self.next_token = max(self.next_token,token+1)
        self.pending[token] = {"purpose":purpose,"reads":reads,"output":output,
                               "allocation":allocation,"slots":slots,"hash_bytes":hash_bytes}
        self.live_alloc += allocation; self.live_slots += slots
        self.peak_alloc = max(self.peak_alloc,self.live_alloc)
        self.peak_slots = max(self.peak_slots,self.live_slots)
        return Reservation(self, token,slots)

    def reserve(self, purpose, reads=0, output=0, allocation=0, slots=0, hash_bytes=0):
        return self._reserve(purpose,reads,output,allocation,slots,hash_bytes=hash_bytes)

    def rebudget_terminal_prefix_output(self, required):
        """Before-fork internal reservation adjustment, SAME original pool.

        No Source callback grants credit. _reserve performs actual whole checks.
        Only an UNUSED, zero-FD temporary reservation is transferred; explicit
        cumulative output/read/hash counters are never reduced or refunded.
        Partial failure retains the actual extension object and original row.
        """
        self._owned()
        integer(required, DOCUMENT_MAX)
        credit = self.terminal_hold
        row = self.pending.get(credit.token)
        if credit.closed or row is None:
            raise Refused("prefix_existing_terminal_row", "before-effect")
        addition = max(0, required - row["output"])
        if addition == 0:
            return
        extension = self._reserve("prefix-existing-pool-output-rebudget",
                                  output=addition, final=True)
        self.prefix_reservation_extensions.append(extension)
        # Sole Root owner; no materializing/native effect occurs between release
        # of this unused temporary row and the same-token transfer below.
        # Move UNUSED between two actual native rows before clearing the
        # temporary Source mirror. No effect or cap reset lies between them.
        self.master_pool.transfer_output(extension.token,credit.token,addition)
        if extension.release() is not True:
            raise Refused("prefix_unused_rebudget_transfer_unconfirmed", "before-effect")
        row["output"] += addition
        self.prefix_output_added += addition

    def grow(self,token,allocation=0):
        self._owned();integer(allocation,RAM_MAX)
        row=self.pending.get(token)
        if row is None:raise Refused("retired_reservation")
        _,_,peak=self.check()
        if peak+self.live_alloc+allocation>RAM_MAX:raise Refused("capacity_before_effect")
        self.master_pool.grow(token,allocation)
        row["allocation"]+=allocation;self.live_alloc+=allocation
        self.peak_alloc=max(self.peak_alloc,self.live_alloc)

    def commit(self, token, reads=0, output=0, hash_bytes=0):
        self._owned()
        row = self.pending.get(token)
        if row is None or reads < 0 or output < 0 or hash_bytes < 0 or reads > row["reads"] or output > row["output"] or hash_bytes > row["hash_bytes"]:
            raise Refused("reservation_commit")
        self.master_pool.commit(token,reads,output,hash_bytes)
        row["reads"] -= reads; row["output"] -= output; row["hash_bytes"] -= hash_bytes
        self.explicit_read += reads; self.explicit_output += output; self.explicit_hash += hash_bytes

    def release(self, token):
        """Retirement remains finite under exhaustion and preserves first error."""
        self._owned()
        for book in self.fd_state.journals:
            credit=book.grants.get(token)
            if credit is not None and getattr(credit,'fd_rows',{}):return False
        if token==self.fd_state.credit_token and self.fd_state.journals and not getattr(self,'fd_history_completion_received',False):return False
        for owner in self.local_owners.values():
            body=owner[1]
            if getattr(body,'hold',None) is not None and body.hold.token==token and type(body).__name__=='PreparedFullBody':
                # Native outside end is still CODE. No synthetic release.
                return False
        row = self.pending.get(token)
        if row is None:
            self.note_cleanup("unknown-reservation"); return False
        if self.master_pool.release(token) is not True:return False
        self.pending.pop(token)
        self.live_alloc -= row["allocation"]; self.live_slots -= row["slots"]
        return True

    def retire_slots(self,token):
        self._owned();row=self.pending.get(token)
        if row is None:raise Refused('retired_reservation','terminal')
        self.master_pool.retire_slots(token)
        self.live_slots-=row['slots'];row['slots']=0

    def complete_prefix_publication(self,bank):
        """Actual same-Root endpoint end: SPENT stays; UNUSED credit releases.

        Child local dict is never read here. Full terminal wire/real wait IO/
        both native full body/cut ledgers/actual mapping+FD closes establish
        this narrow end. Retained parent/history bytes remain on their original
        terminal/native custody charges. No ACK/RSS/cumulative-IO refund.
        """
        self._owned()
        completion=bank.parent_completion
        if type(completion) is not tuple or len(completion)!=3:
            raise Refused('publication_full_end_completion_required','terminal')
        receiver,transfer,cost=completion
        roots=receiver.require_publication_transfer(transfer)
        if (bank not in roots[1:3] or cost['bank'] is not bank
                or cost['wire'] is not bank.parent_cost_wire
                or not any(item is cost for item in roots[8])
                or roots[0].observer is not self):
            raise Refused('publication_actual_cost_transfer_identity','terminal')
        for birth,cut,offset,raw in bank.preimage_bindings:
            if (birth.credit is not self.terminal_hold or birth.row['status']!='CLOSED'
                    or not birth.mapping.closed or not birth.closed_confirmed
                    or birth.close_error is not None):
                raise Refused('publication_live_writable_credit_UNCONFIRMED','terminal')
            receiver.require_retired_mapping_state(receiver.retired_mapping_state(birth.mapping))
        credit=bank.publication_credit
        if credit is None or credit.meter is not self or credit.closed:
            raise Refused('publication_actual_existing_credit','terminal')
        row=self.pending.get(credit.token)
        amounts=cost['amounts']
        from owned_prefix_bank import _COST, _COST_DATA, _COST_MAGIC, _COST_UNUSED, _COST_FINAL, _HEAD
        import hashlib
        from selected_owned_values import charge_selected_allocation
        charge_selected_allocation(self.terminal_hold,4096)
        self.terminal_hold.commit(reads=_COST.size,hash_bytes=_COST_DATA.size)
        fields=_COST.unpack(cost['wire'])
        expected=dict(zip(('allocation','reads','hash_bytes','output'),fields[3:7]))
        if fields[1]==_COST_UNUSED:expected['output']+=_HEAD.size
        if (fields[0]!=_COST_MAGIC or fields[1] not in (_COST_UNUSED,_COST_FINAL)
                or fields[1]!=cost['phase'] or fields[2]!=roots[3]
                or hashlib.sha256(cost['wire'][:_COST_DATA.size]).digest()!=fields[7]
                or amounts!=expected):
            raise Refused('publication_full_cost_wire_drift','terminal')
        if (type(row) is not dict or getattr(credit,'fd_rows',{})
                or row['slots']!=0
                or any(type(amounts[key]) is not int or amounts[key]<0
                       or amounts[key]>row[key] for key in ('reads','hash_bytes','output'))):
            raise Refused('publication_actual_component_bound','terminal')
        # Convert only the conservatively reported spent IO to monotonic
        # cumulative counters BEFORE releasing genuinely unused future maxima.
        credit.commit(reads=amounts['reads'],output=amounts['output'],
                      hash_bytes=amounts['hash_bytes'])
        cost['committed_to_original_pool']=True
        if credit.release() is not True:
            raise Refused('publication_unused_credit_release_UNCONFIRMED','terminal')
        cost['unused_future_credit_released']=True
        # No decrement of terminal.selected_value_allocation, full history,
        # Source bodies, native retained snapshots or cumulative IO occurs.
        return completion

    def retain_allocation(self,token,allocation):
        """Retain a proven recipient maximum after confirmed child memory ends.

        This is a same-token lifetime narrowing, never admission or an RSS
        substitution. The caller keeps all escaping recipient arenas charged.
        """
        self._owned();integer(allocation,RAM_MAX)
        row=self.pending.get(token)
        if row is None or allocation>row['allocation']:raise Refused('recipient_allocation_identity','terminal')
        self.master_pool.retain(token,allocation)
        self.live_alloc-=row['allocation']-allocation;row['allocation']=allocation

    def accept_capture(self,capture_id,capture,metadata_owner,metadata_hold):
        self._owned()
        owned=self.owned_results.get(id(metadata_owner))
        if owned is None or owned[0] is not metadata_owner or metadata_hold not in owned[1] or metadata_hold.closed:
            raise Refused('capture_recipient_credit','terminal')
        if metadata_owner.get('capture') is not capture:raise Refused('capture_recipient_alias','terminal')
        if not hasattr(self,'accepted_captures'):self.accepted_captures={}
        if capture_id in self.accepted_captures:raise Refused('capture_duplicate_acceptance','terminal')
        self.accepted_captures[capture_id]=(metadata_owner,capture,metadata_hold)
        return {'status':'ACTUAL_SAME_ROOT_FULL_CAPTURE_ACCEPTED','capture_id':capture_id,
            'owner_pid':self.owner_pid,'metadata_token':metadata_hold.token,
            'raw_stdout_identity':str(id(capture['raw_stdout'])),'raw_stderr_identity':str(id(capture['raw_stderr'])),
            'full_raw_sizes':[len(capture['raw_stdout']),len(capture['raw_stderr'])],
            'hash_status':'FULL_SAME_OBJECT' if set(capture['digests'])=={'stdout','stderr'} else 'UNKNOWN_NOT_ZERO_NOT_PROVEN',
            'ordinary_native_grant_retirement_confirmed':False,'ownership_retired':False}

    def note_partial(self, path, size):
        self.partial.append({"path":path,"written_bytes":size,"effects_possible":True})

    def note_cleanup(self, cause):
        self.cleanup_faults.append(cause)

    def retain_local_owner(self,owner):
        if id(owner) not in self.local_owners:
            self.local_serial+=1;self.local_owners[id(owner)]=(self.local_serial,owner)
        self.publish_local_owners()
        return owner
    def retire_local_owner(self,owner):
        journal=getattr(owner,"fdjournal",owner if isinstance(owner,OwnedFDs) else None)
        if journal is not None and journal.fds:return False
        self.local_owners.pop(id(owner),None);self.publish_local_owners();return True
    def _export_attached_journal(self,journal):
        if journal is None:return None
        if getattr(journal,'state',None) is self.fd_state and hasattr(journal,'rows'):
            if type(journal).__name__=='OwnedFDs':
                rows=[r for r in journal.rows if r.get('journal') is journal]
            else:
                rows=list(journal.rows)
            from lifetime import collection_for
            from common import _history_chunks
            body=collection_for(rows,journal.state,id(journal.rows))
            exported={'collection':body,'journal_count':len(rows),'pending_fds':sorted(journal.fds),'truncated':False}
            if hasattr(journal,'grants'):
                credits=[{'token':c.token,'slots':c.slots,'closed':c.closed} for c in journal.grants.values()]
            elif hasattr(journal,'credit_rows'):
                credits=journal.credit_rows
            else:credits=[]
            faults=getattr(journal,'faults',())
            exported['credit_count']=len(credits);exported['credit_chunks']=_history_chunks(credits)
            exported['fault_count']=len(faults);exported['fault_chunks']=_history_chunks(faults)
            if type(journal).__name__=='OwnedFDs' and journal.state is not None:
                exported['history_arena']=journal.state.credit_token
            if type(journal).__name__=='ForkOwner':
                exported['owner_pid']=journal.pid
                exported['parent_pid']=journal.parent_pid
            return exported
        return journal.graph()

    def local_owner_graph(self):
        result=[]
        for sequence,owner in self.local_owners.values():
            journal=getattr(owner,"fdjournal",owner if hasattr(owner,'rows') and hasattr(owner,'graph') else None)
            result.append({"id":sequence,"class":type(owner).__name__,
                "journal":self._export_attached_journal(journal),
                "pending_holds":[h.token for h in getattr(owner,"pending_holds",())],
                "metadata_hold":getattr(getattr(owner,"metadata_hold",None),"token",None),
                "FD_hold":getattr(getattr(owner,"fd_hold",None),"token",None),
                "rootfd":getattr(owner,"rootfd",getattr(owner,"root_fd",None)),
                "arena_holds":[h.token for h in getattr(owner,'holds',())],
                "alias_edges":[{'field':name,'identity':str(id(value)),'class':type(value).__name__}
                    for name,value in getattr(owner,'__dict__',{}).items()
                    if name not in ('meter','call','consumer')]})
        return result
    def publish_local_owners(self):
        return
    def owned_sample(self,path,maximum):
        # Public-consumer sampling is a separate physical acquisition, not a
        # hash pass. A complete FD/return arena precedes its actual open/read.
        from custody import open_absolute
        hold=self.reserve("consumer-stock-sample-before-open",reads=maximum+1,
            allocation=131072,slots=3)
        book=OwnedFDs(credit=hold,meter=self)
        self.retain_local_owner(book)
        try:
            fd=open_absolute(path,journal=book)
            raw=os.read(fd,maximum+1)
            hold.commit(reads=len(raw))
            return raw
        finally:
            book.close()
            if not book.fds:
                # The caller's complete consumer allocation arena covers the
                # short returned raw/sample-frame alias after the FD lifetime.
                hold.release();self.retire_local_owner(book)
            else:self.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","journal":book.graph()})

    @contextlib.contextmanager
    def owned_file(self,path,maximum,flags):
        hold=self.reserve("consumer-file-FD-before-open",allocation=131072,slots=3)
        book=OwnedFDs(credit=hold,meter=self);self.retain_local_owner(book)
        try:
            fd=open_absolute(path,flags,journal=book)
            yield fd
        finally:
            book.close()
            if not book.fds:hold.release();self.retire_local_owner(book)
            else:self.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","journal":book.graph()})

    def track_held_lease(self,lease):
        self.held_leases[id(lease)]=lease;self.publish_local_owners()
    def untrack_held_lease(self,lease):
        self.held_leases.pop(id(lease),None);self.publish_local_owners()
    def held_lease_graph(self):
        rows=[]
        for lease in self.held_leases.values():
            # Export last known facts and the exact journal without any fresh
            # fstat/read/reserve on an expired terminal path.
            fact={"fd":lease.fd,"path":lease.path,"retained_by":lease.retained_by,
                "reservation_token":lease.hold.token if lease.hold else None,
                "expected_pin":lease.pin,"last_identity9":lease.before,"body_sha256":lease.body_sha,
                "closed":lease.closed,"journal":self._export_attached_journal(lease.fdjournal)}
            rows.append(fact)
        return rows

    def result_owner_rows(self):
        rows=[];values=[]
        for key,(value,holds) in self.owned_results.items():
            values.append(value)
            rows.append({"identity":str(key),"class":type(value).__name__,
                "size":len(value) if type(value) in (bytes,bytearray,str,list,dict,tuple,set,frozenset) else None,
                "reservation_tokens":[h.token for h in holds],"cached_full_immutable_sha256":self.owned_digests.get(key),
                "immutable_bytes":type(value) is bytes})
        return rows,values

    def result_owner_graph(self):
        rows,values=self.result_owner_rows()
        return {"count":len(rows),"chunks":[rows[i:i+512] for i in range(0,len(rows),512)],
            'actual_values':full_value_arena(values,body_carrier=getattr(self,'result_body_carrier',None)),"truncated":False}

    def complete_owner_graph(self):
        locals=self.local_owner_graph();held=self.held_lease_graph()
        result_rows,result_values=self.result_owner_rows()
        local_values=[owner for _,owner in self.local_owners.values()]
        held_values=list(self.held_leases.values())
        errors=list(getattr(self,'error_arenas',()))
        arena=full_value_arena(local_values+held_values+result_values+errors,history_states=(self.fd_state,),body_carrier=getattr(self,'owner_body_carrier',None))
        roots=arena['roots'];n_local=len(local_values);n_held=len(held_values);n_result=len(result_values)
        return {"schema":"friday.a181.complete-actor-owner-graph.v1",
            "owner_pid":os.getpid(),"local_count":len(locals),"local_chunks":[locals[i:i+512] for i in range(0,len(locals),512)],
            "held_count":len(held),"held_chunks":[held[i:i+512] for i in range(0,len(held),512)],
            "results":{"count":len(result_rows),"chunks":[result_rows[i:i+512] for i in range(0,len(result_rows),512)],"truncated":False},
            "FD_domain":{"domain_identity":str(id(self.fd_state))},"value_arena":arena,
            "local_root_indexes":roots[:n_local],"held_root_indexes":roots[n_local:n_local+n_held],
            "result_root_indexes":roots[n_local+n_held:n_local+n_held+n_result],
            "error_root_indexes":roots[n_local+n_held+n_result:],"truncated":False}

    def retain_error_arena(self,exc):
        # Exception, traceback frames, locals and chained causes remain actual
        # strongly held objects in the preadmitted owner completion arena.
        if not hasattr(self,"error_arenas"):self.error_arenas=[]
        if all(prior is not exc for prior in self.error_arenas):self.error_arenas.append(exc)
        carrier=getattr(self,'owner_body_carrier',None)
        preown_secondary_selected((exc, exc.__traceback__), _SOURCE_OBJECT_MODULES, carrier)
        self.secondary_own_cuts=secondary_own_records()
        consume_secondary_own_cut(self.secondary_own_cuts, carrier)
        self.publish_local_owners()
        return exc

    def reservation_graph(self):
        # Complete, bounded nesting rather than a >512-key JSON map. These
        # are reservation identities, NOT the original ABI128 process list.
        rows=[{"token":str(k),**dict(v)} for k,v in self.pending.items()]
        if len(rows)>512*512:raise Refused("complete_reservation_graph_capacity","terminal")
        return {"schema":"friday.sol053.complete-reservation-graph.v1","complete_count":len(rows),
            "chunks":[rows[i:i+512] for i in range(0,len(rows),512)],"truncated":False}

    def own_result(self,value,hold):
        key=id(value)
        previous=self.owned_results.get(key)
        if previous is not None:
            if previous[0] is not value:raise Refused("return_owner_alias")
            previous[1].append(hold)
        else:self.owned_results[key]=(value,[hold])
        self.publish_local_owners()
        return value

    def retire_result(self,value):
        key=id(value)
        row=self.owned_results.get(key)
        if row is not None:
            if row[0] is not value:raise Refused("return_owner_identity")
        value=None;row=None
        self.retire_result_id(key)

    def retire_result_id(self,key):
        # Caller has first dropped every alias, passing only the object identity.
        row=self.owned_results.get(key)
        if row is None:return True
        if any(getattr(h,'fd_rows',{}) for h in row[1]):return False
        for hold in row[1]:
            if hold.release() is not True:return False
        self.owned_digests.pop(key,None);self.owned_results.pop(key,None)
        return True

    def cached_digest(self, raw):
        row = self.owned_results.get(id(raw))
        if row is None or row[0] is not raw:
            return None
        return self.owned_digests.get(id(raw))

    def remember_digest(self, raw, digest):
        row = self.owned_results.get(id(raw))
        if row is None or row[0] is not raw:
            return
        prior = self.owned_digests.get(id(raw))
        if prior is None:
            self.owned_digests[id(raw)] = digest
        elif prior != digest:
            raise Refused("owned_digest_drift")

    def retire_results(self):
        # Caller first discards every escaping parsed/body alias. This arena
        # is NEVER retired while a child or unconfirmed owner can use it.
        keys=list(self.owned_results)
        for key in keys:
            if self.retire_result_id(key) is not True:return False
        return True

    def before_reap(self, pid):
        """Read zombie/held-finish IO before direct wait4 removes /proc evidence."""
        self._owned()
        try:
            if proc_start(pid,self)["start_ticks"] != self.processes[pid]["start_ticks"]:
                raise Refused("pid_reused")
            self.final_io[pid] = proc_io(pid,self)
        except BaseException as exc:
            self.errors.append(error_fact(exc,"terminal"))
            raise Refused("final_actual_io_unknown","terminal") from exc

    def record_wait4(self, pid, status, usage):
        self.waits.append({"pid":pid,"raw_wait_status":status,
            "exit_code":os.waitstatus_to_exitcode(status),
            "rusage":{name:(str(getattr(usage,name)) if type(getattr(usage,name)) is float else getattr(usage,name)) for name in (
                "ru_utime","ru_stime","ru_maxrss","ru_ixrss","ru_idrss","ru_isrss",
                "ru_minflt","ru_majflt","ru_nswap","ru_inblock","ru_oublock",
                "ru_msgsnd","ru_msgrcv","ru_nsignals","ru_nvcsw","ru_nivcsw")}})

    def receipt(self, final=False):
        sample, io, peak = self.check(final)
        return {"observer_id":self.observer_id,"scope":"producer-and-all-helpers",
            "processes":sorted(self.processes),"peak_ram_bytes":peak,
            "read_bytes":max(self.explicit_read,sum(r["counters"]["rchar"] for r in io.values()),sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"])+self.explicit_hash,
            "physical_read_bytes":max(self.explicit_read,sum(r["counters"]["rchar"] for r in io.values()),sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"]),
            "memory_hash_bytes":self.explicit_hash,
            "output_bytes":max(self.explicit_output,sum(r["counters"]["wchar"] for r in io.values()),sample["block_write"]+io[self.owner_pid]["counters"]["write_bytes"]),
            "transport_output_bytes":self.explicit_output,
            "implicit_io_status":"OBSERVED","raw_kernel":sample,"raw_process_io":{str(k):v for k,v in io.items()},
            "direct_wait4":list(self.waits),"explicit_read_hash_debits":self.explicit_hash,
            "explicit_write_transport_debits":self.explicit_output,
            "pending_reservations":self.reservation_graph(),"reservations_are_observations":False,
            "absolute_inherited_peak_subtracted":False,"partial_writes":list(self.partial),
            "cleanup_faults":list(self.cleanup_faults),"errors":list(self.errors)}

    def a128_aggregate(self):
        r = self.receipt()
        return {k:r[k] for k in ("observer_id","scope","processes","peak_ram_bytes",
                                 "read_bytes","output_bytes","implicit_io_status")}


INPUT_MAX_TERMINAL = 2_000_000


class MeterRPC:
    """Actor accounting channel is owned/created by Root; it grants no authority."""
    def __init__(self, call, owner_export_token=None, terminal_call=None):
        self.call = call
        self.owned_results={}
        self.owned_digests={}
        self.held_leases={}
        self.local_owners={};self.local_serial=0;self.local_sequence=0;self.owner_publication_error=None
        self.owner_export_token=owner_export_token;self.terminal_call=terminal_call
        self.fd_state=FDState(owner_export_token)
        self.owner_dirty=False;self.owner_export_attempted=False;self.owner_export_accepted=False
    own_result=RootObserver.own_result
    cached_digest=RootObserver.cached_digest
    remember_digest=RootObserver.remember_digest
    retire_result=RootObserver.retire_result
    retire_result_id=RootObserver.retire_result_id
    retire_results=RootObserver.retire_results
    track_held_lease=RootObserver.track_held_lease
    untrack_held_lease=RootObserver.untrack_held_lease
    retain_local_owner=RootObserver.retain_local_owner
    retire_local_owner=RootObserver.retire_local_owner
    _export_attached_journal=RootObserver._export_attached_journal
    local_owner_graph=RootObserver.local_owner_graph
    held_lease_graph=RootObserver.held_lease_graph
    result_owner_rows=RootObserver.result_owner_rows
    result_owner_graph=RootObserver.result_owner_graph
    complete_owner_graph=RootObserver.complete_owner_graph
    retain_error_arena=RootObserver.retain_error_arena
    owned_sample=RootObserver.owned_sample
    owned_file=RootObserver.owned_file
    def publish_local_owners(self,terminal=False):
        # Local strong ownership is updated without ordinary RPC allocation.
        # The complete prepaid export has one finite explicit terminal attempt.
        self.owner_dirty=True
        if not terminal:return
        if self.owner_export_attempted:raise Refused("actor_owner_export_already_attempted","terminal")
        if self.owner_export_token is None or self.terminal_call is None:raise Refused("actor_owner_export_not_preadmitted","terminal")
        self.owner_export_attempted=True
        self.local_sequence+=1
        try:
            result=self.terminal_call({"type":"actor-owner-state","sequence":self.local_sequence,
                "owner_pid":os.getpid(),"graph":self.complete_owner_graph(),"prepaid_token":self.owner_export_token})
            if result!={"retained":True,"sequence":self.local_sequence,"complete":True}:raise Refused("actor_owner_acceptance","terminal")
            self.owner_export_accepted=True;self.owner_dirty=False
        except BaseException as exc:
            self.owner_publication_error=error_fact(exc,"terminal")
            # Exact live journals remain strongly in this actor. Failed delivery
            # does not confirm retirement or shrink any owning reservation.
            raise
    def reserve(self, purpose, reads=0, output=0, allocation=0, slots=0, hash_bytes=0):
        reply = self.call({"type":"reserve","purpose":purpose,"reads":reads,
                          "output":output,"allocation":allocation,"slots":slots,"hash_bytes":hash_bytes})
        return Reservation(self, reply["token"],slots)
    def commit(self, token, reads=0, output=0, hash_bytes=0):
        self.call({"type":"commit","token":token,"reads":reads,"output":output,"hash_bytes":hash_bytes})
    def grow(self,token,allocation=0):
        self.call({"type":"grow","token":token,"allocation":allocation})
    def release(self, token):
        try:
            response=(self.terminal_call or self.call)({"type":"release","token":token})
            return response.get('released') is True
        except BaseException as exc:
            self.owner_publication_error=error_fact(exc,'terminal')
            return False
    def note_partial(self, path, size):
        self.call({"type":"partial","path":path,"size":size})
    def note_cleanup(self, cause):
        try: (self.terminal_call or self.call)({"type":"cleanup","cause":cause})
        except BaseException: pass

class FinalArena:
    """One explicit existing-Root completion arena, including error frames.

    It is a lifecycle receiver, never a Root role or admission authority. The
    native caller consumes the actual strongly held graph in a finite callback
    before known arenas can be released. Foreign escaping aliases still need
    independent whole qualification; this API does not manufacture that fact.
    """
    def __init__(self,owner):
        self.owner=owner;self.owner_pid=os.getpid();self.generation=0
        self.roots={};self.completed=False;self.attempted=False
        self.receipt=None;self.receipt_owner=None
        self.receipt_candidate=None
        self.error_roots=[]
    def capture(self):
        if os.getpid()!=self.owner_pid:raise Refused('final_arena_owner','terminal')
        if self.completed:
            self.receipt.require_completed(self)
            return {'schema':'friday.a181.existing-root-final-arena.v1','owner_pid':self.owner_pid,
                'generation':self.generation,'roots':[],'completed':True,'ownership_retired':False}
        self.generation+=1
        self.roots={name:value for name,value in self.owner.__dict__.items() if name!='final_arena'}
        return {'schema':'friday.a181.existing-root-final-arena.v1','owner_pid':self.owner_pid,
            'generation':self.generation,'roots':[{'field':name,'identity':str(id(value)),
                'class':type(value).__name__} for name,value in self.roots.items()],
            'completed':self.completed,'ownership_retired':False}
    def consume(self,consumer):
        if os.getpid()!=self.owner_pid or self.attempted or not callable(consumer):raise Refused('final_arena_receiver','terminal')
        self.attempted=True
        # Full raw captures, original exceptions/tracebacks and metadata remain
        # strong here. No fresh ordinary reservation or decoder is required.
        try:
            self.capture()
            carrier=getattr(self.owner,'final_body_carrier',None)
            if carrier is None:raise Refused('final_prepared_body_prefix_CODE','terminal')
            self.physical_body=full_value_arena([self.roots],body_carrier=carrier)
            validate_full_value_arena(self.physical_body,carrier)
            envelope={'schema':'friday.sol086.native-full-completion.v2',
                'owner_pid':self.owner_pid,'generation':self.generation,'body':self.physical_body}
            document=self.owner.final_document_carrier
            from common import encoded_bound
            document.prospective(encoded_bound(envelope))
            self.physical_document=canonical(envelope,maximum=INPUT_MAX)
            document.add(self.physical_document);document.finish()
            if document.require(document.descriptor())!=self.physical_document:
                raise Refused('native_completion_actual_document_reader','terminal')
            self.physical_record=document.record
            # Consumer gets exact same strong roots and this pre-loss complete
            # physical document/reader through the existing arena. It must still
            # supply real native qualification; no constructor/flags mint it.
            result=consumer(self.roots)
            # A real returned receiver/receipt is retained even if its
            # correspondence/qualification check fails; no completion credit.
            self.receipt_candidate=result
            if not isinstance(result,NativeCompletionReceipt):
                raise Refused('actual_native_completion_receipt_required','terminal')
            result.require(self,self.roots)
        except BaseException as exc:
            self.error_roots.append(exc)
            observer=getattr(self.owner,'observer',None)
            if observer is not None:
                try:observer.retain_error_arena(exc)
                except BaseException as retention_error:
                    self.error_roots.append(retention_error)
            else:
                if not hasattr(self.owner,'prefix_error_arenas'):self.owner.prefix_error_arenas=[]
                self.owner.prefix_error_arenas.append(exc)
            raise
        # A descriptive token or arbitrary callback returning None cannot
        # complete an actual value/body/alias receiver. The existing native
        # owner must retain a full receipt and the exact actual roots itself.
        # Native enrollment/cross-language durable qualification is still a
        # separate obligation; this Source callback does not mint that fact.
        self.receipt=result;self.receipt_owner=result.receiver
        # Detach this local dictionary, do not clear a receiver's possible
        # same-object roots alias. Receipt/receiver still own every full value.
        self.roots={};self.completed=True
    def require_complete(self,owner):
        if owner is not self.owner or os.getpid()!=self.owner_pid or not self.completed or self.roots or self.receipt is None:
            raise Refused('complete_native_caller_arena_required','terminal')
        self.receipt.require_completed(self)
    def detach_confirmed(self):
        if not self.completed or self.roots:raise Refused('final_arena_unconfirmed','terminal')
        receipt=self.receipt
        if receipt is None:raise Refused('native_actual_transfer_receipt','terminal')
        receipt.receiver.require_transfer(receipt)
        # Accepted same-existing-parent transfer is not its final outside end.
        # Native parent keeps receipt/roots/pin/qualifier and full original errors.
        self.owner=None;self.receipt=None;self.receipt_owner=None;self.receipt_candidate=None
        self.error_roots=[]

class NativeCompletionReceipt:
    """Exact Source-side receipt correspondence, never external authority.

    Qualification of receiver/retained receipt against the independently
    enrolled native tool is REQUIRED before this protocol can be admitted.
    A Python object alone cannot establish native durability or enrollment.
    """
    def __init__(self,receiver,arena,roots,durable_pin,qualification):
        self.receiver=receiver;self.arena=arena;self.owner_pid=os.getpid()
        self.generation=arena.generation;self.roots=roots.copy()
        self.durable_pin=durable_pin;self.qualification=qualification
        self.confirmed=False
        self.source_detached=False
    def require(self,arena,roots):
        from common import exact,sha,canonical,parse,INPUT_MAX
        from custody import identity9,open_absolute
        owner=arena.owner
        if self.arena is not arena or self.owner_pid!=os.getpid() or self.generation!=arena.generation:
            raise Refused('native_completion_identity','terminal')
        if owner.qualification is None or self.qualification is not owner.qualification or owner.root_fact is None:
            raise Refused('enrolled_native_full_completion_required','terminal')
        if getattr(self.receiver,'owner_pid',None)!=self.owner_pid or getattr(self.receiver,'root_fact',None)!=owner.root_fact:
            raise Refused('native_completion_existing_root','terminal')
        accepted=getattr(self.receiver,'accepted_source_roots',None)
        if type(accepted) is not dict or set(accepted)!=set(roots) or any(accepted[name] is not value for name,value in roots.items()):
            raise Refused('native_completion_actual_roots','terminal')
        if set(self.roots)!=set(roots) or any(self.roots[name] is not value for name,value in roots.items()):
            raise Refused('native_completion_root_drift','terminal')
        body=arena.physical_body
        carrier=owner.final_body_carrier
        validate_full_value_arena(body,carrier)
        carrier.match_roots(body['physical_body'])
        raw=arena.physical_document
        expected=canonical({'schema':'friday.sol086.native-full-completion.v2','owner_pid':self.owner_pid,
            'generation':self.generation,'body':body},maximum=INPUT_MAX)
        if raw!=expected or self.durable_pin!=arena.physical_record['pin']:
            raise Refused('native_completion_actual_physical_document','terminal')
        pin=exact(self.durable_pin,('path','bytes','sha256','identity9_decimal_strings'),'native_completion_pin')
        owner.check_owned_path(pin['path'])
        if pin['bytes']!=len(raw) or pin['sha256']!=sha(raw,admitted=owner.observer.terminal_hold):
            raise Refused('native_completion_complete_body_pin','terminal')
        document=owner.final_document_carrier
        if document.record['pin']!=pin or document.require(document.descriptor())!=raw:
            raise Refused('native_completion_prepared_document_reader','terminal')
        fd=document.held.fd
        try:
            if identity9(os.fstat(fd))!=pin['identity9_decimal_strings']:raise Refused('native_completion_pin_drift','terminal')
            at=0
            while at<len(raw):
                part=os.pread(fd,min(65536,len(raw)-at),at)
                owner.observer.terminal_hold.commit(reads=len(part))
                if not part or part!=raw[at:at+len(part)]:raise Refused('native_completion_complete_body','terminal')
                at+=len(part)
            if identity9(os.fstat(fd))!=pin['identity9_decimal_strings'] or identity9(os.lstat(pin['path']))!=pin['identity9_decimal_strings']:
                raise Refused('native_completion_pin_drift','terminal')
        finally:document.held.check()
        # The already prepared full Held FD/credit remains strong through the
        # actual outside end. No fresh open or synthetic once-close acceptance.
        # Complete verified durable value preimages, not the graph's labels,
        # allow the caller's exact same-owner roots to be dropped once.
        # Verification happened BEFORE last Source retirement/close effects.
        # Preserve actual mutable cleanup/error lists through those effects;
        # a pre-retirement body pin is not the later native final-end body.
        self.confirmed=True
    def require_completed(self,arena):
        if not self.confirmed or self.arena is not arena or self.owner_pid!=os.getpid() or self.generation!=arena.generation:
            raise Refused('native_completion_not_confirmed','terminal')
        accepted=getattr(self.receiver,'accepted_source_roots',None)
        if type(accepted) is not dict or set(accepted)!=set(self.roots) or any(accepted[name] is not value for name,value in self.roots.items()):
            raise Refused('native_completion_custody_lost_before_end','terminal')

def _receipt_outside_end(self):
    return self.receiver is not None and self.receiver.outside_end_confirmed is True
NativeCompletionReceipt.outside_end_confirmed=property(_receipt_outside_end)
