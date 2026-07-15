from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import settings
from app.services import fx_service
from app.services.user_service import serialize_doc, get_user_name_by_id


async def get_user_events(db: AsyncIOMotorDatabase, user_id: str) -> dict:
    """Get all events for a user with computed dues."""
    pipeline = [
        {"$match": {"users": ObjectId(user_id)}},
        {
            "$addFields": {
                "expenses": {"$ifNull": ["$expenses", []]},
                "users": {"$ifNull": ["$users", []]},
            }
        },
        {
            "$lookup": {
                "from": "expense",
                "let": {"expensesIds": "$expenses"},
                "pipeline": [
                    {"$match": {"$expr": {"$in": ["$_id", "$$expensesIds"]}}},
                    {"$project": {"_id": 1, "payShares": 1, "shares": 1}},
                ],
                "as": "eventExpenses",
            }
        },
        {
            "$lookup": {
                "from": "user",
                "let": {"userIds": "$users"},
                "pipeline": [
                    {"$match": {"$expr": {"$in": ["$_id", "$$userIds"]}}},
                    {"$project": {"_id": 1, "name": 1, "email": 1}},
                ],
                "as": "eventUsers",
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "eventName": {"$first": "$eventName"},
                "description": {"$first": "$description"},
                "currency": {"$first": "$currency"},
                "createdAt": {"$first": "$createdAt"},
                "createdBy": {"$first": "$createdBy"},
                "users": {"$first": "$users"},
                "expenses": {"$first": "$eventExpenses"},
                "eventUsers": {"$push": "$eventUsers"},
            }
        },
    ]

    events = await db.event.aggregate(pipeline).to_list(length=None)
    events_list = []
    overall_owe_amount = 0
    owing_person = "user"
    user_map = {}
    same_name_dict = {}
    same_name_list = set()

    # Build user map
    for event in events:
        for user_list in event.get("eventUsers", []):
            for user in user_list:
                user_map[str(user["_id"])] = user

    user_name = user_map.get(str(user_id), {}).get("name", "You")

    # Overall totals are shown in the viewer's default currency; per-event dues
    # stay in the event's own currency.
    viewer = await db.users.find_one(
        {"_id": ObjectId(user_id)}, {"defaultCurrency": 1}
    )
    viewer_currency = fx_service.normalize_currency(
        (viewer or {}).get("defaultCurrency")
    )
    event_currencies = {
        fx_service.normalize_currency(e.get("currency")) for e in events
    }
    to_viewer_rates = await fx_service.get_rates_to(
        viewer_currency, event_currencies, db=db
    )

    for event in events:
        event_currency = fx_service.normalize_currency(event.get("currency"))
        event_rate = to_viewer_rates.get(event_currency, 1.0)
        event_dict = {
            "id": str(event["_id"]),
            "users": [str(u) for u in event.get("users", [])],
            "eventName": event.get("eventName", ""),
            "description": event.get("description", ""),
            "currency": event_currency,
            "createdAt": str(event.get("createdAt", "")),
            "createdBy": str(event.get("createdBy", "")),
            "expenses": [str(exp["_id"]) for exp in event.get("expenses", [])],
            "dues": {
                "userName": user_name,
                "currency": event_currency,
                "inDebtTo": [],
                "isOwed": [],
                "totalDebt": 0,
                "totalOwed": 0,
            },
        }

        due_dict = {}
        for expense in event.get("expenses", []):
            shares = expense.get("shares", [])
            pay_shares = expense.get("payShares", [])
            total_paid = (
                sum(float(ps["amount"]) for ps in pay_shares) if pay_shares else 0
            )

            for share in shares:
                participant_id = str(share["userId"])
                share_amount = float(share["amount"])

                name = user_map.get(participant_id, {}).get("name", "")
                email = user_map.get(participant_id, {}).get("email", "")
                if name:
                    if name not in same_name_dict:
                        same_name_dict[name] = email
                    elif email != same_name_dict[name]:
                        same_name_list.add(name)

                for pay_share in pay_shares:
                    payer_id = str(pay_share["userId"])
                    payer_amount = float(pay_share["amount"])
                    if total_paid == 0:
                        continue

                    proportional_share = share_amount * (payer_amount / total_paid)

                    if participant_id == payer_id:
                        continue

                    if participant_id == str(user_id):
                        due_dict[payer_id] = (
                            due_dict.get(payer_id, 0.0) + proportional_share
                        )
                    elif payer_id == str(user_id):
                        due_dict[participant_id] = (
                            due_dict.get(participant_id, 0.0) - proportional_share
                        )

        for due_id, amount in due_dict.items():
            user_data = user_map.get(due_id, {})
            name = user_data.get("name", "Unknown")
            if name in same_name_list:
                name += f" ( {user_data.get('email', '')} )"
            abs_amount = float(abs(amount))

            if amount < 0:
                event_dict["dues"]["isOwed"].append(
                    {"id": str(due_id), "name": name, "amount": abs_amount}
                )
                event_dict["dues"]["totalOwed"] += abs_amount
                overall_owe_amount += amount * event_rate
            else:
                event_dict["dues"]["inDebtTo"].append(
                    {"id": str(due_id), "name": name, "amount": abs_amount}
                )
                event_dict["dues"]["totalDebt"] += abs_amount
                overall_owe_amount += amount * event_rate

        events_list.append(event_dict)

    if overall_owe_amount < 0:
        owing_person = "friend"
        overall_owe_amount = abs(overall_owe_amount)

    return {
        "overallOweAmount": round(float(overall_owe_amount), 2),
        "owingPerson": owing_person,
        "currency": viewer_currency,
        "events": events_list,
    }


