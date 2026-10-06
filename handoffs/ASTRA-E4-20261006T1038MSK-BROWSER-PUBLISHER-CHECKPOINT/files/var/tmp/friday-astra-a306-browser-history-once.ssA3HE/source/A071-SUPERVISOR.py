#!/usr/bin/python3.14
"""A061 SOURCE ONLY. Reviewed outer owner; never trust inner success alone.
Future invocation requires external exact source/runtime pins and an already
provisioned exclusive root-controlled cgroup. This source never creates a cgroup.
Private inert controls call the same drain/reap/terminal consumers, without exec.
"""
import errno
import base64
import fcntl
import hashlib
import json
import os
import re
import resource
import select
import signal
import stat
import struct
import sys
import time
import types

BASE = "/home/jericho/.jericho/runtime/subagent-lifecycle/"
STEM = "/var/tmp/friday-astra-browser-a099-whole-source-closure-a104-g1/source/A071"
EXECUTOR, CONTROLS, BILL = (STEM + s for s in ("-EXECUTOR.py", "-CONTROLS.py", "-BILL.json"))
RUNTIME = STEM + "-TRUSTED-RUNTIME.json"
PYTHON = "/usr/bin/python3.14"
STDLIB = "/usr/lib/python3.14"
CGROUP = "/sys/fs/cgroup/friday-browser3-a061-g1"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
PIPE_CAP = 1048576
RSS = 268435456
OUTER_AS = 67108864
INNER_MEMORY = RSS - OUTER_AS
SEALS = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
SOURCE_ROLES = {"executor": (101, EXECUTOR), "controls": (102, CONTROLS),
    "supervisor": (103, STEM + "-SUPERVISOR.py"), "bill": (104, BILL),
    "G1": (105, BASE + "ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1.py"),
    "R4": (106, BASE + "ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1-R4.py"),
    "owner": (107, "/home/jericho/.jericho/grok-takeover/ASTRA-E4-OWNER-DELEGATED-PROJECT-AUTHORITY-20261001.json"),
    "inventory": (108, BASE + "ASTRA-E4-PARTIAL-ACQUISITION-CUSTODY-BROWSER-SUCCESSOR-A035-G1-PARTIAL-INVENTORY.json"),
    "CA": (109, "/etc/ssl/certs/ca-certificates.crt")}
INCOMPLETE = b'{"state":"OUTER_SUPERVISION_FAILED","body_complete":false,"terminal_completion":false,"acceptance_complete":false}\n'


class Refused(Exception):
    pass


def need(ok, cause):
    if not ok:
        raise Refused(cause)


def resource_data_equal(a, b):
    return sol069_equal(a,b)

def full_DATA_copy(value):
    return sol069_copy(value)

def full_DATA_encode(value):
    return sol069_encode(value,PIPE_CAP,need)

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


def full_DATA_decode(raw,parse=None):
    return sol143_native_check(_sol143_prior_full_DATA_decode(raw,parse))

def inert(raw):
    return sol143_native_check(_sol143_prior_inert(raw))

