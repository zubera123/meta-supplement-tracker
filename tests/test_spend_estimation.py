"""Tests for conservative spend range estimation."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import (
    AdRecord,
    Brand,
    MetaAdDetails,
    Region,
    SpendEstimate,
    SpendHistory,
)
from app.services.spend_estimation import SpendEstimator, meaningful_overlap


NOW = datetime(2026, 8, 21, tzinfo=UTC)


def record(
    *,
    impressions: object = None,
    reach: int | None = None,
    active_ads: int = 1,
    age_days: int | None = 30,
) -> AdRecord:
    ads = []
    for index in range(active_ads):
        ads.append(
            MetaAdDetails(
                ad_id=f"ad-{index}",
                page_id="page-1",
                page_name="Example Supplements",
                ad_delivery_start_time=(
                    NOW - timedelta(days=age_days - 1) if age_days is not None else None
                ),
                impressions=impressions,
                eu_total_reach=reach,
            )
        )
    return AdRecord(
        brand=Brand(name="Example Supplements", source_id="page-1"),
        region=Region.UK,
        regions=[Region.UK],
        active_ad_count=active_ads,
        ads=ads,
        observed_at=NOW,
    )


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_impressions_based_estimate_uses_real_range_and_cpm() -> None:
    estimate = SpendEstimator(
        settings(spend_cpm_uk_low_usd=10, spend_cpm_uk_high_usd=20)
    ).estimate(record(impressions={"text": "100K - 200K"}))

    assert (estimate.low_usd, estimate.high_usd) == (1000, 4000)
    assert estimate.source == "Impressions × CPM"
    assert estimate.confidence == "medium"


def test_impressions_estimate_can_qualify() -> None:
    estimate = SpendEstimator(
        settings(spend_cpm_uk_low_usd=10, spend_cpm_uk_high_usd=20)
    ).estimate(record(impressions={"text": "500K - 500K"}))

    assert (estimate.low_usd, estimate.high_usd) == (5_000, 10_000)
    assert estimate.target_match is True


def test_reach_based_estimate_uses_frequency_and_cpm() -> None:
    estimate = SpendEstimator(
        settings(
            spend_cpm_uk_low_usd=10,
            spend_cpm_uk_high_usd=20,
            spend_reach_frequency_low=1,
            spend_reach_frequency_high=2,
        )
    ).estimate(record(reach=100_000))

    assert (estimate.low_usd, estimate.high_usd) == (1000, 4000)
    assert estimate.source == "Reach × CPM"
    assert estimate.confidence == "low"


def test_reach_estimate_can_qualify() -> None:
    estimate = SpendEstimator(
        settings(
            spend_cpm_uk_low_usd=10,
            spend_cpm_uk_high_usd=20,
            spend_reach_frequency_low=1,
            spend_reach_frequency_high=2,
        )
    ).estimate(record(reach=500_000))

    assert (estimate.low_usd, estimate.high_usd) == (5_000, 20_000)
    assert estimate.target_match is True


def test_activity_fallback_is_very_low_confidence_and_cannot_qualify() -> None:
    estimate = SpendEstimator(settings()).estimate(record(active_ads=10))

    assert (estimate.low_usd, estimate.high_usd) == (3000, 15000)
    assert estimate.method == "activity_model"
    assert estimate.source == "Activity model - very rough"
    assert estimate.confidence == "very_low"
    assert estimate.observed_inputs["active_ad_count"] == 10
    assert estimate.target_match is None


def test_activity_model_rejects_a_manual_target_decision() -> None:
    with pytest.raises(ValidationError, match="cannot make a spend-target decision"):
        SpendEstimate(
            low_usd=3_000,
            high_usd=15_000,
            method="activity_model",
            source="Activity model - very rough",
            confidence="very_low",
            target_match=False,
        )


def test_insufficient_data_is_unknown() -> None:
    estimate = SpendEstimator(settings()).estimate(
        record(active_ads=1, age_days=None), SpendHistory(observation_count=0)
    )

    assert estimate.low_usd is None
    assert estimate.source == "Unknown"
    assert estimate.confidence == "unknown"
    assert estimate.target_match is None


def test_repeated_activity_enables_fallback_without_start_dates() -> None:
    estimate = SpendEstimator(settings()).estimate(
        record(active_ads=2, age_days=None),
        SpendHistory(observation_count=1, active_ad_counts=[2]),
    )

    assert estimate.method == "activity_model"
    assert estimate.observed_inputs["prior_active_ad_counts"] == [2]


def test_open_ended_impressions_are_not_fabricated() -> None:
    estimate = SpendEstimator(settings()).estimate(
        record(impressions={"text": ">1M"}, age_days=None)
    )

    assert estimate.method == "unknown"


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (8_000, 14_000, True),
        (1_000, 4_000, False),
        (31_000, 50_000, False),
        (4_000, 6_000, True),
        (1_000, 9_000, True),
    ],
)
def test_target_range_meaningful_overlap(low: float, high: float, expected: bool) -> None:
    assert meaningful_overlap(low, high, 5_000, 30_000) is expected


def test_cpm_low_cannot_exceed_high() -> None:
    with pytest.raises(ValidationError, match="SPEND_CPM_UK"):
        settings(spend_cpm_uk_low_usd=20, spend_cpm_uk_high_usd=10)
