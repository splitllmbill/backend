"""PayPal + UPI settlement rail for SplitLLM.

Settle a friend debt directly between two people — SplitLLM never holds money:

* **PayPal rail** (US / cross-border): the backend creates a PayPal Orders API
  v2 order whose ``payee`` is the *creditor* (PayPal's own "Pay another
  account" flow). The debtor approves it in PayPal Checkout, the backend
  captures it, and only a server-side ``verify_capture`` (order COMPLETED,
  capture COMPLETED, amount and currency match exactly) marks the debt
  settled. The client-side redirect proves nothing and is never trusted.
* **UPI rail** (India-domestic): a UPI intent deep link + QR code built per
  NPCI's UPI Linking Specification. Tapping it opens the debtor's own UPI
  app with the payment prefilled. There is no callback or webhook, so a UPI
  settlement stays ``PENDING_CONFIRMATION`` until the creditor confirms it
  through the existing ``POST /db/settlements/{id}/confirm`` endpoint.

Deliberately NOT used: the Payouts API (business disbursements only; its
terms forbid friends-and-family use), the Invoices API (merchant billing),
partner/progressive-onboarding flows (need approved partner status), and any
Friends & Family API (none exists — Orders API payments are commercial and
the receiver pays the fee, shown before confirming).

Money rules (non-negotiable):
1. Credentials come from environment variables only (``PAYPAL_CLIENT_ID`` /
   ``PAYPAL_CLIENT_SECRET``). Never in code, never in a committed file.
2. Every create-order call sends an idempotency key; retries reuse it so a
   double-tap cannot create two orders for one debt.
3. Never trust a client-reported amount or return URL. Re-fetch the order
   from PayPal server-side before marking anything settled.
4. Verify webhook signatures before acting on any webhook.
5. SplitLLM stores only: PayPal order/capture ids, the creditor's PayPal
   email / VPA, amounts, statuses. No PayPal passwords, no bank details,
   no UPI PINs (SplitLLM never sees a PIN at all).

Ported from the standalone TypeScript reference
(``splitllm-paypal-connector/paypal-settlement.ts``), including its embedded
QR encoder (byte mode, ECC level M, versions 1-10). The QR output is
verified byte-identical against that reference — see
``tests/test_paypal_settlements.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional
from urllib.parse import quote

import httpx
from bson import ObjectId
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.config import settings
from app.services import friend_service, fx_service

logger = logging.getLogger(__name__)

#: Mongo collection holding one document per PayPal settlement attempt.
PAYPAL_SETTLEMENTS_COLLECTION = "paypal_settlements"

#: PayPal India receipt cap (USD) — an Indian account cannot receive more
#: than this in a single payment.
INDIA_RECEIPT_CAP_USD = 10_000

#: NPCI P2P ceiling per UPI transaction (INR).
UPI_MAX_AMOUNT_INR = 100_000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SettlementValidationError(ValueError):
    """Bad input or a settlement that cannot proceed (maps to HTTP 422)."""


class SettlementConflictError(ValueError):
    """A settlement for this debt is already in flight (maps to HTTP 409)."""

    def __init__(self, message: str, settlement_id: str | None = None):
        super().__init__(message)
        self.settlement_id = settlement_id


class SettlementNotConfigured(RuntimeError):
    """PayPal credentials are missing (maps to HTTP 503)."""


class PayPalApiError(RuntimeError):
    """PayPal returned an error (maps to HTTP 502)."""

    def __init__(
        self,
        status: int,
        message: str,
        paypal_name: str | None = None,
        details: Any = None,
    ):
        super().__init__(message)
        self.status = status
        self.paypal_name = paypal_name
        self.details = details


# ---------------------------------------------------------------------------
# Money helpers (integer minor units internally, like the TS reference)
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"^\d+(\.\d{1,2})?$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_EMAIL_RE = re.compile(r"^\S+@\S+\.\S+$")


def format_money(amount: float | str) -> str:
    """Format a major-units amount as a 2-decimal string ("12.50").

    Raises :class:`SettlementValidationError` on bad input (not a number,
    not positive, or more than 2 decimal places).
    """
    raw = str(amount).strip() if isinstance(amount, str) else str(amount)
    if not _MONEY_RE.match(raw):
        raise SettlementValidationError(
            f"Amount must be a positive number with at most 2 decimal "
            f"places, got: {amount!r}"
        )
    value = Decimal(raw)
    if not value.is_finite() or value <= 0:
        raise SettlementValidationError(
            f"Amount must be greater than zero, got: {raw}"
        )
    return format(value.quantize(Decimal("0.01")), "f")


def _to_minor_units(amount: float | str) -> int:
    """Major units -> integer minor units (cents/paise), half-up rounding."""
    return int(
        (Decimal(format_money(amount)) * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _from_minor_units(minor: int) -> str:
    return format(Decimal(minor) / 100, ".2f")


def _round_half_up(value: Decimal) -> int:
    """JS Math.round semantics (half up) — Python's round() is banker's."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _normalize_country(value: str | None) -> str | None:
    """Uppercase a 2-letter ISO country code; None when missing/blank."""
    if value is None:
        return None
    cleaned = value.strip().upper()
    return cleaned or None


def _validate_country(value: str | None, *, who: str) -> str | None:
    country = _normalize_country(value)
    if country is not None and not _COUNTRY_RE.match(country):
        raise SettlementValidationError(
            f"{who} country must be a 2-letter ISO code, got: {value!r}"
        )
    return country


# ---------------------------------------------------------------------------
# PayPal client — Orders API v2, payee-override P2P settlement
# ---------------------------------------------------------------------------

_PAYPAL_BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}


@dataclass
class PayPalConfig:
    client_id: str
    client_secret: str
    environment: str = "sandbox"
    webhook_id: str | None = None
    return_url: str | None = None
    cancel_url: str | None = None


