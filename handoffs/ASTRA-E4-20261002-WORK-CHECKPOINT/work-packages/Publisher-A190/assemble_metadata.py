"""Own stock TEXT/JSON metadata materializer; no supplied Source execution."""
import difflib
import hashlib
import json
import os
import re
import resource
import stat
import time

OUT='/var/tmp/friday-astra-a181-a186-publisher-whole15-all134-all69-all9-connected-source-closure-a190-g1'
NEW=OUT+'/candidate'
OLD='/var/tmp/friday-astra-a173-a177-publisher-whole15-all134-all69-all9-connected-source-closure-a181-g1/candidate'
REVIEW='/var/tmp/friday-astra-a181-publisher-whole15-all134-all69-all9-independent-source-review-a186-g1'
INPUT='/home/jericho/.jericho/grok-takeover/ASTRA-E4-A190-INPUT-20261002.json'
ASSIGNMENT='ASTRA-E4-A181-A186-PUBLISHER-WHOLE15-ALL134-ALL69-ALL9-CONNECTED-SOURCE-CLOSURE-A190'
ledger=[];cache={}
def nine(s):return [str(v) for v in (s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)]
def read(path,expected=None):
    if path not in cache:
        before=os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size>16*1024*1024:raise ValueError('bounded regular TEXT/JSON required')
        with open(path,'rb') as f:raw=f.read(before.st_size+1)
        after=os.lstat(path)
        if len(raw)!=before.st_size or nine(before)!=nine(after):raise ValueError('read drift '+path)
        pin={'path':path,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'identity9_decimal_strings':nine(after),'stable9':True}
        cache[path]=(raw,pin);ledger.append(pin)
    raw,pin=cache[path]
    if expected is not None:
        for key in ('bytes','sha256','identity9_decimal_strings'):
            if key in expected and pin[key]!=expected[key]:raise ValueError('pin mismatch '+path+' '+key)
    return raw,pin
def load(path):return json.loads(read(path)[0])
def wire(value):return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)+'\n').encode('ascii')
def write(path,value):
    raw=wire(value) if not isinstance(value,bytes) else value
    if not path.startswith(OUT+'/'):raise ValueError('write scope')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw)
    os.chmod(path,0o600);cache.pop(path,None)
def pin(path):return read(path)[1]
def relative_pin(path,base):return dict(pin(path),path=os.path.relpath(path,base))
def methods(raw,path):
    lines=raw.decode('utf-8').splitlines(keepends=True);decl=[];classes=[]
    for at,line in enumerate(lines):
        m=re.match(r'^(\s*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)\s*[( :]',line)
        if not m:continue
        indent=len(m.group(1));kind=m.group(2);name=m.group(3)
        while classes and classes[-1][0]>=indent:classes.pop()
        qual='.'.join([v[1] for v in classes]+[name])
        if kind=='class':classes.append((indent,name))
        decl.append((at,indent,kind,qual))
    rows=[]
    for i,(start,indent,kind,name) in enumerate(decl):
        end=next((v[0] for v in decl[i+1:] if v[1]<=indent),len(lines))
        body=''.join(lines[start:end]).encode('utf-8')
        rows.append({'path':path,'line':start+1,'kind':kind,'name':name,'full_literal_span_sha256':hashlib.sha256(body).hexdigest(),
            'full_literal_span_bytes':len(body),'signature_prefix':lines[start].rstrip('\n'),
            'cause_literals':sorted(set(re.findall(r'(?:Refused|ContractError|RuntimeError)\([\x27\"]([^\x27\"]+)',body.decode('utf-8')))),
            'lexical_call_names':sorted(set(re.findall(r'\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(',body.decode('utf-8')))),
            'method':'literal TEXT spans only; not AST/import/compiler/runtime or independent semantic acceptance'})
    return rows

