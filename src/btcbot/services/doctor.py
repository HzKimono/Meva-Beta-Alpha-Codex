from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from btcbot.config import Settings
from btcbot.persistence.sqlite.sqlite_connection import sqlite_connection_context
from btcbot.observability import get_instrumentation
from btcbot.replay.validate import DatasetValidationReport, validate_replay_dataset
from btcbot.services.effective_universe import resolve_effective_universe
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.exchange_rules_service import ExchangeRulesService
from btcbot.services.state_store import StateStore


@dataclass(frozen=True)
class DoctorCheck:
    category: str
    name: str
    status: str
    message: str

    def __post_init__(self) -> None:
        normalized = self.status.strip().lower()
        status_map = {
            "ok": "pass",
            "warning": "warn",
            "error": "fail",
        }
        normalized = status_map.get(normalized, normalized)
        if normalized not in {"pass", "warn", "fail"}:
            msg = f"invalid doctor check status: {self.status}"
            raise ValueError(msg)
        object.__setattr__(self, "status", normalized)


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]
    errors: list[str]
    warnings: list[str]
    actions: list[str]

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)


def doctor_status(report: DoctorReport) -> str:
    if any(check.status == "fail" for check in report.checks):
        return "fail"
    if any(check.status == "warn" for check in report.checks):
        return "warn"
    return "pass"


def run_health_checks(
    settings: Settings,
    *,
    db_path: str | None,
    dataset_path: str | None,
) -> DoctorReport:
    started = perf_counter()
    checks: list[DoctorCheck] = []
    errors: list[str] = []
    warnings: list[str] = []
    actions: list[str] = []

    if settings.stage7_enabled and not settings.dry_run:
        errors.append("STAGE7_ENABLED requires DRY_RUN=true")
        checks.append(DoctorCheck("gates", "stage7_requires_dry_run", "fail", errors[-1]))
    if settings.stage7_enabled and settings.live_trading:
        errors.append("STAGE7_ENABLED requires LIVE_TRADING=false")
        checks.append(DoctorCheck("gates", "stage7_blocks_live_trading", "fail", errors[-1]))

    if settings.live_trading and not settings.is_live_trading_enabled():
        errors.append("LIVE_TRADING=true but LIVE_TRADING_ACK is not set to I_UNDERSTAND")
        checks.append(DoctorCheck("gates", "live_trading_ack", "fail", errors[-1]))
    if settings.live_trading and settings.btcturk_api_key is None:
        errors.append("LIVE_TRADING=true but BTCTURK_API_KEY is missing")
        checks.append(DoctorCheck("gates", "live_trading_api_key", "fail", errors[-1]))
    if settings.live_trading and settings.btcturk_api_secret is None:
        errors.append("LIVE_TRADING=true but BTCTURK_API_SECRET is missing")
        checks.append(DoctorCheck("gates", "live_trading_api_secret", "fail", errors[-1]))

    if settings.kill_switch and settings.live_trading:
        warnings.append(
            "KILL_SWITCH=true with LIVE_TRADING=true will block writes "
            "until kill switch is disabled"
        )
        checks.append(DoctorCheck("gates", "kill_switch_live_trading", "warn", warnings[-1]))

    if not any(check.category == "gates" for check in checks):
        checks.append(DoctorCheck("gates", "coherence", "pass", "gate configuration is coherent"))

    effective_universe = resolve_effective_universe(settings)
    checks.append(
        DoctorCheck(
            "universe",
            "effective_symbols",
            "pass",
            f"effective symbols={effective_universe.symbols} "
            f"size={len(effective_universe.symbols)} "
            f"source={effective_universe.source}",
        )
    )
    checks.append(
        DoctorCheck(
            "universe",
            "metadata_validation",
            "pass" if effective_universe.metadata_available else "warn",
            (
                "metadata validation performed"
                if effective_universe.metadata_available
                else "metadata unavailable; cannot validate symbols"
            ),
        )
    )
    if effective_universe.rejected_symbols:
        warnings.append(
            "Rejected symbols via exchange metadata: "
            f"{','.join(effective_universe.rejected_symbols)}"
        )
        checks.append(
            DoctorCheck(
                "universe",
                "rejected_symbols",
                "warn",
                f"rejected symbols={effective_universe.rejected_symbols} "
                f"suggested={effective_universe.suggestions}",
            )
        )
        if effective_universe.auto_corrected_symbols:
            checks.append(
                DoctorCheck(
                    "universe",
                    "auto_corrected_symbols",
                    "warn",
                    f"auto_corrected={effective_universe.auto_corrected_symbols}",
                )
            )

    _check_exchange_rules(settings, effective_universe.symbols, checks, errors, warnings, actions)

    if dataset_path is not None:
        dataset_report = validate_replay_dataset(Path(dataset_path))
        _merge_dataset_report(dataset_report, checks, errors, warnings)
        if not dataset_report.ok:
            actions.extend(
                [
                    r"Create folder: .\data\replay",
                    r"Run: python -m btcbot.cli replay-init --dataset .\data\replay",
                    "Or omit --dataset if you only run stage7-run.",
                ]
            )
    else:
        checks.append(
            DoctorCheck(
                "backtest_readiness",
                "dataset_optional",
                "pass",
                "dataset is optional; required only for replay/backtest",
            )
        )

    if db_path is not None:
        _validate_db_path(db_path=db_path, errors=errors, warnings=warnings)
        checks.append(
            DoctorCheck(
                "paths", "db_path", "pass" if db_path else "warn", f"db path checked: {db_path}"
            )
        )
    else:
        checks.append(
            DoctorCheck("paths", "db_path", "warn", "db path not provided; skipping db write test")
        )

    _run_slo_checks(
        settings=settings,
        db_path=db_path,
        checks=checks,
        warnings=warnings,
        actions=actions,
    )

    report = DoctorReport(checks=checks, errors=errors, warnings=warnings, actions=actions)
    derived_ok = all(check.status != "fail" for check in report.checks)
    if report.ok != derived_ok:
        summary = ", ".join(
            f"{check.category}/{check.name}:{check.status}" for check in report.checks
        )
        raise RuntimeError(f"doctor report ok invariant violated: {summary}")

    _emit_doctor_telemetry(report, duration_ms=(perf_counter() - started) * 1000)
    return report


