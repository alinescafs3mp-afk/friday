"""SOL069 inert Source. One bounded full frame; never a prefix/hash body.
Representation limits below are NOT proofs of fit for all required stock inputs.
No filesystem backing, grant, channel, role, clock, or cap is added.
"""
SOL069_EXPANDED_BYTES = 16 * 1048576

def sol069_runs(raw):
    out=bytearray();at=0
    while at<len(raw):
        end=at+1
        while end<len(raw) and raw[end]==raw[at]:end+=1
        # Same raw/RLE choice, without allocating an abandoned last run.
        if len(out)+5>=len(raw):return 'raw',raw
        out.extend((end-at).to_bytes(4,'big'));out.append(raw[at]);at=end
    return 'rle',bytes(out)

def sol069_encode(value,cap,check):
    # Necessary complete-wire lower bound, not allocator/RSS qualification.
    # The fixed empty header is 45 bytes without its root expression.
    # Actual final complete JSON/frame validation remains mandatory.
    lower=53
    check(lower<=cap,'SOL069_FULL_FRAME_FIT_NO_CUT')
    nodes=[];identities={};bodies=[];body_ids={};expanded=0
    def reserve(amount):
        nonlocal lower
        check(lower+amount<=cap,'SOL069_FULL_FRAME_FIT_NO_CUT')
        lower+=amount
    def digits_floor(number):
        return max(1,number.bit_length()//4)
    def reference(index):
        reserve(6+digits_floor(index))
        return ['r',index]
    def body(raw):
        nonlocal expanded
        if raw in body_ids:return body_ids[raw]
        expanded+=len(raw)
        check(expanded<=SOL069_EXPANDED_BYTES,'SOL069_EXPANDED_STORAGE_UNPROVEN_NO_CUT')
        # 112 is the zero-width body descriptor with its complete SHA256.
        # Both raw/rle tags have three characters. Larger integer widths
        # only increase the exact final representation, never this floor.
        reserve(112+(1 if bodies else 0))
        codec,stored=sol069_runs(raw)
        reserve(len(stored))
        index=len(bodies);body_ids[raw]=index
        bodies.append(({'bytes':len(raw),'stored':len(stored),'codec':codec,
            'sha256':hashlib.sha256(raw).hexdigest()},stored))
        return index
    def visit(row):
        kind=type(row)
        if row is None or kind in (bool,int,float):
            if row is None:width=4
            elif kind is bool:width=4 if row else 5
            elif kind is int:width=digits_floor(row)+(1 if row<0 else 0)
            else:width=1
            reserve(6+width)
            return ['v',row]
        check(kind in (str,bytes,bytearray,list,dict),'SOL069_DATA_TYPE')
        key=id(row)
        if key in identities:return reference(identities[key])
        # Empty node spellings are necessary wire floors. Debit before
        # allocating the node, identity entry or its nested item cells.
        reserve({str:23,bytes:25,bytearray:29,list:26,dict:26}[kind]+
            (1 if nodes else 0))
        index=len(nodes);identities[key]=index;node={'kind':kind.__name__};nodes.append(node)
        if kind in (str,bytes,bytearray):
            # A single complete body cannot exceed the ORIGINAL expanded
            # bound, even when later equal bodies are deduplicated. Count
            # UTF8 bytes before encode; exact stock types invoke no formatter.
            check(len(row)<=SOL069_EXPANDED_BYTES,'SOL069_EXPANDED_STORAGE_UNPROVEN_NO_CUT')
            if kind is str:
                width=0
                for char in row:
                    point=ord(char)
                    width+=1 if point<128 else 2 if point<2048 else 3 if point<65536 else 4
                    check(width<=SOL069_EXPANDED_BYTES,'SOL069_EXPANDED_STORAGE_UNPROVEN_NO_CUT')
                raw=row.encode('utf8','surrogatepass')
            else:raw=bytes(row)
            node['body']=body(raw)
        elif kind is list:
            items=[];node['items']=items
            for item in row:
                if items:reserve(1)
                items.append(visit(item))
        else:
            check(all(type(k) is str for k in row),'SOL069_DATA_KEYS')
            items=[];node['items']=items
            for k,v in row.items():
                reserve(3+(1 if items else 0))
                left=visit(k);right=visit(v)
                items.append([left,right])
        return reference(index)
    root=visit(value)
    head=json.dumps({'root':root,'nodes':nodes,'bodies':[r[0] for r in bodies],
        'expanded':expanded},separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')
    check(8+len(head)+sum(len(r[1]) for r in bodies)<=cap,'SOL069_FULL_FRAME_FIT_NO_CUT')
    return b'DS69'+len(head).to_bytes(4,'big')+head+b''.join(r[1] for r in bodies)

def sol069_decode(raw,cap,check,parse):
    physical = type(raw) is bytes and raw[:8] == A201_REF_MAGIC
    raw = a201_resolve_body(raw)
    check(type(raw) is bytes and 8<=len(raw)<=(A201_BODY_CAP if physical else cap) and raw[:4]==b'DS69','SOL069_FULL_FRAME')
    size=int.from_bytes(raw[4:8],'big');check(0<size<=len(raw)-8,'SOL069_HEADER_LENGTH')
    head=parse(raw[8:8+size]);at=8+size
    check(type(head) is dict and set(head)=={'root','nodes','bodies','expanded'} and
        type(head['expanded']) is int and 0<=head['expanded']<=SOL069_EXPANDED_BYTES and
        type(head['nodes']) is list and type(head['bodies']) is list,'SOL069_TYPED_HEADER')
    blobs=[];expanded=0
    for row in head['bodies']:
        check(type(row) is dict and set(row)=={'bytes','stored','codec','sha256'} and
            type(row['bytes']) is type(row['stored']) is int and row['bytes']>=0 and
            0<=row['stored']<=len(raw)-at,'SOL069_COMPLETE_BODY_LENGTH')
        expanded+=row['bytes'];check(expanded<=head['expanded'],'SOL069_EXPANDED_SUM')
        chunk=raw[at:at+row['stored']];at+=row['stored']
        check(row['codec'] in ('raw','rle'),'SOL069_CODEC')
        if row['codec']=='rle':
            check(len(chunk)%5==0,'SOL069_RUN_LENGTH');value=bytearray();previous=None
            for pos in range(0,len(chunk),5):
                count=int.from_bytes(chunk[pos:pos+4],'big');byte=chunk[pos+4]
                check(count>0 and byte!=previous and len(value)+count<=row['bytes'],'SOL069_CANONICAL_RUN')
                value.extend(bytes((byte,))*count);previous=byte
            chunk=bytes(value)
        check(len(chunk)==row['bytes'] and hashlib.sha256(chunk).hexdigest()==row['sha256'],
            'SOL069_COMPLETE_BODY_PREIMAGE_SHA')
        blobs.append(chunk)
    check(at==len(raw) and expanded==head['expanded'],'SOL069_COMPLETE_FRAME_NO_TRAILER')
    values=[]
    for node in head['nodes']:
        check(type(node) is dict and node.get('kind') in ('str','bytes','bytearray','list','dict'),
            'SOL069_NODE_KIND')
        kind=node['kind']
        if kind in ('list','dict'):
            check(set(node)=={'kind','items'} and type(node['items']) is list,'SOL069_CONTAINER')
            values.append([] if kind=='list' else {})
        else:
            check(set(node)=={'kind','body'} and type(node['body']) is int and
                0<=node['body']<len(blobs),'SOL069_BODY_REF')
            value=blobs[node['body']]
            values.append(value.decode('utf8','surrogatepass') if kind=='str' else
                bytearray(value) if kind=='bytearray' else value)
    def reference(row):
        check(type(row) is list and len(row)==2,'SOL069_VALUE_REFERENCE')
        tag,index=row
        if tag=='v':
            check(index is None or type(index) in (bool,int,float),'SOL069_SCALAR');return index
        check(tag=='r' and type(index) is int and 0<=index<len(values),'SOL069_NODE_REFERENCE')
        return values[index]
    for index,node in enumerate(head['nodes']):
        if node['kind']=='list':values[index].extend(reference(row) for row in node['items'])
        elif node['kind']=='dict':
            for pair in node['items']:
                check(type(pair) is list and len(pair)==2,'SOL069_KEY_VALUE')
                key=reference(pair[0]);check(type(key) is str and key not in values[index],
                    'SOL069_DUPLICATE_KEY');values[index][key]=reference(pair[1])
    return reference(head['root'])

def sol069_copy(value,memo=None):
    if value is None or type(value) in (str,bytes,int,bool,float):return value
    if memo is None:memo={}
    key=id(value)
    if key in memo:return memo[key]
    if type(value) is bytearray:
        result=bytearray(value);memo[key]=result;return result
    if type(value) is list:
        result=[];memo[key]=result;result.extend(sol069_copy(v,memo) for v in value);return result
    if type(value) is dict:
        result={};memo[key]=result
        for k,v in value.items():result[k]=sol069_copy(v,memo)
        return result
    raise TypeError('SOL069_DATA_COPY_TYPE')

def sol069_equal(left,right):
    """Type/value equality PLUS a bijection of mutable body identities.
    No receiver grants origin authority by Python object address alone.
    """
    work=[(left,right)];seen=set();forward={};reverse={}
    while work:
        a,b=work.pop()
        if type(a) is not type(b):return False
        if type(a) in (dict,list,bytearray):
            x,y=id(a),id(b)
            if x in forward and forward[x]!=y or y in reverse and reverse[y]!=x:return False
            forward[x]=y;reverse[y]=x
            if (x,y) in seen:continue
            seen.add((x,y))
        if type(a) is dict:
            if set(a)!=set(b):return False
            work.extend((a[k],b[k]) for k in a)
        elif type(a) is list:
            if len(a)!=len(b):return False
            work.extend(zip(a,b))
        elif type(a) is float:
            if struct.pack('!d',a)!=struct.pack('!d',b):return False
        elif a!=b:return False
    return True


PACKET_CAP = 1048576
CAP_PROBE_BYTES = PACKET_CAP + 1
CONSTANT_RUN_STORED = 5
WORKER_EVENT_TOTAL = 131072
WORKER_LENGTH_PREFIX = 4
WORKER_LITERAL_BODY_MIN_FAILURE = 131069

def sol069_worker_literal_exceeds(body_len):
    return WORKER_LENGTH_PREFIX + body_len > WORKER_EVENT_TOTAL

def sol069_literal_frame_exceeds(body_len, header_len, cap):
    return 8 + header_len + body_len > cap

def sol069_preowned_backing(frame):
    """Complete Source frame; physical only in exact admitted native binding.
    A generic same-pid/local fallback NEVER claims outside-owner custody.
    """
    if type(frame) is not bytes:
        raise TypeError("SOL069_PREOWNED_FRAME")
    bound = a201_physical_bound()
    reference = a201_store_body(frame) if bound else None
    return {"backing": frame, "view": memoryview(frame).toreadonly(), "length": len(frame),
            "physical_reference": reference, "physical_frame_before_channel_write": bound,
            "complete_native_error_custody": False, "bound_before_channel_write": True,
            "receiver_accepted": False, "pipe_accepted": False, "receipt_accepted": False,
            "both_accepted": False, "exit_is_handover": False, "eof_is_handover": False,
            "digest_is_handover": False, "pending_is_handover": False}

def sol069_commit_existing(fd, frame, cap, write, pread, pwrite, fstat, isreg):
    """Give the complete frame to an fd the caller already owns.
    A regular file is written at offset 0 and read back.
    A full pipe write still leaves acceptance with that existing reader.
    A short write leaves the unwritten tail uncommitted.
    """
    if type(frame) is not bytes or len(frame) == 0 or len(frame) > cap:
        raise RuntimeError("SOL069_COMMIT_CAP")
    # Generic terminal receivers expect ORIGINAL JSON/packet bytes. Physical
    # shadow is independently preheld; a reference is only sent by the
    # explicit worker event path whose actual decoder consumes it.
    if a201_physical_bound():
        a201_store_body(frame)
    st = fstat(fd)
    if isreg(st.st_mode):
        pwrite(fd, frame, 0)
        held = pread(fd, len(frame), 0)
        st2 = fstat(fd)
        if held != frame or st2.st_size != len(frame):
            raise RuntimeError("SOL069_COMMIT_READBACK")
        return "REGULAR_EXISTING_READBACK"
    sent = 0
    while sent < len(frame):
        count = write(fd, frame[sent:])
        if type(count) is not int or count <= 0:
            raise RuntimeError("SOL069_COMMIT_SHORT")
        sent += count
    return "PIPE_FULL_WRITE_ACCEPTANCE_REMAINS_WITH_EXISTING_READER"


# A201: physical Source-only frame custody. This is a transport/body mechanism,
# NOT acceptance of original native/error objects or a whole resource proof.
A201_BODY_MAGIC = b"FRBOD201"
A201_REF_MAGIC = b"FRREF201"
A201_BODY_CAP = SOL069_EXPANDED_BYTES
A201_BODY_HEADER = 128
A201_BODY_FD = 131
A201_BODY_STATE = {}
A201_BODY_VIEWS = {}
A201_PREFIX = struct.Struct("<8sIIQQ32s32s")
A201_REFERENCE = struct.Struct("<8sIIQQQ32s")
A201_RECORD = struct.Struct("<QQ32s")

def a201_guard(end_ns):
    if type(end_ns) is not int or time.monotonic_ns() >= end_ns:
        raise RuntimeError("A201_ORIGINAL_MINIMUM_END_NO_RENEWAL")

def a201_capsule_binding():
    raw = os.pread(100, 849, 0)
    if len(raw) != 848 or raw[:8] != b"FRA061C1":
        raise RuntimeError("A201_ACTUAL_CAPSULE_BINDING")
    # cap v1: magic + six uint32 + ten uint64; session is its first32 body.
    start_ns, work_ns, hard_ns = struct.unpack_from("<QQQ", raw, 32)
    if not start_ns < work_ns < hard_ns:
        raise RuntimeError("A201_ORIGINAL_CAPSULE_CLOCK")
    a201_guard(hard_ns)
    return raw[112:144], hashlib.sha256(raw).digest(), hard_ns

class A201BodyView:
    def __init__(self, fd, slot, session, capsule_sha, end_ns, capacity=A201_BODY_CAP):
        import mmap
        if type(slot) is not int or not 0 <= slot < 4 or len(session) != 32 or len(capsule_sha) != 32:
            raise RuntimeError("A201_BODY_EXPECTED_BINDING")
        a201_guard(end_ns)
        self.end_ns = end_ns
        st = os.fstat(fd)
        required = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        if not stat.S_ISREG(st.st_mode) or st.st_size != A201_BODY_HEADER + capacity or (
                fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required):
            raise RuntimeError("A201_BODY_PHYSICAL_EXTENT_SEALS")
        self.fd, self.slot, self.session, self.capsule_sha = fd, slot, session, capsule_sha
        self.capacity, self.identity = capacity, (st.st_dev, st.st_ino)
        self.expected = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, capacity, 1, session, capsule_sha)
        self.prefix = os.pread(fd, A201_BODY_HEADER, 0)
        if len(self.prefix) != A201_BODY_HEADER or self.prefix[:96] != self.expected:
            raise RuntimeError("A201_BODY_PREFIX_CORRESPONDENCE")
        # Strong readonly descriptor is owned BEFORE the actual bounded birth.
        # Mapping is bounded to each exact record, not four simultaneous16M maps.
        self.mmap = mmap
        self.reads = 0
        self.actual_body_custody = True
        self.complete_native_error_custody = False
        self.records = []

    def read(self, reference):
        a201_guard(self.end_ns)
        if type(reference) is not bytes or len(reference) != A201_REFERENCE.size:
            raise RuntimeError("A201_REFERENCE_SIZE")
        magic, version, slot, sequence, offset, length, sha = A201_REFERENCE.unpack(reference)
        if magic != A201_REF_MAGIC or version != 1 or slot != self.slot or sequence < 2 or sequence % 2:
            raise RuntimeError("A201_REFERENCE_GENERATION_SLOT_SEQUENCE")
        if not A201_BODY_HEADER <= offset <= A201_BODY_HEADER + self.capacity or not (
                0 < length <= A201_BODY_HEADER + self.capacity - offset):
            raise RuntimeError("A201_REFERENCE_ACTUAL_RANGE")
        before = os.fstat(self.fd)
        if (before.st_dev, before.st_ino) != self.identity or before.st_size != A201_BODY_HEADER + self.capacity:
            raise RuntimeError("A201_BODY_RECEIVER_IDENTITY")
        head = os.pread(self.fd, A201_BODY_HEADER, 0)
        self.reads += len(head)
        if len(head) != A201_BODY_HEADER or head[:96] != self.expected:
            raise RuntimeError("A201_BODY_RECEIVER_PREFIX")
        published, end, poison, retired = struct.unpack_from("<QQQQ", head, 96)
        if published < sequence or published % 2 or poison or not offset + length <= end <= (
                A201_BODY_HEADER + self.capacity):
            raise RuntimeError("A201_BODY_UNCOMMITTED_OR_POISONED")
        base = offset - offset % self.mmap.PAGESIZE
        span = offset - base + length
        view = self.mmap.mmap(self.fd, span, access=self.mmap.ACCESS_READ, offset=base)
        try:
            raw = bytes(view[offset - base:offset - base + length])
            self.reads += len(raw)
            if len(raw) != length or hashlib.sha256(raw).digest() != sha:
                raise RuntimeError("A201_FULL_PHYSICAL_BODY_SHA")
            # Appended records never overwrite a previous original body.
            tail = os.pread(self.fd, A201_BODY_HEADER, 0)
            self.reads += len(tail)
            after = os.fstat(self.fd)
            if len(tail) != A201_BODY_HEADER or tail[:96] != self.expected or (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size):
                raise RuntimeError("A201_BODY_RECEIVER_DRIFT")
            seq2, end2, poison2, _ = struct.unpack_from("<QQQQ", tail, 96)
            if seq2 < sequence or end2 < offset + length or poison2:
                raise RuntimeError("A201_BODY_RECEIVER_LATE_FAILURE")
            a201_guard(self.end_ns)
            # Strong original is held by the actual consumer, not copied into
            # a second unbounded per-view lifetime bank.
            return raw
        finally:
            view.close()

def a201_create_body(slot, session, capsule_sha, end_ns, owners, capacity=A201_BODY_CAP,
                     *, source_uid=None, source_gid=None):
    a201_guard(end_ns)
    # Allocate and attach the original intention BEFORE the native acquisition.
    # A refused intention owns no fd; a returned fd enters this existing slot
    # before any initialization, seal, view or further ledger allocation.
    state = {"fd": None, "slot": slot, "capacity": capacity, "readonly_fd": None,
             "view": None, "end_ns": end_ns, "born": False,
             "complete_native_error_custody": False}
    owners.append(state)
    A201_BODY_STATE.setdefault("constructing", []).append(state)
    fd = os.memfd_create("friday-source-body-a201", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    state["fd"] = fd
    # Existing UID0 caller allocates Source-only backing, but native validator
    # requires the ORIGINAL capsule's Source UID/GID, not caller UID0. The
    # outside readonly view remains independently held by that same caller.
    source_uid = os.getuid() if source_uid is None else source_uid
    source_gid = os.getgid() if source_gid is None else source_gid
    if type(source_uid) is not int or type(source_gid) is not int or min(source_uid, source_gid) < 0:
        raise RuntimeError("A201_SOURCE_BODY_CREDENTIAL_TYPES")
    os.fchown(fd, source_uid, source_gid)
    os.fchmod(fd, 0o600)
    st = os.fstat(fd)
    if (st.st_uid, st.st_gid) != (source_uid, source_gid):
        raise RuntimeError("A201_SOURCE_BODY_CREDENTIAL_CORRESPONDENCE")
    os.ftruncate(fd, A201_BODY_HEADER + capacity)
    header = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, capacity, 1, session, capsule_sha)
    header += struct.pack("<QQQQ", 0, A201_BODY_HEADER, 0, 0)
    if os.pwrite(fd, header, 0) != len(header):
        raise RuntimeError("A201_HEADER_SHORT_INITIALIZATION")
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    readonly = os.open("/proc/self/fd/" + str(fd), os.O_RDONLY | os.O_CLOEXEC)
    state["readonly_fd"] = readonly
    state["view"] = A201BodyView(readonly, slot, session, capsule_sha, end_ns, capacity)
    A201_BODY_VIEWS[slot] = state["view"]
    return state

def a201_writer():
    import mmap
    import sys
    cached = A201_BODY_STATE.get("writer")
    if cached is not None:
        return cached
    # Four literal helper copies can execute in the SAME admitted process.
    # They must share the actual plane cursor/fault latch, not each assume the
    # same fd131 is fresh. This is one private in-process state, not a service,
    # receiver, grant, external artifact, or unbounded lifetime bank.
    shared = getattr(sys, "_friday_sol073_plane_writer", None)
    if shared is not None and shared["owner_pid"] == os.getpid():
        st = os.fstat(A201_BODY_FD)
        if shared["fd"] != A201_BODY_FD or (st.st_dev, st.st_ino) != shared["identity"]:
            raise RuntimeError("A201_SHARED_WRITER_DESCRIPTOR_DRIFT")
        A201_BODY_STATE["writer"] = shared
        return shared
    session, capsule_sha, hard_ns = a201_capsule_binding()
    fd = A201_BODY_FD
    st = os.fstat(fd)
    head = os.pread(fd, A201_BODY_HEADER, 0)
    if len(head) != A201_BODY_HEADER:
        raise RuntimeError("A201_SOURCE_BODY_HEADER")
    magic, version, slot, capacity, generation, actual_session, actual_sha = A201_PREFIX.unpack(head[:96])
    expected = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, A201_BODY_CAP, 1, session, capsule_sha)
    required = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if head[:96] != expected or (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()) or not stat.S_ISREG(st.st_mode) or st.st_size != (
            A201_BODY_HEADER + capacity) or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required:
        raise RuntimeError("A201_SOURCE_BODY_BINDING")
    sequence, end, poison, retired = struct.unpack_from("<QQQQ", head, 96)
    if sequence or end != A201_BODY_HEADER or poison or retired:
        raise RuntimeError("A201_SOURCE_BODY_NOT_FRESH")
    cached = {"fd": fd, "slot": slot, "capacity": capacity, "end": end,
              "sequence": 0, "mapping": None, "mapping_bytes": 0,
              "identity": (st.st_dev, st.st_ino), "mmap": mmap, "end_ns": hard_ns,
              "owner_pid": os.getpid(), "complete_native_error_custody": False, "records": []}
    sys._friday_sol073_plane_writer = cached
    A201_BODY_STATE["writer"] = cached
    return cached

