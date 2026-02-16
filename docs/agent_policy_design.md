# Agent Policy Layer Design

## A) Module Diagram + Dataflow

```text
PlanningKernel/DecisionPipeline
        |
        v
  stage4_cycle_runner
        |
        +--> agent/contracts.py (AgentContext, AgentDecision, DecisionRationale, SafeDecision)
        |
        +--> agent/policy.py
        |      |- RuleBasedPolicy (deterministic baseline)
        |      |- LlmPolicy (strict JSON schema + validation)
        |      `- FallbackPolicy (LLM -> RuleBased)
        |
        +--> agent/guardrails.py (deterministic safety envelope)
        |
        +--> agent/audit.py -> StateStore.agent_decision_audit
        |
        v
 OrderLifecycleService -> RiskPolicy/RiskBudget -> ExecutionService
```

Dataflow per cycle:
1. Stage4 runner builds upstream intents from PlanningKernel/legacy decision pipeline.
2. Runner builds `AgentContext` snapshot.
3. `AgentPolicy.evaluate(context)` returns `AgentDecision`.
4. `SafetyGuard.apply(context, decision)` returns `SafeDecision`.
5. Audit persists context/decision/safe-decision + diff hash.
6. Safe intents are mapped back to Stage4 `Order` for downstream unchanged execution layers.

## B) File Plan (Exact Paths)

- `src/btcbot/agent/contracts.py`
- `src/btcbot/agent/policy.py`
- `src/btcbot/agent/guardrails.py`
- `src/btcbot/agent/audit.py`
- `src/btcbot/agent/__init__.py`
- `src/btcbot/services/stage4_cycle_runner.py`
- `src/btcbot/services/state_store.py`
- `src/btcbot/config.py`
- `tests/test_agent_guardrails.py`
- `tests/test_agent_policy.py`
- `tests/test_agent_audit.py`

## E) Rollout Plan

1. **Observe-only bootstrap**
   - `AGENT_POLICY_ENABLED=true`
   - `AGENT_OBSERVE_ONLY=true`
   - `AGENT_POLICY_PROVIDER=rule`
   - Verify audit volume and guardrail drops.
2. **Enable LLM in low-risk mode**
   - `AGENT_POLICY_PROVIDER=llm`
   - keep observe-only override ON
   - keep strict allowlist, low max order notional, tight spread cap.
3. **Progressive relaxation**
   - disable observe-only override gradually
   - increase per-order notional and allowlist breadth in increments
   - monitor fallback frequency, guardrail hit rate, and diff drift.
4. **Steady-state operations**
   - periodic replay tests with frozen contexts
   - automated alarms on invalid LLM output spikes and fallback bursts.
