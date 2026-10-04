"""SOL089 inert Source: actual stock-Python bodies, birth and alias correspondence.
No constructor, test, import or probe of this Source was executed by the author.
The Python graph never admits a native field as a name/FD/hash/body substitute.
"""
import os
import sys
import time
import errno
import hashlib
import json
import threading
import select

_RO, _RT = os, time
GRAPH_SCHEMA = "friday.sol089.stock-python-body-alias.v1"

def require(ok, cause):
 if not ok:raise ValueError(cause)

class NativeBodyUnavailable(TypeError):
 pass

class Catalog:
 def __init__(self, module):
  self.module=module
  self.selected=module.DefaultSelector
  # The actual selected class, not an assumed meaning of DefaultSelector's name.
  self.classes={}
  for cls in (self.selected,module._SelectorMapping):
   key=(cls.__module__,cls.__qualname__)
   self.classes[key]=cls
   require(cls.__new__ is object.__new__,"selected stock allocator is not pure object.__new__")
   require(cls.__getattribute__ is object.__getattribute__,"custom instance lookup is not a qualified dictionary body")
   for base in cls.__mro__:
    require(not vars(base).get("__slots__"),"nonempty stock slots require their complete qualified body")
    require("__del__" not in vars(base),"native/destructor replay cannot be inferred from a dictionary")
  self.key_class=module.SelectorKey
 def descriptor(self, cls):
  require(cls in self.classes.values(),"only actual selected qualified stock classes")
  return {"module":cls.__module__,"qualname":cls.__qualname__,
   "mro":[[b.__module__,b.__qualname__] for b in cls.__mro__],
   "metaclass":[type(cls).__module__,type(cls).__qualname__],
   "layout":"full-instance-dictionary/no-nonempty-slots"}
 def resolve(self, descriptor):
  require(type(descriptor) is dict,"complete stock class binding")
  cls=self.classes.get((descriptor.get("module"),descriptor.get("qualname")))
  require(cls is not None and self.descriptor(cls)==descriptor,"same qualified actual stock class and MRO/metaclass/layout")
  return cls

class FrozenArguments:
 def __init__(self, raw):
  self.raw=raw
  self.wire=value(raw)

class OperandOwner:
 def __init__(self):
  self.catalog=None;self.owners=[];self.ids={};self.births={};self.pending=None
  self.birth_serial=0;self.snapshots=[];self.pending_snapshot=None
  self.damage=[];self.first_error=None
 def configure(self, module):
  if self.catalog is None:self.catalog=Catalog(module)
  require(self.catalog.selected is module.DefaultSelector,"selected stock class cannot change within an actor generation")
 def owns(self, item):
  return id(item) in self.ids and self.owners[self.ids[id(item)]] is item
 def is_stock(self, item):
  return self.catalog is not None and type(item) in self.catalog.classes.values()
 def number(self, item):
  key=id(item)
  if key not in self.ids:
   self.pending=item
   self.owners.append(item)
   self.ids[key]=len(self.owners)-1
  number=self.ids[key]
  require(self.owners[number] is item,"strong ownership prohibits recycled address aliases")
  return number
 def birth(self, raw, operation, actor_owner):
  require(self.catalog is not None and type(raw) is self.catalog.selected,"actual selected original allocator result")
  number=self.number(raw)
  require(number not in self.births,"no second birth or rebind of an owned operand")
  self.births[number]={"pid":os.getpid(),"ordinal":self.birth_serial,
   "operation":operation,"owner":actor_owner,"object":number}
  self.birth_serial+=1
  return self.capture(raw)
 def capture(self, raw):
  require(self.owns(raw) and self.number(raw) in self.births,"stock body must originate in the actual allocated operand")
  nodes=[];seen=set()
  def leaf(item):
   if item is None or type(item) is bool:return item
   number=self.number(item)
   if number in seen:return {"ref":number}
   seen.add(number)
   node={"id":number,"kind":None,"class":None,"body":None}
   nodes.append(node)
   if type(item) in (int,str,float,bytes):
    node["kind"]=type(item).__name__
    node["body"]=str(item) if type(item) is int else item if type(item) is str else item.hex()
   elif type(item) in (list,tuple,set,frozenset):
    node["kind"]=type(item).__name__;node["body"]=[leaf(x) for x in item]
   elif type(item) is bytearray:
    node["kind"]="bytearray";node["body"]=item.hex()
   elif type(item) is dict:
    node["kind"]="dict";node["body"]=[[leaf(k),leaf(v)] for k,v in item.items()]
   elif self.is_stock(item):
    node["kind"]="stock-python";node["class"]=self.catalog.descriptor(type(item))
    # No fileno, constructor, arbitrary properties, public facade or empty stand-in.
    # A genuinely empty pre-init dictionary is captured as an actual dict node.
    node["body"]=leaf(object.__getattribute__(item,"__dict__"))
   elif type(item) is self.catalog.key_class:
    node["kind"]="selector-key";node["body"]=[leaf(x) for x in tuple(item)]
   else:
    raise NativeBodyUnavailable("complete body of actual field is not supplied: "+type(item).__module__+"."+type(item).__qualname__)
   return {"ref":number}
  slot={"raw":raw,"origin":dict(self.births[self.number(raw)]),"wire":None,"error":None}
  self.pending_snapshot=slot;self.snapshots.append(slot)
  try:
   wire={"type":"stock_operand_graph","schema":GRAPH_SCHEMA,
    "origin":dict(slot["origin"]),"root":leaf(raw),"nodes":nodes,"complete_python_body":True}
   slot["wire"]=wire
   return wire
  except BaseException as error:
   slot["error"]=error
   if self.first_error is None:self.first_error=error
   self.damage.append(slot)
   raise

