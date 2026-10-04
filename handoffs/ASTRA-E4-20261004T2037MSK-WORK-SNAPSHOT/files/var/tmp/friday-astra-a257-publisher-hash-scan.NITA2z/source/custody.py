"""Real FD/path/body leases. Digest claims are checked against physical bytes."""
import contextlib
import errno
import hashlib
import os
import stat
from common import Refused, DOCUMENT_MAX, INPUT_MAX, SLOTS_MAX, integer, digest, text, sha, domain, OwnedPreimage, exact
from lifetime import OwnedFDs


def identity9(s):
    return [str(x) for x in (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                            s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)]


def components(path):
    text(path, 4096)
    if not path.startswith("/") or path == "/" or any(x in ("", ".", "..") for x in path[1:].split("/")):
        raise Refused("physical_path")
    return path[1:].split("/")


def _attach_journal(book, exc):
    if book.fds:
        exc.retained_journal = book


def open_beneath(root_fd, relative, flags=os.O_RDONLY, mode=0o600, journal=None):
    """Every parent is a held directory; no lexical-only traversal protection.

    The child is journaled before the parent close. An unconfirmed parent close
    keeps both descriptors and does not retry close.
    """
    text(relative, 4096)
    parts = relative.split("/")
    if any(x in ("", ".", "..") for x in parts):
        raise Refused("physical_path")
    if journal is None:raise Refused("path_existing_owner")
    local = False
    book = journal
    parent = book.acquire(os.dup,root_fd,holder="open-beneath-parent")
    try:
        for part in parts[:-1]:
            child = book.acquire(os.open,part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent,holder="open-beneath-parent")
            book.close_one(parent)
            if parent in book.fds:
                raise Refused("parent_close_unconfirmed", detail={"parent_fd": parent, "child_fd": child, "status": "UNKNOWN"})
            parent = child
        opened = book.acquire(os.open,parts[-1],flags|os.O_NOFOLLOW|os.O_CLOEXEC,mode,dir_fd=parent,holder="open-beneath-result")
        book.close_one(parent)
        if parent in book.fds:
            raise Refused("parent_close_unconfirmed", detail={"parent_fd": parent, "opened_fd": opened, "status": "UNKNOWN"})
        if local:
            book.release_returned(opened)
        return opened
    except BaseException as exc:
        if local:
            _attach_journal(book, exc)
        raise


def open_absolute(path, flags=os.O_RDONLY, journal=None):
    parts = components(path)
    if journal is None:raise Refused("path_existing_owner")
    local = False
    book = journal
    root = book.acquire(os.open,"/",os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC,holder="open-absolute-root")
    try:
        opened = open_beneath(root, "/".join(parts), flags, journal=book)
        book.close_one(root)
        if root in book.fds:
            raise Refused("parent_close_unconfirmed", detail={"parent_fd": root, "opened_fd": opened, "status": "UNKNOWN"})
        if local:
            book.release_returned(opened)
        return opened
    except BaseException as exc:
        if local:
            _attach_journal(book, exc)
        raise


