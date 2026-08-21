"""Tests for the conservative supplement-brand relevance rules."""

from datetime import UTC, datetime

from app.config import Settings
from app.models import AdRecord, Brand, MetaAdDetails, Region
from app.services.relevance import SupplementRelevanceFilter


def advertiser(
    *,
    name: str,
    creative: str = "",
    category: str | None = None,
    about: str | None = None,
) -> AdRecord:
    return AdRecord(
        brand=Brand(name=name, source_id="page-1"),
        region=Region.UK,
        regions=[Region.UK],
        ads=[
            MetaAdDetails(
                ad_id="ad-1",
                page_id="page-1",
                page_name=name,
                creative_bodies=[creative] if creative else [],
                facebook_page_category=category,
                facebook_page_about=about,
                matched_regions=[Region.UK],
            )
        ],
        observed_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


def test_obvious_supplement_brand_passes() -> None:
    result = SupplementRelevanceFilter().evaluate(
        advertiser(
            name="Daily Health",
            creative="Our magnesium and vitamin supplements support your routine.",
        )
    )

    assert result.is_relevant is True
    assert "magnesium" in result.matched_include_keywords


def test_gym_nutrition_brand_passes() -> None:
    result = SupplementRelevanceFilter().evaluate(
        advertiser(
            name="GymFuel",
            creative="Whey protein, creatine and pre-workout for your next session.",
            category="Sports nutrition",
        )
    )

    assert result.is_relevant is True
    assert "sports nutrition" in result.matched_include_keywords


def test_pet_supplement_brand_passes() -> None:
    result = SupplementRelevanceFilter().evaluate(
        advertiser(
            name="Happy Paws",
            about="Daily dog supplements for joint and digestive support.",
        )
    )

    assert result.is_relevant is True
    assert "dog supplements" in result.matched_include_keywords


def test_obvious_food_produce_brand_fails_even_with_nutrient_claim() -> None:
    result = SupplementRelevanceFilter().evaluate(
        advertiser(
            name="Zespri Kiwifruit",
            creative="Fresh kiwifruit naturally high in vitamin C.",
            category="Food and beverage",
        )
    )

    assert result.is_relevant is False
    assert result.reason == (
        "excluded: obvious non-supplement identity keyword(s): kiwifruit"
    )
    assert "vitamin" in result.matched_include_keywords


def test_unknown_ambiguous_advertiser_is_not_aggressively_rejected() -> None:
    result = SupplementRelevanceFilter().evaluate(
        advertiser(name="Wild Botanics", creative="Feel your best every day.")
    )

    assert result.is_relevant is True
    assert result.matched_include_keywords == []
    assert result.matched_exclude_keywords == []
    assert result.reason == "included: no decisive relevance or exclusion keyword matched"


def test_keywords_are_configurable() -> None:
    relevance_filter = SupplementRelevanceFilter(
        include_keywords=("custom wellness product",),
        exclude_keywords=("fruit orchard",),
    )

    included = relevance_filter.evaluate(
        advertiser(name="Example", creative="A custom wellness product.")
    )
    excluded = relevance_filter.evaluate(
        advertiser(name="Example Fruit Orchard")
    )

    assert included.is_relevant is True
    assert excluded.is_relevant is False


def test_settings_parse_configurable_keyword_lists() -> None:
    settings = Settings(
        _env_file=None,
        supplement_relevance_include_keywords="custom one, custom two",
        supplement_relevance_exclude_keywords="exclude one, exclude two",
    )

    assert settings.relevance_include_keywords == ("custom one", "custom two")
    assert settings.relevance_exclude_keywords == ("exclude one", "exclude two")
