"""PayPal + UPI settlement tests — fully offline.

No live PayPal calls, no MongoDB. The PayPal client runs against
``httpx.MockTransport`` and the service layer against an in-memory fake
DB; the router tests mount only the settlement router on a fresh FastAPI
app with ``get_db`` / ``get_current_user`` overridden.

Golden values (QR hash, fee figures, UPI link, routing decisions) were
computed from the TypeScript reference implementation
(``~/workspace/your_files/splitllm-paypal-connector/paypal-settlement.js``)
and are asserted here so the Python port cannot drift from it.
"""

import hashlib
import json

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.auth import get_current_user
from app.database import get_db
from app.routers import settlement_router
from app.services import friend_service, fx_service, settlement_service, user_service
from app.services.settlement_service import (
    PayPalApiError,
    PayPalConfig,
    PayPalSettlementClient,
    SettlementNotConfigured,
    SettlementValidationError,
    build_upi_intent_link,
    build_upi_qr_svg,
    estimate_receiver_fee,
    format_inr_amount,
    format_money,
    get_paypal_client,
    normalize_vpa,
    reset_paypal_client,
    route_settlement,
    upi_qr_modules,
)

# ---------------------------------------------------------------------------
# The parent tests/conftest.py connects to a real MongoDB in a
# session-scoped autouse fixture. These tests run with an in-memory fake DB
# and a mocked PayPal transport instead, so this module-level fixture
# shadows it (same name, same scope) for the tests in this file only.
# It must live in this module, NOT in a tests/offline/conftest.py: a
# second conftest.py breaks `from conftest import ...` in the existing
# test files (both would import as top-level `conftest`).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
async def setup_database():
    yield


# ---------------------------------------------------------------------------
# Golden values from the TypeScript reference (verified via node)
# ---------------------------------------------------------------------------

GOLDEN_UPI_LINK = (
    "upi://pay?pa=siva@okhdfcbank&pn=Sivaganesh%20B"
    "&am=250.50&cu=INR&tn=Dinner"
)
# QR matrix for the golden link, byte mode, ECC level M (37x37, 686 dark).
GOLDEN_QR_SHA256 = (
    "2c95781995790768c25cefe837b96a906a753bb401c1a439a8743dea8dbaed53"
)

MOCK_UID = "test_user_uid_123"
PAYER_ID = ObjectId("64b64c2f4f4f4f4f4f4f4f01")
PAYEE_ID = ObjectId("64b64c2f4f4f4f4f4f4f4f02")


# ---------------------------------------------------------------------------
# Fake in-memory MongoDB (equality filters only — all the settlement code
# ever issues)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, inserted_id=None, matched_count=0, modified_count=0):
        self.inserted_id = inserted_id
        self.matched_count = matched_count
        self.modified_count = modified_count


def _matches(doc: dict, filter_: dict) -> bool:
    return all(doc.get(key) == value for key, value in filter_.items())


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, filter_, *args, **kwargs):
        for doc in self.docs:
            if _matches(doc, filter_):
                return dict(doc)
        return None

    async def insert_one(self, doc):
        stored = dict(doc)
        stored["_id"] = ObjectId()
        self.docs.append(stored)
        return _FakeResult(inserted_id=stored["_id"])

    async def update_one(self, filter_, update, *args, **kwargs):
        for doc in self.docs:
            if _matches(doc, filter_):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                return _FakeResult(matched_count=1, modified_count=1)
        return _FakeResult(matched_count=0, modified_count=0)


class FakeDb:
    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def _collection(self, name: str) -> FakeCollection:
        return self._collections.setdefault(name, FakeCollection())

    def __getattr__(self, name: str) -> FakeCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._collection(name)

    def __getitem__(self, name: str) -> FakeCollection:
        return self._collection(name)


def make_fake_db(payer_country="US", payee_country="IN") -> FakeDb:
    db = FakeDb()
    db.users.docs = [
        {
            "_id": PAYER_ID,
            "firebaseUid": MOCK_UID,
            "email": "payer@example.com",
            "name": "Payer",
            "friendCode": "PAYER001",
            "friends": [PAYEE_ID],
            "country": payer_country,
            "paypalEmail": "",
            "upiId": "",
            "defaultCurrency": "USD",
        },
        {
            "_id": PAYEE_ID,
            "firebaseUid": "payee_uid_456",
            "email": "payee@example.com",
            "name": "Payee",
            "friendCode": "PAYEE002",
            "friends": [PAYER_ID],
            "country": payee_country,
            "paypalEmail": "payee@example.com",
            "upiId": "payee@okhdfcbank",
            "defaultCurrency": "INR",
        },
    ]
    return db


