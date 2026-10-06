"""Source only: qualified stock caller of actual held110 --held-a061.

The caller consumes independently supplied Root-selected typed admission and
oracle read-only descriptors. It cannot create Root, images, grants or receipts.
All its Source and cases are NOT_RUN. This corpus does not close the receiving
side Root-registry residual recorded in A087-EXPECTED-CONTROLS.json.
"""
import base64
import fcntl
import hashlib
import json
import os
import resource
import select
import signal
import stat
import struct
import time

SOURCES=(101,102,103,104,105,106,107,108,109,110,112,113,114,115,116,117,118,119,127)
PACKET=struct.Struct("<8s32sIIIIiiiiQQQ")
PAYLOADS=tuple(("ordinary-owned-a087/%d\n"%i).encode("ascii") for i in range(3))

def need(ok,cause):
    if not ok:raise RuntimeError(cause)


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


def json_DATA(raw):
    return sol143_native_check(_sol143_prior_json_DATA(raw))

def a067_full_DATA_decode(raw,parse=None):
    return sol143_native_check(_sol143_prior_a067_full_DATA_decode(raw,parse))

def _sol143_prior_json_DATA(raw):
    def unique(items):
        out={}
        for key,value in items:need(key not in out,"A087_DUPLICATE_KEY");out[key]=value
        return out
    value=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(RuntimeError("A087_NONFINITE")))
    sol076_receiver_guard(value)
    sol076_browser_terminal_join(value)
    return value

def identity(st):
    return (st.st_dev,st.st_ino,st.st_mode,st.st_uid,st.st_gid,st.st_nlink,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

def proc(pid,custody=None):
    path="/proc/%d/stat"%pid
    if custody is None:fd=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW);slot=None
    else:fd,slot=custody.open_metadata(path)
    error=None
    try:raw=os.read(fd,4097)
    except BaseException as exc:error=exc;raise
    finally:
        if custody is None:os.close(fd)
        elif not custody.close(slot) and error is None:raise RuntimeError("A153_PROC_METADATA_CLOSE")
    need(0<len(raw)<=4096 and b")" in raw,"A087_ACTUAL_PROC")
    fields=raw.rsplit(b")",1)[1].split();need(len(fields)>=20,"A087_PROC_SIZE")
    return int(fields[1]),int(fields[19])

def pidfd_pid(fd,custody=None):
    path="/proc/self/fdinfo/%d"%fd
    if custody is None:read=os.open(path,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW);slot=None
    else:read,slot=custody.open_metadata(path)
    error=None
    try:raw=os.read(read,4097)
    except BaseException as exc:error=exc;raise
    finally:
        if custody is None:os.close(read)
        elif not custody.close(slot) and error is None:raise RuntimeError("A153_PIDFD_METADATA_CLOSE")
    need(len(raw)<=4096,"A087_FDINFO_BOUND")
    values=[int(v[4:].strip()) for v in raw.splitlines() if v.startswith(b"Pid:")]
    need(len(values)==1,"A087_ACTUAL_PIDFD");return values[0]

def pinned(fd,expected,maximum):
    need(type(fd) is int and fd>=0 and type(expected) is str and len(expected)==64 and
        all(v in "0123456789abcdef" for v in expected),"A087_EXTERNAL_PIN_TYPE")
    before=os.fstat(fd)
    need(stat.S_ISREG(before.st_mode) and before.st_uid==before.st_gid==0 and
        0<=before.st_size<=maximum and fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY and
        fcntl.fcntl(fd,fcntl.F_GET_SEALS)==15,"A087_ACTUAL_ROOT_READONLY_SEALED_INPUT")
    raw=os.pread(fd,before.st_size+1,0)
    need(len(raw)==before.st_size and hashlib.sha256(raw).hexdigest()==expected and identity(os.fstat(fd))==identity(before),
        "A087_COMPLETE_INDEPENDENT_PINNED_BYTES")
    return raw,(identity(before),expected)

def prepare(bundle):
    need(type(bundle) is dict and set(bundle)=={"schema","fds","capsule_sha256","source_sha256","image_sha256",
        "admission_fd","admission_sha256","oracle_fd","oracle_sha256","case"} and
        bundle["schema"]=="friday.a087.stock-public-call.v1","A087_TYPED_PUBLIC_INPUT")
    fds=bundle["fds"];expected=bundle["source_sha256"]
    need(type(fds) is dict and set(fds)==set((100,111,120,121,122)+SOURCES) and set(expected)==set(SOURCES),"A087_FULL_FD_ROLE_GRAPH")
    admission,admission_snap=pinned(bundle["admission_fd"],bundle["admission_sha256"],65536)
    admission=json_DATA(admission)
    need(set(admission)=={"schema","expected_driver","resource_class","own_reply_helper","own_real_uid_transition","own_real_gid_transition",
        "own_local_syscall_restriction","Root_selected_expected_inputs"} and
        admission["schema"]=="friday.a087.external-stock-actor-admission.v1" and
        admission["resource_class"]=="ordinary180_reserve10_outer1_inner4_rss256MiB" and
        admission["Root_selected_expected_inputs"] is True,"A087_EXTERNAL_TYPED_ADMISSION")
    parent,birth=proc(os.getpid())
    need(admission["expected_driver"]=={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()} and
        os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0,"A087_ACTUAL_SELECTED_STOCK_ACTOR")
    case=bundle["case"]
    if "origin_pid" in case:need(admission["own_reply_helper"] is True,"A087_SEPARATE_HELPER_ADMISSION")
    if "origin_uid" in case:need(admission["own_real_uid_transition"] is True,"A087_SEPARATE_REAL_UID_ADMISSION")
    if "origin_gid" in case:need(admission["own_real_gid_transition"] is True,"A087_SEPARATE_REAL_GID_ADMISSION")
    if case in ("signal_denied","close_denied"):need(admission["own_local_syscall_restriction"] is True,"A087_SEPARATE_OWN_RESTRICTION_ADMISSION")
    raw_oracle,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],131072)
    matrix=json_DATA(raw_oracle)
    need(matrix["schema"]=="friday.a087.independent-public-oracles.v1" and matrix["full_scoped_cause_closed"] is False and
        matrix["source_ready"] is False and matrix["GO"] is False,"A087_EXPECTED_SCOPE_NO_WAIVER")
    matches=[row for row in matrix["rows"] if row["case"]==case];need(len(matches)==1,"A087_EXACT_PRESELECTED_CASE")
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        pin=bundle["capsule_sha256"] if role==100 else bundle["image_sha256"] if role==111 else expected[role]
        raw,snap=pinned(fds[role],pin,16777216 if role in (110,111) else 1048576)
        raws[role]=raw;snapshots[role]=snap
    cap=raws[100]
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,1,1,1000,1000,19),"A087_PREREQUISITE_VALID_CAPSULE")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    need(start<=time.monotonic_ns() and time.monotonic_ns()+30*10**9<=work<hard and hard-start==180*10**9 and hard-work==10*10**9,"A087_REAL_LIFETIME_AND_PREREQUISITE_HEADROOM")
    need(cap[112:144]!=b"\0"*32 and cap[144:176].hex()==bundle["image_sha256"] and
        cap[176:208].hex()==expected[117] and cap[208:240].hex()==expected[116] and
        all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),"A087_ENTIRE_CAPSULE_SOURCE_IMAGE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,
            outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A087_ACTUAL_SELECTED_ROOT_GROUPS")
    ordinary={"schema":"friday.a087.native-public-input.v1","case":case,
        "workers":[{"payload":v.decode("ascii"),"exit":23+i} for i,v in enumerate(PAYLOADS)]}
    need(raws[119]==(json.dumps(ordinary,separators=(",",":"))+"\n").encode("ascii"),"A087_EXACT_CANONICAL_BENIGN_INPUT")
    snapshots["admission"]=(admission_snap,bundle["admission_fd"]);snapshots["oracle"]=(oracle_snap,bundle["oracle_fd"])
    return snapshots,work,hard,matches[0]

def complement(bundle,snapshots):
    for role,value in snapshots.items():
        if type(role) is int:
            snap=value;fd=bundle["fds"][role]
        else:snap,fd=value
        raw,_=pinned(fd,snap[1],16777216 if role in (110,111) else 1048576)
        need(identity(os.fstat(fd))==snap[0],"A087_WHOLE_SAME_HELD_COMPLEMENT")

def evidence(trace,stage,errno,phase=None):
    need(trace["version"]==1 and trace["stage"]==stage and trace["primitive_errno"]==errno and
        (phase is None or trace["phase"]==phase) and len(bytes.fromhex(trace["expected"]))==96 and
        len(bytes.fromhex(trace["observed"]))==96,"A087_EXACT_ACTUAL_PRIMITIVE_PHASE_STAGE")
    need(trace["rights_close_errno"]==0 and trace["rights_closed"]==trace["rights"],"A087_ALL_ACTUAL_RECEIVED_RIGHTS_CLOSED")

def reply_oracle(trace,case,ctl,owned):
    need((trace["expected_pid"],trace["expected_uid"],trace["expected_gid"])==(owned["pid"],0,0),"A087_EXACT_EXPECTED_PID_UID_GID")
    expected=PACKET.unpack(bytes.fromhex(trace["expected"]));observed=PACKET.unpack(bytes.fromhex(trace["observed"]))
    if "lost_ACK" in case:
        need((trace["observed_pid"],trace["observed_uid"],trace["observed_gid"])==(-1,-1,-1) and
            bytes.fromhex(trace["observed"])==bytes(96),"A087_ACTUAL_LOST_REPLY_NO_FABRICATED_OBSERVATION");return
    need((trace["observed_pid"],trace["observed_uid"],trace["observed_gid"])==
        ((ctl["reply_pid"],ctl["reply_uid"],ctl["reply_gid"]) if ctl["reply_used"] else (owned["pid"],0,0)),"A087_ACTUAL_FULL_CONTROLLER_ORIGIN_CREDENTIALS")
    wanted=list(expected)
    if "owner_birth" in case:wanted[11]+=1
    elif "owner" in case:wanted[7]+=1
    if "session" in case:wanted[1]=bytes((expected[1][0]^1,))+expected[1][1:]
    if "role" in case:wanted[5]+=1
    if "sequence_replay" in case:wanted[4]-=1
    elif "sequence_future" in case or case=="start_sequence":wanted[4]+=1
    if "deadline" in case:wanted[12]-=1
    if "invalid_ACK" in case:wanted[3]=11
    need(observed==tuple(wanted),"A087_SINGLE_CONTROLLED_ARGUMENT_AND_EXACT_PACKET_COMPLEMENT")
    need(trace["rights"]==(2 if case=="ack_many_rights" else 1 if "rights" in case else 0),"A087_EXACT_ACTUAL_RIGHT_COUNT")

def oracle(outer,expected,owned,inner_stdout=None):
    case=expected["case"];positive=expected["completion"]
    need(outer["state"]==expected["outer_state"] and outer["reason"]==expected["outer_cause"] and
        outer["terminal_completion"] is positive and outer["uncertainty_sticky"] is expected["outer_uncertainty"] and
        outer["registered_workers"]==expected["root_registered_workers"] and outer["reaped_workers"]==expected["root_borrowed_reaped_workers"] and
        outer["registry_next_sequence"]==expected["root_sequence"] and outer["coordinator_kernel_status_known"] is True and
        outer["borrowed_status_kernel_credit"] is False and outer["body_complete"] is False and outer["acceptance_complete"] is False,
        "A087_FULL_ROOT_PHASE_COUNT_CAUSE_TERMINAL")
    ctl=outer["public_control"]
    need(ctl["case"]==case and ctl["origin_pid"]==owned["pid"] and ctl["origin_parent"]==owned["owner"] and
        ctl["origin_birth"]==owned["birth"] and ctl["coordinator_pid"]>0 and ctl["coordinator_birth"]>0,"A087_INDEPENDENT_ACTUAL_ORIGIN_BINDINGS")
    helper=1 if "origin_pid" in case else 0;plain=1 if "rights" in case else 0
    need(ctl["helper_created"]==ctl["helper_reaped"]==ctl["helper_closed"]==helper and
        ctl["plaintext_created"]==ctl["plaintext_closed"]==plain and
        ctl["credential_transitions"]==(1 if "origin_uid" in case or "origin_gid" in case else 0),"A087_REAL_HELPER_PLAINTEXT_CREDENTIAL_CLEANUP")
    encoded=outer["native_inner_DATA_hex"]
    if inner_stdout is None:
        need(type(encoded) is str and 0<len(encoded)<=32768,"A087_RAW_INNER_BOUND");raw=bytes.fromhex(encoded)
    else:
        need(type(inner_stdout) is bytes and 0<len(inner_stdout)<=1048576,"A118_RAW_INNER_BOUND");raw=inner_stdout
    need(raw.endswith(b"\n") and raw.count(b"\n")==1 and hashlib.sha256(raw).hexdigest()==outer["inner_terminal_sha256"],"A087_EXACT_BYTES_FRAME_AND_DIGEST")
    inner=json_DATA(raw)
    need(inner["schema"]=="friday.a087.native-public-result.v1" and inner["case"]==case and inner["native_registry_only"] is True and
        inner["terminal_completion"] is positive and inner["body_complete"] is False and inner["acceptance_complete"] is False and
        inner["current_GO"] is False and inner["F10_waiver"] is False and inner["all216"]=="NOT_RUN_SEPARATE_UNRESOLVED_OBLIGATION" and
        inner["full_authoritative_registry_controls"]=="SOURCE_INCOMPLETE","A087_SOURCE_RESIDUAL_NO_BROWSER_CREDIT")
    resources=inner["resources"]
    need(resources["AS"]==[50331648,50331648] and resources["CPU"]==[180,180] and resources["NOFILE"]==[512,512] and
        resources["FSIZE"]==[2147483648,2147483648] and resources["CORE"]==[0,0] and
        0<resources["self_peak_bytes"]<=50331648 and 0<=resources["children_peak_bytes"]<=201326592 and
        type(outer["aggregate_raw_RSS_peak_bytes"]) is int and 0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and
        0<outer["raw_self_peak_KiB"]*1024<=67108864 and 0<=outer["outer_memory_current"]<=67108864 and
        0<=outer["inner_memory_current"]<=201326592,"A087_ACTUAL_COMPLETE_RESOURCE_ORACLE")
    need(outer["aggregate_raw_RSS_peak_bytes"]+resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=268435456,"A087_CALLER_INCLUDED_RSS_CAP")
    if case.startswith("start_"):
        need(inner["phase"]=="START" and inner["refusal_errno"]==expected["expected_primitive_errno"] and inner["ordinary_rows"]==[],"A087_START_CAUSE_NO_CHILD")
        trace=inner["evidence"];evidence(trace,expected["expected_native_stage"],expected["expected_primitive_errno"],1)
        need(trace["expected_pid"]==owned["pid"] and trace["expected_uid"]==trace["expected_gid"]==0,"A087_FULL_EXPECTED_ORIGIN")
        reply_oracle(trace,case,ctl,owned)
        return inner
    before,after=inner["session_before"],inner["session_after"]
    resources=inner["resources"]
    need(resources["AS"]==[50331648,50331648] and resources["CPU"]==[180,180] and resources["NOFILE"]==[512,512] and
        resources["FSIZE"]==[2147483648,2147483648] and resources["CORE"]==[0,0] and 0<resources["self_peak_bytes"]<=50331648 and
        0<=resources["children_peak_bytes"]<=201326592 and resources["affinity"]==sorted(os.sched_getaffinity(0)),"A091_OWNER_CALLER_REAL_ORIGINAL_RESOURCE_ENVELOPE")
    need(before["origin"]==owned["pid"] and before["origin_birth"]==owned["birth"] and
        before["owner"]==ctl["coordinator_pid"] and before["owner_birth"]==ctl["coordinator_birth"] and
        before["uid"]==before["gid"]==1000 and before["session_ready"]==1 and before["creation_poisoned"]==0 and before["next_sequence"]==2 and
        after["next_sequence"]==expected["native_sequence_before_drain_finish"] and
        all(before[k]==after[k] for k in ("owner","origin","uid","gid","owner_birth","origin_birth")),"A087_NATIVE_PRIVATE_FIXED_ORIGIN_SEQUENCE")
    rows=inner["ordinary_rows"]
    need(sum(row["registered"]["pid"]>0 for row in rows)==expected["actual_children_created"] and
        sum(row["ready"] is not None for row in rows)==expected["ordinary_READY_count"],"A087_REAL_CREATION_READY_COUNTS")
    if not positive or case=="abort_positive":
        primary=inner["primary"];need(primary["result"]==expected["expected_primitive_errno"],"A087_EXACT_PRIMARY_CALL_RESULT")
        evidence(primary["evidence"],expected["expected_native_stage"],0 if case=="abort_positive" else expected["expected_primitive_errno"],expected["expected_phase"])
        if case.startswith(("ack_","register_","abort_","reap_")):reply_oracle(primary["evidence"],case,ctl,owned)
    for role,row in enumerate(rows):
        initial,final=row["registered"],row["final"];trace=row["final_evidence"]
        need(initial["status_known"]==initial["wait_observed"]==0 and
            all(initial[k]==final[k] for k in ("owner","origin","pid","role","birth","owner_birth","origin_birth")),"A087_NO_PID_PARENT_GENERATION_ADOPTION")
        if initial["pid"]<=0:
            need(row["ready"] is None and final["state"]==4 and final["status_known"]==0,"A087_PRECLONE_REFUSAL");continue
        if row["ready"] is not None:
            ready_ns,pid,parent,uid,gid,birth,parent_birth=row["ready"]
            need((pid,parent,uid,gid,birth,parent_birth)==(initial["pid"],initial["owner"],1000,1000,initial["birth"],initial["owner_birth"]) and
                row["payload"].encode("ascii")==PAYLOADS[role] and row["spawn_evidence"]["ready_released"]==1 and
                0<row["spawn_evidence"]["intent_ack_ns"]<=row["spawn_evidence"]["clone_ns"]<=row["spawn_evidence"]["register_send_ns"]<=
                row["spawn_evidence"]["register_ack_ns"]<=row["spawn_evidence"]["release_ns"]<=ready_ns,"A087_ACTUAL_REGISTER_ACK_BEFORE_READY")
        else:need(trace["ready_released"]==0 and trace["register_ack_ns"]==0,"A087_NO_EARLY_BODY_OR_READY")
        need(final["wait_observed"]==final["cleanup_reaped"]==1 and trace["wait4_ns"]>0,"A087_ACTUAL_PRIVATE_WAIT_BEFORE_TERMINAL")
        lost_handle=case in ("pidfd_plaintext","pidfd_closed","close_denied")
        need(final["handle_closed"]==(0 if lost_handle else 1) and (lost_handle or final["pidfd"]==-1),"A087_PRECISE_CONFIRMED_OR_UNCONFIRMED_HANDLE_CLOSE")
        if positive:
            need(row["wait_result"]==1 and row["release_result"]==0 and final["state"]==3 and final["status_known"]==1 and
                final["status"]==(23+role)<<8 and 0<trace["wait4_ns"]<=trace["reap_ack_ns"]<=trace["close_ns"],"A087_INDEPENDENT_PRIVATE_KERNEL_EXIT_AND_ACK")
        else:need(final["state"]==4 and final["status_known"]==0 and final["status"]==0 and final["creation_poisoned"]==1 and
            trace["status_redacted"]==1 and PACKET.unpack(bytes.fromhex(trace["expected"]))[8]==PACKET.unpack(bytes.fromhex(trace["observed"]))[8]==0,
            "A087_STICKY_UNKNOWN_NEVER_PUBLIC_OR_DATA_STATUS")
    if not positive:
        again=inner["attempts"][-1];need(again["operation"]=="after_refusal_creation" and again["result"]==-117 and again["created_pid"]==0,"A087_NO_RESET_OR_RETRY_CREATION")
    if case=="register_delayed_ACK":need(rows[0]["spawn_evidence"]["register_ack_ns"]-rows[0]["spawn_evidence"]["register_send_ns"]>=100000000,"A087_REAL_ACK_DELAY_BARRIER")
    if case.startswith("reap_"):need(inner["primary"]["record"]["wait_observed"]==1 and inner["primary"]["record"]["status_known"]==0 and
        inner["primary"]["evidence"]["wait4_ns"]>0 and inner["primary"]["evidence"]["reap_ack_ns"]==0,"A087_WAIT4_STAYS_PRIVATE_AFTER_INVALID_OR_LOST_ACK")
    if case=="signal_denied":need(rows[0]["final_evidence"]["signal_attempts"]==1,"A087_ONE_REAL_SIGNAL_NO_RETRY")
    if case=="close_denied":need(rows[0]["final_evidence"]["close_attempts"]==1 and rows[0]["final_evidence"]["close_ns"]==0,"A087_FAILED_CLOSE_NOT_CONFIRMED")
    if case=="transport_closed":need(inner["transport_evidence"]["transport_close_attempts"]==1 and inner["transport_evidence"]["transport_close_ns"]>0,"A087_REAL_OWN_TRANSPORT_CLOSE")
    return inner

