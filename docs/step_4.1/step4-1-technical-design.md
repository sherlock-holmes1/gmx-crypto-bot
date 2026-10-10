# Step 4.1 technical design: router preflight and same-order evidence

Status: Stage 0 design proposal. No router call or same-order comparison has been verified.

This design implements the [Step 4.1 plan](step4-1-simulation-router-crosscheck-plan.md). The router checks one pending order against supplied prices. Our reconstruction checks the same order only when its inputs and pre-execution state can be proved equivalent. A router result alone is **GMX preflight only**.

## Evidence path and order selection

1. Stream `events.jsonl` from the September recording. Select `gmx_market_log` rows whose `payload.event_name` is `OrderCreated`; these rows contain a raw `payload.log`, **not decoded request fields**. Decode each raw log with the canonical `domain/entries.py` `_decode_recorded_log`, then normalize it with `_event_entry` (or expose equivalent public helpers). Read `key`, `market`, `orderType`, `isLong`, and other creation fields from the normalized entry's `values`. Reject a failed decode or missing key, and retain `(block_number, transaction_index, log_index)`, transaction hash, block hash, and recording identity. Decode `OrderUpdated`, `OrderExecuted`, `OrderCancelled`, and `OrderFrozen` through the same path and join them by key. Do not infer a sequential order number.
2. Reconstruct the effective request from the decoded creation values, any opening checkpoint when the request predates the window, and ordered `OrderUpdated` events up to the pinned block. Use the existing `reconstruction/orders.py` and `reconstruction/observed.py` ordering and normalization patterns. Then read the order at the pinned block from GMX and compare every field used by the router/reconstruction; the pinned on-chain value wins if a recorded event stream is incomplete. Classify only increase and decrease position orders, each with `isLong=true` or `false`. Keep the GMX `orderType` in the record and map it explicitly to the supported action. An unknown type is skipped, not guessed.
3. Consider a **post-creation block boundary** and, where useful, the block immediately before a recorded execution. The chosen block must be at or after the creation block and before terminal execution or cancellation. `OrderFrozen` is a state transition, not a terminal event; preserve its state and decide router eligibility from the pinned on-chain order. The chosen block's full hash is part of the candidate identity. A request created and executed in one block has no archive block boundary while it is pending; skip it for this router helper.
4. Historical eligibility uses archive state at the pinned block, not present-day state. Read the DataStore `NONCE` and derive `keccak256(abi.encode(dataStoreAddress, nonce))` as GMX's current global request key. Require exact equality with the recorded key. Independently read the pending order from the order store/reader at that block and compare the key and request fields. A later deposit, withdrawal, or order can advance the global nonce, even if the selected order remains pending. Such an order is ineligible for `simulateExecuteLatestOrder`.
5. If the September recording yields no suitable candidates, inspect a new, separate one-day recording. Use a bounded public-event watcher only after that. Apply identical gates in every source and record every skip reason. Never mutate a recording.

The current recording contains `OrderCreated` entries and an opening checkpoint with active orders. Its metadata names the DataStore and EventEmitter, but **does not name a SimulationRouter**. The implementation must load a verified chain-specific router address and contract ABI from an explicit configuration source. It must reject a missing address or unexpected chain ID; it must not guess a router address from the order handler.

## GMX call contract

GMX documents `simulateExecuteLatestOrder(SimulatePricesParams)`, where `SimulatePricesParams` contains `address[] primaryTokens`, parallel `Price.Props[] primaryPrices` with `uint256 min` and `max`, and `uint256 minTimestamp` and `maxTimestamp`. Encode the tuple with the verified contract ABI and test its selector and calldata against an independent ABI encoder or known vector. Use `eth_call` with a fixed `to` router address and explicit block identifier. No wallet, private key, signature, `eth_sendTransaction`, or transaction submission is needed.

