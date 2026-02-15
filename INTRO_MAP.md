# INTRO_MAP

## 1) What this project is

BTCTurk spot trading bot in Python with staged architecture: Stage 3 safe execution pipeline, Stage 4 lifecycle hardening, and Stage 7 dry-run-only analytics/backtesting. Live writes are protected by `DRY_RUN`, `KILL_SWITCH`, and explicit live-arming flags. Stage 7’s practical goal is pre-live confidence: deterministic cycle traces, simulated fills/fees/slippage, gross-vs-net ledger PnL, risk-mode decisions, universe selection, portfolio planning, order intents, and replay/parity validation.

## 2) Repository Tree (complete)

```text
.
├── .github
│   └── workflows
│       └── ci.yml
├── data
│   ├── replay
│   │   └── README.md
│   └── README.md
├── docs
│   ├── ARCHITECTURE.md
│   ├── RUNBOOK.md
│   ├── stage4.md
│   ├── stage6_2_metrics_and_atomicity.md
│   ├── stage6_3_risk_budget.md
│   ├── stage6_4_degrade_and_anomalies.md
│   ├── stage6_ledger.md
│   ├── stage7.md
│   └── STAGES.md
├── scripts
│   ├── dev.ps1
│   └── guard_multiline.py
├── src
│   └── btcbot
│       ├── accounting
│       │   ├── __init__.py
│       │   └── accounting_service.py
│       ├── adapters
│       │   ├── action_to_order.py
│       │   ├── btcturk_auth.py
│       │   ├── btcturk_http.py
│       │   ├── exchange.py
│       │   ├── exchange_stage4.py
│       │   └── replay_exchange.py
│       ├── domain
│       │   ├── accounting.py
│       │   ├── adaptation_models.py
│       │   ├── allocation.py
│       │   ├── anomalies.py
│       │   ├── execution_quality.py
│       │   ├── intent.py
│       │   ├── ledger.py
│       │   ├── market_data_models.py
│       │   ├── models.py
│       │   ├── order_intent.py
│       │   ├── order_state.py
│       │   ├── portfolio_policy_models.py
│       │   ├── risk_budget.py
│       │   ├── risk_models.py
│       │   ├── stage4.py
│       │   ├── strategy_core.py
│       │   ├── symbols.py
│       │   ├── universe.py
│       │   └── universe_models.py
│       ├── replay
│       │   ├── __init__.py
│       │   ├── tools.py
│       │   └── validate.py
│       ├── risk
│       │   ├── __init__.py
│       │   ├── exchange_rules.py
│       │   └── policy.py
│       ├── services
│       │   ├── accounting_service_stage4.py
│       │   ├── adaptation_service.py
│       │   ├── allocation_service.py
│       │   ├── anomaly_detector_service.py
│       │   ├── decision_pipeline_service.py
│       │   ├── doctor.py
│       │   ├── exchange_factory.py
│       │   ├── exchange_rules_service.py
│       │   ├── execution_service.py
│       │   ├── execution_service_stage4.py
│       │   ├── exposure_tracker.py
│       │   ├── ledger_service.py
│       │   ├── market_data_replay.py
│       │   ├── market_data_service.py
│       │   ├── metrics_collector.py
│       │   ├── metrics_service.py
│       │   ├── oms_service.py
│       │   ├── order_builder_service.py
│       │   ├── order_lifecycle_service.py
│       │   ├── param_bounds.py
│       │   ├── parity.py
│       │   ├── portfolio_policy_service.py
│       │   ├── portfolio_service.py
│       │   ├── rate_limiter.py
│       │   ├── reconcile_service.py
│       │   ├── retry.py
│       │   ├── risk_budget_service.py
│       │   ├── risk_policy.py
│       │   ├── risk_service.py
│       │   ├── stage4_cycle_runner.py
│       │   ├── stage7_backtest_runner.py
│       │   ├── stage7_cycle_runner.py
│       │   ├── stage7_risk_budget_service.py
│       │   ├── stage7_single_cycle_driver.py
│       │   ├── state_store.py
│       │   ├── strategy_service.py
│       │   ├── sweep_service.py
│       │   ├── trading_policy.py
│       │   ├── universe_selection_service.py
│       │   └── universe_service.py
│       ├── strategies
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── baseline_mean_reversion.py
│       │   ├── context.py
│       │   ├── profit_v1.py
│       │   └── stage5_core.py
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── config.py
│       ├── logging_context.py
│       └── logging_utils.py
├── tests
│   ├── test_accounting_stage3.py
│   ├── test_action_to_order_mapping.py
│   ├── test_adaptation_apply_and_rollback.py
│   ├── test_adaptation_bounds.py
│   ├── test_adaptation_proposals.py
│   ├── test_allocation_service.py
│   ├── test_backtest_data_gaps.py
│   ├── test_backtest_parity_pipeline.py
│   ├── test_backtest_replay_determinism.py
│   ├── test_baseline_mean_reversion_stage5.py
│   ├── test_btcturk_auth.py
│   ├── test_btcturk_exchangeinfo_parsing.py
│   ├── test_btcturk_http.py
│   ├── test_btcturk_submit_cancel.py
│   ├── test_cli.py
│   ├── test_config.py
│   ├── test_config_symbol_parsing.py
│   ├── test_decision_pipeline_service.py
│   ├── test_doctor.py
│   ├── test_domain_models.py
│   ├── test_env_example.py
│   ├── test_exchange_rules_service.py
│   ├── test_exchangeinfo.py
│   ├── test_execution_reconcile.py
│   ├── test_execution_service.py
│   ├── test_execution_service_live_arming.py
│   ├── test_guard_multiline.py
│   ├── test_ledger_domain.py
│   ├── test_ledger_service_integration.py
│   ├── test_logging_utils.py
│   ├── test_oms_crash_recovery.py
│   ├── test_oms_idempotency.py
│   ├── test_oms_retry_backoff.py
│   ├── test_oms_state_machine.py
│   ├── test_oms_throttling.py
│   ├── test_order_builder_service.py
│   ├── test_portfolio_policy_service.py
│   ├── test_replay_exchange.py
│   ├── test_replay_tools.py
│   ├── test_risk_policy_stage3.py
│   ├── test_stage4_cycle_runner.py
│   ├── test_stage4_services.py
│   ├── test_stage6_2_atomicity_metrics.py
│   ├── test_stage6_3_risk_budget.py
│   ├── test_stage6_4_anomalies.py
│   ├── test_stage7_backtest_contracts.py
│   ├── test_stage7_ledger_math.py
│   ├── test_stage7_metrics_collector.py
│   ├── test_stage7_report_cli.py
│   ├── test_stage7_risk_budget_service.py
│   ├── test_stage7_risk_integration.py
│   ├── test_stage7_run_integration.py
│   ├── test_state_store.py
│   ├── test_state_store_ledger.py
│   ├── test_state_store_stage3.py
│   ├── test_strategy_core_models.py
│   ├── test_strategy_registry_stage5.py
│   ├── test_strategy_stage3.py
│   ├── test_sweep_service.py
│   ├── test_trading_policy.py
│   ├── test_universe_selection_service.py
│   └── test_universe_service.py
├── .env.example
├── .gitignore
├── AUDIT_REPORT.md
├── btcbot_state.db
├── check_exchangeinfo.py
├── INTRO_MAP.md
├── pyproject.toml
└── README.md
```

