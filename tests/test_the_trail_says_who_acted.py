"""След в журнале называет ЧЕЛОВЕКА, а не архив, в котором он работал.

В общем архиве `user_id` один на всех — это арендатор. Журнал аудита писал именно
его: «удалено знание, пользователь tenant». Такой след следом быть перестаёт — он
отвечает «кто-то из нас» на единственный вопрос, ради которого ведётся.

Разрыв был виден внутри одного файла: ядро уже писало `own_id` в записи
`tool.user_activity.out_of_scope` и `user_id` — в соседней `tool.user_activity` об
ОДНОМ И ТОМ ЖЕ событии.

Вторая половина — надзор за чтением. `_audit_cross_tenant_read` сравнивал цель с
`actor.user_id`, а `_target_user(None)` возвращает арендатора: сравнение давало
«читает своё» ВСЕГДА, и запись не появлялась ни разу. То есть ровно там, где людей
стало много, надзор за чтением выключался целиком.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer


@pytest.mark.asyncio
async def test_the_journal_names_the_person_not_the_tenant(settings, storage):
    """Мутация: вернуть `user_id=actor.user_id` в записи ядра — тест краснеет.

    Проверяется поставляемое: журнал читается фильтром по человеку, и запись,
    сделанная под арендатором, по этому фильтру не найдётся.
    """
    storage.ensure_user("tenant", preset_key="owner")
    storage.ensure_user("bob", preset_key="owner")
    auth = AuthorizationService(storage, shared_tenant="tenant")
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    actor = auth.actor_for_user("bob", source="test")
    assert actor.user_id == "tenant" and actor.own_id == "bob", "стенд собран не как общий архив"

    await kernel.execute("user_activity", {"person": "совершенно неизвестное имя"}, actor=actor)

    mine = [row for row in storage.list_audit_log("bob", limit=50) if row["action"].startswith("tool.")]
    assert mine, (
        "по фильтру «человек bob» нет ни одной записи — след записан под арендатором "
        "и отвечает «кто-то из нас»"
    )


def test_reading_the_shared_archive_leaves_a_trace(settings, storage):
    """Чтение админской дорогой не проходит бесследно только потому, что архив общий.

    Материал в общем архиве открыт всем по прямой просьбе владельца, и «Боб прочитал
    документ Алисы» — не нарушение. Но это и не «Боб прочитал своё»: приравняв одно
    к другому, система теряла ЕДИНСТВЕННЫЙ след чтения чужого.
    """
    from friday.admin_api._deps import _audit_cross_tenant_read

    storage.ensure_user("tenant", preset_key="owner")
    storage.ensure_user("bob", preset_key="owner")
    auth = AuthorizationService(storage, shared_tenant="tenant")
    actor = auth.actor_for_user("bob", source="test")

    written: list[dict] = []

    class _State:
        pass

    request = _State()
    request.state = _State()
    request.state.actor = actor
    request.state.client_ip = ""
    request.state.request_id = ""
    request.app = _State()
    request.app.state = _State()
    request.app.state.storage = storage

    class _Services:
        storage = None

    # `_audit` ходит в `_services(request).storage`; отдаём ему то же хранилище.
    request.app.state.admin_services = _Services()
    request.app.state.admin_services.storage = storage

    _audit_cross_tenant_read(request, "admin.knowledge.read", actor.user_id, knowledge_id="k1")
    written = [row for row in storage.list_audit_log("bob", limit=20) if row["action"] == "admin.knowledge.read"]
    assert written, "чтение общего архива не оставило следа вовсе"
    assert '"scope": "shared_archive"' in str(written[0].get("after_json") or ""), (
        "чтение общего архива записано так, будто человек читал только своё"
    )


def test_in_a_shared_archive_reading_ones_own_data_stays_quiet(settings, storage):
    """И в общем архиве «своё» остаётся своим.

    Найдено мутацией: сравнение по арендатору вместо человека оставляло все тесты
    зелёными — потому что ни один не описывал этот случай. Дырой это не является,
    но шумом является: в общем архиве `own_id` собственного человека не равен
    арендатору, и каждое чтение своих же данных писалось бы как чужое. Журнал, где
    записано всё, читают так же, как журнал, где не записано ничего.
    """
    from friday.admin_api._deps import _audit_cross_tenant_read

    storage.ensure_user("tenant", preset_key="owner")
    storage.ensure_user("bob", preset_key="owner")
    auth = AuthorizationService(storage, shared_tenant="tenant")
    actor = auth.actor_for_user("bob", source="test")

    class _State:
        pass

    request = _State()
    request.state = _State()
    request.state.actor = actor
    request.state.client_ip = ""
    request.state.request_id = ""
    request.app = _State()
    request.app.state = _State()
    request.app.state.storage = storage

    _audit_cross_tenant_read(request, "admin.knowledge.read", actor.own_id, knowledge_id="k1")
    rows = [row for row in storage.list_audit_log("bob", limit=20) if row["action"] == "admin.knowledge.read"]
    assert not rows, "чтение СВОИХ данных в общем архиве записано как чужое"


def test_reading_ones_own_data_still_stays_quiet(settings, storage):
    """Ошибка в другую сторону: журнал, который пишут все и всегда, не читают."""
    from friday.admin_api._deps import _audit_cross_tenant_read

    storage.ensure_user("solo", preset_key="owner")
    auth = AuthorizationService(storage)
    actor = auth.actor_for_user("solo", source="test")
    assert actor.own_id == actor.user_id, "стенд собран как общий архив, а нужен обычный"

    class _State:
        pass

    request = _State()
    request.state = _State()
    request.state.actor = actor
    request.state.client_ip = ""
    request.state.request_id = ""
    request.app = _State()
    request.app.state = _State()
    request.app.state.storage = storage

    _audit_cross_tenant_read(request, "admin.knowledge.read", actor.own_id, knowledge_id="k1")
    rows = [row for row in storage.list_audit_log("solo", limit=20) if row["action"] == "admin.knowledge.read"]
    assert not rows, "чтение собственных данных засоряет журнал"
