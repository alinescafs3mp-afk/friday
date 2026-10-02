"""Own bounded stock sealing only; no supplied Source execution."""
import os,json,stat,time,hashlib,resource,datetime
from pathlib import Path
ROOT=Path('/var/tmp/friday-sol060-lab863-node-whole6-all35-all6-connected-error-raw-source-closure')
TERMINAL=Path('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL060-LAB863-NODE-WHOLE6-ALL35-ALL6-CONNECTED-ERROR-RAW-SOURCE-CLOSURE-RESULT.json')
ASSIGNMENT='ASTRA-E4-SOL060-LAB863-NODE-WHOLE6-ALL35-ALL6-CONNECTED-ERROR-RAW-SOURCE-CLOSURE'
ACCEPTED=1790934284680479643
READS=0
AUDITS=[]
def nine(s):return [str(x) for x in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read(p):
    global READS
    s=p.lstat();assert stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==0o600
    assert s.st_size<16777216
    b=p.read_bytes();READS+=len(b);assert nine(s)==nine(p.lstat())
    return b,{'path':str(p),'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'identity9_decimal_strings':nine(s)}
def write(p,x):
    assert not p.exists()
    b=(json.dumps(x,indent=2,ensure_ascii=True)+'\n').encode()
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as h:h.write(b);h.flush();os.fsync(h.fileno())
    return read(p)[1]
def listing():
    files=[];dirs=[]
    for d,ds,fs in os.walk(ROOT,followlinks=False):
        p=Path(d);st=p.lstat();assert stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid() and stat.S_IMODE(st.st_mode)==0o700
        dirs.append(str(p.relative_to(ROOT)))
        for n in ds:
            st=(p/n).lstat();assert stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid()
        for n in fs:
            p2=p/n;st=p2.lstat();assert stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid() and st.st_nlink==1
            files.append(p2)
    return sorted(files),sorted(dirs)
assert not TERMINAL.exists()
assert time.time_ns()-ACCEPTED < 6000*10**9  # freeze before reserved last600
assert ROOT.is_dir() and not ROOT.is_symlink() and ROOT.stat().st_uid==os.getuid()
# Normalize only validated NEW owned single-link output files/directories.
for d,ds,fs in os.walk(ROOT,followlinks=False):
    p=Path(d);st=p.lstat();assert stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid();os.chmod(p,0o700)
    for n in ds:
        st=(p/n).lstat();assert stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid()
    for n in fs:
        p2=p/n;st=p2.lstat();assert stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid() and st.st_nlink==1
        if stat.S_IMODE(st.st_mode)!=0o600:os.chmod(p2,0o600)
src={}
for n in ('caller.py','supervisor.py','verifier.py','launch-contract.json','expectations.json','ordinary-surplus.txt'):
    src[n]=read(ROOT/n)[1]
joins=json.loads(read(ROOT/'author/current-source-joins.json')[0])
assert joins['pins']==src,'Source snapshot drifted after metadata'
write(ROOT/'author/source-freeze.json',{'schema':'friday.sol060.final-source-freeze.v1','at_epoch_ns':str(time.time_ns()),'pins':src,'SourceReady':False,'Root_admission':False,'runtime':'NOT_RUN','GO':False})
files,dirs=listing();size=sum(p.stat().st_size for p in files)
assert size<12*1024*1024
write(ROOT/'author/resource-ledger.json',{'schema':'friday.sol060.author-resource-ledger.v1','accepted_epoch_ns':str(ACCEPTED),'author_wall_limit_seconds':6600,'seal_reserve_seconds':600,'freeze_before_end600':True,'read_cap':268435456,'output_cap':16777216,'RAM_cap':8589934592,'intake_exact_reads':4794480,'final_metadata_exact_reads':900852,'stock_seal_reads_before_leaf_acquisition':READS,'tool_transport_literal_helpers_and_uninstrumented_read_reserve':100663296,'all_final_leaf_and_seal_audits_reserved_bytes':3*(16*1024*1024),'known_plus_reserved_upper':4794480+900852+READS+100663296+3*(16*1024*1024),'own_seal_maxrss_native_bytes_observation':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'whole_RAM_IO_native_loader_tool_transport':'UNKNOWN_NOT_ZERO_NOT_PROVEN','Source_execution':0,'Source_AST_syntax_compile_tests':0,'Root_GPG_Node_network_live_gates':0,'model_children':0,'local_workers_used':1,'model_tokens_cost':'UNKNOWN_NOT_ZERO','source_resources':'unchanged original caps; no author allowance transfer','author_only_metadata_correction':'future_Root_requirements was dict not list; partial own literal updates retained then corrected; failed apply_patch verification no effects; no Source/runtime retry or cyclic guard bypass'})
files,dirs=listing();leaves=[]
for p in files:leaves.append(read(p)[1])
seal=write(ROOT/'seal.json',{'schema':'friday.sol060.full-sha9-private-pathset-seal.v1','root':str(ROOT),'leaves':leaves,'directories700':dirs,'pathset':sorted([str(p.relative_to(ROOT)) for p in files]+['seal.json','manifest.json']),'DAG':['current final Source/text/JSON+historical inert leaves','seal.json','manifest.json','external protected PEER RESULT'],'no_self_pin':True,'private700600_nlink1':True,'runtime':'NOT_RUN','whole':'CODE_OPEN'})
manifest=write(ROOT/'manifest.json',{'schema':'friday.sol060.manifest.v1','assignment':ASSIGNMENT,'generation':1,'root':str(ROOT),'seal_pin':seal,'current_source6':src,'leaf_count':len(leaves),'whole':'CODE_OPEN','changed6':5,'equal6':1,'all35':35,'all6':6,'all3':3,'F1_F2':'attempted connected Source; C01-C05 OPEN','SourceReady':False,'Root_admission':False,'compiler_ELF_ABI':'NOT_RUN','runtime':'NOT_RUN','gates':'NOT_RUN','GO':False})
expected={row['path']:row for row in leaves+[seal,manifest]}
for passno in (1,2):
    actual,actual_dirs=listing()
    assert set(map(str,actual))==set(expected) and actual_dirs==dirs
    for p in actual:assert read(p)[1]==expected[str(p)]
    AUDITS.append({'pass':passno,'files':len(actual),'all_full_bytes_SHA9_pathset_private':'PASS'})
files,_=listing();final_output=sum(p.stat().st_size for p in files)
known=4794480+900852+READS
assert known+100663296<268435456 and final_output<16775216
finished=time.time_ns();elapsed=finished-ACCEPTED
msk=datetime.datetime.fromtimestamp(finished/10**9,datetime.timezone(datetime.timedelta(hours=3))).isoformat()
result={'schema':'friday.peer.result.v1','assignment':ASSIGNMENT,'generation':1,'status':'COHERENT_SOURCE_ATTEMPT_CODE_OPEN','root':str(ROOT),'manifest_pin':manifest,'changed_equal':'5/1','all35_all6_all3_F1F2':'complete matrices; actual closure/bounds C01-C05 OPEN','accepted_msk':'2026-10-02T12:44:44.680479643+03:00','finished_msk':msk,'elapsed_ns':str(elapsed),'audit':'two full bytes/SHA9/private700600/nlink1/pathset passes; DAG leaves-seal-manifest-terminal','cost':{'known_stock_reads_before_terminal':known,'implicit_tool_literal_transport_read_reserve':100663296,'output_before_terminal':final_output,'whole_IO_RAM':'UNKNOWN_NOT_ZERO','children':0,'network':0},'SourceReady':False,'Root_admission':False,'compiler_ELF_ABI':'NOT_RUN','runtime':'NOT_RUN','gates':'NOT_RUN','GO':False}
encoded=(json.dumps(result,indent=2)+'\n').encode();assert len(encoded)<2000
terminal=write(TERMINAL,result)
assert read(TERMINAL)[1]==terminal
# No payload/manifest/terminal writes after this point.
assert sum(p.stat().st_size for p in files)+terminal['bytes']<16777216
text={'seal':'PASS_TWO_FULL_BYTE_SHA9_PRIVATE_PATHSET_AUDITS','files':len(files),'output_bytes_including_terminal':final_output+terminal['bytes'],'known_reads_including_terminal':4794480+900852+READS,'known_plus_reserved':4794480+900852+READS+100663296,'finished_msk':msk,'elapsed_seconds':elapsed/10**9,'manifest':manifest,'terminal':terminal,'whole':'CODE_OPEN','runtime':'NOT_RUN'}
print(json.dumps(text))
