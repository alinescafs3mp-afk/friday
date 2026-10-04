""
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import resource
import selectors
import signal
import stat
import subprocess
import time
from datetime import datetime,timezone
ISSUER_ROOT = "/home/jericho/.jericho/quality-source-admission"
ISSUER = "friday.owner.offline-source-run-admission.v1"
SOURCE = "ASTRA-E4-QUALITY-STABLE-ROOT-NAMESPACE-CLOSURE-A074#1"
ASSIGNMENT = "ASTRA-E4-QUALITY-ROOT-NATIVE-FOCUSED-RUN-A075"
PACKAGE = "/var/tmp/friday-sol061-sol059-a179-sender-whole54-all96-all32-all7-connected-source-closure/Source54"
CONTROLLER = "/var/tmp/friday-sol061-sol059-a179-sender-whole54-all96-all32-all7-connected-source-closure/source/controller.py"
INDEX = "/var/tmp/friday-sol061-sol059-a179-sender-whole54-all96-all32-all7-connected-source-closure/index/full-source.json"
INDEX_SHA = "7e3c4bc1f24e7655dfd1f18798d77cc744fe6874081695aa652b6a0885d1a2db"
ENV3 = {"PATH": "/usr/bin:/bin","LANG": "C","LC_ALL": "C"}
ANCESTRY = (("/",0,0,0o755),("/home",0,0,0o755),
   ("/home/jericho",1000,1000,0o750),
   ("/home/jericho/.jericho",1000,1000,0o700),
   (ISSUER_ROOT,1000,1000,0o700))
RESOURCES = {"worker_address_space_bytes_max": 1610612736,
   "worker_cpu_seconds_max": 300,"worker_wall_timeout_seconds_max": 300,
   "parallel_workers_max": 4,"dispatcher_address_space_bytes_max": 1610612736,
   "dispatcher_cpu_seconds_max": 7200,
   "aggregate_address_space_bytes_max": 8053063680,
   "memory_bill_bytes_max": 8589934592}
LIMITS = {"RLIMIT_AS": (67108864,1610612736),"RLIMIT_CPU": (300,7200),
  "RLIMIT_NOFILE": (256,256),"RLIMIT_CORE": (0,0),
  "RLIMIT_FSIZE": (67108864,67108864)}
SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
READ_MAX,CONTROL_MAX,CONTROLLER_OUT_MAX,ROOT_OUT_MAX = 33554432,131072,32768,262144
HANDOFF_SECONDS = 30
INIT_KEYS = {"assignment","generation","root_thread","phase","controller_sha256",
   "init_deadline_epoch","expectation"}
CONTEXT_KEYS = {"schema","assignment","generation","run_id","accepted_at_utc",
    "deadline_at_utc","wall_seconds","seal_reserve_seconds","resources","allowed_modes"}
EXPECTATION_KEYS = {"schema","issuer","source_assignment","admission_sha256",
     "admission_identity","namespace_identities","run_context","source_hashes"}
BODY_KEYS = {"schema","issuer","source_assignment","source_hashes","run_context"}
TASK_KEYS = {"schema","issuer","source_assignment","package_root","source_hashes",
   "run_context","expectation_sha256","expectation_identity",
   "held_admission_identity","entrypoints"}
CLAIM_KEYS = {"status","assignment","generation","holder_pid","holder_start_ticks",
   "fds","identities","expectation_sha256","admission_sha256","admission_identity",
   "namespace_identities","controller_sha256","source54","init_deadline_epoch"}
STAGE1_KEYS = {"schema","phase","assignment","generation","root_thread","origin",
   "controller","sender","package_root","index","source54_identities",
   "source_directories","expectation","dispatcher_label","auth_deadline_epoch"}
STAGE2_KEYS = {"schema","phase","assignment","generation","root_thread","stage1_sha256",
   "holder_pid","holder_start_ticks","task","expected_task_sha256"}
class Refusal(Exception):
 pass
class OwnedPopen(subprocess.Popen):
 def __del__(self):
  pass
def require(ok,cause):
 fault=None if ok else Refusal(cause)
 pending=TRACE.predicate_pending
 try:
  pending["first_error"]=fault
  pending.update(ok=ok,cause=cause,site=None,locals=None,recording_error=None)
  frame=_predicate_frame(1);TRACE.predicate_state(frame)
  site=[frame.f_code.co_qualname,frame.f_lineno]
  observed={key:item for key,item in frame.f_locals.items() if key!="self"
   and type(item) in (type(None),str,bool,int,float,bytes,list,tuple,dict,set,frozenset)}
  pending["site"],pending["locals"]=site,observed
  TRACE.note("sender","grammar.predicate",[site,observed],ok)
 except BaseException as recorder:
  pending["recording_error"]=recorder;TRACE.failed=True
  if fault is not None:raise fault
  raise
 if not ok:
  TRACE.note("sender","guard.refused",[cause],False)
  raise fault
def exact(value,keys):
 require(type(value) is dict and set(value) == keys,"exact_key_set")
def integer(value,low=0,high=None):
 return type(value) is int and value >= low and (high is None or value <= high)
def hex64(value):
 return type(value) is str and re.fullmatch(r"[0-9a-f]{64}",value) is not None
def identity(info):
 return [info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,
   info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns]
def id9(value):
 return type(value) is list and len(value) == 9 and all(integer(v) for v in value)
def digest(raw):
 return hashlib.sha256(raw).hexdigest()
def control_types(value):
 if type(value) is dict:
  require(all(type(key) is str for key in value),"exact_key_set")
  for item in value.values():
   control_types(item)
 elif type(value) is list:
  for item in value:
   control_types(item)
 else:
  require(type(value) in (str,int,bool) or value is None,"control_type")
def encode(value,telemetry=False):
 if not telemetry:
  control_types(value)
 return (json.dumps(value,sort_keys=True,ensure_ascii=True,allow_nan=False,
     separators=(",",":")) + "\n").encode("ascii")
def object_wire(raw,telemetry=False,canonical=True,maximum=CONTROL_MAX):
 def pairs(rows):
  value = {}
  for key,item in rows:
   require(key not in value,"duplicate wire key")
   value[key] = item
  return value
 def no_constant(_token):
  raise Refusal("nonfinite wire")
 def no_float(_token):
  raise Refusal("control_type")
 require(type(raw) is bytes and 0 < len(raw) <= maximum,"wire_bound")
 value = json.loads(raw,object_pairs_hook=pairs,parse_constant=no_constant,
     parse_float=float if telemetry else no_float)
 require(type(value) is dict,"exact_key_set")
 if canonical:
  require(encode(value,telemetry=telemetry) == raw,"canonical object wire")
 return value
def source_map(value):
 require(type(value) is dict and len(value) == 54,"source54")
 for name,pin in value.items():
  require(type(name) is str and name and not name.startswith("/")
    and all(part not in ("",".","..") for part in name.split("/"))
    and hex64(pin),"source54")
def namespace_shape(rows):
 require(type(rows) is list and len(rows) == 5,"ancestry_spec_mismatch")
 for row,(path,uid,gid,mode) in zip(rows,ANCESTRY):
  exact(row,{"path","identity"})
  value = row["identity"]
  require(type(row["path"]) is str and row["path"] == path and id9(value)
    and stat.S_ISDIR(value[2]) and stat.S_IMODE(value[2]) == mode
    and value[3:5] == [uid,gid],"ancestry_spec_mismatch")
def body_shape(body):
 exact(body,BODY_KEYS)
 require(type(body["schema"]) is str and body["schema"] == "friday.a049.offline-run-admission.v1"
   and type(body["issuer"]) is str and body["issuer"] == ISSUER
   and type(body["source_assignment"]) is str and body["source_assignment"] == SOURCE,
   "issuer_body")
 source_map(body["source_hashes"])
 context_shape(body["run_context"])
def custody_shape(custody):
 exact(custody,{"schema","expectation_sha256","expectation_identity","held_admission_identity",
     "admission_identity","namespace_identities","transport"})
 require(custody["schema"] == "friday.a056.independent-parent-custody.v1"
   and hex64(custody["expectation_sha256"])
   and custody["transport"] == "INDEPENDENT_PARENT_READONLY_FULLY_SEALED_HELD_OBJECTS",
   "controller_report")
 for key,nlink,maximum in (("expectation_identity",0,131072),
       ("held_admission_identity",0,65536),
       ("admission_identity",1,65536)):
  value = custody[key]
  require(id9(value) and stat.S_ISREG(value[2]) and stat.S_IMODE(value[2]) == 0o400
    and value[3:6] == [1000,1000,nlink] and 0 < value[6] <= maximum,
    "controller_report")
 namespace_shape(custody["namespace_identities"])
