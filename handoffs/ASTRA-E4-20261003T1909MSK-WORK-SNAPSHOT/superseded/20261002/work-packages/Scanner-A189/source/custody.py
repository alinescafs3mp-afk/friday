"""Before/after custody comparison over caller-supplied NOFOLLOW observations.

compare_custody does not stat. read_nofollow_file is the future granted reader.
Fixtures do not set filesystem_read, so this source job never calls it.
"""

import errno
import os

from .causes import pack
from .pins import HELD_ROOT

_IDENTITY = (
    "device",
    "inode",
    "size",
    "mtime_ns",
    "ctime_ns",
    "uid",
    "gid",
    "full_mode",
    "nlink",
    "sha256",
)


def stat_identity9(st):
    return tuple(getattr(st,key) for key in ("st_dev","st_ino","st_mode","st_uid",
                  "st_gid","st_nlink","st_size","st_mtime_ns","st_ctime_ns"))


def retain_origin_failure(meter,exc,operation,path=None):
    """Own the actual exception BEFORE a typed projection or channel close.

    The exception/traceback graph is retained independently of its serialized
    record. A recorder failure is propagated with the real origin as its cause;
    it cannot quietly replace the origin with a policy string. Native physical
    terminal escrow precedes these producers and no detail is truncated.
    """
    try:
        native=meter.get('native_owner')
        if native is not None:native.begin_terminal()
        meter.setdefault('owned_origin_exceptions',[]).append(exc)
        from .causes import exception_detail
        record=exception_detail(exc)
        record['origin_operation']=operation
        record['origin_path']={'type':'bytes','hex':path.hex()} if isinstance(path,bytes) else path
        meter.setdefault('origin_failures',[]).append(record)
        return record
    except BaseException as recorder_failure:
        # Python's explicit cause retains both full traceback chains even when
        # the admitted record lane itself cannot construct an error record.
        raise recorder_failure from exc


def record_close_uncertainty(meter, operation, fd, path, exc):
    """Detach-once close failures keep their FD quota and causal observation.

    The per-open metadata/lease envelope must cover this failure record. A
    successful close is not inferred from Linux numeric-descriptor reuse.
    """
    retain_origin_failure(meter,exc,operation,path)
    meter.setdefault('fd_close_uncertainties',[]).append({
        'operation':operation,'descriptor':fd,'path':path,
        'exception_class':type(exc).__name__,'errno':getattr(exc,'errno',None),
        'exception_message':str(exc),'filename':{'type':'bytes','hex':exc.filename.hex()} if isinstance(getattr(exc,'filename',None),bytes) else getattr(exc,'filename',None),
        'filename2':{'type':'bytes','hex':exc.filename2.hex()} if isinstance(getattr(exc,'filename2',None),bytes) else getattr(exc,'filename2',None),
        'attempted_once':True,'closed_confirmed':False,'charged':True})
    meter['post_scan_cause']='held_fd_unretained'


ROOT_LIFETIME = {'mode':'whole-root-holder-generation.v1',
    'max_simultaneous_archive_fds':202,'aggregate_is_simultaneous_custody':True,
    'reopen_substitution':False}


class RootHolderEndpoint:
    """Opaque stock endpoint supplied by the separately selected actual tool.

    Neither a role JSON document nor a permission boolean can be this socket.
    The constructor does NOT create an archive holder or authorize Root effects.
    """
    def __init__(self,channel,selected_root_peer,generation):
        self.channel=channel
        self.selected_root_peer=selected_root_peer
        self.generation=generation

    def take(self):
        channel=self.channel; self.channel=None
        if channel is None: raise RuntimeError('Root endpoint already consumed')
        return channel,self.selected_root_peer,self.generation


