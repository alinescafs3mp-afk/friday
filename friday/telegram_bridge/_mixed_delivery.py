"""Fresh backend authority before delivering a file/archive mixed answer."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import quote

import httpx

from friday.orchestration.mixed_file_archive_web_query import mixed_file_archive_web_cues_present
from friday.organs.mixed_journey.observe import MIXED_SOURCE_FACTS
from friday.telegram_bridge._base import PermanentUpdateError


async def require_mixed_delivery_authority(
    bridge: Any,
    backend: httpx.AsyncClient,
    response: dict[str, Any],
    *,
    external_user_id: str,
    chat_id: int,
    from_cache: bool = False,
    require_durable_identity: bool = False,
    request_message: str = "",
) -> None:
    # No cache-supplied internal fact may reach the observer, including the
    # ordinary-response and missing-identity exits below.
    had_mixed_facts = MIXED_SOURCE_FACTS in response
    response.pop(MIXED_SOURCE_FACTS, None)
    context = response.get("context")
    claims_mixed = isinstance(context, dict) and context.get("answer_mode") == "mixed_file_archive_web"
    if not from_cache and not require_durable_identity and not claims_mixed and not had_mixed_facts:
        return
    message_id, message = response.get("message_id"), response.get("message")
    if type(message_id) is not str or not message_id or type(message) is not str:
        if (
            not require_durable_identity
            and not claims_mixed
            and not had_mixed_facts
            and not mixed_file_archive_web_cues_present(request_message)
        ):
            # Legacy notices without durable message identities have no mixed
            # source claim. Their internal status facts were discarded above.
            return
        raise PermanentUpdateError("Mixed answer has no durable source authority")
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    result = await bridge._backend_json(
        backend,
        "GET",
        f"/api/me/mixed-deliveries/{quote(message_id, safe='')}?answer_sha256={digest}",
        None,
        external_user_id,
        str(chat_id),
    )
    if result.get("authorized") is not True:
        # The ordinary durable dead-letter notice owns termination, including
        # existing accepted/unknown send fences. Never reset its chunk cursor
        # or replace the cached body under a cursor belonging to another text.
        raise PermanentUpdateError("Mixed answer source authority is unavailable")
    if result.get("mixed_required") is False:
        if result.get("conversation_id") != response.get("conversation_id"):
            raise PermanentUpdateError("Cached answer has no owned conversation identity")
        if claims_mixed and isinstance(context, dict):
            response["context"] = {key: value for key, value in context.items() if key != "answer_mode"}
        return
    if result.get("mixed_required") is not True:
        raise PermanentUpdateError("Cached answer has no server-owned source classification")
    facts = result.get("mixed_journey")
    if not isinstance(facts, dict) or facts.get("conversation_id") != response.get("conversation_id"):
        raise PermanentUpdateError("Mixed delivery has no authorized operation facts")
    message_format = facts.get("message_format", "plain")
    if (
        message_format not in {"plain", "markdown"}
        or response.get("message_format", "plain") != message_format
    ):
        # Rendering determines chunk boundaries. Never change it underneath a
        # delivery cursor, even when the cached body digest is still correct.
        raise PermanentUpdateError("Mixed delivery presentation has changed")
    # Replace only internal status facts with the freshly authorized server
    # view. Cached copies cannot authorize their own publication or status.
    response[MIXED_SOURCE_FACTS] = facts.get(MIXED_SOURCE_FACTS)
    response["web_research_consumption"] = facts.get("web_research_consumption")
    response["context"] = {
        **(context if isinstance(context, dict) else {}),
        "answer_mode": "mixed_file_archive_web",
    }
