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

def own_error_record_serial(value):
    native=_own_native()
    return None if native is None else native.own_error_record_serial(value)

def own_error_record_builtin(serial,value):
    native=_own_native()
    if native is None or native.own_error_record_check(serial,value) is not True:
        raise Refused('actual_native_error_record_provenance','terminal')
    return native.own_error_record_builtin(serial,value) is True

def retain_error_record_arena(carrier,arena,bindings):
    if not bindings:return
    native=_own_native()
    if native is None:raise Refused('actual_error_record_parent_reader','terminal')
    # SAME original cumulative pool BEFORE metadata list/tuple/attribute work.
    # Actual producer rows already retain all originals if publication fails.
    native.own_prepare(131072+len(bindings)*1024,len(bindings)*4096+8192)
    prior=getattr(carrier,'error_record_arenas',None)
    if prior is None:
        prior=[];carrier.error_record_arenas=prior
    prior.append((arena,tuple(bindings))) # exact arena identity, no overwrite

def _error_record_bindings(carrier,arena):
    native=_own_native()
    if native is None:raise Refused('actual_error_record_parent_reader','terminal')
    groups=getattr(carrier,'error_record_arenas',())
    native.own_prepare(4096,len(groups)*256+8192)
    rows=[bindings for actual,bindings in groups if actual is arena]
    if len(rows)!=1:raise Refused('actual_error_record_arena_identity','terminal')
    return native,rows[0]

def require_error_type_support(carrier,arena,serial,node):
    native,bindings=_error_record_bindings(carrier,arena)
    native.own_prepare(4096,len(bindings)*512+8192)
    rows=[row for row in bindings if row[0]==serial and row[3][2]==node and row[4] is True]
    if len(rows)!=1:raise Refused('actual_error_type_support_binding','terminal')
    record=rows[0][1]
    if (native.own_error_record_check(serial,record) is not True
            or native.own_error_record_builtin(serial,record) is not True):
        raise Refused('actual_error_type_support_producer','terminal')
    return record['original_type'] if type(record) is dict else record[2]
    # Native check joins this exact original to Py_TYPE(error), never names.

def require_error_record_binding(carrier,arena,node,body,nodes):
    native,bindings=_error_record_bindings(carrier,arena)
    native.own_prepare(4096,len(bindings)*512+8192)
    serial=body['producer_serial'];members=tuple(body[name] for name in ('phase','error','type','traceback'))
    rows=[row for row in bindings if row[0]==serial and row[2]==node and row[3]==members]
    if len(rows)!=1:raise Refused('actual_error_record_full_alias_binding','terminal')
    record=rows[0][1];builtin=rows[0][4]
    if (native.own_error_record_check(serial,record) is not True
            or (native.own_error_record_builtin(serial,record) is True) is not builtin):
        raise Refused('actual_error_record_original_type_identity','terminal')
    type_node=nodes[members[2]]
    actual_type=record['original_type'] if type(record) is dict else record[2]
    if type(record) is dict and record['original_error'] is None:
        if actual_type is not None or any(nodes[members[j]]!=['none',None] for j in (1,2,3)):
            raise Refused('actual_error_cell_absent_triple','terminal')
        return
    if builtin:
        if type_node[0]=='runtime-support':
            if native.own_error_record_check(serial,record,type_node[1]['producer_serial']) is not True:
                raise Refused('actual_error_record_runtime_support_same_type','terminal')
        elif type_node[0]=='native-error-type-support':
            actual_type=require_error_type_support(carrier,arena,type_node[1]['producer_serial'],members[2])
            if actual_type is not (record['original_type'] if type(record) is dict else record[2]):
                raise Refused('actual_error_record_same_stock_type','terminal')
        else:
            raise Refused('actual_error_record_stock_TYPE_role','terminal')
    elif type_node[0]!='qualified-class':
        # Unknown/unbound types and real Source classes are NOT SUPPORT.
        raise Refused('actual_error_record_full_Source_class_DATA','terminal')

