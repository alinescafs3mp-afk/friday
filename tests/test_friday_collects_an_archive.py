"""«Собери документы, пришедшие за 10, 13 и 25 число» — одним архивом.

Владелец 2026-08-03: «Пятница же не умеет архивы собирать? Надо, чтобы умела».
Умела она до этого только сочинять новый документ (`make_file`) — а просили
отдать УЖЕ ПРИСЛАННОЕ, как есть.

Три вещи проверяются отдельно, потому что ошибиться можно в каждой:

  * дни считаются в сутках ЧЕЛОВЕКА, а не по Гринвичу (тот же класс, что уже
    чинили в хронике, напоминаниях и тихих часах);
  * «за 10, 13 и 25» — это три дня, а не отрезок между ними: внутри отрезка
    лежат две недели чужих файлов;
  * что не поместилось, называется поимённо. Молчаливый обрез за сутки
    2026-08-01 нашёлся четырежды, и в архиве он опаснее прочих: человек унесёт
    файл с собой, считая его полным.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date, datetime

import pytest

from friday.execution_kernel import _MAX_ARCHIVE_BYTES, ExecutionKernel, _pack_archive
from friday.interaction_control_plane.legacy_trace import CapabilityStatus
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.storage.models import RawObject


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _NoLLM:
    """Выключенная модель: разбор остатка её спрашивает и обязан пережить отказ.

    Эти проверки — про саму сборку, а не про разбор реплики. Выключенная модель
    здесь честнее подставной: она проверяет заодно, что сборка работает и когда
    спросить не у кого.
    """

    enabled = False


def _kernel(settings, storage) -> ExecutionKernel:
    from friday.ingestion import IngestionPipeline

    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, object(), IngestionPipeline(settings, storage, graph))
    return kernel


def _put_file(
    settings,
    storage,
    user: str,
    *,
    name: str,
    when: str,
    body: bytes,
    mime_type: str = "application/octet-stream",
    media_kind: str = "",
) -> str:
    """Положить файл так же, как это делает приём: байты на диск, запись в базу."""
    relative = f"{user}/{name}"
    target = settings.files_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    raw = RawObject(
        id=f"raw-{name}-{when}",
        user_id=user,
        source="upload",
        source_ref=name,
        raw_content="текст документа",
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": name,
            "stored_path": relative,
            "size_bytes": len(body),
            "sha256": digest,
            "mime_type": mime_type,
            **({"media_kind": media_kind} if media_kind else {}),
        },
        received_at=when,
        created_at=when,
    )
    storage.store_raw_object(raw)
    return raw.id


# --- дни, названные человеком -------------------------------------------------


def test_a_bare_number_means_the_last_one_that_happened(settings, storage) -> None:
    """«За 25-е», сказанное 3 августа, — это 25 июля.

    25 августа ещё не наступило, и пустой архив был бы формально правильным и
    совершенно бесполезным ответом.
    """
    kernel = _kernel(settings, storage)
    today = date.today()
    days, unclear = kernel._days_meant(["25"])
    assert unclear == []
    assert len(days) == 1
    picked = date.fromisoformat(days[0])
    assert picked.day == 25
    assert picked <= today, f"{picked} ещё не наступило"


def test_a_number_already_passed_this_month_stays_in_it(
    settings, storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Число, которое в этом месяце уже было, берётся из этого месяца."""
    import friday.execution_kernel as kernel_module

    class FrozenLaterDay(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return cls(2026, 8, 15, 12, tzinfo=tz)

    monkeypatch.setattr(kernel_module, "datetime", FrozenLaterDay)
    kernel = _kernel(settings, storage)
    days, unclear = kernel._days_meant(["1"])
    assert days == ["2026-08-01"] and unclear == []


def test_three_numbers_are_three_days_not_a_range(settings, storage) -> None:
    """Ровно то, о чём просил владелец: «за 10, 13 и 25 число»."""
    kernel = _kernel(settings, storage)
    days, _ = kernel._days_meant(["10", "13", "25"])
    assert len(days) == 3
    assert sorted(date.fromisoformat(day).day for day in days) == [10, 13, 25]


def test_a_full_date_is_taken_as_is(settings, storage) -> None:
    kernel = _kernel(settings, storage)
    days, unclear = kernel._days_meant(["2026-07-29"])
    assert days == ["2026-07-29"]
    assert unclear == []


def test_the_same_day_twice_is_packed_once(settings, storage) -> None:
    kernel = _kernel(settings, storage)
    days, _ = kernel._days_meant(["10", "13", "10"])
    assert len(days) == 3 - 1


def test_a_day_that_never_existed_is_reported_not_guessed(settings, storage) -> None:
    """«31 февраля» заменять соседним днём — врать о том, что просили."""
    kernel = _kernel(settings, storage)
    days, unclear = kernel._days_meant(["позавчера", "сорок пятого"])
    assert days == []
    assert len(unclear) == 2, "непонятое молча отброшено"


# --- сама сборка --------------------------------------------------------------


@pytest.mark.anyio
async def test_the_archive_carries_the_original_files(settings, storage) -> None:
    """Мутация: не класть содержимое — архив пуст, тест краснеет."""
    storage.ensure_user("alice", preset_key="admin")
    _put_file(
        settings, storage, "alice", name="Отчёт.docx", when="2026-07-29T10:00:00+00:00", body=b"A" * 500
    )
    _put_file(
        settings, storage, "alice", name="Смета.xlsx", when="2026-07-29T14:00:00+00:00", body=b"B" * 700
    )
    kernel = _kernel(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("collect_files", {"days": ["2026-07-29"]}, actor=actor)

    assert result.success, result.error
    assert result.data["collected"] is True
    assert result.data["files_in_archive"] == 2
    assert result.attachment, "архив не доехал вложением"
    payload = result.attachment["content_base64"]
    import base64

    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload))) as archive:
        names = sorted(archive.namelist())
        assert names == ["Отчёт.docx", "Смета.xlsx"], f"в архиве {names}"
        assert archive.read("Отчёт.docx") == b"A" * 500, "содержимое подменилось"


