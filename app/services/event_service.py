import random
import string
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import settings
from app.services import fx_service
from app.services.user_service import serialize_doc, get_user_name_by_id


class CurrencyChangeRequiresChoice(Exception):
    """Raised when an event's currency changes while it still has expenses.

    The caller has to say whether the existing transactions should be
    re-converted into the new currency or left on their original one, so the
    router turns this into a 409 the app answers with ``applyToExpenses``.
    """

    def __init__(self, expense_count: int, from_currency: str, to_currency: str):
        self.expense_count = expense_count
        self.from_currency = from_currency
        self.to_currency = to_currency
        super().__init__("Changing the group currency affects existing expenses")


# ─── Multi-currency ledger ──────────────────────────────────────────────
#
# Every expense is recorded in the currency it was actually paid in. Debts are
# therefore accumulated per currency and only collapsed into a single number at
# display time, using the *viewer's* conversion rates. That keeps the raw
# "you owe me $40 and €15" truth exact, and lets two members who disagree about
# what a dollar cost them still see a consistent split.


def _entry_amount(entry: dict) -> float:
    """A share/payShare amount in the currency the expense was entered in."""
    value = entry.get("originalAmount")
    if value is None:
        value = entry.get("amount", 0)
    return float(value or 0)


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _signed_ledger(expenses: list) -> dict[tuple[str, str], dict[str, float]]:
    """Net debts between every pair of members, per currency.

    Result maps an ordered ``(first, second)`` pair to ``{currency: amount}``
    where a positive amount means *first owes second*.
    """
    ledger: dict[tuple[str, str], dict[str, float]] = {}

    for expense in expenses:
        currency = fx_service.normalize_currency(expense.get("currency"))
        shares = expense.get("shares", [])
        pay_shares = expense.get("payShares", [])
        total_paid = sum(_entry_amount(ps) for ps in pay_shares)
        if total_paid == 0:
            continue

        for pay_share in pay_shares:
            payer_id = str(pay_share["userId"])
            payer_ratio = _entry_amount(pay_share) / total_paid
            if payer_ratio == 0:
                continue
            for share in shares:
                participant_id = str(share["userId"])
                if participant_id == payer_id:
                    continue
                amount = _entry_amount(share) * payer_ratio
                if amount == 0:
                    continue
                key = _pair_key(participant_id, payer_id)
                # Positive means key[0] owes key[1].
                signed = amount if key[0] == participant_id else -amount
                bucket = ledger.setdefault(key, {})
                bucket[currency] = bucket.get(currency, 0.0) + signed

    # Drop currencies that netted out to nothing.
    cleaned: dict[tuple[str, str], dict[str, float]] = {}
    for key, by_currency in ledger.items():
        kept = {
            currency: round(amount, 2)
            for currency, amount in by_currency.items()
            if abs(round(amount, 2)) >= 0.01
        }
        if kept:
            cleaned[key] = kept
    return cleaned


def _ledger_currencies(ledger: dict) -> set[str]:
    return {currency for by_currency in ledger.values() for currency in by_currency}


def _expense_currencies(expenses: list) -> set[str]:
    return {
        fx_service.normalize_currency(expense.get("currency")) for expense in expenses
    }


def _convert(by_currency: dict[str, float], rates: dict[str, float]) -> float:
    """Collapse a per-currency map into one number using ``rates``."""
    return round(
        sum(amount * rates.get(currency, 1.0) for currency, amount in by_currency.items()),
        2,
    )


def _split_by_sign(by_currency: dict[str, float], flip: bool) -> dict[str, float]:
    """Per-currency amounts from one member's point of view.

    Positive entries are what they owe, negative what they are owed; ``flip``
    reverses the pair's stored direction.
    """
    sign = -1 if flip else 1
    return {
        currency: round(amount * sign, 2)
        for currency, amount in by_currency.items()
        if abs(amount) >= 0.01
    }