def require_error_cell_binding(carrier,arena,node,body,nodes):
    # Retained full dictionary keys/values, not a replacement tuple or digest.
    native,bindings=_error_record_bindings(carrier,arena)
    native.own_prepare(16384,len(bindings)*512+32768)
    fields={}
    for key,value in body['pairs']:
        if nodes[key][0]!='str' or nodes[key][1] in fields:
            raise Refused('actual_error_cell_keys','terminal')
        fields[nodes[key][1]]=value
    names=('phase','original_type','original_error','original_traceback','actual_document',
        'saved','syscall_attempted','syscall_rc','syscall_errno','actual_written')
    if set(fields)!=set(names) or len(body['pairs'])!=10:
        raise Refused('actual_error_cell_full_fields','terminal')
    linked={'producer_serial':body['producer_serial'],'phase':fields['phase'],
        'error':fields['original_error'],'type':fields['original_type'],'traceback':fields['original_traceback']}
    require_error_record_binding(carrier,arena,node,linked,nodes)
    members=tuple(linked[name] for name in ('phase','error','type','traceback'))
    rows=[row for row in bindings if row[0]==body['producer_serial'] and row[2]==node and row[3]==members]
    if len(rows)!=1 or type(rows[0][1]) is not dict or len(rows[0])!=6:
        raise Refused('actual_error_cell_dictionary_producer','terminal')
    aliases=rows[0][5]
    if (type(aliases) is not tuple or len(aliases)!=10
            or {name for name,original_node in aliases}!=set(names)
            or any(fields[name]!=original_node for name,original_node in aliases)):
        raise Refused('actual_error_cell_all_original_field_nodes','terminal')
    record=rows[0][1]
    if nodes[fields['phase']]!=['str',record['phase']]:
        raise Refused('actual_error_cell_original_phase','terminal')
    for name in ('saved','syscall_attempted','syscall_rc','syscall_errno','actual_written'):
        if nodes[fields[name]]!=['int',str(record[name])]:
            raise Refused('actual_error_cell_original_scalar','terminal')
    document=record['actual_document'];document_node=nodes[fields['actual_document']]
    if document is None:
        if document_node!=['none',None]:
            raise Refused('actual_error_cell_original_document_absence','terminal')
    else:
        # The actual native after-document producer retains exact bytes;
        # neither another equal-looking node nor a digest is that ref-cut.
        if type(document) is not bytes or document_node[0]!='bytes':
            raise Refused('actual_error_cell_original_document_bytes','terminal')
        span=document_node[1]
        if (type(span) is not dict or set(span)!=set(('offset','bytes'))
                or type(span['offset']) is not int or span['offset']<0
                or type(span['bytes']) is not int or span['bytes']!=len(document)):
            raise Refused('actual_error_cell_original_document_span','terminal')
        native.own_prepare(len(document)+131072,2*len(document)+8192)
        physical=carrier.require(arena['physical_body'])
        start=span['offset'];width=span['bytes']
        if (type(physical) is not bytes or start>len(physical)
                or width>len(physical)-start
                or physical[start:start+width]!=document):
            raise Refused('actual_error_cell_original_document_full_body','terminal')


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
        # The caller retains this partial original BEFORE this constructor.
        # The creation dict is allocated below, not a preowned native slot:
        # failure before those first fields remains an explicit outer prefix.
        self.mapping=None;self.created=False;self.creation_error=None
        self.registration_error=None;self.current_cut=None
        self.close_attempted=False;self.close_error=None
        self.closed_confirmed=False;self.close_returned=False
        self.metadata_error=None;self.state_complete=False
        self.creation={'schema':'friday.sol106.actual-mmap-constructor.v1',
            'fd':None,'width':width,'access':mmap.ACCESS_WRITE,
            'attempted':False,'returned':False,'returned_mapping':None,
            'original_error':None,'metadata_error':None,'prebirth_error':None,
            'row':None,'credit':None,'birth':None,'state_complete':False}
        try:
            prepared._owned()
            row=prepared.store.fdjournal._row(prepared.fd)
            if row is None or row['status'] not in ('ACQUIRED','HELD') or not 1<=width<=DOCUMENT_MAX:
                raise Refused('selected_mapping_actual_prepared_row','terminal')
            self.fd=prepared.fd;self.row=row;self.credit=prepared.hold
            self.owner_pid=os.getpid();self.width=width;self.role=role
            self.binding=prepared.binding()
            self.creation['fd']=self.fd
            self.creation['row']=row
            self.creation['credit']=self.credit
            if self.binding['allocation']<width+131072:
                raise Refused('selected_mapping_original_RAM_before_birth','terminal')
            charge_selected_allocation(self.credit,width+131072)
            if row.get('status') not in ('ACQUIRED','HELD') or row.get('attempted_close') not in (None,'NOT_ATTEMPTED'):
                raise Refused('selected_mapping_keeper_still_owned','terminal')
            self.birth=identity9(os.fstat(self.fd))
            self.creation['birth']=self.birth
            self.cuts=[];self.child_reads=0;self.child_allocation=0
            self.borrowed=False
            self._own_registry=_own_native()
            if self._own_registry is not None:
                self._own_registry.own_mapping('birth',self,None,self.creation,self.cuts,self.row,self.credit)
            key=(os.getpid(),id(self))
            if key in _MAP_BIRTHS:raise Refused('selected_mapping_birth_identity_reuse','terminal')
            _MAP_BIRTHS[key]=self
        except BaseException as error:
            self.creation['prebirth_error']=error
            self.creation['state_complete']=False
            self.state_complete=False
            self.creation_error=error
            raise
    def create(self):
        if self.created or self.mapping is not None or self.creation.get('returned') is True or os.getpid()!=self.owner_pid:
            raise Refused('selected_mapping_birth_once','terminal')
        if self.creation.get('prebirth_error') is not None:
            raise Refused('selected_mapping_prebirth_incomplete','terminal')
        try:
            if self._own_registry is None:
                self.creation['attempted']=True
                result=mmap.mmap(self.fd,self.width,access=mmap.ACCESS_WRITE)
                self.mapping=result
                self.creation['returned_mapping']=result
                self.creation['returned']=True
            else:
                # Native retains the constructor return in its row and in this
                # preowned record before Source metadata below can fail.
                result=self._own_registry.own_mapping('create',self,None,
                    self.creation,self.cuts,self.row,self.credit)
                if result is None:
                    result=self._own_registry.own_mapping('retained',self,None,
                        self.creation,self.cuts,self.row,self.credit)
                if self.mapping is None and result is not None:
                    self.mapping=result
                    self.creation['returned_mapping']=result
                    self.creation['returned']=True
                if (result is None or self.mapping is not result
                        or self.creation.get('returned_mapping') is not result
                        or self.creation.get('returned') is not True):
                    raise Refused('selected_mapping_returned_alias','terminal')
            self.created=True
            self.state_complete=True
            self.creation['state_complete']=True
        except BaseException as error:
            recovery_unconfirmed=False
            if self.mapping is None and self._own_registry is not None:
                held=None
                try:
                    held=self._own_registry.own_mapping('retained',self,None,
                        self.creation,self.cuts,self.row,self.credit)
                except BaseException:
                    held=None
                    recovery_unconfirmed=True
                if held is not None:
                    self.mapping=held
                    self.creation['returned_mapping']=held
                    self.creation['returned']=True
            if self.mapping is not None and self.creation.get('returned_mapping') is None:
                self.creation['returned_mapping']=self.mapping
                self.creation['returned']=True
            published=self.creation.get('returned_mapping')
            if (self.mapping is None and published is not None
                    and self.creation.get('returned') is True):
                self.mapping=published
            self.state_complete=False
            self.creation['state_complete']=False
            returned=self.creation.get('returned') is True or self.mapping is not None
            if recovery_unconfirmed and not returned:
                # The recovery query missed. Keep the first error and do not
                # relabel that returned constructor as a factory failure.
                pass
            elif returned:
                self.metadata_error=error
                self.creation['metadata_error']=error
            else:
                self.creation_error=error
                self.creation['original_error']=error
            raise
        return self.mapping
    def _require_owned_body_fd(self):
        row=self.row
        if (type(row) is not dict or row.get('status') not in ('ACQUIRED','HELD')
                or row.get('attempted_close') not in (None,'NOT_ATTEMPTED')):
            raise Refused('selected_mapping_keeper_still_owned','terminal')
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
        if mapping is None or self.close_attempted or mapping.closed:
            if self.close_attempted and not self.closed_confirmed:
                raise Refused('selected_mapping_close_outcome_unknown','terminal')
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
        self._require_owned_body_fd()
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
        if self.close_attempted and not (self.closed_confirmed and self.close_error is None and self.close_returned):
            raise Refused('selected_mapping_close_outcome_unknown','terminal')
        if (self.mapping is not None and self.closed_confirmed and self.close_attempted
                and self.close_error is None and self.close_returned and self.mapping.closed):
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
        if not any(item is cut for item in self.cuts) or self.mapping is None or self.close_attempted:
            raise Refused('selected_mapping_actual_cut_custody','terminal')
        if cut.get('cohort_before_effect') is True:
            retained=getattr(self,'before_effect_raw',None)
            recorded=tuple(cut['backing_cut9'])[:6]
            self._require_owned_body_fd()
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
        self._require_owned_body_fd()
        view=memoryview(self.mapping)
        try:
            if (view!=memoryview(cut['full_bytes']) or self.mapping.tell()!=cut['position'] or
                    tuple(identity9(os.fstat(self.fd)))!=cut['backing_cut9']):
                raise Refused('selected_mapping_original_alias_changed','terminal')
        finally:view.release()
    def final_current_cut_before_close(self):
        if self.close_attempted or self.mapping is None or self.mapping.closed:
            if self.close_attempted and not self.closed_confirmed:
                raise Refused('selected_mapping_close_outcome_unknown','terminal')
            raise Refused('selected_mapping_before_close_live_only','terminal')
        if self._own_registry is not None and os.getpid()==self.owner_pid:
            self._before_cut()
            cut=self._own_registry.own_mapping('cut',self,self.mapping,None,None,self.row,self.credit)
            self.current_cut=cut;self.final_current_cut=cut
            return cut
        self._before_cut()
        self._require_owned_body_fd()
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
        if self.close_returned or self.close_error is not None or self.closed_confirmed:
            raise Refused('selected_mapping_close_outcome_unknown','terminal')
        if self._own_registry is None:self.mapping.close()
        else:self._own_registry.own_mapping('close',self,self.mapping,None,None,self.row,self.credit)
        self.close_returned=True
    def retire(self,receiver,receipt):
        # Full byte/cut/row ownership must reach actual SAME native receiver,
        # then one mapping.close. Closing the backing FD alone is never enough.
        if self.close_attempted:return self.closed_confirmed and self.close_error is None
        receiver.require_transfer(receipt)
        if self.mapping is None:
            return receiver.accept_unmapped_birth(self,receipt)
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
        if self.mapping is None:
            return receiver.accept_publication_unmapped_birth(self,transfer)
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
        if self.close_attempted:
            raise Refused('selected_mapping_close_outcome_unknown','terminal')
        self._before_cut()
        if self._own_registry is not None and os.getpid()==self.owner_pid:
            cut=self._own_registry.own_mapping('cohort-cut',self,mapping,None,None,self.row,self.credit)
            if cut.get('cohort_before_effect') is not True or type(cut.get('full_bytes')) is not bytes:
                raise Refused('selected_mapping_cohort_keeper_cut','terminal')
            self.before_effect_raw=cut['full_bytes']
            self.before_effect_cut=cut
            if not hasattr(carrier,'selected_mapping_cuts'):carrier.selected_mapping_cuts=[]
            carrier.selected_mapping_cuts.append((self,cut))
            if not hasattr(carrier,'cohort_before_effect'):carrier.cohort_before_effect=[]
            carrier.cohort_before_effect.append(cut)
            return cut
        self._require_owned_body_fd()
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

