"""Unit tests for the Catalyst QuickML LLM provider.

Offline: the tolerant response parsers are pure, and the request-building
methods are exercised with ``_request`` monkeypatched so no network/OAuth call
is made.
"""

import pytest

from app.services.llm.catalyst_provider import (
    CatalystLLMProvider,
    _extract_text,
    _extract_usage,
)


class TestExtractText:
    def test_openai_style_message_content(self):
        data = {"choices": [{"message": {"content": "hello"}}]}
        assert _extract_text(data) == "hello"

    def test_openai_style_choice_text_fallback(self):
        data = {"choices": [{"text": "legacy completion"}]}
        assert _extract_text(data) == "legacy completion"

    def test_top_level_string_keys(self):
        assert _extract_text({"output": "out"}) == "out"
        assert _extract_text({"response": "resp"}) == "resp"

    def test_nested_data_envelope(self):
        data = {"data": {"choices": [{"message": {"content": "deep"}}]}}
        assert _extract_text(data) == "deep"

    def test_unrecognized_shape_returns_none(self):
        assert _extract_text({"unexpected": 123}) is None
        assert _extract_text({}) is None


class TestExtractUsage:
    def test_reads_reported_tokens(self):
        usage = _extract_usage(
            {"usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}
        )
        assert usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}

    def test_derives_total_when_absent(self):
        usage = _extract_usage({"usage": {"prompt_tokens": 4, "completion_tokens": 6}})
        assert usage["total_tokens"] == 10

    def test_missing_usage_defaults_zero(self):
        assert _extract_usage({}) == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def test_reads_usage_from_data_envelope(self):
        usage = _extract_usage({"data": {"usage": {"prompt_tokens": 2}}})
        assert usage["prompt_tokens"] == 2


async def test_request_requires_configured_endpoint():
    """An empty endpoint fails fast, before any OAuth/network call."""
    provider = CatalystLLMProvider()
    with pytest.raises(RuntimeError, match="endpoint not configured"):
        await provider._request("", {"model": "x"})


async def test_generate_content_builds_chat_payload(monkeypatch):
    provider = CatalystLLMProvider()
    captured = {}

    async def fake_request(self, endpoint, payload):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {
            "choices": [{"message": {"content": "  spaced  "}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setattr(CatalystLLMProvider, "_request", fake_request)

    text, usage = await provider.generate_content("glm-4.7-flash", "hi there")

    assert text == "  spaced  "  # provider returns raw text; caller trims
    assert usage["total_tokens"] == 3
    msg = captured["payload"]["messages"][0]
    assert msg["role"] == "user"
    assert msg["content"] == "hi there"
    assert captured["payload"]["model"] == "glm-4.7-flash"


async def test_generate_vision_content_embeds_data_uri(monkeypatch):
    provider = CatalystLLMProvider()
    captured = {}

    async def fake_request(self, endpoint, payload):
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(CatalystLLMProvider, "_request", fake_request)

    text, _ = await provider.generate_vision_content(
        "qwen3.6-35b-vl", "read this", "QUJD", "image/png"
    )

    assert text == "{}"
    content = captured["payload"]["messages"][0]["content"]
    kinds = {part["type"] for part in content}
    assert kinds == {"text", "image_url"}
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"] == "data:image/png;base64,QUJD"


async def test_generate_content_raises_on_unparseable_response(monkeypatch):
    provider = CatalystLLMProvider()

    async def fake_request(self, endpoint, payload):
        return {"nothing": "useful"}

    monkeypatch.setattr(CatalystLLMProvider, "_request", fake_request)

    with pytest.raises(RuntimeError, match="Could not parse"):
        await provider.generate_content("glm-4.7-flash", "hi")
