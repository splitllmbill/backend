from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth import get_current_user
from app.database import get_db
from app.services import fx_service

router = APIRouter()


@router.get("/fx/currencies")
async def list_currencies():
    """Supported currencies with symbols (no auth: static reference data)."""
    return {
        "default": fx_service.normalize_currency(None),
        "currencies": [
            {"code": code, **meta}
            for code, meta in fx_service.SUPPORTED_CURRENCIES.items()
        ],
    }


@router.get("/fx/rate")
async def get_rate(
    from_currency: str,
    to_currency: str,
    date: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Conversion rate between two currencies, optionally on a past date."""
    rate = await fx_service.get_rate(from_currency, to_currency, on=date, db=db)
    return {
        "from": fx_service.normalize_currency(from_currency),
        "to": fx_service.normalize_currency(to_currency),
        "date": date or datetime.now(timezone.utc).date().isoformat(),
        "rate": rate,
    }
