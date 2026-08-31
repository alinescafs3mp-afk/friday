from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tools import release_artifact_retention as retention
from tools import release_artifact_retention_operator as operator


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")


def _epoch() -> dict[str, Any]:
    return {
        "activation_receipt_file_sha256": "1" * 64,
        "activation_receipt_sha256": "2" * 64,
        "current_candidate_sha256": "3" * 64,
        "current_generation_id": "4" * 64,
        "current_generation_receipt_sha256": "5" * 64,
        "index_journal_sha256": "6" * 64,
        "index_revision": 9,
        "older_candidate_sha256": "7" * 64,
        "older_generation_id": "8" * 64,
        "older_generation_receipt_sha256": "9" * 64,
        "retention_scope_schema": retention.RETENTION_SCOPE_SCHEMA,
        "retention_scope_sha256": "a" * 64,
        "topology": "two_v2",
    }


def _receipt(context: dict[str, Any], *, count: int, terminal: bool = False) -> dict[str, Any]:
    return {
        "admission_reason": "fresh_eligible_zero" if terminal else "effectful_applied",
        "admission_status": "release_admissible" if terminal else "nonterminal",
        "batch_ordinal": context["batch_ordinal"],
        "deleted_candidate_count": count,
        "receipt_sha256": hashlib.sha256(
            _canonical([context["cycle_sha256"], context["batch_ordinal"], count, terminal])
        ).hexdigest(),
    }


