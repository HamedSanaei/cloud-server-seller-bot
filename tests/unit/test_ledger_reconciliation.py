"""Tests for the ledger reconciliation report (M05-008).

Acceptance: detects impossible/corrupt balance states.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from cloud_platform.core.money import Money
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    LedgerEntry,
    LedgerEntryType,
    Wallet,
    WalletStatus,
)
from cloud_platform.modules.wallet.reconciliation import (
    BALANCE_LEDGER_MISMATCH,
    CAPTURED_HOLD_MISSING_CHARGE,
    CHARGE_WITHOUT_HOLD,
    CLOSED_NONZERO_BALANCE,
    CURRENCY_MISMATCH_ENTRY,
    CURRENCY_MISMATCH_HOLD,
    DUPLICATE_IDEMPOTENCY_KEY,
    HOLD_WITHOUT_ENTRY,
    NEGATIVE_BALANCE,
    OVERRESERVED,
    RELEASE_WITHOUT_HOLD,
    RELEASED_HOLD_MISSING_RELEASE,
    ZERO_AMOUNT_ENTRY,
    FindingSeverity,
    LedgerReconciliationService,
    check_wallet,
)

WALLET_ID = uuid4()
USER_ID = uuid4()


def _wallet(balance: int = 1000, status: WalletStatus = WalletStatus.ACTIVE) -> Wallet:
    return Wallet(
        user_id=USER_ID,
        id=WALLET_ID,
        balance=balance,
        currency="EUR",
        status=status,
    )


def _hold(
    key: str,
    amount: int = 100,
    status: HoldStatus = HoldStatus.CREATED,
    hold_id: object = None,
    currency: str = "EUR",
) -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=amount,
        currency=currency,
        idempotency_key=key,
        id=hold_id,
        status=status,
    )


def _entry(
    key: str,
    amount: int,
    entry_type: LedgerEntryType,
    reference_type: str = "",
    reference_id: str = "",
    currency: str = "EUR",
) -> LedgerEntry:
    return LedgerEntry(
        id=uuid4(),
        wallet_id=WALLET_ID,
        entry_type=entry_type,
        amount=Money(Decimal(amount), currency),
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=key,
    )


def _codes(findings: list) -> list[str]:
    return sorted(f.code for f in findings)


class TestCleanStates:
    def test_fresh_wallet_is_clean(self) -> None:
        assert check_wallet(_wallet(0), [], []) == []

    def test_deposit_balance_is_clean(self) -> None:
        wallet = _wallet(500)
        entries = [_entry("dep-1", 500, LedgerEntryType.DEPOSIT)]
        assert check_wallet(wallet, [], entries) == []

    def test_hold_lifecycle_is_clean(self) -> None:
        hold_id = uuid4()
        wallet = _wallet(1000)
        holds = [_hold("k1", 100, HoldStatus.CREATED, hold_id)]
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("hold-k1", 100, LedgerEntryType.HOLD, "hold", str(hold_id)),
        ]
        assert check_wallet(wallet, holds, entries) == []

    def test_captured_hold_with_charge_is_clean(self) -> None:
        hold_id = uuid4()
        # 1000 deposited, hold 100, capture -> balance 900
        wallet = _wallet(900)
        holds = [_hold("k1", 100, HoldStatus.CAPTURED, hold_id)]
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("hold-k1", 100, LedgerEntryType.HOLD, "hold", str(hold_id)),
            _entry("capture-k1", 100, LedgerEntryType.CHARGE, "hold", str(hold_id)),
        ]
        assert check_wallet(wallet, holds, entries) == []

    def test_released_hold_with_release_is_clean(self) -> None:
        hold_id = uuid4()
        wallet = _wallet(1000)
        holds = [_hold("k1", 100, HoldStatus.RELEASED, hold_id)]
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("hold-k1", 100, LedgerEntryType.HOLD, "hold", str(hold_id)),
            _entry("release-k1", 100, LedgerEntryType.RELEASE, "hold", str(hold_id)),
        ]
        assert check_wallet(wallet, holds, entries) == []

    def test_adjustment_explained_residual_is_warning_only(self) -> None:
        # 1000 deposited, admin debited 400 -> balance 600; adjustment entry
        # stores the unsigned 400, so the residual -400 is explainable.
        wallet = _wallet(600)
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("adj-1", 400, LedgerEntryType.ADJUSTMENT, "admin_adjustment", ""),
        ]
        findings = check_wallet(wallet, [], entries)
        assert _codes(findings) == [BALANCE_LEDGER_MISMATCH]
        assert findings[0].severity is FindingSeverity.WARNING


class TestCorruptStates:
    def test_negative_balance(self) -> None:
        findings = check_wallet(_wallet(-1), [], [])
        assert NEGATIVE_BALANCE in _codes(findings)
        assert all(
            f.severity is FindingSeverity.ERROR for f in findings if f.code == NEGATIVE_BALANCE
        )

    def test_overreserved(self) -> None:
        wallet = _wallet(100)
        holds = [_hold("k1", 100), _hold("k2", 50)]
        findings = check_wallet(wallet, holds, [])
        assert OVERRESERVED in _codes(findings)

    def test_closed_wallet_nonzero_balance(self) -> None:
        findings = check_wallet(_wallet(50, WalletStatus.CLOSED), [], [])
        assert CLOSED_NONZERO_BALANCE in _codes(findings)

    def test_hold_currency_mismatch(self) -> None:
        findings = check_wallet(_wallet(100), [_hold("k1", currency="USD")], [])
        assert CURRENCY_MISMATCH_HOLD in _codes(findings)

    def test_entry_currency_mismatch(self) -> None:
        findings = check_wallet(
            _wallet(0), [], [_entry("dep-1", 10, LedgerEntryType.DEPOSIT, currency="USD")]
        )
        assert CURRENCY_MISMATCH_ENTRY in _codes(findings)

    def test_zero_amount_entry(self) -> None:
        # LedgerEntry forbids zero at construction; simulate a corrupt row
        # by overwriting the frozen field directly.
        entry = _entry("dep-0", 1, LedgerEntryType.DEPOSIT)
        object.__setattr__(entry, "amount", Money(Decimal(0), "EUR"))
        findings = check_wallet(_wallet(0), [], [entry])
        assert ZERO_AMOUNT_ENTRY in _codes(findings)

    def test_duplicate_idempotency_key(self) -> None:
        entries = [
            _entry("dup", 10, LedgerEntryType.DEPOSIT),
            _entry("dup", 20, LedgerEntryType.DEPOSIT),
        ]
        wallet = _wallet(30)
        findings = check_wallet(wallet, [], entries)
        assert DUPLICATE_IDEMPOTENCY_KEY in _codes(findings)

    def test_captured_hold_missing_charge(self) -> None:
        wallet = _wallet(900)
        holds = [_hold("k1", 100, HoldStatus.CAPTURED, uuid4())]
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("hold-k1", 100, LedgerEntryType.HOLD, "hold", str(holds[0].id)),
        ]
        findings = check_wallet(wallet, holds, entries)
        assert CAPTURED_HOLD_MISSING_CHARGE in _codes(findings)

    def test_released_hold_missing_release(self) -> None:
        wallet = _wallet(1000)
        holds = [_hold("k1", 100, HoldStatus.RELEASED, uuid4())]
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("hold-k1", 100, LedgerEntryType.HOLD, "hold", str(holds[0].id)),
        ]
        findings = check_wallet(wallet, holds, entries)
        assert RELEASED_HOLD_MISSING_RELEASE in _codes(findings)

    def test_charge_without_hold(self) -> None:
        wallet = _wallet(900)
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("capture-ghost", 100, LedgerEntryType.CHARGE, "hold", "no-such-hold"),
        ]
        findings = check_wallet(wallet, [], entries)
        assert CHARGE_WITHOUT_HOLD in _codes(findings)

    def test_charge_referencing_created_hold(self) -> None:
        hold_id = uuid4()
        wallet = _wallet(1000)
        holds = [_hold("k1", 100, HoldStatus.CREATED, hold_id)]
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("hold-k1", 100, LedgerEntryType.HOLD, "hold", str(hold_id)),
            _entry("capture-k1", 100, LedgerEntryType.CHARGE, "hold", str(hold_id)),
        ]
        findings = check_wallet(wallet, holds, entries)
        assert CHARGE_WITHOUT_HOLD in _codes(findings)

    def test_release_without_hold(self) -> None:
        wallet = _wallet(1000)
        entries = [
            _entry("dep-1", 1000, LedgerEntryType.DEPOSIT),
            _entry("release-ghost", 100, LedgerEntryType.RELEASE, "hold", "no-such-hold"),
        ]
        findings = check_wallet(wallet, [], entries)
        assert RELEASE_WITHOUT_HOLD in _codes(findings)

    def test_hold_without_entry_is_warning(self) -> None:
        wallet = _wallet(1000)
        holds = [_hold("k1", 100)]
        entries = [_entry("dep-1", 1000, LedgerEntryType.DEPOSIT)]
        findings = check_wallet(wallet, holds, entries)
        assert HOLD_WITHOUT_ENTRY in _codes(findings)
        flagged = next(f for f in findings if f.code == HOLD_WITHOUT_ENTRY)
        assert flagged.severity is FindingSeverity.WARNING

    def test_unexplained_residual_is_error(self) -> None:
        # 1000 deposited, no charges/adjustments, but balance is 500.
        wallet = _wallet(500)
        entries = [_entry("dep-1", 1000, LedgerEntryType.DEPOSIT)]
        findings = check_wallet(wallet, [], entries)
        flagged = [f for f in findings if f.code == BALANCE_LEDGER_MISMATCH]
        assert len(flagged) == 1
        assert flagged[0].severity is FindingSeverity.ERROR
        assert flagged[0].details["residual"] == "-500"


class TestReportRendering:
    def test_render_lists_findings(self) -> None:
        from cloud_platform.modules.wallet.reconciliation import ReconciliationReport

        findings = check_wallet(_wallet(-5), [], [])
        report = ReconciliationReport(wallets_checked=1, findings=tuple(findings))
        text = report.render()
        assert "1 wallet(s) checked" in text
        assert "ERROR" in text
        assert NEGATIVE_BALANCE in text
        assert not report.is_clean

    def test_clean_report(self) -> None:
        from cloud_platform.modules.wallet.reconciliation import ReconciliationReport

        report = ReconciliationReport(wallets_checked=3, findings=())
        assert report.is_clean
        assert "all wallet states consistent" in report.render()


class _FakeRepos:
    def __init__(
        self,
        wallets: list[Wallet],
        holds: dict,
        entries: dict,
    ) -> None:
        self.wallets = wallets
        self.holds = holds
        self.entries = entries
        self.wallet_calls = 0
        self.hold_calls = 0
        self.entry_calls = 0

    async def list_all(self) -> list[Wallet]:
        self.wallet_calls += 1
        return list(self.wallets)

    async def list_by_wallet(self, wallet_id) -> list[Hold]:
        self.hold_calls += 1
        return list(self.holds.get(wallet_id, ()))

    async def list_entries(self, wallet_id) -> list[LedgerEntry]:
        self.entry_calls += 1
        return list(self.entries.get(wallet_id, ()))


class TestService:
    async def test_aggregates_findings_across_wallets(self) -> None:
        good = _wallet(100)
        bad = _wallet(-5)
        good.id = uuid4()
        bad.id = uuid4()
        good_id, bad_id = good.id, bad.id
        assert good_id is not None and bad_id is not None
        repos = _FakeRepos(
            wallets=[good, bad],
            holds={good_id: [], bad_id: []},
            entries={
                good_id: [_entry("dep-1", 100, LedgerEntryType.DEPOSIT)],
                bad_id: [],
            },
        )
        service = LedgerReconciliationService(repos, repos, repos)  # type: ignore[arg-type]
        report = await service.generate_report()

        assert report.wallets_checked == 2
        # -5 balance with an empty ledger is NEGATIVE_BALANCE, OVERRESERVED,
        # and an unexplained BALANCE_LEDGER_MISMATCH; all from the bad wallet
        assert {e.code for e in report.errors} == {
            NEGATIVE_BALANCE,
            OVERRESERVED,
            BALANCE_LEDGER_MISMATCH,
        }
        assert all(e.wallet_id == bad_id for e in report.errors)
        # each wallet's holds and entries were fetched once
        assert repos.hold_calls == 2
        assert repos.entry_calls == 2

    async def test_no_wallets(self) -> None:
        repos = _FakeRepos(wallets=[], holds={}, entries={})
        service = LedgerReconciliationService(repos, repos, repos)  # type: ignore[arg-type]
        report = await service.generate_report()
        assert report.wallets_checked == 0
        assert report.is_clean

    async def test_report_is_read_only(self) -> None:
        # the pass must not mutate wallets, holds, or entries
        wallet = _wallet(0)
        repos = _FakeRepos(wallets=[wallet], holds={wallet.id: []}, entries={wallet.id: []})
        service = LedgerReconciliationService(repos, repos, repos)  # type: ignore[arg-type]
        await service.generate_report()
        assert wallet.balance == 0
        assert wallet.status is WalletStatus.ACTIVE
