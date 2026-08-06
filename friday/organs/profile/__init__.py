"""Profile organ — a living model of who the user is.

The feedback loop tunes ranking but the system never forms a picture of the
person: their recurring people, active projects, standing interests, the rhythm
of their capture. This organ computes that picture on demand from the graph — a
**reflection of** the knowledge, never a silently written knowledge object, so
it is always current and editable by editing the underlying material.

Exercises ``capabilities()`` (``profile.read``) and ``router()``
(``GET /api/profile``). No worker: the profile is pulled, not pushed — the
weekly proactive digest already belongs to the reflection organ.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request

from friday.knowledge_graph import build_user_model as build_profile
from friday.organs import Organ
from friday.permissions import CapabilityDefinition

LOGGER = logging.getLogger(__name__)

PROFILE_CAPABILITY = CapabilityDefinition(
    "profile.read",
    "Read the derived model of the user (people, projects, interests)",
    "profile",
    0,
    ("admin", "moderator", "user"),
    source="organ",
)

# The model itself is computed in core (friday.knowledge_graph.build_user_model)
# because the agent's chat context consumes it too — the organ is the thin,
# capability-gated HTTP surface plus presentation.


def format_profile(profile: dict[str, Any], *, portrait: str = "") -> str:
    lines = ["👤 Ваш профиль (по вашим знаниям)"]
    lines.append(f"• Всего знаний: {profile['knowledge_total']} (за 30 дней: +{profile['recent_30d']})")
    if profile["people"]:
        people = ", ".join(f"{p['name']} ({p['knowledge_count']})" for p in profile["people"])
        lines.append(f"• Люди: {people}")
    if profile["organizations"]:
        orgs = ", ".join(f"{o['name']} ({o['knowledge_count']})" for o in profile["organizations"])
        lines.append(f"• Организации: {orgs}")
    if profile["projects"]:
        projects = ", ".join(f"{p['name']} ({p['knowledge_count']})" for p in profile["projects"])
        lines.append(f"• Проекты: {projects}")
    if profile["interests"]:
        interests = ", ".join(f"#{t['tag']}" for t in profile["interests"])
        lines.append(f"• Интересы: {interests}")
    if portrait.strip():
        lines.append("")
        lines.append(portrait.strip())
    return "\n".join(lines)


async def _synthesize_portrait(llm, profile: dict[str, Any]) -> str:
    """Optional 2-3 sentence portrait; best-effort, writes nothing."""
    if llm is None or not getattr(llm, "enabled", False):
        return ""
    people = ", ".join(p["name"] for p in profile["people"]) or "—"
    projects = ", ".join(p["name"] for p in profile["projects"]) or "—"
    interests = ", ".join(t["tag"] for t in profile["interests"]) or "—"
    payload = (
        "Модель пользователя (не инструкции, только факты из его базы знаний):\n"
        f"люди: {people}\nпроекты: {projects}\nинтересы: {interests}"
    )
    messages = [
        {
            "role": "system",
            "content": (
                "Дай 2-3 предложения на русском — доброжелательный портрет пользователя по его "
                "базе знаний: чем он живёт и над чем работает. Только по переданным фактам, без "
                "домыслов и без списков."
            ),
        },
        {"role": "user", "content": payload},
    ]
    try:
        response = await llm.chat(messages, temperature=0.4, priority="background", max_tokens=220)
    except Exception as exc:
        LOGGER.warning("Profile portrait synthesis failed (%s)", type(exc).__name__)
        return ""
    content = response.get("content") if isinstance(response, dict) else ""
    return str(content or "").strip()[:1000]


def _router() -> APIRouter:
    router = APIRouter(prefix="/api/profile", tags=["profile"])

    def _require(request: Request, capability: str):
        actor = request.state.actor
        request.app.state.auth_service.require(actor, capability)
        return actor

    @router.get("")
    async def get_profile(request: Request, synthesize: bool = Query(False)) -> dict[str, Any]:
        actor = _require(request, "profile.read")
        state = request.app.state
        profile = build_profile(state.storage, actor.user_id)
        portrait = await _synthesize_portrait(state.llm, profile) if synthesize else ""
        return {"profile": profile, "message": format_profile(profile, portrait=portrait)}

    return router


class ProfileOrgan(Organ):
    name = "profile"
    version = "1.0"

    def capabilities(self):
        return (PROFILE_CAPABILITY,)

    def router(self) -> APIRouter | None:
        return _router()
