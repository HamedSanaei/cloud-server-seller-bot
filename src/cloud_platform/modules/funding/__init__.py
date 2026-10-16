"""Operator funding module: organisation-funded grants and immutable expenses.

Funding a provider cost on the operator's behalf is authorization evidence and
immutable organisation cost — never customer-wallet credit and never a
synthetic payment. See :mod:`cloud_platform.modules.funding.domain`.
"""

from cloud_platform.modules.funding.domain import (
    MAX_KEY_LENGTH,
    MAX_LABEL_LENGTH,
    MAX_PROVENANCE_LENGTH,
    MAX_REASON_LENGTH,
    FundingAuthorizationError,
    FundingError,
    FundingFactMismatchError,
    FundingGrantNotFoundError,
    FundingGrantState,
    FundingIdempotencyConflictError,
    FundingPurpose,
    FundingSource,
    FundingStateError,
    FundingValidationError,
    OperatorExpense,
    OperatorFundingCutover,
    OperatorFundingGrant,
    OperatorFundingRepository,
    ProviderCostComponent,
    ProviderCostSnapshot,
    canonical_amount,
    expense_operation_key,
)
from cloud_platform.modules.funding.repository import (
    FUNDING_ADVISORY_LOCK_KEY,
    MAX_EXPENSE_PAGE,
    SqlAlchemyOperatorFundingRepository,
)
from cloud_platform.modules.funding.service import FundingService

__all__ = [
    "FUNDING_ADVISORY_LOCK_KEY",
    "MAX_EXPENSE_PAGE",
    "MAX_KEY_LENGTH",
    "MAX_LABEL_LENGTH",
    "MAX_PROVENANCE_LENGTH",
    "MAX_REASON_LENGTH",
    "FundingAuthorizationError",
    "FundingError",
    "FundingFactMismatchError",
    "FundingGrantNotFoundError",
    "FundingGrantState",
    "FundingIdempotencyConflictError",
    "FundingPurpose",
    "FundingService",
    "FundingSource",
    "FundingStateError",
    "FundingValidationError",
    "OperatorExpense",
    "OperatorFundingCutover",
    "OperatorFundingGrant",
    "OperatorFundingRepository",
    "ProviderCostComponent",
    "ProviderCostSnapshot",
    "SqlAlchemyOperatorFundingRepository",
    "canonical_amount",
    "expense_operation_key",
]