## 3) File Index (every file, 1-line)

- `.env.example` [CONFIG] — Env template.
- `.github/workflows/ci.yml` [CI] — CI pipeline.
- `.gitignore` [CONFIG] — Ignore rules.
- `AUDIT_REPORT.md` [DOC] — Audit report.
- `INTRO_MAP.md` [DOC] — Support file.
- `README.md` [DOC] — Project intro.
- `btcbot_state.db` [STATE/DB] — Runtime DB.
- `check_exchangeinfo.py` [SCRIPT] — Diagnostic script.
- `data/README.md` [DATA] — Data notes.
- `data/replay/README.md` [DATA] — Replay format.
- `docs/ARCHITECTURE.md` [DOC] — Architecture doc.
- `docs/RUNBOOK.md` [DOC] — Runbook doc.
- `docs/STAGES.md` [DOC] — Stage roadmap.
- `docs/stage4.md` [DOC] — Stage 4 doc.
- `docs/stage6_2_metrics_and_atomicity.md` [DOC] — Stage 6.2 doc.
- `docs/stage6_3_risk_budget.md` [DOC] — Stage 6.3 doc.
- `docs/stage6_4_degrade_and_anomalies.md` [DOC] — Stage 6.4 doc.
- `docs/stage6_ledger.md` [DOC] — Stage 6 ledger doc.
- `docs/stage7.md` [DOC] — Stage 7 doc.
- `pyproject.toml` [CONFIG] — Package config.
- `scripts/dev.ps1` [SCRIPT] — Dev helper.
- `scripts/guard_multiline.py` [SCRIPT] — Format guard.
- `src/btcbot/__init__.py` [ORCHESTRATION] — Package init.
- `src/btcbot/__main__.py` [ENTRYPOINT] — Module entry.
- `src/btcbot/accounting/__init__.py` [SERVICE] — Package init.
- `src/btcbot/accounting/accounting_service.py` [SERVICE] — Accounting service.
- `src/btcbot/adapters/action_to_order.py` [ADAPTER] — IO adapter.
- `src/btcbot/adapters/btcturk_auth.py` [ADAPTER] — IO adapter.
- `src/btcbot/adapters/btcturk_http.py` [ADAPTER] — IO adapter.
- `src/btcbot/adapters/exchange.py` [ADAPTER] — IO adapter.
- `src/btcbot/adapters/exchange_stage4.py` [ADAPTER] — IO adapter.
- `src/btcbot/adapters/replay_exchange.py` [ADAPTER] — IO adapter.
- `src/btcbot/cli.py` [ENTRYPOINT] — CLI router.
- `src/btcbot/config.py` [ORCHESTRATION] — Settings parser.
- `src/btcbot/domain/accounting.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/adaptation_models.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/allocation.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/anomalies.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/execution_quality.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/intent.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/ledger.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/market_data_models.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/models.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/order_intent.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/order_state.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/portfolio_policy_models.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/risk_budget.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/risk_models.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/stage4.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/strategy_core.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/symbols.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/universe.py` [DOMAIN] — Domain model.
- `src/btcbot/domain/universe_models.py` [DOMAIN] — Domain model.
- `src/btcbot/logging_context.py` [ORCHESTRATION] — Log context.
- `src/btcbot/logging_utils.py` [ORCHESTRATION] — Log setup.
- `src/btcbot/replay/__init__.py` [SERVICE] — Package init.
- `src/btcbot/replay/tools.py` [SERVICE] — Support file.
- `src/btcbot/replay/validate.py` [SERVICE] — Support file.
- `src/btcbot/risk/__init__.py` [SERVICE] — Package init.
- `src/btcbot/risk/exchange_rules.py` [SERVICE] — Support file.
- `src/btcbot/risk/policy.py` [SERVICE] — Support file.
- `src/btcbot/services/accounting_service_stage4.py` [SERVICE] — Service logic.
- `src/btcbot/services/adaptation_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/allocation_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/anomaly_detector_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/decision_pipeline_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/doctor.py` [SERVICE] — Service logic.
- `src/btcbot/services/exchange_factory.py` [SERVICE] — Service logic.
- `src/btcbot/services/exchange_rules_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/execution_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/execution_service_stage4.py` [SERVICE] — Service logic.
- `src/btcbot/services/exposure_tracker.py` [SERVICE] — Service logic.
- `src/btcbot/services/ledger_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/market_data_replay.py` [SERVICE] — Service logic.
- `src/btcbot/services/market_data_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/metrics_collector.py` [SERVICE] — Service logic.
- `src/btcbot/services/metrics_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/oms_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/order_builder_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/order_lifecycle_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/param_bounds.py` [SERVICE] — Service logic.
- `src/btcbot/services/parity.py` [SERVICE] — Service logic.
- `src/btcbot/services/portfolio_policy_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/portfolio_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/rate_limiter.py` [SERVICE] — Service logic.
- `src/btcbot/services/reconcile_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/retry.py` [SERVICE] — Service logic.
- `src/btcbot/services/risk_budget_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/risk_policy.py` [SERVICE] — Service logic.
- `src/btcbot/services/risk_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/stage4_cycle_runner.py` [SERVICE] — Service logic.
- `src/btcbot/services/stage7_backtest_runner.py` [SERVICE] — Service logic.
- `src/btcbot/services/stage7_cycle_runner.py` [SERVICE] — Service logic.
- `src/btcbot/services/stage7_risk_budget_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/stage7_single_cycle_driver.py` [SERVICE] — Service logic.
- `src/btcbot/services/state_store.py` [SERVICE] — Service logic.
- `src/btcbot/services/strategy_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/sweep_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/trading_policy.py` [SERVICE] — Service logic.
- `src/btcbot/services/universe_selection_service.py` [SERVICE] — Service logic.
- `src/btcbot/services/universe_service.py` [SERVICE] — Service logic.
- `src/btcbot/strategies/__init__.py` [DOMAIN] — Strategy code.
- `src/btcbot/strategies/base.py` [DOMAIN] — Strategy code.
- `src/btcbot/strategies/baseline_mean_reversion.py` [DOMAIN] — Strategy code.
- `src/btcbot/strategies/context.py` [DOMAIN] — Strategy code.
- `src/btcbot/strategies/profit_v1.py` [DOMAIN] — Strategy code.
- `src/btcbot/strategies/stage5_core.py` [DOMAIN] — Strategy code.
- `tests/test_accounting_stage3.py` [TEST] — Test module.
- `tests/test_action_to_order_mapping.py` [TEST] — Test module.
- `tests/test_adaptation_apply_and_rollback.py` [TEST] — Test module.
- `tests/test_adaptation_bounds.py` [TEST] — Test module.
- `tests/test_adaptation_proposals.py` [TEST] — Test module.
- `tests/test_allocation_service.py` [TEST] — Test module.
- `tests/test_backtest_data_gaps.py` [TEST] — Test module.
- `tests/test_backtest_parity_pipeline.py` [TEST] — Test module.
- `tests/test_backtest_replay_determinism.py` [TEST] — Test module.
- `tests/test_baseline_mean_reversion_stage5.py` [TEST] — Test module.
- `tests/test_btcturk_auth.py` [TEST] — Test module.
- `tests/test_btcturk_exchangeinfo_parsing.py` [TEST] — Test module.
- `tests/test_btcturk_http.py` [TEST] — Test module.
- `tests/test_btcturk_submit_cancel.py` [TEST] — Test module.
- `tests/test_cli.py` [TEST] — Test module.
- `tests/test_config.py` [TEST] — Test module.
- `tests/test_config_symbol_parsing.py` [TEST] — Test module.
- `tests/test_decision_pipeline_service.py` [TEST] — Test module.
- `tests/test_doctor.py` [TEST] — Test module.
- `tests/test_domain_models.py` [TEST] — Test module.
- `tests/test_env_example.py` [TEST] — Test module.
- `tests/test_exchange_rules_service.py` [TEST] — Test module.
- `tests/test_exchangeinfo.py` [TEST] — Test module.
- `tests/test_execution_reconcile.py` [TEST] — Test module.
- `tests/test_execution_service.py` [TEST] — Test module.
- `tests/test_execution_service_live_arming.py` [TEST] — Test module.
- `tests/test_guard_multiline.py` [TEST] — Test module.
- `tests/test_ledger_domain.py` [TEST] — Test module.
- `tests/test_ledger_service_integration.py` [TEST] — Test module.
- `tests/test_logging_utils.py` [TEST] — Test module.
- `tests/test_oms_crash_recovery.py` [TEST] — Test module.
- `tests/test_oms_idempotency.py` [TEST] — Test module.
- `tests/test_oms_retry_backoff.py` [TEST] — Test module.
- `tests/test_oms_state_machine.py` [TEST] — Test module.
- `tests/test_oms_throttling.py` [TEST] — Test module.
- `tests/test_order_builder_service.py` [TEST] — Test module.
- `tests/test_portfolio_policy_service.py` [TEST] — Test module.
- `tests/test_replay_exchange.py` [TEST] — Test module.
- `tests/test_replay_tools.py` [TEST] — Test module.
- `tests/test_risk_policy_stage3.py` [TEST] — Test module.
- `tests/test_stage4_cycle_runner.py` [TEST] — Test module.
- `tests/test_stage4_services.py` [TEST] — Test module.
- `tests/test_stage6_2_atomicity_metrics.py` [TEST] — Test module.
- `tests/test_stage6_3_risk_budget.py` [TEST] — Test module.
- `tests/test_stage6_4_anomalies.py` [TEST] — Test module.
- `tests/test_stage7_backtest_contracts.py` [TEST] — Test module.
- `tests/test_stage7_ledger_math.py` [TEST] — Test module.
- `tests/test_stage7_metrics_collector.py` [TEST] — Test module.
- `tests/test_stage7_report_cli.py` [TEST] — Test module.
- `tests/test_stage7_risk_budget_service.py` [TEST] — Test module.
- `tests/test_stage7_risk_integration.py` [TEST] — Test module.
- `tests/test_stage7_run_integration.py` [TEST] — Test module.
- `tests/test_state_store.py` [TEST] — Test module.
- `tests/test_state_store_ledger.py` [TEST] — Test module.
- `tests/test_state_store_stage3.py` [TEST] — Test module.
- `tests/test_strategy_core_models.py` [TEST] — Test module.
- `tests/test_strategy_registry_stage5.py` [TEST] — Test module.
- `tests/test_strategy_stage3.py` [TEST] — Test module.
- `tests/test_sweep_service.py` [TEST] — Test module.
- `tests/test_trading_policy.py` [TEST] — Test module.
- `tests/test_universe_selection_service.py` [TEST] — Test module.
- `tests/test_universe_service.py` [TEST] — Test module.

