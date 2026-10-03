_P=[]
def _i(n):
 _P.append(n);return __import__(n)
errno,fcntl,os,signal,sys,time=map(_i,("errno","fcntl","os","signal","sys","time"))
_RO, _RT = os, time
_BR = 0
_BT = []
def quoted(value):
 parts = []
 for char in value:
  number = ord(char)
  if number > 65535:
   number -= 65536
   parts.append('\\u%04x\\u%04x' % (55296 + (number >> 10),56320 + (number & 1023)))
  elif number < 32 or number > 126:
   parts.append('\\u%04x' % number)
  else:
   parts.append('\\' + char if char in ('"','\\') else char)
 return '"' + ''.join(parts) + '"'
class AcquisitionOwner:
 def __init__(self):
  self.slots=[];self.pending=None;self.first_error=None;self.cleanup_errors=[]
 def reserve(self,purpose,args):
  slot={"purpose":purpose,"arguments":args,"raw":None,"error":None,
    "state":"OWNED_PARTIAL","close_attempted":False,"close_error":None}
  self.pending=slot
  self.slots.append(slot)
  self.pending=None
  return slot
 def acquire(self,purpose,function,*args,**kwargs):
  slot=self.reserve(purpose,[args,kwargs])
  try:
   slot["raw"]=function(*args,**kwargs)
  except BaseException as error:
   slot["error"]=error
   if self.first_error is None:self.first_error=error
   slot["state"]="FAILED"
   raise
  slot["state"]="OWNED"
  return slot["raw"]
 def fail(self,error):
  if self.first_error is None:self.first_error=error
 def close(self,fd,function):
  active=sys.exc_info()[1]
  if active is not None:self.fail(active)
  owned=next((s for s in reversed(self.slots) if s["raw"]==fd),None)
  if owned is None:
   owned=self.reserve("existing_fd",[]);owned["raw"]=fd;owned["state"]="OWNED"
  if owned["close_attempted"]:
   if owned["state"]!="CLOSED":raise RuntimeError("ambiguous_close_no_retry")
   return
  owned["close_attempted"]=True
  owned["state"]="CLOSE_ATTEMPTED"
  try:function(fd)
  except BaseException as error:
   owned["close_error"]=error;owned["state"]="AMBIGUOUS_CLOSE"
   try:self.cleanup_errors.append(error)
   except BaseException as recorder:owned["recording_error"]=recorder
   if active is not None:return
   raise
  owned["state"]="CLOSED"
class RecorderSemantics:
 @staticmethod
 def check(state):
  if state['failed']:raise RuntimeError('explicit_operation_recording_failed')
  producer=state['producer']
  if producer is not None and producer['recording_failure'] is not None:
   raise RuntimeError('a132_lossless_recording_failed')
 @staticmethod
 def wire(state):
  if state['producer'] is None:
   return {'schema':'friday.a132.bootstrap-reached-full-rows.v1',
    'events':list(state['pending']),'calls':state['calls'],'complete':not state['failed'],
    'recording_failure':state['recording_failure'],'Root_owned_sideband_attached':False}
  return {'schema':'friday.a132.Root-held-full-events-ref.v1',
   'producer':state['producer'],'calls':state['calls'],'complete':not state['failed'],
   'recording_failure':state['recording_failure'],'Source_issued_Root_fact':False}
