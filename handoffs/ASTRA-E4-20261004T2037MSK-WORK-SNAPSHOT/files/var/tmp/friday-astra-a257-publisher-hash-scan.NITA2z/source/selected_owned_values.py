"""Prescribed SELF-OWNED mmap/public-stock cuts, never a heap inspector.

Birth records below are Source data, not native authority. Only the actual
Root-prepared FD/credit may create a mapping, before fork. Full bytes and actual
row aliases travel through the SAME full-value codec used by both receivers.
A cohort cut keeps the full raw image before the bank writes. Unregistered
or unstable mappings remain explicit refusal.
"""
import os
import sys
import types
import mmap
from common import Refused, DOCUMENT_MAX
from custody import identity9

# Exact own constructors and transitions. No empty-context callback or inferred
# default. Foreign/untracked variables stay explicit incomplete selected data.
import contextvars
_OWN_NATIVE=sys.modules.get('publisher_owned_custody')
_OWN_NATIVE_PID=os.getpid()
_CONTEXT_BIRTHS=[]
_CONTEXT_TRANSITIONS=[]

def _own_native():
    return _OWN_NATIVE if os.getpid()==_OWN_NATIVE_PID else None

def own_context_var(name,default=contextvars.Token.MISSING):
    native=_own_native()
    if native is not None:native.own_prepare(131072)
    has_default=default is not contextvars.Token.MISSING
    birth={'schema':'friday.sol105.own-context-birth.v1','var':None,'name':name,
        'has_default':has_default,'default':default if has_default else None,
        'attempted':False,'confirmed':False,'original_error':None}
    _CONTEXT_BIRTHS.append(birth) # full constructor inputs BEFORE effect
    birth['attempted']=True
    native=_own_native()
    try:
        var=(native.own_context_var(name,has_default,birth['default']) if native is not None
            else contextvars.ContextVar(name,default=default) if has_default else contextvars.ContextVar(name))
        birth['var']=var;birth['confirmed']=True
        return var
    except BaseException as error:
        birth['original_error']=error
        raise

def own_context_set(var,value,transition=None):
    native=_own_native()
    if native is not None:native.own_prepare(131072)
    if transition is None:
        transition={'sol070_context_transition':True,'token':None,'var':var,
            'before':contextvars.copy_context(),'set_value':value,
            'reset_attempted':False,'reset_confirmed':False,'reset_error':None,
            'actual_context':None,'used':False}
    else:
        transition['actual_context']=None;transition['used']=False
    _CONTEXT_TRANSITIONS.append(transition) # before the actual setter
    native=_own_native()
    token=native.own_context_set(var,value,transition) if native is not None else var.set(value)
    transition['token']=token
    # Native setter captured/published actual_context BEFORE its sole effect;
    # returning the real token does not invoke another fallible metadata factory.
    return token

def own_context_reset(var,token):
    rows=[row for row in _CONTEXT_TRANSITIONS if row['token'] is token and row['var'] is var]
    if len(rows)!=1:raise Refused('own_context_reset_actual_transition','terminal')
    row=rows[0];row['reset_attempted']=True
    native=_own_native()
    try:
        if native is None:var.reset(token)
        else:native.own_context_reset(var,token)
        row['reset_confirmed']=True;row['used']=True
    except BaseException as error:
        row['reset_confirmed']=None;row['used']=None;row['reset_error']=error
        raise

def own_context_body(value):
    native=_own_native()
    if native is not None:
        return native.own_context_body(value)
    # Preserve standalone/fork-shadow stock consumers. Known constructor
    # defaults are real inputs; an absent default is not invented as None.
    if type(value) is contextvars.ContextVar:
        rows=[row for row in _CONTEXT_BIRTHS if row['var'] is value]
        if len(rows)!=1:return None
        row=rows[0];active=contextvars.copy_context();present=value in active
        return {'name':row['name'],'has_default':row['has_default'],'default':row['default'],
            'present':present,'current':active[value] if present else None}
    return None # actual native token-context/used body unavailable, never guessed

