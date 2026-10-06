"""SOURCE ONLY. Ordinary public subset, not full registry control acceptance.

Future execution needs independently approved stock/native Root invocation,
protected runtime, source bytes and capsule pins. This file creates only the
EXISTING four Source body planes in its actual outside caller; it creates no
Root/runtime/admission authority. All Source execution remains NOT_RUN.
"""
import base64
import fcntl
import hashlib
import json
import os
import select
import signal
import stat
import struct
import time

SOURCES=(101,102,103,104,105,106,107,108,109,110,112,113,114,115,116,117,118,119,127)
CASES=("positive","wait_flags_DATA","caller_status_DATA")
PAYLOADS=tuple(("ordinary-owned-a079/%d\n"%i).encode("ascii") for i in range(3))

def need(ok,cause):
    if not ok:raise RuntimeError(cause)

def strict_json(raw):
    def unique(items):
        result={}
        for key,value in items:
            need(key not in result,"A079_DUPLICATE_DATA_KEY");result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique,
        parse_constant=lambda _:(_ for _ in ()).throw(RuntimeError("A079_NONFINITE_DATA")))

# SOL147 ordinary wirev2. Retain EVERY original636-byte row, including zeros.
def sol145_root_row(text):
    need(type(text) is str and 0 < len(text) <= 852 and len(text) % 4 == 0 and
         all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=' for c in text),
         'SOL147_CANONICAL_ROOT_BASE64_BOUND')
    coded = base64.b64decode(text, validate=True)
    need(21 <= len(coded) <= 637 and base64.b64encode(coded).decode('ascii') == text,
         'SOL147_ROOT_BASE64_EXACT')
    if coded[0] == 0:
        need(len(coded) == 637, 'SOL147_RAW_ALL636_NO_SHORT_OR_TRAILING')
        raw = coded[1:]
        sparse_bytes = 21
        for cell in range(159):
            v = struct.unpack_from('<I', raw, 4 * cell)[0]
            if v: sparse_bytes += (v.bit_length() + 6) // 7
        need(sparse_bytes >= 637, 'SOL147_CANONICAL_RAW_ONLY_IF_NOT_LONGER')
        return raw
    need(coded[0] == 1 and len(coded) < 637 and not coded[20] & 128,
         'SOL147_SPARSE_TAG_BOUND_AND_UNUSED159_BIT')
    raw, at = bytearray(636), 21
    for cell in range(159):
        if not coded[1 + cell // 8] & (1 << (cell % 8)): continue
        v, finished = 0, False
        for shift in range(5):
            need(at < len(coded), 'SOL147_ROOT_NO_SHORT_VARINT')
            b = coded[at]; at += 1
            need(shift != 4 or b <= 15, 'SOL147_ROOT_UINT32_NOT_OVERFLOW')
            v |= (b & 127) << (7 * shift)
            if not b & 128:
                need(shift == 0 or v >= (1 << (7 * shift)), 'SOL147_ROOT_NO_OVERLONG_VARINT')
                finished = True; break
        need(finished and v != 0, 'SOL147_ROOT_NONZERO_CANONICAL_PRESENCE')
        struct.pack_into('<I', raw, 4 * cell, v)
    need(at == len(coded), 'SOL147_ROOT_NO_UNJOINED_TRAILING_DATA')
    return bytes(raw)

def sol145_root_decode(value):
    need(type(value) is dict and value.get('schema') == 'friday.sol147.actual-root-ordinary.v2' and
         value.get('version') == 2 and value.get('row_bytes') == 636 and
         value.get('capacity') == 3136 and value.get('request_capacity') == 1568 and
         value.get('codec') == 'sparse159-uleb32-or-raw636-base64.v2' and type(value.get('count')) is int and
         0 <= value['count'] <= 3136 and value.get('overflow') in (0, 1) and
         type(value.get('records')) is list and len(value['records']) == value['count'],
         'SOL145_ORIGINAL512_ROOT_STORAGE_WIRE')
    # Keep complete original strings/bytes in the final object; this pure decoder
    # adds no process/status authority, scope, resource fit or acceptance fields.
    sol143_history_decode(value['native_history'])
    result, requests = [], 0
    for text in value['records']:
        raw = sol145_root_row(text)
        fields = struct.unpack_from('<4I11i5Q', raw)
        kind, phase, stage, status_invalid, error = fields[:5]
        need(1 <= kind <= 10 and status_invalid in (0, 1), 'SOL145_ROOT_ACTUAL_KIND')
        if kind in (1, 8): requests += 1
        if kind == 1:
            receipt = sol143_receipt_decode(raw[100:540])
            need(receipt['complete'] and phase == struct.unpack_from('<I', raw, 584)[0],
                 'SOL145_DURABLE_RECEIVE_AND_ORIGINAL_FRAME')
            if stage == error == 0:
                need(receipt['full'] and receipt['bytes'] == 96 and not receipt['close_errno'],
                     'SOL145_NO_POSITIVE_FROM_LOST_OR_TRUNCATED_DATA')
        if kind == 10 or (kind in (6, 7) and any(raw[540:])):
            op, flags, operand, peer_fd, rc, primary, cleanup_rc, cleanup_errno, acquired, cleanup = struct.unpack_from('<II6iQQ', raw, 540)
            need(op in (1, 2, 3, 4, 5, 7, 8) and flags & 1 and flags <= 7 and
                 primary >= 0 and cleanup_errno >= 0 and not any(raw[588:]) and
                 error == -primary, 'SOL145_ORIGINAL_LOCAL_PRIMITIVE_DATA')
            need((rc >= 0 and primary == 0) or (rc == -1 and primary > 0),
                 'SOL145_ACTUAL_SYSCALL_RESULT_ERRNO_PAIR')
            if flags & 4:
                need(op == 8 and cleanup_rc == rc and cleanup_errno == primary,
                     'SOL145_ONE_ORIGINAL_CLOSE_NOT_RETRY')
            else:
                need(cleanup_rc == cleanup_errno == cleanup == 0, 'SOL145_NO_INVENTED_CLEANUP')
        result.append(raw)
    need(requests <= 1568 and value['overflow'] == 0,
         'SOL145_STORAGE_OVERFLOW_HELD_NOT_COMPLETE')
    return result

# SOL143 ordinary fixed native DATA decoder. Numeric PIDs/fds/status grant no authority.
def sol143_hex(value, size, label):
    need(type(value) is str and len(value) == 2 * size and
         all(c in '0123456789abcdef' for c in value), label)
    return bytes.fromhex(value)

def sol143_receipt_decode(raw):
    need(type(raw) is bytes and len(raw) == 440, 'SOL143_RECEIPT440')
    credentials, rights, closed, flags, close_errno, count = struct.unpack_from('<IIIIii', raw)
    capacity, length, rows, full, reached, recv_errno, ready_result, complete = struct.unpack_from('<IIIIiiii', raw, 24)
    need(capacity in (0, 64) and length <= 64 and rows <= 16 and full in (0, 1) and
         reached in (0, 1) and complete in (0, 1) and recv_errno >= 0 and close_errno >= 0,
         'SOL143_RECEIPT_DOMAINS')
    control = raw[56:120]
    outcomes = [struct.unpack_from('<iiiii', raw, 120 + 20 * i) for i in range(rows)]
    need(not any(raw[120 + 20 * rows:]) and rights == rows and closed <= rows,
         'SOL143_RECEIPT_ALL_RIGHTS_NO_HIDDEN_TAIL')
    failures, orders, successful = [], [], 0
    for fd, attempted, result, error, disposition in outcomes:
        need(fd >= 0 and 0 <= attempted <= 16 and disposition in (1, 2, 3, 4) and
             ((not attempted and result == error == 0 and disposition in (1, 4)) or
              (attempted and ((result == 0 and error == 0 and disposition == 2) or
                              (result == -1 and error > 0 and disposition == 3)))),
             'SOL143_ACTUAL_ONE_CLOSE_OUTCOME')
        if attempted and not error: successful += 1
        if attempted: orders.append(attempted)
        if error: failures.append((attempted, error))
    need(sorted(orders) == list(range(1, len(orders) + 1)), 'SOL143_ONE_CHRONOLOGICAL_CLOSE_PER_RIGHT')
    need(closed == successful and close_errno == (min(failures)[1] if failures else 0),
         'SOL143_FIRST_AND_EACH_CLOSE_ERROR')
    if complete and reached and count >= 0 and full:
        need(not (flags & 8), 'SOL143_CTRUNC_NEVER_FULL')
        at, seen_credentials, fds = 0, 0, []
        while at + 16 <= length:
            size, level, kind = struct.unpack_from('<Qii', control, at)
            need(16 <= size <= length - at, 'SOL143_CMSG_ACTUAL_BOUND')
            body = control[at + 16:at + size]
            if level == 1 and kind == 2 and size == 28: seen_credentials += 1
            elif level == 1 and kind == 1:
                need(len(body) % 4 == 0, 'SOL143_CMSG_RIGHT_WIDTH')
                fds.extend(struct.unpack_from('<i', body, i)[0] for i in range(0, len(body), 4))
            at += (size + 7) // 8 * 8
        need(seen_credentials == credentials and fds == [r[0] for r in outcomes],
             'SOL143_ORIGINAL_CONTROL_RIGHT_OPERAND_ALIAS')
    if complete and not reached:
        need(count == -1 and ready_result < 0 and not length and not rows and not full and not recv_errno,
             'SOL143_PRE_RECV_REFUSAL_NOT_EMPTY_SUCCESS')
    if complete and reached and count < 0:
        need(count == -1 and recv_errno > 0 and not length and not rows and not full,
             'SOL143_RECV_ERRNO_NOT_EOF')
    return dict(credentials=credentials, rights=rights, closed=closed, flags=flags,
                close_errno=close_errno, bytes=count, full=bool(full), reached=bool(reached),
                ready_result=ready_result, complete=bool(complete), outcomes=outcomes)

def sol143_pairs_decode(value):
    need(type(value) is dict and value.get('version') == 1 and value.get('row_bytes') == 72 and
         value.get('capacity') == 512 and type(value.get('count')) is int and
         0 <= value['count'] <= 512 and value.get('overflow') in (0, 1) and
         type(value.get('records')) is list and len(value['records']) == value['count'],
         'SOL143_PAIRED_ERROR_GRAPH')
    for index, text in enumerate(value['records']):
        raw = sol143_hex(text, 72, 'SOL143_PAIR72')
        op, sequence, owner, operand, opened, flags, read_result, oe, re, cr, ce, parse, result, parent, pe, birth = struct.unpack('<IIiiiIq8iQ', raw)
        need(op in (1, 2) and sequence == index + 1 and owner > 0 and flags <= 63 and
             oe >= 0 and re >= 0 and ce >= 0 and pe >= 0 and
             (oe > 0 or re > 0 or ce > 0 or parse < 0), 'SOL143_ORIGINAL_PAIR_FIELDS')
        need((not (flags & 8) and cr == ce == 0) or
             ((flags & 8) and ((cr == 0 and ce == 0) or (cr == -1 and ce > 0))),
             'SOL143_REACHED_CLOSE_NO_INVENTION')
        first = -oe if oe else -re if re else parse if parse == -5 and not (flags & 16) else -ce if ce else parse
        need(result == first and result < 0, 'SOL143_FIRST_READ_PARSE_CLOSE_PRECEDENCE')
    return value

def sol143_history_decode(value):
    need(type(value) is dict and value.get('version') == 1 and value.get('evidence_bytes') == 344 and
         value.get('receipt_bytes') == 440 and value.get('row_bytes') == 792 and
         value.get('capacity') == 16 and type(value.get('count')) is int and
         0 <= value['count'] <= 16 and value.get('overflow') in (0, 1) and
         type(value.get('records')) is list and len(value['records']) == value['count'],
         'SOL143_HISTORY_ACTUAL_SCHEMA')
    sol143_pairs_decode(value['pairs'])
    records = []
    for text in value['records']:
        raw = sol143_hex(text, 792, 'SOL143_FULL_HISTORY_ROW')
        result, complete = struct.unpack_from('<iI', raw, 784)
        need(complete == 1, 'SOL143_UNFINISHED_SLOT_NOT_FULL_HISTORY')
        receipt = sol143_receipt_decode(raw[344:784])
        need(struct.unpack_from('<IIi', raw, 40) ==
             (receipt['rights'], receipt['closed'], receipt['close_errno']),
             'SOL143_HISTORY_RIGHTS_PROJECTION')
        if result == 0:
            need(receipt['complete'] and receipt['full'] and receipt['bytes'] == 96 and
                 receipt['credentials'] == 1 and not receipt['flags'] & (8 | 32) and
                 not receipt['close_errno'], 'SOL143_NO_SUCCESS_FROM_TRUNCATED_OR_INCOMPLETE_HISTORY')
        records.append(raw)
    need(value['overflow'] == 0 and value['pairs']['overflow'] == 0,
         'SOL143_OVERFLOW_HELD_NOT_COMPLETE_ERROR_HISTORY')
    return records

def sol143_native_check(value):
    # Keep the decoded original object and all its aliases; validation adds no fields.
    stack, seen = [(value, None)], set()
    while stack:
        item, inherited = stack.pop()
        if type(item) not in (dict, list, tuple): continue
        key = (id(item), id(inherited))
        if key in seen: continue
        seen.add(key)
        if type(item) in (list, tuple):
            stack.extend((v, inherited) for v in item); continue
        native_trace = all(k in item for k in ('phase', 'primitive_errno', 'rights_close_errno', 'expected', 'observed'))
        native_container = item.get('schema') in ('friday.a091.actual-root-receipt.v1', 'friday.a091.stock-caller-receipt.v1', 'friday.sol145.actual-root-ordinary.v1', 'friday.sol147.actual-root-ordinary.v2')
        if item.get('preinterpreter_rejection') is True or all(k in item for k in ('registry_next_sequence', 'coordinator_kernel_status_known', 'registered_workers')):
            need('root_ordinary_history' in item, 'SOL145_ROOT_ORDINARY_DATA_REQUIRED')
            rows = sol145_root_decode(item['root_ordinary_history'])
            if item.get('preinterpreter_rejection') is True:
                need(rows and struct.unpack_from('<I', rows[-1])[0] == 9 and
                     struct.unpack_from('<i', rows[-1], 16)[0] == item['primitive_errno'],
                     'SOL145_PRE_PREFIX_SAME_ORIGINAL_PRIMARY')
                if item.get('reason') == 'ORDINARY_ROOT_EARLY_PREFIX':
                    for original in rows:
                        if struct.unpack_from('<I', original)[0] != 10: continue
                        operation = struct.unpack_from('<I', original, 540)[0]
                        first = struct.unpack_from('<i', original, 16)[0]
                        if operation != 8 and first < 0:
                            need(item['primitive_errno'] == first, 'SOL145_EARLY_PRIMARY_NOT_CLEANUP_ERROR')
                            break
        if item.get('schema') in ('friday.sol145.actual-root-ordinary.v1', 'friday.sol147.actual-root-ordinary.v2'): sol145_root_decode(item)
        current = item.get('native_history', inherited) if native_trace or native_container else inherited
        if (native_trace or native_container) and 'native_history' in item:
            # The same validated history feeds the trace projection below.
            # Keep the first decode/error ordering; do not parse it twice.
            if native_trace: records = sol143_history_decode(current)
            else: sol143_history_decode(current)
        if native_trace:
            need(current is not None, 'SOL143_NATIVE_HISTORY_REQUIRED_NOT_OLD344_ONLY')
            if 'native_history' not in item:
                records = sol143_history_decode(current)
            if records:
                last = records[-1]
                need(struct.unpack_from('<I', last, 4)[0] == item['phase'], 'SOL143_ACTUAL_LAST_PHASE_JOIN')
                for offset, field in ((152, 'expected'), (248, 'observed')):
                    original = last[offset:offset + 96]
                    if item.get('status_redacted'):
                        original = original[:64] + bytes(4) + original[68:]
                    need(original == sol143_hex(item[field], 96, 'SOL143_LEGACY_PACKET96'),
                         'SOL143_HISTORY_ORIGINAL_VS_PUBLIC_PROJECTION')
            else:
                need(item.get('rights', 0) == item.get('rights_closed', 0) == 0,
                     'SOL143_NO_RIGHT_WITHOUT_RETAINED_RECEIPT')
        if item.get('schema') == 'friday.a091.actual-root-receipt.v1':
            need('native_history' in item, 'SOL143_ROOT_PAIRED_ERRORS_REQUIRED')
            for event in item['events']:
                if event['kind'] != 1: continue
                receipt = sol143_receipt_decode(sol143_hex(event.get('native_receipt_hex'), 440, 'SOL143_ROOT_RECEIPT440'))
                need(receipt['complete'] and all(receipt[actual] == event[projected] for actual, projected in
                     (('credentials', 'credentials'), ('rights', 'rights'), ('closed', 'rights_closed'),
                      ('close_errno', 'rights_close_errno'), ('bytes', 'receive_bytes'), ('flags', 'receive_flags'))),
                     'SOL143_ROOT_FULL_NATIVE_RECEIPT_JOIN')
                if event['stage'] == 0 and event['primitive_errno'] == 0:
                    need(receipt['full'] and receipt['bytes'] == 96 and not receipt['close_errno'],
                         'SOL143_ROOT_NO_INCOMPLETE_POSITIVE')
        stack.extend((v, current) for k, v in item.items() if k != 'native_history')
    return value


def identity(st):
    return (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

def generation(pid):
    fd=os.open("/proc/%d/stat"%pid,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:raw=os.read(fd,4097)
    finally:os.close(fd)
    need(0<len(raw)<=4096 and b")" in raw,"A079_ACTUAL_PROC")
    fields=raw.rsplit(b")",1)[1].split();need(len(fields)>=20,"A079_PROC_FIELDS")
    return int(fields[1]),int(fields[19])

def pidfd_pid(fd):
    read=os.open("/proc/self/fdinfo/%d"%fd,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW)
    try:raw=os.read(read,4097)
    finally:os.close(read)
    need(len(raw)<=4096,"A079_PIDFD_BOUND")
    values=[int(v[4:].strip()) for v in raw.splitlines() if v.startswith(b"Pid:")]
    need(len(values)==1,"A079_ACTUAL_PIDFD");return values[0]

def inputs(bundle,case):
    """bundle=(actual FD map, independently fixed capsule SHA, full source SHA
    expectations). No expected pin is selected from the bytes under test."""
    fds,pin,expected=bundle
    need(case in CASES and set(fds)==set((100,111,120,121,122)+SOURCES) and
        set(expected)==set(SOURCES) and type(pin) is str and len(pin)==64,"A079_FULL_PUBLIC_INPUT_SET")
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        fd=fds[role];st=os.fstat(fd);limit=16777216 if role in (110,111) else 1048576
        need(stat.S_ISREG(st.st_mode) and st.st_uid==st.st_gid==0 and 0<=st.st_size<=limit and
            fcntl.fcntl(fd,fcntl.F_GET_SEALS)==15,"A079_ACTUAL_ROOT_SEALED_INPUT")
        raw=os.pread(fd,limit+1,0);need(len(raw)==st.st_size,"A079_FULL_INPUT_READ")
        sha=hashlib.sha256(raw).hexdigest()
        if role==100:need(sha==pin,"A079_INDEPENDENT_CAPSULE_PIN")
        elif role in SOURCES:need(sha==expected[role],"A079_INDEPENDENT_SOURCE_PIN")
        snapshots[role]=(identity(st),sha);raws[role]=raw
    cap=raws[100]
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,1,1,1000,1000,19),"A079_MODE1_FULL_CAPSULE")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    need(start<=time.monotonic_ns()<work<hard and hard-start==180*10**9 and hard-work==10*10**9,"A079_FIXED_180_10_LIFETIME")
    need(cap[112:144]!=b"\0"*32 and cap[144:176].hex()==snapshots[111][1] and
        cap[176:208].hex()==expected[117] and cap[208:240].hex()==expected[116] and
        all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),"A079_FULL_CAPSULE_SOURCE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,
            outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A079_EXTERNAL_ACTUAL_ROOT_AND_GROUP_BINDING")
    data=strict_json(raws[119])
    need(data=={"schema":"friday.a079.ordinary-owned-native-input.v1","case":case,
        "workers":[{"payload":v.decode("ascii"),"exit":17+i} for i,v in enumerate(PAYLOADS)]},"A079_EXACT_INDEPENDENT_ORDINARY_INPUT_DATA")
    return snapshots,work,hard

def complement(bundle,snapshots):
    fds,_,_=bundle
    for role,(wanted,sha) in snapshots.items():
        limit=16777216 if role in (110,111) else 1048576
        need(identity(os.fstat(fds[role]))==wanted and
            hashlib.sha256(os.pread(fds[role],limit+1,0)).hexdigest()==sha and
            fcntl.fcntl(fds[role],fcntl.F_GET_SEALS)==15,"A079_EXACT_FULL_HELD_COMPLEMENT")

def oracle(outer,case,inner_stdout=None):
    """Expectations are fixed here before the independent observed output."""
    need(outer.get("acceptance_complete") is False and outer.get("body_complete") is False and
        outer.get("coordinator_kernel_status_known") is True and outer.get("borrowed_status_kernel_credit") is False,
        "A079_NO_CALLER_STATUS_KERNEL_CREDIT")
    encoded=outer.get("native_inner_DATA_hex")
    if inner_stdout is None:
        # Preserve the old bounded inline caller; it is not silently disabled.
        need(type(encoded) is str and 0<len(encoded)<=32768,"A079_BOUNDED_ACTUAL_NATIVE_OUTPUT")
        raw=bytes.fromhex(encoded)
    else:
        need(type(inner_stdout) is bytes and 0<len(inner_stdout)<=1048576 and encoded is None,
            "SOL149_EXISTING_FULL_RAW_INNER_ROUTE")
        raw=inner_stdout
    need(raw.endswith(b"\n") and raw.count(b"\n")==1 and hashlib.sha256(raw).hexdigest()==outer["inner_terminal_sha256"],"A079_EXACT_ACTUAL_OUTPUT_BYTES_HASH_FRAME")
    inner=sol143_native_check(strict_json(raw))
    need(inner.get("schema")=="friday.a079.native-public-subset-result.v1" and inner.get("native_registry_only") is True and
        inner.get("case")==case and inner.get("all216")=="NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION" and
        inner.get("full_authoritative_registry_controls")=="SOURCE_INCOMPLETE" and inner.get("acceptance_complete") is False and
        inner.get("current_GO") is False and inner.get("F10_waiver") is False,"A079_SUBSET_NOT_WHOLE_BROWSER")
    positive=case=="positive";rows=inner["ordinary_rows"]
    need(len(rows)==(3 if positive else 1) and outer["registered_workers"]==(3 if positive else 1) and
        outer["reaped_workers"]==(3 if positive else 0) and outer["registry_next_sequence"]==(13 if positive else 4),"A079_FULL_COUNT_AND_SEQUENCE_ORACLE")
    need(outer["state"]==("OUTER_BOUNDED_DRAINED_FINISHED" if positive else "STOP_UNCONFIRMED") and
        outer["reason"]==(None if positive else "COORD_EXIT") and outer["terminal_completion"] is positive and
        outer["uncertainty_sticky"] is (not positive),"A079_EXACT_OUTER_STATE_CAUSE_UNKNOWN")
    need(type(outer["aggregate_raw_RSS_peak_bytes"]) is int and 0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and
        0<outer["raw_self_peak_KiB"]<=65536 and 0<=outer["outer_memory_current"]<=67108864 and
        0<=outer["inner_memory_current"]<=201326592,"A079_ACTUAL_NATIVE_RESOURCE_ORACLE")
    before=inner["session_before"];after=inner["session_after"]
    need(before["uid"]==before["gid"]==1000 and before["session_ready"]==1 and before["creation_poisoned"]==0 and before["next_sequence"]==2 and
        before["owner"]>0 and before["origin"]>0 and before["owner_birth"]>0 and before["origin_birth"]>0 and
        all(before[k]==after[k] for k in ("owner","origin","uid","gid","owner_birth","origin_birth")),"A079_FIXED_ORIGIN_COMPLEMENT")
    for role,row in enumerate(rows):
        initial,final=row["registered"],row["final"]
        need(row["role"]==role and row["payload"].encode("ascii")==PAYLOADS[role] and initial["pid"]>0 and initial["birth"]>0 and
            initial["state"]==2 and initial["wait_observed"]==initial["status_known"]==initial["cleanup_reaped"]==0 and
            all(initial[k]==final[k] for k in ("owner","origin","pid","role","birth","owner_birth","origin_birth")) and
            final["wait_observed"]==final["cleanup_reaped"]==final["handle_closed"]==1 and final["pidfd"]==-1,"A079_REGISTERED_READY_AND_EXACT_OWN_DISPOSAL")
        if positive:
            need(row["exit"]==17+role and final["state"]==3 and final["status_known"]==1 and final["status"]==((17+role)<<8) and
                final["creation_poisoned"]==0,"A079_INDEPENDENT_KERNEL_WAIT_AND_REAP_ACK")
        else:
            need(row["refusal_errno"]==(-22 if case=="wait_flags_DATA" else -1) and row["stop_errno"]==-117 and row["wait_status"] is None and
                final["state"]==4 and final["status_known"]==0 and final["status"]==0 and final["creation_poisoned"]==1,"A079_INERT_CALL_DATA_UNKNOWN_STATUS_ORACLE")
    return inner

# A158 normal held-entry custody; inlined into both actual ordinary drivers.
# The caller supplies only held descriptors and a separately selected pin.
class HeldAuxiliaries:
    def __init__(self,metadata_next=7):
        self.entries=[{"fd":None,"kind":None,"identity9":None,
            "attempted":False,"closed":False,"close_errno":None,"error":None,
            "finalization_error":None} for _ in range(18)]
        self.metadata_next=metadata_next
    def own(self,slot,fd,kind):
        entry=self.entries[slot]
        entry["fd"]=fd;entry["kind"]=kind
        return fd
    def close(self,slot):
        entry=self.entries[slot]
        if entry["fd"] is None:return True
        if entry["attempted"]:return entry["closed"]
        entry["attempted"]=True
        try:os.close(entry["fd"])
        except BaseException as exc:
            entry["error"]=exc;entry["close_errno"]=getattr(exc,"errno",None) or type(exc).__name__
            return False
        entry["closed"]=True
        return True
    def metadata(self,path,*,dir_fd=None,end_ns=None):
        if self.metadata_next>=18:raise RuntimeError("A158_METADATA_OPEN_BOUND")
        slot=self.metadata_next;self.metadata_next+=1
        if end_ns is not None:a201_guard(end_ns)
        fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=dir_fd)
        self.own(slot,fd,"proc_metadata")
        primary=None
        try:
            if end_ns is not None:a201_guard(end_ns)
            self.entries[slot]["identity9"]=[str(v) for v in identity(os.fstat(fd))]
            if end_ns is not None:a201_guard(end_ns)
            raw=os.read(fd,4097)
            if not 0<len(raw)<=4096:raise RuntimeError("A158_METADATA_READ_BOUND")
            return raw
        except BaseException as exc:primary=exc;raise
        finally:
            try:
                if end_ns is not None:a201_guard(end_ns)
                if not self.close(slot) and primary is None:raise self.entries[slot]["error"]
            except BaseException as exc:
                self.entries[slot]["finalization_error"]=exc
                if primary is None:raise
    def generation(self,pid):
        raw=self.metadata("/proc/%d/stat"%pid)
        if b")" not in raw:raise RuntimeError("A158_ACTUAL_PROC_FRAME")
        fields=raw.rsplit(b")",1)[1].split()
        if len(fields)<20:raise RuntimeError("A158_ACTUAL_PROC_FIELDS")
        return int(fields[1]),int(fields[19])
    def pidfd_pid(self,fd):
        raw=self.metadata("/proc/self/fdinfo/%d"%fd)
        values=[int(v[4:].strip()) for v in raw.splitlines() if v.startswith(b"Pid:")]
        if len(values)!=1:raise RuntimeError("A158_ACTUAL_PIDFD_IDENTITY")
        return values[0]
    def cleanup(self):
        for slot,entry in enumerate(self.entries):
            if entry["kind"]!="pidfd":self.close(slot)
    def receipt(self):
        return [dict(entry) for entry in self.entries if entry["fd"] is not None]