async def get_event_by_id(db: AsyncIOMotorDatabase, event_id: str) -> dict | None:
    """Get single event with populated user details."""
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        return None

    user_data = []
    for user_ref in event.get("users", []):
        user = await db.users.find_one(
            {"_id": user_ref if isinstance(user_ref, ObjectId) else ObjectId(user_ref)}
        )
        if user:
            user_data.append(
                {
                    "id": str(user["_id"]),
                    "name": user.get("name", ""),
                    "email": user.get("email", ""),
                }
            )

    result = serialize_doc(event)
    result["users"] = user_data
    return result


async def save_event(
    db: AsyncIOMotorDatabase, user_id: str, request_data: dict
) -> dict:
    """Create or update an event."""
    now = datetime.now(timezone.utc)

    if "id" in request_data and request_data["id"]:
        # Update existing event
        event_id = request_data["id"]
        event = await db.event.find_one({"_id": ObjectId(event_id)})
        if not event:
            raise ValueError("Event not found")

        # Resolve users - could be ObjectId strings or email addresses
        new_user_ids = await _resolve_users(db, request_data.get("users", []))

        original_set = set(str(u) for u in event.get("users", []))
        modified_set = set(str(u) for u in new_user_ids)
        removed_elements = original_set - modified_set

        if str(event.get("createdBy")) in removed_elements:
            raise ValueError("Cannot remove event creator")

        # Check removed users aren't in any expenses
        for expense_ref in event.get("expenses", []):
            expense = await db.expense.find_one({"_id": expense_ref if isinstance(expense_ref, ObjectId) else ObjectId(expense_ref)})
            if expense:
                for ps in expense.get("payShares", []):
                    if str(ps["userId"]) in removed_elements:
                        raise ValueError("Cannot remove user who has paid for expenses")
                for s in expense.get("shares", []):
                    if str(s["userId"]) in removed_elements:
                        raise ValueError("Cannot remove user who is in expense shares")

        update_data = {
            "eventName": request_data.get("eventName", event.get("eventName")),
            "description": request_data.get("description", event.get("description", "")),
            "users": new_user_ids,
            "updatedBy": ObjectId(user_id),
            "updatedAt": now,
        }

        # Currency can only change while the event has no expenses — otherwise
        # stored converted amounts would no longer match the event currency.
        new_currency = request_data.get("currency")
        if new_currency:
            new_currency = fx_service.normalize_currency(new_currency)
            current_currency = fx_service.normalize_currency(event.get("currency"))
            if new_currency != current_currency:
                if event.get("expenses"):
                    raise ValueError(
                        "Cannot change event currency after expenses are added"
                    )
                update_data["currency"] = new_currency

        await db.event.update_one({"_id": ObjectId(event_id)}, {"$set": update_data})

        # Auto-add friends
        for uid in new_user_ids:
            if str(uid) != user_id:
                await _auto_add_friend(db, user_id, str(uid))

        return await get_event_by_id(db, event_id)
    else:
        # Create new event
        user_ids = await _resolve_users(db, request_data.get("users", []))

        # Ensure creator is in the list
        creator_oid = ObjectId(user_id)
        if creator_oid not in user_ids:
            user_ids.insert(0, creator_oid)

        # Event currency: explicit choice, else the creator's default currency
        currency = request_data.get("currency")
        if not currency:
            creator = await db.users.find_one(
                {"_id": creator_oid}, {"defaultCurrency": 1}
            )
            currency = (creator or {}).get("defaultCurrency")
        currency = fx_service.normalize_currency(currency)

        new_event = {
            "eventName": request_data.get("eventName", "Untitled Event"),
            "description": request_data.get("description", ""),
            "currency": currency,
            "users": user_ids,
            "expenses": [],
            "createdBy": creator_oid,
            "createdAt": now,
            "updatedBy": creator_oid,
            "updatedAt": now,
        }
        result = await db.event.insert_one(new_event)

        # Auto-add friends
        for uid in user_ids:
            if str(uid) != user_id:
                await _auto_add_friend(db, user_id, str(uid))

        new_event["_id"] = result.inserted_id
        return await get_event_by_id(db, str(result.inserted_id))


