# REPOSITORY INDEX MAP

## A) Entrypoints & Commands
- CLI package entrypoint: `btcbot` -> `btcbot.cli:main` (declared in `pyproject.toml`).
- Module entrypoint: `python -m btcbot` -> `src/btcbot/__main__.py` calling `main()`.
- Full CLI subcommands are defined in `src/btcbot/cli.py` `main()` via `argparse.add_parser(...)`: `run`, `stage4-run`, `stage7-run`, `health`, `stage7-report`, `stage7-export`, `stage7-alerts`, `stage7-backtest`, `stage7-parity`, `doctor`, `replay-init`, `replay-capture`, `stage7-backtest-export` (alias: `stage7-backtest-report`), `stage7-db-count`.
- Common runnable commands:
  - `python -m btcbot.cli run --dry-run` — starts Stage 3 cycle execution (`run_cycle`).
  - `python -m btcbot.cli stage4-run --dry-run` — starts Stage 4 cycle runner (`run_cycle_stage4`).
  - `python -m btcbot.cli stage7-run --dry-run` — starts Stage 7 cycle runner (`run_cycle_stage7`).
  - `python -m btcbot.cli health` — runs health checks (`run_health`).
  - `python -m btcbot.cli doctor --json` — validates env/DB/dataset (`run_doctor`).
  - `python -m btcbot.cli stage7-backtest --out-db ./out.db --start <iso> --end <iso>` — replay backtest (`run_stage7_backtest`).
  - `python -m btcbot.cli stage7-parity --db-a ./a.db --db-b ./b.db --start <iso> --end <iso>` — DB parity comparison (`run_stage7_parity`).
  - `python -m btcbot.cli replay-init --dataset <path>` — initializes replay dataset (`run_replay_init`).
  - `python -m btcbot.cli replay-capture --dataset <path> --symbols BTCTRY` — captures public replay data (`run_replay_capture`).
- Script entrypoints: `python scripts/guard_multiline.py`, `python scripts/capture_exchangeinfo_fixture.py`, `python scripts/debug_stage7_schema.py`, `python scripts/debug_stage7_metrics.py`, and PowerShell helper `scripts/dev.ps1`.
- Container entrypoint: `docker run <image>` starts `btcbot run --once` from `Dockerfile` `ENTRYPOINT` + `CMD`.
- Compose entrypoint: `docker compose up --build` starts looped bot command from `docker-compose.yml`.

## B) File Tree (depth 5)
Legend: `path — one-line purpose`.
- `.env.example` — Environment template for runtime configuration defaults.
- `.env.pilot.example` — Environment template for pilot-live profile.
- `.github/`
  - `.github/workflows/`
    - `.github/workflows/ci.yml` — CI workflow definition.
- `AUDIT_REPORT.md` — Project audit/report document (legacy).
- `Dockerfile` — Container build/runtime definition for btcbot service.
- `INTRO_MAP.md` — High-level repository mapping notes.
- `Makefile` — Developer quality-gate convenience target(s).
- `PROJECT_REPORT.md` — Project status/report document (legacy).
- `README.md` — Primary project overview, safety gates, and operator commands.
- `TECHNICAL_AUDIT_REPORT.md` — Prior technical audit artifact committed in previous PR.
- `btcbot_state.db` — Local SQLite runtime state database artifact.
- `check_exchangeinfo.py` — Utility script to inspect BTCTurk exchange info responses.
- `constraints.txt` — Pinned dependency constraints for reproducible installs.
- `data/`
  - `data/README.md` — Data directory usage notes.
  - `data/replay/`
    - `data/replay/README.md` — Replay dataset format/usage notes.
- `docker-compose.yml` — Local compose wiring for btcbot + persistent data volume.
- `docs/`
  - `docs/ARCHITECTURE.md` — Documentation for ARCHITECTURE.
  - `docs/ARCHITECTURE_MAP.md` — Documentation for ARCHITECTURE MAP.
  - `docs/BTCTURK_ADAPTER_AUDIT.md` — Documentation for BTCTURK ADAPTER AUDIT.
  - `docs/DECISION_LAYER_AUDIT.md` — Documentation for DECISION LAYER AUDIT.
  - `docs/EXECUTION_PIPELINE_MAP.md` — Documentation for EXECUTION PIPELINE MAP.
  - `docs/RUNBOOK.md` — Documentation for RUNBOOK.
  - `docs/SLO.md` — Documentation for SLO.
  - `docs/STAGES.md` — Documentation for STAGES.
  - `docs/TEST_QUALITY_GATES_AUDIT.md` — Documentation for TEST QUALITY GATES AUDIT.
  - `docs/THREAT_MODEL.md` — Documentation for THREAT MODEL.
  - `docs/agent_policy_design.md` — Documentation for agent policy design.
  - `docs/pilot_live.md` — Documentation for pilot live.
  - `docs/planning_kernel_refactor.md` — Documentation for planning kernel refactor.
  - `docs/stage4.md` — Documentation for stage4.
  - `docs/stage6_2_metrics_and_atomicity.md` — Documentation for stage6 2 metrics and atomicity.
  - `docs/stage6_3_risk_budget.md` — Documentation for stage6 3 risk budget.
  - `docs/stage6_4_degrade_and_anomalies.md` — Documentation for stage6 4 degrade and anomalies.
  - `docs/stage6_ledger.md` — Documentation for stage6 ledger.
  - `docs/stage7.md` — Documentation for stage7.