def normalize_drawdown_ratio(
    max_drawdown_ratio: object | None,
    max_drawdown_pct: object | None,
) -> float:
    raw_value: float
    if max_drawdown_ratio is not None:
        raw_value = float(max_drawdown_ratio)
    elif max_drawdown_pct is not None:
        pct_val = float(max_drawdown_pct)
        raw_value = pct_val / 100.0 if pct_val > 1.0 else pct_val
    else:
        raw_value = 0.0
    return max(0.0, raw_value)


def evaluate_slo_status_for_rows(
    settings: Settings,
    rows: Sequence[dict[str, object]],
    *,
    drawdown_ratio: float | None,
) -> tuple[str, dict[str, float], list[str]]:
    if not rows:
        return "warn", {}, ["no stage7 metrics found"]

    submitted = sum(max(0, int(row.get("oms_submitted_count", 0))) for row in rows)
    rejected = sum(max(0, int(row.get("oms_rejected_count", 0))) for row in rows)
    filled = sum(max(0, int(row.get("oms_filled_count", 0))) for row in rows)
    latencies = [max(0, int(row.get("latency_ms_total", 0))) for row in rows]
    reject_rate = rejected / max(1, submitted)
    fill_rate = filled / max(1, submitted)
    latency_p95_ms = _p95_latency(latencies)
    if drawdown_ratio is None:
        latest = rows[0]
        drawdown_ratio = normalize_drawdown_ratio(
            latest.get("max_drawdown_ratio"),
            latest.get("max_drawdown_pct"),
        )

    metrics = {
        "reject_rate": reject_rate,
        "fill_rate": fill_rate,
        "latency_p95_ms": float(latency_p95_ms),
        "max_drawdown_ratio": float(drawdown_ratio),
    }
    violations: list[str] = []
    status = "pass"

    def bump(next_status: str, message: str) -> None:
        nonlocal status
        violations.append(message)
        if next_status == "fail" or (next_status == "warn" and status == "pass"):
            status = next_status

    if reject_rate > settings.doctor_slo_max_reject_rate_fail:
        bump(
            "fail",
            f"reject_rate={reject_rate:.4f} threshold_fail={settings.doctor_slo_max_reject_rate_fail:.4f}",
        )
    elif reject_rate > settings.doctor_slo_max_reject_rate_warn:
        bump(
            "warn",
            f"reject_rate={reject_rate:.4f} threshold_warn={settings.doctor_slo_max_reject_rate_warn:.4f}",
        )

    if fill_rate < settings.doctor_slo_min_fill_rate_fail:
        bump(
            "fail",
            f"fill_rate={fill_rate:.4f} threshold_fail={settings.doctor_slo_min_fill_rate_fail:.4f}",
        )
    elif fill_rate < settings.doctor_slo_min_fill_rate_warn:
        bump(
            "warn",
            f"fill_rate={fill_rate:.4f} threshold_warn={settings.doctor_slo_min_fill_rate_warn:.4f}",
        )

    if latency_p95_ms > settings.doctor_slo_max_latency_ms_fail:
        bump(
            "fail",
            f"latency_p95_ms={latency_p95_ms} threshold_fail={settings.doctor_slo_max_latency_ms_fail}",
        )
    elif latency_p95_ms > settings.doctor_slo_max_latency_ms_warn:
        bump(
            "warn",
            f"latency_p95_ms={latency_p95_ms} threshold_warn={settings.doctor_slo_max_latency_ms_warn}",
        )

    if drawdown_ratio > settings.doctor_slo_max_drawdown_ratio_fail:
        bump(
            "fail",
            "max_drawdown_ratio="
            f"{drawdown_ratio:.4f} threshold_fail={settings.doctor_slo_max_drawdown_ratio_fail:.4f}",
        )
    elif drawdown_ratio > settings.doctor_slo_max_drawdown_ratio_warn:
        bump(
            "warn",
            "max_drawdown_ratio="
            f"{drawdown_ratio:.4f} threshold_warn={settings.doctor_slo_max_drawdown_ratio_warn:.4f}",
        )
    return status, metrics, violations


