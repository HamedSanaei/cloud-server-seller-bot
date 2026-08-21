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


class ProviderNotFound(ProviderError):
    pass