@pytest.mark.anyio
async def test_collect_documents_excludes_voice_from_zip_and_exact_count(settings, storage) -> None:
    storage.ensure_user("alice", preset_key="admin")
    _put_file(
        settings,
        storage,
        "alice",
        name="Отчёт.pdf",
        when="2026-07-29T10:00:00+00:00",
        body=b"document",
        mime_type="application/pdf",
        media_kind="document",
    )
    _put_file(
        settings,
        storage,
        "alice",
        name="Голосовое.ogg",
        when="2026-07-29T10:01:00+00:00",
        body=b"voice",
        mime_type="audio/ogg",
        media_kind="voice",
    )
    storage.commit()

    ordinary_activity = storage.user_activity("alice", files_only=False, limit=10)
    assert {row.get("filename") for row in ordinary_activity} == {"Отчёт.pdf", "Голосовое.ogg"}

    kernel = _kernel(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")
    result = await kernel.execute("collect_files", {"days": ["2026-07-29"]}, actor=actor)

    assert result.success, result.error
    assert result.data["collected"] is True
    assert result.data["files_in_archive"] == 1
    assert result.data["found_total"] == 1
    assert result.attachment

    import base64

    with zipfile.ZipFile(io.BytesIO(base64.b64decode(result.attachment["content_base64"]))) as archive:
        assert archive.namelist() == ["Отчёт.pdf"]
        assert archive.read("Отчёт.pdf") == b"document"


@pytest.mark.anyio
async def test_the_base64_never_reaches_the_model(settings, storage) -> None:
    """Мегабайты вложения в контекст модели с окном 32 768 токенов не влезут.

    Ключ `_attachment` вынимается ядром до `to_llm_message()` — проверяется, что
    инструмент им и пользуется, а не кладёт байты в обычное поле.
    """
    storage.ensure_user("alice", preset_key="admin")
    _put_file(settings, storage, "alice", name="Файл.pdf", when="2026-07-29T10:00:00+00:00", body=b"X" * 4000)
    kernel = _kernel(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("collect_files", {"days": ["2026-07-29"]}, actor=actor)

    said = result.to_llm_message()
    assert "content_base64" not in said
    assert "XXXX" not in said, "содержимое файла уехало в контекст"
    assert "_attachment" not in result.data


@pytest.mark.anyio
async def test_days_are_counted_in_the_persons_own_day(settings, storage) -> None:
    """Файл, пришедший в 23:30 по Москве, принадлежит МОСКОВСКОМУ дню.

    По Гринвичу это ещё 20:30 того же дня — совпадает. Проверяется обратный
    край: 00:30 по Москве 30-го числа — это 21:30 UTC 29-го, и по UTC файл
    попал бы в выборку за 29-е, хотя человек прислал его тридцатого.
    """
    storage.ensure_user("alice", preset_key="admin")
    _put_file(settings, storage, "alice", name="Ночной.txt", when="2026-07-29T21:30:00+00:00", body=b"n")
    offset = 180  # МСК

    at_29 = storage.list_files_received_on("alice", days=["2026-07-29"], utc_offset_minutes=offset)
    at_30 = storage.list_files_received_on("alice", days=["2026-07-30"], utc_offset_minutes=offset)

    assert at_29 == [], "файл остался в дне по Гринвичу"
    assert len(at_30) == 1, "файл не нашёлся в дне человека"


@pytest.mark.anyio
async def test_an_empty_day_says_so_plainly(settings, storage) -> None:
    storage.ensure_user("alice", preset_key="admin")
    kernel = _kernel(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("collect_files", {"days": ["2026-07-29"]}, actor=actor)

    assert result.data["collected"] is False
    assert result.data["found"] == 0
    assert not result.attachment


@pytest.mark.anyio
async def test_what_did_not_fit_is_named(settings, storage) -> None:
    """Мутация: перестать считать `left_out` — обрез становится молчаливым."""
    storage.ensure_user("alice", preset_key="admin")
    _put_file(settings, storage, "alice", name="Целый.bin", when="2026-07-29T10:00:00+00:00", body=b"ok")
    # Запись есть, файла за ней нет: так выглядит потерянное хранилище.
    raw = RawObject(
        id="raw-missing",
        user_id="alice",
        source="upload",
        source_ref="Пропавший.docx",
        raw_content="",
        content_type="file",
        metadata_json={"filename": "Пропавший.docx", "stored_path": "alice/нет-такого.docx"},
        received_at="2026-07-29T11:00:00+00:00",
        created_at="2026-07-29T11:00:00+00:00",
    )
    storage.store_raw_object(raw)
    kernel = _kernel(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("collect_files", {"days": ["2026-07-29"]}, actor=actor)

    assert result.data["collected"] is True
    left = result.data.get("left_out") or []
    assert any("Пропавший.docx" in line for line in left), f"пропажа не названа: {left}"


@pytest.mark.anyio
async def test_the_tool_reports_the_whole_day_not_its_own_page(settings, storage, monkeypatch) -> None:
    """Проверяется ПОДКЛЮЧЕНИЕ отдельного счёта, а не сам счёт.

    Первая редакция проверяла `count_files_received_on` напрямую — и мутация
    «взять `len(rows)` вместо счёта» её НЕ уронила: механизм был исправен, а
    инструмент им не пользовался. Ровно тот случай, который ловится только
    мутацией вызова.
    """
    import friday.execution_kernel as kernel_module

    storage.ensure_user("alice", preset_key="admin")
    for number in range(5):
        _put_file(
            settings,
            storage,
            "alice",
            name=f"Файл-{number}.txt",
            when="2026-07-29T10:00:00+00:00",
            body=b"x",
        )
    monkeypatch.setattr(kernel_module, "_MAX_ARCHIVE_FILES", 2)
    kernel = _kernel(settings, storage)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("collect_files", {"days": ["2026-07-29"]}, actor=actor)

    assert result.data["files_in_archive"] == 2, "в архив уехало больше потолка"
    assert result.data["found_total"] == 5, "число за день пришло из длины своей страницы"
    assert "5" in str(result.data.get("not_all") or ""), "человеку не сказали, что вошло не всё"


def test_the_count_is_not_the_length_of_the_page(settings, storage) -> None:
    """«Длина страницы — не факт о корпусе»: за одну ночь та же ошибка трижды."""
    storage.ensure_user("alice", preset_key="admin")
    for number in range(5):
        _put_file(
            settings,
            storage,
            "alice",
            name=f"Файл-{number}.txt",
            when="2026-07-29T10:00:00+00:00",
            body=b"x",
        )

    page = storage.list_files_received_on("alice", days=["2026-07-29"], limit=2)
    total = storage.count_files_received_on("alice", days=["2026-07-29"])

    assert len(page) == 2
    assert total == 5, "счёт пришёл из длины страницы"


# --- упаковка -----------------------------------------------------------------


def test_files_are_named_humanly_not_by_hash(tmp_path) -> None:
    """`dded8fc9….ogg` в архиве бесполезен — имя должно быть человеческим."""
    root = tmp_path
    (root / "aa").mkdir()
    (root / "aa" / "deadbeef.ogg").write_bytes(b"voice")
    packed, left, size = _pack_archive(
        root, [{"stored_path": "aa/deadbeef.ogg", "filename": "Голосовое 12.ogg"}], "arch"
    )
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert archive.namelist() == ["Голосовое 12.ogg"]
    assert left == []
    assert size == 5


def test_two_files_with_one_name_do_not_overwrite_each_other(tmp_path) -> None:
    """Второй «Отчёт.docx» молча затёр бы первый — и человек не узнал бы."""
    root = tmp_path
    for index, folder in enumerate(("a", "b")):
        (root / folder).mkdir()
        (root / folder / "x.bin").write_bytes(bytes([index]) * 10)
    packed, _, _ = _pack_archive(
        root,
        [
            {"stored_path": "a/x.bin", "filename": "Отчёт.docx"},
            {"stored_path": "b/x.bin", "filename": "Отчёт.docx"},
        ],
        "arch",
    )
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert len(archive.namelist()) == 2, archive.namelist()
        assert archive.read("Отчёт.docx") != archive.read(archive.namelist()[1])


def test_a_path_leaving_the_storage_is_not_read(tmp_path) -> None:
    """Путь пришёл из базы, но `..` в нём увёл бы чтение за пределы хранилища."""
    root = tmp_path / "files"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("чужое", encoding="utf-8")
    packed, left, _ = _pack_archive(
        root, [{"stored_path": "../secret.txt", "filename": "secret.txt"}], "arch"
    )
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert archive.namelist() == []
    assert left and "нет" in left[0]


def test_the_size_ceiling_holds_and_says_what_was_dropped(tmp_path) -> None:
    root = tmp_path
    (root / "big.bin").write_bytes(b"0" * (_MAX_ARCHIVE_BYTES + 10))
    (root / "small.bin").write_bytes(b"1" * 10)
    packed, left, size = _pack_archive(
        root,
        [
            {"stored_path": "big.bin", "filename": "Большой.bin"},
            {"stored_path": "small.bin", "filename": "Малый.bin"},
        ],
        "arch",
    )
    with zipfile.ZipFile(io.BytesIO(packed)) as archive:
        assert archive.namelist() == ["Малый.bin"]
    assert size == 10
    assert any("Большой.bin" in line for line in left), left


# --- просьба узнаётся пониманием ---------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (("файл", "10,13,25"), ["10", "13", "25"]),
        (("файл", "2026-07-29"), ["2026-07-29"]),
    ],
)
@pytest.mark.anyio
async def test_the_archive_is_built_without_waiting_for_the_model(verdict, expected) -> None:
    """Мутация: убрать предварительную сборку — архив снова зависит от модели.

    Замерено на живом экземпляре 2026-08-01: `make_file` модель звала в 1 случае
    из 12, и два подхода уговорить её промптом дали те же 1/12. Решение звать
    инструмент остаётся её всюду, кроме случаев, где человек попросил
    однозначно, — а «собери документы за 10, 13 и 25 число» однозначно.
    """
    from friday.agent_runtime import AgentContext, AgentRuntime

    calls: list[tuple[str, dict]] = []

    class _Kernel:
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            calls.append((tool, params))

            class _Result:
                success = True
                data = {
                    "filename": "Документы.zip",
                    "files_in_archive": 2,
                    "days": ["2026-07-26", "2026-07-28"],
                }
                attachment = {"filename": "Документы.zip", "content_base64": "UEs="}

                def to_llm_message(self) -> str:
                    return "Архив: 2 файла."

            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    # Разбор остатка зовёт модель. Прибор без неё падал бы `AttributeError`,
    # то есть проверял бы не то, что работает у человека.
    runtime.llm = _NoLLM()
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=verdict)
    messages: list[dict] = []
    tools_used: list[str] = []
    evidence: list[dict] = []
    clips: list[dict] = []
    bound = AgentRuntime._prefetch_the_archive_if_asked.__get__(runtime, AgentRuntime)

    done = await bound(context, None, messages, tools_used, evidence, clips)

    assert done is True
    assert calls == [("collect_files", {"days": expected})], calls
    assert clips and clips[0]["filename"] == "Документы.zip"
    assert tools_used == ["collect_files"], "сборка не попала в основания хода"
    assert evidence and evidence[0]["tool"] == "collect_files"
    assert context.trace_tool_outcomes == [("collect_files", CapabilityStatus.SUCCEEDED)]


@pytest.mark.anyio
async def test_the_model_is_told_which_dates_were_actually_packed() -> None:
    """Замерено на живом экземпляре 2026-08-03: слово разошлось с делом.

    На «собери документы за 26 и 28 число» Пятница написала «соберу документы за
    26 и 28 АВГУСТА», а приложен был архив за 26 и 28 июля — собранный верно,
    августовских чисел ещё не наступало. Модель пересказывала число из реплики
    человека, потому что о результате сборки не знала: та шла ПОСЛЕ ответа.
    """
    from friday.agent_runtime import AgentContext, AgentRuntime

    class _Kernel:
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            class _Result:
                success = True
                # Имя файла НАМЕРЕННО без дат. Прежде оно звалось «Документы за
                # 2026-07-26 2026-07-28.zip», и проверка проходила даже когда
                # разобранные дни выбрасывались вовсе: даты попадали в текст через
                # имя. Показала мутация — тест не различал два источника.
                data = {
                    "filename": "Документы.zip",
                    "files_in_archive": 66,
                    "days": ["2026-07-26", "2026-07-28"],
                    "not_all": "за эти дни файлов 1605, в архив вошли первые 300",
                }
                attachment = {"filename": "Документы.zip", "content_base64": "UEs="}

                def to_llm_message(self) -> str:
                    return "Архив: 66 файлов."

            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    # Разбор остатка зовёт модель. Прибор без неё падал бы `AttributeError`,
    # то есть проверял бы не то, что работает у человека.
    runtime.llm = _NoLLM()
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=("файл", "26,28"))
    messages: list[dict] = []
    bound = AgentRuntime._prefetch_the_archive_if_asked.__get__(runtime, AgentRuntime)

    await bound(context, None, messages, [], [], [])

    # Читается то, что получает ЧЕЛОВЕК. Прежде это была служебная строка для
    # модели в расчёте на удачный пересказ; теперь факт о собранном говорит
    # структура, и проверять надо её текст.
    said = context.structural_answer
    assert "2026-07-26" in said and "2026-07-28" in said, said
    assert "66" in said, "человек не узнал, сколько файлов собрано"
    assert "1605" in said, "человеку не сказали, что вошло не всё"
    # Модели остаётся знать, что дело сделано, — иначе она предложит сделать его.
    told = str(messages[0]["content"])
    assert "уже собран" in told, told


