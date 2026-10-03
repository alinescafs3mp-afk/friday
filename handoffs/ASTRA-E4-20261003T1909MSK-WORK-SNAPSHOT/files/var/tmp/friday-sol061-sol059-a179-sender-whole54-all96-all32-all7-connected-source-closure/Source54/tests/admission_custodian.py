"""Read-only nonroot parent receiver for separately authentic native RUN tasks.

Only the independent parent controller calls receive_and_launch. There is no CLI,
issuer writer, receipt refresh, sealed-object creator or source admission fallback.
The controller must authenticate the TASK/pin through its existing native channel
before this call. A source-produced record is never a TASK. This source package
has no current TASK, issuer, transport or RUN: all remain NOT_ISSUED.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess

PACKAGE = Path(__file__).resolve().parents[1]
R=globals().get("_BOUND_RECEIPTS")
if R is None:
    raise PermissionError("receiver requires its already owned same-process controller receipts")
ENTRYPOINTS = {"dispatcher": "tests/execute_pair.py",
    "reports": "fixtures/coordination/finalize_reports.py", "terminal": "tests/close_package.py"}


def validate_independent_parent_task(task, package):
    """Strict TASK data, not a local issuer or an authentication substitute."""
    R.require(type(task) is dict and set(task) == {"schema", "issuer", "source_assignment",
        "package_root", "source_hashes", "run_context", "expectation_sha256",
        "expectation_identity", "held_admission_identity", "entrypoints"}
        and task["schema"] == "friday.a056.independent-parent-run-task.v1"
        and task["issuer"] == R.ADMISSION_ISSUER
        and task["source_assignment"] == R.SOURCE_ASSIGNMENT
        and task["package_root"] == str(package), "exact independently admitted parent TASK")
    R.require(task["source_hashes"] == R.source_inventory(package),
        "parent TASK exact entire final source inventory")
    for key, maximum in (("expectation_identity", 131072), ("held_admission_identity", 65536)):
        value = task[key]
        R.require(type(value) is list and len(value) == 9 and all(R.is_int(v) for v in value)
            and value[3:6] == [*R.ADMISSION_OWNER, 0]
            and 0 < value[6] <= maximum, "parent TASK exact externally admitted held identity")
    R.require(type(task["expectation_sha256"]) is str
        and len(task["expectation_sha256"]) == 64
        and all(c in "0123456789abcdef" for c in task["expectation_sha256"]),
        "separately externally pinned whole parent expectation")
    steps = task["entrypoints"]
    R.require(type(steps) is list and steps and len(steps) <= 4,
        "finite independently admitted parent entrypoint sequence")
    for step in steps:
        R.require(type(step) is dict and set(step) == {"entrypoint", "arguments"}
            and step["entrypoint"] in ENTRYPOINTS and type(step["arguments"]) is list
            and all(type(arg) is str and len(arg) <= 1024 for arg in step["arguments"]),
            "fixed executable and exact independently admitted arguments")
        R.require(not any(arg.startswith(("--run-admission", "--expected-run-admission",
            "--assignment-deadline", "--seal-reserve")) for arg in step["arguments"]),
            "parent/child cannot select authority path/pin/deadline/reserve by CLI")
    return task


def receive_and_launch(parent_task_raw, expected_parent_task_sha256,
        held_expectation_fd, held_admission_fd):
    """Actual future caller path, invoked only by the existing independent parent.

    parent_task_raw and its whole-byte expected pin come from the parent's already
    authenticated immutable native TASK, independently of SOURCE/worker inputs.
    Both held objects already exist and are independently admitted by that TASK.
    Their origin is an external precondition, not inferred from owner UID or seal.
    This receiver authenticates them before any dispatcher/generated execution.
    """
    R.raw_client().note("receiver.receive.begin",[],{"parent_task_sha256":expected_parent_task_sha256,
        "held_fds":[held_expectation_fd,held_admission_fd]})
    R.instrument_root_module(globals(),"receiver")
    R.require((os.geteuid(), os.getegid()) == R.ADMISSION_OWNER,
        "declared ordinary nonroot independent parent owner")
    R.require(type(parent_task_raw) is bytes and 0 < len(parent_task_raw) <= 131072
        and type(expected_parent_task_sha256) is str
        and hashlib.sha256(parent_task_raw).hexdigest() == expected_parent_task_sha256,
        "externally authenticated parent TASK whole-byte pin; no CLI selfapproval")
    task = validate_independent_parent_task(R.unique_object(parent_task_raw), PACKAGE)
    expectation_raw, expectation_identity = R.read_sealed_custody(held_expectation_fd, 131072)
    admission_raw, admission_identity = R.read_sealed_custody(held_admission_fd, 65536)
    R.require(hashlib.sha256(expectation_raw).hexdigest() == task["expectation_sha256"]
        and expectation_identity == task["expectation_identity"]
        and admission_identity == task["held_admission_identity"],
        "independently expected parent/issuer transport pin and identities")
    expected = R.validate_parent_expectation(R.unique_object(expectation_raw))
    document = R.unique_object(admission_raw)
    context = R.validate_context_document(document, task["source_hashes"])
    R.require(context == task["run_context"] == expected["run_context"]
        and expected["source_hashes"] == task["source_hashes"]
        and hashlib.sha256(admission_raw).hexdigest() == expected["admission_sha256"],
        "independent TASK/expectation/issuer exact source/context/body agreement")
    # Duplicate held read-only objects only: no new object, authority or namespace.
    # Move temporary duplicates away from fixed descriptors before replacing them.
    temporary = [None, None]
    fixed_owned = [False, False]
    active_error = None
    close_errors = []
    owner={"temporary":temporary,"fixed_owned":fixed_owned,"first_error":None,
        "close_errors":close_errors,"pending_close_error":None,"close_attempted":set()}
    R.RECEIVER_PENDING=owner
    R.RECEIVER_OWNERS.append(owner)
    try:
        for index, fd in enumerate((held_expectation_fd, held_admission_fd)):
            temporary[index] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 200)
        for index, (source, target) in enumerate(zip(temporary, R.PARENT_CUSTODY_FDS)):
            os.dup2(source, target, inheritable=False)
            fixed_owned[index] = True
        R.load_run_context(PACKAGE)  # real named/opened/post identity/custody check
        R.require(R.full_source_inventory_generation(PACKAGE,task["source_hashes"],
            (task["run_context"]["run_id"],"receiver admitted sequence")) == task["source_hashes"],
            "actual fresh whole Source54 before the admitted sequence")
        completed = []
        for step in task["entrypoints"]:
            R.require(R.source_inventory(PACKAGE) == task["source_hashes"],
                "final source unchanged before each actual parent entrypoint")
            R.load_run_context(PACKAGE)  # same immutable admission, no refresh
            remaining = (R.instant(context["deadline_at_utc"]) - datetime.now(timezone.utc)).total_seconds()
            timeout = remaining - context["seal_reserve_seconds"] - 1
            R.require(timeout > 0, "parent launch within immutable wall/reserve")
            argv = ["/usr/bin/python3.14", "-I", "-S", "-B",
                str(PACKAGE / ENTRYPOINTS[step["entrypoint"]]), *step["arguments"]]
            env = {"HOME": str(PACKAGE), "TMPDIR": str(PACKAGE / "fixtures"),
                "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1"}
            child = subprocess.run(argv, env=env, cwd=PACKAGE, stdin=subprocess.DEVNULL,
                pass_fds=R.observed_fds(R.PARENT_CUSTODY_FDS), close_fds=True, timeout=timeout, check=False)
            completed.append({"entrypoint": step["entrypoint"], "argv": argv,
                "returncode": child.returncode, "run_context": R.run_binding(),
                "independent_parent_task_sha256": expected_parent_task_sha256})
            if child.returncode != 0:
                break
        R.require(R.source_inventory(PACKAGE) == task["source_hashes"], "post parent source integrity")
        R.raw_client().note("receiver.receive.completed",[],completed)
        return completed
    except BaseException as error:
        active_error = error
        owner["first_error"]=error
        R.raw_client().note("receiver.receive.error", [], error=error)
        raise
    finally:
        # Attempt every actually acquired descriptor, even after a close error.
        # Never close a fixed target whose dup2 did not return successfully.
        for fd in temporary + [fd if fixed_owned[index] else None
                for index, fd in enumerate(R.PARENT_CUSTODY_FDS)]:
            if fd is None:
                continue
            if fd in owner["close_attempted"]:continue
            owner["close_attempted"].add(fd)
            try:
                os.close(fd)
                R.raw_client().note("receiver.final_close.returned", [fd], None)
            except BaseException as error:
                owner["pending_close_error"]=error
                try:close_errors.append(error)
                except BaseException as recorder:owner["cleanup_recording_error"]=recorder
                R.raw_client().note("receiver.final_close.error", [fd], error=error)
        if close_errors and active_error is None:
            raise close_errors[0]


if __name__ == "__main__":
    raise PermissionError("parent-controller API only; source/CLI cannot issue or approve a RUN")
