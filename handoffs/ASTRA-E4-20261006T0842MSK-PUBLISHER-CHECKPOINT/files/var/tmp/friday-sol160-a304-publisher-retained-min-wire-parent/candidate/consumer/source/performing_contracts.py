"""Performing consumers of independent ordinary observation bodies.

The stock caller selects the expected complete body and a different producer.
The held observed body is parsed and compared in full, before any member is
used. These are future input contracts, never present runtime observations,
publisher authentication, an extraction recipe or an authority grant.
"""
from body_scope import document_scope
from canonical import canonical_bytes, domain_digest, parse_exact
from contract import ContractError, is_digest
from document_vector import _Lease
from resource_meter import HashlibProxy, checkpoint, reserve_allocation, debit_read

hashlib = HashlibProxy()
_SECTIONS = ('materials', 'operations', 'closures', 'snapshot')
_FIELDS = {'producer_id', 'target', 'input_body', 'members', 'dependencies',
           'abi', 'resources', 'runtime', 'custody', 'output_paths', 'package_bindings'}
_MEMBER = {'operation', 'path', 'kind', 'mode', 'size', 'uid', 'gid', 'nlink',
           'device', 'mount_domain', 'parent_path', 'link_target',
           'link_target_sha256', 'content_sha256', 'status', 'source_ref'}

def descriptor(context, section, target):
    if context is None or context.get('performing_contracts') is None:
        return None
    if section not in _SECTIONS:
        raise ContractError('operation_dependency')
    rows = context['performing_contracts'][section]
    chosen = None
    for row in rows:
        if row['target'] == target:
            if chosen is not None: raise ContractError('duplicate_observation')
            chosen = row
    return chosen

@document_scope
def expected_body(held, inline, ref):
    if (inline is None) == (ref is None):
        raise ContractError('selected_record')
    if inline is not None:
        if type(inline) is not str or len(inline) > 2000000 or not inline.isascii():
            raise ContractError('document_size')
        reserve_allocation(len(inline) + 1)
        raw = inline.encode('ascii')
    else:
        if type(ref) is not dict or set(ref) != {'kind','path','sha256','size','producer_id','selector_id','document_sha256','offset'} or ref['producer_id'] == ref['selector_id']:
            raise ContractError('expected_bound_to_itself')
        if type(ref['size']) is not int or not 0 < ref['size'] <= 2000000:
            raise ContractError('document_size')
        digest = ref['sha256'] if ref['document_sha256'] is None else ref['document_sha256']
        with _Lease(held, ref['kind'], ref['path'], digest) as lease:
            raw = selected_range(lease.body, ref['sha256'], ref['size'], ref['document_sha256'], ref['offset'])
    # Returning parsed JSON metadata cannot let the physical body alias escape.
    return parse_exact(raw, max_bytes=2000000, max_depth=12, max_items=512, max_string=2000000)

def selected_range(document, digest, size, document_digest=None, offset=None):
    if type(document) is not bytes or type(size) is not int or not 0 <= size <= 80000000 or not is_digest(digest):
        raise ContractError('selected_record')
    if document_digest is None:
        if offset is not None or len(document) != size:
            raise ContractError('selected_record')
        raw = document
    else:
        if not is_digest(document_digest) or type(offset) is not int or not 0 <= offset <= len(document) or size > len(document)-offset:
            raise ContractError('selected_record')
        if hashlib.sha256(document).hexdigest() != document_digest:
            raise ContractError('selected_record')
        reserve_allocation(size+64); debit_read(size)
        raw = document[offset:offset+size]
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ContractError('selected_record')
    return raw

@document_scope
def _member_bytes(held, ref, expected_sha, expected_size):
    fields = set(ref) if type(ref) is dict else set()
    if fields not in ({'kind','path','sha256'}, {'kind','path','sha256','document_sha256','offset','size'}):
        raise ContractError('operation_computation')
    if ref['sha256'] != expected_sha or not is_digest(expected_sha):
        raise ContractError('operation_computation')
    if 'size' in ref and ref['size'] != expected_size:
        raise ContractError('operation_computation')
    digest = ref.get('document_sha256') or ref['sha256']
    with _Lease(held, ref['kind'], ref['path'], digest) as lease:
        selected_range(lease.body, expected_sha, expected_size, ref.get('document_sha256'), ref.get('offset'))
    return {'sha256': expected_sha, 'size': expected_size}

