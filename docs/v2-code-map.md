# V2 directory and file guide

Companion to the [code review](v2-code-review.md). This inventory covers every Python source file under `src/gmx_crypto_bot_v2` as of 2026-09-28. Generated `__pycache__`/bytecode files are not source modules.

## How the pieces fit

Collection: `collector.py → application/collection.py → collection/coordinator.py → sources + collection stages → evidence stores`.

Validation: `validator.py → application/validation.py → evidence repositories → reconstruction → models/checks → reporting`.

Replay: `replay.py → application/replay.py → evidence/indexed_recording.py → reconstruction/observed.py → state/event digests`.

## Storage ownership

| Store | Purpose | Rebuildable from the other saved evidence? |
|---|---|---|
| `.collection/rpc/requests.sqlite` | Durable RPC response reuse across attempts | Treat as saved acquisition evidence; do not discard during resume |
| `.collection/traces.sqlite` | Durable transaction traces and gas probes | Treat as authoritative trace evidence |
| `.gmx-v2/catalog.sqlite` | Index and decoded projection for replay/validation | Yes |
| `.collection/journal.json` | Identity and committed artifact/row digests | Collection progress, not a disposable index |
| `raw/`, `events.jsonl`, snapshot JSON | Original responses, normalized observations, historical anchors | Preserve |

## Package root — Package root

The three root modules support `python -m` commands. Installed console scripts call
`application/` directly; all other code imports the implementation subpackages.

| File | Purpose |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/__init__.py) | Package marker. |
| [collector.py](../src/gmx_crypto_bot_v2/collector.py) | Module CLI for collection. |
| [replay.py](../src/gmx_crypto_bot_v2/replay.py) | Module CLI for replay. |
| [validator.py](../src/gmx_crypto_bot_v2/validator.py) | Module CLI for validation. |

## application — Command orchestration

Parses commands, opens evidence sessions, runs workflows, and presents reports.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/application/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [collection.py](../src/gmx_crypto_bot_v2/application/collection.py) | CLI for the unified, read-only collection pipeline. Main definitions: `parser`, `run`, `main`, `stage_main`. |
| [replay.py](../src/gmx_crypto_bot_v2/application/replay.py) | Stream canonical observations and verify deterministic replay digests. Main definitions: `ReplayReport`, `canonical_events`, `_replay_with_state`, `replay`, `main`. |
| [validation.py](../src/gmx_crypto_bot_v2/application/validation.py) | Assemble verified evidence, execute order checks and publish compatible reports. Main definitions: `_validate_orders`, `validate_orders`, `main`. |

## collection — Evidence acquisition

Captures the base recording and historical dependencies, coordinates resume and publication, and owns collection progress.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/collection/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [accrual.py](../src/gmx_crypto_bot_v2/collection/accrual.py) | Collect opening and closing funding and borrowing state. Main definitions: `collect`. |
| [base.py](../src/gmx_crypto_bot_v2/collection/base.py) | Capture the fixed recording window, opening checkpoint and public observations. Main definitions: `GmxCollector`, `load_spec`. |
| [checkpoint.py](../src/gmx_crypto_bot_v2/collection/checkpoint.py) | Capture block-pinned opening orders, positions and market state. Main definitions: `Rpc`, `_word`, `_address_word`, `_bytes32_word`, `_words`, `_uint`, `_int`, plus supporting helpers. |
| [coordinator.py](../src/gmx_crypto_bot_v2/collection/coordinator.py) | One resumable evidence pipeline; readiness is distinct from economic validity. Main definitions: `CollectionCoordinator`. |
| [fees.py](../src/gmx_crypto_bot_v2/collection/fees.py) | Collect block-pinned position and UI fee configuration. Main definitions: `fetch_opening_fee_configuration`. |
| [impact.py](../src/gmx_crypto_bot_v2/collection/impact.py) | Collect and verify opening position-impact configuration. Main definitions: `fetch_opening_impact_factors`. |
| [journal.py](../src/gmx_crypto_bot_v2/collection/journal.py) | Collection progress survives deletion of the disposable SQLite catalog. Main definitions: `CollectionJournal`. |
| [ranges.py](../src/gmx_crypto_bot_v2/collection/ranges.py) | Split provider-limited log ranges while preserving explicit gaps. Main definitions: `RangeGap`, `adaptive_ranges`. |
| [referral.py](../src/gmx_crypto_bot_v2/collection/referral.py) | Expand and collect trader, code, affiliate, tier and pro dependencies. Main definitions: `collect_referral_ranges`, `executed_traders`, `collect`. |
| [swaps.py](../src/gmx_crypto_bot_v2/collection/swaps.py) | Collect route-market configuration, pools and shared virtual inventories. Main definitions: `collect`. |
| [traces.py](../src/gmx_crypto_bot_v2/collection/traces.py) | Capture block-bound transaction traces and optional opcode evidence. Main definitions: `select_transactions`, `recorded_hashes`, `collect_transaction`, `_number`, `collect_gas_probe`. |
| [window.py](../src/gmx_crypto_bot_v2/collection/window.py) | Resolve a rolling window and fetch the validator's closing configuration. Main definitions: `utc`, `resolve_window`. |

