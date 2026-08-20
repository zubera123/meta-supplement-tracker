"""Meta advertising provider contract."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.models import AdRecord
from app.services import ProviderConfigurationError


class MetaAdsProvider(ABC):
    """Interface for a real, configured advertiser data source."""

    @abstractmethod
    async def retrieve_advertisers(
        self, *, regions: Sequence[str], categories: Sequence[str]
    ) -> list[AdRecord]:
        """Return normalized advertiser records from the provider."""


class UnconfiguredMetaAdsProvider(MetaAdsProvider):
    async def retrieve_advertisers(
        self, *, regions: Sequence[str], categories: Sequence[str]
    ) -> list[AdRecord]:
        raise ProviderConfigurationError("No Meta ads provider has been configured")
