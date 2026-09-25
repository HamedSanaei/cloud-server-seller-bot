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


class ProviderCapacityError(ProviderError):
    """The provider ACCOUNT has no capacity for a NEW billable resource.

    Provider-neutral on purpose: application code must be able to tell "this
    credential account reached its provider limit" (Leaseweb ``PC-2031`` /
    "Customer limit reached") apart from "this offer/image is unavailable"
    without importing any provider adapter.

    It is a fact about the ACCOUNT, so the correct responses are a dedicated
    customer message, a durable per-account capacity signal, and NO automatic
    retry — least of all through another credential account, which would break
    the accepted contract's pinned account and its exactly-once guarantees.
    """

    retryable = False


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
