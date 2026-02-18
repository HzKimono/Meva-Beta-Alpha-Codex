from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CycleHolding:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True)
class CycleAccountSnapshot:
    cash_try_total: Decimal
    cash_try_free: Decimal
    cash_try_locked: Decimal
    holdings: dict[str, CycleHolding]
    equity_try: Decimal | None
    source_fields: tuple[str, ...]


def build_cycle_account_snapshot(
    balances: list[object],
    *,
    quote_asset: str = "TRY",
) -> CycleAccountSnapshot:
    normalized_quote = str(quote_asset).upper().strip() or "TRY"
    holdings: dict[str, CycleHolding] = {}

    for balance in balances:
        asset = str(getattr(balance, "asset", "")).upper().strip()
        if not asset:
            continue
        free = Decimal(str(getattr(balance, "free", 0)))
        locked = Decimal(str(getattr(balance, "locked", 0)))
        holdings[asset] = CycleHolding(asset=asset, free=free, locked=locked)

    quote_holding = holdings.get(
        normalized_quote,
        CycleHolding(asset=normalized_quote, free=Decimal("0"), locked=Decimal("0")),
    )

    return CycleAccountSnapshot(
        cash_try_total=quote_holding.total,
        cash_try_free=quote_holding.free,
        cash_try_locked=quote_holding.locked,
        holdings=holdings,
        equity_try=None,
        source_fields=(
            "balances[].asset",
            "balances[].free",
            "balances[].locked",
            f"quote_asset={normalized_quote}",
        ),
    )

