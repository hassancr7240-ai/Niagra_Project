from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_SECRETS_LOADED = False


async def load_secrets_from_key_vault() -> bool:
    """
    Load secrets from Azure Key Vault into environment variables at startup.
    Uses Managed Identity (DefaultAzureCredential) — no passwords in code.
    Called once during FastAPI lifespan startup.
    Returns True if secrets loaded, False if Key Vault not configured.
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.azure_key_vault_url:
        logger.info("Azure Key Vault URL not configured — using environment variables")
        return False

    try:
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient

        # Map of Key Vault secret names → environment variable names
        SECRET_MAP = {
            "pm-azure-ad-client-secret":            "AZURE_AD_CLIENT_SECRET",
            "pm-azure-storage-connection-string":   "AZURE_STORAGE_CONNECTION_STRING",
            "pm-database-url":                      "DATABASE_URL",
            "pm-watsonx-api-key":                   "WATSONX_API_KEY",
            "pm-azure-search-api-key":              "AZURE_SEARCH_API_KEY",
            "pm-azure-doc-intelligence-key":        "AZURE_DOC_INTELLIGENCE_KEY",
            "pm-app-secret-key":                    "APP_SECRET_KEY",
            "pm-ftp-key-path":                      "FTP_KEY_PATH",
        }

        async with DefaultAzureCredential() as credential:
            async with SecretClient(
                vault_url=settings.azure_key_vault_url,
                credential=credential,
            ) as client:
                loaded = 0
                for secret_name, env_var in SECRET_MAP.items():
                    try:
                        secret = await client.get_secret(secret_name)
                        if secret.value:
                            os.environ[env_var] = secret.value
                            loaded += 1
                    except Exception as e:
                        logger.debug("Secret '%s' not found in Key Vault: %s", secret_name, e)

        logger.info("Loaded %d secrets from Azure Key Vault", loaded)
        global _SECRETS_LOADED
        _SECRETS_LOADED = True
        return True

    except Exception as exc:
        logger.warning(
            "Azure Key Vault load failed (will use env vars): %s", exc
        )
        return False
