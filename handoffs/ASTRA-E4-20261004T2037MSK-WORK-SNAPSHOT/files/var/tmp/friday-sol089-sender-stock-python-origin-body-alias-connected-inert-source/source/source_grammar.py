"""A137 Root-only pure nested grammar, verbatim performing Sender predicates.
No executing Source import, fixture, current Root fact, admission or cap increase.
Source Sender remains the actual performer; Root rechecks its independently held
input/report bytes with these same ordered pure predicates after future custody.
Effectful copies use strict prior-observation facades, never native fallthrough.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
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

PREDICATE_LOG=None
METHOD_STATE_OBSERVER=None
class Refusal(Exception):
 pass
def require(ok,cause):
 if PREDICATE_LOG is not None:
  frame=sys._getframe(1)
  key=frame.f_lineno
  site=PREDICATE_SITE_MAP[key]
  if METHOD_STATE_OBSERVER is not None:METHOD_STATE_OBSERVER(frame)
  observed={name:item for name,item in frame.f_locals.items() if name!="self"
   and type(item) in (type(None),str,bool,int,float,bytes,list,tuple,dict,set,frozenset)}
  PREDICATE_LOG.append({"site":site,"cause":cause,"ok":ok,"locals":observed})
 if not ok:
  (TRACE.note("sender","guard.refused",[cause],False) if TRACE is not None else None);raise Refusal(cause)
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

SOURCE_PREDICATES=json.loads("[{\"id\":\"sender.predicate.exact.1\",\"function\":\"exact\",\"line_start\":85,\"line_end\":85,\"ordered_expression\":\"require(type(value) is dict and set(value) == keys,\\\"exact_key_set\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.control_types.1\",\"function\":\"control_types\",\"line_start\":99,\"line_end\":99,\"ordered_expression\":\"require(all(type(key) is str for key in value),\\\"exact_key_set\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.control_types.2\",\"function\":\"control_types\",\"line_start\":106,\"line_end\":106,\"ordered_expression\":\"require(type(value) in (str,int,bool) or value is None,\\\"control_type\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.object_wire.pairs.1\",\"function\":\"object_wire.pairs\",\"line_start\":116,\"line_end\":116,\"ordered_expression\":\"require(key not in value,\\\"duplicate wire key\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.object_wire.1\",\"function\":\"object_wire\",\"line_start\":123,\"line_end\":123,\"ordered_expression\":\"require(type(raw) is bytes and 0 < len(raw) <= maximum,\\\"wire_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.object_wire.2\",\"function\":\"object_wire\",\"line_start\":126,\"line_end\":126,\"ordered_expression\":\"require(type(value) is dict,\\\"exact_key_set\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.object_wire.3\",\"function\":\"object_wire\",\"line_start\":128,\"line_end\":128,\"ordered_expression\":\"require(encode(value,telemetry=telemetry) == raw,\\\"canonical object wire\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.source_map.1\",\"function\":\"source_map\",\"line_start\":131,\"line_end\":131,\"ordered_expression\":\"require(type(value) is dict and len(value) == 54,\\\"source54\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.source_map.2\",\"function\":\"source_map\",\"line_start\":133,\"line_end\":135,\"ordered_expression\":\"require(type(name) is str and name and not name.startswith(\\\"/\\\")\\n    and all(part not in (\\\"\\\",\\\".\\\",\\\"..\\\") for part in name.split(\\\"/\\\"))\\n    and hex64(pin),\\\"source54\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.namespace_shape.1\",\"function\":\"namespace_shape\",\"line_start\":137,\"line_end\":137,\"ordered_expression\":\"require(type(rows) is list and len(rows) == 5,\\\"ancestry_spec_mismatch\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.namespace_shape.2\",\"function\":\"namespace_shape\",\"line_start\":141,\"line_end\":143,\"ordered_expression\":\"require(type(row[\\\"path\\\"]) is str and row[\\\"path\\\"] == path and id9(value)\\n    and stat.S_ISDIR(value[2]) and stat.S_IMODE(value[2]) == mode\\n    and value[3:5] == [uid,gid],\\\"ancestry_spec_mismatch\\\")\",\"operands\":[\"row[\\\"path\\\"]\",\"value[2]\",\"value[3:5]\"]},{\"id\":\"sender.predicate.body_shape.1\",\"function\":\"body_shape\",\"line_start\":146,\"line_end\":149,\"ordered_expression\":\"require(type(body[\\\"schema\\\"]) is str and body[\\\"schema\\\"] == \\\"friday.a049.offline-run-admission.v1\\\"\\n   and type(body[\\\"issuer\\\"]) is str and body[\\\"issuer\\\"] == ISSUER\\n   and type(body[\\\"source_assignment\\\"]) is str and body[\\\"source_assignment\\\"] == SOURCE,\\n   \\\"issuer_body\\\")\",\"operands\":[\"body[\\\"schema\\\"]\",\"body[\\\"issuer\\\"]\",\"body[\\\"source_assignment\\\"]\"]},{\"id\":\"sender.predicate.custody_shape.1\",\"function\":\"custody_shape\",\"line_start\":155,\"line_end\":158,\"ordered_expression\":\"require(custody[\\\"schema\\\"] == \\\"friday.a056.independent-parent-custody.v1\\\"\\n   and hex64(custody[\\\"expectation_sha256\\\"])\\n   and custody[\\\"transport\\\"] == \\\"INDEPENDENT_PARENT_READONLY_FULLY_SEALED_HELD_OBJECTS\\\",\\n   \\\"controller_report\\\")\",\"operands\":[\"custody[\\\"schema\\\"]\",\"custody[\\\"expectation_sha256\\\"]\",\"custody[\\\"transport\\\"]\"]},{\"id\":\"sender.predicate.custody_shape.2\",\"function\":\"custody_shape\",\"line_start\":163,\"line_end\":165,\"ordered_expression\":\"require(id9(value) and stat.S_ISREG(value[2]) and stat.S_IMODE(value[2]) == 0o400\\n    and value[3:6] == [1000,1000,nlink] and 0 < value[6] <= maximum,\\n    \\\"controller_report\\\")\",\"operands\":[\"value[2]\",\"value[3:6]\",\"value[6]\"]},{\"id\":\"sender.predicate.context_shape.1\",\"function\":\"context_shape\",\"line_start\":169,\"line_end\":171,\"ordered_expression\":\"require(context[\\\"schema\\\"] == \\\"friday.a049.offline-run-context.v1\\\"\\n   and context[\\\"assignment\\\"] == ASSIGNMENT + \\\"#1\\\"\\n   and integer(context[\\\"generation\\\"],1,1),\\\"phase_or_role\\\")\",\"operands\":[\"context[\\\"schema\\\"]\",\"context[\\\"assignment\\\"]\",\"context[\\\"generation\\\"]\"]},{\"id\":\"sender.predicate.context_shape.2\",\"function\":\"context_shape\",\"line_start\":172,\"line_end\":172,\"ordered_expression\":\"require(hex64(context[\\\"run_id\\\"]),\\\"pin_mismatch\\\")\",\"operands\":[\"context[\\\"run_id\\\"]\"]},{\"id\":\"sender.predicate.context_shape.3\",\"function\":\"context_shape\",\"line_start\":174,\"line_end\":175,\"ordered_expression\":\"require(all(type(context[\\\"resources\\\"][key]) is int and context[\\\"resources\\\"][key] == value\\n    for key,value in RESOURCES.items()),\\\"resources_topology_changed\\\")\",\"operands\":[\"context[\\\"resources\\\"][key]\"]},{\"id\":\"sender.predicate.context_shape.4\",\"function\":\"context_shape\",\"line_start\":176,\"line_end\":177,\"ordered_expression\":\"require(type(context[\\\"allowed_modes\\\"]) is list\\n   and context[\\\"allowed_modes\\\"] == [\\\"affected\\\",\\\"collect\\\",\\\"selfcheck\\\"],\\\"tests_lowered\\\")\",\"operands\":[\"context[\\\"allowed_modes\\\"]\"]},{\"id\":\"sender.predicate.context_shape.5\",\"function\":\"context_shape\",\"line_start\":178,\"line_end\":179,\"ordered_expression\":\"require(integer(context[\\\"seal_reserve_seconds\\\"],600,600)\\n   and integer(context[\\\"wall_seconds\\\"],601,7200),\\\"fixed_deadline\\\")\",\"operands\":[\"context[\\\"seal_reserve_seconds\\\"]\",\"context[\\\"wall_seconds\\\"]\"]},{\"id\":\"sender.predicate.context_shape.6\",\"function\":\"context_shape\",\"line_start\":182,\"line_end\":182,\"ordered_expression\":\"require(type(context[key]) is str,\\\"fixed_deadline\\\")\",\"operands\":[\"context[key]\"]},{\"id\":\"sender.predicate.context_shape.7\",\"function\":\"context_shape\",\"line_start\":184,\"line_end\":185,\"ordered_expression\":\"require(parsed.tzinfo is not None and parsed.astimezone(timezone.utc).isoformat() == context[key],\\n    \\\"fixed_deadline\\\")\",\"operands\":[\"context[key]\"]},{\"id\":\"sender.predicate.context_shape.8\",\"function\":\"context_shape\",\"line_start\":187,\"line_end\":187,\"ordered_expression\":\"require(stamps[1] - stamps[0] == context[\\\"wall_seconds\\\"],\\\"fixed_deadline\\\")\",\"operands\":[\"stamps[1]\",\"stamps[0]\",\"context[\\\"wall_seconds\\\"]\"]},{\"id\":\"sender.predicate.expectation_shape.1\",\"function\":\"expectation_shape\",\"line_start\":191,\"line_end\":192,\"ordered_expression\":\"require(expected[\\\"schema\\\"] == \\\"friday.a056.independent-parent-custody.v1\\\"\\n   and expected[\\\"issuer\\\"] == ISSUER and expected[\\\"source_assignment\\\"] == SOURCE,\\\"phase_or_role\\\")\",\"operands\":[\"expected[\\\"schema\\\"]\",\"expected[\\\"issuer\\\"]\",\"expected[\\\"source_assignment\\\"]\"]},{\"id\":\"sender.predicate.expectation_shape.2\",\"function\":\"expectation_shape\",\"line_start\":193,\"line_end\":193,\"ordered_expression\":\"require(hex64(expected[\\\"admission_sha256\\\"]),\\\"pin_mismatch\\\")\",\"operands\":[\"expected[\\\"admission_sha256\\\"]\"]},{\"id\":\"sender.predicate.expectation_shape.3\",\"function\":\"expectation_shape\",\"line_start\":195,\"line_end\":196,\"ordered_expression\":\"require(id9(ident) and stat.S_ISREG(ident[2]) and stat.S_IMODE(ident[2]) == 0o400\\n   and ident[3:6] == [1000,1000,1] and 0 < ident[6] <= 65536,\\\"issuer_body\\\")\",\"operands\":[\"ident[2]\",\"ident[3:6]\",\"ident[6]\"]},{\"id\":\"sender.predicate.validate_stage1.1\",\"function\":\"validate_stage1\",\"line_start\":202,\"line_end\":203,\"ordered_expression\":\"require(value[\\\"schema\\\"] == \\\"friday.a081.root-native-stage1.v1\\\" and value[\\\"phase\\\"] == \\\"PREPARE\\\"\\n   and value[\\\"assignment\\\"] == ASSIGNMENT and integer(value[\\\"generation\\\"],1,1),\\\"phase_or_role\\\")\",\"operands\":[\"value[\\\"schema\\\"]\",\"value[\\\"phase\\\"]\",\"value[\\\"assignment\\\"]\",\"value[\\\"generation\\\"]\"]},{\"id\":\"sender.predicate.validate_stage1.2\",\"function\":\"validate_stage1\",\"line_start\":204,\"line_end\":206,\"ordered_expression\":\"require(value[\\\"origin\\\"] == \\\"external-genuine-root-native-exec-tool\\\"\\n   and type(value[\\\"root_thread\\\"]) is str and len(value[\\\"root_thread\\\"]) <= 128\\n   and value[\\\"root_thread\\\"] != \\\"UNISSUED\\\" and value[\\\"root_thread\\\"],\\\"external_origin_precondition\\\")\",\"operands\":[\"value[\\\"origin\\\"]\",\"value[\\\"root_thread\\\"]\"]},{\"id\":\"sender.predicate.validate_stage1.3\",\"function\":\"validate_stage1\",\"line_start\":209,\"line_end\":212,\"ordered_expression\":\"require(value[\\\"controller\\\"][\\\"path\\\"] == CONTROLLER and hex64(value[\\\"controller\\\"][\\\"sha256\\\"])\\n   and id9(value[\\\"controller\\\"][\\\"identity\\\"]) and integer(value[\\\"controller\\\"][\\\"accepted_epoch\\\"],1)\\n   and value[\\\"index\\\"][\\\"path\\\"] == INDEX and value[\\\"index\\\"][\\\"sha256\\\"] == INDEX_SHA\\n   and id9(value[\\\"index\\\"][\\\"identity\\\"]) and value[\\\"package_root\\\"] == PACKAGE,\\\"pin_mismatch\\\")\",\"operands\":[\"value[\\\"controller\\\"][\\\"path\\\"]\",\"value[\\\"controller\\\"][\\\"sha256\\\"]\",\"value[\\\"controller\\\"][\\\"identity\\\"]\",\"value[\\\"controller\\\"][\\\"accepted_epoch\\\"]\",\"value[\\\"index\\\"][\\\"path\\\"]\",\"value[\\\"index\\\"][\\\"sha256\\\"]\",\"value[\\\"index\\\"][\\\"identity\\\"]\",\"value[\\\"package_root\\\"]\"]},{\"id\":\"sender.predicate.validate_stage1.4\",\"function\":\"validate_stage1\",\"line_start\":215,\"line_end\":216,\"ordered_expression\":\"require(type(root) is str and root.startswith(\\\"/\\\") and os.path.normpath(root) == root\\n   and not root.startswith(ISSUER_ROOT + \\\"/\\\") and root != ISSUER_ROOT,\\\"caller_source_selection\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.validate_stage1.5\",\"function\":\"validate_stage1\",\"line_start\":220,\"line_end\":220,\"ordered_expression\":\"require(hex64(pin[\\\"sha256\\\"]) and id9(pin[\\\"identity\\\"]),\\\"caller_source_selection\\\")\",\"operands\":[\"pin[\\\"sha256\\\"]\",\"pin[\\\"identity\\\"]\"]},{\"id\":\"sender.predicate.validate_stage1.6\",\"function\":\"validate_stage1\",\"line_start\":223,\"line_end\":224,\"ordered_expression\":\"require(type(value[\\\"source54_identities\\\"]) is dict and set(value[\\\"source54_identities\\\"]) == set(pins)\\n   and all(id9(v) for v in value[\\\"source54_identities\\\"].values()),\\\"source54\\\")\",\"operands\":[\"value[\\\"source54_identities\\\"]\"]},{\"id\":\"sender.predicate.validate_stage1.7\",\"function\":\"validate_stage1\",\"line_start\":229,\"line_end\":230,\"ordered_expression\":\"require(type(value[\\\"source_directories\\\"]) is dict and set(value[\\\"source_directories\\\"]) == dirs\\n   and all(id9(v) for v in value[\\\"source_directories\\\"].values()),\\\"source54\\\")\",\"operands\":[\"value[\\\"source_directories\\\"]\"]},{\"id\":\"sender.predicate.validate_stage1.8\",\"function\":\"validate_stage1\",\"line_start\":231,\"line_end\":233,\"ordered_expression\":\"require(type(value[\\\"dispatcher_label\\\"]) is str\\n   and re.fullmatch(r\\\"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}\\\",value[\\\"dispatcher_label\\\"]) is not None,\\n   \\\"phase_or_role\\\")\",\"operands\":[\"value[\\\"dispatcher_label\\\"]\"]},{\"id\":\"sender.predicate.validate_stage1.9\",\"function\":\"validate_stage1\",\"line_start\":234,\"line_end\":237,\"ordered_expression\":\"require(integer(value[\\\"auth_deadline_epoch\\\"],1)\\n   and begin < value[\\\"auth_deadline_epoch\\\"] <= min(end - 600,\\n    value[\\\"controller\\\"][\\\"accepted_epoch\\\"] + 6600)\\n   and end <= value[\\\"controller\\\"][\\\"accepted_epoch\\\"] + 7200,\\\"fixed_deadline\\\")\",\"operands\":[\"value[\\\"auth_deadline_epoch\\\"]\",\"value[\\\"controller\\\"][\\\"accepted_epoch\\\"]\"]},{\"id\":\"sender.predicate.validate_init.1\",\"function\":\"validate_init\",\"line_start\":242,\"line_end\":242,\"ordered_expression\":\"require(value[\\\"phase\\\"] == \\\"INIT\\\",\\\"phase_or_role\\\")\",\"operands\":[\"value[\\\"phase\\\"]\"]},{\"id\":\"sender.predicate.validate_init.2\",\"function\":\"validate_init\",\"line_start\":243,\"line_end\":248,\"ordered_expression\":\"require(value[\\\"assignment\\\"] == stage1[\\\"assignment\\\"] and integer(value[\\\"generation\\\"],1,1)\\n   and value[\\\"root_thread\\\"] == stage1[\\\"root_thread\\\"]\\n   and value[\\\"controller_sha256\\\"] == stage1[\\\"controller\\\"][\\\"sha256\\\"]\\n   and type(value[\\\"init_deadline_epoch\\\"]) is int\\n   and value[\\\"init_deadline_epoch\\\"] == stage1[\\\"auth_deadline_epoch\\\"]\\n   and value[\\\"expectation\\\"] == stage1[\\\"expectation\\\"],\\\"claim_binding\\\")\",\"operands\":[\"value[\\\"assignment\\\"]\",\"stage1[\\\"assignment\\\"]\",\"value[\\\"generation\\\"]\",\"value[\\\"root_thread\\\"]\",\"stage1[\\\"root_thread\\\"]\",\"value[\\\"controller_sha256\\\"]\",\"stage1[\\\"controller\\\"][\\\"sha256\\\"]\",\"value[\\\"init_deadline_epoch\\\"]\",\"stage1[\\\"auth_deadline_epoch\\\"]\",\"value[\\\"expectation\\\"]\",\"stage1[\\\"expectation\\\"]\"]},{\"id\":\"sender.predicate.validate_claim.1\",\"function\":\"validate_claim\",\"line_start\":251,\"line_end\":251,\"ordered_expression\":\"require(value[\\\"status\\\"] == \\\"CUSTODY_READY\\\" and value[\\\"assignment\\\"] == stage1[\\\"assignment\\\"],\\\"phase_or_role\\\")\",\"operands\":[\"value[\\\"status\\\"]\",\"value[\\\"assignment\\\"]\",\"stage1[\\\"assignment\\\"]\"]},{\"id\":\"sender.predicate.validate_claim.2\",\"function\":\"validate_claim\",\"line_start\":252,\"line_end\":254,\"ordered_expression\":\"require(integer(value[\\\"generation\\\"],1,1) and integer(value[\\\"holder_pid\\\"],1)\\n   and integer(value[\\\"holder_start_ticks\\\"],1) and value[\\\"holder_pid\\\"] == pid\\n   and value[\\\"holder_start_ticks\\\"] == ticks,\\\"pid_generation\\\")\",\"operands\":[\"value[\\\"generation\\\"]\",\"value[\\\"holder_pid\\\"]\",\"value[\\\"holder_start_ticks\\\"]\"]},{\"id\":\"sender.predicate.validate_claim.3\",\"function\":\"validate_claim\",\"line_start\":255,\"line_end\":257,\"ordered_expression\":\"require(type(value[\\\"fds\\\"]) is list and all(type(v) is int for v in value[\\\"fds\\\"])\\n   and value[\\\"fds\\\"] == [198,199] and type(value[\\\"identities\\\"]) is list\\n   and len(value[\\\"identities\\\"]) == 2 and all(id9(v) for v in value[\\\"identities\\\"]),\\\"stale_identity\\\")\",\"operands\":[\"value[\\\"fds\\\"]\",\"value[\\\"identities\\\"]\"]},{\"id\":\"sender.predicate.validate_claim.4\",\"function\":\"validate_claim\",\"line_start\":261,\"line_end\":268,\"ordered_expression\":\"require(hex64(value[\\\"expectation_sha256\\\"]) and value[\\\"expectation_sha256\\\"] == digest(encode(expected))\\n   and hex64(value[\\\"admission_sha256\\\"]) and value[\\\"admission_sha256\\\"] == expected[\\\"admission_sha256\\\"]\\n   and id9(value[\\\"admission_identity\\\"]) and value[\\\"admission_identity\\\"] == expected[\\\"admission_identity\\\"]\\n   and value[\\\"namespace_identities\\\"] == expected[\\\"namespace_identities\\\"]\\n   and value[\\\"controller_sha256\\\"] == stage1[\\\"controller\\\"][\\\"sha256\\\"]\\n   and value[\\\"source54\\\"] == expected[\\\"source_hashes\\\"]\\n   and type(value[\\\"init_deadline_epoch\\\"]) is int\\n   and value[\\\"init_deadline_epoch\\\"] == stage1[\\\"auth_deadline_epoch\\\"],\\\"claim_binding\\\")\",\"operands\":[\"value[\\\"expectation_sha256\\\"]\",\"value[\\\"admission_sha256\\\"]\",\"expected[\\\"admission_sha256\\\"]\",\"value[\\\"admission_identity\\\"]\",\"expected[\\\"admission_identity\\\"]\",\"value[\\\"namespace_identities\\\"]\",\"expected[\\\"namespace_identities\\\"]\",\"value[\\\"controller_sha256\\\"]\",\"stage1[\\\"controller\\\"][\\\"sha256\\\"]\",\"value[\\\"source54\\\"]\",\"expected[\\\"source_hashes\\\"]\",\"value[\\\"init_deadline_epoch\\\"]\",\"stage1[\\\"auth_deadline_epoch\\\"]\"]},{\"id\":\"sender.predicate.check_held_observation.1\",\"function\":\"check_held_observation\",\"line_start\":273,\"line_end\":278,\"ordered_expression\":\"require(integer(value[\\\"original_flags\\\"]) and integer(value[\\\"opened_flags\\\"])\\n   and value[\\\"original_flags\\\"] & os.O_ACCMODE == os.O_RDONLY\\n   and value[\\\"original_flags\\\"] & os.O_CLOEXEC == os.O_CLOEXEC\\n   and value[\\\"opened_flags\\\"] & os.O_ACCMODE == os.O_RDONLY\\n   and type(value[\\\"opened_cloexec\\\"]) is bool and value[\\\"opened_cloexec\\\"] is True,\\n   \\\"fd_not_readonly\\\")\",\"operands\":[\"value[\\\"original_flags\\\"]\",\"value[\\\"opened_flags\\\"]\",\"value[\\\"opened_cloexec\\\"]\"]},{\"id\":\"sender.predicate.check_held_observation.2\",\"function\":\"check_held_observation\",\"line_start\":279,\"line_end\":279,\"ordered_expression\":\"require(integer(value[\\\"seals\\\"]) and value[\\\"seals\\\"] & SEALS == SEALS,\\\"seals_incomplete\\\")\",\"operands\":[\"value[\\\"seals\\\"]\"]},{\"id\":\"sender.predicate.check_held_observation.3\",\"function\":\"check_held_observation\",\"line_start\":281,\"line_end\":282,\"ordered_expression\":\"require(id9(ident) and stat.S_ISREG(ident[2]) and stat.S_IMODE(ident[2]) == 0o400\\n   and ident[3:6] == [1000,1000,0] and ident[6] == len(raw),\\\"stale_identity\\\")\",\"operands\":[\"ident[2]\",\"ident[3:6]\",\"ident[6]\"]},{\"id\":\"sender.predicate.check_held_observation.4\",\"function\":\"check_held_observation\",\"line_start\":283,\"line_end\":283,\"ordered_expression\":\"require(hex64(value[\\\"sha256\\\"]) and value[\\\"sha256\\\"] == digest(raw),\\\"pin_mismatch\\\")\",\"operands\":[\"value[\\\"sha256\\\"]\"]},{\"id\":\"sender.predicate.check_held_observation.5\",\"function\":\"check_held_observation\",\"line_start\":285,\"line_end\":285,\"ordered_expression\":\"require(ident == expected_identity,\\\"stale_identity\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.validate_stage2.1\",\"function\":\"validate_stage2\",\"line_start\":288,\"line_end\":291,\"ordered_expression\":\"require(value[\\\"schema\\\"] == \\\"friday.a081.root-native-stage2.v1\\\" and value[\\\"phase\\\"] == \\\"AUTHORIZE\\\"\\n   and value[\\\"assignment\\\"] == stage1[\\\"assignment\\\"] and integer(value[\\\"generation\\\"],1,1)\\n   and value[\\\"root_thread\\\"] == stage1[\\\"root_thread\\\"] and value[\\\"stage1_sha256\\\"] == stage1_sha,\\n   \\\"phase_or_role\\\")\",\"operands\":[\"value[\\\"schema\\\"]\",\"value[\\\"phase\\\"]\",\"value[\\\"assignment\\\"]\",\"stage1[\\\"assignment\\\"]\",\"value[\\\"generation\\\"]\",\"value[\\\"root_thread\\\"]\",\"stage1[\\\"root_thread\\\"]\",\"value[\\\"stage1_sha256\\\"]\"]},{\"id\":\"sender.predicate.validate_stage2.2\",\"function\":\"validate_stage2\",\"line_start\":292,\"line_end\":293,\"ordered_expression\":\"require(integer(value[\\\"holder_pid\\\"],1) and integer(value[\\\"holder_start_ticks\\\"],1)\\n   and value[\\\"holder_pid\\\"] == pid and value[\\\"holder_start_ticks\\\"] == ticks,\\\"pid_generation\\\")\",\"operands\":[\"value[\\\"holder_pid\\\"]\",\"value[\\\"holder_start_ticks\\\"]\"]},{\"id\":\"sender.predicate.validate_stage2.3\",\"function\":\"validate_stage2\",\"line_start\":296,\"line_end\":297,\"ordered_expression\":\"require(hex64(value[\\\"expected_task_sha256\\\"])\\n   and digest(encode(task)) == value[\\\"expected_task_sha256\\\"],\\\"pin_mismatch\\\")\",\"operands\":[\"value[\\\"expected_task_sha256\\\"]\"]},{\"id\":\"sender.predicate.validate_stage2.4\",\"function\":\"validate_stage2\",\"line_start\":301,\"line_end\":303,\"ordered_expression\":\"require(task[\\\"schema\\\"] == \\\"friday.a056.independent-parent-run-task.v1\\\"\\n   and task[\\\"issuer\\\"] == ISSUER and task[\\\"source_assignment\\\"] == SOURCE\\n   and task[\\\"package_root\\\"] == PACKAGE,\\\"phase_or_role\\\")\",\"operands\":[\"task[\\\"schema\\\"]\",\"task[\\\"issuer\\\"]\",\"task[\\\"source_assignment\\\"]\",\"task[\\\"package_root\\\"]\"]},{\"id\":\"sender.predicate.validate_stage2.5\",\"function\":\"validate_stage2\",\"line_start\":304,\"line_end\":305,\"ordered_expression\":\"require(task[\\\"source_hashes\\\"] == expected[\\\"source_hashes\\\"] and task[\\\"run_context\\\"] == expected[\\\"run_context\\\"]\\n   and task[\\\"expectation_sha256\\\"] == held[0][\\\"sha256\\\"],\\\"pin_mismatch\\\")\",\"operands\":[\"task[\\\"source_hashes\\\"]\",\"expected[\\\"source_hashes\\\"]\",\"task[\\\"run_context\\\"]\",\"expected[\\\"run_context\\\"]\",\"task[\\\"expectation_sha256\\\"]\",\"held[0][\\\"sha256\\\"]\"]},{\"id\":\"sender.predicate.validate_stage2.6\",\"function\":\"validate_stage2\",\"line_start\":308,\"line_end\":310,\"ordered_expression\":\"require(id9(task[\\\"expectation_identity\\\"]) and id9(task[\\\"held_admission_identity\\\"])\\n   and task[\\\"expectation_identity\\\"] == held[0][\\\"identity\\\"]\\n   and task[\\\"held_admission_identity\\\"] == held[1][\\\"identity\\\"],\\\"stale_identity\\\")\",\"operands\":[\"task[\\\"expectation_identity\\\"]\",\"task[\\\"held_admission_identity\\\"]\",\"held[0][\\\"identity\\\"]\",\"held[1][\\\"identity\\\"]\"]},{\"id\":\"sender.predicate.validate_stage2.7\",\"function\":\"validate_stage2\",\"line_start\":311,\"line_end\":311,\"ordered_expression\":\"require(type(task[\\\"entrypoints\\\"]) is list and len(task[\\\"entrypoints\\\"]) == 1,\\\"exact_key_set\\\")\",\"operands\":[\"task[\\\"entrypoints\\\"]\"]},{\"id\":\"sender.predicate.validate_stage2.8\",\"function\":\"validate_stage2\",\"line_start\":314,\"line_end\":316,\"ordered_expression\":\"require(step[\\\"entrypoint\\\"] == \\\"dispatcher\\\" and type(step[\\\"arguments\\\"]) is list\\n   and all(type(v) is str for v in step[\\\"arguments\\\"])\\n   and step[\\\"arguments\\\"] == [\\\"--label\\\",stage1[\\\"dispatcher_label\\\"],\\\"--focused-checks\\\"],\\\"phase_or_role\\\")\",\"operands\":[\"step[\\\"entrypoint\\\"]\",\"step[\\\"arguments\\\"]\",\"stage1[\\\"dispatcher_label\\\"]\"]},{\"id\":\"sender.predicate.Clock.__init__.1\",\"function\":\"Clock.__init__\",\"line_start\":326,\"line_end\":326,\"ordered_expression\":\"require(begin <= self.now() < self.run and self.now() < self.auth <= wall0 + 300,\\\"fixed_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Clock.now.1\",\"function\":\"Clock.now\",\"line_start\":329,\"line_end\":329,\"ordered_expression\":\"require(abs(wall - self.wall0 - (time.monotonic() - self.mono0)) <= 2,\\\"clock drift\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Named.__init__.1\",\"function\":\"Named.__init__\",\"line_start\":341,\"line_end\":341,\"ordered_expression\":\"require(id9(pinned_identity) and identity(before) == pinned_identity,\\\"stale_identity\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Named.__init__.2\",\"function\":\"Named.__init__\",\"line_start\":342,\"line_end\":344,\"ordered_expression\":\"require(stat.S_ISREG(before.st_mode) and stat.S_IMODE(before.st_mode) == mode\\n    and (before.st_uid,before.st_gid,before.st_nlink) == (1000,1000,1)\\n    and 0 < before.st_size <= maximum,\\\"issuer_body\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Named.check.1\",\"function\":\"Named.check\",\"line_start\":356,\"line_end\":357,\"ordered_expression\":\"require(identity(os.lstat(self.path)) == self.identity and identity(os.fstat(self.fd)) == self.identity,\\n    \\\"stale_identity\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Named.check.2\",\"function\":\"Named.check\",\"line_start\":359,\"line_end\":360,\"ordered_expression\":\"require(len(raw) == self.identity[6] and identity(os.fstat(self.fd)) == self.identity\\n    and identity(os.lstat(self.path)) == self.identity,\\\"stale_identity\\\")\",\"operands\":[\"identity[6]\"]},{\"id\":\"sender.predicate.Named.check.3\",\"function\":\"Named.check\",\"line_start\":361,\"line_end\":361,\"ordered_expression\":\"require(digest(raw) == self.pin,\\\"pin_mismatch\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Directory.__init__.1\",\"function\":\"Directory.__init__\",\"line_start\":372,\"line_end\":374,\"ordered_expression\":\"require(id9(expected) and identity(info) == expected and stat.S_ISDIR(info.st_mode)\\n    and stat.S_IMODE(info.st_mode) == mode and (info.st_uid,info.st_gid) == (uid,gid),\\n    \\\"stale_identity\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Directory.check.1\",\"function\":\"Directory.check\",\"line_start\":384,\"line_end\":385,\"ordered_expression\":\"require(identity(os.lstat(self.path)) == self.identity and identity(os.fstat(self.fd)) == self.identity,\\n    \\\"stale_identity\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.proc_read.1\",\"function\":\"Generation.proc_read\",\"line_start\":409,\"line_end\":409,\"ordered_expression\":\"require(len(raw) <= cap,\\\"proc_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.stat.1\",\"function\":\"Generation.stat\",\"line_start\":417,\"line_end\":417,\"ordered_expression\":\"require(int(head.split(\\\"(\\\",1)[0]) == self.pid and int(values[1]) == self.parent,\\\"pid_generation\\\")\",\"operands\":[\"values[1]\"]},{\"id\":\"sender.predicate.Generation.check.1\",\"function\":\"Generation.check\",\"line_start\":424,\"line_end\":424,\"ordered_expression\":\"require((allow_exited or not self.exited()) and self.stat()[1] == self.ticks,\\\"pid_generation\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.check.2\",\"function\":\"Generation.check\",\"line_start\":428,\"line_end\":428,\"ordered_expression\":\"require(len(raw) <= 4096,\\\"proc_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.check.3\",\"function\":\"Generation.check\",\"line_start\":430,\"line_end\":430,\"ordered_expression\":\"require(rows == [str(self.pid)],\\\"pid_generation\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.children.1\",\"function\":\"Generation.children\",\"line_start\":438,\"line_end\":438,\"ordered_expression\":\"require(len(tasks) <= 32 and all(v.isdigit() for v in tasks),\\\"own_tree_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.children.2\",\"function\":\"Generation.children\",\"line_start\":443,\"line_end\":443,\"ordered_expression\":\"require(all(v.isdigit() for v in values),\\\"own_tree_visibility\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.children.3\",\"function\":\"Generation.children\",\"line_start\":445,\"line_end\":445,\"ordered_expression\":\"require(len(children) <= 16,\\\"own_tree_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Generation.children.4\",\"function\":\"Generation.children\",\"line_start\":448,\"line_end\":449,\"ordered_expression\":\"require(self.exited() and self.stat()[0] in (\\\"Z\\\",\\\"X\\\") and not children,\\n      \\\"terminal_children_unconfirmed\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.__init__.1\",\"function\":\"OwnScope.__init__\",\"line_start\":475,\"line_end\":475,\"ordered_expression\":\"require(signal.SIGCHLD not in signal.pthread_sigmask(signal.SIG_BLOCK,[]),\\\"SIGCHLD_blocked\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.__init__.2\",\"function\":\"OwnScope.__init__\",\"line_start\":477,\"line_end\":477,\"ordered_expression\":\"require(signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,\\\"own_reaper_policy\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.__init__.3\",\"function\":\"OwnScope.__init__\",\"line_start\":481,\"line_end\":481,\"ordered_expression\":\"require(not self.children(),\\\"initial_own_scope_not_empty\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.proc_read.1\",\"function\":\"OwnScope.proc_read\",\"line_start\":509,\"line_end\":509,\"ordered_expression\":\"require(len(raw) <= maximum,\\\"proc_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.single_thread.1\",\"function\":\"OwnScope.single_thread\",\"line_start\":515,\"line_end\":516,\"ordered_expression\":\"require(rows.get(\\\"Threads\\\",\\\"\\\").strip() == \\\"1\\\" and rows.get(\\\"Pid\\\",\\\"\\\").strip() == str(os.getpid()),\\n    \\\"own_scope_single_thread\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.runtime_check.1\",\"function\":\"OwnScope.runtime_check\",\"line_start\":519,\"line_end\":521,\"ordered_expression\":\"require(identity(os.fstat(r[\\\"fd\\\"])) == r[\\\"identity\\\"]\\n    and fcntl.fcntl(r[\\\"fd\\\"],fcntl.F_GETFD) == fcntl.FD_CLOEXEC\\n    and fcntl.fcntl(r[\\\"fd\\\"],fcntl.F_GETFL) == r[\\\"flags\\\"],\\\"stock_runtime_fd_drift\\\")\",\"operands\":[\"r[\\\"fd\\\"]\",\"r[\\\"identity\\\"]\",\"r[\\\"flags\\\"]\"]},{\"id\":\"sender.predicate.OwnScope.runtime_check.2\",\"function\":\"OwnScope.runtime_check\",\"line_start\":524,\"line_end\":524,\"ordered_expression\":\"require(link == r[\\\"path\\\"],\\\"stock_runtime_fd_drift\\\")\",\"operands\":[\"r[\\\"path\\\"]\"]},{\"id\":\"sender.predicate.OwnScope.check.1\",\"function\":\"OwnScope.check\",\"line_start\":529,\"line_end\":530,\"ordered_expression\":\"require(value == 1 and signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL\\n    and signal.SIGCHLD not in signal.pthread_sigmask(signal.SIG_BLOCK,[]),\\\"own_reaper_policy\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.children.1\",\"function\":\"OwnScope.children\",\"line_start\":534,\"line_end\":535,\"ordered_expression\":\"require(len(values) <= 16 and all(v.isascii() and v.isdecimal() and int(v) > 0 for v in values),\\n    \\\"own_tree_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.terminal.1\",\"function\":\"OwnScope.terminal\",\"line_start\":540,\"line_end\":541,\"ordered_expression\":\"require(not before and all(gen.reaped and gen.terminal_children == [] for gen in generations.values()),\\n    \\\"own_tree_terminal_unconfirmed\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.OwnScope.terminal.2\",\"function\":\"OwnScope.terminal\",\"line_start\":544,\"line_end\":544,\"ordered_expression\":\"require(not after and self.initial_empty and self.creation_started,\\\"own_tree_terminal_unconfirmed\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Held.__init__.1\",\"function\":\"Held.__init__\",\"line_start\":558,\"line_end\":559,\"ordered_expression\":\"require(self.original_flags & os.O_ACCMODE == os.O_RDONLY\\n    and self.original_flags & os.O_CLOEXEC == os.O_CLOEXEC,\\\"fd_not_readonly\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Held.flags.1\",\"function\":\"Held.flags\",\"line_start\":571,\"line_end\":571,\"ordered_expression\":\"require(len(rows) == 1,\\\"fd_not_readonly\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Held.check.1\",\"function\":\"Held.check\",\"line_start\":576,\"line_end\":576,\"ordered_expression\":\"require(flags == self.original_flags,\\\"fd_not_readonly\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Held.check.2\",\"function\":\"Held.check\",\"line_start\":579,\"line_end\":579,\"ordered_expression\":\"require(before == opened and 0 < opened[6] <= self.maximum,\\\"stale_identity\\\")\",\"operands\":[\"opened[6]\"]},{\"id\":\"sender.predicate.Held.check.3\",\"function\":\"Held.check\",\"line_start\":582,\"line_end\":582,\"ordered_expression\":\"require(identity(os.fstat(current)) == opened,\\\"stale_identity\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Held.check.4\",\"function\":\"Held.check\",\"line_start\":590,\"line_end\":595,\"ordered_expression\":\"require(fcntl.fcntl(current,fcntl.F_GET_SEALS) & SEALS == SEALS\\n     and fcntl.fcntl(current,fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY\\n     and raw == current_raw == self.raw and len(raw) == opened[6]\\n     and identity(os.fstat(self.fd)) == opened and identity(os.fstat(current)) == opened\\n     and identity(os.stat(\\\"fd/%d\\\" % self.target,dir_fd=self.generation.procfd)) == opened\\n     and self.flags() == flags,\\\"stale_identity\\\")\",\"operands\":[\"opened[6]\"]},{\"id\":\"sender.predicate.Held.check.5\",\"function\":\"Held.check\",\"line_start\":597,\"line_end\":597,\"ordered_expression\":\"require(observation == fixed,\\\"stale_identity\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.terminal.1\",\"function\":\"IO.terminal\",\"line_start\":644,\"line_end\":645,\"ordered_expression\":\"require(type(raw) is bytes and 0 < len(raw) <= ROOT_OUT_MAX\\n     and self.root_output + len(raw) <= ROOT_OUT_MAX,\\\"root_output_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.terminal.2\",\"function\":\"IO.terminal\",\"line_start\":651,\"line_end\":651,\"ordered_expression\":\"require(left > 0,\\\"failed_handoff_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.terminal.3\",\"function\":\"IO.terminal\",\"line_start\":654,\"line_end\":654,\"ordered_expression\":\"require(self.clock.handoff_left() > 0,\\\"failed_handoff_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.terminal.4\",\"function\":\"IO.terminal\",\"line_start\":664,\"line_end\":664,\"ordered_expression\":\"require(count > 0,\\\"failed_handoff_write\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.terminal.5\",\"function\":\"IO.terminal\",\"line_start\":670,\"line_end\":671,\"ordered_expression\":\"require(wall < result[\\\"original_wall_end\\\"] and mono < result[\\\"original_monotonic_end\\\"],\\n     \\\"failed_handoff_deadline\\\")\",\"operands\":[\"result[\\\"original_wall_end\\\"]\",\"result[\\\"original_monotonic_end\\\"]\"]},{\"id\":\"sender.predicate.IO.pump.1\",\"function\":\"IO.pump\",\"line_start\":682,\"line_end\":682,\"ordered_expression\":\"require(left > 0,\\\"fixed_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.write.1\",\"function\":\"IO.write\",\"line_start\":719,\"line_end\":719,\"ordered_expression\":\"require(type(raw) is bytes and 0 < len(raw) <= (ROOT_OUT_MAX if root else CONTROL_MAX),\\\"wire_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.write.2\",\"function\":\"IO.write\",\"line_start\":721,\"line_end\":721,\"ordered_expression\":\"require(self.root_output + len(raw) <= ROOT_OUT_MAX,\\\"root_output_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.write.3\",\"function\":\"IO.write\",\"line_start\":738,\"line_end\":738,\"ordered_expression\":\"require(count > 0,\\\"pipe_write\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.line.1\",\"function\":\"IO.line\",\"line_start\":747,\"line_end\":747,\"ordered_expression\":\"require(len(buffer) <= maximum and fd not in self.eof,\\\"bounded control or EOF\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.IO.line.2\",\"function\":\"IO.line\",\"line_start\":759,\"line_end\":759,\"ordered_expression\":\"require(index <= maximum,\\\"wire_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.validate_report.1\",\"function\":\"validate_report\",\"line_start\":774,\"line_end\":774,\"ordered_expression\":\"require(type(value) is dict,\\\"controller_report\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.validate_report.2\",\"function\":\"validate_report\",\"line_start\":777,\"line_end\":779,\"ordered_expression\":\"require(type(value[\\\"error_type\\\"]) is str and type(value[\\\"reason\\\"]) is str\\n    and len(value[\\\"reason\\\"]) <= 512 and integer(value[\\\"controller_read_bytes\\\"])\\n    and type(value[\\\"release_credit\\\"]) is bool and value[\\\"release_credit\\\"] is False,\\\"controller_report\\\")\",\"operands\":[\"value[\\\"error_type\\\"]\",\"value[\\\"reason\\\"]\",\"value[\\\"controller_read_bytes\\\"]\",\"value[\\\"release_credit\\\"]\"]},{\"id\":\"sender.predicate.validate_report.3\",\"function\":\"validate_report\",\"line_start\":782,\"line_end\":783,\"ordered_expression\":\"require(value[\\\"status\\\"] == \\\"RUN_RETURNED\\\" and integer(value[\\\"controller_read_bytes\\\"],0,16777216)\\n   and type(value[\\\"release_credit\\\"]) is bool and value[\\\"release_credit\\\"] is False,\\\"controller_report\\\")\",\"operands\":[\"value[\\\"status\\\"]\",\"value[\\\"controller_read_bytes\\\"]\",\"value[\\\"release_credit\\\"]\"]},{\"id\":\"sender.predicate.validate_report.4\",\"function\":\"validate_report\",\"line_start\":785,\"line_end\":787,\"ordered_expression\":\"require(type(usage) is list and len(usage) == 16\\n   and all(type(v) in (int,float) and math.isfinite(v) and v >= 0 for v in usage[:2])\\n   and all(integer(v) for v in usage[2:]),\\\"telemetry_type\\\")\",\"operands\":[\"usage[:2]\",\"usage[2:]\"]},{\"id\":\"sender.predicate.validate_report.5\",\"function\":\"validate_report\",\"line_start\":789,\"line_end\":789,\"ordered_expression\":\"require(type(completed) is list and len(completed) == 1,\\\"controller_report\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.validate_report.6\",\"function\":\"validate_report\",\"line_start\":799,\"line_end\":806,\"ordered_expression\":\"require(row[\\\"entrypoint\\\"] == \\\"dispatcher\\\" and type(row[\\\"argv\\\"]) is list\\n   and all(type(v) is str for v in row[\\\"argv\\\"])\\n   and row[\\\"argv\\\"] == [\\\"/usr/bin/python3.14\\\",\\\"-I\\\",\\\"-S\\\",\\\"-B\\\",PACKAGE + \\\"/tests/execute_pair.py\\\",\\n        *stage2[\\\"task\\\"][\\\"entrypoints\\\"][0][\\\"arguments\\\"]]\\n   and type(row[\\\"returncode\\\"]) is int\\n   and hex64(row[\\\"independent_parent_task_sha256\\\"])\\n   and row[\\\"independent_parent_task_sha256\\\"] == stage2[\\\"expected_task_sha256\\\"]\\n   and binding[\\\"run_context\\\"] == stage2[\\\"task\\\"][\\\"run_context\\\"],\\\"controller_report\\\")\",\"operands\":[\"row[\\\"entrypoint\\\"]\",\"row[\\\"argv\\\"]\",\"stage2[\\\"task\\\"][\\\"entrypoints\\\"][0][\\\"arguments\\\"]\",\"row[\\\"returncode\\\"]\",\"row[\\\"independent_parent_task_sha256\\\"]\",\"stage2[\\\"expected_task_sha256\\\"]\",\"binding[\\\"run_context\\\"]\",\"stage2[\\\"task\\\"][\\\"run_context\\\"]\"]},{\"id\":\"sender.predicate.validate_report.7\",\"function\":\"validate_report\",\"line_start\":811,\"line_end\":818,\"ordered_expression\":\"require(binding[\\\"admission_path\\\"] == ISSUER_ROOT + \\\"/\\\" + expected[\\\"admission_sha256\\\"] + \\\".json\\\"\\n   and binding[\\\"admission_sha256\\\"] == expected[\\\"admission_sha256\\\"] and binding[\\\"issuer\\\"] == ISSUER\\n   and binding[\\\"source_assignment\\\"] == SOURCE and binding[\\\"source_hashes\\\"] == expected[\\\"source_hashes\\\"]\\n   and custody == {\\\"schema\\\": expected[\\\"schema\\\"],\\\"expectation_sha256\\\": stage2[\\\"task\\\"][\\\"expectation_sha256\\\"],\\n    \\\"expectation_identity\\\": stage2[\\\"task\\\"][\\\"expectation_identity\\\"],\\n    \\\"held_admission_identity\\\": stage2[\\\"task\\\"][\\\"held_admission_identity\\\"],\\n    \\\"admission_identity\\\": expected[\\\"admission_identity\\\"],\\\"namespace_identities\\\": expected[\\\"namespace_identities\\\"],\\n    \\\"transport\\\": \\\"INDEPENDENT_PARENT_READONLY_FULLY_SEALED_HELD_OBJECTS\\\"},\\\"controller_report\\\")\",\"operands\":[\"binding[\\\"admission_path\\\"]\",\"expected[\\\"admission_sha256\\\"]\",\"binding[\\\"admission_sha256\\\"]\",\"binding[\\\"issuer\\\"]\",\"binding[\\\"source_assignment\\\"]\",\"binding[\\\"source_hashes\\\"]\",\"expected[\\\"source_hashes\\\"]\",\"expected[\\\"schema\\\"]\",\"stage2[\\\"task\\\"][\\\"expectation_sha256\\\"]\",\"stage2[\\\"task\\\"][\\\"expectation_identity\\\"]\",\"stage2[\\\"task\\\"][\\\"held_admission_identity\\\"]\",\"expected[\\\"admission_identity\\\"]\",\"expected[\\\"namespace_identities\\\"]\"]},{\"id\":\"sender.predicate.Session.fd_guard.1\",\"function\":\"Session.fd_guard\",\"line_start\":857,\"line_end\":857,\"ordered_expression\":\"require(name.isascii() and name.isdecimal(),\\\"bounded_fd_map\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.fd_guard.2\",\"function\":\"Session.fd_guard\",\"line_start\":862,\"line_end\":862,\"ordered_expression\":\"require(error.errno == errno.EBADF,\\\"bounded_fd_map\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.fd_guard.3\",\"function\":\"Session.fd_guard\",\"line_start\":864,\"line_end\":864,\"ordered_expression\":\"require(flags == (0 if fd < 3 else fcntl.FD_CLOEXEC),\\\"owned_fd_cloexec\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.fd_guard.4\",\"function\":\"Session.fd_guard\",\"line_start\":866,\"line_end\":866,\"ordered_expression\":\"require(actual == expected,\\\"exact_owned_fd_map\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.prepare_inputs.1\",\"function\":\"Session.prepare_inputs\",\"line_start\":881,\"line_end\":883,\"ordered_expression\":\"require(body == {\\\"schema\\\": \\\"friday.a049.offline-run-admission.v1\\\",\\\"issuer\\\": ISSUER,\\n    \\\"source_assignment\\\": SOURCE,\\\"source_hashes\\\": expected[\\\"source_hashes\\\"],\\n    \\\"run_context\\\": expected[\\\"run_context\\\"]},\\\"issuer_body\\\")\",\"operands\":[\"expected[\\\"source_hashes\\\"]\",\"expected[\\\"run_context\\\"]\"]},{\"id\":\"sender.predicate.Session.prepare_inputs.2\",\"function\":\"Session.prepare_inputs\",\"line_start\":885,\"line_end\":886,\"ordered_expression\":\"require(type(index.get(\\\"mandatory_member_count\\\")) is int and index[\\\"mandatory_member_count\\\"] == 54\\n    and index.get(\\\"mandatory_source_hashes\\\") == expected[\\\"source_hashes\\\"],\\\"source54\\\")\",\"operands\":[\"index[\\\"mandatory_member_count\\\"]\",\"expected[\\\"source_hashes\\\"]\"]},{\"id\":\"sender.predicate.Session.prepare_inputs.3\",\"function\":\"Session.prepare_inputs\",\"line_start\":888,\"line_end\":889,\"ordered_expression\":\"require(re.search(rb'^ACCEPTED = ' + str(self.s1[\\\"controller\\\"][\\\"accepted_epoch\\\"]).encode(\\\"ascii\\\") + rb'$',\\n      controller_raw,re.MULTILINE) is not None,\\\"fixed_deadline\\\")\",\"operands\":[\"s1[\\\"controller\\\"][\\\"accepted_epoch\\\"]\"]},{\"id\":\"sender.predicate.Session.prepare_inputs.4\",\"function\":\"Session.prepare_inputs\",\"line_start\":891,\"line_end\":891,\"ordered_expression\":\"require(b\\\"\\\\nROOT_THREAD = \\\" + thread_literal + b\\\"\\\\n\\\" in controller_raw,\\\"claim_binding\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.prepare_inputs.5\",\"function\":\"Session.prepare_inputs\",\"line_start\":899,\"line_end\":899,\"ordered_expression\":\"require(self.clock.left(self.clock.init) > 0,\\\"fixed_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.sample_tree.1\",\"function\":\"Session.sample_tree\",\"line_start\":913,\"line_end\":913,\"ordered_expression\":\"require(self.scope.creation_started and len(self.generations) < 16,\\\"own_tree_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.sample_tree.2\",\"function\":\"Session.sample_tree\",\"line_start\":916,\"line_end\":916,\"ordered_expression\":\"require(not gen.reaped,\\\"pid_generation\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.spawn.1\",\"function\":\"Session.spawn\",\"line_start\":927,\"line_end\":927,\"ordered_expression\":\"require(not self.scope.children(),\\\"initial_own_scope_not_empty\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.spawn.2\",\"function\":\"Session.spawn\",\"line_start\":928,\"line_end\":928,\"ordered_expression\":\"require(self.named[\\\"controller\\\"].check() == self.named[\\\"controller\\\"].raw,\\\"pin_mismatch\\\")\",\"operands\":[\"named[\\\"controller\\\"]\"]},{\"id\":\"sender.predicate.Session.prepare_custody.1\",\"function\":\"Session.prepare_custody\",\"line_start\":958,\"line_end\":958,\"ordered_expression\":\"require(claim[\\\"identities\\\"] == [item[\\\"identity\\\"] for item in observations],\\\"stale_identity\\\")\",\"operands\":[\"claim[\\\"identities\\\"]\",\"item[\\\"identity\\\"]\"]},{\"id\":\"sender.predicate.Session.prepare_custody.2\",\"function\":\"Session.prepare_custody\",\"line_start\":959,\"line_end\":959,\"ordered_expression\":\"require(self.clock.left(self.clock.init) > 0,\\\"fixed_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.preauth.1\",\"function\":\"Session.preauth\",\"line_start\":972,\"line_end\":972,\"ordered_expression\":\"require(handle.check() == handle.raw,\\\"pin_mismatch\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.preauth.2\",\"function\":\"Session.preauth\",\"line_start\":980,\"line_end\":980,\"ordered_expression\":\"require(self.clock.left(self.clock.auth) > 0,\\\"fixed_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.final_namespace.1\",\"function\":\"Session.final_namespace\",\"line_start\":983,\"line_end\":983,\"ordered_expression\":\"require(self.clock.left(self.clock.auth) > 0,\\\"fixed_deadline\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.Session.cleanup.1\",\"function\":\"Session.cleanup\",\"line_start\":1041,\"line_end\":1041,\"ordered_expression\":\"require(pid == gen.pid,\\\"exact_own_wait4\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.perform_staged_send.1\",\"function\":\"perform_staged_send\",\"line_start\":1159,\"line_end\":1159,\"ordered_expression\":\"require(resource.getrlimit(getattr(resource,name)) == expected,\\\"trusted_pre_interpreter_limits\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.perform_staged_send.2\",\"function\":\"perform_staged_send\",\"line_start\":1161,\"line_end\":1161,\"ordered_expression\":\"require(encode(stage1) == stage1_raw,\\\"canonical object wire\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.perform_staged_send.3\",\"function\":\"perform_staged_send\",\"line_start\":1162,\"line_end\":1162,\"ordered_expression\":\"require(integer(bootstrap_read_bytes,len(stage1_raw),393216),\\\"read_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.perform_staged_send.4\",\"function\":\"perform_staged_send\",\"line_start\":1201,\"line_end\":1201,\"ordered_expression\":\"require(len(raw) <= ROOT_OUT_MAX,\\\"root_output_bound\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.perform_staged_send.5\",\"function\":\"perform_staged_send\",\"line_start\":1213,\"line_end\":1213,\"ordered_expression\":\"require(count == len(raw),\\\"pipe_write\\\")\",\"operands\":[]},{\"id\":\"sender.predicate.perform_staged_send.6\",\"function\":\"perform_staged_send\",\"line_start\":1216,\"line_end\":1216,\"ordered_expression\":\"require(wall < wall0 + 30 and mono < mono0 + 30,\\\"failed_handoff_deadline\\\")\",\"operands\":[]}]")
def source_predicate(site):
 if not(type(site) is list and len(site)==2 and type(site[0]) is str and type(site[1]) is int):raise Refusal("actual source predicate site")
 name=site[0].replace(".<locals>.","." )
 rows=[row for row in SOURCE_PREDICATES if row["line_start"]<=site[1]<=row["line_end"] and
  (row["function"]==name or name==row["function"]+".<lambda>")]
 if not(len(rows)==1):raise Refusal("one exact frozen source expression and field/prior ordering")
 return rows[0]

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

class _FailureOwner:
 def __init__(self):
  self.domain = "caller_bootstrap"
  self.session = None
  self.emergency_handle = None
  self.cleanup = self.delivery = None
  self.bootstrap_written = 0

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


PREDICATE_SITE_MAP={76: 'sender.predicate.exact.1', 90: 'sender.predicate.control_types.1', 97: 'sender.predicate.control_types.2', 107: 'sender.predicate.object_wire.pairs.1', 114: 'sender.predicate.object_wire.1', 117: 'sender.predicate.object_wire.2', 119: 'sender.predicate.object_wire.3', 122: 'sender.predicate.source_map.1', 124: 'sender.predicate.source_map.2', 128: 'sender.predicate.namespace_shape.1', 132: 'sender.predicate.namespace_shape.2', 137: 'sender.predicate.body_shape.1', 146: 'sender.predicate.custody_shape.1', 154: 'sender.predicate.custody_shape.2', 160: 'sender.predicate.context_shape.1', 163: 'sender.predicate.context_shape.2', 165: 'sender.predicate.context_shape.3', 167: 'sender.predicate.context_shape.4', 169: 'sender.predicate.context_shape.5', 173: 'sender.predicate.context_shape.6', 175: 'sender.predicate.context_shape.7', 178: 'sender.predicate.context_shape.8', 182: 'sender.predicate.expectation_shape.1', 184: 'sender.predicate.expectation_shape.2', 186: 'sender.predicate.expectation_shape.3', 193: 'sender.predicate.validate_stage1.1', 195: 'sender.predicate.validate_stage1.2', 200: 'sender.predicate.validate_stage1.3', 206: 'sender.predicate.validate_stage1.4', 211: 'sender.predicate.validate_stage1.5', 214: 'sender.predicate.validate_stage1.6', 220: 'sender.predicate.validate_stage1.7', 222: 'sender.predicate.validate_stage1.8', 225: 'sender.predicate.validate_stage1.9', 233: 'sender.predicate.validate_init.1', 234: 'sender.predicate.validate_init.2', 242: 'sender.predicate.validate_claim.1', 243: 'sender.predicate.validate_claim.2', 246: 'sender.predicate.validate_claim.3', 252: 'sender.predicate.validate_claim.4', 264: 'sender.predicate.check_held_observation.1', 270: 'sender.predicate.check_held_observation.2', 272: 'sender.predicate.check_held_observation.3', 274: 'sender.predicate.check_held_observation.4', 276: 'sender.predicate.check_held_observation.5', 279: 'sender.predicate.validate_stage2.1', 283: 'sender.predicate.validate_stage2.2', 287: 'sender.predicate.validate_stage2.3', 292: 'sender.predicate.validate_stage2.4', 295: 'sender.predicate.validate_stage2.5', 299: 'sender.predicate.validate_stage2.6', 302: 'sender.predicate.validate_stage2.7', 305: 'sender.predicate.validate_stage2.8', 417: 'sender.predicate.Clock.__init__.1', 420: 'sender.predicate.Clock.now.1', 432: 'sender.predicate.Named.__init__.1', 433: 'sender.predicate.Named.__init__.2', 447: 'sender.predicate.Named.check.1', 450: 'sender.predicate.Named.check.2', 452: 'sender.predicate.Named.check.3', 463: 'sender.predicate.Directory.__init__.1', 475: 'sender.predicate.Directory.check.1', 500: 'sender.predicate.Generation.proc_read.1', 508: 'sender.predicate.Generation.stat.1', 515: 'sender.predicate.Generation.check.1', 519: 'sender.predicate.Generation.check.2', 521: 'sender.predicate.Generation.check.3', 529: 'sender.predicate.Generation.children.1', 534: 'sender.predicate.Generation.children.2', 536: 'sender.predicate.Generation.children.3', 539: 'sender.predicate.Generation.children.4', 566: 'sender.predicate.OwnScope.__init__.1', 568: 'sender.predicate.OwnScope.__init__.2', 572: 'sender.predicate.OwnScope.__init__.3', 600: 'sender.predicate.OwnScope.proc_read.1', 606: 'sender.predicate.OwnScope.single_thread.1', 610: 'sender.predicate.OwnScope.runtime_check.1', 615: 'sender.predicate.OwnScope.runtime_check.2', 620: 'sender.predicate.OwnScope.check.1', 625: 'sender.predicate.OwnScope.children.1', 631: 'sender.predicate.OwnScope.terminal.1', 635: 'sender.predicate.OwnScope.terminal.2', 649: 'sender.predicate.Held.__init__.1', 662: 'sender.predicate.Held.flags.1', 667: 'sender.predicate.Held.check.1', 670: 'sender.predicate.Held.check.2', 673: 'sender.predicate.Held.check.3', 681: 'sender.predicate.Held.check.4', 688: 'sender.predicate.Held.check.5', 735: 'sender.predicate.IO.terminal.1', 742: 'sender.predicate.IO.terminal.2', 745: 'sender.predicate.IO.terminal.3', 755: 'sender.predicate.IO.terminal.4', 761: 'sender.predicate.IO.terminal.5', 773: 'sender.predicate.IO.pump.1', 810: 'sender.predicate.IO.write.1', 812: 'sender.predicate.IO.write.2', 829: 'sender.predicate.IO.write.3', 838: 'sender.predicate.IO.line.1', 850: 'sender.predicate.IO.line.2', 311: 'sender.predicate.validate_report.1', 314: 'sender.predicate.validate_report.2', 319: 'sender.predicate.validate_report.3', 322: 'sender.predicate.validate_report.4', 326: 'sender.predicate.validate_report.5', 336: 'sender.predicate.validate_report.6', 348: 'sender.predicate.validate_report.7', 897: 'sender.predicate.Session.fd_guard.1', 902: 'sender.predicate.Session.fd_guard.2', 904: 'sender.predicate.Session.fd_guard.3', 906: 'sender.predicate.Session.fd_guard.4', 921: 'sender.predicate.Session.prepare_inputs.1', 925: 'sender.predicate.Session.prepare_inputs.2', 928: 'sender.predicate.Session.prepare_inputs.3', 931: 'sender.predicate.Session.prepare_inputs.4', 939: 'sender.predicate.Session.prepare_inputs.5', 953: 'sender.predicate.Session.sample_tree.1', 956: 'sender.predicate.Session.sample_tree.2', 967: 'sender.predicate.Session.spawn.1', 968: 'sender.predicate.Session.spawn.2', 998: 'sender.predicate.Session.prepare_custody.1', 999: 'sender.predicate.Session.prepare_custody.2', 1012: 'sender.predicate.Session.preauth.1', 1020: 'sender.predicate.Session.preauth.2', 1023: 'sender.predicate.Session.final_namespace.1', 1081: 'sender.predicate.Session.cleanup.1', 1200: 'sender.predicate.perform_staged_send.1', 1202: 'sender.predicate.perform_staged_send.2', 1203: 'sender.predicate.perform_staged_send.3', 1242: 'sender.predicate.perform_staged_send.4', 1254: 'sender.predicate.perform_staged_send.5', 1257: 'sender.predicate.perform_staged_send.6'}
TRACE=None
_PURE=("exact","control_types","encode","object_wire","source_map","expectation_shape","body_shape","context_shape","namespace_shape","custody_shape","validate_stage1","validate_init","validate_stage2","validate_claim","validate_report","check_held_observation")
_CLASSES=(Clock,Named,Directory,Generation,OwnScope,Held,IO,Session)
def install_performing_wrappers(trace):
 def scoped(function,name):
  def invoke(*args,**kwargs):
   cleanup=name.endswith(".close") or name=="Session.cleanup"
   if not cleanup:
    trace.check()
    if "." in name:trace.check()
   return function(*args,**kwargs)
  return invoke
 saved=[]
 def undo():
  for obj,name,old in reversed(saved):
   if obj is None:globals()[name]=old
   else:setattr(obj,name,old)
 try:
  for name in _PURE:
   old=globals()[name];saved.append((None,name,old));globals()[name]=scoped(old,name)
  for cls in _CLASSES:
   for name,function in tuple(vars(cls).items()):
    if not callable(function):continue
    saved.append((cls,name,function));setattr(cls,name,scoped(function,cls.__name__+"."+name))
  return undo
 except BaseException:
  undo();raise
