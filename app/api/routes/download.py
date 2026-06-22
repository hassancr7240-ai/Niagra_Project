from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.storage_module import get_local_file
from app.db.database import get_db
from app.dependencies import CurrentUserDep, get_current_user

router = APIRouter(prefix="/api/download", tags=["Download"])
settings = get_settings()
_bearer = HTTPBearer(auto_error=False)


@router.get("/{machine_id}/{year}/{month}/{file_name}")
async def download_pm_file(
    machine_id: str,
    year: str,
    month: str,
    file_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    token: Optional[str] = Query(None, description="Bearer token as query param for direct browser links"),
) -> FileResponse:
    """
    Download a locally stored PM document.
    Accepts auth via:
      - Authorization: Bearer <token>  header (API calls)
      - ?token=<token>                 query param (direct browser download links)
    """
    # Build a synthetic request with the token if passed as query param
    raw_token = None
    if credentials:
        raw_token = credentials.credentials
    elif token:
        raw_token = token
    elif settings.dev_api_key and request.headers.get("X-API-Key") == settings.dev_api_key:
        raw_token = settings.dev_api_key

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required for download",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate token
    from app.auth.azure_ad import validate_azure_token, validate_dev_token
    from app.db.crud import upsert_user
    from app.auth.rbac import normalize_role, CurrentUser
    from datetime import datetime
    try:
        if settings.use_azure_ad:
            claims = await validate_azure_token(raw_token)
        else:
            claims = validate_dev_token(raw_token)
            if claims is None:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        role = normalize_role(claims.roles) if claims.roles else "Technician"
        await upsert_user(db, {
            "user_id": claims.user_id, "email": claims.email,
            "name": claims.name, "role": role, "last_login": datetime.utcnow(),
        })
        user = CurrentUser(user_id=claims.user_id, email=claims.email, name=claims.name, role=role)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user.require("download:read")

    path = get_local_file(machine_id, year, month, file_name)
    if not path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    media_type = "application/pdf"
    if file_name.endswith(".docx"):
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif file_name.endswith(".xlsx"):
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    return FileResponse(
        path=str(path),
        media_type=media_type,
        filename=file_name,
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
