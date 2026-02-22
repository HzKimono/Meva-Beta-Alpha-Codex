# Replace Atomicity Envelope (Stage4)

## Contract
- Replace is executed as one transaction per `(symbol, side)`.
- Default is strict cancel-confirm-then-submit (no overlap mode).
- Unknown-order freeze blocks replace submit progression.
- Replace TX state is persisted and monotonic across cycles/restarts.

## States
Flow: `INIT -> CANCEL_SENT -> CANCEL_CONFIRMED -> SUBMIT_SENT -> SUBMIT_CONFIRMED`

Retryable/open: `INIT`, `CANCEL_SENT`, `CANCEL_CONFIRMED`, `SUBMIT_SENT`, `BLOCKED_UNKNOWN`, `BLOCKED_RECONCILE`

Terminal: `SUBMIT_CONFIRMED`, `FAILED`

`SUBMIT_CONFIRMED` means the submit side-effect succeeded client-side (live ACK or dry-run simulation). It does **not** mean filled.

## Cancel confirmation rule (conservative)
For each replaced old client order id:
1. If found in exchange open-orders -> defer (`old_id_still_open`).
2. If not found in exchange open-orders:
   - if local record missing -> defer (`local_missing_record`),
   - if local record non-terminal -> defer (`local_state_not_terminal`),
   - if local terminal (`canceled|filled|rejected|unknown_closed`) -> confirmed for that old id.

Any unresolved old id defers replacement submit for the cycle.

## Multi-submit policy
If multiple `replace_submit` actions exist in one `(symbol, side)` group, only the latest intent is executed; earlier submits are suppressed and observed.

## Observability
Decision events:
- `replace_deferred_unknown_order_freeze`
- `replace_deferred_cancel_unconfirmed`
- `replace_multiple_submits_coalesced`
- `replace_tx_metadata_mismatch`
- `replace_committed`

Counters:
- `replace_tx_started_total`
- `replace_tx_deferred_total`
- `replace_tx_committed_total`
- `replace_tx_blocked_unknown_total`
- `replace_tx_failed_total`
- `replace_multiple_submits_coalesced_total`
- `replace_tx_metadata_mismatch_total`

## Operator Runbook
Watch for stuck behavior:
- same `replace_tx_id` repeatedly in `BLOCKED_RECONCILE`
- sustained rise in `replace_tx_deferred_total`
- non-zero `replace_tx_metadata_mismatch_total`

Operational actions:
1. Inspect exchange open-orders for old ids in the replace tx.
2. Check unknown-freeze status and resolve unknown orders first.
3. Investigate 429/timeouts; increase reconcile cadence only if exchange limits allow.
