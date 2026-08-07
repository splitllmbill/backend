from fastapi import APIRouter, Depends, HTTPException
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


@router.get("/invite/validate")
async def validate_invite(
    code: str = "",
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Check an invite code before signing up.

    Deliberately unauthenticated: with Google sign-in the code has to be
    checked *before* the OAuth redirect, when there is no session yet — being
    bounced after a round trip through Google because of a typo is miserable.
    Only tells the caller whether a code works and who it came from.
    """
    code = (code or "").strip().upper()
    # The very first account bootstraps the instance and needs no invite.
    if await db.users.count_documents({}, limit=1) == 0:
        return {"valid": True, "required": False, "type": "bootstrap"}

    if not code:
        return {"valid": False, "required": True, "reason": "Invite code is required"}

    invite = await user_service.resolve_invite_code(db, code)
    if not invite:
        return {"valid": False, "required": True, "reason": "That invite code is not valid"}

    return {
        "valid": True,
        "required": True,
        "type": invite["type"],
        "invitedBy": invite.get("inviterName", ""),
        "groupName": invite.get("eventName", ""),
    }


# /login and /signup use the verify-only dependency: get_current_user would
# auto-create the Mongo user before these handlers run, without the invite
# code from the request body — rejecting every new (non-first) user.
@router.post("/login")
async def login(
    request: LoginRequest,
    identity: dict = Depends(get_verified_identity),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Called after Firebase sign-in. Creates user if not exists."""
    user = await user_service.find_or_create_user(
        db,
        uid=identity["uid"],
        email=identity["email"] or request.email,
        name=identity.get("name", ""),
        password=request.password,
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
        email=identity["email"] or request.email,
        name=request.name,
        password=request.password,
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
    result = await user_service.change_password(db, current_user["uid"], request.password)
    return result
