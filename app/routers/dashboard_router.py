from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_current_user
from app.database import get_db
from app.services import dashboard_service, user_service
from app.models.schemas import DashboardRequest

router = APIRouter()


@router.post("/dashboard/summary")
async def get_dashboard_summary(
    request: DashboardRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Aggregate expense summary for current user within a date range."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    result = await dashboard_service.get_summary_for_homepage(
        db, user_id, request.model_dump()
    )
    if result:
        return result
    return {"message": "No data found"}


@router.get("/dashboard/trends")
async def get_spend_trends(
    months: int = 6,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Monthly spend split personal vs group, plus per-category series."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    return await dashboard_service.get_spend_trends(db, user_id, months)


@router.post("/dashboard/chart")
async def get_dashboard_chart(
    request: DashboardRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Category-wise expense breakdown for chart display."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    result = await dashboard_service.get_dashboard_chart(
        db, user_id, request.model_dump()
    )
    return result
