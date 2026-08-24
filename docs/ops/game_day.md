# Game Day Runbook - disaster & cost runaway (M16-008)

Acceptance: **kill switches and recovery proven.**

The scenarios below are exercised in CI by
`tests/unit/test_game_day.py` (the automated form of this drill) and can
be replayed manually against staging. Each scenario names the inject,
the expected KILL SWITCH response, and the RECOVERY procedure.

## Roles

- **Drill lead**: declares the inject, watches dashboards.
- **Operator on call**: performs the kill-switch/recovery actions.
- **Scribe**: records timeline + evidence links into the task note.

## Scenario 1 - cost runaway

**Inject**: a bug or runaway fleet accrues provider cost far beyond the
daily budget (modeled with 900 EUR accrued vs a 500 EUR global cap).

**Kill switch** (`CostCircuitBreakerService`, M10-004):
- the daily breaker TRIPS for the scope; `check()` returns a
  `CostLimitTrigger` naming the most specific tripped scope;
- every new order is refused before any hold/reservation
  (`CostLimitReachedError`) - existing servers keep running and billing.

**Recovery**: the operator raises the global cap above actual spend (or
fixes the runaway source); `check()` returns None and ordering resumes.
No restart needed - the breaker reads live accruals.

**Proven by**: `test_game_day_cost_runaway_trip_and_recovery`.

## Scenario 2 - provider incident

**Inject**: Hetzner has an outage affecting order placement.

**Kill switch** (`MaintenanceSwitchService`, M10-005):
- operator blocks the provider:
  block requires a non-empty reason, is audited (SYSTEM actor for
  automation, ADMIN for humans), and immediately stops NEW orders for
  that provider while other providers keep serving;
- scoped variants exist per location.

**Recovery**: when the incident closes, unblock with its own audited
reason; orders resume. Un-blocking twice is a safe no-op.

**Proven by**: `test_game_day_provider_incident_block_and_unblock`.

## Scenario 3 - operation loss during outage

**Inject**: a rebuild command is accepted right as the provider starts
failing (503s).

**Behavior under failure / recovery path**:
- the operation is RE-QUEUED under its original ledger key (attempts
  increments; nothing is lost);
- duplicate client requests during the outage never create a second
  operation;
- when the provider heals, the worker drains the pending queue to
  COMPLETED using the SAME deterministic image lookup;
- completed commands replay without touching the provider again.

**Proven by**: `test_game_day_operation_survives_outage_then_completes`.

## Pass criteria

A drill passes when, for each scenario:

1. the kill switch engaged WITHOUT manual database edits,
2. no money was moved and no resource was double-acted during the incident,
3. recovery restored normal service through documented commands only,
4. every operator action left an audit trail entry with a reason.

## Manual replay checklist (staging)

1. Announce drill window; confirm alerts route to on-call.
2. Fire scenario injects one at a time (staging env vars/scripts).
3. For scenario 1: seed accruals above the configured limit via the
   admin API; attempt an order; verify refusal + metric.
4. For scenario 2: POST the maintenance block via admin API; verify new
   orders fail with maintenance error; unblock after.
5. For scenario 3: start a rebuild against a chaos transport (see
   `tests/unit/test_provider_timeout_chaos.py`), stop the fake outage,
   run the worker, verify completion + idempotent replay.
6. Scribe attaches outputs to the task evidence entry.