def context_shape(context):
 exact(context,CONTEXT_KEYS)
 require(context["schema"] == "friday.a049.offline-run-context.v1"
   and context["assignment"] == ASSIGNMENT + "#1"
   and integer(context["generation"],1,1),"phase_or_role")
 require(hex64(context["run_id"]),"pin_mismatch")
 exact(context["resources"],set(RESOURCES))
 require(all(type(context["resources"][key]) is int and context["resources"][key] == value
    for key,value in RESOURCES.items()),"resources_topology_changed")
 require(type(context["allowed_modes"]) is list
   and context["allowed_modes"] == ["affected","collect","selfcheck"],"tests_lowered")
 require(integer(context["seal_reserve_seconds"],600,600)
   and integer(context["wall_seconds"],601,7200),"fixed_deadline")
 stamps = []
 for key in ("accepted_at_utc","deadline_at_utc"):
  require(type(context[key]) is str,"fixed_deadline")
  parsed = datetime.fromisoformat(context[key])
  require(parsed.tzinfo is not None and parsed.astimezone(timezone.utc).isoformat() == context[key],
    "fixed_deadline")
  stamps.append(parsed.timestamp())
 require(stamps[1] - stamps[0] == context["wall_seconds"],"fixed_deadline")
 return stamps
def expectation_shape(expected):
 exact(expected,EXPECTATION_KEYS)
 require(expected["schema"] == "friday.a056.independent-parent-custody.v1"
   and expected["issuer"] == ISSUER and expected["source_assignment"] == SOURCE,"phase_or_role")
 require(hex64(expected["admission_sha256"]),"pin_mismatch")
 ident = expected["admission_identity"]
 require(id9(ident) and stat.S_ISREG(ident[2]) and stat.S_IMODE(ident[2]) == 0o400
   and ident[3:6] == [1000,1000,1] and 0 < ident[6] <= 65536,"issuer_body")
 namespace_shape(expected["namespace_identities"])
 source_map(expected["source_hashes"])
 return context_shape(expected["run_context"])
def validate_stage1(value):
 exact(value,STAGE1_KEYS)
 require(value["schema"] == "friday.a081.root-native-stage1.v1" and value["phase"] == "PREPARE"
   and value["assignment"] == ASSIGNMENT and integer(value["generation"],1,1),"phase_or_role")
 require(value["origin"] == "external-genuine-root-native-exec-tool"
   and type(value["root_thread"]) is str and len(value["root_thread"]) <= 128
   and value["root_thread"] != "UNISSUED" and value["root_thread"],"external_origin_precondition")
 exact(value["controller"],{"path","sha256","identity","accepted_epoch"})
 exact(value["index"],{"path","sha256","identity"})
 require(value["controller"]["path"] == CONTROLLER and hex64(value["controller"]["sha256"])
   and id9(value["controller"]["identity"]) and integer(value["controller"]["accepted_epoch"],1)
   and value["index"]["path"] == INDEX and value["index"]["sha256"] == INDEX_SHA
   and id9(value["index"]["identity"]) and value["package_root"] == PACKAGE,"pin_mismatch")
 exact(value["sender"],{"root","files"})
 root = value["sender"]["root"]
 require(type(root) is str and root.startswith("/") and os.path.normpath(root) == root
   and not root.startswith(ISSUER_ROOT + "/") and root != ISSUER_ROOT,"caller_source_selection")
 exact(value["sender"]["files"],{"caller.py","staged_sender.py"})
 for pin in value["sender"]["files"].values():
  exact(pin,{"sha256","identity"})
  require(hex64(pin["sha256"]) and id9(pin["identity"]),"caller_source_selection")
 begin,end = expectation_shape(value["expectation"])
 pins = value["expectation"]["source_hashes"]
 require(type(value["source54_identities"]) is dict and set(value["source54_identities"]) == set(pins)
   and all(id9(v) for v in value["source54_identities"].values()),"source54")
 dirs = {PACKAGE,root}
 for name in pins:
  parts = name.split("/")
  dirs.update(PACKAGE + "/" + "/".join(parts[:n]) for n in range(1,len(parts)))
 require(type(value["source_directories"]) is dict and set(value["source_directories"]) == dirs
   and all(id9(v) for v in value["source_directories"].values()),"source54")
 require(type(value["dispatcher_label"]) is str
   and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}",value["dispatcher_label"]) is not None,
   "phase_or_role")
 require(integer(value["auth_deadline_epoch"],1)
   and begin < value["auth_deadline_epoch"] <= min(end - 600,
    value["controller"]["accepted_epoch"] + 6600)
   and end <= value["controller"]["accepted_epoch"] + 7200,"fixed_deadline")
 return begin,end
def validate_init(value,stage1):
 exact(value,INIT_KEYS)
 expectation_shape(value["expectation"])
 require(value["phase"] == "INIT","phase_or_role")
 require(value["assignment"] == stage1["assignment"] and integer(value["generation"],1,1)
   and value["root_thread"] == stage1["root_thread"]
   and value["controller_sha256"] == stage1["controller"]["sha256"]
   and type(value["init_deadline_epoch"]) is int
   and value["init_deadline_epoch"] == stage1["auth_deadline_epoch"]
   and value["expectation"] == stage1["expectation"],"claim_binding")
def validate_claim(value,stage1,pid,ticks):
 exact(value,CLAIM_KEYS)
 require(value["status"] == "CUSTODY_READY" and value["assignment"] == stage1["assignment"],"phase_or_role")
 require(integer(value["generation"],1,1) and integer(value["holder_pid"],1)
   and integer(value["holder_start_ticks"],1) and value["holder_pid"] == pid
   and value["holder_start_ticks"] == ticks,"pid_generation")
 require(type(value["fds"]) is list and all(type(v) is int for v in value["fds"])
   and value["fds"] == [198,199] and type(value["identities"]) is list
   and len(value["identities"]) == 2 and all(id9(v) for v in value["identities"]),"stale_identity")
 expected = stage1["expectation"]
 namespace_shape(value["namespace_identities"])
 source_map(value["source54"])
 require(hex64(value["expectation_sha256"]) and value["expectation_sha256"] == digest(encode(expected))
   and hex64(value["admission_sha256"]) and value["admission_sha256"] == expected["admission_sha256"]
   and id9(value["admission_identity"]) and value["admission_identity"] == expected["admission_identity"]
   and value["namespace_identities"] == expected["namespace_identities"]
   and value["controller_sha256"] == stage1["controller"]["sha256"]
   and value["source54"] == expected["source_hashes"]
   and type(value["init_deadline_epoch"]) is int
   and value["init_deadline_epoch"] == stage1["auth_deadline_epoch"],"claim_binding")
 expectation_shape({**expected,"namespace_identities": value["namespace_identities"]})
 source_map(value["source54"])
def check_held_observation(value,raw,expected_identity=None):
 exact(value,{"identity","original_flags","opened_flags","opened_cloexec","seals","sha256"})
 require(integer(value["original_flags"]) and integer(value["opened_flags"])
   and value["original_flags"] & os.O_ACCMODE == os.O_RDONLY
   and value["original_flags"] & os.O_CLOEXEC == os.O_CLOEXEC
   and value["opened_flags"] & os.O_ACCMODE == os.O_RDONLY
   and type(value["opened_cloexec"]) is bool and value["opened_cloexec"] is True,
   "fd_not_readonly")
 require(integer(value["seals"]) and value["seals"] & SEALS == SEALS,"seals_incomplete")
 ident = value["identity"]
 require(id9(ident) and stat.S_ISREG(ident[2]) and stat.S_IMODE(ident[2]) == 0o400
   and ident[3:6] == [1000,1000,0] and ident[6] == len(raw),"stale_identity")
 require(hex64(value["sha256"]) and value["sha256"] == digest(raw),"pin_mismatch")
 if expected_identity is not None:
  require(ident == expected_identity,"stale_identity")
def validate_stage2(value,stage1,stage1_sha,pid,ticks,held):
 exact(value,STAGE2_KEYS)
 require(value["schema"] == "friday.a081.root-native-stage2.v1" and value["phase"] == "AUTHORIZE"
   and value["assignment"] == stage1["assignment"] and integer(value["generation"],1,1)
   and value["root_thread"] == stage1["root_thread"] and value["stage1_sha256"] == stage1_sha,
   "phase_or_role")
 require(integer(value["holder_pid"],1) and integer(value["holder_start_ticks"],1)
   and value["holder_pid"] == pid and value["holder_start_ticks"] == ticks,"pid_generation")
 task = value["task"]
 exact(task,TASK_KEYS)
 require(hex64(value["expected_task_sha256"])
   and digest(encode(task)) == value["expected_task_sha256"],"pin_mismatch")
 expected = stage1["expectation"]
 source_map(task["source_hashes"])
 context_shape(task["run_context"])
 require(task["schema"] == "friday.a056.independent-parent-run-task.v1"
   and task["issuer"] == ISSUER and task["source_assignment"] == SOURCE
   and task["package_root"] == PACKAGE,"phase_or_role")
 require(task["source_hashes"] == expected["source_hashes"] and task["run_context"] == expected["run_context"]
   and task["expectation_sha256"] == held[0]["sha256"],"pin_mismatch")
 source_map(task["source_hashes"])
 context_shape(task["run_context"])
 require(id9(task["expectation_identity"]) and id9(task["held_admission_identity"])
   and task["expectation_identity"] == held[0]["identity"]
   and task["held_admission_identity"] == held[1]["identity"],"stale_identity")
 require(type(task["entrypoints"]) is list and len(task["entrypoints"]) == 1,"exact_key_set")
 step = task["entrypoints"][0]
 exact(step,{"entrypoint","arguments"})
 require(step["entrypoint"] == "dispatcher" and type(step["arguments"]) is list
   and all(type(v) is str for v in step["arguments"])
   and step["arguments"] == ["--label",stage1["dispatcher_label"],"--focused-checks"],"phase_or_role")
 return {"assignment": stage1["assignment"],"generation": 1,"root_thread": stage1["root_thread"],
   "phase": "AUTH","task": task,"expected_task_sha256": value["expected_task_sha256"]}