## sources — External transports and RPC reuse

Performs HTTP/JSON-RPC reads, retries failures, preserves responses, and caches successful RPC work.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/sources/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [http.py](../src/gmx_crypto_bot_v2/sources/http.py) | Timestamped public HTTP observations, with bounded retry and raw capture. Main definitions: `fetch_json`. |
| [request_store.py](../src/gmx_crypto_bot_v2/sources/request_store.py) | Durable compressed RPC responses, independent of the derived evidence catalog. Main definitions: `request_key`, `RequestStore`. |
| [resumable.py](../src/gmx_crypto_bot_v2/sources/resumable.py) | Read-only RPC work units, preserving exact responses before reuse. Main definitions: `ResumableRpc`. |
| [rpc.py](../src/gmx_crypto_bot_v2/sources/rpc.py) | Read-only JSON-RPC transport with raw capture, retries and endpoint redaction. Main definitions: `SourceError`, `utc_now`, `hex_block`, `parse_hex_number`, `redact_rpc_endpoint`, `retryable_rpc_error`, `PublicJsonRpc`. |

## evidence — Storage and access

Persists raw/normalized evidence, indexes it in a rebuildable catalog, supplies repositories, and stores durable traces.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/evidence/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [anchors.py](../src/gmx_crypto_bot_v2/evidence/anchors.py) | Resolve recorded block identities for historical configuration. Main definitions: `_recorded_block_hash`. |
| [artifacts.py](../src/gmx_crypto_bot_v2/evidence/artifacts.py) | Preserve exact public response bodies in rotated compressed bundles. Main definitions: `RawArtifactStore`. |
| [catalog.py](../src/gmx_crypto_bot_v2/evidence/catalog.py) | Rebuildable SQLite projection of authoritative recording evidence. Main definitions: `encode`, `fingerprint`, `digest`, `EvidenceCatalog`. |
| [discovery.py](../src/gmx_crypto_bot_v2/evidence/discovery.py) | Discover collection targets from recorded facts, without economic checks. Main definitions: `discover`. |
| [indexed_recording.py](../src/gmx_crypto_bot_v2/evidence/indexed_recording.py) | Repeatable, bounded-memory canonical recording view. Main definitions: `IndexedEvents`, `load_indexed_recording`. |
| [publication.py](../src/gmx_crypto_bot_v2/evidence/publication.py) | Durable atomic publication and single-writer recording ownership. Main definitions: `atomic_bytes`, `atomic_json`, `recording_writer`, `atomic_copy`. |
| [recording.py](../src/gmx_crypto_bot_v2/evidence/recording.py) | Legacy-compatible normalized recording envelopes and append-only capture. Main definitions: `RecordedEvent`, `JsonlRecorder`, `load_recording`, `_validate_sequence`. |
| [repository.py](../src/gmx_crypto_bot_v2/evidence/repository.py) | Verified evidence access shared by collection, reconstruction and replay. Main definitions: `EvidenceRepository`, `evidence_session`, `active_repository`, `event_rows`, `raw_logs`, `receipt_rows`, `block_timestamps`. |
| [snapshots.py](../src/gmx_crypto_bot_v2/evidence/snapshots.py) | Select additive V2 supplements without rewriting original recordings. Main definitions: `snapshot_path`. |
| [timestamps.py](../src/gmx_crypto_bot_v2/evidence/timestamps.py) | Block timestamps from the shared evidence repository. Main definitions: `load_timestamps`. |
| [trace_store.py](../src/gmx_crypto_bot_v2/evidence/trace_store.py) | Durable transaction and gas-probe evidence, independent of derived indexes. Main definitions: `sha256`, `TraceStore`. |
| [traces.py](../src/gmx_crypto_bot_v2/evidence/traces.py) | Lazy trace evidence supplied to checks; checks do not choose file paths. Main definitions: `TraceEvidence`, `TraceEvidenceSource`, `TraceRepository`. |