STREAM_CAP = 1048576

class PublicPreCloneRefused(RuntimeError):
    def __init__(self,cause,cleanup,original_error):
        super().__init__(cause)
        self.original_error=original_error
        self.receipt={"schema":"friday.a118.pre-clone-refusal.v1","cause":cause,"native_process_created":False,
            "native_streams":None,"owned_process":None,"actual_auxiliary_cleanup":cleanup,
            "original_error":original_error,"original_error_type":type(original_error).__name__,
            "primitive_errno":getattr(original_error,"errno",None),
            "state":"STOP_UNCONFIRMED" if any(not v["closed"] for v in cleanup) else "REFUSED_PRECLONE",
            "SourceReady_granted_here":False,"GO":False}

class PublicAuxiliaries:
    """Allocate the ledger before its first resource. A failed close is never
    silently marked closed or retried against a potentially reused FD."""
    def __init__(self):
        self.pairs=[None]*5
        self.entries=[{"slot":i,"kind":"metadata" if i>=10 else "barrier" if i>=8 else "output",
            "fd":None,"identity9":None,"close_attempts":0,"closed":False,
            "close_errno":None,"close_error_type":None,"original_error":None,"close_ns":None} for i in range(18)]
    def acquire(self):
        for i in range(5):
            pair=os.pipe2(os.O_CLOEXEC)
            self.pairs[i]=pair
            self.entries[2*i]["fd"]=pair[0];self.entries[2*i+1]["fd"]=pair[1]
    def open_metadata(self,path):
        # At most six existing proc/fdinfo acquisitions occur in this finite
        # driver generation. Track each actual FD immediately, before fstat.
        slot=next((i for i in range(10,18) if self.entries[i]["fd"] is None),None)
        need(slot is not None,"A153_FINITE_METADATA_LEDGER")
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        self.entries[slot]["fd"]=fd
        self.entries[slot]["identity9"]=[str(v) for v in identity(os.fstat(fd))]
        return fd,slot
    def close(self,slot):
        entry=self.entries[slot]
        if entry["fd"] is None:return True
        if entry["close_attempts"]:return entry["closed"]
        entry["close_attempts"]=1
        try:os.close(entry["fd"])
        except BaseException as exc:
            entry["close_errno"]=getattr(exc,"errno",None);entry["close_error_type"]=type(exc).__name__
            entry["original_error"]=exc
            a067_public_remember(exc)
            return False
        entry["closed"]=True;entry["close_errno"]=0;entry["close_ns"]=time.monotonic_ns()
        return True
    def cleanup(self):
        for slot in range(len(self.entries)):self.close(slot)
    def receipt(self):return [entry for entry in self.entries if entry["fd"] is not None]

def public_pipes(custody):
    custody.acquire()
    return custody.pairs[:4],custody.pairs[4]

def public_error(exc):
    return str(exc) if isinstance(exc,RuntimeError) else "%s:%s"%(type(exc).__name__,getattr(exc,"errno",None))

def _sol143_prior_a067_full_DATA_decode(raw,parse=None):
    if raw[:4]==b'DS69' or raw[:8]==A201_REF_MAGIC:return sol069_decode(raw,STREAM_CAP,need,json_DATA if parse is None else parse)
    need(type(raw) is bytes and 8<=len(raw)<=STREAM_CAP and raw[:4]==b'DS67','A067_DATA_FULL_FRAME')
    length=int.from_bytes(raw[4:8],'big')
    need(0<length<=len(raw)-8,'A067_DATA_HEADER_BOUND')
    head=(json_DATA if parse is None else parse)(raw[8:8+length]);at=8+length;strings=[]
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


def a066_private_source(bundle, role):
    # Future use is permitted ONLY after the independently selected Root stock
    # actor's exact Source review and explicit readonly instrument admission.
    # No sys.modules write, child, dispatcher or VM shared with Source1000.
    raw,_ = pinned(bundle['fds'][role], bundle['source_sha256'][role], 1048576)
    namespace = {'__name__':'friday_sol066_private_readonly_instrument', '__builtins__':__builtins__}
    exec(compile(raw, '/proc/self/fd/%d' % bundle['fds'][role], 'exec'), namespace)
    return namespace

def a066_preinner_resources(bundle, snapshots, work, expected):
    # This is BEFORE public_pipes, fork, cgroup fd120 attachment and inner entry.
    # It is the already-existing UID0 selected caller, not another performer.
    resources = None;primary = None;causes = [];cleanup = [];first = None;second = None;raw = None
    instrument = None;actor = None;originals = [];driver_resources = None
    A067_PUBLIC_CUSTODY.update(resource_original_errors=originals,resource_snapshots=snapshots)
    # Keep actual original errors and complete capture objects through both
    # receivers and complement. A copy/parse/close refusal never becomes a pass.
    try:
        admission_raw,_ = pinned(bundle['admission_fd'],bundle['admission_sha256'],65536)
        admission = json_DATA(admission_raw)
        need(admission['resource_source_instrument'] == {
            'consumer':'G1.resources','domain':'root_preinner','readonly':True,
            'source_instrument_only':True,'kernel_live_admission_credit':False},
            'RESOURCE_SEPARATE_ROOT_SELECTED_READONLY_SCOPE')
        parent,birth = proc(os.getpid())
        actor = {'pid':os.getpid(),'parent':parent,'birth':birth,'uid':os.getuid(),'gid':os.getgid()}
        need(actor == admission['expected_driver'] and actor['uid'] == actor['gid'] == 0,
             'RESOURCE_ACTUAL_ROOT_PREINNER_ACTOR')
        need(time.monotonic_ns()<work,'RESOURCE_ORIGINAL_END_BEFORE_MODULE_LOAD')
        loader = a066_private_source(bundle,101)
        g1 = a066_private_source(bundle,105)
        receiver = a066_private_source(bundle,103)
        instrument = loader['ResourceInstrument'](g1['resources'], bundle['case'], work)
        A067_PUBLIC_CUSTODY.update(resource_instrument=instrument,resource_loader=loader,
            resource_original_consumer=g1,resource_original_receiver=receiver)
        selected = expected['resource_provider']
        need((instrument.consumer,instrument.domain,instrument.fault)==
            (selected['consumer'],selected['domain'],selected['fault']), 'RESOURCE_SELECTED_PROVIDER_AND_ACTUAL_SOURCE_JOIN')
        try:resources = instrument.call()
        except BaseException as exc:
            primary = exc;a067_public_remember(exc)
        data = instrument.DATA()
        false = next((v for v in data['predicates'] if not v['ok']), None)
        cause = None if false is None else false['cause']
        if primary is not None and cause != expected['expected']['cause']:causes.append(public_error(primary))
        if not data['cleanup_confirmed']:cleanup.append('RESOURCE_CLOSE_UNCONFIRMED')
        value = {'schema':'friday.sol066.resource-controller.v1','id':expected['id'],
            'position':expected['position'],'expected':expected['expected'],'actor':actor,
            'domain':'root_preinner','end_ns':str(work),'instrument':data,'resources':resources,
            'actual_cause':cause,'state':'CONSUMER_ACCEPTED_NO_ADMISSION' if cause is None else 'REFUSED',
            'stage':'G1.resources','consumer_calls':int(data['entered']),'children':[],
            'started_routes':0,'hashes':[],'cleanup_errors':cleanup,
            'error_graph':receiver['causal_error_DATA'](([primary] if primary is not None else []) +
                [error for error in instrument.objects if error is not primary]),
            'terminal_completion':not causes and not cleanup,'native_child':None,'native_evidence':None,
            'source_instrument_only':True,'kernel_live_admission_credit':False,
            'root_live_admission_credit':False,'SourceReady':False,'GO':False}
        raw = receiver['full_DATA_encode'](value)
        need(len(raw)<=1048576,'RESOURCE_ORIGINAL_COMPLETE_WIRE_BOUND_NO_CUT')
        preowned = sol069_preowned_backing(raw)
        need(bytes(preowned["view"]) == raw and preowned["bound_before_channel_write"]
             and preowned["exit_is_handover"] is False and preowned["eof_is_handover"] is False
             and preowned["digest_is_handover"] is False and preowned["pending_is_handover"] is False,
             "PREOWNED_BODY_BEFORE_BOTH_RECEIVERS")
        held = bytes(preowned["view"])
        first = receiver['resource_instrument_packet'](held, expected['expected'], actor, work, 'root_preinner')
        second = a067_full_DATA_decode(held)
        a066_resource_oracle(second,expected,actor,work)
        need(sol069_equal(second,first), 'RESOURCE_BOTH_RECEIVERS_FULL_DATA_MUTABLE_ALIAS_JOIN')
        preowned["both_decoded"] = True
        preowned["in_process_pair_is_outside_owner"] = False
        preowned["receiver_accepted"] = False
        complement(bundle,snapshots)
        need(time.monotonic_ns()<work,'RESOURCE_SAME_ORIGINAL_END_NO_REFRESH')
        driver_resources=public_resources()
        need(0<driver_resources['self_peak_bytes']<=268435456,'RESOURCE_ACTUAL_ORIGINAL_AGGREGATE_RSS')
    except BaseException as exc:
        if primary is None:primary=exc
        a067_public_remember(exc)
        originals.append(exc)
        causes.append(public_error(exc))
    accepted = first is not None and second is not None and not causes and not cleanup
    return {'schema':'friday.a118.actual-public-receipt.v1','case':bundle['case'],
        'accepted':accepted,'passed':accepted,'terminal_completion':accepted,
        'state':'ACCEPTED_SOURCE_INSTRUMENT_NO_ADMISSION' if accepted else 'REFUSED_SCOPED_RECEIPT',
        'route_domain':'root_preinner_resource_instrument','driver':actor,'outer':None,'inner':second,
        'native_child':None,'owned':None,'zero_child_scope':True,'resource_raw':raw,
        'original_resource_objects':originals + ([] if instrument is None else instrument.objects),'original_error':primary,
        'captured_original_resource_reads':None if instrument is None else instrument.reads,
        'causes':causes,'cleanup_errors':cleanup,'driver_resources':driver_resources,
        'private_Root_owner_only_never_transferred_to_Source1000':True,
        'bindings':{'source_sha256':dict(bundle['source_sha256']),'source_manifest_sha256':bundle['source_manifest_sha256']},
        'source_instrument_only':True,'body_credit':False,'root_live_admission_credit':False,
        'source_ready':False,'current_GO':False,'F10_waiver':False,
        'whole_assignment_RAM_and_implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN'}

def resource_data_equal(a, b):
    if type(a) is not type(b):return False
    if type(a) is dict:return set(a)==set(b) and all(resource_data_equal(a[k],b[k]) for k in a)
    if type(a) is list:return len(a)==len(b) and all(resource_data_equal(x,y) for x,y in zip(a,b))
    return a==b

def a066_resource_provider(manifest, case, position, meta):
    rows=manifest['resource_instruments']
    need(type(rows) is list and len(rows)==17 and len({r['id'] for r in rows})==17,
        'RESOURCE_SEALED_COMPLETE_PROVIDER_INPUT')
    selected=[r for r in rows if r['id']==case]
    need(len(selected)==1,'RESOURCE_UNIQUE_SELECTED_PROVIDER')
    row=selected[0]
    need(type(row['position']) is int and row['position']==position and resource_data_equal(row['expected'],meta) and
        row['consumer']==meta['consumer'] and row['domain']==('root_preinner' if meta['consumer']=='G1.resources' else 'inner192') and
        row['source_instrument_only'] is True and row['kernel_live_admission_credit'] is False and
        row['Root_admission'] is False and row['runtime']=='REQUIRED_NOT_RUN' and row['waiver'] is False and
        (row['fault'] is None if meta['cause'] is None else type(row['fault']) is str),
        'RESOURCE_EXACT_ORIGINAL_PROVIDER_EXPECTATION_NO_ADMISSION')
    return row

def a066_resource_oracle(row, expected, actor, work):
    meta=expected['expected'];data=row['instrument']
    need(row['id']==expected['id'] and type(row['position']) is int and row['position']==expected['position'] and
        resource_data_equal(row['expected'],meta) and resource_data_equal(row['actor'],actor) and row['end_ns']==str(work) and
        row['domain']=='root_preinner' and actor['uid']==actor['gid']==0 and
        row['native_child'] is row['native_evidence'] is None and row['children']==[] and
        row['started_routes']==0 and row['hashes']==[] and row['stage']==meta['stage']=='G1.resources' and
        row['actual_cause']==meta['cause'] and row['state']==meta['state'] and
        row['source_instrument_only'] is True and row['kernel_live_admission_credit'] is False and
        row['root_live_admission_credit'] is False and row['SourceReady'] is row['GO'] is False and
        row['terminal_completion'] is True and not row['cleanup_errors'], 'RESOURCE_EXTERNAL_ORIGINAL_ORACLE')
    need(data['id']==expected['id'] and data['consumer']=='G1.resources' and data['domain']=='root_preinner' and data['entered'] is True and
        type(row['consumer_calls']) is int and row['consumer_calls']>=meta['minimum_consumer_calls'] and data['cleanup_confirmed'] is True and
        type(data['fault_applied']) is type(data['changed_observations']) is int,
        'RESOURCE_EXTERNAL_ACTUAL_CALL_AND_RETIREMENT')
    trace=data['predicates'];errors=[v for v in trace if not v['ok']]
    need((not errors and data['returned'] is True and data['fault'] is None and data['fault_applied']==0) if meta['cause'] is None
        else len(errors)==1 and trace[-1]==errors[0] and errors[0]['cause']==meta['cause'] and
        data['fault'] is not None and data['fault_applied']==1 and data['changed_observations']==1 and data['returned'] is False,
        'RESOURCE_EXTERNAL_EARLIER_PREDICATES_AND_FIRST_FAULT')
    for record in data['reads']:
        if 'fd' in record:
            need(record['fd'] is None or record['owner']==actor['pid'] and record['close_attempted'] is True and
                record['closed'] is True and record['close_error'] is None, 'RESOURCE_EXTERNAL_ONCE_CLOSE')
            if meta['cause'] is None:need(all(r['captured']==r['presented'] and r['read_error'] is r['presentation_error'] is None
                for r in record['reads']), 'RESOURCE_EXTERNAL_REAL_POSITIVE_READS')
        elif meta['cause'] is None:need(record['captured']==record['presented'] and record['changed'] is False,
            'RESOURCE_EXTERNAL_REAL_POSITIVE_HOST_VALUES')
    return row

def a067_public_error_DATA(errors):
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
        raise RuntimeError("ERROR_ARGUMENT_TRANSPORT_UNSUPPORTED")
    def visit(error):
        if error is None:
            return None
        if not isinstance(error, BaseException):
            error = RuntimeError(error)
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


A067_PUBLIC_CUSTODY=None

def a067_public_remember(error):
    if A067_PUBLIC_CUSTODY is not None:A067_PUBLIC_CUSTODY['original_errors'].append(error)

def run_public(bundle):
    global A067_PUBLIC_CUSTODY
    A067_PUBLIC_CUSTODY={'schema':'friday.sol067.Root-entry-original-custody.v1',
        'bundle':bundle,'owner':os.getpid(),'original_errors':[],'terminal_completion':False}
    result=None
    try:result=a067_run_public_body(bundle)
    except BaseException as exc:
        a067_public_remember(exc)
        result={'schema':'friday.a118.actual-public-receipt.v1','case':bundle.get('case') if type(bundle) is dict else None,
            'accepted':False,'ordinary_consumption':None,'source_ready':False,'current_GO':False,
            'terminal_completion':False,'body_credit':False,'F10_waiver':False,'original_error':exc,
            'causes':['ENTRY_OR_FREEZE_REFUSAL'],'cleanup_errors':[]}
        if isinstance(exc,PublicPreCloneRefused):
            result['pre_clone_refusal']=exc.receipt
            result['state']=exc.receipt['state']
    originals=A067_PUBLIC_CUSTODY['original_errors']
    # Return the SAME actual ordinary owner's partial acquisitions and data,
    # not only a label naming that owner. No result back-edge is included.
    retained=A067_PUBLIC_CUSTODY.get('ordinary_data')
    result['ordinary_data_custody']=retained
    if retained is not None:
        # Fixed original18 auxiliary slots and original4 physical planes.
        # Keep primitive close exceptions, not just a later synthetic refusal.
        close_refused=False
        for entry in retained['auxiliaries'].entries:
            origin=entry['original_error']
            if origin is not None:
                close_refused=True
                if not any(origin is previous for previous in originals):originals.append(origin)
        for state in retained['physical_source_body_states']:
            for _,_,origin in state.get('close_errors',()):
                close_refused=True
                if not any(origin is previous for previous in originals):originals.append(origin)
        if close_refused:
            result.update(accepted=False,terminal_completion=False,state='STOP_UNCONFIRMED')
    result['original_errors']=originals
    result['private_entry_custody_reference']={'owner':A067_PUBLIC_CUSTODY['owner'],
        'case':result.get('case'),'domain':'same independently selected Root owner only',
        'lifetime':'through final caller consumption; never shared writable with Source',
        'not_a_cross_process_body_or_wire_credit':True}
    try:
        result['original_error_graph']=a067_public_error_DATA(originals)
        result['complete_error_DATA']=True
        result['complete_error_transport']=False
    except BaseException as recorder:
        originals.append(recorder);result['original_error_graph']=None
        result.update(accepted=False,terminal_completion=False,complete_error_DATA=False,complete_error_transport=False)
        if result.get('state')=='ACCEPTED_SCOPED_RECEIPT':result['state']='REFUSED_SCOPED_RECEIPT'
    A067_PUBLIC_CUSTODY['result']=result
    return result

