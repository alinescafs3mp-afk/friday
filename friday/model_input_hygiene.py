"""Closed secret predicate for every model-visible V12 projection."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from friday.secret_hygiene import named_secrets
from friday.telemetry.logging import redact_friday_api_tokens

_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:^|[\s{,;])[\"']?[A-Z0-9_]*(?:TOKEN|SECRET|API_KEY|PASSWORD)[A-Z0-9_]*[\"']?"
    r"\s*[:=]\s*[\"']?[^\s\"',;}{]{8,}",
    re.IGNORECASE,
)
_BEARER_CREDENTIAL = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)
_OPENAI_STYLE_CREDENTIAL = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_TELEGRAM_CREDENTIAL = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b")
_ENV_ROW = re.compile(r"(?m)^[A-Z][A-Z0-9_]{1,63}=[^\r\n]{0,4096}$")


def model_visible_text_is_secret_free(value: object) -> bool:
    """Reject structural Friday tokens and every currently configured secret."""

    if not isinstance(value, str):
        return False
    secrets = tuple(secret for secret in named_secrets().values() if secret)
    return bool(redact_friday_api_tokens(value) == value and not any(secret in value for secret in secrets))


def model_messages_are_secret_free(
    messages: Sequence[Mapping[str, object]],
    *,
    additional_secrets: Iterable[str] = (),
) -> bool:
    """Validate only the content projection actually sent to the model."""

    extra = tuple(value for value in additional_secrets if value)
    return all(
        model_visible_text_is_secret_free(content := item.get("content"))
        and isinstance(content, str)
        and not any(secret in content for secret in extra)
        for item in messages
    )


def secondary_model_messages_are_secret_free(
    messages: Sequence[Mapping[str, object]],
    *,
    additional_secrets: Iterable[str] = (),
) -> bool:
    """Fail closed on exact Friday secrets and common credential/env shapes."""

    if not model_messages_are_secret_free(messages, additional_secrets=additional_secrets):
        return False
    for item in messages:
        content = item.get("content")
        if not isinstance(content, str):
            return False
        if (
            _CREDENTIAL_ASSIGNMENT.search(content)
            or _BEARER_CREDENTIAL.search(content)
            or _OPENAI_STYLE_CREDENTIAL.search(content)
            or _TELEGRAM_CREDENTIAL.search(content)
            or "-----BEGIN PRIVATE KEY-----" in content
            or "-----BEGIN RSA PRIVATE KEY-----" in content
            or len(_ENV_ROW.findall(content)) >= 3
        ):
            return False
    return True


__all__ = [
    "model_messages_are_secret_free",
    "model_visible_text_is_secret_free",
    "secondary_model_messages_are_secret_free",
]