class RootHolderClient:
    """Borrow original Root FDs without owning or retiring Root's generations.

    The reviewed actual native launcher constructs this client with its selected
    Root process credentials/channel, never from admission JSON or fixture data.
    A private accepted AF_UNIX connection is required: a pre-fork socketpair's
    SO_PEERCRED can identify the creator rather than the connecting Source.
    The socket and each SCM_RIGHTS duplicate count in Source's unchanged 16.
    """
    def __init__(self,channel,selected_root_peer,generation,resources,meter,prepaid=False):
        import socket,struct
        from .bounds import reserve
        from .digests import DigestStop
        self.channel=channel
        self.generation=generation
        self.resources=resources
        self.meter=meter
        self.prepared=None
        self.committed=None
        self.borrowed=set()
        self.borrowed_set_live=0
        if not prepaid:
            tick=reserve(resources,meter,fds=0 if meter.get('native_owner') is not None else 1,
                         live_bytes=16384,work_bytes=4096)
            if tick: raise DigestStop(tick)
        try:
            if channel.family!=socket.AF_UNIX or channel.type!=socket.SOCK_SEQPACKET:
                raise DigestStop('held_fd_unretained')
            peer=struct.unpack('3i',channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if peer!=tuple(selected_root_peer) or peer[0]==os.getpid():
                raise DigestStop('held_fd_unretained')
            self.peer=peer
            # A forked socketpair's SO_PEERCRED belongs to its Root creator.
            # Root verifies this READY packet's actual kernel SCM_CREDENTIALS.
            self._send({'operation':'READY','generation':generation})
            hello,fd=self._receive('HELLO',16384)
            if (fd is not None or hello.get('root_pid')!=peer[0] or
                hello.get('generation')!=generation or hello.get('archive_count')!=202 or
                hello.get('scope')!=ROOT_LIFETIME['mode'] or hello.get('source_max_fds')!=16 or
                hello.get('canonical_workers')!=4 or hello.get('owner')!='actual-root-native-tool'):
                raise DigestStop('held_fd_unretained')
            hello=None
            from .bounds import release_live
            release_live(meter,16384*16)
        except BaseException as exc:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,exc)
            try:self.close()
            except BaseException as cleanup:
                retain_source_origin(meter,cleanup)
                raise
            raise

    def _send(self,message):
        from .canonical import canonical_bytes_bounded
        from .bounds import reserve,release_live
        from .digests import DigestStop
        raw=canonical_bytes_bounded(message,16384,self.resources,self.meter)
        if raw is None: raise DigestStop('resource_ceiling_exceeded')
        try:
            tick=reserve(self.resources,self.meter,output_bytes=len(raw),work_bytes=len(raw))
            if tick: raise DigestStop(tick)
            native=self.meter.get('native_owner')
            if native is None: raise DigestStop('ingress_context_unbound')
            if native.send_packet(self.channel.fileno(),raw)!=len(raw): raise DigestStop('held_fd_unretained')
        finally:
            amount=len(raw); raw=None; release_live(self.meter,amount)

    def _receive(self,operation,limit,descriptor=False):
        from .bounds import reserve,release_live
        from .canonical import canonical_loads
        from .digests import DigestStop
        # A 56-byte native ancillary arena may contain at most ten raw rights
        # descriptors when mandatory credentials are absent. Reserve this
        # error-state maximum too, within original Source16. Native registers
        # every delivered descriptor BEFORE any Python return/JSON allocation.
        tick=reserve(self.resources,self.meter,read_bytes=limit,work_bytes=limit*8,
                     live_bytes=limit*16,fds=10)
        if tick: raise DigestStop(tick)
        raw=message=None
        received=None
        passed=False
        native=self.meter.get('native_owner')
        try:
            if native is None: raise DigestStop('ingress_context_unbound')
            raw,fd,pid,uid,gid=native.recv_packet(self.channel.fileno(),limit,1 if descriptor else 0)
            received=fd if fd>=0 else None
            self.meter['actual_read_bytes']=self.meter.get('actual_read_bytes',0)+len(raw)
            if (pid,uid,gid)!=self.peer or descriptor!=(received is not None):
                raise DigestStop('held_fd_unretained')
            # Register the successfully returned physical lease before parsing.
            if received is not None:
                self.meter['retained_fd']=received
            message=canonical_loads(raw,max_bytes=limit,max_depth=64)
            if not isinstance(message,dict) or message.get('operation')!=operation:
                raise DigestStop('held_fd_unretained')
            if operation!='HELLO' and message.get('generation')!=self.generation:
                raise DigestStop('held_fd_unretained')
            # Returned protocol graphs stay in the client's enclosing charge;
            # the full selected native/stdlib allocation audit is still required.
            passed=True
            return message,received
        finally:
            raw=None
            # Native close uncertainty is not an unused reservation refund.
            if native is not None:
                state=native.snapshot()
                self.meter['fds']=state['fds']+state['uncertain_fds']
            else: self.meter['fds']-=10
            if not passed:
                message=None
                release_live(self.meter,limit*16)
                if received is not None:
                    retired=received; received=None
                    if self.meter.get('retained_fd')==retired: self.meter.pop('retained_fd')
                    try: native.close_fd(retired)
                    except OSError as exc:
                        record_close_uncertainty(self.meter,'root_borrow.close',retired,None,exc)
                    else: self.meter['fds']-=1

    def borrow(self,path):
        from .digests import DigestStop
        from .bounds import reserve,release_live
        if self.prepared is not None or path in self.borrowed:
            raise DigestStop('held_fd_unretained')
        self._send({'operation':'BORROW','relative_path':path})
        record,fd=self._receive('BORROW',16384,True)
        # Register the duplicate in the public owner before validation may fail.
        self.meter['retained_fd']=fd
        self.meter['retained_relative_path']=path
        if record.get('relative_path')!=path or record.get('identity9')!=[str(v) for v in stat_identity9(os.fstat(fd))]:
            raise DigestStop('custody_identity_changed')
        tick=reserve(self.resources,self.meter,live_bytes=512,work_bytes=1024)
        if tick: raise DigestStop(tick)
        self.borrowed.add(path)
        self.borrowed_set_live+=512
        record=None
        release_live(self.meter,16384*16)
        return fd

    def prepare(self,payload_sha256):
        from .digests import DigestStop
        if len(self.borrowed)!=202 or self.prepared is not None:
            raise DigestStop('held_fd_unretained')
        self._send({'operation':'PREPARE','payload_sha256':payload_sha256})
        record,fd=self._receive('PREPARE',262144)
        if (fd is not None or record.get('root_pid')!=self.peer[0] or
            record.get('payload_sha256')!=payload_sha256 or record.get('archive_count')!=202 or
            record.get('scope')!=ROOT_LIFETIME['mode'] or len(record.get('archives',[]))!=202):
            raise DigestStop('held_fd_unretained')
        self.prepared=record
        return record

    def commit(self,blob):
        from .digests import content_sha256,DigestStop
        from .bounds import reserve,release_live
        if self.prepared is None or self.committed is not None:
            raise DigestStop('held_fd_unretained')
        digest=content_sha256(blob,self.resources,self.meter)
        self._send({'operation':'COMMIT','bytes':len(blob),'sha256':digest})
        for offset in range(0,len(blob),65536):
            amount=min(65536,len(blob)-offset)
            tick=reserve(self.resources,self.meter,work_bytes=amount,live_bytes=amount+64,output_bytes=amount)
            if tick: raise DigestStop(tick)
            piece=None
            try:
                piece=blob[offset:offset+amount]
                if self.meter['native_owner'].send_packet(self.channel.fileno(),piece)!=amount:
                    raise DigestStop('held_fd_unretained')
            finally: piece=None; release_live(self.meter,amount+64)
        record,fd=self._receive('COMMIT',262144)
        if (fd is not None or record.get('root_pid')!=self.peer[0] or
            record.get('source_output_bytes')!=len(blob) or record.get('source_output_sha256')!=digest or
            record.get('archive_count')!=202 or record.get('archives')!=self.prepared['archives'] or
            record.get('originals_still_held') is not True or record.get('permission_is_metadata') is not False):
            raise DigestStop('held_fd_unretained')
        self.committed=record
        # This external Root receipt hashes exact Source bytes without a cycle.
        # It is not inserted into the already encoded Source output.
        self.meter['external_root_terminal_receipt']=record
        return record

    def close(self):
        from .bounds import release_live
        channel=self.channel; self.channel=None
        if channel is None: return
        descriptor=channel.detach()
        try:
            native=self.meter.get('native_owner')
            if native is None: os.close(descriptor)
            else: native.close_fd(descriptor)
        except OSError as exc:
            record_close_uncertainty(self.meter,'root_channel.close',descriptor,None,exc)
        else: self.meter['fds']-=1
        self.borrowed.clear()
        release_live(self.meter,self.borrowed_set_live)
        self.borrowed_set_live=0
        # There is deliberately no RELEASE RPC for any Root original FD.