# Selected secondary own cuts. The Python list is the Source registry alias.
# Native rows live in the same RootStorage, not in a second native pool.
# Bodies are materialized before a failing codec. A later refusal does not clear the alias list.
_SECONDARY_SCHEMA='friday.lab942.secondary-own-cut.v1'
_SECONDARY_KINDS=('frame','code','source','buffer','traceback','error','error_handler')
_SECONDARY_CAP=65536
_SECONDARY_WALK_CAP=262144
_SECONDARY_CUTS=[]
_SECONDARY_BY_ID={}

def secondary_own_records():
    return _SECONDARY_CUTS

_U64 = (1 << 64) - 1

def _plain_int(value):
    return type(value) is int and not isinstance(value, bool)

def _u64(value):
    return _plain_int(value) and 0 <= value <= _U64

def _room(used, amount, ceiling):
    if not _u64(used) or not _u64(amount) or not _u64(ceiling):
        return False
    if used > ceiling or amount > ceiling - used:
        return False
    return True

def _scale(total, factor, extra):
    if not _u64(total) or not _plain_int(factor) or factor < 0 or not _u64(extra):
        return None
    if total and factor > _U64 // total:
        return None
    product = total * factor
    if product > _U64 - extra:
        return None
    return product + extra

def _secondary_before(reads, allocation, carrier):
    """Debit the caller's already preowned carrier before a scan or copy.

    Parent Root pays reads and allocation on its existing credit and, when
    the same Root native pool is present, one combined prepare. A bound
    child pays only its copied local quota. A stock or control channel and
    a fork owner pay the binding they already hold. No birth is inferred.
    """
    if not _u64(reads) or not _u64(allocation) or carrier is None:
        raise Refused('secondary_preown_original_envelope', 'terminal')
    name = type(carrier).__name__
    if name == 'PreparedFullBody':
        carrier._owned()
        native = _own_native()
        if native is not None and (reads or allocation):
            native.own_prepare(allocation, reads)
        charge_selected_allocation(carrier.hold, allocation)
        if reads:
            carrier.hold.commit(reads=reads)
        return
    if name == 'PrefixBodyMailbox':
        pid = os.getpid()
        if carrier.bound and pid == carrier.pid and pid != carrier.parent_pid:
            quota = carrier.child_quota
            used = carrier.child_used
            if (type(quota) is not dict or type(used) is not dict or
                    not _room(used.get('reads'), reads, quota.get('reads')) or
                    not _room(used.get('allocation'), allocation, quota.get('allocation'))):
                raise Refused('secondary_preown_child_original_envelope', 'terminal')
            carrier._quota_debit(allocation=allocation, reads=reads)
            return
        if pid == carrier.parent_pid and carrier.publication_credit is not None:
            native = _own_native()
            if native is not None and (reads or allocation):
                native.own_prepare(allocation, reads)
            charge_selected_allocation(carrier.publication_credit, allocation)
            if reads:
                carrier.publication_credit.commit(reads=reads)
            return
        raise Refused('secondary_preown_original_envelope', 'terminal')
    if name == 'NativeMemoryBody':
        if reads or allocation:
            receiver = carrier.receiver
            receiver.native.before_scan(receiver.context, reads, allocation)
        return
    if name in ('StockChannel', 'ControlChannel', 'ForkOwner'):
        if name == 'ForkOwner':
            binding = carrier.body_binding
            if type(binding) is not dict:
                raise Refused('secondary_preown_original_envelope', 'terminal')
            body_reads = _scale(carrier.body_count, 3, carrier.body_transport)
            body_alloc = 0 if carrier.body_count == 0 else _scale(carrier.body_count, 6, 131072)
        else:
            binding = carrier.body_begin()
            if type(binding) is not dict:
                raise Refused('secondary_preown_original_envelope', 'terminal')
            body_reads = _scale(carrier.body_count, 4, 0)
            body_alloc = 0 if carrier.body_count == 0 else _scale(carrier.body_count, 6, 131072)
        scan_reads = getattr(carrier, 'secondary_scan_reads', 0)
        scan_alloc = getattr(carrier, 'secondary_scan_allocation', 0)
        if (body_reads is None or body_alloc is None or
                not _room(body_reads, scan_reads, _U64) or
                not _room(body_alloc, scan_alloc, _U64) or
                not _room(body_reads + scan_reads, reads, binding.get('reads')) or
                not _room(body_alloc + scan_alloc, allocation, binding.get('allocation'))):
            raise Refused('secondary_preown_original_envelope', 'terminal')
        carrier.secondary_scan_reads = scan_reads + reads
        carrier.secondary_scan_allocation = scan_alloc + allocation
        return
    raise Refused('secondary_preown_original_envelope', 'terminal')

