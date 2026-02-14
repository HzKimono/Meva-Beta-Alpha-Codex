from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from btcbot.config import Settings


@dataclass(frozen=True)
class DoctorReport:
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def run_health_checks(
    settings: Settings,
    *,
    db_path: str | None,
    dataset_path: str | None,
) -> DoctorReport:
    errors: list[str] = []
    warnings: list[str] = []

    if settings.stage7_enabled and not settings.dry_run:
        errors.append("STAGE7_ENABLED requires DRY_RUN=true")
    if settings.stage7_enabled and settings.live_trading:
        errors.append("STAGE7_ENABLED requires LIVE_TRADING=false")

    if settings.live_trading and not settings.is_live_trading_enabled():
        errors.append("LIVE_TRADING=true but LIVE_TRADING_ACK is not set to I_UNDERSTAND")
    if settings.live_trading and settings.btcturk_api_key is None:
        errors.append("LIVE_TRADING=true but BTCTURK_API_KEY is missing")
    if settings.live_trading and settings.btcturk_api_secret is None:
        errors.append("LIVE_TRADING=true but BTCTURK_API_SECRET is missing")

    if settings.kill_switch and settings.live_trading:
        warnings.append(
            "KILL_SWITCH=true with LIVE_TRADING=true will block writes "
            "until kill switch is disabled"
        )

    if dataset_path is not None:
        dataset_root = Path(dataset_path)
        if not dataset_root.exists():
            errors.append(f"dataset path does not exist: {dataset_path}")
        elif not dataset_root.is_dir():
            errors.append(f"dataset path is not a directory: {dataset_path}")
        else:
            for required_folder in ("candles", "orderbook", "ticker"):
                candidate = dataset_root / required_folder
                if not candidate.exists():
                    warnings.append(
                        f"dataset folder missing '{required_folder}' "
                        "(some replays may be incomplete)"
                    )

    if db_path is not None:
        _validate_db_path(db_path=db_path, errors=errors, warnings=warnings)

    return DoctorReport(errors=errors, warnings=warnings)


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
