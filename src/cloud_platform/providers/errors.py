class ProviderError(RuntimeError):
    """Base provider failure that is safe for application-layer mapping."""


class ProviderAuthError(ProviderError):
    pass


class ProviderRateLimited(ProviderError):
    def __init__(self, message: str, reset_at_unix: int | None = None) -> None:
        super().__init__(message)
        self.reset_at_unix = reset_at_unix


class ProviderConflict(ProviderError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class ProviderOutcomeUnknown(ProviderError):
    """A chargeable provider mutation may or may not have been applied.

    Raised when the HTTP layer cannot prove whether the provider accepted a
    billable POST (read timeout, write timeout, connection dropped mid-
    request, 5xx after the request was sent). The platform MUST NOT blindly
    retry the mutation: it records the operation as
    ``PROVIDER_OUTCOME_UNKNOWN`` and only proceeds through READ-ONLY
    reconciliation (or explicit human review).
    """


class ProviderNotFound(ProviderError):
    pass
