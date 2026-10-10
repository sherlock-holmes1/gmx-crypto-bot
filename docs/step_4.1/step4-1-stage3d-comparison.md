# Stage 3d reproducible contract comparison

**Result: Step 4.1's minimum completion gate is NOT met.** The September
recording produced a reproducible, boundary-pinned, same-order run against the
real GMX deployment, and the blocker Stage 3c recorded
(`sidecar_pin_is_not_pre_execution_pin`) is gone. The run's comparison status is
`gmx_preflight_only` with reason
`independent_market_increase_rule_coverage_incomplete`: GMX passed its whole
preflight, our model accepted every rule it can evaluate, and **six of fifteen**
coded MarketIncrease rules still have no evidence. A `gmx_preflight_only` result
never counts toward the gate. The one-day backup recording and the bounded
watcher were **not** needed and were not run — the September recording supplied
the order, and the missing evidence is rule coverage, not a recording.

## The run

| Item | Value |
|---|---|
| Recording | `recordings/eth-usdc-v2-sep-20-sep-27` (`events.jsonl` SHA-256 `0x75ddbc6e66b71bb1d35874e29449b4d725d93df52f8b75863f11ee4bdbe60ef1`) |
| Order key | `0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90` |
| Category | long `MarketIncrease`, USDC collateral, ETH/USD two-token market |
| Created at | block 507206345, transaction index 2, log index 18 |
| Observed execution | block 507206358, transaction index 1, log index 24, transaction `0xd7c47b71…a212` |
| Pin | **block 507206357**, hash **`0x6624873b47717258e06e038aa89bf86c2bd9b34542b5b3f72b5b04badc8b4af5`** |
| Pin mode | `prestate_proved_pre_execution_boundary` |
| Pin timestamp | 1789932725, equal to the execution block's |
| Pending and latest at the pin | yes; nonce 7727799, derived latest key equals the order key |
| GMX result | `passed_preflight` — raw revert `0x4e48dcda` = `EndOfOracleSimulation()` |
| Our model | accepted every evaluable rule; no rejection |
| Comparison | `gmx_preflight_only`, `independent_market_increase_rule_coverage_incomplete` |
| `comparison_scope` | `observed_execution_pre_execution_block_boundary` |
| `completion_gate_met` | `false` |

Deployment and code identity at the pin. All seven runtime code hashes, and
both OrderHandler pointers, are **identical** to those observed at the creation
block 507206345, so moving the pin did not change the deployment under test:

| Role | Address | Runtime code keccak |
|---|---|---|
| SimulationRouter | `0xaD3051cB1aE3a86b335f12A9a41BD4d995a137ea` | `0x23082b1f…9235a` |
| DataStore | `0xfd70de6b91282d8017aa4e741e9ae325cab992d8` | `0x3e7aea6e…5088e` |
| Reader | `0xfA26cBb46e2614609406de08CA1Dc7f70a684184` | `0x49ed1cb3…ac04a` |
| OrderHandler | `0xa5d2d45228ee2e3a18ab122b2ce84997d008f4eb` | `0xc35bbb93…89452` |
| ReferralStorage | `0xe6fab3f0c7199b0d34d7fbe83394fc0e0d06e99d` | `0x0e60e917…6b4dd` |
| IncreaseOrderExecutor (via `increaseOrderExecutor()`) | `0x8f83a77a8f075466904b7b926dc3c80052a59ff8` | `0xf6e53548…f2af4` |
| SwapHandler (via `swapHandler()`) | `0xff38607a4e1f5f753a317ceb451f7b068df8257b` | `0x4d4e4481…0451f` |

Oracle inputs — the observed execution transaction's own `OraclePriceUpdate`
events, logs 2 and 3 of transaction `0xd7c47b71…a212`, both at timestamp
1789932724 against block timestamp 1789932725:

| Token | min | max |
|---|---|---|
| WETH `0x82af4944…fbab1` | 2631796557018883 | 2631796557018883 |
| USDC `0xaf88d065…e5831` | 999913061406316750000000 | 999913061406316750000000 |

