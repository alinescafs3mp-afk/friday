"""UNISSUED non-current Source template; never run by its Source author.
Root independently fixes ACCEPTED/ROOT_THREAD and authenticates each native wire
before its exact relay. No clocks, grants, bodies or held objects are issued here.
UID, seals, a wire's claimed thread, and local hashes do not authenticate Root.
INIT creates transport only; AUTH alone may import the reviewed receiver.
"""
import fcntl, hashlib, importlib.util, json, os, resource, selectors, stat, time, types
_RAW_OS=types.SimpleNamespace(**vars(os))
from datetime import datetime, timezone

ASSIGNMENT = "ASTRA-E4-QUALITY-ROOT-NATIVE-FOCUSED-RUN-A075"
ROOT_THREAD = "UNISSUED"
ACCEPTED = 0
PACKAGE = "/var/tmp/friday-sol061-sol059-a179-sender-whole54-all96-all32-all7-connected-source-closure/Source54"
INDEX = "/var/tmp/friday-sol061-sol059-a179-sender-whole54-all96-all32-all7-connected-source-closure/index/full-source.json"
INDEX_SHA = "7e3c4bc1f24e7655dfd1f18798d77cc744fe6874081695aa652b6a0885d1a2db"
RECEIPT = "/var/tmp/friday-sol061-sol059-a179-sender-whole54-all96-all32-all7-connected-source-closure/Source54/tests/receipt_contract.py"
ISSUER_ROOT = "/home/jericho/.jericho/quality-source-admission"
ISSUER = "friday.owner.offline-source-run-admission.v1"
SOURCE = "ASTRA-E4-QUALITY-STABLE-ROOT-NAMESPACE-CLOSURE-A074#1"
ANCESTRY = [("/", 0, 0, 0o755), ("/home", 0, 0, 0o755),
    ("/home/jericho", 1000, 1000, 0o750),
    ("/home/jericho/.jericho", 1000, 1000, 0o700),
    (ISSUER_ROOT, 1000, 1000, 0o700)]
RESOURCES = {"worker_address_space_bytes_max": 1610612736, "worker_cpu_seconds_max": 300,
    "worker_wall_timeout_seconds_max": 300, "parallel_workers_max": 4,
    "dispatcher_address_space_bytes_max": 1610612736, "dispatcher_cpu_seconds_max": 7200,
    "aggregate_address_space_bytes_max": 8053063680, "memory_bill_bytes_max": 8589934592}
SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
READ_MAX, OUTPUT_MAX = 16777216, 32768
read_bytes = output_bytes = 0
held = []
START_WALL, START_MONO = time.time(), time.monotonic()

