"""Qualified-brand output provider contracts."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.models import BrandCandidate
from app.services import ProviderConfigurationError


class BrandOutputProvider(ABC):
    @abstractmethod
    async def write_candidates(self, candidates: Sequence[BrandCandidate]) -> None:
        """Persist all qualifying candidates to an output destination."""


class GoogleDocsOutputProvider(BrandOutputProvider):
    """Google Docs integration boundary; API implementation is intentionally pending."""

    def __init__(self, *, service_account_json: str | None, document_id: str | None) -> None:
        self._service_account_json = service_account_json
        self._document_id = document_id

    async def write_candidates(self, candidates: Sequence[BrandCandidate]) -> None:
        if not self._service_account_json or not self._document_id:
            raise ProviderConfigurationError(
                "Google Docs credentials and document ID are not configured"
            )
        raise NotImplementedError(
            "Google Docs API writing is not implemented; configure a verified provider first"
        )