def a067_run_public_body(bundle):
    receiving=bundle.get("schema")=="friday.a091.stock-public-call.v1"
    whole216=bundle.get("schema")=="friday.a158.whole216-stock-call.v1"
    snapshots,work,hard,expected=prepare_whole216(bundle) if whole216 else prepare_receiving(bundle) if receiving else prepare(bundle)
    if whole216 and expected['expected']['consumer']=='G1.resources':
        return a066_preinner_resources(bundle,snapshots,work,expected)
    fds=bundle["fds"]
    body_states=[]
    # Four independent pipes keep every original stream at its original 1MiB
    # cap. Inner bytes are not duplicated or hex-expanded into the outer frame.
    auxiliaries=PublicAuxiliaries()
    pipe_identities=[None]*4;pipe_final_identities=[None]*4;active=[False]*4
    buffers=[None]*4;digests=[None]*4;seen=[0]*4;eof=[False]*4;overflow=[False]*4
    read_errors=[None]*4;close_errors=[None]*4;retention_errors=[None]*4
    causes=[];cleanup_errors=[];outer=inner=consumption=None
    driver=None;pid=None;first_error=None;stage="A153_PIPE_ACQUISITION";released=False
    owned={"owner":os.getpid(),"pid":None,"pidfd":None,"birth":None,"reaped":False,
        "kernel_status":None,"handle_closed":False,"signal_attempts":0,"signal_errno":0,
        "close_errno":0,"wait_ns":0,"close_ns":0,"pidfd_identity9":None,
        "handle_acquired":False,"wait_error":None,"acquisition_stage":None}
    A067_PUBLIC_CUSTODY.update(auxiliaries=auxiliaries,owned=owned,buffers=buffers,digests=digests,
        snapshots=snapshots,expected=expected,work_ns=work,hard_ns=hard)
    # Publish these existing containers before the first pipe/body allocation.
    # On refusal the real caller receives acquired prefixes and original alias
    # references; an unallocated slot remains None, never a fabricated EOF.
    A067_PUBLIC_CUSTODY['ordinary_data']={'auxiliaries':auxiliaries,'owned':owned,
        'buffers':buffers,'digests':digests,'seen':seen,'eof':eof,'overflow':overflow,
        'read_errors':read_errors,'close_errors':close_errors,'retention_errors':retention_errors,
        'pipe_identities':pipe_identities,'pipe_final_identities':pipe_final_identities,
        'snapshots':snapshots,'expected':expected,'physical_source_body_states':body_states}
    def reap():
        nonlocal first_error
        if pid is not None and not owned["reaped"] and owned["wait_error"] is None:
            try:done,status=os.waitpid(pid,os.WNOHANG)
            except BaseException as exc:
                a067_public_remember(exc)
                if first_error is None:first_error=exc
                owned["wait_error"]=public_error(exc);cleanup_errors.append("WAIT:"+owned["wait_error"]);return
            if done==pid:owned.update(reaped=True,kernel_status=status,wait_ns=time.monotonic_ns())
    def drain():
        nonlocal first_error
        # Each actual reader is nonblocking before clone. One refused reader
        # cannot prevent finite drainage/custody of the remaining real streams.
        for index in range(4):
            if not active[index]:continue
            fd=auxiliaries.entries[2*index]["fd"]
            try:part=os.read(fd,65536)
            except BlockingIOError:continue
            except BaseException as exc:
                a067_public_remember(exc)
                if first_error is None:first_error=exc
                read_errors[index]=getattr(exc,"errno",None) or type(exc).__name__
                causes.append("READ:"+public_error(exc));active[index]=False
                if not auxiliaries.close(2*index):close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
                continue
            if not part:
                eof[index]=True
                try:pipe_final_identities[index]=[str(v) for v in identity(os.fstat(fd))]
                except BaseException as exc:
                    a067_public_remember(exc)
                    cleanup_errors.append("PIPE_FINAL_IDENTITY:"+public_error(exc))
                if not auxiliaries.close(2*index):close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
                active[index]=False;continue
            try:
                seen[index]+=len(part);digests[index].update(part)
                room=STREAM_CAP-len(buffers[index]);buffers[index].extend(part[:room])
                if seen[index]>STREAM_CAP:overflow[index]=True
            except BaseException as exc:
                a067_public_remember(exc)
                if first_error is None:first_error=exc
                retention_errors[index]=public_error(exc);causes.append("RETENTION:"+retention_errors[index])
                active[index]=False
                if not auxiliaries.close(2*index):close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
    try:
        pairs,barrier=public_pipes(auxiliaries)
        stage="A153_PIPE_METADATA"
        for index,pair in enumerate(pairs):
            pipe_identities[index]=identity(os.fstat(pair[0]))
            auxiliaries.entries[2*index]["identity9"]=[str(v) for v in pipe_identities[index]]
            os.set_blocking(pair[0],False);active[index]=True
        stage="A153_DRIVER_BIRTH"
        parent,birth=proc(os.getpid(),auxiliaries)
        driver={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()}
        stage="A153_STREAM_ALLOCATION"
        for index in range(4):buffers[index]=bytearray();digests[index]=hashlib.sha256()
        stage="A201_PREOWNED_SOURCE_BODY_BEFORE_OUTER_BIRTH"
        # Exact capsule bytes, independent of Source-provided reference labels.
        capsule= os.pread(fds[100],849,0)
        need(len(capsule)==848 and hashlib.sha256(capsule).hexdigest()==bundle["capsule_sha256"],"A201_SOURCE_BODY_CAPSULE")
        session=capsule[112:144]
        capsule_sha=hashlib.sha256(capsule).digest()
        source_uid,source_gid=struct.unpack_from("<II",capsule,20)
        need((source_uid,source_gid)==(1000,1000),"A201_ORIGINAL_SOURCE_CREDENTIALS")
        for slot in range(4):
            state=a201_create_body(slot,session,capsule_sha,struct.unpack_from("<Q",capsule,48)[0],body_states,
                source_uid=source_uid,source_gid=source_gid)
        A067_PUBLIC_CUSTODY["physical_source_body_states"]=body_states
        stage="A153_OUTER_CLONE"
        pid=os.fork()
        if pid>0:
            for state in body_states:state["born"]=True
        if pid==0:
            # Setup errors terminate this actual child branch. They never
            # unwind into the driver's entry point or manufacture a receipt.
            try:
                for read,_ in pairs:os.close(read)
                os.close(barrier[1])
                while time.monotonic_ns()<work:
                    if select.select([barrier[0]],[],[],.002)[0]:
                        if os.read(barrier[0],1)!=b"A":os._exit(124)
                        break
                else:os._exit(124)
                os.close(barrier[0])
                mapping={**fds,1:pairs[0][1],2:pairs[1][1],128:pairs[2][1],129:pairs[3][1],
                    **{131+state["slot"]:state["fd"] for state in body_states}}
                copies={dst:fcntl.fcntl(src,fcntl.F_DUPFD_CLOEXEC,400) for dst,src in mapping.items()}
                for dst,src in copies.items():os.dup2(src,dst,inheritable=True)
                for src in copies.values():os.close(src)
                attach=os.open("cgroup.procs",os.O_WRONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=120)
                try:need(os.write(attach,str(os.getpid()).encode("ascii"))>0,"A087_ACTUAL_SELF_ATTACH")
                finally:os.close(attach)
                for name in os.listdir("/proc/self/fd"):
                    fd=int(name)
                    if fd not in mapping:
                        try:os.close(fd)
                        except OSError:pass
                os.execve(110,["friday-approved-native-browser3","--held-a061",bundle["capsule_sha256"]],
                    {"PATH":"/usr/bin:/bin","LANG":"C","LC_ALL":"C"})
            except BaseException:os._exit(125)
            os._exit(125)
        # The actual fork return is recorded before every parent acquisition.
        owned["pid"]=pid;stage="A153_PARENT_ENDPOINT_CLOSE"
        for index in range(4):need(auxiliaries.close(2*index+1),"A153_PARENT_WRITER_CLOSE")
        need(auxiliaries.close(8),"A153_PARENT_BARRIER_READER_CLOSE")
        stage="A153_OWNED_PIDFD"
        owned["pidfd"]=os.pidfd_open(pid,0);owned["handle_acquired"]=True
        parent,birth=proc(pid,auxiliaries);owned["birth"]=birth
        owned["pidfd_identity9"]=[str(v) for v in identity(os.fstat(owned["pidfd"]))]
        need(parent==owned["owner"] and pidfd_pid(owned["pidfd"],auxiliaries)==pid and proc(pid,auxiliaries)==(parent,birth),
            "A087_ACTUAL_OWNED_ORIGIN_BEFORE_RELEASE")
        stage="A153_OUTER_RELEASE"
        need(os.write(barrier[1],b"A")==1,"A087_PUBLIC_RELEASE");released=True
        need(auxiliaries.close(9),"A153_PUBLIC_RELEASE_CLOSE")
        stage="A153_CONTINUOUS_DRAIN"
        while any(active) or not owned["reaped"]:
            need(time.monotonic_ns()<hard-10**9,"A087_FINITE_CONTINUOUS_DRAIN")
            drain();reap()
            need(not causes and owned["wait_error"] is None,"A153_DRAIN_OR_WAIT_REFUSED")
            need(not any(overflow),"A087_OUTPUT_CAP")
            select.select([pairs[i][0] for i in range(4) if active[i]],[],[],.002)
    except BaseException as exc:
        a067_public_remember(exc)
        if first_error is None:first_error=exc
        owned["acquisition_stage"]=stage;causes.append(stage+":"+public_error(exc))
    finally:
        # Closing the actual unreleased barrier permits finite child exit even
        # when proc/pidfd acquisition failed. No guessed PID is used to signal.
        auxiliaries.close(9)
        if pid is not None and pid>0:
            reap()
            if released and not owned["reaped"]:
                try:
                    need(owned["pidfd"] is not None and pidfd_pid(owned["pidfd"],auxiliaries)==pid and
                        proc(pid,auxiliaries)==(owned["owner"],owned["birth"]) and
                        [str(v) for v in identity(os.fstat(owned["pidfd"]))]==owned["pidfd_identity9"],
                        "A087_EXACT_OWN_HANDLE_BEFORE_SIGNAL")
                    owned["signal_attempts"]+=1
                    try:signal.pidfd_send_signal(owned["pidfd"],signal.SIGTERM)
                    except OSError as exc:owned["signal_errno"]=exc.errno;raise
                except BaseException as exc:
                    a067_public_remember(exc)
                    cleanup_errors.append("SIGNAL:"+public_error(exc))
            stop=min(hard-100000000,time.monotonic_ns()+10**9)
            while (any(active) or not owned["reaped"]) and time.monotonic_ns()<stop:
                try:drain()
                except BaseException as exc:
                    a067_public_remember(exc)
                    cleanup_errors.append("DRAIN:"+public_error(exc));break
                reap()
                try:
                    select.select([pairs[i][0] for i in range(4) if active[i]],[],[],.002)
                except BaseException as exc:
                    a067_public_remember(exc)
                    cleanup_errors.append("POLL:"+public_error(exc));break
            reap()
        auxiliaries.cleanup()
        for index in range(4):
            if auxiliaries.entries[2*index]["fd"] is not None and not auxiliaries.entries[2*index]["closed"]:
                close_errors[index]=auxiliaries.entries[2*index]["close_errno"] or "UNKNOWN"
        # A live exact handle remains owned by this same driver on an uncertain
        # stop. It is never transferred to a new process or silently discarded.
        if owned["reaped"] and owned["pidfd"] is not None:
            try:os.close(owned["pidfd"])
            except BaseException as exc:
                a067_public_remember(exc)
                owned["close_errno"]=getattr(exc,"errno",None) or "UNKNOWN"
            else:owned.update(handle_closed=True,close_ns=time.monotonic_ns(),pidfd=None)
        if pid is not None and (not owned["reaped"] or
            owned["handle_acquired"] and not owned["handle_closed"] or
            any(not entry["closed"] for entry in auxiliaries.receipt())):
            cleanup_errors.append("A087_STOP_UNCONFIRMED")
    if pid is None:
        # No birth: close only these exact caller-owned empty Source planes.
        # Primary + all reached cleanup origins remain actual caller-owned.
        for state in body_states:
            try:
                closed=a201_close_unborn_plane(state)
                for _,_,origin in state.get('close_errors',()):a067_public_remember(origin)
                need(closed,"A201_UNBORN_CLOSE_UNCONFIRMED")
            except BaseException as exc:
                a067_public_remember(exc)
                cleanup_errors.append("A201_UNBORN_CLEANUP:"+public_error(exc))
        raise PublicPreCloneRefused(stage+":"+public_error(first_error),auxiliaries.receipt(),first_error) from first_error
    if owned["reaped"]:
        # Consume complete physical records EVEN after an undelivered reference.
        # This is real frame custody, not proof of original native/error bodies.
        for state in body_states:
            try:
                a201_snapshot_plane(state)
            except BaseException as exc:
                a067_public_remember(exc)
                cleanup_errors.append("A201_BODY_SNAPSHOT:"+public_error(exc))
    streams=[]
    for i,buf in enumerate(buffers):
        raw=None if buf is None else bytes(buf)
        transfer_complete=(raw is not None and eof[i] and not overflow[i] and read_errors[i] is None and
            retention_errors[i] is None and seen[i]==len(raw))
        streams.append({"cap":STREAM_CAP,"size":seen[i],"retained_size":None if raw is None else len(raw),"eof":eof[i],
            "overflow":overflow[i],"raw":raw,"sha256":None if raw is None else hashlib.sha256(raw).hexdigest(),
            "observed_sha256":None if digests[i] is None else digests[i].hexdigest(),
            "observed_sha_complete":digests[i] is not None and eof[i] and read_errors[i] is None,
            "prefix_hex":None if raw is None else raw[:64].hex(),"hash_only":False,"read_errno":read_errors[i],
            "retention_error":retention_errors[i],"close_errno":close_errors[i],
            "pipe_identity9":None if pipe_identities[i] is None else [str(v) for v in pipe_identities[i]],
            "pipe_final_identity9":pipe_final_identities[i],
            "transfer_complete":transfer_complete,"transfer_eof":eof[i],"transfer_overflow":overflow[i],
            "stream_domain":"outer_original" if i<2 else "inner_transfer",
            "native_original":None,"native_original_binding_error":None,
            "original_stream_complete":transfer_complete if i<2 else None,
            "full_original_bounded_raw":transfer_complete if i<2 else None})
    try:
        self_usage=resource.getrusage(resource.RUSAGE_SELF);children_usage=resource.getrusage(resource.RUSAGE_CHILDREN)
        driver_resources={"self_peak_bytes":self_usage.ru_maxrss*1024,"children_historical_peak_bytes":children_usage.ru_maxrss*1024,
            "affinity":sorted(os.sched_getaffinity(0)),"AS":list(resource.getrlimit(resource.RLIMIT_AS)),
            "CPU":list(resource.getrlimit(resource.RLIMIT_CPU)),"NOFILE":list(resource.getrlimit(resource.RLIMIT_NOFILE)),
            "FSIZE":list(resource.getrlimit(resource.RLIMIT_FSIZE)),"CORE":list(resource.getrlimit(resource.RLIMIT_CORE))}
    except OSError:driver_resources=None
    receipt={"schema":"friday.a118.actual-public-receipt.v1","case":bundle["case"],"accepted":False,
        "outer":None,"inner":None,"driver":driver,"owned":owned,"stdout_sha256":streams[0]["sha256"],
        "stdout_stream":streams[0],"stderr_stream":streams[1],
        "inner_stdout_stream":streams[2],"inner_stderr_stream":streams[3],
        "causes":causes,"cleanup_errors":cleanup_errors,"ordinary_consumption":None,"driver_resources":driver_resources,
        "original_error":first_error,"actual_auxiliary_cleanup":auxiliaries.receipt(),
        "whole_assignment_RAM_and_implicit_IO":"UNKNOWN_NOT_ZERO_NOT_PROVEN",
        "bindings":{"capsule_sha256":bundle["capsule_sha256"],"source_sha256":dict(bundle["source_sha256"]),
            "image_sha256":bundle["image_sha256"],"source_manifest_sha256":bundle.get("source_manifest_sha256")},
        "source_ready":False,"current_GO":False,"whole_native_cause_closed":False,"body_credit":False,"F10_waiver":False}
    decoded_inner=None
    if whole216 and streams[2]['transfer_complete'] and streams[2]['raw']:
        try:
            # Refusal/error raw is parsed BEFORE success-only native/oracle
            # conditions. Original full streams remain the immutable preimage.
            decoded_inner=a064_receive_packet(streams[2]['raw'],bundle['capsule_sha256'])
            receipt['decoded_inner_custody']=decoded_inner
        except BaseException as exc:
            a067_public_remember(exc)
            if receipt['original_error'] is None:receipt['original_error']=exc
            causes.append(public_error(exc))
    # Output custody is finalized before any semantic, frame, kernel-exit or
    # complement check. Every later refusal returns this same raw receipt.
    # Bind physical original-stream facts even on prior startup/drain refusal.
    # Transfer EOF alone cannot assert anything about the inner original EOF.
    try:
        raw=streams[0]["raw"]
        need(streams[0]["transfer_complete"] and raw.endswith(b"\n") and raw.count(b"\n")==1,
            "A087_FULL_PUBLIC_OUTPUT_FRAME")
        outer=json_DATA(raw);receipt["outer"]=outer
        bind_inner_streams(outer,streams[2:])
    except BaseException as exc:
        a067_public_remember(exc)
        if receipt["original_error"] is None:receipt["original_error"]=exc
        causes.append(public_error(exc))
    if not causes and not cleanup_errors:
        try:
            need(all(v["full_original_bounded_raw"] is True and v["close_errno"] is None for v in streams),
                "A118_FULL_FOUR_STREAM_EOF")
            need(not streams[1]["raw"],"A087_PUBLIC_STDERR")
            inner=oracle_whole216(outer,expected,owned,streams[2]["raw"],decoded_inner) if whole216 else oracle_receiving(outer,expected,owned,streams[2]["raw"]) if receiving else oracle(outer,expected,owned,streams[2]["raw"])
            receipt["inner"]=inner
            need(os.WIFEXITED(owned["kernel_status"]) and os.WEXITSTATUS(owned["kernel_status"])==expected["outer_exit"],
                "A087_ACTUAL_OWN_KERNEL_WAIT_STATUS")
            complement(bundle,snapshots)
            if receiving and bundle["case"]=="positive":
                receipt["ordinary_consumption"]=consume_connected_positive(receipt,expected,snapshots)
            if whole216:
                receipt["ordinary_consumption"]=consume_whole216(receipt,expected,snapshots)
            receipt["accepted"]=True
        except BaseException as exc:
            a067_public_remember(exc)
            if receipt["original_error"] is None:receipt["original_error"]=exc
            causes.append(public_error(exc))
    receipt["physical_original_frame_states"]=body_states
    receipt["physical_frame_custody_not_native_acceptance"]=True
    # Both actual metadata/oracle consumers have completed or the contour is red.
    # Strong complete original frames stay in receipt before descriptor retirement.
    if owned["reaped"]:
        for state in body_states:
            if state.get("snapshot_complete"):
                try:
                    closed=a201_close_snapshotted_plane(state)
                    for _,_,origin in state.get('close_errors',()):a067_public_remember(origin)
                    need(closed,"A201_BODY_CLOSE_UNCONFIRMED")
                except BaseException as exc:
                    a067_public_remember(exc)
                    cleanup_errors.append("A201_BODY_CLOSE:"+public_error(exc))
    if cleanup_errors:
        receipt["accepted"]=False
    receipt["state"]="ACCEPTED_SCOPED_RECEIPT" if receipt["accepted"] else "STOP_UNCONFIRMED" if cleanup_errors else "REFUSED_SCOPED_RECEIPT"
    return receipt

def run_positive_first(positive,controlled):
    need(positive["case"]=="positive" and controlled["case"]!="positive" and positive["capsule_sha256"]!=controlled["capsule_sha256"],"A087_FRESH_POSITIVE_FIRST_PAIR")
    # Actual OS/runtime-index/capsule bindings can differ between independently
    # selected views; all program/consumer Source pins must remain identical.
    need(all(positive["source_sha256"][role]==controlled["source_sha256"][role] for role in SOURCES if role not in (116,117,119)) and
        positive["oracle_sha256"]==controlled["oracle_sha256"],"A087_PROGRAM_AND_INDEPENDENT_ORACLE_COMPLEMENT")
    a,b=(os.fstat(v["fds"][122]) for v in (positive,controlled))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A087_FRESH_ACTUAL_PROTECTED_VIEWS")
    ordinary=run_public(positive)
    if not ordinary["accepted"]:return {"positive":ordinary,"controlled":None,"controlled_disposition":"NOT_RUN_POSITIVE_REFUSED","source_ready":False,"current_GO":False}
    negative=run_public(controlled)
    return {"positive":ordinary,"controlled":negative,"full_scoped_cause_closed":False,"source_ready":False,"current_GO":False}

