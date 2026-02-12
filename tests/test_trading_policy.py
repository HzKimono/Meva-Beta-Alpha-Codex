from __future__ import annotations

from btcbot.services.trading_policy import PolicyBlockReason, validate_live_side_effects_policy


def test_validate_live_side_effects_policy_blocks_when_not_armed() -> None:
    reason = validate_live_side_effects_policy(
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=False,
    )

    assert reason is PolicyBlockReason.LIVE_NOT_ARMED


def test_validate_live_side_effects_policy_allows_only_fully_armed() -> None:
    reason = validate_live_side_effects_policy(
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
    )

    assert reason is None


def test_validate_live_side_effects_policy_blocks_when_dry_run_even_if_live_armed() -> None:
    reason = validate_live_side_effects_policy(
        dry_run=True,
        kill_switch=False,
        live_trading_enabled=True,
    )

    assert reason is PolicyBlockReason.DRY_RUN


def test_validate_live_side_effects_policy_blocks_when_kill_switch_even_if_live_armed() -> None:
    reason = validate_live_side_effects_policy(
        dry_run=False,
        kill_switch=True,
        live_trading_enabled=True,
    )

    assert reason is PolicyBlockReason.KILL_SWITCH
