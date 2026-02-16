from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from btcbot.agent.contracts import AgentContext, AgentDecision, SafeDecision
from btcbot.services.state_store import StateStore

SENSITIVE_KEYWORDS = ("secret", "token", "password", "api_key", "apikey", "auth")


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if any(word in key.lower() for word in SENSITIVE_KEYWORDS):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = redact_secrets(item)
        return sanitized
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


@dataclass(frozen=True)
class AgentAuditTrail:
    state_store: StateStore
    include_prompt_payloads: bool = False
    max_payload_chars: int = 4000

    def persist(
        self,
        *,
        cycle_id: str,
        correlation_id: str,
        context: AgentContext,
        decision: AgentDecision,
        safe_decision: SafeDecision,
        prompt: str | None = None,
        response: str | None = None,
    ) -> None:
        context_payload = redact_secrets(context.model_dump(mode="json"))
        decision_payload = redact_secrets(decision.model_dump(mode="json"))
        safe_payload = redact_secrets(safe_decision.model_dump(mode="json"))

        prompt_payload = None
        response_payload = None
        if self.include_prompt_payloads:
            prompt_payload = (prompt or "")[: self.max_payload_chars]
            response_payload = (response or "")[: self.max_payload_chars]

        diff_hash = sha256(
            json.dumps(safe_payload.get("diff", {}), sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.state_store.persist_agent_decision_audit(
            cycle_id=cycle_id,
            correlation_id=correlation_id,
            context_json=json.dumps(context_payload, sort_keys=True),
            decision_json=json.dumps(decision_payload, sort_keys=True),
            safe_decision_json=json.dumps(safe_payload, sort_keys=True),
            diff_json=json.dumps(safe_payload.get("diff", {}), sort_keys=True),
            diff_hash=diff_hash,
            prompt_json=prompt_payload,
            response_json=response_payload,
        )
