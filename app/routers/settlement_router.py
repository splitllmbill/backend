"""PayPal + UPI settlement endpoints (PayPal AI Hackathon entry).

A debt is settled in full, debtor -> creditor, over one of two rails:

* **PayPal** — ``POST .../settleup/paypal`` creates a PayPal Orders v2 order
  with the creditor as ``payee`` and returns the approval URL. The debtor
  approves in PayPal Checkout; ``POST /settlements/paypal/{order_id}/capture``
  (called from the return-URL handler) captures it and the server re-fetches
  the order to verify it is COMPLETED for the exact amount before the debt
  is marked settled. ``POST /webhooks/paypal`` is the production-grade
  confirmation path: a verified ``PAYMENT.CAPTURE.COMPLETED`` event settles
  the debt even if the payer closed the browser after approving.
* **UPI** — ``POST .../settleup/upi`` returns a ``upi://pay`` intent link +
  QR code and records a pending settlement the creditor confirms with the
  existing ``POST /db/settlements/{id}/confirm`` endpoint. The payment
  itself happens in the payer's own UPI app; SplitLLM never touches money
  on this rail and there is no automatic confirmation (no UPI webhook
  exists — this is recorded honestly, not papered over).

All endpoints require a Supabase JWT except the webhook, where the PayPal
signature verification IS the authentication.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.auth import get_current_user
from app.database import get_db
from app.models.schemas import (
    StartPayPalSettlementRequest,
    StartUpiSettlementRequest,
)
from app.services import settlement_service, user_service
from app.services.settlement_service import (
    PayPalApiError,
    SettlementConflictError,
    SettlementNotConfigured,
    SettlementValidationError,
)

router = APIRouter()


async def _run(coro):
    """Run a settlement-service call, translating its domain errors into
    HTTP statuses. FastAPI would turn the uncaught ValueError subclasses
    into 500s, which would hide a fixable client error (e.g. no PayPal
    email on the creditor) behind a server error."""
    try:
        return await coro
    except SettlementValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SettlementConflictError as exc:
        detail: dict = {"message": str(exc)}
        if exc.settlement_id:
            detail["settlementId"] = exc.settlement_id
        raise HTTPException(status_code=409, detail=detail)
    except SettlementNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except PayPalApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


async def _internal_id(db: AsyncIOMotorDatabase, current_user: dict) -> str:
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")
    return user_id


@router.post("/user/expense/friend/{friend_id}/settleup/paypal")
async def start_paypal_settlement(
    friend_id: str,
    body: StartPayPalSettlementRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Start a PayPal settlement — returns the PayPal Checkout approval URL."""
    user_id = await _internal_id(db, current_user)
    return await _run(
        settlement_service.start_paypal_settlement(
            db,
            user_id,
            friend_id,
            note=body.note,
            creditor_paypal_email=body.creditorPaypalEmail,
            payer_country=body.payerCountry,
            payee_country=body.payeeCountry,
        )
    )


@router.post("/user/expense/friend/{friend_id}/settleup/upi")
async def start_upi_settlement(
    friend_id: str,
    body: StartUpiSettlementRequest,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Start a UPI settlement — returns the intent link + QR code and the
    pending settlement id."""
    user_id = await _internal_id(db, current_user)
    return await _run(
        settlement_service.start_upi_settlement(
            db,
            user_id,
            friend_id,
            note=body.note,
            creditor_vpa=body.creditorVpa,
            payer_country=body.payerCountry,
            payee_country=body.payeeCountry,
        )
    )


@router.post("/settlements/paypal/{order_id}/capture")
async def capture_paypal_settlement(
    order_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Capture an approved PayPal order, verify it server-side, and settle
    the debt if the capture checks out. Safe to retry — a settled attempt
    returns its stored outcome."""
    user_id = await _internal_id(db, current_user)
    return await _run(
        settlement_service.capture_paypal_settlement(db, order_id, user_id)
    )


@router.get("/settlements/paypal/{order_id}/status")
async def paypal_settlement_status(
    order_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Re-check a PayPal order's state (pollable by the frontend while the
    payer is in Checkout). Finalizes if the order is now captured."""
    user_id = await _internal_id(db, current_user)
    return await _run(
        settlement_service.get_paypal_settlement_status(db, order_id, user_id)
    )


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request, db: AsyncIOMotorDatabase = Depends(get_db)):
    """PayPal webhook receiver — no JWT; the PayPal signature verification
    is the authentication. The raw body must be read unparsed: the
    signature covers the exact bytes PayPal sent."""
    raw_body = await request.body()
    result = await _run(
        settlement_service.handle_paypal_webhook(
            db, raw_body=raw_body, headers=dict(request.headers)
        )
    )
    if result.get("accepted") is False:
        raise HTTPException(status_code=401, detail=result.get("reason"))
    return {"ok": True, **result}
