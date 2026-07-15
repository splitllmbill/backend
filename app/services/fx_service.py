"""Currency conversion service.

Rates are resolved in this order:
1. The external FX API (``FX_API_URL``) — the user-provided endpoint that will
   serve historic rates (backfilled ~3 months) and live rates refreshed twice a
   day. Until that endpoint exists, leave ``FX_API_URL`` unset.
2. The local ``fx_rates`` Mongo collection (docs: ``{base, quote, rate, date}``),
   so rates can also be bulk-loaded directly into the DB.
3. Identity (1.0) — amounts pass through unconverted, keeping the app usable
   before any rate source is configured.

Rates are cached in-memory per (base, quote, date) for 12 hours to match the
twice-daily refresh cadence of the upstream feed.
"""

import logging
import time
from datetime import date, datetime, timezone

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import settings

logger = logging.getLogger(__name__)

# ISO code -> display metadata. Kept in sync with the Flutter currency list.
SUPPORTED_CURRENCIES = {
    "INR": {"symbol": "₹", "name": "Indian Rupee"},
    "USD": {"symbol": "$", "name": "US Dollar"},
    "EUR": {"symbol": "€", "name": "Euro"},
    "GBP": {"symbol": "£", "name": "British Pound"},
    "JPY": {"symbol": "¥", "name": "Japanese Yen"},
    "CNY": {"symbol": "¥", "name": "Chinese Yuan"},
    "AED": {"symbol": "د.إ", "name": "UAE Dirham"},
    "SGD": {"symbol": "S$", "name": "Singapore Dollar"},
    "AUD": {"symbol": "A$", "name": "Australian Dollar"},
    "CAD": {"symbol": "C$", "name": "Canadian Dollar"},
    "CHF": {"symbol": "Fr", "name": "Swiss Franc"},
    "THB": {"symbol": "฿", "name": "Thai Baht"},
    "MYR": {"symbol": "RM", "name": "Malaysian Ringgit"},
    "LKR": {"symbol": "Rs", "name": "Sri Lankan Rupee"},
    "NPR": {"symbol": "Rs", "name": "Nepalese Rupee"},
    "BDT": {"symbol": "৳", "name": "Bangladeshi Taka"},
    "IDR": {"symbol": "Rp", "name": "Indonesian Rupiah"},
    "VND": {"symbol": "₫", "name": "Vietnamese Dong"},
    "KRW": {"symbol": "₩", "name": "South Korean Won"},
    "SAR": {"symbol": "﷼", "name": "Saudi Riyal"},
    "QAR": {"symbol": "﷼", "name": "Qatari Riyal"},
    "KWD": {"symbol": "د.ك", "name": "Kuwaiti Dinar"},
    "NZD": {"symbol": "NZ$", "name": "New Zealand Dollar"},
    "ZAR": {"symbol": "R", "name": "South African Rand"},
    "HKD": {"symbol": "HK$", "name": "Hong Kong Dollar"},
    "TWD": {"symbol": "NT$", "name": "New Taiwan Dollar"},
    "PHP": {"symbol": "₱", "name": "Philippine Peso"},
    "EGP": {"symbol": "E£", "name": "Egyptian Pound"},
    "TRY": {"symbol": "₺", "name": "Turkish Lira"},
    "BRL": {"symbol": "R$", "name": "Brazilian Real"},
    "MXN": {"symbol": "Mex$", "name": "Mexican Peso"},
}

_CACHE_TTL_SECONDS = 12 * 60 * 60
# (base, quote, date-iso) -> (rate, fetched_at_monotonic)
_rate_cache: dict[tuple[str, str, str], tuple[float, float]] = {}
_warned_identity = False


def normalize_currency(code: str | None) -> str:
    """Uppercase and validate a currency code, falling back to the app default."""
    code = (code or "").strip().upper()
    if code in SUPPORTED_CURRENCIES:
        return code
    return settings.DEFAULT_CURRENCY


