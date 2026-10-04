"""Consumer of the built-in, pre-initialization owner in native/source_owner.c.

This module cannot construct a native lease. A missing selected image is a
future admission fact; ordinary Python estimates never substitute for it.
"""
import sys
import types
from contextvars import ContextVar

_source_raw_scope=ContextVar('friday_same_original_source_raw_owner',default=None)

class SourceRawOrigin:
    __slots__=('exception','previous')

class SourceRawOwner:
    """Exact raw objects rooted before any fallible evidence projection.

    source_native_raw_endpoint retires this root. This constructor does not, and the Root destructor is not the Source endpoint.

    Pending slots precede recorder allocation. The original native registry
    owns this graph before meter/decoder construction; ResultOwner borrows that
    same graph. Native allocation remains charged, including captured frames.
    """
    __slots__=('native','head','pending','pending_recorder')
    def __init__(self,native):
        self.native=native;self.head=None;self.pending=None;self.pending_recorder=None

    def capture(self,exc):
        if self.pending is not None:
            if exc is self.pending or exc is self.pending_recorder:return
            # Never overwrite an uncommitted origin or recorder. The existing
            # fixed native error lane owns this next actual origin first.
            if self.native is not None:self.native.retain_source_raw_exception(exc)
            raise exc
        self.pending=exc
        try:
            if self.native is not None:self.native.begin_terminal()
            existing=self.head
            while existing is not None:
                if existing.exception is exc:
                    self.pending=None
                    return
                existing=existing.previous
            node=SourceRawOrigin.__new__(SourceRawOrigin)
            node.exception=exc;node.previous=self.head
            self.head=node;self.pending=None
        except BaseException as recorder:
            self.pending_recorder=recorder
            raise recorder from exc

def source_raw_owner():
    native=selected_owner()
    previous=native.source_raw_custody() if native is not None else None
    if previous is not None:return previous
    try:
        owner=SourceRawOwner.__new__(SourceRawOwner)
        # Commit partial ownership before constructor or subsequent meter factory.
        if native is not None:native.source_raw_custody(owner)
        SourceRawOwner.__init__(owner,native)
    except BaseException as exc:
        # The ORIGINAL preinit fixed error lane precedes even this __new__.
        if native is not None:native.retain_source_raw_exception(exc)
        raise
    return owner

def retain_source_origin(meter,exc):
    owner=meter.get('source_raw_owner') if isinstance(meter,dict) else _source_raw_scope.get()
    if owner is None:
        native=selected_owner()
        if native is None:return
        owner=source_raw_owner()
    owner.capture(exc)

def source_raw_state_matches(scope,native):
    """Called stock-state consumer of this exact original native root.

    This checks registered records, not an inferred universal interpreter heap.
    Full allocations/frames still require the original native producer fence.
    """
    if scope is not _source_raw_scope:return False
    from contextvars import copy_context
    actual_context=copy_context()
    owner=native.source_raw_custody()
    if type(owner) is not SourceRawOwner or scope.get() is not owner:return False
    if len(actual_context)!=1 or actual_context.get(scope) is not owner:return False
    if owner.native is not native:return False
    node=owner.head;seen=set()
    while node is not None:
        if type(node) is not SourceRawOrigin or id(node) in seen or not isinstance(node.exception,BaseException):return False
        seen.add(id(node));node=node.previous
    return all(value is None or isinstance(value,BaseException) for value in (owner.pending,owner.pending_recorder))

def leave_source_scope(meter):
    token=meter.pop('source_raw_scope_token',None)
    if token is not None:_source_raw_scope.reset(token)

_selected=None