## domain — Shared protocol vocabulary

Defines order/event types, coordinates, ABI codecs, storage keys, and evidence identities. It should not perform external I/O.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/domain/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [accrual.py](../src/gmx_crypto_bot_v2/domain/accrual.py) | GMX funding and borrowing storage-key inventories. Main definitions: `slots`, `decode`. |
| [checkpoint.py](../src/gmx_crypto_bot_v2/domain/checkpoint.py) | Opening-order normalization shared by capture and replay. Main definitions: `normalize_recorded_order_checkpoint`. |
| [configuration.py](../src/gmx_crypto_bot_v2/domain/configuration.py) | GMX fee storage-key descriptions. Main definitions: `ConfigKey`, `fee_keys`. |
| [constants.py](../src/gmx_crypto_bot_v2/domain/constants.py) | Order types, event families, precision and deployment constants. |
| [entries.py](../src/gmx_crypto_bot_v2/domain/entries.py) | Canonical decoded event entries and coordinate helpers. Main definitions: `_decode_recorded_log`, `_event_entry`, `_event_entry_from_log`, `_order_key`, `_coordinate`, `decoded_log`. |
| [events.py](../src/gmx_crypto_bot_v2/domain/events.py) | Decode GMX EventEmitter ABI payloads without source access. Main definitions: `EventDecodeError`, `DecodedEventLog`, `event_name_from_data`, `decode_event_log`, `_decode_items`, `_decode_item_value`, `_hex_bytes`, plus supporting helpers. |
| [evidence.py](../src/gmx_crypto_bot_v2/domain/evidence.py) | Immutable identities crossing the collection and validation boundaries. Main definitions: `Coordinate`, `BlockReference`, `EvidenceReference`, `TraceRequirement`, `CollectionRequirements`, `CheckResult`, `OrderContext`. |
| [filtering.py](../src/gmx_crypto_bot_v2/domain/filtering.py) | Select target-market fields from public observations. Main definitions: `contains_address`, `event_name`, `target_snapshot`, `contains_value`. |
| [gas.py](../src/gmx_crypto_bot_v2/domain/gas.py) | Reviewed bytecode-specific gas calibration; unknown code stays unsupported. |
| [keys.py](../src/gmx_crypto_bot_v2/domain/keys.py) | Ethereum Keccak and GMX configuration-key encoding. Main definitions: `keccak256`, `config_base_key`, `config_market_side_data`, `config_market_data`. |
| [payments.py](../src/gmx_crypto_bot_v2/domain/payments.py) | Typed execution-fee call inputs and committed trace traversal. Main definitions: `PaymentInput`, `TraceFrame`, `walk_trace`. |
| [referral.py](../src/gmx_crypto_bot_v2/domain/referral.py) | Referral ABI encoding, log decoding and storage-key identifiers. Main definitions: `word`, `calldata`, `datastore_key`, `words`, `address`, `decode_referral_log`, `config_change`. |
| [swap_keys.py](../src/gmx_crypto_bot_v2/domain/swap_keys.py) | ABI calls and GMX swap storage keys, shared below the pipeline layers. Main definitions: `call_data`, `key`, `market_keys`, `market_field_key`. |

## reconstruction — Historical state reconstruction