def _install_cycle_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    topology: str = "two_v2",
) -> tuple[Path, dict[str, Any], Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    activation = state_dir / "immutable-release-activation.v1.json"
    root_path = tmp_path / "reviewed.json"
    root_path.write_text("{}\n", encoding="ascii")
    root_sha = "b" * 64
    root = {"plan_sha256": root_sha, "kind": "root"}
    epoch = {**_epoch(), "topology": topology}
    monkeypatch.setattr(operator, "_read_reviewed_plan", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(
        operator,
        "_plan_inputs",
        lambda _plan: {"activation_journal": activation},
    )
    monkeypatch.setattr(
        operator,
        "_current_activation_receipt_path",
        lambda _state: tmp_path / "activation-receipt.json",
    )
    monkeypatch.setattr(operator, "_retention_epoch", lambda **_kwargs: (epoch, "c" * 64))
    monkeypatch.setattr(operator, "_reviewed_candidate_set_sha256", lambda _plan: "d" * 64)
    monkeypatch.setattr(operator, "_load_journal", lambda _path: None)
    monkeypatch.setattr(operator, "_load_accepted_root_plan", lambda *_args, **_kwargs: root)
    reviewed_identity = {"entry_count": 1, "path": "/reviewed"}
    monkeypatch.setattr(
        operator,
        "_reviewed_candidate_identities",
        lambda _plan: (reviewed_identity,),
    )
    monkeypatch.setattr(
        operator,
        "_applied_cycle_identities_locked",
        lambda *_args, **_kwargs: {hashlib.sha256(_canonical(reviewed_identity)).hexdigest()},
    )
    return root_path, root, state_dir


def test_seventeen_candidates_converge_as_sixteen_then_one_then_fresh_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path, _root, _state_dir = _install_cycle_fakes(monkeypatch, tmp_path)
    one = {"kind": "one", "plan_sha256": "e" * 64}
    zero = {"kind": "zero", "plan_sha256": "f" * 64}
    fresh = iter((one, zero))
    monkeypatch.setattr(operator, "_build_reviewed_subset_plan", lambda **_kwargs: next(fresh))
    monkeypatch.setattr(
        operator,
        "_candidate_records",
        lambda plan: tuple(
            {"path": f"/{index}"} for index in range({"root": 16, "one": 1}.get(plan["kind"], 0))
        ),
    )
    monkeypatch.setattr(
        operator,
        "_is_exact_terminal_zero_plan",
        lambda plan, _candidates: plan["kind"] == "zero",
    )
    monkeypatch.setattr(
        operator, "_stage_generated_plan", lambda _state, plan: tmp_path / f"{plan['kind']}.json"
    )
    calls: list[tuple[Path | None, dict[str, Any]]] = []

    def apply(**kwargs: Any) -> dict[str, Any]:
        context = dict(kwargs["_cycle_context"])
        calls.append((kwargs["plan_path"], context))
        count = (16, 1, 0)[len(calls) - 1]
        return _receipt(context, count=count, terminal=count == 0)

    monkeypatch.setattr(operator, "apply_retention_plan", apply)
    monkeypatch.setattr(
        operator,
        "_converged_receipt_for_state",
        lambda **_kwargs: {"schema": operator.CONVERGENCE_RECEIPT_SCHEMA, "status": "converged"},
    )

    result = operator.converge_retention_cycle(
        reviewed_plan_path=root_path,
        expected_reviewed_plan_sha256="b" * 64,
    )

    assert result["status"] == "converged"
    assert [context["batch_ordinal"] for _path, context in calls] == [0, 1, 2]
    assert calls[1][1]["previous_receipt_sha256"] == _receipt(calls[0][1], count=16)["receipt_sha256"]
    assert calls[2][1]["previous_receipt_sha256"] == _receipt(calls[1][1], count=1)["receipt_sha256"]
    assert len({context["cycle_sha256"] for _path, context in calls}) == 1


def test_restart_resumes_unfinished_batch_before_building_terminal_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path, root, state_dir = _install_cycle_fakes(monkeypatch, tmp_path)
    initial = operator._new_cycle_context(  # noqa: SLF001
        accepted_root_plan_path=root_path,
        accepted_root_plan_sha256="b" * 64,
        reviewed_full_candidate_set_sha256="d" * 64,
        retention_epoch_sha256="c" * 64,
        batch_ordinal=3,
        previous_receipt_sha256="1" * 64,
    )
    journal = {
        **initial,
        "phase": "applying",
        "plan_sha256": "2" * 64,
        "transaction_id": "3" * 64,
    }
    monkeypatch.setattr(operator, "_load_journal", lambda _path: journal)
    zero = {"kind": "zero", "plan_sha256": "f" * 64}
    monkeypatch.setattr(operator, "_build_reviewed_subset_plan", lambda **_kwargs: zero)
    monkeypatch.setattr(operator, "_candidate_records", lambda plan: () if plan is zero else ({},))
    monkeypatch.setattr(operator, "_is_exact_terminal_zero_plan", lambda plan, _items: plan is zero)
    monkeypatch.setattr(operator, "_stage_generated_plan", lambda *_args: tmp_path / "zero.json")
    calls: list[dict[str, Any]] = []

    def apply(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        context = dict(kwargs["_cycle_context"])
        return _receipt(context, count=1 if len(calls) == 1 else 0, terminal=len(calls) == 2)

    monkeypatch.setattr(operator, "apply_retention_plan", apply)
    monkeypatch.setattr(
        operator,
        "_converged_receipt_for_state",
        lambda **_kwargs: {"schema": operator.CONVERGENCE_RECEIPT_SCHEMA, "status": "converged"},
    )

    result = operator.converge_retention_cycle(
        reviewed_plan_path=root_path,
        expected_reviewed_plan_sha256="b" * 64,
        state_dir=state_dir,
    )

    assert result["status"] == "converged"
    assert calls[0]["plan_path"] is None
    assert calls[0]["state_dir"] == state_dir
    assert calls[0]["_cycle_context"] == initial
    assert calls[1]["_cycle_context"]["batch_ordinal"] == 4
    assert root["kind"] == "root"


def test_effectful_and_deferred_zero_receipts_are_never_release_admissible() -> None:
    plan = {
        "apply_authority": False,
        "open_inventory": {"source": "code_owned_no_delete_candidates_v1"},
        "targets": [],
        "backup_targets": [],
    }
    assert operator._is_exact_terminal_zero_plan(plan, ()) is True  # noqa: SLF001
    plan["targets"] = [{"reason": "deferred_batch_bound"}]
    assert operator._is_exact_terminal_zero_plan(plan, ()) is False  # noqa: SLF001
    plan["targets"] = [{"reason": "open_reference"}]
    assert operator._is_exact_terminal_zero_plan(plan, ()) is False  # noqa: SLF001
    plan["targets"] = []
    plan["apply_authority"] = True
    plan["open_inventory"] = {"source": "code_owned_privileged_target_proc_v1"}
    assert operator._is_exact_terminal_zero_plan(plan, ()) is False  # noqa: SLF001


def test_first_v2_defers_before_any_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path, _root, _state_dir = _install_cycle_fakes(
        monkeypatch,
        tmp_path,
        topology="first_v2",
    )
    monkeypatch.setattr(
        operator,
        "apply_retention_plan",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("first v2 must not apply")),
    )

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_convergence_first_v2_deferred$",
    ):
        operator.converge_retention_cycle(
            reviewed_plan_path=root_path,
            expected_reviewed_plan_sha256="b" * 64,
        )
    admission = operator._convergence_receipt(  # noqa: SLF001
        epoch={
            **_epoch(),
            "retention_scope_schema": "",
            "retention_scope_sha256": "",
            "topology": "first_v2",
        },
        status="first_v2_deferred",
    )
    assert admission["status"] == "first_v2_deferred"


@pytest.mark.parametrize(
    ("epoch_changes", "status"),
    (
        ({"topology": "pre_v2"}, "review_required"),
        ({"topology": "two_v2"}, "first_v2_deferred"),
        ({"topology": "first_v2"}, "review_required"),
        (
            {"retention_scope_schema": "", "retention_scope_sha256": ""},
            "review_required",
        ),
        (
            {
                "retention_scope_schema": retention.RETENTION_SCOPE_SCHEMA,
                "retention_scope_sha256": "",
                "topology": "first_v2",
            },
            "first_v2_deferred",
        ),
        ({"index_revision": True}, "review_required"),
        ({"unexpected": "field"}, "review_required"),
    ),
)
def test_convergence_receipt_rejects_mismatched_or_open_epoch_projection(
    epoch_changes: dict[str, Any],
    status: str,
) -> None:
    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_admission_status_invalid$",
    ):
        operator._convergence_receipt(  # noqa: SLF001
            epoch={**_epoch(), **epoch_changes},
            status=status,
        )


