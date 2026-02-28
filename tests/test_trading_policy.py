from __future__ import annotations

import pytest

from btcbot.domain.decision_codes import ReasonCode
from btcbot.services.trading_policy import policy_reason_to_code, validate_live_side_effects_policy


def _expected_reasons(
    *, dry_run: bool, kill_switch: bool, live_trading_enabled: bool, live_trading_ack: bool
) -> list[str]:
    reasons: list[str] = []
    if kill_switch:
        reasons.append("KILL_SWITCH")
    if dry_run:
        reasons.append("DRY_RUN")
    if not live_trading_enabled:
        reasons.append("NOT_ARMED")
    if not live_trading_ack:
        reasons.append("ACK_MISSING")
    return reasons


def _case_id(
    dry_run: bool, kill_switch: bool, live_trading_enabled: bool, live_trading_ack: bool
) -> str:
    return (
        f"dry_run={int(dry_run)}|kill_switch={int(kill_switch)}|"
        f"live={int(live_trading_enabled)}|ack={int(live_trading_ack)}"
    )


_TRUTH_TABLE_CASES = [
    pytest.param(
        dry_run,
        kill_switch,
        live_trading_enabled,
        live_trading_ack,
        id=_case_id(dry_run, kill_switch, live_trading_enabled, live_trading_ack),
    )
    for dry_run in (False, True)
    for kill_switch in (False, True)
    for live_trading_enabled in (False, True)
    for live_trading_ack in (False, True)
]


@pytest.mark.parametrize(
    ("dry_run", "kill_switch", "live_trading_enabled", "live_trading_ack"),
    _TRUTH_TABLE_CASES,
)
def test_validate_live_side_effects_policy_truth_table(
    dry_run: bool,
    kill_switch: bool,
    live_trading_enabled: bool,
    live_trading_ack: bool,
) -> None:
    result = validate_live_side_effects_policy(
        dry_run=dry_run,
        kill_switch=kill_switch,
        live_trading_enabled=live_trading_enabled,
        live_trading_ack=live_trading_ack,
    )

    assert result.allowed is (
        (not dry_run) and (not kill_switch) and live_trading_enabled and live_trading_ack
    )
    assert result.reasons == _expected_reasons(
        dry_run=dry_run,
        kill_switch=kill_switch,
        live_trading_enabled=live_trading_enabled,
        live_trading_ack=live_trading_ack,
    )


def test_kill_switch_reason_is_preserved_alongside_other_failures() -> None:
    result = validate_live_side_effects_policy(
        dry_run=True,
        kill_switch=True,
        live_trading_enabled=False,
        live_trading_ack=False,
    )

    assert result.allowed is False
    assert "KILL_SWITCH" in result.reasons
    assert result.reasons == ["KILL_SWITCH", "DRY_RUN", "NOT_ARMED", "ACK_MISSING"]
    assert "KILL_SWITCH=true blocks side effects" in result.message


def test_policy_reasons_map_to_canonical_reason_codes() -> None:
    result = validate_live_side_effects_policy(
        dry_run=True,
        kill_switch=True,
        live_trading_enabled=False,
        live_trading_ack=False,
    )
    codes = [policy_reason_to_code(reason) for reason in result.reasons]
    assert codes == [
        ReasonCode.POLICY_BLOCK_KILL_SWITCH,
        ReasonCode.POLICY_BLOCK_DRY_RUN,
        ReasonCode.POLICY_BLOCK_NOT_ARMED,
        ReasonCode.POLICY_BLOCK_ACK_MISSING,
    ]


def test_monitor_role_always_blocks_side_effects() -> None:
    result = validate_live_side_effects_policy(
        process_role="MONITOR",
        enforce_monitor_role=True,
        dry_run=False,
        kill_switch=False,
        live_trading_enabled=True,
        live_trading_ack=True,
    )

    assert result.allowed is False
    assert result.reasons == ["MONITOR_ROLE"]
    assert result.message == "MONITOR role blocks side effects"
    assert policy_reason_to_code(result.reasons[0]) == ReasonCode.POLICY_BLOCK_MONITOR_ROLE
