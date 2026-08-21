"""Environment-backed application settings."""

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.relevance import (
    DEFAULT_RELEVANCE_EXCLUDE_KEYWORDS,
    DEFAULT_RELEVANCE_INCLUDE_KEYWORDS,
)


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
    supplement_relevance_include_keywords: str = ",".join(
        DEFAULT_RELEVANCE_INCLUDE_KEYWORDS
    )
    supplement_relevance_exclude_keywords: str = ",".join(
        DEFAULT_RELEVANCE_EXCLUDE_KEYWORDS
    )
    target_min_monthly_spend_usd: float = Field(default=5_000, ge=0)
    target_max_monthly_spend_usd: float = Field(default=30_000, ge=0)
    spend_estimation_enabled: bool = True
    spend_target_min_usd: float = Field(default=5_000, ge=0)
    spend_target_max_usd: float = Field(default=30_000, ge=0)
    spend_cpm_uk_low_usd: float = Field(default=8.0, gt=0)
    spend_cpm_uk_high_usd: float = Field(default=18.0, gt=0)
    spend_cpm_europe_low_usd: float = Field(default=5.0, gt=0)
    spend_cpm_europe_high_usd: float = Field(default=18.0, gt=0)
    spend_cpm_usa_low_usd: float = Field(default=10.0, gt=0)
    spend_cpm_usa_high_usd: float = Field(default=25.0, gt=0)
    spend_cpm_canada_low_usd: float = Field(default=8.0, gt=0)
    spend_cpm_canada_high_usd: float = Field(default=20.0, gt=0)
    spend_reach_frequency_low: float = Field(default=1.0, gt=0)
    spend_reach_frequency_high: float = Field(default=3.0, gt=0)
    spend_activity_daily_low_usd: float = Field(default=10.0, gt=0)
    spend_activity_daily_high_usd: float = Field(default=50.0, gt=0)
    spend_min_observation_days: int = Field(default=7, ge=1)
    target_min_instagram_followers: int = Field(default=10_000, ge=0)
    target_max_instagram_followers: int = Field(default=100_000, ge=0)
    desirable_trustpilot_review_count: int = Field(default=300, ge=0)
    scan_interval_hours: int = Field(default=12, ge=1)
    provider_retry_attempts: int = Field(default=3, ge=1, le=10)
    provider_retry_min_wait_seconds: float = Field(default=1.0, ge=0)
    provider_retry_max_wait_seconds: float = Field(default=10.0, ge=0)

    database_url: str | None = None
    persist_scan_results: bool = False
    database_connect_timeout_seconds: int = Field(default=10, ge=1, le=60)

    meta_access_token: str | None = None
    meta_ad_provider: str | None = None
    meta_api_version: str = Field(default="v26.0", pattern=r"^v\d+\.\d+$")
    meta_request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    meta_max_pages_per_query: int = Field(default=100, ge=1, le=10_000)
    apify_api_token: str | None = None
    apify_actor_id: str = "solidcode/meta-ads-library-scraper"
    apify_max_results_per_query: int = Field(default=500, ge=1, le=50_000)
    apify_max_total_charge_usd_per_run: float = Field(default=0.02, gt=0)
    apify_include_advertiser_details: bool = True
    apify_monthly_budget_gbp: float = Field(default=30.0, gt=0)
    apify_budget_gbp_per_usd: float = Field(default=1.0, gt=0)
    apify_request_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    google_sheets_enabled: bool = False
    google_sheet_id: str | None = None
    google_sheet_tab: str = Field(default="Candidates", min_length=1, max_length=100)
    google_service_account_json: str | None = None
    instagram_provider: str | None = None
    instagram_api_key: str | None = None
    reviews_provider: str | None = None
    reviews_api_key: str | None = None

    @model_validator(mode="after")
    def validate_apify_charge_ceiling(self) -> Self:
        """Reject a single-run ceiling that already exceeds the monthly guard."""

        ceiling_gbp = (
            self.apify_max_total_charge_usd_per_run
            * self.apify_budget_gbp_per_usd
        )
        if ceiling_gbp > self.apify_monthly_budget_gbp:
            raise ValueError(
                "APIFY_MAX_TOTAL_CHARGE_USD_PER_RUN exceeds "
                "APIFY_MONTHLY_BUDGET_GBP after conversion"
            )
        pairs = (
            ("SPEND_TARGET", self.spend_target_min_usd, self.spend_target_max_usd),
            ("SPEND_CPM_UK", self.spend_cpm_uk_low_usd, self.spend_cpm_uk_high_usd),
            ("SPEND_CPM_EUROPE", self.spend_cpm_europe_low_usd, self.spend_cpm_europe_high_usd),
            ("SPEND_CPM_USA", self.spend_cpm_usa_low_usd, self.spend_cpm_usa_high_usd),
            ("SPEND_CPM_CANADA", self.spend_cpm_canada_low_usd, self.spend_cpm_canada_high_usd),
            ("SPEND_REACH_FREQUENCY", self.spend_reach_frequency_low, self.spend_reach_frequency_high),
            ("SPEND_ACTIVITY_DAILY", self.spend_activity_daily_low_usd, self.spend_activity_daily_high_usd),
        )
        for name, low, high in pairs:
            if low > high:
                raise ValueError(f"{name} low value cannot exceed its high value")
        return self

    @property
    def regions(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.scan_regions.split(",") if value.strip())

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(
            value.strip() for value in self.supplement_categories.split(",") if value.strip()
        )

    @property
    def relevance_include_keywords(self) -> tuple[str, ...]:
        return _comma_separated(self.supplement_relevance_include_keywords)

    @property
    def relevance_exclude_keywords(self) -> tuple[str, ...]:
        return _comma_separated(self.supplement_relevance_exclude_keywords)


@lru_cache
def get_settings() -> Settings:
    """Return a cached settings instance for application-wide use."""

    return Settings()


def _comma_separated(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
