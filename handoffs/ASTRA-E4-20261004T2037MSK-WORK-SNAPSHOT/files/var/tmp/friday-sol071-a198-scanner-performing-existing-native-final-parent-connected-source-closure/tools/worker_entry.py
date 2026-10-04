"""Fixed future Source entry carried as a held text description on fd4.

Only native/source_owner.c enters it, after the Root-issued physical owner is
installed. fd7 is independently selected sealed metadata; it is never a Root
grant produced from Source JSON. This file was not executed by A141.
"""
import fcntl
import hashlib
import json
import os
import socket
import sys

import _friday_source_owner as native


def selected_bootstrap():
    before=os.fstat(7)
    seals=fcntl.fcntl(7,fcntl.F_GET_SEALS)
    required=fcntl.F_SEAL_SEAL|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_GROW|fcntl.F_SEAL_WRITE
    if before.st_uid!=0 or before.st_nlink!=0 or seals&required!=required:
        raise RuntimeError('actual Root sealed bootstrap description absent')
    # The native allocator/read owner already precedes this buffer/JSON graph.
    # Selected input transport belongs to the original Source domain too.
    limit=native.snapshot()['max_read_bytes']
    if before.st_size>limit: raise MemoryError('original Source bootstrap read ceiling')
    parts=[];remaining=before.st_size
    while remaining:
        piece=os.read(7,min(65536,remaining))
        if not piece: raise OSError('selected bootstrap short read')
        parts.append(piece);remaining-=len(piece)
    raw=b''.join(parts);parts.clear()
    if os.fstat(7)!=before: raise RuntimeError('selected bootstrap description changed')
    os.close(7)
    from source.canonical import canonical_loads
    return canonical_loads(raw,max_bytes=limit,max_depth=256)


def build_stock(boot):
    """Consume the Root-selected full bodies and immutable actual-object graph.

    Expected code/state/native hashes come exclusively from sealed issuer DATA.
    This builds object references, never manufactures their expected hashes.
    """
    import importlib
    from source.context import ROLES
    from source.pins import CODEC_PINS
    names=('__init__','bounds','broker_map','canonical','causes','compression',
        'context','contracts','controls','custody','deb_archive','digests','filename',
        'fixtures','guards','normalize','pins','public','raw_relations','schema_validate',
        'semantics','zip_wheel')
    modules={name:importlib.import_module('source' if name=='__init__' else 'source.'+name) for name in names}
    globals_selected={name:{key:value for key,value in vars(module).items() if not key.startswith('__')}
                      for name,module in modules.items()}
    from source.public import scan_retained,scan_retained_bytes
    from source.compression import decode_admitted
    from source.schema_validate import validate_document
    from source.zip_wheel import scan_wheel
    from source.deb_archive import scan_deb
    stock=boot['stock_context']
    stock['implementation_binding']['objects']={'scan_retained':scan_retained,'scan_retained_bytes':scan_retained_bytes,
        'decode_admitted':decode_admitted,'validate_document':validate_document,'scan_wheel':scan_wheel,'scan_deb':scan_deb}
    stock['implementation_binding']['source_modules']=modules
    stock['implementation_binding']['source_globals']=globals_selected
    codecs={}
    for method,item in boot['selected_codecs'].items():
        expected=CODEC_PINS[method]['implementation'].removeprefix('stdlib.')
        if item['module_name']!=expected: raise RuntimeError('independent original codec changed')
        codecs[method]={'module':importlib.import_module(expected),'sha256':item['sha256'],
            'protocol':item['protocol'],'allocation_contract':item['allocation_contract'],
            'evidence_raw':item['evidence_ascii'].encode('ascii')}
    # Close the independently selected runtime module set before any source
    # content/namespace capture. Lazy imports cannot acquire unseen native
    # allocation or mutable-state exemptions at a later decoder/effect.
    for name in sorted(stock['implementation_binding']['native_contract']['runtime_modules']):
        importlib.import_module(name)
    stock['codec_modules']=codecs
    stock['implementation_binding']['codec_objects']={key:value['module'] for key,value in codecs.items()}
    for role in ROLES:
        selected=stock['bodies'][role]
        raw=selected.pop('raw_ascii').encode('ascii')
        if hashlib.sha256(raw).hexdigest()!=selected['sha256']:
            raise RuntimeError('Root selected role body SHA differs')
        selected['raw']=raw
    from source.custody import RootHolderEndpoint
    if tuple(boot['root_peer'])!=(os.getppid(),0,0):
        raise RuntimeError('sealed original Root peer is not actual Source birth parent')
    topology=boot.get('parent_topology')
    if not isinstance(topology,dict) or set(topology)!={'mode','birth_parent_pid','generation'} or topology!={
        'mode':'existing-outside-native-parent-source-birth.v2','birth_parent_pid':os.getppid(),'generation':boot['generation']}:
        raise RuntimeError('sealed actual selected-parent topology differs')
    stock['root_holder']=RootHolderEndpoint(socket.socket(fileno=3),
        tuple(boot['root_peer']),boot['generation'])
    return stock


def main():
    if native.snapshot()['role']!='readonly-archive-scan':
        raise RuntimeError('selected Source native role absent')
    boot=selected_bootstrap()
    from source.public import scan_retained_bytes
    stock=build_stock(boot)
    raw=scan_retained_bytes(boot['index_ascii'].encode('ascii'),boot['held_ascii'].encode('ascii'),
                            boot['admission_ascii'].encode('ascii'),stock)
    offset=0
    while offset<len(raw):
        amount=os.write(1,memoryview(raw)[offset:offset+65536])
        if amount<1: raise OSError('exact final Source pipe short write')
        offset+=amount
    # The wire bytes have passed to the independent Root pipe owner. Physical
    # allocator free events, not this release declaration, retire live aliases.
    raw.release()


if __name__=='__main__':
    main()
