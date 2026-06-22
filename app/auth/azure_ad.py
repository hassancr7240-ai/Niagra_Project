from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from jose import JWTError, jwt
from jose.utils import base64url_decode

from app.config import get_settings

settings = get_settings()

_jwks_cache: dict = {}
_jwks_fetched_at: float = 0
_JWKS_TTL = 3600  # seconds


@dataclass
class TokenClaims:
    user_id: str       # oid claim (Azure AD Object ID)
    email: str
    name: str
    roles: list[str]
    tid: str           # tenant id


async def _get_jwks() -> dict:
    global _jwks_cache, _jwks_fetched_at
    if time.time() - _jwks_fetched_at < _JWKS_TTL and _jwks_cache:
        return _jwks_cache

    openid_config_url = (
        f"{settings.azure_ad_authority}{settings.azure_ad_tenant_id}"
        f"/v2.0/.well-known/openid-configuration"
    )
    async with httpx.AsyncClient() as client:
        resp = await client.get(openid_config_url, timeout=10)
        resp.raise_for_status()
        openid_config = resp.json()

        jwks_resp = await client.get(openid_config["jwks_uri"], timeout=10)
        jwks_resp.raise_for_status()
        _jwks_cache = jwks_resp.json()
        _jwks_fetched_at = time.time()

    return _jwks_cache


async def validate_azure_token(token: str) -> TokenClaims:
    """Validate an Azure AD JWT and return decoded claims."""
    jwks = await _get_jwks()

    header = jwt.get_unverified_header(token)
    kid = header.get("kid")

    signing_key = None
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            signing_key = key
            break

    if not signing_key:
        raise JWTError("Unable to find matching signing key")

    audience = settings.azure_ad_client_id
    issuer = f"https://login.microsoftonline.com/{settings.azure_ad_tenant_id}/v2.0"

    payload = jwt.decode(
        token,
        signing_key,
        algorithms=["RS256"],
        audience=audience,
        issuer=issuer,
        options={"verify_at_hash": False},
    )

    return TokenClaims(
        user_id=payload.get("oid", ""),
        email=payload.get("upn") or payload.get("email") or payload.get("preferred_username", ""),
        name=payload.get("name", ""),
        roles=payload.get("roles", []),
        tid=payload.get("tid", ""),
    )


def validate_dev_token(token: str) -> Optional[TokenClaims]:
    """
    In dev mode (no Azure AD configured), accept simple HS256 dev tokens
    or the raw dev API key.
    """
    if token == settings.dev_api_key:
        return TokenClaims(
            user_id="dev-user-001",
            email="dev@pm-system.local",
            name="Dev User",
            roles=["Manager"],
            tid="dev",
        )
    try:
        payload = jwt.decode(
            token,
            settings.app_secret_key,
            algorithms=["HS256"],
        )
        return TokenClaims(
            user_id=payload.get("sub", "dev-user"),
            email=payload.get("email", "dev@local"),
            name=payload.get("name", "Dev User"),
            roles=payload.get("roles", ["Manager"]),
            tid="dev",
        )
    except JWTError:
        return None


def create_dev_token(user_id: str, email: str, name: str, roles: list[str]) -> str:
    """Create a 24-hour dev token. Long enough to never expire during RAG pipeline uploads."""
    payload = {
        "sub": user_id,
        "email": email,
        "name": name,
        "roles": roles,
        "exp": int(time.time()) + 86400,  # 24 hours — prevents logout during long uploads
        "iat": int(time.time()),
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm="HS256")