class NativeHandle(tuple):
    snapshot=property(lambda self:self[0])
    begin_terminal=property(lambda self:self[1])
    recv_packet=property(lambda self:self[2])
    send_packet=property(lambda self:self[3])
    spawn_source=property(lambda self:self[4])
    close_fd=property(lambda self:self[5])
    send_root_completion=property(lambda self:self[6])
    capture_root_ledger=property(lambda self:self[7])
    direct_source_wait=property(lambda self:self[8])
    direct_root_wait=property(lambda self:self[9])
    claim_existing_parent_custody=property(lambda self:self[10])
    accept_existing_parent_delivery=property(lambda self:self[11])
    accept_existing_parent_result=property(lambda self:self[12])
    bind_observed_root=property(lambda self:self[13])
    lock_canonical_slot=property(lambda self:self[14])
    unlock_canonical_slots=property(lambda self:self[15])
    received_history=property(lambda self:self[16])
    bind_original_consumer_end=property(lambda self:self[17])
    retire_direct_children=property(lambda self:self[18])
    source_raw_custody=property(lambda self:self[19])
    retain_source_raw_exception=property(lambda self:self[20])
    accept_registered_carrier_before_birth=property(lambda self:self[21])
    note_source_plane_readonly=property(lambda self:self[22])
    accept_source_plane_bytes=property(lambda self:self[23])
    source_backing_extent=property(lambda self:self[24])
    borrow_preowned_source_backing=property(lambda self:self[25])
    preowned_source_backing_view=property(lambda self:self[26])


def selected_owner():
    global _selected
    if _selected is not None: return _selected
    module=sys.modules.get('_friday_source_owner')
    if not isinstance(module,types.ModuleType) or getattr(module,'__file__',None) is not None:
        return None
    functions={}
    for name in ('snapshot','begin_terminal','recv_packet','send_packet','spawn_source','close_fd',
                 'send_root_completion','capture_root_ledger','direct_source_wait','direct_root_wait',
                 'claim_existing_parent_custody','accept_existing_parent_delivery',
                 'accept_existing_parent_result','bind_observed_root','lock_canonical_slot',
                 'unlock_canonical_slots','received_history','bind_original_consumer_end','retire_direct_children',
                 'source_raw_custody','retain_source_raw_exception',
                 'accept_registered_carrier_before_birth','note_source_plane_readonly','accept_source_plane_bytes',
                 'source_backing_extent','borrow_preowned_source_backing','preowned_source_backing_view'):
        value=getattr(module,name,None)
        if not isinstance(value,types.BuiltinFunctionType) or value.__self__ is not module:
            return None
        functions[name]=value
    # Capture immutable bound native functions; replacing a module attribute
    # after binding cannot redirect the performing owner into Python code.
    _selected=NativeHandle(tuple(functions[name] for name in
        ('snapshot','begin_terminal','recv_packet','send_packet','spawn_source','close_fd',
         'send_root_completion','capture_root_ledger','direct_source_wait','direct_root_wait',
         'claim_existing_parent_custody','accept_existing_parent_delivery',
         'accept_existing_parent_result','bind_observed_root','lock_canonical_slot',
         'unlock_canonical_slots','received_history','bind_original_consumer_end','retire_direct_children',
         'source_raw_custody','retain_source_raw_exception',
         'accept_registered_carrier_before_birth','note_source_plane_readonly','accept_source_plane_bytes',
         'source_backing_extent','borrow_preowned_source_backing','preowned_source_backing_view')))
    return _selected


def native_entry_cause(resources,meter):
    from source.bounds import performing_requested
    if not performing_requested(resources): return None
    owner=selected_owner()
    if owner is None: return 'ingress_context_unbound'
    actual=owner.snapshot()
    ceilings=resources['ceilings']
    if (actual['role']!='readonly-archive-scan' or actual['max_fds']!=16 or
        actual['canonical_workers']!=4 or actual['preinitialization'] is not True):
        return 'ingress_context_unbound'
    for key in ('max_live_bytes','max_work_bytes','max_read_bytes','max_output_bytes','max_wall_ms','max_rss_bytes'):
        if actual[key]!=ceilings.get(key): return 'resource_ceilings_absent'
    if actual['failed']:
        return 'resource_ceiling_exceeded'
    meter['native_owner']=owner
    return None


def begin_terminal(meter):
    owner=meter.get('native_owner') or selected_owner()
    if owner is not None:
        owner.begin_terminal()
        meter['physical_terminal_owner']=owner


class ResultOwner:
    """Caller-owned capsule. Aliases retain the real meter and native session.

    Releasing this capsule does not claim physical free: allocator free hooks
    retire actual Python/native blocks, including live aliases and traceback
    frames. A released public view is invalidated; aliases remain physically
    charged independently of this optional explicit release convenience.
    """
    def __init__(self,meter):
        self.meter=meter
        self.raw_owner=meter['source_raw_owner']
        self.native=selected_owner()
        # Alias the selected native owner, not a repeated full packet copy for
        # every leaf/map. Its actually called C terminal consumer owns complete
        # originals even if Python could not construct a projection.
        self.native_packet_history_owner=self.native
        self.released=False

    def release(self):
        self.released=True
        self.meter=None
        self.raw_owner=None
        self.native=None
        self.native_packet_history_owner=None