def member_wire(item):
 require(OPERANDS.catalog is not None and type(item) is OPERANDS.catalog.key_class,
  "actual qualified stock key body, never FD/name tuple substitute")
 number=OPERANDS.number(item)
 for root in OPERANDS.owners:
  if OPERANDS.is_stock(root) and OPERANDS.number(root) in OPERANDS.births:
   graph=OPERANDS.capture(root)
   if any(n["id"]==number for n in graph["nodes"]):
    return {"type":"stock_operand_member","graph":graph,"member":number}
 raise NativeBodyUnavailable("actual result member has no held full-body origin/alias graph")

OPERANDS=OperandOwner()

class OperandRegistry:
 """Independent receiver of whole captured graphs from its actual held ledger.
 Only prior birth data can prepare an operand. Completed expected arguments cannot.
 Python correspondence is distinct from later native/private body admission.
 """
 def __init__(self, held, stock_module):
  self.held=held;self.catalog=Catalog(stock_module)
  self.birth_rows=[];self.birth_by_origin={};self.taken=set();self.birth_last={}
  self.objects={};self.reverse={};self.owner_keys={};self.cut=None
 def key(self, wire):
  o=wire["origin"]
  require(set(o)=={"pid","ordinal","operation","owner","object"}
   and all(type(o[k]) is int and o[k]>=0 for k in ("pid","ordinal","object"))
   and o["pid"]>0 and o["operation"]=="selector.allocate" and type(o["owner"]) is str,
   "exact original allocator origin, not class/FD/shape-only")
  return (o["pid"],o["ordinal"],o["object"])
 def validate(self, wire):
  require(type(wire) is dict and set(wire)=={"type","schema","origin","root","nodes","complete_python_body"}
   and wire["type"]=="stock_operand_graph" and wire["schema"]==GRAPH_SCHEMA
   and wire["complete_python_body"] is True and type(wire["nodes"]) is list,
   "exact full Python graph; no opaque/partial/native promotion")
  key=self.key(wire);nodes={}
  for row in wire["nodes"]:
   require(type(row) is dict and set(row)=={"id","kind","class","body"}
    and type(row["id"]) is int and row["id"]>=0 and row["id"] not in nodes,
    "unique exact node identities")
   nodes[row["id"]]=row
  require(type(wire["root"]) is dict and set(wire["root"])=={"ref"}
   and wire["root"]["ref"]==key[2] and key[2] in nodes
   and nodes[key[2]]["kind"]=="stock-python","full original root body node")
  used=set()
  def visit(part):
   if part is None or type(part) is bool:return
   require(type(part) is dict,"all non-singleton graph edges explicitly typed and aliased")
   if part.get("scalar")=="float":
    require(set(part)=={"scalar","hex"} and type(part["hex"]) is str
     and float.fromhex(part["hex"]).hex()==part["hex"],"canonical float body");return
   if part.get("scalar")=="bytes":
    require(set(part)=={"scalar","hex"} and type(part["hex"]) is str
     and bytes.fromhex(part["hex"]).hex()==part["hex"],"complete canonical bytes");return
   require(set(part)=={"ref"} and type(part["ref"]) is int and part["ref"] in nodes,"all aliases refer to complete body")
   number=part["ref"]
   if number in used:return
   used.add(number);node=nodes[number];kind=node["kind"];body=node["body"]
   if kind=="stock-python":
    self.catalog.resolve(node["class"])
    require(type(body) is dict and set(body)=={"ref"} and nodes.get(body["ref"],{}).get("kind")=="dict",
     "actual whole dictionary node, never class tag or empty body stand-in")
    visit(body)
   else:
    require(node["class"] is None,"no alternate nominal type grant")
    if kind in ("int","str","float","bytes"):
     require(type(body) is str,"full exact immutable body")
     if kind=="int":require(str(int(body))==body,"canonical exact integer body, no float round trip")
     elif kind=="float":require(float.fromhex(body).hex()==body,"canonical exact float body")
     elif kind=="bytes":require(bytes.fromhex(body).hex()==body,"full immutable byte body")
    elif kind in ("list","tuple","set","frozenset","selector-key"):
     require(type(body) is list and (kind!="selector-key" or len(body)==4),"full typed collection body")
     for x in body:visit(x)
    elif kind=="dict":
     require(type(body) is list,"full ordered dictionary body")
     for pair in body:
      require(type(pair) is list and len(pair)==2,"exact dictionary pair")
      visit(pair[0]);visit(pair[1])
    elif kind=="bytearray":
     require(type(body) is str and bytearray.fromhex(body).hex()==body,"full bytearray")
    else:raise NativeBodyUnavailable("native/raw/private/unknown body has no Python graph route")
  visit(wire["root"])
  require(used==set(nodes),"no unexplained unreachable/body-suffix nodes")
  return key,nodes
 def ingest_birth(self, index, event, wire):
  key,nodes=self.validate(wire)
  require(event["operation"]=="selector.allocate" and event["pid"]==key[0]
   and key not in self.birth_by_origin and event["error"] is None,
   "unique successful actual allocation event in independently held chronology")
  require(nodes[key[2]]["class"]==self.catalog.descriptor(self.catalog.selected),
   "Root actual qualified selected class, no DefaultSelector-name guess")
  # Actual stock object.__new__ has no initializer effect. Its real body is
  # checked here; the schema does not supply {} as a substitute for other bodies.
  body=nodes[nodes[key[2]]["body"]["ref"]]
  require(body["kind"]=="dict" and body["body"]==[],
   "observed exact object.__new__ pre-init body, not a completed-call argument copy")
  require(key[1]==self.birth_last.get(key[0],-1)+1,"original allocator ordinal is consecutive, no invented/recycled origin")
  self.birth_last[key[0]]=key[1]
  self.birth_rows.append((index,event,wire))
  self.birth_by_origin[key]=(index,event,wire)
 def allocate(self, owner, class_descriptor):
  remaining=[r for r in self.birth_rows if self.key(r[2]) not in self.taken]
  require(remaining,"performing stock allocation has a real earlier held birth")
  index,event,wire=remaining[0];key,nodes=self.validate(wire)
  require(wire["origin"]["owner"]==owner and nodes[key[2]]["class"]==class_descriptor,
   "performing caller selected its own original class/owner before argument matching")
  # Calls only exact qualified Python object allocator, never selector.__init__.
  cls=self.catalog.resolve(class_descriptor)
  raw=object.__new__(cls)
  require(type(raw) is cls and object.__getattribute__(raw,"__dict__")=={},
   "actual readonly allocation body matches independently held pre-init origin")
  self.taken.add(key);self.cut=index
  self.objects[(key[0],key[2])]=raw;self.reverse[id(raw)]=(key[0],key[2])
  self.objects[(key[0],nodes[key[2]]["body"]["ref"])]=object.__getattribute__(raw,"__dict__")
  self.reverse[id(object.__getattribute__(raw,"__dict__"))]=(key[0],nodes[key[2]]["body"]["ref"])
  return raw
 def decode(self, wire, *, restore=False):
  key,nodes=self.validate(wire)
  require(key in self.birth_by_origin and key in self.taken,"no operands materialized from expected completed-call arguments")
  pid=key[0];busy=set();filled=set()
  def make(part):
   if part is None or type(part) in (bool,int,str):return part
   if part.get("scalar")=="float":return float.fromhex(part["hex"])
   if part.get("scalar")=="bytes":return bytes.fromhex(part["hex"])
   number=part["ref"];identity=(pid,number);row=nodes[number];kind=row["kind"]
   if identity in self.objects and not restore:return self.objects[identity]
   if identity in busy:
    require(identity in self.objects and kind in ("stock-python","dict","list","set","bytearray"),"immutable self-cycle requires a separately qualified body")
    return self.objects[identity]
   if identity not in self.objects:
    if kind=="stock-python":obj=object.__new__(self.catalog.resolve(row["class"]))
    elif kind=="int":obj=int(row["body"])
    elif kind=="str":obj=row["body"].encode("utf-8","surrogatepass").decode("utf-8","surrogatepass")
    elif kind=="float":obj=float.fromhex(row["body"])
    elif kind=="bytes":obj=bytes.fromhex(row["body"])
    elif kind=="dict":obj={}
    elif kind=="list":obj=[]
    elif kind in ("set","frozenset"):obj=set()
    elif kind=="bytearray":obj=bytearray.fromhex(row["body"])
    elif kind in ("tuple","selector-key"):obj=None
    else:raise NativeBodyUnavailable("unknown complete body kind")
    if obj is not None:self.objects[identity]=obj;self.reverse[id(obj)]=identity
   obj=self.objects.get(identity);busy.add(identity)
   body=row["body"]
   if kind=="stock-python":
    dictionary=make(body)
    require(type(dictionary) is dict,"whole raw dictionary is not a facade")
    if restore:object.__setattr__(obj,"__dict__",dictionary)
   elif kind=="dict":
    pairs=[(make(k),make(v)) for k,v in body]
    require(len({k for k,v in pairs})==len(pairs),"duplicate physical mapping keys")
    if restore:obj.clear();obj.update(pairs)
   elif kind in ("list","tuple","selector-key"):
    parts=[make(x) for x in body]
    if kind=="list":
     if restore:obj[:]=parts
    else:
     candidate=tuple(parts) if kind=="tuple" else self.catalog.key_class(*parts)
     if obj is not None:require(obj==candidate and type(obj) is type(candidate),"immutable aliases cannot be rebound")
     else:obj=candidate;self.objects[identity]=obj;self.reverse[id(obj)]=identity
   elif kind in ("set","frozenset"):
    parts=[make(x) for x in body]
    if kind=="set":
     if restore:obj.clear();obj.update(parts)
    else:
     candidate=frozenset(parts)
     if type(obj) is set:obj=candidate;self.objects[identity]=obj;self.reverse[id(obj)]=identity
     else:require(obj==candidate,"same immutable set alias")
   elif kind=="bytearray" and restore:obj[:]=bytearray.fromhex(body)
   busy.remove(identity);filled.add(identity)
   return obj
  result=make(wire["root"])
  self.match(wire,result)
  return result
 def match(self, wire, raw):
  key,nodes=self.validate(wire);seen=set();pid=key[0]
  require(key in self.taken and self.birth_by_origin[key][0]<=self.cut,"birth and body timing before strict matcher")
  def check(part,item):
   if part is None or type(part) in (bool,int,str):
    require(type(part) is type(item) and part==item,"full typed Python scalar body");return
   if part.get("scalar")=="float":
    require(type(item) is float and item.hex()==part["hex"],"full float body");return
   if part.get("scalar")=="bytes":
    require(type(item) is bytes and item.hex()==part["hex"],"full byte body");return
   number=part["ref"];identity=(pid,number);row=nodes[number];kind=row["kind"]
   require(self.objects.get(identity) is item and self.reverse.get(id(item)) in (None,identity),
    "explicit bidirectional original alias correspondence before same_typed")
   self.reverse[id(item)]=identity
   if number in seen:return
   seen.add(number);body=row["body"]
   if kind=="stock-python":
    require(type(item) is self.catalog.resolve(row["class"]),"same actual full stock Python class")
    check(body,object.__getattribute__(item,"__dict__"))
   elif kind in ("int","str","float","bytes"):
    cls={"int":int,"str":str,"float":float,"bytes":bytes}[kind]
    actual=str(item) if kind=="int" else item if kind=="str" else item.hex()
    require(type(item) is cls and actual==body,"full typed immutable body and alias")
   elif kind=="dict":
    require(type(item) is dict and len(item)==len(body),"no lost/added body fields")
    for pair,actual in zip(body,item.items()):check(pair[0],actual[0]);check(pair[1],actual[1])
   elif kind in ("list","tuple","selector-key"):
    cls=self.catalog.key_class if kind=="selector-key" else list if kind=="list" else tuple
    require(type(item) is cls and len(item)==len(body),"same full typed sequence")
    for part,actual in zip(body,item):check(part,actual)
   elif kind=="bytearray":
    require(type(item) is bytearray and item.hex()==body,"complete mutable body")
   elif kind in ("set","frozenset"):
    require(type(item) is (set if kind=="set" else frozenset) and len(item)==len(body),"same exact set")
    remaining=list(item)
    for part in body:
     hits=[x for x in remaining if (type(part) is dict and set(part)=={"ref"} and self.objects.get((pid,part["ref"])) is x)
      or (type(part) is not dict and type(part) is type(x) and part==x)]
     require(len(hits)==1,"unique original set membership, never order by repr/address")
     check(part,hits[0]);remaining.remove(hits[0])
  check(wire["root"],raw)

 def fork(self, cut, pid):
  result=OperandRegistry(self.held,self.catalog.module)
  result.birth_rows=[r for r in self.birth_rows if r[1]["pid"]==pid]
  result.birth_by_origin={result.key(r[2]):r for r in result.birth_rows}
  for index,event,wire in result.birth_rows:
   if index>=cut:break
   number=wire["root"]["ref"]
   node=next(n for n in wire["nodes"] if n["id"]==number)
   result.allocate(wire["origin"]["owner"],node["class"])
  result.cut=cut;result.pid=pid
  return result
 def before(self, wire, cut):
  key,nodes=self.validate(wire)
  require(key in self.taken and self.birth_by_origin[key][0]<cut,"existing operand genuinely born before this method")
  self.cut=cut
  return self.decode(wire,restore=True)

 def member(self, wire):
  require(type(wire) is dict and set(wire)=={"type","graph","member"}
   and wire["type"]=="stock_operand_member" and type(wire["member"]) is int,
   "whole member body must share its actual parent origin")
  key,nodes=self.validate(wire["graph"])
  require(wire["member"] in nodes and nodes[wire["member"]]["kind"]=="selector-key",
   "full actual stock key and data alias node, not a descriptor label")
  self.decode(wire["graph"])
  return self.objects[(key[0],wire["member"])]

