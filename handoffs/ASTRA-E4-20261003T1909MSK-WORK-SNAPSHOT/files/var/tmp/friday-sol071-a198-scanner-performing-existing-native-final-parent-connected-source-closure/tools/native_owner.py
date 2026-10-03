"""Performing future Root owner. SOURCE ONLY; no run is authorized by A141.

Every effect requires the same actual independently selected opaque permit and
original distinct Root role. No Source JSON, hash equality or local proof can
construct it. Root holds original202; a held pidfd identifies the direct child;
the final pipe terminal supersedes the protocol's provisional COMMIT bytes.
"""
import array
import errno
import mmap
import fcntl
import hashlib
import json
import os
import selectors
import signal
import socket
import stat
import struct
import time

from tools.root_holder import RootBudget,RootHolder,HolderStop,full9
from tools.native_support import selected_owner,RootRetainedCapsule


def canonical(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,
                       allow_nan=False)+'\n').encode('ascii')


class RootNativeOwner:
    """One launch/serve/drain/deadline/reap/custody/publication/terminal owner.

    The publication directory and four slot descriptors are borrowed from the
    actual issuer's independently held lifetime. They are never closed/reopened
    by this owner or inferred from pathname metadata. All other created leases
    are detached before exactly one close; uncertainty retains original quota.
    """
    def __init__(self,permit,selected_permit,selection,resource_contract,launch):
        self.permit=permit
        self.selection=selection
        self.launch=launch
        self.budget=None
        self.holder=None
        self.child=None
        self.pidfd=None
        self.channel=None
        self.leases={}
        self.final_parts=[]
        self.diagnostic_parts=[]
        self.final_size=0
        self.diagnostic_size=0
        # Actual acquired/unlock-uncertain state is fixed in native storage,
        # not a fallible list appended after the flock effect.
        self.slot_locks_native=True
        self.child_reaped=False
        self.child_status=None
        self.provisional=None
        self.protocol_completion_failure=None
        self.source_final=None
        self.final_document=None
        self.source_candidate=None
        self.source_validation_state='UNSEEN'
        self.source_validation_failure=None
        self.source_final_validated=False
        self.pipe_eof=set()
        self.source_native_metrics=None
        self.source_native_metric_failure=None
        self.source_publish_attempted=False
        self.root_terminal=None
        self.closed=False
        self.selector=None
        self.publication_current=None
        self.capsule=None
        self.source_rusage=None
        self.raw_protocol_failure=None
        self.raw_metric_failure=None
        self.source_plane_close_attempted=False
        self.source_plane_close_uncertain=False
        # Owner fields exist before authorization/init failures. The outer run
        # completion consumes full native/host exception and cleanup evidence.
        if permit is None or permit is not selected_permit:
            raise HolderStop('actual selected Root permit absent')
        permit.authorize_holder(selection,resource_contract)
        permit.authorize_native_source_launch(selection,resource_contract,launch)
        native=selected_owner()
        if native is None: raise HolderStop('Root preinitialization allocator image absent')
        physical=native.snapshot()
        if (physical['role']!='actual-root-native-tool' or physical['canonical_workers']!=4 or
            physical.get('selected_final_parent_preowned') is not True or
            physical.get('entry_topology')!='existing-outside-native-parent.v2' or
            physical.get('parent_custody_claimed') is not True or physical.get('existing_observer_entry') is not False or
            physical.get('parent_custody_generation')!=selection.get('generation')):
            raise HolderStop('original distinct native Root binding absent')
        for key in ('max_fds','max_read_bytes','max_work_bytes','max_live_bytes','max_output_bytes','max_wall_ms'):
            if physical[key]!=resource_contract[key]:
                raise HolderStop('native Root/original role ceilings differ')
        self.native=native
        self.budget=RootBudget(resource_contract,terminal_prepaid={'fds':2,
            'work_bytes':physical['terminal_work_bytes'],'live_bytes':physical['terminal_live_bytes'],
            'output_bytes':physical['terminal_output_bytes']},started_ns=physical['started_ns'],
            terminal_wall_ms=physical['terminal_wall_ms'])
        self.budget.reserve(fds=physical['fds'],read_bytes=physical['actual_read_bytes'])
        self.budget.actual_read_bytes=physical['actual_read_bytes']
        self.budget.actual_output_bytes=physical['actual_output_bytes']
        # B is the actual inherited/issuer-borrowed Root baseline, not zero.
        # B+218 includes original202, root directory, Root-owned pipe/pidfd/
        # selector state and ten pre-admitted ancillary error-state rights.
        # Two additional completion slots are escrowed INSIDE original caps.
        if resource_contract['max_fds']<physical['fds']+220:
            raise HolderStop('NOT_FIT: exact native owner topology exceeds original Root FD admission; no cap increase')
        self.started=self.budget.started
        required={'executable_fd','executable_identity9','executable_sha256',
            'entry_fd','entry_identity9','entry_sha256','caps_fd','caps_identity9',
            'bootstrap_fd','bootstrap_identity9','bootstrap_sha256','argv','environment',
            'source_uid','source_gid','source_ceilings','source_native_contract_sha256','slot_fds','slot_identity9',
            'publication_dir_fd','publication_identity9','source_name','terminal_name',
            'cgroup_dir_fd','cgroup_identity9','memory_max_bytes','diagnostic_limit_bytes','parent_topology',
            'completion_original_end_ns'}
        if not isinstance(launch,dict) or set(launch)!=required:
            raise HolderStop('exact native launch/publication contract absent')
        topology=launch['parent_topology']
        expected_topology={'mode':'existing-outside-native-parent-source-birth.v2','birth_parent_pid':os.getpid(),
            'generation':selection.get('generation'),'original_started_ns':physical['started_ns'],
            'original_end_ns':physical['started_ns']+physical['max_wall_ms']*1000000}
        if topology!=expected_topology or selection.get('birth_parent_pid')!=os.getpid() or selection.get('parent_topology')!=topology:
            raise HolderStop('actual independently selected existing Source-parent topology absent')
        if type(launch['completion_original_end_ns']) is not int or launch['completion_original_end_ns']<=self.started:
            raise HolderStop('independently selected existing completion caller original end absent')
        self.budget.original_consumer_end_ns=launch['completion_original_end_ns']
        if launch['source_ceilings'].get('max_fds')!=16 or selection.get('canonical_workers')!=4:
            raise HolderStop('Source16/canonical4 changed')
        if type(launch['source_uid']) is not int or not 0<launch['source_uid']<=4294967295 or type(launch['source_gid']) is not int or not 0<launch['source_gid']<=4294967295:
            raise HolderStop('independently selected unprivileged Source identity absent')
        if launch['source_ceilings'].get('max_read_bytes',0)<1490280568:
            raise HolderStop('explicit whole Source read grant cannot fit original four SHA passes')
        if resource_contract['max_read_bytes']<1490280568:
            raise HolderStop('NOT_FIT: original Root read ceiling cannot fit mandatory four whole SHA passes')
        if launch['memory_max_bytes']!=launch['source_ceilings'].get('max_rss_bytes'):
            raise HolderStop('actual original Source hard memory domain absent')
        if len(launch['slot_fds'])!=4 or len(launch['slot_identity9'])!=4:
            raise HolderStop('original four canonical slots absent')
        for fd,identity in zip(launch['slot_fds'],launch['slot_identity9']):
            if full9(os.fstat(fd))!=identity: raise HolderStop('selected slot description changed')
        for key in ('executable','entry','caps','bootstrap','publication_dir','cgroup_dir'):
            fd=launch[key+'_fd'];identity=launch[key+'_identity9']
            if type(fd) is not int or full9(os.fstat(fd))!=identity:
                raise HolderStop('selected '+key+' held description changed')
        for name in (launch['source_name'],launch['terminal_name']):
            if not isinstance(name,str) or not name or '/' in name or name in ('.','..'):
                raise HolderStop('issuer fixed publication name absent')
        if launch['source_name']==launch['terminal_name']:
            raise HolderStop('publication names collide')
        parent=os.fstat(launch['publication_dir_fd'])
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=0 or stat.S_IMODE(parent.st_mode)!=0o700:
            raise HolderStop('actual private Root publication directory absent')
        self.publication_current=launch['publication_identity9']
        for key in ('executable','entry','bootstrap'):
            self._hash_selected(launch[key+'_fd'],launch[key+'_identity9'],launch[key+'_sha256'])
        seals=fcntl.fcntl(launch['caps_fd'],fcntl.F_GET_SEALS)
        required_seals=fcntl.F_SEAL_SEAL|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_GROW|fcntl.F_SEAL_WRITE
        if seals&required_seals!=required_seals: raise HolderStop('selected native caps description mutable')
        self._check_source_caps()
        self._check_source_bootstrap()

    def _check_source_bootstrap(self):
        """The selected producer's sealed bootstrap must name this real parent.

        This is an additional metadata read, not another full archive pass and
        not authority learned from the contents. The independently selected
        held bootstrap identity/hash was checked before this consumer.
        """
        fd=self.launch['bootstrap_fd'];identity=self.launch['bootstrap_identity9']
        size=int(identity[6]);self.budget.reserve(read_bytes=size,work_bytes=size*8,live_bytes=size*18+8192)
        parts=[];offset=0
        while offset<size:
            self._deadline()
            raw=os.pread(fd,min(65536,size-offset),offset)
            self.budget.actual_read_bytes+=len(raw)
            if not raw:raise HolderStop('Source bootstrap topology short read')
            parts.append(raw);offset+=len(raw)
        raw=b''.join(parts)
        from source.canonical import canonical_loads
        boot=canonical_loads(raw,max_bytes=size,max_depth=256)
        topology=self.launch['parent_topology']
        if (hashlib.sha256(raw).hexdigest()!=self.launch['bootstrap_sha256'] or
            full9(os.fstat(fd))!=identity or tuple(boot['root_peer'])!=(os.getpid(),0,0) or
            boot['generation']!=self.selection['generation'] or boot.get('parent_topology')!={
                key:topology[key] for key in ('mode','birth_parent_pid','generation')}):
            raise HolderStop('actual selected-parent bootstrap/credentials/generation differ')

    def _check_source_caps(self):
        fd=self.launch['caps_fd'];before=os.fstat(fd)
        if before.st_uid!=0 or before.st_nlink!=0 or not stat.S_ISREG(before.st_mode) or before.st_size!=20632:
            raise HolderStop('exact Root-owned sealed Source native ABI absent')
        self.budget.reserve(read_bytes=152,work_bytes=4096,live_bytes=8192)
        raw=os.pread(fd,152,0);self.budget.actual_read_bytes+=len(raw)
        if len(raw)!=152:raise HolderStop('Source native cap header short read')
        fields=struct.unpack('<14Q32sQ',raw);raw=None
        ceilings=self.launch['source_ceilings']
        if fields[:5]!=(0x4652494441594131,1,1,4,16):raise HolderStop('actual Source16/canonical4 native header differs')
        for index,key in ((5,'max_live_bytes'),(6,'max_work_bytes'),(7,'max_read_bytes'),(8,'max_output_bytes'),(9,'max_wall_ms'),(10,'max_rss_bytes')):
            if fields[index]!=ceilings[key]:raise HolderStop('actual sealed Source original ceiling differs: '+key)
        if not 0<fields[11]<fields[5] or not 0<fields[12]<fields[6] or not 0<fields[13]<=fields[8]:raise HolderStop('original Source terminal escrow cannot fit')
        if fields[14].hex()!=self.launch['source_native_contract_sha256'] or full9(os.fstat(fd))!=self.launch['caps_identity9']:
            raise HolderStop('Source full native contract/header custody differs')
        self.source_caps_header=fields

    def _hash_selected(self,fd,identity,expected):
        size=int(identity[6]);offset=0;state=hashlib.sha256()
        self.budget.reserve(work_bytes=8192,live_bytes=8192)
        while offset<size:
            amount=min(65536,size-offset)
            self.budget.reserve(read_bytes=amount,work_bytes=amount,live_bytes=amount+64)
            piece=None
            try:
                piece=os.pread(fd,amount,offset);self.budget.actual_read_bytes+=len(piece)
                if not piece: raise HolderStop('selected native text/binary short read')
                state.update(piece);offset+=len(piece)
            finally: piece=None;self.budget.release('live_bytes',amount+64)
        digest=state.hexdigest();state=None;self.budget.release('live_bytes',8192)
        if digest!=expected or full9(os.fstat(fd))!=identity:
            raise HolderStop('selected native text/binary fullSHA9 mismatch')

    def _lease(self,fd,label):
        # Slot keys are allocated before the kernel producer. Native FD wrappers
        # additionally register descriptors before Python can allocate an int.
        if label not in self.leases: raise HolderStop('unprepared descriptor slot')
        self.leases[label]=fd
        return fd

    def _close(self,label):
        fd=self.leases.pop(label,None)
        if fd is None: return
        self.budget.close_fd(fd,label)

    def _open(self,name,flags,parent,label):
        self.budget.reserve(fds=1,work_bytes=4096,live_bytes=4096)
        self.leases[label]=None
        try: return self._lease(os.open(name,flags,0o600,dir_fd=parent),label)
        except BaseException:
            if self.leases.get(label) is None:
                self.leases.pop(label,None);self.budget.release('fds',1)
            raise

    def _deadline(self):
        end=self.budget.operation_end_ns()
        remaining=end-time.monotonic_ns()
        if remaining<=0: raise TimeoutError('original Root/source blocking deadline')
        return remaining/1000000000

    def _lock_slots(self):
        for ordinal in (0,1):
            fd=self.launch['slot_fds'][ordinal]
            self.native.lock_canonical_slot(fd,ordinal,self.selection['generation'])

    def _put_source_in_original_memory_domain(self):
        directory=self.launch['cgroup_dir_fd']
        fd=self._open('memory.max',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,directory,'memory.max')
        self.budget.reserve(read_bytes=128,work_bytes=512,live_bytes=512)
        raw=os.read(fd,128);self.budget.actual_read_bytes+=len(raw)
        if raw!=str(self.launch['memory_max_bytes']).encode('ascii')+b'\n':
            raise HolderStop('actual original Source cgroup memory ceiling differs')
        raw=None;self._close('memory.max')
        fd=self._open('cgroup.procs',os.O_WRONLY|os.O_NOFOLLOW|os.O_CLOEXEC,directory,'cgroup.procs')
        raw=(str(self.child)+'\n').encode('ascii')
        self.budget.reserve(work_bytes=len(raw),output_bytes=len(raw))
        if os.write(fd,raw)!=len(raw): raise HolderStop('Source memory-domain placement incomplete')
        self.budget.actual_output_bytes+=len(raw);raw=None;self._close('cgroup.procs')
        fd=self._open('cgroup.procs',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,directory,'cgroup.procs.read')
        self.budget.reserve(read_bytes=4096,work_bytes=8192,live_bytes=16384)
        raw=os.read(fd,4096);self.budget.actual_read_bytes+=len(raw)
        if raw!=(str(self.child)+'\n').encode('ascii'):
            raise HolderStop('selected Source memory domain is not exclusive')
        raw=None;self._close('cgroup.procs.read')
        if self.budget.close_uncertainties: raise HolderStop('memory-domain lease close unconfirmed')


    def _close_source_plane_once(self,fd,node):
        # The prospective native/Root-backed raw node and detached lease exist
        # BEFORE the sole close. An uncertain descriptor is never refunded or
        # retried, including failure of the secondary exception recorder.
        if self.source_plane_close_attempted:
            raise HolderStop('source plane duplicate close already attempted')
        self.source_plane_close_attempted=True
        caller=self.capsule.existing_caller
        try:os.close(fd)
        except OSError as exc:
            self.source_plane_close_uncertain=True
            caller.capture_raw_origin(node,exc,self.capsule)
            caller.record_uncertainty(self.budget,node,'Root.source_plane.close')
            self.budget.retain_origin(exc,'Root.source_plane.close')
            return False
        self.budget.release('fds',1)
        self.source_plane_writable_closed=True
        return True

    def _preown_registered_source_plane(self):
        """Called before clone3. Parent keeps a read-only view of Source-owned backing.

        The writable descriptor is queued on the existing channel and then closed
        once. Root private carrier memory is not placed in this backing. A failed
        close refuses birth. This is not an ACK, a pidfd, a name, or peer adoption.
        """
        ceilings=self.launch['source_ceilings']
        if type(ceilings.get('max_live_bytes')) is not int or type(ceilings.get('max_output_bytes')) is not int:
            raise HolderStop('original Source live/output ceilings absent')
        try:
            plane=self.native.source_backing_extent(ceilings['max_live_bytes'], ceilings['max_output_bytes'])
        except Exception:
            raise HolderStop('NOT_FIT: original packet body and header exceed sealed source live/output ceiling')
        if type(plane) is not int or plane<=0:
            raise HolderStop('NOT_FIT: source backing extent absent')
        if plane!=ceilings['max_live_bytes']:
            raise HolderStop('registered Source backing must replace, not add to, the exact original allocator total')
        prepaid=0 if self.budget.terminal else self.budget.terminal_prepaid.get('live_bytes',0)
        room=self.budget.contract['max_live_bytes']-self.budget.used['live_bytes']-prepaid
        if plane>room:
            raise HolderStop('NOT_FIT: registered source plane exceeds remaining original Root live room')
        self.budget.reserve(fds=1,live_bytes=plane,work_bytes=4096)
        fields=self.source_caps_header
        caller=self.capsule.existing_caller
        producer_origin=caller.prepare_raw_origin('Root.source_plane.prebirth_handover')
        close_origin=caller.prepare_raw_origin('Root.source_plane.close_once')
        fd=None
        try:
            fd=self.native.borrow_preowned_source_backing(plane,self.selection['generation'],fields[14])
            self.source_plane_view=self.native.preowned_source_backing_view()
            if len(self.source_plane_view)!=plane or self.source_plane_view.readonly is not True:
                raise HolderStop('same full native preowned Source view is not readonly')
            # Both actual receiving objects own the full read-only Source
            # storage BEFORE birth and any terminal parse/projection failure.
            if self.holder is None:raise HolderStop('original prebirth holder absent')
            self.holder.adopted_source_plane=self.source_plane_view
            self.native.note_source_plane_readonly(plane,fields[11],fields[8],
                                                   self.selection['generation'],fields[14],fields[9])
            ancillary=array.array('i',[fd])
            envelope=b'FRCUSTO2'+struct.pack('<Q',self.selection['generation'])+fields[14]
            self.budget.reserve(work_bytes=len(envelope),output_bytes=len(envelope))
            sent=self.channel.sendmsg([envelope],[(socket.SOL_SOCKET, socket.SCM_RIGHTS, ancillary.tobytes())])
            self.budget.actual_output_bytes+=sent
            if sent!=len(envelope):
                raise HolderStop('registered source plane handover incomplete')
        except BaseException as primary:
            caller.capture_raw_origin(producer_origin,primary,self.capsule)
            # A failed native PyLong return may already own the duplicate in
            # its actual FD table. No guessed descriptor or quota refund.
            if fd is not None:self._close_source_plane_once(fd,close_origin)
            raise
        if not self._close_source_plane_once(fd,close_origin):
            raise HolderStop('source plane writable close unconfirmed')

    def _accept_registered_source_plane(self):
        view=getattr(self,'source_plane_view',None)
        if view is None:
            raise HolderStop('registered source plane absent at the existing parent receiver')
        # Prospectively bind the finite native range/record inspection before
        # the receiver reads any byte. Full physical storage is retained already;
        # this is not a success receipt or re-execution of Source.
        extent=len(view)
        self.budget.reserve(read_bytes=extent,work_bytes=extent)
        inspected_before=self.native.snapshot().get('source_allocator_inspection_bytes',0)
        try:
            self.native.accept_source_plane_bytes(view)
        finally:
            # On rejected terminal prefixes, exact native inspection accounting
            # survives as native state; the existing outer caller retains any
            # secondary snapshot/projection error rather than inventing zero.
            physical=self.native.snapshot()
            inspected_after=physical.get('source_allocator_inspection_bytes')
            if type(inspected_after) is not int or type(inspected_before) is not int or not inspected_before<=inspected_after:
                raise HolderStop('native Source backing inspection accounting unavailable')
            self.budget.actual_read_bytes+=inspected_after-inspected_before
        kind=physical.get('plane_commit_kind')
        if kind!=4 or physical.get('plane_not_fit') not in (0, False) or physical.get('plane_confirmed_revoked') not in (0, False):
            raise HolderStop('source plane receiver refused unconfirmed backing')
        if physical.get('outside_peer_adoption') is not False or physical.get('python_bodies_byte_exported') is not False:
            raise HolderStop('pidfd, name, or same-TGID alias is not backing transfer')
        body=int(physical.get('plane_packet_bytes_retained') or 0)
        self.budget.reserve(work_bytes=4096)
        adopted={'backing_bytes':body,'range_count':int(physical.get('plane_range_count') or 0),
                 'generation':int(physical.get('plane_generation') or 0),
                 'commit_kind':kind,'storage':'preheld-source-backing'}
        self.adopted_source_body=adopted
        self.source_plane_commit_kind=kind
        if self.holder is not None:
            self.holder.adopted_source_body=adopted
            self.holder.adopted_source_plane=view

    def launch_source(self):
        # Every ownership edge below belongs to the fixed native existing
        # caller, not to a local bounded role2 main. Recheck before any birth;
        # the native spawn primitive independently enforces this same fact.
        physical=self.native.snapshot()
        if (physical.get('entry_topology')!='existing-outside-native-parent.v2' or
            physical.get('parent_custody_claimed') is not True or
            physical.get('parent_custody_generation')!=self.selection['generation']):
            raise HolderStop('actual existing-parent pre-birth custody lost')
        if physical.get('selected_caller_returns') is not True:
            raise HolderStop('bounded birth refused: selected caller would exit as sole owner')
        if physical.get('selected_final_parent_preowned') is not True:
            raise HolderStop('full privileged Root backing absent before Source birth')
        if physical.get('registered_carrier_accepted_before_birth') is not True:
            raise HolderStop('bounded birth refused: registered carrier was not accepted before birth')
        if physical.get('outside_peer_adoption') is not False:
            raise HolderStop('bounded birth refused: peer adoption is not parenthood')
        if physical.get('source_plane_readonly_accepted') is True:
            raise HolderStop('source plane was marked accepted before this preowner')
        remaining=self._deadline()
        if self.launch['source_ceilings']['max_wall_ms']*1000000>int(remaining*1000000000):
            raise HolderStop('NOT_FIT: original Source interval cannot fit existing parent work cutoff and cleanup reserve')
        self._lock_slots()
        # The actual selected existing parent acquires and owns the COMPLETE
        # original generation BEFORE clone3. Constructor/prefix custody is
        # reachable by the outer capsule before its first acquisition effect.
        self.holder=RootHolder.__new__(RootHolder)
        RootHolder.__init__(self.holder,None,self.selection,self.permit,self.permit,
                            self.budget.contract,budget=self.budget)
        self.holder.io_owner=self
        # Acquiring the whole original generation has real body/metadata cost.
        # Recheck AFTER it, not only before that potentially long operation.
        if self.launch['source_ceilings']['max_wall_ms']*1000000>int(self._deadline()*1000000000):
            raise HolderStop('NOT_FIT: complete acquisition used the original Source/cleanup interval')
        self.budget.reserve(fds=8,work_bytes=32768,live_bytes=131072)
        for label in ('root_channel','child_channel','out_read','out_write','err_read','err_write','release_read','release_write','pidfd'):
            self.leases[label]=None
        root_channel,child_channel=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET|socket.SOCK_CLOEXEC)
        root_channel.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
        child_channel.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
        self.channel=root_channel
        self.leases['root_channel']=root_channel.fileno()
        self.leases['child_channel']=child_channel.fileno()
        out_read,out_write=os.pipe2(os.O_CLOEXEC);self.leases['out_read']=out_read;self.leases['out_write']=out_write
        err_read,err_write=os.pipe2(os.O_CLOEXEC);self.leases['err_read']=err_read;self.leases['err_write']=err_write
        release_read,release_write=os.pipe2(os.O_CLOEXEC);self.leases['release_read']=release_read;self.leases['release_write']=release_write
        # The native C trampoline atomically obtains a pidfd at clone3 birth;
        # it performs fixed descriptor/UID isolation without Python allocation.
        self.budget.reserve(fds=1,work_bytes=4096)
        sources=(out_write,err_write,child_channel.fileno(),self.launch['entry_fd'],
            self.launch['caps_fd'],self.launch['executable_fd'],self.launch['bootstrap_fd'],release_read)
        self._preown_registered_source_plane()
        self.child,self.pidfd=self.native.spawn_source(sources,self.launch['source_uid'],
            self.launch['source_gid'],self.launch['argv'],self.launch['environment'])
        self.leases['pidfd']=self.pidfd
        child_channel.detach();self._close('child_channel')
        for label in ('out_write','err_write','release_read'): self._close(label)
        self._put_source_in_original_memory_domain()
        peer=(self.child,self.launch['source_uid'],self.launch['source_gid'])
        # The same existing original202 owner also performed clone3 and has
        # the genuine direct wait relationship. No grandchild/SCM adoption is
        # asserted by relabeling the already-held Source pidfd.
        channel=self.channel;self.channel=None;self.leases.pop('root_channel')
        self.holder.bind_source_channel(channel,peer)
        for label in ('out_read','err_read'): os.set_blocking(self.leases[label],False)
        self.budget.reserve(fds=1,work_bytes=8192,live_bytes=16384)
        self.selector=selectors.DefaultSelector()
        self.selector.register(self.holder.channel.fileno(),selectors.EVENT_READ,'protocol')
        for label in ('out_read','err_read'):
            self.selector.register(self.leases[label],selectors.EVENT_READ,label)
        self.selector.register(self.pidfd,selectors.EVENT_READ,'pidfd')
        # SAME original release pipe. Source remains in the fixed privileged
        # exec trampoline until exec erased all Root mappings; selected native
        # main consumes this record and drops UID/GID BEFORE Source/Python.
        birth=self.native.snapshot()['source_birth_started_ns']
        release=b'FRDROP71'+struct.pack('<QQII32s',self.selection['generation'],birth,
            self.launch['source_uid'],self.launch['source_gid'],self.source_caps_header[14])
        self.budget.reserve(work_bytes=len(release),output_bytes=len(release))
        written=os.write(self.leases['release_write'],release)
        self.budget.actual_output_bytes+=written
        if written!=len(release):raise HolderStop('fixed selected Source privilege-release record incomplete')
        self._close('release_write')
        try:
            self.provisional=self.holder.serve()
        except BaseException as protocol_failure:
            # A Source REFUSED (including its late close successor) closes the
            # protocol before writing/finalizing stdout. Do NOT kill it merely
            # for that EOF: the same deadline owner must first consume both
            # pipes and direct exit. Invalid/noncompleted terminals still fail
            # and the outer owner performs held-pidfd cancellation.
            from source.causes import exception_detail
            self.raw_protocol_failure=protocol_failure
            self.protocol_completion_failure=exception_detail(protocol_failure)
            self._drain_and_reap()
            if not self.source_final_validated or self.final_document.get('status')!='REFUSED':raise
        else:self._drain_and_reap()

    def _take_pipe_piece(self,key):
        label=key.data
        limit=self.launch['source_ceilings']['max_output_bytes'] if label=='out_read' else self.launch['diagnostic_limit_bytes']
        used=self.final_size if label=='out_read' else self.diagnostic_size
        amount=min(65536,limit-used+1)
        if amount<=0:
            raise HolderStop('pipe retained prefix exhausted original cap; EOF not observed')
        self.budget.reserve(read_bytes=amount,work_bytes=amount,live_bytes=amount+128)
        piece=os.read(key.fd,amount);self.budget.actual_read_bytes+=len(piece)
        if not piece:
            self.pipe_eof.add(label)
            self.selector.unregister(key.fd);self._close(label)
            piece=None;self.budget.release('live_bytes',amount+128)
        else:
            # Own every actually received byte BEFORE cap/canonical/schema
            # projection. A bounded cap+1 prefix remains forensic, never EOF.
            if label=='out_read':self.final_parts.append(piece);self.final_size+=len(piece)
            else:self.diagnostic_parts.append(piece);self.diagnostic_size+=len(piece)
            if used+len(piece)>limit:
                if label=='out_read':self.source_validation_state='REJECTED_LIMIT_PREFIX'
                raise HolderStop('full terminal/diagnostic pipe exceeds original cap')

    def _observe_direct_exit(self):
        got=self.native.direct_source_wait()
        if got is None:return
        pid,status,usage=got
        if pid!=self.child: raise HolderStop('direct child wait/reap mismatch')
        self.child_status=status;self.child_reaped=True
        self.source_rusage=usage
        self._accept_registered_source_plane()
        self.selector.unregister(self.pidfd)

    def wait_protocol_readable(self):
        """Drain BOTH output pipes while a protocol operation waits.

        This is the concrete blocking owner, not an unimplemented callback.
        A startup/native failure cannot deadlock on stderr while Root waits for
        READY/BORROW/COMMIT. All bytes remain retained under original caps.
        """
        while True:
            ready=self.selector.select(self._deadline())
            if not ready: raise TimeoutError('Root protocol/pipe deadline')
            protocol=False
            for key,mask in ready:
                if key.data=='protocol': protocol=True
                elif key.data=='pidfd': self._observe_direct_exit()
                else: self._take_pipe_piece(key)
            if protocol: return
            if self.child_reaped: raise HolderStop('Source exited before protocol completion')

    def _drain_and_reap(self):
        if self.holder is not None and self.holder.channel is not None:
            try:self.selector.unregister(self.holder.channel.fileno())
            except KeyError:pass
        while any(label in self.leases for label in ('out_read','err_read')) or not self.child_reaped:
                events=self.selector.select(self._deadline())
                if not events: raise TimeoutError('Source exit/pipe deadline')
                for key,mask in events:
                    if key.data=='pidfd':
                        self._observe_direct_exit()
                    else: self._take_pipe_piece(key)
        self._retain_final_source()
        if not os.WIFEXITED(self.child_status) or os.WEXITSTATUS(self.child_status)!=0:
            raise HolderStop('selected Source/native completion failed')

    def _retain_final_source(self):
        if self.source_final_validated:return
        if self.source_validation_state.startswith('REJECTED'):
            raise HolderStop('final Source candidate previously rejected; forensic custody only')
        if not self.child_reaped or 'out_read' not in self.pipe_eof:
            raise HolderStop('final Source candidate has no exact direct exit/stdout EOF')
        # Raw fragments remain owned throughout validation, including failure.
        # The joined candidate has forensic identity, never final eligibility.
        self.budget.reserve(work_bytes=self.final_size*8,live_bytes=self.final_size*18+8192)
        self.source_candidate=b''.join(self.final_parts)
        self.source_validation_state='VALIDATING'
        try:
            from source.canonical import canonical_loads
            from source.schema_validate import validate_document,RESULT_SCHEMA
            from source.bounds import new_meter
            document=canonical_loads(self.source_candidate,max_bytes=self.launch['source_ceilings']['max_output_bytes'],max_depth=256)
            validator_meter=new_meter()
            validator_meter['native_owner']=self.native
            cause=validate_document(document,RESULT_SCHEMA,None,validator_meter)
            # Origin catches in actual schema metadata consumers belong to
            # Root's terminal too; a typed schema cause cannot erase them.
            self.budget.owned_origin_exceptions.extend(validator_meter.get('owned_origin_exceptions',[]))
            self.budget.origin_failures.extend(validator_meter.get('origin_failures',[]))
            if cause:raise HolderStop('exact final Source terminal schema: '+cause)
            if document.get('status') not in ('OBSERVED','REFUSED','NOT_COVERED','NOT_PROVEN'):
                raise HolderStop('exact final Source terminal status')
            committed=self.holder.source_output if self.holder is not None else None
            if document['status']=='OBSERVED' and self.source_candidate!=committed:
                raise HolderStop('final observed terminal differs from provisional COMMIT')
        except BaseException as validation_failure:
            self.source_validation_state='REJECTED'
            self.source_validation_failure=self.budget.retain_origin(validation_failure,'Source.final_terminal.validation')
            raise
        # A close-uncertainty REFUSED successor is real final pipe DATA. Never
        # substitute the earlier observed/provisionally committed bytes for it.
        self.source_final=self.source_candidate
        self.final_document=document
        self.source_final_validated=True
        self.source_validation_state='VALIDATED_COMPLETE'
        try:self._retain_native_metrics()
        except BaseException as metric_failure:
            from source.causes import exception_detail
            self.raw_metric_failure=metric_failure
            self.source_native_metric_failure=exception_detail(metric_failure)
            # Valid exact Source wire DATA remains retained/publishable. A
            # missing native observation refuses qualification, not custody
            # of already complete canonical bytes or the original exception.

    def _retain_native_metrics(self):
        if self.source_native_metrics is not None:return
        self.budget.reserve(work_bytes=self.diagnostic_size*4+8192,live_bytes=self.diagnostic_size+8192)
        raw=b''.join(self.diagnostic_parts)
        if len(raw)<152:raise HolderStop('full native finalization trailer absent')
        values=struct.unpack('<15Q32s',raw[-152:]);raw=None
        if values[:4]!=(0x4652494441594d31,2,1,self.child) or values[15].hex()!=self.launch['source_native_contract_sha256']:
            raise HolderStop('actual Source finalization/image/role trailer mismatch')
        names=('magic','version','role','owner_pid','actual_read_bytes','actual_output_before_native_trailer',
            'allocation_work_bytes','physical_live_bytes','physical_peak_upper_bytes',
            'active_fds','uncertain_fds','failed','started_ns','elapsed_ns','finalized_status')
        self.source_native_metrics=dict(zip(names,values[:15]))
        # The complete 152-byte trailer was actually consumed after pipe EOF,
        # not pre-added to a native counter as an intended write.
        self.source_native_metrics['native_trailer_delivered_bytes']=152
        self.source_native_metrics['actual_output_bytes']=values[5]+152
        self.source_native_metrics['binding_sha256']=values[15].hex()
        self.source_native_metrics['phase']='AFTER_INTERPRETER_FINALIZATION'
        self.source_native_metrics['peak_kind']='SUM_OF_ARENA_PEAKS_CONSERVATIVE_UPPER_NOT_RSS'

    def complete_error_pipes(self):
        """Capture all remaining finite output after direct-child cancellation.

        The same original deadline/caps remain in force. Failure to establish
        EOF is preserved as cleanup failure; retained prefixes are never passed
        off as a complete terminal. No unbounded second drain is introduced.
        """
        if self.selector is None or not self.child_reaped:return
        if self.holder is not None and self.holder.channel is not None:
            try:self.selector.unregister(self.holder.channel.fileno())
            except KeyError:pass
        while any(label in self.leases for label in ('out_read','err_read')):
            events=self.selector.select(self._deadline())
            if not events:raise TimeoutError('error pipe completion original deadline')
            for key,mask in events:
                if key.data in ('out_read','err_read'):self._take_pipe_piece(key)
        if self.final_size:self._retain_final_source()

    def cancel_and_reap(self):
        # Native direct-child commit precedes snapshot/tuple allocations and
        # covers a birth whose Python constructor never received its pair.
        self.native.begin_terminal()
        self.native.retire_direct_children()
        if self.child is None:
            pending=self.native.snapshot()
            if pending['pending_child']>=0:
                self.child=pending['pending_child'];self.pidfd=pending['pending_pidfd']
                self.leases['pidfd']=self.pidfd
        if self.child is None or self.child_reaped: return
        # A prior native wait may have consumed the exact child before Python
        # could allocate its result. Reconsume the fixed result, not waitid on
        # an already-reaped generation and not a fabricated return status.
        if self.pidfd is not None:
            got=self.native.direct_source_wait()
            if got is not None:
                if got[0]!=self.child:raise HolderStop('retained native Source wait generation differs')
                self.child_reaped=True;self.child_status=got[1];self.source_rusage=got[2];return
        if self.pidfd is None:
            # Fork succeeded but no held pidfd was obtained; do NOT numeric kill
            # or declare cleanup. Direct-child wait is nonblocking and truthful.
            pid,status=os.waitpid(self.child,os.WNOHANG)
            if pid==self.child: self.child_reaped=True;self.child_status=status
            else: raise HolderStop('STOP_UNCONFIRMED: live child without held pidfd')
            return
        observed=os.waitid(os.P_PIDFD,self.pidfd,os.WEXITED|os.WNOWAIT|os.WNOHANG)
        if observed is not None:
            got=self.native.direct_source_wait()
            if got is not None and got[0]==self.child:
                self.child_reaped=True;self.child_status=got[1];self.source_rusage=got[2];return
        try:signal.pidfd_send_signal(self.pidfd,signal.SIGKILL,None,0)
        except ProcessLookupError:
            # Exit may race the held-pidfd signal. This is not a second signal
            # or numeric target: consume the exact direct child's exit once.
            observed=os.waitid(os.P_PIDFD,self.pidfd,os.WEXITED|os.WNOWAIT|os.WNOHANG)
            got=self.native.direct_source_wait()
            if observed is None or got is None or got[0]!=self.child:raise HolderStop('STOP_UNCONFIRMED: pidfd exit race not directly reaped')
            self.child_reaped=True;self.child_status=got[1];self.source_rusage=got[2];return
        remaining=self._deadline()
        self.budget.reserve(fds=1,work_bytes=8192,live_bytes=16384)
        selector=selectors.DefaultSelector()
        try:
            selector.register(self.pidfd,selectors.EVENT_READ)
            if not selector.select(remaining): raise HolderStop('STOP_UNCONFIRMED: pidfd reap deadline')
            observed=os.waitid(os.P_PIDFD,self.pidfd,os.WEXITED|os.WNOWAIT|os.WNOHANG)
            got=self.native.direct_source_wait()
            if observed is None or got is None or got[0]!=self.child: raise HolderStop('STOP_UNCONFIRMED: direct wait mismatch')
            self.child_reaped=True;self.child_status=got[1];self.source_rusage=got[2]
        finally:
            try: selector.close()
            except BaseException as exc:
                self.budget.retain_origin(exc,'Root.cancel.selector.close')
                self.budget.close_uncertainties.append({'operation':'cancel.selector.close','charged':True})
                raise
            else: self.budget.release('fds',1)

    def _publish_bytes(self,name,raw):
        parent=self.launch['publication_dir_fd']
        if full9(os.fstat(parent))!=self.publication_current:
            raise HolderStop('original publication parent changed')
        previous_parent=self.publication_current
        self.budget.reserve(output_bytes=len(raw),work_bytes=len(raw)*2+8192,live_bytes=8192)
        fd=self._open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,parent,'publish:'+name)
        offset=0
        try:
            while offset<len(raw):
                self._deadline()
                wrote=os.write(fd,memoryview(raw)[offset:offset+65536])
                if wrote<1: raise HolderStop('Root publication short write')
                offset+=wrote;self.budget.actual_output_bytes+=wrote
            os.fsync(fd)
            observed=full9(os.fstat(fd))
        finally: self._close('publish:'+name)
        os.fsync(parent)
        if self.budget.close_uncertainties: raise HolderStop('publication lease close unconfirmed')
        if full9(os.stat(name,dir_fd=parent,follow_symlinks=False))!=observed:
            raise HolderStop('publication named final identity changed')
        current_parent=full9(os.fstat(parent))
        if current_parent[:6]!=previous_parent[:6]:
            raise HolderStop('owned publication parent stable fields changed')
        # Only our exclusive regular-file creation changes size/mtime/ctime.
        # This owned transition becomes the exact precondition of the second
        # publication; the issuer's original selection is never rewritten.
        self.publication_current=current_parent
        return {'name':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                'identity9':observed,'parent_identity9_before':previous_parent,
                'parent_identity9':current_parent}

    def _completion_handoff(self,value):
        """Deliver a noncircular receipt to the existing actual Root parent.

        This message is an AFTER_PUBLICATION milestone, not final-end evidence.
        The concrete outside observer below joins its actual delivery, native
        finalization trailer, BOTH EOFs and direct wait4 before end eligibility.
        """
        limit=self.budget.contract['max_output_bytes']-self.budget.used['output_bytes']
        self.budget.reserve(work_bytes=limit*8+8192,live_bytes=limit*4+8192)
        raw=canonical(value)
        self.budget.reserve(output_bytes=len(raw))
        if self.native.send_root_completion(raw,min(self.capsule.original_end_ns,
                self.launch['completion_original_end_ns']))!=len(raw):
            raise HolderStop('STOP_UNCONFIRMED: outside Root completion receipt delivery incomplete')
        self.budget.actual_output_bytes+=len(raw)
        # Actual outside receiver acceptance, after its full terminal body/SHA9
        # equality, precedes native finalization/view retirement. This is bytes
        # acceptance, NOT transfer of originals/direct parenthood: those were
        # already preowned in the actual existing Source parent before birth.
        self.budget.reserve(read_bytes=4096,work_bytes=32768,live_bytes=81920)
        ack,fd,pid,uid,gid=self.native.recv_packet(3,4096,0,
            min(self.started+self.budget.contract['max_wall_ms']*1000000,
                self.launch['completion_original_end_ns']))
        self.budget.actual_read_bytes+=len(ack)
        self.capsule.peer_acceptance=ack
        from source.canonical import canonical_loads
        accepted=canonical_loads(ack,max_bytes=4096,max_depth=64)
        expected={'schema':'friday.scanner.root-parent-accepted.v2','root_pid':os.getpid(),
            'generation':self.selection['generation'],'started_ns':self.started,
            'original_end_ns':self.capsule.original_end_ns,
            'consumer_original_end_ns':self.launch['completion_original_end_ns'],
            'handoff_sha256':hashlib.sha256(raw).hexdigest(),
            'receipt_sha256':value['receipt']['sha256'] if value.get('receipt') is not None else None,
            'status':value['status'],'GO':False}
        if ((pid,uid,gid)!=(self.native.snapshot()['root_completion_peer'],0,0) or fd!=-1 or accepted!=expected):
            raise HolderStop('STOP_UNCONFIRMED: actual selected outside caller receipt acceptance differs')
        self.native.accept_existing_parent_delivery(self.capsule.existing_caller,self.selection['generation'])
        # C retains this exact performed ledger through Py_FinalizeEx; no
        # Python snapshot before its own encoder is called a final-end total.
        self.native.capture_root_ledger(tuple(self.budget.used[key] for key in
            ('fds','read_bytes','work_bytes','live_bytes','output_bytes')),
            self.budget.actual_read_bytes,self.budget.actual_output_bytes)

    def close(self):
        if self.closed: return
        if self.child is not None and not self.child_reaped:
            # Keep held pidfd, Root originals and canonical slots owned. The
            # returned STOP_UNCONFIRMED capsule retains this actual owner;
            # closing them would erase control of an unreaped direct child.
            raise HolderStop('STOP_UNCONFIRMED: owned live child retained')
        self.closed=True
        if self.selector is not None:
            selector=self.selector;self.selector=None
            try: selector.close()
            except OSError as exc:
                self.budget.retain_origin(exc,'Root.selector.close')
                self.budget.close_uncertainties.append({'operation':'selector.close',
                    'exception_class':type(exc).__name__,'message':str(exc),'errno':exc.errno,'charged':True})
            else: self.budget.release('fds',1)
        if self.holder is not None: self.holder.close()
        elif self.channel is not None:
            self.channel.detach();self.channel=None
        for label in tuple(self.leases): self._close(label)
        try:self.native.unlock_canonical_slots()
        except BaseException as exc:
            # Native once-state remains owned even if either recorder fails.
            self.capsule.retain(exc)
            self.budget.retain_origin(exc,'Root.canonical_slot.unlock.native_once')
            self.budget.close_uncertainties.append({'operation':'canonical_slot.unlock.native_once',
                'exception_class':type(exc).__name__,'charged':True,'attempted_once':True})


