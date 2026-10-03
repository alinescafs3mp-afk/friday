"""A172 connected complete publication Source only; independent whole review and NEW Root admission required.

Neither this source, the selection DATA nor a receipt grants execution authority.
Root supplies a NEW monotonic start/deadline and independently chosen full hashes
and nine-field leaf identities. No Root grant, native API or privileged service is
implemented here. Installed stock Python3.14/Linux/libc are qualified assumptions.
"""
import base64
import datetime
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import resource
import select
import signal
import stat
import sys
import time

# Future package path only. This source does not create that directory.
PACKAGE = "/var/tmp/friday-lab865-sol060-a182-node-whole6-all35-all6-raw-bound-schema-connected-source-closure"
SOURCE = PACKAGE
BASE = "/var/tmp/friday-astra-material-acquisition-20261001-a023-g1"
TARGET = "node-v22.23.2-linux-x64.tar.xz"
PRIMARY = "CC68F5A3106FF448322E48ED27F5E38D5B0A215F"
ARCHIVE = "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307"
VHASH = "2b2e64f0062ba156b5b927a9af8960d85444e79f989a93fa180876f647bfbc60"
CHASH = "f4af798566916c32ef504f95865faceffadd5e62e247e3c8e2ce64b4cd74bb7d"
SHASH = "49b507fbc2a4b99791908e92213979056ebaa1da4ddfe733c9eab86738e79d5c"
ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
AS_OUTER = 134217728
READ_CAP, PUBLIC_CAP = 268435456, 2097152
STDOUT_CAP, STDERR_CAP = 65536, 32768
RESOURCE_SCHEMA_IDS = {"supervisor": "friday.e4.node.supervisor-resources.a172.v1",
                       "verifier": "friday.e4.node.verifier-resources.a172.v1"}
MASK = set(signal.valid_signals()) - {signal.SIGKILL, signal.SIGSTOP}
FIXED = {
    # accepted/materialized are unchanged historical DATA pins. Their old Root
    # decisions grant nothing to A172. NEW independent whole Source review and
    # actual Root materialization/qualification/admission must precede effects.
    "prlimit": ("/usr/bin/prlimit", 27616, "cb28811cb3902773c1a0f3ac0ea554c7a1e232724e9d4cae055ede54b65a7bc4", 0, 0o755),
    "python": ("/usr/bin/python3.14", 7477160, "52e0a13e60a981d8c4b6478be2ba5176f69da07948a056bf49cf6f077e30cb41", 0, 0o755),
    "gpg": ("/usr/bin/gpg", 1186456, "c8f3dc07a34ab0a0a081368b482724b79a2a6d6b7aa856741c09edf7af854754", 0, 0o755),
    "gpgv": ("/usr/bin/gpgv", 302296, "95ecd00d02b79d091f103af175b1dbf95b5b0c66503104bfcea447cca7e3829d", 0, 0o755),
    "ffi": ("/usr/lib/x86_64-linux-gnu/libffi.so.8.2.0", 64184, "1a0dc86f787f73e025a6e521056360afcbe70f2a82cd808132fefc2b4ee95daa", 0, 0o644),
    "supervisor": (SOURCE + "/supervisor.py", 101776, SHASH, 1000, 0o600),
    "verifier": (SOURCE + "/verifier.py", 73847, VHASH, 1000, 0o600),
    "contract": (SOURCE + "/launch-contract.json", 48792, CHASH, 1000, 0o600),
    "bill": ("/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-ACQUISITION-INPUT-DRIFT-SUCCESSOR-A032-G1-BILL.json", 466956, "cd973b83717015b985055482c9835e7ac1cf8311120c71461d4c0f17ad2d50a2", 1000, 0o600),
    "receipts": (BASE + "/transport-receipts.ndjson", 400979, "180c44ad4439a96f89335a62e35a5668327a228ce164d1f31b503b867b05fe9b", 1000, 0o600),
    "raw": (BASE + "/metadata/node/SHASUMS256.txt.asc", 4696, "cfcf12eb3146d641be185d08ca018166b98495b1d8e5f7f94ab77f9965b651d2", 1000, 0o600),
    "key": (BASE + "/metadata/node/publisher-key.asc", 3163, "e31e1aa40a8331f01d753cef475f7b9eab934fc25f5f0b36995bfd80bd66ad27", 1000, 0o600),
    "archive": (BASE + "/archives/node/" + TARGET, 31058332, ARCHIVE, 1000, 0o600),
    "original_archive": ("/home/jericho/.jericho/runtime/friday-toolchain22.bSt8Uu/archives/" + TARGET, 31058332, ARCHIVE, 1000, 0o600),
    "accepted": ("/home/jericho/.jericho/grok-takeover/ASTRA-E4-LAB830-NODE-SOURCE-ACCEPTED-20261001.json", 5880, "d29ad65ae1f6b1c1373c021cb1cad334bfc204f3c62c93381b7346d1fb904023", 1000, 0o600),
    "materialized": ("/home/jericho/.jericho/grok-takeover/ASTRA-E4-LAB830-ROOT-SOURCE-MATERIALIZED-20261001.json", 3724, "893dabaca55b8abe3d4fe80ef04593f30f6d1a133bd8df9c53868729965c7cbc", 1000, 0o600),
}
LOCAL = {"caller": PACKAGE + "/caller.py", "expectations": PACKAGE + "/expectations.json",
         "plain": PACKAGE + "/ordinary-surplus.txt"}
CASES = ("startup-env-negative", "startup-as-negative", "verify-node",
         "supervisor-entry-fd7", "supervisor-entry-fd127", "verifier-entry-fd7")
START = time.monotonic_ns()
HOST_START = DEADLINE = CANONICAL_END = CONTROLS_END = 0
CLEANUP_END = None
CLEANUP_ATTEMPTED = False
TERMINAL_ATTEMPTED = False
READS = 0
CHILD_READ_RESERVE = 0
PLAN_RESERVE = 0
OUTER_PID = None
LEDGER = {}
PROC_PHASE = "bootstrap"
CASE_INDEX = None
OBSERVATION_END = 0
FD_LEASE_GENERATION = 0
FD_CLOSE_ATTEMPTS = 0
FD_CLOSE_SUCCESSES = 0
FD_CLOSE_FAILURES = []
PIDFD_POLL_CALLS = 0
LEASE_CHECK_CALLS = 0
CLOCK_CALLS = 0
NATURAL_EXIT_ATTEMPTS = 0
NATURAL_EXIT_CHECKS = 6
NATURAL_EXIT_BOUND_NS = 50000000
LIFETIME_MEMORY_CONTRACT = {
    "schema": "friday.sol042.retained-lease-natural-ending.v1",
    "materialization_path": PACKAGE,
    "numeric_slot_is_not_allocation_lease": True,
    "retire_launch_aliases_at_first_close": True,
    "tracked_leases_retained_through_terminal": True,
    "strict_exit_requires_POLLIN": True,
    "optional_exit_bits": ["POLLHUP"],
    "closed_ERR_NVAL_HUP_only_exit": False,
    "status_X_Z_is_only_potential_ending": True,
    "natural_exit_bound_ns": NATURAL_EXIT_BOUND_NS,
    "natural_exit_max_checks": NATURAL_EXIT_CHECKS,
    "max_ending_attempts_whole_caller": 32,
    "phase_shared_deadline_never_extended": True,
    "signals_or_reopen_for_ending_proof": False,
    "fabricated_live_memory": False,
    "observed_credential_parent_tick_resource_errors_sticky": True,
    "first_last_genuine_live_samples_preserved": True,
    "unresolved_ending_is_original_sticky_refusal": True,
    "execution_authority": False,
}
PLAN_LOCAL9 = {}
FD_NAME_BOUND = 4096
SELECTION_SLOT = 65537
PROC_BUDGET = 0
CALLER_BYTES = FULL19_BYTES = 0
SUBSET_SUPERVISOR_BYTES = SUBSET_VERIFIER_BYTES = 0
EXPECTATIONS_BYTES = PLAIN_BYTES = 0
# One real overflow byte per inclusive stream. EOF returns zero bytes; request
# admission never spends unused capacity, however fragmented the stream is.
TRANSPORT_CASE = STDOUT_CAP + STDERR_CAP + 2
_STAT_REQ = 16384 + 1
_FDINFO_REQ = 4096 + 1
_CHILD_REQ = 4096 + 1
_NAME_REQ = 256  # stock Linux NAME_MAX plus the charged name terminator
_DIR_REQ = FD_NAME_BOUND + _NAME_REQ
_RECORD_MAX = 32
_FREEZE_MAX = 16
_BIND_REQ = 2 * _STAT_REQ + _FDINFO_REQ
_GEN_REQ = _FDINFO_REQ + _STAT_REQ
_OBSERVE_REQ = 2 * _FDINFO_REQ + 3 * _STAT_REQ
_SAMPLE_REQ = _DIR_REQ + _STAT_REQ
# Pre-release observe may reread status and generation once when Vm* is absent.
# A complete live tuple does not spend this reserve. Later rechecks use proc_running.
_EXIT_RECHECK_REQ = _STAT_REQ + _GEN_REQ
PROC_BOOTSTRAP = _DIR_REQ + _SAMPLE_REQ + _CHILD_REQ
PROC_CASE_LAUNCH = _CHILD_REQ + _BIND_REQ + _OBSERVE_REQ + _EXIT_RECHECK_REQ + _SAMPLE_REQ
PROC_CASE_CLOSE = 2 * _CHILD_REQ + _SAMPLE_REQ
# One cleanup, with <=32 registered generations over the WHOLE caller, not
# 32 new generations on every pass. reconcile caches detached old generations;
# each adopted record is synchronously stopped/reaped before another pass.
# One finite cleanup. Reconciliation reserves BOTH mutually exclusive full
# bind and cached-generation branches, conservatively, not an actual IO claim.
# The narrowed tree never rereads the parent's children to excuse a child error.
PROC_CLEANUP_PARTS = {
    "freeze16_fdinfo_stat_status": _FREEZE_MAX * (_GEN_REQ + _STAT_REQ),
    "tree32_generation_children": _RECORD_MAX * (_GEN_REQ + _CHILD_REQ),
    "tree32_reconcile_bind_or_cached": _RECORD_MAX * (_BIND_REQ + _GEN_REQ),
    "descendant31_presignal_generation": (_RECORD_MAX - 1) * _GEN_REQ,
    "direct_presignal_target": _FDINFO_REQ,
    "adopted32_reconcile_and_presignal": _RECORD_MAX * (_BIND_REQ + _GEN_REQ + _FDINFO_REQ),
    "tracked32_unknown_wait_target": _RECORD_MAX * _FDINFO_REQ,
    "own_children_baseline_while33_adoption32_final": (2 * _RECORD_MAX + 3) * _CHILD_REQ,
}
PROC_CLEANUP = sum(PROC_CLEANUP_PARTS.values())
PROC_TERMINAL = _CHILD_REQ + _SAMPLE_REQ
PUBLIC_METADATA_PARTS = {
    "fixed19_custody_selection_plan_and_expectations": 32768,
    "owned32_complete_records_including_ending_and_original_leases": _RECORD_MAX * 5120,
    "entry_six_running_six_completed_terminal_resources": (2 * len(CASES) + 2) * 4096,
    "six_case_fixed_metadata": len(CASES) * 4096,
    "finite35_case_cleanup_terminal_failures": 35 * 2048,
    "known256_both_descriptor_closure_error_copies": 256 * 512,
    "fd_lease_policy_counters_and_bounded_native_check_plan": 8192,
}
PUBLIC_FIXED_RESERVE = sum(PUBLIC_METADATA_PARTS.values())
# The exact verifier's original producer limit remains32768, not a fit claim.
# Full parsed supervisor/inner objects have exact reversible same-raw preimages;
# wire_receipt/recover_wire_receipt verify their complete original bodies.
# Every new history/error metadata branch still requires independent bounds.
# Reserve BOTH raw channels in EVERY case, including validation-red cases which
# can continue. Reserve an inclusive overflow byte on every channel as well.
# For raw n bytes, ordinary quoted ASCII is <=2*n+2. Exceptional full base64
# plus the retained null text is <=4*ceil(n/3)+6. Both fit 2*(cap+1)+16.
# Keys/colons/commas/encoding-reason fields belong only to fixed metadata.
PUBLIC_RAW_STDOUT = 2 * (STDOUT_CAP + 1) + 16
PUBLIC_RAW_STDERR = 2 * (STDERR_CAP + 1) + 16
PUBLIC_INNER_CAP = 32768
PUBLIC_PAYLOAD_KEYS = frozenset(("stdout_text", "stderr_text", "stdout_raw_base64",
    "stderr_raw_base64", "stdout_pending_raw_base64", "stderr_pending_raw_base64"))
PUBLIC_CASE_PAYLOAD_BOUNDS = tuple(
    PUBLIC_RAW_STDOUT + PUBLIC_RAW_STDERR
    for i in range(len(CASES)))
PUBLIC_STREAM_RESERVE = sum(PUBLIC_CASE_PAYLOAD_BOUNDS)
PUBLICATION_SOURCE_CONTRACT = {
    "schema": "friday.sol060.lossless-raw-preimage-publication.v1",
    "fixed_metadata_bound_bytes": PUBLIC_FIXED_RESERVE,
    "case_payload_bound_bytes": list(PUBLIC_CASE_PAYLOAD_BOUNDS),
    "all_case_payload_bound_bytes": PUBLIC_STREAM_RESERVE,
    "complete_bound_with_newline_bytes": PUBLIC_FIXED_RESERVE + PUBLIC_STREAM_RESERVE + 1,
    "inner_complete_producer_cap_bytes": PUBLIC_INNER_CAP,
    "every_raw_stdout_and_stderr_reserved": True,
    "overflow_byte_reserved_on_every_stream": True,
    "whole_metadata_reserved_once": True,
    "past_payloads_measured_future_payloads_reserved": True,
    "field_removal_or_peak_subtraction": False,
    "fixed_producer_encoding": "full raw once; exact canonical same-object preimage refs, checked reversible before emission",
    "execution_authority": False,
}
STOP = False
DIRS, HELD, OWN = {}, {}, {}
TRACKED = []
ACTIVE = None
LAUNCH_OWNERS = []
SELECTED = None
SELECT_META = None
R = {"schema": "friday.e4.node.ordinary-stock-outer.sol060.v1", "status": "NOT_PROVEN",
     "qualified_node_only": False, "GO": False, "gate_release_credit": False,
     "Root_image_proven": False, "RAM_IO_proven": False,
     "execution_authority": "External genuine Root native tool only; selection is ordinary DATA",
     "qualified_dependencies": "Root separately admits stock Python3.14/Linux/libc/stdlib and exact current caller; installed tools/loader dependencies are assumed, not reconstructed",
     "cases": [{"case": name, "status": "NOT_RUN", "reason": "not reached"} for name in CASES],
     "custody": {}, "custody_phases": [], "resources": [], "ownership": [],
     "explicit_reads": {"budget_bytes": READ_CAP, "implicit_IO": "UNKNOWN_NOT_ZERO",
                         "descendant_reads": "NOT_CHARGED_TO_OUTER; inner separate ledger retained",
                         "reset_or_inherited_peak_subtraction": False}}


class Refusal(Exception):
    pass


def require(ok, message):
    if not ok:
        raise Refusal(message)


def uncertain(message):
    global STOP
    STOP = True
    raise Refusal("STOP_UNCONFIRMED: " + message)


def now():
    global CLOCK_CALLS
    CLOCK_CALLS += 1
    return time.monotonic_ns()


def due(end=None):
    require(DEADLINE > 0 and now() < (DEADLINE if end is None else min(DEADLINE, end)),
            "single external deadline/reserve exceeded")



def sync_plan():
    global PLAN_RESERVE
    PLAN_RESERVE = sum(LEDGER.values())


def plan_fits(extra):
    require(type(extra) is int and extra >= 0, "invalid explicit read request")
    require(READS + CHILD_READ_RESERVE + PLAN_RESERVE + extra <= READ_CAP,
            "read capacity unavailable before syscall")


