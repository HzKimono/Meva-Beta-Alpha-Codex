from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.intent import Intent
from btcbot.risk.policy import RiskPolicy, RiskPolicyContext
from btcbot.services.state_store import StateStore


class RiskService:
    def __init__(self, risk_policy: RiskPolicy, state_store: StateStore) -> None:
        self.risk_policy = risk_policy
        self.state_store = state_store

    def filter(
        self,
        cycle_id: str,
        intents: list[Intent],
        *,
        try_cash_target: Decimal = Decimal("0"),
        investable_try: Decimal = Decimal("0"),
    ) -> list[Intent]:
        open_orders_by_symbol: dict[str, int] = {}
        find_open_or_unknown_orders = getattr(self.state_store, "find_open_or_unknown_orders", None)
        existing_orders = (
            find_open_or_unknown_orders() if callable(find_open_or_unknown_orders) else []
        )
        for item in existing_orders:
            open_orders_by_symbol[item.symbol] = open_orders_by_symbol.get(item.symbol, 0) + 1

        get_last_intent_ts_by_symbol_side = getattr(
            self.state_store, "get_last_intent_ts_by_symbol_side", None
        )
        last_intent_ts = (
            get_last_intent_ts_by_symbol_side()
            if callable(get_last_intent_ts_by_symbol_side)
            else {}
        )

        context = RiskPolicyContext(
            cycle_id=cycle_id,
            open_orders_by_symbol=open_orders_by_symbol,
            last_intent_ts_by_symbol_side=last_intent_ts,
            mark_prices={},
            cash_try_free=self._extract_try_balance(),
            try_cash_target=try_cash_target,
            investable_try=investable_try,
        )
        approved = self.risk_policy.evaluate(context, intents)
        now = datetime.now(UTC)
        record_intent = getattr(self.state_store, "record_intent", None)
        if callable(record_intent):
            for intent in approved:
                record_intent(intent, now)
        return approved

    def _extract_try_balance(self) -> Decimal:
        get_balances = getattr(self.state_store, "get_latest_balances", None)
        if not callable(get_balances):
            return Decimal("0")
        try:
            balances = get_balances()
        except Exception:  # noqa: BLE001
            return Decimal("0")
        for balance in balances:
            asset = str(getattr(balance, "asset", "")).upper()
            if asset == "TRY":
                return Decimal(str(getattr(balance, "free", 0)))
        return Decimal("0")
