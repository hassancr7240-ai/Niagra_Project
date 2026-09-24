from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ────────────────────────────────────────────────────────
    app_env: str = "development"
    app_secret_key: str = "change-me-32-char-secret-key-dev!"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = False
    app_title: str = "PM Automation System"
    app_version: str = "1.0.0"

    # ── Azure AD Authentication ────────────────────────────────────────────
    azure_ad_tenant_id: Optional[str] = None
    azure_ad_client_id: Optional[str] = None
    azure_ad_client_secret: Optional[str] = None
    azure_ad_authority: str = "https://login.microsoftonline.com/"

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: Optional[str] = None  # blank → SQLite local dev

    # ── Azure Blob Storage ────────────────────────────────────────────────────
    azure_storage_account_name: Optional[str] = None
    azure_storage_container_name: str = "pm-docs"
    azure_storage_connection_string: Optional[str] = None
    download_link_expiry_hours: int = 8

    # ── Azure Key Vault ───────────────────────────────────────────────────────
    azure_key_vault_url: Optional[str] = None

    # ── FTP / SFTP (File Transfer) ────────────────────────────────────────────
    # Upload extracted Excel/CSV files to on-premises ERP system
    # Config from Azure App Service settings
    pmw_file_transfer_host: Optional[str] = None              # 10.30.225.118
    pmw_file_transfer_port: int = 22                          # 220
    pmw_file_transfer_username: Optional[str] = None          # pmwftp
    pmw_file_transfer_password: Optional[str] = None          # from Key Vault: ADR-PMW-FTP-PWD
    pmw_file_transfer_incoming_dir: str = "/"                 # /
    pmw_file_transfer_pool_size: int = 1                      # 1 connection

    # ── ERP REST API Integration ────────────────────────────────────────────────
    # Call ERP API to create work definitions after PM extraction
    erp_api_url: str = "https://apim-dev-intg.azure-api.net/api/dev/sp/v1/pmw/process-work-definition"
    erp_api_key: Optional[str] = None           # 24318647205f417492d88f3740d3b17d
    erp_org_code: str = "All"                   # default org code

    # ── IBM watsonx.ai ────────────────────────────────────────────────────────
    # AI provider — IBM watsonx.ai cloud (us-south).
    # Task extraction  : meta/llama-3-3-70b-instruct  (set via WATSONX_MODEL_GENERATION)
    # Classification   : meta/llama-3-3-70b-instruct  (set via WATSONX_MODEL_CLASSIFICATION)
    # Analytics        : meta/llama-3-3-70b-instruct  (set via WATSONX_MODEL_ANALYTICS)
    # Embedding        : ibm/slate-125m-english-rtrvr-v2  (1024 dims — still available)
    # Granite instruct models were deprecated by IBM; Llama 3.3 70B confirmed available.
    watsonx_api_key: Optional[str] = None
    watsonx_project_id: Optional[str] = None
    watsonx_url: str = "https://us-south.ml.cloud.ibm.com"
    watsonx_model_generation: str = "meta-llama/llama-3-3-70b-instruct"
    watsonx_model_classification: str = "meta-llama/llama-3-3-70b-instruct"
    watsonx_model_analytics: str = "meta-llama/llama-3-3-70b-instruct"
    watsonx_embedding_model: str = "ibm/slate-125m-english-rtrvr-v2"
    watsonx_embedding_dims: int = 1024

    # ── Ollama (local AI fallback) ────────────────────────────────────────────
    # Set OLLAMA_URL when an Ollama instance is available (e.g. http://<azure-vm-ip>:11434)
    # Automatically used when IBM watsonx.ai returns 403 or is unreachable.
    ollama_url: str = ""
    ollama_model: str = "llama3.2:3b"
    ollama_embedding_model: str = "nomic-embed-text"

    # ── Azure Document Intelligence ───────────────────────────────────────────
    azure_doc_intelligence_endpoint: Optional[str] = None
    azure_doc_intelligence_key: Optional[str] = None
    azure_doc_intelligence_model: str = "prebuilt-layout"

    # ── Azure AI Search ───────────────────────────────────────────────────────
    azure_search_endpoint: Optional[str] = None
    azure_search_api_key: Optional[str] = None
    azure_search_index_name: str = "pm-manuals"

    # ── RAG Pipeline ─────────────────────────────────────────────────────────
    # Smart chunking (structure-aware — primary approach)
    rag_max_section_words: int = 800   # max words per section chunk before paragraph split
    rag_min_section_words: int = 20    # sections smaller than this are merged into next
    rag_top_k: int = 10                # top-K chunks retrieved for AI extraction
    # Sliding-window fallback (used only when structure detection finds nothing)
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 103

    # ── Storage ───────────────────────────────────────────────────────────────
    default_storage_target: str = "local"  # azure | ftp | local
    local_storage_path: str = "./output/pm-docs"

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_per_minute: int = 100

    # ── App Insights ─────────────────────────────────────────────────────────
    applicationinsights_connection_string: Optional[str] = None

    # ── Dev API Key (when Azure AD tenant is not configured) ──────────────────
    dev_api_key: str = "dev-secret-key-change-in-prod"

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed origins in production.
    # Example: https://pm-automation.azurewebsites.net,https://pm.yourcompany.com
    allowed_origins: str = ""

    # ── Session ───────────────────────────────────────────────────────────────
    session_timeout_minutes: int = 30

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    @property
    def use_azure_ad(self) -> bool:
        return bool(self.azure_ad_tenant_id and self.azure_ad_client_id)

    @property
    def use_azure_storage(self) -> bool:
        return bool(
            self.azure_storage_connection_string or self.azure_storage_account_name
        )

    @property
    def sqlite_path(self) -> str:
        db_dir = Path("./data")
        db_dir.mkdir(exist_ok=True)
        return f"sqlite:///{db_dir}/pm_automation.db"

    @property
    def effective_database_url(self) -> str:
        return self.database_url or self.sqlite_path

    @property
    def pm_library_path(self) -> Path:
        return Path(__file__).parent.parent / "data" / "pm_library.json"

    @property
    def output_path(self) -> Path:
        p = Path(self.local_storage_path)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def jwt_algorithm(self) -> str:
        return "RS256" if self.use_azure_ad else "HS256"

    def resolve_secret(self, value: Optional[str]) -> Optional[str]:
        """
        Resolve Azure Key Vault references in config values.
        Format: @Microsoft.KeyVault(SecretUri=https://vault-name.vault.azure.net/secrets/secret-name/)
        """
        if not value or not value.startswith("@Microsoft.KeyVault"):
            return value

        import logging
        log = logging.getLogger(__name__)

        try:
            # Extract SecretUri from reference
            start = value.find("SecretUri=") + len("SecretUri=")
            end = value.find(")", start)
            if start == len("SecretUri=") - 1 or end == -1:
                log.error("[CONFIG-KEYVAULT] Invalid Key Vault reference format: %s", value[:50])
                return None

            secret_uri = value[start:end]
            log.info("[CONFIG-KEYVAULT] Attempting to resolve secret from: %s", secret_uri[:80])

            # Use Azure SDK to fetch secret
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient

                # Extract vault URL from secret URI (e.g., https://vault.vault.azure.net/secrets/name/)
                vault_url = secret_uri.split("/secrets/")[0]
                log.info("[CONFIG-KEYVAULT] Connecting to vault: %s", vault_url)

                credential = DefaultAzureCredential()
                client = SecretClient(vault_url=vault_url, credential=credential)

                # Extract secret name from URI
                secret_name = secret_uri.split("/secrets/")[1].rstrip("/")
                log.info("[CONFIG-KEYVAULT] Fetching secret: %s", secret_name)

                secret = client.get_secret(secret_name)
                log.critical("[CONFIG-KEYVAULT] Successfully resolved secret: %s", secret_name)
                return secret.value

            except Exception as e:
                log.error("[CONFIG-KEYVAULT] Failed to fetch from Key Vault: %s", str(e)[:200])
                return None

        except Exception as e:
            log.error("[CONFIG-KEYVAULT] Error resolving reference: %s", str(e)[:200])
            return None

    def model_post_init(self, __context) -> None:
        """Post-init hook to resolve Key Vault references after settings are loaded."""
        import logging
        log = logging.getLogger(__name__)

        # Resolve Key Vault references for sensitive config values
        log.info("[CONFIG-POST-INIT] Resolving Key Vault references")

        if self.pmw_file_transfer_password and self.pmw_file_transfer_password.startswith("@Microsoft"):
            log.info("[CONFIG-RESOLVE] Resolving FTP password from Key Vault")
            resolved = self.resolve_secret(self.pmw_file_transfer_password)
            if resolved:
                self.pmw_file_transfer_password = resolved
                log.critical("[CONFIG-RESOLVE] FTP password resolved successfully")
            else:
                log.error("[CONFIG-RESOLVE] Failed to resolve FTP password - using reference as-is (will fail at runtime)")


@lru_cache
def get_settings() -> Settings:
    return Settings()
