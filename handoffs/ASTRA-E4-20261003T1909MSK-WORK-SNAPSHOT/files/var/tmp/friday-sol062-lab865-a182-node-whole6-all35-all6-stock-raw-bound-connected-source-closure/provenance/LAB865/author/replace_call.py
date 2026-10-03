"""Stock literal substitution only, no Source parse/import/execute."""
from pathlib import Path
import os
root=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
p=root/'verifier.py'; s=p.read_text()
a=s.index('def call(label, tool, args, inputs, writable=()):'); b=s.index('def statuses(c):',a)
replacement=(root/'author/call_body.py.txt').read_text()
s=s[:a]+replacement+'\n\n'+s[b:]
s=s.replace('OUTPUT_UNKNOWN = False','OUTPUT_UNKNOWN = False\nCALL_OWNERS = []',1)
a=s.index('except BaseException as exc:\n    verified = False\n    produced = []')
b=s.index('\ntry:\n    emit(data)',a)
s=s[:a]+'''except BaseException as exc:
    verified = False
    # Original R/fullraw/errors remain owned. No hash-only smaller replacement.
    ERROR_OBJECTS.append(exc)
    R["publication_failure"] = exact_error(exc)
    os._exit(3 if STOP else 2)  # CODE_OPEN: cannot emit full raw within32KiB
'''+s[b:]
with p.open('w') as h: h.write(s)
os.chmod(p,0o600)
print('inert call/raw complete graph materialized; runtime NOT_RUN')