def take_budget(bucket, request):
    require(type(request) is int and request >= 0 and bucket in LEDGER, "unknown read budget")
    require(LEDGER[bucket] >= request, "read budget unavailable before syscall")
    LEDGER[bucket] -= request
    sync_plan()
    plan_fits(request)


def take_bounded(bucket, request, slot):
    require(type(request) is int and type(slot) is int and 0 <= request <= slot,
            "invalid bounded read request")
    require(LEDGER[bucket] >= slot, "read budget unavailable before syscall")
    LEDGER[bucket] -= slot
    sync_plan()
    plan_fits(request)


def proc_bucket():
    if PROC_PHASE in ("launch", "close"):
        require(type(CASE_INDEX) is int and 0 <= CASE_INDEX < len(CASES),
                "actual active case index missing")
        return "proc_" + PROC_PHASE + "_" + str(CASE_INDEX)
    require(PROC_PHASE in ("bootstrap", "running", "cleanup", "terminal"),
            "unknown finite proc phase")
    return "proc_" + PROC_PHASE


def admit_request(bucket, request):
    # Admission leaves the allowance in its OWN bucket. Neither a short read,
    # zero-byte EOF nor EAGAIN needs a refund, reset or subtraction from READS.
    require(type(request) is int and request > 0 and bucket in LEDGER,
            "invalid bounded explicit read request")
    require(LEDGER[bucket] >= request, "read budget unavailable before syscall: " + bucket)
    plan_fits(0)


def charge_returned(bucket, request, size):
    global READS
    require(type(size) is int and 0 <= size <= request <= LEDGER[bucket],
            "actual read exceeded admitted request")
    LEDGER[bucket] -= size
    READS += size
    sync_plan()
    plan_fits(0)


def install_plan():
    global OUTER_PID, LEDGER, CALLER_BYTES, FULL19_BYTES, EXPECTATIONS_BYTES, PLAIN_BYTES
    global SUBSET_SUPERVISOR_BYTES, SUBSET_VERIFIER_BYTES, PROC_BUDGET, PLAN_LOCAL9
    OUTER_PID = os.getpid()
    require(type(OUTER_PID) is int and OUTER_PID > 0, "actual positive numeric process id required")
    # Metadata does not read content or create an input descriptor. Root still
    # chooses the independent DATA/full9/SHA; hold later matches these exact
    # local lengths/identities before any source becomes an admitted input.
    PLAN_LOCAL9 = {name: full9(os.stat(path, follow_symlinks=False))
                   for name, path in LOCAL.items()}
    for fields in PLAN_LOCAL9.values():
        require(stat.S_ISREG(fields[2]) and stat.S_IMODE(fields[2]) == 0o600
                and fields[3:6] == [1000, 1000, 1] and 0 < fields[6] <= 33554432,
                "actual final local Source lengths/private leaves required")
    CALLER_BYTES = PLAN_LOCAL9["caller"][6]
    EXPECTATIONS_BYTES = PLAN_LOCAL9["expectations"][6]
    PLAIN_BYTES = PLAN_LOCAL9["plain"][6]
    sizes = {name: item[1] for name, item in FIXED.items()}
    sizes.update({name: fields[6] for name, fields in PLAN_LOCAL9.items()})
    FULL19_BYTES = sum(sizes.values())
    SUBSET_SUPERVISOR_BYTES = sum(sizes[name] for name in
        ("prlimit", "python", "supervisor", "contract", "caller", "expectations", "plain"))
    SUBSET_VERIFIER_BYTES = sum(sizes[name] for name in
        ("prlimit", "python", "verifier", "contract", "caller", "expectations", "plain"))
    sup_pass = SUBSET_SUPERVISOR_BYTES + 7
    ver_pass = SUBSET_VERIFIER_BYTES + 7
    LEDGER = {
        "hash_initial": FULL19_BYTES + 19,
        "hash_subsets": 10 * sup_pass + 2 * ver_pass,
        "hash_terminal": FULL19_BYTES + 19,
        "selection": 14 * SELECTION_SLOT,
        "child": 6 * 32768,
        "proc_bootstrap": PROC_BOOTSTRAP,
        "proc_cleanup": PROC_CLEANUP,
        "proc_terminal": PROC_TERMINAL,
        "expectations_body": EXPECTATIONS_BYTES + 1,
    }
    for index in range(len(CASES)):
        LEDGER["transport_" + str(index)] = TRANSPORT_CASE
        LEDGER["proc_launch_" + str(index)] = PROC_CASE_LAUNCH
        LEDGER["proc_close_" + str(index)] = PROC_CASE_CLOSE
    PROC_BUDGET = READ_CAP - sum(LEDGER.values())
    require(PROC_BUDGET >= _OBSERVE_REQ + _EXIT_RECHECK_REQ, "whole performing read plan does not fit before admission")
    LEDGER["proc_running"] = PROC_BUDGET
    sync_plan()
    require(READS == 0 and CHILD_READ_RESERVE == 0,
            "performing plan does not fit before admission")
    plan_fits(0)
    require(PUBLIC_FIXED_RESERVE + PUBLIC_STREAM_RESERVE + 1 <= PUBLIC_CAP,
            "whole complete serialization reserve does not fit before admission")
    R["explicit_reads"]["performing_plan"] = {
        "reserved_before_first_explicit_read": True,
        "outer_cap_bytes": READ_CAP,
        "plan_reserve_bytes": PLAN_RESERVE,
        "buckets": dict(LEDGER),
        "caller_bytes": CALLER_BYTES,
        "full19_bytes": FULL19_BYTES,
        "selection_slots": 14,
        "selection_slot_bytes": SELECTION_SLOT,
        "child_debit_bytes": 196608,
        "transport_case_bytes": TRANSPORT_CASE,
        "poll_iterations": "VARIABLE_BOUNDED_BY_SEPARATE_RUNNING_BUCKET",
        "withheld_from_running": ["proc_cleanup", "proc_terminal", "hash_terminal",
                                  "selection", "proc_launch_i", "proc_close_i", "transport_other_i"],
        "proc_cleanup_bytes": PROC_CLEANUP,
        "proc_cleanup_components": dict(PROC_CLEANUP_PARTS),
        "proc_terminal_bytes": PROC_TERMINAL,
        "proc_case_launch_bytes": PROC_CASE_LAUNCH,
        "exit_recheck_bytes": _EXIT_RECHECK_REQ,
        "exit_recheck_scope": "proc_launch_i reserves one status plus generation recheck; later rechecks use proc_running",
        "natural_ending_check_plan": {"checks_per_attempt": NATURAL_EXIT_CHECKS,
            "absolute_bound_ns": NATURAL_EXIT_BOUND_NS,
            "new_content_reads": 0,
            "native_operations": "same original held lease fstat + strict poll + monotonic clocks; actual poll calls counted",
            "implicit_io": "UNKNOWN_NOT_ZERO; zero new proc-content reads is not zero native/implicit IO",
            "phase_end": "min(existing operation end, shared deadline, start+50ms); never reset host/cleanup clocks",
            "signal_calls": 0, "reopen_calls": 0, "live_memory_substitution": False},
        "proc_case_close_bytes": PROC_CASE_CLOSE,
        "serialization_reserved_bytes": PUBLIC_FIXED_RESERVE + PUBLIC_STREAM_RESERVE + 1,
        "serialization_fixed_bytes": PUBLIC_FIXED_RESERVE,
        "serialization_stream_bytes": PUBLIC_STREAM_RESERVE,
        "serialization_metadata_consumers": dict(PUBLIC_METADATA_PARTS),
        "serialization_disjoint_source_contract": dict(PUBLICATION_SOURCE_CONTRACT),
        "raw_fallback": "lossless base64 only when ASCII text has >2x JSON escaping",
        "cleanup_invocations": 1,
        "freeze_observations": _FREEZE_MAX,
        "dedup_or_reset": False,
        "copied_parent_prefix": False,
        "inherited_peak_subtracted": False,
        "host_rss": "UNKNOWN",
        "implicit_io": "UNKNOWN_NOT_ZERO",
    }


def remaining_cases_fit(index):
    left = len(CASES) - index
    sup_left = sum(1 for name in CASES[index:] if name != "verifier-entry-fd7")
    ver_left = left - sup_left
    need_sub = (sup_left * 2 * (SUBSET_SUPERVISOR_BYTES + 7)
                + ver_left * 2 * (SUBSET_VERIFIER_BYTES + 7))
    need_sel = (2 * left + 1) * SELECTION_SLOT
    return (LEDGER["hash_subsets"] >= need_sub and
            LEDGER["selection"] >= need_sel and
            LEDGER["child"] >= 32768 * left and
            all(LEDGER["transport_" + str(i)] >= TRANSPORT_CASE and
                LEDGER["proc_launch_" + str(i)] >= PROC_CASE_LAUNCH and
                LEDGER["proc_close_" + str(i)] >= PROC_CASE_CLOSE for i in range(index, len(CASES))) and
            LEDGER["proc_cleanup"] >= PROC_CLEANUP and
            LEDGER["proc_terminal"] >= PROC_TERMINAL and
            LEDGER["hash_terminal"] >= FULL19_BYTES + 19 and
            READS + CHILD_READ_RESERVE + PLAN_RESERVE <= READ_CAP)


def own_fd_names():
    pid = os.getpid()
    require(type(pid) is int and pid > 0, "actual positive numeric process id required")
    # The forked child lists its own numeric fd table inside the fixed child
    # debit reserved before fork. That copy is not the outer ledger.
    outer = pid == OUTER_PID
    names, actual = [], 0
    bucket = proc_bucket() if outer else None
    if outer:
        admit_request(bucket, _DIR_REQ)
    # Iterator kernel buffering is UNKNOWN implicit IO, not a zero claim.
    # Explicitly surfaced complete names are charged individually. One bounded
    # next NAME_MAX request can observe EOF even at the inclusive byte limit.
    with os.scandir("/proc/" + str(pid) + "/fd") as entries:
        while True:
            if outer:
                admit_request(bucket, _NAME_REQ)
            try:
                entry = next(entries)
            except StopIteration:
                break
            name = entry.name
            size = len(name.encode("ascii", "strict")) + 1
            if outer:
                charge_returned(bucket, _NAME_REQ, size)
            actual += size
            names.append(name)
            require(actual <= FD_NAME_BOUND, "own fd name list exceeded declared bound")
    return names


def charge(size):
    global READS
    require(type(size) is int and size >= 0, "invalid explicit read charge")
    READS += size
    require(READS + CHILD_READ_RESERVE + PLAN_RESERVE <= READ_CAP,
            "outer cumulative read ceiling exceeded")


def full9(s):
    return [s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def dir_identity(s):
    # Directory size/times change with owned private home creation. No immutable
    # directory byte-image claim is made; identity/owner/type/mode stay pinned.
    return [s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid]


ERROR_OBJECT_CAP = 256
ERROR_OBJECTS = []


def retain_error_object(exc):
    if len(ERROR_OBJECTS) >= ERROR_OBJECT_CAP:
        raise Refusal("prefunded error-object arena exhausted; original exception retained") from exc
    ERROR_OBJECTS.append(exc)


def exact_error(exc):
    # Original native Python objects/tracebacks stay in the same owner lifetime.
    # Lossless stock value graph includes UnicodeDecodeError's original bytes.
    retain_error_object(exc)
    objects, nodes, values, cells = [exc], [], [], []
    def ref(value):
        if value is None:
            return None
        for i, old in enumerate(objects):
            if old is value:
                return i
        if len(objects) >= 128:
            raise Refusal("prefunded exception-node arena exhausted") from exc
        objects.append(value)
        return len(objects) - 1
    def val(value):
        if type(value) in (type(None), bool, int, str):
            return {"literal": value}
        if type(value) is float:
            # Exact binary floating value, no JSON NaN or lossy decimal inference.
            return {"float_hex": value.hex()}
        for i, old in enumerate(values):
            if old is value:
                return {"value_ref": i}
        require(type(value) in (bytes, bytearray, list, tuple, dict),
                "unsupported native error value; original object retained, not proof")
        if len(values) >= 128:
            raise Refusal("prefunded error-value arena exhausted") from exc
        values.append(value)
        return {"value_ref": len(values) - 1}
    i = 0
    while i < len(objects):
        obj = objects[i]
        frames, tb = [], obj.__traceback__
        while tb is not None:
            if len(frames) >= 128:
                raise Refusal("prefunded traceback arena exhausted") from exc
            frames.append({"file": tb.tb_frame.f_code.co_filename,
                           "function": tb.tb_frame.f_code.co_name,
                           "line": tb.tb_lineno, "lasti": tb.tb_lasti})
            tb = tb.tb_next
        nodes.append({"id": i, "module": type(obj).__module__,
            "type": type(obj).__name__, "message": str(obj), "args": val(obj.args),
            "state": val(vars(obj)), "errno": val(getattr(obj, "errno", None)),
            "filename": val(getattr(obj, "filename", None)),
            "filename2": val(getattr(obj, "filename2", None)),
            "notes": val(getattr(obj, "__notes__", None)), "traceback": frames,
            "cause": ref(obj.__cause__), "context": ref(obj.__context__),
            "suppress_context": obj.__suppress_context__,
            "group": [ref(e) for e in obj.exceptions] if isinstance(obj, BaseExceptionGroup) else None})
        i += 1
    i = 0
    while i < len(values):
        obj = values[i]
        if type(obj) in (bytes, bytearray):
            cell = {"id": i, "kind": type(obj).__name__,
                    "bytes": len(obj), "sha256": hashlib.sha256(obj).hexdigest(),
                    "base64": base64.b64encode(obj).decode("ascii")}
        elif type(obj) in (list, tuple):
            cell = {"id": i, "kind": type(obj).__name__, "items": [val(v) for v in obj]}
        else:
            cell = {"id": i, "kind": "dict", "pairs": [[val(k), val(v)] for k, v in obj.items()]}
        cells.append(cell)
        i += 1
    require(nodes and nodes[0]["id"] == 0, "error-graph root missing")
    stock = {"type": nodes[0]["type"], "message": nodes[0]["message"]}
    return {"schema": "friday.sol060.error-graph.v2", "root": 0,
            "nodes": nodes, "values": cells, "stock_cause": stock,
            "native_object_reconstruction": "NOT_QUALIFIED"}


def original_stock_cause(graph):
    # Recompute the original stock type and message from the retained root.
    # The graph remains the failure value. A two-key dict is not the receipt.
    require(type(graph) is dict and graph.get("schema") == "friday.sol060.error-graph.v2"
            and type(graph.get("root")) is int and type(graph.get("nodes")) is list
            and type(graph.get("values")) is list and 0 <= graph["root"] < len(graph["nodes"]),
            "exact error-graph.v2 root required")
    node = graph["nodes"][graph["root"]]
    require(type(node) is dict and node.get("id") == graph["root"]
            and type(node.get("type")) is str and type(node.get("message")) is str,
            "error-graph root lacks original stock type/message")
    stock = {"type": node["type"], "message": node["message"]}
    require(graph.get("stock_cause") == stock, "stock_cause is not the retained root type/message")
    return stock


FD_ALLOCATION_CAP = 256
FD_ALLOCATION_USED = 0
FD_ALLOCATION_HISTORY = [None] * FD_ALLOCATION_CAP
SELECT_CLOSE = {"attempted": False, "state": "NOT_ATTEMPTED", "error_object": None}


class OwnedFD(int):
    """One allocation lease, not merely a reusable numeric descriptor slot."""
    def __new__(cls, number, role, generation):
        value = int.__new__(cls, number)
        value.role = role
        value.lease_generation = generation
        value.identity9 = None
        value.generation_record = None
        value.terminal_proc_identity9 = None
        value.retired = False
        value.close_failed = False
        value.allocation_record = None
        return value


def register_fd(fd, role, record=None):
    global FD_LEASE_GENERATION
    require(type(fd) is int and fd >= 0 and fd not in OWN,
            "new allocation conflicts with a retained owned lease")
    FD_LEASE_GENERATION += 1
    lease = OwnedFD(fd, role, FD_LEASE_GENERATION)
    if record is not None:
        record["lease"] = lease
        lease.allocation_record = record
    OWN[fd] = lease
    lease.identity9 = full9(os.fstat(fd))
    return lease


def preown_fd(role):
    global FD_ALLOCATION_USED
    record = {"role": role, "number": None, "lease": None, "state": "PLANNED",
              "attempted": False, "error_object": None, "error": None}
    require(FD_ALLOCATION_USED < FD_ALLOCATION_CAP,
            "prefunded caller allocation arena exhausted before native acquire")
    FD_ALLOCATION_HISTORY[FD_ALLOCATION_USED] = record
    FD_ALLOCATION_USED += 1
    return record


def publish_fd(record, number):
    record["number"], record["state"] = number, "RETURNED"
    lease = register_fd(number, record["role"], record)
    # register_fd/fstat failure is still owned by the prospective history above.
    record["lease"] = lease
    lease.allocation_record = record
    return lease


def allocate_owned(operation, role, *args, **kwargs):
    record = preown_fd(role)
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
    try:
        record["state"] = "ACQUIRE_UNKNOWN"
        number = operation(*args, **kwargs)
        return publish_fd(record, number)
    except BaseException as exc:
        record["error_object"] = exc
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def allocate_pipe(role):
    records = [preown_fd(role + ":" + str(i)) for i in (0, 1)]
    leases = [None, None]
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
    try:
        for record in records:
            record["state"] = "ACQUIRE_UNKNOWN"
        numbers = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        records[0]["number"], records[1]["number"] = numbers
        records[0]["state"] = records[1]["state"] = "RETURNED"
        for i, record in enumerate(records):
            leases[i] = publish_fd(record, numbers[i])
        return leases
    except BaseException as exc:
        for record in records:
            record["error_object"] = exc
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def require_lease(fd, retiring=False):
    global LEASE_CHECK_CALLS
    LEASE_CHECK_CALLS += 1
    require(isinstance(fd, OwnedFD) and not fd.retired and
            OWN.get(int(fd)) is fd and fd.identity9 is not None,
            "closed/recycled/changed descriptor is not the retained original lease")
    held = os.fstat(fd)
    if retiring and fd.terminal_proc_identity9 is not None:
        # A terminal token belongs only to this registered held /proc generation.
        # The terminal observer captured ALL9 before ANY tracked handle closes.
        # No token, unknown fstat, drift after that capture, or recycled alias can
        # enter this branch. Readonly byte inputs never receive such a token.
        record = fd.generation_record
        require(record is not None and any(item is record for item in TRACKED) and
                record["procfd"] is fd and record["evidence"]["exit_confirmed"] is True and
                record["detached"] is True and
                full9(held) == fd.terminal_proc_identity9,
                "terminal held proc lease no longer matches captured generation")
        return fd
    # Keep the original mutable-directory contract: dev/inode/type/owner/mode,
    # not a false immutable directory-size/time claim across home creation.
    require((dir_identity(held) == fd.identity9[:5] if stat.S_ISDIR(held.st_mode)
             else full9(held) == fd.identity9), "owned held descriptor identity changed")
    return fd


def close_owned(fd):
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
    try:
        return retire_and_close_owned(fd)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)


