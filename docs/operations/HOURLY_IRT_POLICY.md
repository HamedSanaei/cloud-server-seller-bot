# Customer hourly credit policy

Customer wallets and every debit remain integer **IRT (toman)**. USD prices
are an internal contract unit, with **1 USD = 1 USDT** explicitly assumed.
Fresh AbanTether USDT/IRT quotes convert each prepaid hour. For IRR quotes,
10 IRR = 1 IRT; round up only at the final IRT charge boundary. Immutable
period receipts retain the accepted nominal price, proxy marker, FX evidence
and exact IRT debit. Replays must not reprice a bound period or debit twice.

Set `[fx].allow_usdt_proxy_for_settlement = true` in the configuration used
by the deployment. This is now the example/default policy; an explicit false
remains an operator kill switch. Existing private/production configuration is
not silently rewritten by an application upgrade. No USD wallet is required.

An accepted first-hour reservation is captured before activation. Subsequent
hours are prepaid in the last minute of current coverage. A failed renewal,
including a positive but insufficient available balance, requests power-off
when paid coverage expires. **Zero remaining cash never truncates paid time.**
The existing `prepaid_zero_since` field now records an *unfunded episode*:
after coverage expires, inability to fund one next hour starts the 24-hour
retention clock, including positive balances. Sufficient replenishment clears
the episode; partial insufficient top-ups do not reset it. Deletion requires
provider-confirmed power-off and a fresh affordability check. Quote/provider
outages must not invent a price or authorize deletion from an unknown balance.

The management keyboard hides START when no paid coverage or affordable next
hour exists. Refresh/reopening re-evaluates eligibility after a top-up. Backend
power commands independently require durable paid coverage, including stale
buttons/public API calls. Explicit restart buys one new hour from restart time,
not backdated powered-off hours; retries recover committed receipts first.
Power-off does not itself guarantee the provider stops charging infrastructure.

Every minute, active customer prepaid hourly resources are grouped per user.
Forecasting simulates their staggered hourly renewal deadlines with available
IRT funds (excluding holds), at current per-server quotes. If the **first**
unaffordable renewal is less than 72 hours away, send one aggregate Telegram
reminder, then at most one per six hours. The reminder explains shutdown,
subsequent deletion risk, and the need to top up. A shared PostgreSQL advisory
lock serializes concurrent warning jobs; successful-send history persists
across worker restarts and server-set changes. Failed sends can retry. Telegram
and PostgreSQL are not one transaction: crash-after-send can duplicate a
message. Forecasts are estimates, not a guarantee against changing FX rates.

Corporate/operator-funded resources do not enter personal-wallet enforcement
or customer runway warnings. Existing wallets/ledger/history are not recreated
or relabeled. No schema migration is required. Production configuration and
deployment require a separate explicitly authorized delivery step.
