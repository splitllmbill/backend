from fastapi import APIRouter, Depends, HTTPException
from app.models.schemas import EventCreate, EventUpdate, JoinGroupRequest
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_current_user
from app.database import get_db
from app.services import event_service, expense_service, user_service

router = APIRouter()


@router.get("/user/events")
async def get_user_events(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List all events the current user is a member of."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        result = await event_service.get_user_events(db, user_id)
        return result
    except Exception as e:
        print(f"Error in getUserEvents route: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/event/{event_id}")
async def get_event(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get single event details."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    event = await event_service.get_event_by_id(db, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.post("/event")
async def create_event(
    request: EventCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Create a new event."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    request_data = request.model_dump(by_alias=True)

    try:
        result = await event_service.save_event(db, user_id, request_data)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/event")
async def update_event(
    request: EventUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update an existing event."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    # Only the fields the client actually sent. A plain model_dump emits every
    # optional field at its default, so an update that just flips one setting
    # also sent `users: []` — which read as "remove everyone from the group".
    request_data = request.model_dump(by_alias=True, exclude_unset=True)

    try:
        result = await event_service.save_event(db, user_id, request_data)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/event/{event_id}")
async def delete_event(
    event_id: str,
    force: bool = False,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete an event and its associated expenses.

    Refused with 409 while anyone still owes anyone inside it. ``force=true``
    overrides that, and only the group's creator may use it.
    """
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    return await event_service.delete_event(db, event_id, user_id, force=force)


@router.get("/event/{event_id}/invite")
async def get_event_invite(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get (creating on first use) the group's join code."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    return await event_service.get_join_link(db, event_id, user_id)


@router.post("/event/{event_id}/invite")
async def rotate_event_invite(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Issue a fresh join code, invalidating every link shared so far."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    return await event_service.get_join_link(db, event_id, user_id, rotate=True)


@router.delete("/event/{event_id}/invite")
async def disable_event_invite(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Stop accepting joins by link for this group."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    return await event_service.disable_join_link(db, event_id, user_id)


@router.get("/join/{code}")
async def preview_join(
    code: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Public: what group an invite link points at.

    Unauthenticated on purpose — someone following the link has to see what
    they are being invited to *before* creating an account. Only the group's
    name, size and currency are exposed; see event_service.preview_join_code.
    """
    return await event_service.preview_join_code(db, code)


@router.get("/join/{code}/claimable")
async def claimable_members(
    code: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Placeholder members a joiner can identify themselves as.

    Signed-in only: unlike the preview above this returns member names.
    """
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    return await event_service.get_claimable_members(db, code)


@router.post("/join/{code}")
async def join_by_code(
    code: str,
    request: JoinGroupRequest | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Join the group an invite link points at, optionally claiming a slot."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    return await event_service.join_event_by_code(
        db, code, user_id, claim_member_id=(request.claimMemberId if request else "")
    )


@router.get("/event/{event_id}/expenses")
async def get_event_expenses(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """List all expenses for an event."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    result = await expense_service.get_event_expenses_with_summary(
        db, user_id, event_id
    )
    return result


@router.get("/event/{event_id}/summary")
async def get_event_summary(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Where the money went: group total, and each member's spend/paid/net."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    return await event_service.get_event_summary(db, event_id, user_id)


@router.get("/user/event/{event_id}/dues")
async def get_event_dues_for_user(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Calculate who owes whom within an event for the current user."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    result = await event_service.get_event_dues_for_user(db, event_id, user_id)
    return result


@router.get("/event/{event_id}/dues")
async def get_event_dues(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Calculate all dues within an event."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    result = await event_service.get_event_dues(db, event_id)
    return result