class SourceReadMeter:
 """Original controller actor budget, shared with its same-process receiver."""
 def __init__(self):
  self.maximum=16777216;self.used=self.requested=self.calls=0
  self.rows=[];self.pending=None;self.client=None;self.damaged=False
  self.inflight=0;self.accounting_complete=True;self.unknown_physical_reads=[]
  self.semantic_units=0;self.post_read_pending=None
 def read(self,operation,function,args,fd=None,offset=None):
  requested=args[1] if len(args)>1 else args[0]
  return self.perform(operation,function,args,{},requested,fd,offset,"bytes")
 def perform(self,operation,function,args,kwargs,requested,fd,offset,kind,buffers=()):
  row={"operation":operation,"arguments":args,"fd":fd,"offset":offset,
   "requested":requested,"prefix_returned":self.used,"returned":None,
   "kwargs":kwargs,"kind":kind,"buffers":buffers,"raw_buffer_after":None,
   "error":None,"denied":False,"eof":False,"would_block":False,
   "physical_count_known":kind!="buffered_stream","recording_error":None}
  self.pending=row
  self.rows.append(row)
  if type(requested) is not int or requested<0 or self.used+self.inflight+requested>self.maximum:
   row["denied"]=True
   self.record(row)
   raise ValueError("original_controller_read_bound")
  if self.client is not None:self.client.check()
  self.requested+=requested;self.calls+=1;self.inflight+=requested
  try:raw=function(*args,**kwargs)
  except BaseException as error:
   row["error"]=error;row["would_block"]=isinstance(error,BlockingIOError)
   if buffers:
    # A failing vector/readinto can mutate its buffers without returning a
    # count. Retain actual buffer owners first; never certify zero physical IO.
    self.accounting_complete=False
    row["physical_count_known"]=False
    try:row["raw_buffer_after"]=tuple(bytes(memoryview(b)) for b in buffers)
    except BaseException as capture:row["buffer_capture_error"]=capture
   self.record(row)
   raise
  finally:self.inflight-=requested
  row["returned"]=raw
  if kind=="count":
   amount=raw
   if raw is None:amount=0
   if buffers:
    try:row["raw_buffer_after"]=tuple(bytes(memoryview(b)) for b in buffers)
    except BaseException as capture:
     row["buffer_capture_error"]=capture;self.accounting_complete=False
  elif kind=="buffered_stream":
   # This is an actual semantic read hook, not a fictitious physical syscall.
   # Stock buffering/decoder read-ahead and prior buffered bytes do not equal
   # characters accepted by this call. Their raw-layer closure remains open.
   self.accounting_complete=False
   self.unknown_physical_reads.append(row)
   if type(raw) in (bytes,str):amount=len(raw)
   elif type(raw) in (int,type(None)):amount=0 if raw is None else raw
   elif type(raw) is list:
    amount=sum(len(part) for part in raw)
   else:amount=None
  else:amount=len(raw)
  if type(amount) is not int or amount<0 or amount>requested:
   row["count_error"]=ValueError("actual_read_count_exceeds_reserved_request")
   self.accounting_complete=False;self.record(row)
   raise row["count_error"]
  if kind=="buffered_stream":
   self.semantic_units+=amount
   row["semantic_units_not_physical_bytes"]=amount
   row["physical_bytes"]=None
  else:
   self.used+=amount
   row["physical_bytes"]=amount
  row["eof"]=requested>0 and (raw in (b"","") if kind=="buffered_stream" else amount==0)
  if raw is None:row["would_block"]=True;row["eof"]=False
  self.record(row)
  return raw
 def adapter(self,operation,function,args,kwargs,fd=None):
  if operation in ("os.readv","os.preadv"):
   buffers=args[1] if len(args)>1 else kwargs["buffers"]
   requested=sum(memoryview(b).nbytes for b in buffers)
   return self.perform(operation,function,args,kwargs,requested,
    args[0] if args else kwargs["fd"],args[2] if operation=="os.preadv" and len(args)>2 else kwargs.get("offset"),"count",buffers)
  bound=getattr(function,"__self__",None)
  name=operation.rsplit(".",1)[-1]
  buffers=(args[0],) if name in ("readinto","readinto1") and args else ()
  if buffers:requested=memoryview(buffers[0]).nbytes
  else:
   requested=args[0] if args else kwargs.get("size",kwargs.get("n",kwargs.get("hint",-1)))
   if type(requested) is int and requested<0:
    info=_RAW_OS.fstat(fd)
    # Only a held regular-file size bounds an unbounded semantic request.
    # A pipe/readall has no such proof; it remains a concrete open protocol.
    if not stat.S_ISREG(info.st_mode):
     requested=None
    else:requested=info.st_size+1
  return self.perform(operation,function,args,kwargs,requested,fd,None,"buffered_stream",buffers)
 def record(self,row):
  if self.client is not None:
   try:
    arguments=row["arguments"]
    if row["buffers"]:
     arguments={"fd":row["fd"],"offset":row["offset"],"requested":row["requested"],
      "buffer_after":row["raw_buffer_after"],"raw_operands_retained":True}
    self.client.note("Source.controller_read",arguments,
     {"requested":row["requested"],"prefix_returned":row["prefix_returned"],
      "returned":row["returned"],"denied":row["denied"],"eof":row["eof"],
      "would_block":row["would_block"],"actor_limit":self.maximum,
      "physical_count_known":row["physical_count_known"],
      "physical_bytes":row.get("physical_bytes"),
      "semantic_units_not_physical_bytes":row.get("semantic_units_not_physical_bytes"),
      "physical_accounting_complete":self.accounting_complete},
     error=row["error"])
   except BaseException as recorder:
    row["recording_error"]=recorder;self.damaged=True
 def bind(self,client):
  if self.client is not None and self.client is not client:raise ValueError("Source meter owner cannot change")
  self.client=client
  for row in self.rows:self.record(row)