@pytest.mark.anyio
async def test_a_failed_assembly_is_not_promised_as_a_file() -> None:
    """Иначе Пятница пообещает архив, которого нет: «сейчас соберу» и тишина."""
    from friday.agent_runtime import AgentContext, AgentRuntime

    class _Kernel:
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            class _Result:
                success = True
                data = {
                    "collected": False,
                    "found": 0,
                    "reason": "за эти дни файлов не приходило",
                }
                attachment = None

                def to_llm_message(self) -> str:
                    return "Нечего собирать."

            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    # Разбор остатка зовёт модель. Прибор без неё падал бы `AttributeError`,
    # то есть проверял бы не то, что работает у человека.
    runtime.llm = _NoLLM()
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=("файл", "13"))
    messages: list[dict] = []
    clips: list[dict] = []
    bound = AgentRuntime._prefetch_the_archive_if_asked.__get__(runtime, AgentRuntime)

    done = await bound(context, None, messages, [], [], clips)

    assert done is False
    assert clips == []
    said = str(messages[0]["content"])
    assert "не удалось" in said.lower()
    assert "не приходило" in said
    # Служебная реплика обязана читаться как нормальный ответ, даже если её
    # перескажут дословно. Замерено на живом экземпляре 2026-08-03: прежняя
    # редакция кончалась словами «Скажи это человеку прямо и не обещай файл», и
    # Пятница переслала владельцу всю строку целиком, вместе с указанием.
    assert "скажи" not in said.lower(), f"указание самой себе уедет человеку: {said}"
    assert "не обещай" not in said.lower(), said
    # И просьба остаётся просьбой об АРХИВЕ: сочинять вместо него документ нельзя.
    assert context.asked_for_an_archive is True
    assert context.trace_tool_outcomes == [("collect_files", CapabilityStatus.EMPTY)]


