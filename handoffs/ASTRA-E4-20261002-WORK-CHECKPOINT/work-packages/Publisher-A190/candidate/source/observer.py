"""Root-owned prospective budget and real kernel observations.

The supervisor, never the performer, owns this object. Reservations are not
observations. Absolute inherited peaks are retained without subtraction.
"""
import os
import contextlib
import resource
import stat
import time
import types
from common import (Refused, READ_MAX, OUTPUT_MAX, RAM_MAX, SLOTS_MAX,
                    WORKERS_MAX, WALL_MAX, REFUSAL_RESERVE, mono, integer,
                    canonical, parse, error_fact)
from custody import open_absolute, identity9
from lifetime import OwnedFDs, FDState, FD_HISTORY_ALLOCATION

_SOURCE_OBJECT_MODULES=frozenset(('actor_context','admission','capacity','class_semantics','common',
    'consumer_bridge','custody','extraction','fact_bridge','independent_selector','launcher','lifetime',
    'native','normalization','observer','operations','retention','roles','root_tool_adapter','snapshot_producer',
    'actor_bootstrap','authority','bill','body_scope','canonical','capability','contract','declared_controls',
    'document_vector','document_windows','effects','fixtures','formats','ingress','material_literals',
    'performing_contracts','pins','producer_consumer','receipt_chain','recipe_planner','resource_meter',
    'runtime_consumer','schema_validate','semantics','whole_join'))

def full_value_arena(roots):
    """Actual bounded stock value preimages, preserving repeated identities.

    Unsupported stock/Source objects remain explicit incompleteness. Their
    identity is diagnostic and can never cause a complete receiver receipt.
    No arbitrary interpreter heap is presumed enrolled by this encoder.
    """
    nodes=[];seen={};unsupported=[]
    def ref(value):
        key=id(value)
        if key in seen:return seen[key]
        index=len(nodes)
        if index>=262144:raise Refused('full_value_node_capacity','terminal')
        seen[key]=index;nodes.append(None)
        t=type(value)
        if value is None:row=['none',None]
        elif t is bool:row=['bool',value]
        elif t is int:row=['int',str(value)]
        elif t is str:row=['str',value]
        elif t in (bytes,bytearray):
            row=['bytes' if t is bytes else 'bytearray',
                [bytes(value[i:i+16384]).hex() for i in range(0,len(value),16384)]]
        elif t in (list,tuple):row=['list' if t is list else 'tuple',[ref(v) for v in value]]
        elif t is dict:row=['dict',[[ref(k),ref(v)] for k,v in value.items()]]
        elif isinstance(value,BaseException):
            row=['error',{'module':t.__module__,'class':t.__name__,'args':ref(value.args),
                'attributes':ref(value.__dict__),'cause':ref(value.__cause__),
                'context':ref(value.__context__),'suppress_context':value.__suppress_context__,
                'traceback':ref(value.__traceback__)}]
        elif t is types.TracebackType:
            row=['traceback',{'next':ref(value.tb_next),'frame':ref(value.tb_frame),
                'line':value.tb_lineno,'lasti':value.tb_lasti}]
        elif t is types.FrameType:
            # Globals are actual mutable frame aliases, not assumed immutable
            # merely because their code came from a reviewed source snapshot.
            row=['frame',{'filename':value.f_code.co_filename,'name':value.f_code.co_name,
                'line':value.f_lineno,'locals':ref(value.f_locals),'globals':ref(value.f_globals)}]
        elif t.__module__ in _SOURCE_OBJECT_MODULES and hasattr(value,'__dict__'):
            # Every actual mutable instance field is included, including meter,
            # parent_state, callbacks and owner backedges. Unknown callback or
            # stock state is still explicit incompleteness, not a skipped alias.
            row=['Source-object',{'module':t.__module__,'class':t.__name__,
                'state':ref(value.__dict__)}]
        elif t.__name__=='StockChildJournal' and t.__module__=='__main__' and hasattr(value,'__dict__'):
            row=['Source-object',{'module':'actor_bootstrap','class':'StockChildJournal',
                'state':ref(value.__dict__)}]
        elif t is types.MethodType:
            row=['bound-method',{'self':ref(value.__self__),'function':ref(value.__func__)}]
        elif t is memoryview:
            row=['memoryview',{'object':ref(value.obj),'full_bytes':ref(value.tobytes()),
                'format':value.format,'shape':ref(value.shape),'strides':ref(value.strides),
                'readonly':value.readonly}]
        else:
            unsupported.append({'identity':str(key),'module':t.__module__,'class':t.__name__})
            row=['unsupported',str(key)]
        nodes[index]=row
        return index
    selected=[ref(value) for value in roots]
    return {'schema':'friday.a190.stock-full-value-arena.v1','roots':selected,
        'node_count':len(nodes),'chunks':[nodes[i:i+512] for i in range(0,len(nodes),512)],
        'unsupported':unsupported,'complete':not unsupported,'truncated':False}

