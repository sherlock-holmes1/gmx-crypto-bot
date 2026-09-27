# GMX Crypto Bot

Read-only GMX perpetuals research tooling: collect public GMX and chain state,
replay it deterministically, then model execution, collateral, fees, funding,
borrowing, price impact, and liquidation.

## Status

The repository provides an append-only recording format, bounded public GMX
collector, and deterministic observable-state replay. It reduces target-market
orders, oracle prices, configuration, open interest, funding, borrowing,
positions, and fees. The pinned seven-day recording has no gaps or reorgs and
passes reconstructed-state determinism verification. Its embedded block-pinned opening checkpoint reconstructs pre-existing orders
and positions. The observed-order validator passes all 3,614 terminal orders,
including all 103 liquidation settlements and all 3,511 execution-fee proofs. A position simulator is not implemented
yet.

No wallet, private key, signing, order-submission, or live-capital code belongs
in this project without a separate explicit decision and runbook.

## Why this is not the Polymarket bot

GMX orders are committed on-chain and subsequently executed by keepers using
oracle prices. There is no public CLOB queue to join, and a long/short position
does not merge with a complementary token into a fixed payout.

| Reused framework | GMX-specific replacement |
|---|---|
| Raw event recorder | Chain blocks, GMX events, oracle prices, market configuration |
| Deterministic replay | Canonical block/transaction/log ordering and reorg detection |
| Assumption grid | Request-to-execution latency, oracle movement, gas, and slippage scenarios |
| PnL report | Collateral, long/short PnL, fees, funding, borrowing, price impact, liquidation |

## Quick start

