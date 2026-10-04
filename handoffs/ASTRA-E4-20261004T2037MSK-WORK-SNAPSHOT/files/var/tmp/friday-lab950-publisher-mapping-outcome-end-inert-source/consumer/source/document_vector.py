"""Separate whole-vector raw consumers; no cross-document concatenation.

Full externally fixed contracts are optional only for the retained pending
baseline. Their absence is explicit NOT_PROVEN, never vector acceptance.
Current imports/bytes/controls are not run by the Source preparation task.
"""
from canonical import canonical_bytes, domain_digest, parse_exact
from capability import authenticate_capability, capability_digest
from contract import ContractError, is_digest, _STAGE
from formats import parse_clearsign, parse_release, select_package, parse_deb822, iter_deb822
from ingress import load_pinned_schema
from receipt_chain import assess_node_chain, compare_archive, correspond_algorithms
from resource_meter import HashlibProxy, checkpoint
hashlib=HashlibProxy()
from whole_join import bind_other_document, join_node_projection
from body_scope import register, document_scope, begin_producer, end_producer, retire_producer

class _Lease:
    def __init__(self,held,kind,path,digest):
        self.held=held; self.kind=kind; self.path=path; self.digest=digest; self.body=None
        self._charged=False
        self._retire_attempted=False
        self._retired=False
    def __enter__(self):
        register(self)
        self.held.meter.charge_slots(1)
        self._charged=True
        scope,previous=begin_producer(self)
        try:
            self.body=self.held.get_document(self.kind,self.path,self.digest)
            if type(self.body) is not bytes:
                raise ContractError('body_not_paged')
            if retire_producer(scope,self) is not True:
                raise ContractError('body_cleanup')
            return self
        finally:
            end_producer(scope,previous)
    def __exit__(self,*args):
        # The decorated consumer owns both this body and parser aliases.
        return False
    def retire(self):
        # A refused/throwing release leaves the original charge and body owned.
        if self._retired:return True
        if self._retire_attempted:return False
        self._retire_attempted=True
        if self._charged:
            if self.held.meter.release_slots(1) is not True:
                return False
            self._charged=False
        self.body=None
        self._retired=True
        return True

def _parsed_custody(contract, row, result, capability):
    raw=contract['custody_body']
    if raw is None:return None
    body=parse_exact(raw.encode('ascii'),max_bytes=8000,max_depth=8,max_items=512,max_string=512)
    expected={'document_kind':row['document_kind'],'path':row['path'],
              'sha256':row['sha256'],'size':row['size'],
              'produced_by_this_package':False}
    # Raw custody precedes capability/result. Their complete hashes must not
    # be fed back into a body whose digest those same objects contain.
    if body!=expected:raise ContractError('custody_linkage')
    digest=domain_digest('friday.sol037.document-custody.v1',body)
    if digest!=row['custody_sha256']:raise ContractError('custody_linkage')
    if result is not None and result['custody_sha256']!=digest:
        raise ContractError('custody_linkage')
    return body

def _result_pin(contract,result):
    if result is None or contract['expected_result_sha256'] is None:return False
    if domain_digest('friday.lab815.verification-result.v1',result)!=contract['expected_result_sha256']:
        raise ContractError('result_binding')
    return True

def _selected(contract, selected, held=None):
    raw=contract['selected_body']
    ref=contract['selected_ref']
    if raw is not None and ref is not None:
        raise ContractError('selected_record')
    if raw is None:
        if ref is None:return False
        from performing_contracts import expected_body
        if selected != expected_body(held, None, ref):
            raise ContractError('selected_record')
        return True
    actual=canonical_bytes(selected)
    if actual!=raw.encode('ascii'):raise ContractError('selected_record')
    return True

