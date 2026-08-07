from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import settings
from app.services import category_service, fx_service
from app.services.user_service import serialize_doc


def _entry_amount(entry: dict) -> float:
    """A share/payShare amount in the currency the expense was entered in."""
    value = entry.get("originalAmount")
    if value is None:
        value = entry.get("amount", 0)
    return float(value or 0)


async def user_can_access_expense(
    db: AsyncIOMotorDatabase, expense: dict, user_id: str
) -> bool:
    """True if the user created the expense, is a participant, or is a member
    of the event it belongs to. Guards read/update/delete against IDOR."""
    if str(expense.get("createdBy")) == user_id:
        return True
    participants = {
        str(s.get("userId"))
        for s in expense.get("shares", []) + expense.get("payShares", [])
    }
    if user_id in participants:
        return True
    event_ref = expense.get("eventId")
    if event_ref:
        try:
            event = await db.event.find_one(
                {"_id": event_ref if isinstance(event_ref, ObjectId) else ObjectId(event_ref)},
                {"users": 1},
            )
        except Exception:
            event = None
        if event and ObjectId(user_id) in event.get("users", []):
            return True
    return False


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
    # Settlements carry their own marker rather than a spending category.
    if expense_type != "settle":
        category = category_service.normalize(category)
    shares = request_data.get("shares", [])
    pay_shares = request_data.get("payShares", [])

    # A personal ("normal") expense is implicitly paid by — and owed by — its
    # creator for the full amount. The split UI only sends shares/payShares for
    # multi-person expenses, so without this a personal expense is stored with
    # empty payShares and then never matches the personal-list / dashboard
    # queries (which filter on payShares.userId), silently disappearing from
    # the user's own history.
    if expense_type == "normal":
        if not pay_shares:
            pay_shares = [{"userId": user_id, "amount": amount}]
        if not shares:
            shares = [{"userId": user_id, "amount": amount}]

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

    # Fingerprint of the source row when this came from an import, so importing
    # the same export again only brings in what is new.
    if request_data.get("importKey"):
        new_expense["importKey"] = str(request_data["importKey"])

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

    # Teaches the category table what this title is usually filed under.
    if expense_type != "settle":
        await category_service.learn(db, expense_name, category, user_id)

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
    # Hide expenses the caller isn't part of (return None -> router 404).
    if not await user_can_access_expense(db, expense, user_id):
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
        "paidByName": ", ".join(ps["name"] for ps in new_pay_shares)
        if new_pay_shares
        else "Unknown",
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
    if not await user_can_access_expense(db, expense, user_id):
        raise HTTPException(
            status_code=403, detail="You do not have access to this expense"
        )

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

    if expense.get("type") != "settle":
        category = category_service.normalize(category)

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

    # A correction is the most valuable signal the table gets.
    if expense.get("type") != "settle":
        await category_service.learn(db, expense_name, category, user_id)

    return {"message": "Successfully updated expense", "success": "true"}


async def delete_expense(
    db: AsyncIOMotorDatabase, expense_id: str, user_id: str
) -> dict:
    """Delete an expense and remove from its event if applicable."""
    expense = await db.expense.find_one({"_id": ObjectId(expense_id)})
    if not expense:
        return {"message": "Expense not found", "success": "false"}
    if not await user_can_access_expense(db, expense, user_id):
        raise HTTPException(
            status_code=403, detail="You do not have access to this expense"
        )

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
    """Get event expenses with per-user summary (who owes whom per expense).

    Each row carries the amount in the currency it was actually paid in *and*
    the same amount converted into the group currency at the viewer's own
    rates, so the list can be shown either way without a second round trip.
    """
    from app.services import event_service

    expenses = await get_event_expenses(db, event_id)
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    base_currency = fx_service.normalize_currency((event or {}).get("currency"))
    currencies = {
        fx_service.normalize_currency(e.get("currency")) for e in expenses
    }
    rates = await event_service.get_member_rates(
        db, event or {}, user_id, currencies
    )

    # One lookup for every name on the list, rather than one per expense.
    participant_ids = {
        entry["userId"]
        for expense in expenses
        for entry in expense.get("payShares", []) + expense.get("shares", [])
        if entry.get("userId")
    }
    names = {}
    if participant_ids:
        async for user in db.users.find(
            {"_id": {"$in": list(participant_ids)}}, {"name": 1}
        ):
            names[str(user["_id"])] = user.get("name", "Unknown")

    def _names(entries: list) -> str:
        return ", ".join(
            "You" if str(e["userId"]) == str(user_id)
            else names.get(str(e["userId"]), "Unknown")
            for e in entries
        )

    expenses_with_summary = []
    for expense in expenses:
        original_currency = fx_service.normalize_currency(expense.get("currency"))
        rate = rates.get(original_currency, 1.0)
        original_amount = expense.get("originalAmount")
        if original_amount is None:
            original_amount = expense.get("amount", 0)
        original_amount = float(original_amount or 0)
        summary = _get_expense_share_summary(expense, user_id)
        expenses_with_summary.append(
            {
                "expenseName": expense.get("expenseName", ""),
                "expenseId": str(expense["_id"]),
                "expenseDate": str(expense.get("date", "")),
                "type": expense.get("type", ""),
                "paidByName": _names(expense.get("payShares", [])),
                # For a settlement the shares side is who received the money,
                # which is what makes "A paid B" readable in the list.
                "paidToName": _names(expense.get("shares", []))
                if expense.get("type") == "settle"
                else "",
                # `amount`/`currency` stay in the group currency for callers
                # that only care about one number.
                "amount": round(original_amount * rate, 2),
                "currency": base_currency,
                "originalAmount": original_amount,
                "originalCurrency": original_currency,
                "rate": rate,
                "category": expense.get("category", ""),
                "user_summary": {
                    **summary,
                    "oweAmountConverted": round(summary["oweAmount"] * rate, 2),
                    "oweCurrency": original_currency,
                },
            }
        )

    return {
        "eventName": event.get("eventName", "") if event else "",
        "currency": base_currency,
        "currencies": sorted(currencies),
        "rates": rates,
        "expenses": expenses_with_summary,
    }


def _get_expense_share_summary(expense: dict, user_id: str) -> dict:
    """Calculate the owe summary for a user on a single expense.

    Amounts are in the expense's own currency — converting is the caller's job,
    since only it knows whose rates to use.
    """
    user_owe = 0.0
    friends_owe = 0.0
    pay_share_user_ids = [str(ps["userId"]) for ps in expense.get("payShares", [])]

    if user_id in pay_share_user_ids:
        for share in expense.get("shares", []):
            if str(share["userId"]) != user_id:
                friends_owe += _entry_amount(share)
    else:
        for share in expense.get("shares", []):
            if str(share["userId"]) == user_id:
                user_owe += _entry_amount(share)

    return {
        "oweAmount": round(abs(user_owe - friends_owe), 2),
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
    for f in filters:
        field = f.get("field", "")
        operator = f.get("operator", "")
        values = f.get("values", [])

        if operator == "IN":
            # Match stored values as-is; categories are stored capitalized
            # (e.g. "General"), so lowercasing here matched nothing.
            query[field] = {"$in": values}
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
