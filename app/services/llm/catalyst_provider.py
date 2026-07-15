"""Catalyst QuickML LLM Serving provider.

Talks to models deployed in the Catalyst console (QuickML -> LLM Serving),
e.g. GLM-4.7-Flash for text and Qwen 3.6-35B VL for receipt images. Endpoint
URLs come from the Model Details page (CATALYST_LLM_ENDPOINT /
CATALYST_VLM_ENDPOINT); auth is a Zoho OAuth token plus the Catalyst org id.

The request/response shape follows the OpenAI-style chat format QuickML's
serving endpoints use; ``_extract_text`` is deliberately tolerant so minor
schema differences between QuickML releases don't break parsing.
"""

import json
import logging
from typing import Any, Dict, Tuple

import httpx

from app.config import settings
from .provider_base import BaseLLMProvider
from .zoho_oauth import get_access_token

logger = logging.getLogger(__name__)


def _extract_text(data: dict) -> str | None:
    """Pull the completion text out of the response, tolerating variants."""
    try:
        choices = data.get("choices")
        if choices:
            choice = choices[0]
            message = choice.get("message") or {}
            return message.get("content") or choice.get("text")
    except (AttributeError, IndexError, TypeError):
        pass
    for key in ("output", "response", "answer", "text", "content", "result"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            nested = _extract_text(value)
            if nested:
                return nested
    if isinstance(data.get("data"), dict):
        return _extract_text(data["data"])
    return None


def _extract_usage(data: dict) -> Dict[str, int]:
    usage = data.get("usage") or (data.get("data") or {}).get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": int(
            usage.get("total_tokens", prompt_tokens + completion_tokens)
        ),
    }


class CatalystLLMProvider(BaseLLMProvider):
    """QuickML LLM Serving (text chat) provider."""

    async def _request(self, endpoint: str, payload: dict) -> dict:
        if not endpoint:
            raise RuntimeError(
                "Catalyst LLM endpoint not configured "
                "(set CATALYST_LLM_ENDPOINT / CATALYST_VLM_ENDPOINT)"
            )
        token = await get_access_token()
        headers = {
            "Authorization": f"Zoho-oauthtoken {token}",
            "Content-Type": "application/json",
        }
        if settings.CATALYST_ORG_ID:
            headers["CATALYST-ORG"] = settings.CATALYST_ORG_ID

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
        if resp.status_code >= 300:
            logger.error(
                "Catalyst LLM call failed (%s): %s",
                resp.status_code,
                resp.text[:500],
            )
            raise RuntimeError(f"Catalyst LLM call failed: HTTP {resp.status_code}")
        try:
            return resp.json()
        except json.JSONDecodeError:
            raise RuntimeError("Catalyst LLM returned a non-JSON response")

    async def generate_content(
        self, model_name: str, prompt: str
    ) -> Tuple[str, Dict[str, Any]]:
        payload = {
            "model": model_name or settings.CATALYST_LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        data = await self._request(settings.CATALYST_LLM_ENDPOINT, payload)
        text = _extract_text(data)
        if text is None:
            logger.error("Unrecognized Catalyst LLM response shape: %s", str(data)[:500])
            raise RuntimeError("Could not parse Catalyst LLM response")
        return text, _extract_usage(data)

    async def generate_vision_content(
        self, model_name: str, prompt: str, image_base64: str, mime_type: str
    ) -> Tuple[str, Dict[str, Any]]:
        """Send text + one image to the vision-language endpoint (Qwen VL)."""
        payload = {
            "model": model_name or settings.CATALYST_VLM_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0.1,
        }
        endpoint = settings.CATALYST_VLM_ENDPOINT or settings.CATALYST_LLM_ENDPOINT
        data = await self._request(endpoint, payload)
        text = _extract_text(data)
        if text is None:
            logger.error("Unrecognized Catalyst VLM response shape: %s", str(data)[:500])
            raise RuntimeError("Could not parse Catalyst VLM response")
        return text, _extract_usage(data)