_SemanticReadMeter=SourceReadMeter
class SourceReadMeter(_SemanticReadMeter):
 """Atomic reservations settle against ACTUAL raw calls, never characters."""
 def __init__(self):
  super().__init__()
  import _thread
  self.lock=_thread.allocate_lock();self.upper_used=0
  self.raw_pending=None;self.raw_operations=[];self.semantic_operations=[]
 def perform(self,operation,function,args,kwargs,requested,fd,offset,kind,buffers=()):
  row={"operation":operation,"arguments":args,"kwargs":kwargs,"fd":fd,"offset":offset,
   "requested":requested,"kind":kind,"buffers":buffers,"returned":None,"error":None,
   "raw_buffer_after":None,"recording_error":None,"denied":False,"eof":False,
   "would_block":False,"physical_bytes":None,"physical_count_known":False,
   "prefix_returned":None,"settled":False,"reserved":False}
  self.raw_pending=row;self.raw_operations.append(row)
  with self.lock:
   row["prefix_returned"]=self.used
   if type(requested) is not int or requested<0 or self.upper_used+self.inflight+requested>self.maximum:
    row["denied"]=True
   else:
    self.inflight+=requested;self.requested+=requested;self.calls+=1;row["reserved"]=True
  if row["denied"]:
   self.record(row)
   raise ValueError("original_controller_read_bound")
  try:
   if self.client is not None:self.client.check()
   row["returned"]=function(*args,**kwargs)
  except BaseException as error:
   row["error"]=error;row["would_block"]=isinstance(error,BlockingIOError)
   # The original operation/error and mutable buffers precede projection.
   # Unreturned physical counts are unknown; reserve the full request upper,
   # not a fictitious successful zero, and disallow complete accounting credit.
   with self.lock:
    self.inflight-=requested;self.upper_used+=requested;row["settled"]=True
    self.accounting_complete=False
   if buffers:
    try:row["raw_buffer_after"]=tuple(bytes(memoryview(b)) for b in buffers)
    except BaseException as capture:row["buffer_capture_error"]=capture
   self.record(row)
   raise
  raw=row["returned"]
  amount=(0 if raw is None else raw) if kind=="count" else (0 if raw is None else len(raw))
  with self.lock:
   # Release and actual charge are one indivisible transition. A second
   # thread cannot borrow the uncharged interval from this reservation.
   self.inflight-=requested;row["settled"]=True
   if type(amount) is int and 0<=amount<=requested:
    self.used+=amount;self.upper_used+=amount
    row["physical_bytes"]=amount;row["physical_count_known"]=True
   else:
    self.upper_used+=requested;self.accounting_complete=False
    row["count_error"]=ValueError("actual_read_count_exceeds_reserved_request")
  if buffers:
   try:row["raw_buffer_after"]=tuple(bytes(memoryview(b)) for b in buffers)
   except BaseException as capture:
    row["buffer_capture_error"]=capture;self.accounting_complete=False
  row["eof"]=requested>0 and amount==0 and raw is not None
  row["would_block"]=raw is None
  self.record(row)
  if "count_error" in row:raise row["count_error"]
  return raw
 def raw_read(self,operation,function,args,kwargs,requested,fd,kind="bytes",buffers=()):
  return self.perform(operation,function,args,kwargs,requested,fd,None,kind,buffers)
 def adapter(self,operation,function,args,kwargs,fd=None):
  if operation in ("os.readv","os.preadv"):
   buffers=args[1] if len(args)>1 else kwargs["buffers"]
   requested=sum(memoryview(b).nbytes for b in buffers)
   return self.perform(operation,function,args,kwargs,requested,
    args[0] if args else kwargs["fd"],args[2] if operation=="os.preadv" and len(args)>2 else kwargs.get("offset"),"count",buffers)
  # A high-level read can consume existing buffered data or call the actual
  # RawIOBase adapter repeatedly. Only that adapter is charged. readlines(hint)
  # is a hint, never an upper-bound reservation on line/character acceptance.
  row={"operation":operation,"arguments":[args,kwargs],"fd":fd,
   "raw_result":None,"raw_error":None,"before_raw_call":self.calls,"after_raw_call":None}
  self.post_read_pending=row;self.semantic_operations.append(row)
  try:row["raw_result"]=function(*args,**kwargs)
  except BaseException as error:row["raw_error"]=error;raise
  finally:row["after_raw_call"]=self.calls
  return row["raw_result"]
