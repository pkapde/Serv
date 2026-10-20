from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["demo", "production"] = "demo"
    model_path: Path = Path("examples/cx.ossie.yaml")
    database_url: str = "sqlite:///var/service.db"
    demo_database_path: Path = Path("var/warehouse.duckdb")
    resolver_mode: Literal["fixture", "llm"] = "fixture"
    execution_mode: Literal["demo", "live"] = "demo"
    default_warehouse: Literal["snowflake", "teradata"] = "snowflake"
    api_tokens: dict[str, str] = Field(default_factory=lambda: {"local-demo-token": "demo-user"})
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1:*", "localhost:*", "testserver"])
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:*", "http://localhost:*"])
    row_cap: int = Field(default=5000, ge=1, le=50000)
    query_timeout_seconds: int = Field(default=60, ge=1, le=600)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    model_context_fraction: float = Field(default=0.5, gt=0, le=1)
    llm_context_window: int = Field(default=128000, ge=1024)
    llm_base_url: str = ""
    llm_provider: Literal["openai_compatible", "azure_openai"] = "openai_compatible"
    llm_api_version: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: int = Field(default=30, ge=1, le=120)
    snowflake_account: str = ""
    snowflake_user: str = ""
    snowflake_password: str = ""
    snowflake_warehouse: str = ""
    snowflake_database: str = ""
    snowflake_schema: str = ""
    snowflake_role: str = ""
    teradata_host: str = ""
    teradata_user: str = ""
    teradata_password: str = ""
    teradata_database: str = ""

    @model_validator(mode="after")
    def production_requirements(self):
        if not self.api_tokens or any(not token or not caller for token, caller in self.api_tokens.items()):
            raise ValueError("API_TOKENS must contain nonempty token-to-caller mappings")
        if self.resolver_mode == "llm" and not all((self.llm_base_url, self.llm_api_key, self.llm_model)):
            raise ValueError("LLM mode requires LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
        if self.resolver_mode == "llm" and self.llm_provider == "azure_openai":
            endpoint = urlsplit(self.llm_base_url)
            if (
                endpoint.scheme not in ("https", "http")
                or not endpoint.netloc
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("Azure LLM_BASE_URL must be an HTTP(S) base URL without query or fragment")
            if endpoint.path.rstrip("/") not in ("", "/openai/v1"):
                raise ValueError("Azure base URL must be the resource root or end with /openai/v1")
            if not endpoint.path.rstrip("/") and not self.llm_api_version:
                raise ValueError("Azure resource-root endpoints require LLM_API_VERSION")
        if self.app_env == "production":
            if self.resolver_mode != "llm" or self.execution_mode != "live":
                raise ValueError("Production requires a real resolver and live execution")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
            if "local-demo-token" in self.api_tokens:
                raise ValueError("Replace the demo authentication token before production")
            if not self.llm_base_url.startswith("https://"):
                raise ValueError("Production LLM endpoint must use HTTPS")
        return self