class FullEventJournal:
 def __init__(self):
  self.pending = []
  self.calls = 0
  self.failed = False
  self.recording_failure = None
  self.guard_scope = []
  self.guard_scope_snapshot=()
  self.client = None
  self.raw_events = []
  self.pending_raw = None
  self.effect_owners = []
  self.effect_pending = None
  self.recording_errors = []
  self.recording_pending = None
  self.predicate_pending={"ok":None,"cause":None,"first_error":None,
   "site":None,"locals":None,"recording_error":None}
  self.emergency_raw=[None]*6;self.first_failed_raw=[None]*6;self.emergency_damage=[None,None]
  self.last_recording_allocation_error=None
  self.method_serial=0;self.method_stack=[];self.state_ids={};self.state_owners=[]
  self.state_pending=None;self.method_pending=None
  self.state_byte_pins=set()
  self.recorder_owners=[];self.recorder_pending=None
 def state(self,item):
  nodes=[];seen={};opaque=[]
  def walk(value):
   if value is None or type(value) is bool:return value
   if isinstance(value,os.stat_result):return {"kind":"identity9","value":_identity(value)}
   key=id(value)
   if key in seen:return {"ref":seen[key]}
   if key not in self.state_ids:
    self.state_pending=value;self.state_owners.append(value)
    self.state_ids[key]=_codec.OPERANDS.number(value) if _codec is not None else len(self.state_owners)-1
   number=self.state_ids[key];seen[key]=number
   node={"id":number,"module":type(value).__module__,"class":type(value).__qualname__,"kind":None,"state":None}
   nodes.append(node)
   if type(value) in (int,str,float,bytes):
    node["kind"]=type(value).__name__
    if type(value) is bytes:
     pin=hashlib.sha256(value).hexdigest()
     node["state"]={"sha256":pin,"bytes":len(value)} if pin in self.state_byte_pins else {"hex":value.hex()}
    elif type(value) is float:node["state"]=value.hex()
    elif type(value) is int:node["state"]=str(value)
    else:node["state"]=value
   elif type(value) in (list,tuple,set,frozenset,bytearray):
    node["kind"]=type(value).__name__
    node["state"]=value.hex() if type(value) is bytearray else [walk(v) for v in value]
   elif type(value) is dict:
    node["kind"]="dict";node["state"]=[[walk(k),walk(v)] for k,v in value.items()]
   elif type(value).__name__ in ("Clock","Named","Directory","Generation","OwnScope","Held","IO","Session","AcquisitionOwner","Budget","ReadJournal","_FailureOwner"):
    node["kind"]="object";node["state"]={k:walk(v) for k,v in vars(value).items()}
   elif isinstance(value,BaseException):
    node["kind"]="error";node["state"]={"type":type(value).__name__,"errno":getattr(value,"errno",None),"text":str(value),"args":walk(value.args),"filename":walk(getattr(value,"filename",None)),"filename2":walk(getattr(value,"filename2",None))}
   elif value is self:
    node['kind']='facade';node['state']={'name':'FullEventJournal'}
   elif type(value) is type and value.__name__=='Refusal':
    node['kind']='facade';node['state']={'name':'Refusal'}
   elif type(value).__name__=="_ObservedSelector":
    node["kind"]="selector_facade"
    node["state"]={k:walk(v) for k,v in vars(value).items()}
   elif _codec is not None and _codec.OPERANDS.is_stock(value):
    node["kind"]="stock_operand";node["state"]=_codec.OPERANDS.capture(value)
   elif type(value).__name__=="_ObservedModule" or type(value) is type(os):
    node["kind"]="facade";node["state"]={"name":getattr(value,"__name__",type(value).__name__)}
   else:node["kind"]="opaque_owned";opaque.append(number)
   return {"ref":number}
  root=walk(item)
  return {"schema":"friday.sol089.method-state-graph.v2","root":root,"nodes":nodes,
   "opaque_owned_ids":opaque,"complete_replayable_state":not opaque}
 def method_enter(self,name,args,kwargs,cleanup=False):
  row={"call":self.method_serial,"parent":self.method_stack[-1] if self.method_stack else None,
   "name":name,"args":args,"kwargs":kwargs,"raw_self":args[0] if args else None,
   "raw_result":None,"raw_error":None,"before":None,"after":None,"recording_error":None,"cleanup":cleanup}
  self.method_pending=row;self.method_serial+=1
  self.method_stack.append(row["call"])
  try:
   row["before"]=self.state([args,kwargs])
   self.note("sender","method.enter",[name,row["call"],row["parent"]],row["before"])
   if not cleanup:self.check()
  except BaseException as error:
   self.method_end(row,error=error)
   if not cleanup:raise
  return row
 def method_end(self,row,result=None,error=None):
  row["ended"]=True
  row["raw_result"],row["raw_error"]=result,error
  try:
   row["after"]=self.state([row["args"],row["kwargs"],result])
   if error is not None:self.note("sender","method.after",[row["name"],row["call"],row["parent"]],row["after"])
   self.note("sender","method.error" if error is not None else "method.return",
    [row["name"],row["call"],row["parent"]],row["after"],error)
  except BaseException as recorder:
   row["recording_error"]=recorder
   if error is None and not row["cleanup"]:raise
  finally:
   if self.method_stack and self.method_stack[-1]==row["call"]:self.method_stack.pop()
 def predicate_state(self,frame):
  if "self" not in frame.f_locals:return
  raw=frame.f_locals["self"]
  self.state_pending=raw
  graph=self.state(raw)
  self.note("sender","method.predicate.state",
   [[frame.f_code.co_qualname,frame.f_lineno],self.method_stack[-1] if self.method_stack else None],graph)
 def value(self,value):
  if value is None or type(value) in (bool,int,str):
   return value
  if type(value) is float:
   return ["float_hex",value.hex()]
  if type(value) is bytes:
   return ["bytes_full",len(value),value.hex()]
  if isinstance(value,os.stat_result):
   return ["identity9",*_identity(value)]
  if isinstance(value,(tuple,list)):
   return [self.value(item) for item in value]
  if isinstance(value,(set,frozenset)):
   return ["set",[self.value(item) for item in sorted(value)]]
  if isinstance(value,dict):
   return [[self.value(key),self.value(item)] for key,item in value.items()]
  if isinstance(value,int):
   return ["integer_subtype",type(value).__name__,int(value)]
  if hasattr(value,"fd") and hasattr(value,"events"):
   return ["selector_key",value.fd,value.events,self.value(value.data)]
  raise TypeError("unrepresented_explicit_operation_result:" + type(value).__name__)
 def note(self,owner,operation,arguments,result=None,error=None):
  raw=self.emergency_raw
  raw[0]=owner;raw[1]=operation;raw[2]=arguments;raw[3]=result;raw[4]=error;raw[5]=self.guard_scope_snapshot
  self.pending_raw = raw
  try:
   raw = [owner,operation,arguments,result,error,tuple(self.guard_scope)]
   self.pending_raw=raw
   self.raw_events.append(raw)
   self.calls += 1
   scope = list(self.guard_scope)
   if self.client is None:
    self.pending.append([owner,operation,self.value(arguments),
     ["returned",self.value(result)] if error is None else
     ["error",type(error).__name__,getattr(error,"errno",None),type(error).__module__,self.value(error.args),str(error),self.value(getattr(error,"filename",None)),self.value(getattr(error,"filename2",None)),self.value(_xlinks(error))],scope])
   else:
    self.client.note(operation,[owner,arguments,scope],result,error)
    self.client.check()
  except BaseException as failure:
   self.failed = True
   if self.recording_failure is None:
    first=self.first_failed_raw
    first[0]=raw[0];first[1]=raw[1];first[2]=raw[2];first[3]=raw[3];first[4]=raw[4];first[5]=raw[5]
    self.emergency_damage[0]=first;self.emergency_damage[1]=failure
    self.recording_failure = self.emergency_damage
   self.recording_pending=self.emergency_damage
   try:
    self.recording_pending=[raw,failure]
    self.recording_errors.append(self.recording_pending)
   except BaseException as second:
    self.last_recording_allocation_error=second
 def attach(self,client):
  self.client = client
  for row in self.pending:
   client.note("bootstrap.reached_row",[],row)
  self.pending.clear()
  client.check()
 def recorder_state(self,operation):
  slot={'operation':operation,'raw_journal':self,'raw_client':self.client,
   'state':None,'raw_error':None,'traceback':None}
  self.recorder_pending=slot;self.recorder_owners.append(slot)
  try:
   damage=None if self.recording_failure is None else _damage(self.recording_failure[0],self.recording_failure[1],self.last_recording_allocation_error)
   slot['state']={'schema':'friday.sol061.recorder-state.v1','failed':self.failed,
    'calls':self.calls,'pending':list(self.pending) if self.client is None else [],
    'recording_failure':damage,'producer':None if self.client is None else self.client.reference()}
   self.note('sender','recorder.'+operation+'.state',[],slot['state'])
   return slot['state']
  except BaseException as error:
   slot['raw_error']=error;slot['traceback']=error.__traceback__
   raise
 def check(self):
  state=self.recorder_state('check')
  if self.failed:raise RuntimeError('explicit_operation_recording_failed')
  RecorderSemantics.check(state)
  if self.client is not None:self.client.check()
 def call(self,owner,operation,function,*args,**kwargs):
  cleanup = operation == "close" or operation.endswith(".close")
  if not cleanup:
   self.check()
  slot = [owner,operation,args,kwargs,None,None,None]
  self.effect_pending = slot
  self.effect_owners.append(slot)
  original_arguments=[args,kwargs]
  arguments=(_codec.FrozenArguments(original_arguments) if operation.startswith("selector.") else original_arguments)
  raw_operand=(args[0] if operation=="selector.create" else getattr(function,"__self__",None)) if operation.startswith("selector.") else None
  self.note(owner,operation + ".begin",arguments)
  if not cleanup:
   self.check()
  try:
   result = function(*args,**kwargs)
  except BaseException as error:
   slot[5] = error
   self.note(owner,operation,arguments,error=error)
   if raw_operand is not None:self.note(owner,"selector.body.after",[operation,self.client.sequence-1],raw_operand)
   raise
  slot[4] = result
  self.note(owner,operation,arguments,result)
  if raw_operand is not None:self.note(owner,"selector.body.after",[operation,self.client.sequence-1],raw_operand)
  return result
 def wire(self):
  state=self.recorder_state('wire')
  return RecorderSemantics.wire(state)
 @staticmethod
 def check_ref(wire):
  if (type(wire) is not dict or wire.get("schema") not in
    ("friday.a132.bootstrap-reached-full-rows.v1","friday.a132.Root-held-full-events-ref.v1")
    or wire.get("complete") is not True or wire.get("recording_failure") is not None
    or type(wire.get("calls")) is not int or wire["calls"] < 0):
   raise ValueError("whole_Root_sideband_reference_not_complete")
 @staticmethod
 def replay(wire):
  FullEventJournal.check_ref(wire)
  if wire["schema"] == "friday.a132.bootstrap-reached-full-rows.v1":
   for row in wire["events"]:
    yield row[:4]
   return
  raise ValueError("independently_Root_held_sideband_reader_required_outside_Source")
class Budget:
 def __init__(self,bootstrap_read_bytes,prior_trace,api):
  self.api=api
  self.observed = bootstrap_read_bytes
  self.reserved = 131072 + 2 * 65537 + 64185 + 8192
  self.trace = ReadJournal(prior_trace)
  self.omitted = 0
  self.denied = None
  self.later_denials = []
  self.consumer = "caller_bootstrap"
  self.raw_read=None;self.read_owners=[]
 def _row(self,kind,requested_cap,returned_bytes,zero_return,would_block,charged_observed,error=None,fd=None,offset=None):
  row = {"consumer": kind,"kind": kind,"requested_cap": requested_cap,
   "returned_bytes": returned_bytes,"returned_bytes_per_call": returned_bytes,
   "repeat": 1,"zero_return": zero_return,"would_block": would_block,
   "charged_observed": charged_observed,
   "outcome": "error" if error else ("would_block" if would_block else "returned"),
      "error_type": type(error).__name__ if error else None,
      "error_errno": getattr(error,"errno",None),"fd": fd,"offset": offset}
  self.trace.append(row)
  self.api['TRACE'].note("sender","guarded_read",[kind,requested_cap,fd,offset],row)
 def _refuse(self,kind,requested_cap):
  denial = {"consumer": kind,"kind": kind,"requested_cap": requested_cap,
    "prefix_events": self.trace.calls,"prefix_returned_bytes": self.observed,
    "prefix_reserved_bytes": self.reserved,"asserted_cap_plus_one": False,
    "charged": False}
  if self.denied is None:
   self.denied = denial
  else:
   self.later_denials.append(denial)
  self.trace.append(dict(denial,outcome="denied",repeat=1,returned_bytes=0,
     returned_bytes_per_call=0,zero_return=False,would_block=False,
     charged_observed=False))
  self.api['TRACE'].note("sender","read.denied",[],denial)
  raise self.api["Refusal"]("read_bound")
 def strings(self,values,maximum,*,kind):
  self.consumer = kind
  if type(values) is not list or any(type(value) is not str for value in values):
   self._refuse(kind,None)
  amount = sum(len(value.encode("utf-8")) + 1 for value in values)
  if amount > maximum or self.observed + amount > 33554432:
   self._refuse(kind,amount)
  self.observed += amount
  self.reserved += maximum
  self._row(kind,amount,amount,amount == 0,False,True)
  return values
 def read(self,fd,cap,offset=None,*,kind):
  self.consumer = kind
  if not (type(cap) is int and cap>=1) or self.observed + cap > 33554432:
   self._refuse(kind,cap if (type(cap) is int and cap>=1) else None)
  self.reserved += cap
  slot=[fd,cap,offset,None,None];self.raw_read=slot;self.read_owners.append(slot)
  try:
   raw = self.api['os'].read(fd,cap) if offset is None else self.api['os'].pread(fd,cap,offset)
  except BlockingIOError as error:
   slot[4]=error
   self._row(kind,cap,0,False,True,False,fd=fd,offset=offset)
   raise
  except OSError as error:
   slot[4]=error
   self._row(kind,cap,0,False,False,False,error=error,fd=fd,offset=offset)
   raise
  slot[3]=raw
  self.observed += len(raw)
  self.api['TRACE'].note("sender","read.result",[fd,cap,offset],raw)
  self._row(kind,cap,len(raw),len(raw) == 0,False,True,fd=fd,offset=offset)
  return raw
