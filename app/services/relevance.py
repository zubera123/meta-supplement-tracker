"""Conservative deterministic supplement-brand relevance filtering."""

import re
import unicodedata
from collections.abc import Sequence

from app.models import AdRecord, RelevanceResult


DEFAULT_RELEVANCE_INCLUDE_KEYWORDS: tuple[str, ...] = (
    "supplement",
    "supplements",
    "vitamin",
    "vitamins",
    "multivitamin",
    "mineral supplement",
    "trace minerals",
    "protein",
    "protein powder",
    "whey",
    "creatine",
    "pre workout",
    "collagen",
    "gummy",
    "gummies",
    "electrolyte",
    "electrolytes",
    "greens powder",
    "super greens",
    "daily greens",
    "probiotic",
    "probiotics",
    "omega 3",
    "magnesium",
    "wellness supplement",
    "pet supplement",
    "pet supplements",
    "dog supplement",
    "dog supplements",
    "cat supplement",
    "cat supplements",
    "sports nutrition",
    "gym nutrition",
    "amino acid",
    "amino acids",
    "bcaa",
    "hydration powder",
    "meal replacement",
    "nutrition shake",
    "fish oil",
)

DEFAULT_RELEVANCE_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "kiwifruit",
    "fresh produce",
    "produce company",
    "produce market",
    "fruit company",
    "fruit grower",
    "fruit growers",
    "greengrocer",
    "grocery store",
    "supermarket",
    "restaurant",
    "food delivery",
    "hair care",
    "haircare",
    "shampoo",
    "conditioner",
    "skin care",
    "skincare",
    "cosmetics",
    "makeup",
    "mineral specimen",
    "mineral specimens",
    "gemstone",
    "gemstones",
    "crystal shop",
    "jewelry",
    "jewellery",
    "clothing",
    "apparel",
    "gym equipment",
)

# Generic nutrient words in creative copy can describe ordinary food. They are
# positive when part of the page identity, but need another include signal when
# they occur only in ad/About copy.
_CREATIVE_ONLY_WEAK_INCLUDE_KEYWORDS = frozenset({"vitamin", "vitamins"})


class SupplementRelevanceFilter:
    """Exclude only clear non-supplement advertisers using provider-returned text."""

    def __init__(
        self,
        *,
        include_keywords: Sequence[str] = DEFAULT_RELEVANCE_INCLUDE_KEYWORDS,
        exclude_keywords: Sequence[str] = DEFAULT_RELEVANCE_EXCLUDE_KEYWORDS,
    ) -> None:
        self._include_keywords = _clean_keywords(include_keywords)
        self._exclude_keywords = _clean_keywords(exclude_keywords)

    def evaluate(self, record: AdRecord) -> RelevanceResult:
        identity_text = _identity_text(record)
        all_text = _all_text(record, identity_text)
        identity_includes = _matches(identity_text, self._include_keywords)
        identity_excludes = _matches(identity_text, self._exclude_keywords)
        all_includes = _matches(all_text, self._include_keywords)
        all_excludes = _matches(all_text, self._exclude_keywords)

        if identity_excludes and not identity_includes:
            return RelevanceResult(
                is_relevant=False,
                has_positive_evidence=False,
                reason=(
                    "excluded: obvious non-supplement identity keyword(s): "
                    + ", ".join(identity_excludes)
                ),
                matched_include_keywords=all_includes,
                matched_exclude_keywords=all_excludes,
            )
        strong_includes = [
            keyword
            for keyword in all_includes
            if keyword not in _CREATIVE_ONLY_WEAK_INCLUDE_KEYWORDS
            or keyword in identity_includes
        ]
        if strong_includes:
            return RelevanceResult(
                is_relevant=True,
                has_positive_evidence=True,
                reason=(
                    "included: supplement keyword(s): " + ", ".join(all_includes)
                ),
                matched_include_keywords=all_includes,
                matched_exclude_keywords=all_excludes,
            )
        if all_excludes:
            return RelevanceResult(
                is_relevant=False,
                has_positive_evidence=False,
                reason=(
                    "excluded: obvious non-supplement keyword(s): "
                    + ", ".join(all_excludes)
                ),
                matched_exclude_keywords=all_excludes,
            )
        if all_includes:
            return RelevanceResult(
                is_relevant=True,
                has_positive_evidence=False,
                reason=(
                    "ambiguous: generic nutrient keyword(s) without positive "
                    "supplement/product identity: " + ", ".join(all_includes)
                ),
                matched_include_keywords=all_includes,
            )
        return RelevanceResult(
            is_relevant=True,
            has_positive_evidence=False,
            reason="ambiguous: no positive supplement keyword or explicit exclusion matched",
        )


def _identity_text(record: AdRecord) -> str:
    values = [record.brand.name]
    values.extend(
        ad.facebook_page_category
        for ad in record.ads
        if ad.facebook_page_category
    )
    return _normalize(" ".join(values))


def _all_text(record: AdRecord, identity_text: str) -> str:
    values = [identity_text]
    for ad in record.ads:
        values.extend(ad.creative_bodies)
        values.extend(ad.creative_link_captions)
        values.extend(ad.creative_link_descriptions)
        values.extend(ad.creative_link_titles)
        values.extend(
            value
            for value in (
                ad.cta_headline,
                ad.cta_description,
                ad.facebook_page_about,
            )
            if value
        )
    return _normalize(" ".join(values))


def _clean_keywords(keywords: Sequence[str]) -> tuple[str, ...]:
    cleaned = {_normalize(value) for value in keywords if value.strip()}
    cleaned.discard("")
    return tuple(sorted(cleaned))


def _matches(text: str, keywords: Sequence[str]) -> list[str]:
    return [
        keyword
        for keyword in keywords
        if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text)
    ]


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())