def held_error(exc):
    return None if exc is None else type(exc).__name__+":"+str(exc)[:512]

def held_capture(fds,pin,work,hard,*,main_reserve_ns,argpin=None,cleanup_reserve_ns=0,four_streams=False,body_plane_fds=None,body_custody=None):
    # Ledger and fixed custody state exist BEFORE the first real pipe. No PID,
    # raw stream, EOF or wait status is invented for a failed pre-clone entry.
    # The same authorized A118 fd128/129 streams, not a new transport role.
    # Default legacy invocation remains supported; run_public selects all four.
    stream_count=4 if four_streams else 2
    barrier_slot=2*stream_count;pidfd_slot=barrier_slot+2
    aux=HeldAuxiliaries(metadata_next=pidfd_slot+1)
    record={"pid":None,"pidfd":None,"birth":None,"owner":os.getpid(),
        "reaped":False,"status":None,"wait_status":None,"stop_attempted":False,
        "uncertainty_sticky":False,"handle_closed":False,"handle_identity9":None,
        "wait_error":None,"signal_error":None,"acquisition_stage":None}
    pairs=[];buffers=[bytearray() for _ in range(stream_count)];hashers=[hashlib.sha256() for _ in range(stream_count)]
    active=[False]*stream_count;seen=[0]*stream_count;eof=[False]*stream_count;overflow=[False]*stream_count
    identities=[None]*stream_count;final_identities=[None]*stream_count
    read_errors=[None]*stream_count;retention_errors=[None]*stream_count;close_errors=[None]*stream_count
    pid=None;released=False;first_error=None;cleanup_errors=[];stage="FIRST_PIPE"
    if body_custody is not None:
        body_custody.update(launch_record=record,launch_auxiliaries=aux,
            launch_buffers=buffers,launch_read_errors=read_errors,
            launch_retention_errors=retention_errors,launch_close_errors=close_errors,
            launch_cleanup_errors=cleanup_errors)
    def remember(exc):
        nonlocal first_error
        if first_error is None:
            first_error=exc
            if body_custody is not None:body_custody["launch_first_error"]=exc
    def reap():
        if pid is None or record["reaped"] or record["wait_error"] is not None:return
        try:done,status=os.waitpid(pid,os.WNOHANG)
        except BaseException as exc:
            remember(exc);record["wait_error"]=exc;cleanup_errors.append("WAIT:"+held_error(exc));return
        if done==pid:record.update(reaped=True,status=status,wait_status=status,wait_ns=time.monotonic_ns())
    def drain():
        for index in range(stream_count):
            if not active[index]:continue
            fd=aux.entries[2*index]["fd"]
            try:part=os.read(fd,65536)
            except BlockingIOError:continue
            except BaseException as exc:
                remember(exc);read_errors[index]=exc;active[index]=False
                if not aux.close(2*index):close_errors[index]=aux.entries[2*index]["close_errno"]
                continue
            if not part:
                eof[index]=True
                try:final_identities[index]=[str(v) for v in identity(os.fstat(fd))]
                except BaseException as exc:remember(exc);cleanup_errors.append("RAW_IDENTITY:"+held_error(exc))
                if not aux.close(2*index):close_errors[index]=aux.entries[2*index]["close_errno"]
                active[index]=False;continue
            seen[index]+=len(part)
            try:
                hashers[index].update(part)
                room=1048576-len(buffers[index]);buffers[index].extend(part[:room])
                if seen[index]>1048576:overflow[index]=True
            except BaseException as exc:
                remember(exc);retention_errors[index]=exc
                # Keep observing/draining the same pipe without claiming that
                # a failed retained prefix is the original complete stream.
    try:
        # The actual run_public caller now creates and retains these exact
        # canonical Source objects before entering this capture/fork path.
        # The direct legacy interface still creates no body objects itself.
        # Native Root independently validates the same capsule-bound headers.
        if body_plane_fds is not None:
            need(type(body_plane_fds) is tuple and len(body_plane_fds)==4 and
                 all(type(fd) is int and fd>=0 for fd in body_plane_fds) and
                 len(set(body_plane_fds))==4,"SOL149_EXISTING_SOURCE_BODY_FD_SET")
            need(all(stat.S_ISREG(os.fstat(fd).st_mode) for fd in body_plane_fds),
                 "SOL149_EXISTING_SOURCE_BODY_FD_KIND")
        for index,kind in enumerate(("stdout","stderr")+(("inner_stdout","inner_stderr") if four_streams else ())+("barrier",)):
            pair=os.pipe2(os.O_CLOEXEC)
            aux.own(2*index,pair[0],kind+"_reader")
            aux.own(2*index+1,pair[1],kind+"_writer")
            pairs.append(pair)
        stage="PRECLONE_PIPE_METADATA"
        for index in range(stream_count):
            fd=pairs[index][0]
            identities[index]=[str(v) for v in identity(os.fstat(fd))]
            aux.entries[2*index]["identity9"]=identities[index]
            os.set_blocking(fd,False);active[index]=True
        stage="ACTUAL_FORK"
        pid=os.fork()
        if pid==0:
            try:
                for reader,_ in pairs[:stream_count]:os.close(reader)
                os.close(pairs[stream_count][1])
                while time.monotonic_ns()<work:
                    if select.select([pairs[stream_count][0]],[],[],.005)[0]:
                        if os.read(pairs[stream_count][0],1)!=b"A":os._exit(124)
                        break
                else:os._exit(124)
                os.close(pairs[stream_count][0])
                mapping={**fds,1:pairs[0][1],2:pairs[1][1]}
                if four_streams:mapping.update({128:pairs[2][1],129:pairs[3][1]})
                if body_plane_fds is not None:
                    mapping.update({131+i:fd for i,fd in enumerate(body_plane_fds)})
                copies={dst:fcntl.fcntl(src,fcntl.F_DUPFD_CLOEXEC,400) for dst,src in mapping.items()}
                for dst,src in copies.items():os.dup2(src,dst,inheritable=True)
                for src in copies.values():os.close(src)
                attach=os.open("cgroup.procs",os.O_WRONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=120)
                try:
                    value=str(os.getpid()).encode("ascii")
                    if os.write(attach,value)!=len(value):raise RuntimeError("A158_ACTUAL_SELF_ATTACH")
                finally:os.close(attach)
                for name in os.listdir("/proc/self/fd"):
                    fd=int(name)
                    if fd not in mapping:
                        try:os.close(fd)
                        except OSError:pass
                os.execve(110,["friday-approved-native-browser3","--held-a061",pin if argpin is None else argpin],
                    {"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"})
            except BaseException:os._exit(125)
            os._exit(125)
        # No parent endpoint close, allocation, proc or handle setup precedes
        # recording the exact actual fork result in this driver's own custody.
        record["pid"]=pid
        if body_custody is not None:
            body_custody["actual_outer_pid"]=pid
            for state in body_custody["planes"]:state["born"]=True
        stage="PARENT_ENDPOINT_CLOSE"
        for slot in tuple(2*i+1 for i in range(stream_count))+(barrier_slot,):
            if not aux.close(slot):raise RuntimeError("A158_PARENT_ENDPOINT_CLOSE")
        stage="PARENT_PIDFD"
        record["pidfd"]=os.pidfd_open(pid,0);aux.own(pidfd_slot,record["pidfd"],"pidfd")
        record["handle_identity9"]=[str(v) for v in identity(os.fstat(record["pidfd"]))]
        parent,birth=aux.generation(pid);record["birth"]=birth
        if parent!=record["owner"] or aux.pidfd_pid(record["pidfd"])!=pid or aux.generation(pid)!=(parent,birth):
            raise RuntimeError("A158_ACTUAL_DIRECT_PARENT_GENERATION")
        stage="RELEASE"
        if os.write(pairs[stream_count][1],b"A")!=1:raise RuntimeError("A158_ACTUAL_RELEASE")
        released=True
        if not aux.close(barrier_slot+1):raise RuntimeError("A158_ACTUAL_RELEASE_CLOSE")
        stage="CONTINUOUS_DRAIN_REAP"
        while any(active) or not record["reaped"]:
            if time.monotonic_ns()>=hard-main_reserve_ns:raise RuntimeError("A158_ORIGINAL_MAIN_END")
            drain();reap()
            if first_error is not None:raise first_error
            if any(overflow):raise RuntimeError("A158_ORIGINAL_RAW_CAP")
            select.select([pairs[i][0] for i in range(stream_count) if active[i]],[],[],.005)
    except BaseException as exc:
        remember(exc);record["acquisition_stage"]=stage
    finally:
        # EOF on this driver's unreleased barrier permits this exact child to
        # exit even when pidfd/proc setup failed. Signal has no guessed fallback.
        aux.close(barrier_slot+1)
        if pid is not None and pid>0:
            reap()
            if released and not record["reaped"]:
                record["stop_attempted"]=True
                try:
                    handle=record["pidfd"]
                    if handle is None or aux.pidfd_pid(handle)!=pid or aux.generation(pid)!=(record["owner"],record["birth"]) or \
                        [str(v) for v in identity(os.fstat(handle))]!=record["handle_identity9"]:
                        raise RuntimeError("A158_SAME_ACTUAL_OWNED_HANDLE_REQUIRED")
                    signal.pidfd_send_signal(handle,signal.SIGKILL)
                except BaseException as exc:
                    record["signal_error"]=exc;cleanup_errors.append("SIGNAL:"+held_error(exc))
            end=min(hard-cleanup_reserve_ns,time.monotonic_ns()+10**9)
            while (any(active) or not record["reaped"]) and time.monotonic_ns()<end:
                drain();reap()
                try:select.select([pairs[i][0] for i in range(stream_count) if active[i]],[],[],.005)
                except BaseException as exc:cleanup_errors.append("POLL:"+held_error(exc));break
            reap()
        aux.cleanup()
        if record["reaped"] and record["pidfd"] is not None:
            if aux.close(pidfd_slot):record.update(handle_closed=True,pidfd=None,close_ns=time.monotonic_ns())
        if any(not entry["closed"] for entry in aux.receipt()) or pid is not None and not record["reaped"]:
            record["uncertainty_sticky"]=True;cleanup_errors.append("STOP_UNCONFIRMED")
    if pid is None:
        return {"schema":"friday.a158.held-preclone-refusal.v1","accepted":False,"passed":False,
            "state":"STOP_UNCONFIRMED" if cleanup_errors else "REFUSED_BEFORE_CLONE",
            "owned":record,"stdout_stream":None,"stderr_stream":None,"inner_stdout_stream":None,"inner_stderr_stream":None,
            "actual_auxiliary_cleanup":aux.receipt(),"outside_auxiliaries":aux,
            "original_error":first_error,"cleanup_errors":cleanup_errors}
    streams=[]
    for index in range(stream_count):
        raw=None;freeze_error=None
        try:raw=bytes(buffers[index])
        except BaseException as exc:freeze_error=exc;remember(exc);retention_errors[index]=exc
        complete=eof[index] and not overflow[index] and read_errors[index] is None and retention_errors[index] is None and \
            close_errors[index] is None and raw is not None and seen[index]==len(raw)
        if not aux.entries[2*index]["closed"]:close_errors[index]=aux.entries[2*index]["close_errno"] or "UNKNOWN";complete=False
        raw_sha=None
        if raw is not None:
            try:raw_sha=hashlib.sha256(raw).hexdigest()
            except BaseException as exc:remember(exc);retention_errors[index]=exc;complete=False
        streams.append({"cap":1048576,"raw":raw,"retained_on_freeze_error":buffers[index] if freeze_error else None,
            "retained_size":len(buffers[index]),"total_seen":seen[index],"eof":eof[index],"overflow":overflow[index],
            "read_error":read_errors[index],"retention_error":retention_errors[index],"close_errno":close_errors[index],
            "pipe_identity9":identities[index],"pipe_final_identity9":final_identities[index],
            "sha256":raw_sha,"prefix_hex":None if raw is None else raw[:64].hex(),
            "size":len(buffers[index]),"transfer_eof":eof[index],"transfer_overflow":overflow[index],
            "transfer_complete":complete,"observed_sha_complete":read_errors[index] is None,
            "read_errno":None if read_errors[index] is None else getattr(read_errors[index],"errno",None) or "UNKNOWN",
            "observed_sha256":hashers[index].hexdigest(),"hash_only":False,"original_stream_complete":complete,
            "full_original_bounded_raw":complete,"custody_domain":"same_actual_driver_original"})
    return {"schema":"friday.a158.held-raw-custody.v1","accepted":False,"passed":False,
        "state":"STOP_UNCONFIRMED" if cleanup_errors else "REFUSED_SCOPED_RECEIPT",
        "owned":record,"stdout_stream":streams[0],"stderr_stream":streams[1],
        "inner_stdout_stream":streams[2] if four_streams else None,
        "inner_stderr_stream":streams[3] if four_streams else None,
        "wire_route":"existing128_129_four_stream" if four_streams else "legacy_inline",
        "actual_auxiliary_cleanup":aux.receipt(),"outside_auxiliaries":aux,
        "original_error":first_error,"cleanup_errors":cleanup_errors,
        "whole_assignment_RAM_and_implicit_IO":"UNKNOWN_NOT_ZERO_NOT_PROVEN","SourceReady":False,"GO":False}