def main():
    start=time.monotonic_ns();current=load(INPUT);original_scope=load(REVIEW+'/independent-scope-matrices.json')
    paths=[r['name'] for r in current['current134']]
    actual=sorted(os.path.relpath(os.path.join(root,name),NEW) for root,dirs,files in os.walk(NEW) for name in files)
    if actual!=sorted(paths) or len(paths)!=134:raise ValueError('current134 pathset')
    oldbytes={r['name']:read(r['pin']['path'],r['pin'])[0] for r in current['current134']}
    newbytes={name:read(NEW+'/'+name)[0] for name in paths}
    codes=[name for name in paths if (name.startswith('source/') or name.startswith('consumer/source/')) and name.endswith('.py')]
    schemas=[name for name in paths if name.startswith('consumer/schemas/')]
    source=[name for name in codes if name.startswith('source/')]
    consumer=[name for name in codes if name.startswith('consumer/source/')]
    if (len(source),len(consumer),len(schemas))!=(21,24,20):raise ValueError('21/24/20')
    code_schema=sorted(codes+schemas+['schemas/friday.a138.roles.v1.json'])
    method_rows=[row for name in codes for row in methods(newbytes[name],name)]
    index={(r['path'],r['name']):r for r in method_rows}
    old_method_rows=[row for name in codes for row in methods(oldbytes[name],name)]
    old_index={(r['path'],r['name']):r for r in old_method_rows}
    # Actual unchanged combined guards/hash-admission mechanism, not an old
    # runtime credit. Capture receiver existence and full original inputs stay.
    keep=[('consumer/source/resource_meter.py','WholeMeter._combined_ok'),('consumer/source/resource_meter.py','WholeMeter._read_debit'),
          ('consumer/source/resource_meter.py','WholeMeter.check'),('consumer/source/resource_meter.py','WholeMeter.hash'),
          ('source/actor_bootstrap.py','VerifiedSource.admit'),('source/observer.py','RootObserver.before_physical_sample'),
          ('source/observer.py','RootObserver.check'),('source/observer.py','RootObserver.accept_capture')]
    genuine=[]
    for key in keep:
        a=old_index[key];b=index[key]
        equal=a['full_literal_span_sha256']==b['full_literal_span_sha256']
        if not equal:raise ValueError('genuine guard/receiver changed '+str(key))
        genuine.append({'path':key[0],'method':key[1],'full_literal_span_SHA_equal':True,'after':b,'runtime_credit':False})
    facts=[
      {'id':'A186-CODE-01','changed':['source/actor_bootstrap.py','source/lifetime.py','source/native.py','source/root_tool_adapter.py'],
       'actual_change':'Stock high-bit reader agrees with Frame; broad stock bootstrap boundary covers bind/hash/import/connect/configure/constructor; dedicated native/actor final-failure packets and parent readers now exist after the pre-exec packet.',
       'remaining_CODE':'Stock pre-admission prefix is explicitly incomplete. Unsupported full function/module/frame state and single-wire/deadline refusal can still prevent actual complete transfer before local loss. Not a complete closure.',
       'remaining':True},
      {'id':'A186-CODE-02','changed':['consumer/source/resource_meter.py','consumer/source/recipe_planner.py','source/consumer_bridge.py'],
       'actual_change':'Shared PID arena charged once per domain, monotonically identified call rows, result/error roots bound at both public entries, same-existing-source consumer arena receives full call roots before removing completed PID registrations.',
       'remaining_CODE':'Original direct standalone five-argument entry has no installed actual finite accepted completion receiver. Its exact unaccepted/UNKNOWN graph remains registered; full history workload-compatible bound is unclosed.',
       'remaining':True},
      {'id':'A186-CODE-03','changed':['source/lifetime.py','source/common.py','source/observer.py','source/native.py','source/root_tool_adapter.py'],
       'actual_change':'Both preowned cells built before publication; every publication/factory failure has finite acknowledged cancellation, unique reserved generations, PREOWNED occupies capacity. Actual fd_rows and Root arena-history completion now gate all final credit/history settlement and deregistration; failed releases keep graphs.',
       'remaining_CODE':None,'remaining':False,'closure_claim':'LOCAL_SOURCE_TRANSACTION_IMPLEMENTED_AWAITING_DIFFERENT_AUTHOR_WHOLE_REVIEW_NOT_RUNTIME_ACCEPTANCE'},
      {'id':'A186-CODE-04','changed':['source/capacity.py','source/observer.py','source/actor_bootstrap.py','source/lifetime.py','source/native.py','source/root_tool_adapter.py'],
       'actual_change':'Prospective width arithmetic explicitly includes row names/new fields and actual source/consumer/input/member banks; full values have repeated-identity references and all Source instance fields instead of result-only descriptions.',
       'remaining_CODE':'Full original reachable history/owner/body graph still has no unchanged 2M wire representability proof. Primitive body chunks/nodes can exceed canonical width/list bounds; no clipping, sampling, cap increase or scope reduction was introduced.',
       'remaining':True},
      {'id':'A186-CODE-05','changed':['source/observer.py','source/lifetime.py','source/root_tool_adapter.py'],
       'actual_change':'None callback is rejected. Full actual roots/native receipt/durable whole value body pin checked; one final settlement enumerates real Source roots/grants/rows, checks pending/live counters, retires attached histories, detaches holds and FinalArena and clears the entire adapter namespace before removing exact keys.',
       'remaining_CODE':'Complete native/error-frame/callback value correspondence is unsupported on the full actual graph; prefix qualified receiver correspondence and full tracebacks/closure alias census cannot be claimed complete. Current end transaction therefore preserves unresolved graphs.',
       'remaining':True},
      {'id':'A186-CODE-06','changed':['source/capacity.py','source/observer.py','source/actor_bootstrap.py','source/lifetime.py','source/native.py','source/root_tool_adapter.py','source/consumer_bridge.py'],
       'actual_change':'Actual prospective bank dimensions and duplicate final-failure receive arenas are declared before dependent effects. Narrowing retains both distinct accepted packet bodies; ordinary capture release only clears after actual credit confirmation.',
       'remaining_CODE':'Complete numeric original8GiB/read/output/time whole lifetime upper is still REQUIRED_NOT_CLOSED: Source functions/modules/frames/native stock, all normalized copies and end/fallback/tail/multiplicity remain unbounded by a workload-compatible proof.',
       'remaining':True},
      {'id':'A186-CODE-09','changed':['source/observer.py','source/root_tool_adapter.py','source/consumer_bridge.py','consumer/source/resource_meter.py'],
       'actual_change':'Actual primitive/container/bytes/error/traceback/frame/Source-object/bound-method/memoryview arenas are sent with alias references; Root validates nested bodies/counts/indices/result sizes/live credit/FD correspondence and retains refused bodies. No top-shape/token/None callback becomes full acceptance.',
       'remaining_CODE':'Actual functions/classes/modules/stock/native values reached through the full Source graph are explicitly unsupported; complete actor/native body acceptance is not established. Native complete receipt can only confirm a fully supported full body, not identities or hashes alone.',
       'remaining':True}
    ]
    for row in facts:
        row['sites']=[{'path':name,'pin':pin(NEW+'/'+name)} for name in row['changed']]
        row['runtime']='NOT_RUN';row['independent_review']='REQUIRED_NEW_DIFFERENT_AUTHOR';row['operational_payload']=None
    remaining=[{'id':r['id'],'exact_remaining_CODE':r['remaining_CODE']} for r in facts if r['remaining']]
    # All original58 raw physical preimages and complete A128 counterparts.
    historical58=load(OLD+'/proofs/historical58-and-current70-continuity.json')['all58']['rows58']
    matrix58=[]
    for row in historical58:
        before=read(row['before']['path'],row['before'])[0];a128=read(row['after']['path'],row['after'])[0]
        name='consumer/'+row['path'];after=newbytes[name]
        matrix58.append({'name':row['path'],'before':pin(row['before']['path']),'A128':pin(row['after']['path']),
            'after':pin(NEW+'/'+name),'full_before_vs_A128_equal':before==a128,'full_A128_vs_after_equal':a128==after,
            'complete_raw_read':True,'prior_acceptance_inherited':False})
    matrix22=[]
    for row in load(OLD+'/source-delta-index.json')['rows']:
        name=row['relative_path'];before=row['before'];a138=None if before is None else read(before['path'],before)[0]
        after=newbytes[name]
        matrix22.append({'name':name,'original_absence':before is None,'before':before,'after':pin(NEW+'/'+name),
            'full_byte_equal':a138==after,'absence_not_replaced_by_null_content':True,'prior_acceptance_inherited':False})
    if (len(matrix58),len(matrix22))!=(58,22):raise ValueError('original58/22')
    original_oracles=load(REVIEW+'/all69-independent-oracle-matrix.json')
    catalog=load(NEW+'/consumer/controls/catalog.json')['controls']
    text=newbytes['consumer/source/declared_controls.py'].decode('utf-8')
    begin=text.index('CATALOG = [')+len('CATALOG = ');end=text.index('\n]',begin)+2
    literal=json.loads(re.sub(r',\s*\]$', '\n]',text[begin:end]))
    extra_begin=text.index('EXTRA_CATALOG = [')+len('EXTRA_CATALOG = ')
    extra_end=text.index('\n]',extra_begin)+2
    literal+=json.loads(re.sub(r',\s*\]$', '\n]',text[extra_begin:extra_end]))
    keys=('id','scenario','status','cause','stage','match')
    tuples=[{k:r[k] for k in keys} for r in catalog]
    if len(tuples)!=69 or tuples!=[{k:r[k] for k in keys} for r in literal] or tuples!=[r['six_fields'] for r in original_oracles['rows']]:raise ValueError('literal/catalog/original six fields')
    a128catalog=load('/var/tmp/friday-astra-publisher-a122-whole-connected-source-closure-a128-g1/controls/catalog.json')['controls']
    if tuples!=[{k:r[k] for k in keys} for r in a128catalog]:raise ValueError('A128 original tuples')
    unsafe={'member_dotdot_refused','symlink_escape_refused'}
    oracles=[{'index':i,'six_fields':row,'required':True,'waiver':False,'operational_payload':None,
        'runtime':'REQUIRED_NOT_RUN','unsafe_historical_abstract_only':row['id'] in unsafe,
        'actual_same_full_six_fields':True,'golden_observation_not_fabricated':True,
        'Source_dispatch_pin':pin(NEW+'/consumer/source/declared_controls.py'),
        'full_independent_expected_public_output_still_required':True} for i,row in enumerate(tuples)]
    caps=original_scope['original_caps_and_obligations']
    closure={'schema':'friday.a190.connected-Source-closure.v1','assignment':ASSIGNMENT,'generation':1,
        'status':'ACTUAL_CONNECTED_PERFORMING_SOURCE_ATTEMPT_WITH_PRECISE_REMAINING_CODE',
        'SourceReady':False,'Root_admission':False,'compiler_ELF_ABI':'NOT_RUN','runtime':'NOT_RUN','gates':'NOT_RUN','GO':False,
        'source_count':21,'consumer_count':24,'schema_count':20,'all15':15,'all69':69,'all9_original':9,'all9_current_extra09':9,
        'current134_pathset':sorted(paths),'actual_bound_manifest':'manifest.json excludes itself; consumer/enrollment-manifest.json selects current-source-package.json',
        'current_code_schema_hashes':[{ 'name':name,'bytes':len(newbytes[name]),'sha256':hashlib.sha256(newbytes[name]).hexdigest()} for name in code_schema],
        'original_caps_and_obligations':caps,'original_roles_not_changed':True,'all30_F0_F11_full_gates_live':'ALL_ORIGINAL_REQUIRED_NOT_RUN',
        'remaining_CODE':remaining,'codegap0':False,'independent_A186_current7_attempted':facts,
        'new_different_author_WHOLE_review':'REQUIRED_BEFORE_CURRENT_ROOT_COMPILER_EXECUTION',
        'original9_current9_extra09_original11':'ALL_RETAINED; NEW AUTHOR IMPLEMENTATION CLAIMS NOT REVIEW ACCEPTANCE',
        'exact_integer_semantics':'stock integer JSON and exact raw optional presence; no V8 normalization',
        'unsafe_controls':'TWO ABSTRACT REQUIRED_NOT_RUN required=true waiver=false operational_payload=null ONLY',
        'historical_other_copied_members':'Authenticated inert provenance only, not current Source/review/runtime/gate credit',
        'hash_DAG':'code/schema -> identical closure/current-source-package -> consumer enrollment -> Source manifest -> external package manifest/seal/RESULT; self digests excluded'}
    write(NEW+'/closure.json',closure);write(NEW+'/consumer/contracts/current-source-package.json',closure)
    write(NEW+'/fix-matrix.json',{'schema':'friday.a190.actual7-connected-fix-matrix.v1','rows':facts,'remaining_CODE':remaining,'GO':False,'runtime':'NOT_RUN'})
    I=2000000;H=65536*8192
    costs={'schema':'friday.a190.actual-source-cost-matrix.v1','original_caps':caps,
        'rows':[
          {'component':'Root history domain','prospective_floor_bytes':H,'allocation_owner':'PreObserverHash token0 through real whole completion'},
          {'component':'actor complete-owner export','prospective_floor_bytes':H+I*560+131072,'wire_reads_reserved':33554432+I*8,'wire_output_reserved':I*2+16},
          {'component':'actor/native pre-exec and final-failure receiver','prospective_floor_bytes':H+I*1120+131072,'wire_reads_reserved':2*(I+8),'wire_output_reserved':2*(I+8),'both_distinct_received_bodies_retained':True},
          {'component':'bootstrap/error raw/parser/frame/Source values','numeric_complete_upper':None,'status':'REQUIRED_NOT_CLOSED'},
          {'component':'standalone shared PID','history_reservation_once_per_domain':H,'default_actual_complete_receiver':'ABSENT_CODE_OPEN'},
          {'component':'full native/result/copies/canonical/report/terminal/tail/fallback/end','numeric_complete_upper':None,'status':'REQUIRED_NOT_CLOSED'}],
        'full_actual_RAM_IO_CPU':'REQUIRED_NOT_RUN_NOT_ZERO_NOT_PASS','caps_raised':False,'reservations_are_measurements':False,'GO':False}
    write(NEW+'/cost-matrix.json',costs)
    diffs=[]
    for name in source+consumer:
        if oldbytes[name]!=newbytes[name]:
            diffs.extend(difflib.unified_diff(oldbytes[name].decode('utf-8').splitlines(keepends=True),newbytes[name].decode('utf-8').splitlines(keepends=True),fromfile='A181/'+name,tofile='A190/'+name,n=3))
    write(NEW+'/complete-unified.diff',''.join(diffs).encode('utf-8'))
    # Consumer current70 complement is complete and canonical under the actual
    # unchanged Source common.parse serializer. No previous manifest is edited.
    cpaths=sorted(name[len('consumer/'):] for name in paths if name.startswith('consumer/') and name!='consumer/enrollment-manifest.json')
    if len(cpaths)!=70:raise ValueError('consumer70')
    write(NEW+'/consumer/enrollment-manifest.json',{'schema':'friday.a190.consumer-enrollment-manifest.v1',
        'members':[relative_pin(NEW+'/consumer/'+name,NEW+'/consumer') for name in cpaths],
        'enrollment_files_count':70,'future_consumer_manifest_selects_this_file_not_original_manifest':True,
        'original_consumer_manifest_retained_as_historical_member':True,'GO':False})
    write(NEW+'/manifest.json',{'schema':'friday.a190.Source-manifest.v1','assignment':ASSIGNMENT,'generation':1,
        'members':[relative_pin(NEW+'/'+name,NEW) for name in sorted(paths) if name!='manifest.json'],
        'Source_ready':False,'GO':False,'new_author_package':True,'old_Source_and_consumer_bytes_immutable':True,
        'new_different_author_whole_review_REQUIRED':True})
    delta=[]
    for name in sorted(paths):
        after=read(NEW+'/'+name)[0]
        delta.append({'name':name,'before':current['current134'][paths.index(name)]['pin'],'after':pin(NEW+'/'+name),
            'full_raw_bytes_equal':oldbytes[name]==after,'before_bytes':len(oldbytes[name]),'after_bytes':len(after),
            'comparison':'FULL_RAW_BYTES; not summary diff or execution credit'})
    # Rebuild matrices against current bytes/sites, retaining normative duties
    # only. Independent author/classification from A186 is never inherited.
    all15=[]
    for row in original_scope['all15']:
        entry=dict(row);entry.pop('site',None)
        entry['actual_current_performing_pin']=pin(NEW+'/source/operations.py')
        method=row['actual_Source_method'];entry['current_literal_declaration']=index.get(('source/operations.py',method))
        entry['end_dependencies']=[r['id'] for r in facts if r['remaining']]
        entry['prior_independent_acceptance_inherited']=False;entry['runtime']='REQUIRED_NOT_RUN';all15.append(entry)
    original11=[{'id':row['id'],'normative_mechanism':load('/home/jericho/.jericho/grok-takeover/ASTRA-E4-SOL053-INPUT-20261002.json')['all11'][i],
        'current_Source21_pins':[pin(NEW+'/'+name) for name in source],
        'author_status':'GENUINE_LOCAL_MECHANISMS_RETAINED_WHOLE_END_COST_OPEN' if row['id']!='S052-11' else 'NUMERIC_WHOLE_UPPER_REQUIRED_NOT_CLOSED',
        'runtime':'NOT_RUN','old_review_acceptance_inherited':False} for i,row in enumerate(original_scope['original11'])]
    matrices={'schema':'friday.a190.whole-original-and-current-matrices.v1','current134':delta,
        'Source21':[pin(NEW+'/'+name) for name in source],'consumer24':[pin(NEW+'/'+name) for name in consumer],
        'schema20':[{'pin':pin(NEW+'/'+name),'raw_equal_to_current_A181':newbytes[name]==oldbytes[name]} for name in schemas],
        'all15':all15,'original11':original11,'original9':original_scope['original_A1709'],
        'current9_and_extra09':[dict(row,independent=False,prior_classification_only=True,current_A190_whole_acceptance=False) for row in original_scope['current9']],
        'phase_matrix':[dict(row,current_A190_review_acceptance=False,current_source_pins=[pin(NEW+'/'+name) for name in source],runtime='NOT_RUN') for row in original_scope['phase_matrix']],
        'ownership_matrix':[dict(row,current_A190_actual7_status=facts,current_A190_whole_acceptance=False,runtime='NOT_RUN') for row in original_scope['ownership_matrix']],
        'original_caps':caps,'genuine_C07_C08':genuine,'method_alias_bindings':original_scope['method_alias_bindings'],
        'default_and_unadmitted_paths':{'original_normative_routes':original_scope['default_and_unadmitted_paths'],
            'current_Source_method_index':'method-default-cause-index.json','whole_original_default_acceptance':False,
            'actual_remaining_default_standalone_receiver':'A186-CODE-02',
            'actual_remaining_default_bootstrap_owner_and_error_bodies':['A186-CODE-01','A186-CODE-09'],
            'not_privateheap_or_arbitrary_host_assumptions':True,'runtime':'NOT_RUN'},
        'original_body11':original_scope['original_body11'],'original_capability_ABI16':original_scope['original_capability_ABI16'],
        'outer_expected_ref_not_a_body11_field':original_scope['outer_expected_ref_not_a_body11_field'],
        'retain_then_independent_expected_then_consumer_then_public_hash':original_scope['retain_then_independent_expected_then_consumer_then_public_hash'],
        'compiler_ELF_ABI':'NOT_RUN','Root_admission':False,'SourceReady':False,'runtime':'NOT_RUN','gates':'NOT_RUN','live':'NOT_RUN','GO':False}
    write(OUT+'/whole-matrices.json',matrices)
    write(OUT+'/all69-sixfield-oracle-matrix.json',{'schema':'friday.a190.all69-exact-original-sixfields.v1','rows':oracles,
        'two_full_original_positives':'BOTH_REQUIRED_NOT_RUN','tuple_migrations':original_oracles['tuple_migrations'],
        'unrelated_sentinel':original_oracles['unrelated_sentinel'],'GO':False})
    write(OUT+'/original58-22-raw-continuity.json',{'schema':'friday.a190.full-original58-22-raw.v1','original58':matrix58,'original22':matrix22,
        'all_original_full_body_preimages_physically_read':True,'absence_preserved':True,'acceptance_inherited':False,'GO':False})
    write(OUT+'/method-default-cause-index.json',{'schema':'friday.a190.actual45-full-text-method-index.v1','rows':method_rows,
        'full_code_and_defaults':'ALL45 full bytes read; literal spans/cause/call metadata only; semantic review/compiler/tests NOT_RUN',
        'genuine_preserved':genuine,'GO':False})
    write(OUT+'/delta-equal-current134.json',{'schema':'friday.a190.current134-full-byte-delta-equal.v1','rows':delta,
        'changed':sum(not r['full_raw_bytes_equal'] for r in delta),'equal':sum(r['full_raw_bytes_equal'] for r in delta),'count':134,'GO':False})
    # Canonical wire/readiness is a stock TEXT/JSON comparison, not a supplied
    # Source parser or gate. All source/module body execution remains forbidden.
    canonical=[]
    for name in ('manifest.json','closure.json','consumer/enrollment-manifest.json','consumer/contracts/current-source-package.json'):
        raw=read(NEW+'/'+name)[0];value=json.loads(raw)
        if raw!=wire(value):raise ValueError('raw canonical mismatch '+name)
        canonical.append({'name':name,'raw_canonical_equal':True,'pin':pin(NEW+'/'+name)})
    write(OUT+'/stock-canonical-and-source-readiness.json',{'schema':'friday.a190.stock-only-canonical-readiness.v1','wires':canonical,
        'declared_original_source133':133,'declared_original_consumer70':70,'actual134_pathset_equal':True,
        'all69_ordered_sixfields_source_catalog_A128_equal':True,'exact_integer_and_absence':True,
        'supplied_source_import_AST_compiler_eval_exec_tests_native_Root_archive_net_install_UI_model_live_gates':False,
        'GO':False})
    write(OUT+'/read-ledger-build.json',{'rows':ledger,'distinct_read_bytes':sum(r['bytes'] for r in ledger),
        'method':'own stock raw TEXT/JSON full read/hash/lstat; trusted metadata only','runtime_credit':False,
        'metadata_helper_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'metadata_helper_elapsed_ns':time.monotonic_ns()-start,'whole_actual_RAM_IO_CPU':'UNKNOWN_NOT_ZERO_NOT_PASS'})
    print(json.dumps({'current134':134,'source21':len(source),'consumer24':len(consumer),'schema20':len(schemas),
        'original58':len(matrix58),'original22':len(matrix22),'all69':len(oracles),'methods_classes_literal':len(method_rows),
        'changed134':sum(not r['full_raw_bytes_equal'] for r in delta),'remaining_CODE':[r['id'] for r in remaining],
        'metadata_read_bytes':sum(r['bytes'] for r in ledger),'GO':False}))

if __name__=='__main__':
    try:main()
    except BaseException as exc:
        write(OUT+'/metadata-build-fault.json',{'error':str(exc),'rows':ledger,
            'distinct_read_bytes':sum(r['bytes'] for r in ledger),'Source_executed':False})
        raise
