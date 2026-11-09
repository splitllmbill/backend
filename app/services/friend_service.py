from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from collections import defaultdict
from datetime import datetime, timezone
from app.services import expense_service, fx_service


async def _viewer_currency(db: AsyncIOMotorDatabase, user_id: str) -> str:
    user = await db.users.find_one({"_id": ObjectId(user_id)}, {"defaultCurrency": 1})
    return fx_service.normalize_currency((user or {}).get("defaultCurrency"))


async def _base_to_viewer_rates(
    db: AsyncIOMotorDatabase, user_id: str, expenses: list, viewer_currency: str
) -> dict[str, float]:
    """Rates from each expense's base currency to the viewer currency.

    Friend balances span groups in different currencies plus one-to-one
    expenses that carry no group at all, so the viewer's own rate table is the
    only thing that can reconcile them. Without it these sums silently fell
    back to 1.0 and added dollars to rupees.
    """
    bases = {
        fx_service.normalize_currency(e.get("baseCurrency")) for e in expenses
    }
    return await fx_service.get_rates_to(
        viewer_currency,
        bases,
        db=db,
        tables=[await fx_service.user_rate_table(db, user_id)],
    )


def _expense_rate(expense: dict, rates: dict[str, float]) -> float:
    return rates.get(fx_service.normalize_currency(expense.get("baseCurrency")), 1.0)


async def get_friend_list(db: AsyncIOMotorDatabase, user_id: str) -> dict:
    """Get all friends for a user with balance info."""
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return {
            "uuid": "",
            "overallYouOwe": 0,
            "overallYouAreOwed": 0,
            "friendsList": [],
        }

    friends = user.get("friends", [])
    if not friends:
        return {
            "uuid": user.get("friendCode", ""),
            "overallYouOwe": 0,
            "overallYouAreOwed": 0,
            "friendsList": [],
        }

    user_ids = friends + [ObjectId(user_id)]

    # Get user details
    users_cursor = db.users.find(
        {"_id": {"$in": user_ids}}, {"_id": 1, "name": 1, "email": 1, "friendCode": 1}
    )
    user_details = await users_cursor.to_list(length=None)
    user_map = {str(u["_id"]): u for u in user_details}

    # Get all relevant expenses
    friend_oid_strs = [str(f) for f in friends]
    expenses = await db.expense.find(
        {
            "type": {"$in": ["settle", "friend", "group"]},
            "$or": [
                {"payShares.userId": ObjectId(user_id)},
                {"payShares.userId": {"$in": friends}},
            ],
        },
        {"payShares": 1, "shares": 1, "type": 1, "baseCurrency": 1},
    ).to_list(length=None)

    # Balances span events with different currencies, so each expense is
    # converted from its base currency to the viewer's currency at current rates.
    viewer_ccy = await _viewer_currency(db, user_id)
    rates = await _base_to_viewer_rates(db, user_id, expenses, viewer_ccy)

    # Compute dues
    friend_dues = defaultdict(float)
    for expense in expenses:
        pay_shares = expense.get("payShares", [])
        shares = expense.get("shares", [])
        total_paid_amount = sum(float(p["amount"]) for p in pay_shares)
        if total_paid_amount == 0:
            continue
        rate = _expense_rate(expense, rates)

        for share in shares:
            share_user_id = str(share["userId"])
            share_amount = float(share["amount"]) * rate

            for pay in pay_shares:
                payer_id = str(pay["userId"])
                payer_amount = float(pay["amount"])
                proportion = (payer_amount / total_paid_amount) * share_amount

                if share_user_id == user_id and payer_id in friend_oid_strs:
                    friend_dues[payer_id] -= proportion
                elif payer_id == user_id and share_user_id in friend_oid_strs:
                    friend_dues[share_user_id] += proportion

    # Prepare response
    friend_list = []
    overall_you_owe = 0
    overall_you_are_owed = 0

    for friend_oid in friends:
        fid = str(friend_oid)
        if fid == user_id or fid not in user_map:
            continue

        amount = friend_dues[fid]
        if amount < 0:
            overall_you_owe += abs(amount)
        else:
            overall_you_are_owed += abs(amount)

        friend_list.append(
            {
                "id": fid,
                "name": user_map[fid].get("name", ""),
                "email": user_map[fid].get("email", ""),
                # Needed so the list can unfriend by code (delete_friend keys on
                # friendCode); without it removal silently fails.
                "friendCode": user_map[fid].get("friendCode", ""),
                "oweAmount": abs(amount),
                "whoOwes": "friend" if amount > 0 else "user",
            }
        )

    return {
        "uuid": user.get("friendCode", ""),
        "overallYouOwe": round(float(overall_you_owe), 2),
        "overallYouAreOwed": round(float(overall_you_are_owed), 2),
        "currency": viewer_ccy,
        "friendsList": friend_list,
    }