def held_semantic_refusal(receipt,exc):
    if receipt["original_error"] is None:receipt["original_error"]=exc
    receipt["accepted"]=receipt["passed"]=False
    if receipt["state"]!="STOP_UNCONFIRMED":receipt["state"]="REFUSED_SCOPED_RECEIPT"
    return receipt
def sol149_bind_inner_streams(outer,streams):
    """Join full original producer and same driver raw values before semantics."""
    for received in streams:
        received['full_original_bounded_raw']=received['original_stream_complete']=None
        received['native_original']=None
    transport=outer.get('native_inner_streams')
    need(type(transport) is dict and transport.get('schema')=='friday.a118.native-raw-transport.v1' and
         type(transport.get('streams')) is list and len(transport['streams'])==2,
         'SOL149_EXISTING_NATIVE_FULL_STREAM_PAIR')
    for index,(producer,received) in enumerate(zip(transport['streams'],streams)):
        received['native_original']=producer
        need(type(producer) is dict,'SOL149_NATIVE_ORIGINAL_RECORD')
        if producer.get('eof') is False or producer.get('overflow') is True or producer.get('read_errno',0)!=0 or                 producer.get('read_close_errno',0)!=0 or producer.get('total_seen')!=producer.get('retained_size'):
            received['full_original_bounded_raw']=received['original_stream_complete']=False
        need(producer['eof'] is True and producer['overflow'] is False and
             producer['total_seen']==producer['retained_size'] and producer['read_errno']==producer['read_close_errno']==0,
             'SOL149_ORIGINAL_EOF_FULL_BODY_NOT_JUST_TRANSFER')
        need(producer['stream']==index and producer['cap']==received['cap']==1048576 and
             producer['retained_size']==producer['emitted_bytes']==received['retained_size']==received['size'] and
             producer['sha256']==received['sha256']==received['observed_sha256'] and
             producer['prefix_hex']==received['prefix_hex'], 'SOL149_SAME_FULL_RAW_BYTES')
        for key,actual in (('writer_identity9','pipe_identity9'),('writer_identity9_after','pipe_final_identity9')):
            value=producer[key]
            need(type(value) is list and len(value)==9 and all(type(v) is str and v.isdecimal() for v in value) and
                 value==received[actual], 'SOL149_ORIGINAL_NINE_FIELD_PIPE_IDENTITY')
        need(producer['writer_identity9'][:7]==producer['writer_identity9_after'][:7] and
             producer['write_errno']==producer['close_errno']==0 and producer['writer_closed'] is True and
             producer['close_ns']>0 and received['transfer_eof'] is True and received['transfer_overflow'] is False and
             received['transfer_complete'] is True and received['observed_sha_complete'] is True and
             received['read_error'] is None and received['close_errno'] is None and received['retention_error'] is None,
             'SOL149_BOTH_ORIGINAL_AND_TRANSFER_EOF_CLOSE')
        if index==0:need(received['sha256']==outer['inner_terminal_sha256'],'SOL149_FULL_ORIGINAL_STDOUT_SHA')
        received['full_original_bounded_raw']=received['original_stream_complete']=True

