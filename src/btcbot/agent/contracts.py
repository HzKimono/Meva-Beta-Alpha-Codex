from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DecisionAction(StrEnum):
    PROPOSE_INTENTS = "propose_intents"
    ADJUST_RISK = "adjust_risk"
    OBSERVE_ONLY = "observe_only"
    NO_OP = "no_op"


class OrderIntentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    side: str
    notional_try: Decimal = Field(gt=Decimal("0"))
    qty: Decimal = Field(gt=Decimal("0"))
    price_try: Decimal = Field(gt=Decimal("0"))
    reason: str = Field(min_length=1, max_length=256)
    client_order_id: str | None = Field(default=None, min_length=4, max_length=64)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.replace("_", "").upper().strip()

    @field_validator("side")
    @classmethod
    def normalize_side(cls, value: str) -> str:
        side = value.upper().strip()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return side


class DecisionRationale(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reasons: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    constraints_hit: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: DecisionAction
    propose_intents: list[OrderIntentProposal] = Field(default_factory=list)
    adjust_risk: dict[str, Decimal | str | bool] = Field(default_factory=dict)
    observe_only: bool = False
    rationale: DecisionRationale


class AgentContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle_id: str
    generated_at: datetime
    market_snapshot: dict[str, Decimal]
    market_spreads_bps: dict[str, Decimal] = Field(default_factory=dict)
    market_data_age_seconds: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    portfolio: dict[str, Decimal]
    open_orders: list[dict[str, str]]
    risk_state: dict[str, Decimal | str | bool]
    recent_events: list[str] = Field(default_factory=list)
    started_at: datetime
    is_live_mode: bool


class SafeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: AgentDecision
    blocked_reasons: list[str] = Field(default_factory=list)
    dropped_symbols: list[str] = Field(default_factory=list)
    observe_only_override: bool = False
    diff: dict[str, object] = Field(default_factory=dict)


class LlmDecisionEnvelope(BaseModel):
    """Strict output schema expected from LLM responses."""

    model_config = ConfigDict(extra="forbid")

    action: DecisionAction
    propose_intents: list[OrderIntentProposal] = Field(default_factory=list)
    adjust_risk: dict[str, Decimal | str | bool] = Field(default_factory=dict)
    observe_only: bool = False
    rationale: DecisionRationale