_WF = 197
_AF = 196
MAGIC = b"A132"
HEADER_BYTES = 72
PAYLOAD_BYTES = 3072
_C = None
def canonical(value):
 return json.dumps(value, sort_keys=True, ensure_ascii=True,
  allow_nan=False, separators=(",", ":")).encode("ascii")
def id9(info):
 return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
  info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
def value(item):
 if isinstance(item,FrozenArguments):return item.wire
 if OPERANDS.is_stock(item):return OPERANDS.capture(item)
 if OPERANDS.catalog is not None and type(item) is OPERANDS.catalog.key_class:return member_wire(item)
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
class Producer:
 def __init__(self,fd,actor,ends):
  self.fd,self.actor,self.ends=fd,actor,tuple(ends)
  self.failed=False;self.failure=None;self.raw_events=[];self.pending_raw=None
  self.recorder_errors=[];self.recorder_pending=None;self.effects=[];self.pending_effect=None
  self.close_attempted=False;self.close_error=None;self.close_state="OWNED"
  self.emergency_raw=[None]*7;self.first_failed_raw=[None]*7;self.emergency_damage=[None,None]
  self.last_recording_allocation_error=None
  self.pid,self.parent=_RO.getpid(),_RO.getppid()
  self.sequence=self.frames=self.written_bytes=self.attempts=0
  self.terminal_sent=False;self.lock=threading.RLock()
  self._write,self._select=_RO.write,select.select
  self._time,self._mono=_RT.time,_RT.monotonic
  _RO.set_blocking(fd,False)
  self.identity=id9(_RO.fstat(fd))
  self.note("channel.attach",[],{"fd":fd,"pipe_identity":self.identity,
   "parent_pid":self.parent,"actual_pid":self.pid})
 def left(self):
  return min(self.ends[0]-self._time(),self.ends[1]-self._mono())
 def damage(self,raw,error):
  self.failed=True
  if self.failure is None:
   first=self.first_failed_raw
   first[0]=raw[0];first[1]=raw[1];first[2]=raw[2];first[3]=raw[3]
   first[4]=raw[4];first[5]=raw[5];first[6]=raw[6]
   self.emergency_damage[0]=first;self.emergency_damage[1]=error
   self.failure=self.emergency_damage
  self.recorder_pending=self.emergency_damage
  try:
   additional=[raw,error];self.recorder_pending=additional;self.recorder_errors.append(additional)
  except BaseException as later:self.last_recording_allocation_error=later
 def note(self,operation,arguments,result=None,error=None):
  raw_owner=self.emergency_raw
  raw_owner[0]=self.sequence;raw_owner[1]=operation;raw_owner[2]=arguments
  raw_owner[3]=result;raw_owner[4]=error;raw_owner[5]=raw_owner[6]=None
  self.pending_raw=raw_owner
  try:
   with self.lock:
    raw_owner=[self.sequence,operation,arguments,result,error,None,None]
    self.pending_raw=raw_owner
    self.raw_events.append(raw_owner)
    self.sequence+=1
    if self.failed:return
    graph=None if error is None else _xlinks(error)
    row={"schema":"friday.a137.actor-event.v3","actor":self.actor,
     "pid":self.pid,"parent_pid":self.parent,"sequence":raw_owner[0],
     "operation":operation,"arguments":value(arguments),
     "result":value(result) if error is None else None,
     "error":None if error is None else {"type":type(error).__name__,
      "module":type(error).__module__,"errno":getattr(error,"errno",None),
      "args":graph["nodes"][0]["args"],"text":str(error),
      "filename":value(getattr(error,"filename",None)),
      "filename2":value(getattr(error,"filename2",None)),"graph":graph},
     "producer_data_not_Root_authority":True}
    raw=canonical(row);raw_owner[5]=raw
    digest=hashlib.sha256(raw).digest()
    for at in range(0,len(raw),PAYLOAD_BYTES):
     payload=raw[at:at+PAYLOAD_BYTES]
     header=(MAGIC+self.pid.to_bytes(8,"big")+raw_owner[0].to_bytes(8,"big")
      +len(raw).to_bytes(8,"big")+at.to_bytes(8,"big")
      +len(payload).to_bytes(4,"big")+digest)
     if len(header)!=HEADER_BYTES:raise ValueError("a132_transport_header")
     frame=header+payload;raw_owner[6]=[frame,0,None]
     while True:
      left=self.left()
      if left<=0:raise TimeoutError("a132_original_end_transport")
      if not self._select([],[self.fd],[],left)[1]:
       raise TimeoutError("a132_original_end_transport")
      self.attempts+=1
      try:count=self._write(self.fd,frame)
      except BlockingIOError as blocked:
       raw_owner[6][2]=blocked
       continue
      except BaseException as failure:
       raw_owner[6][2]=failure
       raise
      raw_owner[6][1]=count
      self.written_bytes+=count
      if count!=len(frame):raise OSError(errno.EIO,"a132_atomic_frame_incomplete")
      self.frames+=1
      break
  except BaseException as failure:self.damage(self.pending_raw,failure)
 def call(self,operation,function,*args,**kwargs):
  cleanup=operation=="close" or operation.endswith(".close")
  if not cleanup:self.check()
  slot=[operation,args,kwargs,None,None]
  self.pending_effect=slot
  self.effects.append(slot)
  arguments=[args,kwargs]
  end_operation=operation+".end"
  self.note(operation+".begin",arguments)
  if not cleanup:self.check()
  try:result=function(*args,**kwargs)
  except BaseException as error:
   slot[4]=error
   self.note(end_operation,arguments,error=error)
   raise
  slot[3]=result
  self.note(end_operation,arguments,result)
  return result
 def check(self):
  if self.failed:raise RuntimeError("a132_lossless_recording_failed")
 def reference(self):
  return {"schema":"friday.a132.Root-held-actor-events-ref.v1","actor":self.actor,
   "pid":self.pid,"event_count":self.sequence,"frames_sent":self.frames,
   "transport_successful_bytes":self.written_bytes,"transport_attempts":self.attempts,
   "recording_failure":None if self.failure is None else dict(_damage(self.failure[0],self.failure[1],self.last_recording_allocation_error),event_sequence=self.failure[0][0]),
   "producer_complete_claim":not self.failed,
   "independent_Root_confirmation":"REQUIRED_NOT_SUPPLIED_BY_SOURCE"}
 def terminal(self,code):
  self.note("actor.terminal",[],{"native_exit_intent":code,
   "recording_state_before_terminal":self.reference()})
  self.terminal_sent=not self.failed
 def close(self):
  if self.close_attempted:
   if self.close_state!="CLOSED":raise RuntimeError("ambiguous_close_no_retry")
   return
  self.close_attempted=True;self.close_state="CLOSE_ATTEMPTED"
  self.note("actor.final_close.begin",[self.fd])
  try:_RO.close(self.fd)
  except BaseException as error:
   self.close_error=error;self.close_state="AMBIGUOUS_CLOSE"
   self.note("actor.final_close.error",[self.fd],error=error)
   raise
  self.close_state="CLOSED"
