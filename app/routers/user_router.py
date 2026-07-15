from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_current_user
from app.database import get_db
from app.services import email_service, user_service
from app.models.schemas import UpdateAccountRequest
from pydantic import BaseModel

class PhantomUserRequest(BaseModel):
    email: str
    name: str = ""

router = APIRouter()


@router.get("/user/account")
async def get_account(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get current user's profile."""
    account = await user_service.get_user_account(db, current_user["uid"])
    if not account:
        raise HTTPException(status_code=404, detail="User not found")
    return account


@router.put("/user/account")
async def update_account(
    request: UpdateAccountRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update current user's profile."""
    # Get internal user ID
    user = await user_service.get_user_by_firebase_uid(db, current_user["uid"])
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    result = await user_service.update_user_account(
        db, current_user["uid"], request.model_dump(exclude_none=True)
    )
    if not result:
        raise HTTPException(status_code=500, detail="Failed to update account details")
    return {"message": "Account updated successfully"}

@router.get("/user/invites")
async def get_invites(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get list of users invited by the current user."""
    account = await user_service.get_user_account(db, current_user["uid"])
    if not account or not account.get("friendCode"):
        return []
    invites = await user_service.get_invited_users(db, account["friendCode"])
    return invites

@router.post("/user/phantom")
async def create_phantom_user(
    request: PhantomUserRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Create or find a phantom user by email."""
    user = await user_service.create_phantom_user(
        db, email=request.email, name=request.name
    )
    if user.pop("wasCreated", False):
        email_service.send_in_background(
            email_service.send_invite_email(
                to_email=request.email,
                to_name=user.get("name", ""),
                inviter_name=current_user.get("name", ""),
            )
        )
    return user

