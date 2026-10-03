"""Prescribed SELF-OWNED mmap/public-stock cuts, never a heap inspector.

Birth records below are Source data, not native authority. Only the actual
Root-prepared FD/credit may create a mapping, before fork. Full bytes and actual
row aliases travel through the SAME full-value codec used by both receivers.
Unregistered/unstable/self-writing mappings remain explicit CODE refusal.
"""
import os
import sys
import types
import mmap
from common import Refused, DOCUMENT_MAX
from custody import identity9

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
        prepared._owned()
        row=prepared.store.fdjournal._row(prepared.fd)
        if row is None or row['status'] not in ('ACQUIRED','HELD') or not 1<=width<=DOCUMENT_MAX:
            raise Refused('selected_mapping_actual_prepared_row','terminal')
        self.fd=prepared.fd;self.row=row;self.credit=prepared.hold
        self.owner_pid=os.getpid();self.width=width;self.role=role
        self.binding=prepared.binding();self.mapping=None;self.created=False
        self.close_attempted=False;self.close_error=None;self.closed_confirmed=False
        self.birth=identity9(os.fstat(self.fd))
        self.cuts=[];self.child_reads=0;self.child_allocation=0
        self.borrowed=False
        # Debit before mmap allocation. Mapping is not a newly granted role.
        if self.binding['allocation']<width+131072:
            raise Refused('selected_mapping_original_RAM_before_birth','terminal')
        charge_selected_allocation(self.credit,width+131072)
    def create(self):
        if self.created or self.mapping is not None or os.getpid()!=self.owner_pid:
            raise Refused('selected_mapping_birth_once','terminal')
        self.created=True
        self.mapping=mmap.mmap(self.fd,self.width,access=mmap.ACCESS_WRITE)
        # Exact object is strongly held on this already retained birth record
        # before registration. Failed registration cannot lose the mapping.
        key=(os.getpid(),id(self.mapping))
        if key in _MAP_BIRTHS:raise Refused('selected_mapping_identity_reuse','terminal')
        _MAP_BIRTHS[key]=self
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
    def cut(self,carrier):
        mapping=self.mapping
        if mapping is None or mapping.closed or self.close_attempted:
            raise Refused('selected_mapping_current_live_body','terminal')
        if getattr(carrier,'map',None) is mapping or getattr(carrier,'meta',None) is mapping:
            # A bank cannot overwrite its own full physical cut while claiming
            # stable alias equality. No sparse/digest projection or extra cap.
            raise Refused('selected_mapping_self_encoding_physical_alias_CODE','terminal')
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
            'closed':mapping.closed,'full_bytes':raw}
        self.cuts.append(cut)
        # The carrier retains the actual mapping too, and compares again before
        # sealing. mmap.close() is separate from closing its numeric FD.
        if not hasattr(carrier,'selected_mapping_cuts'):carrier.selected_mapping_cuts=[]
        carrier.selected_mapping_cuts.append((self,cut))
        return cut
    def require_cut(self,cut):
        if not any(item is cut for item in self.cuts) or self.mapping is None or self.mapping.closed:
            raise Refused('selected_mapping_actual_cut_custody','terminal')
        self._before_cut()
        view=memoryview(self.mapping)
        try:
            if (view!=memoryview(cut['full_bytes']) or self.mapping.tell()!=cut['position'] or
                    tuple(identity9(os.fstat(self.fd)))!=cut['backing_cut9']):
                raise Refused('selected_mapping_original_alias_changed','terminal')
        finally:view.release()
    def retire(self,receiver,receipt):
        # Full byte/cut/row ownership must reach actual SAME native receiver,
        # then one mapping.close. Closing the backing FD alone is never enough.
        if self.close_attempted:return self.closed_confirmed
        receiver.require_transfer(receipt)
        receiver.accept_mapping(self,receipt)
        self.close_attempted=True
        try:self.mapping.close()
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
