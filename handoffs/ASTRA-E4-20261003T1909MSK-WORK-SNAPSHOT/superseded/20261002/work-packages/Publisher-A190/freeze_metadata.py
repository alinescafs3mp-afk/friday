"""Own final private-pathset/SHA9 stock metadata; never supplied Source exec."""
import datetime
import hashlib
import json
import os
import resource
import stat
import time

OUT='/var/tmp/friday-astra-a181-a186-publisher-whole15-all134-all69-all9-connected-source-closure-a190-g1'
NEW=OUT+'/candidate'
INPUT='/home/jericho/.jericho/grok-takeover/ASTRA-E4-A190-INPUT-20261002.json'
ASSIGNMENT='ASTRA-E4-A181-A186-PUBLISHER-WHOLE15-ALL134-ALL69-ALL9-CONNECTED-SOURCE-CLOSURE-A190'
TERMINAL='/home/jericho/.jericho/runtime/subagent-lifecycle/'+ASSIGNMENT+'-G1-RESULT.json'
CAP=268435456
read_bytes=0;hash_bytes=0;rows=[];cache={}
def nine(s):return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def wire(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
def read(path,expected=None,again=False):
    global read_bytes,hash_bytes
    if path not in cache or again:
        before=os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size>16*1024*1024:raise ValueError('bounded regular file')
        if read_bytes+before.st_size>24*1024*1024:raise ValueError('reserved final read budget')
        with open(path,'rb') as f:raw=f.read(before.st_size+1)
        after=os.lstat(path);read_bytes+=len(raw);hash_bytes+=len(raw)
        if len(raw)!=before.st_size or nine(before)!=nine(after):raise ValueError('file changed '+path)
        pin={'path':path,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
            'identity9_decimal_strings':nine(after),'stable9':True,'private600':stat.S_IMODE(after.st_mode)==0o600,'nlink1':after.st_nlink==1}
        cache[path]=(raw,pin);rows.append(pin)
    raw,pin=cache[path]
    if expected is not None:
        for key in ('bytes','sha256','identity9_decimal_strings'):
            if key in expected and expected[key]!=pin[key]:raise ValueError('pin drift '+path+' '+key)
    return raw,pin
def load(path):return json.loads(read(path)[0])
def write(name,value):
    path=OUT+'/'+name
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(wire(value));f.flush();os.fsync(f.fileno())
    return path
def full_pathset():
    paths=[];dirs=[]
    for root,names,files in os.walk(OUT,followlinks=False):
        for directory in [root]+[os.path.join(root,n) for n in names]:
            s=os.lstat(directory)
            if not stat.S_ISDIR(s.st_mode) or stat.S_IMODE(s.st_mode)!=0o700:raise ValueError('private directory')
        dirs.append(root)
        for name in files:
            path=os.path.join(root,name);s=os.lstat(path)
            if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode)!=0o600 or s.st_nlink!=1:raise ValueError('private nlink1 regular '+path)
            paths.append(path)
    return sorted(paths),sorted(dirs)
