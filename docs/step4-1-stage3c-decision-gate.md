# Stage 3c independent execution decision gate

The CLI can join the selected order's pinned [archive sidecar](../evidence/step4-1/long-increase-archive-sidecar.json), [five-contract source proof](../evidence/step4-1/sourcify-source-proof.json), [source manifest](../evidence/step4-1/historical-source-manifest.json), [pinned increase executor](../evidence/step4-1/pinned-increase-executor.json) and [source](../evidence/step4-1/sourcify-v2/increase_executor.json), [pinned SwapHandler](../evidence/step4-1/pinned-swap-handler.json) and [source](../evidence/step4-1/sourcify-v2/swap_handler.json), and one recorded oracle evidence file. The adapter checks the digest links and compares the order, pin, request, feature flag, and exact oracle inputs with the router preflight result. It reports `gmx_preflight_only` with a specific reason when any identity or source check fails.

The historical `OrderHandler`, `ExecuteOrderUtils`, `IncreaseOrderUtils`, and `IncreasePositionUtils` sources show these MarketIncrease decision rules. `decision_adapter.MARKET_INCREASE_RULES` is the coded inventory. Its current state is:

| Rule group | Current proof |
|---|---|
| Pending latest request, order fields, execute-order feature | Pinned sidecar and router preflight; checked for identity. |
| Oracle source, integer price ranges, timestamps, age | Stage 3b artifacts; checked against router call. |
| Valid-from and request expiration | Historical source known; pinned `REQUEST_EXPIRATION_TIME` not in current sidecar. |
| Market and collateral token, swap and minimum output | The selected USDC collateral belongs to the pinned market. Its empty swap path and zero minimum output follow the pinned SwapHandler's no-swap branch, which returns the input 20,000,000 USDC. Market-enabled state remains unproved. |
| Execution and acceptable price | Narrow economics diagnostic exists, but no complete point reader and final token delta is proved. |
| Fees, collateral sufficiency, reserves, minimums, position validity | Complete independent rule inputs and implementation absent. |
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
--swap-handler-source evidence/step4-1/sourcify-v2/swap_handler.json
```

The report's main `comparison` remains independent from the separate narrow `rule_comparison` acceptable-price diagnostic. Neither confirms fees, PnL, or a complete lifecycle.