class HeldRange:
    """Bounded range of the SAME original generation, not mmap/scratch/reopen.
    Headers allocate <=1MiB; content chunks <=64KiB. Only the custody owner
    closes the shared generation. Metadata returns remain conservatively
    charged until their explicit enclosing archive scope finishes.
    """
    def __init__(self,owner,start,size,resources,meter):
        self.owner,self.start,self.size=owner,start,size
        self.resources,self.meter=resources,meter

    def __len__(self): return self.size

    def part(self,start,stop):
        from .bounds import reserve
        from .digests import DigestStop
        if not 0<=start<=stop<=self.size: raise DigestStop("expected_size_mismatch")
        cause=reserve(self.resources,self.meter,live_bytes=512,work_bytes=128)
        if cause: raise DigestStop(cause)
        return HeldRange(self.owner,self.start+start,stop-start,self.resources,self.meter)

    def _check(self):
        from .digests import DigestStop
        if self.owner["retired"] or stat_identity9(os.fstat(self.owner["fd"]))!=self.owner["identity9"]:
            raise DigestStop("held_fd_unretained")

    def read_part(self,start,size):
        from .bounds import reserve,release_live
        from .digests import DigestStop
        if not 0<=size<=1048576 or not 0<=start<=start+size<=self.size:
            raise DigestStop("resource_ceiling_exceeded")
        cause=reserve(self.resources,self.meter,read_bytes=size,work_bytes=size*2,live_bytes=size*2+64)
        if cause: raise DigestStop(cause)
        try:
            self._check()
            raw=os.pread(self.owner["fd"],size,self.start+start)
            self.meter["actual_read_bytes"]=self.meter.get("actual_read_bytes",0)+len(raw)
            if len(raw)!=size: raise DigestStop("expected_size_mismatch")
            self._check()
            self.meter["range_return_live"]=self.meter.get("range_return_live",0)+size+32
            return raw
        except BaseException as exc:
            from tools.native_support import retain_source_origin
            retain_source_origin(self.meter,exc)
            release_live(self.meter,size+32)
            raise
        finally:
            release_live(self.meter,size+32)

    def chunks(self):
        from .bounds import release_live
        for offset in range(0,self.size,65536):
            raw=self.read_part(offset,min(65536,self.size-offset))
            try: yield raw
            finally:
                amount=len(raw)+32
                raw=None
                self.meter["range_return_live"]-=amount
                release_live(self.meter,amount)

    def __getitem__(self,key):
        if isinstance(key,int):
            start=key if key>=0 else self.size+key
            return self.read_part(start,1)[0]
        start,stop,step=key.indices(self.size)
        if step!=1: raise ValueError("stream_range_stride")
        return self.read_part(start,max(0,stop-start))

    def startswith(self,prefix): return self[:len(prefix)]==prefix

    def rfind(self,token,start=0,stop=None):
        stop=self.size if stop is None else stop
        found=self[start:stop].rfind(token)
        return -1 if found<0 else start+found


