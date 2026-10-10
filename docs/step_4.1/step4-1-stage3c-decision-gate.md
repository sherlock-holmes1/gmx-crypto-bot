# Stage 3c independent execution decision gate

The CLI can join the selected order's pinned [archive sidecar](../../evidence/step4-1/long-increase-archive-sidecar.json), [five-contract source proof](../../evidence/step4-1/sourcify-source-proof.json), [source manifest](../../evidence/step4-1/historical-source-manifest.json), [pinned increase executor](../../evidence/step4-1/pinned-increase-executor.json) and [source](../../evidence/step4-1/sourcify-v2/increase_executor.json), [pinned SwapHandler](../../evidence/step4-1/pinned-swap-handler.json) and [source](../../evidence/step4-1/sourcify-v2/swap_handler.json), one recorded oracle evidence file, and — for the observed execution — the [intra-block prestate proof](../../evidence/step4-1/pinned-system-transaction-prestate.json). The adapter checks the digest links and compares the order, pin, request, feature flag, and exact oracle inputs with the router preflight result. It reports `gmx_preflight_only` with a specific reason when any identity or source check fails.

The historical `OrderHandler`, `ExecuteOrderUtils`, `IncreaseOrderUtils`, and `IncreasePositionUtils` sources show these MarketIncrease decision rules. `decision_adapter.MARKET_INCREASE_RULES` is the coded inventory. Its current state is:

| Rule group | Current proof |
|---|---|
| Pending latest request, order fields, execute-order feature | Pinned sidecar and router preflight; checked for identity. |
| Oracle source, integer price ranges, timestamps, age | Stage 3b artifacts; checked against router call. |
| Valid-from and request expiration | Historical IncreaseOrderUtils and Order sources show that MarketIncrease checks minimum oracle time against order update, exempts market orders from the separate `validFromTime` check, and checks maximum oracle time against update plus the pinned `REQUEST_EXPIRATION_TIME = 300`. The adapter proves and follows that branch. |
| Market and collateral token, swap and minimum output | The selected USDC collateral belongs to the pinned market. Its empty swap path and zero minimum output follow the pinned SwapHandler's no-swap branch, which returns the input 20,000,000 USDC. The pinned DataStore transcript proves `IS_MARKET_DISABLED = false` for this market. |
| Execution and acceptable price | A pinned [balance-input transcript](../../evidence/step4-1/pinned-balance-inputs.json) proves the token-OI impact branch. For this selected long, the increase stays on the short-heavy side and improves balance, so impact is nonnegative and execution price is at most 2,630,415,700,000,000, below the acceptable 2,641,514,664,515,446. This proves only an acceptable-price upper bound; exact impact and execution price remain unavailable. |
| Fees, collateral sufficiency, reserves, minimums, position validity | A separate pinned [risk-cell transcript](../../evidence/step4-1/pinned-risk-cells.json) supplies minimum position size, maximum long open interest, both long reserve factors, and the USD open interest cells. The adapter checks resulting size, post-increase USD open interest, and both token-denominated reserve limits. The selected order's UI fee is zero and gross position fee before discounts is 79,679 USDC base units. Pinned [fee clocks](../../evidence/step4-1/pinned-fee-clocks.json) prove a 51-second update interval. The pinned [smaller-side borrowing switch](../../evidence/step4-1/pinned-borrowing-skip.json) is true; long token OI is below short token OI and the position borrowing factor equals the cumulative factor, proving zero long borrowing fee. The pinned [funding selector](../../evidence/step4-1/pinned-funding-selector.json) is nonzero, so the historical adaptive funding branch applies. Funding, net fee-adjusted collateral, minimum collateral, and full position validity remain unavailable. |
| Market token balances, gas and keeper checks | Execution context absent. |

## Intra-block prestate gate

The selected order executed at transaction index 1 of block 507206358. One earlier transaction is in that block. The filtered recording contains no relevant logs for it, but a recording can never prove that a transaction did not change GMX storage, so that absence is not evidence.