_METER=SourceReadMeter()
_SOURCE_PARTIAL=[]
class BootstrapOwner:
 def __init__(self):
  self.rows=[];self.pending=None;self.client=None;self.first_error=None;self.fd_states={}
  self.fd_history=[];self.cleanup_errors=[];self.cleanup_pending=None
 def call(self,operation,function,*args,**kwargs):
  active=sys.exc_info()[1]
  slot={"operation":operation,"arguments":[args,kwargs],"raw_result":None,"raw_error":None}
  self.pending=slot;self.rows.append(slot)
  if operation=="open":self.fd_history.append(slot)
  if self.client is not None and operation!="close":self.client.check()
  if operation=="close":
   state=self.fd_states.get(args[0])
   if state is not None:
    if state["attempted"]:raise RuntimeError("bootstrap ambiguous close: no retry")
    state["attempted"]=True
  try:slot["raw_result"]=function(*args,**kwargs)
  except BaseException as error:
   slot["raw_error"]=error
   if self.first_error is None:self.first_error=error
   self.record(slot)
   if operation=="close" and active is not None:return None
   raise
  if operation=="open":self.fd_states[slot["raw_result"]]={"attempted":False,"returned":False}
  if operation=="close" and state is not None:state["returned"]=True
  self.record(slot)
  return slot["raw_result"]
 def record(self,slot):
  if self.client is not None:
   try:self.client.note("controller.bootstrap."+slot["operation"],slot["arguments"],slot["raw_result"],slot["raw_error"])
   except BaseException as recorder:slot["recording_error"]=recorder
 def bind(self,client):
  self.client=client
  for row in self.rows:self.record(row)
  client.check()
_BOOTSTRAP=BootstrapOwner()
def now():
    value = time.time()
    require(abs(value - START_WALL - (time.monotonic() - START_MONO)) <= 2, "clock drift")
    return value

def require(ok, why):
    if not ok:
        raise ValueError(why)

def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
        separators=(",", ":")) + "\n").encode("ascii")

def object_wire(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "duplicate wire key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: require(False, "nonfinite wire"))
    require(type(value) is dict and encode(value) == raw, "canonical object wire")
    return value

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def identity(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]

def read_file(path, maximum, mode=0o600):
    global read_bytes
    before = _BOOTSTRAP.call("lstat",_RAW_OS.lstat,path) if _BOOTSTRAP.client is None else os.lstat(path)
    require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == mode
        and (before.st_uid, before.st_gid, before.st_nlink) == (1000, 1000, 1)
        and 0 < before.st_size <= maximum and _METER.used + before.st_size + 1 <= READ_MAX, "file bounds")
    fd = (_BOOTSTRAP.call("open",_RAW_OS.open,path,os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        if _BOOTSTRAP.client is None else os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC))
    try:
        opened=(_BOOTSTRAP.call("fstat",_RAW_OS.fstat,fd) if _BOOTSTRAP.client is None else os.fstat(fd))
        require(identity(opened) == identity(before), "opened file identity")
        raw = _METER.read("controller.read_file",_RAW_OS.pread,(fd,before.st_size+1,0),fd,0)
        after=(_BOOTSTRAP.call("fstat",_RAW_OS.fstat,fd) if _BOOTSTRAP.client is None else os.fstat(fd))
        named=(_BOOTSTRAP.call("lstat",_RAW_OS.lstat,path) if _BOOTSTRAP.client is None else os.lstat(path))
        require(len(raw) == before.st_size and identity(after) == identity(before)
            and identity(named) == identity(before), "stable file read")
        read_bytes = _METER.used
        return raw, identity(before)
    finally:
        if _BOOTSTRAP.client is None:_BOOTSTRAP.call("close",_RAW_OS.close,fd)
        else:os.close(fd)

