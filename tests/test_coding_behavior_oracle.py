from friday.organs.coding.behavior_oracle import (
    CSV_SUMMARY_CASES,
    ORACLE_ID,
    CodingBehaviorOracleState,
    admit_coding_behavior_oracle,
    admit_coding_behavior_oracle_id,
    oracle_sha256,
    oracle_worker_program,
)


def test_csv_summary_request_is_admitted() -> None:
    result = admit_coding_behavior_oracle(
        "создай python cli который читает csv из stdin и печатает сводку rows и sum колонки amount"
    )
    assert result.state is CodingBehaviorOracleState.ADMITTED
    assert result.oracle_id == ORACLE_ID
    assert result.cases == CSV_SUMMARY_CASES
    assert "main.py" in result.cli_contract
    assert "10" not in result.cli_contract and "alice" not in result.cli_contract


def test_add_function_request_is_not_admitted() -> None:
    result = admit_coding_behavior_oracle("создай python проект с функцией add(a, b), возвращающей сумму")
    assert result.state is CodingBehaviorOracleState.EMPTY
    assert result.cases == ()


def test_oracle_identity_is_stable() -> None:
    first = admit_coding_behavior_oracle("create a python csv summary cli")
    second = admit_coding_behavior_oracle("Generate CSV summary tool")
    assert first.oracle_sha256 == second.oracle_sha256 == oracle_sha256(ORACLE_ID, CSV_SUMMARY_CASES)
    assert len(first.oracle_sha256 or "") == 64
    program = oracle_worker_program()
    assert "two_rows" in program and "alice" in program
    assert "import docker" not in program


def test_persisted_oracle_id_readmits_the_frozen_family() -> None:
    admitted = admit_coding_behavior_oracle_id(ORACLE_ID)
    assert admitted.state is CodingBehaviorOracleState.ADMITTED
    assert admitted.oracle_sha256 == oracle_sha256(ORACLE_ID, CSV_SUMMARY_CASES)
    assert admit_coding_behavior_oracle_id("latest").state is CodingBehaviorOracleState.EMPTY
    assert admit_coding_behavior_oracle_id(None).state is CodingBehaviorOracleState.EMPTY
