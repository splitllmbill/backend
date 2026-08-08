import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth import get_current_user
from app.database import get_db
from app.services import event_service, import_service, user_service
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
    # Preview against an existing group to see how much of the file is new.
    eventId: str = Form(""),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Parse a Splitwise export and return members/rows for the mapping UI."""
    text = await _read_csv(file)
    try:
        parsed = import_service.parse_splitwise_csv(text)
    except SplitwiseParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing_keys = set()
    event_members = []
    if eventId:
        user_id = await user_service.get_user_internal_id(db, current_user["uid"])
        if not user_id or not await event_service.is_event_member(db, eventId, user_id):
            raise HTTPException(status_code=404, detail="Event not found")
        existing_keys = await import_service.existing_import_keys(db, eventId)
        event = await event_service.get_event_by_id(db, eventId)
        event_members = (event or {}).get("users", [])

    preview = import_service.build_preview(file.filename or "", parsed, existing_keys)
    if eventId:
        preview["eventId"] = eventId
        preview["eventMembers"] = event_members
    return preview


@router.post("/splitwise")
async def import_splitwise(
    file: UploadFile = File(...),
    eventName: str = Form(""),
    currency: str = Form(""),
    # Import into an existing group instead of creating one; only rows that
    # are not already in it get added.
    eventId: str = Form(""),
    # JSON: {"CSV member name": {"userId": "..."} | {"email": "...", "name": "..."}}
    mapping: str = Form(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Import a Splitwise export into a new — or existing — event."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")
    if eventId and not await event_service.is_event_member(db, eventId, user_id):
        raise HTTPException(status_code=404, detail="Event not found")
    if not eventId and not eventName.strip():
        raise HTTPException(status_code=400, detail="A group name is required")

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
            event_id=eventId or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