def validate_full_value_arena(arena):
    """Independent nested acceptance of every body/index/count, not a label."""
    from common import exact,integer
    exact(arena,('schema','roots','node_count','chunks','unsupported','complete','truncated'),'full_value_arena')
    if arena['schema']!='friday.a190.stock-full-value-arena.v1' or arena['truncated'] is not False or arena['complete'] is not True or arena['unsupported']!=[]:
        raise Refused('full_value_arena_incomplete','terminal')
    count=integer(arena['node_count'],262144)
    nodes=[row for chunk in arena['chunks'] for row in chunk]
    if len(nodes)!=count or any(type(chunk) is not list or len(chunk)>512 for chunk in arena['chunks']):raise Refused('full_value_arena_count','terminal')
    def index(i):
        if type(i) is not int or not 0<=i<count:raise Refused('full_value_arena_reference','terminal')
    for i in arena['roots']:index(i)
    for row in nodes:
        if type(row) is not list or len(row)!=2:raise Refused('full_value_arena_row','terminal')
        kind,body=row
        if kind=='none':
            if body is not None:raise Refused('full_value_arena_scalar','terminal')
        elif kind=='bool':
            if type(body) is not bool:raise Refused('full_value_arena_scalar','terminal')
        elif kind=='int':
            if type(body) is not str or str(int(body))!=body:raise Refused('full_value_arena_integer','terminal')
        elif kind=='str':
            if type(body) is not str:raise Refused('full_value_arena_scalar','terminal')
        elif kind in ('bytes','bytearray'):
            if type(body) is not list:raise Refused('full_value_arena_bytes','terminal')
            for part in body:
                if type(part) is not str or len(part)>32768 or len(part)%2 or any(c not in '0123456789abcdef' for c in part):raise Refused('full_value_arena_bytes','terminal')
        elif kind in ('list','tuple'):
            if type(body) is not list:raise Refused('full_value_arena_sequence','terminal')
            for i in body:index(i)
        elif kind=='dict':
            if type(body) is not list:raise Refused('full_value_arena_dict','terminal')
            for pair in body:
                if type(pair) is not list or len(pair)!=2:raise Refused('full_value_arena_dict','terminal')
                index(pair[0]);index(pair[1])
        elif kind=='error':
            exact(body,('module','class','args','attributes','cause','context','suppress_context','traceback'),'full_value_error')
            for name in ('args','attributes','cause','context','traceback'):index(body[name])
            if type(body['module']) is not str or type(body['class']) is not str or type(body['suppress_context']) is not bool:raise Refused('full_value_error_type','terminal')
        elif kind=='traceback':
            exact(body,('next','frame','line','lasti'),'full_value_traceback');index(body['next']);index(body['frame'])
            if type(body['line']) is not int or type(body['lasti']) is not int:raise Refused('full_value_traceback','terminal')
        elif kind=='frame':
            exact(body,('filename','name','line','locals','globals'),'full_value_frame');index(body['locals']);index(body['globals'])
            if type(body['filename']) is not str or type(body['name']) is not str or type(body['line']) is not int:raise Refused('full_value_frame','terminal')
        elif kind=='Source-object':
            exact(body,('module','class','state'),'full_value_Source_object')
            if body['module'] not in _SOURCE_OBJECT_MODULES or type(body['class']) is not str:raise Refused('full_value_Source_type','terminal')
            index(body['state'])
        elif kind=='bound-method':
            exact(body,('self','function'),'full_value_bound_method');index(body['self']);index(body['function'])
        elif kind=='memoryview':
            exact(body,('object','full_bytes','format','shape','strides','readonly'),'full_value_memoryview')
            for name in ('object','full_bytes','shape','strides'):index(body[name])
            if type(body['readonly']) is not bool or type(body['format']) is not str:raise Refused('full_value_memoryview','terminal')
        else:raise Refused('full_value_arena_unsupported','terminal')
    return nodes


def raw_file(path, maximum=65536, owner=None):
    if owner is None:raise Refused("raw_file_existing_owner")
    credit=owner.observation_credit if hasattr(owner,"observation_credit") else owner
    book = OwnedFDs(credit=credit)
    credit.retain_journal(book)
    fd = -1
    try:
        credit.before_read(maximum+1)
        fd = open_absolute(path, journal=book)
        credit.before_read(maximum+1)
        raw = os.read(fd, maximum+1)
        credit.read_debit(len(raw))
        if len(raw) > maximum: raise Refused("observer_raw_size")
        return raw.decode("ascii")
    finally:
        if fd >= 0:
            book.close_one(fd)
        else:
            book.close()
        if book.fds:
            import sys
            current = sys.exc_info()[1]
            fact = {"fds": [{"fd": held, "holder": book.meta.get(held, {}).get("holder"),
                "credit": book.meta.get(held, {}).get("credit"),
                "identity9_decimal_strings": book.meta.get(held, {}).get("identity9_decimal_strings"),
                "status": book.meta.get(held, {}).get("status")} for held in sorted(book.fds)]}
            if current is None:
                exc = Refused("temporary_fd_close_unconfirmed", detail=fact)
                exc.retained_journal = book
                raise exc
            current.retained_journal = book
        else:
            credit.journals.remove(book)


def proc_start(pid, owner):
    raw = raw_file("/proc/"+str(pid)+"/stat", 8192,owner)
    close = raw.rfind(")")
    if close < 0: raise Refused("process_identity")
    fields = raw[close+2:].split()
    return {"pid":pid,"start_ticks":int(fields[19]),"raw_stat":raw}


def proc_io(pid, owner):
    raw = raw_file("/proc/"+str(pid)+"/io", 8192,owner)
    rows = {}
    for line in raw.splitlines():
        k, v = line.split(":", 1)
        if k in rows or not v.strip().isdigit(): raise Refused("observer_io")
        rows[k] = int(v.strip())
    required = {"rchar","wchar","syscr","syscw","read_bytes","write_bytes","cancelled_write_bytes"}
    if set(rows) != required: raise Refused("observer_io")
    return {"raw":raw,"counters":rows}


class Reservation:
    def __init__(self, meter, token, slots=0):
        self.meter, self.token, self.closed = meter, token, False
        self.slots=slots
    def commit(self, reads=0, output=0, hash_bytes=0):
        if self.closed: raise Refused("retired_reservation")
        self.meter.commit(self.token, reads, output, hash_bytes)
    def release(self):
        if not self.closed:
            if getattr(self,'fd_rows',{}):return False
            accepted=self.meter.release(self.token)
            if accepted is False:return False
            self.closed = True
        return True
    def retire_slots(self):
        if self.closed or getattr(self,'fd_rows',{}):return False
        self.meter.retire_slots(self.token);self.slots=0;return True