RECEIVING_CASES=("positive","peer_pid","peer_uid","peer_gid","origin_parent_argument","origin_birth_argument","owner","owner_birth",
    "frame_session","version","type","role","sequence_replay","sequence_future","deadline","intent_right",
    "register_zero_rights","register_many_rights","register_plaintext","register_other_owned_pidfd","register_parent",
    "register_birth","register_stale_pidfd","register_without_intent","register_late","abort_without_intent","abort_detail",
    "abort_right","abort_positive","reap_live","reap_birth","reap_pid","reap_status","reap_right","reap_invalid_ACK",
    "reap_lost_ACK","pending_timeout","drain_pending","drain_live","finish_without_drain","transport_closed",
    "Root_signal_positive","Root_signal_denied","Root_transport_close_denied","inherited_start_owner")

def prepare_receiving(bundle):
    need(type(bundle) is dict and set(bundle)=={"schema","fds","capsule_sha256","source_sha256","image_sha256",
        "admission_fd","admission_sha256","oracle_fd","oracle_sha256","case","source_manifest_fd","source_manifest_sha256"} and
        bundle["schema"]=="friday.a091.stock-public-call.v1" and bundle["case"] in RECEIVING_CASES,"A091_TYPED_PUBLIC_INPUT")
    case=bundle["case"];fds=bundle["fds"];expected=bundle["source_sha256"]
    need(type(fds) is dict and set(fds)==set((100,111,120,121,122)+SOURCES) and set(expected)==set(SOURCES),"A091_EXACT19_ROLE_GRAPH")
    raw_admission,admission_snap=pinned(bundle["admission_fd"],bundle["admission_sha256"],65536)
    admission=json_DATA(raw_admission)
    need(set(admission)=={"schema","expected_driver","resource_class","ordinary_child_credentials","own_root_syscall_restriction",
        "Root_selected_expected_inputs","independent_final_byte_source_review","source_manifest_sha256","compiled_from_manifest_sha256"} and
        admission["schema"]=="friday.a091.external-stock-actor-admission.v1" and admission["Root_selected_expected_inputs"] is True and
        admission["independent_final_byte_source_review"] is True and admission["resource_class"]=="ordinary180_reserve10_outer1_inner4_rss256MiB" and
        admission["source_manifest_sha256"]==admission["compiled_from_manifest_sha256"]==bundle["source_manifest_sha256"],"A091_FUTURE_EXTERNAL_REVIEW_AND_TYPED_STOCK_ASSUMPTIONS")
    parent,birth=proc(os.getpid())
    need(admission["expected_driver"]=={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()} and
        os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0,"A091_ACTUAL_INDEPENDENT_SELECTED_DRIVER")
    wanted_uid=1001 if case=="peer_uid" else 1000;wanted_gid=1001 if case=="peer_gid" else 1000
    need(admission["ordinary_child_credentials"]==[wanted_uid,wanted_gid] and type(admission["own_root_syscall_restriction"]) is bool and
        (case not in ("Root_signal_denied","Root_transport_close_denied") or admission["own_root_syscall_restriction"]),"A091_EXPLICIT_ACTUAL_OWN_STOCK_ACTOR_AND_REDUCTION")
    raw_manifest,manifest_snap=pinned(bundle["source_manifest_fd"],bundle["source_manifest_sha256"],1048576)
    manifest=json_DATA(raw_manifest)
    need(manifest["schema"]=="friday.sol069.connected-whole-source-manifest.v1" and manifest["assignment"]==
        "ASTRA-E4-SOL068-BROWSER-BOUNDED-DATA-ERROR-CUSTODY-AND-ORIGINAL-RETIREMENT-CLOSURE-SOL069" and manifest["generation"]==1 and
        manifest["GO"] is False and manifest["runtime"]=="NOT_RUN" and manifest["all216"]=="REQUIRED_NOT_RUN" and
        manifest["source_execution_admission"] is False,"A118_SOURCE_MANIFEST_NOT_AUTHORITY")
    a064_manifest_source_join(manifest,expected)
    raw_oracle,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],262144);matrix=json_DATA(raw_oracle)
    need(matrix["schema"]=="friday.a091.independent-receiving-oracles.v1" and matrix["GO"] is False and matrix["runtime"]=="NOT_RUN" and
        matrix["all216"]=="SEPARATE_UNRESOLVED" and len(matrix["rows"])==len(RECEIVING_CASES) and
        {row["case"] for row in matrix["rows"]}==set(RECEIVING_CASES),"A091_COMPLETE_INDEPENDENT_PRESELECTED_ORACLE")
    rows=[row for row in matrix["rows"] if row["case"]==case];need(len(rows)==1,"A091_UNIQUE_CASE")
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        pin=bundle["capsule_sha256"] if role==100 else bundle["image_sha256"] if role==111 else expected[role]
        raw,snap=pinned(fds[role],pin,16777216 if role in (110,111) else 1048576);raws[role]=raw;snapshots[role]=snap
    cap=raws[100]
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,1,1,1000,1000,19),"A091_ORIGINAL_CAPSULE_ABI")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    need(start<=time.monotonic_ns() and time.monotonic_ns()+30*10**9<=work<hard and hard-start==180*10**9 and hard-work==10*10**9,"A091_REAL_FINITE_PREREQUISITE_HEADROOM")
    need(cap[112:144]!=bytes(32) and cap[144:176].hex()==bundle["image_sha256"] and cap[176:208].hex()==expected[117] and
        cap[208:240].hex()==expected[116] and all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),"A091_ENTIRE_SOURCE_AND_SAME_IMAGE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A091_ACTUAL_OWN_INDEPENDENT_VIEW_AND_GROUPS")
    canonical={"schema":"friday.a091.receiving-public-input.v1","case":case,"effects":"ordinary-own-process-and-plaintext-fd-only"}
    need(raws[119]==(json.dumps(canonical,separators=(",",":"))+"\n").encode("ascii"),"A091_FULL_PINNED_ORDINARY_INPUT")
    snapshots["admission"]=(admission_snap,bundle["admission_fd"]);snapshots["oracle"]=(oracle_snap,bundle["oracle_fd"])
    snapshots["manifest"]=(manifest_snap,bundle["source_manifest_fd"])
    row={**matrix["defaults"],**rows[0]};row["context"]={"session_hex":cap[112:144].hex(),"work_ns":work,"actual_uid":wanted_uid,"actual_gid":wanted_gid}
    return snapshots,work,hard,row

def whole216_producer_for(meta):
    consumer=meta["consumer"]
    if consumer.startswith("mapping_check"):return "mapping_check"
    return {"compile_bill":"compile_bill","tokens":"tokens","G1.inert_json":"inert_json",
        "disk_reservation":"disk_reservation","reservations":"reservations",
        "G1.HeaderReader.readline":"header_reader","Supervisor.bounded_fd_bytes":"bounded_fd_bytes",
        "public_admission":"public_admission","BoundedOutput.__init__":"output_count_refusal"}.get(consumer) or (
        "a171_actual" if consumer in {
        "sealed_bytes","execute_core/R4.run_wave/G1.worker","R4.drain/collect/G1.worker",
        "RetainedTree.check","R4.run_wave/G1.process_status","Run.guard","R4.close_fd",
        "R4.stop_child/collect","require_absent_target","BoundedOutput/G1.Output.check/create",
        "R4.guarded_digest","R4.guarded_write","ca_context/R4.guarded_digest",
        "ca_context/SSLContext.load_verify_locations","G1.resources",
        "G1.worker/BoundedResponse/HeaderReader","BoundedOutput/G1.Output.close",
        "execute_core/signal.signal","Run.guard/R4.stop_child","Run.guard/R4.guarded_digest",
        "terminal_bytes/Run.guard","terminal_bytes","emit_terminal","R4.run_wave/stop_child",
        "R4.run_wave/Run.guard","G1.worker/BoundedResponse/write_all","R4.collect",
        "R4.stop_child/G1.Output.close/terminal_bytes","supervise_owned/terminal_check",
        "Supervisor.seal","Supervisor.runtime_preflight","Supervisor.write_terminal",
        "Supervisor.HeldSource.bytes","Supervisor.NativeLaunchAdapter","Supervisor.supervise_owned",
        "acquisition_resources","Supervisor.cgroup_values"} else None)

SOL068_INTERFACE='friday.sol068.production-clock-source-interface.v1'
SOL068_PRODUCTION={35:'whole3_actual_G1_worker_positive',37:'whole3_http404',38:'whole3_tls_certificate',
    39:'whole3_tls_protocol',44:'whole3_truncated',57:'deadline_receipt_write',58:'deadline_receipts_fsynced',
    59:'deadline_inventory_write',60:'deadline_inventory_fsynced',61:'deadline_terminal_seal',
    62:'deadline_closed_terminal',112:'deadline_final_retained_hash',113:'deadline_inventory_serialization',
    114:'deadline_inventory_serialized',125:'body_eof_at_cap',126:'body_cap_without_eof',
    127:'body_short_write',128:'body_partial_write'}

def prepare_whole216(bundle):
    need(type(bundle) is dict and set(bundle)=={"schema","fds","capsule_sha256","source_sha256","image_sha256",
        "admission_fd","admission_sha256","oracle_fd","oracle_sha256","case","source_manifest_fd","source_manifest_sha256"} and
        bundle["schema"]=="friday.a158.whole216-stock-call.v1","A158_TYPED_STOCK_CALL")
    fds=bundle["fds"];expected=bundle["source_sha256"]
    need(type(fds) is dict and set(fds)==set((100,111,120,121,122)+SOURCES) and set(expected)==set(SOURCES),"A158_SAME19_FIXED_ROLES")
    admission_raw,admission_snap=pinned(bundle["admission_fd"],bundle["admission_sha256"],65536)
    admission=json_DATA(admission_raw)
    g1_instrument = bundle['case'] in ('actual_resources_positive','actual_resources_leaf','actual_resources_limit',
        'actual_resources_memory','actual_resources_ancestor','actual_resources_disk','actual_resources_host_memory')
    admission_keys={"schema","expected_driver","resource_class","ordinary_child_credentials","own_root_syscall_restriction",
        "Root_selected_expected_inputs","independent_final_byte_source_review","source_manifest_sha256","compiled_from_manifest_sha256"}
    production_instrument=bundle['case'] in SOL068_PRODUCTION.values()
    if g1_instrument:admission_keys.add('resource_source_instrument')
    if production_instrument:admission_keys.add('production_source_instrument')
    need(set(admission)==admission_keys and
        admission["schema"]==('friday.sol066.root-readonly-instrument-admission.v1' if g1_instrument else 'friday.a091.external-stock-actor-admission.v1') and
        admission["Root_selected_expected_inputs"] is True and admission["independent_final_byte_source_review"] is True and
        admission["resource_class"]==('ordinary1200_reserve60_outer1_inner4_rss256MiB' if production_instrument
            else 'ordinary180_reserve10_outer1_inner4_rss256MiB') and
        admission["ordinary_child_credentials"]==[1000,1000] and admission["own_root_syscall_restriction"] is False and
        admission["source_manifest_sha256"]==admission["compiled_from_manifest_sha256"]==bundle["source_manifest_sha256"],
        "A158_INDEPENDENT_REVIEW_COMPILED_SOURCE_AND_ROOT_SELECTION")
    parent,birth=proc(os.getpid())
    need(admission["expected_driver"]=={"pid":os.getpid(),"parent":parent,"birth":birth,"uid":os.getuid(),"gid":os.getgid()} and
        os.getuid()==os.geteuid()==os.getgid()==os.getegid()==0,"A158_ACTUAL_EXTERNAL_STOCK_OWNER")
    manifest_raw,manifest_snap=pinned(bundle["source_manifest_fd"],bundle["source_manifest_sha256"],1048576)
    manifest=json_DATA(manifest_raw)
    need(manifest["schema"]=="friday.sol069.connected-whole-source-manifest.v1" and manifest["assignment"]==
        "ASTRA-E4-SOL068-BROWSER-BOUNDED-DATA-ERROR-CUSTODY-AND-ORIGINAL-RETIREMENT-CLOSURE-SOL069" and
        manifest["generation"]==1 and manifest["source_execution_admission"] is False and manifest["runtime"]=="NOT_RUN" and
        manifest["all216"]=="REQUIRED_NOT_RUN" and manifest["GO"] is False,"A158_SOURCE_SEAL_IS_NOT_AUTHORITY")
    a064_manifest_source_join(manifest,expected)
    snapshots={};raws={}
    for role in (100,111)+SOURCES:
        pin=bundle["capsule_sha256"] if role==100 else bundle["image_sha256"] if role==111 else expected[role]
        raw,snap=pinned(fds[role],pin,16777216 if role in (110,111) else 1048576);raws[role]=raw;snapshots[role]=snap
    cap=raws[100]
    mode_id=3 if production_instrument else 1
    need(len(cap)==848 and cap[:8]==b"FRA061C1" and struct.unpack_from("<6I",cap,8)==(1,mode_id,1,1000,1000,19),"A158_ORIGINAL_CAPSULE_WIRE")
    start,work,hard=struct.unpack_from("<3Q",cap,32)
    wall,reserve=(1200,60) if production_instrument else (180,10)
    need(start<=time.monotonic_ns() and time.monotonic_ns()+30*10**9<=work<hard and hard-start==wall*10**9 and
        hard-work==reserve*10**9,"A158_ORIGINAL_ACTUAL_CLOCK_ENDS")
    need(cap[112:144]!=bytes(32) and cap[144:176].hex()==bundle["image_sha256"] and cap[176:208].hex()==expected[117] and
        cap[208:240].hex()==expected[116] and all(cap[240+32*i:272+32*i].hex()==expected[role] for i,role in enumerate(SOURCES)),
        "A158_WHOLE_SOURCE_IMAGE_OS_CAPSULE_BINDING")
    root,outer,inner=(os.fstat(fds[v]) for v in (122,120,121))
    need(all(stat.S_ISDIR(v.st_mode) and v.st_uid==v.st_gid==0 for v in (root,outer,inner)) and
        struct.unpack_from("<7Q",cap,56)==(root.st_dev,root.st_ino,os.stat("/proc/self/ns/mnt").st_ino,
            outer.st_dev,outer.st_ino,inner.st_dev,inner.st_ino),"A158_ACTUAL_SAME_PROTECTED_ROOT_AND_CGROUPS")
    bill=json_DATA(raws[104]);ids=list(bill["control_map"])
    need(len(ids)==216 and type(bundle["case"]) is str and bundle["case"] in ids,"A158_WHOLE_EXACT216_MEMBERSHIP")
    position=ids.index(bundle["case"]);meta=bill["control_map"][bundle["case"]]
    producer=whole216_producer_for(meta)
    need(producer is not None,"A158_REQUIRED_NOT_RUN_NO_SAFE_SOURCE_PRODUCER")
    data=json_DATA(raws[119])
    data_keys={"schema","position","id","generation","producer","arguments"}
    if production_instrument:
        data_keys.add('source_interface')
        need(SOL068_PRODUCTION.get(position)==bundle['case'] and meta['owned_children']==3 and
            admission['production_source_instrument']=={'interface':SOL068_INTERFACE,'mode_id':3,
                'position':position,'id':bundle['case'],'source_manifest_sha256':bundle['source_manifest_sha256']} and
            data.get('source_interface')==SOL068_INTERFACE,'SOL068_INDEPENDENT_SELECTED_ALLOWLIST_AND_ACTUAL_MODE')
    need(set(data)==data_keys and
        data["schema"]=="friday.a158.whole216-input.v1" and type(data["position"]) is int and data["position"]==position and
        data["id"]==bundle["case"] and type(data["generation"]) is int and data["generation"]==1 and
        data["producer"]==producer and type(data["arguments"]) is dict and
        raws[119]==(json.dumps(data,separators=(",",":"))+"\n").encode("ascii"),"A158_CANONICAL_ROOT_SELECTED_PER_ID_INPUT")
    a171_validate_input(data,meta,need)
    oracle_raw,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],131072)
    row=json_DATA(oracle_raw)
    need(set(row)=={"schema","id","position","expected","mandatory_scope","SourceReady","GO"} and
        row["schema"]=="friday.a158.independent-per-id-oracle.v1" and row["id"]==bundle["case"] and
        type(row["position"]) is int and row["position"]==position and resource_data_equal(row["expected"],meta) and
        row["mandatory_scope"]=="WHOLE216_PRESERVED" and row["SourceReady"] is False and row["GO"] is False,
        "A158_INDEPENDENT_EXACT_ORIGINAL_EXPECTATION_NO_SCOPE_CUT")
    if meta['consumer'] in ('G1.resources','acquisition_resources'):
        row['resource_provider']=a066_resource_provider(manifest,bundle['case'],position,meta)
    snapshots["admission"]=(admission_snap,bundle["admission_fd"]);snapshots["oracle"]=(oracle_snap,bundle["oracle_fd"])
    snapshots["manifest"]=(manifest_snap,bundle["source_manifest_fd"])
    return snapshots,work,hard,{**row,"case":bundle["case"],"producer":producer,"outer_exit":0,
        "context":{"session_hex":cap[112:144].hex(),"work_ns":work,"capsule_sha256":bundle['capsule_sha256'],
            'mode_id':mode_id,'start_ns':start,'hard_ns':hard,
            'source_interface':SOL068_INTERFACE if production_instrument else None,
            'fixture_sha256':expected[119]}}

A064_META_PATHS=(
    ("operation_observation","descriptor_records"),
    ("operation_observation","facts","dependency_error_graph"),
    ("operation_observation","facts","worker_origin_error_graphs"),
    ("operation_observation","facts","worker_preformat_error_snapshots"),
    ("operation_observation","facts","operation_error_graph"),
    ("operation_observation","facts","coordinator_error_graph"),
    ("operation_observation","facts","outer_error_graph"),
    ("operation_observation","facts","outer_fd_journal"),
    ("operation_observation","facts","resource_instrument"),
    ("operation_observation","facts","resource_original_error_graph"),
    ("operation_observation","facts","resource_result"),
    ("operation_auxiliary_cleanup",))

def a064_fd_decode_v1(blob,need):
    import struct
    need(type(blob) is bytes and 12<=len(blob)<=1048576,"A064_FD_TABLE_FULL_BOUND")
    magic,count,extra_size=struct.unpack_from("<4sII",blob)
    stride=struct.calcsize("<HqqqB9Q")
    need(magic==b"FD64" and 12+count*stride+extra_size==len(blob),"A064_FD_TABLE_EXACT_LENGTH")
    # Same duplicate-key/nonfinite rejection as other immutable DATA inputs.
    def unique(items):
        out={}
        for key,value in items:need(key not in out,"A064_FD_DUPLICATE_KEY");out[key]=value
        return out
    extras=json.loads(blob[12+count*stride:],object_pairs_hook=unique,
        parse_constant=lambda value:need(False,"A064_FD_NONFINITE"))
    need(type(extras) is dict and all(type(key) is str and key.isdecimal() and str(int(key))==key and
        0<=int(key)<count and type(value) is dict for key,value in extras.items()),"A064_FD_EXTRAS_DATA")
    keys=("token","fd","owner","identity9","close_attempted","closed","borrowed_close_only","close_error","hook_error")
    result=[]
    for index in range(count):
        fields=struct.unpack_from("<HqqqB9Q",blob,12+index*stride);mask=fields[0];flags=fields[4]
        need(mask<512 and flags<128,"A064_FD_RESERVED_BITS")
        record={}
        for i,key in enumerate(keys[:3]):
            if mask&(1<<i):
                need(fields[1+i]>=0,"A064_FD_NONNEGATIVE_DATA")
                record[key]=fields[1+i] if flags&(1<<(4+i)) else None
        if mask&8:record["identity9"]=[str(v) for v in fields[5:]] if flags&8 else None
        for i,key in enumerate(keys[4:7]):
            if mask&(1<<(4+i)):record[key]=bool(flags&(1<<i))
        for i,key in enumerate(keys[7:],7):
            if mask&(1<<i):record[key]=None
        extra=extras.get(str(index),{})
        need(all(key not in keys[:7] and (key not in keys or key in record) for key in extra),"A064_FD_EXTRA_NO_OVERRIDE")
        record.update(extra);result.append(record)
    return result