def run_selected_source(permit,selected_permit,selection,resource_contract,launch,capsule=None):
    """Finite actual Root entry; all failures return full owner terminal evidence.

    The exact final Source publication occurs while all202 original descriptions
    remain held. The independent Root terminal is formed after owned cleanup.
    A failed final receipt publication is STOP_UNCONFIRMED, never acceptance.
    """
    if type(capsule) is not RootRetainedCapsule:
        raise HolderStop('actual preowned caller capsule absent')
    owner=None;primary=None;publication=None;cleanup=[]
    from source.causes import exception_detail
    try:
        owner=RootNativeOwner.__new__(RootNativeOwner)
        capsule.attach(owner)
        owner.__init__(permit,selected_permit,selection,resource_contract,launch)
        capsule.attach(owner);owner.capsule=capsule
        # The physical owner already preceded capsule/constructor allocation.
        # Reserve the complete finite return/control envelope before producers;
        # this is logical escrow inside original caps, never measured RSS.
        owner.budget.reserve(work_bytes=32768,live_bytes=65536)
        owner.launch_source()
        owner.holder.verify_whole()
        if not owner.source_final_validated:
            raise HolderStop('exact final Source terminal not validated; forensic bytes cannot publish as final')
        owner.source_publish_attempted=True
        publication=owner._publish_bytes(launch['source_name'],owner.source_final)
    except BaseException as exc:
        capsule.retain(exc)
        primary=exception_detail(exc)
    finally:
        if owner is not None and owner.budget is not None:
            owner.native.begin_terminal()
            owner.budget.begin_terminal()
            try: owner.cancel_and_reap()
            except BaseException as exc:
                capsule.retain(exc);cleanup.append(exception_detail(exc))
            try:
                owner.complete_error_pipes()
                if not owner.source_publish_attempted and owner.source_final_validated:
                    # REFUSED final Source bytes (including late Source-close
                    # successor) are also published while Root originals are
                    # still held. They do not turn a failed run into OBSERVED.
                    if owner.holder is not None:owner.holder.verify_whole()
                    owner.source_publish_attempted=True
                    publication=owner._publish_bytes(launch['source_name'],owner.source_final)
            except BaseException as exc:
                capsule.retain(exc);cleanup.append(exception_detail(exc))
            try: owner.close()
            except BaseException as exc:
                capsule.retain(exc);cleanup.append(exception_detail(exc))
    if owner is None or owner.budget is None:
        return capsule.complete({'status':'STOP_UNCONFIRMED','failure':primary,'publication':None,'GO':False})
    capsule.attach(owner)
    physical=owner.native.snapshot()
    terminal={'schema':'friday.scanner.root-native-terminal.v1',
        'status':'REFUSED' if primary or cleanup or owner.protocol_completion_failure or owner.source_native_metric_failure or not owner.source_final_validated or owner.final_document['status']!='OBSERVED' or owner.budget.close_uncertainties or physical['failed'] or physical['uncertain_fds'] else 'OBSERVED',
        'primary_failure':primary,'cleanup_failures':cleanup,
        'protocol_completion_failure':owner.protocol_completion_failure,
        'close_uncertainties':owner.budget.close_uncertainties[:],
        'origin_failures':owner.budget.origin_failures[:],
        'direct_child':owner.child,'held_pidfd_observed':owner.pidfd is not None,
        'child_reaped':owner.child_reaped,'direct_wait_status':owner.child_status,
        'direct_child_wait4_usage':owner.source_rusage,
        'parent_topology':launch['parent_topology'],
        'final_source_publication':publication,
        'provisional_commit_sha256':hashlib.sha256(owner.holder.source_output).hexdigest() if owner.holder is not None and owner.holder.source_output is not None else None,
        'provisional_commit_is_final_publication':False,
        'source_final_sha256':hashlib.sha256(owner.source_final).hexdigest() if owner.source_final is not None else None,
        'source_final_bytes':len(owner.source_final) if owner.source_final_validated else None,
        'source_wire_bytes':owner.final_size,'source_diagnostic_hex':b''.join(owner.diagnostic_parts).hex(),
        'source_wire_forensic':{'validation_state':owner.source_validation_state,
            'validation_failure':owner.source_validation_failure,
            'stdout_EOF': 'out_read' in owner.pipe_eof,'stderr_EOF':'err_read' in owner.pipe_eof,
            'candidate_sha256':hashlib.sha256(owner.source_candidate).hexdigest() if owner.source_candidate is not None else None,
            'candidate_hex':owner.source_candidate.hex() if owner.source_candidate is not None else None,
            'fragments_hex':[piece.hex() for piece in owner.final_parts],
            'eligible_completed_terminal':owner.source_final_validated},
        'Root_cost_pre_publication_milestone':{'phase':'BEFORE_OWN_TERMINAL_CANONICALIZATION_AND_PUBLICATION',
            'complete_final_end':False,'reserved':dict(owner.budget.used),
            'actual_read_bytes':owner.budget.actual_read_bytes,'actual_output_bytes':owner.budget.actual_output_bytes,
            'native_physical':physical,'terminal_wall_ms_inside_original_total':owner.budget.terminal_wall_ms},
        'Source_max_fds':16,'canonical_workers':4,
        'Source_final_native_metrics':owner.source_native_metrics,
        'Source_native_metric_failure':owner.source_native_metric_failure,
        'GO':False,'Root_Image':'NOT_PROVEN','runtime_acceptance':'NOT_GRANTED'}
    owner.root_terminal=terminal
    if owner.child is not None and not owner.child_reaped:
        return capsule.complete({'status':'STOP_UNCONFIRMED','terminal':terminal,'receipt':None,'GO':False})
    try:
        room=owner.budget.contract['max_output_bytes']-owner.budget.used['output_bytes']
        owner.budget.reserve(work_bytes=room*8+8192,live_bytes=room*4+8192)
        receipt=owner._publish_bytes(launch['terminal_name'],canonical(terminal))
    except BaseException as exc:
        capsule.retain(exc)
        failed={'schema':'friday.scanner.root-completion-handoff.v1','status':'STOP_UNCONFIRMED',
            'publication_failure':owner.budget.retain_origin(exc,'Root.terminal_publication'),
            'terminal':terminal,'receipt':None,'GO':False}
        return capsule.complete(failed)
    handoff={'schema':'friday.scanner.root-completion-handoff.v1','status':terminal['status'],
        'terminal':terminal,'receipt':receipt,
        'Root_cost_after_publication_milestone':{'phase':'AFTER_OWN_PUBLISH_CLOSE_FSYNC_FULL9_HASH_RECEIPT_BEFORE_DELIVERY_AND_FINALIZATION',
            'complete_final_end':False,'reserved':dict(owner.budget.used),
            'actual_read_bytes':owner.budget.actual_read_bytes,'actual_output_bytes':owner.budget.actual_output_bytes},'GO':False}
    return capsule.complete(handoff)


