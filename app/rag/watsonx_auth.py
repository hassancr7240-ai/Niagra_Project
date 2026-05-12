from __future__ import annotations

"""
IBM watsonx.ai IAM token helper.

IBM watsonx requires a short-lived IAM Bearer token obtained by exchanging
the API key at https://iam.cloud.ibm.com/identity/token. The raw API key
must NOT be passed as a Bearer token — that always returns 401.

Token TTL is 3600 s; we refresh 5 minutes before expiry.
"""

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_IAM_URL = "https://iam.cloud.ibm.com/identity/token"
_cached_token: Optional[str] = None
_token_expiry: float = 0.0
_REFRESH_BUFFER = 300  # seconds before expiry to pre-refresh


async def get_iam_token(api_key: str) -> str:
    """
    Return a valid IAM Bearer token for the given API key.
    Caches the token and refreshes automatically before expiry.
    """
    global _cached_token, _token_expiry

    now = time.time()
    if _cached_token and now < _token_expiry - _REFRESH_BUFFER:
        return _cached_token

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _IAM_URL,
                data={
                    "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                    "apikey": api_key,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()
            _cached_token = data["access_token"]
            _token_expiry = now + int(data.get("expires_in", 3600))
            logger.debug("IBM IAM token refreshed, expires in %ds", data.get("expires_in", 3600))
            return _cached_token
    except Exception as exc:
        logger.error("IBM IAM token fetch failed: %s", exc)
        raise RuntimeError(f"Failed to obtain IBM IAM token: {exc}") from exc


async def watsonx_headers(api_key: str) -> dict:
    """Return auth + content-type headers for watsonx REST calls."""
    token = await get_iam_token(api_key)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
