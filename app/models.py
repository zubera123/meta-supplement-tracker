"""Domain models shared across providers, jobs, and scoring."""

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, HttpUrl, JsonValue, model_validator


class Region(StrEnum):
    UK = "UK"
    EUROPE = "Europe"
    USA = "USA"
    CANADA = "Canada"


class Brand(BaseModel):
    """A supplement brand identified by a source provider."""

    name: str = Field(min_length=1)
    website: HttpUrl | None = None
    instagram_handle: str | None = None
    source_id: str | None = None


class MetaAdDetails(BaseModel):
    """Provider-normalized fields for one real Meta Ad Library result."""

    ad_id: str = Field(min_length=1)
    page_id: str = Field(min_length=1)
    page_name: str = Field(min_length=1)
    ad_creation_time: datetime | None = None
    ad_delivery_start_time: datetime | None = None
    ad_delivery_stop_time: datetime | None = None
    ad_status: str | None = None
    ad_library_url: str | None = None
    ad_snapshot_url: str | None = None
    landing_page_url: str | None = None
    landing_page_domain: str | None = None
    cta_headline: str | None = None
    cta_description: str | None = None
    cta_text: str | None = None
    cta_type: str | None = None
    advertiser_page_url: str | None = None
    page_profile_picture_url: str | None = None
    advertiser_country: str | None = None
    facebook_page_category: str | None = None
    facebook_page_likes: int | None = Field(default=None, ge=0)
    facebook_page_verified: bool | None = None
    facebook_page_about: str | None = None
    instagram_handle: str | None = None
    instagram_followers: int | None = Field(default=None, ge=0)
    creative_bodies: list[str] = Field(default_factory=list)
    creative_link_captions: list[str] = Field(default_factory=list)
    creative_link_descriptions: list[str] = Field(default_factory=list)
    creative_link_titles: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    eu_total_reach: int | None = Field(default=None, ge=0)
    total_reach_by_location: JsonValue | None = None
    age_country_gender_reach_breakdown: JsonValue | None = None
    target_ages: list[str] = Field(default_factory=list)
    target_gender: str | None = None
    target_locations: JsonValue | None = None
    beneficiary_payers: JsonValue | None = None
    declared_spend: str | None = None
    currency: str | None = None
    impressions: JsonValue | None = None
    reach_estimate: str | None = None
    estimated_audience_size: int | None = Field(default=None, ge=0)
    regions_reached: JsonValue | None = None
    demographics: JsonValue | None = None
    source: str | None = None
    source_query: str | None = None
    matched_countries: list[str] = Field(default_factory=list)
    matched_regions: list[Region] = Field(default_factory=list)


class SocialStats(BaseModel):
    """Normalized social audience data."""

    instagram_followers: int | None = Field(default=None, ge=0)
    instagram_handle: str | None = None
    instagram_profile_url: HttpUrl | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SpendEstimate(BaseModel):
    """A bounded estimate with its evidence and explicit assumptions."""

    low_usd: float | None = Field(default=None, ge=0)
    high_usd: float | None = Field(default=None, ge=0)
    method: str = Field(min_length=1, max_length=50)
    source: str = Field(min_length=1, max_length=50)
    confidence: str = Field(pattern=r"^(very_low|low|medium|high|unknown)$")
    observed_inputs: dict[str, JsonValue] = Field(default_factory=dict)
    assumptions: dict[str, JsonValue] = Field(default_factory=dict)
    target_match: bool | None = None

    @model_validator(mode="after")
    def validate_evidence_semantics(self) -> Self:
        expected_confidence = {
            "impressions_cpm": "medium",
            "reach_cpm": "low",
            "activity_model": "very_low",
            "unknown": "unknown",
        }.get(self.method)
        if expected_confidence is not None and self.confidence != expected_confidence:
            raise ValueError(
                f"{self.method} estimates require {expected_confidence} confidence"
            )
        if self.method in {"activity_model", "unknown"} and self.target_match is not None:
            raise ValueError(
                f"{self.method} estimates cannot make a spend-target decision"
            )
        return self


