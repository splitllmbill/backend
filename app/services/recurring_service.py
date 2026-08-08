"""Expenses that repeat: subscriptions, rent, the shared streaming plan.

A recurring expense is a **template plus a schedule**, not an expense. Nothing
is owed until an occurrence actually falls due, at which point a perfectly
ordinary expense is created through :func:`expense_service.create_expense` —
same validation, same currency conversion, same audit trail. Everything
downstream (balances, dashboards, exports) therefore needs to know nothing
about recurrence, and deleting a schedule leaves the expenses it already made
alone, because they really did happen.

Posting is driven by :func:`post_due`, which the app's hourly loop calls
alongside the card reminders. Two properties make that safe to run repeatedly
and from more than one instance:

* **Idempotent.** Each (schedule, occurrence date) can be posted once, enforced
  by a unique index on ``recurring_run`` — the insert fails on a duplicate, so
  an instance racing another posts nothing rather than charging the user twice.
* **Catch-up, bounded.** A schedule that was missed for a month posts the
  occurrences it missed, oldest first, up to :data:`MAX_CATCHUP` in one pass.
  A container that was down for a year does not wake up and post 365 rows.

Group schedules can split either way. ``fixed`` replays the shares the user set
up; ``equal`` divides across the group's members *as they are on the day it
posts*, which is what people actually mean by "we split Netflix" when someone
joins in March.
"""

import calendar
import logging
from datetime import date, datetime, timedelta, timezone

from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError

from app.services import category_service

logger = logging.getLogger(__name__)

#: How the interval between occurrences is described.
FREQUENCIES = ("weekly", "fortnightly", "monthly", "quarterly", "yearly")

#: The most occurrences one schedule may post in a single pass. A schedule that
#: has been due since last year catches up over several passes instead of
#: dumping a year of history into the user's list at once.
MAX_CATCHUP = 12

#: How a group schedule divides. See the module docstring.
SPLIT_MODES = ("equal", "fixed")


# ─── Date arithmetic (pure) ─────────────────────────────────────────────


