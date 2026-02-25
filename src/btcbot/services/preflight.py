from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from btcbot.adapters.btcturk_http import BtcturkHttpClient
from btcbot.config import Settings
from btcbot.obs.process_role import ProcessRole
from btcbot.services.state_store import StateStore


def _default_private_auth_check(settings: Settings) -> tuple[bool, str]:
    client = BtcturkHttpClient(
        api_key=settings.btcturk_api_key.get_secret_value() if settings.btcturk_api_key else None,
        api_secret=settings.btcturk_api_secret.get_secret_value()
        if settings.btcturk_api_secret
        else None,
        base_url=settings.btcturk_base_url,
    )
    try:
        _ = client.get_balances()
        return True, "private_api_ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"private_api_error:{type(exc).__name__}:{exc}"
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def run_preflight_checks(
    *,
    settings: Settings,
    db_path: str | None = None,
    profile: str = "live",
    auth_check: Callable[[Settings], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    profile_normalized = str(profile).strip().lower()
    is_live_profile = profile_normalized == "live"
    resolved_db_path = str(Path(db_path or settings.state_db_path).expanduser())
    checks: list[dict[str, Any]] = []

    store: StateStore | None = None
    db_ok = True
    db_detail = "ok"
    try:
        store = StateStore(resolved_db_path, strict_instance_lock=True)
        with store.transaction() as conn:
            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO op_state(key, int_value, text_value, updated_at)
                VALUES ('preflight_probe', 1, ?, ?)
                ON CONFLICT(key) DO UPDATE SET text_value=excluded.text_value, updated_at=excluded.updated_at
                """,
                (json.dumps({"ts": now}, sort_keys=True), now),
            )
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_detail = f"db_lock_or_write_error:{type(exc).__name__}:{exc}"
    checks.append({"name": "db_lock_and_writable", "ok": db_ok, "detail": db_detail})

    live_armed_by_env = bool(settings.is_live_trading_enabled() and not settings.dry_run)
    checks.append(
        {
            "name": "live_env_armed",
            "ok": (live_armed_by_env if is_live_profile else True),
            "detail": (
                "ok"
                if live_armed_by_env or not is_live_profile
                else "requires LIVE_TRADING=true and LIVE_TRADING_ACK=I_UNDERSTAND and DRY_RUN=false"
            ),
        }
    )
    checks.append(
        {
            "name": "safe_mode_disabled",
            "ok": (not settings.safe_mode if is_live_profile else True),
            "detail": (
                "ok"
                if (not settings.safe_mode) or not is_live_profile
                else "SAFE_MODE must be false for live profile"
            ),
        }
    )
    checks.append(
        {
            "name": "kill_switch_disabled_in_settings",
            "ok": (not settings.kill_switch if is_live_profile else True),
            "detail": (
                "ok"
                if (not settings.kill_switch) or not is_live_profile
                else "KILL_SWITCH must be false for live profile"
            ),
        }
    )

    freeze_ok = True
    freeze_detail = "ok"
    if store is not None and is_live_profile:
        freeze = store.stage4_get_freeze(ProcessRole.LIVE.value)
        freeze_ok = not freeze.active
        if not freeze_ok:
            freeze_detail = f"active:{freeze.reason or 'unknown_reason'}"
    checks.append({"name": "stage4_freeze_inactive_live", "ok": freeze_ok, "detail": freeze_detail})

    db_kill_ok = True
    db_kill_detail = "ok"
    if store is not None and is_live_profile:
        enabled, reason, _until = store.get_kill_switch(ProcessRole.LIVE.value)
        db_kill_ok = not enabled
        if enabled:
            db_kill_detail = f"enabled:{reason or 'no_reason'}"
    checks.append({"name": "db_kill_switch_inactive_live", "ok": db_kill_ok, "detail": db_kill_detail})

    creds_ok = bool(settings.btcturk_api_key and settings.btcturk_api_secret)
    checks.append(
        {
            "name": "credentials_present",
            "ok": (creds_ok if is_live_profile else True),
            "detail": "ok" if creds_ok or not is_live_profile else "BTCTURK_API_KEY/SECRET missing",
        }
    )

    auth_fn = auth_check or _default_private_auth_check
    auth_ok = True
    auth_detail = "skipped_for_profile"
    if is_live_profile:
        if creds_ok:
            auth_ok, auth_detail = auth_fn(settings)
        else:
            auth_ok = False
            auth_detail = "credentials_missing"
    checks.append({"name": "private_api_auth", "ok": auth_ok, "detail": auth_detail})

    caps_ok = bool(
        settings.max_orders_per_cycle > 0
        and settings.try_cash_target > 0
        and settings.notional_cap_try_per_cycle > 0
        and settings.max_notional_per_order_try > 0
    )
    checks.append(
        {
            "name": "hard_caps_configured",
            "ok": caps_ok,
            "detail": (
                "ok"
                if caps_ok
                else "max_orders_per_cycle/try_cash_target/notional_cap_try_per_cycle/max_notional_per_order_try must be > 0"
            ),
        }
    )

    metrics_ok = bool(settings.observability_metrics_exporter in {"none", "otlp", "prometheus"})
    checks.append(
        {
            "name": "monitoring_config",
            "ok": metrics_ok,
            "detail": "ok" if metrics_ok else "unsupported OBSERVABILITY_METRICS_EXPORTER",
        }
    )

    passed = all(bool(check["ok"]) for check in checks)
    return {
        "profile": "live" if is_live_profile else "dry-run",
        "db_path": resolved_db_path,
        "passed": passed,
        "checks": checks,
        "ts": datetime.now(UTC).isoformat(),
    }