def _bind_metadata(row,contract,parsed,cap,result,selected,decision,class_runtime=None):
    actual={
        'raw_sha256':row['sha256'] if parsed is None else parsed['raw_document_sha256'],
        'normalized_body_sha256':None if parsed is None else parsed['signed_body_sha256'],
        'armor_sha256':None if parsed is None else parsed['signature_armor_sha256'],
        'capability_sha256':None if cap is None else capability_digest(cap),
        'expected_capability_sha256':None if contract['expected_capability'] is None else capability_digest(contract['expected_capability']),
        'result_sha256':None if result is None else domain_digest('friday.lab815.verification-result.v1',result),
        'selected_sha256':hashlib.sha256(canonical_bytes(selected)).hexdigest(),
        'input_sha256':domain_digest('friday.sol037.document-input.v1',contract),
        'key_fingerprint':None if cap is None else cap['key_fingerprint'],
        'decision':decision,
    }
    if class_runtime is not None:
        actual['capability_sha256'] = class_runtime['capability_sha256']
        actual['expected_capability_sha256'] = class_runtime['capability_sha256']
        actual['result_sha256'] = class_runtime['result_sha256']
        if contract['expected_result_sha256'] != actual['result_sha256']:
            raise ContractError('result_binding')
    for key,value in actual.items():
        if row[key]!=value:raise ContractError('full_receipt_binding')
    return actual

@document_scope
def _ubuntu_index(index, contracts, rows, held, package_expectations):
    in_key=('ubuntu-inrelease',index['id']); pa_key=('ubuntu-packages',index['id']+'/'+index['member_name'])
    c=contracts[in_key]; row=rows[in_key]; p_row=rows[pa_key]
    release={'architecture':index['architecture'],'component':index['component'],
             'packages_sha256':index['packages_sha256'],'size':index['size'],'suite':index['suite']}
    if c['release_expected']!=release:raise ContractError('index_link')
    with _Lease(held,*in_key,row['sha256']) as signed, _Lease(held,*pa_key,p_row['sha256']) as packages:
        parsed=parse_clearsign(signed.body)
        selected=parse_release(parsed['cleartext'],release)
        if hashlib.sha256(packages.body).hexdigest()!=selected['packages_sha256'] or len(packages.body)!=selected['size']:
            raise ContractError('member_identity')
        cap=c['capability']; result=c['verification_result']
        if cap is None:
            decision={'status':'NOT_PROVEN','cause':'full_capability_absent'}
        else:
            correspond_algorithms(parsed,cap,result)
            decision=authenticate_capability(cap,c['expected_capability'],result,signed.body,
                parsed['normalized_body'],parsed['signature_armor'],None,
                load_pinned_schema('verifier-capability.v1'),load_pinned_schema('verification-result.v1'),
                c['approved_fingerprint'])
        bound_result=_result_pin(c,result)
        custody=_parsed_custody(c,row,result,cap)
        selected_bound=_selected(c,selected,held)
        metadata=_bind_metadata(row,c,parsed,cap,result,selected,decision['status'])
        closed=decision['status'] in ('AUTHENTICATED','STRUCTURALLY_BOUND') and bound_result and custody is not None and selected_bound
        out={'index':dict(index),'decision':decision,'raw_sha256':parsed['raw_document_sha256'],
             'normalized_body_sha256':parsed['signed_body_sha256'],'armor_sha256':parsed['signature_armor_sha256'],
             'packages_sha256':selected['packages_sha256'],'selected':selected,'capability':cap,
             'expected_capability':c['expected_capability'],'verification_result':result,
             'custody_body':custody,'closed':closed,'publisher_proof':False}
        out['bound_metadata']=metadata
        # Packages has a distinct admitted identity and custody, not merely the signed index alias.
        p_c=contracts[pa_key]
        p_custody=_parsed_custody(p_c,p_row,None,None)
        out['packages_bound_metadata']=_bind_metadata(p_row,p_c,None,None,None,selected,'STRUCTURALLY_BOUND')
        out['packages_custody_body']=p_custody
        out['closed']=closed and p_custody is not None and _selected(p_c,selected,held)
        wanted={(p['name'],p['version'],p['architecture'],p['filename']):p for p in package_expectations}
        complete=[]; seen=set()
        for stanza in iter_deb822(packages.body,max_bytes=80000000):
            identity=tuple(stanza.get(k,b'').decode('utf-8') for k in ('Package','Version','Architecture','Filename'))
            if identity not in wanted:continue
            if identity in seen:raise ContractError('duplicate_package')
            seen.add(identity); expected=wanted[identity]
            from contract import bounded_int
            if any(k not in stanza for k in ('Size','SHA256')) or bounded_int(stanza['Size'],2000000000,'integer_text')!=expected['size'] or stanza['SHA256'].decode('ascii')!=expected['sha256']:
                raise ContractError('package_archive_identity')
            chosen={k:expected[k] for k in ('name','version','architecture','filename','sha256','size')}
            chosen.update({'unknown_fields':sorted(set(stanza)-{'Package','Version','Architecture','Filename','Size','SHA256'}),'publisher_proof':False})
            complete.append({'fields':[{'name':k,'value_hex':v.hex()} for k,v in sorted(stanza.items())],'selected_identity':chosen})
        if seen!=set(wanted):raise ContractError('package_missing')
        out['complete_package_records']=complete
        return out

