"""Author-owned mechanical text/JSON materializer. Never evaluates supplied code."""
import os
import sys
import json
import hashlib
sys.path.insert(0,'/var/tmp/friday-astra-lab858-sol057-whole209-all216-actual-source-implementation-a171-g1')
import a171_metadata as m

ASSIGNMENT='ASTRA-E4-LAB858-SOL057-WHOLE209-ALL216-ACTUAL-SOURCE-IMPLEMENTATION-A171'
OLD='/var/tmp/friday-astra-browser-a153-a156-whole216-all6-all2-connected-source-implementation-a158-g1'
LAB='/var/tmp/friday-lab858-a158-whole216-all6-remaining-connected-source-implementation'

def replace(raw,before,after,count=1):
    if raw.count(before)!=count:raise ValueError('mechanical anchor count '+str(raw.count(before))+' for '+before[:90])
    return raw.replace(before,after)

def dump(data):return (json.dumps(data,ensure_ascii=True,indent=2)+'\n').encode('ascii')

def store_raw(name,raw):
    fd=os.open(m.ROOT+'/'+name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        pos=0
        while pos<len(raw):pos+=os.write(fd,raw[pos:])
        os.fsync(fd)
    finally:os.close(fd)

def main():
    source=m.load(OLD+'/SOURCE-REFERENCE-MANIFEST.json')
    members={item['name']:item for item in source['members']}
    originals={name:m.read(members[name]['pin']['path'],members[name]['pin'])[0] for name in
        ('A071-CONTROLS.py','A087-PUBLIC-DRIVER.py','A071-SUPERVISOR.py','A071-BILL.json',
         'A071-SCHEMA.json','A071-BUILD-RECIPE.json','A118-CONNECTED-CONTRACT.json','A104-ORDINARY-CONTRACTS.json')}
    operations=m.read(m.ROOT+'/A171-OPERATIONS.txt')[0].decode('ascii')
    input_code=m.read(m.ROOT+'/A171-INPUT.txt')[0].decode('ascii')
    validator=operations[operations.index('def a171_validate_observation('):]
    table=m.load(OLD+'/TYPED216-CONTRACTS.json')['rows']
    original_expected=m.load(m.ROOT+'/ORIGINAL216-EXPECTATIONS.json')['rows']
    assert len(table)==216
    for i,(row,original) in enumerate(zip(table,original_expected)):
        assert row['position']==i==original['position'] and row['id']==original['id'] and row['expected']==original['expected']
        if not row['active']:
            row.update(active=True,producer='a171_actual',
                effect_domain='original_bounded_owned_operands_actual_consumer_or_explicit_code_gap',
                safety_classification='BENIGN_FUNCTIONAL_OBLIGATION_HISTORICAL_METHOD_NOT_AUTHORIZED')
    controls=originals['A071-CONTROLS.py'].decode('ascii')
    lines=controls.splitlines(keepends=True)
    matches=[i for i,line in enumerate(lines) if line.startswith('WHOLE216_TABLE=')]
    assert len(matches)==1
    lines[matches[0]]='WHOLE216_TABLE='+repr(table)+'\n'
    controls=''.join(lines)
    controls=replace(controls,'    return data,spec\n','    a171_validate_input(data,spec["expected"],c.need)\n    return data,spec\n')
    controls=replace(controls,'    args=data["arguments"];route=spec["producer"]\n',
        '    args=data["arguments"];route=spec["producer"]\n    if route=="a171_actual":return a171_perform(data,spec,plan,custody)\n')
    # Each existing branch calls the same original consumer; instrumentation records
    # the actual invocation, never a table-derived claim that a call happened.
    old_instrumentation=[
        ('result=x.mapping_check(docs,plan["browser_archives"])','result=a171_called("mapping_check","mapping_check",x.mapping_check,docs,plan["browser_archives"])'),
        ('return x.compile_bill(args["plan"])','return a171_called("compile_bill","compile_bill",x.compile_bill,args["plan"])'),
        ('tokens=x.tokens(whole216_hex(args["raw_hex"],524288))','tokens=a171_called("tokens","tokens",x.tokens,whole216_hex(args["raw_hex"],524288))'),
        ('value=x.m.inert_json(whole216_hex(args["raw_hex"],262144),262144)','value=a171_called("G1.inert_json","G1.inert_json",x.m.inert_json,whole216_hex(args["raw_hex"],262144),262144)'),
        ('line=reader.readline();lines+=bool(line)','line=a171_called("G1.HeaderReader.readline","G1.HeaderReader.readline",reader.readline);lines+=bool(line)'),
        ('value=x.public_admission(args["argv"],args["environment"],s.sys.flags,x.STEM+"-EXECUTOR.py")','value=a171_called("public_admission","public_admission",x.public_admission,args["argv"],args["environment"],s.sys.flags,x.STEM+"-EXECUTOR.py")'),
        ('x.BoundedOutput(None,args["paths"],args["dirs"],None)','a171_called("BoundedOutput.__init__","BoundedOutput.__init__",x.BoundedOutput,None,args["paths"],args["dirs"],None)'),
        ('x.disk_reservation(args["remote"],args["metadata_bytes"])','a171_called("disk_reservation","disk_reservation",x.disk_reservation,args["remote"],args["metadata_bytes"])'),
        ('x.reservations(args["remote"],whole216_hex(args["raw_hex"],262144),args["paths"],args["dirs"])','a171_called("reservations","reservations",x.reservations,args["remote"],whole216_hex(args["raw_hex"],262144),args["paths"],args["dirs"])'),
        ('value=s.bounded_fd_bytes(fd,args["sha256"],args["cap"],sealed=args["sealed"],root=args["root"],','value=a171_called("Supervisor.bounded_fd_bytes","Supervisor.bounded_fd_bytes",s.bounded_fd_bytes,fd,args["sha256"],args["cap"],sealed=args["sealed"],root=args["root"],')]
    for before,after in old_instrumentation:controls=replace(controls,before,after)
    controls=replace(controls,'def whole216_producer(data,spec,plan):\n',
        'def whole216_producer(data,spec,plan):\n    global A171_OBSERVATION\n    A171_OBSERVATION=a171_new_observation()\n')
    controls=replace(controls,'    resources=c.production_resources(c.ADMISSION)\n    result=None;primary=None;outcome="RETURNED";custody=[];operation_error=None\n    try:result=whole216_perform(data,spec,plan,custody)\n    except BaseException as exc:primary=x.cause(exc);outcome="REFUSED";operation_error=exc\n',
        '''    resources=None
    result=None;primary=None;outcome="RETURNED";custody=[];operation_error=None
    try:
        resources=c.production_resources(c.ADMISSION)
        A171_OBSERVATION["resource_precondition"]={"passed":True,"actual":resources}
        result=whole216_perform(data,spec,plan,custody)
    except BaseException as exc:
        operation_error=exc;outcome="REFUSED"
        if isinstance(exc,A171CodeGap):
            A171_OBSERVATION["unavailable_reason"]=str(exc)
            primary="A171_CODE_GAP:"+str(exc)
        else:
            fault=A171_OBSERVATION["first_fault"]
            primary=fault["cause"] if fault is not None else x.cause(exc)
            if fault is None:
                A171_OBSERVATION["first_fault"]={"cause":primary,"stage":"producer_precondition",
                    "consumer":None,"at_ns":str(time.monotonic_ns())}
        if resources is None:A171_OBSERVATION["resource_precondition"]={"passed":False,"cause":primary}
    if primary is None and A171_OBSERVATION["first_fault"] is not None:
        primary=A171_OBSERVATION["first_fault"]["cause"];outcome="REFUSED"
    if A171_OBSERVATION["state"] is None:
        A171_OBSERVATION["state"]="REFUSED" if primary is not None else "CONSUMER_ACCEPTED_NO_ADMISSION"
    A171_OBSERVATION["descriptor_records"]=[{key:value for key,value in record.items() if key!="original_error"} for record in custody]
''')
    controls=replace(controls,'        "consumer_result":result,"actual_actor":',
        '        "consumer_result":result,"operation_observation":A171_OBSERVATION,"operation_error_DATA":None if operation_error is None else {"type":type(operation_error).__name__,"cause":primary,"errno":getattr(operation_error,"errno",None),"original_object_lifetime":"same_producer_until_actual_exit"},"actual_actor":')
    controls=replace(controls,'            passed=True\n        except BaseException as exc:primary=x.cause(exc);original_error=exc\n',
        '            a171_validate_observation(producer["operation_observation"],spec["expected"],c.need)\n            passed=True\n        except BaseException as exc:primary=x.cause(exc);original_error=exc\n')
    controls+='\n'+input_code+'\n'+operations+'\n'
    driver=originals['A087-PUBLIC-DRIVER.py'].decode('ascii')
    driver=replace(driver,'"public_admission":"public_admission","BoundedOutput.__init__":"output_count_refusal"}.get(consumer)',
        '"public_admission":"public_admission","BoundedOutput.__init__":"output_count_refusal"}.get(consumer,"a171_actual")')
    driver=replace(driver,'    oracle_raw,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],131072)\n    row=json_DATA(oracle_raw)\n    need(set(row)=={"schema","id","position","expected","mandatory_scope","SourceReady","GO"}',
        '    a171_validate_input(data,meta,need)\n    oracle_raw,oracle_snap=pinned(bundle["oracle_fd"],bundle["oracle_sha256"],131072)\n    row=json_DATA(oracle_raw)\n    need(set(row)=={"schema","id","position","expected","mandatory_scope","SourceReady","GO"}')
    driver=replace(driver,'    actor=producer["actual_actor"];need(actor==',
        '    a171_validate_observation(producer["operation_observation"],expected["expected"],need)\n    actor=producer["actual_actor"];need(actor==')
    driver=replace(driver,'manifest["schema"]=="friday.a158.connected-whole-source-manifest.v1" and manifest["assignment"]==\n        "ASTRA-E4-BROWSER-A153-A156-WHOLE216-ALL6-ALL2-CONNECTED-SOURCE-IMPLEMENTATION-A158"',
        'manifest["schema"]=="friday.a171.connected-whole-source-manifest.v1" and manifest["assignment"]==\n        "'+ASSIGNMENT+'"',2)
    driver+='\n'+input_code+'\n'+validator+'\n'
    supervisor=originals['A071-SUPERVISOR.py'].decode('ascii')
    supervisor=replace(supervisor,'def bounded_fd_bytes(fd, sha, cap, *, sealed=False, root=False, deadline=None, collect=True):',
        '''def before_deadline(deadline, deadline_ns=None):
    if deadline_ns is not None:
        need(type(deadline_ns) is int and deadline_ns>=0,"EXACT_DEADLINE_NS")
        return time.monotonic_ns()<deadline_ns
    return deadline is None or time.monotonic()<deadline


def bounded_fd_bytes(fd, sha, cap, *, sealed=False, root=False, deadline=None, collect=True, deadline_ns=None):''')
    supervisor=replace(supervisor,'        if deadline is not None:\n            need(time.monotonic() < deadline, "HELD_READ_TIMEOUT")',
        '        if deadline is not None or deadline_ns is not None:\n            need(before_deadline(deadline,deadline_ns), "HELD_READ_TIMEOUT")')
    supervisor=replace(supervisor,'def __init__(self, path, sha, cap=PIPE_CAP, *, system=False, deadline=None):',
        'def __init__(self, path, sha, cap=PIPE_CAP, *, system=False, deadline=None, deadline_ns=None):')
    supervisor=replace(supervisor,'raw = bounded_fd_bytes(original, sha, cap, deadline=deadline)',
        'raw = bounded_fd_bytes(original, sha, cap, deadline=deadline, deadline_ns=deadline_ns)')
    supervisor=replace(supervisor,'                if deadline is not None:\n                    need(time.monotonic() < deadline, "HELD_READ_TIMEOUT")',
        '                if deadline is not None or deadline_ns is not None:\n                    need(before_deadline(deadline,deadline_ns), "HELD_READ_TIMEOUT")')
    supervisor=replace(supervisor,'need(bounded_fd_bytes(self.fd, sha, cap, sealed=True, deadline=deadline) == raw, "HELD_COPY_DRIFT")',
        'need(bounded_fd_bytes(self.fd, sha, cap, sealed=True, deadline=deadline, deadline_ns=deadline_ns) == raw, "HELD_COPY_DRIFT")')
    supervisor=replace(supervisor,'    def bytes(self, cap=PIPE_CAP, deadline=None):\n        return bounded_fd_bytes(self.fd, self.sha, cap, sealed=True, deadline=deadline)',
        '    def bytes(self, cap=PIPE_CAP, deadline=None, *, deadline_ns=None):\n        return bounded_fd_bytes(self.fd, self.sha, cap, sealed=True, deadline=deadline, deadline_ns=deadline_ns)')
    supervisor=replace(supervisor,'    def held(self, logical, row, deadline):\n        return HeldSource(self.path(logical), row["sha256"], row["bytes"],\n                          system=not self.fixture, deadline=deadline)',
        '    def held(self, logical, row, deadline, *, deadline_ns=None):\n        return HeldSource(self.path(logical), row["sha256"], row["bytes"],\n                          system=not self.fixture, deadline=deadline, deadline_ns=deadline_ns)')
    supervisor=replace(supervisor,'    def check_held(self, deadline):', '    def check_held(self, deadline, *, deadline_ns=None):')
    supervisor=replace(supervisor,'                             deadline=deadline, collect=False)',
        '                             deadline=deadline, collect=False, deadline_ns=deadline_ns)')
    supervisor=replace(supervisor,'def runtime_preflight(path, sha, deadline, *, view=None):',
        'def runtime_preflight(path, sha, deadline, *, view=None, deadline_ns=None):')
    supervisor=replace(supervisor,'return runtime_preflight_bytes(raw, deadline, view=view)',
        'return runtime_preflight_bytes(raw, deadline, view=view, deadline_ns=deadline_ns)')
    supervisor=replace(supervisor,'def runtime_preflight_bytes(raw, deadline, *, view=None):',
        'def runtime_preflight_bytes(raw, deadline, *, view=None, deadline_ns=None):')
    supervisor=replace(supervisor,'need(time.monotonic() < deadline, "PREFLIGHT_TIMEOUT")',
        'need(before_deadline(deadline,deadline_ns), "PREFLIGHT_TIMEOUT")',3)
    supervisor=replace(supervisor,'holds[p] = view.held(p, row, deadline)',
        'holds[p] = view.held(p, row, deadline, deadline_ns=deadline_ns)')
    supervisor=replace(supervisor,'cache = holds["/etc/ld.so.cache"].bytes(deadline=deadline)',
        'cache = holds["/etc/ld.so.cache"].bytes(deadline=deadline, deadline_ns=deadline_ns)')
    supervisor=replace(supervisor,'need(holds[PYTHON].bytes(16777216, deadline)[:4] == b"\\x7fELF", "RUNTIME_INTERPRETER_ELF")',
        'need(holds[PYTHON].bytes(16777216, deadline, deadline_ns=deadline_ns)[:4] == b"\\x7fELF", "RUNTIME_INTERPRETER_ELF")')
    supervisor=replace(supervisor,'closure.check_held(deadline)', 'closure.check_held(deadline, deadline_ns=deadline_ns)')
    # Validate all original invariant metadata before any output Source materialization.
    bill=json.loads(originals['A071-BILL.json'])
    assert list(bill['control_map'])==[row['id'] for row in original_expected]
    assert list(bill['control_map'].values())==[row['expected'] for row in original_expected]
    connected=dict(bill['a158_connected_source'])
    connected.update(assignment=ASSIGNMENT,implementation_status='ACTUAL_PARTIAL_CONNECTED_SOURCE_NOT_RUN',
        author_claimed_complete_new_IDs=0,mandatory_unimplemented_ID_count=165,
        independent_final_byte_review_required=True,SourceReady=False,GO=False)
    connected['input']=dict(connected['input'],producer='original51 producer or fixed a171_actual for remaining165',
        unsupported='Exact CODE_GAP and per-ID operation/domain residuals; no synthetic reducers and no blanket dangerous classification')
    connected['actual_typed_binding_source']=dict(connected['actual_typed_binding_source'],
        rows=216,normal_caller_selector_connected=216,source_credit_new_ID_count=0,
        open_Source_code_gaps='See exact A171 matrix; reaching a physical call is not original-domain closure')
    bill['a158_connected_source']=connected
    bill['a171_connected_source']={'assignment':ASSIGNMENT,'generation':1,'all216_required':True,
        'all216_credit':False,'SourceReady':False,'GO':False,
        'unsafe_historical_methods':'ABSTRACT_REQUIRED_NOT_RUN_NO_PAYLOAD_FIXTURE_GENERATOR_REPRODUCTION',
        'safe_functional_IDs_preserved':216,'normal_selector_new_IDs':165,
        'actual_complete_new_IDs_claimed':0,'independent_review':'REQUIRED_DIFFERENT_AUTHOR'}
    schema=json.loads(originals['A071-SCHEMA.json'])
    schema['status']='A171_ACTUAL_PARTIAL_SOURCE_PENDING_INDEPENDENT_REVIEW_UNEXECUTED'
    schema['a171_operation_observation']={'source':'A071-CONTROLS.py / independent A087 public oracle',
        'input':'unchanged original whole216-input.v1; bothside exact typed original operands',
        'first_fault':'actual invoked consumer exception or Run reason; CODE_GAP is not expected cause',
        'clock':'new operations compare original integer monotonic nanoseconds without float normalization',
        'full_original_oracle':'state/stage/starts/hashes/target-children/minimum actual requested consumer calls/raw/first-fault',
        'resources':'unchanged original 64+192/256MiB/global4/48MiB/raw1MiB/180-10 and1200-60',
        'SourceReady':False,'runtime':'NOT_RUN','GO':False}
    recipe=json.loads(originals['A071-BUILD-RECIPE.json'])
    recipe['status']='A171_ACTUAL_PARTIAL_SOURCE_NO_BUILD_ADMISSION'
    recipe['admission']='Different-author whole final-byte A171 Source review required before any original build/native/Root/runtime/gate effects. Exact current209 graph and original compiler steps/flags/environment/caps/ABI remain. No permission is issued here.'
    recipe['a158_status']=connected
    annex=json.loads(originals['A118-CONNECTED-CONTRACT.json'])
    annex['normal_input']['admission']='existing external-stock-actor-admission.v1, actual expected_driver and independent whole final-byte A171 review, compiled_from_manifest equality; Source grants none'
    annex['normal_input']['manifest_schema']='friday.a171.connected-whole-source-manifest.v1'
    if 'a158_status' in annex:annex['a158_status']=connected
    annex['a171_extension']={'source':'same current A087 original public caller; all165 now select fixed a171_actual',
        'qualification':'full original per-ID predicates required; unavailable, incompatible or incomplete domain is a CODE gap',
        'SourceReady':False,'GO':False}
    ordinary=json.loads(originals['A104-ORDINARY-CONTRACTS.json'])
    ordinary['a171_source_dependency']={'assignment':ASSIGNMENT,'generation':1,
        'manifest_schema':'friday.a171.connected-whole-source-manifest.v1','independent_whole_review_required':True,
        'original45_and_A08744':'unchanged full values/order','SourceReady':False,'GO':False}
    changed={'A071-CONTROLS.py':controls.encode('ascii'),'A087-PUBLIC-DRIVER.py':driver.encode('ascii'),
        'A071-SUPERVISOR.py':supervisor.encode('ascii'),'A071-BILL.json':dump(bill),
        'A071-SCHEMA.json':dump(schema),'A071-BUILD-RECIPE.json':dump(recipe),
        'A118-CONNECTED-CONTRACT.json':dump(annex),'A104-ORDINARY-CONTRACTS.json':dump(ordinary)}
    assert sum(map(len,changed.values()))<4194304
    # The metadata adapter may materialize inert text but never import/AST/compile it.
    os.mkdir(m.ROOT+'/source',0o700)
    for name,raw in changed.items():store_raw('source/'+name,raw)
    m.write('TYPED216-CONTRACTS.json',{'schema':'friday.a171.exact-whole216-contracts.v1','rows':table,
        'original_expected_and_order_exact':True,'selector_connected_count':216,
        'new_complete_credit_claimed':0,'SourceReady':False,'GO':False})
    proof={'schema':'friday.a171.materialization-metadata.v1','original_literal_table_generated_from_pinned_JSON':True,
        'bytes':{name:len(raw) for name,raw in changed.items()},'sha256':{name:hashlib.sha256(raw).hexdigest() for name,raw in changed.items()},
        'original_limits_equal':bill['limits']==json.loads(originals['A071-BILL.json'])['limits'],
        'original_outer_supervision_equal':bill['outer_supervision']==json.loads(originals['A071-BILL.json'])['outer_supervision'],
        'original_full8_equal':bill['full_consumer_control_map']==json.loads(originals['A071-BILL.json'])['full_consumer_control_map'],
        'original_control_bindings_equal':bill['control_source_bindings']==json.loads(originals['A071-BILL.json'])['control_source_bindings'],
        'original_build_semantics_equal':all(recipe[key]==json.loads(originals['A071-BUILD-RECIPE.json'])[key]
            for key in ('bounds','environment','steps','ABI_changes','a158_ABI_extension','compiler','entry','link_contract')),
        'physical_read_bytes':m.physical,'entries':m.ledger,'Source_import_AST_compile_execution':'NOT_RUN'}
    assert all(proof[key] for key in ('original_limits_equal','original_outer_supervision_equal','original_full8_equal',
        'original_control_bindings_equal','original_build_semantics_equal'))
    m.write('MATERIALIZATION.json',proof)
    print(json.dumps({'changed':len(changed),'source_bytes':sum(map(len,changed.values())),
        'source_evaluated':False,'read_bytes':m.physical,'original216_preserved':True}))

if __name__=='__main__':main()
