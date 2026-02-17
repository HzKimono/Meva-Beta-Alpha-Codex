from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from btcbot.agent.contracts import AgentContext, AgentDecision, SafeDecision
from btcbot.security.redaction import redact_data
from btcbot.services.state_store import StateStore


def redact_secrets(value: Any) -> Any:
    return redact_data(value)


def store_compact_text(text: str, *, max_chars: int) -> dict[str, object]:
    digest = sha256(text.encode("utf-8")).hexdigest()
    if len(text) <= max_chars:
        return {
            "truncated": False,
            "sha256": digest,
            "chars": len(text),
            "text": text,
        }
    head_len = max(0, max_chars // 2)
    tail_len = max(0, max_chars - head_len)
    return {
        "truncated": True,
        "sha256": digest,
        "chars": len(text),
        "head": text[:head_len],
        "tail": text[-tail_len:] if tail_len > 0 else "",
    }


def store_compact_json(payload: dict[str, object], *, max_chars: int) -> dict[str, object]:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    compact = store_compact_text(text, max_chars=max_chars)
    compact["is_json"] = True
    return compact


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

        compact_context = store_compact_json(context_payload, max_chars=self.max_payload_chars)
        compact_decision = store_compact_json(decision_payload, max_chars=self.max_payload_chars)
        compact_safe = store_compact_json(safe_payload, max_chars=self.max_payload_chars)
        compact_diff = store_compact_json(
            redact_secrets(safe_payload.get("diff", {})),
            max_chars=self.max_payload_chars,
        )

        prompt_payload = None
        response_payload = None
        if self.include_prompt_payloads:
            prompt_payload = json.dumps(
                store_compact_text(prompt or "", max_chars=self.max_payload_chars), sort_keys=True
            )
            response_payload = json.dumps(
                store_compact_text(response or "", max_chars=self.max_payload_chars), sort_keys=True
            )

        self.state_store.persist_agent_decision_audit(
            cycle_id=cycle_id,
            correlation_id=correlation_id,
            context_json=json.dumps(compact_context, sort_keys=True),
            decision_json=json.dumps(compact_decision, sort_keys=True),
            safe_decision_json=json.dumps(compact_safe, sort_keys=True),
            diff_json=json.dumps(compact_diff, sort_keys=True),
            diff_hash=str(compact_diff.get("sha256", "")),
            prompt_json=prompt_payload,
            response_json=response_payload,
        )
