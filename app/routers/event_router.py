from fastapi import APIRouter, Depends, HTTPException
from app.config import settings
from app.models.schemas import (
    EventCreate,
    EventUpdate,
    JoinEventRequest,
    MemberRatesRequest,
)
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_current_user
from app.database import get_db
from app.services import event_service, expense_service, user_service
from app.services.event_service import CurrencyChangeRequiresChoice

router = APIRouter()


async def _require_member(db: AsyncIOMotorDatabase, uid: str, event_id: str) -> str:
    """Resolve the caller's internal id, 404-ing unless they are a member."""
    user_id = await user_service.get_user_internal_id(db, uid)
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")
    return user_id


# ─── Join links ─────────────────────────────────────────────────────────
# Declared before /event/{event_id} so "join" is not swallowed as an event id.


@router.get("/event/join/{code}")
async def preview_join(
    code: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """What a group link shows before you commit: members and free spots."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    return await event_service.get_join_preview(db, code, user_id)


@router.post("/event/join/{code}")
async def join_event(
    code: str,
    request: JoinEventRequest = JoinEventRequest(),
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Join a group from its link, optionally claiming a placeholder spot."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")
    return await event_service.join_event(
        db, code, user_id, claim_member_id=request.claimMemberId
    )


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

    request_data = request.model_dump(by_alias=True)

    try:
        result = await event_service.save_event(db, user_id, request_data)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/event/{event_id}/join-link")
async def get_join_link(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """The shareable link for this group (created on first request)."""
    await _require_member(db, current_user["uid"], event_id)
    code = await event_service.ensure_join_code(db, event_id)
    return {
        "code": code,
        "url": f"{settings.APP_BASE_URL.rstrip('/')}/join/{code}",
    }


@router.post("/event/{event_id}/join-link/rotate")
async def rotate_join_link(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Issue a new link and stop the old one from working."""
    await _require_member(db, current_user["uid"], event_id)
    code = await event_service.rotate_join_code(db, event_id)
    return {
        "code": code,
        "url": f"{settings.APP_BASE_URL.rstrip('/')}/join/{code}",
    }


@router.get("/event/{event_id}/currency-impact")
async def get_currency_impact(
    event_id: str,
    currency: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """How many transactions switching the group currency would rewrite."""
    await _require_member(db, current_user["uid"], event_id)
    return await event_service.get_currency_change_impact(db, event_id, currency)


@router.get("/event/{event_id}/rates")
async def get_event_rates(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """This member's conversion rates for the currencies used in the group."""
    user_id = await _require_member(db, current_user["uid"], event_id)
    return await event_service.get_event_rate_settings(db, event_id, user_id)


@router.put("/event/{event_id}/rates")
async def set_event_rates(
    event_id: str,
    request: MemberRatesRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Set this member's own conversion rates (affects only what they see)."""
    user_id = await _require_member(db, current_user["uid"], event_id)
    return await event_service.set_event_member_rates(
        db, event_id, user_id, request.rates
    )


@router.get("/event/{event_id}/summary")
async def get_event_summary(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Per-member spend/paid totals for the group, plus the overall total."""
    user_id = await _require_member(db, current_user["uid"], event_id)
    return await event_service.get_event_summary(db, event_id, user_id)


@router.delete("/event/{event_id}")
async def delete_event(
    event_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete an event and its associated expenses."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id or not await event_service.is_event_member(db, event_id, user_id):
        raise HTTPException(status_code=404, detail="Event not found")

    result = await event_service.delete_event(db, event_id)
    return result


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

    result = await event_service.get_event_dues(db, event_id, viewer_id=user_id)
    return result
