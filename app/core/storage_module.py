from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.utils.security import is_safe_path

logger = logging.getLogger(__name__)
settings = get_settings()


class StorageError(Exception):
    pass


def _blob_path(machine_id: str, file_name: str) -> str:
    """Azure Blob path: pm-docs/machine/year/month/filename.pdf"""
    now = datetime.utcnow()
    return f"pm-docs/{machine_id}/{now.year}/{now.month:02d}/{file_name}"


def _local_path(machine_id: str, file_name: str) -> Path:
    now = datetime.utcnow()
    p = settings.output_path / machine_id / str(now.year) / f"{now.month:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return p / file_name


async def upload_file(
    local_file: Path,
    machine_id: str,
    file_name: str,
    target: Optional[str] = None,
) -> tuple[str, str]:
    """
    Upload a generated PM file to the configured storage backend.
    Returns (storage_target_used, download_url).
    """
    target = target or settings.default_storage_target

    if target == "azure":
        return await _upload_azure(local_file, machine_id, file_name)
    elif target == "ftp":
        return await _upload_ftp(local_file, machine_id, file_name)
    else:
        return _upload_local(local_file, machine_id, file_name)


async def _upload_azure(
    local_file: Path, machine_id: str, file_name: str
) -> tuple[str, str]:
    try:
        from azure.storage.blob import (
            BlobServiceClient,
            BlobSasPermissions,
            generate_blob_sas,
        )
        from azure.identity import DefaultAzureCredential

        blob_path = _blob_path(machine_id, file_name)

        if settings.azure_storage_connection_string:
            client = BlobServiceClient.from_connection_string(
                settings.azure_storage_connection_string
            )
        else:
            credential = DefaultAzureCredential()
            client = BlobServiceClient(
                account_url=f"https://{settings.azure_storage_account_name}.blob.core.windows.net",
                credential=credential,
            )

        container_client = client.get_container_client(settings.azure_storage_container_name)

        with open(local_file, "rb") as f:
            container_client.upload_blob(
                name=blob_path,
                data=f,
                overwrite=True,
                content_settings=__import__(
                    "azure.storage.blob", fromlist=["ContentSettings"]
                ).ContentSettings(
                    content_type="application/pdf",
                    content_disposition=f'attachment; filename="{file_name}"',
                ),
            )

        # Generate time-limited SAS URL
        expiry = datetime.now(timezone.utc) + timedelta(hours=settings.download_link_expiry_hours)
        sas = generate_blob_sas(
            account_name=settings.azure_storage_account_name or client.account_name,
            container_name=settings.azure_storage_container_name,
            blob_name=blob_path,
            account_key=client.credential.account_key if hasattr(client.credential, "account_key") else None,
            permission=BlobSasPermissions(read=True),
            expiry=expiry,
        )
        url = f"https://{settings.azure_storage_account_name}.blob.core.windows.net/{settings.azure_storage_container_name}/{blob_path}?{sas}"
        logger.info("Uploaded to Azure Blob: %s", blob_path)
        return "azure", url

    except Exception as exc:
        logger.error("Azure upload failed: %s — falling back to local", exc)
        return _upload_local(local_file, machine_id, file_name)


async def _upload_ftp(
    local_file: Path, machine_id: str, file_name: str
) -> tuple[str, str]:
    try:
        import paramiko

        now = datetime.utcnow()
        remote_dir = f"{settings.ftp_remote_base_path}/{machine_id}/{now.year}/{now.month:02d}"
        remote_path = f"{remote_dir}/{file_name}"

        transport = paramiko.Transport((settings.ftp_host, settings.ftp_port))
        if settings.ftp_key_path:
            key = paramiko.RSAKey.from_private_key_file(settings.ftp_key_path)
            transport.connect(username=settings.ftp_username, pkey=key)
        else:
            raise StorageError("FTP key path not configured")

        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            sftp.makedirs(remote_dir)
        except Exception:
            pass
        sftp.put(str(local_file), remote_path)
        sftp.close()
        transport.close()

        url = f"sftp://{settings.ftp_host}{remote_path}"
        logger.info("Uploaded to SFTP: %s", remote_path)
        return "ftp", url

    except Exception as exc:
        logger.error("SFTP upload failed: %s — falling back to local", exc)
        return _upload_local(local_file, machine_id, file_name)


def _upload_local(
    local_file: Path, machine_id: str, file_name: str
) -> tuple[str, str]:
    dest = _local_path(machine_id, file_name)
    # Verify path is safe (no traversal)
    if not is_safe_path(settings.output_path, dest):
        raise StorageError("Path traversal detected")

    if local_file != dest:
        shutil.copy2(str(local_file), str(dest))

    # Path structure: output/pm-docs/<machine>/<year>/<month>/<file>
    month = dest.parent.name        # e.g. "05"
    year = dest.parent.parent.name  # e.g. "2026"
    url = f"/api/download/{machine_id}/{year}/{month}/{file_name}"
    logger.info("Stored locally: %s", dest)
    return "local", url


def get_local_file(machine_id: str, year: str, month: str, file_name: str) -> Optional[Path]:
    safe_name = Path(file_name).name
    p = settings.output_path / machine_id / year / month / safe_name
    if not is_safe_path(settings.output_path, p):
        return None
    if p.exists():
        return p
    return None
