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
        self.attempted=True
        self.receiver.native.finish_bank(self.receiver.context,self.key,self.aliases)
        return {'endpoint':self.key,'bytes':self.count}
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
        return raw
    def match_roots(self,descriptor):
        self.require(descriptor)
        from selected_owned_values import require_selected_mapping_cuts
        require_selected_mapping_cuts(self)
        for value,raw,offset in self.aliases:
            if memoryview(value)!=memoryview(raw):raise Refused('native_body_actual_alias_drift','terminal')

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
        self.fdjournal=None;self.receipt=None;self.owner=None;self.serial=0
        self.accepted_mappings=[];self.mapping_ends=[]
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
        # Keep the original error/TB and all aliases BEFORE diagnostic encoding.
        self.serial+=1
        self.native.hold(self.context,'actual-error:'+str(self.serial)+':'+phase,error)
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
        # A failed seal still has an exact original live row, bytes and aliases.
        # Do not retry finish/seal and do not discard an unsealed partial bank.
        if body.raw is not None:
            body.require(body.descriptor());body.match_roots(body.descriptor())
        if self.fdjournal is None:self.fdjournal=OwnedFDs(credit=body.hold,meter=body.store.meter)
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
    def accept_mapping(self,birth,receipt):
        self.require_transfer(receipt)
        if birth.mapping is None or birth.close_attempted:
            raise Refused('selected_mapping_before_actual_close','terminal')
        snapshot=self.capture('selected-mapping-transfer:'+birth.role,[birth.mapping,birth.row,birth.credit])
        self.native.hold(self.context,'accepted-mapping:'+str(id(birth)),(birth,snapshot))
        self.accepted_mappings.append((birth,snapshot))
        return snapshot
    def mapping_closed(self,birth,receipt):
        self.require_transfer(receipt)
        held=self.native.get_anchor(self.context,'accepted-mapping:'+str(id(birth)))
        if held is None or held[0] is not birth or birth.mapping.closed is not True:
            raise Refused('selected_mapping_close_actual_custody','terminal')
        record=(birth,birth.mapping,birth.row,birth.close_attempted,birth.closed_confirmed,birth.close_error)
        self.native.hold(self.context,'mapping-closed:'+str(id(birth)),record)
        self.mapping_ends.append(record)
        return record
    def close_prepared(self,body,receipt):
        self.require_transfer(receipt)
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
        for birth in births:
            if not birth.retire(self,receipt):return False
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
        from common import parse,exact
        value=exact(parse(raw,maximum=INPUT_MAX),('schema','owner_pid','generation','body'),'native_v2_document')
        snapshot=self.snapshots[-1]
        if (value['schema']!='friday.sol086.native-full-completion.v2' or value['owner_pid']!=self.owner_pid
                or value['generation']!=self.receipt.generation or value['body']!=snapshot['body']):
            raise Refused('native_v2_actual_body_correspondence','terminal')
        validate_full_value_arena(value['body'],snapshot['reader'])
        if canonical(value,maximum=INPUT_MAX)!=raw:raise Refused('native_v2_actual_document','terminal')
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
        if (schema!='friday.a229.actual-native-caller-full-bank-transfer.v1'
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
                or type(roots) is not tuple or len(roots)!=28
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