def _viewer_positions(
    ledger: dict[tuple[str, str], dict[str, float]], viewer_id: str
) -> dict[str, dict[str, float]]:
    """Counterpart id -> per-currency amounts, positive = the viewer owes."""
    positions: dict[str, dict[str, float]] = {}
    for (first, second), by_currency in ledger.items():
        if viewer_id == first:
            positions[second] = _split_by_sign(by_currency, flip=False)
        elif viewer_id == second:
            positions[first] = _split_by_sign(by_currency, flip=True)
    return positions


async def get_member_rates(
    db: AsyncIOMotorDatabase,
    event: dict,
    user_id: str,
    currencies: set[str] | None = None,
) -> dict[str, float]:
    """Conversion rates into the event currency for one member.

    A member may override any rate (``event.memberRates[userId]``) — useful when
    the rate their bank actually charged differs from the market one. Anything
    they have not overridden falls back to the market rate.
    """
    base = fx_service.normalize_currency(event.get("currency"))
    overrides = ((event.get("memberRates") or {}).get(str(user_id)) or {})
    rates: dict[str, float] = {base: 1.0}

    for currency in currencies or set():
        currency = fx_service.normalize_currency(currency)
        if currency in rates:
            continue
        override = overrides.get(currency)
        if override:
            try:
                rates[currency] = float(override)
                continue
            except (TypeError, ValueError):
                pass
        rates[currency] = await fx_service.get_rate(currency, base, db=db)
    return rates


async def get_event_rate_settings(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> dict:
    """The rate editor's payload: every currency in play, market vs. custom."""
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    base = fx_service.normalize_currency(event.get("currency"))
    expenses = await _load_event_expenses(db, event)
    currencies = _expense_currencies(expenses) | {base}
    overrides = ((event.get("memberRates") or {}).get(str(user_id)) or {})

    rates = []
    for currency in sorted(currencies):
        if currency == base:
            continue
        market = await fx_service.get_rate(currency, base, db=db)
        override = overrides.get(currency)
        rates.append(
            {
                "currency": currency,
                "marketRate": market,
                "rate": float(override) if override else market,
                "isCustom": bool(override),
            }
        )

    return {"eventId": str(event["_id"]), "baseCurrency": base, "rates": rates}


async def set_event_member_rates(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str, rates: dict
) -> dict:
    """Store (or clear) one member's own conversion rates for an event."""
    event = await db.event.find_one({"_id": ObjectId(event_id)}, {"currency": 1})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    base = fx_service.normalize_currency(event.get("currency"))

    cleaned: dict[str, float] = {}
    for currency, value in (rates or {}).items():
        code = fx_service.normalize_currency(currency)
        if code == base:
            continue  # the event currency is always 1:1 with itself
        if value in (None, ""):
            continue  # omitted -> fall back to the market rate
        try:
            rate = float(value)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail=f"Invalid rate for {code}"
            )
        if rate <= 0:
            raise HTTPException(
                status_code=400, detail=f"Rate for {code} must be greater than zero"
            )
        cleaned[code] = rate

    field = f"memberRates.{user_id}"
    if cleaned:
        await db.event.update_one(
            {"_id": ObjectId(event_id)}, {"$set": {field: cleaned}}
        )
    else:
        await db.event.update_one(
            {"_id": ObjectId(event_id)}, {"$unset": {field: ""}}
        )
    return await get_event_rate_settings(db, event_id, user_id)


