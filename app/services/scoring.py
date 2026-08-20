"""Deterministic filtering and scoring for enriched brand candidates."""

from dataclasses import dataclass

from app.models import BrandCandidate


def instagram_follower_filter(
    followers: int | None,
    *,
    minimum: int = 10_000,
    maximum: int = 100_000,
) -> bool | None:
    """Return inclusive filter status, preserving unknown as ``None``."""

    if minimum > maximum:
        raise ValueError("Minimum followers cannot exceed maximum followers")
    if followers is None:
        return None
    return minimum <= followers <= maximum


@dataclass(frozen=True, slots=True)
class ScoringCriteria:
    min_monthly_spend_usd: float = 5_000
    max_monthly_spend_usd: float = 30_000
    min_instagram_followers: int = 10_000
    max_instagram_followers: int = 100_000
    desirable_review_count: int = 300

    def __post_init__(self) -> None:
        if self.min_monthly_spend_usd > self.max_monthly_spend_usd:
            raise ValueError("Minimum monthly spend cannot exceed maximum monthly spend")
        if self.min_instagram_followers > self.max_instagram_followers:
            raise ValueError("Minimum followers cannot exceed maximum followers")


class CandidateScorer:
    """Apply mandatory range filters and an optional reviews bonus."""

    def __init__(self, criteria: ScoringCriteria | None = None) -> None:
        self.criteria = criteria or ScoringCriteria()

    def evaluate(self, candidate: BrandCandidate) -> BrandCandidate:
        spend = candidate.ad_record.estimated_monthly_spend_usd
        followers = (
            candidate.social_stats.instagram_followers if candidate.social_stats else None
        )
        reasons: list[str] = []

        spend_qualifies = spend is not None and (
            self.criteria.min_monthly_spend_usd
            <= spend
            <= self.criteria.max_monthly_spend_usd
        )
        followers_qualify = instagram_follower_filter(
            followers,
            minimum=self.criteria.min_instagram_followers,
            maximum=self.criteria.max_instagram_followers,
        ) is True

        if spend is None:
            reasons.append("Monthly Meta ad spend estimate is unavailable")
        elif not spend_qualifies:
            reasons.append("Monthly Meta ad spend is outside the target range")
        else:
            reasons.append("Monthly Meta ad spend is within the target range")

        if followers is None:
            reasons.append("Instagram follower count is unavailable")
        elif not followers_qualify:
            reasons.append("Instagram follower count is outside the target range")
        else:
            reasons.append("Instagram follower count is within the target range")

        score = (50.0 if spend_qualifies else 0.0) + (
            40.0 if followers_qualify else 0.0
        )
        reviews = candidate.review_stats
        if reviews and reviews.review_count >= self.criteria.desirable_review_count:
            score += 10.0
            reasons.append("Review count meets the desirable bonus threshold")
        elif reviews:
            reasons.append("Review data is present but below the bonus threshold")
        else:
            reasons.append("Review data is unavailable; no penalty applied")

        return candidate.model_copy(
            update={
                "qualifies": spend_qualifies and followers_qualify,
                "score": score,
                "evaluation_reasons": reasons,
            }
        )
