"""Authority-free creation boundary for detached authenticated advisory work."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

from friday.orchestration.turn_context_runtime import suspend_authenticated_turn_context


@contextmanager
def suspend_authenticated_advisory_authority() -> Iterator[None]:
    """Clear turn, request-effect, and publication ContextVars as one seam.

    Imports stay local to avoid coupling the lightweight orchestration import
    graph to the execution kernel during module initialization.
    """

    from friday.execution_kernel import suspend_request_effect_authority_for_advisory
    from friday.orchestration.turn_context_publication import (
        suspend_authenticated_turn_publication_for_advisory,
    )

    with ExitStack() as stack:
        # Suspend the turn first so effect/publication values are restored on
        # exit while primary authority is still unavailable.  No task or
        # callback is created until all three scopes have entered.
        stack.enter_context(suspend_authenticated_turn_context())
        stack.enter_context(suspend_request_effect_authority_for_advisory())
        stack.enter_context(suspend_authenticated_turn_publication_for_advisory())
        yield


__all__ = ["suspend_authenticated_advisory_authority"]