async def _load_event_expenses(db: AsyncIOMotorDatabase, event: dict) -> list:
    expense_ids = [
        ref if isinstance(ref, ObjectId) else ObjectId(ref)
        for ref in event.get("expenses", [])
    ]
    if not expense_ids:
        return []
    return await db.expense.find({"_id": {"$in": expense_ids}}).to_list(length=None)


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
                    {
                        "$project": {
                            "_id": 1,
                            "payShares": 1,
                            "shares": 1,
                            "currency": 1,
                        }
                    },
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
                "memberRates": {"$first": "$memberRates"},
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

    # Names shown with an email suffix when two members share a display name.
    for event in events:
        for user_list in event.get("eventUsers", []):
            for user in user_list:
                name = user.get("name", "")
                email = user.get("email", "")
                if not name:
                    continue
                if name not in same_name_dict:
                    same_name_dict[name] = email
                elif email != same_name_dict[name]:
                    same_name_list.add(name)

    for event in events:
        event_currency = fx_service.normalize_currency(event.get("currency"))
        event_rate = to_viewer_rates.get(event_currency, 1.0)
        expenses = event.get("expenses", [])
        ledger = _signed_ledger(expenses)
        rates = await get_member_rates(
            db, event, user_id, _ledger_currencies(ledger)
        )
        positions = _viewer_positions(ledger, str(user_id))

        event_dict = {
            "id": str(event["_id"]),
            "users": [str(u) for u in event.get("users", [])],
            "eventName": event.get("eventName", ""),
            "description": event.get("description", ""),
            "currency": event_currency,
            "createdAt": str(event.get("createdAt", "")),
            "createdBy": str(event.get("createdBy", "")),
            "expenses": [str(exp["_id"]) for exp in expenses],
            "dues": {
                "userName": user_name,
                "currency": event_currency,
                "inDebtTo": [],
                "isOwed": [],
                "totalDebt": 0,
                "totalOwed": 0,
            },
        }

        for counterpart_id, by_currency in positions.items():
            user_data = user_map.get(counterpart_id, {})
            name = user_data.get("name", "Unknown")
            if name in same_name_list:
                name += f" ( {user_data.get('email', '')} )"
            amount = _convert(by_currency, rates)
            if abs(amount) < 0.01:
                continue
            entry = {
                "id": counterpart_id,
                "name": name,
                "amount": abs(amount),
                "byCurrency": by_currency,
            }
            if amount < 0:
                event_dict["dues"]["isOwed"].append(entry)
                event_dict["dues"]["totalOwed"] += abs(amount)
            else:
                event_dict["dues"]["inDebtTo"].append(entry)
                event_dict["dues"]["totalDebt"] += abs(amount)
            overall_owe_amount += amount * event_rate

        event_dict["dues"]["totalDebt"] = round(
            float(event_dict["dues"]["totalDebt"]), 2
        )
        event_dict["dues"]["totalOwed"] = round(
            float(event_dict["dues"]["totalOwed"]), 2
        )
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


async def is_event_member(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> bool:
    """True if the user belongs to the event. Guards event reads/writes."""
    try:
        event = await db.event.find_one({"_id": ObjectId(event_id)}, {"users": 1})
        return bool(event) and ObjectId(user_id) in event.get("users", [])
    except Exception:
        return False


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
                    # Phantom members are the "unclaimed spots" a join link
                    # lets a real person take over.
                    "isPhantom": bool(user.get("isPhantom")),
                }
            )

    result = serialize_doc(event)
    result["users"] = user_data
    return result


# ─── Join links ─────────────────────────────────────────────────────────