@document_scope
def _ubuntu_archive(package,index_result,contracts,rows,held):
    key=('ubuntu-archive',package['filename']); c=contracts[key]; row=rows[key]
    index=index_result['index']; p_key=('ubuntu-packages',index['id']+'/'+index['member_name'])
    expected={'name':package['name'],'version':package['version'],'architecture':package['architecture'],
              'filename':package['filename'],'sha256':package['sha256'],'size':package['size']}
    if c['package_expected']!=expected:raise ContractError('package_archive_identity')
    with _Lease(held,*key,row['sha256']) as archive:
        records=[record for record in index_result['complete_package_records'] if all(record['selected_identity'][k]==v for k,v in expected.items())]
        if len(records)!=1:raise ContractError('duplicate_package')
        complete=records[0]
        selected_bound=_selected(c,complete,held)
        archive_decision=compare_archive(archive.body,package['sha256'],package['size'])
        custody=_parsed_custody(c,row,None,None)
        metadata=_bind_metadata(row,c,None,None,None,complete,archive_decision['status'])
        return {'package':dict(package),'selected':complete,'archive_decision':archive_decision,
                'publisher_index':index_result,'custody_body':custody,'closed':index_result['closed'] and selected_bound and custody is not None,
                'bound_metadata':metadata,'publisher_proof':False}

@document_scope
def _other_document(key,row,contract,context,held):
    with _Lease(held,*key,row['sha256']) as body:
        if len(body.body)!=row['size'] or hashlib.sha256(body.body).hexdigest()!=row['sha256']:
            raise ContractError('receipt_membership')
        custody=_parsed_custody(contract,row,None,None)
        selected_obj={'document_kind':key[0],'path':key[1],
                      'sha256':hashlib.sha256(body.body).hexdigest(),'size':len(body.body)}
        selected_bound=_selected(contract, selected_obj,held)
        from performing_contracts import consume
        performing=consume(context,held,'materials',key[0]+'/'+key[1],selected_obj)
        class_bind=bind_other_document(selected_obj, contract, custody, selected_bound, performing)
        runtime_join = class_bind['class_runtime_consumer']
        bound_metadata=_bind_metadata(row,contract,None,None,None,selected_obj,class_bind['material_status'],runtime_join)
        return {'document_kind':key[0],'path':key[1],'sha256':row['sha256'],
                'size':row['size'],'custody_body':custody,'raw_bound':True,
                'material_status':class_bind['material_status'],'cause':class_bind['cause'],
                'installed_observation_status':class_bind['installed_observation_status'],
                'actor_custody_sha256':class_bind['actor_custody_sha256'],
                'actor_custody':class_bind['actor_custody'],'performing_consumer':performing,
                'bound_metadata':bound_metadata,'runtime':'NOT_RUN'}

