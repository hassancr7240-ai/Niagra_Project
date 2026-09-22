"""
ERP REST API integration for work definition creation.

Calls factory ERP system to create maintenance work definitions
after PM tasks are extracted.
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class ERPIntegrationError(Exception):
    """ERP API call failed."""
    pass


async def create_work_definition(
    work_definition_name: str,
    org_code: str,
    email_id: str,
    api_url: str,
    api_key: str,
    timeout: int = 30,
) -> bool:
    """
    Create a work definition in the ERP system.

    Args:
        work_definition_name: Name (e.g., "TETRAPAK-ASEPTIC-L3 PM")
        org_code: Organization code (e.g., "All")
        email_id: User email (e.g., "user@example.com")
        api_url: ERP API endpoint
        api_key: API authentication key (x-api-key header)
        timeout: Request timeout in seconds

    Returns:
        True if successful, False otherwise.

    Raises:
        ERPIntegrationError: If API call fails.
    """
    payload = {
        "workDefinitionName": work_definition_name,
        "orgCode": org_code,
        "emailId": email_id,
    }

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                api_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )

            if response.status_code in (200, 201, 202):
                logger.info(
                    "ERP work definition created: %s (org=%s)",
                    work_definition_name,
                    org_code,
                )
                return True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error("ERP API call failed: %s", error_msg)
                raise ERPIntegrationError(error_msg)

    except httpx.TimeoutException as e:
        logger.error("ERP API timeout: %s", e)
        raise ERPIntegrationError(f"ERP API timeout: {e}") from e
    except Exception as e:
        logger.error("ERP API call failed: %s", e)
        raise ERPIntegrationError(f"Failed to create work definition: {e}") from e


async def send_to_erp(
    manual_id: str,
    machine_id: str,
    email_id: str,
    config,
) -> bool:
    """
    Send extracted PM tasks to ERP system.

    Args:
        manual_id: Upload ID (e.g., abc123)
        machine_id: Machine ID (e.g., "TETRAPAK-ASEPTIC-L3")
        email_id: Engineer email
        config: Settings with ERP credentials

    Returns:
        True if successful, False otherwise.
    """
    if not config.erp_api_key:
        logger.info("ERP API key not configured, skipping work definition creation")
        return True

    work_definition_name = f"{machine_id} PM - {manual_id}"
    org_code = config.erp_org_code or "All"

    try:
        return await create_work_definition(
            work_definition_name=work_definition_name,
            org_code=org_code,
            email_id=email_id,
            api_url=config.erp_api_url,
            api_key=config.erp_api_key,
        )
    except ERPIntegrationError as e:
        logger.error("Failed to send to ERP: %s", e)
        return False
