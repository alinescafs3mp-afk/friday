"""A137 independently invoked Root-native launcher/collector Source.

Only the EXISTING actual Root native actor may select/invoke this reviewed code
with its separately issued immutable admission and original native fd0/1/2.
No JSON field, source file, UID, seal, CLI, or child event authenticates that actor.
This code never issues a grant. Its future observations belong to that Root actor.
It adds zero models/TUIs/canonical workers. A changed OS launcher/pipe edge needs
independent review/admission before effects. A132 itself executes none of this.

The collector is real code, owns custody before Popen, and survives caller's exit.
Producer results and Root recipient results are distinct typed namespaces.
Resource/schedule/custody proof is a separate unmet admission condition; this
implementation is NOT advertised as an all-path lossless closure under old caps.
"""
import errno
import fcntl
import hashlib
import json
import os
import resource
import select
import stat
import subprocess
import time
import ctypes

ROOT_WRITER_FD = 197
ACTOR_WRITER_FD = 196
SOURCE_READ_MAX = 33554432
FUTURE_ROOT_READ_CEILING = 268435456
NATIVE_MAX = 262144
SMALL_SOURCE_MAX = 65536
AS_SOFT = 67108864
AS_HARD = 1610612736
WHOLE_BILL = 8589934592
SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
MAGIC = b"A132"
HEADER_BYTES = 72
FRAME_PAYLOAD = 3072
CONTROL_OBJECT_SHA="e7255f04bc4fa806960fcca79b6b37b67202417170ed59c3b9101863e3e91224"
CONTROL_OBJECT_BYTES=4139478

def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
        separators=(",", ":")) + "\n").encode("ascii")