def _secondary_store(kind, original, edges, scalars, carrier):
    if original is None or kind not in _SECONDARY_KINDS:
        raise Refused('secondary_own_cut_alias','terminal')
    if type(edges) is not tuple or type(scalars) is not tuple or not edges:
        raise Refused('secondary_pointer_only_cut','terminal')
    if any(type(item) is not int or type(item) is bool for item in scalars):
        raise Refused('secondary_own_cut_scalar','terminal')
    edge_n = len(edges) + len(scalars) + 1
    record_allocation = (len(edges) + 1) * 64
    _secondary_before(edge_n, record_allocation, carrier)
    prior=_SECONDARY_BY_ID.get(id(original))
    if prior is not None:
        if prior['original'] is not original or prior['kind']!=kind:
            raise Refused('secondary_own_cut_alias','terminal')
        return prior
    if len(_SECONDARY_CUTS)>=_SECONDARY_CAP:
        raise Refused('secondary_own_cut_capacity','terminal')
    native=_own_native()
    if native is not None:
        stored=native.own_secondary_cut((_SECONDARY_SCHEMA, kind, original, edges, scalars))
        if stored is None:
            raise Refused('secondary_own_cut_capacity','terminal')
        if stored is not original:
            raise Refused('secondary_own_cut_alias','terminal')
    record={'schema':_SECONDARY_SCHEMA,'kind':kind,'original':original,
        'edges':edges,'scalars':scalars}
    _SECONDARY_CUTS.append(record)
    _SECONDARY_BY_ID[id(original)]=record
    return record

