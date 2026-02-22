from __future__ import annotations

from btcbot.domain.risk_budget import Mode
from btcbot.domain.risk_mode_codec import dump_risk_mode, parse_risk_mode


def test_parse_risk_mode_roundtrip() -> None:
    for risk_mode in (Mode.NORMAL, Mode.REDUCE_RISK_ONLY, Mode.OBSERVE_ONLY):
        assert parse_risk_mode(dump_risk_mode(risk_mode)) == risk_mode


def test_parse_risk_mode_unknown() -> None:
    assert parse_risk_mode("NOT_A_MODE") is None
