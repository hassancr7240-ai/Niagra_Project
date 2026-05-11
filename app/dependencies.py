from __future__ import annotations

from typing import Annotated, AsyncGenerator

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.azure_ad import validate_azure_token, validate_dev_token
from app.auth.rbac import CurrentUser, normalize_role
from app.config import get_settings
from app.db.crud import upsert_user
from app.db.database import get_db

settings = get_settings()
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    token: str | None = None

    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        # Also accept X-API-Key header for dev/testing
        token = request.headers.get("X-API-Key")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided",
            headers={"WWW-Authenticate": "Bearer"},
        )

    ip = request.client.host if request.client else None

    try:
        if settings.use_azure_ad:
            claims = await validate_azure_token(token)
        else:
            claims = validate_dev_token(token)
            if claims is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid API key or token",
                )

        role = normalize_role(claims.roles) if claims.roles else "Technician"

        await upsert_user(
            db,
            {
                "user_id": claims.user_id,
                "email": claims.email,
                "name": claims.name,
                "role": role,
                "last_login": __import__("datetime").datetime.utcnow(),
            },
        )

        return CurrentUser(
            user_id=claims.user_id,
            email=claims.email,
            name=claims.name,
            role=role,
            ip_address=ip,
        )
    except HTTPException:
        raise
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token validation failed: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication error",
            headers={"WWW-Authenticate": "Bearer"},
        )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
DBDep = Annotated[AsyncSession, Depends(get_db)]
