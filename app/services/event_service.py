import secrets
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.config import settings
from app.services import audit_service, fx_service
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
            # Collection is `users` (plural). Looking up "user" always matched
            # nothing, so every member resolved to "Unknown" in the event list.
            "$lookup": {
                "from": "users",
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
        {"_id": ObjectId(user_id)}, {"defaultCurrency": 1, "fxRates": 1}
    )
    viewer_currency = fx_service.normalize_currency(
        (viewer or {}).get("defaultCurrency")
    )
    event_currencies = {
        fx_service.normalize_currency(e.get("currency")) for e in events
    }
    to_viewer_rates = await fx_service.get_rates_to(
        viewer_currency,
        event_currencies,
        db=db,
        tables=[((viewer or {}).get("fxRates"), viewer_currency)],
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
                    # Lets the editor show "not invited yet" instead of an
                    # empty email, and keeps the chip removable by id.
                    "isPlaceholder": bool(user.get("claimable"))
                    and not user.get("email"),
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

        # Only members may edit an event.
        if ObjectId(user_id) not in event.get("users", []):
            raise HTTPException(
                status_code=403, detail="You are not a member of this event"
            )

        # Resolve users - could be ObjectId strings or email addresses. An
        # absent `users` key means "leave the membership alone", not "empty it".
        if "users" in request_data:
            new_user_ids, invited = await _resolve_users(
                db,
                request_data["users"],
                actor_id=user_id,
                extra_allowed={str(u) for u in event.get("users", [])},
            )
        else:
            new_user_ids, invited = list(event.get("users", [])), []

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

        # `.get(key, default)` returns None when the key is present *and* null,
        # which a partial client update sends — that wiped the event's name.
        update_data = {
            "eventName": request_data.get("eventName") or event.get("eventName"),
            "description": request_data.get("description")
            or event.get("description", ""),
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

        # The group's own FX rates. Unlike the currency these stay editable
        # forever: they only affect expenses entered *after* the change, since
        # each expense stores the rate it was converted at.
        if request_data.get("fxRates") is not None:
            update_data["fxRates"] = fx_service.clean_rate_table(
                request_data["fxRates"]
            )

        if request_data.get("simplifyDebts") is not None:
            update_data["simplifyDebts"] = bool(request_data["simplifyDebts"])

        await db.event.update_one({"_id": ObjectId(event_id)}, {"$set": update_data})

        updated = await db.event.find_one({"_id": ObjectId(event_id)})
        await audit_service.record(
            db,
            entity="event",
            entity_id=event_id,
            action="update",
            actor_id=user_id,
            # Removed members see the change that removed them.
            audience=list(event.get("users", []))
            + list((updated or {}).get("users", [])),
            summary=f"the group {update_data['eventName']}",
            before=audit_service.snapshot(event),
            after=audit_service.snapshot(updated),
        )

        # Auto-add friends
        for uid in new_user_ids:
            if str(uid) != user_id:
                await _auto_add_friend(db, user_id, str(uid))

        await _send_member_invites(db, user_id, invited)
        return await get_event_by_id(db, event_id)
    else:
        # Create new event
        user_ids, invited = await _resolve_users(
            db, request_data.get("users", []), actor_id=user_id
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

        new_event = {
            "eventName": request_data.get("eventName") or "Untitled Event",
            "description": request_data.get("description") or "",
            "currency": currency,
            "fxRates": fx_service.clean_rate_table(request_data.get("fxRates")),
            "simplifyDebts": bool(request_data.get("simplifyDebts")),
            "users": user_ids,
            "expenses": [],
            "createdBy": creator_oid,
            "createdAt": now,
            "updatedBy": creator_oid,
            "updatedAt": now,
        }
        result = await db.event.insert_one(new_event)
        new_event["_id"] = result.inserted_id

        await audit_service.record(
            db,
            entity="event",
            entity_id=result.inserted_id,
            action="create",
            actor_id=user_id,
            audience=user_ids,
            summary=f"the group {new_event['eventName']}",
            after=audit_service.snapshot(new_event),
        )

        # Auto-add friends
        for uid in user_ids:
            if str(uid) != user_id:
                await _auto_add_friend(db, user_id, str(uid))

        await _send_member_invites(db, user_id, invited)
        return await get_event_by_id(db, str(result.inserted_id))


async def _is_phantom(db: AsyncIOMotorDatabase, oid: ObjectId) -> bool:
    """True if the id belongs to a placeholder account nobody has claimed.

    Phantoms represent people who have not registered, so adding one to an
    event cannot expose a real user's data — the Splitwise import creates them
    and then passes their ids straight into event creation.
    """
    user = await db.users.find_one(
        {"_id": oid}, {"isPhantom": 1, "firebaseUid": 1}
    )
    if not user:
        return False
    return bool(user.get("isPhantom")) or str(
        user.get("firebaseUid", "")
    ).startswith("phantom_")


async def _resolve_users(
    db: AsyncIOMotorDatabase,
    user_refs: list,
    actor_id: str,
    extra_allowed: set[str] | None = None,
) -> tuple[list, list]:
    """Resolve user references to ObjectIds.

    A reference is one of:

    * an ObjectId string — an existing user (a friend, or a current member);
    * an email address — find-or-create a phantom account so the person is
      actually added instead of being silently dropped, and they get an invite;
    * ``{"name": "Sam"}`` (optionally with ``"email"``) — a name-only
      placeholder for someone who is not being invited yet. Whoever joins the
      group by link can later claim that slot as themselves.

    Returns ``(user_ids, newly_invited_phantoms)``; only the ones with an email
    are worth mailing, so placeholders never appear in the second list.

    ``actor_id`` bounds who may be added: the actor's existing friends, plus
    people who are not registered yet (a fresh phantom the actor is inviting),
    plus ``extra_allowed`` (on edit, the event's current members — they may
    have been added by a different member and need not be the actor's friends).
    An existing registered stranger is rejected — otherwise any authenticated
    user could pull an arbitrary victim into an event they control, which
    exposes the victim's email and (via _auto_add_friend) writes an unconsented
    friendship into the victim's own document.
    """
    from app.services import user_service

    actor_oid = ObjectId(actor_id)
    actor = await db.users.find_one({"_id": actor_oid}, {"friends": 1})
    allowed = {str(f) for f in (actor or {}).get("friends", [])}
    allowed.add(str(actor_oid))
    allowed |= extra_allowed or set()

    user_ids: list = []
    invited: list = []
    rejected: list = []
    for ref in user_refs:
        if isinstance(ref, dict):
            explicit_id = ref.get("id") or ref.get("_id") or ""
            if not explicit_id:
                # New member given by name (and maybe an email). With an email
                # this is the ordinary invite path; without one it is a
                # placeholder nobody is being emailed about.
                email = str(ref.get("email") or "").strip()
                name = str(ref.get("name") or "").strip()
                if email:
                    phantom = await user_service.create_phantom_user(
                        db, email=email, name=name
                    )
                    try:
                        oid = ObjectId(phantom["id"])
                    except Exception:
                        continue
                    if (
                        not phantom.get("wasCreated")
                        and str(oid) not in allowed
                        and not await _is_phantom(db, oid)
                    ):
                        rejected.append(email)
                        continue
                    if oid not in user_ids:
                        user_ids.append(oid)
                    if phantom.get("wasCreated"):
                        invited.append(phantom)
                elif name:
                    placeholder = await user_service.create_placeholder_user(
                        db, name=name, created_by=actor_id
                    )
                    try:
                        user_ids.append(ObjectId(placeholder["id"]))
                    except Exception:
                        pass
                continue
            ref = explicit_id
        ref = str(ref).strip()
        if not ref:
            continue
        try:
            oid = ObjectId(ref)
            if str(oid) not in allowed and not await _is_phantom(db, oid):
                rejected.append(ref)
                continue
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
            # A pre-existing *registered* account reached by email is only
            # allowed if the actor is already connected to them.
            if (
                not phantom.get("wasCreated")
                and str(oid) not in allowed
                and not await _is_phantom(db, oid)
            ):
                rejected.append(ref)
                continue
            if oid not in user_ids:
                user_ids.append(oid)
            if phantom.get("wasCreated"):
                invited.append(phantom)

    if rejected:
        raise HTTPException(
            status_code=403,
            detail="You can only add your friends or invite new people by email.",
        )
    return user_ids, invited


async def _send_member_invites(
    db: AsyncIOMotorDatabase, inviter_id: str, invited: list
) -> None:
    """Email newly-created phantom members an invite to join."""
    if not invited:
        return
    from app.services import email_service

    inviter = await db.users.find_one(
        {"_id": ObjectId(inviter_id)}, {"name": 1, "friendCode": 1}
    )
    inviter_name = (inviter or {}).get("name", "")
    for phantom in invited:
        email = phantom.get("email")
        if email:
            email_service.send_in_background(
                email_service.send_invite_email(
                    to_email=email,
                    to_name=phantom.get("name", ""),
                    inviter_name=inviter_name,
                    invite_code=(inviter or {}).get("friendCode", ""),
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


#: Join-code alphabet. Excludes the characters people confuse when retyping a
#: code read off a screen (0/O, 1/I/L) — the code travels in a link, but it is
#: also short enough that someone will inevitably type it by hand.
_JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_JOIN_CODE_LENGTH = 10


def generate_join_code(length: int = _JOIN_CODE_LENGTH) -> str:
    """Generate a group join code.

    `secrets`, not `random`: possessing this code is what authorises joining a
    group, so it has to be unguessable — same reasoning as
    user_service.generate_friend_code.
    """
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(length))


async def _new_join_code(db: AsyncIOMotorDatabase) -> str:
    code = generate_join_code()
    while await db.event.find_one({"joinCode": code}, {"_id": 1}):
        code = generate_join_code()
    return code


async def get_join_link(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str, rotate: bool = False
) -> dict:
    """Return the group's join code, creating (or rotating) it on demand.

    Codes are created lazily so groups that never share a link never carry one.
    Rotating invalidates every link already handed out, which is the only way
    to undo an over-shared invite.
    """
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    code = event.get("joinCode")
    if rotate or not code:
        code = await _new_join_code(db)
        await db.event.update_one(
            {"_id": event["_id"]},
            {
                "$set": {
                    "joinCode": code,
                    "joinCodeUpdatedAt": datetime.now(timezone.utc),
                    "updatedBy": ObjectId(user_id),
                }
            },
        )
    return {"joinCode": code, "eventName": event.get("eventName", "")}


async def disable_join_link(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> dict:
    """Turn off link joining for a group. Existing links stop working."""
    result = await db.event.update_one(
        {"_id": ObjectId(event_id)},
        {
            "$unset": {"joinCode": "", "joinCodeUpdatedAt": ""},
            "$set": {"updatedBy": ObjectId(user_id)},
        },
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"success": True}


async def preview_join_code(db: AsyncIOMotorDatabase, code: str) -> dict:
    """What an invitee sees before signing in: just enough to decide to join.

    Deliberately public and deliberately thin — the group's name, size and
    currency. No member list, no balances, no expenses. The code is the only
    credential, so anything richer here would leak to whoever the link reached.
    """
    event = await db.event.find_one(
        {"joinCode": (code or "").strip().upper()},
        {"eventName": 1, "users": 1, "currency": 1},
    )
    if not event:
        raise HTTPException(status_code=404, detail="This invite link is not valid")
    return {
        "eventId": str(event["_id"]),
        "eventName": event.get("eventName", ""),
        "memberCount": len(event.get("users", [])),
        "currency": event.get("currency"),
    }


async def _is_claimable_placeholder(db: AsyncIOMotorDatabase, oid: ObjectId) -> bool:
    """True for a name-only slot anyone joining by link may claim.

    An email-backed phantom is excluded: it stands for a specific address, and
    letting whoever holds the link claim it would hand them that person's
    shares and balances.
    """
    user = await db.users.find_one(
        {"_id": oid}, {"claimable": 1, "email": 1, "firebaseUid": 1, "isPhantom": 1}
    )
    if not user:
        return False
    if user.get("email"):
        return False
    unregistered = bool(user.get("isPhantom")) or str(
        user.get("firebaseUid", "")
    ).startswith("phantom_")
    return bool(user.get("claimable")) and unregistered


async def get_claimable_members(db: AsyncIOMotorDatabase, code: str) -> dict:
    """The unclaimed name-only slots in the group a join code points at.

    Authenticated callers only (see the router): this is the group's member
    names, which the public preview deliberately withholds.
    """
    event = await db.event.find_one(
        {"joinCode": (code or "").strip().upper()}, {"eventName": 1, "users": 1}
    )
    if not event:
        raise HTTPException(status_code=404, detail="This invite link is not valid")

    members = []
    for member in event.get("users", []):
        if await _is_claimable_placeholder(db, member):
            user = await db.users.find_one({"_id": member}, {"name": 1})
            members.append({"id": str(member), "name": (user or {}).get("name", "")})
    return {"eventName": event.get("eventName", ""), "members": members}


async def merge_placeholder_into_user(
    db: AsyncIOMotorDatabase, placeholder_id: ObjectId, user_id: ObjectId
) -> None:
    """Rewrite every reference to a placeholder to point at a real account.

    The merge is global rather than per-group: a placeholder can have been
    added to more than one group (members auto-friend each other, so it shows
    up in their friend lists too), and leaving half the references behind would
    split one person's balances across two identities.
    """
    # Expense shares: the placeholder's amounts become the real user's. An
    # expense that already names both would end up with two rows for the same
    # person, so those are merged rather than rewritten.
    expenses = await db.expense.find(
        {
            "$or": [
                {"shares.userId": placeholder_id},
                {"payShares.userId": placeholder_id},
            ]
        }
    ).to_list(length=None)
    for expense in expenses:
        update = {}
        for field in ("shares", "payShares"):
            rows = expense.get(field, [])
            if not any(r.get("userId") == placeholder_id for r in rows):
                continue
            merged: list = []
            for row in rows:
                if row.get("userId") == placeholder_id:
                    row = {**row, "userId": user_id}
                existing = next(
                    (m for m in merged if m.get("userId") == row.get("userId")), None
                )
                if existing:
                    existing["amount"] = float(existing.get("amount", 0)) + float(
                        row.get("amount", 0)
                    )
                else:
                    merged.append(dict(row))
            update[field] = merged
        if update:
            await db.expense.update_one({"_id": expense["_id"]}, {"$set": update})

    # Group membership, friend lists and pending settle-ups.
    await db.event.update_many(
        {"users": placeholder_id}, {"$addToSet": {"users": user_id}}
    )
    await db.event.update_many(
        {"users": placeholder_id}, {"$pull": {"users": placeholder_id}}
    )
    await db.users.update_many(
        {"friends": placeholder_id}, {"$addToSet": {"friends": user_id}}
    )
    await db.users.update_many(
        {"friends": placeholder_id}, {"$pull": {"friends": placeholder_id}}
    )
    # Nobody can be their own friend — the claimer was already a member of some
    # of those groups, so this is reachable.
    await db.users.update_one({"_id": user_id}, {"$pull": {"friends": user_id}})
    for field in ("payerId", "payeeId"):
        await db.settlement_request.update_many(
            {field: placeholder_id}, {"$set": {field: user_id}}
        )

    await db.users.delete_one({"_id": placeholder_id})


async def join_event_by_code(
    db: AsyncIOMotorDatabase, code: str, user_id: str, claim_member_id: str = ""
) -> dict:
    """Add the caller to the group the code belongs to.

    This is the one path that adds a member without the actor being an existing
    member's friend: consent runs the other way round — a member handed out the
    link, and the joiner chose to use it. As with any other membership change,
    everyone involved becomes friends so later expenses can be split.

    ``claim_member_id`` says "I am this placeholder": their existing shares in
    the group transfer to the joiner instead of the joiner starting at zero.
    """
    event = await db.event.find_one({"joinCode": (code or "").strip().upper()})
    if not event:
        raise HTTPException(status_code=404, detail="This invite link is not valid")

    user_oid = ObjectId(user_id)
    members = event.get("users", [])

    if claim_member_id:
        try:
            placeholder_oid = ObjectId(claim_member_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid member selected")
        if placeholder_oid not in members:
            raise HTTPException(
                status_code=400, detail="That member is not in this group"
            )
        if not await _is_claimable_placeholder(db, placeholder_oid):
            raise HTTPException(
                status_code=400, detail="That member has already been claimed"
            )
        await merge_placeholder_into_user(db, placeholder_oid, user_oid)
        event = await db.event.find_one({"_id": event["_id"]})
        members = event.get("users", [])

    # Claiming a placeholder already put the joiner in `users`, so this is only
    # false when they arrived without claiming anyone.
    already_member = user_oid in members
    if not already_member:
        await db.event.update_one(
            {"_id": event["_id"]},
            {
                "$addToSet": {"users": user_oid},
                "$set": {"updatedBy": user_oid, "updatedAt": datetime.now(timezone.utc)},
            },
        )
        for member in members:
            if str(member) != user_id:
                await _auto_add_friend(db, user_id, str(member))

    if not already_member or claim_member_id:
        updated = await db.event.find_one({"_id": event["_id"]})
        await audit_service.record(
            db,
            entity="event",
            entity_id=str(event["_id"]),
            action="update",
            actor_id=user_id,
            audience=list((updated or {}).get("users", [])),
            summary=f"the group {event.get('eventName', '')}",
            before=audit_service.snapshot(event),
            after=audit_service.snapshot(updated),
        )

    return {
        "eventId": str(event["_id"]),
        "eventName": event.get("eventName", ""),
        # From the joiner's point of view "already a member" means they had
        # nothing to do — claiming a slot is very much something.
        "alreadyMember": already_member and not claim_member_id,
        "claimed": bool(claim_member_id),
    }


async def delete_event(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str | None = None
) -> dict:
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

    # Snapshot the expenses before deleting them: undoing the group deletion
    # has to bring its history back, not just an empty group.
    expense_ids = [
        expense_ref if isinstance(expense_ref, ObjectId) else ObjectId(expense_ref)
        for expense_ref in event.get("expenses", [])
    ]
    deleted_expenses = await db.expense.find({"_id": {"$in": expense_ids}}).to_list(
        length=None
    )
    if expense_ids:
        await db.expense.delete_many({"_id": {"$in": expense_ids}})

    await db.event.delete_one({"_id": ObjectId(event_id)})

    await audit_service.record(
        db,
        entity="event",
        entity_id=event_id,
        action="delete",
        actor_id=user_id or event.get("createdBy"),
        audience=event.get("users", []),
        summary=f"the group {event.get('eventName', '')}",
        before=audit_service.snapshot(event),
        extra={"deletedExpenses": deleted_expenses},
    )
    return {"message": "Successfully deleted event", "success": "true"}


#: Balances below this are treated as zero. Splitting a bill three ways leaves
#: sub-cent residue that would otherwise show up as a "you owe ₹0.00" row and
#: block event deletion forever.
_SETTLED_EPSILON = 0.01


def gross_debts(expenses: list) -> dict[str, dict[str, float]]:
    """Raw ``{debtor: {creditor: amount}}`` implied by every expense.

    A participant owes each payer in proportion to how much of the bill that
    payer actually fronted, so a dinner two people split but one paid produces
    a single debt, and one they both paid produces two.
    """
    debts: dict[str, dict[str, float]] = {}
    for expense in expenses:
        pay_shares = expense.get("payShares", [])
        total_paid = sum(float(ps["amount"]) for ps in pay_shares)
        if total_paid == 0:
            continue
        for share in expense.get("shares", []):
            participant_id = str(share["userId"])
            share_amount = float(share["amount"])
            for pay_share in pay_shares:
                payer_id = str(pay_share["userId"])
                if participant_id == payer_id:
                    continue
                portion = share_amount * (float(pay_share["amount"]) / total_paid)
                debts.setdefault(participant_id, {}).setdefault(payer_id, 0.0)
                debts[participant_id][payer_id] += portion
    return debts


def net_pairwise(debts: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Cancel each pair's two directions against each other.

    A owing B ₹100 while B owes A ₹40 is one ₹60 debt, not two. Everything is
    computed into a fresh dict — the previous implementation rewrote
    ``amounts_owed`` while looping over it, which both corrupted the running
    totals and could raise "dictionary changed size during iteration" once a
    pair's reverse debt did not already exist.
    """
    people = set(debts) | {c for row in debts.values() for c in row}
    netted: dict[str, dict[str, float]] = {}
    for debtor in people:
        for creditor in people:
            if debtor >= creditor:  # visit each unordered pair once
                continue
            forward = debts.get(debtor, {}).get(creditor, 0.0)
            reverse = debts.get(creditor, {}).get(debtor, 0.0)
            net = forward - reverse
            if net > _SETTLED_EPSILON:
                netted.setdefault(debtor, {})[creditor] = net
            elif -net > _SETTLED_EPSILON:
                netted.setdefault(creditor, {})[debtor] = -net
    return netted


def simplify_debts(
    debts: dict[str, dict[str, float]],
) -> dict[str, dict[str, float]]:
    """Reduce the whole group to the fewest transfers that clear every balance.

    Pairwise netting still leaves A→B→C chains. This collapses each person to a
    single net balance and then repeatedly settles the largest debtor against
    the largest creditor, which is the standard greedy min-cash-flow reduction:
    every transfer zeroes at least one person, so n people need at most n-1
    transfers instead of up to n(n-1)/2.

    Totals are preserved — each person's net position is identical to before,
    only the routing changes. That is the whole trade-off: you may end up
    paying someone you never shared a bill with.
    """
    balances: dict[str, float] = {}
    for debtor, row in debts.items():
        for creditor, amount in row.items():
            balances[debtor] = balances.get(debtor, 0.0) - amount
            balances[creditor] = balances.get(creditor, 0.0) + amount

    debtors = sorted(
        ((p, -b) for p, b in balances.items() if b < -_SETTLED_EPSILON),
        key=lambda x: -x[1],
    )
    creditors = sorted(
        ((p, b) for p, b in balances.items() if b > _SETTLED_EPSILON),
        key=lambda x: -x[1],
    )

    simplified: dict[str, dict[str, float]] = {}
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        debtor, owed = debtors[i]
        creditor, due = creditors[j]
        transfer = min(owed, due)
        if transfer > _SETTLED_EPSILON:
            simplified.setdefault(debtor, {})[creditor] = (
                simplified.setdefault(debtor, {}).get(creditor, 0.0) + transfer
            )
        debtors[i] = (debtor, owed - transfer)
        creditors[j] = (creditor, due - transfer)
        if debtors[i][1] <= _SETTLED_EPSILON:
            i += 1
        if creditors[j][1] <= _SETTLED_EPSILON:
            j += 1
    return simplified


async def get_event_dues(db: AsyncIOMotorDatabase, event_id: str) -> dict:
    """Calculate who owes whom within an event.

    Groups with ``simplifyDebts`` set get the minimum-transfer routing; the
    rest keep strict pairwise balances, so you only ever settle with people you
    actually shared a bill with.
    """
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        return {"error": "Event not found"}

    expense_ids = [
        exp_ref if isinstance(exp_ref, ObjectId) else ObjectId(exp_ref)
        for exp_ref in event.get("expenses", [])
    ]
    expenses = await db.expense.find({"_id": {"$in": expense_ids}}).to_list(
        length=None
    )

    simplify = bool(event.get("simplifyDebts"))
    debts = gross_debts(expenses)
    debts = simplify_debts(debts) if simplify else net_pairwise(debts)

    # One name lookup per person, not per pair.
    people = set(debts) | {c for row in debts.values() for c in row}
    user_name_map = {p: await get_user_name_by_id(db, p) for p in people}

    event_dues = [
        {
            "id": debtor,
            "debtor": user_name_map.get(debtor, "Unknown"),
            "creditorDetails": [
                {
                    "id": creditor,
                    "name": user_name_map.get(creditor, "Unknown"),
                    "amount": round(amount, 2),
                }
                for creditor, amount in row.items()
                if amount > _SETTLED_EPSILON
            ],
        }
        for debtor, row in debts.items()
    ]

    return {
        "eventDues": event_dues,
        "currency": event.get("currency", settings.DEFAULT_CURRENCY),
        "simplifyDebts": simplify,
    }


async def get_event_summary(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> dict:
    """Per-member spend/paid totals for an event, plus the group total.

    Three different questions, which a single "balance" number conflates:

    * ``paid`` — what someone put on the table;
    * ``spend`` — their own share of the bills, i.e. what the group cost them;
    * ``net`` — the difference, positive when the group still owes them.

    Settling up is tracked separately and kept out of ``spend``, so squaring a
    debt moves ``net`` towards zero without ever looking like the group
    consumed something. Dues answer "who pays whom"; this answers "where did
    the money go", and the two are not the same question.
    """
    event = await db.event.find_one({"_id": ObjectId(event_id)})
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    expense_ids = [
        ref if isinstance(ref, ObjectId) else ObjectId(ref)
        for ref in event.get("expenses", [])
    ]
    expenses = await db.expense.find({"_id": {"$in": expense_ids}}).to_list(
        length=None
    )

    paid: dict[str, float] = {}
    spend: dict[str, float] = {}
    # Money moved between members to square up. Positive = paid out.
    settled: dict[str, float] = {}
    group_total = 0.0

    for expense in expenses:
        is_settlement = expense.get("type") == "settle"

        for pay_share in expense.get("payShares", []):
            member_id = str(pay_share["userId"])
            amount = float(pay_share.get("amount", 0))
            if is_settlement:
                settled[member_id] = settled.get(member_id, 0.0) + amount
            else:
                paid[member_id] = paid.get(member_id, 0.0) + amount
                group_total += amount

        for share in expense.get("shares", []):
            member_id = str(share["userId"])
            amount = float(share.get("amount", 0))
            if is_settlement:
                # The receiving side of a settlement.
                settled[member_id] = settled.get(member_id, 0.0) - amount
            else:
                spend[member_id] = spend.get(member_id, 0.0) + amount

    member_oids = [
        ref if isinstance(ref, ObjectId) else ObjectId(ref)
        for ref in event.get("users", [])
    ]
    # One query for every name, rather than one per member.
    docs = await db.users.find(
        {"_id": {"$in": member_oids}}, {"name": 1}
    ).to_list(length=None)
    names = {str(d["_id"]): d.get("name", "Unknown") for d in docs}

    members = []
    for oid in member_oids:
        member_id = str(oid)
        member_paid = round(paid.get(member_id, 0.0), 2)
        member_spend = round(spend.get(member_id, 0.0), 2)
        member_settled = round(settled.get(member_id, 0.0), 2)
        members.append(
            {
                "id": member_id,
                "name": names.get(member_id, "Unknown"),
                "isYou": member_id == str(user_id),
                "paid": member_paid,
                "spend": member_spend,
                "settled": member_settled,
                # Positive when the group still owes them. Settling up moves
                # this towards zero without touching what anyone spent.
                "net": round(member_paid - member_spend + member_settled, 2),
            }
        )

    members.sort(key=lambda m: m["spend"], reverse=True)

    return {
        "eventId": str(event["_id"]),
        "eventName": event.get("eventName", ""),
        "currency": event.get("currency", settings.DEFAULT_CURRENCY),
        "groupTotal": round(group_total, 2),
        "expenseCount": sum(1 for e in expenses if e.get("type") != "settle"),
        "members": members,
    }


async def get_event_dues_for_user(
    db: AsyncIOMotorDatabase, event_id: str, user_id: str
) -> dict:
    """Calculate who owes whom for a specific user within an event."""
    result = {
        "userName": "",
        "currency": settings.DEFAULT_CURRENCY,
        "simplifyDebts": False,
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
    result["simplifyDebts"] = bool(event_dues_summary.get("simplifyDebts"))

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
