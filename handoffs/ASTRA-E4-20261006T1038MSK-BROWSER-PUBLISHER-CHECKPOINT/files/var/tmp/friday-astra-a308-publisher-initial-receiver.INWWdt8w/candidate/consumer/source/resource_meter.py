"""One call-owned resource envelope. No approval, effect or process creation.

Externally fixed absolute deadlines can only tighten the bootstrap envelope.
RSS and every codec/hash/read debit are observations, never zero by omission.
The caller's resident inputs are included in observed RSS, not subtracted.
"""
import contextvars
import hashlib as _hashlib
import json
import os
import resource
import time
from contract import ContractError, MAX_WHOLE_READ_BYTES, MAX_OUTPUT_BYTES

# Standalone/fork shadow keeps stock objects and stock effects. Only the
# existing held Root loader installs its native own producer before exec.
_SOL105_OWN_NATIVE=globals().get('_SOL105_OWN_NATIVE')
_SOL105_OWN_PID=globals().get('_SOL105_OWN_PID')
_CONTEXT_BIRTHS=[]
_CONTEXT_TRANSITIONS=[]
_HASH_ORIGINS=[]

def _own_native():
    return _SOL105_OWN_NATIVE if os.getpid()==_SOL105_OWN_PID else None

def own_context_var(name,default=contextvars.Token.MISSING):
    native=_own_native()
    if native is not None:native.own_prepare(131072)
    has_default=default is not contextvars.Token.MISSING
    row={'schema':'friday.sol105.own-context-birth.v1','var':None,'name':name,
        'has_default':has_default,'default':default if has_default else None,
        'attempted':True,'confirmed':False,'original_error':None}
    _CONTEXT_BIRTHS.append(row)
    native=_own_native()
    try:
        value=(native.own_context_var(name,has_default,row['default']) if native is not None
            else contextvars.ContextVar(name,default=default) if has_default else contextvars.ContextVar(name))
        row['var']=value;row['confirmed']=True
        return value
    except BaseException as error:
        row['original_error']=error
        raise

def own_context_set(var,value,transition):
    native=_own_native()
    if native is not None:native.own_prepare(131072)
    transition['actual_context']=None;transition['used']=False
    _CONTEXT_TRANSITIONS.append(transition)
    native=_own_native()
    token=native.own_context_set(var,value,transition) if native is not None else var.set(value)
    transition['token']=token
    # Actual context was published by native BEFORE the setter, not obtained
    # through a post-effect body factory which could strand a successful token.
    return token

def own_context_reset(var,token):
    rows=[row for row in _CONTEXT_TRANSITIONS if row['token'] is token and row['var'] is var]
    if len(rows)!=1:raise ContractError('own_context_reset_actual_transition')
    row=rows[0];row['reset_attempted']=True
    native=_own_native()
    try:
        if native is None:var.reset(token)
        else:native.own_context_reset(var,token)
        row['reset_confirmed']=True;row['used']=True
    except BaseException as error:
        row['reset_confirmed']=None;row['used']=None;row['reset_error']=error
        raise


_CURRENT = own_context_var('publisher_resource_meter', default=None)
_STOCK_FD_DOMAINS={}
_STOCK_CALL_SEQUENCE=0
_RSS_LIMIT = 8 * 1024**3
_ALLOCATION_LIMIT = 16 * 1024**3
_WALL_NS = 4200 * 10**9
# Text size is a sealed metadata fact, never a fake import/read measurement.
# The stock outer caller must include imports in its independent envelope.
SOURCE_TEXT_RESERVE = None
CONTROL_SETUP_RESERVE = 65536
REFUSAL_WIRE_RESERVE = 4096
REFUSAL_TIME_RESERVE_NS = 5 * 10**9
_SAFE_ERROR = frozenset('abcdefghijklmnopqrstuvwxyz0123456789_')

