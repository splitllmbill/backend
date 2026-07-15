from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import settings
from app.services import fx_service
from app.services.user_service import serialize_doc


async def _resolve_base_currency(
    db: AsyncIOMotorDatabase, user_id: str, event: dict | None
) -> str:
    """Context currency: the event's currency, else the creator's default."""
    if event is not None:
        return fx_service.normalize_currency(event.get("currency"))
    creator = await db.users.find_one({"_id": ObjectId(user_id)}, {"defaultCurrency": 1})
    return fx_service.normalize_currency((creator or {}).get("defaultCurrency"))


async def create_expense(
    db: AsyncIOMotorDatabase, user_id: str, request_data: dict
) -> dict:
    """Create a new expense."""
    expense_name = request_data.get("expenseName", request_data.get("title", ""))
    amount = request_data.get("amount", 0)
    category = request_data.get("category", "General")

    if not expense_name or amount == 0:
        return {
            "message": "There are some missing/invalid fields. Please check your input and try again!",
            "success": "false",
        }

    expense_type = request_data.get("type", "normal")
    shares = request_data.get("shares", [])
    pay_shares = request_data.get("payShares", [])

    if expense_type != "normal" and (len(pay_shares) == 0 or len(shares) == 0):
        return {
            "message": "There are some missing/invalid fields. Please check your input and try again!",
            "success": "false",
        }

    # Validate shares total (in the entered/transaction currency)
    share_total = sum(float(s.get("amount", 0)) for s in shares)
    if expense_type != "normal" and abs(share_total - amount) > 1:
        raise ValueError("Expense amount not equal to sum of shares")

    now = datetime.now(timezone.utc)

    # Parse expense date up-front — FX rates are date-sensitive
    expense_date = request_data.get("date") or now
    if isinstance(expense_date, str):
        try:
            expense_date = datetime.fromisoformat(expense_date.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            expense_date = now

    # Resolve event first: it defines the base (context) currency
    event = None
    event_id = request_data.get("eventId")
    if expense_type in ["group", "settle"] and event_id and event_id != "":
        event = await db.event.find_one({"_id": ObjectId(event_id)})
        if not event:
            raise ValueError("Event not found for the provided eventId")

    base_currency = await _resolve_base_currency(db, user_id, event)
    currency = fx_service.normalize_currency(
        request_data.get("currency") or base_currency
    )
    fx_rate = await fx_service.get_rate(currency, base_currency, on=expense_date, db=db)

    # Amounts are stored converted to the base currency; the entered amounts
    # are kept as originalAmount for display/editing.
    db_shares = []
    for share in shares:
        share_amount = float(share["amount"])
        db_shares.append(
            {
                "userId": ObjectId(share["userId"]),
                "amount": round(share_amount * fx_rate, 2),
                "originalAmount": share_amount,
                "eventId": ObjectId(share["eventId"]) if share.get("eventId") else None,
            }
        )

    db_pay_shares = []
    for ps in pay_shares:
        ps_amount = float(ps["amount"])
        db_pay_shares.append(
            {
                "userId": ObjectId(ps["userId"]),
                "amount": round(ps_amount * fx_rate, 2),
                "originalAmount": ps_amount,
            }
        )

    new_expense = {
        "expenseName": expense_name,
        "amount": round(float(amount) * fx_rate, 2),
        "originalAmount": float(amount),
        "currency": currency,
        "baseCurrency": base_currency,
        "fxRate": fx_rate,
        "type": expense_type,
        "category": category,
        "date": expense_date,
        "shares": db_shares,
        "payShares": db_pay_shares,
        "createdBy": ObjectId(user_id),
        "updatedBy": ObjectId(user_id),
        "createdAt": now,
        "updatedAt": now,
    }

    if event is not None:
        new_expense["eventId"] = ObjectId(event_id)
        result = await db.expense.insert_one(new_expense)
        new_expense["_id"] = result.inserted_id
        # Add expense to event
        await db.event.update_one(
            {"_id": ObjectId(event_id)},
            {"$push": {"expenses": result.inserted_id}},
        )
    else:
        result = await db.expense.insert_one(new_expense)
        new_expense["_id"] = result.inserted_id

    return {
        "message": "Expense created!",
        "success": "true",
        "data": serialize_doc(new_expense),
    }


async def get_expense_by_id(
    db: AsyncIOMotorDatabase, expense_id: str, user_id: str
) -> dict | None:
    """Get expense by ID with populated user names in shares."""
    expense = await db.expense.find_one({"_id": ObjectId(expense_id)})
    if not expense:
        return None

    # Populate shares with user names. Amounts are surfaced in the transaction
    # currency (originalAmount) so edit forms round-trip what was entered.
    new_shares = []
    for share in expense.get("shares", []):
        user = await db.users.find_one({"_id": share["userId"]})
        new_shares.append(
            {
                "userId": str(share["userId"]),
                "name": user["name"] if user else "Unknown",
                "email": user["email"] if user else "",
                "amount": float(share.get("originalAmount", share["amount"])),
                "convertedAmount": float(share["amount"]),
            }
        )

    new_pay_shares = []
    for ps in expense.get("payShares", []):
        user = await db.users.find_one({"_id": ps["userId"]})
        new_pay_shares.append(
            {
                "userId": str(ps["userId"]),
                "name": user["name"] if user else "Unknown",
                "email": user["email"] if user else "",
                "amount": float(ps.get("originalAmount", ps["amount"])),
                "convertedAmount": float(ps["amount"]),
            }
        )

    # Determine createdBy display name
    created_by_name = "you"
    if str(expense.get("createdBy")) != user_id:
        creator = await db.users.find_one({"_id": expense["createdBy"]})
        created_by_name = creator["name"] if creator else "Unknown"

    updated_by_name = "you"
    if str(expense.get("updatedBy")) != user_id:
        updater = await db.users.find_one({"_id": expense["updatedBy"]})
        updated_by_name = updater["name"] if updater else "Unknown"

    event_id = "None"
    event_name = "None"
    if expense.get("eventId"):
        event_id = str(expense["eventId"])
        event = await db.event.find_one({"_id": expense["eventId"]})
        if event:
            event_name = event.get("eventName", "None")

    return {
        "id": str(expense["_id"]),
        "expenseName": expense.get("expenseName", ""),
        "amount": float(expense.get("originalAmount", expense.get("amount", 0))),
        "convertedAmount": float(expense.get("amount", 0)),
        "currency": expense.get("currency", settings.DEFAULT_CURRENCY),
        "baseCurrency": expense.get("baseCurrency", settings.DEFAULT_CURRENCY),
        "fxRate": float(expense.get("fxRate", 1.0)),
        "type": expense.get("type", ""),
        "payShares": new_pay_shares,
        "shares": new_shares,
        "createdAt": str(expense.get("createdAt", "")),
        "updatedAt": str(expense.get("updatedAt", "")),
        "createdBy": created_by_name,
        "updatedBy": updated_by_name,
        "category": expense.get("category", ""),
        "date": str(expense.get("date", "")),
        "eventId": event_id,
        "eventName": event_name,
    }


async def update_expense(
    db: AsyncIOMotorDatabase, user_id: str, expense_id: str, request_data: dict
) -> dict:
    """Update an existing expense."""
    expense_name = request_data.get("expenseName", request_data.get("title", ""))
    category = request_data.get("category", "")
    amount = request_data.get("amount", 0)

    if not expense_name or not category or amount == 0:
        return {
            "message": "There are some missing/invalid fields. Please check your input and try again!",
            "success": "false",
        }

    expense = await db.expense.find_one({"_id": ObjectId(expense_id)})
    if not expense:
        return {"message": "Expense not found", "success": "false"}

    # Re-resolve the FX conversion: the transaction currency may have changed,
    # while the base (context) currency stays with the event/creator.
    event = None
    if expense.get("eventId"):
        event = await db.event.find_one({"_id": expense["eventId"]})
    base_currency = fx_service.normalize_currency(
        expense.get("baseCurrency")
        or (event or {}).get("currency")
        or settings.DEFAULT_CURRENCY
    )
    currency = fx_service.normalize_currency(
        request_data.get("currency") or expense.get("currency") or base_currency
    )

    date_val = expense.get("date")
    if "date" in request_data and request_data["date"]:
        date_val = request_data["date"]
        if isinstance(date_val, str):
            try:
                date_val = datetime.fromisoformat(date_val.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                date_val = expense.get("date")

    fx_rate = await fx_service.get_rate(currency, base_currency, on=date_val, db=db)

    update_data = {
        "expenseName": expense_name,
        "amount": round(float(amount) * fx_rate, 2),
        "originalAmount": float(amount),
        "currency": currency,
        "baseCurrency": base_currency,
        "fxRate": fx_rate,
        "category": category,
        "updatedAt": datetime.now(timezone.utc),
        "updatedBy": ObjectId(user_id),
    }

    if "date" in request_data and request_data["date"]:
        update_data["date"] = date_val

    if "type" in request_data:
        update_data["type"] = request_data["type"]

    if "payShares" in request_data:
        new_pay_shares = []
        for ps in request_data["payShares"]:
            ps_amount = float(ps["amount"])
            new_pay_shares.append(
                {
                    "userId": ObjectId(ps["userId"]),
                    "amount": round(ps_amount * fx_rate, 2),
                    "originalAmount": ps_amount,
                }
            )
        update_data["payShares"] = new_pay_shares

    if "shares" in request_data:
        new_shares = []
        for s in request_data["shares"]:
            s_amount = float(s["amount"])
            new_shares.append(
                {
                    "userId": ObjectId(s["userId"]),
                    "amount": round(s_amount * fx_rate, 2),
                    "originalAmount": s_amount,
                    "eventId": ObjectId(s["eventId"]) if s.get("eventId") else None,
                }
            )
        update_data["shares"] = new_shares

    await db.expense.update_one({"_id": ObjectId(expense_id)}, {"$set": update_data})
    return {"message": "Successfully updated expense", "success": "true"}


async def delete_expense(db: AsyncIOMotorDatabase, expense_id: str) -> dict:
    """Delete an expense and remove from its event if applicable."""
    expense = await db.expense.find_one({"_id": ObjectId(expense_id)})
    if not expense:
        return {"message": "Expense not found", "success": "false"}

    # Remove from event if linked
    if expense.get("eventId"):
        await db.event.update_one(
            {"_id": expense["eventId"]},
            {"$pull": {"expenses": expense["_id"]}},
        )

    await db.expense.delete_one({"_id": ObjectId(expense_id)})
    return {"message": "Successfully deleted expense", "success": "true"}


async def get_event_expenses(db: AsyncIOMotorDatabase, event_id: str) -> list:
    """Get all expenses for an event."""
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        return []

    expense_ids = []
    for exp_ref in event.get("expenses", []):
        exp_id = exp_ref if isinstance(exp_ref, ObjectId) else ObjectId(exp_ref)
        expense_ids.append(exp_id)

    expenses = await db.expense.find({"_id": {"$in": expense_ids}}).to_list(
        length=None
    )
    return expenses


async def get_event_expenses_with_summary(
    db: AsyncIOMotorDatabase, user_id: str, event_id: str
) -> dict:
    """Get event expenses with per-user summary (who owes whom per expense)."""
    expenses = await get_event_expenses(db, event_id)
    event = await db.event.find_one({"_id": ObjectId(event_id)})

    expenses_with_summary = []
    for expense in expenses:
        summary = _get_expense_share_summary(expense, user_id)
        expenses_with_summary.append(
            {
                "expenseName": expense.get("expenseName", ""),
                "expenseId": str(expense["_id"]),
                "expenseDate": str(expense.get("date", "")),
                "type": expense.get("type", ""),
                "amount": float(expense.get("amount", 0)),
                "currency": expense.get("baseCurrency", settings.DEFAULT_CURRENCY),
                "originalAmount": float(
                    expense.get("originalAmount", expense.get("amount", 0))
                ),
                "originalCurrency": expense.get("currency", settings.DEFAULT_CURRENCY),
                "category": expense.get("category", ""),
                "user_summary": summary,
            }
        )

    return {
        "eventName": event.get("eventName", "") if event else "",
        "currency": (event or {}).get("currency", settings.DEFAULT_CURRENCY),
        "expenses": expenses_with_summary,
    }


def _get_expense_share_summary(expense: dict, user_id: str) -> dict:
    """Calculate the owe summary for a user on a single expense."""
    user_owe = 0
    friends_owe = 0
    pay_share_user_ids = [str(ps["userId"]) for ps in expense.get("payShares", [])]

    if user_id in pay_share_user_ids:
        for share in expense.get("shares", []):
            if str(share["userId"]) != user_id:
                friends_owe += float(share["amount"])
    else:
        for share in expense.get("shares", []):
            if str(share["userId"]) == user_id:
                user_owe += float(share["amount"])

    return {
        "oweAmount": float(abs(user_owe - friends_owe)),
        "owePerson": "user" if user_owe > friends_owe else "friend",
    }


async def get_all_expenses_for_user(
    db: AsyncIOMotorDatabase, user_id: str, request_data: dict
) -> list:
    """Get all personal expenses for a user with optional filters."""
    user_object_id = ObjectId(user_id)
    filters = request_data.get("filters", [])

    query = {
        "type": "normal",
        "payShares": {"$elemMatch": {"userId": user_object_id}},
    }

    # Apply filters
    operator_map = {"IN": "$in"}
    for f in filters:
        field = f.get("field", "")
        operator = f.get("operator", "")
        values = f.get("values", [])

        if operator == "IN":
            lowercase_values = [v.lower() for v in values]
            query[field] = {"$in": lowercase_values}
        elif operator == "BTW":
            if values[0] != "MIN":
                query.setdefault(field, {})
                query[field]["$gte"] = float(values[0])
            if len(values) > 1 and values[1] != "MAX":
                query.setdefault(field, {})
                query[field]["$lte"] = float(values[1])

    expenses = (
        await db.expense.find(query)
        .sort([("date", -1), ("updatedAt", -1)])
        .to_list(length=None)
    )

    return [serialize_doc(e) for e in expenses]