`crosscheck/block_prestate.py` now settles the question from raw chain data instead. For every transaction ordered before the observed execution it reads the transaction and its receipt from block bodies and receipts — **not** historical state, so no archive provider is required — and requires all of:

| Field | Required value |
|---|---|
| `type` | `0x6a`, the Arbitrum internal transaction type |
| `from`, `to` | both `0x00000000000000000000000000000000000a4b05`, the ArbOS internal account |
| input selector | `0x6bf6a42d`, the ArbOS start-block call |
| `gas`, `gasPrice`, `value` | zero |
| receipt `status` | `0x1` |
| receipt `gasUsed` | zero |
| receipt `logs` | empty |
| receipt `contractAddress` | null |
| receipt `to`, `type` | ArbOS internal account, `0x6a` |
| receipt block hash and coordinate | equal to the pinned execution block hash and index |

The capture also reads the boundary block `B - 1` itself. It derives that block's hash from the verified execution header's `parentHash`, requires the `B - 1` header to carry exactly that hash, and requires `timestamp(B - 1)` to equal `timestamp(B)`. GMX's MarketIncrease path reads `block.timestamp` for the oracle max-age gate and for borrowing and funding elapsed seconds, so a boundary whose timestamp advances is refused outright with `block_timestamp_advances_across_pin_boundary`. The `l1BlockNumber` of both blocks is recorded, because the proved start-block transaction exists precisely to advance it.

The execution block hash is read before and after the capture and must equal the recorded execution block hash both times; a change is classified `reorg_detected`. Any missing transaction, missing receipt, provider error, or unexpected field fails the capture closed with an exact machine-readable reason, never with an inferred equivalence.

The capture is saved as a `GmxStep41SystemTransactionPrestate` version 1 transcript at [pinned-system-transaction-prestate.json](../../evidence/step4-1/pinned-system-transaction-prestate.json). It holds the chain id, redacted endpoint identity, execution block number, hash and timestamp, observed transaction index and hash, `equivalent_pin_block` with its derived hash and timestamp, both `l1BlockNumber` values, the per-transaction records, the raw RPC request/response transcript and its digest, and the boolean verdict with its reason. `verify_system_transaction_prestate` rebuilds a positive verdict — including the boundary hash, the timestamp equality and the verdict's own reason string — from the raw transcript alone before the adapter may use it.