def _p95_latency(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) < 3:
        return max(ordered)
    index = int((len(ordered) - 1) * 0.95)
    return ordered[index]


def _run_slo_checks(
    *,
    settings: Settings,
    db_path: str | None,
    checks: list[DoctorCheck],
    warnings: list[str],
    actions: list[str],
) -> None:
    if not settings.doctor_slo_enabled:
        checks.append(DoctorCheck("slo", "enabled", "pass", "slo checks disabled by config"))
        return
    if db_path is None:
        message = "db_path not provided; skipping slo checks"
        warnings.append(message)
        checks.append(DoctorCheck("slo", "coverage", "warn", message))
        return

    store = StateStore(db_path=db_path)
    rows = store.fetch_stage7_run_metrics(limit=settings.doctor_slo_lookback, order_desc=True)
    ledger_metrics = store.get_latest_stage7_ledger_metrics()
    drawdown_ratio = (
        normalize_drawdown_ratio(
            ledger_metrics.get("max_drawdown_ratio") if ledger_metrics is not None else None,
            ledger_metrics.get("max_drawdown_pct") if ledger_metrics is not None else None,
        )
        if ledger_metrics is not None
        else None
    )
    status, metrics, violations = evaluate_slo_status_for_rows(
        settings,
        rows,
        drawdown_ratio=drawdown_ratio,
    )
    if not rows:
        checks.append(DoctorCheck("slo", "coverage", "warn", "no stage7 metrics found"))
        return

    checks.append(
        DoctorCheck(
            "slo",
            "coverage",
            "pass",
            f"metrics present window={len(rows)}/{settings.doctor_slo_lookback}",
        )
    )

    checks.extend(
        [
            _slo_metric_check(
                "reject_rate",
                metrics["reject_rate"],
                warn=settings.doctor_slo_max_reject_rate_warn,
                fail=settings.doctor_slo_max_reject_rate_fail,
                lower_is_better=True,
            ),
            _slo_metric_check(
                "fill_rate",
                metrics["fill_rate"],
                warn=settings.doctor_slo_min_fill_rate_warn,
                fail=settings.doctor_slo_min_fill_rate_fail,
                lower_is_better=False,
            ),
            _slo_metric_check(
                "latency",
                metrics["latency_p95_ms"],
                warn=float(settings.doctor_slo_max_latency_ms_warn),
                fail=float(settings.doctor_slo_max_latency_ms_fail),
                lower_is_better=True,
            ),
            _slo_metric_check(
                "max_drawdown_ratio",
                metrics["max_drawdown_ratio"],
                warn=settings.doctor_slo_max_drawdown_ratio_warn,
                fail=settings.doctor_slo_max_drawdown_ratio_fail,
                lower_is_better=True,
            ),
        ]
    )
    if status == "fail":
        actions.extend(
            [
                "Investigate exchange rejects and order rule mismatches.",
                "Lower strategy aggressiveness and order frequency.",
                "Check network/exchange connectivity for latency spikes.",
            ]
        )
    if violations:
        for detail in violations:
            if "threshold_warn" in detail and status == "warn":
                warnings.append(f"slo warning: {detail}")

    inst = get_instrumentation()
    inst.gauge("doctor_slo_reject_rate", metrics["reject_rate"])
    inst.gauge("doctor_slo_fill_rate", metrics["fill_rate"])
    inst.gauge("doctor_slo_latency_ms", metrics["latency_p95_ms"])
    inst.gauge("doctor_slo_max_drawdown_ratio", metrics["max_drawdown_ratio"])


