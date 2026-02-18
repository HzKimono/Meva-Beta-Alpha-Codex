from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
    timestamp_utc: datetime
    quote_asset: str
    try_cash_target: Decimal
    cash_try_total: Decimal
    cash_try_free: Decimal
    cash_try_locked: Decimal
    investable_try: Decimal
    holdings: dict[str, CycleHolding]
    source_name: str
    source_fields: tuple[str, ...]


def build_cycle_account_snapshot(
    balances: list[object],
    *,
    try_cash_target: Decimal,
    now_utc: datetime | None = None,
    quote_asset: str = "TRY",
) -> CycleAccountSnapshot:
    normalized_quote = str(quote_asset).upper().strip() or "TRY"
    resolved_now = now_utc if now_utc is not None else datetime.now(UTC)
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

    investable_try = max(Decimal("0"), quote_holding.free - try_cash_target)

    return CycleAccountSnapshot(
        timestamp_utc=resolved_now.astimezone(UTC),
        quote_asset=normalized_quote,
        try_cash_target=try_cash_target,
        cash_try_total=quote_holding.total,
        cash_try_free=quote_holding.free,
        cash_try_locked=quote_holding.locked,
        investable_try=investable_try,
        holdings=holdings,
        source_name="exchange:get_balances",
        source_fields=(
            "balances[].asset",
            "balances[].free",
            "balances[].locked",
        ),
    )
