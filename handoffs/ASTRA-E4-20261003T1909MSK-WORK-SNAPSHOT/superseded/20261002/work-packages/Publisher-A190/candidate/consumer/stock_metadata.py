"""Author-owned stock text/JSON metadata transport. Never loads candidate code."""
import datetime
import difflib
import hashlib
import json
import os
import stat
import subprocess
import sys
import time

ROOT = '/var/tmp/friday-astra-publisher-a122-whole-connected-source-closure-a128-g1'
OLD = '/var/tmp/friday-astra-publisher-a112-whole-source-closure-a117-g1'
INPUT = '/home/jericho/.jericho/grok-takeover/ASTRA-E4-A128-INPUT-20261002.json'
TASK = '/home/jericho/.jericho/runtime/subagent-lifecycle/state/astra/contracts/ASTRA-E4-PUBLISHER-A122-WHOLE-CONNECTED-SOURCE-CLOSURE-A128__1.md'
RESULT = '/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-PUBLISHER-A122-WHOLE-CONNECTED-SOURCE-CLOSURE-A128-G1-RESULT.json'
ASSIGNMENT = 'ASTRA-E4-PUBLISHER-A122-WHOLE-CONNECTED-SOURCE-CLOSURE-A128'
PREFIX = 64 * 1024**2
FORWARD = 32 * 1024**2
CAP = 256 * 1024**2
OUTPUT_CAP = 32 * 1024**2
ANCHOR = 1790897913806934719
TZ = datetime.timezone(datetime.timedelta(hours=3))
os.umask(0o077)
LEDGER = ROOT + '/read-ledger.json'
if os.path.exists(LEDGER):
    with open(LEDGER, 'rb') as h:
        prefix = h.read(2 * 1024**2 + 1)
    ledger = json.loads(prefix)
    ledger['charged'] += len(prefix) * 2 + 1
else:
    ledger = {'charged': PREFIX + FORWARD, 'reservations': {'initial_read_tool_hash_transport_prefix': PREFIX, 'forward_helper_seal_transport': FORWARD}, 'events': [], 'actual_aggregate_RAM': 'NOT_ZERO_NOT_PROVEN', 'implicit_IO': 'NOT_ZERO_NOT_PROVEN', 'reservations_are_measurements': False, 'initial_stock_metadata_lookup_error': 'ONE KeyError files; no candidate execution or candidate retry; charged to prefix'}

def debit(n, why):
    if type(n) is not int or n < 0 or ledger['charged'] + n > CAP:
        raise RuntimeError('read cap')
    if time.time_ns() > ANCHOR + 7200 * 10**9:
        raise RuntimeError('original wall cap')
    ledger['charged'] += n
    ledger['events'].append({'bytes': n, 'reason': why, 'cumulative': ledger['charged']})

def nine(s):
    return [str(v) for v in (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)]

def read(p, expected=None):
    b = os.lstat(p)
    if not stat.S_ISREG(b.st_mode) or b.st_nlink != 1 or stat.S_IMODE(b.st_mode) != 0o600 or b.st_size > OUTPUT_CAP:
        raise RuntimeError('private regular identity ' + p)
    debit(b.st_size + 1, 'physical full read request ' + p)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as h:
        o = os.fstat(h.fileno())
        raw = h.read(b.st_size + 1)
        a = os.fstat(h.fileno())
    n = os.lstat(p)
    if nine(b) != nine(o) or nine(o) != nine(a) or nine(a) != nine(n) or len(raw) != b.st_size:
        raise RuntimeError('identity changed ' + p)
    debit(len(raw), 'complete SHA buffer scan ' + p)
    pin = {'path': p, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'identity9_decimal_strings': nine(b), 'before_opened_held_after_named_after_stable': True}
    if expected is not None:
        for k in ('bytes', 'sha256', 'identity9_decimal_strings'):
            if pin[k] != expected[k]:
                raise RuntimeError('input pin ' + p + ' ' + k)
    return raw, pin

def doc(p):
    raw, pin = read(p)
    debit(len(raw), 'stock JSON decode ' + p)
    return json.loads(raw), pin

def patch(p, raw, pre_reserved=False):
    if p != RESULT and not p.startswith(ROOT + '/'):
        raise RuntimeError('output scope')
    if type(raw) is not bytes or len(raw) > OUTPUT_CAP or not raw.endswith(b'\n'):
        raise RuntimeError('text output')
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    if os.path.exists(p):
        if pre_reserved:
            with open(p, 'rb') as h:
                before = h.read(2 * 1024**2 + 1)
        else:
            before, _ = read(p)
        text = '*** Begin Patch\n*** Update File: ' + p + '\n@@\n' + ''.join('-' + l + '\n' for l in before.decode().splitlines()) + ''.join('+' + l + '\n' for l in raw.decode().splitlines()) + '*** End Patch\n'
    else:
        text = '*** Begin Patch\n*** Add File: ' + p + '\n' + ''.join('+' + l + '\n' for l in raw.decode().splitlines()) + '*** End Patch\n'
    if not pre_reserved:
        debit(len(text.encode()) + len(raw), 'apply_patch input and output text ' + p)
    subprocess.run(['apply_patch'], input=text.encode(), check=True, stdout=subprocess.DEVNULL)
    os.chmod(p, 0o600)

