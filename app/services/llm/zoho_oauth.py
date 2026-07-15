"""Zoho OAuth access-token management for Catalyst/QuickML APIs.

Uses a Self Client (created at api-console.zoho.in / .com) whose refresh token
is stored in env. Access tokens are cached in-process until shortly before
expiry (~1 hour).
"""

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_cached_token: str | None = None
_expires_at: float = 0.0


class ZohoAuthError(RuntimeError):
    pass


async def get_access_token() -> str:
    """Return a valid Zoho OAuth access token, refreshing if needed."""
    global _cached_token, _expires_at

    if _cached_token and time.monotonic() < _expires_at:
        return _cached_token

    if not (
        settings.ZOHO_CLIENT_ID
        and settings.ZOHO_CLIENT_SECRET
        and settings.ZOHO_REFRESH_TOKEN
    ):
        raise ZohoAuthError(
            "Zoho OAuth is not configured: set ZOHO_CLIENT_ID, "
            "ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN"
        )

    url = f"{settings.ZOHO_ACCOUNTS_URL.rstrip('/')}/oauth/v2/token"
    params = {
        "grant_type": "refresh_token",
        "refresh_token": settings.ZOHO_REFRESH_TOKEN,
        "client_id": settings.ZOHO_CLIENT_ID,
        "client_secret": settings.ZOHO_CLIENT_SECRET,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, params=params)
    data = resp.json() if resp.content else {}
    if resp.status_code >= 300 or "access_token" not in data:
        logger.error("Zoho token refresh failed: %s %s", resp.status_code, data)
        raise ZohoAuthError(f"Zoho token refresh failed: {data.get('error', resp.status_code)}")

    _cached_token = data["access_token"]
    # Refresh 60s before actual expiry
    _expires_at = time.monotonic() + int(data.get("expires_in", 3600)) - 60
    return _cached_token