Model inputs that decided a branch: `sizeDeltaUsd` 199181621808000000000000000000000,
`initialCollateralDeltaAmount` 20000000, `acceptablePrice` 2641514664515446,
`minOutputAmount` 0, empty swap path, `updatedAtTime` 1789932722,
`REQUEST_EXPIRATION_TIME` 300, `IS_MARKET_DISABLED` false, prior position
`sizeInUsd` 199199145611028315780000000000000, funding and long borrowing clocks
both 1789932671 (54 elapsed seconds at the pin), adaptive funding branch
selected. Derived diagnostics: execution-price upper bound 2631796557018883
(below the acceptable price), zero UI fee, zero long borrowing fee, gross
position fee before discounts 79679 USDC base units.

## What was recaptured, and how it was verified

Every pinned artifact was recaptured at 507206357 **by tooling**, never written
by hand. The creation-block files are preserved unchanged; the boundary files
are new paths.

| Artifact | Creation-block file (kept) | Boundary file | SHA-256 |
|---|---|---|---|
| Observed deployment | `observed-deployment.json` | `observed-deployment-507206357.json` | `0x1ff97b28…3032e` |
| Archive sidecar | `long-increase-archive-sidecar.json` | `long-increase-archive-sidecar-507206357.json` | `0x1e00f269…6907b` |
| Source manifest | `historical-source-manifest.json` | `historical-source-manifest-507206357.json` | `0x02fc0718…87642` |
| Sourcify proof | `sourcify-source-proof.json` | `sourcify-source-proof-507206357.json` | `0x398bb083…18f0` |
| Increase executor | `pinned-increase-executor.json` | `pinned-507206357-increase-executor.json` | `0xb49774f6…a222` |
| Swap handler | `pinned-swap-handler.json` | `pinned-507206357-swap-handler.json` | `0xcffe8b06…14a7c` |
| Decision config | `pinned-decision-config.json` | `pinned-507206357-decision-config.json` | `0x1ec00df4…c8684` |
| Risk cells | `pinned-risk-cells.json` | `pinned-507206357-risk-cells.json` | `0x6ccd5893…789f1` |
| Balance inputs | `pinned-balance-inputs.json` | `pinned-507206357-balance-inputs.json` | `0x19a193c7…a2d82` |
| Fee clocks | `pinned-fee-clocks.json` | `pinned-507206357-fee-clocks.json` | `0x7077c918…c7b5e` |
| Borrowing skip | `pinned-borrowing-skip.json` | `pinned-507206357-borrowing-skip.json` | `0x64044509…42aa8` |
| Funding selector | `pinned-funding-selector.json` | `pinned-507206357-funding-selector.json` | `0x07903daf…f95980` |

Every boundary record carries `block_number` 507206357 and `block_hash`
`0x6624873b…4af5`, and each capture reads the block header before **and** after
its reads so a changed block fails the capture closed. Every value is identical
to the creation-block capture except `pinned-507206357-fee-clocks.json`'s
`block_timestamp`, which moves from 1789932722 to 1789932725 — the value the
prestate proof requires, because it equals the execution block's timestamp. The
sidecar additionally re-proves pending and latest at the new pin.

### The pin is not caller-chosen

`crosscheck/boundary_pin.py` holds the single repin rule now used by the
preflight, the deployment observation and the sidecar. It refuses unless a
**verified** prestate proof exists, moves the pin only to that proof's
`equivalent_pin_block` and derived hash, requires exactly one candidate matching
the oracle evidence's order key, requires the proof's observed execution to be
that candidate's recorded terminal transaction, and requires
`creation ≤ boundary < execution`. The preflight then re-reads that block hash on
chain before and after the call.

### The capture tool reproduces the Stage 3c evidence

`gmx-pinned-capture` was first run against the **creation-block** sidecar. It
reproduced all eight Stage 3c records with identical keys, values and
non-transcript fields; seven of the eight transcripts were byte-identical, and
`pinned-risk-cells.json` differed only by one extra intermediate block-header
read in the original hand capture. That is the evidence that the tool captures
what Stage 3c captured, rather than something new that merely passes.

## The sidecar trust gap is closed

The Stage 3c reviewer found that the adapter replayed every evidence file's raw
`rpc_transcript` except the sidecar's, while `fixed_values.market` and
`position.value.sizeInUsd` feed the reject-side rules
`market_and_collateral_token_valid`, `minimum_position_and_collateral` and
`max_open_interest`. Stage 3d may emit exactly such a reject-side
`disagreement`, so the gap is closed before any real comparison is emitted.