async def _resolve_users(db: AsyncIOMotorDatabase, user_refs: list) -> list:
    """Resolve a list of user references (ObjectId strings) to ObjectIds."""
    user_ids = []
    for ref in user_refs:
        if isinstance(ref, dict):
            ref = ref.get("id", ref.get("_id", ""))
        ref = str(ref)
        try:
            user_ids.append(ObjectId(ref))
        except Exception:
            # Try looking up by email
            user = await db.users.find_one({"email": ref})
            if user:
                user_ids.append(user["_id"])
    return user_ids


async def _auto_add_friend(db: AsyncIOMotorDatabase, user_id: str, friend_id: str):
    """Silently add a friend if they're not already friends."""
    user_oid = ObjectId(user_id)
    friend_oid = ObjectId(friend_id)

    # Add friend to user's list
    await db.users.update_one(
        {"_id": user_oid, "friends": {"$ne": friend_oid}},
        {"$addToSet": {"friends": friend_oid}},
    )
    # Add user to friend's list
    await db.users.update_one(
        {"_id": friend_oid, "friends": {"$ne": user_oid}},
        {"$addToSet": {"friends": user_oid}},
    )


async def delete_event(db: AsyncIOMotorDatabase, event_id: str) -> dict:
    """Delete an event and its expenses (if no unsettled dues)."""
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        return {"message": "Event not found", "success": "false"}

    # Check for unsettled dues
    dues = await get_event_dues(db, event_id)
    if dues and "eventDues" in dues:
        for due in dues["eventDues"]:
            if due.get("creditorDetails") and len(due["creditorDetails"]) > 0:
                return {
                    "message": "Cannot delete event. There are unsettled dues.",
                    "success": "false",
                }

    # Delete all expenses
    for expense_ref in event.get("expenses", []):
        exp_id = expense_ref if isinstance(expense_ref, ObjectId) else ObjectId(expense_ref)
        await db.expense.delete_one({"_id": exp_id})

    await db.event.delete_one({"_id": ObjectId(event_id)})
    return {"message": "Successfully deleted event", "success": "true"}


