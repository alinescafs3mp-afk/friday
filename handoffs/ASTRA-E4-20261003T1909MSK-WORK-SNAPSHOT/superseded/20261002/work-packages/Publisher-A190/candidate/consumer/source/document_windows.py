"""Future read-only page inputs selected by the independent stock caller.

This object owns metadata only, not resident page bodies. A caller supplies
regular file page pins, not executable callbacks. All bodies are call-local;
get_document does not retain an assembly. No file is created or modified.
This Source does not approve or run any supplied body.
"""
import os
import stat
from contract import ContractError, is_digest, MAX_PACKAGES_BYTES
from resource_meter import HashlibProxy, current, reserve_allocation, checkpoint, fd_scope
hashlib=HashlibProxy()

def _nine(s):
    return list(map(str,(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_gid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)))

class FilePageSource:
    def __init__(self, page_files):
        if type(page_files) is not list or len(page_files)>512:
            raise ContractError('held_stream')
        reserve_allocation(len(page_files)*8192+64)
        self.files={}
        for pin in page_files:
            if type(pin) is not dict or set(pin)!={'kind','path','page_index','file_path','identity9','sha256'}:
                raise ContractError('held_stream')
            if type(pin['kind']) is not str or pin['kind'] not in ('member','node-archive','node-shasums256','ubuntu-archive','ubuntu-inrelease','ubuntu-packages','wheel','kernel','native','data','browser','candidate','golden','unrar','custody') or type(pin['path']) is not str or not 1<=len(pin['path'])<=240 or '\x00' in pin['path']:
                raise ContractError('held_stream')
            key=(pin['kind'],pin['path'],pin['page_index'])
            if key in self.files or type(pin['page_index']) is not int or pin['page_index']<0:
                raise ContractError('held_stream')
            identity=pin['identity9']
            if type(identity) is not list or len(identity)!=9 or any(type(v) is not str or not 1<=len(v)<=24 or not v.isascii() or not v.isdigit() for v in identity):
                raise ContractError('held_stream')
            if type(pin['file_path']) is not str or not pin['file_path'].startswith('/') or not is_digest(pin['sha256']):
                raise ContractError('held_stream')
            if len(pin['file_path'])>4096 or '\x00' in pin['file_path'] or any(p in ('','.','..') for p in pin['file_path'].split('/')[1:]):
                raise ContractError('held_stream')
            self.files[key]=dict(pin)
        self.documents=None
    def bind(self,context,meter):
        chosen=context['page_sequence'] if context['page_sequence'] is not None else context['streams']
        if type(chosen) is not list or len(chosen)>512:
            raise ContractError('held_stream')
        reserve_allocation(len(chosen)*8192+64)
        docs={};keys=set()
        for item in chosen:
            key=(item['kind'],item['path'],item['page_index'])
            if key in keys or key not in self.files or self.files[key]['sha256']!=item['sha256']:
                raise ContractError('held_stream')
            keys.add(key); docs.setdefault(key[:2],[]).append(dict(item))
        if keys!=set(self.files):raise ContractError('held_stream')
        for key,pages in docs.items():
            pages.sort(key=lambda p:p['page_index'])
            first=pages[0]; size=first['size']
            if type(size) is not int or not 0<=size<=MAX_PACKAGES_BYTES:
                raise ContractError('aggregate_budget')
            if [p['page_index'] for p in pages]!=list(range(first['page_count'])):
                raise ContractError('held_stream')
            if any(p['page_count']!=len(pages) or p['size']!=size or p['body_sha256']!=first['body_sha256'] or p['custody_sha256']!=first['custody_sha256'] for p in pages):
                raise ContractError('held_stream')
            if sum(p['page_size'] for p in pages)!=size or not is_digest(first['body_sha256']):
                raise ContractError('held_stream')
            meter.charge_document(size)
        self.documents=docs; self.meter=meter
        return self
    def get_document(self,kind,path,digest=None):
        if self.documents is None:raise ContractError('held_stream')
        pages=self.documents.get((kind,path))
        if pages is None:return None
        expected=pages[0]['body_sha256']
        if digest is not None and expected!=digest:raise ContractError('held_stream')
        size=pages[0]['size']; reserve_allocation(size*2+65536)
        self.meter.charge_slots(1)
        assembly=bytearray(); document_hash=hashlib.sha256()
        try:
            for page in pages:
                pin=self.files[(kind,path,page['page_index'])]
                before=os.stat(pin['file_path'],follow_symlinks=False)
                if _nine(before)!=pin['identity9'] or not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size!=page['page_size']:
                    raise ContractError('held_stream')
                with fd_scope(pin['file_path'],page['page_size'],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC) as fd:
                    if _nine(os.fstat(fd))!=pin['identity9']:raise ContractError('held_stream')
                    page_hash=hashlib.sha256(); total=0
                    while total<page['page_size']:
                        request=min(65536,page['page_size']-total)
                        self.meter.charge_read(request); self.meter.charge_slots(1)
                        try:
                            part=os.read(fd,request)
                            if not part:raise ContractError('held_stream')
                            total+=len(part); page_hash.update(part); document_hash.update(part)
                            assembly.extend(part)
                            del part
                        finally:self.meter.release_slots(1)
                    self.meter.charge_read(1)
                    if os.read(fd,1)!=b'':raise ContractError('held_stream')
                    if page_hash.hexdigest()!=pin['sha256'] or _nine(os.fstat(fd))!=pin['identity9'] or _nine(os.stat(pin['file_path'],follow_symlinks=False))!=pin['identity9']:
                        raise ContractError('held_stream')
            if len(assembly)!=size or document_hash.hexdigest()!=expected:
                raise ContractError('held_stream')
            # The caller owns this returned lease. This provider retains no body.
            return bytes(assembly)
        finally:
            assembly.clear(); self.meter.release_slots(1)
    def get(self,digest):
        if self.documents is None:raise ContractError('held_stream')
        selected=[key for key,pages in self.documents.items() if pages[0]['body_sha256']==digest or (len(pages)==1 and pages[0]['sha256']==digest)]
        if len(selected)>1:raise ContractError('ambiguous_document')
        if not selected:return None
        from document_vector import _Lease
        lease=_Lease(self,*selected[0],self.documents[selected[0]][0]['body_sha256'])
        lease.__enter__()
        return lease.body
