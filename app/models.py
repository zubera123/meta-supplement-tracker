"""Domain models shared across providers, jobs, and scoring."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


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


class AdRecord(BaseModel):
    """Provider-normalized advertising data for one brand."""

    brand: Brand
    region: Region
    estimated_monthly_spend_usd: float | None = Field(default=None, ge=0)
    active_ad_count: int | None = Field(default=None, ge=0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider_metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )


class SocialStats(BaseModel):
    """Normalized social audience data."""

    instagram_followers: int | None = Field(default=None, ge=0)
    instagram_handle: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReviewStats(BaseModel):
    """Normalized review data from an optional reviews provider."""

    source: str = Field(min_length=1)
    review_count: int = Field(ge=0)
    rating: float | None = Field(default=None, ge=0, le=5)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BrandCandidate(BaseModel):
    """A fully or partially enriched brand ready for evaluation."""

    brand: Brand
    ad_record: AdRecord
    social_stats: SocialStats | None = None
    review_stats: ReviewStats | None = None
    qualifies: bool = False
    score: float = Field(default=0, ge=0, le=100)
    evaluation_reasons: list[str] = Field(default_factory=list)