# A201: physical Source-only frame custody. This is a transport/body mechanism,
# NOT acceptance of original native/error objects or a whole resource proof.
A201_BODY_MAGIC = b"FRBOD201"
A201_REF_MAGIC = b"FRREF201"
A201_BODY_CAP = 16 * 1048576
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
    def __init__(self, fd, slot, session, capsule_sha, end_ns, capacity=A201_BODY_CAP,
                 *,source_uid=1000,source_gid=1000):
        import mmap
        if type(slot) is not int or not 0 <= slot < 4 or len(session) != 32 or len(capsule_sha) != 32:
            raise RuntimeError("A201_BODY_EXPECTED_BINDING")
        a201_guard(end_ns)
        self.end_ns = end_ns
        st = os.fstat(fd)
        required = fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        if (st.st_uid,st.st_gid)!=(source_uid,source_gid) or \
                fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE!=os.O_RDONLY or \
                not stat.S_ISREG(st.st_mode) or st.st_size != A201_BODY_HEADER + capacity or (
                fcntl.fcntl(fd, fcntl.F_GET_SEALS) & required != required):
            raise RuntimeError("A201_BODY_PHYSICAL_EXTENT_SEALS")
        self.fd, self.slot, self.session, self.capsule_sha = fd, slot, session, capsule_sha
        self.capacity, self.identity = capacity, (st.st_dev, st.st_ino)
        self.source_identity=(source_uid,source_gid,stat.S_IMODE(st.st_mode))
        self.expected = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, capacity, 1, session, capsule_sha)
        self.prefix = os.pread(fd, A201_BODY_HEADER, 0)
        if len(self.prefix) != A201_BODY_HEADER or self.prefix[:96] != self.expected:
            raise RuntimeError("A201_BODY_PREFIX_CORRESPONDENCE")
        if self.prefix[96:]!=struct.pack("<QQQQ",0,A201_BODY_HEADER,0,0):
            raise RuntimeError("SOL151_ORIGINAL_PREBIRTH_FRESH_VIEW")
        # Strong readonly descriptor is owned BEFORE the actual bounded birth.
        # Mapping is bounded to each exact record, not four simultaneous16M maps.
        self.mmap = mmap
        self.reads = 0
        self.actual_body_custody = True
        self.complete_native_error_custody = False
        self.records = []
        self.pending_record = None

    def read(self, reference):
        # Same-role outside caller owns each acquisition intention, original
        # value and both reached errors BEFORE the next fallible operation.
        entry={"reference":reference,"mapping":None,"mapping_close_attempted":False,
               "mapping_closed":False,"mapping_close_entered":False,"raw":None,"complete":False,
               "original_error":None,"mapping_close_error":None}
        self.pending_record=entry
        self.records.append(entry)
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
        if (before.st_dev, before.st_ino) != self.identity or before.st_size != A201_BODY_HEADER + self.capacity or \
                (before.st_uid,before.st_gid,stat.S_IMODE(before.st_mode))!=self.source_identity or \
                fcntl.fcntl(self.fd,fcntl.F_GET_SEALS)!=7:
            raise RuntimeError("A201_BODY_RECEIVER_IDENTITY")
        head = os.pread(self.fd, A201_BODY_HEADER, 0)
        self.reads += len(head)
        if len(head) != A201_BODY_HEADER or head[:96] != self.expected:
            raise RuntimeError("A201_BODY_RECEIVER_PREFIX")
        published, end, poison, retired = struct.unpack_from("<QQQQ", head, 96)
        if published < sequence or published % 2 or poison or retired or not offset + length <= end <= (
                A201_BODY_HEADER + self.capacity):
            raise RuntimeError("A201_BODY_UNCOMMITTED_OR_POISONED")
        base = offset - offset % self.mmap.PAGESIZE
        span = offset - base + length
        view = self.mmap.mmap(self.fd, span, access=self.mmap.ACCESS_READ, offset=base)
        entry["mapping"]=view
        try:
            raw = view[offset - base:offset - base + length]
            entry["raw"]=raw
            self.reads += len(raw)
            if len(raw) != length or hashlib.sha256(raw).digest() != sha:
                raise RuntimeError("A201_FULL_PHYSICAL_BODY_SHA")
            # Appended records never overwrite a previous original body.
            tail = os.pread(self.fd, A201_BODY_HEADER, 0)
            self.reads += len(tail)
            after = os.fstat(self.fd)
            if len(tail) != A201_BODY_HEADER or tail[:96] != self.expected or (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size) or \
                    (after.st_uid,after.st_gid,stat.S_IMODE(after.st_mode))!=self.source_identity:
                raise RuntimeError("A201_BODY_RECEIVER_DRIFT")
            seq2, end2, poison2, retired2 = struct.unpack_from("<QQQQ", tail, 96)
            if seq2 < sequence or seq2 % 2 or end2 < offset + length or poison2 or retired2:
                raise RuntimeError("A201_BODY_RECEIVER_LATE_FAILURE")
            a201_guard(self.end_ns)
            # Strong original is held by the actual consumer, not copied into
            # a second unbounded per-view lifetime bank.
            entry["complete"]=True
        except BaseException as exc:
            entry["original_error"]=exc
        finally:
            entry["mapping_close_attempted"]=True
            try:
                a201_guard(self.end_ns)
                entry["mapping_close_entered"]=True
                view.close()
            except BaseException as exc:entry["mapping_close_error"]=exc
            else:
                entry["mapping_closed"]=True
                entry["mapping"]=None
        if entry["original_error"] is not None:raise entry["original_error"]
        if entry["mapping_close_error"] is not None:raise entry["mapping_close_error"]
        return raw