class SpendHistory(BaseModel):
    """Prior activity observations used without triggering provider calls."""

    observation_count: int = Field(default=0, ge=0)
    active_ad_counts: list[int] = Field(default_factory=list)


class AdRecord(BaseModel):
    """Provider-normalized, advertiser-level advertising data."""

    brand: Brand
    region: Region
    regions: list[Region] = Field(default_factory=list)
    estimated_monthly_spend_usd: float | None = Field(default=None, ge=0)
    active_ad_count: int | None = Field(default=None, ge=0)
    oldest_active_ad: datetime | None = None
    newest_active_ad: datetime | None = None
    ads: list[MetaAdDetails] = Field(default_factory=list)
    social_stats: SocialStats | None = None
    spend_estimate: SpendEstimate | None = None
    review_enrichment: "ReviewEnrichmentResult | None" = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider_metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )

class ReviewStats(BaseModel):
    """Normalized review data from an optional reviews provider."""

    source: str = Field(min_length=1)
    review_count: int = Field(ge=0)
    rating: float | None = Field(default=None, ge=0, le=5)
    trust_score: float | None = Field(default=None, ge=0, le=5)
    star_score: float | None = Field(default=None, ge=0, le=5)
    business_unit_id: str | None = None
    matched_domain: str | None = None
    desirable: bool | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewEnrichmentResult(BaseModel):
    """Review lookup outcome, including unknown/error reasons for persistence."""

    status: str = Field(
        pattern=r"^(matched|cached|unavailable|error|skipped|disabled)$"
    )
    stats: ReviewStats | None = None
    reason: str = Field(min_length=1, max_length=2000)
    attempted_domain: str | None = None
    refreshed_at: datetime | None = None


class ReviewCache(BaseModel):
    """Persisted Trustpilot identity and latest values for refresh decisions."""

    business_unit_id: str | None = None
    matched_domain: str | None = None
    last_refreshed_at: datetime | None = None
    latest_stats: ReviewStats | None = None


class BrandCandidate(BaseModel):
    """A fully or partially enriched brand ready for evaluation."""

    brand: Brand
    ad_record: AdRecord
    social_stats: SocialStats | None = None
    review_stats: ReviewStats | None = None
    qualifies: bool = False
    score: float = Field(default=0, ge=0, le=100)
    evaluation_reasons: list[str] = Field(default_factory=list)


class SheetCandidate(BaseModel):
    """A qualifying advertiser prepared from PostgreSQL for Sheet output."""

    company_id: int = Field(gt=0)
    first_seen: date
    brand: str = Field(min_length=1)
    region: str = Field(min_length=1)
    instagram_username: str | None = None
    followers: int = Field(ge=0)
    active_ads: int = Field(ge=0)
    spend_estimate: str | None = None
    spend_source: str | None = None
    review_count: int | None = Field(default=None, ge=0)
    review_source: str | None = None


class SheetRowState(BaseModel):
    """PostgreSQL-only cache for a metadata-identified company Sheet row."""

    company_id: int = Field(gt=0)
    spreadsheet_id: str = Field(min_length=1)
    sheet_tab: str = Field(min_length=1)
    row_number: int = Field(ge=2)
    developer_metadata_id: int | None = Field(default=None, gt=0)
    last_exported_first_seen: date
    last_exported_brand: str = Field(min_length=1)
    last_exported_region: str | None = None
    last_exported_instagram: str | None = None


class RelevanceResult(BaseModel):
    """Internal deterministic relevance decision for one advertiser observation."""

    is_relevant: bool
    has_positive_evidence: bool = False
    reason: str = Field(min_length=1, max_length=2000)
    matched_include_keywords: list[str] = Field(default_factory=list)
    matched_exclude_keywords: list[str] = Field(default_factory=list)
