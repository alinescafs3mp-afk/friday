"""A190 own bounded stock metadata, never imports/executes supplied code."""
import hashlib
import json
import os
import stat
import sys
import time

OUT = '/var/tmp/friday-astra-a181-a186-publisher-whole15-all134-all69-all9-connected-source-closure-a190-g1'
INPUT = '/home/jericho/.jericho/grok-takeover/ASTRA-E4-A190-INPUT-20261002.json'
LIMIT = 256 * 1024 * 1024
ledger = []
cache = {}

def identity(s):
    return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]

def read(path, expected=None):
    if path in cache:
        raw,pin=cache[path]
    else:
        before=os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size>32*1024*1024:
            raise ValueError('regular bounded file required: '+path)
        if sum(row['bytes'] for row in ledger)+before.st_size>LIMIT-96*1024*1024:
            raise ValueError('read budget')
        with open(path,'rb') as f:raw=f.read(before.st_size+1)
        after=os.lstat(path)
        if identity(before)!=identity(after) or len(raw)!=before.st_size:raise ValueError('read drift: '+path)
        pin={'path':path,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
             'identity9_decimal_strings':identity(after),'stable9':True}
        cache[path]=(raw,pin);ledger.append(pin)
    if expected is not None:
        for key in ('bytes','sha256','identity9_decimal_strings'):
            if key in expected and expected[key]!=pin[key]:raise ValueError('pin mismatch '+key+': '+path)
    return raw,pin

def load(path,pin=None):return json.loads(read(path,pin)[0])

def pins(value):
    if isinstance(value,dict):
        if isinstance(value.get('path'),str) and 'sha256' in value and 'bytes' in value:
            yield value
        else:
            for v in value.values():yield from pins(v)
    elif isinstance(value,list):
        for v in value:yield from pins(v)

def save(name,value):
    path=os.path.join(OUT,name)
    raw=(json.dumps(value,sort_keys=True,ensure_ascii=True,indent=2,allow_nan=False)+'\n').encode('ascii')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw)

def main():
    start=time.monotonic_ns()
    current=load(INPUT)
    if hashlib.sha256(cache[INPUT][0]).hexdigest()!='6e5cfaccf0f0d3f66bedd7e0104b2cf5951ad34e331731c5e68a3eabc93ba77a':raise ValueError('input SHA')
    a181=load(current['original_A181_input_pin']['path'],current['original_A181_input_pin'])
    a173=load(a181['original_exact_A173_input']['path'],a181['original_exact_A173_input'])
    a177=load(a181['original_exact_A177_input']['path'],a181['original_exact_A177_input'])
    a170=load(a173['original_full_A170_input']['path'],a173['original_full_A170_input'])
    lab859=load(a170['original_author_input']['path'],a170['original_author_input'])
    sol053=load(a170['original_full_fence']['path'],a170['original_full_fence'])
    sol052=load(sol053['Source']['original_SOL052_input'])
    if cache[sol053['Source']['original_SOL052_input']][1]['sha256']!=sol053['Source']['original_SOL052_input_SHA256']:raise ValueError('original SOL052 SHA')
    documents=[current,a181,a173,a177,a170,lab859,sol053,sol052]
    # Read every complete pinned current134 and every complete A186 review leaf.
    # Historical INPUTs are fully parsed with exact integer and absence semantics.
    for doc in documents:
        for pin in pins(doc):read(pin['path'],pin)
    # Full original SOL052/A138 normative review and Source manifest expose the
    # original58/22 paths; only exact declared metadata references are followed.
    extra=[sol053['independent_review']['full_findings'],sol053['independent_review']['all58delta'],
           sol053['independent_review']['all20schemas69join'],sol053['independent_review']['costs']]
    for path in extra:read(path)
    for pin in list(pins(sol053)):
        if pin['path'].endswith('/manifest.json'):
            manifest=load(pin['path'],pin)
            for row in pins(manifest):
                path=row['path']
                if not os.path.isabs(path):path=os.path.join(os.path.dirname(pin['path']),path)
                read(path,row)
    a138=load(sol052['original_materials']['A138_full_input'])
    if cache[sol052['original_materials']['A138_full_input']][1]['sha256']!=sol052['original_materials']['sha256']:raise ValueError('A138 input SHA')
    for pin in pins(a138):read(pin['path'],pin)
    save('read-ledger-initial.json',{'schema':'friday.a190.full-raw-stock-metadata-read.v1','method':'lstat/read-all-bytes/hash/lstat; stock integer JSON; no supplied import/AST/compiler/exec',
         'rows':ledger,'distinct_read_bytes':sum(r['bytes'] for r in ledger),
         'failed_initial_administrative_pass_read_upper_bytes':64*1024*1024,
         'manual_text_and_repeated_read_reserve_bytes':32*1024*1024,
         'own_administrative_correction':'relative manifest path resolved against exact manifest parent; no supplied Source execution',
         'elapsed_metadata_ns':time.monotonic_ns()-start,'whole_RAM_IO_CPU':'UNKNOWN_NOT_ZERO_NOT_PASS'})
    print(json.dumps({'files':len(ledger),'bytes':sum(r['bytes'] for r in ledger),'complete_current134':len(current['current134']),
                      'A186_full_pins':len(current['A186_review_pins']),'original_SOL052_keys':list(sol052),'status':'AUTHENTICATED_RAW_ONLY'}))

if __name__=='__main__':main()
