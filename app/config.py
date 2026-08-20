"""Environment-backed application settings."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CATEGORIES = (
    "vitamins,minerals,supplements,protein,whey,creatine,pre-workout,collagen,"
    "gummies,electrolytes,greens,probiotics,omega 3,magnesium,pet supplements,"
    "dog supplements,cat supplements"
)


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "meta-supplement-tracker"
    app_env: str = "development"
    log_level: str = "INFO"
    port: int = Field(default=8000, ge=1, le=65535)

    scan_regions: str = "UK,Europe,USA,Canada"
    supplement_categories: str = DEFAULT_CATEGORIES
    target_min_monthly_spend_usd: float = Field(default=5_000, ge=0)
    target_max_monthly_spend_usd: float = Field(default=30_000, ge=0)
    target_min_instagram_followers: int = Field(default=10_000, ge=0)
    target_max_instagram_followers: int = Field(default=100_000, ge=0)
    desirable_trustpilot_review_count: int = Field(default=300, ge=0)
    scan_interval_hours: int = Field(default=12, ge=1)
    provider_retry_attempts: int = Field(default=3, ge=1, le=10)
    provider_retry_min_wait_seconds: float = Field(default=1.0, ge=0)
    provider_retry_max_wait_seconds: float = Field(default=10.0, ge=0)

    meta_access_token: str | None = None
    meta_ad_provider: str | None = None
    meta_api_version: str = Field(default="v26.0", pattern=r"^v\d+\.\d+$")
    meta_request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    meta_max_pages_per_query: int = Field(default=100, ge=1, le=10_000)
    google_service_account_json: str | None = None
    google_doc_id: str | None = None
    instagram_provider: str | None = None
    instagram_api_key: str | None = None
    reviews_provider: str | None = None
    reviews_api_key: str | None = None

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.scan_regions.split(",") if value.strip())

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(
            value.strip() for value in self.supplement_categories.split(",") if value.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance for application-wide use."""

    return Settings()
