# Production SLOs and Alerts

## Service-level objectives
- **WS reconnect rate**: <= 6 reconnects / 5m rolling window.
- **Stale-data rate**: <= 0.5% of symbol snapshots stale over 15m.
- **Reconcile lag**: <= 1500ms p95 over 15m.
- **Order submit latency**: <= 1200ms p95 over 15m.

## Alert thresholds
- **Critical**
  - `ws_reconnect_rate > 12/5m` for 10m.
  - `stale_market_data_rate > 2%` for 10m.
  - `reconcile_lag_ms_p95 > 3000` for 10m.
  - `order_submit_latency_ms_p95 > 2500` for 10m.
- **Warning**
  - `ws_reconnect_rate > 6/5m` for 10m.
  - `stale_market_data_rate > 0.5%` for 15m.
  - `reconcile_lag_ms_p95 > 1500` for 15m.
  - `order_submit_latency_ms_p95 > 1200` for 15m.

## Paging/runbook mapping
- Reconnect and stale-data incidents: [RUNBOOK: WS reconnect storm / stale market data](./RUNBOOK.md#incident-playbooks)
- Reconcile/order latency incidents: [RUNBOOK: rollback + emergency disable](./RUNBOOK.md#rollback-checklist)
- Key/credential incidents: [RUNBOOK: API key rotation](./RUNBOOK.md#api-key-rotation)