class _ObservedSelector:
 def __init__(self,module,owner):
  self.owner = owner
  self.raw = None
  self.close_attempted = False
  self.close_error = None
  _codec.OPERANDS.configure(module)
  descriptor=_codec.OPERANDS.catalog.descriptor(module.DefaultSelector)
  try:self.raw=module.DefaultSelector.__new__(module.DefaultSelector)
  except BaseException as error:
   _E.note(owner,"selector.allocate",[[descriptor],{}],error=error)
   raise
  _codec.OPERANDS.birth(self.raw,"selector.allocate",owner)
  _E.note(owner,"selector.allocate",[[descriptor],{}],self.raw)
  _E.pending_selector_owner = self
  try:
   _E.call(owner,"selector.create",module.DefaultSelector.__init__,self.raw)
  except BaseException as error:
   self.create_error = error
   raise
 def register(self,*args,**kwargs):
  return _E.call(self.owner,"selector.register",self.raw.register,*args,**kwargs)
 def select(self,*args,**kwargs):
  return _E.call(self.owner,"selector.select",self.raw.select,*args,**kwargs)
 def __enter__(self):
  return self
 def __exit__(self,*args):
  if self.close_attempted:
   if self.close_error is not None:raise RuntimeError("selector ambiguous close: no retry")
   return
  self.close_attempted = True
  try:_E.call(self.owner,"selector.close",self.raw.close)
  except BaseException as error:
   self.close_error = error
   if args[1] is None:raise
class _ObservedModule:
 OPERATIONS = {"lstat","fstat","stat","readlink","listdir","open","close",
     "read","pread","write","pwrite","dup2",
     "pidfd_open","set_blocking","getpid",
     "geteuid","getegid","wait4","fcntl","time","monotonic",
     "getrlimit","setrlimit","getrusage","pthread_sigmask","signal","getsignal","waitstatus_to_exitcode"}
 def __init__(self,module,owner):
  self.raw,self.owner = module,owner
 def __getattr__(self,name):
  if name == "DefaultSelector":
   return lambda: _ObservedSelector(self.raw,self.owner)
  value = getattr(self.raw,name)
  if self.owner=="sender.stockinternal" and name in ("read","pread"):
   def internal_read(*args,**kwargs):
    budget=getattr(getattr(_FO,"session",None),"budget",None)
    if budget is None:raise RuntimeError("stock subprocess read has no original Sender meter")
    return budget.read(args[0],args[1],args[2] if name=="pread" else None,
      kind="stock-subprocess:"+name)
   return internal_read
  if name in self.OPERATIONS:
   return lambda *args,**kwargs: _E.call(self.owner,name,value,*args,**kwargs)
  return value
class ReadJournal:
 ""
 def __init__(self,prior=()):
  self.table,self.runs,self.indices = [],[],{}
  self.calls = self.returned = 0
  self.failed = False
  for row in prior:
   self.append(row)
 def __len__(self):
  return len(self.runs)
 def append(self,row):
  try:
   self._append(row)
  except BaseException:
   self.failed = True
   raise
 def _append(self,row):
  count = row["repeat"]
  if type(count) is not int or count < 1:
   raise ValueError("read_trace_repeat")
  item = {key: value for key,value in row.items() if key not in ("repeat","returned_bytes")}
  returned = item["returned_bytes_per_call"]
  if (type(returned) is not int or returned < 0 or row["returned_bytes"] != count * returned
    or type(item["charged_observed"]) is not bool):
   raise ValueError("read_trace_total")
  key = repr(sorted(item.items()))
  index = self.indices.get(key)
  if index is None:
   index = len(self.table)
   self.indices[key] = index
   self.table.append(item)
  if self.runs and self.runs[-1][0] == index:
   self.runs[-1][1] += count
  else:
   self.runs.append([index,count])
  self.calls += count
  self.returned += count * returned if item["charged_observed"] else 0
 def wire(self):
  return {"schema": "friday.a127.guarded-read-table-rle.v2","table": list(self.table),
    "complete": not self.failed,
    "runs": [list(row) for row in self.runs],"calls": self.calls,
    "charged_returned_bytes": self.returned,"omitted_events": None if self.failed else 0}
 @staticmethod
 def replay(wire):
  if (type(wire) is not dict or wire.get("schema") != "friday.a127.guarded-read-table-rle.v2"
    or wire.get("complete") is not True
    or wire.get("omitted_events") != 0 or type(wire.get("table")) is not list
    or type(wire.get("runs")) is not list):
   raise ValueError("read_trace_wire")
  calls = returned = 0
  for run in wire["runs"]:
   if (type(run) is not list or len(run) != 2 or any(type(v) is not int for v in run)
     or not 0 <= run[0] < len(wire["table"]) or run[1] < 1):
    raise ValueError("read_trace_run")
   item = wire["table"][run[0]]
   if (type(item) is not dict or type(item.get("returned_bytes_per_call")) is not int
     or item["returned_bytes_per_call"] < 0 or type(item.get("charged_observed")) is not bool):
    raise ValueError("read_trace_row")
   calls += run[1]
   returned += run[1] * item["returned_bytes_per_call"] if item["charged_observed"] else 0
   yield dict(item,repeat=run[1],returned_bytes=run[1] * item["returned_bytes_per_call"])
  if (type(wire.get("calls")) is not int or wire["calls"] != calls
    or type(wire.get("charged_returned_bytes")) is not int
    or wire["charged_returned_bytes"] != returned):
   raise ValueError("read_trace_aggregate")
