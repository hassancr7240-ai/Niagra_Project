from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from app.core.storage_module import get_local_file
from app.dependencies import CurrentUserDep

router = APIRouter(prefix="/api/download", tags=["Download"])


@router.get("/{machine_id}/{year}/{month}/{file_name}")
async def download_pm_file(
    machine_id: str,
    year: str,
    month: str,
    file_name: str,
    user: CurrentUserDep,
) -> FileResponse:
    """Download a locally stored PM document."""
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
    )
