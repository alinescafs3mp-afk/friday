"""Аудит собственных правок: что сборка архива не обходит и не ломает.

Инструмент `collect_files` появился 2026-08-03 и пакует ИСХОДНЫЕ файлы. Он читает
хранилище напрямую, минуя поиск, — а значит мимо всего, что поиск попутно
соблюдает. Отсюда четыре вопроса, каждый из которых проверяется здесь:

  * не обходит ли он права (`knowledge.read` есть у гостя — значит гость получает
    ЧТО-ТО; вопрос в том, что именно);
  * не выходит ли он за пределы своего арендатора;
  * не отдаёт ли удалённое;
  * честен ли он в числах, когда упирается в собственные потолки.

Это ревью СВОИХ правок, а не системы: по опыту этого проекта оно даёт больше
находок (20 подтверждённых дефектов против 33 у общего аудита), потому что
свежий код ещё никем не прожит.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.storage.models import RawObject


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _kernel(settings, storage) -> ExecutionKernel:
    from friday.ingestion import IngestionPipeline

    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), IngestionPipeline(settings, storage, graph))
    return kernel


def _put(settings, storage, user: str, *, name: str, when: str, deleted: str | None = None) -> str:
    relative = f"{user}/{name}"
    target = settings.files_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"codex" * 20)
    raw = RawObject(
        id=f"raw-{user}-{name}",
        user_id=user,
        source="upload",
        source_ref=name,
        raw_content="текст",
        content_type="file",
        metadata_json={"filename": name, "stored_path": relative, "size_bytes": 100},
        received_at=when,
        created_at=when,
        deleted_at=deleted,
    )
    storage.store_raw_object(raw)
    return raw.id


@pytest.mark.anyio
async def test_one_tenants_files_never_reach_another(settings, storage) -> None:
    """Общий архив — решение владельца, но он общий не для ВСЕХ подряд.

    Арендатор задаётся актором, и файлы чужого арендатора в архив попасть не
    должны, как бы ни совпали дни.
    """
    storage.ensure_user("alice", preset_key="admin")
    storage.ensure_user("bob", preset_key="admin")
    _put(settings, storage, "alice", name="Своё.txt", when="2026-07-26T10:00:00+00:00")
    _put(settings, storage, "bob", name="Чужое.txt", when="2026-07-26T10:00:00+00:00")
    kernel = _kernel(settings, storage)

    result = await kernel.execute(
        "collect_files",
        {"days": ["2026-07-26"]},
        actor=ActorContext(user_id="alice", preset_key="admin", source="test"),
    )

    assert result.data["files_in_archive"] == 1, "в архив попал файл чужого арендатора"
    assert result.data["found_total"] == 1


@pytest.mark.anyio
async def test_a_deleted_file_is_not_resurrected(settings, storage) -> None:
    """Удалённое остаётся удалённым: архив — не способ достать то, что убрали."""
    storage.ensure_user("alice", preset_key="admin")
    _put(settings, storage, "alice", name="Живой.txt", when="2026-07-26T10:00:00+00:00")
    _put(
        settings,
        storage,
        "alice",
        name="Удалённый.txt",
        when="2026-07-26T11:00:00+00:00",
        deleted="2026-07-27T00:00:00+00:00",
    )
    kernel = _kernel(settings, storage)

    result = await kernel.execute(
        "collect_files",
        {"days": ["2026-07-26"]},
        actor=ActorContext(user_id="alice", preset_key="admin", source="test"),
    )

    assert result.data["files_in_archive"] == 1
    assert result.data["found_total"] == 1, "счёт видит удалённое, хотя выборка — нет"


@pytest.mark.anyio
async def test_a_guest_is_refused_when_the_preset_says_so(settings, storage) -> None:
    """Право проверяется ядром, а не инструментом — и оно должно работать.

    Гость с урезанным пресетом не должен получать чужие документы одним файлом
    только потому, что появился новый путь к ним.
    """
    storage.ensure_user("alice", preset_key="admin")
    storage.ensure_user("guest", preset_key="guest")
    _put(settings, storage, "alice", name="Секрет.txt", when="2026-07-26T10:00:00+00:00")
    kernel = _kernel(settings, storage)

    result = await kernel.execute(
        "collect_files",
        {"days": ["2026-07-26"]},
        actor=ActorContext(user_id="guest", preset_key="guest", source="test"),
    )

    # Гость либо получает отказ, либо СВОЙ (пустой) арендатор — но не чужие файлы.
    if result.success:
        assert result.data.get("files_in_archive", 0) == 0, "гость получил чужие документы архивом"


@pytest.mark.anyio
async def test_taking_other_peoples_files_leaves_a_trace(settings, storage) -> None:
    """Найдено этим самым ревью: след был безымянным.

    В общем архиве один запрос уносит файлы всех участников. В журнале при этом
    оставалось только имя инструмента, и на вопрос «что человек выгрузил вчера»
    ответить было нечем. Соседний `user_activity` такой след оставляет и прямо
    обещает это в описании.

    Мутация: убрать ветку `collect_files` из `_audit_details` — тест краснеет.
    """
    storage.ensure_user("alice", preset_key="admin")
    _put(settings, storage, "alice", name="Дело.txt", when="2026-07-26T10:00:00+00:00")
    kernel = _kernel(settings, storage)

    await kernel.execute(
        "collect_files",
        {"days": ["2026-07-26", "2026-07-29"]},
        actor=ActorContext(user_id="alice", preset_key="admin", source="test"),
    )

    import json

    rows = [
        row
        for row in storage.list_audit_log("alice", limit=20)
        if row["action"] == "tool.invoke" and row["target_id"] == "collect_files"
    ]
    assert rows, "выгрузка чужих файлов не оставила следа"
    details = json.loads(rows[0]["after_json"] or "{}")
    assert details.get("days") == ["2026-07-26", "2026-07-29"], f"в следе не видно, что выгружено: {details}"
    assert details.get("day_count") == 2


@pytest.mark.anyio
async def test_the_numbers_stay_honest_at_the_ceiling(settings, storage, monkeypatch) -> None:
    """У потолка числа обязаны сходиться: вошло + не вошло = всего.

    Замерено на живом архиве: инструмент отдавал слагаемые, и модель сложила их
    по-своему — «остальные 140 не поместились» при 1511 не вошедших.
    """
    import friday.execution_kernel as kernel_module

    storage.ensure_user("alice", preset_key="admin")
    for index in range(7):
        _put(settings, storage, "alice", name=f"Ф-{index}.txt", when="2026-07-26T10:00:00+00:00")
    monkeypatch.setattr(kernel_module, "_MAX_ARCHIVE_FILES", 3)
    kernel = _kernel(settings, storage)

    result = await kernel.execute(
        "collect_files",
        {"days": ["2026-07-26"]},
        actor=ActorContext(user_id="alice", preset_key="admin", source="test"),
    )

    packed = int(result.data["files_in_archive"])
    total = int(result.data["found_total"])
    said = str(result.data.get("not_all") or "")
    assert packed == 3 and total == 7
    assert "не вошло 4" in said, f"числа у потолка не сходятся: {said!r}"