async def get_friend_details(
    db: AsyncIOMotorDatabase, user_id: str, friend_id: str
) -> dict | None:
    """Get detailed friend info with shared expenses and balance."""
    friend = await db.users.find_one({"_id": ObjectId(friend_id)})
    user = await db.users.find_one({"_id": ObjectId(user_id)})

    if not friend or not user:
        return None

    # Check if they are friends
    user_friends = user.get("friends", [])
    if ObjectId(friend_id) not in user_friends:
        return None

    # Get shared expenses
    expenses = await db.expense.find(
        {
            "type": {"$in": ["group", "friend", "settle"]},
            "$or": [
                {"payShares.userId": ObjectId(friend_id)},
                {"payShares.userId": ObjectId(user_id)},
            ],
        }
    ).to_list(length=None)

    expenses_list = []
    total_friend_owe = 0
    total_user_owe = 0

    viewer_ccy = await _viewer_currency(db, user_id)
    rates = await _base_to_viewer_rates(db, user_id, expenses, viewer_ccy)

    for expense in expenses:
        user_owe = 0
        friend_owe = 0

        pay_shares = expense.get("payShares", [])
        shares = expense.get("shares", [])
        rate = _expense_rate(expense, rates)

        user_paid = sum(
            float(ps["amount"])
            for ps in pay_shares
            if str(ps["userId"]) == user_id
        )
        friend_paid = sum(
            float(ps["amount"])
            for ps in pay_shares
            if str(ps["userId"]) == friend_id
        )
        total_paid = user_paid + friend_paid

        for share in shares:
            share_user_id = str(share["userId"])
            if share_user_id not in [user_id, friend_id]:
                continue
            if total_paid == 0:
                continue

            share_amount = float(share["amount"]) * rate
            if share_user_id == user_id:
                user_owe += share_amount * (friend_paid / total_paid)
            elif share_user_id == friend_id:
                friend_owe += share_amount * (user_paid / total_paid)

        if user_owe > 0 or friend_owe > 0:
            owe_amount = abs(friend_owe - user_owe)
            who_owes = "friend" if friend_owe > user_owe else "user"

            event_id_str = None
            event_name_str = None
            if expense.get("eventId"):
                event_id_str = str(expense["eventId"])
                event = await db.event.find_one({"_id": expense["eventId"]})
                if event:
                    event_name_str = event.get("eventName")

            expense_details = {
                "expenseDate": expense.get("createdAt", "").isoformat()
                if isinstance(expense.get("createdAt"), datetime)
                else str(expense.get("createdAt", "")),
                "expenseName": expense.get("expenseName", ""),
                "expenseId": str(expense["_id"]),
                "eventId": event_id_str,
                "eventName": event_name_str,
                "category": expense.get("category", ""),
                "user_summary": {
                    "oweAmount": float(owe_amount),
                    "owePerson": who_owes,
                },
                "expenseType": expense.get("type", ""),
            }
            expenses_list.append(expense_details)

            if who_owes == "friend":
                total_friend_owe += owe_amount
            else:
                total_user_owe += owe_amount

    total_owe_amount = abs(total_friend_owe - total_user_owe)
    overall_who_owes = "friend" if total_friend_owe > total_user_owe else "user"

    return {
        "uuid": friend.get("friendCode", ""),
        "name": friend.get("name", ""),
        "overallOweAmount": round(float(total_owe_amount), 2),
        "overallWhoOwes": overall_who_owes,
        "currency": viewer_ccy,
        "expenses": expenses_list,
    }