def unique(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Root duplicate input key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite Root input")))

def identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

def require(ok, reason):
    if not ok:
        raise ValueError(reason)

def value(item):
    if item is None or type(item) in (str, bool, int):
        return item
    if type(item) is bytes:
        return {"type":"bytes","length":len(item),"raw_hex":item.hex(),
            "sha256":hashlib.sha256(item).hexdigest()}
    if type(item) is float:
        return {"type":"float_hex","value":item.hex()}
    if isinstance(item, os.stat_result):
        return {"type":"identity9","value":identity(item)}
    if isinstance(item, (tuple, list)):
        return {"type":"tuple" if type(item) is tuple else "list",
            "value":[value(part) for part in item]}
    if type(item) is dict:
        return {"type":"mapping","value":[[value(k),value(v)] for k,v in item.items()]}
    if callable(item):
        return {"type":"callable_reference","module":getattr(item,"__module__",None),
            "qualname":getattr(item,"__qualname__",type(item).__qualname__)}
    raise TypeError("unrepresented_Root_value:" + type(item).__name__)

def root_error_graph(error):
    owners=[error];ids={id(error):0};nodes=[]
    def ref(item):
        if item is None:return None
        if id(item) not in ids:ids[id(item)]=len(owners);owners.append(item)
        return ids[id(item)]
    def graph_value(item):
     if isinstance(item,BaseException):return {'type':'error_ref','id':ref(item)}
     if type(item) in (list,tuple):return [graph_value(v) for v in item]
     if type(item) is dict:return {'type':'mapping','value':[[graph_value(k),graph_value(v)] for k,v in item.items()]}
     return value(item)
    for item in owners:
        frames=[];tb=item.__traceback__
        while tb is not None:
            code=tb.tb_frame.f_code;frames.append([code.co_filename,tb.tb_lineno,code.co_name]);tb=tb.tb_next
        nodes.append({'id':ids[id(item)],'type':type(item).__name__,'module':type(item).__module__,
            'args':graph_value(item.args),'text':str(item),'errno':getattr(item,'errno',None),
            'state':graph_value(vars(item)),'notes':graph_value(getattr(item,'__notes__',None)),
            'frames':frames,'cause':ref(item.__cause__),'context':ref(item.__context__),
            'suppress_context':item.__suppress_context__,'groups':[ref(x) for x in getattr(item,'exceptions',())]})
    return {'schema':'friday.Root-error-graph.v1','root':0,'nodes':nodes,'truncated':False}

def error_value(error):
    graph=root_error_graph(error)
    # No hash, truncation or type/errno projection stands in for the arguments.
    return {"type":type(error).__name__,"module":type(error).__module__,
        "errno":getattr(error,"errno",None),"args":graph["nodes"][0]["args"],
        "text":str(error),"filename":value(getattr(error,"filename",None)),
        "filename2":value(getattr(error,"filename2",None)),"graph":graph}

class Slot:
    def __init__(self, purpose):
        self.purpose = purpose
        self.fd = None
        self.pair = None
        self.pair_closed = [False, False]
        self.close_attempted = False
        self.pair_close_attempted = [False,False]
        self.process = None
        self.stream = None
        self.returned = self.closed = False
        self.error = None
        self.arguments = None
        self.function = None
        self.stream_close_attempted = {}
        self.raw_arguments = None

class RootLifetime:
    def __init__(self):
        self.slots = []
        self.pending = None
        self.close_events = []
        self.closed = False
        self.ledger = None
        self.last_error = None
        self.first_error = None
        self.recording_damage = False
        self.recording_errors = []
        self.pending_close = None
        self.tail = None

    def reserve(self, purpose):
        slot = Slot(purpose)
        # The emergency slot is reachable even if list growth fails.
        self.pending = slot
        self.slots.append(slot)
        if self.tail is not None:
            self.tail.own_raw(slot)
        self.pending = None
        return slot

    def acquire_fd(self, purpose, function, *args, **kwargs):
        require(not self.recording_damage and not any(event.get("raw_error") is not None
            for event in self.close_events),"no new acquisition after ambiguous ownership")
        slot = self.reserve(purpose)
        slot.raw_arguments = [args,kwargs]
        slot.arguments = None
        slot.function=function.__module__+"."+function.__name__
        try:
            slot.fd = function(*args, **kwargs)
            slot.returned = True
            return slot.fd
        except BaseException as error:
            slot.error = error
            self.last_error = error
            raise

    def pipe(self, flags):
        slot = self.reserve("collector.pipe2.partial_pair")
        slot.raw_arguments = [flags]
        slot.arguments = None
        slot.function="os.pipe2"
        try:
            slot.pair = os.pipe2(flags)
            slot.returned = True
            return slot.pair
        except BaseException as error:
            slot.error = error
            self.last_error = error
            raise

    def process(self, *args, **kwargs):
        slot = self.reserve("caller.Popen.partial_process")
        slot.raw_arguments = [args,kwargs]
        slot.arguments = None
        slot.function="subprocess.Popen.__init__"
        slot.process = subprocess.Popen.__new__(subprocess.Popen)
        # The initialized owner keeps the same partial object before Popen init.
        slot.process.pid = None
        try:
            subprocess.Popen.__init__(slot.process, *args, **kwargs)
            slot.returned = True
            return slot.process
        except BaseException as error:
            slot.error = error
            self.last_error = error
            raise

    def forget_closed(self, fd, owners):
        for slot in owners:
            if slot.fd == fd:
                slot.closed = True
            if slot.pair is not None and fd in slot.pair:
                slot.pair_closed[slot.pair.index(fd)] = True

    def close_fd(self, fd, purpose):
        import sys
        active=sys.exc_info()[1]
        if active is not None and self.first_error is None:self.first_error=active
        # Preserve the actual close attempt independently of the closing fd.
        owners = [slot for slot in self.slots if slot.fd == fd and not slot.closed or
            slot.pair is not None and fd in slot.pair and not slot.pair_closed[slot.pair.index(fd)]]
        require(not any(slot.close_attempted if slot.fd == fd else
            slot.pair_close_attempted[slot.pair.index(fd)] for slot in owners),
            "sticky close generation: never retry a numeric descriptor")
        event = {"operation":"Root.close","purpose":purpose,"fd":fd,
            "returned":False,"error":None,"raw_error":None,"recording_error":None}
        self.pending_close = event
        self.close_events.append(event)
        # A failed/ambiguous close is sticky and is never blindly retried on a
        # descriptor number that could have been reused by a later acquisition.
        for slot in owners:
            if slot.fd==fd:
                slot.close_attempted=True
            if slot.pair is not None and fd in slot.pair:
                slot.pair_close_attempted[slot.pair.index(fd)]=True
        try:
            os.close(fd)
            event["returned"] = True
            self.forget_closed(fd,owners)
        except BaseException as error:
            event["raw_error"] = error
            self.last_error = error
            if active is None:raise
        finally:
            try:
                event["error"] = None if event["raw_error"] is None else error_value(event["raw_error"])
                row = {key:item for key,item in event.items() if key not in ("raw_error","recording_error")}
                if self.tail is not None:self.tail.append(row,event)
                if self.ledger is not None and not self.ledger.sealed and fd != self.ledger.writer:
                    self.ledger.append({"namespace":"Root_recipient", **row})
            except BaseException as recorder:
                event["recording_error"] = recorder
                self.recording_damage = True
                try:self.recording_errors.append(recorder)
                except BaseException:pass

    def close(self, retain_reader=None, retained_fds=()):
        # Root-held reader adoption is explicit. Do not discard a final close
        # result or silently close an adopted independent custody object.
        if self.closed:
            return self.snapshot(retain_reader,retained_fds)
        slots = list(self.slots) + ([self.pending] if self.pending else [])
        # Keep the recorder until every other actual close result is retained.
        ledger_fds = () if self.ledger is None else (self.ledger.writer,self.ledger.reader)
        slots.sort(key=lambda slot: slot.fd in ledger_fds)
        for slot in slots:
            fds = []
            if slot.fd is not None and not slot.close_attempted:
                fds.append(slot.fd)
            if slot.pair is not None:
                fds.extend(fd for index,fd in enumerate(slot.pair) if not slot.pair_close_attempted[index])
            if slot.process is not None:
                for stream in (getattr(slot.process,"stdin",None),getattr(slot.process,"stdout",None),getattr(slot.process,"stderr",None)):
                    if stream is not None and not stream.closed and id(stream) not in slot.stream_close_attempted:
                        try:
                            event = {"operation":"Root.stream.close","purpose":slot.purpose,
                                "fd":stream.fileno(),"returned":False,"error":None,"raw_error":None}
                            slot.stream_close_attempted[id(stream)] = event
                            self.pending_close = event
                            self.close_events.append(event)
                            try:
                                stream.close()
                                event["returned"] = True
                            except BaseException as error:
                                event["raw_error"] = error
                                self.last_error = error
                            if self.tail is not None:self.tail.append({"operation":"Root.stream.close",
                                "purpose":slot.purpose,"fd":event["fd"],"returned":event["returned"],
                                "error":None if event["raw_error"] is None else error_value(event["raw_error"])},event)
                        except BaseException as error:
                            self.last_error = error
                            self.recording_damage = True
            for fd in fds:
                if fd == retain_reader or fd in retained_fds:
                    continue
                try:
                    self.close_fd(fd, slot.purpose)
                except BaseException:
                    pass
        self.closed = retain_reader is None and not retained_fds and not self.recording_damage and all(
            (slot.fd is None or slot.closed) and (slot.pair is None or all(slot.pair_closed))
            and all(event["returned"] for event in slot.stream_close_attempted.values()) for slot in slots)
        return self.snapshot(retain_reader,retained_fds)

    def snapshot(self, retained_reader,retained_fds=()):
        return {"schema":"friday.a137.future-Root-lifetime-result.v1",
            "close_events":[{key:item for key,item in event.items() if key not in ("raw_error","recording_error")} for event in self.close_events],
            "acquisitions":[{"purpose":slot.purpose,"function":slot.function,"arguments":value(slot.raw_arguments),
                "returned":slot.returned,"fd":slot.fd,"pair":slot.pair,
                "close_attempted":slot.close_attempted,"close_returned":slot.closed,
                "pair_close_attempted":list(slot.pair_close_attempted),"pair_close_returned":list(slot.pair_closed),
                "partial_process_pid":None if slot.process is None else getattr(slot.process,"pid",None),
                "error":None if slot.error is None else error_value(slot.error)} for slot in self.slots],
            "Root_owned_close_errors":[{key:item for key,item in event.items() if key not in ("raw_error","recording_error")} for event in self.close_events if event.get("raw_error") is not None],
            "retained_reader_adoption":retained_reader,
            "Root_owned_retained_fds":list(retained_fds),
            "external_adoption_confirmed":False,
            "recording_damage":self.recording_damage,
            "all_owned_closes_confirmed":self.closed,
            "actual_native_exit_observer":"EXISTING_PARENT_TAIL" if self.tail is not None else "EXISTING_NATIVE_ROOT_TOOL_REQUIRED_SEPARATELY",
            "allpath_resource_and_adopted_descendant_proof":"INDEPENDENT_FINITE_PROOF_REQUIRED",
            "GO":False}

class ExistingRootTail:
    """Independent custody object created/retained by the already existing Root.

    The caller of the helper owns record_fd throughout helper close/export/exit.
    Descriptor metadata is checked, not used as origin authentication. The native
    tool must select/adopt this exact object independently; no Source envelope
    constructs it, and no new model, root service, endpoint or deadline exists.
    """
    def __init__(self, record_fd, identity9, wall_end, mono_end):
        self.fd=record_fd;self.identity=identity9;self.ends=(wall_end,mono_end)
        self.raw=[];self.pending=None;self.first_error=None;self.recorder_errors=[]
        self.bytes=0;self.digest=hashlib.sha256();self.adopted=[];self.actual_native_exit=None
        self.owner_pid=os.getpid();self.launchers=[];self.raw_objects=[]
        self.raw_pending=None;self.native_generations=[];self.native_receipt=None
        self.finished=False;self.close_attempted=False;self.close_error=None
        self.direct_native_exits=[];self.helper_boundary=None;self.cleanup_owner=None
        info=os.fstat(record_fd)
        require(identity(info)==identity9 and stat.S_ISREG(info.st_mode) and info.st_nlink==1
            and stat.S_IMODE(info.st_mode)==0o600 and info.st_uid==info.st_gid==1000,
            "outside Root independently owned private durable tail descriptor")
    def append(self,row,raw_owner=None):
        slot={"row":row,"raw_owner":raw_owner,"wire":None,"written":0,"error":None,
            "last_write_arguments":None,"last_write_result":None}
        self.pending=slot;self.raw.append(slot)
        try:
            require(not self.finished and min(self.ends[0]-time.time(),self.ends[1]-time.monotonic())>0,
                "durable tail within original reserved dual ends; no refresh")
            slot["wire"]=encode({"namespace":"existing_Root_tail","sequence":len(self.raw)-1,**row})
            while slot["written"]<len(slot["wire"]):
                require(min(self.ends[0]-time.time(),self.ends[1]-time.monotonic())>0,
                    "original Root tail deadline")
                slot["last_write_arguments"]=(self.fd,slot["wire"][slot["written"]:])
                count=os.write(*slot["last_write_arguments"])
                slot["last_write_result"]=count
                require(type(count) is int and count>0,"tail durable progress")
                slot["written"]+=count;self.bytes+=count
            os.fsync(self.fd)
            self.digest.update(slot["wire"])
        except BaseException as error:
            slot["error"]=error
            if self.first_error is None:self.first_error=error
            try:self.recorder_errors.append(error)
            except BaseException:pass
            raise
    def own_raw(self,owner):
        # The independently selected existing parent owns the actual reference
        # before the bounded caller's operation, not a JSON object/pid label.
        require(os.getpid()==self.owner_pid,"raw custody cannot cross a process by numeric values")
        self.raw_pending=owner
        self.raw_objects.append(owner)
        self.raw_pending=None
    def capture_error(self,error):
        graph={"root":error,"pending":error,"nodes":[],"capture_error":None,"complete_python_links":False}
        self.own_raw(graph)
        try:
            todo=[error];seen=set()
            while todo:
                current=todo.pop()
                if id(current) in seen:continue
                seen.add(id(current));graph["pending"]=current
                row={"error":current,"args":current.args,"traceback":current.__traceback__,
                    "cause":current.__cause__,"context":current.__context__,
                    "suppress_context":current.__suppress_context__,"notes":getattr(current,"__notes__",None),
                    "groups":getattr(current,"exceptions",()),"python_state":vars(current)}
                graph["nodes"].append(row)
                for linked in (row["cause"],row["context"],*row["groups"]):
                    if linked is not None:todo.append(linked)
            graph["pending"]=None;graph["complete_python_links"]=True
        except BaseException as capture:graph["capture_error"]=capture
        return graph
    def select_before_birth(self,launcher,lifetime):
        require(os.getpid()==self.owner_pid and not self.finished and lifetime.tail is None,
            "existing parent selects real launcher/partial lifetime before initialization")
        slot={"launcher":launcher,"lifetime":lifetime,"selection_pid":self.owner_pid,
            "constructor_returned":False,"generations":[],"error":None}
        self.own_raw(slot);self.launchers.append(slot);lifetime.tail=self
        return slot
    def bind_native_generation(self,launcher,generation):
        selected=[s for s in self.launchers if s["launcher"] is launcher]
        require(len(selected)==1 and generation["parent_pid"]==self.owner_pid
            and generation["relation"]=="direct_caller","preselected existing parent/direct native creation")
        context={"generation":generation,"selection":selected[0],"creation_owner":next(
            s for s in launcher.lifetime.slots if s.process is launcher.child)}
        self.own_raw(context);selected[0]["generations"].append(generation)
        self.native_generations.append(context)
    def adopt(self,launcher):
        selected=[s for s in self.launchers if s["launcher"] is launcher]
        require(len(selected)==1 and os.getpid()==self.owner_pid,
            "only actual pre-birth selected same existing Root parent can receive live objects")
        slot={"launcher":launcher,"fds":[],"accepted":False,"error":None,
            "aliases":[],"raw_owner":selected[0],"raw_graph":None,"receipt":None}
        self.own_raw(slot);self.pending=slot;self.adopted.append(slot)
        ledger=getattr(launcher,"ledger",None)
        if ledger is not None:
            slot["fds"]=[fd for fd in (getattr(ledger,"reader",None) or getattr(ledger,"prefix_reader",None),
                getattr(ledger,"sideband_reader",None) or getattr(ledger,"sideband_prefix_reader",None)) if fd is not None]
            slot["fds"].extend(item["reader"] for item in getattr(ledger,"byte_objects",{}).values())
        slot["fds"].extend(row["same_held_pidfd"] for row in getattr(launcher,"generation_custody",{}).values()
            if row["wait4"] is None)
        slot["fds"]=list(dict.fromkeys(slot["fds"]))
        try:
            slot["raw_graph"]={"launcher":launcher,"lifetime":launcher.lifetime,
                "ledger":ledger,"meter":getattr(launcher,"meter",None),
                "collector":getattr(launcher,"collector",None),"run_failure":getattr(launcher,"run_failure",None)}
            # Real receiver aliases acquired BEFORE any helper-local close.
            # Each partial acquisition belongs to the already selected parent.
            for fd in slot["fds"]:
                alias={"source_fd":fd,"fd":None,"before":None,"after":None,
                    "close_attempted":False,"closed":False,"error":None}
                slot["aliases"].append(alias);self.own_raw(alias)
                alias["before"]=identity(os.fstat(fd))
                alias["fd"]=fcntl.fcntl(fd,fcntl.F_DUPFD_CLOEXEC,3)
                alias["after"]=identity(os.fstat(alias["fd"]))
                require(alias["before"]==alias["after"]==identity(os.fstat(fd)),
                    "same genuinely held open-file generation after receiver duplication")
            slot["receipt"]={"operation":"Root.adoption.accepted","owner_pid":self.owner_pid,
                "descriptor_generations":[{"source_fd":a["source_fd"],"receiver_fd":a["fd"],
                    "identity9":a["after"]} for a in slot["aliases"]],
                "partial_lifetime_objects":len(launcher.lifetime.slots),
                "prebirth_selection":True,"native_generations":[g["start_ticks"] for g in selected[0]["generations"]]}
            self.append(slot["receipt"],slot)
            aliases={a["source_fd"]:a["fd"] for a in slot["aliases"]}
            if ledger is not None:
                for name in ("reader","prefix_reader","sideband_reader","sideband_prefix_reader"):
                    old=getattr(ledger,name,None)
                    if old in aliases:setattr(ledger,name,aliases[old])
                for item in getattr(ledger,"byte_objects",{}).values():
                    if item["reader"] in aliases:item["reader"]=aliases[item["reader"]]
            for generation in getattr(launcher,"generation_custody",{}).values():
                if generation["same_held_pidfd"] in aliases:
                    generation["same_held_pidfd"]=aliases[generation["same_held_pidfd"]]
            slot["accepted"]=True
        except BaseException as error:
            slot["error"]=error
            raise
        return slot
    def outside_wait4(self,generation,options=0):
        """Wait for the actual same-held direct creation owned before Popen."""
        contexts=[c for c in self.native_generations if c["generation"] is generation]
        require(len(contexts)==1
            and generation["parent_pid"]==self.owner_pid and os.getpid()==self.owner_pid
            and contexts[0]["creation_owner"].process.pid==generation["pid"],
            "native wait must consume preselected direct creation, not numeric PID")
        require(generation["wait4"] is None,"same native generation cannot be reaped twice")
        pid=generation["pid"]
        slot={"generation":generation,"pid":pid,"options":options,"raw_result":None,"raw_error":None}
        self.own_raw(slot)
        self.pending=slot;self.raw.append(slot)
        try:slot["raw_result"]=os.wait4(pid,options)
        except BaseException as error:
            slot["raw_error"]=error
            self.append({"operation":"outside_native.wait4.error","pid":pid,"error":error_value(error)},slot)
            raise
        actual,status,usage=slot["raw_result"]
        if actual==0:
            require(options&os.WNOHANG,"zero wait result only from performed nonblocking wait")
            return slot["raw_result"]
        require(select.select([generation["same_held_pidfd"]],[],[],0)[0],
            "wait result belongs to independently held exited native creation")
        require(actual==pid and (os.WIFEXITED(status) or os.WIFSIGNALED(status)),
            "actual exited/signalled same native creation, never intended Source status")
        self.actual_native_exit=os.waitstatus_to_exitcode(status)
        self.direct_native_exits.append(slot)
        generation["wait4"]={"pid":actual,"status":status,"usage":list(usage)}
        self.native_receipt={"owner_pid":self.owner_pid,"pid":pid,"start_ticks":generation["start_ticks"],
            "same_held_pidfd":generation["same_held_pidfd"],"status":status,"native_exit_actual":self.actual_native_exit,"relation":"DIRECT_CALLER_NOT_HELPER_NATIVE_END"}
        self.append({"operation":"outside_native.wait4.returned","pid":pid,"status":status,
            "usage":list(usage),"actual_native_exit":self.actual_native_exit},slot)
        return slot["raw_result"]
    def finish(self):
        require(min(self.ends[0]-time.time(),self.ends[1]-time.monotonic())>0,
            'called retirement within original dual ends; no expired parking completion')
        require(self.actual_native_exit is not None and self.first_error is None
            and all(c['generation']['wait4'] is not None for c in self.native_generations),
            "all actual direct creation generations settled and durable tail undamaged")
        require(any(type(slot) is dict and type(slot.get("row")) is dict
            and slot["row"].get("operation")=="outside_native.helper_descriptors.cleaned"
            for slot in self.raw),
            "seal follows live lifetime descriptor cleanup")
        cleanup={'adoptions':self.adopted,'direct_native_exits':self.direct_native_exits,
            'raw_close_errors':[],'pending':None,'primary_error':None,
            'helper_native_end_verified':False,'native_C_memory_retirement':'NOT_QUALIFIED'}
        self.own_raw(cleanup);self.cleanup_owner=cleanup
        for adopted in self.adopted:
            require(adopted["accepted"],"actual custody adoption, never an unconfirmed label")
            launcher=adopted["launcher"]
            for alias in adopted["aliases"]:
                require(not alias["close_attempted"],"receiver generation close attempt never retried")
                cleanup["pending"]=alias
                alias["close_attempted"]=True
                try:
                    os.close(alias["fd"]);alias["closed"]=True
                    self.append({"operation":"Root.receiver.alias.final_close","fd":alias["fd"],
                        "identity9":alias["after"],"returned":True},alias)
                except BaseException as error:
                    alias["error"]=error
                    alias["traceback"]=error.__traceback__
                    if cleanup["primary_error"] is None:cleanup["primary_error"]=error
                    cleanup["raw_close_errors"].append(error)
                    try:
                        self.append({"operation":"Root.receiver.alias.final_close","fd":alias["fd"],
                            "identity9":alias["after"],"returned":False,"error":error_value(error)},alias)
                    except BaseException as recorder:
                        cleanup["observation_errors"]=list(cleanup.get("observation_errors") or [])+[recorder]
        require(not cleanup["raw_close_errors"] and not cleanup.get("observation_errors"),
            "same-owner first-close uncertainties, never complete")
        require(all(not item["launcher"].lifetime.recording_damage and not any(event.get("raw_error") is not None
            for event in item["launcher"].lifetime.close_events) for item in self.adopted),
            "all final/adopted closes confirmed within original ends")
        self.append({"operation":"outside_native.tail.complete","actual_native_exit":self.actual_native_exit,
            "all_adoptions_closed":True})
        self.finished=True
        return {"fd":self.fd,"identity9":identity(os.fstat(self.fd)),"bytes":self.bytes,
            "sha256":self.digest.hexdigest(),"actual_native_exit":self.actual_native_exit,
            "tail_close_and_result_retention":"OUTSIDE_EXISTING_NATIVE_ROOT_OWNER",
            "native_relation":"DIRECT_CALLER_NOT_HELPER_NATIVE_END",
            "helper_native_end_verified":False,"native_C_memory_retirement":"NOT_QUALIFIED",
            "GO":False}

    def final_close_by_native_recipient(self, holder=None):
        """The existing outside owner holds this outcome before the descriptor close.

        The outcome is not written into the descriptor being closed. Holding it
        here is not native TOOL or HELPER qualification.
        """
        require(self.finished,"outside actual exit/adoption/complete tail before final recorder close")
        if self.close_attempted:
            if self.close_error is not None:raise RuntimeError("ambiguous tail close: no retry")
            return self.native_close_outcome
        self.native_close_outcome={"operation":"outside_native.tail.final_close","returned":False,
            "raw_error":None,"error":None,
            "native_response_retention":"HELD_BY_EXISTING_OUTSIDE_OWNER_BEFORE_CLOSE",
            "written_into_closed_fd":False,"native_tool_qualification":"NOT_QUALIFIED"}
        self.own_raw(self.native_close_outcome)
        if type(holder) is dict:
            holder["last_close"]=self.native_close_outcome
            holder["recorder_outcome_holder"]="EXISTING_OUTSIDE_NATIVE_OWNER"
            holder["helper_native_end_verified"]=False
        self.close_attempted=True
        try:
            os.close(self.fd)
            self.native_close_outcome["returned"]=True
        except BaseException as error:
            self.close_error=error;self.native_close_outcome["raw_error"]=error
            try:self.native_close_outcome["error"]=error_value(error)
            except BaseException as recorder:self.native_close_outcome["recording_error_object"]=recorder
            raise
        return {key:item for key,item in self.native_close_outcome.items() if key!="raw_error"}

    def settle_helper_and_tool(self, launcher):
        """Lifetime descriptor cleanup while the outside recorder is still live.

        A later native TOOL or HELPER retirement is a separate qualification.
        Python closure of these descriptors does not certify that retirement.
        """
        require(not self.finished and not self.close_attempted,
            "lifetime descriptor cleanup while the outside recorder is live")
        require(min(self.ends[0]-time.time(),self.ends[1]-time.monotonic())>0,
            "descriptor cleanup inside original dual ends; no fresh clock")
        lifetime=launcher.lifetime
        for slot in list(lifetime.slots):
            if slot.fd is not None and not slot.closed and not slot.close_attempted:
                lifetime.close_fd(slot.fd, slot.purpose)
            if slot.pair is not None:
                for end, closed, attempted in zip(slot.pair, slot.pair_closed, slot.pair_close_attempted):
                    if not closed and not attempted:
                        lifetime.close_fd(end, slot.purpose)
        damaged = lifetime.recording_damage or any(
            event.get("raw_error") is not None or event.get("recording_error") is not None
            for event in lifetime.close_events)
        if damaged:
            primary = None
            for event in lifetime.close_events:
                if event.get("raw_error") is not None:
                    primary = event["raw_error"]
                    break
            if primary is None:
                for event in lifetime.close_events:
                    if event.get("recording_error") is not None:
                        primary = event["recording_error"]
                        break
            if primary is None:
                primary = RuntimeError("cleanup or observation failure revokes terminal completion")
            raise primary.with_traceback(primary.__traceback__)
        require(all((slot.fd is None or slot.closed) and (slot.pair is None or all(slot.pair_closed)) for slot in lifetime.slots),
            "every owned descriptor is closed; an attempted unclosed end stays unverified")
        self.append({"operation":"outside_native.helper_descriptors.cleaned",
            "native_C_memory_retirement":"NOT_QUALIFIED","helper_native_end_verified":False})
        return {"helper_native_end_verified":False,"native_tool_helper_retirement":"NOT_QUALIFIED",
            "descriptor_cleanup_recorded":True}

    def reserve_invocation(self,admission,retained_root,stage1_raw,caller_path):
        require(os.getpid()==self.owner_pid and not self.finished
            and min(self.ends[0]-time.time(),self.ends[1]-time.monotonic())>0,
            'same existing selected original Root before helper entry')
        entry={'admission':admission,'retained_root':retained_root,'stage1_raw':stage1_raw,
            'caller_path':caller_path,'lifetime':None,'launcher':None,'result':None,
            'raw_error':None,'raw_traceback':None,'cleanup_error':None,'outer_error':None,
            'native_helper_end_verified':False}
        self.own_raw(entry)
        return entry

class ReadMeter:
    """Actual independent Root reads only; never move a Source read here.

    The first envelope read has its original 131073-byte request bound and an
    explicit pending policy. A genuine external invocation is still required.
    The separately reviewed policy binds before any launcher acquisition/child
    effect. This meter supplies counts, not proof that every Source path fits.
    Requested caps are cumulative reservations, not actual returned bytes.
    No loop/event count, retry budget, time quantum or RAM claim is invented.
    """
    SCOPES = ("external_envelope", "Root_source_custody", "Root_native_input",
        "Root_native_output", "Root_sideband", "Root_proc", "Root_held_object",
        "Root_replay", "Root_export")
    def __init__(self):
        self.returned = self.requested = self.calls = 0
        self.allowance = None
        self.policy = None
        self.by_scope = {name: {"requested": 0, "returned": 0, "calls": 0,
            "positive_returns": 0, "actual_zero_returns": 0,
            "errors": 0, "denied_requests": 0} for name in self.SCOPES}
        self.denied_requests = 0
        self.denied_requested = 0
        self.accounting_complete = True
        self.last_effect_result = self.last_effect_error = None
        # Original exception objects remain owned; this is not independent
        # durable error custody or a bounded error-object storage proof.
        self.error_objects = []
    def bind_policy(self, policy):
        require(self.policy is None and type(policy) is dict and set(policy) == {
            "schema", "owner_role", "returned_bytes_max", "scopes",
            "original_Source_checked_read_bytes_max", "capacity_proof_sha256"},
            "exact separate future Root read policy")
        require(policy["schema"] == "friday.a137.future-independent-Root-read-policy.v1"
            and policy["owner_role"] == "FUTURE_ACTUAL_EXISTING_ROOT_NATIVE_ACTOR"
            and type(policy["returned_bytes_max"]) is int
            and self.returned <= policy["returned_bytes_max"] <= FUTURE_ROOT_READ_CEILING
            and policy["returned_bytes_max"] > 0
            and policy["scopes"] == list(self.SCOPES)
            and type(policy["original_Source_checked_read_bytes_max"]) is int
            and policy["original_Source_checked_read_bytes_max"] == SOURCE_READ_MAX
            and type(policy["capacity_proof_sha256"]) is str
            and len(policy["capacity_proof_sha256"]) == 64
            and all(c in "0123456789abcdef" for c in policy["capacity_proof_sha256"]),
            "externally selected Root allowance, unchanged Source read cap")
        self.policy = dict(policy)
        self.allowance = policy["returned_bytes_max"]
    def read(self, fd, cap, offset=None, *, scope):
        require(self.accounting_complete, "Root previous read accounting damaged; no next effect")
        require(type(cap) is int and cap > 0, "Root positive read request")
        require(scope in self.by_scope and type(fd) is int,
            "actual Root owner and concrete read purpose required")
        if self.allowance is None:
            require(scope == "external_envelope" and self.calls == 0 and cap <= 131073,
                "pending Root policy allows only original bounded outer envelope")
            maximum = 131073
        else:
            maximum = self.allowance
        row = self.by_scope[scope]
        if self.returned + cap > maximum:
            self.denied_requests += 1
            self.denied_requested += cap
            row["denied_requests"] += 1
            raise RuntimeError("independent Root read allowance exhausted; no Source success or scope cut")
        self.requested += cap
        self.calls += 1
        row["requested"] += cap
        row["calls"] += 1
        try:
            raw = os.read(fd, cap) if offset is None else os.pread(fd, cap, offset)
        except BaseException as error:
            self.last_effect_error = error
            try:
                row["errors"] += 1
                self.error_objects.append((scope, fd, cap, offset, error))
            except BaseException:
                self.accounting_complete = False
            # Preserve the original operation's exception on recording damage.
            raise
        self.last_effect_result = raw
        try:
            self.returned += len(raw)
            row["returned"] += len(raw)
            row["positive_returns" if raw else "actual_zero_returns"] += 1
        except BaseException:
            self.accounting_complete = False
        # The just returned full byte object remains owned even if counting
        # fails. Damage is sticky and prevents the next read; it is not success.
        return raw
    def snapshot(self):
        return {"schema": "friday.a137.future-Root-actual-read-counters.v1",
            "owner_role": "FUTURE_ACTUAL_EXISTING_ROOT_NATIVE_ACTOR",
            "selected_returned_bytes_max": self.allowance,
            "original_Source_checked_read_bytes_max": SOURCE_READ_MAX,
            "returned_bytes": self.returned, "requested_caps_cumulative": self.requested,
            "actual_read_calls": self.calls, "denied_requests": self.denied_requests,
            "accounting_complete": self.accounting_complete,
            "denied_requested_caps_cumulative": self.denied_requested,
            "by_scope": {name: dict(row) for name, row in self.by_scope.items()},
            "read_errors_full":[{"scope":scope,"fd":fd,"requested":cap,"offset":offset,
                "error":error_value(error)} for scope,fd,cap,offset,error in self.error_objects],
            "no_Source_operations_reassigned": True,
            "complete_raw_error_custody_and_allpath_bounds": "NOT_PROVEN",
            "implicit_IO_and_whole_RAM": "UNKNOWN_NOT_ZERO_NOT_PROVEN"}

class HeldLedger:
    """Anonymous Root-only writable object, sealed read-only after collection.
    The child receives only an anonymous pipe writer. It never gets this fd.
    Holding this object is provenance only under genuine external Root invocation.
    """
    def __init__(self, meter, lifetime):
        self.meter = meter
        self.lifetime = lifetime
        self.writer = lifetime.acquire_fd("Root.memfd.writer", os.memfd_create,
            "a137-independent-Root-observations", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        self.reader = None
        self.prefix_reader=lifetime.acquire_fd("Root.memfd.pre_effect_prefix_reader",os.open,
            "/proc/self/fd/%d"%self.writer,os.O_RDONLY|os.O_CLOEXEC)
        self.bytes = self.rows = self.write_calls = 0
        self.digest = hashlib.sha256()
        self.failed = False
        self.sealed = False
        self.byte_objects = {}
        self.allowed_byte_pins={}
        self.raw_rows=[];self.pending_raw=None;self.recording_errors=[]
        self.prefix_bytes_owner=None
        self.sideband_writer=lifetime.acquire_fd("Root.raw_sideband.writer",os.memfd_create,
            "a147-original-read-custody",os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
        self.sideband_reader=None;self.sideband_bytes=0;self.sideband_digest=hashlib.sha256();self.prefix_sealed=False
        self.sideband_prefix_reader=lifetime.acquire_fd("Root.raw_sideband.pre_effect_prefix_reader",os.open,
            "/proc/self/fd/%d"%self.sideband_writer,os.O_RDONLY|os.O_CLOEXEC)
        self.sideband_reads=[];self.pending_sideband=None
        lifetime.ledger = self

    def hold_full_byte_object(self, raw):
        # Root acquired and SHA-checked these complete Source bytes itself before
        # any caller launch. Source references cannot create or replace an object.
        pin=hashlib.sha256(raw).hexdigest()
        require(type(raw) is bytes and self.allowed_byte_pins.get(pin)==len(raw),
            "only full independently selected before-birth Source bytes can populate Root cache")
        if pin in self.byte_objects:
            require(self.byte_objects[pin]["raw"]==raw,"one actual full owned generation per identical immutable byte object")
            return
        writer=self.lifetime.acquire_fd("Root.full_byte_object.writer",os.memfd_create,
            "a137-original-full-control-bytes",os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
        at=0
        while at<len(raw):
            count=os.write(writer,raw[at:at+8192])
            require(count>0,"Root byte-object full write progress")
            at+=count
        os.fchmod(writer,0o400)
        fcntl.fcntl(writer,fcntl.F_ADD_SEALS,SEALS)
        reader=self.lifetime.acquire_fd("Root.full_byte_object.reader",os.open,
            "/proc/self/fd/%d"%writer,os.O_RDONLY|os.O_CLOEXEC)
        info=os.fstat(reader)
        require(info.st_size==len(raw) and stat.S_IMODE(info.st_mode)==0o400
            and info.st_nlink==0 and fcntl.fcntl(reader,fcntl.F_GET_SEALS)&SEALS==SEALS,
            "Root actually held full readonly sealed byte object")
        self.lifetime.close_fd(writer,"Root.full_byte_object.writer.after_seal")
        self.byte_objects[pin]={"raw":raw,"reader":reader,"identity9":identity(info),"sha256":pin,"bytes":len(raw)}
        self.append({"namespace":"Root_recipient","operation":"Root.full_byte_object.held_before_launch",
            "sha256":pin,"bytes":len(raw),"identity9":identity(info),"held_read_fd":reader,
            "seals":SEALS,"Source_created_object":False})

    def full_byte_reference(self, reference):
        require(type(reference) is dict and set(reference)=={"type","length","sha256"}
            and reference["type"]=="held_bytes" and type(reference["length"]) is int
            and reference["sha256"] in self.byte_objects,"only actual full Root-held object reference")
        item=self.byte_objects[reference["sha256"]]
        require(reference["length"]==item["bytes"] and identity(os.fstat(item["reader"]))==item["identity9"]
            and hashlib.sha256(item["raw"]).hexdigest()==item["sha256"],"same complete owned bytes, never a hash-only projection")
        # Reuse the actual Root-owned byte object, not another physical read. Any
        # eventual physical replay/export read still uses the same actual meter.
        return item["raw"]
    def append(self, value):
        require(not self.sealed, "Root sealed ledger immutable")
        owner={"row":value,"encoded":None,"written":0,"error":None,"last_write_arguments":None,"last_write_result":None}
        self.pending_raw=owner
        self.raw_rows.append(owner)
        at = 0
        try:
            if self.lifetime.tail is not None:
                self.lifetime.tail.append({"operation":"Root.ledger.row_owned","row":value},owner)
            raw = encode(value)
            owner["encoded"]=raw
            while at < len(raw):
                owner["last_write_arguments"]=(self.writer,raw[at:])
                count = os.write(*owner["last_write_arguments"])
                owner["last_write_result"]=count
                self.write_calls += 1
                require(count > 0, "Root ledger progress")
                self.digest.update(raw[at:at + count])
                self.bytes += count
                at += count
                owner["written"]=at
            self.rows += 1
        except BaseException as error:
            owner["error"]=error
            self.failed = True
            raise

    def read_sideband(self,meter,fd,requested):
        owner={"fd":fd,"requested":requested,"offset":self.sideband_bytes,
            "raw":None,"error":None,"denied":False,"recording_error":None,"persisted":0,
            "last_write_arguments":None,"last_write_result":None}
        self.pending_sideband=owner
        self.sideband_reads.append(owner)
        try:
            raw=meter.read(fd,requested,scope="Root_sideband")
        except BaseException as error:
            owner["error"]=error
            owner["denied"]=meter.returned+requested>meter.allowance
            try:self.persist_sideband_read(owner)
            except BaseException as recorder:owner["recording_error"]=recorder
            raise
        owner["raw"]=raw
        self.persist_sideband_read(owner)
        return raw

    def persist_sideband_read(self,owner):
        raw=owner["raw"]
        if self.lifetime.tail is not None:
            self.lifetime.tail.append({"operation":"Root.original_sideband.read_owned",
                "fd":owner["fd"],"requested":owner["requested"],"stream_offset":owner["offset"],
                "raw":value(raw),"denied":owner["denied"],
                "error":None if owner["error"] is None else error_value(owner["error"])},owner)
        if raw is not None:
            at=0
            try:
                while at<len(raw):
                    owner["last_write_arguments"]=(self.sideband_writer,raw[at:])
                    count=os.write(*owner["last_write_arguments"])
                    owner["last_write_result"]=count
                    require(count>0,"Root original raw read custody progress")
                    self.sideband_digest.update(raw[at:at+count])
                    self.sideband_bytes+=count
                    at+=count;owner["persisted"]=at
            except BaseException as error:
                owner["recording_error"]=error;self.failed=True
                raise
        self.append({"namespace":"Root_recipient","operation":"sideband.read_owned",
            "fd":owner["fd"],"requested":owner["requested"],"stream_offset":owner["offset"],
            "actual_bytes":None if raw is None else len(raw),
            "actual_eof":raw==b"","denied":owner["denied"],
            "error":None if owner["error"] is None else error_value(owner["error"]),
            "raw_body_custody":"same independently Root-owned binary object",
            "raw_sha256":None if raw is None else hashlib.sha256(raw).hexdigest()})

    def seal_sideband(self):
        require(self.sideband_reader is None,"one raw sideband seal attempt")
        os.fchmod(self.sideband_writer,0o400)
        fcntl.fcntl(self.sideband_writer,fcntl.F_ADD_SEALS,SEALS)
        self.sideband_reader=self.sideband_prefix_reader
        self.sideband_identity=identity(os.fstat(self.sideband_reader))
        pin=self.sideband_digest.hexdigest()
        self.byte_objects[pin]={"reader":self.sideband_reader,"identity9":self.sideband_identity,
            "sha256":pin,"bytes":self.sideband_bytes,"kind":"literal_original_sideband"}
        self.lifetime.close_fd(self.sideband_writer,"Root.raw_sideband.writer.after_seal")
        self.sideband_writer=None
    def seal(self):
        require(not self.failed, "Root ledger recording incomplete")
        self.seal_sideband()
        os.fchmod(self.writer, 0o400)
        fcntl.fcntl(self.writer, fcntl.F_ADD_SEALS, SEALS)
        self.reader = self.prefix_reader
        self.lifetime.close_fd(self.writer, "Root.memfd.writer.after_seal")
        self.sealed = True
        self.writer = None
        info = os.fstat(self.reader)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 0
            and stat.S_IMODE(info.st_mode) == 0o400 and info.st_size == self.bytes
            and fcntl.fcntl(self.reader, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            and fcntl.fcntl(self.reader, fcntl.F_GET_SEALS) & SEALS == SEALS,
            "Root final independent readonly sealed custody")
        return {"schema": "friday.a132.independent-Root-ledger-pin.v1",
            "identity9": identity(info), "bytes": self.bytes, "rows": self.rows,
            "sha256": self.digest.hexdigest(), "seals": SEALS,
            "owner": "FUTURE_INVOKING_EXISTING_ROOT_NATIVE_ACTOR",
            "Source_issued": False, "read_fd": self.reader}
    def rows_from_held(self):
        require(self.sealed and self.reader is not None, "independent Root held reader required")
        before = identity(os.fstat(self.reader))
        buffer = b""
        offset = 0
        digest = hashlib.sha256()
        while offset < self.bytes:
            raw = self.meter.read(self.reader, min(8192, self.bytes - offset), offset, scope="Root_replay")
            require(raw, "Root full held ledger prefix not EOF")
            offset += len(raw)
            digest.update(raw)
            buffer += raw
            while b"\n" in buffer:
                raw_row, buffer = buffer.split(b"\n", 1)
                yield unique(raw_row)
        require(not buffer and identity(os.fstat(self.reader)) == before
            and digest.hexdigest() == self.digest.hexdigest(), "Root stable complete held ledger replay")
    def close(self):
        for name in ("writer", "reader"):
            fd = getattr(self, name)
            if fd is not None:
                self.lifetime.close_fd(fd, "Root.ledger." + name)
                setattr(self, name, None)

    def seal_incomplete_prefix(self):
        """Freeze literal partial bytes, never parse/promote them as complete."""
        require(self.failed and not self.sealed and not self.prefix_sealed,"one incomplete-prefix seal attempt")
        if self.sideband_reader is None:self.seal_sideband()
        os.fchmod(self.writer,0o400)
        fcntl.fcntl(self.writer,fcntl.F_ADD_SEALS,SEALS)
        self.reader=self.prefix_reader
        self.lifetime.close_fd(self.writer,"Root.incomplete_prefix.writer.after_seal")
        self.writer=None;self.prefix_sealed=True
        if self.lifetime.tail is not None:self.lifetime.tail.append({"operation":"Root.incomplete_prefix.held",
            "bytes":self.bytes,"identity9":identity(os.fstat(self.reader)),
            "sha256":self.digest.hexdigest(),"complete":False},self.pending_raw)
        return {"bytes":self.bytes,"sha256":self.digest.hexdigest(),"complete":False}

    def sideband_read_bytes(self):
        """Every actual read boundary, including EOF/error/denial, reversible."""
        require(self.sideband_reader is not None,"same Root raw read custody required")
        if self.prefix_sealed:
            # A prefix has its own literal recipe. Never pass its possibly torn
            # last JSON row to the complete-only rows_from_held() consumer.
            prefix=self.incomplete_prefix_recipe()
            for owner in self.sideband_reads:
                raw=owner["raw"]
                persisted=owner["persisted"]
                held=(b"" if persisted==0 else self.meter.read(self.sideband_reader,
                    persisted,owner["offset"],scope="Root_replay"))
                require(len(held)==persisted and (raw is None and not held
                    or type(raw) is bytes and raw.startswith(held)),
                    "literal held failed-prefix bytes match actual same-owner read")
                require(identity(os.fstat(self.sideband_reader))==self.sideband_identity,
                    "same full9 sealed incomplete raw sideband generation")
                yield {"fd":owner["fd"],"requested":owner["requested"],"result":raw,
                    "raw_error":owner["error"],"raw_recording_error":owner["recording_error"],
                    "denied":owner["denied"],"eof":raw==b"",
                    "persisted_bytes":held,"complete":False,"held_prefix_recipe":prefix,
                    "raw_owner":owner,"outside_raw_owner_acceptance":"REQUIRED_NOT_CONFIRMED"}
            return
        for row in self.rows_from_held():
            if row.get("operation")!="sideband.read_owned":continue
            amount=row["actual_bytes"]
            raw=None
            if amount is not None:
                raw=b"" if amount==0 else self.meter.read(self.sideband_reader,amount,
                    row["stream_offset"],scope="Root_replay")
                require(len(raw)==amount and hashlib.sha256(raw).hexdigest()==row["raw_sha256"],
                    "complete original raw bytes including uncommitted frames")
            require(identity(os.fstat(self.sideband_reader))==self.sideband_identity,
                "same full9 sealed raw sideband generation")
            yield {"fd":row["fd"],"requested":row["requested"],"result":raw,
                "error":row["error"],"denied":row["denied"],"eof":row["actual_eof"]}

    def incomplete_prefix_recipe(self):
        """Literal immutable prefix plus raw causal owners, not complete rows.

        Last partial row, framing fragments, actual reads and recorder errors
        remain explicit. Holding this recipe in memory is not outside acceptance.
        """
        require(self.prefix_sealed and not self.sealed and self.reader is not None,
            "distinct actual incomplete-prefix held generation")
        before=identity(os.fstat(self.reader))
        require(before[6]==self.bytes and fcntl.fcntl(self.reader,fcntl.F_GET_SEALS)&SEALS==SEALS
            and fcntl.fcntl(self.reader,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY,
            "readonly fully sealed literal partial ledger")
        owned=self.prefix_bytes_owner
        if owned is None:
            owned={'identity9':before,'chunks':[],'sha256':None,'pending_raw':None,'raw_error':None}
            self.prefix_bytes_owner=owned
            self.lifetime.tail.own_raw(owned)
            offset=0;digest=hashlib.sha256()
            try:
                while offset<self.bytes:
                    raw=self.meter.read(self.reader,min(8192,self.bytes-offset),offset,scope='Root_replay')
                    owned['pending_raw']=raw
                    require(raw,'literal incomplete held body is not invented EOF')
                    owned['chunks'].append((offset,raw));offset+=len(raw);digest.update(raw)
                require(identity(os.fstat(self.reader))==before and digest.hexdigest()==self.digest.hexdigest(),
                    'same held full9/SHA literal incomplete ledger')
                owned['sha256']=digest.hexdigest()
            except BaseException as error:owned['raw_error']=error;raise
        require(owned['raw_error'] is None and owned['identity9']==before
            and owned['sha256']==self.digest.hexdigest(), 'only actually completed same sealed generation reuse')
        return {"complete":False,"prefix_sealed":True,"identity9":before,
            "sha256":owned['sha256'],"literal_chunks":owned['chunks'],
            "raw_row_owners":self.raw_rows,"raw_pending_row":self.pending_raw,
            "raw_sideband_owners":self.sideband_reads,
            "raw_pending_sideband":self.pending_sideband,
            "outside_acceptance":"REQUIRED_NOT_CONFIRMED"}

def consume_adopted_incomplete_prefix(receiver,adoption):
    """Actual receiver consumes held aliases/raw graph, never complete rows."""
    require(any(adoption is a for a in receiver.adopted) and adoption["accepted"]
        and receiver.owner_pid==os.getpid(),"same preselected existing parent accepted this actual object graph")
    graph=adoption["raw_graph"];ledger=graph["ledger"]
    require(ledger.prefix_sealed and not ledger.sealed,"prefix consumer has a distinct incomplete domain")
    aliases={a["fd"]:a for a in adoption["aliases"]}
    require(ledger.reader in aliases and not aliases[ledger.reader]["close_attempted"],
        "receiver still genuinely holds the adopted ledger descriptor")
    recipe=ledger.incomplete_prefix_recipe()
    reads=list(ledger.sideband_read_bytes())
    require(recipe["complete"] is False and all(row["complete"] is False for row in reads),
        "no incomplete body/torn row/read promoted as complete collection")
    receipt={"schema":"friday.sol056.accepted-incomplete-prefix.v1","complete":False,
        "prefix_identity9":recipe["identity9"],"literal_bytes":sum(len(raw) for _,raw in recipe["literal_chunks"]),
        "sha256":recipe["sha256"],"raw_read_owners":len(reads),
        "actual_receiver_descriptor":ledger.reader,"prebirth_parent_owned":True,
        "whole_damage_capacity_proof":False,"SourceReady":False,"GO":False}
    receiver.append({"operation":"Root.prefix.receiver.consumed","receipt":receipt},
        {"adoption":adoption,"recipe":recipe,"reads":reads})
    return receipt

class FrameCollector:
    def __init__(self, ledger):
        self.ledger = ledger
        self.buffer = bytearray()
        self.pending = {}
        self.sequences = {}
        self.terminal = {}
        self.frames = self.received = 0
        self.stream_consumed = 0
        self.complete = False
        self.pending_parse=None
    def feed(self, raw):
        self.pending_parse={"original_raw":raw,"buffer_before":bytes(self.buffer),
            "stream_consumed_before":self.stream_consumed,"header":None,"payload":None,
            "proposed_event":None,"validated":False,"committed":False}
        self.received += len(raw)
        self.buffer.extend(raw)
        while len(self.buffer) >= HEADER_BYTES:
            header = bytes(self.buffer[:HEADER_BYTES])
            require(header[:4] == MAGIC, "Root frame marker")
            pid = int.from_bytes(header[4:12], "big")
            sequence = int.from_bytes(header[12:20], "big")
            total = int.from_bytes(header[20:28], "big")
            offset = int.from_bytes(header[28:36], "big")
            size = int.from_bytes(header[36:40], "big")
            full_digest = header[40:72]
            require(pid > 0 and total > 0 and 0 < size <= FRAME_PAYLOAD
                and offset + size <= total, "Root exact frame shape")
            if len(self.buffer) < HEADER_BYTES + size:
                return
            payload = bytes(self.buffer[HEADER_BYTES:HEADER_BYTES + size])
            self.pending_parse["header"]=header
            self.pending_parse["payload"]=payload
            key = (pid, sequence)
            if offset == 0:
                require(sequence == self.sequences.get(pid, 0) and key not in self.pending,
                    "Root complete per-actor event sequence")
                self.pending[key] = [total, full_digest, bytearray()]
            require(key in self.pending, "Root missing event first fragment")
            expected_total, expected_digest, data = self.pending[key]
            require(total == expected_total and full_digest == expected_digest and offset == len(data),
                "Root exact per-event fragment order")
            data.extend(payload)
            self.pending_parse["proposed_event"]=data
            self.ledger.append({"namespace": "Root_recipient", "operation": "sideband.frame_received",
                "pid_claim": pid, "sequence_claim": sequence, "offset": offset,
                "payload_bytes": size, "frame_bytes": HEADER_BYTES + size,
                "original_header_hex":header.hex(),"stream_offset":self.stream_consumed,
                "actor_identity_authentication": "PINNED_PRODUCER_GRAPH_EXTERNAL_CUSTODY_REQUIRED"})
            self.stream_consumed += HEADER_BYTES + size
            if len(data) == total:
                require(hashlib.sha256(data).digest() == expected_digest, "Root exact event bytes")
                event = unique(data)
                require(encode(event)[:-1]==bytes(data),"Root exact original Source canonical event recipe")
                require(type(event) is dict and set(event) == {"schema", "actor", "pid", "parent_pid",
                    "sequence", "operation", "arguments", "result", "error", "producer_data_not_Root_authority"}
                    and event["schema"] == "friday.a137.actor-event.v3"
                    and event["pid"] == pid and event["sequence"] == sequence
                    and event["producer_data_not_Root_authority"] is True,
                    "Root exact Source event, never Root fact")
                self.ledger.append({"namespace": "Source_producer", "event": event})
                if event["operation"] == "actor.terminal":
                    require(pid not in self.terminal, "Root duplicate actor terminal")
                    self.terminal[pid] = event
                self.sequences[pid] = sequence + 1
                del self.pending[key]
            self.pending_parse["validated"]=True
            self.pending_parse["committed"]=True
            del self.buffer[:HEADER_BYTES+size]
            self.frames+=1
    def finish(self):
        require(not self.buffer and not self.pending, "Root original sideband incomplete prefix")
        self.complete = True

class RootNativeLauncher:
    def __init__(self, external_admission, retained_root, meter=None, lifetime=None, independent_tail=None):
        # Entry owns this instance and this lifetime BEFORE this constructor.
        self.lifetime = RootLifetime() if lifetime is None else lifetime
        require(isinstance(independent_tail,ExistingRootTail),
            "already existing outside Root must own qualified durable tail before launch")
        require(any(s["launcher"] is self and s["lifetime"] is self.lifetime
            for s in independent_tail.launchers),"actual existing parent pre-birth selection before constructor")
        self.tail=independent_tail;self.lifetime.tail=independent_tail
        self.ledger = self.child = self.pipe_read = None
        self.source_wait = None
        self.owned_fds = set()
        self.generation_custody = {}
        self.launcher_pid = os.getpid()
        # This is checking separately admitted data, never authenticating Root.
        require(type(external_admission) is dict and set(external_admission) == {
            "schema", "source_pins", "stage1_sha256", "original_wall_end", "original_monotonic_end",
            "resources", "independent_review_sha256", "whole_bound_proof_sha256", "transport_contract_sha256",
            "future_Root_read_policy", "independent_Source_inventory"},
            "exact external Root launcher contract")
        require(external_admission["schema"] == "friday.a137.external-Root-launcher-admission.v1",
            "external reviewed launcher contract")
        bounds = external_admission["resources"]
        require(bounds == {"caller_source_max": 65536, "sender_source_max": 65536,
            "native_bytes_max": 262144, "checked_read_bytes_max": 33554432,
            "caller_sender_AS_soft": 67108864, "canonical_workers": 4,
            "whole_memory_bill_max": 8589934592}, "original Source caps unchanged")
        require(all(type(external_admission[key]) is str and len(external_admission[key]) == 64
            for key in ("independent_review_sha256", "whole_bound_proof_sha256", "transport_contract_sha256")),
            "external independently reviewed topology and whole resource proof required")
        self.admission = external_admission
        require(self.tail.ends==(external_admission["original_wall_end"],external_admission["original_monotonic_end"]),
            "same original Root tail ends, never independently extended")
        self.retained_root = retained_root
        self.meter = ReadMeter() if meter is None else meter
        require(type(self.meter) is ReadMeter, "same actual Root envelope/read meter")
        self.meter.bind_policy(external_admission["future_Root_read_policy"])
        require(self.meter.policy["capacity_proof_sha256"] == external_admission["whole_bound_proof_sha256"],
            "same external whole-bound proof and separate Root read policy")
        self.ledger = HeldLedger.__new__(HeldLedger)
        self.tail.own_raw(self.ledger)
        HeldLedger.__init__(self.ledger,self.meter,self.lifetime)
        self.collector = FrameCollector.__new__(FrameCollector)
        self.tail.own_raw(self.collector)
        FrameCollector.__init__(self.collector,self.ledger)
        self.child = self.pipe_read = None
        self.owned_fds = set()
        self.source_wait = None
        self.stdout = self.stderr = self.stdin_bytes = 0
        self.unknown = []
        self.input_buffer = bytearray()
        self.output_buffer = bytearray()
        self.stdout_hash = hashlib.sha256()
        self.stderr_hash = hashlib.sha256()
        self.native_lines = bytearray()
        self.independent_controller_custody = None
        self.original_fds = {fd: identity(os.fstat(fd)) for fd in (0, 1, 2)}
        self.pending_effect = None
        self.raw_effects = []
        self.recording_errors = []
        self.recording_pending = None
        # Wrapper errors have an owner even if the first projection/list growth
        # fails. Raw error and traceback precede every fallible encoding.
        self.run_failure={"raw_error":None,"traceback":None,"encoded":None,
            "projection_error":None,"prefix_recording_error":None,
            "result_recording_error":None}

    def effect(self, operation, function, *args, **kwargs):
        # Preserve the real operands/result even if the after-effect journal
        # append itself fails. Recording failure never turns it into success.
        owner={"operation":operation,"arguments":[args,kwargs],"raw_result":None,
            "raw_error":None,"recording_error":None,"row":None}
        self.pending_effect=owner
        self.raw_effects.append(owner)
        self.tail.append({"operation":"Root.effect.reserve","effect":operation},owner)
        require(not self.ledger.failed or operation.startswith("Root.export"),
            "no ordinary effect after recorder damage; qualified original-reserve export only")
        try:
            raw=function(*args,**kwargs)
        except BaseException as error:
            owner["raw_error"]=error
            try:owner["exception_graph"]=self.tail.capture_error(error)
            except BaseException as capture:owner["error_graph_capture_error"]=capture
            if self.lifetime.first_error is None:self.lifetime.first_error=error
            self.record_owned_effect(owner)
            raise
        owner["raw_result"]=raw
        self.record_owned_effect(owner)
        require(owner["recording_error"] is None,"Root effect result owned but recording damaged")
        return raw

    def record_owned_effect(self,owner):
        try:
            owner["row"]={"namespace":"Root_recipient","operation":owner["operation"],
                "arguments":value(owner["arguments"]),"result":value(owner["raw_result"]),
                "error":None if owner["raw_error"] is None else error_value(owner["raw_error"])}
            self.tail.append(owner["row"],owner)
            if not self.ledger.sealed and not self.ledger.prefix_sealed:self.ledger.append(owner["row"])
        except BaseException as failure:
            owner["recording_error"]=failure
            self.recording_pending=[owner,failure]
            self.ledger.failed=True
            try:self.recording_errors.append(self.recording_pending)
            except BaseException as later:self.recording_pending=[owner,failure,later]

    def now_left(self):
        return min(self.admission["original_wall_end"] - time.time(),
            self.admission["original_monotonic_end"] - time.monotonic())

    def check_adoption_before_launch(self):
        # GET only: external Root installs subreaper custody, never this Source.
        flag = ctypes.c_int()
        libc = ctypes.CDLL(None, use_errno=True)
        require(libc.prctl(37, ctypes.byref(flag), 0, 0, 0) == 0 and flag.value == 1,
            "future actual Root must already own adopted descendants as subreaper")
        children = self.proc_bytes("/proc/self/task/%d/children" % self.launcher_pid, 4097)
        require(not children.strip(), "isolated Root launcher has no preexisting child to reap")
        self.ledger.append({"namespace":"Root_recipient","operation":"Root.adoption.before_launch",
            "launcher_pid":self.launcher_pid,"actual_subreaper_get":flag.value,
            "actual_children_raw":value(children),"Source_installed_policy":False})

    def proc_bytes(self, path, cap):
        fd = self.lifetime.acquire_fd("Root.lifetime.proc." + path, os.open,
            path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            raw = self.meter.read(fd, cap, scope="Root_proc")
            require(len(raw) < cap, "Root exact lifetime proc object not truncated")
            self.ledger.append({"namespace":"Root_recipient","operation":"Root.lifetime.proc_read",
                "path":path,"requested":cap,"raw":value(raw)})
            return raw
        finally:
            self.lifetime.close_fd(fd, "Root.lifetime.proc." + path)

    def hold_generation(self, pid, relation, expected_parent):
        require(type(pid) is int and pid > 0 and pid not in self.generation_custody,
            "new actual Root-owned child generation")
        pidfd = self.lifetime.acquire_fd("Root.child.same_generation.pidfd", os.pidfd_open, pid, 0)
        raw = self.proc_bytes("/proc/%d/stat" % pid, 4097)
        fields = raw.decode("ascii").rsplit(")", 1)[1].split()
        require(int(fields[1]) == expected_parent and int(fields[19]) > 0,
            "Root actual direct/adopted child membership before wait")
        row = {"pid":pid,"start_ticks":int(fields[19]),"parent_pid":int(fields[1]),
            "same_held_pidfd":pidfd,"relation":relation,"wait4":None}
        self.generation_custody[pid] = row
        if relation=="direct_caller":self.tail.bind_native_generation(self,row)
        self.ledger.append({"namespace":"Root_recipient","operation":"Root.child.held_generation",**row})
        return row

    def inspect_adopted_children(self):
        raw = self.proc_bytes("/proc/self/task/%d/children" % self.launcher_pid, 4097)
        for token in raw.split():
            pid = int(token)
            if pid not in self.generation_custody:
                self.hold_generation(pid, "actual_subreaper_adopted", self.launcher_pid)
        for pid, row in self.generation_custody.items():
            if row["relation"] == "direct_caller" or row["wait4"] is not None:
                continue
            waited, status, usage = os.wait4(pid, os.WNOHANG)
            self.ledger.append({"namespace":"Root_recipient","operation":"Root.adopted.wait4_returned",
                "pid_argument":pid,"same_held_pidfd":row["same_held_pidfd"],
                "result":value((waited,status,list(usage)))})
            if waited:
                require(waited == pid and select.select([row["same_held_pidfd"]],[],[],0)[0],
                    "Root adopted wait binds the same held exited generation")
                row["wait4"] = {"pid":waited,"status":status,"usage":list(usage)}
        return all(row["wait4"] is not None for row in self.generation_custody.values())

    def held_source(self, path, pin):
        before = os.lstat(path)
        require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o600
            and before.st_nlink == 1 and before.st_uid == before.st_gid == 1000
            and identity(before) == pin["identity"], "Root independent source custody before child effects")
        fd = self.lifetime.acquire_fd("Root.source." + path, os.open,
            path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.owned_fds.add(fd)
        require(identity(os.fstat(fd)) == pin["identity"], "Root opened source full identity")
        raw = self.meter.read(fd, before.st_size + 1, 0, scope="Root_source_custody")
        require(len(raw) == before.st_size and hashlib.sha256(raw).hexdigest() == pin["sha256"]
            and identity(os.fstat(fd)) == pin["identity"] and identity(os.lstat(path)) == pin["identity"],
            "Root independently held exact Source full bytes")
        self.ledger.append({"namespace": "Root_recipient", "operation": "source.held_before_launch",
            "path": path, "identity9": identity(before), "sha256": pin["sha256"], "bytes": len(raw)})
        return raw

    def setup(self, stage1_raw, caller_path):
        require(type(stage1_raw) is bytes and 0 < len(stage1_raw) <= 131072
            and hashlib.sha256(stage1_raw).hexdigest() == self.admission["stage1_sha256"],
            "Root independently selected complete stage1 wire")
        # Observer custody must not preempt the Source's own malformed/early
        # input guards. This independent Source selection is outside that wire.
        inventory=self.admission["independent_Source_inventory"]
        require(type(inventory) is dict and set(inventory)=={"package_root","sender_path",
            "controller_path","index_path","mandatory_source_hashes","Source54_identities"},
            "independently Root-selected whole Source inventory")
        pins = self.admission["source_pins"]
        require(type(pins) is dict and caller_path in pins and len(pins) >= 2,
            "Root complete separate launcher/caller/sender/Source54 pin graph")
        mandatory54 = inventory["mandatory_source_hashes"]
        require(type(mandatory54) is dict and len(mandatory54) == 54
            and set(mandatory54) == set(inventory["Source54_identities"]),
            "Root original complete mandatory54, no partial Source selection")
        for relative, digest in mandatory54.items():
            require(type(relative) is str and not relative.startswith("/")
                and all(part not in ("",".","..") for part in relative.split("/")), "Root exact inventory relative name")
            path = inventory["package_root"] + "/" + relative
            require(path in pins and pins[path]["sha256"] == digest
                and pins[path]["identity"] == inventory["Source54_identities"][relative],
                "Root own exact independently pinned whole54 before caller")
        for path in (caller_path,inventory["sender_path"],inventory["controller_path"],
                inventory["index_path"],__file__):
            require(path in pins, "Root actual whole launcher/caller/sender/controller/index custody")
        for path, pin in pins.items():
            raw = self.held_source(path, pin)
            self.ledger.allowed_byte_pins[pin["sha256"]]=pin["identity"][6]
            self.ledger.hold_full_byte_object(raw)
            if path == caller_path or path.endswith("/staged_sender.py"):
                require(0 < len(raw) <= SMALL_SOURCE_MAX, "Root caller/sender original byte caps")
        self.check_adoption_before_launch()
        read_fd, write_fd = self.lifetime.pipe(os.O_CLOEXEC | os.O_NONBLOCK)
        self.pipe_read = read_fd
        self.owned_fds.add(read_fd)
        # FD197 is owned only by this Root launcher. close_fds prevents inheritance.
        require(ROOT_WRITER_FD not in self.owned_fds, "Root endpoint descriptor collision")
        try:
            fcntl.fcntl(ROOT_WRITER_FD, fcntl.F_GETFD)
        except OSError as error:
            require(error.errno == errno.EBADF, "Root fixed endpoint status")
        else:
            raise RuntimeError("Root descriptor197 already owned; never overwrite")
        self.lifetime.acquire_fd("Root.fixed197.writer", os.dup2,
            write_fd, ROOT_WRITER_FD, inheritable=False)
        self.lifetime.close_fd(write_fd, "Root.pipe.temporary_writer")
        self.owned_fds.add(ROOT_WRITER_FD)
        self.ledger.append({"namespace": "Root_recipient", "operation": "collector.owned_before_launch",
            "pipe_identity9": identity(os.fstat(read_fd)), "ledger_identity9": identity(os.fstat(self.ledger.writer)),
            "original_native_identity9": self.original_fds, "Source_ledger_access": False,
            "original_stage1_hex": stage1_raw.hex(), "original_stage1_sha256": self.admission["stage1_sha256"]})
        self.child = self.lifetime.process(["/usr/bin/python3.14", "-I", "-S", "-B", caller_path],
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, restore_signals=True)
        self.hold_generation(self.child.pid, "direct_caller", expected_parent=self.launcher_pid)
        self.ledger.append({"namespace": "Root_recipient", "operation": "caller.spawn_returned",
            "pid": self.child.pid, "direct_parent_pid": os.getpid(),
            "caller_entry_inherited_fds": [0, 1, 2], "observer_ownership": "ROOT_NOT_CALLER_CONTROLLER"})
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            os.set_blocking(stream.fileno(), False)
        for fd in (0, 1, 2):
            os.set_blocking(fd, False)
        self.input_buffer.extend(stage1_raw)

    def relay_write(self, fd, buffer, operation):
        if not buffer:
            return
        try:
            count = self.effect("Root.relay.write",os.write,fd,bytes(buffer[:4096]))
        except BlockingIOError as error:
            self.ledger.append({"namespace": "Root_recipient", "operation": operation,
                "arguments":value([fd,bytes(buffer[:4096])]),
                "would_block": True, "bytes_written": 0, "error":error_value(error)})
            return
        except BaseException as error:
            self.ledger.append({"namespace":"Root_recipient", "operation":operation,
                "arguments":value([fd,bytes(buffer[:4096])]),"error":error_value(error)})
            raise
        self.ledger.append({"namespace": "Root_recipient", "operation": operation,
            "arguments":value([fd,bytes(buffer[:4096])]),
            "would_block": False, "bytes_written": count, "raw_prefix_hex": bytes(buffer[:count]).hex()})
        require(count > 0, "Root relay progress")
        del buffer[:count]

    def inspect_controller_request(self, request):
        """Actual independent Root acquisition; never copy Source claims as facts.
        Root holds pidfd/procfd and the two read-only sealed objects before any
        independently selected stage2 is relayed. Claimed IDs bind this observed
        generation only after the real proc/descriptor/body checks below.
        """
        pid = request["holder_pid"]
        ticks = request["holder_start_ticks"]
        require(type(pid) is int and pid > 0 and type(ticks) is int and ticks > 0,
            "Root genuine request PID/ticks type")
        pidfd = self.lifetime.acquire_fd("Root.controller.same_generation.pidfd", os.pidfd_open, pid, 0)
        self.owned_fds.add(pidfd)
        proc = self.lifetime.acquire_fd("Root.controller.procfd", os.open,
            "/proc/%d" % pid, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        self.owned_fds.add(proc)
        def proc_read(relative, cap):
            fd = self.lifetime.acquire_fd("Root.controller.proc_read." + relative, os.open,
                relative, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=proc)
            try:
                raw = self.meter.read(fd, cap, scope="Root_proc")
                self.ledger.append({"namespace":"Root_recipient","operation":"controller.proc_read",
                    "relative":relative,"requested":cap,"raw_hex":raw.hex(),"actual_bytes":len(raw)})
                return raw
            finally:
                self.lifetime.close_fd(fd, "Root.controller.proc_read." + relative)
        before = proc_read("stat", 4097).decode("ascii").rsplit(")", 1)[1].split()
        require(int(before[1]) == self.child.pid and int(before[19]) == ticks
            and not select.select([pidfd], [], [], 0)[0], "Root independently observed live caller-owned controller generation")
        observed = []
        for target, maximum in ((198, 131072), (199, 65536)):
            flags = proc_read("fdinfo/%d" % target, 4097).decode("ascii").splitlines()
            originals = [int(line.split()[1], 8) for line in flags if line.startswith("flags:")]
            require(len(originals) == 1 and originals[0] & os.O_ACCMODE == os.O_RDONLY
                and originals[0] & os.O_CLOEXEC, "Root actual original holder readonly/CLOEXEC flags")
            held = self.lifetime.acquire_fd("Root.controller.held%d" % target, os.open,
                "fd/%d" % target, os.O_RDONLY | os.O_CLOEXEC, dir_fd=proc)
            self.owned_fds.add(held)
            info = os.fstat(held)
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o400
                and info.st_uid == info.st_gid == 1000 and info.st_nlink == 0
                and 0 < info.st_size <= maximum
                and fcntl.fcntl(held, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
                and fcntl.fcntl(held, fcntl.F_GET_SEALS) & SEALS == SEALS,
                "Root actually held readonly fully sealed original objects")
            raw = self.meter.read(held, info.st_size + 1, 0, scope="Root_held_object")
            require(len(raw) == info.st_size and identity(os.fstat(held)) == identity(info),
                "Root independent complete held object exact bytes/identity")
            observed.append({"fd":target,"Root_held_fd":held,"identity9":identity(info),
                "sha256":hashlib.sha256(raw).hexdigest(),"raw_hex":raw.hex(),"seals":SEALS,
                "original_flags":originals[0]})
        after = proc_read("stat", 4097).decode("ascii").rsplit(")", 1)[1].split()
        require(int(after[1]) == self.child.pid and int(after[19]) == ticks
            and not select.select([pidfd], [], [], 0)[0], "Root stable independent live holder generation")
        self.independent_controller_custody = {"holder_pid":pid,"holder_start_ticks":ticks,
            "Root_same_held_pidfd":pidfd,"Root_same_held_procfd":proc,
            "objects":observed,"direct_parent_pid_actual":self.child.pid}
        self.ledger.append({"namespace":"Root_recipient","operation":"controller.custody_independently_held",
            **self.independent_controller_custody,"Source_issued_Root_fact":False})

    def observe_native_lines(self, raw):
        self.native_lines.extend(raw)
        while b"\n" in self.native_lines:
            line, rest = self.native_lines.split(b"\n", 1)
            self.native_lines = bytearray(rest)
            document = unique(line)
            if document.get("status") == "ROOT_CUSTODY_REQUEST":
                self.inspect_controller_request(document)

    def check_stage2_before_relay(self, raw):
        require(self.independent_controller_custody is not None,
            "Root actual independent held custody before stage2 relay")
        stage2 = unique(raw)
        require(encode(stage2) == raw and set(stage2) == {"schema","phase","assignment","generation",
            "root_thread","stage1_sha256","holder_pid","holder_start_ticks","task","expected_task_sha256"},
            "Root exact independently selected original stage2, no new grant")
        custody = self.independent_controller_custody
        task = stage2["task"]
        require(stage2["holder_pid"] == custody["holder_pid"]
            and stage2["holder_start_ticks"] == custody["holder_start_ticks"]
            and stage2["stage1_sha256"] == self.admission["stage1_sha256"]
            and task["expectation_identity"] == custody["objects"][0]["identity9"]
            and task["held_admission_identity"] == custody["objects"][1]["identity9"]
            and hashlib.sha256(encode(task)).hexdigest() == stage2["expected_task_sha256"],
            "Root actual separately supplied ten-key TASK against own held observations")
        self.ledger.append({"namespace":"Root_recipient","operation":"stage2.independent_input_checked",
            "raw_hex":raw.hex(),"sha256":hashlib.sha256(raw).hexdigest(),
            "origin":"ACTUAL_ROOT_NATIVE_INPUT_EXTERNAL_NOT_SOURCE_LABEL"})

    def run(self, stage1_raw, caller_path):
        result = {"schema": "friday.a132.future-independent-Root-result.v1",
            "GO": False, "release_credit": False, "source_facts_are_not_Root_grants": True,
            "whole_resource_compliance": "UNKNOWN_NOT_ZERO_NOT_PROVEN"}
        try:
            self.setup(stage1_raw, caller_path)
            eof = set()
            stage2_buffer = bytearray()
            stage2_relayed = False
            while True:
                left = self.now_left()
                require(left > 0, "Root same original dual terminal ends")
                reads = [] if self.pipe_read in eof else [self.pipe_read]
                for stream in (self.child.stdout, self.child.stderr):
                    if stream.fileno() not in eof:
                        reads.append(stream.fileno())
                if 0 not in eof and len(self.input_buffer) < 131072:
                    reads.append(0)
                writes = []
                if self.input_buffer and not self.child.stdin.closed:
                    writes.append(self.child.stdin.fileno())
                if self.output_buffer:
                    writes.append(1)
                ready, writable, _ = self.effect("Root.selector.select",select.select,reads,writes,[],min(left,0.25))
                self.ledger.append({"namespace": "Root_recipient", "operation": "selector.returned",
                    "read_fds": ready, "write_fds": writable,
                    "wall": time.time(), "monotonic": time.monotonic()})
                for fd in ready:
                    scope = ("Root_sideband" if fd == self.pipe_read else
                        "Root_native_input" if fd == 0 else "Root_native_output")
                    # At exhaustion, request one byte through the real denial
                    # guard; never turn an unperformed EOF read into actual zero.
                    requested=min(4096,max(1,self.meter.allowance-self.meter.returned))
                    raw=(self.ledger.read_sideband(self.meter,fd,requested)
                        if fd==self.pipe_read else self.meter.read(fd,requested,scope=scope))
                    if not raw:
                        eof.add(fd)
                        self.ledger.append({"namespace": "Root_recipient", "operation": "original_channel.actual_EOF", "fd": fd})
                        if fd == self.pipe_read:
                            self.collector.finish()
                        continue
                    if fd == self.pipe_read:
                        self.ledger.append({"namespace":"Root_recipient","operation":"sideband.raw_received",
                            "fd":fd,"requested":requested,
                            "stream_offset":self.collector.received,"actual_bytes":len(raw),
                            "full_bytes_recipe":"SAME_ROOT_HELD_ORIGINAL_HEADERS_EVENTS_AND_FAILED_PREFIX"})
                        self.collector.feed(raw)
                    elif fd == 0:
                        self.stdin_bytes += len(raw)
                        self.ledger.append({"namespace": "Root_recipient", "operation": "original_Root_input.received",
                            "raw_hex": raw.hex(), "prefix_bytes": self.stdin_bytes})
                        stage2_buffer.extend(raw)
                        require(len(stage2_buffer) <= 131072, "Root original complete stage2 envelope")
                        if not stage2_relayed and stage2_buffer.endswith(b"\n"):
                            self.check_stage2_before_relay(bytes(stage2_buffer))
                            self.input_buffer.extend(stage2_buffer)
                            stage2_buffer.clear()
                            stage2_relayed = True
                    elif fd == self.child.stdout.fileno():
                        self.stdout += len(raw)
                        require(self.stdout <= NATIVE_MAX, "Root original native envelope no raise")
                        self.stdout_hash.update(raw)
                        self.output_buffer.extend(raw)
                        self.ledger.append({"namespace": "Root_recipient", "operation": "caller.stdout.received",
                            "raw_hex": raw.hex(), "prefix_bytes": self.stdout})
                        self.observe_native_lines(raw)
                    else:
                        self.stderr += len(raw)
                        self.stderr_hash.update(raw)
                        self.ledger.append({"namespace":"Root_recipient","operation":"caller.stderr.received",
                            "raw":value(raw),"prefix_bytes":self.stderr})
                        # Original stderr is preserved; errors/prefixes are retained.
                        at = 0
                        while at < len(raw):
                            require(self.now_left() > 0, "Root original stderr end")
                            if select.select([], [2], [], self.now_left())[1]:
                                count = self.effect("Root.original_fd2.write",os.write,2,raw[at:])
                                require(count > 0, "Root original stderr progress")
                                self.ledger.append({"namespace": "Root_recipient", "operation": "original_fd2.write_returned",
                                    "bytes_written": count, "raw_prefix_hex": raw[at:at + count].hex()})
                                at += count
                for fd in writable:
                    if fd == 1:
                        self.relay_write(1, self.output_buffer, "original_fd1.write_returned")
                    else:
                        self.relay_write(fd, self.input_buffer, "caller.fd0.write_returned")
                if 0 in eof and not self.input_buffer and not self.child.stdin.closed:
                    self.child.stdin.close()
                    self.ledger.append({"namespace": "Root_recipient", "operation": "caller.fd0.closed_after_actual_Root_EOF"})
                if self.source_wait is None:
                    pid, status, usage = self.tail.outside_wait4(
                        self.generation_custody[self.child.pid],os.WNOHANG)
                    if pid:
                        require(pid == self.child.pid, "Root exact owned direct caller wait4")
                        self.source_wait = {"pid": pid, "status": status, "raw_usage": list(usage)}
                        self.child.returncode = os.waitstatus_to_exitcode(status)
                        generation = self.generation_custody[self.child.pid]
                        require(select.select([generation["same_held_pidfd"]],[],[],0)[0],
                            "Root direct wait binds same held exited caller generation")
                        generation["wait4"] = dict(self.source_wait)
                        self.ledger.append({"namespace": "Root_recipient", "operation": "caller.owned_wait4", **self.source_wait})
                        # Root's writer must close after caller exits, then actor writers
                        # determine genuine sideband EOF; EOF is never guessed from wait.
                        self.lifetime.close_fd(ROOT_WRITER_FD, "Root.fixed197.after_actual_caller_wait")
                        self.owned_fds.remove(ROOT_WRITER_FD)
                descendants_reaped = self.inspect_adopted_children()
                if (self.source_wait is not None and descendants_reaped and self.pipe_read in eof
                        and self.child.stdout.fileno() in eof and self.child.stderr.fileno() in eof
                        and not self.output_buffer):
                    break
            self.collector.finish()
            require(self.meter.accounting_complete, "Root incomplete read accounting cannot seal success")
            require(self.child.pid in self.collector.terminal, "Root caller actual terminal intent missing")
            self.ledger.append({"namespace": "Root_recipient", "operation": "postdelivery.actual_terminal",
                "caller_native_exit": self.child.returncode, "original_stdout_bytes": self.stdout,
                "original_stdout_sha256": self.stdout_hash.hexdigest(), "original_stderr_bytes": self.stderr,
                "original_stderr_sha256": self.stderr_hash.hexdigest(), "Root_reads_returned": self.meter.returned,
                "Root_reads_requested": self.meter.requested, "Root_read_calls": self.meter.calls,
                "Root_read_counters": self.meter.snapshot(),
                "source_sideband_frames": self.collector.frames, "source_sideband_received_bytes": self.collector.received,
                "whole_actor_syscall_coverage": "NOT_PROVEN", "whole_aggregate_RAM_implicit_IO": "NOT_PROVEN"})
            result.update(native_exit_actual=self.child.returncode, independent_ledger_pin=self.ledger.seal(),
                collector_complete=True, all_actor_internals_complete=False,
                admitted_whole_bound_proof=self.admission["whole_bound_proof_sha256"])
        except BaseException as error:
            failure=self.run_failure
            failure["raw_error"]=error
            failure["traceback"]=error.__traceback__
            try:failure["exception_graph"]=self.tail.capture_error(error)
            except BaseException as capture:failure["error_graph_capture_error"]=capture
            if self.lifetime.first_error is None:self.lifetime.first_error=error
            self.lifetime.last_error=error
            try:
                failure["encoded"]=error_value(error)
                self.unknown.append(failure["encoded"])
            except BaseException as projection:
                failure["projection_error"]=projection
                self.lifetime.recording_damage=True
            try:
                self.ledger.append({"namespace":"Root_recipient","operation":"collector.failed_prefix",
                    "error":failure["encoded"],"unparsed_native_tail":value(bytes(self.native_lines)),
                    "unparsed_sideband_tail":value(bytes(self.collector.buffer)),
                    "pending_events":value({key:bytes(row[2]) for key,row in self.collector.pending.items()}),
                    "original_fd1_undelivered":value(bytes(self.output_buffer)),
                    "last_actual_read_result":value(self.meter.last_effect_result),
                    "pending_actual_effect":self.pending_effect,
                    "actual_read_counters":self.meter.snapshot()})
            except BaseException as record_error:
                failure["prefix_recording_error"]=record_error
                self.lifetime.recording_damage = True
            # This retained raw object is deliberately not JSON-certified. Its
            # eventual outside custody is a separate mandatory open boundary.
            try:
                result.update(collector_complete=False, status="STOP_UNCONFIRMED",
                    native_exit_actual=None if self.source_wait is None else self.child.returncode,
                    independent_ledger_pin=None, retained_prefix_rows=self.ledger.rows,
                    retained_prefix_bytes=self.ledger.bytes, unknown=list(self.unknown),
                    whole_actor_internals_complete=False, Root_read_counters=self.meter.snapshot())
            except BaseException as result_error:
                failure["result_recording_error"]=result_error
                raise error from result_error
            # No kill, retry, endpoint refresh, fabricated EOF, or global absence.
            # Failure-path finite containment/adoption/Root durability is unsatisfied.
        return result

    def retain(self, result, leaf):
        """Retain exact sealed contents in Root's admitted private result directory.
        Ledger replay/export costs count cumulatively in the same Root meter.
        Failure retains original custody and explicit unknown; it grants no success.
        """
        require(self.ledger.sealed or self.ledger.prefix_sealed, "Root literal complete or explicitly incomplete held prefix required")
        directory = self.effect("Root.export.lstat",os.lstat,self.retained_root)
        require(stat.S_ISDIR(directory.st_mode) and stat.S_IMODE(directory.st_mode) == 0o700
            and directory.st_uid == directory.st_gid == 1000, "Root retained private directory")
        require(type(leaf) is str and leaf and "/" not in leaf and leaf not in (".", ".."), "Root exact retention leaf")
        directory_fd = self.lifetime.acquire_fd("Root.export.directory", os.open,
            self.retained_root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        require(identity(self.effect("Root.export.fstat",os.fstat,directory_fd)) == identity(directory), "Root retained directory held identity")
        fd = self.lifetime.acquire_fd("Root.export.file", os.open, leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < self.ledger.bytes:
                raw = self.meter.read(self.ledger.reader, min(8192, self.ledger.bytes - offset), offset, scope="Root_export")
                require(raw, "Root held export fullbytes")
                at = 0
                while at < len(raw):
                    count = self.effect("Root.export.write",os.write,fd, raw[at:])
                    require(count > 0, "Root exact retained export write progress")
                    at += count
                offset += len(raw)
            self.effect("Root.export.fsync",os.fsync,fd)
            retained = self.effect("Root.export.fstat",os.fstat,fd)
            require(stat.S_ISREG(retained.st_mode) and stat.S_IMODE(retained.st_mode) == 0o600
                and retained.st_nlink == 1 and retained.st_size == self.ledger.bytes
                and identity(self.effect("Root.export.stat",os.stat,leaf, dir_fd=directory_fd, follow_symlinks=False)) == identity(retained),
                "Root exact retained fullbytes after fsync held/named identity")
            self.effect("Root.export.fsync",os.fsync,directory_fd)
            require(identity(self.effect("Root.export.fstat",os.fstat,directory_fd)) == identity(self.effect("Root.export.lstat",os.lstat,self.retained_root)),
                "Root retained directory stable after persistence")
            object_pins=[]
            for pin,item in self.ledger.byte_objects.items():
                object_leaf="Root-full-byte-object-"+pin+".json"
                object_fd=self.lifetime.acquire_fd("Root.export.full_byte_object",os.open,object_leaf,
                    os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
                try:
                    cursor=0
                    digest=hashlib.sha256()
                    while cursor<item["bytes"]:
                        raw=self.meter.read(item["reader"],min(8192,item["bytes"]-cursor),cursor,scope="Root_export")
                        require(raw,"Root full byte-object actual export not EOF")
                        digest.update(raw)
                        at=0
                        while at<len(raw):
                            count=self.effect("Root.export.write",os.write,object_fd,raw[at:])
                            require(count>0,"Root full byte-object retained write progress")
                            at+=count
                        cursor+=len(raw)
                    self.effect("Root.export.fsync",os.fsync,object_fd)
                    info=self.effect("Root.export.fstat",os.fstat,object_fd)
                    require(info.st_size==item["bytes"] and digest.hexdigest()==pin
                        and stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1
                        and identity(info)==identity(self.effect("Root.export.stat",os.stat,object_leaf,dir_fd=directory_fd,follow_symlinks=False)),
                        "Root full exact retained reversible byte-object identity and SHA")
                    object_pins.append({"path":self.retained_root+"/"+object_leaf,"identity9":identity(info),
                        "bytes":cursor,"sha256":pin,"complete_original_bytes":True})
                finally:
                    self.lifetime.close_fd(object_fd,"Root.export.full_byte_object.final_close")
            self.effect("Root.export.fsync",os.fsync,directory_fd)
        finally:
            self.lifetime.close_fd(fd, "Root.export.file.final_close")
            self.lifetime.close_fd(directory_fd, "Root.export.directory.final_close")
        return {"path": self.retained_root + "/" + leaf, "bytes": self.ledger.bytes,
            "complete":self.ledger.sealed and not self.ledger.failed,
            "sha256": self.ledger.digest.hexdigest(), "independently_Root_held": True,
            "full_reversible_byte_objects":object_pins,
            "Root_export_returned_bytes_cumulative": self.meter.returned,
            "Root_read_counters": self.meter.snapshot(), "GO": False}

    def consume_all32_case_by_same_Root(self,pinned_schema_data,case,variables,
            variable_bindings,input_documents,fault,native,returned,native_wire,native_exit):
        """Connected future Root-owned consumer, before custody-owner release.

        The existing actual Root must have independently loaded these exact three
        Root-only modules from its held reviewed Source graph; import is not an
        authority claim. Loader/stdlib implicit reads remain additional unknown
        billable work, never discharged by a previous Source hash operation.
        """
        import ordinary_root_consumers as consumer
        for module in (consumer,consumer.G,consumer.N):
            path=os.path.abspath(module.__file__)
            require(path in self.admission["source_pins"],"same independently held reviewed Root module graph")
            pin=self.admission["source_pins"][path]
            require(identity(os.lstat(path))==pin["identity"],"Root module held/current full identity")
        require(self.ledger.sealed and self.ledger.reader is not None,
            "same actual Root still owns complete readonly ledger/byte-object custody")
        return consumer.All32RootConsumer(pinned_schema_data,self.ledger).consume(case,variables,
            variable_bindings,input_documents,fault,native,returned,native_wire,native_exit)

    def close(self):
        closed = self.lifetime.close()
        closed.update(direct_caller_reaped=self.source_wait is not None,
            exact_same_held_generations=list(self.generation_custody.values()),
            all_descendant_terminal_ownership=("OBSERVED_ALL_HELD_REAPED" if self.source_wait is not None
                and all(row["wait4"] is not None for row in self.generation_custody.values()) else "STOP_UNCONFIRMED"))
        return closed

def invoke_by_existing_Root(external_admission, retained_root, stage1_raw, caller_path, independent_tail):
    """Concrete future native entry: no callback, local grant, or source RUN issuer.
    The actual existing Root separately reviews/adopts this launcher's exact bytes
    and original OS topology/resource contract BEFORE invoking this function.
    Native fd0 remains the real Root stage2 channel; fd1/2 relay original outputs.
    The external tool observes this launcher's final process exit independently.
    A132 has no valid whole_bound_proof and therefore admits no invocation.
    """
    entry=independent_tail.reserve_invocation(external_admission,retained_root,stage1_raw,caller_path)
    lifetime=launcher=None
    result = {"schema":"friday.a137.Root-invocation-result.v1","GO":False,"release_credit":False}
    entry['result']=result
    entry_failure={"error":None,"traceback":None,"projection_error":None,"cleanup_error":None}
    independent_tail.own_raw(entry_failure)
    try:
        lifetime=RootLifetime.__new__(RootLifetime);entry['lifetime']=lifetime
        RootLifetime.__init__(lifetime)
        launcher=RootNativeLauncher.__new__(RootNativeLauncher);entry['launcher']=launcher
        launcher.lifetime=lifetime
        selection=independent_tail.select_before_birth(launcher,lifetime)
        RootNativeLauncher.__init__(launcher, external_admission, retained_root, lifetime=lifetime,
            independent_tail=independent_tail)
        selection["constructor_returned"]=True
        result = launcher.run(stage1_raw, caller_path)
        entry['result']=result
        if launcher.ledger.failed:result["independent_incomplete_prefix_pin"]=launcher.ledger.seal_incomplete_prefix()
        if not launcher.ledger.sealed and not launcher.ledger.failed:
            result["independent_prefix_pin"] = launcher.ledger.seal()
        if launcher.ledger.sealed or launcher.ledger.prefix_sealed:
            result["Root_retention"] = launcher.retain(result, "Root-observer-ledger.jsonl")
            result["Root_retention"]["complete_collection"] = result.get("collector_complete",False)
    except BaseException as error:
        entry_failure["error"]=error;entry_failure["traceback"]=error.__traceback__
        entry['raw_error']=error;entry['raw_traceback']=error.__traceback__
        if lifetime is not None:
            if getattr(lifetime,'first_error',None) is None:lifetime.first_error=error
            lifetime.last_error=error
        result["original_exception_object"]=error
        result.update(status="STOP_UNCONFIRMED",collector_complete=False)
        try:result["exception"]=error_value(error)
        except BaseException as projection:entry_failure["projection_error"]=projection
    finally:
        if launcher is None:
            result.update(status='STOP_UNCONFIRMED',collector_complete=False,helper_native_end_verified=False)
            return result
        ledger=getattr(launcher,"ledger",None)
        try:
            adoption=independent_tail.adopt(launcher)
            require(adoption["accepted"],"receiver acceptance before local alias retirement")
            result["Root_adoption_receipt"]=adoption["receipt"]
            if ledger is not None:
                ledger.custody_acceptance=adoption
                if getattr(ledger,"prefix_sealed",False):
                    result["Root_incomplete_prefix_receipt"]=consume_adopted_incomplete_prefix(independent_tail,adoption)
            result["Root_final_close_evidence"] = lifetime.close()
            independent_tail.append({"operation":"Root.helper.final_close", "evidence":result["Root_final_close_evidence"]},lifetime)
            result["Root_actual_adopted_fds"]=[a["fd"] for a in adoption["aliases"]]
        except BaseException as tail_error:
            entry_failure["cleanup_error"]=tail_error
            entry['cleanup_error']=tail_error
            result["Root_tail_error_object"]=tail_error
            result.update(status="STOP_UNCONFIRMED",collector_complete=False)
        # The same genuine invoking Root process receives this live owner, not a
        # Source label, JSON grant or now-closed descriptor masquerading as held.
        # It must independently adopt/consume/close under the original contract.
        result["Root_owned_custody_object"] = launcher
        result["Root_partial_launcher_custody"] = {
            "constructor_returned":getattr(launcher,"admission",None) is external_admission,
            "same_held_generations":list(getattr(launcher,"generation_custody",{}).values())}
        try:
            import ordinary_root_consumers as _outer
            result["existing_outer_native"]=_outer.drive_existing_outer_native(independent_tail,launcher)
        except BaseException as outer_error:
            entry['outer_error']=outer_error
            result["existing_outer_native"]={"raw_error":outer_error,'raw_traceback':outer_error.__traceback__,
                "creations":len(getattr(launcher,"generation_custody",{}) or {}),'helper_native_end_verified':False}
        result['helper_native_end_verified']=False
        entry['native_helper_end_verified']=False
        outer=result.get("existing_outer_native")
        if type(outer) is dict:
            outer['helper_native_end_verified']=False
            outer['native_tool_helper_retirement']='NOT_QUALIFIED'
    return result

def main():
    """Concrete native command entry under separately authentic Root invocation.
    CLI input supplies data only; the independent actor/tool establishes origin.
    No author-generated envelope exists in A132, and no present RUN is selected.
    """
    import sys
    require(len(sys.argv) == 3 and sys.argv[1] == "--externally-selected-launch",
        "exact independently Root-selected native launcher command")
    require(resource.getrlimit(resource.RLIMIT_AS) == (AS_SOFT, AS_HARD)
        and resource.getrlimit(resource.RLIMIT_CPU) == (300, 7200)
        and resource.getrlimit(resource.RLIMIT_NOFILE) == (256, 256),
        "unchanged trusted pre-interpreter Root/caller ceilings")
    path = sys.argv[2]
    before = os.lstat(path)
    require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o400
        and before.st_nlink == 1 and before.st_uid == before.st_gid == 1000
        and 0 < before.st_size <= 131072, "external Root launch envelope private immutable named input")
    meter = ReadMeter()
    lifetime = RootLifetime()
    result = {"GO":False,"release_credit":False}
    launcher = RootNativeLauncher.__new__(RootNativeLauncher)
    try:
        fd = lifetime.acquire_fd("Root.external_envelope", os.open,
            path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        raw = meter.read(fd, before.st_size + 1, 0, scope="external_envelope")
        require(len(raw) == before.st_size and identity(os.fstat(fd)) == identity(before)
            and identity(os.lstat(path)) == identity(before), "external Root launch envelope exact stable custody")
        envelope = unique(raw)
        require(set(envelope) == {"admission", "retained_root", "stage1_wire_hex", "caller_path","outside_tail"},
            "external Root exact executable envelope")
        tail_data=envelope["outside_tail"]
        require(type(tail_data) is dict and set(tail_data)=={"fd","identity9","original_wall_end","original_monotonic_end"}
            and tail_data["fd"]==195,"already outside-owned exact durable Root tail transport")
        tail=ExistingRootTail(tail_data["fd"],tail_data["identity9"],
            tail_data["original_wall_end"],tail_data["original_monotonic_end"])
        # A standalone helper cannot transfer its live Python objects to its
        # outside tool by JSON/fd numbers. Only the in-process existing-parent
        # entry above supplies that topology. Refuse BEFORE child birth/closure.
        raise ValueError("CLI helper custody ABI not admitted; use actual existing-parent entry")
        RootNativeLauncher.__init__(launcher, envelope["admission"], envelope["retained_root"], meter, lifetime,
            independent_tail=tail)
        launcher.owned_fds.add(fd)
        launcher.ledger.append({"namespace": "Root_recipient", "operation": "external_Root_envelope.held",
            "identity9": identity(before), "sha256": hashlib.sha256(raw).hexdigest(),
            "authentication": "ACTUAL_EXISTING_ROOT_NATIVE_INVOCATION_EXTERNAL_NOT_INFERRED_FROM_DATA"})
        result = launcher.run(bytes.fromhex(envelope["stage1_wire_hex"]), envelope["caller_path"])
        if launcher.ledger.failed:result["independent_incomplete_prefix_pin"]=launcher.ledger.seal_incomplete_prefix()
        if not launcher.ledger.sealed and not launcher.ledger.failed:
            result["independent_prefix_pin"] = launcher.ledger.seal()
        if launcher.ledger.sealed or launcher.ledger.prefix_sealed:
            result["Root_retention"] = launcher.retain(result, "Root-observer-ledger.jsonl")
        cleanup = lifetime.close()
        tail.append({"operation":"Root.helper.final_close","evidence":cleanup},lifetime)
        result["Root_final_close_evidence"] = cleanup
        global ROOT_EXIT_EVIDENCE
        ROOT_EXIT_EVIDENCE = result
        require(result.get("collector_complete") and not cleanup["Root_owned_close_errors"],
            "Root complete original-channel handoff and independently retained custody required")
        return result["native_exit_actual"]
    except BaseException as error:
        if lifetime.first_error is None:lifetime.first_error=error
        result.update(status="STOP_UNCONFIRMED",exception=error_value(error),
            original_exception_object=error,Root_read_counters=meter.snapshot())
        ROOT_EXIT_EVIDENCE = result
        raise
    finally:
        active=sys.exc_info()[1]
        try:
            result["Root_final_close_evidence"] = lifetime.close()
            if lifetime.tail is not None:
                lifetime.tail.append({"operation":"Root.helper.finally.actual_close","evidence":result["Root_final_close_evidence"]},lifetime)
        except BaseException as cleanup_error:
            result["Root_final_cleanup_error_object"]=cleanup_error
            if lifetime.first_error is None:lifetime.first_error=cleanup_error
            lifetime.last_error=cleanup_error
            if active is None:raise

ROOT_EXIT_EVIDENCE = None

if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as error:
        # Memory/object custody survives through the actual exit. A native tool
        # must independently retain this result and observe exit; exit125 alone
        # never establishes complete raw failure/close persistence.
        if ROOT_EXIT_EVIDENCE is None:
            ROOT_EXIT_EVIDENCE = {"exception":error_value(error),"original_exception_object":error,"GO":False}
        exit_code = 125
    raise SystemExit(exit_code)
