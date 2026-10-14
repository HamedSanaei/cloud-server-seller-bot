#: Adapter-documented codes that prove a billable create was refused before acceptance.
DEFINITIVE_CAPACITY_REFUSAL_CODES = frozenset({"resource_limit_exceeded"})


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


class ProviderRejected(ProviderError):
    """An authoritative response definitively rejected the requested mutation."""


class ProviderCapacityError(ProviderError):
    """The provider ACCOUNT has no capacity for a NEW billable resource.

    Provider-neutral on purpose: application code must be able to tell "this
    credential account reached its provider limit" (Leaseweb ``PC-2031`` /
    "Customer limit reached") apart from "this offer/image is unavailable"
    without importing any provider adapter.

    Durable orchestration may choose another account only after a proven
    pre-acceptance capacity refusal. Accepted or uncertain mutations remain
    pinned; this error does not authorize an ordinary retry.
    """

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        quota_names: tuple[str, ...] = (),
        definitive_refusal: bool = False,
    ) -> None:
        super().__init__(message)
        self._error_code = error_code
        self.quota_names = tuple(quota_names)
        # Only the adapter that interpreted an authoritative pre-acceptance
        # response may grant account continuation, never a local headroom error.
        self.definitive_refusal = definitive_refusal

    @property
    def error_code(self) -> str | None:
        return self._error_code

    @property
    def allows_account_failover(self) -> bool:
        return self.definitive_refusal and self.error_code in DEFINITIVE_CAPACITY_REFUSAL_CODES


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
