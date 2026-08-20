"""External provider interfaces and domain services."""


class ProviderError(Exception):
    """Base exception for provider failures."""


class ProviderConfigurationError(ProviderError):
    """Raised when provider configuration is missing or invalid."""


class TransientProviderError(ProviderError):
    """Raised for provider failures that are safe to retry."""
