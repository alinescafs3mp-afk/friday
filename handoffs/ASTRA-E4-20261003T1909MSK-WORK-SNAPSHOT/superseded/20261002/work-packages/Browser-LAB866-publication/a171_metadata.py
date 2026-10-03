"""Author-owned bounded metadata only. Never imports or parses supplied Source."""
import os
import stat
import json
import hashlib
import sys
import time

ROOT = '/var/tmp/friday-astra-lab858-sol057-whole209-all216-actual-source-implementation-a171-g1'
INPUT = '/home/jericho/.jericho/grok-takeover/ASTRA-E4-A171-INPUT-20261002.json'
LIMIT = 268435456
ledger = []
cached = {}
physical = 0

def ident(s):
    return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]

def read(path, pin=None):
    global physical
    if path in cached:
        raw, got = cached[path]
        if pin is not None:
            check(pin, got)
        return raw, got
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 16777216:
        raise ValueError('bounded regular nlink1 read required: '+path)
    if physical + before.st_size > LIMIT:
        raise ValueError('read cap')
    fd = os.open(path, os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        held = os.fstat(fd)
        chunks = []
        while True:
            part = os.read(fd, min(1048576, before.st_size + 1 - sum(map(len,chunks))))
            if not part: break
            chunks.append(part)
            if sum(map(len,chunks)) > before.st_size: raise ValueError('grew: '+path)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    named_after = os.stat(path,follow_symlinks=False)
    raw = b''.join(chunks)
    physical += len(raw)
    if not (ident(before)==ident(held)==ident(after)==ident(named_after)) or len(raw)!=before.st_size:
        raise ValueError('changed read: '+path)
    got={'path':path,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
         'identity9_decimal_strings':ident(before),'stable9':True}
    if pin is not None: check(pin,got)
    ledger.append(got)
    cached[path]=(raw,got)
    return raw,got

def check(want,got):
    for key in ('path','bytes','sha256','identity9_decimal_strings'):
        if key in want and want[key]!=got[key]: raise ValueError('pin mismatch '+key+': '+got['path'])

def load(path,pin=None):
    return json.loads(read(path,pin)[0],parse_int=int,parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))

def pins(obj):
    if type(obj) is dict:
        if {'path','sha256','bytes'} <= set(obj): yield obj
        for value in obj.values(): yield from pins(value)
    elif type(obj) is list:
        for value in obj: yield from pins(value)

def write(name,data):
    path=ROOT+'/'+name
    if os.path.commonpath((ROOT,path))!=ROOT: raise ValueError('output escape')
    raw=(json.dumps(data,ensure_ascii=True,indent=2)+'\n').encode('ascii')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        offset=0
        while offset<len(raw): offset+=os.write(fd,raw[offset:])
        os.fsync(fd)
    finally: os.close(fd)

def intake():
    input_data=load(INPUT,{'sha256':'e830b090eaf26001e000842f3ee2d8c1a8e407cd1779812d74f046a93fc2eaaf','bytes':181649})
    for pin in pins(input_data): read(pin['path'],pin)
    sol=load(input_data['original_full_SOL057_input']['path'])
    lab=load(input_data['original_author_input']['path'])
    for data in (sol,lab):
        for pin in pins(data): read(pin['path'],pin)
    current=load(input_data['Source']['A158_original209']['full_current_manifest'])
    for member in current['members']: read(member['pin']['path'],member['pin'])
    for name in ('REVIEW.md',):
        path='/var/tmp/friday-astra-browser-a153-whole216-all3-independent-source-review-a156-g1/'+name
        read(path)
    # Every whole-review file is explicitly pinned in A171; data is read, never evaluated.
    review='/var/tmp/friday-sol057-lab858-whole209-all216-independent-source-review/'
    rows=load(review+'whole216-independent-matrix.json')['rows']
    safety=load(review+'historical29-safety-review.json')
    write('INTAKE-READ-LEDGER.json',{'schema':'friday.a171.read-metadata.v1','physical_read_bytes':physical,
        'unique_files':len(ledger),'entries':ledger,'raw_source_interpreted':False,
        'accounting_scope':'this trusted metadata invocation; preceding shell/tool reads reserved separately'})
    write('ORIGINAL216-EXPECTATIONS.json',{'rows':[{'position':r['position'],'id':r['id'],
        'expected':r['original_expected'],'binding':r['original_binding'],'prior_review':r['review_status']} for r in rows],
        'exact_original_order':True,'count':len(rows),'runtime':'NOT_RUN'})
    write('ORIGINAL29-SAFETY-REQUIREMENTS.json',safety)
    print(json.dumps({'files':len(ledger),'read_bytes':physical,'rows':len(rows),'current':len(current['members']),
        'full_inputs_sha_verified':True,'review_read':True,'Source_evaluated':False}))

if __name__=='__main__':
    if sys.argv[1:] == ['intake']: intake()
    else: raise SystemExit('metadata command only')
