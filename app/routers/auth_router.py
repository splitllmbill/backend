from fastapi import APIRouter, Body, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_account_user, get_current_user, get_verified_identity
from app.database import get_db
from app.services import user_service
from app.models.schemas import (
    ActivateRequest,
    ChangePasswordRequest,
    LoginRequest,
    SignupRequest,
)

router = APIRouter()


# /login and /signup use the verify-only dependency: get_current_user would
# auto-create the Mongo user before these handlers run, without the invite
# code from the request body — rejecting every new (non-first) user.
@router.post("/login")
async def login(
    identity: dict = Depends(get_verified_identity),
    db: AsyncIOMotorDatabase = Depends(get_db),
    request: LoginRequest = Body(default_factory=LoginRequest),
):
    """Called after Firebase sign-in. Creates user if not exists.

    The body is optional: clients registering after an OAuth or
    email-confirmation redirect have a verified session but nothing to send.
    """
    user = await user_service.find_or_create_user(
        db,
        uid=identity["uid"],
        # Identity comes from the verified JWT only. Falling back to the
        # client-supplied email let a caller whose token carried no email
        # claim name an arbitrary victim, and find_or_create_user would then
        # rebind that account to the caller's uid.
        email=identity["email"],
        name=identity.get("name", ""),
        invite_code=request.inviteCode,
        allow_pending=True,
    )
    return user


@router.post("/signup")
async def signup(
    request: SignupRequest,
    identity: dict = Depends(get_verified_identity),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Called after Firebase sign-up. Creates user record."""
    user = await user_service.find_or_create_user(
        db,
        uid=identity["uid"],
        # Identity comes from the verified JWT only. Falling back to the
        # client-supplied email let a caller whose token carried no email
        # claim name an arbitrary victim, and find_or_create_user would then
        # rebind that account to the caller's uid.
        email=identity["email"],
        name=request.name,
        invite_code=request.inviteCode,
        allow_pending=True,
    )
    return user


@router.post("/activate")
async def activate(
    request: ActivateRequest,
    current_user: dict = Depends(get_account_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Redeem an invite code for an account that was created without one.

    Signing up without a code no longer turns anyone away — it parks them in a
    pending account that can see nothing. This is how they get out of it, and
    it takes the un-gated dependency because by definition the caller is gated.
    """
    return await user_service.activate_with_invite(
        db, current_user["uid"], request.inviteCode
    )


# Signing out must work whatever state the account is in.
@router.post("/logout")
async def logout(current_user: dict = Depends(get_account_user)):
    """Logout acknowledgement. Firebase tokens are stateless."""
    return {"message": "Logged out"}


@router.put("/changePassword")
async def change_password(
    request: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update password in backend (Firebase password change is client-side)."""
    result = await user_service.change_password(db, current_user["uid"])
    return result
