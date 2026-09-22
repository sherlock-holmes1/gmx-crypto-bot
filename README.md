# GMX Crypto Bot

Read-only GMX perpetuals research tooling: collect public GMX and chain state,
replay it deterministically, then model execution, collateral, fees, funding,
borrowing, price impact, and liquidation.

## Status

The repository provides an append-only recording format, bounded public GMX
collector, and deterministic observable-state replay. It reduces target-market
orders, oracle prices, configuration, open interest, funding, borrowing,
positions, and fees. The pinned seven-day recording has no gaps or reorgs and
passes reconstructed-state determinism verification. It remains state-incomplete
without a block-pinned opening checkpoint for pre-existing orders and positions.
The observed-order validator is active. A position simulator is not implemented
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
PYTHONPATH=src python -m gmx_crypto_bot.replay recordings/<recording> --spec gmx-market-spec-v1.json --output replay-report.json
PYTHONPATH=src python -m gmx_crypto_bot.validator recordings/<recording> --output recordings/<recording>/order-validation.json


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

## Roadmap

1. Completed — Fix the target surface.
2. Completed — Build the read-only GMX collector.
3. Completed — Build deterministic GMX replay.
   1. Active — Validate reconstructed execution against observed orders.
   2. Next — Understand GMX perpetuals architecture.
4. Replace the Polymarket simulator.
   1. Cross-check reconstructed execution with SimulationRouter.
5. Calibrate before interpreting results.
6. Evaluate a strategy only after calibration.

Technical references: [GMX architecture](https://docs.gmx.io/docs/api/contracts/architecture/), [fees](https://docs.gmx.io/docs/trading/fees/), and [liquidations](https://docs.gmx.io/docs/trading/liquidations/).