@pytest.mark.anyio
async def test_an_archive_provider_error_is_kept_outside_public_tool_evidence() -> None:
    from friday.agent_runtime import AgentContext, AgentRuntime

    class _Kernel:
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            class _Result:
                success = False
                # A stale zero-hit payload cannot override the terminal failure.
                data = {"collected": False, "found": 0}
                attachment = None
                error = "provider failed"

                def to_llm_message(self) -> str:
                    return ""

            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.llm = _NoLLM()
    context = AgentContext(
        conversation_id="c",
        user_id="u",
        outward_verdict=("файл", "13"),
    )
    tools_used: list[str] = []
    evidence: list[dict] = []
    clips: list[dict] = []

    done = await AgentRuntime._prefetch_the_archive_if_asked(
        runtime,
        context,
        None,
        [],
        tools_used,
        evidence,
        clips,
    )

    assert done is False
    assert tools_used == []
    assert evidence == []
    assert clips == []
    assert context.trace_tool_outcomes == [("collect_files", CapabilityStatus.FAILED)]


@pytest.mark.anyio
async def test_a_failed_assembly_does_not_turn_into_an_invented_document() -> None:
    """«Собери документы за 13 число» — файлов нет, и docx вместо них не нужен.

    Замерено на живом экземпляре 2026-08-03: архив не собрался, а человеку уехал
    сочинённый «Собери документы за 13 число.docx». Просили присланное, получили
    выдумку на пустом месте.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime.chat)
    at = source.index("_file_for_a_request_that_wanted_one(")
    branch_start = source.rfind("\n        if (", 0, at)
    assert branch_start >= 0, "не найдена ветка поздней сборки файла"
    branch_end = source.find("\n        ):", branch_start, at)
    assert branch_end >= 0, "не найдено условие ветки поздней сборки файла"
    guard = source[branch_start:branch_end]
    assert "not context.asked_for_an_archive" in guard, "выдумка снова заменяет архив"


@pytest.mark.anyio
async def test_the_same_archive_does_not_arrive_twice() -> None:
    """Замерено на живом экземпляре 2026-08-03: человек получил ДВА одинаковых архива.

    Собрала предварительная сборка, а потом модель позвала `collect_files` сама.
    Сказать ей «уже собрано» мало — решение звать инструмент остаётся её.
    """
    from friday.agent_runtime import AgentContext, AgentRuntime

    class _Kernel:
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            class _Result:
                success = True
                data = {"filename": "Д.zip", "files_in_archive": 3, "days": ["2026-07-26"]}
                attachment = {"filename": "Д.zip", "content_base64": "UEs="}

                def to_llm_message(self) -> str:
                    return "Архив: 3 файла."

            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    # Разбор остатка зовёт модель. Прибор без неё падал бы `AttributeError`,
    # то есть проверял бы не то, что работает у человека.
    runtime.llm = _NoLLM()
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=("файл", "26"))
    tools = [
        {"function": {"name": "collect_files"}},
        {"function": {"name": "make_file"}},
    ]
    bound = AgentRuntime._prefetch_the_archive_if_asked.__get__(runtime, AgentRuntime)

    await bound(context, None, [], [], [], [], tools)

    names = [str((tool.get("function") or {}).get("name")) for tool in tools]
    assert "collect_files" not in names, "модель может собрать второй такой же архив"
    assert "make_file" in names, "остальные инструменты трогать не за чем"


def test_what_did_not_fit_is_counted_by_the_code_not_the_model() -> None:
    """Замерено: модель сложила числа по-своему и сказала неправду.

    Инструмент отдавал «файлов 1671, вошли первые 160» и отдельно 140
    пропущенных имён; в ответе человеку получилось «остальные 140 не
    поместились», хотя не вошло 1511. Там, где ошибка в сложении меняет смысл,
    считать должен код.
    """
    import inspect

    from friday.execution_kernel import ExecutionKernel

    source = inspect.getsource(ExecutionKernel._collect_files)
    assert "missed = total - packed_count" in source, "разность снова считает модель"
    assert "не вошло {missed}" in source


@pytest.mark.anyio
async def test_the_successful_notice_reads_as_an_answer() -> None:
    """Текст о собранном уходит человеку НАПРЯМУЮ и должен читаться как ответ.

    Прежняя редакция проверяла ТЕКСТ ИСХОДНИКА между двумя строками — приём,
    который на этом проекте уже краснел от переписанного комментария и молчал бы
    при переименовании переменной. Здесь проверяется произведённый текст: он и
    есть то, что увидит человек.

    Требования те же, что у отказа в правах, и по той же причине. Никаких
    указаний самой себе — раньше служебная строка кончалась словами «Скажи это
    человеку прямо и не обещай файл», и она уехала человеку целиком. Никакого
    будущего времени — «Собираю архив… сейчас выгружу» при уже приложенном файле
    заставляет ждать пришедшего.
    """
    from friday.agent_runtime import AgentContext, AgentRuntime

    class _Kernel:
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            class _Result:
                success = True
                data = {
                    "filename": "Документы.zip",
                    "files_in_archive": 4,
                    "days": ["2026-07-26"],
                }
                attachment = {"filename": "Документы.zip", "content_base64": "UEs="}

                def to_llm_message(self) -> str:
                    return "Архив: 4 файла."

            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    # Разбор остатка зовёт модель. Прибор без неё падал бы `AttributeError`,
    # то есть проверял бы не то, что работает у человека.
    runtime.llm = _NoLLM()
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=("файл", "26"))
    bound = AgentRuntime._prefetch_the_archive_if_asked.__get__(runtime, AgentRuntime)

    await bound(context, None, [], [], [], [])

    said = context.structural_answer
    assert said, "о собранном архиве человеку не сказали ничего"
    lowered = said.casefold()
    for imperative in ("скажи", "не обещай", "переспроси", "ответь"):
        assert imperative not in lowered, f"указание самой себе уедет человеку: {imperative}"
    for promise in ("соберу", "сейчас ", "выгружу", "займёт"):
        assert promise not in lowered, f"обещание вместо факта: {promise}"


@pytest.mark.parametrize(
    "verdict",
    [("файл", None), ("архив", "10,13"), ("человек", "Пегас"), None],
)
@pytest.mark.anyio
async def test_without_days_nothing_is_packed(verdict) -> None:
    """«Сделай справку в word» — тоже вид «файл», но паковать там нечего.

    Пустое поле «дни» и есть та граница, которая отделяет «сочини документ» от
    «отдай присланное».
    """
    from friday.agent_runtime import AgentContext, AgentRuntime

    calls: list[str] = []

    class _Kernel:
        async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
            calls.append(tool)
            raise AssertionError("сборка запустилась там, где дней не называли")

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    # Разбор остатка зовёт модель. Прибор без неё падал бы `AttributeError`,
    # то есть проверял бы не то, что работает у человека.
    runtime.llm = _NoLLM()
    context = AgentContext(conversation_id="c", user_id="u", outward_verdict=verdict)
    bound = AgentRuntime._prefetch_the_archive_if_asked.__get__(runtime, AgentRuntime)

    assert await bound(context, None, [], [], [], []) is False
    assert calls == []


def test_the_assembly_runs_before_the_model_speaks() -> None:
    """Проверяется подключённое: сборка стоит в цикле, ДО хода модели.

    Стояла после ответа — и модель называла даты из реплики человека вместо
    собранных. Порядок здесь и есть исправление.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    loop = inspect.getsource(AgentRuntime._agentic_loop)
    assert "_prefetch_the_archive_if_asked(" in loop, "сборку никто не зовёт"
    chat = inspect.getsource(AgentRuntime.chat)
    assert "_prefetch_the_archive_if_asked(" not in chat, "сборка снова после ответа"


