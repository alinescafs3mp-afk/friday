"""Existing parent's preowned selected body FD, shared before child effects.

Inert text only. No new pathname, role, service, grant, fd-cap raise or retry.
The Root-created PreparedFullBody FD and original prepaid envelope are reused.
A publication is not a receiver receipt; native/ABI/whole cost still required.
A dedicated bank uses its own original-journal row; normal body FD is untouched.
"""
import os
import mmap
import struct
import hashlib
import json

_MAGIC=b'F90BANK1'
_HEAD=struct.Struct('>8sQQQQ32s')
_META=2_000_000
_BASE=0
_META_BYTES=128+_META

class PrefixBodyMailbox:
    def __init__(self,fd,capacity,parent_pid,prepared,metadata_prepared):
        self.fd=fd;self.capacity=capacity;self.parent_pid=parent_pid
        self.prepared=prepared;self.metadata_prepared=metadata_prepared
        self.retained_fds=(fd,metadata_prepared.fd)
        self.owner_pid=os.getpid();self.pid=None
        self.count=0;self.aliases=[];self.raw_packet=None;self.packet=None
        self.attempted=False;self.accepted=None;self.errors=[]
        self.bound=False;self.late=None;self.publication_error=None
        self.ack_attempted=False;self.ack_error=None;self.ack_completed=False
        self.endpoint='preowned-native-prefix-bank:'+prepared.name
        self.map=None;self.meta=None
        self.parent_copies_allocation=0
        self.before=os.fstat(fd)
        if capacity<1 or self.before.st_size!=capacity:
            raise RuntimeError('prefix-bank-actual-prepared-size')
        from selected_owned_values import OwnedMappingBirth
        for attr,carrier,width in (('map_birth',prepared,capacity),('meta_birth',metadata_prepared,_META_BYTES)):
            birth=OwnedMappingBirth.__new__(OwnedMappingBirth)
            if not hasattr(carrier,'selected_mapping_births'):carrier.selected_mapping_births=[]
            carrier.selected_mapping_births.append(birth)
            setattr(self,attr,birth)
            birth.__init__(carrier,width,carrier.name)
            actual=birth.create()
            setattr(self,'map' if attr=='map_birth' else 'meta',actual)
        self.retain_cohort_before_effect()
    @classmethod
    def prepare(cls,carrier,metadata_carrier):
        from capacity import prospective_owned_bank_layout
        from common import DOCUMENT_MAX
        hold=carrier.hold
        row=hold.meter.pending.get(hold.token)
        if row is None or type(row) is not dict:
            raise RuntimeError('prefix-bank-total-original-pool-CODE')
        layout=getattr(hold,'prefix_bank_layout',None)
        if layout is None:
            slots=6 if row.get('purpose')=='full-terminal-forward' else 0
            layout=prospective_owned_bank_layout(row.get('output'),row.get('reads'),
                row.get('hash_bytes'),row.get('allocation'),slots)
            hold.prefix_bank_layout=layout
            hold.prefix_bank_slot=0
        if layout.get('fits') is not True or layout.get('caps_changed') is not False:
            raise RuntimeError('prefix-bank-total-original-pool-CODE')
        slot=getattr(hold,'prefix_bank_slot',None)
        banks=layout.get('banks')
        if type(slot) is not int or type(banks) is not int or slot<0 or slot>=banks:
            raise RuntimeError('prefix-bank-layout-two-banks')
        hold.prefix_bank_slot=slot+1
        body=layout.get('body_output_each')
        meta=layout.get('metadata_file_bytes')
        if meta!=_META_BYTES or type(body) is not int or not 1<=body<=DOCUMENT_MAX:
            raise RuntimeError('prefix-bank-total-original-pool-CODE')
        metadata_carrier.prospective(meta)
        os.ftruncate(metadata_carrier.fd,meta)
        metadata_carrier.hold.commit(output=meta)
        metadata_carrier.count=meta
        carrier.prospective(body)
        os.ftruncate(carrier.fd,body)
        carrier.hold.commit(output=body)
        carrier.count=body
        mailbox=cls.__new__(cls)
        carrier.mapping_mailbox=mailbox
        mailbox.__init__(carrier.fd,body,os.getpid(),carrier,metadata_carrier)
        return mailbox
    def attach_late(self,carrier,metadata):
        if self.bound or self.attempted or self.late is not None or os.getpid()!=self.parent_pid:
            raise RuntimeError('prefix-bank-late-before-fork-only')
        self.late=type(self).prepare(carrier,metadata)
        self.retained_fds=self.retained_fds+self.late.retained_fds
        return self.late
    def retain_cohort_before_effect(self):
        held=getattr(self,'before_effect_cut',None)
        if held is not None:
            return held
        from selected_owned_values import own_mapping
        cuts=[]
        for mapping in (self.map,self.meta):
            birth=own_mapping(mapping)
            if birth is None:raise RuntimeError('prefix-bank-cohort-before-effect')
            cuts.append(birth.cut(self))
        if self.late is not None:
            cuts.extend(self.late.retain_cohort_before_effect())
        self.before_effect_cut=cuts
        return cuts
    def bind_child(self):
        if self.bound or self.attempted or os.getppid()!=self.parent_pid:raise RuntimeError('prefix-bank-actual-parent-once')
        self.bound=True
        self.pid=os.getpid();self.owner_pid=self.pid
        self.meta[:_HEAD.size]=_HEAD.pack(_MAGIC,1,self.pid,0,0,b'\0'*32)
    def add(self,value):
        if self.pid!=os.getpid() or self.attempted:raise RuntimeError('prefix-bank-child-writer')
        if type(value) not in (bytes,bytearray):raise RuntimeError('prefix-bank-body-type')
        raw=value if type(value) is bytes else bytes(value)
        birth=getattr(self,'map_birth',None)
        if birth is not None and raw is getattr(birth,'before_effect_raw',None) and birth.mapping is self.map:
            if self.count!=0 or len(raw)!=self.capacity:
                raise RuntimeError('prefix-bank-cohort-image-occupies-bank-CODE')
            self.aliases.append((value,raw,0));self.count=len(raw)
            return {'offset':0,'bytes':len(raw)}
        width=len(raw)
        if width>self.capacity-_BASE-self.count:raise RuntimeError('prefix-bank-full-body-bound-CODE')
        offset=self.count
        self.map[_BASE+offset:_BASE+offset+width]=raw
        self.aliases.append((value,raw,offset));self.count+=width
        return {'offset':offset,'bytes':width}
    def finish(self):
        from selected_owned_values import require_selected_mapping_cuts
        require_selected_mapping_cuts(self)
        if self.attempted:raise RuntimeError('prefix-bank-body-once')
        for value,raw,offset in self.aliases:
            if len(value)!=len(raw) or memoryview(value)!=memoryview(raw):
                raise RuntimeError('prefix-bank-original-alias-drift')
        return {'endpoint':self.endpoint,'bytes':self.count}
    def publish(self,packet):
        if self.pid!=os.getpid() or self.attempted:raise RuntimeError('prefix-bank-publication-once')
        # Attempt is committed BEFORE ANY serialization/write/flush. A partial
        # publication is never retried on this bank.
        self.attempted=True
        raw=json.dumps(packet,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')+b'\n'
        if not 1<=len(raw)<=_META:raise RuntimeError('prefix-bank-metadata-bound-CODE')
        self.raw_packet=raw;self.packet=packet
        self.meta[128:128+len(raw)]=raw
        digest=hashlib.sha256(raw+bytes(self.map[_BASE:_BASE+self.count])).digest()
        self.meta[:_HEAD.size]=_HEAD.pack(_MAGIC,2,self.pid,len(raw),self.count,digest)
        self.map.flush();self.meta.flush()
        # Physical parent FD existed BEFORE fork and still owns every byte.
        # This is supply only; Root must validate before any acceptance/close.
        return {'owner_pid':self.pid,'parent_pid':self.parent_pid,
            'full_bytes_supplied':True,'receiver_confirmed':False}
    def read_parent(self,pid):
        if os.getpid()!=self.parent_pid:raise RuntimeError('prefix-bank-parent-reader')
        # Existing Root prepaid carrier debits BEFORE full physical read/copy/hash.
        # Header has a fixed bound; no Source-issued native grant or late reserve.
        from selected_owned_values import charge_selected_allocation
        charge_selected_allocation(self.prepared.hold,_HEAD.size*2)
        self.prepared.hold.commit(reads=_HEAD.size)
        first=bytes(self.meta[:_HEAD.size])
        magic,state,owner,n,width,digest=_HEAD.unpack(first)
        if magic!=_MAGIC or state not in (2,3) or owner!=pid:return None
        if not 1<=n<=_META or width>self.capacity-_BASE:raise RuntimeError('prefix-bank-physical-length')
        charge_selected_allocation(self.prepared.hold,6*(n+width)+131072)
        self.prepared.hold.commit(reads=n+width+_HEAD.size,hash_bytes=n+width)
        raw=bytes(self.meta[128:128+n]);body=bytes(self.map[_BASE:_BASE+width])
        if hashlib.sha256(raw+body).digest()!=digest or bytes(self.meta[:_HEAD.size])!=first:
            raise RuntimeError('prefix-bank-physical-full-drift')
        value=json.loads(raw)
        if type(value) is not dict or value.get('owner_pid')!=pid or value.get('parent_pid')!=self.parent_pid:
            raise RuntimeError('prefix-bank-physical-owner')
        self.raw_packet=raw;self.packet=value;self.count=width;self.parent_body=body
        return value
    def require(self,descriptor):
        if descriptor!={'endpoint':'preowned-native-prefix-bank','bytes':self.count} or not hasattr(self,'parent_body'):
            raise RuntimeError('prefix-bank-full-parent-reader')
        return self.parent_body
    def validate_spans(self,nodes,descriptor):
        raw=self.require(descriptor);at=0
        for kind,body in nodes:
            if kind not in ('bytes','bytearray'):continue
            if set(body)!=set(('offset','bytes')) or body['offset']!=at or body['bytes']>len(raw)-at:
                raise RuntimeError('prefix-bank-full-span')
            at+=body['bytes']
        if at!=len(raw):raise RuntimeError('prefix-bank-unreferenced-full-bytes')
        return raw
    def acknowledge(self,pid,accepted):
        if os.getpid()!=self.parent_pid or accepted is not self.packet or self.raw_packet is None:
            raise RuntimeError('prefix-bank-actual-accepted-reader')
        magic,state,owner,n,width,digest=_HEAD.unpack(self.meta[:_HEAD.size])
        if (magic!=_MAGIC or state!=2 or owner!=pid or n!=len(self.raw_packet)
                or width!=len(self.parent_body)
                or bytes(self.meta[128:128+n])!=self.raw_packet):
            raise RuntimeError('prefix-bank-accepted-physical-drift')
        # SAME packet returned by read_parent and strictly decoded by Root.
        if self.ack_attempted:raise RuntimeError('prefix-bank-actual-ack-once')
        self.ack_attempted=True
        self.accepted=accepted
        try:self.meta[:_HEAD.size]=_HEAD.pack(magic,3,owner,n,width,digest)
        except BaseException as error:
            self.ack_error=error
            self.errors.append(error)
            raise
        self.ack_completed=True
    def accepted_by_parent(self):
        magic,state,owner,n,width,digest=_HEAD.unpack(self.meta[:_HEAD.size])
        return magic==_MAGIC and state==3 and owner==self.pid and self.attempted and self.publication_error is None
