"""Actual same-process existing native Publisher owner, not an issuer.

This module is called INSIDE the enrolled native entry before Source object
birth. Native context creation is a C API of that same entry, unavailable to
Source. No standalone Python fallback, grant, late open, or background owner.
All text remains inert until independent current Source/Root/ABI admission.
"""
import os
from common import Refused, INPUT_MAX, DOCUMENT_MAX, canonical, encoded_bound
from observer import full_value_arena, validate_full_value_arena, NativeCompletionReceipt
from lifetime import OwnedFDs

_MAPPING_CUT_SCALARS=('schema','creator_pid','actual_pid','role','backing_birth9',
    'backing_cut9','width','position','access','closed','cohort_before_effect')
_MAPPING_CUT_KEYS=frozenset(_MAPPING_CUT_SCALARS+('actual_row','actual_credit','full_bytes'))

def _freeze_mapping_cut(birth,cut):
    # An OWNED full physical cut, not a summary/hash or closed flag. Called
    # while live and before native bank seal/actual mapping.close.
    if (type(cut) is not dict or set(cut)!=_MAPPING_CUT_KEYS
            or cut['schema']!='friday.sol091.selected-owned-mmap-public-cut.v1'
            or type(cut['full_bytes']) is not bytes or len(cut['full_bytes'])!=birth.width
            or cut['actual_row'] is not birth.row or cut['actual_credit'] is not birth.credit
            or cut['closed'] is not False or birth.mapping is None or birth.mapping.closed
            or not any(item is cut for item in birth.cuts)):
        raise Refused('native_mapping_full_before_close_cut','terminal')
    birth.require_cut(cut)  # Meaningful live full-byte/alias/backing checks stay.
    return (birth,cut,birth.mapping,cut['full_bytes'],birth.row,birth.credit,
        tuple(cut[name] for name in _MAPPING_CUT_SCALARS))

def _require_frozen_mapping_cut(ledger):
    birth,cut,mapping,raw,row,credit,scalars=ledger
    if (type(cut) is not dict or set(cut)!=_MAPPING_CUT_KEYS
            or birth.mapping is not mapping or birth.row is not row or birth.credit is not credit
            or cut['full_bytes'] is not raw or type(raw) is not bytes or len(raw)!=birth.width
            or cut['actual_row'] is not row or cut['actual_credit'] is not credit
            or tuple(cut[name] for name in _MAPPING_CUT_SCALARS)!=scalars
            or not any(item is cut for item in birth.cuts)):
        raise Refused('native_mapping_full_historical_cut_drift','terminal')
    return birth,cut

def _native():
    # Independently enrolled stock dependency; its current() has NO Python
    # constructor. A Source-created dict/capsule cannot substitute for a context.
    import publisher_owned_custody
    return publisher_owned_custody

def current_native_owner():
    native=_native()
    context=native.current()
    if context is None:
        raise Refused('existing_enrolled_native_entry_before_Source_required','before-effect')
    existing=native.get_anchor(context,'performing-receiver')
    return existing if existing is not None else NativeRootReceiver(native,context)