class Held:
    def __init__(self, path, pin, meter, maximum=DOCUMENT_MAX, private=False):
        self.path, self.pin, self.meter, self.maximum = path, pin, meter, maximum
        self.fdjournal = OwnedFDs()
        self.fd, self.hold, self.before, self.body_sha, self.closed = -1, None, None, None, False
        self.private = private
        self.retained_by=None

    def __enter__(self):
        needed = max(3,len(components(self.path)) + 1)
        if needed > SLOTS_MAX:
            raise Refused("fd_acquisition")
        self.hold = self.meter.reserve("held-file", slots=needed, reads=self.maximum, allocation=65536+needed*16384)
        self.fdjournal = OwnedFDs(credit=self.hold,meter=self.meter)
        self.meter.track_held_lease(self)
        try:
            named = os.lstat(self.path)
            self.fd = open_absolute(self.path, journal=self.fdjournal)
            opened = os.fstat(self.fd)
            self.before = identity9(opened)
            if not stat.S_ISREG(opened.st_mode) or identity9(named) != self.before:
                raise Refused("held_identity")
            integer(opened.st_size, self.maximum)
            if self.private and (stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1):
                raise Refused("held_private")
            if self.pin is not None:
                if self.before != self.pin["identity9_decimal_strings"]:
                    raise Refused("held_pin")
                if opened.st_size != self.pin["bytes"]: raise Refused("held_size")
            hash_hold = self.meter.reserve("held-acquisition-hash", hash_bytes=opened.st_size, allocation=64)
            try:
                h = hashlib.sha256(); at = 0
                while at < opened.st_size:
                    part = os.pread(self.fd, min(65536, opened.st_size-at), at)
                    if not part: raise Refused("held_short")
                    self.hold.commit(reads=len(part))
                    hash_hold.commit(hash_bytes=len(part))
                    h.update(part); at += len(part)
                self.body_sha = h.hexdigest()
            finally:
                hash_hold.release()
            if self.pin is not None and self.body_sha != self.pin["sha256"]:
                raise Refused("held_sha")
            self.check()
            return self
        except BaseException:
            self.retire()
            raise


    @classmethod
    def from_prepared(cls, store, name, pin, hold):
        """Borrow the already owned prepared FD; no open or fresh reserve."""
        fd, fd_hold = store.prepared_fds[name]
        result = cls.__new__(cls)
        result.path, result.pin, result.meter = pin['path'], pin, store.meter
        result.maximum, result.private = DOCUMENT_MAX, True
        result.fd, result.fdjournal = fd, store.fdjournal
        result.hold, result.before, result.body_sha = hold, pin['identity9_decimal_strings'], pin['sha256']
        result.closed, result.retained_by = False, None
        result.borrowed_prepared = True
        result.check()
        return result

    def read_prepaid(self):
        """Full acquisition/read/hash charged to the same pre-effect credit."""
        self.check()
        size = self.size
        raw = bytearray(size); at = 0
        h = hashlib.sha256()
        while at < size:
            part = os.pread(self.fd, min(65536, size-at), at)
            if not part: raise Refused('held_short', 'terminal')
            self.hold.commit(reads=len(part), hash_bytes=len(part))
            raw[at:at+len(part)] = part; h.update(part); at += len(part)
        self.check()
        if h.hexdigest() != self.body_sha: raise Refused('held_body_drift', 'terminal')
        result = bytes(raw)
        self.meter.own_result(result, self.hold)
        self.meter.remember_digest(result, self.body_sha)
        return result

    @property
    def size(self): return int(self.before[6])

    def check(self):
        if self.fd < 0 or identity9(os.fstat(self.fd)) != self.before or identity9(os.lstat(self.path)) != self.before:
            raise Refused("held_drift", "execution")

    def read(self, maximum=None):
        limit = self.maximum if maximum is None else maximum
        integer(self.size, limit)
        hold = self.meter.reserve("held-body", reads=self.size, allocation=self.size*2+65536)
        try:
            out = bytearray(self.size); at = 0
            while at < self.size:
                part = os.pread(self.fd, min(65536, self.size-at), at)
                if not part: raise Refused("held_short")
                out[at:at+len(part)] = part; at += len(part); hold.commit(reads=len(part))
            self.check()
            body = bytes(out)
            digest = sha(body, self.meter)
            if digest != self.body_sha: raise Refused("held_body_drift")
            self.meter.own_result(body, hold); hold = None
            self.meter.remember_digest(body, digest)
            return body
        finally:
            if hold is not None:hold.release()

    def ref(self, kind, logical):
        self.check()
        return {"kind": kind, "path": logical, "sha256": self.body_sha}

    def pin_now(self):
        self.check()
        return {"path":self.path,"sha256":self.body_sha,"bytes":self.size,
                "identity9_decimal_strings":list(self.before)}

    def retain_until_terminal(self,pid):
        if self.closed or self.fd<0:raise Refused("retained_closed_lease")
        self.retained_by=pid

    def retire(self,confirmed=False):
        faults = []
        if getattr(self,'borrowed_prepared',False):return faults
        if self.retained_by is not None and not confirmed:return faults
        self.retained_by=None
        for held in tuple(self.fdjournal.fds):
            self.fdjournal.close_one(held)
        faults.extend(self.fdjournal.faults)
        unknown = any(row.get("status") == "UNKNOWN" for row in self.fdjournal.meta.values())
        if self.fdjournal.fds or unknown:
            for held, meta in self.fdjournal.meta.items():
                if held in self.fdjournal.fds or meta.get("status") == "UNKNOWN":
                    self.meter.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fd":held,"path":self.path,
                        "credit":meta.get("credit"),"holder":meta.get("holder"),
                        "identity9_decimal_strings":meta.get("identity9_decimal_strings"),"status":meta.get("status")})
            return faults
        self.fd = -1
        if self.hold is not None:
            self.hold.release(); self.hold = None
        self.closed = True
        self.meter.untrack_held_lease(self)
        return faults

    def __exit__(self, typ, exc, tb):
        drift = None
        try: self.check()
        except BaseException as problem: drift = problem
        faults=self.retire()
        for fault in faults:self.meter.note_cleanup(fault)
        if drift is not None and exc is not None:
            self.meter.note_cleanup({"cause":"held_drift_during_original_failure","error":str(drift)})
        if drift is not None and exc is None: raise drift
        return False