def a064_fd_decode(blob,need):
    if blob[:4]==b'FD64':return a064_fd_decode_v1(blob,need)
    need(type(blob) is bytes and 16<=len(blob)<=1048576,'A066_FULL_HISTORY_FRAME_BOUND')
    magic,count,unique,size=struct.unpack_from('<4sIII',blob)
    end=16+31*count+72*unique
    need(magic==b'FD65' and unique<=count and end+size==len(blob),'A066_EXACT_FULL_HISTORY_LENGTH')
    identities=[struct.unpack_from('<9Q',blob,16+31*count+72*i) for i in range(unique)]
    need(len(set(identities))==unique,'A066_IDENTITY_DICTIONARY_CANONICAL')
    extras=json_DATA(blob[end:])
    need(type(extras) is dict and all(type(k) is str and k.isdecimal() and str(int(k))==k and
        0<=int(k)<count and type(v) is dict for k,v in extras.items()),'A066_EXACT_HISTORY_EXTRAS')
    keys=('token','fd','owner','identity9','close_attempted','closed','borrowed_close_only','close_error','hook_error')
    result=[];used=set()
    for index in range(count):
        mask,token,fd,owner,flags,reference=struct.unpack_from('<HqqqBI',blob,16+31*index)
        need(mask<512 and flags<128 and 0<=reference<=unique and
            bool(flags&8)==bool(reference) and (not reference or bool(mask&8)),'A066_HISTORY_TYPED_IDENTITY_REFERENCE')
        row={}
        for i,key in enumerate(keys[:3]):
            if mask&(1<<i):
                value=(token,fd,owner)[i];need(value>=0,'A066_HISTORY_NONNEGATIVE_DATA')
                row[key]=value if flags&(1<<(4+i)) else None
        if mask&8:
            row['identity9']=[str(v) for v in identities[reference-1]] if reference else None
        if reference:used.add(reference)
        for i,key in enumerate(keys[4:7]):
            if mask&(1<<(4+i)):row[key]=bool(flags&(1<<i))
        for i,key in enumerate(keys[7:],7):
            if mask&(1<<i):row[key]=None
        extra=extras.get(str(index),{})
        need(all(key not in keys[:7] and (key not in keys or key in row) for key in extra),'A066_HISTORY_EXTRA_NO_OVERRIDE')
        row.update(extra);result.append(row)
    need(used==set(range(1,unique+1)),'A066_EVERY_ORIGINAL_IDENTITY_BOUND')
    return result

def a064_restore_metadata(producer,item,blob,need):
    path=tuple(item["path"])
    need(path in A064_META_PATHS and len(blob)==item["bytes"] and
        hashlib.sha256(blob).hexdigest()==item["sha256"],"A064_EXACT_METADATA_PATH_RAW_SHA")
    container=producer
    for key in path[:-1]:container=container[key]
    need(container[path[-1]] is None,"A064_METADATA_NOT_ALREADY_RESTORED")
    need(item["codec"] in ("fd64.v1","fd64.dict.v2","json.DATA.v1","string.DATA.sol067.v1","bounded.DATA.sol069.v1"),"A064_METADATA_CODEC")
    if item['codec'].startswith('fd64'):
        need(blob[:4]==(b'FD65' if item['codec']=='fd64.dict.v2' else b'FD64'),'A066_DECLARED_FULL_HISTORY_CODEC')
        container[path[-1]]=a064_fd_decode(blob,need)
    elif item['codec'] in ('string.DATA.sol067.v1','bounded.DATA.sol069.v1'):container[path[-1]]=a067_full_DATA_decode(blob)
    else:container[path[-1]]=json_DATA(blob)


def a064_manifest_source_join(manifest,expected):
    # Both ELF roles are fresh compiled artifacts, never author-placeholder SHA.
    # OS/runtime-index/fixture roles are independently Root-selected per view.
    fixed=set(SOURCES)-{110,113,116,117,119}
    bindings=manifest.get("fixed_source_bindings")
    need(type(bindings) is dict and set(bindings)=={str(role) for role in fixed} and
        all(type(bindings[str(role)]) is dict and bindings[str(role)].get("sha256")==expected[role]
            for role in fixed),"A064_CURRENT_MANIFEST_ACTUAL_FIXED_SOURCE_JOIN")
    need(manifest["SourceReady"] is False and manifest["Root_admission"] is False and
        manifest["whole216"]==216 and manifest["whole209"]==209 and manifest["whole29"]==29 and
        manifest["F10_waiver"] is False,"A064_NO_AUTHOR_AUTHORITY_OR_SCOPE_CUT")

def a064_restore_producer(raw):
    """Independent receiver: bounded JSON prefix plus exact full raw frames."""
    need(type(raw) is bytes and len(raw)<=1048576,"A064_PRODUCER_STREAM_BOUND")
    newline=raw.find(b"\n")
    need(0<=newline<32768,"A064_PRODUCER_HEADER_BOUND")
    producer=json_DATA(raw[:newline+1]);at=newline+1
    need(type(producer) is dict,"A064_TYPED_PERFORMING_PRODUCER")
    for record in producer.get("operation_observation",{}).get("operation_raw",[]):
        if record.get("transport")=="stdout_frame":
            need(record.get("raw_hex") is None and len(raw)-at>=4,"A064_RAW_FRAME_NOT_ALREADY_RESTORED")
            size=int.from_bytes(raw[at:at+4],"big");at+=4
            need(type(record.get("bytes")) is int and size==record["bytes"]<=1048576 and
                size<=len(raw)-at,"A064_PRODUCER_EXACT_FRAME_LENGTH")
            blob=bytes(raw[at:at+size]);at+=size
            need(hashlib.sha256(blob).hexdigest()==record.get("sha256"),"A064_PRODUCER_FULL_RAW_SHA")
            record["raw_hex"]=blob.hex()
    for item in producer.get("_metadata_frames",[]):
        need(len(raw)-at>=4,"A064_PRODUCER_METADATA_LENGTH")
        size=int.from_bytes(raw[at:at+4],"big");at+=4
        need(size==item["bytes"] and 0<=size<=1048576 and size<=len(raw)-at,"A064_PRODUCER_METADATA_LENGTH")
        blob=bytes(raw[at:at+size]);at+=size
        a064_restore_metadata(producer,item,blob,need)
    need(at==len(raw),"A064_PRODUCER_NO_UNBOUND_TRAILER")
    return producer

def a064_receive_packet(raw,expected_capsule_sha=None):
    need(type(raw) is bytes and 0<len(raw)<=1048576,"A064_ORIGINAL_COORDINATOR_PACKET_BOUND")
    newline=raw.find(b"\n");need(0<=newline<32768,"A064_PACKET_HEADER_BOUND")
    header=json_DATA(raw[:newline+1])
    need(type(header) is dict and set(header)=={"schema","receipt","frames"} and
        header["schema"]=="friday.sol064.full-raw-frames.v1" and type(header["receipt"]) is dict and
        type(header["frames"]) is list and len(header["frames"])<=64,"A064_EXACT_PACKET_SCHEMA")
    frames=[];at=newline+1
    for row in header["frames"]:
        need(type(row) is dict and set(row)=={"bytes","encoded_bytes","codec","sha256"} and
            type(row["bytes"]) is type(row["encoded_bytes"]) is int and
            0<=row["bytes"]<=1048576 and 0<=row["encoded_bytes"]<=1048576 and len(raw)-at>=4,
            "A064_TYPED_FRAME_LENGTH")
        size=int.from_bytes(raw[at:at+4],"big");at+=4
        need(size==row["encoded_bytes"] and size<=len(raw)-at,"A064_EXACT_PACKET_LENGTH")
        blob=bytes(raw[at:at+size]);at+=size
        need(row["codec"] in ("raw.DATA.v1","rle.DATA.v1"),"A064_RAW_DATA_CODEC")
        if row["codec"]=="rle.DATA.v1":
            need(size%5==0,"A064_RLE_RECORD_BOUND")
            decoded=bytearray();previous=None
            for offset in range(0,size,5):
                count=int.from_bytes(blob[offset:offset+4],"big");byte=blob[offset+4]
                need(count>0 and len(decoded)+count<=row["bytes"] and byte!=previous,"A064_RLE_CANONICAL_FULL_BOUND")
                decoded.extend(bytes((byte,))*count);previous=byte
            blob=bytes(decoded)
        need(len(blob)==row["bytes"],"A064_EXACT_DECODED_RAW_BOUND")
        need(hashlib.sha256(blob).hexdigest()==row["sha256"],"A064_EXACT_PACKET_RAW_SHA")
        frames.append(blob)
    need(at==len(raw),"A064_PACKET_NO_UNBOUND_TRAILER")
    inner=header["receipt"];used=set()
    for item in inner.get("_receipt_metadata_frames",[]):
        need(item["path"]==["original_error_graph"] and item["codec"] in ("json.DATA.v1","bounded.DATA.sol069.v1") and
            inner["original_error_graph"] is None and type(item.get("raw_frame")) is int and
            0<=item["raw_frame"]<len(frames),"A064_RECEIPT_ERROR_METADATA_FRAME")
        index=item.pop("raw_frame");blob=frames[index];used.add(index)
        need(len(blob)==item["bytes"] and hashlib.sha256(blob).hexdigest()==item["sha256"],
            "A064_RECEIPT_FULL_ERROR_GRAPH_RAW_SHA")
        inner["original_error_graph"]=a067_full_DATA_decode(blob) if item["codec"]=="bounded.DATA.sol069.v1" else json_DATA(blob)
    def restore(record,sizekey):
        need(type(record) is dict and "raw_hex" not in record and
            type(record.get("raw_frame")) is int and 0<=record["raw_frame"]<len(frames),"A064_TYPED_RAW_REFERENCE")
        index=record.pop("raw_frame");blob=frames[index];used.add(index)
        need(record.get(sizekey)==len(blob) and record.get("sha256")==hashlib.sha256(blob).hexdigest(),
            "A064_ORIGINAL_RECORD_FULL_RAW_BINDING")
        record["raw_hex"]=blob.hex()
    streams=inner.get("producer_streams")
    if streams is not None:
        need(type(streams) is list and len(streams)==2,"A064_BOTH_ORIGINAL_PRODUCER_STREAMS")
        for record in streams:restore(record,"raw_size")
    if inner.get('child_error_from_original_stderr') is True:
        need(inner.get('child_error_receipt') is None and inner['passed'] is False and
            inner['terminal_completion'] is False and type(streams) is list and len(streams)==2,
            'A067_ORIGINAL_CHILD_FAILURE_NOT_COMPLETION')
        child=a067_full_DATA_decode(bytes.fromhex(streams[1]['raw_hex']))
        need(child['schema']=='friday.sol067.actual-child-error.v1' and child['terminal_completion'] is False and
            child['SourceReady'] is child['GO'] is False,'A067_COMPLETE_CHILD_ERROR_DATA_SCOPE')
        native=inner['native_child']
        inner['child_error_receipt']=child
        inner['child_error_actor_join_verified']=type(native) is dict and expected_capsule_sha is not None and (
            child['actor']['pid']==native['pid'] and child['actor']['parent']==native['owner'] and
            child['generation']==expected_capsule_sha and child['actor']['uid']==child['actor']['gid']==1000)
        # Unverified cleanup/actor remains explicitly unverified, not a missing
        # preimage or relabelled successful producer. Full original raw stays.
    producer=inner.get("producer")
    if inner.get("producer_from_original_stdout") is True and inner.get('child_error_from_original_stderr') is not True:
        need(producer is None and type(streams) is list and len(streams)==2,"A064_ONE_ORIGINAL_PRODUCER_PROJECTION")
        producer=a064_restore_producer(bytes.fromhex(streams[0]["raw_hex"]));inner["producer"]=producer
    elif type(producer) is dict:
        for item in producer.get("_metadata_frames",[]):
            need(type(item.get("raw_frame")) is int and 0<=item["raw_frame"]<len(frames),"A064_PARENT_METADATA_FRAME")
            index=item.pop("raw_frame");used.add(index)
            a064_restore_metadata(producer,item,frames[index],need)
        for record in producer.get("operation_observation",{}).get("operation_raw",[]):
            restore(record,"bytes")
    need(used==set(range(len(frames))),"A064_EVERY_PACKET_FRAME_BOUND")
    return inner

def a064_direct_oracle(outer,expected,owned,inner):
    meta=expected["expected"];count=meta["owned_children"]
    if count == 0:
        need(meta['consumer']=='acquisition_resources' and inner['route_domain']=='inner_resource_observer' and
            inner['native_child'] is inner['native_evidence'] is None and inner['producer_streams']==[],
            'RESOURCE_INNER_ACTUAL_ZERO_TARGET_NOT_FAKE_DELEGATE')
        producer=inner['producer'];before,after=inner['session_before'],inner['session_after']
        need(producer['actual_actor']=={'pid':before['owner'],'parent':owned['pid'],
            'birth':before['owner_birth'],'uid':1000,'gid':1000} and before['next_sequence']==after['next_sequence']==2 and
            producer['id']==expected['id'] and producer['position']==expected['position'] and
            producer['producer']==expected['producer'] and producer['delegated_fork'] is False and
            producer['actual_cause']==inner['actual_cause']==meta['cause'], 'RESOURCE_ACTUAL_INNER_ACTOR_AND_FIRST_CAUSE')
        a171_validate_observation(producer['operation_observation'],meta,need,expected['id'])
        return inner
    producer=inner["producer"];before,after=inner["session_before"],inner["session_after"]
    need(inner["route_domain"]=="coordinator_targets" and producer["delegated_fork"] is False and
        before["next_sequence"]==2 and after["next_sequence"]==2+3*count and
        producer["actual_actor"]=={"pid":before["owner"],"parent":owned["pid"],
            "birth":before["owner_birth"],"uid":1000,"gid":1000},"A064_ORIGINAL_COORDINATOR_TARGET_DOMAIN")
    need(producer["id"]==expected["id"] and producer["position"]==expected["position"] and
        producer["producer"]==expected["producer"] and
        producer["actual_cause"]==inner["actual_cause"]==meta["cause"] and
        producer["operation_outcome"]==inner["operation_outcome"]==("RETURNED" if meta["cause"] is None else "REFUSED"),
        "A064_ACTUAL_DIRECT_CONSUMER_CAUSE")
    a171_validate_observation(producer["operation_observation"],meta,need,expected["id"])
    targets=inner["native_targets"]
    need(type(targets) is list and len(targets)==count,"A064_ACTUAL_REQUIRED_TARGET_COUNT")
    events=outer["root_receiving"]["events"];pids=set()
    def packet(event):
        raw=bytes.fromhex(event["packet_hex"])
        need(len(raw)==96 and event["stage"]==0 and event["primitive_errno"]==0 and
            event["ack_ns"]>0,"A064_ACTUAL_ACCEPTED_ROOT_PACKET")
        value=PACKET.unpack(raw)
        need(value[0]==b"FRA061P1" and value[1].hex()==expected["context"]["session_hex"] and
            value[2]==1 and value[12]==expected["context"]["work_ns"],"A064_SAME_FULL96_SESSION_DEADLINE")
        return value
    for target in targets:
        native,trace=target["observation"],target["evidence"]
        need(native["pid"] not in pids and native["pid"]>0 and native["birth"]>0 and
            native["owner"]==before["owner"] and native["owner_birth"]==before["owner_birth"] and
            native["origin"]==owned["pid"] and native["origin_birth"]==owned["birth"] and
            native["state"]==3 and native["status_known"]==native["wait_observed"]==
            native["cleanup_reaped"]==native["handle_closed"]==1 and native["pidfd"]==-1 and
            (os.WIFEXITED(native["status"]) or os.WIFSIGNALED(native["status"])) and
            trace["intent_ack_ns"]<=trace["clone_ns"]<=trace["register_send_ns"]<=
            trace["register_ack_ns"]<=trace["release_ns"]<trace["wait4_ns"]<=
            trace["reap_ack_ns"]<=trace["close_ns"],"A064_SAME_ACTUAL_TARGET_PRIVATE_RETIREMENT")
        pids.add(native["pid"])
        registered=[event for event in events if event["kind"]==1 and event["phase"]==5 and
            event["handle_pid"]==native["pid"] and event["proc_birth"]==native["birth"]]
        reaped=[event for event in events if event["kind"]==1 and event["phase"]==7 and
            PACKET.unpack(bytes.fromhex(event["packet_hex"]))[6]==native["pid"] and
            PACKET.unpack(bytes.fromhex(event["packet_hex"]))[10]==native["birth"]]
        need(len(registered)==len(reaped)==1 and registered[0]["proc_parent"]==before["owner"] and
            registered[0]["peer"]==reaped[0]["peer"]==[before["owner"],1000,1000] and
            registered[0]["ack_ns"]<=trace["release_ns"] and reaped[0]["ack_ns"]<=trace["reap_ack_ns"],
            "A064_INDEPENDENT_ROOT_REGISTER_REAP_JOIN")
        reg,reap=packet(registered[0]),packet(reaped[0])
        need(reg[3]==5 and reap[3]==7 and reg[5]==reap[5]==native["role"] and
            reg[6]==reap[6]==native["pid"] and reg[7]==reap[7]==before["owner"] and
            reg[10]==reap[10]==native["birth"] and reg[11]==reap[11]==before["owner_birth"] and
            reap[8]==native["status"] and registered[0]["rights"]==1 and registered[0]["rights_closed"]==0 and
            registered[0]["rights_close_errno"]==0 and reaped[0]["rights"]==reaped[0]["rights_closed"]==0,
            "A064_FULL_TARGET_PACKET_RIGHTS_STATUS_JOIN")
        # The received pidfd is transferred into Root's registry at REGISTER,
        # not closed there. Its single actual close belongs to the final event.
        closed=[event for event in events if event["kind"]==6 and event["phase"]==native["role"]+1]
        need(len(closed)==1 and closed[0]["primitive_errno"]==0,"A064_ROOT_TRANSFERRED_RIGHT_FINAL_CLOSE")
    selected=[event for event in events if event["kind"]==8 and event["phase"]==1]
    need(len(selected)==1 and selected[0]["expected_peer"][0]==before["owner"] and
        selected[0]["expected_parent"]==owned["pid"] and selected[0]["expected_birth"]==before["owner_birth"] and
        selected[0]["ack_ns"]>0,"A064_REAL_INITIAL_ROOT_COORDINATOR_SELECTION")
    start=packet(selected[0])
    need(start[3]==1 and start[4]==1 and start[5]==0 and start[6]==before["owner"] and
        start[7]==owned["pid"] and start[8]==start[9]==0 and start[10]==before["owner_birth"] and
        start[11]==owned["birth"],"A064_ROOT_INITIAL_PACKET_NOT_DELEGATE_GRANT")
    need({row["pid"] for row in producer["operation_observation"]["target_children"]}==pids,
        "A064_ACTUAL_OPERATION_NATIVE_TARGET_JOIN")
    resources=producer["resources"]
    need(resources["aggregate_envelope"]==268435456 and resources["coordinator_as"]==50331648,
        "A064_ORIGINAL_RESOURCE_BOUNDS_UNCHANGED")
    return inner