def attach_refusal(exc, whole=None):
    """Uses only the arena reserved before normal work; never re-enters check.

    No reservation is an aggregate resource observation. A descheduled process
    can miss an absolute deadline; the independent outer observer must reject
    that execution. This finite path preserves the original bounded cause.
    """
    if getattr(exc, 'public_refusal_raw', None) is not None:
        return exc
    cause = exc.cause if type(exc.cause) is str and 1 <= len(exc.cause) <= 128 and all(c in _SAFE_ERROR for c in exc.cause) else 'body_consumer_error'
    stage = exc.stage if type(exc.stage) is str and 1 <= len(exc.stage) <= 128 and all(c in _SAFE_ERROR for c in exc.stage) else 'ingress'
    document = {'schema':'friday.sol037.public-refusal.v1','status':'REFUSED',
                'cause':cause,'stage':stage,'effects_denied':True,'publisher_proof':False,
                'accepted':False,'ready_for_exec':False,'go':False}
    # Bounded ASCII strings and the fixed nine-field schema fit the 4096-byte
    # arena. Serializing a refusal cannot invoke the exhausted ordinary meter.
    arena = whole.refusal_arena if whole is not None else bytearray(REFUSAL_WIRE_RESERVE)
    pieces=(b'{"accepted":false,"cause":"',cause.encode('ascii'),
            b'","effects_denied":true,"go":false,"publisher_proof":false,"ready_for_exec":false,"schema":"friday.sol037.public-refusal.v1","stage":"',
            stage.encode('ascii'),b'","status":"REFUSED"}\n')
    position=0
    for piece in pieces:
        end=position+len(piece)
        if end>len(arena):
            raise ContractError('refusal_bound')
        arena[position:end]=piece
        position=end
    raw=bytes(arena[:position])
    exc.public_refusal = document
    exc.public_refusal_raw = raw
    return exc

def current():
    return _CURRENT.get()