## 4) Entrypoints & Commands

- `btcbot`: `pyproject.toml` -> `btcbot.cli:main`; launches CLI commands.
- `python -m btcbot`: `src/btcbot/__main__.py`; calls CLI main.
- `python -m btcbot.cli`: `src/btcbot/cli.py:main`; parses and dispatches commands.
- `run`: `run_cycle`; executes one Stage 3 pipeline cycle.
- `stage4-run`: `run_cycle_stage4`; executes one Stage 4 cycle.
- `stage7-run`: `run_cycle_stage7`; executes one Stage 7 dry-run cycle.
- `health`: `run_health`; connectivity/readiness check.
- `stage7-report`/`stage7-export`/`stage7-alerts`: metrics read/export commands.
- `stage7-backtest`/`stage7-parity`: replay backtest + deterministic parity compare.
- `stage7-backtest-export` (`stage7-backtest-report`)/`stage7-db-count`: DB export/count tools.
- `doctor`: `run_doctor`; validates env/config/DB/dataset readiness.
- `replay-init`/`replay-capture`: replay dataset setup and capture tools.
- `scripts/guard_multiline.py`: style guard script.
- `check_exchangeinfo.py`: exchange metadata diagnostic script.

## 5) Introduction-Ready Architecture Snapshot

```text
[CLI entrypoints]
  -> [Runners: Stage3 / Stage4 / Stage7]
  -> [Services: market/accounting/strategy/risk/execution/backtest]
  -> [Domain contracts: ledger/intents/risk/universe/allocation]
  -> [Adapters: BTCTurk HTTP/auth + replay exchange]
  -> [State: SQLite cycle + Stage7 trace/intents/metrics]
  -> [Ops: docs, CI, scripts, tests]
```

## Unreadable paths

- None for source/docs/tests under working tree.
- Excluded generated artifacts: `.pytest_cache/`, `__pycache__/`, `*.pyc`, `src/btcbot.egg-info/`.
- Excluded VCS metadata: `.git/`.