def main():
    if os.path.exists(TERMINAL) or os.path.exists(OUT+'/PACKAGE-MANIFEST.json') or os.path.exists(OUT+'/SEAL.json'):raise ValueError('single terminal/freeze already exists')
    for root,names,files in os.walk(OUT,followlinks=False):
        for path in [root]+[os.path.join(root,n) for n in names]:
            s=os.lstat(path)
            if not stat.S_ISDIR(s.st_mode):raise ValueError('directory symlink')
            if stat.S_IMODE(s.st_mode)!=0o700:os.chmod(path,0o700)
        for name in files:
            path=os.path.join(root,name);s=os.lstat(path)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:raise ValueError('regular private leaf')
            if stat.S_IMODE(s.st_mode)!=0o600:os.chmod(path,0o600)
    current=load(INPUT)
    if read(INPUT)[1]['sha256']!='6e5cfaccf0f0d3f66bedd7e0104b2cf5951ad34e331731c5e68a3eabc93ba77a':raise ValueError('input SHA')
    # Exact OLD snapshot reauthentication after all NEW Source writes.
    for row in current['current134']:read(row['pin']['path'],row['pin'])
    initial=load(OUT+'/read-ledger-initial.json');build=load(OUT+'/read-ledger-build.json')
    fault=load(OUT+'/metadata-build-fault.json')
    prior_success=[14258573,14248521]
    known_upper=(initial['distinct_read_bytes']+initial['failed_initial_administrative_pass_read_upper_bytes']+
        initial['manual_text_and_repeated_read_reserve_bytes']+32*1024*1024+fault['distinct_read_bytes']+
        sum(prior_success)+build['distinct_read_bytes'])
    if known_upper+24*1024*1024>CAP:raise ValueError('known bounded raw read cap')
    current_paths=sorted(row['name'] for row in current['current134'])
    actual=sorted(os.path.relpath(os.path.join(root,name),NEW) for root,dirs,files in os.walk(NEW) for name in files)
    if actual!=current_paths:raise ValueError('exact actual NEW134')
    source_manifest=load(NEW+'/manifest.json');consumer_manifest=load(NEW+'/consumer/enrollment-manifest.json')
    closure=load(NEW+'/closure.json')
    edges=[]
    def edge(parent,child):edges.append([parent,child])
    for row in source_manifest['members']:
        read(NEW+'/'+row['path'],row);edge('candidate/manifest.json','candidate/'+row['path'])
    if len(source_manifest['members'])!=133:raise ValueError('Source133')
    for row in consumer_manifest['members']:
        read(NEW+'/consumer/'+row['path'],row)
        edge('candidate/consumer/enrollment-manifest.json','candidate/consumer/'+row['path'])
    if len(consumer_manifest['members'])!=70:raise ValueError('consumer70')
    if read(NEW+'/closure.json')[0]!=read(NEW+'/consumer/contracts/current-source-package.json')[0]:raise ValueError('both-side current Source body')
    for row in closure['current_code_schema_hashes']:
        read(NEW+'/'+row['name'],row)
        for parent in ('candidate/closure.json','candidate/consumer/contracts/current-source-package.json'):edge(parent,'candidate/'+row['name'])
    graph={};visiting=set();seen=set()
    for a,b in edges:graph.setdefault(a,[]).append(b)
    def visit(node):
        if node in visiting:raise ValueError('active DAG cycle')
        if node in seen:return
        visiting.add(node)
        for child in graph.get(node,[]):visit(child)
        visiting.remove(node);seen.add(node)
    for node in graph:visit(node)
    write('active-package-DAG.json',{'schema':'friday.a190.actual-current134-active-DAG.v1','edges':edges,
        'edge_count':len(edges),'acyclic':True,'Source133':133,'consumer70':70,'code_schema66':len(closure['current_code_schema_hashes']),
        'historical_leaves':'inert provenance only; external historical pins are not active Source/admission/review credit','GO':False})
    write('resources-and-effects.json',{'schema':'friday.a190.author-resources-and-effects.v1',
        'author_caps':current['resources'],'known_raw_read_upper_before_final_pass':known_upper,
        'final_read_reserve_bytes':24*1024*1024,'known_bounded_raw_read_upper_including_final_reserve':known_upper+24*1024*1024,
        'recorded_initial_raw_read_bytes':initial['distinct_read_bytes'],
        'initial_failed_pass_read_upper':initial['failed_initial_administrative_pass_read_upper_bytes'],
        'manual_and_repeated_TEXT_read_reserve':initial['manual_text_and_repeated_read_reserve_bytes'],
        'failed_first_builder_read_upper':32*1024*1024,'failed_second_builder_recorded_raw_read_bytes':fault['distinct_read_bytes'],
        'prior_successful_builder_raw_read_bytes':prior_success,'final_builder_raw_read_bytes':build['distinct_read_bytes'],
        'metadata_SHA256_passes':'Full raw preimages; separately declared work, not product runtime/memory-hash credit',
        'aggregate_model_and_host_RAM_IO_CPU':'UNKNOWN_NOT_ZERO_NOT_PASS','implicit_interpreter_import_IO':'UNKNOWN_NOT_ZERO_NOT_PASS',
        'metadata_helper_peak_rss_bytes':build['metadata_helper_peak_rss_bytes'],
        'local_workers_used':1,'model_children':0,'network':0,'external_retries':0,
        'administrative_metadata_corrections':['relative manifest base','stock JSON literal trailing comma','complete60plus9 literal selector'],
        'supplied_import_AST_compiler_eval_exec_tests_native_Root_archive_net_install_UI_model_live_gates':False,
        'old_Source_product_Git_index_config_models_guards_locks_writes':False,
        'SourceReady':False,'Root_admission':False,'compiler_ELF_ABI':'NOT_RUN','runtime':'NOT_RUN','gates':'NOT_RUN','live':'NOT_RUN','GO':False})
    payload_paths,dirs=full_pathset()
    members=[]
    for path in payload_paths:
        p=read(path)[1]
        members.append(dict(p,relative_path=os.path.relpath(path,OUT)))
    payload_bytes=sum(row['bytes'] for row in members)
    if payload_bytes>16*1024*1024-1024*1024:raise ValueError('output reserve')
    frozen=datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest_path=write('PACKAGE-MANIFEST.json',{'schema':'friday.a190.new-private-whole-source-package-manifest.v1',
        'assignment':ASSIGNMENT,'generation':1,'members':members,'member_count':len(members),'member_bytes':payload_bytes,
        'exact_payload_pathset':[row['relative_path'] for row in members],
        'directory_pins_except_root':[{'path':os.path.relpath(path,OUT),'identity9_decimal_strings':nine(os.lstat(path))} for path in dirs if path!=OUT],
        'private700_600':True,'nlink1':True,'full_SHA9':True,'active_DAG_acyclic':True,
        'substantive_frozen_at_utc':frozen,'self_digest_exclusion':['PACKAGE-MANIFEST.json','SEAL.json'],
        'SourceReady':False,'Root_admission':False,'runtime':'NOT_RUN','gates':'NOT_RUN','GO':False})
    manifest_pin=read(manifest_path)[1]
    # Final full bytes: every payload leaf and manifest, not a cached claim.
    for row in members:read(row['path'],row,again=True)
    read(manifest_path,manifest_pin,again=True)
    final_total=known_upper+read_bytes
    if final_total>CAP:raise ValueError('final bounded raw read total')
    seal_path=write('SEAL.json',{'schema':'friday.a190.full-new-private-source-seal.v1','assignment':ASSIGNMENT,'generation':1,
        'manifest_pin':manifest_pin,'payload_pins':members,'member_count':len(members),
        'final_bytes_reauthenticated':True,'old_current134_reauthenticated':True,'private700_600':True,'nlink1':True,
        'acyclic':True,'substantive_frozen_at_utc':frozen,
        'final_stock_metadata_raw_read_bytes':read_bytes,'final_stock_metadata_memory_SHA_bytes':hash_bytes,
        'bounded_known_cumulative_raw_read_upper':final_total,'author_read_cap':CAP,
        'actual_whole_RAM_IO_CPU':'UNKNOWN_NOT_ZERO_NOT_PASS','SourceReady':False,'Root_admission':False,
        'compiler_ELF_ABI':'NOT_RUN','runtime':'NOT_RUN','gates':'NOT_RUN','live':'NOT_RUN','GO':False})
    seal_pin=read(seal_path)[1]
    final_paths,final_dirs=full_pathset()
    expected=sorted(payload_paths+[manifest_path,seal_path])
    if final_paths!=expected:raise ValueError('final exact pathset')
    finished=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    accepted=datetime.datetime(2026,10,2,14,54,33,tzinfo=finished.tzinfo)
    elapsed=(finished-accepted).total_seconds()
    if elapsed>6000:raise ValueError('substantive freeze crossed required seal reserve')
    output_bytes=sum(os.lstat(path).st_size for path in final_paths)
    if output_bytes>16*1024*1024:raise ValueError('final output cap')
    result={'schema':'friday.subagent.result.v2','assignment':ASSIGNMENT,'generation':1,
        'status':'DELIVERED_ACTUAL_CONNECTED_SOURCE_ATTEMPT_WITH_PRECISE_REMAINING_CODE',
        'accepted_msk':accepted.isoformat(),'finished_msk':finished.isoformat(),'actual_elapsed_from_accepted_seconds':elapsed,
        'accepted_timestamp_precision_seconds':1,'output_root':OUT,'manifest_pin':manifest_pin,'seal_pin':seal_pin,
        'root_directory_identity9_decimal_strings':nine(os.lstat(OUT)),
        'directory9':[{'path':os.path.relpath(path,OUT),'identity9_decimal_strings':nine(os.lstat(path))} for path in final_dirs],
        'exact_final_pathset':[os.path.relpath(path,OUT) for path in final_paths],
        'actual_current134':134,'changed_current134':17,'equal_current134':117,'changed_performing_files':10,
        'Source21':21,'consumer24':24,'schema20':20,'Source_manifest133':133,'consumer_enrollment70':70,
        'original58':58,'original22':22,'all15':15,'all69_ordered_sixfields_equal':True,'original9_current9_extra09_original11_retained':True,
        'all7_A186_actual_connected_attempted':True,'C03':'LOCAL_SOURCE_TRANSACTION_IMPLEMENTED_NEW_WHOLE_REVIEW_REQUIRED',
        'remaining_CODE':closure['remaining_CODE'],'codegap0':False,
        'bounded_known_cumulative_raw_read_upper':known_upper+read_bytes,
        'known_final_metadata_memory_SHA_bytes':hash_bytes,'output_bytes':output_bytes,
        'metadata_finalizer_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'whole_actual_RAM_implicit_IO_CPU':'UNKNOWN_NOT_ZERO_NOT_PASS',
        'supplied_source_execution_tests_compiler_native_Root_archive_network_install_UI_model_live_gates':False,
        'old_Source_product_Git_config_guards_models_locks_changed':False,
        'new_different_author_whole_review':'REQUIRED_BEFORE_CURRENT_ROOT_COMPILER_OR_EXECUTION',
        'SourceReady':False,'Root_admission':False,'compiler_ELF_ABI':'NOT_RUN','runtime':'NOT_RUN','gates':'NOT_RUN','live':'NOT_RUN','GO':False}
    fd=os.open(TERMINAL,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(wire(result));f.flush();os.fsync(f.fileno())
    result_pin=read(TERMINAL)[1]
    # No writes after terminal. Exact final directory9/pathset and every leaf
    # identity remain unchanged; RESULT is outside the sealed tree.
    if nine(os.lstat(OUT))!=result['root_directory_identity9_decimal_strings']:raise ValueError('root directory drift')
    if full_pathset()[0]!=final_paths:raise ValueError('post-terminal pathset drift')
    for row in members:
        if nine(os.lstat(row['path']))!=row['identity9_decimal_strings']:raise ValueError('post-terminal identity drift')
    print(json.dumps({'terminal':result_pin,'manifest_sha256':manifest_pin['sha256'],'seal_sha256':seal_pin['sha256'],
        'finished_msk':result['finished_msk'],'elapsed_seconds':elapsed,'output_bytes':output_bytes,
        'bounded_known_raw_read_upper':known_upper+read_bytes,'remaining_CODE':[r['id'] for r in closure['remaining_CODE']],
        'SourceReady':False,'GO':False}))

if __name__=='__main__':main()
