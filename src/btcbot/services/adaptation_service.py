from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from btcbot.config import Settings
from btcbot.domain.adaptation_models import ParamChange, Stage7Params
from btcbot.domain.risk_budget import Mode
from btcbot.services.param_bounds import ParamBounds, has_rollback_trigger
from btcbot.services.state_store import StateStore


class AdaptationService:
    _WINDOW = 5
    _SAFE_WINDOW = 3

    def propose_update(
        self,
        *,
        recent_metrics: list[dict[str, object]],
        active_params: Stage7Params,
        settings: Settings,
        now_utc: datetime,
    ) -> tuple[Stage7Params, ParamChange]:
        proposed = active_params
        reasons: list[str] = []
        if not recent_metrics:
            reasons.append("insufficient_metrics")
        else:
            latest = recent_metrics[0]
            alert_flags = dict(latest.get("alert_flags") or {})
            quality_flags = dict(latest.get("quality_flags") or {})
            changed = 0

            if alert_flags.get("reject_spike") and changed < 3:
                proposed = replace(proposed, order_offset_bps=max(0, proposed.order_offset_bps - 2))
                reasons.append("reject_spike_reduce_offset")
                changed += 1
            if alert_flags.get("throttled") and changed < 3:
                proposed = replace(
                    proposed, max_orders_per_cycle=max(1, proposed.max_orders_per_cycle - 1)
                )
                reasons.append("throttled_reduce_order_count")
                changed += 1
            drawdown = Decimal(str(latest.get("max_drawdown_pct", "0")))
            if (
                drawdown >= (Decimal(str(settings.stage7_max_drawdown_pct)) * Decimal("0.8"))
                and changed < 3
            ):
                proposed = replace(
                    proposed,
                    turnover_cap_try=(proposed.turnover_cap_try * Decimal("0.95")),
                )
                reasons.append("high_drawdown_reduce_turnover")
                changed += 1
            if quality_flags.get("missing_mark_price") and changed < 3:
                proposed = replace(
                    proposed,
                    max_spread_bps=max(10, proposed.max_spread_bps - 10),
                    min_quote_volume_try=(proposed.min_quote_volume_try + Decimal("100")),
                )
                reasons.append("low_liquidity_tighten_filters")
                changed += 1

            healthy = recent_metrics[: self._SAFE_WINDOW]
            healthy_window = len(healthy) == self._SAFE_WINDOW and all(
                str(item.get("mode_final")) == Mode.NORMAL.value
                and not any(bool(v) for v in dict(item.get("alert_flags") or {}).values())
                for item in healthy
            )
            if healthy_window and changed == 0:
                proposed = replace(
                    proposed,
                    universe_size=proposed.universe_size + 1,
                    turnover_cap_try=(proposed.turnover_cap_try * Decimal("1.05")),
                )
                reasons.append("healthy_window_expand_small")
                changed += 1

        bounded = ParamBounds.apply_bounds(proposed, settings)
        if bounded == active_params:
            reason = "no_change"
        else:
            reason = reasons[0] if reasons else "heuristic_update"
        changes = _diff_params(active_params, bounded)
        next_version = active_params.version + (1 if changes else 0)
        candidate = replace(bounded, version=next_version, updated_at=now_utc)
        window_summary = _metrics_summary(recent_metrics)
        change = ParamChange(
            change_id=_build_change_id(
                ts=now_utc,
                from_version=active_params.version,
                to_version=next_version,
                params=candidate,
            ),
            ts=now_utc,
            from_version=active_params.version,
            to_version=next_version,
            changes=changes,
            reason=reason,
            metrics_window=window_summary,
            outcome="REJECTED",
            notes=reasons,
        )
        return candidate, change

    def evaluate_and_apply(
        self,
        *,
        state_store: StateStore,
        settings: Settings,
        now_utc: datetime,
    ) -> ParamChange | None:
        active = state_store.get_active_stage7_params(settings=settings, now_utc=now_utc)
        recent = state_store.fetch_stage7_run_metrics(limit=self._WINDOW, order_desc=True)
        if not recent:
            return None
        if has_rollback_trigger(recent_metrics=recent):
            state_store.set_stage7_checkpoint_goodness(version=active.version, is_good=False)
            checkpoint = state_store.get_last_good_stage7_params_checkpoint()
            if checkpoint is None:
                return None
            if checkpoint.version == active.version:
                checkpoint = state_store.get_previous_good_stage7_params_checkpoint(
                    before_version=active.version
                )
                if checkpoint is None:
                    return None
            rollback_change = ParamChange(
                change_id=_build_change_id(
                    ts=now_utc,
                    from_version=active.version,
                    to_version=checkpoint.version,
                    params=checkpoint,
                ),
                ts=now_utc,
                from_version=active.version,
                to_version=checkpoint.version,
                changes=_diff_params(active, checkpoint),
                reason="guardrail_breach_rollback",
                metrics_window=_metrics_summary(recent),
                outcome="ROLLED_BACK",
                notes=["rollback_triggered"],
            )
            state_store.set_active_stage7_params(checkpoint, rollback_change)
            return rollback_change

        candidate, change = self.propose_update(
            recent_metrics=recent,
            active_params=active,
            settings=settings,
            now_utc=now_utc,
        )
        latest = recent[0]
        if str(latest.get("mode_final")) != Mode.NORMAL.value:
            rejected = replace(change, outcome="REJECTED", reason="mode_not_normal")
            state_store.record_stage7_param_change(rejected)
            return rejected
        flags = dict(latest.get("alert_flags") or {})
        if any(bool(flags.get(name)) for name in ("drawdown_breach", "reject_spike", "throttled")):
            rejected = replace(change, outcome="REJECTED", reason="recent_breach_flags")
            state_store.record_stage7_param_change(rejected)
            return rejected
        if not change.changes:
            rejected = replace(change, outcome="REJECTED", reason="no_bounded_change")
            state_store.record_stage7_param_change(rejected)
            return rejected

        applied = replace(change, outcome="APPLIED", ts=now_utc)
        state_store.set_active_stage7_params(replace(candidate, updated_at=now_utc), applied)
        return applied