def a201_store_body(frame):
    if type(frame) is not bytes or not frame:
        raise RuntimeError("A201_BODY_FULL_FRAME_REQUIRED")
    state = a201_writer()
    a201_guard(state["end_ns"])
    # A failed attempt is terminal for this actual plane writer. In particular
    # an ordinary exception AFTER either publication word must not let finally
    # re-enter with an old cursor and overwrite a committed original.
    if state.get("publication_fault"):
        raise RuntimeError("A201_PRIOR_PUBLICATION_FAULT_NO_RETRY")
    if state["records"] and state["records"][-1][3] is frame:
        return state["records"][-1][4]
    state["publication_fault"] = True
    record_start = state["end"]
    start = record_start + A201_RECORD.size
    if len(frame) > A201_BODY_HEADER + state["capacity"] - start:
        raise RuntimeError("A201_BODY_CAPACITY_NOT_FIT_NO_CUT")
    need_end = start + len(frame)
    mapping_bytes = (need_end + state["mmap"].PAGESIZE - 1) // state["mmap"].PAGESIZE * state["mmap"].PAGESIZE
    mapping_bytes = min(mapping_bytes, A201_BODY_HEADER + state["capacity"])
    if mapping_bytes > state["mapping_bytes"]:
        old = state["mapping"]
        state["mapping"] = state["mmap"].mmap(state["fd"], mapping_bytes, access=state["mmap"].ACCESS_WRITE)
        state["mapping_bytes"] = mapping_bytes
        if old is not None:
            old.close()
    mapping = state["mapping"]
    sequence = state["sequence"] + 2
    sha = hashlib.sha256(frame).digest()
    record = A201_RECORD.pack(sequence, len(frame), sha)
    reference = A201_REFERENCE.pack(A201_REF_MAGIC, 1, state["slot"], sequence, start, len(frame), sha)
    end_word, sequence_word = struct.pack("<Q", need_end), struct.pack("<Q", sequence)
    # All allocating cursor/ledger/reference operations precede publication.
    # Cursor is monotonically reserved; no rollback and no reuse after fault.
    state["records"].append((sequence, start, len(frame), frame, reference))
    state["end"], state["sequence"] = need_end, sequence
    state["pending_original"] = frame
    mapping[start:need_end] = frame
    if bytes(mapping[start:need_end]) != frame:
        raise RuntimeError("A201_SOURCE_BODY_COPY_MISMATCH")
    mapping[record_start:start] = record
    a201_guard(state["end_ns"])
    mapping[104:112] = end_word
    mapping[96:104] = sequence_word
    # Even if this assignment fails, publication_fault remains fail-closed.
    # No allocating bookkeeping remains after the actual final commit word.
    state["publication_fault"] = False
    return reference

