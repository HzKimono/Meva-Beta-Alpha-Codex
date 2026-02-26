from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from btcbot.services.state_store import StateStore

_DECIMAL_ZERO = Decimal("0")
_NET_TOLERANCE_TRY = Decimal("0.01")
STAGE7_REPORT_SCHEMA_VERSION = "phase6-v1"


@dataclass(slots=True)
class CycleReportRow:
    ts: str
    cycle_id: str
    run_id: str | None
    mode_base: str
    mode_final: str
    universe_size: int
    gross_pnl_try: Decimal
    net_pnl_try: Decimal
    realized_pnl_try: Decimal
    unrealized_pnl_try: Decimal
    fees_try: Decimal
    funding_cost_try: Decimal
    slippage_try: Decimal
    turnover_try: Decimal
    equity_try: Decimal
    max_drawdown_ratio: Decimal
    max_drawdown_pct: Decimal | None
    rejects: int
    fill_rate: Decimal
    intents_planned_count: int
    oms_submitted_count: int
    oms_filled_count: int
    quality_flags: dict[str, object]
    alert_flags: dict[str, object]


@dataclass(slots=True)
class ValidationFinding:
    code: str
    severity: Literal["error", "warning"]
    message: str
    cycle_id: str | None
    details: dict[str, object]


@dataclass(slots=True)
class RollupBucket:
    period_start: str
    period_end: str
    cycles_count: int
    gross_pnl_try: Decimal
    net_pnl_try: Decimal
    fees_try: Decimal
    slippage_try: Decimal
    turnover_try: Decimal
    rejects: int
    fill_rate_avg: Decimal
    max_drawdown_ratio: Decimal


@dataclass(slots=True)
class RollupReport:
    period: Literal["daily", "weekly"]
    buckets: list[RollupBucket]


def build_cycle_rows(store: StateStore, limit: int) -> list[CycleReportRow]:
    rows = store.fetch_stage7_cycles_for_export(limit=max(0, int(limit)))
    payload: list[CycleReportRow] = []
    for row in rows:
        submitted = _as_int(row.get("oms_submitted_count"))
        filled = _as_int(row.get("oms_filled_count"))
        fill_rate = Decimal(filled) / Decimal(max(1, submitted))
        ratio, pct = _normalize_drawdown_fields(
            raw_ratio=row.get("max_drawdown_ratio"),
            raw_pct=row.get("max_drawdown_pct"),
        )
        payload.append(
            CycleReportRow(
                ts=str(row.get("ts", "")),
                cycle_id=str(row.get("cycle_id", "")),
                run_id=(str(row["run_id"]) if row.get("run_id") not in (None, "") else None),
                mode_base=str(row.get("mode_base", "")),
                mode_final=str(row.get("mode_final", "")),
                universe_size=_as_int(row.get("universe_size")),
                gross_pnl_try=_as_decimal(row.get("gross_pnl_try")),
                net_pnl_try=_as_decimal(row.get("net_pnl_try")),
                realized_pnl_try=_as_decimal(row.get("ledger_realized_pnl_try")),
                unrealized_pnl_try=_as_decimal(row.get("ledger_unrealized_pnl_try")),
                fees_try=_as_decimal(row.get("fees_try")),
                funding_cost_try=_as_decimal(row.get("funding_cost_try")),
                slippage_try=_as_decimal(row.get("slippage_try")),
                turnover_try=_as_decimal(row.get("turnover_try")),
                equity_try=_as_decimal(row.get("equity_try")),
                max_drawdown_ratio=ratio,
                max_drawdown_pct=pct,
                rejects=_as_int(row.get("oms_rejected_count")),
                fill_rate=fill_rate,
                intents_planned_count=_as_int(row.get("intents_planned_count")),
                oms_submitted_count=submitted,
                oms_filled_count=filled,
                quality_flags=_as_dict(row.get("quality_flags")),
                alert_flags=_as_dict(row.get("alert_flags")),
            )
        )
    return payload


