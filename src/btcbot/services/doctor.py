from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from btcbot.config import Settings
from btcbot.replay.validate import DatasetValidationReport, validate_replay_dataset
from btcbot.services.exchange_factory import build_exchange_stage4
from btcbot.services.exchange_rules_service import ExchangeRulesService


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


def run_health_checks(
    settings: Settings,
    *,
    db_path: str | None,
    dataset_path: str | None,
) -> DoctorReport:
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

    _check_exchange_rules(settings, checks, errors, warnings, actions)

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

    report = DoctorReport(checks=checks, errors=errors, warnings=warnings, actions=actions)
    derived_ok = all(check.status != "fail" for check in report.checks)
    if report.ok != derived_ok:
        summary = ", ".join(
            f"{check.category}/{check.name}:{check.status}" for check in report.checks
        )
        raise RuntimeError(f"doctor report ok invariant violated: {summary}")

    return report


def _check_exchange_rules(
    settings: Settings,
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

        for symbol in settings.symbols:
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
                f"exchange rules usable for {len(settings.symbols)} configured symbols",
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
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            has_schema_version = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if has_schema_version is None:
                warnings.append(
                    "schema_version table missing (db will be initialized on first StateStore use)"
                )
    except sqlite3.Error as exc:
        errors.append(f"db path is not writable/readable: {db_path} ({exc})")