class WholeMeter:
    def __init__(self):
        self.start_mono = time.monotonic_ns()
        self.start_wall = time.time_ns()
        self.end_mono = self.start_mono + _WALL_NS
        self.end_wall = self.start_wall + _WALL_NS
        self.memory_max = _RSS_LIMIT
        self.allocation_max = _ALLOCATION_LIMIT
        # Forward reserves are debits, explicitly reported as reservations.
        # They cover emergency encode/output even if entry or check fails.
        self.read_bytes = 4 * REFUSAL_WIRE_RESERVE
        self.hash_bytes = 0
        self.allocation_bytes = 4 * REFUSAL_WIRE_RESERVE + 4096
        self.refusal_arena = bytearray(REFUSAL_WIRE_RESERVE)
        self.active_slots = 0
        self.peak_slots = 0
        self.peak_rss = 0
        self.fixed_external = False
        self.token = None
        self.cleanup_fault = False
        # Complete standalone descriptor history belongs to this same call.
        # This added prospective cost does not change either original cap.
        domain=_STOCK_FD_DOMAINS.get(os.getpid())
        if domain is None:
            domain={'history':[],'generation':0,'unknown':False,'active':0,'owners':{},
                'history_allocation_reservation':65536*8192,'history_charge_owner':None}
            _STOCK_FD_DOMAINS[os.getpid()]=domain
            self.allocation_bytes+=domain['history_allocation_reservation']
        global _STOCK_CALL_SEQUENCE
        _STOCK_CALL_SEQUENCE+=1;self.call_sequence=_STOCK_CALL_SEQUENCE
        self.fd_domain=domain;domain['owners'][self.call_sequence]=self
        if domain['history_charge_owner'] is None:domain['history_charge_owner']=self.call_sequence
        self.fd_history=domain['history']
        self.completion_result=None;self.completion_error=None
        self.completion_attempted=False;self.completion_received=False
        self.body_scopes=[]
        self.completion_errors=[]
        self.completion_cleanup_errors=[]
        self.context_transition=None
    def __enter__(self):
        if current() is not None:
            raise ContractError('resource_reentry')
        # Prepaid with the constructor's forward envelope; sampling before
        # _CURRENT is installed would enter an unowned standalone FD path.
        transition={'sol070_context_transition':True,'token':None,'var':_CURRENT,
            'before':contextvars.copy_context(),'set_value':self,
            'reset_attempted':False,'reset_confirmed':False,'reset_error':None}
        self.context_transition=transition
        self.token = own_context_set(_CURRENT,self,transition)
        transition['token']=self.token
        try:
            self.check()
        except BaseException as exc:
            transition['reset_attempted']=True
            try:
                own_context_reset(_CURRENT,self.token)
                transition['reset_confirmed']=True
            except BaseException as secondary:
                transition['reset_confirmed']=None;transition['reset_error']=secondary
                self.completion_errors.append(secondary);self.cleanup_fault=True
            self.token=None
            if type(exc) is ContractError: attach_refusal(exc, self)
            self.finish_call(exc)
            raise
        return self
    def __exit__(self, *args):
        error=args[1] if len(args)>1 else None
        if self.token is not None:
            transition=self.context_transition
            try:
                transition['reset_attempted']=True
                own_context_reset(_CURRENT,self.token)
                transition['reset_confirmed']=True
            except BaseException as secondary:
                transition['reset_confirmed']=None;transition['reset_error']=secondary
                self.completion_errors.append(secondary);self.cleanup_fault=True
            finally:self.token=None
        settled=self.finish_call(error)
        if error is None and (not settled or self.cleanup_fault):
            refusal=ContractError('call_completion')
            self.completion_errors.append(refusal)
            attach_refusal(refusal,self)
            raise refusal from None
    def bind_result(self,value):
        self.completion_result=value
        return value
    def discard_body_scope(self,scope):
        for index,actual in enumerate(self.body_scopes):
            if actual is scope:
                del self.body_scopes[index]
                return
        raise ContractError('body_scope_owner')
    def completion_roots(self):
        domain=self.fd_domain
        rows=tuple(row for row in domain['history'] if row.get('call_sequence')==self.call_sequence)
        for scope in self.body_scopes:
            if scope['body_roots'] is None:
                # Stable strong original buffer roots survive later lease/slot
                # retirement. Error graphs remain original objects, not copies.
                scope['body_roots']=tuple(lease.body for lease in scope['leases'])+tuple(
                    cell['body'] for cell in scope['temporary'])
        return {'owner_pid':os.getpid(),'call_sequence':self.call_sequence,
            'whole':self,'rows':rows,'result':self.completion_result,'error':self.completion_error,
            'domain':domain,'history_allocation_reservation':domain['history_allocation_reservation'],
            'error_custody':self.body_scopes,
            'body_leases':tuple(lease for scope in self.body_scopes for lease in scope['leases']),
            'completion_errors':self.completion_errors,
            'cleanup_errors':self.completion_cleanup_errors}
    def finish_call(self,error=None):
        if self.completion_attempted:return self.completion_received
        self.completion_attempted=True;self.completion_error=error
        roots=None
        try:
            roots=self.completion_roots()
            receiver=globals().get('OUTER_COMPLETE_CALL')
            if receiver is None:
                self.cleanup_fault=True
                return False
            receipt=receiver(roots)
            if type(receipt) is not dict or receipt!={'owner_pid':os.getpid(),
                'call_sequence':self.call_sequence,'actual_roots_received':True}:
                self.cleanup_fault=True;return False
            # Both actual receivers retain this exact complete roots object,
            # including the original primary graph and mutable cleanup lists.
            if getattr(self,'accepted_call_custody',None) is not roots:
                self.cleanup_fault=True;return False
            domain=self.fd_domain
            rows=[row for row in domain['history'] if row.get('call_sequence')==self.call_sequence]
            if any(row['status'] not in ('CLOSED','NO_RETURN') for row in rows):
                self.cleanup_fault=True;return False
            from body_scope import _retire_scope
            for scope in self.body_scopes:
                if not scope['retired']:
                    scope['retired']=_retire_scope(scope)
                if not scope['retired']:
                    # The receiver already holds the same cleanup list and
                    # original body roots. Never pop unresolved registrations.
                    self.cleanup_fault=True
            if self.cleanup_fault or self.active_slots:
                self.cleanup_fault=True;return False
            domain['owners'].pop(self.call_sequence,None)
            domain['history'][:]=[row for row in domain['history'] if row.get('call_sequence')!=self.call_sequence]
            self.completion_received=True
            if not domain['owners'] and not domain['history'] and not domain['active'] and not domain['unknown']:
                if _STOCK_FD_DOMAINS.get(os.getpid()) is domain:_STOCK_FD_DOMAINS.pop(os.getpid())
            return True
        except BaseException as secondary:
            # Callback/projection/ordinary cleanup failure does not replace or
            # mutate the primary. Both exact objects remain rooted in the call.
            self.completion_errors.append(secondary)
            self.completion_receiver_error=secondary;self.cleanup_fault=True
            return False
    def configure(self, fact):
        if fact is None:
            return
        keys={'started_monotonic_ns','deadline_monotonic_ns','deadline_wall_ns',
              'memory_max','allocation_max','whole_read_max','output_bytes_max',
              'active_slots_max','artifact_max','member_max','document_bytes_max',
              'selected_independently','effects_granted'}
        if type(fact) is not dict or set(fact)!=keys:
            raise ContractError('resource_contract')
        if fact['selected_independently'] is not True or fact['effects_granted'] is not False:
            raise ContractError('resource_contract')
        for k in keys-{'selected_independently','effects_granted'}:
            if type(fact[k]) is not int or fact[k]<0:
                raise ContractError('resource_contract')
        constants={'whole_read_max':MAX_WHOLE_READ_BYTES,'output_bytes_max':MAX_OUTPUT_BYTES,
                   'active_slots_max':128,'artifact_max':350,'member_max':512,
                   'document_bytes_max':80000000}
        if any(fact[k]!=v for k,v in constants.items()):
            raise ContractError('resource_contract')
        start=fact['started_monotonic_ns']; end=fact['deadline_monotonic_ns']
        if start>self.start_mono or not start<end<=start+_WALL_NS:
            raise ContractError('resource_contract')
        if not 0<fact['memory_max']<=_RSS_LIMIT or not 0<fact['allocation_max']<=_ALLOCATION_LIMIT:
            raise ContractError('resource_contract')
        end_mono=min(self.end_mono,end)
        end_wall=min(self.end_wall,fact['deadline_wall_ns'])
        if time.monotonic_ns() >= end_mono - REFUSAL_TIME_RESERVE_NS or time.time_ns() >= end_wall - REFUSAL_TIME_RESERVE_NS or self.allocation_bytes > fact['allocation_max']:
            raise ContractError('whole_deadline')
        # Actual sampling may consume IO even on refusal; none of the proposed
        # configuration is committed until all fallible checks have passed.
        self.check()
        if self.peak_rss > fact['memory_max']:
            raise ContractError('whole_memory')
        if time.monotonic_ns() >= end_mono - REFUSAL_TIME_RESERVE_NS or time.time_ns() >= end_wall - REFUSAL_TIME_RESERVE_NS:
            raise ContractError('whole_deadline')
        self.end_mono=end_mono; self.end_wall=end_wall
        self.memory_max=fact['memory_max']; self.allocation_max=fact['allocation_max']
        self.fixed_external=True
    def _combined_ok(self, size):
        if type(size) is not int or size<0 or self.read_bytes+self.hash_bytes>MAX_WHOLE_READ_BYTES-size:
            raise ContractError('aggregate_budget')
    def _read_debit(self, size):
        self._combined_ok(size)
        self.read_bytes+=size
    def check(self):
        if time.monotonic_ns()>=self.end_mono-REFUSAL_TIME_RESERVE_NS or time.time_ns()>=self.end_wall-REFUSAL_TIME_RESERVE_NS:
            raise ContractError('whole_deadline')
        # Complete bounded statm read, no cached observation or baseline subtraction.
        self._read_debit(4097)
        sampler=globals().get('OUTER_STOCK_SAMPLE')
        if sampler is not None:
            raw=sampler('/proc/self/statm',4096)
        else:
            # Original standalone consumer stock fence. The performing package
            # always installs its actual owning-phase sampler before calls.
            with fd_scope('/proc/self/statm',4096,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC) as fd:
                raw=os.read(fd,4097)
        if len(raw)>4096 or not raw.endswith(b'\n'):
            raise ContractError('memory_observation')
        fields=raw.split()
        if len(fields)!=7 or any(not p.isdigit() for p in fields):
            raise ContractError('memory_observation')
        rss=int(fields[1])*os.sysconf('SC_PAGE_SIZE')
        self.peak_rss=max(self.peak_rss,rss,resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024)
        if self.peak_rss>self.memory_max:
            raise ContractError('whole_memory')
    def read(self, size):
        # Sampling is actual consumed work, including a failed sample. The
        # requested caller read is committed only after the checkpoint passes.
        self.check(); self._read_debit(size)
    def hash(self, size):
        # Memory-hash debit is not a kernel read and is not folded into read_bytes.
        self._combined_ok(size)
        self.check()
        self._combined_ok(size)
        self.hash_bytes += size
    def allocate(self, size):
        if type(size) is not int or size<0 or self.allocation_bytes>self.allocation_max-size:
            raise ContractError('whole_allocation')
        self.check()
        self.allocation_bytes+=size
    def slots(self, delta):
        if type(delta) is not int or not 0<=self.active_slots+delta<=128:
            raise ContractError('held_stream')
        if delta > 0: self.check()
        self.active_slots+=delta; self.peak_slots=max(self.peak_slots,self.active_slots)
    def retire_slots(self, count):
        # Retirement is finite and does not sample/read/allocate or throw from
        # an expired meter. Invalid ownership is retained as a refusal fact.
        if type(count) is not int or not 0 <= count <= self.active_slots:
            self.cleanup_fault = True
            return False
        self.active_slots -= count
        return True
    def report(self):
        self._combined_ok(0)
        self.check()
        return {'whole_read_debit':self.read_bytes,'memory_hash_debit':self.hash_bytes,'cumulative_read_plus_hash':self.read_bytes+self.hash_bytes,'allocation_reservation':self.allocation_bytes,
                'peak_rss_observed':self.peak_rss,'active_slots':self.active_slots,
                'peak_slots':self.peak_slots,'deadline_monotonic_ns':self.end_mono,
                'deadline_wall_ns':self.end_wall,'externally_fixed':self.fixed_external,
                'runtime_compliance':'NOT_PROVEN','unknown_runtime_io':'NOT_ZERO_NOT_PROVEN',
                'source_import_io':'NOT_ZERO_NOT_PROVEN',
                'whole_host_helper_memory':'NOT_ZERO_NOT_PROVEN',
                'refusal_forward_read_allocation_reserve':4*REFUSAL_WIRE_RESERVE,
                'refusal_time_reserve_ns':REFUSAL_TIME_RESERVE_NS,
                'cleanup_fault':self.cleanup_fault,
                'observation_scope':'call-prefix-before-final-wire-not-independent-outer-aggregate',
                'final_wire_seal_transport_actual':'NOT_ZERO_NOT_PROVEN',
                'reservations_are_measurements':False}