def _members(body, held):
    rows = body['members']
    if type(rows) is not list or not 1 <= len(rows) <= 512:
        raise ContractError('member_limit')
    reserve_allocation(len(rows) * 160 + 64)
    by_path = {}
    for row in rows:
        checkpoint()
        if type(row) is not dict or set(row) != _MEMBER:
            raise ContractError('operation_computation')
        path = row['path']
        if type(path) is not str or path in by_path:
            raise ContractError('duplicate_member')
        if row['status'] != 'STRUCTURALLY_BOUND':
            raise ContractError('operation_computation')
        for key in ('size', 'uid', 'gid', 'nlink', 'device'):
            if type(row[key]) is not int or row[key] < 0:
                raise ContractError('operation_computation')
        if row['size'] > 80000000 or row['uid'] > 65535 or row['gid'] > 65535:
            raise ContractError('operation_computation')
        roots=('/opt/friday/quality-toolchain/venv','/work/candidate','/inputs/golden')
        parent = '' if path in roots else path.rsplit('/', 1)[0] if '/' in path else ''
        if row['parent_path'] != parent or type(row['mount_domain']) is not str:
            raise ContractError('operation_computation')
        if row['kind'] == 'regular':
            if row['nlink'] != 1 or row['link_target'] is not None or row['link_target_sha256'] is not None:
                raise ContractError('operation_computation')
            _member_bytes(held, row['source_ref'], row['content_sha256'], row['size'])
        elif row['kind'] == 'directory':
            if row['nlink'] < 2 or row['source_ref'] is not None or row['link_target'] is not None or row['link_target_sha256'] is not None:
                raise ContractError('operation_computation')
        elif row['kind'] == 'symlink':
            if type(row['link_target']) is not str or row['source_ref'] is not None:
                raise ContractError('operation_computation')
            if hashlib.sha256(row['link_target'].encode('utf-8')).hexdigest() != row['link_target_sha256']:
                raise ContractError('link_target_identity')
        else:
            raise ContractError('member_kind')
        by_path[path] = row
    from whole_join import bind_member_hierarchy
    if bind_member_hierarchy(rows)['closed'] is not True:
        raise ContractError('operation_computation')
    return by_path

