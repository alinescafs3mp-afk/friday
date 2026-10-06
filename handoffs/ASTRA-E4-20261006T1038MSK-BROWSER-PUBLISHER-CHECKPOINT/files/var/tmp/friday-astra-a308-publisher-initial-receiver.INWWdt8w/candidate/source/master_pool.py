"""NEW SAME-role bridge to the native Root's one persistent master pool.

This is an owned view, not an issuer/cap or a second observer. C counters and
native retained allocation outlive clearing every Source __dict__. All methods
require the existing native context; fork shadows cannot spend parent credit.
No wrapper object or Python dictionary can provision the native pool.
"""
from common import Refused

class MasterPool:
    def __init__(self):
        from existing_root_caller import _native
        self.native=_native()
        self.context=self.native.current()
        if self.context is None:raise Refused('actual_original_master_pool_required')
        self.owner_pid=self.native.master_owner(self.context)
    def change(self,op,token=0,reads=0,output=0,hash_bytes=0,allocation=0,slots=0):
        return self.native.master_change(self.context,op,token,reads,output,
                                        hash_bytes,allocation,slots)
    def reserve(self,reads=0,output=0,hash_bytes=0,allocation=0,slots=0):
        return self.change('reserve',reads=reads,output=output,hash_bytes=hash_bytes,
                           allocation=allocation,slots=slots)
    def commit(self,token,reads=0,output=0,hash_bytes=0):
        return self.change('commit',token,reads,output,hash_bytes)
    def release(self,token):return self.change('release',token)
    def grow(self,token,allocation):return self.change('grow',token,allocation=allocation)
    def retain(self,token,allocation):return self.change('retain',token,allocation=allocation)
    def retire_slots(self,token):return self.change('slots',token)
    def preowner(self,allocation,slots):
        return self.change('preowner',allocation=allocation,slots=slots)
    def spent(self,reads=0,hash_bytes=0):
        return self.change('spent',reads=reads,hash_bytes=hash_bytes)
    def observe(self,reads,output,ram,workers):
        return self.change('observe',reads=reads,output=output,allocation=ram,slots=workers)
    def transfer_output(self,source,destination,amount):
        # token=source, reads=destination are integer identity scalars, not IO.
        return self.change('transfer',source,reads=destination,output=amount)
    def detach(self):return self.change('detach')
    def state(self):return self.native.master_state(self.context)