- `pyproject.toml` — Packaging metadata, dependencies, tool configuration, console script entrypoint.
- `scripts/`
  - `scripts/capture_exchangeinfo_fixture.py` — Operator/developer helper script for capture_exchangeinfo_fixture workflow.
  - `scripts/debug_stage7_metrics.py` — Operator/developer helper script for debug_stage7_metrics workflow.
  - `scripts/debug_stage7_schema.py` — Operator/developer helper script for debug_stage7_schema workflow.
  - `scripts/dev.ps1` — Operator/developer helper script for dev workflow.
  - `scripts/guard_multiline.py` — Operator/developer helper script for guard_multiline workflow.
- `src/`
  - `src/btcbot/`
    - `src/btcbot/__init__.py` — Package initializer.
    - `src/btcbot/__main__.py` — Module entrypoint forwarding to `btcbot.cli:main`.
    - `src/btcbot/accounting/`
      - `src/btcbot/accounting/__init__.py` — Package initializer.
      - `src/btcbot/accounting/accounting_service.py` — Runtime module implementing `accounting_service` domain/service logic.
      - `src/btcbot/accounting/ledger.py` — Runtime module implementing `ledger` domain/service logic.
      - `src/btcbot/accounting/models.py` — Runtime module implementing `models` domain/service logic.
    - `src/btcbot/adapters/`
      - `src/btcbot/adapters/action_to_order.py` — Runtime module implementing `action_to_order` domain/service logic.
      - `src/btcbot/adapters/btcturk/`
        - `src/btcbot/adapters/btcturk/__init__.py` — Package initializer.
        - `src/btcbot/adapters/btcturk/clock_sync.py` — Runtime module implementing `clock_sync` domain/service logic.
        - `src/btcbot/adapters/btcturk/instrumentation.py` — Runtime module implementing `instrumentation` domain/service logic.
        - `src/btcbot/adapters/btcturk/market_data.py` — Runtime module implementing `market_data` domain/service logic.
        - `src/btcbot/adapters/btcturk/rate_limit.py` — Runtime module implementing `rate_limit` domain/service logic.
        - `src/btcbot/adapters/btcturk/reconcile.py` — Runtime module implementing `reconcile` domain/service logic.
        - `src/btcbot/adapters/btcturk/rest_client.py` — Runtime module implementing `rest_client` domain/service logic.
        - `src/btcbot/adapters/btcturk/retry.py` — Runtime module implementing `retry` domain/service logic.
        - `src/btcbot/adapters/btcturk/ws_client.py` — Runtime module implementing `ws_client` domain/service logic.
      - `src/btcbot/adapters/btcturk_auth.py` — Runtime module implementing `btcturk_auth` domain/service logic.
      - `src/btcbot/adapters/btcturk_http.py` — Runtime module implementing `btcturk_http` domain/service logic.
      - `src/btcbot/adapters/exchange.py` — Runtime module implementing `exchange` domain/service logic.
      - `src/btcbot/adapters/exchange_stage4.py` — Runtime module implementing `exchange_stage4` domain/service logic.
      - `src/btcbot/adapters/replay_exchange.py` — Runtime module implementing `replay_exchange` domain/service logic.
    - `src/btcbot/agent/`
      - `src/btcbot/agent/__init__.py` — Package initializer.
      - `src/btcbot/agent/audit.py` — Runtime module implementing `audit` domain/service logic.
      - `src/btcbot/agent/contracts.py` — Runtime module implementing `contracts` domain/service logic.
      - `src/btcbot/agent/guardrails.py` — Runtime module implementing `guardrails` domain/service logic.
      - `src/btcbot/agent/policy.py` — Runtime module implementing `policy` domain/service logic.
    - `src/btcbot/cli.py` — Runtime module implementing `cli` domain/service logic.
    - `src/btcbot/config.py` — Runtime module implementing `config` domain/service logic.
    - `src/btcbot/domain/`
      - `src/btcbot/domain/account_snapshot.py` — Runtime module implementing `account_snapshot` domain/service logic.
      - `src/btcbot/domain/accounting.py` — Runtime module implementing `accounting` domain/service logic.
      - `src/btcbot/domain/adaptation_models.py` — Runtime module implementing `adaptation_models` domain/service logic.
      - `src/btcbot/domain/allocation.py` — Runtime module implementing `allocation` domain/service logic.
      - `src/btcbot/domain/anomalies.py` — Runtime module implementing `anomalies` domain/service logic.
      - `src/btcbot/domain/execution_quality.py` — Runtime module implementing `execution_quality` domain/service logic.
      - `src/btcbot/domain/intent.py` — Runtime module implementing `intent` domain/service logic.
      - `src/btcbot/domain/ledger.py` — Runtime module implementing `ledger` domain/service logic.
      - `src/btcbot/domain/market_data_models.py` — Runtime module implementing `market_data_models` domain/service logic.
      - `src/btcbot/domain/models.py` — Runtime module implementing `models` domain/service logic.
      - `src/btcbot/domain/order_intent.py` — Runtime module implementing `order_intent` domain/service logic.
      - `src/btcbot/domain/order_state.py` — Runtime module implementing `order_state` domain/service logic.
      - `src/btcbot/domain/portfolio_policy_models.py` — Runtime module implementing `portfolio_policy_models` domain/service logic.
      - `src/btcbot/domain/risk_budget.py` — Runtime module implementing `risk_budget` domain/service logic.
      - `src/btcbot/domain/risk_models.py` — Runtime module implementing `risk_models` domain/service logic.
      - `src/btcbot/domain/stage4.py` — Runtime module implementing `stage4` domain/service logic.
      - `src/btcbot/domain/strategy_core.py` — Runtime module implementing `strategy_core` domain/service logic.
      - `src/btcbot/domain/symbols.py` — Runtime module implementing `symbols` domain/service logic.
      - `src/btcbot/domain/universe.py` — Runtime module implementing `universe` domain/service logic.
      - `src/btcbot/domain/universe_models.py` — Runtime module implementing `universe_models` domain/service logic.
    - `src/btcbot/logging_context.py` — Runtime module implementing `logging_context` domain/service logic.
    - `src/btcbot/logging_utils.py` — Runtime module implementing `logging_utils` domain/service logic.
    - `src/btcbot/observability.py` — Runtime module implementing `observability` domain/service logic.
    - `src/btcbot/planning_kernel.py` — Runtime module implementing `planning_kernel` domain/service logic.
    - `src/btcbot/replay/`
      - `src/btcbot/replay/__init__.py` — Package initializer.
      - `src/btcbot/replay/tools.py` — Runtime module implementing `tools` domain/service logic.
      - `src/btcbot/replay/validate.py` — Runtime module implementing `validate` domain/service logic.
    - `src/btcbot/risk/`
      - `src/btcbot/risk/__init__.py` — Package initializer.
      - `src/btcbot/risk/budget.py` — Runtime module implementing `budget` domain/service logic.
      - `src/btcbot/risk/exchange_rules.py` — Runtime module implementing `exchange_rules` domain/service logic.
      - `src/btcbot/risk/policy.py` — Runtime module implementing `policy` domain/service logic.
    - `src/btcbot/security/`
      - `src/btcbot/security/__init__.py` — Package initializer.
      - `src/btcbot/security/redaction.py` — Runtime module implementing `redaction` domain/service logic.
      - `src/btcbot/security/secrets.py` — Runtime module implementing `secrets` domain/service logic.
    - `src/btcbot/services/`
      - `src/btcbot/services/account_snapshot_service.py` — Runtime module implementing `account_snapshot_service` domain/service logic.
      - `src/btcbot/services/accounting_service_stage4.py` — Runtime module implementing `accounting_service_stage4` domain/service logic.
      - `src/btcbot/services/adaptation_service.py` — Runtime module implementing `adaptation_service` domain/service logic.
      - `src/btcbot/services/allocation_service.py` — Runtime module implementing `allocation_service` domain/service logic.
      - `src/btcbot/services/anomaly_detector_service.py` — Runtime module implementing `anomaly_detector_service` domain/service logic.
      - `src/btcbot/services/client_order_id_service.py` — Runtime module implementing `client_order_id_service` domain/service logic.
      - `src/btcbot/services/decision_pipeline_service.py` — Runtime module implementing `decision_pipeline_service` domain/service logic.
      - `src/btcbot/services/doctor.py` — Runtime module implementing `doctor` domain/service logic.
      - `src/btcbot/services/dynamic_universe_service.py` — Runtime module implementing `dynamic_universe_service` domain/service logic.
      - `src/btcbot/services/effective_universe.py` — Runtime module implementing `effective_universe` domain/service logic.
      - `src/btcbot/services/exchange_factory.py` — Runtime module implementing `exchange_factory` domain/service logic.
      - `src/btcbot/services/exchange_rules_service.py` — Runtime module implementing `exchange_rules_service` domain/service logic.
      - `src/btcbot/services/execution_service.py` — Runtime module implementing `execution_service` domain/service logic.
      - `src/btcbot/services/execution_service_stage4.py` — Runtime module implementing `execution_service_stage4` domain/service logic.
      - `src/btcbot/services/exposure_tracker.py` — Runtime module implementing `exposure_tracker` domain/service logic.
      - `src/btcbot/services/ledger_service.py` — Runtime module implementing `ledger_service` domain/service logic.
      - `src/btcbot/services/market_data_replay.py` — Runtime module implementing `market_data_replay` domain/service logic.
      - `src/btcbot/services/market_data_service.py` — Runtime module implementing `market_data_service` domain/service logic.
      - `src/btcbot/services/metrics_collector.py` — Runtime module implementing `metrics_collector` domain/service logic.
      - `src/btcbot/services/metrics_service.py` — Runtime module implementing `metrics_service` domain/service logic.
      - `src/btcbot/services/oms_service.py` — Runtime module implementing `oms_service` domain/service logic.
      - `src/btcbot/services/order_builder_service.py` — Runtime module implementing `order_builder_service` domain/service logic.
      - `src/btcbot/services/order_lifecycle_service.py` — Runtime module implementing `order_lifecycle_service` domain/service logic.
      - `src/btcbot/services/param_bounds.py` — Runtime module implementing `param_bounds` domain/service logic.
      - `src/btcbot/services/parity.py` — Runtime module implementing `parity` domain/service logic.
      - `src/btcbot/services/planning_kernel_adapters.py` — Runtime module implementing `planning_kernel_adapters` domain/service logic.
      - `src/btcbot/services/portfolio_policy_service.py` — Runtime module implementing `portfolio_policy_service` domain/service logic.
      - `src/btcbot/services/portfolio_service.py` — Runtime module implementing `portfolio_service` domain/service logic.
      - `src/btcbot/services/process_lock.py` — Runtime module implementing `process_lock` domain/service logic.
      - `src/btcbot/services/rate_limiter.py` — Runtime module implementing `rate_limiter` domain/service logic.
      - `src/btcbot/services/reconcile_service.py` — Runtime module implementing `reconcile_service` domain/service logic.
      - `src/btcbot/services/retry.py` — Runtime module implementing `retry` domain/service logic.
      - `src/btcbot/services/risk_budget_service.py` — Runtime module implementing `risk_budget_service` domain/service logic.
      - `src/btcbot/services/risk_policy.py` — Runtime module implementing `risk_policy` domain/service logic.
      - `src/btcbot/services/risk_service.py` — Runtime module implementing `risk_service` domain/service logic.
      - `src/btcbot/services/stage4_cycle_runner.py` — Runtime module implementing `stage4_cycle_runner` domain/service logic.
      - `src/btcbot/services/stage4_planning_kernel_integration.py` — Runtime module implementing `stage4_planning_kernel_integration` domain/service logic.
      - `src/btcbot/services/stage7_backtest_runner.py` — Runtime module implementing `stage7_backtest_runner` domain/service logic.
      - `src/btcbot/services/stage7_cycle_runner.py` — Runtime module implementing `stage7_cycle_runner` domain/service logic.
      - `src/btcbot/services/stage7_planning_kernel_integration.py` — Runtime module implementing `stage7_planning_kernel_integration` domain/service logic.
      - `src/btcbot/services/stage7_risk_budget_service.py` — Runtime module implementing `stage7_risk_budget_service` domain/service logic.
      - `src/btcbot/services/stage7_single_cycle_driver.py` — Runtime module implementing `stage7_single_cycle_driver` domain/service logic.
      - `src/btcbot/services/startup_recovery.py` — Runtime module implementing `startup_recovery` domain/service logic.
      - `src/btcbot/services/state_store.py` — Runtime module implementing `state_store` domain/service logic.
      - `src/btcbot/services/strategy_service.py` — Runtime module implementing `strategy_service` domain/service logic.
      - `src/btcbot/services/sweep_service.py` — Runtime module implementing `sweep_service` domain/service logic.
      - `src/btcbot/services/trading_policy.py` — Runtime module implementing `trading_policy` domain/service logic.
      - `src/btcbot/services/universe_selection_service.py` — Runtime module implementing `universe_selection_service` domain/service logic.
      - `src/btcbot/services/universe_service.py` — Runtime module implementing `universe_service` domain/service logic.
    - `src/btcbot/strategies/`
      - `src/btcbot/strategies/__init__.py` — Package initializer.
      - `src/btcbot/strategies/base.py` — Runtime module implementing `base` domain/service logic.
      - `src/btcbot/strategies/baseline_mean_reversion.py` — Runtime module implementing `baseline_mean_reversion` domain/service logic.
      - `src/btcbot/strategies/context.py` — Runtime module implementing `context` domain/service logic.
      - `src/btcbot/strategies/profit_v1.py` — Runtime module implementing `profit_v1` domain/service logic.
      - `src/btcbot/strategies/stage5_core.py` — Runtime module implementing `stage5_core` domain/service logic.
  - `src/btcbot.egg-info/`
    - `src/btcbot.egg-info/PKG-INFO` — Setuptools-generated package metadata.
    - `src/btcbot.egg-info/SOURCES.txt` — Setuptools-generated package metadata.
    - `src/btcbot.egg-info/dependency_links.txt` — Setuptools-generated package metadata.
    - `src/btcbot.egg-info/entry_points.txt` — Setuptools-generated package metadata.
    - `src/btcbot.egg-info/requires.txt` — Setuptools-generated package metadata.
    - `src/btcbot.egg-info/top_level.txt` — Setuptools-generated package metadata.