async def get_friend_dues(
    db: AsyncIOMotorDatabase, user_id: str, friend_id: str
) -> dict:
    """Get net dues between two users across all expenses."""
    friend = await db.users.find_one({"_id": ObjectId(friend_id)})
    user = await db.users.find_one({"_id": ObjectId(user_id)})

    if not friend or not user:
        return {"name": "", "id": "", "oweAmount": 0, "whoOwes": "user"}

    # Only expenses one of these two actually paid for can create a debt
    # between them; the unfiltered scan this replaced walked every expense in
    # the database on each call.
    expenses = await db.expense.find(
        {
            "type": {"$in": ["group", "friend", "settle"]},
            "payShares.userId": {"$in": [ObjectId(user_id), ObjectId(friend_id)]},
        }
    ).to_list(length=None)

    total_friend_owe = 0
    total_user_owe = 0

    viewer_ccy = await _viewer_currency(db, user_id)
    rates = await _base_to_viewer_rates(db, user_id, expenses, viewer_ccy)

    for expense in expenses:
        user_owe = 0
        friend_owe = 0

        pay_shares = expense.get("payShares", [])
        total_paid = sum(float(ps["amount"]) for ps in pay_shares) if pay_shares else 0
        if total_paid == 0:
            continue
        rate = _expense_rate(expense, rates)

        for share in expense.get("shares", []):
            share_amount = float(share["amount"]) * rate
            for ps in pay_shares:
                payer_id = str(ps["userId"])
                payer_amount = float(ps["amount"])
                proportion = (payer_amount / total_paid) * share_amount

                if str(share["userId"]) == user_id and payer_id == friend_id:
                    user_owe += proportion
                elif str(share["userId"]) == friend_id and payer_id == user_id:
                    friend_owe += proportion

        if user_owe > 0 or friend_owe > 0:
            owe_amount = abs(friend_owe - user_owe)
            who_owes = "friend" if friend_owe > user_owe else "user"
            if who_owes == "friend":
                total_friend_owe += owe_amount
            else:
                total_user_owe += owe_amount

    total_owe_amount = abs(total_friend_owe - total_user_owe)
    overall_who_owes = "friend" if total_friend_owe > total_user_owe else "user"

    return {
        "name": friend.get("name", ""),
        "id": friend_id,
        "oweAmount": round(float(total_owe_amount), 2),
        "whoOwes": overall_who_owes,
        "currency": viewer_ccy,
    }


async def _event_net_owed(
    db: AsyncIOMotorDatabase, event: dict, payer_id: str, payee_id: str
) -> float:
    """How much ``payer_id`` owes ``payee_id`` inside one event, in its currency.

    The proportional-split arithmetic lives in one place — ``gross_debts`` —
    because it is the formula the balances screen also renders. Two copies of
    it would let the amount you are asked to settle disagree with the amount
    the app says you owe.
    """
    from app.services import event_service

    expense_ids = [
        e if isinstance(e, ObjectId) else ObjectId(e)
        for e in event.get("expenses", [])
    ]
    expenses = await db.expense.find({"_id": {"$in": expense_ids}}).to_list(
        length=None
    )

    debts = event_service.gross_debts(expenses)
    return debts.get(payer_id, {}).get(payee_id, 0.0) - debts.get(payee_id, {}).get(
        payer_id, 0.0
    )


