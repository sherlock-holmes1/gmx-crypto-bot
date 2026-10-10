# Stage 3c independent execution decision gate

The CLI can join the selected order's pinned [archive sidecar](../../evidence/step4-1/long-increase-archive-sidecar.json), [five-contract source proof](../../evidence/step4-1/sourcify-source-proof.json), [source manifest](../../evidence/step4-1/historical-source-manifest.json), [pinned increase executor](../../evidence/step4-1/pinned-increase-executor.json) and [source](../../evidence/step4-1/sourcify-v2/increase_executor.json), [pinned SwapHandler](../../evidence/step4-1/pinned-swap-handler.json) and [source](../../evidence/step4-1/sourcify-v2/swap_handler.json), and one recorded oracle evidence file. The adapter checks the digest links and compares the order, pin, request, feature flag, and exact oracle inputs with the router preflight result. It reports `gmx_preflight_only` with a specific reason when any identity or source check fails.

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

The selected order executed at transaction index 1 of block 507206358. An earlier transaction is in that block. The filtered recording contains no relevant logs for it, but the recording cannot prove that it did not change GMX storage. A block-boundary `eth_call` at 507206358 is therefore not equivalent to the state immediately before the observed execution. The adapter rejects an observed-execution comparison from the creation-block sidecar with `execution_oracle_and_creation_pin_not_equivalent`. A creation-block router call with creation-transaction prices is a **counterfactual same-order preflight**; the current missing rule inventory keeps it `gmx_preflight_only` as well.

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
--funding-selector evidence/step4-1/pinned-funding-selector.json
```

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

The observed execution is still at transaction index 1 of a later block. No intra-block prestate proof has been established, so the report remains `gmx_preflight_only` even if additional rule inputs are captured.