def _metrics_summary(recent_metrics: list[dict[str, object]]) -> dict[str, str]:
    if not recent_metrics:
        return {}
    count = len(recent_metrics)
    pnl_values = [Decimal(str(item.get("net_pnl_try", "0"))) for item in recent_metrics]
    drawdowns = [Decimal(str(item.get("max_drawdown_pct", "0"))) for item in recent_metrics]
    return {
        "cycles": str(count),
        "avg_net_pnl_try": str(sum(pnl_values, Decimal("0")) / Decimal(str(count))),
        "max_drawdown_pct": str(max(drawdowns, default=Decimal("0"))),
        "rejects_total": str(
            sum(int(item.get("oms_rejected_count", 0)) for item in recent_metrics)
        ),
        "throttled_total": str(
            sum(int(item.get("oms_throttled_count", 0)) for item in recent_metrics)
        ),
    }


def _build_change_id(
    *, ts: datetime, from_version: int, to_version: int, params: Stage7Params
) -> str:
    ts_part = ts.astimezone(UTC).strftime("%Y%m%d%H%M%S")
    payload = json.dumps(params.to_dict(), sort_keys=True, separators=(",", ":"))
    short_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    return f"s7chg:{ts_part}:{from_version}->{to_version}:{short_hash}"


def _diff_params(old: Stage7Params, new: Stage7Params) -> dict[str, dict[str, str]]:
    old_map = old.to_dict()
    new_map = new.to_dict()
    fields = (
        "universe_size",
        "score_weights",
        "order_offset_bps",
        "turnover_cap_try",
        "max_orders_per_cycle",
        "max_spread_bps",
        "cash_target_try",
        "min_quote_volume_try",
    )
    diff: dict[str, dict[str, str]] = {}
    for field in fields:
        if old_map[field] != new_map[field]:
            diff[field] = {"old": str(old_map[field]), "new": str(new_map[field])}
    return diff