# ---------------------------------------------------------------------------
# Mocked PayPal transport
# ---------------------------------------------------------------------------

ORDER_CREATED = {
    "id": "ORDER-1",
    "status": "CREATED",
    "links": [
        {
            "href": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1",
            "rel": "approve",
            "method": "GET",
        },
        {
            "href": "https://api.sandbox.paypal.com/v2/checkout/orders/ORDER-1",
            "rel": "self",
            "method": "GET",
        },
    ],
}

ORDER_COMPLETED = {
    "id": "ORDER-1",
    "status": "COMPLETED",
    "purchase_units": [
        {
            "amount": {"currency_code": "USD", "value": "25.00"},
            "payee": {"email_address": "payee@example.com"},
            "payments": {
                "captures": [
                    {
                        "id": "CAP-1",
                        "status": "COMPLETED",
                        "amount": {"currency_code": "USD", "value": "25.00"},
                    }
                ]
            },
        }
    ],
}


def make_paypal_transport(requests_log: list, *, capture_amount="25.00"):
    """MockTransport answering the token / order / capture / verify calls.

    ``capture_amount`` lets a test make the captured amount differ from
    the expected one (verification must then fail).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requests_log.append(request)
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "TEST-TOKEN",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if path == "/v2/checkout/orders" and request.method == "POST":
            return httpx.Response(201, json=ORDER_CREATED)
        if path == "/v2/checkout/orders/ORDER-1/capture":
            completed = json.loads(json.dumps(ORDER_COMPLETED))
            completed["purchase_units"][0]["payments"]["captures"][0][
                "amount"
            ]["value"] = capture_amount
            return httpx.Response(201, json=completed)
        if path == "/v2/checkout/orders/ORDER-1":
            completed = json.loads(json.dumps(ORDER_COMPLETED))
            completed["purchase_units"][0]["payments"]["captures"][0][
                "amount"
            ]["value"] = capture_amount
            return httpx.Response(200, json=completed)
        if path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": "SUCCESS"})
        return httpx.Response(404, json={"name": "NOT_FOUND"})

    return httpx.MockTransport(handler)


def make_paypal_client(requests_log: list, **transport_kwargs):
    return PayPalSettlementClient(
        PayPalConfig(
            client_id="test-client-id",
            client_secret="test-client-secret",
            environment="sandbox",
        ),
        transport=make_paypal_transport(requests_log, **transport_kwargs),
    )


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db():
    return make_fake_db()


@pytest.fixture
def fake_db_in():
    """Both parties in India — the UPI rail."""
    return make_fake_db(payer_country="IN", payee_country="IN")


@pytest.fixture
def mock_dues(monkeypatch):
    """Pretend the caller owes the friend $25.00."""

    async def _dues(db, user_id, friend_id):
        return {
            "name": "Payee",
            "id": str(PAYEE_ID),
            "oweAmount": 25.0,
            "whoOwes": "user",
            "currency": "USD",
        }

    monkeypatch.setattr(friend_service, "get_friend_dues", _dues)
    return _dues


@pytest.fixture
def mock_dues_inr(monkeypatch):
    """Pretend the caller owes the friend ₹250.50."""

    async def _dues(db, user_id, friend_id):
        return {
            "name": "Payee",
            "id": str(PAYEE_ID),
            "oweAmount": 250.5,
            "whoOwes": "user",
            "currency": "INR",
        }

    monkeypatch.setattr(friend_service, "get_friend_dues", _dues)
    return _dues


@pytest.fixture
def mock_finalize(monkeypatch):
    calls: list = []

    async def _finalize(db, actor_id, settlement_id):
        calls.append((actor_id, str(settlement_id)))
        return {"success": "true", "message": "Settlement recorded."}

    monkeypatch.setattr(friend_service, "_finalize_settlement", _finalize)
    return calls


@pytest.fixture
def mock_fx(monkeypatch):
    """No custom FX rates on the payer — the default (None, None)."""

    async def _table(db, user_id):
        return (None, None)

    monkeypatch.setattr(fx_service, "user_rate_table", _table)
    return _table


@pytest.fixture
def api(fake_db, monkeypatch):
    """TestClient with ONLY the settlement router mounted, auth + DB faked."""
    app = FastAPI()
    app.include_router(settlement_router.router, prefix="/db")

    async def _current_user():
        return {
            "uid": MOCK_UID,
            "email": "payer@example.com",
            "name": "Payer",
            "status": "active",
        }

    async def _internal_id(db, uid):
        return str(PAYER_ID)

    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_db] = lambda: fake_db
    monkeypatch.setattr(user_service, "get_user_internal_id", _internal_id)
    return TestClient(app)


@pytest.fixture
def mock_paypal_client(monkeypatch):
    """Patch the module-level get_paypal_client() so router/service calls
    hit the mocked transport instead of the real PayPal API."""
    requests_log: list = []
    client = make_paypal_client(requests_log)
    monkeypatch.setattr(settlement_service, "get_paypal_client", lambda **kw: client)
    return client, requests_log


# ---------------------------------------------------------------------------
# Pure functions — golden values from the TypeScript reference
# ---------------------------------------------------------------------------


def test_format_money():
    assert format_money(12.5) == "12.50"
    assert format_money(0.1) == "0.10"
    assert format_money("250.5") == "250.50"
    assert format_money(100) == "100.00"
    for bad in (0, -5, "12.345", "abc", "", None):
        with pytest.raises(SettlementValidationError):
            format_money(bad)


def test_fee_estimates_match_reference():
    # US domestic $100: 3.49% + $0.49 = $3.98.
    assert estimate_receiver_fee(100, "USD", cross_border=False) == {
        "gross": "100.00",
        "fee": "3.98",
        "netToCreditor": "96.02",
        "currency": "USD",
        "crossBorder": False,
        "rateNote": (
            "US domestic PayPal Checkout rate: 3.49% + $0.49. "
            "Fee is charged to the receiver (the creditor)."
        ),
    }
    # Cross-border $100: visible fee only, 4.4% + $0.30 = $4.70. The
    # ~3-4% FX markup + 18% GST are described in the note, not added.
    cross = estimate_receiver_fee(100, "USD", cross_border=True)
    assert cross["fee"] == "4.70"
    assert cross["netToCreditor"] == "95.30"
    assert "7.5" in cross["rateNote"] and "exact figure" in cross["rateNote"]
    assert estimate_receiver_fee(12.5, "USD", cross_border=False)["fee"] == "0.93"
    assert estimate_receiver_fee(1000, "INR", cross_border=False)["fee"] == "35.39"


def test_route_settlement_table():
    assert route_settlement(
        payer_country="IN", payee_country="IN", amount=100, currency="INR"
    )["rail"] == "upi"
    assert route_settlement(
        payer_country="US", payee_country="IN", amount=100, currency="USD"
    )["rail"] == "paypal"
    assert route_settlement(
        payer_country="IN", payee_country="US", amount=100, currency="USD"
    )["rail"] == "unavailable"
    assert route_settlement(
        payer_country="US", payee_country="US", amount=50, currency="USD"
    )["rail"] == "paypal"
    # Missing country is "unavailable", not a crash (the TS reference
    # throws on null here; the Python port answers instead).
    assert route_settlement(
        payer_country=None, payee_country="IN", amount=100, currency="INR"
    )["rail"] == "unavailable"
    assert route_settlement(
        payer_country="us", payee_country="in", amount=100, currency="USD"
    )["rail"] == "paypal"  # case-insensitive
    # India receipt cap: over $10,000 into India is unavailable.
    assert route_settlement(
        payer_country="US", payee_country="IN", amount=10000.01, currency="USD"
    )["rail"] == "unavailable"
    assert route_settlement(
        payer_country="US", payee_country="IN", amount=10000, currency="USD"
    )["rail"] == "paypal"


def test_upi_link_matches_reference():
    link = build_upi_intent_link(
        vpa="siva@okhdfcbank", name="Sivaganesh B", amount_inr=250.5, note="Dinner"
    )
    assert link == GOLDEN_UPI_LINK
    # NPCI order is pa, pn, am, cu, tn — and spaces encode as %20, never +.
    assert "pn=Sivaganesh%20B" in link
    assert "+" not in link
    # VPA is lowercased; the bank part needs at least 2 characters.
    assert normalize_vpa("Siva@OKHDFCBANK") == "siva@okhdfcbank"
    for bad in ("not-a-vpa", "a@b", "a@@b", "", "x@" * 40):
        with pytest.raises(SettlementValidationError):
            normalize_vpa(bad)
    # INR ceiling: over ₹1,00,000 must split.
    assert format_inr_amount(250.5) == "250.50"
    with pytest.raises(SettlementValidationError):
        format_inr_amount(100000.01)
    # Note limit (80) and name limit (99) are enforced.
    with pytest.raises(SettlementValidationError):
        build_upi_intent_link(
            vpa="a@okbank", name="X", amount_inr=10, note="n" * 81
        )


def test_qr_matrix_matches_typescript_reference():
    """The embedded QR encoder must produce the exact module matrix the
    TypeScript reference produces for the same input."""
    modules = upi_qr_modules(GOLDEN_UPI_LINK)
    assert len(modules) == 37
    assert all(len(row) == 37 for row in modules)
    flat = "\n".join("".join("1" if cell else "0" for cell in row) for row in modules)
    assert hashlib.sha256(flat.encode()).hexdigest() == GOLDEN_QR_SHA256
    assert sum(sum(row) for row in modules) == 686
    # Finder patterns: the three corners are dark 7x7 frames.
    assert modules[0][0] and modules[0][6] and modules[6][0] and modules[6][6]
    assert modules[0][36] and modules[36][0]
    # Too long for version 10 at level M.
    with pytest.raises(SettlementValidationError):
        upi_qr_modules("x" * 300)


def test_qr_svg_structure():
    svg = build_upi_qr_svg(GOLDEN_UPI_LINK)
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert 'viewBox="0 0 360 360"' in svg  # (37 + 2*4) * 8
    assert svg.count("<path") == 1
    assert "#000000" in svg and "#ffffff" in svg


# ---------------------------------------------------------------------------
# PayPal client — mocked transport, no network
# ---------------------------------------------------------------------------


async def test_client_create_order_uses_payee_override():
    log: list = []
    client = make_paypal_client(log)
    order = await client.create_settlement_order(
        creditor_paypal_email="payee@example.com",
        amount=25,
        currency="USD",
        description="SplitLLM settlement",
        splitllm_debt_id="debt-123",
        idempotency_key="splitllm-debt-123",
    )
    assert order["orderId"] == "ORDER-1"
    assert order["approvalUrl"].startswith("https://www.sandbox.paypal.com/")
    create = [r for r in log if r.url.path == "/v2/checkout/orders"][0]
    body = json.loads(create.content)
    assert body["intent"] == "CAPTURE"
    assert body["purchase_units"][0]["payee"]["email_address"] == "payee@example.com"
    assert body["purchase_units"][0]["amount"] == {
        "currency_code": "USD",
        "value": "25.00",
    }
    assert body["purchase_units"][0]["reference_id"] == "debt-123"
    assert create.headers["PayPal-Request-Id"] == "splitllm-debt-123"
    # The OAuth token was fetched exactly once.
    assert len([r for r in log if r.url.path == "/v1/oauth2/token"]) == 1
    await client.aclose()


async def test_client_token_is_cached_across_calls():
    log: list = []
    client = make_paypal_client(log)
    await client.create_settlement_order(
        creditor_paypal_email="payee@example.com", amount=25, currency="USD"
    )
    await client.create_settlement_order(
        creditor_paypal_email="payee@example.com", amount=25, currency="USD"
    )
    assert len([r for r in log if r.url.path == "/v1/oauth2/token"]) == 1
    await client.aclose()


async def test_client_verify_capture_happy_and_mismatch():
    log: list = []
    client = make_paypal_client(log)
    result = await client.verify_capture("ORDER-1", "25.00", "USD")
    assert result["verified"] is True
    assert result["captureId"] == "CAP-1"
    assert result["captureStatus"] == "COMPLETED"
    assert result["amountMatches"] is True

    log2: list = []
    wrong = make_paypal_client(log2, capture_amount="24.00")
    result = await wrong.verify_capture("ORDER-1", "25.00", "USD")
    assert result["verified"] is False
    assert result["amountMatches"] is False
    assert "24.00" in result["reason"] and "25.00" in result["reason"]
    await client.aclose()
    await wrong.aclose()


async def test_client_create_order_rejects_bad_email():
    log: list = []
    client = make_paypal_client(log)
    with pytest.raises(SettlementValidationError):
        await client.create_settlement_order(
            creditor_paypal_email="not-an-email", amount=25, currency="USD"
        )
    await client.aclose()


async def test_client_api_error_surfaces_status():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "t", "token_type": "Bearer", "expires_in": 3600},
            )
        return httpx.Response(
            401, json={"name": "AUTHENTICATION_FAILURE", "message": "bad creds"}
        )

    client = PayPalSettlementClient(
        PayPalConfig(
            client_id="bad", client_secret="bad", environment="sandbox"
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PayPalApiError) as excinfo:
        await client.create_settlement_order(
            creditor_paypal_email="payee@example.com", amount=25, currency="USD"
        )
    assert excinfo.value.status == 401
    await client.aclose()


# ---------------------------------------------------------------------------
# Service orchestration — fake DB + mocked PayPal
# ---------------------------------------------------------------------------


def _seed_created_attempt(db: FakeDb) -> dict:
    attempt = {
        "_id": ObjectId(),
        "payerId": PAYER_ID,
        "payeeId": PAYEE_ID,
        "amount": "25.00",
        "currency": "USD",
        "duesAmount": "25.00",
        "duesCurrency": "USD",
        "fxRate": None,
        "orderId": "ORDER-1",
        "approvalUrl": "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1",
        "captureId": None,
        "feeEstimate": None,
        "status": "CREATED",
        "verifyReason": None,
        "settlementRequestId": None,
    }
    db["paypal_settlements"].docs.append(attempt)
    return attempt


async def test_start_paypal_happy_path(fake_db, mock_dues, mock_fx):
    log: list = []
    client = make_paypal_client(log)
    result = await settlement_service.start_paypal_settlement(
        fake_db, str(PAYER_ID), str(PAYEE_ID), client=client
    )
    assert result["rail"] == "paypal"
    assert result["orderId"] == "ORDER-1"
    assert result["approvalUrl"].startswith("https://www.sandbox.paypal.com/")
    assert result["amount"] == "25.00"
    assert result["currency"] == "USD"
    # US payer -> IN payee is cross-border: 4.4% + $0.30 on $25 = $1.40.
    assert result["feeEstimate"]["fee"] == "1.40"
    attempt = await fake_db["paypal_settlements"].find_one({"orderId": "ORDER-1"})
    assert attempt is not None
    assert attempt["status"] == "CREATED"
    assert attempt["duesAmount"] == "25.00"
    await client.aclose()


async def test_start_paypal_rejects_when_friend_owes_you(fake_db, monkeypatch):
    async def _dues(db, user_id, friend_id):
        return {
            "name": "Payee",
            "id": str(PAYEE_ID),
            "oweAmount": 25.0,
            "whoOwes": "friend",
            "currency": "USD",
        }

    monkeypatch.setattr(friend_service, "get_friend_dues", _dues)
    with pytest.raises(SettlementValidationError, match="friend owes you"):
        await settlement_service.start_paypal_settlement(
            fake_db, str(PAYER_ID), str(PAYEE_ID)
        )


async def test_start_paypal_rejects_without_creditor_email(
    fake_db, mock_dues, mock_fx, monkeypatch
):
    # Creditor with no PayPal email on file and no override.
    payee = await fake_db.users.find_one({"_id": PAYEE_ID})
    payee["paypalEmail"] = ""
    await fake_db.users.update_one(
        {"_id": PAYEE_ID}, {"$set": {"paypalEmail": ""}}
    )
    with pytest.raises(SettlementValidationError, match="PayPal email"):
        await settlement_service.start_paypal_settlement(
            fake_db, str(PAYER_ID), str(PAYEE_ID)
        )


async def test_start_paypal_inr_needs_explicit_fx_rate(fake_db, monkeypatch):
    async def _dues(db, user_id, friend_id):
        return {
            "name": "Payee",
            "id": str(PAYEE_ID),
            "oweAmount": 8300.0,
            "whoOwes": "user",
            "currency": "INR",
        }

    async def _table(db, user_id):
        return (None, None)

    monkeypatch.setattr(friend_service, "get_friend_dues", _dues)
    monkeypatch.setattr(fx_service, "user_rate_table", _table)
    # An INR debt with no explicit INR->USD rate must fail loudly — never
    # guess an FX rate for money movement.
    with pytest.raises(SettlementValidationError, match="INR"):
        await settlement_service.start_paypal_settlement(
            fake_db, str(PAYER_ID), str(PAYEE_ID)
        )


async def test_start_paypal_inr_with_explicit_fx_rate(fake_db, monkeypatch):
    async def _dues(db, user_id, friend_id):
        return {
            "name": "Payee",
            "id": str(PAYEE_ID),
            "oweAmount": 8300.0,
            "whoOwes": "user",
            "currency": "INR",
        }

    async def _table(db, user_id):
        # Payer's table, quoted in USD: 1 INR = $0.012.
        return ({"INR": 0.012}, "USD")

    monkeypatch.setattr(friend_service, "get_friend_dues", _dues)
    monkeypatch.setattr(fx_service, "user_rate_table", _table)
    log: list = []
    client = make_paypal_client(log)
    result = await settlement_service.start_paypal_settlement(
        fake_db, str(PAYER_ID), str(PAYEE_ID), client=client
    )
    assert result["amount"] == "99.60"  # 8300 * 0.012
    assert result["currency"] == "USD"
    await client.aclose()


async def test_start_upi_happy_path(fake_db_in, mock_dues_inr):
    result = await settlement_service.start_upi_settlement(
        fake_db_in, str(PAYER_ID), str(PAYEE_ID), note="Dinner"
    )
    assert result["rail"] == "upi"
    assert result["intentLink"] == (
        "upi://pay?pa=payee@okhdfcbank&pn=Payee&am=250.50&cu=INR&tn=Dinner"
    )
    assert result["qrSvg"].startswith("<svg")
    assert result["amount"] == "250.50"
    request = await fake_db_in.settlement_request.find_one(
        {"_id": ObjectId(result["settlementId"])}
    )
    assert request is not None
    assert request["status"] == "pending"
    assert request["rail"] == "upi"
    assert request["upiLink"] == result["intentLink"]


async def test_start_upi_rejects_paypal_route(fake_db, mock_dues, mock_fx):
    # US payer -> IN payee routes to PayPal, not UPI.
    with pytest.raises(SettlementValidationError, match="PayPal"):
        await settlement_service.start_upi_settlement(
            fake_db, str(PAYER_ID), str(PAYEE_ID)
        )


async def test_capture_flow_settles_end_to_end(
    fake_db, mock_dues, mock_fx, mock_finalize
):
    log: list = []
    client = make_paypal_client(log)
    _seed_created_attempt(fake_db)
    result = await settlement_service.capture_paypal_settlement(
        fake_db, "ORDER-1", str(PAYER_ID), client=client
    )
    assert result["verified"] is True
    assert result["settled"] is True
    assert result["settlementId"] is not None
    assert result["captureId"] == "CAP-1"
    # The debt was finalized through the existing flow (once).
    assert len(mock_finalize) == 1
    assert mock_finalize[0][0] == str(PAYEE_ID)
    # The attempt is now SETTLED, and a second capture is idempotent.
    attempt = await fake_db["paypal_settlements"].find_one({"orderId": "ORDER-1"})
    assert attempt["status"] == "SETTLED"
    again = await settlement_service.capture_paypal_settlement(
        fake_db, "ORDER-1", str(PAYER_ID), client=client
    )
    assert again["settled"] is True
    assert len(mock_finalize) == 1  # not finalized twice
    await client.aclose()


async def test_capture_needs_review_when_balance_changed(
    fake_db, mock_fx, mock_finalize, monkeypatch
):
    async def _dues(db, user_id, friend_id):
        # New expenses landed after the order was created.
        return {
            "name": "Payee",
            "id": str(PAYEE_ID),
            "oweAmount": 30.0,
            "whoOwes": "user",
            "currency": "USD",
        }

    monkeypatch.setattr(friend_service, "get_friend_dues", _dues)
    log: list = []
    client = make_paypal_client(log)
    _seed_created_attempt(fake_db)
    result = await settlement_service.capture_paypal_settlement(
        fake_db, "ORDER-1", str(PAYER_ID), client=client
    )
    # Money verified, but the debt is NOT auto-settled — a human must look.
    assert result["verified"] is True
    assert result["settled"] is False
    assert "balance changed" in result["reason"]
    assert mock_finalize == []
    attempt = await fake_db["paypal_settlements"].find_one({"orderId": "ORDER-1"})
    assert attempt["status"] == "NEEDS_REVIEW"
    await client.aclose()


async def test_capture_amount_mismatch_does_not_settle(
    fake_db, mock_dues, mock_fx, mock_finalize
):
    log: list = []
    client = make_paypal_client(log, capture_amount="24.00")
    _seed_created_attempt(fake_db)
    result = await settlement_service.capture_paypal_settlement(
        fake_db, "ORDER-1", str(PAYER_ID), client=client
    )
    assert result["verified"] is False
    assert result["settled"] is False
    assert mock_finalize == []
    await client.aclose()


def _webhook_event(order_id="ORDER-1"):
    return {
        "id": "WH-EVENT-1",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": "CAP-1",
            "supplementary_data": {"related_ids": {"order_id": order_id}},
        },
    }


def _webhook_headers():
    return {
        "paypal-transmission-id": "transmission-1",
        "paypal-transmission-time": "2026-11-08T12:00:00Z",
        "paypal-cert-url": "https://api.sandbox.paypal.com/certs",
        "paypal-auth-algo": "SHA256withRSA",
        "paypal-transmission-sig": "sig",
    }


async def test_webhook_success_settles(
    fake_db, mock_dues, mock_fx, mock_finalize, monkeypatch
):
    monkeypatch.setattr(settings, "PAYPAL_WEBHOOK_ID", "WH-TEST-123")
    log: list = []
    client = make_paypal_client(log)
    _seed_created_attempt(fake_db)
    result = await settlement_service.handle_paypal_webhook(
        fake_db,
        raw_body=json.dumps(_webhook_event()).encode(),
        headers=_webhook_headers(),
        client=client,
    )
    assert result["accepted"] is True
    assert result["settled"] is True
    assert len(mock_finalize) == 1
    # The signature-verification call carried the webhook id + event.
    verify = [
        r for r in log if r.url.path == "/v1/notifications/verify-webhook-signature"
    ]
    assert len(verify) == 1
    body = json.loads(verify[0].content)
    assert body["webhook_id"] == "WH-TEST-123"
    assert body["webhook_event"]["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    await client.aclose()


async def test_webhook_bad_signature_rejected(
    fake_db, mock_dues, mock_fx, mock_finalize, monkeypatch
):
    monkeypatch.setattr(settings, "PAYPAL_WEBHOOK_ID", "WH-TEST-123")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "t", "token_type": "Bearer", "expires_in": 3600},
            )
        return httpx.Response(200, json={"verification_status": "FAILURE"})

    client = PayPalSettlementClient(
        PayPalConfig(client_id="id", client_secret="secret", environment="sandbox"),
        transport=httpx.MockTransport(handler),
    )
    _seed_created_attempt(fake_db)
    result = await settlement_service.handle_paypal_webhook(
        fake_db,
        raw_body=json.dumps(_webhook_event()).encode(),
        headers=_webhook_headers(),
        client=client,
    )
    assert result["accepted"] is False
    assert mock_finalize == []
    await client.aclose()


async def test_webhook_unknown_event_acknowledged_not_settled(
    fake_db, mock_dues, mock_fx, mock_finalize, monkeypatch
):
    monkeypatch.setattr(settings, "PAYPAL_WEBHOOK_ID", "WH-TEST-123")
    log: list = []
    client = make_paypal_client(log)
    _seed_created_attempt(fake_db)
    event = _webhook_event()
    event["event_type"] = "CHECKOUT.ORDER.APPROVED"
    result = await settlement_service.handle_paypal_webhook(
        fake_db,
        raw_body=json.dumps(event).encode(),
        headers=_webhook_headers(),
        client=client,
    )
    assert result["accepted"] is True
    assert result.get("ignored") is True
    assert mock_finalize == []
    await client.aclose()


def test_get_paypal_client_requires_config(monkeypatch):
    reset_paypal_client()
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "")
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_SECRET", "")
    with pytest.raises(SettlementNotConfigured):
        get_paypal_client()
    reset_paypal_client()


# ---------------------------------------------------------------------------
# Profile fields — country + PayPal email (additive, validated)
# ---------------------------------------------------------------------------


async def test_update_account_country_and_paypal_email(fake_db):
    ok = await user_service.update_user_account(
        fake_db,
        MOCK_UID,
        {"country": "in", "paypalEmail": "creditor@example.com"},
    )
    assert ok is True
    user = await fake_db.users.find_one({"firebaseUid": MOCK_UID})
    assert user["country"] == "IN"
    assert user["paypalEmail"] == "creditor@example.com"

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await user_service.update_user_account(
            fake_db, MOCK_UID, {"country": "USA"}
        )
    with pytest.raises(HTTPException):
        await user_service.update_user_account(
            fake_db, MOCK_UID, {"paypalEmail": "not-an-email"}
        )


async def test_get_user_account_includes_settlement_fields(fake_db):
    account = await user_service.get_user_account(fake_db, MOCK_UID)
    assert account["country"] == "US"
    assert account["paypalEmail"] == ""


# ---------------------------------------------------------------------------
# Router — only the settlement router mounted, auth + DB faked
# ---------------------------------------------------------------------------


@pytest.fixture
def api_in(fake_db_in, monkeypatch):
    app = FastAPI()
    app.include_router(settlement_router.router, prefix="/db")

    async def _current_user():
        return {
            "uid": MOCK_UID,
            "email": "payer@example.com",
            "name": "Payer",
            "status": "active",
        }

    async def _internal_id(db, uid):
        return str(PAYER_ID)

    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_db] = lambda: fake_db_in
    monkeypatch.setattr(user_service, "get_user_internal_id", _internal_id)
    return TestClient(app)


def test_router_paypal_start(api, mock_dues, mock_fx, mock_paypal_client):
    resp = api.post(
        f"/db/user/expense/friend/{PAYEE_ID}/settleup/paypal",
        json={"note": "Dinner"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rail"] == "paypal"
    assert body["orderId"] == "ORDER-1"
    assert body["approvalUrl"].startswith("https://www.sandbox.paypal.com/")
    assert body["feeEstimate"]["fee"] == "1.40"  # cross-border US -> IN


def test_router_paypal_start_friend_owes_you(api, mock_fx, monkeypatch):
    async def _dues(db, user_id, friend_id):
        return {
            "name": "Payee",
            "id": str(PAYEE_ID),
            "oweAmount": 25.0,
            "whoOwes": "friend",
            "currency": "USD",
        }

    monkeypatch.setattr(friend_service, "get_friend_dues", _dues)
    resp = api.post(
        f"/db/user/expense/friend/{PAYEE_ID}/settleup/paypal", json={}
    )
    assert resp.status_code == 400
    assert "friend owes you" in resp.text


def test_router_upi_start(api_in, mock_dues_inr):
    resp = api_in.post(
        f"/db/user/expense/friend/{PAYEE_ID}/settleup/upi",
        json={"note": "Dinner"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rail"] == "upi"
    assert body["intentLink"].startswith("upi://pay?pa=payee@okhdfcbank")
    assert body["qrSvg"].startswith("<svg")
    assert body["amount"] == "250.50"


def test_router_capture_and_status(
    api, fake_db, mock_dues, mock_fx, mock_finalize, mock_paypal_client
):
    _seed_created_attempt(fake_db)
    resp = api.post("/db/settlements/paypal/ORDER-1/capture")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verified"] is True
    assert body["settled"] is True

    resp = api.get("/db/settlements/paypal/ORDER-1/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["settled"] is True
    assert len(mock_finalize) == 1


def test_router_capture_unknown_order_is_404(api, mock_paypal_client):
    resp = api.post("/db/settlements/paypal/NOPE/capture")
    assert resp.status_code == 404


def test_router_webhook(
    api, fake_db, mock_dues, mock_fx, mock_finalize, mock_paypal_client, monkeypatch
):
    monkeypatch.setattr(settings, "PAYPAL_WEBHOOK_ID", "WH-TEST-123")
    _seed_created_attempt(fake_db)
    resp = api.post(
        "/db/webhooks/paypal",
        content=json.dumps(_webhook_event()),
        headers={**_webhook_headers(), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["accepted"] is True
    assert body["settled"] is True


def test_router_webhook_missing_headers_is_400(api, monkeypatch):
    monkeypatch.setattr(settings, "PAYPAL_WEBHOOK_ID", "WH-TEST-123")
    resp = api.post(
        "/db/webhooks/paypal",
        content=json.dumps(_webhook_event()),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