def validate_cycle_rows(rows: list[CycleReportRow]) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    last_ts: datetime | None = None
    required = (
        "cycle_id",
        "ts",
        "mode_base",
        "mode_final",
    )
    for row in rows:
        for field_name in required:
            if not getattr(row, field_name):
                findings.append(
                    ValidationFinding(
                        code="missing_critical_field",
                        severity="error",
                        message=f"Missing required field: {field_name}",
                        cycle_id=row.cycle_id or None,
                        details={"field": field_name},
                    )
                )
        try:
            current_ts = _parse_ts_utc(row.ts)
        except ValueError:
            findings.append(
                ValidationFinding(
                    code="invalid_timestamp",
                    severity="error",
                    message="Timestamp is not ISO-8601 parseable.",
                    cycle_id=row.cycle_id or None,
                    details={"ts": row.ts},
                )
            )
            continue

        if last_ts is not None and current_ts > last_ts:
            findings.append(
                ValidationFinding(
                    code="non_monotonic_ts",
                    severity="error",
                    message="Rows are not in monotonic descending timestamp order.",
                    cycle_id=row.cycle_id or None,
                    details={"previous_ts": last_ts.isoformat(), "current_ts": current_ts.isoformat()},
                )
            )
        last_ts = current_ts

        expected_net = row.gross_pnl_try - row.fees_try - row.funding_cost_try - row.slippage_try
        delta = abs(row.net_pnl_try - expected_net)
        if delta > _NET_TOLERANCE_TRY:
            findings.append(
                ValidationFinding(
                    code="net_pnl_identity_mismatch",
                    severity="error",
                    message="net_pnl_try mismatch against gross-fees-slippage",
                    cycle_id=row.cycle_id or None,
                    details={
                        "gross_pnl_try": str(row.gross_pnl_try),
                        "fees_try": str(row.fees_try),
                        "slippage_try": str(row.slippage_try),
                        "funding_cost_try": str(row.funding_cost_try),
                        "net_pnl_try": str(row.net_pnl_try),
                        "expected_net_pnl_try": str(expected_net),
                        "abs_delta_try": str(delta),
                        "tolerance_try": str(_NET_TOLERANCE_TRY),
                    },
                )
            )

        if row.max_drawdown_ratio < _DECIMAL_ZERO or row.max_drawdown_ratio > Decimal("1"):
            findings.append(
                ValidationFinding(
                    code="drawdown_ratio_out_of_range",
                    severity="error",
                    message="max_drawdown_ratio must be in [0,1] after normalization.",
                    cycle_id=row.cycle_id or None,
                    details={"max_drawdown_ratio": str(row.max_drawdown_ratio)},
                )
            )

    return findings


def rollup(rows: list[CycleReportRow], period: Literal["daily", "weekly"], tz: UTC = UTC) -> RollupReport:
    buckets: dict[str, dict[str, object]] = {}
    for row in rows:
        ts = _parse_ts_utc(row.ts).astimezone(tz)
        day_start = datetime(ts.year, ts.month, ts.day, tzinfo=tz)
        if period == "daily":
            start = day_start
            end = day_start.replace(hour=23, minute=59, second=59)
            bucket_key = start.date().isoformat()
        else:
            start = day_start - timedelta(days=day_start.weekday())
            end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
            bucket_key = start.date().isoformat()

        bucket = buckets.setdefault(
            bucket_key,
            {
                "start": start,
                "end": end,
                "cycles_count": 0,
                "gross_pnl_try": _DECIMAL_ZERO,
                "net_pnl_try": _DECIMAL_ZERO,
                "fees_try": _DECIMAL_ZERO,
                "slippage_try": _DECIMAL_ZERO,
                "turnover_try": _DECIMAL_ZERO,
                "rejects": 0,
                "fill_rate_total": _DECIMAL_ZERO,
                "max_drawdown_ratio": _DECIMAL_ZERO,
            },
        )
        bucket["cycles_count"] = int(bucket["cycles_count"]) + 1
        bucket["gross_pnl_try"] = Decimal(bucket["gross_pnl_try"]) + row.gross_pnl_try
        bucket["net_pnl_try"] = Decimal(bucket["net_pnl_try"]) + row.net_pnl_try
        bucket["fees_try"] = Decimal(bucket["fees_try"]) + row.fees_try
        bucket["slippage_try"] = Decimal(bucket["slippage_try"]) + row.slippage_try
        bucket["turnover_try"] = Decimal(bucket["turnover_try"]) + row.turnover_try
        bucket["rejects"] = int(bucket["rejects"]) + row.rejects
        bucket["fill_rate_total"] = Decimal(bucket["fill_rate_total"]) + row.fill_rate
        bucket["max_drawdown_ratio"] = max(Decimal(bucket["max_drawdown_ratio"]), row.max_drawdown_ratio)

    ordered: list[RollupBucket] = []
    for key in sorted(buckets.keys(), reverse=True):
        item = buckets[key]
        cycles_count = int(item["cycles_count"])
        fill_rate_avg = Decimal(item["fill_rate_total"]) / Decimal(max(1, cycles_count))
        ordered.append(
            RollupBucket(
                period_start=item["start"].isoformat(),
                period_end=item["end"].isoformat(),
                cycles_count=cycles_count,
                gross_pnl_try=Decimal(item["gross_pnl_try"]),
                net_pnl_try=Decimal(item["net_pnl_try"]),
                fees_try=Decimal(item["fees_try"]),
                slippage_try=Decimal(item["slippage_try"]),
                turnover_try=Decimal(item["turnover_try"]),
                rejects=int(item["rejects"]),
                fill_rate_avg=fill_rate_avg,
                max_drawdown_ratio=Decimal(item["max_drawdown_ratio"]),
            )
        )

    return RollupReport(period=period, buckets=ordered)