def inventory(expected):
    require(type(expected) is dict and len(expected) == 54, "complete source54")
    for relative, pin in expected.items():
        parts = relative.split("/")
        require(all(p not in ("", ".", "..") for p in parts), "relative source path")
        for n in range(len(parts)):
            info = os.lstat(PACKAGE + ("/" + "/".join(parts[:n]) if n else ""))
            require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
                and (info.st_uid, info.st_gid) == (1000, 1000), "private source directory")
    return _observer_receipts.full_source_inventory_generation(
        _observer_receipts.Path(PACKAGE),expected,("controller",ASSIGNMENT,1))

def named_body(expected):
    rows = expected["namespace_identities"]
    require(type(rows) is list and len(rows) == len(ANCESTRY), "exact ancestry count")
    for row, (path, uid, gid, mode) in zip(rows, ANCESTRY):
        info = os.lstat(path)
        require(type(row) is dict and set(row) == {"path", "identity"} and row["path"] == path
            and type(row["identity"]) is list and len(row["identity"]) == 9
            and all(type(value) is int and value >= 0 for value in row["identity"])
            and identity(info) == row["identity"] and stat.S_ISDIR(info.st_mode)
            and stat.S_IMODE(info.st_mode) == mode and (info.st_uid, info.st_gid) == (uid, gid), "ancestry")
    issuer_identity = expected["admission_identity"]
    require(type(issuer_identity) is list and len(issuer_identity) == 9
        and all(type(value) is int and value >= 0 for value in issuer_identity), "exact issuer nine integer fields")
    pin = expected["admission_sha256"]
    require(type(pin) is str and len(pin) == 64 and all(c in "0123456789abcdef" for c in pin), "issuer pin")
    raw, info = read_file(ISSUER_ROOT + "/" + pin + ".json", 65536, 0o400)
    require(info == expected["admission_identity"] and digest(raw) == pin, "Root issuer identity/body")
    require(all(identity(os.lstat(row["path"])) == row["identity"] for row in rows), "stable ancestry")
    return raw

def emit(value):
    global output_bytes
    raw = encode(value)
    require(output_bytes + len(raw) <= OUTPUT_MAX, "output bound")
    output_bytes += len(raw)
    # Preserve the exact original line/body and output cap, but each ACTUAL
    # shared-pipe syscall is atomic-sized. No receipt order substitutes for it.
    pipe_buf=os.fpathconf(1,"PC_PIPE_BUF")
    for at in range(0,len(raw),pipe_buf):
        part=raw[at:at+pipe_buf]
        require(os.write(1,part)==len(part),"complete output")

def control(deadline, phase):
    raw = b""
    with selectors.DefaultSelector() as selector:
        selector.register(0, selectors.EVENT_READ)
        while not raw.endswith(b"\n"):
            left = min(deadline - now(), deadline - START_WALL - (time.monotonic() - START_MONO))
            require(left > 0 and selector.select(left), "fixed control deadline")
            cap=min(8192,131073-len(raw))
            part = _METER.read("controller.control",_RAW_OS.read,(0,cap),0)
            require(part and len(raw) + len(part) <= 131072, "bounded control or EOF")
            raw += part
    value = object_wire(raw)
    require(value["assignment"] == ASSIGNMENT and type(value["generation"]) is int
        and value["generation"] == 1 and value["root_thread"] == ROOT_THREAD
        and value["phase"] == phase, "native relay binding; external origin remains required")
    return value

def sealed(raw, maximum, target):
    require(0 < len(raw) <= maximum, "transport size")
    fd = os.memfd_create("a075-" + str(target), os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        require(os.write(fd, raw) == len(raw), "complete transport write")
        os.fchmod(fd, 0o400)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, SEALS)
        readonly = os.open("/proc/self/fd/" + str(fd), os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.dup2(readonly, target, inheritable=False)
        finally:
            os.close(readonly)
    finally:
        os.close(fd)
    held.append(target)

def held_state(fd, raw):
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o400
        and (info.st_uid, info.st_gid, info.st_nlink) == (1000, 1000, 0)
        and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        and fcntl.fcntl(fd, fcntl.F_GET_SEALS) & SEALS == SEALS
        and _METER.read("controller.held_state",_RAW_OS.pread,(fd,len(raw)+1,0),fd,0)==raw,
        "immutable read-only held object")
    return identity(info)