def debit_read(size):
    if current() is not None: current().read(size)
def debit_hash(size):
    if current() is not None: current().hash(size)
def reserve_allocation(size):
    if current() is not None: current().allocate(size)
def checkpoint():
    if current() is not None: current().check()

class _ConsumerFileScope:
    """Existing standalone owner; retained failure state is bound Source DATA."""
    def __init__(self,path,maximum,flags):
        self.path=path;self.maximum=maximum;self.flags=flags
        self.whole=None;self.domain=None;self.row=None;self.fd=None
        self.enter_attempted=False;self.close_attempted=False
    def __enter__(self):
        if self.enter_attempted:raise ContractError('fd_scope_reentry')
        self.enter_attempted=True
        self.whole=current()
        if self.whole is None:raise ContractError('fd_existing_stock_owner')
        self.domain=self.whole.fd_domain
        if self.domain['unknown']:raise ContractError('fd_close_unconfirmed')
        if len(self.whole.fd_history)>=65536 or self.domain['active']>=128:raise ContractError('held_stream')
        self.domain['generation']+=1
        attempt={'status':'NOT_ATTEMPTED','error':None}
        self.row={'fd':None,'generation':self.domain['generation'],'status':'PREOWNED','identity9':None,
             'path':self.path,'credit':'same-call-prepaid-FD-arena','attempt':attempt,
             'call_sequence':self.whole.call_sequence}
        # Actual preowned row and manager FD slot precede the only factory.
        self.whole.fd_history.append(self.row)
        try:self.fd=os.open(self.path,self.flags)
        except BaseException:self.row['status']='NO_RETURN';raise
        self.row['fd']=self.fd;self.row['status']='ACQUIRED';self.domain['active']+=1
        try:
            s=os.fstat(self.fd)
            self.row['identity9']=[str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
            self.row['status']='HELD'
            return self.fd
        except BaseException:
            self._close()
            raise
    def _close(self):
        if self.close_attempted:return
        self.close_attempted=True
        attempt=self.row['attempt'];attempt['status']='ATTEMPTED'
        try:os.close(self.fd)
        except BaseException as exc:
            self.row['status']='UNKNOWN';attempt['status']='UNKNOWN';attempt['error']=exc
            self.domain['unknown']=True;self.whole.cleanup_fault=True
        else:
            self.row['status']='CLOSED';attempt['status']='CLOSED';self.domain['active']-=1
    def __exit__(self,error_type,error,traceback):
        # No generator.throw, temporary foreign manager or traceback rewrite.
        self._close()
        return False

def fd_scope(path,maximum,flags):
    external=globals().get('OUTER_FD_SCOPE')
    if external is not None:return external(path,maximum,flags)
    return _ConsumerFileScope(path,maximum,flags)

def bounded_file(path, maximum):
    checkpoint()
    with fd_scope(path,maximum,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK) as fd:
        before=os.fstat(fd)
        if before.st_size>maximum or before.st_nlink!=1 or (before.st_mode&0o170000)!=0o100000:
            raise ContractError('document_size')
        reserve_allocation(before.st_size+1)
        chunks=[]; total=0
        while True:
            allowance=min(65536,maximum-total+1)
            debit_read(allowance)
            part=os.read(fd,allowance)
            if not part:break
            total+=len(part)
            if total>maximum:raise ContractError('document_size')
            chunks.append(part)
        after=os.fstat(fd); named=os.stat(path,follow_symlinks=False)
        nine=lambda s:(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        if nine(before)!=nine(after) or nine(after)!=nine(named) or total!=before.st_size:
            raise ContractError('input_changed')
        reserve_allocation(total)
        return b''.join(chunks)

def json_preflight(raw, max_bytes, max_depth, max_items, max_string):
    if type(raw) is not bytes or len(raw)>max_bytes:
        raise ContractError('document_size')
    debit_read(len(raw))
    depth=0; quote=False; escape=False; string_len=0; containers=0
    reserve_allocation(len(raw) + ((len(raw)+65535)//65536)*64)
    # Reject pathological depth before json.loads recursion/allocation.
    for p in range(0,len(raw),65536):
        checkpoint()
        for c in raw[p:p+65536]:
            if quote:
                string_len+=1
                if string_len>max_string*12+12:raise ContractError('string_length')
                if escape:escape=False
                elif c==92:escape=True
                elif c==34:quote=False
            elif c==34:quote=True; string_len=0
            elif c in (91,123):
                depth+=1
                containers+=1
                if depth>max_depth:raise ContractError('depth')
            elif c in (93,125):depth-=1
    # Conservative finite object allocation reservation; never a live-memory claim.
    reserve_allocation(len(raw)*256+4096+containers*48)

def canonical_bound(value, maximum=MAX_OUTPUT_BYTES):
    reserve_allocation(128)
    total=1; todo=[(value,1)]; visited=0
    while todo:
        item,depth=todo.pop(); visited+=1
        if depth>24 or visited>1000000:raise ContractError('depth')
        if visited%512==0:checkpoint()
        t=type(item)
        if t is dict:
            if len(item)>512:raise ContractError('count')
            reserve_allocation(len(item)*64+64)
            total+=2+len(item)*2
            for k,v in item.items():
                if type(k) is not str:raise ContractError('canonical_encoding')
                todo.extend(((k,depth+1),(v,depth+1)))
        elif t in (list,tuple):
            if len(item)>512:raise ContractError('count')
            reserve_allocation(len(item)*32+64)
            total+=2+len(item); todo.extend((v,depth+1) for v in item)
        elif t is str:
            # Compute the complete ensure_ascii JSON width before allocation.
            # A factor of twelve for every ASCII character rejects ordinary
            # complete outputs whose actual wire is within the original cap.
            debit_read(len(item)*4)
            width=2
            for character in item:
                code=ord(character)
                if character in ('"','\\') or character in ('\b','\f','\n','\r','\t'):width+=2
                elif code<32 or 127<=code<=65535:width+=6
                elif code>65535:width+=12
                else:width+=1
            total+=width
        elif t is bool:total+=5
        elif item is None:total+=4
        elif t is int:
            if item.bit_length()>128:raise ContractError('integer_bounds')
            total+=42
        else:raise ContractError('canonical_encoding')
        if total>maximum:raise ContractError('whole_output')
    reserve_allocation(total*4+4096)
    return total

class _Hash:
    def __init__(self,mode,name,data=b'',parent=None):
        # Exact immutable constructor/update inputs are OWN computation data.
        # Mutable stock implementation is real runtime support, not a digest/
        # name/pointer surrogate for a demanded foreign private heap.
        reserve_allocation(2*len(data)+16384)
        raw=bytes(data)
        ledger={'schema':'friday.astra256.own-hash-input-ledger.v2','mode':mode,
            'name':name,'constructor_original':data,'constructor_bytes':raw,
            'parent':parent,'parent_operations_at_copy':(1+len(parent.own_state['updates'])) if parent is not None else None,
            'runtime_h':None,'actual_attempted':False,'constructed':False,'updates':[],
            'original_error':None,'metadata_error':None,'state_complete':False}
        self.h=None;self.own_state=ledger
        _HASH_ORIGINS.append(self) # original partial wrapper BEFORE factory
        native=_own_native()
        try:
            if native is not None:
                self.h=native.own_hash_new(mode,name,raw,ledger,parent.h if parent is not None else None)
            else:
                ledger['actual_attempted']=True
                if mode=='copy':self.h=parent.h.copy()
                elif mode=='sha256':self.h=_hashlib.sha256(raw)
                else:self.h=_hashlib.new(name,raw)
                ledger['runtime_h']=self.h;ledger['constructed']=True
            # Native returned-object/constructed facts are already committed
            # to these preowned slots before Python assignment can fail.
            ledger['state_complete']=parent is None or parent.own_state['state_complete'] is True
        except BaseException as error:
            if ledger['constructed'] is True:ledger['metadata_error']=error
            else:ledger['original_error']=error
            ledger['state_complete']=False
            raise
    def update(self,data):
        debit_hash(len(data));reserve_allocation(2*len(data)+16384)
        raw=bytes(data)
        operation={'original_input':data,'full_bytes':raw,'attempted':True,
            'actual_attempted':False,'confirmed':False,'original_error':None,
            'metadata_error':None}
        before_complete=self.own_state['state_complete'] is True
        self.own_state['state_complete']=False
        self.own_state['updates'].append(operation)
        native=_own_native()
        try:
            if native is None:
                operation['actual_attempted']=True;self.h.update(raw)
                operation['confirmed']=True
            else:native.own_hash_update(self.h,raw,operation)
            self.own_state['state_complete']=before_complete
        except BaseException as error:
            if operation['confirmed'] is True:operation['metadata_error']=error
            else:operation['original_error']=error
            self.own_state['state_complete']=False
            raise
    def hexdigest(self):checkpoint();return self.h.hexdigest()
    def digest(self):checkpoint();return self.h.digest()
    def copy(self):
        reserve_allocation(8192)
        return _Hash('copy',self.own_state['name'],b'',self)
class HashlibProxy:
    def sha256(self,data=b''):
        debit_hash(len(data));return _Hash('sha256','sha256',data)
    def new(self,name,data=b''):
        debit_hash(len(data));return _Hash('new',name,data)
