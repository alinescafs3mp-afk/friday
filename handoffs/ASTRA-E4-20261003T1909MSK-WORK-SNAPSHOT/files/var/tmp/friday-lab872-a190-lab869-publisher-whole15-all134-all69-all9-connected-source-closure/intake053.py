"""SOL053 trusted stock metadata only. Never load, parse or execute Source."""
import os, stat, json, hashlib, resource, sys
ROOT='/var/tmp/friday-sol053-a138-sol052-whole15-69-performing-publisher-connected-source-repair'
REVIEW='/var/tmp/friday-sol052-a138-whole15-performing-publisher-root-source-review'
SOURCE='/var/tmp/friday-astra-publisher-a135-whole-performing-root-actor-consumer-source-closure-a138-g1'
CONSUMER='/var/tmp/friday-astra-publisher-a122-whole-connected-source-closure-a128-g1'
resource.setrlimit(resource.RLIMIT_AS,(8589934592,)*2)
resource.setrlimit(resource.RLIMIT_CPU,(120,)*2)
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
resource.setrlimit(resource.RLIMIT_NOFILE,(256,)*2)
os.umask(0o077)
ledger=[]
def nine(s):return list(map(str,(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)))
def read(path,pin=None):
    a=os.lstat(path);fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        b=os.fstat(fd)
        assert nine(a)==nine(b) and stat.S_ISREG(b.st_mode) and b.st_nlink==1 and stat.S_IMODE(b.st_mode)==0o600 and b.st_uid==1000
        assert b.st_size<=16000000
        raw=bytearray()
        while len(raw)<b.st_size:
            part=os.read(fd,min(65536,b.st_size-len(raw)));assert part;raw.extend(part)
        assert not os.read(fd,1) and nine(os.fstat(fd))==nine(a)==nine(os.lstat(path))
        raw=bytes(raw);h=hashlib.sha256(raw).hexdigest()
        p={'path':path,'bytes':len(raw),'sha256':h,'identity9_decimal_strings':nine(a),'stable9':True}
        if pin:
            assert p['bytes']==pin['bytes'] and h==pin['sha256']
            assert nine(a)==pin['identity9_decimal_strings']
        ledger.append({'path':path,'bytes':len(raw)})
        assert sum(r['bytes'] for r in ledger)<134217728
        return raw,p
    finally:os.close(fd)
def write(name,value):
    raw=(json.dumps(value,sort_keys=True,ensure_ascii=True,separators=(',',':'))+'\n').encode()
    fd=os.open(ROOT+'/'+name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        at=0
        while at<len(raw):at+=os.write(fd,raw[at:])
        os.fsync(fd)
    finally:os.close(fd)
if __name__=='__main__':
    inv=json.loads(read(REVIEW+'/verified-input-inventory.json')[0])
    bodies={};verified=[]
    for pin in inv['pins']:
        raw,p=read(pin['path'],pin);verified.append(p);bodies[pin['path']]=raw
    for row in inv['directories']:
        s=os.lstat(row['path']);assert nine(s)==row['identity9_decimal_strings'] and stat.S_ISDIR(s.st_mode) and stat.S_IMODE(s.st_mode)==0o700
    for base,key in ((SOURCE,'source_pathset26'),(CONSUMER,'consumer_pathset70')):
        actual=sorted(os.path.relpath(os.path.join(d,f),base) for d,ds,fs in os.walk(base,followlinks=False) for f in fs)
        assert actual==sorted(inv[key])
    inp_path='/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL053-INPUT-20261002.json'
    raw,p=read(inp_path);assert p['sha256']=='000340226c12c969e2fbbff7547a53c5181af4b1fbe47822fbba82e38beaca9a';inp=json.loads(raw)
    for section in ('Source','independent_review'):
        for key in ('terminal','manifest','seal','whole_review'):
            if key in inp[section]:read(inp[section][key]['path'],inp[section][key])
    cp_path='/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL053-PRE-TASK-COMPACT-20261002.json'
    cpraw,cppin=read(cp_path);cp=json.loads(cpraw)
    assert cp['assignment']==inp['assignment'] and cp['generation']==1 and cp['same_tui'] and cp['completion']['status']=='completed' and cp['completion']['item_type']=='contextCompaction' and cp['completion']['error'] is None and cp['after']['queue_count']==0
    if sys.argv[1]=='verify':
        write('received.json',{'input_pin':p,'assignment':inp['assignment'],'generation':1,'accepted_msk':'2026-10-02T06:40:38+03:00','pre_task_compact':cp,'compact_pin':cppin,'input':inp,'verified_input_files':verified,'directories':inv['directories'],'source_pathset26':inv['source_pathset26'],'consumer_pathset70':inv['consumer_pathset70'],'candidate_execution':0,'children':0,'GO':False})
        write('intake-read-ledger.json',{'events':ledger,'actual_read_bytes':sum(r['bytes'] for r in ledger),'implicit_IO':'UNKNOWN_NOT_ZERO_NOT_PROVEN','aggregate_RAM':'UNKNOWN_NOT_ZERO_NOT_PROVEN'})
        print(json.dumps({'verified':len(verified),'pathsets':'26+70 exact','read_bytes':sum(r['bytes'] for r in ledger)}))
    elif sys.argv[1]=='patch':
        print('*** Begin Patch')
        for base,paths,prefix in ((SOURCE,[r for r in inv['source_pathset26'] if r.startswith(('source/','schemas/'))],''),):
            for rel in paths:
                raw=bodies[base+'/'+rel];assert raw.endswith(b'\n')
                print('*** Add File: '+ROOT+'/'+prefix+rel)
                for line in raw.decode('utf-8').splitlines():print('+'+line)
        print('*** End Patch')
    else:raise RuntimeError('mode')