_AO=None
_AS="NEVER_ATTEMPTED"
def attach_caller(ends):
 global _C,_AO,_AS
 if _AS!="NEVER_ATTEMPTED":raise RuntimeError("attachment_generation_already_attempted")
 _AS="OWNED_PARTIAL"
 _AO=[None,None,None,[None,None],None,[False,False]]
 _AO[4]=Producer.__new__(Producer)
 fd=None
 try:
  parent=_RO.getppid()
  fd=_RO.open("/proc/%d/fd/%d"%(parent,_WF),_RO.O_WRONLY|_RO.O_CLOEXEC)
  _AO[0]=fd
  info=_RO.fstat(fd)
  if not __import__("stat").S_ISFIFO(info.st_mode):raise ValueError("a132_Root_pipe_required")
  _AO[1]=_RO.dup2(fd,_AF,inheritable=False)
  if fd!=_AF:
   _AO[5][0]=True
   _RO.close(fd)
   _AO[0]=None
  Producer.__init__(_AO[4],_AF,"caller",ends)
  _C=_AO[4];_AS="ATTACHED"
 except BaseException as error:
  _AO[2]=error;_AS="FAILED"
  for index,owned in enumerate(_AO[:2]):
   if owned is not None and not _AO[5][index] and (index==0 or owned!=fd):
    _AO[5][index]=True
    try:
     _RO.close(owned);_AO[index]=None
    except BaseException as close_error:_AO[3][index]=close_error
  raise
 return _C
