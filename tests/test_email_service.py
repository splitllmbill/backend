"""Unit tests for the ZeptoMail email service.

Offline: the gating/formatting helpers are pure, and the message builders are
tested with ``_send`` monkeypatched so nothing is actually sent.
"""

import pytest

from app.services import email_service


class TestGating:
    def test_disabled_without_token(self, monkeypatch):
        monkeypatch.setattr(email_service.settings, "ZEPTOMAIL_ENABLED", True)
        monkeypatch.setattr(email_service.settings, "ZEPTOMAIL_TOKEN", "")
        assert email_service._enabled() is False

    def test_disabled_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(email_service.settings, "ZEPTOMAIL_ENABLED", False)
        monkeypatch.setattr(email_service.settings, "ZEPTOMAIL_TOKEN", "tok")
        assert email_service._enabled() is False

    def test_enabled_with_flag_and_token(self, monkeypatch):
        monkeypatch.setattr(email_service.settings, "ZEPTOMAIL_ENABLED", True)
        monkeypatch.setattr(email_service.settings, "ZEPTOMAIL_TOKEN", "tok")
        assert email_service._enabled() is True

    async def test_send_short_circuits_when_disabled(self, monkeypatch):
        monkeypatch.setattr(email_service.settings, "ZEPTOMAIL_ENABLED", False)
        ok = await email_service._send("a@b.com", "A", "subject", "<p>x</p>")
        assert ok is False


class TestSymbol:
    def test_known_currency(self):
        assert email_service._symbol("USD") == "$"
        assert email_service._symbol("inr") == "₹"

    def test_unknown_falls_back_to_default_currency(self):
        # normalize_currency maps unknown codes to DEFAULT_CURRENCY (INR -> ₹)
        assert email_service._symbol("ZZZ") == "₹"
        assert email_service._symbol("") == "₹"


def _capture_send(monkeypatch):
    """Replace _send with a spy; returns the dict it records into."""
    captured = {}

    async def fake_send(to_email, to_name, subject, html_body):
        captured.update(
            to_email=to_email, to_name=to_name, subject=subject, html=html_body
        )
        return True

    monkeypatch.setattr(email_service, "_send", fake_send)
    return captured


async def test_invite_email_names_inviter(monkeypatch):
    captured = _capture_send(monkeypatch)
    ok = await email_service.send_invite_email("neo@zion.io", "Neo", "Trinity")
    assert ok is True
    assert captured["to_email"] == "neo@zion.io"
    assert "Trinity" in captured["subject"]
    assert "Neo" in captured["html"]


async def test_expense_notification_formats_amount_and_escapes(monkeypatch):
    captured = _capture_send(monkeypatch)
    await email_service.send_expense_notification(
        to_email="x@y.io",
        to_name="Sam",
        creator_name="Bob",
        expense_name="Pizza <b>night</b>",
        amount=1234.5,
        currency="USD",
        event_name="Trip & Co",
    )
    html = captured["html"]
    assert "$1,234.50" in html
    assert "Pizza &lt;b&gt;night&lt;/b&gt;" in html  # HTML-escaped, not injected
    assert "Trip &amp; Co" in html
    assert "$1,234.50" in captured["subject"]


async def test_settleup_email_omits_amount_when_none(monkeypatch):
    captured = _capture_send(monkeypatch)
    await email_service.send_settleup_email("x@y.io", "Sam", "Bob")
    # No amount block rendered, but the settle-up copy is present.
    assert "settled up" in captured["html"].lower()


async def test_payment_request_uses_pay_now_cta_with_link(monkeypatch):
    captured = _capture_send(monkeypatch)
    await email_service.send_payment_request_email(
        to_email="x@y.io",
        to_name="Sam",
        requester_name="Bob",
        amount=50.0,
        currency="INR",
        payment_link="https://splitllm.com/payments/abc",
    )
    assert "Pay now" in captured["html"]
    assert "https://splitllm.com/payments/abc" in captured["html"]
    assert "₹50.00" in captured["subject"]
