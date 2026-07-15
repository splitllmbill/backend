from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from collections import defaultdict
from datetime import datetime, timezone
from app.services import expense_service, fx_service


async def _viewer_currency(db: AsyncIOMotorDatabase, user_id: str) -> str:
    user = await db.users.find_one({"_id": ObjectId(user_id)}, {"defaultCurrency": 1})
    return fx_service.normalize_currency((user or {}).get("defaultCurrency"))


async def _base_to_viewer_rates(
    db: AsyncIOMotorDatabase, expenses: list, viewer_currency: str
) -> dict[str, float]:
    """Current rates from each expense's base currency to the viewer currency."""
    bases = {
        fx_service.normalize_currency(e.get("baseCurrency")) for e in expenses
    }
    return await fx_service.get_rates_to(viewer_currency, bases, db=db)


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
    rates = await _base_to_viewer_rates(db, expenses, viewer_ccy)

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
    rates = await _base_to_viewer_rates(db, expenses, viewer_ccy)

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

    expenses = await db.expense.find(
        {"type": {"$in": ["group", "friend", "settle"]}}
    ).to_list(length=None)

    total_friend_owe = 0
    total_user_owe = 0

    viewer_ccy = await _viewer_currency(db, user_id)
    rates = await _base_to_viewer_rates(db, expenses, viewer_ccy)

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


async def settle_up_friend_dues(
    db: AsyncIOMotorDatabase, user_id: str, friend_id: str
) -> dict:
    """Settle all outstanding dues with a friend."""
    friend = await db.users.find_one({"_id": ObjectId(friend_id)})
    user = await db.users.find_one({"_id": ObjectId(user_id)})

    if not friend or not user:
        return {"success": "false", "message": "Invalid request"}

    now = datetime.now(timezone.utc)

    # 1. Settle for group events
    events = await db.event.find(
        {"users": {"$all": [ObjectId(user_id), ObjectId(friend_id)]}}
    ).to_list(length=None)

    for event in events:
        net_amount = 0
        expense_ids = [
            e if isinstance(e, ObjectId) else ObjectId(e)
            for e in event.get("expenses", [])
        ]
        expenses = await db.expense.find({"_id": {"$in": expense_ids}}).to_list(
            length=None
        )

        for expense in expenses:
            payers = [str(ps["userId"]) for ps in expense.get("payShares", [])]
            if user_id in payers or friend_id in payers:
                total_paid = sum(
                    float(ps["amount"]) for ps in expense.get("payShares", [])
                )
                if total_paid == 0:
                    continue

                for share in expense.get("shares", []):
                    proportion = 0
                    for ps in expense.get("payShares", []):
                        ps_user = str(ps["userId"])
                        share_user = str(share["userId"])
                        ps_amount = float(ps["amount"])
                        share_amount = float(share["amount"])

                        if ps_user == user_id and share_user == friend_id:
                            proportion -= share_amount * (ps_amount / total_paid)
                        elif ps_user == friend_id and share_user == user_id:
                            proportion += share_amount * (ps_amount / total_paid)

                    net_amount += proportion

        if net_amount > 0:
            request_data = {
                "expenseName": f"{user.get('name', '')} settled up with {friend.get('name', '')} ({event.get('eventName', '')})",
                "amount": net_amount,
                "payShares": [{"userId": user_id, "amount": net_amount}],
                "type": "settle",
                "eventId": str(event["_id"]),
                "category": "settle",
                "shares": [{"userId": friend_id, "amount": net_amount}],
                "date": now,
            }
            await expense_service.create_expense(db, user_id, request_data)

    # 2. Settle remaining dues (non-group)
    friend_dues_result = await get_friend_dues(db, user_id, friend_id)
    if (
        friend_dues_result["whoOwes"] == "user"
        and friend_dues_result["oweAmount"] != 0
    ):
        net_amount = friend_dues_result["oweAmount"]
        request_data = {
            "expenseName": f"{user.get('name', '')} settled up with {friend.get('name', '')}",
            "amount": net_amount,
            "payShares": [{"userId": user_id, "amount": net_amount}],
            "type": "settle",
            "category": "settle",
            "shares": [{"userId": friend_id, "amount": net_amount}],
            "date": now,
        }
        await expense_service.create_expense(db, user_id, request_data)

    return {"success": "true", "message": "Settled successfully!"}


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