class NativeMemoryBody:
    """Full binary bank in the already prepaid native parent, no FD/open/seal."""
    def __init__(self,receiver,kind):
        self.receiver=receiver;self.kind=kind;self.aliases=[];self.count=0
        self.retired_mapping_nodes=[]
        self.key=receiver.native.begin_bank(receiver.context,kind)
        self.attempted=False
    def add(self,value):
        if type(value) not in (bytes,bytearray):raise Refused('native_body_type','terminal')
        self.receiver.native.before_bytes(self.receiver.context,self.key,len(value))
        raw=value if type(value) is bytes else bytes(value)
        offset=self.receiver.native.append_bytes(self.receiver.context,self.key,raw)
        self.aliases.append((value,raw,offset));self.count+=len(raw)
        return {'offset':offset,'bytes':len(raw)}
    def finish(self):
        from selected_owned_values import require_selected_mapping_cuts
        require_selected_mapping_cuts(self)
        if self.attempted:raise Refused('native_body_once','terminal')
        for value,raw,offset in self.aliases:
            if len(value)!=len(raw) or memoryview(value)!=memoryview(raw):
                raise Refused('native_actual_alias_drift','terminal')
        cuts=getattr(self,'selected_mapping_cuts',())
        if cuts:self.receiver.native.before_graph(self.receiver.context,len(cuts))
        self.mapping_cut_ledger=tuple(_freeze_mapping_cut(birth,cut) for birth,cut in cuts)
        self.attempted=True
        self.receiver.native.finish_bank(self.receiver.context,self.key,self.aliases)
        return {'endpoint':self.key,'bytes':self.count}
    def require_held_binding(self,nodes,pin,raw,descriptor):
        from observer import fd_value_fields,full_value_literal
        fields=fd_value_fields(nodes,pin)
        original=full_value_literal(nodes,pin)
        if type(original) is not dict:
            raise Refused('native_original_binding_pin_fields','terminal')
        span=nodes[raw][1];body=self.require(descriptor)
        # Bank is already sealed: its write preflight cannot debit a read.
        # SAME original Root owns the independent before-copy debit instead.
        self.receiver.native.own_prepare(span['bytes']+131072)
        held=body[span['offset']:span['offset']+span['bytes']]
        if self.receiver.native.own_held_body_check(original,held) is not True:
            raise Refused('native_full_original_held_Source_body','terminal')
    def require_runtime_support(self,serial):
        if self.receiver.native.own_support_check(serial) is not True:
            raise Refused('actual_runtime_support_relation_retained','terminal')
    def require_error_record(self,arena,node,body,nodes):
        from selected_owned_values import require_error_record_binding
        require_error_record_binding(self,arena,node,body,nodes)
    def require_error_cell(self,arena,node,body,nodes):
        from selected_owned_values import require_error_cell_binding
        require_error_cell_binding(self,arena,node,body,nodes)
    def require_error_type_support(self,arena,serial,node):
        from selected_owned_values import require_error_type_support
        require_error_type_support(self,arena,serial,node)
    def require(self,descriptor):
        if descriptor!={'endpoint':self.key,'bytes':self.count} or not self.attempted:
            raise Refused('native_body_descriptor','terminal')
        return self.receiver.native.read_bank(self.receiver.context,self.key)
    def validate_spans(self,nodes,descriptor):
        raw=self.require(descriptor);at=0
        for kind,body in nodes:
            if kind not in ('bytes','bytearray'):continue
            if set(body)!=set(('offset','bytes')) or body['offset']!=at or body['bytes']>len(raw)-at:
                raise Refused('native_body_full_span','terminal')
            at+=body['bytes']
        if at!=len(raw):raise Refused('native_body_unreferenced_bytes','terminal')
        retired=0
        for index,row in enumerate(nodes):
            if row[0]!='selected-retired-mmap':continue
            if (retired>=len(self.retired_mapping_nodes)
                    or self.retired_mapping_nodes[retired][0]!=index):
                raise Refused('native_retired_mapping_node_inventory','terminal')
            retired+=1
        if retired!=len(self.retired_mapping_nodes):
            raise Refused('native_retired_mapping_node_inventory','terminal')
        for index,state in self.retired_mapping_nodes:
            self.receiver.require_retired_mapping_state(state)
        return raw
    def match_roots(self,descriptor):
        self.require(descriptor)
        cuts=getattr(self,'selected_mapping_cuts',())
        ledger=getattr(self,'mapping_cut_ledger',())
        if len(cuts)!=len(ledger):raise Refused('native_mapping_cut_inventory','terminal')
        for actual,held in zip(cuts,ledger):
            birth,cut=_require_frozen_mapping_cut(held)
            if actual[0] is not birth or actual[1] is not cut:
                raise Refused('native_mapping_cut_alias_inventory','terminal')
            if birth.mapping.closed:
                self.receiver.require_retired_mapping_cut(held)
            else:
                birth.require_cut(cut)
        for value,raw,offset in self.aliases:
            if memoryview(value)!=memoryview(raw):raise Refused('native_body_actual_alias_drift','terminal')
        for index,state in self.retired_mapping_nodes:
            self.receiver.require_retired_mapping_state(state)
    def retired_mapping_state(self,value):
        import mmap
        if type(value) is not mmap.mmap:return None
        return self.receiver.retired_mapping_state(value)
    def retain_retired_mapping_node(self,index,state):
        # The actual producing carrier keeps the original closed-object
        # evidence. A syntactically valid wire record cannot enroll a mapping.
        self.receiver.native.before_graph(self.receiver.context,3)
        self.receiver.require_retired_mapping_state(state)
        if any(old==index for old,held in self.retired_mapping_nodes):
            raise Refused('native_retired_mapping_node_duplicate','terminal')
        self.retired_mapping_nodes.append((index,state))