def secondary_frame_locals(frame, carrier):
    """Exact locals dict. A proxy is materialized once, before the terminal reader."""
    row=_SECONDARY_BY_ID.get(id(frame))
    if row is not None and row['kind']=='frame' and len(row['edges'])>1 and type(row['edges'][1]) is dict:
        return row['edges'][1]
    _secondary_before(64, 256, carrier)
    raw=frame.f_locals
    if type(raw) is dict:
        return raw
    built={}
    for key in raw:
        _secondary_before(64, 256, carrier)
        built[key]=raw[key]
    return built

def retain_secondary_error_handler(handler, errors, carrier):
    """Capture a possible ordinary re-raise in the actual retained handler.

    This is exception metadata, not process/contextvars state. First error
    cuts are immutable. The handler and its complete pre-raise context chain
    are new required DATA; a later reader must also see a new TB prefix.
    Native registration independently observes the actual handled exception.
    """
    if sys.exception() is not handler:
        return
    count=len(errors)
    reads=_scale(count, 64, 0)
    allocation=_scale(count, 128, 0)
    bound=len(_SECONDARY_CUTS)
    chain_reads=_scale(bound, 2 * 64, 0)
    chain_allocation=_scale(bound, 128, 0)
    if None in (reads, allocation, chain_reads, chain_allocation):
        raise Refused('secondary_preown_original_envelope', 'terminal')
    _secondary_before(reads, allocation, carrier)
    _secondary_before(chain_reads, chain_allocation, carrier)
    chain=[];seen=set();cursor=handler
    while cursor is not None and id(cursor) not in seen:
        if len(chain)>=bound or not isinstance(cursor, BaseException):
            raise Refused('secondary_error_handler_chain', 'terminal')
        seen.add(id(cursor))
        context=cursor.__context__
        chain.append((cursor, context));cursor=context
    chain=tuple(chain)
    for error in errors:
        if error is handler:
            continue
        row=_SECONDARY_BY_ID.get(id(error))
        if row is None or row['original'] is not error or row['kind']!='error':
            raise Refused('secondary_error_handler_original', 'terminal')
        _secondary_before(5 * 64, 6 * 64, carrier)
        witness=(error, handler, error.__context__, error.__traceback__, chain)
        _secondary_store('error_handler', witness, witness, (), carrier)

