"""Instagram enrichment provider contract."""

from abc import ABC, abstractmethod

from app.models import Brand, SocialStats
from app.services import ProviderConfigurationError


class InstagramProvider(ABC):
    @abstractmethod
    async def get_social_stats(self, brand: Brand) -> SocialStats | None:
        """Return normalized Instagram statistics, or None when unavailable."""


class UnconfiguredInstagramProvider(InstagramProvider):
    async def get_social_stats(self, brand: Brand) -> SocialStats | None:
        raise ProviderConfigurationError("No Instagram provider has been configured")