class OwnedMap(dict):
    def release(self):
        owner=self._source_owner
        self.clear()
        self._source_owner=None
        # The byte view may still own this same capsule. Its physical blocks
        # are not refunded here and no result graph is destroyed by assertion.
        if sys.getrefcount(owner)<=2: owner.release()


class OwnedBytes(bytes):
    def release(self):
        self._source_owner=None
        # bytes are immutable; any remaining caller reference remains live.


def transfer_result(result,blob,meter):
    if isinstance(result,OwnedMap) and getattr(result,'_source_owner',None) is not None:
        owner=result._source_owner
    else:
        owner=ResultOwner(meter)
        result=OwnedMap(result)
    blob=OwnedBytes(blob)
    meter.pop('final_output_bytes',None)
    meter.pop('pending_terminal_result',None)
    result._source_owner=owner
    blob._source_owner=owner
    leave_source_scope(meter)
    return result,blob


def own_map(result,meter):
    if isinstance(result,OwnedMap):
        leave_source_scope(meter)
        return result
    view=OwnedMap(result);view._source_owner=ResultOwner(meter)
    leave_source_scope(meter)
    return view


class RootRetainedCapsule:
    """Finite caller-owned ABI; raw owners precede any fallible projection.

    The same object is passed to its concrete ordinary consumer. No descriptor
    duplication, reconstructed owner, pidfd adoption or physical-free assertion
    is hidden in this ABI. An unconfirmed child prevents actual retirement.
    All allocations use the same selected preinitialization native owner.
    """
    __slots__=('owner','payload','raw_exceptions','state','consumer_failure',
               'construction_failure','receipt','native','original_end_ns',
               'raw_primary','raw_recording_failure','existing_caller','peer_acceptance')
    def __init__(self,owner=None):
        self.owner=owner
        self.payload=None
        self.raw_exceptions=[]
        self.state='CONSTRUCTING'
        self.consumer_failure=None
        self.construction_failure=None
        self.receipt=None
        self.native=selected_owner()
        self.original_end_ns=None
        self.raw_primary=None
        self.raw_recording_failure=None
        self.existing_caller=None
        self.peer_acceptance=None

    def attach(self,owner):
        if self.owner is not None and self.owner is not owner:
            raise RuntimeError('retained capsule owner replacement')
        self.owner=owner
        budget=getattr(owner,'budget',None)
        if budget is not None:
            self.original_end_ns=budget.started+budget.contract['max_wall_ms']*1000000

    def retain(self,exc):
        # Never encode class/message/hex before owning the actual exception and
        # every implicit/explicit traceback/frame alias reachable from it.
        caller=self.existing_caller
        if caller is not None:caller.retain(exc)
        if self.raw_primary is None:self.raw_primary=exc
        node=caller.prepare_raw_origin('RootRetainedCapsule.raw_exceptions.append') if caller is not None else None
        try:self.raw_exceptions.append(exc)
        except BaseException as recorder:
            if node is not None:caller.capture_raw_origin(node,recorder)
            self.raw_recording_failure=recorder
            raise recorder from exc

    def complete(self,payload):
        if self.state!='CONSTRUCTING':raise RuntimeError('capsule completed twice')
        self.payload=payload
        self.state='READY'
        return self

    def begin_consume(self):
        if self.state!='READY':raise RuntimeError('capsule is not an unconsumed terminal')
        self.state='CONSUMING'
        return self.payload

    def consumed(self,receipt):
        if self.state!='CONSUMING':raise RuntimeError('capsule consumer transition')
        self.receipt=receipt
        # Retain the actual owner even after byte delivery. Native destruction
        # and its outside direct-wait consumer alone establish the later end.
        self.state='CONSUMED_RETAINED'

    def retire(self):
        owner=self.owner
        if owner is not None and getattr(owner,'child',None) is not None and not getattr(owner,'child_reaped',False):
            raise RuntimeError('STOP_UNCONFIRMED: actual inner child owner retained')
        if owner is not None and hasattr(owner,'root_reaped') and not owner.root_reaped:
            raise RuntimeError('STOP_UNCONFIRMED: actual observed Root owner retained')
        if self.state!='CONSUMED_RETAINED':raise RuntimeError('capsule has no consumed receipt')
        # Detach this view once. Any independently returned map/bytes/error
        # alias still owns its objects; no logical/native live refund occurs.
        self.payload=None
        self.owner=None
        self.state='VIEW_RETIRED_ALIASES_NATIVE_CHARGED'