def preown_secondary_selected(roots, source_modules=(), body_carrier=None):
    """Walk selected aliases and store exact bodies before the terminal codec.

    Owned frame globals are entered through the same namespace relation the
    arena uses. Genuine foreign frame globals stay on the cut and are not
    entered. Builtins stay unentered. An unproven namespace is not walked.
    """
    root_count=len(roots)
    _secondary_before(root_count, root_count * 64, body_carrier)
    stack=list(roots)
    seen=set()
    allowed=frozenset(source_modules)
    while stack:
        obj=stack.pop()
        if obj is None:
            continue
        ident=id(obj)
        if ident in seen:
            continue
        if len(seen)>=_SECONDARY_WALK_CAP:
            raise Refused('secondary_preown_capacity','terminal')
        _secondary_before(64, 64, body_carrier)
        seen.add(ident)
        kind=type(obj)
        if kind is types.FrameType:
            _secondary_before(5 * 64, 5 * 64, body_carrier)
            local_body=secondary_frame_locals(obj, body_carrier)
            if type(local_body) is not dict:
                raise Refused('secondary_frame_locals_body','terminal')
            trace=obj.f_trace
            globals_obj=obj.f_globals
            edges=(obj.f_code, local_body, globals_obj, trace, obj.f_builtins)
            scalars=(int(obj.f_lasti), 1, int(obj.f_lineno),
                1 if obj.f_trace_lines else 0, 1 if obj.f_trace_opcodes else 0)
            _secondary_store('frame', obj, edges, scalars, body_carrier)
            stack.append(obj.f_code)
            stack.append(local_body)
            if own_binding(globals_obj) is not None:
                stack.append(globals_obj)
            if trace is not None:
                stack.append(trace)
        elif kind is types.CodeType:
            _secondary_before(11 * 64, 11 * 64, body_carrier)
            code_bytes=obj.co_code
            if type(code_bytes) is not bytes:
                raise Refused('secondary_code_body','terminal')
            _secondary_before(len(code_bytes), len(code_bytes), body_carrier)
            edges=(code_bytes, obj.co_consts, obj.co_names, obj.co_varnames,
                obj.co_filename, obj.co_name, obj.co_qualname, obj.co_linetable,
                obj.co_exceptiontable, obj.co_freevars, obj.co_cellvars)
            scalars=(int(obj.co_argcount), int(obj.co_posonlyargcount),
                int(obj.co_kwonlyargcount), int(obj.co_nlocals), int(obj.co_stacksize),
                int(obj.co_flags), int(obj.co_firstlineno))
            _secondary_store('code', obj, edges, scalars, body_carrier)
            for edge in edges:
                if edge is not None:
                    stack.append(edge)
        elif kind is memoryview:
            nbytes=obj.nbytes
            if not _u64(nbytes):
                raise Refused('secondary_preown_original_envelope', 'terminal')
            _secondary_before(nbytes, nbytes, body_carrier)
            raw=obj.tobytes()
            if type(raw) is not bytes:
                raise Refused('secondary_buffer_body','terminal')
            edges=(obj.obj, raw, obj.format, obj.shape, obj.strides)
            scalars=(1 if obj.readonly else 0, int(obj.nbytes))
            _secondary_store('buffer', obj, edges, scalars, body_carrier)
            if obj.obj is not None:
                stack.append(obj.obj)
        elif kind is types.TracebackType:
            _secondary_store('traceback', obj, (obj.tb_next, obj.tb_frame),
                (int(obj.tb_lineno), int(obj.tb_lasti)), body_carrier)
            stack.append(obj.tb_next)
            stack.append(obj.tb_frame)
        elif isinstance(obj, BaseException):
            state=obj.__dict__ if type(getattr(obj, '__dict__', None)) is dict else None
            notes=state.get('__notes__') if state is not None and '__notes__' in state else None
            body=state if state is not None else obj.__dict__
            edges=(type(obj), obj.args, body, notes, obj.__cause__, obj.__context__, obj.__traceback__)
            _secondary_store('error', obj, edges, (1 if obj.__suppress_context__ else 0,), body_carrier)
            stack.append(obj.args)
            if type(body) is dict:
                stack.append(body)
            stack.append(obj.__cause__)
            stack.append(obj.__context__)
            stack.append(obj.__traceback__)
        elif kind is types.FunctionType:
            stack.append(obj.__code__)
        elif kind is types.MethodType:
            stack.append(obj.__func__)
            stack.append(obj.__self__)
        elif kind in (staticmethod, classmethod):
            stack.append(obj.__func__)
        elif kind is types.CellType:
            try:
                stack.append(obj.cell_contents)
            except ValueError:
                pass
        elif kind is types.ModuleType or kind is type:
            pass
        elif kind in (list, tuple):
            item_count=len(obj)
            _secondary_before(item_count, item_count * 64, body_carrier)
            stack.extend(obj)
        elif kind is dict:
            item_count=len(obj)
            _secondary_before(item_count, item_count * 64, body_carrier)
            for key, value in obj.items():
                stack.append(key)
                stack.append(value)
        elif getattr(kind, '__module__', None) in allowed:
            state=getattr(obj, '__dict__', None)
            if type(state) is dict:
                _secondary_store('source', obj, (kind, state), (), body_carrier)
                stack.append(state)
    return _SECONDARY_CUTS

def consume_secondary_own_cut(records=None, body_carrier=None):
    """Alias pass over the entire retained cut list. Refusal keeps every stored original."""
    rows=_SECONDARY_CUTS if records is None else records
    if rows is not _SECONDARY_CUTS:
        raise Refused('secondary_own_cut_carrier','terminal')
    # Header plus the widest alias prefix, before any row field is read.
    # Historical rows, traceback, error, Source, and a zero-width buffer are
    # included. A later call pays this pass again. Frame, code, and buffer
    # copies stay on their own debits.
    count=len(rows)
    pass_reads=_scale(count, 20 * 64, 0)
    if pass_reads is None:
        raise Refused('secondary_preown_original_envelope', 'terminal')
    _secondary_before(pass_reads, 0, body_carrier)
    for row in rows:
        if type(row) is not dict or not row.get('edges') or row.get('original') is None:
            raise Refused('secondary_pointer_only_cut','terminal')
        if row.get('kind') not in _SECONDARY_KINDS:
            raise Refused('secondary_own_cut_alias','terminal')
        _consume_secondary_row(row, body_carrier)
    return True

