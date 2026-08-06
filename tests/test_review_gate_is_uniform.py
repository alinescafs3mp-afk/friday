"""Четыре из семи точек входа шли мимо ревью, и это нельзя было увидеть из настройки.

`ingestion_strict_review` читался ровно в одном месте — текстовом. `ingest_file`
знал только про `force_review`, отсутствие текста и vision, так что «строгий
режим» на деле означал «строгий к набранному руками, любой к стостраничному
docx». Замер на стенде: 342 документа из 344 стали каноническим знанием, не
будучи просмотренными.

Тест, который ищет имя настройки регуляркой по коду (`test_inert_settings`),
этого НЕ ловит: имя встречалось, настройка была «живая», а половина конвейера её
не читала. Поэтому здесь матрица «точка входа × политика», и утверждение всегда
одно и то же — появился канонический объект или нет.

Плюс структурный переучёт: множество мест, где вообще вызывается `ingest_*`,
закреплено списком. Новая точка входа роняет тест и заставляет автора назвать
политику для своего пути, а не унаследовать молчание.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import json
import pathlib
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from friday.config import REVIEW_POLICIES
from friday.security import sign_bridge_request
from friday.server import create_app

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Материал, который классификатор уверенно относит к знанию: иначе ветка политики
# не исполняется вовсе и тест зеленеет на любом коде.
FACT = "Сервер Atlas работает на Ubuntu 24.04 и обслуживает внутренний реестр компании."


def _client(settings, policy: str):
    return create_app(dataclasses.replace(settings, ingestion_review_policy=policy))


def _knowledge_count(app, user_id: str) -> int:
    return app.state.storage.count_knowledge_objects(user_id)


def _owner(settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _me(client, headers) -> str:
    return client.get("/api/admin/users", headers=headers).json()["items"][0]["id"]


def _telegram_post(client, settings, payload: dict) -> object:
    path = "/api/chat"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": "5001",
        "X-Friday-Chat": "5001",
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            settings.telegram_bridge_secret,
            timestamp=timestamp,
            method="POST",
            path=path,
            external_user_id="5001",
            chat_id="5001",
            nonce=nonce,
            body=body,
        ),
    }
    return client.post(path, content=body, headers=headers)


# --- матрица: точка входа × политика ---------------------------------------


@pytest.mark.parametrize("policy", REVIEW_POLICIES)
def test_pasted_text_follows_the_policy(settings, policy):
    app = _client(settings, policy)
    with TestClient(app) as client:
        headers = _owner(settings)
        user_id = _me(client, headers)
        response = client.post("/api/ingest", json={"content": FACT, "source_ref": "t:1"}, headers=headers)
        assert response.status_code == 200, response.text

        promoted = _knowledge_count(app, user_id) > 0
        assert promoted is (policy == "assessed"), (
            f"политика {policy}: вставленный текст {'продвинулся' if promoted else 'ушёл в Inbox'}"
        )


@pytest.mark.parametrize("policy", REVIEW_POLICIES)
def test_telegram_attachment_and_direct_upload_follow_the_same_policy(settings, policy):
    """Обе дороги файла обязаны дать один и тот же ответ политики.

    Файл обязан нести извлекаемый текст: без него ветка `not
    extraction_succeeded` отправит его в Inbox при любой политике, и тест
    позеленел бы, ничего не проверив.
    """
    app = _client(settings, policy)
    with TestClient(app) as client:
        headers = _owner(settings)

        async def capture_chat(user_id: str, *args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            conversation = app.state.storage.create_conversation(user_id, "synthetic review policy")
            return {"conversation_id": conversation["id"], "content": "ok"}

        app.state.agent.chat = capture_chat
        direct = client.post(
            "/api/files",
            files={"file": ("direct-policy.txt", FACT.encode("utf-8"), "text/plain")},
            headers=headers,
        )
        assert direct.status_code == 200, direct.text

        telegram = _telegram_post(
            client,
            settings,
            {
                "message": "Что написано в синтетическом файле?",
                "source_ref": "telegram-review-policy:1",
                "telegram_message_id": 1,
                "telegram_user": {"id": 5001},
                "document": {
                    "filename": "telegram-policy.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(FACT.encode("utf-8")).decode("ascii"),
                },
            },
        )
        assert telegram.status_code == 200, telegram.text

        direct_result = direct.json()
        telegram_result = telegram.json()["file_ingestion"]
        expected_promotion = policy == "assessed"
        assert direct_result["promoted"] is expected_promotion
        assert telegram_result["promoted"] is expected_promotion
        assert direct_result["queued_for_review"] is telegram_result["queued_for_review"]
        if not expected_promotion:
            assert direct_result["queued_for_review"] is True

        total_knowledge = app.state.storage.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE deleted_at IS NULL"
        ).fetchone()[0]
        assert total_knowledge == (2 if expected_promotion else 0)


@pytest.mark.parametrize("policy", REVIEW_POLICIES)
def test_an_explicit_save_still_promotes_unless_everything_is_reviewed(settings, policy):
    """`force_knowledge` — это решение человека, и `unless_explicit` его уважает."""
    app = _client(settings, policy)
    with TestClient(app) as client:
        headers = _owner(settings)
        user_id = _me(client, headers)
        client.post(
            "/api/ingest",
            json={"content": FACT, "source_ref": "t:2", "force_knowledge": True},
            headers=headers,
        )

        promoted = _knowledge_count(app, user_id) > 0
        assert promoted is (policy != "always"), (
            f"политика {policy}: явное сохранение {'продвинулось' if promoted else 'ушло в Inbox'}"
        )


@pytest.mark.parametrize("policy", REVIEW_POLICIES)
def test_force_review_is_a_floor_no_policy_can_lower(settings, policy):
    """Массовый импорт ждёт человека при любой политике.

    «Указать на папку» — одно действие, а файлов в ней сотни: разрешив политике
    снимать `force_review`, мы дали бы одному клику канонизировать всё.
    """
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph

    tuned = dataclasses.replace(settings, ingestion_review_policy=policy)
    app = _client(settings, policy)
    with TestClient(app) as client:  # noqa: F841 — нужен только инициализированный storage
        storage = app.state.storage
        storage.ensure_user("alice")
        pipeline = IngestionPipeline(tuned, storage, KnowledgeGraph(storage))
        import asyncio

        result = asyncio.run(pipeline.ingest_text("alice", FACT, source_ref="bulk:1", force_review=True))
        assert result["promoted"] is False
        assert result["queued_for_review"] is True
        assert storage.count_knowledge_objects("alice") == 0


def test_an_unknown_policy_is_refused_by_name(monkeypatch):
    from friday.config import _choice_env

    monkeypatch.setenv("FRIDAY_INGESTION_REVIEW_POLICY", "строго")
    with pytest.raises(ValueError, match="Unknown FRIDAY_INGESTION_REVIEW_POLICY"):
        _choice_env("FRIDAY_INGESTION_REVIEW_POLICY", "unless_explicit", REVIEW_POLICIES)


def test_clean_environment_defaults_to_review_unless_explicit(tmp_path, monkeypatch):
    """Новая установка не должна канонизировать материал без человека."""
    from friday.config import load_settings

    monkeypatch.setenv("FRIDAY_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "clean-home"))
    monkeypatch.delenv("FRIDAY_INGESTION_REVIEW_POLICY", raising=False)
    monkeypatch.delenv("JERICHO_INGESTION_REVIEW_POLICY", raising=False)

    assert load_settings().ingestion_review_policy == "unless_explicit"


# --- структурный переучёт --------------------------------------------------

# Каждый вызов `ingest_text`/`ingest_file` в дереве, с ответом «а что с ревью».
# Список — не украшение: пока он был в голове, четыре точки входа молча
# унаследовали «мимо ревью».
# `force_review` записан ЗДЕСЬ, а не только в коде: это и есть то решение, которое
# каждая точка входа обязана принять явно. `False` значит «подчиняется политике».
EXPECTED_INGEST_CALLS = {
    ("friday/api/files.py", "ingest_file", False),  # загрузка файла — по политике
    ("friday/api/ingest.py", "ingest_text", False),  # вставленный текст — по политике
    ("friday/api/ingest.py", "ingest_text", True),  # /ingest/url — веб-страницу никто не читал
    ("friday/bulk_import.py", "ingest_file", True),  # папка — одно действие, файлов сотни
    # Страницы, найденные поиском Пятницы: их не читал ни один человек, а нашла
    # их модель по своему запросу — тем более через ревью, как и `/ingest/url`.
    ("friday/execution_kernel/__init__.py", "ingest_text", True),
    ("friday/organs/importer/__init__.py", "ingest_text", True),  # импортёр — то же самое
    ("friday/server.py", "ingest_file", False),  # вложение в чате — по политике
    ("friday/server.py", "ingest_text", False),  # сообщение в чате — по политике
}


def _ingest_call_sites() -> set[tuple[str, str, bool]]:
    """Каждый вызов, и передаёт ли он `force_review=True` литералом.

    Именно передачу литерала, а не наличие имени: `force_review=flag` означает,
    что решение принято где-то ещё, и такой вызов обязан быть замечен.
    """
    found: set[tuple[str, str, bool]] = set()
    for path in (ROOT / "friday").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"ingest_text", "ingest_file"}:
                continue
            forced = any(
                keyword.arg == "force_review"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            found.add((str(path.relative_to(ROOT)), name, forced))
    return found


def test_every_place_that_ingests_has_been_given_a_policy():
    actual = _ingest_call_sites()
    new = sorted(actual - EXPECTED_INGEST_CALLS)
    gone = sorted(EXPECTED_INGEST_CALLS - actual)
    assert not new, (
        f"новая или изменившаяся точка поступления материала: {new} — решите, как она "
        "относится к ingestion_review_policy, и внесите её в список (молчание здесь "
        "означает «мимо ревью»)"
    )
    assert not gone, f"точка поступления исчезла или сменила режим, список устарел: {gone}"


def test_the_policy_is_read_by_both_paths_not_just_one():
    """Ровно тот дефект, который тест на «мёртвые настройки» пропускал.

    Он ищет имя поля регуляркой по любому `.py`, поэтому был зелёным, пока
    политику читал один текстовый путь. Здесь требуется, чтобы решение принимал
    ОДИН предикат и чтобы обе ветки звали именно его.
    """
    core = (ROOT / "friday" / "ingestion" / "_core.py").read_text(encoding="utf-8")
    assert "def review_required" in core
    assert "ingestion_review_policy" in core

    for module in ("_capture.py", "_files.py"):
        source = (ROOT / "friday" / "ingestion" / module).read_text(encoding="utf-8")
        assert "self.review_required(" in source, f"{module} решает судьбу поступления сам по себе"
        assert "ingestion_review_policy" not in source, (
            f"{module} читает политику напрямую — это вторая реализация одного правила"
        )
