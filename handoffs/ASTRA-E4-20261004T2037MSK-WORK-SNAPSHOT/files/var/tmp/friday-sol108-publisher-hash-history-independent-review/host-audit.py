"""Host-only JSON/SHA/stat/literal-LF audit. Never parses or executes Source."""
import difflib
import hashlib
import json
import os
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

READ_BYTES = 0

def identity(s):
    return [str(x) for x in (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                            s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)]

def read_pin(p, expected=None):
    global READ_BYTES
    before = os.lstat(p)
    assert stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
    assert before.st_mode & 0o077 == 0, (p, "not private")
    with open(p, "rb") as f:
        assert identity(os.fstat(f.fileno())) == identity(before)
        b = f.read()
        assert identity(os.fstat(f.fileno())) == identity(before)
    assert identity(os.lstat(p)) == identity(before)
    READ_BYTES += len(b)
    pin = dict(path=p, bytes=len(b), sha256=hashlib.sha256(b).hexdigest(),
               identity9_decimal_strings=identity(before), stable9=True)
    if expected:
        for k in ("bytes", "sha256", "identity9_decimal_strings"):
            if k in expected:
                assert pin[k] == expected[k], (p, k, pin[k], expected[k])
    return b, pin

input_path = "/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL108-INPUT-20261004.json"
ib, ip = read_pin(input_path, {"sha256": "3b8a67649940ac46e5a56d72b0f3a724855cf40e966af2bb0068962a76a2ebbf"})
d = json.loads(ib)
cb, cp = read_pin(d["snapshot"]["path"], d["snapshot"])
bb, bp = read_pin(d["baseline"]["path"], d["baseline"])
candidate, baseline = json.loads(cb), json.loads(bb)
baseline_root = baseline["root"]
basepins = {x["path"][len(baseline_root)+1:]: x for x in baseline["files"]}
changes, same, allpins = [], [], []
assert len(d["Source57"]) == len({x["name"] for x in d["Source57"]}) == 57
for x in d["Source57"]:
    name = x["name"]
    new, np = read_pin(x["path"], x)
    old, op = read_pin(basepins[name]["path"], basepins[name])
    assert new.endswith(b"\n") and old.endswith(b"\n")
    row = dict(name=name, candidate=np, baseline=op,
               candidate_LF_count=new.count(b"\n"), baseline_LF_count=old.count(b"\n"))
    allpins.append(row)
    if new == old:
        same.append(name)
        continue
    ol, nl = old.splitlines(keepends=True), new.splitlines(keepends=True)
    # These files use literal LF; no Source syntax interpretation.
    assert b"".join(ol) == old and b"".join(nl) == new
    ops = difflib.SequenceMatcher(None, ol, nl, autojunk=False).get_opcodes()
    forward, inverse, hunks = [], [], []
    for tag, a, z, c, e in ops:
        if tag == "equal":
            assert ol[a:z] == nl[c:e]
            forward.extend(ol[a:z]); inverse.extend(nl[c:e])
        else:
            forward.extend(nl[c:e]); inverse.extend(ol[a:z])
            hunks.append(dict(tag=tag, old_LF_start=a+1, old_LF_end=z,
                              new_LF_start=c+1, new_LF_end=e,
                              old_text=b"".join(ol[a:z]).decode(),
                              new_text=b"".join(nl[c:e]).decode()))
    assert b"".join(forward) == new and b"".join(inverse) == old
    changes.append(dict(name=name, hunks=hunks, forward_exact=True, inverse_exact=True))
refs=[]
for x in d["references"]:
    b,p=read_pin(x["path"],x); refs.append(dict(pin=p,value=json.loads(b)))
report = dict(schema="friday.sol108.host-literal-audit.v1", input=ip,
              candidate_manifest=cp, baseline_manifest=bp,
              verified_msk=datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
              source_pins=allpins, changed=changes, unchanged=same, references=refs,
              counted_host_read_bytes=READ_BYTES, source_execution=0)
print(json.dumps(report, separators=(",", ":")))
