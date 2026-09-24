"""
FTP file transfer to on-premises ERP system.

Handles uploading generated Excel/CSV files to the factory file server
at 10.30.225.118:220 for ERP ingestion.
"""

import asyncio
import logging
from ftplib import FTP, all_errors
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class FTPTransferError(Exception):
    """FTP transfer failed."""
    pass


async def upload_file_to_ftp(
    local_file_path: Path,
    remote_filename: str,
    host: str,
    port: int,
    username: str,
    password: Optional[str] = None,
    remote_dir: str = "/",
) -> bool:
    """
    Upload a file to FTP server asynchronously.

    Args:
        local_file_path: Path to local file (e.g., /tmp/tasks.xlsx)
        remote_filename: Name on remote server (e.g., ALAL3_BOTTLE_CODER_PM.xlsx)
        host: FTP server IP (e.g., 10.30.225.118)
        port: FTP port (e.g., 220)
        username: FTP username (e.g., pmwftp)
        password: FTP password (from Key Vault)
        remote_dir: Remote directory (e.g., /)

    Returns:
        True if successful, False otherwise.

    Raises:
        FTPTransferError: If connection or upload fails.
    """
    def _upload():
        """Synchronous FTP upload (wrapped in thread)."""
        try:
            ftp = FTP()
            ftp.connect(host, port, timeout=30)
            ftp.login(username, password or "")

            # Change to remote directory
            try:
                if remote_dir and remote_dir != "/":
                    ftp.cwd(remote_dir)
            except all_errors as e:
                logger.warning("[FTP-CWD] Failed to change to directory %s: %s, using root", remote_dir, e)

            # Upload file
            with open(local_file_path, "rb") as f:
                ftp.storbinary(f"STOR {remote_filename}", f)

            logger.info("[FTP-UPLOAD-SUCCESS] %s → %s:%d/%s",
                       local_file_path, host, port, remote_filename)

            ftp.quit()
            return True

        except Exception as e:
            logger.error("[FTP-UPLOAD-FAILED] %s", e)
            raise FTPTransferError(f"Failed to upload to {host}:{port}: {e}") from e

    # Run upload in thread pool to avoid blocking event loop
    try:
        return await asyncio.to_thread(_upload)
    except FTPTransferError as e:
        logger.error("[FTP-TRANSFER-ERROR] %s", e)
        return False


async def upload_extracted_tasks(
    manual_id: str,
    zip_file_path: Path,
    config,
) -> bool:
    """
    Upload generated Excel ZIP to SFTP server.

    Args:
        manual_id: Upload ID (e.g., abc123)
        zip_file_path: Path to generated ZIP file
        config: Settings with FTP credentials

    Returns:
        True if upload successful, False otherwise.
    """
    logger.critical("[%s] [FTP-START] upload_extracted_tasks called", manual_id)

    if not config.pmw_file_transfer_host or not config.pmw_file_transfer_username:
        logger.info("[%s] [FTP-SKIP] FTP not configured, skipping upload", manual_id)
        return True

    logger.info("[%s] [FTP-CONFIG] host=%s port=%s user=%s", manual_id,
                config.pmw_file_transfer_host, config.pmw_file_transfer_port,
                config.pmw_file_transfer_username)

    from datetime import datetime
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    remote_filename = f"{manual_id}_TASKS_{timestamp}.zip"
    logger.info("[%s] [FTP-FILENAME] %s", manual_id, remote_filename)

    try:
        result = await upload_file_to_ftp(
            local_file_path=zip_file_path,
            remote_filename=remote_filename,
            host=config.pmw_file_transfer_host,
            port=int(config.pmw_file_transfer_port),
            username=config.pmw_file_transfer_username,
            password=config.pmw_file_transfer_password,
            remote_dir=config.pmw_file_transfer_incoming_dir,
        )
        logger.critical("[%s] [FTP-RESULT] %s", manual_id, "SUCCESS" if result else "FAILED")
        return result
    except Exception as e:
        logger.error("[%s] [FTP-ERROR] %s", manual_id, e)
        return False