def _typed_domains(body, members, held, section, target):
    packages=body['package_bindings']
    if type(packages) is not list or len(packages)>350:
        raise ContractError('closure_membership')
    names=set()
    for package in packages:
        if type(package) is not dict or set(package)!={'name','members'} or type(package['name']) is not str or package['name'] in names:
            raise ContractError('closure_membership')
        names.add(package['name'])
        if type(package['members']) is not list or not package['members'] or any(path not in members for path in package['members']):
            raise ContractError('closure_membership')
    edges = body['dependencies']
    if type(edges) is not list or len(edges) > 512:
        raise ContractError('dependency_inventory')
    identities = set()
    for edge in edges:
        if type(edge) is not dict or set(edge) != {'consumer_path', 'provider_path', 'provider_sha256', 'abi'}:
            raise ContractError('dependency_inventory')
        if type(edge['consumer_path']) is not str or type(edge['provider_path']) is not str:
            raise ContractError('dependency_inventory')
        pair = (edge['consumer_path'], edge['provider_path'])
        if pair in identities or any(path not in members for path in pair):
            raise ContractError('dependency_inventory')
        identities.add(pair)
        if members[pair[1]]['content_sha256'] != edge['provider_sha256']:
            raise ContractError('dependency_closure')
        if type(edge['abi']) is not str or not edge['abi']:
            raise ContractError('dependency_closure')
    abi = body['abi']
    if type(abi) is not dict or set(abi) != {'architecture', 'python_abi', 'loader_path', 'loader_sha256', 'libraries'}:
        raise ContractError('python_abi')
    if abi['architecture'] != 'amd64' or abi['python_abi'] != 'cp314-regular':
        raise ContractError('python_abi')
    if type(abi['loader_path']) is not str:
        raise ContractError('dependency_closure')
    loader = members.get(abi['loader_path'])
    if loader is None or loader['content_sha256'] != abi['loader_sha256'] or not is_digest(abi['loader_sha256']):
        raise ContractError('dependency_closure')
    if type(abi['libraries']) is not list or any(type(p) is not str for p in abi['libraries']) or len(set(abi['libraries'])) != len(abi['libraries']):
        raise ContractError('dependency_inventory')
    if any(path not in members for path in abi['libraries']):
        raise ContractError('dependency_inventory')
    resources = body['resources']
    if type(resources) is not list or len(resources) > 512:
        raise ContractError('closure_membership')
    roles = set()
    for row in resources:
        if type(row) is not dict or set(row) != {'role', 'path', 'sha256', 'size'}:
            raise ContractError('closure_membership')
        if type(row['role']) is not str or not 1<=len(row['role'])<=80 or type(row['path']) is not str or type(row['size']) is not int or not is_digest(row['sha256']) or row['role'] in roles:
            raise ContractError('closure_membership')
        roles.add(row['role'])
        member = members.get(row['path'])
        if member is None or member['content_sha256'] != row['sha256'] or member['size'] != row['size']:
            raise ContractError('closure_body')
    from runtime_consumer import consume_runtime
    runtime = body['runtime']
    runtime_join = consume_runtime(body, members, held, section, target)
    custody = body['custody']
    if type(custody) is not dict or set(custody) != {'producer_id', 'target', 'input_sha256', 'member_inventory_sha256', 'runtime_sha256', 'resources_sha256', 'selected_by', 'effects_granted'}:
        raise ContractError('custody_linkage')
    computed = {
        'input_sha256': domain_digest('friday.a117.performing-input.v1', body['input_body']),
        'member_inventory_sha256': domain_digest('friday.a117.installed-members.v1', body['members']),
        'runtime_sha256': domain_digest('friday.a117.runtime-observation.v1', runtime),
        'resources_sha256': domain_digest('friday.a117.resources.v1', resources),
    }
    if custody['producer_id'] != body['producer_id'] or custody['target'] != body['target'] or custody['effects_granted'] is not False:
        raise ContractError('custody_linkage')
    if any(custody[k] != value for k, value in computed.items()):
        raise ContractError('custody_linkage')
    return runtime_join

@document_scope
def consume(context, held, section, target, actual_input=None):
    selected = descriptor(context, section, target)
    if selected is None:
        return None
    if selected['producer_id'] == selected['selector_id']:
        raise ContractError('expected_bound_to_itself')
    digest = selected['sha256'] if selected['document_sha256'] is None else selected['document_sha256']
    with _Lease(held, selected['kind'], selected['path'], digest) as lease:
        raw = selected_range(lease.body, selected['sha256'], selected['size'], selected['document_sha256'], selected['offset'])
        body = parse_exact(raw, max_bytes=2000000, max_depth=12, max_items=512, max_string=2000000)
        trusted = expected_body(held, selected['expected_body'], selected['expected_ref'])
        ref = selected['expected_ref']
        if ref is not None:
            if ref['selector_id'] != selected['selector_id'] or ref['producer_id'] == selected['producer_id']:
                raise ContractError('expected_bound_to_itself')
            old_offset=0 if selected['document_sha256'] is None else selected['offset']
            expected_offset=0 if ref['document_sha256'] is None else ref['offset']
            if ref['kind'] == selected['kind'] and ref['path'] == selected['path'] and expected_offset == old_offset and ref['size'] == selected['size']:
                raise ContractError('expected_bound_to_itself')
        if body != trusted or hashlib.sha256(canonical_bytes(body)).hexdigest() != selected['sha256']:
            raise ContractError('selected_record')
        if type(body) is not dict or set(body) != _FIELDS:
            raise ContractError('operation_computation')
        if body['producer_id'] != selected['producer_id'] or body['target'] != target:
            raise ContractError('result_issuer')
        if actual_input is not None and body['input_body'] != actual_input:
            raise ContractError('operation_computation')
        members = _members(body, held)
        paths = body['output_paths']
        if type(paths) is not list or not paths or len(set(paths)) != len(paths) or any(path not in members for path in paths):
            raise ContractError('operation_computation')
        runtime_join = _typed_domains(body, members, held, section, target)
        artifact_sha = actual_input['sha256'] if section=='materials' and type(actual_input) is dict and 'sha256' in actual_input else domain_digest('friday.a128.operation-artifact.v1',body['input_body'])
        if body['runtime']['artifact_sha256'] != artifact_sha:
            raise ContractError('custody_linkage')
        if section=='closures':
            required=actual_input['data_packages'] if target=='data' else actual_input['interpreter_packages']
            have={p['name'] for p in body['package_bindings']}
            if have!=set(required):raise ContractError('closure_membership')
            if target=='data' and {r['role'] for r in body['resources']}!={'certificates','fonts','locales','timezones','nss','glib','gpu'}:
                raise ContractError('closure_membership')
        if body['custody']['selected_by'] != selected['selector_id']:
            raise ContractError('custody_linkage')
        return {'body': body, 'raw_sha256': selected['sha256'],
                'selected_body_sha256': hashlib.sha256(canonical_bytes(body)).hexdigest(),
                'observed_body_ref':{k:selected[k] for k in ('kind','path','sha256','document_sha256','offset','size')},
                'expected_body_ref':selected['expected_ref'],
                'producer_id':selected['producer_id'],'selector_id':selected['selector_id'],
                'custody_sha256': domain_digest('friday.a117.actor-custody.v1', body['custody']),
                'runtime_consumer': runtime_join,
                'status': 'STRUCTURALLY_BOUND', 'publisher_proof': False,
                'runtime': 'FUTURE_PRODUCER_BOUND_INPUT_NOT_CURRENT_OBSERVATION'}

