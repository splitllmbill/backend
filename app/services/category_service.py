"""Expense categories: one vocabulary, and a table that learns.

Categories used to exist in three places that disagreed — two Flutter pickers
and the receipt-scanner prompt — so a scanned "Dining out" had to be hand-mapped
onto "Food". ``CATEGORIES`` is now the single list, ``normalize`` folds every
older or foreign spelling (including Splitwise's) onto it, and everything is
normalized on write.

Suggestions come from three places, best first:

1. **You** — what this person picked for a title like this before.
2. **Everyone** — the same, aggregated across users. This is the part that
   grows: every saved expense teaches the table a little more.
3. **Keywords** — a static seed table, so a brand-new install is still useful.

No model involved; it is counting.
"""

import re
from datetime import datetime, timezone

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

DEFAULT_CATEGORY = "General"

# The canonical vocabulary. Mirrored in the Flutter app's `Categories` list —
# tests on both sides fail if they drift.
CATEGORIES = [
    "General",
    "Food",
    "Groceries",
    "Transport",
    "Travel",
    "Accommodation",
    "Shopping",
    "Entertainment",
    "Utilities",
    "Health",
]

# Older spellings, receipt-scanner output and Splitwise's own category names.
_ALIASES = {
    "other": "General",
    "misc": "General",
    "miscellaneous": "General",
    "uncategorized": "General",
    "uncategorised": "General",
    "general": "General",
    # Food
    "dining out": "Food",
    "dining": "Food",
    "restaurant": "Food",
    "restaurants": "Food",
    "eating out": "Food",
    "liquor": "Food",
    "food and drink": "Food",
    "dining and drinks": "Food",
    # Groceries
    "grocery": "Groceries",
    "groceries": "Groceries",
    "supplies": "Groceries",
    "household supplies": "Groceries",
    # Transport
    "taxi": "Transport",
    "cab": "Transport",
    "car": "Transport",
    "gas/fuel": "Transport",
    "fuel": "Transport",
    "petrol": "Transport",
    "parking": "Transport",
    "bus/train": "Transport",
    "bus": "Transport",
    "train": "Transport",
    "transportation": "Transport",
    # Travel
    "plane": "Travel",
    "flight": "Travel",
    "flights": "Travel",
    "trip": "Travel",
    "vacation": "Travel",
    # Accommodation
    "hotel": "Accommodation",
    "rent": "Accommodation",
    "lodging": "Accommodation",
    "stay": "Accommodation",
    "home": "Accommodation",
    "airbnb": "Accommodation",
    # Shopping
    "clothing": "Shopping",
    "clothes": "Shopping",
    "gifts": "Shopping",
    "electronics": "Shopping",
    # Entertainment
    "music": "Entertainment",
    "movies": "Entertainment",
    "games": "Entertainment",
    "sports": "Entertainment",
    "activities": "Entertainment",
    # Utilities
    "electricity": "Utilities",
    "water": "Utilities",
    "internet": "Utilities",
    "phone": "Utilities",
    "tv/phone/internet": "Utilities",
    "bills": "Utilities",
    # Health
    "medical expenses": "Health",
    "medical": "Health",
    "pharmacy": "Health",
    "doctor": "Health",
    "insurance": "Health",
}

# Seed keywords, matched on whole words in the expense title. Longer phrases win
# so "cab" never fires on "cabbage" and "coffee table" is not a coffee.
_KEYWORDS: dict[str, list[str]] = {
    "Food": [
        "restaurant", "dinner", "lunch", "breakfast", "brunch", "cafe", "coffee",
        "pizza", "burger", "biryani", "swiggy", "zomato", "ubereats", "uber eats",
        "doordash", "deliveroo", "starbucks", "mcdonalds", "kfc", "dominos",
        "bar", "pub", "beer", "drinks", "snacks", "food", "meal", "takeaway",
        "dessert", "ice cream", "bakery", "chai", "tea",
    ],
    "Groceries": [
        "grocery", "groceries", "supermarket", "bigbasket", "blinkit", "zepto",
        "instamart", "dmart", "walmart", "tesco", "costco", "vegetables",
        "milk", "provisions", "kirana",
    ],
    "Transport": [
        "uber", "ola", "lyft", "taxi", "cab", "auto", "rickshaw", "metro",
        "bus", "train ticket", "fuel", "petrol", "diesel", "gas station",
        "parking", "toll", "rapido", "grab", "bolt", "car rental", "bike",
    ],
    "Travel": [
        "flight", "flights", "airfare", "airline", "indigo", "airasia",
        "emirates", "boarding", "visa fee", "passport", "baggage", "airport",
        "makemytrip", "booking.com", "expedia", "travel insurance", "train fare",
    ],
    "Accommodation": [
        "hotel", "hostel", "airbnb", "resort", "lodge", "stay", "room",
        "accommodation", "villa", "guesthouse", "oyo", "rent", "deposit",
    ],
    "Shopping": [
        "amazon", "flipkart", "myntra", "shopping", "clothes", "shirt", "shoes",
        "mall", "ikea", "decathlon", "zara", "uniqlo", "gift", "souvenir",
        "electronics", "headphones", "charger",
    ],
    "Entertainment": [
        "movie", "movies", "cinema", "pvr", "inox", "netflix", "spotify",
        "concert", "tickets", "museum", "park", "game", "gaming", "bowling",
        "club", "show", "theatre", "theater", "subscription",
    ],
    "Utilities": [
        "electricity", "water bill", "gas bill", "internet", "wifi", "broadband",
        "airtel", "jio", "vodafone", "recharge", "mobile bill", "maintenance",
        "utility", "utilities", "cleaning",
    ],
    "Health": [
        "pharmacy", "medicine", "medical", "doctor", "hospital", "clinic",
        "dentist", "apollo", "insurance", "gym", "test", "vaccine",
    ],
}

