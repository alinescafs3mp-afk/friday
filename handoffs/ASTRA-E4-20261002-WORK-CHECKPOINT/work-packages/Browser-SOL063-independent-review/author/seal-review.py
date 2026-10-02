"""Own final fullbyte private SHA9 seal; only stock metadata, no supplied Source execution."""
import os, stat, json, hashlib, datetime, resource, time
from pathlib import Path
OUT=Path('/var/tmp/friday-sol063-lab866-browser-whole209-all216-all29-independent-source-review')
os.umask(0o077);READ_BYTES=0;T0=time.monotonic_ns()
def nine(s):return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def rd(path,pin=None):
    global READ_BYTES
    path=Path(path);a=os.lstat(path)
    assert stat.S_ISREG(a.st_mode) and a.st_uid==os.getuid() and a.st_nlink==1 and a.st_mode&0o777==0o600,str(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        assert nine(os.fstat(fd))==nine(a)
        parts=[]
        while True:
            b=os.read(fd,1048576)
            if not b:break
            parts.append(b);READ_BYTES+=len(b)
        raw=b''.join(parts)
        assert nine(os.fstat(fd))==nine(a)==nine(os.lstat(path))
    finally:os.close(fd)
    row={'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'identity9_decimal_strings':nine(a)}
    if pin:
        assert row['bytes']==pin['bytes'] and row['sha256']==pin['sha256'],str(path)+' SHA'
        assert row['identity9_decimal_strings']==pin['identity9_decimal_strings'],str(path)+' nine'
    return raw,row
def save(name,obj):
    path=OUT/name;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    raw=(json.dumps(obj,ensure_ascii=False,indent=2)+'\n').encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw)
def private_tree():
    for path in [OUT]+list(OUT.rglob('*')):
        a=os.lstat(path);assert a.st_uid==os.getuid()
        if stat.S_ISDIR(a.st_mode):assert a.st_mode&0o777==0o700,str(path)
        else:assert stat.S_ISREG(a.st_mode) and a.st_mode&0o777==0o600 and a.st_nlink==1,str(path)
private_tree()
intake=json.loads(rd(OUT/'author/intake-verification.json')[0])
material=json.loads(rd(OUT/'author/materialization-read-ledger.json')[0])
# Revalidate every fullbyte witness from both successful helpers, not just new2.
pins={}
for p in intake['read']+material['reads']:
    if p['path'] in pins:
        assert pins[p['path']]==p,('cross-reader pin drift',p['path'])
    pins[p['path']]=p
for p in pins.values():rd(p['path'],p)
save('final-witness-revalidation.json',{'status':'ALL_FULLBYTE_WITNESSES_SHA9_STILL_MATCH','distinct':len(pins),'physical_read_bytes_this_phase':READ_BYTES,'source_execution':'NOT_RUN','private_originals_not_modified':True,'own_helper':True})
leaf_bytes=sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file())
shell_debit=112*1024*1024
sealing_future_debit=4*leaf_bytes+4*1024*1024
total=intake['read_bytes']+material['read_bytes']+READ_BYTES+shell_debit+sealing_future_debit
assert total<268435456,total
msk=datetime.timezone(datetime.timedelta(hours=3))
observed=datetime.datetime.now(msk)
accepted=datetime.datetime.fromisoformat('2026-10-02T15:19:09+03:00')
assert observed-accepted<datetime.timedelta(seconds=6000),'seal reserve breached'
save('costs.json',{'accepted_msk':accepted.isoformat(),'cost_ledger_observed_msk':observed.isoformat(timespec='seconds'),
 'wall_elapsed_seconds_at_ledger':int((observed-accepted).total_seconds()),'hard_wall_seconds':6600,'seal_reserve_seconds':600,
 'intake_fullbyte_read_bytes':intake['read_bytes'],'materialization_fullbyte_read_bytes':material['read_bytes'],
 'seal_witness_revalidation_read_bytes_so_far':READ_BYTES,'shell_physical_read_actual':'UNKNOWN_NOT_ZERO_NOT_PROVEN',
 'shell_conservative_read_budget_debit_bytes':shell_debit,'remaining_seal_verification_and_transport_read_budget_debit_bytes':sealing_future_debit,
 'cumulative_conservative_read_budget_debit_bytes':total,'read_limit_bytes':268435456,'RAM_limit_bytes':8589934592,
 'intake_helper_max_rss_kib':intake['max_rss_kib'],'materialization_helper_max_rss_kib':material['max_rss_kib'],
 'materialization_CPU_user_seconds':material['user_CPU_sec'],'materialization_CPU_system_seconds':material['system_CPU_sec'],
 'whole_assignment_RAM_implicit_IO_CPU_and_model_tokens':'UNKNOWN_NOT_ZERO_NOT_PROVEN',
 'output_limit_bytes':16777216,'output_bytes_before_seal':leaf_bytes,'model_children':0,'local_workers_used':1,'external_network':0,
 'supplied_execution_or_effect_retries':0,'metadata_helpers':'Successful single execution each, bounded stock bytes/hash/JSON only',
 'read_only_query_diagnostics':'Some jq/path selection reads returned diagnostics; not Source tests or execution. No declined effect retried.',
 'source_execution':'NOT_RUN','native_runtime_gates':'NOT_RUN','native_RESULT_transport':'Only required existing-channel RESULT; no Source/native execution',
 'completed_time_and_final_wall_elapsed':'Exact final protected RESULT after complete seal; this ledger is prior-to-final bounded snapshot'})