def test_locked_release_admission_uses_explicit_home_without_reacquiring_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "data/state"
    state.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(operator, "_validate_friday_home", lambda supplied: supplied)
    monkeypatch.setattr(retention, "_strict_private_directory", lambda path, **_kwargs: path)
    monkeypatch.setattr(
        operator,
        "_retention_epoch_locked",
        lambda **_kwargs: ({**_epoch(), "topology": "first_v2"}, "f" * 64),
    )
    monkeypatch.setattr(
        operator.release_operator,
        "OperatorTransactionLock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("nested lock")),
    )
    guards = 0

    def guard() -> None:
        nonlocal guards
        guards += 1

    receipt = operator._retention_release_admission_locked(  # noqa: SLF001
        activation_receipt=tmp_path / "activation.json",
        friday_home=home,
        namespace_guard=guard,
    )

    assert receipt["status"] == "first_v2_deferred"
    assert guards == 2


def test_locked_release_admission_rejects_pre_v2_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "data/state"
    state.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(operator, "_validate_friday_home", lambda supplied: supplied)
    monkeypatch.setattr(retention, "_strict_private_directory", lambda path, **_kwargs: path)
    monkeypatch.setattr(
        operator,
        "_retention_epoch_locked",
        lambda **_kwargs: ({**_epoch(), "topology": "pre_v2"}, "f" * 64),
    )
    monkeypatch.setattr(
        operator,
        "_validated_terminal_chain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pre-v2 must not inspect convergence state")
        ),
    )

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_admission_dr_topology_invalid$",
    ):
        operator._retention_release_admission_locked(  # noqa: SLF001
            activation_receipt=tmp_path / "activation.json",
            friday_home=home,
            namespace_guard=lambda: None,
        )


