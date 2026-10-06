"""Full independently selected future actor/tool/runtime preimages.

These consumers compare held ordinary records and physical regular bytes.
They neither run a tool nor give an actor Root, publisher, or effect credit.
An external producer must supply the observations in a separately authorized
process. Digest labels without the corresponding bodies cannot close a join.
"""
from canonical import canonical_bytes, domain_digest
from contract import ContractError, is_digest
from resource_meter import checkpoint, reserve_allocation

ISSUERS = {
    'wheel': 'pypi-retained', 'native': 'system-native',
    'browser': 'browser-publisher', 'data': 'system-data',
    'candidate': 'owner-candidate', 'golden': 'owner-golden',
    'kernel': 'owner-kernel', 'unrar': 'rarlab-publisher',
    'member': 'independent-member', 'custody': 'independent-custody',
    'ubuntu-inrelease': 'ubuntu-archive', 'ubuntu-packages': 'ubuntu-archive',
    'ubuntu-archive': 'ubuntu-archive', 'node-archive': 'nodejs.org',
    'node-shasums256': 'nodejs.org', 'data-closure': 'system-data',
    'native-closure': 'system-native', 'snapshot': 'independent-snapshot',
    'operation': 'independent-operation',
}
METHODS = {
    'authenticate-ubuntu-indexes': 'index-selection',
    'authenticate-ubuntu-archives': 'archive-install',
    'authenticate-node-archive': 'node-install',
    'hold-unrar-publisher-gap': 'unrar-gap',
    'authenticate-wheels': 'wheel-install',
    'map-cpython-venv': 'venv-map', 'map-lib-dynload': 'dynload-map',
    'map-native-loader': 'loader-map', 'map-browser-resources': 'browser-install',
    'map-data-closure': 'data-map', 'bind-candidate': 'candidate-tree',
    'bind-golden': 'golden-tree', 'assemble-members': 'assemble',
    'write-final-manifest': 'manifest', 'external-custody': 'custody',
}

def exact(value, keys, cause):
    if type(value) is not dict or len(value) > 512:
        raise ContractError(cause)
    reserve_allocation((len(value)+len(keys))*80+128)
    if set(value) != set(keys):
        raise ContractError(cause)
    return value

def text(value, maximum=240, empty=False):
    if type(value) is not str or not (0 if empty else 1) <= len(value) <= maximum or '\x00' in value:
        raise ContractError('custody_linkage')
    return value

def integer(value, maximum=10**21):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ContractError('custody_linkage')
    return value

def rows(value, maximum=512):
    if type(value) is not list or len(value) > maximum:
        raise ContractError('count')
    reserve_allocation(len(value) * 16 + 64)
    return value

def physical(ref, size, held):
    from performing_contracts import _member_bytes
    if type(ref) is not dict or set(ref) not in ({'kind','path','sha256'}, {'kind','path','sha256','document_sha256','offset','size'}):
        raise ContractError('operation_computation')
    text(ref['kind'], 40); text(ref['path']); integer(size, 80000000)
    if not is_digest(ref['sha256']):
        raise ContractError('operation_computation')
    return _member_bytes(held, ref, ref['sha256'], size)