class OutputStore:
    """Private prospective artifact tree, immutable completed-file pin registry."""
    def __init__(self, root, meter):
        self.root, self.meter, self.records = root, meter, {}
        self.meter.retain_local_owner(self)
        self.root_fd=-1;self.pending_fds={};self.prepared_fds={}
        self.file_holds={};self.full_bodies={}
        path_slots=len(components(root))+1
        if path_slots<3:path_slots=3
        self.root_hold=meter.reserve("output-root-FD-lifetime",allocation=65536+path_slots*16384,slots=path_slots)
        self.fdjournal = OwnedFDs(credit=self.root_hold,meter=meter)
        self.root_fd = open_absolute(root, os.O_RDONLY|os.O_DIRECTORY, journal=self.fdjournal)
        s = os.fstat(self.root_fd)
        if stat.S_IMODE(s.st_mode) != 0o700:
            refused_fd=self.root_fd
            close_error=None
            try:self.fdjournal.close_one(self.root_fd)
            except BaseException as exc:
                # Preserve the original invalid-root refusal even when the
                # secondary same-owner publication/cleanup route fails.
                from common import error_fact
                close_error=error_fact(exc,"terminal")
            meta=dict(self.fdjournal.meta.get(refused_fd,{}))
            if refused_fd not in self.fdjournal.fds:self.root_fd=-1
            raise Refused("output_root",detail={"fd":refused_fd,"status":meta.get("status"),
                "credit":meta.get("credit"),"holder":meta.get("holder"),
                "identity9_decimal_strings":meta.get("identity9_decimal_strings"),
                "attempted_close":meta.get("attempted_close"),"close_history":meta.get("close_history"),
                "secondary_close_error":close_error})

    def prepare_reserved(self,name):
        """Final capture endpoint/slot acquired before any child effect."""
        if name in self.prepared_fds or name in self.records:raise Refused("output_name")
        hold=self.meter.reserve("prepared-output-FD-lifetime",allocation=65536,slots=2)
        self.file_holds[hold.token]=hold
        fd=-1
        try:
            # Actual native openat+keeper/table birth precedes exposure of fd.
            # Source supplies only preowned metadata, never authority.
            fd=self.fdjournal.acquire_prepared(self.root_fd,name,hold)
            self.prepared_fds[name]=(fd,hold);fd=-1;hold=None
        finally:
            if fd>=0:
                self.fdjournal.close_one(fd)
                if fd in self.fdjournal.fds:
                    self.pending_fds[fd]=hold
                    meta=self.fdjournal.meta.get(fd, {})
                    self.meter.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fd":fd,"credit":meta.get("credit"),"holder":meta.get("holder"),"identity9_decimal_strings":meta.get("identity9_decimal_strings")})
                elif hold is not None:
                    hold.release()
                    self.file_holds.pop(hold.token,None)
            elif hold is not None and not any(self.fdjournal.meta[f]["credit"]==hold.token for f in self.fdjournal.fds):
                hold.release();self.file_holds.pop(hold.token,None)

    def prepare_full_body(self,name,hold):
        if name in self.full_bodies:raise Refused('prepared_body_endpoint_once','terminal')
        result=PreparedFullBody(self,name,hold)
        self.full_bodies[name]=result
        self.meter.retain_local_owner(result)
        return result

    def retire_fd(self,fd,hold):
        if fd not in self.fdjournal.meta:raise Refused("output_FD_missing_owner","terminal")
        self.fdjournal.close_one(fd)
        if fd in self.fdjournal.fds:
            self.pending_fds[fd]=hold
            meta=self.fdjournal.meta.get(fd, {})
            self.meter.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fd":fd,"credit":meta.get("credit"),"holder":meta.get("holder"),"identity9_decimal_strings":meta.get("identity9_decimal_strings")})
            return
        if hold is not None:
            hold.release();self.file_holds.pop(hold.token,None)
        self.pending_fds.pop(fd,None)

    def put(self, name, raw, kind="member", logical=None):
        text(name, 180)
        if "/" in name or name in (".", "..") or name in self.records: raise Refused("output_name")
        if type(raw) is not bytes or len(raw) > DOCUMENT_MAX: raise Refused("output_size")
        hold = self.meter.reserve("artifact-write", reads=len(raw), output=len(raw), hash_bytes=len(raw), allocation=65536, slots=1)
        try:return self.put_reserved(name,raw,kind,logical,hold)
        finally:
            if not any(self.fdjournal.meta[f]["credit"]==hold.token for f in self.fdjournal.fds):hold.release()

    def put_reserved(self,name,raw,kind,logical,hold,preimage=None):
        """Full preimage retention using credit acquired before the effect.

        Retirement/error paths do not re-enter an exhausted budget. Caller owns
        the reservation and its complete finite lifetime, including this write.
        """
        text(name,180)
        if "/" in name or name in (".","..") or name in self.records:raise Refused("output_name")
        if type(raw) is not bytes or len(raw)>DOCUMENT_MAX:raise Refused("output_size")
        fd = -1; at = 0;fd_hold=None
        try:
            if name in self.prepared_fds:fd,fd_hold=self.prepared_fds.pop(name)
            else:
                if hold.slots<1:raise Refused("output_endpoint_not_prepared","terminal")
                fd_hold=hold
                self.file_holds[hold.token]=hold
                fd=self.fdjournal.acquire(os.open,name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=self.root_fd,holder="output-file",credit=fd_hold)
            while at < len(raw):
                n = os.write(fd, memoryview(raw)[at:at+65536])
                if n <= 0: raise Refused("output_short", "execution")
                at += n; hold.commit(reads=n, output=n)
            os.fsync(fd)
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or stat.S_IMODE(s.st_mode) != 0o600 or s.st_size != len(raw):
                raise Refused("output_custody")
            if preimage is not None and type(preimage) is not OwnedPreimage:raise Refused("immutable_preimage")
            full_digest=sha(raw,self.meter,hold) if preimage is None else preimage.for_same_object(raw)
            p = {"path":self.root+"/"+name,"bytes":len(raw),"sha256":full_digest,
                 "identity9_decimal_strings":identity9(s)}
            self.records[name] = {"pin":p,"ref":{"kind":kind,"path":logical or name,"sha256":p["sha256"]}}
            return self.records[name]
        except BaseException:
            # A partial file is retained, with exact written byte count, never
            # silently removed or called an effect-free refusal.
            self.meter.note_partial(self.root+"/"+name, at)
            raise
        finally:
            if fd >= 0:self.retire_fd(fd,fd_hold)
            elif fd_hold is not None and not any(self.fdjournal.meta[f]["credit"]==fd_hold.token for f in self.fdjournal.fds):
                fd_hold.release();self.file_holds.pop(fd_hold.token,None)

    def lease(self, record):
        return Held(record["pin"]["path"], record["pin"], self.meter, private=True)

    def close(self,completion=None):
        if self.full_bodies:
            if completion is None or not completion.completed:return False
            for body in tuple(self.full_bodies.values()):
                if not body.transfer_and_close(completion.receipt):return False
        # Full physical readers still belong to actual retained Source roots.
        # A closed process or descriptive receipt cannot retire these aliases.
        if self.full_bodies:return False
        prior_pending=list(self.pending_fds.items())
        for name,(fd,hold) in list(self.prepared_fds.items()):
            self.retire_fd(fd,hold);self.prepared_fds.pop(name)
        for fd,hold in prior_pending:self.retire_fd(fd,hold)
        self.fdjournal.close()
        if self.root_fd>=0:
            self.fdjournal.close_one(self.root_fd)
            if self.root_fd in self.fdjournal.fds:
                meta=self.fdjournal.meta.get(self.root_fd, {})
                self.meter.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","fd":self.root_fd,"credit":meta.get("credit"),"holder":meta.get("holder"),"identity9_decimal_strings":meta.get("identity9_decimal_strings")})
            else:
                self.root_fd=-1
        if self.root_fd<0 and not self.fdjournal.fds:
            for hold in self.file_holds.values():
                if not hold.closed:hold.release()
            self.file_holds.clear();self.root_hold.release()
            self.meter.retire_local_owner(self)