def retire_and_close_owned(fd):
    global FD_CLOSE_ATTEMPTS, FD_CLOSE_SUCCESSES, STOP
    if fd is None:
        return
    require(isinstance(fd, OwnedFD), "unregistered numeric close refused")
    if fd.retired:
        require(not fd.close_failed, "earlier owned descriptor close unconfirmed; no retry")
        return  # A retired old alias can NEVER close a later lease of this slot.
    if fd.identity9 is None:
        require(OWN.get(int(fd)) is fd,
                "unobserved exact allocation is not the retained original lease")
        # This exact masked syscall-returned allocation has never been exposed
        # as an admitted input/process descriptor. Only closing it is permitted.
    else:
        require_lease(fd, retiring=True)
    fd.retired = True
    del OWN[int(fd)]
    record = fd.allocation_record
    if record is not None:
        record["attempted"], record["state"] = True, "RETIRE_UNKNOWN"
    FD_CLOSE_ATTEMPTS += 1
    try:
        os.close(fd)
    except BaseException as exc:
        # Retire BEFORE syscall: EINTR/other unknown close outcomes must never
        # cause a second close on a potentially already reused numeric slot.
        fd.close_failed = True
        STOP = True
        graph = exact_error(exc)
        if record is not None:
            record["error_object"] = exc
            record["error"] = graph
        require(len(FD_CLOSE_FAILURES) < 256, "bounded closure failure evidence exceeded")
        FD_CLOSE_FAILURES.append({"fd": int(fd), "lease_generation": fd.lease_generation,
            "role": fd.role, "type": type(exc).__name__, "errno": getattr(exc, "errno", None),
            "error": graph})
        raise
    FD_CLOSE_SUCCESSES += 1
    if record is not None:
        record["state"] = "CONFIRMED_CLOSED"


def close_batch(leases):
    first = None
    for lease in leases:
        try:
            close_owned(lease)
        except BaseException as exc:
            globals()["STOP"] = True
            first = first or exc
    if first is not None:
        raise first


def component(path):
    require(path.startswith("/") and "//" not in path, "noncanonical fixed path")
    if path in DIRS:
        return require_lease(DIRS[path]["fd"])
    parent, leaf = os.path.split(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    if path == "/":
        before = os.stat("/", follow_symlinks=False)
        fd = allocate_owned(os.open, "component:/", "/", flags)
        after = os.stat("/", follow_symlinks=False)
    else:
        require(leaf not in ("", ".", ".."), "invalid component")
        pfd = component(parent or "/")
        before = os.stat(leaf, dir_fd=pfd, follow_symlinks=False)
        fd = allocate_owned(os.open, "component:" + path, leaf, flags, dir_fd=pfd)
        after = os.stat(leaf, dir_fd=pfd, follow_symlinks=False)
    require(stat.S_ISDIR(before.st_mode) and dir_identity(before) ==
            dir_identity(os.fstat(fd)) == dir_identity(after), "component before/opened/after race")
    DIRS[path] = {"fd": fd, "identity": dir_identity(before)}
    return fd


def named(path):
    parent, leaf = os.path.split(path)
    return os.stat(leaf, dir_fd=component(parent), follow_symlinks=False)


def read_at(fd, count, offset):
    if fd != 3:
        require_lease(fd)
    plan_fits(count)
    data = os.pread(fd, count, offset)
    charge(len(data))
    return data


def hash_fd(fd, size, end=None):
    if fd != 3:
        require_lease(fd)
    before = full9(os.fstat(fd))
    require(before[6] == size and 0 <= size <= 33554432, "leaf hash size bound")
    h, offset = hashlib.sha256(), 0
    while offset < size:
        due(end)
        block = read_at(fd, min(65536, size - offset), offset)
        require(block, "short full-byte pin")
        h.update(block)
        offset += len(block)
    require(read_at(fd, 1, offset) == b"" and full9(os.fstat(fd)) == before,
            "EOF/metadata changed while full hashing")
    return h.hexdigest()


def hold(name, selection):
    item = selection[name]
    path, size, sha = item["path"], item["size"], item["sha256"]
    uid, mode = FIXED[name][3:] if name in FIXED else (1000, 0o600)
    before = named(path)
    parent, leaf = os.path.split(path)
    fd = allocate_owned(os.open, "input:" + name, leaf,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=component(parent))
    opened, after = os.fstat(fd), named(path)
    expected = item["identity9"]
    if name in LOCAL:
        require(expected == PLAN_LOCAL9[name], "final local Source changed after whole-plan admission")
    require(full9(before) == full9(opened) == full9(after) == expected,
            "independently selected full9 before/opened/after mismatch: " + name)
    require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and opened.st_uid == uid
            and opened.st_gid == uid and stat.S_IMODE(opened.st_mode) == mode,
            "fixed leaf owner/mode/type/nlink mismatch: " + name)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    require(flags & os.O_ACCMODE == os.O_RDONLY and not flags & (os.O_PATH | os.O_APPEND)
            and fcntl.fcntl(fd, fcntl.F_GETFD) == fcntl.FD_CLOEXEC,
            "held leaf must be ordinary readonly CLOEXEC")
    HELD[name] = {"fd": fd, "path": path, "size": size, "sha256": sha,
                  "identity9": expected, "status_flags": flags}
    take_budget("hash_initial", size + 1)
    require(hash_fd(fd, size, DEADLINE - 45000000000) == sha,
            "independent whole-byte pin mismatch: " + name)
    require(full9(named(path)) == full9(os.fstat(fd)) == expected,
            "named metadata drift after full hash")
    R["custody"][name] = {"path": path, "size": size, "sha256": sha,
                           "identity9": expected, "readonly": True,
                           "named_before_opened_after": True, "full_sha_observed": True}


def custody(phase, hash_names=(), end=None):
    for path, item in DIRS.items():
        due(end)
        require_lease(item["fd"])
        current = os.stat("/", follow_symlinks=False) if path == "/" else named(path)
        require(dir_identity(os.fstat(item["fd"])) == dir_identity(current) == item["identity"],
                "held component drift: " + path)
    for name, item in HELD.items():
        due(end)
        require_lease(item["fd"])
        require(full9(named(item["path"])) == full9(os.fstat(item["fd"])) == item["identity9"]
                and fcntl.fcntl(item["fd"], fcntl.F_GETFL) == item["status_flags"]
                and fcntl.fcntl(item["fd"], fcntl.F_GETFD) == fcntl.FD_CLOEXEC,
                "held readonly leaf path/full9/flags drift: " + name)
        if name in hash_names:
            take_budget("hash_terminal" if str(phase).startswith("terminal") else "hash_subsets",
                        item["size"] + 1)
            require(hash_fd(item["fd"], item["size"], end) == item["sha256"],
                    "held full SHA drift: " + name)
            require(full9(named(item["path"])) == item["identity9"], "hash named-after drift")
    require(full9(os.fstat(3)) == SELECT_META, "ordinary selected DATA identity changed")
    take_bounded("selection", SELECT_META[6] + 1, SELECTION_SLOT)
    require(hash_fd(3, SELECT_META[6], end) == R["selection_data"]["sha256"],
            "ordinary selected DATA changed")
    R["custody_phases"].append({"phase": phase, "all_components_full9_leaves": True,
                               "full_hash_names": list(hash_names), "at_ns": now()})


