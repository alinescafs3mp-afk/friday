"""The public reply keeps a closed label/value contract inside one HTML span."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.text_shape import exact_emphasis_label_literal_owned
from tools import synthetic_live_battery as battery


class _DraftBoundary:
    enabled = True
    model = "adjacent-emphasis-offline-boundary"
    total_budget_sec = 30.0

    def __init__(self, draft):
        self.draft = draft

    async def chat(self, _messages, **_kwargs):
        return {"content": self.draft}


@pytest.mark.parametrize("draft", ["**готово** SYN-TELEGRAM-B10-16", "**готово SYN-TELEGRAM-B10-16**"])
def test_adjacent_emphasis_public_http_and_transport_keep_value_inside(settings, tmp_path, draft):
    case = next(
        item
        for item in battery.expand_manifest_cases(battery.load_manifest(battery.MANIFEST_PATHS["B"]))
        if item.pass_id == "B-P10" and item.question_index == 16
    )
    app = create_app(replace(settings, verify_answers=False))
    llm = _DraftBoundary(draft)
    with TestClient(app) as client:
        app.state.agent.llm = llm
        app.state.llm = llm
        response = client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {settings.api_token}"},
            json={"message": case.question, "source_ref": "adjacent-emphasis", "enable_tools": True},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
    message = payload.get("message")
    published = message.get("content") if isinstance(message, dict) else message
    assert published == "**готово SYN-TELEGRAM-B10-16**"
    assert exact_emphasis_label_literal_owned(case.question, published)
    assert payload.get("files") in (None, [])
    assert payload.get("tools_used") in (None, [])
    assert battery._telegram_shape_matches(case, published)
    probe = battery._telegram_transport_probe(published, mode="normal", home=tmp_path)
    assert probe["transport_delivery_shape_exact"] is True
