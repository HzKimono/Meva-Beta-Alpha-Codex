# Replace Atomicity Envelope (Stage 4)

## Problem
A logical replace (`old_order -> new_order`) used to run as two independent actions (`CANCEL` then `SUBMIT`). If cancel confirmation was delayed or uncertain, the submit could still happen and temporarily double-open risk.

## Invariants
1. A replace submit must carry `replace_for_client_order_id`.
2. New replace submit is blocked while any unknown order freeze is active.
3. New replace submit is blocked until the old order is terminal (`canceled`, `filled`, `rejected`, `unknown_closed`).
4. Replace state is persisted in `stage4_replace_transactions` for replay/restart observability.
5. Existing safety gates are unchanged: kill switch, live-arm, unknown freeze, dedupe/idempotency.

## State machine
`stage4_replace_transactions.status`:

- `pending_cancel`: replace intent exists; old order not terminal yet.
- `blocked_unknown`: global unknown-order freeze blocked progression.
- `submitted`: replacement submit was executed (live or dry-run simulated).

Transitions:

1. `replace_submit` arrives -> upsert transaction as `pending_cancel`.
2. If unknown freeze active -> `blocked_unknown` and no submit.
3. If old order not terminal -> stay `pending_cancel` and no submit.
4. If old order terminal -> run submit path with existing idempotency + validation.
5. On successful submit/simulated submit -> `submitted`.

## Observability
Execution emits:
- decision events (`replace_waiting_cancel_confirmation`, `replace_blocked_unknown_order_freeze`, `replace_committed`)
- metrics counters/gauge:
  - `stage4_replace_missing_linkage_total`
  - `stage4_replace_blocked_unknown_total`
  - `stage4_replace_waiting_cancel_total`
  - `stage4_replace_committed_total`
  - `stage4_replace_inflight`