def oracle_whole216(outer,expected,owned,inner_stdout,decoded_inner=None):
    meta=expected["expected"]
    domain=("coordinator_targets" if meta["owned_children"] else
        "inner_resource_observer" if meta["consumer"] in ("G1.resources","acquisition_resources") else "delegated_consumer")
    count=meta["owned_children"] if domain=="coordinator_targets" else 0 if domain=="inner_resource_observer" else 1
    need(outer["state"]=="OUTER_BOUNDED_DRAINED_FINISHED" and outer["reason"] is None and
        outer["terminal_completion"] is True and outer["uncertainty_sticky"] is False and
        outer["registered_workers"]==outer["reaped_workers"]==count and outer["registry_next_sequence"]==4+3*count and
        outer["coordinator_kernel_status_known"] is True and outer["borrowed_status_kernel_credit"] is False and
        outer["body_complete"] is False and outer["acceptance_complete"] is False,"A158_ACTUAL_ROOT_CAUSAL_COMPLETION")
    need(type(inner_stdout) is bytes and 0<len(inner_stdout)<=1048576 and
        hashlib.sha256(inner_stdout).hexdigest()==outer["inner_terminal_sha256"],
        "A158_EXACT_ORIGINAL_COORDINATOR_RAW")
    inner=a064_receive_packet(inner_stdout,expected['context']['capsule_sha256']) if decoded_inner is None else decoded_inner
    if expected['context'].get('source_interface') is not None:
        context=expected['context']
        need(context['mode_id']==3 and context['hard_ns']-context['start_ns']==1200*10**9 and
            context['hard_ns']-context['work_ns']==60*10**9 and
            inner.get('source_interface')=={'interface':SOL068_INTERFACE,'position':expected['position'],
                'id':expected['id'],'mode_id':3,'fixture_sha256':context['fixture_sha256'],
                'source_only_credit':True} and inner.get('original_clock_ends_ns')==
                [str(context[k]) for k in ('start_ns','work_ns','hard_ns')],
            'SOL068_BOTH_RECEIVERS_SAME_ACTUAL_PRODUCTION_CLOCK_SELECTION')
    need(inner["schema"]=="friday.a158.whole216-controller-result.v1" and
        inner["id"]==expected["id"] and inner["position"]==expected["position"] and inner["generation"]==1 and
        inner["route_domain"]==domain and inner["producer_called"] is True and
        inner["child_created"] is (domain!="inner_resource_observer") and inner["passed"] is True and
        inner["terminal_run_reason"] is None and inner["terminal_sticky_uncertainty"] is False and
        inner["primary_error"] is None and not inner["cleanup_errors"] and
        inner["terminal_completion"] is True and inner["body_complete"] is False and inner["acceptance_complete"] is False and
        inner["whole216_credit"] is False and inner["F10_waiver"] is False and inner["SourceReady"] is False and inner["GO"] is False,
        "A158_ACTUAL_SCOPED_PER_ID_RESULT_NO_WHOLE_GRANT")
    control=outer["public_control"];before,after=inner["session_before"],inner["session_after"]
    need(control["origin_pid"]==owned["pid"] and control["origin_birth"]==owned["birth"] and
        before["owner"]==after["owner"]==control["coordinator_pid"] and
        before["owner_birth"]==after["owner_birth"]==control["coordinator_birth"] and
        before["origin"]==after["origin"]==owned["pid"] and before["origin_birth"]==after["origin_birth"]==owned["birth"] and
        before["next_sequence"]==2 and after["next_sequence"]==2+3*count and before["session_ready"]==after["session_ready"]==1 and
        before["creation_poisoned"]==after["creation_poisoned"]==0,"A158_ORIGINAL_COORDINATOR_BINDING_NEVER_RESET")
    need(0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and
        0<outer["raw_self_peak_KiB"]*1024<=67108864 and 0<=outer["outer_memory_current"]<=67108864 and
        0<=outer["inner_memory_current"]<=201326592 and outer["aggregate_raw_RSS_peak_bytes"]+
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=268435456,"A064_ACTUAL_AGGREGATE_ENVELOPE")
    if domain!="delegated_consumer":return a064_direct_oracle(outer,expected,owned,inner)
    native,trace=inner["native_child"],inner["native_evidence"]
    need(native["owner"]==before["owner"] and native["origin"]==owned["pid"] and native["role"]==0 and native["pid"]>0 and
        native["pid"]!=native["owner"] and native["birth"]>0 and native["state"]==3 and native["status_known"]==1 and
        native["status"]==0 and native["wait_observed"]==native["cleanup_reaped"]==native["handle_closed"]==1 and
        native["pidfd"]==-1 and trace["stage"]==0 and trace["rights"]==trace["rights_closed"]==0 and
        trace["intent_ack_ns"]<=trace["clone_ns"]<=trace["register_send_ns"]<=trace["register_ack_ns"]<=trace["release_ns"] and
        trace["wait4_ns"]<=trace["reap_ack_ns"]<=trace["close_ns"] and trace["wait4_ns"]>trace["release_ns"],
        "A158_REAL_PRIVATE_CLONE_READY_WAIT4_ACK_CLOSE")
    streams=inner["producer_streams"];need(len(streams)==2,"A158_BOTH_PRODUCER_ORIGINAL_STREAMS")
    originals=[]
    for index,stream in enumerate(streams):
        raw=bytes.fromhex(stream["raw_hex"]);originals.append(raw)
        need(stream["stream"]==index and stream["cap"]==1048576 and stream["original_complete"] is True and
            stream["eof"] is True and stream["overflow"] is False and stream["read_error"] is None and
            stream["retention_error"] is None and stream["close_error"] is None and stream["hash_only"] is False and
            stream["raw_size"]==stream["total_seen"]==len(raw)<=1048576 and hashlib.sha256(raw).hexdigest()==stream["sha256"],
            "A158_COMPLETE_IMMUTABLE_ACTUAL_PRODUCER_RAW")
    need(not originals[1],"A158_ORIGINAL_STDERR")
    producer=a064_restore_producer(originals[0]);need(producer==inner["producer"] and producer["id"]==expected["id"] and
        producer["position"]==expected["position"] and producer["producer"]==expected["producer"] and
        producer["actual_cause"]==inner["actual_cause"]==expected["expected"]["cause"] and
        producer["operation_outcome"]==inner["operation_outcome"]==("RETURNED" if producer["actual_cause"] is None else "REFUSED"),
        "A158_PERFORMED_ACTUAL_CONSUMER_OUTPUT_AND_EXPECTED_CAUSE")
    a171_validate_observation(producer["operation_observation"],expected["expected"],need,expected["id"])
    actor=producer["actual_actor"];need(actor=={"pid":native["pid"],"parent":native["owner"],"birth":native["birth"],"uid":1000,"gid":1000},
        "A158_SAME_ACTUAL_SELECTED_PERFORMING_ACTOR")
    delegated_before,delegated_after=producer["delegation_before"],producer["delegation_after"]
    for delegated in (delegated_before,delegated_after):
        need(delegated["owner"]==delegated["pid"]==native["pid"] and delegated["owner_birth"]==delegated["birth"]==native["birth"] and
            delegated["origin"]==owned["pid"] and delegated["origin_birth"]==owned["birth"] and
            delegated["uid"]==delegated["gid"]==1000 and delegated["pidfd"]==-1 and delegated["role"]==0 and
            delegated["state"]==2 and delegated["creation_poisoned"]==0 and
            all(delegated[field]==0 for field in ("wait_observed","status_known","status","cleanup_reaped","handle_closed","stop_attempted")),
            "A158_DISTINCT_CHILD_CONTEXT_NEVER_KERNEL_WAIT_AUTHORITY")
    need(delegated_before["session_ready"]==1 and delegated_before["next_sequence"]==2 and
        delegated_after["session_ready"]==0 and delegated_after["next_sequence"]==3,
        "A158_SINGLE_IMMUTABLE_CHILD_START_FINISH_LIFETIME")
    need(type(producer["operation_auxiliary_cleanup"]) is list and
        all(record["owner"]==native["pid"] and record["fd"]>=0 and record["close_attempted"] is True and
            record["closed"] is True and record["close_error"] is None and len(record["identity9"])==9 and
            all(type(value) is str and value.isdecimal() for value in record["identity9"])
            for record in producer["operation_auxiliary_cleanup"]),"A158_SAME_ACTUAL_OPERATION_FD_CUSTODY")
    for kind,phase in (("Root_selected_start",1),("Root_selected_finish",10)):
        evidence=producer[kind];need(evidence["stage"]==0 and evidence["phase"]==phase and evidence["primitive_errno"]==0 and
            evidence["observed_pid"]==evidence["expected_pid"]==owned["pid"] and
            evidence["observed_uid"]==evidence["observed_gid"]==evidence["expected_uid"]==evidence["expected_gid"]==0 and
            evidence["rights"]==evidence["rights_closed"]==0 and evidence["rights_close_errno"]==0 and
            evidence["expected"]==evidence["observed"],"A158_GENUINE_ROOT_SELECTED_CONTROLLER_EVIDENCE")
        packet=PACKET.unpack(bytes.fromhex(evidence["observed"]))
        need(packet[0]==b"FRA061P1" and packet[1].hex()==expected["context"]["session_hex"] and packet[2]==1 and
            packet[3]==phase and packet[4]==(1 if phase==1 else 2) and packet[5]==0 and packet[6]==native["pid"] and
            packet[7]==(native["owner"] if phase==1 else native["pid"]) and packet[8]==0 and
            packet[9]==expected["position"]+1 and packet[10]==native["birth"] and
            packet[11]==(before["owner_birth"] if phase==1 else native["birth"]) and packet[12]==expected["context"]["work_ns"],
            "A158_FULL96_CONTROLLER_PACKET_COMPLEMENT")
    events=outer["root_receiving"]["events"]
    selected=[e for e in events if e["kind"]==8 and e["phase"]==1]
    finished=[e for e in events if e["kind"]==1 and e["phase"]==9 and e["peer"][0]==native["pid"]]
    need(len(selected)==len(finished)==1 and selected[0]["expected_peer"][0]==native["pid"] and
        selected[0]["expected_parent"]==native["owner"] and selected[0]["expected_birth"]==native["birth"] and
        finished[0]["peer"]==[native["pid"],1000,1000] and finished[0]["proc_parent"]==native["owner"] and
        finished[0]["proc_birth"]==native["birth"] and selected[0]["ack_ns"]<=finished[0]["ack_ns"]<trace["wait4_ns"],
        "A158_ROOT_REGISTERED_SELECTION_FINISH_BEFORE_SAME_PARENT_PRIVATE_WAIT")
    need(0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and 0<outer["raw_self_peak_KiB"]*1024<=67108864 and
        0<=outer["outer_memory_current"]<=67108864 and 0<=outer["inner_memory_current"]<=201326592 and
        outer["aggregate_raw_RSS_peak_bytes"]+resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=268435456 and
        producer["resources"]["aggregate_envelope"]==268435456 and producer["resources"]["coordinator_as"]==50331648 and
        producer["whole_assignment_RAM_and_implicit_IO"]=="UNKNOWN_NOT_ZERO_NOT_PROVEN","A158_ORIGINAL_ACTUAL_RESOURCE_ENVELOPE_NO_FAKE_WHOLE_RAM")
    return inner

def consume_whole216(receipt,expected,snapshots):
    inner=receipt["inner"]
    originals=tuple(bytes.fromhex(v["raw_hex"]) for v in inner["producer_streams"]) if inner["producer_streams"] is not None else ()
    return {"schema":"friday.a158.whole216-causal-consumption.v1","id":expected["id"],"position":expected["position"],
        "driver":receipt["driver"],"outer_origin":dict(receipt["owned"]),"coordinator":inner["session_before"],
        "performing_actor":inner["producer"]["actual_actor"],"producer":inner["producer"],
        "producer_original_stdout":originals[0] if originals else None,"producer_original_stderr":originals[1] if originals else None,
        "performing_domain":inner["route_domain"],"native_targets":inner.get("native_targets"),
        "streams":tuple(receipt[key] for key in ("stdout_stream","stderr_stream","inner_stdout_stream","inner_stderr_stream")),
        "raw_retained":True,"native_child":inner["native_child"],"native_evidence":inner["native_evidence"],
        "bindings":receipt["bindings"],"scoped_consumer_cause":expected["expected"]["cause"],
        "all216_credit":False,"body_credit":False,"SourceReady_granted_here":False,"GO":False}

def run_whole216_positive_first(positive,controlled):
    positive_id="actual_pinned_registry_browsers_cft_positive"
    need(positive.get("schema")==controlled.get("schema")=="friday.a158.whole216-stock-call.v1" and
        positive["case"]==positive_id and controlled["case"]!=positive_id and
        positive["capsule_sha256"]!=controlled["capsule_sha256"] and
        positive["source_manifest_sha256"]==controlled["source_manifest_sha256"] and
        all(positive["source_sha256"][role]==controlled["source_sha256"][role] for role in SOURCES if role not in (116,117,119)),
        "A158_FRESH_INDEPENDENT_POSITIVE_FIRST_SAME_PROGRAM")
    a,b=(os.fstat(v["fds"][122]) for v in (positive,controlled))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A158_DISTINCT_EXTERNAL_PROTECTED_VIEWS")
    observed=run_public(positive)
    if not observed["accepted"]:return {"positive":observed,"controlled":None,"controlled_disposition":"NOT_RUN_POSITIVE_REFUSED",
        "SourceReady":False,"GO":False}
    return {"positive":observed,"controlled":run_public(controlled),"whole216_credit":False,"SourceReady":False,"GO":False}

def receiving_packet(row,event,control,owned):
    """Full complement from fixed Source spec, actual trusted own allocations
    and independently pinned capsule. Received bytes never choose their pin.
    The sole omitted private value is the explicitly redacted unacked status.
    """
    packet=PACKET.unpack(bytes.fromhex(event["packet_hex"]))
    need(len(bytes.fromhex(event["packet_hex"]))==96,"A091_FULL96_PACKET")
    case=row["case"];context=row["context"];root_pid=owned["pid"];coord=control["coordinator_pid"];coord_birth=control["coordinator_birth"]
    need(packet[0]==b"FRA061P1" and packet[1].hex()==context["session_hex"] or
        case=="frame_session" and packet[0]==b"FRA061P1" and packet[1]==bytes((bytes.fromhex(context["session_hex"])[0]^1,))+bytes.fromhex(context["session_hex"])[1:],"A091_EXACT_FRAME_COMPLEMENT")
    need(packet[2]==(2 if case=="version" else 1) and packet[3]==row["target_phase"] and
        packet[4]==row["request_sequence"] and packet[5]==row["request_role"] and
        packet[7]==coord+(1 if case=="owner" else 0) and packet[11]==coord_birth+(1 if case=="owner_birth" else 0) and
        packet[12]==context["work_ns"]-(1 if case=="deadline" else 0),"A091_PRESELECTED_PHASE_ROLE_SEQUENCE_OWNER_DEADLINE_COMPLEMENT")
    if packet[3] in (1,2,9,12):need(packet[6]==packet[8]==packet[9]==packet[10]==0,"A091_FULL_ZERO_CONTROL_BODY")
    if packet[3]==6:need(packet[6]==packet[8]==packet[10]==0 and packet[9]==(0 if case=="abort_detail" else 24),"A091_REAL_ABORT_BODY_COMPLEMENT")
    if packet[3] in (4,7):
        if case=="register_without_intent":
            need(packet[3]==4 and row["actor_rows"]==[],"A104_REGISTER_NO_OUT_OF_INTENTION_CLONE")
            wanted_pid,wanted_birth=coord,coord_birth
        else:
            actor=row["actor_rows"][0]
            wanted_pid=coord if case in ("register_parent","reap_pid") else actor["pid"]
            wanted_birth=coord_birth if case=="register_parent" else actor["birth"]+(1 if case in ("register_birth","reap_birth") else 0)
        need(packet[6]==wanted_pid and packet[10]==wanted_birth and packet[9]==0 and
            packet[8]==0 and (packet[3]!=7 or event["status_redacted"] is True),"A091_ENTIRE_OWNED_PACKET_BODY_AND_PRIVATE_STATUS_COMPLEMENT")
    need(event["expected_peer"]==[coord,1000,1000],"A091_EXPECTED_FULL_ORIGIN")
    if case=="peer_pid":need(event["peer"][0]==row["actual_peer_pid"] and event["peer"][0]!=coord and event["peer"][1:]==[1000,1000],"A091_ACTUAL_OWN_OTHER_PEER")
    else:need(event["peer"]==[coord,context["actual_uid"],context["actual_gid"]],"A091_AUTOMATIC_ACTUAL_CREDENTIALS")
    need(event["credentials"]==1 and event["receive_bytes"]==96 and event["receive_flags"]==0 and
        event["rights"]==row["target_rights"] and event["rights_closed"]==row["target_rights_closed"] and event["rights_close_errno"]==0,"A091_EXACT_SCM_RIGHTS_RECEIPT_AND_CLOSE")
    if case=="origin_parent_argument":need(event["proc_parent"]==root_pid and event["expected_parent"]==root_pid+1 and event["proc_birth"]==event["expected_birth"]==coord_birth,"A091_GENUINE_PROC_WRONG_EXPECTED_PARENT_ARGUMENT")
    elif case=="origin_birth_argument":need(event["proc_parent"]==event["expected_parent"]==root_pid and event["proc_birth"]==coord_birth and event["expected_birth"]==coord_birth+1,"A091_GENUINE_PROC_WRONG_EXPECTED_BIRTH_ARGUMENT")
    elif case=="register_parent":need(event["handle_pid"]==packet[6]==coord and event["proc_parent"]==root_pid and event["expected_parent"]==coord and event["proc_birth"]==packet[10]==coord_birth,"A091_ACTUAL_OWN_PIDFD_WRONG_PARENT")
    elif case=="register_birth":need(event["handle_pid"]==packet[6] and event["proc_parent"]==event["expected_parent"]==coord and event["proc_birth"]+1==event["expected_birth"]==packet[10],"A091_ACTUAL_PROC_BIRTH_VS_WRONG_ARGUMENT")
    elif case=="register_stale_pidfd":need(event["handle_pid"]==-1 and packet[6]>0 and packet[10]>0,"A091_REAL_OPEN_STALE_PIDFD")
    elif case=="register_plaintext":need(event["handle_pid"]==-74,"A091_ACTUAL_PLAINTEXT_TYPE_NOT_PIDFD")
    elif case=="register_other_owned_pidfd":need(event["handle_pid"]==coord and packet[6]!=coord,"A091_OTHER_OWN_HANDLE_NOT_ADOPTED")
    elif case=="register_without_intent":
        need(event["pending_role"]==-1 and event["pending_end"]==0 and event["handle_pid"]==-1 and
            event["proc_parent"]==event["expected_parent"]==root_pid and
            event["proc_birth"]==event["expected_birth"]==coord_birth and
            event["ack_ns"]==0,"A104_REAL_PENDING_REFUSAL_BEFORE_HANDLE_OR_CHILD_ADOPTION")
    if case=="reap_status":need(event["status_argument_invalid"] is True and event["handle_pid"]==-1,"A091_REAL_INVALID_STATUS_ARGUMENT_REDACTED_NOT_ADOPTED")
    elif case=="reap_live":need(event["status_argument_invalid"] is False and event["handle_pid"]==packet[6],"A091_REAL_LIVE_EXACT_OWN_PIDFD")

def inherited_owner_oracle(inner,outer,control):
    need(inner["schema"]=="friday.a091.inherited-owner-result.v1" and inner["case"]=="inherited_start_owner" and
        inner["terminal_completion"] is True and inner["body_complete"] is False and inner["acceptance_complete"] is False and
        inner["GO"] is False and inner["all216"]=="SEPARATE_UNRESOLVED_NOT_RUN" and len(inner["rows"])==3,"A091_OWNER_PAIR_ONLY")
    before,after=inner["session_before"],inner["session_after"]
    need(before["owner"]==after["owner"]==control["coordinator_pid"] and before["owner_birth"]==after["owner_birth"]==control["coordinator_birth"] and
        before["session_ready"]==after["session_ready"]==1 and before["creation_poisoned"]==after["creation_poisoned"]==0 and
        before["next_sequence"]==2 and after["next_sequence"]==11,"A091_PARENT_FULL_ORDINARY_POSITIVE")
    for role,row in enumerate(inner["rows"]):
        pair,native,final,trace=row["child"],row["native"],row["final"],row["evidence"]
        need(row["role"]==pair["role"]==role and pair["pid"]==native["pid"]==final["pid"] and pair["parent"]==native["owner"]==before["owner"] and
            pair["birth"]==native["birth"]==final["birth"] and pair["copy_unchanged"] is True and pair["new_pid"]==0 and
            pair["start_result"]==pair["observe_result"]==pair["binding_result"]==pair["creation_result"]==-1 and pair["status_authority"] is False,"A091_ACTUAL_CHILD_ALL_OWNER_BINDING_REFUSALS")
        a,b=pair["inherited_before"],pair["inherited_after"]
        fixed=("owner","owner_birth","origin","origin_birth","uid","gid","next_sequence")
        need(all(a[key]==b[key] for key in fixed) and a["owner"]==before["owner"] and a["owner_birth"]==before["owner_birth"] and
            a["origin"]==before["origin"] and a["origin_birth"]==before["origin_birth"] and a["uid"]==a["gid"]==1000 and
            a["next_sequence"]==3+3*role and a["session_ready"]==b["session_ready"]==0 and a["creation_poisoned"]==0 and b["creation_poisoned"]==1,"A091_FULL_INHERITED_OWNER_BIRTH_READY_CAUSAL_PAIR")
        need(native["status_known"]==native["cleanup_reaped"]==1 and native["status"]==(23+role)<<8 and final["handle_closed"]==1 and
            final["pidfd"]==-1 and row["release_result"]==0 and trace["ready_released"]==1 and
            0<trace["intent_ack_ns"]<=trace["clone_ns"]<=trace["register_send_ns"]<=trace["register_ack_ns"]<=trace["release_ns"]<=trace["wait4_ns"]<=trace["reap_ack_ns"],"A091_ACTUAL_NATIVE_ORDINARY_WAIT_ACK_AND_CLOSE")

