from __future__ import annotations

import os
from pathlib import Path

DOTENV_DISABLED_MESSAGE = (
    "Dotenv bootstrap is disabled for production PowerShell runs. "
    "Remove --env-file and unset SETTINGS_ENV_FILE, then provide configuration via "
    "environment variables (for example: $env:STATE_DB_PATH='C:\\bot\\live_state.db')."
)


def require_no_dotenv(env_file_arg: str | None) -> None:
    env_file = os.getenv("SETTINGS_ENV_FILE")
    if env_file_arg is not None or env_file:
        raise ValueError(DOTENV_DISABLED_MESSAGE)


def normalize_db_path(raw: str) -> Path:
    candidate = raw.strip()
    if not candidate:
        raise ValueError(
            "STATE_DB_PATH is required and cannot be empty. "
            "Use a .db path in PowerShell, e.g. "
            "$env:STATE_DB_PATH='C:\\btcbot\\live\\state_live.db'."
        )

    path = Path(candidate).expanduser()
    if not path.is_absolute() and candidate.startswith("/"):
        path = Path(candidate)
    if not path.is_absolute():
        path = path.resolve()

    if path.suffix.lower() != ".db":
        raise ValueError("STATE_DB_PATH must end with '.db'.")

    path.parent.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def enforce_role_db_convention(process_role: str, live_trading: bool, db_path: Path) -> None:
    normalized_role = (process_role or "").strip().lower()
    db_lower = str(db_path).lower()
    live_role = "live" in normalized_role or live_trading

    if live_role:
        if "live" not in db_lower:
            raise ValueError(
                "Invalid DB path for LIVE role/process. "
                f"role={process_role!r} db_path='{db_path}'. "
                "LIVE trading processes must use a DB path containing 'live'."
            )
        return

    if "monitor" not in db_lower:
        raise ValueError(
            "Invalid DB path for MONITOR role/process. "
            f"role={process_role!r} db_path='{db_path}'. "
            "Monitor processes must use a DB path containing 'monitor'."
        )
