from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
import datetime

from app.services import fx_service


def get_datetime_range(request_data: dict) -> list:
    """Parse date range from request data."""
    start = request_data.get("startDate")
    end = request_data.get("endDate")

    if start:
        try:
            start_dt = datetime.datetime.strptime(start, "%a %b %d %Y")
        except ValueError:
            try:
                start_dt = datetime.datetime.fromisoformat(
                    start.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                start_dt = datetime.datetime.min
    else:
        start_dt = datetime.datetime.min

    if end:
        try:
            end_dt = datetime.datetime.strptime(end, "%a %b %d %Y")
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
        except ValueError:
            try:
                end_dt = datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                end_dt = datetime.datetime.max
    else:
        end_dt = datetime.datetime.max

    return [start_dt, end_dt]


async def group_owe_details(
    db: AsyncIOMotorDatabase, user_id: str, viewer_currency: str
) -> tuple:
    """Calculate total owed and owing amounts for group/friend/settle expenses."""
    pipeline = [
        {
            "$match": {
                "type": {"$in": ["group", "friend", "settle"]},
                "shares.userId": ObjectId(user_id),
            }
        },
        {"$project": {"payShares": 1, "shares": 1, "type": 1, "baseCurrency": 1}},
    ]

    result = await db.expense.aggregate(pipeline).to_list(length=None)

    bases = {fx_service.normalize_currency(e.get("baseCurrency")) for e in result}
    rates = await fx_service.get_rates_to(viewer_currency, bases, db=db)

    total_owed_amount = 0  # others owe me
    total_owe_amount = 0  # I owe others

    for expense in result:
        shares = expense.get("shares", [])
        pay_shares = expense.get("payShares", [])
        total_paid = (
            sum(float(p["amount"]) for p in pay_shares) if pay_shares else 0
        )
        rate = rates.get(
            fx_service.normalize_currency(expense.get("baseCurrency")), 1.0
        )

        for share in shares:
            participant_id = str(share["userId"])
            share_amount = float(share["amount"]) * rate

            for ps in pay_shares:
                payer_id = str(ps["userId"])
                pay_amount = float(ps["amount"])

                if total_paid == 0:
                    continue

                proportional_amount = share_amount * (pay_amount / total_paid)

                if payer_id == participant_id:
                    continue

                if participant_id == user_id:
                    total_owe_amount += proportional_amount
                elif payer_id == user_id:
                    total_owed_amount += proportional_amount

    return (round(total_owe_amount, 2), round(total_owed_amount, 2))


async def get_summary_for_homepage(
    db: AsyncIOMotorDatabase, user_id: str, request_data: dict
) -> dict:
    """Get dashboard summary with expense totals and owe amounts."""
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        return None

    date_range = get_datetime_range(request_data)
    total_share = 0

    viewer_currency = fx_service.normalize_currency(user.get("defaultCurrency"))

    # Aggregate personal expenses where user paid, per base currency, then
    # convert each bucket into the viewer's currency.
    personal_pipeline = [
        {
            "$match": {
                "type": "normal",
                "payShares.userId": ObjectId(user_id),
                "date": {"$gte": date_range[0], "$lte": date_range[1]},
            }
        },
        {"$unwind": "$payShares"},
        {"$match": {"payShares.userId": ObjectId(user_id)}},
        {
            "$group": {
                "_id": {"$ifNull": ["$baseCurrency", viewer_currency]},
                "totalAmount": {"$sum": "$payShares.amount"},
            }
        },
    ]

    result = await db.expense.aggregate(personal_pipeline).to_list(length=None)
    personal_bases = {fx_service.normalize_currency(r["_id"]) for r in result}
    personal_rates = await fx_service.get_rates_to(
        viewer_currency, personal_bases, db=db
    )
    personal_expenses = sum(
        float(r["totalAmount"])
        * personal_rates.get(fx_service.normalize_currency(r["_id"]), 1.0)
        for r in result
    )

    # Group/friend/settle expenses
    other_pipeline = [
        {
            "$match": {
                "type": {"$in": ["group", "friend", "settle"]},
                "date": {"$gte": date_range[0], "$lte": date_range[1]},
                "shares.userId": ObjectId(user_id),
            }
        },
        {"$project": {"payShares": 1, "type": 1, "shares": 1, "baseCurrency": 1}},
    ]

    result = await db.expense.aggregate(other_pipeline).to_list(length=None)
    other_bases = {fx_service.normalize_currency(e.get("baseCurrency")) for e in result}
    other_rates = await fx_service.get_rates_to(viewer_currency, other_bases, db=db)
    for expense in result:
        rate = other_rates.get(
            fx_service.normalize_currency(expense.get("baseCurrency")), 1.0
        )
        for share in expense.get("shares", []):
            if str(share["userId"]) == user_id and expense.get("type") != "settle":
                total_share += float(share["amount"]) * rate

    total_owe_amount, total_owed_amount = await group_owe_details(
        db, user_id, viewer_currency
    )

    return {
        "group_expenses": round(float(total_share), 2),
        "personal_expenses": round(float(personal_expenses), 2),
        "total_you_owe": float(total_owe_amount),
        "total_owed_to_you": float(total_owed_amount),
        "currency": viewer_currency,
        "username": user.get("name", ""),
    }


async def get_dashboard_chart(
    db: AsyncIOMotorDatabase, user_id: str, request_data: dict
) -> list:
    """Get category-wise expense breakdown for chart display."""
    date_range = get_datetime_range(request_data)

    user = await db.users.find_one(
        {"_id": ObjectId(user_id)}, {"defaultCurrency": 1}
    )
    viewer_currency = fx_service.normalize_currency(
        (user or {}).get("defaultCurrency")
    )

    # Group per (category, base currency); currency conversion and percentage
    # math happen in Python since rates live outside Mongo.
    pipeline = [
        {
            "$match": {
                "type": "normal",
                "date": {"$gte": date_range[0], "$lte": date_range[1]},
            }
        },
        {"$unwind": "$payShares"},
        {"$match": {"payShares.userId": ObjectId(user_id)}},
        {
            "$group": {
                "_id": {
                    "category": "$category",
                    "base": {"$ifNull": ["$baseCurrency", viewer_currency]},
                },
                "cost": {"$sum": "$payShares.amount"},
                "noOfTransactions": {"$sum": 1},
            }
        },
    ]

    rows = await db.expense.aggregate(pipeline).to_list(length=None)
    bases = {fx_service.normalize_currency(r["_id"]["base"]) for r in rows}
    rates = await fx_service.get_rates_to(viewer_currency, bases, db=db)

    by_category: dict[str, dict] = {}
    total_cost = 0.0
    for row in rows:
        category = row["_id"]["category"]
        rate = rates.get(fx_service.normalize_currency(row["_id"]["base"]), 1.0)
        cost = float(row["cost"]) * rate
        entry = by_category.setdefault(
            category, {"category": category, "cost": 0.0, "noOfTransactions": 0}
        )
        entry["cost"] += cost
        entry["noOfTransactions"] += int(row["noOfTransactions"])
        total_cost += cost

    result = []
    for entry in sorted(by_category.values(), key=lambda e: -e["cost"]):
        entry["cost"] = round(entry["cost"], 2)
        entry["percent"] = (
            round(entry["cost"] / total_cost * 100, 2) if total_cost else 0.0
        )
        entry["currency"] = viewer_currency
        result.append(entry)
    return result
