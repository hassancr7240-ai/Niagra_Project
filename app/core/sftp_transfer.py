"""
SFTP file transfer to on-premises ERP system.

Handles uploading generated Excel/CSV files to the factory file server
at 10.30.225.118:220 for ERP ingestion.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import paramiko

logger = logging.getLogger(__name__)


class SFTPTransferError(Exception):
    """SFTP transfer failed."""
    pass


async def upload_file_to_sftp(
    local_file_path: Path,
    remote_filename: str,
    host: str,
    port: int,
    username: str,
    password: Optional[str] = None,
    private_key_path: Optional[str] = None,
    remote_dir: str = "/",
) -> bool:
    """
    Upload a file to SFTP server asynchronously.

    Args:
        local_file_path: Path to local file (e.g., /tmp/tasks.xlsx)
        remote_filename: Name on remote server (e.g., ALAL3_BOTTLE_CODER_PM.xlsx)
        host: SFTP server IP (e.g., 10.30.225.118)
        port: SFTP port (e.g., 220)
        username: SFTP username (e.g., pmwftp)
        password: SFTP password (from Key Vault)
        private_key_path: Alternative: SSH private key path
        remote_dir: Remote directory (e.g., /)

    Returns:
        True if successful, False otherwise.

    Raises:
        SFTPTransferError: If connection or upload fails.
    """
    def _upload():
        """Synchronous SFTP upload (wrapped in thread)."""
        try:
            transport = paramiko.Transport((host, port))

            # Authenticate
            if password:
                transport.connect(username=username, password=password)
            elif private_key_path:
                pkey = paramiko.RSAKey.from_private_key_file(private_key_path)
                transport.connect(username=username, pkey=pkey)
            else:
                raise SFTPTransferError("No password or private key provided")

            sftp = paramiko.SFTPClient.from_transport(transport)

            # Change to remote directory
            try:
                sftp.chdir(remote_dir)
            except IOError:
                logger.warning("Remote directory %s does not exist, using root", remote_dir)

            # Upload file
            remote_path = f"{remote_dir}/{remote_filename}" if remote_dir != "/" else f"/{remote_filename}"
            sftp.put(str(local_file_path), remote_path)

            logger.info("SFTP upload successful: %s → %s:%d%s",
                       local_file_path, host, port, remote_path)

            sftp.close()
            transport.close()
            return True

        except Exception as e:
            logger.error("SFTP upload failed: %s", e)
            raise SFTPTransferError(f"Failed to upload to {host}:{port}: {e}") from e

    # Run upload in thread pool to avoid blocking event loop
    try:
        return await asyncio.to_thread(_upload)
    except SFTPTransferError as e:
        logger.error("SFTP transfer failed: %s", e)
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
    if not config.ftp_host or not config.ftp_username:
        logger.info("SFTP not configured (ftp_host or ftp_username missing), skipping upload")
        return True  # Not an error, just not configured

    # Generate remote filename: MANUAL_ID_TASKS_YYYYMMDD.zip
    from datetime import datetime
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    remote_filename = f"{manual_id}_TASKS_{timestamp}.zip"

    return await upload_file_to_sftp(
        local_file_path=zip_file_path,
        remote_filename=remote_filename,
        host=config.ftp_host,
        port=config.ftp_port,
        username=config.ftp_username,
        password=config.ftp_password,
        private_key_path=config.ftp_key_path,
        remote_dir=config.ftp_remote_base_path,
    )
