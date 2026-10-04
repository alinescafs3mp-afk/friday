"""Content binding of code/defaults/closures and nested Source state.

Actual selected native/loader/kernel observations are supplied independently.
No id-only check or this producer's own digest issues their expected identity.
"""
import hashlib
import json
import marshal
import re
import types
import weakref
import _thread
import functools

_RLOCK_TYPE=type(_thread.RLock())


def state_digest(value,native_cache_policy=None):
    seen={}
    def visit(item):
        if item is None or type(item) in (bool,int,str): return [type(item).__name__,item]
        if isinstance(item,bytes): return ['bytes',item.hex()]
        if isinstance(item,float): return ['float',item.hex()]
        if isinstance(item,types.ModuleType):
            # The COMPLETE module's code/state/native/loaded facts are consumed
            # by bind_native separately, not replaced with module identity.
            return ['module-edge',item.__name__]
        identity=id(item)
        if identity in seen: return ['alias',seen[identity]]
        ordinal=len(seen);seen[identity]=ordinal
        if isinstance(item,types.FunctionType):
            return ['function',ordinal,item.__module__,item.__qualname__,
                marshal.dumps(item.__code__).hex(),visit(item.__defaults__),
                visit(item.__kwdefaults__),visit(item.__annotations__),visit(item.__dict__),
                [visit(cell.cell_contents) for cell in item.__closure__ or ()]]
        if isinstance(item,types.MethodType):return ['bound-method',visit(item.__func__),visit(item.__self__)]
        if isinstance(item,weakref.ReferenceType):return ['weakref',visit(item())]
        if isinstance(item,_thread.LockType):return ['native-lock',item.locked()]
        if isinstance(item,_RLOCK_TYPE):return ['native-rlock',item._is_owned(),item._recursion_count()]
        if isinstance(item,functools._lru_cache_wrapper):
            # Counts are NOT full hidden C key/result proof. The independently
            # selected owner can normalize this internal performance cache to
            # exact empty state before effects, with wrapped semantics pinned.
            # Clear retires only cache references; live caller aliases remain
            # charged by actual allocator frees, and global IO/work counters
            # are never reset. Without that selected policy, refuse safely.
            info=item.cache_info()
            if info.currsize:
                if native_cache_policy!='owned-empty-before-effects':raise ValueError('unbound nonempty native LRU cache')
                item.cache_clear()
                if item.cache_info().currsize:raise ValueError('native LRU state not exclusively owned')
            return ['empty-native-lru',visit(vars(item)),visit(item.cache_parameters())]
        if isinstance(item,(staticmethod,classmethod)): return [type(item).__name__,visit(item.__func__)]
        if isinstance(item,property): return ['property',visit(item.fget),visit(item.fset),visit(item.fdel)]
        if isinstance(item,types.CodeType): return ['code',marshal.dumps(item).hex()]
        if isinstance(item,re.Pattern): return ['compiled-pattern',item.pattern,item.flags,item.groups,visit(dict(item.groupindex))]
        if isinstance(item,dict):
            # Dict order is part of state; do not silently canonical-sort away
            # an in-place ordered-state mutation used by a semantic consumer.
            return ['dict',ordinal,[[visit(key),visit(value)] for key,value in item.items()]]
        if isinstance(item,(list,tuple)):
            return [type(item).__name__,ordinal,[visit(value) for value in item]]
        if isinstance(item,(set,frozenset)):
            values=sorted(item,key=lambda value:json.dumps(visit_order(value),sort_keys=True,separators=(',',':')))
            return [type(item).__name__,ordinal,[visit(value) for value in values]]
        if isinstance(item,type):
            if item.__module__=='builtins': return ['native-type',item.__module__,item.__qualname__]
            namespace={key:value for key,value in vars(item).items() if key not in ('__dict__','__weakref__','_abc_impl')}
            if '_abc_impl' in vars(item):
                import _abc
                registry,positive,negative,version=_abc._get_dump(item)
                if any(not isinstance(value,weakref.ReferenceType) or value() is not None and not isinstance(value(),type)
                       for values in (registry,positive,negative) for value in values):raise ValueError('unbound ABC cache key')
                # Mutable positive/negative caches are owned by the selected
                # native ABC API. The semantic registry is NEVER exempted.
                registry_classes=sorted((value() for value in registry if value() is not None),key=lambda value:(value.__module__,value.__qualname__))
                labels=[(value.__module__,value.__qualname__) for value in registry_classes]
                if len(set(labels))!=len(labels):raise ValueError('ambiguous ABC registry class identity')
                namespace['independent_abc_registry']=[visit(value) for value in registry_classes]
            return ['class',ordinal,item.__module__,item.__qualname__,
                [(base.__module__,base.__qualname__) for base in item.__bases__],visit(namespace)]
        if isinstance(item,(types.BuiltinFunctionType,types.MethodDescriptorType,types.WrapperDescriptorType,types.GetSetDescriptorType,types.MemberDescriptorType)):
            return ['native-api',type(item).__name__,getattr(item,'__module__',None),
                getattr(item,'__qualname__',getattr(item,'__name__',None)),getattr(item,'__text_signature__',None)]
        if hasattr(item,'__dict__'):
            return ['instance',ordinal,visit(type(item)),visit(vars(item))]
        raise ValueError('unbound transitive state type: '+type(item).__module__+'.'+type(item).__qualname__)
    def visit_order(item):
        if item is None or type(item) in (bool,int,str,float): return [type(item).__name__,repr(item)]
        if isinstance(item,tuple): return ['tuple',[visit_order(value) for value in item]]
        if isinstance(item,bytes): return ['bytes',item.hex()]
        raise ValueError('unbound set order type')
    raw=json.dumps(visit(value),sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    return hashlib.sha256(raw).hexdigest()


def source_state_cause(modules,expected):
    if not isinstance(expected,dict) or set(expected)!=set(modules): return 'ingress_context_unbound'
    for name,module in modules.items():
        actual={key:value for key,value in vars(module).items() if not key.startswith('__')}
        chosen=expected[name]
        if not isinstance(chosen,dict) or set(chosen)!=set(actual): return 'ingress_context_unbound'
        for key,value in actual.items():
            if state_digest(value)!=chosen[key]: return 'ingress_context_unbound'
    return None


def bind_native(contract,resources,meter):
    """Consume complete independently issued image/ABI/allocation/state relation.

    The immutable native session binds the whole contract digest; declarations
    without that actual Root-issued relation have no code/authority credit.
    Every producer family remains covered, including startup and all errors.
    """
    import sys
    from tools.native_support import selected_owner
    from source.custody import owned_metadata_bytes
    required={'schema','allocation_producers','fd_producers','io_producers',
        'runtime_modules','native_apis','kernel_contract','loader_contract','terminal_contract'}
    if not isinstance(contract,dict) or set(contract)!=required or contract['schema']!='friday.scanner.native-transitive-owner.v1':
        return 'ingress_context_unbound'
    native=selected_owner()
    if native is None: return 'ingress_context_unbound'
    raw=(json.dumps(contract,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
    if hashlib.sha256(raw).hexdigest()!=native.snapshot()['binding_sha256']:
        return 'ingress_context_unbound'
    names={'__init__','bounds','broker_map','canonical','causes','compression','context','contracts',
        'controls','custody','deb_archive','digests','filename','fixtures','guards','normalize','pins',
        'public','raw_relations','schema_validate','semantics','zip_wheel'}
    families=names|{'CPython.preinit','CPython.raw','CPython.mem','CPython.obj','CPython.arena',
        'stdlib.json','stdlib.re','stdlib.csv','stdlib.unicode','stdlib.marshal',
        'native.loader','native.libc','native.deflate','native.gzip','native.xz','native.zstd',
        'native.sha256','native.sha384','native.sha512','native.crc32',
        'returned.graphs','traceback.frames','terminal.all_errors'}
    if set(contract['allocation_producers'])!=families or any(value!='native.friday_malloc-beforecreation/free-at-destruction' for value in contract['allocation_producers'].values()):
        return 'ingress_context_unbound'
    fd_families={'inherited','open','openat','socket','socketpair','pipe','pipe2','dup','dup2','dup3',
        'fcntl.duplicate','accept','accept4','recvmsg.rights','epoll','clone3.pidfd','close.uncertainty'}
    if set(contract['fd_producers'])!=fd_families or any(value!='native.beforecreation-detachonce-ledger' for value in contract['fd_producers'].values()):
        return 'ingress_context_unbound'
    if set(contract['io_producers'])!={'read','pread','recvmsg','stdio','send','sendmsg','write','pipe'}:
        return 'ingress_context_unbound'
    if any(value!='native.reserved-and-actual-original-domain' for value in contract['io_producers'].values()):
        return 'ingress_context_unbound'
    if set(contract['native_apis'])!={'allocator','descriptor','blocking_deadline','crypto','codec','immutable_state','entry','terminal'}:
        return 'ingress_context_unbound'
    for record in contract['native_apis'].values():
        if set(record)!={'implementation_sha256','full_loaded_relation_sha256','api_state_sha256'}:
            return 'ingress_context_unbound'
        if any(type(value) is not str or len(value)!=64 or any(char not in '0123456789abcdef' for char in value) for value in record.values()):
            return 'ingress_context_unbound'
    for family in ('kernel_contract','loader_contract','terminal_contract'):
        if not isinstance(contract[family],dict) or contract[family].get('independently_selected_full_relation') is not True:
            return 'ingress_context_unbound'
    if contract['terminal_contract'].get('before_preinitialization') is not True or contract['terminal_contract'].get('full_origin_error_detail') is not True:
        return 'ingress_context_unbound'
    if set(contract['runtime_modules'])!=set(sys.modules):
        return 'ingress_context_unbound'
    for name,record in contract['runtime_modules'].items():
        module=sys.modules.get(name)
        if not isinstance(module,types.ModuleType): return 'ingress_context_unbound'
        if set(record)!={'path','identity9','sha256','immutable_state_sha256','owned_state'}:
            return 'ingress_context_unbound'
        if record['path'] is not None:
            if getattr(module,'__file__',None)!=record['path']: return 'ingress_context_unbound'
            body,pin,cause=owned_metadata_bytes(record['path'],resources['ceilings']['max_read_bytes'],resources,meter)
            if cause: return cause
            if pin['sha256']!=record['sha256'] or [str(v) for v in pin['identity9']]!=record['identity9']:
                return 'ingress_context_unbound'
            body=None
        namespace=vars(module)
        owned=record['owned_state']
        if not isinstance(owned,dict) or not set(owned)<=set(namespace): return 'ingress_context_unbound'
        projection={key:value for key,value in namespace.items() if key not in owned}
        policy=contract['loader_contract'].get('native_cache_policy')
        if state_digest(projection,policy)!=record['immutable_state_sha256']: return 'ingress_context_unbound'
        for key,kind in owned.items():
            # The sole normal mutable regex cache has a complete typed consumer.
            # Other mutable fields require their own actual selected owner code.
            cache=namespace[key]
            if name=='re' and key in ('_cache','_cache2') and kind=='typed-regexp-cache' and isinstance(cache,dict):
                for cache_key,pattern in cache.items():
                    if (not isinstance(cache_key,tuple) or len(cache_key)!=3 or cache_key[0] not in (str,bytes) or
                        not isinstance(pattern,re.Pattern) or pattern.pattern!=cache_key[1] or
                        (pattern.flags&cache_key[2])!=cache_key[2]):return 'ingress_context_unbound'
            elif name=='threading' and kind=='single-native-entry-thread-state':
                import threading
                current=threading.current_thread()
                if current is not threading.main_thread() or current.ident!=threading.get_ident():return 'ingress_context_unbound'
                if key=='_active' and (not isinstance(cache,dict) or set(cache)!={current.ident} or cache[current.ident] is not current):return 'ingress_context_unbound'
                elif key=='_main_thread' and cache is not current:return 'ingress_context_unbound'
                elif key=='_limbo' and cache!={}:return 'ingress_context_unbound'
                elif key=='_dangling' and set(cache)!={current}:return 'ingress_context_unbound'
                elif key=='_shutdown_locks' and any(not isinstance(lock,_thread.LockType) for lock in cache):return 'ingress_context_unbound'
                elif key not in ('_active','_main_thread','_limbo','_dangling','_shutdown_locks'):return 'ingress_context_unbound'
                # Exempt the current entry's PID/TID/cache only, never Thread
                # implementation/default/closure/class code or other modules.
                if type(current).__module__!='threading' or type(current).__qualname__!='_MainThread':return 'ingress_context_unbound'
                live_keys={'_ident','_native_id','_handle','_tstate_lock','_started','_stderr'}
                immutable={field:value for field,value in vars(current).items() if field not in live_keys}
                expected=contract['loader_contract'].get('entry_thread_immutable_state_sha256')
                if state_digest(immutable,policy)!=expected:return 'ingress_context_unbound'
                if getattr(current,'native_id',None)!=native.snapshot()['owner_pid']:return 'ingress_context_unbound'
                handle=getattr(current,'_handle',None)
                if handle is not None and handle.is_done():return 'ingress_context_unbound'
                lock=getattr(current,'_tstate_lock',None)
                if lock is not None and (not isinstance(lock,_thread.LockType) or not lock.locked()):return 'ingress_context_unbound'
                if not isinstance(current._started,threading.Event) or not current._started.is_set():return 'ingress_context_unbound'
                if current._stderr is not sys.stderr or sys.stderr.fileno()!=2:return 'ingress_context_unbound'
            elif name=='_frozen_importlib' and key in ('_module_locks','_blocking_on') and kind=='closed-import-lock-state':
                if cache!={}:return 'ingress_context_unbound'
            elif name=='tools.native_support' and key=='_source_raw_scope' and kind=='prospective-original-source-raw-owner.v1':
                from tools.native_support import source_raw_state_matches
                if not source_raw_state_matches(cache,native):return 'ingress_context_unbound'
            else:return 'ingress_context_unbound'
    # Exact native module set is independently selected; a newly loaded module
    # cannot silently acquire an allocation/API/state exemption.
    meter['native_transitive_contract']=contract
    return None