def attach_inherited(actor,ends):
 global _C,_AO,_AS
 if _AS=="ATTACHED":
  if _C.actor!=actor:raise RuntimeError("a132_actor_ownership_rebinding")
  return _C
 if _AS!="NEVER_ATTEMPTED":raise RuntimeError("attachment_generation_already_attempted")
 _AS="OWNED_PARTIAL"
 _AO=[_AF,None,None,[None,None],None,[False,False]]
 _AO[4]=Producer.__new__(Producer)
 _AO[4].fd=_AF
 try:
  info=_RO.fstat(_AF)
  if not __import__("stat").S_ISFIFO(info.st_mode):raise ValueError("a132_inherited_Root_pipe_required")
  Producer.__init__(_AO[4],_AF,actor,ends)
  _C=_AO[4];_AS="ATTACHED"
 except BaseException as error:
  _AO[2]=error;_AS="FAILED";_AO[5][0]=True
  try:
   _RO.close(_AF);_AO[0]=None
  except BaseException as close_error:_AO[3][0]=close_error
  raise
 return _C
def observed_fds(ordinary):
 if _C is None:
  raise RuntimeError("a132_missing_performing_actor")
 _C.check()
 return tuple(ordinary) + (_AF,)
def note(operation, arguments, result=None, error=None):
 if _C is None:
  raise RuntimeError("a132_unattached_actor_event")
 _C.note(operation, arguments, result, error)
def raw_client():
  if _C is None:
    raise RuntimeError("a132_unattached_actor")
  return _C
