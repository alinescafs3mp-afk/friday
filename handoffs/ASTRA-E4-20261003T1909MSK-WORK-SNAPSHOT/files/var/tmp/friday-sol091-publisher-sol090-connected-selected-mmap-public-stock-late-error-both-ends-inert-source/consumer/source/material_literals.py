"""Actual literal PyPI selectors, separate from their explicit effective view.

An externally selected raw document and both complete selector expectations
are compared. Retained literal null and empty string are never conflated.
This is byte/selector correspondence, not publisher or installed acceptance.
"""
import json
import re
from canonical import _pairs, _reject_constant, _walk, canonical_bytes, parse_exact
from contract import ContractError
from resource_meter import HashlibProxy, json_preflight, checkpoint
from document_vector import _Lease
from body_scope import document_scope
from performing_contracts import expected_body
hashlib=HashlibProxy()

def _raw_json(raw):
    json_preflight(raw,2000000,12,512,4000000)
    try:
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=_pairs,parse_constant=_reject_constant)
    except (ValueError,UnicodeError,RecursionError,MemoryError) as exc:
        raise ContractError('wheel_observation_set') from exc
    _walk(value,1,12,512,4000000)
    return value

def _name(value):
    if type(value) is not str:raise ContractError('wheel_observation_set')
    return re.sub('[-_.]+','-',value).lower()

@document_scope
def consume_wheel_literals(context,held,items,views):
    if context is None or context['wheel_literal_contracts'] is None:
        return views
    supplied={}
    for c in context['wheel_literal_contracts']:
        if c['filename'] in supplied:raise ContractError('duplicate_observation')
        supplied[c['filename']]=c
    if set(supplied)!={i['filename'] for i in items}:raise ContractError('wheel_observation_set')
    by_name={v['filename']:v for v in views}; out=[]
    for item in items:
        checkpoint(); c=supplied[item['filename']]
        with _Lease(held,'member',c['raw_document_path'],c['raw_document_sha256']) as lease:
            raw=lease.body
            document=_raw_json(raw)
            if type(document) is not dict or type(document.get('info')) is not dict or type(document.get('urls')) is not list:
                raise ContractError('wheel_observation_set')
            info=document['info']
            if _name(info.get('name'))!=_name(item['name']) or info.get('version')!=item['version']:
                raise ContractError('wheel_observation_set')
            selected=[u for u in document['urls'] if type(u) is dict and u.get('filename')==item['filename']]
            if len(selected)!=1:raise ContractError('wheel_observation_set')
            selected=selected[0]
            expected_info=expected_body(held,c['package_info_selector_body'],c['package_info_selector_ref'])
            expected_selected=expected_body(held,c['selected_artifact_selector_body'],c['selected_artifact_selector_ref'])
            if info!=expected_info or selected!=expected_selected:
                raise ContractError('selected_record')
            if type(selected.get('digests')) is not dict or selected['digests'].get('sha256')!=item['sha256'] or type(selected.get('size')) is not int or selected['size']!=item['size']:
                raise ContractError('wheel_observation')
            for k in ('url','packagetype','yanked'):
                if selected.get(k)!=c[k]:raise ContractError('wheel_observation_set')
            if c['packagetype']!='bdist_wheel' or type(c['url']) is not str or not c['url'].startswith('https://') or type(c['yanked']) is not bool:
                raise ContractError('wheel_observation_set')
            if 'requires_python' not in info or 'requires_python' not in selected:
                raise ContractError('requires_python')
            raw_info=info['requires_python']; raw_selected=selected['requires_python']
            if raw_info!=c['package_info_requires_python_raw'] or raw_selected!=c['selected_artifact_requires_python_raw']:
                raise ContractError('requires_python_normalization')
            normalize=lambda value:None if value in (None,'') else value
            if any(v is not None and type(v) is not str for v in (raw_info,raw_selected)):
                raise ContractError('requires_python')
            if normalize(raw_info)!=normalize(raw_selected) or normalize(raw_info)!=item['requires_python']:
                raise ContractError('requires_python_normalization')
            view=dict(by_name[item['filename']])
            view.update({'metadata_raw_sha256':hashlib.sha256(raw).hexdigest(),
                         'historical_metadata_raw_sha256':item['metadata_raw_sha256'],
                         'package_info_selector':info,'selected_artifact_selector':selected,
                         'package_info_selector_sha256':hashlib.sha256(canonical_bytes(info)).hexdigest(),
                         'selected_artifact_selector_sha256':hashlib.sha256(canonical_bytes(selected)).hexdigest(),
                         'requires_python_raw':raw_info,'selected_artifact_requires_python_raw':raw_selected,
                         'requires_python_effective':normalize(raw_info),
                         'normalization':'empty_string_to_null' if raw_info=='' else 'identity',
                         'literal_selector_status':'STRUCTURALLY_BOUND',
                         'selector_bound_bytes':2000000,
                         'actual_package_info_selector_bytes':len(canonical_bytes(expected_info)),
                         'actual_selected_artifact_selector_bytes':len(canonical_bytes(expected_selected)),
                         'selector_adequacy':'FULL_COMPARE_WITHIN_ORIGINAL_DOCUMENT_CAP_NOT_WHOLE_DATASET_PROOF',
                         'installed_body_runtime_custody_status':'NOT_PROVEN','publisher_proof':False})
            out.append(view)
            del raw
    return out