def render_csv(rows: list[CycleReportRow]) -> str:
    columns = [
        "ts",
        "cycle_id",
        "run_id",
        "mode_base",
        "mode_final",
        "universe_size",
        "gross_pnl_try",
        "net_pnl_try",
        "realized_pnl_try",
        "unrealized_pnl_try",
        "fees_try",
        "funding_cost_try",
        "slippage_try",
        "turnover_try",
        "equity_try",
        "max_drawdown_ratio",
        "rejects",
        "fill_rate",
        "intents_planned_count",
        "oms_submitted_count",
        "oms_filled_count",
        "quality_flags_json",
        "alert_flags_json",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "ts": row.ts,
                "cycle_id": row.cycle_id,
                "run_id": row.run_id,
                "mode_base": row.mode_base,
                "mode_final": row.mode_final,
                "universe_size": row.universe_size,
                "gross_pnl_try": str(row.gross_pnl_try),
                "net_pnl_try": str(row.net_pnl_try),
                "realized_pnl_try": str(row.realized_pnl_try),
                "unrealized_pnl_try": str(row.unrealized_pnl_try),
                "fees_try": str(row.fees_try),
                "funding_cost_try": str(row.funding_cost_try),
                "slippage_try": str(row.slippage_try),
                "turnover_try": str(row.turnover_try),
                "equity_try": str(row.equity_try),
                "max_drawdown_ratio": str(row.max_drawdown_ratio),
                "rejects": row.rejects,
                "fill_rate": str(row.fill_rate),
                "intents_planned_count": row.intents_planned_count,
                "oms_submitted_count": row.oms_submitted_count,
                "oms_filled_count": row.oms_filled_count,
                "quality_flags_json": json.dumps(row.quality_flags, sort_keys=True),
                "alert_flags_json": json.dumps(row.alert_flags, sort_keys=True),
            }
        )
    return buf.getvalue()


def render_json(report_obj: object) -> str:
    return json.dumps(_to_jsonable(report_obj), sort_keys=True)


def _to_jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _as_decimal(value: object) -> Decimal:
    if value is None:
        return _DECIMAL_ZERO
    return Decimal(str(value))


def _as_int(value: object) -> int:
    if value is None:
        return 0
    return int(value)


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    return {}


def _parse_ts_utc(raw: str) -> datetime:
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_drawdown_fields(
    *,
    raw_ratio: object | None,
    raw_pct: object | None,
) -> tuple[Decimal, Decimal | None]:
    if raw_ratio not in (None, ""):
        ratio = _as_decimal(raw_ratio)
        if ratio > Decimal("1"):
            # Some legacy rows persisted percentage-like values in ratio columns.
            pct = ratio
            ratio = ratio / Decimal("100")
            return ratio, pct
        pct = ratio * Decimal("100")
        return ratio, pct
    if raw_pct in (None, ""):
        return _DECIMAL_ZERO, None
    pct = _as_decimal(raw_pct)
    ratio = pct / Decimal("100") if pct > Decimal("1") else pct
    return ratio, pct