class PayPalSettlementClient:
    """Minimal async client for the PayPal calls the settlement flow needs.

    ``transport`` is an ``httpx`` transport (used by the tests with
    ``httpx.MockTransport``); pass nothing in production.
    """

    def __init__(
        self,
        config: PayPalConfig,
        *,
        transport: httpx.AsyncHTTPTransport | None = None,
        timeout: float = 30.0,
    ):
        if not config.client_id or not config.client_secret:
            raise SettlementValidationError(
                "PayPal client_id and client_secret are required — pass them "
                "from environment variables (PAYPAL_CLIENT_ID / "
                "PAYPAL_CLIENT_SECRET), never hard-code them."
            )
        if config.environment not in _PAYPAL_BASE_URLS:
            raise SettlementValidationError(
                f"PayPal environment must be 'sandbox' or 'live', "
                f"got: {config.environment!r}"
            )
        self.config = config
        self.environment = config.environment
        self.base_url = _PAYPAL_BASE_URLS[config.environment]
        self._http = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=timeout
        )
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0  # monotonic clock
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "PayPalSettlementClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def get_access_token(self) -> str:
        """OAuth2 client-credentials grant, cached until shortly before expiry."""
        now = time.monotonic()
        if self._access_token and now < self._access_token_expires_at - 60:
            return self._access_token
        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < self._token_expires_at - 60:
                return self._access_token
            basic = base64.b64encode(
                f"{self.config.client_id}:{self.config.client_secret}".encode()
            ).decode()
            resp = await self._http.post(
                "/v1/oauth2/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content="grant_type=client_credentials",
            )
            if resp.status_code >= 400:
                err = _read_error_body(resp)
                raise PayPalApiError(
                    resp.status_code,
                    f"PayPal token request failed: "
                    f"{err.get('message') or resp.text}",
                    err.get("name"),
                    err.get("details"),
                )
            data = resp.json()
            self._access_token = data["access_token"]
            self._access_token_expires_at = now + float(data.get("expires_in", 0))
            return self._access_token

    async def _api(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        idempotency_key: str | None = None,
    ) -> dict:
        token = await self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key
        resp = await self._http.request(
            method, path, headers=headers, json=json_body
        )
        if resp.status_code >= 400:
            err = _read_error_body(resp)
            raise PayPalApiError(
                resp.status_code,
                f"PayPal {method} {path} failed ({resp.status_code}): "
                f"{err.get('message') or resp.text}",
                err.get("name"),
                err.get("details"),
            )
        return resp.json()

    async def create_settlement_order(
        self,
        *,
        creditor_paypal_email: str,
        amount: float | str,
        currency: str,
        description: str | None = None,
        splitllm_debt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Create an order whose money goes to the CREDITOR (payee override).

        Returns the approval URL the debtor must open in PayPal Checkout.
        Pass the same ``idempotency_key`` on retry and PayPal returns the
        original order instead of creating a duplicate.
        """
        email = creditor_paypal_email.strip()
        if not _EMAIL_RE.match(email):
            raise SettlementValidationError(
                f"Invalid creditor PayPal email: {creditor_paypal_email!r}"
            )
        value = format_money(amount)
        code = currency.strip().upper()
        if not _CURRENCY_RE.match(code):
            raise SettlementValidationError(
                f"Currency must be a 3-letter ISO code, got: {currency!r}"
            )
        if description is not None and len(description) > 127:
            raise SettlementValidationError(
                "Description must be at most 127 characters."
            )
        if splitllm_debt_id is not None and len(splitllm_debt_id) > 256:
            raise SettlementValidationError(
                "splitllm_debt_id must be at most 256 characters."
            )

        purchase_unit: dict[str, Any] = {
            "amount": {"currency_code": code, "value": value},
            "payee": {"email_address": email},
        }
        if splitllm_debt_id:
            purchase_unit["reference_id"] = splitllm_debt_id
        if description:
            purchase_unit["description"] = description
        body: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [purchase_unit],
            "application_context": {
                "brand_name": "SplitLLM",
                "user_action": "PAY_NOW",
            },
        }
        if self.config.return_url:
            body["application_context"]["return_url"] = self.config.return_url
        if self.config.cancel_url:
            body["application_context"]["cancel_url"] = self.config.cancel_url

        key = idempotency_key or str(uuid.uuid4())
        order = await self._api("POST", "/v2/checkout/orders", body, key)
        approval_url = next(
            (link.get("href") for link in order.get("links", [])
             if link.get("rel") == "approve"),
            None,
        )
        return {
            "orderId": order["id"],
            "status": order.get("status", ""),
            "approvalUrl": approval_url,
            "amount": {"currency_code": code, "value": value},
            "payeeEmail": email,
            "raw": order,
        }

    async def get_order(self, order_id: str) -> dict:
        """Fetch the current state of an order from PayPal (server-side truth)."""
        if not order_id:
            raise SettlementValidationError("order_id is required.")
        return await self._api(
            "GET", f"/v2/checkout/orders/{_quote_path(order_id)}"
        )

    async def capture_order(self, order_id: str) -> dict:
        """Capture an approved order — this is when money actually moves."""
        result = await self._api(
            "POST",
            f"/v2/checkout/orders/{_quote_path(order_id)}/capture",
            {},
            str(uuid.uuid4()),
        )
        capture = _first_capture(result)
        if capture is None:
            raise PayPalApiError(
                502,
                f"Capture response for order {order_id} contained no capture "
                f"object.",
                None,
                result,
            )
        return {
            "captureId": capture["id"],
            "status": capture.get("status", ""),
            "amount": {
                "currency_code": capture["amount"]["currency_code"],
                "value": capture["amount"]["value"],
            },
            "orderId": result.get("id", order_id),
            "raw": result,
        }

    async def verify_capture(
        self,
        order_id: str,
        expected_amount: float | str,
        expected_currency: str,
    ) -> dict:
        """Server-side verification: re-fetch the order and check it is
        COMPLETED, a capture exists and is COMPLETED, and the captured
        amount/currency match the debt exactly.

        Only a ``verified: True`` result may mark a SplitLLM debt SETTLED.
        Never trust the client-side redirect.
        """
        expected_value = format_money(expected_amount)
        expected_code = expected_currency.strip().upper()
        order = await self.get_order(order_id)
        capture = _first_capture(order)
        amount_matches = (
            capture is not None
            and capture.get("amount", {}).get("value") == expected_value
            and capture.get("amount", {}).get("currency_code") == expected_code
        )
        order_status = order.get("status", "")
        capture_status = capture.get("status") if capture else None
        verified = (
            order_status == "COMPLETED"
            and capture_status == "COMPLETED"
            and amount_matches
        )
        if verified:
            reason = ("Order COMPLETED, capture COMPLETED, amount and "
                      "currency match.")
        elif order_status != "COMPLETED":
            reason = f"Order status is {order_status}, not COMPLETED."
        elif capture is None:
            reason = ("Order has no capture yet — payer may not have approved, "
                      "or capture was not called.")
        elif capture_status != "COMPLETED":
            reason = f"Capture status is {capture_status}, not COMPLETED."
        else:
            reason = (
                f"Captured amount {capture['amount'].get('value')} "
                f"{capture['amount'].get('currency_code')} does not match "
                f"expected {expected_value} {expected_code}."
            )
        return {
            "verified": verified,
            "orderId": order.get("id", order_id),
            "orderStatus": order_status,
            "captureId": capture.get("id") if capture else None,
            "captureStatus": capture_status,
            "amountMatches": amount_matches,
            "reason": reason,
        }

    async def verify_webhook_signature(
        self,
        *,
        transmission_id: str,
        transmission_time: str,
        cert_url: str,
        auth_algo: str,
        transmission_sig: str,
        webhook_id: str,
        raw_body: str,
    ) -> bool:
        """Verify a PayPal webhook delivery (production-grade confirmation).

        POSTs the five paypal-* headers, the webhook id and the event back
        to PayPal; only ``verification_status == "SUCCESS"`` is genuine.
        Pass ``raw_body`` exactly as received — re-serializing parsed JSON
        can change bytes and turn a genuine event into FAILURE.
        """
        try:
            event = json.loads(raw_body)
        except (ValueError, TypeError):
            raise SettlementValidationError(
                "Webhook raw_body is not valid JSON."
            )
        result = await self._api(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            {
                "auth_algo": auth_algo,
                "cert_url": cert_url,
                "transmission_id": transmission_id,
                "transmission_sig": transmission_sig,
                "transmission_time": transmission_time,
                "webhook_id": webhook_id,
                "webhook_event": event,
            },
        )
        return result.get("verification_status") == "SUCCESS"


def _read_error_body(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {"message": resp.text}
    except ValueError:
        return {"message": resp.text}


def _first_capture(order: dict) -> dict | None:
    try:
        captures = order["purchase_units"][0]["payments"]["captures"]
    except (KeyError, IndexError, TypeError):
        return None
    return captures[0] if captures else None


def _quote_path(value: str) -> str:
    return quote(value, safe="")


# ---------------------------------------------------------------------------
# Fee estimate — show BEFORE the debtor confirms
# ---------------------------------------------------------------------------


def estimate_receiver_fee(
    amount: float | str,
    currency: str,
    *,
    cross_border: bool,
) -> dict:
    """Estimate what the creditor actually receives.

    US domestic: PayPal's published 3.49% + $0.49. Cross-border into India:
    4.4% + $0.30 visible fee, plus a ~3-4% FX markup inside the exchange
    rate and 18% GST on the fee (all-in roughly 7.5-9% per third-party
    breakdowns, Sep-Oct 2026). The FX markup moves with the rate, so
    cross-border numbers are estimates only — PayPal shows the exact fee
    at checkout.
    """
    gross_minor = _to_minor_units(amount)
    code = currency.strip().upper()
    if not cross_border:
        fee_minor = _round_half_up(Decimal(gross_minor) * Decimal("0.0349")) + 49
        rate_note = (
            "US domestic PayPal Checkout rate: 3.49% + $0.49. Fee is charged "
            "to the receiver (the creditor)."
        )
    else:
        fee_minor = _round_half_up(Decimal(gross_minor) * Decimal("0.044")) + 30
        rate_note = (
            "Cross-border estimate: 4.4% + $0.30 visible fee. On top of that "
            "PayPal applies a ~3–4% currency-conversion markup inside the "
            "exchange rate and 18% GST on the fee, so the all-in cost is "
            "roughly 7.5–9% — the exact figure is shown by PayPal at checkout."
        )
    net_minor = max(0, gross_minor - fee_minor)
    return {
        "gross": _from_minor_units(gross_minor),
        "fee": _from_minor_units(fee_minor),
        "netToCreditor": _from_minor_units(net_minor),
        "currency": code,
        "crossBorder": cross_border,
        "rateNote": rate_note,
    }


# ---------------------------------------------------------------------------
# Rail routing — PayPal vs UPI vs unavailable (pure function, no API calls)
# ---------------------------------------------------------------------------


def route_settlement(
    *,
    payer_country: str | None,
    payee_country: str | None,
    amount: float | str | None = None,
    currency: str | None = None,
) -> dict:
    """Decide which rail can settle a debt, from the payer's and payee's
    countries (ISO alpha-2, e.g. "US", "IN").

    PayPal India is cross-border ONLY since Apr 2021: an Indian account can
    RECEIVE from abroad but cannot SEND abroad, and there is no domestic
    PayPal in India. So India->India is UPI, India->abroad is unsupported,
    and anything into India is PayPal (USD, auto-converted to INR).
    """
    payer = _normalize_country(payer_country)
    payee = _normalize_country(payee_country)
    if (
        payer is None
        or payee is None
        or not _COUNTRY_RE.match(payer)
        or not _COUNTRY_RE.match(payee)
    ):
        return {
            "rail": "unavailable",
            "reason": (
                "Payer and payee countries are required (ISO alpha-2) to "
                "pick a settlement rail."
            ),
            "currency": None,
        }
    if payer == "IN" and payee == "IN":
        return {
            "rail": "upi",
            "reason": (
                "Both people are in India — PayPal has no domestic payments "
                "in India, so settle over UPI (free)."
            ),
            "currency": "INR",
        }
    if payer == "IN":
        return {
            "rail": "unavailable",
            "reason": (
                "Indian PayPal accounts cannot send money abroad, and UPI is "
                "domestic-only — there is no supported rail for an "
                "India-based payer settling with someone abroad yet."
            ),
            "currency": None,
        }
    if payee == "IN":
        if (
            amount is not None
            and (currency or "USD").strip().upper() == "USD"
            and Decimal(format_money(amount)) > INDIA_RECEIPT_CAP_USD
        ):
            return {
                "rail": "unavailable",
                "reason": (
                    "Amount exceeds PayPal India's $10,000 per-receipt cap "
                    "— split it into smaller settlements."
                ),
                "currency": None,
            }
        return {
            "rail": "paypal",
            "reason": (
                "Cross-border PayPal: the Indian creditor receives in USD, "
                "PayPal auto-converts to INR and auto-withdraws to their "
                "bank. Show the fee estimate before confirming."
            ),
            "currency": "USD",
        }
    return {
        "rail": "paypal",
        "reason": "PayPal settlement between two non-India accounts.",
        "currency": (currency or "USD").strip().upper(),
    }


# ---------------------------------------------------------------------------
# UPI rail — intent deep link + QR (India-domestic, P2P)
# ---------------------------------------------------------------------------

UPI_VPA_RE = re.compile(r"^[a-z0-9._-]{1,256}@[a-z0-9]{2,64}$")


def normalize_vpa(vpa: str) -> str:
    """Lowercase + format-check a VPA (UPI ID). Raises on invalid input."""
    cleaned = vpa.strip().lower()
    if not UPI_VPA_RE.match(cleaned):
        raise SettlementValidationError(
            f"Invalid UPI ID (VPA): {vpa!r} — expected format like "
            f'"name@okhdfcbank".'
        )
    return cleaned


def format_inr_amount(amount_inr: float | str) -> str:
    """Format an INR amount as a 2-decimal string.

    Enforces the ₹1,00,000 P2P ceiling.
    """
    value = format_money(amount_inr)
    if Decimal(value) > UPI_MAX_AMOUNT_INR:
        raise SettlementValidationError(
            f"UPI amount ₹{value} exceeds the ₹1,00,000 per-transaction P2P "
            f"ceiling — split it into smaller payments."
        )
    return value


def build_upi_intent_link(
    *,
    vpa: str,
    name: str,
    amount_inr: float | str,
    note: str | None = None,
    reference: str | None = None,
) -> str:
    """Build a UPI intent deep link per NPCI's UPI Linking Specification.

    Parameter order pa, pn, am, cu, tn, tr. Values are percent-encoded with
    %20 for spaces (never "+"). ``mc`` (merchant code) is deliberately
    omitted: this is a personal P2P payment, not a merchant payment — no
    business registration is needed to receive on a personal VPA.

    Tapping the link opens the debtor's own UPI app (GPay / PhonePe / BHIM)
    with the payment prefilled; the debtor approves with their UPI PIN.
    There is NO callback or webhook — SplitLLM cannot verify a UPI payment
    automatically. Record UPI settlements as PENDING_CONFIRMATION until a
    human confirms.
    """
    pa = normalize_vpa(vpa)
    am = format_inr_amount(amount_inr)
    payee_name = name.strip()
    if not payee_name:
        raise SettlementValidationError("UPI payee name is required.")
    if len(payee_name) > 99:
        raise SettlementValidationError(
            "UPI payee name must be at most 99 characters."
        )
    if note is not None and len(note) > 80:
        raise SettlementValidationError(
            "UPI transaction note must be at most 80 characters."
        )
    if reference is not None and len(reference) > 35:
        raise SettlementValidationError(
            "UPI transaction reference must be at most 35 characters."
        )
    # Match JS encodeURIComponent: leave - _ . ! ~ * ' ( ) unescaped.
    enc = lambda s: quote(s, safe="-_.!~*'()")
    link = f"upi://pay?pa={pa}&pn={enc(payee_name)}&am={am}&cu=INR"
    if note:
        link += f"&tn={enc(note)}"
    if reference:
        link += f"&tr={enc(reference)}"
    return link


# ---------------------------------------------------------------------------
# Embedded QR encoder (byte mode, error-correction level M, versions 1-10)
# ---------------------------------------------------------------------------
#
# Zero-dependency on purpose: the UPI QR is generated locally from the
# intent link, no third-party service ever sees the payment details.
# Implementation follows the QR spec (ISO/IEC 18004) — byte mode, ECC level
# M, automatic version selection, mask chosen by the standard penalty rules.
# Ported from the TypeScript reference; the test suite checks the output is
# identical to that reference (module-matrix hashes) for fixed inputs.


@dataclass
class _QrVersionInfo:
    total_codewords: int
    ecc_per_block: int
    # Groups of blocks: shorter blocks first.
    groups: list[dict]
    alignment: list[int]


_QR_VERSIONS: list[_QrVersionInfo] = [
    _QrVersionInfo(26, 10, [{"count": 1, "data_codewords": 16}], []),
    _QrVersionInfo(44, 16, [{"count": 1, "data_codewords": 28}], [6, 18]),
    _QrVersionInfo(70, 26, [{"count": 1, "data_codewords": 44}], [6, 22]),
    _QrVersionInfo(100, 18, [{"count": 2, "data_codewords": 32}], [6, 26]),
    _QrVersionInfo(134, 24, [{"count": 2, "data_codewords": 43}], [6, 30]),
    _QrVersionInfo(
        172, 16,
        [{"count": 2, "data_codewords": 27}, {"count": 2, "data_codewords": 27}],
        [6, 34],
    ),
    _QrVersionInfo(
        196, 18,
        [{"count": 2, "data_codewords": 31}, {"count": 2, "data_codewords": 31}],
        [6, 22, 38],
    ),
    _QrVersionInfo(
        242, 22,
        [{"count": 2, "data_codewords": 38}, {"count": 2, "data_codewords": 39}],
        [6, 24, 42],
    ),
    _QrVersionInfo(
        292, 22,
        [{"count": 3, "data_codewords": 36}, {"count": 2, "data_codewords": 37}],
        [6, 26, 46],
    ),
    _QrVersionInfo(
        346, 26,
        [{"count": 4, "data_codewords": 43}, {"count": 1, "data_codewords": 44}],
        [6, 28, 50],
    ),
]

# Galois field GF(256) with primitive polynomial 0x11D.
_GF_EXP: list[int] = [0] * 512
_GF_LOG: list[int] = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]
del _x, _i


def _gf_mul(x: int, y: int) -> int:
    return 0 if x == 0 or y == 0 else _GF_EXP[_GF_LOG[x] + _GF_LOG[y]]


def _reed_solomon_divisor(degree: int) -> list[int]:
    result = [0] * degree
    result[degree - 1] = 1
    root = 1
    for _ in range(degree):
        for j in range(len(result)):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _reed_solomon_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


class _BitBuffer:
    def __init__(self) -> None:
        self.bits: list[bool] = []

    def __len__(self) -> int:
        return len(self.bits)

    def push(self, value: int, count: int) -> None:
        for i in range(count - 1, -1, -1):
            self.bits.append(((value >> i) & 1) != 0)

    def to_bytes(self) -> list[int]:
        out: list[int] = []
        for i in range(0, len(self.bits), 8):
            byte = 0
            for j in range(8):
                byte = (byte << 1) | (1 if self.bits[i + j] else 0)
            out.append(byte)
        return out


def _get_bit(x: int, i: int) -> bool:
    return ((x >> i) & 1) != 0


def _get_format_bits(mask: int) -> int:
    # ECC level M = 0b00. BCH(15,5) with generator 0x537, XOR mask 0x5412.
    data = mask  # (0b00 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _get_version_bits(version: int) -> int:
    # BCH(18,6) with generator 0x1F25.
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (version << 12) | rem


def _mask_0(x: int, y: int) -> bool:
    return (y + x) % 2 == 0


def _mask_1(x: int, y: int) -> bool:
    return y % 2 == 0


def _mask_2(x: int, y: int) -> bool:
    return x % 3 == 0


def _mask_3(x: int, y: int) -> bool:
    return (y + x) % 3 == 0


def _mask_4(x: int, y: int) -> bool:
    return (y // 2 + x // 3) % 2 == 0


def _mask_5(x: int, y: int) -> bool:
    return ((y * x) % 2) + ((y * x) % 3) == 0


def _mask_6(x: int, y: int) -> bool:
    return (((y * x) % 2) + ((y * x) % 3)) % 2 == 0


def _mask_7(x: int, y: int) -> bool:
    return (((y + x) % 2) + ((y * x) % 3)) % 2 == 0


_MASK_FUNCTIONS = [
    _mask_0, _mask_1, _mask_2, _mask_3, _mask_4, _mask_5, _mask_6, _mask_7,
]


def _qr_penalty(modules: list[list[bool]]) -> int:
    """QR penalty score (lower is better) — the four standard rules."""
    size = len(modules)
    score = 0
    # Rule 1: runs of 5+ same-colour modules in rows and columns.
    for y in range(size):
        run_color = modules[y][0]
        run_len = 1
        for x in range(1, size + 1):
            color = modules[y][x] if x < size else not run_color
            if color == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += run_len - 2
                run_color = color
                run_len = 1
    for x in range(size):
        run_color = modules[0][x]
        run_len = 1
        for y in range(1, size + 1):
            color = modules[y][x] if y < size else not run_color
            if color == run_color:
                run_len += 1
            else:
                if run_len >= 5:
                    score += run_len - 2
                run_color = color
                run_len = 1
    # Rule 2: 2x2 blocks of the same colour.
    for y in range(size - 1):
        for x in range(size - 1):
            c = modules[y][x]
            if (
                c == modules[y][x + 1]
                and c == modules[y + 1][x]
                and c == modules[y + 1][x + 1]
            ):
                score += 3
    # Rule 3: finder-like patterns (1011101 with 4 light modules on either side).
    def has_finder_pattern(line: list[bool]) -> bool:
        a = [False, False, False, False, True, False, True, True, True, False, True]
        b = [True, False, True, True, True, False, True, False, False, False, False]
        for i in range(len(line) - 11 + 1):
            window = line[i:i + 11]
            if window == a or window == b:
                return True
        return False

    for y in range(size):
        if has_finder_pattern(modules[y]):
            score += 40
    for x in range(size):
        if has_finder_pattern([row[x] for row in modules]):
            score += 40
    # Rule 4: proportion of dark modules.
    dark = sum(1 for row in modules for c in row if c)
    total = size * size
    score += int(abs((dark / total) * 100 - 50) / 5) * 10
    return score


def upi_qr_modules(text: str) -> list[list[bool]]:
    """Encode text as a QR module matrix (byte mode, ECC level M).

    Returns a size x size grid of booleans (True = dark module).
    Raises if the text does not fit in version 10 at level M (~213 bytes).
    """
    data_bytes = list(text.encode("utf-8"))
    # Pick the smallest version that fits.
    version = -1
    info: _QrVersionInfo | None = None
    for v in range(1, 11):
        candidate = _QR_VERSIONS[v - 1]
        data_codewords = sum(
            g["count"] * g["data_codewords"] for g in candidate.groups
        )
        count_bits = 8 if v <= 9 else 16
        if 4 + count_bits + len(data_bytes) * 8 <= data_codewords * 8:
            version = v
            info = candidate
            break
    if version == -1 or info is None:
        raise SettlementValidationError(
            f"Text too long for a version-10 QR code at error-correction "
            f"level M ({len(data_bytes)} bytes)."
        )
    data_codewords = sum(g["count"] * g["data_codewords"] for g in info.groups)

    # Data codewords: mode + count + bytes + terminator + byte pad + EC/11 pad.
    bits = _BitBuffer()
    bits.push(0b0100, 4)
    bits.push(len(data_bytes), 8 if version <= 9 else 16)
    for b in data_bytes:
        bits.push(b, 8)
    capacity_bits = data_codewords * 8
    bits.push(0, min(4, capacity_bits - len(bits)))
    while len(bits) % 8 != 0:
        bits.push(0, 1)
    pad_toggle = 0
    while len(bits) < capacity_bits:
        bits.push(0xEC if pad_toggle % 2 == 0 else 0x11, 8)
        pad_toggle += 1
    data = bits.to_bytes()

    # Split into blocks, compute ECC, interleave.
    divisor = _reed_solomon_divisor(info.ecc_per_block)
    blocks: list[list[int]] = []
    offset = 0
    for group in info.groups:
        for _ in range(group["count"]):
            blocks.append(data[offset:offset + group["data_codewords"]])
            offset += group["data_codewords"]
    ecc_blocks = [_reed_solomon_remainder(b, divisor) for b in blocks]
    max_data_len = max(len(b) for b in blocks)
    final_codewords: list[int] = []
    for i in range(max_data_len):
        for b in blocks:
            if i < len(b):
                final_codewords.append(b[i])
    for i in range(info.ecc_per_block):
        for b in ecc_blocks:
            final_codewords.append(b[i])

    # Matrix construction.
    size = version * 4 + 17
    modules: list[list[bool]] = [[False] * size for _ in range(size)]
    is_function: list[list[bool]] = [[False] * size for _ in range(size)]

    def set_function(x: int, y: int, value: bool) -> None:
        modules[y][x] = value
        is_function[y][x] = True

    # Timing patterns (drawn first; finders overwrite the overlap).
    for i in range(size):
        set_function(6, i, i % 2 == 0)
        set_function(i, 6, i % 2 == 0)

    # Finder patterns + separators (9x9 regions centred on the three corners).
    def draw_finder(cx: int, cy: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if x < 0 or x >= size or y < 0 or y >= size:
                    continue
                dist = max(abs(dx), abs(dy))
                set_function(x, y, dist != 2 and dist != 4)

    draw_finder(3, 3)
    draw_finder(size - 4, 3)
    draw_finder(3, size - 4)

    # Alignment patterns. The three pairs that touch a finder corner are
    # skipped: (first, first), (first, last) and (last, first) — drawing
    # them would overwrite the finders and steal data cells.
    align_count = len(info.alignment)
    for iy in range(align_count):
        for ix in range(align_count):
            if (
                (iy == 0 and ix == 0)
                or (iy == 0 and ix == align_count - 1)
                or (iy == align_count - 1 and ix == 0)
            ):
                continue
            ax, ay = info.alignment[ix], info.alignment[iy]
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_function(
                        ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1
                    )

    # Reserve format-info and version-info cells (values filled in later).
    for i in range(6):
        set_function(8, i, False)
    set_function(8, 7, False)
    set_function(8, 8, False)
    set_function(7, 8, False)
    for i in range(9, 15):
        set_function(14 - i, 8, False)
    for i in range(8):
        set_function(size - 1 - i, 8, False)
    for i in range(8, 15):
        set_function(8, size - 15 + i, False)
    set_function(size - 8, 8, True)  # dark module
    if version >= 7:
        for i in range(18):
            a = size - 11 + (i % 3)
            b = i // 3
            set_function(a, b, False)
            set_function(b, a, False)

    # Data placement (zigzag, skipping column 6).
    bit_index = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if (
                    not is_function[y][x]
                    and bit_index < len(final_codewords) * 8
                ):
                    modules[y][x] = _get_bit(
                        final_codewords[bit_index >> 3], 7 - (bit_index & 7)
                    )
                    bit_index += 1
        right -= 2
    if bit_index != len(final_codewords) * 8:
        raise RuntimeError(
            "QR encoder internal error: data placement did not fill the matrix."
        )

    # Try every mask; keep the one with the lowest penalty.
    best_mask = 0
    best_score = float("inf")
    best_modules: list[list[bool]] = modules
    for mask in range(8):
        trial = [row[:] for row in modules]
        fn = _MASK_FUNCTIONS[mask]
        for y in range(size):
            for x in range(size):
                if not is_function[y][x] and fn(x, y):
                    trial[y][x] = not trial[y][x]
        score = _qr_penalty(trial)
        if score < best_score:
            best_score = score
            best_mask = mask
            best_modules = trial

    # Write format info (with the chosen mask) and version info.
    # NOTE: modules is indexed [row][col] = [y][x]. Keep the coordinates
    # in (x, y) order straight.
    bits15 = _get_format_bits(best_mask)
    for i in range(6):
        best_modules[i][8] = _get_bit(bits15, i)
    best_modules[7][8] = _get_bit(bits15, 6)
    best_modules[8][8] = _get_bit(bits15, 7)
    best_modules[8][7] = _get_bit(bits15, 8)
    for i in range(9, 15):
        best_modules[8][14 - i] = _get_bit(bits15, i)
    for i in range(8):
        best_modules[8][size - 1 - i] = _get_bit(bits15, i)
    for i in range(8, 15):
        best_modules[size - 15 + i][8] = _get_bit(bits15, i)
    best_modules[8][size - 8] = True
    if version >= 7:
        bits18 = _get_version_bits(version)
        for i in range(18):
            bit = _get_bit(bits18, i)
            a = size - 11 + (i % 3)
            b = i // 3
            best_modules[b][a] = bit
            best_modules[a][b] = bit
    return best_modules


def build_upi_qr_svg(
    intent_url: str,
    *,
    scale: int = 8,
    margin: int = 4,
    dark: str = "#000000",
    light: str = "#ffffff",
) -> str:
    """Render a UPI intent link as a QR code SVG (black on white, quiet
    zone included). Render it inline in the SplitLLM app — the payer scans
    it with their UPI app's scanner. No external service involved."""
    modules = upi_qr_modules(intent_url)
    size = len(modules)
    total = (size + margin * 2) * scale
    path = ""
    for y in range(size):
        for x in range(size):
            if modules[y][x]:
                px = (x + margin) * scale
                py = (y + margin) * scale
                path += f"M{px} {py}h{scale}v{scale}H{px}z"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" '
        f'width="{total}" height="{total}" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="{light}"/>'
        f'<path d="{path}" fill="{dark}"/></svg>'
    )