def _clamp_day(day: int, year: int, month: int) -> date:
    """``day`` in the given month, clamped to months that end sooner.

    Rent due on the 31st is due on the 28th in February — the alternative is
    skipping the month, which is worse than being three days early.
    """
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def _add_months(d: date, months: int, anchor_day: int) -> date:
    """``d`` plus ``months``, keeping the anchor day where the month allows.

    The anchor is carried separately from the date so a schedule that fell on
    the 28th of February climbs back to the 31st in March, rather than being
    permanently dragged down to the 28th by its own clamping.
    """
    total = (d.year * 12 + d.month - 1) + months
    return _clamp_day(anchor_day, total // 12, total % 12 + 1)


def advance(current: date, frequency: str, anchor_day: int) -> date:
    """The occurrence after ``current``."""
    if frequency == "weekly":
        return current + timedelta(days=7)
    if frequency == "fortnightly":
        return current + timedelta(days=14)
    if frequency == "monthly":
        return _add_months(current, 1, anchor_day)
    if frequency == "quarterly":
        return _add_months(current, 3, anchor_day)
    if frequency == "yearly":
        # Feb 29 in a non-leap year lands on the 28th, and climbs back in the
        # next leap year for the same reason monthly anchors do.
        return _clamp_day(anchor_day, current.year + 1, current.month)
    raise ValueError(f"Unknown frequency: {frequency}")


def due_dates(
    next_due: date,
    frequency: str,
    anchor_day: int,
    today: date,
    end_date: date | None = None,
    limit: int = MAX_CATCHUP,
) -> list[date]:
    """Every occurrence from ``next_due`` up to and including ``today``.

    Empty when the schedule is not due yet — the common case, and the reason
    this is cheap to call on every pass.
    """
    dates: list[date] = []
    cursor = next_due
    while cursor <= today and len(dates) < limit:
        if end_date and cursor > end_date:
            break
        dates.append(cursor)
        cursor = advance(cursor, frequency, anchor_day)
    return dates


def _parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


# ─── Validation & serialisation ─────────────────────────────────────────


def _validate(data: dict) -> dict:
    """Check a schedule the user is creating, and normalise it."""
    name = (data.get("expenseName") or data.get("title") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the subscription a name")

    try:
        amount = round(float(data.get("amount") or 0), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Amount must be a number")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be more than zero")

    frequency = (data.get("frequency") or "monthly").lower()
    if frequency not in FREQUENCIES:
        raise HTTPException(
            status_code=400,
            detail=f"Frequency must be one of: {', '.join(FREQUENCIES)}",
        )

    start = _parse_date(data.get("startDate")) or datetime.now(timezone.utc).date()
    end = _parse_date(data.get("endDate"))
    if end and end < start:
        raise HTTPException(status_code=400, detail="End date is before the start date")

    expense_type = (data.get("type") or "normal").lower()
    if expense_type not in ("normal", "group"):
        raise HTTPException(status_code=400, detail="Type must be normal or group")
    event_id = data.get("eventId") or None
    if expense_type == "group" and not event_id:
        raise HTTPException(status_code=400, detail="A group subscription needs a group")

    split_mode = (data.get("splitMode") or "equal").lower()
    if split_mode not in SPLIT_MODES:
        raise HTTPException(status_code=400, detail="Split must be equal or fixed")
    shares = data.get("shares") or []
    if split_mode == "fixed" and expense_type == "group" and not shares:
        raise HTTPException(status_code=400, detail="A fixed split needs shares")

    return {
        "expenseName": name,
        "amount": amount,
        "category": category_service.normalize(data.get("category") or "General"),
        "description": (data.get("description") or "").strip(),
        "currency": data.get("currency") or None,
        "frequency": frequency,
        # The day of the month the schedule *wants*, kept separate from the
        # dates it actually lands on. See _add_months.
        "anchorDay": start.day,
        "startDate": start.isoformat(),
        "nextDueDate": start.isoformat(),
        "endDate": end.isoformat() if end else None,
        "type": expense_type,
        "eventId": event_id,
        "splitMode": split_mode,
        "shares": shares,
        "payShares": data.get("payShares") or [],
        "paused": bool(data.get("paused", False)),
    }


def serialize(schedule: dict) -> dict:
    out = {k: v for k, v in schedule.items() if k not in ("_id", "userId")}
    out["id"] = str(schedule["_id"])
    out["userId"] = str(schedule["userId"])
    if out.get("eventId"):
        out["eventId"] = str(out["eventId"])
    for key in ("createdAt", "updatedAt", "lastPostedAt"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


# ─── CRUD ───────────────────────────────────────────────────────────────


async def list_schedules(db: AsyncIOMotorDatabase, user_id: str) -> list[dict]:
    schedules = await db.recurring_expense.find(
        {"userId": ObjectId(user_id)}
    ).sort("nextDueDate", 1).to_list(length=None)
    return [serialize(s) for s in schedules]


async def create_schedule(
    db: AsyncIOMotorDatabase, user_id: str, data: dict
) -> dict:
    schedule = _validate(data)
    if schedule["type"] == "group":
        await _assert_member(db, user_id, schedule["eventId"])
        schedule["eventId"] = ObjectId(schedule["eventId"])

    now = datetime.now(timezone.utc)
    schedule.update(
        {
            "userId": ObjectId(user_id),
            "createdAt": now,
            "updatedAt": now,
            "occurrencesPosted": 0,
        }
    )
    result = await db.recurring_expense.insert_one(schedule)
    schedule["_id"] = result.inserted_id
    return serialize(schedule)


async def update_schedule(
    db: AsyncIOMotorDatabase, user_id: str, schedule_id: str, data: dict
) -> dict:
    existing = await _owned(db, user_id, schedule_id)

    updates: dict = {}
    # Only the fields it makes sense to change on a live schedule. The start
    # date is not one of them — occurrences already posted came from it.
    if "paused" in data:
        updates["paused"] = bool(data["paused"])
    if data.get("amount") is not None:
        try:
            amount = round(float(data["amount"]), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Amount must be a number")
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be more than zero")
        updates["amount"] = amount
    if data.get("expenseName"):
        updates["expenseName"] = data["expenseName"].strip()
    if data.get("category"):
        updates["category"] = category_service.normalize(data["category"])
    if "endDate" in data:
        end = _parse_date(data.get("endDate"))
        start = _parse_date(existing.get("startDate"))
        if end and start and end < start:
            raise HTTPException(
                status_code=400, detail="End date is before the start date"
            )
        updates["endDate"] = end.isoformat() if end else None
    if data.get("frequency"):
        frequency = data["frequency"].lower()
        if frequency not in FREQUENCIES:
            raise HTTPException(status_code=400, detail="Unknown frequency")
        updates["frequency"] = frequency

    if not updates:
        return serialize(existing)

    updates["updatedAt"] = datetime.now(timezone.utc)
    await db.recurring_expense.update_one(
        {"_id": existing["_id"]}, {"$set": updates}
    )
    existing.update(updates)
    return serialize(existing)


async def delete_schedule(
    db: AsyncIOMotorDatabase, user_id: str, schedule_id: str
) -> dict:
    existing = await _owned(db, user_id, schedule_id)
    await db.recurring_expense.delete_one({"_id": existing["_id"]})
    # The expenses it already posted stay. They were real spending; the
    # schedule ending does not un-spend them.
    return {"message": "Subscription stopped", "success": "true"}


async def _owned(db: AsyncIOMotorDatabase, user_id: str, schedule_id: str) -> dict:
    try:
        oid = ObjectId(schedule_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Subscription not found")
    schedule = await db.recurring_expense.find_one({"_id": oid})
    if not schedule:
        raise HTTPException(status_code=404, detail="Subscription not found")
    if str(schedule.get("userId")) != str(user_id):
        # Not 403: whose subscription it is, is not this caller's business.
        raise HTTPException(status_code=404, detail="Subscription not found")
    return schedule


async def _assert_member(db: AsyncIOMotorDatabase, user_id: str, event_id: str) -> dict:
    try:
        event = await db.event.find_one({"_id": ObjectId(event_id)})
    except Exception:
        event = None
    if not event:
        raise HTTPException(status_code=404, detail="Group not found")
    if ObjectId(user_id) not in event.get("users", []):
        raise HTTPException(status_code=403, detail="You are not a member of this group")
    return event


# ─── Posting ────────────────────────────────────────────────────────────


async def _claim(
    db: AsyncIOMotorDatabase, schedule_id: ObjectId, occurrence: date
) -> bool:
    """Reserve one (schedule, occurrence). False if another pass has it.

    The unique index arbitrates, so this is safe across instances — the loser
    of a race gets DuplicateKeyError and posts nothing.
    """
    try:
        await db.recurring_run.insert_one(
            {
                "recurringId": schedule_id,
                "occurrenceDate": occurrence.isoformat(),
                "postedAt": datetime.now(timezone.utc),
            }
        )
        return True
    except DuplicateKeyError:
        return False


async def _shares_for(
    db: AsyncIOMotorDatabase, schedule: dict, amount: float
) -> tuple[list, list]:
    """The shares and payShares one occurrence should carry."""
    owner_id = str(schedule["userId"])

    if schedule.get("type") != "group":
        return (
            [{"userId": owner_id, "amount": amount}],
            [{"userId": owner_id, "amount": amount}],
        )

    if schedule.get("splitMode") == "fixed":
        return list(schedule.get("shares") or []), list(
            schedule.get("payShares") or [{"userId": owner_id, "amount": amount}]
        )

    event = await db.event.find_one({"_id": schedule["eventId"]})
    members = [str(u) for u in (event or {}).get("users", [])] or [owner_id]
    # Split equally, and give the remainder to the payer rather than losing it
    # to rounding — the shares must add up to the amount or create_expense
    # rejects the whole occurrence.
    each = round(amount / len(members), 2)
    shares = [{"userId": m, "amount": each} for m in members]
    shares[0]["amount"] = round(each + (amount - each * len(members)), 2)
    return shares, [{"userId": owner_id, "amount": amount}]


async def post_due(
    db: AsyncIOMotorDatabase, today: date | None = None, user_id: str | None = None
) -> int:
    """Post every occurrence that has come due. Returns how many were created.

    Pass ``user_id`` to run only one person's schedules — the app does this
    when they open their expense list, so a subscription that fell due an hour
    ago is there when they look rather than up to an hour later.
    """
    from app.services import expense_service

    today = today or datetime.now(timezone.utc).date()
    query: dict = {
        "paused": {"$ne": True},
        "nextDueDate": {"$lte": today.isoformat()},
    }
    if user_id:
        query["userId"] = ObjectId(user_id)

    schedules = await db.recurring_expense.find(query).to_list(length=None)
    posted = 0

    for schedule in schedules:
        try:
            posted += await _post_one(db, expense_service, schedule, today)
        except Exception as e:
            # One broken schedule must not stop the others — a group that was
            # deleted, say. The next pass retries it.
            logger.error(
                "Recurring schedule %s failed to post: %s", schedule.get("_id"), e
            )

    return posted


async def _post_one(
    db: AsyncIOMotorDatabase, expense_service, schedule: dict, today: date
) -> int:
    next_due = _parse_date(schedule.get("nextDueDate"))
    if not next_due:
        return 0

    end = _parse_date(schedule.get("endDate"))
    occurrences = due_dates(
        next_due,
        schedule.get("frequency", "monthly"),
        int(schedule.get("anchorDay") or next_due.day),
        today,
        end_date=end,
    )
    if not occurrences:
        return 0

    owner_id = str(schedule["userId"])
    amount = float(schedule.get("amount") or 0)
    posted = 0
    cursor = next_due

    for occurrence in occurrences:
        cursor = advance(
            occurrence,
            schedule.get("frequency", "monthly"),
            int(schedule.get("anchorDay") or occurrence.day),
        )
        if not await _claim(db, schedule["_id"], occurrence):
            continue

        shares, pay_shares = await _shares_for(db, schedule, amount)
        payload = {
            "expenseName": schedule.get("expenseName"),
            "amount": amount,
            "category": schedule.get("category", "General"),
            "description": schedule.get("description", ""),
            "type": schedule.get("type", "normal"),
            "date": datetime.combine(
                occurrence, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat(),
            "shares": shares,
            "payShares": pay_shares,
        }
        if schedule.get("currency"):
            payload["currency"] = schedule["currency"]
        if schedule.get("eventId"):
            payload["eventId"] = str(schedule["eventId"])

        result = await expense_service.create_expense(db, owner_id, payload)
        created = (result or {}).get("data") or {}
        if not created.get("_id") and not created.get("id"):
            # create_expense refused it. Release the claim so a fixed schedule
            # can post this occurrence on a later pass instead of it being
            # silently lost forever.
            await db.recurring_run.delete_one(
                {
                    "recurringId": schedule["_id"],
                    "occurrenceDate": occurrence.isoformat(),
                }
            )
            logger.error(
                "Recurring schedule %s: occurrence %s rejected: %s",
                schedule["_id"],
                occurrence,
                (result or {}).get("message"),
            )
            break

        expense_id = created.get("_id") or created.get("id")
        await db.expense.update_one(
            {"_id": ObjectId(expense_id)},
            {
                "$set": {
                    "recurringId": schedule["_id"],
                    "occurrenceDate": occurrence.isoformat(),
                }
            },
        )
        await db.recurring_run.update_one(
            {
                "recurringId": schedule["_id"],
                "occurrenceDate": occurrence.isoformat(),
            },
            {"$set": {"expenseId": ObjectId(expense_id)}},
        )
        posted += 1

    finished = bool(end and cursor > end)
    await db.recurring_expense.update_one(
        {"_id": schedule["_id"]},
        {
            "$set": {
                "nextDueDate": cursor.isoformat(),
                "lastPostedAt": datetime.now(timezone.utc),
                **({"paused": True} if finished else {}),
            },
            "$inc": {"occurrencesPosted": posted},
        },
    )
    return posted
