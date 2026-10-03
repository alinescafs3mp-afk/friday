"""A189 author-owned bounded stock TEXT/JSON/stat/SHA sealer, no supplied imports."""
import os,json,hashlib,stat,time,re,difflib,resource
from pathlib import Path
from datetime import datetime,timezone,timedelta

ROOT=Path('/var/tmp/friday-astra-a180-a185-scanner-whole17-all52-all2-connected-native-raw-source-closure-a189-g1')
BASE=Path('/var/tmp/friday-astra-lab862-a176-whole17-all52-all3-native-end-parenthood-raw-source-closure-a180-g1')
INPUT=Path('/home/jericho/.jericho/grok-takeover/ASTRA-E4-A189-INPUT-20261002.json')
TERMINAL=Path('/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-A180-A185-SCANNER-WHOLE17-ALL52-ALL2-CONNECTED-NATIVE-RAW-SOURCE-CLOSURE-A189-G1-RESULT.json')
ASSIGNMENT='ASTRA-E4-A180-A185-SCANNER-WHOLE17-ALL52-ALL2-CONNECTED-NATIVE-RAW-SOURCE-CLOSURE-A189'
OPEN='A189-C1-FINAL-ORIGINAL-PROCESS-CUSTODY-PARENTHOOD-UNACCEPTED'
read_bytes=0
def identity(s):return [str(getattr(s,k)) for k in ('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')]
def read(path):
    global read_bytes
    path=Path(path);before=path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1:raise ValueError('regular nlink1 '+str(path))
    if read_bytes+before.st_size>150000000:raise ValueError('final metadata cap')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        opened=os.fstat(fd);parts=[]
        while True:
            piece=os.read(fd,65536)
            if not piece:break
            parts.append(piece);read_bytes+=len(piece)
        raw=b''.join(parts);after=os.fstat(fd)
    finally:os.close(fd)
    named=path.lstat();ids=[identity(s) for s in (before,opened,after,named)]
    if any(v!=ids[0] for v in ids):raise ValueError('full9 drift '+str(path))
    return raw,{'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'identity9_decimal_strings':ids[0],'stable9':True}
def pairs(items):
    out={}
    for k,v in items:
        if k in out:raise ValueError('duplicate key '+k)
        out[k]=v
    return out
def parse(raw):return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x)))
def get(path):return parse(read(path)[0])
def write(name,obj):
    path=ROOT/name if not isinstance(name,Path) else name
    raw=(json.dumps(obj,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    if len(raw)>8000000:raise ValueError('leaf output cap')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        offset=0
        while offset<len(raw):offset+=os.write(fd,raw[offset:])
        os.fsync(fd)
    finally:os.close(fd)
    return read(path)[1]
def text_write(name,text):
    fd=os.open(ROOT/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        raw=text.encode();offset=0
        while offset<len(raw):offset+=os.write(fd,raw[offset:])
        os.fsync(fd)
    finally:os.close(fd)
def lex(file,text):
    lines=text.splitlines();found=[]
    for i,line in enumerate(lines):
        if file.endswith('.py'):
            m=re.match(r'^(\s*)(?:async )?(def|class) (\w+)',line)
            if m:found.append((i,len(m[1]),m[3],m[2]))
        else:
            m=re.match(r'^(?:static\s+)?(?:void|int|long|ssize_t|uint64_t|PyObject|PyMODINIT_FUNC)[\w\s*]*?\b(\w+)\s*\(',line)
            if m and '{' in '\n'.join(lines[i:i+12]).split(';',1)[0]:found.append((i,0,m[1],'native'))
    rows=[]
    for n,(start,indent,name,kind) in enumerate(found):
        end=next((a for a,b,c,d in found[n+1:] if b<=indent),len(lines));body=lines[start:end]
        sig=[]
        for line in body[:12]:
            sig.append(line)
            if (':' if kind!='native' else '{') in line:break
        rows.append({'name':name,'kind':kind,'line':start+1,'region_end_line':end,'signature_defaults_TEXT':'\n'.join(sig),
          'region_TEXT_sha256':hashlib.sha256(('\n'.join(body)+'\n').encode()).hexdigest(),
          'catch_cleanup_default_return_TEXT':[{'line':start+j+1,'text':line.strip()} for j,line in enumerate(body) if re.search(r'\b(except|finally|return|retire_|deadline|_exit|source_raw|capture|raw_origin|clear_tid)\b',line)],
          'ownership_end_resource_relation':'Same original native/meter/ResultOwner roots, finite original ends; '+OPEN,
          'runtime':'NOT_RUN','dynamic_call_proof':False})
    return rows

def main():
    for directory,dirs,files in os.walk(ROOT):
        os.chmod(directory,0o700)
        for name in files:
            p=Path(directory)/name
            if not stat.S_ISREG(p.lstat().st_mode):raise ValueError('nonregular owned output')
            os.chmod(p,0o600)
    inp=get(INPUT);auth=get(ROOT/'input-validation.json')
    if hashlib.sha256(read(INPUT)[0]).hexdigest()!='5508c60ea1482ee7f1dc31d8ae7c629e3571a696973960b2232b89f21b645ce0':raise ValueError('INPUT')
    validations=[]
    for expected in auth['authenticated_unique_pins']:
        raw,actual=read(expected['path'])
        if any(actual[k]!=expected[k] for k in ('bytes','sha256','identity9_decimal_strings')):raise ValueError('input drift '+expected['path'])
        validations.append(actual)
    write('input-final-validation.json',{'all_unique_pins':validations,'count':len(validations),'full_SHA9_stable':True,'stock_exact_integer_JSON':True,'explicit_sealer_read_bytes_to_this_checkpoint':read_bytes})
    rows=[];current={};source_text={};diff=[];methods=[]
    for entry in inp['current52']:
        file=entry['file'];before,bpin=read(entry['path']);after,apin=read(ROOT/file);current[file]=apin
        text=after.decode('utf-8');source_text[file]=text;changed=before!=after
        row={'file':file,'before':bpin,'current':apin,'changed':changed,'whole_byte_equal':not changed,'whole_current_bytes_read':True,
             'acceptance':'AUTHOR_ATTEMPT_UNREVIEWED','CODE_dependencies':[OPEN],'runtime':'NOT_RUN'};rows.append(row)
        if changed:diff.extend(difflib.unified_diff(before.decode().splitlines(True),text.splitlines(True),fromfile=entry['path'],tofile=str(ROOT/file)))
        if file.endswith(('.py','.c')):methods.append({'file':file,'pin':apin,'methods':lex(file,text),'complete_TEXT_read':True})
    if len(rows)!=52 or len(methods)!=28:raise ValueError('52/28 census')
    changed=[r['file'] for r in rows if r['changed']];equal=[r['file'] for r in rows if not r['changed']]
    write('source52-matrix.json',{'schema':'friday.a189.whole52.v1','source52':52,'code28':28,'schema14':14,'DATA5':5,'controls3':3,'index1':1,'plan1':1,'changed':changed,'equal':equal,'rows':rows,'SourceReady':False})
    write('delta-equal-matrix.json',{'schema':'friday.a189.delta-equal.v1','changed_count':len(changed),'equal_count':len(equal),'rows':rows,'old_acceptance_transferred':False})
    write('source-index.json',{'schema':'friday.a189.actual-source-index.v1','rows':[{'file':r['file'],'pin':r['current']} for r in rows],'loaded_code_namespace_schema_and_source_goldens':'INDEPENDENT_NEW_ROOT_BINDING_REQUIRED_NOT_RUN'})
    write('method-native-state-error-custody-phase-cost-matrix.json',{'schema':'friday.a189.code28.full-TEXT.v1','code28':28,'methods':methods,'definition_regions':sum(len(x['methods']) for x in methods),'inventory':'LEXICAL_TEXT_NOT_AST_OR_EXECUTION','native_handle_functions':21,'raw_owner_stock_consumer':'tools.state_binding.bind_native / source_raw_state_matches','physical_costs':'ORIGINAL_NATIVE_FENCE_REQUIRED; ACTUAL_SIZEOF/RSS/IO/CPU_UNKNOWN_NOT_ZERO'})
    text_write('changed-source.diff',''.join(diff))
    prior=get(BASE/'schema14-ref61-matrix.json');schemas={};srows=[]
    for s in prior['schemas']:
        file=s['file'];schemas[s['schema_id_or_alias']]=parse(read(ROOT/file)[0]);srows.append({'file':file,'id_or_original_alias':s['schema_id_or_alias'],'pin':current[file],'whole_schema_bytes_equal':file in equal})
    refs=[]
    def walk(value,file,pointer):
        if isinstance(value,dict):
            if '$ref' in value:
                ref=value['$ref'];schema,sep,fragment=ref.partition('#');target=schemas[schema]
                if sep:
                    for part in fragment.lstrip('/').split('/') if fragment else []:
                        part=part.replace('~1','/').replace('~0','~');target=target[int(part)] if isinstance(target,list) else target[part]
                refs.append({'file':file,'source_pointer':pointer,'reference':ref,'target_schema':schema,'resolved_target_kind':type(target).__name__,'stock_pointer_resolved':True})
            for k,v in value.items():walk(v,file,pointer+'/'+str(k).replace('~','~0').replace('/','~1'))
        elif isinstance(value,list):
            for i,v in enumerate(value):walk(v,file,pointer+'/'+str(i))
    for s in srows:walk(schemas[s['id_or_original_alias']],s['file'],'')
    if len(srows)!=14 or len(refs)!=61 or not all(s['whole_schema_bytes_equal'] for s in srows):raise ValueError('14/61 unchanged schema equation')
    write('schema14-ref61-matrix.json',{'schema':'friday.a189.schema14-ref61.v1','schema_count':14,'reference_count':61,'schemas':srows,'references':refs,'resolver':'OWN_STOCK_JSON_POINTER; SUPPLIED_VALIDATOR_NOT_RUN','SourceReady':False})
    original=get(BASE/'whole17-matrix.json');eqs=[]
    for e in original['equations']:
        eqs.append({k:e[k] for k in ('id','source_files','producer','consumer','raw_relation','phase') if k in e}|{'actual_source_pins':[current[f] for f in e['source_files']],
          'raw_custody_phase_end':'Prospective generic raw roots + prior original packet/slot/both-family consumers; original physical process loss remains '+OPEN,
          'cost':'Original caps unchanged; complete raw aliases/ContextVar/native/factory/cleanup costs UNKNOWN_NOT_ZERO_NOT_PROVEN',
          'SourceReady':False,'independent_review':'REQUIRED_NEW_DIFFERENT_AUTHOR_WHOLE','runtime':'NOT_RUN'})
    if len(eqs)!=17:raise ValueError('17')
    write('whole17-matrix.json',{'schema':'friday.a189.whole17.v1','count':17,'equations':eqs,'whole_closed':False,'CODE':[OPEN],'GO':False})
    for source,output,key in [('all6-matrix.json','all6-matrix.json','rows'),('current3-matrix.json','current3-matrix.json','rows')]:
        original=get(BASE/source);out=[]
        for r in original[key]:
            out.append({'original_id':r['original_id'],'genuine_prior_credit':r.get('genuine_current_credit',r.get('actual_attempt')),
              'actual_current_scope':'Original whole dependency preserved; warm FD/error retirement and Source/native raw registry are changed current bytes',
              'current_code_dependencies':[OPEN],'final_acceptance':'NOT_ACCEPTED_AUTHOR_ATTEMPT','actual_source_index':'source-index.json','runtime':'NOT_RUN'})
        write(output,{'schema':'friday.a189.'+output[:-5]+'.v1','count':len(out),'rows':out,'SourceReady':False})
    sites=[]
    for file,text in source_text.items():
        if file.endswith(('.py','.c')):
            for line,body in enumerate(text.splitlines(),1):
                if re.search(r'\b(existing_parent_deadline|native_cold_original_caller_consume|run_selected_existing_caller|source_raw_custody|retain_source_raw_exception|source_raw_state_matches|retain_source_origin|source_exception_detail|can_finalize|retirement_uncertain|PyRun_FileExFlags)\b',body):sites.append({'file':file,'line':line,'TEXT':body.strip(),'current_pin':current[file]})
    write('new2-matrix.json',{'schema':'friday.a189.a185-two-current-causes.v1','rows':[
      {'original_id':inp['current_two_CODE'][0]['id'],'current_id':OPEN,'status':'OPEN_CURRENT_CODE_AFTER_CONNECTED_ATTEMPT','actual_changes':'Warm FD/child/slot confirmation precedes raw graph clearance; eligible error finalization remains finite and failed','remaining':'Hard-end/startup/task/cold exits still lack actual accepted complete original external custody/parenthood','SourceReady':False},
      {'original_id':inp['current_two_CODE'][1]['id'],'status':'RAW_BEFORE_PROJECTION_SOURCE_IMPLEMENTED_AUTHOR_ATTEMPT_UNREVIEWED','actual_changes':'Native preowned raw factory/Source entry/error lane + whole generic firstcatch + ContextVar/meter/ResultOwner + no retained-scratch refund','lifetime_dependencies':[OPEN],'whole_accepted':False}], 'actual_sites':sites,'runtime':'NOT_RUN'})
    write('end-parenthood-raw-matrix.json',{'schema':'friday.a189.end-parenthood-raw.v1','actual_sites':sites,'original_topology_files':[current[f] for f in ('tools/native_owner.py','tools/root_holder.py','tools/worker_entry.py','tools/native_support.py','native/source_owner.c')],
      'warm':'Both direct-child/FD/slot retirement must be confirmed before graph clearance; error output remains failure',
      'cold_and_hard':'Actual sameTGID roots/clear_tid/finite waits are local retention; final original process loss unaccepted',
      'missing_original_authority_or_mechanism':'No pinned original reviewed stock relation authorizes shared arenas/file tables/adoption/external raw-root acceptance; no such mechanism invented',
      'normative_input':{'path':inp['original_A180_input_pin']['path'],'sha256':inp['original_A180_input_pin']['sha256'],'JSON_pointer':'/original_mandatory_edges/1 through /4'},'CODE':[OPEN]})
    phases=get(BASE/'phase-resource-matrix.json')
    write('phase-resource-matrix.json',{'schema':'friday.a189.phase-resource.v1','original_phase_names':[p.get('phase') for p in phases['phases']],
      'original_Source_seven_phases':phases['actual_Source_seven_phases'],'current_sources':current,'new_raw_scope':['preinit fixed error16','partial raw owner','ContextVar set/meter factory','codec/generic firstcatch','ZIP/DEB normal catch/cleanup','typed projector/recorder','terminal logical and native roots','ResultOwner/map/bytes factory','warm eligible error retirement','cold/hard remaining original process loss'],
      'Source16':True,'Root_original_B_plus_220':True,'canonical_global4':True,'original_ends':'UNCHANGED_NO_REFRESH','native_error_capacity':16,'extra_native_raw_root_pointers':1,'new_builtin_count':2,'whole_RAM_IO_CPU':'UNKNOWN_NOT_ZERO_NOT_PROVEN','CODE':[OPEN],'runtime':'NOT_RUN'})
    meta=get(BASE/'original202-data-metadata.json');meta['schema']='friday.a189.original202-metadata.v1';meta['actual_map_pin']=current['data/all202-identity-map.json'];meta['actual_index_pin']=current['input-index/index.json'];meta['current_source_index']='source-index.json';meta['archive_execution']='NOT_RUN';meta['SourceReady']=False;meta['GO']=False
    if meta['row_count']!=202 or len(meta['all_rows'])!=202:raise ValueError('202')
    write('original202-data-metadata.json',meta)
    write('resource-cost-delta-matrix.json',{'schema':'friday.a189.resource-cost-delta.v1','original_holder':13500416,'original_descriptions':[206,207],'one_body':372570142,'Source4':1490280568,'Root4':1490280568,'combined_body_lower':2980561136,'historical_NOT_FIT':{'required':630636544,'available':268435456,'excess':362201088},
      'new_costs':['one original BSS PyObject pointer sizeof UNKNOWN until actual ABI','SourceRawOwner/linked unique raw graph/header/frame/cause/context/notes aliases','ContextVar context/token/copies and actual closed runtime modules','matched21 builtin binding vs prior19','factory/error/cleanup/helper CPU and source metadata bytes','confirmed warm error finalization and earlier exact FD retirement'],
      'new_tasks_processes_FDs_slots':0,'A180_existing_internal_task':'UNCHANGED_ACTUAL_ORIGINAL_PID_TASK_FIT_REQUIRED_NOT_PROVEN','whole_RAM_IO_CPU':'UNKNOWN_NOT_ZERO_NOT_PROVEN','author_allowance_is_Source_grant':False})
    write('findings.json',{'schema':'friday.a189.author.remaining-CODE.v1','remaining_CODE':[{'id':OPEN,'status':'OPEN_CURRENT_CODE','statement':'Final physical original process loss is not preceded on all prefixes by confirmed finite retirement or actual legal original complete raw/native/202/FD/slot/parenthood acceptance','precise_sites':'end-parenthood-raw-matrix.json','original_constraints':'SOURCE.md and authenticated original A180 mandatory edges','scope':'ACTUAL_WHOLE52_28_14_61_17_6_3_2_202','future_facts_alone_not_CODE':True}],'generic_raw_firstcatch':'IMPLEMENTED_AUTHOR_ATTEMPT_UNREVIEWED_DEPENDS_FINAL_LIFETIME','SourceReady':False,'Root_admission':False,'runtime':'NOT_RUN','gates':'NOT_RUN','GO':False})
    write('implementation-contract.json',{'schema':'friday.a189.performing-source-contract.v1','Source52':52,'code28':28,'schemas14':14,'refs61':61,'whole17':17,'all6':6,'current3':3,'new2':2,'full202':202,'actual_code_pins':current,'native_handle_functions':21,'both_real_receivers':'UNCHANGED_ORIGINAL_TOPOLOGY_WITH_CHANGED_MATCHED_NATIVE_SUPPORT_AND_NATIVE_OWNER','new_external_provider_callback':False,'new_VM_filetable_sharing_adoption_service':False,'SourceReady':False,'independent_whole_review':'REQUIRED_NEW_DIFFERENT_AUTHOR','compiler_Root_runtime_gates':'NOT_RUN'})
    cost={'schema':'friday.a189.author.costs.v1','accepted_MSK':'2026-10-02 14:44:09 MSK','completed_at_seal_MSK':datetime.now(timezone(timedelta(hours=3))).isoformat(),'author_initial_explicit_read_bytes':auth['explicit_read_bytes'],'author_sealer_explicit_read_bytes_to_checkpoint':read_bytes,'unmetered_prior_shell_read_upper_bound_bytes':33554432,'sealer_remaining_reads_upper_bound_bytes':16777216,'cumulative_bound_bytes':auth['explicit_read_bytes']+read_bytes+33554432+16777216,'read_cap':268435456,'own_process_peak_RSS_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,'aggregate_shell_transport_RAM_IO_CPU':'UNKNOWN_NOT_ZERO','affinity':sorted(os.sched_getaffinity(0)),'local_workers_used':1,'model_children':0,'network':0,'source_execution':0,'tests_gates':0,'assignment_retries':0,'metadata_python_alias_missing_once':True}
    if cost['cumulative_bound_bytes']>268435456:raise ValueError('cumulative author read allowance')
    write('author-costs.json',cost)
    names=sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file());leafpins=[read(ROOT/n)[1] for n in names]
    for p in leafpins:
        if p['identity9_decimal_strings'][2]!='33152':raise ValueError('leaf mode600')
    planned=sorted(names+['seal.json','manifest.json']);pathset_sha=hashlib.sha256(json.dumps(planned,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
    seal=write('seal.json',{'schema':'friday.a189.leaf-seal.v1','leaves':leafpins,'expected_final_pathset':planned,'pathset_sha256':pathset_sha,'edges':'leaves only; expected namespace names are not hash edges','SourceReady':False})
    manifest=write('manifest.json',{'schema':'friday.a189.manifest.v1','assignment':ASSIGNMENT,'generation':1,'snapshot':inp['snapshot'],'leaf_pins':leafpins,'seal':seal,'expected_final_pathset':planned,'pathset_sha256':pathset_sha,'DAG':'leaves -> seal -> manifest -> protected external RESULT','SourceReady':False,'Root_admission':False,'runtime':'NOT_RUN','gates':'NOT_RUN','GO':False})
    observed=sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file())
    if observed!=planned:raise ValueError('final pathset')
    for expected in leafpins+[seal,manifest]:
        actual=read(expected['path'])[1]
        if actual!=expected:raise ValueError('frozen leaf drift')
    directories=[]
    for p in [ROOT]+sorted(p for p in ROOT.rglob('*') if p.is_dir()):
        if stat.S_IMODE(p.stat().st_mode)!=0o700:raise ValueError('private directory')
        directories.append({'path':str(p),'identity9_decimal_strings':identity(p.stat())})
    total=sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())
    if total>16777216:raise ValueError('output allowance')
    completed=datetime.now(timezone(timedelta(hours=3)));started=datetime(2026,10,2,14,44,9,tzinfo=completed.tzinfo);elapsed=(completed-started).total_seconds()
    if elapsed>6000:raise ValueError('author missed seal reserve')
    result={'schema':'friday.a189.connected-source.RESULT.v1','assignment':ASSIGNMENT,'generation':1,'owner_scope':'goal','status':'SOURCE_IMPLEMENTATION_ATTEMPT_WHOLE_OPEN','accepted_MSK':started.isoformat(),'completed_MSK':completed.isoformat(),'elapsed_seconds':elapsed,'output':str(ROOT),'manifest':manifest,'seal':seal,'directories_private700_full9':directories,'exact_pathset_count':len(planned),'pathset_sha256':pathset_sha,'changed_count':len(changed),'equal_count':len(equal),'changed':changed,'source52':52,'code28':28,'schema14':14,'ref61':61,'whole17':17,'all6':6,'current3':3,'new2':2,'full202':202,'remaining_CODE':[OPEN],'generic_Source_raw_origin':'SOURCE_IMPLEMENTED_AUTHOR_ATTEMPT_UNREVIEWED_FINAL_LIFETIME_DEPENDS_C1','source_execution':0,'model_children':0,'network':0,'output_bytes':total,'explicit_metadata_read_bytes':auth['explicit_read_bytes']+read_bytes,'unmetered_shell_transport_cost':'UNKNOWN_NOT_ZERO_WITH_RECORDED_READ_UPPER_BOUND','new_different_author_whole_review':'REQUIRED_NOT_RUN','SourceReady':False,'Root_admission':False,'compiler_ELF_ABI':'NOT_RUN','runtime':'NOT_RUN','gates':'NOT_RUN','GO':False,'unsafe_historical':'ABSTRACT_REQUIRED_NOT_RUN_NO_OPERATIONAL_BODY_OR_WAIVER','stop_after_this_terminal':True}
    terminal=write(TERMINAL,result)
    print(json.dumps({'RESULT':terminal,'manifest':manifest,'seal':seal,'changed':changed,'equal_count':len(equal),'output_bytes':total,'elapsed_seconds':elapsed,'remaining_CODE':[OPEN]},ensure_ascii=False))
if __name__=='__main__':main()
