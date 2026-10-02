"""Bounded isolated official Python test subprocesses; exact retained launch record."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PACKAGE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sol019_dispatch_receipts", PACKAGE / "tests/receipt_contract.py")
RECEIPTS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RECEIPTS)


def encode(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("ascii")


TESTS = ("test_canonical", "test_manifest", "test_provenance", "test_archive",
         "test_broker", "test_ledger", "test_custody", "test_install_recovery",
         "test_uninstall", "test_effect_bills")
SHARDS = (("test_canonical", "test_manifest", "test_provenance", "test_archive"),
          ("test_broker", "test_ledger", "test_custody"),
          ("test_install_recovery",), ("test_uninstall", "test_effect_bills"))


def resources(usage_start):
    return {"run_context": RECEIPTS.run_binding(), "address_space_bytes_max": RECEIPTS.DISPATCHER_AS_LIMIT,
        "cpu_seconds_max": RECEIPTS.ASSIGNMENT_WALL_SECONDS,
        "actual_rlimit_as": list(resource.getrlimit(resource.RLIMIT_AS)),
        "actual_rlimit_cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
        "usage_start": usage_start, "usage": list(resource.getrusage(resource.RUSAGE_SELF))}


def dispatcher_invocation():
    # Report the consumed environment fields only; a full shell environment may
    # carry credentials and is not a closed-startup proof. Workers receive an
    # independently checked exact six-field environment.
    fields = ("HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE")
    return {"run_context": RECEIPTS.run_binding(), "argv": [sys.executable, "-I", "-S", "-B"] + sys.argv,
        "cwd": str(Path.cwd()), "environment": {key: os.environ.get(key) for key in fields},
        "environment_scope": "consumed-fields-only; protected-startup NOT_PROVEN",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "isolation": {"isolated": sys.flags.isolated, "no_site": sys.flags.no_site,
            "dont_write_bytecode": sys.flags.dont_write_bytecode}}


def new_deadline(value=None):
    deadline = RECEIPTS.current_deadline()
    if value is not None and RECEIPTS.instant(value) != RECEIPTS.instant(deadline["assignment_deadline_utc"]):
        raise ValueError("caller cannot extend or replace independently admitted RUN deadline")
    return deadline


def write_new(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())


def launch(parent, run, *, only=None, case_ids=None, collect=False, harness=False, deadline=None, diagnostic_lane=None):
    if run.exists() or run.is_symlink():
        raise ValueError("fresh retained root already exists")
    script = "harness_selfcheck.py" if harness else "run_tests.py"
    argv = ["/usr/bin/python3.14", "-I", "-S", "-B", str(PACKAGE / "tests" / script),
            "--run-root", str(run)]
    if only:
        argv += ["--only"] + list(only)
    if case_ids:
        argv += ["--case-id"] + list(case_ids)
    if collect:
        argv += ["--collect-only"]
    deadline = deadline or new_deadline()
    if diagnostic_lane:
        argv += ["--diagnostic-lane", diagnostic_lane]
    argv += RECEIPTS.run_context_flags()
    argv += ["--assignment-deadline-utc", deadline["assignment_deadline_utc"],
        "--seal-reserve-seconds", str(deadline["seal_reserve_seconds"])]
    env = {"HOME": str(run / "home"), "TMPDIR": str(run / "tmp"), "PATH": "/usr/bin:/bin",
           "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
    stdout_path, stderr_path = parent / (run.name + ".stdout"), parent / (run.name + ".stderr")
    RECEIPTS.raw_client().note("dispatcher.worker_launch.begin",[],{
        "run_root":str(run),"argv":argv,"stdout_ref":str(stdout_path.relative_to(PACKAGE)),
        "stderr_ref":str(stderr_path.relative_to(PACKAGE)),
        "ordinary_raw_member_consumer":"receipt_contract.raw_member"})
    record = {"argv": argv, "environment": env, "cwd": str(PACKAGE),
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "run_root": str(run),
              "deadline": deadline,
              "stdout_ref": str(stdout_path.relative_to(PACKAGE)),
              "stderr_ref": str(stderr_path.relative_to(PACKAGE))}
    clock = time.monotonic()
    remaining = (RECEIPTS.instant(deadline["assignment_deadline_utc"]) - RECEIPTS.instant(record["started_at_utc"])).total_seconds() - deadline["seal_reserve_seconds"]
    if remaining <= 1:
        record.update(admission_status="NOT_RUN", reason="NOT_RUN_ABSOLUTE_ASSIGNMENT_SEAL_DEADLINE",
            rc=125, timed_out=False, timeout_sec=0, completed_at_utc=record["started_at_utc"], elapsed_seconds=0)
        return record
    timeout = min(300, remaining - 1)
    RECEIPTS.validate_deadline(deadline, record["started_at_utc"], timeout)
    record.update(admission_status="RUN", timeout_sec=timeout)
    with os.fdopen(os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stdout, \
            os.fdopen(os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stderr:
        try:
            child = subprocess.run(argv, env=env, cwd=PACKAGE, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, timeout=timeout, check=False,
                                   start_new_session=True, pass_fds=RECEIPTS.observed_fds(RECEIPTS.PARENT_CUSTODY_FDS),
                                   close_fds=True)
            record["rc"], record["timed_out"] = child.returncode, False
        except subprocess.TimeoutExpired:
            record["rc"], record["timed_out"] = 124, True
    record["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    record["elapsed_seconds"] = time.monotonic() - clock
    record["stdout_sha256"] = hashlib.sha256(stdout_path.read_bytes()).hexdigest()
    record["stderr_sha256"] = hashlib.sha256(stderr_path.read_bytes()).hexdigest()
    RECEIPTS.raw_client().note("dispatcher.worker_launch.completed",[],record)
    return record


def full_pass(parent, run, *, deadline=None):
    """Collect the exact inventory; execute every case once in disjoint workers.

    Max four workers, each isolated/non-root and bounded300s; no mocked replacement
    or omitted case. Full collection and source identity are proved independently
    before combining their deterministic semantic evidence.
    """
    began, clock = datetime.now(timezone.utc).isoformat(), time.monotonic()
    usage_start = list(resource.getrusage(resource.RUSAGE_SELF))
    deadline = deadline or new_deadline()
    collector = launch(parent, run, collect=True, deadline=deadline)
    detail = {"run_root": str(run), "collection_launch": collector, "workers": [], "rc": collector["rc"]}
    def finish():
        detail.update(started_at_utc=began, completed_at_utc=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=time.monotonic() - clock, deadline=deadline, dispatcher_resources=resources(usage_start))
        return detail
    if collector["rc"] != 0:
        return finish()
    expected = json.loads((run / "collection.json").read_bytes())
    RECEIPTS.validate_expected_union(PACKAGE, expected)
    RECEIPTS.validate_collection(expected, RECEIPTS.source_inventory(PACKAGE))
    if not expected["full_inventory"] or expected["selected_test_modules"] != list(TESTS):
        raise ValueError("mandatory full collection changed")
    plans = [(SHARDS[0], None), (SHARDS[1], None), (("test_effect_bills",), None)]
    # Each generated install/remove batch is one bounded process, while the
    # independent collector fixes the exact entire case/row universe first.
    for module in ("test_install_recovery", "test_uninstall"):
        plans += [((module,), [identifier]) for identifier in expected["test_ids"] if identifier.startswith(module + ".")]
    detail["execution_plan"] = {"workers": [{"selected_test_modules": list(group), "case_ids": ids} for group, ids in plans],
        "planned_worker_count": len(plans), "max_concurrent": 4, "per_worker_timeout_seconds": 300,
        "worst_case_worker_waves_seconds": ((len(plans) + 3) // 4) * 300,
        "first_pass_duration_estimate": None, "admission": "each queued launch rechecks common immutable seal deadline"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(launch, parent, parent / (run.name + "-worker" + str(i + 1)), only=group, case_ids=ids, deadline=deadline)
                   for i, (group, ids) in enumerate(plans)]
        workers = [future.result() for future in pending]
    detail["workers"] = workers
    detail["rc"] = 0 if all(w["rc"] == 0 for w in workers) else 1
    if detail["rc"]:
        return finish()
    projection = {"run_context": RECEIPTS.run_binding(), "collection": {}, "controls": [], "observations": [], "matrices": {},
                  "source_hashes": dict(expected["source_hashes"]), "forbidden_effect_attempts": [],
                  "matrix_test_ids": {},
                  "module_contracts": {}, "control_contract": expected["control_contract"]}
    records, logs = [], []
    all_case_ids = []
    for worker, (selected, case_ids) in zip(workers, plans):
        worker_root = Path(worker["run_root"])
        record, part = RECEIPTS.validate_worker(PACKAGE, worker, expected, expected["source_hashes"])
        all_case_ids.extend(record["selected_test_ids"])
        if not record["success"] or record["selected_test_modules"] != list(selected):
            raise ValueError("worker collection/success mismatch")
        if (not case_ids and part.get("control_closure", {}).get("complete") is not True) or record.get("host_effect_fence_installed") is not True:
            raise ValueError("worker mandatory source-method/effect boundary incomplete")
        if set(part["matrices"]) & set(projection["matrices"]):
            raise ValueError("duplicate shard matrix")
        for path, sha in part["source_hashes"].items():
            if expected["source_hashes"].get(path) != sha:
                raise ValueError("shards do not use the collected source snapshot")
        for module, count in part["collection"].items():
            if module in projection["collection"] and projection["collection"][module] != count:
                raise ValueError("repeated module collection changed")
            projection["collection"][module] = count
        projection["module_contracts"].update(part["module_contracts"])
        projection["controls"].extend(part["controls"])
        projection["observations"].extend(part["observations"])
        projection["matrices"].update(part["matrices"])
        projection["matrix_test_ids"].update(part["matrix_test_ids"])
        projection["forbidden_effect_attempts"].extend(part["forbidden_effect_attempts"])
        records.append(record)
        logs.append((worker_root / "unittest.log").read_bytes())
    if projection["collection"] != expected["collection"]:
        raise ValueError("incomplete full collection union")
    projection["control_contract"]["required_observations"] = sorted(set(projection["control_contract"]["required_observations"]))
    projection["control_contract"]["declared_modules"] = sorted(set(projection["control_contract"]["declared_modules"]))
    if sorted(all_case_ids) != expected["test_ids"]:
        raise ValueError("partial case union omitted or duplicated a mandatory case")
    if projection["control_contract"] != expected["control_contract"]:
        raise ValueError("executed control contract differs from independent full precollection")
    contract = expected["control_contract"]
    actual_keys = {row["category"] + ":" + row["key"] for row in projection["observations"]}
    if set(projection["matrices"]) != set(contract["matrices"]) or set(contract["required_observations"]) - actual_keys:
        raise ValueError("missing/extra mandatory matrix or negative observation union")
    for name, rows in contract["matrices"].items():
        if projection["matrices"][name] != {"expected": rows, "observed": rows}:
            raise ValueError("mandatory actual matrix union differs: " + name)
    projection["control_closure"] = {"complete": True, "missing_matrices": [],
        "unexpected_matrices": [], "mismatched_matrices": [], "missing_observations": [],
        "kernel_or_privileged_enforcement_credit": False}
    ordinary = sorted(row["id"] for row in projection["controls"] if row["id"] in expected["test_ids"])
    if ordinary != expected["test_ids"] or any(row["status"] != "PASS" for row in projection["controls"]):
        raise ValueError("missing/duplicate/failed mandatory test case")
    for path, sha in expected["source_hashes"].items():
        if hashlib.sha256((PACKAGE / path).read_bytes()).hexdigest() != sha:
            raise ValueError("source changed during full worker pass")
    projection["source_hashes"]["tests/execute_pair.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    projection["controls"].sort(key=lambda row: encode(row))
    projection["observations"].sort(key=lambda row: encode(row))
    write_new(run / "semantic-projection.json", projection)
    with (run / "unittest.log").open("xb") as stream:
        stream.write(b"\n".join(logs))
        stream.flush()
        os.fsync(stream.fileno())
    success = not projection["forbidden_effect_attempts"]
    record = {"schema": "friday.a049.official-test-run.v1", "started_at_utc": began,
              "completed_at_utc": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": time.monotonic() - clock,
              "rc": 0 if success else 1, "success": success, "test_count": sum(r["test_count"] for r in records),
              "subcontrol_count": len(projection["controls"]), "failures": 0, "errors": 0, "skips": 0,
              "semantic_sha256": hashlib.sha256(encode(projection)).hexdigest(),
              "selected_test_modules": list(TESTS), "full_inventory": True,
              "host_effect_fence_installed": True, "control_closure": projection["control_closure"],
              "source_hashes": expected["source_hashes"], "selected_test_ids": expected["test_ids"], "deadline": deadline,
              "worker_launches": workers, "collection_launch": collector,
              "dispatcher_invocation": dispatcher_invocation(),
              "resources": {"run_context": RECEIPTS.run_binding(), "parallel_workers_max": 4,
                  "aggregate_address_space_bytes_max": 4 * RECEIPTS.WORKER_AS_LIMIT + RECEIPTS.DISPATCHER_AS_LIMIT,
                  "dispatcher_address_space_bytes_max": RECEIPTS.DISPATCHER_AS_LIMIT,
                  "dispatcher": resources(usage_start),
                  "worker_resources": [r["resources"] for r in records]},
              "execution": "exact full collection partitioned into four disjoint isolated workers",
              "worker_result_refs": [str(Path(w["run_root"]).relative_to(PACKAGE)) + "/run-result.json" for w in workers]}
    write_new(run / "run-result.json", record)
    detail["rc"] = record["rc"]
    detail["elapsed_seconds"] = time.monotonic() - clock
    detail["started_at_utc"] = began
    detail["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    RECEIPTS.validate_pass(PACKAGE, detail, run, expected["source_hashes"])
    detail["elapsed_seconds"] = time.monotonic() - clock
    return finish()


def focused_checks(parent, label, deadline, usage_start):
    """A separate admitted short slice: full collect, selfcheck, exact methods."""
    RECEIPTS.validate_mode_admission("collect")
    RECEIPTS.validate_mode_admission("selfcheck")
    RECEIPTS.validate_mode_admission("affected")
    plan, _ = RECEIPTS.object_member(PACKAGE, "schemas/affected-check-plan.v1.json")
    if (plan.get("schema") != "friday.a049.affected-check-plan.v1"
            or plan.get("parallel_workers_max") != 4 or plan.get("worker_seconds_max") != 300
            or plan.get("worker_memory_bytes_max") != RECEIPTS.WORKER_AS_LIMIT
            or plan.get("full_pair_authorized") is not False):
        raise ValueError("strict finite short-check source plan")
    collected = parent / (label + "-collect")
    collection_launch = launch(parent, collected, collect=True, deadline=deadline)
    if collection_launch["rc"] != 0:
        raise ValueError("fresh actual full collection failed; no ordinary execution")
    collection, collection_sha = RECEIPTS.object_member(PACKAGE,
        str(collected.relative_to(PACKAGE)) + "/collection.json")
    RECEIPTS.validate_collection(collection, RECEIPTS.source_inventory(PACKAGE))
    RECEIPTS.validate_expected_union(PACKAGE, collection)
    selfcheck_launch = launch(parent, parent / (label + "-selfcheck"), harness=True, deadline=deadline)
    if selfcheck_launch["rc"] != 0:
        raise ValueError("strict actual selfcheck failed; no ordinary execution")
    RECEIPTS.validate_harness_selfcheck(PACKAGE, selfcheck_launch, collection["source_hashes"])
    groups = plan["groups"]
    ids = sorted(identifier for group in groups for identifier in group["case_ids"])
    if ids != plan["expected_method_ids"] or len(ids) != len(set(ids)) or not set(ids) <= set(collection["test_ids"]):
        raise ValueError("exact source-declared short method union")
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(launch, parent, parent / (label + "-focused-worker" + str(number)),
            only=group["modules"], case_ids=group["case_ids"], deadline=deadline)
            for number, group in enumerate(groups, 1)]
        workers = [job.result() for job in jobs]
    if any(worker["rc"] != 0 for worker in workers):
        raise ValueError("affected method FAIL/timeout retained; no fabricated focused closure")
    for worker in workers:
        RECEIPTS.validate_worker(PACKAGE, worker, collection, collection["source_hashes"])
    selfcheck_ref = str(Path(selfcheck_launch["run_root"]).relative_to(PACKAGE)) + "/run-receipt.json"
    _, selfcheck_sha = RECEIPTS.object_member(PACKAGE, selfcheck_ref)
    document = {"schema": "friday.a049.focused-revalidation.v1",
        "source_hashes": collection["source_hashes"],
        "collection_ref": str(collected.relative_to(PACKAGE)) + "/collection.json",
        "collection_sha256": collection_sha, "collection_launch": collection_launch,
        "workers": workers, "method_ids": ids, "source_credit": False, "independent": False,
        "deadline": deadline, "resources": resources(usage_start),
        "run_context": RECEIPTS.run_binding(), "dispatcher_invocation": dispatcher_invocation(),
        "selfcheck_launch": selfcheck_launch, "selfcheck_ref": selfcheck_ref,
        "selfcheck_sha256": selfcheck_sha}
    RECEIPTS.validate_focused(PACKAGE, document)
    write_new(PACKAGE / "fixtures/coordination/a049-focused.json", document)
    return document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--selfcheck-harness", action="store_true")
    parser.add_argument("--collect-readiness", action="store_true")
    parser.add_argument("--focused-checks", action="store_true")
    args = parser.parse_args()
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", args.label) is None:
        raise ValueError("invalid pair label")
    RECEIPTS.load_run_context(PACKAGE)
    if sum(bool(value) for value in (args.only, args.selfcheck_harness, args.collect_readiness, args.focused_checks)) > 1:
        raise ValueError("one explicit separately admitted dispatcher mode")
    RECEIPTS.validate_mode_admission("collect" if args.collect_readiness else "selfcheck" if args.selfcheck_harness else "affected" if args.only or args.focused_checks else "full-pair")
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (RECEIPTS.DISPATCHER_AS_LIMIT, RECEIPTS.DISPATCHER_AS_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (RECEIPTS.ASSIGNMENT_WALL_SECONDS, RECEIPTS.ASSIGNMENT_WALL_SECONDS))
    usage_start = list(resource.getrusage(resource.RUSAGE_SELF))
    main_started, main_clock = datetime.now(timezone.utc).isoformat(), time.monotonic()
    deadline = new_deadline()
    parent = PACKAGE / "fixtures/official-runs"
    report_path = parent / (args.label + ".execution.json")
    if report_path.exists():
        raise ValueError("retained launch collision: no reuse")
    if args.focused_checks:
        focused_checks(parent, args.label, deadline, usage_start)
        return 0
    if args.collect_readiness:
        if args.only or args.selfcheck_harness:
            raise ValueError("full collection readiness cannot be a subset/selfcheck")
        record = launch(parent, parent / (args.label + "-collect"), collect=True, deadline=deadline)
        write_new(report_path, {"schema": "friday.a049.collection-readiness.v1",
            "launch": record, "official_full_pair": False, "success": record["rc"] == 0,
            "started_at_utc": main_started, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.monotonic() - main_clock, "deadline": deadline,
            "resources": resources(usage_start), "dispatcher_invocation": dispatcher_invocation()})
        print(json.dumps({"report": str(report_path), "rc": record["rc"]}, sort_keys=True))
        return record["rc"]
    if args.selfcheck_harness:
        record = launch(parent, parent / (args.label + "-selfcheck"), harness=True, deadline=deadline)
        write_new(report_path, {"schema": "friday.a049.harness-execution.v1",
            "launch": record, "official_full_pair": False, "success": record["rc"] == 0,
            "started_at_utc": main_started, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": time.monotonic() - main_clock, "deadline": deadline,
            "resources": resources(usage_start), "dispatcher_invocation": dispatcher_invocation()})
        print(json.dumps({"report": str(report_path), "rc": record["rc"]}, sort_keys=True))
        return record["rc"]
    launches = []
    stopped_for_budget = None
    for number in (1, 2):
        if deadline:
            end = RECEIPTS.instant(deadline["assignment_deadline_utc"])
            remaining = (end - datetime.now(timezone.utc)).total_seconds()
            estimated = launches[0].get("elapsed_seconds") if launches else None
            if estimated is not None and remaining < estimated + deadline["seal_reserve_seconds"]:
                stopped_for_budget = {"reason": "NOT_RUN_FINITE_ASSIGNMENT_TIME_BUDGET",
                    "pass_number": number, "remaining_seconds": remaining,
                    "estimated_pass_seconds": estimated, "terminal_seal_reserve_seconds": deadline["seal_reserve_seconds"]}
                break
        run = parent / (args.label + "-" + str(number))
        if not args.only:
            record = full_pass(parent, run, deadline=deadline)
            launches.append(record)
            if record["rc"] != 0:
                break
            continue
        record = launch(parent, run, only=args.only, deadline=deadline)
        launches.append(record)
        if record["rc"] != 0:
            break
    report = {"schema": "friday.a049.test-pair-execution.v1", "launches": launches,
              "complete": len(launches) == 2, "success": len(launches) == 2 and all(x["rc"] == 0 for x in launches)}
    report.update(source_hashes=RECEIPTS.source_inventory(PACKAGE), deadline=deadline,
        resources=resources(usage_start), official_full_pair=not bool(args.only),
        started_at_utc=main_started, completed_at_utc=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=time.monotonic() - main_clock, dispatcher_invocation=dispatcher_invocation())
    report["budget_stop"] = stopped_for_budget
    if report["success"] and not args.only:
        RECEIPTS.validate_pair(PACKAGE, report, args.label)
    write_new(report_path, report)
    print(json.dumps({"report": str(report_path), "success": report["success"],
                      "return_codes": [x["rc"] for x in launches]}, sort_keys=True))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    _wall,_mono=time.time(),time.monotonic()
    RECEIPTS.install_root_observer("dispatcher",(_wall + 30,_mono + 30))
    RECEIPTS.load_run_context(PACKAGE)
    _context=RECEIPTS.run_context()
    _end=RECEIPTS.instant(_context["deadline_at_utc"]).timestamp()
    RECEIPTS.raw_client().ends=(_end,_mono + _end - _wall)
    RECEIPTS.instrument_root_module(globals(),"dispatcher")
    _native_exit=125
    try:
        _native_exit=RECEIPTS.observe_root_call("dispatcher.main",main)
    finally:
        RECEIPTS.observer_terminal(_native_exit)
    raise SystemExit(_native_exit)