For the selected long increase the real capture passes: the single preceding transaction at index 0 is the ArbOS start-block transaction `0xce4cf32d…9ccf`, so `equivalent_pin_block` is **507206357** with hash **0x6624873b…4af5** (the execution block's `parentHash`), and both blocks carry timestamp 1789932725. Their `l1BlockNumber` values differ: 26020834 at 507206357, 26020836 at 507206358.

**What this proves.** No **non-ArbOS contract storage** can have changed between the end of block 507206357 and the observed execution: the only transactions ordered before it are ArbOS internal start-block transactions that consumed zero gas, emitted no logs, created no contract, and targeted the ArbOS internal account rather than any GMX contract. The boundary block is identified by hash, and it carries the same `block.timestamp` as the execution block.

**What it does not prove.** It does not prove that **ArbOS internal state** is equal across the boundary — the proved transaction advances ArbOS's L1 view, and `l1BlockNumber` demonstrably differs. It does not prove that the rest of the EVM **block context** is equal: a call pinned at 507206357 observes that block's `block.number` and `blockhash`, and only `block.timestamp` equality is proved. It does not prove that an archive provider will serve state at 507206357, or that the chain has not since reorganised past it. It does not prove that any GMX state cell was read at that pin — every pinned DataStore transcript remains its own separate gate. It says nothing about transactions ordered after the observed execution, nor about whether the observed execution succeeded. And it does not by itself bind the block to this order; that binding comes from the recorded oracle evidence's coordinates, which the adapter checks against the proof's block, hash and transaction index.

### Exact reasons the adapter can now emit

| Condition | Reason |
|---|---|
| Observed-execution prices, no proof supplied | `execution_oracle_and_creation_pin_not_equivalent` |
| Proof supplied but its verdict is false | `observed_execution_prestate_proof_not_verified`, with the capture's own reason in `prestate_proof_reason` |
| Proof passes, but the sidecar's pin block number is elsewhere (today: the creation block 507206345, not 507206357) | `sidecar_pin_is_not_pre_execution_pin`, with `prestate_equivalent_pin_block` and `sidecar_pin_block` |
| Proof passes and the pin numbers agree, but the sidecar's pin hash is not the derived boundary hash | `sidecar_pin_hash_is_not_pre_execution_block_hash`, with `prestate_equivalent_pin_block_hash` and `sidecar_pin_block_hash` |
| Proof passes and the sidecar is pinned at `equivalent_pin_block` by number **and** hash | the pre-execution equivalence gate is satisfied, `observed_execution_prestate_equivalent_to_pre_execution_pin` joins `proved_branches`, and the decision continues through the remaining rules |
| Every coded rule proved, but prices or state are not the observed pre-execution pair | `independent_decision_state_equivalence_unproved` |

The two sides of a full comparison are deliberately **asymmetric**, and both disclose `missing_rule_evidence`, `proved_rule_count` and `unproved_rule_count`:

- **Accept-side `agreement` or `disagreement`** needs the complete inventory. Our model only accepts the order if every coded rule passed, so the claim is only as good as the inventory: it is emitted when the prestate proof passes, the sidecar is pinned at `equivalent_pin_block` by number and hash, the oracle evidence carries the observed execution's prices, and `missing_rule_evidence` is empty. While any coded rule lacks evidence the report stays `gmx_preflight_only` with `independent_market_increase_rule_coverage_incomplete`.
- **Reject-side `disagreement`** needs the one proved rule plus equivalent state, not the whole inventory. If GMX passed its entire preflight at that same equivalent state, it also passed the rule we reject, so the rules we cannot evaluate cannot explain the conflict. Against a GMX *validation error* it stays `gmx_preflight_only`, because the two rejections are not proved to be the same rule.

The proof can never on its own produce an agreement. With today's evidence the selected order reports `sidecar_pin_is_not_pre_execution_pin`: the pinned archive captures were all taken at the creation block.

A creation-block router call with creation-transaction prices remains a **counterfactual same-order preflight**; the missing rule inventory keeps it `gmx_preflight_only` as well.

To use the gate, add these arguments to `gmx-router-check` along with `--recording`, `--deployment`, `--output`, and `--oracle-evidence`:

```bash
--archive-sidecar evidence/step4-1/long-increase-archive-sidecar.json \
--source-proof evidence/step4-1/sourcify-source-proof.json \
--source-manifest evidence/step4-1/historical-source-manifest.json \
--increase-executor-proof evidence/step4-1/pinned-increase-executor.json \
--increase-executor-source evidence/step4-1/sourcify-v2/increase_executor.json \
--swap-handler-proof evidence/step4-1/pinned-swap-handler.json \
--swap-handler-source evidence/step4-1/sourcify-v2/swap_handler.json \
--decision-config evidence/step4-1/pinned-decision-config.json \
--risk-cells evidence/step4-1/pinned-risk-cells.json \
--balance-inputs evidence/step4-1/pinned-balance-inputs.json \
--fee-clocks evidence/step4-1/pinned-fee-clocks.json \
--borrowing-skip evidence/step4-1/pinned-borrowing-skip.json \
--funding-selector evidence/step4-1/pinned-funding-selector.json \
--prestate-proof evidence/step4-1/pinned-system-transaction-prestate.json
```

Use `--capture-prestate-proof <path>` instead of `--prestate-proof <path>` to capture the transcript first and then run the gate with it. The capture reads block bodies, receipts and two block headers only — never historical state — so it uses `GMX_LOGS_RPC_URL` or the public `https://arb1.arbitrum.io/rpc` endpoint and never needs `GMX_ARCHIVE_RPC_URL`. It requires `--deployment` for the chain id and the observed-execution oracle evidence for the block, hash and transaction index; the two prestate arguments are mutually exclusive. The report gains a `prestate_proof` block with the coordinate, `equivalent_pin_block`, verdict, reason, and the file digest.

### Pinning the preflight at the proved boundary

Candidates are pinned at their **creation** block by `router_candidates.py`, and `evaluate` requires `preflight["pin_block"] == sidecar pin`. For an order created in one block and executed in a later one, that default can never reach the equivalence branch. `--pin-at-prestate-boundary` supplies the missing capability. It is not a free-form pin: it refuses unless a *verified* prestate proof exists, and it moves the pin only to that proof's `equivalent_pin_block` and derived hash. It additionally requires that exactly one candidate matches the oracle evidence's order key, that the proof's observed execution is that candidate's recorded terminal transaction (block number and transaction hash), and that the boundary lies inside the order's pending interval (`creation ≤ boundary < execution`). The preflight then re-reads that block hash on chain and rejects a mismatch, and `report["run"]["pin_mode"]` records which pin was used.

The sidecar and every pinned DataStore transcript must agree on the same boundary. Until they are recaptured there, the preflight pin and the sidecar pin differ and the adapter reports `selected_order_or_pin_mismatch` — the earlier, more general identity gate — before it reaches the prestate reasons. That recapture is Stage 3d work, not Stage 3c.

The report's main `comparison` remains independent from the separate narrow `rule_comparison` acceptable-price diagnostic. Neither confirms fees, PnL, or a complete lifecycle.

### Remaining selected-order inputs

The pinned executor's `IncreaseOrderUtils` delegates to `IncreasePositionUtils`. The adapter now reads the exact nine-cell [risk transcript](../../evidence/step4-1/pinned-risk-cells.json) at the sidecar block and independently recomputes each key and ABI value. Its reserve check uses the saved WETH pool, both long open-interest-in-tokens cells, the long base token delta (`sizeDeltaUsd / indexPrice.max`), and the two new factors. The USDC collateral path changes the USDC fee pool, so this check does not use a hypothetical WETH pool adjustment. The new USD open-interest cells and maximum long open interest prove the preceding `validateOpenInterest` check. These passed branches are included in `proved_branches`; they do not imply that intervening economics succeeded.

| Unproved rule | Actual-branch evidence still needed |
|---|---|
| Exact execution price | Complete `PositionPricingUtils.getPriceImpactUsd` magnitude with historical impact factor/exponent rounding and the positive cap. The selected order's acceptable-price condition is bounded independently, but the exact token delta and price remain unproved. The separate narrow acceptable-price diagnostic is not this decision. |
| Net fees and collateral | Adaptive funding's saved rate, stable/decrease thresholds, minimum increase rate, maximum funding rate, funding exponent, and updated per-size funding amounts over the proved 51-second interval; account-specific referral discounts; and the post-fee collateral amount. Gross position fee, zero UI fee, and zero long borrowing fee are proved, but cannot decide the minimum collateral gate without all costs. |
| Full position validity | Post-update token size and pending impact, full-close PnL and impact, max-PnL and liquidation impact factors, and fee terms used by `PositionUtils.isPositionLiquidatable`. The minimum size check alone does not prove this rule. |
| Market token balances | Actual WETH and USDC `balanceOf(marketToken)` plus all expected-min-balance DataStore components, collateral sums, and claimable funding for both tokens after simulated updates. |
| Gas and keeper | Historical `OrderHandler` execution context, including `startingGas`, `tx.gasprice`, gas-limit configuration, keeper role and error-handling gas. A block-boundary preflight does not expose an equivalent observed transaction context. |

The observed execution is still at transaction index 1 of block 507206358. The intra-block prestate proof is now established and reproducible: for non-ArbOS contract storage, the pre-execution state equals the block 507206357 boundary, which is identified by hash and carries the same block timestamp. The remaining blockers are different ones. The pinned sidecar and every pinned DataStore transcript were captured at the creation block 507206345, so the adapter reports `sidecar_pin_is_not_pre_execution_pin`. Recapturing them at 507206357 — together with a preflight run with `--pin-at-prestate-boundary` — is Stage 3d work. Even after that, the five unproved rules above keep the report `gmx_preflight_only`.