- `tests/`
  - `tests/chaos/`
    - `tests/chaos/test_resilience_scenarios.py` — Pytest coverage for resilience scenarios behavior.
  - `tests/conftest.py` — Pytest coverage for conftest behavior.
  - `tests/fixtures/`
    - `tests/fixtures/btcturk_exchangeinfo_min_notional_absent.json` — Static fixture input used by tests.
    - `tests/fixtures/btcturk_exchangeinfo_min_notional_present.json` — Static fixture input used by tests.
    - `tests/fixtures/btcturk_ws/`
      - `tests/fixtures/btcturk_ws/channel_423_trade_match.json` — Static fixture input used by tests.
  - `tests/soak/`
    - `tests/soak/test_market_data_soak.py` — Pytest coverage for market data soak behavior.
  - `tests/test_account_snapshot_service.py` — Pytest coverage for account snapshot service behavior.
  - `tests/test_accounting_stage3.py` — Pytest coverage for accounting stage3 behavior.
  - `tests/test_action_to_order_mapping.py` — Pytest coverage for action to order mapping behavior.
  - `tests/test_adaptation_apply_and_rollback.py` — Pytest coverage for adaptation apply and rollback behavior.
  - `tests/test_adaptation_bounds.py` — Pytest coverage for adaptation bounds behavior.
  - `tests/test_adaptation_proposals.py` — Pytest coverage for adaptation proposals behavior.
  - `tests/test_agent_audit.py` — Pytest coverage for agent audit behavior.
  - `tests/test_agent_guardrails.py` — Pytest coverage for agent guardrails behavior.
  - `tests/test_agent_policy.py` — Pytest coverage for agent policy behavior.
  - `tests/test_allocation_service.py` — Pytest coverage for allocation service behavior.
  - `tests/test_backtest_data_gaps.py` — Pytest coverage for backdata gaps behavior.
  - `tests/test_backtest_parity_pipeline.py` — Pytest coverage for backparity pipeline behavior.
  - `tests/test_backtest_replay_determinism.py` — Pytest coverage for backreplay determinism behavior.
  - `tests/test_baseline_mean_reversion_stage5.py` — Pytest coverage for baseline mean reversion stage5 behavior.
  - `tests/test_btcturk_auth.py` — Pytest coverage for btcturk auth behavior.
  - `tests/test_btcturk_clock_sync.py` — Pytest coverage for btcturk clock sync behavior.
  - `tests/test_btcturk_exchangeinfo_parsing.py` — Pytest coverage for btcturk exchangeinfo parsing behavior.
  - `tests/test_btcturk_http.py` — Pytest coverage for btcturk http behavior.
  - `tests/test_btcturk_market_data.py` — Pytest coverage for btcturk market data behavior.
  - `tests/test_btcturk_rate_limit.py` — Pytest coverage for btcturk rate limit behavior.
  - `tests/test_btcturk_reconcile.py` — Pytest coverage for btcturk reconcile behavior.
  - `tests/test_btcturk_rest_client.py` — Pytest coverage for btcturk rest client behavior.
  - `tests/test_btcturk_retry_reliability.py` — Pytest coverage for btcturk retry reliability behavior.
  - `tests/test_btcturk_submit_cancel.py` — Pytest coverage for btcturk submit cancel behavior.
  - `tests/test_btcturk_ws_client.py` — Pytest coverage for btcturk ws client behavior.
  - `tests/test_cli.py` — Pytest coverage for cli behavior.
  - `tests/test_client_order_id_service.py` — Pytest coverage for client order id service behavior.
  - `tests/test_config.py` — Pytest coverage for config behavior.
  - `tests/test_config_symbol_parsing.py` — Pytest coverage for config symbol parsing behavior.
  - `tests/test_decision_pipeline_budget_hook.py` — Pytest coverage for decision pipeline budget hook behavior.
  - `tests/test_decision_pipeline_service.py` — Pytest coverage for decision pipeline service behavior.
  - `tests/test_doctor.py` — Pytest coverage for doctor behavior.
  - `tests/test_domain_models.py` — Pytest coverage for domain models behavior.
  - `tests/test_dynamic_universe_service.py` — Pytest coverage for dynamic universe service behavior.
  - `tests/test_effective_universe.py` — Pytest coverage for effective universe behavior.
  - `tests/test_env_example.py` — Pytest coverage for env example behavior.
  - `tests/test_exchange_rules_service.py` — Pytest coverage for exchange rules service behavior.
  - `tests/test_exchangeinfo.py` — Pytest coverage for exchangeinfo behavior.
  - `tests/test_execution_reconcile.py` — Pytest coverage for execution reconcile behavior.
  - `tests/test_execution_service.py` — Pytest coverage for execution service behavior.
  - `tests/test_execution_service_live_arming.py` — Pytest coverage for execution service live arming behavior.
  - `tests/test_guard_multiline.py` — Pytest coverage for guard multiline behavior.
  - `tests/test_ledger_domain.py` — Pytest coverage for ledger domain behavior.
  - `tests/test_ledger_service_integration.py` — Pytest coverage for ledger service integration behavior.
  - `tests/test_logging_utils.py` — Pytest coverage for logging utils behavior.
  - `tests/test_oms_crash_recovery.py` — Pytest coverage for oms crash recovery behavior.
  - `tests/test_oms_idempotency.py` — Pytest coverage for oms idempotency behavior.
  - `tests/test_oms_retry_backoff.py` — Pytest coverage for oms retry backoff behavior.
  - `tests/test_oms_state_machine.py` — Pytest coverage for oms state machine behavior.
  - `tests/test_oms_throttling.py` — Pytest coverage for oms throttling behavior.
  - `tests/test_order_builder_service.py` — Pytest coverage for order builder service behavior.
  - `tests/test_plan_consumers_contract.py` — Pytest coverage for plan consumers contract behavior.
  - `tests/test_planning_kernel_parity.py` — Pytest coverage for planning kernel parity behavior.
  - `tests/test_portfolio_policy_service.py` — Pytest coverage for portfolio policy service behavior.
  - `tests/test_process_lock.py` — Pytest coverage for process lock behavior.
  - `tests/test_replay_exchange.py` — Pytest coverage for replay exchange behavior.
  - `tests/test_replay_tools.py` — Pytest coverage for replay tools behavior.
  - `tests/test_risk_policy_stage3.py` — Pytest coverage for risk policy stage3 behavior.
  - `tests/test_security_controls.py` — Pytest coverage for security controls behavior.
  - `tests/test_self_financing_budget.py` — Pytest coverage for self financing budget behavior.
  - `tests/test_self_financing_ledger.py` — Pytest coverage for self financing ledger behavior.
  - `tests/test_stage4_cycle_runner.py` — Pytest coverage for stage4 cycle runner behavior.
  - `tests/test_stage4_planning_kernel_integration.py` — Pytest coverage for stage4 planning kernel integration behavior.
  - `tests/test_stage4_services.py` — Pytest coverage for stage4 services behavior.
  - `tests/test_stage6_2_atomicity_metrics.py` — Pytest coverage for stage6 2 atomicity metrics behavior.
  - `tests/test_stage6_3_risk_budget.py` — Pytest coverage for stage6 3 risk budget behavior.
  - `tests/test_stage6_4_anomalies.py` — Pytest coverage for stage6 4 anomalies behavior.
  - `tests/test_stage7_accounting_refactor.py` — Pytest coverage for stage7 accounting refactor behavior.
  - `tests/test_stage7_backtest_contracts.py` — Pytest coverage for stage7 backcontracts behavior.
  - `tests/test_stage7_ledger_math.py` — Pytest coverage for stage7 ledger math behavior.
  - `tests/test_stage7_metrics_collector.py` — Pytest coverage for stage7 metrics collector behavior.
  - `tests/test_stage7_planning_kernel_integration.py` — Pytest coverage for stage7 planning kernel integration behavior.
  - `tests/test_stage7_report_cli.py` — Pytest coverage for stage7 report cli behavior.
  - `tests/test_stage7_risk_budget_service.py` — Pytest coverage for stage7 risk budget service behavior.
  - `tests/test_stage7_risk_integration.py` — Pytest coverage for stage7 risk integration behavior.
  - `tests/test_stage7_run_integration.py` — Pytest coverage for stage7 run integration behavior.
  - `tests/test_startup_recovery.py` — Pytest coverage for startup recovery behavior.
  - `tests/test_state_store.py` — Pytest coverage for state store behavior.
  - `tests/test_state_store_ledger.py` — Pytest coverage for state store ledger behavior.
  - `tests/test_state_store_stage3.py` — Pytest coverage for state store stage3 behavior.
  - `tests/test_strategy_core_models.py` — Pytest coverage for strategy core models behavior.
  - `tests/test_strategy_registry_stage5.py` — Pytest coverage for strategy registry stage5 behavior.
  - `tests/test_strategy_stage3.py` — Pytest coverage for strategy stage3 behavior.
  - `tests/test_sweep_service.py` — Pytest coverage for sweep service behavior.
  - `tests/test_trading_policy.py` — Pytest coverage for trading policy behavior.
  - `tests/test_universe_selection_service.py` — Pytest coverage for universe selection service behavior.
  - `tests/test_universe_service.py` — Pytest coverage for universe service behavior.