private_tree()
paths=sorted(p for p in OUT.rglob('*') if p.is_file())
leaves=[rd(p)[1] for p in paths]
save('seal/pathset.json',{'schema':'friday.sol063.private-full9-pathset.v1','leaves':leaves,
 'exact_pathset_including_seal_manifest':sorted([str(p.relative_to(OUT)) for p in paths]+['seal/pathset.json','manifest.json']),
 'identity9_fields':['dev','ino','mode','uid','gid','nlink','size','mtime_ns','ctime_ns'],'private_dirs0700_files0600_nlink1':True,
 'DAG':'leaves -> seal/pathset -> manifest -> external RESULT; exact names for seal/manifest not cyclic content hashes'})
seal_pin=rd(OUT/'seal/pathset.json')[1]
save('manifest.json',{'schema':'friday.sol063.independent-review-manifest.v1',
 'assignment':'ASTRA-E4-LAB866-BROWSER-WHOLE209-ALL216-ALL29-INDEPENDENT-SOURCE-REVIEW-SOL063','generation':1,
 'event':'ASTRA-SOL-E4-SOL063-T1','input_sha256':'541ef5fa6d0a386ca29c3f69c1020fb9bcc39a388a23be96a7300cb613f58109',
 'task_sha256':'af88460e12461a27632ef9e29c6121c82935c24efe09b20f0c0af294ece53ebb',
 'sealed_payload_pathset_pin':seal_pin,'payload_leaf_count':len(leaves),'review_relative_path':'review.json',
 'decision':'REJECT_CURRENT_WHOLE_SOURCE','CODE_count':9,'metadata_source_join_open':1,'SourceReady':False,'Root_admission':False,
 'runtime':'NOT_RUN','gates':'REQUIRED_NOT_RUN_NOT_WAIVED','GO':False,'DAG':'payload leaves -> pathset -> manifest -> one external RESULT'})
manifest_raw,manifest_pin=rd(OUT/'manifest.json')
sealed=json.loads(rd(OUT/'seal/pathset.json',seal_pin)[0])
assert sorted(str(p.relative_to(OUT)) for p in OUT.rglob('*') if p.is_file())==sealed['exact_pathset_including_seal_manifest']
for p in sealed['leaves']:rd(p['path'],p)
rd(OUT/'manifest.json',manifest_pin);private_tree()
output=sum(p.stat().st_size for p in OUT.rglob('*') if p.is_file())
assert output<16777216 and intake['read_bytes']+material['read_bytes']+READ_BYTES+shell_debit<total
print(json.dumps({'status':'SEALED_VERIFIED','manifest_pin':manifest_pin,'review_pin':next(p for p in leaves if p['path']==str(OUT/'review.json')),
 'files':len(sealed['exact_pathset_including_seal_manifest']),'output_bytes':output,'seal_fullbyte_read_bytes':READ_BYTES,
 'cumulative_read_debit_bytes':total,'seal_CPU_user_sec':resource.getrusage(resource.RUSAGE_SELF).ru_utime,
 'seal_CPU_system_sec':resource.getrusage(resource.RUSAGE_SELF).ru_stime,'seal_max_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
 'seal_elapsed_ns':str(time.monotonic_ns()-T0),'sealed_msk':datetime.datetime.now(msk).isoformat(timespec='seconds')}))