Builds position, fee, referral, swap, and accrual histories from saved evidence before checks run.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/reconstruction/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [accrual.py](../src/gmx_crypto_bot_v2/reconstruction/accrual.py) | Independently replay funding and borrowing from block-pinned anchors. Main definitions: `coordinate`, `AccrualReplay`. |
| [accrual_configuration.py](../src/gmx_crypto_bot_v2/reconstruction/accrual_configuration.py) | Verify and decode historical accrual snapshot coverage. Main definitions: `load_snapshot`. |
| [context.py](../src/gmx_crypto_bot_v2/reconstruction/context.py) | Typed evidence shared by the dependency-ordered order checks. Main definitions: `ValidationContext`. |
| [fees.py](../src/gmx_crypto_bot_v2/reconstruction/fees.py) | Build block-bounded fee-configuration histories. Main definitions: `ConfigHistory`, `build_fee_histories`. |
| [impact.py](../src/gmx_crypto_bot_v2/reconstruction/impact.py) | Version position-impact factors from opening, closing and change evidence. Main definitions: `HistoricalFactor`, `load_factor_histories`. |
| [observed.py](../src/gmx_crypto_bot_v2/reconstruction/observed.py) | Observable-state projection used by deterministic recording replay. Main definitions: `_coordinate`, `_side_key`, `_jsonable`, `ReplayState`, `build_state`, `_load_checkpoint`, `report_from_state`, plus supporting helpers. |
| [orders.py](../src/gmx_crypto_bot_v2/reconstruction/orders.py) | Join indexed requests, lifecycle events, receipt transfers and swaps. Main definitions: `_load_replay_evidence`, `_load_terminal_lifecycle`, `_associate_execution_fees`, `_load_order_update_topups`, `_load_execution_receipt_evidence`, `_single_update_topup`, `_iter_raw_event_emitter_logs`. |
| [positions.py](../src/gmx_crypto_bot_v2/reconstruction/positions.py) | Recover position, inventory and pool context immediately before execution. Main definitions: `_attach_pre_position_state`, `_attach_pre_virtual_inventory`, `_attach_pre_impact_pool`. |
| [referral.py](../src/gmx_crypto_bot_v2/reconstruction/referral.py) | Version referral identity and discount settings in canonical event order. Main definitions: `History`, `ReferralState`. |
| [swaps.py](../src/gmx_crypto_bot_v2/reconstruction/swaps.py) | Reconstruct swap configuration and pool state for each route hop. Main definitions: `coordinate`, `SwapReplay`. |

## models — Economic calculations

Implements deterministic integer arithmetic and protocol formulas. Inputs may include observed values; independence is established by the surrounding checks.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/models/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [accrual.py](../src/gmx_crypto_bot_v2/models/accrual.py) | Pure fixed-precision funding, borrowing and liquidation-fee formulas. Main definitions: `ceil_div`, `borrowing_rate`, `funding_rate`, `funding_deltas`, `liquidation_fee`. |
| [arithmetic.py](../src/gmx_crypto_bot_v2/models/arithmetic.py) | Signed division and rounding rules shared by economic models. Main definitions: `_ceil_div`, `_proportional_pending_impact`, `_trunc_div`. |
| [decrease.py](../src/gmx_crypto_bot_v2/models/decrease.py) | Pure integer model of decrease-position collateral settlement. Main definitions: `_model_decrease_settlement`. |
| [execution.py](../src/gmx_crypto_bot_v2/models/execution.py) | Pure execution-gas settings and committed transfer matching. Main definitions: `GasSettings`, `settings_from_calls`, `transfer_candidates`. |
| [impact.py](../src/gmx_crypto_bot_v2/models/impact.py) | Pure GMX position-impact curve and cap inputs. Main definitions: `balance_impact`, `apply_exponent_factor`, `predict_current_impact`. |
| [prb.py](../src/gmx_crypto_bot_v2/models/prb.py) | Integer PRBMath exponentiation used by protocol calculations. Main definitions: `pow_ud60x18`, `apply_exponent_factor`. |
| [referral.py](../src/gmx_crypto_bot_v2/models/referral.py) | Pure referral and pro discount arithmetic with protocol rounding. Main definitions: `discount_amounts`. |
| [settlement.py](../src/gmx_crypto_bot_v2/models/settlement.py) | Pure liquidation cash flow and independently reconstructed fee inputs. Main definitions: `coordinate`, `Cash`, `settle`, `reconstruct_fees`. |
| [swaps.py](../src/gmx_crypto_bot_v2/models/swaps.py) | Pure swap fee, impact, pool and output arithmetic. Main definitions: `curve`, `price_swap`. |

