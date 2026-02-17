from __future__ import annotations

import json
import logging
from decimal import Decimal

from btcbot import cli
from btcbot.logging_utils import JsonFormatter
from btcbot.security.redaction import sanitize_mapping, sanitize_text
from btcbot.services.doctor import DoctorReport

FAKE_KEY = "AK_test_1234567890"
FAKE_SECRET = "SK_test_ABCDEFGHIJ"


def test_sanitize_mapping_redacts_sensitive_keys() -> None:
    payload = {
        "API_KEY": FAKE_KEY,
        "nested": {"authorization": f"Bearer {FAKE_SECRET}", "safe": 1},
    }

    sanitized = sanitize_mapping(payload)

    assert sanitized["API_KEY"] != FAKE_KEY
    assert sanitized["nested"]["authorization"] != f"Bearer {FAKE_SECRET}"
    assert sanitized["nested"]["safe"] == 1


def test_sanitize_text_redacts_exact_known_secrets_and_headers() -> None:
    text = f"Authorization: Bearer {FAKE_SECRET} X-API-KEY: {FAKE_KEY} raw={FAKE_SECRET}"

    sanitized = sanitize_text(text, known_secrets=[FAKE_KEY, FAKE_SECRET])

    assert FAKE_KEY not in sanitized
    assert FAKE_SECRET not in sanitized
    assert "Authorization: Bearer [REDACTED]" in sanitized


def test_json_formatter_redacts_sensitive_log_content() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="btcbot.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Authorization: Bearer %s",
        args=(FAKE_SECRET,),
        exc_info=None,
    )

    output = formatter.format(record)

    assert FAKE_SECRET not in output
    assert "[REDACTED]" in output


def test_cli_json_outputs_doctor_and_canary_are_sanitized(monkeypatch, capsys) -> None:
    monkeypatch.setenv("BTCTURK_API_KEY", FAKE_KEY)
    monkeypatch.setenv("BTCTURK_API_SECRET", FAKE_SECRET)

    settings = cli.Settings(_env_file=None, STATE_DB_PATH=":memory:")
    monkeypatch.setattr(
        cli,
        "run_health_checks",
        lambda *args, **kwargs: DoctorReport(checks=[], errors=[], warnings=[], actions=[]),
    )
    rc = cli.run_doctor(settings, db_path=":memory:", dataset_path=None, json_output=True)
    assert rc == 0
    doctor_out = capsys.readouterr().out
    assert FAKE_KEY not in doctor_out
    assert FAKE_SECRET not in doctor_out

    monkeypatch.setattr(cli, "single_instance_lock", lambda **kwargs: __import__("contextlib").nullcontext())
    monkeypatch.setattr(cli, "_resolve_canary_symbol", lambda *_args, **_kwargs: "BTCTRY")
    monkeypatch.setattr(cli, "_check_canary_min_notional", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(cli, "_run_canary_doctor_gate", lambda *_args, **_kwargs: ("ok", 0))
    monkeypatch.setattr(cli, "_print_effective_side_effects_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_compute_live_policy", lambda *args, **kwargs: (None, type("P", (), {"allowed": True})()))
    monkeypatch.setattr(cli, "run_cycle", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cli, "_print_canary_evidence_commands", lambda *_args, **_kwargs: None)

    rc_canary = cli.run_canary(
        settings,
        mode="once",
        symbol="BTCTRY",
        notional_try=Decimal("10"),
        cycle_seconds=0,
        max_cycles=1,
        ttl_seconds=30,
        db_path=":memory:",
        market_data_mode=None,
        allow_warn=True,
        export_out=None,
        json_output=True,
    )
    assert rc_canary == 0
    canary_out = capsys.readouterr().out
    assert FAKE_KEY not in canary_out
    assert FAKE_SECRET not in canary_out
    payload = json.loads(canary_out)
    assert payload["final_doctor_status"] == "OK"
