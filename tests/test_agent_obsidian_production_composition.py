"""Production-composition gate for the stateful Obsidian acceptance paths.

Unlike the broad exact-message routing matrix, these tests keep the real
``ExecutionKernel`` schemas and handlers, ``ObsidianRuntime``, vault files and
durable continuation rows in the loop.  Syncthing itself remains a typed fake;
physical Android delivery belongs to the manual acceptance battery.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from test_obsidian_runtime import _Client, _DeletionAwareClient, _Manager

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.orchestration.effect_outcome import (
    EffectAction,
    EffectObservationState,
    EffectPublishability,
    EffectStatus,
    load_accepted_effect_outcome_receipt,
)
from friday.organs import ServiceContext
from friday.organs.obsidian import ObsidianOrgan
from friday.organs.obsidian.frontmatter import parse_frontmatter
from friday.organs.obsidian.runtime import ObsidianRuntime
from friday.organs.obsidian.vault_store import VaultStore
from friday.permissions import ActorContext, AuthorizationService

_TODAY = date(2026, 8, 22)
_ACTOR = ActorContext(user_id="alice", preset_key="owner", source="test")
_NOTE_CREATE = (
    "Создай в Obsidian заметку `Projects/Friday Test.md`. Заголовок: «Тест интеграции "
    "Friday». Внутри напиши, что заметка создана через Telegram, и добавь текущую дату."
)
_NOTE_APPEND = (
    "Добавь в конец заметки `Projects/Friday Test.md` раздел «Проверка дополнения» "
    "и одну строку: «Этот текст был добавлен отдельной командой»."
)
_DAILY_APPEND = (
    "Добавь в сегодняшнюю ежедневную заметку раздел «Friday» и пункт: «Проверена интеграция с Obsidian»."
)
_TASK_ADD = "Добавь в сегодняшнюю заметку задачу проверить поиск в Obsidian завтра в 10 утра."
_TASK_QUERY = "Покажи незавершённые задачи про Obsidian."
_LIVE_SUMMARY = "Обобщи все мои сегодняшние заметки в обсидиан"
_LIVE_META = (
    "У заметки Projects/Friday Test.md поставь статус review, проект Friday и "
    "добавь теги integration, obsidian и test."
)
_SEARCH_PARAPHRASE = (
    "Найди в Obsidian заметку, где мы обсуждали, что старые файлы не попадали в поиск "
    "из-за слишком маленького списка кандидатов."
)
_LIVE_DATED_SEARCH = "Найди заметку про проблемы поиска, которую я делал примерно в начале августа."
_CONT_SEARCH = "Найди все заметки про Friday и поиск."
_CONT_SELECT = "Открой вторую."
_CONT_APPEND = "Добавь туда раздел «Следующие шаги» и пункт про проверку семантического индекса."
_BACKLINKS = "Какие заметки ссылаются на `Projects/Friday`?"
_MOVE = "Перемести `Projects/Friday.md` в `Architecture/Friday.md` и обнови ссылки на неё."
_MOVED_BACKLINKS = "Какие заметки теперь ссылаются на архитектуру Friday?"
_TEMPLATE_CREATE = (
    "Создай по шаблону Meeting заметку о проверке интеграции Obsidian. Проект Friday, "
    "участники Алиса и Борис. В обсуждение добавь, что базовая синхронизация работает. "
    "В действия добавь задачу проверить конфликты."
)
_WORK_SAVE = (
    "Сохрани краткие итоги нашего текущего разговора в Obsidian. Создай заметку "
    "`Research/Conversation Summary.md`, отдельно укажи выводы, нерешённые вопросы "
    "и следующие действия."
)
_WORK_LINKS = "Добавь туда ссылки на заметки, которые мы сегодня использовали."
_USED_NOTE_SEARCH = "Найди заметку про фиолетовый маршрутизатор."
_BASE_CREATE = (
    "Создай Base `Friday Active Notes`, который показывает заметки проекта Friday "
    "со статусом не `done`. Выведи название, статус и дату изменения."
)
_BASE_QUERY = "Покажи актуальные заметки из Base `Friday Active Notes`."
_OFFLINE_CREATE = (
    "Создай заметку `Offline/Pending Delivery.md` и напиши, что она была создана, пока телефон был offline."
)
_CONFLICT_REPLACE = "Замени раздел «Проверка дополнения» текстом: «Версия, записанная Friday»."
_CONFLICT_PREVIEW = "Покажи различия и собери объединённую версию, сохранив оба изменения."
_CONFLICT_ACCEPT = "Прими эту объединённую версию."
_RECOVERY_APPEND = "Добавь в ежедневную заметку строку «Проверка идемпотентности»."
_RECOVERY_RESUME = "Продолжай предыдущую задачу."
_DELETE = "Удали тестовую заметку `Scratch/Delete Me.md`."
_DELETE_SEARCH = "Найди заметку Delete Me."


class _NoModel:
    enabled = False
    total_budget_sec = 1.0

    async def chat(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("an exact Obsidian composition request reached the model")


class _RecordingKernel(ExecutionKernel):
    """Run the production kernel while retaining its transient result for assertions."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.results: list[tuple[str, Any]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
    ) -> Any:
        result = await super().execute(name, arguments, actor=actor)
        self.results.append((name, result))
        return result