class OrdinaryNativeContract:
 SESSION_KEYS = set("status cause cleanup reported_controller_output_claim reported_entrypoint_returncodes controller_raw_output_hex_by_original_fd controller_pipe_observation controller_original_fd_map controlled_stdout_lines producer_stdio child_failure_stderr controller_overflow_probe_hex_by_original_fd sender_read_bytes_observed sender_read_requested_upper_bytes read_trace_sha256 read_trace_bytes read_trace_events read_trace_omitted read_trace_complete read_trace whole_performing_trace_complete read_denied later_read_denials root_custody_request_bytes_written root_custody_request_observations read_accounting_scope sender_rlimits_observed sender_raw_usage_observed Root_image_RAM_IO aggregate_RAM_IO origin execution_custody handoff native_exit_rule runtime_GO release_credit full_gate retry explicit_operation_trace explicit_operation_trace_scope".split())
 EARLY_KEYS = set("status cause spawned runtime_GO release_credit cleanup retry read_trace_sha256 read_trace_events read_trace read_trace_bytes read_trace_complete read_denied producer_stdio child_failure_stderr explicit_operation_trace whole_performing_trace_complete".split())
 @staticmethod
 def schema(outcome,early=False):
  expected = OrdinaryNativeContract.EARLY_KEYS if early else OrdinaryNativeContract.SESSION_KEYS
  if (type(outcome) is not dict or set(outcome) != expected
    or outcome["runtime_GO"] is not False or outcome["release_credit"] is not False
    or outcome["retry"] is not False or outcome["whole_performing_trace_complete"] is not False
    or type(outcome["read_trace_complete"]) is not bool
    or type(outcome["cause"]) not in (str,type(None))):
   raise ValueError("ordinary_exact_native_schema")
  for key in ("read_trace_events","read_trace_bytes"):
   if type(outcome[key]) is not int or outcome[key] < 0:
    raise ValueError("ordinary_exact_native_integer")
  if early:
   if outcome["spawned"] is not False or outcome["status"] != "REFUSED":
    raise ValueError("ordinary_early_native_schema")
   return
  if outcome["status"] not in ("RUN_REPORTED","REFUSED_OR_FAILED","STOP_UNCONFIRMED"):
   raise ValueError("ordinary_native_status_enum")
  for key in ("sender_read_bytes_observed","sender_read_requested_upper_bytes","read_trace_omitted","root_custody_request_bytes_written"):
   if type(outcome[key]) is not int or outcome[key] < 0:
    raise ValueError("ordinary_exact_native_integer")
  usage = outcome["sender_raw_usage_observed"]
  if (type(usage) is not list or len(usage) != 16
    or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in usage[:2])
    or any(type(v) is not int or v < 0 for v in usage[2:])):
   raise ValueError("ordinary_native_usage16")
  codes = outcome["reported_entrypoint_returncodes"]
  if codes is not None and (type(codes) is not list or any(type(v) is not int for v in codes)):
   raise ValueError("ordinary_native_codes")
  for pipe in outcome["controller_pipe_observation"].values():
   if (type(pipe) is not dict or set(pipe) != {"prefix_hex","prefix_bytes","eof","capture_stopped","actual_zero","silent_zero","capture_error","overflow_probe_hex"}
      or type(pipe["prefix_bytes"]) is not int or pipe["prefix_bytes"] < 0
      or any(type(pipe[k]) is not bool for k in ("eof","capture_stopped","actual_zero","silent_zero"))
      or any(type(pipe[k]) not in (str,type(None)) for k in ("prefix_hex","overflow_probe_hex"))
      or pipe["silent_zero"] is not False):
    raise ValueError("ordinary_exact_pipe_schema")
 @staticmethod
 def canonical(value):
  return (json.dumps(value,sort_keys=True,ensure_ascii=True,allow_nan=False,
        separators=(",",":")) + "\n").encode("ascii")
 @staticmethod
 def bootstrap_wire(outcome):
  if (type(outcome) is not dict or set(outcome) != {"cause","error_type","reason","release_credit",
    "runtime_GO","spawned","status","explicit_operation_trace","whole_performing_trace_complete"}
    or outcome["whole_performing_trace_complete"] is not False or outcome["cause"] != "caller_bootstrap"
    or type(outcome["error_type"]) is not str or len(outcome["error_type"]) > 64
    or type(outcome["reason"]) is not str or len(outcome["reason"]) > 256
    or outcome["release_credit"] is not False or outcome["runtime_GO"] is not False
    or outcome["spawned"] is not False or outcome["status"] != "REFUSED"):
   raise ValueError("ordinary_bootstrap_types")
  return ('{"cause":"caller_bootstrap","error_type":' + quoted(outcome["error_type"])
    + ',"explicit_operation_trace":' + self_bootstrap_json(outcome["explicit_operation_trace"])
    + ',"reason":' + quoted(outcome["reason"])
    + ',"release_credit":false,"runtime_GO":false,"spawned":false,"status":"REFUSED"'
    + ',"whole_performing_trace_complete":false}\n').encode('ascii')
 def outside_early_capture(self,predelivery,root_stdout,native_exit,bootstrap=False):
  if (type(predelivery) is not bytes or type(root_stdout) is not bytes or type(native_exit) is not int
    or native_exit != 125 or len(root_stdout) > 262144 or not predelivery.startswith(root_stdout)):
   raise ValueError("ordinary_early_actual_capture")
  outcome = json.loads(predelivery)
  if bootstrap:
   if self.bootstrap_wire(outcome) != predelivery:
    raise ValueError("ordinary_bootstrap_wire_recipe")
   for _row in FullEventJournal.replay(outcome["explicit_operation_trace"]):
    pass
  else:
   self.schema(outcome,early=True)
   if (self.canonical(outcome) != predelivery or outcome.get("spawned") is not False
      or outcome.get("status") != "REFUSED" or outcome.get("runtime_GO") is not False
      or outcome.get("release_credit") is not False):
    raise ValueError("ordinary_pre_session_wire")
   for _row in ReadJournal.replay(outcome["read_trace"]["retained"]):
    pass
   for _row in FullEventJournal.replay(outcome["explicit_operation_trace"]):
    pass
  return outcome
 def before_early_delivery(self,outcome,wire):
  self.schema(outcome,early=True)
  if (type(wire) is not bytes or len(wire) > 262144 or self.canonical(outcome) != wire
    or outcome["status"] != "REFUSED" or outcome["spawned"] is not False
    or "delivery" in outcome or outcome["runtime_GO"] is not False
    or outcome["release_credit"] is not False):
   raise ValueError("ordinary_early_predelivery_domain")
  for _row in ReadJournal.replay(outcome["read_trace"]["retained"]):
   pass
  FullEventJournal.check_ref(outcome["explicit_operation_trace"])
 def before_delivery(self,outcome,wire,prior_root_bytes):
  self.schema(outcome)
  if (type(wire) is not bytes or self.canonical(outcome) != wire
    or type(prior_root_bytes) is not int or prior_root_bytes < 0
    or prior_root_bytes + len(wire) > 262144
    or "delivery" in outcome or "native_final_line" in outcome):
   raise ValueError("ordinary_predelivery_domain")
  rows = outcome["read_trace"]["retained"]
  total = sum(row["returned_bytes"] for row in ReadJournal.replay(rows)
      if row["charged_observed"])
  if total != outcome["sender_read_bytes_observed"] or total > 33554432:
   raise ValueError("ordinary_read_resource_correspondence")
  FullEventJournal.check_ref(outcome["explicit_operation_trace"])
  if outcome.get("runtime_GO") is not False or outcome.get("release_credit") is not False:
   raise ValueError("ordinary_no_authority")
  written = 0
  for request in outcome["root_custody_request_observations"]:
   intended = bytes.fromhex(request["wire_hex"])
   prefix = request["successful_prefix_bytes"]
   if (type(prefix) is not int or not 0 <= prefix <= len(intended)
      or request["wire_bytes"] != len(intended)
      or hashlib.sha256(intended).hexdigest() != request["sha256"]
      or self.canonical(json.loads(intended)) != intended):
    raise ValueError("ordinary_root_request_prefix")
   written += prefix
  if written != prior_root_bytes or outcome["root_custody_request_bytes_written"] != written:
   raise ValueError("ordinary_root_request_total")
  for pipe in outcome["controller_pipe_observation"].values():
   if any(type(pipe[k]) is not bool for k in ("eof","capture_stopped","actual_zero")):
    raise ValueError("ordinary_pipe_types")
   actual_zero = pipe["eof"] and not pipe["capture_stopped"] and pipe["prefix_bytes"] == 0
   if pipe["actual_zero"] != actual_zero:
    raise ValueError("ordinary_eof_stopped_relation")
   raw = pipe["prefix_hex"]
   if raw is not None and len(bytes.fromhex(raw)) != pipe["prefix_bytes"]:
    raise ValueError("ordinary_pipe_prefix")
  if sum(row["prefix_bytes"] for row in outcome["controller_pipe_observation"].values()) > 32768:
   raise ValueError("ordinary_combined_pipe_bound")
 def after_delivery(self,predelivery,outcome,total_root_bytes):
  delivery = outcome["delivery"]
  count = delivery["bytes_written"]
  prior = outcome["root_custody_request_bytes_written"]
  if (type(count) is not int or not 0 <= count <= len(predelivery)
    or total_root_bytes != prior + count or total_root_bytes > 262144
    or delivery["complete_line"] != (count == len(predelivery))):
   raise ValueError("ordinary_delivery_prefix_relation")
  if delivery["confirmed"]:
   if (delivery["complete_line"] is not True
      or delivery["completed_wall"] >= delivery["original_wall_end"]
      or delivery["completed_monotonic"] >= delivery["original_monotonic_end"]):
    raise ValueError("ordinary_original_ends")
 def outside_capture(self,outcome,predelivery,root_stdout,native_exit):
  if type(root_stdout) is not bytes or type(native_exit) is not int:
   raise ValueError("ordinary_actual_root_capture_types")
  expected = b"".join(bytes.fromhex(row["wire_hex"])[:row["successful_prefix_bytes"]]
         for row in outcome["root_custody_request_observations"])
  expected += predelivery[:outcome["delivery"]["bytes_written"]]
  if root_stdout != expected or len(root_stdout) > 262144:
   raise ValueError("ordinary_root_observed_prefix_relation")
  success = (outcome["status"] == "RUN_REPORTED"
     and outcome["reported_entrypoint_returncodes"] == [0]
     and outcome["cleanup"].get("confirmed") is True
     and outcome["delivery"].get("confirmed") is True)
  if native_exit != (0 if success else 125):
   raise ValueError("ordinary_native_exit_relation")
  return {"runtime": "EXTERNALLY_SUPPLIED_OBSERVATIONS_ONLY","GO": False}
 @staticmethod
 def producer_streams(segments,controller_stdout,controller_stderr,worker_files):
     ""
     if (type(segments) is not list or type(controller_stdout) is not bytes
             or type(controller_stderr) is not bytes or type(worker_files) is not dict):
         raise ValueError("ordinary_producer_stream_types")
     combined = {1: bytearray(),2: bytearray()}
     ranks={1:0,2:0};origins=set();pipe_generations={}
     for segment in segments:
         if (type(segment) is not dict or set(segment) != {"sequence","producer","pid","fd","bytes",
                 "physical_sequence","pipe_identity9","pipe_buf","physical_rank","written_bytes","unread_suffix"}
                 or type(segment["sequence"]) is not int or segment["sequence"] in origins
                 or segment["producer"] not in ("controller","dispatcher")
                 or type(segment["fd"]) is not int or segment["fd"] not in (1,2)
                 or type(segment["bytes"]) is not bytes):
             raise ValueError("ordinary_producer_segment")
         origins.add(segment["sequence"])
         fd=segment["fd"]
         if (type(segment["physical_rank"]) is not int or segment["physical_rank"]!=ranks[fd]
                 or type(segment["pid"]) is not int or segment["pid"]<=0
                 or type(segment["physical_sequence"]) is not int or segment["physical_sequence"]<0
                 or type(segment["pipe_identity9"]) is not list or len(segment["pipe_identity9"])!=9
                 or type(segment["pipe_buf"]) is not int or not 0<len(segment["bytes"])<=segment["pipe_buf"]
                 or type(segment['written_bytes']) is not bytes or type(segment['unread_suffix']) is not bytes
                 or segment['bytes']+segment['unread_suffix']!=segment['written_bytes']
                 or len(segment['written_bytes'])>segment['pipe_buf']):
             raise ValueError("ordinary_actual_per_channel_physical_writer_proof")
         ranks[fd]+=1
         generation=segment["pipe_identity9"]
         if fd in pipe_generations and generation!=pipe_generations[fd]:
             raise ValueError("ordinary_writer_pipe_generation_changed")
         pipe_generations[fd]=generation
         combined[segment["fd"]].extend(segment["bytes"])
     if bytes(combined[1]) != controller_stdout or bytes(combined[2]) != controller_stderr:
         raise ValueError("ordinary_combined_dispatcher_controller_pipes")
     for path,wire in worker_files.items():
         if type(path) is not str or not path or type(wire) is not bytes:
             raise ValueError("ordinary_exclusive_worker_stream")
     return {"stdout": controller_stdout,"stderr": controller_stderr,
             "worker_files": dict(worker_files),"claimed_authority": False,
             "ordering_scope":"ACTUAL_PER_PIPE_BYTE_ORDER_NOT_GLOBAL_RECEIPT_ARRIVAL"}
 @staticmethod
 def matching_bytes(trace_wire,byte_producer):
  ""
  if not callable(byte_producer):
   raise ValueError("ordinary_matching_byte_producer")
  def match(value,event_index):
   if type(value) is list:
    if value and value[0] == "bytes_commitment":
      if (len(value) != 3 or type(value[1]) is not int or value[1] < 0
         or type(value[2]) is not str or len(value[2]) != 64):
       raise ValueError("ordinary_byte_commitment_type")
      supplied = byte_producer(event_index,value[1],value[2])
      if (type(supplied) is not bytes or len(supplied) != value[1]
         or hashlib.sha256(supplied).hexdigest() != value[2]):
       raise ValueError("ordinary_byte_producer_mismatch")
      return supplied
    return [match(item,event_index) for item in value]
   return value
  for index,event in enumerate(FullEventJournal.replay(trace_wire)):
   yield match(event,index)