Build prices for **every token required by the order**, including WETH and USDC for the ETH/USD market and any additional swap-path token. Preserve the exact integer price units accepted by GMX's router. The recorded `OraclePriceUpdate` values are transaction-scoped observations; prove their units and provenance before using them as router inputs. A decimal display price is never silently substituted. Record token order, token address, min/max, timestamp interval, source transaction or oracle endpoint, and any conversion. Require positive prices, `min <= max`, and a valid interval. Do not assume an execution transaction's oracle values existed when an earlier historical block was mined: when they are supplied hypothetically, label them **counterfactual prices**.

GMX's simulation call always reverts. Decode `EndOfOracleSimulation` as **passed preflight**. Decode a recognized other custom error as **GMX validation error**, preserving its selector and arguments. An unknown revert remains **unknown contract error**. RPC timeouts, absent archive state, malformed RPC errors, and missing revert data are **provider or evidence failures**, never validation errors. Save the raw revert payload. Check the block hash before and after the call; if it changes, discard the result as reorg affected.

Use a pinned block number or EIP-1898 block-hash selector if the provider supports it. Do not assume all providers support hash selectors; re-read the block hash around a number-pinned call. Verify the deployed router code at that block and use an ABI compatible with that deployment. A current ABI or address is not proof of a historical deployment.

## State-equivalence gate for our reconstruction

The existing `RecordingEvidence.at(coordinate)` in `simulation/evidence.py` reconstructs state **before a log**, including only oracle updates from that same transaction. `state_for_opportunity()` finds the position log before an `OrderExecuted` event. A router `eth_call` instead observes state at a **whole block boundary**. These are different coordinates. A same-order comparison requires an explicit adapter and evidence manifest that proves:

- The order key, versioned request fields, type, side, market, collateral token, size, acceptable and trigger prices, and pending/frozen status match the on-chain order at the pinned block.
- The oracle token set, integer min/max prices, and timestamp interval used by our reconstructed decision match the router calldata. The evidence identifies observed versus counterfactual prices.
- Every state cell relevant to the compared validation rule is equal at the block boundary and the reconstruction coordinate: position and collateral state, market and risk configuration, liquidity/open interest, fees or impact state when the rule uses them, and order/feature flags. Prove equality from pinned archive reads or from complete event continuity; do not infer it merely because no relevant log was captured.
- The reconstruction runs the **recorded request** through a named observed-order adapter or equivalent deterministic case. The existing hypothetical scenario scheduler is not itself a same-order adapter. Its output must not be labeled an agreement until the exact rule and inputs compared are stated.

If a later transaction in the pinned block changes any relevant state before the observed keeper execution, block-boundary state may differ from actual pre-execution state. If the required state cannot be proved equal, or the order is executed in its creation block, emit **GMX preflight only**. A router pass may be compared to our eligibility decision, not to fee, PnL, risk, or full lifecycle values. If our implementation lacks the particular GMX validation rule, label that rule **unsupported by reconstruction** instead of interpreting a mismatch as an implementation defect.

## Report contract

Use a versioned JSON report with a run section (`schema_version`, recording path and hash, chain ID, spec hash, router address and code hash, ABI identity, UTC run time, RPC capability summary) and deterministic candidate records sorted by creation coordinate. A candidate record contains:

| Field group | Required contents |
|---|---|
| Identity | Source mode, order key, request type/side, market, creation coordinate and transaction hash, terminal coordinate if known |
| Pin and gates | Block number/hash, order-store result, nonce, derived latest key, pending/latest booleans, each failed gate and skip reason |
| Oracle input | Ordered tokens, integer min/max values, timestamp interval, provenance, observed/counterfactual label, scale proof |
| Router call | Router and code hash, calldata or its hash plus reproducible full input, raw revert, decoded selector/name/arguments, provider error if any |
| Reconstruction | Adapter/version, compared rule, result or unavailable reason, state-equivalence manifest with cell sources and equality checks |
| Outcome | `passed_preflight`, `validation_error`, `unknown_contract_error`, `provider_failure`, `evidence_failure`, or `ineligible_latest_request`; comparison `agreement`, `disagreement`, or `gmx_preflight_only` |