def _generate_join_code(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


async def ensure_join_code(db: AsyncIOMotorDatabase, event_id: str) -> str:
    """Return the event's join code, creating one on first use."""
    event = await db.event.find_one({"_id": ObjectId(event_id)}, {"joinCode": 1})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event.get("joinCode"):
        return event["joinCode"]

    code = _generate_join_code()
    while await db.event.find_one({"joinCode": code}):
        code = _generate_join_code()
    await db.event.update_one({"_id": event["_id"]}, {"$set": {"joinCode": code}})
    return code


async def rotate_join_code(db: AsyncIOMotorDatabase, event_id: str) -> str:
    """Invalidate the old link and issue a new one."""
    code = _generate_join_code()
    while await db.event.find_one({"joinCode": code}):
        code = _generate_join_code()
    result = await db.event.update_one(
        {"_id": ObjectId(event_id)}, {"$set": {"joinCode": code}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Event not found")
    return code


async def get_join_preview(
    db: AsyncIOMotorDatabase, code: str, user_id: str | None
) -> dict:
    """What someone opening a group link sees before they commit to joining."""
    event = await db.event.find_one({"joinCode": (code or "").strip().upper()})
    if not event:
        raise HTTPException(status_code=404, detail="This group link is not valid")

    members = []
    for user_ref in event.get("users", []):
        member = await db.users.find_one(
            {"_id": user_ref if isinstance(user_ref, ObjectId) else ObjectId(user_ref)}
        )
        if not member:
            continue
        members.append(
            {
                "id": str(member["_id"]),
                "name": member.get("name", ""),
                # A phantom's email is a placeholder the importer typed, so it
                # is only a hint about who the spot belongs to.
                "email": member.get("email", "") if member.get("isPhantom") else "",
                "isPhantom": bool(member.get("isPhantom")),
            }
        )

    return {
        "eventId": str(event["_id"]),
        "eventName": event.get("eventName", ""),
        "description": event.get("description", ""),
        "currency": fx_service.normalize_currency(event.get("currency")),
        "memberCount": len(members),
        "members": members,
        # Unclaimed spots: a new member can take one of these over instead of
        # being added as an extra person with no history.
        "claimableMembers": [m for m in members if m["isPhantom"]],
        "isMember": bool(
            user_id and ObjectId(user_id) in event.get("users", [])
        ),
    }


async def join_event(
    db: AsyncIOMotorDatabase,
    code: str,
    user_id: str,
    claim_member_id: str | None = None,
) -> dict:
    """Join a group from its link, optionally claiming an existing spot."""
    from app.services import user_service

    event = await db.event.find_one({"joinCode": (code or "").strip().upper()})
    if not event:
        raise HTTPException(status_code=404, detail="This group link is not valid")

    event_id = str(event["_id"])
    user_oid = ObjectId(user_id)
    member_ids = {str(u) for u in event.get("users", [])}

    if claim_member_id:
        if claim_member_id not in member_ids:
            raise HTTPException(
                status_code=400, detail="That member is not part of this group"
            )
        if claim_member_id == user_id:
            raise HTTPException(status_code=400, detail="That spot is already yours")
        # Absorbs the placeholder's whole history — shares, payments, balances.
        await user_service.merge_phantom_into_user(db, claim_member_id, user_id)
    elif user_id in member_ids:
        return {
            "eventId": event_id,
            "eventName": event.get("eventName", ""),
            "joined": False,
            "alreadyMember": True,
        }
    else:
        await db.event.update_one(
            {"_id": event["_id"]}, {"$addToSet": {"users": user_oid}}
        )

    for member_id in member_ids:
        if member_id != user_id and member_id != claim_member_id:
            await _auto_add_friend(db, user_id, member_id)

    return {
        "eventId": event_id,
        "eventName": event.get("eventName", ""),
        "joined": True,
        "claimed": bool(claim_member_id),
    }


# ─── Create / update ────────────────────────────────────────────────────


async def get_currency_change_impact(
    db: AsyncIOMotorDatabase, event_id: str, new_currency: str
) -> dict:
    """How many transactions a currency switch would touch (drives the prompt)."""
    event = await db.event.find_one(
        {"_id": ObjectId(event_id)}, {"currency": 1, "expenses": 1}
    )
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    current = fx_service.normalize_currency(event.get("currency"))
    target = fx_service.normalize_currency(new_currency)
    expense_count = len(event.get("expenses", []))
    return {
        "fromCurrency": current,
        "toCurrency": target,
        "expenseCount": expense_count,
        "requiresChoice": target != current and expense_count > 0,
    }


async def _reconvert_event_expenses(
    db: AsyncIOMotorDatabase, event: dict, new_currency: str
) -> int:
    """Re-express every expense in the event's new currency.

    Each expense keeps the currency it was actually paid in; only the stored
    conversion into the group currency is recomputed, at the rate for that
    expense's own date.
    """
    expenses = await _load_event_expenses(db, event)
    updated = 0
    for expense in expenses:
        currency = fx_service.normalize_currency(expense.get("currency"))
        rate = await fx_service.get_rate(
            currency, new_currency, on=expense.get("date"), db=db
        )

        def _reconvert(entries: list) -> list:
            converted = []
            for entry in entries:
                original = _entry_amount(entry)
                new_entry = dict(entry)
                new_entry["originalAmount"] = original
                new_entry["amount"] = round(original * rate, 2)
                converted.append(new_entry)
            return converted

        original_total = expense.get("originalAmount")
        if original_total is None:
            original_total = expense.get("amount", 0)

        await db.expense.update_one(
            {"_id": expense["_id"]},
            {
                "$set": {
                    "amount": round(float(original_total or 0) * rate, 2),
                    "originalAmount": float(original_total or 0),
                    "baseCurrency": new_currency,
                    "fxRate": rate,
                    "shares": _reconvert(expense.get("shares", [])),
                    "payShares": _reconvert(expense.get("payShares", [])),
                }
            },
        )
        updated += 1
    return updated


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

        # Only members may edit an event.
        if ObjectId(user_id) not in event.get("users", []):
            raise HTTPException(
                status_code=403, detail="You are not a member of this event"
            )

        # Resolve users - could be ObjectId strings or email addresses
        new_user_ids, invited = await _resolve_users(
            db, request_data.get("users", [])
        )

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

        # Changing the group currency re-prices history, so the caller has to
        # choose: re-convert every transaction into the new currency, or leave
        # them as they are (each expense keeps its own recorded currency either
        # way — only the group-level view changes).
        new_currency = request_data.get("currency")
        reconverted = 0
        if new_currency:
            new_currency = fx_service.normalize_currency(new_currency)
            current_currency = fx_service.normalize_currency(event.get("currency"))
            if new_currency != current_currency:
                apply_to_expenses = request_data.get("applyToExpenses")
                expense_count = len(event.get("expenses", []))
                if expense_count and apply_to_expenses is None:
                    raise CurrencyChangeRequiresChoice(
                        expense_count, current_currency, new_currency
                    )
                update_data["currency"] = new_currency
                if expense_count and apply_to_expenses:
                    reconverted = await _reconvert_event_expenses(
                        db, event, new_currency
                    )

        await db.event.update_one({"_id": ObjectId(event_id)}, {"$set": update_data})

        # Auto-add friends
        for uid in new_user_ids:
            if str(uid) != user_id:
                await _auto_add_friend(db, user_id, str(uid))

        await _send_member_invites(db, user_id, invited)
        result = await get_event_by_id(db, event_id)
        if result is not None:
            result["reconvertedExpenses"] = reconverted
        return result
    else:
        # Create new event
        user_ids, invited = await _resolve_users(
            db, request_data.get("users", [])
        )

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

        join_code = _generate_join_code()
        while await db.event.find_one({"joinCode": join_code}):
            join_code = _generate_join_code()

        new_event = {
            "eventName": request_data.get("eventName", "Untitled Event"),
            "description": request_data.get("description", ""),
            "currency": currency,
            "users": user_ids,
            "expenses": [],
            "joinCode": join_code,
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

        await _send_member_invites(db, user_id, invited)
        new_event["_id"] = result.inserted_id
        return await get_event_by_id(db, str(result.inserted_id))


async def _resolve_users(
    db: AsyncIOMotorDatabase, user_refs: list
) -> tuple[list, list]:
    """Resolve user references to ObjectIds.

    References may be ObjectId strings or email addresses. An email that isn't a
    registered user creates a phantom account (find-or-create) so the person is
    actually added to the event instead of being silently dropped; newly
    created phantoms are returned so the caller can email them an invite.
    Returns ``(user_ids, newly_invited_phantoms)``.
    """
    from app.services import user_service

    user_ids: list = []
    invited: list = []
    for ref in user_refs:
        if isinstance(ref, dict):
            ref = ref.get("id", ref.get("_id", ""))
        ref = str(ref).strip()
        if not ref:
            continue
        try:
            oid = ObjectId(ref)
            if oid not in user_ids:
                user_ids.append(oid)
            continue
        except Exception:
            pass
        # Not an ObjectId — treat as an email and find-or-create the user.
        if "@" in ref:
            phantom = await user_service.create_phantom_user(db, email=ref)
            try:
                oid = ObjectId(phantom["id"])
            except Exception:
                continue
            if oid not in user_ids:
                user_ids.append(oid)
            if phantom.get("wasCreated"):
                invited.append(phantom)
    return user_ids, invited


async def _send_member_invites(
    db: AsyncIOMotorDatabase, inviter_id: str, invited: list
) -> None:
    """Email newly-created phantom members an invite to join."""
    if not invited:
        return
    from app.services import email_service

    inviter = await db.users.find_one({"_id": ObjectId(inviter_id)}, {"name": 1})
    inviter_name = (inviter or {}).get("name", "")
    for phantom in invited:
        email = phantom.get("email")
        if email:
            email_service.send_in_background(
                email_service.send_invite_email(
                    to_email=email,
                    to_name=phantom.get("name", ""),
                    inviter_name=inviter_name,
                )
            )


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


# ─── Dues & totals ──────────────────────────────────────────────────────


async def get_event_dues(
    db: AsyncIOMotorDatabase, event_id: str, viewer_id: str | None = None
) -> dict:
    """Calculate who owes whom within an event.

    Amounts are netted per currency; ``amount`` is that netted position
    converted into the event currency with ``viewer_id``'s rates (market rates
    when no viewer is given).
    """
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        return {"error": "Event not found"}

    expenses = await _load_event_expenses(db, event)
    ledger = _signed_ledger(expenses)
    currencies = _ledger_currencies(ledger)
    rates = await get_member_rates(db, event, viewer_id or "", currencies)

    user_name_map: dict[str, str] = {}

    async def _name(user_id: str) -> str:
        if user_id not in user_name_map:
            user_name_map[user_id] = await get_user_name_by_id(db, user_id)
        return user_name_map[user_id]

    # debtor -> creditor -> per-currency amounts owed
    debts: dict[str, dict[str, dict[str, float]]] = {}
    for (first, second), by_currency in ledger.items():
        amount = _convert(by_currency, rates)
        if abs(amount) < 0.01:
            continue
        if amount > 0:
            debtor, creditor, flip = first, second, False
        else:
            debtor, creditor, flip = second, first, True
        debts.setdefault(debtor, {})[creditor] = {
            "amount": abs(amount),
            "byCurrency": _split_by_sign(by_currency, flip=flip),
        }

    event_dues = []
    for debtor, creditors in debts.items():
        creditor_details = []
        for creditor, position in creditors.items():
            creditor_details.append(
                {
                    "id": creditor,
                    "name": await _name(creditor),
                    "amount": position["amount"],
                    "byCurrency": position["byCurrency"],
                }
            )
        event_dues.append(
            {
                "id": debtor,
                "debtor": await _name(debtor),
                "creditorDetails": creditor_details,
            }
        )

    return {
        "eventDues": event_dues,
        "currency": fx_service.normalize_currency(event.get("currency")),
        "currencies": sorted(currencies),
        "rates": rates,
    }


def _merge_currency_totals(target: dict[str, float], source: dict[str, float]) -> None:
    for currency, amount in source.items():
        target[currency] = round(target.get(currency, 0.0) + amount, 2)


async def get_event_dues_for_user(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> dict:
    """Calculate who owes whom for a specific user within an event."""
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        return {
            "userName": "",
            "currency": settings.DEFAULT_CURRENCY,
            "inDebtTo": [],
            "isOwed": [],
            "totalDebt": 0,
            "totalOwed": 0,
        }

    expenses = await _load_event_expenses(db, event)
    ledger = _signed_ledger(expenses)
    rates = await get_member_rates(db, event, user_id, _ledger_currencies(ledger))
    positions = _viewer_positions(ledger, str(user_id))

    result = {
        "userName": await get_user_name_by_id(db, user_id),
        "currency": fx_service.normalize_currency(event.get("currency")),
        "inDebtTo": [],
        "isOwed": [],
        "totalDebt": 0.0,
        "totalOwed": 0.0,
        # Per-currency totals let the app show "you owe $40 and ₹600" without
        # any conversion at all.
        "totalDebtByCurrency": {},
        "totalOwedByCurrency": {},
        "currencies": sorted(_ledger_currencies(ledger)),
        "rates": rates,
    }

    for counterpart_id, by_currency in positions.items():
        amount = _convert(by_currency, rates)
        if abs(amount) < 0.01:
            continue
        entry = {
            "id": counterpart_id,
            "name": await get_user_name_by_id(db, counterpart_id),
            "amount": abs(amount),
            "byCurrency": by_currency,
        }
        if amount > 0:
            result["inDebtTo"].append(entry)
            result["totalDebt"] += abs(amount)
        else:
            result["isOwed"].append(entry)
            result["totalOwed"] += abs(amount)

        # Per-currency totals follow each currency's own direction, which can
        # differ from the converted net (owing dollars while being owed rupees).
        _merge_currency_totals(
            result["totalDebtByCurrency"],
            {c: a for c, a in by_currency.items() if a > 0},
        )
        _merge_currency_totals(
            result["totalOwedByCurrency"],
            {c: -a for c, a in by_currency.items() if a < 0},
        )

    result["totalDebt"] = round(result["totalDebt"], 2)
    result["totalOwed"] = round(result["totalOwed"], 2)
    return result


async def get_event_summary(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> dict:
    """Per-member spend/paid totals for an event, plus the group total.

    ``paid`` is what someone put on the table, ``spend`` is their own share of
    the bills, and ``net`` is the difference — positive when the group owes
    them. Every figure is given both per original currency and converted into
    the group currency at the viewer's rates.
    """
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    expenses = await _load_event_expenses(db, event)
    base = fx_service.normalize_currency(event.get("currency"))
    currencies = _expense_currencies(expenses)
    rates = await get_member_rates(db, event, user_id, currencies)

    paid: dict[str, dict[str, float]] = {}
    spend: dict[str, dict[str, float]] = {}
    # Money moved between members to square up, kept out of "spend" so it never
    # looks like the group consumed anything. Positive = paid out.
    settled: dict[str, dict[str, float]] = {}
    group_total: dict[str, float] = {}

    def _add(bucket: dict, member_id: str, currency: str, amount: float) -> None:
        totals = bucket.setdefault(member_id, {})
        totals[currency] = round(totals.get(currency, 0.0) + amount, 2)

    for expense in expenses:
        currency = fx_service.normalize_currency(expense.get("currency"))
        is_settlement = expense.get("type") == "settle"

        for pay_share in expense.get("payShares", []):
            member_id = str(pay_share["userId"])
            amount = _entry_amount(pay_share)
            if is_settlement:
                _add(settled, member_id, currency, amount)
            else:
                _add(paid, member_id, currency, amount)
                group_total[currency] = round(
                    group_total.get(currency, 0.0) + amount, 2
                )

        for share in expense.get("shares", []):
            member_id = str(share["userId"])
            amount = _entry_amount(share)
            if is_settlement:
                # The receiving side of a settlement.
                _add(settled, member_id, currency, -amount)
            else:
                _add(spend, member_id, currency, amount)

    members = []
    for user_ref in event.get("users", []):
        member_oid = user_ref if isinstance(user_ref, ObjectId) else ObjectId(user_ref)
        member_id = str(member_oid)
        member = await db.users.find_one({"_id": member_oid}, {"name": 1, "email": 1})
        member_paid = paid.get(member_id, {})
        member_spend = spend.get(member_id, {})
        member_settled = settled.get(member_id, {})
        paid_total = _convert(member_paid, rates)
        spend_total = _convert(member_spend, rates)
        settled_total = _convert(member_settled, rates)
        members.append(
            {
                "id": member_id,
                "name": (member or {}).get("name", "Unknown"),
                "isYou": member_id == str(user_id),
                "paid": paid_total,
                "spend": spend_total,
                "settled": settled_total,
                # Positive when the group still owes them. Settling up moves
                # this towards zero without touching what anyone spent.
                "net": round(paid_total - spend_total + settled_total, 2),
                "paidByCurrency": member_paid,
                "spendByCurrency": member_spend,
                "settledByCurrency": member_settled,
            }
        )

    members.sort(key=lambda m: m["spend"], reverse=True)

    return {
        "eventId": str(event["_id"]),
        "eventName": event.get("eventName", ""),
        "currency": base,
        "currencies": sorted(currencies),
        "rates": rates,
        "total": _convert(group_total, rates),
        "totalByCurrency": group_total,
        "expenseCount": len(expenses),
        "members": members,
    }


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
