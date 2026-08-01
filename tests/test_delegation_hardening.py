"""Service-level delegation checks + fail-closed kernel — §21.

The "nobody delegates beyond their own authority" invariant lived only in the
HTTP admin layer, so any non-HTTP caller of AuthorizationService could grant
arbitrary capabilities; and ExecutionKernel(authorization=None) silently ran
every tool with zero capability checks.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.permissions import (
    ActorContext,
    AuthorizationError,
    AuthorizationService,
)


def _service(storage) -> AuthorizationService:
    return AuthorizationService(storage)


def _moderator(storage) -> ActorContext:
    storage.ensure_user("local:mod", preset_key="moderator")
    return ActorContext("local:mod", "moderator", "test")


def test_issuing_a_token_is_delegation_too(storage):
    """A token IS the target account's whole authority, handed over in one step.

    `_require_delegable` guards `grant_permission`, `set_user_preset` and custom
    presets — the narrow delegations. Minting an API token for another account
    was the broad one and carried no invariant at all: the only barrier
    recognised the legacy owner id and the `owner` preset, so an ordinary account
    holding one capability above the administrator's own (`code.run`, which no
    preset but `owner` grants) slipped through. Print a token, authenticate as
    them, keep the difference.
    """
    service = _service(storage)
    moderator = _moderator(storage)
    storage.ensure_user("local:tooled", preset_key="user")
    service.grant_permission("local:tooled", "code.run")

    with pytest.raises(AuthorizationError):
        service.require_delegable_account(moderator, "local:tooled")

    # An account strictly within the moderator's own authority is fine.
    storage.ensure_user("local:plain", preset_key="guest")
    service.require_delegable_account(moderator, "local:plain")

    # And the owner delegates whatever they like — it is their authority.
    service.require_delegable_account(None, "local:tooled")


def test_effective_capabilities_follow_the_overrides(storage):
    service = _service(storage)
    storage.ensure_user("local:mixed", preset_key="guest")
    baseline = service.effective_capabilities("local:mixed")

    service.grant_permission("local:mixed", "code.run")
    assert "code.run" in service.effective_capabilities("local:mixed")

    denied = next(iter(baseline))
    service.deny_permission("local:mixed", denied)
    assert denied not in service.effective_capabilities("local:mixed")


def test_grant_permission_enforces_delegation_at_service_level(storage):
    service = _service(storage)
    moderator = _moderator(storage)
    storage.ensure_user("local:kate")

    with pytest.raises(AuthorizationError, match="Cannot delegate"):
        service.grant_permission("local:kate", "admin.data.purge", acting_actor=moderator)

    owner = ActorContext("local:boss", "owner", "test")
    storage.ensure_user("local:boss", preset_key="owner")
    service.grant_permission("local:kate", "admin.data.purge", acting_actor=owner)
    assert storage.get_permission_overrides("local:kate").get("admin.data.purge") == "allow"

    # Removing authority needs no delegation: a moderator may deny anything.
    service.deny_permission("local:kate", "admin.data.purge")
    assert storage.get_permission_overrides("local:kate").get("admin.data.purge") == "deny"

    # Trusted internal callers (no acting actor) remain unrestricted.
    service.revoke_permission("local:kate", "admin.data.purge")
    service.grant_permission("local:kate", "admin.data.purge")


def test_preset_assignment_enforces_delegation_at_service_level(storage):
    service = _service(storage)
    moderator = _moderator(storage)
    storage.ensure_user("local:kate")

    with pytest.raises(AuthorizationError, match="owner"):
        service.set_user_preset("local:kate", "owner", acting_actor=moderator)
    # The admin preset includes capabilities a moderator does not hold.
    with pytest.raises(AuthorizationError, match="Cannot delegate"):
        service.set_user_preset("local:kate", "admin", acting_actor=moderator)

    service.set_user_preset("local:kate", "user", acting_actor=moderator)
    assert service.get_user_preset("local:kate") == "user"


def test_custom_preset_creation_enforces_delegation(storage):
    service = _service(storage)
    moderator = _moderator(storage)

    with pytest.raises(AuthorizationError, match="Cannot delegate"):
        service.create_custom_preset(
            "super",
            "Super",
            {"admin.tokens.manage"},
            created_by="local:mod",
            acting_actor=moderator,
        )

    preset = service.create_custom_preset(
        "readers",
        "Readers",
        {"kg.read"},
        created_by="local:mod",
        acting_actor=moderator,
    )
    assert preset["preset_key"] == "readers"


@pytest.mark.asyncio
async def test_kernel_without_authorization_denies_everything(settings, storage):
    kernel = ExecutionKernel(settings=settings)
    actor = ActorContext("local:mod", "owner", "test")

    assert kernel.get_tool_definitions(actor) == []
    result = await kernel.execute("memory_search", {"query": "x"}, actor=actor)
    assert result.success is False
    assert "authorization" in (result.error or "").casefold()


def test_agent_runtime_fallback_kernel_is_authorized(settings, storage):
    from friday.agent_runtime import AgentRuntime

    runtime = AgentRuntime(settings, storage)
    assert runtime.kernel.authorization is not None
