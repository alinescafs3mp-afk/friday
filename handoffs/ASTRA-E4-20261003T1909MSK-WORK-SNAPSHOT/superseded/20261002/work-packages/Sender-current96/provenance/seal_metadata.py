"""Own finite stock SHA/stat/JSON seal only. Never imports or executes Source."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from datetime import datetime, timezone, timedelta

ROOT = Path('/var/tmp/friday-sol056-sol055-whole54-all32-remaining-connected-source-implementation')
TERMINAL = Path('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL056-SOL055-WHOLE54-ALL32-REMAINING-CONNECTED-SOURCE-IMPLEMENTATION-RESULT.json')
ASSIGNMENT = 'ASTRA-E4-SOL056-SOL055-WHOLE54-ALL32-REMAINING-CONNECTED-SOURCE-IMPLEMENTATION'
STATUS = 'SUBSTANTIAL_CONNECTED_CODE_ATTEMPT_WITH_EXACT_MANDATORY_RESIDUAL'
READ_BYTES = 0
MAX_READS = 24 * 1024 * 1024
CACHE = {}
MSK = timezone(timedelta(hours=3))

def nine(s):
    return [str(v) for v in (s.st_dev, s.st_ino, s.st_mode, s.st_uid,
        s.st_gid, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)]

def read(path):
    global READ_BYTES
    path = Path(path)
    a = path.lstat()
    assert stat.S_ISREG(a.st_mode) and a.st_nlink == 1
    assert a.st_uid == a.st_gid == 1000 and stat.S_IMODE(a.st_mode) == 0o600
    assert READ_BYTES + a.st_size <= MAX_READS
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        b = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            READ_BYTES += len(chunk)
            assert READ_BYTES <= MAX_READS
            chunks.append(chunk)
        raw = b''.join(chunks)
        c = os.fstat(fd)
    finally:
        os.close(fd)
    assert nine(a) == nine(b) == nine(c) == nine(path.lstat())
    assert len(raw) == a.st_size
    pin = {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
        'identity9_decimal_strings': nine(c), 'stable9': True, 'private_owned': True}
    return raw, pin

def patch_json(path, value, existing=False):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(',', ':')) + '\n').encode()
    assert path.exists() == existing
    if existing:
        old, _ = read(path)
        patch = '*** Update File: ' + str(path) + '\n@@\n'
        patch += ''.join('-' + x + '\n' for x in old.decode().splitlines())
    else:
        patch = '*** Add File: ' + str(path) + '\n'
    patch += ''.join('+' + x + '\n' for x in raw.decode().splitlines())
    result = subprocess.run(['apply_patch'], input=('*** Begin Patch\n' + patch +
        '*** End Patch\n').encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert result.returncode == 0, result.stderr.decode()
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        os.chmod(path, 0o600)
    return raw

def inventory():
    entries = {}
    rootstat = ROOT.lstat()
    assert stat.S_ISDIR(rootstat.st_mode) and rootstat.st_uid == rootstat.st_gid == 1000
    for parent, dirs, files in os.walk(ROOT, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            s = path.lstat()
            assert not stat.S_ISLNK(s.st_mode)
            assert stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)
            assert s.st_uid == s.st_gid == 1000
            if stat.S_ISREG(s.st_mode):
                assert s.st_nlink == 1
            entries[str(path.relative_to(ROOT))] = path
    return dict(sorted(entries.items()))

assert os.getuid() == os.getgid() == 1000
assert not TERMINAL.exists() and not TERMINAL.is_symlink()
assert not (ROOT / 'seal.json').exists() and not (ROOT / 'manifest.json').exists()
assert stat.S_IMODE(ROOT.lstat().st_mode) == 0o700
for relative, path in inventory().items():
    s = path.lstat()
    required = 0o700 if stat.S_ISDIR(s.st_mode) else 0o600
    if stat.S_IMODE(s.st_mode) != required:
        # Own new package only. Unchanged private Source pins are not touched.
        os.chmod(path, required)
patch_json(ROOT / 'seal.json', {})
patch_json(ROOT / 'manifest.json', {})
entries = inventory()
root9 = nine(ROOT.lstat())
dirs = []
members = []
for relative, path in entries.items():
    if stat.S_ISDIR(path.lstat().st_mode):
        assert stat.S_IMODE(path.lstat().st_mode) == 0o700
        dirs.append({'path': str(path), 'relative': relative,
            'identity9_decimal_strings': nine(path.lstat()), 'private_owned': True})
    elif relative not in ('seal.json', 'manifest.json'):
        raw, pin = read(path)
        CACHE[relative] = raw, pin
        members.append({**pin, 'relative': relative})

received = json.loads(CACHE['received.json'][0])
cost = json.loads(CACHE['cost-resource-custody-matrix.json'][0])
index = json.loads(CACHE['index/full-source.json'][0])
delta = json.loads(CACHE['full-byte-delta.json'][0])
assert received['assignment'] == cost.get('assignment', ASSIGNMENT) == ASSIGNMENT
assert received['generation'] == 1 and received['compact_completed_verified'] is True
assert time.time() < received['freeze_end_epoch'] < received['wall_end_epoch']
assert len(index['members']) == 54 and len(delta['rows']) == 61
assert len(delta['Source54_changed']) == 1 and len(delta['Source54_equal_complement']) == 53
assert len(delta['actors_changed']) == 7
for row in index['members']:
    pin = CACHE['Source54/' + row['path']][1]
    for key in ('bytes', 'sha256', 'identity9_decimal_strings'):
        assert pin[key] == row[key], (row['path'], key)
for row in delta['rows']:
    pin = CACHE[row['relative']][1]
    for key in ('path', 'bytes', 'sha256', 'identity9_decimal_strings'):
        assert pin[key] == row['current'][key], (row['relative'], key)
assert CACHE['index/full-source.json'][1]['sha256'] == '5064b09f8b6339ff6e5e74fedc4e658dccd323e94d8649af6a6d4cd3e22a6072'
assert len(CACHE['source/caller.py'][0]) <= 65536
assert len(CACHE['source/staged_sender.py'][0]) <= 65536
for pin in received['reused_original_read_proofs']:
    assert nine(Path(pin['path']).lstat()) == pin['identity9_decimal_strings'], pin['path']
for key in ('task', 'input', 'compact'):
    pin = received[key]
    assert nine(Path(pin['path']).lstat()) == pin['identity9_decimal_strings'], pin['path']

seal_value = {'schema': 'friday.sol056.acyclic-member-seal.v1',
    'assignment': ASSIGNMENT, 'generation': 1, 'root': str(ROOT), 'root9': root9,
    'members': members, 'directories': dirs,
    'excluded': ['seal.json', 'manifest.json', str(TERMINAL)],
    'member_byte_total': sum(p['bytes'] for p in members),
    'chain': 'members -> seal -> manifest -> external terminal',
    'status': STATUS, 'whole_implementation_completed': False,
    'SourceReady': False, 'Root_admission': False, 'GO': False,
    'different_author_whole_changed_dependency_review': 'REQUIRED_NOT_RUN',
    'runtime': 'NOT_RUN', 'gates': 'NOT_RUN'}
seal_raw = patch_json(ROOT / 'seal.json', seal_value, existing=True)
assert nine(ROOT.lstat()) == root9
seal_read, seal_pin = read(ROOT / 'seal.json')
assert seal_read == seal_raw
seal_member = {**seal_pin, 'relative': 'seal.json'}
manifest_value = {'schema': 'friday.sol056.full-private-manifest.v1',
    'assignment': ASSIGNMENT, 'generation': 1, 'root': str(ROOT), 'root9': root9,
    'members': members + [seal_member], 'directories': dirs,
    'exact_pathset': list(entries), 'manifest_self_excluded': True,
    'external_terminal_excluded': str(TERMINAL), 'seal': seal_pin,
    'Source54': 54, 'Source54_changed': 1, 'Source54_equal': 53,
    'all_seven_actors_changed': True, 'all32': 32, 'literal_predicates': 132,
    'pure_functions': 16, 'effectful_body_predicates': 72,
    'residual': CACHE['RESIDUAL.txt'][1],
    'closure_matrix': CACHE['closure-matrix.json'][1],
    'all32_matrix': CACHE['all32-current-matrix.json'][1],
    'whole54_matrix': CACHE['whole54-current-matrix.json'][1],
    'consumer_interfaces': CACHE['consumer-interface-matrix.json'][1],
    'resource_custody': CACHE['cost-resource-custody-matrix.json'][1],
    'delta': CACHE['full-byte-delta.json'][1],
    'index': CACHE['index/full-source.json'][1],
    'substantive_freeze_epoch': cost['substantive_freeze_epoch'],
    'status': STATUS, 'whole_implementation_completed': False,
    'SourceReady': False, 'Root_admission': False, 'GO': False,
    'different_author_whole_changed_dependency_review': 'REQUIRED_NOT_RUN',
    'runtime': 'NOT_RUN', 'gates': 'NOT_RUN'}
manifest_raw = patch_json(ROOT / 'manifest.json', manifest_value, existing=True)
assert nine(ROOT.lstat()) == root9
manifest_read, manifest_pin = read(ROOT / 'manifest.json')
assert manifest_read == manifest_raw

# Independent second byte-read/stat pass of this author seal, not semantic review.
before_audit_reads = READ_BYTES
assert list(inventory()) == list(entries)
assert nine(ROOT.lstat()) == root9
for pin in dirs:
    assert nine(Path(pin['path']).lstat()) == pin['identity9_decimal_strings']
for pin in members + [seal_member]:
    raw, current = read(pin['path'])
    for key in ('bytes', 'sha256', 'identity9_decimal_strings'):
        assert current[key] == pin[key], (pin['path'], key)
manifest_final, current = read(manifest_pin['path'])
assert current == manifest_pin and manifest_final == manifest_raw
audit_pass_bytes = READ_BYTES - before_audit_reads
package_bytes = sum(p['bytes'] for p in members) + len(seal_raw) + len(manifest_raw)
assert package_bytes <= 16 * 1024 * 1024
assert nine(ROOT.lstat()) == root9
for pin in received['reused_original_read_proofs']:
    assert nine(Path(pin['path']).lstat()) == pin['identity9_decimal_strings']
finished = int(time.time())
assert finished < received['freeze_end_epoch']
declared_upper = cost['declared_author_read_upper'] + max(0, READ_BYTES - 16 * 1024 * 1024)
assert declared_upper <= cost['author_read_cap']
terminal = {'schema': 'friday.sol056.Source-result.v1', 'assignment': ASSIGNMENT,
    'generation': 1, 'status': STATUS, 'root': str(ROOT), 'root9': root9,
    'manifest': manifest_pin, 'seal': seal_pin,
    'whole_implementation_completed': False, 'SourceReady': False,
    'Root_admission': False, 'runtime': 'NOT_RUN', 'gates': 'NOT_RUN', 'GO': False,
    'Source54_changed_equal': [1, 53], 'actors_changed': 7, 'all32': 32,
    'predicates_pure_effectful': [132, 16, 72],
    'mandatory_open': 'outer-helper custody; first-startup/C IO; opaque full-method/all32 causal replay; damage/aggregate capacity; exact RESIDUAL.txt in manifest',
    'different_author_whole_review': 'REQUIRED_NOT_RUN',
    'accepted_msk': datetime.fromtimestamp(received['accepted_epoch'], MSK).isoformat(),
    'finished_msk': datetime.fromtimestamp(finished, MSK).isoformat(),
    'elapsed_seconds': finished - received['accepted_epoch'],
    'seal_audit_explicit_read_bytes': READ_BYTES,
    'declared_author_read_upper': declared_upper,
    'actual_aggregate_IO_RAM': 'UNKNOWN_NOT_ZERO; known costs/reserves separate',
    'private_fullSHA9_exact_pathset_audit': 'PASS_METADATA_ONLY_NOT_SEMANTIC_ACCEPTANCE'}
terminal_raw = patch_json(TERMINAL, terminal)
assert package_bytes + len(terminal_raw) <= 16 * 1024 * 1024
actual_raw, terminal_pin = read(TERMINAL)
assert actual_raw == terminal_raw
assert list(inventory()) == list(entries) and nine(ROOT.lstat()) == root9
for pin in members + [seal_member, manifest_pin]:
    assert nine(Path(pin['path']).lstat()) == pin['identity9_decimal_strings']
assert time.time() < received['wall_end_epoch']
print(json.dumps({'terminal': terminal_pin, 'status': STATUS,
    'whole_implementation_completed': False, 'metadata_audit': 'PASS',
    'members': len(members), 'directories': len(dirs), 'package_bytes': package_bytes,
    'seal_audit_explicit_read_bytes_before_terminal': terminal['seal_audit_explicit_read_bytes'],
    'terminal_self_read_bytes': len(actual_raw), 'total_helper_read_bytes': READ_BYTES,
    'second_audit_pass_bytes': audit_pass_bytes, 'declared_author_read_upper': declared_upper,
    'finished_msk': terminal['finished_msk'], 'elapsed_seconds': terminal['elapsed_seconds']}))