def main():
    require(type(ACCEPTED) is int and type(ROOT_THREAD) is str
        and ROOT_THREAD != "UNISSUED", "UNISSUED parent Source template")
    require((os.geteuid(), os.getegid()) == (1000, 1000), "ordinary owner")
    require(resource.getrlimit(resource.RLIMIT_AS) == (67108864, 1610612736)
        and resource.getrlimit(resource.RLIMIT_CPU) == (300, 7200)
        and resource.getrlimit(resource.RLIMIT_NOFILE) == (256, 256), "trusted pre-interpreter limits")
    started = now()
    init = control(min(started + 30, ACCEPTED + 6600), "INIT")
    require(set(init) == {"assignment", "generation", "root_thread", "phase", "controller_sha256",
        "init_deadline_epoch", "expectation"}, "exact INIT relay")
    require(digest(read_file(__file__, 65536)[0]) == init["controller_sha256"], "reviewed controller bytes")
    expected = init["expectation"]
    require(set(expected) == {"schema", "issuer", "source_assignment", "admission_sha256",
        "admission_identity", "namespace_identities", "run_context", "source_hashes"}
        and expected["schema"] == "friday.a056.independent-parent-custody.v1"
        and expected["issuer"] == ISSUER and expected["source_assignment"] == SOURCE, "exact Root expectation")
    index_raw = read_file(INDEX, 65536)[0]
    require(digest(index_raw) == INDEX_SHA and expected["source_hashes"] == json.loads(index_raw)["mandatory_source_hashes"], "frozen54")
    context = expected["run_context"]
    require(type(context) is dict and set(context) == {"schema", "assignment", "generation", "run_id",
        "accepted_at_utc", "deadline_at_utc", "wall_seconds", "seal_reserve_seconds", "resources", "allowed_modes"}, "exact context keys")
    accepted = datetime.fromisoformat(context["accepted_at_utc"])
    deadline = datetime.fromisoformat(context["deadline_at_utc"])
    require(accepted.tzinfo is not None and deadline.tzinfo is not None
        and accepted.astimezone(timezone.utc).isoformat() == context["accepted_at_utc"]
        and deadline.astimezone(timezone.utc).isoformat() == context["deadline_at_utc"], "canonical UTC clock")
    end, begin = deadline.timestamp(), accepted.timestamp()
    require(context["schema"] == "friday.a049.offline-run-context.v1"
        and type(context["run_id"]) is str and len(context["run_id"]) == 64
        and all(c in "0123456789abcdef" for c in context["run_id"])
        and context["assignment"] == ASSIGNMENT + "#1" and type(context["generation"]) is int and context["generation"] == 1
        and context["allowed_modes"] == ["affected", "collect", "selfcheck"]
        and context["resources"] == RESOURCES and context["seal_reserve_seconds"] == 600
        and type(context["wall_seconds"]) is int and 600 < context["wall_seconds"] <= 7200
        and end - begin == context["wall_seconds"] and begin <= started < end - 600
        and end <= ACCEPTED + 7200, "fixed finite Root RUN; no refresh")
    until = init["init_deadline_epoch"]
    require(type(until) in (int, float) and started < until <= min(started + 300, end - 600, ACCEPTED + 6600), "INIT deadline")
    inventory(expected["source_hashes"])
    body = named_body(expected)
    require(object_wire(body) == {"schema": "friday.a049.offline-run-admission.v1", "issuer": ISSUER,
        "source_assignment": SOURCE, "source_hashes": expected["source_hashes"], "run_context": context}, "Root body agreement")
    expectation = encode(expected)
    sealed(expectation, 131072, 198)
    sealed(body, 65536, 199)
    ids = [held_state(198, expectation), held_state(199, body)]
    _proc_fd=os.open("/proc/self/stat",os.O_RDONLY|os.O_CLOEXEC)
    _SOURCE_PARTIAL.append([_proc_fd,False,None])
    try:
        _proc_raw=_METER.read("controller.proc_stat",_RAW_OS.read,(_proc_fd,4096),_proc_fd)
        start_ticks=int(_proc_raw.decode("ascii").rsplit(")",1)[1].split()[19])
    finally:
        _SOURCE_PARTIAL[-1][1]=True
        try:os.close(_proc_fd)
        except BaseException as error:
            _SOURCE_PARTIAL[-1][2]=error
            raise
    emit({"status": "CUSTODY_READY", "assignment": ASSIGNMENT, "generation": 1, "holder_pid": os.getpid(),
        "holder_start_ticks": start_ticks, "fds": [198, 199], "identities": ids,
        "expectation_sha256": digest(expectation), "admission_sha256": digest(body),
        "admission_identity": expected["admission_identity"], "namespace_identities": expected["namespace_identities"],
        "controller_sha256": init["controller_sha256"], "source54": expected["source_hashes"], "init_deadline_epoch": until})
    auth = control(until, "AUTH")
    require(set(auth) == {"assignment", "generation", "root_thread", "phase", "task", "expected_task_sha256"}, "exact AUTH relay")
    task, pin = auth["task"], auth["expected_task_sha256"]
    task_raw = encode(task)
    require(type(pin) is str and len(pin) == 64 and digest(task_raw) == pin
        and task["source_hashes"] == expected["source_hashes"] and task["run_context"] == context
        and task["package_root"] == PACKAGE and task["expectation_sha256"] == digest(expectation)
        and task["expectation_identity"] == ids[0] and task["held_admission_identity"] == ids[1], "Root-native TASK pin/binding")
    steps = task["entrypoints"]
    require(len(steps) == 1 and steps[0]["entrypoint"] == "dispatcher"
        and len(steps[0]["arguments"]) == 3 and steps[0]["arguments"][0] == "--label"
        and steps[0]["arguments"][2] == "--focused-checks", "only collect/selfcheck/exact56 entrypoint")
    require(now() < min(until, end - 600, ACCEPTED + 6600), "AUTH current fixed time")
    inventory(expected["source_hashes"])
    require(named_body(expected) == body and [held_state(198, expectation), held_state(199, body)] == ids, "no rebinding")
    spec = importlib.util.spec_from_file_location("a074_reviewed_parent", PACKAGE + "/tests/admission_custodian.py")
    receiver = importlib.util.module_from_spec(spec)
    receiver.__dict__["_BOUND_RECEIPTS"]=_observer_receipts
    _receiver_raw=read_file(PACKAGE+"/tests/admission_custodian.py",READ_MAX)[0]
    exec(compile(_receiver_raw,spec.origin,"exec",dont_inherit=True),receiver.__dict__)
    receiver.R.bind_root_observer(_A132_CHANNEL)
    receiver.R.bind_source_read_meter(_METER)
    completed = receiver.R.observe_root_call("receiver.receive_and_launch",
        receiver.receive_and_launch,task_raw,pin,198,199)
    emit({"status": "RUN_RETURNED", "completed": completed, "controller_read_bytes": _METER.used,
        "raw_usage": list(resource.getrusage(resource.RUSAGE_SELF)), "release_credit": False})