## checks — Evidence comparisons

Compares modeled or reconstructed expectations with observed executions and emits check outcomes and discrepancies.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/checks/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [accrual.py](../src/gmx_crypto_bot_v2/checks/accrual.py) | Funding, borrowing and liquidation fee-configuration comparisons. Main definitions: `compare_accrual`, `compare_liquidation_configuration`, `compare_erased_liquidation`. |
| [common.py](../src/gmx_crypto_bot_v2/checks/common.py) | Shared check outcome aggregation. Main definitions: `_comparison`. |
| [decrease.py](../src/gmx_crypto_bot_v2/checks/decrease.py) | Compare collateral conversion and decrease settlement outcomes. Main definitions: `_compare_collateral_conversion`, `_compare_decrease_settlement`. |
| [execution.py](../src/gmx_crypto_bot_v2/checks/execution.py) | Join terminal orders to independently verified execution-fee payments. Main definitions: `apply_execution_fee_proof`. |
| [fees.py](../src/gmx_crypto_bot_v2/checks/fees.py) | Compare recorded position fees with historical configuration. Main definitions: `compare_historical_fees`. |
| [liquidation.py](../src/gmx_crypto_bot_v2/checks/liquidation.py) | Compare independent liquidation settlement with recorded outcomes. Main definitions: `compare_liquidation_settlement`. |
| [orders.py](../src/gmx_crypto_bot_v2/checks/orders.py) | Dependency-ordered checks for one order and its supplied evidence context. Main definitions: `validate_order`, `_reconstruct_terminal_reason`, `_position_identity`, `_request_position_identity`. |
| [payouts.py](../src/gmx_crypto_bot_v2/checks/payouts.py) | Verify ERC-20, native and multichain payout observations. Main definitions: `verify_native`, `verify_payouts`. |
| [position.py](../src/gmx_crypto_bot_v2/checks/position.py) | Position size, execution price, oracle and independent impact comparisons. Main definitions: `_compare_independent_price_impact`, `_compare_execution`, `_compare_position_math`, `_compare_execution_price`. |
| [position_fees.py](../src/gmx_crypto_bot_v2/checks/position_fees.py) | Position fee arithmetic and request-to-terminal execution-fee balance checks. Main definitions: `_compare_execution_fee_events`, `_compare_fee_math`. |
| [referral.py](../src/gmx_crypto_bot_v2/checks/referral.py) | Compare independently reconstructed referral and pro discounts. Main definitions: `compare_referral`. |
| [swaps.py](../src/gmx_crypto_bot_v2/checks/swaps.py) | Compare per-hop swap models with recorded executions. Main definitions: `compare_swaps`. |
| [trace.py](../src/gmx_crypto_bot_v2/checks/trace.py) | Offline gas and transfer proofs from supplied transaction traces. Main definitions: `_word_address`, `_opcode_positions`, `_receipt_transfers`, `_events`, `_fee_events`, `_trace_fee_events`, `_transfer_proof`, plus supporting helpers. |

## reporting — Report records

Defines the validation report and per-order report serialization shape.

| File | Purpose and important contents |
|---|---|
| [__init__.py](../src/gmx_crypto_bot_v2/reporting/__init__.py) | Marks this directory as a Python package; contains no workflow implementation. |
| [orders.py](../src/gmx_crypto_bot_v2/reporting/orders.py) | Compatible validation report records and per-order serialization. Main definitions: `ValidationReport`, `_order_result`. |


## Recommended reading order

1. `domain/evidence.py` and `reconstruction/context.py` for workflow inputs and identities.
2. `application/collection.py` and `collection/coordinator.py` for acquisition/resume.
3. `evidence/repository.py`, `catalog.py`, `trace_store.py`, and `sources/request_store.py` for ownership and storage.
4. `application/validation.py` and `checks/orders.py` for validation control flow.
5. A vertical slice such as `collection/fees.py → reconstruction/fees.py → checks/fees.py`.
6. `application/replay.py` and `reconstruction/observed.py` for replay.
7. Root CLI modules only when tracing a `python -m` command.

Inventory coverage: **90 Python files**.