# Longest phrases first, so a specific match beats a generic one.
_KEYWORD_INDEX: list[tuple[str, str]] = sorted(
    ((keyword, category) for category, words in _KEYWORDS.items() for keyword in words),
    key=lambda pair: -len(pair[0]),
)

_STOPWORDS = {
    "the", "and", "for", "with", "from", "our", "out", "was", "were", "has",
    "had", "this", "that", "paid", "pay", "bill", "expense", "expenses", "cost",
    "some", "few", "new", "old", "his", "her", "their", "your", "you", "one",
    "two", "all", "day", "night", "trip", "back", "got", "get",
}

_HINTS = "category_hints"
# A whole-title hint is worth more than any single word from it.
_TITLE_WEIGHT = 3


def normalize(value: str | None) -> str:
    """Fold any spelling onto the canonical vocabulary."""
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_CATEGORY
    for category in CATEGORIES:
        if raw.lower() == category.lower():
            return category
    return _ALIASES.get(raw.lower(), DEFAULT_CATEGORY)


def _clean(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).strip()


def tokenize(title: str) -> list[str]:
    """Hint keys for a title: the whole thing, plus its meaningful words."""
    cleaned = _clean(title)
    if not cleaned:
        return []
    words = [
        word
        for word in cleaned.split()
        if len(word) >= 3 and word not in _STOPWORDS and not word.isdigit()
    ]
    keys = [f"~{' '.join(cleaned.split())}"]  # "~" marks the whole-title key
    keys.extend(dict.fromkeys(words))
    return keys


def suggest_from_keywords(title: str) -> str | None:
    """The static seed table: longest whole-word match wins."""
    cleaned = f" {_clean(title)} "
    if not cleaned.strip():
        return None
    for keyword, category in _KEYWORD_INDEX:
        if f" {keyword} " in cleaned:
            return category
    return None


async def _score_hints(
    db: AsyncIOMotorDatabase, keys: list[str], user_id: str | None
) -> tuple[str, float] | None:
    """Best category for these keys among one user's history, or everyone's."""
    query = {
        "token": {"$in": keys},
        "userId": ObjectId(user_id) if user_id else None,
    }
    totals: dict[str, float] = {}
    async for hint in db[_HINTS].find(query):
        weight = _TITLE_WEIGHT if hint["token"].startswith("~") else 1
        for category, count in (hint.get("counts") or {}).items():
            totals[category] = totals.get(category, 0.0) + count * weight
    if not totals:
        return None

    best = max(totals, key=lambda category: totals[category])
    total = sum(totals.values())
    return best, (totals[best] / total if total else 0.0)


async def suggest(
    db: AsyncIOMotorDatabase, title: str, user_id: str | None = None
) -> dict | None:
    """Suggest a category for an expense title.

    Returns ``{"category", "source", "confidence"}`` or None when nothing is
    known — the caller should leave whatever the user already picked alone.
    """
    keys = tokenize(title)
    if not keys:
        return None

    if user_id:
        personal = await _score_hints(db, keys, user_id)
        # Your own history wins on a single confident precedent: if you called
        # it Groceries last time, you meant it.
        if personal and personal[1] >= 0.5:
            return {
                "category": personal[0],
                "source": "you",
                "confidence": round(personal[1], 2),
            }

    community = await _score_hints(db, keys, None)
    # Everyone else needs to actually agree before overriding the seed table.
    if community and community[1] >= 0.6:
        return {
            "category": community[0],
            "source": "community",
            "confidence": round(community[1], 2),
        }

    keyword = suggest_from_keywords(title)
    if keyword:
        return {"category": keyword, "source": "keyword", "confidence": 0.5}

    if community:
        return {
            "category": community[0],
            "source": "community",
            "confidence": round(community[1], 2),
        }
    return None


async def learn(
    db: AsyncIOMotorDatabase, title: str, category: str, user_id: str | None = None
) -> None:
    """Record that this title was filed under this category.

    Called on every saved expense, which is what makes the table grow. Both the
    person's own tally and the shared one are incremented; a correction simply
    outvotes the old answer over time rather than needing to erase it.
    """
    category = normalize(category)
    keys = tokenize(title)
    if not keys:
        return

    now = datetime.now(timezone.utc)
    owners = [None]
    if user_id:
        owners.append(ObjectId(user_id))

    operations = []
    from pymongo import UpdateOne

    for owner in owners:
        for key in keys:
            operations.append(
                UpdateOne(
                    {"token": key, "userId": owner},
                    {
                        "$inc": {f"counts.{category}": 1, "total": 1},
                        "$set": {"updatedAt": now},
                    },
                    upsert=True,
                )
            )
    if operations:
        try:
            await db[_HINTS].bulk_write(operations, ordered=False)
        except Exception:
            # Learning is a nicety; never fail saving an expense over it.
            pass
