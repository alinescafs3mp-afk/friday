"""Future actual Root-native holder. SOURCE ONLY: not launched by this package.

The actual Root tool independently reviews/selects this helper and supplies its
opaque native permit, already selected scope and original role ceilings. Source
has no launcher edge, no permit constructor and no RPC release operation.
Root owns all original archive generations until its own publication/terminal.
One holder process plus one Source process uses two of the existing four slots.
An original GLOBAL max16 admission cannot admit this topology: require_domain
refuses it. A Source-role max16 is preserved, never relabeled as a global cap.
"""
import array
import hashlib
import json
import os
import socket
import stat
import struct
import time

MAX_MESSAGE=1048576
CHUNK=65536
IDENTITY=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')


class HolderStop(Exception):
    pass


def full9(st):
    return [str(getattr(st,key)) for key in IDENTITY]


class RootBudget:
    """Original independently admitted Root domain; no default/granted limits.

    A selected native allocation audit is still required. These envelopes and
    exact read/transport counters are not host RSS/implicit-IO measurements.
    """
    def __init__(self,contract,terminal_prepaid=None,started_ns=None,terminal_wall_ms=0):
        required={'owner','scope','max_fds','max_read_bytes','max_work_bytes',
                  'max_live_bytes','max_output_bytes','max_wall_ms','canonical_workers'}
        if not isinstance(contract,dict) or set(contract)!=required:
            raise HolderStop('Root role resource contract absent')
        if contract['owner']!='actual-root-native-tool' or contract['scope']!='distinct-root-holder-role':
            raise HolderStop('GLOBAL16_OR_UNSELECTED_ROOT_DOMAIN')
        if contract['canonical_workers']!=4 or any(type(contract[key]) is not int or contract[key]<1
                for key in required-{'owner','scope','canonical_workers'}):
            raise HolderStop('invalid original Root ceilings')
        self.contract=contract
        self.used={'fds':0,'read_bytes':0,'work_bytes':0,'live_bytes':0,'output_bytes':0}
        self.actual_read_bytes=0
        self.actual_output_bytes=0
        self.started=time.monotonic_ns() if started_ns is None else started_ns
        if type(terminal_wall_ms) is not int or not 0<=terminal_wall_ms<contract['max_wall_ms']:
            raise HolderStop('original Root terminal wall partition cannot fit')
        self.terminal_wall_ms=terminal_wall_ms
        self.close_uncertainties=[]
        self.origin_failures=[]
        self.owned_origin_exceptions=[]
        self.terminal_prepaid=terminal_prepaid or {}
        self.terminal=False
        self.original_consumer_end_ns=None
        if any(key not in self.used or type(value) is not int or value<0 or value>=contract['max_'+key]
               for key,value in self.terminal_prepaid.items()):
            raise HolderStop('original Root terminal escrow cannot fit')

    def begin_terminal(self):
        self.terminal=True

    def operation_end_ns(self):
        end=self.started+self.contract['max_wall_ms']*1000000
        if self.original_consumer_end_ns is not None:end=min(end,self.original_consumer_end_ns)
        return end-(0 if self.terminal else self.terminal_wall_ms*1000000)

    def reserve(self,**delta):
        if self.close_uncertainties and not self.terminal:
            raise HolderStop('Root close uncertainty retains original quota')
        if time.monotonic_ns()>self.operation_end_ns():
            raise HolderStop('Root holder wall ceiling')
        for key,value in delta.items():
            if key not in self.used or type(value) is not int or value<0 or self.used[key]+value>self.contract['max_'+key]-(0 if self.terminal else self.terminal_prepaid.get(key,0)):
                raise HolderStop('Root holder '+key+' ceiling')
        for key,value in delta.items(): self.used[key]+=value

    def release(self,key,value):
        if key not in ('fds','live_bytes') or type(value) is not int or value<0 or value>self.used[key]:
            raise HolderStop('Root holder ownership imbalance')
        self.used[key]-=value

    def close_fd(self,fd,path):
        # The caller detached this slot before the ONLY close attempt.
        try: os.close(fd)
        except OSError as exc:
            self.retain_origin(exc,'Root.close_fd',path)
            self.close_uncertainties.append({'fd':fd,'path':path,'errno':exc.errno,
                'exception_class':type(exc).__name__,'exception_message':str(exc),
                'filename':{'type':'bytes','hex':exc.filename.hex()} if isinstance(exc.filename,bytes) else exc.filename,
                'filename2':{'type':'bytes','hex':exc.filename2.hex()} if isinstance(exc.filename2,bytes) else exc.filename2,
                'closed_confirmed':False,'charged':True,'attempted_once':True})
        else: self.release('fds',1)

    def retain_origin(self,exc,operation,path=None):
        """Keep full original host/recorder chains before cause projection."""
        try:
            self.owned_origin_exceptions.append(exc)
            from source.causes import exception_detail
            record=exception_detail(exc)
            record['origin_operation']=operation
            record['origin_path']={'type':'bytes','hex':path.hex()} if isinstance(path,bytes) else path
            self.origin_failures.append(record)
            return record
        except BaseException as recorder_failure:
            raise recorder_failure from exc