def oracle_receiving(outer,row,owned,inner_stdout=None):
    case=row["case"];positive=row["completion"]
    need(outer["state"]==row["outer_state"] and outer["reason"]==row["outer_cause"] and outer["terminal_completion"] is positive and
        outer["uncertainty_sticky"] is row["uncertainty"] and outer["registered_workers"]==row["root_registered"] and
        outer["reaped_workers"]==row["root_reaped"] and outer["registry_next_sequence"]==row["root_sequence"] and
        outer["coordinator_kernel_status_known"] is True and outer["borrowed_status_kernel_credit"] is False and
        outer["body_complete"] is False and outer["acceptance_complete"] is False,"A091_EXACT_FULL_ROOT_TERMINAL")
    control=outer["public_control"];root=outer["root_receiving"]
    need(control["case"]==root["case"]==case and control["origin_pid"]==owned["pid"] and control["origin_parent"]==owned["owner"] and
        control["origin_birth"]==owned["birth"] and control["coordinator_pid"]>0 and control["coordinator_birth"]>0 and
        root["schema"]=="friday.a091.actual-root-receipt.v1" and root["event_count"]==len(root["events"])<=64,"A091_ACTUAL_FIXED_ROOT_AND_COORDINATOR_IDENTITIES")
    events=root["events"];receives=[event for event in events if event["kind"]==1]
    need(all(event["kind"] in (1,2,3,4,5,6,7) and event["at_ns"]>0 for event in events),"A091_COMPLETE_TYPED_PRIMITIVE_EVENTS")
    need(root["transport_close_attempts"]==1 and root["transport_close_errno"]==(1 if case=="Root_transport_close_denied" else 0) and
        root["transport_retained"] is (case=="Root_transport_close_denied") and
        (root["transport_close_ns"]==0 if case=="Root_transport_close_denied" else root["transport_close_ns"]>0),"A091_ACTUAL_ROOT_TRANSPORT_CLOSE_RESULT")
    owners=root["owned"];need(len(owners)==1+row["root_registered"] and owners[0]["pid"]==control["coordinator_pid"] and
        owners[0]["birth"]==control["coordinator_birth"] and owners[0]["kernel_status_known"] is True and owners[0]["wait4_ns"]>0,"A091_EXACT_ROOT_DIRECT_WAIT_CUSTODY")
    for item in owners:
        need(item["close_attempts"]==1 and item["close_errno"]==0 and item["close_ns"]>0 and item["handle_retained"] is False and
            item["signal_attempts"]<=1 and (item["kernel_status_known"] or item["status"]==0),"A091_CONFIRMED_OWN_HANDLE_CLOSE_UNKNOWN_NOT_STATUS")
    need(0<outer["aggregate_raw_RSS_peak_bytes"]<=268435456 and 0<outer["raw_self_peak_KiB"]*1024<=67108864 and
        0<=outer["outer_memory_current"]<=67108864 and 0<=outer["inner_memory_current"]<=201326592 and
        outer["aggregate_raw_RSS_peak_bytes"]+resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024<=268435456,"A091_EXISTING_FINITE_RESOURCE_ENVELOPE")
    encoded=outer["native_inner_DATA_hex"];inner=None
    signal_case=case in ("Root_signal_positive","Root_signal_denied")
    signal_prefix=None
    if inner_stdout is None:
        need(type(encoded) is str and 0<len(encoded)<=32768,"A091_COMPLETE_CALLER_PIPE_DATA");raw=bytes.fromhex(encoded)
    else:
        need(type(inner_stdout) is bytes and 0<len(inner_stdout)<=1048576,"A118_COMPLETE_CALLER_PIPE_DATA");raw=inner_stdout
    need(raw.endswith(b"\n") and hashlib.sha256(raw).hexdigest()==outer["inner_terminal_sha256"],"A091_COMPLETE_CALLER_PIPE_SHA")
    if signal_case:
        lines=raw.splitlines(keepends=True);need(len(lines)==(1 if case=="Root_signal_positive" else 2),"A091_PRECISE_SIGNAL_PREFIX_AND_FINAL_PATHSET")
        signal_prefix=json_DATA(lines[0])
        need(signal_prefix["schema"]=="friday.a091.pre-signal-receipt.v1" and signal_prefix["case"]==case and
            signal_prefix["pid"]==control["coordinator_pid"] and signal_prefix["parent"]==owned["pid"] and signal_prefix["birth"]==control["coordinator_birth"] and
            signal_prefix["uid"]==signal_prefix["gid"]==1000 and signal_prefix["child_pid"]==owners[1]["pid"] and signal_prefix["child_birth"]==owners[1]["birth"] and
            0<signal_prefix["clone_ns"]<=signal_prefix["register_ack_ns"]<=signal_prefix["release_ns"]<=signal_prefix["ready_ns"] and
            signal_prefix["private_wait4"] is False and signal_prefix["status_known"] is False and signal_prefix["status"]==0 and
            signal_prefix["body"]=="ordinary-owned-a091\n","A091_ACTUAL_READY_WITNESS_BEFORE_ROOT_SIGNAL")
        if case=="Root_signal_denied":raw=lines[1]
    if case=="Root_signal_positive":
        need(all(item["signal_attempts"]==1 and item["signal_errno"]==0 and item["signal_ns"]>=signal_prefix["ready_ns"] and
            item["kernel_status_known"] is True and item["status"]==9 for item in owners),"A091_ACTUAL_SUCCESSFUL_ROOT_SIGNALS_AND_PRIVATE_WAIT4")
    else:
        need(type(raw) is bytes and 0<len(raw)<=1048576,"A091_COMPLETE_CALLER_RECEIPT")
        need(raw.endswith(b"\n") and raw.count(b"\n")==1,"A091_CALLER_FULL_SINGLE_FINAL_FRAME")
        inner=json_DATA(raw)
        if case=="inherited_start_owner":inherited_owner_oracle(inner,outer,control)
        else:
            need(inner["schema"]=="friday.a091.stock-caller-receipt.v1" and inner["case"]==case and inner["start_ok"] is True and
                inner["pid"]==control["coordinator_pid"] and inner["birth"]==control["coordinator_birth"] and inner["parent"]==owned["pid"] and
                inner["origin_birth"]==owned["birth"] and [inner["uid"],inner["gid"]]==[row["context"]["actual_uid"],row["context"]["actual_gid"]] and
                inner["result"]==row["caller_result"] and inner["cleanup_errno"]==0 and inner["status_authority"] is False and
                inner["plaintext_created"]==inner["plaintext_closed"]==row["plaintexts"] and len(inner["rows"])==row["created"],"A091_ACTUAL_PUBLIC_ACTOR_COMPLETE_RESULT_AND_CLEANUP")
            operand=inner["self_register_operand"]
            if case=="register_without_intent":
                need(operand=={"opened":1,"closed":1,"close_errno":0,"retained":False,
                    "pid":control["coordinator_pid"],"birth":control["coordinator_birth"],
                    "handle_pid":control["coordinator_pid"]} and inner["rows"]==[] and
                    inner["send_count"]==1 and inner["receive_count"]==0 and inner["next_sequence"]==2 and
                    inner["intent_ack_ns"]==0 and inner["last_send_type"]==4,
                    "A104_EXACT_OWN_SELF_PIDFD_CLOSED_NO_INTENT_CLONE_OR_STATUS")
            else:need(operand=={"opened":0,"closed":0,"close_errno":0,"retained":False,
                "pid":0,"birth":0,"handle_pid":-1},"A104_SELF_REGISTER_OPERAND_ABSENT_IN_OTHER_CONTOURS")
            transport=inner["transport"]
            need(transport["attempts"]==1 and transport["close_errno"]==0 and transport["closed"] is True and transport["close_ns"]>0,"A091_ACTUAL_PRODUCER_TRANSPORT_CLOSE")
            for i,item in enumerate(inner["rows"]):
                need(item["pid"]>0 and item["birth"]>0 and item["private_wait4"] is True and item["handle_closed"] is True and
                    item["close_ns"]>=item["wait4_ns"]>0 and item["registered"] is row["registered"] and item["ready"] is row["ready"] and
                    item["reap_ack"] is row["reap_ack"] and item["status_known"] is row["reap_ack"] and
                    item["status"]==((23+i)<<8 if row["reap_ack"] else 0),"A091_FULL_OWN_CHILD_CAUSAL_RECEIPT_NO_STATUS_ADOPTION")
                if item["ready"]:need(0<item["clone_ns"]<=item["register_ack_ns"]<=item["release_ns"]<=item["ready_ns"]<=item["wait4_ns"],"A091_ACTUAL_REGISTER_ACK_BEFORE_READY")
                else:need(item["register_ack_ns"]==item["release_ns"]==item["ready_ns"]==0,"A091_NO_READY_BEFORE_ACCEPTANCE")
                if item["reap_ack"]:need(item["wait4_ns"]<item["reap_ack_ns"]<=item["close_ns"],"A091_PRIVATE_WAIT4_BEFORE_ACK_AND_PUBLICATION")
            row["actor_rows"]=inner["rows"]
            resources=inner["resources"]
            need(resources["AS"]==[50331648,50331648] and resources["CPU"]==[180,180] and resources["FSIZE"]==[2147483648,2147483648] and
                resources["CORE"]==[0,0] and resources["NOFILE"]==[512,512] and 0<resources["self_peak_bytes"]<=50331648 and
                0<=resources["children_peak_bytes"]<=201326592,"A091_ACTUAL_CALLER_AND_OWN_CHILD_RESOURCE_RECEIPT")
            ack=inner["ack_evidence"]
            need(ack["version"]==1 and ack["phase"]==row["caller_ack_phase"] and ack["stage"]==row["caller_ack_stage"] and
                ack["primitive_errno"]==row["caller_ack_errno"] and ack["expected_peer"]==[owned["pid"],0,0] and
                ack["rights"]==ack["rights_closed"]==ack["rights_close_errno"]==0,"A091_EXACT_PUBLIC_CALLER_ACK_PRIMITIVE")
            if case=="reap_invalid_ACK":
                a,b=PACKET.unpack(bytes.fromhex(ack["expected"])),PACKET.unpack(bytes.fromhex(ack["observed"]))
                wanted=list(a);wanted[3]=11
                need(a[3]==8 and tuple(wanted)==b and ack["peer"]==[owned["pid"],0,0] and ack["status_redacted"] is True,
                    "A091_FULL_INVALID_REAP_ACK_ORIGIN_AND_SINGLE_TYPE_DELTA")
            elif ack["stage"]==5:need(ack["peer"]==[-1,-1,-1] and bytes.fromhex(ack["observed"])==bytes(96),"A091_NO_INVENTED_ACK_AFTER_ROOT_REFUSAL")
            else:need(ack["peer"]==[owned["pid"],0,0] and ack["expected"]==ack["observed"],"A091_EXACT_ACTUAL_VALID_LAST_ACK")
            if signal_prefix is not None:need(inner["rows"][0]["pid"]==signal_prefix["child_pid"] and inner["rows"][0]["birth"]==signal_prefix["child_birth"],"A091_SIGNAL_PREFIX_FINAL_EXACT_BINDING")
            if case in ("abort_positive","abort_detail","abort_right"):need(inner["creation_errno"]==24,"A091_REAL_EMFILE_NOT_CREATION_LABEL")
    if case=="Root_signal_denied":need(all(item["signal_attempts"]==1 and item["signal_errno"]==1 and item["signal_ns"]>0 for item in owners),"A091_REAL_ROOT_SIGNAL_DENIAL_ONE_ATTEMPT")
    if row["target_kind"]:
        targets=[event for event in events if event["kind"] in row["target_kind"] and event["stage"]==row["stage"] and event["primitive_errno"]==row["errno_by_kind"][str(event["kind"])]]
        need(len(targets)==1,"A091_ONE_EXACT_REQUIRED_STAGE_NOT_GENERIC_FAILURE")
        target=targets[0];need(target["phase"]==row["target_phase"] or case in ("transport_closed","Root_signal_positive","Root_signal_denied","Root_transport_close_denied"),"A091_EXACT_REQUIRED_PHASE")
        if target["kind"]==1 and target["receive_bytes"]:
            if case=="peer_pid":row["actual_peer_pid"]=owners[1]["pid"]
            receiving_packet(row,target,control,owned)
        if target["kind"]==2:need(target["pending_role"]==0 and root["failure_ns"]>=target["at_ns"]>=target["pending_end"]>0 and
            inner["intent_ack_ns"]>0 and (inner["rows"]==[] if case=="pending_timeout" else
                len(inner["rows"])==1 and inner["last_send_type"]==4 and inner["last_send_ns"]>target["pending_end"]),"A091_REAL_PENDING_TIMEOUT_OR_ACTUAL_LATE_REGISTER_NOT_LABELS")
        if target["kind"]==3:need(target["receive_flags"]& (select.POLLHUP|select.POLLERR|select.POLLNVAL),"A091_ACTUAL_POLL_TRANSPORT_CLOSE")
        if target["kind"]==1 and not target["receive_bytes"]:need(target["credentials"]==target["rights"]==target["rights_closed"]==0 and target["peer"]==[-1,-1,-1],"A091_ACTUAL_EOF_NO_INVENTED_CREDENTIALS")
    else:
        phases=tuple(row["receive_phases"]) if case=="positive" else (2,6)+(2,4,7)*3+(12,9) if case=="abort_positive" else (2,4,7)*3+(12,9)
        need(tuple(event["phase"] for event in receives)==phases and all(event["stage"]==event["primitive_errno"]==0 and
            event["credentials"]==1 and event["receive_bytes"]==96 and event["rights"]==(1 if event["phase"]==4 else 0) and
            event["rights_closed"]==0 and event["rights_close_errno"]==0 and event["ack_ns"]>=event["at_ns"]>0 for event in receives),"A091_ENTIRE_ORDINARY_POSITIVE_PHASE_COMPLEMENT")
        need(all(not item["kernel_status_known"] and item["reaped"] and item["status"]==0 for item in owners[1:]),"A091_BORROWED_REAP_ACK_NEVER_ROOT_KERNEL_STATUS")
        coord,coord_birth=control["coordinator_pid"],control["coordinator_birth"]
        actors=([{**item["native"],"register_ack_ns":item["evidence"]["register_ack_ns"],"release_ns":item["evidence"]["release_ns"]} for item in inner["rows"]]
            if case=="inherited_start_owner" else inner["rows"])
        worker=0
        for offset,event in enumerate(receives):
            packet=PACKET.unpack(bytes.fromhex(event["packet_hex"]))
            need(packet[0]==b"FRA061P1" and packet[1].hex()==row["context"]["session_hex"] and packet[2]==1 and
                packet[3]==event["phase"] and packet[4]==2+offset and packet[7]==coord and packet[11]==coord_birth and
                packet[12]==row["context"]["work_ns"] and event["peer"]==event["expected_peer"]==[coord,1000,1000] and
                event["receive_flags"]==0 and event["status_argument_invalid"] is False,"A091_FULL_POSITIVE96_FRAME_SEQUENCE_ORIGIN_COMPLEMENT")
            phase=event["phase"]
            if case=="positive" and phase in (2,4,7):worker=packet[5]
            if phase in (2,6):
                need(packet[5]==worker and packet[6]==packet[8]==packet[10]==0 and packet[9]==(24 if phase==6 else 0),"A091_POSITIVE_INTENT_REAL_ABORT_BODY")
            elif phase in (4,7):
                actor=actors[worker]
                need(packet[5]==worker and packet[6]==actor["pid"] and packet[10]==actor["birth"] and packet[9]==0 and
                    packet[8]==((23+worker)<<8 if phase==7 else 0) and event["handle_pid"]==(actor["pid"] if phase==4 else -1) and
                    (phase!=4 or event["proc_parent"]==event["expected_parent"]==coord and event["proc_birth"]==event["expected_birth"]==actor["birth"]),"A091_FULL_POSITIVE_ACTUAL_OWN_HANDLE_PARENT_BIRTH_BODY")
                if phase==4:need(event["ack_ns"]<=actor["register_ack_ns"]<=actor["release_ns"],"A091_ROOT_SEND_ACK_BEFORE_ACTUAL_CALLER_RELEASE")
                else:worker+=1
            else:need(packet[5]==packet[6]==packet[8]==packet[9]==packet[10]==0,"A091_FULL_POSITIVE_DRAIN_FINISH_ZERO_BODY")
    return inner

def run_receiving_positive_first(positive,controlled):
    need(positive["schema"]==controlled["schema"]=="friday.a091.stock-public-call.v1" and positive["case"]=="positive" and
        controlled["case"]!="positive" and positive["capsule_sha256"]!=controlled["capsule_sha256"] and
        positive["source_manifest_sha256"]==controlled["source_manifest_sha256"] and positive["oracle_sha256"]==controlled["oracle_sha256"] and
        all(positive["source_sha256"][role]==controlled["source_sha256"][role] for role in SOURCES if role not in (116,117,119)),"A091_SAME_PUBLIC_PROGRAM_FULL_POSITIVE_FIRST")
    a,b=(os.fstat(value["fds"][122]) for value in (positive,controlled))
    need((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino),"A091_FRESH_ACTUAL_INDEPENDENT_VIEWS")
    ordinary=run_connected(positive)
    if not ordinary["accepted"]:return {"positive":ordinary,"controlled":None,"controlled_disposition":"NOT_RUN_POSITIVE_REFUSED","body_credit":False,"all216_credit":False,"SourceReady_granted_here":False,"current_GO":False,"F10_waiver":False}
    negative=run_public(controlled)
    return {"positive":ordinary,"controlled":negative,"runtime_native_controls":True,"body_credit":False,"all216_credit":False,
        "SourceReady_granted_here":False,"current_GO":False,"F10_waiver":False}