def a201_create_body(slot, session, capsule_sha, end_ns, owners, capacity=A201_BODY_CAP,
                     *, source_uid=None, source_gid=None,prebirth_end_ns=None):
    a201_guard(end_ns)
    prebirth_end_ns=end_ns if prebirth_end_ns is None else min(end_ns,prebirth_end_ns)
    a201_guard(prebirth_end_ns)
    # Allocate and attach the original intention BEFORE the native acquisition.
    # A refused intention owns no fd; a returned fd enters this existing slot
    # before any initialization, seal, view or further ledger allocation.
    state = {"fd": None, "slot": slot, "capacity": capacity, "readonly_fd": None,
             "view": None, "end_ns": end_ns, "born": False,
             "native_fd_returned":False,"initial_header":None,"original_error":None,
             "acquisition_phase":"INTENTION","header_write_result":None,
             "initial_stat":None,"credential_stat":None,"unborn_header":None,"unborn_stat":None,
             "readonly_fd_close_attempted":False,"fd_close_attempted":False,
             "readonly_fd_close_entered":False,"fd_close_entered":False,
             "readonly_fd_close_error":None,"fd_close_error":None,
             "readonly_fd_closed":False,"fd_closed":False,
             "close_attempted":False,"closed":False,"close_errors":[None,None],
             "snapshot_complete":False,"snapshot_header":None,"snapshot_final_header":None,
             "snapshot_error":None,"consumption_error":None,"retirement_error":None,
             "record_headers":[],"snapshotted_original_frames":None,
             "decoded_originals":[],"pending_decoded_original":None,
             "complete_native_error_custody": False}
    owners.append(state)
    A201_BODY_STATE.setdefault("constructing", []).append(state)
    state["acquisition_phase"]="MEMFD"
    a201_guard(prebirth_end_ns)
    fd = os.memfd_create("friday-source-body-a201", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    state["fd"] = fd
    state["native_fd_returned"] = True
    # Existing UID0 caller allocates Source-only backing, but native validator
    # requires the ORIGINAL capsule's Source UID/GID, not caller UID0. The
    # outside readonly view remains independently held by that same caller.
    source_uid = os.getuid() if source_uid is None else source_uid
    source_gid = os.getgid() if source_gid is None else source_gid
    if type(source_uid) is not int or type(source_gid) is not int or min(source_uid, source_gid) < 0:
        raise RuntimeError("A201_SOURCE_BODY_CREDENTIAL_TYPES")
    a201_guard(prebirth_end_ns)
    state["initial_stat"]=os.fstat(fd)
    state["acquisition_phase"]="SOURCE_OWNERSHIP"
    a201_guard(prebirth_end_ns)
    os.fchown(fd, source_uid, source_gid)
    state["acquisition_phase"]="SOURCE_MODE"
    a201_guard(prebirth_end_ns)
    os.fchmod(fd, 0o600)
    a201_guard(prebirth_end_ns)
    st = os.fstat(fd)
    state["credential_stat"]=st
    if (st.st_uid, st.st_gid) != (source_uid, source_gid):
        raise RuntimeError("A201_SOURCE_BODY_CREDENTIAL_CORRESPONDENCE")
    state["acquisition_phase"]="FIXED_EXTENT"
    a201_guard(prebirth_end_ns)
    os.ftruncate(fd, A201_BODY_HEADER + capacity)
    state["acquisition_phase"]="HEADER"
    header = A201_PREFIX.pack(A201_BODY_MAGIC, 1, slot, capacity, 1, session, capsule_sha)
    header += struct.pack("<QQQQ", 0, A201_BODY_HEADER, 0, 0)
    state["initial_header"] = header
    a201_guard(prebirth_end_ns)
    written=os.pwrite(fd, header, 0)
    state["header_write_result"]=written
    if written != len(header):
        raise RuntimeError("A201_HEADER_SHORT_INITIALIZATION")
    state["acquisition_phase"]="SEALS"
    a201_guard(prebirth_end_ns)
    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    state["acquisition_phase"]="OUTSIDE_READONLY_FD"
    a201_guard(prebirth_end_ns)
    readonly = os.open("/proc/self/fd/" + str(fd), os.O_RDONLY | os.O_CLOEXEC)
    state["readonly_fd"] = readonly
    state["acquisition_phase"]="OUTSIDE_VIEW"
    a201_guard(prebirth_end_ns)
    state["view"] = A201BodyView(readonly, slot, session, capsule_sha, end_ns, capacity,
        source_uid=source_uid,source_gid=source_gid)
    if state["view"].identity!=(st.st_dev,st.st_ino):
        raise RuntimeError("SOL151_SAME_ORIGINAL_WRITER_AND_OUTSIDE_VIEW")
    A201_BODY_VIEWS[slot] = state["view"]
    state["acquisition_phase"]="PREPARED"
    a201_guard(prebirth_end_ns)
    return state

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
    actual=os.fstat(view.fd)
    if (actual.st_dev,actual.st_ino)!=view.identity or actual.st_size!=A201_BODY_HEADER+view.capacity or \
            (actual.st_uid,actual.st_gid,stat.S_IMODE(actual.st_mode))!=view.source_identity or \
            fcntl.fcntl(view.fd,fcntl.F_GET_SEALS)!=7:
        raise RuntimeError("SOL151_FINAL_BODY_IDENTITY_CREDENTIAL_SEAL_DRIFT")
    head = os.pread(view.fd, A201_BODY_HEADER, 0)
    state["snapshot_header"]=head
    if len(head) != A201_BODY_HEADER or head[:96] != view.expected:
        raise RuntimeError("A201_FINAL_PLANE_BINDING")
    sequence, end, poison, retired = struct.unpack_from("<QQQQ", head, 96)
    if sequence % 2 or poison or retired or not A201_BODY_HEADER <= end <= A201_BODY_HEADER + view.capacity:
        raise RuntimeError("A201_FINAL_PLANE_UNCONFIRMED")
    offset = A201_BODY_HEADER
    expected_sequence = 2
    # The final sequence word commits a prefix; end can have advanced before
    # an ordinary fault in the final sequence store. Never reinterpret that
    # uncommitted suffix as a committed record or discard older originals.
    while expected_sequence <= sequence:
        a201_guard(view.end_ns)
        record = os.pread(view.fd, A201_RECORD.size, offset)
        state["record_headers"].append(record)
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
    # No write seal is invented: original mandatory SEAL_SEAL forbids adding
    # F_SEAL_WRITE. Quiescence is checked by the caller before this read path.
    tail=os.pread(view.fd,A201_BODY_HEADER,0)
    view.reads+=len(tail)
    state["snapshot_final_header"]=tail
    if tail!=head:raise RuntimeError("SOL151_FINAL_BODY_HEADER_DRIFT")
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
    view=state.get("view")
    if view is not None and any(not r["mapping_closed"] for r in view.records):
        raise RuntimeError("SOL151_BODY_MAPPING_CLOSE_UNCONFIRMED")
    state["close_attempted"] = True
    for index,key in enumerate(("readonly_fd", "fd")):
        fd = state.get(key)
        if fd is None:
            continue
        # A failed close is UNKNOWN, not a known-live reusable descriptor.
        # Store both fixed error slots before any fallible list bookkeeping.
        state[key+"_close_attempted"]=True
        try:
            a201_guard(state["end_ns"])
            state[key+"_close_entered"]=True
            os.close(fd)
        except BaseException as exc:
            state[key+"_close_error"]=exc
            state["close_errors"][index]=exc
        else:
            state[key] = None
            state[key+"_closed"]=True
    state["closed"] = not any(state["close_errors"])
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
    if state["fd"] is not None:
        state["unborn_stat"]=os.fstat(state["fd"])
        state["unborn_header"]=os.pread(state["fd"],A201_BODY_HEADER,0)
    if view is not None:
        head = os.pread(view.fd, A201_BODY_HEADER, 0)
        if len(head) != A201_BODY_HEADER or head[:96] != view.expected or (
                struct.unpack_from("<QQQQ", head, 96) != (0, A201_BODY_HEADER, 0, 0)):
            raise RuntimeError("A201_UNBORN_NOT_CONFIRMED_EMPTY")
    state["snapshotted_original_frames"] = ()
    state["snapshot_complete"] = True
    return a201_close_snapshotted_plane(state)


SOL069_EXPANDED_BYTES = 16 * 1048576

def sol069_decode(raw,cap,check,parse,*,node_owner=None):
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
    if node_owner is not None:
        check(type(node_owner) is dict and 'decoded_nodes' in node_owner and
            node_owner['decoded_nodes'] is None,'SOL151_FRESH_DECODE_NODE_OWNER')
    values=[]
    # Optional SAME table custody, not a graph copy. The canonical callers
    # omit this argument and still receive the original decoded root.
    # Capture before edges exist, including nodes unreachable from that root.
    if node_owner is not None:node_owner['decoded_nodes']=values
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


SOL151_A079_CUSTODY=None

def sol151_remember(custody,origin):
    custody["pending_original_error"]=origin
    if custody["first_error"] is None:custody["first_error"]=origin
    if custody["error_ledger_error"] is not None:return False
    try:custody["original_errors"].append(origin)
    except BaseException as exc:
        custody["error_ledger_error"]=exc
        return False
    return True

def sol151_writer_end(custody,fds):
    """Actual outer kernel end PLUS original hierarchical cgroups, not exit-only.
    This is conditional on the original qualified Root containment contract;
    it creates no Source admission or new outside observer/ownership role.
    """
    record=custody["launch_record"];aux=custody["launch_auxiliaries"]
    need(record is not None and record["pid"]==custody["actual_outer_pid"] and
         record["owner"]==custody["owner"]==os.getpid() and record["reaped"] is True and
         type(record["wait_status"]) is int,"SOL151_ACTUAL_ROOT_END_REQUIRED")
    cap=custody["capsule"]
    for row,role,offset in zip(custody["writer_end_evidence"],(120,121),(80,96)):
        a201_guard(custody["hard_ns"])
        wanted=struct.unpack_from("<QQ",cap,offset)
        before=os.fstat(fds[role]);row["group_identity9"]=identity(before)
        need(stat.S_ISDIR(before.st_mode) and before.st_uid==before.st_gid==0 and
             (before.st_dev,before.st_ino)==wanted,"SOL151_SAME_ORIGINAL_GROUP_IDENTITY")
        raw=aux.metadata("cgroup.events",dir_fd=fds[role],end_ns=custody["hard_ns"])
        row["raw"]=raw
        after=os.fstat(fds[role]);row["group_final_identity9"]=identity(after)
        need(identity(before)==identity(after) and raw==b"populated 0\nfrozen 0\n",
             "SOL151_SOURCE_WRITERS_NOT_CONFIRMED_ENDED")
        row["populated_zero"]=True
    a201_guard(custody["hard_ns"])
    custody["writer_end_confirmed"]=True

def a201_resolve_body(raw):
    # Resolve only the original already-snapshotted complete bytes held by THIS
    # existing outside caller. Do not open a new FD, map again, or substitute a
    # digest/reference for a missing body. Aliases refer to the same raw object.
    if type(raw) is bytes and raw[:8]==A201_REF_MAGIC:
        need(len(raw)==A201_REFERENCE.size,"SOL151_ACTUAL_BODY_REFERENCE_SIZE")
        fields=A201_REFERENCE.unpack(raw)
        slot,sequence=fields[2],fields[3]
        custody=SOL151_A079_CUSTODY
        need(custody is not None and custody["owner"]==os.getpid() and
             custody["writer_end_confirmed"] and 0<=slot<4,
             "SOL151_SAME_ACTUAL_OUTSIDE_BODY_OWNER")
        state=custody["planes"][slot]
        need(state["slot"]==slot and state["snapshot_complete"],"SOL151_COMPLETE_CURRENT_BODY_REQUIRED")
        # The actual snapshot producer accepts ONLY the contiguous even
        # sequence 2,4,... and appends exactly once per fully read record.
        # Reuse that existing tuple's index; no second table/body copy or
        # repeated scan of all prior originals. Sequence selects a candidate,
        # not authority: require the SAME complete original reference bytes.
        frames=state["snapshotted_original_frames"]
        if sequence>=2 and sequence%2==0:
            index=sequence//2-1
            if index<len(frames):
                reference,original=frames[index]
                if raw==reference:return original
        raise RuntimeError("SOL151_REFERENCE_WITHOUT_ORIGINAL_PREFIX")
    return raw

def sol151_consume_planes(custody):
    for state in custody["planes"]:
        if not state["snapshot_complete"]:continue
        try:
            for reference,original in state["snapshotted_original_frames"]:
                a201_guard(custody["hard_ns"])
                entry={"reference":reference,"original":original,"decoded":None,"original_error":None,
                       "decoded_nodes":None,"decoded_retired":False}
                state["pending_decoded_original"]=entry
                state["decoded_originals"].append(entry)
                # Original non-codec records stay full raw; they are not
                # certified as native/error objects merely by this snapshot.
                if original[:4]==b"DS69":
                    entry["decoded"]=sol069_decode(reference,1048576,need,strict_json,node_owner=entry)
                    sol143_native_check(entry["decoded"])
                    # All local readers of this private graph succeeded. Its
                    # complete DS69 bytes still retain every value and alias.
                    # Break ALL decoder-owned container edges, including
                    # unreachable cycles, before dropping the SAME node table.
                    # No collector, error/traceback clearing or failed-decode
                    # retirement: failures retain their exact acquired prefix.
                    a201_guard(custody["hard_ns"])
                    for transient in entry["decoded_nodes"]:
                        a201_guard(custody["hard_ns"])
                        if type(transient) is list or type(transient) is dict:
                            transient.clear()
                    transient=None
                    a201_guard(custody["hard_ns"])
                    entry["decoded"]=None
                    entry["decoded_nodes"].clear()
                    entry["decoded_nodes"]=None
                    a201_guard(custody["hard_ns"])
                    entry["decoded_retired"]=True
        except BaseException as exc:
            state["consumption_error"]=exc
            if state["pending_decoded_original"] is not None:
                state["pending_decoded_original"]["original_error"]=exc
            sol151_remember(custody,exc)

def sol151_retire(custody,unborn=False):
    for state in custody["planes"]:
        # A failed snapshot leaves its complete acquired prefix/readonly view/
        # original FD/mapping strong and charged. Never retry an unknown close.
        if state["close_attempted"] or not unborn and not state["snapshot_complete"]:continue
        try:
            closed=a201_close_unborn_plane(state) if unborn else a201_close_snapshotted_plane(state)
            for error in state["close_errors"]:
                if error is not None:sol151_remember(custody,error)
            need(closed,"SOL151_ORIGINAL_BODY_CLOSE_UNCONFIRMED")
        except BaseException as exc:
            state["retirement_error"]=exc
            sol151_remember(custody,exc)
    custody["retirement_complete"]=all(state["closed"] for state in custody["planes"])

def run_public(bundle,case,*,body_plane_fds=None):
    global SOL151_A079_CUSTODY
    snapshots,work,hard=inputs(bundle,case);fds,pin,_=bundle
    need(SOL151_A079_CUSTODY is None or SOL151_A079_CUSTODY["retirement_complete"],
         "SOL151_PRIOR_UNCONFIRMED_BODY_OWNER_NO_REENTRY")
    # Preserve the parameter for explicit refusal: bare FD numbers are not an
    # outside custody contract. Direct held_capture still has its legacy scope.
    need(body_plane_fds is None,"SOL151_BARE_EXTERNAL_BODY_FDS_NO_OWNER_CONTRACT")
    custody={"owner":os.getpid(),"planes":[],"capsule":None,"work_ns":work,"hard_ns":hard,
        "first_error":None,"original_errors":[],"pending_original_error":None,"error_ledger_error":None,
        "actual_outer_pid":None,"launch_record":None,"launch_auxiliaries":None,
        "phase":"PREOWNED_SOURCE_BODY","launch_error":None,"launch_first_error":None,
        "launch_buffers":None,"launch_read_errors":None,"launch_retention_errors":None,
        "launch_close_errors":None,"launch_cleanup_errors":None,"writer_end_confirmed":False,
        "final_clock_error":None,
        "writer_end_error":None,"writer_end_evidence":[{"raw":None,"group_identity9":None,
            "group_final_identity9":None,"populated_zero":False} for _ in range(2)],
        "retirement_complete":False,"receipt":None,"SourceReady":False,"GO":False}
    SOL151_A079_CUSTODY=custody
    receipt={"schema":"friday.sol151.actual-a079-body-custody.v1","accepted":False,"passed":False,
        "state":"REFUSED_BEFORE_CLONE","owned":None,"stdout_stream":None,"stderr_stream":None,
        "inner_stdout_stream":None,"inner_stderr_stream":None,"original_error":None,"cleanup_errors":[],
        "actual_auxiliary_cleanup":None,"case":case,"body_custody":custody,
        "network_effects":0,"body_credit":False,"whole_browser_credit":False,
        "full_registry_controls":"SOURCE_INCOMPLETE","SourceReady":False,"GO":False}
    custody["receipt"]=receipt
    try:
        cap=os.pread(fds[100],849,0);custody["capsule"]=cap
        need(len(cap)==848 and hashlib.sha256(cap).hexdigest()==pin and
             identity(os.fstat(fds[100]))==snapshots[100][0],"SOL151_SAME_HELD_CAPSULE_FOR_FACTORY")
        session=cap[112:144];sha=hashlib.sha256(cap).digest()
        source_uid,source_gid=struct.unpack_from("<II",cap,20)
        for slot in range(4):
            need(time.monotonic_ns()<work,"SOL151_ORIGINAL_PREBIRTH_WORK_END")
            a201_create_body(slot,session,sha,hard,custody["planes"],
                source_uid=source_uid,source_gid=source_gid,prebirth_end_ns=work)
        body_plane_fds=tuple(state["fd"] for state in custody["planes"])
        custody["phase"]="ACTUAL_OUTER_CAPTURE"
        receipt=held_capture(fds,pin,work,hard,main_reserve_ns=10**9,cleanup_reserve_ns=100000000,
            four_streams=True,body_plane_fds=body_plane_fds,body_custody=custody)
        custody["receipt"]=receipt
    except BaseException as exc:
        if custody["phase"]=="PREOWNED_SOURCE_BODY" and custody["planes"]:
            custody["planes"][-1]["original_error"]=exc
        else:custody["launch_error"]=exc
        if custody["launch_first_error"] is not None:sol151_remember(custody,custody["launch_first_error"])
        sol151_remember(custody,exc)
        if custody["launch_record"] is not None:
            receipt["owned"]=custody["launch_record"]
            receipt["cleanup_errors"]=custody["launch_cleanup_errors"]
    receipt.update(case=case,network_effects=0,body_credit=False,whole_browser_credit=False,
        full_registry_controls="SOURCE_INCOMPLETE",body_custody=custody,SourceReady=False,GO=False)
    if receipt["original_error"] is not None:sol151_remember(custody,receipt["original_error"])
    if custody["actual_outer_pid"] is None:
        sol151_retire(custody,unborn=True)
    else:
        try:sol151_writer_end(custody,fds)
        except BaseException as exc:
            custody["writer_end_error"]=exc;sol151_remember(custody,exc)
        if custody["writer_end_confirmed"]:
            for state in custody["planes"]:
                try:a201_snapshot_plane(state)
                except BaseException as exc:
                    state["snapshot_error"]=exc;sol151_remember(custody,exc)
            sol151_consume_planes(custody)
    # Full original streams/body prefixes survive every semantic refusal. The
    # actual third decoder and the original strict case/count/status oracles
    # remain mandatory; no default-positive credit from merely adding planes.
    semantic_ok=False
    if receipt["stdout_stream"] is not None:
        try:
            stdout,stderr=receipt["stdout_stream"],receipt["stderr_stream"]
            need(stdout["full_original_bounded_raw"] and stderr["full_original_bounded_raw"],"A079_FULL_ORIGINAL_RAW_CUSTODY")
            raw=stdout["raw"]
            need(not stderr["raw"] and raw.endswith(b"\n") and raw.count(b"\n")==1,"A079_NATIVE_TERMINAL_FRAME_NO_PREEMPTION")
            outer=strict_json(raw);receipt["outer"]=outer
            sol149_bind_inner_streams(outer,[receipt['inner_stdout_stream'],receipt['inner_stderr_stream']])
            need(not receipt['inner_stderr_stream']['raw'],'SOL149_ORIGINAL_INNER_STDERR_EMPTY')
            sol143_native_check(outer)
            receipt["inner"]=oracle(outer,case,receipt['inner_stdout_stream']['raw'])
            status=receipt["owned"]["status"]
            need(os.WIFEXITED(status) and os.WEXITSTATUS(status)==(0 if case=="positive" else 2),"A079_ACTUAL_PUBLIC_NATIVE_EXIT")
            complement(bundle,snapshots)
            a201_guard(hard)
            semantic_ok=True
        except BaseException as exc:sol151_remember(custody,exc)
    if custody["writer_end_confirmed"]:sol151_retire(custody)
    try:a201_guard(hard)
    except BaseException as exc:
        custody["final_clock_error"]=exc;sol151_remember(custody,exc)
    if custody["launch_auxiliaries"] is not None:
        receipt["actual_auxiliary_cleanup"]=custody["launch_auxiliaries"].receipt()
    receipt["original_error"]=custody["first_error"]
    complete=semantic_ok and custody["actual_outer_pid"] is not None and custody["writer_end_confirmed"] and \
        custody["retirement_complete"] and custody["first_error"] is None and custody["error_ledger_error"] is None and \
        not receipt["cleanup_errors"] and all(e["closed"] for e in receipt["actual_auxiliary_cleanup"])
    receipt["accepted"]=receipt["passed"]=complete
    receipt["state"]="ACCEPTED_SCOPED_RECEIPT" if complete else "STOP_UNCONFIRMED" if \
        not custody["retirement_complete"] or receipt["cleanup_errors"] else "REFUSED_SCOPED_RECEIPT"
    if complete:receipt["terminal_sha256"]=receipt["stdout_stream"]["sha256"]
    return receipt


def run_positive_first(positive,negative,case,*,positive_body_plane_fds=None,negative_body_plane_fds=None):
    need(case in CASES[1:] and positive[1]!=negative[1],"A079_FRESH_INDEPENDENT_PAIR")
    a,b=(os.fstat(bundle[0][122]) for bundle in (positive,negative))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A079_FRESH_PROTECTED_VIEWS")
    need(all(positive[2][role]==negative[2][role] for role in SOURCES if role!=119),"A079_FULL_SOURCE_COMPLEMENT_BETWEEN_CASES")
    # A separate approved capsule binds each real view and single inert DATA
    # selector. No shared Root grant is invented from this pair or its result.
    pos=run_public(positive,"positive",body_plane_fds=positive_body_plane_fds)
    if not pos["accepted"]:
        return {"positive":pos,"negative":None,"negative_disposition":"NOT_RUN_POSITIVE_REFUSED",
            "body_credit":False,"whole_browser_credit":False,"SourceReady":False,"GO":False}
    neg=run_public(negative,case,body_plane_fds=negative_body_plane_fds)
    return {"positive":pos,"negative":neg,"source_ready":False,"current_GO":False,"full_registry_controls":"SOURCE_INCOMPLETE"}