def consume_selected_source_capsule(capsule):
    """Ordinary actual caller transport AFTER construction, before native end.

    The caller now consumes the SAME retained raw owner, rather than obtaining
    a cost-labelled map that no callable reads. The existing parent observer
    consumes the resulting packet and later native tail. Full expiry/adoption
    remains separate: byte delivery cannot transfer direct-parent authority.
    """
    if type(capsule) is not RootRetainedCapsule:raise HolderStop('exact retained capsule ABI absent')
    value=capsule.begin_consume();owner=capsule.owner
    if owner is None or owner.budget is None:
        capsule.consumed(None)
        return {'status':'STOP_UNCONFIRMED','owned_lifetime':capsule,'GO':False}
    try:
        owner.native.begin_terminal();owner.budget.begin_terminal();owner._deadline()
        if value.get('schema')!='friday.scanner.root-completion-handoff.v1':
            # A live direct Source keeps the original owner/capsule; no packet
            # pretending to carry202 descriptions or wait authority is sent.
            capsule.consumed(None)
            return {'status':'STOP_UNCONFIRMED','owned_lifetime':capsule,'GO':False}
        owner.budget.reserve(work_bytes=32768,live_bytes=65536)
        # The terminal, original fragments, actual errors, publication receipt,
        # holder and selector are still reachable before the fallible encoder.
        value['Root_retained_capsule_boundary']={'schema':'friday.scanner.retained-capsule.v1',
            'state':'CALLER_CONSUMING_BEFORE_NATIVE_DESTRUCTION',
            'birth_parent_pid':os.getpid(),'original_end_ns':capsule.original_end_ns,
            'completion_original_end_ns':owner.launch['completion_original_end_ns'],
            'owner_aliases_retained':True,'physical_free_claim':False,
            'complete_whole_caller_final_end':False}
        owner._completion_handoff(value)
        capsule.consumed(value.get('receipt'))
        return {'status':value['status'],'receipt':value.get('receipt'),
            'complete_final_end':False,'owned_lifetime':capsule,'GO':False}
    except BaseException as exc:
        capsule.retain(exc);capsule.consumer_failure=exc
        capsule.state='CONSUMER_FAILED_RETAINED'
        return {'status':'STOP_UNCONFIRMED','owned_lifetime':capsule,'GO':False}


