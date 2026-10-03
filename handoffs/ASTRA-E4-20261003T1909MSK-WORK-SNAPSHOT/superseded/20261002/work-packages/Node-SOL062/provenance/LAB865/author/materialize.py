"""Owned literal/mechanical Source materialization; no supplied code execution."""
import os, json, hashlib, re
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
OLD='/var/tmp/friday-lab863-a172-node-whole6-all35-error-raw-onceclose-source-closure'
body=(ROOT/'author/owned_fd_text.py.txt').read_text().split('\n',1)[1]
def put(p,s):
    with p.open('w') as h: h.write(s)
    os.chmod(p,0o600)
def section(s,first,last,new):
    a=s.index(first); b=s.index(last,a); return s[:a]+new+'\n\n'+s[b:]
for name in ('supervisor.py','verifier.py'):
    p=ROOT/name; s=p.read_text().replace(OLD,str(ROOT))
    s=s.replace('CLOSED_NUMBERS = set()\nUNCERTAIN_NUMBERS = set()\n','')
    at=s.index('\ndef due(')
    s=s[:at]+'\n\n'+body+'\n'+s[at:]
    # All lexical descriptor acquisitions after the unchanged inherited guard.
    s=s.replace('os.open(', 'acquire_owned(os.open, "open", ')
    s=s.replace('os.memfd_create(', 'acquire_owned(os.memfd_create, "memfd", ')
    s=s.replace('os.pidfd_open(', 'acquire_owned(os.pidfd_open, "pidfd", ')
    s=s.replace('fcntl.fcntl(HELD[tool]["fd"], fcntl.F_DUPFD_CLOEXEC, 16)',
                'acquire_owned(fcntl.fcntl, "launch_copy", HELD[tool]["fd"], fcntl.F_DUPFD_CLOEXEC, 16)')
    s=s.replace('LAUNCH_RESERVE["gate"][:] = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)',
                'LAUNCH_RESERVE["gate"][:] = acquire_pipe("launch_gate")')
    # Only legacy actor-body closes, not the one native firstclose in shared body.
    s=s.replace('os.close(fd)', 'close_allocation(fd, "lexical_close")')
    s=s.replace('STOP = True\n            raise', 'globals()["STOP"] = True\n            raise')
    if name=='supervisor.py':
        s=section(s,'def close_taken(fd, where):','def close_launch_reserve():',
                  'def close_taken(fd, where):\n    return close_allocation(fd, where)')
        s=s.replace('for fd in (3, 4, 5):\n            close_taken(fd, "inherited_low")',
                    'for fd in INHERITED_OWNED:\n            close_taken(fd, "inherited_low")')
        s=s.replace('prove_strict_inherited_before_ctypes()\n',
                    'prove_strict_inherited_before_ctypes()\n    global INHERITED_OWNED\n    INHERITED_OWNED = [adopt_exact_fd(fd, "inherited_low") for fd in (3, 4, 5)]\n')
        s=s.replace('RUNTIME = {"fd": fd,', 'RUNTIME = {"fd": adopt_exact_fd(fd, "runtime"),')
        s=s.replace('RUNTIME = None','INHERITED_OWNED = []\nRUNTIME = None')
        # Preowned transport shell before the syscall rather than append after.
        s=s.replace('        fd = acquire_owned(os.memfd_create, "memfd", "A048-supervised-" + label, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)\n        TRANSPORT.append({"label": label, "fd": fd, "offset": 0, "data": bytearray()})',
                    '        item = {"label": label, "fd": None, "offset": 0, "data": bytearray(), "retain_error_object": None}\n        TRANSPORT.append(item)\n        item["fd"] = acquire_owned(os.memfd_create, "memfd", "A048-supervised-" + label, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)')
    else:
        s=section(s,'def close_once(fd, where):','def close_own(fd):',
                  'def close_once(fd, where):\n    return close_allocation(fd, where)')
        s=section(s,'def close_own(fd):','def retain_stream(',
                  'def close_own(fd):\n    require(isinstance(fd, AllocationFD), "unregistered own close")\n    if fd.allocation["attempted_once"]:\n        return close_once(fd, "own")\n    require(fd in OWN, "close of an unowned allocation refused")\n    OWN.remove(fd)\n    return close_once(fd, "own")')
        s=s.replace('for fd in (3, 4, 5, 6):\n        close_once(fd, "inherited_role")',
                    'for fd in INHERITED_OWNED:\n        close_once(fd, "inherited_role")')
        s=s.replace('VERIFIER_RUNTIME = {"fd": fd,', 'VERIFIER_RUNTIME = {"fd": adopt_exact_fd(fd, "runtime"),')
        # Guard already verified the exact inherited set before general imports.
        s=s.replace('def main():\n', 'INHERITED_OWNED = [adopt_exact_fd(fd, "inherited_role") for fd in (3, 4, 5, 6)]\n\n\ndef main():\n')
    # Complete causal projection, not type/message-only.
    s=s.replace('{"type": type(exc).__name__, "message": str(exc)}','exact_error(exc)')
    # Cleanup of remaining known exact allocations survives another close failure.
    marker='\ntry:\n    due(0)' if name=='supervisor.py' else '\ntry:\n    due(True)\n    finalize_read_accounting()'
    assert marker in s
    tail='\ntry:\n    finish_allocations()\nexcept BaseException as exc:\n    STOP = True\n    '+('qualified' if name=='supervisor.py' else 'verified')+' = False\n    R["allocation_cleanup_failure"] = exact_error(exc)\n'
    s=s.replace(marker,tail+marker,1)
    # Record schemas change for ALL actual producers/consumers separately.
    s=s.replace('friday.e4.node.bounded-verifier.a172.v1','friday.e4.node.bounded-verifier.sol060.v1')
    s=s.replace('friday.e4.node.actual-supervisor.a172.v1','friday.e4.node.actual-supervisor.sol060.v1')
    put(p,s)
print(json.dumps({'mechanical_materialization':'actor allocation body/routing/close/schema copied as inert text','executed_source':False}))
