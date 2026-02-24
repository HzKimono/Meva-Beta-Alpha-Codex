from __future__ import annotations

import json
import logging
import sys

from btcbot.logging_utils import JsonFormatter, setup_logging


def test_json_formatter_includes_exception_details() -> None:
    formatter = JsonFormatter()

    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="btcbot.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Cycle failed",
            args=(),
            exc_info=sys.exc_info(),
        )
        rendered = formatter.format(record)

    payload = json.loads(rendered)
    assert payload["message"] == "Cycle failed"
    assert payload["error_type"] == "ValueError"
    assert payload["error_message"] == "boom"
    assert "ValueError: boom" in payload["traceback"]


def test_setup_logging_uses_log_level_env(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    setup_logging()

    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_defaults_http_loggers_for_info() -> None:
    setup_logging("INFO")

    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_setup_logging_debug_enables_http_debug() -> None:
    setup_logging("DEBUG")

    assert logging.getLogger("httpx").level == logging.DEBUG
    assert logging.getLogger("httpcore").level == logging.DEBUG


def test_setup_logging_respects_http_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("HTTPX_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("HTTPCORE_LOG_LEVEL", "CRITICAL")

    setup_logging("INFO")

    assert logging.getLogger("httpx").level == logging.ERROR
    assert logging.getLogger("httpcore").level == logging.CRITICAL


def test_json_formatter_includes_correlation_fields_even_when_unset() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="btcbot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert "run_id" in payload
    assert "cycle_id" in payload
    assert "client_order_id" in payload
    assert "order_id" in payload
    assert "symbol" in payload


def test_json_formatter_info_redacts_token_in_output() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="btcbot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token=abcd1234SECRETzz",
        args=(),
        exc_info=None,
    )
    payload = formatter.format(record)
    assert "abcd1234SECRETzz" not in payload


def test_json_formatter_exception_redacts_traceback_message() -> None:
    formatter = JsonFormatter()
    try:
        raise RuntimeError("Authorization: Bearer TOPSECRET123456")
    except RuntimeError:
        record = logging.LogRecord(
            name="btcbot.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failure",
            args=(),
            exc_info=sys.exc_info(),
        )
    rendered = formatter.format(record)
    assert "TOPSECRET123456" not in rendered


def test_json_formatter_redacts_non_record_extra_fields() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="btcbot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ok",
        args=(),
        exc_info=None,
    )
    record.authorization = "Bearer SUPERSECRET"
    rendered = formatter.format(record)
    assert "SUPERSECRET" not in rendered

