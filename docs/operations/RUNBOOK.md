# Operations runbook starter

## Golden signals
- API error rate/latency
- Telegram update processing delay
- worker queue depth/oldest job age
- provider API errors/429s and remaining rate limit
- provisioning success time
- reconciliation mismatch count
- wallet/ledger posting failures
- payment callback failures
- servers in `manual_review`
- provider spend vs customer billed amount

## Immediate kill switches to implement before launch
- disable all new provisioning
- disable a provider account
- disable a location/plan/offer
- freeze one user
- freeze payments
- pause destructive automation while preserving observation/reconciliation

## Incident priority
Financial integrity and accidental resource creation/deletion outrank feature availability. Prefer a controlled stop with clear status over ambiguous retries.