class TypedOrdinaryContract:
 def __new__(cls,worker_reader=None):
  import native_grammar
  return native_grammar.TypedOrdinaryContract(worker_reader)
_E = None
_FO = None
_BW = _BM = None
def initialize_outside_consumer(worker_reader=None):
 ""
 global json,hashlib,math,base64
 import json,hashlib,math,base64
 return TypedOrdinaryContract(worker_reader)
class _FailureOwner:
 def __init__(self):
  self.domain = "caller_bootstrap"
  self.session = None
  self.emergency_handle = None
  self.cleanup = self.delivery = None
  self.bootstrap_written = 0
def _begin_performing():
 global _E,_FO,os,fcntl,signal,time,_BW,_BM
 _FO = _FailureOwner()
 _E = FullEventJournal()
 _E.note("caller","pre",_P)
 os = _ObservedModule(os,"caller")
 fcntl = _ObservedModule(fcntl,"caller")
 signal = _ObservedModule(signal,"caller")
 time = _ObservedModule(time,"caller")
 _BW,_BM = time.time(),time.monotonic()
 _E.note("caller","original.anchors",[],{"wall0":_BW,"mono0":_BM})
def self_bootstrap_json(value):
 ""
 if value is None:
  return "null"
 if type(value) is bool:
  return "true" if value else "false"
 if type(value) is int:
  return str(value)
 if type(value) is str:
  parts = []
  escapes = {"\b": "\\b","\t": "\\t","\n": "\\n","\f": "\\f","\r": "\\r"}
  for char in value:
   number = ord(char)
   if char in escapes:
    parts.append(escapes[char])
   elif number > 65535:
    number -= 65536
    parts.append('\\u%04x\\u%04x' % (55296 + (number >> 10),56320 + (number & 1023)))
   elif number < 32 or number > 126:
    parts.append('\\u%04x' % number)
   else:
    parts.append('\\' + char if char in ('"','\\') else char)
  return '"' + ''.join(parts) + '"'
 if type(value) is list:
  return "[" + ",".join(self_bootstrap_json(item) for item in value) + "]"
 if type(value) is dict and all(type(key) is str for key in value):
  return "{" + ",".join(self_bootstrap_json(key) + ":" + self_bootstrap_json(value[key])
     for key in sorted(value)) + "}"
 raise TypeError("bootstrap_control_type")
def _note(consumer,kind,requested_cap,returned_bytes,repeat=1,would_block=False,charged=True,error=None,raw=None):
 row = {"consumer": consumer,"kind": kind,"requested_cap": requested_cap,
   "returned_bytes": returned_bytes,"repeat": repeat,
   "returned_bytes_per_call": returned_bytes // repeat,
   "zero_return": charged and not would_block and returned_bytes == 0,
   "would_block": would_block,"charged_observed": charged,
   "outcome": "error" if error else ("would_block" if would_block else "returned"),
   "error_type": type(error).__name__ if error else None,
   "error_errno": getattr(error,"errno",None)}
 keys = set(row) - {"repeat","returned_bytes"}
 if _BT and all(_BT[-1].get(k) == row[k] for k in keys):
  _BT[-1]["repeat"] += repeat
  _BT[-1]["returned_bytes"] += returned_bytes
 else:
  _BT.append(row)
 _E.note("caller","guarded_read",[consumer,kind,requested_cap],
    [row,raw] if raw is not None else row)
