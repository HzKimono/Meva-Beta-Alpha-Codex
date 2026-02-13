# Stage 6.1 Ledger & Net PnL Foundation

## Event schema
Ledger events are stored in `ledger_events` with append-only writes. Core fields:
- `event_id` primary key
- UTC timestamp `ts`
- `symbol`, `type`, `side`
- Decimal-string values for `qty`, `price`, `fee`
- Exchange identifiers (`exchange_trade_id`, `exchange_order_id`, `client_order_id`)
- JSON metadata in `meta_json`

## Idempotency strategy
1. Primary idempotency: `UNIQUE(exchange_trade_id)` when exchange trade IDs exist.
2. Fallback idempotency for fills without trade IDs:
   `UNIQUE(client_order_id, symbol, side, price, qty, ts)` (fill-only index).
3. Inserts use `INSERT OR IGNORE`; duplicates are ignored safely.

Limitation: fallback uniqueness depends on timestamp precision and client order IDs being present.

## PnL methodology
- FIFO lot accounting is used.
- Realized PnL is produced by sell fills matched against oldest open buy lots.
- Unrealized PnL is mark-to-market over remaining lots.
- Fees are tracked per currency; TRY fees are also debited from realized PnL.

## Known limitations
- Non-TRY fee conversion is not performed in this stage.
- Oversell protection is conservative via lot depletion; no shorting model.
- Equity estimate depends on provided mark prices.

## Run locally
```bash
pytest -q tests/test_ledger_domain.py tests/test_state_store_ledger.py tests/test_ledger_service_integration.py tests/test_stage4_cycle_runner.py
```
