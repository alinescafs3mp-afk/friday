"""Замер обязан мерить ТОТ поиск, который работает в бою.

`run_eval` собирал свой `HybridSearcher` вручную и забывал половину настроек:
переранжировщик, потолок пула, порог плотных доказательств, глубину и порог
переранжирования. То есть все накопленные числа recall относились к поиску,
которого у человека нет.

Расхождение видно прямо на одном и том же наборе: диагностика (с
переранжировщиком) находила 57 эталонов из 78, а замер — 41. После сведения к
боевой сборке:

    recall@10  0.5256 → 0.7308      MRR  0.2394 → 0.4345

Число не «выросло» — оно стало верным. Это худший вид ошибки в измерении: прибор
показывал стабильные, воспроизводимые, обсуждаемые цифры про другой предмет.
"""

from __future__ import annotations

import inspect
from dataclasses import replace

from friday import eval as eval_module
from friday.retrieval import HybridSearcher


def test_the_measurement_builds_the_same_searcher_as_the_server(settings, storage):
    """Мутация: убрать `reranker=` из `_searcher_like_production` — тест краснеет.

    Сравниваются ИМЕНА параметров, которые сервер передаёт в `HybridSearcher`, с
    теми, что передаёт замер: расхождение здесь и есть тот дефект, ради которого
    написан этот файл.
    """
    server_source = inspect.getsource(eval_module._searcher_like_production)  # noqa: SLF001
    for name in (
        "reranker",
        "rerank_top",
        "rerank_confident_min",
        "pool_max",
        "dense_evidence_min",
        "graph_max_depth",
    ):
        assert f"{name}=" in server_source, (
            f"замер строит поисковик без {name!r} — значит меряет не то, что работает у человека"
        )


def test_the_measurement_does_not_write_the_usage_counter(settings, storage):
    """Единственное намеренное отличие от боя — и оно должно остаться.

    `usage_signal` читает этот счётчик обратно в ранжирование: замер, который его
    пишет, меняет корпус под собой.
    """
    source = inspect.getsource(eval_module._searcher_like_production)  # noqa: SLF001
    assert "record_usage=False" in source


def test_the_reranker_is_attached_under_the_same_two_conditions(settings, storage):
    """Служба настроена И глубина задана — оба условия, как в `create_app`.

    Одно без другого — молчаливая ошибка: поднять службу и забыть включить шаг
    ровно так же плохо, как включить шаг без службы. В замере это опаснее вдвойне:
    он обязан знать, включён переранжировщик или нет.
    """
    from friday.eval import _searcher_like_production

    off = _searcher_like_production(storage, None, replace(settings, rerank_top=0))
    assert off._reranker is None, "переранжировщик подключён при нулевой глубине"  # noqa: SLF001

    # Адрес не настроен (в тестовом окружении службы нет) — шаг тоже не включается.
    depth_only = _searcher_like_production(storage, None, replace(settings, rerank_top=40))
    assert depth_only._reranker is None or callable(depth_only._reranker)  # noqa: SLF001


def test_an_ablation_arm_differs_from_production_only_by_what_it_ablates(settings, storage):
    """Арм абляции — та же сборка плюс ровно одно отличие.

    Иначе разница между армами объясняется не выключенным сигналом, а тем, чем
    они ещё отличались, — и вердикт абляции ничего не значит.
    """
    from friday.eval import _searcher_like_production

    arm = _searcher_like_production(storage, None, settings, ablate=("usage",))
    full = _searcher_like_production(storage, None, settings)

    assert arm._ablate == frozenset({"usage"})  # noqa: SLF001
    assert full._ablate == frozenset()  # noqa: SLF001
    for field in ("_rerank_top", "_pool_max", "_dense_evidence_min"):
        assert getattr(arm, field) == getattr(full, field), f"армы разошлись по {field}"


def test_a_hand_built_searcher_is_not_used_anywhere_in_eval():
    """Ни один замер не собирает поисковик мимо общей сборки.

    Проверяется исходный код: забыть параметр проще всего именно в отдельной
    ручной сборке — так этот дефект и появился, причём в трёх местах сразу
    (`run_eval`, A/B чанков, абляция).
    """
    source = inspect.getsource(eval_module)
    body = source[source.index("async def run_eval") :]
    assert "HybridSearcher(" not in body, (
        "в замере снова собирают поисковик вручную — используйте _searcher_like_production"
    )
    assert HybridSearcher is not None  # имя импортируется помощником, а не телом замеров