def _live_fds():
 global _BR
 names = os.listdir("/proc/self/fd")
 charged = sum(len(name.encode("ascii")) + 1 for name in names)
 _BR += charged
 _note("caller.live_fds","proc_fd_names",charged,charged)
 if len(names) > 257 or any(not name.isascii() or not name.isdecimal() for name in names):
  raise ValueError("bounded_fd_map")
 rows = []
 for name in names:
  fd = int(name)
  try:
   rows.append([fd,fcntl.fcntl(fd,fcntl.F_GETFD),fcntl.fcntl(fd,fcntl.F_GETFL)])
  except OSError as error:
   if error.errno != errno.EBADF:
    raise
 return sorted(rows)
FFI_PATH = "/usr/lib/x86_64-linux-gnu/libffi.so.8.2.0"
FFI_SHA = "1a0dc86f787f73e025a6e521056360afcbe70f2a82cd808132fefc2b4ee95daa"
_LOADS=[];_LUP=0
def _install_loader():
 external=sys.modules['_frozen_importlib_external'];old=external.FileLoader.get_data
 def data(loader,path):
  global _BR,_LUP
  slot=[loader,path,None,None,None,None,None,None,None];_LOADS.append(slot)
  try:
   slot[2]=_RO.open(path,_RO.O_RDONLY|_RO.O_CLOEXEC)
   slot[3]=bytearray();offset=0
   while True:
    slot[6]=min(8192,33554432-_BR-_LUP);slot[8]=None
    if slot[6]<=0:raise ValueError('original_sender_loader_read_bound')
    slot[8]=_RO.pread(slot[2],slot[6],offset);_BR+=len(slot[8])
    _note('caller.loader:'+path,'physical_loader_pread',slot[6],len(slot[8]),raw=slot[8])
    if not slot[8]:return bytes(slot[3])
    slot[3].extend(slot[8]);offset+=len(slot[8])
  except BaseException as error:
   slot[4]=error
   if slot[6] is not None and slot[8] is None:_LUP+=max(0,slot[6])
   try:_note('caller.loader:'+path,'physical_loader_pread_error',slot[6] or 0,0,charged=False,error=error)
   except BaseException as recorder:slot[7]=recorder
   raise
  finally:
   if slot[2] is not None:
    try:_RO.close(slot[2])
    except BaseException as close:
     slot[5]=close
     if slot[4] is None:raise
 external.FileLoader.get_data=data
 original=external.ExtensionFileLoader.create_module
 def dynamic(loader,spec):
  slot=[loader,spec,None,None];_LOADS.append(slot)
  try:slot[2]=original(loader,spec);return slot[2]
  except BaseException as error:slot[3]=error;raise
 external.ExtensionFileLoader.create_module=dynamic
 return old
def _prepare_runtime():
 global _EF,_EI,_BR,_PF,_FFI_CALLBACK,LIMITS
 global ctypes,hashlib,json,resource,selectors,stat,types,math
 global base64,select,threading
 _EF = _live_fds()
 if ([row[0] for row in _EF] != [0,1,2]
     or any(row[1] != 0 for row in _EF)
     or [row[2] & os.O_ACCMODE for row in _EF] != [os.O_RDONLY,os.O_WRONLY,os.O_WRONLY]):
    raise ValueError("strict_inherited_0_1_2")
 _EI = {}
 for fd in range(3):
    info = os.fstat(fd)
    link = os.readlink("/proc/self/fd/%d" % fd)
    charged = len(link.encode("utf-8"))
    _BR += charged
    _note("caller.entry_readlink:" + str(fd),"proc_fd_link",charged,charged)
    _EI[fd] = (info,link)
 _install_loader()
 import ctypes,hashlib,json,resource,selectors,stat,types
 import math
 import base64,select,threading
 _load_operands()
 _codec.OPERANDS.configure(selectors)
 resource = _ObservedModule(resource,"caller")
 selectors = _ObservedModule(selectors,"caller")
 _FFI_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int)(lambda: 0)
 _PF = _live_fds()
 LIMITS = ((resource.RLIMIT_AS,(67108864,1610612736)),
      (resource.RLIMIT_CPU,(300,7200)),
      (resource.RLIMIT_NOFILE,(256,256)),
      (resource.RLIMIT_CORE,(0,0)),
      (resource.RLIMIT_FSIZE,(67108864,67108864)))
def _bootstrap_failure(error):
 try:
  reason = str(error)[:256]
 except BaseException:
  reason = 'exception_string_unavailable'
 kind = type(error).__name__[:64]
 try:
  message = ('{"cause":"caller_bootstrap","error_type":' + quoted(kind)
    + ',"explicit_operation_trace":' + self_bootstrap_json(_E.wire())
    + ',"reason":' + quoted(reason)
    + ',"release_credit":false,"runtime_GO":false,"spawned":false,"status":"REFUSED"'
    + ',"whole_performing_trace_complete":false}\n').encode('ascii')
  _E.note("caller","native.before_delivery",[],message)
  os.set_blocking(1,False)
  if len(message) > 262144:
   return 125
  if min(_BW + 30 - time.time(),_BM + 30 - time.monotonic()) <= 0:
   return 125
  count = os.write(1,message)
  _FO.bootstrap_written += count
  _E.note("caller","bootstrap.write",[1,message],count)
 except BaseException as failure:
  _E.note("caller","bootstrap.write.error",[1],error=failure)
 try:
  _bootstrap_tail()
 except BaseException:
  _E.failed=True
 return 125  # never success,even if the complete bootstrap line left fd1
def _bootstrap_tail():
 global hashlib,json,threading,select,stat
 if _C is None and _AS=="NEVER_ATTEMPTED":
  import hashlib,json,threading,select,stat
  _E.attach(attach_caller((_BW+30,_BM+30)))
def _owned_failure(error):
 ""
 owner = _FO
 if owner is not None and owner.domain == "caller_bootstrap":
  return _bootstrap_failure(error)
 try:
  session = owner.session if owner is not None else None
  io = getattr(session,"io",None)
  prior = getattr(io,"root_output",0)
  proc = getattr(session,"proc",None)
  pid = getattr(proc,"pid",None)
  spawned = True if type(pid) is int and pid > 0 else (False if proc is None else None)
  attempted = None if session is None else getattr(session,"spawn_attempted",None)
  message = self_bootstrap_json({"cause": "late_owned_failure",
   "domain": owner.domain if owner is not None else "UNKNOWN",
   "error_type": type(error).__name__[:64],"status": "STOP_UNCONFIRMED",
   "spawned": spawned,"spawn_attempted": attempted,
   "root_bytes_written_before_failure": prior,
   "cleanup_confirmed": owner.cleanup.get("confirmed") if owner is not None and type(owner.cleanup) is dict else None,
   "delivery_confirmed": owner.delivery.get("confirmed") if owner is not None and type(owner.delivery) is dict else None,
   "complete_cleanup_delivery_evidence": "RETAINED_BY_PHASE_OWNER_TRANSPORT_NOT_PROVEN",
   "runtime_GO": False,"release_credit": False,
   "whole_performing_trace_complete": False}) .encode("ascii") + b"\n"
  if len(message) + prior > 262144:
   return 125
  clock = getattr(session,"clock",None)
  if clock is None or clock.handoff_left() <= 0:
   return 125
  count = os.write(1,message)
  if io is not None:
   io.root_output += count
  _E.note("caller","late_owned_failure.write",[1,message],count)
 except BaseException:
  pass
 return 125
