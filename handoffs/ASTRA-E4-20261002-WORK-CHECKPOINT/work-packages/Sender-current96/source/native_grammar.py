"""A137 Root-only verbatim performing Caller native/read/byte recipes.
No candidate module import or effectful Source functions. These pure rules do
not establish Root origin, independent custody or all-path resource sufficiency.
FullEventJournal is an explicitly Root-chronology-bound adapter, not a callback.
"""
import hashlib
import json
import math

FullEventJournal = None

class TypedOrdinaryContract:

 CASES = ("positive","nonzero-dispatcher","body","deadline","pid-generation",
          "pin","read-write","stale-identity","unsealed","wrong-field","wrong-role",
          "adopted-child-pending","blocked-SIGCHLD","body-context-bool","clock-drift",
          "controller-output-bound","final-ancestor-boundary","handle-close-error",
          "initial-scope-nonempty","ordinary-regular-artifact-refusal","proc-acquisition-error",
          "report-custody-bool","report-namespace-bool","report-resource-bool","sender-read-bound",
          "spawn-constructor-error","strict-inherited-fd","terminal-original-end",
          "terminal-pipe-error","terminal-tree-list-missed","usage-integer-bool","wait4-unavailable")
 def __init__(self,worker_reader=None):
     if worker_reader is not None and not callable(worker_reader):
         raise ValueError("ordinary_regular_reader_type")
     self.worker_reader = worker_reader
 @staticmethod
 def read_denial(pre):
     ""
     trace = pre.get("read_trace")
     if type(trace) is not dict or trace.get("complete") is not True:
         raise ValueError("ordinary_read_denial_incomplete")
     charged = calls = 0
     denials = []
     for row in ReadJournal.replay(trace["retained"]):
         if row["outcome"] == "denied":
             keys = ("consumer","kind","requested_cap","prefix_events","prefix_returned_bytes",
                     "prefix_reserved_bytes","asserted_cap_plus_one","charged")
             denial = {key: row[key] for key in keys}
             if (row["repeat"] != 1 or row["charged_observed"] is not False
                     or row["returned_bytes"] != 0 or denial["charged"] is not False
                     or type(denial["prefix_events"]) is not int or denial["prefix_events"] != calls
                     or type(denial["prefix_returned_bytes"]) is not int
                     or denial["prefix_returned_bytes"] != charged
                     or type(denial["requested_cap"]) is not int or denial["requested_cap"] < 1
                     or denial["asserted_cap_plus_one"] is not False
                     or charged + denial["requested_cap"] <= 33554432):
                 raise ValueError("ordinary_true_sender_read_denial_unproved")
             denials.append(denial)
         if row["charged_observed"]:
             charged += row["returned_bytes"]
         calls += row["repeat"]
     if (not denials or denials[0] != pre.get("read_denied") or trace.get("denied") != denials[0]
             or denials[1:] != pre.get("later_read_denials",[])
             or trace.get("later_denials",[]) != denials[1:]
             or charged != pre.get("sender_read_bytes_observed")):
         raise ValueError("ordinary_first_and_later_denial_correspondence")
 def captured_producers(self,pre,captured):
     ""
     mapping = pre["controller_original_fd_map"]
     if mapping is None:
         if pre["controller_pipe_observation"] or pre["controlled_stdout_lines"]:
             raise ValueError("ordinary_unobserved_original_pipe_domain")
         return
     if (type(mapping) is not dict or set(mapping) != {"stdout","stderr"}
             or any(type(v) is not int or v < 0 for v in mapping.values())
             or len(set(mapping.values())) != 2
             or set(pre["controller_pipe_observation"]) != {str(v) for v in mapping.values()}):
         raise ValueError("ordinary_original_pipe_map")
     for name,fd in mapping.items():
         pipe = pre["controller_pipe_observation"][str(fd)]
         prefix = bytes.fromhex(pipe["prefix_hex"] or "")
         sample = bytes.fromhex(pipe["overflow_probe_hex"] or "")
         raw = captured[name]
         if (len(prefix) != pipe["prefix_bytes"] or not raw.startswith(prefix + sample)
                 or sample and pipe["capture_stopped"] is not True
                 or pipe["eof"] and not pipe["capture_stopped"] and raw != prefix):
             raise ValueError("ordinary_captured_producer_correspondence")
     offsets = {}
     for line in pre["controlled_stdout_lines"]:
         if (type(line) is not dict or set(line) != {"fd","bytes","sha256","role","producer",
                                                   "offset","controller_pipe"}
                 or any(type(line[k]) is not int or line[k] < 0 for k in ("fd","bytes","offset"))
                 or line["producer"] is not False or type(line["controller_pipe"]) is not bool):
             raise ValueError("ordinary_controlled_slice_type")
         fd = line["fd"]
         if line["offset"] != offsets.get(fd,0):
             raise ValueError("ordinary_controlled_slice_order")
         offsets[fd] = line["offset"] + line["bytes"]
         if line["controller_pipe"]:
             if fd != mapping["stdout"] or line["role"] not in ("controlled_claim","controlled_report"):
                 raise ValueError("ordinary_controlled_slice_role")
             raw = bytes.fromhex(pre["controller_pipe_observation"][str(fd)]["prefix_hex"] or "")
             selected = raw[line["offset"]:offsets[fd]]
             if len(selected) != line["bytes"] or hashlib.sha256(selected).hexdigest() != line["sha256"]:
                 raise ValueError("ordinary_reached_claim_report_slice")
     for path,raw in captured["worker_files"].items():
         if (self.worker_reader is None or not path or path.startswith("/")
                 or any(p in ("",".","..") for p in path.split("/"))):
             raise ValueError("ordinary_regular_artifact_reader_required")
         if self.worker_reader(path) != raw:
             raise ValueError("ordinary_worker_original_regular_receipt_mismatch")
 @staticmethod
 def source_inputs(stage1,stage2,selected_source):
     ""
     if (type(stage1) is not dict or type(stage2) is not dict
             or type(selected_source) is not dict or set(selected_source) != {"root","files"}
             or type(selected_source["root"]) is not str
             or type(selected_source["files"]) is not dict
             or set(selected_source["files"]) != {"caller.py","staged_sender.py"}):
         raise ValueError("ordinary_selected_source_input")
     selection = {"root": selected_source["root"],"files": {}}
     for name,pin in selected_source["files"].items():
         if (type(pin) is not dict or set(pin) != {"identity","sha256"}
                 or type(pin["identity"]) is not list or len(pin["identity"]) != 9
                 or any(type(v) is not int or v < 0 for v in pin["identity"])
                 or not 0 < pin["identity"][6] <= 65536
                 or type(pin["sha256"]) is not str or len(pin["sha256"]) != 64):
             raise ValueError("ordinary_current_source_pin")
         selection["files"][name] = {"identity": list(pin["identity"]),"sha256": pin["sha256"]}
     if stage1.get("sender") != selection:
         raise ValueError("ordinary_external_selection_not_current")
     first = OrdinaryNativeContract.canonical(stage1)
     second = OrdinaryNativeContract.canonical(stage2)
     if (len(first) > 131072 or len(second) > 131072
             or stage2.get("stage1_sha256") != hashlib.sha256(first).hexdigest()):
         raise ValueError("ordinary_causal_stage1_stage2_binding")
     return {"stage1_wire": first,"stage2_wire": second,"selection": selection,
             "future_producer_only": True,"Source_issued_Root_authority": False}
 def consume_stock_prelude(self,case_id,root_stdout,root_stderr,native_exit,caller_owner_reached):
     if (type(case_id) is not str or case_id not in self.CASES
             or type(root_stdout) is not bytes or type(root_stderr) is not bytes
             or type(native_exit) is not int or caller_owner_reached is not False
             or len(root_stdout) > 262144):
         raise ValueError("ordinary_actual_stock_prelude")
     return {"case": case_id,"native_exit_actual": native_exit,
             "stdout_bytes_actual": len(root_stdout),"stderr_bytes_actual": len(root_stderr),
             "caller_bootstrap_JSON_not_inferred": True,
             "runtime_credit": False,"gate_credit": False,"GO": False}
 @staticmethod
 def variables(values):
     keys = {"holder_pid","parent_pid","holder_start_ticks","wall0","mono0",
             "original_wall_end","original_monotonic_end","identities","wait4_status",
             "wait4_usage","fd_names","error_errno"}
     if type(values) is not dict or set(values) != keys:
         raise ValueError("ordinary_variable_exact_keys")
     for key in ("holder_pid","parent_pid","holder_start_ticks"):
         value = values[key]
         if value is not None and (type(value) is not int or value < 1):
             raise ValueError("ordinary_process_variable")
     for key in ("wall0","mono0","original_wall_end","original_monotonic_end"):
         value = values[key]
         if value is not None and (type(value) not in (int,float) or not math.isfinite(value)):
             raise ValueError("ordinary_clock_variable")
     if type(values["identities"]) is not list:
         raise ValueError("ordinary_identity_variable")
     for identity in values["identities"]:
         if (type(identity) is not list or len(identity) != 9
                 or any(type(v) is not int or v < 0 for v in identity)):
             raise ValueError("ordinary_identity9_variable")
     usage = values["wait4_usage"]
     if usage is not None and (type(usage) is not list or len(usage) != 16
             or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 for v in usage[:2])
             or any(type(v) is not int or v < 0 for v in usage[2:])):
         raise ValueError("ordinary_owned_wait_usage_variable")
     for key in ("wait4_status","error_errno"):
         if values[key] is not None and type(values[key]) is not int:
             raise ValueError("ordinary_status_errno_variable")
     if (type(values["fd_names"]) is not list
             or any(type(v) is not str or not v.isascii() or not v.isdecimal() for v in values["fd_names"])):
         raise ValueError("ordinary_fd_name_variable")
     return values
 def consume(self,case_id,variables,outcome,predelivery,root_stdout,native_exit,
             segments,controller_stdout,controller_stderr,worker_files,byte_producer):
     if type(case_id) is not str or case_id not in self.CASES:
         raise ValueError("ordinary_exact32_case")
     self.variables(variables)
     native = OrdinaryNativeContract()
     captured = native.producer_streams(segments,controller_stdout,controller_stderr,worker_files)
     pre = json.loads(predelivery)
     if pre.get("cause") == "caller_bootstrap" and case_id != "strict-inherited-fd":
         raise ValueError("ordinary_case_native_domain_not_permitted")
     early_body=case_id=="body" and pre.get("spawned") is False and pre.get("status")=="REFUSED" and pre.get("cause") in ("issuer_body","canonical object wire")
     if pre.get("spawned") is False and pre.get("cause") != "caller_bootstrap" and not early_body and case_id not in ("deadline","wrong-field","wrong-role","pin","body-context-bool"):
         raise ValueError("ordinary_case_pre_session_domain_not_permitted")
     if case_id == "sender-read-bound":
         self.read_denial(pre)
     self.case_relation(case_id,pre,outcome,native_exit)
     if pre.get("cause") == "caller_bootstrap" or pre.get("spawned") is False:
         native.outside_early_capture(predelivery,root_stdout,native_exit,
                                      bootstrap=pre.get("cause") == "caller_bootstrap")
         return {"case": case_id,"Source_contract_checked": False,"early_variant": True,
                 "whole_trace_transport": "NOT_PROVEN","runtime_credit": False,"GO": False}
     native.before_delivery(pre,predelivery,pre["root_custody_request_bytes_written"])
     self.captured_producers(pre,captured)
     native.after_delivery(predelivery,outcome,len(root_stdout))
     native.outside_capture(outcome,predelivery,root_stdout,native_exit)
     delivery = outcome["delivery"]
     if (variables["original_wall_end"] != delivery["original_wall_end"]
             or variables["original_monotonic_end"] != delivery["original_monotonic_end"]
             or variables["wall0"] is None or variables["mono0"] is None
             or variables["original_monotonic_end"] != variables["mono0"]
                + variables["original_wall_end"] - variables["wall0"]):
         raise ValueError("ordinary_original_clock_binding")
     if pre["root_custody_request_observations"]:
         request = json.loads(bytes.fromhex(pre["root_custody_request_observations"][0]["wire_hex"]))
         if (variables["holder_pid"] != request["holder_pid"]
                 or variables["holder_start_ticks"] != request["holder_start_ticks"]):
             raise ValueError("ordinary_actual_generation_binding")
     for _event in native.matching_bytes(pre["explicit_operation_trace"],byte_producer):
         pass
     return {"case": case_id,"Source_contract_checked": False,
             "checked_relations": "PARTIAL_RELATIONS_FULL_SOURCE_CLOSURE_NOT_PROVEN",
             "runtime_credit": False,"gate_credit": False,"GO": False}
 @staticmethod
 def case_relation(case_id,pre,returned,native_exit):
     if case_id == "positive":
         if (returned.get("status") != "RUN_REPORTED" or native_exit != 0
                 or returned.get("reported_entrypoint_returncodes") != [0]
                 or returned.get("cleanup",{}).get("confirmed") is not True
                 or returned.get("delivery",{}).get("confirmed") is not True):
             raise ValueError("ordinary_positive_causal_relation")
         return
     if native_exit != 125:
         raise ValueError("ordinary_negative_native_relation")
     if case_id == "nonzero-dispatcher":
         codes = returned.get("reported_entrypoint_returncodes")
         if (returned.get("status") != "RUN_REPORTED" or type(codes) is not list or len(codes) != 1
                 or type(codes[0]) is not int or codes[0] == 0):
             raise ValueError("ordinary_real_nonzero_dispatcher_relation")
         return
     if case_id == "ordinary-regular-artifact-refusal":
         # Successful reporting of this *negative child* is its original
         # ordinary domain, not a generic exemption for RUN_REPORTED failures.
         claim = pre.get("reported_controller_output_claim")
         if (pre.get("status") != "RUN_REPORTED" or returned.get("status") != "RUN_REPORTED"
                 or type(claim) is not dict or claim.get("status") != "RUN_RETURNED"
                 or pre.get("cause") is not None
                 or returned.get("reported_entrypoint_returncodes") != [1]
                 or returned.get("cleanup",{}).get("confirmed") is not True
                 or returned.get("delivery",{}).get("confirmed") is not True):
             raise ValueError("ordinary_actual_child_refusal_reporting_relation")
         return
     if returned.get("status") == "RUN_REPORTED":
         raise ValueError("ordinary_negative_unreached_failure")
     causes = {"body":"issuer_body|canonical object wire","deadline":"fixed_deadline",
         "pid-generation":"pid_generation","pin":"pin_mismatch","read-write":"fd_not_readonly",
         "stale-identity":"stale_identity","unsealed":"seals_incomplete","wrong-field":"exact_key_set",
         "wrong-role":"phase_or_role","blocked-SIGCHLD":"SIGCHLD_blocked",
         "body-context-bool":"phase_or_role|fixed_deadline|resources_topology_changed",
         "clock-drift":"clock drift","final-ancestor-boundary":"stale_identity",
         "initial-scope-nonempty":"initial_own_scope_not_empty","report-custody-bool":"controller_report",
         "report-namespace-bool":"ancestry_spec_mismatch|controller_report",
         "report-resource-bool":"resources_topology_changed","sender-read-bound":"read_bound",
         "usage-integer-bool":"telemetry_type"}
     events = list(FullEventJournal.replay(pre["explicit_operation_trace"]))
     if case_id in causes:
         cause = pre.get("cause")
         if cause not in causes[case_id].split("|"):
             raise ValueError("ordinary_case_first_cause_mismatch")
         if not any(e[1] == "run.failure" and e[2][0] == cause for e in events):
             raise ValueError("ordinary_case_performing_failure_unbound")
     if case_id in ("proc-acquisition-error","spawn-constructor-error"):
         operations = ("open","pidfd_open") if case_id == "proc-acquisition-error" else ("spawn.error",)
         if not any(e[1] in operations and e[3][0] == "error" and e[3][1] == pre.get("cause") for e in events):
             raise ValueError("ordinary_case_actual_constructor_acquisition_error")
     if case_id == "strict-inherited-fd":
         if pre.get("cause") != "caller_bootstrap" or pre.get("reason") != "strict_inherited_0_1_2":
             raise ValueError("ordinary_strict_entry_relation")
     if case_id in ("adopted-child-pending","terminal-tree-list-missed","wait4-unavailable","handle-close-error"):
         cleanup = returned.get("cleanup")
         if type(cleanup) is not dict or cleanup.get("confirmed") is not False:
             raise ValueError("ordinary_sticky_unknown_cleanup_relation")
     if case_id in ("terminal-original-end","terminal-pipe-error"):
         if returned.get("delivery",{}).get("confirmed") is not False:
             raise ValueError("ordinary_actual_failed_delivery_relation")
         delivery = returned["delivery"]
         if case_id == "terminal-original-end" and delivery.get("reason") != "failed_handoff_deadline":
             raise ValueError("ordinary_actual_original_end_cause")
         if case_id == "terminal-pipe-error" and delivery.get("error_type") not in ("OSError","BrokenPipeError"):
             raise ValueError("ordinary_actual_terminal_pipe_cause")
     if case_id == "controller-output-bound":
         rows = pre.get("controller_pipe_observation",{}).values()
         if not any(row.get("capture_stopped") is True and row.get("overflow_probe_hex") for row in rows):
             raise ValueError("ordinary_actual_overflow_probe_relation")
_E = None

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
