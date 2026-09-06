"""Temporary exact-tree transport. Never changes a remote reference."""
from __future__ import annotations
import base64
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

REPOSITORY = 'alinescafs3mp-afk/friday'
BASE = 'ba704a5455fdbe76f518a1479b487cf3cd167665'
TREE = '20c58bdd0ee07fb0262d624af58f3e2ba24abbf9'
PATCH_SHA = '57e4d7087b271e4a4dc786d7932f265c7cb15d7b92089913d191dff99692900e'
PACKED_SHA = '7f6335c41f1fed29483752a3ca2119b3d50711c77489dd3b65207204e08690f2'
TEMPORARY = ('.github/workflows/audit-source-snapshot.yml', '.github/audit-c1.py', '.github/audit-c1.patch.xz.b64')
PATHS = (
    'friday/execution_kernel/web_research_gates.py',
    'friday/orchestration/__init__.py',
    'friday/orchestration/current_file_web_comparison.py',
    'friday/orchestration/supervisor_production_baseline.py',
    'friday/organs/coding/loop.py',
    'friday/organs/coding/static_turn.py',
    'friday/organs/coding/worker_programs.py',
    'friday/organs/coding/worker_spawn.py',
    'outer_sol/PROJECT_BACKLOG.md',
    'tests/test_audit_research_baseline_regressions.py',
    'tests/test_coding_isolated_loop.py',
    'tests/test_coding_mode_surface.py',
    'tests/test_coding_worker_runner_regressions.py',
    'tests/test_coding_worker_spawn.py',
    'tests/test_current_file_web_comparison.py',
    'tests/test_orchestration_import_boundaries.py',
    'tools/quality_gate_inventory.tsv',
)

def git(*args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(['/usr/bin/git', *args], input=data, check=True, stdout=subprocess.PIPE).stdout

def prepare() -> dict[str, str]:
    head = git('rev-parse', 'HEAD').decode().strip()
    if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY or head != os.environ.get('GITHUB_SHA'):
        raise ValueError('unexpected repository or trigger identity')
    if git('rev-parse', 'HEAD^').decode().strip() != BASE:
        raise ValueError('base advanced; reconstruct and revalidate, never force')
    git('diff', '--exit-code'); git('diff', '--cached', '--exit-code')
    packed = base64.b64decode(git('show', 'HEAD:' + TEMPORARY[2]).strip(), validate=True)
    if hashlib.sha256(packed).hexdigest() != PACKED_SHA:
        raise ValueError('packed patch digest mismatch')
    decoder = lzma.LZMADecompressor()
    patch = decoder.decompress(packed, max_length=131073)
    if not decoder.eof or decoder.unused_data or len(patch) != 82004 or hashlib.sha256(patch).hexdigest() != PATCH_SHA:
        raise ValueError('patch size, digest or framing mismatch')
    git('apply', '--unidiff-zero', '--index', '--check', '-', data=patch)
    git('apply', '--unidiff-zero', '--index', '-', data=patch)
    git('rm', '--', *TEMPORARY)
    changed = set(git('diff', '--cached', '--name-only', '-z').decode().split('\0')) - {''}
    if changed != set(PATHS) | set(TEMPORARY):
        raise ValueError('unexpected changed path')
    actual = git('write-tree').decode().strip()
    if actual != TREE:
        raise ValueError('candidate tree mismatch')
    receipt = {'base_commit': BASE, 'workflow_commit': head, 'tree_sha': actual, 'patch_sha256': PATCH_SHA}
    print(json.dumps(receipt, sort_keys=True))
    return receipt

def upload(receipt: dict[str, str]) -> None:
    token = os.environ.get('GH_TREE_TOKEN')
    if not token:
        raise ValueError('tree-upload credential missing')
    entries = []
    for path in PATHS:
        row = git('ls-files', '-s', '--', path).decode().strip().split('\t')[0].split()
        if len(row) != 3 or row[0] != '100644' or row[2] != '0':
            raise ValueError('unexpected staged mode')
        entries.append({'path': path, 'mode': row[0], 'type': 'blob', 'content': git('show', ':' + path).decode('utf-8')})
    entries.extend({'path': path, 'mode': '100644', 'type': 'blob', 'sha': None} for path in TEMPORARY)
    payload = json.dumps({'base_tree': git('rev-parse', 'HEAD^{tree}').decode().strip(), 'tree': entries}, ensure_ascii=False).encode()
    request = urllib.request.Request('https://api.github.com/repos/' + REPOSITORY + '/git/trees', data=payload, method='POST', headers={'Accept': 'application/vnd.github+json', 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.load(response)
    if result.get('sha') != TREE:
        raise ValueError('GitHub returned a different tree')
    out = Path(os.environ['RUNNER_TEMP']) / 'c1-tree'
    out.mkdir(mode=0o700, exist_ok=True)
    (out / 'tree.json').write_text(json.dumps(receipt, sort_keys=True) + '\n')
    print('Unreferenced validated tree uploaded: ' + TREE)

if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in {'prepare', 'upload'}:
        raise SystemExit('expected prepare or upload')
    receipt = prepare()
    if sys.argv[1] == 'upload':
        upload(receipt)