@dataclass(slots=True)
class _Stack:
    settings: Any
    storage: Any
    runtime: ObsidianRuntime
    client: _Client
    agent: AgentRuntime
    kernel: _RecordingKernel

    @property
    def store(self) -> VaultStore:
        vault = self.storage.get_obsidian_vault("alice")
        assert vault is not None
        return VaultStore(Path(str(vault["server_path"])))


def _build_agent(
    configured: Any,
    storage: Any,
    runtime: ObsidianRuntime,
) -> tuple[AgentRuntime, _RecordingKernel]:
    authorization = AuthorizationService(storage)
    organ = ObsidianOrgan()
    for capability in organ.capabilities():
        authorization.register_capability(capability)
    kernel = _RecordingKernel(authorization, configured)
    # This is the normal server-side kernel binding.  The unrelated core tools
    # stay present, which also pins that an Obsidian path never falls into
    # ``make_file``.
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    context = ServiceContext(
        settings=configured,
        storage=storage,
        kg=None,
        ingestion=None,
        auth=authorization,
        obsidian=runtime,
    )
    for tool in organ.tools(context):
        kernel.register(tool)
    kernel.assert_risk_declarations_agree()

    agent = AgentRuntime(
        configured,
        storage,
        llm=_NoModel(),  # type: ignore[arg-type]
        kernel=kernel,
    )
    agent._local_today = lambda: _TODAY  # type: ignore[method-assign]

    async def forbidden_general_context(*args: object, **kwargs: object) -> None:
        raise AssertionError("an exact Obsidian request entered general retrieval")

    agent._prepare_context = forbidden_general_context  # type: ignore[method-assign]
    return agent, kernel


async def _ready_stack(
    settings: Any,
    storage: Any,
    tmp_path: Path,
    *,
    client: _Client | None = None,
) -> _Stack:
    storage.ensure_user("alice", preset_key="owner")
    short_root = tmp_path.parents[1] / (
        "obs-composition-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:10]
    )
    configured = replace(
        settings,
        verify_answers=False,
        local_timezone="Europe/Berlin",
        obsidian_enabled=True,
        obsidian_root=short_root,
        obsidian_vault_name="Friday-Test",
        obsidian_syncthing_binary="/bin/true",
        obsidian_public_base_url="https://friday.example",
        obsidian_pairing_ttl_sec=900,
    )
    selected = client or _Client()
    runtime = ObsidianRuntime(configured, storage, _Manager(selected))
    assert (await runtime.start("alice"))["state"] == "awaiting_android_device"
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    agent, kernel = _build_agent(configured, storage, runtime)
    return _Stack(configured, storage, runtime, selected, agent, kernel)