async def _execute_settlement(
    db: AsyncIOMotorDatabase, actor_id: str, payer_id: str, payee_id: str
) -> list[str]:
    """Write the settle records that clear ``payer_id``'s debt to ``payee_id``.

    One record per shared group (each in that group's own currency, so the
    group's balances zero out exactly) plus one for everything outside any
    group. Returns the created expense ids so an undo can remove precisely
    these and nothing else.
    """
    payer = await db.users.find_one({"_id": ObjectId(payer_id)}, {"name": 1})
    payee = await db.users.find_one({"_id": ObjectId(payee_id)}, {"name": 1})
    payer_name = (payer or {}).get("name", "")
    payee_name = (payee or {}).get("name", "")
    now = datetime.now(timezone.utc)
    created: list[str] = []

    async def _write(
        amount: float, event_id: str | None, label: str, currency: str | None = None
    ) -> None:
        result = await expense_service.create_expense(
            db,
            actor_id,
            {
                "expenseName": label,
                "amount": amount,
                "payShares": [{"userId": payer_id, "amount": amount}],
                "shares": [{"userId": payee_id, "amount": amount}],
                "type": "settle",
                "category": "settle",
                "date": now,
                **({"eventId": event_id} if event_id else {}),
                # The amount is already in the currency it was computed in, so
                # state that currency explicitly and pin the rate. The actor
                # may not be the payer (a creditor can settle on their behalf),
                # and their default currency would otherwise be assumed.
                **({"currency": currency} if currency else {}),
                "fxRate": 1.0,
            },
        )
        if result.get("success") == "true" and result.get("data", {}).get("id"):
            created.append(result["data"]["id"])

    events = await db.event.find(
        {"users": {"$all": [ObjectId(payer_id), ObjectId(payee_id)]}}
    ).to_list(length=None)
    for event in events:
        net_amount = await _event_net_owed(db, event, payer_id, payee_id)
        if net_amount > 0.01:
            await _write(
                round(net_amount, 2),
                str(event["_id"]),
                f"{payer_name} settled up with {payee_name} ({event.get('eventName', '')})",
            )

    # Whatever is left is one-to-one, outside any group.
    remaining = await get_friend_dues(db, payer_id, payee_id)
    if remaining["whoOwes"] == "user" and remaining["oweAmount"] > 0.01:
        await _write(
            remaining["oweAmount"],
            None,
            f"{payer_name} settled up with {payee_name}",
            currency=remaining.get("currency"),
        )

    return created


async def _notify_settled(
    db: AsyncIOMotorDatabase, payer_id: str, payee_id: str, amount: float, currency: str
) -> None:
    """Tell both sides it is done — only ever called once the money is recorded."""
    from app.services import email_service

    people = await db.users.find(
        {"_id": {"$in": [ObjectId(payer_id), ObjectId(payee_id)]}},
        {"email": 1, "name": 1},
    ).to_list(length=None)
    by_id = {str(p["_id"]): p for p in people}
    payer = by_id.get(str(payer_id), {})
    payee = by_id.get(str(payee_id), {})

    for person, is_payer in ((payer, True), (payee, False)):
        if not person.get("email"):
            continue
        email_service.send_in_background(
            email_service.send_settleup_email(
                to_email=person["email"],
                to_name=person.get("name", ""),
                payer_name=payer.get("name", ""),
                payee_name=payee.get("name", ""),
                amount=amount or None,
                currency=currency,
                is_payer=is_payer,
            )
        )


