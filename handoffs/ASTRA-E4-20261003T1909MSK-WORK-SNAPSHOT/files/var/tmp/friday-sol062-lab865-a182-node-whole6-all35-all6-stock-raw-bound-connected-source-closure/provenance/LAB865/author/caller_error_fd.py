"""Owned stock literal editing; no AST/supplied-code evaluation."""
from pathlib import Path
import os
root=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
p=root/'caller.py'; s=p.read_text()
s=s.replace('/var/tmp/friday-lab863-a172-node-whole6-all35-error-raw-onceclose-source-closure',str(root))
template=(root/'author/owned_fd_text.py.txt').read_text()
err=template[template.index('ERROR_OBJECTS = []'):template.index('class AllocationFD')]
s=s.replace('class OwnedFD(int):',err+'\nFD_ALLOCATION_HISTORY = []\nSELECT_CLOSE = {"attempted": False, "state": "NOT_ATTEMPTED", "error_object": None}\n\n\nclass OwnedFD(int):',1)
s=s.replace('        value.close_failed = False','        value.close_failed = False\n        value.allocation_record = None')
a=s.index('def allocate_owned(operation, role, *args, **kwargs):'); b=s.index('def require_lease(fd, retiring=False):',a)
s=s[:a]+'''def preown_fd(role):
    record = {"role": role, "number": None, "lease": None, "state": "PLANNED",
              "attempted": False, "error_object": None, "error": None}
    FD_ALLOCATION_HISTORY.append(record)  # before any native acquisition
    return record


def publish_fd(record, number):
    record["number"], record["state"] = number, "RETURNED"
    lease = register_fd(number, record["role"])
    # register_fd/fstat failure is still owned by the prospective history above.
    record["lease"] = lease
    lease.allocation_record = record
    return lease


def allocate_owned(operation, role, *args, **kwargs):
    record = preown_fd(role)
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
    try:
        record["state"] = "ACQUIRE_UNKNOWN"
        number = operation(*args, **kwargs)
        return publish_fd(record, number)
    except BaseException as exc:
        record["error_object"] = exc
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def allocate_pipe(role):
    records = [preown_fd(role + ":" + str(i)) for i in (0, 1)]
    leases = [None, None]
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
    try:
        for record in records:
            record["state"] = "ACQUIRE_UNKNOWN"
        numbers = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        records[0]["number"], records[1]["number"] = numbers
        records[0]["state"] = records[1]["state"] = "RETURNED"
        for i, record in enumerate(records):
            leases[i] = publish_fd(record, numbers[i])
        return leases
    except BaseException as exc:
        for record in records:
            record["error_object"] = exc
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


'''+s[b:]
# Link the record before register_fd's fallible fstat, not only after return.
s=s.replace('def register_fd(fd, role):','def register_fd(fd, role, record=None):')
s=s.replace('    OWN[fd] = lease\n    lease.identity9', '    if record is not None:\n        record["lease"] = lease\n        lease.allocation_record = record\n    OWN[fd] = lease\n    lease.identity9')
s=s.replace('lease = register_fd(number, record["role"])','lease = register_fd(number, record["role"], record)')
s=s.replace('    FD_CLOSE_ATTEMPTS += 1\n    try:\n        os.close(fd)',
'''    record = fd.allocation_record
    if record is not None:
        record["attempted"], record["state"] = True, "RETIRE_UNKNOWN"
    FD_CLOSE_ATTEMPTS += 1
    try:
        os.close(fd)''')
s=s.replace('        fd.close_failed = True\n        STOP = True',
'''        fd.close_failed = True
        STOP = True
        if record is not None:
            record["error_object"] = exc
            record["error"] = exact_error(exc)''')
s=s.replace('    FD_CLOSE_SUCCESSES += 1\n', '    FD_CLOSE_SUCCESSES += 1\n    if record is not None:\n        record["state"] = "CONFIRMED_CLOSED"\n',1)
s=s.replace('{"type": type(exc).__name__, "message": str(exc)}','exact_error(exc)')
s=s.replace('{"type": type(cleanup_exc).__name__, "message": str(cleanup_exc)}','exact_error(cleanup_exc)')
s=s.replace('friday.e4.node.bounded-verifier.a172.v1','friday.e4.node.bounded-verifier.sol060.v1')
s=s.replace('friday.e4.node.actual-supervisor.a172.v1','friday.e4.node.actual-supervisor.sol060.v1')
s=s.replace('friday.e4.node.ordinary-stock-outer.a172.v1','friday.e4.node.ordinary-stock-outer.sol060.v1')
with p.open('w') as h: h.write(s)
os.chmod(p,0o600)
print('Caller prospective FD/pipe records + full error projection authored, NOT_RUN')