@pytest.mark.anyio
async def test_the_days_survive_the_trip_from_the_model() -> None:
    """Разбор ответа арбитра, а не готовый вердикт.

    Первая редакция проверяла только сборку по уже готовому «10,13,25» — и
    мутация «не разбирать поле дни» её НЕ уронила: путь от JSON модели до
    вердикта оставался непроверенным.
    """
    from friday.agent_runtime import AgentRuntime

    class _LLM:
        enabled = True

        async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
            return {"content": '{"вид": "файл", "запрос": "", "кто": "", "дни": ["10", "13", "25"]}'}

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.llm = _LLM()
    runtime.settings = None
    bound = AgentRuntime._web_query_by_arbiter.__get__(runtime, AgentRuntime)

    kind, payload = await bound("собери документы за 10, 13 и 25 число")

    assert kind.startswith("файл"), kind
    assert payload == "10,13,25", payload


def test_the_arbiter_is_asked_about_days() -> None:
    """Поле, которого нет в промпте, модель не вернёт."""
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert '"дни"' in source, "про дни арбитра не спрашивают"
    assert "три дня, а не отрезок" in source, "арбитр снова может понять список как диапазон"


def test_the_tool_is_offered_to_the_model() -> None:
    """Механизм, который никто не зовёт, работой не является."""
    import inspect

    source = inspect.getsource(ExecutionKernel._register_specs)
    assert '"collect_files"' in source, "инструмент не зарегистрирован"
    assert "collect_files" in ExecutionKernel._RELEVANT_TOOLS["архив"]
    assert "collect_files" in ExecutionKernel._RELEVANT_TOOLS["файл"]
