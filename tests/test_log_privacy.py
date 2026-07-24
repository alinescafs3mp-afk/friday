"""Log privacy — §25: query strings and URLs must not leak into logs.

Search queries travel as URL parameters, so the uvicorn access log was
recording personal data verbatim; web_surfer logged httpx exception strings,
which embed full request URLs (including search-provider query parameters).
"""

from __future__ import annotations

import logging

import pytest

from jericho.telemetry.logging import AccessLogQueryStripper, install_access_log_privacy
from jericho.web_surfer import _log_safe_host


def _access_record(path: str) -> logging.LogRecord:
    # Mirrors uvicorn.access's record shape: ('%s - "%s %s HTTP/%s" %d', 5 args).
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1", "GET", path, "1.1", 200),
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


def test_log_safe_host_drops_path_and_query():
    assert _log_safe_host("https://example.com/search?q=секрет") == "example.com"
    assert _log_safe_host("https://api.host:8443/v1?key=abc") == "api.host:8443"
    assert _log_safe_host("not a url") == "<invalid-url>"


@pytest.mark.asyncio
async def test_web_fetch_failure_logs_host_and_type_only(settings, caplog):
    from jericho.web_surfer import WebSurfer

    surfer = WebSurfer(settings)
    try:
        with caplog.at_level(logging.DEBUG, logger="jericho.web_surfer"):
            result = await surfer.fetch("https://192.0.2.7/private/path?token=supersecret")
        assert result.error
        for record in caplog.records:
            message = record.getMessage()
            assert "supersecret" not in message
            assert "/private/path" not in message
    finally:
        await surfer.close()
