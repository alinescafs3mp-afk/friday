"""Own bounded lexical connected custody edits; never imports/executes actors."""
import os,json
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol062-lab865-a182-node-whole6-all35-all6-stock-raw-bound-connected-source-closure')
def rep(t,a,b,n=1):
    assert t.count(a)==n,(a[:100],t.count(a),n)
    return t.replace(a,b)
texts={name:(ROOT/name).read_text() for name in ('caller.py','supervisor.py','verifier.py')}
v=texts['verifier.py']
v=rep(v,'    secondary_objects = []\n','''    # One reap + every raw cell + every stream close + every public-cell copy.
    cells = stream_cells + write_cells
    secondary_capacity = 2 + 2 * len(cells) + len(streams)
    secondary_objects = [None] * secondary_capacity
    secondary_public = [None] * secondary_capacity
    secondary_used = 0
    def note_secondary(exc):
        nonlocal secondary_used
        # Store native object first, before the private marker/public copy.
        require(secondary_used < secondary_capacity, "finite call secondary slot overflow")
        secondary_objects[secondary_used] = exc
        secondary_public[secondary_used] = deferred_error(exc)
        secondary_used += 1
''')
v=rep(v,'                secondary_objects.append(exc)\n                item["secondary_errors"].append(deferred_error(exc))','                note_secondary(exc)',3)
v=rep(v,'        item["elapsed_seconds"] = (time.monotonic_ns() - start) / 1e9\n','')
v=rep(v,'        for cell in stream_cells + write_cells:\n','        for cell in cells:\n')
old='''        for name, cell in zip(("stdout", "stderr", "status"), stream_cells):
            item[name] = published_cell(cell)
        item["written_data"] = [published_cell(cell) for cell in write_cells]
        cells = stream_cells + write_cells
        item["capture_complete"] = all(c["retained"] and not c["partial"] for c in cells)
        item["output_charge_unknown"] = any(c["output_charge_bytes"] is None for c in cells)
'''
v=rep(v,old,'')
anchor='''    if primary is not None:
        raise primary.with_traceback(primary_tb)'''
v=rep(v,anchor,'''        # Fallible public-cell copying happens only AFTER all stream firstcloses.
        for name, cell in zip(("stdout", "stderr", "status"), stream_cells):
            try:
                item[name] = published_cell(cell)
            except BaseException as exc:
                note_secondary(exc)
        try:
            item["written_data"] = [published_cell(cell) for cell in write_cells]
            item["capture_complete"] = all(c["retained"] and not c["partial"] for c in cells)
            item["output_charge_unknown"] = any(c["output_charge_bytes"] is None for c in cells)
            item["elapsed_seconds"] = (time.monotonic_ns() - start) / 1e9
        except BaseException as exc:
            note_secondary(exc)
        owner["secondary_used"] = secondary_used
        item["secondary_errors"] = secondary_public[:secondary_used]
    if primary is not None:
        raise primary.with_traceback(primary_tb)''')
v=rep(v,'    if secondary_objects:\n        raise secondary_objects[0]','    if secondary_used:\n        raise secondary_objects[0]')
v=rep(v,'''            owner["parts"].append(owner["pending"])
            pos += len(owner["pending"])
            owner["pending"] = None''','''            # Compute allocating arithmetic BEFORE commit. Mask only the exact
            # append/offset/pending commit, not reads, deadlines or whole cleanup.
            next_pos = pos + len(owner["pending"])
            saved = signal.pthread_sigmask(signal.SIG_BLOCK, FD_SIGNALS)
            try:
                owner["parts"].append(owner["pending"])
                pos = next_pos
                owner["pending"] = None
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, saved)''')
texts['verifier.py']=v
s=texts['supervisor.py']
commit='''            next_offset = item["offset"] + len(chunk)
            saved = signal.pthread_sigmask(signal.SIG_BLOCK, REGISTRATION_SIGNALS)
            try:
                item["data"].extend(chunk)
                item["offset"] = next_offset
                item["pending_chunk"] = None
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, saved)'''
s=rep(s,'''            item["data"].extend(chunk)
            item["offset"] += len(chunk)
            item["pending_chunk"] = None''',commit,2)
# Keep the original full current one-cell body; isolate each cell's fallible work.
a=s.index('def retain_produced_transport():'); b=s.index('\n\ndef register_parent(',a)
old=s[a:b]; lines=old.splitlines(keepends=True)
for_pos=old.index('    for item in TRANSPORT:\n')
body=old[for_pos+len('    for item in TRANSPORT:\n'):]
indented=''.join('    '+line if line.strip() else line for line in body.splitlines(keepends=True))
new=old[:for_pos]+'''    for item in TRANSPORT:
        # Never overwrite an uncommitted returned chunk or retry a failed read.
        if item.get("pending_chunk") is not None or item.get("retain_error_object") is not None:
            item["partial"] = True
            continue
        try:
'''+indented+'''        except BaseException as exc:
            item["retain_error_object"] = exc
            item["partial"] = True
            item["retain_error"] = deferred_error(exc)
            # The next independent cell is still attempted before any FD close.
'''
s=s[:a]+new+s[b:]; texts['supervisor.py']=s
c=texts['caller.py']
c=rep(c,'''                item["data"].extend(data)
                item["pending_chunk"] = None''','''                saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
                try:
                    item["data"].extend(data)
                    item["pending_chunk"] = None
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, saved)''')
# Save the first capture error before making even its private reference.
c=rep(c,'''                launch_owner["capture_errors"][index] = exc
                receipt[item["label"] + "_capture_error"] = deferred_error(exc)
                first_capture_error = first_capture_error or exc''','''                launch_owner["capture_errors"][index] = exc
                first_capture_error = first_capture_error or exc
                receipt[item["label"] + "_capture_error"] = deferred_error(exc)''')
texts['caller.py']=c
# Check ALL literal matches before writing any changed byte.
for name,text in texts.items():
    fd=os.open(ROOT/name,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW)
    with os.fdopen(fd,'w') as f: f.write(text)
print(json.dumps({'verifier_finite_secondary_slots':True,'projection_after_raw_firstclose':True,'transport_pending_commits_masked':True,'source_exec':False}))
