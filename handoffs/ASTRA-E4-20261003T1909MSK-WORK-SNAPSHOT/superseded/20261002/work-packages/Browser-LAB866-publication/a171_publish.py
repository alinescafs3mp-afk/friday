"""Author-owned bounded stock metadata; never imports or parses supplied Source."""
import os
import sys
import json
import hashlib
import stat
import resource
from datetime import datetime, timezone, timedelta
sys.path.insert(0,'/var/tmp/friday-astra-lab858-sol057-whole209-all216-actual-source-implementation-a171-g1')
import a171_metadata as m

ASSIGNMENT='ASTRA-E4-LAB858-SOL057-WHOLE209-ALL216-ACTUAL-SOURCE-IMPLEMENTATION-A171'
OLD='/var/tmp/friday-astra-browser-a153-a156-whole216-all6-all2-connected-source-implementation-a158-g1'
LAB='/var/tmp/friday-lab858-a158-whole216-all6-remaining-connected-source-implementation'
REV='/var/tmp/friday-sol057-lab858-whole209-all216-independent-source-review'
TERMINAL='/home/jericho/.jericho/runtime/subagent-lifecycle/'+ASSIGNMENT+'-G1-RESULT.json'
ACCEPTED='2026-10-02 10:53:10 MSK'
MSK=timezone(timedelta(hours=3))
CHANGED=('A071-CONTROLS.py','A087-PUBLIC-DRIVER.py','A071-SUPERVISOR.py','A071-BILL.json',
         'A071-SCHEMA.json','A071-BUILD-RECIPE.json','A118-CONNECTED-CONTRACT.json','A104-ORDINARY-CONTRACTS.json')
# This is a static implementation-candidate set, not passing or admitted cases.
CANDIDATES={28,29,66,67,68,69,71,72,73,74,75,76,77,78,79,80,81,82,83,85,89,92,93,
            119,136,152,153,156,158,163,164,*range(171,201),212,213,214,215}

