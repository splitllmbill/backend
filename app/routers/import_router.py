import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth import get_current_user
from app.database import get_db
from app.services import import_service, user_service
from app.services.import_service import SplitwiseParseError

router = APIRouter()

_MAX_FILE_BYTES = 2 * 1024 * 1024  # Splitwise exports are small CSVs


async def _read_csv(file: UploadFile) -> str:
    content = await file.read()
    if len(content) > _MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")


@router.post("/splitwise/preview")
async def preview_splitwise_import(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Parse a Splitwise export and return members/rows for the mapping UI."""
    text = await _read_csv(file)
    try:
        parsed = import_service.parse_splitwise_csv(text)
    except SplitwiseParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return import_service.build_preview(file.filename or "", parsed)


@router.post("/splitwise")
async def import_splitwise(
    file: UploadFile = File(...),
    eventName: str = Form(...),
    currency: str = Form(""),
    # JSON: {"CSV member name": {"userId": "..."} | {"email": "...", "name": "..."}}
    mapping: str = Form(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Import a Splitwise export as a new event with all its expenses."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    text = await _read_csv(file)
    try:
        parsed = import_service.parse_splitwise_csv(text)
        mapping_dict = json.loads(mapping)
        if not isinstance(mapping_dict, dict):
            raise ValueError("mapping must be a JSON object")
    except SplitwiseParseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid mapping: {e}")

    # Default event currency: the most common row currency in the file
    if not currency:
        counts: dict[str, int] = {}
        for row in parsed["rows"]:
            counts[row["currency"]] = counts.get(row["currency"], 0) + 1
        currency = max(counts, key=counts.get)

    try:
        result = await import_service.import_splitwise(
            db,
            user_id,
            parsed,
            event_name=eventName,
            event_currency=currency,
            mapping=mapping_dict,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
