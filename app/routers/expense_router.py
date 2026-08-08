from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.auth import get_current_user
from app.database import get_db
from app.services import (
    bulk_expense_service,
    category_service,
    email_service,
    expense_service,
    recurring_service,
    user_service,
)
from app.models.schemas import (
    BulkExpenseRequest,
    PersonalExpensesRequest,
    ExpenseCreate,
    ExpenseUpdate,
)

router = APIRouter()


@router.get("/categories")
async def list_categories():
    """The one category vocabulary (no auth: static reference data)."""
    return {
        "default": category_service.DEFAULT_CATEGORY,
        "categories": category_service.CATEGORIES,
    }


@router.get("/expense/category-suggestion")
async def suggest_category(
    name: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Guess a category from the expense title.

    Answers from what this person has filed under that title before, then what
    everyone has, then a seed keyword table — see category_service. Returns
    ``{"category": null}`` when nothing is known, which means "leave their
    choice alone" rather than "General".
    """
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    suggestion = await category_service.suggest(db, name, user_id)
    return suggestion or {"category": None, "source": None, "confidence": 0.0}


async def _notify_expense_members(
    db: AsyncIOMotorDatabase, creator_id: str, creator_name: str, expense: dict
):
    """Email everyone involved in a new shared expense (except the creator)."""
    if expense.get("type") == "normal":
        return

    member_ids = {
        s.get("userId")
        for s in expense.get("shares", []) + expense.get("payShares", [])
        if s.get("userId")
    }
    member_ids.discard(creator_id)
    if not member_ids:
        return

    event_name = None
    if expense.get("eventId"):
        event = await db.event.find_one(
            {"_id": ObjectId(expense["eventId"])}, {"eventName": 1}
        )
        event_name = (event or {}).get("eventName")

    users = await db.users.find(
        {"_id": {"$in": [ObjectId(mid) for mid in member_ids]}},
        {"email": 1, "name": 1},
    ).to_list(length=None)

    for member in users:
        if not member.get("email"):
            continue
        email_service.send_in_background(
            email_service.send_expense_notification(
                to_email=member["email"],
                to_name=member.get("name", ""),
                creator_name=creator_name,
                expense_name=expense.get("expenseName", ""),
                amount=float(
                    expense.get("originalAmount", expense.get("amount", 0))
                ),
                currency=expense.get("currency", ""),
                event_name=event_name,
            )
        )


@router.post("/expense")
async def create_expense(
    request: ExpenseCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Create a new expense (can be event, friend, or personal)."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    request_data = request.model_dump(by_alias=True)
    try:
        result = await expense_service.create_expense(db, user_id, request_data)
        if result.get("success") == "true":
            await _notify_expense_members(
                db, user_id, current_user.get("name", ""), result.get("data", {})
            )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/expense/{expense_id}")
async def get_expense(
    expense_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get full expense details."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    result = await expense_service.get_expense_by_id(db, expense_id, user_id)
    if not result:
        raise HTTPException(status_code=404, detail="Expense not found")
    return result


@router.put("/expense/{expense_id}")
async def update_expense(
    expense_id: str,
    request: ExpenseUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Update an expense."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    # `exclude_unset` matters here: a plain model_dump emits every optional
    # field at its default, so an edit form that never touches the split still
    # sent `shares: []` and `payShares: []` — which the service faithfully
    # wrote, erasing the split and making the expense vanish from every list
    # that queries on payShares. Only fields the client actually sent are
    # applied.
    request_data = request.model_dump(by_alias=True, exclude_unset=True)
    result = await expense_service.update_expense(db, user_id, expense_id, request_data)
    return result


@router.delete("/expense/{expense_id}")
async def delete_expense(
    expense_id: str,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Delete an expense."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    result = await expense_service.delete_expense(db, expense_id, user_id)
    return result


@router.post("/expenses/personal")
async def get_personal_expenses(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get all personal expenses for the current user."""
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    # Post any subscription that has fallen due first, so opening the list is
    # also what catches it up. The hourly loop covers users who never look.
    await recurring_service.post_due(db, user_id=user_id)

    request_data = await request.json()
    result = await expense_service.get_all_expenses_for_user(db, user_id, request_data)
    return result


# ─── Bulk import ────────────────────────────────────────────────────────
#
# One contract, three producers: our own model, a chatbot the person pasted a
# prompt into, and a bank statement their device parsed. Preview and commit are
# separate calls so nothing is written until the person has seen the rows.


@router.get("/expense/bulk/prompt")
async def get_bulk_prompt(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """The prompt to paste into whichever chatbot the person already uses."""
    account = await user_service.get_user_account(db, current_user["uid"])
    return {
        "prompt": bulk_expense_service.build_prompt(
            currency=(account or {}).get("defaultCurrency", "")
        ),
        "fields": bulk_expense_service.ROW_FIELDS,
        "maxRows": bulk_expense_service.MAX_ROWS,
    }


@router.post("/expense/bulk/preview")
async def preview_bulk_expenses(
    request: BulkExpenseRequest,
    current_user: dict = Depends(get_current_user),
):
    """Check a batch and report exactly what would be created. Writes nothing."""
    return bulk_expense_service.validate(request.payload)


@router.post("/expense/bulk")
async def create_bulk_expenses(
    request: BulkExpenseRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Create every row that validates, and report the ones that did not.

    Re-validates rather than trusting the preview: the client may have edited
    rows in between, and the preview is explicitly a read-only opinion.
    """
    user_id = await user_service.get_user_internal_id(db, current_user["uid"])
    if not user_id:
        raise HTTPException(status_code=404, detail="User not found")

    checked = bulk_expense_service.validate(request.payload)
    if not checked.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=checked.get("error") or "Nothing in that batch could be imported.",
        )

    created, failed = [], []
    for row in checked["rows"]:
        if not row.get("ok"):
            failed.append(row)
            continue
        data = dict(row["expense"])
        # A personal expense is paid by, and owed by, its creator in full;
        # create_expense fills the shares in. Group rows are not supported yet.
        data["type"] = "normal"
        result = await expense_service.create_expense(db, user_id, data)
        if result.get("success") == "false":
            failed.append({**row, "ok": False, "errors": [result.get("message", "rejected")]})
        else:
            created.append(result.get("expenseId") or result.get("id"))

    return {
        "imported": len(created),
        "failed": len(failed),
        "failedRows": failed,
    }