## C) Module Responsibility Table
| Module/Package | Responsibility | Key symbols | Key dependencies (imports) |
|---|---|---|---|
| `btcbot.cli` | Primary command parsing and runtime orchestration for Stage3/4/7, health, doctor, replay, parity/export flows. | `main`, `_load_settings`, `run_cycle`, `run_cycle_stage4`, `run_cycle_stage7`, `run_health`, `run_doctor` | `argparse`, `Settings`, service modules (`ExecutionService`, `Stage4CycleRunner`, `Stage7CycleRunner`, `StateStore`) |
| `btcbot.config` | Central environment/config schema and validators. | `Settings`, `parse_symbols`, `parse_btcturk_api_scopes` | `pydantic`, `pydantic_settings`, domain symbol helpers |
| `btcbot.adapters.btcturk_http` | Synchronous BTCTurk REST client, auth headers, submit/cancel/fetch endpoints, retry and reconciliation helpers. | `BtcturkHttpClient`, `_private_request`, `submit_order`, `cancel_order`, `get_open_orders` | `httpx`, `build_auth_headers`, domain order models, retry utilities |
| `btcbot.adapters.btcturk` | Async BTCTurk integration utilities (WS, rate limit, retry, clock sync, reconcile, market-data wrappers). | `BtcturkWsClient` (`ws_client.py`), `RestClient` (`rest_client.py`) | `asyncio`, `httpx`, adapter instrumentation interfaces |
| `btcbot.adapters.exchange*` | Exchange abstraction contracts and Stage4 adapter interfaces. | `ExchangeClient`, `ExchangeClientStage4` | Domain models + typing protocols |
| `btcbot.services.state_store` | SQLite persistence layer for actions/orders/fills/positions/intents/ledger/risk/anomalies/stage7/audit schemas and IO operations. | `StateStore`, `transaction`, `record_action`, schema ensure methods | `sqlite3`, domain models, dataclasses |
| `btcbot.services.execution_service` | Order lifecycle refresh, stale cancellation, intent execution, dedupe and uncertain-error reconciliation. | `ExecutionService`, `cancel_stale_orders`, `execute_intents` | `ExchangeClient`, `StateStore`, market-data service, trading policy |
| `btcbot.services.decision_pipeline_service` | Stage5/7 decision pipeline: universe select -> strategy intents -> allocation -> order mapping + counters/reporting. | `DecisionPipelineService`, `run_cycle`, `CycleDecisionReport` | Allocation, universe, strategy registry, action-to-order adapter |
| `btcbot.services.market_data_service` | Market snapshot/bid-ask retrieval abstraction for strategy/risk layers. | `MarketDataService` | Exchange adapters, domain market models |
| `btcbot.services.risk_service` | Applies stage risk policy to candidate intents and records approved intents. | `RiskService.filter` | `RiskPolicy`, `StateStore` |
| `btcbot.risk.policy` | Core risk rule evaluation (cooldown, open order cap, notional limits, min-notional quantization). | `RiskPolicy`, `RiskPolicyContext` | `ExchangeRulesProvider`, `Intent`, symbol normalization |
| `btcbot.accounting.accounting_service` | Applies fills to positions and computes realized/unrealized pnl snapshots. | `AccountingService.refresh`, `_apply_fill`, `compute_total_pnl` | `ExchangeClient`, `StateStore`, accounting domain models |
| `btcbot.accounting.ledger` | Deterministic append-only ledger replay for self-financing accounting state and symbol-level pnl. | `AccountingLedger.recompute`, `_dedupe_events` | Accounting ledger event models/quantizers |
| `btcbot.security.secrets` | Secret provider chain, env/dotenv injection, scope and rotation validation. | `build_default_provider`, `inject_runtime_secrets`, `validate_secret_controls` | `os`, `Path`, redaction constants |
| `btcbot.security.redaction` | Sensitive value redaction for structured logs and free text. | `redact_value`, `redact_text`, `REDACTED` | `re`, recursive value walkers |
| `btcbot.logging_utils` | JSON logging formatter + root/httpx/httpcore logger setup. | `JsonFormatter`, `setup_logging` | `logging`, `json`, logging context, redaction |
| `btcbot.observability` | Instrumentation configuration and flush hooks for metrics/traces exporters. | `configure_instrumentation`, `get_instrumentation`, `flush_instrumentation` | OpenTelemetry APIs/exporters |
| `btcbot.strategies` | Strategy protocol and implementations for Stage3 and Stage5. | `Strategy` (`base.py`), `ProfitAwareStrategyV1.generate_intents`, Stage5 baseline strategy modules | Domain intents/context models |
| `btcbot.agent` | Agent policy contracts, guardrails, auditing, LLM/rule fallback logic. | `SafetyGuard.apply`, `RulePolicy`/`LlmPolicy` (`policy.py`), audit helpers | Agent contracts, pydantic models, logging |
| `btcbot.replay` | Replay dataset creation, capture, validation and utility operations. | `init_replay_dataset`, `capture_replay_dataset`, `validate_replay_dataset` | Filesystem IO, CSV/JSON parsers, BTCTurk public data |
| `btcbot.services.process_lock` | Cross-process single-instance lock for DB/account scope. | `single_instance_lock`, `ProcessLock` | `os`, `tempfile`, `fcntl`/`msvcrt` |