_observer_spec=importlib.util.spec_from_file_location("a132_controller_observer",
    RECEIPT)
_observer_receipts=importlib.util.module_from_spec(_observer_spec)
import sys
sys.modules[_observer_spec.name]=_observer_receipts
_observer_raw=read_file(RECEIPT,READ_MAX)[0]
exec(compile(_observer_raw,_observer_spec.origin,"exec",dont_inherit=True),_observer_receipts.__dict__)
_observer_receipts.bind_source_read_meter(_METER)
_A132_CHANNEL=_observer_receipts.install_root_observer("controller",
    (ACCEPTED + 7200,START_MONO + ACCEPTED + 7200 - START_WALL))
_METER.bind(_A132_CHANNEL)
_BOOTSTRAP.bind(_A132_CHANNEL)
_observer_receipts.bind_source_read_meter(_METER)
_observer_receipts.instrument_root_module(globals(),"controller")
exit_code = 0
try:
    _observer_receipts.observe_root_call("controller.main",main)
except Exception as error:
    _BOOTSTRAP.first_error=error
    exit_code = 125
    emit({"status": "REFUSED_OR_FAILED", "error_type": type(error).__name__, "reason": str(error)[:512],
        "controller_read_bytes": _METER.used, "release_credit": False})
finally:
    for fd in held:
        try:
            _BOOTSTRAP.call("close",_RAW_OS.close,fd)
        except BaseException as close_error:
            _BOOTSTRAP.cleanup_pending=close_error
            _BOOTSTRAP.cleanup_errors.append(close_error)
_observer_receipts.observer_terminal(exit_code)
raise SystemExit(exit_code)
