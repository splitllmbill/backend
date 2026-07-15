from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.services import email_service, payment_service, user_service

router = APIRouter()


@router.post("/payment")
async def create_payment(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Create a payment page. Optional ``friendId`` emails them the link."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    request_data = await request.json()
    page = await payment_service.create_page(db, user_id, request_data)

    friend_id = request_data.get("friendId")
    if friend_id and page.get("link"):
        friend = await db.users.find_one(
            {"_id": ObjectId(friend_id)}, {"email": 1, "name": 1}
        )
        if friend and friend.get("email"):
            email_service.send_in_background(
                email_service.send_payment_request_email(
                    to_email=friend["email"],
                    to_name=friend.get("name", ""),
                    requester_name=current_user.get("name", ""),
                    amount=float(page.get("amount", 0)),
                    currency=request_data.get("currency", settings.DEFAULT_CURRENCY),
                    payment_link=f"{settings.APP_BASE_URL}/payments/{page['link']}",
                )
            )
    return page


@router.get("/payment")
async def view_payment_pages(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """View all payment pages for the current user."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    pages = await payment_service.view_pages(db, user_id)
    return pages


@router.get("/payment/{link:path}")
async def view_payment_page(
    link: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Public payment page - no authentication required."""
    page = await payment_service.view_page(db, link)
    if not page:
        raise HTTPException(status_code=404, detail="Payment page not found")
    return page