@document_scope
def assess_document_vector(expected,context,held,receipt_index):
    if context is None or context['document_contracts'] is None:
        return {'status':'NOT_PROVEN','cause':'full_document_contract_absent','indexes':[],
                'archives':[],'node':None,'other_documents':[],'all_documents_closed':False,
                'all_materials_closed':False,'runtime':'NOT_RUN','publisher_proof':False}
    contracts={}
    for c in context['document_contracts']:
        key=(c['document_kind'],c['path'])
        if key in contracts:raise ContractError('duplicate_receipt')
        contracts[key]=c
    if set(contracts)!=set(receipt_index):raise ContractError('receipt_membership')
    for key,row in receipt_index.items():
        if row['body_held'] is not True or not is_digest(row['sha256']):raise ContractError('body_not_paged')
    _STAGE[0]='ubuntu_receipt'; indexes=[]; index_by={}
    for index in expected['ubuntu_minimum']['indexes']:
        checkpoint(); package_expectations=[p for p in expected['ubuntu_minimum']['packages'] if (p['suite'],p['component'])==(index['suite'],index['component'])]
        result=_ubuntu_index(index,contracts,receipt_index,held,package_expectations)
        indexes.append(result); index_by[(index['suite'],index['component'])]=result
    archives=[]
    for p in expected['ubuntu_minimum']['packages']:
        checkpoint(); archives.append(_ubuntu_archive(p,index_by[(p['suite'],p['component'])],contracts,receipt_index,held))
    _STAGE[0]='node_receipt'; signed_key=('node-shasums256','SHASUMS256.txt'); archive_key=('node-archive',expected['node']['filename'])
    c=contracts[signed_key]; row=receipt_index[signed_key]; a_row=receipt_index[archive_key]
    node=None
    if c['capability'] is not None:
        with _Lease(held,*signed_key,row['sha256']) as signed, _Lease(held,*archive_key,a_row['sha256']) as archive:
            node=assess_node_chain(c['capability'],c['expected_capability'],c['approved_fingerprint'],
                c['node_authoritative'],signed.body,archive.body,load_pinned_schema('verifier-capability.v1'),
                load_pinned_schema('verification-result.v1'),load_pinned_schema('raw-publisher-receipt.v1'),
                result=c['verification_result'],expected_result_sha256=c['expected_result_sha256'],
                authority_archive_sha256=expected['node']['authoritative_sha256'])
            node['raw_custody_body']=_parsed_custody(c,row,c['verification_result'],c['capability'])
            node['archive_custody_body']=_parsed_custody(contracts[archive_key],a_row,None,None)
            selected={'filename':expected['node']['filename'],'sha256':node['archive_sha256'],
                      'size':node['archive_size'],'normalized_body_sha256':node['signed_body_sha256']}
            parsed_meta={k:node[k] for k in ('raw_document_sha256','signed_body_sha256','signature_armor_sha256')}
            node['bound_metadata']=_bind_metadata(row,c,parsed_meta,c['capability'],c['verification_result'],selected,node['proof_status'])
            archive_selected={'filename':expected['node']['filename'],'sha256':hashlib.sha256(archive.body).hexdigest(),'size':len(archive.body)}
            archive_contract=contracts[archive_key]
            node['archive_bound_metadata']=_bind_metadata(a_row,archive_contract,None,None,None,archive_selected,'STRUCTURALLY_BOUND')
            node['full_contract_closed']=bool(node['proof_status'] in ('AUTHENTICATED','STRUCTURALLY_BOUND') and
                _result_pin(c,c['verification_result']) and _selected(c,selected,held) and _selected(archive_contract,archive_selected,held) and
                node['raw_custody_body'] is not None and node['archive_custody_body'] is not None)
    consumed={('ubuntu-inrelease',x['index']['id']) for x in indexes}|{('ubuntu-packages',x['index']['id']+'/'+x['index']['member_name']) for x in indexes}|{('ubuntu-archive',x['package']['filename']) for x in archives}|{signed_key,archive_key}
    others=[]
    for key,row in receipt_index.items():
        if key in consumed:continue
        _STAGE[0]='bill'
        # A separate consumer frame releases each body after all its aliases
        # are gone. The vector retains JSON metadata, not 100+ body leases.
        others.append(_other_document(key,row,contracts[key],context,held))
    raw_closed=all(x['closed'] for x in indexes+archives) and node is not None and node['full_contract_closed'] and all(x['custody_body'] is not None for x in others)
    node_projection=join_node_projection(node, expected['node'] if type(expected) is dict else None)
    doc_closed=raw_closed and node_projection['status']=='STRUCTURALLY_BOUND' and all(x['material_status']=='STRUCTURALLY_BOUND' for x in others)
    return {'status':'STRUCTURALLY_BOUND' if doc_closed else 'NOT_PROVEN','cause':'separate_full_raw_consumers',
            'indexes':indexes,'archives':archives,'node':node,'other_documents':others,
            'all_documents_closed':doc_closed,'all_materials_closed':doc_closed,'node_projection':node_projection,
            'raw_documents_bound':raw_closed,
            'runtime':'NOT_RUN','publisher_proof':False}
