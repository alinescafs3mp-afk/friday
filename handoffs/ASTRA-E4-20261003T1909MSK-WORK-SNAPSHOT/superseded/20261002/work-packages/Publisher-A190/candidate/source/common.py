"""Bounded wire/canonical primitives. No authority is inferred from a label."""
import errno
import hashlib
import json
import os
import stat
import selectors
import struct
import time

INPUT_MAX = 2_000_000
DOCUMENT_MAX = 80_000_000
OUTPUT_MAX = 33_554_432
READ_MAX = 40_960_000_000
RAM_MAX = 8_589_934_592
SLOTS_MAX = 128
MEMBERS_MAX = 512
ARTIFACTS_MAX = 350
WORKERS_MAX = 4
WALL_MAX = 4200
REFUSAL_RESERVE = 4096
KINDS = frozenset(("member", "node-archive", "node-shasums256",
    "ubuntu-archive", "ubuntu-inrelease", "ubuntu-packages", "wheel",
    "kernel", "native", "data", "browser", "candidate", "golden",
    "unrar", "custody"))


class Refused(Exception):
    def __init__(self, cause, phase="before-effect", detail=None):
        self.cause, self.phase, self.detail = cause, phase, detail
        super().__init__(cause)


def exact(value, fields, cause="schema"):
    if type(value) is not dict or len(value) > 512 or set(value) != set(fields):
        raise Refused(cause)
    return value