`crosscheck/sidecar_replay.py` now re-derives, from the sidecar's own saved
calls alone:

- the transcript digest `rpc_transcript_sha256`;
- the router gate — DataStore `NONCE`, `ORDER_LIST` membership, and the derived
  latest key — so `pending`, `latest` and `nonce` are not trusted;
- all 26 pinned order fields, by recomputing each `keccak256(orderKey ‖ Keys
  field)` storage key and decoding the ABI word;
- the Reader `getOrder` result as an independent second read of the same request;
- the position key, recomputed with `keccak256(abi.encode(account, market,
  collateral, isLong))`, and the full decoded position struct;
- the four market address cells, from `keccak256(abi.encode(market, field))`;
- the entire 27-cell fixed inventory — rebuilt from the re-derived order and the
  re-derived market tokens, so a cell cannot be added, removed or renamed — and
  every one of its decoded values;
- the virtual token ID, virtual inventory, and both aggregated open-interest
  sides;
- the execute-order feature flag cell and value.

Each call is located by its exact `to` and calldata at the pin's block tag. A
missing call, a call at another block, two different results for one call, a
non-read-only method, or any changed reported value raises and the adapter
refuses the sidecar outright. The report now carries
`sidecar_transcript_sha256` and
`sidecar_values_rederived_from_raw_transcript: true`.

## Reproducing the run

All chain access is read-only. The router call is an `eth_call` that reverts by
design. `GMX_ARCHIVE_RPC_URL` must point at an Arbitrum mainnet **archive**
endpoint; it is never written into any artifact, and the captures refuse to save
a record containing a URL.

```bash
# 1. Observe the deployment at the proved boundary
PYTHONPATH=src .venv/bin/python -m gmx_crypto_bot_v2.application.deployment_prep \
  --recording recordings/eth-usdc-v2-sep-20-sep-27 \
  --spec gmx-market-spec-v1.json \
  --order-key 0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90 \
  --simulation-router 0xaD3051cB1aE3a86b335f12A9a41BD4d995a137ea \
  --referral-storage 0xe6fab3f0c7199b0d34d7fbe83394fc0e0d06e99d \
  --oracle-evidence evidence/step4-1/selected-order-execution-oracle.json \
  --prestate-proof evidence/step4-1/pinned-system-transaction-prestate.json \
  --pin-at-prestate-boundary \
  --output evidence/step4-1/observed-deployment-507206357.json

# 2. Collect the sidecar at the same boundary
PYTHONPATH=src .venv/bin/python -m gmx_crypto_bot_v2.application.archive_sidecar \
  --recording recordings/eth-usdc-v2-sep-20-sep-27 \
  --order-key 0xfd65a4c31638d78c68c9986e0bb21c002790d0a897aaa31d4109b9cd2f0bff90 \
  --deployment evidence/step4-1/observed-deployment-507206357.json \
  --oracle-evidence evidence/step4-1/selected-order-execution-oracle.json \
  --prestate-proof evidence/step4-1/pinned-system-transaction-prestate.json \
  --pin-at-prestate-boundary \
  --output evidence/step4-1/long-increase-archive-sidecar-507206357.json

# 3. Rebind the historical source evidence to the new sidecar (offline)
PYTHONPATH=src .venv/bin/python -m gmx_crypto_bot_v2.application.source_manifest \
  --sidecar evidence/step4-1/long-increase-archive-sidecar-507206357.json \
  --output evidence/step4-1/historical-source-manifest-507206357.json
PYTHONPATH=src .venv/bin/python -m gmx_crypto_bot_v2.application.sourcify_proof \
  --manifest evidence/step4-1/historical-source-manifest-507206357.json \
  --records evidence/step4-1/sourcify-v2 \
  --output evidence/step4-1/sourcify-source-proof-507206357.json

# 4. Capture the eight pinned decision transcripts at the sidecar's pin
PYTHONPATH=src .venv/bin/python -m gmx_crypto_bot_v2.application.pinned_capture \
  --sidecar evidence/step4-1/long-increase-archive-sidecar-507206357.json \
  --increase-executor-source evidence/step4-1/sourcify-v2/increase_executor.json \
  --swap-handler-source evidence/step4-1/sourcify-v2/swap_handler.json \
  --output-prefix evidence/step4-1/pinned-507206357-

# 5. Run the comparison
PYTHONPATH=src .venv/bin/python -m gmx_crypto_bot_v2.application.router_check \
  --recording recordings/eth-usdc-v2-sep-20-sep-27 \
  --deployment evidence/step4-1/observed-deployment-507206357.json \
  --oracle-evidence evidence/step4-1/selected-order-execution-oracle.json \
  --archive-sidecar evidence/step4-1/long-increase-archive-sidecar-507206357.json \
  --source-proof evidence/step4-1/sourcify-source-proof-507206357.json \
  --source-manifest evidence/step4-1/historical-source-manifest-507206357.json \
  --increase-executor-proof evidence/step4-1/pinned-507206357-increase-executor.json \
  --increase-executor-source evidence/step4-1/sourcify-v2/increase_executor.json \
  --swap-handler-proof evidence/step4-1/pinned-507206357-swap-handler.json \
  --swap-handler-source evidence/step4-1/sourcify-v2/swap_handler.json \
  --decision-config evidence/step4-1/pinned-507206357-decision-config.json \
  --risk-cells evidence/step4-1/pinned-507206357-risk-cells.json \
  --balance-inputs evidence/step4-1/pinned-507206357-balance-inputs.json \
  --fee-clocks evidence/step4-1/pinned-507206357-fee-clocks.json \
  --borrowing-skip evidence/step4-1/pinned-507206357-borrowing-skip.json \
  --funding-selector evidence/step4-1/pinned-507206357-funding-selector.json \
  --prestate-proof evidence/step4-1/pinned-system-transaction-prestate.json \
  --pin-at-prestate-boundary \
  --output evidence/step4-1/stage3d-comparison-run1.json
```