def a201_resolve_body(raw):
    if type(raw) is bytes and raw[:8] == A201_REF_MAGIC:
        if len(raw) != A201_REFERENCE.size:
            raise RuntimeError("A201_BODY_REFERENCE_FRAME")
        slot = A201_REFERENCE.unpack(raw)[2]
        view = A201_BODY_VIEWS.get(slot)
        if view is None:
            session, capsule_sha, hard_ns = a201_capsule_binding()
            # Coordinator sees original readonly worker views; its writable
            # own slot remains131, other source-only descriptors132..134.
            fd = 131 + slot
            readonly = os.open("/proc/self/fd/" + str(fd), os.O_RDONLY | os.O_CLOEXEC)
            state = {"readonly_fd": readonly, "slot": slot, "view": None}
            A201_BODY_STATE.setdefault("receiving", []).append(state)
            view = A201BodyView(readonly, slot, session, capsule_sha, hard_ns)
            state["view"] = view
            A201_BODY_VIEWS[slot] = view
        return view.read(raw)
    return raw

def a201_physical_bound():
    try:
        return os.pread(A201_BODY_FD, 8, 0) == A201_BODY_MAGIC
    except OSError as exc:
        if exc.errno == 9:
            return False
        raise

def a201_encode_cap(original):
    return A201_BODY_CAP if a201_physical_bound() else original