def bind_inner_streams(outer,streams):
    # Unknown until native original facts AND actual transfer correspondence
    # bind. On a refusal, retain both producer records and both immutable raws.
    for received in streams:
        received["full_original_bounded_raw"]=received["original_stream_complete"]=None
        received["native_original"]=None;received["native_original_binding_error"]=None
    transport=outer.get("native_inner_streams")
    need(type(transport) is dict and transport.get("schema")=="friday.a118.native-raw-transport.v1" and
        type(transport.get("streams")) is list and len(transport["streams"])==2,"A118_REAL_NATIVE_RAW_TRANSPORT")
    errors=[]
    for index,(producer,received) in enumerate(zip(transport["streams"],streams)):
        received["native_original"]=producer if type(producer) is dict else None
        try:
            need(type(producer) is dict,"A153_NATIVE_ORIGINAL_STREAM_RECORD")
            # A completely transferred retained prefix is still incomplete
            # when native original EOF is missing or overflow/read refusal
            # occurred. Those facts describe the original, not the transfer.
            incomplete=(producer.get("eof") is False or producer.get("overflow") is True or
                producer.get("read_errno",0)!=0 or producer.get("read_close_errno",0)!=0 or
                producer.get("total_seen")!=producer.get("retained_size"))
            if incomplete:
                received["full_original_bounded_raw"]=received["original_stream_complete"]=False
            need(producer["eof"] is True and producer["overflow"] is False and
                producer["total_seen"]==producer["retained_size"] and producer["read_errno"]==producer["read_close_errno"]==0,
                "A153_NATIVE_ORIGINAL_EOF_OVERFLOW_READ_SIZE")
            need(producer["stream"]==index and producer["cap"]==received["cap"]==STREAM_CAP and
                producer["retained_size"]==producer["emitted_bytes"]==received["retained_size"]==received["size"] and
                producer["sha256"]==received["sha256"]==received["observed_sha256"] and
                producer["prefix_hex"]==received["prefix_hex"] and
                _identity9(producer["writer_identity9"])==received["pipe_identity9"] and
                _identity9(producer["writer_identity9_after"])==received["pipe_final_identity9"] and
                producer["writer_identity9"][:7]==producer["writer_identity9_after"][:7],
                "A118_EXACT_RAW_PIPE_SIZE_SHA_IDENTITY")
            need(producer["write_errno"]==producer["close_errno"]==0 and producer["writer_closed"] is True and
                producer["close_ns"]>0 and received["transfer_eof"] is True and received["transfer_overflow"] is False and
                received["transfer_complete"] is True and received["observed_sha_complete"] is True and
                received["read_errno"] is None and received["close_errno"] is None and received["retention_error"] is None,
                "A118_INDEPENDENT_PRODUCER_AND_RECEIVER_EOF_CLOSE")
            if index==0:need(received["sha256"]==outer["inner_terminal_sha256"],"A118_NATIVE_FULL_ORIGINAL_STDOUT_SHA")
            received["full_original_bounded_raw"]=received["original_stream_complete"]=True
        except BaseException as exc:
            received["native_original_binding_error"]=public_error(exc);errors.append(public_error(exc))
    need(not errors,"A153_NATIVE_ORIGINAL_BIND_REFUSED:"+";".join(errors))

def _identity9(values):
    need(type(values) is list and len(values)==9 and all(type(v) is str and v.isdecimal() for v in values),
        "A118_IDENTITY9_DECIMAL_STRINGS")
    return values

def consume_connected_positive(receipt,row,snapshots):
    outer=receipt["outer"];stock=receipt["inner"];owned=receipt["owned"];driver=receipt["driver"]
    control=outer["public_control"];root=outer["root_receiving"]
    need(receipt["case"]==stock["case"]==root["case"]==control["case"]=="positive" and
        not receipt["causes"] and not receipt["cleanup_errors"] and stock["result"]==stock["cleanup_errno"]==0,
        "A118_POSITIVE_ACTUAL_ACCEPTED_PRODUCER")
    need(driver["pid"]==owned["owner"] and driver["pid"]!=owned["pid"] and
        control["origin_pid"]==owned["pid"] and control["origin_parent"]==driver["pid"] and
        control["origin_birth"]==owned["birth"] and owned["reaped"] is True and owned["handle_closed"] is True and
        owned["kernel_status"]==0 and 0<owned["wait_ns"]<=owned["close_ns"],"A118_DRIVER_OWNS_OUTER_NOT_COORDINATOR")
    owners=root["owned"]
    need(len(owners)==4 and owners[0]["index"]==0 and owners[0]["role"]==-1 and
        owners[0]["pid"]==control["coordinator_pid"]==stock["pid"] and
        owners[0]["birth"]==control["coordinator_birth"]==stock["birth"] and
        stock["parent"]==owned["pid"] and stock["origin_birth"]==owned["birth"] and
        owners[0]["pid"] not in (owned["pid"],driver["pid"]) and
        owners[0]["kernel_status_known"] is True and owners[0]["status"]==0,"A118_ACTUAL_ROOT_RECORDS0_DIRECT_CUSTODY")
    root_live=root["all3_live"];stock_live=stock["all3_live"]
    need(type(root_live) is dict and type(stock_live) is dict and
        root_live["inner_processes"]==stock_live["inner_processes"]==4 and
        len(root_live["members"])==len(stock_live["members"])==4 and
        0<root_live["observed_ns"]<=stock_live["observed_ns"]<row["context"]["work_ns"],"A118_TWO_ACTUAL_ALL3_LIVE_OBSERVATIONS")
    need(stock_live["uid"]==stock_live["gid"]==1000 and stock_live["same_held_image"] is True and
        root_live["same_actual_image_and_argv"] is True and
        stock_live["held_image_sha256"]==root_live["image_sha256"]==receipt["bindings"]["source_sha256"][110] and
        _identity9(stock_live["held_image_identity9"])==_identity9(root_live["image_identity9"])==
        [str(v) for v in snapshots[110][0]],"A118_SAME_ACTUAL_ROOT_SELECTED_NATIVE_IMAGE")
    argv=b"friday-approved-native-browser3\0--held-a061\0"+receipt["bindings"]["capsule_sha256"].encode("ascii")+b"\0"
    need(stock_live["own_argv_sha256"]==root_live["argv_sha256"]==hashlib.sha256(argv).hexdigest() and
        stock_live["own_argv_bytes"]==root_live["argv_bytes"]==len(argv),
        "A118_ACTUAL_INHERITED_EXACT_SELECTED_ARGV")
    seen=set();workers=[]
    for slot,(a,b,final) in enumerate(zip(root_live["members"],stock_live["members"],owners)):
        pid=final["pid"];birth=final["birth"];parent=owned["pid"] if slot==0 else stock["pid"];role=slot-1
        need(a["slot"]==b["slot"]==slot and a["role"]==b["role"]==final["role"]==role and
            a["pid"]==b["pid"]==a["handle_pid"]==b["handle_pid"]==pid and
            a["birth"]==b["birth"]==birth and a["parent"]==b["parent"]==parent and
            type(pid) is int and pid>0 and pid not in seen and pid not in (owned["pid"],driver["pid"]),
            "A118_LIVE_PHASE_PID_BIRTH_ROLE_PARENT_COMPLEMENT")
        seen.add(pid);_identity9(a["pidfd_identity9"]);_identity9(b["pidfd_identity9"])
        need(final["reaped"] is True and final["handle_retained"] is False and final["close_errno"]==0 and
            final["close_ns"]>0,"A118_ROOT_FINAL_EXACT_HANDLES")
        if slot:
            actor=stock["rows"][role]
            need(a["pidfd_identity9"]==b["pidfd_identity9"] and actor["pid"]==pid and actor["birth"]==birth and
                actor["registered"] is True and actor["ready"] is True and actor["private_wait4"] is True and
                actor["reap_ack"] is True and actor["handle_closed"] is True and actor["status_known"] is True and
                actor["status"]==(23+role)<<8 and actor["ready_ns"]<=stock_live["observed_ns"]<actor["wait4_ns"] and
                actor["wait4_ns"]<actor["reap_ack_ns"]<=actor["close_ns"] and
                final["kernel_status_known"] is False and final["status"]==0,"A118_WORKER_PRIVATE_WAIT_VERSUS_ROOT_BORROWED_ACK")
            body=bytes.fromhex(actor["body_hex"])
            need(body==b"ordinary-owned-a091\n" and actor["body_size"]==len(body),"A118_ACTUALLY_READ_WORKER_BODY")
            workers.append({"role":role,"pid":pid,"birth":birth,"parent":stock["pid"],
                "native_owner_receipt":actor,"Root_membership_receipt":final,"live_root":a,"live_private":b,
                "body_raw":body,"body_sha256":hashlib.sha256(body).hexdigest(),"Root_kernel_status_credit":False})
    receives=[event for event in root["events"] if event["kind"]==1]
    need(tuple(event["phase"] for event in receives)==tuple(row["receive_phases"])==(2,4,2,4,2,4,7,7,7,12,9),
        "A118_REGISTRY_ALLOCATE_ALL3_BEFORE_PRIVATE_REAPS")
    need(root_live["observed_ns"]<=receives[5]["ack_ns"]<=stock["rows"][2]["register_ack_ns"]<=
        stock["rows"][2]["ready_ns"]<=stock_live["observed_ns"]<min(v["wait4_ns"] for v in stock["rows"]),
        "A118_CAUSAL_ROOT_REGISTER_THEN_ALL_READY_THEN_WAIT")
    streams={key:receipt[key] for key in ("stdout_stream","stderr_stream","inner_stdout_stream","inner_stderr_stream")}
    need(all(v["raw"] is not None and v["full_original_bounded_raw"] is True for v in streams.values()),
        "A118_RETURNED_DURABLE_RAW_CUSTODY")
    return {"schema":"friday.a118.connected-positive-consumption.v1","case":"positive","status":"RECEIPTS_CHECKED",
        "driver":driver,"outer_origin":owned,"coordinator":owners[0],"workers":workers,
        "all3_live_root":root_live,"all3_live_private":stock_live,"streams":streams,"raw_retained":True,
        "session_hex":row["context"]["session_hex"],"bindings":receipt["bindings"],
        "body_credit":False,"all216_credit":False,"GO":False,"SourceReady_granted_here":False}

def run_connected(bundle):
    need(type(bundle) is dict and bundle.get("schema")=="friday.a091.stock-public-call.v1" and
        bundle.get("case")=="positive","A118_CONNECTED_ACTUAL_STOCK_INPUT")
    return run_public(bundle)

# Data-only bothside validation. Exact original table/order/cause is separate.
def a171_validate_input(data,meta,need):
    args=data["arguments"];consumer=meta["consumer"];case=data["id"]
    if data["producer"]!="a171_actual":return
    schemas={"sealed_bytes":{"parent","relative","sha256","cap"},
        "require_absent_target":{"parent","relative"},"RetainedTree.check":{"parent","inventory"},
        "BoundedOutput/G1.Output.check/create":{"parent","relative","raw_hex"},
        "R4.guarded_digest":{"parent","relative","cap","sha256"},
        "ca_context/R4.guarded_digest":{"parent","relative","cap","sha256"},
        "ca_context/SSLContext.load_verify_locations":{"parent","relative","cap","sha256"},
        "G1.resources":set(),"acquisition_resources":set(),
        "Supervisor.runtime_preflight":{"parent","manifest_sha256"},
        "Supervisor.cgroup_values":{"parent"},"Supervisor.HeldSource.bytes":{"parent","relative","sha256"},
        "Supervisor.seal":{"parent","relative","sha256"},
        "Supervisor.NativeLaunchAdapter":{"parent","relative","sha256"},
        "terminal_bytes":{"value"},"Run.guard":set()}
    wanted=schemas.get(consumer,set())
    if case=="held_path_replaced_exact_bytes":wanted=wanted|{"mutation_hex"}
    if consumer=="RetainedTree.check" and case!="retained_positive":wanted=wanted|{"mutation_hex"}
    if consumer=="emit_terminal" and case in ("terminal_output_path","terminal_sink_blocked"):wanted={"value"}
    if case=="outer_sink_blocked":wanted={"value"}
    if meta["owned_children"]:wanted={"parent","inventory"} if case in {
        "whole3_actual_G1_worker_positive","whole3_http404","whole3_tls_certificate","whole3_tls_protocol",
        "whole3_truncated","body_short_write","body_partial_write","body_eof_at_cap"} else set()
    need(type(args) is dict and set(args)==wanted,"A171_BOTHSIDE_EXACT_TYPED_OPERANDS")
    if "parent" in args:
        need(type(args["parent"]) is str and len(args["parent"])<=256 and
            args["parent"].startswith("/var/tmp/astra-e4-browser3-a061-offline-") and
            os.path.dirname(args["parent"])=="/var/tmp","A171_ORIGINAL_OWNED_INPUT_SCOPE")
    if "relative" in args:
        need(type(args["relative"]) is str and len(args["relative"])<=256 and
            all(part not in ("",".","..") for part in args["relative"].split("/")),"A171_OWNED_PATH_OPERAND")
    for key in ("sha256","manifest_sha256"):
        if key in args:need((case=="outer_unknown_pin" and key=="sha256" and args[key] is None) or
            (type(args[key]) is str and len(args[key])==64 and
            all(ch in "0123456789abcdef" for ch in args[key])),"A171_TYPED_SHA_OPERAND")
    if "cap" in args:need(type(args["cap"]) is int and 0<=args["cap"]<=2147483648,"A171_EXACT_INTEGER_CAP")
    for key in ("raw_hex","mutation_hex"):
        if key in args:need(type(args[key]) is str and len(args[key])<=524288 and
            len(args[key])%2==0 and all(ch in "0123456789abcdef" for ch in args[key]),"A171_OWNED_INERT_BYTES")
    if "inventory" in args:need(type(args["inventory"]) is dict and
        args["inventory"].get("root")==args["parent"]+"/held","A171_ORIGINAL_RETAINED_INPUT_DOMAIN")

def a066_resource_observation(value,meta,need,case):
    data=value['facts']['resource_instrument']
    need(data['schema']=='friday.sol066.resource-source-instrument.v1' and data['id']==case and
        data['consumer']==meta['consumer']=='acquisition_resources' and data['domain']=='inner192' and
        data['source_instrument_only'] is True and data['kernel_live_admission_credit'] is False and
        data['entered'] is True and data['cleanup_confirmed'] is True,
        'RESOURCE_EXACT_INNER_INSTRUMENT_NOT_HOST_PREPARER')
    trace=data['predicates'];errors=[row for row in trace if not row['ok']]
    need((not errors and data['returned'] is True and data['fault'] is None and data['fault_applied']==0)
        if meta['cause'] is None else len(errors)==1 and trace[-1]==errors[0] and
        errors[0]['cause']==meta['cause'] and data['fault'] is not None and data['fault_applied']==1 and
        data['changed_observations']==1 and data['returned'] is False, 'RESOURCE_ACTUAL_UNCHANGED_PREDICATE_FIRST_FAULT')
    if meta['cause'] is None:
        need(all(not record.get('changed',False) and all(row['captured']==row['presented'] and
            row['read_error'] is row['presentation_error'] is None for row in record.get('reads',[]))
            for record in data['reads']), 'RESOURCE_NO_PRESENTED_POSITIVE_FACTS')

def a171_validate_observation(value,meta,need,case):
    """Called separately by producer's original controller AND external A087 oracle."""
    need(type(value) is dict and value.get("schema")=="friday.a171.actual-operation-observation.v1",
        "A171_FULL_OPERATION_OBSERVATION")
    if meta['consumer']=='acquisition_resources':a066_resource_observation(value,meta,need,case)
    need(value["unavailable_reason"] is None,"A171_MANDATORY_CODE_GAP_NOT_A_PASS")
    need(value["resource_precondition"] is not None and value["resource_precondition"]["passed"] is True,
        "A171_ACTUAL_OPERATION_RESOURCE_PRECONDITION")
    need(not value["facts"].get("auxiliary_close_unconfirmed",False) and
        all((record["fd"] is None or (record["closed"] is True and record["close_attempted"] is True)) and
            record["close_error"] is None for record in value["descriptor_records"]),
        "A171_ACTUAL_OPERATION_DESCRIPTOR_CLEANUP")
    dependency_errors=[item for item in value["facts"].get("run_errors") or [] if item!=meta["cause"]]
    need(not dependency_errors and value["facts"].get("sticky_uncertainty") is not True,
        "A171_DEPENDENCY_CLEANUP_BLOCKS_SUCCESS")
    need(value["state"]==meta["state"] and value["started_routes"]==meta["started_routes"],
        "A171_ORIGINAL_STATE_STARTS")
    need(type(value["stages"]) is dict and value["stages"].get(meta["stage"],0)>0,
        "A171_ORIGINAL_STAGE_REACHED")
    calls=value["consumer_calls"]
    need(type(calls) is list and len(calls)>=meta["minimum_consumer_calls"] and
        all(type(call) is dict and (call["returned"] or call["error"] is not None) for call in calls),
        "A171_ACTUAL_ORIGINAL_CONSUMER_INVOCATIONS")
    aliases={"mapping_check/tokens/container/route_object":"mapping_check","mapping_check/container":"mapping_check"}
    wanted=aliases.get(meta["consumer"],meta["consumer"])
    need(sum(call["consumer"]==wanted for call in calls)>=meta["minimum_consumer_calls"],
        "A171_ACTUAL_REQUESTED_CONSUMER_INVOCATIONS")
    hashes=value["hashes"];children=value["target_children"]
    need(type(hashes) is list and type(children) is list and len(children)==meta["owned_children"],
        "A171_ORIGINAL_ACTUAL_TARGET_CHILDREN")
    if meta["hashes"]=="none":need(len(hashes)==0,"A171_ORIGINAL_NO_HASH")
    elif meta["hashes"]=="exact3":need(len(hashes)==3,"A171_ORIGINAL_EXACT3_HASH")
    elif meta["hashes"]=="at_least1":need(len(hashes)>=1,"A171_ORIGINAL_HASH_REACHED")
    elif meta["hashes"]=="stage_dependent":
        need(value["facts"].get("actual_stage_hash_relation") is True,"A171_STAGE_DEPENDENT_HASH_UNPROVEN")
    else:need(False,"A171_UNKNOWN_ORIGINAL_HASH_DOMAIN")
    fault=value["first_fault"]
    need(fault is None if meta["cause"] is None else type(fault) is dict and fault["cause"]==meta["cause"],
        "A171_ORIGINAL_ACTUAL_FIRST_FAULT")
    need(type(value["phase_order"]) is list and all(type(phase["at_ns"]) is str and phase["at_ns"].isdecimal()
        for phase in value["phase_order"]),"A171_ACTUAL_PHASE_ORDER")
    actual_times=[int(phase["at_ns"]) for phase in value["phase_order"]]
    need(actual_times==sorted(actual_times),"A171_ACTUAL_MONOTONIC_PHASE_ORDER")
    if case.startswith("outer_") or case.startswith("owned_registration_"):
        labels=[item.get("label") for item in value["operation_raw"]]
        need("outer_stdout" in labels and "outer_stderr" in labels,"A171_COMPLETE_OUTER_STREAMS_REQUIRED")
    if case.startswith("owned_registration_"):
        instrument=value["facts"]["registration_instrument"]
        need(instrument["schema"]=="friday.sol067.owned-registration-instrument.v1" and
            instrument["id"]==case and instrument["source_instrument_only"] is True and
            instrument["kernel_failure_credit"] is False and instrument["fault_applied"]==1,
            "A067_EXACT_SOURCE_REGISTRATION_PRESENTATION_NOT_KERNEL_FAILURE")
        changed=[row for row in instrument["observations"] if row["changed"]]
        need(len(changed)==1 and len(children)==1 and changed[0]["pid"]==children[0]["pid"] and
            changed[0]["generation"]==instrument["generation"] and
            changed[0]["owner"]>0 and changed[0]["owner_birth"]>0,
            "A067_ACTUAL_SAME_OWNED_CHILD_PRESENTATION")
    for raw in value["operation_raw"]:
        if raw.get("transport")=="stdout_frame" and type(raw.get("raw_hex")) is not str:
            need(False,"A171_FULL_RAW_FRAME_NOT_RESTORED")
        actual=bytes.fromhex(raw["raw_hex"])
        need(raw["hash_only"] is False and len(actual)==raw["bytes"] and
            hashlib.sha256(actual).hexdigest()==raw["sha256"],"A171_FULL_OPERATION_RAW_HASH")
    # Positive source credit also needs per-ID controlled post-admission facts.
    # Their absence remains a CODE residual even if a simpler physical call returned.
    if case=="held_kernel_write_refused":
        need(value["facts"].get("actual_kernel_write_errno")==1,"A171_ACTUAL_ORIGINAL_KERNEL_SEAL_EPERRM")
    if case=="held_path_replaced_exact_bytes":
        need(value["facts"].get("post_capture_path_replacement_observed") is True,
            "A171_ACTUAL_POST_CAPTURE_PATH_CONTROL_REQUIRED")

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
