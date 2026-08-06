"""Log privacy — §25: query strings and URLs must not leak into logs.

Search queries travel as URL parameters, so the uvicorn access log was
recording personal data verbatim; web_surfer logged httpx exception strings,
which embed full request URLs (including search-provider query parameters).
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from friday.telemetry.logging import (
    AccessLogQueryStripper,
    ExternalExceptionStripper,
    install_access_log_privacy,
    install_external_exception_privacy,
)
from friday.web_surfer import _log_safe_host

_LOGGER_LEVELS = {"debug", "info", "warning", "error", "critical", "exception"}
_PRIVATE_LOG_NAME = re.compile(
    r"(?i)(?:^|_)(?:user_id|chat_id|callback_id|filename|source_ref|display_name|query|"
    r"content|body|title|text|url|path|detail|reason|database|relative|md_file|safe_name|"
    r"exc|error|[a-z0-9_]+_id)(?:$|_)"
)
_PRIVATE_LOG_KEYS = {
    "id",
    "user_id",
    "chat_id",
    "callback_id",
    "filename",
    "source_ref",
    "display_name",
    "query",
    "content",
    "body",
    "title",
    "text",
    "url",
    "path",
    "detail",
    "reason",
    "database",
}


def _logger_calls() -> list[tuple[Path, ast.Call]]:
    root = Path(__file__).resolve().parents[1] / "friday"
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _LOGGER_LEVELS:
                continue
            owner = node.func.value
            named_logger = isinstance(owner, ast.Name) and (
                owner.id.casefold() == "log" or owner.id.casefold().endswith("logger")
            )
            inline_logger = (
                isinstance(owner, ast.Call)
                and isinstance(owner.func, ast.Attribute)
                and owner.func.attr == "getLogger"
                and isinstance(owner.func.value, ast.Name)
                and owner.func.value.id == "logging"
            )
            if not named_logger and not inline_logger:
                continue
            found.append((path, node))
    return found


def _is_safe_private_derivative(node: ast.AST) -> bool:
    # Exception CLASS is diagnostic signal; message and traceback are not.
    if isinstance(node, ast.Attribute) and node.attr == "__name__":
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "type"
            and len(call.args) == 1
        ):
            return True
    # Length discloses only an already-bounded count, never content.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        return True
    # This helper deliberately returns hostname only; path/query/userinfo are dropped.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_log_safe_host":
        return True
    return isinstance(node, ast.Name) and node.id.startswith("safe_")


def test_application_log_calls_are_content_free_by_construction():
    violations: list[str] = []
    for path, call in _logger_calls():
        location = f"{path.relative_to(path.parents[1])}:{call.lineno}"
        if call.func.attr == "exception":
            violations.append(f"{location}: LOGGER.exception captures a traceback")
        if (
            not call.args
            or not isinstance(call.args[0], ast.Constant)
            or not isinstance(call.args[0].value, str)
        ):
            violations.append(f"{location}: log message must be a source-code literal")
        for keyword in call.keywords:
            if keyword.arg in {"exc_info", "stack_info", "extra"}:
                violations.append(f"{location}: forbidden logging keyword {keyword.arg}")
        for argument in call.args[1:]:
            if _is_safe_private_derivative(argument):
                continue
            for item in ast.walk(argument):
                if isinstance(item, ast.Name) and _PRIVATE_LOG_NAME.search(item.id):
                    violations.append(f"{location}: raw private argument {item.id}")
                    break
                if isinstance(item, ast.Attribute) and _PRIVATE_LOG_NAME.search(item.attr):
                    violations.append(f"{location}: raw private attribute {item.attr}")
                    break
                if (
                    isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                    and item.value in _PRIVATE_LOG_KEYS
                ):
                    violations.append(f"{location}: raw private mapping key {item.value}")
                    break
    assert not violations, "\n".join(violations)


def _access_record(path: str, *, method: str = "GET") -> logging.LogRecord:
    # Mirrors uvicorn.access's record shape: ('%s - "%s %s HTTP/%s" %d', 5 args).
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", method, path, "1.1", 200),
        exc_info=None,
    )


def test_access_log_filter_strips_query_strings():
    stripper = AccessLogQueryStripper()

    record = _access_record(
        "/api/search?q=%D0%BB%D0%B8%D1%87%D0%BD%D1%8B%D0%B9+%D0%B2%D0%BE%D0%BF%D1%80%D0%BE%D1%81"
    )
    assert stripper.filter(record) is True
    rendered = record.getMessage()
    assert "q=" not in rendered
    assert "/api/search?[stripped]" in rendered

    plain = _access_record("/api/health")
    stripper.filter(plain)
    assert "/api/health" in plain.getMessage()


def test_access_log_filter_strips_client_and_private_path_segments():
    sentinel = "SYNTHETIC_PRIVATE_PERSON_ID"
    record = _access_record(f"/api/admin/users/{sentinel}?q=private")

    assert AccessLogQueryStripper().filter(record) is True
    rendered = record.getMessage()
    assert sentinel not in rendered
    assert "q=" not in rendered
    assert "127.0.0.1" not in rendered
    assert "/api/admin/[...]?[stripped]" in rendered


def test_access_log_filter_does_not_trust_an_unknown_route_family():
    sentinel = "SYNTHETIC_PRIVATE_ROUTE_FAMILY"
    stripper = AccessLogQueryStripper()

    api_record = _access_record(f"/api/{sentinel}")
    root_record = _access_record(f"/{sentinel}/child")
    assert stripper.filter(api_record) is True
    assert stripper.filter(root_record) is True

    rendered = api_record.getMessage() + root_record.getMessage()
    assert sentinel not in rendered
    assert "/api/<unknown>" in rendered
    assert "/<unknown>/[...]" in rendered


def test_access_log_filter_does_not_trust_an_extension_method():
    sentinel = "SYNTHETIC_PRIVATE_METHOD_SENTINEL"
    record = _access_record("/api/health", method=sentinel)

    assert AccessLogQueryStripper().filter(record) is True
    rendered = record.getMessage()
    assert sentinel not in rendered
    assert "<unknown-method> /api/health" in rendered


def test_install_access_log_privacy_is_idempotent():
    logger = logging.getLogger("uvicorn.access")
    before = list(logger.filters)
    try:
        install_access_log_privacy()
        install_access_log_privacy()
        added = [f for f in logger.filters if isinstance(f, AccessLogQueryStripper)]
        assert len(added) == 1
    finally:
        logger.filters = before


def test_uvicorn_exception_filter_keeps_class_but_drops_message_and_traceback():
    sentinel = "SYNTHETIC_PRIVATE_ASGI_EXCEPTION_" + "x" * 5_000
    try:
        raise ValueError(sentinel)
    except ValueError:
        exc_info = __import__("sys").exc_info()
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Exception in ASGI application",
        args=(),
        exc_info=exc_info,
    )
    record.stack_info = sentinel

    assert ExternalExceptionStripper().filter(record) is True
    assert record.getMessage() == "ASGI application failed (ValueError)"
    assert sentinel not in record.getMessage()
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None


def test_uvicorn_exception_filter_does_not_trust_a_dynamic_class_name():
    sentinel = "SYNTHETIC_PRIVATE_EXCEPTION_CLASS"
    hostile_type = type(sentinel, (Exception,), {})
    hostile = hostile_type("private message")
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Exception in ASGI application",
        args=(),
        exc_info=(hostile_type, hostile, None),
    )

    assert ExternalExceptionStripper().filter(record) is True
    assert record.getMessage() == "ASGI application failed (Exception)"
    assert sentinel not in record.getMessage()


def test_install_external_exception_privacy_is_idempotent():
    logger = logging.getLogger("uvicorn.error")
    before = list(logger.filters)
    try:
        install_external_exception_privacy()
        install_external_exception_privacy()
        added = [item for item in logger.filters if isinstance(item, ExternalExceptionStripper)]
        assert len(added) == 1
    finally:
        logger.filters = before


def test_log_safe_host_drops_path_and_query():
    assert _log_safe_host("https://example.com/search?q=секрет") == "example.com"
    assert _log_safe_host("https://api.host:8443/v1?key=abc") == "api.host"
    assert _log_safe_host("https://user:password@api.host/private") == "api.host"
    assert _log_safe_host("not a url") == "<invalid-url>"


def test_query_repair_failure_logs_neither_query_nor_exception(
    storage,
    monkeypatch,
    caplog,
):
    from friday.retrieval import HybridSearcher

    private_query = "SYNTHETIC_PRIVATE_QUERY_SENTINEL_" + "q" * 5_000
    private_exception = "SYNTHETIC_PRIVATE_EXCEPTION_SENTINEL_" + private_query

    def refuse_repair(*_args, **_kwargs):
        raise RuntimeError(private_exception)

    monkeypatch.setattr("friday.retrieval.repair_query", refuse_repair)
    with caplog.at_level(logging.DEBUG, logger="friday.retrieval"):
        result = HybridSearcher(storage, record_usage=False)._repair_query(
            "synthetic-user",
            private_query,
        )

    assert result is None
    assert caplog.records
    for record in caplog.records:
        assert private_query not in record.getMessage()
        assert private_exception not in record.getMessage()
        assert record.exc_info is None


@pytest.mark.asyncio
async def test_web_fetch_failure_logs_host_and_type_only(settings, caplog):
    from friday.web_surfer import WebSurfer

    surfer = WebSurfer(settings)
    try:
        with caplog.at_level(logging.DEBUG, logger="friday.web_surfer"):
            result = await surfer.fetch("https://192.0.2.7/private/path?token=supersecret")
        assert result.error
        for record in caplog.records:
            message = record.getMessage()
            assert "supersecret" not in message
            assert "/private/path" not in message
    finally:
        await surfer.close()