The scaffold has no dependencies beyond Python 3.12+.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
read -rsp "Alchemy Arbitrum archive RPC URL: " GMX_ARCHIVE_RPC_URL
echo
export GMX_ARCHIVE_RPC_URL
PYTHONPATH=src python -m gmx_crypto_bot.collector --spec gmx-market-spec-v1.json --output recordings/eth-usdc-week-1
unset GMX_ARCHIVE_RPC_URL
PYTHONPATH=src python -m gmx_crypto_bot.replay recordings/<recording> --verify
PYTHONPATH=src python -m gmx_crypto_bot.validator recordings/<recording> --output recordings/<recording>/order-validation.json
```

### Get an Alchemy archive RPC URL

The opening checkpoint reads historical contract state, so the collector needs
an archive-capable Arbitrum Mainnet endpoint. Alchemy provides archive access
for Arbitrum API keys.

1. Sign in to the [Alchemy Dashboard](https://dashboard.alchemy.com/). A new
   account normally has a default app; otherwise open **Apps**, select
   **Create new app**, and enable **Arbitrum Mainnet**.
2. Open the app, then open **Endpoints**.
3. Copy the **Arbitrum Mainnet HTTP** endpoint, not the WebSocket endpoint. It
   has this form:

   ```text
   https://arb-mainnet.g.alchemy.com/v2/YOUR_API_KEY
   ```

4. Load it without putting the credential in shell history:

   ```bash
   read -rsp "Alchemy Arbitrum archive RPC URL: " GMX_ARCHIVE_RPC_URL
   echo
   export GMX_ARCHIVE_RPC_URL
   ```

5. Run the collector normally. It reads `GMX_ARCHIVE_RPC_URL` automatically;
   no archive command-line parameter is needed. Remove it from the current
   shell afterward with `unset GMX_ARCHIVE_RPC_URL`.

Do not add the URL to `gmx-market-spec-v1.json`, `.env`, source control, command
arguments, or recording files. The collector stores only a redacted provider
URL. See Alchemy's [API-key instructions](https://www.alchemy.com/docs/create-an-api-key)
and [Arbitrum quickstart](https://www.alchemy.com/docs/reference/arbitrum-api-quickstart).

The command exits with status `2` before reading the spec or creating the output
directory if neither `GMX_ARCHIVE_RPC_URL` nor `--archive-rpc-url` is supplied.
The command-line option exists for automation, but the environment variable is
preferred because command arguments may be visible in shell history and process
listings.

## Recording schema

A recording directory contains:

```text
metadata.json     # source, chain, market, configuration and capture bounds
events.jsonl      # self-contained append-only ETH/USD replay events
raw/              # rotated compressed JSONL response bundles + manifest
completeness-report.json # source ranges, explicit gaps, and reorg findings
```

Each event envelope contains `seq`, `kind`, `block_number`,
`transaction_index`, `log_index`, local receipt timestamps, and its replay
payload. Every raw source response is stored before a replay event is derived.
`raw/rpc-000001.jsonl.gz` bundles base64-encoded exact response bodies and rotates
at 256 MiB of uncompressed records. The manifest maps each source request to its
bundle record and SHA-256. Replay events do not reference raw files. Chain events
replay in canonical block/transaction/log order; events without canonical coordinates
remain in arrival order after canonical events.

## Stateful replay

`gmx-replay` reduces target-market order, oracle, configuration, open-interest,
funding, borrowing, position, and fee events into a deterministic state digest.
`--output` writes the complete JSON state and calibration report; ordinary output
is a human summary. Older recordings that predate token metadata can use `--spec`.
The collector writes target-order terminal lifecycle events directly to
`events.jsonl`; ordinary replay reads no compressed raw artifacts.

A bounded window cannot reconstruct positions or pending orders that already
existed at its first block from window events alone. The collector therefore
uses `GMX_ARCHIVE_RPC_URL` (or `--archive-rpc-url`) to read `start_block - 1`
and writes a self-contained `opening_state_checkpoint` row into `events.jsonl`.
It enumerates GMX's active order and position sets, keeps the target market,
and captures opening open interest, cumulative borrowing, and funding state.
Older recordings can still supply an external `--opening-checkpoint` file.

## Collector

`gmx-collect` accepts a pinned `GmxMarketSpec` and a new output directory. It
queries only public JSON-RPC and GMX public HTTP endpoints. It has no wallet,
account, private-key, signing, transaction, or order-submission code.

The ordinary `--rpc-url` handles block resolution, headers, logs, and receipts.
The separate archive URL is used only for block-pinned opening-state calls. Its
credential is redacted in `metadata.json` and is not part of raw RPC request
payloads. The CLI refuses to start without an archive URL, preventing an
apparently complete recording that lacks its opening state.

Alchemy may occasionally return a transient Arbitrum archive error such as
`getStateObject ... layer stale` for one item in a JSON-RPC batch. The collector
keeps successful batch results and retries only the failed read with bounded
backoff. If all retries fail, collection stops instead of writing an incomplete
opening checkpoint. A failed output directory is intentionally retained as
diagnostic evidence and cannot be reused for a new run.

It stores full JSON-RPC and HTTP responses in rotated `raw/rpc-*.jsonl.gz` bundles
and records their request, bundle location, and SHA-256 in `raw/manifest.jsonl`
before it derives replay events. It watches the pinned GMX EventEmitter, DataStore, Oracle,
OrderHandler, and LiquidationHandler contracts. `events.jsonl` retains only the
pinned ETH/USD market events, target-market configuration changes, and WETH/USDC
oracle updates. Unrelated GMX markets remain only in the raw source artifacts.
Receipts and headers are fetched only for retained events.

Large `eth_getLogs` ranges are split recursively. A source failure at a single
block becomes a `data_gap` record and makes `complete` false. A log whose block
hash differs from a freshly read canonical header becomes a `reorg_detected`
record and also makes the recording incomplete. The collector refuses ranges
newer than the selected confirmation depth.

## Historical fee configuration

The validator versions position-fee settings from the complete raw `SetUint`
history and the pinned closing configuration. It reconstructs the balance
improvement flag, including the selected virtual-inventory curve, before choosing
the position-fee tier. Global receiver factors and nonzero UI fees require an
opening archive snapshot; missing values stay unavailable. In `eth-usdc-week-2`,
the block-hash-verified opening snapshot supplies all ten required settings, and
all six historical fee comparisons match all 2,948 ordinary executions. Liquidation fee configuration and independently derived funding/borrowing
accumulators have their separate checks below. Swap configuration has its own
checks.

With `GMX_ARCHIVE_RPC_URL` available in the environment, run:

```bash
PYTHONPATH=src python3 -m gmx_crypto_bot.fee_config_backfill recordings/eth-usdc-week-2
PYTHONPATH=src python3 -m gmx_crypto_bot.validator recordings/eth-usdc-week-2 --output recordings/eth-usdc-week-2/order-validation.json
```

The backfill checks the recorded opening block hash before and after read-only
historical calls. It creates `fee-opening-configuration.json` with storage keys
and raw return values, and refuses to overwrite an existing file. It stores no
RPC URL. The validator checks the snapshot identity, block hash, storage keys,
and values before applying subsequent writes in block/transaction/log order.
Validator exit code 2 means full economic validation remains incomplete.

## Referral and pro discounts

`referral_backfill` reads the historical ReferralStorage address from the pinned
OrderHandler, captures opening trader codes, owners, affiliate tiers, custom
shares, pro tiers, pro factors, and minimum affiliate rewards. It collects the
complete ReferralStorage change-log interval and verifies log block hashes
against the archive provider. Original RPC responses are preserved in a separate
`referral-backfill-*` raw-evidence directory; the recording is unchanged.

Run from a terminal with `GMX_ARCHIVE_RPC_URL` exported:

```bash
PYTHONPATH=src python3 -m gmx_crypto_bot.referral_backfill recordings/eth-usdc-week-2
PYTHONPATH=src python3 -m gmx_crypto_bot.validator recordings/eth-usdc-week-2 --output recordings/eth-usdc-week-2/order-validation.json
```

Historical state reads use Alchemy. Log queries use public Arbitrum RPC by
default; `GMX_LOGS_RPC_URL` can select another log provider. Successful backfills
create `referral-configuration.json` and refuse to overwrite an existing file.
Failures retain raw diagnostics without publishing a complete snapshot.

The seven-day recording passes all four historical referral/pro checks on all
2,948 ordinary executions, with zero mismatches. The archive snapshot contains
839 calls for 368 traders and a complete interval of 563 referral change logs.
All observed pro tiers are zero; nonzero pro-discount overlap is tested with
synthetic cases and is not exercised by this recording.

The validator reconstructs referral identity and discounts at each fee event,
using block, transaction, and log order. It compares trader discounts, affiliate
rewards, pro discounts, and the remaining protocol fee. Missing historical
inputs remain unavailable. Liquidations retain their separate settlement gate.

Formula sources: [ReferralUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/referral/ReferralUtils.sol),
[PositionPricingUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/pricing/PositionPricingUtils.sol),
and [ReferralStorage](https://github.com/gmx-io/gmx-contracts/blob/master/contracts/referrals/ReferralStorage.sol).

## Independent swap validation

The validator reconstructs swap fees, pool and shared virtual inventory, impact
curves, impact-pool caps, and output amounts from opening archive state and
canonical raw events. All four independent comparisons match all 660 hops across 37 markets
(635 orders). The opening snapshot contains 549 archive reads. All fee, impact,
pool-delta, and output comparisons require exact integer equality.

Run from a terminal with `GMX_ARCHIVE_RPC_URL` exported:

```bash
PYTHONPATH=src python3 -m gmx_crypto_bot.swap_backfill recordings/eth-usdc-week-2
PYTHONPATH=src python3 -m gmx_crypto_bot.validator recordings/eth-usdc-week-2 --output recordings/eth-usdc-week-2/order-validation.json
```

The backfill reads market token pairs, pool and impact-pool balances, virtual
market IDs and inventories, both fee/impact factors, exponents, fee-receiver
settings, and UI settings at opening block 505256001. It checks the recorded
block hash before and after calls, preserves original responses, writes
`swap-opening-state.json`, and refuses to overwrite an existing snapshot.

Absent anchors, broken state continuity, missing same-transaction oracle prices,
and changed virtual-market assignments remain unavailable. Swap exponentiation
reproduces PRBMath 2.4.3 integer log2, multiplication, and exp2 rounding; no
comparison tolerance is used for swaps. The recording exercises 377 positive and
283 negative impacts, 226 virtual-curve selections, and two input impact-pool
supplements. The test suite contains 81 tests, including eight recorded rounding
regressions. Historical configuration, liquidation settlement, and keeper-cost proof are
complete for this recording.

Formula sources: [SwapPricingUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/pricing/SwapPricingUtils.sol),
[SwapUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/swap/SwapUtils.sol),
[MarketUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/market/MarketUtils.sol),
and [PRBMath 2.4.3](https://github.com/PaulRBerg/prb-math/tree/v2.4.3/contracts).

## Independent funding, borrowing, and liquidation configuration

All 3,397 funding updates and 6,794 borrowing updates match exact integer
reconstruction from historical settings, pool/open-interest state, prices, and
elapsed time. Both checks cover all 3,051 position executions. All 48 closing
configuration/state values match archive reads. Block timestamps are recovered
from raw RPC headers and verified against execution-log block hashes.

The replay models static/adaptive funding, collateral-specific funding and
claimable amounts, saved-rate evolution and bounds, legacy/kink borrowing, and
the smaller-side exemption. Observed accumulator results are comparison targets,
not inputs to subsequent modeled accumulator updates. Missing evidence,
inconsistent state, or mismatched closing values prevent the gate from closing.

Historical liquidation fee configuration matches all 103 liquidations. One
hundred intact structures match directly. For three erased structures, the model
reconstructs fees and the exact unpaid balance at the insolvency fee step.
Complete liquidation settlement and payout transfers match all 103 liquidations.

For a new recording, from an archive-enabled terminal:

```bash
PYTHONPATH=src python3 -m gmx_crypto_bot.accrual_backfill recordings/eth-usdc-week-2
PYTHONPATH=src python3 -m gmx_crypto_bot.validator recordings/eth-usdc-week-2 --output recordings/eth-usdc-week-2/order-validation.json
```

`accrual_backfill` captures 96 read-only archive values across opening and closing
blocks, checks both hashes before/after calls, preserves raw responses, and
creates `accrual-configuration.json` without overwriting existing evidence.
Subsequent validation is offline. `historical_configuration_complete` is true
only when the historical fee/referral/impact/swap, accrual, closing-state, and
liquidation-configuration checks all pass. Overall validation also requires liquidation settlement and execution-fee proof
to pass; both gates are complete for the seven-day recording.

Sources: [MarketUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/market/MarketUtils.sol),
[PositionPricingUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/pricing/PositionPricingUtils.sol),
and [DecreasePositionCollateralUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/position/DecreasePositionCollateralUtils.sol).

## Execution-fee trace evidence

Collect six representative transaction traces from the terminal containing
`GMX_ARCHIVE_RPC_URL`:

```bash
PYTHONPATH=src python3 -m gmx_crypto_bot.execution_fees.backfill recordings/eth-usdc-week-2
```

The sample covers increases, decreases, user/automatic cancellations, multichain
refunds, and callback orders where present. The collector saves receipts,
transactions, call traces, and historical payment-library bytecode under
`execution-fee-traces/`, with original RPC responses in each capture directory.
Completed transaction files are reused on reruns. Collection checks transaction
and receipt identities against recorded block hashes and rechecks the block
hash after tracing. `--limit 0` selects all applicable transactions.

Opcode probes are optional because full struct-log responses are large. Add
`--gas-probes` to collect GMX's two `GAS` readings from every
`payExecutionFee` frame into `{transaction_hash}.gas-v2.json`; the original RPC
response is preserved in the referenced capture directory. The validator also
supports the sample-calibrated compact-trace profile, bound to the payment-library
runtime hash and 388-byte calldata. It rejects other code or input shapes unless
an opcode probe is present. The profile matches all seven sampled gas readings;
full-population validation matches every positive-fee call in the recording.

Replay the captured evidence offline:

```bash
PYTHONPATH=src python3 -m gmx_crypto_bot.execution_fees.validate recordings/eth-usdc-week-2
```

The validator reads the three gas settings from committed historical DataStore
calls, reconstructs GMX's measured gas from the two probed `GAS` readings or the
sample-calibrated compact-trace profile, and checks the keeper fee against the
transaction gas price with integer rounding.
It also verifies committed native or wrapped-native transfers, corroborates
wrapped-native ERC-20 transfers in receipt logs, and checks multichain refund
credits. Reverted calls and descendants of reverted calls do not count as
payments. Delegate-call values do not count as native transfers. Six sample
transactions cover common execution, cancellation, multichain, and wrapped
refund paths. Full offline validation covers 3,151 transactions, 3,511 orders
joined to validated requests, and 12 additional positive-fee calls joined to
traced fee events. All 3,523 positive-fee calls match the historical gas
settings and keeper fee; keeper and refund transfers are proven. Twelve zero-fee
calls are classified separately. The 12 extra order keys lack entries in the
order-validation table, but their payment calls and receipt transfers are
independently verified.

The main order validator runs this proof against its freshly reconstructed
orders and saved traces. It does not trust `verification.json` or require an
older `order-validation.json` as an input. All 3,511 fee-paying terminal orders
must have exactly one matching payment proof; 103 zero-fee liquidations are not
applicable to execution fees. Missing trace/gas evidence keeps validation
incomplete. Corrupt evidence, changed block identities, omitted/duplicate
payments, or failed transfer proofs produce mismatches. The 12 additional proven
payment calls retain their separate unindexed-order reconciliation status.

## Liquidation settlement

The seven-day recording matches all 103 liquidations: 100 solvent closes and
three fee-step insolvent closes. The expanded referral snapshot covers all
liquidation-only traders. All 3,614 terminal orders match with zero mismatches
and decode errors. The test suite passes 116 tests.

`liquidation_settlement` reconstructs full-close cash flows in GMX payment order:
funding, loss, fees, negative impact, then impact-cap difference. It stops at the
first insolvent payment, checks the exact `InsolventClose` step and unpaid USD,
and checks erased fees while retaining claimable funding. Position size and
collateral must close to zero; released outputs must match recipient transfers.
Multichain payouts require both the vault transfer and matching account/chain
credit. Native payouts require a committed withdrawal and recipient CALL from
the saved transaction trace; a WETH burn alone is insufficient.

Funding, borrowing, position/liquidation fees, UI fees, and referral/pro discounts
come from historical inputs. Cash comparisons use exact integers. Observed price
impact is admitted only after the separate independent-impact check passes its
existing tolerance. Unsupported capped-PnL or output-swap paths, missing opening
state, and missing receipt/trace evidence remain unavailable. This verifies
settlement of observed liquidations; it does not model liquidation eligibility.

Older referral snapshots exclude liquidation-only traders. With
`GMX_ARCHIVE_RPC_URL` exported, collect the complete expanded snapshot:

```bash
PYTHONPATH=src python -m gmx_crypto_bot.referral_backfill recordings/eth-usdc-week-2 --include-liquidations
PYTHONPATH=src python -m gmx_crypto_bot.validator recordings/eth-usdc-week-2 --output recordings/eth-usdc-week-2/order-validation.json
```

The backfill writes `liquidation-referral-configuration.json` and preserves the
original snapshot and raw recording. It includes ordinary and liquidation
traders and their referral/pro dependencies. The validator prefers this expanded
snapshot and checks its identity and historical evidence before using it.
Subsequent validation is offline. An existing expanded snapshot is never
overwritten. `liquidation_settlement` leaves the remaining-check list only when
all applicable orders match.

Settlement rules: [GMX DecreasePositionCollateralUtils](https://github.com/gmx-io/gmx-synthetics/blob/main/contracts/position/DecreasePositionCollateralUtils.sol).

## Roadmap

1. Completed — Fix the target surface.
2. Completed — Build the read-only GMX collector.
3. Completed — Build deterministic GMX replay.
   1. Completed — Validate reconstructed execution against observed orders in the seven-day recording.
   2. Next — Understand GMX perpetuals architecture.
4. Replace the Polymarket simulator.
   1. Cross-check reconstructed execution with SimulationRouter.
5. Calibrate before interpreting results.
6. Evaluate a strategy only after calibration.

Technical references: [GMX architecture](https://docs.gmx.io/docs/api/contracts/architecture/), [fees](https://docs.gmx.io/docs/trading/fees/), and [liquidations](https://docs.gmx.io/docs/trading/liquidations/).