def write_json(p, value):
    raw = (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
    debit(len(raw), 'stock encode output buffer ' + p)
    patch(p, raw)

def flush():
    # One forward booking covers the ledger's own read/encode/patch/transport;
    # it is charged before bytes containing the cumulative counter are frozen.
    debit(4 * 1024 * 1024, 'forward ledger read/encode/apply_patch/transport reservation')
    raw = (json.dumps(ledger, sort_keys=True, separators=(',', ':')) + '\n').encode()
    if len(raw) > 512000:
        raise RuntimeError('ledger forward reservation exhausted')
    patch(LEDGER, raw, pre_reserved=True)

def payloads(m):
    return [p for p in m['members'] if p['path'].split('/')[0] in ('source', 'schemas', 'benign', 'contracts', 'controls', 'coverage', 'fixtures')]

def clone():
    inp, _ = doc(INPUT)
    read(TASK)
    for group in ('source', 'final_independent_review'):
        for key in ('terminal', 'manifest'):
            x = inp[group][key]; read(x['path'], x)
        for x in inp[group]['selected']:
            read(x['path'], x)
    old, oldpin = doc(OLD + '/manifest.json')
    selected = payloads(old)
    if len(selected) != 58:
        raise RuntimeError('full payload58')
    for p in selected:
        raw, _ = read(OLD + '/' + p['path'], p)
        patch(ROOT + '/' + p['path'], raw)
    write_json(ROOT + '/predecessor-pins.json', {'manifest': oldpin, 'full_payload_pins': selected, 'payload_pathset': [p['path'] for p in selected], 'whole_acceptance_credit': 'NONE'})
    print('cloned58', sum(p['bytes'] for p in selected), 'charged', ledger['charged'])

def show(paths):
    for p in paths:
        raw, _ = read(ROOT + '/' + p)
        print(p, raw.decode())

def refresh():
    # This is ordinary JSON/text maintenance, not candidate schema execution.
    import copy
    name = 'friday.lab824.independent-trust-context.v1'
    ctx, _ = doc(ROOT + '/schemas/' + name + '.json')
    fields = ctx['root']['fields']
    digest = {'type':'string','min':64,'max':64,'pattern':'digest'}
    nullable = lambda value: {'type':'nullable','of':copy.deepcopy(value)}
    integer = lambda minimum,maximum: {'type':'integer','minimum':minimum,'maximum':maximum}
    kinds = fields['streams']['items']['fields']['kind']
    expected_ref = {'type':'object','exact':True,'fields':{
        'kind':copy.deepcopy(kinds),'path':{'type':'string','min':1,'max':240},
        'sha256':copy.deepcopy(digest),'size':integer(1,2000000),
        'producer_id':{'type':'string','min':1,'max':80},
        'selector_id':{'type':'string','min':1,'max':80},
        'document_sha256':nullable(digest),'offset':nullable(integer(0,80000000))}}
    for section in ('materials','operations','closures','snapshot'):
        desc = fields['performing_contracts']['of']['fields'][section]['items']['fields']
        desc['expected_body'] = nullable({'type':'string','min':1,'max':2000000})
        desc['expected_ref'] = nullable(expected_ref)
        desc['document_sha256'] = nullable(digest)
        desc['offset'] = nullable(integer(0,80000000))
        desc['size'] = integer(1,2000000)
    fields['document_contracts']['of']['items']['fields']['selected_ref'] = nullable(expected_ref)
    wheel = fields['wheel_literal_contracts']['of']['items']['fields']
    for prefix in ('package_info','selected_artifact'):
        wheel[prefix+'_selector_body'] = nullable({'type':'string','min':1,'max':2000000})
        wheel[prefix+'_selector_ref'] = nullable(expected_ref)
    write_json(ROOT + '/schemas/' + name + '.json', ctx)
    bill_schema, _ = doc(ROOT+'/schemas/friday.lab820.expected-bill.v1.json')
    bill, _ = doc(ROOT+'/fixtures/expected-bill.json')
    for target in ('data_closure','native_closure'):
        bill_schema['root']['fields'][target]['fields']['legacy_body_sha256'] = nullable(digest)
        bill[target]['legacy_body_sha256'] = None
    write_json(ROOT+'/schemas/friday.lab820.expected-bill.v1.json', bill_schema)
    write_json(ROOT+'/fixtures/expected-bill.json', bill)
    schemas = {}
    for filename in sorted(os.listdir(ROOT+'/schemas')):
        raw, pin = read(ROOT+'/schemas/'+filename)
        value = json.loads(raw); debit(len(raw),'stock schema name decode')
        schemas[value['name']] = pin['sha256']
    authority, _ = doc(ROOT+'/fixtures/authority.json')
    # Retain all authority identity/approval fields; only the concrete schema
    # pin for the changed ordinary expected bill follows the new schema bytes.
    print('authority keys',sorted(authority))
    authority['schema_pin'] = schemas['friday.lab820.expected-bill.v1']
    write_json(ROOT+'/fixtures/authority.json',authority)
    _, ep = read(ROOT+'/fixtures/expected-bill.json')
    _, ap = read(ROOT+'/fixtures/authority.json')
    pins, _ = read(ROOT+'/source/pins.py')
    prefix, marker, suffix = pins.decode().partition('SCHEMA_PINS = {\n')
    # partition deliberately selects the final ordinary SCHEMA_PINS, not the
    # historical table whose independent values must remain unchanged.
    if not marker or not suffix.endswith('}\n'):
        raise RuntimeError('source pin table textual boundary')
    # HISTORICAL_SCHEMA_PINS contains the substring; use a full line boundary.
    start = pins.decode().rfind('\nSCHEMA_PINS = {\n')
    if start < 0: raise RuntimeError('schema pins exact line')
    newpins = pins.decode()[:start+1] + 'SCHEMA_PINS = ' + json.dumps(schemas,sort_keys=True,indent=4) + '\n'
    for field,pin in (('EXPECTED_FIXTURE_SHA256',ep),('AUTHORITY_FIXTURE_SHA256',ap)):
        lines = newpins.splitlines(True)
        matching = [i for i,line in enumerate(lines) if line.startswith(field+' = ')]
        if len(matching)!=1: raise RuntimeError('fixture pin boundary')
        lines[matching[0]] = field + " = '" + pin['sha256'] + "'\n"
        newpins=''.join(lines)
    patch(ROOT+'/source/pins.py',newpins.encode())
    positive, _ = doc(ROOT+'/benign/full-positive.json')
    positive['authority_sha256']=ap['sha256'];positive['expected_sha256']=ep['sha256']
    positive['future_ordinary_binding']['golden_selection']='independent full golden preimage, producer/selector/resource-observer and actual five-argument input digest; exact entire public outcome comparison, never current observations'
    positive['remaining_connected_cause']='complete ordinary all69 producer-bound variants and distinct full positives are absent; future typed/sliced references reduce nesting and aggregate inline-body growth but do not prove capacity or an actual baseline'
    write_json(ROOT+'/benign/full-positive.json',positive)
    write_json(ROOT+'/data/schema-pin-join.json',{'schema':'friday.a128.stock-schema-pin-join.v1','schemas':schemas,'expected_fixture':ep,'authority_fixture':ap,'stock_JSON_only':True,'candidate_schema_validator_run':False,'all20':len(schemas),'historical_schema_pins_modified':False})
    print('refreshed',len(schemas),'schemas','charged',ledger['charged'])

def disposition():
    inp, _ = doc(INPUT)
    selected=inp['final_independent_review']['selected'][0]
    raw,reviewpin=read(selected['path'],selected)
    review=json.loads(raw); debit(len(raw),'stock independent-review JSON decode')
    mechanisms={
      'A122-R1':{
        'actual_changes':['Full typed class capability/result/tool/environment/observation preimages and exact derived hashes replace four digest-only runtime labels. Every executable/dependency/kernel/stdout/stderr regular preimage is held and fully hashed.',
          'Class-specific issuer, document-kind, target and method dispatch is reached from performing_contracts through the material vector and closure planner. Public material/closure status derives from full reached consumers, not runtime_closed=False. Unrelated OpenPGP roles are still refused.'],
        'sites':['source/runtime_consumer.py:consume_runtime','source/performing_contracts.py:_typed_domains','source/whole_join.py:bind_other_document','source/document_vector.py:_bind_metadata','source/recipe_planner.py:_consume_closures'],
        'precise_residual':'Full preimage/typed correspondence consumers now exist, but class issuer authentication and extraction/install/native execution semantics are not proved by selected observations or fixed issuer strings. Independently produced actual observations and class-specific producers remain absent. No publisher or runtime acceptance is claimed.',
        'author_disposition':'REAL_FULL_PREIMAGE_CONSUMER_REPAIR_REQUIRES_INDEPENDENT_REVIEW_NOT_WHOLE_CLOSURE'},
      'A122-R2':{
        'actual_changes':['All15 operation-specific methods, complete actual called-material input hashes, selected physical member derivations, tool/environment/dependency/ABI/resource/runtime/custody preimages and producer/selector roles are compared.',
          'Acyclic complete predecessor/output hashes retain full preimages in public composition and operation output bodies without repeatedly nesting those full bodies in subsequent input bodies. Existing assembly/manifest computed-byte checks, exact membership, hierarchy-before-hash and all A009 fields remain.'],
        'sites':['source/runtime_consumer.py:METHODS','source/runtime_consumer.py:consume_runtime','source/performing_contracts.py:operation_input','source/recipe_planner.py:_plan_performing_operations','source/whole_join.py:a009_domain_map'],
        'precise_residual':'The selected derivation consumer does not itself parse/extract/install an archive or supply native ABI/runtime producers. All15 and A009 are future conditional graphs, not observed coherent producer output. Legacy fixed fallback remains explicitly diagnostic; it does not close this residual.',
        'author_disposition':'REAL_CONNECTED_DERIVATION_INPUT_REPAIR_DISTINCT_PRODUCER_SEMANTICS_STILL_UNCLOSED'},
      'A122-R3':{
        'actual_changes':['Exact independently selected complete expected bodies can be supplied by exclusive inline or independently producer-bound held slice references. Observed and expected physical ranges may not be the same range; all full document/range/raw SHA and size checks remain.',
          'All four performing sections, full document selectors and wheel selector contracts have matching nullable inline/ref schemas. All20 current schema SHA pins and concrete fixture pins are refreshed together. Original 2M/80M/350/512/128 bounds are unchanged.',
          'External five-argument variants consume a full producer-bound golden with actual input domain SHA, exact whole output SHA/size, distinct producer/selector/resource-observer identities and inline-or-held full preimage. Unrelated-cause sentinel and full Root preliminary-manifest comparison remain.'],
        'sites':['source/performing_contracts.py:expected_body','source/performing_contracts.py:selected_range','source/document_vector.py:_selected','source/material_literals.py:consume_wheel_literals','source/declared_controls.py:_invoke_declared_control','schemas/friday.lab824.independent-trust-context.v1.json'],
        'precise_residual':'No independently prepared complete ordinary baseline, all69 complete golden outcomes, two distinct full ordinary positives or concrete safe causal variants are supplied. The retained context still has 228 unheld NOT_PROVEN receipts and six null prerequisite domains. Acyclic references are a real representation repair, not proof that a complete valid ordinary package fits all unchanged limits. No generator, synthetic current observations or full-golden selfacceptance is used.',
        'author_disposition':'REAL_REFERENCE_AND_FULL_GOLDEN_PRODUCER_BINDING_REPAIR_REQUIRED_DATA_AND_CAPACITY_STILL_UNCLOSED'},
      'A122-R4':{
        'actual_changes':['Requested allocation/read/slot reservations commit after fallible checkpoints; proposed configuration commits only after complete validation and actual sampling. Actual sampled IO remains a debit even on refusal.',
          'Slot retirement does not check the exhausted meter; every lease retirement is attempted and the original failure is preserved. Container/encoding/window/JSON/canonical work has earlier bounded reservations.',
          'WholeMeter physically preallocates a bounded 4096-byte refusal arena with forward read/allocation/time reserves. Public planner, wire and control entry/final-wire ContractErrors are caught outside meter entry. Fixed ASCII complete refusal bytes do not re-enter canonical_bytes or expired check.'],
        'sites':['source/resource_meter.py:WholeMeter','source/resource_meter.py:attach_refusal','source/recipe_planner.py:_ConstructionMeter','source/recipe_planner.py:plan_construction','source/recipe_planner.py:plan_construction_wire','source/declared_controls.py:invoke_declared_control','source/body_scope.py:document_scope','source/document_windows.py','source/canonical.py:parse_exact'],
        'precise_residual':'No independently owned performing outer import/provider/native/helper/output/seal/transport aggregate instrumenter or actual end-to-end RAM/implicit IO evidence is supplied. Forward reservations and one-process samples are not aggregate observations or a guarantee against host descheduling/OOM. Remaining temporary containers and exception paths require new independent review; no runtime refusal proof was run.',
        'author_disposition':'REAL_ATOMIC_RETIREMENT_AND_BOUNDED_REFUSAL_SOURCE_REPAIR_OUTER_AGGREGATE_STILL_UNCLOSED'},
      'A122-R5':{
        'actual_changes':['Legacy exactly-five-field bodies use explicit separate legacy_body_sha256. Full exactly-eleven-field performing closure bodies retain body_sha256 and their full selected SHA.',
          'Public data/native/full-runtime closure status is derived solely from both full reached typed consumers; legacy body availability is a separate diagnostic field and never an acceptance prerequisite. Expected-bill schema, concrete fixture, authority schema pin and Source fixture/schema pins agree.'],
        'sites':['source/recipe_planner.py:_consume_closures','schemas/friday.lab820.expected-bill.v1.json','fixtures/expected-bill.json','fixtures/authority.json','source/pins.py'],
        'precise_residual':'The identified incompatible legacy/typed raw-SHA requirement has an actual Source/schema/public-status repair. This is an author proposition awaiting independent review, not a successful whole closure or runtime test. R1/R2/R3/R4 remain independently relevant.',
        'author_disposition':'IDENTIFIED_PREIMAGE_MISMATCH_SOURCE_REPAIR_REQUIRES_INDEPENDENT_REVIEW'}}
    causes=[]
    for cause in review['remaining_causes']:
        row=dict(cause); row['A128']=mechanisms[cause['id']]; causes.append(row)
    all9=[]
    for prior in review['all9_dispositions']:
        all9.append({'id':prior['id'],'prior_independent_disposition':prior,
                     'current_connected_causes':[c for c in causes if prior['id'] in c['P33']],
                     'status':'AUTHOR_SOURCE_CHANGES_NOT_INDEPENDENT_ACCEPTANCE'})
    schemas,_=doc(ROOT+'/data/schema-pin-join.json')
    schema_map={name:{'current_sha256':schemas['schemas'][name],
                       'original_reached_consumers':consumer,
                       'A128_delta':'full typed descriptor/sliced selector shape' if name=='friday.lab824.independent-trust-context.v1' else 'distinct legacy_body_sha256/full typed body_sha256' if name=='friday.lab820.expected-bill.v1' else 'unchanged shape and original consumers retained',
                       'candidate_validator_executed':False}
                for name,consumer in review['all20_schema_consumer_map'].items()}
    package={'schema':'friday.a128.whole-connected-source-repair.v1','assignment':ASSIGNMENT,'generation':1,
      'accepted_msk':'2026-10-02T02:39:10+03:00','immutable_predecessor_review':reviewpin,
      'whole_Source_ready':False,'GO':False,'runtime':'NOT_RUN','gates':'NOT_RUN',
      'execution_authorized':False,'publisher_proof':False,'release_credit':'NONE',
      'required_original_scope':review['required_original_scope'],'scope_cut':False,'cap_raise':False,
      'all5_connected_causes':causes,'all9':all9,'all20_schema_consumer_map':schema_map,
      'remaining_connected_causes':['A122-R1 class-specific authentication/semantic actual producers not established','A122-R2 distinct extraction/install/native/runtime producer graph absent','A122-R3 complete independent ordinary baseline/all69/two full positives/safe variants and actual capacity absent','A122-R4 performing independent outer aggregate instrumenter/actual evidence absent'],
      'A122_R5':'ACTUAL_SOURCE_SCHEMA_PREIMAGE_REPAIR_AWAITING_INDEPENDENT_REVIEW',
      'missing_current_Root_Image_fact_is_Source_bug':False,
      'historical_unsafe_IDs':'INERT_ABSTRACT_DATA_REQUIRED_NOT_RUN_NO_WAIVER_NO_EXECUTABLE_FIXTURE_OR_GENERATOR',
      'no_candidate_import_AST_compile_eval_exec_tests_parser_controls_GPG_archive_network_Root_product_Git_install_live_gates':True,
      'actual_aggregate_RAM':'NOT_ZERO_NOT_PROVEN','actual_implicit_IO':'NOT_ZERO_NOT_PROVEN',
      'independent_review_requested':'ONE new whole immutable Source snapshot; all connected code/schema/producer-consumer/resource/ownership/positional delta and exact complement together'}
    write_json(ROOT+'/contracts/current-source-package.json',package)
    write_json(ROOT+'/contracts/all9-source-closure.json',{'schema':'friday.a128.all9-author-source-disposition.v1','findings':all9,'Source_ready':False,'GO':False,'runtime':'NOT_RUN','gates':'NOT_RUN','release_credit':'NONE','execution_authorized':False})
    source_bytes=sum(os.stat(ROOT+'/source/'+f).st_size for f in os.listdir(ROOT+'/source'))
    resource_doc={'schema':'friday.a128.whole-resource-source-qualification.v1','actual_final_source_text_bytes':source_bytes,'source_size_is_import_IO_measurement':False,'actual_aggregate_RAM':'NOT_ZERO_NOT_PROVEN','implicit_host_helper_IO':'NOT_ZERO_NOT_PROVEN','native_output_seal_transport_envelope':'NOT_PROVEN','bounded_refusal_source':'physically preallocated private call arena; actual runtime exhaustion/refusal NOT_RUN','whole_caps_unchanged':True,'Source_ready':False,'GO':False,'runtime':'NOT_RUN'}
    write_json(ROOT+'/contracts/whole-resource.json',resource_doc)
    observation,_=doc(ROOT+'/contracts/performing-observation.json')
    observation['schema']='friday.a128.performing-observation-contract.v1'
    observation['runtime_consumer']='source/runtime_consumer.py full class issuer/method/capability/result/tool/environment/observation/resource/physical derivation preimages; selected future metadata, never current runtime or publisher proof'
    observation['semantic_consumer']='source/performing_contracts.py and reached material/closure/all15/A009 consumers; whole independent review required'
    observation['class_specific_parser_producers']='REMAINING_NOT_IMPLEMENTED'
    write_json(ROOT+'/contracts/performing-observation.json',observation)
    caller,_=doc(ROOT+'/contracts/public-caller.json');caller['source_ready']=False
    caller['source_ready_means']='whole Source closure is not established; real five-argument graph requires new independent review'
    write_json(ROOT+'/contracts/public-caller.json',caller)
    coverage,_=doc(ROOT+'/coverage/closure.json')
    coverage['schema']='friday.a128.whole-source-change-review-request.v1'
    coverage['remaining']=package['remaining_connected_causes']
    coverage['all5_repair_package']='contracts/current-source-package.json'
    coverage['independent_review']='NEW_WHOLE_REVIEW_REQUIRED'
    write_json(ROOT+'/coverage/closure.json',coverage)
    for name in ('controls/causal-contract.json','benign/oracles.json'):
        value,_=doc(ROOT+'/'+name)
        rows=value['controls'] if 'controls' in value else value.get('oracles',[])
        for row in rows:
            binding=row.get('future_ordinary_binding')
            if binding is not None: binding['golden_selection']='full independent producer/selector/observer/input-bound inline-or-held golden preimage; entire public outcome equality; no selfacceptance or current observations'
        value['A128_actual_source_changes']='full producer-bound golden and independently selected full-body reference consumers; prepared complete actual variants remain absent'
        write_json(ROOT+'/'+name,value)
    ledger['ancillary_stock_helper_errors_not_candidate_retries']=['initial manifest key lookup KeyError files','missing python executable; no execution','wrong optional display filename contracts/remaining-causes.json; no read','wrong optional assessment subdirectory; no read','optional map display used slice on dict; no candidate action']
    print('disposition all5 all9 all20 Source',source_bytes,'charged',ledger['charged'])

def tree(root):
    files=[]; directories=[]
    for directory, dirs, names in os.walk(root,followlinks=False):
        s=os.lstat(directory)
        if not stat.S_ISDIR(s.st_mode) or stat.S_IMODE(s.st_mode)!=0o700:
            raise RuntimeError('private directory '+directory)
        directories.append({'path':os.path.relpath(directory,root),'mode':'0700','identity9_decimal_strings':nine(s)})
        for name in dirs:
            if not stat.S_ISDIR(os.lstat(directory+'/'+name).st_mode):raise RuntimeError('directory link')
        files.extend(os.path.relpath(directory+'/'+name,root) for name in names)
    return sorted(files),sorted(directories,key=lambda r:r['path'])

def audit():
    # A finite stock JSON/byte/identity audit, not Source execution, parsing,
    # controls, syntax checking, tests or acceptance.
    inp,ip=doc(INPUT)
    if ip['sha256']!='2c0187ac057cd25476a6f3d799fa2bb87000afcd342683ee0e833528ce8e45ca':raise RuntimeError('input SHA')
    _,tp=read(TASK)
    if tp['sha256']!='aa50890c4f6708ddd6cf59a79fdaa0c69587420d508c798c40bb535937df179c':raise RuntimeError('task SHA')
    external=[ip,tp]
    for group in ('source','final_independent_review'):
        g=inp[group]
        for key in ('terminal','manifest'):
            x=g[key];_,pin=read(x['path'],x);external.append(pin)
        for x in g['selected']:
            _,pin=read(x['path'],x);external.append(pin)
        m,mp=doc(g['manifest']['path'])
        base=os.path.dirname(g['manifest']['path'])
        paths,_=tree(base)
        expected=sorted([row['path'] for row in m['members']]+['manifest.json'])
        if paths!=expected:raise RuntimeError('pinned input exact pathset '+group)
        for row in m['members']:
            _,pin=read(base+'/'+row['path'],row);external.append(pin)
    _,rp=read(inp['final_independent_review']['root_received_ref'])
    if rp['sha256']!='b37e6b7ce197fb672c5c4474f20c717416911d342de314c4cdaba4dba1ea562f':raise RuntimeError('Rootreceived SHA')
    external.append(rp)
    old,_=doc(OLD+'/manifest.json')
    oldset={p['path'] for p in payloads(old)}
    currentpaths,dirs=tree(ROOT)
    currentset={name for name in currentpaths if name.split('/')[0] in ('source','schemas','benign','contracts','controls','coverage','fixtures')}
    if not oldset.issubset(currentset) or len(oldset)!=58:raise RuntimeError('original58 payload retention')
    source=[p for p in currentpaths if p.startswith('source/')]
    if len(source)!=24:raise RuntimeError('fullSource24')
    sourcepins=[]
    for name in source:
        _,pin=read(ROOT+'/'+name);sourcepins.append(dict(pin,path=name))
    shapes={}; schema_pins={}
    for name in sorted(p for p in currentpaths if p.startswith('schemas/')):
        value,pin=doc(ROOT+'/'+name)
        shapes[value['name']]=value;schema_pins[value['name']]=pin['sha256']
    if len(shapes)!=20:raise RuntimeError('all20 schemas')
    joins,_=doc(ROOT+'/data/schema-pin-join.json')
    if schema_pins!=joins['schemas']:raise RuntimeError('all20 current pin join')
    pinsraw,_=read(ROOT+'/source/pins.py')
    pintext=pinsraw.decode(); table=pintext[pintext.rfind('\nSCHEMA_PINS = '):]
    for name,digest in schema_pins.items():
        if ('"'+name+'": "'+digest+'"') not in table:raise RuntimeError('Source textual schema pin '+name)
    newbill,ep=doc(ROOT+'/fixtures/expected-bill.json')
    oldbill,_=doc(OLD+'/fixtures/expected-bill.json')
    comparison=json.loads(json.dumps(newbill));debit(len(json.dumps(newbill))*2,'stock fixture semantic copy compare')
    for target in ('data_closure','native_closure'):
        if comparison[target].pop('legacy_body_sha256') is not None:raise RuntimeError('legacy placeholder remains null')
    if comparison!=oldbill:raise RuntimeError('original whole expected bill retained')
    authority,ap=doc(ROOT+'/fixtures/authority.json');olda,_=doc(OLD+'/fixtures/authority.json')
    if authority['schema_pin']!=schema_pins['friday.lab820.expected-bill.v1']:raise RuntimeError('expected schema authority pin')
    ac=dict(authority);ac['schema_pin']=olda['schema_pin']
    if ac!=olda:raise RuntimeError('authority only concrete schema pin')
    for field,pin in (('EXPECTED_FIXTURE_SHA256',ep),('AUTHORITY_FIXTURE_SHA256',ap)):
        if field+" = '"+pin['sha256']+"'" not in pintext:raise RuntimeError('Source fixture pin')
    unchanged=[]
    for name in ('benign/context.json','benign/streams.json','fixtures/presented.json','controls/catalog.json','contracts/a009-output-map.json'):
        a,_=read(OLD+'/'+name);b,_=read(ROOT+'/'+name)
        debit(len(a)+len(b),'exact retained metadata equality '+name)
        if a!=b:raise RuntimeError('original invariant '+name)
        unchanged.append(name)
    control,_=read(ROOT+'/source/declared_controls.py');oldcontrol,_=read(OLD+'/source/declared_controls.py')
    section=lambda b:b.split(b'CATALOG = [',1)[1].split(b'\ndef _load',1)[0]
    if section(control)!=section(oldcontrol):raise RuntimeError('full69 source tuples and migrations retained')
    context,_=doc(ROOT+'/benign/context.json')
    catalog,_=doc(ROOT+'/controls/catalog.json')
    # The independent count is metadata, not a control execution result.
    controls=catalog if type(catalog) is list else catalog.get('controls',catalog.get('catalog',[]))
    if len(context['document_receipts'])!=228 or len(controls)!=69:raise RuntimeError('all228/all69 metadata census')
    cf=shapes['friday.lab824.independent-trust-context.v1']['root']['fields']
    desired={'expected_body','expected_ref','kind','path','producer_id','selector_id','sha256','target','document_sha256','offset','size'}
    for section_name in ('materials','operations','closures','snapshot'):
        shape=cf['performing_contracts']['of']['fields'][section_name]['items']
        if shape['exact'] is not True or set(shape['fields'])!=desired:raise RuntimeError('descriptor11 exact')
    for target in ('data_closure','native_closure'):
        if 'legacy_body_sha256' not in shapes['friday.lab820.expected-bill.v1']['root']['fields'][target]['fields']:raise RuntimeError('distinct closure identity schema')
    package,_=doc(ROOT+'/contracts/current-source-package.json')
    if package['whole_Source_ready'] is not False or package['GO'] is not False or len(package['all5_connected_causes'])!=5 or len(package['all9'])!=9:raise RuntimeError('honest finite package disposition')
    write_json(ROOT+'/data/external-input-pins.json',{'schema':'friday.a128.complete-pinned-input-census.v1','pins':external,'full_pinned_manifests_and_members_read':True,'all_full_SHA_stable9':True,'read_before_effects':'completed before clone; this is final immutable-input revalidation','candidate_code_executed':False})
    stock={'schema':'friday.a128.stock-source-metadata-audit.v1','source_files':sourcepins,'source_file_count':24,'actual_source_text_bytes':sum(p['bytes'] for p in sourcepins),'all20_schema_SHA_join':schema_pins,'all_original58_payload_paths_retained':True,'added_payload_paths':sorted(currentset-oldset),'unchanged_exact_metadata':unchanged,'original_expected_bill_exact_except_two_null_distinct_legacy_identities':True,'authority_exact_except_concrete_expected_schema_pin':True,'all69_source_catalog_and_original_tuple_region_bytes_equal':True,'retained_receipts':len(context['document_receipts']),'retained_receipt_bodies_held':sum(r['body_held'] is True for r in context['document_receipts']),'performing_descriptor_fields':sorted(desired),'original_caps_unchanged':{'artifacts':350,'members':512,'active_slots':128,'document_bytes':80000000,'aggregate_context_bytes':2000000},'all_required_scope':package['required_original_scope'],'candidate_import_AST_compile_eval_exec_tests_parser_controls_archive_native_network_Git':False,'Source_acceptance':False,'runtime':'NOT_RUN','gates':'NOT_RUN','GO':False,'resource_proof':'NOT_ZERO_NOT_PROVEN; known debits and forward reservations are not aggregate actual observations'}
    write_json(ROOT+'/data/stock-source-metadata-audit.json',stock)
    return package

def seal(package):
    if os.path.exists(ROOT+'/manifest.json') or os.path.exists(RESULT):raise RuntimeError('single terminal seal only')
    freeze=time.time_ns()
    if freeze>ANCHOR+(7200-600)*10**9:raise RuntimeError('missed original substantive cutoff')
    paths,_=tree(ROOT)
    size=sum(os.lstat(ROOT+'/'+p).st_size for p in paths)
    # Three full read+SHA passes (this seal, postseal and bounded Root transport),
    # manifest/terminal creation and frozen ledger metadata are forward charged
    # before sealing. Unknown interpreter/host IO and aggregate RAM stay unknown.
    reservation=size*6+8*1024**2
    debit(reservation,'forward exact complete member seal/hash/postseal/Root-transport and manifest/terminal reservation')
    ledger['seal_forward_reservation']=reservation
    ledger['source_substantive_freeze_wall_ns']=str(freeze)
    ledger['whole_resource_compliance']='NOT_PROVEN'
    ledger['all_unknown_actual_aggregate_RAM_and_implicit_IO']='NOT_ZERO_NOT_PROVEN'
    flush()
    sealed_charged=ledger['charged'];used=0
    def reserved_read(p,expected=None):
        nonlocal used
        before=os.lstat(p)
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode)!=0o600 or before.st_nlink!=1:raise RuntimeError('sealed private file')
        used+=before.st_size*2+1
        if used>reservation:raise RuntimeError('seal forward reserve exhausted')
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        with os.fdopen(fd,'rb') as h:
            opened=os.fstat(h.fileno());raw=h.read(before.st_size+1);after=os.fstat(h.fileno())
        named=os.lstat(p)
        if nine(before)!=nine(opened) or nine(opened)!=nine(after) or nine(after)!=nine(named) or len(raw)!=before.st_size:raise RuntimeError('sealed stable9')
        pin={'path':p,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'identity9_decimal_strings':nine(before),'before_opened_held_after_named_after_stable':True}
        if expected is not None and any(pin[k]!=expected[k] for k in ('bytes','sha256','identity9_decimal_strings')):raise RuntimeError('sealed bytes changed')
        return raw,pin
    paths,directories=tree(ROOT);members=[]
    for name in paths:
        _,pin=reserved_read(ROOT+'/'+name);members.append(dict(pin,path=name))
    manifest={'schema':'friday.a128.immutable-whole-source-manifest.v1','assignment':ASSIGNMENT,'generation':1,'output_root':ROOT,'members':members,'sealed_member_pathset':paths,'sealed_final_pathset':sorted(paths+['manifest.json']),'member_count':len(members),'member_bytes':sum(p['bytes'] for p in members),'private_files_mode':'0600','private_directories_mode':'0700','nlink_regular_files':1,'full9_decimal_strings':True,'full_SHA256_only':True,'self_digest_exclusion':['manifest.json','protected RESULT outside root'],'acyclic':True,'directory_pins_except_root':[p for p in directories if p['path']!='.'],'root_directory_final_identity_is_in_protected_RESULT':True,'substantive_freeze_wall_ns':str(freeze),'original_wall_anchor_ns':str(ANCHOR),'freeze_before_original_600_second_seal_reserve':True,'cumulative_read_hash_helper_seal_transport_charged':sealed_charged,'read_cap':CAP,'known_debits_and_reservations_are_actual_aggregate_measurements':False,'actual_aggregate_RAM':'NOT_ZERO_NOT_PROVEN','implicit_IO':'NOT_ZERO_NOT_PROVEN','Source_ready':False,'independent_review':'NEW_WHOLE_SOURCE_REVIEW_REQUIRED','runtime':'NOT_RUN','gates':'NOT_RUN','GO':False,'required_scope':package['required_original_scope'],'source_graph':'all original23 files plus runtime_consumer.py; full affected immutable Source, typed schema/producer-consumer/ownership/resource/positional delta/equal complement together','input_evidence':'data/external-input-pins.json','delta':'data/delta.json','exact_equal_complement':'data/complement.json','all5_all9_all20_disposition':'contracts/current-source-package.json'}
    raw=(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\n').encode()
    used+=len(raw)*4
    if used>reservation:raise RuntimeError('manifest reserve')
    patch(ROOT+'/manifest.json',raw,pre_reserved=True)
    _,mp=reserved_read(ROOT+'/manifest.json')
    rootstat=os.lstat(ROOT);rootnine=nine(rootstat)
    finalpaths,finaldirs=tree(ROOT)
    if finalpaths!=manifest['sealed_final_pathset']:raise RuntimeError('final exact pathset')
    if sum(os.lstat(ROOT+'/'+name).st_size for name in finalpaths)>OUTPUT_CAP:raise RuntimeError('whole output cap')
    for p in members:reserved_read(ROOT+'/'+p['path'],p)
    for d in manifest['directory_pins_except_root']:
        if nine(os.lstat(ROOT+'/'+d['path']))!=d['identity9_decimal_strings']:raise RuntimeError('sealed directory changed')
    finished=time.time_ns()
    if finished>ANCHOR+7200*10**9:raise RuntimeError('seal original wall')
    elapsed=(finished-ANCHOR)/10**9
    result={'schema':'friday.subagent.result.v2','assignment':ASSIGNMENT,'generation':1,'owner_scope':'goal','accepted_msk':'2026-10-02T02:39:10+03:00','finished_msk':datetime.datetime.fromtimestamp(finished/10**9,TZ).isoformat(),'actual_elapsed_original_wall_seconds':elapsed,'actual_elapsed_from_accepted_seconds':(finished-1790897950*10**9)/10**9,'original_wall_anchor_ns':str(ANCHOR),'status':'DELIVERED_REAL_CONNECTED_SOURCE_REPAIR_WITH_PRECISE_UNCLOSED_RESIDUAL','finite_assignment_complete':True,'whole_Source_ready':False,'GO':False,'runtime':'NOT_RUN','gates':'NOT_RUN','execution_authorized':False,'publisher_proof':False,'release_credit':'NONE','Source_root':ROOT,'manifest':mp,'root_identity9_decimal_strings':rootnine,'root_private_mode':'0700','exact_final_pathset':finalpaths,'sealed_source_files':24,'all_original58_payload_paths_retained':True,'remaining_connected_causes':package['remaining_connected_causes'],'R5_disposition':package['A122_R5'],'full_cause_schema_scope_package':ROOT+'/contracts/current-source-package.json','full_delta':ROOT+'/data/delta.json','full_equal_complement':ROOT+'/data/complement.json','stock_metadata_audit':ROOT+'/data/stock-source-metadata-audit.json','input_pins':ROOT+'/data/external-input-pins.json','known_cumulative_read_hash_helper_seal_transport_debits_and_forward_reservations':sealed_charged,'read_cap_bytes':CAP,'seal_forward_reserved_bytes':reservation,'seal_forward_known_used_at_terminal_encode':used,'forward_transport_still_reserved':True,'actual_aggregate_RAM':'NOT_ZERO_NOT_PROVEN','actual_implicit_IO':'NOT_ZERO_NOT_PROVEN','whole_resource_compliance':'NOT_PROVEN','reservations_are_measurements':False,'hard_caps':{'wall_seconds':7200,'inclusive_seal_reserve_seconds':600,'read_bytes':CAP,'RAM_bytes':8589934592,'output_bytes':OUTPUT_CAP,'local':4,'modelchildren':0,'retries':0},'candidate_import_AST_compile_eval_exec_tests_parser_controls_GPG_archive_native_network_Root_product_Git_install_live_gates':'NOT_RUN','models_reasoning_tier_provider_TUI_goal_guard_writer_lock_changes':False,'unsafe_historical_IDs':'INERT_ABSTRACT_DATA_REQUIRED_NOT_RUN_NO_WAIVER','new_independent_review_required':True,'no_selfacceptance':True,'obligation_caps_scenarios_fullbytes_live_negatives_waivers_changed':False,'completion_boundary':'one protected RESULT; no following task/goal/poll/wait; timestamp is final seal verification immediately before this terminal write'}
    rr=(json.dumps(result,sort_keys=True,separators=(',',':'))+'\n').encode()
    used+=len(rr)*4
    if used>reservation:raise RuntimeError('terminal reserve')
    patch(RESULT,rr,pre_reserved=True)
    _,terminal=reserved_read(RESULT)
    if nine(os.lstat(ROOT))!=rootnine or tree(ROOT)[0]!=finalpaths:raise RuntimeError('root after terminal')
    if used>reservation:raise RuntimeError('final reserve')
    print(json.dumps({'RESULT':terminal,'manifest':mp,'finished_msk':result['finished_msk'],'actual_elapsed_original_wall_seconds':elapsed,'charged_with_forward_reserves':sealed_charged,'known_seal_used':used,'remaining_forward_reserve':reservation-used,'Source_ready':False,'GO':False},sort_keys=True))

def finalize():
    disposition()
    delta()
    package=audit()
    seal(package)

def delta():
    m, mp = doc(OLD + '/manifest.json')
    changed = []; equal = []; diff = []; complement = []
    for p in payloads(m):
        name = p['path']; before, bp = read(OLD + '/' + name, p); after, ap = read(ROOT + '/' + name)
        debit(len(before) + len(after), 'complete byte compare and positional delta ' + name)
        if before == after:
            equal.append({'path': name, 'before': bp, 'after': ap, 'bytes_equal': True})
            continue
        spans = []; eq = []
        prefix = 0
        while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]: prefix += 1
        suffix = 0
        while suffix < min(len(before), len(after)) - prefix and before[-1-suffix] == after[-1-suffix]: suffix += 1
        opcodes = [('equal',0,prefix,0,prefix),('replace',prefix,len(before)-suffix,prefix,len(after)-suffix),('equal',len(before)-suffix,len(before),len(after)-suffix,len(after))]
        for tag, i, j, k, l in opcodes:
            if i == j and k == l: continue
            row = {'old_start': i, 'old_end': j, 'new_start': k, 'new_end': l, 'kind': tag, 'old_sha256': hashlib.sha256(before[i:j]).hexdigest(), 'new_sha256': hashlib.sha256(after[k:l]).hexdigest()}
            debit((j-i) + (l-k), 'delta span SHA ' + name)
            if tag == 'equal':
                if before[i:j] != after[k:l]: raise RuntimeError('equal complement')
                eq.append(row)
            else:
                row['old_hex'] = before[i:j].hex(); row['new_hex'] = after[k:l].hex(); spans.append(row)
        changed.append({'path': name, 'before': bp, 'after': ap, 'changed_spans': spans, 'equal_spans': eq})
        complement.append({'path': name, 'equal_spans': eq, 'full_positional_partition': True})
        diff.extend(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True), fromfile='A117/' + name, tofile='A128/' + name))
    newpaths = sorted(p for d, ds, fs in os.walk(ROOT) for f in fs for p in [os.path.relpath(d+'/'+f, ROOT)] if p.split('/')[0] in ('source','schemas','benign','contracts','controls','coverage','fixtures'))
    oldpaths = {p['path'] for p in payloads(m)}
    additions = []
    for name in newpaths:
        if name not in oldpaths:
            raw, pin = read(ROOT + '/' + name); additions.append({'path':name,'after':pin,'raw_hex':raw.hex(),'old_exists':False})
    write_json(ROOT + '/data/delta.json', {'schema':'friday.a128.raw-positional-full-delta.v1','predecessor_manifest':mp,'changed':changed,'unchanged':equal,'additions':additions,'deleted':[],'all_original58_payload_paths_retained':True,'candidate_code_executed':False})
    write_json(ROOT + '/data/complement.json', {'schema':'friday.a128.exact-equal-complement.v1','changed_equal_spans':complement,'unchanged':equal,'bytes_equal_compared':True,'predecessor':mp})
    patch(ROOT + '/data/complete-unified.diff', ''.join(diff).encode())
    print('delta',len(changed),len(equal),len(additions),'charged',ledger['charged'])

if __name__ == '__main__':
    mode = sys.argv[1]
    debit(os.stat(__file__).st_size * 2 + 1, 'helper load/read/hash reserve')
    if mode == 'clone': clone()
    elif mode == 'show': show(sys.argv[2:])
    elif mode == 'book': debit(int(sys.argv[2]), 'external tool read/hash/edit reservation ' + ' '.join(sys.argv[3:]))
    elif mode == 'delta': delta()
    elif mode == 'refresh': refresh()
    elif mode == 'disposition': disposition()
    elif mode == 'finalize': finalize();sys.exit(0)
    else: raise RuntimeError('unknown stock mode')
    flush()