def proc_read(pfd, leaf, cap=16384):
    require_lease(pfd)
    fd = allocate_owned(os.open, "proc-read:" + leaf, leaf,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
    try:
        bucket = proc_bucket()
        admit_request(bucket, cap + 1)
        content = bytearray()
        while True:
            due(CLEANUP_END if PROC_PHASE == "cleanup" else None)
            request = min(4096, cap + 1 - len(content))
            admit_request(bucket, request)
            data = os.read(fd, request)
            charge_returned(bucket, request, len(data))
            content.extend(data)
            require(len(content) <= cap, "complete proc content cap exceeded")
            if not data:
                return bytes(content)
    finally:
        close_owned(fd)


def proc_dir(pid):
    require(type(pid) is int and pid > 0, "actual positive numeric process id required")
    return allocate_owned(os.open, "proc-dir:" + str(pid), "/proc/" + str(pid),
                          os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def generation(pfd, pid):
    text = proc_read(pfd, "stat").decode("ascii", "strict")
    require(text.startswith(str(pid) + " ("), "proc held numeric identity")
    tail = text[text.rfind(")") + 2:].split()
    require(len(tail) >= 20 and tail[1].isdigit() and tail[19].isdigit(), "proc stat fields")
    return int(tail[1]), int(tail[19])


def child_numbers(pfd, pid):
    raw = proc_read(pfd, "task/" + str(pid) + "/children", 4096)
    require(re.fullmatch(rb"(?:[0-9]+ )*", raw) is not None, "malformed children observation")
    values = [int(x) for x in raw.split()]
    require(len(values) <= 16 and len(set(values)) == len(values), "bounded child fanout")
    return values


def pidfd_target(pfd):
    require_lease(pfd)
    root = proc_dir(os.getpid())
    try:
        raw = proc_read(root, "fdinfo/" + str(pfd), 4096)
        values = re.findall(rb"(?m)^Pid:\s+(-?[0-9]+)$", raw)
        require(len(values) == 1, "actual pidfd target missing")
        return int(values[0])
    finally:
        close_owned(root)


def exited(record, timeout_ms=0):
    global PIDFD_POLL_CALLS
    require(type(timeout_ms) is int and 0 <= timeout_ms <= 10,
            "finite held exit poll interval required")
    require_lease(record["pidfd"])
    poll = select.poll()
    poll.register(record["pidfd"], select.POLLIN)
    PIDFD_POLL_CALLS += 1
    events = poll.poll(timeout_ms)
    record["evidence"]["last_held_exit_poll"] = {"fd":int(record["pidfd"]),
        "lease_generation":record["pidfd"].lease_generation,"at_ns":now(),
        "timeout_ms":timeout_ms,"events":[[int(fd),int(bits)] for fd,bits in events]}
    require_lease(record["pidfd"])
    if not events:
        return False
    require(len(events) == 1 and events[0][0] == record["pidfd"] and
            (events[0][1] & ~(select.POLLIN | select.POLLHUP)) == 0 and
            (events[0][1] & select.POLLIN) != 0,
            "held pidfd exit observation invalid")
    return True


def status(pfd):
    raw = proc_read(pfd, "status")
    values = {}
    for row in raw.splitlines():
        if b":" in row:
            key, value = row.split(b":", 1)
            values[key.decode("ascii")] = value.strip()
    return values


def memory(values):
    result = {}
    for key in ("VmSize", "VmRSS", "VmHWM", "VmPeak"):
        match = re.fullmatch(rb"([0-9]+) kB", values.get(key, b""))
        require(match is not None, "mandatory live memory missing: " + key)
        result[key + "_kib"] = int(match[1])
    return result


def credentials(values, ppid=None):
    require(values.get("Uid") == b"1000\t1000\t1000\t1000" and
            values.get("Gid") == b"1000\t1000\t1000\t1000" and values.get("Threads") == b"1",
            "unprivileged single-thread assumption absent")
    caps = {}
    for key in ("CapInh", "CapPrm", "CapEff", "CapAmb", "CapBnd"):
        require(re.fullmatch(rb"[0-9a-fA-F]+", values.get(key, b"")) is not None,
                "actual capability observation missing")
        caps[key] = int(values[key], 16)
        if key != "CapBnd":
            require(caps[key] == 0, "actual active/inheritable/ambient capability not clear")
    require(values.get("NoNewPrivs") == b"1", "actual NO_NEW_PRIVS absent")
    if ppid is not None:
        require(values.get("PPid") == str(ppid).encode("ascii"), "actual PPid mismatch")
    return caps


def raw_usage(usage):
    return {name: getattr(usage, name) for name in (
        "ru_utime", "ru_stime", "ru_maxrss", "ru_ixrss", "ru_idrss", "ru_isrss",
        "ru_minflt", "ru_majflt", "ru_nswap", "ru_inblock", "ru_oublock",
        "ru_msgsnd", "ru_msgrcv", "ru_nsignals", "ru_nvcsw", "ru_nivcsw")}


def sample_outer(phase):
    pfd = proc_dir(os.getpid())
    try:
        values = status(pfd)
        mm = memory(values)
        caps = credentials(values)
        names = own_fd_names()
        actual = []
        for name in names:
            require(name.isascii() and name.isdecimal(), "malformed own fd names")
            try:
                os.fstat(int(name))
                actual.append(int(name))
            except OSError as exc:
                require(exc.errno == errno.EBADF, "unknown fd sample error")
        usage = raw_usage(resource.getrusage(resource.RUSAGE_SELF))
        observation = {"phase": phase, "at_ns": now(), "current_proc": mm,
                       "raw_self_rusage": usage, "capability_masks": caps,
                       "fd_count": len(actual), "limits": {
                           "AS": list(resource.getrlimit(resource.RLIMIT_AS)),
                           "CPU": list(resource.getrlimit(resource.RLIMIT_CPU)),
                           "FSIZE": list(resource.getrlimit(resource.RLIMIT_FSIZE)),
                           "NOFILE": list(resource.getrlimit(resource.RLIMIT_NOFILE)),
                           "CORE": list(resource.getrlimit(resource.RLIMIT_CORE))},
                       "inherited_peak_subtracted": False}
        R["resources"].append(observation)
        require(all(v <= AS_OUTER // 1024 for v in mm.values()) and
                0 <= usage["ru_maxrss"] <= AS_OUTER // 1024 and
                usage["ru_utime"] + usage["ru_stime"] <= 60 and len(actual) <= 256,
                "outer actual resource ceiling exceeded")
        require(observation["limits"] == {"AS": [AS_OUTER, AS_OUTER], "CPU": [60, 120],
                "FSIZE": [PUBLIC_CAP, PUBLIC_CAP], "NOFILE": [256, 256], "CORE": [0, 0]},
                "outer pre-interpreter limits changed")
    finally:
        close_owned(pfd)


def kernel_policy():
    # libffi stays open for the life of this import. The exact inherited
    # 0..3 gate has already run; this is the first ctypes consumer.
    import ctypes
    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                    ("inheritable", ctypes.c_uint32)]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.capset.argtypes = [ctypes.POINTER(Header), ctypes.POINTER(Data)]
    libc.capset.restype = ctypes.c_int
    header, data = Header(0x20080522, 0), (Data * 2)()
    require(libc.capset(ctypes.byref(header), data) == 0, "clear own inh/prm/eff capset failed")
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    require(libc.prctl(47, 4, 0, 0, 0) == 0, "clear own ambient capabilities failed")
    require(libc.prctl(38, 1, 0, 0, 0) == 0, "set own NO_NEW_PRIVS failed")
    require(libc.prctl(36, 1, 0, 0, 0) == 0, "set own subreaper failed")
    flag = ctypes.c_int(0)
    require(libc.prctl(37, ctypes.addressof(flag), 0, 0, 0) == 0 and flag.value == 1,
            "actual own subreaper absent")
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    # Soft CPU60 bounds this collector while hard120 permits the unprivileged
    # fork child to retain the ORIGINAL target CPU120 cap before exec.
    signal.signal(signal.SIGXCPU, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    require(signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL and
            signal.SIGCHLD not in signal.pthread_sigmask(signal.SIG_BLOCK, set()),
            "SIGCHLD must be DFL and unblocked")
    R["kernel_policy"] = {"own_capset_clear": True, "ambient_clear": True,
                           "NO_NEW_PRIVS": True, "observed_subreaper": True,
                           "SIGCHLD_DFL_unblocked": True}


def new_record(pid=None, origin=None):
    require(len(TRACKED) < _RECORD_MAX, "owned generation registry ceiling")
    evidence = {"pid": pid, "start_ticks": None, "parent_pid": None, "origin": origin,
                "registered_before_release": False, "gate_release_written": False,
                "exit_confirmed": False, "reaped_by_outer": False,
                "fork_state": "NOT_ATTEMPTED" if origin == "os.fork exact return" else "ENUMERATED",
                "raw_wait4_rusage": None, "wait_status": None, "exit_code": None,
                "live_sample_count": 0, "first_live_sample": None, "last_live_sample": None,
                "postexec_live_sample_count": 0, "missing_sample_reason": "not observed yet",
                "uncommitted_exit_status": None}
    record = {"pid": pid, "pidfd": None, "procfd": None, "ticks": None,
              "origin": origin, "evidence": evidence, "reaped": False, "detached": False}
    TRACKED.append(record)
    R["ownership"].append(evidence)
    return record


def bind(record, expected_parent):
    pid = record["pid"]
    require(type(pid) is int and pid > 0, "only actual fork/enumerated PID may be bound")
    if record["procfd"] is None:
        record["procfd"] = proc_dir(pid)
    before = generation(record["procfd"], pid)
    require(before[0] == expected_parent, "actual held proc parent mismatch")
    if record["pidfd"] is None:
        record["pidfd"] = allocate_owned(os.pidfd_open, "pidfd:" + str(pid), pid, 0)
    require(generation(record["procfd"], pid) == before and
            pidfd_target(record["pidfd"]) == pid, "held PID/pidfd/start registration race")
    record["ticks"] = before[1]
    record["procfd"].generation_record = record
    record["evidence"].update(pid=pid, parent_pid=expected_parent, start_ticks=before[1])
    record["evidence"]["retained_original_leases"] = {
        name: {"fd":int(record[name]),"lease_generation":record[name].lease_generation,
               "identity9":list(record[name].identity9)} for name in ("pidfd","procfd")}
    return record


def prepare_proc_retirement(record):
    """Capture a terminal held-object relation; never refresh an input pin.

    Linux proc PID directories are live kernel objects, not immutable files.
    Root must independently qualify this exact provider's post-exit ownership
    transition before effects. ALL9 at acquisition and terminal remain visible.
    The raw A169 receipt does not contain the changed field; no field-specific
    explanation of that historical Refusal is asserted here.
    """
    fd = record["procfd"]
    require(isinstance(fd, OwnedFD) and not fd.retired and OWN.get(int(fd)) is fd and
            fd.identity9 is not None and fd.generation_record is record and
            fd.terminal_proc_identity9 is None,
            "terminal proc allocation provenance required")
    require(record["evidence"]["exit_confirmed"] is True and
            record["detached"] is True and exited(record),
            "terminal proc retirement requires exact detached held exit")
    current = full9(os.fstat(fd))
    original = fd.identity9
    require(stat.S_ISDIR(original[2]) and stat.S_ISDIR(current[2]) and
            current[:3] == original[:3] and
            current[3:5] in (original[3:5], [0, 0]),
            "terminal proc stable object/type or qualified owner transition failed")
    fd.terminal_proc_identity9 = current
    record["evidence"]["terminal_proc_lease"] = {
        "identity9": list(current), "lease_generation": fd.lease_generation,
        "same_held_dev_ino_mode": True, "strict_exit_and_detachment": True,
        "qualified_owner_transition": "acquired owner or Linux post-exit root pair",
        "immutable_input_policy_unchanged": True}


def held_generation_after_target(record, target, ppid=None):
    # target is the actual fdinfo observation of THIS retained descriptor.
    # Reconcile passes its already-read value: no second numerical lookup.
    if target == -1:
        require(exited(record), "detached pidfd without actual exit")
        record["evidence"]["exit_confirmed"] = True
        record["detached"] = True
        return False
    require(target == record["pid"], "held pidfd numerical target changed")
    if exited(record):
        record["evidence"]["exit_confirmed"] = True
        return False
    try:
        current_parent, ticks = generation(record["procfd"], record["pid"])
    except (FileNotFoundError, ProcessLookupError):
        if not exited(record):
            raise
        record["evidence"]["exit_confirmed"] = True
        return False
    require(ticks == record["ticks"] and (ppid is None or current_parent == ppid),
            "held actual generation/parent drift")
    if exited(record):
        record["evidence"]["exit_confirmed"] = True
        return False
    return True


def same_generation(record, ppid=None):
    return held_generation_after_target(record, pidfd_target(record["pidfd"]), ppid)


def absent_memory_key(values):
    for key in ("VmSize", "VmRSS", "VmHWM", "VmPeak"):
        if re.fullmatch(rb"([0-9]+) kB", values.get(key, b"")) is None:
            return key
    return None


def dead_or_exiting_state(values):
    return values.get("State", b"")[:1] in (b"X", b"Z")


def note_uncommitted(record, reason, kind):
    require(exited(record), "uncommitted ending status lacks same-held strict natural exit")
    evidence = record["evidence"]
    evidence["exit_confirmed"] = True
    evidence["uncommitted_exit_status"] = kind
    evidence["skipped_ending_reason"] = reason
    if evidence["live_sample_count"] == 0:
        evidence["missing_sample_reason"] = reason


def validate_status_prefix(record, values):
    # Available violations are sticky even if a later poll reports exit. A
    # partial memory tuple is NOT fabricated and never counted as a live sample.
    credentials(values, record["evidence"]["parent_pid"])
    for key in ("VmSize", "VmRSS", "VmHWM", "VmPeak"):
        if key in values:
            match = re.fullmatch(rb"([0-9]+) kB", values[key])
            require(match is not None, "malformed observed live memory: " + key)
            require(int(match[1]) <= AS_OUTER // 1024,
                    "owned child current observation >128MiB")


def resolve_natural_ending(record, absent):
    global NATURAL_EXIT_ATTEMPTS
    # This is bounded native observation, never signalling, waiting for a
    # model, reopening by PID, or manufacturing a cleanup-induced exit.
    require("ending_observation" not in record["evidence"] and NATURAL_EXIT_ATTEMPTS < _RECORD_MAX,
            "whole caller single ending attempt per retained generation exceeded")
    NATURAL_EXIT_ATTEMPTS += 1
    counters = (PIDFD_POLL_CALLS, LEASE_CHECK_CALLS, CLOCK_CALLS)
    begin = now()
    phase_end = CLEANUP_END if PROC_PHASE == "cleanup" else OBSERVATION_END
    require(type(phase_end) is int and phase_end > 0, "actual observation phase bound absent")
    end = min(DEADLINE, phase_end, begin + NATURAL_EXIT_BOUND_NS)
    detail = {"started_ns":begin,"absolute_end_ns":end,"checks":0,
              "same_original_lease_generation":record["pidfd"].lease_generation,
              "missing_key":absent,"signals_sent":0,"reopened":False,
              "strict_exit_observed":False,"live_sample_committed":False}
    record["evidence"]["ending_observation"] = detail
    for index in range(NATURAL_EXIT_CHECKS):
        at = now()
        if at >= end:
            break
        # Floor milliseconds: never request a poll longer than remaining bound.
        timeout = 0 if index == 0 else min(10, max(0, (end-at)//1000000))
        detail["checks"] += 1
        if exited(record, timeout):
            detail["strict_exit_observed"] = True
            detail["completed_ns"] = now()
            due(end)
            note_uncommitted(record, "potential-ending status resolved by bounded same-held natural exit; no live tuple",
                             "pidfd_confirmed_natural_ending_not_a_live_sample")
            detail["native_counter_deltas"] = {"polls":PIDFD_POLL_CALLS-counters[0],
                "lease_checks":LEASE_CHECK_CALLS-counters[1],"clocks":CLOCK_CALLS-counters[2]}
            return
    detail["completed_ns"] = now()
    detail["native_counter_deltas"] = {"polls":PIDFD_POLL_CALLS-counters[0],
        "lease_checks":LEASE_CHECK_CALLS-counters[1],"clocks":CLOCK_CALLS-counters[2]}
    detail["unresolved_reason"] = "same-held strict natural exit not observed within unchanged phase/shared/50ms bound"
    require(False, "mandatory live memory missing: " + absent if absent is not None else
            "dead/exiting status lacks bounded same-held strict natural exit")


def observe(record, postexec=False):
    try:
        observe_live(record, postexec)
    except (FileNotFoundError, ProcessLookupError):
        # Only a real exit of the already-held pidfd resolves vanished proc data.
        # Live, invalid-fd and all other errors retain the original refusal.
        if not exited(record):
            raise
        note_uncommitted(record, "held pidfd-confirmed exit during proc observation",
                         "pidfd_confirmed_exit_not_a_live_sample")


def observe_live(record, postexec=False):
    if not same_generation(record, record["evidence"]["parent_pid"]):
        if record["evidence"]["live_sample_count"] == 0:
            record["evidence"]["missing_sample_reason"] = "held pidfd-confirmed exit before full live observation"
        record["evidence"]["uncommitted_exit_status"] = "pidfd_confirmed_exit_not_a_live_sample"
        return
    values = status(record["procfd"])
    validate_status_prefix(record, values)
    if not observation_identity(record):
        return
    # Retain the single status+generation recheck reservation. X/Z is only a
    # potential ending, NEVER substitute exit proof for the held strict poll.
    if dead_or_exiting_state(values) or absent_memory_key(values) is not None:
        values = status(record["procfd"])
        validate_status_prefix(record, values)
        if not observation_identity(record):
            return
        absent = absent_memory_key(values)
        if absent is not None or dead_or_exiting_state(values):
            resolve_natural_ending(record, absent)
            return
    mm = memory(values)
    caps = credentials(values)
    if exited(record):
        note_uncommitted(record, "held pidfd-confirmed exit at final sample commit",
                         "pidfd_confirmed_exit_not_a_live_sample")
        return
    observation = {"at_ns": now(), "current_proc": mm, "capability_masks": caps}
    ev = record["evidence"]
    ev["live_sample_count"] += 1
    ev["first_live_sample"] = ev["first_live_sample"] or observation
    ev["last_live_sample"] = observation
    ev["missing_sample_reason"] = None
    ev["uncommitted_exit_status"] = None
    if postexec:
        ev["postexec_live_sample_count"] += 1
    require(all(v <= AS_OUTER // 1024 for v in mm.values()), "owned child current observation >128MiB")


def observation_identity(record):
    parent, ticks = generation(record["procfd"], record["pid"])
    require(ticks == record["ticks"] and parent == record["evidence"]["parent_pid"],
            "live sample generation/parent mismatch")
    target = pidfd_target(record["pidfd"])
    if target == -1:
        require(exited(record), "final detached pidfd without actual exit")
        record["detached"] = True
        note_uncommitted(record, "held pidfd-confirmed exit at final sample target",
                         "pidfd_confirmed_exit_not_a_live_sample")
        return False
    require(target == record["pid"], "live sample generation mismatch")
    if exited(record):
        note_uncommitted(record, "held pidfd-confirmed exit at final sample identity",
                         "pidfd_confirmed_exit_not_a_live_sample")
        return False
    return True


def reconcile(pid, ppid, origin):
    for record in TRACKED:
        if record["pid"] == pid and record["pidfd"] is not None and not record["detached"]:
            require(record["procfd"] is not None and type(record["ticks"]) is int,
                    "enumerated child registration incomplete")
            target = pidfd_target(record["pidfd"])
            if target == pid:
                # False means THIS held generation ended, not the parent.
                # Return it unchanged; its exact wait/adoption/terminal duties stay.
                held_generation_after_target(record, target, ppid)
                return record
            require(target == -1, "old generation unresolved during PID reuse")
            held_generation_after_target(record, target, ppid)
    return bind(new_record(pid, origin), ppid)


def tree(record, depth=0, seen=None):
    require(depth <= 4, "unexpected owned descendant depth")
    seen = set() if seen is None else seen
    identity = (record["pid"], record["ticks"])
    require(identity not in seen and len(seen) < _RECORD_MAX,
            "duplicate or excessive actual owned tree generation")
    seen.add(identity)
    result = []
    try:
        if not same_generation(record):
            return result
        children = child_numbers(record["procfd"], record["pid"])
    except (FileNotFoundError, ProcessLookupError):
        if not exited(record):
            raise
        record["evidence"]["exit_confirmed"] = True
        return result
    # Only the reads above belong to record. Child reconciliation and recursive
    # reads MUST NOT be resolved by the enumerating parent's exit. A partially
    # registered vanished child stays TRACKED and its failure remains sticky.
    for pid in children:
        child = reconcile(pid, record["pid"], "actual enumerated held-parent descendant")
        result.append(child)
        result.extend(tree(child, depth + 1, seen))
    return result


def direct_children():
    pfd = proc_dir(os.getpid())
    try:
        return child_numbers(pfd, os.getpid())
    finally:
        close_owned(pfd)


def adopted_records():
    # Exclusive single-thread launcher, observed subreaper, empty baseline, one
    # active fork site. Actual direct adoptees belong to the released subtree.
    result = []
    for pid in direct_children():
        if ACTIVE is not None and pid == ACTIVE["pid"] and not ACTIVE["reaped"]:
            continue
        require(R.get("initial_children_empty") is True and
                R.get("kernel_policy", {}).get("observed_subreaper") is True,
                "adopted lineage premise unavailable")
        result.append(reconcile(pid, os.getpid(), "actual sole-fork subtree adoption"))
    return result


def wait_exact(record, end):
    while now() < min(end, DEADLINE):
        try:
            pid, wait_status, usage = os.wait4(record["pid"], os.WNOHANG)
        except ChildProcessError:
            uncertain("exact child wait ownership missing")
        if pid == record["pid"]:
            record["reaped"] = True
            record["detached"] = True
            record["evidence"].update(reaped_by_outer=True, exit_confirmed=True,
                wait_status=wait_status, exit_code=os.waitstatus_to_exitcode(wait_status),
                raw_wait4_rusage=raw_usage(usage))
            return
        select.select([], [], [], min(0.01, max(0, (end - now()) / 1e9)))
    uncertain("exact owned wait4 missed single absolute bound")


def signal_held(record, signum):
    require_lease(record["pidfd"])
    try:
        signal.pidfd_send_signal(record["pidfd"], signum)
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            raise
        require(exited(record), "held signal ESRCH without same-generation exit")
        record["evidence"]["exit_confirmed"] = True
        return False
    return True


def stop_record(record, end):
    if record["reaped"]:
        return
    if record["pidfd"] is None:
        require(record is ACTIVE and record["origin"] == "os.fork exact return" and
                not record["evidence"]["gate_release_written"],
                "numeric fallback lacks exact unreleased direct fork ownership")
        os.waitid(os.P_PID, record["pid"], os.WEXITED | os.WNOHANG | os.WNOWAIT)
        try:
            os.kill(record["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif not exited(record):
        target = pidfd_target(record["pidfd"])
        if target == -1:
            require(exited(record), "stop detached pidfd without actual exit")
            record["detached"] = True
            record["evidence"]["exit_confirmed"] = True
        else:
            require(target == record["pid"], "stop held pidfd mismatch")
            signal_held(record, signal.SIGKILL)
    # A strictly confirmed ending race never skips exact direct/adopted reap.
    wait_exact(record, end)


def settle_owned():
    global CLEANUP_END, CLEANUP_ATTEMPTED, STOP, PROC_PHASE
    require(not CLEANUP_ATTEMPTED, "single known-own cleanup already attempted")
    CLEANUP_ATTEMPTED = True
    previous_phase = PROC_PHASE
    PROC_PHASE = "cleanup"
    if CLEANUP_END is None:
        CLEANUP_END = min(DEADLINE - 15000000000, now() + 5000000000)
    end = CLEANUP_END
    saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
    problems = []
    try:
        if ACTIVE is not None and not ACTIVE["reaped"]:
            record = ACTIVE
            if record["pid"] is None:
                if record["evidence"]["fork_state"] == "NOT_ATTEMPTED" and direct_children() == []:
                    record["evidence"].update(no_creation_confirmed=True,
                        wait4_unknown_reason="fork not attempted; no child exists")
                    record["reaped"] = True
                else:
                    uncertain("fork result missing; no guessed ownership recovery")
            try:
                if record["pidfd"] is not None and not exited(record):
                    signal_held(record, signal.SIGSTOP)
                    frozen = False
                    freeze_observations = 0
                    while now() < end and not exited(record) and freeze_observations < _FREEZE_MAX:
                        freeze_observations += 1
                        if not same_generation(record):
                            break
                        try:
                            values = status(record["procfd"])
                        except (FileNotFoundError, ProcessLookupError):
                            if not exited(record):
                                raise
                            record["evidence"]["exit_confirmed"] = True
                            break
                        if exited(record):
                            record["evidence"]["exit_confirmed"] = True
                            break
                        if values.get("State", b"")[:1] in (b"T", b"t"):
                            frozen = True
                            break
                        select.select([], [], [], min(0.005, max(0, (end - now()) / 1e9)))
                    require(frozen or exited(record), "held parent stop unconfirmed")
                    for child in reversed(tree(record)):
                        if not exited(child) and same_generation(child):
                            signal_held(child, signal.SIGKILL)
            except BaseException as exc:
                STOP = True
                problems.append({"stage": "freeze_or_descendants", "error": exact_error(exc)})
            # A descendant observation failure does not skip stopping the known
            # direct parent; actual adoptees are then reconciled independently.
            try:
                stop_record(record, end)
            except BaseException as exc:
                STOP = True
                problems.append({"stage": "exact_parent_stop_reap", "error": exact_error(exc)})
        # Only actually enumerated direct adopted generations are waited. An
        # already reaped descendant is never given an invented wait4 usage.
        rounds = 0
        while direct_children():
            due(end)
            rounds += 1
            require(rounds <= _RECORD_MAX, "bounded actual adopted cleanup passes exceeded")
            for record in adopted_records():
                stop_record(record, end)
        for record in TRACKED:
            if record["evidence"].get("no_creation_confirmed") is True:
                continue
            require(record["pidfd"] is not None and exited(record),
                    "registered held generation terminal exit unconfirmed")
            record["evidence"]["exit_confirmed"] = True
            if not record["reaped"]:
                require(pidfd_target(record["pidfd"]) == -1,
                        "descendant still has kernel PID after actual empty-child check")
                record["detached"] = True
                record["evidence"]["raw_wait4_rusage"] = None
                record["evidence"]["wait4_unknown_reason"] = "reaped by actual descendant parent; outer cannot wait this generation"
        require(direct_children() == [], "actual final own child set not empty")
        R["actual_children_empty"] = True
        R["single_cleanup_end_ns"] = end
        if problems:
            R.setdefault("owned_cleanup_problems", []).extend(problems)
            uncertain("one or more ownership cleanup observations failed")
        R["owned_cleanup_complete"] = True
    except BaseException:
        STOP = True
        if problems:
            R.setdefault("owned_cleanup_problems", []).extend(problems)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, saved)
        PROC_PHASE = previous_phase


def strict_json(data, newline=False):
    require(data and (not newline or data.endswith(b"\n") and not data.endswith(b"\n\n")),
            "complete single JSON newline required")
    text = data.decode("ascii", "strict")
    def pairs(rows):
        answer = {}
        for key, value in rows:
            require(key not in answer, "duplicate JSON key")
            answer[key] = value
        return answer
    def bad_constant(value):
        raise Refusal("nonfinite JSON constant")
    decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=bad_constant)
    value, end = decoder.raw_decode(text)
    require(end == len(text) - (1 if newline else 0) and type(value) is dict,
            "trailing/incomplete ordinary JSON")
    def finite(item):
        if type(item) is float:
            require(math.isfinite(item), "nonfinite JSON float")
        elif type(item) is dict:
            for child in item.values():
                finite(child)
        elif type(item) is list:
            for child in item:
                finite(child)
    finite(value)
    return value


def selection_data():
    global SELECTED, SELECT_META
    info = os.fstat(3)
    flags = fcntl.fcntl(3, fcntl.F_GETFL)
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            info.st_uid == 1000 and info.st_gid == 1000 and stat.S_IMODE(info.st_mode) == 0o600
            and 0 < info.st_size <= 65536 and flags & os.O_ACCMODE == os.O_RDONLY
            and not flags & (os.O_PATH | os.O_APPEND), "selection must be private ordinary readonly regular DATA")
    SELECT_META = full9(info)
    take_bounded("selection", info.st_size + 1, SELECTION_SLOT)
    raw = read_at(3, info.st_size + 1, 0)
    require(len(raw) == info.st_size and full9(os.fstat(3)) == SELECT_META,
            "complete selected ordinary DATA read/full9 mismatch")
    parsed = strict_json(raw, raw.endswith(b"\n"))
    require(set(parsed) == {"schema", "pins"} and
            parsed["schema"] == "friday.e4.node.ordinary-root-selection-data.a082.v1",
            "ordinary pin DATA schema; no execution grant accepted")
    expected_names = set(FIXED) | set(LOCAL)
    require(type(parsed["pins"]) is dict and set(parsed["pins"]) == expected_names,
            "complete fixed input/source selection required")
    for name, item in parsed["pins"].items():
        require(type(item) is dict and set(item) == {"path", "size", "sha256", "identity9"},
                "fixed ordinary pin DATA fields")
        require(type(item["size"]) is int and 0 < item["size"] <= 33554432 and
                type(item["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]),
                "ordinary selected size/hash types")
        vector = item["identity9"]
        require(type(vector) is list and len(vector) == 9 and
                all(type(x) is int and x >= 0 for x in vector) and vector[6] == item["size"],
                "complete numeric full9 vector required")
        if name in FIXED:
            require((item["path"], item["size"], item["sha256"]) == FIXED[name][:3],
                    "Root selection must independently match accepted fixed bytes")
        else:
            require(item["path"] == LOCAL[name], "canonical final package Source/DATA path required")
    SELECTED = parsed["pins"]
    R["selection_data"] = {"identity9": SELECT_META, "sha256": hashlib.sha256(raw).hexdigest(),
                            "ordinary_DATA_only": True, "execution_authority": False}


def vector(case, private_name=None):
    if case == "verifier-entry-fd7":
        return ["prlimit", "--as=117440512:117440512", "--cpu=120:120",
                "--fsize=32768:32768", "--nofile=128:128", "--core=0:0", "--",
                "/proc/self/fd/4", "-I", "-S", "-B", "/proc/self/fd/5",
                "--reviewed-source-sha256", VHASH, "--reviewed-contract-sha256", CHASH,
                "--deadline-monotonic-ns", str(CONTROLS_END), "--private-home-name", private_name,
                "--child-bootstrap-read-debit", "32768"]
    mode = case if case in CASES[:3] else "verify-node"
    return ["prlimit", "--as=33554432:117440512", "--cpu=120:120",
            "--fsize=32768:32768", "--nofile=64:128", "--core=0:0", "--",
            "/proc/self/fd/4", "-I", "-S", "-B", "/proc/self/fd/5",
            "--host-deadline-monotonic-ns", str(CANONICAL_END), "--mode", mode]


def capture_pipe(item, evidence):
    raw = bytes(item["data"])
    label = item["label"]
    evidence[label + "_bytes"] = len(raw)
    evidence[label + "_sha256"] = hashlib.sha256(raw).hexdigest()
    evidence[label + "_eof"] = item["eof"]
    evidence[label + "_overflow"] = item.get("overflow", False)
    pending = item.get("pending_chunk")
    if pending is not None:
        evidence[label + "_pending_raw_base64"] = base64.b64encode(pending).decode("ascii")
        evidence[label + "_pending_raw_bytes"] = len(pending)
        evidence[label + "_pending_raw_sha256"] = hashlib.sha256(pending).hexdigest()
        evidence[label + "_pending_append_outcome"] = "UNCONFIRMED_NOT_FULLRAW_PROOF"
    # Normal JSON and the stock traceback use <=2x string escaping. Retain
    # every exceptional returned byte losslessly without an unbounded escape
    # expansion, while refusing to treat it as an ordinary ASCII receipt.
    escape_bytes = sum(2 if b in (34, 92, 8, 9, 10, 12, 13) else 6 if b < 32 else 1 for b in raw)
    if all(b < 128 for b in raw) and escape_bytes <= 2 * len(raw):
        evidence[label + "_text"] = raw.decode("ascii", "strict")
    else:
        evidence[label + "_text"] = None
        evidence[label + "_raw_base64"] = base64.b64encode(raw).decode("ascii")
        evidence[label + "_encoding_reason"] = "complete exceptional raw bytes; not an ordinary ASCII receipt"


def drain_pipes(pipes, evidence, end, complete=False):
    require(type(CASE_INDEX) is int, "active transport case missing")
    bucket = "transport_" + str(CASE_INDEX)
    while True:
        due(end)
        progress = False
        for item in pipes:
            if item["eof"] or item.get("overflow"):
                continue
            while True:
                request = min(4096, item["cap"] + 1 - len(item["data"]))
                admit_request(bucket, request)
                try:
                    require_lease(item["fd"])
                    item["pending_chunk"] = os.read(item["fd"], request)
                    data = item["pending_chunk"]
                except BlockingIOError:
                    break
                charge_returned(bucket, request, len(data))
                item["data"].extend(data)
                item["pending_chunk"] = None
                if not data:
                    item["eof"] = True
                    capture_pipe(item, evidence)
                    break
                if len(item["data"]) > item["cap"]:
                    item["overflow"] = True
                    capture_pipe(item, evidence)
                # The full mutable raw buffer is already retained in pipes.
                # Snapshot on EOF/overflow and the launch finally, avoiding
                # quadratic re-encoding when valid reads are fragmented.
                require(not item.get("overflow"), "finite pipe output cap exceeded; complete returned overflow byte retained")
                progress = True
        if all(item["eof"] or item.get("overflow") for item in pipes):
            require(not any(item.get("overflow") for item in pipes),
                    "finite pipe output cap exceeded; stream EOF not claimed")
            return
        if not complete:
            return
        if not progress:
            select.select([x["fd"] for x in pipes if not x["eof"] and not x.get("overflow")], [], [],
                          min(0.01, max(0, (end - now()) / 1e9)))


def entry_fds():
    names = own_fd_names()
    live = []
    not_live = []
    for name in names:
        require(name.isascii() and name.isdecimal(), "entry descriptor name syntax")
        number = int(name)
        try:
            os.fstat(number)
        except OSError as exc:
            require(exc.errno == errno.EBADF, "entry descriptor observation error")
            not_live.append(name)
        else:
            live.append(number)
    observations = R.setdefault("entry_observations", [])
    require(len(observations) < 8, "entry observation bound")
    observations.append({"observed_names": list(names), "live_descriptors": live,
                         "not_live_ebadf": not_live, "reclassified": False,
                         "closed_unproven": False})
    return set(live)


def launch(case, receipt, end):
    global ACTIVE, CHILD_READ_RESERVE, PROC_PHASE, STOP, OBSERVATION_END
    launch_owner = {"primary": None, "primary_tb": None,
                    "capture_errors": [None, None], "close_error": None,
                    "pipes": None, "receipt": receipt}
    LAUNCH_OWNERS.append(launch_owner)
    require(not CLEANUP_ATTEMPTED, "cannot fork after the single final cleanup")
    PROC_PHASE = "launch"
    verifier_control = case == "verifier-entry-fd7"
    source_name = "verifier" if verifier_control else "supervisor"
    hash_names = ("prlimit", "python", source_name, "contract", "caller", "expectations", "plain")
    custody("prelaunch_" + case, hash_names, end)
    require(direct_children() == [], "unknown own child before exclusive fork")
    record = new_record(origin="os.fork exact return")
    ACTIVE = record
    require(LEDGER["child"] >= 32768, "read budget unavailable before fork")
    LEDGER["child"] -= 32768
    sync_plan()
    plan_fits(0)
    CHILD_READ_RESERVE += 32768
    receipt["own_child_bootstrap_read_debit"] = {"bytes": 32768,
        "kind": "FIXED_CONSERVATIVE_UPPER_BOUND_NOT_MEASURED", "observed_bytes": None,
        "observation": "UNKNOWN_NOT_ZERO", "copied_parent_prefix": False}
    receipt["ownership_index"] = len(TRACKED) - 1
    receipt["actual_launch"] = {"env": dict(ENV), "shell": False,
                                 "host_deadline_monotonic_ns": DEADLINE,
                                 "canonical_end_ns": CANONICAL_END, "operation_end_ns": end}
    private_name = "friday-a048-g1-" + os.urandom(16).hex() if verifier_control else None
    if verifier_control:
        try:
            os.stat(private_name, dir_fd=component("/var/tmp"), follow_symlinks=False)
        except FileNotFoundError:
            receipt["own_control_private_name_absent_before"] = True
        else:
            raise Refusal("ordinary control private name collision; never delete")
        receipt["own_control_private_name"] = private_name
    argv = vector(case, private_name)
    receipt["actual_launch"]["argv"] = argv
    role_names = ("prlimit", "python", source_name) + (("contract",) if verifier_control else ())
    copies = [allocate_owned(fcntl.fcntl, "launch-copy:" + name,
                             require_lease(HELD[name]["fd"]), fcntl.F_DUPFD_CLOEXEC, 128)
              for name in role_names]
    extra_target = 127 if case == "supervisor-entry-fd127" else 7 if case in CASES[3:] else None
    extra_copy = allocate_owned(fcntl.fcntl, "launch-surplus",
        require_lease(HELD["plain"]["fd"]), fcntl.F_DUPFD_CLOEXEC, 128) if extra_target else None
    gate = allocate_pipe("launch-gate")
    pipe_pairs = []
    pipes = []
    launch_owner["pipes"] = pipes
    null = None
    gate_end = min(end, now() + 5000000000)
    OBSERVATION_END = gate_end
    try:
        for label, cap in (("stdout", STDOUT_CAP), ("stderr", STDERR_CAP)):
            pair = allocate_pipe("launch-pipe:" + label)
            pipe_pairs.append(pair)
            pipes.append({"fd": pair[0], "label": label, "cap": cap, "data": bytearray(),
                          "pending_chunk": None, "eof": False})
        null = allocate_owned(os.open, "launch-null", "null",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=component("/dev"))
        ns = os.fstat(null)
        require(stat.S_ISCHR(ns.st_mode) and ns.st_rdev == os.makedev(1, 3)
                and ns.st_uid == 0, "actual /dev/null device mismatch")
        saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
        try:
            receipt["fork_prepared"] = {"actual_children_empty": True, "gate_end_ns": gate_end,
                "gate_identity": full9(os.fstat(gate[0])), "signal_masked": True,
                "complete_record_reserved": True}
            record["evidence"]["fork_state"] = "PENDING"
            record["pid"] = os.fork()
            if record["pid"] == 0:
                try:
                    closed_child = set()
                    uncertain_child = set()

                    def child_close_once(fd, sweep=False):
                        number = int(fd)
                        if number in uncertain_child:
                            os._exit(125)
                        if number in closed_child:
                            return
                        closed_child.add(number)  # prospective before native close
                        try:
                            os.close(number)
                        except OSError as exc:
                            if sweep and exc.errno == errno.EBADF:
                                closed_child.add(number)
                                return
                            uncertain_child.add(number)
                            raise

                    child_close_once(gate[1])  # child fork table only; parent lease stays for parent retirement
                    while now() < gate_end:
                        try:
                            token = os.read(gate[0], 1)
                        except BlockingIOError:
                            select.select([gate[0]], [], [], min(0.005, max(0, (gate_end - now()) / 1e9)))
                            continue
                        if token != b"\x01":
                            os._exit(125)
                        break
                    else:
                        os._exit(125)
                    # Exact original caps are lowered before the first prlimit
                    # or target Python instruction; outer CPU60 is not inherited.
                    as_pair = (117440512, 117440512) if verifier_control else (33554432, 117440512)
                    resource.setrlimit(resource.RLIMIT_AS, as_pair)
                    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
                    resource.setrlimit(resource.RLIMIT_FSIZE, (32768, 32768))
                    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                    os.dup2(null, 0, inheritable=True)
                    os.dup2(pipe_pairs[0][1], 1, inheritable=True)
                    os.dup2(pipe_pairs[1][1], 2, inheritable=True)
                    for old, new in zip(copies, (3, 4, 5, 6)):
                        os.dup2(old, new, inheritable=True)
                    if extra_target is not None:
                        os.dup2(extra_copy, extra_target, inheritable=True)
                    # Establish high127 while the inherited own soft256 still
                    # permits dup2, then lower to the EXACT source64:128 pair.
                    # The already-open high descriptor remains detectable by
                    # the target's unchanged strict whole-fd entry guard.
                    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128) if verifier_control else (64, 128))
                    allowed = set(range(7 if verifier_control else 6))
                    if extra_target is not None:
                        allowed.add(extra_target)
                    # Close using the inherited outer hard ceiling, including
                    # high duplicates even after effective source NOFILE64.
                    for fd in range(6 if not verifier_control else 7, 256):
                        if fd not in allowed:
                            child_close_once(fd, sweep=True)
                    require(entry_fds() == allowed, "actual pre-interpreter exact fd set mismatch")
                    for fd, name in zip((3, 4, 5, 6), role_names):
                        require(full9(os.fstat(fd)) == HELD[name]["identity9"] and
                                fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY,
                                "actual preexec source role identity/readonly mismatch")
                    if extra_target is not None:
                        require(full9(os.fstat(extra_target)) == HELD["plain"]["identity9"],
                                "ordinary own surplus readonly role mismatch")
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    for signum in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM,
                                   signal.SIGPIPE, signal.SIGCHLD, signal.SIGXCPU):
                        signal.signal(signum, signal.SIG_DFL)
                    signal.pthread_sigmask(signal.SIG_SETMASK, saved)
                    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
                    os.execve(3, argv, ENV)
                except BaseException:
                    os._exit(125)
            record["evidence"]["pid"] = record["pid"]
            record["evidence"]["fork_state"] = "CONFIRMED_CREATED"
            bind(record, os.getpid())
            observe(record)
            record["evidence"]["registered_before_release"] = True
            require(now() < gate_end, "direct registration missed finite gate")
            require(os.write(gate[1], b"\x01") == 1, "owned gate release failed")
            record["evidence"]["gate_release_written"] = True
        except BaseException:
            globals()["STOP"] = True
            raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, saved)
            early = gate + copies + [extra_copy, null] + [pair[1] for pair in pipe_pairs]
            # Retire aliases BEFORE attempting all first closes. Later finally
            # sees no stale slot numbers, even if a close or a signal raises.
            gate.clear()
            copies.clear()
            extra_copy = null = None
            for pair in pipe_pairs:
                pair[1] = None
            close_batch(early)
        OBSERVATION_END = end
        sample_outer("running_" + case)
        receipt["outer_running_sample"] = True
        PROC_PHASE = "running"
        while not exited(record):
            due(end)
            observe(record, postexec=True)
            for child in tree(record):
                observe(child, postexec=True)
            drain_pipes(pipes, receipt, end)
            select.select([x["fd"] for x in pipes if not x["eof"]], [], [],
                          min(0.02, max(0, (end - now()) / 1e9)))
        # Reap first, drain to actual EOF next. Leaked inherited writers do not
        # masquerade as complete transport and cannot extend the fixed end.
        PROC_PHASE = "close"
        wait_exact(record, end)
        receipt["exit_code"] = record["evidence"]["exit_code"]
        receipt["raw_wait4_rusage"] = record["evidence"]["raw_wait4_rusage"]
        drain_pipes(pipes, receipt, end, complete=True)
        receipt["transport_complete"] = all(x["eof"] for x in pipes)
        receipt["exit_code"] = record["evidence"]["exit_code"]
        receipt["raw_wait4_rusage"] = record["evidence"]["raw_wait4_rusage"]
        require(0 <= receipt["raw_wait4_rusage"]["ru_maxrss"] <= 117440512 // 1024,
                "actual waited source raw high-water exceeds original112MiB")
        require(not adopted_records(), "source exit left actual adopted descendants")
        require(direct_children() == [], "own child set not empty after exact reap")
        custody("postexit_" + case, hash_names, end)
        if verifier_control:
            try:
                os.stat(private_name, dir_fd=component("/var/tmp"), follow_symlinks=False)
            except FileNotFoundError:
                receipt["own_control_private_name_absent_after"] = True
            else:
                uncertain("own control name unexpectedly present; never delete")
    except BaseException as primary_exc:
        # Running admission cannot consume the independent cleanup/terminal
        # buckets. Stop/reap while readers are still held, then observe all
        # admitted remaining bytes/EOF before their descriptors are closed.
        STOP = True
        launch_owner["primary"], launch_owner["primary_tb"] = primary_exc, primary_exc.__traceback__
        receipt["launch_firstfault"] = exact_error(primary_exc)
        PROC_PHASE = "close"
        if not CLEANUP_ATTEMPTED:
            try:
                settle_owned()
            except BaseException as exc:
                receipt["cleanup_failure"] = exact_error(exc)
        try:
            drain_pipes(pipes, receipt, DEADLINE - 15000000000, complete=True)
        except BaseException as exc:
            receipt["closure_transport_failure"] = exact_error(exc)
        raise
    finally:
        # Complete data remain in the case record even if parsing, wait4,
        # postexit telemetry or custody later refuses. No digest replaces them.
        first_capture_error = None
        for index, item in enumerate(pipes):
            try:
                capture_pipe(item, receipt)
            except BaseException as exc:
                STOP = True
                launch_owner["capture_errors"][index] = exc
                receipt[item["label"] + "_capture_error"] = exact_error(exc)
                first_capture_error = first_capture_error or exc
        receipt["transport_complete"] = bool(pipes) and all(x["eof"] and not x.get("overflow")
                and x.get("pending_chunk") is None for x in pipes) and first_capture_error is None
        receipt["exit_code"] = record["evidence"]["exit_code"]
        receipt["raw_wait4_rusage"] = record["evidence"]["raw_wait4_rusage"]
        if receipt["raw_wait4_rusage"] is None:
            receipt["wait4_unknown_reason"] = record["evidence"].get("wait4_unknown_reason", "actual exact wait4 not confirmed")
        final_aliases = gate + copies + [extra_copy, null] + [x for pair in pipe_pairs for x in pair]
        gate.clear()
        copies.clear()
        extra_copy = null = None
        for pair in pipe_pairs:
            pair[:] = [None, None]
        try:
            close_batch(final_aliases)
        except BaseException as exc:
            STOP = True
            launch_owner["close_error"] = exc
            receipt["launch_close_secondary_error"] = exact_error(exc)
        if launch_owner["primary"] is None:
            if first_capture_error is not None:
                raise first_capture_error
            if launch_owner["close_error"] is not None:
                raise launch_owner["close_error"]


def numeric_mm(item, cap):
    require(type(item) is dict and set(item) == {"VmSize_kib", "VmRSS_kib", "VmHWM_kib", "VmPeak_kib"}
            and all(type(x) is int and 0 <= x <= cap for x in item.values()),
            "complete actual numeric resource evidence required")


def bounded_number(value, cap, why):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= cap, why)


def supervisor_resources(outer, case):
    # Preserve the heterogeneous original producer; validate every typed cell.
    # Telemetry is not a self-phase record, and no resource value is skipped.
    require(outer["resource_schema"] == "friday.e4.node.supervisor-resources.a172.v1",
            "current supervisor resource schema required")
    cells = outer["resources"]
    phases = ("bootstrap_startup", "last_live_supervisor", "postexit")
    telemetry = ("last_parent_sample", "last_observed_aggregate_as_kib", "last_fd_counts",
                 "last_live_aggregate")
    require(type(cells) is dict and set(cells) == set(phases + telemetry),
            "complete supervisor self and telemetry resource cells required")
    sup_peak = 0
    for phase in phases:
        item = cells[phase]
        require(type(item) is dict and set(item) == {"current", "ru_maxrss_self_kib",
                "elapsed_seconds", "enforced_as_soft_hard_bytes", "enforced_nofile_soft_hard"},
                "complete typed supervisor self-phase fields required")
        numeric_mm(item["current"], 32768)
        raw = item["ru_maxrss_self_kib"]
        bounded_number(raw, 32768, "raw supervisor high-water")
        bounded_number(item["elapsed_seconds"], 900, "supervisor elapsed phase bound")
        require(item["enforced_as_soft_hard_bytes"] == [33554432, 117440512] and
                item["enforced_nofile_soft_hard"] == [64, 128],
                "original supervisor imposed resource pairs required")
        sup_peak = max(sup_peak, raw)
    parent_cap = 65536 if case == "startup-as-negative" else 114688
    parent = cells["last_parent_sample"]
    require(type(parent) is dict and set(parent) == {"sample", "missing_reason", "complete_live_samples"}
            and type(parent["complete_live_samples"]) is int and parent["complete_live_samples"] > 0,
            "typed parent resource observation and genuine live count required")
    if parent["sample"] is None:
        require(type(parent["missing_reason"]) is str and bool(parent["missing_reason"]),
                "missing parent resource observation requires actual reason")
    else:
        numeric_mm(parent["sample"], parent_cap)
        require(parent["missing_reason"] is None, "present parent sample has missing reason")
    aggregate = cells["last_observed_aggregate_as_kib"]
    require(type(aggregate) is int and 0 <= aggregate <= 262144,
            "complete actual live aggregate AS scalar required")
    counts = cells["last_fd_counts"]
    require(type(counts) is dict and set(counts) <= {"supervisor", "verifier", "gpg", "gpg_missing_reason"}
            and {"supervisor", "verifier", "gpg"} <= set(counts),
            "complete typed live FD count fields required")
    require(type(counts["supervisor"]) is int and 0 <= counts["supervisor"] <= 64 and
            type(counts["verifier"]) is int and 0 <= counts["verifier"] <= 128,
            "complete actual supervisor and verifier FD counts required")
    if counts["gpg"] is None:
        require(type(counts.get("gpg_missing_reason")) is str and bool(counts["gpg_missing_reason"]),
                "unknown GPG FD count requires original actual reason")
    else:
        require(type(counts["gpg"]) is int and 0 <= counts["gpg"] <= 128 and
                "gpg_missing_reason" not in counts, "complete actual GPG FD count required")
    require(counts["supervisor"] + counts["verifier"] +
            (counts["gpg"] if counts["gpg"] is not None else 0) <= 320,
            "observed aggregate FD count exceeds original cap")
    live = cells["last_live_aggregate"]
    require(type(live) is dict and set(live) == {"supervisor", "verifier", "gpg", "as_kib", "fd_counts"}
            and type(live["gpg"]) is list and len(live["gpg"]) <= 1,
            "complete joined live aggregate observations required")
    numeric_mm(live["supervisor"], 32768)
    numeric_mm(live["verifier"], parent_cap)
    for child in live["gpg"]:
        numeric_mm(child, 114688)  # original fork transient remains112MiB
    require(type(live["as_kib"]) is int and live["as_kib"] == aggregate ==
            live["supervisor"]["VmSize_kib"] + live["verifier"]["VmSize_kib"] +
            sum(child["VmSize_kib"] for child in live["gpg"]) and live["fd_counts"] == counts,
            "actual aggregate producer and consumer observation join failed")
    exit_record = outer["parent_exit"]
    bounded_number(exit_record["raw_wait4_ru_maxrss_kib"], 114688, "raw parent wait4 high-water")
    require(type(exit_record["complete_live_samples"]) is int and
            exit_record["complete_live_samples"] >= parent["complete_live_samples"],
            "actual parent sample count and wait4 resource join failed")
    return sup_peak


def validate_fullraw_layers(outer, inner):
    # Bothside consumer: full bytes, not counters/hash-only records.
    for name in ("stdout", "stderr"):
        cell = outer["output"][name]
        if cell["encoding"] == "same-inner-json-preimage":
            require(name == "stdout" and cell["raw_base64"] is None,
                    "same original stdout preimage required")
            raw = canonical_object_bytes(inner) + b"\n"
        else:
            require(cell["encoding"] == "base64", "full supervisor raw encoding")
            raw = base64.b64decode(cell["raw_base64"], validate=True)
        require(cell["partial"] is False and not cell["pending_raw_base64"] and
                len(raw) == cell["bytes"] == cell["produced_bytes"] and
                hashlib.sha256(raw).hexdigest() == cell["sha256"],
                "complete terminal supervisor raw/count/SHA required")
    total = 0
    require(inner["aggregate_gpg_output_charge_unknown"] is False,
            "unknown GPG production is not zero")
    for call in inner["calls"]:
        require(call["capture_complete"] is True and call["output_charge_unknown"] is False,
                "complete all-prefix inner raw capture required")
        for cell in [call[k] for k in ("stdout", "stderr", "status")] + call["written_data"]:
            if cell["encoding"] == "utf-8":
                raw = cell["text"].encode("utf-8", "strict")
            else:
                require(cell["encoding"] == "base64" and cell["text"] is None,
                        "original lossless output encoding")
                raw = base64.b64decode(cell["raw_base64"], validate=True)
            require(cell["retained"] is True and cell["partial"] is False and
                    cell["bytes"] == cell["produced_bytes"] == cell["output_charge_bytes"] == len(raw)
                    and hashlib.sha256(raw).hexdigest() == cell["sha256"] and
                    cell["identity_before"] == cell["identity_after"],
                    "complete inner raw/custody/count/charge mismatch")
            total += len(raw)
    require(total == inner["aggregate_gpg_output_bytes_including_written_key_and_body"],
            "aggregate original raw production/count mismatch")


def validate_canonical(case, receipt):
    outer = strict_json(receipt["stdout_text"].encode("ascii"), True)
    receipt["full_supervisor_receipt"] = outer  # retained before any qualifier
    if type(outer.get("inner_complete_evidence")) is dict:
        receipt["full_verifier_receipt"] = outer["inner_complete_evidence"]
    require(receipt["stderr_text"] == "" and receipt["exit_code"] == 0 and
            outer["schema"] == "friday.e4.node.actual-supervisor.sol060.v1" and
            outer["assignment"] == "ASTRA-E4-NODE-LAUNCH-OWNERSHIP-EVIDENCE-CLOSURE-A048"
            and type(outer["generation"]) is int and outer["generation"] == 1 and
            outer["qualified"] is True and outer["mode"] == case,
            "canonical complete outer success/transport/current binding required")
    require(not any(key in outer for key in ("failure", "cleanup_failure", "custody_failure",
            "home_failure", "descriptor_cleanup_failure", "inner_evidence_capture_failure")),
            "canonical terminal failure cannot qualify")
    inner = outer["inner_complete_evidence"]
    receipt["full_verifier_receipt"] = inner
    validate_fullraw_layers(outer, inner)
    require(inner["schema"] == "friday.e4.node.bounded-verifier.sol060.v1" and
            inner["assignment"] == outer["assignment"] and inner["generation"] == 1,
            "complete current verifier receipt required")
    require(outer["actual_launch"]["argv"][11:] == inner["reviewed_invocation"]["argv"] and
            inner["reviewed_invocation"]["source_sha256"] == VHASH and
            inner["reviewed_invocation"]["contract_sha256"] == CHASH and
            inner["reviewed_invocation"]["deadline_monotonic_ns"] == CANONICAL_END - 20000000000 and
            outer["contract_argv_linkage"]["complete_contract_sha256"] == CHASH,
            "current argv/hash/deadline bindings required")
    require(outer["custody_postexit"] is True and outer["custody_terminal"] is True and
            outer["all_supervisor_owned_descriptors_closed"] is True and
            outer["own_runtime_descriptor_closed"] is True and
            outer["home"]["postexit_absent"] is True and
            outer["owned_cleanup"]["exact_verifier_reaped"] is True and
            outer["owned_cleanup"]["actual_supervisor_children_empty"] is True and
            outer["owned_cleanup"]["all_held_generation_exits_confirmed"] is True and
            outer["all_generation_registration_bound_to_actual_enumeration"] is True and
            inner["final_custody_confirmed"] is True and
            inner["cleanup"]["exact_owned_child_reaped"] is True and
            inner["cleanup"]["all_own_and_input_and_component_descriptors_closed"] is True,
            "complete inner+outer actual final custody/ownership/closure required")
    validation = outer["inner_evidence_validation"]
    require(validation["complete_receipt_preserved"] is True and
            validation["common_observations_validated"] is True and
            validation["read_accounting_validated"] is True, "current full inner validation required")
    sup_peak = supervisor_resources(outer, case)
    expected_phases = {"startup", "before_cleanup"}
    if case == "verify-node":
        expected_phases.update("before_call_" + str(index) for index in range(1, 7))
        expected_phases.add("verification_end")
    require(inner["resource_schema"] == "friday.e4.node.verifier-resources.a172.v1" and
            type(inner["resources"]) is dict and set(inner["resources"]) == expected_phases,
            "complete original verifier resource phase schema required")
    for item in inner["resources"].values():
        require(type(item) is dict and set(item) == {"ru_maxrss_self_kib",
                "ru_maxrss_largest_child_kib", "current_proc_self_status", "elapsed_seconds"},
                "complete verifier self-phase resource fields required")
        cap = 65536 if case == "startup-as-negative" else 114688
        numeric_mm(item["current_proc_self_status"], cap)
        own, child = item["ru_maxrss_self_kib"], item["ru_maxrss_largest_child_kib"]
        bounded_number(own, cap, "raw verifier self high-water")
        bounded_number(child, 114688, "raw verifier largest-child high-water")
        bounded_number(item["elapsed_seconds"], 900, "verifier elapsed phase bound")
        require(type(own) in (int, float) and type(child) in (int, float) and
                0 <= own <= cap and 0 <= child <= 114688 and own + child + sup_peak <= 262144,
                "raw target aggregate cap; no inherited high-water subtraction")
    require(outer["implicit_io"] == "UNKNOWN_NOT_ZERO" and
            inner["explicit_read_accounting"]["implicit_io"] == "UNKNOWN_NOT_ZERO" and
            inner["explicit_read_accounting"]["child_observation"] == "UNKNOWN_NOT_ZERO" and
            inner["explicit_read_accounting"]["whole_content_charge_bytes"] ==
            32768 + inner["explicit_read_accounting"]["performing_verifier_content_bytes"],
            "honest actual inner read ledger required")
    if case != "verify-node":
        message = "nonroot exact three-variable startup environment required" if case == "startup-env-negative" else "pre-interpreter inherited limit mismatch"
        require(outer["status"] == "STARTUP_CONTROL_CAUSE_CONFIRMED" and
                outer["startup_control"]["passed"] is True and
                validation["intended_cause_validated"] is True and
                original_stock_cause(inner["failure"]) == {"type": "Refusal", "message": message} and
                inner["status"] == "NOT_PROVEN" and inner["calls"] == [] and
                outer["parent_exit"]["exit_code"] == 2 and
                inner["private_home_intent"]["state"] == "NOT_ATTEMPTED" and
                inner["cleanup"]["private_home_absent"] is None and
                inner["aggregate_gpg_output_bytes_including_written_key_and_body"] == 0,
                "exact startup cause/no-GPG/home-null evidence required")
    else:
        require(inner["readonly_keyring_policy"] == "friday.e4.node.readonly-keyring.a172.v1",
                "current readonly six-call keyring recipe required")
        for index, call in enumerate(inner["calls"]):
            argv = call["argv"][9:]
            require(argv.count("--keyring") == (1 if index >= 2 else 0) and
                    argv.count("--no-keyring") == (1 if index < 2 else 0) and
                    argv.count("--no-default-keyring") == (0 if index == 4 else 1),
                    "original six-call keyless or explicit sealed keyring recipe mismatch")
        require(outer["status"] == "QUALIFIED_SUPERVISED_NODE_RAW_PUBLISHER_CHAIN_PROVEN" and
                inner["status"] == "QUALIFIED_NODE_RAW_PUBLISHER_CHAIN_PROVEN" and
                outer["parent_exit"]["exit_code"] == 0 and
                validation["positive_chain_validated"] is True and
                [call["label"] for call in inner["calls"]] == ["show-raw-key", "dearmor",
                    "check-self-signatures-and-bindings", "positive-gpg", "positive-gpgv-body",
                    "negative-same-path-changed-row"] and
                all(call["reaped"] is True for call in inner["calls"]) and
                inner["authority"]["independently_expected_primary"] == PRIMARY and
                inner["positive_gpg"]["accepted"] is True and inner["positive_gpg"]["primary"] == PRIMARY and
                inner["positive_gpgv"]["accepted"] is True and
                inner["positive_gpgv"]["signer"] == inner["positive_gpg"]["signer"] and
                inner["gpgv_body_equals_strict_signed_body"] is True and
                inner["positive_archive"]["actual_sha256"] == ARCHIVE and
                inner["positive_archive"]["actual_bytes"] == 31058332 and
                inner["negative_signed_row"]["passed"] is True and inner["negative_archive"]["passed"] is True,
                "complete canonical six-call publisher positives/negatives required")


def validate_entry(case, receipt):
    if case == "verifier-entry-fd7":
        require(receipt["exit_code"] == 1 and receipt["stdout_text"] == "" and
                receipt["stderr_text"].startswith("Traceback (most recent call last):\n") and
                receipt["stderr_text"].endswith("RuntimeError: unknown bootstrap inherited descriptor\n") and
                receipt["own_control_private_name_absent_before"] is True and
                receipt["own_control_private_name_absent_after"] is True,
                "strict verifier entry guard exact uncaught RuntimeError before general imports")
        receipt["inner_receipt"] = None
        receipt["inner_telemetry"] = None
        receipt["inner_telemetry_unknown_reason"] = "intended pre-general-import guard has no JSON/resource receipt"
    else:
        outer = strict_json(receipt["stdout_text"].encode("ascii"), True)
        receipt["full_supervisor_receipt"] = outer
        require(receipt["exit_code"] == 2 and receipt["stderr_text"] == "" and
                outer["schema"] == "friday.e4.node.actual-supervisor.sol060.v1" and
                original_stock_cause(outer["failure"]) == {"type": "Refusal", "message": "unknown bootstrap inherited descriptor"} and
                outer["status"] == "NOT_PROVEN" and outer["qualified"] is False and
                outer["fork_state"] == "NOT_ATTEMPTED" and outer["process_history"] == [] and
                "actual_launch" not in outer and "inner_complete_evidence" not in outer and
                outer["home"]["name"] is None and outer["home"]["postexit_absent"] is None and
                outer["all_supervisor_owned_descriptors_closed"] is True,
                "supervisor exact strict entry cause before ctypes/ffi/fork required")
    receipt["cause"] = "unknown bootstrap inherited descriptor"
    receipt["cause_qualifier"] = "own readonly plaintext surplus descriptor; exit and complete bounded transport plus actual outer empty-child observation"


def public_value_bytes(value):
    total = 0
    for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False,
                                separators=(",", ":")).iterencode(value):
        due()
        total += len(part.encode("ascii"))
        require(total <= PUBLIC_CAP, "complete retained value exceeds outer output ceiling")
    return total


def canonical_object_bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode("ascii")


def case_raw_stdout(case):
    if case.get("stdout_text") is not None:
        return case["stdout_text"].encode("ascii", "strict")
    require(type(case.get("stdout_raw_base64")) is str,
            "complete raw stdout preimage absent")
    return base64.b64decode(case["stdout_raw_base64"], validate=True)


def wire_receipt(record):
    wire = dict(record)
    cases = [dict(case) for case in record["cases"]]
    wire["cases"] = cases
    wire["raw_preimage_schema"] = "friday.sol060.same-raw-object.v1"
    for original, case in zip(record["cases"], cases):
        if "full_supervisor_receipt" not in original:
            continue
        raw = case_raw_stdout(original)
        require(len(raw) == original["stdout_bytes"] and
                hashlib.sha256(raw).hexdigest() == original["stdout_sha256"],
                "full stdout reference identity/count/SHA mismatch")
        full = original["full_supervisor_receipt"]
        require(canonical_object_bytes(full) + b"\n" == raw,
                "parsed full supervisor has no exact canonical raw preimage")
        case["full_supervisor_receipt"] = {
            "$same_raw": "stdout-json", "bytes": len(raw),
            "sha256": original["stdout_sha256"]}
        if "full_verifier_receipt" in original:
            require(full.get("inner_complete_evidence") is original["full_verifier_receipt"],
                    "full verifier is not the original same-object inner subtree")
            case["full_verifier_receipt"] = {"$same_raw": "supervisor-inner"}
    return wire


def recover_wire_receipt(wire):
    # Existing caller is also the complete codec consumer: no new role/provider.
    # Only the SAME case's complete existing raw bytes are admissible references.
    require(wire["raw_preimage_schema"] == "friday.sol060.same-raw-object.v1",
            "exact current fullraw reference grammar required")
    record = dict(wire)
    del record["raw_preimage_schema"]
    cases = [dict(case) for case in wire["cases"]]
    record["cases"] = cases
    for case in cases:
        full = case.get("full_supervisor_receipt")
        if type(full) is not dict or "$same_raw" not in full:
            continue
        require(set(full) == {"$same_raw", "bytes", "sha256"} and
                full["$same_raw"] == "stdout-json", "full supervisor reference grammar")
        raw = case_raw_stdout(case)
        require(len(raw) == full["bytes"] == case["stdout_bytes"] and
                hashlib.sha256(raw).hexdigest() == full["sha256"] == case["stdout_sha256"],
                "same raw preimage count/SHA mismatch")
        parsed = strict_json(raw, True)
        require(canonical_object_bytes(parsed) + b"\n" == raw,
                "lossless parsed preimage canonical equality required")
        case["full_supervisor_receipt"] = parsed
        inner = case.get("full_verifier_receipt")
        if type(inner) is dict and "$same_raw" in inner:
            require(inner == {"$same_raw": "supervisor-inner"} and
                    type(parsed.get("inner_complete_evidence")) is dict,
                    "exact original full inner subtree required")
            case["full_verifier_receipt"] = parsed["inner_complete_evidence"]
    return record


def public_partition_bytes():
    # This is a byte partition, NOT a shortened output or a placeholder copy.
    # Container punctuation and each key/colon belong to metadata. Only the six
    # explicitly named whole case payload VALUES belong to that case's cell.
    # Every other whole value belongs to metadata, including all error branches.
    # The cells therefore cover the normal complete JSON exactly and disjointly.
    projected = wire_receipt(R)
    fixed = 2 + max(0, len(projected) - 1)
    payloads = [0] * len(CASES)
    require(type(R.get("cases")) is list and len(R["cases"]) == len(CASES),
            "complete original all-six publication rows required")
    for key, value in projected.items():
        fixed += public_value_bytes(key) + 1
        if key != "cases":
            fixed += public_value_bytes(value)
            continue
        fixed += 2 + max(0, len(value) - 1)
        for index, row in enumerate(value):
            require(type(row) is dict, "complete case publication object required")
            fixed += 2 + max(0, len(row) - 1)
            for label, item in row.items():
                fixed += public_value_bytes(label) + 1
                size = public_value_bytes(item)
                if label in PUBLIC_PAYLOAD_KEYS:
                    payloads[index] += size
                else:
                    fixed += size
    return fixed, payloads


def public_plan_fits(index):
    require(type(index) is int and 0 <= index <= len(CASES),
            "invalid whole publication boundary")
    fixed, payloads = public_partition_bytes()
    require(fixed <= PUBLIC_FIXED_RESERVE,
            "complete metadata exceeds independently bounded whole metadata reserve")
    require(all(size <= bound for size, bound in zip(payloads, PUBLIC_CASE_PAYLOAD_BOUNDS)),
            "complete case payload exceeds original producer/raw publication bound")
    require(not any(payloads[index:]), "future case payload already present before fork")
    # Observed fixed bytes are inside the ONE whole metadata allowance, never
    # added a second time. Only past payloads are present; only future cells are
    # reserved. Only exact reversible preimage sharing, never raw field removal.
    future = sum(PUBLIC_CASE_PAYLOAD_BOUNDS[index:])
    require(PUBLIC_FIXED_RESERVE + sum(payloads[:index]) + future + 1 <= PUBLIC_CAP,
            "complete future serialization reserve unavailable before fork")


def finish_terminal():
    global TERMINAL_ATTEMPTED, PROC_PHASE, STOP
    if TERMINAL_ATTEMPTED:
        return
    TERMINAL_ATTEMPTED = True
    previous_phase = PROC_PHASE
    PROC_PHASE = "terminal"
    problems = []
    try:
        if not CLEANUP_ATTEMPTED:
            try:
                settle_owned()
            except BaseException as exc:
                STOP = True
                problems.append({"stage": "known_own_cleanup", "error": exact_error(exc)})
        elif R.get("owned_cleanup_complete") is not True:
            STOP = True
            problems.append({"stage": "known_own_cleanup", "type": "Refusal",
                             "message": "earlier single cleanup incomplete; retained without replay"})
        try:
            require(direct_children() == [], "actual terminal own child set not empty")
            R["terminal_actual_children_empty"] = True
            for record in TRACKED:
                require(record["evidence"].get("no_creation_confirmed") is True or
                        record["pidfd"] is not None and exited(record),
                        "actual terminal held generation exit missing")
            R["terminal_actual_generation_exits_confirmed"] = True
            for record in TRACKED:
                if record["evidence"].get("no_creation_confirmed") is not True:
                    prepare_proc_retirement(record)
        except BaseException as exc:
            STOP = True
            problems.append({"stage": "terminal_own_observation", "error": exact_error(exc)})
        try:
            require(SELECT_META is not None and set(HELD) == set(FIXED) | set(LOCAL),
                    "full nineteen held inputs unavailable; partial custody cannot qualify")
            custody("terminal_all_inputs_source_tools", tuple(HELD), DEADLINE - 5000000000)
            R["terminal_full_nineteen_SHA_confirmed"] = True
        except BaseException as exc:
            STOP = True
            problems.append({"stage": "terminal_full_custody", "error": exact_error(exc)})
        try:
            sample_outer("terminal")
            R["terminal_current_memory_observed"] = True
        except BaseException as exc:
            STOP = True
            problems.append({"stage": "terminal_current_resources", "error": exact_error(exc)})
        R["terminal_once"] = {"attempted": True, "problems": problems,
                              "cleanup_attempted_once": CLEANUP_ATTEMPTED,
                              "complete": not problems and R.get("owned_cleanup_complete") is True}
    finally:
        PROC_PHASE = previous_phase
    if problems:
        raise Refusal("one or more complete terminal observations failed")


def main():
    global HOST_START, DEADLINE, CANONICAL_END, CONTROLS_END, CASE_INDEX, PROC_PHASE
    require(len(sys.argv) == 5 and sys.argv[0] == LOCAL["caller"] and
            sys.argv[1] == "--host-start-monotonic-ns" and sys.argv[2].isdigit() and
            sys.argv[3] == "--host-deadline-monotonic-ns" and sys.argv[4].isdigit(),
            "Root must supply exact new external clock CLI")
    HOST_START, DEADLINE = int(sys.argv[2]), int(sys.argv[4])
    require(0 < HOST_START <= START <= now() < DEADLINE and
            60000000000 < DEADLINE - START and DEADLINE - HOST_START <= 900000000000,
            "new finite external monotonic900s window required")
    CANONICAL_END = DEADLINE - 25000000000
    signal.signal(signal.SIGALRM, external_deadline_alarm)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
    signal.setitimer(signal.ITIMER_REAL, max(0.001, (DEADLINE - now()) / 1e9))
    R["external_clock"] = {"host_start_ns": HOST_START, "host_deadline_ns": DEADLINE,
        "canonical_absolute_end_ns": CANONICAL_END, "unchanged_all_three": True,
        "ordinary_controls_shared_reserve_seconds": 5, "outer_final_reserve_seconds": 20,
        "cleanup_within_final_reserve_seconds": 5, "clock_issuer": "external Root native tool, not Source"}
    require(os.getresuid() == (1000, 1000, 1000) and os.getresgid() == (1000, 1000, 1000) and
            dict(os.environ) == ENV, "outer qualified stock unprivileged ENV3 required")
    install_plan()
    require(entry_fds() == {0, 1, 2, 3}, "outer unknown inherited descriptor; never reclassify")
    require(resource.getrlimit(resource.RLIMIT_AS) == (AS_OUTER, AS_OUTER) and
            resource.getrlimit(resource.RLIMIT_CPU) == (60, 120) and
            resource.getrlimit(resource.RLIMIT_FSIZE) == (PUBLIC_CAP, PUBLIC_CAP) and
            resource.getrlimit(resource.RLIMIT_NOFILE) == (256, 256) and
            resource.getrlimit(resource.RLIMIT_CORE) == (0, 0),
            "Root must impose outer caps before outer Python startup")
    kernel_policy()
    sample_outer("entry")
    require(direct_children() == [], "outer starts with unknown child")
    R["initial_children_empty"] = True
    selection_data()
    for name in SELECTED:
        hold(name, SELECTED)
    require(full9(os.stat("/proc/self/exe")) == HELD["python"]["identity9"],
            "actual outer interpreter must equal Root-selected pinned stock Python")
    take_budget("expectations_body", HELD["expectations"]["size"] + 1)
    data = read_at(HELD["expectations"]["fd"], HELD["expectations"]["size"] + 1, 0)
    expectations = strict_json(data, data.endswith(b"\n"))
    require(expectations["schema"] == "friday.e4.node.ordinary-case-expectations.sol060.v1" and
            expectations["case_order"] == list(CASES) and expectations["authority"] == "NONE_ORDINARY_DATA",
            "current complete ordinary case DATA required")
    require(expectations.get("lifetime_memory_source_contract") == LIFETIME_MEMORY_CONTRACT,
            "current complete retained-lease/natural-ending ordinary DATA contract required")
    require(expectations.get("publication_source_contract") == PUBLICATION_SOURCE_CONTRACT,
            "current complete disjoint publication ordinary DATA contract required")
    require(expectations.get("resource_receipt_schemas") == RESOURCE_SCHEMA_IDS,
            "current producer and consumer resource schema DATA required")
    R["expected_contract_DATA"] = expectations
    public_plan_fits(0)
    for index, case in enumerate(CASES):
        CASE_INDEX = index
        PROC_PHASE = "launch"
        receipt = R["cases"][index]
        if STOP:
            receipt["reason"] = "sticky ownership/custody/cleanup unknown"
            continue
        if index == 3:
            CONTROLS_END = min(DEADLINE - 20000000000, now() + 5000000000)
            R["external_clock"]["ordinary_controls_single_end_ns"] = CONTROLS_END
        end = CANONICAL_END if index < 3 else CONTROLS_END
        receipt["started_monotonic_ns"] = now()
        if not remaining_cases_fit(index):
            for later in R["cases"][index:]:
                later["status"] = "NOT_RUN"
                later["reason"] = "read budget unavailable before fork; later cases not started"
            break
        if now() >= end or index != 5 and CANONICAL_END - now() <= 60000000000:
            receipt["reason"] = "absolute phase cutoff/reserves unavailable; no deadline refresh"
            continue
        receipt.update(status="NOT_PROVEN", reason=None)
        launched_and_complete = False
        qualified_case = False
        try:
            public_plan_fits(index)
            launch(case, receipt, end)
            launched_and_complete = True
            validate_canonical(case, receipt) if index < 3 else validate_entry(case, receipt)
            qualified_case = True
        except BaseException as exc:
            receipt["failure"] = exact_error(exc)
            if ("read budget unavailable" in str(exc) or
                    "read capacity unavailable" in str(exc) or
                    str(exc) == "outer cumulative read ceiling exceeded"):
                receipt["reason"] = "read budget unavailable before next read; not a descriptor or startup cause"
                ownership_index = receipt.get("ownership_index")
                if ownership_index is None or TRACKED[ownership_index]["evidence"]["fork_state"] == "NOT_ATTEMPTED":
                    receipt["status"] = "NOT_RUN"
                for later in R["cases"][index + 1:]:
                    later["status"] = "NOT_RUN"
                    later["reason"] = "read budget unavailable before fork; later cases not started"
            if not launched_and_complete:
                globals()["STOP"] = True
            if not CLEANUP_ATTEMPTED and not launched_and_complete:
                try:
                    settle_owned()
                except BaseException as cleanup_exc:
                    globals()["STOP"] = True
                    receipt["cleanup_failure"] = exact_error(cleanup_exc)
            # A stopped/incomplete supervisor is not a product-only red result.
            if receipt.get("full_supervisor_receipt", {}).get("status") == "STOP_UNCONFIRMED":
                globals()["STOP"] = True
            if "full_supervisor_receipt" not in receipt and receipt.get("stdout_text") and index != 5:
                globals()["STOP"] = True
        receipt["completed_monotonic_ns"] = now()
        PROC_PHASE = "close"
        try:
            sample_outer("completed_" + case)
        except BaseException as exc:
            globals()["STOP"] = True
            qualified_case = False
            receipt["completion_resource_failure"] = exact_error(exc)
        if qualified_case and not STOP:
            receipt["status"] = "CAUSE_CONFIRMED" if case != "verify-node" else "QUALIFIED_NODE_PUBLISHER_CHAIN_ONLY"
    finish_terminal()
    successful = all(row["status"] in ("CAUSE_CONFIRMED", "QUALIFIED_NODE_PUBLISHER_CHAIN_ONLY") for row in R["cases"])
    R["qualified_node_only"] = successful and not STOP
    R["status"] = "QUALIFIED_NODE_PUBLISHER_CHAIN_AND_OWN_CAUSES_ONLY" if successful and not STOP else "NOT_PROVEN"


def close_final():
    global FD_CLOSE_ATTEMPTS, FD_CLOSE_SUCCESSES
    closed = []
    for fd in sorted(list(OWN.values()), key=int, reverse=True):
        try:
            close_owned(fd)
            closed.append(fd)
        except BaseException as exc:
            R.setdefault("descriptor_closure_errors", []).append({"fd": int(fd),
                "lease_generation":fd.lease_generation,"errno":getattr(exc,"errno",None),
                "type":type(exc).__name__, "error": exact_error(exc)})
            globals()["STOP"] = True
    # A failed wrapper/map/fstat must not erase an already returned allocation.
    # These never-published records are exact original native returns, not guessed
    # integer slots and not admitted input/proc leases with relaxed identity.
    for record in FD_ALLOCATION_HISTORY[:FD_ALLOCATION_USED]:
        lease = record["lease"]
        if record["state"] != "RETURNED" or record["attempted"]:
            continue
        if lease is not None and OWN.get(int(lease)) is lease:
            continue  # previous identity refusal is sticky; not a retry bypass
        saved = signal.pthread_sigmask(signal.SIG_BLOCK, MASK)
        try:
            record["attempted"], record["state"] = True, "RETIRE_UNKNOWN"
            if lease is not None:
                lease.retired = True
            FD_CLOSE_ATTEMPTS += 1
            os.close(record["number"])
            record["state"] = "CONFIRMED_CLOSED"
            FD_CLOSE_SUCCESSES += 1
        except BaseException as exc:
            record["error_object"] = exc
            record["error"] = exact_error(exc)
            FD_CLOSE_FAILURES.append({"fd": record["number"], "role": record["role"],
                                      "unpublished_returned_allocation": True,
                                      "error": record["error"]})
            globals()["STOP"] = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, saved)
    # fd3 is a caller-selected ordinary input. It is known and accepted above,
    # and may be closed only if its identity was actually registered.
    if SELECT_META is not None:
        try:
            require(full9(os.fstat(3)) == SELECT_META, "known selected DATA identity changed before closure")
            require(not SELECT_CLOSE["attempted"], "selected DATA once-close already attempted")
            SELECT_CLOSE["attempted"], SELECT_CLOSE["state"] = True, "RETIRE_UNKNOWN"
            os.close(3)
            SELECT_CLOSE["state"] = "CONFIRMED_CLOSED"
            closed.append(3)
        except BaseException as exc:
            SELECT_CLOSE["error_object"] = exc
            R["selected_DATA_closure_failure"] = exact_error(exc)
            globals()["STOP"] = True
    R["known_owned_fds_closed"] = closed
    R["selected_DATA_onceclose"] = {"attempted": SELECT_CLOSE["attempted"],
                                    "state": SELECT_CLOSE["state"]}
    R["fd_lease_closure"] = {"allocations":FD_LEASE_GENERATION,
        "close_attempts":FD_CLOSE_ATTEMPTS,"successful_closes":FD_CLOSE_SUCCESSES,
        "unconfirmed_closes":list(FD_CLOSE_FAILURES),"remaining_leases":len(OWN),
        "retired_alias_never_closes_new_slot_lease":True,
        "tracked_pidfd_procfd_retained_through_terminal":True,
        "pidfd_poll_calls":PIDFD_POLL_CALLS,"lease_check_calls":LEASE_CHECK_CALLS,
        "clock_calls":CLOCK_CALLS,"natural_ending_attempts":NATURAL_EXIT_ATTEMPTS,
        "implicit_IO":"UNKNOWN_NOT_ZERO"}
    R["all_known_owned_fds_closed"] = (len(OWN) == 0 and not FD_CLOSE_FAILURES
        and SELECT_META is not None and 3 in closed
        and all(r["state"] in ("PLANNED", "CONFIRMED_CLOSED") for r in FD_ALLOCATION_HISTORY[:FD_ALLOCATION_USED]))
    R["descriptor_allocation_history"] = [{k:r[k] for k in
        ("role", "number", "state", "attempted", "error")} for r in FD_ALLOCATION_HISTORY[:FD_ALLOCATION_USED]]


def encode_complete():
    public_plan_fits(len(CASES))
    blocks, total = [], 0
    projected = wire_receipt(R)
    restored = recover_wire_receipt(projected)
    require(restored == R, "lossless complete original receipt round-trip failed")
    for part in json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":")).iterencode(projected):
        due()
        block = part.encode("ascii")
        total += len(block)
        require(total + 1 <= PUBLIC_CAP, "complete outer2MiB output does not fit; no field dropping")
        blocks.append(block)
    return b"".join(blocks) + b"\n"