class RootObserver:
    """Independent actual parent owns actor, every native child and terminal."""
    def __init__(self, cgroup, admission, observer_id, started_ns=None, carried_hash=0, preowner=None):
        self.owner_pid = os.getpid()
        self.observer_id, self.cgroup, self.admission = observer_id, cgroup, admission
        self.started = mono() if started_ns is None else started_ns
        if type(self.started) is not int or not 0<self.started<=mono():raise Refused("observer_wall_anchor")
        self.deadline = self.started+WALL_MAX*10**9
        self.reserve_deadline = self.deadline-600*10**9
        self.explicit_read = self.explicit_output = self.live_alloc = self.live_slots = 0
        self.explicit_hash = integer(carried_hash, READ_MAX)
        self.peak_slots = self.peak_alloc = 0
        self.pending, self.next_token, self.events, self.errors = {}, 1, [], []
        self.last_observed_physical=0
        self.reservation_preview_read=self.reservation_preview_hash=0
        self.processes, self.pidfds, self.final_io, self.waits = {}, {}, {}, []
        self.partial, self.cleanup_faults = [], []
        self.owned_results={}
        self.owned_digests={}
        self.held_leases={}
        self.local_owners={};self.local_serial=0
        if preowner is None or preowner.pid!=self.owner_pid or preowner.closed:
            raise Refused("observer_existing_stock_credit")
        self.observation_credit=preowner;preowner.observer=self
        self.fd_state=preowner.fd_state
        self.explicit_read=preowner.read_bytes
        self.pending[preowner.token]={"purpose":"same-root-stock-observation-arena",
            "reads":0,"output":0,"hash_bytes":0,"allocation":preowner.allocation,"slots":preowner.slots}
        self.live_alloc=preowner.allocation;self.live_slots=preowner.slots
        # Full stdout/stderr and the terminal envelope have separate physical
        # preimages; pipe capture is also actual output. Credit is not a meter.
        self.terminal_credit = REFUSAL_RESERVE+INPUT_MAX_TERMINAL*6
        self.root_io_before = proc_io(self.owner_pid,self)
        self.root_usage_before = resource.getrusage(resource.RUSAGE_SELF)
        self.before = self.sample()
        if self.before["pids_current"] != 0 or self.before["processes"] or self.before["memory_current"] != 0:
            raise Refused("cgroup_not_empty")
        # Externally provisioned cgroup limits are verified, never raised here.
        if self.before["memory_max"] != RAM_MAX or self.before["pids_max"] != WORKERS_MAX-1:
            raise Refused("cgroup_limits")
        if self.before["memory_peak"] != 0:
            raise Refused("cgroup_prior_peak")
        self.register(self.owner_pid, None, external_root=True)
        self.terminal_hold = self._reserve("full-terminal-forward", output=self.terminal_credit,
            reads=INPUT_MAX_TERMINAL*8, allocation=INPUT_MAX_TERMINAL*8+REFUSAL_RESERVE, slots=0,
            hash_bytes=INPUT_MAX_TERMINAL*4, final=True)

    def _owned(self):
        if os.getpid() != self.owner_pid: raise Refused("observer_owner")

    def before_physical_sample(self,maximum,additional_hash=0):
        """Guard each real sample against the complete current Root debit.

        The max is one physical observation, never added twice to explicit
        physical reads. Pending memory-hash passes remain separately additive.
        """
        integer(maximum,READ_MAX);integer(additional_hash,READ_MAX)
        physical=max(self.explicit_read,self.last_observed_physical)
        pending_read=sum(r["reads"] for r in self.pending.values())
        pending_hash=sum(r["hash_bytes"] for r in self.pending.values())
        if physical+self.explicit_hash+pending_read+pending_hash+maximum+additional_hash+self.reservation_preview_read+self.reservation_preview_hash>READ_MAX:
            raise Refused("aggregate_read")

    def sample(self):
        raw = {}
        for name in ("memory.current","memory.peak","memory.max","pids.current",
                     "pids.max","cgroup.procs","io.stat","cgroup.events"):
            raw[name] = raw_file(self.cgroup+"/"+name,owner=self)
        def number(name):
            value = raw[name].strip()
            if not value.isdigit(): raise Refused("observer_metric_missing")
            return int(value)
        processes = []
        for value in raw["cgroup.procs"].split():
            if not value.isdigit(): raise Refused("observer_processes")
            processes.append(int(value))
        if len(processes) > WORKERS_MAX-1 or len(set(processes)) != len(processes):
            raise Refused("worker_cap")
        block_read = block_write = 0
        for line in raw["io.stat"].splitlines():
            fields = line.split()
            if not fields or ":" not in fields[0]: raise Refused("observer_io")
            nums = {}
            for field in fields[1:]:
                k, v = field.split("=",1)
                if k in nums or not v.isdigit(): raise Refused("observer_io")
                nums[k] = int(v)
            if not {"rbytes","wbytes","rios","wios"}.issubset(nums): raise Refused("observer_io")
            block_read += nums["rbytes"]; block_write += nums["wbytes"]
        # An empty io.stat is an actual empty kernel counter set; its complete
        # raw file is retained. Failure to read it is not converted to zero.
        return {"at_ns":mono(),"raw":raw,"memory_current":number("memory.current"),
            "memory_peak":number("memory.peak"),"memory_max":number("memory.max"),
            "pids_current":number("pids.current"),"pids_max":number("pids.max"),
            "processes":processes,"block_read":block_read,"block_write":block_write}

    def register(self, pid, pidfd, external_root=False):
        self._owned()
        if pid in self.processes: raise Refused("duplicate_process")
        # Complete history itself participates in the original ABI128. No
        # truncation or exporting only active processes can satisfy that ABI.
        if len(self.processes) >= SLOTS_MAX: raise Refused("process_history_cap")
        fact = proc_start(pid,self)
        if not external_root:
            current = self.sample()
            if pid not in current["processes"]: raise Refused("child_cgroup")
            if pidfd is None: raise Refused("child_pidfd")
        self.processes[pid] = fact
        if pidfd is not None: self.pidfds[pid] = pidfd

    def sample_processes(self):
        self._owned()
        io = {}; missing = []
        for pid, identity in self.processes.items():
            if pid in self.final_io:
                io[pid] = self.final_io[pid]
                continue
            try:
                if proc_start(pid,self)["start_ticks"] != identity["start_ticks"]:
                    raise Refused("pid_reused")
                io[pid] = proc_io(pid,self)
            except (OSError, Refused) as exc:
                missing.append({"pid":pid,"error":error_fact(exc,"execution")})
        return io, missing

    def check(self, final=False):
        self._owned()
        now = mono()
        if now > (self.deadline if final else self.reserve_deadline):
            raise Refused("whole_deadline", "terminal" if final else "execution")
        sample = self.sample()
        if any(pid not in self.processes for pid in sample["processes"]):raise Refused("unexpected_unowned_process","execution")
        io, missing = self.sample_processes()
        if missing: raise Refused("actual_io_unknown", "execution", missing)
        actual_read = sum(row["counters"]["rchar"] for row in io.values())
        actual_write = sum(row["counters"]["wchar"] for row in io.values())
        physical_read=sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"]
        physical_write=sample["block_write"]+io[self.owner_pid]["counters"]["write_bytes"]
        # Sum of absolute maxima is conservative over the actual simultaneous
        # aggregate, and includes all parent preparation/import/terminal costs.
        own_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
        upper_peak = sample["memory_peak"]+own_peak
        pending_read = sum(r["reads"] for r in self.pending.values())
        pending_hash = sum(r["hash_bytes"] for r in self.pending.values())
        pending_output = sum(r["output"] for r in self.pending.values())
        physical = max(self.explicit_read, actual_read, physical_read)
        self.last_observed_physical=physical
        transport = max(self.explicit_output, actual_write, physical_write)
        # Memory-hash passes are additive. Kernel rchar does not measure them.
        if physical + self.explicit_hash + pending_read + pending_hash > READ_MAX: raise Refused("aggregate_read")
        if transport + pending_output > OUTPUT_MAX: raise Refused("aggregate_output")
        if upper_peak > RAM_MAX: raise Refused("aggregate_ram")
        if sample["pids_current"]+1 > WORKERS_MAX: raise Refused("worker_cap")
        # Unknown values are fatal to a successful receipt, never labeled OBSERVED.
        return sample, io, upper_peak

    def _reserve(self, purpose, reads=0, output=0, allocation=0, slots=0, hash_bytes=0, final=False):
        self._owned()
        for value, cap in ((reads,READ_MAX),(output,OUTPUT_MAX),(allocation,RAM_MAX),(slots,SLOTS_MAX),(hash_bytes,READ_MAX)):
            integer(value, cap)
        self.reservation_preview_read=reads;self.reservation_preview_hash=hash_bytes
        try:sample,io,peak=self.check(final)
        finally:self.reservation_preview_read=self.reservation_preview_hash=0
        if self.live_slots+slots > SLOTS_MAX or peak+self.live_alloc+allocation > RAM_MAX:
            raise Refused("capacity_before_effect")
        actual_read=max(self.explicit_read,sum(r["counters"]["rchar"] for r in io.values()),sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"])
        actual_output=max(self.explicit_output,sum(r["counters"]["wchar"] for r in io.values()),sample["block_write"]+io[self.owner_pid]["counters"]["write_bytes"])
        pending_hash=sum(x["hash_bytes"] for x in self.pending.values())
        if actual_read+sum(x["reads"] for x in self.pending.values())+reads+self.explicit_hash+pending_hash+hash_bytes > READ_MAX:
            raise Refused("aggregate_read")
        if actual_output+sum(x["output"] for x in self.pending.values())+output > OUTPUT_MAX:
            raise Refused("aggregate_output")
        token = self.next_token
        # Commit accounting only after all fallible checks.
        self.next_token += 1
        self.pending[token] = {"purpose":purpose,"reads":reads,"output":output,
                               "allocation":allocation,"slots":slots,"hash_bytes":hash_bytes}
        self.live_alloc += allocation; self.live_slots += slots
        self.peak_alloc = max(self.peak_alloc,self.live_alloc)
        self.peak_slots = max(self.peak_slots,self.live_slots)
        return Reservation(self, token,slots)

    def reserve(self, purpose, reads=0, output=0, allocation=0, slots=0, hash_bytes=0):
        return self._reserve(purpose,reads,output,allocation,slots,hash_bytes=hash_bytes)

    def grow(self,token,allocation=0):
        self._owned();integer(allocation,RAM_MAX)
        row=self.pending.get(token)
        if row is None:raise Refused("retired_reservation")
        _,_,peak=self.check()
        if peak+self.live_alloc+allocation>RAM_MAX:raise Refused("capacity_before_effect")
        row["allocation"]+=allocation;self.live_alloc+=allocation
        self.peak_alloc=max(self.peak_alloc,self.live_alloc)

    def commit(self, token, reads=0, output=0, hash_bytes=0):
        self._owned()
        row = self.pending.get(token)
        if row is None or reads < 0 or output < 0 or hash_bytes < 0 or reads > row["reads"] or output > row["output"] or hash_bytes > row["hash_bytes"]:
            raise Refused("reservation_commit")
        row["reads"] -= reads; row["output"] -= output; row["hash_bytes"] -= hash_bytes
        self.explicit_read += reads; self.explicit_output += output; self.explicit_hash += hash_bytes

    def release(self, token):
        """Retirement remains finite under exhaustion and preserves first error."""
        self._owned()
        for book in self.fd_state.journals:
            credit=book.grants.get(token)
            if credit is not None and getattr(credit,'fd_rows',{}):return False
        if token==self.fd_state.credit_token and self.fd_state.journals and not getattr(self,'fd_history_completion_received',False):return False
        row = self.pending.pop(token, None)
        if row is None:
            self.note_cleanup("unknown-reservation"); return False
        self.live_alloc -= row["allocation"]; self.live_slots -= row["slots"]
        return True

    def retire_slots(self,token):
        self._owned();row=self.pending.get(token)
        if row is None:raise Refused('retired_reservation','terminal')
        self.live_slots-=row['slots'];row['slots']=0

    def retain_allocation(self,token,allocation):
        """Retain a proven recipient maximum after confirmed child memory ends.

        This is a same-token lifetime narrowing, never admission or an RSS
        substitution. The caller keeps all escaping recipient arenas charged.
        """
        self._owned();integer(allocation,RAM_MAX)
        row=self.pending.get(token)
        if row is None or allocation>row['allocation']:raise Refused('recipient_allocation_identity','terminal')
        self.live_alloc-=row['allocation']-allocation;row['allocation']=allocation

    def accept_capture(self,capture_id,capture,metadata_owner,metadata_hold):
        self._owned()
        owned=self.owned_results.get(id(metadata_owner))
        if owned is None or owned[0] is not metadata_owner or metadata_hold not in owned[1] or metadata_hold.closed:
            raise Refused('capture_recipient_credit','terminal')
        if metadata_owner.get('capture') is not capture:raise Refused('capture_recipient_alias','terminal')
        if not hasattr(self,'accepted_captures'):self.accepted_captures={}
        if capture_id in self.accepted_captures:raise Refused('capture_duplicate_acceptance','terminal')
        self.accepted_captures[capture_id]=(metadata_owner,capture,metadata_hold)
        return {'status':'ACTUAL_SAME_ROOT_FULL_CAPTURE_ACCEPTED','capture_id':capture_id,
            'owner_pid':self.owner_pid,'metadata_token':metadata_hold.token,
            'raw_stdout_identity':str(id(capture['raw_stdout'])),'raw_stderr_identity':str(id(capture['raw_stderr'])),
            'full_raw_sizes':[len(capture['raw_stdout']),len(capture['raw_stderr'])],
            'hash_status':'FULL_SAME_OBJECT' if set(capture['digests'])=={'stdout','stderr'} else 'UNKNOWN_NOT_ZERO_NOT_PROVEN',
            'ordinary_native_grant_retirement_confirmed':False,'ownership_retired':False}

    def note_partial(self, path, size):
        self.partial.append({"path":path,"written_bytes":size,"effects_possible":True})

    def note_cleanup(self, cause):
        self.cleanup_faults.append(cause)

    def retain_local_owner(self,owner):
        if id(owner) not in self.local_owners:
            self.local_serial+=1;self.local_owners[id(owner)]=(self.local_serial,owner)
        self.publish_local_owners()
        return owner
    def retire_local_owner(self,owner):
        journal=getattr(owner,"fdjournal",owner if isinstance(owner,OwnedFDs) else None)
        if journal is not None and journal.fds:return False
        self.local_owners.pop(id(owner),None);self.publish_local_owners();return True
    def local_owner_graph(self):
        result=[]
        for sequence,owner in self.local_owners.values():
            journal=getattr(owner,"fdjournal",owner if hasattr(owner,'rows') and hasattr(owner,'graph') else None)
            result.append({"id":sequence,"class":type(owner).__name__,
                "journal":journal.graph() if journal is not None else None,
                "pending_holds":[h.token for h in getattr(owner,"pending_holds",())],
                "metadata_hold":getattr(getattr(owner,"metadata_hold",None),"token",None),
                "FD_hold":getattr(getattr(owner,"fd_hold",None),"token",None),
                "rootfd":getattr(owner,"rootfd",getattr(owner,"root_fd",None)),
                "arena_holds":[h.token for h in getattr(owner,'holds',())],
                "alias_edges":[{'field':name,'identity':str(id(value)),'class':type(value).__name__}
                    for name,value in getattr(owner,'__dict__',{}).items()
                    if name not in ('meter','call','consumer')]})
        return result
    def publish_local_owners(self):
        return
    def owned_sample(self,path,maximum):
        # Public-consumer sampling is a separate physical acquisition, not a
        # hash pass. A complete FD/return arena precedes its actual open/read.
        from custody import open_absolute
        hold=self.reserve("consumer-stock-sample-before-open",reads=maximum+1,
            allocation=131072,slots=3)
        book=OwnedFDs(credit=hold,meter=self)
        self.retain_local_owner(book)
        try:
            fd=open_absolute(path,journal=book)
            raw=os.read(fd,maximum+1)
            hold.commit(reads=len(raw))
            return raw
        finally:
            book.close()
            if not book.fds:
                # The caller's complete consumer allocation arena covers the
                # short returned raw/sample-frame alias after the FD lifetime.
                hold.release();self.retire_local_owner(book)
            else:self.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","journal":book.graph()})

    @contextlib.contextmanager
    def owned_file(self,path,maximum,flags):
        hold=self.reserve("consumer-file-FD-before-open",allocation=131072,slots=3)
        book=OwnedFDs(credit=hold,meter=self);self.retain_local_owner(book)
        try:
            fd=open_absolute(path,flags,journal=book)
            yield fd
        finally:
            book.close()
            if not book.fds:hold.release();self.retire_local_owner(book)
            else:self.note_cleanup({"cause":"FD_CLOSE_UNCONFIRMED","journal":book.graph()})

    def track_held_lease(self,lease):
        self.held_leases[id(lease)]=lease;self.publish_local_owners()
    def untrack_held_lease(self,lease):
        self.held_leases.pop(id(lease),None);self.publish_local_owners()
    def held_lease_graph(self):
        rows=[]
        for lease in self.held_leases.values():
            # Export last known facts and the exact journal without any fresh
            # fstat/read/reserve on an expired terminal path.
            fact={"fd":lease.fd,"path":lease.path,"retained_by":lease.retained_by,
                "reservation_token":lease.hold.token if lease.hold else None,
                "expected_pin":lease.pin,"last_identity9":lease.before,"body_sha256":lease.body_sha,
                "closed":lease.closed,"journal":lease.fdjournal.graph()}
            rows.append(fact)
        return rows

    def result_owner_graph(self):
        rows=[]
        values=[]
        for key,(value,holds) in self.owned_results.items():
            values.append(value)
            rows.append({"identity":str(key),"class":type(value).__name__,
                "size":len(value) if type(value) in (bytes,bytearray,str,list,dict,tuple) else None,
                "reservation_tokens":[h.token for h in holds],"cached_full_immutable_sha256":self.owned_digests.get(key),
                "immutable_bytes":type(value) is bytes})
        return {"count":len(rows),"chunks":[rows[i:i+512] for i in range(0,len(rows),512)],
            'actual_values':full_value_arena(values),"truncated":False}

    def complete_owner_graph(self):
        locals=self.local_owner_graph();held=self.held_lease_graph()
        return {"schema":"friday.a181.complete-actor-owner-graph.v1",
            "owner_pid":os.getpid(),"local_count":len(locals),"local_chunks":[locals[i:i+512] for i in range(0,len(locals),512)],
            "held_count":len(held),"held_chunks":[held[i:i+512] for i in range(0,len(held),512)],
            "results":self.result_owner_graph(),"FD_domain":self.fd_state.graph(),
            'local_values':full_value_arena([owner for _,owner in self.local_owners.values()]),
            'held_values':full_value_arena(list(self.held_leases.values())),
            "error_arenas":full_value_arena(getattr(self,'error_arenas',())),"truncated":False}

    def retain_error_arena(self,exc):
        # Exception, traceback frames, locals and chained causes remain actual
        # strongly held objects in the preadmitted owner completion arena.
        if not hasattr(self,"error_arenas"):self.error_arenas=[]
        if all(prior is not exc for prior in self.error_arenas):self.error_arenas.append(exc)
        self.publish_local_owners()
        return exc

    def reservation_graph(self):
        # Complete, bounded nesting rather than a >512-key JSON map. These
        # are reservation identities, NOT the original ABI128 process list.
        rows=[{"token":str(k),**dict(v)} for k,v in self.pending.items()]
        if len(rows)>512*512:raise Refused("complete_reservation_graph_capacity","terminal")
        return {"schema":"friday.sol053.complete-reservation-graph.v1","complete_count":len(rows),
            "chunks":[rows[i:i+512] for i in range(0,len(rows),512)],"truncated":False}

    def own_result(self,value,hold):
        key=id(value)
        previous=self.owned_results.get(key)
        if previous is not None:
            if previous[0] is not value:raise Refused("return_owner_alias")
            previous[1].append(hold)
        else:self.owned_results[key]=(value,[hold])
        self.publish_local_owners()
        return value

    def retire_result(self,value):
        key=id(value)
        row=self.owned_results.get(key)
        if row is not None:
            if row[0] is not value:raise Refused("return_owner_identity")
        value=None;row=None
        self.retire_result_id(key)

    def retire_result_id(self,key):
        # Caller has first dropped every alias, passing only the object identity.
        row=self.owned_results.get(key)
        if row is None:return True
        if any(getattr(h,'fd_rows',{}) for h in row[1]):return False
        for hold in row[1]:
            if hold.release() is not True:return False
        self.owned_digests.pop(key,None);self.owned_results.pop(key,None)
        return True

    def cached_digest(self, raw):
        row = self.owned_results.get(id(raw))
        if row is None or row[0] is not raw:
            return None
        return self.owned_digests.get(id(raw))

    def remember_digest(self, raw, digest):
        row = self.owned_results.get(id(raw))
        if row is None or row[0] is not raw:
            return
        prior = self.owned_digests.get(id(raw))
        if prior is None:
            self.owned_digests[id(raw)] = digest
        elif prior != digest:
            raise Refused("owned_digest_drift")

    def retire_results(self):
        # Caller first discards every escaping parsed/body alias. This arena
        # is NEVER retired while a child or unconfirmed owner can use it.
        keys=list(self.owned_results)
        for key in keys:
            if self.retire_result_id(key) is not True:return False
        return True

    def before_reap(self, pid):
        """Read zombie/held-finish IO before direct wait4 removes /proc evidence."""
        self._owned()
        try:
            if proc_start(pid,self)["start_ticks"] != self.processes[pid]["start_ticks"]:
                raise Refused("pid_reused")
            self.final_io[pid] = proc_io(pid,self)
        except BaseException as exc:
            self.errors.append(error_fact(exc,"terminal"))
            raise Refused("final_actual_io_unknown","terminal") from exc

    def record_wait4(self, pid, status, usage):
        self.waits.append({"pid":pid,"raw_wait_status":status,
            "exit_code":os.waitstatus_to_exitcode(status),
            "rusage":{name:(str(getattr(usage,name)) if type(getattr(usage,name)) is float else getattr(usage,name)) for name in (
                "ru_utime","ru_stime","ru_maxrss","ru_ixrss","ru_idrss","ru_isrss",
                "ru_minflt","ru_majflt","ru_nswap","ru_inblock","ru_oublock",
                "ru_msgsnd","ru_msgrcv","ru_nsignals","ru_nvcsw","ru_nivcsw")}})

    def receipt(self, final=False):
        sample, io, peak = self.check(final)
        return {"observer_id":self.observer_id,"scope":"producer-and-all-helpers",
            "processes":sorted(self.processes),"peak_ram_bytes":peak,
            "read_bytes":max(self.explicit_read,sum(r["counters"]["rchar"] for r in io.values()),sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"])+self.explicit_hash,
            "physical_read_bytes":max(self.explicit_read,sum(r["counters"]["rchar"] for r in io.values()),sample["block_read"]+io[self.owner_pid]["counters"]["read_bytes"]),
            "memory_hash_bytes":self.explicit_hash,
            "output_bytes":max(self.explicit_output,sum(r["counters"]["wchar"] for r in io.values()),sample["block_write"]+io[self.owner_pid]["counters"]["write_bytes"]),
            "transport_output_bytes":self.explicit_output,
            "implicit_io_status":"OBSERVED","raw_kernel":sample,"raw_process_io":{str(k):v for k,v in io.items()},
            "direct_wait4":list(self.waits),"explicit_read_hash_debits":self.explicit_hash,
            "explicit_write_transport_debits":self.explicit_output,
            "pending_reservations":self.reservation_graph(),"reservations_are_observations":False,
            "absolute_inherited_peak_subtracted":False,"partial_writes":list(self.partial),
            "cleanup_faults":list(self.cleanup_faults),"errors":list(self.errors)}

    def a128_aggregate(self):
        r = self.receipt()
        return {k:r[k] for k in ("observer_id","scope","processes","peak_ram_bytes",
                                 "read_bytes","output_bytes","implicit_io_status")}


INPUT_MAX_TERMINAL = 2_000_000


class MeterRPC:
    """Actor accounting channel is owned/created by Root; it grants no authority."""
    def __init__(self, call, owner_export_token=None, terminal_call=None):
        self.call = call
        self.owned_results={}
        self.owned_digests={}
        self.held_leases={}
        self.local_owners={};self.local_serial=0;self.local_sequence=0;self.owner_publication_error=None
        self.owner_export_token=owner_export_token;self.terminal_call=terminal_call
        self.fd_state=FDState(owner_export_token)
        self.owner_dirty=False;self.owner_export_attempted=False;self.owner_export_accepted=False
    own_result=RootObserver.own_result
    cached_digest=RootObserver.cached_digest
    remember_digest=RootObserver.remember_digest
    retire_result=RootObserver.retire_result
    retire_result_id=RootObserver.retire_result_id
    retire_results=RootObserver.retire_results
    track_held_lease=RootObserver.track_held_lease
    untrack_held_lease=RootObserver.untrack_held_lease
    retain_local_owner=RootObserver.retain_local_owner
    retire_local_owner=RootObserver.retire_local_owner
    local_owner_graph=RootObserver.local_owner_graph
    held_lease_graph=RootObserver.held_lease_graph
    result_owner_graph=RootObserver.result_owner_graph
    complete_owner_graph=RootObserver.complete_owner_graph
    retain_error_arena=RootObserver.retain_error_arena
    owned_sample=RootObserver.owned_sample
    owned_file=RootObserver.owned_file
    def publish_local_owners(self,terminal=False):
        # Local strong ownership is updated without ordinary RPC allocation.
        # The complete prepaid export has one finite explicit terminal attempt.
        self.owner_dirty=True
        if not terminal:return
        if self.owner_export_attempted:raise Refused("actor_owner_export_already_attempted","terminal")
        if self.owner_export_token is None or self.terminal_call is None:raise Refused("actor_owner_export_not_preadmitted","terminal")
        self.owner_export_attempted=True
        self.local_sequence+=1
        try:
            result=self.terminal_call({"type":"actor-owner-state","sequence":self.local_sequence,
                "owner_pid":os.getpid(),"graph":self.complete_owner_graph(),"prepaid_token":self.owner_export_token})
            if result!={"retained":True,"sequence":self.local_sequence,"complete":True}:raise Refused("actor_owner_acceptance","terminal")
            self.owner_export_accepted=True;self.owner_dirty=False
        except BaseException as exc:
            self.owner_publication_error=error_fact(exc,"terminal")
            # Exact live journals remain strongly in this actor. Failed delivery
            # does not confirm retirement or shrink any owning reservation.
            raise
    def reserve(self, purpose, reads=0, output=0, allocation=0, slots=0, hash_bytes=0):
        reply = self.call({"type":"reserve","purpose":purpose,"reads":reads,
                          "output":output,"allocation":allocation,"slots":slots,"hash_bytes":hash_bytes})
        return Reservation(self, reply["token"],slots)
    def commit(self, token, reads=0, output=0, hash_bytes=0):
        self.call({"type":"commit","token":token,"reads":reads,"output":output,"hash_bytes":hash_bytes})
    def grow(self,token,allocation=0):
        self.call({"type":"grow","token":token,"allocation":allocation})
    def release(self, token):
        try:
            response=(self.terminal_call or self.call)({"type":"release","token":token})
            return response.get('released') is True
        except BaseException as exc:
            self.owner_publication_error=error_fact(exc,'terminal')
            return False
    def note_partial(self, path, size):
        self.call({"type":"partial","path":path,"size":size})
    def note_cleanup(self, cause):
        try: (self.terminal_call or self.call)({"type":"cleanup","cause":cause})
        except BaseException: pass

class FinalArena:
    """One explicit existing-Root completion arena, including error frames.

    It is a lifecycle receiver, never a Root role or admission authority. The
    native caller consumes the actual strongly held graph in a finite callback
    before known arenas can be released. Foreign escaping aliases still need
    independent whole qualification; this API does not manufacture that fact.
    """
    def __init__(self,owner):
        self.owner=owner;self.owner_pid=os.getpid();self.generation=0
        self.roots={};self.completed=False;self.attempted=False
        self.receipt=None;self.receipt_owner=None
    def capture(self):
        if os.getpid()!=self.owner_pid:raise Refused('final_arena_owner','terminal')
        self.generation+=1
        self.roots={name:value for name,value in self.owner.__dict__.items() if name!='final_arena'}
        return {'schema':'friday.a181.existing-root-final-arena.v1','owner_pid':self.owner_pid,
            'generation':self.generation,'roots':[{'field':name,'identity':str(id(value)),
                'class':type(value).__name__} for name,value in self.roots.items()],
            'completed':self.completed,'ownership_retired':False}
    def consume(self,consumer):
        if os.getpid()!=self.owner_pid or self.attempted or not callable(consumer):raise Refused('final_arena_receiver','terminal')
        self.attempted=True;self.capture()
        # Full raw captures, original exceptions/tracebacks and metadata remain
        # strong here. No fresh ordinary reservation or decoder is required.
        try:result=consumer(self.roots)
        except BaseException as exc:
            observer=getattr(self.owner,'observer',None)
            if observer is not None:observer.retain_error_arena(exc)
            else:
                if not hasattr(self.owner,'prefix_error_arenas'):self.owner.prefix_error_arenas=[]
                self.owner.prefix_error_arenas.append(exc)
            raise
        # A descriptive token or arbitrary callback returning None cannot
        # complete an actual value/body/alias receiver. The existing native
        # owner must retain a full receipt and the exact actual roots itself.
        # Native enrollment/cross-language durable qualification is still a
        # separate obligation; this Source callback does not mint that fact.
        if not isinstance(result,NativeCompletionReceipt):raise Refused('actual_native_completion_receipt_required','terminal')
        result.require(self,self.roots)
        self.receipt=result;self.receipt_owner=result.receiver
        self.roots.clear();self.completed=True
    def require_complete(self,owner):
        if owner is not self.owner or os.getpid()!=self.owner_pid or not self.completed or self.roots or self.receipt is None:
            raise Refused('complete_native_caller_arena_required','terminal')
        self.receipt.require_completed(self)
    def detach_confirmed(self):
        if not self.completed or self.roots:raise Refused('final_arena_unconfirmed','terminal')
        receipt=self.receipt
        if receipt is not None:
            receipt.roots.clear();receipt.arena=None;receipt.receiver=None;receipt.qualification=None
            receipt.durable_pin=None
        self.owner=None;self.receipt=None;self.receipt_owner=None

class NativeCompletionReceipt:
    """Exact Source-side receipt correspondence, never external authority.

    Qualification of receiver/retained receipt against the independently
    enrolled native tool is REQUIRED before this protocol can be admitted.
    A Python object alone cannot establish native durability or enrollment.
    """
    def __init__(self,receiver,arena,roots,durable_pin,qualification):
        self.receiver=receiver;self.arena=arena;self.owner_pid=os.getpid()
        self.generation=arena.generation;self.roots=roots.copy()
        self.durable_pin=durable_pin;self.qualification=qualification
        self.confirmed=False
    def require(self,arena,roots):
        from common import exact,sha,canonical,parse,INPUT_MAX
        from custody import identity9,open_absolute
        owner=arena.owner
        if self.arena is not arena or self.owner_pid!=os.getpid() or self.generation!=arena.generation:
            raise Refused('native_completion_identity','terminal')
        if owner.qualification is None or self.qualification is not owner.qualification or owner.root_fact is None:
            raise Refused('enrolled_native_full_completion_required','terminal')
        if getattr(self.receiver,'owner_pid',None)!=self.owner_pid or getattr(self.receiver,'root_fact',None)!=owner.root_fact:
            raise Refused('native_completion_existing_root','terminal')
        accepted=getattr(self.receiver,'accepted_source_roots',None)
        if type(accepted) is not dict or set(accepted)!=set(roots) or any(accepted[name] is not value for name,value in roots.items()):
            raise Refused('native_completion_actual_roots','terminal')
        if set(self.roots)!=set(roots) or any(self.roots[name] is not value for name,value in roots.items()):
            raise Refused('native_completion_root_drift','terminal')
        body=full_value_arena([roots])
        validate_full_value_arena(body)
        raw=canonical({'schema':'friday.a190.native-full-completion.v1','owner_pid':self.owner_pid,
            'generation':self.generation,'body':body},maximum=INPUT_MAX)
        pin=exact(self.durable_pin,('path','bytes','sha256','identity9_decimal_strings'),'native_completion_pin')
        owner.check_owned_path(pin['path'])
        if pin['bytes']!=len(raw) or pin['sha256']!=sha(raw,admitted=owner.observer.terminal_hold):
            raise Refused('native_completion_complete_body_pin','terminal')
        fd=open_absolute(pin['path'],journal=owner.fdjournal)
        try:
            if identity9(os.fstat(fd))!=pin['identity9_decimal_strings']:raise Refused('native_completion_pin_drift','terminal')
            at=0
            while at<len(raw):
                part=os.pread(fd,min(65536,len(raw)-at),at)
                owner.observer.terminal_hold.commit(reads=len(part))
                if not part or part!=raw[at:at+len(part)]:raise Refused('native_completion_complete_body','terminal')
                at+=len(part)
            if identity9(os.fstat(fd))!=pin['identity9_decimal_strings'] or identity9(os.lstat(pin['path']))!=pin['identity9_decimal_strings']:
                raise Refused('native_completion_pin_drift','terminal')
        finally:owner.fdjournal.close_one(fd)
        if fd in owner.fdjournal.fds:raise Refused('native_completion_FD_unconfirmed','terminal')
        # Complete verified durable value preimages, not the graph's labels,
        # allow the caller's exact same-owner roots to be dropped once.
        accepted.clear();self.roots.clear();self.confirmed=True
    def require_completed(self,arena):
        if not self.confirmed or self.arena is not arena or self.owner_pid!=os.getpid() or self.generation!=arena.generation:
            raise Refused('native_completion_not_confirmed','terminal')