async def get_event_dues(db: AsyncIOMotorDatabase, event_id: str) -> dict:
    """Calculate who owes whom within an event."""
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        return {"error": "Event not found"}

    # Get all expenses for the event
    expense_ids = []
    for exp_ref in event.get("expenses", []):
        exp_id = exp_ref if isinstance(exp_ref, ObjectId) else ObjectId(exp_ref)
        expense_ids.append(exp_id)

    expenses = await db.expense.find({"_id": {"$in": expense_ids}}).to_list(
        length=None
    )

    amounts_owed = {}
    user_name_map = {}
    calculated_list = []

    for expense in expenses:
        shares = expense.get("shares", [])
        pay_shares = expense.get("payShares", [])

        for pay_share in pay_shares:
            payer_id = str(pay_share["userId"])

            for share in shares:
                share_amount = float(share["amount"])
                participant_id = str(share["userId"])

                if participant_id == payer_id:
                    continue

                if participant_id not in amounts_owed:
                    amounts_owed[participant_id] = {}
                if payer_id not in amounts_owed[participant_id]:
                    amounts_owed[participant_id][payer_id] = 0.0

                total_paid = sum(float(ps["amount"]) for ps in pay_shares)
                payer_ratio = (
                    float(pay_share["amount"]) / total_paid if total_paid != 0 else 0
                )
                proportional_share = share_amount * payer_ratio

                amounts_owed[participant_id][payer_id] += proportional_share

    # Resolve net debts
    for participant, debts in amounts_owed.items():
        for payer, amount in debts.items():
            key1 = payer + participant
            key2 = participant + payer
            if key1 not in calculated_list and key2 not in calculated_list:
                reverse_exist = False
                payer_to_participant = 0
                participant_to_payer = amount

                if payer in amounts_owed and participant in amounts_owed[payer]:
                    reverse_exist = True
                    payer_to_participant = amounts_owed[payer][participant]

                net_amount = participant_to_payer - payer_to_participant
                if net_amount > 0:
                    amounts_owed[participant][payer] = net_amount
                    if reverse_exist:
                        amounts_owed[payer][participant] = 0
                elif net_amount < 0:
                    if not reverse_exist:
                        amounts_owed[payer] = amounts_owed.get(payer, {})
                    amounts_owed[payer][participant] = -net_amount
                    amounts_owed[participant][payer] = 0
                else:
                    if reverse_exist:
                        amounts_owed[payer][participant] = 0
                    amounts_owed[participant][payer] = 0

                if payer not in user_name_map:
                    user_name_map[payer] = await get_user_name_by_id(db, payer)
                if participant not in user_name_map:
                    user_name_map[participant] = await get_user_name_by_id(
                        db, participant
                    )

                calculated_list.append(key1)

    # Format response
    event_dues = []
    for debtor in amounts_owed:
        debtor_name = user_name_map.get(str(debtor), "Unknown")
        creditor_details = []
        for creditor in amounts_owed[debtor]:
            amount = amounts_owed[debtor][creditor]
            if amount != 0:
                creditor_name = user_name_map.get(str(creditor), "Unknown")
                creditor_details.append(
                    {"id": creditor, "name": creditor_name, "amount": amount}
                )
        event_dues.append(
            {"id": debtor, "debtor": debtor_name, "creditorDetails": creditor_details}
        )

    return {
        "eventDues": event_dues,
        "currency": event.get("currency", settings.DEFAULT_CURRENCY),
    }


async def get_event_dues_for_user(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> dict:
    """Calculate who owes whom for a specific user within an event."""
    result = {
        "userName": "",
        "currency": settings.DEFAULT_CURRENCY,
        "inDebtTo": [],
        "isOwed": [],
        "totalDebt": 0,
        "totalOwed": 0,
    }

    user_name = await get_user_name_by_id(db, user_id)
    result["userName"] = user_name

    event_dues_summary = await get_event_dues(db, event_id)
    result["currency"] = event_dues_summary.get(
        "currency", settings.DEFAULT_CURRENCY
    )

    if event_dues_summary and "eventDues" in event_dues_summary:
        for person_in_debt in event_dues_summary["eventDues"]:
            if person_in_debt["id"] == user_id:
                for owed_person in person_in_debt.get("creditorDetails", []):
                    result["inDebtTo"].append(owed_person)
                    result["totalDebt"] += owed_person["amount"]
            else:
                for owed_person in person_in_debt.get("creditorDetails", []):
                    if owed_person["id"] == user_id:
                        result["isOwed"].append(
                            {
                                "id": person_in_debt["id"],
                                "name": person_in_debt["debtor"],
                                "amount": owed_person["amount"],
                            }
                        )
                        result["totalOwed"] += owed_person["amount"]

    return result


async def get_event_or_friend_users(
    db: AsyncIOMotorDatabase, user_id: str, context_type: str, context_id: str
) -> list:
    """Get users related to a context (event or friend)."""
    users = []
    if context_type == "friend":
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if user:
            users.append(
                {
                    "name": user.get("name", ""),
                    "email": user.get("email", ""),
                    "id": str(user["_id"]),
                }
            )
        friend = await db.users.find_one({"_id": ObjectId(context_id)})
        if friend:
            users.append(
                {
                    "name": friend.get("name", ""),
                    "email": friend.get("email", ""),
                    "id": str(friend["_id"]),
                }
            )
    elif context_type == "event":
        event = await db.event.find_one({"_id": ObjectId(context_id)})
        if event:
            for user_ref in event.get("users", []):
                uid = user_ref if isinstance(user_ref, ObjectId) else ObjectId(user_ref)
                user = await db.users.find_one({"_id": uid})
                if user:
                    users.append(
                        {
                            "name": user.get("name", ""),
                            "email": user.get("email", ""),
                            "id": str(user["_id"]),
                        }
                    )
    return users
