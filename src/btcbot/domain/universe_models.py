from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from btcbot.domain.models import PairInfo
from btcbot.domain.symbols import canonical_symbol


@dataclass(frozen=True)
class UniverseKnobs:
    """Configuration knobs for offline universe selection used in Stage 5 integration later."""

    quote_currency: str = "TRY"
    max_universe_size: int = 20
    min_notional_try: Decimal = Decimal("50")
    max_spread_bps: Decimal = Decimal("200")
    allow_symbols: tuple[str, ...] = ()
    deny_symbols: tuple[str, ...] = ()
    require_active: bool = True
    require_try_quote: bool = True


@dataclass(frozen=True)
class SymbolInfo:
    """Exchange metadata used by deterministic offline universe selection."""

    symbol: str
    base: str | None
    quote: str | None
    active: bool = True
    min_notional_try: Decimal | None = None
    volume_try: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", canonical_symbol(self.symbol))
        if self.base is not None:
            object.__setattr__(self, "base", str(self.base).upper())
        if self.quote is not None:
            object.__setattr__(self, "quote", str(self.quote).upper())

    @classmethod
    def from_pair_info(cls, pair: PairInfo) -> SymbolInfo:
        status = (pair.status or "").strip().upper()
        active = True if not status else status in {"TRADING", "ACTIVE", "ENABLED"}
        return cls(
            symbol=pair.pair_symbol,
            base=pair.numerator,
            quote=pair.denominator,
            active=active,
            min_notional_try=pair.min_total_amount,
        )