class NativeRootReceiver:
    """Existing caller owns all originals, actual bytes, errors, and close rows.

    The same native context owns this receiver BEFORE the adapter constructor.
    Receipt confirmation and outside-end confirmation are distinct operations.
    No receipt boolean can create actual native ownership.
    """
    def __init__(self,native,context):
        self.native=native;self.context=context
        self.owner_pid=os.getpid();self.root_fact=native.root_fact(context)
        self.qualification=native.qualification(context)
        self.accepted_source_roots={}
        self.prepared_bodies=[];self.snapshots=[];self.full_errors=[]
        self.source_end=None;self.final_document=None;self.final_result=None
        self.final_snapshot=None
        self.fdjournal=None;self.receipt=None;self.owner=None;self.serial=0
        self.accepted_mappings=[];self.mapping_ends=[]
        self.publication_transfers=[]
        self.prefix_envelope=native.prefix_envelope(context)
        self.after_document_snapshots=[];self.prefix_snapshots=[]
        native.hold(context,'performing-receiver',self)
    def bind_source(self,owner):
        self.native.bind_source(self.context,owner.root_fact,owner.qualification)
        self.qualification=owner.qualification;self.root_fact=owner.root_fact
    def retain_before_birth(self,value,kind):
        # Native INCREF precedes selected __init__, including partial objects.
        self.native.hold(self.context,kind,value)
    def retain_error(self,error,phase):
        # The same preowned native Run retains original error/phase/TB before
        # serial, key, tuple, dictionary or Source list publication can fail.
        # Any partial native handoff remains in the actual caller inventory.
        self.native.hold_source_error(self.context,error,phase)
        self.serial+=1
        self.full_errors.append((phase,error))
    def capture(self,kind,roots):
        self.serial+=1;kind=str(self.serial)+':'+kind
        self.native.before_graph(self.context,len(roots))
        self.native.hold(self.context,'actual-roots:'+kind,roots)
        bank=NativeMemoryBody(self,kind)
        body=full_value_arena(roots,body_carrier=bank)
        validate_full_value_arena(body,bank)
        snapshot={'kind':kind,'body':body,'physical':bank.require(body['physical_body'])}
        self.native.hold(self.context,'complete-snapshot:'+kind,snapshot)
        snapshot['reader']=bank
        self.snapshots.append(snapshot)
        return snapshot
    def consume_final(self,owner,roots):
        self.owner=owner
        if self.owner_pid!=owner.final_arena.owner_pid or self.root_fact!=owner.root_fact:
            raise Refused('native_completion_actual_owner','terminal')
        if self.qualification is not owner.qualification:
            raise Refused('native_completion_qualification_identity','terminal')
        self.accepted_source_roots=roots.copy()
        self.native.hold(self.context,'accepted-final-Source-roots',self.accepted_source_roots)
        self.capture('initial-final-roots',[roots])
        receipt=NativeCompletionReceipt(self,owner.final_arena,roots,
            owner.final_arena.physical_record['pin'],self.qualification)
        self.native.hold(self.context,'actual-completion-receipt',receipt)
        self.receipt=receipt
        return receipt
    def require_transfer(self,receipt):
        if receipt is not self.receipt or not receipt.confirmed:
            raise Refused('native_actual_transfer_receipt','terminal')
        if not self.native.matches(self.context,'accepted-final-Source-roots',self.accepted_source_roots):
            raise Refused('native_actual_transfer_custody','terminal')
    def accept_prepared(self,body,receipt):
        self.require_transfer(receipt)
        return self._accept_prepared_body(body)
    def _accept_prepared_body(self,body):
        prior=next((packet for packet in self.prepared_bodies if packet[0] is body),None)
        if prior is not None:
            if not self.native.matches(self.context,'prepared-body:'+body.name,prior):
                raise Refused('native_prepared_existing_custody','terminal')
            return prior
        # A failed seal still has an exact original live row, bytes and aliases.
        # Do not retry finish/seal and do not discard an unsealed partial bank.
        if body.raw is not None:
            body.require(body.descriptor());body.match_roots(body.descriptor())
        if self.fdjournal is None:self.fdjournal=OwnedFDs(meter=body.store.meter)
        # body.hold is the zero-slot terminal BODY credit, not its separately
        # prepared FD/keeper credit. transfer() adopts the actual original
        # row's credit; no zero-slot substitute is installed here.
        # Native retains exact raw bytes/aliases/hold/row before Source removes
        # anything. Both journals share the already charged original FD state.
        fd=body.held.fd if body.held is not None else body.fd
        row=body.store.fdjournal._row(fd)
        packet=(body,body.raw,body.alias_snapshots,fd,body.record,body.hold,row)
        self.native.hold(self.context,'prepared-body:'+body.name,packet)
        body.store.fdjournal.transfer(fd,self.fdjournal)
        if self.fdjournal._row(fd) is not packet[-1]:
            raise Refused('native_prepared_actual_row_transfer','terminal')
        self.prepared_bodies.append(packet)
        if body.raw is None:
            fd_info=os.fstat(fd);count=fd_info.st_size
            if not 0<=count<=80_000_000:
                raise Refused('native_partial_body_original_bound','terminal')
            bank=NativeMemoryBody(self,'partial-seal:'+body.name)
            self.native.before_bytes(self.context,bank.key,count)
            chunks=[];at=0
            while at<count:
                raw=os.pread(fd,min(65536,count-at),at)
                if not raw:raise Refused('native_partial_body_full_read','terminal')
                chunks.append(raw);at+=len(raw)
            after=os.fstat(fd)
            fields=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
            if any(getattr(fd_info,k)!=getattr(after,k) for k in fields):
                raise Refused('native_partial_body_physical_drift','terminal')
            descriptor=bank.add(b''.join(chunks));physical=bank.finish()
            partial={'actual_body':body,'exact_original_row':row,'full_reader':bank,
                'physical_body':physical,'raw':bank.require(physical),'span':descriptor,
                'actual_aliases':tuple(body.alias_snapshots),'original_record':body.record}
            self.native.hold(self.context,'partial-seal-body:'+body.name,partial)
        return packet
    def _accept_prebirth_record(self,birth,body,row,credit):
        # No constructor fields are read here: even self.mapping or
        # self.close_attempted may never have been assigned.
        record=self.native.own_mapping('prebirth-state',birth,None,
            body,None,row,credit)
        if record is None:return False
        if (type(record) is not tuple or len(record)!=8
                or record[0] is not birth or record[1] is not body
                or record[2] is not row or record[3] is not credit):
            raise Refused('native_prebirth_original_aliases','terminal')
        self.native.before_graph(self.context,10)
        key='mapping-prebirth-handover:'+str(id(birth))
        prior=self.native.get_anchor(self.context,key)
        if prior is not None:
            if (type(prior) is not tuple or len(prior)!=2
                    or type(prior[0]) is not tuple or len(prior[0])!=8
                    or any(a is not b for a,b in zip(prior[0],record))
                    or not self.native.matches(self.context,key,prior)):
                raise Refused('native_prebirth_record_drift','terminal')
            snapshot=prior[1]
            validate_full_value_arena(snapshot['body'],snapshot['reader'])
            snapshot['reader'].require(snapshot['body']['physical_body'])
        else:
            self.native.hold(self.context,key+':originals',record)
            snapshot=self.capture('mapping-prebirth-full-prefix',record)
            # Publish immutable HANDOVER before ending capability. It is NOT
            # an end flag and is never mutated after exposure.
            self.native.hold(self.context,key,(record,snapshot))
        # A previous confirmed capability end is observed, not retried after
        # an interrupted return. Actual native status, never tuple presence,
        # permits the backing close. Strong originals still await both readers.
        ended=self.native.own_mapping('prebirth-ended',birth,None,
            body,None,row,credit)
        if ended is not True:
            if ended is not False or self.native.own_mapping('retire-prebirth',
                    birth,None,body,None,row,credit) is not True:
                raise Refused('native_prebirth_end_unconfirmed','terminal')
        return True
    def accept_unmapped_birth(self,birth,receipt):
        self.require_transfer(receipt)
        return self._accept_unmapped_birth(birth)
    def _accept_unmapped_birth(self,birth):
        self.native.before_scan(self.context,4096,0)
        # No mapping.close receipt is invented for an actual never-born map.
        # Source mapping=None alone is insufficient: native must have retained
        # this exact birth/row/credit and an unattempted or returned-error call.
        if (birth.mapping is not None or birth.close_attempted
                or birth._own_registry is not self.native
                or os.getpid()!=birth.owner_pid):
            raise Refused('native_unmapped_birth_actual_owner','terminal')
        self.native.before_graph(self.context,8)
        key='unmapped-birth:'+str(id(birth))
        prior=self.native.get_anchor(self.context,key+':complete')
        if prior is not None:
            if (type(prior) is not tuple or len(prior)!=6 or prior[0] is not birth
                    or prior[1] is not birth.creation or prior[2] is not birth.row
                    or prior[3] is not birth.credit
                    or not self.native.matches(self.context,key+':complete',prior)):
                raise Refused('native_unmapped_birth_custody','terminal')
            outcome=self.native.own_mapping('unmapped-state',birth,None,
                birth.creation,birth.cuts,birth.row,birth.credit)
            if outcome!=prior[4]:
                raise Refused('native_unmapped_birth_phase_drift','terminal')
            snapshot=prior[5]
            validate_full_value_arena(snapshot['body'],snapshot['reader'])
            snapshot['reader'].require(snapshot['body']['physical_body'])
            return True
        outcome=self.native.own_mapping('unmapped-state',birth,None,
            birth.creation,birth.cuts,birth.row,birth.credit)
        if type(outcome) is not int or outcome not in (1,2):
            raise Refused('native_unmapped_birth_outcome','terminal')
        roots=(birth,birth.creation,birth.row,birth.credit,outcome)
        self.native.hold(self.context,key+':originals',roots)
        snapshot=self.capture('unmapped-birth',roots)
        # This receiver has read the full prefix/error graph. Retiring only
        # the creation capability does NOT release its native original owners.
        # The later command reader binds their no-map row/phase/error DATA;
        # both command readers must succeed before those strong owners end.
        # A failed capture keeps originals; it cannot authorize a backing close.
        if self.native.own_mapping('retire-unmapped',birth,None,
                birth.creation,birth.cuts,birth.row,birth.credit) is not True:
            raise Refused('native_unmapped_birth_end_unconfirmed','terminal')
        complete=(birth,birth.creation,birth.row,birth.credit,outcome,snapshot)
        self.native.hold(self.context,key+':complete',complete)
        return True
    def accept_mapping(self,birth,receipt):
        self.require_transfer(receipt)
        return self._accept_mapping_before_close(birth)
    def _accept_mapping_before_close(self,birth):
        if birth.mapping is None or birth.close_attempted:
            raise Refused('selected_mapping_before_actual_close','terminal')
        # Terminal current bytes are taken while live, distinct from original
        # cohort-before-effect cuts. AFTER close no live mapping/FD read occurs.
        birth.final_current_cut_before_close()
        self.native.before_graph(self.context,len(birth.cuts)+3)
        original_ledgers=tuple(_freeze_mapping_cut(birth,cut) for cut in birth.cuts)
        self.native.hold(self.context,'mapping-original-cuts:'+str(id(birth)),original_ledgers)
        snapshot=self.capture('selected-mapping-transfer:'+birth.role,
                              [birth.mapping,birth.row,birth.credit,tuple(birth.cuts)])
        reader=snapshot['reader']
        extra=tuple(ledger for ledger in original_ledgers
                    if not any(old[0] is ledger[0] and old[1] is ledger[1]
                               for old in reader.mapping_cut_ledger))
        reader.mapping_cut_ledger=reader.mapping_cut_ledger+extra
        reader.selected_mapping_cuts=list(reader.selected_mapping_cuts)+[(ledger[0],ledger[1]) for ledger in extra]
        self.native.hold(self.context,'mapping-complete-cut-ledger:'+str(id(birth)),reader.mapping_cut_ledger)
        # Every previously retained cut of this same original mapping is
        # checked in its live phase immediately before the one actual close.
        for old in self.snapshots:
            for ledger in getattr(old['reader'],'mapping_cut_ledger',()):
                if ledger[0] is birth:
                    _,cut=_require_frozen_mapping_cut(ledger)
                    birth.require_cut(cut)
        self.native.hold(self.context,'accepted-mapping:'+str(id(birth)),(birth,snapshot))
        self.accepted_mappings.append((birth,snapshot))
        if birth._own_registry is not None:
            birth._own_registry.own_mapping('before-close',birth,birth.mapping,
                birth.final_current_cut,reader.mapping_cut_ledger,birth.row,birth.credit)
        return snapshot
    def mapping_closed(self,birth,receipt):
        self.require_transfer(receipt)
        return self._mapping_closed_record(birth)
    def _mapping_closed_record(self,birth):
        held=self.native.get_anchor(self.context,'accepted-mapping:'+str(id(birth)))
        if (held is None or held[0] is not birth or birth.mapping.closed is not True
                or birth.close_attempted is not True or birth.closed_confirmed is not True
                or birth.close_error is not None
                or any(record[0] is birth for record in self.mapping_ends)):
            raise Refused('selected_mapping_close_actual_custody','terminal')
        # Cut/full-byte/row/credit ledger was taken BEFORE actual close. The
        # original pinned retire implementation calls us only after .close().
        # This is a phase witness plus full originals, never flag-only proof.
        record=(birth,birth.mapping,birth.row,birth.credit,held[1],
            birth.close_attempted,birth.closed_confirmed,birth.close_error)
        self.native.hold(self.context,'mapping-closed:'+str(id(birth)),record)
        self.mapping_ends.append(record)
        return record
    def require_retired_mapping_cut(self,ledger):
        birth,cut=_require_frozen_mapping_cut(ledger)
        held=self.native.get_anchor(self.context,'accepted-mapping:'+str(id(birth)))
        record=self.native.get_anchor(self.context,'mapping-closed:'+str(id(birth)))
        if (held is None or held[0] is not birth or record is None
                or not any(item is record for item in self.mapping_ends)
                or len(record)!=8 or record[0] is not birth
                or record[1] is not ledger[2] or record[2] is not ledger[4]
                or record[3] is not ledger[5] or record[4] is not held[1]
                or record[5] is not True or record[6] is not True or record[7] is not None
                or birth.close_attempted is not True or birth.closed_confirmed is not True
                or birth.close_error is not None or birth.mapping.closed is not True):
            raise Refused('native_mapping_actual_close_transition','terminal')
        transfer=held[1]
        # Retain and validate the COMPLETE accepted before-close bank; do not
        # recurse into live matching or reopen/fstat a retired backing FD.
        validate_full_value_arena(transfer['body'],transfer['reader'])
        transfer['reader'].require(transfer['body']['physical_body'])
        if not any(item[0] is birth for item in transfer['reader'].mapping_cut_ledger):
            raise Refused('native_mapping_transfer_full_cut_missing','terminal')
        return cut['full_bytes']
    def retired_mapping_state(self,value):
        record=None
        for candidate in self.mapping_ends:
            if candidate[1] is value:
                if record is not None:raise Refused('native_retired_mapping_actual_identity','terminal')
                record=candidate
        if record is None:return None
        birth=record[0];transfer=record[4];count=0
        for held in transfer['reader'].mapping_cut_ledger:
            if held[0] is birth:count+=1
        if not count:raise Refused('native_retired_mapping_full_cut_required','terminal')
        self.native.before_graph(self.context,count+8)
        ledgers=tuple(held for held in transfer['reader'].mapping_cut_ledger if held[0] is birth)
        for held in ledgers:self.require_retired_mapping_cut(held)
        # ALL original cut/body/aliases/row/credit and actual close transition,
        # plus the current selected object identity. No fstat/reopen/live map
        # read after close. BOTH N3 readers must adopt/validate this exact phase
        # representation; do not relabel it as the old live v7 node.
        return {'schema':'friday.sol095.owned-retired-mmap-full-state.v1',
            'actual_mapping':value,'original_birth':birth,'full_cut_ledgers':ledgers,
            'full_transfer_snapshot':transfer,'actual_row':record[2],
            'actual_credit':record[3],'actual_close_record':record}
    def require_retired_mapping_state(self,state):
        fields={'schema','actual_mapping','original_birth','full_cut_ledgers',
            'full_transfer_snapshot','actual_row','actual_credit','actual_close_record'}
        if (type(state) is not dict or set(state)!=fields
                or state['schema']!='friday.sol095.owned-retired-mmap-full-state.v1'):
            raise Refused('native_retired_mapping_state_schema','terminal')
        record=state['actual_close_record'];birth=state['original_birth']
        if (type(record) is not tuple or len(record)!=8
                or not any(old is record for old in self.mapping_ends)
                or record[0] is not birth or record[1] is not state['actual_mapping']
                or record[2] is not state['actual_row'] or record[3] is not state['actual_credit']
                or record[4] is not state['full_transfer_snapshot']):
            raise Refused('native_retired_mapping_state_aliases','terminal')
        actual=record[4]['reader'].mapping_cut_ledger
        ledgers=state['full_cut_ledgers']
        if type(ledgers) is not tuple or not ledgers:
            raise Refused('native_retired_mapping_state_full_cuts','terminal')
        at=0
        for held in actual:
            if held[0] is not birth:continue
            if at>=len(ledgers) or ledgers[at] is not held:
                raise Refused('native_retired_mapping_state_cut_inventory','terminal')
            self.require_retired_mapping_cut(held);at+=1
        if at!=len(ledgers):
            raise Refused('native_retired_mapping_state_cut_inventory','terminal')
        return state
    def close_prepared(self,body,receipt):
        self.require_transfer(receipt)
        return self._close_prepared_body(body,receipt,False)
    def _close_prepared_body(self,body,receipt,publication):
        packet=next((p for p in self.prepared_bodies if p[0] is body),None)
        if packet is None or not self.native.matches(self.context,'prepared-body:'+body.name,packet):
            raise Refused('native_prepared_custody_missing','terminal')
        row=packet[-1]
        # Retire actual mmap aliases BEFORE their backing-row close; a numeric
        # fd close can never stand in for mapping.close's actual result.
        from selected_owned_values import own_mapping
        births=[]
        for value,raw,span in body.alias_snapshots:
            birth=own_mapping(value)
            if birth is not None and birth not in births:births.append(birth)
        for birth in getattr(body,'selected_mapping_births',()):
            if birth not in births:births.append(birth)
        # Includes originals whose Source append/setattr failed BEFORE init.
        # The same native registry owns them; no reconstructed missing object.
        known=self.native.own_mapping('prepared-births',None,None,
            body,None,row,body.hold)
        if type(known) is not tuple:
            raise Refused('native_prebirth_inventory','terminal')
        self.native.before_graph(self.context,len(known))
        from selected_owned_values import _scale
        reads=_scale(len(known),(len(births)+len(known))*64,0)
        if reads is None:raise Refused('native_prebirth_inventory_overflow','terminal')
        self.native.before_scan(self.context,reads,0)
        for birth in known:
            if not any(old is birth for old in births):births.append(birth)
        for birth in births:
            if self._accept_prebirth_record(birth,body,row,body.hold):continue
            if publication:
                if not birth.retire_publication(self,receipt):return False
            elif not birth.retire(self,receipt):return False
        if row['status']=='CLOSED':return True
        if row['status']=='UNKNOWN':return False
        # One actual original close, executed by the current native caller.
        # No retry on EINTR/UNKNOWN or reassignment of a reused numeric slot.
        try:self.native.close_owned_row(self.context,row,self.fdjournal.grants[row['credit']])
        except BaseException as error:
            self.retain_after_document(error,'prepared-close:'+body.name)
            return False
        self.fdjournal._changed()
        self.capture('prepared-close:'+body.name,[row,self.full_errors])
        return row['status']=='CLOSED'
    def begin_publication_transfer(self,owner,primary,late,pid,status,witness,costs):
        # SAME enrolled native context/receiver, not a new completion issuer.
        # Hold exact originals before any fallible byte read/mapping/FD close.
        if (os.getpid()!=self.owner_pid or owner.parent_pid!=self.owner_pid
                or owner.parent_child_pid!=pid or owner.parent_table_end is None
                or owner.failure_mailbox is not primary or primary.late is not late
                or late.primary is not primary or len(costs)!=2
                or costs[0]['bank'] is not primary or costs[1]['bank'] is not late
                or costs[0]['wait'] is not owner.parent_table_end
                or costs[1]['wait'] is not owner.parent_table_end):
            raise Refused('publication_actual_native_transfer_join','terminal')
        self.serial+=1
        key='publication-full-custody:'+str(self.serial)
        roots=(owner,primary,late,pid,status,owner.parent_table_end,
               owner.observer.final_io[pid],witness,tuple(costs))
        self.native.hold(self.context,key,roots)
        transfer=(key,roots)
        self.native.hold(self.context,key+':transfer',transfer)
        self.publication_transfers.append(transfer)
        return transfer
    def require_publication_transfer(self,transfer):
        if (type(transfer) is not tuple or len(transfer)!=2
                or not any(item is transfer for item in self.publication_transfers)
                or os.getpid()!=self.owner_pid
                or not self.native.matches(self.context,transfer[0],transfer[1])
                or not self.native.matches(self.context,transfer[0]+':transfer',transfer)):
            raise Refused('publication_actual_native_full_custody','terminal')
        roots=transfer[1];owner,primary,late,pid,status,wait,io,witness,costs=roots
        if (owner.parent_child_pid!=pid or owner.parent_table_end is not wait
                or not any(row is wait for row in owner.observer.waits)
                or wait['raw_wait_status']!=status
                or owner.observer.final_io.get(pid) is not io
                or owner.failure_mailbox is not primary or primary.late is not late):
            raise Refused('publication_actual_wait_IO_custody','terminal')
        return roots
    def accept_publication_unmapped_birth(self,birth,transfer):
        roots=self.require_publication_transfer(transfer)
        self.native.before_graph(self.context,8)
        self.native.before_scan(self.context,4096,0)
        primary,late=roots[1:3]
        records=tuple(getattr(body,'selected_mapping_births',())
                      for body in (primary.prepared,primary.metadata_prepared,
                                   late.prepared,late.metadata_prepared))
        if any(type(rows) not in (list,tuple) for rows in records):
            raise Refused('publication_unmapped_birth_inventory','terminal')
        from selected_owned_values import _scale
        reads=_scale(sum(len(rows) for rows in records),64,0)
        if reads is None:
            raise Refused('publication_unmapped_birth_scan_overflow','terminal')
        self.native.before_scan(self.context,reads,0)
        if not any(any(old is birth for old in rows) for rows in records):
            raise Refused('publication_unmapped_birth_inventory','terminal')
        return self._accept_unmapped_birth(birth)
    def accept_publication_mapping(self,birth,transfer):
        roots=self.require_publication_transfer(transfer)
        if not any(binding[0] is birth for binding in roots[1].preimage_bindings):
            raise Refused('publication_actual_mapping_inventory','terminal')
        return self._accept_mapping_before_close(birth)
    def publication_mapping_closed(self,birth,transfer):
        self.require_publication_transfer(transfer)
        return self._mapping_closed_record(birth)
    def close_publication_body(self,body,transfer):
        roots=self.require_publication_transfer(transfer)
        primary,late=roots[1:3]
        if not any(item is body for item in (primary.prepared,primary.metadata_prepared,
                                           late.prepared,late.metadata_prepared)):
            raise Refused('publication_actual_prepared_inventory','terminal')
        self._accept_prepared_body(body)
        if not self._close_prepared_body(body,transfer,True):return False
        # Original full bytes/cuts/rows/aliases remain in native anchors and
        # bank owners. Retire ONLY the live writer endpoint from OutputStore.
        store=body.store
        if store.full_bodies.get(body.name) is not body:
            raise Refused('publication_actual_store_body_identity','terminal')
        body.native_transferred=True;body.native_closed=True
        store.full_bodies.pop(body.name);store.prepared_fds.pop(body.name,None)
        if body.held is not None:body.held.fd=-1;body.held.closed=True
        body.fd=-1
        store.meter.retire_local_owner(body)
        return True

    def retain_after_document(self,error,phase):
        self.retain_error(error,phase)
        # A post-seal exception is never patched into the old immutable v2 pin.
        # Full public stock body/alias data goes to this preowned native bank.
        return self.capture('post-document:'+phase,[error,self.full_errors])
    def source_end_received(self,receipt,outcome):
        self.require_transfer(receipt)
        self.native.hold(self.context,'actual-source-end',outcome)
        self.source_end=outcome
    def complete_prefix(self,owner,error):
        # Native prefix owner already existed before Source/OutputStore/carriers.
        # Failure to encode/qualify this body keeps the native original owner.
        self.retain_error(error,'pre-OutputStore')
        snapshot=self.capture('pre-OutputStore-prefix',[self.prefix_envelope,owner,error])
        self.native.hold(self.context,'actual-prefix-transfer',snapshot)
        self.prefix_snapshots.append(snapshot)
        return {'status':'EXISTING_NATIVE_PARENT_FULL_PREFIX_HELD',
            'ownership_retired':False,'GO':False}
    def verify_native_error_snapshot(self,cut,snapshot):
        if not any(item is snapshot for item in self.snapshots):
            raise Refused('native_error_actual_full_snapshot','terminal')
        if not self.native.matches(self.context,'after-document-cell-before-codec',cut):
            raise Refused('native_error_actual_original_cut','terminal')
        nodes=validate_full_value_arena(snapshot['body'],snapshot['reader'])
        if len(snapshot['body']['roots'])!=1:
            raise Refused('native_error_full_original_root','terminal')
        snapshot['reader'].match_roots(snapshot['body']['physical_body'])
        # Actual graph/cut correspondence is strong owned identity; copying a
        # digest/schema/true bit cannot satisfy it.
        if snapshot.get('actual_error_cut') is not cut:
            raise Refused('native_error_original_cut_alias','terminal')
        return snapshot
    def consume_native_after_document(self):
        cut=self.native.after_document_cell(self.context)
        if cut is None:return None
        if self.after_document_snapshots:
            raise Refused('native_after_document_acceptance_not_repeated','terminal')
        self.native.hold(self.context,'after-document-cell-before-codec',cut)
        snapshot=self.capture('actual-native-after-document-error',[cut])
        snapshot['actual_error_cut']=cut
        self.after_document_snapshots.append(snapshot)
        self.native.accept_after_document(self.context,cut,snapshot)
        return snapshot
    def verify_v2_document(self,raw):
        from common import parse,exact,json_preflight
        if (type(raw) is not bytes or not 1<=len(raw)<=INPUT_MAX
                or raw is not self.final_document):
            raise Refused('native_v2_actual_document_bytes','terminal')
        # Original Root debits BEFORE lexical scans, decoder objects and the
        # two full canonical representations. No late grant or refunded cost.
        self.native.own_prepare(262144,4*len(raw)+8192)
        decode_allocation=json_preflight(raw,maximum=INPUT_MAX)
        self.native.own_prepare(decode_allocation+8*len(raw)+262144,
            128*(len(raw)+1)+256*len(self.snapshots))
        value=exact(parse(raw,maximum=INPUT_MAX),('schema','owner_pid','generation','body'),'native_v2_document')
        snapshot=self.final_snapshot
        if (type(snapshot) is not dict
                or not any(item is snapshot for item in self.snapshots)):
            raise Refused('native_v2_actual_snapshot_identity','terminal')
        if (value['schema']!='friday.sol086.native-full-completion.v2' or value['owner_pid']!=self.owner_pid
                or value['generation']!=self.receipt.generation or value['body']!=snapshot['body']):
            raise Refused('native_v2_actual_body_correspondence','terminal')
        # parse() retains duplicate-key/type/canonical-wire checks. Join ALL
        # decoded bytes to the retained original envelope, not merely Python
        # equality (where True and 1 compare equal). A decoded JSON dictionary
        # is never registered as an original arena or given its TYPE authority.
        retained={'schema':'friday.sol086.native-full-completion.v2',
            'owner_pid':self.owner_pid,'generation':self.receipt.generation,
            'body':snapshot['body']}
        if canonical(retained,maximum=INPUT_MAX)!=raw:
            raise Refused('native_v2_actual_document','terminal')
        reader=snapshot['reader']
        validate_full_value_arena(snapshot['body'],reader)
        self.native.own_prepare(131072+128*len(reader.aliases),
            2*len(snapshot['physical'])+512*len(reader.aliases)+8192)
        reader.match_roots(snapshot['body']['physical_body'])
        return raw
    def verify_native_caller_packet(self,packet):
        """Actual consumer before original native Run reference retirement.
        This is representation/custody validation, NOT independent acceptance.
        The same caller receives full immutable bank rows after native cleanup;
        the earlier Source receipt/document is never rewritten as a final end.
        """
        if type(packet) is not tuple or len(packet)!=13:
            raise Refused('actual_native_caller_packet_shape','terminal')
        (schema,source_return,early,document,end,banks,roots,closes,
            root_fact,root_qualification,source_qualification,status,cost)=packet
        if (schema!='friday.a268.actual-native-caller-full-bank-transfer.v2'
                or type(source_return) is not dict
                or source_return.get('actual_native_outside_end') is not self.final_result
                or source_return.get('actual_Source_end') is not self.source_end
                or source_return.get('ownership_retired') is not False
                or early is not self.final_result
                or type(early) is not dict or early.get('ownership_retired') is not False
                or document is not self.final_document or end is not self.source_end
                or type(end) is not dict or end.get('ownership_retired') is not True
                or root_fact is not self.native.root_fact(self.context)
                or root_qualification is not self.native.qualification(self.context)
                or source_qualification is not self.qualification
                or type(status) is not tuple or len(status)!=2
                or status!=(self.owner_pid,False)
                or type(cost) is not tuple or len(cost)!=3
                or any(type(x) is not int or x<0 for x in cost)
                or type(roots) is not tuple or len(roots)!=32
                or any(value is not None for value in roots[28:32])
                or roots[27] is not source_return
                or roots[3] is not early or roots[4] is not document
                or roots[5] is not end or roots[6] is not self.context):
            raise Refused('actual_native_caller_root_and_phase_correspondence','terminal')
        self.verify_v2_document(document)
        if type(banks) is not tuple or type(closes) is not tuple:
            raise Refused('actual_native_caller_complete_inventory','terminal')
        indexed={}
        for row in banks:
            if (type(row) is not tuple or len(row)!=4 or type(row[0]) is not str
                    or type(row[1]) is not bytes or type(row[2]) is not tuple
                    or type(row[3]) is not tuple or row[0] in indexed
                    or any(type(part) is not bytes for part in row[3])
                    or sum(len(part) for part in row[3])!=len(row[1])):
                raise Refused('actual_native_caller_full_bank_row','terminal')
            indexed[row[0]]=row
        for snapshot in self.snapshots:
            reader=snapshot['reader']
            row=indexed.get(reader.key)
            if row is None or row[1] is not snapshot['physical']:
                raise Refused('actual_native_caller_full_bank_alias','terminal')
            validate_full_value_arena(snapshot['body'],reader)
            reader.match_roots(snapshot['body']['physical_body'])
            if len(row[2])!=len(reader.aliases):
                raise Refused('actual_native_caller_alias_inventory','terminal')
            for exported,actual in zip(row[2],reader.aliases):
                if exported is not actual:
                    raise Refused('actual_native_caller_alias_correspondence','terminal')
        for row in closes:
            if (type(row) is not tuple or len(row)!=5 or type(row[0]) is not dict
                    or row[0].get('status')!='CLOSED' or row[1]!=1
                    or row[2]!=0 or row[4]!=1):
                raise Refused('actual_native_caller_close_not_confirmed','terminal')
        return packet

    def finish_outside(self):
        if self.source_end is None or self.source_end.get('ownership_retired') is not True:
            return {'status':'SOURCE_END_UNCONFIRMED','ownership_retired':False,'GO':False}
        # Close rows, complete roots, raw errors and actual source-end are all
        # present before one final native document. No second Source seal.
        final_roots={'full_errors':self.full_errors,'actual_Source_end':self.source_end,
            'prepared_bodies':self.prepared_bodies,'Source_receipt_pin':self.receipt.durable_pin,
            'prior_complete_snapshots':self.snapshots,'accepted_mappings':self.accepted_mappings,
            'actual_mapping_ends':self.mapping_ends}
        snapshot=self.capture('native-final-errors-and-end',[final_roots])
        # This exact cut owns the document relation even if a later error
        # appends another snapshot. Never reinterpret the wire as "latest".
        self.native.own_prepare(4096,4096)
        self.final_snapshot=snapshot
        envelope={'schema':'friday.sol086.native-full-completion.v2',
            'owner_pid':self.owner_pid,'generation':self.receipt.generation,'body':snapshot['body']}
        self.native.before_document(self.context,encoded_bound(envelope,maximum=INPUT_MAX))
        self.final_document=canonical(envelope,maximum=INPUT_MAX)
        # C receives full bytes, not a digest or a caller-supplied true bit.
        outcome=self.native.finish_outside(self.context,self.final_document,
            self.source_end,self.prepared_bodies)
        self.final_result=outcome
        return outcome
    @property
    def outside_end_confirmed(self):
        return self.native.outside_end(self.context,self.final_document,self.source_end)
