from datetime import datetime

import pytest

from app.models import AdRecord, Brand, BrandCandidate, Region, ReviewStats, SocialStats
from app.services.scoring import CandidateScorer, ScoringCriteria


def make_candidate(
    *,
    spend: float | None = 10_000,
    followers: int | None = 20_000,
    reviews: int | None = None,
) -> BrandCandidate:
    brand = Brand(name="Example Supplements")
    return BrandCandidate(
        brand=brand,
        ad_record=AdRecord(
            brand=brand,
            region=Region.UK,
            estimated_monthly_spend_usd=spend,
            observed_at=datetime(2026, 1, 1),
        ),
        social_stats=(
            SocialStats(
                instagram_followers=followers,
                observed_at=datetime(2026, 1, 1),
            )
            if followers is not None
            else None
        ),
        review_stats=(
            ReviewStats(
                source="Trustpilot",
                review_count=reviews,
                observed_at=datetime(2026, 1, 1),
            )
            if reviews is not None
            else None
        ),
    )


@pytest.mark.parametrize(
    ("spend", "followers"),
    [(5_000, 10_000), (30_000, 100_000), (15_000, 50_000)],
)
def test_inclusive_target_boundaries_qualify(spend: float, followers: int) -> None:
    result = CandidateScorer().evaluate(make_candidate(spend=spend, followers=followers))

    assert result.qualifies is True
    assert result.score == 90


@pytest.mark.parametrize("spend", [4_999, 30_001, None])
def test_spend_outside_range_does_not_qualify(spend: float | None) -> None:
    result = CandidateScorer().evaluate(make_candidate(spend=spend))

    assert result.qualifies is False
    assert result.score == 40


@pytest.mark.parametrize("followers", [9_999, 100_001, None])
def test_followers_outside_range_does_not_qualify(followers: int | None) -> None:
    result = CandidateScorer().evaluate(make_candidate(followers=followers))

    assert result.qualifies is False
    assert result.score == 50


def test_desirable_reviews_add_bonus() -> None:
    result = CandidateScorer().evaluate(make_candidate(reviews=300))

    assert result.qualifies is True
    assert result.score == 100


@pytest.mark.parametrize("reviews", [None, 0, 299])
def test_reviews_never_determine_qualification(reviews: int | None) -> None:
    result = CandidateScorer().evaluate(make_candidate(reviews=reviews))

    assert result.qualifies is True
    assert result.score == 90


def test_invalid_criteria_are_rejected() -> None:
    with pytest.raises(ValueError, match="Minimum monthly spend"):
        ScoringCriteria(min_monthly_spend_usd=30_000, max_monthly_spend_usd=5_000)
