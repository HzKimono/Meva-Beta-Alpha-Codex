# BTCBot Code Map (src + key tests)

| File | Purpose | Key APIs | Side Effects | Used By |
|---|---|---|---|---|
| `src/btcbot/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/__main__.py` | Defines module symbols | `—` | Pure/none | LIVE |
| `src/btcbot/accounting/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/accounting/accounting_service.py` | Defines AccountingService | `AccountingService` | Logs | LIVE |
| `src/btcbot/accounting/ledger.py` | Defines AccountingLedger | `AccountingLedger` | Pure/none | LIVE |
| `src/btcbot/accounting/models.py` | Defines AccountingEventType (+4 more) | `AccountingEventType, AccountingLedgerEvent, PositionLot, SymbolPnlState, PortfolioAccountingState, quantize_money` | Pure/none | LIVE |
| `src/btcbot/adapters/action_to_order.py` | Defines build_deterministic_client_order_id() (+2 more) | `build_deterministic_client_order_id, build_exchange_rules, sized_action_to_order` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/adapters/btcturk/clock_sync.py` | Defines ServerTimeProvider (+1 more) | `ServerTimeProvider, ClockSyncService` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk/instrumentation.py` | Defines MetricsSink (+1 more) | `MetricsSink, InMemoryMetricsSink` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk/market_data.py` | Defines TopOfBook (+4 more) | `TopOfBook, TradeTick, MarketDataSnapshot, MarketDataBuildResult, MarketDataSnapshotBuilder, should_observe_only` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk/rate_limit.py` | Defines AsyncTokenBucket | `AsyncTokenBucket` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk/reconcile.py` | Defines OpenOrderView (+5 more) | `OpenOrderView, FillEvent, OrderTerminalUpdate, ReconcileResult, ReconcileState, Reconciler` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk/rest_client.py` | Defines RestErrorKind (+4 more) | `RestErrorKind, RestRequestError, RestReliabilityConfig, OrderOperationPolicy, BtcturkRestClient` | Network; Logs | LIVE |
| `src/btcbot/adapters/btcturk/retry.py` | Defines RetryDecision | `RetryDecision, parse_retry_after_seconds, compute_delay, async_retry` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk/ws_client.py` | Defines WsSocket (+3 more) | `WsSocket, WsEnvelope, WsIdleTimeoutError, BtcturkWsClient` | Network; Logs | LIVE |
| `src/btcbot/adapters/btcturk_auth.py` | Defines MonotonicNonceGenerator | `MonotonicNonceGenerator, compute_signature, build_auth_headers` | Pure/none | LIVE |
| `src/btcbot/adapters/btcturk_http.py` | Defines ConfigurationError (+4 more) | `ConfigurationError, BtcturkHttpClient, DryRunExchangeClient, BtcturkHttpClientStage4, DryRunExchangeClientStage4` | Network; Logs | BOTH |
| `src/btcbot/adapters/exchange.py` | Defines ExchangeClient | `ExchangeClient` | Pure/none | LIVE |
| `src/btcbot/adapters/exchange_stage4.py` | Defines OrderAck (+1 more) | `OrderAck, ExchangeClientStage4` | Pure/none | LIVE |
| `src/btcbot/adapters/replay_exchange.py` | Defines ReplayExchangeClient | `ReplayExchangeClient` | Pure/none | LIVE |
| `src/btcbot/agent/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/agent/audit.py` | Defines AgentAuditTrail | `AgentAuditTrail, redact_secrets, store_compact_text, store_compact_json` | Pure/none | LIVE |
| `src/btcbot/agent/contracts.py` | Defines DecisionAction (+6 more) | `DecisionAction, OrderIntentProposal, DecisionRationale, AgentDecision, AgentContext, SafeDecision` | Pure/none | LIVE |
| `src/btcbot/agent/guardrails.py` | Defines SafetyGuard | `SafetyGuard` | Pure/none | LIVE |
| `src/btcbot/agent/policy.py` | Defines AgentPolicy (+7 more) | `AgentPolicy, LlmClient, LlmPolicyError, PromptBuildResult, PromptBuilder, RuleBasedPolicy` | Logs | LIVE |
| `src/btcbot/cli.py` | Defines main() (+17 more) | `main, run_with_optional_loop, run_stage3_runtime, run_canary, run_cycle, run_cycle_stage4` | env:BTCTBOT_REPLAY_DATASET,STATE_DB_PATH; DB; Logs | BOTH |
| `src/btcbot/config.py` | Defines Settings | `Settings` | env:SETTINGS_ENV_FILE,SYMBOLS,UNIVERSE_SYMBOLS; DB; Network | BOTH |
| `src/btcbot/domain/account_snapshot.py` | Defines Holding (+1 more) | `Holding, AccountSnapshot` | Pure/none | LIVE |
| `src/btcbot/domain/accounting.py` | Defines TradeFill (+1 more) | `TradeFill, Position` | Pure/none | LIVE |
| `src/btcbot/domain/adaptation_models.py` | Defines Stage7Params (+1 more) | `Stage7Params, ParamChange` | Pure/none | LIVE |
| `src/btcbot/domain/allocation.py` | Defines SizedAction (+2 more) | `SizedAction, AllocationDecision, AllocationResult` | Pure/none | LIVE |
| `src/btcbot/domain/anomalies.py` | Defines AnomalyCode (+2 more) | `AnomalyCode, AnomalyEvent, DegradeDecision, combine_modes, decide_degrade` | Pure/none | LIVE |
| `src/btcbot/domain/decision_codes.py` | Defines ReasonCode | `ReasonCode, map_risk_reason` | Pure/none | LIVE |
| `src/btcbot/domain/execution_quality.py` | Defines PerSymbolExecutionQuality (+1 more) | `PerSymbolExecutionQuality, ExecutionQualitySnapshot, compute_execution_quality` | Pure/none | LIVE |
| `src/btcbot/domain/intent.py` | Defines Intent | `Intent, build_idempotency_key, to_order_intent` | Pure/none | LIVE |
| `src/btcbot/domain/ledger.py` | Defines LedgerEventType (+6 more) | `LedgerEventType, LedgerEvent, PositionLot, SymbolLedger, LedgerState, LedgerSnapshot` | Pure/none | LIVE |
| `src/btcbot/domain/market_data_models.py` | Defines Candle (+2 more) | `Candle, OrderBookTop, TickerStat` | Pure/none | LIVE |
| `src/btcbot/domain/models.py` | Defines ValidationError (+19 more) | `ValidationError, OrderSide, OrderStatus, ExchangeOrderStatus, ReconcileStatus, Balance` | Pure/none | LIVE |
| `src/btcbot/domain/order_intent.py` | Defines OrderIntent | `OrderIntent` | Pure/none | LIVE |
| `src/btcbot/domain/order_state.py` | Defines OrderStatus (+2 more) | `OrderStatus, Stage7Order, OrderEvent, short_hash, make_order_id, make_event_id` | Pure/none | LIVE |
| `src/btcbot/domain/portfolio_policy_models.py` | Defines PositionSnapshot (+4 more) | `PositionSnapshot, PortfolioSnapshot, TargetAllocation, RebalanceAction, PortfolioPlan` | Pure/none | LIVE |
| `src/btcbot/domain/risk_budget.py` | Defines Mode (+3 more) | `Mode, RiskLimits, RiskSignals, RiskDecision, decide_mode` | Pure/none | LIVE |
| `src/btcbot/domain/risk_models.py` | Defines RiskDecision (+1 more) | `RiskDecision, ExposureSnapshot, combine_risk_modes, stable_hash_payload` | Pure/none | LIVE |
| `src/btcbot/domain/stage4.py` | Defines OrderId (+9 more) | `OrderId, ClientOrderId, LifecycleActionType, ExchangeRules, Order, Fill` | Pure/none | LIVE |
| `src/btcbot/domain/strategy_core.py` | Defines Signal (+6 more) | `Signal, OrderBookSummary, PositionSummary, OpenOrdersSummary, StrategyKnobs, StrategyContext` | Pure/none | LIVE |
| `src/btcbot/domain/symbols.py` | Defines canonical_symbol() (+2 more) | `canonical_symbol, split_symbol, quote_currency` | Pure/none | LIVE |
| `src/btcbot/domain/universe.py` | Defines UniverseCandidate (+1 more) | `UniverseCandidate, UniverseSelectionResult` | Pure/none | LIVE |
| `src/btcbot/domain/universe_models.py` | Defines UniverseKnobs (+1 more) | `UniverseKnobs, SymbolInfo` | Pure/none | LIVE |
| `src/btcbot/logging_context.py` | Defines get_logging_context() (+2 more) | `get_logging_context, with_logging_context, with_cycle_context` | Pure/none | LIVE |
| `src/btcbot/logging_utils.py` | Defines JsonFormatter | `JsonFormatter, setup_logging` | env:LOG_LEVEL; Network; Logs | BOTH |
| `src/btcbot/observability.py` | Defines CorrelationContext (+3 more) | `CorrelationContext, Instrumentation, NoopInstrumentation, OTelInstrumentation, configure_instrumentation, get_instrumentation` | Logs | BOTH |
| `src/btcbot/observability_decisions.py` | Defines emit_decision() | `emit_decision` | Logs | LIVE |
| `src/btcbot/planning_kernel.py` | Defines OpenOrderView (+12 more) | `OpenOrderView, MarketDataSnapshot, PortfolioState, Intent, Plan, PlanningContext` | Pure/none | LIVE |
| `src/btcbot/ports_price_conversion.py` | Defines FeeConversionRateError (+1 more) | `FeeConversionRateError, PriceConverter` | Pure/none | LIVE |
| `src/btcbot/replay/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/replay/tools.py` | Defines ReplayCaptureConfig | `ReplayCaptureConfig, init_replay_dataset, capture_replay_dataset` | Pure/none | LIVE |
| `src/btcbot/replay/validate.py` | Defines ValidationIssue (+1 more) | `ValidationIssue, DatasetValidationReport, validate_replay_dataset` | Pure/none | LIVE |
| `src/btcbot/risk/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/risk/budget.py` | Defines SelfFinancingPolicy (+2 more) | `SelfFinancingPolicy, RiskBudgetView, RiskBudgetPolicy` | Pure/none | LIVE |
| `src/btcbot/risk/exchange_rules.py` | Defines ExchangeRulesUnavailableError (+3 more) | `ExchangeRulesUnavailableError, ExchangeRules, ExchangeRulesProvider, MarketDataExchangeRulesProvider` | Network; Logs | LIVE |
| `src/btcbot/risk/policy.py` | Defines RiskPolicyContext (+1 more) | `RiskPolicyContext, RiskPolicy` | Logs | LIVE |
| `src/btcbot/security/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/security/redaction.py` | Defines redact_value() (+5 more) | `redact_value, sanitize_mapping, sanitize_text, safe_repr, redact_data, redact_text` | Pure/none | LIVE |
| `src/btcbot/security/secrets.py` | Defines SecretProvider (+4 more) | `SecretProvider, EnvSecretProvider, DotenvSecretProvider, ChainedSecretProvider, SecretValidationResult, build_default_provider` | Logs | LIVE |
| `src/btcbot/services/account_snapshot_service.py` | Defines AccountSnapshotService | `AccountSnapshotService` | Logs | LIVE |
| `src/btcbot/services/accounting_service_stage4.py` | Defines AccountingIntegrityError (+2 more) | `AccountingIntegrityError, FetchFillsResult, AccountingService` | Pure/none | LIVE |
| `src/btcbot/services/adaptation_service.py` | Defines AdaptationService | `AdaptationService` | Pure/none | LIVE |
| `src/btcbot/services/allocation_service.py` | Defines AllocationKnobs (+1 more) | `AllocationKnobs, AllocationService` | Pure/none | LIVE |
| `src/btcbot/services/anomaly_detector_service.py` | Defines AnomalyDetectorConfig (+1 more) | `AnomalyDetectorConfig, AnomalyDetectorService` | Pure/none | LIVE |
| `src/btcbot/services/client_order_id_service.py` | Defines build_exchange_client_id() (+1 more) | `build_exchange_client_id, is_btcturk_client_id_safe` | Pure/none | LIVE |
| `src/btcbot/services/cycle_account_snapshot.py` | Defines CycleHolding (+1 more) | `CycleHolding, CycleAccountSnapshot, build_cycle_account_snapshot` | Pure/none | LIVE |
| `src/btcbot/services/decision_pipeline_service.py` | Defines CycleDecisionReport (+1 more) | `CycleDecisionReport, DecisionPipelineService` | Network; Logs | LIVE |
| `src/btcbot/services/doctor.py` | Defines DoctorCheck (+1 more) | `DoctorCheck, DoctorReport, doctor_status, run_health_checks, normalize_drawdown_ratio, evaluate_slo_status_for_rows` | DB | LIVE |
| `src/btcbot/services/dynamic_universe_service.py` | Defines DynamicUniverseSelection (+1 more) | `DynamicUniverseSelection, DynamicUniverseService` | Logs | LIVE |
| `src/btcbot/services/effective_universe.py` | Defines EffectiveUniverse | `EffectiveUniverse, resolve_effective_universe` | Logs | LIVE |
| `src/btcbot/services/exchange_factory.py` | Defines build_exchange_stage3() (+1 more) | `build_exchange_stage3, build_exchange_stage4` | Logs | LIVE |
| `src/btcbot/services/exchange_rules_service.py` | Defines SymbolRules (+2 more) | `SymbolRules, SymbolRulesResolution, ExchangeRulesService` | Logs | LIVE |
| `src/btcbot/services/execution_errors.py` | Defines ExecutionErrorCategory | `ExecutionErrorCategory, classify_exchange_error` | Network | LIVE |
| `src/btcbot/services/execution_service.py` | Defines SubmitBlockedDueToUnknownError (+2 more) | `SubmitBlockedDueToUnknownError, LiveTradingNotArmedError, ExecutionService` | env:EXECUTION_BALANCE_SAFETY_BUFFER_RATIO,EXECUTION_ESTIMATED_FEE_BPS,EXECUTION_QUOTE_ASSET,EXECUTION_SELL_FEE_IN_BASE_BPS; DB; Network; Logs | LIVE |
| `src/btcbot/services/execution_service_stage4.py` | Defines ExecutionReport (+2 more) | `ExecutionReport, ReplaceGroup, ExecutionService` | Logs | LIVE |
| `src/btcbot/services/execution_wrapper.py` | Defines UncertainResult (+1 more) | `UncertainResult, ExecutionWrapper` | Logs | LIVE |
| `src/btcbot/services/exposure_tracker.py` | Defines ExposureTracker | `ExposureTracker` | Pure/none | LIVE |
| `src/btcbot/services/ledger_service.py` | Defines LedgerIngestResult (+6 more) | `LedgerIngestResult, SymbolPnlBreakdown, PnlReport, SimulatedFill, FinancialBreakdown, LedgerCheckpoint` | Logs | LIVE |
| `src/btcbot/services/market_data_replay.py` | Defines MarketDataSchemaError (+1 more) | `MarketDataSchemaError, MarketDataReplay` | Pure/none | LIVE |
| `src/btcbot/services/market_data_service.py` | Defines SymbolRulesNotFoundError (+6 more) | `SymbolRulesNotFoundError, MarketDataSnapshot, MarketDataFreshness, MarketDataProvider, RestMarketDataProvider, WsMarketDataProvider` | Logs | LIVE |
| `src/btcbot/services/metrics_collector.py` | Defines MetricsCollector | `MetricsCollector` | Pure/none | LIVE |
| `src/btcbot/services/metrics_service.py` | Defines CycleMetrics | `CycleMetrics, build_cycle_metrics, persist_cycle_metrics` | Pure/none | LIVE |
| `src/btcbot/services/oms_service.py` | Defines TransientOMSAdapterError (+6 more) | `TransientOMSAdapterError, NetworkTimeout, RateLimitError, TemporaryUnavailable, NonRetryableOMSAdapterError, Stage7MarketSimulator` | Network | LIVE |
| `src/btcbot/services/order_builder_service.py` | Defines OrderBuilderService | `OrderBuilderService` | Pure/none | LIVE |
| `src/btcbot/services/order_lifecycle_service.py` | Defines LifecyclePlan (+1 more) | `LifecyclePlan, OrderLifecycleService` | Pure/none | LIVE |
| `src/btcbot/services/param_bounds.py` | Defines ParamBounds | `ParamBounds, has_rollback_trigger` | Pure/none | LIVE |
| `src/btcbot/services/parity.py` | Defines find_missing_stage7_parity_tables() (+2 more) | `find_missing_stage7_parity_tables, compute_run_fingerprint, compare_fingerprints` | DB | LIVE |
| `src/btcbot/services/planning_kernel_adapters.py` | Defines Stage4PlanConsumer (+3 more) | `Stage4PlanConsumer, Stage7PlanConsumer, InMemoryExecutionPort, Stage7ExecutionPort` | Network | LIVE |
| `src/btcbot/services/portfolio_policy_service.py` | Defines PortfolioPolicyService | `PortfolioPolicyService, split_symbol` | Pure/none | LIVE |
| `src/btcbot/services/portfolio_service.py` | Defines PortfolioService | `PortfolioService` | Pure/none | LIVE |
| `src/btcbot/services/price_conversion_service.py` | Defines MarkPriceConverter | `MarkPriceConverter` | Pure/none | LIVE |
| `src/btcbot/services/process_lock.py` | Defines ProcessLock | `ProcessLock, single_instance_lock` | env:BTCBOT_LOCK_DIR; DB | LIVE |
| `src/btcbot/services/rate_limiter.py` | Defines EndpointBudget (+2 more) | `EndpointBudget, TokenBucketRateLimiter, AsyncTokenBucketRateLimiter, map_endpoint_group` | Pure/none | LIVE |
| `src/btcbot/services/reconcile_service.py` | Defines ReconcileResult (+1 more) | `ReconcileResult, ReconcileService` | Pure/none | LIVE |
| `src/btcbot/services/retry.py` | Defines RetryResponseLike (+1 more) | `RetryResponseLike, RetryAttempt, parse_retry_after_seconds, retry_with_backoff, retry_with_backoff_async` | Pure/none | LIVE |
| `src/btcbot/services/risk_budget_service.py` | Defines BudgetDecision (+3 more) | `BudgetDecision, CapitalPolicyResult, CapitalPolicyError, RiskBudgetService` | Logs | LIVE |
| `src/btcbot/services/risk_policy.py` | Defines RiskDecision (+1 more) | `RiskDecision, RiskPolicy` | Pure/none | LIVE |
| `src/btcbot/services/risk_service.py` | Defines RiskService | `RiskService` | Logs | LIVE |
| `src/btcbot/services/stage4_cycle_runner.py` | Defines MarketSnapshot (+4 more) | `MarketSnapshot, Stage4ConfigurationError, Stage4ExchangeError, Stage4InvariantError, Stage4CycleRunner` | DB; Network; Logs | LIVE |
| `src/btcbot/services/stage4_planning_kernel_integration.py` | Defines Stage4MarketDataSnapshot (+7 more) | `Stage4MarketDataSnapshot, Stage4PortfolioState, Stage4PlanningContext, Stage4KernelPlanningResult, Stage4UniverseSelectorAdapter, Stage4DecisionStrategyAdapter` | Network | LIVE |
| `src/btcbot/services/stage7_backtest_runner.py` | Defines BacktestSummary (+1 more) | `BacktestSummary, Stage7BacktestRunner` | DB | LIVE |
| `src/btcbot/services/stage7_cycle_runner.py` | Defines Stage7CycleRunner | `Stage7CycleRunner` | DB; Logs | LIVE |
| `src/btcbot/services/stage7_planning_kernel_integration.py` | Defines Stage7MarketDataSnapshot (+5 more) | `Stage7MarketDataSnapshot, Stage7PortfolioState, Stage7UniverseSelectorAdapter, Stage7PortfolioStrategyAdapter, Stage7PassThroughAllocator, Stage7OrderIntentBuilderAdapter` | Pure/none | LIVE |
| `src/btcbot/services/stage7_risk_budget_service.py` | Defines Stage7RiskInputs (+1 more) | `Stage7RiskInputs, Stage7RiskBudgetService` | Pure/none | LIVE |
| `src/btcbot/services/stage7_single_cycle_driver.py` | Defines BacktestSummary (+1 more) | `BacktestSummary, Stage7SingleCycleDriver` | DB | LIVE |
| `src/btcbot/services/startup_recovery.py` | Defines StartupRecoveryResult (+1 more) | `StartupRecoveryResult, StartupRecoveryService` | Logs | LIVE |
| `src/btcbot/services/state_store.py` | Defines StoredOrder (+9 more) | `StoredOrder, StoredIntentTs, AppendResult, LedgerReducerCheckpoint, IdempotencyConflictError, SubmitDedupeDecision` | DB; Logs | BOTH |
| `src/btcbot/services/strategy_service.py` | Defines StrategyService | `StrategyService` | Pure/none | LIVE |
| `src/btcbot/services/sweep_service.py` | Defines SweepService | `SweepService` | Logs | LIVE |
| `src/btcbot/services/trading_policy.py` | Defines PolicyBlockReason (+1 more) | `PolicyBlockReason, LiveSideEffectsPolicyResult, policy_reason_to_code, validate_live_side_effects_policy, policy_block_message` | Pure/none | LIVE |
| `src/btcbot/services/universe_selection_service.py` | Defines UniverseSelectionService | `UniverseSelectionService` | Pure/none | LIVE |
| `src/btcbot/services/universe_service.py` | Defines select_universe() | `select_universe` | Pure/none | LIVE |
| `src/btcbot/services/unknown_order_registry.py` | Defines UnknownOrderRecord (+1 more) | `UnknownOrderRecord, UnknownOrderRegistry` | Pure/none | LIVE |
| `src/btcbot/strategies/__init__.py` | Defines module symbols | `—` | Pure/none | BOTH |
| `src/btcbot/strategies/base.py` | Defines Strategy | `Strategy` | Pure/none | LIVE |
| `src/btcbot/strategies/baseline_mean_reversion.py` | Defines BaselineMeanReversionStrategy | `BaselineMeanReversionStrategy` | Pure/none | LIVE |
| `src/btcbot/strategies/context.py` | Defines StrategyContext | `StrategyContext` | Pure/none | LIVE |
| `src/btcbot/strategies/profit_v1.py` | Defines ProfitAwareStrategyV1 | `ProfitAwareStrategyV1` | Pure/none | LIVE |
| `src/btcbot/strategies/stage5_core.py` | Defines BaseStrategy (+1 more) | `BaseStrategy, StrategyRegistry` | Pure/none | LIVE |

## Key Test Files

| File | Purpose | Key APIs | Side Effects | Used By |
|---|---|---|---|---|
| `tests/test_cli.py` | Validates cli behavior | `test_health_returns_zero_on_success, test_health_returns_nonzero_on_failure, test_health_prints_effective_risk_config, test_health_prints_effective_risk_config_on_unreachable…` | Test I/O only | BOTH |
| `tests/test_canary_cli.py` | Validates canary cli behavior | `test_canary_aborts_on_doctor_fail, test_canary_aborts_on_doctor_warn_without_allow_warn, test_canary_proceeds_on_pass_and_forces_caps, test_canary_loop_hard_stops_on_doctor_fail_midway…` | Test I/O only | BOTH |
| `tests/test_config.py` | Validates config behavior | `test_parse_symbols_json_list, test_parse_symbols_csv, test_loads_values_from_env_file, test_invalid_settings_raise…` | Test I/O only | BOTH |
| `tests/test_execution_service_live_arming.py` | Validates execution service live arming behavior | `test_live_arming_blocks_side_effects, test_execute_intents_quantizes_before_submit, test_execute_intents_enforces_min_total, test_execute_intents_live_not_armed_records_no_place_action…` | Test I/O only | BOTH |
| `tests/test_execution_service.py` | Validates execution service behavior | `test_kill_switch_logs_would_place_without_side_effects, test_kill_switch_logs_would_cancel_without_side_effects, test_dry_run_cancel_stale_logs_would_cancel_without_side_effects, test_sell_precheck_with_zero_base_balance_skips_exchange_submit…` | Test I/O only | BOTH |
| `tests/test_state_store.py` | Validates state store behavior | `test_risk_state_current_table_is_created_on_fresh_db, test_orders_unknown_retry_columns_are_added_for_legacy_db, test_record_action_returns_action_id_and_dedupes, test_state_store_strict_instance_lock_fails_on_active_conflict…` | Test I/O only | BOTH |
| `tests/test_process_lock.py` | Validates process lock behavior | `test_single_instance_lock_blocks_second_acquire, test_single_instance_lock_reacquire_after_release, test_single_instance_lock_writes_pid, test_single_instance_lock_removes_pid_on_exception…` | Test I/O only | BOTH |
| `tests/test_startup_recovery.py` | Validates startup recovery behavior | `test_run_with_prices_calls_refresh_and_runs_invariants, test_run_with_do_refresh_lifecycle_true_calls_refresh_and_marks, test_run_without_prices_forces_observe_only_and_skips_refresh, test_startup_recovery_is_idempotent…` | Test I/O only | BOTH |
| `tests/test_doctor.py` | Validates doctor behavior | `test_doctor_report_ok_true_when_all_checks_pass, test_doctor_report_ok_true_when_warn_present, test_doctor_report_ok_false_when_any_check_fails, test_doctor_accepts_creatable_db_path…` | Test I/O only | BOTH |
| `tests/test_risk_service.py` | Validates risk service behavior | `test_risk_service_phantom_unknown_is_closed_after_threshold_and_not_counted, test_risk_service_late_fill_safety_does_not_close_on_first_missing_observation, test_risk_service_counts_reconciled_open_orders_and_identifiers, test_risk_service_phantom_unknown_is_closed_after_time_window` | Test I/O only | BOTH |
| `tests/test_market_data_freshness_gate.py` | Validates market data freshness gate behavior | `test_market_data_deterministic_api_returns_bids_and_freshness_same_snapshot, test_market_data_freshness_ws_disconnected_is_stale, test_market_data_freshness_ws_age_stale_without_fallback, test_market_data_ws_age_stale_falls_back_to_rest_when_enabled` | Test I/O only | BOTH |
| `tests/test_btcturk_http.py` | Validates btcturk http behavior | `test_should_retry_on_429_and_5xx, test_get_orderbook_parses_valid_payload, test_get_orderbook_rejects_malformed_payloads, test_get_balances_parses_comma_decimal_values…` | Test I/O only | BOTH |
| `tests/test_stage7_run_integration.py` | Validates stage7 run integration behavior | `test_stage7_run_dry_run_persists_trace_and_metrics, test_stage7_run_respects_reduce_risk_mode, test_stage7_run_skips_open_order_with_missing_mark_price, test_stage7_universe_selection_does_not_change_ledger_metrics_shape…` | Test I/O only | BOTH |
## Coupling/Risk Findings
- `btcbot.cli` is a god-module: it parses args, enforces policy, opens SQLite directly (`sqlite3.connect`), orchestrates cycle services, runs replay/backtest export, and health checks in one file. This increases blast radius for both `run` and `health` paths.
- DB access is not fully isolated to `StateStore`: direct `sqlite3` usage appears in `btcbot.cli`, `btcbot.services.doctor`, `btcbot.services.parity`, and `btcbot.services.stage7_backtest_runner`, so the “single writer” boundary is porous.
- MONITOR (`run_health`) constructs a writable `StateStore` object (`StateStore(db_path=settings.state_db_path)`), so monitor path is not read-only by construction.
- Process-role concept (`APP_ROLE=live|monitor`) is not a first-class setting; role behavior is inferred from flags (`LIVE_TRADING`, `DRY_RUN`, `SAFE_MODE`, `KILL_SWITCH`), which can drift into ambiguous combinations.
- Configuration concerns are centralized in `Settings` but still partially bypassed by ad-hoc env reads (`STATE_DB_PATH` / replay env reads in `cli.py`), creating a split config surface.
- Stage-specific services (`stage4_*`, `stage7_*`) and main runtime services share the same import space under `btcbot.services`, increasing accidental cross-import risk.
- `ExecutionService` has substantial policy/env coupling (arming, safe mode, kill switch, fee knobs), while policy logic also exists in `trading_policy.validate_live_side_effects_policy`, creating dual enforcement points.
- Networking and orchestration are interleaved in several places (e.g., `stage4_cycle_runner`, CLI), reducing substitution testability versus strict port-adapter boundaries.

## Refactor Actions (ordered)
1. Introduce `AppRole` in `Settings` (`live|monitor`) and validate hard profiles at config load; wire `cli run` to require `live` and `cli health` to require `monitor`.
2. Extract `run`/`health` command handlers from `btcbot.cli` into `btcbot.services.runtime_run` and `btcbot.services.runtime_health` to reduce CLI to argument dispatch only.
3. Add `ReadOnlyStateStore` protocol (or `StateReader`) and switch `run_health`/doctor checks to read-only interfaces; block write methods when `AppRole=monitor`.
4. Move all raw `sqlite3.connect` usage behind persistence adapters (`state_store` + read repositories), starting with `cli.py` and `services/parity.py`.
5. Consolidate live side-effect gates into one policy object used by `ExecutionService` and CLI prechecks to remove duplicated arming logic.
6. Split `services` into `services/runtime`, `services/stage4`, `services/stage7` namespaces and add import-linter rules to forbid cross-stage cycles.
7. Add a `NoopExecutionService` in monitor wiring and assert it in tests (health path must not call submit/cancel APIs).
8. Add architecture tests that fail on new direct imports of `sqlite3` outside persistence modules.
9. Centralize env access: replace residual `os.getenv` reads in `cli.py` with `Settings` fields so all runtime config is typed and validated in one place.
10. Add explicit DB ownership metadata (`db_role`, `db_uuid`) and startup checks in `StateStore` to prevent live/monitor cross-opening the same file.