def raw_store(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        pos=0
        while pos<len(raw):pos+=os.write(fd,raw[pos:])
        os.fsync(fd)
    finally:os.close(fd)

def pin(path):return m.read(path)[1]

def text_location(body,needle):
    at=body.find(needle.encode('ascii'))
    if at<0:raise ValueError('missing textual anchor '+needle)
    return {'byte_offset':at,'line':body[:at].count(b'\n')+1,'anchor':needle,
            'method':'bounded literal byte search, not AST or supplied execution'}

def gap_for(row):
    p=row['position'];e=row['expected'];consumer=e['consumer']
    if e['owned_children']:
        return ['ORIGINAL_COORDINATOR_TARGET_CHILD_ROUTE_NOT_CONNECTED',
                'EXTRA_PERFORMING_PRODUCER_CANNOT_BE_COUNTED_AS_ORIGINAL_TARGET_WORKER',
                'NO_NATIVE_AUTHORITY_ROLE_SESSION_RESET_OR_PROCESS_CAP_RAISE_GRANTED']
    if 94<=p<=100:
        return ['G1_IMMUTABLE_LIVE_256MiB_HEADROOM_VS_ORIGINAL192MiB_LEAF',
                'ORIGINAL_PER_ID_RESOURCE_CONTROL_DOMAIN_AND_FIRST_FAULT_NOT_CONNECTED',
                'UNAVOIDABLE_LEAF_SHORTFALL_IS_NOT_SELECTED_NEGATIVE_CONTROL_CREDIT']
    if 202<=p<=211:
        return ['ORIGINAL_PER_ID_RESOURCE_KERNEL_DOMAIN_NOT_CONNECTED',
                'ACTUAL_LIVE_CALL_IS_NOT_OLD_SUBSTITUTED_IO_OR_SELECTED_NEGATIVE_CONTROL_PROOF']
    if p==54:return ['ORIGINAL_EXECUTE_CORE_ABORT_STATE_NOT_CONNECTED_TO_ACTUAL_RETAINED_NEGATIVE']
    if p==84:return ['ORIGINAL_SINGLE_SPARSE_FILE2147483649_EXCEEDS_ORIGINAL_RLIMIT_FSIZE2147483648',
                    'DO_NOT_SPLIT_OR_REWRITE_SCENARIO_RAISE_CAP_OR_REPLACE_FIRST_FAULT']
    if p in (56,90,91,115,116,122,123,201):
        return ['ORIGINAL_DEADLINE_PHASE_OR_CONTROLLED_FAULT_NOT_CONNECTED',
                'ORIGINAL_CAPSULE_ENDS_RETAINED_NO_FAKE_CLOCK_REFRESH_OR_EXPIRED_PARKING']
    if p in (117,118):return ['ORIGINAL_SERIALIZER_FAILURE_AND_STICKY_UNKNOWN_DOMAIN_NOT_CONNECTED',
                             'JSON_ONLY_TYPED_INPUT_IS_NOT_A_FAILING_SERIALIZER_CALLBACK']
    if p in (133,134,135,137,154,155):
        return ['ORIGINAL_PARTIAL_ZERO_EIO_WRITE_DOMAIN_NOT_CONNECTED',
                'NO_FABRICATED_WRITE_RETURN_ERRNO_OR_SUCCESS_FLAG']
    if p==63:return ['ORIGINAL_OWNER_STOP_CONTROL_DOMAIN_NOT_CONNECTED']
    if p==64:return ['ORIGINAL_CLEANUP_FAILURE_DOMAIN_NOT_CONNECTED_NO_UNKNOWN_CLOSE_RETRY']
    return ['ORIGINAL_OPERATION_PHASE_OR_APPROVED_DOMAIN_NOT_CONNECTED']

def domain(row):
    e=row['expected'];c=e['consumer'];p=row['position']
    if e['owned_children']:return 'original coordinator with actual target children; no extra delegated producer'
    if c=='sealed_bytes':return 'Root-selected original private owned file, actual no-follow fd custody/read/hash'
    if c=='RetainedTree.check':return 'existing original316-file13-directory owned inventory; physical post-admission mutations'
    if c.startswith('BoundedOutput/G1.Output'):return 'actual original six-file three-directory bounded owned output'
    if c.startswith('ca_context/'):return 'Root-selected owned inert CA bytes, actual guarded digest and local SSL parser; no network'
    if c=='R4.guarded_digest':return 'actual owned input fd/cap/size/hash; no digest-matches flag'
    if c=='Supervisor.runtime_preflight':return 'original approved existing private filesystem view; full real held files/stdlib/cache/ELF consumers'
    if c=='Supervisor.cgroup_values':return 'original owned control-text file I/O; no claim of kernel cgroup authority'
    if c=='Supervisor.HeldSource.bytes':return 'actual captured sealed memfd, original path replacement or EPERM control'
    if c=='Supervisor.NativeLaunchAdapter':return 'actual legacy constructor Root-custody refusal only; no handoff or native execution'
    if c=='Supervisor.seal':return 'actual original unknown-pin consumer refusal before path open'
    if p in (119,136,156):return 'actual owned memfd rejection or bounded nonblocking pipe saturation/drain; no writer-result seam'
    if c in ('G1.resources','acquisition_resources'):return 'actual immutable live resource consumer; original selected resource-control domain unresolved'
    return 'original fixed input/consumer route or exact unresolved original phase'

def reports():
    # Continue the exact author-metadata checkpoint after its local JSON-key
    # defect, without overwriting any Source or previously published proof.
    original_write=m.write
    checkpoint_names={'SOURCE-REFERENCE-MANIFEST.json','CURRENT-SOURCE209.json','EQUAL-COMPLEMENT.json','DELTA-RAW-GAPLESS.json'}
    def checkpoint_write(name,data):
        if name in checkpoint_names and os.path.exists(m.ROOT+'/'+name):
            expected_raw=(json.dumps(data,ensure_ascii=True,indent=2)+'\n').encode('ascii')
            assert m.read(m.ROOT+'/'+name)[0]==expected_raw
            return
        original_write(name,data)
    m.write=checkpoint_write
    original=m.load(OLD+'/SOURCE-REFERENCE-MANIFEST.json')
    input_data=m.load(m.INPUT,{'bytes':181649,'sha256':'e830b090eaf26001e000842f3ee2d8c1a8e407cd1779812d74f046a93fc2eaaf'})
    typed=m.load(m.ROOT+'/TYPED216-CONTRACTS.json')
    expected=m.load(m.ROOT+'/ORIGINAL216-EXPECTATIONS.json')['rows']
    safety=m.load(m.ROOT+'/ORIGINAL29-SAFETY-REQUIREMENTS.json')['rows']
    safety_positions={r['position'] for r in safety}
    assert len(original['members'])==209 and len(expected)==216 and len(safety_positions)==29
    source_bodies={name:m.read(m.ROOT+'/source/'+name)[0] for name in CHANGED}
    controls=source_bodies['A071-CONTROLS.py'];driver=source_bodies['A087-PUBLIC-DRIVER.py']
    assert controls.count(b'WHOLE216_TABLE=')==1
    table_line=b'WHOLE216_TABLE='+repr(typed['rows']).encode('ascii')+b'\n'
    assert table_line in controls
    assert all(t['position']==r['position'] and t['id']==r['id'] and t['expected']==r['expected']
               for t,r in zip(typed['rows'],expected))
    # Whole validators and the typed operands on both independent consumers match as
    # raw text. This is NOT Python syntax checking, compilation or execution.
    validator_prefix=b'def a171_validate_observation('
    validators=[body[body.index(validator_prefix):].rstrip() for body in (controls,driver)]
    assert validators[0]==validators[1]
    input_prefix=b'def a171_validate_input('
    input_end=b'\n# A171 inert Source text.'
    left=controls[controls.index(input_prefix):controls.index(input_end)].rstrip()
    right=driver[driver.index(input_prefix):driver.index(validator_prefix)].rstrip()
    # The driver contains only this validator followed by the independent oracle.
    assert left==right
    for required in (b'ORIGINAL_G1_RESOURCE_CONTROL_DOMAIN_UNRESOLVED',
                     b'ORIGINAL_ACQUISITION_RESOURCE_CONTROL_DOMAIN_UNRESOLVED',
                     b'ORIGINAL_COORDINATOR_TARGET_CHILD_ROUTE_REQUIRED',
                     b'A171_MANDATORY_CODE_GAP_NOT_A_PASS',b'A171_ACTUAL_MONOTONIC_PHASE_ORDER'):
        assert required in controls
    for name in ('A071-CONTRACT.py','A071-EXECUTOR.py','A071-BOOTSTRAP.c','A071-OWNED.c',
                 'A071-FULL-CONTROLS.py','A079-NATIVE-PUBLIC-DRIVER.py'):
        item=next(item for item in original['members'] if item['name']==name)
        m.read(item['pin']['path'],item['pin'])
    helper_pins={}
    for suffix,sha in (('', 'ba70d584ab4b145195283456b17d98a8ff129a8f7bb3be3595c711fe97d6c223'),
                      ('-R4','a68f62ce16883f108992a8f237b9614d964ff0141d9002b5e714fdc2887b414c')):
        path='/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-MATERIAL-ACQUISITION-EXECUTOR-A025-G1'+suffix+'.py'
        helper_pins['G1' if not suffix else 'R4']=m.read(path,{'sha256':sha})[1]
    current=[];equal=[];deltas=[]
    for member in original['members']:
        name=member['name'];old_pin=member['pin']
        if name=='A071-CONTROLS.py':
            # Effective immediate predecessor is LAB858's one-member overlay.
            old_raw,old_pin=m.read(LAB+'/source/'+name,{'bytes':190485,
                'sha256':'a46c7e64211cd1c7157a8a83a099cd6dfb4f5ca2511acffa36f2d899a8e3c4b8'})
        else:old_raw,old_pin=m.read(old_pin['path'],old_pin)
        if name in CHANGED:
            new_raw,new_pin=m.read(m.ROOT+'/source/'+name)
            assert old_raw!=new_raw
            prefix=0
            while prefix<min(len(old_raw),len(new_raw)) and old_raw[prefix]==new_raw[prefix]:prefix+=1
            suffix=0
            while suffix<min(len(old_raw),len(new_raw))-prefix and old_raw[-suffix-1]==new_raw[-suffix-1]:suffix+=1
            old_end=len(old_raw)-suffix;new_end=len(new_raw)-suffix
            spans=[]
            if prefix:spans.append({'kind':'EQUAL','old':[0,prefix],'new':[0,prefix],
                'raw_hex':old_raw[:prefix].hex(),'sha256':hashlib.sha256(old_raw[:prefix]).hexdigest()})
            spans.append({'kind':'REPLACE','old':[prefix,old_end],'new':[prefix,new_end],
                'old_raw_hex':old_raw[prefix:old_end].hex(),'new_raw_hex':new_raw[prefix:new_end].hex(),
                'old_sha256':hashlib.sha256(old_raw[prefix:old_end]).hexdigest(),
                'new_sha256':hashlib.sha256(new_raw[prefix:new_end]).hexdigest()})
            if suffix:spans.append({'kind':'EQUAL','old':[old_end,len(old_raw)],'new':[new_end,len(new_raw)],
                'raw_hex':old_raw[old_end:].hex(),'sha256':hashlib.sha256(old_raw[old_end:]).hexdigest()})
            rebuilt_old=b''.join(bytes.fromhex(s['old_raw_hex'] if s['kind']=='REPLACE' else s['raw_hex']) for s in spans)
            rebuilt_new=b''.join(bytes.fromhex(s['new_raw_hex'] if s['kind']=='REPLACE' else s['raw_hex']) for s in spans)
            assert rebuilt_old==old_raw and rebuilt_new==new_raw
            assert spans[0]['old'][0]==spans[0]['new'][0]==0
            assert all(a['old'][1]==b['old'][0] and a['new'][1]==b['new'][0] for a,b in zip(spans,spans[1:]))
            deltas.append({'name':name,'old_pin':old_pin,'A158_base_pin':member['pin'],'new_pin':new_pin,
                'full_both_sides_gapless':True,'both_reconstructions_sha_verified':True,'spans':spans})
            disposition='CHANGED_ACTUAL_SOURCE_PENDING_DIFFERENT_AUTHOR_REVIEW'
        else:
            new_pin=old_pin;disposition='UNCHANGED_CURRENT_SOURCE_EXACT_FULL_SHA9'
            equal.append({'name':name,'old_pin':old_pin,'new_pin':new_pin,'full_bytes_equal':True})
        current.append({'name':name,'pin':new_pin,'predecessor_pin':old_pin,'A158_lineage_predecessor_pin':member.get('predecessor_pin'),
            'disposition':disposition,'Source_execution':'NOT_RUN','SourceReady':False,'GO':False})
    assert len(current)==209 and len(equal)==201 and len(deltas)==8
    m.write('SOURCE-REFERENCE-MANIFEST.json',{'schema':'friday.a171.current209-source-reference.v1',
        'assignment':ASSIGNMENT,'generation':1,'members':current,'count':209,'changed_count':8,'equal_count':201,
        'effective_predecessor':'A158 current209 plus exact LAB858 one-member Controls overlay',
        'SourceReady':False,'source_execution_admission':False,'runtime':'NOT_RUN','GO':False})
    m.write('CURRENT-SOURCE209.json',{'count':209,'members':current,'final_actual_source_bytes_not_initial_materialization':True})
    m.write('EQUAL-COMPLEMENT.json',{'count':201,'entries':equal,'full_raw_and_SHA9_checked':True})
    m.write('DELTA-RAW-GAPLESS.json',{'count':8,'base':'effective LAB858 current209','entries':deltas,
        'gapless_old_and_new_full_bodies':True,'dangerous_historical_source_methods_reproduced':False})
    byname={r['name']:r['pin'] for r in current}
    map_before=m.load(next(item['pin']['path'] for item in original['members'] if item['name']=='A071-BILL.json'))
    map_after=json.loads(source_bodies['A071-BILL.json'])
    assert list(map_before['control_map'])==list(map_after['control_map'])==[r['id'] for r in expected]
    assert map_before['control_map']==map_after['control_map']
    assert map_before['limits']==map_after['limits']
    assert map_before['helper_source_pins']==map_after['helper_source_pins']
    old_build=m.load(next(item['pin']['path'] for item in original['members'] if item['name']=='A071-BUILD-RECIPE.json'))
    new_build=json.loads(source_bodies['A071-BUILD-RECIPE.json'])
    build_semantics=('schema','allowed_output','compiler','entry','bounds','environment','steps','link_contract',
                     'post_build_required','cleanup','performed_now','ABI_changes','a158_ABI_extension')
    old_fields={k:old_build[k] for k in build_semantics}
    new_fields={k:new_build[k] for k in build_semantics}
    assert old_fields==new_fields
    records=[];counts={'existing_genuine_routes_retained_unexecuted':0,'new_actual_operation_candidates_unreviewed':0,'mandatory_specific_code_residuals':0}
    for r,t in zip(expected,typed['rows']):
        p=r['position'];e=r['expected'];existing=r['prior_review'].startswith('GENUINE_FIXED_CONSUMER_SOURCE_ROUTE')
        if existing:category='EXISTING_GENUINE_SOURCE_PRIMITIVE_ROUTE_RETAINED_NOT_RERUN';counts['existing_genuine_routes_retained_unexecuted']+=1
        elif p in CANDIDATES:category='NEW_ACTUAL_CONSUMER_SOURCE_CANDIDATE_NOT_REVIEWED_NOT_RUN';counts['new_actual_operation_candidates_unreviewed']+=1
        else:category='MANDATORY_SPECIFIC_SOURCE_CODE_GAP';counts['mandatory_specific_code_residuals']+=1
        residual=[] if existing else (['DIFFERENT_AUTHOR_FINAL_BYTE_SOURCE_REVIEW_REQUIRED',
            'ACTUAL_ROOT_SELECTED_ORIGINAL_INPUT_PRODUCER_AND_RUNTIME_CUSTODY_NOT_EXECUTED',
            'FULL_FIRST_FAULT_BODY_PHASE_RESOURCE_ORACLE_NOT_YET_OBSERVED'] if p in CANDIDATES else gap_for(r))
        records.append({'position':p,'id':r['id'],'original_expected':e,'original_binding':r['binding'],
            'prior_review':r['prior_review'],'classification':category,'domain_safety29_member':p in safety_positions,
            'current_selector':t['producer'],'original_caller':'A087-PUBLIC-DRIVER.prepare_whole216 -> Root-selected native mode1 -> Controls.whole216_input -> whole216_producer -> actual operation -> Controls full oracle -> A087 full independent oracle',
            'current_Source_pins':{key:byname[key] for key in ('A087-PUBLIC-DRIVER.py','A071-CONTROLS.py','A071-SUPERVISOR.py','A071-BILL.json')},
            'actual_operation_domain':domain(r),'first_fault':'actual exception or original consumer run/trace reason; never expected cause copied into result',
            'state':'actual consumer result/class or original terminal state; original expected state remains independent; gaps refuse',
            'body_and_hash':'original native stdout/stderr full raw retained before semantic interpretation; new operation actual bounded raw bytes/hash when available; original 32768 typed producer and1MiB raw caps unchanged',
            'phase':'actual monotonic_ns decimal timestamps plus original stage/call counters; integer capsule ends retained',
            'resource':'actual original production_resources before operation; all original caps unchanged; original domain contradictions never credited',
            'custody':'actual native original producer wait/FINISH/release + auxiliary fd records one close attempt; dependency exceptional cleanup remains unexecuted',
            'residuals':residual,'observed':'NOT_RUN','qualification_credit_new':0,'SourceReady':False,'Root_admission':False,'GO':False})
    assert counts['existing_genuine_routes_retained_unexecuted']==51
    assert counts['new_actual_operation_candidates_unreviewed']==65
    assert counts['mandatory_specific_code_residuals']==100
    m.write('WHOLE216-ACTUAL-SOURCE-MATRIX.json',{'count':216,'original_order_and_all_fields_exact':True,
        'counts':counts,'remaining165_original_ordinary136_and_exact29':{'ordinary':136,'exact29':29},
        'new_qualified_cases':0,'all216_complete':False,'rows':records})
    safety_out=[]
    for historical in safety:
        r=records[historical['position']]
        safety_out.append({'position':r['position'],'id':r['id'],'original_expected':r['original_expected'],
            'classification':'BENIGN_REQUIRED_FUNCTIONAL_OBLIGATION_NOT_UNSAFE_BY_CONSUMER_NAME',
            'exact_original_review_evidence':historical['textual_evidence'],
            'safe_method_status':r['classification'],'actual_safe_domain':r['actual_operation_domain'],
            'residuals':r['residuals'],'historical_dangerous_mechanisms':'ABSTRACT_REQUIRED_NOT_RUN_NO_PAYLOAD_FIXTURE_GENERATOR_OR_OPERATIONAL_REPRODUCTION',
            'no_waiver':True,'no_network_exec_original_write':True,'reviewed_new_author_source':False,'runtime':'NOT_RUN'})
    m.write('EXACT29-SAFETY-MATRIX.json',{'count':29,'rows':safety_out,'safe_functional_obligations_waived':0,
        'dangerous_operational_method_authorization':False,'new_actual_CA_local_parse_Source_candidate':1,
        'original_target_child_domains_still_mandatory_specific_code_gap':28,'new_qualified_cases':0})
    causes=[
      {'id':'SOL057-1','cause':'normal public caller/table/typed-producer missing165','attempt':'all216 literal table now active; original51 fixed routes, exact original remaining consumer allowlist -> a171_actual; same canonical Root119 data; both input validators equal text; selected unsupported domains explicitly refuse',
       'code_locations':{'caller':text_location(driver,'def whole216_producer_for('),'typed_input':text_location(controls,'def a171_validate_input('),'producer':text_location(controls,'def whole216_producer(')},'status':'ACTUAL_DISPATCH_SOURCE_WRITTEN_UNREVIEWED_NOT_RUN'},
      {'id':'SOL057-2','cause':'synthetic flag reducers/no original operation or owned body/custody','attempt':'LAB858 reducers removed by rebuilding from A158 actual51; actual original x/s/G1/R4 calls with Root-selected owned files, fd/tree/output/CA/runtime/cgroup/held/sink domains; no sha-matches/target-exists/result-flags reducers',
       'code_locations':{'operations':text_location(controls,'def a171_perform('),'fault_calls':text_location(controls,'def a171_called('),'fd_cleanup':text_location(controls,'def a171_close(')},'status':'65_NEW_ACTUAL_DOMAIN_CANDIDATES_100_SPECIFIC_CODE_RESIDUALS_NEW_CREDIT0'},
      {'id':'SOL057-3','cause':'29 names treated as unsafe and waived','attempt':'exact29 per-ID independent review retained; all benign functional obligations mandatory; one actual local CA parser route written;28 original target child domains exact CODE residuals; genuinely dangerous historical mechanisms abstract only',
       'status':'NAME_BASED_WAIVER_REMOVED_NO_OBLIGATION_CLOSED_BY_ABSTRACTION'},
      {'id':'SOL057-4','cause':'actual resource first-fault and256vs192 contradictions','attempt':'immutable G1 actual resource call retained with physically original observer; mandatory resource control gap marker prevents expected incidental leaf shortfall becoming pass; original production_resources/caps and full oracle retained',
       'status':'HONEST_SPECIFIC_CODE_GAP_NO_RESOURCE_DOMAIN_RESOLUTION_GRANTED'},
      {'id':'SOL057-5','cause':'full original oracle and coordinator+3 target children versus producer count/authority','attempt':'both separate original controller and external A087 now validate original state/stage/starts/hash cardinality/consumer calls/target child count/actual first fault/full raw hash/phase/custody; no expected state/child count assignment; native mode1 still coordinator+one selected producer; original three-target operation refused instead of extra child or authority reset',
       'code_locations':{'full_oracle':text_location(controls,'def a171_validate_observation('),'target_gap':text_location(controls,'if spec["expected"]["owned_children"]:')},
       'status':'FULL_ORACLE_SOURCE_WRITTEN_ORIGINAL_TARGET_TOPOLOGY_MANDATORY_CODE_GAP'}]
    m.write('ALL-FIVE-CONNECTED-CAUSES.json',{'causes':causes,'all_five_attempted_together':True,
        'complete_resolution':False,'new_source_gate_permission':False})
    preserved=m.load(REV+'/all2-and-preserved-scope.json')
    graph=m.load(REV+'/connected-consumer-owner-resource-graph.json')
    resources=m.load(REV+'/resource-custody-cost-review.json')
    m.write('PRESERVED-ALL2-AND-ORIGINAL-SCOPE.json',{'original_independent_review':preserved,
        'actual_FINAL_invariant_checks':{'original216_values_and_order':True,'original_bill_limits':True,
            'original_G1_R4_SHA_pins':True,'original_build_steps_flags_env_bounds_ABI':True,
            'A071_FULL_CONTROLS_and_A079_DRIVER_byte_equal':True,'original_native_bootstrap_owned_byte_equal':True},
        'helper_pins':helper_pins,'Source_execution':'NOT_RUN','old_current51_source_credits_not_new_runtime_credit':True})
    m.write('OWNER-RESOURCE-CUSTODY-GRAPH.json',{'original_graph':graph,'original_resource_review':resources,
        'current_Source':{'caller':byname['A087-PUBLIC-DRIVER.py'],'performer':byname['A071-CONTROLS.py'],
            'actual_dependencies':[byname[n] for n in ('A071-EXECUTOR.py','A071-SUPERVISOR.py','A071-CONTRACT.py','A071-BOOTSTRAP.c','A071-OWNED.c')],
            'helper_pins':helper_pins},
        'original_product_caps':map_before['limits'],'assignment_resources':input_data['resources'],'gap_proofs':{
          'G1_leaf':'201326592 - nonnegative actual memory.current < 268435456; original G1.resources demand unchanged. Later ancestor/disk/host failures cannot be selected after unavoidable leaf failure.',
          'global_processes':'coordinator1 + delegated producer1 + original target workers3 =5 > original inner/global4. Current original Root fr_controller requests permit only selected producer. Do not reinterpret producer as target or grant child session reset.',
          'single_output_file':'original output_disk one sparse2147483649 file > original RLIMIT_FSIZE2147483648; honest exact CODE gap, no two-file replacement or cap raise',
          'typed_full_raw':'original producer frame32768 and raw per-stream1048576 unchanged; physical observed operation prefix/input/inventory may exceed frame. Full raw is never silently omitted/hash-only substituted; final runtime envelope remains unproven.',
          'exceptional_dependency_cleanup':'original RetainedTree/BoundedOutput/RuntimeClosure exceptional close behavior not executed or declared independently qualified by Source text; no retry unknown close.'},
        'new_role_authority':False,'native_Root_process_changes':False,'runtime':'NOT_RUN'})
    m.write('WHOLE209-CLASSIFICATION.json',{'count':209,'rows':[{'name':r['name'],'pin':r['pin'],
        'changed':r['name'] in CHANGED,'classification':r['disposition'],'functional_closure':'NOT_CLAIMED',
        'independent_review':'REQUIRED_FOR_FINAL_CHANGED_DEPENDENCIES','runtime':'NOT_RUN'} for r in current],
        'changed8_equal201':True,'complete_whole_source_reference_not_a_release':True})
    m.write('FINAL-TEXTUAL-INVARIANTS.json',{'schema':'friday.a171.literal-byte-JSON-invariants.v1',
        'supplied_AST_import_compile_eval_exec_tests':False,'both_full_oracle_text_identical':True,
        'both_typed_operand_validator_text_identical':True,'literal_table_is_exact_pinned216_JSON_literal':True,
        'all_original_expected_fields_preserved':True,'old65_candidate_label_not_result_credit':False,
        'new_actual_candidate65_is_not_qualification_credit':True,'new_specific_code_residual100':True,
        'MATERIALIZATION_json':'INITIAL_ASSEMBLY_ONLY; its initial eight SHA pins are stale after subsequent author manual Source edits; CURRENT-SOURCE209 and final manifest alone designate final Source',
        'A171_INPUT_OPERATIONS_templates_and_a171_materialize':'INITIAL_AUTHOR_WORKNOTES_NOT_REPLAYABLE_FINAL_SOURCE_BUILDER; never load supplied Source',
        'python_syntax':'NOT_CHECKED_TASK_PROHIBITS_COMPILE_AST_OR_SUPPLIED_EXECUTION',
        'observed_metadata_helper_peak_self_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'whole_assignment_RAM_implicit_IO_model_cost':'UNKNOWN_NOT_ZERO'})
    m.write('AUTHOR-METADATA-CHECKPOINT-CONTINUATION.json',{'initial_own_metadata_failure':'KeyError helper_pins: original bill exact key is helper_source_pins',
        'effect_before_failure':'only four complete hash9 Source/delta reference JSON artifacts written; no Source imported/evaluated/compiled/executed/tested',
        'continuation':'changed author-owned metadata code; exact previously emitted four JSON bytes verified and reused without overwrite; source snapshot unchanged',
        'supplied_Runtime_native_network_attempts_or_retries':0,'untraced_failed_metadata_reads':'UNKNOWN_WITHIN_CONSERVATIVE_LITERAL_TOOL_READ_RESERVATION_NOT_ZERO'})
    # Exact removal of author metadata bytecode, not any supplied/product artifact.
    cache=m.ROOT+'/__pycache__/a171_metadata.cpython-314.pyc'
    cache_dir=m.ROOT+'/__pycache__'
    if os.path.exists(cache):
        st=os.stat(cache,follow_symlinks=False)
        assert stat.S_ISREG(st.st_mode) and st.st_nlink==1 and os.listdir(cache_dir)==['a171_metadata.cpython-314.pyc']
        cached_pin=m.read(cache)[1]
        os.unlink(cache);os.rmdir(cache_dir)
        m.write('AUTHOR-METADATA-CACHE-CLEANUP.json',{'removed_exact_owned_cache':cached_pin,
            'reason':'author stock metadata-only module generated pyc under first invocation; no supplied Source imported or compiled',
            'recoverable':'from author metadata text if authorized; no product/user artifact removed'})
    m.write('REPORTS-READ-LEDGER.json',{'physical_read_bytes':m.physical,'entries':m.ledger,
        'scope':'this bounded stock metadata report invocation; earlier reads and final seal accounted separately',
        'supplied_Source_evaluated':False})
    print(json.dumps({'reports':'written','current209':len(current),'changed':8,'equal':201,'counts':counts,
                      'Source_executed':False,'physical_read_bytes':m.physical}))

def file_set():
    files=[];dirs=[]
    for directory,nested,names in os.walk(m.ROOT,followlinks=False):
        st=os.stat(directory,follow_symlinks=False)
        assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode)==0o700 and st.st_uid==st.st_gid==1000
        dirs.append(os.path.relpath(directory,m.ROOT))
        for name in sorted(names):
            path=directory+'/'+name;st=os.stat(path,follow_symlinks=False)
            assert stat.S_ISREG(st.st_mode) and stat.S_IMODE(st.st_mode)==0o600 and st.st_nlink==1 and st.st_uid==st.st_gid==1000
            files.append(os.path.relpath(path,m.ROOT))
    return sorted(files),sorted(dirs)

def seal():
    before_files,before_dirs=file_set()
    for forbidden in ('SEAL.json','MANIFEST.json'):
        assert forbidden not in before_files
    source=m.load(m.ROOT+'/SOURCE-REFERENCE-MANIFEST.json')
    for member in source['members']:m.read(member['pin']['path'],member['pin'])
    leaf_pins=[pin(m.ROOT+'/'+name) for name in before_files]
    path_set=before_files+['SEAL.json','MANIFEST.json']
    path_set.sort()
    m.write('SEAL.json',{'schema':'friday.a171.final-leaf-seal.v1','assignment':ASSIGNMENT,'generation':1,
        'leaf_pins':leaf_pins,'expected_exact_output_relative_file_pathset':path_set,
        'expected_directory_pathset':before_dirs,'owner_uid_gid':[1000,1000],'directory_mode':'0700',
        'regular_file_mode':'0600','regular_file_nlink':1,'symlinks':0,'exact_decimal_identity9':True,
        'acyclic':'leaves -> SEAL -> MANIFEST -> external terminal; none contains own/later hashes',
        'independent_final_source_review':'REQUIRED_NOT_PERFORMED','source_execution_admission':False,
        'runtime':'NOT_RUN','all216':'REQUIRED_NOT_RUN','SourceReady':False,'GO':False})
    seal_pin=pin(m.ROOT+'/SEAL.json')
    intake=m.load(m.ROOT+'/INTAKE-READ-LEDGER.json')
    assembly=m.load(m.ROOT+'/MATERIALIZATION.json')
    report_ledger=m.load(m.ROOT+'/REPORTS-READ-LEDGER.json')
    traced=intake['physical_read_bytes']+assembly['physical_read_bytes']+report_ledger['physical_read_bytes']+m.physical
    # Conservative allowances cover uninstrumented literal shell/tool reads,
    # apply_patch/source transport, metadata interpreter libraries and final re-read.
    reserves={'literal_shell_tool_reads':67108864,'apply_patch_and_tool_source_transport':33554432,
              'author_stock_interpreter_implicit_library_io':33554432,'final_reverification_allowance':16777216}
    upper=traced+sum(reserves.values())
    assert upper<268435456
    current_bytes=sum(p['bytes'] for p in leaf_pins)+seal_pin['bytes']
    assert current_bytes<16777216
    manifest={'schema':'friday.a171.connected-whole-source-manifest.v1','assignment':ASSIGNMENT,'generation':1,
        'source_reference_manifest_pin':pin(m.ROOT+'/SOURCE-REFERENCE-MANIFEST.json'),'seal_pin':seal_pin,
        'leaf_pins':leaf_pins,'full209':209,'changed_members':8,'equal_members':201,
        'original216':216,'original_remaining165':{'ordinary':136,'exact29':29},
        'Source_counts':{'existing_genuine_source_primitives_retained':51,'new_actual_source_candidates_unreviewed':65,
                         'new_ordinary_actual_source_candidates_unreviewed':64,'new_exact29_actual_source_candidates_unreviewed':1,
                         'mandatory_specific_code_residuals':100,'ordinary_specific_code_residuals':72,
                         'exact29_specific_code_residuals':28,'new_qualified_cases':0},
        'allfive':'COHERENT_SOURCE_ATTEMPT_RESIDUALS_EXPLICIT_NOT_COMPLETE',
        'original_scope_caps_topology_scenarios':'PRESERVED_UNWEAKENED',
        'actual_acceptance_MSK':ACCEPTED,'resource_accounting':{'bounded_assignment_seconds':6600,'seal_reserve_seconds':600,
            'read_cap':268435456,'traced_physical_read_bytes_so_far':traced,'conservative_read_reservations':reserves,
            'conservative_read_upper_bound_bytes':upper,'RAM_cap':8589934592,'output_cap':16777216,
            'output_leaf_and_seal_bytes':current_bytes,'author_metadata_helper_peak_self_KiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'whole_assignment_RAM':'UNKNOWN_NOT_ZERO','implicit_IO_actual_total':'UNKNOWN_WITH_CONSERVATIVE_BOUNDED_RESERVE',
            'model_input_output_cached_reasoning_tokens_dollars':'UNKNOWN_NOT_ZERO','local_parallel_cap':4,
            'model_children':0,'network':0,'supplied_execution':0,'tests_native_Root_runtime_gates':0,'retries':0},
        'source_execution_admission':False,'SourceReady':False,'Root_admission':False,'runtime':'NOT_RUN',
        'all216':'REQUIRED_NOT_RUN','gates':'NOT_RUN','GO':False,'installed':'0.208.58/r5RED_UNCHANGED',
        'frozen':'0.208.64/schema50/cecd28a9_UNCHANGED','acyclic_hash_graph':True,
        'initial_worknotes_are_not_final_Source':'MATERIALIZATION.json/A171-INPUT.txt/A171-OPERATIONS.txt/a171_materialize.py are initial assembly history, not final-byte build replay; use final source directory/ref pins'}
    m.write('MANIFEST.json',manifest)
    manifest_pin=pin(m.ROOT+'/MANIFEST.json')
    actual_files,actual_dirs=file_set()
    assert actual_files==path_set and actual_dirs==before_dirs
    actual_output_bytes=sum(os.stat(m.ROOT+'/'+name,follow_symlinks=False).st_size for name in actual_files)
    assert actual_output_bytes<16777216
    # Fresh noncached full read pins revalidate the final own tree, including all
    # changed Source bodies, after manifest creation. No file mutates after seal.
    m.cached.clear()
    for held in leaf_pins+[seal_pin,manifest_pin]:m.read(held['path'],held)
    finished=datetime.now(MSK)
    accepted=datetime.strptime('2026-10-02 10:53:10','%Y-%m-%d %H:%M:%S').replace(tzinfo=MSK)
    elapsed=int((finished-accepted).total_seconds())
    assert 0<=elapsed<=6600
    finished_text=finished.strftime('%Y-%m-%d %H:%M:%S MSK')
    result={'schema':'friday.a171.actual-source-implementation-result.v1','assignment':ASSIGNMENT,'generation':1,
        'lifecycle':'RESULT_TERMINAL_RETURN_THEN_STOP','verdict':'SOURCE_PARTIAL_COHERENT_ALLFIVE_ATTEMPT100_MANDATORY_CODE_RESIDUALS_NOT_GO',
        'accepted_MSK':ACCEPTED,'finished_MSK':finished_text,'elapsed_seconds':elapsed,
        'input_pin':pin(m.INPUT),'output':m.ROOT,'manifest_pin':manifest_pin,'seal_pin':seal_pin,
        'current209_pin':pin(m.ROOT+'/CURRENT-SOURCE209.json'),'whole216_pin':pin(m.ROOT+'/WHOLE216-ACTUAL-SOURCE-MATRIX.json'),
        'safety29_pin':pin(m.ROOT+'/EXACT29-SAFETY-MATRIX.json'),
        'counts':manifest['Source_counts'],'original216_expectations_exact':True,
        'allfive_connected_attempt':True,'changed8_equal201':True,'exact_output_pathset_verified':True,
        'nlink1_private700600_exact_decimal_SHA9_acyclic':True,'original_all2_and_scopes_caps_preserved':True,
        'new_Source_qualification_credit':0,'whole216_complete':False,
        'mandatory_residuals':'See every exact position/id in WHOLE216-ACTUAL-SOURCE-MATRIX.json; full original165 includes65 actual operation Source candidates awaiting independent review/Root-owned original input execution and100 specific CODE gaps.',
        'principal_code_gaps':['original coordinator+3 target topology vs delegated extra producer/global4',
            'immutable G1.resources256MiB demand vs original192MiB leaf and actual first-fault domains',
            'original selected deadline/fault/owner-stop/cleanup/partial-write control domains',
            'single sparse2147483649 output input vs unchanged RLIMIT_FSIZE2147483648',
            'full original actual custody/raw producer frame constraints not yet observed'],
        'actual_effects':{'new_inert_SourceTEXT_JSON_only':True,'own_trusted_stock_hash9_metadata':True,
            'supplied_import_AST_compile_eval_exec_tests':False,'native_Root_runtime_UI_browser_live_models_gates':False,
            'network':0,'model_children':0,'product_frozen_Git_config_settings_edits':False,
            'deadline_refresh_expired_parking_cap_raise_authority_waiver_unknown_close_retry':False},
        'actual_costs':{**manifest['resource_accounting'],'actual_output_bytes':actual_output_bytes,
            'final_seal_invocation_traced_read_bytes':m.physical,
            'actual_elapsed_seconds':elapsed,'model_token_and_monetary_costs':'UNKNOWN_NOT_ZERO'},
        'different_author_review_required':True,'SourceReady':False,'Root_admission':False,
        'runtime':'NOT_RUN','gates':'NOT_RUN','GO':False,
        'terminal_self_hash':'ABSENT_TO_AVOID_HASH_CYCLE; external recipient must hash9 this exact terminal'}
    raw=(json.dumps(result,ensure_ascii=True,indent=2)+'\n').encode('ascii')
    raw_store(TERMINAL,raw)
    result_pin=pin(TERMINAL)
    print(json.dumps({'terminal_pin':result_pin,'manifest_pin':manifest_pin,'finished_MSK':finished_text,
        'elapsed_seconds':elapsed,'actual_output_bytes':actual_output_bytes,'traced_seal_read_bytes':m.physical,
        'verdict':result['verdict']}))

if __name__=='__main__':
    if sys.argv[1:]==['reports']:reports()
    elif sys.argv[1:]==['seal']:seal()
    else:raise SystemExit('bounded author metadata reports/seal only')