def own_binding(value):
    native=_own_native()
    if native is None:return None
    relation=native.own_binding(value)
    if relation is not None and native.own_binding_check(relation) is not True:
        raise Refused('actual_retained_compiled_binding_disagrees','terminal')
    return relation

def own_hash_body(value):
    native=_own_native()
    return None if native is None else native.own_hash_body(value)

def own_function_body(value):
    native=_own_native()
    return None if native is None else native.own_function(value)

def own_class_body(value):
    native=_own_native()
    return None if native is None else native.own_class_body(value)

def own_runtime_support(value,origin,key,mode):
    native=_own_native()
    return None if native is None else native.own_support(value,origin,key,mode)

def require_runtime_support(serial):
    native=_own_native()
    if native is None or native.own_support_check(serial) is not True:
        raise Refused('actual_runtime_support_relation_retained','terminal')


_MAP_BIRTHS={}
_PUBLIC_STOCK=frozenset(('mmap','struct','hashlib','_hashlib','_struct','os',
    'posix','stat','time','resource','fcntl','signal','selectors','select',
    'contextvars','_contextvars','contextlib','json','builtins'))

def charge_selected_allocation(credit,allocation):
    # Reservation.commit has NO allocation argument. Retain a cumulative
    # original-credit subledger BEFORE materialization; never grow/refund it.
    meter=credit.meter;meter._owned()
    pending=meter.pending.get(credit.token)
    used=getattr(credit,'selected_value_allocation',0)
    banks=[item for _,item in meter.local_owners.values()
        if getattr(item,'hold',None) is credit and type(item).__name__=='PreparedFullBody']
    backing=sum(6*item.count+131072 for item in banks)
    if (credit.closed or pending is None or allocation<0 or
            used+allocation+backing>pending['allocation']):
        raise Refused('selected_value_original_RAM_overlap','terminal')
    credit.selected_value_allocation=used+allocation

