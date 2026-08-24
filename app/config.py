from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""
    llm_providers_path: Path | None = None
    llm_provider_retry_count: int = Field(default=1, ge=0, le=3)
    llm_circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    llm_circuit_cooldown_seconds: float = Field(default=60.0, gt=0)
    llm_max_provider_attempts: int = Field(default=3, ge=1, le=10)
    sqlite_db_path: Path = Path(
        "../data/数据入库v_1.1_0722/query_ready_v2/zhangbei_energy_query_ready_v2.sqlite3"
    )
    ddl_directory: Path = Path(
        "../data/数据入库v_1.1_0722/query_ready_v2/ddl"
    )
    catalog_path: Path = Path("config/catalog.json")
    examples_path: Path = Path("config/examples.json")
    table_cards_path: Path = Path("config/table_cards.json")
    ddl_registry_path: Path = Path("config/ddl_registry.json")
    query_knowledge_path: Path = Path("config/query_knowledge.json")
    validation_cases_path: Path = Path("config/validation_cases.json")
    administrative_regions_path: Path = Path("config/administrative_regions.json")
    prompts_path: Path = Path("config/prompts.json")
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_trace_log_path: Path = Path("runtime/llm_trace.jsonl")
    enable_llm_trace: bool = Field(
        default=False,
        validation_alias="ENABLE_LLM_TRACE",
    )
    query_timeout_seconds: float = Field(default=10.0, gt=0)
    query_total_timeout_seconds: float = Field(default=240.0, gt=0)
    max_result_rows: int = Field(default=100, ge=1, le=1000)
    max_sql_modification_attempts: int = Field(default=1, ge=0, le=1)
    max_result_requery_attempts: int = Field(default=1, ge=0, le=1)
    audit_log_path: Path = Path("runtime/query_audit.jsonl")
    stage_timing_log_path: Path = Path("runtime/stage_timing.jsonl")
    platform_db_path: Path = Path("runtime/platform.sqlite3")
    query_diagnostics_enabled: bool = Field(
        default=False,
        validation_alias="ENABLE_QUERY_DIAGNOSTICS",
    )
    admin_api_key: str = Field(default="", validation_alias="ADMIN_API_KEY")
    viewer_api_key: str = Field(default="", validation_alias="VIEWER_API_KEY")
    deployment_mode: Literal["development", "production"] = Field(
        default="development", validation_alias="DEPLOYMENT_MODE"
    )
    otel_exporter_endpoint: str = Field(default="", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_service_name: str = Field(default="resources-agent", validation_alias="OTEL_SERVICE_NAME")
    conversation_ttl_seconds: float = Field(default=900.0, gt=0, le=86400)
    conversation_max_sessions: int = Field(default=1000, ge=1, le=100000)

    @property
    def llm_configured(self) -> bool:
        if self.llm_providers_path is not None:
            return True
        return bool(
            self.openai_base_url.strip()
            and self.openai_api_key.strip()
            and self.openai_model.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