class Clock:
 def __init__(self,stage1,wall0,mono0):
  self.wall0,self.mono0 = wall0,mono0
  begin,end = validate_stage1(stage1)
  self.auth = stage1["auth_deadline_epoch"]
  self.init = min(wall0 + 30,self.auth,end - 600)
  self.run,self.cleanup,self.reap_end = end - 600,end,end - HANDOFF_SECONDS
  require(begin <= self.now() < self.run and self.now() < self.auth <= wall0 + 300,"fixed_deadline")
 def now(self):
  wall = time.time()
  require(abs(wall - self.wall0 - (time.monotonic() - self.mono0)) <= 2,"clock drift")
  return wall
 def left(self,deadline):
  return min(deadline - self.now(),self.mono0 + deadline - self.wall0 - time.monotonic())
 def cleanup_left(self):
  return self.mono0 + self.reap_end - self.wall0 - time.monotonic()
 def handoff_left(self):
  return min(self.cleanup - time.time(),
    self.mono0 + self.cleanup - self.wall0 - time.monotonic())
class Named:
 def __init__(self,path,pinned_identity,pin,mode,maximum,budget):
  before = os.lstat(path)
  require(id9(pinned_identity) and identity(before) == pinned_identity,"stale_identity")
  require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == mode
    and (before.st_uid,before.st_gid,before.st_nlink) == (1000,1000,1)
    and 0 < before.st_size <= maximum,"issuer_body")
  self.path,self.identity,self.pin,self.budget = path,pinned_identity,pin,budget
  self.fd = None
  self.fd = self._owner.acquire("Named.fd",os.open,path,os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
  try:
   self.raw = self.check()
  except BaseException as error:
   self._owner.fail(error)
   try:self.close()
   except BaseException:pass
   raise
 def check(self):
  require(identity(os.lstat(self.path)) == self.identity and identity(os.fstat(self.fd)) == self.identity,
    "stale_identity")
  raw = self.budget.read(self.fd,self.identity[6] + 1,0,kind="named:" + self.path)
  require(len(raw) == self.identity[6] and identity(os.fstat(self.fd)) == self.identity
    and identity(os.lstat(self.path)) == self.identity,"stale_identity")
  require(digest(raw) == self.pin,"pin_mismatch")
  return raw
 def close(self):
  if self.fd is not None:
   self._owner.close(self.fd,os.close)
   self.fd = None
class Directory:
 def __init__(self,path,expected,uid=1000,gid=1000,mode=0o700):
  self.path,self.identity = path,expected
  self.fd = None
  info = os.lstat(path)
  require(id9(expected) and identity(info) == expected and stat.S_ISDIR(info.st_mode)
    and stat.S_IMODE(info.st_mode) == mode and (info.st_uid,info.st_gid) == (uid,gid),
    "stale_identity")
  self.fd = self._owner.acquire("Directory.fd",os.open,path,os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
  try:
   self.check()
  except BaseException as error:
   self._owner.fail(error)
   try:self.close()
   except BaseException:pass
   raise
 def check(self):
  require(identity(os.lstat(self.path)) == self.identity and identity(os.fstat(self.fd)) == self.identity,
    "stale_identity")
 def close(self):
  if self.fd is not None:
   self._owner.close(self.fd,os.close)
   self.fd = None
class Generation:
 def __init__(self,pid,parent,budget,allow_exited=False):
  self.pid,self.parent,self.budget = pid,parent,budget
  self.pidfd,self.procfd = None,None
  self.terminal_children,self.reaped,self.wait_status,self.wait_usage = None,False,None,None
  try:
   self.pidfd = self._owner.acquire("Generation.pidfd",os.pidfd_open,pid,0)
   self.procfd = self._owner.acquire("Generation.procfd",os.open,"/proc/%d" % pid,os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
   self.ticks = self.stat()[1]
   self.check(allow_exited=allow_exited)
  except BaseException as error:
   self._owner.fail(error)
   try:self.close()
   except BaseException:pass
   raise
 def proc_read(self,relative,cap):
  fd = self._owner.acquire("temporary",os.open,relative,os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,dir_fd=self.procfd)
  try:
   raw = self.budget.read(fd,cap + 1,kind="proc:" + relative)
   require(len(raw) <= cap,"proc_bound")
   return raw
  finally:
   self._owner.close(fd,os.close)
 def stat(self):
  raw = self.proc_read("stat",4096).decode("ascii")
  head,tail = raw.rsplit(")",1)
  values = tail.split()
  require(int(head.split("(",1)[0]) == self.pid and int(values[1]) == self.parent,"pid_generation")
  return values[0],int(values[19])
 def exited(self):
  with selectors.DefaultSelector() as selector:
   selector.register(self.pidfd,selectors.EVENT_READ)
   return bool(selector.select(0))
 def check(self,allow_exited=False):
  require((allow_exited or not self.exited()) and self.stat()[1] == self.ticks,"pid_generation")
  fd = self._owner.acquire("temporary",os.open,"/proc/self/fdinfo/%d" % self.pidfd,os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
  try:
   raw = self.budget.read(fd,4097,kind="proc:self_fdinfo")
   require(len(raw) <= 4096,"proc_bound")
   rows = [v.split()[1] for v in raw.decode("ascii").splitlines() if v.startswith("Pid:")]
   require(rows == [str(self.pid)],"pid_generation")
  finally:
   self._owner.close(fd,os.close)
 def children(self,terminal=False):
  self.check(allow_exited=terminal)
  tasksfd = self._owner.acquire("temporary",os.open,"task",os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,dir_fd=self.procfd)
  try:
   tasks = self.budget.strings(os.listdir(tasksfd),4096,kind="proc:task_names")
   require(len(tasks) <= 32 and all(v.isdigit() for v in tasks),"own_tree_bound")
   children = set()
   for tid in tasks:
    raw = self.proc_read("task/" + tid + "/children",4096)
    values = raw.decode("ascii").split()
    require(all(v.isdigit() for v in values),"own_tree_visibility")
    children.update(int(v) for v in values)
   require(len(children) <= 16,"own_tree_bound")
   self.check(allow_exited=terminal)
   if terminal:
    require(self.exited() and self.stat()[0] in ("Z","X") and not children,
      "terminal_children_unconfirmed")
    self.terminal_children = []
   return children
  finally:
   self._owner.close(tasksfd,os.close)
 def close(self):
  first=None
  for name in ("procfd","pidfd"):
   fd = getattr(self,name,None)
   if fd is not None:
    try:
     self._owner.close(fd,os.close)
     setattr(self,name,None)
    except BaseException as error:
     if first is None:first=error
  if first is not None:raise first
class OwnScope:
 """Isolated owning subreaper; terminal wait4/list proof,sticky UNKNOWN."""
 def __init__(self,budget,runtime):
  self.budget,self.runtime,self.fd = budget,runtime,None
  self.lib = self._owner.acquire("OwnScope.library",ctypes.CDLL,None,use_errno=True)
  self.lib.prctl.restype = ctypes.c_int
  self.lib.prctl.argtypes = [ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
  self.initial_empty,self.terminal_empty,self.creation_started = False,False,False
  self.snapshots = []
  self.runtime_check()
  require(signal.SIGCHLD not in signal.pthread_sigmask(signal.SIG_BLOCK,[]),"SIGCHLD_blocked")
  signal.signal(signal.SIGCHLD,signal.SIG_DFL)
  require(signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,"own_reaper_policy")
  self.fd = self._owner.acquire("OwnScope.procfd",os.open,"/proc/self",os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
  try:
   self.single_thread()
   require(not self.children(),"initial_own_scope_not_empty")
   self.initial_empty = True
   self.pr(36,1)
   self.check()
  except BaseException as error:
   self._owner.fail(error)
   try:self.close()
   except BaseException:pass
   raise
 def pr(self,op,value=None):
  observed = ctypes.c_int(0) if op == 37 else None
  argument = ctypes.addressof(observed) if observed is not None else value
  slot=self._owner.reserve("prctl",[op,argument,observed]);TRACE.check()
  returned = self.lib.prctl(op,argument,0,0,0)
  slot["raw"]=returned
  failure_errno = ctypes.get_errno() if returned < 0 else None
  fault=OSError(failure_errno,"own_subreaper") if returned<0 else None
  slot["error"]=fault
  output = observed.value if observed is not None and returned >= 0 else None
  TRACE.note("sender","ownscope.prctl",[op],
      {"returned": returned,"error_errno": failure_errno,"output_value": output})
  if fault is not None:raise fault
  TRACE.check()
  return output
 def proc_read(self,name,maximum=4096):
  fd = self._owner.acquire("temporary",os.open,name,os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,dir_fd=self.fd)
  try:
   raw = self.budget.read(fd,maximum + 1,kind="proc:" + name)
   require(len(raw) <= maximum,"proc_bound")
   return raw.decode("ascii")
  finally:
   self._owner.close(fd,os.close)
 def single_thread(self):
  rows = dict(v.split(":",1) for v in self.proc_read("status").splitlines() if ":" in v)
  require(rows.get("Threads","").strip() == "1" and rows.get("Pid","").strip() == str(os.getpid()),
    "own_scope_single_thread")
 def runtime_check(self):
  r = self.runtime
  require(identity(os.fstat(r["fd"])) == r["identity"]
    and fcntl.fcntl(r["fd"],fcntl.F_GETFD) == fcntl.FD_CLOEXEC
    and fcntl.fcntl(r["fd"],fcntl.F_GETFL) == r["flags"],"stock_runtime_fd_drift")
  link = os.readlink("/proc/self/fd/%d" % r["fd"])
  self.budget.strings([link],4096,kind="proc:fd_link")
  require(link == r["path"],"stock_runtime_fd_drift")
 def check(self):
  self.single_thread()
  self.runtime_check()
  value = self.pr(37)
  require(value == 1 and signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL
    and signal.SIGCHLD not in signal.pthread_sigmask(signal.SIG_BLOCK,[]),"own_reaper_policy")
 def children(self):
  self.single_thread()
  values = self.proc_read("task/%d/children" % os.getpid()).split()
  require(len(values) <= 16 and all(v.isascii() and v.isdecimal() and int(v) > 0 for v in values),
    "own_tree_bound")
  return {int(v) for v in values}
 def terminal(self,generations):
  self.check()
  before = self.children()
  require(not before and all(gen.reaped and gen.terminal_children == [] for gen in generations.values()),
    "own_tree_terminal_unconfirmed")
  self.check()
  after = self.children()
  require(not after and self.initial_empty and self.creation_started,"own_tree_terminal_unconfirmed")
  self.terminal_empty = True
  self.snapshots = [{"boundary": "terminal_before","children": sorted(before)},
      {"boundary": "terminal_after","children": sorted(after)}]
 def close(self):
  if self.fd is not None:
   self._owner.close(self.fd,os.close)
   self.fd = None
class Held:
 def __init__(self,generation,target,maximum,raw,budget):
  self.generation,self.target,self.maximum = generation,target,maximum
  self.raw,self.budget,self.fd = raw,budget,None
  generation.check()
  self.original_flags = self.flags()
  require(self.original_flags & os.O_ACCMODE == os.O_RDONLY
    and self.original_flags & os.O_CLOEXEC == os.O_CLOEXEC,"fd_not_readonly")
  self.fd = self._owner.acquire("Held.fd",os.open,"fd/%d" % target,os.O_RDONLY | os.O_CLOEXEC,dir_fd=generation.procfd)
  try:
   self.observation = self.check()
  except BaseException as error:
   self._owner.fail(error)
   try:self.close()
   except BaseException:pass
   raise
 def flags(self):
  raw = self.generation.proc_read("fdinfo/%d" % self.target,4096).decode("ascii")
  rows = [line.split()[1] for line in raw.splitlines() if line.startswith("flags:")]
  require(len(rows) == 1,"fd_not_readonly")
  return int(rows[0],8)
 def check(self,fixed=None):
  self.generation.check()
  flags = self.flags()
  require(flags == self.original_flags,"fd_not_readonly")
  before = identity(os.stat("fd/%d" % self.target,dir_fd=self.generation.procfd,follow_symlinks=True))
  opened = identity(os.fstat(self.fd))
  require(before == opened and 0 < opened[6] <= self.maximum,"stale_identity")
  current = self._owner.acquire("temporary",os.open,"fd/%d" % self.target,os.O_RDONLY | os.O_CLOEXEC,dir_fd=self.generation.procfd)
  try:
   require(identity(os.fstat(current)) == opened,"stale_identity")
   raw = self.budget.read(self.fd,opened[6] + 1,0,kind="held:original")
   current_raw = self.budget.read(current,opened[6] + 1,0,kind="held:current")
   observation = {"identity": opened,"original_flags": flags,
    "opened_flags": fcntl.fcntl(self.fd,fcntl.F_GETFL),
    "opened_cloexec": bool(fcntl.fcntl(self.fd,fcntl.F_GETFD) & fcntl.FD_CLOEXEC),
    "seals": fcntl.fcntl(self.fd,fcntl.F_GET_SEALS),"sha256": digest(raw)}
   check_held_observation(observation,self.raw,None if fixed is None else fixed["identity"])
   require(fcntl.fcntl(current,fcntl.F_GET_SEALS) & SEALS == SEALS
     and fcntl.fcntl(current,fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
     and raw == current_raw == self.raw and len(raw) == opened[6]
     and identity(os.fstat(self.fd)) == opened and identity(os.fstat(current)) == opened
     and identity(os.stat("fd/%d" % self.target,dir_fd=self.generation.procfd)) == opened
     and self.flags() == flags,"stale_identity")
   if fixed is not None:
    require(observation == fixed,"stale_identity")
   self.generation.check()
   return observation
  finally:
   self._owner.close(current,os.close)
 def close(self):
  if self.fd is not None:
   self._owner.close(self.fd,os.close)
   self.fd = None
class IO:
 def __init__(self,clock,budget):
  self.clock,self.budget = clock,budget
  self.buffers,self.eof,self.root_output = {},set(),0
  self.raw_outputs,self.overflow_samples,self.capture_stopped = {},{},set()
  self.capture_errors = {}
  self.root_requests = []
  self.controlled_lines = []
  self.controlled_offsets = {}
  self.original_fd_map = None
  self.producer_stdio = {"dispatcher_stdout": "INHERITED_CONTROLLER_STDOUT",
       "dispatcher_stderr": "INHERITED_CONTROLLER_STDERR",
       "worker_stdout": "EXCLUSIVE_REGULAR_FILE",
       "worker_stderr": "EXCLUSIVE_REGULAR_FILE",
       "observed_on_controller_pipes": False,"separate_dispatcher_bytes": "UNKNOWN_NOT_ZERO",
       "filler_is_producer_output": False,
       "silent_zero": False}
  self.proc,self.sample = None,lambda: None
  self.delivery_error=None
  for fd in (0,1):
   os.set_blocking(fd,False)
 def attach(self,proc,sample):
  self.proc,self.sample = proc,sample
  self.original_fd_map = {"stdout": proc.stdout.fileno(),"stderr": proc.stderr.fileno()}
  self.producer_stdio["observed_on_controller_pipes"] = True
  self.raw_outputs = {proc.stdout.fileno(): bytearray(),proc.stderr.fileno(): bytearray()}
  for pipe in (proc.stdin,proc.stdout,proc.stderr):
   os.set_blocking(pipe.fileno(),False)
 def detach(self):
  self.proc,self.sample = None,lambda: None
  self.buffers.clear()
 def terminal(self,raw):
  TRACE.note("sender","native.before_delivery",[],raw)
  result = {"confirmed": False,"bytes_written": 0,"complete_line": False,
    "original_channel": True,"no_endpoint_refresh": True,
    "original_wall_end": self.clock.cleanup,
    "original_monotonic_end": self.clock.mono0 + self.clock.cleanup - self.clock.wall0}
  try:
   require(type(raw) is bytes and 0 < len(raw) <= ROOT_OUT_MAX
     and self.root_output + len(raw) <= ROOT_OUT_MAX,"root_output_bound")
   result["attempted_bytes"] = len(raw)
   with selectors.DefaultSelector() as selector:
    selector.register(1,selectors.EVENT_WRITE)
    while result["bytes_written"] < len(raw):
     left = self.clock.handoff_left()
     require(left > 0,"failed_handoff_deadline")
     if not selector.select(min(left,0.25)):
      continue
     require(self.clock.handoff_left() > 0,"failed_handoff_deadline")
     try:
      at = result["bytes_written"]
      count = os.write(1,raw[at:at + 4096])
     except BlockingIOError as error:
      TRACE.note("sender","terminal.write",[1,at,raw[at:at + 4096]],error=error)
      continue
     except BaseException as error:
      TRACE.note("sender","terminal.write",[1,at,raw[at:at + 4096]],error=error)
      raise
     require(count > 0,"failed_handoff_write")
     result["bytes_written"] += count
     self.root_output += count
     TRACE.note("sender","terminal.write",[1,at,raw[at:at + 4096]],count)
   wall,mono = time.time(),time.monotonic()
   result.update(complete_line=True,completed_wall=wall,completed_monotonic=mono)
   require(wall < result["original_wall_end"] and mono < result["original_monotonic_end"],
     "failed_handoff_deadline")
   result["confirmed"] = True
  except BaseException as error:
   self.delivery_error=error
   result.update(error_type=type(error).__name__,
      error_errno=getattr(error,"errno",None),
      reason=str(error)[:512] if isinstance(error,Refusal) else type(error).__name__)
   result["complete_line"] = result.get("attempted_bytes") == result["bytes_written"]
  return result
 def pump(self,target,event,deadline):
  left = self.clock.left(deadline)
  require(left > 0,"fixed_deadline")
  with selectors.DefaultSelector() as selector:
   interests = {target: event}
   for fd in self.raw_outputs:
    if fd not in self.eof and fd not in self.capture_stopped:
     interests[fd] = interests.get(fd,0) | selectors.EVENT_READ
   for fd,mask in interests.items():
    if fd not in self.eof and fd not in self.capture_stopped:
     selector.register(fd,mask)
   events = selector.select(min(left,0.25))
   for key,mask in events:
    fd = key.fd
    if fd in self.raw_outputs and mask & selectors.EVENT_READ:
     cap = CONTROLLER_OUT_MAX - sum(len(v) for v in self.raw_outputs.values())
     try:
      part = self.budget.read(fd,min(8192,cap + 1),kind="controller_pipe")
     except BlockingIOError:
      continue
     except (OSError,Refusal) as error:
      self.capture_stopped.add(fd)
      self.capture_errors[str(fd)] = {"error_type": type(error).__name__,
          "error_errno": getattr(error,"errno",None),
          "cause": str(error)[:512] if isinstance(error,Refusal) else type(error).__name__}
      raise
     if len(part) > cap:
      self.raw_outputs[fd].extend(part[:cap])
      self.overflow_samples[str(fd)] = part[cap:].hex()
      self.capture_stopped.add(fd)
      raise Refusal("controller_output_bound")
     if part:
      self.raw_outputs[fd].extend(part)
      self.buffers.setdefault(fd,bytearray()).extend(part)
     else:
      self.eof.add(fd)
  self.sample()
  return any(key.fd == target and mask & event for key,mask in events)
 def write(self,fd,raw,deadline,root=False,before_write=None):
  require(type(raw) is bytes and 0 < len(raw) <= (ROOT_OUT_MAX if root else CONTROL_MAX),"wire_bound")
  if root:
   require(self.root_output + len(raw) <= ROOT_OUT_MAX,"root_output_bound")
   self.root_requests.append({"wire_hex": raw.hex(),"wire_bytes": len(raw),
                              "sha256": digest(raw),"successful_prefix_bytes": 0})
  at = 0
  while at < len(raw):
   if not self.pump(fd,selectors.EVENT_WRITE,deadline):
    continue
   if before_write is not None:
    before_write()
   try:
    count = os.write(fd,raw[at:at + 4096])
   except BlockingIOError as error:
    TRACE.note("sender","write",[fd,at,raw[at:at + 4096]],error=error)
    continue
   except BaseException as error:
    TRACE.note("sender","write",[fd,at,raw[at:at + 4096]],error=error)
    raise
   require(count > 0,"pipe_write")
   at += count
   if root:
    self.root_output += count
    self.root_requests[-1]["successful_prefix_bytes"] += count
   TRACE.note("sender","write",[fd,at - count,raw[at - count:at - count + 4096]],count)
 def line(self,fd,deadline,maximum,role="controlled_line"):
  buffer = self.buffers.setdefault(fd,bytearray())
  while b"\n" not in buffer:
   require(len(buffer) <= maximum and fd not in self.eof,"bounded control or EOF")
   if not self.pump(fd,selectors.EVENT_READ,deadline):
    continue
   if fd not in self.raw_outputs:
    try:
     part = self.budget.read(fd,1,kind="inputwire_fd0")
    except BlockingIOError:
     continue
    if not part:
     self.eof.add(fd)
    buffer.extend(part)
  index = buffer.index(10) + 1
  require(index <= maximum,"wire_bound")
  result = bytes(buffer[:index])
  self.controlled_lines.append({"fd": fd,"bytes": len(result),"sha256": digest(result),
         "role": role,"producer": False,
         "offset": self.controlled_offsets.get(fd,0),
         "controller_pipe": fd in self.raw_outputs})
  self.controlled_offsets[fd] = self.controlled_offsets.get(fd,0) + len(result)
  del buffer[:index]
  return result
def controller_limits():
 for name,expected in LIMITS.items():
  resource.setrlimit(getattr(resource,name),expected)
def usage_wire(value):
 return list(value)
def validate_report(value,stage2,stage1):
 require(type(value) is dict,"controller_report")
 if value.get("status") == "REFUSED_OR_FAILED":
  exact(value,{"status","error_type","reason","controller_read_bytes","release_credit"})
  require(type(value["error_type"]) is str and type(value["reason"]) is str
    and len(value["reason"]) <= 512 and integer(value["controller_read_bytes"])
    and type(value["release_credit"]) is bool and value["release_credit"] is False,"controller_report")
  return []
 exact(value,{"status","completed","controller_read_bytes","raw_usage","release_credit"})
 require(value["status"] == "RUN_RETURNED" and integer(value["controller_read_bytes"],0,16777216)
   and type(value["release_credit"]) is bool and value["release_credit"] is False,"controller_report")
 usage = value["raw_usage"]
 require(type(usage) is list and len(usage) == 16
   and all(type(v) in (int,float) and math.isfinite(v) and v >= 0 for v in usage[:2])
   and all(integer(v) for v in usage[2:]),"telemetry_type")
 completed = value["completed"]
 require(type(completed) is list and len(completed) == 1,"controller_report")
 row = completed[0]
 exact(row,{"entrypoint","argv","returncode","run_context","independent_parent_task_sha256"})
 binding = row["run_context"]
 exact(binding,{"admission_path","admission_sha256","issuer","source_assignment",
     "source_hashes","run_context","parent_custody"})
 context_shape(binding["run_context"])
 source_map(binding["source_hashes"])
 custody = binding["parent_custody"]
 custody_shape(custody)
 require(row["entrypoint"] == "dispatcher" and type(row["argv"]) is list
   and all(type(v) is str for v in row["argv"])
   and row["argv"] == ["/usr/bin/python3.14","-I","-S","-B",PACKAGE + "/tests/execute_pair.py",
        *stage2["task"]["entrypoints"][0]["arguments"]]
   and type(row["returncode"]) is int
   and hex64(row["independent_parent_task_sha256"])
   and row["independent_parent_task_sha256"] == stage2["expected_task_sha256"]
   and binding["run_context"] == stage2["task"]["run_context"],"controller_report")
 control_types(binding)
 context_shape(binding["run_context"])
 source_map(binding["source_hashes"])
 expected = stage1["expectation"]
 require(binding["admission_path"] == ISSUER_ROOT + "/" + expected["admission_sha256"] + ".json"
   and binding["admission_sha256"] == expected["admission_sha256"] and binding["issuer"] == ISSUER
   and binding["source_assignment"] == SOURCE and binding["source_hashes"] == expected["source_hashes"]
   and custody == {"schema": expected["schema"],"expectation_sha256": stage2["task"]["expectation_sha256"],
    "expectation_identity": stage2["task"]["expectation_identity"],
    "held_admission_identity": stage2["task"]["held_admission_identity"],
    "admission_identity": expected["admission_identity"],"namespace_identities": expected["namespace_identities"],
    "transport": "INDEPENDENT_PARENT_READONLY_FULLY_SEALED_HELD_OBJECTS"},"controller_report")
 return [row["returncode"]]
class Session:
 def __init__(self,stage1,raw,wall0,mono0,bootstrap_read_bytes,runtime_fd,prior_trace):
  self.handles,self.proc,self.generation = [],None,None
  self.generations,self.cleanup_unknown = {},False
  self.reaped,self.wait_status,self.wait_usage = False,None,None
  self.spawn_attempted,self.sample_at = False,0
  self.held,self.stage2,self.report,self.prior_error = [],None,None,None
  self.scope,self.runtime_fd = None,runtime_fd
  self.clock = self.budget = self.io = None
  self.close_error_objects=[];self.stream_close_attempted={}
  failure_owner.session,failure_owner.domain = self,"sender_session"
  self.s1,self.stage1_sha = stage1,digest(raw)
  self.clock,self.budget = Clock(stage1,wall0,mono0),Budget(bootstrap_read_bytes,prior_trace,{"os":os,"TRACE":TRACE,"Refusal":Refusal})
  TRACE.client.ends = (self.clock.cleanup,self.clock.mono0 + self.clock.cleanup - self.clock.wall0)
  self.io=IO.__new__(IO)
  IO.__init__(self.io,self.clock,self.budget)
 def own(self,handle):
  failure_owner.emergency_handle = handle
  self.handles.append(handle)
  failure_owner.emergency_handle = None
  TRACE.check()
  return handle
 def acquire(self,cls,*args,**kwargs):
  handle = self.own(cls.__new__(cls))
  handle._owner=AcquisitionOwner()
  cls.__init__(handle,*args,**kwargs)
  return handle
 def fd_guard(self):
  expected = {0,1,2,196,self.runtime_fd["fd"]}
  for handle in self.handles:
   expected.update(fd for fd in (getattr(handle,name,None) for name in ("fd","procfd","pidfd"))
       if fd is not None)
  if self.proc is not None:
   expected.update(pipe.fileno() for pipe in (self.proc.stdin,self.proc.stdout,self.proc.stderr)
       if pipe is not None and not pipe.closed)
  actual = set()
  for name in self.budget.strings(os.listdir("/proc/self/fd"),4096,kind="proc:self_fd_names"):
   require(name.isascii() and name.isdecimal(),"bounded_fd_map")
   fd = int(name)
   try:
    flags = fcntl.fcntl(fd,fcntl.F_GETFD)
   except OSError as error:
    require(error.errno == errno.EBADF,"bounded_fd_map")
    continue
   require(flags == (0 if fd < 3 else fcntl.FD_CLOEXEC),"owned_fd_cloexec")
   actual.add(fd)
  require(actual == expected,"exact_owned_fd_map")
 def prepare_inputs(self):
  expected = self.s1["expectation"]
  for row,(_,uid,gid,mode) in zip(expected["namespace_identities"],ANCESTRY):
   self.acquire(Directory,row["path"],row["identity"],uid,gid,mode)
  for path,ident in self.s1["source_directories"].items():
   self.acquire(Directory,path,ident)
  self.named = {}
  for name in ("controller","index"):
   pin = self.s1[name]
   self.named[name] = self.acquire(Named,pin["path"],pin["identity"],pin["sha256"],0o600,65536,self.budget)
  self.body = self.acquire(Named,ISSUER_ROOT + "/" + expected["admission_sha256"] + ".json",
      expected["admission_identity"],expected["admission_sha256"],0o400,65536,self.budget)
  body = object_wire(self.body.raw)
  body_shape(body)
  require(body == {"schema": "friday.a049.offline-run-admission.v1","issuer": ISSUER,
    "source_assignment": SOURCE,"source_hashes": expected["source_hashes"],
    "run_context": expected["run_context"]},"issuer_body")
  index = object_wire(self.named["index"].raw,canonical=False,maximum=65536)
  require(type(index.get("mandatory_member_count")) is int and index["mandatory_member_count"] == 54
    and index.get("mandatory_source_hashes") == expected["source_hashes"],"source54")
  controller_raw = self.named["controller"].raw
  require(re.search(rb'^ACCEPTED = ' + str(self.s1["controller"]["accepted_epoch"]).encode("ascii") + rb'$',
      controller_raw,re.MULTILINE) is not None,"fixed_deadline")
  thread_literal = json.dumps(self.s1["root_thread"],ensure_ascii=True).encode("ascii")
  require(b"\nROOT_THREAD = " + thread_literal + b"\n" in controller_raw,"claim_binding")
  for name,pin in self.s1["sender"]["files"].items():
   self.acquire(Named,self.s1["sender"]["root"] + "/" + name,pin["identity"],pin["sha256"],0o600,65536,self.budget)
  for name,pin in expected["source_hashes"].items():
   self.acquire(Named,PACKAGE + "/" + name,self.s1["source54_identities"][name],pin,0o600,16777216,self.budget)
  for handle in self.handles:
   if isinstance(handle,Directory):
    handle.check()
  require(self.clock.left(self.clock.init) > 0,"fixed_deadline")
  self.scope = self.acquire(OwnScope,self.budget,self.runtime_fd)
  self.fd_guard()
 def sample_tree(self,force=False):
  if self.scope is None or self.proc is None:
   return
  now = time.monotonic()
  if not force and now - self.sample_at < 5:
   return
  self.sample_at = now
  try:
   self.scope.check()
   for pid in self.scope.children():
    if pid not in self.generations:
     require(self.scope.creation_started and len(self.generations) < 16,"own_tree_bound")
     self.generations[pid] = self.acquire(Generation,pid,os.getpid(),self.budget,allow_exited=True)
    gen = self.generations[pid]
    require(not gen.reaped,"pid_generation")
    if gen.exited():
     gen.children(terminal=True)
    else:
     gen.check()
  except (OSError,ValueError,IndexError,Refusal):
   self.cleanup_unknown = True
   raise Refusal("own_tree_visibility")
 def spawn(self):
  self.fd_guard()
  self.scope.check()
  require(not self.scope.children(),"initial_own_scope_not_empty")
  require(self.named["controller"].check() == self.named["controller"].raw,"pin_mismatch")
  self.scope.creation_started = True
  self.spawn_attempted = True
  try:
   self.proc = OwnedPopen.__new__(OwnedPopen)
   OwnedPopen.__init__(self.proc,["/usr/bin/python3.14","-I","-S","-B",CONTROLLER],
    env=dict(ENV3),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
    close_fds=True,pass_fds=(196,),preexec_fn=controller_limits)
  except BaseException as error:
   TRACE.note("sender","spawn.error",[],error=error)
   raise
  TRACE.note("sender","spawn.returned",[],[self.proc.pid,self.proc.stdin.fileno(),self.proc.stdout.fileno(),self.proc.stderr.fileno()])
  self.io.attach(self.proc,self.sample_tree)
  self.generation = self.acquire(Generation,self.proc.pid,os.getpid(),self.budget,allow_exited=True)
  self.generations[self.proc.pid] = self.generation
  self.sample_tree(True)
  self.fd_guard()
 def prepare_custody(self):
  expected = self.s1["expectation"]
  init = {"assignment": self.s1["assignment"],"generation": 1,"root_thread": self.s1["root_thread"],
    "phase": "INIT","controller_sha256": self.s1["controller"]["sha256"],
    "init_deadline_epoch": self.s1["auth_deadline_epoch"],"expectation": expected}
  validate_init(init,self.s1)
  self.io.write(self.proc.stdin.fileno(),encode(init),self.clock.init)
  claim = object_wire(self.io.line(self.proc.stdout.fileno(),self.clock.init,CONTROLLER_OUT_MAX,
         role="controlled_claim"))
  validate_claim(claim,self.s1,self.generation.pid,self.generation.ticks)
  self.held = [self.acquire(Held,self.generation,198,131072,encode(expected),self.budget),
     self.acquire(Held,self.generation,199,65536,self.body.raw,self.budget)]
  observations = [item.observation for item in self.held]
  require(claim["identities"] == [item["identity"] for item in observations],"stale_identity")
  require(self.clock.left(self.clock.init) > 0,"fixed_deadline")
  self.io.write(1,encode({"status": "ROOT_CUSTODY_REQUEST","claims_are_not_authority": True,
   "stage1_sha256": self.stage1_sha,"holder_pid": self.generation.pid,
   "holder_start_ticks": self.generation.ticks,"claim": claim,"held_observations": observations,
   "auth_deadline_epoch": self.clock.auth,
   "original_cleanup_deadline_at_utc": expected["run_context"]["deadline_at_utc"],
   "run_completion_deadline": "original_cleanup_deadline_at_utc minus seal_reserve_seconds",
   "seal_reserve_seconds": expected["run_context"]["seal_reserve_seconds"],
   "outside_Root_must_independently_inspect_and_select_existing_TASK": True}),self.clock.auth,root=True)
 def preauth(self):
  self.generation.check()
  for handle in self.handles:
   if isinstance(handle,Named):
    require(handle.check() == handle.raw,"pin_mismatch")
   elif isinstance(handle,Directory):
    handle.check()
  for item in self.held:
   item.check(item.observation)
  self.sample_tree(True)
  self.generation.check()
  self.fd_guard()
  require(self.clock.left(self.clock.auth) > 0,"fixed_deadline")
  self.final_namespace()
 def final_namespace(self):
  require(self.clock.left(self.clock.auth) > 0,"fixed_deadline")
  for handle in self.handles:
   if isinstance(handle,Directory):
    handle.check()
 def run(self):
  TRACE.check()
  TRACE.note("sender","phase",["prepare_inputs"])
  self.prepare_inputs()
  TRACE.note("sender","phase",["spawn"])
  self.spawn()
  TRACE.note("sender","phase",["prepare_custody"])
  self.prepare_custody()
  raw = self.io.line(0,self.clock.auth,CONTROL_MAX,role="root_stage2_input")
  self.stage2 = object_wire(raw)
  auth = validate_stage2(self.stage2,self.s1,self.stage1_sha,self.generation.pid,
       self.generation.ticks,[v.observation for v in self.held])
  self.preauth()
  self.io.write(self.proc.stdin.fileno(),encode(auth),self.clock.auth,
     before_write=self.final_namespace)
  TRACE.call("sender","controller.stdin.close",self.proc.stdin.close)
  while True:
   raw = self.io.line(self.proc.stdout.fileno(),self.clock.run,CONTROLLER_OUT_MAX,
       role="controlled_report")
   report = object_wire(raw,telemetry=True,maximum=CONTROLLER_OUT_MAX)
   if report.get("status") in ("RUN_RETURNED","REFUSED_OR_FAILED"):
    self.report = report
    return validate_report(report,self.stage2,self.s1)
   raise Refusal("unexpected_controller_output")
 def cleanup(self):
  TRACE.note("sender","phase",["cleanup"])
  if self.proc is None or not integer(getattr(self.proc,"pid",None),1):
   return {"confirmed": not self.spawn_attempted,"cause": "spawn_not_observed" if self.spawn_attempted else None}
  if self.proc.stdin is not None and not self.proc.stdin.closed:
   TRACE.call("sender","controller.stdin.close",self.proc.stdin.close)
  generation_known = self.generation is not None
  if not generation_known or self.scope is None:
   self.cleanup_unknown = True
  if self.io.capture_stopped:
   self.cleanup_unknown = True
  terminal_scope = False
  while self.clock.cleanup_left() > 0:
   if self.scope is not None:
    try:
     self.sample_tree(True)
    except BaseException:
     self.cleanup_unknown = True
   for gen in list(self.generations.values()):
    if gen.reaped:
     continue
    try:
     if not gen.exited():
      continue
     try:
      gen.children(terminal=True)
     except BaseException:
      self.cleanup_unknown = True
     gen.check(allow_exited=True)
     pid,status,usage = os.wait4(gen.pid,os.WNOHANG)
     require(pid == gen.pid,"exact_own_wait4")
     gen.reaped,gen.wait_status,gen.wait_usage = True,status,usage
     if gen.pid == self.proc.pid:
      self.reaped,self.wait_status,self.wait_usage = True,status,gen.wait_usage
      self.proc.returncode = os.waitstatus_to_exitcode(status)
    except BaseException:
     self.cleanup_unknown = True
   if self.generation is None and not self.reaped:
    try:
     pid,status,usage = os.wait4(self.proc.pid,os.WNOHANG)
     if pid == self.proc.pid:
      self.reaped,self.wait_status,self.wait_usage = True,status,usage
      self.proc.returncode = os.waitstatus_to_exitcode(status)
    except (ChildProcessError,OSError):
     self.cleanup_unknown = True
   if self.reaped and self.scope is not None:
    try:
     self.sample_tree(True)
     if not self.scope.children() and all(gen.reaped for gen in self.generations.values()):
      self.scope.terminal(self.generations)
      terminal_scope = True
    except BaseException:
     terminal_scope = False
     self.cleanup_unknown = True
   if terminal_scope and all(fd in self.io.eof or fd in self.io.capture_stopped for fd in self.io.raw_outputs):
    break
   with selectors.DefaultSelector() as selector:
    for gen in self.generations.values():
     if not gen.reaped:
      selector.register(gen.pidfd,selectors.EVENT_READ)
    for fd in self.io.raw_outputs:
     if fd not in self.io.eof and fd not in self.io.capture_stopped:
      selector.register(fd,selectors.EVENT_READ)
    for key,_mask in selector.select(min(0.25,self.clock.cleanup_left())):
     if key.fd not in self.io.raw_outputs:
      continue
     try:
      cap = CONTROLLER_OUT_MAX - sum(len(v) for v in self.io.raw_outputs.values())
      part = self.budget.read(key.fd,min(8192,cap + 1),kind="controller_pipe_cleanup")
      if len(part) > cap:
       self.io.raw_outputs[key.fd].extend(part[:cap])
       self.io.overflow_samples[str(key.fd)] = part[cap:].hex()
       self.cleanup_unknown = True
       self.io.capture_stopped.add(key.fd)
      elif part:
       self.io.raw_outputs[key.fd].extend(part)
      else:
       self.io.eof.add(key.fd)
     except BlockingIOError:
      continue
     except (OSError,Refusal) as error:
      self.cleanup_unknown = True
      self.io.capture_stopped.add(key.fd)
      self.io.capture_errors[str(key.fd)] = {"error_type": type(error).__name__,
           "error_errno": getattr(error,"errno",None),
       "cause": str(error)[:512] if isinstance(error,Refusal) else type(error).__name__}
  if any(fd not in self.io.eof for fd in self.io.raw_outputs):
   self.cleanup_unknown = True
  if not terminal_scope:
   self.cleanup_unknown = True
  descendants_closed = (terminal_scope and bool(self.generations)
   and all(gen.reaped and gen.terminal_children == [] for gen in self.generations.values()))
  confirmed = (self.reaped and generation_known and descendants_closed and not self.cleanup_unknown
     and all(fd in self.io.eof for fd in self.io.raw_outputs))
  return {"confirmed": confirmed,"exact_direct_child_reaped": self.reaped,
    "generation_observed": generation_known,"known_descendants_closed": descendants_closed,
    "bounded_own_tree_visibility_unknown": self.cleanup_unknown,
    "wait4_status": self.wait_status,"wait4_raw_usage_observed": self.wait_usage,
    "returncode_observed": self.proc.returncode,
    "capture_stopped_original_fds": sorted(self.io.capture_stopped),
    "capture_errors_by_original_fd": dict(self.io.capture_errors),
    "tree_evidence_scope": "ISOLATED_OWN_SUBREAPER_EMPTY_INITIAL_SCOPE_TERMINAL_DIRECT_ADOPTED_LISTS_EXACT_WAIT4_NO_GLOBAL_ABSENCE",
    "terminal_scope": self.scope.snapshots if self.scope is not None else None,
    "handoff_reserved_seconds": HANDOFF_SECONDS,
    "known_generations": [{"pid": gen.pid,"parent_pid": gen.parent,"start_ticks": gen.ticks,
     "terminal_children": gen.terminal_children,"reaped": gen.reaped,
     "wait4_status": gen.wait_status,"wait4_raw_usage": gen.wait_usage}
         for gen in self.generations.values()],"signals": "NONE","retry": False}
 def close(self):
  TRACE.note("sender","phase",["close"])
  errors = []
  emergency = failure_owner.emergency_handle
  if emergency is not None:
   try:
    emergency.close()
    failure_owner.emergency_handle = None
   except BaseException as error:
    self.close_error_objects.append(error)
    errors.append(type(error).__name__)
  if self.io is not None:
   self.io.detach()
  for handle in reversed(self.handles):
   try:
    handle.close()
   except BaseException as error:
    self.close_error_objects.append(error)
    errors.append(type(error).__name__)
  if self.proc is not None:
   for pipe in (getattr(self.proc,name,None) for name in ("stdin","stdout","stderr")):
    if pipe is not None and not pipe.closed and id(pipe) not in self.stream_close_attempted:
     self.stream_close_attempted[id(pipe)]=[pipe,None]
     try:
      TRACE.call("sender","controller.pipe.close",pipe.close)
     except BaseException as error:
      self.stream_close_attempted[id(pipe)][1]=error
      self.close_error_objects.append(error)
      errors.append(type(error).__name__)
  for handle in self.handles:
   for error in getattr(getattr(handle,"_owner",None),"cleanup_errors",[]):
    if error not in self.close_error_objects:
     self.close_error_objects.append(error);errors.append(type(error).__name__)
  return errors
def perform_staged_send(stage1,stage1_raw,wall0,mono0,bootstrap_read_bytes,runtime_fd,prior_trace):
 """Actual stock call graph; no execution was performed by the Source author."""
 session,codes,cause,cleanup = None,None,None,None
 failure_owner.domain = "sender_pre_session"
 try:
  for name,expected in LIMITS.items():
   require(resource.getrlimit(getattr(resource,name)) == expected,"trusted_pre_interpreter_limits")
  validate_stage1(stage1)
  require(encode(stage1) == stage1_raw,"canonical object wire")
  require(integer(bootstrap_read_bytes,len(stage1_raw),393216),"read_bound")
  session = Session.__new__(Session)
  Session.__init__(session,stage1,stage1_raw,wall0,mono0,bootstrap_read_bytes,runtime_fd,prior_trace)
  codes = session.run()
 except BaseException as error:
  failure_owner.first_error=error
  cause = str(error)[:512] if isinstance(error,Refusal) else type(error).__name__
  TRACE.note("sender","run.failure",[cause,type(error).__name__],error=error)
 finally:
  if session is not None:
   try:
    cleanup = session.cleanup()
   except BaseException as error:
    failure_owner.cleanup_error=error
    cleanup = {"confirmed": False,"cause": "cleanup_exception","error_type": type(error).__name__}
   failure_owner.cleanup = cleanup
   close_errors = session.close()
   if close_errors:
    cleanup["confirmed"] = False
    cleanup["handle_close_errors"] = close_errors
   failure_owner.cleanup = cleanup
 if session is None:
  early_commitment = {"retained": ReadJournal(prior_trace).wire(),"omitted_events": 0,"complete": True,"denied": None}
  early_raw = encode(early_commitment)
  outcome = {"status": "REFUSED","cause": cause,"spawned": False,"runtime_GO": False,
    "release_credit": False,"cleanup": "NO_CHILD_NO_OWN_HANDLES","retry": False,
         "read_trace_sha256": digest(early_raw),"read_trace_events": len(prior_trace),
         "read_trace": early_commitment,"read_trace_bytes": len(early_raw),
    "read_trace_complete": True,"read_denied": None,
    "producer_stdio": {"dispatcher_stdout": "NOT_OBSERVED","dispatcher_stderr": "NOT_OBSERVED",
         "worker_stdout": "NOT_OBSERVED","worker_stderr": "NOT_OBSERVED",
         "observed_on_controller_pipes": False,"filler_is_producer_output": False,
         "silent_zero": False},
    "child_failure_stderr": "NO_KNOWN_CHILD_FAILURE_NOT_A_ZERO_MEASUREMENT",
    "explicit_operation_trace": TRACE.wire(),"whole_performing_trace_complete": False}
  try:
   raw = encode(outcome)
   TRACE.note("sender","native.before_delivery",[],raw)
   native_contract.before_early_delivery(outcome,raw)
   require(len(raw) <= ROOT_OUT_MAX,"root_output_bound")
   os.set_blocking(1,False)
   with selectors.DefaultSelector() as selector:
    selector.register(1,selectors.EVENT_WRITE)
    left = min(wall0 + 30 - time.time(),mono0 + 30 - time.monotonic())
    if left <= 0 or not selector.select(left):
     raise Refusal("fixed_deadline")
    count = os.write(1,raw)
    outcome["delivery"] = {"confirmed": False,"original_channel": True,
         "bytes_written": count,"complete_line": count == len(raw)}
    failure_owner.delivery = outcome["delivery"]
    TRACE.note("sender","early.write",[1,raw],count)
    require(count == len(raw),"pipe_write")
    wall,mono = time.time(),time.monotonic()
    outcome["delivery"].update(completed_wall=wall,completed_monotonic=mono)
    require(wall < wall0 + 30 and mono < mono0 + 30,"failed_handoff_deadline")
    outcome["delivery"]["confirmed"] = True
  except BaseException as error:
   outcome.setdefault("delivery",{"confirmed": False,"bytes_written": 0})
   outcome["delivery"]["error_type"] = type(error).__name__
  outcome["post_delivery_explicit_operation_trace"] = TRACE.wire()
  outcome["post_delivery_trace_is_in_written_line"] = False
  return outcome
 if not cleanup["confirmed"]:
  status = "STOP_UNCONFIRMED"
 elif (cause is not None or session.proc is None or session.proc.returncode != 0
  or session.report is None or (session.report["status"]!="RUN_RETURNED" and not (codes==[] and session.report.get("status")=="REFUSED_OR_FAILED"))):
  status = "REFUSED_OR_FAILED"
 else:
  status = "RUN_REPORTED"
 def pipe_hex(fd,buf):
  raw = bytes(buf)
  eof = fd in session.io.eof
  stopped = fd in session.io.capture_stopped
  actual_zero = bool(eof and not stopped and not raw)
  return {"prefix_hex": raw.hex() if raw else ("" if actual_zero else None),
    "prefix_bytes": len(raw),"eof": eof,"capture_stopped": stopped,
    "actual_zero": actual_zero,"silent_zero": False,
    "capture_error": session.io.capture_errors.get(str(fd)),
    "overflow_probe_hex": session.io.overflow_samples.get(str(fd))}
 known_child_failure = bool(session.report is not None and session.report.get("status") == "REFUSED_OR_FAILED")
 if codes and any(code != 0 for code in codes):
  known_child_failure = True
 if cleanup.get("returncode_observed") not in (None,0):
  known_child_failure = True
 commitment = {"retained": session.budget.trace.wire(),"omitted_events": session.budget.omitted,
    "complete": not session.budget.trace.failed,"denied": session.budget.denied,
    "later_denials": session.budget.later_denials,
               "encoding": "LOSSLESS_TABLE_AND_ADJACENT_EQUAL_CALL_RLE_NOT_DEDUPLICATION",
               "scope": "GUARDED_READ_AND_STRING_CALLS; NOT_FULL_STAT_CLOCK_WAIT_WRITE_SEQUENCE"}
 commitment_raw = encode(commitment)
 pipes = {str(fd): pipe_hex(fd,value) for fd,value in session.io.raw_outputs.items()}
 outcome = {"status": status,"cause": cause,"cleanup": cleanup,
  "reported_controller_output_claim": session.report,"reported_entrypoint_returncodes": codes,
  "controller_raw_output_hex_by_original_fd": {fd: row["prefix_hex"] for fd,row in pipes.items()},
  "controller_pipe_observation": pipes,
  "controller_original_fd_map": session.io.original_fd_map,
  "controlled_stdout_lines": list(session.io.controlled_lines),
  "producer_stdio": dict(session.io.producer_stdio),
  "child_failure_stderr": "UNKNOWN_NOT_ZERO" if known_child_failure else "NO_KNOWN_CHILD_FAILURE_NOT_A_ZERO_MEASUREMENT",
  "controller_overflow_probe_hex_by_original_fd": dict(session.io.overflow_samples),
  "sender_read_bytes_observed": session.budget.observed,
  "sender_read_requested_upper_bytes": session.budget.reserved,
  "read_trace_sha256": digest(commitment_raw),"read_trace_bytes": len(commitment_raw),
  "read_trace_events": len(session.budget.trace),"read_trace_omitted": session.budget.omitted,
  "read_trace_complete": not session.budget.trace.failed,"read_trace": commitment,
  "whole_performing_trace_complete": False,
  "read_denied": session.budget.denied,
  "later_read_denials": session.budget.later_denials,
  "root_custody_request_bytes_written": session.io.root_output,
  "root_custody_request_observations": session.io.root_requests,
  "read_accounting_scope": "EXPLICIT_REPEAT_HASH_READS_COUNTED_CAPS_ARE_RESERVATIONS_IMPLICIT_IO_UNKNOWN_NOT_ZERO",
  "sender_rlimits_observed": {name: list(resource.getrlimit(getattr(resource,name))) for name in LIMITS},
  "sender_raw_usage_observed": usage_wire(resource.getrusage(resource.RUSAGE_SELF)),
  "Root_image_RAM_IO": "UNKNOWN_NOT_ZERO","aggregate_RAM_IO": "UNKNOWN_NOT_ZERO",
  "origin": "EXTERNAL_PRECONDITION_NOT_AUTHENTICATED_FROM_DATA",
  "execution_custody": "EXTERNAL_INDEPENDENT_IMMUTABLE_EXECUTION_PRECONDITION_REQUIRED_CHECKED_SENDER_BYTES_DIRECTLY_LOADED_NO_SOURCE_GRANT",
  "handoff": "FULL_ORIGINAL_ROOT_LINE_AND_NATIVE_EXIT_REQUIRED_INCOMPLETE_OR_125_FAILED_HANDOFF",
  "native_exit_rule": "NATIVE0_IFF_RUN_REPORTED_CODES0_CLEANUP_DELIVERY_CONFIRMED_ELSE125",
  "runtime_GO": False,"release_credit": False,"full_gate": "NOT_PROVEN","retry": False}
 outcome["explicit_operation_trace"] = TRACE.wire()
 outcome["explicit_operation_trace_scope"] = "EXPLICIT_METADATA_EXTERNAL_MATCHING_BYTES_CHILD_INTERNALS_IMPLICIT_IO_SIZE_RAM_NOT_PROVEN"
 _pre = encode(outcome,telemetry=True)
 try:
  native_contract.before_delivery(outcome,_pre,session.io.root_output)
  outcome["delivery"] = session.io.terminal(_pre)
  failure_owner.delivery = outcome["delivery"]
  native_contract.after_delivery(_pre,outcome,session.io.root_output)
  if not outcome["delivery"]["confirmed"]:
   outcome["status"] = "STOP_UNCONFIRMED" if not cleanup["confirmed"] else "FAILED_HANDOFF"
 except BaseException as error:
  outcome.setdefault("delivery",{"bytes_written": 0,"complete_line": False})
  outcome["delivery"].update(confirmed=False,error_type=type(error).__name__)
  outcome["status"] = "STOP_UNCONFIRMED" if not cleanup["confirmed"] else "FAILED_HANDOFF"
 outcome["native_final_line"] = {"channel": "original_fd1","bytes": len(_pre),
         "sha256": digest(_pre),"contained_in_written_line": False}
 outcome["post_delivery_explicit_operation_trace"] = TRACE.wire()
 outcome["post_delivery_trace_is_in_written_line"] = False
 return outcome
if __name__ == "__main__":
 raise SystemExit("use the independently Root-selected stock caller; no sender CLI")
