from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.mixed_journey_store_projection import MixedJourneyStoreProjectionState
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
    WebResearchConsumptionV1,
)
from friday.organs.mixed_journey import mixed_status_admitted, observe_mixed_journey
from friday.telegram_bridge._status import TelegramStatusStage, render_interactive_turn_status

DIGEST = "d" * 64
TURN = "chat:1001"
JOURNEY = "chat:1001"


def _web() -> WebResearchConsumptionV1:
    return WebResearchConsumptionV1(
        "web-1",
        TURN,
        WebResearchConsumptionState.CONSUMABLE,
        "yandex",
        1,
        WebResearchConsumptionReason.PRIMARY_SOURCES,
    )


class _Storage:
    def __init__(self) -> None:
        self.raw = {
            "raw_aaaaaaaaaaaaaaaa": {
                "id": "raw_aaaaaaaaaaaaaaaa",
                "content_hash": DIGEST,
                "metadata_json": {"sha256": DIGEST, "mime_type": "application/pdf"},
            },
            "arch_dddddddddddddddd": {
                "id": "arch_dddddddddddddddd",
                "content_hash": DIGEST,
                "member_count": 2,
                "metadata_json": {"sha256": DIGEST, "mime_type": "application/zip"},
            },
        }
        self.conversations = {"conv_bbbbbbbbbbbbbbbb": {"id": "conv_bbbbbbbbbbbbbbbb"}}
        self.knowledge = {
            "ko_cccccccccccccccc": {
                "id": "ko_cccccccccccccccc",
                "knowledge_kind": "table",
                "content_hash": DIGEST,
            }
        }

    def get_raw_object(self, raw_id: str, user_id: str) -> dict[str, object] | None:
        assert user_id == "person-1"
        return self.raw.get(raw_id)

    def get_conversation(self, conversation_id: str, user_id: str) -> dict[str, object] | None:
        assert user_id == "person-1"
        return self.conversations.get(conversation_id)

    def get_knowledge_object(self, ko_id: str, user_id: str) -> dict[str, object] | None:
        assert user_id == "person-1"
        return self.knowledge.get(ko_id)


def test_empty_observation_is_empty() -> None:
    result = observe_mixed_journey(JOURNEY, TURN)
    assert result.state is MixedJourneyStoreProjectionState.EMPTY
    assert mixed_status_admitted(result) is False


def test_store_protocol_projects_mixed_organs_and_keeps_primary_owner() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        storage=_Storage(),
        user_id="person-1",
        conversation_id="conv_bbbbbbbbbbbbbbbb",
        file_ids=("raw_aaaaaaaaaaaaaaaa",),
        archive_ids=("arch_dddddddddddddddd",),
        table_ids=("ko_cccccccccccccccc",),
        web=_web(),
        engineer_current_advisories=True,
        coding_current_docs=True,
    )
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert set(result.view.organs.present_organs) == {
        "file",
        "archive",
        "conversation",
        "web",
        "table",
        "engineer",
        "coding",
    }
    assert result.view.pending_work_owner.value == "primary"
    assert result.view.restart.status == "unknown"
    assert result.view.restart.execution == "unknown"
    assert result.view.publication_admitted is False
    assert mixed_status_admitted(result) is True
    with pytest.raises(FrozenInstanceError):
        result.state = MixedJourneyStoreProjectionState.EMPTY  # type: ignore[misc]


def test_conversation_plus_file_is_projected_but_not_mixed_status() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        conversation_id="conv_bbbbbbbbbbbbbbbb",
        files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST, "filename": "report.pdf"},),
    )
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert result.view.organs.is_present("file")
    assert result.view.organs.is_present("conversation")
    assert mixed_status_admitted(result) is False


def test_file_plus_web_admits_mixed_status() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},),
        web=_web(),
    )
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert mixed_status_admitted(result) is True
    text = render_interactive_turn_status(
        TelegramStatusStage.DELIVERING_RESULT,
        9,
        mixed_projection=result,
        operation_id=JOURNEY,
        authenticated_turn_id=TURN,
    )
    assert text.startswith("⏳ Смешанный маршрут")
    assert "отправляю готовый результат" in text
    assert "Исследую вопрос" not in text
    assert "ETA" not in text