# ---------------------------------------------------------------------------
# Backend wiring — start / capture / verify / webhook
# ---------------------------------------------------------------------------
#
# These functions are the only ones that touch MongoDB. They reuse the
# existing settlement flow: a PayPal capture that verifies becomes a
# `settlement_request` that is immediately finalized through
# ``friend_service._finalize_settlement`` (same balance-clearing, audit and
# emails as a creditor confirming a manual claim). A UPI settlement IS a
# pending `settlement_request` (rail "upi") that the creditor confirms with
# the existing POST /db/settlements/{id}/confirm endpoint — there is no
# automatic UPI verification.

_PAYPAL_CLIENT: PayPalSettlementClient | None = None


def _build_paypal_client(
    *, transport: httpx.AsyncHTTPTransport | None = None
) -> PayPalSettlementClient:
    if not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET:
        raise SettlementNotConfigured(
            "PayPal is not configured. Set PAYPAL_CLIENT_ID and "
            "PAYPAL_CLIENT_SECRET (sandbox first) in the environment."
        )
    return PayPalSettlementClient(
        PayPalConfig(
            client_id=settings.PAYPAL_CLIENT_ID,
            client_secret=settings.PAYPAL_CLIENT_SECRET,
            environment=settings.PAYPAL_ENVIRONMENT or "sandbox",
            webhook_id=settings.PAYPAL_WEBHOOK_ID or None,
            return_url=settings.PAYPAL_RETURN_URL or None,
            cancel_url=settings.PAYPAL_CANCEL_URL or None,
        ),
        transport=transport,
    )