`--deployment` accepts the saved `GmxStep41ObservedDeployment` file directly, so
the preflight runs against the same pinned code identity the sidecar is bound
to, instead of a separately maintained copy of the deployment fields.

### Reproducibility

Step 5 was run twice, against the live chain both times, into
`stage3d-comparison-run1.json` and `stage3d-comparison-run2.json`. The two
reports are **identical except for exactly one field**, `run.utc_run_time`
(`2026-10-10T01:43:02.095665+00:00` and `2026-10-10T01:43:48.696191+00:00`).
With that one field removed, both reports hash to
`b66df4c49613c3855f2429ecc2261f50c0287a77aca50a3efa7dc84b9401a147`. Everything
else — the re-read pin hash before and after the call, the router calldata, the
raw revert bytes, every model input, every proved branch, every digest, and the
comparison result — is byte-identical. The whole-file digests differ only
because of the timestamp: run 1 is
`0xdf23b24a7807f7b0fb47c854b1a3136550115c8f9cbf2f99c201e17315acae25`, run 2 is
`0x60e672248cd2c4da531517b548b2645f3aa9246a39a14ba02195442dcbe8724b`.

## Why the gate is not met

Nine of the fifteen coded MarketIncrease rules are proved:
`pending_latest_request`, `order_field_identity`,
`execute_order_feature_enabled`, `oracle_price_source_and_age`,
`order_valid_from_and_expiration`, `market_and_collateral_token_valid`,
`swap_path_and_min_output`, `max_open_interest`, and
`reserve_and_open_interest_reserve`. The pre-execution equivalence gate is now
satisfied and `observed_execution_prestate_equivalent_to_pre_execution_pin`
appears in `proved_branches`.

Six remain unproved, so the accept-side comparison cannot become an `agreement`:

| Unproved rule | Exactly what would close it |
|---|---|
| `execution_price_and_acceptable_price` | The full `PositionPricingUtils.getPriceImpactUsd` magnitude at the pin: both impact factors and exponents with historical rounding, the positive cap, and the resulting exact token delta and execution price. Today only a nonnegative-impact **upper bound** is proved. |
| `position_fees_and_collateral_sufficiency` | Adaptive funding's saved rate, stable/decrease thresholds, minimum increase rate, maximum funding rate and funding exponent over the proved 54-second interval; the updated per-size funding amounts; the account's referral discount terms; and the post-fee collateral amount. Gross position fee, zero UI fee and zero long borrowing fee are proved but cannot decide the gate alone. |
| `minimum_position_and_collateral` | `MIN_COLLATERAL_USD`, `MIN_COLLATERAL_FACTOR` and the open-interest multiplier are **already captured** in the risk transcript, but the check needs the post-fee collateral from the row above. The minimum **size** check alone is proved and is reported as the separate branch `minimum_position_size_usd`. |
| `post_update_position_validity` | Post-update token size and pending impact, full-close PnL and impact, the max-PnL and liquidation impact factors, and the fee terms `PositionUtils.isPositionLiquidatable` uses. |
| `market_token_balances` | `balanceOf(marketToken)` for WETH and USDC at the pin, every expected-min-balance DataStore component, the collateral sums, and claimable funding for both tokens after the simulated updates. |
| `gas_and_keeper_checks` | The historical `OrderHandler` execution context: `startingGas`, `tx.gasprice`, the gas-limit configuration, the keeper role and the error-handling gas. **A block-boundary preflight cannot expose this**; it needs a different evidence source than a pinned `eth_call`. |

The reject-side route to a `disagreement` is unavailable for a different
reason: it requires our model to *reject* a rule GMX passed, and our model
rejected nothing. That is a finding about this order, not a defect — this order
really did execute.

## Remaining evidence gaps

> **The accept side is structurally unreachable.** Because none of the six
> unproved rules is ever appended to `proved`, `missing_rule_evidence` can never
> be empty and the terminal `agreement` branch is dead code. Capturing the five
> capturable rules does **not** close the completion gate: `gas_and_keeper_checks`
> needs transaction-execution context that a block-boundary `eth_call` never has.
> As the inventory and the preflight design stand today, Step 4.1's gate can be
> met **only** by a `disagreement`. This is a design decision to re-argue, not a
> capture task.

1. **Six unproved rules** (table above). Five are capturable at this same pin
   with more DataStore and impact evidence; `gas_and_keeper_checks` is not
   reachable from a block-boundary preflight at all.
2. **No independent compiler rebuild.** The source identity rests on Sourcify's
   exact-match result plus local content hashes
   (`sourcify_exact_match_and_local_content_hashes_verified: true`), while
   `raw_metadata_cid_preimage_verified` and
   `independent_compiler_rebuild_verified` stay false.
3. **Block context at the pin is not fully equal to the execution context.** The
   prestate proof establishes non-ArbOS contract storage equality and equal
   `block.timestamp`. It does not make `block.number`, `blockhash` or ArbOS
   internal state equal; `l1BlockNumber` demonstrably differs (26020834 at
   507206357 versus 26020836 at 507206358).
4. **`request_at_proposed_pin` is still computed at the creation block.** A
   repin does not recompute the recorded request for the boundary. For this
   order that is harmless and checked: the sidecar compared the recorded request
   with the order read on chain at 507206357 and they agree field for field, and
   the preflight re-read it again. An order updated between creation and the
   boundary would fail that comparison closed rather than pass silently.
5. **No complete long or short lifecycle** has been demonstrated; that is Stage
   4's separate evidence goal and its absence stays visible.
6. **Only one order.** One reproducible same-order run exists; it is not a
   verified comparison, and nothing here says anything about other orders,
   fees, PnL, or a complete lifecycle.

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/v2 -q
# 363 passed, 3 skipped, 122 subtests passed in 227s; 0 failures.
# Stage 3c baseline was 325 passed, 3 skipped, 100 subtests.
```

New focused tests: `tests/v2/test_sidecar_replay.py` (replay of both saved
sidecars plus tamper cases for market identity, position size, position key,
order fields, fixed values, open-interest aggregates, the router gate, the
feature flag, the cell inventory, the transcript digest, a re-digested edit, a
dropped call, two results for one call, a pin at another block, and the
adapter's refusal of a sidecar it cannot replay),
`tests/v2/test_boundary_pin.py` (the repin gate and its refusals), and
`tests/v2/test_pinned_capture.py` (every record recaptured from its saved
responses and replayed by its own verifier, plus endpoint redaction, a refused
write method, chain mismatch, a block that changes mid-capture, a wrong expected
hash, a malformed boolean, a fee clock later than the block, and a contract
without code at the pin).