class OwnedMappingBirth:
    def __init__(self,prepared,width,role):
        # Partial original lives on the caller's preowned birth list BEFORE
        # this constructor. Initialize its custody before fallible validation.
        self.mapping=None;self.created=False;self.creation_error=None
        self.registration_error=None;self.current_cut=None
        self.close_attempted=False;self.close_error=None
        self.closed_confirmed=False;self.close_returned=False
        self.creation={'schema':'friday.sol106.actual-mmap-constructor.v1',
            'fd':prepared.fd,'width':width,'access':mmap.ACCESS_WRITE,
            'attempted':False,'returned':False,'original_error':None}
        prepared._owned()
        row=prepared.store.fdjournal._row(prepared.fd)
        if row is None or row['status'] not in ('ACQUIRED','HELD') or not 1<=width<=DOCUMENT_MAX:
            raise Refused('selected_mapping_actual_prepared_row','terminal')
        self.fd=prepared.fd;self.row=row;self.credit=prepared.hold
        self.owner_pid=os.getpid();self.width=width;self.role=role
        self.binding=prepared.binding()
        self.birth=identity9(os.fstat(self.fd))
        self.cuts=[];self.child_reads=0;self.child_allocation=0
        self.borrowed=False
        # Debit before mmap allocation. Mapping is not a newly granted role.
        if self.binding['allocation']<width+131072:
            raise Refused('selected_mapping_original_RAM_before_birth','terminal')
        charge_selected_allocation(self.credit,width+131072)
        self._own_registry=_own_native()
        if self._own_registry is not None:
            self._own_registry.own_mapping('birth',self,None,self.creation,self.cuts,self.row,self.credit)
        # Record the BIRTH identity before mmap, not an allocating registration
        # after return. A returned map is reachable even if metadata later fails.
        key=(os.getpid(),id(self))
        if key in _MAP_BIRTHS:raise Refused('selected_mapping_birth_identity_reuse','terminal')
        _MAP_BIRTHS[key]=self
    def create(self):
        if self.created or self.mapping is not None or os.getpid()!=self.owner_pid:
            raise Refused('selected_mapping_birth_once','terminal')
        self.created=True
        try:
            if self._own_registry is None:
                self.creation['attempted']=True
                self.mapping=mmap.mmap(self.fd,self.width,access=mmap.ACCESS_WRITE)
            else:
                # SAME constructor, now the actual native recipient retains
                # the returned object before any Source metadata operation.
                self.mapping=self._own_registry.own_mapping('create',self,None,
                    self.creation,self.cuts,self.row,self.credit)
            self.creation['returned']=True
        except BaseException as error:
            self.creation_error=error;self.creation['original_error']=error
            raise
        return self.mapping
    def _before_cut(self):
        reads=2*self.width;allocation=2*self.width+131072
        if os.getpid()==self.owner_pid:
            charge_selected_allocation(self.credit,allocation)
            self.credit.commit(reads=reads)
        elif os.getppid()==self.owner_pid:
            # Fork's selected own copy only; NEVER inherited Root debit call.
            if (self.child_reads+reads>self.binding['reads'] or
                    self.child_allocation+allocation>self.binding['allocation']):
                raise Refused('selected_mapping_child_original_envelope','terminal')
            self.child_reads+=reads;self.child_allocation+=allocation
        else:raise Refused('selected_mapping_actual_process','terminal')
    def cut(self,carrier,cohort=None):
        mapping=self.mapping
        if mapping is None or mapping.closed or self.close_attempted:
            raise Refused('selected_mapping_current_live_body','terminal')
        # A bank cannot encode the current image of its OWN changing transport
        # into itself. Its explicitly labelled before-write image stays exact;
        # final current bytes are separately captured before close and native
        # current-phase readback. Other actual mappings get a real current cut.
        if cohort is True or (cohort is None and
                (getattr(carrier,'map',None) is mapping or getattr(carrier,'meta',None) is mapping)):
            return self._cohort_cut_before_effect(carrier, mapping)
        if self._own_registry is not None and os.getpid()==self.owner_pid:
            self._before_cut()
            cut=self._own_registry.own_mapping('cut',self,mapping,None,None,self.row,self.credit)
            self.current_cut=cut
            if not hasattr(carrier,'selected_mapping_cuts'):carrier.selected_mapping_cuts=[]
            carrier.selected_mapping_cuts.append((self,cut))
            return cut
        self._before_cut()
        before=identity9(os.fstat(self.fd));position=mapping.tell()
        if before[:6]!=self.birth[:6] or before[6]!=str(self.width) or len(mapping)!=self.width:
            raise Refused('selected_mapping_backing_birth','terminal')
        view=memoryview(mapping)
        try:
            raw=view.tobytes()
            if view!=memoryview(raw) or identity9(os.fstat(self.fd))!=before or mapping.tell()!=position:
                raise Refused('selected_mapping_full_public_cut_drift','terminal')
        finally:view.release()
        cut={'schema':'friday.sol091.selected-owned-mmap-public-cut.v1',
            'creator_pid':self.owner_pid,'actual_pid':os.getpid(),
            'role':self.role,'backing_birth9':tuple(self.birth),'backing_cut9':tuple(before),
            'actual_row':self.row,'actual_credit':self.credit,
            'width':self.width,'position':position,'access':'WRITE',
            'closed':mapping.closed,'full_bytes':raw,'cohort_before_effect':False}
        self.cuts.append(cut);self.current_cut=cut
        # The carrier retains the actual mapping too, and compares again before
        # sealing. mmap.close() is separate from closing its numeric FD.
        if not hasattr(carrier,'selected_mapping_cuts'):carrier.selected_mapping_cuts=[]
        carrier.selected_mapping_cuts.append((self,cut))
        return cut
    def require_cut(self,cut):
        if (self.mapping is not None and self.mapping.closed
                and self.close_attempted and self.closed_confirmed and self.close_error is None):
            from existing_root_caller import current_native_owner, _require_frozen_mapping_cut
            receiver = current_native_owner()
            state = receiver.retired_mapping_state(self.mapping)
            if state is None:
                raise Refused('selected_mapping_closed_full_custody_missing','terminal')
            receiver.require_retired_mapping_state(state)
            if not any(_require_frozen_mapping_cut(ledger)[1] is cut
                       for ledger in state['full_cut_ledgers']):
                raise Refused('selected_mapping_closed_cut_identity','terminal')
            return
        if not any(item is cut for item in self.cuts) or self.mapping is None or self.mapping.closed:
            raise Refused('selected_mapping_actual_cut_custody','terminal')
        if cut.get('cohort_before_effect') is True:
            retained=getattr(self,'before_effect_raw',None)
            recorded=tuple(cut['backing_cut9'])[:6]
            if (retained is not cut['full_bytes'] or type(retained) is not bytes
                    or len(retained)!=self.width or len(self.mapping)!=self.width
                    or self.mapping.tell()!=cut['position'] or tuple(self.birth)[:6]!=recorded):
                raise Refused('selected_mapping_original_alias_changed','terminal')
            return
        if self._own_registry is not None and os.getpid()==self.owner_pid:
            if self._own_registry.own_mapping('require-cut',self,self.mapping,
                    cut,None,self.row,self.credit) is not True:
                raise Refused('selected_mapping_native_current_cut_disagrees','terminal')
            return
        self._before_cut()
        view=memoryview(self.mapping)
        try:
            if (view!=memoryview(cut['full_bytes']) or self.mapping.tell()!=cut['position'] or
                    tuple(identity9(os.fstat(self.fd)))!=cut['backing_cut9']):
                raise Refused('selected_mapping_original_alias_changed','terminal')
        finally:view.release()
    def final_current_cut_before_close(self):
        if self.close_attempted or self.mapping is None or self.mapping.closed:
            raise Refused('selected_mapping_before_close_live_only','terminal')
        if self._own_registry is not None and os.getpid()==self.owner_pid:
            self._before_cut()
            cut=self._own_registry.own_mapping('cut',self,self.mapping,None,None,self.row,self.credit)
            self.current_cut=cut;self.final_current_cut=cut
            return cut
        self._before_cut()
        before=identity9(os.fstat(self.fd));position=self.mapping.tell()
        if before[:6]!=self.birth[:6] or before[6]!=str(self.width) or len(self.mapping)!=self.width:
            raise Refused('selected_mapping_terminal_backing_identity','terminal')
        view=memoryview(self.mapping)
        try:
            raw=view.tobytes()
            if view!=memoryview(raw) or identity9(os.fstat(self.fd))!=before or self.mapping.tell()!=position:
                raise Refused('selected_mapping_terminal_full_drift','terminal')
        finally:view.release()
        cut={'schema':'friday.sol091.selected-owned-mmap-public-cut.v1',
            'creator_pid':self.owner_pid,'actual_pid':os.getpid(),
            'role':self.role,'backing_birth9':tuple(self.birth),'backing_cut9':tuple(before),
            'actual_row':self.row,'actual_credit':self.credit,'width':self.width,
            'position':position,'access':'WRITE','closed':False,'full_bytes':raw,
            'cohort_before_effect':False}
        self.cuts.append(cut);self.final_current_cut=cut;self.current_cut=cut
        return cut
    def close_actual_once(self):
        if self._own_registry is None:self.mapping.close()
        else:self._own_registry.own_mapping('close',self,self.mapping,None,None,self.row,self.credit)
        self.close_returned=True
    def retire(self,receiver,receipt):
        # Full byte/cut/row ownership must reach actual SAME native receiver,
        # then one mapping.close. Closing the backing FD alone is never enough.
        if self.close_attempted:return self.closed_confirmed
        receiver.require_transfer(receipt)
        receiver.accept_mapping(self,receipt)
        self.close_attempted=True
        try:self.close_actual_once()
        except BaseException as error:
            self.close_error=error
            receiver.retain_after_document(error,'selected-mapping-close:'+self.role)
            return False
        self.closed_confirmed=self.mapping.closed
        if not self.closed_confirmed:return False
        receiver.mapping_closed(self,receipt)
        for key,value in tuple(_MAP_BIRTHS.items()):
            if value is self:_MAP_BIRTHS.pop(key)
        return True
    def retire_publication(self, receiver, transfer):
        if self.close_attempted:
            return self.closed_confirmed and self.close_error is None
        receiver.require_publication_transfer(transfer)
        receiver.accept_publication_mapping(self, transfer)
        self.close_attempted = True
        try:
            self.close_actual_once()
        except BaseException as error:
            self.close_error = error
            receiver.retain_after_document(error, 'publication-mapping-close:' + self.role)
            return False
        self.closed_confirmed = self.mapping.closed
        if not self.closed_confirmed:
            return False
        receiver.publication_mapping_closed(self, transfer)
        for key, value in tuple(_MAP_BIRTHS.items()):
            if value is self:
                _MAP_BIRTHS.pop(key)
        return True

    def _cohort_cut_before_effect(self,carrier,mapping):
        prior=getattr(self,'before_effect_cut',None)
        if prior is not None:
            return prior
        self._before_cut()
        before=identity9(os.fstat(self.fd));position=mapping.tell()
        if before[:6]!=self.birth[:6] or before[6]!=str(self.width) or len(mapping)!=self.width:
            raise Refused('selected_mapping_backing_birth','terminal')
        view=memoryview(mapping)
        try:
            raw=view.tobytes()
            if view!=memoryview(raw) or identity9(os.fstat(self.fd))!=before or mapping.tell()!=position:
                raise Refused('selected_mapping_full_public_cut_drift','terminal')
        finally:view.release()
        self.before_effect_raw=raw
        cut={'schema':'friday.sol091.selected-owned-mmap-public-cut.v1',
            'creator_pid':self.owner_pid,'actual_pid':os.getpid(),
            'role':self.role,'backing_birth9':tuple(self.birth),'backing_cut9':tuple(before),
            'actual_row':self.row,'actual_credit':self.credit,
            'width':self.width,'position':position,'access':'WRITE',
            'closed':mapping.closed,'full_bytes':raw,'cohort_before_effect':True}
        self.cuts.append(cut)
        self.before_effect_cut=cut
        if not hasattr(carrier,'selected_mapping_cuts'):carrier.selected_mapping_cuts=[]
        carrier.selected_mapping_cuts.append((self,cut))
        if not hasattr(carrier,'cohort_before_effect'):carrier.cohort_before_effect=[]
        carrier.cohort_before_effect.append(cut)
        return cut