def _slo_metric_check(
    name: str,
    value: float,
    *,
    warn: float,
    fail: float,
    lower_is_better: bool,
) -> DoctorCheck:
    if lower_is_better:
        if value > fail:
            return DoctorCheck("slo", name, "fail", f"value={value:.4f} threshold_fail={fail:.4f}")
        if value > warn:
            return DoctorCheck("slo", name, "warn", f"value={value:.4f} threshold_warn={warn:.4f}")
    else:
        if value < fail:
            return DoctorCheck("slo", name, "fail", f"value={value:.4f} threshold_fail={fail:.4f}")
        if value < warn:
            return DoctorCheck("slo", name, "warn", f"value={value:.4f} threshold_warn={warn:.4f}")
    return DoctorCheck("slo", name, "pass", f"value={value:.4f}")


def _emit_doctor_telemetry(report: DoctorReport, *, duration_ms: float) -> None:
    inst = get_instrumentation()
    run_status = doctor_status(report)
    inst.counter("doctor_runs_total", attrs={"status": run_status})
    for check in report.checks:
        inst.counter(
            "doctor_checks_total",
            attrs={"category": check.category, "status": check.status},
        )
    inst.histogram("doctor_duration_ms", max(duration_ms, 0.0), attrs={"status": run_status})


def _check_exchange_rules(
    settings: Settings,
    symbols: list[str],
    checks: list[DoctorCheck],
    errors: list[str],
    warnings: list[str],
    actions: list[str],
) -> None:
    exchange = build_exchange_stage4(settings, dry_run=True)
    base_client = getattr(exchange, "client", exchange)
    rules_service = ExchangeRulesService(
        base_client,
        cache_ttl_sec=settings.rules_cache_ttl_sec,
        settings=settings,
    )
    allow_fallback = not bool(getattr(settings, "stage7_rules_require_metadata", True))
    require_metadata = bool(getattr(settings, "stage7_rules_require_metadata", False))
    blocking = bool(getattr(settings, "live_trading", False)) and bool(
        getattr(settings, "stage7_enabled", False)
    )
    if hasattr(settings, "stage7_rules_require_metadata"):
        blocking = blocking and require_metadata
    bad_symbols: list[tuple[str, str]] = []

    get_info = getattr(base_client, "get_exchange_info", None)
    try:
        pairs = get_info() if callable(get_info) else []
        if not pairs:
            checks.append(
                DoctorCheck(
                    "exchange_rules",
                    "symbols_metadata_unavailable",
                    "warn",
                    "exchange info unavailable; could not validate symbol rules",
                )
            )
            actions.extend(
                [
                    "Check BTCTurk public API connectivity and base URL.",
                    "Re-run doctor when exchangeinfo endpoint is reachable.",
                ]
            )
            return

        for symbol in symbols:
            resolution = rules_service.resolve_symbol_rules(symbol)
            status = resolution.status
            if status in {
                "missing",
                "invalid_metadata",
                "unsupported_schema_variant",
                "upstream_fetch_failure",
            }:
                detail = resolution.reason or status
                bad_symbols.append((symbol, f"{status}:{detail}"))
            if status == "fallback" and not allow_fallback:
                detail = resolution.reason or status
                bad_symbols.append((symbol, f"{status}:{detail}"))
    finally:
        close = getattr(exchange, "close", None)
        if callable(close):
            close()

    if bad_symbols:
        for symbol, status in bad_symbols:
            message = f"exchange rules unusable for symbol={symbol} status={status}"
            if not blocking:
                message += " safe_behavior=reject_and_continue"
            check_status = "fail" if blocking else "warn"
            checks.append(
                DoctorCheck("exchange_rules", f"rules_{symbol.lower()}", check_status, message)
            )
            if blocking:
                errors.append(message)
            else:
                warnings.append(message)
        actions.extend(
            [
                "Verify BTCTurk /api/v2/server/exchangeinfo schema and symbol names.",
                "Review invalid_fields/missing_fields details and map new schema variants.",
                "Set STAGE7_RULES_REQUIRE_METADATA=false only as temporary fallback.",
            ]
        )
    else:
        checks.append(
            DoctorCheck(
                "exchange_rules",
                "symbols_metadata",
                "pass",
                f"exchange rules usable for {len(symbols)} configured symbols",
            )
        )


