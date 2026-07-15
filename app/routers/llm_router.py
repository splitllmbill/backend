from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from app.auth import get_current_user
from app.database import get_db
from app.services import llm_service
from app.models.schemas import LLMExpenseRequest

router = APIRouter()

_MAX_IMAGE_B64_CHARS = 8 * 1024 * 1024  # ~6 MB image


class ReceiptScanRequest(BaseModel):
    image: str  # base64-encoded image (no data: prefix)
    mimeType: str = "image/jpeg"


@router.post("/expense")
async def convert_expense(
    request: LLMExpenseRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Use LLM to parse a natural language expense description.
    Fetches the prompt from the database (with versioning) and sends to Vertex AI (Gemini).
    """
    sentence = request.requestData.get("sentence", "")
    if not sentence:
        raise HTTPException(status_code=400, detail="Sentence is required")

    result = await llm_service.process_expense_from_text(db, sentence)
    return result

@router.post("/receipt")
async def scan_receipt(
    request: ReceiptScanRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Parse a receipt image into a structured expense (Qwen VL on Catalyst)."""
    image = request.image.strip()
    if not image:
        raise HTTPException(status_code=400, detail="Image is required")
    if len(image) > _MAX_IMAGE_B64_CHARS:
        raise HTTPException(status_code=413, detail="Image too large (max ~6 MB)")

    # Tolerate data URLs from web clients
    if image.startswith("data:"):
        try:
            header, image = image.split(",", 1)
            request.mimeType = header.split(";")[0].removeprefix("data:")
        except ValueError:
            raise HTTPException(status_code=400, detail="Malformed data URL")

    return await llm_service.process_expense_from_receipt(
        db, image, request.mimeType
    )


@router.get("/active-model")
async def get_active_model(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get the currently active LLM model."""
    settings = await db.settings.find_one({"_id": "llm_settings"})
    active_model = settings.get("activeModel", "gemini-1.5-flash") if settings else "gemini-1.5-flash"
    return {"activeModel": active_model}
