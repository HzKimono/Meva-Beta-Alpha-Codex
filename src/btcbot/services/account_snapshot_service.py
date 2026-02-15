from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from btcbot.domain.account_snapshot import AccountSnapshot, Holding
from btcbot.services.retry import retry_with_backoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountSnapshotService:
    exchange: object

    def build_snapshot(self, *, symbols: list[str], fallback_try_cash: Decimal) -> AccountSnapshot:
        ts = datetime.now(UTC)
        base = getattr(self.exchange, "client", self.exchange)
        source_endpoints: list[str] = []
        flags: list[str] = []

        holdings = self._fetch_holdings(base=base, source_endpoints=source_endpoints, flags=flags)
        cash_try = holdings.get(
            "TRY", Holding(asset="TRY", free=Decimal("0"), locked=Decimal("0"))
        ).free
        if cash_try <= Decimal("0") and fallback_try_cash > Decimal("0"):
            cash_try = fallback_try_cash
            flags.append("used_fallback_try_cash")

        prices = self._fetch_mark_prices(
            base=base,
            symbols=symbols,
            source_endpoints=source_endpoints,
            flags=flags,
        )
        equity_try = cash_try
        for asset, holding in holdings.items():
            if asset == "TRY":
                equity_try += holding.locked
                continue
            symbol = f"{asset}TRY"
            mark = prices.get(symbol)
            if mark is None:
                flags.append(f"missing_mark_price:{symbol}")
                continue
            equity_try += holding.total * mark

        if "balances_private_unavailable" in flags:
            flags.append("missing_private_data")

        return AccountSnapshot(
            timestamp=ts,
            exchange="btcturk",
            cash_try=cash_try,
            holdings=holdings,
            total_equity_try=equity_try,
            source_endpoints=tuple(source_endpoints),
            flags=tuple(flags),
        )

    def _fetch_holdings(
        self, *, base: object, source_endpoints: list[str], flags: list[str]
    ) -> dict[str, Holding]:
        get_balances = getattr(base, "get_balances", None)
        if not callable(get_balances):
            flags.append("balances_private_unavailable")
            return {}

        def _call():
            return get_balances()

        try:
            balances = retry_with_backoff(
                _call,
                max_attempts=3,
                base_delay_ms=100,
                max_delay_ms=400,
                jitter_seed=7,
                retry_on_exceptions=(Exception,),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "account_snapshot_balances_failed", extra={"extra": {"error": type(exc).__name__}}
            )
            flags.append("balances_private_unavailable")
            return {}

        source_endpoints.append("private:get_balances")
        holdings: dict[str, Holding] = {}
        for row in balances:
            asset = str(getattr(row, "asset", "")).upper().strip()
            if not asset:
                continue
            free = self._safe_decimal(getattr(row, "free", 0))
            locked = self._safe_decimal(getattr(row, "locked", 0))
            if free is None or locked is None:
                flags.append(f"invalid_balance_row:{asset}")
                continue
            holdings[asset] = Holding(asset=asset, free=free, locked=locked)
        return holdings

    def _fetch_mark_prices(
        self,
        *,
        base: object,
        symbols: list[str],
        source_endpoints: list[str],
        flags: list[str],
    ) -> dict[str, Decimal]:
        get_orderbook = getattr(base, "get_orderbook", None)
        if not callable(get_orderbook):
            flags.append("orderbook_unavailable")
            return {}

        source_endpoints.append("public:get_orderbook")
        marks: dict[str, Decimal] = {}
        for symbol in symbols:
            normalized = "".join(ch for ch in str(symbol).upper() if ch.isalnum())
            try:
                bid_raw, ask_raw = retry_with_backoff(
                    lambda s=symbol: get_orderbook(s),
                    max_attempts=3,
                    base_delay_ms=100,
                    max_delay_ms=400,
                    jitter_seed=17,
                    retry_on_exceptions=(Exception,),
                )
            except Exception:  # noqa: BLE001
                flags.append(f"orderbook_fetch_failed:{normalized}")
                continue
            bid = self._safe_decimal(bid_raw)
            ask = self._safe_decimal(ask_raw)
            if bid is not None and ask is not None and ask < bid:
                bid, ask = ask, bid
                flags.append(f"orderbook_crossed:{normalized}")
            if bid is not None and bid > 0 and ask is not None and ask > 0:
                marks[normalized] = (bid + ask) / Decimal("2")
            elif bid is not None and bid > 0:
                marks[normalized] = bid
            elif ask is not None and ask > 0:
                marks[normalized] = ask
            else:
                flags.append(f"orderbook_invalid:{normalized}")
        return marks

    @staticmethod
    def _safe_decimal(value: object) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not parsed.is_finite() or parsed < 0:
            return None
        return parsed
