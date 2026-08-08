"""Splitwise CSV import.

Export format (one file per Splitwise group):
    Date,Description,Category,Cost,Currency,<Member 1>,<Member 2>,...
    2023-03-07,Cab sai house,Taxi,500.00,INR,-100.00,350.00,-100.00,-150.00
    ...
    2026-07-15,Total balance, , ,INR,3783.00,-2231.00,...

Each member column holds that member's net balance change for the row:
positive = they are owed (paid more than their share), negative = they owe.
Nets sum to zero per row. The final "Total balance" row is skipped.

Reconstruction into this app's model: debtors' shares are their |net|; the
creditors' combined consumption (Cost - total debt) is split equally among
them, and each creditor's payment is net + own share — this reproduces the
exact same pairwise debts Splitwise reports. Rows with category "Payment"
are settlements (type="settle").
"""

import csv
import hashlib
import io
import logging
from datetime import datetime, timezone

from bson import ObjectId

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services import email_service, expense_service, event_service, user_service

logger = logging.getLogger(__name__)

_FIXED_COLUMNS = ["Date", "Description", "Category", "Cost", "Currency"]


class SplitwiseParseError(ValueError):
    pass


def parse_splitwise_csv(content: str) -> dict:
    """Parse a Splitwise export into members + normalized rows."""
    reader = csv.reader(io.StringIO(content))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise SplitwiseParseError("The file is empty")

    header = [h.strip() for h in rows[0]]
    if header[: len(_FIXED_COLUMNS)] != _FIXED_COLUMNS:
        raise SplitwiseParseError(
            "Not a Splitwise export: expected columns "
            + ", ".join(_FIXED_COLUMNS)
        )
    members = [m for m in header[len(_FIXED_COLUMNS):] if m]
    if not members:
        raise SplitwiseParseError("No member columns found")

    parsed_rows = []
    for raw in rows[1:]:
        if len(raw) < len(_FIXED_COLUMNS) + len(members):
            continue
        date_s, description, category, cost_s, currency = (
            c.strip() for c in raw[: len(_FIXED_COLUMNS)]
        )
        if description == "Total balance":
            continue
        try:
            cost = float(cost_s)
        except ValueError:
            continue  # summary/blank rows have a blank Cost

        nets = {}
        for i, member in enumerate(members):
            cell = raw[len(_FIXED_COLUMNS) + i].strip()
            try:
                nets[member] = float(cell) if cell else 0.0
            except ValueError:
                nets[member] = 0.0

        parsed_rows.append(
            {
                "date": date_s,
                "description": description or "Imported expense",
                "category": category or "General",
                "cost": cost,
                "currency": currency.upper() or "INR",
                "nets": nets,
            }
        )

    if not parsed_rows:
        raise SplitwiseParseError("No expense rows found in the file")

    return {"members": members, "rows": parsed_rows}


