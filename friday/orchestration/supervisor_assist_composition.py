"""Fail-closed production composition for the one promoted assist journey."""

from __future__ import annotations

from typing import Any, cast

from friday.orchestration.supervisor_assist_activation import (
    AssistPromotionActivationMaterial,
)
from friday.orchestration.supervisor_assist_controller import SupervisorAssistController
from friday.orchestration.supervisor_assist_graph_adapter import SupervisorAssistGraphAdapter
from friday.orchestration.supervisor_assist_ports import (
    AssistPromotionEvaluator,
    AttestedPrimaryModel,
    SchedulerAssistPlanner,
    SchedulerAssistReviewer,
)
from friday.orchestration.supervisor_assist_production import (
    AssistConversationModeReader,
    CurrentTurnAssistFileEvidenceReader,
    SupervisorAssistActorBinding,
    SupervisorAssistAuthorityGate,
    SupervisorPromotedProductObserver,
    supervisor_assist_read_only_effect_gate,
)
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
    SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
)
from friday.orchestration.supervisor_assist_runtime import SemanticSupervisorAssistRuntime
from friday.orchestration.supervisor_contracts import SupervisorMode
from friday.orchestration.transient_web_comparison import TransientWebComparisonAdapter
from friday.permissions import AuthorizationService


def build_supervisor_assist_production_runtime(
    *,
    settings: object,
    primary: Any,
    storage: Any,
    authorization: AuthorizationService,
    scheduler: Any,
    activation_material: AssistPromotionActivationMaterial | None,
    graph_adapter: SupervisorAssistGraphAdapter,
    web: Any,
    primary_model_runtime: Any,
) -> SemanticSupervisorAssistRuntime | None:
    """Build authority only for the exact evidence-bound P4 configuration."""

    requested = SupervisorMode.fail_closed(
        getattr(settings, "semantic_supervisor_mode", SupervisorMode.OFF.value)
    )
    if type(activation_material) is not AssistPromotionActivationMaterial:
        return None
    material = cast(AssistPromotionActivationMaterial, activation_material)
    if (
        requested not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}
        or not material.configured
        or material.requested_mode is not requested
        or tuple(getattr(settings, "semantic_supervisor_tasks", ()))
        != ("compare_current_file_with_current_web",)
        or getattr(settings, "semantic_supervisor_max_steps", None)
        != SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS
        or getattr(settings, "semantic_supervisor_max_review_rounds", None)
        != SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS
        or not isinstance(authorization, AuthorizationService)
        or type(graph_adapter) is not SupervisorAssistGraphAdapter
        or primary_model_runtime is None
    ):
        return None

    runtime_settings = cast(Any, settings)
    promotion = AssistPromotionEvaluator(material, scheduler)
    actor_binding = SupervisorAssistActorBinding(storage)
    observer = SupervisorPromotedProductObserver(
        storage=storage,
        promotion_evaluator=promotion,
        actor_binding=actor_binding,
    )
    controller = SupervisorAssistController(
        settings=settings,
        promotion_evaluator=promotion,
        planner=SchedulerAssistPlanner(scheduler),
        reviewer=SchedulerAssistReviewer(scheduler),
        primary_model=AttestedPrimaryModel(primary_model_runtime),
        graph_adapter=graph_adapter,
        file_reader=CurrentTurnAssistFileEvidenceReader(
            storage=storage,
            authorization=authorization,
            files_root=runtime_settings.files_dir,
            max_bytes=runtime_settings.max_upload_bytes,
        ),
        web_reader=TransientWebComparisonAdapter(authorization, web),
        canary_actor_binding=actor_binding,
        authority_check=SupervisorAssistAuthorityGate(storage, authorization),
        effect_check=supervisor_assist_read_only_effect_gate,
        post_commit_observer=observer,
        max_review_rounds=SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
    )
    return SemanticSupervisorAssistRuntime(
        settings=settings,
        primary=primary,
        controller=controller,
        conversation_is_dialogue=AssistConversationModeReader(storage),
        ordinary_observer=observer.observe_ordinary,
    )


__all__ = ["build_supervisor_assist_production_runtime"]
