"""Review enrichment provider contract."""

from abc import ABC, abstractmethod

from app.models import Brand, ReviewStats
from app.services import ProviderConfigurationError


class ReviewsProvider(ABC):
    @abstractmethod
    async def get_review_stats(self, brand: Brand) -> ReviewStats | None:
        """Return normalized review statistics, or None when unavailable."""


class UnconfiguredReviewsProvider(ReviewsProvider):
    async def get_review_stats(self, brand: Brand) -> ReviewStats | None:
        raise ProviderConfigurationError("No reviews provider has been configured")
