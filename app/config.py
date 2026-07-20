from functools import lru_cache
from pathlib import Path

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
    sqlite_db_path: Path = Path(
        "../data/数据入库_v1.0.1_2026.07.17/data_1_all/zhangbei_energy_data_data1.sqlite3"
    )
    query_timeout_seconds: float = Field(default=10.0, gt=0)
    max_result_rows: int = Field(default=100, ge=1, le=1000)
    audit_log_path: Path = Path("runtime/query_audit.jsonl")

    @property
    def llm_configured(self) -> bool:
        return bool(
            self.openai_base_url.strip()
            and self.openai_api_key.strip()
            and self.openai_model.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
