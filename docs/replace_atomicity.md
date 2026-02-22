# Replace Atomicity Envelope (Stage4)

## Invariants
- Replace actions are treated as a transaction by `(symbol, side)` group.
- Replacement submit is **deferred** until old replace-cancel orders are confirmed closed from exchange open-orders view.
- Unknown-order freeze blocks replace progression (`BLOCKED_UNKNOWN`).
- Replace transaction is persisted with stable `replace_tx_id` so retries/cycles continue idempotently.
- Existing kill-switch/live-arm/idempotency and submit/cancel safety gates remain active.

## Replace TX state machine
`INIT -> CANCEL_SENT -> CANCEL_CONFIRMED -> SUBMIT_SENT -> SUBMIT_CONFIRMED`

Blocked/error terminal states:
- `BLOCKED_UNKNOWN`
- `BLOCKED_RECONCILE`
- `FAILED`

## Persistence
`stage4_replace_transactions` stores:
- `replace_tx_id`
- `symbol`, `side`
- `old_client_order_ids_json`
- `new_client_order_id`
- `state`, `last_error`
- `created_at`, `last_updated_at`

## Observability
Decision events emitted per replace group:
- `replace_deferred_unknown_order_freeze`
- `replace_deferred_cancel_unconfirmed`
- `replace_committed`

Metrics:
- `replace_tx_started`
- `replace_tx_deferred`
- `replace_tx_committed`
- `replace_tx_blocked_unknown`
- `replace_tx_failed`
