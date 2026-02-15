from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.domain.anomalies import AnomalyCode, AnomalyEvent
from btcbot.domain.stage4 import PnLSnapshot
from btcbot.services.ledger_service import PnlReport


@dataclass(frozen=True)
class AnomalyDetectorConfig:
    stale_market_data_seconds: int = 30
    reject_spike_threshold: int = 3
    latency_spike_ms: int | None = 2000
    cursor_stall_cycles: int = 5
    clock_skew_seconds_threshold: int = 30
    pnl_divergence_try_warn: Decimal = Decimal("50")
    pnl_divergence_try_error: Decimal = Decimal("200")


class AnomalyDetectorService:
    def __init__(
        self,
        *,
        config: AnomalyDetectorConfig | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or AnomalyDetectorConfig()
        self.now_provider = now_provider or (lambda: datetime.now(UTC))

    def detect(
        self,
        *,
        market_data_age_seconds: dict[str, int | float] | None,
        reject_count: int,
        cycle_duration_ms: int | None,
        cursor_stall_by_symbol: dict[str, int],
        pnl_snapshot: PnLSnapshot,
        pnl_report: PnlReport,
    ) -> list[AnomalyEvent]:
        ts = self.now_provider()
        events: list[AnomalyEvent] = []
        seen: set[tuple[AnomalyCode, str]] = set()

        def add_event(event: AnomalyEvent) -> None:
            key = (event.code, event.severity)
            if key in seen:
                return
            seen.add(key)
            events.append(event)

        if market_data_age_seconds is not None:
            stale_symbols = sorted(
                symbol
                for symbol, age in market_data_age_seconds.items()
                if float(age) > float(self.config.stale_market_data_seconds)
            )
            if stale_symbols:
                max_age = max(float(market_data_age_seconds[symbol]) for symbol in stale_symbols)
                add_event(
                    AnomalyEvent(
                        code=AnomalyCode.STALE_MARKET_DATA,
                        severity="WARN",
                        ts=ts,
                        details={
                            "symbols": ",".join(stale_symbols),
                            "max_age_seconds": str(max_age),
                            "threshold_seconds": str(self.config.stale_market_data_seconds),
                        },
                    )
                )

        if self.config.latency_spike_ms is not None and cycle_duration_ms is not None:
            if cycle_duration_ms >= self.config.latency_spike_ms:
                add_event(
                    AnomalyEvent(
                        code=AnomalyCode.EXCHANGE_LATENCY_SPIKE,
                        severity="WARN",
                        ts=ts,
                        details={
                            "cycle_duration_ms": str(cycle_duration_ms),
                            "threshold_ms": str(self.config.latency_spike_ms),
                        },
                    )
                )

        if reject_count >= self.config.reject_spike_threshold:
            add_event(
                AnomalyEvent(
                    code=AnomalyCode.ORDER_REJECT_SPIKE,
                    severity="WARN",
                    ts=ts,
                    details={
                        "reject_count": str(reject_count),
                        "threshold": str(self.config.reject_spike_threshold),
                    },
                )
            )

        stalled_symbols = sorted(
            symbol
            for symbol, stall_cycles in cursor_stall_by_symbol.items()
            if stall_cycles >= self.config.cursor_stall_cycles
        )
        if stalled_symbols:
            max_stall = max(cursor_stall_by_symbol[symbol] for symbol in stalled_symbols)
            add_event(
                AnomalyEvent(
                    code=AnomalyCode.CURSOR_STALL,
                    severity="WARN",
                    ts=ts,
                    details={
                        "symbols": ",".join(stalled_symbols),
                        "max_stall_cycles": str(max_stall),
                        "threshold_cycles": str(self.config.cursor_stall_cycles),
                    },
                )
            )

        clock_skew_seconds = abs((ts - pnl_snapshot.ts).total_seconds())
        if clock_skew_seconds > float(self.config.clock_skew_seconds_threshold):
            add_event(
                AnomalyEvent(
                    code=AnomalyCode.CLOCK_SKEW,
                    severity="WARN",
                    ts=ts,
                    details={
                        "clock_skew_seconds": str(clock_skew_seconds),
                        "threshold_seconds": str(self.config.clock_skew_seconds_threshold),
                    },
                )
            )

        snapshot_equity = pnl_snapshot.total_equity_try
        ledger_equity = pnl_report.equity_estimate
        diff = snapshot_equity - ledger_equity
        abs_diff = abs(diff)
        if abs_diff >= self.config.pnl_divergence_try_error:
            severity = "ERROR"
        elif abs_diff >= self.config.pnl_divergence_try_warn:
            severity = "WARN"
        else:
            severity = None
        if severity is not None:
            add_event(
                AnomalyEvent(
                    code=AnomalyCode.PNL_DIVERGENCE,
                    severity=severity,
                    ts=ts,
                    details={
                        "equity_snapshot_try": str(snapshot_equity),
                        "equity_ledger_try": str(ledger_equity),
                        "recomputed_equity_try": str(ledger_equity),
                        "cash_try": "unknown_stage4",
                        "mtm_try": "unknown_stage4",
                        "realized_try": str(pnl_report.realized_pnl_total),
                        "unrealized_try": str(pnl_report.unrealized_pnl_total),
                        "fees_try": str(sum(pnl_report.fees_total_by_currency.values(), Decimal('0'))),
                        "slippage_try": "unknown_stage4",
                        "diff_try": str(diff),
                        "warn_threshold_try": str(self.config.pnl_divergence_try_warn),
                        "error_threshold_try": str(self.config.pnl_divergence_try_error),
                    },
                )
            )

        return events
