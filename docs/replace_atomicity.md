# Replace Atomicity Envelope (Stage4)

## Invariants
- Replace actions are processed as one transaction per `(symbol, side)`.
- Default mode is strict: replacement submit is deferred until all replaced orders are cancel-confirmed.
- Unknown-order freeze blocks replace progression.
- Replace transaction state is monotonic and resume-safe across cycles/restarts.

## Replace TX states
Flow states:
`INIT -> CANCEL_SENT -> CANCEL_CONFIRMED -> SUBMIT_SENT -> SUBMIT_CONFIRMED`

Retryable/open states:
- `INIT`
- `CANCEL_SENT`
- `CANCEL_CONFIRMED`
- `SUBMIT_SENT`
- `BLOCKED_UNKNOWN`
- `BLOCKED_RECONCILE`

Terminal states:
- `SUBMIT_CONFIRMED`
- `FAILED`

`SUBMIT_CONFIRMED` means client-side submit succeeded (acknowledged/simulated), **not** filled.

## Cancel confirmation rule (strict)
For each old client order in a replace group:
1. If it is still present in exchange `open_orders` -> unconfirmed.
2. If absent from exchange `open_orders`:
   - local order must exist and be terminal (`canceled|filled|rejected|unknown_closed`) to confirm,
   - otherwise defer (`local_state_not_terminal`).

This remains conservative by design.

## Persistence
`stage4_replace_transactions` stores:
- `replace_tx_id`
- `symbol`, `side`
- `old_client_order_ids_json`
- `new_client_order_id`
- `state`, `last_error`
- `created_at`, `last_updated_at`

## Observability
Decision events:
- `replace_deferred_unknown_order_freeze`
- `replace_deferred_cancel_unconfirmed`
- `replace_multiple_submits_coalesced`
- `replace_committed`
- `replace_tx_metadata_mismatch`

Metrics:
- `replace_tx_started`
- `replace_tx_deferred`
- `replace_tx_committed`
- `replace_tx_blocked_unknown`
- `replace_tx_failed`
- `replace_multiple_submits_coalesced_total`
- `replace_tx_metadata_mismatch_total`

## Runbook snippet
Watch:
- rising `replace_tx_deferred` with repeated `replace_deferred_cancel_unconfirmed`
- rising `replace_tx_blocked_unknown` with freeze decisions
- any `replace_tx_metadata_mismatch_total` > 0

Likely stuck indicators:
- same `replace_tx_id` repeatedly in `BLOCKED_RECONCILE`
- no progression to `SUBMIT_CONFIRMED`

Operator action:
- check exchange/open-orders consistency and reconcile cadence,
- investigate API throttling/429 or timeout anomalies,
- resolve unknown-order freeze before expecting replace submit progression.