async def settle_up_friend_dues(
    db: AsyncIOMotorDatabase, user_id: str, friend_id: str
) -> dict:
    """Start a settle-up, which needs confirmation only when the payer asks.

    Whoever *owes* the money can only claim to have paid it; the person who is
    owed has to say they received it, so their click confirms rather than
    initiates. When the creditor is the one settling there is nobody left to
    confirm — they are the only person who could dispute it — so it completes
    immediately. Emails go out once the settlement is actually recorded, never
    when it is merely requested-and-pending.
    """
    friend = await db.users.find_one({"_id": ObjectId(friend_id)})
    user = await db.users.find_one({"_id": ObjectId(user_id)})

    if not friend or not user:
        return {"success": "false", "message": "Invalid request"}

    # Existence is not a relationship. get_friend_details already gates its read
    # on this; without the same check here a caller could write settle records
    # against — and email — someone they have no connection to.
    if ObjectId(friend_id) not in user.get("friends", []):
        return {"success": "false", "message": "Invalid request"}

    dues = await get_friend_dues(db, user_id, friend_id)
    amount = float(dues.get("oweAmount") or 0)
    currency = dues.get("currency", "")
    if amount <= 0.01:
        return {"success": "false", "message": "Nothing to settle up."}

    caller_owes = dues.get("whoOwes") == "user"
    payer_id, payee_id = (
        (user_id, friend_id) if caller_owes else (friend_id, user_id)
    )

    existing = await db.settlement_request.find_one(
        {
            "payerId": ObjectId(payer_id),
            "payeeId": ObjectId(payee_id),
            "status": "pending",
        }
    )
    if existing:
        return {
            "success": "false",
            "status": "pending",
            "settlementId": str(existing["_id"]),
            "message": "A settle-up is already waiting to be confirmed.",
        }

    now = datetime.now(timezone.utc)
    request_doc = {
        "payerId": ObjectId(payer_id),
        "payeeId": ObjectId(payee_id),
        "requestedBy": ObjectId(user_id),
        "amount": round(amount, 2),
        "currency": currency,
        "status": "pending",
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.settlement_request.insert_one(request_doc)
    settlement_id = result.inserted_id

    if caller_owes:
        from app.services import email_service

        if friend.get("email"):
            email_service.send_in_background(
                email_service.send_settle_confirm_request_email(
                    to_email=friend["email"],
                    to_name=friend.get("name", ""),
                    payer_name=user.get("name", ""),
                    amount=amount,
                    currency=currency,
                )
            )
        return {
            "success": "true",
            "status": "pending",
            "settlementId": str(settlement_id),
            "message": f"Waiting for {friend.get('name', 'your friend')} to confirm they received it.",
        }

    return await _finalize_settlement(db, user_id, settlement_id)


async def _finalize_settlement(
    db: AsyncIOMotorDatabase, actor_id: str, settlement_id: ObjectId
) -> dict:
    """Record the settle expenses, log it, and mail both sides."""
    from app.services import audit_service

    # Claim it atomically so a double-tap cannot settle the same debt twice.
    settlement = await db.settlement_request.find_one_and_update(
        {"_id": settlement_id, "status": "pending"},
        {
            "$set": {
                "status": "confirmed",
                "confirmedBy": ObjectId(actor_id),
                "confirmedAt": datetime.now(timezone.utc),
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    if settlement is None:
        return {"success": "false", "message": "This settle-up is no longer pending."}

    payer_id = str(settlement["payerId"])
    payee_id = str(settlement["payeeId"])
    expense_ids = await _execute_settlement(db, actor_id, payer_id, payee_id)

    await db.settlement_request.update_one(
        {"_id": settlement_id}, {"$set": {"expenseIds": expense_ids}}
    )

    await audit_service.record(
        db,
        entity="settlement",
        entity_id=settlement_id,
        action="settle",
        actor_id=actor_id,
        audience=[payer_id, payee_id],
        summary=f"a settle-up of {settlement.get('currency', '')} {settlement.get('amount', 0):,.2f}",
        after={"expenseIds": expense_ids},
    )

    await _notify_settled(
        db,
        payer_id,
        payee_id,
        float(settlement.get("amount") or 0),
        settlement.get("currency", ""),
    )

    return {
        "success": "true",
        "status": "settled",
        "settlementId": str(settlement_id),
        "message": "Settled successfully!",
    }


async def get_pending_settlements(
    db: AsyncIOMotorDatabase, user_id: str
) -> list[dict]:
    """Open settle-ups involving the caller, flagged with who has to act."""
    oid = ObjectId(user_id)
    rows = (
        await db.settlement_request.find(
            {"status": "pending", "$or": [{"payerId": oid}, {"payeeId": oid}]}
        )
        .sort("createdAt", -1)
        .to_list(length=100)
    )

    people_ids = {r["payerId"] for r in rows} | {r["payeeId"] for r in rows}
    people = await db.users.find(
        {"_id": {"$in": list(people_ids)}}, {"name": 1}
    ).to_list(length=None)
    names = {str(p["_id"]): p.get("name", "Someone") for p in people}

    return [
        {
            "id": str(r["_id"]),
            "payerId": str(r["payerId"]),
            "payerName": names.get(str(r["payerId"]), "Someone"),
            "payeeId": str(r["payeeId"]),
            "payeeName": names.get(str(r["payeeId"]), "Someone"),
            "amount": float(r.get("amount") or 0),
            "currency": r.get("currency", ""),
            # Settlement rail: "paypal" or "upi" when the request was
            # created through the settlement flow; absent for old manual
            # claims. upiLink is the UPI intent link the payer scans/pays
            # with — the creditor sees it here so they can remind the payer.
            "rail": r.get("rail"),
            "upiLink": r.get("upiLink"),
            # Only the person owed the money can confirm they received it.
            "awaitingYou": str(r["payeeId"]) == str(user_id),
            "createdAt": r["createdAt"].isoformat()
            if isinstance(r.get("createdAt"), datetime)
            else str(r.get("createdAt", "")),
        }
        for r in rows
    ]


async def confirm_settlement(
    db: AsyncIOMotorDatabase, user_id: str, settlement_id: str
) -> dict:
    """Confirm receipt of a payment a friend said they made."""
    try:
        oid = ObjectId(settlement_id)
    except Exception:
        return {"success": "false", "message": "Settle-up not found."}

    settlement = await db.settlement_request.find_one({"_id": oid})
    if not settlement or str(settlement["payeeId"]) != str(user_id):
        # Only the payee confirms; anyone else gets the same answer as a
        # nonexistent id so this cannot be used to probe others' settle-ups.
        return {"success": "false", "message": "Settle-up not found."}
    if settlement.get("status") != "pending":
        return {"success": "false", "message": "This settle-up is no longer pending."}

    return await _finalize_settlement(db, user_id, oid)


async def reject_settlement(
    db: AsyncIOMotorDatabase, user_id: str, settlement_id: str
) -> dict:
    """Decline a claimed payment, or withdraw one you claimed yourself."""
    try:
        oid = ObjectId(settlement_id)
    except Exception:
        return {"success": "false", "message": "Settle-up not found."}

    settlement = await db.settlement_request.find_one({"_id": oid})
    if not settlement or str(user_id) not in (
        str(settlement["payeeId"]),
        str(settlement["payerId"]),
    ):
        return {"success": "false", "message": "Settle-up not found."}

    updated = await db.settlement_request.find_one_and_update(
        {"_id": oid, "status": "pending"},
        {
            "$set": {
                "status": "rejected",
                "rejectedBy": ObjectId(user_id),
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    if updated is None:
        return {"success": "false", "message": "This settle-up is no longer pending."}
    return {"success": "true", "message": "Settle-up declined."}


async def add_friend(
    db: AsyncIOMotorDatabase, user_id: str, request_data: dict
) -> dict:
    """Add a friend by friend code."""
    friend_code = request_data.get("friendCode", "")
    friend_user = await db.users.find_one({"friendCode": friend_code})

    if not friend_user:
        return {"message": "Friend not found!"}

    if user_id == str(friend_user["_id"]):
        return {"message": "Cannot add yourself as a friend!"}

    user_oid = ObjectId(user_id)
    friend_oid = friend_user["_id"]

    # Check if already friends
    user = await db.users.find_one({"_id": user_oid})
    if user and friend_oid in user.get("friends", []):
        return {"message": "Existing friend!"}

    # Add to both friend lists
    await db.users.update_one(
        {"_id": user_oid}, {"$addToSet": {"friends": friend_oid}}
    )
    await db.users.update_one(
        {"_id": friend_oid}, {"$addToSet": {"friends": user_oid}}
    )

    return {"message": "Friend Added Successfully!"}


async def delete_friend(
    db: AsyncIOMotorDatabase, user_id: str, request_data: dict
) -> dict:
    """Delete a friend by friend code."""
    friend_code = request_data.get("friendCode", "")
    friend_user = await db.users.find_one({"friendCode": friend_code})

    if not friend_user or user_id == str(friend_user["_id"]):
        return {"success": "false", "message": "Invalid request"}

    friend_oid = friend_user["_id"]
    user_oid = ObjectId(user_id)

    # Check for shared groups
    shared_groups = await db.event.find(
        {"users": {"$all": [user_oid, friend_oid]}}
    ).to_list(length=1)
    if shared_groups:
        return {
            "success": "false",
            "message": "Cannot delete friend. You have shared groups.",
        }

    # Check for unsettled expenses
    unsettled = await get_friend_dues(db, user_id, str(friend_oid))
    if unsettled["oweAmount"] > 0:
        return {
            "success": "false",
            "message": "Cannot delete friend. There are unsettled expenses.",
        }

    # Remove from both friend lists
    await db.users.update_one({"_id": user_oid}, {"$pull": {"friends": friend_oid}})
    await db.users.update_one({"_id": friend_oid}, {"$pull": {"friends": user_oid}})

    return {"success": "true", "message": "Unfriended successfully!"}
