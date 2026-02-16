# PlanningKernel Refactor Blueprint

## A) Findings (duplication map)

| Planning responsibility | Stage4 implementation | Stage7 implementation | Duplication notes |
| --- | --- | --- | --- |
| Universe selection | `DecisionPipelineService._select_universe` and `DynamicUniverseService.select` call site in `Stage4CycleRunner.run_one_cycle` | `UniverseSelectionService.select_universe` in `Stage7CycleRunner.run_one_cycle_with_dependencies` | Both pipelines independently derive candidate symbols and selection policy. |
| Strategy invocation / intent generation | `DecisionPipelineService._generate_intents` using `StrategyRegistry.generate_intents` | `PortfolioPolicyService.build_plan` builds target deltas (intent-equivalent actions) | Stage7 encodes strategy intent as portfolio rebalance actions while Stage4 uses explicit strategy intents. |
| Allocation / sizing | `AllocationService.allocate` from `DecisionPipelineService.run_cycle` plus aggressive path `_run_aggressive_path` | `PortfolioPolicyService._build_allocations`, `_build_raw_actions`, `_apply_constraints` | Both compute cash budget, caps, per-symbol sizing, and order limits with different code paths. |
| Order-intent building (quantization, min-notional, symbol rules) | `sized_action_to_order` in `DecisionPipelineService.run_cycle` and fallback `_build_intents` in `Stage4CycleRunner` | `OrderBuilderService.build_intents` + `_build_action_intent` | Both quantize qty/price, enforce min notional, and derive deterministic client IDs. |
| Planning vs execution validation gates | Planning gates mixed in `DecisionPipelineService` + `_build_intents` (`missing_mark_price`, `min_notional`, `missing_pair_info`) and partially in `RiskPolicy.filter_actions` | Planning gates mixed in `OrderBuilderService` (`rules_unavailable`, `qty_rounds_to_zero`) and `PortfolioPolicyService` constraints; execution gating in OMS path | Boundary between planning rejections and execution/risk-mode rejections is duplicated and inconsistent. |

## B) Target Architecture (text diagram)

```text
Stage4Runner  ----\
                    > PlanningKernel.plan(context) -> Plan -> Stage4 ExecutionPort adapter
Stage7Runner  ----/                                     \-> Stage7 ExecutionPort adapter

PlanningKernel internals (deterministic, shared):
  1) UniverseSelector
  2) StrategyEngine
  3) Allocator
  4) OrderIntentBuilder
  5) Planning gates + diagnostics emission

Execution layer (stage-specific):
  - Stage4 ExecutionPort: maps OrderIntent -> lifecycle/exchange submit/cancel/replace/reconcile
  - Stage7 ExecutionPort: maps OrderIntent -> OMS event-sourcing submit/cancel/replace/reconcile
```

## C) Step-by-step refactor plan (commit-sized)

1. **Introduce shared contracts and kernel skeleton (no wiring).**
   - Add: `src/btcbot/planning_kernel.py`.
   - Define shared typed interfaces and `PlanningKernel.plan(context) -> Plan`.

2. **Add execution adapter glue (no behavior change).**
   - Add: `src/btcbot/services/planning_kernel_adapters.py`.
   - Include `Stage4PlanConsumer`, `Stage7PlanConsumer`, and `ExecutionPort` test double.

3. **Expose migration hooks in runners (not invoked in critical path).**
   - Modify: `src/btcbot/services/stage4_cycle_runner.py`, `src/btcbot/services/stage7_cycle_runner.py`.
   - Add `consume_shared_plan(...)` methods only.

4. **Add parity tests for shared plan consumption.**
   - Add: `tests/test_plan_consumers_contract.py`.
   - Assert Stage4 and Stage7 consume identical shared `Plan.order_intents` into identical execution submissions.

5. **Future migration (follow-up, not in this patch).**
   - Replace Stage4 `DecisionPipelineService` and Stage7 `PortfolioPolicyService + OrderBuilderService` planning calls with one `PlanningKernel` composition.
   - Keep CLI entrypoints and DB schema unchanged.
   - Move one duplicated responsibility at a time (universe -> strategy -> allocation -> intent building), asserting parity at each step.

## D) Code skeleton status

Implemented in:
- `src/btcbot/planning_kernel.py`
- `src/btcbot/services/planning_kernel_adapters.py`
- Runner glue methods in Stage4/Stage7 runners.

TODO markers identify where live wiring should occur without changing current runtime behavior.

## E) Test plan

- New test: `tests/test_plan_consumers_contract.py`
  - Builds deterministic fake planning components.
  - Generates a shared `Plan` from `PlanningKernel`.
  - Verifies Stage4 and Stage7 consumers submit identical `OrderIntent`s to an `ExecutionPort`.
- Run command:
  - `pytest tests/test_plan_consumers_contract.py`


## Rollout toggle

- `STAGE7_USE_PLANNING_KERNEL=true|false` switches Stage7 between shared-kernel and legacy planning paths for safe rollout.

## Production hardening notes

### Determinism contract

- `PlanningKernel.plan()` now enforces deterministic ordering before returning `Plan`:
  - universe by normalized symbol
  - raw/allocated intents by `(symbol, side, strategy_id, rationale, target_notional_try)`
  - order intents by `(symbol, side, client_order_id)`
- Consumers (`Stage4PlanConsumer`/`Stage7PlanConsumer`) must preserve plan order and must not re-sort.

### Typed open-orders planning view

- Kernel context uses `OpenOrderView` to pass minimal open-order state into planning:
  - `symbol`, `side`, `order_type`, `price`, `qty`, `client_order_id`, optional `status`
- Stage7 converts persisted stage4 open orders to `OpenOrderView` before invoking the kernel.

### Strict normalization policy

- Stage7 order-intent normalization is strict:
  - invalid `side` or `order_type` does **not** default into a tradable order
  - resulting intent is marked `skipped=True` with `skip_reason="invalid_normalized_fields"`

### Live-execution/reconciliation note

- For live execution, final order state transitions may arrive asynchronously over WebSocket channels.
- `reconcile()` should remain the source of truth merger for REST snapshots + stream events, including late fills/cancels, consistent with BTCTurk channel semantics.
