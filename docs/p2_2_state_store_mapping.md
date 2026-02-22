# P2-2 StateStore Scan and Mapping

## Scan results

### `StateStore` usage hotspots
- `src/btcbot/services/stage4_cycle_runner.py`
- `src/btcbot/services/risk_budget_service.py`
- `src/btcbot/services/metrics_service.py`
- `src/btcbot/services/execution_service.py`
- `src/btcbot/services/ledger_service.py`
- `src/btcbot/services/strategy_service.py`
- `src/btcbot/cli.py`

### Direct `sqlite3` usage hotspots outside persistence
- `src/btcbot/cli.py`
- `src/btcbot/services/parity.py`
- Tests that inspect sqlite directly for assertions

## Method cluster mapping (StateStore -> repo)

### Risk state + decisions
- `get_risk_state_current` -> `RiskRepoProtocol.get_risk_state_current`
- `save_risk_decision` -> `RiskRepoProtocol.save_risk_decision`
- `upsert_risk_state_current` -> `RiskRepoProtocol.upsert_risk_state_current`
- `persist_risk` -> UoW transaction calling both risk write methods

### Orders / executions / fills
- `client_order_id_exists` -> `OrdersRepoProtocol.client_order_id_exists`
- Stage4 order/fill methods remain in facade (next migration tranche)

### Metrics / cycle traces
- `save_cycle_metrics` -> `MetricsRepoProtocol.save_cycle_metrics`
- `record_cycle_audit` -> `TraceRepoProtocol.record_cycle_audit`

### Config / metadata
- `meta`, cursor and checkpoint methods remain in facade (next migration tranche)

## Migration plan (ordered)
1. Move Stage4 order lifecycle methods (`stage4_orders`, replace tx, unknown probes) into `orders_repo.py`.
2. Move ledger event + checkpoint methods into a dedicated `ledger_repo.py` and route `LedgerService` through UoW.
3. Move stage7 trace/metrics exports into `trace_repo.py` + `metrics_repo.py` with read models.
4. Move configuration/cursor/meta methods into `meta_repo.py`.
5. Remove raw SQL from `StateStore`; keep only backward-compatible delegation shims.
6. Flip remaining services to UoW injection, then delete facade methods and finally `StateStore`.