def owned_metadata_directory(path,expected_names,resources,meter):
    """Bounded Linux metadata namespace enumeration with owned directory/iterator FDs."""
    import stat
    from .bounds import reserve
    if not isinstance(expected_names,(tuple,list,set,dict)) or not 1 <= len(expected_names) <= 64:
        return "schema_unknown"
    if any(not isinstance(name,str) or not name or len(name)>255 or "/" in name or name in (".","..") for name in expected_names):
        return "schema_unknown"
    # Linux NAME_MAX and one entry at a time bound the iterator's native entry
    # allocation. The duplicated scandir descriptor is prepaid as well.
    tick = reserve(resources,meter,fds=2,work_bytes=(len(expected_names)+1)*8192,
                   live_bytes=(len(expected_names)+1)*8192)
    if tick:
        return tick
    fd = None
    iterator = None
    try:
        before = os.stat(path,follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            return "schema_unknown"
        fd = os.open(path,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        if stat_identity9(os.fstat(fd)) != stat_identity9(before):
            return "custody_identity_changed"
        seen = set()
        iterator = os.scandir(fd)
        for entry in iterator:
            name = entry.name
            if name not in expected_names or name in seen or len(seen) >= len(expected_names):
                return "schema_unknown"
            seen.add(name)
        if seen != set(expected_names):
            return "schema_unknown"
        if stat_identity9(os.fstat(fd)) != stat_identity9(before) or stat_identity9(os.stat(path,follow_symlinks=False)) != stat_identity9(before):
            return "custody_identity_changed"
        meter.setdefault("metadata_directory_identities",{})[path] = stat_identity9(before)
        return None
    except OSError as exc:
        retain_origin_failure(meter,exc,'metadata_directory.read',path)
        return "nofollow_open_failed"
    finally:
        uncertain=0
        try:
            if iterator is not None:
                retired=iterator
                iterator=None
                try: retired.close()
                except (MemoryError,OSError,ValueError,RuntimeError) as exc:
                    uncertain+=1
                    record_close_uncertainty(meter,'scandir.close',None,path,exc)
                finally: retired=None
        finally:
            if fd is not None:
                retired_fd=fd
                fd=None
                try: os.close(retired_fd)
                except OSError as exc:
                    uncertain+=1
                    record_close_uncertainty(meter,'metadata_directory.close',retired_fd,path,exc)
            meter["fds"] = max(0,meter.get("fds",0)-(2-uncertain))


def owned_metadata_bytes(path, limit, resources, meter):
    """One prepaid exact regular-file read with uniform descriptor ownership.

    This is a future Source/schema metadata reader, never an authority issuer.
    Its input directory is independently selected by the trusted embedding.
    """
    import stat
    from .bounds import reserve
    from .digests import content_sha256
    try:before = os.stat(path,follow_symlinks=False)
    except OSError as exc:
        retain_origin_failure(meter,exc,'metadata_file.stat',path)
        return None,None,'nofollow_open_failed'
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= limit:
        return None,None,"schema_unknown"
    amount = before.st_size
    tick = reserve(resources,meter,fds=1,read_bytes=amount,work_bytes=amount * 4,
                   live_bytes=amount * 16 + 65536)
    if tick:
        return None,None,tick
    fd = None
    try:
        fd = os.open(path,os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(fd)
        if stat_identity9(before) != stat_identity9(opened):
            return None,None,"custody_identity_changed"
        out = bytearray(amount)
        offset = 0
        while offset < amount:
            piece = os.read(fd,min(65536,amount - offset))
            if not piece:
                return None,None,"expected_size_mismatch"
            meter["actual_read_bytes"] = meter.get("actual_read_bytes",0) + len(piece)
            out[offset:offset+len(piece)] = piece
            offset += len(piece)
        digest = content_sha256(out,resources,meter)
        after = os.fstat(fd)
        named = os.stat(path,follow_symlinks=False)
        if any(stat_identity9(st) != stat_identity9(before) for st in (after,named)):
            return None,None,"custody_identity_changed"
        return out,{"sha256":digest,"size":amount,"identity9":stat_identity9(before)},None
    except OSError as exc:
        retain_origin_failure(meter,exc,'metadata_file.read',path)
        return None,None,"nofollow_open_failed"
    finally:
        uncertain=0
        if fd is not None:
            retired_fd=fd
            fd=None
            try: os.close(retired_fd)
            except OSError as exc:
                uncertain=1
                record_close_uncertainty(meter,'metadata_file.close',retired_fd,path,exc)
        meter["fds"] = max(0,meter.get("fds",0)-(1-uncertain))


def fd_mount_id(fd,resources,meter):
    """Read the actual Linux descriptor mount ID in a separately metered domain."""
    from .bounds import reserve
    tick = reserve(resources,meter,fds=1,read_bytes=8192,work_bytes=16384,live_bytes=16384)
    if tick:
        return None,tick
    info = None
    try:
        info = os.open("/proc/self/fdinfo/"+str(fd),os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        raw = bytearray()
        while len(raw) < 8192:
            piece = os.read(info,min(4096,8192-len(raw)))
            meter["actual_read_bytes"] = meter.get("actual_read_bytes",0) + len(piece)
            if not piece:
                break
            raw.extend(piece)
        if len(raw) == 8192:
            return None,"held_root_unpinned"
        lines = [line[7:].strip() for line in raw.splitlines() if line.startswith(b"mnt_id:")]
        if len(lines) != 1 or not lines[0] or len(lines[0]) > 20 or any(c not in b"0123456789" for c in lines[0]):
            return None,"held_root_unpinned"
        return int(lines[0]),None
    except OSError as exc:
        retain_origin_failure(meter,exc,'fd_mount_id.read','/proc/self/fdinfo/'+str(fd))
        return None,"held_root_unpinned"
    finally:
        uncertain=0
        if info is not None:
            retired_fd=info
            info=None
            try: os.close(retired_fd)
            except OSError as exc:
                uncertain=1
                record_close_uncertainty(meter,'fdinfo.close',retired_fd,'/proc/self/fdinfo/'+str(fd),exc)
        meter["fds"] = max(0,meter.get("fds",0)-(1-uncertain))


def selected_directory_cause(fd,path,resources,meter):
    """Consume the selected root/directory/full9/mount contract for this actual FD."""
    scope = meter.get("held_scope")
    if not isinstance(scope,dict):
        return "held_root_unpinned"
    root = scope["root"]
    selected = meter.get("held_scope_index",{}).get(path)
    if not isinstance(selected,dict):
        return "held_root_unpinned"
    st = os.fstat(fd)
    actual = {"device":st.st_dev,"inode":st.st_ino,"full_mode":st.st_mode,
              "uid":st.st_uid,"gid":st.st_gid,"nlink":st.st_nlink,"size":st.st_size,
              "mtime_ns":st.st_mtime_ns,"ctime_ns":st.st_ctime_ns}
    if actual != selected["identity"] or actual["device"] != root["identity"]["device"]:
        return "custody_identity_changed"
    mount,cause = fd_mount_id(fd,resources,meter)
    if meter.get('fd_close_uncertainties'):
        return 'held_fd_unretained'
    if cause:
        return cause
    if mount != selected["mount_id"] or mount != root["mount_id"] or selected["mount_domain"] != root["mount_domain"]:
        return "held_root_unpinned"
    return None


def _hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _record_cause(record):
    if not isinstance(record, dict):
        return "custody_record_invalid"
    if record.get("nofollow") is not True:
        return "custody_follow_refused"
    path = record.get("relative_path")
    if not isinstance(path, str) or path == "" or path.startswith("/") or ".." in path.split("/"):
        return "custody_record_invalid"
    for key in _IDENTITY:
        if key not in record:
            return "custody_record_invalid"
    if not _hex64(record.get("sha256")):
        return "custody_record_invalid"
    for key in ("device", "inode", "size", "mtime_ns", "ctime_ns", "uid", "gid", "full_mode", "nlink"):
        value = record.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return "custody_record_invalid"
    return None


def _index(records):
    if not isinstance(records, list):
        return None, "custody_record_invalid"
    indexed = {}
    for record in records:
        cause = _record_cause(record)
        if cause is not None:
            return None, cause
        path = record["relative_path"]
        if path in indexed:
            return None, "custody_membership_changed"
        indexed[path] = record
    return indexed, None


def plan_nofollow_read(relative_path):
    """Return a read plan. The plan is data and does not open a descriptor."""
    return {
        "op": "openat2",
        "relative_path": relative_path,
        "flags": ["O_RDONLY", "O_NOFOLLOW", "O_CLOEXEC"],
        "nofollow": True,
        "execute": False,
        "extract": False,
        "follow_symlink": False,
    }


def release_retained_fd(meter):
    """Fstat the retained descriptor, close it, and return the inode."""
    if not isinstance(meter, dict) or "retained_fd" not in meter:
        return None
    fd = meter.pop("retained_fd")
    owner = meter.get("archive_stream_owner")
    if isinstance(owner, dict) and owner.get("fd") == fd:
        owner["retired"] = True
    if not isinstance(fd, int) or isinstance(fd, bool):
        return {"inode": None, "device": None, "cause": "held_fd_unretained"}
    ident = None
    cause = None
    close_confirmed = False
    try:
        st = os.fstat(fd)
        ident = {
            "inode": int(st.st_ino),
            "device": int(st.st_dev),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
            "ctime_ns": int(st.st_ctime_ns),
            "uid": int(st.st_uid),
            "gid": int(st.st_gid),
            "full_mode": int(st.st_mode),
            "nlink": int(st.st_nlink),
            "cause": None,
        }
    except OSError as exc:
        retain_origin_failure(meter,exc,'retained_archive.fstat',meter.get('retained_relative_path'))
        cause = "held_fd_unretained"
    finally:
        try:
            os.close(fd)
            close_confirmed = True
        except OSError as exc:
            cause = "held_fd_unretained"
            record_close_uncertainty(meter,'retained_archive.close',fd,meter.get('retained_relative_path'),exc)
        if close_confirmed and meter.get("fds", 0) > 0:
            meter["fds"] -= 1
    # A failed close is sticky uncertainty, NEVER a repeated numeric close.
    return ident if cause is None else {"inode":None,"device":None,"cause":cause}


def read_nofollow_file(relative, limit, resources, meter):
    """Stream one retained file. Each path component is opened with O_NOFOLLOW.

    The caller must already have passed admission. This function does not
    follow symlinks, does not extract, and stops at the admitted byte limit.
    """
    from .bounds import charge, reserve
    from .context import effects_cause
    from .causes import enter_phase
    phase = enter_phase(meter,"HELD_CUSTODY_AND_ADMISSION","read_nofollow_file",relative)
    if phase:
        return None,phase
    effects = effects_cause(resources, meter)
    if effects:
        return None, effects

    if not isinstance(resources, dict) or resources.get("held_root") != HELD_ROOT:
        return None, "held_root_unpinned"
    if not isinstance(relative, str) or relative == "" or relative.startswith("/"):
        return None, "path_escape"
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None, "path_escape"
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        return None, "resource_ceiling_exceeded"
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    flags_file = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory_fds = []
    directory_paths = {}
    file_fd = None
    stashed = False

    def _open_charged(path, flags, dir_fd=None):
        tick = charge(resources, meter, fds=1)
        if tick is not None:
            return None, tick
        try:
            if dir_fd is None:
                opened_fd = os.open(path, flags)
            else:
                opened_fd = os.open(path, flags, dir_fd=dir_fd)
        except OSError as exc:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,exc)
            if isinstance(meter, dict) and meter.get("fds", 0) > 0:
                meter["fds"] = meter["fds"] - 1
            raise
        return opened_fd, None

    def _identity(fd, content_sha):
        st = os.fstat(fd)
        return {
            "relative_path": relative,
            "device": int(st.st_dev),
            "inode": int(st.st_ino),
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
            "ctime_ns": int(st.st_ctime_ns),
            "uid": int(st.st_uid),
            "gid": int(st.st_gid),
            "full_mode": int(st.st_mode),
            "nlink": int(st.st_nlink),
            "sha256": content_sha,
            "nofollow": True,
        }

    try:
        client=meter.get('root_holder_client')
        if isinstance(client,RootHolderClient):
            file_fd=client.borrow(relative)
            current=None
        else:
            current, tick = _open_charged(HELD_ROOT, flags_dir)
            if tick is not None:
                return None, tick
        if client is None:
            directory_fds.append(current)
            directory_paths[current] = ""
            directory_cause = selected_directory_cause(current,"",resources,meter)
            if directory_cause:
                return None,directory_cause
        for index,part in enumerate(parts[:-1] if client is None else []):
            previous = current
            current, tick = _open_charged(part, flags_dir, dir_fd=previous)
            if tick is not None:
                return None, tick
            directory_fds.append(current)
            directory_paths[current] = "/".join(parts[:index+1])
            directory_cause = selected_directory_cause(current,directory_paths[current],resources,meter)
            if directory_cause:
                return None,directory_cause
            directory_cause = selected_directory_cause(previous,directory_paths[previous],resources,meter)
            if directory_cause:
                return None,directory_cause
            directory_fds.remove(previous)
            directory_paths.pop(previous)
            try:
                os.close(previous)
            except OSError as exc:
                record_close_uncertainty(meter,'archive_directory.close',previous,None,exc)
                return None,'held_fd_unretained'
            else:
                meter["fds"] -= 1
        if client is None:
            file_fd, tick = _open_charged(parts[-1], flags_file, dir_fd=current)
            if tick is not None:
                return None, tick
        before = _identity(file_fd, None)
        mount,mount_cause = fd_mount_id(file_fd,resources,meter)
        if mount_cause or mount != meter["held_scope"]["root"]["mount_id"]:
            return None,mount_cause or "held_root_unpinned"
        if isinstance(meter, dict):
            meter["filesystem_stat_performed"] = True
        expected_identity = meter.get("expected_identity") if isinstance(meter, dict) else None
        if isinstance(expected_identity, dict):
            for field in ("device", "inode", "mtime_ns", "ctime_ns", "uid", "gid", "full_mode", "nlink"):
                if field in expected_identity and expected_identity.get(field) != before.get(field):
                    return None, "custody_identity_changed"
        import stat as statmod

        if not statmod.S_ISREG(before["full_mode"]):
            return None, "custody_record_invalid"
        expected_size = meter.get("expected_size") if isinstance(meter, dict) else None
        if isinstance(expected_size, int) and not isinstance(expected_size, bool) and before["size"] != expected_size:
            return None, "expected_size_mismatch"
        if before["size"] > limit:
            return None, "resource_ceiling_exceeded"
        need = before["size"]
        tick = reserve(resources, meter, live_bytes=131072 + 4096, work_bytes=need)
        if tick is not None:
            return None, tick
        owner={"fd":file_fd,"identity9":stat_identity9(os.fstat(file_fd)),"retired":False}
        meter["archive_persistent_live"]=meter.get("archive_persistent_live",0)+4096
        out = HeldRange(owner,0,need,resources,meter)
        import hashlib
        digest_state = hashlib.sha256()
        offset = 0
        chunks=out.chunks()
        try:
            for chunk in chunks:
                offset += len(chunk)
                digest_state.update(chunk)
        finally:
            chunk=None
            chunks.close()
        digest = digest_state.hexdigest()
        before["sha256"] = digest
        opened_record = _identity(file_fd, digest)
        after = _identity(file_fd, digest)
        if isinstance(meter, dict):
            meter["custody_before"] = before
            meter["custody_opened"] = opened_record
            meter["custody_after"] = after
            meter["retained_fd"] = file_fd
            meter["archive_stream_owner"] = owner
        stashed = True
        meter["retained_relative_path"] = relative
        return out, None
    except OSError as exc:
        retain_origin_failure(meter,exc,'archive.read_nofollow_file',relative)
        if exc.errno == errno.ELOOP:
            return None, "custody_follow_refused"
        return None, "nofollow_open_failed"
    finally:
        for fd in reversed(directory_fds):
            try:
                checked = selected_directory_cause(fd,directory_paths[fd],resources,meter)
                if checked:
                    meter["post_scan_cause"] = checked
            except (OSError,KeyError,TypeError) as exc:
                retain_origin_failure(meter,exc,'archive_directory.final_check',directory_paths.get(fd))
                meter["post_scan_cause"] = "held_fd_unretained"
            finally:
                try:
                    os.close(fd)
                except OSError as exc:
                    record_close_uncertainty(meter,'archive_directory.close',fd,directory_paths.get(fd),exc)
                else:
                    if isinstance(meter, dict) and meter.get("fds", 0) > 0:
                        meter["fds"] = meter["fds"] - 1
        if file_fd is not None and not stashed:
            retired=file_fd
            file_fd=None
            # BORROW registered this lease before early identity/size/admission
            # checks. Detach the authoritative entry before its only close.
            if meter.get('retained_fd') == retired:
                meter.pop('retained_fd')
                retire_stream_owner(meter,retired)
            try:
                os.close(retired)
            except OSError as exc:
                record_close_uncertainty(meter,'archive_original.close',retired,relative,exc)
            else:
                if isinstance(meter, dict) and meter.get("fds", 0) > 0:
                    meter["fds"] = meter["fds"] - 1
        if isinstance(meter, dict):
            meter["fds_peak"] = max(meter.get("fds_peak", 0), meter.get("fds", 0))


def compare_custody(before, after):
    left, left_cause = _index(before)
    if left_cause is not None:
        return pack("REFUSED", left_cause)
    right, right_cause = _index(after)
    if right_cause is not None:
        return pack("REFUSED", right_cause)
    if set(left) != set(right):
        return pack(
            "REFUSED",
            "custody_membership_changed",
            detail={"missing": sorted(set(left) - set(right)), "added": sorted(set(right) - set(left))},
        )
    changed = []
    for path, record in left.items():
        other = right[path]
        for key in _IDENTITY:
            if record[key] != other[key]:
                changed.append({"path": path, "field": key})
                break
    if changed:
        cause = "custody_bytes_changed" if any(item["field"] == "sha256" for item in changed) else "custody_identity_changed"
        return pack("REFUSED", cause, detail={"changed": changed})
    from .schema_validate import CUSTODY_SCHEMA, validate_value

    for record in left.values():
        schema_cause = validate_value(CUSTODY_SCHEMA, record)
        if schema_cause is not None:
            return pack("REFUSED", schema_cause)
    return pack(
        "OBSERVED",
        None,
        observation={
            "custody_changed": False,
            "compared": len(left),
            "nofollow": True,
            "filesystem_stat_performed": False,
        },
    )


def seal_archive_custody(meter, data, expected_sha, resources=None):
    from .digests import content_sha256

    digest = content_sha256(data, resources, meter) if isinstance(data, (bytes, bytearray,HeldRange)) else None
    before = meter.get("custody_before") if isinstance(meter, dict) else None
    opened = meter.get("custody_opened") if isinstance(meter, dict) else None
    after = meter.get("custody_after") if isinstance(meter, dict) else None
    if isinstance(meter, dict) and isinstance(digest, str):
        if isinstance(expected_sha, str) and expected_sha != digest:
            meter["post_scan_cause"] = "expected_sha256_mismatch"
        if isinstance(opened, dict) and isinstance(opened.get("sha256"), str) and opened.get("sha256") != digest:
            meter["post_scan_cause"] = "custody_identity_changed"
    seal = {
        "before": before,
        "opened": opened,
        "after": after,
        "terminal_content_sha256": digest,
        "expected_sha256": expected_sha,
    }
    if isinstance(meter, dict) and meter.get("whole_custody") is True and "retained_fd" in meter:
        meter.setdefault("custody_leases", []).append({"fd": meter.pop("retained_fd"),
            "relative_path": meter.pop("retained_relative_path"), "before": before,
            "expected_sha256": expected_sha, "seal": seal})
    return seal


def begin_archive(meter, expected, held):
    # Never overwrite a previous generation's actual descriptor.  The public
    # orchestrator must complete its selected per-material output scope first.
    if meter.get("custody_leases") or "retained_fd" in meter:
        from .digests import DigestStop
        raise DigestStop("held_fd_unretained")
    meter["archive_live_start"] = meter.get("live_bytes", 0)
    meter["archive_persistent_live"] = 0
    for key in ("expected_identity", "expected_size", "expected_sha256", "expected_path",
                "custody_before", "custody_opened", "custody_after", "post_scan_cause"):
        meter.pop(key, None)
    meter["expected_size"] = expected.get("size")
    meter["expected_sha256"] = expected.get("sha256")
    meter["expected_path"] = expected.get("relative_path")
    identity = expected.get("held_identity")
    supplied = held.get("before") if isinstance(held, dict) else None
    if not isinstance(identity, dict) and isinstance(supplied, list) and len(supplied) == 1:
        identity = supplied[0]
    if isinstance(identity, dict):
        meter["expected_identity"] = identity


def reserve_public_members(resources, meter, members):
    from .bounds import reserve
    amount = 0
    for member in members:
        amount += 16384 + sum(len(member.get(key) or "") * 8 for key in ("name", "normalized_path", "link_target", "uname", "gname"))
        for origin in member.get("extension_records", []):
            amount += 4096 + sum(len(value) * 8 for value in origin.values() if isinstance(value, str))
    tick = reserve(resources, meter, live_bytes=amount, work_bytes=max(1, len(members)) * 512)
    if tick is None:
        meter["archive_persistent_live"] = meter.get("archive_persistent_live", 0) + amount
    return tick


def release_archive_live(meter):
    from .bounds import release_live
    start = meter.pop("archive_live_start", meter.get("live_bytes", 0))
    persistent = meter.pop("archive_persistent_live", 0)
    release_live(meter, max(0, meter.get("live_bytes", 0) - start - persistent))


def retain_public_observation(resources,meter,observation):
    """Transfer the ENTIRE returned Python graph, not a flat member estimate.
    Parser reservations stay admitted while this transfer is measured/reserved.
    Scalars, UTF8/hex/RECORD/control maps/origins and nested containers are all
    counted by object identity, including shared aliases. This actual Python
    object envelope is NOT a native allocator/RSS/implicit-load proof.
    """
    import sys
    from .bounds import reserve,release_live
    tick=reserve(resources,meter,live_bytes=4096,work_bytes=4096)
    if tick: return tick
    stack=[observation]; seen=set(); amount=0; traversal=4096
    try:
        while stack:
            value=stack.pop(); identity=id(value)
            if identity in seen: continue
            children=2*len(value) if isinstance(value,dict) else len(value) if isinstance(value,(list,tuple)) else 0
            temporary=128+children*16
            tick=reserve(resources,meter,live_bytes=temporary,work_bytes=128+children*64)
            if tick: return tick
            traversal+=temporary
            seen.add(identity); amount+=sys.getsizeof(value)*2+64
            if isinstance(value,dict):
                for key,item in value.items(): stack.append(key); stack.append(item)
            elif isinstance(value,(list,tuple)): stack.extend(value)
            elif not isinstance(value,(str,int,bool,bytes,bytearray,type(None))):
                return "expected_shape_invalid"
        tick=reserve(resources,meter,live_bytes=amount,work_bytes=amount)
        if tick: return tick
        meter["archive_persistent_live"] = meter.get("archive_persistent_live",0)+amount
        meter.setdefault("complete_retained_graphs",[]).append({"objects":len(seen),"envelope_bytes":amount,"native_RAM_proof":False})
        return None
    finally:
        stack.clear(); seen.clear(); release_live(meter,traversal)


def retire_stream_owner(meter,fd):
    owner=meter.get("archive_stream_owner")
    if isinstance(owner,dict) and owner.get("fd")==fd: owner["retired"]=True


def checked_named_identity(relative,resources,meter):
    """Each created traversal descriptor has its own before-open reservation.

    A slot is detached before one close. A failed close retains its charge and
    complete uncertainty; an uncreated slot alone may be refunded on open error.
    All selected directory/mount and final named checks still execute.
    """
    from .bounds import reserve
    from .digests import DigestStop
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC
    current=None; current_path=None
    def opened(path,parent):
        tick=reserve(resources,meter,fds=1,work_bytes=4096,live_bytes=4096)
        if tick: raise DigestStop(tick)
        try: return os.open(path,flags,dir_fd=parent)
        except BaseException as exc:
            from tools.native_support import retain_source_origin
            retain_source_origin(meter,exc)
            meter['fds']-=1
            raise
    def closed(fd,path):
        try: os.close(fd)
        except OSError as exc:
            record_close_uncertainty(meter,'named_traversal.close',fd,path,exc)
        else: meter['fds']-=1
    try:
        current=opened(HELD_ROOT,None); current_path=''
        problem=selected_directory_cause(current,current_path,resources,meter)
        if problem: raise DigestStop(problem)
        parts=relative.split('/')
        for index,part in enumerate(parts[:-1]):
            next_fd=opened(part,current)
            previous=current; previous_path=current_path
            current=next_fd; current_path='/'.join(parts[:index+1])
            try:
                problem=selected_directory_cause(previous,previous_path,resources,meter)
                if problem: raise DigestStop(problem)
            finally: closed(previous,previous_path)
            if meter.get('fd_close_uncertainties'): raise DigestStop('held_fd_unretained')
            problem=selected_directory_cause(current,current_path,resources,meter)
            if problem: raise DigestStop(problem)
        named=os.stat(parts[-1],dir_fd=current,follow_symlinks=False)
        problem=selected_directory_cause(current,current_path,resources,meter)
        if problem: raise DigestStop(problem)
        return named
    finally:
        retired=current; current=None
        if retired is not None: closed(retired,current_path)


def complete_custody(resources, meter, release=True):
    import hashlib
    from .bounds import reserve, release_live
    cause = "held_fd_unretained" if meter.get('fd_close_uncertainties') else None
    leases = meter.get("custody_leases", [])
    for lease in tuple(leases):
        fd = lease["fd"]
        digest=None
        digest_live=0
        try:
            original = lease["before"]
            st = os.fstat(fd)
            current = {"device": st.st_dev, "inode": st.st_ino, "size": st.st_size,
                       "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
                       "uid": st.st_uid, "gid": st.st_gid, "full_mode": st.st_mode, "nlink": st.st_nlink}
            if any(current[key] != original[key] for key in current):
                cause = cause or "custody_identity_changed"
                continue
            tick=reserve(resources,meter,work_bytes=8192,live_bytes=8192)
            if tick:
                cause=cause or tick
                continue
            digest_live=8192
            digest = hashlib.sha256()
            offset = 0
            while offset < current["size"]:
                size = min(65536, current["size"] - offset)
                tick = reserve(resources, meter, read_bytes=size, work_bytes=size, live_bytes=size+64)
                if tick:
                    cause = cause or tick
                    break
                piece=None
                try:
                    piece = os.pread(fd, size, offset)
                    meter["actual_read_bytes"] = meter.get("actual_read_bytes",0) + len(piece)
                    if not piece:
                        cause = cause or "expected_size_mismatch"
                        break
                    digest.update(piece)
                    offset += len(piece)
                finally:
                    piece=None
                    release_live(meter, size+64)
            terminal = os.fstat(fd)
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(terminal, key) != getattr(st, key) for key in fields):
                cause = cause or "custody_identity_changed"
            named=checked_named_identity(lease['relative_path'],resources,meter)
            if any(getattr(named,key)!=getattr(st,key) for key in fields):
                cause=cause or 'custody_identity_changed'
            if meter.get('fd_close_uncertainties'): cause=cause or 'held_fd_unretained'
            if offset == current["size"] and digest.hexdigest() != lease["expected_sha256"]:
                cause = cause or "custody_bytes_changed"
            lease["seal"].update({"terminal_identity": current,
                "terminal_content_sha256": digest.hexdigest() if offset == current["size"] else None,
                "completion": "CONTENT_REHASH_BEFORE_FINAL_OUTPUT", "named_path_checked": True})
            lease["terminal_identity"] = current
        except (OSError, KeyError, TypeError,ValueError,RuntimeError) as exc:
            from .digests import DigestStop
            if not isinstance(exc,DigestStop):
                retain_origin_failure(meter,exc,'complete_custody',lease.get('relative_path'))
            cause=cause or (exc.cause if isinstance(exc,DigestStop) else 'held_fd_unretained')
        finally:
            digest=None
            release_live(meter,digest_live)
            if release:
                # Detach BEFORE the one close attempt; fallback cannot replay it.
                leases.remove(lease)
                retire_stream_owner(meter,fd)
                try:
                    os.close(fd)
                except OSError as exc:
                    cause = cause or "held_fd_unretained"
                    record_close_uncertainty(meter,'custody_lease.close',fd,lease.get('relative_path'),exc)
                else:
                    meter["fds"] = max(0, meter.get("fds", 0) - 1)
    if release:
        meter.pop("custody_leases", None)
    if "retained_fd" in meter:
        released = release_retained_fd(meter)
        if isinstance(released, dict) and released.get("cause"):
            cause = cause or released["cause"]
    return cause


def close_whole_custody(meter):
    from .bounds import reserve
    resources = meter.get("active_resources")
    cause = None
    for lease in meter.pop("custody_leases", []):
        fd = lease["fd"]
        try:
            st = os.fstat(fd)
            actual = {"device": st.st_dev, "inode": st.st_ino, "size": st.st_size,
                      "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
                      "uid": st.st_uid, "gid": st.st_gid, "full_mode": st.st_mode, "nlink": st.st_nlink}
            if actual != lease.get("terminal_identity"):
                cause = cause or "custody_identity_changed"
            named=checked_named_identity(lease['relative_path'],resources,meter)
            fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_uid','st_gid','st_mode','st_nlink')
            if any(getattr(named,key)!=getattr(st,key) for key in fields):
                cause=cause or 'custody_identity_changed'
            if meter.get('fd_close_uncertainties'): cause=cause or 'held_fd_unretained'
        except (OSError,KeyError,ValueError,TypeError,RuntimeError) as exc:
            from .digests import DigestStop
            if not isinstance(exc,DigestStop):
                retain_origin_failure(meter,exc,'close_whole_custody',lease.get('relative_path'))
            cause=cause or (exc.cause if isinstance(exc,DigestStop) else 'held_fd_unretained')
        finally:
            retire_stream_owner(meter,fd)
            try:
                os.close(fd)
            except OSError as exc:
                cause = cause or "held_fd_unretained"
                record_close_uncertainty(meter,'whole_custody.close',fd,lease.get('relative_path'),exc)
            else:
                meter["fds"] = max(0, meter.get("fds", 0) - 1)
    return cause


def complete_material_output(observed, expected, resources, meter):
    """Encode one COMPLETE observation while its ORIGINAL archive FD is held.

    This is an explicitly selected per-material generation, not a simultaneous
    whole202 snapshot.  No arbitrary archive reopen occurs.  Later aggregate
    consumers receive only the completed observation and its exact-byte receipt.
    The future embedding must independently select this lifetime before ingress;
    this function does not waive custody or issue a read grant.
    """
    from .bounds import reserve, release_live
    from .canonical import canonical_bytes_bounded
    from .digests import content_sha256, DigestStop
    from .causes import enter_phase
    leases = meter.get("custody_leases", [])
    if len(leases) != 1 or leases[0]["relative_path"] != expected["relative_path"]:
        return None, "held_fd_unretained"
    if observed.get("status") != "OBSERVED":
        return None, observed.get("cause") or "expected_shape_invalid"
    tick = enter_phase(meter, "FINAL_SCHEMA_OUTPUT_AND_CUSTODY", "complete_material_output", expected["relative_path"])
    if tick:
        return None, tick
    tick = reserve(resources, meter, work_bytes=8192, live_bytes=20480)
    if tick:
        return None, tick
    blob = None
    receipt = None
    receipt_live=0
    cause = None
    try:
        cause = complete_custody(resources, meter, release=False)
        if cause:
            raise DigestStop(cause)
        from .schema_validate import WHEEL_SCHEMA, DEB_SCHEMA, validate_document
        observation = observed["observation"]
        cause = validate_document(observation, WHEEL_SCHEMA if expected["archive_class"] == "wheel" else DEB_SCHEMA, resources, meter)
        if cause:
            raise DigestStop(cause)
        room = resources["ceilings"]["max_output_bytes"] - meter.get("output_bytes", 0) - 8192
        blob = canonical_bytes_bounded(observation, room, resources, meter)
        if blob is None:
            raise DigestStop("resource_ceiling_exceeded")
        cause = reserve(resources, meter, output_bytes=len(blob))
        if cause:
            raise DigestStop(cause)
        digest = content_sha256(blob, resources, meter)
        lease = leases[0]
        cause=reserve(resources,meter,work_bytes=4096,live_bytes=8192)
        if cause: raise DigestStop(cause)
        receipt_live=8192
        receipt = {"relative_path": expected["relative_path"],
            "archive_sha256": expected["sha256"], "identity": lease["terminal_identity"],
            "observation_sha256": digest, "observation_bytes": len(blob),
            "same_original_fd_through_encoding": True,
            "held_after_named_after_checked": True,
            "scope": "per-material-complete-observation-generation.v1"}
    except DigestStop as stop:
        from .causes import source_exception_detail
        detail=source_exception_detail(meter,stop)
        meter.setdefault('origin_failures',[]).append(detail)
        cause = stop.cause
    finally:
        # Checks are still against this original descriptor.  Close is not a new
        # successful observation producer, and no successful bytes are rewritten.
        try:
            final = close_whole_custody(meter)
        except (OSError,KeyError,TypeError,ValueError,RuntimeError) as cleanup_exc:
            from .causes import source_exception_detail
            detail=source_exception_detail(meter,cleanup_exc)
            meter.setdefault('origin_failures',[]).append(detail)
            final = "held_fd_unretained"
        cause = cause or final
        if blob is not None:
            amount=len(blob)
            blob=None
            release_live(meter, amount)
        release_live(meter, 20480)
    if cause:
        receipt=None
        release_live(meter,receipt_live)
        return None, cause
    meter.setdefault("material_output_receipts", []).append(receipt)
    return receipt, None