def row_import_key(row: dict) -> str:
    """Stable fingerprint of a Splitwise row.

    Re-exporting a group produces byte-identical rows for expenses that already
    came across, so hashing the row is enough to import only the delta. The
    member nets are part of the key: editing an expense in Splitwise changes
    them, and that genuinely is a different transaction to us.
    """
    nets = ",".join(
        f"{member}:{row['nets'].get(member, 0.0):.2f}"
        for member in sorted(row.get("nets", {}))
    )
    payload = "|".join(
        [
            (row.get("date") or "")[:10],
            row.get("description", ""),
            f"{float(row.get('cost', 0)):.2f}",
            row.get("currency", ""),
            nets,
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _loose_key(date: str, description: str, cost: float, currency: str) -> str:
    """Fingerprint without member nets.

    Catches expenses imported before import keys existed, where all we can
    match on is what the expense document itself kept.
    """
    payload = "|".join(
        [(date or "")[:10], description or "", f"{float(cost or 0):.2f}", currency or ""]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def row_loose_key(row: dict) -> str:
    return _loose_key(
        row.get("date", ""),
        row.get("description", ""),
        row.get("cost", 0),
        row.get("currency", ""),
    )


async def existing_import_keys(db, event_id: str) -> set[str]:
    """Fingerprints of everything already sitting in an event."""
    event = await db.event.find_one({"_id": ObjectId(event_id)}, {"expenses": 1})
    if not event:
        return set()
    expense_ids = [
        ref if isinstance(ref, ObjectId) else ObjectId(ref)
        for ref in event.get("expenses", [])
    ]
    if not expense_ids:
        return set()

    expenses = await db.expense.find(
        {"_id": {"$in": expense_ids}},
        {"importKey": 1, "expenseName": 1, "date": 1, "originalAmount": 1,
         "amount": 1, "currency": 1},
    ).to_list(length=None)

    keys: set[str] = set()
    for expense in expenses:
        if expense.get("importKey"):
            keys.add(expense["importKey"])
        original = expense.get("originalAmount")
        if original is None:
            original = expense.get("amount", 0)
        keys.add(
            _loose_key(
                str(expense.get("date", ""))[:10],
                expense.get("expenseName", ""),
                original or 0,
                expense.get("currency", ""),
            )
        )
    return keys


def split_new_rows(rows: list, existing_keys: set[str]) -> tuple[list, list]:
    """Partition parsed rows into (new, already imported)."""
    new_rows, duplicates = [], []
    for row in rows:
        if row_import_key(row) in existing_keys or row_loose_key(row) in existing_keys:
            duplicates.append(row)
        else:
            new_rows.append(row)
    return new_rows, duplicates


def build_preview(
    filename: str, parsed: dict, existing_keys: set[str] | None = None
) -> dict:
    """Summary shown before the user maps members and confirms the import."""
    rows = parsed["rows"]
    currencies = sorted({r["currency"] for r in rows})
    dates = sorted(r["date"] for r in rows if r["date"])
    # "saiv-will_2026-07-15_export.csv" -> "saiv-will"
    event_name = (filename or "Imported group").rsplit("_export", 1)[0]
    for suffix_len in (11, 0):  # strip trailing "_YYYY-MM-DD" if present
        if suffix_len and len(event_name) > suffix_len:
            tail = event_name[-suffix_len:]
            if tail[0] == "_" and tail[1:5].isdigit():
                event_name = event_name[:-suffix_len]
                break
    new_rows, duplicates = split_new_rows(rows, existing_keys or set())

    return {
        "members": parsed["members"],
        "rowCount": len(rows),
        # When importing into an existing group, only the delta gets added.
        "newRowCount": len(new_rows),
        "duplicateRowCount": len(duplicates),
        "currencies": currencies,
        "dateRange": {
            "from": dates[0] if dates else None,
            "to": dates[-1] if dates else None,
        },
        "suggestedEventName": event_name.replace("-", " ").replace("_", " ").title(),
        "paymentRows": sum(1 for r in rows if r["category"].lower() == "payment"),
    }


def _row_to_expense(row: dict, member_ids: dict, event_id: str) -> dict | None:
    """Convert one Splitwise row into a create_expense payload."""
    creditors = {m: n for m, n in row["nets"].items() if n > 0.005}
    debtors = {m: -n for m, n in row["nets"].items() if n < -0.005}
    if not creditors or row["cost"] == 0:
        return None  # nothing owed on this row

    is_payment = row["category"].strip().lower() == "payment"

    if is_payment:
        # Settlement: creditor(s) paid money to debtor(s)
        pay_shares = [
            {"userId": member_ids[m], "amount": round(n, 2)}
            for m, n in creditors.items()
        ]
        shares = [
            {"userId": member_ids[m], "amount": round(n, 2), "eventId": event_id}
            for m, n in debtors.items()
        ]
        expense_type = "settle"
    else:
        total_debt = sum(debtors.values())
        creditors_consumption = max(row["cost"] - total_debt, 0.0)
        own_share = creditors_consumption / len(creditors)

        shares = [
            {"userId": member_ids[m], "amount": round(n, 2), "eventId": event_id}
            for m, n in debtors.items()
        ]
        pay_shares = []
        for m, n in creditors.items():
            pay_shares.append(
                {"userId": member_ids[m], "amount": round(n + own_share, 2)}
            )
            if own_share > 0.005:
                shares.append(
                    {
                        "userId": member_ids[m],
                        "amount": round(own_share, 2),
                        "eventId": event_id,
                    }
                )
        expense_type = "group"

    return {
        "expenseName": row["description"],
        "amount": row["cost"],
        "importKey": row_import_key(row),
        "category": row["category"] if not is_payment else "settle",
        "type": expense_type,
        "eventId": event_id,
        "currency": row["currency"],
        "date": row["date"],
        "shares": shares,
        "payShares": pay_shares,
    }


async def import_splitwise(
    db: AsyncIOMotorDatabase,
    user_id: str,
    parsed: dict,
    event_name: str,
    event_currency: str,
    mapping: dict,
    event_id: str | None = None,
    skip_existing: bool = True,
) -> dict:
    """Create an event + expenses from a parsed Splitwise export.

    ``mapping``: CSV member name -> one of

    * ``{"userId": "..."}`` — an existing account;
    * ``{"email": "...", "name": "..."}`` — creates/reuses a phantom user and
      emails them an invite;
    * ``{"name": "..."}`` or nothing at all — a name-only placeholder. The
      import runs without anyone having to be identified up front; each of
      those people can claim their own slot later via the group's invite link.

    Passing ``event_id`` imports into an existing group instead of creating a
    new one, and (unless ``skip_existing`` is off) brings in only the rows that
    are not already there — so re-exporting a group after a trip tops it up
    rather than duplicating the whole history.
    """
    importer = await user_service.get_user_by_id(db, user_id)
    importer_name = (importer or {}).get("name", "")

    member_ids: dict[str, str] = {}
    for member in parsed["members"]:
        target = mapping.get(member) or {}
        if target.get("userId"):
            member_ids[member] = target["userId"]
        elif target.get("email"):
            phantom = await user_service.create_phantom_user(
                db, email=target["email"], name=target.get("name") or member
            )
            member_ids[member] = phantom["id"]
            if phantom.get("wasCreated"):
                email_service.send_in_background(
                    email_service.send_invite_email(
                        to_email=target["email"],
                        to_name=phantom.get("name", ""),
                        inviter_name=importer_name,
                        invite_code=(importer or {}).get("friendCode", ""),
                    )
                )
        else:
            placeholder = await user_service.create_placeholder_user(
                db, name=target.get("name") or member, created_by=user_id
            )
            member_ids[member] = placeholder["id"]

    rows = parsed["rows"]
    duplicates: list[dict] = []

    if event_id:
        event = await db.event.find_one({"_id": ObjectId(event_id)})
        if not event:
            raise ValueError("Event not found")
        event_name = event.get("eventName", event_name)
        # Anyone in the file who is not in the group yet joins it.
        new_members = [
            ObjectId(mid)
            for mid in set(member_ids.values())
            if ObjectId(mid) not in event.get("users", [])
        ]
        if new_members:
            await db.event.update_one(
                {"_id": event["_id"]},
                {"$addToSet": {"users": {"$each": new_members}}},
            )
        if skip_existing:
            rows, duplicates = split_new_rows(
                rows, await existing_import_keys(db, event_id)
            )
    else:
        event = await event_service.save_event(
            db,
            user_id,
            {
                "eventName": event_name,
                "description": "Imported from Splitwise",
                "users": list(member_ids.values()),
                "currency": event_currency,
            },
        )
        event_id = event["id"]

    imported = 0
    skipped: list[dict] = []
    for index, row in enumerate(rows):
        payload = _row_to_expense(row, member_ids, event_id)
        if payload is None:
            skipped.append(
                {"row": index + 1, "description": row["description"], "reason": "no dues"}
            )
            continue
        try:
            result = await expense_service.create_expense(db, user_id, payload)
            if result.get("success") == "true":
                imported += 1
            else:
                skipped.append(
                    {
                        "row": index + 1,
                        "description": row["description"],
                        "reason": result.get("message", "rejected"),
                    }
                )
        except Exception as e:
            logger.exception("Failed to import row %s", index + 1)
            skipped.append(
                {"row": index + 1, "description": row["description"], "reason": str(e)}
            )

    return {
        "success": "true",
        "eventId": event_id,
        "eventName": event_name,
        "imported": imported,
        "duplicates": len(duplicates),
        "skipped": skipped,
        "members": member_ids,
    }