def _merge_dataset_report(
    dataset_report: DatasetValidationReport,
    checks: list[DoctorCheck],
    errors: list[str],
    warnings: list[str],
) -> None:
    if dataset_report.ok:
        checks.append(
            DoctorCheck(
                "backtest_readiness",
                "dataset_contract",
                "pass",
                f"dataset contract validated: {dataset_report.dataset_path}",
            )
        )
        return

    for issue in dataset_report.issues:
        if issue.level == "error":
            errors.append(issue.message)
            checks.append(DoctorCheck("backtest_readiness", issue.code, "fail", issue.message))
        else:
            warnings.append(issue.message)
            checks.append(DoctorCheck("backtest_readiness", issue.code, "warn", issue.message))


def _validate_db_path(*, db_path: str, errors: list[str], warnings: list[str]) -> None:
    db_file = Path(db_path)
    if db_file.exists() and db_file.is_dir():
        errors.append(f"db path points to a directory, expected sqlite file: {db_path}")
        return

    try:
        db_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        errors.append(f"db parent directory is not accessible: {db_file.parent} ({exc})")
        return

    try:
        with sqlite_connection_context(str(db_file)) as conn:
            has_schema_version = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if has_schema_version is None:
                warnings.append(
                    "schema_version table missing (db will be initialized on first StateStore use)"
                )
    except sqlite3.Error as exc:
        errors.append(f"db path is not writable/readable: {db_path} ({exc})")
