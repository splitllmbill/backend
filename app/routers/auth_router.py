from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_current_user, get_verified_identity
from app.database import get_db
from app.services import user_service
from app.models.schemas import LoginRequest, SignupRequest, ChangePasswordRequest

router = APIRouter()


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
    )
    return user


@router.post("/logout")
async def logout(current_user: dict = Depends(get_current_user)):
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