def test_backend_wait_does_not_render_mixed() -> None:
    result = observe_mixed_journey(
        JOURNEY, TURN, files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},), web=_web()
    )
    text = render_interactive_turn_status(
        TelegramStatusStage.BACKEND_WAIT,
        12,
        mixed_projection=result,
    )
    assert "Смешанный маршрут" not in text
    assert "ядро обрабатывает запрос" in text


def test_inbound_album_keeps_document_even_with_mixed_projection() -> None:
    result = observe_mixed_journey(
        JOURNEY, TURN, files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},), web=_web()
    )
    text = render_interactive_turn_status(
        TelegramStatusStage.DELIVERING_RESULT,
        8,
        item_total=2,
        received_items=2,
        staged_items=2,
        mixed_projection=result,
    )
    assert text.startswith("⏳ Обрабатываю файлы")
    assert "Смешанный маршрут" not in text


def test_web_source_urls_are_not_fabricated_into_web_organ() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        response={
            "conversation_id": "conv_bbbbbbbbbbbbbbbb",
            "files": [{"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST, "filename": "note.pdf"}],
            "web_sources": [{"url": "https://example.com", "title": "Example"}],
        },
    )
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert result.view.organs.is_present("web") is False
    assert "example.com" not in str(result.to_mapping())
    assert mixed_status_admitted(result) is False


def test_storage_without_user_id_is_blocked() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        storage=_Storage(),
        file_ids=("raw_aaaaaaaaaaaaaaaa",),
        web=_web(),
    )
    assert result.state is MixedJourneyStoreProjectionState.BLOCKED
    assert result.view is None


def test_filename_only_unpersisted_attachment_is_empty_not_blocked() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        files=({"filename": "report.pdf", "content_base64": "aaaa"},),
        web=_web(),
    )
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert result.view.organs.is_present("file") is False
    assert mixed_status_admitted(result) is False


def test_missing_digest_stays_empty_and_is_not_invented() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        storage=_Storage(),
        user_id="person-1",
        file_ids=("raw_missingmissingm",),
        web=_web(),
    )
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert result.view.organs.is_present("file") is False
    assert result.view.organs.is_present("web") is True


def test_private_path_and_filename_identities_block_without_payload() -> None:
    path = observe_mixed_journey(
        JOURNEY, TURN, files=({"file_id": "/private/report.pdf", "sha256": DIGEST},), web=_web()
    )
    assert path.state is MixedJourneyStoreProjectionState.BLOCKED
    assert path.view is None
    assert "/private/report.pdf" not in str(path.to_mapping())
    named = observe_mixed_journey(JOURNEY, TURN, files=({"id": "report.pdf", "sha256": DIGEST},), web=_web())
    assert named.state is MixedJourneyStoreProjectionState.BLOCKED
    assert "report.pdf" not in str(named.to_mapping())


def test_multiple_owners_and_revoke_before_publish_block() -> None:
    owners = observe_mixed_journey(
        JOURNEY,
        TURN,
        files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},),
        web=_web(),
        effect_owners=["primary", "secondary"],
    )
    assert owners.state is MixedJourneyStoreProjectionState.BLOCKED
    assert owners.view is None
    revoked = observe_mixed_journey(
        JOURNEY,
        TURN,
        files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},),
        web=_web(),
        revoked=True,
        publication_claimed=True,
    )
    assert revoked.state is MixedJourneyStoreProjectionState.BLOCKED
    assert revoked.view is None


def test_restarted_status_and_unknown_default() -> None:
    unknown = observe_mixed_journey(
        JOURNEY, TURN, files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},), web=_web()
    )
    assert unknown.view is not None
    assert unknown.view.restart.status == "unknown"
    assert unknown.view.restart.execution == "unknown"
    restarted = observe_mixed_journey(
        JOURNEY,
        TURN,
        files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},),
        web=_web(),
        status="restarted",
        execution="running",
        restarted=True,
    )
    assert restarted.state is MixedJourneyStoreProjectionState.PROJECTED
    assert restarted.view is not None
    assert restarted.view.restart.state.value == "restarted"


def test_secondary_absence_rejects_secondary_pending_owner() -> None:
    result = observe_mixed_journey(
        JOURNEY,
        TURN,
        files=({"id": "raw_aaaaaaaaaaaaaaaa", "sha256": DIGEST},),
        web=_web(),
    )
    assert result.state is MixedJourneyStoreProjectionState.PROJECTED
    assert result.view is not None
    assert result.view.pending_work_owner.value == "primary"