class RootCompletionObserver:
    """Concrete consumer in the independently selected EXISTING Root parent.

    No process, service, model, authority or deadline is created here. The issuer
    supplies already-held pidfd/pipes/channel/publication directory and original
    absolute end. The final result is a caller-owned capsule, not another file
    whose own publication would make its recorded counters circular.

    This observer does NOT pretend to adopt the inner Source child or its202
    originals. The fixed existing-caller destructor retires this session at the
    original end, including a hard-end prefix. An unreaped direct child remains
    an unconfirmed reap on the failed native216 trailer, not a retained caller.
    """
    def __init__(self,permit,selected_permit,selection,resource_contract):
        self.selection=selection
        self.channel=None
        self.selector=None
        self.root_reaped=False
        self.root_wait_status=None
        self.root_rusage=None
        self.handoff=None
        self.handoff_raw=None
        self.stdout_parts=[]
        self.stderr_parts=[]
        self.stdout_size=0
        self.stderr_size=0
        self.eof=set()
        self.origin_failures=[]
        self.owned_origin_exceptions=[]
        self.budget=None
        self.closed=False
        self.capsule=None
        self.receipt_validated=False
        self.acceptance_sent=False
        self.completion_original_end_ns=None
        if permit is None or permit is not selected_permit or os.geteuid()!=0:
            raise HolderStop('actual existing Root completion owner absent')
        permit.authorize_root_completion(selection,resource_contract)
        required={'root_pid','root_pidfd','root_pidfd_identity9','root_native_binding_sha256','root_max_rss_bytes',
            'started_ns','original_end_ns','channel','stdout_fd','stderr_fd',
            'stdout_identity9','stderr_identity9','publication_dir_fd','publication_identity9',
            'terminal_name','root_ceilings','receipt_limit_bytes','stdout_limit_bytes','stderr_limit_bytes','transport_kind','generation'}
        if selection.get('transport_kind')=='root-parent-terminal.v1':
            required=required|{'completion_original_end_ns'}
        if not isinstance(selection,dict) or set(selection)!=required:
            raise HolderStop('exact original existing Root completion selection absent')
        if selection['transport_kind'] not in ('root-parent-terminal.v1','root-observer-capsule.v1'):
            raise HolderStop('actual Root completion consumer kind unselected')
        if selection['original_end_ns']!=selection['started_ns']+resource_contract['max_wall_ms']*1000000:
            raise HolderStop('original Root total end changed')
        if selection['root_ceilings']!=resource_contract:
            raise HolderStop('Root worker/observer original capacity relation not selected')
        if type(selection['root_max_rss_bytes']) is not int or selection['root_max_rss_bytes']<1:
            raise HolderStop('independently selected original Root RSS ceiling absent')
        self.budget=RootBudget(resource_contract,started_ns=selection['started_ns'])
        self.budget.begin_terminal()
        self.native=selected_owner()
        if self.native is None:raise HolderStop('existing observer physical preinitialization owner absent')
        self.native.begin_terminal()
        physical=self.native.snapshot()
        if physical['role']!='actual-root-native-tool' or physical['canonical_workers']!=4:
            raise HolderStop('existing observer genuine native Root role differs')
        for key in ('max_fds','max_read_bytes','max_work_bytes','max_live_bytes','max_output_bytes','max_wall_ms'):
            if physical[key]!=resource_contract[key]:
                raise HolderStop('observer/original Root physical ceilings differ')
        # The existing parent was initialized before this direct worker. Its
        # own original absolute end may therefore be earlier. Do not reject a
        # normal early worker completion merely for that ordering, and never
        # refresh either endpoint: every consumer operation uses their minimum.
        self.observer_original_end_ns=physical['started_ns']+physical['max_wall_ms']*1000000
        self.observer_started_ns=physical['started_ns']
        if selection['transport_kind']=='root-parent-terminal.v1':
            end=selection['completion_original_end_ns']
            if type(end) is not int or end<=physical['started_ns']:
                raise HolderStop('original independently selected final consumer end absent')
            self.completion_original_end_ns=end
        else:self.completion_original_end_ns=self.observer_original_end_ns
        self.budget.original_consumer_end_ns=min(self.observer_original_end_ns,self.completion_original_end_ns)
        self.budget.reserve(fds=physical['fds'],read_bytes=physical['actual_read_bytes'])
        self.budget.actual_read_bytes=physical['actual_read_bytes']
        self.budget.actual_output_bytes=physical['actual_output_bytes']
        # Each description is already issuer-held; no pidfd_open, subprocess,
        # numeric signal, pathname reopen or invented parenthood is introduced.
        for key in ('root_pidfd','stdout','stderr'):
            fd=selection['root_pidfd'] if key=='root_pidfd' else selection[key+'_fd']
            if full9(os.fstat(fd))!=selection[key+'_identity9']:
                raise HolderStop('existing Root completion held '+key+' changed')
        parent=os.fstat(selection['publication_dir_fd'])
        if full9(parent)!=selection['publication_identity9'] or not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=0 or stat.S_IMODE(parent.st_mode)!=0o700:
            raise HolderStop('existing Root completion publication parent changed')
        self.channel=selection['channel']
        if self.channel.family!=socket.AF_UNIX or self.channel.type!=socket.SOCK_SEQPACKET:
            raise HolderStop('existing Root completion packet channel absent')
        self.channel.setsockopt(socket.SOL_SOCKET,socket.SO_PASSCRED,1)
        self.channel.setblocking(False)
        # ECHILD is a real refusal, never an adoption claim. WNOWAIT preserves
        # the exact direct child for the one actual wait4 after all tail EOFs.
        os.waitid(os.P_PIDFD,selection['root_pidfd'],os.WEXITED|os.WNOWAIT|os.WNOHANG)
        for key in ('stdout_fd','stderr_fd'):os.set_blocking(selection[key],False)
        self.budget.reserve(fds=1,work_bytes=8192,live_bytes=16384)
        self.selector=selectors.DefaultSelector()
        self.selector.register(self.channel.fileno(),selectors.EVENT_READ,'receipt')
        self.selector.register(selection['stdout_fd'],selectors.EVENT_READ,'stdout')
        self.selector.register(selection['stderr_fd'],selectors.EVENT_READ,'stderr')
        self.selector.register(selection['root_pidfd'],selectors.EVENT_READ,'pidfd')

    def _remaining(self):
        remaining=self.operation_end_ns()-time.monotonic_ns()
        if remaining<=0:raise HolderStop('STOP_UNCONFIRMED: original Root final-end deadline')
        return remaining/1000000000

    def operation_end_ns(self):
        return min(self.selection['original_end_ns'],self.observer_original_end_ns,
            self.completion_original_end_ns)

    def _receive(self):
        if self.handoff_raw is not None:
            # A second packet is not silently ignored, even after valid receipt.
            raise HolderStop('Root completion receipt duplicate/trailing packet')
        limit=self.selection['receipt_limit_bytes']
        self.budget.reserve(read_bytes=limit,work_bytes=limit*8,live_bytes=limit*18+8192)
        raw,fd,pid,uid,gid=self.native.recv_packet(self.channel.fileno(),limit,0,
            self.operation_end_ns())
        self.budget.actual_read_bytes+=len(raw)
        self.handoff_raw=raw
        if fd!=-1 or (pid,uid,gid)!=(self.selection['root_pid'],0,0):
            raise HolderStop('Root completion actual kernel packet peer differs')
        from source.canonical import canonical_loads
        value=canonical_loads(raw,max_bytes=limit,max_depth=512)
        if self.selection['transport_kind']=='root-observer-capsule.v1':
            required={'schema','status','generation','observed_root_result','origin_failures','retained_custody','prior_completed_Root_cost',
                'observer_started_ns','observer_original_end_ns','completion_original_end_ns','worker_original_end_ns','cost_phase','GO'}
            if (not isinstance(value,dict) or set(value)!=required or
                value['schema']!='friday.scanner.root-observer-capsule.v1' or value['GO'] is not False or
                value['generation']!=self.selection['generation'] or
                value['status'] not in ('OBSERVED','REFUSED','STOP_UNCONFIRMED') or
                value['observer_started_ns']!=self.selection['started_ns'] or
                value['observer_original_end_ns']!=self.selection['original_end_ns'] or
                value['completion_original_end_ns']!=self.observer_original_end_ns or
                value['cost_phase']!='AFTER_CAPSULE_CONSTRUCTION_BEFORE_OWN_TRANSPORT_AND_NATIVE_END' or
                value['retained_custody']!='NATIVE_DESTRUCTION_AND_EXISTING_CALLER_DIRECT_WAIT_REQUIRED'):
                raise HolderStop('actual observer/caller capsule strict ABI differs')
            observed=value['observed_root_result']
            if not isinstance(observed,dict) or observed.get('GO') is not False or observed.get('status')!=value['status']:
                raise HolderStop('observer capsule inner actual result/status differs')
            prior=value['prior_completed_Root_cost']
            if not isinstance(prior,dict) or set(prior)!={'read_bytes','output_bytes','allocation_work_bytes'} or any(
                type(n) is not int or n<0 for n in prior.values()):
                raise HolderStop('observer capsule complete prior native cost shape differs')
            self.handoff=value
            self._accept_observer_handoff()
            self.selector.unregister(self.channel.fileno())
            return
        if value.get('schema')!='friday.scanner.root-completion-handoff.v1' or value.get('GO') is not False:
            raise HolderStop('Root completion exact handoff envelope differs')
        if value.get('status') not in ('OBSERVED','REFUSED','STOP_UNCONFIRMED'):
            raise HolderStop('Root completion handoff status differs')
        normal={'schema','status','terminal','receipt','Root_cost_after_publication_milestone','Root_retained_capsule_boundary','GO'}
        refused={'schema','status','terminal','receipt','publication_failure','Root_retained_capsule_boundary','GO'}
        if set(value) not in (normal,refused):
            raise HolderStop('Root completion exact handoff keys differ')
        terminal=value.get('terminal')
        keys={'schema','status','primary_failure','cleanup_failures','protocol_completion_failure',
            'close_uncertainties','origin_failures','direct_child','held_pidfd_observed','child_reaped',
            'direct_wait_status','direct_child_wait4_usage','parent_topology','final_source_publication','provisional_commit_sha256',
            'provisional_commit_is_final_publication','source_final_sha256','source_final_bytes',
            'source_wire_bytes','source_diagnostic_hex','source_wire_forensic',
            'Root_cost_pre_publication_milestone','Source_max_fds','canonical_workers',
            'Source_final_native_metrics','Source_native_metric_failure','GO','Root_Image','runtime_acceptance'}
        if not isinstance(terminal,dict) or set(terminal)!=keys or terminal.get('schema')!='friday.scanner.root-native-terminal.v1':
            raise HolderStop('Root completion strict actual terminal contract differs')
        if terminal['status'] not in ('OBSERVED','REFUSED') or terminal['Source_max_fds']!=16 or terminal['canonical_workers']!=4 or terminal['GO'] is not False or terminal['Root_Image']!='NOT_PROVEN' or terminal['runtime_acceptance']!='NOT_GRANTED':
            raise HolderStop('Root completion terminal status/original roles differ')
        if terminal['Root_cost_pre_publication_milestone'].get('complete_final_end') is not False:
            raise HolderStop('Root prepublication milestone falsely claims complete end')
        topology=terminal['parent_topology']
        if topology!={'mode':'existing-outside-native-parent-source-birth.v2','birth_parent_pid':self.selection['root_pid'],
            'generation':self.selection['generation'],'original_started_ns':self.selection['started_ns'],
            'original_end_ns':self.selection['original_end_ns']}:
            raise HolderStop('Root terminal actual parent/original generation/end differs')
        boundary=value['Root_retained_capsule_boundary']
        if boundary!={'schema':'friday.scanner.retained-capsule.v1',
            'state':'CALLER_CONSUMING_BEFORE_NATIVE_DESTRUCTION',
            'birth_parent_pid':self.selection['root_pid'],'original_end_ns':self.selection['original_end_ns'],
            'completion_original_end_ns':self.observer_original_end_ns,
            'owner_aliases_retained':True,'physical_free_claim':False,'complete_whole_caller_final_end':False}:
            raise HolderStop('Root retained capsule caller-boundary differs')
        if terminal['final_source_publication'] is not None and terminal['source_wire_forensic'].get('eligible_completed_terminal') is not True:
            raise HolderStop('Root final Source publication lacks validated Source final state')
        if value['receipt'] is not None and value['status']!=terminal['status']:
            raise HolderStop('Root completion receipt/terminal status equality differs')
        self.handoff=value
        native_physical=terminal['Root_cost_pre_publication_milestone'].get('native_physical')
        if (not isinstance(native_physical,dict) or
            native_physical.get('entry_topology')!='existing-outside-native-parent.v2' or
            native_physical.get('parent_custody_claimed') is not True or
            native_physical.get('parent_custody_generation')!=self.selection['generation']):
            raise HolderStop('Root publication did not come from preowned actual outside Source parent')
        if value['receipt'] is not None:
            self._verify_receipt();self.receipt_validated=True
        elif value['status']!='STOP_UNCONFIRMED':
            raise HolderStop('Root completed status lacks exact publication receipt')
        self._accept_parent_handoff()
        self.selector.unregister(self.channel.fileno())
        # EOF of the already-held completion channel is consumed by the final
        # nonblocking recv after direct exit; this first packet alone is not EOF.

    def _accept_parent_handoff(self):
        if self.acceptance_sent:raise HolderStop('Root parent handoff accepted twice')
        self._remaining()
        receipt=self.handoff.get('receipt')
        value={'schema':'friday.scanner.root-parent-accepted.v2','root_pid':self.selection['root_pid'],
            'generation':self.selection['generation'],'started_ns':self.selection['started_ns'],
            'original_end_ns':self.selection['original_end_ns'],
            'consumer_original_end_ns':self.observer_original_end_ns,
            'handoff_sha256':hashlib.sha256(self.handoff_raw).hexdigest(),
            'receipt_sha256':receipt['sha256'] if receipt is not None else None,
            'status':self.handoff['status'],'GO':False}
        self.budget.reserve(work_bytes=32768,live_bytes=81920)
        raw=canonical(value);self.budget.reserve(output_bytes=len(raw))
        if self.native.send_packet(self.channel.fileno(),raw,
                self.operation_end_ns())!=len(raw):
            raise HolderStop('STOP_UNCONFIRMED: actual Root parent acceptance packet incomplete')
        self.budget.actual_output_bytes+=len(raw)
        self.acceptance_sent=True

    def _accept_observer_handoff(self):
        """Own full credential-bound packet BEFORE acknowledging native end."""
        if self.acceptance_sent:raise HolderStop('observer packet acceptance cannot repeat')
        self._remaining()
        payload={'schema':'friday.scanner.root-observer-accepted.v2','root_pid':self.selection['root_pid'],
            'generation':self.selection['generation'],'started_ns':self.selection['started_ns'],
            'original_end_ns':self.selection['original_end_ns'],
            'consumer_original_end_ns':self.observer_original_end_ns,
            'handoff_sha256':hashlib.sha256(self.handoff_raw).hexdigest(),
            'status':self.handoff['status'],'GO':False}
        self.budget.reserve(work_bytes=32768,live_bytes=81920)
        self.capsule.peer_acceptance=payload
        raw=canonical(payload);self.budget.reserve(output_bytes=len(raw))
        if self.native.send_packet(self.channel.fileno(),raw,self.operation_end_ns())!=len(raw):
            raise HolderStop('STOP_UNCONFIRMED: exact observer acceptance packet incomplete')
        self.budget.actual_output_bytes+=len(raw);self.acceptance_sent=True

    def _pipe(self,key):
        label=key.data
        used=self.stdout_size if label=='stdout' else self.stderr_size
        limit=self.selection[label+'_limit_bytes']
        amount=min(65536,limit-used+1)
        if amount<=0:raise HolderStop('Root completion retained prefix cap reached without EOF')
        self.budget.reserve(read_bytes=amount,work_bytes=amount,live_bytes=amount+128)
        raw=os.read(key.fd,amount);self.budget.actual_read_bytes+=len(raw)
        if not raw:
            self.eof.add(label);self.selector.unregister(key.fd)
            self.budget.release('live_bytes',amount+128)
        else:
            # Preserve the actual observed cap+1 forensic prefix before typed
            # projection. A failed prefix is never EOF or a validated trailer.
            if label=='stdout':self.stdout_parts.append(raw);self.stdout_size+=len(raw)
            else:self.stderr_parts.append(raw);self.stderr_size+=len(raw)
            if used+len(raw)>limit:raise HolderStop('Root completion pipe original output cap')

    def _direct_wait(self):
        observed=self.native.direct_root_wait(self.selection['root_pid'],self.selection['root_pidfd'])
        if observed is None:return
        pid,status,usage=observed
        if pid!=self.selection['root_pid']:raise HolderStop('Root completion exact direct wait4 mismatch')
        self.root_reaped=True;self.root_wait_status=status;self.root_rusage=usage
        self.selector.unregister(self.selection['root_pidfd'])

    def cancel_and_reap(self):
        """Retire the actual observed Root in its original native interval.

        This caller cannot signal away an unaccepted inner Source/original202
        generation. Native retirement therefore consumes a real direct wait;
        it refuses if that Root cannot finish within the original end.
        """
        if self.root_reaped:return
        self.native.begin_terminal()
        self.native.retire_direct_children()
        observed=self.native.direct_root_wait(self.selection['root_pid'],self.selection['root_pidfd'])
        if observed is None:raise HolderStop('STOP_UNCONFIRMED: observed Root direct retirement absent')
        pid,status,usage=observed
        if pid!=self.selection['root_pid']:raise HolderStop('observed Root retirement generation differs')
        self.root_reaped=True;self.root_wait_status=status;self.root_rusage=usage

    def complete_error_pipes(self):
        if self.selector is None or not self.root_reaped:return
        while not {'stdout','stderr'}.issubset(self.eof):
            ready=self.selector.select(self._remaining())
            if not ready:raise HolderStop('STOP_UNCONFIRMED: observed Root error EOF end')
            for key,mask in ready:
                if key.data in ('stdout','stderr'):self._pipe(key)
                elif key.data=='pidfd':self.selector.unregister(key.fd)
                elif key.data=='receipt':
                    # Keep the actually received credential-bound raw packet
                    # before any canonical projection/receipt verification.
                    self._receive()

    def _verify_receipt(self):
        receipt=self.handoff.get('receipt')
        if not isinstance(receipt,dict):raise HolderStop('Root completion exact publication receipt absent')
        required={'name','bytes','sha256','identity9','parent_identity9_before','parent_identity9'}
        if set(receipt)!=required or receipt['name']!=self.selection['terminal_name']:
            raise HolderStop('Root completion publication receipt fields/name differ')
        parent=self.selection['publication_dir_fd']
        if full9(os.fstat(parent))!=receipt['parent_identity9']:
            raise HolderStop('Root completion final publication directory changed')
        self.budget.reserve(fds=1,work_bytes=8192,live_bytes=8192)
        fd=os.open(receipt['name'],os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
        try:
            opened=os.fstat(fd);before=full9(opened)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid!=0 or opened.st_nlink!=1 or stat.S_IMODE(opened.st_mode)!=0o600:
                raise HolderStop('Root completion actual private regular terminal differs')
            if before!=receipt['identity9'] or int(before[6])!=receipt['bytes']:
                raise HolderStop('Root completion terminal opened identity/size differs')
            state=hashlib.sha256();offset=0;parts=[]
            while offset<receipt['bytes']:
                self._remaining()
                amount=min(65536,receipt['bytes']-offset)
                self.budget.reserve(read_bytes=amount,work_bytes=amount,live_bytes=amount+128)
                raw=os.pread(fd,amount,offset);self.budget.actual_read_bytes+=len(raw)
                if not raw:raise HolderStop('Root completion terminal short body')
                state.update(raw);parts.append(raw);offset+=len(raw)
            if state.hexdigest()!=receipt['sha256'] or full9(os.fstat(fd))!=before or full9(os.stat(receipt['name'],dir_fd=parent,follow_symlinks=False))!=before:
                raise HolderStop('Root completion full terminal SHA9/named differs')
            self.budget.reserve(work_bytes=receipt['bytes']*8,live_bytes=receipt['bytes']*18+8192)
            from source.canonical import canonical_loads
            raw=b''.join(parts)
            value=canonical_loads(raw,max_bytes=self.selection['receipt_limit_bytes'],max_depth=512)
            if value!=self.handoff['terminal']:
                raise HolderStop('Root completion actual published terminal/handoff equality differs')
        finally:self.budget.close_fd(fd,'Root.completed_terminal.read')
        if self.budget.close_uncertainties:raise HolderStop('Root completion reader close unconfirmed')

    def finish(self):
        """Finite same-end consumer. Failure retains exact actual performed prefix."""
        hard_end_trailer=None
        try:
            while not self.root_reaped or self.eof!={'stdout','stderr'}:
                ready=self.selector.select(self._remaining())
                if not ready:raise HolderStop('STOP_UNCONFIRMED: Root final-end wait/EOF deadline')
                for key,mask in ready:
                    if key.data=='receipt':self._receive()
                    elif key.data=='pidfd':self._direct_wait()
                    else:self._pipe(key)
            if self.handoff is None:
                raw_tail=b''.join(self.stderr_parts)
                if len(raw_tail)>=216:
                    tail=raw_tail[-216:]
                    values=struct.unpack('<23Q32s',tail)
                    if values[0]==0x4652494441594d31 and values[1]==2 and values[2]==2:
                        hard_end_trailer=tail
                raise HolderStop('Root final receipt never consumed')
            # The root sender has actually exited: no producer remains. No
            # duplicate packet or partial completion channel is accepted.
            self.budget.reserve(read_bytes=1,work_bytes=512,live_bytes=512)
            trailing=self.channel.recv(1);self.budget.actual_read_bytes+=len(trailing)
            if trailing:raise HolderStop('Root final receipt has trailing packet')
            self.eof.add('receipt')
            raw=b''.join(self.stderr_parts)
            self.budget.reserve(work_bytes=len(raw)*4+8192,live_bytes=len(raw)+8192)
            if len(raw)<216:raise HolderStop('actual Root after-finalization trailer absent')
            values=struct.unpack('<23Q32s',raw[-216:])
            if values[:4]!=(0x4652494441594d31,2,2,self.selection['root_pid']) or values[23].hex()!=self.selection['root_native_binding_sha256']:
                raise HolderStop('actual Root final native image/role trailer differs')
            if values[12]!=self.selection['started_ns'] or values[22]!=1:
                raise HolderStop('actual original Root start/final logical ledger absent')
            native_ok=os.WIFEXITED(self.root_wait_status) and os.WEXITSTATUS(self.root_wait_status)==0 and values[14]==0 and not values[11] and not values[10]
            receipt_validated=False
            if self.selection['transport_kind']=='root-observer-capsule.v1':
                # The actual preceding observer's capsule construction/errors,
                # aliases and ordinary caller transport precede this native
                # tail. This receiver consumes that exact parent/direct-wait
                # domain, not a self-sampled preconstruction cost milestone.
                if not self.acceptance_sent:raise HolderStop('observer native-end lacks actual accepted delivery')
                receipt_validated=True
            elif self.handoff.get('receipt') is not None:
                if not self.receipt_validated or not self.acceptance_sent:
                    raise HolderStop('actual pre-finalization parent acceptance absent')
                receipt_validated=True
            elif self.handoff.get('status')!='STOP_UNCONFIRMED':
                raise HolderStop('Root completed-status handoff lacks publication receipt')
            logical=dict(zip(('fds','read_bytes','work_bytes','live_bytes','output_bytes'),values[15:20]))
            actual={'read_bytes':values[4],'output_bytes':values[5]+216,
                'allocation_work_bytes':values[6],'physical_live_bytes':values[7],
                'physical_peak_upper_bytes':values[8],'active_fds':values[9],
                'uncertain_fds':values[10],'native_trailer_delivered_bytes':216,
                'RSS_bytes':self.root_rusage['RSS_bytes'],
                'RSS_source':'ACTUAL_wait4.ru_maxrss_LINUX_KiB_x1024',
                'kernel_input_blocks_wait4':self.root_rusage['kernel_input_blocks_wait4'],
                'kernel_output_blocks_wait4':self.root_rusage['kernel_output_blocks_wait4'],
                'implicit_IO_bytes':'UNKNOWN','aggregate_RAM_bytes':'UNKNOWN',
                'peak_kind':'SUM_OF_ARENA_PEAKS_CONSERVATIVE_UPPER_NOT_RSS',
                'phase':'AFTER_OWN_PUBLICATION_RECEIPT_DELIVERY_FINALIZATION_PIPE_EOF_AND_NATIVE_EXIT'}
            ceilings=self.selection['root_ceilings']
            prior=self.handoff['prior_completed_Root_cost'] if self.selection['transport_kind']=='root-observer-capsule.v1' else {
                'read_bytes':0,'output_bytes':0,'allocation_work_bytes':0}
            completed_cost={'read_bytes':prior['read_bytes']+actual['read_bytes'],
                'output_bytes':prior['output_bytes']+actual['output_bytes'],
                'allocation_work_bytes':prior['allocation_work_bytes']+actual['allocation_work_bytes']}
            if completed_cost['read_bytes']+self.budget.actual_read_bytes>ceilings['max_read_bytes'] or completed_cost['output_bytes']+self.budget.actual_output_bytes>ceilings['max_output_bytes']:
                raise HolderStop('Root worker plus observer actual IO does not fit original aggregate end domain')
            if completed_cost['allocation_work_bytes']>ceilings['max_work_bytes'] or actual['RSS_bytes']>self.selection['root_max_rss_bytes']:
                raise HolderStop('Root actual native work/RSS does not fit original selected domain')
            self._remaining()
            self.budget.reserve(work_bytes=len(self.handoff_raw)+8192,live_bytes=8192)
            handoff_digest=hashlib.sha256(self.handoff_raw).hexdigest()
            status=self.handoff['status'] if native_ok and receipt_validated else 'STOP_UNCONFIRMED'
            self.budget.reserve(work_bytes=32768,live_bytes=65536)
            value={'status':status,'complete_Root_worker_final_end':True,
                'completed_domain':self.selection['transport_kind'],
                'completed_Root_cost':completed_cost,
                'Root_worker_actual_final_end':actual,'Root_logical_after_receipt_delivery':logical,
                'Root_logical_actual_read_bytes':values[20],'Root_logical_actual_output_bytes':values[21],
                'actual_receipt':self.handoff.get('receipt'),'receipt_validated':receipt_validated,
                'native_completion_ok':native_ok,'actual_receipt_raw_sha256':handoff_digest,
                'direct_wait_status':self.root_wait_status,'EOF':sorted(self.eof),
                'observer_reserved':dict(self.budget.used),'observer_actual_read_bytes':self.budget.actual_read_bytes,
                'observer_actual_output_bytes':self.budget.actual_output_bytes,
                'observer_cost_phase':'BEFORE_CALLER_OWNED_CAPSULE_CONSTRUCTION_NOT_WHOLE_ROOT_FINAL_END',
                'observer_own_final_end':'RETAINED_CALLER_NATIVE_OWNER; NO_ADDITIONAL_PUBLICATION_OR_COMPLETENESS_CLAIM',
                'original_worker_end_ns':self.selection['original_end_ns'],
                'original_observer_end_ns':self.observer_original_end_ns,
                'aggregate_RAM':'UNKNOWN','implicit_IO':'UNKNOWN','GO':False}
            return self.capsule.complete(value)
        except BaseException as exc:
            self.capsule.retain(exc)
            if self.budget is not None:self.budget.retain_origin(exc,'RootCompletionObserver.finish')
            if self.budget is not None:self.budget.reserve(work_bytes=32768,live_bytes=65536)
            return self.capsule.complete({'status':'STOP_UNCONFIRMED','complete_Root_worker_final_end':False,
                'origin_failures':self.budget.origin_failures if self.budget is not None else [],
                'root_reaped':self.root_reaped,'direct_wait_status':self.root_wait_status,
                'EOF':sorted(self.eof),'receipt_raw_hex':self.handoff_raw.hex() if self.handoff_raw is not None else None,
                'stdout_fragments_hex':[part.hex() for part in self.stdout_parts],
                'stderr_fragments_hex':[part.hex() for part in self.stderr_parts],
                'hard_end_trailer_hex':hard_end_trailer.hex() if hard_end_trailer is not None else None,
                'observer_reserved':dict(self.budget.used) if self.budget is not None else None,
                'R1_inner_Source_custody':'PREBIRTH_EXISTING_PARENT_REQUIRED; NO_ADOPTION_BY_THIS_RECEIVER',
                'parent_acceptance_sent':self.acceptance_sent,'GO':False})


def observe_selected_root_completion(permit,selected_permit,selection,resource_contract,capsule=None):
    """The actual existing parent calls this entry, never a Source permit issuer."""
    if type(capsule) is not RootRetainedCapsule:
        raise HolderStop('actual preowned observer caller capsule absent')
    owner=RootCompletionObserver.__new__(RootCompletionObserver)
    capsule.attach(owner)
    try:
        owner.__init__(permit,selected_permit,selection,resource_contract)
        capsule.attach(owner);owner.capsule=capsule
        return owner.finish()
    except BaseException as exc:
        capsule.retain(exc)
        capsule.construction_failure=exc
        from source.causes import exception_detail
        if capsule.state!='CONSTRUCTING':
            capsule.state='CONSTRUCTION_FAILED_RETAINED'
            return capsule
        return capsule.complete({'status':'STOP_UNCONFIRMED','failure':exception_detail(exc),
            'complete_Root_worker_final_end':False,'GO':False})


def consume_selected_observer_capsule(capsule):
    """Concrete ordinary caller transport, consumed by the EXISTING next parent.

    The separately selected next caller uses complete_selected_root with
    transport_kind=root-observer-capsule.v1, real already-held pidfd/pipes and
    actual direct-parent wait. It then sees this observer's after-finalization
    native216 trailer, all EOFs, actual wait4/RSS and all transport allocation.
    No process is created and no new resource or time allowance is granted.
    The final external caller's own retained view is a bounded contract boundary,
    explicitly not a complete self-observation or release/runtime acceptance.
    """
    if type(capsule) is not RootRetainedCapsule:raise HolderStop('observer retained capsule ABI absent')
    owner=capsule.owner
    if capsule.state!='READY' or owner is None or owner.budget is None:
        return {'status':'STOP_UNCONFIRMED','owned_lifetime':capsule,'GO':False}
    value=capsule.begin_consume()
    try:
        owner._remaining()
        owner.budget.reserve(work_bytes=32768,live_bytes=65536)
        # Owned selector retires once; issuer-held endpoint/pipes/directory and
        # all raw/error aliases remain caller-owned through real destruction.
        selector=owner.selector;owner.selector=None
        if selector is not None:
            try:selector.close()
            except BaseException as exc:
                capsule.retain(exc);owner.budget.retain_origin(exc,'RootObserverCapsule.selector.close')
                owner.budget.close_uncertainties.append({'operation':'RootObserverCapsule.selector.close','charged':True})
                value['status']='STOP_UNCONFIRMED'
            else:owner.budget.release('fds',1)
        prior=value.get('completed_Root_cost')
        if not value.get('complete_Root_worker_final_end'):
            # Missing prior facts are UNKNOWN, never zero used to pass a later
            # aggregate bound. A partial observer capsule can be retained but
            # cannot produce a complete-domain transport receipt.
            raise HolderStop('STOP_UNCONFIRMED: completed prior native cost is unavailable')
        if not isinstance(prior,dict) or set(prior)!={'read_bytes','output_bytes','allocation_work_bytes'} or any(
                type(n) is not int or n<0 for n in prior.values()):
            raise HolderStop('STOP_UNCONFIRMED: original prior native cost is missing or malformed')
        payload={'schema':'friday.scanner.root-observer-capsule.v1','status':value['status'],
            'generation':owner.selection['generation'],
            'prior_completed_Root_cost':prior,
            'observed_root_result':value,'origin_failures':owner.budget.origin_failures,
            'retained_custody':'NATIVE_DESTRUCTION_AND_EXISTING_CALLER_DIRECT_WAIT_REQUIRED',
            'observer_started_ns':owner.observer_started_ns,
            'observer_original_end_ns':owner.observer_original_end_ns,
            'completion_original_end_ns':owner.completion_original_end_ns,
            'worker_original_end_ns':owner.selection['original_end_ns'],
            'cost_phase':'AFTER_CAPSULE_CONSTRUCTION_BEFORE_OWN_TRANSPORT_AND_NATIVE_END','GO':False}
        # Preserve the complete raw payload before encoding can fail. Exactly
        # one bounded packet is produced; no terminal/publication retry occurs.
        capsule.payload=payload
        room=owner.budget.contract['max_output_bytes']-owner.budget.used['output_bytes']
        owner.budget.reserve(work_bytes=room*8+8192,live_bytes=room*4+8192)
        raw=canonical(payload)
        owner.budget.reserve(output_bytes=len(raw))
        owner._remaining()
        if owner.native.send_root_completion(raw,owner.operation_end_ns())!=len(raw):
            raise HolderStop('STOP_UNCONFIRMED: observer caller transport short packet')
        owner.budget.actual_output_bytes+=len(raw)
        owner.budget.reserve(read_bytes=4096,work_bytes=32768,live_bytes=81920)
        ack,fd,pid,uid,gid=owner.native.recv_packet(3,4096,0,owner.operation_end_ns())
        capsule.peer_acceptance=ack
        owner.budget.actual_read_bytes+=len(ack)
        from source.canonical import canonical_loads
        accepted=canonical_loads(ack,max_bytes=4096,max_depth=64)
        expected={'schema':'friday.scanner.root-observer-accepted.v2','root_pid':os.getpid(),
            'generation':owner.selection['generation'],'started_ns':owner.observer_started_ns,
            'original_end_ns':owner.observer_original_end_ns,
            'consumer_original_end_ns':owner.completion_original_end_ns,
            'handoff_sha256':hashlib.sha256(raw).hexdigest(),'status':value['status'],'GO':False}
        if fd!=-1 or (pid,uid,gid)!=(owner.native.snapshot()['root_completion_peer'],0,0) or accepted!=expected:
            raise HolderStop('STOP_UNCONFIRMED: actual observer peer/generation/end/hash acceptance differs')
        owner.native.accept_existing_parent_delivery(capsule.existing_caller,owner.selection['generation'])
        owner.native.capture_root_ledger(tuple(owner.budget.used[key] for key in
            ('fds','read_bytes','work_bytes','live_bytes','output_bytes')),
            owner.budget.actual_read_bytes,owner.budget.actual_output_bytes)
        capsule.consumed(None)
        return {'status':value['status'],'whole_external_caller_final_end':False,
            'native_final_consumer_kind':'root-observer-capsule.v1','owned_lifetime':capsule,'GO':False}
    except BaseException as exc:
        capsule.retain(exc);capsule.consumer_failure=exc;capsule.state='CONSUMER_FAILED_RETAINED'
        return {'status':'STOP_UNCONFIRMED','owned_lifetime':capsule,'GO':False}


def retain_selected_final_caller_capsule(capsule):
    """Consume into ONE preowned original C final caller; do not add an observer.

    This is a finite two-hop protocol, not recursive observer construction.
    The actual preceding observer native end and its ordinary transport were
    consumed by finish(). complete_selected_root then calls caller.accept; the
    fixed C entry actually calls native_caller_destruct/Py_FinalizeEx, retires
    endpoint aliases once and exits after the actual native tail. The returned
    Python view is still BEFORE that end and never claims its own future costs.
    """
    if type(capsule) is not RootRetainedCapsule:raise HolderStop('final caller retained ABI absent')
    if capsule.state!='READY' or capsule.owner is None or capsule.owner.budget is None:
        return {'status':'STOP_UNCONFIRMED','owned_lifetime':capsule,'GO':False}
    value=capsule.begin_consume();owner=capsule.owner
    try:
        owner._remaining();owner.budget.reserve(work_bytes=32768,live_bytes=65536)
        selector=owner.selector;owner.selector=None
        if selector is not None:
            try:selector.close()
            except BaseException as exc:
                capsule.retain(exc);owner.budget.retain_origin(exc,'FinalCallerCapsule.selector.close')
                owner.budget.close_uncertainties.append({'operation':'FinalCallerCapsule.selector.close','charged':True})
                value['status']='STOP_UNCONFIRMED'
            else:owner.budget.release('fds',1)
        view={'status':value['status'],'actual_consumed_observer_result':value,
            'Source_boundary':'FINITE_CONSUMED_RETAINED_EXTERNAL_CALLER_CAPSULE.v1',
            'complete_whole_external_caller_final_end':False,'external_native_end':'CALLED_FIXED_C_FINAL_CONSUMER_SOURCE_ONLY_NOT_RUN',
            'actual_native_owner':capsule.native,'owned_lifetime':capsule,'GO':False,
            'selected_caller_relation':'RETURNED_SAME_TGID_SELECTED_CALLER_PEER_ADOPTION_NOT_CLAIMED'}
        capsule.consumed(value.get('actual_receipt'))
        return view
    except BaseException as exc:
        capsule.retain(exc);capsule.consumer_failure=exc;capsule.state='CONSUMER_FAILED_RETAINED'
        return {'status':'STOP_UNCONFIRMED','owned_lifetime':capsule,'GO':False}