def _sol143_prior_full_DATA_decode(raw,parse=None):
    if raw[:4]==b'DS69' or raw[:8]==A201_REF_MAGIC:return sol069_decode(raw,PIPE_CAP,need,inert if parse is None else parse)
    need(type(raw) is bytes and 8<=len(raw)<=PIPE_CAP and raw[:4]==b'DS67','A067_DATA_FULL_FRAME')
    length=int.from_bytes(raw[4:8],'big')
    need(0<length<=len(raw)-8,'A067_DATA_HEADER_BOUND')
    head=(inert if parse is None else parse)(raw[8:8+length]);at=8+length;strings=[]
    need(type(head) is dict and set(head)=={'tree','strings'} and type(head['strings']) is int and
        0<=head['strings']<=(len(raw)-at)//4,'A067_DATA_STRING_COUNT')
    for _ in range(head['strings']):
        need(at+4<=len(raw),'A067_DATA_COMPLETE_STRING_LENGTH')
        size=int.from_bytes(raw[at:at+4],'big');at+=4
        need(size<=len(raw)-at,'A067_DATA_COMPLETE_STRING_BODY')
        strings.append(raw[at:at+size].decode('utf8','surrogatepass'));at+=size
    need(at==len(raw),'A067_DATA_NO_TRAILER')
    def string(index):
        need(type(index) is int and 0<=index<len(strings),'A067_DATA_STRING_REF')
        return strings[index]
    def restore(row):
        need(type(row) is list and len(row)==2,'A067_DATA_TAGGED_VALUE')
        tag,body=row
        if tag=='s':return string(body)
        if tag=='v':
            need(body is None or type(body) in (bool,int,float),'A067_DATA_SCALAR')
            return body
        if tag=='l':
            need(type(body) is list,'A067_DATA_LIST');return [restore(v) for v in body]
        need(tag=='d' and type(body) is list,'A067_DATA_DICTIONARY')
        result={}
        for pair in body:
            need(type(pair) is list and len(pair)==2,'A067_DATA_KEY_VALUE_PAIR')
            key=string(pair[0]);need(key not in result,'A067_DATA_DUPLICATE_KEY')
            result[key]=restore(pair[1])
        return result
    return restore(head['tree'])

def resource_instrument_packet(raw, expected, actor, end_ns, domain):
    """Original receiver's pure DATA consumer, also used by pre-inner caller.
    No native/session/child fact is invented for a zero-child instrument.
    This function grants neither kernel resource credit nor Source execution.
    """
    need(type(raw) is bytes and 0 < len(raw) <= PIPE_CAP and (raw[:4] in (b'DS67',b'DS69') or raw.endswith(b'\n')),
         'RESOURCE_COMPLETE_PACKET_BOUND')
    row = full_DATA_decode(raw) if raw[:4] in (b'DS67',b'DS69') else inert(raw)
    need(type(row) is dict and row['schema'] == 'friday.sol066.resource-controller.v1' and
         resource_data_equal(row['expected'],expected) and resource_data_equal(row['actor'],actor) and row['domain'] == domain and
         row['end_ns'] == str(end_ns) and row['source_instrument_only'] is True and
         row['kernel_live_admission_credit'] is False and row['children'] == [] and
         row['started_routes'] == 0 and row['hashes'] == [] and
         row['SourceReady'] is False and row['GO'] is False, 'RESOURCE_EXACT_ORIGINAL_SCOPE')
    instrument = row['instrument']
    need(instrument['schema'] == 'friday.sol066.resource-source-instrument.v1' and
         instrument['id'] == row['id'] and type(row['position']) is int and
         instrument['consumer'] == expected['consumer'] and instrument['domain'] == domain and
         instrument['source_instrument_only'] is True and instrument['kernel_live_admission_credit'] is False and
         instrument['entered'] is True and type(row['consumer_calls']) is int and row['consumer_calls'] >= expected['minimum_consumer_calls'] and
         expected['owned_children'] == expected['started_routes'] == 0 and expected['hashes'] == 'none',
         'RESOURCE_ACTUAL_CONSUMER_ZERO_TARGET_SCOPE')
    predicates = instrument['predicates']
    need(type(predicates) is list and all(type(p) is dict and type(p['ok']) is bool and
         type(p['cause']) is str and type(p['at_ns']) is str and p['at_ns'].isdecimal() for p in predicates),
         'RESOURCE_ACTUAL_PREDICATE_TRACE')
    first = next((i for i,p in enumerate(predicates) if not p['ok']), None)
    need(first is None if expected['cause'] is None else first is not None and
         first == len(predicates)-1 and predicates[first]['cause'] == expected['cause'],
         'RESOURCE_ORIGINAL_FIRST_FAULT_WITH_ALL_EARLIER_PREDICATES')
    need(row['actual_cause'] == expected['cause'] and row['state'] == expected['state'] and
         row['stage'] == expected['stage'] and not row['cleanup_errors'] and
         instrument['cleanup_confirmed'] is True, 'RESOURCE_CLEANUP_INVALIDATES_PASS')
    if expected['cause'] is None:
        need(instrument['fault'] is None and instrument['fault_applied'] == 0 and
             instrument['returned'] is True and instrument['positive_uses_actual_reads'] is True and
             all(not r.get('changed',False) and all(v['captured'] == v['presented'] and
                 v['read_error'] is v['presentation_error'] is None for v in r.get('reads',[]))
                 for r in instrument['reads']), 'RESOURCE_NO_FAKE_POSITIVE_OR_KERNEL_FACTS')
    else:
        need(instrument['fault'] is not None and instrument['fault_applied'] == 1 and
             type(instrument['fault_applied']) is type(instrument['changed_observations']) is int and
             instrument['changed_observations'] == 1 and
             instrument['returned'] is False and row['error_graph']['roots'],
             'RESOURCE_SINGLE_SEALED_NEGATIVE_PRESENTATION')
    need(row['terminal_completion'] is True and row['native_child'] is None and
         row['native_evidence'] is None and row['root_live_admission_credit'] is False,
         'RESOURCE_NO_FAKE_NATIVE_CHILD_OR_ROOT_GRANT')
    return row


def ident(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
            st.st_uid, st.st_gid, st.st_mode, st.st_nlink)


def before_deadline(deadline, deadline_ns=None):
    if deadline_ns is not None:
        need(type(deadline_ns) is int and deadline_ns>=0,"EXACT_DEADLINE_NS")
        return time.monotonic_ns()<deadline_ns
    return deadline is None or time.monotonic()<deadline


def bounded_fd_bytes(fd, sha, cap, *, sealed=False, root=False, deadline=None, collect=True, deadline_ns=None):
    """Hash and consume the SAME descriptor. Sealed memfds cannot be substituted.
    Root custody is separate from a parser fixture; no fixture grants root proof.
    """
    need(type(sha) is str and re.fullmatch(r"[0-9a-f]{64}", sha), "UNKNOWN_PIN")
    before = os.fstat(fd)
    need(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= cap, "HELD_FD_CUSTODY")
    if root:
        need(before.st_uid == before.st_gid == 0, "HELD_ROOT_AUTHORITY")
    if sealed:
        need(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS == SEALS, "HELD_KERNEL_SEALS")
    parts, h, count = [], hashlib.sha256(), 0
    while True:
        if deadline is not None or deadline_ns is not None:
            need(before_deadline(deadline,deadline_ns), "HELD_READ_TIMEOUT")
        block = os.pread(fd, min(65536, cap - count + 1), count)
        if not block:
            break
        count += len(block)
        need(count <= cap, "HELD_FD_SIZE")
        h.update(block)
        if collect: parts.append(block)
    need(count == before.st_size and h.hexdigest() == sha, "HELD_FD_SHA")
    need(ident(before) == ident(os.fstat(fd)), "HELD_FD_DRIFT")
    return b"".join(parts) if collect else ident(before)


class HeldSource:
    """Freeze an approved source/tool/data file into a real sealed memfd.
    The original path is checked during capture only. Later consumers use this
    exact sealed fd, never a new named-path open. No runtime execution here.
    """
    def __init__(self, path, sha, cap=PIPE_CAP, *, system=False, deadline=None, deadline_ns=None):
        self.path, self.sha, self.fd = path, sha, None
        original = nf(path)
        try:
            before = os.fstat(original)
            need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                 ((before.st_uid == before.st_gid == 0 and not before.st_mode & 0o022) if system else
                  (before.st_uid == before.st_gid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600)),
                 "PIN_CUSTODY")
            raw = bounded_fd_bytes(original, sha, cap, deadline=deadline, deadline_ns=deadline_ns)
            linked = nf(path)
            try: need(ident(os.fstat(linked)) == ident(before), "PIN_PATH_DRIFT")
            finally: os.close(linked)
            self.fd = os.memfd_create("friday-a061-approved-bytes", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            left = raw
            while left:
                if deadline is not None or deadline_ns is not None:
                    need(before_deadline(deadline,deadline_ns), "HELD_READ_TIMEOUT")
                n = os.write(self.fd, left)
                need(n > 0, "HELD_SHORT_WRITE"); left = left[n:]
            fcntl.fcntl(self.fd, fcntl.F_ADD_SEALS, SEALS)
            need(bounded_fd_bytes(self.fd, sha, cap, sealed=True, deadline=deadline, deadline_ns=deadline_ns) == raw, "HELD_COPY_DRIFT")
        except BaseException:
            self.close(); raise
        finally:
            os.close(original)

    def bytes(self, cap=PIPE_CAP, deadline=None, *, deadline_ns=None):
        return bounded_fd_bytes(self.fd, self.sha, cap, sealed=True, deadline=deadline, deadline_ns=deadline_ns)

    def close(self):
        if self.fd is not None:
            value, self.fd = self.fd, None
            os.close(value)


def source_bundle(pins, deadline):
    need(type(pins) is dict and set(pins) == set(SOURCE_ROLES), "HELD_SOURCE_SET")
    held = {}
    try:
        for role, (number, path) in SOURCE_ROLES.items():
            held[role] = HeldSource(path, pins[role], system=role == "CA", deadline=deadline)
        return held
    except BaseException:
        for blob in held.values(): blob.close()
        raise


class NativeLaunchAdapter:
    """Functional fd-only handoff to a SEPARATELY approved STATIC native launcher.
    Legacy structural-control adapter, NOT the A061 public production entry.
    The actual A061 implementation is BOOTSTRAP/COMMON/OWNED.c. This legacy
    signature is retained only for the inherited216 fixture obligations and cannot
    approve that native implementation. Separately admitted runtime must implement
    the declared pre-interpreter runtime/mount/limits/clone-registry contract.
    Its exact binary SHA and runtime closure come from independent root review.
    No arbitrary command, path, URL, environment or weaker interpreter route.
    """
    def __init__(self, launcher, runtime, *, launcher_sha, runtime_sha, deadline, deadline_ns=None):
        self.launcher, self.runtime, self.deadline = launcher, runtime, deadline
        self.deadline_ns=deadline_ns
        raw = bounded_fd_bytes(launcher, launcher_sha, 16777216, sealed=True, root=True, deadline=deadline,deadline_ns=deadline_ns)
        need(raw[:4] == b"\x7fELF", "NATIVE_LAUNCHER_ELF")
        dependencies, loader = elf_needed_fd(launcher)
        need(not dependencies and loader is None, "NATIVE_LAUNCHER_NOT_STATIC")
        bounded_fd_bytes(runtime, runtime_sha, 134217728, sealed=True, root=True, deadline=deadline, collect=False,deadline_ns=deadline_ns)
        self.pins = {"launcher_sha256": launcher_sha, "runtime_sha256": runtime_sha}

    def handoff(self, held, capsule_fd, out, err, group, attach):
        # Called in the exact owned child, after authoritative intention registration
        # in its parent. execve(fd) consumes the already approved static ELF bytes.
        need(before_deadline(self.deadline,self.deadline_ns), "NATIVE_LAUNCHER_TIMEOUT")
        need(set(held) == set(SOURCE_ROLES), "HELD_SOURCE_SET")
        needed = {100: capsule_fd, 110: self.launcher, 111: self.runtime, 120: group, 121: attach}
        needed.update({SOURCE_ROLES[role][0]: blob.fd for role, blob in held.items()})
        # Duplicate above all fixed destinations BEFORE dup2, avoiding fd permutation.
        temporary = {}
        try:
            for number, fd in needed.items():
                temporary[number] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 256)
            for number, fd in temporary.items(): os.dup2(fd, number, inheritable=True)
        finally:
            for fd in temporary.values(): os.close(fd)
        os.dup2(out, 1); os.dup2(err, 2)
        keep = {0, 1, 2} | set(needed)
        for name in os.listdir("/proc/self/fd"):
            fd = int(name)
            if fd not in keep:
                try: os.close(fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF: raise
        os.execve(110, ["friday-approved-native-browser3", "--inner-held-a061", "100", "111"], ENV)


def nf(path):
    need(type(path) is str and path.startswith("/") and
         all(p not in ("", ".", "..") for p in path.split("/")[1:]), "ABSOLUTE_PATH")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.split("/")[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        return os.open(path.split("/")[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)


def seal(path, sha, cap, system=False, collect=True):
    need(type(sha) is str and re.fullmatch(r"[0-9a-f]{64}", sha), "UNKNOWN_PIN")
    fd = nf(path)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= cap,
             "PIN_CUSTODY")
        need((before.st_uid == before.st_gid == 0 and not before.st_mode & 0o022) if system else
             (before.st_uid == before.st_gid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600),
             "PIN_CUSTODY")
        h, data, count = hashlib.sha256(), [], 0
        while True:
            block = os.read(fd, min(65536, cap - count + 1))
            if not block: break
            count += len(block); need(count <= cap, "PIN_SIZE")
            h.update(block)
            if collect: data.append(block)
        need(count == before.st_size and h.hexdigest() == sha and ident(before) == ident(os.fstat(fd)), "PIN_SHA_OR_DRIFT")
        linked = nf(path)
        try: need(ident(os.fstat(linked)) == ident(before), "PIN_PATH_DRIFT")
        finally: os.close(linked)
        return b"".join(data) if collect else ident(before)
    finally: os.close(fd)



def sol081_decode_receipt_stream(raw, require, expected=None):
    """Bounded physical candidate pairs. No physical line is final qualification."""
    require(type(raw) is str, 'SOL081_STREAM_TEXT')
    data = raw.encode('utf-8', 'strict')
    require(0 < len(data) <= 1048576 and data.endswith(b'\n') and
            data.count(b'\n') == 6, 'SOL081_STREAM_COMPLETE_THREE_PAIRS')

    def unique(items):
        row = {}
        for key, value in items:
            require(key not in row, 'SOL081_STREAM_DUPLICATE_KEY')
            row[key] = value
        return row

    def constant(value):
        require(False, 'SOL081_STREAM_NONFINITE')

    def canon(row):
        return json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False)

    lines = data.split(b'\n')[:-1]
    rows = [json.loads(line, object_pairs_hook=unique, parse_constant=constant)
            for line in lines]
    require(all(canon(row).encode() == line for row, line in zip(rows, lines)),
            'SOL081_CANONICAL_PHYSICAL_LINES')
    routes, pids, candidates = set(), set(), {}
    for index in range(0, 6, 2):
        provisional, candidate = rows[index:index + 2]
        require(type(provisional) is dict and type(candidate) is dict,
                'SOL081_STREAM_ROW')
        require(provisional.get('receipt_protocol') == 'friday.sol081.browser.v1' and
                candidate.get('receipt_protocol') == 'friday.sol081.browser.v1' and
                provisional.get('receipt_commitment') == 'PROVISIONAL' and
                provisional.get('receipt_qualified') is False and
                candidate.get('receipt_commitment') == 'CANDIDATE' and
                candidate.get('receipt_qualified') is False,
                'SOL081_STREAM_NOT_A_SELF_QUALIFIED_TERMINAL')
        witness = hashlib.sha256(lines[index] + b'\n').hexdigest()
        require(candidate.get('provisional_sha256') == witness,
                'SOL081_EXACT_PROVISIONAL_PREIMAGE')
        comparable = dict(provisional, receipt_commitment='CANDIDATE',
                          receipt_qualified=False, provisional_sha256=witness)
        require(canon(comparable) == canon(candidate),
                'SOL081_EXACT_RECORD_CORRESPONDENCE')
        route = candidate.get('relative_path')
        launch = candidate.get('launch')
        require(type(route) is str and 0 < len(route) <= 256 and
                not route.startswith('/') and
                all(part not in ('', '.', '..') for part in route.split('/')) and
                type(launch) is dict and launch.get('relative_path') == route,
                'SOL081_STREAM_ROUTE')
        pid = launch.get('pid')
        require(type(pid) is int and pid > 0 and route not in routes and
                pid not in pids, 'SOL081_STREAM_OWNED_PID_ROUTE')
        routes.add(route); pids.add(pid); candidates[route] = candidate
        require(type(candidate.get('bytes')) is int and candidate['bytes'] >= 0 and
                type(candidate.get('observed_retained_bytes')) is int and
                candidate['observed_retained_bytes'] == candidate['bytes'] and
                type(candidate.get('sha256')) is str and len(candidate['sha256']) == 64 and
                all(ch in '0123456789abcdef' for ch in candidate['sha256']) and
                candidate.get('observed_retained_sha256') == candidate['sha256'],
                'SOL081_EXACT_BODY_BYTES_SHA')
        require(candidate.get('body_complete') is True and
                candidate.get('worker_lifecycle') == 'REAPED' and
                candidate.get('stop_confirmed') is True and
                candidate.get('pipe_accepted') is True,
                'SOL081_STREAM_GENUINE_COMPLETE_MATERIAL')
    if expected is not None:
        require(type(expected) is dict and set(expected) == routes,
                'SOL081_STREAM_MATERIAL_SET')
        for route, material in expected.items():
            require(type(material) is dict, 'SOL081_STREAM_MATERIAL')
            comparable = dict(material, receipt_commitment='CANDIDATE',
                              receipt_qualified=False)
            require(canon(comparable) == canon(candidates[route]),
                    'SOL081_STREAM_ACTUAL_MATERIAL_JOIN')
    return candidates


def sol081_rebuild_receipt_stream(materials, pairs, require):
    """Exact canonical metadata preimage, not native/body or retirement credit."""
    require(type(materials) is dict and len(materials) == 3 and
            type(pairs) is list and len(pairs) == 3, 'SOL081_PROJECTION_CARDINALITY')
    chunks, routes, offset = [], set(), 0
    for pair in pairs:
        require(type(pair) is dict and set(pair) ==
                {'relative_path', 'pid', 'offset', 'provisional_bytes',
                 'candidate_bytes', 'provisional_sha256'},
                'SOL081_PROJECTION_FIELDS')
        route = pair['relative_path']
        require(type(route) is str and route in materials and route not in routes,
                'SOL081_PROJECTION_ROUTE')
        routes.add(route)
        material = materials[route]
        require(type(material) is dict and type(material.get('launch')) is dict and
                type(pair['pid']) is int and pair['pid'] > 0 and
                material['launch'].get('pid') == pair['pid'] and
                type(material['launch'].get('pid')) is int,
                'SOL081_PROJECTION_EXACT_PID')
        for key in ('offset', 'provisional_bytes', 'candidate_bytes'):
            require(type(pair[key]) is int and 0 <= pair[key] <= 1048576,
                    'SOL081_PROJECTION_EXACT_BOUNDS')
        require(pair['offset'] == offset, 'SOL081_PROJECTION_GAPLESS')
        candidate = dict(material, receipt_commitment='CANDIDATE', receipt_qualified=False)
        provisional = dict(candidate, receipt_commitment='PROVISIONAL')
        provisional.pop('provisional_sha256', None)
        p = json.dumps(provisional, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        witness = hashlib.sha256(p).hexdigest()
        require(material.get('provisional_sha256') == witness ==
                pair['provisional_sha256'], 'SOL081_PROJECTION_EXACT_WITNESS')
        q = json.dumps(candidate, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        require(len(p) == pair['provisional_bytes'] and len(q) == pair['candidate_bytes'] and
                0 < offset + len(p) + len(q) <= 1048576,
                'SOL081_PROJECTION_EXACT_PHYSICAL_BYTES')
        chunks.extend((p, q)); offset += len(p) + len(q)
    require(routes == set(materials), 'SOL081_PROJECTION_COMPLETE_ROUTES')
    return b''.join(chunks)


def sol081_browser_stream_join(value):
    """Both actual parsers require the final caller snapshot, not an NDJSON flag."""
    if type(value) is not dict or value.get('state') != 'BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS':
        return
    stream = value.get('receipt_stream')
    need(type(stream) is dict and set(stream) ==
         {'schema', 'pairs', 'bytes', 'sha256', 'identity9_decimal_strings',
          'finite_end_confirmed'} and
         stream['schema'] == 'friday.sol081.browser-receipt-stream.v1' and
         stream['finite_end_confirmed'] is True and
         value.get('reason') is None and value.get('uncertainty_sticky') is False,
         'SOL081_FINITE_CALLER_END_REQUIRED')
    materials = value.get('materials')
    data = sol081_rebuild_receipt_stream(materials, stream['pairs'], need)
    need(type(stream['bytes']) is int and
         0 < stream['bytes'] <= 1048576, 'SOL081_SNAPSHOT_CAP')
    need(len(data) == stream['bytes'] and
         hashlib.sha256(data).hexdigest() == stream['sha256'],
         'SOL081_PHYSICAL_STREAM_SHA_SIZE')
    nine = stream['identity9_decimal_strings']
    need(type(nine) is list and len(nine) == 9 and
         all(type(item) is str and item.isascii() and item.isdecimal() for item in nine) and
         int(nine[0]) > 0 and int(nine[1]) > 0 and int(nine[2]) == 33152 and
         int(nine[3]) == int(nine[4]) and int(nine[5]) == 1 and
         int(nine[6]) == stream['bytes'], 'SOL081_PHYSICAL_STREAM_IDENTITY9')
    candidates = sol081_decode_receipt_stream(data.decode('utf-8', 'strict'), need, materials)
    need(type(materials) is dict and len(materials) == 3 and
         all(row.get('receipt_commitment') == 'QUALIFIED' and
             row.get('receipt_qualified') is True for row in materials.values()),
         'SOL081_ONLY_FINAL_CALLER_QUALIFIES')
    retained = value.get('retained')
    need(type(retained) is list and len(retained) == 5 and
         all(type(row) is dict for row in retained) and
         len({row.get('path') for row in retained}) == 5,
         'SOL081_FINAL_RETAINED_SET')
    by_route = {row['path']: row for row in retained}
    receipt = by_route.get('transport-receipts.ndjson')
    need(type(receipt) is dict and receipt.get('bytes') == stream['bytes'] and
         receipt.get('sha256') == stream['sha256'] and
         receipt.get('identity9_decimal_strings') == nine,
         'SOL081_PHYSICAL_RETAINED_STREAM_JOIN')
    need(set(by_route) == set(candidates) | {'bill.json', 'transport-receipts.ndjson'},
         'SOL081_EXACT_FINAL_ROUTE_SET')
    children, journal = value.get('children'), value.get('launch_journal')
    need(type(children) is list and type(journal) is list and len(children) == len(journal) == 3,
         'SOL081_ACTUAL_CHILD_JOURNAL_CARDINALITY')
    need(all(type(row) is dict and type(row.get('pid')) is int and row['pid'] > 0 and
             row.get('lifecycle') == 'REAPED' and row.get('connected') is True for row in children),
         'SOL081_EXACT_COMPLETED_CHILDREN')
    child_ids = {(row.get('pid'), row.get('relative_path')) for row in children if type(row) is dict}
    journal_ids = {(row.get('pid'), row.get('relative_path')) for row in journal if type(row) is dict}
    actual_ids = {(row['launch']['pid'], route) for route, row in candidates.items()}
    need(len(actual_ids) == len(child_ids) == len(journal_ids) == 3 and
         child_ids == journal_ids == actual_ids, 'SOL081_CHILD_MATERIAL_STREAM_JOIN')
    for route, material in materials.items():
        body = by_route[route]
        need(body.get('bytes') == material.get('observed_retained_bytes') ==
             material.get('bytes') and
             body.get('sha256') == material.get('observed_retained_sha256') ==
             material.get('sha256'), 'SOL081_PHYSICAL_BODY_STREAM_JOIN')
        matches = [row for row in journal if row.get('relative_path') == route]
        need(len(matches) == 1 and
             json.dumps(matches[0], sort_keys=True, separators=(',', ':'), allow_nan=False) ==
             json.dumps(material['launch'], sort_keys=True, separators=(',', ':'), allow_nan=False),
             'SOL081_FINAL_LAUNCH_RECORD_JOIN')


def sol076_browser_terminal_join(value):
    """Exact integer pid and route join for the actual browser acquisition terminal."""
    if type(value) is not dict:
        return
    if value.get('state') != 'BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS':
        return
    children = value.get('children')
    journal = value.get('launch_journal')
    need(type(children) is list and type(journal) is list and len(children) == 3 and len(journal) == 3,
         'SOL076_BROWSER_PID_CARDINALITY')

    def ident(row, cause):
        need(type(row) is dict, cause)
        pid = row.get('pid')
        route = row.get('relative_path')
        need(type(pid) is int and pid > 0, cause)
        need(type(route) is str and 0 < len(route) <= 256 and
             all(part not in ('', '.', '..') for part in route.split('/')), 'SOL076_BROWSER_ROUTE')
        return pid, route

    child_ids = [ident(row, 'SOL076_BROWSER_CHILD_PID') for row in children]
    journal_ids = [ident(row, 'SOL076_BROWSER_JOURNAL_PID') for row in journal]
    need(len({pid for pid, route in child_ids}) == 3 and
         len({route for pid, route in child_ids}) == 3 and
         set(child_ids) == set(journal_ids), 'SOL076_BROWSER_PID_ROUTE_JOIN')


def sol076_receiver_guard(value):
    """Reject false ordinary-launch completion; never grant native retirement."""
    if type(value) is not dict: return
    rows = []
    journal = value.get('launch_journal')
    if journal is not None:
        need(type(journal) is list, 'SOL076_LAUNCH_JOURNAL_TYPE')
        rows.extend((row, value) for row in journal)
    materials = value.get('materials')
    if type(materials) is dict:
        for material in materials.values():
            if type(material) is dict and 'launch' in material:
                rows.append((material['launch'], material))
    failed = False
    descriptor_journal = value.get('descriptor_journal')
    if descriptor_journal is not None:
        need(type(descriptor_journal) is list, 'SOL076_DESCRIPTOR_JOURNAL_TYPE')
        for rec in descriptor_journal:
            need(type(rec) is dict, 'SOL076_DESCRIPTOR_JOURNAL_ROW')
            if rec.get('fd') is not None and not (
                    rec.get('close_attempted') is True and rec.get('closed') is True and
                    rec.get('close_error') is None and rec.get('hook_error') is None):
                failed = True
    for key in ('wave_a', 'wave_b'):
        if type(value.get(key)) is dict:
            failed = bool(sol076_receiver_guard(value[key])) or failed
    for row, enclosing in rows:
        need(type(row) is dict and row.get('schema') == 'friday.sol076.ordinary-launch.v1',
             'SOL076_ORDINARY_LAUNCH_SCHEMA')
        birth = row.get('birth')
        need(birth in ('CREATED', 'NOT_CREATED', 'PENDING', 'UNCONFIRMED') and
             row.get('child_retirement_accepted') is False and
             row.get('native_body_retirement_accepted') is False,
             'SOL076_NO_NATIVE_RETIREMENT_CREDIT')
        records = row.get('descriptor_records')
        need(type(records) is list and len(records) <= 4 and
             len({rec.get('slot') for rec in records if type(rec) is dict}) == len(records),
             'SOL076_DESCRIPTOR_SLOTS')
        for rec in records:
            need(type(rec) is dict and rec.get('slot') in ('body', 'pipe', 'write_pipe', 'pidfd') and
                 type(rec.get('fd')) is int and rec['fd'] >= 0 and
                 type(rec.get('close_attempted')) is bool and
                 type(rec.get('close_confirmed')) is bool and
                 (not rec['close_confirmed'] or (rec['close_attempted'] and rec.get('close_error') is None)),
                 'SOL076_DESCRIPTOR_CLOSE_FACTS')
        need(type(row.get('cleanup_fault', False)) is bool, 'SOL081_TYPED_CLEANUP_FAULT')
        clean = not row.get('cleanup_fault', False) and all(rec['close_attempted'] and rec['close_confirmed'] and
                    rec.get('close_error') is None for rec in records)
        need(type(row.get('ordinary_cleanup_complete')) is bool and
             row['ordinary_cleanup_complete'] is clean,
             'SOL076_CLEANUP_CONSISTENCY')
        if birth == 'CREATED':
            need(type(row.get('pid')) is int and row['pid'] > 0 and
                 row.get('lifecycle') in ('LIVE', 'REAPED', 'STOP_UNCONFIRMED') and
                 row.get('stop_confirmed') is (row['lifecycle'] == 'REAPED'),
                 'SOL076_ACTUAL_BORN_CHILD')
        else:
            need(row.get('pid') is None and row.get('stop_confirmed') is False and
                 row.get('lifecycle') in ('PENDING', 'NOT_CREATED', 'BIRTH_UNCONFIRMED') and
                 (birth != 'UNCONFIRMED' or row['lifecycle'] == 'BIRTH_UNCONFIRMED'),
                 'SOL076_NO_CHILD_NOT_REAPED')
            if birth == 'NOT_CREATED':
                need(type(row.get('first_failure')) is dict and
                     type(row['first_failure'].get('type')) is str,
                     'SOL076_ORIGINAL_LAUNCH_FAILURE')
        bad = birth != 'CREATED' or not clean or row['lifecycle'] != 'REAPED'
        failed = failed or bad
        if bad:
            need(enclosing.get('body_complete') is not True and
                 enclosing.get('terminal_completion') is not True and
                 enclosing.get('acceptance_complete') is not True,
                 'SOL076_FAILED_ROUTE_NOT_COMPLETE')
    if failed:
        need(value.get('body_complete') is not True and value.get('terminal_completion') is not True and
             value.get('acceptance_complete') is not True, 'SOL076_LATE_FAILURE_INVALIDATES_TERMINAL')
    def unqualified_receipt(row):
        if type(row) is not dict:
            return
        commitment = row.get('receipt_commitment')
        if commitment is None:
            return
        need(commitment in ('PROVISIONAL', 'CANDIDATE', 'QUALIFIED', 'END_UNCONFIRMED', 'PUBLICATION_FAULT', 'COMMIT_FAULT'),
             'SOL076_RECEIPT_COMMITMENT')
        qualified = commitment == 'QUALIFIED' and row.get('receipt_qualified') is True
        if not qualified:
            # Original negative oracles retain already-reaped positive BODY
            # prefixes. A nonfinal local body fact is never receipt/terminal
            # acceptance, and cannot be admitted as a standalone positive row.
            negative_prefix = (row is not value and
                row.get('receipt_protocol') == 'friday.sol081.browser.v1' and
                commitment == 'CANDIDATE' and row.get('receipt_qualified') is False and
                row.get('both_receivers_accepted') is False and
                value.get('state') in ('CONTOUR_ABORTED', 'STOP_UNCONFIRMED') and
                value.get('body_complete') is False and value.get('acceptance_complete') is False and
                value.get('receipt_stream') is None)
            need((row.get('body_complete') is not True or negative_prefix) and
                 row.get('terminal_completion') is not True and
                 row.get('acceptance_complete') is not True, 'SOL076_UNQUALIFIED_RECEIPT_NOT_TERMINAL')
        else:
            if row.get('receipt_protocol') == 'friday.sol081.browser.v1':
                need(value.get('state') == 'BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS' and
                     type(value.get('receipt_stream')) is dict and
                     value['receipt_stream'].get('finite_end_confirmed') is True,
                     'SOL081_NO_STANDALONE_OR_FAILED_CANDIDATE_QUALIFICATION')
            witness = row.get('provisional_sha256')
            need(type(witness) is str and len(witness) == 64 and
                 all(ch in '0123456789abcdef' for ch in witness), 'SOL076_QUALIFIED_RECEIPT_WITNESS')
    sol081_browser_stream_join(value)
    unqualified_receipt(value)
    if type(materials) is dict:
        for material in materials.values():
            unqualified_receipt(material)
    return failed


def _sol143_prior_inert(raw):
    need(len(raw) <= PIPE_CAP, "JSON_CAP")
    def pairs(rows):
        result = {}
        for k, v in rows:
            need(k not in result, "JSON_DUPLICATE")
            result[k] = v
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(Refused("JSON_CONSTANT")))
        sol076_receiver_guard(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refused("JSON_PARSE") from exc


def elf_needed_fd(fd):
    """Read inert ELF64 headers only; never run a loader/ldd/native probe."""
    before = os.fstat(fd)
    def read(count, offset, reason):
        need(type(offset) is int and offset >= 0 and count >= 0 and
             offset + count <= before.st_size, reason)
        raw = os.pread(fd, count, offset)
        need(len(raw) == count, reason)
        return raw
    if True:
        header = os.pread(fd, 64, 0)
        if header[:4] != b"\x7fELF": return (), None
        need(len(header) == 64 and header[4:7] == b"\x02\x01\x01" and
             struct.unpack_from("<HHI", header, 16) in ((2, 62, 1), (3, 62, 1)), "RUNTIME_ELF_FORMAT")
        phoff = struct.unpack_from("<Q", header, 32)[0]
        phsize, phcount = struct.unpack_from("<HH", header, 54)
        need(phsize == 56 and 0 < phcount <= 128, "RUNTIME_ELF_HEADERS")
        loads, dynamic, interpreter = [], None, None
        for i in range(phcount):
            ph = read(56, phoff + i * phsize, "RUNTIME_ELF_HEADERS")
            typ, flags, off, addr, physical, filesz, memsz, alignment = struct.unpack("<IIQQQQQQ", ph)
            if typ == 1: loads.append((addr, filesz, off))
            if typ == 2: dynamic = (off, filesz)
            if typ == 3:
                need(interpreter is None and 0 < filesz <= 512, "RUNTIME_LOADER_CAP")
                raw = read(filesz, off, "RUNTIME_LOADER_CAP")
                need(raw.endswith(b"\0") and b"\0" not in raw[:-1], "RUNTIME_LOADER_CAP")
                interpreter = raw[:-1].decode("ascii", "strict")
        if dynamic is None: return (), interpreter
        need(dynamic[1] <= 65536 and dynamic[1] % 16 == 0, "RUNTIME_DYNAMIC_CAP")
        entries = [struct.unpack("<qQ", read(16, dynamic[0] + i, "RUNTIME_DYNAMIC_CAP"))
                   for i in range(0, dynamic[1], 16)]
        needed = [v for k, v in entries if k == 1]
        strings = [v for k, v in entries if k == 5]
        sizes = [v for k, v in entries if k == 10]
        need(len(strings) == len(sizes) == 1 and sizes[0] <= PIPE_CAP and
             not any(k in (15, 29) for k, v in entries), "RUNTIME_DYNAMIC_SEARCH")
        locations = [off + strings[0] - addr for addr, size, off in loads
                     if addr <= strings[0] and strings[0] + sizes[0] <= addr + size]
        need(len(locations) == 1, "RUNTIME_STRTAB")
        table = read(sizes[0], locations[0], "RUNTIME_STRTAB")
        result = []
        for off in needed:
            need(off < len(table) and b"\0" in table[off:], "RUNTIME_SONAME")
            name = table[off:].split(b"\0", 1)[0].decode("ascii", "strict")
            need(re.fullmatch(r"[A-Za-z0-9_.+-]+", name), "RUNTIME_SONAME")
            result.append(name)
        need(ident(before) == ident(os.fstat(fd)), "RUNTIME_ELF_DRIFT")
        return tuple(sorted(result)), interpreter


def elf_needed(path):
    fd = nf(path)
    try: return elf_needed_fd(fd)
    finally: os.close(fd)


class RuntimeView:
    """Actual filesystem boundary; private owned fixture mapping only changes I/O.
    Production always constructs RuntimeView() for the real fixed OS namespace.
    No public flag or runtime manifest can choose another physical root/owner.
    """
    def __init__(self, root="/", *, fixture=False):
        self.root = root.rstrip("/")
        self.fixture = fixture
        if fixture:
            parent = os.path.dirname(root)
            need(parent.startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
                 os.path.dirname(parent) == "/var/tmp" and root == parent + "/runtime",
                 "RUNTIME_FIXTURE_SCOPE")
            st = os.lstat(root)
            need(stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == os.getuid() and
                 stat.S_IMODE(st.st_mode) == 0o700, "RUNTIME_DIRECTORY_CUSTODY")
        else:
            need(root == "/", "RUNTIME_PATH")
        self.owner = os.getuid() if fixture else 0

    def path(self, logical):
        need(type(logical) is str and logical.startswith("/") and
             all(p not in ("", ".", "..") for p in logical.split("/")[1:]), "RUNTIME_LIBRARY_PATHS")
        return self.root + logical

    def exists(self, logical):
        return os.path.lexists(self.path(logical))

    def stat(self, logical):
        return os.lstat(self.path(logical))

    def realpath(self, logical):
        actual = os.path.realpath(self.path(logical))
        need(not self.root or actual.startswith(self.root + "/"), "RUNTIME_ALIAS_ESCAPE")
        return actual[len(self.root):]

    def directories(self):
        physical = self.path(STDLIB)
        need(os.path.isdir(physical), "RUNTIME_STDLIB_ABSENT")
        for base, dirs, leaves in os.walk(physical, followlinks=False):
            logical = base[len(self.root):]
            yield logical, dirs, leaves

    def held(self, logical, row, deadline, *, deadline_ns=None):
        return HeldSource(self.path(logical), row["sha256"], row["bytes"],
                          system=not self.fixture, deadline=deadline, deadline_ns=deadline_ns)


class RuntimeClosure:
    def __init__(self, manifest, holds, view):
        self.manifest, self.holds, self.view = manifest, holds, view
        # A parser fixture can prove byte consumers, never real root/OS authority.
        self.authority = "NOT_PROVEN" if view.fixture else "ROOT_FILE_CUSTODY_ONLY_NOT_PREINTERPRETER_PROOF"

    def __getitem__(self, key):
        return self.manifest[key]

    def close(self):
        values, self.holds = self.holds, {}
        for blob in values.values(): blob.close()

    def check_held(self, deadline, *, deadline_ns=None):
        for path, blob in self.holds.items():
            row = self.manifest["files"][path]
            bounded_fd_bytes(blob.fd, row["sha256"], row["bytes"], sealed=True,
                             deadline=deadline, collect=False, deadline_ns=deadline_ns)


def cache_resolutions(cache, view):
    need(cache[:20] == b"glibc-ld.so.cache1.1" and len(cache) >= 48, "RUNTIME_CACHE_FORMAT")
    count = struct.unpack_from("<I", cache, 20)[0]
    need(count <= 8192 and 48 + 24 * count <= len(cache), "RUNTIME_CACHE_CAP")
    resolutions = {}
    for i in range(count):
        flags, key, value, version, hwcap = struct.unpack_from("<iIIIQ", cache, 48 + 24 * i)
        if flags & 0xff00 != 0x300:
            continue
        def string(offset):
            need(48 + 24 * count <= offset < len(cache) and b"\0" in cache[offset:],
                 "RUNTIME_CACHE_STRING")
            return cache[offset:].split(b"\0", 1)[0].decode("ascii", "strict")
        resolutions.setdefault(string(key), set()).add(view.realpath(string(value)))
    return resolutions


def runtime_preflight(path, sha, deadline, *, view=None, deadline_ns=None):
    # Fixed production path and schema. Only an in-process owned I/O fixture view
    # is permitted; it cannot escape to the CLI or carry OS-authority credit.
    view = RuntimeView() if view is None else view
    need(type(view) is RuntimeView, "RUNTIME_VIEW")
    need(path == RUNTIME, "RUNTIME_PATH")
    raw = seal(view.path(path), sha, PIPE_CAP)
    return runtime_preflight_bytes(raw, deadline, view=view, deadline_ns=deadline_ns)


def runtime_preflight_bytes(raw, deadline, *, view=None, deadline_ns=None):
    """Same complete held-file/ELF/cache consumer; image producer consumes held manifest bytes."""
    view = RuntimeView() if view is None else view
    need(type(view) is RuntimeView, "RUNTIME_VIEW")
    manifest = inert(raw)
    need(type(manifest) is dict and set(manifest) ==
         {"schema", "interpreter", "stdlib", "files", "sonames", "loader_alias"} and
         manifest["schema"] == "friday.browser3.trusted-runtime.v1" and
         manifest["interpreter"] == PYTHON and manifest["stdlib"] == STDLIB, "RUNTIME_SCHEMA")
    files, names = manifest["files"], manifest["sonames"]
    need(type(files) is dict and 0 < len(files) <= 8192 and PYTHON in files and
         type(names) is dict, "RUNTIME_FILE_SET")
    actual = {PYTHON}
    for root, dirs, leaves in view.directories():
        need(before_deadline(deadline,deadline_ns), "PREFLIGHT_TIMEOUT")
        st = view.stat(root)
        need(stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == view.owner and
             not st.st_mode & 0o022, "RUNTIME_DIRECTORY_CUSTODY")
        for name in dirs:
            need(not stat.S_ISLNK(view.stat(root + "/" + name).st_mode), "RUNTIME_SYMLINK")
        for leaf in leaves: actual.add(root + "/" + leaf)
    need(not view.exists("/usr/lib/python314.zip"), "UNKNOWN_RUNTIME_ZIP")
    extras = set(files) - actual
    need(extras and "/etc/ld.so.cache" in extras and not view.exists("/etc/ld.so.preload") and
         all(p == "/etc/ld.so.cache" or p.startswith("/usr/lib/x86_64-linux-gnu/") or
             p.startswith("/usr/lib64/") for p in extras), "RUNTIME_LIBRARY_PATHS")
    need(actual <= set(files), "RUNTIME_MEMBERSHIP")
    holds, total = {}, 0
    try:
        for p, row in files.items():
            need(before_deadline(deadline,deadline_ns), "PREFLIGHT_TIMEOUT")
            need(type(row) is dict and set(row) == {"sha256", "bytes"} and
                 type(row["bytes"]) is int and row["bytes"] >= 0, "RUNTIME_PIN_ROW")
            total += row["bytes"]
            need(total <= 134217728, "RUNTIME_READ_CAP")
            holds[p] = view.held(p, row, deadline, deadline_ns=deadline_ns)
        need(all(type(k) is str and re.fullmatch(r"[A-Za-z0-9_.+-]+", k) and
                 type(v) is str and v in extras for k, v in names.items()), "RUNTIME_SONAME_MAP")
        cache = holds["/etc/ld.so.cache"].bytes(deadline=deadline, deadline_ns=deadline_ns)
        resolutions = cache_resolutions(cache, view)
        need(all(resolutions.get(name) == {target} for name, target in names.items()),
             "RUNTIME_CACHE_RESOLUTION")
        need(holds[PYTHON].bytes(16777216, deadline, deadline_ns=deadline_ns)[:4] == b"\x7fELF", "RUNTIME_INTERPRETER_ELF")
        for p, blob in holds.items():
            need(before_deadline(deadline,deadline_ns), "PREFLIGHT_TIMEOUT")
            dependencies, loader = elf_needed_fd(blob.fd)
            need(all(name in names and names[name] in files for name in dependencies),
                 "UNKNOWN_RUNTIME_DEPENDENCY")
            if loader is not None:
                alias = manifest["loader_alias"]
                need(type(alias) is dict and set(alias) == {"path", "target"} and
                     loader == alias["path"] and alias["target"] in extras and
                     view.realpath(loader) == alias["target"], "RUNTIME_LOADER_PIN")
                for prefix in ("/lib", "/lib64", "/usr/lib64"):
                    if view.exists(prefix):
                        st = view.stat(prefix)
                        need(st.st_uid == st.st_gid == view.owner and
                             (stat.S_ISLNK(st.st_mode) or not st.st_mode & 0o022),
                             "RUNTIME_LOADER_CUSTODY")
        closure = RuntimeClosure(manifest, holds, view)
        closure.check_held(deadline, deadline_ns=deadline_ns)
        return closure
    except BaseException:
        for blob in holds.values(): blob.close()
        raise


def cgroup_values(read):
    expected = {"memory.max": str(INNER_MEMORY), "memory.swap.max": "0", "memory.oom.group": "1",
                "pids.max": "4", "cpu.max": "max 100000"}
    need(all(read(key) == value for key, value in expected.items()), "CGROUP_BOUND_UNKNOWN")
    need(read("cgroup.procs") == "" and read("cgroup.events") == "populated 0\nfrozen 0",
         "CGROUP_NOT_EXCLUSIVE_EMPTY")
    return expected


def cgroup_preflight(directory_fd=None, attach_fd=None):
    fd = nf(CGROUP) if directory_fd is None else os.dup(directory_fd)
    st = os.fstat(fd)
    need(stat.S_ISDIR(st.st_mode) and st.st_uid == st.st_gid == 0 and not st.st_mode & 0o022, "CGROUP_CUSTODY")
    expected = {"memory.max": str(INNER_MEMORY), "memory.swap.max": "0", "memory.oom.group": "1", "pids.max": "4", "cpu.max": "max 100000"}
    def read(key):
        item = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            s = os.fstat(item)
            need(s.st_uid == s.st_gid == 0 and not s.st_mode & 0o002, "CGROUP_LIMIT_CUSTODY")
            if key in expected:
                need(not s.st_mode & 0o022 and not any("acl" in x for x in os.listxattr(item)), "CGROUP_LIMIT_CUSTODY")
            raw = os.read(item, 65537); need(len(raw) <= 65536, "CGROUP_READ_CAP")
            return raw.decode("ascii", "strict").strip()
        finally: os.close(item)
    try:
        cgroup_values(read)
        attach = os.open("cgroup.procs", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=fd) if attach_fd is None else os.dup(attach_fd)
        attached = os.fstat(attach)
        original = os.stat("cgroup.procs", dir_fd=fd, follow_symlinks=False)
        need((attached.st_dev, attached.st_ino) == (original.st_dev, original.st_ino), "CGROUP_ATTACH_CUSTODY")
        return fd, attach, read, expected
    except BaseException:
        if "attach" in locals(): os.close(attach)
        os.close(fd); raise


def terminal_check(raw, mode, expected_controls):
    need(raw.endswith(b"\n") and raw.count(b"\n") == 1, "PARTIAL_OR_MULTIPLE_TERMINAL")
    obj = inert(raw)
    need(type(obj) is dict, "TERMINAL_TYPE")
    if mode == "--controls":
        need(obj.get("state") == "CONTROLS_FINISHED_REQUIRES_INDEPENDENT_CAUSAL_RECEIPT_REVIEW" and
             obj.get("network_effects") == 0 and obj.get("production_target_created") is False and
             obj.get("production_admission") is False, "CONTROL_TERMINAL")
        rows = obj.get("controls")
        need(type(rows) is list and len(rows) == len(expected_controls) and
             {v.get("control") for v in rows if type(v) is dict} == set(expected_controls), "CONTROL_COVERAGE")
        for row in rows:
            meta = expected_controls[row["control"]]
            need(row.get("passed") is True and row.get("coverage") == meta and
                 row.get("forbidden_effects") == {"network": 0, "exec": 0, "original_write": 0}, "CONTROL_RECEIPT")
    else:
        need(obj.get("acceptance_complete") is False and obj.get("reason") is None and
             obj.get("state") != "STOP_UNCONFIRMED" and obj.get("uncertainty_sticky", False) is False,
             "INNER_FAILURE")
        if mode == "--preflight":
            need(obj.get("state") == "REAL_RETAINED_METADATA_PATH_PREFLIGHT_PASSED" and
                 obj.get("network") is False and obj.get("target_created") is False, "PREFLIGHT_TERMINAL")
        else:
            need(obj.get("state") == "BROWSER3_FETCH_FINISHED_WITH_EXPLICIT_ACCEPTANCE_GAPS" and
                 obj.get("body_complete") is True and obj.get("started_routes") == 3 and
                 obj.get("charged_body_bytes", RSS * 9) <= 956301312 and obj.get("peak_workers") <= 3 and
                 obj.get("execution_install_root_gate_credit") is False and
                 len(obj.get("children", [])) == 3 and all(c.get("lifecycle") == "REAPED" for c in obj["children"]) and
                 len(obj.get('launch_journal', [])) == 3 and
                 {c['pid'] for c in obj['launch_journal']} == {c['pid'] for c in obj['children']}, "ACQUISITION_TERMINAL")
    sol076_browser_terminal_join(obj)
    return obj


def proc(pid):
    with open("/proc/%d/stat" % pid, "rb") as f: raw = f.read(65537)
    need(len(raw) <= 65536, "PROC_CAP")
    fields = raw.rsplit(b")", 1)[1].split()
    return int(fields[1]), int(fields[19])


def causal_error_DATA(errors):
    """Lossless bounded DATA for the actual ordinary first/secondary error graph.
    Original objects stay in their owner. Unknown argument types are not called
    or stringified and cannot receive complete-error transport credit.
    """
    nodes, identities, values, value_ids = [], {}, [], {}
    def scalar(value):
        if value is None or type(value) in (str, int, bool):
            return {"kind": "scalar", "value": value}
        if type(value) is float:
            return {"kind":"float64","network_hex":struct.pack("!d",value).hex()}
        if type(value) in (bytes,bytearray):
            key=id(value)
            if key in value_ids:return {"kind":"value_ref","index":value_ids[key]}
            index=len(values);value_ids[key]=index
            values.append({"index":index,"kind":type(value).__name__,"body":bytes(value)})
            return {"kind":"value_ref","index":index}
        if isinstance(value,BaseException):
            return {"kind":"exception","index":visit(value)}
        if type(value) in (tuple,list,dict,set,frozenset):
            key=id(value)
            if key in value_ids:return {"kind":"value_ref","index":value_ids[key]}
            need(len(values)<4096,"ERROR_ARGUMENT_GRAPH_TRANSPORT_BOUND")
            index=len(values);value_ids[key]=index
            row={"index":index,"kind":type(value).__name__,"items":[]};values.append(row)
            row["items"]=([{"key":scalar(k),"value":scalar(v)} for k,v in value.items()]
                if type(value) is dict else [scalar(v) for v in value])
            return {"kind":"value_ref","index":index}
        raise Refused("ERROR_ARGUMENT_TRANSPORT_UNSUPPORTED")
    def visit(error):
        if error is None:
            return None
        if not isinstance(error, BaseException):
            error = Refused(error)
        key = id(error)
        if key in identities:
            return identities[key]
        need(len(nodes) < 4096, "ERROR_GRAPH_TRANSPORT_BOUND")
        index = len(nodes); identities[key] = index
        row = {"index": index, "module": type(error).__module__,
               "type": type(error).__qualname__, "args": None,
               "errno": getattr(error, "errno", None),
               "filename": getattr(error, "filename", None),
               "filename2": getattr(error, "filename2", None),
               "suppress_context": error.__suppress_context__,
               "cause": None, "context": None, "members": []}
        nodes.append(row)
        row["args"]=scalar(error.args)
        row['attributes']=scalar(error.__dict__)
        row['notes']=scalar(getattr(error,'__notes__',[]))
        stock={}
        for name in ('name','path','obj','encoding','object','start','end','reason','verify_code',
                'verify_message','library','msg','lineno','offset','text','end_lineno','end_offset','print_file_and_line'):
            if hasattr(error,name):stock[name]=scalar(getattr(error,name))
        row['stock_attributes']=stock
        row['traceback']=[];trace=error.__traceback__
        while trace is not None:
            need(len(row['traceback'])<4096,'ERROR_TRACEBACK_TRANSPORT_BOUND')
            code=trace.tb_frame.f_code
            row['traceback'].append({'filename':code.co_filename,'name':code.co_name,
                'qualname':code.co_qualname,'line':trace.tb_lineno,'lasti':trace.tb_lasti})
            trace=trace.tb_next
        row['frame_locals_private_not_transported']=True
        row["cause"] = visit(error.__cause__)
        row["context"] = visit(error.__context__)
        if isinstance(error, BaseExceptionGroup):
            row["members"] = [visit(v) for v in error.exceptions]
        return index
    roots = [visit(error) for error in errors]
    return {"schema": "friday.sol064.ordinary-error-graph.v1", "roots": roots,
            "first": roots[0] if roots else None, "nodes": nodes,
            "value_nodes":values,"argument_codec":"typed-full-binary-identity-graph.sol069.v1",
            "complete": True, "original_objects_retained_in_owner": True}


def supervise_owned(spawn, *, wall, reserve, mode, expected_controls, members=None, rss=None,
                    stop=None, cancel=None, now=time.monotonic, postcustody=None, terminal_reserve=0,
                    original_ends=None, generation=None, registration_case=None):
    """Actual bounded pipes, kernel waits, owned pidfds and no implicit success.
    spawn writes only to passed pipe fds. Production spawn imposes kernel limits
    and cgroup membership before exec. No public fixture route invokes this seam.
    """
    started = now(); hard = started + wall; work = hard - reserve
    need(0 <= terminal_reserve < reserve < wall, "OUTER_TIME_BOUNDS")
    if original_ends is not None:
        original_start, work, hard = original_ends
        need(original_start <= started < work < hard and hard-work == reserve,
             "OUTER_ORIGINAL_ENDS_NOT_REFRESHED")
    cleanup_cutoff = hard - terminal_reserve
    registration_cases={"owned_registration_pidfd":"pidfd",
        "owned_registration_proc":"proc","owned_registration_after_spawn":"after_spawn",
        "owned_registration_race":"race","owned_registration_post_custody":"post_custody"}
    need(registration_case is None or registration_case in registration_cases,
        "ORIGINAL_REGISTRATION_CASE_SELECTION")
    registration={"schema":"friday.sol067.owned-registration-instrument.v1",
        "id":registration_case,"generation":generation,"source_instrument_only":True,
        "kernel_failure_credit":False,"observations":[],"fault_applied":0}
    def registration_observe(phase,captured):
        # Original bounded Source seam only. Every observation names the SAME
        # real direct fork/handle and precedes its one selected presentation.
        selected=registration_case is not None and registration_cases[registration_case]==phase
        row={"phase":phase,"captured":captured,"presented":captured,
            "owner":direct["parent"],"owner_birth":direct["parent_birth"],
            "pid":direct["pid"],"generation":generation,"at_ns":str(time.monotonic_ns()),
            "changed":selected,"presentation_error":None}
        registration["observations"].append(row)
        if not selected:return tuple(captured) if phase=="race" else captured
        registration["fault_applied"]+=1
        need(registration["fault_applied"]==1,"ONE_REGISTRATION_PRESENTATION")
        if phase=="race":
            row["presented"]=[captured[0],captured[1]+1]
            return tuple(row["presented"])
        error=(OSError(38,"owned readonly Source pidfd presentation") if phase=="pidfd" else
            Refused({"proc":"OWNED_PROC_FIXTURE","after_spawn":"OWNED_POST_FORK_FIXTURE",
                "post_custody":"OWNED_POST_CUSTODY_FIXTURE"}[phase]))
        row["presentation_error"]={"type":type(error).__name__,"args":list(error.args)}
        raise error
    readout = writeout = readerr = writeerr = None
    # Intentions precede BOTH pipe acquisitions; second-pipe allocation failure
    # still reaches cleanup for every first-pipe endpoint, with one close attempt.
    fd_journal = [{"fd": None, "identity9": None, "close_attempted": False,
                   "closed": False, "close_error": None, "original_error": None}
                  for _ in range(4)]
    # Allocate ownership intention BEFORE a trusted fork. The trusted spawn writes
    # its actual direct-child PID into this exact object immediately after fork,
    # before it performs any /proc, pidfd, pipe, resource or observation operation.
    # This independent record survives registration/validation failure.
    direct = {"pid": None, "fd": None, "birth": None, "parent": os.getpid(),
              "parent_birth": None, "generation": generation,
              "direct": True, "reaped": False, "status": None, "stop_attempted": False,
              "handle_close_attempted": False, "handle_closed": False, "handle_close_error": None}
    pid, records, buffers, openpipes = None, {}, [bytearray(), bytearray()], {}
    seen, eof, overflow = [0,0], [False,False], [False,False]
    read_errors, retention_errors, stream_close_errors = [None,None], [None,None], [None,None]
    reason, uncertain, status, errors, terminal = None, False, None, [], None
    original_errors = []
    def fail(value, sticky=False, original=None):
        nonlocal reason, uncertain
        if reason is None: reason = value
        uncertain = uncertain or sticky
        errors.append(value)
        original_errors.append(original if original is not None else Refused(value))
    def close_slot(slot):
        item = fd_journal[slot]
        if item["fd"] is None or item["close_attempted"]:
            return
        item["close_attempted"] = True
        try:
            os.close(item["fd"])
        except BaseException as exc:
            item["close_error"] = type(exc).__name__
            item["original_error"] = exc
            if slot in (0,2): stream_close_errors[slot//2] = type(exc).__name__
            fail("OUTER_CLOSE_FAILED", True, exc)
        else:
            item["closed"] = True
    def consume(fd, data):
        index = openpipes[fd]
        if not data:
            eof[index] = True; close_slot(2*index); del openpipes[fd]
            return
        seen[index] += len(data)
        try:
            buffers[index].extend(data[:max(0, PIPE_CAP-len(buffers[index]))])
        except BaseException as exc:
            retention_errors[index] = type(exc).__name__
            fail("OUTER_RETENTION_ERROR", True, exc)
        if seen[index] > PIPE_CAP:
            overflow[index] = True; fail("OUTER_PIPE_CAP")
    def drain(fd):
        try:
            consume(fd, os.read(fd,65536))
        except BlockingIOError:
            return
        except BaseException as exc:
            index=openpipes[fd];read_errors[index]=type(exc).__name__
            fail("OUTER_DRAIN_UNCONFIRMED", True, exc)
            close_slot(2*index);del openpipes[fd]
    def track_direct(p):
        # No namespace/PID scan establishes ownership. Only the actual fork
        # intention authorizes this exact unreaped direct-child PID fallback.
        need(type(p) is int and p > 0 and direct["pid"] == p, "SPAWN_NOT_OWNED")
        records[p] = direct
        parent, birth = proc(p)
        parent,birth=registration_observe("proc",[parent,birth])
        need(parent == direct["parent"] and proc(parent)[1] == direct["parent_birth"], "SPAWN_NOT_OWNED")
        direct["birth"] = birth
        try:
            direct["fd"] = os.pidfd_open(p)
            handle_stat=os.fstat(direct["fd"])
            registration_observe("pidfd",{"fd":direct["fd"],"identity9":[str(v) for v in
                (handle_stat.st_dev,handle_stat.st_ino,handle_stat.st_mode,handle_stat.st_uid,
                 handle_stat.st_gid,handle_stat.st_nlink,handle_stat.st_size,handle_stat.st_mtime_ns,
                 handle_stat.st_ctime_ns)]})
        except OSError as exc: raise Refused("OUTER_PIDFD:" + str(exc.errno)) from exc
        need(registration_observe("race",list(proc(p))) == (parent, birth), "OWNERSHIP_RACE")
    def dispose(p, rec):
        if rec["reaped"] or rec.get("retirement_consumed"):
            return
        if rec["stop_attempted"]:
            return
        rec["stop_attempted"] = True
        try:
            # One wait collects retirement. A reaped owner is not signalled again.
            done, value = os.waitpid(p, os.WNOHANG)
            if done:
                rec.update(reaped=True, status=value, retirement_consumed=True)
                return
            if stop is not None:
                need(stop(p, rec), "OUTER_STOP_UNCONFIRMED")
            elif rec["fd"] is not None:
                signal.pidfd_send_signal(rec["fd"], signal.SIGKILL)
            else:
                need(rec is direct and direct["pid"] == p, "OUTER_OWNERSHIP_UNCONFIRMED")
                os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            # ESRCH does not establish reaping; a later wait still must confirm.
            pass
        except BaseException as exc:
            fail(str(exc) if isinstance(exc, Refused) else "OUTER_STOP_EXCEPTION:" + type(exc).__name__, True, exc)
    def reap(p, rec):
        if rec["reaped"]:
            return
        try:
            done, value = os.waitpid(p, os.WNOHANG)
            if done:
                rec.update(reaped=True, status=value, retirement_consumed=True)
        except ChildProcessError as exc:
            # No disappeared /proc entry, guessed parent or inner JSON grants
            # ownership/reap credit. Losing exact wait custody stays sticky.
            fail("OUTER_REAP_CUSTODY_LOST", True, exc)
    try:
        direct["parent_birth"] = proc(direct["parent"])[1]
        for index in range(2):
            pair = os.pipe2(os.O_CLOEXEC)
            fd_journal[2*index]["fd"], fd_journal[2*index+1]["fd"] = pair
            openpipes[pair[0]] = index
            for slot in (2*index,2*index+1):
                st=os.fstat(fd_journal[slot]["fd"])
                fd_journal[slot]["identity9"]=[str(v) for v in
                    (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)]
        readout,writeout,readerr,writeerr=[item["fd"] for item in fd_journal]
        pid = spawn(writeout, writeerr, direct)
        need(pid == direct["pid"], "SPAWN_INTENTION_MISMATCH")
        registration_observe("after_spawn",{"actual_fork_return":pid})
        track_direct(pid)
        close_slot(1); writeout = None; close_slot(3); writeerr = None
        need(reason is None,"OUTER_PARENT_WRITER_CLOSE")
        for fd in openpipes: os.set_blocking(fd, False)
        while openpipes or any(not v["reaped"] for v in records.values()):
            if reason is None and cancel is not None and cancel(): fail("OUTER_OWNER_STOP")
            if reason is None and now() >= work: fail("OUTER_WORK_TIMEOUT")
            if reason is None and rss is not None:
                try: need(rss() <= RSS, "OUTER_RSS_CAP")
                except BaseException as exc: fail(str(exc), True, exc)
            if reason is None and members is not None:
                try:
                    # A cgroup proves containment, not exact clone ownership.
                    # Unknown members are neither adopted nor signalled. A future
                    # authenticated native clone registry is still required for
                    # acquisition workers; this source grants no guessed credit.
                    need(all(p in records for p in members()), "UNKNOWN_CGROUP_MEMBER")
                except BaseException as exc: fail(str(exc), True, exc)
            if reason:
                for p, rec in records.items():
                    dispose(p, rec)
            ready = select.select(list(openpipes), [], [], min(0.01, max(0, hard - now())))[0]
            for fd in ready:
                drain(fd)
            for p, rec in records.items():
                reap(p, rec)
                if p == pid and rec["reaped"]: status = rec["status"]
            if now() >= cleanup_cutoff:
                fail("OUTER_HARD_TIMEOUT", any(not v["reaped"] for v in records.values()) or bool(openpipes)); break
        need(reason is None, reason or "OUTER_FAILURE")
        need(status is not None and os.waitstatus_to_exitcode(status) == 0, "INNER_EXIT")
        need(not buffers[1], "INNER_STDERR")
        need(not openpipes and all(v["reaped"] for v in records.values()), "OUTER_REAP_OR_DRAIN_UNKNOWN")
        need(members is None or members() == [], "OUTER_CGROUP_NOT_EMPTY")
        terminal=terminal_check(bytes(buffers[0]), mode, expected_controls)
        registration_observe("post_custody",{"complete_stdout_bytes":len(buffers[0]),
            "complete_stderr_bytes":len(buffers[1]),"kernel_status":status,
            "actual_terminal":terminal})
        if postcustody is not None:
            postcustody()
        if mode=="--execute":
            need({c["pid"] for c in terminal["children"]} == set(records)-{pid}, "INNER_OWNERSHIP_CLAIM")
    except BaseException as exc:
        fail(str(exc) if isinstance(exc, Refused) else type(exc).__name__, original=exc)
    finally:
        # A failure during spawn/track/drain must still stop only the actual direct child.
        # Never retry the operation that failed during tracking. Use the actual
        # spawn intention, even if spawn itself raised after recording its PID.
        if direct["pid"] is not None and direct["pid"] not in records:
            records[direct["pid"]] = direct
        if pid is None:
            pid = direct["pid"]
        end = min(cleanup_cutoff, now() + reserve)
        for p, rec in records.items():
            dispose(p, rec)
        # Close the parent's write ends before draining exceptional startup.
        close_slot(1);close_slot(3)
        writeout = writeerr = None
        for fd in openpipes:
            try: os.set_blocking(fd, False)
            except OSError as exc: fail("OUTER_PIPE_STATE_UNKNOWN", True, exc)
        while (openpipes or any(not v["reaped"] for v in records.values())) and now() < end:
            try: ready = select.select(list(openpipes), [], [], 0)[0]
            except BaseException as exc:
                fail("OUTER_DRAIN_UNCONFIRMED", True, exc); ready = []
            for fd in ready:
                drain(fd)
            for p, rec in records.items():
                reap(p, rec)
            try: select.select([], [], [], min(0.01, max(0, end - now())))
            except BaseException as exc: fail("OUTER_CLEANUP_WAIT_FAILED", True, exc)
        if any(not v["reaped"] for v in records.values()): fail("OUTER_REAP_UNCONFIRMED", True)
        if openpipes: fail("OUTER_DRAIN_UNCONFIRMED", True)
        for rec in records.values():
            if rec["fd"] is not None and not rec["handle_close_attempted"]:
                rec["handle_close_attempted"]=True
                try: os.close(rec["fd"])
                except BaseException as exc:
                    rec["handle_close_error"]=type(exc).__name__;fail("OUTER_CLOSE_FAILED", True, exc)
                else:rec["handle_closed"]=True
        for slot in range(4):close_slot(slot)
    raw = [bytes(value) for value in buffers]
    streams = [{"stream":index,"raw":raw[index],"raw_size":len(raw[index]),
                "retained_size":len(raw[index]),"cap":PIPE_CAP,"total_seen":seen[index],
                "eof":eof[index],"overflow":overflow[index],"read_error":read_errors[index],
                "retention_error":retention_errors[index],"close_error":stream_close_errors[index],
                "sha256":hashlib.sha256(raw[index]).hexdigest(),"hash_only":False,
                "original_complete":eof[index] and not overflow[index] and not read_errors[index] and
                    not retention_errors[index] and not stream_close_errors[index] and
                    fd_journal[2*index]["closed"] and seen[index]==len(raw[index])}
               for index in range(2)]
    error_graph = None
    try:error_graph=causal_error_DATA(original_errors)
    except BaseException as exc:fail("OUTER_ERROR_GRAPH_TRANSPORT_INCOMPLETE",True,exc)
    return {"state": "STOP_UNCONFIRMED" if uncertain else ("OUTER_FAILED" if reason else "OUTER_BOUNDED_DRAINED_FINISHED"),
        "reason": reason, "body_complete": False, "acceptance_complete": False,
        "errors": errors,"original_errors":original_errors,"error_graph":error_graph,
        "registration_instrument":registration,
        "fd_journal":fd_journal,"streams":streams,
        "terminal_completion": reason is None and not uncertain, "uncertainty_sticky": uncertain,
        "elapsed_sec": now() - started, "stdout_bytes": len(buffers[0]), "stderr_bytes": len(buffers[1]),
        "stdout": bytes(buffers[0]), "stderr": bytes(buffers[1]),
        "stdout_sha256": hashlib.sha256(buffers[0]).hexdigest(),
        "stderr_sha256": hashlib.sha256(buffers[1]).hexdigest(),
        "inner_terminal_sha256":hashlib.sha256(buffers[0]).hexdigest(),
        "inner_terminal":terminal if reason is None and not uncertain else None,
        "owned": [{"pid": p, "owner": r["parent"], "owner_birth":r["parent_birth"],
                   "generation":r["generation"],"birth":r["birth"],
                   "reaped": r["reaped"], "status": r["status"],
                   "handle_close_attempted":r["handle_close_attempted"],
                   "handle_closed":r["handle_closed"],"handle_close_error":r["handle_close_error"],
                   "retirement_consumed": r.get("retirement_consumed", False)} for p, r in records.items()]}


def write_terminal(result, fd=1, trace=None, deadline=None, *, deadline_ns=None):
    """Outer emission is itself a finite drained-pipe requirement."""
    try:
        data = json.dumps(result, separators=(",", ":")).encode() + b"\n"
        need(len(data) <= PIPE_CAP and stat.S_ISFIFO(os.fstat(fd).st_mode), "OUTER_TERMINAL_SINK")
        preowned = sol069_preowned_backing(data)
        need(bytes(preowned["view"]) == data and preowned["bound_before_channel_write"]
             and preowned["receiver_accepted"] is False and preowned["exit_is_handover"] is False,
             "OUTER_PREOWNED_BEFORE_EXISTING_READER")
        data = preowned["backing"]
        os.set_blocking(fd, False)
        if deadline_ns is not None:
            need(type(deadline_ns) is int and deadline_ns>=0,"EXACT_DEADLINE_NS")
            end_ns=min(time.monotonic_ns()+1000000000,deadline_ns)
        else:end = min(time.monotonic() + 1, deadline) if deadline is not None else time.monotonic() + 1
        while data:
            need(time.monotonic_ns()<end_ns if deadline_ns is not None else time.monotonic()<end, "OUTER_TERMINAL_TIMEOUT")
            try: n = os.write(fd, data)
            except BlockingIOError:
                if deadline_ns is not None:select.select([], [fd], [], min(10000000,max(0,end_ns-time.monotonic_ns()))/1000000000)
                else:select.select([], [fd], [], min(0.01, max(0, end - time.monotonic())))
                continue
            need(n > 0, "OUTER_TERMINAL_SHORT_WRITE"); data = data[n:]
        return True
    except BaseException as exc:
        if trace is not None: trace["cause"] = str(exc) if isinstance(exc, Refused) else type(exc).__name__ + ":" + str(getattr(exc,"errno",None))
        return False


class RootHeldBlob:
    def __init__(self, fd, sha):
        self.fd, self.sha = fd, sha
        bounded_fd_bytes(fd, sha, PIPE_CAP, sealed=True, root=True)


def main():
    # The native root launcher must establish outer limits/watchdog and consume
    # this exact held source BEFORE starting Python. This Python check cannot
    # retroactively certify startup. Its root capsule is mandatory, never minted.
    need(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode and
         dict(os.environ) == ENV, "ISOLATED_ENV")
    need(len(sys.argv) == 7 and sys.argv[1] in ("--controls", "--preflight", "--execute"),
         "OUTER_PUBLIC_ARGS")
    mode, supervisor_sha, executor_sha, controls_sha, bill_sha, capsule_sha = sys.argv[1:]
    try:
        capsule_raw = bounded_fd_bytes(100, capsule_sha, 65536, sealed=True, root=True)
    except OSError as exc:
        raise Refused("EXTERNAL_PREINTERPRETER_LAUNCH_REQUIRED") from exc
    capsule = inert(capsule_raw)
    need(type(capsule) is dict and type(capsule.get("sources")) is dict, "HELD_CAPSULE_IDENTITY")
    rows = capsule["sources"]
    for role, (fd, path) in SOURCE_ROLES.items():
        need(role in rows and type(rows[role]) is dict and rows[role].get("fd") == fd and
             rows[role].get("path") == path, "HELD_SOURCE_ROW")
    for role, wanted in (("supervisor", supervisor_sha), ("executor", executor_sha),
                          ("controls", controls_sha), ("bill", bill_sha)):
        need(rows[role].get("sha256") == wanted, "EXTERNAL_SOURCE_PIN")
    executor_bytes = bounded_fd_bytes(101, executor_sha, PIPE_CAP, sealed=True, root=True)
    bounded_fd_bytes(103, supervisor_sha, PIPE_CAP, sealed=True, root=True)
    x = types.ModuleType("a061_held_root_executor"); x.__file__ = EXECUTOR
    exec(compile(executor_bytes, EXECUTOR, "exec"), x.__dict__)
    admitted = x.install_held_capsule()
    need(admitted["mode"] == mode, "HELD_CAPSULE_MODE")
    hard, work = admitted["hard"], admitted["work"]
    bounds_seen={kind:resource.getrlimit(kind) for kind in
                 (resource.RLIMIT_AS,resource.RLIMIT_CPU,resource.RLIMIT_NOFILE)}
    need(all(0 <= soft <= hard_bound and hard_bound != resource.RLIM_INFINITY for soft,hard_bound in bounds_seen.values()) and
         bounds_seen[resource.RLIMIT_AS][1] <= OUTER_AS and
         bounds_seen[resource.RLIMIT_CPU][1] <= (180 if mode == "--controls" else 1200) and
         bounds_seen[resource.RLIMIT_NOFILE][1] <= 1024,
         "OUTER_PREINTERPRETER_LIMITS")
    need(stat.S_ISFIFO(os.fstat(1).st_mode), "OUTER_TERMINAL_SINK")
    plan = inert(x.HELD_BYTES[BILL])
    x.compile_bill(plan)
    adapter = NativeLaunchAdapter(110, 111, launcher_sha=admitted["native_launcher_sha256"],
                                  runtime_sha=admitted["runtime_sha256"], deadline=work)
    held = {role: RootHeldBlob(fd, rows[role]["sha256"]) for role, (fd, path) in SOURCE_ROLES.items()}
    # Root passes these actual descriptors; no reopen of cgroup attachment or
    # immutable code/runtime is used to launch the generated child.
    group, attach, read, bounds = cgroup_preflight(120, 121)
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    filesystem = ctypes.create_string_buffer(256)
    need(libc.fstatfs(group, filesystem) == 0 and
         struct.unpack_from("<q", filesystem.raw)[0] == 0x63677270, "CGROUP_KERNEL_FILESYSTEM")
    need(libc.prctl(36, 1, 0, 0, 0) == 0, "SUBREAPER_REFUSED")
    cancelled, previous, result = [False], {}, None
    try:
        def owner_stop(signum, frame): cancelled[0] = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, owner_stop)
        def spawn(out, err, intention):
            need(time.monotonic() < work and all(read(k) == v for k, v in bounds.items()) and
                 read("cgroup.procs") == "", "CGROUP_PREEXEC_DRIFT")
            child = os.fork()
            if child:
                intention["pid"] = child
                return child
            try:
                # Kernel envelope applies even to native launcher startup.
                resource.setrlimit(resource.RLIMIT_AS, (INNER_MEMORY, INNER_MEMORY))
                resource.setrlimit(resource.RLIMIT_CPU, (180 if mode == "--controls" else 1200,) * 2)
                resource.setrlimit(resource.RLIMIT_FSIZE, (2147483648,) * 2)
                resource.setrlimit(resource.RLIMIT_NOFILE, (512,) * 2)
                os.umask(0o077)
                payload = str(os.getpid()).encode()
                need(os.write(attach, payload) == len(payload), "CGROUP_ATTACH_SHORT")
                need(str(os.getpid()) in read("cgroup.procs").split(), "CGROUP_ATTACH_UNKNOWN")
                null = os.open("/dev/null", os.O_RDONLY | os.O_NOFOLLOW); os.dup2(null, 0); os.close(null)
                adapter.handoff(held, 100, out, err, group, attach)
            except BaseException:
                os._exit(125)
        def members():
            raw = read("cgroup.procs")
            need(all(re.fullmatch("[1-9][0-9]*", p) for p in raw.split()), "CGROUP_PID_FORMAT")
            return sorted(int(p) for p in raw.split())
        def rss():
            total = 0
            for p in [os.getpid()] + members():
                try:
                    with open("/proc/%d/status" % p, "rt", encoding="ascii") as stream:
                        text = stream.read(65537)
                except FileNotFoundError:
                    continue
                need(len(text) <= 65536, "PROC_RSS_CAP")
                match = re.search(r"^VmRSS:\s+(\d+) kB$", text, re.M)
                if match: total += int(match[1]) * 1024
                else: need(re.search(r"^State:\s+Z", text, re.M), "PROC_RSS_UNKNOWN")
            return total
        def postcustody():
            need(read("cgroup.events") == "populated 0\nfrozen 0" and
                 all(read(k) == v for k, v in bounds.items()), "CGROUP_FINAL_UNKNOWN")
            for role, blob in held.items():
                bounded_fd_bytes(blob.fd, blob.sha, PIPE_CAP, sealed=True, root=True, deadline=hard - 1)
        result = supervise_owned(spawn, wall=hard-time.monotonic(),
            reserve=10 if mode == "--controls" else 60, terminal_reserve=1,
            mode=mode, expected_controls=plan["control_map"], members=members, rss=rss,
            cancel=lambda: cancelled[0], postcustody=postcustody)
    finally:
        for sig, handler in previous.items():
            try: signal.signal(sig, handler)
            except BaseException:
                if result is not None:
                    result.update(state="STOP_UNCONFIRMED", terminal_completion=False,
                                  uncertainty_sticky=True, reason=result["reason"] or "OUTER_SIGNAL_RESTORE")
        for fd in (attach, group):
            try: os.close(fd)
            except OSError:
                if result is not None:
                    result.update(state="STOP_UNCONFIRMED", terminal_completion=False,
                                  uncertainty_sticky=True, reason=result["reason"] or "OUTER_CLOSE_FAILED")
    emitted = write_terminal(result, deadline=hard)
    return 0 if emitted and result["terminal_completion"] else 2


legacy_outer_main = main


def load_held_module(name, fd, sha, path):
    record={'name':name,'fd':fd,'sha256':sha,'path':path,'raw':None,'module':None,'original_error':None}
    A067_START_CUSTODY.setdefault('module_loads',[]).append(record)
    raw = bounded_fd_bytes(fd, sha, 1048576, sealed=True, root=True)
    record['raw']=raw
    module = types.ModuleType(name); module.__file__ = path
    record['module']=module
    sys.modules[name] = module
    try:exec(compile(raw, path, "exec"), module.__dict__)
    except BaseException as exc:
        record['original_error']=exc;A067_START_CUSTODY['original_errors'].append(exc);raise
    return module


def main():
    """The actual C bootstrap has already bounded/verified this interpreter.
    Public startup uses its single capsule pin and exact held source descriptors.
    No fixture flag, path override, native-method adapter or named source load.
    """
    need(len(sys.argv) == 2 and re.fullmatch("[0-9a-f]{64}", sys.argv[1]), "A061_PUBLIC_ARGS")
    raw = bounded_fd_bytes(100, sys.argv[1], 4096, sealed=True, root=True)
    A067_START_CUSTODY['original_capsule_raw']=raw
    # Fixed schema: first source pin is byte240; contract role16 is byte752.
    need(len(raw) == 848 and raw[:8] == b"FRA061C1", "A061_CAPSULE_SIZE")
    contract_sha = raw[752:784].hex()
    c = load_held_module("a061_contract", 118, contract_sha, STEM + "-CONTRACT.py")
    try:
        cap = c.startup(sys.argv[1])
    except c.Refused:
        if c.START_REFUSAL is None: raise
        write_terminal(c.START_REFUSAL)
        return 2
    native = c.native_bridge(cap)
    x = load_held_module("a061_executor", 101, cap["pins"]["executor"], EXECUTOR)
    admitted = x.install_held_capsule()
    x.load_helpers()
    plan_raw = x.HELD_BYTES[BILL]
    plan = x.m.inert_json(plan_raw, 262144); x.compile_bill(plan)
    result, restore, wire_result, ctr = None, None, None, None
    try:
        if cap["mode_id"] == 1 or cap.get('source_instrument') is not None:
            restore = c.enable_fixture_forks(cap, native)
            ctr = load_held_module("a061_controls", 102, cap["pins"]["controls"], CONTROLS)
            ctr.x, ctr.s = x, sys.modules[__name__]
            ctr.PINS.update(executor=cap["pins"]["executor"], controls=cap["pins"]["controls"], supervisor=cap["pins"]["supervisor"])
            ctr.HELD_BYTES.update(x.HELD_BYTES)
            def deny(event, args):
                if event.startswith(("socket.", "subprocess.")) or event in ("os.exec", "os.posix_spawn", "ctypes.dlopen"):
                    raise x.m.Refused("OFFLINE_NETWORK_EXEC_DENIED")
            sys.addaudithook(deny)
            result = ctr.controls(plan, plan_raw)
            if result.get("schema")=="friday.a158.whole216-controller-result.v1":
                wire_result=ctr.a064_result_packet(result)
            if result.get("schema")=="friday.a158.whole216-controller-result.v1" and result.get("terminal_completion") is not True:
                need(result.get("body_complete") is False and result.get("acceptance_complete") is False and
                    result.get("SourceReady") is False and result.get("GO") is False,"A158_NO_UNEXECUTED_OR_REFUSAL_CREDIT")
                if restore is not None:
                    pending,restore=restore,None;pending()
                return 2 if x.emit_terminal(wire_result,None) else 2
            if result.get("native_registry_only") and result.get("terminal_completion") is not True:
                need(result.get("all216")=="NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION" and
                    result.get("full_authoritative_registry_controls")=="SOURCE_INCOMPLETE" and
                    result.get("acceptance_complete") is False and result.get("current_GO") is False,
                    "A079_NO_FULL_CONTROL_OR_WAIVER_CREDIT")
                if restore is not None:
                    pending,restore=restore,None;pending()
                return 2 if x.emit_terminal(result,None) else 2
            c.command(cap, 12, 13)
        elif cap["mode_id"] == 2:
            result = x.preflight(plan)
            if result.get("reason") is None: c.command(cap, 12, 13)
        else:
            owned = load_held_module("a061_owner", 127, cap["pins"]["owned_consumer"], STEM + "-OWNER.py")
            run = x.adopt_held_deadline(x.make_run()); x.ACTIVE_RUN = run
            run.native_owner = owned.NativeOwner(x, cap, native)
            observed = c.production_resources(cap)
            context = x.ca_context(run)
            if cap["mode_id"] == 4:
                fixture = x.m.inert_json(c.held(119, cap["pins"]["fixture"], 1048576), 1048576)
                need(type(fixture) is dict and fixture.get("schema") == "friday.a061.benign-fixture.v1" and
                     set(fixture) == {"schema", "held_root", "inventory", "target", "responses"}, "A061_BENIGN_FIXTURE_SCHEMA")
                parent = os.path.dirname(fixture["held_root"])
                need(parent.startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
                     os.path.dirname(parent) == "/var/tmp" and fixture["held_root"] == parent + "/held" and
                     fixture["target"] == parent + "/fresh-three" and len(fixture["responses"]) == 3,
                     "A061_BENIGN_OWNED_SCOPE")
                held = x.RetainedTree(fixture["held_root"], fixture["inventory"], run)
                target = fixture["target"]
            else:
                _, _, _, held = x.prepare(plan, run); target = x.ROOT
            result = x.execute_core(plan, plan_raw, target, run, held, context, observed, x.m.worker)
        if cap["mode_id"]==4:
            # A fully reaped benign negative is a completed control contour.
            # The separate driver owns its independent exact cause/byte oracle;
            # an acquisition failure is never transformed into body success.
            need(type(result) is dict and result.get("acceptance_complete") is False and
                 result.get("execution_install_root_gate_credit") is False and
                 result.get("started_routes")==3 and len(result.get("children",[]))==3 and
                 all(v.get("lifecycle")=="REAPED" for v in result["children"]) and
                 not result.get("uncertainty_sticky") and
                 result.get("charged_body_bytes",956301313)<=956301312,
                 "BENIGN_OWNED_TERMINAL")
        elif cap["mode_id"]==1 and result.get("schema")=="friday.a091.inherited-owner-result.v1":
            need(result.get("case")=="inherited_start_owner" and len(result.get("rows",[]))==3 and
                result.get("terminal_completion") is True and result.get("all216")=="SEPARATE_UNRESOLVED_NOT_RUN" and
                result.get("body_complete") is False and result.get("acceptance_complete") is False and result.get("GO") is False,
                "A091_ORDINARY_OWNER_PAIR_NOT_WHOLE_BROWSER")
        elif (cap["mode_id"]==1 or cap.get('source_instrument') is not None) and result.get("schema")=="friday.a158.whole216-controller-result.v1":
            original_meta=plan["control_map"][result["id"]]
            domain=("coordinator_targets" if original_meta["owned_children"] else
                "inner_resource_observer" if original_meta["consumer"] in ("G1.resources","acquisition_resources") else "delegated_consumer")
            need(result.get("route_domain")==domain and
                result.get("child_created") is (domain!="inner_resource_observer") and
                result.get("passed") is True and result.get("producer_called") is True and
                result.get("terminal_completion") is True and result.get("whole216_credit") is False and
                result.get("body_complete") is False and result.get("acceptance_complete") is False and
                result.get("SourceReady") is False and result.get("GO") is False and result.get("F10_waiver") is False,
                "A158_SCOPED_CAUSAL_ROUTE_NOT_WHOLE_BROWSER")
        elif cap["mode_id"]==1 and result.get("native_registry_only"):
            need(result.get("case") in ("positive","register_delayed_ACK","abort_positive") and len(result.get("ordinary_rows",[]))==3 and
                result.get("terminal_completion") is True and result.get("all216")=="NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION" and
                result.get("full_authoritative_registry_controls")=="SOURCE_INCOMPLETE" and
                result.get("acceptance_complete") is False and result.get("current_GO") is False,
                "A079_SCOPED_ORDINARY_POSITIVE_NOT_WHOLE_BROWSER")
        else:
            terminal_check(json.dumps(result,separators=(",",":")).encode()+b"\n",cap["mode"],plan["control_map"])
        need(x.ACTIVE_RUN is None or not x.ACTIVE_RUN.uncertain, "A061_UNKNOWN_INNER_RESULT")
        c.check_loaded_runtime(cap)
        c.command(cap, 9, 10)
        if restore is not None:
            # Once-only restoration belongs BEFORE final emission, not after
            # success bytes. A failure enters the same original-custody handler.
            pending,restore=restore,None;pending()
        if result.get('schema')=='friday.a158.whole216-controller-result.v1':
            # Rebuild from the same originals AFTER final runtime/Root finish.
            # No byte cache from before these last refusal/uncertainty points.
            wire_result=ctr.a064_result_packet(result)
        # Root native owner drains this exact line and validates all registration,
        # reap/pipe/exit/resource conditions before its separate finite terminal.
        return 0 if x.emit_terminal(wire_result if wire_result is not None else result, x.ACTIVE_RUN) else 2
    except BaseException as exc:
        if result is not None and result.get("schema")=="friday.a158.whole216-controller-result.v1" and ctr is not None:
            # Runtime/semantic/finish refusal preserves the same actual raw
            # receipt/error objects; no replacement hash-only terminal.
            result.update(passed=False,terminal_completion=False,disposition="REFUSED_SCOPED_RECEIPT")
            result["primary_error"]=result.get("primary_error") or x.cause(exc)
            result["original_error"]=result.get("original_error") or exc
            result.setdefault("original_errors",[]).append(exc)
            wire_result=ctr.a064_result_packet(result)
            return 2 if x.emit_terminal(wire_result,x.ACTIVE_RUN) else 2
        raise
    finally:
        if restore is not None: restore()


A067_START_CUSTODY={'schema':'friday.sol067.supervisor-entry-custody.v1',
    'original_errors':[],'owner':os.getpid(),'terminal_completion':False}
a067_main_body=main

def main():
    try:return a067_main_body()
    except BaseException as exc:
        A067_START_CUSTODY['original_errors'].append(exc)
        raise

"""SOL069 inert Source. One bounded full frame; never a prefix/hash body.
Representation limits below are NOT proofs of fit for all required stock inputs.
No filesystem backing, grant, channel, role, clock, or cap is added.
"""
SOL069_EXPANDED_BYTES = 16 * 1048576

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


if __name__ == "__main__":
    try: sys.exit(main())
    except SystemExit: raise
    except BaseException as exc:
        A067_START_CUSTODY['original_errors'].append(exc)
        try:graph=causal_error_DATA(A067_START_CUSTODY['original_errors'])
        except BaseException as recorder:
            A067_START_CUSTODY['original_errors'].append(recorder);graph=None
        write_terminal({"state": "OUTER_SUPERVISION_FAILED", "body_complete": False,
                        "terminal_completion": False, "acceptance_complete": False,
                        "original_error_graph":graph,"complete_error_transport":graph is not None,
                        "reason": str(exc) if isinstance(exc, Refused) else type(exc).__name__})
        sys.exit(2)