def emit_complete(data):
    flags = fcntl.fcntl(1, fcntl.F_GETFL)
    fcntl.fcntl(1, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    offset = 0
    while offset < len(data):
        due()
        try:
            written = os.write(1, data[offset:offset + 4096])
            require(written > 0, "outer native-tool transport made no progress")
            offset += written
        except BlockingIOError:
            select.select([], [1], [], min(0.01, max(0, (DEADLINE - now()) / 1e9)))
    due()


def external_deadline_alarm(signum, frame):
    raise Refusal("actual outer external absolute deadline alarm")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        R["failure"] = exact_error(exc)
        R["qualified_node_only"] = False
    finally:
        if DEADLINE > 0:
            try:
                finish_terminal()
            except BaseException as exc:
                STOP = True
                R["terminal_failure"] = exact_error(exc)
        close_final()
    R["explicit_reads"]["outer_actual_content_bytes"] = READS
    R["explicit_reads"]["own_child_bootstrap_upper_debit_bytes"] = CHILD_READ_RESERVE
    R["explicit_reads"]["whole_charge_bytes"] = READS + CHILD_READ_RESERVE
    R["explicit_reads"]["own_child_content_observation"] = "UNKNOWN_NOT_ZERO; fixed conservative debit, no copied prefix"
    R["explicit_reads"]["plan_reserve_remaining_bytes"] = PLAN_RESERVE
    R["explicit_reads"]["plan_buckets_remaining"] = dict(LEDGER)
    R["actual_elapsed_seconds"] = (now() - HOST_START) / 1e9 if HOST_START else None
    R["completed_msk"] = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).isoformat()
    if STOP or not R["all_known_owned_fds_closed"] or not R.get("actual_children_empty"):
        R["qualified_node_only"] = False
        R["status"] = "STOP_UNCONFIRMED" if STOP else "NOT_PROVEN"
    try:
        data = encode_complete()
        emit_complete(data)
    except BaseException:
        # Never substitute a shortened proof. External Root must reject absent,
        # partial, oversized output and nonzero exit. No fresh deadline here.
        os._exit(3 if STOP else 2)
    os._exit(0 if R["qualified_node_only"] else 3 if STOP else 2)