def _named_traceback_cut(obj):
    if type(obj) is not types.TracebackType:
        return False
    row=_SECONDARY_BY_ID.get(id(obj))
    return row is not None and row.get('original') is obj and row.get('kind')=='traceback'

def _retained_error_traceback(historical, current, carrier):
    """Same-phase identity stays. A later traceback may name the first cut.

    The stored error row is not rewritten. An unrelated traceback is refused.
    The bounded walk is debited before any tb_next read.
    """
    if historical is current:
        return True
    if historical is None:
        return True
    if type(historical) is not types.TracebackType or type(current) is not types.TracebackType:
        return False
    if not _named_traceback_cut(historical) or not _named_traceback_cut(current):
        return False
    bound=len(_SECONDARY_CUTS)
    reads=_scale(bound, 64, 0)
    if reads is None:
        raise Refused('secondary_preown_original_envelope', 'terminal')
    _secondary_before(reads, 0, carrier)
    cursor=current
    for _step in range(bound):
        if cursor is historical:
            return True
        if type(cursor) is not types.TracebackType:
            return False
        cursor=cursor.tb_next
    return False

def _retained_handler_current(error, context, after, carrier):
    """Join a witnessed handler phase to a later witnessed handler phase.

    A cycle cut remains historical DATA when the re-raised error subsequently
    changes handler again. This finite suffix is not recursive graph probing.
    """
    if context is error.__context__:
        return True
    count=len(_SECONDARY_CUTS)
    if after<0 or after>count:
        return False
    reads=_scale(count-after, 64, 0)
    if reads is None:
        raise Refused('secondary_preown_original_envelope', 'terminal')
    _secondary_before(reads, 0, carrier)
    for index in range(after, count):
        row=_SECONDARY_CUTS[index]
        if row['kind']!='error_handler':
            continue
        raised,handler,before_context,before_tb,_chain=row['edges']
        if raised is not error or before_context is not context:
            continue
        current_tb=error.__traceback__
        if current_tb is before_tb or not _named_traceback_cut(current_tb):
            continue
        if not _retained_error_traceback(before_tb, current_tb, carrier):
            # Match the native suffix reader: an applicable unresolved prefix
            # cannot be skipped to manufacture a later successful relation.
            return False
        context=handler
        if context is error.__context__:
            return True
    return False

def _retained_error_context(error, historical, current, carrier):
    """Only chronological, pre-captured actual-handler relations may change.

    The normal raise can also clear a context edge in the handler chain to
    prevent a cycle. Its pre-raise chain and the re-raised original/new TB
    are both required; an arbitrary unrelated context never qualifies.
    """
    if historical is current:
        return True
    bound=len(_SECONDARY_CUTS)
    reads=_scale(bound, 64, 0)
    if reads is None:
        raise Refused('secondary_preown_original_envelope', 'terminal')
    _secondary_before(reads, 0, carrier)
    cursor=historical
    for index,record in enumerate(_SECONDARY_CUTS):
        if record['kind']!='error_handler':
            continue
        raised,handler,before_context,before_tb,chain=record['edges']
        if error is not raised and cursor is not raised:
            continue
        if raised.__traceback__ is before_tb:
            continue
        if not _named_traceback_cut(raised.__traceback__):
            continue
        if not _retained_error_traceback(before_tb, raised.__traceback__, carrier):
            # Native rejects this same applicable prefix before a later row.
            # Preserve the original history and refuse the unresolved relation.
            return False
        if error is raised and cursor is before_context:
            cursor=handler
        elif cursor is raised and _retained_handler_current(raised, handler, index+1, carrier):
            steps=_scale(len(chain), 2 * 64, 0)
            if steps is None:
                raise Refused('secondary_preown_original_envelope', 'terminal')
            _secondary_before(steps, 0, carrier)
            for node,context in chain:
                if node is error and context is raised:
                    cursor=None
                    break
        if cursor is current:
            return True
    return False