def a201_fork_plane(entry):
    """Select an EXISTING original browser-role plane before the actual fork.
    Legacy non-admitted G1 workflows keep their original descriptor scope.
    No new role, plane, process, end, or resource grant is created here.
    """
    if not a201_physical_bound():
        return None
    paths = ("archives/playwright/chrome-linux64.zip",
             "archives/playwright/chrome-headless-shell-linux64.zip",
             "archives/playwright/ffmpeg-linux.zip")
    path = entry.get("relative_path")
    if path not in paths:
        raise RuntimeError("A201_FORK_EXACT_EXISTING_BROWSER_ROLE")
    slot = paths.index(path) + 1
    fd = A201_BODY_FD + slot
    session, capsule_sha, hard_ns = a201_capsule_binding()
    a201_guard(hard_ns)
    st = os.fstat(fd)
    head = os.pread(fd, A201_BODY_HEADER, 0)
    expected = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, A201_BODY_CAP, 1, session, capsule_sha)
    seals = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if (st.st_uid, st.st_gid) != (os.getuid(), os.getgid()) or not stat.S_ISREG(st.st_mode) or st.st_size != A201_BODY_HEADER + A201_BODY_CAP or (
            fcntl.fcntl(fd, fcntl.F_GET_SEALS) & seals != seals) or len(head) != A201_BODY_HEADER or (
            head[:96] != expected or struct.unpack_from("<QQQQ", head, 96) != (0, A201_BODY_HEADER, 0, 0)):
        raise RuntimeError("A201_FORK_EXISTING_FRESH_PLANE_BINDING")
    return fd

