"""A189 owned TEXT/JSON/stat/SHA utility. Never imports supplied Source."""
import hashlib,json,os,stat,time,re
from pathlib import Path

ROOT=Path('/var/tmp/friday-astra-a180-a185-scanner-whole17-all52-all2-connected-native-raw-source-closure-a189-g1')
INPUT=Path('/home/jericho/.jericho/grok-takeover/ASTRA-E4-A189-INPUT-20261002.json')
INPUT_SHA='5508c60ea1482ee7f1dc31d8ae7c629e3571a696973960b2232b89f21b645ce0'
read_bytes=0
def identity(s):
    return [str(getattr(s,k)) for k in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')]
def read(path):
    global read_bytes
    path=Path(path);before=path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:raise ValueError('regular nlink1: '+str(path))
    if read_bytes+before.st_size>230000000:raise ValueError('metadata read allowance')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        opened=os.fstat(fd);pieces=[]
        while True:
            part=os.read(fd,65536)
            if not part:break
            read_bytes+=len(part);pieces.append(part)
        raw=b''.join(pieces);after=os.fstat(fd)
    finally:os.close(fd)
    named=path.lstat();ids=[identity(s) for s in (before,opened,after,named)]
    if not all(i==ids[0] for i in ids):raise ValueError('identity drift: '+str(path))
    return raw,{'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'identity9_decimal_strings':ids[0],'stable9':True}
def pairs(items):
    out={}
    for k,v in items:
        if k in out:raise ValueError('duplicate JSON key '+k)
        out[k]=v
    return out
def load(raw):
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
def write(name,value):
    target=ROOT/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    raw=(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode()
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:os.write(fd,raw)
    finally:os.close(fd)
def pins(value):
    if isinstance(value,dict):
        if {'path','sha256','bytes','identity9_decimal_strings'}<=value.keys():yield value
        for v in value.values():yield from pins(v)
    elif isinstance(value,list):
        for v in value:yield from pins(v)
def authenticate_and_materialize():
    raw,pin=read(INPUT)
    if pin['sha256']!=INPUT_SHA:raise ValueError('INPUT SHA')
    inp=load(raw);seen={str(INPUT):pin};queue=[inp];audits=[pin];documents={str(INPUT):inp}
    while queue:
        document=queue.pop(0)
        for expected in pins(document):
            name=expected['path']
            if name in seen:
                actual=seen[name]
            else:
                if Path(name).suffix.lower() not in ('.json','.py','.c','.md','.txt','.diff'):raise ValueError('non-TEXT linked pin '+name)
                body,actual=read(name);seen[name]=actual;audits.append(actual)
                if '-INPUT-' in Path(name).name and name.endswith('.json'):
                    child=load(body);documents[name]=child;queue.append(child)
            for k in ('bytes','sha256','identity9_decimal_strings'):
                if expected[k]!=actual[k]:raise ValueError('pin mismatch '+name+' '+k)
    inventory=[]
    for entry in inp['current52']:
        body,p=read(entry['path']);text=body.decode('utf-8')
        target=ROOT/entry['file'];target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        # Mechanical inert TEXT copy only. No candidate import/AST/compile/run.
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:os.write(fd,body)
        finally:os.close(fd)
        inventory.append({'file':entry['file'],'input':p,'complete_TEXT_read':True,
          'definitions':[{'line':i,'text':line.strip()} for i,line in enumerate(text.splitlines(),1) if re.match(r'\s*(def |class |static .*\(|int friday_)',line)],
          'catches':[{'line':i,'text':line.strip()} for i,line in enumerate(text.splitlines(),1) if re.match(r'\s*(except|finally)',line)]})
    write('input-validation.json',{'input':pin,'authenticated_unique_pins':audits,'fully_parsed_INPUT_paths':list(documents),'stock_exact_integer_JSON':True,'explicit_read_bytes':read_bytes,'prior_shell_read_bytes':'UNKNOWN_BOUNDED_BELOW_5MiB','supplied_execution':'NOT_RUN'})
    write('original-input-directions.json',{name:{k:v for k,v in x.items() if k in ('scope','SCOPE','mandatory_edges','original_mandatory_edges','directions','resources','effects','DONE_WHEN')} for name,x in documents.items()})
    write('current52-TEXT-inventory.json',inventory)
    print(json.dumps({'unique_pins':len(audits),'input_documents':len(documents),'current52':len(inventory),'explicit_read_bytes':read_bytes}))
if __name__=='__main__':authenticate_and_materialize()