def _consume_secondary_row(row, carrier):
    kind=row['kind']
    obj=row['original']
    edges=row['edges']
    scalars=row['scalars']
    if kind=='frame':
        if type(obj) is not types.FrameType or len(edges)!=5 or len(scalars)!=5:
            raise Refused('secondary_frame_alias','terminal')
        _secondary_before(5 * 64, 256, carrier)
        if edges[0] is not obj.f_code or edges[2] is not obj.f_globals or edges[4] is not obj.f_builtins:
            raise Refused('secondary_frame_alias','terminal')
        if edges[1] is not secondary_frame_locals(obj, carrier) or edges[3] is not obj.f_trace:
            raise Refused('secondary_frame_locals_alias','terminal')
        if scalars[3]!=(1 if obj.f_trace_lines else 0) or scalars[4]!=(1 if obj.f_trace_opcodes else 0):
            raise Refused('secondary_frame_scalar','terminal')
    elif kind=='code':
        if type(obj) is not types.CodeType or len(edges)!=11 or len(scalars)!=7:
            raise Refused('secondary_code_alias','terminal')
        _secondary_before(11 * 64, 11 * 64, carrier)
        code_bytes=obj.co_code
        if type(code_bytes) is bytes:
            _secondary_before(len(code_bytes), len(code_bytes), carrier)
        live=(code_bytes, obj.co_consts, obj.co_names, obj.co_varnames, obj.co_filename,
            obj.co_name, obj.co_qualname, obj.co_linetable, obj.co_exceptiontable,
            obj.co_freevars, obj.co_cellvars)
        if any(edges[i] is not live[i] for i in range(11)):
            raise Refused('secondary_code_alias','terminal')
        live_scalars=(obj.co_argcount, obj.co_posonlyargcount, obj.co_kwonlyargcount,
            obj.co_nlocals, obj.co_stacksize, obj.co_flags, obj.co_firstlineno)
        if tuple(scalars)!=tuple(int(item) for item in live_scalars):
            raise Refused('secondary_code_scalar','terminal')
    elif kind=='buffer':
        if type(obj) is not memoryview or len(edges)!=5 or len(scalars)!=2:
            raise Refused('secondary_buffer_alias','terminal')
        nbytes=obj.nbytes
        if not _u64(nbytes):
            raise Refused('secondary_preown_original_envelope', 'terminal')
        _secondary_before(nbytes, nbytes, carrier)
        if edges[0] is not obj.obj or edges[1]!=obj.tobytes():
            raise Refused('secondary_buffer_alias','terminal')
        if edges[2]!=obj.format or edges[3]!=obj.shape or edges[4]!=obj.strides:
            raise Refused('secondary_buffer_alias','terminal')
        if scalars[0]!=(1 if obj.readonly else 0) or scalars[1]!=obj.nbytes:
            raise Refused('secondary_buffer_scalar','terminal')
    elif kind=='traceback':
        if type(obj) is not types.TracebackType or len(edges)!=2 or len(scalars)!=2:
            raise Refused('secondary_traceback_alias','terminal')
        if edges[0] is not obj.tb_next or edges[1] is not obj.tb_frame:
            raise Refused('secondary_traceback_alias','terminal')
        if scalars[0]!=obj.tb_lineno or scalars[1]!=obj.tb_lasti:
            raise Refused('secondary_traceback_scalar','terminal')
    elif kind=='error':
        if not isinstance(obj, BaseException) or len(edges)!=7 or len(scalars)!=1:
            raise Refused('secondary_error_alias','terminal')
        if edges[0] is not type(obj) or edges[1] is not obj.args or edges[2] is not obj.__dict__:
            raise Refused('secondary_error_alias','terminal')
        if edges[4] is not obj.__cause__:
            raise Refused('secondary_error_alias','terminal')
        if not _retained_error_context(obj, edges[5], obj.__context__, carrier):
            raise Refused('secondary_error_context_phase','terminal')
        if not _retained_error_traceback(edges[6], obj.__traceback__, carrier):
            raise Refused('secondary_error_alias','terminal')
        if edges[3] is None:
            state=obj.__dict__ if type(obj.__dict__) is dict else {}
            if '__notes__' in state:
                raise Refused('secondary_error_notes','terminal')
        elif edges[3] is not obj.__notes__:
            raise Refused('secondary_error_notes','terminal')
        if scalars[0]!=(1 if obj.__suppress_context__ else 0):
            raise Refused('secondary_error_scalar','terminal')
    elif kind=='error_handler':
        if obj is not edges or len(edges)!=5 or scalars:
            raise Refused('secondary_error_handler_alias','terminal')
        raised,handler,context,tb,chain=edges
        if (not isinstance(raised, BaseException) or not isinstance(handler, BaseException)
                or raised is handler or type(chain) is not tuple or not chain):
            raise Refused('secondary_error_handler_alias','terminal')
        steps=_scale(len(chain), 2 * 64, 0)
        if steps is None:
            raise Refused('secondary_preown_original_envelope', 'terminal')
        _secondary_before(steps, 0, carrier)
        cursor=handler
        for pair in chain:
            if (type(pair) is not tuple or len(pair)!=2 or pair[0] is not cursor
                    or not isinstance(cursor, BaseException)):
                raise Refused('secondary_error_handler_chain','terminal')
            cursor=pair[1]
        if cursor is not None:
            reads=_scale(len(chain), 64, 0)
            if reads is None:
                raise Refused('secondary_preown_original_envelope', 'terminal')
            _secondary_before(reads, 0, carrier)
            if not any(pair[0] is cursor for pair in chain):
                raise Refused('secondary_error_handler_chain','terminal')
    elif kind=='source':
        if len(edges)!=2 or edges[0] is not type(obj) or edges[1] is not getattr(obj, '__dict__', None):
            raise Refused('secondary_source_alias','terminal')
    else:
        raise Refused('secondary_own_cut_alias','terminal')
