import json
import traceback
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import settings
from datetime import datetime, timezone
from .llm.provider_base import BaseLLMProvider
from .llm.catalyst_provider import CatalystLLMProvider


def get_llm_provider(model_name: str) -> BaseLLMProvider:
    """Return the LLM provider. Catalyst QuickML (GLM text / Qwen vision) is the
    only backend now; the active model name is forwarded to the provider at call
    time so it can pick the right endpoint."""
    return CatalystLLMProvider()


async def get_prompt(db: AsyncIOMotorDatabase, prompt_type: str) -> dict | None:
    """Get the latest prompt from the database by type, ordered by version."""
    prompt = await db.prompts.find_one(
        {"type": prompt_type},
        sort=[("version", -1)],
    )
    return prompt


async def generate_text_content(
    db: AsyncIOMotorDatabase, prompt_type: str, user_input: str
) -> tuple[str | None, str | None]:
    """
    Generate text content using generic LLM API.
    Fetches the prompt from the database and appends user input.
    Returns (result, error).
    """
    # Get prompt from database
    prompt_doc = await get_prompt(db, prompt_type)
    if not prompt_doc:
        return None, f"Prompt type '{prompt_type}' not found in database"

    prompt_text = prompt_doc.get("prompt", "")
    full_prompt = prompt_text + user_input

    try:
        # Fetch active model from DB
        settings_doc = await db.settings.find_one({"_id": "llm_settings"})
        active_model = settings_doc.get("activeModel", settings.CATALYST_LLM_MODEL) if settings_doc else settings.CATALYST_LLM_MODEL

        provider = get_llm_provider(active_model)
        result_text, stats = await provider.generate_content(active_model, full_prompt)

        # Clean up the response
        result_text = (
            result_text.replace("`", "")
            .replace("json", "")
            .replace("JSON", "")
            .strip()
        )

        # Update prompt hit counters
        await db.prompts.update_one(
            {"_id": prompt_doc["_id"]},
            {"$inc": {"hits": 1, "successHits": 1}},
        )

        # Update model global stats
        await db.llm_stats.update_one(
            {"model": active_model},
            {
                "$inc": {
                    "totalRequests": 1,
                    "promptTokens": stats["prompt_tokens"],
                    "completionTokens": stats["completion_tokens"],
                    "totalTokens": stats["total_tokens"],
                },
                "$set": {"lastUsedAt": datetime.now(timezone.utc)}
            },
            upsert=True
        )

        return result_text, None

    except Exception as e:
        traceback.print_exc()
        # Update failure counter
        if prompt_doc:
            await db.prompts.update_one(
                {"_id": prompt_doc["_id"]},
                {"$inc": {"hits": 1, "failureHits": 1}},
            )
        return None, str(e)


async def process_expense_from_text(
    db: AsyncIOMotorDatabase, sentence: str
) -> dict:
    """
    Parse a natural language expense description using LLM.
    Returns structured expense data or error.
    """
    # LLM feature is disabled: short-circuit before touching any provider so
    # the app runs without GCP/genai. Flip settings.LLM_ENABLED to re-enable.
    if not settings.LLM_ENABLED:
        return {
            "message": "LLM feature is currently disabled",
            "llmoutput": {},
        }

    result, error = await generate_text_content(db, "chat", sentence)

    if error:
        return {
            "message": f"Exception while calling LLM API: {error}",
            "llmoutput": {},
        }

    return _parse_llm_json(result)


def _parse_llm_json(result: str) -> dict:
    """Parse an LLM response as normalized expense JSON."""
    try:
        json_response = json.loads(result)
        # Normalize keys to lowercase
        if isinstance(json_response, list):
            json_array = [
                {k.lower(): v for k, v in item.items()} for item in json_response
            ]
        else:
            json_array = [{k.lower(): v for k, v in json_response.items()}]

        return {
            "message": "Expense Processed Successfully",
            "llmoutput": json_array,
        }
    except json.JSONDecodeError:
        return {
            "message": "Failed to parse LLM response as JSON",
            "llmoutput": {},
            "raw": result,
        }


# Fallback prompt when no "receipt" prompt is stored in db.prompts
_DEFAULT_RECEIPT_PROMPT = """You are a receipt parser for an expense-splitting app.
Look at the receipt image and respond with ONLY a JSON object (no markdown, no
code fences, no commentary) with these keys:
- "name": short merchant/expense title
- "amount": grand total as a number
- "currency": ISO 4217 code inferred from the receipt (e.g. "INR", "USD")
- "date": receipt date as YYYY-MM-DD, or null if unreadable
- "category": one of "Dining out", "Groceries", "Taxi", "Travel", "Shopping",
  "Entertainment", "Utilities", "Health", "General"
- "items": array of {"name": string, "amount": number} line items (may be [])
If the image is not a receipt, respond with {"error": "not a receipt"}.
"""


async def process_expense_from_receipt(
    db: AsyncIOMotorDatabase, image_base64: str, mime_type: str
) -> dict:
    """Parse a receipt image into structured expense data via the VL model."""
    if not settings.LLM_ENABLED:
        return {
            "message": "LLM feature is currently disabled",
            "llmoutput": {},
        }

    prompt_doc = await get_prompt(db, "receipt")
    prompt_text = (
        prompt_doc.get("prompt") if prompt_doc else None
    ) or _DEFAULT_RECEIPT_PROMPT

    provider = CatalystLLMProvider()
    model = settings.CATALYST_VLM_MODEL

    try:
        result_text, stats = await provider.generate_vision_content(
            model, prompt_text, image_base64, mime_type
        )
    except Exception as e:
        traceback.print_exc()
        return {
            "message": f"Exception while calling vision LLM API: {e}",
            "llmoutput": {},
        }

    result_text = (
        result_text.replace("```", "").replace("json", "").replace("JSON", "").strip()
    )

    # Track usage the same way as the text path
    await db.llm_stats.update_one(
        {"model": model},
        {
            "$inc": {
                "totalRequests": 1,
                "promptTokens": stats["prompt_tokens"],
                "completionTokens": stats["completion_tokens"],
                "totalTokens": stats["total_tokens"],
            },
            "$set": {"lastUsedAt": datetime.now(timezone.utc)},
        },
        upsert=True,
    )

    return _parse_llm_json(result_text)
