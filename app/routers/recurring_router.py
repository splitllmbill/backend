"""Subscriptions and other expenses that repeat.

The list endpoint posts anything that has come due for the caller before it
answers, so opening the screen is also what catches the schedule up. The hourly
loop in main.py does the same for everyone, so a user who never opens the app
still has their rent recorded.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.auth import get_current_user
from app.database import get_db
from app.services import recurring_service, user_service

router = APIRouter()


class RecurringCreate(BaseModel):
    expenseName: str = ""
    title: Optional[str] = None
    amount: float
    category: str = "General"
    description: str = ""
    currency: Optional[str] = None
    #: One of recurring_service.FREQUENCIES.
    frequency: str = "monthly"
    startDate: Optional[str] = None
    endDate: Optional[str] = None
    #: "normal" for a personal subscription, "group" to charge a group.
    type: str = "normal"
    eventId: Optional[str] = None
    #: "equal" re-splits across the group's members every time it posts;
    #: "fixed" replays the shares below.
    splitMode: str = "equal"
    shares: list[dict] = []
    payShares: list[dict] = []
    paused: bool = False


class RecurringUpdate(BaseModel):
    expenseName: Optional[str] = None
    amount: Optional[float] = None
    category: Optional[str] = None
    frequency: Optional[str] = None
    endDate: Optional[str] = None
    paused: Optional[bool] = None


async def _require_user(db: AsyncIOMotorDatabase, current_user: dict) -> str:
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")
    return user_id


@router.get("/recurring")
async def list_recurring(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = await _require_user(db, current_user)
    posted = await recurring_service.post_due(db, user_id=user_id)
    return {
        "recurring": await recurring_service.list_schedules(db, user_id),
        # So the app can say "2 added" rather than silently growing the list.
        "posted": posted,
    }


@router.post("/recurring")
async def create_recurring(
    request: RecurringCreate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = await _require_user(db, current_user)
    payload = request.model_dump()
    payload["expenseName"] = request.title or request.expenseName
    schedule = await recurring_service.create_schedule(db, user_id, payload)
    # A schedule starting today or in the past owes its first occurrence
    # immediately; making the user wait an hour for it looks broken.
    await recurring_service.post_due(db, user_id=user_id)
    return {"message": "Subscription added", "success": "true", "data": schedule}


@router.put("/recurring/{schedule_id}")
async def update_recurring(
    schedule_id: str,
    request: RecurringUpdate,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = await _require_user(db, current_user)
    payload = {k: v for k, v in request.model_dump(exclude_unset=True).items()}
    schedule = await recurring_service.update_schedule(
        db, user_id, schedule_id, payload
    )
    return {"message": "Subscription updated", "success": "true", "data": schedule}


@router.delete("/recurring/{schedule_id}")
async def delete_recurring(
    schedule_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    user_id = await _require_user(db, current_user)
    return await recurring_service.delete_schedule(db, user_id, schedule_id)
