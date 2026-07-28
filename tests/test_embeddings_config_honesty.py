"""Semantic search that is off must not report itself as on.

`EmbeddingBackend.remote_enabled` needs the base URL AND the model name, but
`JERICHO_EMBEDDINGS_MODEL` ships empty in both templates. So an operator who set
`JERICHO_EMBEDDINGS_ENABLED=1` and nothing else got no dense recall at all, while
`validate_settings` returned clean, `/api/config` published `enabled: true`, and
`jericho model-check` probed with `embeddings_model or llm_model` — hitting a real
endpoint with the CHAT model and printing green dimensions for a channel that
never sent a single request.
"""

from __future__ import annotations

from dataclasses import replace

from jericho.config import validate_settings


def test_enabled_without_a_model_is_an_error_not_a_warning(settings):
    broken = replace(settings, embeddings_enabled=True, embeddings_model="")
    problems = validate_settings(broken)
    hard_errors = [item for item in problems if not item.startswith("warning:")]
    assert any("JERICHO_EMBEDDINGS_MODEL" in item for item in hard_errors), (
        f"a configuration that cannot work passed validation: {problems}"
    )


def test_a_configured_model_validates_cleanly(settings):
    working = replace(
        settings,
        embeddings_enabled=True,
        embeddings_model="bge-m3",
        embeddings_base_url="http://127.0.0.1:8002/v1",
    )
    hard_errors = [item for item in validate_settings(working) if not item.startswith("warning:")]
    assert not [item for item in hard_errors if "EMBEDDINGS" in item], hard_errors


def test_public_config_reports_what_retrieval_will_actually_do(settings):
    broken = replace(settings, embeddings_enabled=True, embeddings_model="")
    published = broken.public_dict()["embeddings"]
    assert published["enabled"] is True  # the flag, as set
    assert published["effective"] is False, "the API told the operator dense recall was working"

    working = replace(
        settings,
        embeddings_enabled=True,
        embeddings_model="bge-m3",
        embeddings_base_url="http://127.0.0.1:8002/v1",
    )
    assert working.public_dict()["embeddings"]["effective"] is True


def test_the_model_check_probe_fails_on_the_same_condition_the_runtime_does(settings):
    """No substitution: the probe must not pass by asking a different model."""
    import inspect

    from jericho import model_check

    source = inspect.getsource(model_check)
    assert "embeddings_model or settings.llm_model" not in source
    assert "settings.embeddings_model or model" not in source