def _identity(info):
 return [info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
  info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
def _bootstrap_wire(raw):
 def pairs(rows):
  result = {}
  for key,value in rows:
   if key in result:
    raise ValueError("duplicate wire key")
   result[key] = value
  return result
 def no_float(_value):
  raise ValueError("control float")
 value = json.loads(raw,object_pairs_hook=pairs,parse_float=no_float,
     parse_constant=no_float)
 canonical = (json.dumps(value,sort_keys=True,ensure_ascii=True,
      allow_nan=False,separators=(",",":")) + "\n").encode("ascii")
 if type(value) is not dict or raw != canonical:
  raise ValueError("canonical object wire")
 return value
def _runtime_fd():
 global _BR
 if _live_fds() != _PF or [row for row in _PF if row[0] < 3] != _EF:
  raise ValueError("stock_runtime_window_drift")
 for fd,(info,link) in _EI.items():
  current_link = os.readlink("/proc/self/fd/%d" % fd)
  charged = len(current_link.encode("utf-8"))
  _BR += charged
  _note("caller.entry_reread","proc_fd_link",charged,charged)
  if _identity(os.fstat(fd)) != _identity(info) or current_link != link:
   raise ValueError("inherited_fd_drift")
 extras = [row for row in _PF if row[0] >= 3]
 if len(extras) != 1 or extras[0][1] != fcntl.FD_CLOEXEC or extras[0][2] & os.O_ACCMODE != os.O_RDONLY:
  raise ValueError("only_new_stock_ffi")
 fd,_,flags = extras[0]
 before = os.fstat(fd)
 current_link = os.readlink("/proc/self/fd/%d" % fd)
 charged = len(current_link.encode("utf-8"))
 _BR += charged
 _note("caller.ffi_link","proc_fd_link",charged,charged)
 if (current_link != FFI_PATH
   or _identity(os.lstat(FFI_PATH)) != _identity(before)
   or not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o644
   or (before.st_uid,before.st_gid,before.st_nlink,before.st_size) != (0,0,1,64184)
   or flags & (os.O_PATH | os.O_APPEND)):
  raise ValueError("stock_ffi_identity")
 raw = os.pread(fd,before.st_size + 1,0)
 _note("caller.stock_ffi","pread",before.st_size + 1,len(raw),raw=raw)
 if len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != FFI_SHA or _identity(os.fstat(fd)) != _identity(before):
  raise ValueError("stock_ffi_bytes")
 return {"fd": fd,"identity": _identity(before),"flags": flags,"path": FFI_PATH,
   "sha256": FFI_SHA,"observed_bytes": len(raw),"requested_cap": before.st_size + 1,
   "entry_fd_map": _EF,"live_fd_map": _PF,
   "provenance": "ABSENT_AT_STRICT_ENTRY_NEW_DURING_STOCK_IMPORTS_CALLBACK_PREPARATION"}
def _first_line(start_wall,start_mono):
 os.set_blocking(0,False)
 raw = bytearray()
 with selectors.DefaultSelector() as selector:
  selector.register(0,selectors.EVENT_READ)
  while True:
   wall,mono = time.time(),time.monotonic()
   if abs(wall - start_wall - (mono - start_mono)) > 2:
    raise ValueError("clock drift")
   left = min(start_wall + 30 - wall,start_mono + 30 - mono)
   if left <= 0:
    raise ValueError("fixed_deadline")
   if not selector.select(min(left,0.25)):
    continue
   try:
    part = os.read(0,1)  # retain the following AUTH in the original pipe
   except BlockingIOError:
    _note("caller.stage1_fd0","inputwire",1,0,would_block=True,charged=False)
    continue
   except OSError as error:
    _note("caller.stage1_fd0","inputwire",1,0,charged=False,error=error)
    raise
   _note("caller.stage1_fd0","inputwire",1,len(part),raw=part)
   if not part or len(raw) >= 131072:
    raise ValueError("bounded stage1 or EOF")
   raw.extend(part)
   if part == b"\n":
    return bytes(raw)
def _pinned_sender(stage1):
 selected = stage1["sender"]
 folder = os.path.dirname(os.path.abspath(__file__))
 if (type(selected) is not dict or set(selected) != {"root","files"}
   or selected["root"] != folder or type(selected["files"]) is not dict
   or set(selected["files"]) != {"caller.py","staged_sender.py"}):
  raise ValueError("caller_source_selection")
 directory = os.lstat(folder)
 if (not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700
   or (directory.st_uid,directory.st_gid) != (1000,1000)):
  raise ValueError("caller_source_selection")
 read_bytes,sender_raw = 0,None
 for name in ("caller.py","staged_sender.py"):
  pin = selected["files"][name]
  if (type(pin) is not dict or set(pin) != {"identity","sha256"}
    or type(pin["identity"]) is not list or len(pin["identity"]) != 9
    or any(type(v) is not int or v < 0 for v in pin["identity"])
    or type(pin["sha256"]) is not str or len(pin["sha256"]) != 64):
   raise ValueError("caller_source_selection")
  path = folder + "/" + name
  before = os.lstat(path)
  if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600
    or (before.st_uid,before.st_gid,before.st_nlink) != (1000,1000,1)
    or not 0 < before.st_size <= 65536 or _identity(before) != pin["identity"]):
   raise ValueError("caller_source_selection")
  fd = os.open(path,os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
  try:
   if _identity(os.fstat(fd)) != pin["identity"]:
    raise ValueError("caller_source_selection")
   raw = os.pread(fd,before.st_size + 1,0)
   read_bytes += len(raw)
   _note("caller.pinned:" + name,"pread",before.st_size + 1,len(raw),raw=raw)
   if (_identity(os.fstat(fd)) != pin["identity"] or len(raw) != before.st_size
     or _identity(os.lstat(path)) != pin["identity"]
     or hashlib.sha256(raw).hexdigest() != pin["sha256"]):
    raise ValueError("caller_source_selection")
   if name == "staged_sender.py":
    sender_raw = raw
  finally:
   os.close(fd)
 sender = types.ModuleType("a088_checked_staged_sender")
 sender.__file__ = folder + "/staged_sender.py"
 exec(compile(sender_raw,sender.__file__,"exec",dont_inherit=True),sender.__dict__)
 sender.ReadJournal,sender.TRACE = ReadJournal,_E
 sender.Budget = Budget
 sender.AcquisitionOwner = AcquisitionOwner
 sender._predicate_frame=sys._getframe
 sender.failure_owner = _FO
 sender.native_contract = OrdinaryNativeContract()
 _expected=stage1.get("expectation") if type(stage1) is dict else None
 _pins=_expected.get("source_hashes") if type(_expected) is dict else None
 _E.state_byte_pins={v for v in _pins.values() if type(v) is str and len(v)==64} if type(_pins) is dict else set()
 def scoped(function,name):
  def invoke(*args,**kwargs):
   previous_scope=_E.guard_scope_snapshot
   _E.guard_scope.append(name)
   pure = "." not in name
   method=None
   cleanup=name.endswith(".close") or name=="Session.cleanup"
   try:
    _E.guard_scope_snapshot=tuple(_E.guard_scope)
    if pure:_E.note("sender","grammar.enter",[name,args,kwargs])
    else:method=_E.method_enter(name,args,kwargs,cleanup)
    if not cleanup:_E.check()
    result = function(*args,**kwargs)
    if pure:
     _E.note("sender","grammar.return",[name],result)
    else:_E.method_end(method,result)
    return result
   except BaseException as error:
    if pure:
     _E.note("sender","grammar.error",[name],error=error)
    elif method is not None and not method.get("ended"):_E.method_end(method,error=error)
    raise
   finally:
    _E.guard_scope.pop()
    _E.guard_scope_snapshot=previous_scope
  return invoke
 for name in ("exact","control_types","encode","object_wire","source_map","expectation_shape",
  "body_shape","context_shape","namespace_shape","custody_shape","validate_stage1","validate_init",
  "validate_stage2","validate_claim","validate_report","check_held_observation"):
  setattr(sender,name,scoped(getattr(sender,name),name))
 for cls in (sender.Clock,sender.Named,sender.Directory,sender.Generation,sender.OwnScope,sender.Held,sender.IO,sender.Session):
  for name,function in tuple(vars(cls).items()):
   if callable(function):setattr(cls,name,scoped(function,cls.__name__+"."+name))
 sender.perform_staged_send=scoped(sender.perform_staged_send,"staged.perform_staged_send")
 for name in ("os","time","fcntl","resource","selectors","signal"):
  setattr(sender,name,_ObservedModule(getattr(sender,name),"sender"))
 sender.subprocess.os=_ObservedModule(sender.subprocess.os,"sender.stockinternal")
 return sender,read_bytes
def main():
 _begin_performing()
 _prepare_runtime()
 if len(sys.argv) != 1 or (os.geteuid(),os.getegid()) != (1000,1000):
  raise ValueError("exact_stock_caller")
 for kind,expected in LIMITS:
  if resource.getrlimit(kind) != expected:
   raise ValueError("trusted_pre_interpreter_limits")
 if (fcntl.fcntl(0,fcntl.F_GETFL) & os.O_ACCMODE) != os.O_RDONLY:
  raise ValueError("original_root_input_not_readonly")
 wall0,mono0 = _BW,_BM
 runtime_fd = _runtime_fd()
 client = attach_caller((wall0 + 30,mono0 + 30))
 _E.attach(client)
 raw = _first_line(wall0,mono0)
 stage1 = _bootstrap_wire(raw)
 sender,bootstrap_named_read_bytes = _pinned_sender(stage1)
 bootstrap_read_bytes = (len(raw) + bootstrap_named_read_bytes
     + runtime_fd["observed_bytes"] + _BR)
 charged_trace = sum(row["returned_bytes"] for row in _BT if row["charged_observed"])
 if charged_trace != bootstrap_read_bytes:
  raise ValueError("bootstrap_trace_incomplete")
 result = sender.perform_staged_send(stage1,raw,wall0,mono0,bootstrap_read_bytes,
      runtime_fd,_BT)
 _E.note("caller","sender.returned_outcome",[],result)
 _E.check()
 return 0 if (result["status"] == "RUN_REPORTED"
    and result["reported_entrypoint_returncodes"] == [0]
    and result.get("cleanup",{}).get("confirmed") is True
    and result.get("delivery",{}).get("confirmed") is True) else 125
def value(item):
 if item is None or type(item) in (str, bool, int):
  return item
 if type(item) is float:
  return {"type": "float_hex", "value": item.hex()}
 if type(item) is bytes:
  pin=hashlib.sha256(item).hexdigest()
  if len(item)==4139478 and pin=="e7255f04bc4fa806960fcca79b6b37b67202417170ed59c3b9101863e3e91224":
   return {"type":"held_bytes","length":len(item),"sha256":pin}
  return {"type": "bytes", "length": len(item), "sha256": hashlib.sha256(item).hexdigest(),
   "raw_hex": item.hex()}
 if isinstance(item, _RO.stat_result):
  return {"type": "identity9", "value": id9(item)}
 if isinstance(item, (list, tuple)):
  return [value(v) for v in item]
 if type(item) in (set, frozenset):
  return {"type": "set", "value": [value(v) for v in sorted(item)]}
 if type(item) is dict:
  return {"type": "mapping", "value": [[value(k), value(v)] for k, v in item.items()]}
 if type(item).__module__=="pathlib" and isinstance(item,_RO.PathLike):
  return {"type":"path","value":_RO.fspath(item)}
 if type(item).__name__=="CompletedProcess" and type(item).__module__=="subprocess":
  return {"type":"completed_process","args":value(item.args),"returncode":item.returncode,"stdout":value(item.stdout),"stderr":value(item.stderr)}
 if hasattr(item, "fd") and hasattr(item, "events"):
  return {"type": "selector_key", "fd": item.fd, "events": item.events, "data": value(item.data)}
 if isinstance(item, int):
  return {"type": "integer_subtype", "name": type(item).__name__, "value": int(item)}
 if hasattr(item, "fileno"):
  return {"type": "file_handle", "fd": item.fileno(), "name": value(getattr(item, "name", None))}
 if hasattr(item, "pid"):
  return {"type": "process", "pid": item.pid, "returncode": getattr(item, "returncode", None)}
 raise TypeError("unrepresented_a132_actor_value:" + type(item).__name__)
def _xlinks(e,s=None,d=0):
 if e is None:return None
 owners=[e];ids={id(e):0};nodes=[]
 def ref(error):
  if error is None:return None
  if id(error) not in ids:ids[id(error)]=len(owners);owners.append(error)
  return ids[id(error)]
 def graph_value(item):
  if isinstance(item,BaseException):return {'type':'error_ref','id':ref(item)}
  if type(item) in (list,tuple):return [graph_value(v) for v in item]
  if type(item) is dict:return {'type':'mapping','value':[[graph_value(k),graph_value(v)] for k,v in item.items()]}
  return value(item)
 for error in owners:
  frames=[];tb=error.__traceback__
  while tb is not None:
   c=tb.tb_frame.f_code;frames.append([c.co_filename,tb.tb_lineno,c.co_name]);tb=tb.tb_next
  nodes.append({'id':ids[id(error)],'type':type(error).__name__,'module':type(error).__module__,
   'args':graph_value(error.args),'text':str(error),'errno':getattr(error,'errno',None),
   'filename':graph_value(getattr(error,'filename',None)),'filename2':graph_value(getattr(error,'filename2',None)),
   'state':graph_value(vars(error)),'notes':graph_value(getattr(error,'__notes__',None)),
   'frames':frames,'cause':ref(error.__cause__),'context':ref(error.__context__),
   'suppress_context':error.__suppress_context__,'groups':[ref(x) for x in getattr(error,'exceptions',())]})
 return {'schema':'friday.error-graph.v1','root':0,'nodes':nodes,'truncated':False}
def _damage(raw,err,secondary):
 return {"operation":raw[1],"full_raw_owner_retained":True,"durable_independent_custody":"NOT_CONFIRMED","exception":_xlinks(err),"secondary_fault":_xlinks(secondary)}
_codec=None
_C=None
_AO=None
_AS="NEVER_ATTEMPTED"
_CODEC_PATH="/var/tmp/friday-sol089-sender-stock-python-origin-body-alias-connected-inert-source/source/sender_operand_codec.py"
_CODEC_SHA="389575b32c9627e057d098b7a2f1efa66e646ba41c61ef73519f6afe05a3e650"
_CODEC_ID=[2050,4371525,33152,1000,1000,1,31023,1791014779193548867,1791014779193548867]
def _load_operands():
 global _codec,_BR,attach_inherited,observed_fds,note,raw_client
 if _codec is not None:raise RuntimeError("one same-generation operand codec load")
 before=_RO.lstat(_CODEC_PATH)
 if _identity(before)!=_CODEC_ID or before.st_size!=31023:
  raise ValueError("whole operand codec current identity")
 if _BR+before.st_size+1+_LUP>33554432:raise ValueError("original_sender_loader_read_bound")
 fd=_RO.open(_CODEC_PATH,_RO.O_RDONLY|_RO.O_CLOEXEC|_RO.O_NOFOLLOW)
 try:
  if _identity(_RO.fstat(fd))!=_CODEC_ID:raise ValueError("held operand codec identity")
  raw=_RO.pread(fd,before.st_size+1,0);_BR+=len(raw)
  _note("caller.operand_codec","pread",before.st_size+1,len(raw),raw=raw)
  if len(raw)!=before.st_size or hashlib.sha256(raw).hexdigest()!=_CODEC_SHA or _identity(_RO.fstat(fd))!=_CODEC_ID or _identity(_RO.lstat(_CODEC_PATH))!=_CODEC_ID:
   raise ValueError("whole held/named operand codec bytes")
 finally:_RO.close(fd)
 module=types.ModuleType("sender_operand_codec");module.__file__=_CODEC_PATH
 sys.modules[module.__name__]=module
 exec(compile(raw,_CODEC_PATH,"exec",dont_inherit=True),module.__dict__)
 _codec=module
 attach_inherited,observed_fds,note,raw_client=(module.attach_inherited,module.observed_fds,module.note,module.raw_client)
def attach_caller(ends):
 global _C,_AO,_AS
 try:return _codec.attach_caller(ends)
 finally:_C,_AO,_AS=_codec._C,_codec._AO,_codec._AS
if __name__ == "__main__":
 try:
   exit_code = main()
 except BaseException as error:
   _E.note("caller","main.error",[],error=error)
   exit_code = _owned_failure(error)
 if _C is not None:
   _C.terminal(exit_code)
   if _C.failed:
     exit_code = 125
   try:
     _C.close()
   except BaseException as error:
     _E.note("caller","actor.final_close.error",[],error=error)
     exit_code = 125
 raise SystemExit(exit_code)
