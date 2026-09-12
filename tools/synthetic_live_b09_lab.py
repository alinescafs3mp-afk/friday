"""Read actual Friday lab delivery records from a preregistered operator store.

The signing operator and its local lab store are trusted. A candidate, response,
or supplied ENQUEUED receipt is not. This reader creates no jobs or model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any

PROTOCOL = "friday-lab/2"
SCHEMA = "friday.synthetic-live-b09-lab-capture.v1"


class LabProvenanceError(ValueError):
    pass


def _require(condition: Any) -> None:
    if not condition:
        raise LabProvenanceError("lab_review_provenance_invalid")


def _id(value: Any, prefix: str) -> bool:
    return isinstance(value, str) and re.fullmatch(prefix + r"_[0-9a-f]{32}", value) is not None


def _directory(path: Path) -> os.stat_result:
    metadata = path.lstat()
    _require(path.is_absolute() and path.resolve() == path)
    _require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid())
    _require(stat.S_IMODE(metadata.st_mode) == 0o700)
    return metadata


def read_private(path: Path) -> tuple[dict[str, Any], str]:
    """Read and hash the same bounded, private regular-file bytes."""
    _require(path.is_absolute() and path.resolve() == path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid())
        _require(before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o600)
        _require(0 < before.st_size <= 2 * 1024 * 1024)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        after = os.fstat(descriptor)
        current = path.lstat()

        def identity(metadata: os.stat_result) -> tuple[int, ...]:
            return tuple(
                getattr(metadata, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_mode",
                    "st_uid",
                    "st_gid",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            )

        _require(identity(before) == identity(after) == identity(current) and len(raw) == before.st_size)
        value = json.loads(raw)
        _require(isinstance(value, dict))
        return value, hashlib.sha256(raw).hexdigest()
    finally:
        os.close(descriptor)


def transport_is_valid(value: Any) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value)
        == {"protocol", "root", "device", "inode", "job_id", "instance_id", "session_id", "epoch"}
        and value["protocol"] == PROTOCOL
        and isinstance(value["root"], str)
        and Path(value["root"]).is_absolute()
        and all(type(value[k]) is int and value[k] >= 0 for k in ("device", "inode", "epoch"))
        and _id(value["job_id"], "job")
        and _id(value["instance_id"], "inst")
        and isinstance(value["session_id"], str)
        and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value["session_id"])
    )


def preregister(root: Path, job_id: str) -> dict[str, Any]:
    """Anchor the operator-selected existing store before any run or review."""
    metadata = _directory(root)
    _require(_id(job_id, "job"))
    _require(not (root / "jobs" / f"{job_id}.json").exists())
    control, _ = read_private(root / "control.json")
    attachment = control.get("attachment", {})
    value = {
        "protocol": PROTOCOL,
        "root": str(root),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "job_id": job_id,
        "instance_id": attachment.get("instance_id"),
        "session_id": attachment.get("session_id"),
        "epoch": attachment.get("epoch"),
    }
    _require(transport_is_valid(value))
    return value


def _time(value: Any) -> datetime:
    _require(isinstance(value, str))
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None)
    return parsed


def capture(
    *,
    plan: dict[str, Any],
    task_path: Path,
    task_sha256: str,
    result_path: Path,
    result_sha256: str,
    issued_at: str,
) -> dict[str, Any]:
    """Capture a delivered RUN and completed RESULT; never consume claims JSON."""
    transport = plan["lab_transport"]
    _require(transport_is_valid(transport))
    root = Path(transport["root"])
    metadata = _directory(root)
    _require((metadata.st_dev, metadata.st_ino) == (transport["device"], transport["inode"]))
    control, _ = read_private(root / "control.json")
    _require(
        all(
            control.get("attachment", {}).get(k) == transport[k]
            for k in ("instance_id", "session_id", "epoch")
        )
    )
    reviewer = plan["reviewer"]
    _require(reviewer["reviewer_thread"] == transport["session_id"])
    _require(reviewer["reviewer_generation"] == transport["instance_id"])
    job_id = transport["job_id"]
    observed: dict[str, str] = {}

    def read(relative: str) -> dict[str, Any]:
        value, digest = read_private(root / relative)
        observed[relative] = digest
        return value

    job = read(f"jobs/{job_id}.json")
    _require(job.get("protocol") == PROTOCOL and job.get("job_id") == job_id)
    _require(job.get("state") == "DONE" and job.get("from") == "astra")
    _require(job.get("kind") == "source-review" and job.get("identity") == "owner")
    _require(type(job.get("task_revision")) is int and job["task_revision"] == 1)
    _require(all(job.get(k) is False for k in ("cancel_requested", "expired", "effect_unknown")))
    _require(job.get("waiting_for") is None)
    run_id, result_id = job.get("run_message_id"), job.get("result_message_id")
    _require(_id(run_id, "msg") and _id(result_id, "msg") and run_id != result_id)
    run = read(f"messages/{run_id}.json")
    result = read(f"messages/{result_id}.json")
    for envelope, message_id, kind, sender, recipient in (
        (run, run_id, "RUN", "astra", "grok-lab"),
        (result, result_id, "RESULT", "grok-lab", "astra"),
    ):
        _require(
            all(
                envelope.get(k) == v
                for k, v in {
                    "protocol": PROTOCOL,
                    "message_id": message_id,
                    "job_id": job_id,
                    "type": kind,
                    "from": sender,
                    "to": recipient,
                }.items()
            )
        )
        _require(isinstance(envelope.get("payload"), dict))
        _require(envelope.get("auth", {}).get("euid") == os.getuid())
    _require(run["payload"] == job.get("payload"))
    _require(run["payload"].get("job_id") == job_id)
    _require(run["payload"].get("execution_mode") == "ATTACHED_TUI_MONITOR")
    _require(run["payload"].get("goal") == reviewer["assignment_id"])
    submit = read(f"artifacts/{job_id}/submit_receipt.json")
    _require(submit.get("protocol") == PROTOCOL and submit.get("job_id") == job_id)
    _require(submit.get("message_id") == run_id)
    _require(submit.get("receipt_sha256") == observed[f"messages/{run_id}.json"])
    claim = job.get("claim", {})
    delivery_id = claim.get("delivery_id")
    _require(_id(delivery_id, "del"))
    delivery = read(f"deliveries/{delivery_id}.json")
    _require(delivery.get("protocol") == PROTOCOL and delivery.get("delivery_id") == delivery_id)
    _require(delivery.get("message_id") == run_id and delivery.get("job_id") == job_id)
    _require(delivery.get("kind") == "RUN" and delivery.get("epoch") == transport["epoch"])
    for record in (claim, delivery):
        _require(all(record.get(k) == transport[k] for k in ("instance_id", "session_id")))
        _require(record.get("executor_ack") is True)
    _require(claim.get("source") == "tui-monitor" and claim.get("waiting_current") is False)
    _require(delivery.get("claim_source") == "tui-monitor" and delivery.get("claimed") is True)
    _require(delivery.get("stdout_confirmed") is True and delivery.get("submit_admitted") is True)
    _require(delivery.get("needs_executor_ack") is False)
    _require(claim.get("executor_ack_at") == delivery.get("executor_ack_at"))
    # Historical waiting_current on a promoted delivery is retained by lab/2.
    # The current job claim and the actual execution ACK establish promotion.
    created, ack, finished = (
        _time(run.get("created_at")),
        _time(claim.get("executor_ack_at")),
        _time(result.get("created_at")),
    )
    _require(
        _time(plan["issued_at"])
        <= created
        <= _time(claim.get("claimed_at"))
        <= ack
        <= finished
        <= _time(issued_at)
    )
    _require(created == _time(job.get("created_at")))
    _require(created <= _time(delivery.get("created_at")) <= ack)
    _require(finished <= _time(job.get("deadline_at")))
    _require(finished <= _time(job.get("updated_at")) <= _time(issued_at))
    # ACKs are messages without a job index in lab/2. Inspect the local store
    # once; this is an issuance check, not a monitor or model polling loop.
    message_paths = list((root / "messages").glob("*.json"))
    _require(len(message_paths) <= 20_000)
    run_count = result_count = 0
    acks: list[dict[str, Any]] = []
    completions: list[dict[str, Any]] = []
    for path in message_paths:
        envelope, digest = read_private(path)
        same_job = envelope.get("job_id") == job_id
        if same_job and envelope.get("type") == "RUN":
            run_count += 1
        if same_job and envelope.get("type") == "RESULT":
            result_count += 1
        if same_job and envelope.get("type") in {"QUESTION", "ANSWER", "CANCEL", "PAUSE"}:
            raise LabProvenanceError("lab_review_provenance_invalid")
        if envelope.get("from") != "grok-lab":
            continue
        if envelope.get("type") == "ACK" and envelope.get("in_reply_to") == run_id:
            acks.append(envelope)
        elif same_job and envelope.get("type") == "ACK_RESULT":
            _require(envelope.get("in_reply_to") == result_id)
            completions.append(envelope)
        else:
            continue
        _require(envelope.get("protocol") == PROTOCOL and envelope.get("to") == "grok-lab")
        _require(envelope.get("auth", {}).get("euid") == os.getuid())
        _require(envelope.get("message_id") == path.stem)
        observed[str(path.relative_to(root))] = digest
    _require(run_count == result_count == 1 and len(acks) == 1 and len(completions) >= 1)
    _require(ack <= _time(acks[0].get("created_at")) <= finished)
    for completion in completions:
        _require(
            finished
            <= _time(completion.get("created_at"))
            <= min(_time(issued_at), _time(job.get("deadline_at")))
        )
    plan_sha = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    binding = {
        "assignment_id": reviewer["assignment_id"],
        "assignment_generation": reviewer["assignment_generation"],
        "plan_sha256": plan_sha,
        "task_event_id": reviewer["task_event_id"],
        "result_event_id": reviewer["result_event_id"],
        "task_path": str(task_path),
        "task_sha256": task_sha256,
    }
    _require(run["payload"].get("content_review") == binding)
    artifact = read(f"artifacts/{job_id}/result.json")
    _require(artifact == result["payload"] and artifact.get("job_id") == job_id)
    expected_result = {**binding, "result_path": str(result_path), "result_sha256": result_sha256}
    _require(artifact.get("content_review") == expected_result)
    # Detect cross-file races before root signs the captured hashes.
    for relative, digest in observed.items():
        _require(read_private(root / relative)[1] == digest)
    final_control, _ = read_private(root / "control.json")
    _require(
        all(
            final_control.get("attachment", {}).get(k) == transport[k]
            for k in ("instance_id", "session_id", "epoch")
        )
    )
    after = _directory(root)
    _require((after.st_dev, after.st_ino) == (metadata.st_dev, metadata.st_ino))
    return {
        "schema": SCHEMA,
        "transport": transport,
        "run_message_id": run_id,
        "result_message_id": result_id,
        "delivery_id": delivery_id,
        "files": observed,
        "task_sha256": task_sha256,
        "result_sha256": result_sha256,
        "captured_at": issued_at,
    }