def _date_key(on: date | datetime | str | None) -> str:
    if on is None:
        return datetime.now(timezone.utc).date().isoformat()
    if isinstance(on, datetime):
        return on.date().isoformat()
    if isinstance(on, date):
        return on.isoformat()
    # String: keep just the date portion of an ISO timestamp
    return str(on)[:10]


async def _fetch_rate_from_api(base: str, quote: str, date_key: str) -> float | None:
    """Fetch a rate from the external FX endpoint.

    Expected contract (flexible on purpose until the real endpoint lands):
    GET {FX_API_URL}/rate?from=USD&to=INR&date=2026-07-15
    Accepted responses: {"rate": 83.2} | {"rates": {"INR": 83.2}} | 83.2
    """
    url = f"{settings.FX_API_URL.rstrip('/')}/rate"
    params = {"from": base, "to": quote, "date": date_key}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, dict):
            if "rate" in data:
                return float(data["rate"])
            rates = data.get("rates", {})
            if quote in rates:
                return float(rates[quote])
    except Exception as e:
        logger.warning("FX API lookup failed for %s->%s@%s: %s", base, quote, date_key, e)
    return None


async def _fetch_rate_from_db(
    db: AsyncIOMotorDatabase | None, base: str, quote: str, date_key: str
) -> float | None:
    """Latest stored rate on or before the requested date, direct or inverse."""
    if db is None:
        return None
    try:
        doc = await db.fx_rates.find_one(
            {"base": base, "quote": quote, "date": {"$lte": date_key}},
            sort=[("date", -1)],
        )
        if doc and doc.get("rate"):
            return float(doc["rate"])
        inverse = await db.fx_rates.find_one(
            {"base": quote, "quote": base, "date": {"$lte": date_key}},
            sort=[("date", -1)],
        )
        if inverse and inverse.get("rate"):
            return 1.0 / float(inverse["rate"])
    except Exception as e:
        logger.warning("fx_rates DB lookup failed for %s->%s: %s", base, quote, e)
    return None


async def get_rate(
    from_currency: str,
    to_currency: str,
    on: date | datetime | str | None = None,
    db: AsyncIOMotorDatabase | None = None,
) -> float:
    """Conversion rate from ``from_currency`` to ``to_currency`` on a date."""
    global _warned_identity
    base = normalize_currency(from_currency)
    quote = normalize_currency(to_currency)
    if base == quote:
        return 1.0

    date_key = _date_key(on)
    cache_key = (base, quote, date_key)
    cached = _rate_cache.get(cache_key)
    if cached and (time.monotonic() - cached[1]) < _CACHE_TTL_SECONDS:
        return cached[0]

    rate = None
    if settings.FX_API_URL:
        rate = await _fetch_rate_from_api(base, quote, date_key)
    if rate is None:
        rate = await _fetch_rate_from_db(db, base, quote, date_key)
    if rate is None:
        if not _warned_identity:
            logger.warning(
                "No FX rate source available (FX_API_URL unset, fx_rates empty); "
                "using identity rate 1.0. Cross-currency amounts will be wrong "
                "until a rate source is configured."
            )
            _warned_identity = True
        rate = 1.0

    _rate_cache[cache_key] = (rate, time.monotonic())
    return rate


async def convert(
    amount: float,
    from_currency: str,
    to_currency: str,
    on: date | datetime | str | None = None,
    db: AsyncIOMotorDatabase | None = None,
) -> float:
    rate = await get_rate(from_currency, to_currency, on=on, db=db)
    return round(float(amount) * rate, 2)


async def get_rates_to(
    target_currency: str,
    base_currencies: set[str],
    db: AsyncIOMotorDatabase | None = None,
) -> dict[str, float]:
    """Current rates from each base currency to the target, for read-time sums."""
    target = normalize_currency(target_currency)
    rates: dict[str, float] = {}
    for base in base_currencies:
        base_n = normalize_currency(base)
        if base_n not in rates:
            rates[base_n] = await get_rate(base_n, target, db=db)
    return rates