class RootHolder:
    """A real independently owned process, not a Source-issued generation.

    `native_permit` comes only from the actual Root tool and is checked against
    the independently selected permit object. It is never constructed from
    admission JSON, SHA equality, a boolean or a Source function.
    """
    def __init__(self,channel,selection,native_permit,selected_permit,resource_contract,
                 budget=None,channel_prepaid=False):
        # Uniform owner fields precede validation/init/acquisition failures.
        self.channel=channel;self.root_fd=None;self.files={};self.closed=False
        self.prepared=None;self.committed=None;self.source_output=None
        self.budget=budget
        from tools.native_support import selected_owner
        actual=selected_owner()
        physical=actual.snapshot() if actual is not None else None
        if (physical is None or physical['role']!='actual-root-native-tool' or os.geteuid()!=0 or
            physical.get('entry_topology')!='existing-outside-native-parent.v2' or
            physical.get('parent_custody_claimed') is not True or physical.get('existing_observer_entry') is not False or
            physical.get('parent_custody_generation')!=selection.get('generation')):
            raise HolderStop('actual independently admitted Root native role absent')
        if native_permit is not selected_permit or native_permit is None:
            raise HolderStop('actual Root-native permit object absent')
        # Actual tool authorization is performed before archive/native effects.
        native_permit.authorize_holder(selection,resource_contract)
        if channel is not None and (channel.family!=socket.AF_UNIX or channel.type!=socket.SOCK_SEQPACKET):
            raise HolderStop('Root requires accepted private AF_UNIX SEQPACKET channel')
        if self.budget is None: self.budget=RootBudget(resource_contract)
        if selection.get('source_max_fds')!=16 or selection.get('canonical_workers')!=4:
            raise HolderStop('Source16/canonical4 contract changed')
        rows=selection.get('rows')
        if not isinstance(rows,list) or len(rows)!=202:
            raise HolderStop('original whole202 selection absent')
        # All root originals, channel, traversed directories and temporary path
        # checks belong to ROOT. Nothing is charged to a fictitious Source role.
        if len(rows)+5>resource_contract['max_fds']:
            raise HolderStop('original Root FD ceiling cannot hold whole202')
        self.budget.reserve(fds=0 if channel is None or channel_prepaid else 1,work_bytes=len(rows)*32768,
                            live_bytes=len(rows)*65536+262144)
        self.channel=channel
        self.selection=selection
        self.root_fd=None
        self.files={}
        self.root_pid=os.getpid()
        self.worker_peer=None
        self.prepared=None
        self.committed=None
        self.source_output=None
        self.closed=False
        scope=selection.get('held_scope')
        if not isinstance(scope,dict): raise HolderStop('Root selected directory/mount scope absent')
        self.directories={row['path']:row for row in [scope['root']]+scope['directories']}
        required={''}
        for row in rows:
            parts=row['relative_path'].split('/')
            required.update('/'.join(parts[:index]) for index in range(1,len(parts)))
        if set(self.directories)!=required:
            raise HolderStop('Root selected directory/mount pathset mismatch')
        self._acquire()

    def bind_source_channel(self,channel,source_peer):
        """Bind only AFTER original202 acquisition, in their actual birth parent.

        Acquisition can now fail without producing an inner child. The actual
        holder is already installed in the outer owner before its constructor;
        this method never reconstructs a holder from a custody label or pidfd.
        """
        if self.closed or self.channel is not None or len(self.files)!=202:
            raise HolderStop('pre-birth original holder is not available')
        if channel.family!=socket.AF_UNIX or channel.type!=socket.SOCK_SEQPACKET:
            raise HolderStop('actual Source control description absent')
        if self.root_pid!=os.getpid() or self.selection.get('birth_parent_pid')!=self.root_pid:
            raise HolderStop('actual original202 owner is not selected Source birth parent')
        if not isinstance(source_peer,tuple) or len(source_peer)!=3:
            raise HolderStop('actual Source birth credentials absent')
        self.selection['source_peer']=list(source_peer)
        self.channel=channel

    def _selected_directory(self,fd,path):
        selected=self.directories[path]
        fields=('device','inode','full_mode','uid','gid','nlink','size','mtime_ns','ctime_ns')
        expected=[str(selected['identity'][key]) for key in fields]
        if full9(os.fstat(fd))!=expected:
            raise HolderStop('Root directory selected full9 mismatch')
        root=self.directories['']
        if (selected['identity']['device']!=root['identity']['device'] or
            selected['mount_id']!=root['mount_id'] or selected['mount_domain']!=root['mount_domain']):
            raise HolderStop('Root selected mount domain mismatch')
        self.budget.reserve(read_bytes=4096,work_bytes=8192,live_bytes=16384)
        info=None; raw=None
        try:
            info=self._open('/proc/self/fdinfo/'+str(fd),os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
            raw=os.read(info,4096)
            self.budget.actual_read_bytes+=len(raw)
            if len(raw)==4096: raise HolderStop('Root mount metadata bound')
            values=[line.split(b':',1)[1].strip() for line in raw.splitlines() if line.startswith(b'mnt_id:')]
            if len(values)!=1 or int(values[0])!=selected['mount_id']:
                raise HolderStop('Root actual mount domain mismatch')
        finally:
            raw=None
            if info is not None: self.budget.close_fd(info,path)
            self.budget.release('live_bytes',16384)

    def _open(self,path,flags,parent=None):
        self.budget.reserve(fds=1,work_bytes=4096,live_bytes=4096)
        try: return os.open(path,flags,dir_fd=parent)
        except BaseException:
            self.budget.release('fds',1)
            raise

    def _directory(self,relative):
        if relative.startswith('/') or any(part in ('','.','..') for part in relative.split('/')):
            raise HolderStop('selected path invalid')
        parent=self.root_fd
        current=None
        try:
            parts=relative.split('/')
            for index,part in enumerate(parts[:-1]):
                next_fd=self._open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,parent)
                try: self._selected_directory(next_fd,'/'.join(parts[:index+1]))
                except BaseException:
                    self.budget.close_fd(next_fd,relative)
                    raise
                old=current
                current=next_fd
                parent=current
                if old is not None: self.budget.close_fd(old,relative)
            return current,relative.rsplit('/',1)[-1]
        except BaseException:
            if current is not None: self.budget.close_fd(current,relative)
            raise

    def _hash(self,fd,size):
        self.budget.reserve(work_bytes=8192,live_bytes=8192)
        state=None
        try:
            state=hashlib.sha256()
            offset=0
            while offset<size:
                amount=min(CHUNK,size-offset)
                self.budget.reserve(read_bytes=amount,work_bytes=amount,live_bytes=amount+64)
                piece=None
                try:
                    piece=os.pread(fd,amount,offset)
                    self.budget.actual_read_bytes+=len(piece)
                    if not piece: raise HolderStop('Root short original read')
                    state.update(piece); offset+=len(piece)
                finally:
                    piece=None
                    self.budget.release('live_bytes',amount+64)
            self.budget.reserve(work_bytes=512,live_bytes=512)
            return state.hexdigest()
        finally:
            state=None
            self.budget.release('live_bytes',8192)

    def _acquire(self):
        try:
            self.root_fd=self._open(self.selection['held_root'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            if full9(os.fstat(self.root_fd))!=self.selection['root_identity9']:
                raise HolderStop('Root selected directory identity changed')
            self._selected_directory(self.root_fd,'')
            for row in self.selection['rows']:
                path=row['relative_path']
                if path in self.files: raise HolderStop('Root whole membership duplicate')
                # Register an owned slot BEFORE open. Assignment into its fixed
                # fields cannot strand a new descriptor behind a dict resize.
                slot={'fd':None,'identity9':None,'sha256':None,'row':row}
                self.files[path]=slot
                directory=None
                try:
                    directory,name=self._directory(path)
                    slot['fd']=self._open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,
                        self.root_fd if directory is None else directory)
                    before=os.fstat(slot['fd'])
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size!=row['size']:
                        raise HolderStop('Root original regular/size/nlink mismatch')
                    slot['identity9']=full9(before)
                    if slot['identity9']!=row['identity9']:
                        raise HolderStop('Root original selected full9 mismatch')
                    slot['sha256']=self._hash(slot['fd'],row['size'])
                    if slot['sha256']!=row['sha256'] or full9(os.fstat(slot['fd']))!=slot['identity9']:
                        raise HolderStop('Root original SHA/full9 mismatch')
                finally:
                    if directory is not None: self.budget.close_fd(directory,path)
            if self.budget.close_uncertainties: raise HolderStop('Root close uncertainty')
        except BaseException:
            self.close()
            raise

    def verify_whole(self):
        if self.closed or len(self.files)!=202: raise HolderStop('Root generation retired')
        if full9(os.fstat(self.root_fd))!=self.selection['root_identity9']:
            raise HolderStop('Root directory changed')
        self._selected_directory(self.root_fd,'')
        records=[]
        for path,slot in self.files.items():
            fd=slot['fd']; directory=None
            if fd is None or full9(os.fstat(fd))!=slot['identity9']:
                raise HolderStop('Root original generation changed')
            digest=self._hash(fd,slot['row']['size'])
            try:
                directory,name=self._directory(path)
                named=os.stat(name,dir_fd=self.root_fd if directory is None else directory,follow_symlinks=False)
                if digest!=slot['sha256'] or full9(named)!=slot['identity9'] or full9(os.fstat(fd))!=slot['identity9']:
                    raise HolderStop('Root original full causal terminal custody changed')
            finally:
                if directory is not None: self.budget.close_fd(directory,path)
            records.append({'relative_path':path,'identity9':slot['identity9'],'sha256':digest})
        if self.budget.close_uncertainties: raise HolderStop('Root terminal close uncertainty')
        return records

    def _send(self,message,fd=None):
        limit=262144 if message.get('operation') in ('PREPARE','COMMIT') else 16384
        self.budget.reserve(work_bytes=limit*8,live_bytes=limit*4,output_bytes=limit)
        raw=None
        try:
            raw=(json.dumps(message,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
            if len(raw)>limit: raise HolderStop('Root message bound')
            ancillary=[] if fd is None else [(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))]
            self.channel.settimeout(self._remaining())
            if self.channel.sendmsg([raw],ancillary)!=len(raw): raise HolderStop('Root short packet')
            self.budget.actual_output_bytes+=len(raw)
        finally:
            raw=None
            self.budget.release('live_bytes',limit*4)

    def _remaining(self):
        remaining=self.budget.operation_end_ns()-time.monotonic_ns()
        if remaining<=0: raise TimeoutError('Root blocking protocol deadline')
        return remaining/1000000000

    def _request(self,limit):
        from tools.native_support import selected_owner
        native=selected_owner()
        if native is None: raise HolderStop('Root native receiver owner absent')
        pump=getattr(self,'io_owner',None)
        if pump is not None: pump.wait_protocol_readable()
        raw,fd,pid,uid,gid=native.recv_packet(self.channel.fileno(),limit,0,self.budget.operation_end_ns())
        if fd!=-1 or (pid,uid,gid)!=tuple(self.selection['source_peer']):
            raise HolderStop('actual Source kernel packet peer mismatch')
        self.budget.actual_read_bytes+=len(raw)
        from source.canonical import canonical_loads
        request=canonical_loads(raw,max_bytes=limit,max_depth=64)
        return raw,request

    def serve(self):
        """Finite BORROW/PREPARE/COMMIT; Source cannot close Root generations."""
        self.budget.reserve(read_bytes=16384,work_bytes=131072,live_bytes=262144)
        raw,ready=self._request(16384)
        if ready!={'operation':'READY','generation':self.selection['generation']}:
            raise HolderStop('actual Source READY generation mismatch')
        raw=ready=None;self.budget.release('live_bytes',262144)
        self.worker_peer=tuple(self.selection['source_peer'])
        self._send({'operation':'HELLO','root_pid':self.root_pid,'owner':'actual-root-native-tool',
            'source_max_fds':16,'canonical_workers':4,'scope':'whole-root-holder-generation.v1',
            'generation':self.selection['generation'],'archive_count':202})
        borrowed=set()
        for ordinal in range(205):
            self.budget.reserve(read_bytes=16384,work_bytes=131072,live_bytes=262144)
            raw=None
            try:
                raw,request=self._request(16384)
                operation=request.get('operation')
                if operation=='BORROW':
                    if set(request)!={'operation','relative_path'} or self.prepared is not None:
                        raise HolderStop('Root borrow phase mismatch')
                    path=request['relative_path']; slot=self.files.get(path)
                    if slot is None or path in borrowed: raise HolderStop('Root borrow membership mismatch')
                    borrowed.add(path)
                    self._send({'operation':'BORROW','generation':self.selection['generation'],
                        'relative_path':path,'identity9':slot['identity9'],'sha256':slot['sha256']},slot['fd'])
                elif operation=='PREPARE':
                    if set(request)!={'operation','payload_sha256'} or borrowed!=set(self.files) or self.prepared is not None:
                        raise HolderStop('Root whole prepare membership/phase mismatch')
                    records=self.verify_whole()
                    self.prepared={'operation':'PREPARE','generation':self.selection['generation'],
                        'payload_sha256':request['payload_sha256'],'root_pid':self.root_pid,
                        'archive_count':202,'archives':records,'scope':'whole-root-holder-generation.v1'}
                    self._send(self.prepared)
                elif operation=='COMMIT':
                    if set(request)!={'operation','bytes','sha256'} or self.prepared is None or self.committed is not None:
                        raise HolderStop('Root commit phase mismatch')
                    size=request['bytes']
                    if type(size) is not int or not 1<=size<=self.budget.contract['max_output_bytes']:
                        raise HolderStop('Root full Source output size')
                    self.budget.reserve(read_bytes=size,work_bytes=size*4,live_bytes=size*3+8192)
                    parts=[]; remaining=size
                    while remaining:
                        from tools.native_support import selected_owner
                        pump=getattr(self,'io_owner',None)
                        if pump is not None: pump.wait_protocol_readable()
                        part,fd,pid,uid,gid=selected_owner().recv_packet(self.channel.fileno(),min(CHUNK,remaining),0,
                            self.budget.operation_end_ns())
                        if fd!=-1 or (pid,uid,gid)!=self.worker_peer:
                            raise HolderStop('Root Source output actual peer mismatch')
                        self.budget.actual_read_bytes+=len(part)
                        if not part: raise HolderStop('Root incomplete Source output transport')
                        parts.append(part); remaining-=len(part)
                    self.source_output=b''.join(parts)
                    parts.clear()
                    if hashlib.sha256(self.source_output).hexdigest()!=request['sha256']:
                        raise HolderStop('Root exact Source output SHA mismatch')
                    records=self.verify_whole()
                    self.committed={'operation':'COMMIT','generation':self.selection['generation'],
                        'source_output_bytes':size,'source_output_sha256':request['sha256'],
                        'root_pid':self.root_pid,'archive_count':202,'archives':records,
                        'scope':'whole-root-holder-generation.v1','originals_still_held':True,
                        'permission_is_metadata':False,'Root_Image':'NOT_PROVEN'}
                    self._send(self.committed)
                    return self.source_output,self.committed
                else: raise HolderStop('Root unknown operation')
            finally:
                raw=None
                raw=None; request=None
                self.budget.release('live_bytes',262144)
        raise HolderStop('Root finite protocol exhausted')

    def close(self):
        """Only the actual Root owner calls after publication or terminal refusal.

        Full uncertainty remains in the Root owner's terminal evidence; it must
        not claim a successful release when any close cannot be confirmed.
        """
        if self.closed: return self.budget.close_uncertainties
        self.closed=True
        for path,slot in self.files.items():
            fd=slot['fd']; slot['fd']=None
            if fd is not None: self.budget.close_fd(fd,path)
        fd=self.root_fd; self.root_fd=None
        if fd is not None: self.budget.close_fd(fd,self.selection['held_root'])
        channel=self.channel; self.channel=None
        if channel is not None:
            try: channel.close()
            except OSError as exc:
                self.budget.retain_origin(exc,'RootHolder.channel.close')
                self.budget.close_uncertainties.append({'fd':None,'path':None,'errno':exc.errno,
                    'exception_class':type(exc).__name__,'closed_confirmed':False,'charged':True})
            else: self.budget.release('fds',1)
        return self.budget.close_uncertainties


def existing_parent_source_entry(permit,selected_permit,selection,resource_contract,launch_contract):
    """Called pre-birth entry in the selected EXISTING outside native parent.

    Its selected native image entered friday_existing_parent_run with the
    actual immutable original start/end/generation BEFORE reading caps or
    initializing this interpreter. The fixed native registry first accepts the
    actual caller object; the caller accepts the partial capsule before Root
    construction; Root acquires original202 before native Source birth. No Root
    worker/process is launched, no pidfd transfer is called parenthood, and no
    JSON or ordinary role2 main can activate the independently selected entry.
    """
    from tools.native_owner import run_selected_source,consume_selected_source_capsule
    from tools.native_support import selected_owner,ExistingParentCaller
    if permit is None or permit is not selected_permit:raise HolderStop('actual selected existing-parent permit absent')
    permit.authorize_holder(selection,resource_contract)
    permit.authorize_native_source_launch(selection,resource_contract,launch_contract)
    native=selected_owner()
    if native is None:raise HolderStop('selected existing-parent preinitialization image absent')
    topology=selection['parent_topology']
    physical=native.snapshot()
    if physical.get('selected_final_parent_preowned') is not True:
        raise HolderStop('actual original native final-parent Root backing absent before caller initialization')
    if (physical['preowned_original_started_ns']!=topology['original_started_ns'] or
        physical['preowned_original_end_ns']!=topology['original_end_ns'] or
        physical['preowned_original_generation']!=selection['generation']):
        raise HolderStop('actual original native caller timing/generation differs')
    caller=ExistingParentCaller.__new__(ExistingParentCaller)
    native.claim_existing_parent_custody(caller,selection['generation'],
        topology['original_started_ns'],topology['original_end_ns'])
    native.accept_registered_carrier_before_birth(caller,selection['generation'])
    # The C owner already holds the partial caller even if __init__, its raw
    # error list, the capsule constructor, or the first Root allocation fails.
    capsule=None
    try:
        native.bind_original_consumer_end(caller,selection['generation'],launch_contract['completion_original_end_ns'])
        ExistingParentCaller.__init__(caller,native,selection,resource_contract)
        capsule=caller.new_capsule()
        run_selected_source(permit,selected_permit,selection,resource_contract,launch_contract,capsule=capsule)
        return caller.accept(consume_selected_source_capsule(capsule))
    except BaseException as exc:
        # Ownership is already outside the dying worker, before this encoder
        # or recorder. A partial __init__ may not yet have list fields: store
        # the raw exception in the fixed slot first and leave native ownership.
        if getattr(caller,'raw_primary',None) is None:caller.raw_primary=exc
        try:caller.retain(exc)
        except BaseException as recorder:caller.raw_recording_failure=recorder
        if capsule is not None:
            capsule.construction_failure=exc;capsule.state='CONSTRUCTION_OR_CALLER_FAILED_RETAINED'
        view={'status':'STOP_UNCONFIRMED','owned_lifetime':caller,'GO':False}
        try:caller.accept(view)
        except BaseException as accept_failure:
            caller.raw_accept_failure=accept_failure
        return view


def launch_selected_source(permit,selected_permit,selection,resource_contract,launch_contract):
    """Concrete public caller; legacy local/dying role2 path is no longer used."""
    return existing_parent_source_entry(permit,selected_permit,selection,resource_contract,launch_contract)


def complete_selected_root(permit,selected_permit,selection,resource_contract):
    """Consume within the SAME fixed existing C caller, before fallible owner.

    The C caller returns still-owned custody to the already-existing selected image. It does not claim the completion peer adopted the graph.

    Intermediate and final observer entry kinds are selected by the original
    native provider, never activated by this Python function or transport JSON.
    """
    from tools.native_owner import observe_selected_root_completion,consume_selected_observer_capsule,retain_selected_final_caller_capsule
    from tools.native_support import selected_owner,ExistingParentCaller
    if permit is None or permit is not selected_permit:raise HolderStop('actual selected observer permit absent')
    permit.authorize_root_completion(selection,resource_contract)
    native=selected_owner()
    if native is None:raise HolderStop('actual preinitialization observer caller absent')
    physical=native.snapshot()
    final=selection.get('transport_kind')=='root-observer-capsule.v1'
    if physical.get('existing_observer_entry_kind')!=(2 if final else 1):
        raise HolderStop('original C observer/final-caller entry kind not selected')
    if (physical['preowned_root_child']!=selection['root_pid'] or
        physical['preowned_root_pidfd']!=selection['root_pidfd'] or
        physical['preowned_root_generation']!=selection['generation'] or
        physical['preowned_original_generation']!=selection['generation']):
        raise HolderStop('original native preinit direct Root relation differs')
    topology={'original_started_ns':physical['preowned_original_started_ns'],
        'original_end_ns':physical['preowned_original_end_ns']}
    caller=ExistingParentCaller.__new__(ExistingParentCaller)
    native.claim_existing_parent_custody(caller,selection['generation'],
        topology['original_started_ns'],topology['original_end_ns'])
    native.accept_registered_carrier_before_birth(caller,selection['generation'])
    capsule=None
    try:
        consumer_end=min(selection['original_end_ns'],topology['original_end_ns'],
            selection.get('completion_original_end_ns',topology['original_end_ns']))
        native.bind_original_consumer_end(caller,selection['generation'],consumer_end)
        # This checks the SAME actual direct-child pair already accepted in the
        # original native preinit receiver, not a post-import first binding.
        # Fixed partial caller precedes capsule/selector/recorder projection.
        native.bind_observed_root(selection['root_pid'],selection['root_pidfd'],selection['generation'])
        ExistingParentCaller.__init__(caller,native,selection,resource_contract,topology=topology)
        capsule=caller.new_capsule()
        observe_selected_root_completion(permit,selected_permit,selection,resource_contract,capsule=capsule)
        result=retain_selected_final_caller_capsule(capsule) if final else consume_selected_observer_capsule(capsule)
        return caller.accept(result)
    except BaseException as exc:
        if getattr(caller,'raw_primary',None) is None:caller.raw_primary=exc
        try:caller.retain(exc)
        except BaseException as recorder:caller.raw_recording_failure=recorder
        if capsule is not None:
            capsule.construction_failure=exc;capsule.state='CONSTRUCTION_OR_CALLER_FAILED_RETAINED'
        view={'status':'STOP_UNCONFIRMED','owned_lifetime':caller,'GO':False}
        try:caller.accept(view)
        except BaseException as accept_failure:
            caller.raw_accept_failure=accept_failure
        return view