def operation_input(name, expected, composition, presented, predecessors):
    vector = composition['full_document_vector']
    other = vector['other_documents']
    def classes(kinds):
        return [row for row in other if row['document_kind'] in kinds]
    if name == 'authenticate-ubuntu-indexes':
        material = vector['indexes']
    elif name == 'authenticate-ubuntu-archives':
        material = vector['archives']
    elif name == 'authenticate-node-archive':
        material = {'consumer': vector['node'], 'projection': vector.get('node_projection')}
    elif name == 'hold-unrar-publisher-gap':
        material = {'required': expected['unrar'], 'consumers': classes(('unrar',))}
    elif name == 'authenticate-wheels':
        material = {'literal_consumers': composition['wheels'], 'body_consumers': classes(('wheel',))}
    elif name in ('map-cpython-venv', 'map-lib-dynload', 'map-native-loader'):
        material = {'platform': expected['platform'], 'closure': composition['closure_view'], 'consumers': classes(('native',))}
    elif name == 'map-browser-resources':
        material = {'browser': expected['browser'], 'consumers': classes(('browser',))}
    elif name == 'map-data-closure':
        material = {'required': expected['data_packages'], 'closure': composition['closure_view'], 'consumers': classes(('data',))}
    elif name == 'bind-candidate':
        material = {'identity': expected['candidate'], 'consumers': classes(('candidate',))}
    elif name == 'bind-golden':
        material = {'identity': expected['golden'], 'consumers': classes(('golden',))}
    elif name in ('assemble-members', 'write-final-manifest', 'external-custody'):
        # Preserve and compare every byte of complete predecessor bodies while
        # keeping the input graph acyclic and bounded in nesting depth.
        material = {'full_predecessor_output_refs': [
            {'name':row['name'],'output_sha256':row['output_sha256'],
             'body_sha256':hashlib.sha256(canonical_bytes(row['full_output_body'])).hexdigest(),
             'observed_body_ref':dict(row['performing_ref'])}
            for row in predecessors]}
    else:
        raise ContractError('recipe_operations')
    # This digest is computed from the COMPLETE already-called consumer output,
    # not copied from a claimed label. Its full preimage stays in the public
    # composition/operation outputs, while a small acyclic input avoids repeated
    # nesting of complete capabilities, inventories and predecessor bodies.
    material_ref = {'full_material_input_sha256':domain_digest(
        'friday.a128.full-called-operation-material.v1',material)}
    return {'operation': name, 'material': material_ref,
            'predecessors': [{'name': p['name'], 'output_sha256': p['output_sha256']} for p in predecessors]}
