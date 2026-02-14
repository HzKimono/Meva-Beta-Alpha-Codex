from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from btcbot.config import Settings
from btcbot.replay.validate import DatasetValidationReport, validate_replay_dataset


@dataclass(frozen=True)
class DoctorCheck:
    category: str
    name: str
    status: str
    message: str


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]
    errors: list[str]
    warnings: list[str]
    actions: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


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

    return DoctorReport(checks=checks, errors=errors, warnings=warnings, actions=actions)


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