def get_paypal_client(
    *, transport: httpx.AsyncHTTPTransport | None = None
) -> PayPalSettlementClient:
    """The shared PayPal client (one per process; the OAuth token is cached
    on it). Pass ``transport`` only from tests — it builds a throwaway client
    instead of the shared one."""
    global _PAYPAL_CLIENT
    if transport is not None:
        return _build_paypal_client(transport=transport)
    if _PAYPAL_CLIENT is None:
        _PAYPAL_CLIENT = _build_paypal_client()
    return _PAYPAL_CLIENT


def reset_paypal_client() -> None:
    """Drop the shared client (tests only)."""
    global _PAYPAL_CLIENT
    _PAYPAL_CLIENT = None


async def _load_parties(
    db: AsyncIOMotorDatabase, payer_id: str, friend_id: str
) -> tuple[dict, dict]:
    """Load payer + payee docs and enforce that they are friends."""
    try:
        friend_oid = ObjectId(friend_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Friend not found")
    payer = await db.users.find_one({"_id": ObjectId(payer_id)})
    payee = await db.users.find_one({"_id": friend_oid})
    if not payer or not payee:
        raise HTTPException(status_code=404, detail="Friend not found")
    if friend_oid not in (payer.get("friends") or []):
        raise SettlementValidationError(
            "You can only settle up with a friend."
        )
    return payer, payee


async def _require_caller_owes(
    db: AsyncIOMotorDatabase, payer_id: str, friend_id: str
) -> dict:
    """The dues between the caller and the friend, requiring the caller to
    be the one who owes (a PayPal/UPI payment only flows debtor -> creditor).
    """
    dues = await friend_service.get_friend_dues(db, payer_id, friend_id)
    amount = float(dues.get("oweAmount") or 0)
    if amount <= 0.01:
        raise SettlementValidationError("Nothing to settle up.")
    if dues.get("whoOwes") != "user":
        raise SettlementValidationError(
            "Your friend owes you — ask them to settle up with you."
        )
    return dues


async def _pending_request_for_pair(
    db: AsyncIOMotorDatabase, payer_id: str, payee_id: str
) -> dict | None:
    return await db.settlement_request.find_one(
        {
            "payerId": ObjectId(payer_id),
            "payeeId": ObjectId(payee_id),
            "status": "pending",
        }
    )


def _resolve_countries(
    payer: dict,
    payee: dict,
    *,
    payer_country: str | None,
    payee_country: str | None,
) -> tuple[str | None, str | None]:
    """Profile countries, with explicit request-body overrides winning.

    Countries are optional profile fields (see the open questions in the PR
    description) — when neither the profile nor the request supplies one,
    routing returns "unavailable" instead of guessing.
    """
    return (
        _validate_country(
            payer_country if payer_country is not None else payer.get("country"),
            who="Payer",
        ),
        _validate_country(
            payee_country if payee_country is not None else payee.get("country"),
            who="Payee",
        ),
    )


async def _resolve_paypal_amount(
    db: AsyncIOMotorDatabase,
    payer_id: str,
    dues_amount: float,
    dues_currency: str,
) -> tuple[str, str, float | None]:
    """Amount + currency to put on the PayPal order.

    PayPal orders are denominated in USD for the India corridor (the Indian
    receiver's conversion is done by PayPal). An INR debt is converted only
    when the payer has an *explicit* INR->USD rate on their account — an
    FX rate is never guessed for money movement. Other currencies are used
    as-is; PayPal rejects unsupported ones with a clear API error.
    """
    code = (dues_currency or "").strip().upper()
    if code == "USD":
        return format_money(dues_amount), "USD", None
    if code == "INR":
        table, table_currency = await fx_service.user_rate_table(db, payer_id)
        rate = fx_service.override_rate(table, table_currency, "INR", "USD")
        if rate is None:
            raise SettlementValidationError(
                "This debt is in INR and PayPal settles in USD. Set an "
                "INR→USD rate on your account (Profile → FX rates) and "
                "retry, or settle in INR over UPI."
            )
        converted = (
            Decimal(format_money(dues_amount)) * Decimal(str(rate))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return format(converted, "f"), "USD", float(rate)
    return format_money(dues_amount), code, None


async def start_paypal_settlement(
    db: AsyncIOMotorDatabase,
    payer_id: str,
    friend_id: str,
    *,
    note: str | None = None,
    creditor_paypal_email: str | None = None,
    payer_country: str | None = None,
    payee_country: str | None = None,
    client: PayPalSettlementClient | None = None,
) -> dict:
    """Start a PayPal settlement for the caller's full outstanding debt to
    a friend. Returns the approval URL the debtor must open in PayPal
    Checkout. No money moves until capture + server-side verification.
    """
    payer, payee = await _load_parties(db, payer_id, friend_id)
    dues = await _require_caller_owes(db, payer_id, friend_id)
    payer_cty, payee_cty = _resolve_countries(
        payer, payee, payer_country=payer_country, payee_country=payee_country
    )
    route = route_settlement(
        payer_country=payer_cty,
        payee_country=payee_cty,
        amount=dues.get("oweAmount"),
        currency=dues.get("currency"),
    )
    if route["rail"] != "paypal":
        raise SettlementValidationError(route["reason"])

    existing = await _pending_request_for_pair(db, payer_id, friend_id)
    if existing:
        raise SettlementConflictError(
            "A settle-up is already waiting to be confirmed.",
            str(existing["_id"]),
        )

    paypal_amount, paypal_currency, fx_rate = await _resolve_paypal_amount(
        db, payer_id, float(dues["oweAmount"]), dues.get("currency") or ""
    )
    # Re-check the India receipt cap against the settled currency/amount.
    route = route_settlement(
        payer_country=payer_cty,
        payee_country=payee_cty,
        amount=paypal_amount,
        currency=paypal_currency,
    )
    if route["rail"] != "paypal":
        raise SettlementValidationError(route["reason"])

    email = (creditor_paypal_email or payee.get("paypalEmail") or "").strip()
    if not email or not _EMAIL_RE.match(email):
        raise SettlementValidationError(
            "The creditor has not saved a PayPal email yet — ask them to add "
            "one on their profile before settling over PayPal."
        )

    # Reuse an in-flight order for the same pair + amount (double-tap safe).
    attempt = await db[PAYPAL_SETTLEMENTS_COLLECTION].find_one(
        {
            "payerId": ObjectId(payer_id),
            "payeeId": ObjectId(friend_id),
            "amount": paypal_amount,
            "currency": paypal_currency,
            "status": "CREATED",
        }
    )
    if attempt:
        return _paypal_attempt_response(attempt)

    paypal = client or get_paypal_client()
    now = datetime.now(timezone.utc)
    description = (
        f"SplitLLM settlement — {note}" if note else "SplitLLM settlement"
    )[:127]
    attempt_doc: dict[str, Any] = {
        "payerId": ObjectId(payer_id),
        "payeeId": ObjectId(friend_id),
        "amount": paypal_amount,
        "currency": paypal_currency,
        "duesAmount": format_money(dues["oweAmount"]),
        "duesCurrency": (dues.get("currency") or "").strip().upper(),
        "fxRate": fx_rate,
        "orderId": None,
        "approvalUrl": None,
        "captureId": None,
        "feeEstimate": None,
        "status": "CREATING",
        "verifyReason": None,
        "settlementRequestId": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db[PAYPAL_SETTLEMENTS_COLLECTION].insert_one(attempt_doc)
    attempt_id = result.inserted_id
    try:
        order = await paypal.create_settlement_order(
            creditor_paypal_email=email,
            amount=paypal_amount,
            currency=paypal_currency,
            description=description,
            splitllm_debt_id=str(attempt_id),
            idempotency_key=f"splitllm-{attempt_id}",
        )
    except PayPalApiError:
        await db[PAYPAL_SETTLEMENTS_COLLECTION].update_one(
            {"_id": attempt_id},
            {"$set": {"status": "FAILED", "updatedAt": datetime.now(timezone.utc)}},
        )
        raise

    fee = estimate_receiver_fee(
        paypal_amount,
        paypal_currency,
        cross_border=(payer_cty != payee_cty),
    )
    await db[PAYPAL_SETTLEMENTS_COLLECTION].update_one(
        {"_id": attempt_id},
        {
            "$set": {
                "orderId": order["orderId"],
                "approvalUrl": order["approvalUrl"],
                "feeEstimate": fee,
                "status": "CREATED",
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    attempt_doc.update(
        {
            "_id": attempt_id,
            "orderId": order["orderId"],
            "approvalUrl": order["approvalUrl"],
            "feeEstimate": fee,
            "status": "CREATED",
        }
    )
    return _paypal_attempt_response(attempt_doc)


def _paypal_attempt_response(attempt: dict) -> dict:
    return {
        "rail": "paypal",
        "settlementAttemptId": str(attempt["_id"]),
        "orderId": attempt.get("orderId"),
        "approvalUrl": attempt.get("approvalUrl"),
        "amount": attempt["amount"],
        "currency": attempt["currency"],
        "feeEstimate": attempt.get("feeEstimate"),
    }


async def start_upi_settlement(
    db: AsyncIOMotorDatabase,
    payer_id: str,
    friend_id: str,
    *,
    note: str | None = None,
    creditor_vpa: str | None = None,
    payer_country: str | None = None,
    payee_country: str | None = None,
) -> dict:
    """Start a UPI settlement: build the intent link + QR and record a
    pending settlement_request the creditor confirms with the existing
    confirm endpoint. No money moves inside SplitLLM — the payment happens
    in the debtor's own UPI app.
    """
    payer, payee = await _load_parties(db, payer_id, friend_id)
    dues = await _require_caller_owes(db, payer_id, friend_id)
    payer_cty, payee_cty = _resolve_countries(
        payer, payee, payer_country=payer_country, payee_country=payee_country
    )
    route = route_settlement(
        payer_country=payer_cty,
        payee_country=payee_cty,
        amount=dues.get("oweAmount"),
        currency=dues.get("currency"),
    )
    if route["rail"] != "upi":
        raise SettlementValidationError(route["reason"])
    if (dues.get("currency") or "").strip().upper() != "INR":
        raise SettlementValidationError(
            "UPI settlements must be in INR — this debt is in "
            f"{(dues.get('currency') or '').strip().upper() or 'an unknown currency'}."
        )

    vpa_raw = (creditor_vpa or payee.get("upiId") or "").strip()
    if not vpa_raw:
        raise SettlementValidationError(
            "The creditor has not saved a UPI ID yet — ask them to add one "
            "on their profile before settling over UPI."
        )

    existing = await _pending_request_for_pair(db, payer_id, friend_id)
    if existing:
        raise SettlementConflictError(
            "A settle-up is already waiting to be confirmed.",
            str(existing["_id"]),
        )

    link = build_upi_intent_link(
        vpa=vpa_raw,
        name=payee.get("name") or "SplitLLM User",
        amount_inr=dues["oweAmount"],
        note=note,
    )
    qr_svg = build_upi_qr_svg(link)
    now = datetime.now(timezone.utc)
    request_doc = {
        "payerId": ObjectId(payer_id),
        "payeeId": ObjectId(friend_id),
        "requestedBy": ObjectId(payer_id),
        "amount": round(float(dues["oweAmount"]), 2),
        "currency": "INR",
        "status": "pending",
        "rail": "upi",
        "upiLink": link,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.settlement_request.insert_one(request_doc)
    return {
        "rail": "upi",
        "settlementId": str(result.inserted_id),
        "intentLink": link,
        "qrSvg": qr_svg,
        "amount": format_money(dues["oweAmount"]),
        "currency": "INR",
    }


async def _load_paypal_attempt_for_user(
    db: AsyncIOMotorDatabase, order_id: str, user_id: str
) -> dict:
    """Load a PayPal attempt, but only for one of its two parties — anyone
    else gets the same answer as a nonexistent id, so this cannot be used
    to probe other people's settlements."""
    doc = await db[PAYPAL_SETTLEMENTS_COLLECTION].find_one({"orderId": order_id})
    if doc is None or str(user_id) not in (
        str(doc["payerId"]),
        str(doc["payeeId"]),
    ):
        raise HTTPException(status_code=404, detail="Settlement not found")
    return doc


async def _verify_and_finalize(
    db: AsyncIOMotorDatabase,
    attempt: dict,
    client: PayPalSettlementClient,
) -> dict:
    """Verify a capture server-side and, only if it checks out, record the
    settlement through the existing flow. Returns the verification plus
    whether the debt was settled."""
    verification = await client.verify_capture(
        attempt["orderId"], attempt["amount"], attempt["currency"]
    )
    if not verification["verified"]:
        if verification["captureId"] is not None:
            await db[PAYPAL_SETTLEMENTS_COLLECTION].update_one(
                {"_id": attempt["_id"]},
                {
                    "$set": {
                        "status": "CAPTURED",
                        "captureId": verification["captureId"],
                        "verifyReason": verification["reason"],
                        "updatedAt": datetime.now(timezone.utc),
                    }
                },
            )
        return {**verification, "settled": False, "settlementId": None}

    # The money moved for the amount recorded at start. If the balance
    # changed since (new expenses), do NOT clear the whole debt — flag it
    # for a human instead of settling the wrong amount.
    payer_id, payee_id = str(attempt["payerId"]), str(attempt["payeeId"])
    dues = await friend_service.get_friend_dues(db, payer_id, payee_id)
    dues_amount = float(dues.get("oweAmount") or 0)
    if (
        dues.get("whoOwes") != "user"
        or abs(dues_amount - float(attempt["duesAmount"])) > 0.01
        or (dues.get("currency") or "").strip().upper()
        != attempt["duesCurrency"]
    ):
        reason = (
            "Payment verified, but the balance changed after it was created "
            "(new expenses, or the debt was already settled another way). "
            "Confirm the remaining balance manually instead of auto-settling."
        )
        await db[PAYPAL_SETTLEMENTS_COLLECTION].update_one(
            {"_id": attempt["_id"]},
            {
                "$set": {
                    "status": "NEEDS_REVIEW",
                    "captureId": verification["captureId"],
                    "verifyReason": reason,
                    "updatedAt": datetime.now(timezone.utc),
                }
            },
        )
        return {**verification, "settled": False, "settlementId": None,
                "reason": reason}

    now = datetime.now(timezone.utc)
    request_doc = {
        "payerId": ObjectId(payer_id),
        "payeeId": ObjectId(payee_id),
        "requestedBy": ObjectId(payer_id),
        "amount": round(float(attempt["duesAmount"]), 2),
        "currency": attempt["duesCurrency"],
        "status": "pending",
        "rail": "paypal",
        "paypalOrderId": attempt["orderId"],
        "paypalCaptureId": verification["captureId"],
        "createdAt": now,
        "updatedAt": now,
    }
    inserted = await db.settlement_request.insert_one(request_doc)
    settlement_id = inserted.inserted_id
    final = await friend_service._finalize_settlement(db, payee_id, settlement_id)
    if final.get("success") != "true":
        await db[PAYPAL_SETTLEMENTS_COLLECTION].update_one(
            {"_id": attempt["_id"]},
            {
                "$set": {
                    "status": "NEEDS_REVIEW",
                    "captureId": verification["captureId"],
                    "verifyReason": final.get("message", "Finalization failed."),
                    "updatedAt": datetime.now(timezone.utc),
                }
            },
        )
        return {
            **verification,
            "settled": False,
            "settlementId": None,
            "reason": final.get("message", "Finalization failed."),
        }

    await db[PAYPAL_SETTLEMENTS_COLLECTION].update_one(
        {"_id": attempt["_id"]},
        {
            "$set": {
                "status": "SETTLED",
                "captureId": verification["captureId"],
                "settlementRequestId": settlement_id,
                "verifyReason": verification["reason"],
                "updatedAt": datetime.now(timezone.utc),
            }
        },
    )
    return {**verification, "settled": True, "settlementId": str(settlement_id)}


async def capture_paypal_settlement(
    db: AsyncIOMotorDatabase,
    order_id: str,
    user_id: str,
    *,
    client: PayPalSettlementClient | None = None,
) -> dict:
    """Capture an approved order, then verify + finalize.

    Call this from the return-URL handler after the payer approves in
    PayPal Checkout. If the capture call itself fails (e.g. a double-tap
    already captured it), verification is still attempted — a completed
    capture on PayPal's side settles the debt either way.
    """
    attempt = await _load_paypal_attempt_for_user(db, order_id, user_id)
    if attempt["status"] == "SETTLED":
        return {
            "verified": True,
            "settled": True,
            "orderId": order_id,
            "orderStatus": "COMPLETED",
            "captureId": attempt.get("captureId"),
            "captureStatus": "COMPLETED",
            "amountMatches": True,
            "reason": "Already settled.",
            "settlementId": str(attempt["settlementRequestId"])
            if attempt.get("settlementRequestId")
            else None,
        }
    if attempt["status"] == "NEEDS_REVIEW":
        raise SettlementValidationError(
            attempt.get("verifyReason")
            or "This settlement needs manual review."
        )

    paypal = client or get_paypal_client()
    try:
        capture = await paypal.capture_order(order_id)
        await db[PAYPAL_SETTLEMENTS_COLLECTION].update_one(
            {"_id": attempt["_id"]},
            {
                "$set": {
                    "status": "CAPTURED",
                    "captureId": capture["captureId"],
                    "updatedAt": datetime.now(timezone.utc),
                }
            },
        )
    except PayPalApiError as exc:
        # A failed capture is not proof no money moved (double-tap, retry
        # after a timeout). Verify against PayPal's own records instead.
        logger.warning("PayPal capture for %s failed (%s); verifying anyway",
                       order_id, exc)
    return await _verify_and_finalize(db, attempt, paypal)


async def get_paypal_settlement_status(
    db: AsyncIOMotorDatabase,
    order_id: str,
    user_id: str,
    *,
    client: PayPalSettlementClient | None = None,
) -> dict:
    """Re-check a PayPal order and finalize if it is now captured + verified.

    Safe to poll — a settled attempt returns its stored outcome without
    re-finalizing.
    """
    attempt = await _load_paypal_attempt_for_user(db, order_id, user_id)
    if attempt["status"] == "SETTLED":
        return {
            "verified": True,
            "settled": True,
            "orderId": order_id,
            "orderStatus": "COMPLETED",
            "captureId": attempt.get("captureId"),
            "captureStatus": "COMPLETED",
            "amountMatches": True,
            "reason": "Already settled.",
            "settlementId": str(attempt["settlementRequestId"])
            if attempt.get("settlementRequestId")
            else None,
        }
    if attempt["status"] == "NEEDS_REVIEW":
        return {
            "verified": True,
            "settled": False,
            "orderId": order_id,
            "orderStatus": "COMPLETED",
            "captureId": attempt.get("captureId"),
            "captureStatus": "COMPLETED",
            "amountMatches": True,
            "reason": attempt.get("verifyReason")
            or "This settlement needs manual review.",
            "settlementId": None,
        }
    paypal = client or get_paypal_client()
    return await _verify_and_finalize(db, attempt, paypal)


async def handle_paypal_webhook(
    db: AsyncIOMotorDatabase,
    *,
    raw_body: bytes,
    headers: dict,
    client: PayPalSettlementClient | None = None,
) -> dict:
    """Handle a PayPal webhook delivery (production-grade confirmation).

    The signature is verified before anything else. Unknown event types and
    events with no matching settlement are acknowledged (accepted, ignored)
    so PayPal stops retrying them. Returns ``accepted: False`` only when
    the signature fails — the router turns that into a 401.
    """
    webhook_id = settings.PAYPAL_WEBHOOK_ID
    if not webhook_id:
        raise SettlementNotConfigured(
            "PayPal webhook is not configured. Set PAYPAL_WEBHOOK_ID after "
            "registering a webhook in the PayPal developer dashboard."
        )
    required = {
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "cert_url": headers.get("paypal-cert-url"),
        "auth_algo": headers.get("paypal-auth-algo"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
    }
    if any(v is None for v in required.values()):
        raise SettlementValidationError(
            "Webhook delivery is missing required PayPal headers."
        )
    raw_text = raw_body.decode("utf-8")
    paypal = client or get_paypal_client()
    verified = await paypal.verify_webhook_signature(
        transmission_id=required["transmission_id"],
        transmission_time=required["transmission_time"],
        cert_url=required["cert_url"],
        auth_algo=required["auth_algo"],
        transmission_sig=required["transmission_sig"],
        webhook_id=webhook_id,
        raw_body=raw_text,
    )
    if not verified:
        return {
            "accepted": False,
            "reason": "Webhook signature verification failed.",
        }
    event = json.loads(raw_text)
    event_type = event.get("event_type", "")
    if event_type != "PAYMENT.CAPTURE.COMPLETED":
        return {
            "accepted": True,
            "ignored": True,
            "reason": f"Ignoring event type {event_type}.",
        }
    resource = event.get("resource", {}) or {}
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    order_id = related.get("order_id")
    attempt = None
    if order_id:
        attempt = await db[PAYPAL_SETTLEMENTS_COLLECTION].find_one(
            {"orderId": order_id}
        )
    if attempt is None and resource.get("id"):
        attempt = await db[PAYPAL_SETTLEMENTS_COLLECTION].find_one(
            {"captureId": resource["id"]}
        )
    if attempt is None:
        return {
            "accepted": True,
            "ignored": True,
            "reason": "No matching PayPal settlement for this event.",
        }
    if attempt["status"] == "SETTLED":
        return {
            "accepted": True,
            "settled": True,
            "orderId": attempt["orderId"],
            "reason": "Already settled.",
        }
    result = await _verify_and_finalize(db, attempt, paypal)
    return {"accepted": True, **result}