def a201_bind_fork_plane(plane, body, event):
    """Child-only remap; preserve original capsule plus its one Source plane."""
    global A201_BODY_STATE, A201_BODY_VIEWS
    import sys
    if plane is None:
        return (body, event)
    if len({100, A201_BODY_FD, body, event}) != 4 or plane in (100, body, event):
        raise RuntimeError("A201_FORK_DESCRIPTOR_COLLISION")
    os.dup2(plane, A201_BODY_FD, inheritable=False)
    # Fork copies are not ownership of the parent's cache or readonly views.
    A201_BODY_STATE = {}
    A201_BODY_VIEWS = {}
    if hasattr(sys, "_friday_sol073_plane_writer"):
        del sys._friday_sol073_plane_writer
    return (body, event, 100, A201_BODY_FD)

def a201_snapshot_plane(state):
    """Consume committed originals independently of delivery of a reference.
    Called only after confirmed bounded Root retirement; no exit-as-body credit.
    """
    # Bind the existing prefix list to its real owner BEFORE fallible reads.
    # A later refusal must not lose already consumed original record aliases.
    records = []
    state["snapshotted_original_frames"] = records
    state["snapshot_complete"] = False
    view = state["view"]
    a201_guard(view.end_ns)
    head = os.pread(view.fd, A201_BODY_HEADER, 0)
    if len(head) != A201_BODY_HEADER or head[:96] != view.expected:
        raise RuntimeError("A201_FINAL_PLANE_BINDING")
    sequence, end, poison, _ = struct.unpack_from("<QQQQ", head, 96)
    if sequence % 2 or poison or not A201_BODY_HEADER <= end <= A201_BODY_HEADER + view.capacity:
        raise RuntimeError("A201_FINAL_PLANE_UNCONFIRMED")
    offset = A201_BODY_HEADER
    expected_sequence = 2
    # The final sequence word commits a prefix; end can have advanced before
    # an ordinary fault in the final sequence store. Never reinterpret that
    # uncommitted suffix as a committed record or discard older originals.
    while expected_sequence <= sequence:
        a201_guard(view.end_ns)
        record = os.pread(view.fd, A201_RECORD.size, offset)
        view.reads += len(record)
        if len(record) != A201_RECORD.size:
            raise RuntimeError("A201_FINAL_RECORD_HEADER")
        seq, length, sha = A201_RECORD.unpack(record)
        start = offset + A201_RECORD.size
        if seq != expected_sequence or not 0 < length <= end - start:
            raise RuntimeError("A201_FINAL_RECORD_SEQUENCE_RANGE")
        reference = A201_REFERENCE.pack(A201_REF_MAGIC, 1, view.slot, seq, start, length, sha)
        records.append((reference, view.read(reference)))
        offset = start + length
        expected_sequence += 2
    if offset > end or sequence != expected_sequence - 2:
        raise RuntimeError("A201_FINAL_RECORD_FULL_PATHSET")
    # Strong complete ORIGINAL frame bytes and aliases remain on actual caller.
    a201_guard(view.end_ns)
    state["snapshotted_original_frames"] = tuple(records)
    if offset != end:
        state["uncommitted_published_tail"] = os.pread(view.fd, end - offset, offset)
        state["snapshot_complete"] = False
        raise RuntimeError("A201_UNCOMMITTED_TAIL_RETAINED_NO_RETIREMENT")
    state["snapshot_complete"] = True
    return state["snapshotted_original_frames"]

