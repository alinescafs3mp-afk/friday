from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


class _SearchHarness:
    def __init__(self, result):  # noqa: ANN001
        self.result = result

    async def search(self, _user_id: str, _query: str, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        del args, kwargs
        return self.result


def test_retrieval_probe_rejects_foreign_and_unowned_result_ids() -> None:
    searcher = _SearchHarness({"results": [{"id": "ko_foreign"}], "count": 1, "graph_context": {}})
    probe = battery.RetrievalPrivacyProbe(searcher, ())
    probe.configure_ownership(main_ids=["ko_main"], foreign_ids=["ko_foreign"], expected_user="main-user")
    probe.install()
    try:
        asyncio.run(searcher.search("main-user", "synthetic"))
    finally:
        probe.restore()
    assert probe.foreign_id_result_calls == 1
    assert probe.unowned_id_result_calls == 1
    assert probe.unexpected_user_calls == 0


def test_retrieval_probe_accepts_only_main_owned_result_ids() -> None:
    searcher = _SearchHarness({"results": [{"id": "ko_main"}], "count": 1, "graph_context": {}})
    probe = battery.RetrievalPrivacyProbe(searcher, ())
    probe.configure_ownership(main_ids=["ko_main"], foreign_ids=["ko_foreign"], expected_user="main-user")
    probe.install()
    try:
        asyncio.run(searcher.search("wrong-user", "synthetic"))
    finally:
        probe.restore()
    assert probe.foreign_id_result_calls == 0
    assert probe.unowned_id_result_calls == 0
    assert probe.unexpected_user_calls == 1


class _RerankHarness:
    async def rerank(self, _query: str, items):  # noqa: ANN001, ANN201
        return [{**item, "_rerank_score": 1.0} for item in items]


def test_reranker_probe_rejects_foreign_ids_on_both_sides() -> None:
    harness = _RerankHarness()
    searcher = type("Searcher", (), {})()
    searcher._reranker = harness.rerank
    probe = battery.RerankerPrivacyProbe(searcher, ())
    probe.configure_ownership(
        main_ids=["ko_main"],
        foreign_ids=["ko_foreign"],
        expected_user="main-user",
    )
    probe.install()
    try:
        asyncio.run(searcher._reranker("synthetic", [{"id": "ko_foreign"}]))
    finally:
        probe.restore()
    assert probe.foreign_id_calls == 1
    assert probe.foreign_id_result_calls == 1
    assert probe.unowned_id_calls == 1
    assert probe.unowned_id_result_calls == 1
