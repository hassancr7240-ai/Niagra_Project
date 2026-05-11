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

    # ── FTP / SFTP ────────────────────────────────────────────────────────────
    ftp_host: Optional[str] = None
    ftp_port: int = 22
    ftp_username: Optional[str] = None
    ftp_key_path: Optional[str] = None
    ftp_remote_base_path: str = "/pm-docs"

    # ── AI Provider ───────────────────────────────────────────────────────────
    ai_provider: str = "openai"  # openai | watsonx
    openai_api_key: Optional[str] = None
    openai_model_generation: str = "gpt-4o"
    openai_model_classification: str = "gpt-4o-mini"
    openai_embedding_model: str = "text-embedding-3-large"
    openai_embedding_dims: int = 3072

    # IBM watsonx.ai — exact model IDs from the Data Flow Diagram
    # Classification:  granite-13b-instruct-v2  → detect machine type + analyse PM patterns
    # Embedding:       bge-large-en-v1.5        → 1024 dims → Azure AI Search
    # Generation:      granite-3b-code-instruct → extract structured JSON tasks
    # Analytics:       granite-13b-instruct-v2  → predict next PM due, analyse overdue patterns
    watsonx_api_key: Optional[str] = None
    watsonx_project_id: Optional[str] = None
    watsonx_url: str = "https://us-south.ml.cloud.ibm.com"
    watsonx_model_generation: str = "ibm/granite-3b-code-instruct"
    watsonx_model_classification: str = "ibm/granite-13b-instruct-v2"
    watsonx_model_analytics: str = "ibm/granite-13b-instruct-v2"
    watsonx_embedding_model: str = "ibm/slate-125m-english-rtrvr"
    watsonx_embedding_dims: int = 1024

    # ── Azure AI Search ───────────────────────────────────────────────────────
    azure_search_endpoint: Optional[str] = None
    azure_search_api_key: Optional[str] = None
    azure_search_index_name: str = "pm-manuals"

    # ── RAG Pipeline ─────────────────────────────────────────────────────────
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 103
    rag_top_k: int = 10

    # ── Storage ───────────────────────────────────────────────────────────────
    default_storage_target: str = "local"  # azure | ftp | local
    local_storage_path: str = "./output/pm-docs"

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    rate_limit_per_minute: int = 100

    # ── App Insights ─────────────────────────────────────────────────────────
    applicationinsights_connection_string: Optional[str] = None

    # ── Dev API Key (when Azure AD tenant is not configured) ──────────────────
    dev_api_key: str = "dev-secret-key-change-in-prod"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
