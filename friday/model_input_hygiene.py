"""Closed secret predicate for every model-visible V12 projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from friday.secret_hygiene import named_secrets
from friday.telemetry.logging import redact_friday_api_tokens


def model_visible_text_is_secret_free(value: object) -> bool:
    """Reject structural Friday tokens and every currently configured secret."""

    if not isinstance(value, str):
        return False
    secrets = tuple(secret for secret in named_secrets().values() if secret)
    return bool(redact_friday_api_tokens(value) == value and not any(secret in value for secret in secrets))


def model_messages_are_secret_free(messages: Sequence[Mapping[str, object]]) -> bool:
    """Validate only the content projection actually sent to the model."""

    return all(model_visible_text_is_secret_free(item.get("content")) for item in messages)


__all__ = ["model_messages_are_secret_free", "model_visible_text_is_secret_free"]