## D) Runtime Wiring
### Dependency injection / service initialization happens here
1. `src/btcbot/cli.py::main()` parses CLI args and dispatches by subcommand.
2. `src/btcbot/cli.py::_load_settings(env_file)` constructs `Settings` (supports optional `--env-file`).
3. `src/btcbot/cli.py` initializes logging (`setup_logging`), secret controls (`build_default_provider`, `inject_runtime_secrets`, `validate_secret_controls`, `log_secret_validation`), and instrumentation (`configure_instrumentation`).
4. `src/btcbot/services/exchange_factory.py` provides exchange adapter composition (`build_exchange_stage3` and stage-specific factory helpers).
5. `src/btcbot/cli.py::run_cycle`, `run_cycle_stage4`, and `run_cycle_stage7` perform constructor-based injection for `StateStore` and service graph objects (`PortfolioService`, `MarketDataService`, `AccountingService`, `RiskService`, `ExecutionService`, stage runners).
6. `src/btcbot/services/process_lock.py::single_instance_lock` wraps run execution to prevent duplicate bot instances per DB/account key.

### Startup chain (numbered, concrete)
1. `python -m btcbot` -> `src/btcbot/__main__.py` -> `btcbot.cli.main()`.
2. `main()` resolves command and loads `Settings` via `_load_settings()`.
3. Secret and policy preflight executes (`build_default_provider`, `inject_runtime_secrets`, `validate_secret_controls`, live-trading policy checks).
4. Logging + telemetry initialization (`setup_logging`, `configure_instrumentation`).
5. For trading flows, process lock acquired with `single_instance_lock(...)`.
6. Stage entry function executes:
   - Stage3 path: `run_cycle()` -> startup recovery -> market/account/strategy/risk/execution loop orchestration (with `run_with_optional_loop`).
   - Stage4 path: `run_cycle_stage4()` -> `Stage4CycleRunner` orchestration.
   - Stage7 path: `run_cycle_stage7()` or report/export/backtest helpers (`run_stage7_report`, `run_stage7_backtest`, etc.).
7. Shutdown path calls cleanup helpers (`flush_instrumentation`, `_flush_logging_handlers`, `_close_best_effort`).

### UNKNOWN markers
- UNKNOWN: exhaustive constructor graph of every nested dependency inside Stage4/Stage7 runners in one static diagram; confirm by tracing `src/btcbot/services/stage4_cycle_runner.py` and `src/btcbot/services/stage7_cycle_runner.py` call paths.