def a201_close_snapshotted_plane(state):
    if state.get("close_attempted"):
        return state.get("closed", False)
    if state.get("snapshot_complete") is not True:
        raise RuntimeError("A201_NO_CLOSE_BEFORE_FULL_RECORD_CUSTODY")
    a201_guard(state["end_ns"])
    state["close_attempted"] = True
    state["close_errors"] = []
    for key in ("readonly_fd", "fd"):
        fd = state.get(key)
        if fd is None:
            continue
        try:
            os.close(fd)
        except BaseException as exc:
            state["close_errors"].append((key, fd, exc))
        else:
            state[key] = None
    state["closed"] = not state["close_errors"]
    if state["closed"]:
        slot = state["slot"]
        if A201_BODY_VIEWS.get(slot) is state.get("view"):
            A201_BODY_VIEWS.pop(slot)
        state["view"] = None
        constructing = A201_BODY_STATE.get("constructing", [])
        # Retire only this exact confirmed state; original complete frames
        # remain in the actual caller receipt. Failed states stay charged.
        for index, pending in enumerate(constructing):
            if pending is state:
                del constructing[index]
                break
    # No native/error body acceptance is minted by frame retirement.
    return state["closed"]

def a201_close_unborn_plane(state):
    """No Source was born: original native FD owner confirms empty backing.
    Initialization failures retain primary/secondary originals in caller state.
    """
    if state.get("born") is not False or state.get("close_attempted"):
        raise RuntimeError("A201_UNBORN_EXACT_OWNERSHIP_REQUIRED")
    a201_guard(state["end_ns"])
    view = state.get("view")
    if view is not None:
        head = os.pread(view.fd, A201_BODY_HEADER, 0)
        if len(head) != A201_BODY_HEADER or head[:96] != view.expected or (
                struct.unpack_from("<QQQQ", head, 96) != (0, A201_BODY_HEADER, 0, 0)):
            raise RuntimeError("A201_UNBORN_NOT_CONFIRMED_EMPTY")
    state["snapshotted_original_frames"] = ()
    state["snapshot_complete"] = True
    return a201_close_snapshotted_plane(state)