def integer(value, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Refused("integer")
    return value


def text(value, maximum=512, minimum=1):
    if type(value) is not str or not minimum <= len(value) <= maximum or "\0" in value:
        raise Refused("text")
    return value


def digest(value):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise Refused("digest")
    return value


def sha(raw, meter=None, admitted=None):
    """Admit a complete memory-hash pass in the owning meter before hashing.

    A same-object owned digest is reused only for the identical immutable
    bytes object. Kernel rchar is not a substitute. No meter and no admitted
    hold is the pre-owner residue; every in-domain caller passes one.
    """
    if type(raw) is not bytes and type(raw) is not bytearray:
        raise Refused("hash_preimage")
    size = len(raw)
    if size > READ_MAX:
        raise Refused("hash_preimage")
    if meter is not None and type(raw) is bytes:
        cached = meter.cached_digest(raw)
        if cached is not None:
            return cached
    local = None
    if admitted is not None:
        admitted.commit(hash_bytes=size)
    elif meter is not None:
        local = meter.reserve("memory-hash", hash_bytes=size, allocation=size + 64)
        local.commit(hash_bytes=size)
    try:
        digest = hashlib.sha256(raw).hexdigest()
    finally:
        if local is not None:
            local.release()
    if meter is not None and type(raw) is bytes:
        meter.remember_digest(raw, digest)
    return digest


class PreObserverHash:
    """Same-pid hash credit before RootObserver exists. Not a new owner."""
    def __init__(self):
        self.pid = os.getpid()
        self.hash_bytes = 0
        self.read_bytes=0;self.token=0;self.slots=3;self.closed=False
        from lifetime import FDState, FD_HISTORY_ALLOCATION
        self.allocation=INPUT_MAX*260+65536+FD_HISTORY_ALLOCATION
        self.fd_state=FDState(self.token)
        self.journals=[];self.observer=None
    def retain_journal(self,journal):
        if journal not in self.journals:self.journals.append(journal)
    def before_read(self,maximum):
        integer(maximum,READ_MAX)
        if self.observer is not None:
            self.observer.before_physical_sample(maximum)
            return
        if self.read_bytes+self.hash_bytes+maximum>READ_MAX:raise Refused("aggregate_read")
    def read_debit(self,size):
        self.before_read(size);self.read_bytes+=size
        if self.observer is not None:self.observer.explicit_read+=size
    def release(self):
        if self.closed:return True
        if getattr(self,'fd_rows',{}) or any(j.fds for j in self.journals):return False
        if self.observer is not None and self.observer.release(self.token) is not True:return False
        # The descriptor domain retains complete histories until the actual
        # final completion receipt; dropping the sampling list is not enough.
        self.closed=True
        return True
    def commit(self, reads=0, output=0, hash_bytes=0):
        if os.getpid() != self.pid:
            raise Refused("observer_owner")
        size = integer(hash_bytes, READ_MAX)
        if self.observer is not None:self.observer.before_physical_sample(0,additional_hash=size)
        if self.hash_bytes+self.read_bytes > READ_MAX - size:
            raise Refused("aggregate_read")
        self.hash_bytes += size
        if self.observer is not None:self.observer.explicit_hash+=size


class OwnedPreimage:
    """Full strong immutable bytes/digest, computed on an actual prepaid hold."""
    def __init__(self,raw,meter,hold):
        if type(raw) is not bytes:raise Refused("immutable_preimage")
        self.raw=raw
        self.digest=sha(raw,meter,hold)
    def for_same_object(self,raw):
        if raw is not self.raw or type(raw) is not bytes:raise Refused("immutable_preimage_identity")
        return self.digest


def mono():
    return time.monotonic_ns()


def error_fact(exc, phase, delivered=0):
    """Full bounded OS error facts; text is retained separately as raw UTF-8."""
    return {"class": type(exc).__name__, "cause": getattr(exc, "cause", "exception"),
        "errno": getattr(exc, "errno", None), "strerror": getattr(exc, "strerror", None),
        "filename": getattr(exc, "filename", None), "filename2": getattr(exc, "filename2", None),
        "phase": phase, "delivered_bytes": delivered,
        "text": str(exc), "effects_possible": delivered != 0}


def _encoded_string_size(value):
    size = 2
    for ch in value:
        c = ord(ch)
        size += (2 if ch in '"\\\b\f\n\r\t' else 6 if c < 32 or 127 <= c <= 65535
                 else 12 if c > 65535 else 1)
    return size


def encoded_bound(value, maximum=INPUT_MAX, max_depth=24, max_string=INPUT_MAX):
    """Calculate the exact ASCII encoding size before allocating its wire."""
    count, active = 0, set()
    def walk(item, depth):
        nonlocal count
        count += 1
        if count > maximum or depth > max_depth:
            raise Refused("canonical_capacity")
        if type(item) is dict:
            if len(item) > 512 or id(item) in active:
                raise Refused("canonical_capacity")
            active.add(id(item))
            size = 2 + max(0, len(item)-1)
            for k, v in item.items():
                if type(k) is not str or len(k) > max_string:
                    raise Refused("canonical_key")
                size += _encoded_string_size(k) + 1 + walk(v, depth+1)
                if size+1 > maximum: raise Refused("canonical_capacity")
            active.remove(id(item))
            return size
        if type(item) is list:
            if len(item) > 512 or id(item) in active:
                raise Refused("canonical_capacity")
            active.add(id(item))
            size = 2 + max(0, len(item)-1)
            for child in item:
                size += walk(child, depth+1)
                if size+1 > maximum: raise Refused("canonical_capacity")
            active.remove(id(item))
            return size
        if type(item) is str:
            if len(item) > max_string: raise Refused("canonical_string")
            return _encoded_string_size(item)
        if type(item) is bool: return 4 if item else 5
        if item is None: return 4
        if type(item) is int:
            if not -(10**21) <= item <= 10**21: raise Refused("canonical_integer")
            return len(str(item))
        raise Refused("canonical_type")
    size = walk(value, 1)+1
    if size > maximum: raise Refused("canonical_capacity")
    return size


def canonical(value, meter=None, maximum=INPUT_MAX, depth=24):
    n = encoded_bound(value, maximum, depth)
    hold = None if meter is None else meter.reserve("encode", allocation=n*4+1024, reads=n, output=0)
    try:
        raw = (json.dumps(value, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False)+"\n").encode("ascii")
        if len(raw) != n: raise Refused("canonical_size")
        if hold is not None: hold.commit(reads=n)
        if hold is not None:
            meter.own_result(raw,hold);hold=None
        return raw
    finally:
        if hold is not None: hold.release()


def domain(name, value, meter=None, maximum=INPUT_MAX):
    text(name, 128)
    if not name.isascii() or any(c.isspace() for c in name): raise Refused("domain")
    body = canonical(value, meter, maximum)
    try:
        # sha() admits the separate memory-hash pass. It is not a physical read.
        return sha(name.encode("ascii") + b"\0" + body, meter)
    finally:
        if meter is not None: meter.retire_result(body)


def json_preflight(raw, maximum=INPUT_MAX, max_depth=24):
    """Lexical capacity scan precedes json.loads, including string/token bounds."""
    if type(raw) is not bytes or not 1 <= len(raw) <= maximum or not raw.endswith(b"\n"):
        raise Refused("json_size")
    stack, in_string, escaped, string_bytes, nodes = [], False, False, 0, 0
    strings=containers=scalars=colons=commas=0;scalar_open=False
    for c in raw:
        if in_string:
            string_bytes += 1
            if string_bytes > maximum: raise Refused("json_string")
            if escaped: escaped = False
            elif c == 92: escaped = True
            elif c == 34: in_string = False
            elif c < 32: raise Refused("json_string")
            continue
        if c == 34:
            in_string, string_bytes = True, 0
            strings+=1;scalar_open=False
        elif c in (91, 123):
            containers+=1;scalar_open=False
            stack.append([c, 0])
            if len(stack) > max_depth: raise Refused("json_depth")
        elif c in (93, 125):
            scalar_open=False
            if not stack or stack[-1][0] != (91 if c == 93 else 123): raise Refused("json_shape")
            stack.pop()
        elif c == 44 and stack:
            commas+=1;scalar_open=False
            stack[-1][1] += 1
            if stack[-1][1] >= 512: raise Refused("json_items")
        elif c==58:colons+=1;scalar_open=False
        elif c in (9,10,13,32):scalar_open=False
        elif not scalar_open:scalars+=1;scalar_open=True
        nodes += 1
        if nodes > maximum: raise Refused("json_capacity")
    if in_string or escaped or stack: raise Refused("json_shape")
    # Lexically counted BEFORE loads: UTF decode/string overlap, containers,
    # scalar objects, pair-hook tuples/dict entries and list growth. This is a
    # conservative qualified 64-bit CPython bound, not a post-allocation RSS
    # estimate nor a fixed 256*body charge repeated for mostly long strings.
    return len(raw)*8+(strings+containers+scalars)*256+colons*256+commas*64+65536


def parse(raw, meter=None, maximum=INPUT_MAX, depth=24):
    allocation=json_preflight(raw, maximum, depth)
    hold = None if meter is None else meter.reserve("parse", allocation=allocation, reads=len(raw))
    def pairs(rows):
        out = {}
        for k, v in rows:
            if k in out: raise Refused("duplicate_key")
            out[k] = v
        return out
    def nonfinite(_):
        raise Refused("nonfinite")
    try:
        try:
            value = json.loads(raw.decode("ascii"), object_pairs_hook=pairs,
                               parse_constant=nonfinite)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise Refused("json") from exc
        checked=canonical(value,meter,maximum,depth)
        try:
            if checked!=raw:raise Refused("noncanonical")
        finally:
            if meter is not None:meter.retire_result(checked)
        if hold is not None: hold.commit(reads=len(raw))
        if hold is not None:
            meter.own_result(value,hold);hold=None
        return value
    finally:
        if hold is not None: hold.release()


def bounded_tree_paths(root, limit=512, directory_mode=None, files_only=False, refuse_symlinks=False):
    """Refuse before the path past limit is stored. Do not follow links."""
    if type(root) is not str or type(limit) is not int or not 0 <= limit <= MEMBERS_MAX:
        raise Refused("complete_image_inventory")
    if not root.startswith("/") or "//" in root or any(part in ("", ".", "..") for part in root[1:].split("/")):
        raise Refused("physical_path")
    found = []
    stack = [root]
    seen = 0
    while stack:
        directory = stack.pop()
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise Refused("source_directory")
        if directory_mode is not None and stat.S_IMODE(info.st_mode) != directory_mode:
            raise Refused("source_directory")
        with os.scandir(directory) as scanned:
            pending = []
            for entry in scanned:
                seen += 1
                if seen > limit:
                    raise Refused("complete_image_inventory")
                path = entry.path
                if entry.is_symlink():
                    if refuse_symlinks:
                        raise Refused("source_symlink")
                    found.append(path)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                    if files_only:
                        continue
                found.append(path)
            if len(stack) + len(pending) > limit:
                raise Refused("complete_image_inventory")
            stack.extend(pending)
    return found


def bounded_refusal(cause, phase, delivered, cleanup_ok):
    """Preallocated-form refusal: no JSON walk, exhausted-meter entry or user text."""
    allowed = {"before-effect", "preparation", "after-delivery", "execution", "postdelivery", "terminal"}
    p = phase if phase in allowed else "terminal"
    # Cause is a separately retained error preimage; public wire is fixed capacity.
    return (b'{"GO":false,"cause":"root_provider_refused","cleanup_ok":'
        + (b"true" if cleanup_ok else b"false")
        + b',"delivered_bytes":'+str(min(max(delivered,0),OUTPUT_MAX)).encode("ascii")
        + b',"effects_granted":false,"phase":"'+p.encode("ascii")
        + b'","runtime":"NOT_RUN","status":"REFUSED"}\n')


class Frame:
    """Root-created pipes, finite full-frame IO, exact EOF and partial delivery facts."""
    def __init__(self, incoming, outgoing, deadline, meter, maximum=INPUT_MAX, journal=None, owner_hold=None):
        self.incoming, self.outgoing = incoming, outgoing
        self.deadline, self.meter, self.maximum = deadline, meter, maximum
        self.delivered = 0
        self.owner_hold=owner_hold;self.last_prepaid=False
        if journal is None or incoming not in journal.fds or outgoing not in journal.fds:
            raise Refused("frame_existing_owner")
        self.journal=journal
        os.set_blocking(incoming, False); os.set_blocking(outgoing, False)

    def _ready(self, fd, mask):
        left = (self.deadline-mono())/1e9
        if left <= 0: raise Refused("deadline", "after-delivery" if self.delivered else "before-effect")
        with selectors.PollSelector() as s:
            s.register(fd, mask)
            if not s.select(left): raise Refused("deadline", "after-delivery")

    def send_raw(self, raw):
        if type(raw) is not bytes or len(raw) > self.maximum: raise Refused("frame_size")
        hold = self.meter.reserve("pipe-delivery", output=len(raw)+8, allocation=8)
        try:
            for blob in (struct.pack(">Q", len(raw)), raw):
                at = 0
                while at < len(blob):
                    self._ready(self.outgoing, selectors.EVENT_WRITE)
                    try: n = os.write(self.outgoing, memoryview(blob)[at:])
                    except BlockingIOError: continue
                    if n <= 0: raise Refused("pipe_write", "after-delivery")
                    at += n; self.delivered += n; hold.commit(output=n)
        finally:
            hold.release()

    def send(self, value):
        raw=canonical(value,self.meter,self.maximum)
        try:self.send_raw(raw)
        finally:self.meter.retire_result(raw)

    def send_prepaid(self,value):
        if self.owner_hold is None or self.owner_hold.closed:raise Refused("owner_transport_not_preadmitted","terminal")
        raw=canonical(value,maximum=self.maximum)
        for blob in (struct.pack(">Q",len(raw)|(1<<63)),raw):
            at=0
            while at<len(blob):
                self._ready(self.outgoing,selectors.EVENT_WRITE)
                try:n=os.write(self.outgoing,memoryview(blob)[at:])
                except BlockingIOError:continue
                if n<=0:raise Refused("owner_transport_delivery","terminal")
                at+=n;self.delivered+=n;self.owner_hold.commit(output=n)

    def _read_prepaid(self,size):
        if self.owner_hold is None or self.owner_hold.closed or size>self.maximum:raise Refused("owner_transport_not_preadmitted","terminal")
        out=bytearray(size);at=0
        while at<size:
            self._ready(self.incoming,selectors.EVENT_READ)
            try:part=os.read(self.incoming,min(65536,size-at))
            except BlockingIOError:continue
            if not part:raise Refused("owner_transport_eof","terminal")
            out[at:at+len(part)]=part;at+=len(part);self.owner_hold.commit(reads=len(part))
        return bytes(out)

    def _read(self, size):
        hold = self.meter.reserve("pipe-receive", reads=size, allocation=size*2+65536)
        try:
            out = bytearray(size); at = 0
            while at < size:
                self._ready(self.incoming, selectors.EVENT_READ)
                try: part = os.read(self.incoming, min(65536, size-at))
                except BlockingIOError: continue
                if not part: raise Refused("premature_eof", "after-delivery")
                out[at:at+len(part)] = part; at += len(part); hold.commit(reads=len(part))
            body=bytes(out)
            self.meter.own_result(body,hold);hold=None
            return body
        finally:
            if hold is not None:hold.release()

    def receive_raw(self,force_prepaid=False):
        # The fixed header is prepaid before knowing the message kind; this
        # cannot re-enter ordinary admission after the execution deadline.
        header=self._read_prepaid(8) if self.owner_hold is not None else self._read(8)
        try:n=struct.unpack(">Q",header)[0]
        finally:
            if self.owner_hold is None:self.meter.retire_result(header)
        self.last_prepaid=force_prepaid or bool(n&(1<<63));n=n&((1<<63)-1)
        if n > self.maximum: raise Refused("frame_size", "after-delivery")
        return self._read_prepaid(n) if self.last_prepaid else self._read(n)

    def receive(self,force_prepaid=False):
        raw=self.receive_raw(force_prepaid)
        try:return parse(raw,None if self.last_prepaid else self.meter,self.maximum)
        finally:
            if not self.last_prepaid:self.meter.retire_result(raw)

    def close(self):
        faults = []
        for attribute in ("incoming","outgoing"):
            fd=getattr(self,attribute)
            if fd<0:continue
            self.journal.close_one(fd)
            if fd not in self.journal.fds:setattr(self,attribute,-1)
        faults.extend(self.journal.faults)
        return faults