class PreparedFullBody:
    """Existing OutputStore/Held role, one already prepared complete byte bank.

    Root is the physical writer/reader. A selected child sends full actual bytes
    on its existing owned channel; it never calls an inherited Root meter, opens
    a new pathname, obtains a native grant, or reserves during failure cleanup.
    All limits below are the live original prepaid credit, not new domain caps.
    Unknown full graph/native/prefix costs remain separately unqualified.
    """
    def __init__(self, store, name, hold):
        self.store, self.name, self.hold = store, name, hold
        self.owner_pid = os.getpid()
        self.count = 0; self.transport = 0; self.attempted = False
        self.record = self.held = self.raw = None
        self.alias_snapshots = []
        self.extra_read_passes=0;self.extra_hash_passes=0
        store.prepare_reserved(name)
        self.fd = store.prepared_fds[name][0]

    def _owned(self):
        if os.getpid() != self.owner_pid: raise Refused('prepared_body_Root_owner','terminal')
        if self.hold.closed: raise Refused('prepared_body_retired_credit','terminal')
        if self.store.prepared_fds.get(self.name, (None,))[0] != self.fd:
            raise Refused('prepared_body_endpoint','terminal')

    def binding(self):
        self._owned()
        row = self.hold.meter.pending.get(self.hold.token)
        if row is None: raise Refused('prepared_body_credit','terminal')
        banks=[b for b in self.store.full_bodies.values() if b.hold is self.hold]
        future_reads=sum((2+b.extra_read_passes)*b.count for b in banks if b is not self and not b.attempted)
        future_hash=sum((2+b.extra_hash_passes)*b.count for b in banks if b is not self and not b.attempted)
        live=sum(b.count for b in banks if b is not self)
        return {'endpoint':self.name,'reads':max(0,row['reads']-future_reads),
            'output':row['output'],'hash_bytes':max(0,row['hash_bytes']-future_hash),
            'allocation':max(0,row['allocation']-getattr(self.hold,'selected_value_allocation',0)-6*live-131072*max(0,len(banks)-1))}

    def prospective(self, addition, transport=0):
        self._owned()
        integer(addition, DOCUMENT_MAX); integer(transport, DOCUMENT_MAX)
        if self.attempted: raise Refused('prepared_body_already_sealed','terminal')
        total = self.count + addition
        integer(total,DOCUMENT_MAX)
        row = self.hold.meter.pending.get(self.hold.token)
        banks=[b for b in self.store.full_bodies.values() if b.hold is self.hold]
        other_live=sum(b.count for b in banks if b is not self)
        other_read=sum((2+b.extra_read_passes)*b.count for b in banks if b is not self and not b.attempted)
        other_hash=sum((2+b.extra_hash_passes)*b.count for b in banks if b is not self and not b.attempted)
        # Remaining operations: write+readback+held full reread; complete hashes,
        # simultaneous mutable snapshot/full bank/read copies/retained originals.
        if (row is None or row['reads'] < addition + (2+self.extra_read_passes)*total + other_read + transport
                or row['output'] < addition + transport
                or row['hash_bytes'] < (2+self.extra_hash_passes)*total + other_hash
                or row['allocation'] < getattr(self.hold,'selected_value_allocation',0)+6*(total+other_live) + 131072*max(1,len(banks))):
            raise Refused('prepared_body_existing_credit_before_materialization','terminal')
        return total

    def append(self, raw, transport=0):
        if type(raw) not in (bytes,bytearray):raise Refused('prepared_body_type','terminal')
        self.prospective(len(raw),transport)
        if transport:
            self.hold.commit(reads=transport, output=transport)
            self.transport += transport
        offset = self.count; at = 0
        try:
            while at < len(raw):
                n = os.pwrite(self.fd, memoryview(raw)[at:at+65536], offset+at)
                if n <= 0: raise Refused('output_short','terminal')
                at += n; self.count += n
                self.hold.commit(reads=n, output=n)
        except BaseException:
            self.store.meter.note_partial(self.store.root+'/'+self.name,self.count)
            raise
        return {'offset':offset,'bytes':len(raw)}

    def add(self, value):
        # Kind and identity remain the actual full-arena node/reference relation.
        # Snapshot bytearray before the physical write; do not relabel it bytes.
        self.prospective(len(value))
        raw = value if type(value) is bytes else bytes(value)
        span = self.append(raw)
        self.alias_snapshots.append((value,raw,span))
        return span

    def descriptor(self):
        return {'endpoint':self.name,'bytes':self.count}

    def finish(self):
        self._owned()
        from selected_owned_values import require_selected_mapping_cuts
        require_selected_mapping_cuts(self)
        if self.attempted:raise Refused('prepared_body_seal_once','terminal')
        self.prospective(0)
        self.attempted=True
        os.fsync(self.fd)
        before=identity9(os.fstat(self.fd))
        named=identity9(os.lstat(self.store.root+'/'+self.name))
        if (before!=named or int(before[6])!=self.count or int(before[5])!=1
                or stat.S_IMODE(int(before[2]))!=0o600 or not stat.S_ISREG(int(before[2]))
                or int(before[3])!=os.getuid() or int(before[4])!=os.getgid()):
            raise Refused('prepared_body_custody','terminal')
        h=hashlib.sha256();at=0
        while at<self.count:
            part=os.pread(self.fd,min(65536,self.count-at),at)
            if not part:raise Refused('held_short','terminal')
            self.hold.commit(reads=len(part),hash_bytes=len(part))
            h.update(part);at+=len(part)
        pin={'path':self.store.root+'/'+self.name,'bytes':self.count,'sha256':h.hexdigest(),
            'identity9_decimal_strings':before}
        self.record={'pin':pin,'ref':{'kind':'member','path':self.name,'sha256':pin['sha256']}}
        self.store.records[self.name]=self.record
        self.held=Held.from_prepared(self.store,self.name,pin,self.hold)
        self.raw=self.held.read_prepaid()
        for value,raw,span in self.alias_snapshots:
            if len(value)!=span['bytes'] or memoryview(value)!=memoryview(raw):
                raise Refused('prepared_body_mutable_snapshot_drift','terminal')
            if self.raw[span['offset']:span['offset']+span['bytes']]!=raw:
                raise Refused('prepared_body_full_alias_bytes','terminal')
        # Keep actual original/snapshot aliases and FD credit until real final end.
        return self.descriptor()

    def require(self, descriptor):
        self._owned()
        exact(descriptor,('endpoint','bytes'),'prepared_body_descriptor')
        if (self.record is None or self.held is None or type(self.raw) is not bytes
                or descriptor!=self.descriptor() or len(self.raw)!=self.count):
            raise Refused('prepared_body_full_reader_required','terminal')
        self.held.check()
        return self.raw

    def require_held_binding(self,nodes,pin,raw,descriptor):
        self._owned()
        from observer import full_value_literal
        from selected_owned_values import _own_native,charge_selected_allocation
        original=full_value_literal(nodes,pin);span=nodes[raw][1]
        charge_selected_allocation(self.hold,span['bytes']+131072)
        body=self.require(descriptor);held=body[span['offset']:span['offset']+span['bytes']]
        native=_own_native()
        if native is None or native.own_held_body_check(original,held) is not True:
            raise Refused('prepared_full_original_held_Source_body','terminal')
    def require_runtime_support(self,serial):
        self._owned()
        from selected_owned_values import require_runtime_support
        require_runtime_support(serial)
    def validate_spans(self, nodes, descriptor):
        raw=self.require(descriptor);at=0
        for row in nodes:
            if type(row) is not list or len(row)!=2:raise Refused('prepared_body_node','terminal')
            kind,body=row
            if kind not in ('bytes','bytearray'):continue
            exact(body,('offset','bytes'),'prepared_body_span')
            integer(body['offset'],DOCUMENT_MAX);integer(body['bytes'],DOCUMENT_MAX)
            if body['offset']!=at or body['bytes']>len(raw)-at:
                raise Refused('prepared_body_full_span_order','terminal')
            at+=body['bytes']
        if at!=len(raw):raise Refused('prepared_body_unreferenced_bytes','terminal')
        return raw

    def match_roots(self, descriptor):
        self.require(descriptor)
        from selected_owned_values import require_selected_mapping_cuts
        require_selected_mapping_cuts(self)
        for value,snapshot,span in self.alias_snapshots:
            if len(value)!=span['bytes'] or memoryview(value)!=memoryview(snapshot):
                raise Refused('prepared_body_actual_mutability_drift','terminal')

def _prepared_transfer_and_close(self,receipt):
    """One native accepted full-body transfer; no second seal/late open."""
    if getattr(self,'native_transferred',False):return self.native_closed is True
    receiver=receipt.receiver
    receipt.require_completed(receipt.arena)
    if not self.attempted:
        try:self.finish()
        except BaseException as error:
            # Retain original failed-seal body/row/error; never retry the seal.
            receiver.retain_after_document(error,'prepared-seal:'+self.name)
    packet=receiver.accept_prepared(self,receipt)
    if not receiver.close_prepared(self,receipt):return False
    self.native_transferred=True;self.native_closed=True
    store=self.store
    if store.full_bodies.get(self.name) is not self:raise Refused('native_body_store_identity','terminal')
    store.full_bodies.pop(self.name);store.prepared_fds.pop(self.name,None)
    if self.held is not None:self.held.fd=-1;self.held.closed=True
    self.fd=-1
    # Caller retained original raw bytes/aliases before local retirement.
    store.meter.retire_local_owner(self)
    return True
PreparedFullBody.transfer_and_close=_prepared_transfer_and_close