@pytest.mark.parametrize("topology", ("first_v2", "two_v2"))
def test_locked_release_admission_rechecks_namespace_after_constructing_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    topology: str,
) -> None:
    home = tmp_path / "home"
    state = home / "data/state"
    state.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(operator, "_validate_friday_home", lambda supplied: supplied)
    monkeypatch.setattr(retention, "_strict_private_directory", lambda path, **_kwargs: path)
    monkeypatch.setattr(
        operator,
        "_retention_epoch_locked",
        lambda **_kwargs: ({**_epoch(), "topology": topology}, "f" * 64),
    )
    monkeypatch.setattr(operator, "_validated_terminal_chain", lambda *_args, **_kwargs: None)
    receipt_constructed = False

    def convergence_receipt(**_kwargs: Any) -> dict[str, Any]:
        nonlocal receipt_constructed
        receipt_constructed = True
        return {"status": "constructed"}

    def guard() -> None:
        if receipt_constructed:
            raise operator.RetentionApplyError("operator_transaction_lock_changed")

    monkeypatch.setattr(operator, "_convergence_receipt", convergence_receipt)

    with pytest.raises(
        operator.RetentionApplyError,
        match="^operator_transaction_lock_changed$",
    ):
        operator._retention_release_admission_locked(  # noqa: SLF001
            activation_receipt=tmp_path / "activation.json",
            friday_home=home,
            namespace_guard=guard,
        )


def test_converged_receipt_rechecks_namespace_after_constructing_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_constructed = False

    class DisplacedTransaction:
        def __enter__(self) -> DisplacedTransaction:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def assert_held(self) -> None:
            if receipt_constructed:
                raise operator.RetentionApplyError("operator_transaction_lock_changed")

    monkeypatch.setattr(
        operator.release_operator,
        "OperatorTransactionLock",
        lambda _path: DisplacedTransaction(),
    )
    monkeypatch.setattr(
        operator,
        "_retention_epoch_locked",
        lambda **_kwargs: (_epoch(), "f" * 64),
    )
    monkeypatch.setattr(
        operator,
        "_validated_terminal_chain",
        lambda *_args, **_kwargs: {"terminal": True},
    )

    def convergence_receipt(**_kwargs: Any) -> dict[str, Any]:
        nonlocal receipt_constructed
        receipt_constructed = True
        return {"status": "converged"}

    monkeypatch.setattr(operator, "_convergence_receipt", convergence_receipt)

    with pytest.raises(
        operator.RetentionApplyError,
        match="^operator_transaction_lock_changed$",
    ):
        operator._converged_receipt_for_state(  # noqa: SLF001
            state_dir=tmp_path,
            activation_receipt=tmp_path / "activation.json",
        )


def test_open_only_root_never_stages_a_later_false_terminal_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path, _root, _state_dir = _install_cycle_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(operator, "_has_open_only_identity", lambda _plan: True)
    monkeypatch.setattr(
        operator,
        "_stage_generated_plan",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexecutable review must not stage")),
    )
    monkeypatch.setattr(
        operator,
        "apply_retention_plan",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexecutable review must not apply")),
    )

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_convergence_review_unexecutable$",
    ):
        operator.converge_retention_cycle(
            reviewed_plan_path=root_path,
            expected_reviewed_plan_sha256="b" * 64,
        )


def test_oversized_deferred_root_is_rejected_before_any_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path, _root, _state_dir = _install_cycle_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        operator,
        "_reviewed_candidate_identities",
        lambda _plan: (
            {
                "entry_count": operator.proc_probe.MAX_TARGET_OBJECTS + 1,
                "path": "/oversized-deferred",
            },
        ),
    )
    monkeypatch.setattr(
        operator,
        "apply_retention_plan",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("oversized review must not apply")),
    )

    with pytest.raises(
        operator.RetentionApplyError,
        match="^retention_convergence_review_unexecutable$",
    ):
        operator.converge_retention_cycle(
            reviewed_plan_path=root_path,
            expected_reviewed_plan_sha256="b" * 64,
        )