async def _chat(
    stack: _Stack,
    message: str,
    *,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    kwargs = {"conversation_id": conversation_id} if conversation_id else {}
    return await stack.agent.chat("alice", message, actor=_ACTOR, **kwargs)


def _receipt(stack: _Stack, reply: dict[str, Any]) -> dict[str, Any]:
    tools = reply.get("tools_used")
    assert isinstance(tools, list) and tools
    target = str(tools[-1])
    name, result = next(item for item in reversed(stack.kernel.results) if item[0] == target)
    assert name == target and result.success is True
    assert isinstance(result.data, dict)
    return result.data


def _effect_receipt(stack: _Stack, reply: dict[str, Any]):  # noqa: ANN202
    stored = stack.storage.get_message(str(reply["message_id"]), "alice")
    assert stored is not None
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    return load_accepted_effect_outcome_receipt(metadata), metadata


@pytest.mark.asyncio
async def test_note_create_append_and_daily_exact_messages_mutate_the_real_vault(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)

    created = await _chat(stack, _NOTE_CREATE)
    conversation_id = str(created["conversation_id"])
    created_receipt = _receipt(stack, created)
    assert created["tools_used"] == ["obsidian_list_vaults", "obsidian_create_note"]
    assert created_receipt["path"] == "Projects/Friday Test.md"
    assert created_receipt["status"] == "delivered"
    assert created_receipt["delivery"]["android_received"] is True
    created_text = stack.store.read_text("Projects/Friday Test.md").text()
    assert created_text.startswith(
        "# Тест интеграции Friday\n\nЗаметка создана через Telegram.\n\n2026-08-22\n"
    )
    assert created_text.count("Заметка создана через Telegram.") == 1
    assert "Локальная дата: 2026-08-22." in created["message"]
    created_effect, created_metadata = _effect_receipt(stack, created)
    assert created_effect.outcome.status is EffectStatus.SUCCEEDED
    assert created_effect.outcome.action is EffectAction.CREATE
    assert created_effect.outcome.observations.server_sync is EffectObservationState.OBSERVED
    assert created_effect.outcome.observations.reingest is EffectObservationState.OBSERVED
    assert created_effect.outcome.observations.physical_device is EffectObservationState.OBSERVED
    private_effect_json = json.dumps(
        created_metadata["accepted_effect_outcome"], ensure_ascii=False
    )
    assert "Projects/Friday Test.md" not in private_effect_json
    assert "Заметка создана через Telegram" not in private_effect_json
    assert "alice" not in private_effect_json
    assert str(created_receipt["operation_id"]) not in private_effect_json

    appended = await _chat(stack, _NOTE_APPEND, conversation_id=conversation_id)
    appended_receipt = _receipt(stack, appended)
    note = stack.store.read_text("Projects/Friday Test.md").text()
    assert appended["tools_used"] == ["obsidian_list_vaults", "obsidian_append_note"]
    assert appended_receipt["path"] == created_receipt["path"]
    assert appended_receipt["previous_revision"] == created_receipt["revision"]
    assert note.startswith("# Тест интеграции Friday\n\nЗаметка создана через Telegram.")
    assert note.count("## Проверка дополнения") == 1
    assert note.count("Этот текст был добавлен отдельной командой") == 1
    appended_effect, _appended_metadata = _effect_receipt(stack, appended)
    assert appended_effect.outcome.status is EffectStatus.SUCCEEDED
    assert appended_effect.outcome.action is EffectAction.APPEND

    daily = await _chat(stack, _DAILY_APPEND, conversation_id=conversation_id)
    daily_receipt = _receipt(stack, daily)
    daily_text = stack.store.read_text("Daily/2026-08-22.md").text()
    assert daily["tools_used"] == ["obsidian_list_vaults", "obsidian_daily_note"]
    assert daily_receipt["path"] == "Daily/2026-08-22.md"
    assert daily_text.count("## Friday") == 1
    assert daily_text.count("- Проверена интеграция с Obsidian") == 1
    daily_stored = storage.get_message(str(daily["message_id"]), "alice")
    assert daily_stored is not None
    assert "accepted_effect_outcome" not in json.loads(str(daily_stored["metadata_json"] or "{}"))


@pytest.mark.asyncio
async def test_task_exact_add_and_query_use_the_real_workflow_handler(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.store.write_text(
        "Daily/2026-08-21.md",
        "- [x] Старый поиск в Obsidian 📅 2026-08-21 ⏰ 10:00\n",
        create_only=True,
    )

    added = await _chat(stack, _TASK_ADD)
    conversation_id = str(added["conversation_id"])
    added_receipt = _receipt(stack, added)
    daily = stack.store.read_text("Daily/2026-08-22.md").text()
    assert added["tools_used"] == ["obsidian_list_vaults", "obsidian_workflow_write"]
    assert added_receipt["action"] == "add_task"
    assert added_receipt["path"] == "Daily/2026-08-22.md"
    assert daily.count("- [ ] проверить поиск в Obsidian") == 1
    assert "📅 2026-08-23 ⏰ 10:00" in daily

    queried = await _chat(stack, _TASK_QUERY, conversation_id=conversation_id)
    queried_receipt = _receipt(stack, queried)
    assert queried["tools_used"] == ["obsidian_list_vaults", "obsidian_workflow_read"]
    assert queried_receipt["action"] == "search_tasks"
    assert "Daily/2026-08-22.md" in queried_receipt["body"]
    assert "2026-08-23T10:00" in queried_receipt["body"]
    assert "Daily/2026-08-21.md" not in queried_receipt["body"]


@pytest.mark.asyncio
async def test_live_today_summary_uses_the_production_schema_and_real_vault(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.store.write_text(
        "Projects/Today Summary.md",
        "# Today Summary\n\n## Result\n\nИнтеграция Obsidian работает.\n",
        create_only=True,
    )
    stable_mtime = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 12, tzinfo=UTC).timestamp()
    os.utime(stack.store.root / "Projects/Today Summary.md", (stable_mtime, stable_mtime))

    reply = await _chat(stack, _LIVE_SUMMARY)
    receipt = _receipt(stack, reply)

    assert reply["tools_used"] == ["obsidian_list_vaults", "obsidian_workflow_read"]
    assert receipt["action"] == "summarize_today_notes"
    assert receipt["status"] == "completed"
    assert "Projects/Today Summary.md" in receipt["body"]
    assert "Интеграция Obsidian работает." in receipt["body"]
    assert "Projects/Today Summary.md" in reply["message"]


@pytest.mark.asyncio
async def test_search_01_exact_paraphrase_reaches_the_real_semantic_search(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.store.write_text(
        "Projects/Retrieval Problem.md",
        (
            "Старые документы иногда исчезали из семантической выдачи, потому что "
            "набор кандидатов ограничивался сравнительно свежими объектами.\n"
        ),
        create_only=True,
    )
    stack.store.write_text(
        "Projects/Lexical Noise.md",
        "Поиск файлов, список кандидатов, поиск файлов, список кандидатов.\n",
        create_only=True,
    )

    reply = await _chat(stack, _SEARCH_PARAPHRASE)
    receipt = _receipt(stack, reply)

    assert reply["tools_used"] == ["obsidian_list_vaults", "obsidian_search_notes"]
    assert receipt["coverage"]["state"] == "complete"
    assert receipt["matches"][0]["path"] == "Projects/Retrieval Problem.md"
    assert "semantic" in receipt["matches"][0]["match_channels"]
    assert "Projects/Retrieval Problem.md" in reply["message"]


@pytest.mark.asyncio
async def test_backlinks_move_and_requery_exact_messages_use_real_identity_and_link_handlers(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.store.write_text("Projects/Friday.md", "# Friday\n", create_only=True)
    stack.store.write_text("Notes/Search.md", "[[Projects/Friday]]\n", create_only=True)
    stack.store.write_text("Notes/Obsidian.md", "[[Projects/Friday]]\n", create_only=True)
    stack.store.write_text("Notes/Plain.md", "Projects/Friday\n", create_only=True)
    await stack.runtime.reconcile()
    before = next(
        item
        for item in storage.list_obsidian_note_bindings("alice")
        if item["current_path"] == "Projects/Friday.md"
    )

    initial = await _chat(stack, _BACKLINKS)
    conversation_id = str(initial["conversation_id"])
    initial_receipt = _receipt(stack, initial)
    assert initial_receipt["action"] == "backlinks"
    assert set(initial_receipt["changed_paths"]) == {"Notes/Search.md", "Notes/Obsidian.md"}
    assert "Notes/Plain.md" not in initial_receipt["body"]

    moved = await _chat(stack, _MOVE, conversation_id=conversation_id)
    moved_receipt = _receipt(stack, moved)
    assert moved_receipt["action"] == "move_note"
    assert moved_receipt["path"] == "Architecture/Friday.md"
    assert not stack.store.exists("Projects/Friday.md")
    assert stack.store.exists("Architecture/Friday.md")
    assert "[[Architecture/Friday]]" in stack.store.read_text("Notes/Search.md").text()
    assert "[[Architecture/Friday]]" in stack.store.read_text("Notes/Obsidian.md").text()
    after = next(
        item
        for item in storage.list_obsidian_note_bindings("alice")
        if item["current_path"] == "Architecture/Friday.md"
    )
    assert after["integration_id"] == before["integration_id"]

    refreshed = await _chat(stack, _MOVED_BACKLINKS, conversation_id=conversation_id)
    refreshed_receipt = _receipt(stack, refreshed)
    assert refreshed_receipt["action"] == "backlinks"
    assert refreshed_receipt["path"] == "Architecture/Friday.md"
    assert set(refreshed_receipt["changed_paths"]) == {"Notes/Search.md", "Notes/Obsidian.md"}


@pytest.mark.asyncio
async def test_template_exact_message_uses_the_real_template_and_write_handler(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.store.write_text(
        "Templates/Meeting.md",
        (
            "---\ntype: meeting\ndate: {{date}}\nproject: {{project}}\n---\n\n"
            "# {{title}}\n\n## Participants\n\n{{participants}}\n\n"
            "## Discussion\n\n{{discussion}}\n\n## Actions\n\n{{actions}}\n"
        ),
        create_only=True,
    )

    reply = await _chat(stack, _TEMPLATE_CREATE)
    receipt = _receipt(stack, reply)
    expected_path = "Meetings/2026-08-22 Проверка интеграции Obsidian.md"
    rendered = stack.store.read_text(expected_path).text()
    parsed = parse_frontmatter(rendered)

    assert reply["tools_used"] == ["obsidian_list_vaults", "obsidian_workflow_write"]
    assert receipt["action"] == "create_from_template"
    assert receipt["path"] == expected_path
    assert parsed.properties["type"].value == "meeting"
    assert parsed.properties["date"].value == _TODAY
    assert parsed.properties["project"].value == "Friday"
    assert "# Проверка интеграции Obsidian" in rendered
    assert "Алиса, Борис" in rendered
    assert "Базовая синхронизация работает." in rendered
    assert "- [ ] Проверить конфликты" in rendered


@pytest.mark.asyncio
async def test_offline_exact_create_reports_pending_then_delivers_the_same_real_revision(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.client.connected = False
    stack.client.available = False

    reply = await _chat(stack, _OFFLINE_CREATE)
    receipt = _receipt(stack, reply)
    path = "Offline/Pending Delivery.md"
    before = stack.store.read_text(path)

    assert reply["tools_used"] == ["obsidian_list_vaults", "obsidian_create_note"]
    assert receipt["status"] == "delivery_pending"
    assert receipt["path"] == path
    assert receipt["delivery"]["local_write_complete"] is True
    assert receipt["delivery"]["server_scan_complete"] is True
    assert receipt["delivery"]["android_connected"] is False
    assert receipt["delivery"]["android_received"] is False
    assert "Получение этой revision на Android: ожидается." in reply["message"]
    assert before.text().count("телефон был offline") == 1

    stack.client.connected = True
    stack.client.available = True
    delivered = await stack.runtime.get_operation("alice", str(receipt["operation_id"]))

    assert delivered["status"] == "delivered"
    assert delivered["revision"] == receipt["revision"] == before.revision
    assert delivered["delivery"]["android_received"] is True
    assert stack.store.read_text(path).text() == before.text()


@pytest.mark.asyncio
async def test_committed_create_keeps_private_suppressed_effect_receipt_after_late_revoke(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    execute = stack.kernel.execute

    async def execute_then_revoke(
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
    ) -> Any:
        result = await execute(name, arguments, actor=actor)
        if name == "obsidian_create_note" and result.success:
            storage.set_permission_override("alice", "obsidian.write", "deny")
        return result

    stack.kernel.execute = execute_then_revoke  # type: ignore[method-assign]

    reply = await _chat(stack, _NOTE_CREATE)

    assert stack.store.exists("Projects/Friday Test.md")
    assert reply["obsidian_authority_changed_before_publication"] is True
    assert "Путь:" not in reply["message"]
    effect, metadata = _effect_receipt(stack, reply)
    assert effect.outcome.status is EffectStatus.SUCCEEDED
    assert effect.outcome.publishability is EffectPublishability.SUPPRESSED
    assert effect.outcome.authority_rechecked is True
    encoded = json.dumps(metadata["accepted_effect_outcome"], ensure_ascii=False)
    assert "Projects/Friday Test.md" not in encoded
    assert "Заметка создана через Telegram" not in encoded


@pytest.mark.asyncio
async def test_effect_receipt_validation_failure_rolls_back_the_assistant_message(
    settings: Any,
    storage: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)

    def reject_receipt(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic accepted effect receipt rejection")

    monkeypatch.setattr(
        "friday.agent_runtime.load_accepted_effect_outcome_receipt",
        reject_receipt,
    )

    with pytest.raises(RuntimeError, match="synthetic accepted effect receipt rejection"):
        await _chat(stack, _NOTE_CREATE)

    assert stack.store.exists("Projects/Friday Test.md")
    conversation = storage.list_conversations("alice", limit=1)[0]
    rows = storage.get_conversation_messages(str(conversation["id"]), user_id="alice")
    assert [str(row["role"]) for row in rows] == ["user"]


@pytest.mark.asyncio
async def test_conflict_exact_replace_preview_and_accept_use_the_real_preserve_both_handler(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    canonical_path = "Projects/Friday Test.md"
    conflict_path = "Projects/Friday Test.sync-conflict-20260822.md"
    stack.store.write_text(
        canonical_path,
        ("# Friday Test\n\n## Проверка дополнения\n\nСтарая версия\n\n## Keep\n\nСохранить\n"),
        create_only=True,
    )
    stack.client.connected = False
    stack.client.available = False

    replaced = await _chat(stack, _CONFLICT_REPLACE)
    conversation_id = str(replaced["conversation_id"])
    replaced_receipt = _receipt(stack, replaced)
    assert replaced_receipt["action"] == "replace_active_section"
    assert replaced_receipt["path"] == canonical_path
    assert replaced_receipt["delivery"]["android_received"] is False
    canonical_after_replace = stack.store.read_text(canonical_path).text()
    assert "Версия, записанная Friday" in canonical_after_replace
    assert "Старая версия" not in canonical_after_replace
    assert "## Keep\n\nСохранить" in canonical_after_replace

    stack.store.write_text(
        conflict_path,
        ("# Friday Test\n\n## Проверка дополнения\n\nВерсия Android\n\n## Keep\n\nСохранить\n"),
        create_only=True,
    )
    vault = storage.get_obsidian_vault("alice")
    assert vault is not None
    conflict = storage.record_obsidian_conflict(
        "alice",
        vault_id=str(vault["id"]),
        canonical_path=canonical_path,
        conflict_path=conflict_path,
    )
    stack.client.connected = True
    stack.client.available = True

    previewed = await _chat(stack, _CONFLICT_PREVIEW, conversation_id=conversation_id)
    preview_receipt = _receipt(stack, previewed)
    assert preview_receipt["action"] == "conflict_preview"
    assert preview_receipt["status"] == "preview"
    assert "Версия, записанная Friday" in preview_receipt["body"]
    assert "Версия Android" in preview_receipt["body"]
    assert "Merged preview (not applied)" in preview_receipt["body"]
    assert "Версия Android" not in stack.store.read_text(canonical_path).text()
    assert stack.store.exists(conflict_path)

    accepted = await _chat(stack, _CONFLICT_ACCEPT, conversation_id=conversation_id)
    accepted_receipt = _receipt(stack, accepted)
    merged = stack.store.read_text(canonical_path).text()
    assert accepted_receipt["action"] == "accept_conflict_merge"
    assert accepted_receipt["status"] == "completed"
    assert "Версия, записанная Friday" in merged
    assert "Версия Android" in merged
    assert stack.store.exists(conflict_path)
    resolved = storage.get_obsidian_conflict("alice", str(conflict["id"]))
    assert resolved is not None and resolved["status"] == "resolved"


@pytest.mark.asyncio
async def test_live_failed_phrasings_execute_real_metadata_and_expanded_search_contract(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.store.write_text(
        "Projects/Friday Test.md",
        "# Тест интеграции Friday\n\nТело должно сохраниться.\n",
        create_only=True,
    )
    stack.store.write_text(
        "Projects/Retrieval Problem.md",
        (
            "---\ncreated: 2026_08_04\n---\n"
            "# Retrieval Problem\n\n"
            "Проблемы поиска: старые файлы не попадали в поиск из-за слишком маленького списка кандидатов.\n"
        ),
        create_only=True,
    )
    stack.store.write_text(
        "Projects/Search Noise.md",
        ("---\ncreated: 2026_07_18\n---\n# Search Noise\n\nПроблемы поиска и список кандидатов.\n"),
        create_only=True,
    )

    metadata_reply = await _chat(stack, _LIVE_META)
    metadata_receipt = _receipt(stack, metadata_reply)
    assert metadata_reply["tools_used"] == ["obsidian_list_vaults", "obsidian_workflow_write"]
    assert metadata_receipt["action"] == "update_metadata"
    parsed = parse_frontmatter(stack.store.read_text("Projects/Friday Test.md").text())
    assert parsed.body == "# Тест интеграции Friday\n\nТело должно сохраниться.\n"
    assert parsed.properties["status"].value == "review"
    assert parsed.properties["project"].value == "Friday"
    assert parsed.properties["tags"].value == ("integration", "obsidian", "test")

    search_reply = await _chat(stack, _LIVE_DATED_SEARCH)
    search_receipt = _receipt(stack, search_reply)
    assert search_reply["tools_used"] == ["obsidian_list_vaults", "obsidian_search_notes"]
    assert search_receipt["coverage"]["state"] == "complete"
    assert search_receipt["matches"][0]["path"] == "Projects/Retrieval Problem.md"
    assert "property_date_created" in search_receipt["matches"][0]["match_channels"]
    assert {
        "origin",
        "ownership_mode",
        "index_coverage",
    } <= set(search_receipt["matches"][0])
    assert "Projects/Retrieval Problem.md" in search_reply["message"]


@pytest.mark.asyncio
async def test_continuation_exact_messages_use_one_real_revision_pinned_candidate_set(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.store.write_text("Projects/First.md", "Friday поиск first\n", create_only=True)
    stack.store.write_text("Projects/Second.md", "Friday поиск second\n", create_only=True)

    first = await _chat(stack, _CONT_SEARCH)
    conversation_id = str(first["conversation_id"])
    matches = _receipt(stack, first)["matches"]
    assert len(matches) >= 2
    expected = str(matches[1]["path"])
    other = str(matches[0]["path"])
    assert {expected, other} <= {"Projects/First.md", "Projects/Second.md"}

    selected = await _chat(stack, _CONT_SELECT, conversation_id=conversation_id)
    selected_receipt = _receipt(stack, selected)
    assert selected_receipt["action"] == "select_candidate"
    assert selected_receipt["path"] == expected
    assert selected["obsidian_open_url"].endswith("#vault=Friday-Test&file=" + expected.replace("/", "%2F"))

    appended = await _chat(stack, _CONT_APPEND, conversation_id=conversation_id)
    append_receipt = _receipt(stack, appended)
    assert append_receipt["action"] == "append_active_section"
    assert append_receipt["path"] == expected
    assert "## Следующие шаги" in stack.store.read_text(expected).text()
    assert "проверку семантического индекса" in stack.store.read_text(expected).text()
    assert "## Следующие шаги" not in stack.store.read_text(other).text()


@pytest.mark.asyncio
async def test_work_summary_exact_followup_keeps_one_durable_work_item(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    used_path = "Projects/Used Today.md"
    stack.store.write_text(
        used_path,
        "Фиолетовый маршрутизатор использован в разговоре.\n",
        create_only=True,
    )

    used = await _chat(stack, _USED_NOTE_SEARCH)
    conversation_id = str(used["conversation_id"])
    assert _receipt(stack, used)["matches"][0]["path"] == used_path

    saved = await _chat(stack, _WORK_SAVE, conversation_id=conversation_id)
    saved_receipt = _receipt(stack, saved)
    assert saved_receipt["action"] == "save_summary"
    assert saved_receipt["path"] == "Research/Conversation Summary.md"

    linked = await _chat(stack, _WORK_LINKS, conversation_id=conversation_id)
    linked_receipt = _receipt(stack, linked)
    assert linked_receipt["action"] == "append_summary_links"
    assert linked_receipt["path"] == saved_receipt["path"]

    saved_operation = storage.get_obsidian_operation("alice", saved_receipt["operation_id"])
    linked_operation = storage.get_obsidian_operation("alice", linked_receipt["operation_id"])
    assert saved_operation is not None and linked_operation is not None
    work_item_id = str(saved_operation["work_item_id"])
    assert work_item_id.startswith("obswork_")
    assert linked_operation["work_item_id"] == work_item_id
    frame = storage.get_obsidian_active_frame("alice", work_item_id=work_item_id)
    assert frame is not None and frame["active_path"] == saved_receipt["path"]

    summary = stack.store.read_text("Research/Conversation Summary.md").text()
    assert all(heading in summary for heading in ("## Conclusions", "## Open questions", "## Next actions"))
    assert "## Related notes" in summary
    assert "[[Projects/Used Today]]" in summary


@pytest.mark.asyncio
async def test_base_exact_create_query_and_requery_use_current_real_vault_revisions(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    active_path = "Projects/Active.md"
    stack.store.write_text(
        active_path,
        "---\nproject: Friday\nstatus: active\n---\n# Active\n",
        create_only=True,
    )
    stack.store.write_text(
        "Projects/Done.md",
        "---\nproject: Friday\nstatus: done\n---\n# Done\n",
        create_only=True,
    )

    created = await _chat(stack, _BASE_CREATE)
    conversation_id = str(created["conversation_id"])
    created_receipt = _receipt(stack, created)
    assert created_receipt["action"] == "create_base"
    assert created_receipt["path"] == "Bases/Friday Active Notes.base"
    assert stack.store.exists("Bases/Friday Active Notes.base")

    initial = await _chat(stack, _BASE_QUERY, conversation_id=conversation_id)
    initial_receipt = _receipt(stack, initial)
    assert initial_receipt["action"] == "query_base"
    assert "file.name=Active" in initial_receipt["body"]
    assert "file.name=Done" not in initial_receipt["body"]

    current = stack.store.read_text(active_path)
    stack.store.write_text(
        active_path,
        current.text().replace("status: active", "status: done"),
        expected_revision=current.revision,
    )
    await stack.runtime.reconcile()

    refreshed = await _chat(stack, _BASE_QUERY, conversation_id=conversation_id)
    refreshed_receipt = _receipt(stack, refreshed)
    assert refreshed_receipt["action"] == "query_base"
    assert "актуальных строк: 0" in refreshed_receipt["body"]
    assert "file.name=Active" not in refreshed_receipt["body"]


@pytest.mark.asyncio
async def test_recovery_exact_resume_reuses_the_real_pre_restart_operation(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    stack = await _ready_stack(settings, storage, tmp_path)
    stack.client.connected = False
    stack.client.available = False

    interrupted = await _chat(stack, _RECOVERY_APPEND)
    conversation_id = str(interrupted["conversation_id"])
    interrupted_receipt = _receipt(stack, interrupted)
    assert interrupted_receipt["status"] == "delivery_pending"
    assert interrupted_receipt["delivery"]["android_received"] is False
    original_operation_id = str(interrupted_receipt["operation_id"])

    stack.client.connected = True
    stack.client.available = True
    restarted_runtime = ObsidianRuntime(stack.settings, storage, _Manager(stack.client))
    restarted_agent, restarted_kernel = _build_agent(stack.settings, storage, restarted_runtime)
    restarted = _Stack(
        stack.settings,
        storage,
        restarted_runtime,
        stack.client,
        restarted_agent,
        restarted_kernel,
    )

    resumed = await _chat(restarted, _RECOVERY_RESUME, conversation_id=conversation_id)
    resumed_receipt = _receipt(restarted, resumed)
    assert resumed_receipt["action"] == "resume_previous"
    assert resumed_receipt["status"] == "resumed"
    assert resumed_receipt["operation_id"] == original_operation_id
    assert resumed_receipt["delivery"]["android_received"] is True
    assert storage.get_obsidian_operation("alice", original_operation_id)["status"] == "delivered"
    daily = restarted.store.read_text("Daily/2026-08-22.md").text()
    assert daily.count("Проверка идемпотентности") == 1


@pytest.mark.asyncio
async def test_delete_exact_sequence_returns_a_real_tombstone_not_a_live_match(
    settings: Any,
    storage: Any,
    tmp_path: Path,
) -> None:
    client = _DeletionAwareClient()
    stack = await _ready_stack(settings, storage, tmp_path, client=client)
    created = await stack.runtime.create_note(
        "alice",
        "fixture-delete-me",
        "Scratch/Delete Me.md",
        "temporary",
    )
    assert created["path"] == "Scratch/Delete Me.md"

    seeded = await _chat(stack, _DELETE_SEARCH)
    conversation_id = str(seeded["conversation_id"])
    assert "tombstone" not in _receipt(stack, seeded)["matches"][0]["match_channels"]
    client.deleted_paths.add("Scratch/Delete Me.md")

    deleted = await _chat(stack, _DELETE, conversation_id=conversation_id)
    deleted_receipt = _receipt(stack, deleted)
    assert deleted_receipt["action"] == "delete_note"
    assert deleted_receipt["revision"] is None
    assert not stack.store.exists("Scratch/Delete Me.md")

    searched = await _chat(stack, _DELETE_SEARCH, conversation_id=conversation_id)
    search_receipt = _receipt(stack, searched)
    assert search_receipt["matches"][0]["path"] == "Scratch/Delete Me.md"
    assert search_receipt["matches"][0]["match_channels"] == ["tombstone"]
    assert "была удалена" in searched["message"]
    frame = storage.get_obsidian_active_frame("alice", conversation_id)
    assert frame is not None and frame["active_binding_id"] is None