def consume_runtime(body, members, held, section, target):
    runtime = exact(body['runtime'], (
        'producer_id', 'artifact_sha256', 'tool_sha256', 'environment_sha256',
        'observation_sha256', 'tool', 'environment', 'observation',
        'capability', 'verification_result'), 'custody_linkage')
    producer = text(body['producer_id'], 80)
    if runtime['producer_id'] != producer:
        raise ContractError('result_issuer')
    kind = ('operation' if section == 'operations' else 'snapshot' if section == 'snapshot'
            else target + '-closure' if section == 'closures'
            else 'custody' if target == 'a009-custody' else target.split('/', 1)[0])
    if kind not in ISSUERS:
        raise ContractError('document_kind')
    tool = exact(runtime['tool'], ('producer_id', 'executable', 'dependencies', 'argv', 'abi'), 'executable_binding')
    if tool['producer_id'] != producer or tool['abi'] != body['abi']:
        raise ContractError('executable_binding')
    executable = exact(tool['executable'], ('ref', 'size'), 'executable_binding')
    physical(executable['ref'], executable['size'], held)
    argv = rows(tool['argv'], 32)
    if not argv or argv[0] != executable['ref']['path']:
        raise ContractError('argv')
    for arg in argv: text(arg, 512)
    deps = rows(tool['dependencies'], 128)
    if not deps: raise ContractError('dependency_inventory_empty')
    seen = set()
    for dep in deps:
        exact(dep, ('ref', 'size', 'abi', 'custody'), 'dependency_inventory')
        path = dep['ref']['path']
        if path in seen or dep['abi'] != body['abi']['python_abi']:
            raise ContractError('dependency_inventory')
        seen.add(path); physical(dep['ref'], dep['size'], held)
        custody = exact(dep['custody'], ('producer_id', 'path', 'sha256', 'size', 'effects_granted'), 'custody_linkage')
        if custody != {'producer_id': producer, 'path': path, 'sha256': dep['ref']['sha256'], 'size': dep['size'], 'effects_granted': False}:
            raise ContractError('custody_linkage')
    environment = exact(runtime['environment'], ('producer_id', 'entries', 'kernel', 'process', 'observed_at_ns'), 'environment_entries')
    if environment['producer_id'] != producer: raise ContractError('result_issuer')
    names = set()
    for entry in rows(environment['entries'], 32):
        exact(entry, ('name', 'value'), 'environment_entries')
        text(entry['name'], 80); text(entry['value'], 512, empty=True)
        if entry['name'] in names: raise ContractError('environment_entries')
        names.add(entry['name'])
    kernel = exact(environment['kernel'], ('release', 'ref', 'size'), 'custody_linkage')
    text(kernel['release'], 128); physical(kernel['ref'], kernel['size'], held)
    process = exact(environment['process'], ('pid', 'started_ns', 'uid', 'gid'), 'custody_linkage')
    if integer(process['pid'], 4294967295) == 0: raise ContractError('custody_linkage')
    for key in ('started_ns', 'uid', 'gid'): integer(process[key])
    integer(environment['observed_at_ns'])
    observation = exact(runtime['observation'], (
        'producer_id', 'target', 'method', 'input_sha256', 'member_inventory_sha256',
        'dependencies_sha256', 'abi_sha256', 'resources_sha256', 'tool_sha256',
        'environment_sha256', 'exit_code', 'stdout', 'stderr', 'started_ns',
        'finished_ns', 'derivations', 'aggregate_resources'), 'custody_linkage')
    derived = {
        'input_sha256': domain_digest('friday.a117.performing-input.v1', body['input_body']),
        'member_inventory_sha256': domain_digest('friday.a117.installed-members.v1', body['members']),
        'dependencies_sha256': domain_digest('friday.a128.dependencies.v1', body['dependencies']),
        'abi_sha256': domain_digest('friday.a117.abi.v1', body['abi']),
        'resources_sha256': domain_digest('friday.a117.resources.v1', body['resources']),
        'tool_sha256': domain_digest('friday.a128.full-tool.v1', tool),
        'environment_sha256': domain_digest('friday.a128.full-environment.v1', environment),
    }
    if observation['producer_id'] != producer or observation['target'] != target or observation['exit_code'] != 0:
        raise ContractError('result_binding')
    method = METHODS.get(target) if section == 'operations' else kind
    if observation['method'] != method or any(observation[k] != v for k, v in derived.items()):
        raise ContractError('result_binding')
    for key in ('stdout', 'stderr'):
        stream = exact(observation[key], ('ref', 'size'), 'result_binding')
        physical(stream['ref'], stream['size'], held)
    started = integer(observation['started_ns']); finished = integer(observation['finished_ns'])
    if not process['started_ns'] <= started <= finished <= environment['observed_at_ns'] or finished - started > 4200 * 10**9:
        raise ContractError('whole_deadline')
    resources = exact(observation['aggregate_resources'], ('observer_id', 'scope', 'processes', 'peak_ram_bytes', 'read_bytes', 'output_bytes', 'implicit_io_status'), 'resource_contract')
    if resources['observer_id'] == producer or resources['scope'] != 'producer-and-all-helpers' or resources['implicit_io_status'] != 'OBSERVED':
        raise ContractError('resource_contract')
    text(resources['observer_id'], 80)
    if not rows(resources['processes'], 128) or process['pid'] not in resources['processes']:
        raise ContractError('resource_contract')
    if len(set(resources['processes'])) != len(resources['processes']): raise ContractError('resource_contract')
    for pid in resources['processes']:
        if integer(pid, 4294967295) == 0: raise ContractError('resource_contract')
    for key, maximum in (('peak_ram_bytes',8589934592),('read_bytes',40960000000),('output_bytes',33554432)):
        integer(resources[key], maximum)
    if resources['peak_ram_bytes'] == 0: raise ContractError('resource_contract')
    cap = exact(runtime['capability'], (
        'schema', 'issuer_id', 'actor_id', 'document_kind', 'method', 'target',
        'artifact_sha256', 'input_sha256', 'dependencies_sha256', 'abi_sha256',
        'resources_sha256', 'tool_sha256', 'environment_sha256', 'custody_sha256',
        'produced_by_this_package', 'effects_granted'), 'capability_binding')
    if cap['schema'] != 'friday.a128.class-capability.v1' or cap['issuer_id'] != ISSUERS[kind] or cap['actor_id'] != producer or cap['document_kind'] != kind or cap['target'] != target or cap['method'] != method:
        raise ContractError('document_kind')
    if cap['produced_by_this_package'] is not False or cap['effects_granted'] is not False:
        raise ContractError('self_issued_capability')
    if any(cap[k] != v for k, v in derived.items() if k != 'member_inventory_sha256'):
        raise ContractError('capability_binding')
    if not is_digest(runtime['artifact_sha256']) or cap['artifact_sha256'] != runtime['artifact_sha256'] or not is_digest(cap['custody_sha256']):
        raise ContractError('capability_binding')
    for key in ('tool_sha256', 'environment_sha256'):
        if runtime[key] != derived[key]: raise ContractError('result_binding')
    observation_sha = domain_digest('friday.a128.full-runtime-observation.v1', observation)
    if runtime['observation_sha256'] != observation_sha: raise ContractError('result_binding')
    result = exact(runtime['verification_result'], (
        'schema', 'issuer_id', 'actor_id', 'capability_sha256', 'observation_sha256',
        'artifact_sha256', 'member_inventory_sha256', 'custody_sha256',
        'decision', 'produced_by_this_package'), 'result_binding')
    capability_sha = domain_digest('friday.a128.class-capability.v1', cap)
    if result != {'schema':'friday.a128.class-result.v1', 'issuer_id':ISSUERS[kind], 'actor_id':producer, 'capability_sha256':capability_sha, 'observation_sha256':observation_sha, 'artifact_sha256':runtime['artifact_sha256'], 'member_inventory_sha256':derived['member_inventory_sha256'], 'custody_sha256':cap['custody_sha256'], 'decision':'STRUCTURALLY_BOUND', 'produced_by_this_package':False}:
        raise ContractError('result_binding')
    derivations = rows(observation['derivations'])
    paths = set()
    for row in derivations:
        checkpoint()
        exact(row, ('path', 'method', 'source_ref', 'source_size', 'selector'), 'operation_computation')
        path = row['path']
        if path in paths or path not in members or path not in body['output_paths']:
            raise ContractError('operation_computation')
        paths.add(path); member = members[path]
        if row['method'] != method: raise ContractError('operation_computation')
        # The actual input preimage and the exact selected output cannot be
        # replaced by a fixed filename projection or by a digest-only event.
        selector = exact(row['selector'], ('input_sha256','output_path','output_sha256','output_size'), 'operation_computation')
        if selector != {'input_sha256':derived['input_sha256'], 'output_path':path, 'output_sha256':member['content_sha256'], 'output_size':member['size']}:
            raise ContractError('operation_computation')
        if member['kind'] == 'regular':
            if row['source_ref'] != member['source_ref'] or row['source_size'] != member['size']:
                raise ContractError('operation_computation')
            physical(row['source_ref'], row['source_size'], held)
        elif row['source_ref'] is not None or row['source_size'] != 0:
            raise ContractError('operation_computation')
    if paths != set(body['output_paths']): raise ContractError('operation_computation')
    return {'status':'STRUCTURALLY_BOUND', 'document_kind':kind, 'issuer_id':ISSUERS[kind],
            'capability_sha256':capability_sha,
            'result_sha256':domain_digest('friday.a128.class-result.v1', result),
            'custody_sha256':cap['custody_sha256'], 'observation_sha256':observation_sha,
            'tool_sha256':derived['tool_sha256'], 'environment_sha256':derived['environment_sha256'],
            'full_body_consumed':True, 'current_observation':False,
            'publisher_proof':False, 'effects_granted':False}