class CallerRawOrigin:
    """Prospective same-caller raw root, prepared before its producer.

    The linked roots use the original native allocator and remain charged with
    their caller. Storing a caught object in an already prepared slot performs
    no encoder/list append. A later allocation failure cannot remove an earlier
    primary or a secondary origin from this chain.
    """
    __slots__=('operation','exception','recorder_exception','previous')


class ExistingParentCaller:
    """Actual preowned caller in the already-existing outside native parent.

    The native fixed registry owns this exact object BEFORE __init__. Capsules,
    partial Root owners, raw origins and returned aliases are installed here
    before their first fallible producer. This is one generation in the SAME
    original Root role, not another observer process or a new budget domain.
    """
    __slots__=('native','selection','resource_contract','capsule','raw_primary',
               'raw_recording_failure','raw_exceptions','result','state',
               'original_started_ns','original_end_ns','generation','native_packet_history',
               'raw_origin_head','pending_raw_origin','raw_accept_failure')
    def __init__(self,native,selection,resource_contract,topology=None):
        self.native=native;self.selection=selection;self.resource_contract=resource_contract
        self.capsule=None;self.raw_primary=None;self.raw_recording_failure=None;self.raw_accept_failure=None
        self.raw_exceptions=[];self.result=None;self.state='PREOWNED'
        self.native_packet_history=None
        self.raw_origin_head=None;self.pending_raw_origin=None
        topology=selection['parent_topology'] if topology is None else topology
        self.original_started_ns=topology['original_started_ns']
        self.original_end_ns=topology['original_end_ns']
        self.generation=selection['generation']

    def new_capsule(self):
        if self.state!='PREOWNED' or self.capsule is not None:
            raise RuntimeError('existing caller generation reused')
        capsule=RootRetainedCapsule.__new__(RootRetainedCapsule)
        self.capsule=capsule
        RootRetainedCapsule.__init__(capsule)
        capsule.existing_caller=self
        capsule.original_end_ns=self.original_end_ns
        self.state='OWNS_PARTIAL_CAPSULE'
        return capsule

    def retain(self,exc):
        # __slots__ reference assignment owns the actual raw graph even if the
        # later list append or public exception recording cannot allocate.
        if self.raw_primary is None:self.raw_primary=exc
        # Own the reached object before allocating a new linked root. If that
        # prospective recorder fails, the pending root remains owned and this
        # generation stops recording rather than overwriting that raw object.
        if self.pending_raw_origin is not None:
            raise RuntimeError('STOP_UNCONFIRMED: raw recorder has an owned pending origin') from exc
        self.pending_raw_origin=exc
        node=None
        try:
            node=self.prepare_raw_origin('ExistingParentCaller.retain')
            node.exception=exc
            self.pending_raw_origin=None
            self.raw_exceptions.append(exc)
        except BaseException as recorder:
            if node is not None:node.recorder_exception=recorder
            self.raw_recording_failure=recorder
            raise recorder from exc

    def prepare_raw_origin(self,operation):
        node=CallerRawOrigin.__new__(CallerRawOrigin)
        node.operation=operation;node.exception=None;node.recorder_exception=None
        node.previous=getattr(self,'raw_origin_head',None)
        self.raw_origin_head=node
        return node

    def capture_raw_origin(self,node,exc,capsule=None):
        # All three assignments below target existing slots. The exact raw
        # traceback/notes/cause/context graph is retained even with a primary.
        node.exception=exc
        if getattr(self,'raw_primary',None) is None:self.raw_primary=exc
        if capsule is not None and getattr(capsule,'raw_primary',None) is None:
            capsule.raw_primary=exc

    def record_uncertainty(self,budget,node,operation):
        if budget is None:return
        try:
            budget.close_uncertainties.append({'operation':operation,
                'charged':True,'attempted_once':True})
        except BaseException as recorder:
            node.recorder_exception=recorder
            raise

    def accept(self,result):
        if self.result is not None:raise RuntimeError('existing caller result accepted twice')
        self.result=result
        # Fixed native acceptance precedes state/public-return allocations.
        # Its meaning is custody of this exact graph, not peer receipt/GO.
        self.native.accept_existing_parent_result(self,self.generation,result)
        self.state='ACCEPTED_RETAINED_NATIVE_CHARGED'
        return result

    def retire_before_native_end(self):
        """Called once by the C final and error consumer inside the original end.

        Packet history and the performed ledger are committed before any refusal.
        An unreaped child, a failed projection, or a sticky close does not skip
        that commit and does not invent a second unlock or a new role.
        """
        uncertainty = False
        node=self.prepare_raw_origin('native_caller.retire_direct_children')
        try:self.native.retire_direct_children()
        except BaseException as exc:
            uncertainty=True
            self.capture_raw_origin(node,exc)
        node=self.prepare_raw_origin('native_caller.received_history')
        try:
            self.native_packet_history = self.native.received_history()
        except BaseException as exc:
            uncertainty = True
            self.capture_raw_origin(node,exc)
        capsule = getattr(self, "capsule", None)
        owner = getattr(capsule, "owner", None) if capsule is not None else None
        budget = getattr(owner, "budget", None) if owner is not None else None
        if budget is not None:
            node=self.prepare_raw_origin('native_caller.budget.begin_terminal')
            try:
                budget.begin_terminal()
            except BaseException as exc:
                uncertainty = True
                self.capture_raw_origin(node,exc,capsule)
        if owner is not None and hasattr(owner, "cancel_and_reap"):
            node=self.prepare_raw_origin('native_caller.cancel_and_reap')
            try:
                owner.cancel_and_reap()
            except BaseException as exc:
                uncertainty = True
                self.capture_raw_origin(node,exc,capsule)
            # A failed cancellation must not make the next actually reached
            # pipe-origin disappear behind the first primary error.
            if hasattr(owner,'complete_error_pipes'):
                node=self.prepare_raw_origin('native_caller.complete_error_pipes')
                try:owner.complete_error_pipes()
                except BaseException as exc:
                    uncertainty=True
                    self.capture_raw_origin(node,exc,capsule)
        if owner is not None:
            selector = getattr(owner, "selector", None)
            if selector is not None:
                owner.selector = None
                node=self.prepare_raw_origin('native_caller.selector.close')
                try:
                    selector.close()
                except BaseException as exc:
                    uncertainty = True
                    self.capture_raw_origin(node,exc,capsule)
                    self.record_uncertainty(budget,node,'native_caller.selector.close')
                else:
                    if budget is not None:
                        node=self.prepare_raw_origin('native_caller.budget.release_selector')
                        try:
                            budget.release("fds", 1)
                        except BaseException as exc:
                            uncertainty = True
                            self.capture_raw_origin(node,exc,capsule)
            native = getattr(owner, "native", None) or self.native
            node=self.prepare_raw_origin('native_caller.unlock_canonical_slots')
            try:
                native.unlock_canonical_slots()
            except BaseException as exc:
                uncertainty = True
                self.capture_raw_origin(node,exc,capsule)
                self.record_uncertainty(budget,node,'canonical_slot.unlock.native_once')
            if getattr(owner, "child", None) is not None and not getattr(owner, "child_reaped", False):
                uncertainty = True
            elif hasattr(owner, "close"):
                node=self.prepare_raw_origin('native_caller.owner.close')
                try:
                    owner.close()
                except BaseException as exc:
                    uncertainty = True
                    self.capture_raw_origin(node,exc,capsule)
            if hasattr(owner, "root_reaped") and not owner.root_reaped:
                uncertainty = True
        if budget is not None:
            node=self.prepare_raw_origin('native_caller.capture_root_ledger')
            try:
                self.native.capture_root_ledger(tuple(budget.used[key] for key in (
                    "fds", "read_bytes", "work_bytes", "live_bytes", "output_bytes")),
                    budget.actual_read_bytes, budget.actual_output_bytes)
            except BaseException as exc:
                uncertainty = True
                self.capture_raw_origin(node,exc,capsule)
        if uncertainty:
            raise RuntimeError("STOP_UNCONFIRMED: original caller retirement uncertainty")