Preserve historical, one-day, and live-watch source modes separately. Summarize the four target categories and counts by outcome. Never count `gmx_preflight_only` toward the minimum same-order comparison gate. A failure to find a suitable order is an evidence gap, not a pass.

## Feasibility checks before Stage 1 coding

1. Verify the chain ID, router address, deployed historical code, DataStore ABI, order-reader ABI, and custom-error ABI against the actual deployment. Source code on `main` can change after the recorded blocks.
2. Count the September `OrderCreated` events by category and locate block-boundary intervals in which each was pending and still the global latest request. The recording's market filter is insufficient to prove a **global** nonce: use archive DataStore reads. Avoid claiming that a market-scoped log stream contains every global request.
3. Test whether the archive provider supports the required historical block reads and returns revert data for router `eth_call`. The project has no configured `GMX_ARCHIVE_RPC_URL` in the planning environment; runtime evidence depends on a supplied archive endpoint.
4. Establish the exact price scale and timestamp rules for the deployed oracle/router. The current evidence adapter extracts same-transaction oracle ranges, but it does not by itself supply historical router calldata or prove timestamp equivalence.
5. Identify one candidate for which the block-boundary state and our reconstructed pre-execution state are demonstrably equal. If none exists, report the specific reason and continue to the one-day source or bounded watcher. Do not relax the equivalence gate to satisfy a completion count.

The official behavior and parameter shape are documented in [GMX simulations](https://docs.gmx.io/docs/api/contracts/simulations/). The global key derivation is in [GMX `NonceUtils`](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/nonce/NonceUtils.sol). Implementation must bind these facts to the historical deployment before making a verified claim.

## Stage 3 implementation dependency

The recorded-order validator in `application/validation.py` checks observed terminal
events. The scenario scheduler in `simulation/orders.py` accepts hypothetical
requests and later keeper opportunities. Neither can evaluate the full GMX
execution decision for a recorded order at the router's pinned block. The
router report therefore labels all current results `gmx_preflight_only`.

A same-order adapter needs a pinned archive reader for position, collateral,
market, risk configuration, liquidity, open interest, fee and impact state,
feature flags, and any swap-path state used by that order. It must bind each
cell to the block hash, obtain the exact order request and integer oracle
inputs, and pass those values to a model that implements the same validation
rule. An observed `OrderExecuted` or `OrderCancelled` event is not that model
decision. A caller-provided manifest that merely says its cells match cannot
establish equality. The adapter must derive and check its required cells in
code before the report may claim agreement or disagreement. No such complete
adapter or verified archive/oracle input set exists in this repository yet.

The bounded watcher now reads public EventEmitter logs and keeps only
`OrderCreated` events for the recording's market. It rejects logs outside the
requested block range and rejects an order that has an `OrderExecuted` or
`OrderCancelled` event in its creation block. It does not obtain an independent
GMX oracle feed. A watched order without a caller-supplied, verified integer
oracle input is an evidence failure; it cannot produce a router call or a
comparison. A future oracle collector must save token addresses, integer
min/max values, time interval, endpoint or transaction source, and scale proof.

The `EconomicsAcceptablePriceAdapter` now computes one narrow rule from a
typed `PinnedEconomicPoint` using `simulation/economics.py`. Its reader
interface must supply pinned archive state. The adapter checks order key,
block hash, request, oracle ranges and timestamps, order type and action,
and asks its reader to verify every required state group against independent
pinned reads before it calculates. Source strings alone do not pass this gate.
Its `rule_agreement` and `rule_disagreement` results concern the
acceptable-price rule only; they do not meet the full execution comparison
gate. The CLI has no concrete archive point reader, so it cannot emit a live rule
comparison. That reader needs pinned DataStore values for all required state
groups and verified oracle inputs for the same timestamp interval. The mock
reader used in tests demonstrates the calculation path, not live evidence.
