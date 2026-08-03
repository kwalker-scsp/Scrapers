"""Runtime configuration, loaded from the environment (and .env if present)."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite+pysqlite:///./data/timeline.db"

    ingest_api_key: str = "dev-ingest-key-change-me"
    review_api_key: str = "dev-review-key-change-me"

    app_env: str = "development"
    cors_origins: str = "*"
    require_key_for_reads: bool = False

    @property
    def cors_origin_list(self) -> List[str]:
        raw = self.cors_origins.strip()
        if raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def using_default_keys(self) -> bool:
        return "change-me" in self.ingest_api_key or "change-me" in self.review_api_key


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