def own_mapping(value):
    if type(value) is not mmap.mmap:return None
    found=[birth for birth in _MAP_BIRTHS.values() if birth.mapping is value]
    if len(found)!=1:return None
    return found[0]

def public_stock_cut(value,pin_for):
    """Exact selected PUBLIC namespace only. Private/native completeness RED.

    This is not a filename/hash substitute for full original mutable state.
    Values that need unavailable native state keep unsupported entries in the
    enclosing arena, even when their public fields are now represented.
    """
    if type(value) is types.ModuleType:
        name=value.__name__
        if name not in _PUBLIC_STOCK or sys.modules.get(name) is not value:return None
        path=getattr(value,'__file__',None)
        pin=pin_for(path) if type(path) is str else None
        return {'kind':'module','module':name,'actual_namespace':value.__dict__,
            'actual_image_pin':pin,'native_state_complete':False}
    if type(value) in (types.BuiltinFunctionType,types.BuiltinMethodType):
        module=getattr(value,'__module__',None)
        bound=getattr(value,'__self__',None)
        if module not in _PUBLIC_STOCK and not (type(bound) is types.ModuleType and bound.__name__ in _PUBLIC_STOCK):
            return None
        owner=sys.modules.get(module) if module in _PUBLIC_STOCK else bound
        if type(owner) is not types.ModuleType:return None
        path=getattr(owner,'__file__',None)
        return {'kind':'builtin','module':owner.__name__,'name':value.__name__,
            'qualname':value.__qualname__,'actual_self':bound,
            'actual_image_pin':pin_for(path) if type(path) is str else None,
            'native_state_complete':False}
    return None

def require_selected_mapping_cuts(carrier):
    for birth,cut in getattr(carrier,'selected_mapping_cuts',()):
        birth.require_cut(cut)
